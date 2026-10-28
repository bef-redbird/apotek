"""Synthetic saved-summary boundary and display checks; no model fitting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e2_result_view import load_e2_run


def fixture_result(root: Path) -> Path:
    root.mkdir(parents=True)
    score = {
        "task": "direction",
        "split": "evaluation_all",
        "method": "no_change",
        "n": 10,
        "metrics": {"brier": 0.2, "rise_precision_top_decile": 0.1},
    }
    candidate = {**score, "method": "catboost_group", "metrics": {"brier": 0.1}}
    values = {
        "metrics": {
            "aggregate": [score, candidate],
            "per_period": [{**score, "period_start": "2025-01-06"}],
            "calibration": [
                {
                    **score,
                    "bin_lower": 0,
                    "bin_upper": 0.1,
                    "mean_probability": 0.05,
                    "observed_frequency": 0.1,
                }
            ],
            "uncertainty": [],
        },
        "coverage": {
            "overall": {"eligible": 12, "observed": 10, "exit": 1, "unknown": 1},
            "per_period": [{"period_start": "2025-01-06", "unknown": 1}],
        },
        "history_curves": {
            "experiments": [{**candidate, "history_years": 1}, {**candidate, "history_years": 3}]
        },
        "comparisons": {"incremental": [{**candidate, "baseline": "no_change"}]},
        "protocol": {"tuning_budget": 0, "availability": "reconstructed_research"},
    }
    pins = []
    for role, value in values.items():
        content = json.dumps(value).encode()
        (root / f"{role}.json").write_bytes(content)
        pins.append(
            {
                "path": f"{role}.json",
                "role": role,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {
        "schema_version": "e2-saved-results-v1",
        "status": "completed",
        "evaluation_locked": True,
        "files": pins,
        "dataset": {"dataset_id": "synthetic", "price_basis": "AIP DKK/package ex VAT/fees"},
        "definitions": {"direction": "exact next-price sign", "magnitude": "absolute DKK"},
        "split_dates": {"evaluation_all": {"start": "2025-01-06", "end": "2025-01-20"}},
        "methods": {"direction": ["no_change", "catboost_group"]},
        "baseline_by_task": {"direction": "no_change"},
        "candidate_methods": ["catboost_group"],
        "exposure_status": "Historical periods exposed; exploratory development evidence.",
    }
    path = root / "result.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _app(path: Path | None) -> AppTest:
    def render() -> None:
        from pathlib import Path

        import streamlit as st

        from bef_redbird.research.e2_dashboard import main

        value = st.session_state["path"]
        main(Path(value) if value else None)

    app = AppTest.from_function(render, default_timeout=20)
    app.session_state["path"] = str(path) if path else None
    return app.run()


def test_empty_and_missing_e2(tmp_path: Path) -> None:
    empty = _app(None)
    assert not empty.exception and empty.info and not empty.dataframe
    missing = _app(tmp_path / "missing.json")
    assert not missing.exception and missing.error and not missing.dataframe


def test_valid_display_and_exploratory_chart_preserves_aggregate(tmp_path: Path) -> None:
    path = fixture_result(tmp_path / "valid")
    app = _app(path)
    assert not app.exception
    aggregate = app.dataframe[0].value.copy()
    app.selectbox[2].set_value("rise_precision_top_decile").run()
    assert not app.exception and app.dataframe[0].value.equals(aggregate)
    assert len(app.get("plotly_chart")) == 3
    assert any("Actually executed" in item.value for item in app.subheader)
    assert any("Exposure" in item.value for item in app.markdown)
    assert not app.success
    assert not app.button


def test_validation_history_experiments_remain_visible_with_evaluation_selected(
    tmp_path: Path,
) -> None:
    path = fixture_result(tmp_path / "valid")
    manifest = json.loads(path.read_text())
    manifest["split_dates"]["validation"] = {"start": "2022-01-01", "end": "2024-01-01"}
    artifact = path.parent / "history_curves.json"
    value = json.loads(artifact.read_text())
    for row in value["experiments"]:
        row["split"] = "validation"
    payload = json.dumps(value).encode()
    artifact.write_bytes(payload)
    pin = next(pin for pin in manifest["files"] if pin["role"] == "history_curves")
    pin.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    path.write_text(json.dumps(manifest))
    app = _app(path)
    assert not app.exception and app.selectbox[1].value == "evaluation_all"
    assert len(app.get("plotly_chart")) == 3
    assert any(
        "History experiments evaluated on saved split: validation" in x.value for x in app.caption
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("evaluation_locked", False),
        ("status", "running"),
        ("schema_version", "future"),
        ("exposure_status", ""),
        ("baseline_by_task", {"direction": "absent"}),
    ],
)
def test_reject_incomplete_or_invalid_manifest(tmp_path: Path, field: str, value: Any) -> None:
    path = fixture_result(tmp_path / "invalid")
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ResearchError):
        load_e2_run(path)
    app = _app(path)
    assert not app.exception and app.error and not app.dataframe


def test_every_pin_is_checked_and_model_roles_rejected(tmp_path: Path) -> None:
    path = fixture_result(tmp_path / "invalid")
    (path.parent / "protocol.json").write_text("{}")
    with pytest.raises(ResearchError, match="artifact_hash_mismatch"):
        load_e2_run(path)
    app = _app(path)
    assert not app.exception and app.error and not app.dataframe


def test_null_metric_is_missing_and_executable_role_is_rejected(tmp_path: Path) -> None:
    path = fixture_result(tmp_path / "invalid")
    manifest = json.loads(path.read_text())
    artifact = path.parent / "metrics.json"
    value = json.loads(artifact.read_text())
    value["aggregate"][0]["metrics"]["unknown_after_current_rise"] = None
    payload = json.dumps(value).encode()
    artifact.write_bytes(payload)
    pin = next(pin for pin in manifest["files"] if pin["role"] == "metrics")
    pin.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    path.write_text(json.dumps(manifest))
    assert (
        load_e2_run(path).artifacts["metrics"]["aggregate"][0]["metrics"][
            "unknown_after_current_rise"
        ]
        is None
    )
    manifest["files"][0]["role"] = "executable_model"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ResearchError, match="invalid_e2_roles"):
        load_e2_run(path)


@pytest.mark.parametrize("status", ["failed", "running"])
def test_pinned_trial_failures_are_visible_and_running_trials_are_rejected(
    tmp_path: Path,
    status: str,
) -> None:
    path = fixture_result(tmp_path / "run")
    manifest = json.loads(path.read_text())
    artifact = path.parent / "coverage.json"
    value = json.loads(artifact.read_text())
    value["trial_statuses"] = [
        {"trial_id": "screen-expanding", "status": status, "error": "synthetic resource limit"}
    ]
    payload = json.dumps(value).encode()
    artifact.write_bytes(payload)
    pin = next(pin for pin in manifest["files"] if pin["role"] == "coverage")
    pin.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    path.write_text(json.dumps(manifest))
    app = _app(path)
    assert not app.exception
    if status == "failed":
        assert any("declared trial(s) failed" in x.value for x in app.warning)
        assert app.dataframe[0].value.iloc[0]["trial_id"] == "screen-expanding"
        assert not app.success
    else:
        assert app.error and not app.dataframe


@pytest.mark.parametrize("mutation", ["duplicate", "nonfinite", "bad_period", "missing_list"])
def test_invalid_pinned_metrics_rejected(tmp_path: Path, mutation: str) -> None:
    path = fixture_result(tmp_path / "invalid")
    manifest = json.loads(path.read_text())
    metrics = path.parent / "metrics.json"
    value = json.loads(metrics.read_text())
    if mutation == "duplicate":
        value["aggregate"].append(value["aggregate"][0])
    elif mutation == "nonfinite":
        value["aggregate"][0]["metrics"]["brier"] = float("nan")
    elif mutation == "bad_period":
        value["per_period"][0]["period_start"] = "not-a-date"
    else:
        del value["calibration"]
    payload = json.dumps(value).encode()
    metrics.write_bytes(payload)
    pin = next(pin for pin in manifest["files"] if pin["role"] == "metrics")
    pin.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    path.write_text(json.dumps(manifest))
    app = _app(path)
    assert not app.exception and app.error and not app.dataframe


def test_no_execution_imports() -> None:
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bef_redbird.research.e2_dashboard; "
            "assert 'catboost' not in sys.modules; "
            "assert not any(k.startswith('bef_redbird.foundation') for k in sys.modules); "
            "assert 'bef_redbird.research.evaluation' not in sys.modules; "
            "assert 'bef_redbird.research.training' not in sys.modules",
        ],
        check=True,
        capture_output=True,
    )


def test_group_contribution_uses_paired_interval_and_exact_feature_ablation() -> None:
    from bef_redbird.research.e2_dashboard import group_comparison_rows

    rows = [
        {
            "candidate": "catboost_group",
            "baseline": "catboost_package",
            "n": 100,
            "periods": 20,
            "metrics": {"brier_difference": difference, "ci_lower": lower, "ci_upper": upper},
        }
        for difference, lower, upper in [
            (-0.01, -0.02, -0.001),
            (0.01, 0.001, 0.02),
            (-0.001, -0.01, 0.01),
        ]
    ]
    rows.append(
        {
            "candidate": "catboost_group",
            "baseline": "no_change",
            "n": 100,
            "metrics": {"ci_lower": -1, "ci_upper": -0.5},
        }
    )
    result = group_comparison_rows(rows)
    assert [row["Group contribution"] for row in result] == [
        "Helped on these observed periods",
        "Hurt on these observed periods",
        "Inconclusive across periods",
    ]
    assert result[0]["Population"] == "All eligible candidates: observed outcomes"


def _replace_artifact(path: Path, role: str, value: dict[str, Any]) -> None:
    manifest = json.loads(path.read_text())
    artifact = path.parent / f"{role}.json"
    payload = json.dumps(value).encode()
    artifact.write_bytes(payload)
    pin = next(pin for pin in manifest["files"] if pin["role"] == role)
    pin.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    path.write_text(json.dumps(manifest))


def test_primary_probability_default_compact_table_and_saved_reliability_plot(
    tmp_path: Path,
) -> None:
    path = fixture_result(tmp_path / "run")
    metrics = json.loads((path.parent / "metrics.json").read_text())
    for row in [*metrics["aggregate"], *metrics["per_period"]]:
        row["metrics"]["accuracy"] = 0.9
    _replace_artifact(path, "metrics", metrics)
    app = _app(path)
    assert not app.exception and app.selectbox[2].value == "brier"
    assert "metrics · accuracy" not in app.dataframe[0].value.columns
    assert any("metrics · accuracy" in frame.value.columns for frame in app.dataframe[1:])
    figure = json.loads(app.get("plotly_chart")[1].proto.spec)
    assert figure["data"][1]["name"] == "Ideal calibration"
    assert figure["data"][1]["x"] == [0, 1] and figure["data"][1]["y"] == [0, 1]
    assert any("Events forecast at 30%" in x.value for x in app.caption)
    assert any("Above the diagonal" in x.value for x in app.caption)
    text = " ".join(item.value for item in [*app.markdown, *app.caption])
    assert "Which packages are more likely to rise" in text
    assert "is not 10% accuracy" in text
    assert "Unknown top 10% uses every shortlisted case" in text
    assert "own = the package's own price history" in text
    assert "package attributes and current-period ABC" in text
    assert any(
        "This experiment used current-period ABC. Advance ABC for the next period was not tested."
        in item.value
        for item in app.caption
    )
    assert "Simple regression (logistic regression) learns" in text
    assert "CatBoost combines decision trees" in text
    period_figure = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert period_figure["data"][0]["name"] == "No price change"
    history_figure = json.loads(app.get("plotly_chart")[2].proto.spec)
    assert history_figure["data"][0]["name"] == "CatBoost — with competitors"
    assert history_figure["layout"]["xaxis"]["title"]["text"] == "Training history (years)"
    calibration = next(item for item in app.selectbox if item.label == "Calibration method")
    assert calibration.value == "no_change"
    assert calibration.options == ["No price change"]
    assert app.dataframe[0].value.method.tolist() == [
        "No price change",
        "CatBoost — with competitors",
    ]


def test_method_labels_preserve_run_prefixes_and_cover_declared_variants() -> None:
    from bef_redbird.research.e2_dashboard import METHOD_LABELS, method_label

    for method in (
        "no_change",
        "historical_frequency",
        "repeat_last",
        "historical_median",
        "persistence",
        "logistic_own",
        "logistic_package",
        "logistic_group",
        "catboost_own",
        "catboost_package",
        "catboost_group",
        "catboost_group_mae",
    ):
        assert method in METHOD_LABELS
        assert method_label(f"First: {method}") == f"First: {METHOD_LABELS[method]}"
    assert method_label("future_method") == "future method"


def test_magnitude_chart_defaults_to_mae_and_keeps_extra_metrics_optional(tmp_path: Path) -> None:
    path = fixture_result(tmp_path / "run")
    manifest = json.loads(path.read_text())
    manifest["methods"] = {"magnitude": ["no_change", "catboost_group"]}
    manifest["baseline_by_task"] = {"magnitude": "no_change"}
    path.write_text(json.dumps(manifest))
    metrics = json.loads((path.parent / "metrics.json").read_text())
    for row in [*metrics["aggregate"], *metrics["per_period"]]:
        row.update(
            task="magnitude",
            metrics={"mae_dkk": 2.0, "large_error_share": 0.9, "relative_to_current_aip_mae": 0.02},
        )
    metrics["calibration"] = []
    _replace_artifact(path, "metrics", metrics)
    _replace_artifact(path, "history_curves", {"experiments": []})
    _replace_artifact(path, "comparisons", {"incremental": []})
    app = _app(path)
    assert not app.exception and app.selectbox[2].value == "mae_dkk"
    assert "metrics · large_error_share" not in app.dataframe[0].value.columns
    assert any("metrics · large_error_share" in frame.value.columns for frame in app.dataframe[1:])
    text = " ".join(item.value for item in [*app.markdown, *app.caption])
    assert "How close is the forecast to the next AIP change in kroner" in text
    assert "MAE ignores the sign" in text
    assert "Price error is not savings" in text
    assert "Unknown top 10% uses every shortlisted case" not in text


def _comparable_fixture(root: Path) -> Path:
    path = fixture_result(root)
    manifest = json.loads(path.read_text())
    manifest["dataset"]["base"] = {"sha256": "a" * 64}
    path.write_text(json.dumps(manifest))
    protocol = {
        "targets": {"direction": "exact next delta sign"},
        "classes": {"direction": ["fall", "unchanged", "rise"]},
        "weighting": "equal tariff periods",
        "ranking": "frozen top decile",
        "cutoff": "reconstructed",
    }
    _replace_artifact(path, "protocol", protocol)
    coverage = json.loads((path.parent / "coverage.json").read_text())
    coverage["comparison_binding"] = {
        "version": "e2-comparison-binding-v1",
        "splits": {
            "evaluation_all": {
                "direction": {
                    "candidate_keys_sha256": "b" * 64,
                    "observed_outcome_keys_sha256": "c" * 64,
                    "outcome_values_sha256": "d" * 64,
                }
            }
        },
    }
    _replace_artifact(path, "coverage", coverage)
    return path


def test_compatible_second_run_uses_pinned_case_proofs_without_fit(tmp_path: Path) -> None:
    first, second = (
        _comparable_fixture(tmp_path / "first"),
        _comparable_fixture(tmp_path / "second"),
    )
    app = _app(first)
    assert any("Only one E2 run selected" in x.value for x in app.info)
    app.text_input[0].set_value(str(second)).run()
    assert not app.exception
    assert any("Compatible pinned target, cases and outcome values" in x.value for x in app.caption)
    assert any("saved_run" in frame.value.columns for frame in app.dataframe)
    assert not app.success and not app.button


@pytest.mark.parametrize("mutation", ["missing_proof", "outcome_values", "base", "target", "split"])
def test_incompatible_or_unproved_second_run_is_refused(tmp_path: Path, mutation: str) -> None:
    from bef_redbird.research.e2_dashboard import comparison_issues

    first, second = (
        _comparable_fixture(tmp_path / "first"),
        _comparable_fixture(tmp_path / "second"),
    )
    if mutation in {"missing_proof", "outcome_values"}:
        value = json.loads((second.parent / "coverage.json").read_text())
        if mutation == "missing_proof":
            del value["comparison_binding"]
        else:
            value["comparison_binding"]["splits"]["evaluation_all"]["direction"][
                "outcome_values_sha256"
            ] = "e" * 64
        _replace_artifact(second, "coverage", value)
    elif mutation == "target":
        value = json.loads((second.parent / "protocol.json").read_text())
        value["targets"]["direction"] = "different horizon"
        _replace_artifact(second, "protocol", value)
    else:
        value = json.loads(second.read_text())
        if mutation == "base":
            value["dataset"]["base"]["sha256"] = "f" * 64
        else:
            value["split_dates"]["evaluation_all"]["end"] = "2025-02-03"
        second.write_text(json.dumps(value))
    assert comparison_issues(load_e2_run(first), load_e2_run(second), "direction", "evaluation_all")
    app = _app(first)
    app.text_input[0].set_value(str(second)).run()
    assert not app.exception and any("Comparison refused" in x.value for x in app.warning)
    assert not any("saved_run" in frame.value.columns for frame in app.dataframe)


def test_e1_schema_is_rejected_as_second_e2_result(tmp_path: Path) -> None:
    first = _comparable_fixture(tmp_path / "first")
    e1 = tmp_path / "e1.json"
    e1.write_text('{"schema_version":"e1-run-v1"}')
    app = _app(first)
    app.text_input[0].set_value(str(e1)).run()
    assert not app.exception and any(
        "E1 is a separate evaluation schema" in x.value for x in app.warning
    )


def test_saved_coverage_filter_keeps_original_shortlist_and_frozen_metrics(tmp_path: Path) -> None:
    path = fixture_result(tmp_path / "run")
    coverage = json.loads((path.parent / "coverage.json").read_text())
    coverage["outcome_detail"] = {
        "rows": [
            {
                "method": "catboost_group",
                "regime": "all2024_2026",
                "cohort": cohort,
                "condition": "all_origins",
                "all_candidates": {
                    "rows": count,
                    "unknown_fraction": unknown,
                    "coverage_counts": {"observed": count - 1, "pending": 1},
                },
                "original_shortlist": {
                    "rows": 1,
                    "unknown_fraction": 1,
                    "coverage_counts": {"pending": 1},
                },
            }
            for cohort, count, unknown in [("all", 12, 1 / 12), ("new_under6", 3, 1 / 3)]
        ],
        "limitations": ["recorded_exit does not prove physical exit"],
        "source": {"sha256": "a" * 64},
    }
    _replace_artifact(path, "coverage", coverage)
    app = _app(path)
    official = app.dataframe[0].value.copy()
    control = next(item for item in app.selectbox if item.label == "Coverage cohort")
    assert control.value == "all"
    control.set_value("new_under6").run()
    assert not app.exception and app.dataframe[0].value.equals(official)
    summary = next(
        frame.value
        for frame in app.dataframe
        if "Population" in frame.value and "Cases" in frame.value
    )
    assert summary.Cases.tolist() == [3, 1]
    assert summary["Unavailable fraction"].tolist() == [1 / 3, 1]


def test_task_switching_keeps_probability_and_amount_chart_defaults_separate(
    tmp_path: Path,
) -> None:
    from bef_redbird.research.e2_experiment import publish
    from tests.test_research_e2_publish import synthetic_publish_root

    root = synthetic_publish_root(tmp_path / "run")
    publish(root)
    app = _app(root / "saved" / "result.json")
    assert not app.exception
    period = next(item for item in app.selectbox if item.label == "Metric in period chart")
    assert period.value == "brier"
    app.selectbox[0].set_value("magnitude").run()
    assert not app.exception
    period = next(item for item in app.selectbox if item.label == "Metric in period chart")
    assert period.value == "mae_dkk"
    app.selectbox[0].set_value("reversal").run()
    assert not app.exception
    assert (
        next(item for item in app.selectbox if item.label == "Metric in period chart").value
        == "brier"
    )
    text = " ".join(item.value for item in [*app.markdown, *app.caption])
    assert "After a rise has already happened" in text
    assert "Use the lower of the two following prices" in text
    assert "Both future observations must be available" in text
    assert "does not predict the initial rise in advance" in text
    assert not app.button


def test_failed_largest_trial_and_baseline_rows_do_not_inflate_completed_capacity() -> None:
    from bef_redbird.research.e2_dashboard import capacity_summary

    result = capacity_summary(
        [
            {
                "method": "catboost_group",
                "status": "completed",
                "training_rows": 700000,
                "resource": {"sampled_peak_rss_bytes": 8_000_000_000, "elapsed_seconds": 300},
            },
            {
                "method": "logistic_package",
                "status": "completed",
                "training_rows": 650000,
                "resource": {"sampled_peak_rss_bytes": 7_000_000_000, "elapsed_seconds": 400},
            },
            {
                "method": "catboost_group",
                "status": "failed",
                "training_rows": 2_900_000,
                "resource": {"sampled_peak_rss_bytes": 17_000_000_000, "elapsed_seconds": 1524},
            },
            {
                "method": "historical_frequency",
                "status": "completed",
                "training_rows": 9_000_000,
                "resource": {"sampled_peak_rss_bytes": 30_000_000_000, "elapsed_seconds": 2000},
            },
        ]
    )
    assert result["Completed learned trials"] == 2
    assert result["Largest completed learned training rows"] == 700000
    assert result["Largest attempted learned training rows"] == 2_900_000
    assert result["Highest completed learned trial RSS bytes"] == 8_000_000_000
    assert result["Longest completed learned trial seconds"] == 400


def _secondary() -> dict[str, Any]:
    return {
        "task": "direction",
        "split": "evaluation_all",
        "candidate": "catboost_group",
        "method": "catboost_group",
        "baseline": "no_change",
        "metric": "rise_precision_top_decile",
        "n": 10,
        "periods": 2,
        "metrics": {
            "difference": 0.02,
            "ci_lower": 0.01,
            "ci_upper": 0.03,
            "sensitivity_lower": 0.005,
            "sensitivity_upper": 0.035,
        },
    }


def test_saved_secondary_comparison_is_validated_and_labelled_by_metric(tmp_path: Path) -> None:
    from bef_redbird.research.e2_dashboard import secondary_comparison_rows

    path = fixture_result(tmp_path / "run")
    value = json.loads((path.parent / "comparisons.json").read_text())
    value["secondary"] = [_secondary()]
    _replace_artifact(path, "comparisons", value)
    assert load_e2_run(path).artifacts["comparisons"]["secondary"]
    app = _app(path)
    assert not app.exception and any(
        "Other saved paired comparisons" in x.value for x in app.subheader
    )
    assert app.table[0].value.iloc[0]["Paired result for this measure"] == "Observed better"
    row = _secondary()
    row["metric"] = "fall_fraction_top_decile"
    assert secondary_comparison_rows([row])[0]["Paired result for this measure"] == "Observed worse"
    row["metrics"].update(ci_lower=-0.01, ci_upper=0.03)
    assert secondary_comparison_rows([row])[0]["Paired result for this measure"] == "Inconclusive"


@pytest.mark.parametrize("mutation", ["scope", "baseline", "score", "interval", "duplicate"])
def test_invalid_pinned_secondary_comparisons_are_rejected(tmp_path: Path, mutation: str) -> None:
    path = fixture_result(tmp_path / "run")
    row = _secondary()
    if mutation == "scope":
        row["candidate"] = row["method"] = "not_a_saved_method"
    elif mutation == "baseline":
        row["baseline"] = "not_a_saved_baseline"
    elif mutation == "score":
        row["metrics"]["difference"] = "not_a_score"
    elif mutation == "interval":
        row["metrics"].update(ci_lower=0.03, ci_upper=0.01)
    value = json.loads((path.parent / "comparisons.json").read_text())
    value["secondary"] = [row, row.copy()] if mutation == "duplicate" else [row]
    _replace_artifact(path, "comparisons", value)
    with pytest.raises(ResearchError):
        load_e2_run(path)
