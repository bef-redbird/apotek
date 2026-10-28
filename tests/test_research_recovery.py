"""Research recovery with original research unavailable and no refitting."""

from __future__ import annotations

import importlib.metadata
import json
import sys
import zipfile
from pathlib import Path

import pytest

from bef_redbird.research.dataset import ResearchError, digest, read_json
from bef_redbird.research.e2_experiment import publish
from bef_redbird.research.research_recovery import _relative, backup, restore
from bef_redbird.research.training_contract import atomic_json
from tests.test_research_e2_publish import synthetic_publish_root


def _pin(path: Path) -> dict[str, str | int]:
    return {"path": str(path.resolve()), "sha256": digest(path), "size_bytes": path.stat().st_size}


@pytest.fixture
def recovery_case(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "research"
    run = synthetic_publish_root(root / "run")
    training = tmp_path / "training"
    training.mkdir()
    (training / "features.parquet").write_bytes(b"external training fixture, never copied")
    (training / "exclusions.json").write_text("{}")
    atomic_json(
        training / "dataset.json",
        {
            "files": [{**_pin(training / "features.parquet"), "path": "features.parquet"}],
            "binding": {
                "recovery_references": [],
                "exclusion_ledger": _pin(training / "exclusions.json"),
            },
        },
    )
    groups = root / "groups"
    groups.mkdir()
    (groups / "contract.json").write_text('{"scope":"synthetic"}')
    atomic_json(
        groups / "manifest.json",
        {
            "bundle": _pin(training / "dataset.json"),
            "base_inputs": [_pin(training / "features.parquet")],
            "files": [{**_pin(groups / "contract.json"), "role": "contract"}],
        },
    )
    (run / "development-assessment.json").write_text('{"synthetic":true}')
    (run / "selection.json").write_text('{"history":1}')
    repository = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(run / "producer.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for source in (repository / "src").rglob("*.py"):
            archive.write(source, source.relative_to(repository).as_posix())
    protocol = read_json(run / "protocol.json")
    protocol.update(
        {
            "dataset": _pin(training / "dataset.json"),
            "groups": _pin(groups / "manifest.json"),
            "producer": _pin(run / "producer.zip"),
            "development_assessment": _pin(run / "development-assessment.json"),
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("numpy", "pandas", "pyarrow", "catboost")
            },
        }
    )
    atomic_json(run / "protocol.json", protocol)
    for receipt in (run / "trials").glob("*/trial.json"):
        value = read_json(receipt)
        value["protocol_sha256"] = digest(run / "protocol.json")
        atomic_json(receipt, value)
    publish(run)
    atomic_json(run / "freeze-receipt.json", {"protocol": _pin(run / "protocol.json")})
    (root / "independent.json").write_text('{"synthetic_review":"passed"}')
    (groups / "verify_aggregates.py").write_text("raise AssertionError('must never execute')\n")
    (groups / "official-guidance.html").write_text("<html><p>Retained native guidance</p></html>")
    (root / "scratch-unselected.txt").write_text("must not be archived")
    selection = tmp_path / "selection.json"
    atomic_json(
        selection,
        {
            "version": "research-recovery-selection-v1",
            "run": "run",
            "group_manifest": "groups/manifest.json",
            "payload": [
                {"path": "independent.json", "role": "independent_evidence"},
                {"path": "groups/verify_aggregates.py", "role": "independent_evidence"},
                {"path": "groups/official-guidance.html", "role": "semantic_evidence"},
            ],
        },
    )
    return root, selection, training


def test_separate_restore_without_original_research_preserves_bytes_and_replays(
    tmp_path: Path,
    recovery_case: tuple[Path, Path, Path],
) -> None:
    root, selection, training = recovery_case
    reviewed = root / "run" / "saved" / "result-reviewed.json"
    reviewed.write_bytes((reviewed.parent / "result.json").read_bytes())
    config = json.loads(selection.read_text())
    config["display_result"] = "run/saved/result-reviewed.json"
    config["payload"].append({"path": config["display_result"], "role": "acceptance"})
    atomic_json(selection, config)
    bundle = tmp_path / "backup"
    backup(root, selection, bundle)
    document = read_json(bundle / "recovery.json")
    assert document["display_result"] == "run/saved/result-reviewed.json"
    assert not any("scratch-unselected" in item["archive_path"] for item in document["payload"])
    assert all(not item["path"].startswith(str(training)) for item in document["payload"])
    original_result = (root / "run" / "saved" / "result.json").read_bytes()
    root.rename(tmp_path / "original-research-unavailable")
    restored, receipt = tmp_path / "restored", tmp_path / "restore-receipt.json"
    restore(bundle, restored, receipt)
    result = read_json(receipt)
    assert result["status"] == "passed" and len(result["replay"]["trials"]) == 48
    assert (restored / "run" / "saved" / "result.json").read_bytes() == original_result
    assert (restored / "groups" / "verify_aggregates.py").read_text() == (
        "raise AssertionError('must never execute')\n"
    )
    assert (restored / "groups" / "official-guidance.html").read_text() == (
        "<html><p>Retained native guidance</p></html>"
    )
    assert read_json(restored / "groups" / "manifest.json")["bundle"]["path"] == str(
        training / "dataset.json"
    )
    assert result["relocations"]
    with pytest.raises(ResearchError, match="not_unused"):
        restore(bundle, restored, tmp_path / "second-receipt.json")


def test_changed_external_training_file_blocks_restore_before_destination_creation(
    tmp_path: Path,
    recovery_case: tuple[Path, Path, Path],
) -> None:
    root, selection, training = recovery_case
    bundle = tmp_path / "backup"
    backup(root, selection, bundle)
    (training / "features.parquet").write_bytes(b"changed external training")
    with pytest.raises(ResearchError, match="pin_changed"):
        restore(bundle, tmp_path / "restored", tmp_path / "receipt.json")
    assert not (tmp_path / "restored").exists()


def test_corrupt_archive_blocks_restore(
    tmp_path: Path,
    recovery_case: tuple[Path, Path, Path],
) -> None:
    root, selection, _ = recovery_case
    bundle = tmp_path / "backup"
    backup(root, selection, bundle)
    with (bundle / "payload.zip").open("ab") as target:
        target.write(b"changed")
    with pytest.raises(ResearchError, match="pin_changed"):
        restore(bundle, tmp_path / "restored", tmp_path / "receipt.json")


@pytest.mark.parametrize("path", ["../outside.json", "/outside.json", "groups\\contract.json"])
def test_explicit_evidence_paths_cannot_escape(
    tmp_path: Path,
    recovery_case: tuple[Path, Path, Path],
    path: str,
) -> None:
    root, selection, _ = recovery_case
    config = json.loads(selection.read_text())
    config["payload"][0]["path"] = path
    atomic_json(selection, config)
    with pytest.raises(ResearchError, match="path_escape"):
        backup(root, selection, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_validly_pinned_zip_cannot_escape_restore_directory(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with zipfile.ZipFile(bundle / "payload.zip", "w") as archive:
        archive.writestr("../outside.json", "{}")
    atomic_json(
        bundle / "recovery.json",
        {
            "version": "research-file-recovery-v1",
            "status": "completed",
            "python": list(sys.version_info[:2]),
            "dependencies": {},
            "external_references": [],
            "archive": _pin(bundle / "payload.zip"),
            "payload": [],
            "helper": {"archive_path": "tool.py"},
        },
    )
    with pytest.raises(ResearchError, match="path_escape"):
        restore(bundle, tmp_path / "restore", tmp_path / "receipt.json")
    assert not (tmp_path / "outside.json").exists()


def test_payload_link_is_rejected_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    root = tmp_path / "payload"
    root.mkdir()
    link = root / "link.json"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create a synthetic symlink")
        raise
    with pytest.raises(ResearchError, match="link_rejected"):
        _relative(root, "link.json")
