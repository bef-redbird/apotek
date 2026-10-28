from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research import e6_publish as module
from bef_redbird.research.e4_common import frozen_json
from bef_redbird.research.group_features import pin

PERIODS = [
    ("spring", "2026-06-08", 10),
    ("spring", "2026-06-22", 10000),
    ("summer", "2026-09-14", 10),
    ("summer", "2026-10-12", 0),
]
EXPECTED = {(fold, day) for fold, day, _ in PERIODS}


def fixtures() -> tuple[pd.DataFrame, pd.DataFrame, dict[tuple[str, str, str, str, str], str]]:
    periods, queues, proofs = [], [], {}
    for mode in ("current", "hybrid"):
        for fold, day, observed in PERIODS:
            candidates = max(observed, 100)
            for history in ("3y", "6y", "full"):
                brier = 0.2 if history == "3y" else 0.199 if history == "6y" else 0.202
                periods.append(
                    {
                        "investigation": "D",
                        "task": "direction",
                        "route": "routed",
                        "calibration": "calibrated",
                        "stratum": "all",
                        "fold": fold,
                        "period_start": day,
                        "history": history,
                        "information": mode,
                        "candidates": candidates,
                        "observed": observed,
                        "unknown": candidates - observed,
                        "brier": brier if observed else np.nan,
                        "ece": 0.02 if observed else np.nan,
                    }
                )
                queues.append(
                    {
                        "fold": fold,
                        "period_start": day,
                        "history": history,
                        "information": mode,
                        "candidates": candidates,
                        "selected": 5,
                        "observed": 5 if observed else 0,
                        "unknown": 0 if observed else 5,
                        "downside_dkk": 5 if observed else 0,
                        "downside_dkk_per_observed": 1 if observed else None,
                    }
                )
                proofs[("D", "direction", fold, mode, history)] = fold + mode
            for investigation, arm in (
                ("C", "mae"),
                ("C", "huber"),
                ("D", "3y"),
                ("D", "6y"),
                ("D", "full"),
            ):
                history = "3y" if investigation == "C" else arm
                method = arm if investigation == "C" else "rmse"
                mae = 9.8 if arm in ("huber", "6y") else 10.2 if arm == "full" else 10
                rmse = (
                    19 if arm == "huber" else 20.1 if arm == "6y" else 19.5 if arm == "full" else 20
                )
                bias = 2 if arm == "huber" else 0.9 if arm == "6y" else 1.1 if arm == "full" else 1
                periods.append(
                    {
                        "investigation": investigation,
                        "task": "amount",
                        "support": "supported",
                        "stratum": "all",
                        "fold": fold,
                        "period_start": day,
                        "history": history,
                        "method": method,
                        "information": mode,
                        "candidates": candidates,
                        "observed": observed,
                        "unknown": candidates - observed,
                        "mae_dkk": mae if observed else np.nan,
                        "rmse_dkk": rmse if observed else np.nan,
                        "bias_dkk": bias if observed else np.nan,
                    }
                )
                proofs[(investigation, "amount", fold, mode, arm)] = fold + mode
    return pd.DataFrame(periods), pd.DataFrame(queues), proofs


def test_complete_common_population_passes_and_pending_origins_remain_required() -> None:
    periods, queues, proofs = fixtures()
    direction = module.direction_gates(periods, queues, EXPECTED, proofs)
    amounts = module.amount_gates(periods, EXPECTED, proofs)
    assert direction["history_arms"]["6y"]["passes"]
    assert not direction["history_arms"]["full"]["passes"]
    assert amounts["C_huber"]["passes_both_modes"]
    assert amounts["D_6y"]["passes_both_modes"]
    assert not amounts["D_full"]["passes_both_modes"]
    assert amounts["C_huber"]["checks"][0]["absolute_bias_not_worse"] is False
    assert amounts["C_huber"]["checks"][0]["absolute_bias_is_gate"] is False
    # Ensure exact JSON publication, including queue-coverage scalar booleans.
    json.dumps({"direction": direction, "amount": amounts}, allow_nan=False)


@pytest.mark.parametrize("omission", ["summer", "pending", "extra", "counts", "labels"])
def test_incomplete_or_changed_population_cannot_qualify(omission: str) -> None:
    periods, queues, proofs = fixtures()
    if omission == "summer":
        periods, queues = (
            periods.loc[periods.fold.ne("summer")],
            queues.loc[queues.fold.ne("summer")],
        )
    elif omission == "pending":
        periods, queues = (
            periods.loc[periods.period_start.ne("2026-10-12")],
            queues.loc[queues.period_start.ne("2026-10-12")],
        )
    elif omission == "extra":
        extra = periods.loc[periods.history.eq("6y") & periods.period_start.eq("2026-09-14")].copy()
        extra["period_start"] = "2026-09-15"
        periods = pd.concat([periods, extra], ignore_index=True)
    elif omission == "counts":
        mask = periods.history.eq("6y") & periods.period_start.eq("2026-09-14")
        periods.loc[mask, "observed"] -= 1
        periods.loc[mask, "unknown"] += 1
    else:
        proofs[("D", "direction", "summer", "current", "6y")] = "different typed outcomes"
        proofs[("D", "amount", "summer", "current", "6y")] = "different candidate keys"
    assert not module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"][
        "passes"
    ]
    assert not module.amount_gates(periods, EXPECTED, proofs)["D_6y"]["passes_both_modes"]


def test_no_compatibility_proof_or_frozen_calendar_fails_closed() -> None:
    periods, queues, proofs = fixtures()
    assert not module.direction_gates(periods, queues, EXPECTED)["history_arms"]["6y"]["passes"]
    assert not module.amount_gates(periods, proofs=proofs)["D_6y"]["passes_both_modes"]


def test_wholly_unscored_fold_cannot_establish_two_fold_performance() -> None:
    periods, queues, proofs = fixtures()
    mask = periods.fold.eq("summer")
    periods.loc[mask, "unknown"] = periods.loc[mask, "candidates"]
    periods.loc[mask, "observed"] = 0
    periods.loc[mask, ["brier", "ece", "mae_dkk", "rmse_dkk", "bias_dkk"]] = np.nan
    mask = queues.fold.eq("summer")
    queues.loc[mask, "observed"] = 0
    queues.loc[mask, "unknown"] = queues.loc[mask, "selected"]
    queues.loc[mask, "downside_dkk_per_observed"] = np.nan
    assert not module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"][
        "passes"
    ]
    assert not module.amount_gates(periods, EXPECTED, proofs)["D_6y"]["passes_both_modes"]


def test_queue_origin_counts_and_fold_regression_cannot_be_hidden() -> None:
    periods, queues, proofs = fixtures()
    queues["candidates"] += 1
    assert not module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"][
        "passes"
    ]
    _, queues, _ = fixtures()
    periods.loc[
        periods.task.eq("direction") & periods.history.eq("6y") & periods.fold.eq("spring"), "brier"
    ] = 0.19
    periods.loc[
        periods.task.eq("direction") & periods.history.eq("6y") & periods.fold.eq("summer"), "brier"
    ] = 0.202
    assert not module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"][
        "passes"
    ]


def test_direction_uses_equal_period_scores_and_pairs_queue_periods() -> None:
    periods, queues, proofs = fixtures()
    mask = periods.task.eq("direction") & periods.history.eq("6y")
    periods.loc[mask & periods.period_start.eq("2026-06-08"), "brier"] = 0.198
    periods.loc[mask & periods.period_start.eq("2026-06-22"), "brier"] = 0.2002
    periods.loc[mask & periods.period_start.eq("2026-09-14"), "brier"] = 0.2
    # The 10,000-case period worsens; equal-period gain still exceeds .0005.
    assert module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"]["passes"]
    changed = queues.copy()
    changed.loc[
        changed.history.eq("6y") & changed.period_start.eq("2026-09-14"), "period_start"
    ] = "2026-09-15"
    result = module.direction_gates(periods, changed, EXPECTED, proofs)
    assert not result["history_arms"]["6y"]["passes"]
    assert not result["history_arms"]["6y"]["checks"][0]["complete_paired_queue_periods"]


def test_downside_per_pack_gate_keeps_total_unknown_diagnostics_separate() -> None:
    periods, queues, proofs = fixtures()
    candidate = queues.history.eq("6y") & queues.observed.gt(0)
    queues.loc[candidate, "selected"] = 20
    queues.loc[candidate, "observed"] = 10
    queues.loc[candidate, "unknown"] = 10
    queues.loc[candidate, "downside_dkk"] = 10
    # Larger total downside/unknown selected is reported; the frozen gate is per pack.
    result = module.direction_gates(periods, queues, EXPECTED, proofs)
    assert result["history_arms"]["6y"]["passes"]
    diagnostic = result["history_arms"]["6y"]["checks"][0]["queue_diagnostics"]
    assert (
        diagnostic["candidate"]["total_downside_dkk"]
        > diagnostic["reference"]["total_downside_dkk"]
    )
    assert (
        diagnostic["candidate"]["pooled_unknown_rate"]
        > diagnostic["reference"]["pooled_unknown_rate"]
    )
    queues.loc[candidate, "downside_dkk_per_observed"] = 1.051
    assert not module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"][
        "passes"
    ]


def test_asymmetric_missing_queue_support_and_calibration_deterioration_fail() -> None:
    periods, queues, proofs = fixtures()
    mask = queues.history.eq("6y") & queues.period_start.eq("2026-09-14")
    queues.loc[mask, ["observed", "unknown", "downside_dkk_per_observed"]] = [0, 5, np.nan]
    assert not module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"][
        "passes"
    ]
    _, queues, _ = fixtures()
    periods.loc[periods.history.eq("6y") & periods.task.eq("direction"), "ece"] = 0.02101
    assert not module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"][
        "passes"
    ]


def test_zero_reference_amount_error_never_creates_nonfinite_ratios() -> None:
    periods, _, proofs = fixtures()
    periods.loc[periods.task.eq("amount"), ["mae_dkk", "rmse_dkk"]] = 0
    result = module.amount_gates(periods, EXPECTED, proofs)
    assert result["C_huber"]["checks"][0]["mae_ratio"] == 1
    assert not result["C_huber"]["passes_both_modes"]
    periods.loc[periods.method.eq("huber"), "mae_dkk"] = 1
    result = module.amount_gates(periods, EXPECTED, proofs)
    assert result["C_huber"]["checks"][0]["mae_ratio"] is None
    json.dumps(result, allow_nan=False)


def test_scored_missing_metrics_cannot_be_silently_omitted_from_period_means() -> None:
    periods, queues, proofs = fixtures()
    mask = periods.history.eq("6y") & periods.period_start.eq("2026-09-14")
    periods.loc[mask & periods.task.eq("direction"), "ece"] = np.nan
    periods.loc[mask & periods.task.eq("amount"), "mae_dkk"] = np.nan
    assert not module.direction_gates(periods, queues, EXPECTED, proofs)["history_arms"]["6y"][
        "passes"
    ]
    assert not module.amount_gates(periods, EXPECTED, proofs)["D_6y"]["passes_both_modes"]


def test_cohort_proof_detects_label_changes_but_not_row_order(tmp_path: Path) -> None:
    folder = tmp_path / "evaluations/D-spring"
    folder.mkdir(parents=True)
    frames = []
    for history in ("3y", "6y"):
        frames.append(
            pd.DataFrame(
                {
                    "row_key": ["a", "b"],
                    "package_number": ["000001", "000002"],
                    "period_index": [1, 1],
                    "period_start": pd.to_datetime(["2026-06-08"] * 2),
                    "identity_episode_id": ["one", "two"],
                    "next_status": ["observed", "pending"],
                    "next_direction": [1, None],
                    "next_delta_ore": [10, None],
                    "next_maturity_date": pd.to_datetime(["2026-06-22", "2026-06-22"]),
                    "history": history,
                    "information": "current",
                    "method": "rmse",
                }
            ).iloc[::-1]
            if history == "6y"
            else pd.DataFrame(
                {
                    "row_key": ["a", "b"],
                    "package_number": ["000001", "000002"],
                    "period_index": [1, 1],
                    "period_start": pd.to_datetime(["2026-06-08"] * 2),
                    "identity_episode_id": ["one", "two"],
                    "next_status": ["observed", "pending"],
                    "next_direction": [1, None],
                    "next_delta_ore": [10, None],
                    "next_maturity_date": pd.to_datetime(["2026-06-22", "2026-06-22"]),
                    "history": history,
                    "information": "current",
                    "method": "rmse",
                }
            )
        )
    path = folder / "predictions.parquet"
    source = pd.concat(frames, ignore_index=True)
    source.to_parquet(path, index=False)
    metadata: dict[str, Any] = {
        "summaries": [{"task": "amount", "investigation": "D", "fold": "spring"}],
        "files": {"predictions.parquet": pin(path)},
    }
    (folder / "result.json").write_text(json.dumps(metadata))
    proof = module.cohort_proofs(tmp_path)
    assert (
        proof[("D", "amount", "spring", "current", "3y")]
        == proof[("D", "amount", "spring", "current", "6y")]
    )
    source.loc[source.history.eq("6y") & source.row_key.eq("a"), "next_delta_ore"] = 11
    source.to_parquet(path, index=False)
    metadata["files"]["predictions.parquet"] = pin(path)
    (folder / "result.json").write_text(json.dumps(metadata))
    proof = module.cohort_proofs(tmp_path)
    assert (
        proof[("D", "amount", "spring", "current", "3y")]
        != proof[("D", "amount", "spring", "current", "6y")]
    )


def test_resource_counts_include_declared_pending_and_reused_temperatures(tmp_path: Path) -> None:
    folder = tmp_path / "evaluations/direction-spring"
    folder.mkdir(parents=True)
    frozen_json(folder / "calibration-checkpoint.json", {"states": {"one": {}, "two": {}}})
    frozen_json(
        folder / "result.json",
        {"calibration": {"three": {"reused": True}, "four": {"reused": True}, "one": {}}},
    )
    protocol = {
        "matrix": [{"id": "first", "history": "full"}, {"id": "second", "history": "full"}],
        "resource_limits": {"threads": 6, "aggregate_fit_seconds": 57600, "full_fit_seconds": 7200},
    }
    ledger: dict[str, Any] = {
        "trials": [
            {"spec": {"id": "first"}, "status": "completed"},
            {"spec": {"id": "second"}, "status": "running"},
        ],
        "completed": 1,
        "failed": 0,
        "skipped": 0,
        "attempts": 2,
        "fit_seconds": 3600,
        "elapsed_seconds": 4000,
    }
    result = module.resource_summary(tmp_path, protocol, ledger, {"reused_fits": {"old": {}}})
    assert result["full_history_declared"] == 2 and result["full_history_pending"] == 1
    assert result["running_trials"] == 1 and result["full_history_successful"] == 1
    assert result["new_scalar_calibration_fits"] == result["reused_scalar_calibrations"] == 2
    assert result["scalar_calibrations"] == 4

    ledger["trials"].append(
        {
            "spec": {"id": "interrupted"},
            "status": "failed",
            "fit_seconds": 1200,
            "fit_seconds_is_conservative_upper_bound": True,
            "resource": {"peak_rss_unavailable": True},
        }
    )
    ledger["trials"][0]["fit_seconds"] = 3600
    ledger.update(failed=1, attempts=3, fit_seconds=4800)
    result = module.resource_summary(tmp_path, protocol, ledger, {"reused_fits": {}})
    assert result["successful_measured_fit_hours"] == 1
    assert result["interrupted_fit_budget_charge_upper_bound_hours"] == pytest.approx(1 / 3)
    assert result["fit_hours"] == pytest.approx(4 / 3)
    assert result["trials_without_final_peak_memory_measurement"] == 1


def test_expected_calendar_includes_wholly_pending_last_origin(tmp_path: Path) -> None:
    calendar = tmp_path / "calendar.json"
    frozen_json(
        calendar,
        {
            "periods": [
                {"start": day} for day in ("2026-05-25", "2026-06-08", "2026-10-12", "2026-10-26")
            ]
        },
    )
    dataset = tmp_path / "dataset.json"
    frozen_json(
        dataset, {"files": [{**pin(calendar), "path": "calendar.json", "role": "calendar"}]}
    )
    protocol = {
        "pins": {"dataset": pin(dataset)},
        "folds": [
            {"name": "spring", "start": "2026-06-01", "end": "2026-09-01"},
            {"name": "summer", "start": "2026-09-01", "end": "2026-10-26"},
        ],
    }
    assert module.expected_periods(tmp_path, protocol) == {
        ("spring", "2026-06-08"),
        ("summer", "2026-10-12"),
    }


def test_short_answers_follow_measured_counts_and_gate_results() -> None:
    periods, _, proofs = fixtures()
    amount = module.amount_gates(periods, EXPECTED, proofs)

    def queue(net: float, downside: float, added: int, removed: int) -> dict[str, Any]:
        return {
            "selected": {"one_pack_price_exposure_dkk": net, "one_pack_downside_dkk": downside},
            "added": {"cases": added},
            "removed": {"cases": removed},
        }

    original = {
        "groups": {
            "all_eligible": {
                "queues": {"natural": queue(100, 10, 0, 0), "matched": queue(100, 10, 0, 0)}
            }
        }
    }
    corrected = {
        "changed_predictions": 1,
        "groups": {
            "all_eligible": {
                "queues": {"natural": queue(90, 10, 2, 1), "matched": queue(80, 10, 3, 3)}
            }
        },
    }
    cito = {
        "correction": {"eligible_wrong_codes": 2},
        "results": {"actual": original, "corrected_wrong_only": corrected},
        "clean_matched": {
            "results": {
                "actual": original,
                "corrected_wrong_only": corrected,
                "no_report_current": {
                    "groups": {"all_eligible": {"queues": {"matched": queue(50, 15, 0, 0)}}}
                },
            }
        },
    }
    finance = {
        "decision_gates": [{"policy": "amount_dkk", "research_replacement_gate_passed": False}]
    }
    answer = module.short_answers(finance, cito, amount)
    assert "Correcting 2 eligible wrong codes changed 1 predictions" in answer["B"]["answer"]
    assert "1 removed, 2 added" in answer["B"]["answer"]
    assert "DKK -10.00" in answer["B"]["limitations"]
    assert "100.00 observed one-pack movement versus 50.00" in answer["B"]["practical_meaning"]
    assert "Downside was DKK 10.00 versus 15.00" in answer["B"]["practical_meaning"]
    assert answer["C"]["limitations"].startswith("Passes")
    finance["decision_gates"][0]["research_replacement_gate_passed"] = True
    periods.loc[periods.method.eq("huber") & periods.information.eq("current"), "mae_dkk"] = 11
    answer = module.short_answers(finance, cito, module.amount_gates(periods, EXPECTED, proofs))
    assert answer["A"]["status"] == "improved"
    assert "Current-only: typical absolute error worsened" in answer["C"]["answer"]
    assert answer["C"]["limitations"].startswith("Does not pass")


def test_history_answer_separates_weighting_from_additional_history() -> None:
    rows = []
    for mode in ("current", "hybrid"):
        for history, brier in (("3y", 0.20), ("full", 0.23), ("weighted", 0.21)):
            for day in ("2026-09-14", "2026-09-28"):
                rows.append(
                    {
                        "task": "direction",
                        "fold": "summer",
                        "stratum": "all",
                        "route": "routed",
                        "calibration": "calibrated",
                        "observed": 10,
                        "information": mode,
                        "history": history,
                        "brier": brier,
                        "ece": 0.012 if history == "weighted" else 0.01,
                        "period_start": day,
                    }
                )
    periods = pd.DataFrame(rows)
    resources = {
        "full_history_successful": 12,
        "full_history_declared": 12,
        "full_history_pending": 0,
    }
    answer = module.history_answer(periods, {"reference_retained": True}, {}, resources)
    assert "lowered probability error in both modes versus all usable history" in answer
    assert "raised probability error in both modes versus three recent years" in answer
    assert "only 2 scored summer periods" in answer
    assert "Calibration error changes +20.00% current-only and +20.00% conditional" in answer
    incomplete = periods.drop(periods.index[-1])
    answer = module.history_answer(incomplete, {"reference_retained": True}, {}, resources)
    assert "weighting comparison is incomplete" in answer


def test_weighting_display_has_references_on_the_same_summer_periods() -> None:
    rows = []
    for mode in ("current", "hybrid"):
        for history in ("3y", "full", "weighted"):
            days = ["2026-09-14", "2026-09-28"]
            if history != "weighted":
                days.append("2026-06-08")
            for day in days:
                rows.append(
                    {
                        "investigation": "D",
                        "task": "direction",
                        "fold": "spring" if day == "2026-06-08" else "summer",
                        "stratum": "all",
                        "route": "routed",
                        "calibration": "calibrated",
                        "history": history,
                        "information": mode,
                        "period_start": day,
                        "observed": 10,
                        "unknown": 0,
                        "brier": 0.9 if day == "2026-06-08" else 0.2,
                        "ece": 0.01,
                        "logloss": 0.5,
                    }
                )
    display = module.readable_model_rows(pd.DataFrame(rows), "D", "direction")
    summer = [row for row in display if "weighting comparison" in row["Evaluation"]]
    assert len(summer) == 6
    assert all(row["Scored tariff periods"] == 2 for row in summer)
    assert all(row["Observed cases"] == 20 for row in summer)
    assert all(row["Brier probability error ↓"] == pytest.approx(0.2) for row in summer)
    main = [row for row in display if row["Evaluation"] == "June–October 2026"]
    assert len(main) == 4 and all(row["Scored tariff periods"] == 3 for row in main)


def test_earlier_diagnostic_does_not_hide_bias_or_large_error_regressions() -> None:
    rows = []
    for mode in ("current", "hybrid"):
        for history in ("3y", "full"):
            rows.append(
                {
                    "investigation": "D",
                    "task": "amount",
                    "fold": "earlier",
                    "stratum": "all",
                    "support": "supported",
                    "observed": 10,
                    "information": mode,
                    "history": history,
                    "period_start": "2025-06-09",
                    "mae_dkk": 10 if history == "3y" else 9,
                    "rmse_dkk": 20 if history == "3y" else 21 if mode == "current" else 19,
                    "bias_dkk": 0.1 if history == "3y" else -0.2,
                }
            )
    part = pd.DataFrame(rows)
    text = module.earlier_amount_caveat(part)
    assert "current-only root mean squared error" in text
    assert "current-only absolute bias" in text and "conditional absolute bias" in text
    assert "despite lower average absolute error in both modes" in text
    assert "incomplete" in module.earlier_amount_caveat(part.iloc[:-1])
