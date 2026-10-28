"""Independent E3 chronology, physical-price and frozen-review acceptance cases."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from numpy.testing import assert_allclose
from pandas.testing import assert_frame_equal

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e3_methods import (
    calibrate,
    fallback,
    intervals,
    monetary_target,
    next_price,
    residual_fit,
    select_policies,
    select_queue,
    temperature_fit,
)


def predictions() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "row_key": f"{day}-{index}",
                "period_start": pd.Timestamp(day),
                "package_number": f"{index:06d}",
                "p0": 0.05 + index * 0.01,
                "p1": 0.80 - index * 0.04,
                "p2": 0.15 + index * 0.03,
                "aip_ore": 10000,
                "next_aip_dkk": 101.0,
                "next_status": "observed",
                "next_direction": [0, 1, -1][index % 3],
                "next_aip_ore": 10000 + 100 * [0, 1, -1][index % 3],
                "next_maturity_date": pd.Timestamp(maturity),
            }
            for day, maturity in [("2022-01-03", "2022-01-17"), ("2022-01-17", "2022-01-31")]
            for index in range(12)
        ]
    )


@pytest.mark.parametrize("fitter", [temperature_fit, residual_fit])
def test_calibration_uses_only_strictly_mature_earlier_outcomes(fitter: Any) -> None:
    frame = predictions()
    base = fitter(frame, "2022-01-31")
    assert base["rows"] == 12  # Maturity equal to the cutoff is not earlier.
    changed = frame.copy()
    last = changed.next_maturity_date.eq(pd.Timestamp("2022-01-31"))
    changed.loc[last, "next_aip_ore"] = 99999999
    changed.loc[last, "next_direction"] = 1
    assert fitter(changed, "2022-01-31") == base
    with pytest.raises(ResearchError, match="chronological"):
        fitter(frame, "2022-01-17")
    # Inconsistent future origins cannot be admitted just because their maturity is earlier.
    changed = frame.iloc[:12].copy()
    changed["period_start"] = pd.Timestamp("2023-01-01")
    with pytest.raises(ResearchError, match="chronological"):
        fitter(changed, "2022-01-31")


def test_calibrated_probabilities_and_intervals_remain_valid() -> None:
    frame = predictions()
    state = temperature_fit(frame, "2022-02-01")
    probabilities = calibrate(frame[["p0", "p1", "p2"]].to_numpy(), state)
    assert np.isfinite(probabilities).all() and (probabilities >= 0).all()
    assert_allclose(probabilities.sum(axis=1), 1.0)
    bounded = intervals(frame, residual_fit(frame, "2022-02-01"))
    assert (bounded.lower90_dkk >= 0).all()
    assert (bounded.upper90_dkk >= bounded.lower90_dkk).all()
    assert_frame_equal(bounded[frame.columns], frame)


@pytest.mark.parametrize(
    "policy,parameter", [("top_decile", None), ("risk_rank", 0.5), ("threshold", (0.25, 0.2))]
)
def test_review_membership_does_not_read_actual_outcomes(policy: str, parameter: Any) -> None:
    frame = predictions()
    expected = select_queue(frame, policy, parameter)
    changed = frame.copy()
    changed["next_status"] = "pending"
    changed["next_direction"] = 999
    changed["next_aip_ore"] = None
    actual = select_queue(changed, policy, parameter)
    assert_frame_equal(
        actual[["row_key", "selected", "rank", "queue_score"]],
        expected[["row_key", "selected", "rank", "queue_score"]],
    )
    # Complete absence of outcome fields is supported by the prediction-only freezer.
    bare = frame.drop(
        columns=["next_status", "next_direction", "next_aip_ore", "next_maturity_date"]
    )
    actual = select_queue(bare, policy, parameter)
    assert actual.selected.tolist() == expected.selected.tolist()


@pytest.mark.parametrize(
    "policy,parameter", [("top_decile", None), ("risk_rank", 0.5), ("threshold", (0.25, 0.2))]
)
def test_review_ranking_is_invariant_to_dataframe_index(policy: str, parameter: Any) -> None:
    frame = predictions()
    expected = select_queue(frame, policy, parameter)
    frame.index = np.arange(100, 100 + len(frame))
    actual = select_queue(frame, policy, parameter)
    assert_frame_equal(actual, expected)


def test_matched_workload_is_exact_for_every_period() -> None:
    frame = predictions()
    workload = {"2022-01-03": 0, "2022-01-17": 3}
    for policy, parameter in [("top_decile", None), ("risk_rank", 0.5), ("threshold", (1.0, 0.0))]:
        selected = select_queue(frame, policy, parameter, workload=workload)
        assert selected.groupby("period_start").selected.sum().tolist() == [0, 3]
        assert selected.loc[selected.selected, "rank"].tolist() == [3, 2, 1]


@pytest.mark.parametrize("count", [-1, 13, 1.5, True])
def test_invalid_matched_workload_is_rejected_instead_of_silently_truncated(count: Any) -> None:
    with pytest.raises(ResearchError):
        select_queue(predictions(), "top_decile", workload={"2022-01-03": count, "2022-01-17": 1})


@pytest.mark.parametrize("formulation", ["delta_floor", "log_ratio"])
def test_monetary_predictions_have_finite_nonnegative_next_price(formulation: str) -> None:
    frame = pd.DataFrame({"aip_ore": [10000, 5000, 1000], "next_aip_ore": [12000, 4000, 1000]})
    target, state = monetary_target(frame, formulation)
    current = frame.aip_ore.to_numpy(dtype=float) / 100
    assert_allclose(next_price(target, current, state), [120, 40, 10])
    bounded = next_price(np.array([-1000000, 0, 1000000]), current, state)
    assert np.isfinite(bounded).all() and (bounded >= 0).all()
    delta = bounded - current
    assert (delta >= -current).all()  # It cannot forecast a loss larger than all current AIP.
    if formulation == "log_ratio":
        assert_allclose(bounded[[0, 2]] / current[[0, 2]], [0.8, 1.2])
    else:
        assert bounded[0] == 0


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf")])
def test_unusable_actual_money_is_not_a_training_target(amount: float) -> None:
    frame = pd.DataFrame({"aip_ore": [10000], "next_aip_ore": [amount]})
    with pytest.raises(ResearchError):
        monetary_target(frame, "log_ratio")


def test_unknown_amount_formulation_is_refused() -> None:
    with pytest.raises(ResearchError):
        monetary_target(pd.DataFrame({"aip_ore": [10000], "next_aip_ore": [12000]}), "typo")


def test_fallback_uses_only_fixed_origin_support_mask() -> None:
    package = predictions()
    peers = package.copy()
    peers[["p0", "p1", "p2"]] = [0.1, 0.2, 0.7]
    support = np.arange(len(package)) % 2 == 0
    routed = fallback(package, peers, support)
    assert_allclose(routed.loc[support, ["p0", "p1", "p2"]], peers.loc[support, ["p0", "p1", "p2"]])
    assert_allclose(
        routed.loc[~support, ["p0", "p1", "p2"]], package.loc[~support, ["p0", "p1", "p2"]]
    )
    assert routed.route.tolist()[:2] == ["peer_model", "package_fallback"]
    package["next_status"] = "pending"
    peers["next_direction"] = -1
    changed = fallback(package, peers, support)
    assert_allclose(changed[["p0", "p1", "p2"]], routed[["p0", "p1", "p2"]])


@pytest.mark.parametrize("mutation", ["key", "period", "package", "mask_length"])
def test_fallback_rejects_misaligned_keys_and_masks(mutation: str) -> None:
    package, peers = predictions(), predictions()
    support = np.ones(len(package), dtype=bool)
    if mutation == "key":
        peers.loc[0, "row_key"] = "different"
    elif mutation == "period":
        peers.loc[0, "period_start"] = pd.Timestamp("2025-01-06")
    elif mutation == "package":
        peers.loc[0, "package_number"] = "999999"
    else:
        support = support[:-1]
    with pytest.raises(ResearchError):
        fallback(package, peers, support)


def test_policy_selection_uses_only_declared_2023_validation_origins() -> None:
    frame = predictions()
    frame["period_start"] = frame.period_start + pd.DateOffset(years=1)
    expected = select_policies(frame)
    later = frame.copy()
    later["period_start"] = later.period_start + pd.DateOffset(years=1)
    later["next_direction"] = -1
    assert select_policies(pd.concat([frame, later], ignore_index=True)) == expected
