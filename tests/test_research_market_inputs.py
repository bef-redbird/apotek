from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.market_inputs import advance_checkpoint, validate_step


def inputs() -> tuple[dict, dict, list[dict]]:
    previous = {"origin": "2026-01-05", "target": "2026-01-19"}
    snapshot = {
        "version": "e4-official-snapshot-v1",
        "period": "2026-01-19",
        "discrepancies": 0,
        "context": {
            "current_period": "2026-01-19",
            "release_date": "2026-01-19",
            "period_end_exclusive": "2026-02-02",
        },
    }
    calendar = [
        {"index": 10, "start": "2026-01-05", "end_exclusive": "2026-01-19"},
        {"index": 11, "start": "2026-01-19", "end_exclusive": "2026-02-02"},
    ]
    return previous, snapshot, calendar


def test_supported_step_does_not_require_future_market_facts() -> None:
    assert validate_step(*inputs()) == ("2026-01-19", "2026-02-02")


def test_operator_rejects_artifact_storage_inside_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="outside Git"):
        advance_checkpoint(tmp_path / "prior.json", tmp_path / "snapshot.json", repo / "output")
    assert not (repo / "output").exists()


@pytest.mark.parametrize(
    "defect",
    ["missing_period", "duplicate_period", "gap", "prior_target", "release", "end", "proof"],
)
def test_reject_missing_ambiguous_future_or_unverified_context(defect: str) -> None:
    previous, snapshot, calendar = deepcopy(inputs())
    if defect == "missing_period":
        calendar.pop()
    elif defect == "duplicate_period":
        calendar.append(calendar[-1].copy())
    elif defect == "gap":
        calendar[-1]["index"] += 1
    elif defect == "prior_target":
        previous["target"] = "2026-02-02"
    elif defect == "release":
        snapshot["context"]["release_date"] = "2026-02-02"
    elif defect == "end":
        snapshot["context"]["period_end_exclusive"] = "2026-02-16"
    else:
        snapshot["discrepancies"] = 1
    with pytest.raises(ResearchError, match="nonconsecutive_or_unsupported"):
        validate_step(previous, snapshot, calendar)
