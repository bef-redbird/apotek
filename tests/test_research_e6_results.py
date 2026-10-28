"""Consequential amount and temporal accounting examples for E6."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e6_results import (
    aggregate,
    amount_one,
    direction_one,
    evaluate_values,
    paired_interval,
    require_terminal_trials,
)
from bef_redbird.research.group_features import pin
from bef_redbird.research.training_contract import atomic_json


def sample() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "period_start": pd.to_datetime(
                ["2026-06-01", "2026-06-01", "2026-06-15", "2026-06-29"]
            ),
            "next_status": ["observed", "observed", "observed", "pending"],
            "next_delta_ore": [100, -100, 10000, None],
            "next_direction": [1, -1, 1, None],
            "aip_ore": [200, 300, 20000, 500],
            "history_periods": [2, 9, 8, 3],
            "peer_count": [1, 0, None, None],
        }
    )


def test_amount_exact_units_unknown_and_signed_totals() -> None:
    score = amount_one(sample(), np.array([0.0, 0.0, 90.0, -1000000.0]))
    assert score["candidates"] == 4
    assert score["observed"] == 3
    assert score["unknown"] == 1
    assert score["mae_dkk"] == 4
    assert score["bias_dkk"] == pytest.approx(-10 / 3)
    assert score["rmse_dkk"] == pytest.approx(np.sqrt(34))
    assert score["predicted_total_dkk"] == 90
    assert score["observed_total_dkk"] == 100
    assert score["top1_absolute_error_share"] == pytest.approx(10 / 12)


def test_equal_period_primary_does_not_pool_rows_or_score_pending_period() -> None:
    summary, periods = evaluate_values(sample(), np.array([0.0, 0.0, 90.0, 0.0]), "amount", {})
    all_cases = next(row for row in summary if row["stratum"] == "all")
    assert all_cases["mae_dkk"] == 4
    assert all_cases["equal_period_mae_dkk"] == 5.5
    assert all_cases["periods"] == 3
    assert all_cases["observed_periods"] == 2
    pending = next(row for row in periods if row["stratum"] == "all" and row["observed"] == 0)
    assert "mae_dkk" not in pending


def test_probability_classes_and_pending_no_unchanged_imputation() -> None:
    p = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]])
    score = direction_one(sample(), p)
    assert score["brier"] == 0
    assert score["accuracy"] == 1
    assert score["unknown"] == 1


def test_paired_temporal_interval_is_deterministic_and_no_row_resampling() -> None:
    result = paired_interval(np.array([-2.0, -2.0, -2.0, -2.0]), 2)
    assert result["difference"] == result["lower"] == result["upper"] == -2
    assert result["periods"] == 4
    assert paired_interval(np.array([1.0]), 2)["status"] == "insufficient_periods"
    assert paired_interval(np.array([1.0, 9.0]), 2)["status"] == "insufficient_periods"


def test_weighting_is_compared_with_same_full_cohort_on_common_periods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bef_redbird.research.e6_results.require", lambda root: {})
    atomic_json(tmp_path / "protocol.json", {})
    folder = tmp_path / "evaluations" / "direction-summer"
    folder.mkdir(parents=True)
    records = []
    for history, values in {
        "3y": [("spring", "2026-06-08", 0.9), ("summer", "2026-09-14", 0.3)],
        "full": [("spring", "2026-06-08", 0.8), ("summer", "2026-09-14", 0.2)],
        "weighted": [("summer", "2026-09-14", 0.25)],
    }.items():
        for fold, date, error in values:
            records.append(
                {
                    "investigation": "D",
                    "task": "direction",
                    "fold": fold,
                    "period_start": date,
                    "stratum": "all",
                    "route": "routed",
                    "calibration": "calibrated",
                    "history": history,
                    "information": "current",
                    "brier": error,
                    "ece": error,
                    "logloss": error,
                }
            )
    pd.DataFrame(records).to_parquet(folder / "periods.parquet", index=False)
    atomic_json(
        folder / "result.json",
        {
            "summaries": [],
            "reused": {},
            "files": {"periods.parquet": pin(folder / "periods.parquet")},
        },
    )
    contrasts = aggregate(tmp_path)["contrasts"]
    weighted = next(
        row
        for row in contrasts
        if row["reference"] == "full"
        and row["candidate"] == "weighted"
        and row["metric"] == "brier"
    )
    assert weighted["reference_equal_period"] == 0.2
    assert weighted["candidate_equal_period"] == 0.25
    assert weighted["intervals"][0]["periods"] == 1
    assert weighted["intervals"][0]["status"] == "insufficient_periods"


@pytest.mark.parametrize("status", [None, "running", "completed", "failed", "skipped"])
def test_evaluation_waits_for_declared_arms_but_retains_terminal_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    spec = {"id": "weighted", "fold": "summer", "investigation": "D", "task": "direction"}
    monkeypatch.setattr("bef_redbird.research.e6_results.require", lambda root: {"matrix": [spec]})
    if status:
        folder = tmp_path / "trials/weighted"
        folder.mkdir(parents=True)
        atomic_json(folder / "receipt.json", {"status": status})
    if status in {None, "running"}:
        with pytest.raises(ResearchError, match="declared_trial_pending"):
            require_terminal_trials(tmp_path, "summer", "D", "direction")
    else:
        require_terminal_trials(tmp_path, "summer", "D", "direction")
    # An unrelated task/fold must not wait on the optional summer direction arm.
    require_terminal_trials(tmp_path, "spring", "D", "direction")
    require_terminal_trials(tmp_path, "summer", "D", "amount")
