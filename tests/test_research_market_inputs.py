from __future__ import annotations

import json
import zipfile
from copy import deepcopy
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from bef_redbird.research import e4_features, market_inputs
from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e4_common import read
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


def test_nullable_money_preserves_exact_integers_above_float_precision(tmp_path: Path) -> None:
    frame = e4_features.money_frame([{"aip_ore": 2**53 + 1}, {"aip_ore": None}])
    path = tmp_path / "money.parquet"
    frame.to_parquet(path, index=False)
    restored = e4_features.records(pd.read_parquet(path))
    assert restored == [{"aip_ore": 9007199254740993}, {"aip_ore": None}]
    assert type(restored[0]["aip_ore"]) is int


def test_mixed_missing_prices_and_interrupted_checkpoint_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two real fixed-width packages, only one with an LMS03 price.
    original = tmp_path / "native.zip"
    package_rows, group_rows = [], []
    for number in ("000123", "000124"):
        package = bytearray(b" " * 137)
        package[0:11] = b"00000000001"
        package[11:17] = number.encode()
        package[59:67] = b"30      "
        package[67:69] = b"TL"
        package[73:78] = b"B    "
        package[103:111] = b"20200101"
        package[119:127] = b"19000101"
        package_rows.append(bytes(package))
        group = bytearray(b" " * 47)
        group[0:4] = b"0001"
        group[4:10] = number.encode()
        group[10:18] = b"30      "
        group[34:35] = b"A"
        group_rows.append(bytes(group))
    with zipfile.ZipFile(original, "w") as archive:
        archive.writestr("LMS01.TXT", b"00000000001" + b" " * 169 + b"\r\n")
        archive.writestr("LMS02.TXT", b"\r\n".join(package_rows) + b"\r\n")
        archive.writestr("LMS03.TXT", b"000123000001234" + b" " * 81 + b"\r\n")
        archive.writestr("LMS04.TXT", b"\r\n".join(group_rows) + b"\r\n")
        archive.writestr("LMS15.TXT", b"")
    previous, snapshot, calendar = inputs()
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps(calendar))
    prior_snapshot = tmp_path / "prior-snapshot.json"
    prior_snapshot.write_text(json.dumps({"period": previous["origin"], "original": pin(original)}))
    raw, members = native(prior_snapshot, pd.DataFrame())
    base = e4_features.advance(raw, pd.DataFrame(), pd.DataFrame(), calendar)
    previous.update(version="causal-market-checkpoint-v1", calendar=pin(calendar_path))
    for name, frame in (
        ("raw", raw),
        ("features", base),
        ("native", e4_features.money_frame(members)),
    ):
        path = tmp_path / f"prior-{name}.parquet"
        frame.to_parquet(path, index=False)
        previous[name] = pin(path)
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(json.dumps(previous))
    snapshot.update(original=pin(original), rows=previous["raw"])
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot))
    destination = tmp_path / "next"
    # A legacy interrupted stage must also be left intact and not prevent retry.
    legacy = tmp_path / "next.stage"
    legacy.mkdir()
    (legacy / "retained.txt").write_text("earlier interrupted attempt")
    publish = market_inputs.publish_directory

    def fail_publish(stage: Path, output: Path) -> None:
        assert (stage / "checkpoint.json").exists()
        raise OSError("synthetic interruption before publication")

    monkeypatch.setattr(market_inputs, "publish_directory", fail_publish)
    with pytest.raises(OSError, match="interruption"):
        advance_checkpoint(prior_path, snapshot_path, destination)
    assert not destination.exists()
    (orphan,) = tmp_path.glob("next.stage-*")
    retained = {p.name: p.read_bytes() for p in orphan.iterdir()}
    monkeypatch.setattr(market_inputs, "publish_directory", publish)
    result = advance_checkpoint(prior_path, snapshot_path, destination)
    assert {p.name: p.read_bytes() for p in orphan.iterdir()} == retained
    assert (legacy / "retained.txt").read_text() == "earlier interrupted attempt"
    saved = read(result)
    assert saved["rows"] == 2 and saved["eligible"] == 1
    for name in ("raw", "features", "native"):
        frame = pd.read_parquet(market_inputs.checked(saved[name]))
        rows = e4_features.records(frame)
        assert rows[0]["aip_ore"] == 1234 and type(rows[0]["aip_ore"]) is int
        assert rows[1]["aip_ore"] is None
    values = pd.read_parquet(saved["features"]["path"]).set_index("package_number")
    assert bool(values.loc["000123", "eligible"])
    assert values.loc["000123", "history_periods"] == 1
    assert values.loc["000123", "lag1_delta_ore"] == 0
    assert values.loc["000124", "exclusion_reason"] == "aip_missing"
    before = result.read_bytes()
    assert advance_checkpoint(prior_path, snapshot_path, destination) == result
    assert result.read_bytes() == before


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
