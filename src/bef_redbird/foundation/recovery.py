"""Verified cold copies, without opening or migrating the source database."""

from __future__ import annotations

import json
import shutil
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path, PurePosixPath

from .archive import external_path
from .locking import writer_lock
from .records import FoundationError


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
