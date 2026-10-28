"""Whole-import fidelity and rollback for the vector publication boundary."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation.source_audit import reconcile
from tests.test_foundation_reports import changes_rows, workbook


def test_bulk_exact_nullable_bigint_rollback_retry_and_independent_scope(tmp_path: Path) -> None:
    rows = changes_rows()
    example = rows.pop()
    for number in range(1, 41):
        row = example.copy()
        row[4] = str(number).zfill(6)
        row[6] = "90.071.992.547.409,93" if number % 2 else None
        row[8] = "bad"
        rows.append(row)
    original = tmp_path / "invented.xlsx"
    original.write_bytes(workbook(rows))
    database, archive = tmp_path / "foundation.duckdb", tmp_path / "originals"
    with Store(database, archive) as store:
        store.add_period(date(2026, 1, 5), date(2026, 1, 19))
        delivery = store.register_source(
            original,
            source="synthetic",
            receipt_key="receipt",
            current_period=date(2026, 1, 5),
        )
        options: dict[str, Any] = {
            "adapter": "price-changes",
            "version": "price-changes-workbook-v1",
            "mapping_version": "synthetic-v1",
            "mapping_policy": "delivery",
            "context": {"tax_basis": "unknown", "fee_basis": "unknown"},
        }
        insert_many = store._insert_many

        def interrupted(table: str, records: list[dict[str, Any]], **kwargs: Any) -> None:
            if table == "price_observation":
                raise OSError("synthetic publication interruption")
            insert_many(table, records, **kwargs)

        with patch.object(store, "_insert_many", interrupted), pytest.raises(OSError):
            store.ingest(delivery, **options)
        assert store._connection.execute("SELECT count(*) FROM package").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM package_observation").fetchone() == (
            0,
        )
        assert store._connection.execute(
            "SELECT validation_status FROM import_batch"
        ).fetchone() == ("running",)
        first = store.ingest(delivery, **options)
        retry = store.ingest(delivery, **options)
        assert first == retry
        prices = store.observations(first["import_id"], "price_observation")
        assert len(prices) == 160
        large = [
            row["amount_ore"]
            for row in prices
            if row["price_type"] == "aip" and row["price_role"] == "effective_price"
        ]
        assert large.count(9007199254740993) == 20
        assert large.count(None) == 20
    proof = reconcile(database, archive, [original], import_ids=[first["import_id"]])
    assert proof["complete"], proof["mismatches"]
    assert proof["verified_count"] == 200
    assert proof["audit_scope"] == {"kind": "explicit_imports", "import_ids": [first["import_id"]]}
    with pytest.raises(ValueError, match="unique"):
        reconcile(database, archive, import_ids=[first["import_id"], first["import_id"]])


def test_late_large_amount_does_not_depend_on_dataframe_sample(tmp_path: Path) -> None:
    rows = changes_rows()
    example = rows.pop()
    for number in range(1, 601):
        row = example.copy()
        row[4] = str(number).zfill(6)
        row[6] = "21.474.836,49" if number == 600 else "1,23"
        rows.append(row)
    original = tmp_path / "invented.xlsx"
    original.write_bytes(workbook(rows))
    with Store(tmp_path / "foundation.duckdb", tmp_path / "originals") as store:
        store.add_period(date(2026, 1, 5), date(2026, 1, 19))
        delivery = store.register_source(
            original,
            source="synthetic",
            receipt_key="late-bigint",
            current_period=date(2026, 1, 5),
        )
        result = store.ingest(
            delivery,
            adapter="price-changes",
            version="price-changes-workbook-v1",
            mapping_version="synthetic-v1",
            mapping_policy="delivery",
            context={"tax_basis": "unknown", "fee_basis": "unknown"},
        )
        large = store._connection.execute(
            "SELECT amount_ore FROM price_observation WHERE canonical_identifier='000600' "
            "AND price_type='aip' AND price_role='effective_price'"
        ).fetchall()
        assert large == [(2147483649,)]
        assert result["accepted_count"] == 3000
