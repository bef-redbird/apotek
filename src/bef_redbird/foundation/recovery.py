"""Verified cold copies, without opening or migrating the source database."""

from __future__ import annotations

import gzip
import json
import re
import shutil
import zlib
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath

from .archive import external_path
from .locking import writer_lock
from .records import FoundationError

_DATABASE_BLOB = "database.duckdb.gz"


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise FoundationError("Backup does not permit symbolic links")
        if path.is_file() and path.name != ".writer":
            files[path.relative_to(root).as_posix()] = _hash(path)
    return files


def backup_store(database: Path, archive: Path, destination: Path) -> None:
    """Require closed/checkpointed database; never migrate or overwrite any source."""
    database, archive, destination = map(external_path, (database, archive, destination))
    if not database.is_file() or not archive.is_dir() or destination.exists():
        raise ValueError("Backup needs an existing store and a new destination")
    if destination.is_relative_to(archive) or database.is_relative_to(destination):
        raise ValueError("Backup destination overlaps source storage")
    with ExitStack() as stack:
        stack.enter_context(writer_lock(database.with_name(database.name + ".writer")))
        stack.enter_context(writer_lock(archive / ".writer"))
        if database.with_name(database.name + ".wal").exists():
            raise FoundationError("Recover and close the database with matching code before backup")
        before = {"database.duckdb": _hash(database)}
        before.update({f"originals/{key}": value for key, value in _files(archive).items()})
        destination.mkdir()
        shutil.copy2(database, destination / "database.duckdb")
        shutil.copytree(
            archive, destination / "originals", ignore=shutil.ignore_patterns(".writer")
        )
        if _files(destination) != before:
            raise FoundationError("Backup byte verification failed; copy is not usable")
        (destination / "backup.json").write_text(
            json.dumps({"version": 1, "files": before}, sort_keys=True), encoding="utf-8"
        )


def restore_store(backup: Path, database: Path, archive: Path) -> None:
    """Verify every byte before copying to unused destinations. Does not run migrations."""
    backup, database, archive = map(external_path, (backup, database, archive))
    if database.exists() or archive.exists() or database.is_relative_to(archive):
        raise ValueError("Restore needs separate, unused database and archive destinations")
    if database.is_relative_to(backup) or archive.is_relative_to(backup):
        raise ValueError("Restore must not change its backup")
    manifest = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
    expected = manifest.get("files")
    if (
        manifest.get("version") != 1
        or not isinstance(expected, dict)
        or "database.duckdb" not in expected
    ):
        raise ValueError("Invalid backup manifest")
    for key in expected:
        path = PurePosixPath(key)
        if (
            path.is_absolute()
            or ".." in path.parts
            or (key != "database.duckdb" and not key.startswith("originals/"))
        ):
            raise ValueError("Invalid backup member")
    actual = _files(backup)
    actual.pop("backup.json", None)
    if actual != expected:
        raise FoundationError("Backup byte verification failed")
    database.parent.mkdir(parents=True, exist_ok=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(writer_lock(database.with_name(database.name + ".writer")))
        # Recheck after acquiring the database lock; never overwrite a competing restore.
        if database.exists() or archive.exists():
            raise ValueError("Restore destination already exists")
        with database.open("xb") as target, (backup / "database.duckdb").open("rb") as source:
            shutil.copyfileobj(source, target)
        shutil.copytree(backup / "originals", archive)
        if _hash(database) != expected["database.duckdb"] or {
            f"originals/{key}": value for key, value in _files(archive).items()
        } != {key: value for key, value in expected.items() if key.startswith("originals/")}:
            raise FoundationError("Restored byte verification failed")


def _private_path(path: Path) -> Path:
    # Check before resolve(), which would otherwise hide a symbolic-link ancestor.
    path = path.expanduser().absolute()
    if any(item.is_symlink() or item.is_junction() for item in (path, *path.parents)):
        raise ValueError("Compressed recovery does not permit linked paths")
    return external_path(path)


def _cold_files(root: Path) -> dict[str, str]:
    for path in root.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise FoundationError("Compressed recovery does not permit linked members")
    return _files(root)


def _gzip_database(blob: Path, size: int, target: Path | None = None) -> str:
    """Check the entire gzip stream, bounded by the pinned logical size."""
    digest = sha256()
    total = 0
    try:
        with ExitStack() as stack:
            source = stack.enter_context(gzip.open(blob, "rb"))
            output = stack.enter_context(target.open("xb")) if target is not None else None
            while chunk := source.read(min(1024 * 1024, size - total + 1)):
                total += len(chunk)
                if total > size:
                    raise FoundationError("Compressed database logical size mismatch")
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
    except (EOFError, OSError, zlib.error) as exc:
        raise FoundationError("Compressed database stream verification failed") from exc
    if total != size:
        raise FoundationError("Compressed database logical size mismatch")
    return digest.hexdigest()


def backup_store_compressed(database: Path, archive: Path, destination: Path) -> None:
    """Cold v2 backup: gzip only the database; publish its manifest after verification.

    Never opens DuckDB or applies migrations. A failed destination is incomplete
    and must not be reused. The v1 backup/restore API and format are unchanged.
    """
    database, archive, destination = map(_private_path, (database, archive, destination))
    if not database.is_file() or not archive.is_dir() or destination.exists():
        raise ValueError("Backup needs an existing store and a new destination")
    if (
        destination.is_relative_to(archive)
        or database.is_relative_to(destination)
        or database.is_relative_to(archive)
        or archive.is_relative_to(database)
    ):
        raise ValueError("Backup destination overlaps source storage")
    with ExitStack() as stack:
        stack.enter_context(writer_lock(database.with_name(database.name + ".writer")))
        stack.enter_context(writer_lock(archive / ".writer"))
        if database.with_name(database.name + ".wal").exists():
            raise FoundationError("Recover and close the database with matching code before backup")
        logical_size = database.stat().st_size
        expected = {"database.duckdb": _hash(database)}
        expected.update({f"originals/{key}": value for key, value in _cold_files(archive).items()})
        destination.mkdir()
        blob = destination / _DATABASE_BLOB
        with (
            database.open("rb") as source,
            blob.open("xb") as compressed,
            gzip.GzipFile(
                filename="", fileobj=compressed, mode="wb", compresslevel=1, mtime=0
            ) as gz,
        ):
            shutil.copyfileobj(source, gz, length=1024 * 1024)
        blob_hash = _hash(blob)
        if _gzip_database(blob, logical_size) != expected["database.duckdb"]:
            raise FoundationError("Compressed database logical hash mismatch")
        originals = destination / "originals"
        originals.mkdir()
        for key in expected:
            if key == "database.duckdb":
                continue
            relative = Path(*PurePosixPath(key).parts[1:])
            source_path = _private_path(archive / relative)
            target = originals / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source_path.open("rb") as source_stream, target.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
        physical = {key: value for key, value in expected.items() if key != "database.duckdb"}
        physical[_DATABASE_BLOB] = blob_hash
        if _cold_files(destination) != physical:
            raise FoundationError("Backup byte verification failed; copy is not usable")
        manifest = {
            "version": 2,
            "files": expected,
            "database_blob": {
                "path": _DATABASE_BLOB,
                "encoding": "gzip",
                "sha256": blob_hash,
                "size": blob.stat().st_size,
                "logical_size": logical_size,
            },
        }
        # Incomplete writes never acquire the completion marker. Hard-link
        # publication cannot overwrite a competing marker.
        pending = destination / "backup.json.part"
        with pending.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True)
        (destination / "backup.json").hardlink_to(pending)
        pending.unlink()


def restore_store_compressed(backup: Path, database: Path, archive: Path) -> None:
    """Verify a v2 backup and restore to unused private paths without migrations."""
    backup, database, archive = map(_private_path, (backup, database, archive))
    if database.exists() or archive.exists() or database.is_relative_to(archive):
        raise ValueError("Restore needs separate, unused database and archive destinations")
    if (
        database.is_relative_to(backup)
        or archive.is_relative_to(backup)
        or backup.is_relative_to(database)
        or backup.is_relative_to(archive)
        or archive.is_relative_to(database)
    ):
        raise ValueError("Restore must not change its backup")
    # Inspect links before reading even the manifest.
    actual = _cold_files(backup)
    manifest = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Invalid compressed backup manifest")
    expected = manifest.get("files")
    blob = manifest.get("database_blob")
    if (
        manifest.get("version") != 2
        or not isinstance(expected, dict)
        or "database.duckdb" not in expected
        or not isinstance(blob, dict)
        or blob.get("path") != _DATABASE_BLOB
        or blob.get("encoding") != "gzip"
        or type(blob.get("size")) is not int
        or blob["size"] < 0
        or type(blob.get("logical_size")) is not int
        or blob["logical_size"] < 0
        or not isinstance(blob.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", blob["sha256"])
    ):
        raise ValueError("Invalid compressed backup manifest")
    for key, digest in expected.items():
        if not isinstance(key, str):
            raise ValueError("Invalid compressed backup member")
        path = PurePosixPath(key)
        if (
            path.is_absolute()
            or path.as_posix() != key
            or ".." in path.parts
            or "\\" in key
            or ":" in key
            or any(
                part.endswith((".", " "))
                or PureWindowsPath(part).is_reserved()
                or any(ord(character) < 32 or character in '<>"|?*' for character in part)
                for part in path.parts
            )
            or (key != "database.duckdb" and not key.startswith("originals/"))
            or key == "originals/"
            or ".writer" in path.parts
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("Invalid compressed backup member")
    physical = {key: value for key, value in expected.items() if key != "database.duckdb"}
    physical[_DATABASE_BLOB] = blob["sha256"]
    actual.pop("backup.json", None)
    compressed = backup / _DATABASE_BLOB
    if actual != physical or compressed.stat().st_size != blob["size"]:
        raise FoundationError("Backup byte verification failed")
    if _gzip_database(compressed, blob["logical_size"]) != expected["database.duckdb"]:
        raise FoundationError("Compressed database logical hash mismatch")
    database.parent.mkdir(parents=True, exist_ok=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(writer_lock(database.with_name(database.name + ".writer")))
        if database.exists() or archive.exists():
            raise ValueError("Restore destination already exists")
        archive.mkdir()
        stack.enter_context(writer_lock(archive / ".writer"))
        if (
            _gzip_database(compressed, blob["logical_size"], database)
            != expected["database.duckdb"]
        ):
            raise FoundationError("Restored database logical hash mismatch")
        for key in expected:
            if key == "database.duckdb":
                continue
            relative = Path(*PurePosixPath(key).parts[1:])
            source = _private_path(backup / "originals" / relative)
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as source_stream, target.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
        if _hash(database) != expected["database.duckdb"] or {
            f"originals/{key}": value for key, value in _cold_files(archive).items()
        } != {key: value for key, value in expected.items() if key.startswith("originals/")}:
            raise FoundationError("Restored byte verification failed")
