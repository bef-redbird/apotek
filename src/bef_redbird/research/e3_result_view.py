"""Strict saved-only E3 viewing boundary; no foundation or model execution imports."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dataset import ResearchError, digest, parse_json_bytes, read_json

JSON_ROLES = frozenset(
    {
        "summary",
        "comparisons",
        "scenarios",
        "price_uncertainty",
        "queue_tradeoffs",
        "peer_coverage",
        "trials",
    }
)
PARQUET_ROLES = frozenset({"candidate_review", "candidate_outcomes"})
PROOF_FIELDS = ("periods_sha256", "candidate_keys_sha256", "outcomes_sha256")


@dataclass(frozen=True)
class E3RunIndex:
    root: Path
    runs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class E3SavedRun:
    root: Path
    manifest: dict[str, Any]
    artifacts: dict[str, dict[str, Any]]
    pins: dict[str, dict[str, Any]]


def _pin_path(root: Path, pin: dict[str, Any]) -> Path:
    relative = pin.get("path")
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ResearchError("invalid_e3_artifact_path")
    path = root / relative
    if (
        Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or not path.resolve().is_relative_to(root.resolve())
    ):
        raise ResearchError("e3_artifact_path_escape")
    _validate_pin(pin)
    return path


def _validate_pin(pin: dict[str, Any]) -> None:
    if (
        not isinstance(pin.get("sha256"), str)
        or not re.fullmatch("[0-9a-f]{64}", pin["sha256"])
        or type(pin.get("size_bytes")) is not int
        or pin["size_bytes"] < 0
    ):
        raise ResearchError("invalid_e3_file_pin")


def _captured(path: Path, pin: dict[str, Any]) -> bytes:
    """Parse the same bytes whose hash was checked, including on later review reads."""
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ResearchError("e3_artifact_unavailable") from exc
    if len(payload) != pin["size_bytes"] or hashlib.sha256(payload).hexdigest() != pin["sha256"]:
        raise ResearchError("e3_artifact_hash_mismatch")
    return payload


def load_e3_index(path: Path) -> E3RunIndex:
    """Read only the explicitly supplied catalogue, never scan directories for runs."""
    value = read_json(path)
    runs = value.get("runs")
    if value.get("schema_version") != "e3-run-index-v1" or not isinstance(runs, list) or not runs:
        raise ResearchError("invalid_e3_run_index")
    identities: set[str] = set()
    paths: set[Path] = set()
    for run in runs:
        if not isinstance(run, dict) or any(
            not isinstance(run.get(key), str) or not run[key] for key in ("run_id", "label")
        ):
            raise ResearchError("invalid_e3_index_entry")
        selected = _pin_path(path.parent, run)
        if selected.suffix != ".json" or run["run_id"] in identities or selected.resolve() in paths:
            raise ResearchError("duplicate_or_invalid_e3_index_entry")
        _captured(selected, run)
        identities.add(run["run_id"])
        paths.add(selected.resolve())
    return E3RunIndex(path.parent, tuple(runs))


def load_indexed_e3_run(index: E3RunIndex, run_id: str) -> E3SavedRun:
    selected = next((entry for entry in index.runs if entry["run_id"] == run_id), None)
    if selected is None:
        raise ResearchError("e3_run_not_in_index")
    path = _pin_path(index.root, selected)
    return load_e3_run(path, manifest_pin=selected)


def _rows(value: dict[str, Any]) -> list[dict[str, Any]]:
    rows = value.get("rows")
    limitations = value.get("limitations")
    if (
        not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
        or not isinstance(limitations, list)
        or any(not isinstance(item, str) for item in limitations)
    ):
        raise ResearchError("invalid_e3_rows")
    return rows


def load_e3_run(path: Path, *, manifest_pin: dict[str, Any] | None = None) -> E3SavedRun:
    manifest = parse_json_bytes(_captured(path, manifest_pin)) if manifest_pin else read_json(path)
    if (
        manifest.get("schema_version") != "e3-saved-results-v1"
        or manifest.get("status") != "completed"
        or manifest.get("evaluation_locked") is not True
        or not isinstance(manifest.get("exposure_status"), str)
        or not manifest["exposure_status"]
        or not isinstance(manifest.get("definitions"), dict)
        or not manifest["definitions"]
        or not isinstance(manifest.get("split_dates"), list)
        or not manifest["split_dates"]
    ):
        raise ResearchError("unsupported_or_incomplete_e3")
    files = manifest.get("files")
    if not isinstance(files, list) or any(not isinstance(pin, dict) for pin in files):
        raise ResearchError("invalid_e3_roles")
    roles = [pin.get("role") for pin in files]
    if set(roles) != JSON_ROLES | PARQUET_ROLES or len(roles) != len(set(roles)):
        raise ResearchError("invalid_e3_roles")
    artifacts: dict[str, dict[str, Any]] = {}
    pins: dict[str, dict[str, Any]] = {}
    paths: set[Path] = set()
    for pin in files:
        artifact = _pin_path(path.parent, pin)
        role = pin["role"]
        if artifact.resolve() in paths:
            raise ResearchError("duplicate_e3_artifact")
        paths.add(artifact.resolve())
        expected_suffix = ".json" if role in JSON_ROLES else ".parquet"
        if artifact.suffix != expected_suffix:
            raise ResearchError("invalid_e3_artifact_type")
        if role in JSON_ROLES:
            artifacts[role] = parse_json_bytes(_captured(artifact, pin))
            _rows(artifacts[role])
        else:
            try:
                if (
                    artifact.stat().st_size != pin["size_bytes"]
                    or digest(artifact) != pin["sha256"]
                ):
                    raise ResearchError("e3_artifact_hash_mismatch")
            except OSError as exc:
                raise ResearchError("e3_artifact_unavailable") from exc
        pins[role] = pin
    binding = artifacts["comparisons"].get("binding")
    if (
        not isinstance(binding, dict)
        or binding.get("verified") is not True
        or not isinstance(binding.get("target"), str)
        or not binding["target"]
        or any(
            not isinstance(binding.get(field), str)
            or not re.fullmatch("[0-9a-f]{64}", binding[field])
            for field in PROOF_FIELDS
        )
    ):
        raise ResearchError("unreconciled_e3_comparison")
    reference = binding.get("e2_manifest")
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise ResearchError("missing_e3_e2_reference")
    _validate_pin(reference)
    reference_path = Path(reference["path"])
    if not reference_path.is_absolute() or reference_path.suffix != ".json":
        raise ResearchError("invalid_e3_e2_reference")
    reference_value = parse_json_bytes(_captured(reference_path, reference))
    if (
        reference_value.get("schema_version") != "e2-saved-results-v1"
        or reference_value.get("status") != "completed"
        or reference_value.get("evaluation_locked") is not True
    ):
        raise ResearchError("invalid_e3_e2_reference")
    for row in artifacts["trials"]["rows"]:
        if row.get("status") not in {"completed", "failed"}:
            raise ResearchError("unfinished_e3_trial")
    for row in artifacts["comparisons"]["rows"]:
        lower, upper = row.get("lower"), row.get("upper")
        if (lower is None) != (upper is None) or (
            lower is not None
            and (
                type(lower) not in {int, float} or type(upper) not in {int, float} or lower > upper
            )
        ):
            raise ResearchError("invalid_e3_comparison_interval")
    return E3SavedRun(path.parent, manifest, artifacts, pins)


def load_candidate_review(run: E3SavedRun, *, reveal_outcomes: bool = False) -> Any:
    """Read saved candidate evidence on demand; actual outcomes require an explicit choice."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    def table(role: str) -> Any:
        pin = run.pins[role]
        return pq.read_table(pa.BufferReader(_captured(_pin_path(run.root, pin), pin)))

    review_table = table("candidate_review")
    review = review_table.to_pandas()
    required = {
        "row_key",
        "package_number",
        "period_start",
        "target_period",
        "scenario",
        "policy",
        "next_aip_dkk",
        "delta_dkk",
        "selected",
    }
    if not required.issubset(review.columns) or any(
        column in review for column in ("next_status", "next_aip_ore", "next_direction")
    ):
        raise ResearchError("invalid_e3_candidate_review")
    if (
        not pa.types.is_boolean(review_table.schema.field("selected").type)
        or review.selected.isna().any()
    ):
        raise ResearchError("invalid_e3_candidate_review_selection")
    if review.row_key.isna().any() or review.row_key.duplicated().any():
        raise ResearchError("invalid_e3_candidate_review_keys")
    outcome_pin = run.pins["candidate_outcomes"]
    outcome_file = pq.ParquetFile(
        pa.BufferReader(_captured(_pin_path(run.root, outcome_pin), outcome_pin))
    )
    columns = {"row_key", "next_status", "next_aip_ore", "next_direction"}
    if set(outcome_file.schema_arrow.names) != columns:
        raise ResearchError("invalid_e3_candidate_outcomes")
    # Check the full eligible universe without materializing actual labels before reveal.
    outcomes = outcome_file.read(columns=None if reveal_outcomes else ["row_key"]).to_pandas()
    if outcomes.row_key.isna().any() or outcomes.row_key.duplicated().any():
        raise ResearchError("invalid_e3_candidate_outcomes")
    if set(review.row_key) != set(outcomes.row_key):
        raise ResearchError("unreconciled_e3_candidate_outcomes")
    if reveal_outcomes:
        review = review.merge(outcomes, on="row_key", how="left", validate="one_to_one")
    return review
