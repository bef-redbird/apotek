"""Manifest integrity, receipt identity and resumable archive operations."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation.corpus import (
    ingest_plan,
    load_inventory,
    load_plan,
    read_journal,
    reconcile_journal,
)
from tests.test_foundation_reports import intro_rows, workbook


def fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    root = tmp_path / "downloaded"
    root.mkdir()
    original = root / "invented.xlsx"
    original.write_bytes(workbook(intro_rows()))
    version = {
        "archive_relative_path": "invented.xlsx",
        "bytes": original.stat().st_size,
        "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "format": "xlsx",
        "original_filename": "invented.xlsx",
    }
    receipt = {
        **version,
        "item_key": "synthetic-item",
        "receipt_id": "synthetic-receipt-one",
        "received_at_utc": "2026-10-20T12:00:00+00:00",
    }
    manifest = {
        "schema_version": 1,
        "items": {
            "synthetic-item": {
                "status": "complete",
                "current_version": "invented.xlsx",
                "versions": [version],
                "receipts": [receipt],
                "identity": {"family": "introduced", "year": 2099},
                "source_url_sha256": "0" * 64,
            }
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, root, manifest


def plan_for(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "manifest_sha256": inventory["manifest_sha256"],
        "calendar": [
            {"start": "2026-01-05", "end": "2026-01-19", "evidence": "synthetic calendar"}
        ],
        "files": [
            {
                "archive_relative_path": "invented.xlsx",
                "receipt_id": "synthetic-receipt-one",
                "disposition": "ingest",
                "evidence": "synthetic labelled tariff and independently authored rows",
                "source": "synthetic-official",
                "adapter": "introductions",
                "parser_version": "introductions-workbook-v1",
                "metadata": {"current_period": "2026-01-05"},
                "context": {"tax_basis": "unknown", "fee_basis": "unknown"},
            }
        ],
    }


def test_checkpoint_loss_retry_receipt_addition_and_changed_parser(tmp_path: Path) -> None:
    manifest_path, root, manifest = fixture(tmp_path)
    inventory = load_inventory(manifest_path, root)
    plan = plan_for(inventory)
    database, archive, journal = (
        tmp_path / "foundation.duckdb",
        tmp_path / "originals",
        tmp_path / "journal.jsonl",
    )
    with Store(database, archive):
        pass
    with patch("bef_redbird.foundation.corpus._append", side_effect=OSError("interrupted")):
        with pytest.raises(OSError):
            ingest_plan(database, archive, inventory, plan, journal)
    assert not journal.exists()
    assert ingest_plan(database, archive, inventory, plan, journal)["processed"] == 1
    first = read_journal(journal)[0]
    assert ingest_plan(database, archive, inventory, plan, journal)["processed"] == 0

    # Another receipt of identical bytes is distinct evidence, sharing the one
    # explicitly chosen interpretation. Archive year is deliberately misleading.
    receipts = manifest["items"]["synthetic-item"]["receipts"]
    receipts.append({**receipts[0], "receipt_id": "synthetic-receipt-two"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    inventory = load_inventory(manifest_path, root)
    plan["manifest_sha256"] = inventory["manifest_sha256"]
    assert ingest_plan(database, archive, inventory, plan, journal)["processed"] == 1
    second = read_journal(journal)[1]
    assert first["import_id"] == second["import_id"]
    with duckdb.connect(str(database), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM source_delivery").fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM import_batch").fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM price_observation").fetchone() == (2,)
        assert (
            con.execute("SELECT min(effective_period) FROM price_observation")
            .fetchone()[0]
            .isoformat()
            == "2026-01-05"
        )
    proof = reconcile_journal(database, archive, inventory, journal, tmp_path / "proof")
    assert proof == {"processed": 1, "reused_reports": 0, "verified": 1, "failed": 0}
    assert (
        reconcile_journal(database, archive, inventory, journal, tmp_path / "proof")[
            "reused_reports"
        ]
        == 1
    )

    # Deliberately choose a new parser: the prior derivation remains immutable.
    plan["files"][0]["parser_version"] = "introductions-workbook-v2"
    assert ingest_plan(database, archive, inventory, plan, journal)["processed"] == 1
    third = read_journal(journal)[2]
    assert third["import_id"] != first["import_id"]
    with duckdb.connect(str(database), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM import_batch").fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM price_observation").fetchone() == (4,)
    assert (
        reconcile_journal(database, archive, inventory, journal, tmp_path / "proof")["verified"]
        == 2
    )


def test_manifest_integrity_and_complete_dispositions(tmp_path: Path) -> None:
    path, root, _ = fixture(tmp_path)
    inventory = load_inventory(path, root)
    assert inventory["file_count"] == inventory["receipt_count"] == 1
    plan = plan_for(inventory)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert load_plan(plan_path, inventory) == plan
    missing = copy.deepcopy(plan)
    missing["files"] = []
    plan_path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValueError, match="every inventoried"):
        load_plan(plan_path, inventory)
    (root / "invented.xlsx").write_bytes(b"changed local evidence")
    with pytest.raises(ValueError, match="integrity"):
        load_inventory(path, root)


def test_torn_journal_is_visible_and_recoverable_with_a_new_journal(tmp_path: Path) -> None:
    path, root, _ = fixture(tmp_path)
    inventory = load_inventory(path, root)
    database, archive = tmp_path / "foundation.duckdb", tmp_path / "originals"
    journal = tmp_path / "journal.jsonl"
    with Store(database, archive):
        pass
    ingest_plan(database, archive, inventory, plan_for(inventory), journal)
    with journal.open("a", encoding="utf-8") as stream:
        stream.write('{"event":')
    with pytest.raises(json.JSONDecodeError):
        read_journal(journal)
    ingest_plan(database, archive, inventory, plan_for(inventory), tmp_path / "recovery.jsonl")
    with duckdb.connect(str(database), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM import_batch").fetchone() == (1,)


def test_empty_established_report_is_accepted_with_zero_observations(tmp_path: Path) -> None:
    path, root, manifest = fixture(tmp_path)
    original = root / "invented.xlsx"
    original.write_bytes(workbook(intro_rows()[:-1]))
    item = manifest["items"]["synthetic-item"]
    for record in [*item["versions"], *item["receipts"]]:
        record["bytes"] = original.stat().st_size
        record["sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    inventory = load_inventory(path, root)
    database, archive, journal = (
        tmp_path / "foundation.duckdb",
        tmp_path / "originals",
        tmp_path / "journal.jsonl",
    )
    with Store(database, archive):
        pass
    ingest_plan(database, archive, inventory, plan_for(inventory), journal)
    row = read_journal(journal)[0]
    assert row["validation_status"] == "accepted"
    assert row["accepted_count"] == 0
    assert (
        reconcile_journal(database, archive, inventory, journal, tmp_path / "proof")["verified"]
        == 1
    )


def test_orphan_acquisition_receipt_is_not_silently_omitted(tmp_path: Path) -> None:
    path, root, manifest = fixture(tmp_path)
    receipts = manifest["items"]["synthetic-item"]["receipts"]
    receipts.append({**receipts[0], "archive_relative_path": "unlisted.xlsx"})
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="no inventoried version"):
        load_inventory(path, root)


def test_audit_publication_interruption_retries_and_stale_proof_is_rejected(tmp_path: Path) -> None:
    path, root, _ = fixture(tmp_path)
    inventory = load_inventory(path, root)
    database, archive, journal = (
        tmp_path / "foundation.duckdb",
        tmp_path / "originals",
        tmp_path / "journal.jsonl",
    )
    with Store(database, archive):
        pass
    ingest_plan(database, archive, inventory, plan_for(inventory), journal)
    output = tmp_path / "proof"
    with patch("bef_redbird.foundation.corpus.os.link", side_effect=OSError("interrupted")):
        with pytest.raises(OSError):
            reconcile_journal(database, archive, inventory, journal, output)
    assert not list(output.glob("*.json.gz"))
    assert len(list(output.glob("*.partial"))) == 1
    assert reconcile_journal(database, archive, inventory, journal, output)["verified"] == 1
    # Simulate external store damage. Cached evidence must never silently bless it.
    with duckdb.connect(str(database)) as con:
        con.execute("UPDATE price_observation SET amount_ore = 999")
    with pytest.raises(ValueError, match="stale"):
        reconcile_journal(database, archive, inventory, journal, output)


def test_committed_large_wal_survives_exit_before_journal_checkpoint(tmp_path: Path) -> None:
    path, root, manifest = fixture(tmp_path)
    rows = intro_rows()
    template = rows.pop()
    for number in range(300):
        row = template.copy()
        row[3] = "Synthetic licence " + "x" * 27000
        row[5] = str(number).zfill(6)
        row[10] = "90.071.992.547.409,93"
        rows.append(row)
    original = root / "invented.xlsx"
    original.write_bytes(workbook(rows))
    item = manifest["items"]["synthetic-item"]
    for record in [*item["versions"], *item["receipts"]]:
        record["bytes"] = original.stat().st_size
        record["sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    inventory = load_inventory(path, root)
    plan = plan_for(inventory)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    database, archive, journal = (
        tmp_path / "foundation.duckdb",
        tmp_path / "originals",
        tmp_path / "journal.jsonl",
    )
    with Store(database, archive):
        pass
    # The source transaction commits before the external journal append. An
    # abrupt process exit at that boundary must recover from WAL, not lose or
    # duplicate the source assertions when the absent checkpoint is retried.
    script = textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        from bef_redbird.foundation import corpus
        args = list(map(Path, sys.argv[1:]))
        manifest, source, plan, database, archive, journal = args
        inventory = corpus.load_inventory(manifest, source)
        corpus._append = lambda *args: os._exit(73)
        corpus.ingest_plan(database, archive, inventory,
                           json.loads(plan.read_text(encoding="utf-8")), journal)
    """)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            *map(str, (path, root, plan_path, database, archive, journal)),
        ],
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 73
    assert not journal.exists()
    assert database.with_name(database.name + ".wal").stat().st_size > 16 * 1024 * 1024
    assert ingest_plan(database, archive, inventory, plan, journal)["processed"] == 1
    assert ingest_plan(database, archive, inventory, plan, journal)["processed"] == 0
    assert not database.with_name(database.name + ".wal").exists()
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM import_batch").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM package_identifier").fetchone() == (300,)
        assert connection.execute("SELECT count(*) FROM package_observation").fetchone() == (300,)
        assert connection.execute("SELECT count(*) FROM price_observation").fetchone() == (600,)
        assert connection.execute(
            "SELECT DISTINCT amount_ore FROM price_observation WHERE price_type='aip'"
        ).fetchall() == [(9007199254740993,)]
    assert (
        reconcile_journal(database, archive, inventory, journal, tmp_path / "proof")["verified"]
        == 1
    )
