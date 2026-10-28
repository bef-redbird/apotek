"""Strict frozen-panel input boundary. No foundation/database imports or live reads."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


class ResearchError(ValueError):
    """An incompatible, incomplete or corrupt research artifact."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def parse_json_bytes(data: bytes) -> dict[str, Any]:
    """Parse one captured byte sequence; its caller can pin those exact bytes."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ResearchError("duplicate_json_key")
            result[key] = value
        return result

    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise ResearchError("json_object_required")
        canonical(result)  # Reject non-finite JSON values, including nested ones.
        return result
    except (UnicodeError, ValueError) as exc:
        raise ResearchError("invalid_json_artifact") from exc


def read_json(path: Path) -> dict[str, Any]:
    try:
        return parse_json_bytes(path.read_bytes())
    except OSError as exc:
        raise ResearchError("invalid_json_artifact") from exc


def pinned_files(root: Path, pins: Any) -> list[Path]:
    if not isinstance(pins, list) or not pins:
        raise ResearchError("file_pins_required")
    paths: list[Path] = []
    for pin in pins:
        if not isinstance(pin, dict):
            raise ResearchError("invalid_file_pin")
        relative = pin.get("path")
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ResearchError("invalid_relative_artifact_path")
        path = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ResearchError("artifact_path_escape")
        if not path.resolve().is_relative_to(root.resolve()) or path.resolve() in paths:
            raise ResearchError("artifact_path_escape_or_duplicate")
        expected = pin.get("sha256")
        size = pin.get("size_bytes")
        if (
            not isinstance(expected, str)
            or not re.fullmatch("[0-9a-f]{64}", expected)
            or type(size) is not int
            or size < 0
            or not isinstance(pin.get("role"), str)
        ):
            raise ResearchError("invalid_file_pin")
        if not path.is_file() or path.stat().st_size != size or digest(path) != expected:
            raise ResearchError("artifact_hash_mismatch")
        paths.append(path.resolve())
    return paths


@dataclass(frozen=True, slots=True)
class Row:
    period_index: int
    canonical_identifier: str
    identity_episode_id: str
    amount_ore: int | None
    eligible: bool
    exclusion_reason: str
    abc: str
    group_id: str
    provenance_id: str
    lifecycle_age_periods: int | None = None
    identity_state: str = "unknown"
    reported_supply_positive: bool | None = None
    supply_coverage_unknown: bool = True


@dataclass(frozen=True)
class Dataset:
    manifest_path: Path
    manifest_hash: str
    manifest_bytes: bytes
    manifest: dict[str, Any]
    periods: tuple[date, ...]
    rows: tuple[Row, ...]
    by_period: tuple[tuple[Row, ...], ...]
    lookup: dict[tuple[int, str], Row]


REQUIRED_COLUMNS = {
    "period_index",
    "canonical_identifier",
    "identity_episode_id",
    "amount_ore",
    "eligible",
    "exclusion_reason",
    "abc",
    "group_id",
    "provenance_id",
}
OPTIONAL_COLUMNS = {
    "lifecycle_age_periods",
    "identity_state",
    "reported_supply_positive",
    "supply_coverage_unknown",
}


def _integer(value: str, *, nullable: bool = False) -> int | None:
    if not value and nullable:
        return None
    if not re.fullmatch("0|[1-9][0-9]*", value):
        raise ResearchError("invalid_nonnegative_integer")
    result = int(value)
    if result > 2**63 - 1:
        raise ResearchError("integer_out_of_range")
    return result


def _boolean(value: str) -> bool:
    if value not in {"true", "false"}:
        raise ResearchError("invalid_boolean")
    return value == "true"


def load_dataset(manifest_path: Path) -> Dataset:
    """Verify every pinned byte before parsing. Reject ambiguous identities or values.

    Eligible observations in one identity episode must be consecutive and cannot
    resume after an ineligible observation. Episode splitting is a producing
    workflow responsibility; this boundary verifies its structural invariants.
    """
    manifest_path = Path(manifest_path).resolve()
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise ResearchError("invalid_json_artifact") from exc
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = parse_json_bytes(manifest_bytes)
    if manifest.get("schema_version") != "e1-panel-v1":
        raise ResearchError("unsupported_panel_schema")
    for name in ("dataset_id", "population_definition", "identity_policy"):
        if not isinstance(manifest.get(name), str) or not manifest[name].strip():
            raise ResearchError("missing_panel_declaration")
    # Strict replay needs a separately validated, versioned availability-proof
    # contract. A caller's string declaration alone cannot establish that claim.
    if manifest.get("availability_mode") != "reconstructed_research":
        raise ResearchError("unsupported_availability_mode")
    if not isinstance(manifest.get("provenance"), dict) or not manifest["provenance"]:
        raise ResearchError("provenance_declaration_required")
    context = manifest.get("price_context", {})
    if not isinstance(context, dict) or any(
        context.get(key) != value
        for key, value in {
            "price_type": "aip",
            "currency": "DKK",
            "price_basis": "per_package",
        }.items()
    ):
        raise ResearchError("unsupported_price_context")
    if any(
        not isinstance(context.get(key), str) or context[key] not in {"included", "excluded"}
        for key in ("tax_basis", "fee_basis")
    ):
        raise ResearchError("monetary_context_required")
    calendar = manifest.get("periods")
    if not isinstance(calendar, list) or len(calendar) < 3:
        raise ResearchError("calendar_required")
    starts: list[date] = []
    try:
        for index, period in enumerate(calendar):
            if not isinstance(period, dict):
                raise ResearchError("invalid_calendar_record")
            if type(period.get("index")) is not int or period["index"] != index:
                raise ResearchError("noncontiguous_calendar_indices")
            start = date.fromisoformat(period["start"])
            if starts and start <= starts[-1]:
                raise ResearchError("nonincreasing_calendar")
            if "end_exclusive" in period:
                end = date.fromisoformat(period["end_exclusive"])
                if end <= start or (
                    index + 1 < len(calendar)
                    and end != date.fromisoformat(calendar[index + 1]["start"])
                ):
                    raise ResearchError("calendar_end_mismatch")
            starts.append(start)
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchError("invalid_calendar") from exc
    paths = pinned_files(manifest_path.parent, manifest.get("files"))
    panel_paths = [
        path for path, pin in zip(paths, manifest["files"], strict=True) if pin["role"] == "panel"
    ]
    if len(panel_paths) != 1:
        raise ResearchError("one_panel_file_required")
    panel_path = panel_paths[0]
    if not (panel_path.name.endswith(".csv") or panel_path.name.endswith(".csv.gz")):
        raise ResearchError("unsupported_panel_encoding")
    opener = gzip.open if panel_path.name.endswith(".gz") else open
    rows: list[Row] = []
    lookup: dict[tuple[int, str], Row] = {}
    try:
        with opener(panel_path, "rt", encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            headers = reader.fieldnames or []
            if (
                len(headers) != len(set(headers))
                or not REQUIRED_COLUMNS <= set(headers)
                or set(headers) - REQUIRED_COLUMNS - OPTIONAL_COLUMNS
            ):
                raise ResearchError("invalid_panel_columns")
            for raw in reader:
                if None in raw or any(value is None for value in raw.values()):
                    raise ResearchError("malformed_panel_record")
                period_index = _integer(raw["period_index"])
                assert period_index is not None
                identifier = raw["canonical_identifier"]
                episode = raw["identity_episode_id"]
                amount = _integer(raw["amount_ore"], nullable=True)
                eligible = _boolean(raw["eligible"])
                reason = raw["exclusion_reason"]
                if period_index >= len(starts) or not re.fullmatch("[0-9]{6}", identifier):
                    raise ResearchError("invalid_panel_key")
                if (
                    not raw["provenance_id"]
                    or (eligible and (not episode or not amount or reason))
                    or (
                        not eligible and (not reason or reason in {"observed", "not_observed_rise"})
                    )
                ):
                    raise ResearchError("invalid_eligibility_or_provenance")
                if raw["abc"] not in {"", "A", "B", "C", "unknown"}:
                    raise ResearchError("unsupported_ordinary_abc")
                supply = raw.get("reported_supply_positive", "")
                if supply not in {"", "true"}:
                    raise ResearchError("supply_absence_is_not_negative")
                row = Row(
                    period_index,
                    identifier,
                    episode,
                    amount,
                    eligible,
                    reason,
                    raw["abc"],
                    raw["group_id"],
                    raw["provenance_id"],
                    _integer(raw.get("lifecycle_age_periods", ""), nullable=True),
                    raw.get("identity_state", "unknown") or "unknown",
                    True if supply == "true" else None,
                    _boolean(raw.get("supply_coverage_unknown", "true")),
                )
                key = (period_index, identifier)
                if key in lookup:
                    raise ResearchError("duplicate_package_period")
                lookup[key] = row
                rows.append(row)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ResearchError("unreadable_panel") from exc
    rows.sort(key=lambda row: (row.period_index, row.canonical_identifier))
    episodes: dict[str, tuple[str, int, bool]] = {}
    for row in rows:
        if not row.identity_episode_id:
            continue
        previous = episodes.get(row.identity_episode_id)
        if previous:
            identifier, index, was_eligible = previous
            if (
                identifier != row.canonical_identifier
                or row.period_index != index + 1
                or (row.eligible and not was_eligible)
            ):
                raise ResearchError("identity_episode_bridges_gap_or_ineligible_interval")
        episodes[row.identity_episode_id] = (
            row.canonical_identifier,
            row.period_index,
            row.eligible,
        )
    grouped: list[list[Row]] = [[] for _ in starts]
    for row in rows:
        grouped[row.period_index].append(row)
    # Bind parsed context to the exact captured manifest bytes, rather than a
    # later version of the path. Reverify the entire pinned recipe after parsing.
    if digest(manifest_path) != manifest_hash:
        raise ResearchError("input_manifest_changed_during_load")
    pinned_files(manifest_path.parent, manifest["files"])
    if digest(manifest_path) != manifest_hash:
        raise ResearchError("input_manifest_changed_during_load")
    return Dataset(
        manifest_path,
        manifest_hash,
        manifest_bytes,
        manifest,
        tuple(starts),
        tuple(rows),
        tuple(tuple(group) for group in grouped),
        lookup,
    )


def validate_dataset(manifest_path: Path) -> dict[str, Any]:
    dataset = load_dataset(manifest_path)
    return {
        "dataset_id": dataset.manifest["dataset_id"],
        "manifest_sha256": dataset.manifest_hash,
        "rows": len(dataset.rows),
        "eligible": sum(row.eligible for row in dataset.rows),
        "periods": len(dataset.periods),
    }
