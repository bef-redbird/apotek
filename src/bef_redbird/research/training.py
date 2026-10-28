"""Resumable package-partitioned full-history preparation. Never fits a model."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import time
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bef_redbird.foundation import registry_selection
from bef_redbird.foundation.archive import external_path

from .dataset import ResearchError, digest, fingerprint, pinned_files, read_json
from .training_contract import (
    CATEGORIES,
    FEATURE_SCHEMA,
    FEATURES,
    LABEL_SCHEMA,
    RAW_SCHEMA,
    VERSION,
    atomic_json,
    data_dictionary,
    exclusion_reason,
    pin,
    row_key,
    validate_calendar,
)

if TYPE_CHECKING:
    from .training_reader import TrainingBundle


def bucket_expression(key: str, count: int) -> str:
    if type(count) is not int or not 2 <= count <= 1024:
        raise ResearchError("invalid_training_partition_count")
    return f"CASE WHEN ({key}) IS NULL THEN 0 ELSE 1+(hash({key})%{count - 1}) END"


def partition_sql(
    sql: str,
    members: tuple[int, ...],
    bucket: int,
    count: int,
    *,
    raw_key: bool = False,
) -> str:
    """Restrict before canonical aggregation and identity windows; retain full history."""
    if type(bucket) is not int or not 0 <= bucket < count:
        raise ResearchError("invalid_training_partition")
    key = "CASE WHEN list_contains(s.supported_fields,'package_number') THEN r.package_number END"
    if raw_key:
        key = "r.package_number"
    predicate = bucket_expression(key, count)
    for member in members:
        anchor = f"WHERE s.dataset_id=? AND s.member_number={member} AND s.state='selected'"
        if sql.count(anchor) != 1:
            raise ResearchError("training_canonical_partition_contract_changed")
        sql = sql.replace(anchor, f"{anchor} AND ({predicate})={bucket}", 1)
    return sql


def extraction_sql(bucket: int, count: int, *, raw_key: bool = False) -> tuple[str, int]:
    """Join only same-period, final canonical facts, without latest-attribute backfill."""
    package_sql, pc, _ = registry_selection._query_sql("packages")
    price_sql, vc, _ = registry_selection._query_sql("prices")
    class_sql, cc, _ = registry_selection._query_sql("classifications")
    package_sql = partition_sql(package_sql, (2,), bucket, count, raw_key=raw_key)
    price_sql = partition_sql(price_sql, (2, 3), bucket, count, raw_key=raw_key)
    class_sql = partition_sql(class_sql, (4,), bucket, count, raw_key=raw_key)

    def attribute(name: str, drug: bool = False) -> str:
        fields = "drug_supported_fields" if drug else "supported_fields"
        states = "drug_native_field_states" if drug else "native_field_states"
        state = "drug_record_state" if drug else "record_state"
        return (
            f"CASE WHEN p.{state}='present' AND list_contains(p.{fields},'{name}') "
            f"AND coalesce(json_extract_string(p.{states},'$.{name}'),'present')='present' "
            f"THEN p.{name} END AS {name}"
        )

    attrs = ",".join(
        attribute(name, drug)
        for name, drug in (
            ("atc", True),
            ("form_code", True),
            ("strength", True),
            ("strength_unit", True),
            ("pack_size_token", False),
            ("pack_unit", False),
            ("dispensing_code", False),
            ("package_distributor", False),
        )
    )
    # Multiple ordinary group records are ambiguous; never multiply a package example.
    sql = f"""WITH packages AS ({package_sql}), prices AS ({price_sql}),
      classes AS ({class_sql}), cls AS (
        SELECT period_start,package_number,
          CASE WHEN count(*)=1 AND bool_and(record_state='present')
            AND bool_and(list_contains(supported_fields,'abc_code'))
            THEN any_value(abc_code) END AS abc,
          CASE WHEN count(*)=1 AND bool_and(record_state='present')
            AND bool_and(list_contains(supported_fields,'group_code'))
            THEN any_value(group_code) END AS group_code,
          CASE WHEN count(*)=1 AND bool_and(record_state='present') THEN 'observed'
            ELSE 'ambiguous' END AS classification_state
        FROM classes WHERE phase='final' GROUP BY period_start,package_number),
      aip AS (SELECT * FROM prices WHERE phase='final' AND field='aip_ore')
      SELECT coalesce(p.period_start,v.period_start) AS period_start,
        coalesce(p.package_number,v.package_number) AS package_number,
        p.identity_episode_id AS canonical_identity,p.drug_id,
        v.state AS price_state,v.tax_basis,v.fee_basis,v.currency,v.price_basis,v.price_role,
        c.abc,c.group_code,c.classification_state,{attrs},
        json_object('package_import',p.import_id,'package_member',p.member_id,
          'package_locator',p.source_locator,'drug_import',p.drug_import_id,
          'drug_locator',p.drug_source_locator,'price_import',v.import_id,
          'price_member',v.member_id,'price_locator',v.source_locator,
          'price_supporting_imports',v.supporting_import_ids)::VARCHAR AS provenance,
        v.amount_ore AS aip_ore,p.creation_date,p.exit_date
      FROM (SELECT * FROM packages WHERE phase='final') p
      FULL OUTER JOIN aip v ON p.period_start=v.period_start AND p.package_number=v.package_number
      LEFT JOIN cls c ON c.period_start=coalesce(p.period_start,v.period_start)
        AND c.package_number=coalesce(p.package_number,v.package_number)
      WHERE coalesce(p.period_start,v.period_start)>=DATE '2008-01-01'
        AND ({bucket_expression("coalesce(p.package_number,v.package_number)", count)})={bucket}
      ORDER BY package_number,period_start"""
    return sql, pc + vc + cc


def compact_exclusions(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in ledger.get("records", []):
        if item.get("field") not in {"abc_code", "reimbursement_basis.per_package"}:
            continue
        if item.get("underlying_value_relation") not in {
            "unequal",
            "unequal_positive_exact_amounts",
        }:
            continue
        result.append(
            {
                "package": item["package"],
                "date": item.get("date", item["effective_date"]),
                "field": item["field"],
                "phase": item["phase"],
                "reason": "source_inequality_revision_precedence_unresolved",
                "ledger_id": item["id"],
                "originals": sorted(
                    {
                        side["original_sha256"]
                        for name in ("website", "ftp")
                        if isinstance(side := item.get(name), dict) and "original_sha256" in side
                    }
                ),
            }
        )
    return sorted(result, key=lambda x: (x["date"], x["package"], x["field"]))


def package_rows(
    rows: list[dict[str, Any]],
    calendar: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    uncertain_groups: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """At most one package history; episodes and features depend only on past/current data."""
    index_by_date = {p["start"]: p["index"] for p in calendar}
    direct = {(e["date"], e["package"]) for e in exclusions if e["field"] == "abc_code"}
    features: list[dict[str, Any]] = []
    history: list[int] = []
    changes: Counter[int] = Counter()
    previous: dict[str, Any] | None = None
    for raw in rows:
        period = raw["period_start"].isoformat()
        if period not in index_by_date:
            raise ResearchError("training_observation_outside_calendar")
        index = index_by_date[period]
        reason = exclusion_reason(raw)
        continuation = (
            previous is not None
            and previous["period_index"] == index - 1
            and previous["eligible"]
            and not reason
            and previous["canonical_identity"] == raw["canonical_identity"]
        )
        if not continuation:
            history, changes = [], Counter()
            episode = fingerprint(
                {
                    "policy": VERSION,
                    "package": raw["package_number"],
                    "canonical_identity": raw["canonical_identity"],
                    "start": period,
                }
            )
        else:
            assert previous is not None
            episode = previous["identity_episode_id"]
        key = {
            "row_key": row_key(raw["package_number"], period),
            "package_number": raw["package_number"],
            "period_index": index,
            "period_start": raw["period_start"],
            "identity_episode_id": episode,
        }
        amount = raw["aip_ore"]
        deltas = [history[-n] - history[-n - 1] if len(history) > n else None for n in (1, 2)]
        lag = amount - history[-1] if not reason and history else None
        if lag is not None:
            changes[(lag > 0) - (lag < 0)] += 1
        cats = {name: raw.get(name) for name in CATEGORIES}
        cats["supply_state"] = "unavailable_join_unestablished"
        group = raw.get("group_code")
        group_state = "observed_identity_only_population_unestablished" if group else "unobserved"
        if (period, group) in uncertain_groups:
            group_state = "scoped_group_uncertainty"
        if (period, raw["package_number"]) in direct:
            cats["abc"] = None
            group_state = "scoped_source_inequality"
        if raw.get("classification_state") != "observed":
            cats["abc"] = None
            group_state = "unobserved_or_ambiguous"
        cats["group_state"] = group_state
        age = (
            index - index_by_date[raw["creation_date"].isoformat()]
            if (not reason and raw["creation_date"].isoformat() in index_by_date)
            else None
        )
        feature = {
            **key,
            "eligible": not reason,
            "exclusion_reason": reason,
            "aip_ore": amount,
            "lag1_delta_ore": lag,
            "lag2_delta_ore": deltas[0],
            "lag3_delta_ore": deltas[1],
            "history_periods": len(history) if not reason else 0,
            "past_rise_frequency": changes[1] / (sum(changes.values())) if changes else None,
            "past_fall_frequency": changes[-1] / (sum(changes.values())) if changes else None,
            "past_flat_frequency": changes[0] / (sum(changes.values())) if changes else None,
            "lifecycle_age_periods": age,
            **cats,
            "provenance": raw["provenance"],
        }
        features.append(feature)
        previous = {**feature, "canonical_identity": raw["canonical_identity"]}
        if not reason:
            history.append(amount)
    by_index = {f["period_index"]: f for f in features}
    if len(by_index) != len(features):
        raise ResearchError("duplicate_training_package_period")
    labels = []
    for feature in features:
        index = feature["period_index"]

        def future(
            offset: int,
            index: int = index,
            feature: dict[str, Any] = feature,
        ) -> tuple[dict[str, Any] | None, str]:
            if index + offset >= len(calendar):
                return None, "pending"
            target = by_index.get(index + offset)
            if target is None:
                return None, "gap"
            if not target["eligible"]:
                return None, "ineligible"
            if target["identity_episode_id"] != feature["identity_episode_id"]:
                return None, "identity_change"
            return target, "observed"

        next_row, status = future(1) if feature["eligible"] else (None, "origin_ineligible")
        label = {
            **{
                name: feature[name]
                for name in (
                    "row_key",
                    "package_number",
                    "period_index",
                    "period_start",
                    "identity_episode_id",
                )
            },
            "next_status": status,
            "next_maturity_index": index + 1,
            "next_maturity_date": date.fromisoformat(calendar[index + 1]["start"])
            if index + 1 < len(calendar)
            else None,
            "next_aip_ore": next_row["aip_ore"] if next_row else None,
            "next_delta_ore": next_row["aip_ore"] - feature["aip_ore"] if next_row else None,
            "next_direction": None,
            "reversal_status": "not_observed_rise",
            "reversal_maturity_index": None,
            "reference_aip_ore": None,
            "reversal_maturity_date": None,
            "horizon1_aip_ore": None,
            "horizon2_aip_ore": None,
            "reversal_category": None,
        }
        if next_row:
            delta = label["next_delta_ore"]
            label["next_direction"] = (delta > 0) - (delta < 0)
        previous_feature = by_index.get(index - 1)
        if (
            feature["eligible"]
            and previous_feature
            and previous_feature["eligible"]
            and previous_feature["identity_episode_id"] == feature["identity_episode_id"]
            and feature["aip_ore"] > previous_feature["aip_ore"]
        ):
            horizon = [future(offset) for offset in (1, 2)]
            label["reference_aip_ore"] = previous_feature["aip_ore"]
            label["reversal_maturity_index"] = index + 2
            label["reversal_maturity_date"] = (
                date.fromisoformat(calendar[index + 2]["start"])
                if index + 2 < len(calendar)
                else None
            )
            label["reversal_status"] = next(
                (state for f, state in horizon if f is None), "observed"
            )
            if label["reversal_status"] == "observed":
                prices = [f["aip_ore"] for f, _ in horizon if f]
                label["horizon1_aip_ore"], label["horizon2_aip_ore"] = prices
                minimum = min(prices)
                label["reversal_category"] = (
                    "full_reversal"
                    if minimum <= previous_feature["aip_ore"]
                    else "partial_reversal"
                    if minimum < feature["aip_ore"]
                    else "persistence"
                )
        labels.append(label)
    return features, labels


def raw_rows(path: Path) -> Iterator[dict[str, Any]]:
    for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
        yield from batch.to_pylist()


def transform_partition(
    raw: Path,
    destination: Path,
    calendar: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    uncertain_groups: set[tuple[str, str]],
) -> tuple[int, dict[str, int], dict[str, dict[str, int]]]:
    total = 0
    counts: Counter[str] = Counter()
    coverage: dict[str, Counter[str]] = {}
    with (
        pq.ParquetWriter(
            destination / "features.parquet", FEATURE_SCHEMA, compression="zstd"
        ) as fw,
        pq.ParquetWriter(destination / "labels.parquet", LABEL_SCHEMA, compression="zstd") as lw,
    ):
        for _, group in groupby(raw_rows(raw), key=lambda r: r["package_number"]):
            features, labels = package_rows(list(group), calendar, exclusions, uncertain_groups)
            fw.write_table(pa.Table.from_pylist(features, schema=FEATURE_SCHEMA))
            lw.write_table(pa.Table.from_pylist(labels, schema=LABEL_SCHEMA))
            total += len(features)
            counts.update(f["exclusion_reason"] or "eligible" for f in features)
            counts.update("next:" + label["next_status"] for label in labels)
            counts.update("reversal:" + label["reversal_status"] for label in labels)
            for feature, label in zip(features, labels, strict=True):
                period = str(feature["period_index"])
                period_counts = coverage.setdefault(period, Counter())
                period_counts.update(
                    [
                        "source_rows",
                        "eligibility:" + (feature["exclusion_reason"] or "eligible"),
                        "next:" + label["next_status"],
                        "reversal:" + label["reversal_status"],
                    ]
                )
    return total, dict(counts), {key: dict(value) for key, value in coverage.items()}


def external_pin(path: Path) -> dict[str, Any]:
    path = path.resolve(strict=True)
    return {"path": str(path), "sha256": digest(path), "size_bytes": path.stat().st_size}


def producer_capsule(destination: Path) -> None:
    """Small deterministic source/dependency snapshot, never a copy of native originals."""
    root = Path(__file__).resolve().parents[3]
    paths = sorted(
        [p for p in (root / "src").rglob("*") if p.suffix in {".py", ".sql"}]
        + [root / name for name in ("pyproject.toml", "uv.lock", "requirements.txt")]
    )
    temporary = destination.with_suffix(".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                info = zipfile.ZipInfo(
                    path.relative_to(root).as_posix(), date_time=(2020, 1, 1, 0, 0, 0)
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_pins() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in ("duckdb", "pyarrow", "numpy", "pandas", "catboost")
    }


def code_pins() -> dict[str, str]:
    """One used-code boundary for immutable version names and partition resume bindings."""
    result = {
        name: digest(Path(__file__).with_name(name))
        for name in (
            "training.py",
            "training_contract.py",
            "training_reader.py",
            "catboost_data.py",
            "dataset.py",
        )
    }
    result["registry_read_contract"] = registry_selection._read_contract_hash()
    return result


def validate_policy(configuration: dict[str, Any]) -> None:
    windows = configuration.get("windows")
    if not isinstance(windows, list) or not windows or not configuration.get("exposure"):
        raise ResearchError("training_chronology_and_exposure_required")
    previous: date | None = None
    for window in windows:
        lower, upper = (
            date.fromisoformat(window["start"]),
            date.fromisoformat(window["end_exclusive"]),
        )
        if not window.get("name") or lower >= upper or (previous and lower < previous):
            raise ResearchError("invalid_predeclared_training_windows")
        previous = upper


def raw_key_proof(connection: duckdb.DuckDBPyConnection, dataset_id: str) -> dict[str, Any]:
    """Raw pushdown equals the canonical CASE only when every selected scope admits its key."""
    rows = connection.execute(
        "SELECT member_number,count(*),count(*) FILTER(WHERE NOT "
        "coalesce(list_contains(supported_fields,'package_number'),false)) "
        "FROM registry_dataset_member WHERE dataset_id=? AND member_number IN (2,3,4) "
        "AND state='selected' GROUP BY member_number ORDER BY member_number",
        [dataset_id],
    ).fetchall()
    return {
        "safe": all(unsupported == 0 for _, _, unsupported in rows),
        "members": [
            {"member": number, "selected_scopes": count, "unsupported_key_scopes": unsupported}
            for number, count, unsupported in rows
        ],
    }


def produce(
    configuration: dict[str, Any],
    output: Path,
    *,
    _verified: list[TrainingBundle] | None = None,
) -> dict[str, Any]:
    """Closed-DB byte pin, bounded SQL extraction, restartable immutable artifacts."""
    started = time.monotonic()
    validate_policy(configuration)
    output = external_path(output)
    database = Path(configuration["database"]).resolve(strict=True)
    if Path(str(database) + ".wal").exists():
        raise ResearchError("training_requires_cold_database")
    output.mkdir(parents=True, exist_ok=True)
    count = configuration.get("partitions", 64)
    bucket_expression("package_number", count)
    dataset_id = configuration["dataset_id"]
    code = code_pins()
    references = [external_pin(Path(p)) for p in configuration["recovery_references"]]
    if not references:
        raise ResearchError("training_recovery_references_required")
    ledger = Path(configuration["exclusion_ledger"]).resolve(strict=True)
    ledger_pin = external_pin(ledger)
    exclusions = compact_exclusions(read_json(ledger))
    with duckdb.connect(str(database), read_only=True) as connection:
        connection.execute("SET memory_limit='8GB'")
        connection.execute("SET threads=2")
        connection.execute("SET max_temp_directory_size='12GiB'")
        connection.execute("SET temp_directory=?", [str(output / "spill")])
        before = database.stat()
        db_pin = external_pin(database)
        if (
            database.stat().st_size != before.st_size
            or database.stat().st_mtime_ns != before.st_mtime_ns
        ):
            raise ResearchError("training_database_changed_during_pin")
        metadata = connection.execute(
            "SELECT configuration_hash,evidence_hash,manifest FROM analytical_dataset "
            "WHERE dataset_id=?",
            [dataset_id],
        ).fetchone()
        if not metadata:
            raise ResearchError("unknown_training_dataset")
        manifest = json.loads(metadata[2])
        if manifest["read_contract_hash"] != code["registry_read_contract"]:
            raise ResearchError("training_canonical_read_contract_changed")
        calendar = [
            {
                "index": i,
                "start": start.isoformat(),
                "end_exclusive": end.isoformat(),
                "decision_cutoff": datetime.combine(
                    start + timedelta(days=9), day_time(12), ZoneInfo("Europe/Copenhagen")
                ).isoformat(),
            }
            for i, (start, end) in enumerate(
                connection.execute(
                    "SELECT d.period_start,p.period_end_exclusive FROM registry_dataset_period d "
                    "JOIN price_period p USING(period_start) WHERE dataset_id=? AND phase='final' "
                    "AND d.period_start>=DATE '2008-01-01' ORDER BY d.period_start",
                    [dataset_id],
                ).fetchall()
            )
        ]
        validate_calendar(calendar)
        raw_pruning = raw_key_proof(connection, dataset_id)
        binding = {
            "contract": VERSION,
            "database": db_pin,
            "dataset_id": dataset_id,
            "configuration_hash": metadata[0],
            "evidence_hash": metadata[1],
            "code": code,
            "dependencies": runtime_pins(),
            "partitions": count,
            "recovery_references": references,
            "exclusion_ledger": ledger_pin,
            "windows": configuration["windows"],
            "exposure": configuration["exposure"],
            "calendar_hash": fingerprint(calendar),
            "raw_key_pruning": raw_pruning,
        }
        state_path = output / "checkpoint.json"
        state = (
            read_json(state_path) if state_path.exists() else {"binding": binding, "completed": {}}
        )
        if state.get("binding") != binding:
            raise ResearchError("training_resume_binding_changed")
        if (output / "dataset.json").exists():
            from .training_reader import load_bundle

            bundle = load_bundle(output / "dataset.json")
            if _verified is not None:
                _verified.append(bundle)
            return bundle.manifest
        atomic_json(output / "calendar.json", {"periods": calendar})
        atomic_json(output / "contract.json", data_dictionary())
        producer_capsule(output / "producer.zip")
        editions_sql, edition_count, _ = registry_selection._query_sql("editions")
        editions_cursor = connection.execute(editions_sql, [dataset_id] * edition_count)
        edition_names = [column[0] for column in editions_cursor.description]
        editions = [
            dict(
                zip(
                    edition_names,
                    [
                        str(value)
                        if isinstance(value, (date,)) or type(value).__name__ == "UUID"
                        else value
                        for value in row
                    ],
                    strict=True,
                )
            )
            for row in editions_cursor.fetchall()
        ]
        canonical_config = connection.execute(
            "SELECT configuration FROM analytical_dataset WHERE dataset_id=?", [dataset_id]
        ).fetchone()
        assert canonical_config is not None
        atomic_json(
            output / "source-catalogue.json",
            {
                "canonical_manifest": manifest,
                "configuration": json.loads(canonical_config[0]),
                "editions": editions,
                "archive_reference_policy": "sha256/<first-two>/<sha256>",
                "recovery_references": references,
            },
        )
        atomic_json(
            output / "exclusions.json",
            {
                "source": ledger_pin,
                "scopes": exclusions,
                "policy": (
                    "No TSP feature. Unequal ABC keys suppressed; affected group "
                    "identities uncertain. Valid AIP retained."
                ),
            },
        )
        # Resolve affected group identities globally before sharding; no group counts on buckets.
        uncertain: set[tuple[str, str]] = set()
        abc = [e for e in exclusions if e["field"] == "abc_code"]
        if abc:
            cls, n, _ = registry_selection._query_sql("classifications")
            keys = " OR ".join("(period_start=? AND package_number=?)" for _ in abc)
            values = [v for e in abc for v in (e["date"], e["package"])]
            uncertain = {
                (start.isoformat(), group)
                for start, group in connection.execute(
                    f"SELECT DISTINCT period_start,group_code FROM ({cls}) c "
                    f"WHERE phase='final' AND ({keys}) AND group_code IS NOT NULL",
                    [*([dataset_id] * n), *values],
                ).fetchall()
            }
        exclusion_document = read_json(output / "exclusions.json")
        exclusion_document["uncertain_groups"] = [list(pair) for pair in sorted(uncertain)]
        atomic_json(output / "exclusions.json", exclusion_document)
        all_pins = [
            pin(output / "calendar.json", output, "calendar"),
            pin(output / "contract.json", output, "data_dictionary"),
            pin(output / "exclusions.json", output, "exclusions"),
            pin(output / "source-catalogue.json", output, "source_catalogue"),
            pin(output / "producer.zip", output, "producer_capsule"),
        ]
        totals: Counter[str] = Counter()
        period_coverage: dict[str, Counter[str]] = {}
        for bucket in (*range(1, count), 0):
            key = str(bucket)
            destination = output / f"part-{bucket:04d}"
            reused = key in state["completed"]
            if reused:
                entry = state["completed"][key]
                pinned_files(output, entry["files"])
            else:
                destination.mkdir(exist_ok=True)
                sql, n = extraction_sql(bucket, count, raw_key=raw_pruning["safe"])
                raw = destination / "source.parquet"
                cursor = connection.execute(sql, [dataset_id] * n)
                canonical_rows = 0
                with pq.ParquetWriter(raw, RAW_SCHEMA, compression="zstd") as writer:
                    for batch in cursor.fetch_record_batch(8192):
                        writer.write_table(pa.Table.from_batches([batch]).cast(RAW_SCHEMA))
                        canonical_rows += len(batch)
                rows, counts, coverage = transform_partition(
                    raw, destination, calendar, exclusions, uncertain
                )
                if canonical_rows != rows or pq.ParquetFile(raw).metadata.num_rows != rows:
                    raise ResearchError("training_canonical_export_row_count_changed")
                entry = {
                    "rows": rows,
                    "canonical_rows": canonical_rows,
                    "counts": counts,
                    "coverage": coverage,
                    "files": [
                        pin(raw, output, "source", rows),
                        pin(destination / "features.parquet", output, "features", rows),
                        pin(destination / "labels.parquet", output, "labels", rows),
                    ],
                }
                state["completed"][key] = entry
                atomic_json(state_path, state)
            all_pins.extend(entry["files"])
            totals.update(entry["counts"])
            for period, period_counts in entry["coverage"].items():
                period_coverage.setdefault(period, Counter()).update(period_counts)
            print(
                json.dumps(
                    {
                        "training_partition": bucket,
                        "rows": entry["rows"],
                        "reused": reused,
                    }
                ),
                flush=True,
            )
        if (
            database.stat().st_size != before.st_size
            or database.stat().st_mtime_ns != before.st_mtime_ns
            or Path(str(database) + ".wal").exists()
        ):
            raise ResearchError("training_database_changed_during_export")
    coverage_rows = [
        {**period, "counts": dict(period_coverage.get(str(period["index"]), {}))}
        for period in calendar
    ]
    atomic_json(output / "coverage.json", {"periods": coverage_rows, "totals": dict(totals)})
    all_pins.append(pin(output / "coverage.json", output, "period_coverage"))
    result = {
        "contract": VERSION,
        "status": "completed",
        "binding": binding,
        "feature_names": list(FEATURES),
        "categorical_names": list(CATEGORIES),
        "availability": "reconstructed; historical publication/possession unestablished",
        "cutoff": "Wednesday second tariff week 12:00 Europe/Copenhagen; operational assumption",
        "label_contract": (
            "next exact integer-ore direction/delta; observed strict rise "
            "full/partial reversal or persistence over both following tariff "
            "periods; no bridging gaps"
        ),
        "files": all_pins,
        "counts": dict(totals),
        "rows": sum(entry["rows"] for entry in state["completed"].values()),
        "created_utc": datetime.now(UTC).isoformat(),
        "elapsed_seconds": time.monotonic() - started,
    }
    # Complete consumer validation precedes publication; failure leaves restartable shards.
    atomic_json(output / "candidate.json", result)
    from .training_reader import load_bundle

    bundle = load_bundle(output / "candidate.json")
    from .catboost_data import peak_rss_bytes

    result["process_peak_rss_bytes"] = peak_rss_bytes()
    result["elapsed_seconds"] = time.monotonic() - started
    atomic_json(output / "candidate.json", result)
    os.replace(output / "candidate.json", output / "dataset.json")
    if _verified is not None:
        _verified.append(replace(bundle, manifest_path=output / "dataset.json", manifest=result))
    return result


def refresh(operating_path: Path, template_path: Path, output_root: Path) -> dict[str, Any]:
    """Derive current source pins; preserve every old bundle and publish pointer last."""
    operating, template = read_json(operating_path), read_json(template_path)
    publication_path = Path(operating["operation_root"]) / "publication-state.json"
    publication = read_json(publication_path)
    config = {
        **template,
        "database": operating["database"],
        "dataset_id": publication["result"]["dataset_id"],
    }
    database = Path(config["database"])
    policy_pin = {
        "template": template,
        "publication": external_pin(publication_path),
        "database_stat": [database.stat().st_size, database.stat().st_mtime_ns],
        "code": code_pins(),
        "dependencies": runtime_pins(),
        "references": [external_pin(Path(p)) for p in template["recovery_references"]],
        "ledger": external_pin(Path(template["exclusion_ledger"])),
    }
    version = f"{config['dataset_id']}-{fingerprint(policy_pin)[:16]}"
    output_root = external_path(output_root)
    output = output_root / version
    verified: list[TrainingBundle] = []
    result = produce(config, output, _verified=verified)
    from .catboost_data import build_pool

    bundle = verified[0]
    reports = []
    for target in template.get(
        "pool_targets", ["next_direction", "next_delta_ore", "reversal_category"]
    ):
        pool_input = build_pool(bundle, **{**template["pool_window"], "target": target})
        reports.append(pool_input.report)
        del pool_input
    pool_report = {
        "contract": "catboost-multi-target-readiness-v1",
        "tasks": reports,
        "model_fitted": False,
        "predictions_produced": False,
    }
    receipt_path = output / "pool-readiness.json"
    if receipt_path.exists():
        previous = read_json(receipt_path)

        def semantic_tasks(report: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                {
                    k: v
                    for k, v in task.items()
                    if k not in {"elapsed_seconds", "process_peak_rss_bytes"}
                }
                for task in report["tasks"]
            ]

        if semantic_tasks(previous) != semantic_tasks(pool_report):
            raise ResearchError("training_pool_retry_semantics_changed")
    else:
        atomic_json(receipt_path, pool_report)
    pointer = {
        "contract": VERSION,
        "dataset": external_pin(output / "dataset.json"),
        "pool_readiness": external_pin(receipt_path),
        "policy_fingerprint": fingerprint(policy_pin),
        "rows": result["rows"],
        "status": "verified_readback_and_pool_no_fit",
    }
    atomic_json(output_root / "current.json", pointer)
    return pointer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--operating-config", type=Path)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    if args.operating_config and args.template and args.output_root:
        result = refresh(args.operating_config, args.template, args.output_root)
    elif args.config and args.output:
        result = produce(read_json(args.config), args.output)
    else:
        parser.error("provide --config/--output or --operating-config/--template/--output-root")
    print(json.dumps({"rows": result["rows"], "status": result["status"]}))


if __name__ == "__main__":
    main()
