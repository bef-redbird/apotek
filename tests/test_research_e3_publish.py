"""Independent tiny saved-only publication and replay, never a real model fit."""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import catboost
import duckdb
import numpy as np
import pandas as pd
import pytest

from bef_redbird.research import e3_publish as publisher
from bef_redbird.research.dataset import ResearchError, digest
from bef_redbird.research.e3_data import SCENARIOS
from bef_redbird.research.e3_experiment import DIRECTION, FOLDS
from bef_redbird.research.e3_methods import select_queue
from bef_redbird.research.e3_result_view import load_candidate_review, load_e3_run
from bef_redbird.research.group_features import pin
from bef_redbird.research.training_contract import KEYS, atomic_json


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _cohort(years: list[int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    records = []
    for year in years:
        for period in range(3):
            day = pd.Timestamp(year=year, month=2, day=1 + period * 7)
            for package in range(6):
                records.append(
                    {
                        "row_key": f"{year}-{period}-{package}",
                        "package_number": f"{package:06}",
                        "period_index": (year - 2022) * 26 + period,
                        "period_start": day,
                        "identity_episode_id": f"episode-{package}",
                        "target_period": pd.Timestamp(day.to_pydatetime() + timedelta(days=14)),
                        "aip_ore": [5000, 10000, 100000, 5000, 20000, 150000][package],
                        "history_periods": package + 2,
                        "abc": ["A", "B", "C", "A", "B", "C"][package],
                        "advance_abc": ["C", "C", "A", None, "B", "C"][package],
                        "advance_state": "observed"
                        if package != 3
                        else "missing_final_classification",
                        "advance_source": "synthetic-native",
                        "origin_group": "g" + str(package // 2),
                        "peer_count": [2, 1, 0, None, 1, 0][package],
                        "peer_feature_state": [
                            "complete_equal_size_peers",
                            "complete_equal_size_peers",
                            "complete_equal_size_singleton",
                            "unobserved_group",
                            "complete_equal_size_peers",
                            "complete_equal_size_singleton",
                        ][package],
                        "broad_peer_count": [3, 3, 0, 1, None, 1][package],
                        "broad_peer_different_size_count": [1, 1, 0, 0, None, 1][package],
                        "broad_peer_feature_state": [
                            "complete_same_unit_peers",
                            "partial_same_unit_peers",
                            "supported_quantity_singleton",
                            "incomplete_or_uncertain_group",
                            "own_quantity_unknown",
                            "partial_same_unit_peers",
                        ][package],
                    }
                )
    features = pd.DataFrame(records)
    labels = features[list(KEYS)].copy()
    labels["next_status"] = ["observed" if n % 6 != 3 else "unobserved" for n in range(len(labels))]
    labels["next_direction"] = [([-1, 0, 1, None, 1, -1][n % 6]) for n in range(len(labels))]
    labels["next_delta_ore"] = labels.next_direction * 500
    labels["next_aip_ore"] = features.aip_ore + labels.next_delta_ore
    labels["next_maturity_date"] = features.target_period
    labels["next_maturity_index"] = features.period_index + 1
    return features, labels


def synthetic_root(root: Path, *, e2_root: Path | None = None) -> Path:
    root.mkdir()
    e2 = e2_root or root.parent / "e2"
    e2.mkdir()
    atomic_json(
        e2 / "result.json",
        {"schema_version": "e2-saved-results-v1", "status": "completed", "evaluation_locked": True},
    )
    e2_trials: list[dict[str, Any]] = []
    cohorts = {}
    for fold in FOLDS:
        year = int(fold["start"][:4])
        features, labels = _cohort([2022, 2023] if fold["name"] == "validation" else [year])
        excluded = labels.iloc[[0]].copy()
        excluded["row_key"] = "excluded-" + excluded.row_key
        excluded["package_number"] = "999999"
        labels = pd.concat([labels, excluded], ignore_index=True)
        cohorts[fold["name"]] = features, labels
        directory = root / "cohorts" / fold["name"]
        directory.mkdir(parents=True)
        features.to_parquet(directory / "features.parquet", index=False)
        labels.to_parquet(directory / "outcomes.parquet", index=False)
        atomic_json(
            directory / "manifest.json",
            {role: pin(directory / f"{role}.parquet") for role in ("features", "outcomes")},
        )
        if fold["name"] == "validation":
            continue
        for task, methods in (
            ("direction", ["no_change", "historical_frequency", "repeat_last", "catboost_group"]),
            ("magnitude", ["no_change", "historical_median", "catboost_group_mae"]),
        ):
            for method in methods:
                trial_id = f"{fold['name']}-{task}-{method}"
                directory = e2 / "trials" / trial_id
                directory.mkdir(parents=True)
                pred = features[[*KEYS, "aip_ore"]].copy()
                if task == "direction":
                    pred[["p0", "p1", "p2"]] = np.tile([0.2, 0.3, 0.5], (len(pred), 1))
                    # A preserved original selection intentionally differs from new seed ties.
                    pred["selected"] = np.arange(len(pred)) % 6 == 1
                else:
                    pred["prediction_dkk"] = 0.0
                    if method == "catboost_group_mae":
                        pred.loc[0, "prediction_dkk"] = -1000.0
                pred.to_parquet(directory / "predictions.parquet", index=False)
                outcome = pred.merge(labels, on=list(KEYS), validate="one_to_one")
                outcome.to_parquet(directory / "outcomes.parquet", index=False)
                files = [
                    {**pin(directory / f"{role}.parquet"), "path": f"{role}.parquet", "role": role}
                    for role in ("predictions", "outcomes")
                ]
                trial: dict[str, Any] = {
                    "trial_id": trial_id,
                    "status": "completed",
                    "files": files,
                }
                atomic_json(directory / "trial.json", trial)
                e2_trials.append(trial)
    atomic_json(e2 / "trial-ledger.json", {"trials": e2_trials})
    sidecars = {}
    for role in ("advance", "broader"):
        directory = root.parent / role
        directory.mkdir()
        payload = directory / "features.fixture"
        payload.write_bytes(b"synthetic immutable sidecar bytes")
        atomic_json(directory / "manifest.json", {"files": [pin(payload)]})
        sidecars[role] = pin(directory / "manifest.json")
    (root / "producer.zip").write_bytes(b"synthetic immutable main producer bytes")
    protocol = {
        "folds": FOLDS,
        "direction": DIRECTION,
        "exposure": "Synthetic exposed fixture",
        "information_boundary": "Synthetic single-period lead only",
        "pins": {
            "e2_ledger": pin(e2 / "trial-ledger.json"),
            "e2_display": pin(e2 / "result.json"),
            **sidecars,
        },
        "producer": pin(root / "producer.zip"),
        "exact_origins": {
            fold: [{"start": str(day)[:10]} for day in features.period_start.unique()]
            for fold, (features, _) in cohorts.items()
        },
    }
    atomic_json(root / "protocol.json", protocol)
    atomic_json(root / "freeze-receipt.json", {"protocol": pin(root / "protocol.json")})
    selection = {
        "protocol_sha256": digest(root / "protocol.json"),
        "selected_before_evaluation": True,
        "monetary_formulation": "delta_floor",
        "calibration_2023": {"temperature": 1.1},
        "queue_policies": {
            "choices": [
                {"policy": "risk_rank", "status": "selected", "selected_parameter": 0.5},
                {"policy": "threshold", "status": "selected", "selected_parameter": [0.25, 0.2]},
            ]
        },
    }
    atomic_json(root / "selection.json", selection)
    atomic_json(root / "selection-receipt.json", {"selection": pin(root / "selection.json")})
    trials = []
    for fold in FOLDS:
        features, _ = cohorts[fold["name"]]
        specs = [(spec, "direction") for spec in DIRECTION]
        for formulation in (
            ["delta_floor", "log_ratio"] if fold["name"] == "validation" else ["delta_floor"]
        ):
            specs.extend(
                (
                    {
                        "method": f"amount_{formulation}_{info}",
                        "advance": info == "advance",
                        "formulation": formulation,
                    },
                    "magnitude",
                )
                for info in ("current", "advance")
            )
        for spec, task in specs:
            trial_id = f"{fold['name']}-{spec['method']}"
            directory = root / "trials" / trial_id
            directory.mkdir(parents=True)
            pred = features[["row_key"]].copy()
            for scenario in list(SCENARIOS) if spec["advance"] else ["perfect"]:
                if task == "direction":
                    pred[[f"p{k}_{scenario}" for k in range(3)]] = np.tile(
                        [0.15, 0.35, 0.5], (len(pred), 1)
                    )
                else:
                    pred[f"next_aip_dkk_{scenario}"] = features.aip_ore / 100 + 2
            pred.to_parquet(directory / "predictions.parquet", index=False)
            trial = {
                "trial_id": trial_id,
                "method": spec["method"],
                "task": task,
                "fold": fold["name"],
                "spec": spec,
                "status": "completed",
                "protocol_sha256": digest(root / "protocol.json"),
                "new_fit": True,
                "resource": {"elapsed_seconds": 0.1, "sampled_peak_rss_bytes": 100},
                "files": [
                    {**pin(directory / "predictions.parquet"), "path": "predictions.parquet"}
                ],
            }
            atomic_json(directory / "trial.json", trial)
            trials.append(trial)
    atomic_json(root / "trial-ledger.json", {"trials": trials})
    atomic_json(
        root / "execution.json",
        {
            "status": "completed",
            "trials": len(trials),
            "new_main_fits": len(trials),
            "resource": {},
            "failures": [],
        },
    )
    (root / "publisher-producer.zip").write_bytes(b"synthetic retained v1 publisher source bytes")
    atomic_json(
        root / "publisher-freeze.json",
        {
            "protocol": pin(root / "protocol.json"),
            "publisher_source": pin(root / "publisher-producer.zip"),
            "specification": {"version": "e3-publisher-v1"},
        },
    )
    return root


def test_origin_routing_and_transition_remain_outcome_independent() -> None:
    features, _ = _cohort([2024])
    variants = []
    for p in ([1, 0, 0], [0, 1, 0], [0, 0, 1]):
        frame = features.copy()
        frame[["p0", "p1", "p2"]] = np.tile(p, (len(frame), 1))
        variants.append(frame)
    equal, broad = publisher.route_predictions(*variants)
    assert equal.route[:6].tolist() == ["equal", "equal", "package", "package", "equal", "package"]
    assert broad.route[:6].tolist() == ["broad", "broad", "package", "package", "equal", "broad"]
    reference = publisher.transition_reference(features)
    assert reference[["p0", "p1", "p2"]].to_numpy().argmax(axis=1)[:6].tolist() == [
        2,
        2,
        0,
        1,
        1,
        1,
    ]
    mutated = features.assign(next_direction=999, next_aip_ore=999, next_status="future")
    for key, mask in publisher.origin_masks(features).items():
        np.testing.assert_array_equal(mask, publisher.origin_masks(mutated)[key])


def test_equal_period_metrics_and_matched_workload() -> None:
    frame = pd.DataFrame(
        {
            "period_start": pd.to_datetime(["2024-01-01"] + ["2024-01-15"] * 3),
            "next_status": "observed",
            "next_direction": [1, 0, 0, 0],
            "p0": 0.0,
            "p1": 1.0,
            "p2": 0.0,
        }
    )
    rows = publisher.metric_rows(frame, "direction", {"method": "test", "task": "direction"})
    assert rows[0]["brier"] == 1.0  # (2 + 0) / two periods
    assert rows[0]["pooled_brier"] == 0.5
    frame["row_key"] = [str(index) for index in frame.index]
    queue = select_queue(frame, "top10", workload={"2024-01-01": 0, "2024-01-15": 2})
    assert queue.groupby("period_start").selected.sum().tolist() == [0, 2]
    replayed = publisher.metric_rows(
        frame, "direction", {"method": "test", "task": "direction"}, period_rows=rows[1:]
    )
    # Membership was added after the original calculation; compare original metrics.
    assert replayed[0]["brier"] == rows[0]["brier"]
    assert replayed[0]["pooled_brier"] == rows[0]["pooled_brier"]


def test_publish_saved_schema_bindings_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("saved publication/replay attempted model or database execution")

    monkeypatch.setattr(catboost.CatBoostClassifier, "fit", forbidden)
    monkeypatch.setattr(catboost.CatBoostRegressor, "fit", forbidden)
    monkeypatch.setattr(duckdb, "connect", forbidden)
    original_e3 = tmp_path / "original-e3"
    original_e3.mkdir()
    root = synthetic_root(original_e3 / "run", e2_root=tmp_path / "e2")
    v1_hashes = {
        name: digest(root / name) for name in ("publisher-freeze.json", "publisher-producer.zip")
    }
    result = publisher.publish(root)
    run = load_e3_run(result)
    review = load_candidate_review(run)
    revealed = load_candidate_review(run, reveal_outcomes=True)
    assert len(review) == 54
    assert "next_aip_ore" not in review
    assert set(review.scenario) == {"perfect"}
    assert set(review.policy) == {"risk_rank"}
    assert review.row_key.is_unique
    assert len(revealed) == len(review)
    assert run.artifacts["comparisons"]["binding"]["verified"] is True
    original = pd.read_parquet(
        root / "derived/queues/evaluation_2024--perfect--e2_top10--natural.parquet"
    )
    assert original.loc[original.selected, "row_key"].str.endswith("-1").all()
    e2_amount = next(
        row
        for row in run.artifacts["summary"]["rows"]
        if row["method"] == "e2_magnitude_catboost_group_mae"
        and row["split"] == "evaluation_all"
        and row["subgroup"] == "all"
        and "period_start" not in row
    )
    assert e2_amount["invalid"] == 3
    assert any(
        row.get("scenario") == "missing100" and row.get("metric") == "brier"
        for row in run.artifacts["comparisons"]["rows"]
    )
    assert all(
        row["subgroup"] == "all"
        for row in run.artifacts["scenarios"]["rows"]
        if row["scenario"] != "perfect"
    )
    assert all(
        not isinstance(value, (dict, list, tuple))
        for row in run.artifacts["queue_tradeoffs"]["rows"]
        for value in row.values()
    )
    coverage = run.artifacts["scenarios"]["input_coverage"]
    assert coverage["feature_only"] is True and coverage["outcomes_read"] is False
    missing = next(
        row
        for row in coverage["rows"]
        if row["split"] == "evaluation_all" and row["scenario"] == "missing100"
    )
    assert missing["candidates"] == 54
    assert missing["original_available_advance_codes"] == missing["removed_codes"] == 45
    assert missing["retained_codes"] == missing["incorrect_codes"] == 0
    assert missing["unavailable_codes"] == 54
    assert v1_hashes == {name: digest(root / name) for name in v1_hashes}
    assert run.manifest["definitions"]["version"] == "e3-publisher-v3"
    assert "pooled_max_error_dkk" in run.manifest["definitions"]["metric_aggregation"]
    with pytest.raises(ResearchError, match="unused_output"):
        publisher.publish(root)

    # A replay must never refit even the permitted earlier-outcome calibrators.
    monkeypatch.setattr(publisher, "temperature_fit", forbidden)
    monkeypatch.setattr(publisher, "residual_fit", forbidden)
    restored_e3 = tmp_path / "restored-e3"
    shutil.copytree(original_e3, restored_e3)
    restored_root = restored_e3 / "renamed-run"
    unavailable = tmp_path / "unavailable-original-e3"
    for path in (restored_e3 / "run", restored_root, original_e3, unavailable):
        assert path.resolve().is_relative_to(tmp_path.resolve())
    (restored_e3 / "run").rename(restored_root)
    original_e3.rename(unavailable)
    assert not original_e3.exists()
    with pytest.raises(ResearchError, match="requires_explicit_root"):
        publisher.publish(restored_root, tmp_path / "no-relocation")
    replay = publisher.publish(restored_root, tmp_path / "replay", relocate_from=original_e3)
    replay_run = load_e3_run(replay)
    for role in run.artifacts:
        assert run.artifacts[role] == replay_run.artifacts[role]
    assert (
        _read(tmp_path / "replay/derived/calibration-receipt.json")["replay_reuses_saved_states"]
        is True
    )
    assert replay_run.manifest["source_binding"] == run.manifest["source_binding"]
    assert replay_run.manifest["run_id"] == run.manifest["run_id"]
    assert v1_hashes == {name: digest(restored_root / name) for name in v1_hashes}
    (restored_e3 / "advance/features.fixture").write_bytes(b"tampered relocated sidecar")
    with pytest.raises(ResearchError, match="artifact_pin_changed"):
        publisher._source_binding(
            restored_root,
            _read(restored_root / "protocol.json"),
            publisher.Relocation(restored_root, original_e3),
        )


def test_freeze_changed_publisher_and_source_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = synthetic_root(tmp_path / "run")
    publisher.freeze_publisher(root)
    monkeypatch.setattr(publisher, "SPECIFICATION", {"changed": True})
    with pytest.raises(ResearchError, match="changed_after_freeze"):
        publisher.freeze_publisher(root)


def test_outcome_mismatch_rejected_even_when_e2_pin_is_refreshed(tmp_path: Path) -> None:
    root = synthetic_root(tmp_path / "run")
    protocol = _read(root / "protocol.json")
    path = tmp_path / "e2/trials/evaluation_2024-direction-no_change/outcomes.parquet"
    frame = pd.read_parquet(path)
    frame["next_aip_ore"] = frame.next_aip_ore.to_numpy(dtype=float) + 1
    frame.to_parquet(path, index=False)
    ledger = _read(tmp_path / "e2/trial-ledger.json")
    trial = next(
        row for row in ledger["trials"] if row["trial_id"] == "evaluation_2024-direction-no_change"
    )
    item = next(row for row in trial["files"] if row["role"] == "outcomes")
    item.update({**pin(path), "path": path.name})
    atomic_json(path.parent / "trial.json", trial)
    atomic_json(tmp_path / "e2/trial-ledger.json", ledger)
    protocol["pins"]["e2_ledger"] = pin(tmp_path / "e2/trial-ledger.json")
    with pytest.raises(ResearchError, match="cohort_or_outcomes_changed"):
        publisher.reconcile_e2(root, protocol)


def test_paired_origins_cannot_silently_intersect() -> None:
    with pytest.raises(ResearchError, match="scored_origins_changed"):
        publisher.paired_comparison(
            [{"period_start": "2024-01-01", "brier": 0.1}],
            [{"period_start": "2024-01-15", "brier": 0.2}],
            metric="brier",
            metadata={},
        )


def test_later_outcomes_cannot_change_earlier_calibration_queue_or_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source: dict[str, pd.DataFrame] = {}
    for year in (2023, 2024, 2025):
        features, labels = _cohort([year])
        frame = features.merge(labels, on=list(KEYS), validate="one_to_one")
        frame[["p0", "p1", "p2"]] = np.tile([0.15, 0.25, 0.6], (len(frame), 1))
        frame["next_aip_dkk"] = frame.aip_ore / 100 + 2
        source["validation" if year == 2023 else f"evaluation_{year}"] = frame

    def saved(root: Path, fold: str, method: str, **kwargs: Any) -> pd.DataFrame:
        return source[fold].copy()

    monkeypatch.setattr(publisher, "read_prediction", saved)
    root = tmp_path / "causal"
    root.mkdir()
    first, second = root / "first", root / "second"
    first.mkdir()
    second.mkdir()
    selection = {"calibration_2023": {"temperature": 1.1}, "monetary_formulation": "delta_floor"}
    before = publisher.calibration_states(root, first, selection, {})
    changed = source["evaluation_2025"]
    changed["next_aip_ore"] = changed.next_aip_ore + 10000
    changed["next_direction"] = 1.0
    after = publisher.calibration_states(root, second, selection, {})
    for annual_year in ("2024", "2025"):
        assert before["temperature"][annual_year] == after["temperature"][annual_year]
        for info in ("current", "advance"):
            assert (
                before["residual"][f"{annual_year}_{info}"]
                == after["residual"][f"{annual_year}_{info}"]
            )
        forecast = source[f"evaluation_{annual_year}"]
        a, b = forecast.copy(), forecast.copy()
        for value, states in ((a, before), (b, after)):
            value[["p0", "p1", "p2"]] = publisher.calibrate(
                value[["p0", "p1", "p2"]].to_numpy(), states["temperature"][annual_year]
            )
        pd.testing.assert_series_equal(
            select_queue(a, "risk_rank", 0.5).selected, select_queue(b, "risk_rank", 0.5).selected
        )
        interval_a = publisher.intervals(a, before["residual"][f"{annual_year}_advance"])
        interval_b = publisher.intervals(b, after["residual"][f"{annual_year}_advance"])
        pd.testing.assert_frame_equal(
            interval_a[["lower90_dkk", "upper90_dkk"]], interval_b[["lower90_dkk", "upper90_dkk"]]
        )
    assert before["residual"]["2026_advance"] != after["residual"]["2026_advance"]
    assert before["temperature"]["2026"] != after["temperature"]["2026"]


def test_cached_bootstrap_is_identical_to_frozen_e2_definition() -> None:
    for size in (3, 21, 72):
        values = np.linspace(-0.4, 0.7, size)
        for block in (4, 8):
            assert publisher._interval(values, block) == publisher.paired_interval(
                values, seed=20261026, block=block, repetitions=2000
            )


def test_queue_unavailable_rate_counts_include_both_undefined_origins() -> None:
    candidate: list[dict[str, Any]] = [
        {"period_start": "a", "rise_rate": 0.4},
        {"period_start": "b", "rise_rate": None},
        {"period_start": "c", "rise_rate": None},
    ]
    baseline: list[dict[str, Any]] = [
        {"period_start": "a", "rise_rate": 0.3},
        {"period_start": "b", "rise_rate": 0.2},
        {"period_start": "c", "rise_rate": None},
    ]
    rows = publisher.paired_comparison(
        candidate, baseline, metric="rise_rate", metadata={}, allow_missing_rates=True
    )
    assert rows[0]["total_origin_periods"] == 3
    assert rows[0]["joint_rate_periods"] == 1
    assert rows[0]["periods_without_joint_rate"] == 2
    assert rows[0]["status"] == "insufficient_paired_periods"


def test_failed_required_trial_retains_unscored_reason(tmp_path: Path) -> None:
    root = synthetic_root(tmp_path / "run")
    ledger = _read(root / "trial-ledger.json")
    trial = next(
        row for row in ledger["trials"] if row["trial_id"] == "evaluation_2024-cb_equal_advance"
    )
    trial["status"], trial["error"] = "failed", "synthetic failed fit"
    atomic_json(root / "trials" / trial["trial_id"] / "trial.json", trial)
    atomic_json(root / "trial-ledger.json", ledger)
    with pytest.raises(ResearchError, match="failed_required_trials_unscored"):
        publisher.publish(root)
    unavailable = _read(root / "derived/unavailable-comparisons.json")
    assert unavailable["rows"][0]["status"] == "not_scored"
    assert unavailable["rows"][0]["error"] == "synthetic failed fit"
    assert not (root / "saved/result.json").exists()


def test_publisher_budget_records_failure(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    with (
        pytest.raises(ResearchError, match="resource_budget_exceeded"),
        publisher.PublicationBudget(tmp_path, derived) as budget,
    ):
        budget.limits["rss_bytes"] = 0
        budget.check()
    receipt = _read(derived / publisher.RESOURCE_NAME)
    assert receipt["status"] == "failed"
    assert receipt["resource"]["sampled_peak_rss_bytes"] > 0


def prepared_v2_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, full_matrix: bool = False
) -> Path:
    """Tiny independent v2 storage-failure recipe; no actual model/calibrator fits."""
    if not full_matrix:
        monkeypatch.setattr(
            publisher,
            "SCENARIOS",
            {key: value for key, value in SCENARIOS.items() if key in {"perfect", "missing100"}},
        )
    e3 = tmp_path / "e3"
    e3.mkdir()
    root = synthetic_root(e3 / "run", e2_root=tmp_path / "e2")
    (root / "publisher-producer-v2.zip").write_bytes(b"retained synthetic immutable v2 code")
    source = Path(publisher.__file__).parent
    atomic_json(
        root / "publisher-freeze-v2.json",
        {
            "protocol": pin(root / "protocol.json"),
            "specification": {"version": "e3-publisher-v2"},
            "publisher_source": pin(root / "publisher-producer-v2.zip"),
            "code": {
                name: digest(source / name) if name != "e3_publish.py" else "0" * 64
                for name in (
                    "e3_publish.py",
                    "e3_methods.py",
                    "e3_experiment.py",
                    "e3_data.py",
                    "e2_metrics.py",
                )
            },
        },
    )
    derived, saved = root / "derived", root / "saved"
    derived.mkdir()
    saved.mkdir()
    selection = _read(root / "selection.json")
    states = {
        "temperature_2023": selection["calibration_2023"],
        "temperature": {str(year): {"temperature": 1.0} for year in (2024, 2025, 2026)},
        "residual": {
            f"{year}_{info}": {"radii_dkk": {"0": 5.0, "1": 5.0, "2": 5.0}}
            for year in (2024, 2025, 2026)
            for info in ("current", "advance")
        },
    }
    binding_v2 = publisher._source_binding(
        root, _read(root / "protocol.json"), publisher_name="publisher-freeze-v2.json"
    )
    atomic_json(derived / "calibration.json", states)
    atomic_json(
        derived / "calibration-receipt.json",
        {"calibration": pin(derived / "calibration.json"), "source_binding": binding_v2},
    )
    publisher.prepare_predictions(
        root, derived, saved, _read(root / "protocol.json"), selection, states
    )
    atomic_json(
        derived / "publication-resource.json",
        {
            "status": "failed",
            "error": "e3_publisher_result_budget_exceeded",
            "limits": {"result_bytes": 12 * 1024**3},
            "result_bytes": 12 * 1024**3 + 1,
            "resource": {"elapsed_seconds": 1741.672, "sampled_peak_rss_bytes": 4749094912},
        },
    )
    return root


def prohibit_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "finish/replay attempted preparation/calibration/selection/model/DB work"
        )

    for name in (
        "calibration_states",
        "prepare_predictions",
        "select_queue",
        "temperature_fit",
        "residual_fit",
        "read_prediction",
    ):
        monkeypatch.setattr(publisher, name, forbidden)
    monkeypatch.setattr(catboost.CatBoostClassifier, "fit", forbidden)
    monkeypatch.setattr(catboost.CatBoostRegressor, "fit", forbidden)
    monkeypatch.setattr(duckdb, "connect", forbidden)


def test_finish_prepared_matches_original_pipeline_and_relocated_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = prepared_v2_root(tmp_path, monkeypatch, full_matrix=True)
    states = _read(root / "derived/calibration.json")
    before = {
        path.relative_to(root).as_posix(): digest(path)
        for path in root.rglob("*")
        if path.is_file()
    }

    # Run the original full pipeline with the exact same predeclared saved states,
    # into separate output. No fitting is needed to test byte-equivalent scoring.
    def saved_states(
        run_root: Path, derived: Path, selection: dict[str, Any], binding: dict[str, Any]
    ) -> dict[str, Any]:
        atomic_json(derived / "calibration.json", states)
        atomic_json(
            derived / "calibration-receipt.json",
            {"calibration": pin(derived / "calibration.json"), "source_binding": binding},
        )
        return states

    monkeypatch.setattr(publisher, "calibration_states", saved_states)
    baseline_path = publisher.publish(root, tmp_path / "baseline")
    baseline = load_e3_run(baseline_path)
    prohibit_preparation(monkeypatch)
    result = publisher.finish_prepared(root)
    finished = load_e3_run(result)
    for role in baseline.pins:
        assert digest(baseline.root / baseline.pins[role]["path"]) == digest(
            finished.root / finished.pins[role]["path"]
        )
    assert before == {name: digest(root / name) for name in before}
    provenance = finished.manifest["preparation_provenance"]
    assert (
        provenance["original_v2_binding"]
        == _read(root / "derived/calibration-receipt.json")["source_binding"]
    )
    assert provenance["publication_v3_binding"] == finished.manifest["source_binding"]
    assert (
        provenance["original_v2_binding"]["publisher"]
        != provenance["publication_v3_binding"]["publisher"]
    )
    resource = _read(root / "derived" / publisher.RESOURCE_NAME)
    assert resource["limits"]["result_bytes"] == 14 * 1024**3
    assert resource["referenced_prepared_bytes"] > 0
    assert (
        resource["inherited_preparation"]["failed_attempt"]["resource"]["elapsed_seconds"]
        == 1741.672
    )
    original_e3 = root.parent
    restored = tmp_path / "restored-e3"
    shutil.copytree(original_e3, restored)
    renamed = restored / "renamed"
    unavailable = tmp_path / "original-unavailable"
    for path in (restored / "run", renamed, original_e3, unavailable):
        assert path.resolve().is_relative_to(tmp_path.resolve())
    (restored / "run").rename(renamed)
    original_e3.rename(unavailable)
    source_before = {
        path.relative_to(renamed).as_posix(): digest(path)
        for path in renamed.rglob("*")
        if path.is_file()
    }
    replay_path = publisher.finish_prepared(renamed, tmp_path / "replay", relocate_from=original_e3)
    replay = load_e3_run(replay_path)
    assert finished.artifacts == replay.artifacts
    for role in finished.pins:
        assert finished.pins[role]["sha256"] == digest(replay.root / replay.pins[role]["path"])
    assert source_before == {name: digest(renamed / name) for name in source_before}
    assert not (tmp_path / "replay/derived").exists()
    assert replay.manifest["source_binding"] == finished.manifest["source_binding"]
    for role in ("derived_inventory", "queue_inventory", "calibration"):
        assert replay.manifest[role] == finished.manifest[role]


@pytest.mark.parametrize(
    "kind",
    [
        "prediction",
        "queue",
        "inventory",
        "calibration_binding",
        "review",
        "partial_saved",
        "partial_outcome",
        "index",
        "existing_replay",
        "overlap_replay",
        "v3_resource",
    ],
)
def test_finish_prepared_rejects_changed_inputs_and_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = prepared_v2_root(tmp_path, monkeypatch)
    publisher.freeze_publisher(root)
    output = None
    if kind in {"prediction", "queue"}:
        prefix = "predictions" if kind == "prediction" else "queues"
        path = next((root / "derived" / prefix).glob("*.parquet"))
        path.write_bytes(path.read_bytes() + b"changed")
    elif kind == "calibration_binding":
        path = root / "derived/calibration-receipt.json"
        receipt = _read(path)
        receipt["source_binding"]["publisher"] = pin(root / publisher.FREEZE_NAME)
        atomic_json(path, receipt)
    elif kind == "inventory":
        path = root / "derived/queue-inventory.json"
        inventory = _read(path)
        inventory["files"][0]["parameter"] = "changed metadata"
        atomic_json(path, inventory)
    elif kind == "review":
        path = root / "saved/candidate_review.parquet"
        path.write_bytes(path.read_bytes() + b"changed")
    elif kind == "partial_saved":
        (root / "saved/summary.json").write_text("{}")
    elif kind == "partial_outcome":
        (root / "derived/candidate_outcomes.parquet").write_bytes(b"partial")
    elif kind == "existing_replay":
        output = tmp_path / "existing-output"
        output.mkdir()
    elif kind == "overlap_replay":
        output = root / "derived/new-output"
    elif kind == "v3_resource":
        (root / "derived" / publisher.RESOURCE_NAME).write_text("{}")
    else:
        (root.parent / "index.json").write_text("{}")
    prohibit_preparation(monkeypatch)
    with pytest.raises(ResearchError):
        publisher.finish_prepared(root, output)
    assert not (root / "saved/result.json").exists()


def test_prepared_budget_counts_each_referenced_path_once(tmp_path: Path) -> None:
    derived, output = tmp_path / "derived", tmp_path / "saved"
    derived.mkdir()
    output.mkdir()
    source = derived / "source"
    review = output / "review"
    source.write_bytes(b"12345")
    review.write_bytes(b"1234567")
    with publisher.PublicationBudget(
        output, derived, inputs={source, review}, generated={review}
    ) as budget:
        budget.check(files=True)
        assert budget.result_bytes == 12
        assert budget.input_bytes == 12
        assert budget.generated_bytes == 0
