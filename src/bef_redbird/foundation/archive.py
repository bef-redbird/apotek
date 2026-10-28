"""Immutable content-addressed originals; publication precedes database registration."""

from __future__ import annotations

import os
import re
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from .records import ArchiveError


def external_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise ValueError("Foundation storage and source files must be outside Git worktrees")
    return resolved


def archive_path(root: Path, content_hash: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise ArchiveError("Invalid content hash")
    path = root / "sha256" / content_hash[:2] / content_hash
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ArchiveError("Archive object escapes configured root")
    return path


def verify(path: Path, content_hash: str, byte_count: int) -> None:
    try:
        actual = sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual.update(chunk)
                size += len(chunk)
        if actual.hexdigest() != content_hash or size != byte_count:
            raise ArchiveError("Corrupt archive object")
    except FileNotFoundError:
        raise ArchiveError("Missing archive object") from None
    except OSError:
        raise ArchiveError("Unreadable archive object") from None


def archive_file(root: Path, source: Path) -> tuple[str, str, int]:
    source = external_path(source)
    staging = root / ".staging"
    if not staging.resolve().is_relative_to(root):
        raise ArchiveError("Archive staging escapes configured root")
    staging.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        hasher = sha256()
        size = 0
        with (
            source.open("rb") as original,
            NamedTemporaryFile(dir=staging, suffix=".part", delete=False) as output,
        ):
            temporary = Path(output.name)
            while chunk := original.read(1024 * 1024):
                output.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        content_hash = hasher.hexdigest()
        destination = archive_path(root, content_hash)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Hard-link publication is atomic and cannot replace an existing object.
            # No copy fallback: an unsupported filesystem must fail before DB insertion.
            os.link(temporary, destination)
        except FileExistsError:
            pass
        verify(destination, content_hash, size)
        return content_hash, destination.relative_to(root).as_posix(), size
    except OSError:
        raise ArchiveError("Source archiving failed; original was not modified") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
