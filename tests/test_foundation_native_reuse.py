"""Synthetic exact-copy proof inheritance; real stores/originals are never opened."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest

from bef_redbird.foundation import Store, lean, native_reuse
from bef_redbird.foundation import registry_selection as selection
from bef_redbird.foundation.records import ArchiveError
from tests.test_foundation_lms_ftp import members
from tests.test_foundation_registry_selection import configuration, release


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    source, archive = tmp_path / "prior.duckdb", tmp_path / "originals"
    with Store(source, archive) as store:
        files = members()
        for name in ("lms02.txt", "lms03.txt"):
            files[name] += files[name].replace(b"000007", b"000008")
        iid = release(store, tmp_path, "one", "2020-01-06", extra_members=files)
        config = configuration(store, tmp_path, [iid], ["2007-12-03", "2020-01-06"])
        parent = selection.publish(store, config)
    recovery = tmp_path / "recovery"
    lean.write_json(
        recovery / "store/backup.json", {"files": {"database.duckdb": lean.file_hash(source)}}
    )
    lean.write_json(
        recovery / "recovery-acceptance.json",
        {
            "complete": True,
            "store_byte_verification": True,
            "verification": {"status": {"publication": {"result": parent}}},
        },
    )
    destination, receipt, seal = (
        tmp_path / "lean.duckdb",
        tmp_path / "copy.json",
        tmp_path / "seal.json",
    )
    lean.prepare(source, archive, destination, recovery, receipt, parent["dataset_id"])
    return source, archive, destination, receipt, seal


def test_module_cli_publishes_using_canonical_opaque_attempt(
    prepared: tuple[Path, Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    _, archive, destination, receipt, seal = prepared
    config = json.loads(receipt.read_text())["configuration"]
    policy = tmp_path / "reviewed-policy.json"
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
    stage_src = Path(native_reuse.__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bef_redbird.foundation.native_reuse",
            "--receipt",
            str(receipt),
            "--seal",
            str(seal),
            "--policy",
            str(policy),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(stage_src)},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    output = json.loads(completed.stdout)
    value = json.loads(receipt.read_text())
    assert output["verified"] is True
    assert value["complete"] is True
    assert value["publication"]["verified"] is True
    assert output["dataset_id"] == value["publication"]["dataset_id"]
    assert output["dataset_id"] != value["binding"]["dataset_id"]
    assert json.loads(seal.read_text())["code"] == native_reuse._code()
    with Store(destination, archive) as store:
        assert selection.status(store, output["dataset_id"], verify=True)["verified"] is True


def test_exact_reuse_matches_full_publication_and_hashes_originals_fresh_once(
    prepared: tuple[Path, Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    _, archive, destination, receipt, seal = prepared
    regular = tmp_path / "regular.duckdb"
    shutil.copyfile(destination, regular)
    value = json.loads(receipt.read_text())
    with Store(regular, archive) as store:
        full = selection.publish(store, value["configuration"])
        expected = selection.query(store, full["dataset_id"], "prices", verify=False)
    original_input = selection._input
    checks: list[bool] = []

    def observed(*args: Any, **kwargs: Any) -> Any:
        checks.append(kwargs.get("verify_original", True))
        return original_input(*args, **kwargs)

    with (
        patch.object(selection, "_input", observed),
        patch.object(
            selection, "_verify_typed", side_effect=AssertionError("typed rehash repeated")
        ),
    ):
        reused = native_reuse.publish_exact_copy(receipt, seal)
    assert checks == [True, False, False]
    assert reused["dataset_id"] == full["dataset_id"]
    assert reused["configuration_hash"] == full["configuration_hash"]
    assert reused["evidence_hash"] == full["evidence_hash"]
    assert reused["manifest"] == full["manifest"]
    assert reused["verification_basis"]["proof_sha256"] == lean.file_hash(seal)
    with Store(destination, archive) as store:
        assert selection.query(store, reused["dataset_id"], "prices", verify=False) == expected
    assert json.loads(receipt.read_text())["completed_utc"]
    operations = tmp_path / "operations"
    config_path, old_policy, new_policy = (
        tmp_path / "config.json",
        tmp_path / "old-policy.json",
        tmp_path / "new-policy.json",
    )
    lean.write_json(
        config_path,
        {
            "database": str(prepared[0]),
            "archive_root": str(archive),
            "operation_root": str(operations),
            "registry_policy_config": str(old_policy),
        },
    )
    lean.write_json(old_policy, {"legacy_imports": ["old"], "supply_imports": []})
    lean.write_json(new_policy, {"legacy_imports": [], "supply_imports": []})
    lean.write_json(
        tmp_path / "source-review/registered-legacy-evidence.json",
        {"legacy_imports": ["old"], "supply_imports": []},
    )
    lean.write_json(operations / "acquisition-state.json", {"files": {}})
    lean.write_json(operations / "publication-state.json", {"old": True})
    assert lean.cutover(
        receipt, config_path, new_policy, tmp_path / "new-originals", tmp_path / "cutover.json"
    )["complete"]
    assert json.loads(config_path.read_text())["database"] == str(destination)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE registry_lms03 SET aip_ore=aip_ore+1",
        "UPDATE registry_lms03 SET aip_ore=NULL",
        "UPDATE source_delivery SET original_uri='different-provenance'",
        "UPDATE registry_member SET row_ids=list_reverse(row_ids) WHERE row_count=2",
        "CREATE TABLE unexpected_inventory(value INTEGER)",
    ],
)
def test_same_counts_value_null_provenance_order_and_schema_corruption_rejected(
    prepared: tuple[Path, Path, Path, Path, Path],
    mutation: str,
) -> None:
    _, _, destination, receipt, seal = prepared
    with duckdb.connect(str(destination)) as con:
        if "row_ids" in mutation:
            # DuckDB implements FK-parent UPDATE as delete/insert. Restore every
            # dependent row unchanged after the deliberate same-count reorder.
            dependencies = {}
            for table in ("registry_release_member", "registry_member_verification"):
                dependencies[table] = con.execute(f"SELECT * FROM {table}").fetchall()
                con.execute(f"DELETE FROM {table}")
        con.execute(mutation)
        if "row_ids" in mutation:
            for table, rows in dependencies.items():
                con.executemany(
                    f"INSERT INTO {table} VALUES({','.join('?' for _ in rows[0])})", rows
                )
    with pytest.raises(ValueError, match="changed|mismatch"):
        native_reuse.publish_exact_copy(receipt, seal)
    assert not seal.exists()


@pytest.mark.parametrize(
    "where", ["parent_hash", "auditor", "configuration", "read_contract", "native_pins"]
)
def test_changed_accepted_parent_configuration_auditor_and_input_pins_rejected(
    prepared: tuple[Path, Path, Path, Path, Path],
    where: str,
) -> None:
    _, _, destination, receipt, seal = prepared
    value = json.loads(receipt.read_text())
    if where == "parent_hash":
        value["binding"]["recovery_acceptance_sha256"] = "f" * 64
    elif where == "auditor":
        value["configuration"]["auditor_version"] = "f" * 64
    elif where == "configuration":
        value["configuration"]["release_import_ids"] = []
    elif where == "native_pins":
        with duckdb.connect(str(destination)) as con:
            con.execute("UPDATE registry_release_verification SET evidence_hash=?", ["f" * 64])
    else:
        with (
            patch.object(selection, "_read_contract_hash", return_value="f" * 64),
            pytest.raises(ValueError),
        ):
            native_reuse.publish_exact_copy(receipt, seal)
        return
    lean.write_json(receipt, value)
    with pytest.raises(ValueError):
        native_reuse.publish_exact_copy(receipt, seal)


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_missing_or_corrupt_original_blocks_reuse(
    prepared: tuple[Path, Path, Path, Path, Path],
    kind: str,
) -> None:
    source, archive, _, receipt, seal = prepared
    with duckdb.connect(str(source), read_only=True) as con:
        sha = con.execute(
            "SELECT d.content_hash FROM registry_release r "
            "JOIN source_delivery d USING(delivery_id)"
        ).fetchone()[0]
    original = archive / "sha256" / sha[:2] / sha
    if kind == "missing":
        original.unlink()
    else:
        original.write_bytes(b"corrupt")
    with pytest.raises(ArchiveError):
        native_reuse.publish_exact_copy(receipt, seal)


def test_guarded_cursor_cannot_write_and_published_choices_roll_back(
    prepared: tuple[Path, Path, Path, Path, Path],
) -> None:
    _, _, destination, receipt, seal = prepared
    original = selection.publish

    def injected(store: Store, config: dict[str, Any], **kwargs: Any) -> Any:
        store._connection.execute("SELECT 1").execute("UPDATE registry_lms03 SET aip_ore=9")
        return original(store, config, **kwargs)

    with (
        patch.object(selection, "publish", injected),
        pytest.raises(ValueError, match="Unrelated write"),
    ):
        native_reuse.publish_exact_copy(receipt, seal)
    with duckdb.connect(str(destination), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (0,)


def test_choices_changed_inside_publication_roll_back_and_seal_retry_is_safe(
    prepared: tuple[Path, Path, Path, Path, Path],
) -> None:
    _, _, destination, receipt, seal = prepared
    original = native_reuse._Guard.executemany

    def changed(guard: Any, sql: str, parameters: Any) -> Any:
        rows = list(parameters)
        if "registry_dataset_period" in sql:
            rows[0] = [*rows[0][:-1], rows[0][-1] + 1]
        return original(guard, sql, rows)

    with (
        patch.object(native_reuse._Guard, "executemany", changed),
        pytest.raises(ValueError, match="choices"),
    ):
        native_reuse.publish_exact_copy(receipt, seal)
    with duckdb.connect(str(destination), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (0,)
    # Rollback/close may change physical checkpoint bytes. Stale seals fail closed.
    if lean.file_hash(destination) == json.loads(seal.read_text())["destination_sha256"]:
        assert native_reuse.publish_exact_copy(receipt, seal)["verified"]
    else:
        with pytest.raises(ValueError, match="Sealed closed"):
            native_reuse.publish_exact_copy(receipt, seal)


@pytest.mark.parametrize("boundary", ["retry", "nested"])
def test_attempt_cannot_cross_store_retry_or_nested_transaction(
    prepared: tuple[Path, Path, Path, Path, Path],
    tmp_path: Path,
    boundary: str,
) -> None:
    _, archive, _, receipt, seal = prepared
    original = selection.publish

    def nested(store: Store, config: dict[str, Any], **kwargs: Any) -> Any:
        attempt = kwargs["_native_reuse"]
        with Store(tmp_path / "unrelated.duckdb", tmp_path / "other-originals") as other:
            with pytest.raises(ValueError, match="bind"):
                attempt.start(other, config)
        if boundary == "retry":
            attempt.start(store, config)
            return original(store, config, **kwargs)
        else:
            with store.transaction():
                return original(store, config, **kwargs)

    expected_error = "retried" if boundary == "retry" else "Unrelated write"
    with (
        patch.object(selection, "publish", nested),
        pytest.raises(ValueError, match=expected_error),
    ):
        native_reuse.publish_exact_copy(receipt, seal)
    with duckdb.connect(str(prepared[2]), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (0,)
    with Store(tmp_path / "default.duckdb", archive) as store:
        with pytest.raises(ValueError, match="Opaque"):
            selection.publish(
                store, json.loads(receipt.read_text())["configuration"], _native_reuse=True
            )  # type: ignore[arg-type]


def test_changed_seal_and_crash_after_commit_fail_closed(
    prepared: tuple[Path, Path, Path, Path, Path],
) -> None:
    _, _, _, receipt, seal = prepared
    original = lean.write_json

    def interrupted(path: Path, value: Any) -> None:
        if path == receipt:
            raise OSError("crash after database commit before final receipt")
        original(path, value)

    with patch.object(lean, "write_json", interrupted), pytest.raises(OSError):
        native_reuse.publish_exact_copy(receipt, seal)
    with pytest.raises(ValueError, match="Sealed closed"):
        native_reuse.publish_exact_copy(receipt, seal)


def test_changed_source_after_copy_and_before_certification_is_rejected(
    prepared: tuple[Path, Path, Path, Path, Path],
) -> None:
    source, _, destination, receipt, seal = prepared
    before = lean.file_hash(destination)
    with duckdb.connect(str(source)) as con:
        con.execute("UPDATE registry_lms03 SET aip_ore=aip_ore+1")
    with pytest.raises(ValueError, match="parent cold"):
        native_reuse.publish_exact_copy(receipt, seal)
    assert lean.file_hash(destination) == before and not seal.exists()


def test_seal_modified_between_certification_and_consume_rolls_back(
    prepared: tuple[Path, Path, Path, Path, Path],
) -> None:
    _, _, destination, receipt, seal = prepared
    original = selection.status

    def changed(*args: Any, **kwargs: Any) -> Any:
        value = json.loads(seal.read_text())
        value["native_inputs_hash"] = "f" * 64
        lean.write_json(seal, value)
        return original(*args, **kwargs)

    with patch.object(selection, "status", changed), pytest.raises(ValueError, match="bind"):
        native_reuse.publish_exact_copy(receipt, seal)
    with duckdb.connect(str(destination), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (0,)


@pytest.mark.parametrize(
    "boundary", ["early_commit", "nested_begin", "caught_invalid", "finished_insert"]
)
def test_guard_transaction_boundaries_cannot_bypass_finish_or_rollback(
    prepared: tuple[Path, Path, Path, Path, Path],
    boundary: str,
) -> None:
    _, _, destination, receipt, seal = prepared
    original = selection.publish

    def injected(store: Store, config: dict[str, Any], **kwargs: Any) -> Any:
        attempt = kwargs["_native_reuse"]
        attempt.start(store, config)
        with store.transaction():
            if boundary == "early_commit":
                store._connection.execute("COMMIT")
            elif boundary == "nested_begin":
                store._connection.execute("BEGIN TRANSACTION")
            elif boundary == "caught_invalid":
                with pytest.raises(ValueError):
                    store._connection.execute("UPDATE registry_lms03 SET aip_ore=1")
                store._connection.execute(
                    "INSERT INTO analytical_dataset VALUES(?,?,?,?,?,?,?)", [attempt.dataset_id] * 7
                )
            else:
                attempt.finished = True
                store._connection.execute(
                    "INSERT INTO analytical_dataset VALUES(?,?,?,?,?,?,?)", [attempt.dataset_id] * 7
                )
        return original(store, config, **kwargs)

    with (
        patch.object(selection, "publish", injected),
        pytest.raises(ValueError, match="Unrelated write"),
    ):
        native_reuse.publish_exact_copy(receipt, seal)
    with duckdb.connect(str(destination), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (0,)
