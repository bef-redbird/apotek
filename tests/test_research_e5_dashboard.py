from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e5_dashboard import (
    display_values,
    pinned,
    read_rows,
    recommendation_flags,
)
from bef_redbird.research.group_features import pin


def save_json(path: Path, value: dict) -> dict:
    path.write_text(json.dumps(value), encoding="utf-8")
    return pin(path)


def fixture(tmp_path: Path, n: int = 120) -> Path:
    metadata = save_json(tmp_path / "protocol.json", {"version": "synthetic"})
    review = pd.DataFrame(
        {
            "row_key": [f"r{i}" for i in range(n)],
            "package_number": [f"{i:06d}" for i in range(n)],
            "period_start": pd.to_datetime(["2026-10-26"] * n),
            "aip_ore": [10000] * n,
            "p_rise": [0.6] * n,
            "p_fall": [0.05] * n,
            "reference_p_rise": [0.8] * n,
            "reference_p_fall": [0.03] * n,
            "forecast_delta_dkk": [10.0] * n,
            "forecast_percent": [10.0] * n,
            "median_delta_dkk": [2.0] * n,
            "lower_delta_dkk": [-5.0] * n,
            "upper_delta_dkk": [20.0] * n,
            "persistence_probability": [0.4] * n,
            "eligibility_reason": ["origin supported"] * n,
            "eligible": [True] * (n - 1) + [False],
            "policy_eligible": [True] * (n - 1) + [False],
            "selection_reason": ["Saved E4 reference"] * n,
            "information_route": ["current-only fallback"] * n,
            "peer_route": ["equal-size peers"] * n,
            "interval_method": ["Retained E4 price-band residual interval"] * n,
            "package_distributor_name": ["Package company"] * n,
            "drug_distributor_name": ["Drug company"] * n,
            "authorisation_holder_name": ["Authorisation company"] * n,
            "selected": [True] * (n - 1) + [False],
            "rank": list(range(1, n + 1)),
        }
    )
    review.to_parquet(tmp_path / "review.parquet", index=False)
    pd.DataFrame(
        {"row_key": review.row_key, "next_status": ["pending"] * n, "next_aip_ore": [None] * n}
    ).to_parquet(tmp_path / "outcomes.parquet", index=False)
    results = save_json(
        tmp_path / "results.json",
        {
            "version": "e5-results-v1",
            "status": "completed",
            "exposure": "Exposed retrospective research",
            "direction": [{"fold": "spring", "method": "reference", "brier": 0.2, "observed": 90}],
            "amount": [{"method": "rmse", "rmse_dkk": 15.0, "observed": 90}],
            "intervals": [],
            "financial": [],
            "persistence": [],
            "scenarios": [],
            "conclusions": ["Synthetic result includes losses."],
            "selection": {"promoted": False},
            "limitations": ["No pharmacy transactions."],
        },
    )
    index = {
        "version": "e5-index-v1",
        "results": results,
        "protocol": metadata,
        "review": pin(tmp_path / "review.parquet"),
        "outcomes": pin(tmp_path / "outcomes.parquet"),
        "population": metadata,
        "fit_ledger": metadata,
    }
    save_json(tmp_path / "index.json", index)
    return tmp_path / "index.json"


def app(path: Path | None) -> AppTest:
    argument = "None" if path is None else f"Path({str(path)!r})"
    return AppTest.from_string(
        "from pathlib import Path\nfrom bef_redbird.research.e5_dashboard import main\n"
        f"main({argument})"
    ).run()


def tables(value: AppTest) -> list[pd.DataFrame]:
    return [x.value for x in value.dataframe if "Package" in x.value]


def test_missing_invalid_and_pending_saved_index(tmp_path: Path) -> None:
    missing = app(None)
    assert not missing.exception and "Choose a saved E5" in missing.info[0].value
    path = tmp_path / "index.json"
    save_json(path, {"version": "wrong"})
    invalid = app(path)
    assert not invalid.exception and "Incompatible" in invalid.error[0].value
    result = save_json(tmp_path / "results.json", {"version": "e5-results-v1", "status": "pending"})
    save_json(path, {"version": "e5-index-v1", "results": result})
    pending = app(path)
    assert not pending.exception and not pending.error
    assert any("No saved candidate review" in x.value for x in pending.info)


def test_actual_component_cutoffs_and_resource_fallback_are_visible_and_consistent(
    tmp_path: Path,
) -> None:
    path = fixture(tmp_path, 2)
    index = json.loads(path.read_text())
    decision = save_json(tmp_path / "decision.json", {"reason": "unchanged resource cap"})
    components = {"cb_broad_current": "2026-07-01", "mean_current": "2026-09-01"}
    bundle = save_json(
        tmp_path / "bundle.json",
        {
            "direction_fit_cutoff": "2026-07-01",
            "amount_fit_cutoff": "2026-09-01",
            "planned_final_fit_cutoff": "2026-09-01",
            "component_fit_cutoffs": components,
            "resource_fallback": decision,
        },
    )
    index["model_bundle"] = bundle
    assessment = {
        "binding": {"models": bundle},
        "fit_cutoff": "2026-07-01",
        "amount_fit_cutoff": "2026-09-01",
        "component_fit_cutoffs": components,
    }
    index["assessment"] = save_json(tmp_path / "assessment.json", assessment)
    save_json(path, index)
    before = (tmp_path / "results.json").read_bytes()
    value = app(path)
    assert not value.error and not value.exception
    assert any(
        "unchanged resource limit" in r.value and "2026-07-01" in r.value for r in value.warning
    )
    table = next(r.value for r in value.dataframe if "Training cutoff" in r.value)
    assert table["Training cutoff"].tolist() == ["2026-07-01", "2026-09-01", "2026-01-01"]
    assert (tmp_path / "results.json").read_bytes() == before
    assessment["fit_cutoff"] = "2026-09-01"
    index["assessment"] = save_json(tmp_path / "assessment.json", assessment)
    save_json(path, index)
    invalid = app(path)
    assert any("training cutoffs disagree" in r.value for r in invalid.error)


def test_saved_only_candidates_limits_search_and_separate_outcome_reveal(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    value = app(path)
    assert not value.exception and not value.error
    assert len(value.tabs) == 9 and len(tables(value)[0]) == 100
    assert not any("Later next-price status" in t for t in tables(value))
    value.number_input[0].set_value(2).run()
    assert len(tables(value)[0]) == 2
    value.number_input[0].set_value(60).run()
    assert len(tables(value)[0]) == 60
    value.checkbox[1].check().run()
    assert len(tables(value)[0]) == 120
    value.text_input[0].set_value("000001").run()
    assert tables(value)[0].Package.tolist() == ["000001"]
    value.checkbox[0].check().run()
    outcomes = next(t for t in tables(value) if "Later next-price status" in t)
    assert outcomes["Later next-price status"].tolist() == ["pending"]
    assert "Later next-price status" not in tables(value)[0]
    value.radio[0].set_value("Excluded at origin").run()
    assert not tables(value)
    assert any("No candidates match" in x.value for x in value.info)


def test_changed_or_missing_pin_fails_without_live_fallback(tmp_path: Path) -> None:
    path = fixture(tmp_path, 2)
    (tmp_path / "outcomes.parquet").write_bytes(b"changed hidden attachment")
    value = app(path)
    assert not value.exception and "checksum" in value.error[0].value
    assert not value.dataframe
    with pytest.raises(ResearchError):
        pinned({"path": "missing", "sha256": "wrong", "size_bytes": 0})


def test_manifest_rows_and_candidate_outcome_boundary(tmp_path: Path) -> None:
    path = fixture(tmp_path, 2)
    manifest = save_json(tmp_path / "outcome.json", {"rows": pin(tmp_path / "outcomes.parquet")})
    assert len(read_rows(manifest)) == 2
    review = pd.read_parquet(tmp_path / "review.parquet").assign(next_aip_ore=999)
    review.to_parquet(tmp_path / "review.parquet", index=False)
    index = json.loads(path.read_text(encoding="utf-8"))
    index["review"] = pin(tmp_path / "review.parquet")
    save_json(path, index)
    value = app(path)
    assert not value.exception and "saved separately" in value.error[0].value


def test_plain_method_values_preserve_raw_and_common_calibration_meanings() -> None:
    source = pd.DataFrame(
        {
            "method": ["frozen", "frozen", "accepted_e4", "fresh", "tuned"],
            "fold": ["spring"] * 5,
            "population": [
                "full_routes_raw",
                "full_routes_calibrated",
                "full_routes_calibrated",
                "full_routes_calibrated",
                "full_routes_calibrated",
            ],
        }
    )
    shown = display_values(source)
    assert shown.method.tolist() == [
        "Frozen E4 models · raw",
        "Frozen E4 models · common recalibration",
        "Accepted E4 probabilities · original calibration",
        "Refreshed, unchanged settings",
        "Refreshed, tuned settings",
    ]
    assert source.method.tolist() == ["frozen", "frozen", "accepted_e4", "fresh", "tuned"]


def test_saved_temporal_contrasts_and_feature_explanations_are_readable(tmp_path: Path) -> None:
    path = fixture(tmp_path, 2)
    index = json.loads(path.read_text(encoding="utf-8"))
    index["contrasts"] = save_json(
        tmp_path / "contrasts.json",
        {
            "contrasts": [
                {"candidate": "fresh", "reference": "frozen", "mean_brier_difference": -0.01}
            ],
            "periods": [{"method": "accepted_e4", "fold": "spring", "brier": 0.2}],
        },
    )
    index["explanations"] = save_json(
        tmp_path / "explanations.json",
        {
            "claim": "Contributions are not causal effects.",
            "sampling": "Frozen origin sample.",
            "summaries": [
                {
                    "model": "amount_current",
                    "units": "DKK change",
                    "features": [{"feature": "aip_ore", "mean_absolute_contribution": 1.0}],
                }
            ],
            "cases": [
                {
                    "row_key": "r1",
                    "model": "amount_current",
                    "top_contributions": [{"feature": "aip_ore", "contribution": 2.0}],
                }
            ],
        },
    )
    save_json(path, index)
    value = app(path)
    assert not value.exception and not value.error
    assert any("Paired temporal comparisons" == x.value for x in value.subheader)
    assert any("What the models used" == x.value for x in value.subheader)
    assert any(
        "Refreshed, unchanged settings" in x.value.get("Candidate", []).tolist()
        for x in value.dataframe
        if "Candidate" in x.value
    )
    assert any(
        "Current AIP · øre" in x.value.get("Feature", []).tolist()
        for x in value.dataframe
        if "Feature" in x.value
    )


def test_core_candidate_explanations_and_original_reference_are_visible(tmp_path: Path) -> None:
    value = app(fixture(tmp_path, 2))
    assert not value.exception and not value.error
    visible = tables(value)[:-1]  # exclude the optional all-fields expander
    columns = {c for table in visible for c in table.columns}
    assert {
        "Original E4 rise probability",
        "Original E4 fall probability",
        "E5 rise probability",
        "E5 fall probability",
    } <= columns
    assert {
        "Information used / fallback",
        "Peer support used",
        "Selection explanation",
        "Displayed interval method",
        "Recommendation policy allows",
        "Package distributor name at origin",
        "Drug distributor name at origin",
        "Authorisation holder name at origin",
    } <= columns
    control = next(table for table in visible if "Original E4 rise probability" in table)
    assert control["Original E4 rise probability"].iloc[0] == 0.8
    assert control["E5 rise probability"].iloc[0] == 0.6


def test_forecasts_keep_primary_money_visible_in_compact_tables_and_reveal_outcomes_separately(
    tmp_path: Path,
) -> None:
    path = fixture(tmp_path, 2)
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    value = app(path)
    value.selectbox[0].set_value("2026-10-26").run()
    assert not value.exception and not value.error
    shown = tables(value)
    assert shown[0].columns.tolist() == [
        "Package",
        "Current AIP · DKK",
        "E5 rise probability",
        "Mean change · DKK / pack",
        "Forecast change · % of current AIP",
    ]
    assert shown[0]["Mean change · DKK / pack"].tolist() == [10, 10]
    assert shown[0]["Forecast change · % of current AIP"].tolist() == [10, 10]
    assert shown[1].columns.tolist() == [
        "Package",
        "Median change · DKK / pack",
        "Change interval lower · DKK / pack",
        "Change interval upper · DKK / pack",
        "Joint rise + persistence probability",
    ]
    assert "Original E4 rise probability" in shown[2]
    assert any("Displayed interval method: Retained E4" in x.value for x in value.caption)
    assert not any("Later next-price status" in t for t in shown)
    assert all(len(t.columns) <= 6 for t in shown[:-1])
    assert "Frozen rank" not in shown[0] and any("Frozen rank" in t for t in shown[3:-1])
    value.checkbox[0].check().run()
    revealed = tables(value)
    assert "Later next-price status" in revealed[3]
    assert not any("Later next-price status" in t for t in revealed[:3])
    assert all(len(t.columns) <= 6 for t in revealed[:-1])
    value.selectbox[0].set_value("All periods").run()
    assert "Origin period" in tables(value)[0] and len(tables(value)[0].columns) == 6
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_recommendation_flag_coalesces_sources_and_excludes_origin_ineligible() -> None:
    frame = pd.DataFrame(
        {
            "eligible": [True, True, False, True, True],
            "policy_eligible": [None, False, False, None, True],
            "recommendation_eligible": [False, None, False, None, False],
        }
    )
    result = recommendation_flags(frame)
    assert result.recommendation_policy_flagged.tolist() == [True, True, False, False, False]
    assert pd.isna(result.recommendation_policy_eligible.iloc[3])
    assert result.recommendation_policy_eligible.iloc[4]
    assert "recommendation_policy_flagged" not in frame


def test_latest_current_origin_default_all_periods_and_flagged_filter(tmp_path: Path) -> None:
    path = fixture(tmp_path, 5)
    review = pd.read_parquet(tmp_path / "review.parquet")
    review["period_start"] = pd.to_datetime(["2025-06-02"] + ["2026-10-26"] * 4)
    review["review_scope"] = ["historical_research"] + ["current_frozen_assessment"] * 4
    review["eligible"] = [True, True, False, True, True]
    review["policy_eligible"] = [None, False, False, None, True]
    review["recommendation_eligible"] = [False, None, False, None, False]
    review.to_parquet(tmp_path / "review.parquet", index=False)
    index = json.loads(path.read_text(encoding="utf-8"))
    index["review"] = pin(tmp_path / "review.parquet")
    index["assessment"] = save_json(
        tmp_path / "assessment.json", {"context": {"origin": "2026-10-26"}}
    )
    save_json(path, index)
    value = app(path)
    assert value.selectbox[0].value == "2026-10-26"
    assert len(tables(value)[0]) == 4
    assert value.metric[0].value == "4"
    value.radio[0].set_value("Flagged by recommendation policy").run()
    assert tables(value)[0].Package.tolist() == ["000001"]
    value.selectbox[0].set_value("All periods").run()
    assert set(tables(value)[0].Package) == {"000000", "000001"}


def test_overview_humanizes_saved_status_and_keeps_raw_metadata_in_provenance(
    tmp_path: Path,
) -> None:
    path = fixture(tmp_path, 2)
    result_path = tmp_path / "results.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    raw_status = "scientific_evaluation_complete_delivery_record_separate"
    result["status"] = raw_status
    save_json(result_path, result)
    index = json.loads(path.read_text(encoding="utf-8"))
    index["results"] = pin(result_path)
    save_json(path, index)
    value = app(path)
    assert not value.exception and not value.error
    assert any("Historical evaluation saved · research only" in x.value for x in value.markdown)
    assert not any(raw_status in x.value for x in value.markdown)
    assert any(raw_status in str(x.value) for x in value.json)


def test_package_alias_does_not_duplicate_display_columns(tmp_path: Path) -> None:
    path = fixture(tmp_path, 2)
    review_path = tmp_path / "review.parquet"
    review = pd.read_parquet(review_path)
    review["package_id"] = review.package_number
    review.to_parquet(review_path, index=False)
    index = json.loads(path.read_text(encoding="utf-8"))
    index["review"] = pin(review_path)
    save_json(path, index)
    value = app(path)
    assert not value.exception and not value.error
    assert all(not t.columns.duplicated().any() for t in tables(value))
