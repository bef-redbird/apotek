"""Explicit research-file backup and offline captured-producer replay; no fitting."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .dataset import ResearchError, digest, read_json
from .e2_result_view import load_e2_run
from .training_contract import atomic_json

VERSION = "research-file-recovery-v1"


def _external(path: Path) -> Path:
    expanded = Path(os.path.abspath(path.expanduser()))
    for component in (expanded, *expanded.parents):
        if component.is_symlink() or component.is_junction():
            raise ResearchError("research_recovery_link_rejected")
    resolved = expanded.resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise ResearchError("research_recovery_requires_external_storage")
    return resolved


def _relative(root: Path, value: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ResearchError("research_recovery_path_escape")
    target = root / value
    for component in (target, *target.parents):
        if component == root.parent:
            break
        if component.is_symlink() or (
            hasattr(component, "is_junction") and component.is_junction()
        ):
            raise ResearchError("research_recovery_link_rejected")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ResearchError("research_recovery_path_escape")
    return target


def _pin(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": digest(path), "size_bytes": path.stat().st_size}


def _verify(path: Path, pin: dict[str, Any]) -> None:
    if (
        not path.is_file()
        or path.stat().st_size != pin["size_bytes"]
        or digest(path) != pin["sha256"]
    ):
        raise ResearchError("research_recovery_pin_changed")


def _archive_members(archive: zipfile.ZipFile, root: Path) -> list[str]:
    names = []
    for info in archive.infolist():
        _relative(root, info.filename)
        if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.filename in names:
            raise ResearchError("research_recovery_invalid_zip_member")
        names.append(info.filename)
    return names


def _training_references(reference: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(reference["path"])
    _verify(path, reference)
    manifest = read_json(path)
    refs = [reference]
    for item in manifest["files"]:
        refs.append({**item, "path": str(_relative(path.parent, item["path"]).resolve())})
    binding = manifest["binding"]
    refs.extend([*binding["recovery_references"], binding["exclusion_ledger"]])
    return refs


def backup(research_root: Path, selection_path: Path, destination: Path) -> Path:
    root, destination = _external(research_root), _external(destination)
    if destination.exists() or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ResearchError("research_recovery_destination_not_unused")
    selection = read_json(selection_path)
    if selection.get("version") != "research-recovery-selection-v1":
        raise ResearchError("research_recovery_selection_version")
    run = _relative(root, selection["run"])
    group_path = _relative(root, selection["group_manifest"])
    protocol = read_json(run / "protocol.json")
    load_e2_run(run / "saved" / "result.json")
    _verify(run / "protocol.json", read_json(run / "freeze-receipt.json")["protocol"])
    payload: dict[str, dict[str, Any]] = {}

    def include(path: Path, role: str) -> None:
        relative = path.relative_to(root).as_posix()
        target = _relative(root, relative)
        if not target.is_file():
            raise ResearchError("research_recovery_required_payload_missing")
        payload[relative] = {**_pin(target), "archive_path": relative, "role": role}

    for name in (
        "protocol.json",
        "freeze-receipt.json",
        "producer.zip",
        "selection.json",
        "trial-ledger.json",
        "development-assessment.json",
    ):
        include(run / name, "research_run")
    result = read_json(run / "saved" / "result.json")
    saved_protocol = next(item for item in result["files"] if item["role"] == "protocol")
    _verify(run / "protocol.json", saved_protocol)
    include(run / "saved" / "result.json", "saved_result")
    for item in result["files"]:
        include(_relative(run / "saved", item["path"]), "saved_result")
    ledger = read_json(run / "trial-ledger.json")
    for trial in ledger["trials"]:
        trial_root = _relative(run / "trials", trial["trial_id"])
        receipt = trial_root / "trial.json"
        if read_json(receipt) != trial or trial["status"] not in {"completed", "failed"}:
            raise ResearchError("research_recovery_trial_ledger_changed")
        if trial.get("protocol_sha256") != digest(run / "protocol.json"):
            raise ResearchError("research_recovery_trial_protocol_changed")
        include(receipt, "trial_receipt")
        for item in trial.get("files", []):
            source = _relative(trial_root, item["path"])
            _verify(source, item)
            include(source, "trial_artifact")
    group = read_json(group_path)
    include(group_path, "group_manifest")
    for item in group["files"]:
        source = Path(item["path"])
        _verify(source, item)
        include(source, "group_artifact")
    for item in selection.get("payload", []):
        if item.get("role") not in {"semantic_evidence", "independent_evidence", "acceptance"}:
            raise ResearchError("research_recovery_invalid_evidence_role")
        allowed = {".json", ".md", ".txt", ".log"}
        if item["role"] == "independent_evidence":
            allowed.add(".py")
        elif item["role"] == "semantic_evidence":
            allowed.add(".html")
        # Explicit checker source and native guidance are opaque retained bytes;
        # only the separately pinned producer snapshot is used during replay.
        if Path(item["path"]).suffix not in allowed:
            raise ResearchError("research_recovery_evidence_type_rejected")
        include(_relative(root, item["path"]), item["role"])
    display_path = _relative(
        root, selection.get("display_result", selection["run"] + "/saved/result.json")
    )
    if display_path.parent != run / "saved":
        raise ResearchError("research_recovery_display_outside_selected_run")
    display = load_e2_run(display_path)
    display_protocol = next(
        item for item in display.manifest["files"] if item["role"] == "protocol"
    )
    _verify(run / "protocol.json", display_protocol)
    for path in [
        display_path,
        *(_relative(display_path.parent, item["path"]) for item in display.manifest["files"]),
    ]:
        if path.relative_to(root).as_posix() not in payload:
            raise ResearchError("research_recovery_display_payload_not_closed")
    references = _training_references(protocol["dataset"])
    references.extend(group["base_inputs"])
    references.extend(selection.get("external_references", []))
    unique: dict[str, dict[str, Any]] = {}
    for reference in references:
        source = _external(Path(reference["path"]))
        _verify(source, reference)
        key = str(source)
        if key in {item["path"] for item in payload.values()}:
            continue
        if key in unique and unique[key]["sha256"] != reference["sha256"]:
            raise ResearchError("research_recovery_conflicting_external_reference")
        unique[key] = _pin(source)
    for field in ("groups", "producer", "development_assessment"):
        reference = protocol[field]
        source = Path(reference["path"])
        _verify(source, reference)
        if str(source.resolve()) not in {item["path"] for item in payload.values()}:
            raise ResearchError("research_recovery_protocol_payload_not_closed")
    # Semantic admission sources are explicit pins supplied in selection, never a directory scan.
    helper = Path(__file__)
    if "recovery-tool/research_recovery.py" in payload:
        raise ResearchError("research_recovery_reserved_payload_path")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(destination / "payload.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for relative, item in sorted(payload.items()):
            archive.write(item["path"], relative)
            _verify(Path(item["path"]), item)
        archive.write(helper, "recovery-tool/research_recovery.py")
    manifest = {
        "version": VERSION,
        "status": "completed",
        "run": selection["run"],
        "group_manifest": selection["group_manifest"],
        "display_result": display_path.relative_to(root).as_posix(),
        "payload": list(payload.values()),
        "helper": {**_pin(helper), "archive_path": "recovery-tool/research_recovery.py"},
        "external_references": list(unique.values()),
        "dependencies": protocol["dependencies"],
        "python": list(sys.version_info[:2]),
        "archive": _pin(destination / "payload.zip"),
        "boundary": (
            "Research files only; existing pinned training/reference files required externally. "
            "No database, native archive, training Parquet, installation "
            "or off-device recovery claim."
        ),
    }
    atomic_json(destination / "recovery.json", manifest)
    return destination / "recovery.json"


def restore(bundle: Path, destination: Path, receipt: Path) -> Path:
    bundle, destination, receipt = _external(bundle), _external(destination), _external(receipt)
    if destination.exists() or receipt.exists() or receipt.is_relative_to(destination):
        raise ResearchError("research_recovery_restore_destination_not_unused")
    manifest = read_json(bundle / "recovery.json")
    if manifest.get("version") != VERSION or manifest.get("status") != "completed":
        raise ResearchError("research_recovery_manifest_version")
    if manifest["python"] != list(sys.version_info[:2]):
        raise ResearchError("research_recovery_interpreter_mismatch")
    for name, version in manifest["dependencies"].items():
        if importlib.metadata.version(name) != version:
            raise ResearchError("research_recovery_dependency_mismatch")
    for reference in manifest["external_references"]:
        _verify(Path(reference["path"]), reference)
    archive_path = bundle / "payload.zip"
    _verify(archive_path, manifest["archive"])
    expected = {item["archive_path"] for item in manifest["payload"]} | {
        manifest["helper"]["archive_path"]
    }
    with zipfile.ZipFile(archive_path) as archive:
        if set(_archive_members(archive, destination)) != expected:
            raise ResearchError("research_recovery_archive_inventory_changed")
        destination.mkdir(parents=True)
        archive.extractall(destination)
    relocations = {}
    for item in [*manifest["payload"], manifest["helper"]]:
        local = _relative(destination, item["archive_path"])
        _verify(local, item)
        relocations[item["path"]] = str(local)
    run = _relative(destination, manifest["run"])
    group_path = _relative(destination, manifest["group_manifest"])
    group = read_json(group_path)
    for item in [group["bundle"], *group["base_inputs"], *group["files"]]:
        _verify(Path(relocations.get(item["path"], item["path"])), item)
    load_e2_run(run / "saved" / "result.json")
    load_e2_run(
        _relative(
            destination, manifest.get("display_result", manifest["run"] + "/saved/result.json")
        )
    )
    with tempfile.TemporaryDirectory(
        prefix="research-replay-", dir=destination.parent
    ) as temporary:
        work = Path(temporary)
        code = work / "producer"
        with zipfile.ZipFile(run / "producer.zip") as archive:
            _archive_members(archive, code)
            archive.extractall(code)
        shutil.copytree(run, work / "run")
        # Replay writes its new receipt only in this disposable copy.
        script = (
            "import sys,json,socket; from pathlib import Path; "
            "sys.path.insert(0,sys.argv[1]); "
            "import duckdb; from catboost import CatBoostClassifier,CatBoostRegressor; "
            "deny=lambda *a,**k: (_ for _ in ()).throw("
            "RuntimeError('recovery_execution_forbidden')); "
            "duckdb.connect=deny; CatBoostClassifier.fit=deny; CatBoostRegressor.fit=deny; "
            "CatBoostClassifier.load_model=deny; CatBoostRegressor.load_model=deny; "
            "sys.addaudithook(lambda event,args: deny() if event=='socket.connect' else None); "
            "from bef_redbird.research.e2_experiment import replay; "
            "from bef_redbird.research.e2_result_view import load_e2_run; "
            "load_e2_run(Path(sys.argv[2])/'saved'/'result.json'); replay(Path(sys.argv[2]))"
        )
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", script, str(code / "src"), str(work / "run")],
            env=environment,
            cwd=work,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode:
            raise ResearchError("research_recovery_captured_replay_failed")
        replay_result = read_json(work / "run" / "replay.json")
        ledger = read_json(run / "trial-ledger.json")
        expected_trials = sorted(
            t["trial_id"] for t in ledger["trials"] if t["status"] == "completed"
        )
        if (
            replay_result["status"] != "passed"
            or sorted(replay_result["trials"]) != expected_trials
        ):
            raise ResearchError("research_recovery_replay_inventory_changed")
    # Confirm the immutable restored payload survived validation byte-for-byte.
    for item in [*manifest["payload"], manifest["helper"]]:
        _verify(Path(relocations[item["path"]]), item)
    atomic_json(
        receipt,
        {
            "status": "passed",
            "version": VERSION,
            "bundle_manifest": _pin(bundle / "recovery.json"),
            "restored_root": str(destination),
            "payload_files": len(expected),
            "external_files": len(manifest["external_references"]),
            "relocations": relocations,
            "replay": replay_result,
            "boundary": manifest["boundary"],
        },
    )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("backup")
    save.add_argument("--research-root", type=Path, required=True)
    save.add_argument("--selection", type=Path, required=True)
    save.add_argument("--destination", type=Path, required=True)
    recover = commands.add_parser("restore")
    recover.add_argument("--bundle", type=Path, required=True)
    recover.add_argument("--destination", type=Path, required=True)
    recover.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backup":
        backup(args.research_root, args.selection, args.destination)
    else:
        restore(args.bundle, args.destination, args.receipt)


if __name__ == "__main__":
    main()
