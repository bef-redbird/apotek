from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e5_experiment import (
    bounded_support,
    persistence_labels,
    predict,
    training_rows,
)
from bef_redbird.research.e5_protocol import matrix
from bef_redbird.research.training_contract import KEYS


def frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    f = pd.DataFrame(
        {
            "row_key": ["a", "b", "c", "d"],
            "package_number": ["000001"] * 4,
            "period_index": [1, 2, 3, 4],
            "period_start": pd.to_datetime(
                ["2024-12-02", "2024-12-16", "2024-12-30", "2025-01-13"]
            ),
            "identity_episode_id": ["one"] * 4,
            "aip_ore": [1000, 1500, 1100, 900],
        }
    )
    labels = f[list(KEYS)].copy()
    labels["next_status"] = ["observed"] * 3 + ["pending"]
    labels["next_maturity_date"] = pd.to_datetime(
        ["2024-12-16", "2024-12-30", "2025-01-13", "2025-01-27"]
    )
    labels["next_aip_ore"] = [1500, 1100, 900, np.nan]
    labels["next_delta_ore"] = [500, -400, -200, np.nan]
    labels["next_direction"] = [1, -1, -1, np.nan]
    return f, labels


def test_joint_persistence_not_conditioned_on_observed_next_rise() -> None:
    f, labels = frames()
    out = persistence_labels(f, labels)
    assert out.row_key.tolist() == ["a", "b", "c", "d"]
    assert out.persistence_target.iloc[:2].tolist() == [1, 0]
    assert pd.isna(out.persistence_target.iloc[2])
    assert "next_aip_ore" not in f
    assert out.second_aip_ore.iloc[0] == 1100


def test_identity_break_is_not_success_or_flat() -> None:
    f, labels = frames()
    f.loc[1, "identity_episode_id"] = "two"
    labels.loc[1, "identity_episode_id"] = "two"
    out = persistence_labels(f, labels)
    assert out.second_status.iloc[0] == "identity_discontinuity"
    assert pd.isna(out.persistence_target.iloc[0])


def test_long_horizon_must_mature_strictly_before_cutoff() -> None:
    f, labels = frames()
    out = persistence_labels(f, labels)
    assert training_rows(f, out, "2024-12-30", persistence=True).empty
    assert training_rows(f, out, "2025-01-01", persistence=True).row_key.tolist() == ["a"]
    assert training_rows(f, out, "2025-01-01").row_key.tolist() == ["a", "b"]


def test_monetary_support_preserves_coherent_nonnegative_price() -> None:
    raw = np.array([-100.0, 50.0])
    origin = np.array([20.0, 30.0])
    price = bounded_support(raw, origin)
    assert price.tolist() == [0, 80]
    assert (price - origin).tolist() == [-20, 50]
    assert raw.tolist() == [-100, 50]
    with pytest.raises(ResearchError):
        bounded_support(np.array([np.nan]), np.array([10.0]))


def test_finite_matrix_leaves_two_attempts_for_failures() -> None:
    trials = matrix()
    assert len(trials) == 46
    assert len({x["id"] for x in trials}) == 46
    assert sum(x["fold"] == "screen" for x in trials) == 14
    assert sum(x["fold"] == "final" for x in trials) == 8


def test_quantile_order_and_price_support_keep_raw_prediction_and_input_boundary() -> None:
    class SavedModel:
        def predict(self, frame: pd.DataFrame, **_: object) -> np.ndarray:
            assert list(frame) == ["aip_ore"]
            return np.array([[3.0, -20.0, -5.0]])

    frame = pd.DataFrame({"row_key": ["origin"], "aip_ore": [1000], "second_aip_ore": [999999]})
    result = predict(SavedModel(), frame, {"task": "amount", "method": "quantile"}, ["aip_ore"], [])
    assert result.crossed.tolist() == [True]
    assert result[["raw_q05_delta_dkk", "raw_q50_delta_dkk", "raw_q95_delta_dkk"]].iloc[
        0
    ].tolist() == [3, -20, -5]
    assert result[["q05_price_dkk", "q50_price_dkk", "q95_price_dkk"]].iloc[0].tolist() == [
        0,
        5,
        13,
    ]


def test_ineligible_future_observation_stays_an_unknown_horizon_not_a_failed_event() -> None:
    features, labels = frames()
    features["eligible"] = [True, False, True, True]
    result = persistence_labels(features, labels)
    assert result.second_status.iloc[0] == "ineligible_next_origin"
    assert pd.isna(result.persistence_target.iloc[0])
    assert pd.isna(result.second_aip_ore.iloc[0])
    # Its origin itself remains present; future eligibility cannot erase it.
    assert result.row_key.tolist() == features.row_key.tolist()
