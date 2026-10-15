from datetime import date, timedelta

import pytest

from bef_redbird.domain import AnalysisSettings, HistoryDataset, SourceInfo
from bef_redbird.features import calculate_features


def history(values, gaps=None):
    dates = tuple(
        date(2025, 1, 6) + timedelta(days=(gaps or [i * 14 for i in range(len(values))])[i])
        for i in range(len(values))
    )
    return HistoryDataset(
        SourceInfo("synthetic.xlsx", "a" * 64, 1),
        dates,
        {"000001": tuple(None if v is None else round(v * 100) for v in values)},
        {},
        {},
        [],
    )


@pytest.mark.parametrize(
    "values,any_count,full_count,giveback",
    [
        ([100, 120, 118], 1, 0, 0.1),
        ([100, 120, 100], 1, 1, 1),
        ([100, 120, 80], 1, 1, 2),
        ([100, 120, 130], 0, 0, -0.5),
    ],
)
def test_independent_reversal_examples(values, any_count, full_count, giveback):
    h = history(values)
    f = calculate_features(h, "000001", h.periods[-1], AnalysisSettings(3, 3))
    assert f.reversal_denominator == 1
    assert f.any_reversal_count == any_count
    assert f.full_reversal_count == full_count
    assert f.events[0].giveback == pytest.approx(giveback)
    assert f.events[0].next_change == pytest.approx(values[2] / 120 - 1)


def test_exact_threshold_and_final_rise_denominator():
    h = history([100, 105, 100])
    f = calculate_features(h, "000001", h.periods[-1], AnalysisSettings(3, 3))
    assert f.rise_count == 0
    assert f.any_reversal_rate is None
    h = history([100, 105.01, 100, 120])
    f = calculate_features(h, "000001", h.periods[-1], AnalysisSettings(4, 3))
    assert f.rise_count == 2
    assert f.reversal_denominator == 1
    assert f.full_reversal_rate == 1


@pytest.mark.parametrize("values", [[100, 120, None, 90], [100, 120, 0, 90]])
def test_missing_slots_do_not_bridge(values):
    h = history(values)
    f = calculate_features(h, "000001", h.periods[-1], AnalysisSettings(4, 3))
    assert f.rise_count == 1
    assert f.adjacent_pair_count == 1
    assert f.reversal_denominator == 0
    assert f.coverage == 0.75
    assert f.any_reversal_rate is None
    assert f.previous_change is None


@pytest.mark.parametrize("values", [[100, 100, 100, 100], [80, 100, 120, 100]])
def test_midrank_ties(values):
    h = history(values)
    f = calculate_features(h, "000001", h.periods[-1], AnalysisSettings(4, 3))
    assert f.percentile == 0.5
    assert f.median_distance_dkk == 0


def test_half_cent_median_and_irregular_adjacency():
    h = history([100.01, 100])
    f = calculate_features(h, "000001", h.periods[-1], AnalysisSettings(2, 2))
    assert f.median_ore == 10000.5
    assert f.median_distance_dkk == 0.005
    h = history([100, 120, 100], [0, 28, 42])
    f = calculate_features(h, "000001", h.periods[-1], AnalysisSettings(3, 3))
    assert f.rise_count == 0
    assert f.adjacent_pair_count == 1


def test_window_boundaries_and_future_values():
    h = history([100, 120, 118, 1_000_000])
    f = calculate_features(h, "000001", h.periods[2], AnalysisSettings(3, 3))
    assert f.current_aip_ore == 11800
    assert f.n_valid == 3
    assert f.median_ore == 11800
    assert f.reversal_denominator == 1
    f = calculate_features(h, "000001", h.periods[2], AnalysisSettings(2, 2))
    assert f.rise_count == 0  # rise starts before the feature window
    assert f.median_ore == 11900
