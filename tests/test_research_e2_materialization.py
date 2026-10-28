"""Independent exact typed-value check across Arrow batches and partitions."""

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bef_redbird.research.e2_data import ResearchData
from bef_redbird.research.training_contract import FEATURE_SCHEMA, LABEL_SCHEMA, atomic_json


def _table(schema: pa.Schema, partition: int, count: int) -> pa.Table:
    columns = {}
    for field in schema:
        if field.name == "row_key":
            values = [f"partition-{partition}-{index:06}" for index in range(count)]
        elif field.name == "package_number":
            values = [f"{index % 1000000:06}" for index in range(count)]
        else:
            if pa.types.is_string(field.type):
                pattern = ["", "M:", "V:literal", "nan", "None", "000001", "æØ漢"]
            elif pa.types.is_date32(field.type):
                pattern = [date(2008, 1, 14), date(2024, 2, 19), date(2026, 10, 12)]
            elif pa.types.is_boolean(field.type):
                pattern = [False, True, True]
            elif pa.types.is_floating(field.type):
                pattern = [0.0, 0.125, 1.0]
            elif pa.types.is_int64(field.type):
                pattern = [0, 9007199254740993, 9223372036854775806, -9007199254740993]
            else:
                pattern = [0, 1, -1]
            if field.nullable:
                pattern = [None, *pattern]
            values = (pattern * (count // len(pattern) + 1))[:count]
        columns[field.name] = pa.array(values, type=field.type)
    return pa.Table.from_pydict(columns, schema=schema)


def test_arrow_materialization_preserves_every_typed_value_across_batches_and_partitions(
    tmp_path: Path,
) -> None:
    count = 65539  # Forces a second65536-sized batch inside each partition.
    expected = {"features": [], "labels": []}
    files = []
    for partition in range(2):
        folder = tmp_path / f"part-{partition:04}"
        folder.mkdir()
        for role, schema in (("features", FEATURE_SCHEMA), ("labels", LABEL_SCHEMA)):
            table = _table(schema, partition, count)
            pq.write_table(table, folder / f"{role}.parquet", row_group_size=12000)
            expected[role].append(table)
            files.append({"path": f"{folder.name}/{role}.parquet"})
    manifest = tmp_path / "dataset.json"
    atomic_json(manifest, {"rows": count * 2, "files": files})
    data = ResearchData(manifest, scratch=tmp_path / "scratch")
    try:
        for role in ("features", "labels"):
            original = pa.concat_tables(expected[role]).sort_by("row_key")
            if role == "features":
                original = original.drop(["provenance"])
                query = "SELECT * EXCLUDE(provenance) FROM features ORDER BY row_key"
            else:
                query = "SELECT * FROM labels ORDER BY row_key"
            materialized = data.connection.execute(query).to_arrow_table()
            assert materialized.column_names == original.column_names
            assert materialized.num_rows == original.num_rows == count * 2
            for name in original.column_names:
                assert (
                    materialized[name].combine_chunks().equals(original[name].combine_chunks())
                ), name
    finally:
        data.close()
    assert data.spill is not None and not data.spill.exists()
