"""Explicit native-proof inheritance after an exact, locked lean-copy certification.

This is a narrow publication entrypoint, not a general verification cache. Every
attempt repeats the SQL value comparison. A prepublication seal cannot authorize
an ambiguous post-commit retry: changed closed bytes fail before opening Store.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb

from . import lean
from . import registry_selection as selection
from .locking import writer_lock
from .records import digest, identity
from .store import Store

CONTRACT = "locked-exact-copy-native-proof-v1"
_MINT = object()
_TABLES = {
    "analytical_dataset",
    "registry_dataset_release",
    "registry_dataset_period",
    "registry_dataset_member",
    "registry_dataset_legacy",
    "registry_dataset_supply",
}


class _Ownership:
    def __init__(self, source: Path, destination: Path, archive: Path, key: object) -> None:
        if key is not _MINT:
            raise ValueError("Native reuse ownership must be acquired by its factory")
        self.paths = (source, destination, archive)
        self.thread = threading.get_ident()
        self.active = True

    def check(self, store: Store) -> None:
        if (
            not self.active
            or self.thread != threading.get_ident()
            or self.paths[1:] != (store.database, store.archive_root)
        ):
            raise ValueError("Native reuse ownership is not active for this Store")


class _OwnedStore(Store):
    """Borrow only the opaque factory's already-held exact database/archive locks."""

    def __init__(self, ownership: _Ownership) -> None:
        super().__init__(ownership.paths[1], ownership.paths[2])
        self._ownership = ownership

    def __enter__(self) -> _OwnedStore:
        self._ownership.check(self)
        if self._stack is not None:
            raise ValueError("Native reuse Store is already open")
        self._stack = ExitStack()
        try:
            self._open()
            self._owner = threading.get_ident()
        except BaseException:
            self._stack.close()
            self._stack = None
            raise
        return self

    def _open(self) -> None:
        assert self._stack is not None
        self._connection = duckdb.connect(str(self.database))
        self._stack.callback(self._connection.close)
        self._connection.execute("SET TimeZone='UTC'")
        self._connection.execute("SET memory_limit='8GB'")
        self._connection.execute("SET threads=2")
        self._connection.execute("SET max_temp_directory_size='12GB'")

    def closed_hash_and_reopen(self) -> str:
        self._check_owner()
        self._ownership.check(self)
        if self._depth:
            raise ValueError("Certification requires a closed transaction boundary")
        self._connection.execute("CHECKPOINT")
        self._connection.close()
        if Path(str(self.database) + ".wal").exists():
            raise ValueError("Certification requires a closed checkpoint")
        result = lean.file_hash(self.database)
        self._open()
        return result


class _Guard:
    """Only reads, exact publisher INSERT forms and its transaction controls."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, attempt: _NativeAttempt) -> None:
        self.connection, self.attempt = connection, attempt

    def _permit(self, sql: str, parameters: Any, *, many: bool) -> None:
        statements = duckdb.extract_statements(sql)
        if len(statements) != 1:
            self.attempt.invalid = True
            raise ValueError("Native reuse rejects multiple SQL statements")
        kind = str(statements[0].type)
        if kind == "StatementType.SELECT":
            return
        if kind == "StatementType.TRANSACTION":
            command = sql.strip().upper()
            if command == "ROLLBACK":
                return
            if not self.attempt.invalid and self.attempt.started:
                if command == "BEGIN TRANSACTION" and self.attempt.store._depth == 0:
                    return
                if command == "COMMIT" and self.attempt.finished and self.attempt.store._depth == 1:
                    return
        match = re.fullmatch(r"INSERT INTO ([a-z_]+) VALUES\([?,]+\)", sql.strip())
        rows = parameters if many else [parameters]
        if (
            self.attempt.started
            and not self.attempt.invalid
            and not self.attempt.finished
            and self.attempt.store._depth == 1
            and match
            and match[1] in _TABLES
            and all(row and str(row[0]) == self.attempt.dataset_id for row in rows)
        ):
            return
        self.attempt.invalid = True
        raise ValueError("Unrelated write invalidates native proof reuse")

    def execute(self, sql: str, parameters: Any = None) -> Any:
        self._permit(sql, parameters, many=False)
        self.connection.execute(sql, parameters or [])
        return self

    def executemany(self, sql: str, parameters: Any) -> Any:
        rows = list(parameters)
        self._permit(sql, rows, many=True)
        self.connection.executemany(sql, rows)
        return self

    @property
    def description(self) -> Any:
        return self.connection.description

    def fetchone(self) -> Any:
        return self.connection.fetchone()

    def fetchall(self) -> Any:
        return self.connection.fetchall()

    def fetchmany(self, size: int = 1) -> Any:
        return self.connection.fetchmany(size)

    def close(self) -> None:
        self.connection.close()


class _NativeAttempt:
    def __init__(
        self,
        store: _OwnedStore,
        config: dict[str, Any],
        inputs_hash: str,
        proof_hash: str,
        proof_path: Path,
        key: object,
    ) -> None:
        if key is not _MINT:
            raise ValueError("Native proof attempt requires exact-copy certification")
        self.store, self.config_hash, self.inputs_hash = store, digest(config), inputs_hash
        self.proof_hash = proof_hash
        self.proof_path = proof_path
        self.dataset_id = identity("analytical-dataset", {"configuration_hash": self.config_hash})
        self.started = self.consumed = self.invalid = self.finished = False
        self.guard = _Guard(store._connection, self)
        store._connection = cast(duckdb.DuckDBPyConnection, self.guard)

    def start(self, store: Store, config: dict[str, Any]) -> None:
        self._check(store, config)
        if self.started or store._depth:
            self.invalid = True
            raise ValueError("Native proof attempt cannot be retried or nested")
        self.started = True

    def _check(self, store: Store, config: dict[str, Any]) -> None:
        self.store._ownership.check(self.store)
        store._check_owner()
        if (
            store is not self.store
            or store._connection is not self.guard
            or self.invalid
            or digest(config) != self.config_hash
            or lean.file_hash(self.proof_path) != self.proof_hash
        ):
            raise ValueError("Native proof attempt does not bind this Store/configuration")

    def consume(self, store: Store, config: dict[str, Any], inputs: dict[str, Any]) -> None:
        self._check(store, config)
        if not self.started or self.consumed or digest({"inputs": inputs}) != self.inputs_hash:
            self.invalid = True
            raise ValueError("Native proof attempt or input inventory changed")
        self.consumed = True

    def finish(self, store: Store, config: dict[str, Any]) -> None:
        self._check(store, config)
        if not self.consumed:
            raise ValueError("Native proof was not consumed by verification")
        self.finished = True


def _code() -> dict[str, str]:
    return {
        name: lean.file_hash(Path(__file__).with_name(name))
        for name in (
            "native_reuse.py",
            "registry_selection.py",
            "lean.py",
            "store.py",
            "migrations.py",
        )
    }


class _ExtensionAttempt(_NativeAttempt):
    """A one-use proof whose old inputs inherit exact native SQL preservation."""

    def __init__(self, *args: Any, expected_inputs: dict[str, str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.expected_inputs = expected_inputs

    def consume(self, store: Store, config: dict[str, Any], inputs: dict[str, Any]) -> None:
        self._check(store, config)
        actual = {iid: item["input_hash"] for iid, item in inputs.items()}
        if not self.started or self.consumed or actual != self.expected_inputs:
            self.invalid = True
            raise ValueError("Certified append-only native inputs changed")
        self.consumed = True


def publish_verified_extension(
    request_path: Path, proof_path: Path, receipt_path: Path
) -> dict[str, Any]:
    """Certify declared additions and publish while retaining all owning locks.

    No token or general verification bypass escapes this factory. An ambiguous
    interrupted post-commit attempt fails closed; completed retries bind bytes.
    """
    from . import native_extension as extension
    from .archive import external_path, verify
    from .ftp_acquisition import acquisition_lock, load_config, write_json
    from .lms_registry import verify_registry
    from .source_audit import reconcile

    request_path, proof_path, receipt_path = (
        external_path(p) for p in (request_path, proof_path, receipt_path)
    )
    request = json.loads(request_path.read_text())
    if request.get("version") != extension.CONTRACT:
        raise ValueError("Unsupported append-only request")
    config_path = extension.checked(request["operating_config"])
    settings = json.loads(config_path.read_text())
    policy_path = extension.checked(request["policy"])
    if policy_path != Path(settings["registry_policy_config"]).resolve():
        raise ValueError("Operating policy binding changed")
    source = external_path(Path(request["prior_database"]["path"]))
    destination, archive, operations = (
        external_path(Path(settings[key])) for key in ("database", "archive_root", "operation_root")
    )
    if source == destination or proof_path == receipt_path:
        raise ValueError("Separate source, proof and receipt required")
    code = {**_code(), "native_extension.py": lean.file_hash(Path(extension.__file__))}
    binding = {"request_sha256": lean.file_hash(request_path), "code": code}
    with ExitStack() as locks:
        locks.enter_context(writer_lock(operations / "interpretation.writer"))
        locks.enter_context(acquisition_lock(load_config(config_path)))
        for lock in (
            source.with_name(source.name + ".writer"),
            destination.with_name(destination.name + ".writer"),
            archive / ".writer",
        ):
            locks.enter_context(writer_lock(lock))
        for database in (source, destination):
            if Path(str(database) + ".wal").exists():
                raise ValueError("Append-only certification requires closed checkpoints")
        publication_path = operations / "publication-state.json"
        if receipt_path.exists():
            saved = json.loads(receipt_path.read_text())
            if (
                saved.get("complete")
                and saved.get("binding") == binding
                and saved.get("database_after_sha256") == lean.file_hash(destination)
            ):
                if lean.file_hash(proof_path) != saved["proof_sha256"]:
                    raise ValueError("Completed append-only proof changed")
                pointer = saved["publication"]
                if (
                    lean.file_hash(Path(pointer["configuration_path"]))
                    != pointer["configuration_sha256"]
                ):
                    raise ValueError("Completed append-only selection changed")
                current_pointer = json.loads(publication_path.read_text())
                parent_pointer = json.loads(
                    extension.checked(request["parent_publication"]).read_text()
                )
                if current_pointer not in (pointer, parent_pointer):
                    raise ValueError("Publication pointer changed after completion")
                if current_pointer != pointer:
                    write_json(publication_path, pointer)
                return cast(dict[str, Any], saved["result"])
            raise ValueError("Ambiguous append-only retry requires explicit recertification")
        if proof_path.exists():
            raise ValueError("Use new proof/receipt paths for a fresh certification")
        parent, prior_publication = extension.parent_binding(request)
        if json.loads(publication_path.read_text()) != prior_publication:
            raise ValueError("Current publication no longer matches the accepted parent")
        owner = _Ownership(source, destination, archive, _MINT)
        locks.callback(setattr, owner, "active", False)
        # Fresh rechecks are scoped to new originals, never seeded from old members.
        with _OwnedStore(owner) as store:
            con = store._connection
            con.execute("ATTACH '" + str(source).replace("'", "''") + "' AS prior (READ_ONLY)")
            actual = {
                str(row[0])
                for row in con.execute(
                    "SELECT import_id FROM main.import_batch EXCEPT "
                    "SELECT import_id FROM prior.import_batch"
                ).fetchall()
            }
            declared = set(
                request["registry_imports"]
                + request["supplement_imports"]
                + [item["import_id"] for item in request["supply_imports"]]
            )
            if actual != declared:
                raise ValueError("Declared new import inventory differs")
            last = selection._scalar(
                store,
                "SELECT max(period_start) FROM prior.registry_dataset_period WHERE dataset_id=?",
                [parent["dataset_id"]],
            )
            checked_members: set[int] = set()
            for iid in request["registry_imports"]:
                day = selection._scalar(
                    store, "SELECT effective_date FROM registry_release WHERE import_id=?", [iid]
                )
                if day <= last:
                    raise ValueError("New native release could change an old period choice")
                selection._input(
                    store,
                    iid,
                    json.loads(policy_path.read_text()).get("auditor_version"),
                    verify_original=False,
                )
                result = verify_registry(store, iid, recheck=True, checked_members=checked_members)
                if not result["complete"]:
                    raise ValueError("New registry independent recheck failed")
            for iid in request["supplement_imports"]:
                selection._supplement_input(store, iid)
        # The supplier auditor owns a separate genuinely read-only connection.
        for item in request["supply_imports"]:
            original = extension.checked(item["original"])
            audited = reconcile(destination, archive, [original], import_ids=[item["import_id"]])
            if (
                not audited["complete"]
                or audited["mismatches"]
                or audited["quarantined_observation_ids"]
            ):
                raise ValueError("New supplier independent recheck failed")
        with _OwnedStore(owner) as store:
            con = store._connection
            con.execute("ATTACH '" + str(source).replace("'", "''") + "' AS prior (READ_ONLY)")
            current_name = selection._scalar(store, "SELECT current_database()", [])
            inventory = _inventory(con, current_name)
            if inventory != _inventory(con, "prior") or len(inventory["tables"]) != 62:
                raise ValueError("Append-only schema inventory changed")
            old_row = con.execute(
                (
                    "SELECT configuration,configuration_hash,manifest,evidence_hash "
                    "FROM prior.analytical_dataset WHERE dataset_id=?"
                ),
                [parent["dataset_id"]],
            ).fetchone()
            if not old_row:
                raise ValueError("Accepted prior dataset missing")
            old, old_manifest = json.loads(old_row[0]), json.loads(old_row[2])
            if (
                digest(old) != old_row[1]
                or digest(old_manifest) != old_row[3]
                or old_row[1] != parent["configuration_hash"]
                or old_row[3] != parent["evidence_hash"]
                or old_manifest["read_contract_hash"] != selection._read_contract_hash()
            ):
                raise ValueError("Accepted prior publication/read contract changed")
            config = selection.build_configuration(store, json.loads(policy_path.read_text()))
            extension.configuration_scope(
                selection._configuration(old), selection._configuration(config), request
            )
            last = max(p["period_start"] for p in old["period_bindings"])
            if any(
                str(
                    selection._scalar(
                        store,
                        "SELECT effective_date FROM registry_release WHERE import_id=?",
                        [iid],
                    )
                )
                <= last
                for iid in request["registry_imports"]
            ):
                raise ValueError("New native release could change an old period choice")
            comparisons = extension.certify_rows(store, request)
            originals = con.execute(
                "SELECT DISTINCT archive_location,content_hash,byte_count "
                "FROM source_delivery "
                "ORDER BY archive_location"
            ).fetchall()
            for relative, sha, size in originals:
                path = archive / relative
                if (
                    path.is_symlink()
                    or path.is_junction()
                    or path.resolve().parent != (archive / "sha256" / sha[:2]).resolve()
                    or path.name != sha
                ):
                    raise ValueError("Original archive path changed")
                verify(path, sha, size)
            expected = {
                str(iid): h
                for iid, h in con.execute(
                    (
                        "SELECT import_id,input_hash FROM prior.registry_dataset_release "
                        "WHERE dataset_id=?"
                    ),
                    [parent["dataset_id"]],
                ).fetchall()
            }
            for iid in request["registry_imports"]:
                expected[iid] = selection._input(
                    store, iid, config.get("auditor_version"), verify_original=False
                )["input_hash"]
            for item in request["supply_imports"]:
                selection._supply_input(
                    store, {k: item[k] for k in ("import_id", "verification_delivery_id")}
                )
            closed = store.closed_hash_and_reopen()
            proof = {
                "contract": extension.CONTRACT,
                "binding": binding,
                "parent_dataset": parent["dataset_id"],
                "configuration_hash": digest(selection._configuration(config)),
                "old_rows": comparisons,
                "original_objects_verified": len(originals),
                "expected_inputs": expected,
                "destination_sha256": closed,
                "created_utc": datetime.now(UTC).isoformat(),
            }
            lean.write_json(proof_path, proof)
            attempt = _ExtensionAttempt(
                store,
                config,
                "scoped-per-input-hashes",
                lean.file_hash(proof_path),
                proof_path,
                _MINT,
                expected_inputs=expected,
            )
            try:
                result = selection.publish(store, config, _native_reuse=attempt)
            finally:
                attempt.invalid = True
            result["verification_basis"] = {
                "kind": extension.CONTRACT,
                "proof_sha256": lean.file_hash(proof_path),
            }
        frozen = operations / "selections" / (digest(config) + ".json")
        # Use the canonical serializer: configuration hashes are not presentation hashes.
        from .records import canonical_json

        encoded = (canonical_json(config) + "\n").encode()
        frozen.parent.mkdir(parents=True, exist_ok=True)
        if frozen.exists() and frozen.read_bytes() != encoded:
            raise ValueError("Frozen append-only selection changed")
        if not frozen.exists():
            frozen.write_bytes(encoded)
        pointer = {
            "version": 1,
            "configuration_path": str(frozen),
            "configuration_sha256": lean.file_hash(frozen),
            "completed_utc": datetime.now(UTC).isoformat(),
            "result": result,
        }
        completed = {
            "complete": True,
            "binding": binding,
            "proof_sha256": lean.file_hash(proof_path),
            "database_after_sha256": lean.file_hash(destination),
            "result": result,
            "publication": pointer,
        }
        # Receipt precedes the mutable pointer. If pointer publication is interrupted,
        # a completed retry repairs it from this exact committed evidence.
        lean.write_json(receipt_path, completed)
        write_json(publication_path, pointer)
        return result


def _inventory(connection: duckdb.DuckDBPyConnection, database: str) -> dict[str, Any]:
    tables = connection.execute(
        "SELECT table_name,sql FROM duckdb_tables() WHERE database_name=? ORDER BY table_name",
        [database],
    ).fetchall()
    columns = connection.execute(
        "SELECT table_name,column_name,column_index,data_type,is_nullable,column_default "
        "FROM duckdb_columns() WHERE database_name=? ORDER BY table_name,column_index",
        [database],
    ).fetchall()
    return {"tables": tables, "columns": columns}


def publish_exact_copy(receipt: Path, seal: Path, policy: Path | None = None) -> dict[str, Any]:
    """Own, recertify, seal and immediately publish; no token escapes this call."""
    value = json.loads(receipt.read_text())
    if not value.get("copy_complete") or value.get("complete"):
        raise ValueError("Use an unfinished exact lean copy, not a publication/retry receipt")
    binding = value["binding"]
    source, destination, archive, recovery = [
        Path(binding[name]).resolve()
        for name in (
            "source",
            "destination",
            "archive",
            "recovery",
        )
    ]
    if source == destination:
        raise ValueError("Source and destination must be distinct")
    with ExitStack() as locks:
        for lock in (
            source.with_name(source.name + ".writer"),
            destination.with_name(destination.name + ".writer"),
            archive / ".writer",
        ):
            locks.enter_context(writer_lock(lock))
        owner = _Ownership(source, destination, archive, _MINT)
        locks.callback(setattr, owner, "active", False)
        for database in (source, destination):
            if Path(str(database) + ".wal").exists():
                raise ValueError("Native reuse requires closed, checkpointed databases")
        backup_path, acceptance_path = (
            recovery / "store/backup.json",
            recovery / "recovery-acceptance.json",
        )
        backup, accepted = [json.loads(p.read_text()) for p in (backup_path, acceptance_path)]
        if (
            lean.file_hash(backup_path) != binding["recovery_manifest_sha256"]
            or lean.file_hash(acceptance_path) != binding["recovery_acceptance_sha256"]
            or not accepted.get("complete")
            or not accepted.get("store_byte_verification")
            or lean.file_hash(source) != binding["source_sha256"]
            or backup["files"]["database.duckdb"] != binding["source_sha256"]
        ):
            raise ValueError("Accepted parent cold recovery binding changed")
        parent = accepted["verification"]["status"]["publication"]["result"]
        if not parent.get("verified") or parent["dataset_id"] != binding["dataset_id"]:
            raise ValueError("Parent publication was not accepted as verified")
        existing = json.loads(seal.read_text()) if seal.exists() else None
        if existing and (
            existing.get("contract") != CONTRACT
            or existing.get("code") != _code()
            or existing.get("binding") != binding
            or existing.get("destination_sha256") != lean.file_hash(destination)
        ):
            raise ValueError("Sealed closed destination or proof pins changed")
        with _OwnedStore(owner) as store:
            con = store._connection
            con.execute("ATTACH '" + str(source).replace("'", "''") + "' AS prior (READ_ONLY)")
            con.execute("SET memory_limit='8GB'")
            con.execute("SET threads=2")
            parent_row = con.execute(
                "SELECT configuration,configuration_hash,manifest,evidence_hash "
                "FROM prior.analytical_dataset WHERE dataset_id=?",
                [binding["dataset_id"]],
            ).fetchone()
            if not parent_row:
                raise ValueError("Accepted parent dataset missing")
            parent_config, parent_manifest = json.loads(parent_row[0]), json.loads(parent_row[2])
            if (
                digest(parent_config) != parent_row[1]
                or digest(parent_manifest) != parent_row[3]
                or parent_row[1] != parent["configuration_hash"]
                or parent_row[3] != parent["evidence_hash"]
                or parent_manifest["read_contract_hash"] != selection._read_contract_hash()
            ):
                raise ValueError("Accepted parent publication metadata changed")
            config = selection._configuration(value["configuration"])
            if policy is not None:
                config = selection.build_configuration(store, json.loads(policy.read_text()))
            expected = selection._configuration(parent_config)
            expected["legacy_imports"], expected["legacy_selection_ids"] = [], []
            expected["period_bindings"] = [
                p for p in expected["period_bindings"] if p["period_start"] >= "2008-01-01"
            ]
            if {k: v for k, v in config.items() if k != "builder_provenance"} != {
                k: v for k, v in expected.items() if k != "builder_provenance"
            }:
                raise ValueError("Reviewed lean native/configuration scope changed")
            current = con.execute("SELECT current_database()").fetchone()
            assert current is not None
            before, after = _inventory(con, "prior"), _inventory(con, current[0])
            if before != after or len(before["tables"]) != 62:
                raise ValueError("Exact table/schema inventory changed")
            predicates = lean.retention(con, config)
            if set(predicates) != {row[0] for row in before["tables"]}:
                raise ValueError("Retention table inventory changed")
            comparisons = {}
            for table, predicate in predicates.items():
                expected_table = value["expected"][table]
                if expected_table["predicate"] != predicate:
                    raise ValueError("Reviewed retention predicate changed")
                count = lean.exact_compare(con, table, predicate)
                if count != expected_table["retained"]:
                    raise ValueError("Reviewed retention count changed")
                comparisons[table] = {"rows": count, "predicate": predicate}
            inputs = {
                iid: selection._input(store, iid, config.get("auditor_version"))
                for iid in config["release_import_ids"]
            }
            inputs_hash = digest({"inputs": inputs})
            if inputs_hash != parent_manifest["inputs_hash"]:
                raise ValueError("Accepted native input inventory/auditor changed")
            closed_hash = store.closed_hash_and_reopen()
            proof = {
                "contract": CONTRACT,
                "binding": binding,
                "code": _code(),
                "configuration_hash": digest(config),
                "parent_configuration_hash": parent_row[1],
                "parent_evidence_hash": parent_row[3],
                "native_inputs_hash": inputs_hash,
                "read_contract_hash": selection._read_contract_hash(),
                "schema_hash": digest(selection._serial(before)),
                "comparisons": comparisons,
                "destination_sha256": closed_hash,
            }
            if existing is not None and existing != proof:
                raise ValueError("Immutable exact-copy seal changed")
            if existing is None:
                lean.write_json(seal, proof)
            attempt = _NativeAttempt(store, config, inputs_hash, lean.file_hash(seal), seal, _MINT)
            try:
                result = selection.publish(store, config, _native_reuse=attempt)
            finally:
                attempt.invalid = True
            result["verification_basis"] = {"kind": CONTRACT, "proof_sha256": attempt.proof_hash}
        value.update(
            configuration=config,
            publication=result,
            complete=True,
            destination_sha256=lean.file_hash(destination),
            completed_utc=datetime.now(UTC).isoformat(),
        )
        lean.write_json(receipt, value)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("receipt", "seal"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()
    result = publish_exact_copy(args.receipt, args.seal, args.policy)
    print(json.dumps({"dataset_id": result["dataset_id"], "verified": result["verified"]}))


if __name__ == "__main__":
    # Keep the opaque attempt class identical to the canonical module imported
    # by registry_selection, including when this file is launched with -m.
    from bef_redbird.foundation.native_reuse import main as canonical_main

    canonical_main()
