"""Synthetic saved-run boundary and Streamlit workflow checks; no private data."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.research.dataset import ResearchError, fingerprint
from bef_redbird.research.result_view import incompatibilities, load_saved_run, verify_run


def _fixture(root: Path, *, dataset_id: str = "synthetic-panel") -> Path:
    root.mkdir()
    base = {"task": "next_period", "split": "validation", "method": "no_change"}
    period = {
        **base,
        "period_index": 24,
        "period_start": "2026-01-05",
        "n": 4,
        "metrics": {"brier_multiclass": 0.5},
    }
    metrics = {
        "aggregate": [{**base, "n": 4, "metrics": {"brier_multiclass": 0.5}}],
        "per_period": [period],
        "calibration": [
            {
                **base,
                "class_label": "1",
                "bin_lower": 0.0,
                "bin_upper": 0.1,
                "n": 4,
                "mean_probability": 0.0,
                "observed_frequency": 0.25,
            }
        ],
        "uncertainty": [
            {
                **base,
                "metric": "brier_multiclass",
                "n_periods": 1,
                "lower": None,
                "upper": None,
                "seed": 1,
                "replicates": 10,
            }
        ],
    }
    coverage = {
        "overall": {
            "total_rows": 6,
            "eligible_candidates": 5,
            "excluded": {"identity_unresolved": 1},
        },
        "per_period": [
            {
                "period_index": 24,
                "period_start": "2026-01-05",
                "split": "validation",
                "total_rows": 6,
                "eligible_candidates": 5,
                "excluded": {"identity_unresolved": 1},
                "next_period_labels": {"observed": 4, "pending": 1},
                "reversal_labels": {"not_observed_rise": 5},
                "observed_rise_candidates": 0,
            }
        ],
    }
    files = {
        "metrics": ("metrics.json", json.dumps(metrics).encode()),
        "coverage": ("coverage.json", json.dumps(coverage).encode()),
        "input_manifest": ("input-manifest.json", b"{}"),
        "predictions": ("predictions.jsonl.gz", gzip.compress(b"{}\n")),
        "models": ("models.jsonl.gz", gzip.compress(b"{}\n")),
    }
    pins = []
    for role, (name, payload) in files.items():
        (root / name).write_bytes(payload)
        pins.append(
            {
                "role": role,
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    run: dict[str, Any] = {
        "schema_version": "e1-run-v1",
        "status": "completed",
        "evaluation_locked": True,
        "dataset": {
            "dataset_id": dataset_id,
            "manifest_sha256": "a" * 64,
            "price_context": {"price_kind": "AIP", "currency": "DKK"},
            "availability_mode": "reconstructed_research",
            "population_definition": "Synthetic current-period eligible packages",
            "identity_policy": "Synthetic continuity only",
            "provenance": {},
        },
        "method_version": "e1-fixed-ridge-v1",
        "feature_names": ["past_return"],
        "definitions": {"next_period": {"horizon": 1, "price_basis": "AIP"}},
        "splits": {"validation": [24, 31], "final_evaluation": [32, 44]},
        "split_dates": {
            "validation": {"start": "2026-01-05", "end": "2026-04-13"},
            "final_evaluation": {"start": "2026-04-27", "end": "2026-10-12"},
        },
        "cutoff_policy": {"weekday": "Wednesday", "assumption": "not strict replay"},
        "methods": {"next_period": ["no_change", "ridge"], "reversal": ["no_reversal"]},
        "versions": {},
        "files": pins,
    }
    _publish(root / "run.json", run)
    return root / "run.json"


def _publish(path: Path, run: dict[str, Any]) -> None:
    run["run_fingerprint"] = fingerprint(
        {
            key: value
            for key, value in run.items()
            if key not in {"run_fingerprint", "created_at", "lineage_event_hash"}
        }
    )
    run["created_at"] = "2026-10-22T12:00:00+00:00"
    path.write_text(json.dumps(run), encoding="utf-8")


def _app(paths: list[Path]) -> AppTest:
    def render() -> None:
        from pathlib import Path

        import streamlit as st

        from bef_redbird.research.dashboard import main

        main([Path(value) for value in st.session_state["paths"]])

    app = AppTest.from_function(render, default_timeout=20)
    app.session_state["paths"] = [str(path) for path in paths]
    return app.run()


def test_empty_dashboard_and_missing_bundle(tmp_path: Path) -> None:
    app = _app([])
    assert not app.exception
    assert "No saved runs selected" in app.info[0].value
    missing = _app([tmp_path / "missing.json"])
    assert not missing.exception
    assert "unavailable" in missing.error[0].value
    assert not missing.dataframe


def test_valid_dashboard_keeps_official_metrics_when_exploring(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "run")
    app = _app([path])
    assert not app.exception
    assert app.tabs[0].label == "Results"
    assert not any(item.label == "Saved run" for item in app.selectbox)
    assert any("Database expansion did not retrain" in item.value for item in app.caption)
    app.selectbox[1].set_value("validation").run()
    official = app.table[0].value.copy()
    assert official.iloc[0]["No change"] == "0.5000"
    app.multiselect[0].set_value([]).run()
    assert not app.exception
    assert app.table[0].value.equals(official)
    assert any("exploratory" in element.value.lower() for element in app.subheader)
    assert any("Later use as development evidence is unknown" in item.value for item in app.info)
    app.selectbox[1].set_value("final_evaluation").run()
    assert not app.exception
    assert any("locked" in item.value for item in app.caption)
    assert any("No saved observations" in item.value for item in app.info)


@pytest.mark.parametrize("split", ["initial_train", "diagnostic"])
def test_descriptive_splits_do_not_claim_official_evaluation(tmp_path: Path, split: str) -> None:
    path = _fixture(tmp_path / "run")
    run = json.loads(path.read_text())
    run["splits"][split] = [0, 1]
    run["split_dates"][split] = {"start": "2025-01-06", "end": "2025-01-20"}
    run["dataset"]["provenance"]["external_coverage_gaps"] = [{"reason": "unresolved omission"}]
    _publish(path, run)
    app = _app([path])
    app.selectbox[1].set_value(split).run()
    assert not app.exception
    assert any(item.value == "Descriptive saved results" for item in app.subheader)
    assert not any(item.value == "Official frozen comparison" for item in app.subheader)
    assert any("outside the official evaluation" in item.value for item in app.caption)
    assert any("outside the snapshot panel" in item.value for item in app.warning)


@pytest.mark.parametrize(
    "field,value",
    [("evaluation_locked", False), ("status", "running"), ("schema_version", "future")],
)
def test_reject_unlocked_partial_and_unsupported(tmp_path: Path, field: str, value: Any) -> None:
    path = _fixture(tmp_path / "run")
    run = json.loads(path.read_text())
    run[field] = value
    _publish(path, run)
    with pytest.raises(ResearchError):
        load_saved_run(path)
    app = _app([path])
    assert not app.exception
    assert app.error
    assert not app.dataframe


@pytest.mark.parametrize(
    "name",
    [
        "metrics.json",
        "coverage.json",
        "predictions.jsonl.gz",
        "models.jsonl.gz",
        "input-manifest.json",
    ],
)
def test_verifies_every_pinned_file(tmp_path: Path, name: str) -> None:
    path = _fixture(tmp_path / "run")
    artifact = path.parent / name
    payload = artifact.read_bytes()
    artifact.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    with pytest.raises(ResearchError, match="artifact_hash_mismatch"):
        verify_run(path)
    app = _app([path])
    assert not app.exception
    assert app.error
    assert not app.dataframe


def test_crossrun_incompatibility_is_visible(tmp_path: Path) -> None:
    first = _fixture(tmp_path / "first")
    second = _fixture(tmp_path / "second", dataset_id="different-cases")
    assert incompatibilities(load_saved_run(first), load_saved_run(second)) == ["dataset_id"]
    app = _app([first, second])
    assert not app.exception
    assert any("Incompatible runs" in item.value for item in app.warning)
    assert not any("Compatible frozen" in item.value for item in app.success)


def test_compatible_crossrun_comparison_and_missing_outcomes(tmp_path: Path) -> None:
    app = _app([_fixture(tmp_path / "first"), _fixture(tmp_path / "second")])
    assert not app.exception
    assert any("Compatible frozen" in item.value for item in app.success)
    assert any("next_period_labels · pending" in frame.value.columns for frame in app.dataframe)


def test_dashboard_does_not_import_execution_modules() -> None:
    import subprocess
    import sys

    code = (
        "import sys; import bef_redbird.research.dashboard; "
        "assert 'bef_redbird.research.evaluation' not in sys.modules; "
        "assert 'bef_redbird.research.models' not in sys.modules; "
        "assert not any(k.startswith('bef_redbird.foundation') for k in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_explicit_configuration_has_no_directory_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from bef_redbird.research.dashboard import configured_paths

    first, second = tmp_path / "first.json", tmp_path / "second.json"
    monkeypatch.setenv("BEF_E1_RUNS", os.pathsep.join([str(first), str(second)]))
    assert configured_paths([]) == [first, second]
    assert configured_paths(["--", str(second), str(second)]) == [second]


def test_external_lineage_is_read_only_and_corruption_stays_explicit(tmp_path: Path) -> None:
    from bef_redbird.research.result_view import lineage_events

    path = tmp_path / "lineage.jsonl"
    event = {
        "event": "holdout_used_as_development",
        "run_fingerprint": "a" * 64,
        "sequence": 0,
        "previous_event_hash": "0" * 64,
        "recorded_at": "2026-10-22T12:00:00+00:00",
    }
    payload = (json.dumps({**event, "event_hash": fingerprint(event)}) + "\n").encode()
    path.write_bytes(payload)
    assert lineage_events(path, "a" * 64) == [event]
    assert path.read_bytes() == payload
    path.write_bytes(payload[:-1])
    with pytest.raises(ResearchError, match="invalid_lineage_log"):
        lineage_events(path, "a" * 64)


def test_corrupt_manifest_and_missing_pinned_file_are_rejected(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "run")
    (path.parent / "models.jsonl.gz").unlink()
    with pytest.raises(ResearchError):
        verify_run(path)
    path.write_bytes(b'{"broken":')
    app = _app([path])
    assert app.error and not app.exception and not app.dataframe


def test_partial_metrics_structure_is_rejected_even_when_pinned(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "run")
    run = json.loads(path.read_text())
    metrics_path = path.parent / "metrics.json"
    value = json.loads(metrics_path.read_text())
    del value["per_period"][0]["period_start"]
    payload = json.dumps(value).encode()
    metrics_path.write_bytes(payload)
    pin = next(pin for pin in run["files"] if pin["role"] == "metrics")
    pin.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    _publish(path, run)
    app = _app([path])
    assert app.error and not app.exception and not app.dataframe


def test_comparison_reports_mixed_results_and_equal_accuracy() -> None:
    from bef_redbird.research.dashboard import comparison_rows
    from bef_redbird.research.result_view import SavedRun

    rows = [
        {
            "task": "next_period",
            "split": "final_evaluation",
            "method": method,
            "n": 10,
            "metrics": values,
        }
        for method, values in [
            (
                "no_change",
                {"brier_multiclass": 0.45, "precision_top_decile_rise": 0.04, "mae_price_dkk": 7.0},
            ),
            (
                "ridge",
                {
                    "brier_multiclass": 0.27,
                    "precision_top_decile_rise": 0.28,
                    "mae_price_dkk": 23.0,
                },
            ),
        ]
    ]
    run = SavedRun({}, {"aggregate": rows}, {})
    comparison = comparison_rows(run, "next_period", "final_evaluation")
    assert [row["Compared with baseline"] for row in comparison] == [
        "Observed better",
        "Observed better",
        "Observed worse",
    ]
    assert comparison[1]["Fixed ridge"] == "28.00%"
    rows[1]["n"] = 9
    assert all(
        row["Compared with baseline"] == "Not comparable"
        for row in comparison_rows(run, "next_period", "final_evaluation")
    )


@pytest.mark.parametrize("mutation", ["nonfinite", "duplicate", "bad_coverage"])
def test_invalid_saved_values_are_rejected(tmp_path: Path, mutation: str) -> None:
    path = _fixture(tmp_path / "run")
    run = json.loads(path.read_text())
    role = "coverage" if mutation == "bad_coverage" else "metrics"
    artifact = path.parent / f"{role}.json"
    value = json.loads(artifact.read_text())
    if mutation == "nonfinite":
        value["aggregate"][0]["metrics"]["brier_multiclass"] = float("nan")
    elif mutation == "duplicate":
        value["aggregate"].append(value["aggregate"][0].copy())
    else:
        value["per_period"][0]["eligible_candidates"] = "unknown"
    payload = json.dumps(value).encode()
    artifact.write_bytes(payload)
    pin = next(pin for pin in run["files"] if pin["role"] == role)
    pin.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    _publish(path, run)
    app = _app([path])
    assert app.error and not app.exception and not app.table
