"""Publish tiny saved predictions end-to-end without constructing or fitting a model."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError, digest
from bef_redbird.research.e2_experiment import publish
from bef_redbird.research.e2_metrics import calibration_rows, freeze_predictions, score_frame
from bef_redbird.research.e2_result_view import load_e2_run
from bef_redbird.research.training_contract import atomic_json, pin

METHODS = {
    "direction": [
        "no_change",
        "historical_frequency",
        "repeat_last",
        "logistic_own",
        "logistic_package",
        "logistic_group",
        "catboost_own",
        "catboost_package",
        "catboost_group",
    ],
    "magnitude": ["no_change", "historical_median", "catboost_group_mae"],
    "reversal": ["persistence", "historical_frequency", "catboost_group"],
}


def synthetic_publish_root(root: Path) -> Path:
    root.mkdir(parents=True)
    protocol = {
        "version": "synthetic-e2",
        "history_choices": [1, 3, "expanding"],
        "fits": {"maximum_learned_fits": 27},
        "evaluation": [
            {
                "name": f"evaluation_{year}",
                "start": f"{year}-01-01",
                "end": f"{year + 1}-01-01",
                "fit_cutoff": f"{year}-01-01",
            }
            for year in (2024, 2025, 2026)
        ],
        "dataset": {"synthetic": True},
        "groups": {"synthetic": True},
        "weighting": "equal period Brier",
        "ranking": "all eligible before outcomes",
        "targets": {"magnitude": "DKK/package", "reversal": "three classes"},
        "limitations": ["synthetic"],
        "exposure": "Synthetic research fixture only.",
    }
    atomic_json(root / "protocol.json", protocol)
    atomic_json(
        root / "freeze-receipt.json", {"protocol": pin(root / "protocol.json", root, "protocol")}
    )
    plan = [
        (f"screen-{history}", "direction", "catboost_group", "validation", 2022, history)
        for history in protocol["history_choices"]
    ]
    plan += [
        (f"evaluation_{year}-{task}-{method}", task, method, f"evaluation_{year}", year, 1)
        for year in (2024, 2025, 2026)
        for task, methods in METHODS.items()
        for method in methods
    ]
    for trial_id, task, method, split, year, history in plan:
        path = root / "trials" / trial_id
        path.mkdir(parents=True)
        candidates = pd.DataFrame(
            [
                {
                    "row_key": f"{year}-{period}-{package}",
                    "package_number": f"{package:06}",
                    "period_index": period,
                    "period_start": pd.Timestamp(f"{year}-01-{8 + 14 * period:02}"),
                    "identity_episode_id": f"episode-{package}",
                    "aip_ore": 10000,
                    "history_periods": 10,
                    "lag1_delta_ore": 100,
                    "peer_count": 2,
                    "peer_feature_state": "complete_equal_size_peers",
                }
                for period in (0, 1)
                for package in (1, 2)
            ]
        )
        predictions = freeze_predictions(
            candidates,
            np.zeros(len(candidates))
            if task == "magnitude"
            else np.tile([0.2, 0.3, 0.5], (len(candidates), 1)),
            task=task,
            seed=42,
        )
        outcomes = predictions.copy()
        outcomes["next_status"] = ["observed", "exit", "observed", "exit"]
        outcomes["next_direction"] = [1, None, -1, None]
        outcomes["next_delta_ore"] = [1000, None, -1000, None]
        outcomes["reversal_status"] = outcomes.next_status
        outcomes["reversal_category"] = ["full_reversal", None, "persistence", None]
        predictions.to_parquet(path / "predictions.parquet", index=False)
        outcomes.to_parquet(path / "outcomes.parquet", index=False)
        aggregate, periods = score_frame(outcomes, task, split, method)
        atomic_json(
            path / "metrics.json",
            {
                "aggregate": aggregate,
                "per_period": periods,
                "calibration": []
                if task == "magnitude"
                else calibration_rows(outcomes, task, split, method),
            },
        )
        atomic_json(
            path / "trial.json",
            {
                "trial_id": trial_id,
                "task": task,
                "method": method,
                "split": split,
                "status": "completed",
                "history": history,
                "training_rows": 4,
                "protocol_sha256": digest(root / "protocol.json"),
                "prediction_keys_sha256": hashlib.sha256(
                    "\n".join(predictions.row_key).encode()
                ).hexdigest(),
                "resource": {"elapsed_seconds": 0.01, "sampled_peak_rss_bytes": 10000},
                "files": [
                    pin(path / name, path, role)
                    for name, role in (
                        ("predictions.parquet", "predictions"),
                        ("outcomes.parquet", "outcomes"),
                        ("metrics.json", "metrics"),
                    )
                ],
            },
        )
    return root


def test_complete_publisher_result_opens_with_curves_periods_and_paired_intervals(
    tmp_path: Path,
) -> None:
    root = synthetic_publish_root(tmp_path / "run")
    publish(root)
    run = load_e2_run(root / "saved" / "result.json")
    assert len(run.artifacts["coverage"]["trial_statuses"]) == 48
    assert len(run.artifacts["history_curves"]["experiments"]) == 3
    assert any(row["split"] == "evaluation_all" for row in run.artifacts["metrics"]["per_period"])
    assert run.artifacts["metrics"]["uncertainty"]
    assert run.artifacts["comparisons"]["incremental"]


@pytest.mark.parametrize("status", ["missing", "running"])
def test_missing_or_unfinished_trial_never_publishes_completed_result(
    tmp_path: Path,
    status: str,
) -> None:
    root = synthetic_publish_root(tmp_path / "run")
    receipt = root / "trials" / "evaluation_2024-direction-logistic_own" / "trial.json"
    if status == "missing":
        receipt.unlink()
    else:
        value = json.loads(receipt.read_text())
        value["status"] = status
        atomic_json(receipt, value)
    with pytest.raises(ResearchError, match="inventory_incomplete"):
        publish(root)
    assert not (root / "saved" / "result.json").exists()


def test_failed_trial_is_retained_in_pinned_statuses(tmp_path: Path) -> None:
    root = synthetic_publish_root(tmp_path / "run")
    receipt = root / "trials" / "evaluation_2024-direction-logistic_own" / "trial.json"
    value: dict[str, Any] = json.loads(receipt.read_text())
    value.update(status="failed", error="synthetic_resource_limit")
    atomic_json(receipt, value)
    publish(root)
    run = load_e2_run(root / "saved" / "result.json")
    assert run.manifest["execution_status"] == "declared_trials_finished_with_failures"
    failed = [r for r in run.artifacts["coverage"]["trial_statuses"] if r["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["error"] == "synthetic_resource_limit"


def test_changed_completed_trial_artifact_is_rejected_before_publish(tmp_path: Path) -> None:
    root = synthetic_publish_root(tmp_path / "run")
    artifact = root / "trials" / "evaluation_2024-direction-logistic_own" / "metrics.json"
    artifact.write_text("{}")
    with pytest.raises(ResearchError, match="artifact_pin_changed"):
        publish(root)
    assert not (root / "saved" / "result.json").exists()


def test_pending_terminal_period_retains_coverage_but_not_paired_score_denominator(
    tmp_path: Path,
) -> None:
    root = synthetic_publish_root(tmp_path / "run")
    for path in (root / "trials").glob("evaluation_2026-*"):
        receipt = json.loads((path / "trial.json").read_text())
        outcomes = pd.read_parquet(path / "outcomes.parquet")
        tail = outcomes.period_start == outcomes.period_start.max()
        outcomes.loc[tail, ["next_status", "reversal_status"]] = "pending_horizon"
        outcomes.loc[tail, ["next_direction", "next_delta_ore", "reversal_category"]] = None
        outcomes.to_parquet(path / "outcomes.parquet", index=False)
        task, split, method = (receipt[key] for key in ("task", "split", "method"))
        aggregate, periods = score_frame(outcomes, task, split, method)
        atomic_json(
            path / "metrics.json",
            {
                "aggregate": aggregate,
                "per_period": periods,
                "calibration": []
                if task == "magnitude"
                else calibration_rows(outcomes, task, split, method),
            },
        )
        receipt["files"] = [
            pin(path / item["path"], path, item["role"]) for item in receipt["files"]
        ]
        atomic_json(path / "trial.json", receipt)
    publish(root)
    run = load_e2_run(root / "saved" / "result.json")
    coverage = run.artifacts["coverage"]
    assert coverage["overall"]["periods"] == 6
    assert coverage["per_period"][-1]["observed"] == 0
    assert coverage["per_period"][-1]["status_counts"] == {"pending_horizon": 2}
    assert all(row["periods"] == 5 for row in run.artifacts["metrics"]["uncertainty"])
    aggregate = next(
        row
        for row in run.artifacts["metrics"]["aggregate"]
        if row["split"] == "evaluation_all" and row["task"] == "direction"
    )
    assert aggregate["periods"] == 6 and aggregate["scored_periods"] == 5
