"""Independent synthetic append-only inheritance and negative boundary probes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from bef_redbird.foundation import Store, lean, native_reuse
from bef_redbird.foundation import registry_selection as selection
from bef_redbird.foundation.native_extension import CONTRACT
from bef_redbird.foundation.records import digest
from bef_redbird.research.group_features import pin
from tests.test_foundation_registry_selection import configuration, release


@pytest.fixture
def extension(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    source, archive, current = (
        tmp_path / n for n in ("prior.duckdb", "originals", "current.duckdb")
    )
    operations = tmp_path / "operations"
    operations.mkdir()
    with Store(source, archive) as store:
        old_iid = release(store, tmp_path, "old", "2020-01-06")
        config = configuration(store, tmp_path, [old_iid], ["2020-01-06"])
        store.add_period(date(2020, 1, 20), date(2020, 2, 3))
        parent = selection.publish(store, config)
    shutil.copyfile(source, current)
    policy = tmp_path / "policy.json"
    lean.write_json(
        policy,
        {
            "version": "registry-canonical-policy-v1",
            "history_from": "2008-01-01",
            "history_through": "latest_verified",
            "sources": ["synthetic-ftp"],
            "source_role": "registry",
            "phases": ["final"],
            "semantic_profiles": config["semantic_profiles"],
            "native_price_value_policy": config["native_price_value_policy"],
        },
    )
    operating = tmp_path / "config.json"
    lean.write_json(
        operating,
        {
            "version": 1,
            "database": str(current),
            "archive_root": str(archive),
            "native_root": str(archive),
            "operation_root": str(operations),
            "registry_policy_config": str(policy),
            "workspace": str(Path(native_reuse.__file__).resolve().parents[3]),
        },
    )
    old_selection = operations / "old-selection.json"
    lean.write_json(old_selection, config)
    parent_pointer = {
        "version": 1,
        "result": parent,
        "configuration_path": str(old_selection),
        "configuration_sha256": lean.file_hash(old_selection),
    }
    lean.write_json(operations / "publication-state.json", parent_pointer)
    parent_file = tmp_path / "parent.json"
    lean.write_json(parent_file, parent_pointer)
    backup = tmp_path / "backup.json"
    lean.write_json(backup, {"files": {"database.duckdb": lean.file_hash(source)}})
    recovery = tmp_path / "recovery.json"
    lean.write_json(recovery, {"complete": True, "store_manifest_sha256": lean.file_hash(backup)})
    restored = tmp_path / "restored.json"
    lean.write_json(
        restored,
        {
            "complete": True,
            "recovery_manifest_sha256": lean.file_hash(recovery),
            "verification": {
                "complete": True,
                "schema_unchanged": True,
                "datasets": [parent["dataset_id"]],
                "operating_status": {"dataset_id": parent["dataset_id"]},
            },
        },
    )
    with Store(current, archive) as store:
        new_iid = release(store, tmp_path, "new", "2020-01-20", price=20000)
        delivery = store.get_record("import_batch", new_iid)["delivery_id"]
        record = store.get_record("source_delivery", str(delivery))
    request = tmp_path / "request.json"
    lean.write_json(
        request,
        {
            "version": CONTRACT,
            "operating_config": pin(operating),
            "policy": pin(policy),
            "prior_database": pin(source),
            "base_backup": pin(backup),
            "base_recovery": pin(recovery),
            "base_restore": pin(restored),
            "parent_publication": pin(parent_file),
            "registry_imports": [new_iid],
            "supplement_imports": [],
            "supply_imports": [],
            "source_deliveries": {str(delivery): digest(selection._serial(record))},
        },
    )
    return request, tmp_path / "proof.json", tmp_path / "receipt.json", current, archive


def test_inherited_publication_equals_full_native_verification(
    extension: tuple, tmp_path: Path
) -> None:
    request, proof, receipt, current, archive = extension
    regular = tmp_path / "regular.duckdb"
    shutil.copyfile(current, regular)
    settings = json.loads(
        Path(json.loads(request.read_text())["operating_config"]["path"]).read_text()
    )
    with Store(regular, archive) as store:
        config = selection.build_configuration(
            store, json.loads(Path(settings["registry_policy_config"]).read_text())
        )
        expected = selection.publish(store, config)
    with patch.object(selection, "_verify_typed", side_effect=AssertionError("old native rehash")):
        actual = native_reuse.publish_verified_extension(request, proof, receipt)
    assert actual["manifest"] == expected["manifest"]
    assert actual["evidence_hash"] == expected["evidence_hash"]
    assert actual["dataset_id"] == expected["dataset_id"]
    before = receipt.read_bytes()
    assert native_reuse.publish_verified_extension(request, proof, receipt) == actual
    assert receipt.read_bytes() == before
    pointer = Path(settings["operation_root"]) / "publication-state.json"
    pointer.write_text(
        Path(json.loads(request.read_text())["parent_publication"]["path"]).read_text()
    )
    assert native_reuse.publish_verified_extension(request, proof, receipt) == actual
    assert json.loads(pointer.read_text())["result"]["dataset_id"] == actual["dataset_id"]


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE registry_lms03 SET aip_ore=aip_ore+1 WHERE aip_ore=12345",
        "UPDATE registry_lms02 SET creation_date=DATE '1999-01-01'",
        "INSERT INTO package VALUES(uuid())",
        "CREATE TABLE unexpected(x INTEGER)",
        "INSERT INTO price_period VALUES(DATE '2020-02-03', DATE '2020-02-17')",
        "UPDATE analytical_dataset SET evidence_hash='changed'",
    ],
)
def test_mutated_old_money_identity_or_unreviewed_additions_fail(
    extension: tuple, mutation: str
) -> None:
    request, proof, receipt, current, _ = extension
    with duckdb.connect(str(current)) as con:
        con.execute(mutation)
    with pytest.raises((ValueError, RuntimeError)):
        native_reuse.publish_verified_extension(request, proof, receipt)
    assert not receipt.exists()


def test_changed_accepted_recovery_closure_rejected(extension: tuple) -> None:
    request, proof, receipt, _, _ = extension
    value = json.loads(request.read_text())
    restore = Path(value["base_restore"]["path"])
    content = json.loads(restore.read_text())
    content["recovery_manifest_sha256"] = "0" * 64
    lean.write_json(restore, content)
    value["base_restore"] = pin(restore)
    lean.write_json(request, value)
    with pytest.raises(ValueError, match="closure"):
        native_reuse.publish_verified_extension(request, proof, receipt)


def test_interrupted_transaction_preserves_parent_pointer(extension: tuple) -> None:
    request, proof, receipt, current, _ = extension
    value = json.loads(request.read_text())
    settings = json.loads(Path(value["operating_config"]["path"]).read_text())
    pointer = Path(settings["operation_root"]) / "publication-state.json"
    before = pointer.read_bytes()
    with patch.object(
        native_reuse._ExtensionAttempt, "consume", side_effect=RuntimeError("interrupt")
    ):
        with pytest.raises(RuntimeError, match="interrupt"):
            native_reuse.publish_verified_extension(request, proof, receipt)
    assert pointer.read_bytes() == before and not receipt.exists()
    with duckdb.connect(str(current), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (1,)


def test_corrupt_old_original_rejected_without_old_row_rehash(extension: tuple) -> None:
    request, proof, receipt, _, archive = extension
    with duckdb.connect(
        json.loads(request.read_text())["prior_database"]["path"], read_only=True
    ) as con:
        relative = con.execute(
            "SELECT archive_location FROM source_delivery WHERE source='synthetic-ftp'"
        ).fetchone()[0]
    (archive / relative).write_bytes(b"corrupt")
    with pytest.raises(Exception, match="Corrupt archive"):
        native_reuse.publish_verified_extension(request, proof, receipt)
    assert not receipt.exists()


@pytest.mark.parametrize("defect", ["missing_proof", "unaudited_import", "changed_semantics"])
def test_missing_audit_or_unreviewed_inputs_fail_closed(
    extension: tuple, tmp_path: Path, defect: str
) -> None:
    request, proof, receipt, current, archive = extension
    value = json.loads(request.read_text())
    if defect == "missing_proof":
        with duckdb.connect(str(current)) as con:
            con.execute(
                "DELETE FROM registry_release_verification WHERE import_id=?",
                [value["registry_imports"][0]],
            )
    elif defect == "unaudited_import":
        with Store(current, archive) as store:
            release(store, tmp_path, "undeclared", "2020-01-21", price=30000)
    else:
        policy = Path(value["policy"]["path"])
        changed = json.loads(policy.read_text())
        changed["semantic_profiles"][0]["monetary_context"]["3.aip_ore"]["tax_basis"] = "unknown"
        lean.write_json(policy, changed)
        value["policy"] = pin(policy)
        lean.write_json(request, value)
    with pytest.raises(ValueError):
        native_reuse.publish_verified_extension(request, proof, receipt)
    assert not receipt.exists()


def test_canonical_extension_module_entrypoint(extension: tuple, tmp_path: Path) -> None:
    request, proof, receipt, _, _ = extension
    source = Path(native_reuse.__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "bef_redbird.foundation.native_extension",
            "--request",
            str(request),
            "--proof",
            str(proof),
            "--receipt",
            str(receipt),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert json.loads(result.stdout.splitlines()[-1])["verified"] is True
    assert json.loads(receipt.read_text())["complete"] is True


def test_new_member_money_tamper_rejected_despite_existing_audit_proof(extension: tuple) -> None:
    request, proof, receipt, current, _ = extension
    new_iid = json.loads(request.read_text())["registry_imports"][0]
    with duckdb.connect(str(current)) as con:
        assert con.execute(
            "SELECT count(*) FROM registry_release_verification WHERE import_id=?", [new_iid]
        ).fetchone() == (1,)
        con.execute("UPDATE registry_lms03 SET aip_ore=20001 WHERE aip_ore=20000")
    with pytest.raises((ValueError, RuntimeError)):
        native_reuse.publish_verified_extension(request, proof, receipt)
    assert not receipt.exists()
