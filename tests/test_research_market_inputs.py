from __future__ import annotations

import json
import zipfile
from copy import deepcopy
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e4_features import native
from bef_redbird.research.group_features import pin
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
    "exit_date,changed_drug,changed_creation,continues",
    [
        (None, False, False, True),
        (date(1900, 1, 1), False, False, True),
        (date(2026, 11, 9), False, False, True),
        (date(2026, 10, 26), False, False, False),
        (date(2026, 10, 12), False, False, False),
        (date(1900, 1, 1), True, False, False),
        (date(1900, 1, 1), False, True, False),
    ],
)
def test_native_identity_continues_no_exit_but_not_recreated_or_exited_packages(
    tmp_path: Path,
    exit_date: date | None,
    changed_drug: bool,
    changed_creation: bool,
    continues: bool,
) -> None:
    # Native LMS02 positions are independent fixture constants, not the decoder's layout.
    package = bytearray(b" " * 137)
    package[0:11] = b"00000000001"
    package[11:17] = b"000123"
    package[103:111] = b"20200101"
    package[119:127] = b"19000101"
    original = tmp_path / "native.zip"
    with zipfile.ZipFile(original, "w") as archive:
        for number in (1, 3, 4, 15):
            archive.writestr(f"LMS{number:02}.TXT", b"")
        archive.writestr("LMS02.TXT", bytes(package) + b"\r\n")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"period": "2026-10-26", "original": pin(original)}))
    previous = pd.DataFrame(
        [
            {
                "package_number": "000123",
                "canonical_identity": "accepted-prior-identity",
                "drug_id": "00000000002" if changed_drug else "00000000001",
                "creation_date": date(2020, 1, 2) if changed_creation else date(2020, 1, 1),
                "exit_date": exit_date,
            }
        ]
    )
    raw, _ = native(snapshot, previous)
    assert raw.iloc[0].exit_date == date(1900, 1, 1)
    assert (raw.iloc[0].canonical_identity == "accepted-prior-identity") == continues


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
