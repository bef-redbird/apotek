"""Synthetic cold v2 recovery checks with independently hashed native bytes."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from bef_redbird.foundation import FoundationError, Store, migrations, recovery
from bef_redbird.foundation.locking import writer_lock
from bef_redbird.foundation.records import WriterBusy


@pytest.fixture
def cold(tmp_path: Path) -> tuple[Path, Path, Path]:
    database, archive, backup = tmp_path / "store.duckdb", tmp_path / "archive", tmp_path / "cold"
    database.write_bytes(b"synthetic native database\0" * 10000)
    (archive / "sha256" / "aa").mkdir(parents=True)
    (archive / "sha256" / "aa" / "original.zip").write_bytes(bytes(range(256)) * 20)
    (archive / ".writer").write_bytes(b"lock inode")
    return database, archive, backup


def hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".writer"
    }


def read_manifest(backup: Path) -> dict[str, Any]:
    return json.loads((backup / "backup.json").read_text(encoding="utf-8"))


def write_manifest(backup: Path, manifest: dict[str, Any]) -> None:
    (backup / "backup.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_compressed_roundtrip_and_v1_compatibility(
    cold: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    database, archive, backup = cold
    original_bytes = database.read_bytes()
    original_files = hashes(archive)
    recovery.backup_store_compressed(database, archive, backup)
    manifest = read_manifest(backup)
    assert manifest["version"] == 2
    assert not (backup / "database.duckdb").exists()
    blob = backup / "database.duckdb.gz"
    assert blob.stat().st_size < len(original_bytes)
    assert gzip.decompress(blob.read_bytes()) == original_bytes
    assert manifest["files"]["database.duckdb"] == hashlib.sha256(original_bytes).hexdigest()
    assert manifest["database_blob"]["sha256"] == hashlib.sha256(blob.read_bytes()).hexdigest()
    assert manifest["database_blob"]["size"] == blob.stat().st_size
    assert manifest["database_blob"]["logical_size"] == len(original_bytes)
    assert hashes(backup / "originals") == original_files
    before = hashes(backup)
    restored, restored_archive = tmp_path / "restored.duckdb", tmp_path / "restored-originals"
    recovery.restore_store_compressed(backup, restored, restored_archive)
    assert restored.read_bytes() == original_bytes
    assert hashes(restored_archive) == original_files
    assert hashes(backup) == before
    assert database.read_bytes() == original_bytes
    assert hashes(archive) == original_files
    # The established APIs keep the v1 contract and reject v2 explicitly.
    with pytest.raises(ValueError, match="Invalid backup manifest"):
        recovery.restore_store(backup, tmp_path / "wrong.duckdb", tmp_path / "wrong-originals")
    v1 = tmp_path / "v1"
    recovery.backup_store(database, archive, v1)
    assert read_manifest(v1)["version"] == 1
    recovery.restore_store(v1, tmp_path / "v1.duckdb", tmp_path / "v1-originals")
    assert (tmp_path / "v1.duckdb").read_bytes() == original_bytes
    with pytest.raises(ValueError, match="Invalid compressed backup manifest"):
        recovery.restore_store_compressed(v1, tmp_path / "wrong2.duckdb", tmp_path / "wrong2")


@pytest.mark.parametrize("damage", ["blob", "truncated", "original", "missing", "extra"])
def test_physical_corruption_rejected_before_restore(
    cold: tuple[Path, Path, Path], tmp_path: Path, damage: str
) -> None:
    database, archive, backup = cold
    recovery.backup_store_compressed(database, archive, backup)
    blob = backup / "database.duckdb.gz"
    if damage == "blob":
        data = bytearray(blob.read_bytes())
        data[len(data) // 2] ^= 0xFF
        blob.write_bytes(data)
    elif damage == "truncated":
        blob.write_bytes(blob.read_bytes()[:-4])
    elif damage == "original":
        (backup / "originals" / "sha256" / "aa" / "original.zip").write_bytes(b"changed")
    elif damage == "missing":
        blob.unlink()
    else:
        (backup / "unexpected").write_bytes(b"untracked")
    with pytest.raises(FoundationError, match="byte verification"):
        recovery.restore_store_compressed(backup, tmp_path / "out.duckdb", tmp_path / "out")
    assert not (tmp_path / "out.duckdb").exists()
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("damage", ["logical_hash", "logical_size", "bad_gzip", "truncated"])
def test_logical_verification_is_independent_of_blob_hash(
    cold: tuple[Path, Path, Path], tmp_path: Path, damage: str
) -> None:
    database, archive, backup = cold
    recovery.backup_store_compressed(database, archive, backup)
    manifest = read_manifest(backup)
    blob = backup / "database.duckdb.gz"
    if damage == "logical_hash":
        manifest["files"]["database.duckdb"] = "0" * 64
    elif damage == "logical_size":
        manifest["database_blob"]["logical_size"] -= 1
    elif damage == "bad_gzip":
        blob.write_bytes(b"not a gzip stream")
    else:
        blob.write_bytes(blob.read_bytes()[:-4])
    manifest["database_blob"]["sha256"] = hashlib.sha256(blob.read_bytes()).hexdigest()
    manifest["database_blob"]["size"] = blob.stat().st_size
    write_manifest(backup, manifest)
    with pytest.raises(FoundationError, match="Compressed database"):
        recovery.restore_store_compressed(backup, tmp_path / "out.duckdb", tmp_path / "out")
    assert not (tmp_path / "out.duckdb").exists()
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "member",
    [
        "../escape",
        "originals/../../escape",
        "originals/../escape",
        "originals/C:/escape",
        "originals\\escape",
        "/originals/escape",
        "originals//escape",
        "originals/NUL",
        "originals/escape.",
        "originals/.writer",
    ],
)
def test_manifest_members_cannot_escape_or_alias_paths(
    cold: tuple[Path, Path, Path], tmp_path: Path, member: str
) -> None:
    database, archive, backup = cold
    recovery.backup_store_compressed(database, archive, backup)
    manifest = read_manifest(backup)
    manifest["files"][member] = "0" * 64
    write_manifest(backup, manifest)
    with pytest.raises(ValueError, match="Invalid compressed backup member"):
        recovery.restore_store_compressed(backup, tmp_path / "out.duckdb", tmp_path / "out")
    assert not (tmp_path / "out.duckdb").exists()


def test_locks_wal_overlap_and_no_overwrite(cold: tuple[Path, Path, Path], tmp_path: Path) -> None:
    database, archive, backup = cold
    for lock in (database.with_name(database.name + ".writer"), archive / ".writer"):
        with writer_lock(lock), pytest.raises(WriterBusy):
            recovery.backup_store_compressed(database, archive, backup)
        assert not backup.exists()
    wal = database.with_name(database.name + ".wal")
    wal.write_bytes(b"pending")
    with pytest.raises(FoundationError, match="close the database"):
        recovery.backup_store_compressed(database, archive, backup)
    assert wal.read_bytes() == b"pending"
    wal.unlink()
    with pytest.raises(ValueError, match="overlaps"):
        recovery.backup_store_compressed(database, archive, archive / "nested")
    recovery.backup_store_compressed(database, archive, backup)
    before = hashes(backup)
    with pytest.raises(ValueError, match="new destination"):
        recovery.backup_store_compressed(database, archive, backup)
    for restored, originals in (
        (database, tmp_path / "new-originals"),
        (tmp_path / "new.duckdb", archive),
        (backup / "nested.duckdb", tmp_path / "new-originals"),
        (tmp_path / "new.duckdb", backup / "nested-originals"),
        (tmp_path / "new-originals" / "nested.duckdb", tmp_path / "new-originals"),
    ):
        with pytest.raises(ValueError):
            recovery.restore_store_compressed(backup, restored, originals)
    restored = tmp_path / "new.duckdb"
    with writer_lock(restored.with_name(restored.name + ".writer")), pytest.raises(WriterBusy):
        recovery.restore_store_compressed(backup, restored, tmp_path / "new-originals")
    assert not restored.exists()
    assert hashes(backup) == before


def test_linked_paths_and_members_rejected(cold: tuple[Path, Path, Path], tmp_path: Path) -> None:
    database, archive, backup = cold
    linked = archive / "linked"
    try:
        linked.symlink_to(database)
    except OSError:
        pytest.skip("Host does not permit synthetic symbolic links")
    with pytest.raises(FoundationError, match="linked members"):
        recovery.backup_store_compressed(database, archive, backup)
    linked.unlink()
    alias = tmp_path / "alias"
    alias.symlink_to(archive, target_is_directory=True)
    with pytest.raises(ValueError, match="linked paths"):
        recovery.backup_store_compressed(database, alias, backup)
    recovery.backup_store_compressed(database, archive, backup)
    (backup / "originals" / "linked").symlink_to(database)
    with pytest.raises(FoundationError, match="linked members"):
        recovery.restore_store_compressed(backup, tmp_path / "out.duckdb", tmp_path / "out")


def test_failed_backup_has_no_complete_manifest(
    cold: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, archive, backup = cold
    monkeypatch.setattr(recovery, "_gzip_database", lambda *args: "0" * 64)
    with pytest.raises(FoundationError, match="logical hash"):
        recovery.backup_store_compressed(database, archive, backup)
    assert not (backup / "backup.json").exists()
    assert database.read_bytes() == b"synthetic native database\0" * 10000


def test_failed_manifest_write_does_not_publish_completion(
    cold: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, archive, backup = cold

    def fail_manifest(value: Any, stream: Any, **kwargs: Any) -> None:
        stream.write('{"version":')
        raise OSError("synthetic interrupted manifest write")

    monkeypatch.setattr(recovery.json, "dump", fail_manifest)
    with pytest.raises(OSError, match="interrupted"):
        recovery.backup_store_compressed(database, archive, backup)
    assert not (backup / "backup.json").exists()
    assert (backup / "backup.json.part").exists()


@pytest.mark.parametrize("damage", ["database", "original"])
def test_restore_verifies_actual_output_bytes(
    cold: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    database, archive, backup = cold
    recovery.backup_store_compressed(database, archive, backup)
    before = hashes(backup)
    if damage == "database":
        decompress = recovery._gzip_database

        def damage_database(blob: Path, size: int, target: Path | None = None) -> str:
            digest = decompress(blob, size, target)
            if target is not None:
                target.write_bytes(b"damaged after streaming hash")
            return digest

        monkeypatch.setattr(recovery, "_gzip_database", damage_database)
    else:
        copy = recovery.shutil.copyfileobj

        def damage_original(source: Any, target: Any, length: int = 0) -> None:
            copy(source, target, length)
            target.write(b"damaged after copy")

        monkeypatch.setattr(recovery.shutil, "copyfileobj", damage_original)
    with pytest.raises(FoundationError, match="Restored byte verification"):
        recovery.restore_store_compressed(backup, tmp_path / "out.duckdb", tmp_path / "out")
    assert hashes(backup) == before


def test_git_storage_exclusion(cold: tuple[Path, Path, Path], tmp_path: Path) -> None:
    database, archive, backup = cold
    repository = tmp_path / "synthetic-repo"
    (repository / ".git").mkdir(parents=True)
    forbidden = repository / "private"
    with pytest.raises(ValueError, match="outside Git"):
        recovery.backup_store_compressed(database, archive, forbidden)
    recovery.backup_store_compressed(database, archive, backup)
    with pytest.raises(ValueError, match="outside Git"):
        recovery.restore_store_compressed(backup, forbidden / "db.duckdb", tmp_path / "out")
    assert not forbidden.exists()


@pytest.mark.parametrize("upgrade", [False, True])
def test_store_ids_originals_and_pending_migrations_preserved_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upgrade: bool
) -> None:
    database, archive, backup = tmp_path / "store.duckdb", tmp_path / "archive", tmp_path / "cold"
    source = tmp_path / "source.txt"
    source.write_bytes(b"independent synthetic source\r\n\x00\xff")
    full = migrations.MIGRATIONS
    with monkeypatch.context() as initial:
        if upgrade:
            initial.setattr(migrations, "MIGRATIONS", full[:1])
        with Store(database, archive) as store:
            package = store.create_package()
            delivery = store.register_source(source, source="synthetic", receipt_key="original")
            batch = store.begin_import(
                delivery,
                parser_version="synthetic",
                schema_version="synthetic",
                mapping_version="v1",
            )
            store.finish_import(
                batch, status="accepted", record_count=0, accepted_count=0, rejected_count=0
            )
    original_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    restored, originals = tmp_path / "restored.duckdb", tmp_path / "restored-originals"
    with monkeypatch.context() as closed:

        def forbidden(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("Cold recovery must not open DuckDB")

        closed.setattr(duckdb, "connect", forbidden)
        recovery.backup_store_compressed(database, archive, backup)
        recovery.restore_store_compressed(backup, restored, originals)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == original_hash
    with duckdb.connect(str(restored), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM schema_migration").fetchone() == (
            1 if upgrade else len(full),
        )
    with Store(restored, originals) as store:
        assert str(store.get_record("package", package)["package_id"]) == package
        assert str(store.get_record("source_delivery", delivery)["delivery_id"]) == delivery
        assert str(store.get_record("import_batch", batch)["import_id"]) == batch
        assert store.original_path(delivery).read_bytes() == source.read_bytes()
        assert store._connection.execute("SELECT count(*) FROM schema_migration").fetchone() == (
            len(full),
        )
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original_hash
