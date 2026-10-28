"""E2 frozen research access: candidates precede separately attached outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .dataset import ResearchError, digest
from .training_contract import FEATURES, KEYS, VERSION


def verify_pin(path: Path, pin: dict[str, Any]) -> None:
    if (
        not path.is_file()
        or path.stat().st_size != pin["size_bytes"]
        or digest(path) != pin["sha256"]
    ):
        raise ResearchError("e2_artifact_pin_changed")


def verify_training_pointer(pointer: Path) -> tuple[Path, dict[str, Any]]:
    selected = json.loads(pointer.read_text(encoding="utf-8"))
    path = Path(selected["dataset"]["path"])
    verify_pin(path, selected["dataset"])
    return path, verify_training_manifest(path)


def verify_training_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest["contract"] != VERSION or manifest["status"] != "completed":
        raise ResearchError("e2_training_contract_unaccepted")
    for pin in manifest["files"]:
        verify_pin(path.parent / pin["path"], pin)
    for pin in [
        *manifest["binding"]["recovery_references"],
        manifest["binding"]["exclusion_ledger"],
    ]:
        verify_pin(Path(pin["path"]), pin)
    return manifest


class ResearchData:
    """Use pinned Parquet only. This never opens the operating database.

    R1's exhaustive accepted readback is reused after byte verification. E2 checks
    its new joins and populations independently rather than redoing ingestion.
    """

    def __init__(
        self, manifest_path: Path, group_path: Path | None = None, *, scratch: Path | None = None
    ) -> None:
        self.manifest_path = manifest_path
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.connection = duckdb.connect(":memory:")
        self.connection.execute("SET threads=2")
        self.connection.execute("SET memory_limit='8GB'")
        self.spill: Path | None = None
        if scratch is not None:
            self.spill = scratch.resolve() / ("query-" + uuid4().hex)
            self.spill.mkdir(parents=True)
            self.connection.execute("SET temp_directory=?", [str(self.spill)])
            self.connection.execute("SET max_temp_directory_size='8GiB'")
        self.group_features: list[str] = []
        self.group_categories: list[str] = []
        for role in ("features", "labels"):
            paths = [
                str(manifest_path.parent / pin["path"])
                for pin in self.manifest["files"]
                if pin["path"].endswith(f"/{role}.parquet")
            ]
            self._materialize(role, paths, omit_provenance=role == "features")
            if self.manifest.get("rows") is not None:
                count = self.connection.execute(f"SELECT count(*) FROM {role}").fetchone()
                if count is None or count[0] != self.manifest["rows"]:
                    raise ResearchError("e2_materialized_population_changed")
        self.connection.execute("CREATE VIEW predictors AS SELECT * FROM features")
        if group_path is not None:
            self.add_groups(group_path)

    def _materialize(self, name: str, paths: list[str], *, omit_provenance: bool = False) -> None:
        """Arrow reads fragmented immutable Parquet once; DuckDB gets typed batches.

        No learned transform, aggregation, row filtering or output rewrite occurs.
        The non-predictor provenance is kept in the immutable bundle only.
        """
        pa.set_cpu_count(4)
        initialized = False
        for path in paths:
            parquet = pq.ParquetFile(path)
            schema = pa.schema(
                [
                    field
                    for field in parquet.schema_arrow
                    if not (omit_provenance and field.name == "provenance")
                ]
            )
            projection = "*, '' AS provenance" if omit_provenance else "*"
            if not initialized:
                self.connection.register("_typed_batch", pa.Table.from_batches([], schema=schema))
                self.connection.execute(
                    f"CREATE TEMP TABLE {name} AS SELECT {projection} FROM _typed_batch"
                )
                self.connection.unregister("_typed_batch")
                initialized = True
            for batch in parquet.iter_batches(
                batch_size=65536, columns=schema.names, use_threads=True
            ):
                self.connection.register("_typed_batch", pa.Table.from_batches([batch]))
                self.connection.execute(f"INSERT INTO {name} SELECT {projection} FROM _typed_batch")
                self.connection.unregister("_typed_batch")
        if not initialized:
            raise ResearchError("e2_missing_parquet_inventory")

    def add_groups(self, group_path: Path) -> None:
        from .group_features import load_group_features

        group = load_group_features(group_path)
        if group["bundle"]["sha256"] != digest(self.manifest_path):
            raise ResearchError("e2_group_training_binding_changed")
        for item in group["files"]:
            verify_pin(group_path.parent / item["path"], item)
        self.group_features = list(group["feature_names"])
        self.group_categories = list(group["categorical_names"])
        paths = [
            str(group_path.parent / p["path"])
            for p in group["files"]
            if p["path"].endswith(".parquet") and p.get("role") == "features"
        ]
        if not paths:
            raise ResearchError("e2_group_features_missing")
        self._materialize("groups", paths)
        duplicate = self.connection.execute(
            "SELECT count(*)-count(DISTINCT row_key) FROM groups"
        ).fetchone()
        if duplicate is None or duplicate[0]:
            raise ResearchError("e2_duplicate_group_keys")
        unmatched = self.connection.execute(
            "SELECT count(*) FROM features f FULL JOIN groups g USING(row_key) "
            "WHERE f.row_key IS NULL OR g.row_key IS NULL OR "
            "f.package_number <> g.package_number OR f.period_start <> g.period_start"
        ).fetchone()
        if unmatched is None or unmatched[0]:
            raise ResearchError("e2_group_key_population_changed")
        names = ", ".join('g."' + x + '"' for x in self.group_features)
        self.connection.execute(
            "CREATE OR REPLACE VIEW predictors AS SELECT f.*, "
            + names
            + " FROM features f LEFT JOIN groups g "
            + "ON f.row_key=g.row_key AND f.period_start=g.period_start"
        )
        counts = self.connection.execute(
            "SELECT (SELECT count(*) FROM predictors), (SELECT count(*) FROM features)"
        ).fetchone()
        if counts is None or counts[0] != counts[1]:
            raise ResearchError("e2_group_join_changed_population")

    def candidates(self, start: str, end: str, *, reversal: bool = False) -> pd.DataFrame:
        """Feature-only cohort, with no read of future labels or survival status."""
        clause = " AND lag1_delta_ore > 0" if reversal else ""
        return self.connection.execute(
            (
                "SELECT * EXCLUDE(provenance) FROM predictors WHERE eligible AND "
                "period_start >= ?::DATE AND period_start < ?::DATE"
            )
            + clause
            + " ORDER BY period_start, row_key",
            [start, end],
        ).fetchdf()

    def outcomes(self, start: str, end: str) -> pd.DataFrame:
        return self.connection.execute(
            (
                "SELECT * FROM labels WHERE period_start >= ?::DATE AND "
                "period_start < ?::DATE ORDER BY period_start, row_key"
            ),
            [start, end],
        ).fetchdf()

    def training(self, start: str, end: str, *, target: str = "next") -> pd.DataFrame:
        if target not in {"next", "reversal"}:
            raise ResearchError("e2_unknown_target")
        candidates = self.candidates(start, end, reversal=target == "reversal")
        joined = attach_outcomes(candidates, self.outcomes(start, end))
        mask = (joined[target + "_status"] == "observed") & (
            joined[target + "_maturity_date"] < pd.Timestamp(end)
        )
        return joined.loc[mask].reset_index(drop=True)

    def close(self) -> None:
        self.connection.close()
        if self.spill is not None and self.spill.exists() and not any(self.spill.iterdir()):
            self.spill.rmdir()


def attach_outcomes(predictions: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    """Left attachment never replaces a selected item or drops an unknown outcome."""
    if predictions.row_key.duplicated().any() or outcomes.row_key.duplicated().any():
        raise ResearchError("e2_duplicate_prediction_or_label_key")
    result = predictions.merge(
        outcomes, how="left", on=list(KEYS), validate="one_to_one", sort=False
    )
    if len(result) != len(predictions) or not result.row_key.equals(
        predictions.row_key.reset_index(drop=True)
    ):
        raise ResearchError("e2_prediction_label_alignment_changed")
    if result.next_status.isna().any():
        raise ResearchError("e2_missing_label_record")
    return result


OWN_FEATURES = tuple(
    x
    for x in FEATURES
    if x
    in {
        "aip_ore",
        "lag1_delta_ore",
        "lag2_delta_ore",
        "lag3_delta_ore",
        "history_periods",
        "past_rise_frequency",
        "past_fall_frequency",
        "past_flat_frequency",
    }
)
