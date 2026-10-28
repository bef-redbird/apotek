"""Coherent immutable result publication, hash verification and exposure lineage."""

from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import importlib.resources
import json
import os
import platform
import zoneinfo
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .dataset import ResearchError, canonical, digest, fingerprint


def outside_git(path: Path) -> None:
    if any((ancestor / ".git").exists() for ancestor in (path.resolve(), *path.resolve().parents)):
        raise ResearchError("research_artifacts_must_be_outside_git")


def filepin(path: Path, role: str) -> dict[str, Any]:
    return {
        "path": path.name,
        "role": role,
        "sha256": digest(path),
        "size_bytes": path.stat().st_size,
    }


def write_json(path: Path, value: Any) -> None:
    with path.open("xb") as target:
        target.write(canonical(value) + b"\n")
        target.flush()
        os.fsync(target.fileno())


@contextmanager
def jsonlines(path: Path) -> Iterator[gzip.GzipFile]:
    # Empty filename and fixed mtime make gzip bytes independent of run path/time.
    with path.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            yield compressed
        raw.flush()
        os.fsync(raw.fileno())


def _dependency_lock(root: Path) -> dict[str, Any]:
    # Identify only this project's src layout; never pick an unrelated ancestor's
    # lockfile from a wheel/site-packages environment or disclose absolute paths.
    if root.parent.name == "bef_redbird" and root.parent.parent.name == "src":
        lock = root.parent.parent.parent / "uv.lock"
        if lock.is_file():
            return {"status": "present", "filename": "uv.lock", "sha256": digest(lock)}
    return {"status": "absent", "reason": "no_source_checkout_lock"}


def _timezone_version() -> dict[str, Any]:
    key = "Europe/Copenhagen"
    for directory in zoneinfo.TZPATH:
        source = Path(directory) / key
        if not source.is_file():
            continue
        # This is the same ordered system search used by ZoneInfo. Not every OS
        # installs its human-readable release marker; the actual TZif is pinned.
        version = None
        marker = Path(directory) / "tzdata.zi"
        if marker.is_file():
            with marker.open("rt", encoding="utf-8") as stream:
                first = stream.readline().strip()
            if first.startswith("# version "):
                version = first.removeprefix("# version ")
        return {
            "key": key,
            "source": "system_tzdb",
            "iana_version": version,
            "tzif_sha256": digest(source),
        }
    # Windows normally has no system TZPATH and uses the existing tzdata wheel.
    try:
        source_resource = importlib.resources.files("tzdata").joinpath("zoneinfo", *key.split("/"))
        tzif = source_resource.read_bytes()
        tzdata_module = importlib.import_module("tzdata")

        return {
            "key": key,
            "source": "tzdata_package",
            "iana_version": tzdata_module.IANA_VERSION,
            "package_version": importlib.metadata.version("tzdata"),
            "tzif_sha256": hashlib.sha256(tzif).hexdigest(),
        }
    except (ImportError, OSError) as exc:
        raise ResearchError("timezone_data_unavailable") from exc


def versions() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    packages = {name: importlib.metadata.version(name) for name in ("numpy", "bef-redbird")}
    try:
        packages["tzdata"] = importlib.metadata.version("tzdata")
    except importlib.metadata.PackageNotFoundError:
        pass  # The actual system timezone file/version is pinned separately.
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "numpy": packages["numpy"],
        "bef_redbird": packages["bef-redbird"],
        "packages": packages,
        "timezone_data": _timezone_version(),
        "dependency_lock": _dependency_lock(root),
        "code_files": [
            {"path": path.name, "sha256": digest(path)} for path in sorted(root.glob("*.py"))
        ],
    }


def append_exposure(path: Path, event: dict[str, Any]) -> str:
    """Append a hash-chained evaluation-lock event; never rewrite earlier exposure.

    Lock-file exclusion is fail-closed, not a service. A crash may leave a stale
    lock; an operator must investigate rather than have the evaluator remove it.
    """
    path = path.resolve()
    outside_git(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    try:
        lock_handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ResearchError("lineage_writer_busy_or_unclean_shutdown") from exc
    try:
        os.close(lock_handle)
        previous = "0" * 64
        count = 0
        if path.exists():
            with path.open("rb") as source:
                for line in source:
                    if not line.endswith(b"\n"):
                        raise ResearchError("partial_lineage_event")
                    try:
                        record = json.loads(line)
                        stated = record.pop("event_hash")
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        raise ResearchError("corrupt_lineage_event") from exc
                    if (
                        record.get("sequence") != count
                        or record.get("previous_event_hash") != previous
                        or fingerprint(record) != stated
                    ):
                        raise ResearchError("corrupt_lineage_chain")
                    previous = stated
                    count += 1
        record = {
            **event,
            "sequence": count,
            "previous_event_hash": previous,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        event_hash = fingerprint(record)
        with path.open("ab") as target:
            target.write(canonical({**record, "event_hash": event_hash}) + b"\n")
            target.flush()
            os.fsync(target.fileno())
        return event_hash
    finally:
        lock.unlink()


def verify_run(path: Path) -> dict[str, Any]:
    """Read-only integrity boundary used by dashboards. Never load model objects."""
    from .result_view import verify_run as verify_saved_run

    return verify_saved_run(path)


def replay_fingerprint(path: Path) -> str:
    return str(verify_run(path)["run_fingerprint"])
