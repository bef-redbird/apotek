"""Compact storage preserves edition assertions without duplicating native values."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation.lms_registry import (
    FIXED_WIDTH_PROFILE,
    MISSING_FINAL_CRLF_PROFILE,
    RegistryError,
    decode_row,
    ingest_registry,
    member_rows,
    read_release,
    verify_registry,
)
from bef_redbird.foundation.lms_registry_audit import audit_member, audit_release
from tests.test_foundation_lms_ftp import delivery_bytes, members
from tests.test_foundation_registry_audit import native, release_fixture


def fixed_fixture(target: Path, changes: dict[str, bytes]) -> Path:
    source = release_fixture(target)
    with ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    # The fixture's existing control counts remain independent and unchanged.
    files.update(changes)
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return source


def test_fixed_width_variant_preserves_embedded_text_and_exact_final_records(
    tmp_path: Path,
) -> None:
    price = native(96, (1, b"000042"), (7, b"000012345"))
    description = native(555, (1, b"ABCDE"), (56, b"Before\r\nafter"))
    changes = {
        "lms03.txt": price[:-2],
        "lms04.txt": native(47)[:-2],
        "lms31.txt": native(36)[:-2],
        "lms17.txt": description,
    }
    source = fixed_fixture(tmp_path / "variant.zip", changes)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(source, source="synthetic", receipt_key="one")
        rejected = ingest_registry(store, delivery, phase="final", context={})
        original_rejection = store.get_record("import_batch", rejected["import_id"])
        accepted = ingest_registry(
            store, delivery, phase="final", context={}, parser_version=FIXED_WIDTH_PROFILE
        )
        assert accepted["status"] == "accepted"
        proof = verify_registry(store, accepted["import_id"], recheck=True)
        assert proof["complete"]
        assert store._connection.execute("SELECT aip_ore FROM registry_lms03").fetchone() == (
            12345,
        )
        assert store._connection.execute("SELECT description FROM registry_lms17").fetchone() == (
            "Before\r\nafter",
        )
        independent = audit_release(source, fixed_width_framing=True)
        assert independent["fixed_width_members"] == [
            "lms03.txt",
            "lms04.txt",
            "lms17.txt",
            "lms31.txt",
        ]
        assert independent["members"]["lms17.txt"]["physical_records"] == 1
        assert ingest_registry(
            store, delivery, phase="final", context={}, parser_version=FIXED_WIDTH_PROFILE
        )["retry"]
        assert store.get_record("import_batch", rejected["import_id"]) == original_rejection
        member = store._connection.execute(
            "SELECT member_id FROM registry_member WHERE member_number=17"
        ).fetchone()[0]
        rows = member_rows(store, member)
        rows[0]["description"] = "Beforeafter"
        assert not audit_member(17, description, rows, fixed_width_framing=True)["complete"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"lms03.txt": native(96, (1, b"000042"))[:-3]}, "native_record_width"),
        ({"lms03.txt": native(96, (1, b"000042"))[:-2] + b"\n"}, "native_record_terminator"),
        ({"lms17.txt": native(555) + native(555)}, "system_record_count"),
        ({}, "native_variant_not_present"),
    ],
)
def test_fixed_width_variant_refuses_truncation_boundary_and_count_repairs(
    tmp_path: Path, changes: dict[str, bytes], reason: str
) -> None:
    source = fixed_fixture(tmp_path / "bad.zip", changes)
    with pytest.raises(RegistryError, match=reason):
        read_release(source, "final", parser_version=FIXED_WIDTH_PROFILE)
    if changes:
        with pytest.raises(ValueError):
            audit_release(source, fixed_width_framing=True)


def test_final_record_variant_preserves_imports_proofs_and_typed_dedup(tmp_path: Path) -> None:
    complete = native(137, (1, b"28100000001"), (12, b"000042"))
    old = release_fixture(tmp_path / "old.zip")
    variant = release_fixture(tmp_path / "variant.zip", source_changes={"lms02.txt": complete[:-2]})
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old_delivery = store.register_source(old, source="synthetic", receipt_key="old")
        old_import = ingest_registry(store, old_delivery, phase="final", context={})["import_id"]
        old_proof = verify_registry(store, old_import, recheck=True)
        old_rows = store._connection.execute(
            "SELECT * FROM registry_release_verification WHERE import_id=?", [old_import]
        ).fetchall()
        old_members = store._connection.execute(
            "SELECT member_number,member_id FROM registry_release_member WHERE import_id=?",
            [old_import],
        ).fetchall()
        old_package = store._connection.execute("SELECT * FROM registry_lms02").fetchall()
        delivery = store.register_source(variant, source="synthetic", receipt_key="variant")
        rejected = ingest_registry(store, delivery, phase="final", context={})
        assert rejected["reason"] == "native_record_terminator"
        rejected_batch = store.get_record("import_batch", rejected["import_id"])
        result = ingest_registry(
            store, delivery, phase="final", context={}, parser_version=MISSING_FINAL_CRLF_PROFILE
        )
        assert result["status"] == "accepted"
        assert result["import_id"] != rejected["import_id"]
        assert verify_registry(store, result["import_id"], recheck=True)["complete"]
        assert ingest_registry(
            store, delivery, phase="final", context={}, parser_version=MISSING_FINAL_CRLF_PROFILE
        )["retry"]
        assert ingest_registry(store, delivery, phase="final", context={})["status"] == "rejected"
        assert store.get_record("import_batch", rejected["import_id"]) == rejected_batch
        assert verify_registry(store, old_import, recheck=True) == old_proof
        assert (
            store._connection.execute(
                "SELECT * FROM registry_release_verification WHERE import_id=?", [old_import]
            ).fetchall()
            == old_rows
        )
        assert store._connection.execute("SELECT * FROM registry_lms02").fetchall() == old_package
        new_members = dict(
            store._connection.execute(
                "SELECT member_number,member_id FROM registry_release_member WHERE import_id=?",
                [result["import_id"]],
            ).fetchall()
        )
        assert all(new_members[number] == member for number, member in old_members if number != 2)
        assert new_members[2] != dict(old_members)[2]  # Native member hash includes framing.
        assert store.original_path(delivery).read_bytes() == variant.read_bytes()


@pytest.mark.parametrize(
    "payload,reason",
    [
        (native(137, (1, b"28100000001"), (12, b"000042"))[:-3], "native_record_width"),
        (
            native(137, (1, b"28100000001"), (12, b"000042"))[:-2] + b"\n",
            "native_record_terminator",
        ),
        (native(137, (1, b"28100000001"), (12, b"000042")), "native_variant_not_present"),
    ],
)
def test_variant_fails_closed_on_short_bare_lf_and_absent_variant(
    tmp_path: Path, payload: bytes, reason: str
) -> None:
    source = release_fixture(tmp_path / "bad.zip", source_changes={"lms02.txt": payload})
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(source, source="synthetic", receipt_key="bad")
        result = ingest_registry(
            store, delivery, phase="final", context={}, parser_version=MISSING_FINAL_CRLF_PROFILE
        )
        assert result["reason"] == reason
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)


def test_registry_retry_new_edition_and_exact_fields(tmp_path: Path) -> None:
    original = tmp_path / "original.zip"
    original.write_bytes(delivery_bytes())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(original, source="synthetic-registry", receipt_key="one")
        result = ingest_registry(store, delivery, phase="final", context={})
        assert result["status"] == "accepted"
        first = store._connection.execute("SELECT count(*) FROM registry_member").fetchone()
        assert ingest_registry(store, delivery, phase="final", context={})["retry"]
        second = store.register_source(original, source="synthetic-registry", receipt_key="two")
        other = ingest_registry(store, second, phase="final", context={})
        assert other["import_id"] != result["import_id"]
        assert store._connection.execute("SELECT count(*) FROM registry_member").fetchone() == first
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (2,)
        member = store._connection.execute(
            "SELECT member_id FROM registry_member WHERE member_number=3"
        ).fetchone()[0]
        rows = member_rows(store, member)
        assert rows[0]["package_number"] == "000007"
        assert rows[0]["aip_ore"] == 12345
        assert rows[0]["non_reimbursable_ore"] == 789
        assert audit_member(3, members()["lms03.txt"], rows)["complete"]


def test_changed_filename_keeps_prior_values(tmp_path: Path) -> None:
    path = tmp_path / "delivery.zip"
    path.write_bytes(delivery_bytes())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(path, source="synthetic", receipt_key="first")
        ingest_registry(store, delivery, phase="final", context={})
        changed = members()
        changed["lms03.txt"] = changed["lms03.txt"].replace(b"000012345", b"000012346")
        path.write_bytes(delivery_bytes(changed))
        with pytest.raises(ValueError):
            store.register_source(path, source="synthetic", receipt_key="first")
        new = store.register_source(path, source="synthetic", receipt_key="revision")
        ingest_registry(store, new, phase="final", context={})
        assert store._connection.execute(
            "SELECT aip_ore FROM registry_lms03 ORDER BY aip_ore"
        ).fetchall() == [(12345,), (12346,)]
        assert store._connection.execute("SELECT count(*) FROM registry_lms02").fetchone() == (1,)


def test_partial_and_unknown_schema_do_not_publish(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(delivery_bytes()[:-20])
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(path, source="synthetic", receipt_key="broken")
        result = ingest_registry(store, delivery, phase="final", context={})
        assert result["status"] == "rejected"
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)


def test_missing_unknown_invalid_and_encoding_are_separate() -> None:
    raw = bytearray(members()["lms02.txt"][:-2])
    raw[103:111] = b"20230230"
    raw[111:119] = b"        "
    raw[119:127] = b"00000000"
    row = decode_row(2, bytes(raw))
    states = json.loads(row["field_states"])
    assert states["creation_date"] == "invalid"
    assert states["last_price_change_date"] == "blank"
    assert states["exit_date"] == "unknown_date"
    assert (
        decode_row(10, b"28101234567" + b"20260101" + b"x" * 49 + b"\xf3")["previous_name"] is None
    )
    valid = decode_row(2, members()["lms02.txt"][:-2])
    assert valid["creation_date"] == date(2000, 12, 31)


def test_transaction_failure_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bef_redbird.foundation import lms_registry

    path = tmp_path / "delivery.zip"
    path.write_bytes(delivery_bytes())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(path, source="synthetic", receipt_key="first")
        original = lms_registry._insert_member
        count = 0

        def fail(*args: object, **kwargs: object) -> int:
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("synthetic interrupted publication")
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(lms_registry, "_insert_member", fail)
        with pytest.raises(OSError):
            ingest_registry(store, delivery, phase="final", context={})
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)
        monkeypatch.setattr(lms_registry, "_insert_member", original)
        assert ingest_registry(store, delivery, phase="final", context={})["status"] == "accepted"


def test_cached_proof_does_not_hide_typed_corruption(tmp_path: Path) -> None:
    path = tmp_path / "native.zip"
    path.write_bytes(delivery_bytes())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(path, source="synthetic", receipt_key="first")
        imported = ingest_registry(store, delivery, phase="final", context={})["import_id"]
        proof = verify_registry(store, imported, recheck=True, checked_members=set())
        assert proof["complete"]
        store._connection.execute("UPDATE registry_lms03 SET aip_ore=aip_ore+1")
        with pytest.raises(RegistryError, match="independent_typed_reconciliation_failed"):
            verify_registry(store, imported, recheck=True, checked_members=set())


def test_native_lineage_and_accepted_counts_are_verified(tmp_path: Path) -> None:
    path = tmp_path / "native.zip"
    path.write_bytes(delivery_bytes())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(path, source="synthetic", receipt_key="first")
        imported = ingest_registry(store, delivery, phase="final", context={})["import_id"]
        assert verify_registry(store, imported)["complete"]
        store._connection.execute(
            "UPDATE import_batch SET interpretation_context='{}' WHERE import_id=?", [imported]
        )
        with pytest.raises(RegistryError, match="independent_import_lineage_mismatch"):
            verify_registry(store, imported)


def test_auditor_identity_is_bound_to_loaded_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation import lms_registry_audit

    path = tmp_path / "native.zip"
    path.write_bytes(delivery_bytes())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(path, source="synthetic", receipt_key="first")
        imported = ingest_registry(store, delivery, phase="final", context={})["import_id"]
        proof = verify_registry(store, imported, recheck=True)
        modified_source = tmp_path / "changed_auditor.py"
        modified_source.write_text("# different implementation, not loaded\n")
        monkeypatch.setattr(lms_registry_audit, "__file__", str(modified_source))
        assert verify_registry(store, imported, recheck=True) == proof
        assert store._connection.execute(
            "SELECT auditor_version FROM registry_release_verification WHERE import_id=?",
            [imported],
        ).fetchall() == [(lms_registry_audit.AUDITOR_VERSION,)]
