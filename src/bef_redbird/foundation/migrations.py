"""Ordered, checksummed migrations; each schema change and ledger entry commit together."""

from hashlib import sha256
from pathlib import Path

import duckdb

MIGRATIONS = (
    (1, "reference", Path(__file__).with_name("001_reference.sql")),
    (2, "observations", Path(__file__).with_name("002_observations.sql")),
)


def migrate(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migration (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                name VARCHAR NOT NULL,
                checksum VARCHAR NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL
            )
        """)
        applied = connection.execute(
            "SELECT version, name, checksum FROM schema_migration ORDER BY version"
        ).fetchall()
        expected = [
            (version, name, sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest())
            for version, name, path in MIGRATIONS
        ]
        if applied != expected[: len(applied)]:
            raise ValueError("Unknown or modified schema migration history")
        for (version, name, path), (_, _, checksum) in zip(
            MIGRATIONS[len(applied) :], expected[len(applied) :], strict=True
        ):
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migration VALUES (?, ?, ?, current_timestamp)",
                [version, name, checksum],
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
