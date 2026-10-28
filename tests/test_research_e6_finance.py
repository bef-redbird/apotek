from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e6_finance import (
    current_only_frame,
    decision_gates,
    diagnostic_tables,
    evaluate_fold,
    make_queues,
    summaries,
)


def inputs(n: int = 20) -> tuple[pd.DataFrame, ...]:
    predictions = pd.DataFrame(
        {
            "row_key": [f"r{k}" for k in range(n)],
            "package_number": [f"{k:06d}" for k in range(n)],
            "period_start": pd.Timestamp("2026-06-01"),
            "aip_ore": 10000,
            "eligible": True,
            "tuned_current_p0": 0.05,
            "tuned_current_p1": 0.35,
            "tuned_current_p2": 0.6,
            # Poison values must never enter the current-only adapter.
            "tuned_hybrid_p0": 0.9,
            "tuned_hybrid_p2": 0.01,
            "reference_p_rise": 0.99,
            "rmse_price_dkk": 999999.0,
            "quantile_lower_price_dkk": 999999.0,
            "advance_abc": "C",
            "origin_group": "g",
            "pack_size_token": "20",
            "pack_unit": "TAB",
            "peer_count": n - 1,
            "peer_feature_state": "complete_equal_size_peers",
        }
    )
    amount = predictions[["row_key"]].assign(next_price_dkk=np.arange(n) + 101.0)
    quantile = predictions[["row_key"]].assign(q05_price_dkk=99.0)
    policy = predictions[["row_key"]].assign(behavior50_excluded=False)
    e4 = predictions[["row_key"]].assign(
        perfect_current_p0=0.05, perfect_current_p1=0.35, perfect_current_p2=0.6
    )
    outcomes = predictions[["row_key", "period_start", "package_number"]].assign(
        next_status="observed", next_aip_ore=11000
    )
    return predictions, amount, quantile, policy, e4, outcomes


def test_adapter_uses_explicit_current_predictions_and_preserves_population() -> None:
    predictions, amount, quantile, policy, _, _ = inputs()
    policy.loc[19, "behavior50_excluded"] = True
    frame = current_only_frame(predictions, amount.sample(frac=1), quantile, policy)
    assert len(frame) == 20 and frame.origin_eligible.all()
    assert frame.eligible.sum() == 19
    assert frame.p_rise.eq(0.6).all() and frame.p_fall.eq(0.05).all()
    assert frame.forecast_delta_dkk.tolist() == list(np.arange(20) + 1.0)
    assert frame.lower_delta_dkk.eq(-1).all()
    assert not any("hybrid" in c or c.startswith(("next_", "advance", "reference_")) for c in frame)
    frame2 = current_only_frame(
        predictions.assign(advance_abc="A", rmse_price_dkk=-1), amount, quantile, policy
    )
    pd.testing.assert_frame_equal(frame, frame2)
    with pytest.raises(ResearchError, match="missing_saved_prediction_keys"):
        current_only_frame(predictions, amount.iloc[1:], quantile, policy)
    with pytest.raises(ResearchError, match="invalid_current_only_probabilities"):
        current_only_frame(predictions.assign(tuned_current_p0=0.9), amount, quantile, policy)


def test_shared_mask_original_reference_and_matched_expansion_are_explicit() -> None:
    predictions, amount, quantile, policy, e4, _ = inputs()
    policy.loc[19, "behavior50_excluded"] = True
    predictions.loc[0, ["tuned_current_p0", "tuned_current_p1", "tuned_current_p2"]] = [
        0.15,
        0.05,
        0.8,
    ]
    amount["next_price_dkk"] = 90.0
    amount.loc[0, "next_price_dkk"] = 999.0
    frame = current_only_frame(predictions, amount, quantile, policy)
    queues = make_queues(frame, e4)
    main = queues.loc[queues.policy.ne("original_e4_current")]
    assert not main.loc[main.row_key.eq("r19"), "policy_eligible"].any()
    assert queues.loc[queues.policy.eq("original_e4_current"), "policy_eligible"].all()
    assert (
        queues.loc[
            queues.policy.eq("probability_reference") & queues.workload.eq("natural"), "selected"
        ].sum()
        == 2
    )
    assert not queues.loc[
        queues.policy.eq("amount_dkk") & queues.workload.eq("natural"), "selected"
    ].any()
    monetary = queues.loc[
        queues.policy.eq("amount_dkk") & queues.workload.eq("matched") & queues.selected
    ]
    assert (
        len(monetary) == 2 and monetary.matched_expansion.all() and monetary.diagnostic_only.all()
    )
    assert "r0" in monetary.row_key.tolist()  # diagnostic may cross the natural fall gate
    shuffled = make_queues(frame.sample(frac=1, random_state=2), e4.sample(frac=1))
    columns = ["row_key", "policy", "workload", "rank", "selected"]
    order = ["row_key", "policy", "workload"]
    pd.testing.assert_frame_equal(
        queues[columns].sort_values(order).reset_index(drop=True),
        shuffled[columns].sort_values(order).reset_index(drop=True),
    )


def test_outcomes_do_not_change_queues_or_allocations_and_unknowns_not_replaced() -> None:
    predictions, amount, quantile, policy, e4, outcomes = inputs()
    frame = current_only_frame(predictions, amount, quantile, policy)
    observed = evaluate_fold(frame, e4, outcomes, "spring")
    unknown = evaluate_fold(
        frame, e4, outcomes.assign(next_status="identity_gap", next_aip_ore=np.nan), "spring"
    )
    pd.testing.assert_frame_equal(observed["queues"], unknown["queues"])
    pd.testing.assert_frame_equal(observed["allocations"], unknown["allocations"])
    assert unknown["periods"].unknown_selected.eq(unknown["periods"].selected).all()
    assert unknown["periods"].observed_selected.eq(0).all()
    assert unknown["unknown_reasons"].unknown_reason.eq("identity_gap").all()
    assert all(
        row["equal_period_net_dkk_per_observed_pack"] is None
        for row in summaries(unknown["periods"])
    )
    with pytest.raises(ResearchError, match="noncurrent_decision_fields"):
        evaluate_fold(frame.assign(advance_abc="C"), e4, outcomes, "spring")


def test_independent_one_pack_cash_price_bands_and_group_summaries() -> None:
    predictions, amount, quantile, policy, e4, outcomes = inputs(20)
    # Amount ranks r19 then r18. Their observed movements are +20 and -5 DKK.
    outcomes.loc[19, "next_aip_ore"] = 12000
    outcomes.loc[18, "next_aip_ore"] = 9500
    frame = current_only_frame(predictions, amount, quantile, policy)
    output = evaluate_fold(frame, e4, outcomes, "spring")
    row = (
        output["periods"]
        .loc[
            lambda f: (
                f.policy.eq("amount_dkk") & f.workload.eq("natural") & f.allocation.eq("per_pack")
            )
        ]
        .iloc[0]
    )
    assert row.observed_price_exposure_ore == 1500
    assert row.observed_positive_exposure_ore == 2000
    assert row.observed_negative_exposure_ore == -500
    assert row.observed_rises == row.observed_falls == 1
    allocation = output["allocations"].loc[
        lambda f: (
            f.policy.eq("amount_dkk") & f.workload.eq("natural") & f.allocation.eq("fixed_capital")
        )
    ]
    assert allocation.quantity.tolist() == [100, 100]
    assert allocation.allocated_ore.sum() == 2_000_000
    assert allocation.unallocated_capital_ore.tolist() == [8_000_000] * 2
    grouped = next(
        r
        for r in output["grouped"]["price_band"]
        if r["policy"] == "amount_dkk"
        and r["allocation"] == "per_pack"
        and r["workload"] == "natural"
    )
    assert grouped["price_band"] == "100_to1000_dkk" and grouped["net_exposure_ore"] == 1500
    summary = next(
        r
        for r in summaries(output["periods"])
        if r["fold"] == "spring"
        and r["policy"] == "amount_dkk"
        and r["workload"] == "natural"
        and r["allocation"] == "per_pack"
    )
    assert summary["pooled_net_dkk_per_observed_pack"] == 7.5
    assert summary["pooled_downside_dkk_per_observed_pack"] == 2.5


def test_equal_period_means_do_not_become_pooled_pack_means() -> None:
    predictions, amount, quantile, policy, e4, outcomes = inputs()
    frame = current_only_frame(predictions, amount, quantile, policy)
    first = evaluate_fold(frame, e4, outcomes, "spring")["periods"]
    second = first.copy().assign(period_start=pd.Timestamp("2026-06-15"))
    second["observed_funded_quantity"] = 10
    second["observed_price_exposure_ore"] = -1000
    joined = pd.concat([first, second], ignore_index=True)
    result = next(
        r
        for r in summaries(joined)
        if r["fold"] == "spring"
        and r["policy"] == "amount_dkk"
        and r["workload"] == "natural"
        and r["allocation"] == "per_pack"
    )
    # Period 1 +20DKK/2 packs; period2 -10DKK/10 packs.
    assert result["equal_period_net_dkk_per_observed_pack"] == 4.5
    assert result["pooled_net_dkk_per_observed_pack"] == pytest.approx(10 / 12)
    unknown = joined.assign(
        observed_selected=0, observed_funded_quantity=0, unknown_selected=joined.selected
    )
    assert not any(
        row["research_replacement_gate_passed"] for row in decision_gates(summaries(unknown))
    )


def test_empty_queues_and_fractional_label_money_are_explicit() -> None:
    predictions, amount, quantile, policy, e4, outcomes = inputs()
    predictions[["tuned_current_p0", "tuned_current_p1", "tuned_current_p2"]] = [0.2, 0.6, 0.2]
    e4[["perfect_current_p0", "perfect_current_p1", "perfect_current_p2"]] = [0.2, 0.6, 0.2]
    frame = current_only_frame(predictions, amount, quantile, policy)
    result = evaluate_fold(frame, e4, outcomes, "spring")
    assert len(result["periods"]) == 14 and result["periods"].selected.eq(0).all()
    assert result["allocations"].empty
    predictions, amount, quantile, policy, e4, outcomes = inputs()
    frame = current_only_frame(predictions, amount, quantile, policy)
    with pytest.raises(ResearchError, match="exact_outcome_ore"):
        evaluate_fold(frame, e4, outcomes.assign(next_aip_ore=11000.5), "spring")


def test_block_sensitivity_and_supported_group_concentration() -> None:
    predictions, amount, quantile, policy, e4, outcomes = inputs()
    frame = current_only_frame(predictions, amount, quantile, policy)
    output = evaluate_fold(frame, e4, outcomes, "spring")
    periods = pd.concat(
        [
            output["periods"].assign(
                period_start=pd.Timestamp("2026-06-01") + pd.Timedelta(days=14 * k)
            )
            for k in range(10)
        ],
        ignore_index=True,
    )
    candidate = periods.policy.isin(["amount_dkk", "downside_aware"])
    # Every paired period has +3 DKK per observed pack for the monetary policies.
    periods.loc[candidate, "observed_price_exposure_ore"] += (
        periods.loc[candidate, "observed_funded_quantity"] * 300
    )
    result = diagnostic_tables(periods, output["allocations"])
    changes = [
        r for r in result["paired_period_bootstrap"] if r["metric"] == "net_dkk_per_observed_pack"
    ]
    assert len(changes) == 8
    assert all(r["mean_difference"] == r["lower95"] == r["upper95"] == 3.0 for r in changes)
    assert all(r["paired_periods"] == 10 for r in changes)
    groups = result["group_concentration"]
    assert all(r["supported_groups"] == 1 for r in groups)
    assert all(r["max_supported_group_capital_share"] == 1 for r in groups)
    assert (
        diagnostic_tables(periods, output["allocations"])["paired_period_bootstrap"]
        == result["paired_period_bootstrap"]
    )
