"""Bounded model-neutral reader for complete full-history artifacts, never a database."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .dataset import ResearchError, digest, fingerprint, pinned_files, read_json
from .training_contract import (
    CATEGORIES,
    FEATURE_SCHEMA,
    FEATURES,
    KEYS,
    LABEL_SCHEMA,
    RAW_SCHEMA,
    VERSION,
    atomic_json,
    data_dictionary,
    exclusion_reason,
    row_key,
    validate_calendar,
)


def parquet_rows(
    path: Path,
    schema: pa.Schema,
    *,
    lower: date | None = None,
    upper: date | None = None,
) -> Iterator[dict[str, Any]]:
    parquet = pq.ParquetFile(path)
    if not parquet.schema_arrow.equals(schema, check_metadata=False):
        raise ResearchError("training_parquet_schema_changed")
    for batch in parquet.iter_batches(batch_size=8192):
        if any(batch.column(i).null_count for i, field in enumerate(schema) if not field.nullable):
            raise ResearchError("training_required_value_missing")
        if lower is not None and upper is not None:
            dates = batch.column(schema.get_field_index("period_start"))
            batch = batch.filter(
                pc.and_(
                    pc.greater_equal(dates, pa.scalar(lower, pa.date32())),
                    pc.less(dates, pa.scalar(upper, pa.date32())),
                )
            )
        yield from batch.to_pylist()


def _external_references(binding: dict[str, Any], relocations: dict[str, str]) -> None:
    references = [*binding["recovery_references"], binding["exclusion_ledger"]]
    if not binding["recovery_references"]:
        raise ResearchError("training_recovery_references_required")
    for reference in references:
        path = Path(reference["path"])
        for previous, current in relocations.items():
            if path.is_relative_to(Path(previous)):
                path = Path(current) / path.relative_to(Path(previous))
                break
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.stat().st_size != reference["size_bytes"]
            or digest(path) != reference["sha256"]
        ):
            raise ResearchError("training_recovery_reference_changed")


def _validate_package(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    calendar: list[dict[str, Any]],
) -> None:
    by_index = {feature["period_index"]: feature for feature, _ in pairs}
    if len(by_index) != len(pairs):
        raise ResearchError("duplicate_training_package_period")
    previous: dict[str, Any] | None = None
    history: list[int] = []
    changes: Counter[int] = Counter()
    for feature, label in pairs:
        index = feature["period_index"]
        period = feature["period_start"]
        if (
            not 0 <= index < len(calendar)
            or period.isoformat() != calendar[index]["start"]
            or feature["row_key"] != row_key(feature["package_number"], period.isoformat())
            or feature["eligible"] != (feature["exclusion_reason"] == "")
        ):
            raise ResearchError("invalid_training_row_identity")
        if feature["eligible"] and (
            not feature["identity_episode_id"] or not feature["aip_ore"] or feature["aip_ore"] <= 0
        ):
            raise ResearchError("invalid_eligible_training_price")
        if any(isinstance(value, float) and not math.isfinite(value) for value in feature.values()):
            raise ResearchError("nonfinite_training_feature")
        continuation = (
            previous is not None
            and previous["period_index"] == index - 1
            and previous["eligible"]
            and feature["eligible"]
            and previous["identity_episode_id"] == feature["identity_episode_id"]
        )
        if not continuation:
            history, changes = [], Counter()
        lag = feature["aip_ore"] - history[-1] if feature["eligible"] and history else None
        if lag is not None:
            changes[(lag > 0) - (lag < 0)] += 1
        expected = {
            "lag1_delta_ore": lag,
            "lag2_delta_ore": history[-1] - history[-2] if len(history) > 1 else None,
            "lag3_delta_ore": history[-2] - history[-3] if len(history) > 2 else None,
            "history_periods": len(history) if feature["eligible"] else 0,
            "past_rise_frequency": changes[1] / (sum(changes.values())) if changes else None,
            "past_fall_frequency": changes[-1] / (sum(changes.values())) if changes else None,
            "past_flat_frequency": changes[0] / (sum(changes.values())) if changes else None,
        }
        if any(feature[name] != value for name, value in expected.items()):
            raise ResearchError("noncausal_training_history")
        if feature["supply_state"] != "unavailable_join_unestablished":
            raise ResearchError("unsupported_training_supply_join")
        if label["next_maturity_index"] != index + 1 or label["next_maturity_date"] != (
            date.fromisoformat(calendar[index + 1]["start"]) if index + 1 < len(calendar) else None
        ):
            raise ResearchError("invalid_next_target_maturity")

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

        target, status = future(1) if feature["eligible"] else (None, "origin_ineligible")
        delta = target["aip_ore"] - feature["aip_ore"] if target else None
        expected_label = {
            "next_status": status,
            "next_aip_ore": target["aip_ore"] if target else None,
            "next_delta_ore": delta,
            "next_direction": (delta > 0) - (delta < 0) if delta is not None else None,
            "reversal_status": "not_observed_rise",
            "reversal_maturity_index": None,
            "reversal_maturity_date": None,
            "reference_aip_ore": None,
            "horizon1_aip_ore": None,
            "horizon2_aip_ore": None,
            "reversal_category": None,
        }
        if continuation and previous and feature["aip_ore"] > previous["aip_ore"]:
            horizon = [future(offset) for offset in (1, 2)]
            state = next((s for f, s in horizon if f is None), "observed")
            expected_label.update(
                {
                    "reversal_status": state,
                    "reference_aip_ore": previous["aip_ore"],
                    "reversal_maturity_index": index + 2,
                    "reversal_maturity_date": date.fromisoformat(calendar[index + 2]["start"])
                    if index + 2 < len(calendar)
                    else None,
                }
            )
            if state == "observed":
                prices = [f["aip_ore"] for f, _ in horizon if f]
                expected_label.update(
                    {
                        "horizon1_aip_ore": prices[0],
                        "horizon2_aip_ore": prices[1],
                        "reversal_category": "full_reversal"
                        if min(prices) <= previous["aip_ore"]
                        else "partial_reversal"
                        if min(prices) < feature["aip_ore"]
                        else "persistence",
                    }
                )
        if any(label[name] != value for name, value in expected_label.items()):
            raise ResearchError("invalid_or_misaligned_training_label")
        if feature["eligible"]:
            history.append(feature["aip_ore"])
        previous = feature


@dataclass(frozen=True)
class TrainingBundle:
    manifest_path: Path
    manifest: dict[str, Any]
    calendar: list[dict[str, Any]]
    partitions: tuple[tuple[Path, Path], ...]

    def iter_pairs(self) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """One partition batch at a time; all rows, including excluded origins."""
        for feature_path, label_path in self.partitions:
            for feature, label in zip_longest(
                parquet_rows(feature_path, FEATURE_SCHEMA), parquet_rows(label_path, LABEL_SCHEMA)
            ):
                if (
                    feature is None
                    or label is None
                    or any(feature[key] != label[key] for key in KEYS)
                ):
                    raise ResearchError("training_feature_label_alignment_changed")
                yield feature, label

    def window(
        self,
        start: str,
        end_exclusive: str,
        *,
        target: str,
        mature_before: str,
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        if target not in {"next", "reversal"}:
            raise ResearchError("unknown_training_target")
        lower, upper, maturity = map(date.fromisoformat, (start, end_exclusive, mature_before))
        if lower >= upper or upper > maturity:
            raise ResearchError("invalid_chronological_training_window")
        declared = self.manifest["binding"]["windows"]
        if not any(w["start"] <= start and end_exclusive <= w["end_exclusive"] for w in declared):
            raise ResearchError("training_window_not_predeclared")
        for feature_path, label_path in self.partitions:
            pairs = zip_longest(
                parquet_rows(feature_path, FEATURE_SCHEMA, lower=lower, upper=upper),
                parquet_rows(label_path, LABEL_SCHEMA, lower=lower, upper=upper),
            )
            for feature, label in pairs:
                if (
                    feature is None
                    or label is None
                    or any(feature[key] != label[key] for key in KEYS)
                ):
                    raise ResearchError("training_feature_label_alignment_changed")
                mature_date = label[target + "_maturity_date"]
                if (
                    feature["eligible"]
                    and label[target + "_status"] == "observed"
                    and mature_date is not None
                    and mature_date < maturity
                ):
                    yield feature, label


def load_bundle(
    path: Path,
    *,
    verify_references: bool = True,
    relocations: dict[str, str] | None = None,
) -> TrainingBundle:
    """Hash every pinned byte and validate all rows/labels, with package-sized memory."""
    path = path.resolve(strict=True)
    manifest = read_json(path)
    if manifest.get("contract") != VERSION or manifest.get("status") != "completed":
        raise ResearchError("incomplete_or_unsupported_training_bundle")
    if manifest.get("feature_names") != list(FEATURES) or manifest.get("categorical_names") != list(
        CATEGORIES
    ):
        raise ResearchError("training_feature_allowlist_changed")
    pins = manifest["files"]
    files = pinned_files(path.parent, pins)
    by_name = {pin["path"]: (pin, file) for pin, file in zip(pins, files, strict=True)}
    count = manifest["binding"]["partitions"]
    expected_names = {
        "calendar.json",
        "contract.json",
        "exclusions.json",
        "source-catalogue.json",
        "coverage.json",
        "producer.zip",
    }
    expected_names.update(
        f"part-{bucket:04d}/{name}.parquet"
        for bucket in range(count)
        for name in ("source", "features", "labels")
    )
    if set(by_name) != expected_names:
        raise ResearchError("incomplete_training_partition_inventory")
    if read_json(path.parent / "contract.json") != data_dictionary():
        raise ResearchError("training_data_dictionary_changed")
    with zipfile.ZipFile(path.parent / "producer.zip") as archive:
        names = archive.namelist()
        if (
            len(names) != len(set(names))
            or archive.testzip() is not None
            or not {
                "pyproject.toml",
                "uv.lock",
                "src/bef_redbird/research/training.py",
                "src/bef_redbird/research/training_reader.py",
            }.issubset(names)
            or any(
                Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name
                for name in names
            )
        ):
            raise ResearchError("training_producer_capsule_changed")
        for name, expected in manifest["binding"]["code"].items():
            if (
                name.endswith(".py")
                and hashlib.sha256(archive.read("src/bef_redbird/research/" + name)).hexdigest()
                != expected
            ):
                raise ResearchError("training_producer_capsule_binding_changed")
    if verify_references:
        _external_references(manifest["binding"], relocations or {})
    calendar = validate_calendar(read_json(path.parent / "calendar.json")["periods"])
    if fingerprint(calendar) != manifest["binding"]["calendar_hash"]:
        raise ResearchError("training_calendar_binding_changed")
    exclusions = read_json(path.parent / "exclusions.json")
    if exclusions["source"] != manifest["binding"]["exclusion_ledger"]:
        raise ResearchError("training_exclusion_binding_changed")
    partitions = tuple(
        (
            path.parent / f"part-{bucket:04d}/features.parquet",
            path.parent / f"part-{bucket:04d}/labels.parquet",
        )
        for bucket in range(count)
    )
    bundle = TrainingBundle(path, manifest, calendar, partitions)
    catalogue = read_json(path.parent / "source-catalogue.json")
    for key, expected in (
        ("configuration", manifest["binding"]["configuration_hash"]),
        ("canonical_manifest", manifest["binding"]["evidence_hash"]),
    ):
        actual = hashlib.sha256(
            json.dumps(
                catalogue[key],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if actual != expected:
            raise ResearchError("training_source_catalogue_binding_changed")
    if (
        catalogue["canonical_manifest"]["read_contract_hash"]
        != manifest["binding"]["code"]["registry_read_contract"]
    ):
        raise ResearchError("training_source_catalogue_binding_changed")
    seen_packages: set[str] = set()
    direct = {
        (scope["date"], scope["package"])
        for scope in exclusions["scopes"]
        if scope["field"] == "abc_code"
    }
    uncertain_groups = {tuple(pair) for pair in exclusions["uncertain_groups"]}
    calendar_indices = {period["start"]: period["index"] for period in calendar}
    total = 0
    counts: Counter[str] = Counter()
    period_coverage: dict[str, Counter[str]] = {}
    for bucket, (feature_path, label_path) in enumerate(partitions):
        rows = 0
        last_package = ""
        pairs = zip_longest(
            parquet_rows(feature_path, FEATURE_SCHEMA), parquet_rows(label_path, LABEL_SCHEMA)
        )
        source_path = path.parent / f"part-{bucket:04d}/source.parquet"
        source_rows = parquet_rows(source_path, RAW_SCHEMA)
        checked: list[tuple[dict[str, Any], dict[str, Any]]] = []
        previous_source: dict[str, Any] | None = None
        previous_feature: dict[str, Any] | None = None
        for feature, label in pairs:
            if feature is None or label is None or any(feature[key] != label[key] for key in KEYS):
                raise ResearchError("training_feature_label_alignment_changed")
            package = feature["package_number"]
            source = next(source_rows, None)
            if source is None or (
                source["package_number"],
                source["period_start"],
                source["aip_ore"],
                source["provenance"],
            ) != (package, feature["period_start"], feature["aip_ore"], feature["provenance"]):
                raise ResearchError("training_source_feature_alignment_changed")
            expected_reason = exclusion_reason(source)
            if feature["exclusion_reason"] != expected_reason:
                raise ResearchError("training_source_eligibility_changed")
            index = feature["period_index"]
            period = feature["period_start"].isoformat()
            continued = (
                previous_source is not None
                and previous_feature is not None
                and previous_feature["package_number"] == package
                and previous_feature["period_index"] == index - 1
                and previous_feature["eligible"]
                and feature["eligible"]
                and previous_source["canonical_identity"] == source["canonical_identity"]
            )
            expected_episode = (
                previous_feature["identity_episode_id"]
                if continued and previous_feature
                else fingerprint(
                    {
                        "policy": VERSION,
                        "package": package,
                        "canonical_identity": source["canonical_identity"],
                        "start": period,
                    }
                )
            )
            if feature["identity_episode_id"] != expected_episode:
                raise ResearchError("training_source_episode_changed")
            for name in CATEGORIES:
                if name not in {"abc", "group_state", "supply_state"} and feature[
                    name
                ] != source.get(name):
                    raise ResearchError("training_historical_attribute_changed")
            expected_abc = source["abc"]
            group_state = (
                "observed_identity_only_population_unestablished"
                if source["group_code"]
                else "unobserved"
            )
            if (period, source["group_code"]) in uncertain_groups:
                group_state = "scoped_group_uncertainty"
            if (period, package) in direct:
                expected_abc, group_state = None, "scoped_source_inequality"
            if source["classification_state"] != "observed":
                expected_abc, group_state = None, "unobserved_or_ambiguous"
            if feature["abc"] != expected_abc or feature["group_state"] != group_state:
                raise ResearchError("training_classification_exclusion_changed")
            created = source["creation_date"]
            age = (
                index - calendar_indices[created.isoformat()]
                if (feature["eligible"] and created and created.isoformat() in calendar_indices)
                else None
            )
            if feature["lifecycle_age_periods"] != age:
                raise ResearchError("training_lifecycle_age_changed")
            if (
                len(package) != 6
                or not package.isascii()
                or not package.isdigit()
                or package < last_package
            ):
                raise ResearchError("training_package_order_changed")
            if package != last_package and checked:
                _validate_package(checked, calendar)
                checked = []
            if package != last_package:
                if package in seen_packages:
                    raise ResearchError("training_package_history_split_across_partitions")
                seen_packages.add(package)
            if checked and feature["period_index"] <= checked[-1][0]["period_index"]:
                raise ResearchError("training_period_order_changed")
            checked.append((feature, label))
            last_package = package
            previous_source, previous_feature = source, feature
            reason = feature["exclusion_reason"] or "eligible"
            counts.update(
                [reason, "next:" + label["next_status"], "reversal:" + label["reversal_status"]]
            )
            period_coverage.setdefault(str(index), Counter()).update(
                [
                    "source_rows",
                    "eligibility:" + reason,
                    "next:" + label["next_status"],
                    "reversal:" + label["reversal_status"],
                ]
            )
            rows += 1
        if checked:
            _validate_package(checked, calendar)
        if next(source_rows, None) is not None:
            raise ResearchError("training_source_feature_alignment_changed")
        for role in ("source", "features", "labels"):
            file_pin, file = by_name[f"part-{bucket:04d}/{role}.parquet"]
            if file_pin.get("rows") != rows or pq.ParquetFile(file).metadata.num_rows != rows:
                raise ResearchError("training_partition_row_count_changed")
        total += rows
    if total != manifest.get("rows"):
        raise ResearchError("training_total_row_count_changed")
    coverage = read_json(path.parent / "coverage.json")
    expected_coverage = [
        {**period, "counts": dict(period_coverage.get(str(period["index"]), {}))}
        for period in calendar
    ]
    if counts != manifest["counts"] or coverage != {
        "periods": expected_coverage,
        "totals": dict(counts),
    }:
        raise ResearchError("training_period_coverage_changed")
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--relocations", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    bundle = load_bundle(
        args.dataset, relocations=read_json(args.relocations) if args.relocations else None
    )
    from .catboost_data import peak_rss_bytes

    atomic_json(
        args.report,
        {
            "contract": "full-history-readback-v1",
            "passed": True,
            "manifest_sha256": digest(bundle.manifest_path),
            "rows": bundle.manifest["rows"],
            "periods": len(bundle.calendar),
            "first_period": bundle.calendar[0]["start"],
            "last_period": bundle.calendar[-1]["start"],
            "counts": bundle.manifest["counts"],
            "elapsed_seconds": time.monotonic() - started,
            "process_peak_rss_bytes": peak_rss_bytes(),
            "live_database_opened": False,
        },
    )


if __name__ == "__main__":
    main()
