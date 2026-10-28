from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import bef_redbird.research.e6_cito as module
from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e3_methods import select_queue
from bef_redbird.research.e4_common import frozen_json, read
from bef_redbird.research.group_features import pin


def inputs() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_key": ["a", "b", "c", "d", "e"],
            "package_number": ["000001", "000002", "000003", "000004", "000005"],
            "period_start": pd.to_datetime(["2026-10-12"] * 5),
            "eligible": [True, True, True, True, False],
            "aip_ore": [10000, 10000, 10000, 10000, None],
            "origin_identity_token": ["drug1", "drug2", "drug3", "drug4", "drug5"],
            "advance_abc": ["C", "A", None, "B", "C"],
            "advance_state": ["observed", "observed", "absent", "observed", "observed"],
            "origin_group": ["g", "g", None, "h", "h"],
            "exclusion_reason": ["", "", "", "", "missing_aip"],
        }
    )


def comparison() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "package_number": ["000001", "000002", "000004", "000005", "999999"],
            "advance_abc": ["C", "A", "B", "C", "A"],
            "advance_state": ["observed"] * 5,
            "official_advance_abc": ["A", "A", "C", "B", "C"],
            "advance_comparison": [
                "incorrect",
                "correct",
                "identity_discontinuity_or_ambiguity",
                "incorrect",
                "incorrect",
            ],
        }
    )


def fake_predict(frame: pd.DataFrame, bundle: Path, calibration: Path) -> pd.DataFrame:
    del bundle
    out = frame.copy()
    available = out.eligible
    rise = out.advance_abc.map({"A": 0.9, "B": 0.7, "C": 0.01}).fillna(0.6)
    temp = read(calibration)["states"]["2026"]["temperature"]["hybrid"]["temperature"]
    rise = rise * temp
    out["prediction_state"] = np.where(available, "available", "excluded_origin")
    out["p_rise"] = rise.where(available)
    out["p_fall"] = pd.Series(0.05, index=out.index).where(available)
    out["p_unchanged"] = 1 - out.p_rise - out.p_fall
    out["next_aip_dkk"] = (100 + 10 * out.p_rise).where(available)
    out["change_dkk"] = out.next_aip_dkk - 100
    out["lower90_dkk"] = out.next_aip_dkk - 5
    out["upper90_dkk"] = out.next_aip_dkk + 5
    out["peer_route"] = "package history"
    out["information_route"] = np.where(
        out.advance_abc.notna(), "advance ABC", "current-only fallback"
    )
    out["risk_score"] = out.p_rise - 0.5 * out.p_fall
    queue = select_queue(
        out.loc[available, ["row_key", "period_start", "p_fall", "p_unchanged", "p_rise"]].rename(
            columns={"p_fall": "p0", "p_unchanged": "p1", "p_rise": "p2"}
        ),
        "threshold",
        (0.35, 0.1),
    )
    out["selected"] = False
    out["rank"] = np.nan
    out["selection_reason"] = "excluded"
    out.loc[available, "selected"] = queue.selected.to_numpy()
    out.loc[available, "rank"] = queue["rank"].to_numpy()
    out.loc[available, "selection_reason"] = queue.selection_reason.to_numpy()
    out["selected_risk_decile"] = out.selected
    return out


def labels() -> pd.DataFrame:
    out = inputs()[["row_key", "package_number", "aip_ore", "eligible", "origin_identity_token"]]
    out["status"] = [
        "observed",
        "observed",
        "unsupported_or_unresolved_target",
        "observed",
        "origin_ineligible",
    ]
    out["next_aip_ore"] = [11000, 9500, None, 10000, None]
    out["next_delta_ore"] = [1000, -500, None, 0, None]
    out["next_direction"] = [1, -1, None, 0, None]
    out["target_period"] = "2026-10-26"
    return out


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "e6"
    root.mkdir()
    frozen_json(root / "protocol.json", {"test": True})
    frozen_json(root / "freeze.json", {"protocol": pin(root / "protocol.json")})
    temp = {"temperature": 1.0, "cutoff": "2026-01-01", "last_maturity": "2025-12-22"}
    frozen_json(
        tmp_path / "calibration.json",
        {
            "states": {
                "2026": {
                    "temperature": {
                        "hybrid": temp,
                        "current": {**temp, "temperature": 0.8},
                    }
                }
            }
        },
    )
    frozen_json(tmp_path / "models.json", {"test": True})
    inputs().to_parquet(tmp_path / "inputs.parquet", index=False)
    comparison().to_parquet(tmp_path / "comparison.parquet", index=False)
    labels().to_parquet(tmp_path / "outcomes.parquet", index=False)
    fake_predict(inputs(), tmp_path / "models.json", tmp_path / "calibration.json").to_parquet(
        tmp_path / "accepted.parquet", index=False
    )
    source = {
        key: pin(tmp_path / file)
        for key, file in {
            "inputs": "inputs.parquet",
            "accepted_predictions": "accepted.parquet",
            "comparison_rows": "comparison.parquet",
            "outcome_rows": "outcomes.parquet",
            "calibration": "calibration.json",
            "models": "models.json",
        }.items()
    }
    monkeypatch.setattr(module, "_sources", lambda *_: source)
    monkeypatch.setattr(module, "predict", fake_predict)
    return root, source


def test_correction_preserves_identity_exclusions_missing_correct_and_all_other_cells() -> None:
    before = inputs()
    corrected, changed = module.corrected_inputs(before, comparison())
    assert corrected.advance_abc.tolist() == ["A", "A", None, "B", "B"]
    assert changed.row_key.tolist() == ["a", "e"]
    pd.testing.assert_frame_equal(
        before.drop(columns="advance_abc"), corrected.drop(columns="advance_abc")
    )
    assert before.advance_abc.iloc[0] == "C"
    missing = module.no_report_inputs(before)
    assert missing.advance_abc.isna().all()
    pd.testing.assert_frame_equal(
        before.drop(columns=["advance_abc", "advance_state"]),
        missing.drop(columns=["advance_abc", "advance_state"]),
    )


@pytest.mark.parametrize(
    "column,value",
    [("advance_state", "ambiguous"), ("official_advance_abc", "X"), ("advance_abc", "A")],
)
def test_reject_unproved_correction(column: str, value: str) -> None:
    c = comparison()
    c.loc[0, column] = value
    with pytest.raises(ResearchError):
        module.corrected_inputs(inputs(), c)


def test_reject_input_mismatch_and_duplicate_identity() -> None:
    f = inputs()
    f.loc[0, "advance_abc"] = "B"
    with pytest.raises(ResearchError, match="source_mismatch"):
        module.corrected_inputs(f, comparison())
    with pytest.raises(ResearchError, match="ambiguous_package"):
        module.corrected_inputs(inputs(), pd.concat([comparison(), comparison()]))


def test_four_arms_exact_replay_unknown_retention_and_immutable_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = setup(tmp_path, monkeypatch)
    result = module.run(root)
    assert result["actual_exact_replay"]
    assert result["correction"]["report_wrong_codes"] == 3
    assert result["correction"]["origin_wrong_codes"] == 2
    assert result["correction"]["eligible_wrong_codes"] == 1
    assert set(result["results"]) == set(module.SCENARIOS)
    actual = pd.read_parquet(root / "cito/actual-outcomes.parquet")
    corrected = pd.read_parquet(root / "cito/corrected_wrong_only-outcomes.parquet")
    current = pd.read_parquet(root / "cito/no_report_current-predictions.parquet")
    isolation = pd.read_parquet(root / "cito/no_report_hybrid_temperature-predictions.parquet")
    assert current.advance_abc.isna().all()
    assert current.p_rise.iloc[0] == pytest.approx(0.48)
    assert isolation.p_rise.iloc[0] == pytest.approx(0.6)
    assert actual.selected_natural.sum() == corrected.selected_matched.sum() == 1
    assert corrected.loc[corrected.row_key.eq("a"), "prediction_changed"].item()
    assert corrected.loc[corrected.row_key.eq("d"), "prediction_changed"].item() is False
    assert len(corrected) == len(labels()) == 5
    assert (
        corrected.loc[corrected.row_key.eq("c"), "outcome_status"].item()
        == "unsupported_or_unresolved_target"
    )
    assert "outcome_status" not in current
    assert module.run(root) == result
    (root / "cito/corrected-inputs.parquet").write_bytes(b"tampered")
    with pytest.raises(ResearchError):
        module.run(root)


def test_actual_replay_refuses_changed_prediction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, source = setup(tmp_path, monkeypatch)
    p = Path(source["accepted_predictions"]["path"])
    changed = pd.read_parquet(p)
    changed["p_rise"] = changed.p_rise + 0.001
    changed.to_parquet(p, index=False)
    source["accepted_predictions"] = pin(p)
    with pytest.raises(AssertionError):
        module.run(root)


def test_matched_queue_keeps_selected_unknown_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = setup(tmp_path, monkeypatch)
    original = pd.read_parquet(Path(source["accepted_predictions"]["path"]))
    f = inputs()
    f["advance_abc"] = ["C", "C", "A", "C", "C"]
    predicted = fake_predict(f, Path(source["models"]["path"]), Path(source["calibration"]["path"]))
    decisions = module.decisions(predicted, original, "corrected_wrong_only")
    assert decisions.loc[decisions.selected_matched, "row_key"].tolist() == ["c"]
    scored = module.attach_labels(decisions, labels())
    assert scored.loc[scored.selected_matched, "row_key"].tolist() == ["c"]
    values = module.exposure(scored.loc[scored.selected_matched])
    assert values["unknown"] == 1 and values["observed"] == 0
    assert values["unknown_reasons"] == {"unsupported_or_unresolved_target": 1}


def test_outcome_attachment_aligns_keys_and_preserves_unknown_and_exact_money() -> None:
    decision = inputs().assign(selected=True)
    scored = module.attach_labels(decision, labels().iloc[::-1])
    assert scored.outcome_next_delta_ore.iloc[0] == 1000
    values = module.exposure(scored.iloc[:3])
    assert values["unknown"] == 1 and values["falls"] == 1 and values["rises"] == 1
    assert values["one_pack_price_exposure_dkk"] == 5
    assert values["one_pack_downside_dkk"] == 5
    bad = labels()
    bad.loc[0, "origin_identity_token"] = "different"
    with pytest.raises(ResearchError, match="outcome_context_changed"):
        module.attach_labels(decision, bad)
    with pytest.raises(ResearchError, match="population_changed"):
        module.attach_labels(decision, labels().iloc[:-1])


def test_missing_freeze_blocks_execution(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        module.run(tmp_path)


def test_same_rule_matched_diagnostic_preserves_anchored_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = setup(tmp_path, monkeypatch)
    original = module.run(root)
    directory = root / "cito"
    frozen_json(directory / "code-preservation.json", {"preserved": pin(Path(module.__file__))})
    frozen_json(
        directory / "matched-clarification.json",
        {
            "initial_summary": pin(directory / "summary.json"),
            "new_same_rule_scores_inspected": False,
        },
    )
    completed = module.complete_matched(root, original["binding"])
    assert completed["results"] == original["results"]
    assert read(directory / "summary.json") == original
    assert set(completed["clean_matched"]["results"]) == set(module.SCENARIOS)
    for scenario in module.SCENARIOS:
        rows = pd.read_parquet(directory / f"clean-matched-{scenario}.parquet")
        assert rows.selected_matched_same_rule.sum() == 1
        assert rows.actual_selected_matched_same_rule.sum() == 1
    assert module.run(root) == completed
