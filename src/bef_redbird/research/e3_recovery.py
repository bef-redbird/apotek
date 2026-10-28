"""Explicit research-only E3 capsules; no database copy or scientific mutation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .dataset import ResearchError, digest
from .e2_data import verify_pin
from .group_features import pin
from .training_contract import atomic_json

VERSION = "e3-research-capsule-v1"


def safe_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or relative.is_absolute()
        or ".." in relative.parts
        or ":" in value
    ):
        raise ResearchError("e3_recovery_path_escape")
    return relative


def external_dependencies(root: Path) -> list[dict[str, Any]]:
    """Reuse immutable base payloads and exact E2 baselines; never copy their DB."""
    protocol = json.loads((root / "run-v1/protocol.json").read_text())
    references: dict[str, dict[str, Any]] = {}

    def admit(reference: dict[str, Any], parent: Path | None = None) -> None:
        path = Path(reference["path"])
        if not path.is_absolute():
            if parent is None:
                raise ResearchError("e3_external_reference_without_root")
            path = parent / path
        if path.resolve().is_relative_to(root.resolve()):
            return
        verify_pin(path, reference)
        references[str(path.resolve())] = {**reference, "path": str(path.resolve())}

    for reference in protocol["pins"].values():
        admit(reference)
    bundle = Path(protocol["pins"]["dataset"]["path"])
    manifest = json.loads(bundle.read_text())
    for file in manifest["files"]:
        admit(file, bundle.parent)
    for reference in manifest["binding"]["recovery_references"]:
        admit(reference)
    admit(manifest["binding"]["exclusion_ledger"])
    groups = Path(protocol["pins"]["groups"]["path"])
    for file in json.loads(groups.read_text())["files"]:
        admit(file, groups.parent)
    e2_root = Path(protocol["pins"]["e2_protocol"]["path"]).parent
    ledger = json.loads((e2_root / "trial-ledger.json").read_text())
    for trial in ledger["trials"]:
        if trial["status"] == "completed" and trial["task"] in {"direction", "magnitude"}:
            for file in trial["files"]:
                admit(file, e2_root / "trials" / trial["trial_id"])
    return list(references.values())


def backup(root: Path, destination: Path, inventory_path: Path) -> dict[str, Any]:
    """Capture an explicitly reviewed relative-file list, excluding all scratch."""
    root = root.resolve()
    if destination.exists():
        raise ResearchError("e3_recovery_destination_exists")
    inventory = json.loads(inventory_path.read_text())
    relative_files = inventory["files"]
    if len(relative_files) != len(set(relative_files)):
        raise ResearchError("e3_duplicate_recovery_inventory")
    payload = []
    for relative in relative_files:
        safe_relative(relative)
        file = root / relative
        if not file.resolve().is_relative_to(root) or file.is_symlink() or not file.is_file():
            raise ResearchError("e3_recovery_invalid_source")
        payload.append({**pin(file), "path": relative})
    external = external_dependencies(root)
    destination.mkdir(parents=True)
    with zipfile.ZipFile(
        destination / "payload.zip",
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
        allowZip64=True,
    ) as archive:
        for file in payload:
            archive.write(root / file["path"], file["path"])
    result: dict[str, Any] = {
        "version": VERSION,
        "source_root": str(root),
        "payload": payload,
        "external": external,
        "archive": {**pin(destination / "payload.zip"), "path": "payload.zip"},
        "inventory": pin(inventory_path),
        "scope": (
            "Research artifacts/code only; retained R1/E2/source references; "
            "no operating DB copy; local existing-runtime recovery."
        ),
    }
    atomic_json(destination / "recovery.json", result)
    return {
        "files": len(payload),
        "logical_bytes": sum(x["size_bytes"] for x in payload),
        "archive_bytes": result["archive"]["size_bytes"],
        "external_pins": len(external),
    }


def restore(bundle: Path, destination: Path, receipt: Path) -> dict[str, Any]:
    if destination.exists() or receipt.exists():
        raise ResearchError("e3_restore_requires_unused_paths")
    manifest = json.loads((bundle / "recovery.json").read_text())
    if manifest["version"] != VERSION:
        raise ResearchError("e3_recovery_unknown_version")
    verify_pin(bundle / manifest["archive"]["path"], manifest["archive"])
    for item in manifest["external"]:
        verify_pin(Path(item["path"]), item)
    expected = {x["path"]: x for x in manifest["payload"]}
    if len(expected) != len(manifest["payload"]):
        raise ResearchError("e3_recovery_duplicate_payload")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(bundle / manifest["archive"]["path"]) as archive:
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != set(expected):
            raise ResearchError("e3_recovery_archive_inventory_changed")
        for name, reference in expected.items():
            safe_relative(name)
            file = destination / name
            if not file.resolve().is_relative_to(destination.resolve()):
                raise ResearchError("e3_recovery_path_escape")
            file.parent.mkdir(parents=True, exist_ok=True)
            hasher = hashlib.sha256()
            size = 0
            with archive.open(name) as source, file.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
            if hasher.hexdigest() != reference["sha256"] or size != reference["size_bytes"]:
                raise ResearchError("e3_restored_payload_changed")
    result = {
        "version": VERSION,
        "status": "byte_verified",
        "bundle": pin(bundle / "recovery.json"),
        "destination": str(destination.resolve()),
        "files": len(expected),
        "external_pins": len(manifest["external"]),
        "logical_bytes": sum(x["size_bytes"] for x in expected.values()),
        "scope": "Byte restore only; captured producer replay has a separate receipt.",
    }
    atomic_json(receipt, result)
    return result


def verify_restored(bundle: Path, destination: Path) -> dict[str, Any]:
    manifest = json.loads((bundle / "recovery.json").read_text())
    for item in manifest["payload"]:
        safe_relative(item["path"])
        verify_pin(destination / item["path"], item)
    for item in manifest["external"]:
        verify_pin(Path(item["path"]), item)
    return {
        "status": "verified",
        "payload_files": len(manifest["payload"]),
        "external_pins": len(manifest["external"]),
        "manifest_sha256": digest(bundle / "recovery.json"),
    }


def capture_source(repository: Path, target: Path) -> None:
    if target.exists():
        raise ResearchError("e3_source_capture_exists")
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as z:
        for file in [
            *sorted((repository / "src").rglob("*.py")),
            *sorted((repository / "src").rglob("*.sql")),
            repository / "pyproject.toml",
            repository / "uv.lock",
            repository / "research_dashboard.py",
        ]:
            z.write(file, file.relative_to(repository).as_posix())


def extract_source(archive: Path, destination: Path) -> None:
    if destination.exists():
        raise ResearchError("e3_producer_destination_exists")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            safe_relative(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["backup", "restore", "verify", "source"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.command == "backup" and args.root and args.inventory:
        print(json.dumps(backup(args.root, args.destination, args.inventory)))
    elif args.command == "restore" and args.bundle and args.receipt:
        print(json.dumps(restore(args.bundle, args.destination, args.receipt)))
    elif args.command == "verify" and args.bundle:
        print(json.dumps(verify_restored(args.bundle, args.destination)))
    elif args.command == "source" and args.root:
        capture_source(args.root, args.destination)
    else:
        parser.error("Required arguments missing")
