"""Read-only, checkpointed native FTP acquisition. No database writes.

Inventory is an observed directory catalogue, never publication/coverage evidence.
Explicit remote selection avoids downloading loose equivalents of every ZIP.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from defusedxml import ElementTree as ET

from .archive import archive_file, archive_path, external_path, verify
from .ftp_native import KIND, NativeConfig
from .ftp_native import configuration as native_configuration
from .records import FoundationError

NS = {"w": "http://winscp.net/schema/session/1.0"}
Runner = Callable[[dict[str, Any]], dict[str, Any]]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def same_modification(left: str | None, right: str | None) -> bool:
    """Compare server metadata without rewriting its original representation.

    Identical raw values (including missing values) keep the existing retry
    contract. Differing strings are equivalent only as exact aware instants;
    malformed/naive or unsupported sub-microsecond precision never proves that.
    Metadata equality still does not establish unchanged remote bytes.
    """
    if left == right:
        return True
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if any(re.search(r"[.,][0-9]{7,}", value) for value in (left, right)):
        return False  # datetime would silently truncate this precision.
    try:
        earlier, later = datetime.fromisoformat(left), datetime.fromisoformat(right)
        return (
            earlier.utcoffset() is not None and later.utcoffset() is not None and earlier == later
        )
    except ValueError:
        return False


def remote_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not (value == "/LMS" or value.startswith("/LMS/"))
        or re.search(r'[\x00-\x1f"%*?\\]', value)
        or any(p in {".", "..", ""} for p in value.split("/")[1:])
    ):
        raise ValueError("Invalid LMS remote locator")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        # A concurrent status reader can briefly deny replacement on Windows.
        # Keep the durable temporary file and the last accepted checkpoint intact.
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * 2**attempt)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class AcquisitionConfig:
    workspace: Path
    archive_root: Path
    operation_root: Path
    inventory_path: Path
    transport: NativeConfig | None = None
    max_attempts: int = 3
    retry_seconds: float = 2.0
    batch_size: int = 20
    discovery_batch_size: int = 100


def load_config(path: Path) -> AcquisitionConfig:
    raw = json.loads(external_path(path).read_text(encoding="utf-8-sig"))
    if raw.get("version", 1) != 1:
        raise ValueError("Unsupported maintenance configuration")

    def absolute(key: str, fallback: Any = None, *, private: bool = True) -> Path:
        value = raw.get(key, fallback)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError("Configuration requires absolute paths")
        return external_path(Path(value)) if private else Path(value).resolve()

    workspace = absolute("workspace", private=False)
    if (
        not (workspace / "src/bef_redbird").is_dir()
        and (workspace / "bef-redbird/src/bef_redbird").is_dir()
    ):
        workspace = workspace / "bef-redbird"
    if not (workspace / "src/bef_redbird").is_dir():
        raise ValueError("Workspace does not contain the application")
    operations = absolute("operation_root")
    archive = absolute("native_root", raw.get("archive_root", raw.get("archive")))
    inventory = absolute("inventory_path", str(operations / "inventory.json"))
    if archive.is_relative_to(operations) or operations.is_relative_to(archive):
        raise ValueError("Immutable archive must be separate from operation staging")
    attempts, delay = raw.get("max_attempts", 3), raw.get("retry_seconds", 2.0)
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise ValueError("Invalid bounded retry count")
    if type(delay) not in {int, float} or not 0 <= delay <= 60:
        raise ValueError("Invalid bounded retry delay")
    batch_size = raw.get("batch_size", 20)
    if type(batch_size) is not int or not 1 <= batch_size <= 50:
        raise ValueError("Invalid bounded FTP batch size")
    discovery_batch_size = raw.get("discovery_batch_size", 100)
    if type(discovery_batch_size) is not int or not 1 <= discovery_batch_size <= 100:
        raise ValueError("Invalid bounded discovery batch size")
    return AcquisitionConfig(
        workspace,
        archive,
        operations,
        inventory,
        native_configuration(raw.get("ftp_transport")),
        attempts,
        float(delay),
        batch_size,
        discovery_batch_size,
    )


@contextmanager
def acquisition_lock(config: AcquisitionConfig) -> Iterator[None]:
    """OS lock releases on process death; a retained lock file is not a stale lock."""
    config.operation_root.mkdir(parents=True, exist_ok=True)
    with (config.operation_root / "acquisition.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another acquisition process is active") from None
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_inventory(path: Path) -> dict[str, Any]:
    raw = json.loads(external_path(path).read_text(encoding="utf-8-sig"))
    if raw.get("version") != 1 or not isinstance(raw.get("listings"), list):
        raise ValueError("Unsupported FTP inventory")
    seen = set()
    for listing in raw["listings"]:
        directory = remote_path(listing["directory"])
        if directory in seen or not isinstance(listing.get("entries"), list):
            raise ValueError("Duplicate or malformed directory listing")
        seen.add(directory)
        members = set()
        for entry in listing["entries"]:
            name = entry["filename"]
            if not isinstance(name, str) or "/" in name:
                raise ValueError("Invalid directory member")
            remote_path(directory + "/" + name)
            if name in members:
                raise ValueError("Duplicate directory member")
            members.add(name)
            if entry.get("type") not in {"D", "-", "L"}:
                raise ValueError("Unsupported directory member type")
    return catalogue(raw["listings"], raw.get("inventory_cutoff_utc"), raw.get("adoptions", []))


def catalogue(
    listings: list[dict[str, Any]],
    cutoff: str | None,
    adoptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    indexed = {item["directory"]: item for item in listings}
    needed = {"/LMS"}
    for directory, listing in indexed.items():
        needed.update(
            directory + "/" + e["filename"] for e in listing["entries"] if e["type"] == "D"
        )
    pending = sorted(needed - indexed.keys())
    return {
        "version": 1,
        "listings": [indexed[k] for k in sorted(indexed)],
        "pending": pending,
        "complete": not pending,
        "inventory_cutoff_utc": cutoff,
        "adoptions": adoptions or [],
        "timestamp_scope": "Per-directory observation intervals; publication unknown",
    }


def parse_listings(xml_path: Path) -> list[dict[str, Any]]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    if root is None:
        raise ValueError("Empty native XML evidence")
    listings = []
    for listing in root.findall(".//w:ls", NS):
        result, destination = listing.find("w:result", NS), listing.find("w:destination", NS)
        if result is None or result.get("success") != "true" or destination is None:
            continue
        directory = remote_path(str(destination.get("value")).rstrip("/"))
        entries = []
        for item in listing.findall("w:files/w:file", NS):
            facts = {x.tag.split("}")[-1]: x.get("value") for x in item}
            if facts.get("filename") not in {".", ".."}:
                entries.append(facts)
        listings.append(
            {
                "directory": directory,
                "entries": entries,
                "evidence": str(xml_path),
                "observed_at": root.get("start"),
                "observation_completed_utc": root.get("end"),
            }
        )
    return listings


def native_runner(config: AcquisitionConfig) -> Runner:
    from .ftp_native import runner

    return runner(config)


def native_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Read persisted evidence, checking its exact requested operation identity."""
    evidence = external_path(Path(receipt["receipt_path"]))
    persisted = json.loads(evidence.read_text(encoding="utf-8-sig"))
    if (
        persisted.get("version") != 2
        or persisted.get("kind") != KIND
        or persisted.get("name") != evidence.parent.name
        or not re.fullmatch(r"ftp-[a-f0-9]{32}", persisted.get("name", ""))
        or persisted.get("name") != receipt.get("name")
        or persisted.get("operations") != receipt.get("operations")
        or persisted.get("results") != receipt.get("results")
        or {key: value for key, value in receipt.items() if key != "receipt_path"} != persisted
        or type(persisted.get("exit_code")) is not int
    ):
        raise ValueError("Native FTP receipt identity or operation binding differs")
    operations = persisted.get("operations")
    results = persisted.get("results")
    if not isinstance(operations, list) or not isinstance(results, list):
        raise ValueError("Invalid native FTP receipt operations")
    if len(results) > len(operations):
        raise ValueError("Unexpected native FTP operation results")
    for index, result in enumerate(results):
        operation = operations[index]
        if any(result.get(key) != value for key, value in operation.items()):
            raise ValueError("Native FTP result differs from its requested operation")
        validate_interval(result)
        if not (
            datetime.fromisoformat(persisted["started_utc"])
            <= datetime.fromisoformat(result["started_utc"])
            <= datetime.fromisoformat(result["completed_utc"])
            <= datetime.fromisoformat(persisted["completed_utc"])
        ):
            raise ValueError("Native operation interval escapes its acquisition batch")
    validate_interval(persisted)
    return persisted


def validate_interval(value: dict[str, Any]) -> None:
    stamps = [datetime.fromisoformat(value[key]) for key in ("started_utc", "completed_utc")]
    if any(stamp.utcoffset() is None for stamp in stamps) or stamps[1] < stamps[0]:
        raise ValueError("Acquisition interval must be aware and ordered")


def read_listings(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """Dispatch native evidence and historical XML without invoking old clients."""
    if receipt.get("kind") != KIND:
        return parse_listings(Path(receipt["receipt_path"]).parent / "operations.xml")
    persisted = native_receipt(receipt)
    evidence = external_path(Path(receipt["receipt_path"]))
    return [
        {
            "directory": remote_path(result["remote"]),
            "entries": result["entries"],
            "evidence": str(evidence),
            "observed_at": result["started_utc"],
            "observation_completed_utc": result["completed_utc"],
        }
        for result in persisted["results"]
        if result["kind"] == "list" and result["success"] is True
    ]


def attempt(
    config: AcquisitionConfig,
    operations: list[dict[str, Any]],
    runner: Runner,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    for number in range(config.max_attempts):
        name = "ftp-" + uuid4().hex
        requested = [dict(op) for op in operations]
        for op in requested:
            if op["kind"] == "get":
                op["local"] = str(config.operation_root / "downloads" / (name + ".part"))
        request = {"name": name, "operations": requested}
        try:
            result = runner(request)
            if result.get("exit_code") == 0:
                result["operations"] = requested
                return result
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
        if number + 1 < config.max_attempts:
            sleeper(min(60.0, config.retry_seconds * 2**number))
    raise RuntimeError("FTP operation failed after bounded retries; private evidence retained")


def discover(
    config: AcquisitionConfig,
    *,
    adopt_inventory: Path | None = None,
    runner: Runner | None = None,
    refresh: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    with acquisition_lock(config):
        previous = (
            load_inventory(config.inventory_path) if config.inventory_path.is_file() else None
        )
        if cancelled is not None and cancelled():
            return {**(previous or catalogue([], None)), "cancelled": True}
        if adopt_inventory is not None:
            inventory = load_inventory(adopt_inventory)
            inventory["adoptions"].append(
                {
                    "path": str(external_path(adopt_inventory)),
                    "sha256": hashlib.sha256(adopt_inventory.read_bytes()).hexdigest(),
                    "adopted_utc": utc_now(),
                }
            )
        elif previous is not None and (not refresh or not previous["complete"]):
            # An interrupted refresh resumes its own checkpoint, never clears it again.
            inventory = previous
        else:
            inventory = catalogue([], None)
        if previous is not None and inventory is not previous:
            original = config.inventory_path.read_bytes()
            sha = hashlib.sha256(original).hexdigest()
            snapshot = config.operation_root / "inventory-history" / (sha + ".json")
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            try:
                with snapshot.open("xb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if snapshot.read_bytes() != original:
                    raise ValueError("Prior inventory snapshot integrity mismatch") from None
            inventory["adoptions"].append(
                {
                    "path": str(snapshot),
                    "sha256": sha,
                    "archived_utc": utc_now(),
                    "scope": "prior inventory; original cutoff preserved",
                }
            )
        # Checkpoint adopted work before any network action. Resume never re-lists successes.
        write_json(config.inventory_path, inventory)
        transport = runner or native_runner(config)
        failed: set[str] = set()
        while candidates := [d for d in inventory["pending"] if d not in failed]:
            remaining = candidates[: config.discovery_batch_size]
            for number in range(config.max_attempts):
                if cancelled is not None and cancelled():
                    return {**inventory, "cancelled": True}
                try:
                    result = transport(
                        {
                            "name": "ftp-" + uuid4().hex,
                            "operations": [{"kind": "list", "remote": d} for d in remaining],
                        }
                    )
                    listings = [x for x in read_listings(result) if x["directory"] in remaining]
                except (OSError, RuntimeError, KeyError, ET.ParseError, subprocess.TimeoutExpired):
                    listings = []
                if listings:
                    inventory = catalogue(
                        inventory["listings"] + listings, utc_now(), inventory["adoptions"]
                    )
                    write_json(config.inventory_path, inventory)
                    inventory = load_inventory(config.inventory_path)
                    remaining = [d for d in remaining if d in inventory["pending"]]
                if not remaining:
                    break
                if number + 1 < config.max_attempts:
                    time.sleep(min(60.0, config.retry_seconds * 2**number))
            failed.update(remaining)
        if inventory["pending"]:
            raise RuntimeError(
                "FTP discovery failed after bounded retries; successful listings checkpointed"
            )
        return inventory


def selected_files(config: AcquisitionConfig) -> list[dict[str, Any]]:
    inventory = load_inventory(config.inventory_path)
    files = []
    for listing in inventory["listings"]:
        for entry in listing["entries"]:
            if entry["type"] != "-":
                continue
            remote = listing["directory"] + "/" + entry["filename"]
            upper = remote.upper()
            kind = (
                "zip"
                if upper.endswith(".ZIP")
                else "documentation"
                if "/INFO/" in upper or "/DOK/" in upper
                else "preliminary"
                if "FOREL" in upper
                else "loose_or_supplement"
            )
            files.append(
                {
                    "remote": remote,
                    "size": int(entry["size"]),
                    "server_modification": entry.get("modification"),
                    "classification": kind,
                    "listing_evidence": listing.get("evidence"),
                }
            )
    return sorted(files, key=lambda x: (x["classification"] != "zip", x["remote"]))


def read_state(config: AcquisitionConfig) -> dict[str, Any]:
    path = config.operation_root / "acquisition-state.json"
    return (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"version": 1, "files": {}}
    )


def retain_download(
    config: AcquisitionConfig,
    remote: str,
    source: Path,
    receipt: dict[str, Any],
    state: dict[str, Any],
    *,
    indexed: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Adopt successful approved prior gets too; never invent receipt timestamps."""
    remote = remote_path(remote)
    files = indexed if indexed is not None else {x["remote"]: x for x in selected_files(config)}
    if remote not in files:
        raise ValueError("Acquisition requires an explicitly inventoried remote file")
    expected = files[remote]
    if type(receipt.get("exit_code")) is not int or not receipt.get("receipt_path"):
        raise ValueError("A native acquisition receipt is required")
    evidence = external_path(Path(receipt["receipt_path"]))
    if receipt.get("kind") == KIND:
        persisted = native_receipt(receipt)
        matches = [
            result
            for result in persisted["results"]
            if result["kind"] == "get" and result["remote"] == remote and result["success"] is True
        ]
        if (
            len(matches) != 1
            or Path(matches[0]["local"]).resolve() != source.resolve()
            or type(matches[0].get("bytes")) is not int
            or matches[0]["bytes"] != source.stat().st_size
        ):
            raise ValueError("Native download differs from its successful acquisition evidence")
        with source.open("rb") as downloaded:
            if matches[0].get("sha256") != hashlib.file_digest(downloaded, "sha256").hexdigest():
                raise ValueError("Native download hash differs from its acquisition evidence")
    else:
        tree = ET.parse(evidence.parent / "operations.xml")
        matches = []
        root = tree.getroot()
        if root is None:
            raise ValueError("Empty native XML evidence")
        for download in root.findall(".//w:download", NS):
            facts = {x.tag.split("}")[-1]: x.get("value") for x in download}
            result = download.find("w:result", NS)
            if (
                facts.get("filename") == remote
                and result is not None
                and result.get("success") == "true"
            ):
                matches.append(facts)
        if (
            len(matches) != 1
            or Path(str(matches[0].get("destination"))).resolve() != source.resolve()
            or int(str(matches[0].get("size"))) != source.stat().st_size
        ):
            raise ValueError("Native download does not match successful legacy XML evidence")
    for field in ("started_utc", "completed_utc"):
        stamp = datetime.fromisoformat(receipt[field])
        if stamp.utcoffset() is None:
            raise ValueError("Acquisition interval must be timezone aware")
    if datetime.fromisoformat(receipt["completed_utc"]) < datetime.fromisoformat(
        receipt["started_utc"]
    ):
        raise ValueError("Invalid acquisition interval")
    if source.stat().st_size != expected["size"]:
        raise ValueError("Partial or changed-size download is not accepted; refresh inventory")
    content_hash, relative, size = archive_file(config.archive_root, source)
    entry = state["files"].setdefault(remote, {"versions": [], "receipts": []})
    version = {
        "sha256": content_hash,
        "archive_relative_path": relative,
        "bytes": size,
        "original_filename": PurePosixPath(remote).name,
    }
    if content_hash not in {v["sha256"] for v in entry["versions"]}:
        entry["versions"].append(version)
    receipt_key = hashlib.sha256(
        (remote + "\n" + str(receipt["receipt_path"])).encode()
    ).hexdigest()
    if receipt_key not in {r["receipt_id"] for r in entry["receipts"]}:
        entry["receipts"].append(
            {
                **version,
                "receipt_id": receipt_key,
                "remote": remote,
                "started_utc": matches[0]["started_utc"]
                if receipt.get("kind") == KIND
                else receipt["started_utc"],
                "completed_utc": matches[0]["completed_utc"]
                if receipt.get("kind") == KIND
                else receipt["completed_utc"],
                "evidence": str(receipt["receipt_path"]),
                "server_modification": expected["server_modification"],
                "publication_utc": None,
                "batch_exit_code": receipt["exit_code"],
            }
        )
    entry.update(
        {
            "status": "retained",
            "current_sha256": content_hash,
            "inventory_size": expected["size"],
            "inventory_modification": expected["server_modification"],
        }
    )
    write_json(config.operation_root / "acquisition-state.json", state)
    return version


def acquire(
    config: AcquisitionConfig,
    remotes: Sequence[str],
    *,
    runner: Runner | None = None,
    recheck: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    with acquisition_lock(config):
        state, transport = read_state(config), runner or native_runner(config)
        indexed = {x["remote"]: x for x in selected_files(config)}
        pending = []
        for remote in dict.fromkeys(remotes):
            remote_path(remote)
            if remote not in indexed:
                raise ValueError("Selected remote was not inventoried")
            existing = state["files"].get(remote)
            expected = indexed[remote]
            if (
                existing
                and existing.get("status") == "retained"
                and not recheck
                and existing.get("inventory_size") == expected["size"]
                and same_modification(
                    existing.get("inventory_modification"), expected["server_modification"]
                )
            ):
                version = next(
                    v for v in existing["versions"] if v["sha256"] == existing["current_sha256"]
                )
                original = archive_path(config.archive_root, version["sha256"])
                if (
                    original.relative_to(config.archive_root).as_posix()
                    != version["archive_relative_path"]
                ):
                    raise ValueError("Invalid retained object path")
                verify(
                    original,
                    version["sha256"],
                    version["bytes"],
                )
                continue
            pending.append(remote)
        for start in range(0, len(pending), config.batch_size):
            remaining = pending[start : start + config.batch_size]
            for number in range(config.max_attempts):
                if (config.operation_root / "cancel-request.json").exists():
                    return state
                name = "ftp-" + uuid4().hex
                operations = [
                    {
                        "kind": "get",
                        "remote": remote,
                        "local": str(
                            config.operation_root
                            / "downloads"
                            / (name + "-" + str(index) + ".part")
                        ),
                    }
                    for index, remote in enumerate(remaining)
                ]
                try:
                    result = transport({"name": name, "operations": operations})
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    result = {}
                failed = []
                for operation in operations:
                    stage = Path(operation["local"])
                    try:
                        # A batch may abort after successful earlier gets. Only the native
                        # per-file success, exact destination and byte size permit retention.
                        retain_download(
                            config, operation["remote"], stage, result, state, indexed=indexed
                        )
                    except (ValueError, KeyError, OSError, ET.ParseError):
                        failed.append(operation["remote"])
                        continue
                    stage.unlink()
                remaining = failed
                if (config.operation_root / "cancel-request.json").exists():
                    return state
                if not remaining:
                    break
                if number + 1 < config.max_attempts:
                    sleeper(min(60.0, config.retry_seconds * 2**number))
            if remaining:
                raise RuntimeError(
                    "FTP acquisition failed after bounded retries; successful gets checkpointed"
                )
        return state


def adopt_downloads(config: AcquisitionConfig, manifest: Path) -> dict[str, Any]:
    """Explicit original/remote/receipt associations; prior originals stay in place."""
    raw = json.loads(external_path(manifest).read_text(encoding="utf-8-sig"))
    if raw.get("version") != 1 or not isinstance(raw.get("downloads"), list):
        raise ValueError("Unsupported prior-download adoption manifest")
    with acquisition_lock(config):
        state = read_state(config)
        indexed = {x["remote"]: x for x in selected_files(config)}
        for entry in raw["downloads"]:
            evidence = external_path(Path(entry["receipt_path"]))
            receipt = json.loads(evidence.read_text(encoding="utf-8-sig"))
            receipt["receipt_path"] = str(evidence)
            retain_download(
                config,
                entry["remote"],
                external_path(Path(entry["path"])),
                receipt,
                state,
                indexed=indexed,
            )
        return state


def acquisition_status(config: AcquisitionConfig) -> dict[str, Any]:
    inventory = load_inventory(config.inventory_path)
    state, files = read_state(config), selected_files(config)
    counts: Counter[str] = Counter()
    for item in files:
        retained = state["files"].get(item["remote"], {})
        status = "pending"
        if retained.get("status") == "retained":
            status = (
                "retained"
                if retained.get("inventory_size") == item["size"]
                and same_modification(
                    retained.get("inventory_modification"), item["server_modification"]
                )
                else "remote_metadata_changed"
            )
        counts[status] += 1
    return {
        "version": 1,
        "inventory_complete": inventory["complete"],
        "inventory_cutoff_utc": inventory["inventory_cutoff_utc"],
        "directory_count": len(inventory["listings"]),
        "pending_directories": len(inventory["pending"]),
        "file_count": len(files),
        "statuses": dict(counts),
        "classifications": dict(Counter(x["classification"] for x in files)),
        "publication_timing": "unknown",
        "metadata_unchanged_does_not_prove_bytes_unchanged": True,
        "status_integrity_scope": "Accepted retention checkpoints; status does not rehash objects",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["discover", "acquire", "status", "dry-run"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--remote", action="append", default=[])
    parser.add_argument("--adopt-inventory", type=Path)
    parser.add_argument("--adopt-downloads", type=Path)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--recheck", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "discover":
            discover(config, adopt_inventory=args.adopt_inventory, refresh=args.refresh)
        elif args.command == "acquire":
            if args.adopt_downloads:
                adopt_downloads(config, args.adopt_downloads)
            if not args.remote and not args.adopt_downloads:
                raise ValueError("Acquisition requires explicit remote selection")
            if args.remote:
                acquire(config, args.remote, recheck=args.recheck)
        result = acquisition_status(config)
        if args.command == "dry-run":
            selected = set(args.remote)
            if selected - {x["remote"] for x in selected_files(config)}:
                raise ValueError("Dry-run selection includes unlisted remotes")
            result["selected_file_count"] = len(selected)
            result["network_operations"] = 0
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, FoundationError, ET.ParseError):
        print(
            json.dumps(
                {"status": "failed", "detail": "Acquisition failed; inspect private receipts"}
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
