"""The supported single-owner write boundary for provenance and typed assertions."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import duckdb

from . import archive, migrations
from .locking import writer_lock
from .records import (
    ArchiveError,
    FoundationError,
    Timing,
    canonical_identifier,
    canonical_json,
    digest,
    identity,
    manifest,
    required,
)

TABLE_KEYS = {
    "price_period": "period_start",
    "package": "package_id",
    "source_delivery": "delivery_id",
    "import_batch": "import_id",
    "package_identifier": "mapping_id",
    "import_issue": "issue_id",
}
UNKNOWN_TIMING = Timing()


class Store:
    """Use in a `with` block. One instance, process and thread owns all writes.

    Database transactions may group operations. Filesystem publication is not
    rolled back with SQL: a failed registration can leave a harmless orphan.
    """

    def __init__(self, database: Path, archive_root: Path) -> None:
        self.database = archive.external_path(database)
        self.archive_root = archive.external_path(archive_root)
        if self.database.is_relative_to(self.archive_root):
            raise ValueError("Database must be outside the original-byte archive")
        self._stack: ExitStack | None = None
        self._connection: duckdb.DuckDBPyConnection
        self._owner = 0
        self._depth = 0
        self._rollback_only = False

    def __enter__(self) -> Store:
        if self._stack is not None:
            raise FoundationError("Store is already open")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            stack.enter_context(
                writer_lock(self.database.with_name(self.database.name + ".writer"))
            )
            # Also prevent two different databases from concurrently owning the same archive.
            stack.enter_context(writer_lock(self.archive_root / ".writer"))
            self._connection = duckdb.connect(str(self.database))
            stack.callback(self._connection.close)
            self._connection.execute("SET TimeZone='UTC'")
            migrations.migrate(self._connection)
            self._owner = threading.get_ident()
            self._stack = stack.pop_all()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._check_owner()
        assert self._stack is not None
        try:
            self._stack.close()
        finally:
            self._stack = None

    def _check_owner(self) -> None:
        if self._stack is None or threading.get_ident() != self._owner:
            raise FoundationError("Use the open Store only from its owning thread")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._check_owner()
        outer = self._depth == 0
        if outer:
            self._connection.execute("BEGIN TRANSACTION")
            self._rollback_only = False
        self._depth += 1
        try:
            yield
            if outer:
                if self._rollback_only:
                    raise FoundationError("Transaction contains a failed operation")
                self._connection.execute("COMMIT")
        except BaseException:
            self._rollback_only = True
            if outer:
                self._connection.execute("ROLLBACK")
            raise
        finally:
            self._depth -= 1

    def get_record(self, table: str, key: str | date) -> dict[str, Any]:
        """Read one provenance record without exposing a general SQL write interface."""
        self._check_owner()
        if table not in TABLE_KEYS:
            raise ValueError("Unsupported foundation table")
        result = self._connection.execute(
            f"SELECT * FROM {table} WHERE {TABLE_KEYS[table]} = ?", [key]
        )
        row = result.fetchone()
        if row is None:
            raise ValueError("Foundation record does not exist")
        return dict(zip([column[0] for column in result.description], row, strict=True))

    def _insert(self, table: str, record: dict[str, Any]) -> None:
        columns = ", ".join(record)
        parameters = ", ".join("?" for _ in record)
        self._connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({parameters})", list(record.values())
        )

    def _insert_many(
        self, table: str, records: list[dict[str, Any]], *, existing_ok: bool = False
    ) -> None:
        """Bounded vector insertion inside the caller's publication transaction.

        Object dtype is essential: nullable integer money must never pass through
        a float64 dataframe column. DuckDB still enforces every scalar/FK constraint.
        """
        import pandas as pd

        self._check_owner()
        if not self._depth:
            raise FoundationError("Bulk insertion requires a publication transaction")
        for offset in range(0, len(records), 4096):
            batch = records[offset : offset + 4096]
            columns = list(batch[0])
            if any(list(record) != columns for record in batch):
                raise FoundationError("Bulk records have inconsistent columns")
            frame = pd.DataFrame(batch, dtype=object)
            if table == "price_observation":
                # DuckDB samples object columns. Early small values otherwise
                # infer INTEGER and reject a valid later BIGINT. An explicit
                # nullable extension dtype keeps the complete signed-64 range.
                frame["amount_ore"] = pd.array(
                    [record["amount_ore"] for record in batch], dtype="Int64"
                )
            self._connection.register("_foundation_publication", frame)
            try:
                names = ", ".join(columns)
                conflict = " ON CONFLICT DO NOTHING" if existing_ok else ""
                self._connection.execute(
                    f"INSERT INTO {table} ({names}) SELECT {names} "
                    f"FROM _foundation_publication{conflict}"
                )
            finally:
                self._connection.unregister("_foundation_publication")

    def _exists(self, table: str, key: str) -> bool:
        return (
            self._connection.execute(
                f"SELECT 1 FROM {table} WHERE {TABLE_KEYS[table]} = ?", [key]
            ).fetchone()
            is not None
        )

    def add_period(self, start: date, end_exclusive: date) -> None:
        with self.transaction():
            if type(start) is not date or type(end_exclusive) is not date or start >= end_exclusive:
                raise ValueError("Price periods require increasing business dates")
            existing = self._connection.execute(
                "SELECT period_end_exclusive FROM price_period WHERE period_start = ?", [start]
            ).fetchone()
            if existing:
                if existing[0] != end_exclusive:
                    raise ValueError("Conflicting price period")
                return
            if self._connection.execute(
                "SELECT 1 FROM price_period WHERE period_start < ? AND period_end_exclusive > ?",
                [end_exclusive, start],
            ).fetchone():
                raise ValueError("Price periods cannot overlap")
            self._connection.execute(
                "INSERT INTO price_period VALUES (?, ?)", [start, end_exclusive]
            )

    def create_package(self, package_id: str | None = None) -> str:
        with self.transaction():
            package_id = str(UUID(package_id)) if package_id is not None else str(uuid4())
            if not self._exists("package", package_id):
                self._connection.execute("INSERT INTO package VALUES (?)", [package_id])
            return package_id

    def register_source(
        self,
        path: Path,
        *,
        source: str,
        receipt_key: str,
        original_uri: str | None = None,
        received: Timing = UNKNOWN_TIMING,
        published: Timing = UNKNOWN_TIMING,
        edition: str | None = None,
        phase: str | None = None,
        current_period: date | None = None,
        target_period: date | None = None,
        coverage: dict[str, Any] | None = None,
        replacement: dict[str, Any] | None = None,
    ) -> str:
        with self.transaction():
            required(source)
            required(receipt_key)
            received_values = received.values()
            published_values = published.values()
            metadata = {
                "source": source,
                "receipt_key": receipt_key,
                "original_filename": path.name,
                "original_uri": original_uri,
                "received": received.metadata(),
                "published": published.metadata(),
                "edition": edition,
                "phase": phase,
                "current_period": current_period.isoformat() if current_period else None,
                "target_period": target_period.isoformat() if target_period else None,
                "coverage": json.loads(manifest(coverage)),
                "replacement": json.loads(manifest(replacement)),
            }
            content_hash, location, size = archive.archive_file(self.archive_root, path)
            metadata["content_hash"] = content_hash
            metadata_hash = digest(metadata)
            delivery_id = identity("delivery", {"source": source, "receipt_key": receipt_key})
            if self._exists("source_delivery", delivery_id):
                if (
                    self.get_record("source_delivery", delivery_id)["metadata_hash"]
                    != metadata_hash
                ):
                    raise ValueError(
                        "Receipt key already registered with different bytes or metadata"
                    )
                return delivery_id
            self._insert(
                "source_delivery",
                {
                    "delivery_id": delivery_id,
                    "source": source,
                    "receipt_key": receipt_key,
                    "metadata_hash": metadata_hash,
                    "original_filename": path.name,
                    "original_uri": original_uri,
                    "content_hash": content_hash,
                    "archive_location": location,
                    "byte_count": size,
                    **self._timing_columns("received", received_values),
                    **self._timing_columns("published", published_values),
                    "edition": edition,
                    "phase": phase,
                    "current_period": current_period,
                    "target_period": target_period,
                    "coverage": manifest(coverage),
                    "replacement": manifest(replacement),
                    "registered_at": datetime.now(UTC),
                },
            )
            return delivery_id

    @staticmethod
    def _timing_columns(prefix: str, values: tuple[Any, ...]) -> dict[str, Any]:
        return dict(
            zip(
                [
                    f"{prefix}_{suffix}"
                    for suffix in ("lower", "upper", "precision", "basis", "timezone")
                ],
                values,
                strict=True,
            )
        )

    def original_path(self, delivery_id: str) -> Path:
        record = self.get_record("source_delivery", delivery_id)
        path = archive.archive_path(self.archive_root, record["content_hash"])
        archive.verify(path, record["content_hash"], record["byte_count"])
        return path

    def verify_archive(self) -> dict[str, int]:
        """Verify unique referenced objects; report safe counts, never original names."""
        self._check_owner()
        counts = {"ok": 0, "missing": 0, "corrupt": 0, "orphan": 0, "staging": 0}
        records = self._connection.execute(
            "SELECT DISTINCT content_hash, byte_count FROM source_delivery"
        ).fetchall()
        referenced = {content_hash for content_hash, _ in records}
        for content_hash, size in records:
            try:
                path = archive.archive_path(self.archive_root, content_hash)
                if not path.exists():
                    counts["missing"] += 1
                    continue
                archive.verify(path, content_hash, size)
            except ArchiveError:
                counts["corrupt"] += 1
            else:
                counts["ok"] += 1
        counts["orphan"] = sum(
            path.is_file() and path.name not in referenced
            for path in (self.archive_root / "sha256").glob("*/*")
        )
        counts["staging"] = sum(1 for _ in (self.archive_root / ".staging").glob("*"))
        return counts

    def begin_import(
        self,
        delivery_id: str,
        *,
        parser_version: str,
        schema_version: str,
        mapping_version: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        with self.transaction():
            self.original_path(delivery_id)
            context = {} if context is None else context
            record = {
                "delivery_id": str(UUID(delivery_id)),
                "parser_version": required(parser_version),
                "schema_version": required(schema_version),
                "mapping_version": required(mapping_version),
                "interpretation_context_hash": digest(context),
            }
            import_id = identity("import", record)
            if not self._exists("import_batch", import_id):
                self._insert(
                    "import_batch",
                    {
                        "import_id": import_id,
                        **record,
                        "interpretation_context": canonical_json(context),
                        "processing_at": datetime.now(UTC),
                        "validation_status": "running",
                    },
                )
            return import_id

    def finish_import(
        self,
        import_id: str,
        *,
        status: str,
        record_count: int,
        accepted_count: int,
        rejected_count: int,
    ) -> None:
        with self.transaction():
            counts = (record_count, accepted_count, rejected_count)
            if status not in {"accepted", "rejected", "failed"}:
                raise ValueError("Unsupported completed import status")
            if any(type(count) is not int or count < 0 for count in counts):
                raise ValueError("Import counts must be nonnegative integers")
            if accepted_count + rejected_count != record_count:
                raise ValueError("Import counts must account for every record")
            previous = self.get_record("import_batch", import_id)
            if previous["validation_status"] != "running":
                if (status, *counts) != (
                    previous["validation_status"],
                    previous["record_count"],
                    previous["accepted_count"],
                    previous["rejected_count"],
                ):
                    raise ValueError("Completed import is immutable")
                return
            if (
                status == "accepted"
                and self._connection.execute(
                    "SELECT 1 FROM import_issue WHERE import_id = ? AND blocking_scope = 'import'",
                    [import_id],
                ).fetchone()
            ):
                raise ValueError("Import has a blocking issue")
            self._connection.execute(
                """UPDATE import_batch SET validation_status = ?, record_count = ?,
                    accepted_count = ?, rejected_count = ?, completed_at = ?
                    WHERE import_id = ?""",
                [status, *counts, datetime.now(UTC), import_id],
            )

    def add_issue(
        self,
        import_id: str,
        *,
        source_locator: str,
        code: str,
        severity: str,
        blocking_scope: str,
        identifier: str | None = None,
        field: str | None = None,
        period: date | None = None,
    ) -> str:
        with self.transaction():
            record = {
                "import_id": str(UUID(import_id)),
                "source_locator": required(source_locator),
                "code": required(code),
                "severity": severity,
                "blocking_scope": blocking_scope,
                "identifier": required(identifier) if identifier is not None else None,
                "field": required(field) if field is not None else None,
                "period": period.isoformat() if period else None,
            }
            issue_id = identity("issue", record)
            if self._exists("import_issue", issue_id):
                return issue_id
            if self.get_record("import_batch", import_id)["validation_status"] != "running":
                raise ValueError("Cannot add issues to a completed import")
            self._insert("import_issue", {"issue_id": issue_id, **record})
            return issue_id

    def add_identifier(
        self,
        package_id: str | None,
        *,
        namespace: str,
        identifier_value: str,
        evidence_import_id: str,
        source_locator: str,
        mapping_version: str,
        validity_kind: str = "unknown",
        valid_from: date | None = None,
        valid_to: date | None = None,
        scope_key: str | None = None,
        available: Timing = UNKNOWN_TIMING,
    ) -> str:
        with self.transaction():
            canonical = canonical_identifier(namespace, identifier_value)
            record = {
                "package_id": str(UUID(package_id)) if package_id is not None else None,
                "namespace": namespace,
                "identifier_value": identifier_value,
                "canonical_value": canonical,
                "validity_kind": validity_kind,
                "valid_from": valid_from.isoformat() if valid_from else None,
                "valid_to": valid_to.isoformat() if valid_to else None,
                "scope_key": scope_key,
                "evidence_import_id": str(UUID(evidence_import_id)),
                "source_locator": required(source_locator),
                "mapping_version": required(mapping_version),
            }
            mapping_id = identity("mapping", {**record, "available": available.metadata()})
            if self._exists("package_identifier", mapping_id):
                return mapping_id
            if self.get_record("import_batch", evidence_import_id)["validation_status"] in {
                "rejected",
                "failed",
            }:
                raise ValueError("Mapping evidence import is rejected or failed")
            # Only comparable, evidenced scopes can contradict each other. Different
            # mapping versions remain alternatives; lookup requires an explicit version.
            conflicts = self._connection.execute(
                """SELECT m.package_id FROM package_identifier m
                    JOIN import_batch i ON i.import_id = m.evidence_import_id
                    WHERE namespace = ? AND canonical_value = ? AND m.mapping_version = ?
                    AND i.validation_status IN ('running', 'accepted')
                    AND ((validity_kind = 'interval' AND ? = 'interval'
                          AND valid_from < ? AND valid_to > ?)
                         OR (validity_kind = 'scope' AND ? = 'scope' AND scope_key = ?))""",
                [
                    namespace,
                    canonical,
                    mapping_version,
                    validity_kind,
                    valid_to,
                    valid_from,
                    validity_kind,
                    scope_key,
                ],
            ).fetchall()
            if any(
                (str(existing[0]) if existing[0] is not None else None) != record["package_id"]
                for existing in conflicts
            ):
                raise ValueError("Ambiguous overlapping identifier mapping")
            self._insert(
                "package_identifier",
                {
                    "mapping_id": mapping_id,
                    **record,
                    **self._timing_columns("available", available.values()),
                },
            )
            return mapping_id

    def resolve_identifier(
        self,
        namespace: str,
        identifier_value: str,
        *,
        mapping_version: str,
        on_date: date | None = None,
        scope_key: str | None = None,
    ) -> str | None:
        """Resolve evidenced scope only. This is not decision-cutoff source selection."""
        self._check_owner()
        canonical = canonical_identifier(namespace, identifier_value)
        rows = self._connection.execute(
            """SELECT DISTINCT m.package_id FROM package_identifier m
                JOIN import_batch i ON i.import_id = m.evidence_import_id
                WHERE namespace = ? AND canonical_value = ? AND m.mapping_version = ?
                AND i.validation_status = 'accepted'
                AND ((validity_kind = 'interval' AND valid_from <= ? AND valid_to > ?)
                     OR (validity_kind = 'scope' AND scope_key = ?))""",
            [namespace, canonical, required(mapping_version), on_date, on_date, scope_key],
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError("Ambiguous identifier scope")
        return str(rows[0][0]) if rows[0][0] is not None else None

    def ingest(
        self,
        delivery_id: str,
        *,
        adapter: str,
        version: str,
        mapping_version: str,
        context: dict[str, Any],
        mapping_policy: str = "existing",
        mapping_scope: str | None = None,
    ) -> dict[str, Any]:
        """Publish one explicit derivation; retry identical inputs without duplication."""
        from .ingestion import ingest

        return ingest(
            self,
            delivery_id,
            adapter=adapter,
            version=version,
            mapping_version=mapping_version,
            context=context,
            mapping_policy=mapping_policy,
            mapping_scope=mapping_scope,
        )

    def import_summary(self, import_id: str) -> dict[str, Any]:
        """Safe counts and issue codes only: no private values, filenames or locators."""
        from .ingestion import import_summary

        return import_summary(self, import_id)

    def observations(self, import_id: str, table: str) -> list[dict[str, Any]]:
        """Read one accepted derivation explicitly; verify original bytes before use."""
        from .ingestion import observations

        return observations(self, import_id, table)

    def issues(self, import_id: str) -> list[dict[str, Any]]:
        """Local-only detailed issues with actionable source locators."""
        self.get_record("import_batch", import_id)
        result = self._connection.execute(
            "SELECT * FROM import_issue WHERE import_id = ? ORDER BY source_locator, code",
            [import_id],
        )
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]

    def create_selection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        """Freeze explicitly reconciled, scoped evidence under a versioned policy."""
        from .selection import create_selection

        return create_selection(self, configuration)

    def inspect_selection(self, selection_id: str) -> dict[str, Any]:
        """Read the immutable artifact and verify its hashes and archived evidence."""
        from .selection import inspect_selection

        return inspect_selection(self, selection_id)
