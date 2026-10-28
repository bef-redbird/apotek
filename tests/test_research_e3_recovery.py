from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from bef_redbird.research import e3_recovery as recovery
from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.group_features import pin
from bef_redbird.research.training_contract import atomic_json


def capsule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    source = tmp_path / "research"
    (source / "run-v1/trials").mkdir(parents=True)
    (source / "run-v1/trials/predictions.bin").write_bytes(b"frozen-predictions\x00\xff")
    (source / "report.json").write_text('{"result":"mixed"}')
    external = tmp_path / "accepted-base.json"
    external.write_text('{"unchanged":true}')
    monkeypatch.setattr(recovery, "external_dependencies", lambda _: [pin(external)])
    inventory = tmp_path / "reviewed-inventory.json"
    atomic_json(inventory, {"files": ["report.json", "run-v1/trials/predictions.bin"]})
    bundle = tmp_path / "capsule"
    summary = recovery.backup(source, bundle, inventory)
    assert summary["files"] == 2
    assert summary["external_pins"] == 1
    return bundle, external


def test_research_only_restore_retains_exact_bytes_without_original_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = capsule(tmp_path, monkeypatch)
    (tmp_path / "research").rename(tmp_path / "original-unavailable")
    destination = tmp_path / "unused-restore"
    receipt = tmp_path / "receipt.json"
    result = recovery.restore(bundle, destination, receipt)
    assert result["status"] == "byte_verified"
    assert (
        destination / "run-v1/trials/predictions.bin"
    ).read_bytes() == b"frozen-predictions\x00\xff"
    assert recovery.verify_restored(bundle, destination)["payload_files"] == 2
    with pytest.raises(ResearchError, match="unused_paths"):
        recovery.restore(bundle, destination, tmp_path / "other.json")
    with pytest.raises(ResearchError, match="unused_paths"):
        recovery.restore(bundle, tmp_path / "another", receipt)


def test_restore_refuses_changed_external_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, external = capsule(tmp_path, monkeypatch)
    external.write_text('{"changed":true}')
    with pytest.raises(ResearchError, match="pin_changed"):
        recovery.restore(bundle, tmp_path / "unused", tmp_path / "receipt.json")
    assert not (tmp_path / "unused").exists()


def test_restore_refuses_corrupt_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle, _ = capsule(tmp_path, monkeypatch)
    with (bundle / "payload.zip").open("ab") as file:
        file.write(b"altered")
    with pytest.raises(ResearchError, match="pin_changed"):
        recovery.restore(bundle, tmp_path / "unused", tmp_path / "receipt.json")


@pytest.mark.parametrize(
    "name", ["../outside", "C:/outside", "C:\\outside", "/outside", "a/../../outside", ""]
)
def test_capsule_rejects_path_escape(name: str) -> None:
    with pytest.raises(ResearchError, match="path_escape"):
        recovery.safe_relative(name)


def test_archive_inventory_and_verified_payload_are_both_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = capsule(tmp_path, monkeypatch)
    with zipfile.ZipFile(bundle / "payload.zip", "a") as archive:
        archive.writestr("unreviewed.txt", "not authorized")
    manifest = json.loads((bundle / "recovery.json").read_text())
    manifest["archive"] = {**pin(bundle / "payload.zip"), "path": "payload.zip"}
    atomic_json(bundle / "recovery.json", manifest)
    with pytest.raises(ResearchError, match="inventory_changed"):
        recovery.restore(bundle, tmp_path / "unused", tmp_path / "receipt.json")
