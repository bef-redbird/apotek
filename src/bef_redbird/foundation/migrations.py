"""Ordered, checksummed migrations; each schema change and ledger entry commit together."""

from hashlib import sha256
from pathlib import Path

import duckdb

MIGRATIONS = (
    (1, "reference", Path(__file__).with_name("001_reference.sql")),
    (2, "observations", Path(__file__).with_name("002_observations.sql")),
    (3, "selection", Path(__file__).with_name("003_selection.sql")),
    (4, "archive_prices", Path(__file__).with_name("004_archive_prices.sql")),
    (5, "archive_price_references", Path(__file__).with_name("005_archive_price_references.sql")),
    (6, "registry", Path(__file__).with_name("006_registry.sql")),
    (7, "supplements", Path(__file__).with_name("007_supplements.sql")),
    (8, "registry_selection", Path(__file__).with_name("008_registry_selection.sql")),
    (9, "registry_supply_selection", Path(__file__).with_name("009_registry_supply_selection.sql")),
    (10, "partial_registry", Path(__file__).with_name("010_partial_registry.sql")),
    (
        11,
        "partial_unknown_declarations",
        Path(__file__).with_name("011_partial_unknown_declarations.sql"),
    ),
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
    if len(applied) < len(expected):
        # Finish schema reconstruction before exposing the writer. DuckDB's WAL
        # replay has a known failure around transactional table rebuilds; a
        # normal checkpoint removes that DDL replay dependency. Never remove WAL.
        connection.execute("CHECKPOINT")
