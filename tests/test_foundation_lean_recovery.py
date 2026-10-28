"""Cold recovery rejects incomplete or unsafe inputs before restoring data."""

import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest

from bef_redbird.foundation.lean import file_hash, write_json
from bef_redbird.foundation.lean_recovery import _runtime_training_template, backup, restore
from bef_redbird.research.training import produce


def test_restore_rejects_incomplete_bundle_before_creating_destination(tmp_path: Path) -> None:
    bundle, target = tmp_path / "cold", tmp_path / "restored"
    write_json(bundle / "recovery.json", {"version": "lean-recovery-v1", "complete": False})
    with pytest.raises(ValueError, match="Incomplete"):
        restore(bundle, target, tmp_path / "receipt.json")
    assert not target.exists()


def test_restore_receipt_cannot_change_immutable_cold_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "cold"
    bundle.mkdir()
    with pytest.raises(ValueError, match="new destination"):
        restore(bundle, tmp_path / "restore", bundle / "receipt.json")


def test_restore_rejects_escaping_file_and_changed_metadata(tmp_path: Path) -> None:
    bundle, target = tmp_path / "cold", tmp_path / "restore"
    manifest_path = bundle / "store/backup.json"
    write_json(manifest_path, {})
    manifest = {
        "version": "lean-recovery-v1",
        "complete": True,
        "store_manifest_sha256": file_hash(manifest_path),
        "files": {"../outside": {"sha256": "0" * 64}},
    }
    write_json(bundle / "recovery.json", manifest)
    with pytest.raises(ValueError, match="artifact changed"):
        restore(bundle, target, tmp_path / "receipt.json")
    assert not target.exists()
    write_json(manifest_path, {"tampered": True})
    with pytest.raises(ValueError, match="manifest changed"):
        restore(bundle, target, tmp_path / "receipt.json")


def test_backup_refuses_recursive_training_destination(tmp_path: Path) -> None:
    config, training = tmp_path / "config.json", tmp_path / "training"
    (tmp_path / "ops").mkdir()
    write_json(
        config,
        {
            "workspace": str(tmp_path),
            "operation_root": str(tmp_path / "ops"),
            "archive_root": str(tmp_path / "originals"),
        },
    )
    write_json(training / "dataset.json", {"status": "completed"})
    with pytest.raises(ValueError, match="overlaps"):
        backup(config, training, training / "nested", tmp_path / "metadata.json")


@pytest.mark.parametrize("failure", ["uncaptured", "missing", "changed", "outside"])
def test_runtime_refresh_template_rejects_unverified_references(
    tmp_path: Path, failure: str
) -> None:
    original, target = tmp_path / "original", tmp_path / "restored"
    runtime = target / "runtime"
    reference = target / "workspace/evidence.json"
    write_json(reference, {"evidence": "synthetic"})
    source = original / "evidence.json"
    files = {"workspace/evidence.json": {"sha256": file_hash(reference)}}
    if failure == "uncaptured":
        files = {}
    elif failure == "missing":
        reference.unlink()
    elif failure == "changed":
        write_json(reference, {"evidence": "changed"})
    else:
        source = tmp_path / "outside.json"
    template = runtime / "training-policy.json"
    write_json(template, {"recovery_references": [str(source)], "exclusion_ledger": str(source)})
    before = template.read_bytes()
    with pytest.raises(ValueError, match="refresh reference"):
        _runtime_training_template(runtime, original, target, files)
    assert template.read_bytes() == before


@pytest.mark.parametrize("nested_restore", [False, True])
def test_complete_cold_restore_with_original_workspace_unavailable(
    tmp_path_factory: pytest.TempPathFactory,
    nested_restore: bool,
) -> None:
    root = tmp_path_factory.mktemp("lean-recovery")
    workspace = root / "w"
    workspace.mkdir()
    # Reuse the independently expected tiny native source recipe, with no model fitting.
    recipe: Any = inspect.unwrap(
        importlib.import_module("tests.test_research_training").producer_fixture
    )
    training_config, training = recipe(workspace)
    nested_evidence = workspace / "evidence/recovery/pins"
    nested_evidence.mkdir(parents=True)
    ledger = nested_evidence / "ledger.json"
    reference = nested_evidence / "reference.json"
    shutil.move(training_config["exclusion_ledger"], ledger)
    shutil.move(training_config["recovery_references"][0], reference)
    training_config["exclusion_ledger"] = str(ledger)
    training_config["recovery_references"] = [str(reference)]
    assert produce(training_config, training)["rows"] == 4
    repo = Path(__file__).resolve().parents[1]
    captured_repo = workspace / "bef-redbird"
    shutil.copytree(
        repo / "src", captured_repo / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(repo / "scripts", captured_repo / "scripts")
    for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
        shutil.copyfile(repo / name, captured_repo / name)
    operations = workspace / "ops"
    operations.mkdir()
    selection = operations / "selections/current.json"
    publication = operations / "publication-state.json"
    with duckdb.connect(training_config["database"], read_only=True) as connection:
        selected = connection.execute(
            "SELECT configuration FROM analytical_dataset WHERE dataset_id=?",
            [training_config["dataset_id"]],
        ).fetchone()
    assert selected is not None
    write_json(selection, json.loads(selected[0]))
    write_json(
        publication,
        {
            "configuration_path": str(selection),
            "configuration_sha256": file_hash(selection),
            "result": {"dataset_id": training_config["dataset_id"]},
        },
    )
    policy = workspace / "policy.json"
    review = workspace / "source-review/calendar.json"
    write_json(policy, {"supply_imports": [], "legacy_imports": []})
    write_json(review, {"reviewed": "synthetic"})
    config = workspace / "config.json"
    write_json(
        config,
        {
            "workspace": str(workspace),
            "python_path": sys.executable,
            "database": training_config["database"],
            "archive_root": str(workspace / "originals"),
            "native_root": str(workspace / "originals"),
            "operation_root": str(operations),
            "registry_policy_config": str(policy),
            "reviewed_calendar": str(review),
        },
    )
    metadata = workspace / "metadata.json"
    template = workspace / "training-policy.json"
    template_value = {
        key: value
        for key, value in training_config.items()
        if key not in {"database", "dataset_id"}
    }
    template_value["pool_window"] = {
        "start": "2008-01-14",
        "end_exclusive": "2008-02-25",
        "mature_before": "2008-03-10",
    }
    write_json(template, template_value)
    original_template = template.read_bytes()
    write_json(metadata, {"files": [str(policy), str(review), str(template)]})
    supplier_receipt = operations / "supplier-intake/synthetic/receipt.json"
    write_json(supplier_receipt, {"first_receipt": "2026-01-01T12:00:00+00:00"})
    cold = root / "cold"
    assert backup(config, training, cold, metadata)["complete"]
    cold_manifest = (cold / "recovery.json").read_bytes()
    original_config = config.read_bytes()
    original_training = (training / "dataset.json").read_bytes()
    original_publication = publication.read_bytes()
    workspace.rename(root / "original-unavailable")
    assert not workspace.exists()
    restored = (workspace if nested_restore else root) / "restored"
    acceptance = restore(cold, restored, root / "acceptance.json")
    assert acceptance["complete"]
    assert acceptance["verification"]["schema_unchanged"]
    assert acceptance["verification"]["native_retry"]["retry"]
    assert acceptance["verification"]["training"]["rows"] == 4
    assert acceptance["verification"]["training"]["full_readback"]
    assert acceptance["training_refresh_template_relocated"]
    assert acceptance["verification"]["operating_status"]["supplier_publication"] == {
        "status": "already_current",
        "publication_started": False,
    }
    assert (cold / "recovery.json").read_bytes() == cold_manifest
    assert (restored / "training/dataset.json").read_bytes() == original_training
    assert (restored / "workspace/config.json").read_bytes() == original_config
    assert (restored / "workspace/training-policy.json").read_bytes() == original_template
    assert (restored / "workspace/ops/publication-state.json").read_bytes() == original_publication
    assert json.loads(
        (restored / "workspace/ops/supplier-intake/synthetic/receipt.json").read_text()
    ) == {"first_receipt": "2026-01-01T12:00:00+00:00"}
    runtime = json.loads((restored / "runtime/config.restored.json").read_text())
    runtime_publication = json.loads(
        (Path(runtime["operation_root"]) / "publication-state.json").read_text()
    )
    assert Path(runtime_publication["configuration_path"]).is_file()
    assert Path(runtime["registry_policy_config"]).is_relative_to(restored / "runtime")
    assert Path(runtime["reviewed_calendar"]).is_file()
    assert Path(runtime["registry_policy_config"]).is_file()
    assert (restored / "producer/scripts/Import-SupplierReport.ps1").is_file()
    assert not config.exists()
    assert not training.exists()
    assert not (workspace / "bef-redbird").exists()
    fresh_template = restored / "runtime/training-policy.json"
    template_value = json.loads(fresh_template.read_text())
    for reference in [*template_value["recovery_references"], template_value["exclusion_ledger"]]:
        assert Path(reference).is_file()
        assert Path(reference).is_relative_to(restored / "workspace")
    output = restored / "fresh-training"
    command = [
        sys.executable,
        "-m",
        "bef_redbird.research.training",
        "--operating-config",
        str(restored / "runtime/config.restored.json"),
        "--template",
        str(fresh_template),
        "--output-root",
        str(output),
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": str(restored / "producer/src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    subprocess.run(command, env=environment, cwd=restored, check=True, capture_output=True)
    current = (output / "current.json").read_bytes()
    pointer = json.loads(current)
    assert pointer["status"] == "verified_readback_and_pool_no_fit" and pointer["rows"] == 4
    pools = json.loads(Path(pointer["pool_readiness"]["path"]).read_text())
    assert {task["target"] for task in pools["tasks"]} == {
        "next_direction",
        "next_delta_ore",
        "reversal_category",
    }
    assert not pools["model_fitted"] and not pools["predictions_produced"]
    subprocess.run(command, env=environment, cwd=restored, check=True, capture_output=True)
    assert (output / "current.json").read_bytes() == current
    assert (restored / "training/dataset.json").read_bytes() == original_training
    assert (restored / "workspace/training-policy.json").read_bytes() == original_template
    second_cold = root / "second-cold"
    runtime_config = restored / "runtime/config.restored.json"
    runtime_metadata = restored / "runtime/recovery-metadata.json"
    refreshed_bundle = Path(pointer["dataset"]["path"]).parent
    assert backup(runtime_config, refreshed_bundle, second_cold, runtime_metadata)["complete"]
    second_manifest = json.loads((second_cold / "recovery.json").read_text())
    assert second_manifest["source_workspace"] == str(restored)
    refreshed_references = json.loads((refreshed_bundle / "dataset.json").read_text())["binding"]
    for item in [
        *refreshed_references["recovery_references"],
        refreshed_references["exclusion_ledger"],
    ]:
        relative = second_manifest["reference_locations"][item["path"]]
        assert relative.startswith("workspace/workspace/")
        assert file_hash(second_cold / relative) == item["sha256"]
    assert (
        second_cold / "workspace/runtime/training-policy.json"
    ).read_bytes() == fresh_template.read_bytes()
    assert (
        file_hash(second_cold / "workspace/workspace/evidence/recovery/pins/reference.json")
        == json.loads(original_training)["binding"]["recovery_references"][0]["sha256"]
    )
    restored.rename(root / "first-restore-unavailable")
    assert not restored.exists()
    second_restored = root / "second-restored"
    second_acceptance = restore(second_cold, second_restored, root / "second-acceptance.json")
    assert second_acceptance["complete"]
    assert (
        second_acceptance["verification"]["operating_status"]["dataset_id"]
        == (acceptance["verification"]["operating_status"]["dataset_id"])
    )
    assert second_acceptance["verification"]["training"]["full_readback"]
    assert second_acceptance["verification"]["training"]["rows"] == 4
    assert (second_restored / "training/dataset.json").read_bytes() == (
        second_cold / "training/dataset.json"
    ).read_bytes()
    second_config = json.loads((second_restored / "runtime/config.restored.json").read_text())
    assert Path(second_config["registry_policy_config"]).is_file()
    assert all(
        Path(path).is_file()
        for path in json.loads((second_restored / "runtime/training-policy.json").read_text())[
            "recovery_references"
        ]
    )
    second_output = second_restored / "fresh-training"
    second_command = [
        sys.executable,
        "-m",
        "bef_redbird.research.training",
        "--operating-config",
        str(second_restored / "runtime/config.restored.json"),
        "--template",
        str(second_restored / "runtime/training-policy.json"),
        "--output-root",
        str(second_output),
    ]
    second_environment = {
        **os.environ,
        "PYTHONPATH": str(second_restored / "producer/src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    subprocess.run(
        second_command,
        env=second_environment,
        cwd=second_restored,
        check=True,
        capture_output=True,
    )
    third_pointer = json.loads((second_output / "current.json").read_text())
    third_cold = root / "third-cold"
    assert backup(
        second_restored / "runtime/config.restored.json",
        Path(third_pointer["dataset"]["path"]).parent,
        third_cold,
        second_restored / "runtime/recovery-metadata.json",
    )["complete"]
