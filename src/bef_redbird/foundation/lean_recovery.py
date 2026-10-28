"""Cold recovery of the lean store, operating metadata and frozen training bundle.

Requires closed writers. Restore uses unused paths and verifies every byte before
opening a database or a training reader. Earlier experiments use the separately
retained pre-cleanup archive; they are not duplicated into each lean recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .lean import file_hash, write_json
from .locking import writer_lock
from .recovery import backup_store_compressed, restore_store_compressed


def _regular_files(root: Path) -> list[Path]:
    result = []
    for directory, folders, files in os.walk(root, followlinks=False):
        base = Path(directory)
        if base.is_symlink() or base.is_junction():
            raise ValueError("Linked directory in recovery input")
        for name in folders:
            child = base / name
            if child.is_symlink() or child.is_junction():
                raise ValueError("Linked directory in recovery input")
        for name in files:
            path = base / name
            if path.is_symlink():
                raise ValueError("Linked file in recovery input")
            result.append(path)
    return sorted(result)


def backup(config_path: Path, training: Path, destination: Path, metadata: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text())
    with writer_lock(Path(config["operation_root"]) / "interpretation.writer"):
        return _backup(config_path, training, destination, metadata)


def _backup(config_path: Path, training: Path, destination: Path, metadata: Path) -> dict[str, Any]:
    """Capture the explicitly reviewed current metadata list, not an entire workspace."""
    config_path, training, destination, metadata = (
        path.resolve() for path in (config_path, training, destination, metadata)
    )
    if destination.exists():
        raise ValueError("Use a new cold recovery destination")
    config = json.loads(config_path.read_text())
    workspace = Path(config.get("recovery_workspace", config["workspace"])).resolve()
    producer = Path(config["workspace"]).resolve()
    if not producer.is_relative_to(workspace):
        raise ValueError("Recovery producer must be inside the reviewed workspace")
    repo = producer if (producer / "src/bef_redbird").is_dir() else producer / "bef-redbird"
    if not (training / "dataset.json").is_file():
        raise ValueError("A completed training manifest is required")
    training_manifest = json.loads((training / "dataset.json").read_text())
    if training_manifest.get("status") != "completed":
        raise ValueError("Training bundle is not completed")
    if destination.is_relative_to(training) or destination.is_relative_to(
        Path(config["archive_root"])
    ):
        raise ValueError("Recovery destination overlaps source inputs")
    _regular_files(training)
    requested = json.loads(metadata.read_text())
    paths = [Path(path).resolve() for path in requested["files"]]
    paths += [config_path, metadata]
    # Follow the current mutable pointer on every refresh, rather than requiring
    # an operator to add each newly frozen selection to the reviewed file list.
    publication_path = Path(config["operation_root"]) / "publication-state.json"
    publication = json.loads(publication_path.read_text())
    selection = Path(publication["configuration_path"])
    if not selection.is_absolute():
        selection = publication_path.parent / selection
    if file_hash(selection) != publication["configuration_sha256"]:
        raise ValueError("Current publication selection changed")
    paths += [publication_path.resolve(), selection.resolve()]
    # First-receipt sidecars are required for idempotent supplier retries after
    # recovery. New batches join the same bounded, explicitly owned directory.
    intake_root = Path(config["operation_root"]) / "supplier-intake"
    if intake_root.exists():
        paths += _regular_files(intake_root)
    reference_locations: dict[str, str] = {}
    for item in [
        *training_manifest["binding"]["recovery_references"],
        training_manifest["binding"]["exclusion_ledger"],
    ]:
        original = Path(item["path"])
        candidates = [original]
        for previous, current in config.get("training_reference_relocations", {}).items():
            if original.is_relative_to(Path(previous)):
                candidates.append(Path(current) / original.relative_to(Path(previous)))
        resolved = next(
            (
                candidate.resolve()
                for candidate in candidates
                if candidate.is_file()
                and not candidate.is_symlink()
                and candidate.resolve().is_relative_to(workspace)
                and candidate.stat().st_size == item["size_bytes"]
                and file_hash(candidate) == item["sha256"]
            ),
            None,
        )
        if resolved is None:
            raise ValueError("Frozen training recovery reference changed")
        paths.append(resolved)
        reference_locations[str(original)] = (
            Path("workspace") / resolved.relative_to(workspace)
        ).as_posix()
    if any(not p.is_relative_to(workspace) or not p.is_file() or p.is_symlink() for p in paths):
        raise ValueError("Recovery metadata must be reviewed regular workspace files")
    destination.mkdir(parents=True)
    backup_store_compressed(
        Path(config["database"]), Path(config["archive_root"]), destination / "store"
    )
    copied: dict[str, dict[str, Any]] = {}
    for path in sorted(set(paths)):
        relative = Path("workspace") / path.relative_to(workspace)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        expected = file_hash(path)
        if file_hash(target) != expected:
            raise ValueError("Recovery metadata copy changed")
        copied[relative.as_posix()] = {"sha256": expected, "bytes": path.stat().st_size}
    shutil.copytree(training, destination / "training")
    for path in _regular_files(destination / "training"):
        source = training / path.relative_to(destination / "training")
        if file_hash(path) != file_hash(source):
            raise ValueError("Frozen training copy changed")
        copied[path.relative_to(destination).as_posix()] = {
            "sha256": file_hash(path),
            "bytes": path.stat().st_size,
        }
    code = destination / "producer.zip"
    with zipfile.ZipFile(code, "x", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted((repo / "src").rglob("*")):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix in {".py", ".sql"}
            ):
                output.write(path, path.relative_to(repo).as_posix())
        for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
            output.write(repo / name, name)
        for path in sorted((repo / "scripts").glob("*.ps1")):
            output.write(path, path.relative_to(repo).as_posix())
    copied["producer.zip"] = {"sha256": file_hash(code), "bytes": code.stat().st_size}
    manifest = {
        "version": "lean-recovery-v1",
        "complete": True,
        "created_utc": datetime.now(UTC).isoformat(),
        "source_workspace": str(workspace),
        "source_training": str(training),
        "source_config": str(config_path),
        "source_metadata": str(metadata),
        "reference_locations": reference_locations,
        "store_manifest_sha256": file_hash(destination / "store/backup.json"),
        "files": copied,
        "fresh_machine_bootstrap": False,
        "off_device_recovery": False,
        "model_fitting": False,
        "scheduler_installed": False,
    }
    write_json(destination / "recovery.json", manifest)
    return {"complete": True, "metadata_and_training_files": len(copied)}


def _runtime_training_template(
    runtime_root: Path, original_root: Path, destination: Path, files: dict[str, Any]
) -> bool:
    """Relocate only refresh references to byte-verified captured evidence."""
    path = runtime_root / "training-policy.json"
    if not path.exists():
        return False
    template = json.loads(path.read_text())

    def captured(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Training refresh reference must name captured evidence")
        original = Path(value)
        if not original.is_absolute() or not original.is_relative_to(original_root):
            raise ValueError("Training refresh reference is outside captured workspace")
        relative = Path("workspace") / original.relative_to(original_root)
        expected = files.get(relative.as_posix())
        target = destination / relative
        if (
            expected is None
            or not target.is_file()
            or target.is_symlink()
            or file_hash(target) != expected["sha256"]
        ):
            raise ValueError("Training refresh reference is missing, uncaptured or changed")
        return str(target)

    references = template.get("recovery_references")
    if not isinstance(references, list) or not references:
        raise ValueError("Training refresh requires captured recovery references")
    template["recovery_references"] = [captured(value) for value in references]
    template["exclusion_ledger"] = captured(template.get("exclusion_ledger"))
    write_json(path, template)
    return True


def restore(bundle: Path, destination: Path, receipt: Path) -> dict[str, Any]:
    bundle, destination, receipt = (p.resolve() for p in (bundle, destination, receipt))
    if (
        destination.exists()
        or destination.is_relative_to(bundle)
        or receipt.is_relative_to(bundle)
        or receipt.exists()
    ):
        raise ValueError("Restore requires a new destination outside the cold bundle")
    manifest = json.loads((bundle / "recovery.json").read_text())
    if manifest.get("version") != "lean-recovery-v1" or not manifest.get("complete"):
        raise ValueError("Incomplete lean recovery")
    if file_hash(bundle / "store/backup.json") != manifest["store_manifest_sha256"]:
        raise ValueError("Recovery store manifest changed")
    for name, info in manifest["files"].items():
        if "\\" in name or ":" in name or any(part in {"", ".", ".."} for part in name.split("/")):
            raise ValueError("Recovery artifact changed: invalid relative location")
        path = (bundle / name).resolve()
        if (
            not path.is_relative_to(bundle)
            or path.is_symlink()
            or file_hash(path) != info["sha256"]
        ):
            raise ValueError("Recovery artifact changed")
    destination.mkdir(parents=True)
    restore_store_compressed(
        bundle / "store", destination / "store/foundation.duckdb", destination / "store/originals"
    )
    for name in manifest["files"]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / name, target)
    with zipfile.ZipFile(destination / "producer.zip") as archive:
        for member in archive.infolist():
            if (
                not (destination / "producer" / member.filename)
                .resolve()
                .is_relative_to(destination / "producer")
            ):
                raise ValueError("Invalid producer archive location")
        archive.extractall(destination / "producer")
    original_root = Path(manifest["source_workspace"])
    old_config = Path(manifest["source_config"])
    new_config = destination / "workspace" / old_config.relative_to(original_root)

    def relocate(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: relocate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, str) and value.lower().startswith(str(original_root).lower()):
            return str(destination / "workspace" / Path(value).relative_to(original_root))
        return value

    # Preserve the captured metadata byte-for-byte. Future maintenance writes to
    # a separate runtime copy, including its mutable policy and publication pointer.
    runtime_root = destination / "runtime"
    shutil.copytree(new_config.parent, runtime_root)
    refresh_template = _runtime_training_template(
        runtime_root, original_root, destination, manifest["files"]
    )

    def runtime_location(value: str) -> str:
        path = Path(value)
        # A restore may itself be beneath the original workspace. Do not apply
        # the old-root mapping twice to paths already inside the captured tree.
        if not path.is_relative_to(new_config.parent):
            path = Path(relocate(value))
        if path.is_relative_to(new_config.parent):
            return str(runtime_root / path.relative_to(new_config.parent))
        return str(path)

    config = relocate(json.loads(new_config.read_text()))
    for key in (
        "operation_root",
        "inventory_path",
        "registry_policy_config",
        "reviewed_calendar",
    ):
        if key in config:
            config[key] = runtime_location(config[key])
    config.update(
        database=str(destination / "store/foundation.duckdb"),
        archive_root=str(destination / "store/originals"),
        native_root=str(destination / "store/originals"),
        workspace=str(destination / "producer"),
        python_path=sys.executable,
        recovery_workspace=str(destination),
    )
    training_manifest = json.loads((destination / "training/dataset.json").read_text())
    references = [
        *training_manifest["binding"]["recovery_references"],
        training_manifest["binding"]["exclusion_ledger"],
    ]
    if "reference_locations" in manifest:
        if set(manifest["reference_locations"]) != {item["path"] for item in references}:
            raise ValueError("Frozen recovery reference closure changed")
        exact_relocations = {}
        for item in references:
            relative = manifest["reference_locations"][item["path"]]
            if relative not in manifest["files"] or not relative.startswith("workspace/"):
                raise ValueError("Frozen recovery reference is not captured")
            target = destination / relative
            if target.stat().st_size != item["size_bytes"] or file_hash(target) != item["sha256"]:
                raise ValueError("Frozen recovery reference changed")
            exact_relocations[item["path"]] = str(target)
        config["training_reference_relocations"] = exact_relocations
    else:
        config["training_reference_relocations"] = {
            **config.get("training_reference_relocations", {}),
            str(original_root): str(destination / "workspace"),
        }
    if "source_metadata" in manifest:
        source_metadata = Path(manifest["source_metadata"])
        captured_metadata = destination / "workspace" / source_metadata.relative_to(original_root)
        reviewed = json.loads(captured_metadata.read_text())
        runtime_reviewed = []
        for value in reviewed["files"]:
            source = Path(value)
            if not source.is_absolute() or not source.is_relative_to(original_root):
                raise ValueError("Reviewed recovery metadata escapes source workspace")
            relative = Path("workspace") / source.relative_to(original_root)
            expected = manifest["files"].get(relative.as_posix())
            captured = destination / relative
            if (
                expected is None
                or not captured.is_file()
                or file_hash(captured) != expected["sha256"]
            ):
                raise ValueError("Reviewed recovery metadata was not captured unchanged")
            runtime_copy = (
                runtime_root / source.relative_to(old_config.parent)
                if source.is_relative_to(old_config.parent)
                else None
            )
            runtime_reviewed.append(
                str(runtime_copy if runtime_copy and runtime_copy.is_file() else captured)
            )
        write_json(runtime_root / "recovery-metadata.json", {"files": runtime_reviewed})
    publication_path = Path(config["operation_root"]) / "publication-state.json"
    publication = json.loads(publication_path.read_text())
    selection = Path(publication["configuration_path"])
    if selection.is_absolute():
        selection = Path(runtime_location(str(selection)))
    else:
        selection = publication_path.parent / selection
    if file_hash(selection) != publication["configuration_sha256"]:
        raise ValueError("Restored publication selection changed")
    publication["configuration_path"] = str(selection)
    write_json(publication_path, publication)
    # Relative source-review contracts remain beside the runtime configuration.
    runtime = runtime_root / "config.restored.json"
    write_json(runtime, config)
    environment = {
        **os.environ,
        "PYTHONPATH": str(destination / "producer/src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # This process imports the restored producer, without applying migrations or
    # fitting models. The reader command is versioned with the producer snapshot.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bef_redbird.foundation.lean_recovery",
            "check",
            "--config",
            str(runtime),
            "--training",
            str(destination / "training"),
            "--receipt",
            str(destination / "readback.json"),
        ],
        env=environment,
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        (destination / "restore-error.txt").write_text(result.stderr + result.stdout)
        raise ValueError("Restored operation/training readback failed; inspect restore-error.txt")
    proof = json.loads((destination / "readback.json").read_text())
    accepted = {
        "version": "lean-restore-acceptance-v1",
        "complete": True,
        "completed_utc": datetime.now(UTC).isoformat(),
        "bundle": str(bundle),
        "recovery_manifest_sha256": file_hash(bundle / "recovery.json"),
        "destination": str(destination),
        "verification": proof,
        "training_refresh_template_relocated": refresh_template,
        "model_fitting": False,
        "scheduler_installed": False,
        "fresh_machine_bootstrap": False,
        "off_device_recovery": False,
    }
    write_json(receipt, accepted)
    return accepted


def check(config_path: Path, training: Path, receipt: Path) -> dict[str, Any]:
    """Offline checks with the restored exact producer and existing interpreter."""
    import duckdb

    from .ftp_maintenance import status as maintenance_status
    from .lms_registry import ingest_registry
    from .migrations import MIGRATIONS
    from .registry_selection import status
    from .store import Store
    from .supplier_intake import publish_pending_supply

    config = json.loads(config_path.read_text())
    database, archive = Path(config["database"]), Path(config["archive_root"])
    with duckdb.connect(str(database), read_only=True) as con:
        applied = con.execute(
            "SELECT version,name,checksum FROM schema_migration ORDER BY version"
        ).fetchall()
        # Migrations hash normalized UTF-8 text, independent of native newlines.
        import hashlib

        expected = [
            (version, name, hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest())
            for version, name, path in MIGRATIONS
        ]
        if applied != expected:
            raise ValueError("Restored schema would require migration")
        datasets = [
            str(row[0])
            for row in con.execute("SELECT dataset_id FROM analytical_dataset").fetchall()
        ]
        native = con.execute(
            "SELECT r.import_id,r.delivery_id,r.phase,r.context,b.parser_version "
            "FROM registry_release r JOIN import_batch b USING(import_id) "
            "JOIN source_delivery d ON d.delivery_id=r.delivery_id "
            "ORDER BY d.byte_count LIMIT 1"
        ).fetchone()
    with Store(database, archive) as store:
        for did in datasets:
            status(store, did, verify=False)
        if native is None:
            raise ValueError("No native source for restored maintenance retry")
        result = ingest_registry(
            store,
            str(native[1]),
            phase=native[2],
            context=json.loads(native[3]),
            parser_version=native[4],
        )
        if result.get("retry") is not True or result["import_id"] != str(native[0]):
            raise ValueError("Restored native retry was not idempotent")
    operating = maintenance_status(config_path)
    publication = operating.get("publication") or {}
    if publication.get("result", {}).get("dataset_id") not in datasets:
        raise ValueError("Restored current publication does not name a retained dataset")

    def no_publication(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise ValueError("Restored supplier pins require unexpected publication")

    supplier = publish_pending_supply(config_path, runner=no_publication)
    if supplier["status"] != "already_current":
        raise ValueError("Restored supplier publication pointer is not current")
    # The full frozen reader entrypoint is resolved only at runtime so its exact
    # version is included in the final producer capsule.
    from bef_redbird.research.training_reader import load_bundle

    loaded = load_bundle(
        training / "dataset.json", relocations=config.get("training_reference_relocations", {})
    )
    training_proof = {
        "rows": loaded.manifest["rows"],
        "full_readback": True,
        "dataset_manifest_sha256": file_hash(training / "dataset.json"),
    }
    proof = {
        "complete": True,
        "schema_unchanged": True,
        "datasets": datasets,
        "native_retry": result,
        "operating_status": {
            "dataset_id": publication["result"]["dataset_id"],
            "dispositions": operating["dispositions"],
            "failures": operating["failures"],
            "supplier_publication": supplier,
        },
        "training": training_proof,
    }
    write_json(receipt, proof)
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("backup")
    for name in ("config", "training", "destination", "metadata"):
        make.add_argument("--" + name, type=Path, required=True)
    restore_parser = sub.add_parser("restore")
    for name in ("bundle", "destination", "receipt"):
        restore_parser.add_argument("--" + name, type=Path, required=True)
    inspect = sub.add_parser("check")
    for name in ("config", "training", "receipt"):
        inspect.add_argument("--" + name, type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if "config" in args:
        args["config_path"] = args.pop("config")
    if command == "backup":
        value = backup(**args)
    elif command == "restore":
        value = restore(**args)
    else:
        value = check(**args)
    print(json.dumps({"complete": value["complete"]}))


if __name__ == "__main__":
    main()
