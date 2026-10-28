"""Synthetic delivery, transactional publication and independent audit gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation import ftp_supplement_store as module
from bef_redbird.foundation.ftp_supplement_store import (
    SupplementError,
    ingest_supplement,
    supplement_records,
)
from tests.test_foundation_ftp_supplements import _csv, _dok


def _register(store: Store, path: Path, data: bytes, receipt: str = "one") -> str:
    path.write_bytes(data)
    return store.register_source(path, source="synthetic-supplement", receipt_key=receipt)


@pytest.mark.parametrize("kind", ["DOK", "LMS36", "LMS37"])
def test_acceptance_retry_and_located_payload(tmp_path: Path, kind: str) -> None:
    data = {
        "DOK": _dok(),
        "LMS36": _csv("LMS36", "00000000001;EXAMPLE;20240229;\r\n"),
        "LMS37": _csv("LMS37", "EXAMPLE;Group;\r\n"),
    }[kind]
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = _register(store, tmp_path / "unrelated-future-20300101.dat", data)
        result = ingest_supplement(store, delivery, kind)
        assert result["status"] == "accepted"
        assert result["independently_verified"]
        assert result["verification"]["accepted"]
        assert result["counts"] == {"record_count": 1, "accepted_count": 1, "rejected_count": 0}
        assert result["coverage"]["published_rows"] == 1
        rows = supplement_records(store, result["import_id"])
        assert len(rows) == 1
        assert rows[0]["source_line"] == (12 if kind == "DOK" else 2)
        assert "2030" not in json.dumps(rows)
        again = ingest_supplement(store, delivery, kind)
        assert again == {**result, "retry": True}
        assert store._connection.execute("SELECT count(*) FROM supplement_record").fetchone() == (
            1,
        )
        assert store._connection.execute("SELECT count(*) FROM package_identifier").fetchone() == (
            0,
        )
        assert store._connection.execute("SELECT count(*) FROM supply_observation").fetchone() == (
            0,
        )


def test_duplicate_occurrences_preserved_and_receipts_not_conflated(tmp_path: Path) -> None:
    row = "EXAMPLE;Group;\r\n"
    data = _csv("LMS37", row * 2)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        path = tmp_path / "same.csv"
        first = ingest_supplement(store, _register(store, path, data), "LMS37")
        second = ingest_supplement(store, _register(store, path, data, "two"), "LMS37")
        assert first["import_id"] != second["import_id"]
        assert first["counts"]["accepted_count"] == 2
        assert [r["source_line"] for r in supplement_records(store, first["import_id"])] == [2, 3]
        assert store._connection.execute("SELECT count(*) FROM supplement_import").fetchone() == (
            2,
        )
        assert store._connection.execute("SELECT count(*) FROM supplement_record").fetchone() == (
            4,
        )


def test_changed_content_preserves_old_assertions(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        path = tmp_path / "same.csv"
        first_data = _csv("LMS37", "EXAMPLE;First;\r\n")
        first = ingest_supplement(store, _register(store, path, first_data), "LMS37")
        changed = first_data.replace(b"First", b"Second")
        with pytest.raises(ValueError, match="Receipt key"):
            _register(store, path, changed)
        second = ingest_supplement(store, _register(store, path, changed, "revision"), "LMS37")
        assert first["import_id"] != second["import_id"]
        assert supplement_records(store, first["import_id"])[0]["professional_group"] == "First"
        assert supplement_records(store, second["import_id"])[0]["professional_group"] == "Second"


@pytest.mark.parametrize(
    "data,kind,coverage,count",
    [
        (b"Unknown header\n", "DOK", "raw_only", 0),
        (b"DrugID;Unknown\r\n", "LMS36", "raw_only", 0),
        (_csv("LMS37", "EXAMPLE;Group;\r\nbad\r\n"), "LMS37", "partial", 2),
    ],
)
def test_unsupported_partial_never_publish_complete_coverage(
    tmp_path: Path,
    data: bytes,
    kind: str,
    coverage: str,
    count: int,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = _register(store, tmp_path / "source", data)
        result = ingest_supplement(store, delivery, kind)
        assert result["status"] == "rejected"
        assert not result["independently_verified"]
        assert result["coverage"]["status"] == coverage
        assert result["coverage"]["published_rows"] == 0
        assert result["counts"] == {
            "record_count": count,
            "accepted_count": 0,
            "rejected_count": count,
        }
        assert supplement_records(store, result["import_id"]) == []
        assert ingest_supplement(store, delivery, kind) == {**result, "retry": True}
        assert (
            store._connection.execute(
                "SELECT count(*) FROM import_issue WHERE blocking_scope='import'"
            ).fetchone()[0]
            > 0
        )


def test_parser_corruption_fails_native_audit_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = module.parse_supplement

    def corrupt(data: bytes, kind: str) -> dict:
        result = original(data, kind)
        result["records"][0]["valid_from"] = "2030-01-01"
        return result

    monkeypatch.setattr(module, "parse_supplement", corrupt)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = _register(
            store, tmp_path / "source", _csv("LMS36", "00000000001;X;20240229;\r\n")
        )
        result = ingest_supplement(store, delivery, "LMS36")
        assert result["status"] == "rejected"
        assert not result["verification"]["accepted"]
        assert supplement_records(store, result["import_id"]) == []


def test_interrupted_publication_rolls_back_and_same_import_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = module._insert_records

    def fail(store: Store, import_id: str, records: list[dict]) -> None:
        original(store, import_id, records[:1])
        raise OSError("synthetic interrupted publication")

    monkeypatch.setattr(module, "_insert_records", fail)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = _register(
            store, tmp_path / "source", _csv("LMS37", "ONE;Group;\r\nTWO;Group;\r\n")
        )
        with pytest.raises(OSError, match="interrupted"):
            ingest_supplement(store, delivery, "LMS37")
        assert store._connection.execute("SELECT count(*) FROM supplement_record").fetchone() == (
            0,
        )
        assert store._connection.execute("SELECT count(*) FROM supplement_import").fetchone() == (
            0,
        )
        assert store._connection.execute(
            "SELECT validation_status FROM import_batch"
        ).fetchone() == ("running",)
        prior_id = str(
            store._connection.execute("SELECT import_id FROM import_batch").fetchone()[0]
        )
        monkeypatch.setattr(module, "_insert_records", original)
        result = ingest_supplement(store, delivery, "LMS37")
        assert result["status"] == "accepted"
        assert result["import_id"] == prior_id
        assert result["counts"]["accepted_count"] == 2


def test_bad_persisted_readback_rolls_back_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = module._insert_records

    def corrupt(store: Store, import_id: str, records: list[dict]) -> None:
        original(store, import_id, records)
        store._connection.execute(
            "UPDATE supplement_record SET source_line=99 WHERE import_id=?", [import_id]
        )

    monkeypatch.setattr(module, "_insert_records", corrupt)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = _register(store, tmp_path / "source", _csv("LMS37", "ONE;Group;\r\n"))
        with pytest.raises(SupplementError, match="indexed column"):
            ingest_supplement(store, delivery, "LMS37")
        assert store._connection.execute("SELECT count(*) FROM supplement_record").fetchone() == (
            0,
        )
        assert store._connection.execute("SELECT count(*) FROM supplement_import").fetchone() == (
            0,
        )
        assert store._connection.execute(
            "SELECT validation_status FROM import_batch"
        ).fetchone() == ("running",)


@pytest.mark.parametrize("corruption", ["payload", "index", "missing", "metadata", "proof"])
def test_accepted_retry_detects_persisted_corruption(tmp_path: Path, corruption: str) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = _register(store, tmp_path / "source", _csv("LMS37", "ONE;Group;\r\n"))
        result = ingest_supplement(store, delivery, "LMS37")
        import_id = result["import_id"]
        if corruption == "payload":
            row = supplement_records(store, import_id)[0]
            row["professional_group"] = "Changed"
            store._connection.execute("UPDATE supplement_record SET payload=?", [json.dumps(row)])
        elif corruption == "index":
            store._connection.execute(
                "UPDATE supplement_record SET extended_prescribing_code='Wrong'"
            )
        elif corruption == "missing":
            store._connection.execute("DELETE FROM supplement_record")
        elif corruption == "metadata":
            store._connection.execute("UPDATE supplement_import SET metadata='{}'")
        else:
            store._connection.execute("UPDATE supplement_import SET verification='{}'")
        with pytest.raises(SupplementError):
            ingest_supplement(store, delivery, "LMS37")


def test_kind_participates_in_import_identity(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = _register(store, tmp_path / "source", _csv("LMS37", "ONE;Group;\r\n"))
        accepted = ingest_supplement(store, delivery, "LMS37")
        rejected = ingest_supplement(store, delivery, "LMS36")
        assert accepted["status"] == "accepted"
        assert rejected["status"] == "rejected"
        assert accepted["import_id"] != rejected["import_id"]
