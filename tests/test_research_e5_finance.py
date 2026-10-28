from __future__ import annotations

from fractions import Fraction

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e3_methods import select_queue
from bef_redbird.research.e4_scenarios import simulate
from bef_redbird.research.e5_finance import (
    CAPITAL_ORE,
    allocate,
    compare_group_scenarios,
    price_exposure,
    queue_policies,
    queue_summary,
    simulate_group,
)


def predictions(n: int = 30) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "package_id": [f"{i:06d}" for i in range(n)],
            "row_key": [f"row-{i}" for i in range(n)],
            "period_start": pd.to_datetime(["2025-01-06"] * n),
            "aip_ore": [10_000] * n,
            "eligible": [True] * n,
            "p_rise": [0.6] * n,
            "p_fall": [0.05] * n,
            "forecast_delta_dkk": np.arange(n, dtype=float) + 1,
            "median_delta_dkk": [999.0] * n,
            "lower_delta_dkk": [-1.0] * n,
            "upper_delta_dkk": [100.0] * n,
            "origin_group": ["g"] * n,
            "pack_size_token": ["20"] * n,
            "pack_unit": ["TAB"] * n,
            "peer_count": [n - 1] * n,
            "peer_feature_state": ["complete_equal_size_peers"] * n,
        }
    )


def test_reference_exact_e4_threshold_and_deterministic_ties() -> None:
    frame = predictions().assign(p_rise=[0.9, 0.9, 0.2] * 10)
    queues = queue_policies(frame)
    reference = queues.loc[
        (queues.policy == "probability_reference") & (queues.workload == "natural")
    ]
    e4 = select_queue(frame.assign(p2=frame.p_rise, p0=frame.p_fall), "threshold", (0.35, 0.10))
    assert reference.selected.tolist() == e4.selected.tolist()
    shuffled = queue_policies(frame.sample(frac=1, random_state=5))
    pd.testing.assert_frame_equal(
        queues.sort_values(["policy", "workload", "row_key"]).reset_index(drop=True),
        shuffled.sort_values(["policy", "workload", "row_key"]).reset_index(drop=True),
    )
    with pytest.raises(ResearchError, match="outcomes_in_decision_inputs"):
        queue_policies(frame.assign(next_aip_ore=99999))


def test_matched_reference_retains_e4_threshold_when_highest_rise_fails_fall_gate() -> None:
    first = predictions(20).assign(p_rise=0.2, forecast_delta_dkk=-3.0)
    first.loc[0, ["p_rise", "p_fall"]] = [0.8, 0.15]
    first.loc[[1, 2], "p_rise"] = [0.7, 0.65]
    second = predictions(10).assign(
        period_start=pd.Timestamp("2025-01-20"), p_rise=0.2, forecast_delta_dkk=-3.0
    )
    second["row_key"] = "second-" + second.row_key
    second.loc[0, ["p_rise", "p_fall"]] = [0.8, 0.15]
    second.loc[1, "p_rise"] = 0.6
    frame = pd.concat([first, second], ignore_index=True)
    queues = queue_policies(frame)
    control = queues.loc[queues.policy.eq("probability_reference")]
    natural = control.loc[control.workload.eq("natural")]
    matched = control.loc[control.workload.eq("matched")]
    expected = select_queue(
        frame.assign(p2=frame.p_rise, p0=frame.p_fall), "threshold", (0.35, 0.10)
    )
    assert natural.selected.tolist() == matched.selected.tolist() == expected.selected.tolist()
    assert not natural.loc[natural.p_rise.eq(0.8), "selected"].any()
    counts = {pd.Timestamp("2025-01-06"): 2, pd.Timestamp("2025-01-20"): 1}
    assert natural.groupby("period_start").selected.sum().to_dict() == counts
    assert matched.groupby("period_start").selected.sum().to_dict() == counts
    monetary = queues.loc[queues.policy.eq("amount_dkk")]
    assert not monetary.loc[monetary.workload.eq("natural"), "selected"].any()
    forced = monetary.loc[monetary.workload.eq("matched") & monetary.selected]
    assert forced.groupby("period_start").selected.sum().to_dict() == counts
    assert forced.score.lt(0).all()


def test_amount_downside_missing_and_forced_workload_are_distinct() -> None:
    frame = predictions(20)
    frame["forecast_delta_dkk"] = [-3.0] * 20
    frame.loc[0, "forecast_delta_dkk"] = 10.0
    frame.loc[0, "lower_delta_dkk"] = -20.0
    queues = queue_policies(frame)
    summary = queue_summary(queues).set_index(["policy", "workload"])
    assert summary.loc[("probability_reference", "natural"), "selected"] == 2
    assert summary.loc[("amount_dkk", "natural"), "selected"] == 1
    assert summary.loc[("downside_aware", "natural"), "selected"] == 0
    assert summary.loc[("downside_aware", "matched"), "selected"] == 2
    frame.loc[1:, "forecast_delta_dkk"] = np.nan
    summary = queue_summary(queue_policies(frame)).set_index(["policy", "workload"])
    assert summary.loc[("amount_dkk", "matched"), "matched_shortfall"] == 1


def test_original_population_cap_and_persistence_is_gate_only() -> None:
    frame = predictions(30)
    frame.loc[10:, "eligible"] = False
    frame["persistence_probability"] = 0.9
    frame.loc[29, "persistence_probability"] = 0.1
    queues = queue_policies(frame, include_persistence=True)
    natural = queues.loc[queues.workload.eq("natural")]
    assert natural.loc[natural.policy.eq("probability_reference"), "selected"].sum() == 3
    persistence = natural.loc[natural.policy.eq("persistence_aware")]
    downside = natural.loc[natural.policy.eq("downside_aware")]
    assert persistence.score.iloc[:10].tolist() == downside.score.iloc[:10].tolist()
    assert np.isnan(persistence.score.iloc[-1])


def test_capital_integer_package_group_concentration_and_cash() -> None:
    frame = predictions(100).assign(aip_ore=30000)
    queues = queue_policies(frame)
    allocation = allocate(queues)
    for _, part in allocation.groupby(["policy", "workload"]):
        # One supported group; floor(10000/300)=33 each, <=25000 group.
        assert part.allocated_ore.max() <= 1_000_000
        assert part.allocated_ore.sum() <= 2_500_000
        assert (part.quantity % 1 == 0).all()
        assert part.unallocated_capital_ore.nunique() == 1
        assert part.allocated_ore.sum() + part.unallocated_capital_ore.iloc[0] == CAPITAL_ORE
        assert part.quantity.sum() == 83
    expensive = predictions(1).assign(aip_ore=2_000_000, peer_count=0)
    result = allocate(queue_policies(expensive))
    assert result.quantity.eq(0).all() and result.unallocated_capital_ore.eq(CAPITAL_ORE).all()


def test_unknown_selected_not_replaced_and_exposure_is_not_median_times_probability() -> None:
    frame = predictions(10)
    queues = queue_policies(frame)
    allocated = allocate(queues, "four_pack")
    labels = frame[["period_start", "package_id"]].assign(
        next_status="pending", next_aip_ore=np.nan
    )
    result = price_exposure(allocated, labels, queues=queues, allocation="four_pack")
    assert result.unknown_selected.eq(1).all()
    assert result.unknown_quantity.eq(4).all()
    assert result.observed_price_exposure_ore.eq(0).all()
    for row in result.itertuples():
        selected = allocated.loc[
            allocated.policy.eq(row.policy) & allocated.workload.eq(row.workload)
        ]
        assert (
            row.forecast_price_exposure_dkk
            == (selected.quantity * selected.forecast_delta_dkk).sum()
        )
    none = queue_policies(frame.assign(p_rise=0.1))
    empty = price_exposure(allocate(none), labels, queues=none, allocation="fixed_capital")
    assert len(empty) == 6 and empty.selected.eq(0).all()
    assert empty.unallocated_capital_ore.eq(CAPITAL_ORE).all()


@pytest.mark.parametrize("variant", ["normal", "late", "expiry", "returns"])
@pytest.mark.parametrize("compensation", ["none", "half_cap2"])
def test_single_extra_reproduces_e4_exact_accounting(variant: str, compensation: str) -> None:
    prices = [{"a": 10000, "b": 11000}, {"a": 15000, "b": 9000}, {"a": 8000, "b": 12000}]
    expected, events = simulate(
        prices,
        demand=4,
        extra_limit=4,
        compensation=compensation,
        extra_package="a",
        variant=variant,
    )
    actual, actual_events = simulate_group(
        prices, {"a": 4}, capacity_extra=4, demand=4, compensation=compensation, variant=variant
    )
    assert actual == expected
    assert actual_events == events


def test_multiple_packages_share_demand_and_independently_expected_cash_basis() -> None:
    prices = [{"a": 10000, "b": 20000}, {"a": 15000, "b": 30000}, {"a": 15000, "b": 30000}]
    result, events = simulate_group(prices, {"a": 3, "b": 3}, capacity_extra=6, demand=4)
    # 2 initial a +3 a+3 b exactly satisfy 8 shared group demands, no replenishment.
    assert result["sold"] == 8 and result["remaining"] == 0 and result["unmet"] == 0
    assert result["purchases_dkk"] == 900 and result["initial_book_dkk"] == 200
    assert result["sold_book_cost_dkk"] == 1100
    assert sum(e["demand"] for e in events if e["event"] == "group_demand") == 8
    # Independent book/cash equation; terminal valuation is zero, capital stays separate.
    exact = result["exact_ore"]
    assert Fraction(exact["economic_cost_proxy"]) == 110000 + Fraction(exact["capital_cost"])
    late, late_events = simulate_group(
        prices, {"a": 3, "b": 3}, capacity_extra=6, demand=4, variant="late"
    )
    assert late["purchases_dkk"] == 1350
    assert {e["unit_acquisition_ore"] for e in late_events if e["event"] == "receipt"} == {
        15000,
        30000,
    }


def test_group_unknown_population_and_common_origin_capacity() -> None:
    frame = predictions(20)
    queues = queue_policies(frame)
    allocated = pd.concat([allocate(queues, "four_pack"), allocate(queues)], ignore_index=True)
    labels = frame[["period_start", "package_id"]].assign(
        next_status="observed", next_aip_ore=15000, second_status="observed", second_aip_ore=12000
    )
    result, unknown = compare_group_scenarios(frame, allocated, labels)
    assert unknown.empty and len(result) == 6 * 6 * 2
    assert result.capacity_extra.nunique() == 1
    assert result.incremental_unmet.eq(0).all()
    assert (
        result.groupby("name")
        .ordinary_exact_ore.apply(lambda s: all(x == s.iloc[0] for x in s))
        .all()
    )
    labels.loc[0, "second_status"] = "identity_discontinuity"
    unavailable, unknown = compare_group_scenarios(frame, allocated, labels)
    assert unavailable.empty and len(unknown) == 1 and unknown.members.iloc[0] == 20
    assert allocated.selected.all()  # no queue replacements after observing missing trajectory


def test_partial_group_support_is_unscored_and_fractional_money_rejected() -> None:
    frame = predictions(20).iloc[:10]
    queues = queue_policies(frame)
    assert queues.finance_group_supported.all()
    assert not queues.finance_scenario_supported.any()
    with pytest.raises(ResearchError, match="exact_positive_origin_ore"):
        queue_policies(frame.assign(aip_ore=100.5))
    labels = frame[["period_start", "package_id"]].assign(
        next_status="observed", next_aip_ore=100.5
    )
    with pytest.raises(ResearchError, match="exact_outcome_ore"):
        price_exposure(allocate(queues), labels)


def test_none_forecasts_preserve_reference_and_missing_price_excluded_origin() -> None:
    frame = predictions(10)
    frame["forecast_delta_dkk"] = None
    frame["lower_delta_dkk"] = None
    frame.loc[0, "eligible"] = False
    frame.loc[0, "aip_ore"] = np.nan
    queues = queue_policies(frame)
    summary = queue_summary(queues).set_index(["policy", "workload"])
    assert summary.loc[("probability_reference", "natural"), "selected"] == 1
    assert summary.loc[("amount_dkk", "natural"), "selected"] == 0
    assert summary.loc[("amount_dkk", "matched"), "matched_shortfall"] == 1
    labels = frame[["period_start", "package_id"]].assign(
        next_status="pending", next_aip_ore=np.nan
    )
    values = price_exposure(allocate(queues), labels, queues=queues, allocation="fixed_capital")
    reference = values.loc[values.policy.eq("probability_reference")]
    assert reference.forecast_price_exposure_dkk.isna().all()
    assert reference.forecast_supported_selected.eq(0).all()
    assert reference.forecast_unknown_quantity.gt(0).all()


def test_independent_compensation_and_returns_count_each_credit_once() -> None:
    result, _ = simulate_group(
        [{"a": 10000, "b": 20000}, {"a": 8000, "b": 18000}, {"a": 8000, "b": 18000}],
        {"a": 2, "b": 2},
        capacity_extra=4,
        demand=1,
        variant="returns",
        compensation="half_cap2",
    )
    # Initial 2a get DKK20 total compensation, 2b DKK20. FIFO sells initial
    # 2a at adjusted DKK180 book. Unsold 2a+2b adjusted book is DKK580;
    # return cash =580*.9=522, net procurement cash=600-40-522=38.
    assert result["compensation_dkk"] == 40
    assert result["sold_book_cost_dkk"] == 180
    assert result["returned_basis_dkk"] == 580
    assert result["return_credit_dkk"] == 522
    assert result["return_fee_dkk"] == 58
    assert result["net_procurement_cash_dkk"] == 38
    assert result["sold"] == 2 and result["returned"] == 4 and result["remaining"] == 0


def test_frozen_reference_probabilities_set_matched_workload_only() -> None:
    frame = predictions(20).assign(p_rise=0.1, reference_p_rise=0.9, reference_p_fall=0.01)
    summary = queue_summary(queue_policies(frame)).set_index(["policy", "workload"])
    assert summary.loc[("probability_reference", "natural"), "selected"] == 2
    assert summary.loc[("amount_dkk", "natural"), "selected"] == 0
    assert summary.loc[("amount_dkk", "matched"), "selected"] == 2
    assert summary.loc[("amount_dkk", "matched"), "matched_requested"] == 2


def test_downside_funding_and_partial_group_cap_denominators() -> None:
    frame = predictions(100).assign(peer_count=199)
    queues = queue_policies(frame)
    allocation = allocate(queues)
    # Origin peer evidence supports the cap even though ineligible market peers
    # are absent from this candidate table; scenario trajectories need all peers.
    assert (
        allocation.allocated_ore.groupby([allocation.policy, allocation.workload])
        .sum()
        .le(2500000)
        .all()
    )
    labels = frame[["period_start", "package_id"]].assign(next_status="observed", next_aip_ore=9000)
    values = price_exposure(allocation, labels, queues=queues, allocation="fixed_capital")
    assert values.observed_price_exposure_ore.eq(values.observed_negative_exposure_ore).all()
    assert values.observed_positive_exposure_ore.eq(0).all()
    assert values.funded_selected.eq(3).all() and values.selected.eq(10).all()
    assert values.observed_funded_quantity.eq(250).all()
    assert values.max_package_budget_share.eq(0.10).all()
    assert values.top10_allocated_capital_share.eq(1).all()


def test_empty_policy_scenarios_and_unfiltered_reference_eligibility() -> None:
    frame = predictions(20).assign(eligible=False, reference_eligible=True)
    queues = queue_policies(frame)
    summary = queue_summary(queues).set_index(["policy", "workload"])
    assert summary.loc[("probability_reference", "natural"), "selected"] == 2
    assert summary.loc[("amount_dkk", "natural"), "selected"] == 0
    allocations = allocate(queues, "four_pack")
    variants = queue_summary(queues)[["policy", "workload"]].assign(allocation="four_pack")
    labels = frame[["period_start", "package_id"]].assign(
        next_status="observed", next_aip_ore=15000, second_status="observed", second_aip_ore=12000
    )
    results, unknown = compare_group_scenarios(frame, allocations, labels, variants=variants)
    empty = results.loc[results.policy.eq("amount_dkk") & results.workload.eq("natural")]
    assert unknown.empty and len(empty) == 6
    assert (
        empty.extra_quantity.eq(0).all() and empty.incremental_economic_cost_proxy_dkk.eq(0).all()
    )
