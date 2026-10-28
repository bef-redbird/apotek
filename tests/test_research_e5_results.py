from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e5_results import align, binary_temperature, json_records, money_score


def test_missing_saved_prediction_cannot_be_silently_reindexed() -> None:
    with pytest.raises(ResearchError, match="missing_prediction_keys"):
        align(
            pd.DataFrame({"row_key": ["known", "missing"]}),
            pd.DataFrame({"row_key": ["known"], "p0": [0.2]}),
        )


def test_saved_json_handles_empty_outcomes_and_dates() -> None:
    frame = pd.DataFrame({"period_start": pd.to_datetime(["2026-01-05"]), "amount": [np.nan]})
    assert json_records(frame) == [{"period_start": "2026-01-05T00:00:00.000", "amount": None}]


def test_amount_error_preserves_extremes_and_excludes_unknown_labels_only() -> None:
    frame = pd.DataFrame(
        {
            "aip_ore": [1000, 1000, 1000],
            "next_status": ["observed", "observed", "pending"],
            "next_aip_ore": [1100, 100001000, np.nan],
        }
    )
    result = money_score(
        frame, np.array([10.0, 10.0, 10.0]), np.array([0.0, 0.0, 0.0]), np.array([20.0, 20.0, 20.0])
    )
    assert result["candidates"] == 3 and result["observed"] == 2
    assert result["max_error_dkk"] == 1_000_000
    assert result["observed_total_delta_dkk"] == 1_000_001
    assert result["predicted_total_delta_dkk"] == 0
    assert result["coverage90"] == 0.5


def test_persistence_calibration_excludes_immature_equal_cutoff_label() -> None:
    frame = pd.DataFrame(
        {
            "period_start": pd.to_datetime(["2025-04-07", "2025-05-19"]),
            "second_status": ["observed", "observed"],
            "second_maturity_date": pd.to_datetime(["2025-05-05", "2025-06-01"]),
            "persistence_target": [0.0, 1.0],
        }
    )
    result = binary_temperature(frame, np.array([0.2, 0.9]), "2025-06-01")
    assert result["rows"] == 1
    assert result["last_maturity"] == "2025-05-05"


def test_calibration_checkpoint_prevents_refitting_and_enforces_shared_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bef_redbird.research.e5_results as results

    (tmp_path / "protocol.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        results, "require", lambda _: {"calibration": {"maximum_temperature_fits": 1}}
    )
    calls = []

    def fit(frame: pd.DataFrame, cutoff: str) -> dict:
        calls.append(cutoff)
        return {"temperature": 1.25}

    monkeypatch.setattr(results, "temperature_fit", fit)
    frame = pd.DataFrame(
        {
            "row_key": ["a"],
            "period_start": pd.to_datetime(["2025-04-07"]),
            "next_status": ["observed"],
            "next_maturity_date": pd.to_datetime(["2025-04-21"]),
            "next_direction": [1],
        }
    )
    probability = np.array([[0.1, 0.2, 0.7]])
    first = results.temperature_once(
        tmp_path, "screen", "current", frame, probability, "2025-06-01"
    )
    second = results.temperature_once(
        tmp_path, "screen", "current", frame, probability, "2025-06-01"
    )
    assert first == second == {"temperature": 1.25}
    assert len(calls) == 1
    with pytest.raises(ResearchError, match="retry_changed_inputs"):
        results.temperature_once(
            tmp_path, "screen", "current", frame, probability + 0.01, "2025-06-01"
        )
    with pytest.raises(ResearchError, match="budget_exhausted"):
        results.temperature_once(tmp_path, "final", "current", frame, probability, "2025-06-01")
    assert len(calls) == 1


def test_tuning_selection_requires_terminal_complete_information_pairs(tmp_path: Path) -> None:
    from bef_redbird.research.e5_results import screen_candidates

    for method in ("early_stop", "shallow"):
        for mode in ("current", "advance"):
            folder = tmp_path / "trials" / f"screen-{method}-{mode}"
            folder.mkdir(parents=True)
            status = "failed" if (method, mode) == ("early_stop", "advance") else "completed"
            (folder / "receipt.json").write_text(json.dumps({"status": status}))
    # Missing work is not silently called a failed or complete experiment.
    with pytest.raises(ResearchError, match="screen_not_terminal"):
        screen_candidates(tmp_path)
    (tmp_path / "execution-decisions.json").write_text(
        json.dumps({"skipped_trials": ["screen-slow-current", "screen-slow-advance"]})
    )
    selectable, unavailable = screen_candidates(tmp_path)
    assert selectable == ["shallow"]
    assert unavailable == [
        {
            "method": "early_stop",
            "information_status": {"current": "completed", "advance": "failed"},
        },
        {
            "method": "slow",
            "information_status": {"current": "resource_skipped", "advance": "resource_skipped"},
        },
    ]
