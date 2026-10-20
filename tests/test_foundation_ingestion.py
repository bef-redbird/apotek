"""F2 end-to-end synthetic acceptance: fixed expected values, atomicity and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from datetime import date, timedelta

import pytest
from openpyxl import Workbook

from bef_redbird.foundation import (
    ArchiveError,
    FoundationError,
    Store,
    WriterBusy,
    ingestion,
    migrations,
)
from bef_redbird.foundation.__main__ import main
from bef_redbird.foundation.adapters.base import Assertion, Parsed, money
from bef_redbird.foundation.recovery import backup_store, restore_store

CURRENT = date(2026, 1, 5)
NEXT = date(2026, 1, 19)
OPTIONS = {
    "price_type": "aip",
    "currency": "DKK",
    "price_basis": "per_package",
    "tax_basis": "unknown",
    "fee_basis": "unknown",
    "classification_context": "ordinary",
    "group_namespace": "synthetic:group",
}


def cito_file(tmp_path, rows=None):
    path = tmp_path / "invented.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Table"
    sheet.append(["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"])
    for row in rows or [["000007", "", "1.234,56", "A", "C", "001"]]:
        sheet.append(row)
    book.save(path)
    return path


def register(store, path, key="one", **kwargs):
    store.add_period(CURRENT, NEXT)
    store.add_period(NEXT, NEXT + timedelta(days=14))
    return store.register_source(
        path,
        source="synthetic",
        receipt_key=key,
        current_period=CURRENT,
        target_period=NEXT,
        **kwargs,
    )


def ingest(store, delivery, **kwargs):
    return store.ingest(
        delivery,
        **(
            {
                "adapter": "cito",
                "version": "cito-workbook-v1",
                "mapping_version": "map-v1",
                "mapping_policy": "delivery",
                "context": OPTIONS,
            }
            | kwargs
        ),
    )


def test_full_import_and_identical_retry_preserve_exact_provenance(tmp_path):
    source = cito_file(tmp_path)
    original = source.read_bytes()
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source)
        summary = ingest(store, delivery)
        assert summary["validation_status"] == "accepted"
        assert summary["observations"] == {
            "price_observation": 1,
            "package_observation": 1,
            "classification_observation": 2,
            "supply_observation": 0,
            "lifecycle_event": 0,
        }
        batch = summary["import_id"]
        price = store.observations(batch, "price_observation")[0]
        assert price["amount_ore"] == 123456
        assert price["canonical_identifier"] == price["source_identifier"] == "000007"
        assert price["source_locator"] == "sheet:Table!C2"
        assert str(price["delivery_id"]) == delivery
        assert str(price["import_id"]) == batch
        mapping = store.get_record("package_identifier", str(price["mapping_id"]))
        assert str(mapping["package_id"]) == str(price["package_id"])
        assert mapping["scope_key"] == f"delivery:{delivery}"
        assert store.original_path(delivery).read_bytes() == original
        classes = store.observations(batch, "classification_observation")
        assert [(r["effective_period"], r["abc_code"], r["group_code"]) for r in classes] == [
            (CURRENT, "A", "001"),
            (NEXT, "C", None),
        ]
        assert ingest(store, delivery) == summary
        assert store.observations(batch, "price_observation") == [price]
        assert source.read_bytes() == original
        with pytest.raises(ValueError, match="immutable"):
            store.finish_import(
                batch, status="failed", record_count=4, accepted_count=4, rejected_count=0
            )


def test_new_context_parser_mapping_and_receipt_keep_alternatives(tmp_path, monkeypatch):
    source = cito_file(tmp_path)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source)
        first = ingest(store, delivery)
        original = store.observations(first["import_id"], "price_observation")
        changed = ingest(store, delivery, context=OPTIONS | {"tax_basis": "explicit_other"})
        mapped = ingest(store, delivery, mapping_version="map-v2")
        monkeypatch.setitem(ingestion.VERSIONS, "cito", "cito-workbook-v2")
        reparsed = ingest(store, delivery, version="cito-workbook-v2")
        other_delivery = register(store, source, key="two")
        received = ingest(store, other_delivery, version="cito-workbook-v2")
        assert len({r["import_id"] for r in [first, changed, mapped, reparsed, received]}) == 5
        assert store.observations(first["import_id"], "price_observation") == original
        assert store.original_path(delivery) == store.original_path(other_delivery)
        second_package = store.observations(received["import_id"], "price_observation")[0][
            "package_id"
        ]
        assert second_package != original[0]["package_id"]  # No unsupported cross-delivery link.


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0),
        ("0,01", 1),
        ("999,99", 99999),
        ("1.234,56", 123456),
        ("92233720368547758,07", 9223372036854775807),
    ],
)
def test_exact_money_boundaries(value, expected):
    assert money(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "-0,01",
        "123,",
        "12.34",
        "1,001",
        "1,000000000000000000000000000001",
        float("nan"),
        float("inf"),
        "92233720368547758,08",
        True,
    ],
)
def test_money_never_guesses_or_rounds(value):
    with pytest.raises(ValueError):
        money(value)


def test_conflicts_and_invalid_cells_keep_evidence_and_incomplete_coverage(tmp_path):
    source = cito_file(
        tmp_path,
        [
            ["7", "", "0", "A", "C", "001"],
            ["000007", "", "2,00", "B", "C", "001"],
            ["8", "", "12,", "A", "C", ""],
            ["9", "", None, "A", "C", ""],
        ],
    )
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source, coverage={"version": 1, "kind": "complete_snapshot"})
        result = ingest(store, delivery)
        prices = store.observations(result["import_id"], "price_observation")
        assert [r["amount_ore"] for r in prices] == [0, 200, None, None]
        assert [r["quality_state"] for r in prices[:2]] == ["conflicting", "conflicting"]
        assert json.loads(prices[2]["field_presence"])["amount_ore"] == "invalid"
        assert json.loads(prices[3]["field_presence"])["amount_ore"] == "blank"
        assert result["coverage_assessment"] == "incomplete"
        assert len({r["observation_id"] for r in prices}) == 4
        codes = {r["code"] for r in store.issues(result["import_id"])}
        assert "conflicting_assertions" in codes


@pytest.mark.parametrize(
    "coverage,code",
    [
        (
            {"version": 1, "kind": "complete_snapshot", "filter": "invented subset"},
            "filtered_snapshot_conflict",
        ),
        ({"version": 1, "kind": "changes", "record_count": 2}, "declared_record_count_conflict"),
        ({"version": 1, "kind": "changes", "family": "supply"}, "coverage_family_conflict"),
    ],
)
def test_context_coverage_conflicts_reject_publication(tmp_path, coverage, code):
    source = cito_file(tmp_path)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        result = ingest(store, register(store, source, coverage=coverage))
        assert result["validation_status"] == "rejected"
        assert sum(result["observations"].values()) == 0
        assert code in {r["code"] for r in result["issues"]}
        assert store._connection.execute("SELECT count(*) FROM package").fetchone() == (0,)


def test_unresolved_mapping_blocks_only_affected_assertions(tmp_path):
    source = cito_file(tmp_path)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source)
        result = ingest(store, delivery, mapping_policy="existing")
        assert result["validation_status"] == "rejected"
        assert result["rejected_count"] == 4
        assert result["issues"][0]["code"] == "unresolved_mapping"


def test_publication_error_rolls_back_every_assertion_and_can_resume(tmp_path, monkeypatch):
    source = cito_file(tmp_path)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source)
        publish = ingestion._publish

        def interrupted(*args, **kwargs):
            publish(*args, **kwargs)
            raise RuntimeError("injected interruption")

        with monkeypatch.context() as scoped:
            scoped.setattr(ingestion, "_publish", interrupted)
            with pytest.raises(RuntimeError):
                ingest(store, delivery)
        batch = str(store._connection.execute("SELECT import_id FROM import_batch").fetchone()[0])
        assert store.get_record("import_batch", batch)["validation_status"] == "running"
        assert store._connection.execute("SELECT count(*) FROM price_observation").fetchone() == (
            0,
        )
        assert store._connection.execute("SELECT count(*) FROM package_identifier").fetchone() == (
            0,
        )
        resumed = ingest(store, delivery)
        assert resumed["import_id"] == batch
        assert resumed["accepted_count"] == 4


def test_abrupt_process_exit_during_publication_resumes_safely(tmp_path):
    source = cito_file(tmp_path)
    database, archive = tmp_path / "store.duckdb", tmp_path / "archive"
    with Store(database, archive) as store:
        delivery = register(store, source)
    script = textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        from bef_redbird.foundation import Store, ingestion
        real = ingestion._publish
        def interrupted(*args, **kwargs):
            real(*args, **kwargs)
            os._exit(71)
        ingestion._publish = interrupted
        with Store(Path(sys.argv[1]), Path(sys.argv[2])) as store:
            store.ingest(sys.argv[3], adapter='cito', version='cito-workbook-v1',
                         mapping_version='map-v1', mapping_policy='delivery',
                         context=json.loads(sys.argv[4]))
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(database), str(archive), delivery, json.dumps(OPTIONS)],
        capture_output=True,
        timeout=60,
        env=os.environ.copy(),
    )
    assert completed.returncode == 71
    with Store(database, archive) as store:
        result = ingest(store, delivery)
        assert result["accepted_count"] == 4
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == (1,)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_archive_damage_blocks_read_and_identical_retry(tmp_path, damage):
    source = cito_file(tmp_path)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source)
        result = ingest(store, delivery)
        archived = store.original_path(delivery)
        if damage == "missing":
            archived.unlink()
        else:
            archived.write_bytes(b"synthetic damage")
        with pytest.raises(ArchiveError):
            store.observations(result["import_id"], "price_observation")
        with pytest.raises(ArchiveError):
            ingest(store, delivery)
        assert store.import_summary(result["import_id"])["accepted_count"] == 4


def test_mapping_evidence_archive_damage_blocks_import_and_use(tmp_path):
    source = cito_file(tmp_path)
    mapping_source = tmp_path / "identity-evidence.txt"
    mapping_source.write_text("independent synthetic identifier evidence")
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source)
        identity_delivery = store.register_source(
            mapping_source, source="synthetic-identity", receipt_key="one"
        )
        evidence = store.begin_import(
            identity_delivery,
            parser_version="identity-v1",
            schema_version="identity-v1",
            mapping_version="map-v1",
        )
        package = store.create_package()
        store.add_identifier(
            package,
            namespace="dk:varenummer",
            identifier_value="7",
            evidence_import_id=evidence,
            source_locator="text:line:1",
            mapping_version="map-v1",
            validity_kind="scope",
            scope_key="synthetic:edition",
        )
        store.finish_import(
            evidence, status="accepted", record_count=1, accepted_count=1, rejected_count=0
        )
        result = ingest(
            store, delivery, mapping_policy="existing", mapping_scope="synthetic:edition"
        )
        store.original_path(identity_delivery).unlink()
        with pytest.raises(ArchiveError):
            store.observations(result["import_id"], "price_observation")
        with pytest.raises(ArchiveError):
            ingest(store, delivery, mapping_policy="existing", mapping_scope="synthetic:edition")
        with pytest.raises(ArchiveError):
            ingest(
                store,
                delivery,
                mapping_policy="existing",
                mapping_scope="synthetic:edition",
                context=OPTIONS | {"tax_basis": "new-interpretation"},
            )
        counts = store._connection.execute(
            "SELECT validation_status, count(*) FROM import_batch GROUP BY validation_status"
        ).fetchall()
        assert dict(counts) == {"accepted": 2, "running": 1}
        assert store._connection.execute("SELECT count(*) FROM price_observation").fetchone() == (
            1,
        )
        # Re-registration restores a missing object without rewriting corrupt bytes.
        assert (
            store.register_source(mapping_source, source="synthetic-identity", receipt_key="one")
            == identity_delivery
        )
        resumed = ingest(
            store,
            delivery,
            mapping_policy="existing",
            mapping_scope="synthetic:edition",
            context=OPTIONS | {"tax_basis": "new-interpretation"},
        )
        assert resumed["validation_status"] == "accepted"


def test_f1_cold_backup_restore_and_upgrade_preserve_originals(tmp_path, monkeypatch):
    database, archive = tmp_path / "f1.duckdb", tmp_path / "originals"
    source = cito_file(tmp_path)
    with monkeypatch.context() as f1:
        f1.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:1])
        with Store(database, archive) as store:
            delivery = register(store, source)
            package = store.create_package()
            batch = store.begin_import(
                delivery, parser_version="f1", schema_version="f1", mapping_version="map"
            )
            store.finish_import(
                batch, status="accepted", record_count=0, accepted_count=0, rejected_count=0
            )
        backup = tmp_path / "cold-backup"
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        backup_store(database, archive, backup)
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before
        restored, restored_archive = tmp_path / "restored.duckdb", tmp_path / "restored-originals"
        restore_store(backup, restored, restored_archive)
        with Store(restored, restored_archive) as store:
            assert str(store.get_record("package", package)["package_id"]) == package
            assert store.original_path(delivery).read_bytes() == source.read_bytes()
            assert store._connection.execute(
                "SELECT count(*) FROM schema_migration"
            ).fetchone() == (1,)
    with Store(restored, restored_archive) as store:
        assert store._connection.execute("SELECT count(*) FROM schema_migration").fetchone() == (2,)
        assert str(store.get_record("package", package)["package_id"]) == package
        assert store.get_record("import_batch", batch)["parser_version"] == "f1"
        result = ingest(store, delivery)
        price = store.observations(result["import_id"], "price_observation")
    with Store(restored, restored_archive) as store:
        assert store.observations(result["import_id"], "price_observation") == price
    # Original remains a recoverable F1 store, never opened with F2 in this test.
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_backup_refuses_live_writer_and_restore_refuses_corruption(tmp_path):
    database, archive = tmp_path / "store.duckdb", tmp_path / "archive"
    with Store(database, archive):
        with pytest.raises(WriterBusy):
            backup_store(database, archive, tmp_path / "busy-backup")
    backup_store(database, archive, tmp_path / "backup")
    with pytest.raises(ValueError):
        restore_store(tmp_path / "backup", database, archive)
    (tmp_path / "backup" / "database.duckdb").write_bytes(b"broken copy")
    with pytest.raises(FoundationError):
        restore_store(tmp_path / "backup", tmp_path / "new.duckdb", tmp_path / "new-archive")
    assert not (tmp_path / "new.duckdb").exists()


def test_all_five_publication_types_with_independent_expected_values(tmp_path, monkeypatch):
    source = cito_file(tmp_path)
    expected = [
        Assertion(
            "price_observation",
            "sheet:S!A1",
            "7",
            {
                "price_type": "consumer",
                "amount_ore": 123,
                "currency": "DKK",
                "price_basis": "per_unit",
                "tax_basis": "included",
                "fee_basis": "unknown",
                "price_role": "reported_previous_price",
            },
            {"amount_ore": "present"},
            {"amount": "1,23"},
            report_period=CURRENT,
        ),
        Assertion(
            "package_observation",
            "sheet:S!row:2",
            "7",
            {"name": "Invented"},
            {"name": "present", "atc": "withdrawn"},
            {"name": "Invented"},
            effective_period=CURRENT,
        ),
        Assertion(
            "classification_observation",
            "sheet:S!A3",
            "7",
            {
                "classification_context": "dose",
                "abc_code": "B",
                "group_namespace": "test",
                "group_code": "01",
                "group_scope": CURRENT.isoformat(),
            },
            {"abc_code": "present"},
            {},
            effective_period=CURRENT,
        ),
        Assertion(
            "supply_observation",
            "sheet:S!A4",
            "7",
            {
                "observation_date": CURRENT,
                "wholesaler_namespace": "test",
                "wholesaler_identifier": "w",
                "reported_status": "reported_disruption",
            },
            {},
            {},
        ),
        Assertion(
            "lifecycle_event",
            "sheet:S!A5",
            "7",
            {
                "event_type": "deactivation",
                "original_source_code": "Stop",
                "interpretation_status": "established",
            },
            {},
            {},
            effective_date=date(2020, 1, 1),
        ),
        Assertion(
            "lifecycle_event",
            "sheet:S!A6",
            "7",
            {
                "event_type": "permanent_deletion",
                "original_source_code": "Delete",
                "interpretation_status": "established",
            },
            {},
            {},
            effective_date=CURRENT,
        ),
    ]
    monkeypatch.setattr(ingestion, "adapter_reader", lambda *_: lambda *_: Parsed(expected, [], 6))
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        result = ingest(store, register(store, source))
        assert list(result["observations"].values()) == [1, 1, 1, 1, 2]
        price = store.observations(result["import_id"], "price_observation")[0]
        assert price["effective_period"] is None
        assert price["amount_ore"] == 123
        events = store.observations(result["import_id"], "lifecycle_event")
        assert [(r["event_type"], r["effective_date"]) for r in events] == [
            ("deactivation", date(2020, 1, 1)),
            ("permanent_deletion", CURRENT),
        ]


def test_cli_initialization_import_summary_issues_and_retry_are_private_safe(tmp_path, capsys):
    source = cito_file(tmp_path)
    context = tmp_path / "context.json"
    context.write_text(json.dumps(OPTIONS))
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps({"current_period": CURRENT.isoformat(), "target_period": NEXT.isoformat()})
    )
    base = ["--database", str(tmp_path / "store.duckdb"), "--archive", str(tmp_path / "archive")]
    assert main([*base, "init"]) == 0
    assert main([*base, "period", CURRENT.isoformat(), NEXT.isoformat()]) == 0
    assert main([*base, "period", NEXT.isoformat(), "2026-02-02"]) == 0
    capsys.readouterr()
    command = [
        *base,
        "import",
        str(source),
        "--source",
        "synthetic",
        "--receipt-key",
        "one",
        "--metadata",
        str(metadata),
        "--adapter",
        "cito",
        "--version",
        "cito-workbook-v1",
        "--mapping-version",
        "map-v1",
        "--mapping-policy",
        "delivery",
        "--context",
        str(context),
    ]
    assert main(command) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert str(source) not in output and "000007" not in output
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out) == result
    destination = tmp_path / "issues.json"
    assert main([*base, "issues", result["import_id"], "--output", str(destination)]) == 0
    assert destination.exists()
    capsys.readouterr()
    assert main([*base, "issues", result["import_id"], "--output", str(source)]) == 1
    assert source.read_bytes().startswith(b"PK")
