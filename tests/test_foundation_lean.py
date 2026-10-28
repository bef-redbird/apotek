"""Lean copy preserves native values and checks its archival authority."""

import json
from pathlib import Path

import duckdb
import pytest

from bef_redbird.foundation import Store, lean
from bef_redbird.foundation.lean import exact_compare, file_hash, prepare, publish, write_json
from bef_redbird.foundation.registry_selection import publish as original_publish
from tests.test_foundation_registry_selection import configuration, release


def fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    db, archive = tmp_path / "old.duckdb", tmp_path / "originals"
    with Store(db, archive) as store:
        iid = release(store, tmp_path, "one", "2020-01-06")
        config = configuration(store, tmp_path, [iid], ["2007-12-03", "2020-01-06"])
        did = original_publish(store, config)["dataset_id"]
        # Unreferenced experimental receipt must not survive the live cutover.
        extra = tmp_path / "obsolete.txt"
        extra.write_text("old experiment")
        store.register_source(extra, source="obsolete", receipt_key="old")
    recovery = tmp_path / "recovery"
    write_json(recovery / "store/backup.json", {"files": {"database.duckdb": file_hash(db)}})
    write_json(
        recovery / "recovery-acceptance.json",
        {
            "complete": True,
            "store_byte_verification": True,
        },
    )
    return db, archive, recovery, did


def test_lean_copy_retry_and_new_publication_preserve_original(tmp_path: Path) -> None:
    db, archive, recovery, did = fixture(tmp_path)
    before = file_hash(db)
    dest, receipt = tmp_path / "lean.duckdb", tmp_path / "receipt.json"
    result = prepare(db, archive, dest, recovery, receipt, did)
    assert result["copy_complete"] and not result["complete"]
    assert (
        result["expected"]["source_delivery"]["before"]
        == result["expected"]["source_delivery"]["retained"] + 1
    )
    assert prepare(db, archive, dest, recovery, receipt, did)["copy_complete"]
    published = publish(receipt)
    assert published["complete"] and published["dataset_id"] != did
    assert file_hash(db) == before
    with duckdb.connect(str(dest), read_only=True) as con:
        assert con.execute("select count(*) from analytical_dataset").fetchone() == (1,)
        assert con.execute(
            "select count(*) from source_delivery where source='obsolete'"
        ).fetchone() == (0,)


def test_migration_rejects_source_changed_after_accepted_recovery(tmp_path: Path) -> None:
    db, archive, recovery, did = fixture(tmp_path)
    manifest = recovery / "store/backup.json"
    data = json.loads(manifest.read_text())
    data["files"]["database.duckdb"] = "0" * 64
    write_json(manifest, data)
    with pytest.raises(ValueError, match="accepted recovery"):
        prepare(db, archive, tmp_path / "lean.duckdb", recovery, tmp_path / "r.json", did)
    assert not (tmp_path / "lean.duckdb").exists()


def test_exact_comparison_detects_changed_nullable_and_integer_values(tmp_path: Path) -> None:
    source = tmp_path / "prior.duckdb"
    with duckdb.connect(str(source)) as con:
        con.execute("CREATE TABLE facts(id INTEGER, amount BIGINT, state VARCHAR)")
        con.execute("INSERT INTO facts VALUES (1,9007199254740993,NULL)")
    with duckdb.connect() as con:
        con.execute("ATTACH '" + str(source).replace("'", "''") + "' AS prior (READ_ONLY)")
        con.execute("CREATE TABLE facts AS SELECT * FROM prior.facts")
        assert exact_compare(con, "facts", "TRUE") == 1
        con.execute("UPDATE facts SET amount=amount+1")
        with pytest.raises(ValueError, match="values/provenance"):
            exact_compare(con, "facts", "TRUE")


def test_archive_cutover_retries_interruption_and_refuses_operator_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, archive, recovery, did = fixture(tmp_path)
    dest, receipt = tmp_path / "lean.duckdb", tmp_path / "receipt.json"
    prepare(db, archive, dest, recovery, receipt, did)
    publish(receipt)
    operations = tmp_path / "operations"
    config, policy = tmp_path / "config.json", tmp_path / "policy.json"
    new_policy = tmp_path / "new-policy.json"
    write_json(
        config,
        {
            "database": str(db),
            "archive_root": str(archive),
            "operation_root": str(operations),
            "registry_policy_config": str(policy),
        },
    )
    write_json(policy, {"legacy_imports": ["historical"], "supply_imports": []})
    write_json(new_policy, {"legacy_imports": [], "supply_imports": []})
    write_json(
        tmp_path / "source-review/registered-legacy-evidence.json",
        {"legacy_imports": ["historical"], "supply_imports": []},
    )
    write_json(operations / "acquisition-state.json", {"files": {}})
    write_json(operations / "publication-state.json", {"old": True})
    journal, target = tmp_path / "cutover.json", tmp_path / "sources"
    original_link = lean.os.link
    attempts = 0

    def fail_once(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("synthetic copy interruption")
        original_link(source, destination)

    monkeypatch.setattr(lean.os, "link", fail_once)
    with pytest.raises(OSError, match="interruption"):
        lean.cutover(receipt, config, new_policy, target, journal)
    assert json.loads(config.read_text())["database"] == str(db)
    monkeypatch.setattr(lean.os, "link", original_link)
    assert lean.cutover(receipt, config, new_policy, target, journal)["complete"]
    assert lean.cutover(receipt, config, new_policy, target, journal)["complete"]
    assert json.loads(config.read_text())["database"] == str(dest)
    assert json.loads(policy.read_text())["legacy_imports"] == []
    write_json(policy, {"operator_change": True})
    with pytest.raises(ValueError, match="changed during cutover"):
        lean.cutover(receipt, config, new_policy, target, journal)
