"""Synthetic E3 saved-only boundary and review checks; no fitted model required."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e3_result_view import (
    load_candidate_review,
    load_e3_index,
    load_indexed_e3_run,
)


def _pin(path: Path, relative: str, **extra: Any) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "path": relative,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        **extra,
    }


def fixture_index(root: Path, *, label: str = "Synthetic E3") -> Path:
    root.mkdir(parents=True)
    e2 = root / "e2.json"
    e2.write_text(
        json.dumps(
            {
                "schema_version": "e2-saved-results-v1",
                "status": "completed",
                "evaluation_locked": True,
            }
        )
    )
    row = {
        "task": "direction",
        "split": "evaluation_all",
        "method": "current",
        "label": "Current ABC reference",
        "information": "current_abc",
        "peer_set": "comparable",
        "scenario": "current_only",
        "candidates": 12,
        "observed": 10,
        "periods": 2,
        "scored_periods": 2,
        "brier": 0.2,
        "ece": 0.04,
    }
    proxy = {
        **row,
        "method": "advance",
        "label": "Advance ABC model",
        "information": "advance_abc_historical_proxy",
        "scenario": "full_proxy",
        "brier": 0.1,
    }
    values = {
        "summary": {
            "rows": [row, proxy, {**proxy, "split": "2025", "brier": 0.15}],
            "limitations": [],
        },
        "comparisons": {
            "rows": [
                {
                    "baseline": "current",
                    "candidate": "advance",
                    "metric": "brier",
                    "mean_difference": -0.1,
                    "lower": -0.15,
                    "upper": -0.05,
                    "block_length": 4,
                    "n": 10,
                }
            ],
            "limitations": ["Equal tariff-period weighting."],
            "binding": {
                "target": "next-period AIP direction and value",
                "verified": True,
                "periods_sha256": "a" * 64,
                "candidate_keys_sha256": "b" * 64,
                "outcomes_sha256": "c" * 64,
                "e2_manifest": _pin(e2, str(e2.resolve())),
            },
        },
        "scenarios": {
            "rows": [proxy, {**proxy, "scenario": "missing_20pct", "brier": 0.15}],
            "limitations": ["Historical final ABC proxy only."],
        },
        "price_uncertainty": {
            "rows": [
                {
                    "method": "advance",
                    "coverage90": 0.87,
                    "width90": 10.0,
                    "mae": 2.0,
                    "p95": 8.0,
                    "p99": 20.0,
                    "top1_error_share": 0.3,
                    "invalid": 0,
                }
            ],
            "limitations": [],
        },
        "queue_tradeoffs": {
            "rows": [
                {
                    "policy": "top_decile",
                    "method": method,
                    "workload_mode": mode,
                    "selected": 2,
                    "selection_rate": 0.1,
                    "empty_periods": 0,
                    "rise_rate": rise,
                    "fall_rate": 0.1,
                    "unknown_fraction": 0.0,
                }
                for method, rise in [("current", 0.2), ("advance", 0.4)]
                for mode in ["natural", "matched"]
            ],
            "limitations": [],
        },
        "peer_coverage": {
            "rows": [
                {
                    "subgroup": "zero_comparable_peers",
                    "method": "advance",
                    "candidates": 3,
                    "observed": 2,
                    "brier": 0.3,
                }
            ],
            "limitations": [],
        },
        "trials": {
            "rows": [
                {
                    "trial_id": "complete",
                    "method": "advance",
                    "status": "completed",
                    "training_rows": 100,
                    "runtime_seconds": 1.0,
                    "peak_rss_bytes": 200000,
                },
                {
                    "trial_id": "failed",
                    "method": "advance",
                    "status": "failed",
                    "training_rows": 1000,
                    "error": "Synthetic resource limit",
                },
            ],
            "limitations": [],
        },
    }
    pins = []
    for role, value in values.items():
        path = root / f"{role}.json"
        path.write_text(json.dumps(value))
        pins.append(_pin(path, path.name, role=role))
    review = pd.DataFrame(
        [
            {
                "row_key": key,
                "package_number": number,
                "period_start": "2025-01-06",
                "target_period": "2025-01-20",
                "scenario": "full_proxy",
                "policy": "top_decile",
                "next_aip_dkk": 102.0,
                "delta_dkk": 2.0,
                "selected": True,
            }
            for key, number in [("first", "000001"), ("second", "000002")]
        ]
    )
    outcomes = pd.DataFrame(
        [
            {
                "row_key": key,
                "next_status": status,
                "next_aip_ore": amount,
                "next_direction": direction,
            }
            for key, status, amount, direction in [
                ("first", "observed", 10300, "rise"),
                ("second", "pending", None, None),
            ]
        ]
    )
    for role, frame in [("candidate_review", review), ("candidate_outcomes", outcomes)]:
        path = root / f"{role}.parquet"
        frame.to_parquet(path, index=False)
        pins.append(_pin(path, path.name, role=role))
    result = root / "result.json"
    result.write_text(
        json.dumps(
            {
                "schema_version": "e3-saved-results-v1",
                "status": "completed",
                "evaluation_locked": True,
                "exposure_status": "All historical outcomes exposed; exploratory research.",
                "definitions": {
                    "price_basis": "AIP DKK/package ex VAT/fees",
                    "availability": "reconstructed",
                },
                "split_dates": [
                    {"split": "evaluation_all", "start": "2025-01-06", "end": "2025-01-20"}
                ],
                "files": pins,
            }
        )
    )
    index = root / "index.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": "e3-run-index-v1",
                "runs": [_pin(result, result.name, run_id="synthetic", label=label)],
            }
        )
    )
    return index


def _app(path: Path | None) -> AppTest:
    def render() -> None:
        from pathlib import Path

        import streamlit as st

        from bef_redbird.research.e3_dashboard import main

        value = st.session_state["path"]
        main(Path(value) if value else None)

    app = AppTest.from_function(render, default_timeout=30)
    app.session_state["path"] = str(path) if path else None
    return app.run()


def _review_frame(app: AppTest) -> pd.DataFrame:
    return next(frame.value for frame in app.dataframe if "package_number" in frame.value)


def _mutate(index: Path, role: str | None, change: Any) -> None:
    result = index.parent / "result.json"
    manifest = json.loads(result.read_text())
    if role:
        path = index.parent / f"{role}.json"
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))
        pin = next(item for item in manifest["files"] if item["role"] == role)
        pin.update(_pin(path, path.name, role=role))
    else:
        change(manifest)
    result.write_text(json.dumps(manifest))
    value = json.loads(index.read_text())
    value["runs"][0].update(_pin(result, result.name))
    index.write_text(json.dumps(value))


def test_e3_saved_views_are_read_only_and_preserve_frozen_aggregate(tmp_path: Path) -> None:
    index = fixture_index(tmp_path / "run")
    app = _app(index)
    assert not app.exception and not app.error
    assert not app.button and not app.success
    assert len(app.get("plotly_chart")) == 3
    assert app.dataframe[0].value.brier.tolist() == [0.2, 0.1]
    assert app.dataframe[0].value.information.tolist() == [
        "Current ABC only",
        "Advance ABC (historical proxy)",
    ]
    official = app.dataframe[0].value.copy()
    next(item for item in app.selectbox if item.label == "Saved review workload").set_value(
        "matched"
    ).run()
    assert not app.exception and app.dataframe[0].value.equals(official)
    assert any("failed trial" in item.value for item in app.warning)
    assert any("Large" in item.value or "large" in item.value for item in app.subheader)


def test_e3_candidate_review_is_opt_in_and_outcomes_require_separate_toggle(tmp_path: Path) -> None:
    index = fixture_index(tmp_path / "run")
    app = _app(index)
    assert len(app.checkbox) == 1
    app.checkbox[0].check().run()
    assert not app.exception and len(app.checkbox) == 2
    frame = _review_frame(app)
    assert "next_aip_dkk" in frame and "next_aip_ore" not in frame
    next(item for item in app.text_input if item.label == "Find package number").set_value(
        "000001"
    ).run()
    assert _review_frame(app).package_number.tolist() == ["000001"]
    app.checkbox[1].check().run()
    assert not app.exception
    assert _review_frame(app).next_aip_ore.tolist() == [10300]
    app.checkbox[1].uncheck().run()
    assert "next_aip_ore" not in _review_frame(app)


def test_historical_review_pages_reach_every_selected_origin_without_outcomes(
    tmp_path: Path,
) -> None:
    index = fixture_index(tmp_path / "run")
    base = pd.read_parquet(index.parent / "candidate_review.parquet").iloc[0].to_dict()
    review = pd.DataFrame(
        [
            {**base, "row_key": f"row-{i}", "package_number": f"{i:06}", "selected": i >= 500}
            for i in range(1003)
        ]
    )
    outcomes = pd.DataFrame(
        {
            "row_key": review.row_key,
            "next_status": ["observed"] * 1002 + ["pending"],
            "next_aip_ore": [10300] * 1002 + [None],
            "next_direction": ["rise"] * 1002 + [None],
        }
    )
    for role, frame in (("candidate_review", review), ("candidate_outcomes", outcomes)):
        path = index.parent / f"{role}.parquet"
        frame.to_parquet(path, index=False)

    def repin(manifest: dict[str, Any]) -> None:
        for role in ("candidate_review", "candidate_outcomes"):
            path = index.parent / f"{role}.parquet"
            next(item for item in manifest["files"] if item["role"] == role).update(
                _pin(path, path.name, role=role)
            )

    _mutate(index, None, repin)
    original_pins = {path.name: _pin(path, path.name) for path in index.parent.glob("*.parquet")}
    app = _app(index)
    app.checkbox[0].check().run()
    assert _review_frame(app).package_number.tolist() == review.package_number.iloc[:500].tolist()
    assert not _review_frame(app).selected.any()
    app.number_input[0].set_value(3).run()
    assert _review_frame(app).package_number.tolist() == ["001000", "001001", "001002"]
    assert any("Showing 1,001–1,003; page 3 of 3" in item.value for item in app.caption)

    # Selecting saved membership makes origins beyond the old cap directly reachable.
    selection = next(item for item in app.selectbox if item.label == "Saved selection")
    selection.set_value("Selected").run()
    assert app.number_input[0].value == 1
    assert (
        _review_frame(app).package_number.tolist() == review.package_number.iloc[500:1000].tolist()
    )
    app.number_input[0].set_value(2).run()
    assert _review_frame(app).package_number.tolist() == ["001000", "001001", "001002"]
    assert any("503 saved rows match. Showing 501–503; page 2 of 2" in c.value for c in app.caption)
    predictions = _review_frame(app).copy()
    assert "next_status" not in predictions
    app.checkbox[1].check().run()
    assert not app.exception and app.number_input[0].value == 2
    actual = _review_frame(app)
    assert actual.package_number.tolist() == predictions.package_number.tolist()
    assert actual.next_status.tolist() == ["observed", "observed", "pending"]
    assert pd.isna(actual.next_aip_ore.iloc[-1])
    assert actual[predictions.columns].equals(predictions)
    app.checkbox[1].uncheck().run()
    assert _review_frame(app).equals(predictions) and app.number_input[0].value == 2

    # Filter changes reset safely; empty search and one-page exclusion never retain page2.
    next(item for item in app.selectbox if item.label == "Saved selection").set_value(
        "Not selected"
    ).run()
    assert app.number_input[0].value == 1 and len(_review_frame(app)) == 500
    assert not _review_frame(app).selected.any()
    app.text_input[0].set_value("absent").run()
    assert app.number_input[0].value == 1 and not app.exception
    assert any("0 saved rows match. Showing 0–0; page 1 of 1" in c.value for c in app.caption)
    assert original_pins == {
        path.name: _pin(path, path.name) for path in index.parent.glob("*.parquet")
    }


def test_empty_missing_and_unindexed_results_are_explicit(tmp_path: Path) -> None:
    assert _app(None).info
    missing = _app(tmp_path / "absent.json")
    assert missing.error and not missing.dataframe and not missing.exception
    path = fixture_index(tmp_path / "run")
    with pytest.raises(ResearchError, match="not_in_index"):
        load_indexed_e3_run(load_e3_index(path), "arbitrary")


@pytest.mark.parametrize("reveal", [False, True])
@pytest.mark.parametrize("mutation", ["missing", "null", "string", "integer"])
def test_candidate_review_rejects_invalid_saved_selection(
    tmp_path: Path, mutation: str, reveal: bool
) -> None:
    index = fixture_index(tmp_path / "run")
    path = index.parent / "candidate_review.parquet"
    frame = pd.read_parquet(path)
    if mutation == "missing":
        frame = frame.drop(columns="selected")
    elif mutation == "null":
        frame["selected"] = pd.Series([True, None], dtype="boolean")
    elif mutation == "string":
        frame["selected"] = ["true", "false"]
    else:
        frame["selected"] = [1, 0]
    frame.to_parquet(path, index=False)

    def repin(manifest: dict[str, Any]) -> None:
        next(item for item in manifest["files"] if item["role"] == "candidate_review").update(
            _pin(path, path.name, role="candidate_review")
        )

    _mutate(index, None, repin)
    run = load_indexed_e3_run(load_e3_index(index), "synthetic")
    with pytest.raises(ResearchError, match="invalid_e3_candidate_review"):
        load_candidate_review(run, reveal_outcomes=reveal)
    app = _app(index)
    app.checkbox[0].check().run()
    if reveal:
        app.checkbox[1].check().run()
    assert not app.exception
    assert any("Saved candidate evidence is unavailable" in error.value for error in app.error)


def test_run_selector_reads_only_explicit_pinned_entries(tmp_path: Path) -> None:
    index = fixture_index(tmp_path / "catalogue")
    second = fixture_index(index.parent / "second", label="Second pinned result")
    value = json.loads(index.read_text())
    entry = json.loads(second.read_text())["runs"][0]
    entry.update(path="second/result.json", run_id="second")
    value["runs"].append(entry)
    index.write_text(json.dumps(value))
    # An unlisted malformed result is ignored, never auto-discovered.
    (index.parent / "unlisted.json").write_text("not a saved result")
    app = _app(index)
    assert app.selectbox[0].options == ["Synthetic E3", "Second pinned result"]
    app.selectbox[0].set_value("second").run()
    assert not app.exception and not app.error
    assert app.dataframe[0].value.brier.tolist() == [0.2, 0.1]


def test_scenario_gain_chart_uses_saved_paired_difference(tmp_path: Path) -> None:
    index = fixture_index(tmp_path / "run")
    _mutate(index, "comparisons", lambda value: value["rows"][0].update(scenario="full_proxy"))
    app = _app(index)
    assert not app.exception
    spec = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert spec["layout"]["yaxis"]["title"]["text"] == (
        "Brier reduction versus named baseline (higher better)"
    )
    assert "Advance ABC model versus Current ABC reference" in spec["data"][0]["name"]


@pytest.mark.parametrize("mutation", ["escape", "hash", "duplicate", "schema"])
def test_index_rejects_bad_pins_and_discovery(tmp_path: Path, mutation: str) -> None:
    path = fixture_index(tmp_path / "run")
    value = json.loads(path.read_text())
    if mutation == "escape":
        value["runs"][0]["path"] = "../outside.json"
    elif mutation == "hash":
        value["runs"][0]["sha256"] = "0" * 64
    elif mutation == "duplicate":
        value["runs"].append(value["runs"][0])
    else:
        value["schema_version"] = "unknown"
    path.write_text(json.dumps(value))
    app = _app(path)
    assert app.error and not app.dataframe and not app.exception


@pytest.mark.parametrize(
    "mutation",
    [
        "unlocked",
        "running",
        "missing_role",
        "model_role",
        "unverified",
        "missing_keys",
        "bad_e2",
        "bad_interval",
        "unfinished_trial",
        "nonfinite",
    ],
)
def test_corrupt_incomplete_or_unreconciled_results_are_refused(
    tmp_path: Path, mutation: str
) -> None:
    path = fixture_index(tmp_path / "run")
    if mutation == "unlocked":
        _mutate(path, None, lambda value: value.update(evaluation_locked=False))
    elif mutation == "running":
        _mutate(path, None, lambda value: value.update(status="running"))
    elif mutation == "missing_role":
        _mutate(path, None, lambda value: value["files"].pop())
    elif mutation == "model_role":
        _mutate(path, None, lambda value: value["files"][0].update(role="model"))
    elif mutation == "unverified":
        _mutate(path, "comparisons", lambda value: value["binding"].update(verified=False))
    elif mutation == "missing_keys":
        _mutate(path, "comparisons", lambda value: value["binding"].pop("candidate_keys_sha256"))
    elif mutation == "bad_e2":
        (path.parent / "e2.json").write_text("{}")
    elif mutation == "bad_interval":
        _mutate(path, "comparisons", lambda value: value["rows"][0].update(lower=1, upper=0))
    elif mutation == "unfinished_trial":
        _mutate(path, "trials", lambda value: value["rows"][0].update(status="running"))
    else:
        _mutate(path, "summary", lambda value: value["rows"][0].update(brier=float("nan")))
    app = _app(path)
    assert app.error and not app.dataframe and not app.exception


def test_parquet_hash_is_checked_again_before_review(tmp_path: Path) -> None:
    index = fixture_index(tmp_path / "run")
    run = load_indexed_e3_run(load_e3_index(index), "synthetic")
    (index.parent / "candidate_review.parquet").write_bytes(b"changed")
    with pytest.raises(ResearchError, match="hash_mismatch"):
        load_candidate_review(run)


def test_outcome_key_alignment_is_required(tmp_path: Path) -> None:
    index = fixture_index(tmp_path / "run")
    path = index.parent / "candidate_outcomes.parquet"
    frame = pd.read_parquet(path)
    frame.loc[0, "row_key"] = "different"
    frame.to_parquet(path, index=False)
    result = index.parent / "result.json"
    manifest = json.loads(result.read_text())
    next(item for item in manifest["files"] if item["role"] == "candidate_outcomes").update(
        _pin(path, path.name, role="candidate_outcomes")
    )
    result.write_text(json.dumps(manifest))
    _mutate(index, None, lambda value: None)
    run = load_indexed_e3_run(load_e3_index(index), "synthetic")
    with pytest.raises(ResearchError, match="unreconciled"):
        load_candidate_review(run, reveal_outcomes=True)


def test_viewer_imports_no_execution_or_live_database_code() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bef_redbird.research.e3_dashboard; "
            "assert 'catboost' not in sys.modules; assert 'duckdb' not in sys.modules; "
            "assert not any(k.startswith('bef_redbird.foundation') for k in sys.modules); "
            "assert not any(k in sys.modules for k in ['bef_redbird.research.training', "
            "'bef_redbird.research.e2_experiment', 'bef_redbird.research.evaluation'])",
        ],
        check=True,
        capture_output=True,
    )


def publisher_schema_fixture(root: Path) -> Path:
    index = fixture_index(root)
    base = {
        "task": "direction",
        "split": "evaluation_all",
        "subgroup": "all",
        "information": "advance_abc_historical_proxy",
        "peer_set": "equal",
        "scenario": "perfect",
        "candidates": 12,
        "observed": 10,
        "periods": 2,
        "scored_periods": 2,
        "method": "cb_equal_advance",
        "label": "cb equal advance",
        "brier": 0.1,
        "ece": 0.04,
    }
    _mutate(
        index,
        "summary",
        lambda value: value.update(
            rows=[
                {**base, "method": "cb_equal_current", "information": "current_abc", "brier": 0.2},
                base,
                {**base, "split": "per_period", "period_start": "2025-01-06"},
            ]
        ),
    )
    pair = {
        "baseline": "cb_equal_current",
        "candidate": "cb_equal_advance",
        "metric": "brier",
        "comparison": "advance_information",
        "subgroup": "all",
        "split": "evaluation_all",
        "scenario": "perfect",
        "block_length": 4,
        "mean_difference": -0.1,
        "lower": -0.15,
        "upper": -0.05,
        "periods": 2,
    }
    pairs = [
        pair,
        {**pair, "block_length": 8, "mean_difference": -0.09},
        {**pair, "subgroup": "equal_zero", "mean_difference": -0.08},
        {
            **pair,
            "comparison": "same_information_algorithm",
            "baseline": "logistic_equal_advance",
            "mean_difference": -0.07,
        },
        {**pair, "scenario": "missing25", "mean_difference": -0.05},
        {
            **pair,
            "candidate": "cb_package_advance",
            "baseline": "cb_package_current",
            "mean_difference": -0.025,
        },
    ]
    _mutate(index, "comparisons", lambda value: value.update(rows=pairs))
    _mutate(
        index,
        "scenarios",
        lambda value: value.update(
            rows=[
                base,
                {**base, "scenario": "missing25", "brier": 0.15},
                {**base, "method": "cb_package_advance", "peer_set": "package"},
            ]
        ),
    )
    amount = {
        **base,
        "task": "magnitude",
        "method": "amount_log_ratio_advance",
        "mae_dkk": 2.0,
        "coverage90": 0.87,
        "width90_dkk": 10.0,
        "p95_dkk": 8.0,
        "p99_dkk": 20.0,
        "top1_error_share": 0.3,
        "pooled_p95_dkk": 8.0,
        "pooled_p99_dkk": 20.0,
        "pooled_max_error_dkk": 35.0,
        "pooled_top1_error_share": 0.3,
        "pooled_coverage90": 0.87,
        "pooled_width90_dkk": 10.0,
        "invalid": 0,
    }
    period_amount = {key: value for key, value in amount.items() if not key.startswith("pooled_")}
    _mutate(
        index,
        "price_uncertainty",
        lambda value: value.update(
            rows=[
                amount,
                {**period_amount, "split": "per_period", "period_start": "2025-01-06"},
                {
                    **period_amount,
                    "split": "per_period",
                    "period_start": "2025-01-20",
                    "coverage90": 0.91,
                },
                {**amount, "scenario": "missing25", "coverage90": 0.5},
            ]
        ),
    )
    queue = {
        "policy": "risk_rank",
        "method": "cb_equal_advance_calibrated",
        "scenario": "perfect",
        "split": "evaluation_all",
        "workload_mode": "natural",
        "selected": 2,
        "selection_rate": 0.1,
        "empty_periods": 0,
        "rise_rate": 0.4,
        "fall_rate": 0.1,
        "unknown_fraction": 0.0,
        "parameter": 0.5,
    }
    queues = [
        queue,
        {
            **queue,
            "policy": "risk_rank_matched_e2",
            "method": "e2",
            "workload_mode": "matched_count",
        },
        {**queue, "scenario": "missing25", "rise_rate": 0.01},
        {**queue, "split": "per_period", "period_start": "2025-01-06", "rise_rate": 0.99},
        {
            **queue,
            "policy": "grid_risk_rank_0",
            "workload_mode": "descriptive_grid",
            "parameter": 0.25,
        },
        {
            **queue,
            "policy": "grid_risk_rank_1",
            "workload_mode": "descriptive_grid",
            "parameter": 0.5,
        },
        {
            **queue,
            "policy": "grid_risk_rank_2",
            "workload_mode": "descriptive_grid",
            "parameter": 1.0,
        },
        {
            **queue,
            "policy": "grid_threshold_0",
            "workload_mode": "descriptive_grid",
            "parameter": [0.15, 0.1],
        },
        {
            **queue,
            "policy": "grid_threshold_2",
            "workload_mode": "descriptive_grid",
            "parameter": [0.25, 0.1],
        },
    ]
    # Match score_queues' actual saved JSON: display string plus typed curve fields.
    for row in queues:
        parameter = row.get("parameter")
        if "risk_rank" in row["policy"]:
            row["lambda"] = float(parameter)
        elif "threshold" in row["policy"]:
            row["rise_threshold"], row["fall_threshold"] = parameter
        row["parameter"] = str(tuple(parameter) if isinstance(parameter, list) else parameter)
    _mutate(index, "queue_tradeoffs", lambda value: value.update(rows=queues))
    _mutate(
        index,
        "peer_coverage",
        lambda value: value.update(
            rows=[
                {**base, "subgroup": "equal_zero"},
                {**base, "subgroup": "equal_zero", "scenario": "missing25"},
                {
                    **base,
                    "subgroup": "equal_zero",
                    "split": "per_period",
                    "period_start": "2025-01-06",
                },
                {**base, "subgroup": "equal_supported"},
            ]
        ),
    )
    return index


def test_publisher_schema_defaults_are_compact_and_gain_curves_are_not_duplicated(
    tmp_path: Path,
) -> None:
    app = _app(publisher_schema_fixture(tmp_path / "run"))
    assert not app.exception and not app.error
    assert len(app.dataframe[0].value) == 2
    paired = app.dataframe[1].value
    assert len(paired) == 3  # Whole cohort, perfect scenario, block 4; retains different questions.
    assert 8 not in paired.get("block_length", [])
    method = next(
        item for item in app.selectbox if item.label == "Advance-information comparison method"
    )
    assert method.value == "cb_equal_advance"
    gain = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert len(gain["data"]) == 1
    assert gain["data"][0]["x"] == ["Final-code proxy", "25% codes missing"]
    assert "CatBoost · comparable peers" in gain["data"][0]["name"]
    assert (
        len(app.get("plotly_chart")) == 8
    )  # gain, interval coverage/width, queue two, fixed grid three
    official = app.dataframe[0].value.copy()
    method.set_value("cb_package_advance").run()
    assert not app.exception and app.dataframe[0].value.equals(official)
    gain = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert "package details" in gain["data"][0]["name"]


def test_publisher_queue_and_interval_views_default_to_perfect_and_keep_aggregate_separate(
    tmp_path: Path,
) -> None:
    app = _app(publisher_schema_fixture(tmp_path / "run"))
    assert not app.exception
    queue_scenario = next(
        item for item in app.selectbox if item.label == "Review-queue sensitivity scenario"
    )
    interval_scenario = next(
        item for item in app.selectbox if item.label == "Price-interval scenario"
    )
    assert queue_scenario.value == interval_scenario.value == "perfect"
    queue = next(
        frame.value
        for frame in app.dataframe
        if "policy" in frame.value and "rise_rate" in frame.value and "split" not in frame.value
    )
    assert queue.rise_rate.tolist() == [0.4]
    coverage = json.loads(app.get("plotly_chart")[1].proto.spec)
    assert coverage["layout"]["yaxis"]["title"]["text"] == "Observed coverage of 90% interval"
    assert coverage["data"][0]["x"] == ["2025-01-06", "2025-01-20"]
    queue_scenario.set_value("missing25").run()
    assert not app.exception
    queue = next(
        frame.value
        for frame in app.dataframe
        if "policy" in frame.value and "rise_rate" in frame.value and "split" not in frame.value
    )
    assert queue.rise_rate.tolist() == [0.01]


def test_saved_policy_grid_connects_risk_parameters_and_fixed_fall_thresholds() -> None:
    from bef_redbird.research.e3_dashboard import queue_curve_rows

    rows = [
        {
            "policy": f"grid_risk_rank_{index}",
            "parameter": parameter,
            "workload_mode": "descriptive_grid",
            "split": "evaluation_all",
        }
        for index, parameter in enumerate([0.25, 0.5, 1.0])
    ]
    result = queue_curve_rows(rows)
    assert {row["curve_family"] for row in result} == {"Fall penalty λ"}
    assert [row["parameter_value"] for row in result] == [0.25, 0.5, 1.0]
    result = queue_curve_rows(
        [
            {
                "policy": f"grid_threshold_{index}",
                "parameter": parameter,
                "workload_mode": "descriptive_grid",
                "split": "evaluation_all",
            }
            for index, parameter in enumerate([[0.15, 0.1], [0.25, 0.1], [0.15, 0.2]])
        ]
    )
    assert [row["curve_family"] for row in result] == [
        "Fall probability cap 0.1",
        "Fall probability cap 0.1",
        "Fall probability cap 0.2",
    ]


def test_all_publisher_method_families_have_plain_labels() -> None:
    from bef_redbird.research.e3_dashboard import method_label

    assert method_label("cb_package_current") == "CatBoost · package details · current ABC"
    assert "broader peers" in method_label("cb_broad_advance")
    assert "Simple regression" in method_label("logistic_equal_advance")
    assert "Broader → comparable → package fallback" in method_label("fallback_broad_advance")
    assert "nonnegative next price" in method_label("amount_delta_floor_current")
    assert "E2 · No price change" == method_label("e2_direction_no_change")
    assert "CatBoost price change" in method_label("e2_magnitude_catboost_group_mae")


@pytest.mark.parametrize("reveal", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "review_duplicate",
        "review_null",
        "outcome_duplicate",
        "outcome_null",
        "missing_outcome",
        "extra_outcome",
    ],
)
def test_candidate_review_requires_one_exact_nonnull_eligible_key_universe(
    tmp_path: Path, mutation: str, reveal: bool
) -> None:
    index = fixture_index(tmp_path / "run")
    role = "candidate_review" if mutation.startswith("review_") else "candidate_outcomes"
    path = index.parent / f"{role}.parquet"
    frame = pd.read_parquet(path)
    if mutation.endswith("duplicate"):
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    elif mutation.endswith("null"):
        frame.loc[0, "row_key"] = None
    elif mutation == "missing_outcome":
        frame = frame.iloc[1:]
    else:
        extra = frame.iloc[[0]].copy()
        extra["row_key"] = "not-an-eligible-review-origin"
        frame = pd.concat([frame, extra], ignore_index=True)
    frame.to_parquet(path, index=False)

    def repin(manifest: dict[str, Any]) -> None:
        next(item for item in manifest["files"] if item["role"] == role).update(
            _pin(path, path.name, role=role)
        )

    _mutate(index, None, repin)
    run = load_indexed_e3_run(load_e3_index(index), "synthetic")
    with pytest.raises(ResearchError):
        load_candidate_review(run, reveal_outcomes=reveal)


def test_extreme_error_display_uses_pooled_values_and_labels_period_means_separately(
    tmp_path: Path,
) -> None:
    index = publisher_schema_fixture(tmp_path / "run")
    # One period has one 100-DKK error, the next has 99 zero errors.
    # Equal-period MAE and mean period maximum are 50; pooled maximum is 100.
    aggregate = {
        "task": "magnitude",
        "split": "evaluation_all",
        "scenario": "perfect",
        "subgroup": "all",
        "method": "amount_log_ratio_advance",
        "information": "advance_abc_historical_proxy",
        "candidates": 100,
        "observed": 100,
        "periods": 2,
        "scored_periods": 2,
        "mae_dkk": 50.0,
        "pooled_mae_dkk": 1.0,
        "p95_dkk": 50.0,
        "p99_dkk": 50.0,
        "max_error_dkk": 50.0,
        "top1_error_share": 0.5,
        "pooled_p95_dkk": 0.0,
        "pooled_p99_dkk": 1.0,
        "pooled_max_error_dkk": 100.0,
        "pooled_top1_error_share": 1.0,
        "coverage90": 0.5,
        "pooled_coverage90": 0.99,
        "width90_dkk": 20.0,
        "pooled_width90_dkk": 10.2,
    }
    period = {key: value for key, value in aggregate.items() if not key.startswith("pooled_")}
    _mutate(index, "summary", lambda value: value["rows"].append(aggregate))
    _mutate(
        index,
        "price_uncertainty",
        lambda value: value.update(
            rows=[
                aggregate,
                {**aggregate, "split": "evaluation_2025"},
                {
                    **period,
                    "split": "per_period",
                    "period_start": "2025-01-06",
                    "candidates": 1,
                    "observed": 1,
                    "mae_dkk": 100.0,
                    "coverage90": 0.0,
                    "width90_dkk": 30.0,
                    "p95_dkk": 100.0,
                    "p99_dkk": 100.0,
                    "max_error_dkk": 100.0,
                    "top1_error_share": 1.0,
                },
                {
                    **period,
                    "split": "per_period",
                    "period_start": "2025-01-20",
                    "candidates": 99,
                    "observed": 99,
                    "mae_dkk": 0.0,
                    "coverage90": 1.0,
                    "width90_dkk": 10.0,
                    "p95_dkk": 0.0,
                    "p99_dkk": 0.0,
                    "max_error_dkk": 0.0,
                    "top1_error_share": 0.0,
                },
            ]
        ),
    )
    app = _app(index)
    assert not app.exception and not app.error
    tails = next(
        frame.value
        for frame in app.dataframe
        if set(frame.value.columns)
        == {
            "method",
            "pooled_p95_dkk",
            "pooled_p99_dkk",
            "pooled_max_error_dkk",
            "pooled_top1_error_share",
        }
    )
    assert tails.pooled_max_error_dkk.tolist() == [100.0]
    assert tails.pooled_p95_dkk.tolist() == [0.0]
    assert tails.pooled_p99_dkk.tolist() == [1.0]
    assert tails.pooled_top1_error_share.tolist() == [1.0]
    details = next(
        frame.value
        for frame in app.dataframe
        if "mean_period_max_error_dkk" in frame.value and "max_error_dkk" in frame.value
    )
    assert details.loc[
        details.split.eq("evaluation_all"), "mean_period_max_error_dkk"
    ].tolist() == [50.0]
    assert details.loc[details.split.eq("per_period"), "max_error_dkk"].tolist() == [100.0, 0.0]
    error_table = next(
        frame.value
        for frame in app.dataframe
        if set(frame.value.columns) == {"method", "observed", "mae_dkk"}
    )
    assert error_table.mae_dkk.tolist() == [50.0]
    interval_table = next(
        frame.value
        for frame in app.dataframe
        if "coverage90" in frame.value
        and "pooled_coverage90" in frame.value
        and "split" not in frame.value
    )
    assert interval_table.coverage90.tolist() == [0.5]
    assert interval_table.pooled_coverage90.tolist() == [0.99]
    text = " ".join(item.value for item in app.caption)
    assert "each tariff period equal weight" in text
    assert "pool all observed" in text
    from bef_redbird.research.e3_dashboard import LABELS

    assert "Mean of period largest" in LABELS["mean_period_max_error_dkk"]
    assert "all observed cases" in LABELS["pooled_max_error_dkk"]
    next(item for item in app.selectbox if item.label == "Forecast task").set_value(
        "magnitude"
    ).run()
    assert not app.exception and app.dataframe[0].value.mae_dkk.tolist() == [50.0]
    assert app.dataframe[0].value.pooled_p95_dkk.tolist() == [0.0]
    assert "mean_period_p95_dkk" not in app.dataframe[0].value


def test_serialized_policy_display_strings_use_declared_numeric_coordinates_only() -> None:
    from bef_redbird.research.e3_dashboard import queue_curve_rows

    base = {"workload_mode": "descriptive_grid", "split": "evaluation_all"}
    rows = [
        {**base, "policy": "grid_risk_rank_0", "parameter": "0.25", "lambda": 0.25},
        {
            **base,
            "policy": "grid_risk_rank_1",
            "parameter": "display only; not code",
            "lambda": 0.5,
        },
        {**base, "policy": "grid_risk_rank_2", "parameter": "1.0", "lambda": 1.0},
        {
            **base,
            "policy": "grid_threshold_0",
            "parameter": "(0.15, 0.1)",
            "rise_threshold": 0.15,
            "fall_threshold": 0.1,
        },
        {
            **base,
            "policy": "grid_threshold_2",
            "parameter": "(0.25, 0.1)",
            "rise_threshold": 0.25,
            "fall_threshold": 0.1,
        },
    ]
    original = json.loads(json.dumps(rows))
    result = queue_curve_rows(rows)
    assert rows == original
    assert [row["parameter_value"] for row in result] == [0.25, 0.5, 1.0, 0.15, 0.25]
    assert len({row["curve_family"] for row in result[:3]}) == 1
    assert len({row["curve_family"] for row in result[3:]}) == 1
    # Unspecified/invalid numeric coordinates are not inferred by evaluating display strings.
    assert (
        queue_curve_rows([{**base, "policy": "grid_threshold_0", "parameter": "(0.25, 0.1)"}]) == []
    )
    assert (
        queue_curve_rows(
            [{**base, "policy": "grid_risk_rank_0", "parameter": "0.25", "lambda": None}]
        )
        == []
    )


def test_actual_publisher_serialized_grid_renders_connected_risk_and_threshold_curves(
    tmp_path: Path,
) -> None:
    index = publisher_schema_fixture(tmp_path / "run")
    saved = json.loads((index.parent / "queue_tradeoffs.json").read_text())["rows"]
    grid = [row for row in saved if row["workload_mode"] == "descriptive_grid"]
    assert all(isinstance(row["parameter"], str) for row in grid)
    assert [row["lambda"] for row in grid if "risk_rank" in row["policy"]] == [0.25, 0.5, 1.0]
    app = _app(index)
    assert not app.exception and len(app.get("plotly_chart")) == 8

    def x_values(spec: dict[str, Any]) -> list[float]:
        value = spec["data"][0]["x"]
        if isinstance(value, list):
            return value
        assert value["dtype"] == "f8"
        raw = base64.b64decode(value["bdata"])
        return list(struct.unpack("<" + "d" * (len(raw) // 8), raw))

    spec = json.loads(app.get("plotly_chart")[5].proto.spec)
    assert len(spec["data"]) == 1 and x_values(spec) == [0.25, 0.5, 1.0]
    assert spec["data"][0]["name"] == "Fall penalty λ"
    next(item for item in app.selectbox if item.label == "Fixed policy curve").set_value(
        "Probability thresholds"
    ).run()
    assert not app.exception
    spec = json.loads(app.get("plotly_chart")[5].proto.spec)
    assert len(spec["data"]) == 1 and x_values(spec) == [0.15, 0.25]
    assert spec["data"][0]["name"] == "Fall probability cap 0.1"
