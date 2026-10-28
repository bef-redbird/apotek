"""Read-only saved-result boundary; no fitting, database or executable model imports."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dataset import ResearchError, fingerprint, pinned_files, read_json


@dataclass(frozen=True)
class SavedRun:
    manifest: dict[str, Any]
    metrics: dict[str, Any]
    coverage: dict[str, Any]


def verify_run(path: Path) -> dict[str, Any]:
    """Verify every pinned byte, streaming large files without parsing models/predictions."""
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path /= "run.json"
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != "e1-run-v1":
        raise ResearchError("unsupported_run_schema")
    if manifest.get("status") != "completed" or manifest.get("evaluation_locked") is not True:
        raise ResearchError("incomplete_or_unlocked_run")
    pinned_files(manifest_path.parent, manifest.get("files"))
    expected_roles = {"input_manifest", "predictions", "models", "metrics", "coverage"}
    roles = [pin["role"] for pin in manifest["files"]]
    if set(roles) != expected_roles or len(roles) != len(expected_roles):
        raise ResearchError("missing_or_duplicate_result_role")
    semantic = {
        key: value
        for key, value in manifest.items()
        if key not in {"created_at", "run_fingerprint", "lineage_event_hash"}
    }
    if fingerprint(semantic) != manifest.get("run_fingerprint"):
        raise ResearchError("run_fingerprint_mismatch")
    return manifest


def _records(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    result = value.get(key)
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise ResearchError("invalid_result_structure")
    return result


def load_saved_run(path: Path) -> SavedRun:
    manifest = verify_run(path)
    if manifest.get("method_version") != "e1-fixed-ridge-v1":
        raise ResearchError("unsupported_method_version")
    for field in ("dataset", "definitions", "splits", "split_dates", "cutoff_policy", "methods"):
        if not isinstance(manifest.get(field), dict) or not manifest[field]:
            raise ResearchError("invalid_result_structure")
    dataset = manifest["dataset"]
    if any(
        key not in dataset
        for key in (
            "dataset_id",
            "manifest_sha256",
            "price_context",
            "availability_mode",
            "population_definition",
            "identity_policy",
            "provenance",
        )
    ):
        raise ResearchError("invalid_result_structure")
    for task, methods in manifest["methods"].items():
        if task not in {"next_period", "reversal"} or not isinstance(methods, list) or not methods:
            raise ResearchError("invalid_result_structure")
        if any(not isinstance(method, str) for method in methods):
            raise ResearchError("invalid_result_structure")
    for split in manifest["splits"]:
        dates = manifest["split_dates"].get(split)
        if not isinstance(dates, dict) or not all(
            isinstance(dates.get(key), str) for key in ("start", "end")
        ):
            raise ResearchError("invalid_result_structure")
    manifest_path = Path(path) / "run.json" if Path(path).is_dir() else Path(path)
    files = {pin["role"]: manifest_path.parent / pin["path"] for pin in manifest["files"]}
    metrics, coverage = read_json(files["metrics"]), read_json(files["coverage"])
    for field in ("aggregate", "per_period", "calibration", "uncertainty"):
        for row in _records(metrics, field):
            task, split, method = row.get("task"), row.get("split"), row.get("method")
            if (
                task not in manifest["methods"]
                or split not in manifest["splits"]
                or method not in manifest["methods"][task]
            ):
                raise ResearchError("invalid_result_structure")
            if field in {"aggregate", "per_period"} and (
                type(row.get("n")) is not int
                or row["n"] < 0
                or not isinstance(row.get("metrics"), dict)
            ):
                raise ResearchError("invalid_result_structure")
            if field in {"aggregate", "per_period"} and any(
                not isinstance(name, str)
                or type(value) not in {int, float}
                or not math.isfinite(value)
                for name, value in row["metrics"].items()
            ):
                raise ResearchError("invalid_result_structure")
            if field == "per_period" and (
                type(row.get("period_index")) is not int
                or not isinstance(row.get("period_start"), str)
            ):
                raise ResearchError("invalid_result_structure")
    aggregate_keys = [(row["task"], row["split"], row["method"]) for row in metrics["aggregate"]]
    if len(aggregate_keys) != len(set(aggregate_keys)):
        raise ResearchError("duplicate_aggregate_result")
    for row in _records(coverage, "per_period"):
        if any(
            key not in row
            for key in (
                "period_index",
                "period_start",
                "split",
                "total_rows",
                "eligible_candidates",
                "excluded",
                "next_period_labels",
                "reversal_labels",
                "observed_rise_candidates",
            )
        ):
            raise ResearchError("invalid_result_structure")
        for key in ("total_rows", "eligible_candidates", "observed_rise_candidates"):
            if type(row[key]) is not int or row[key] < 0:
                raise ResearchError("invalid_result_structure")
    if not isinstance(coverage.get("overall"), dict):
        raise ResearchError("invalid_result_structure")
    return SavedRun(manifest, metrics, coverage)


def incompatibilities(left: SavedRun, right: SavedRun) -> list[str]:
    """Conservative comparison: identical panel pins prove identical eligible cases."""
    differences = []
    for field in (
        "dataset_id",
        "manifest_sha256",
        "price_context",
        "availability_mode",
        "population_definition",
        "identity_policy",
    ):
        if left.manifest["dataset"].get(field) != right.manifest["dataset"].get(field):
            differences.append(field)
    for field in ("definitions", "split_dates", "splits", "cutoff_policy"):
        if left.manifest.get(field) != right.manifest.get(field):
            differences.append(field)
    return differences


def lineage_events(path: Path, run_fingerprint: str) -> list[dict[str, Any]]:
    """Inspect an explicitly supplied append-only exposure log without changing it."""
    import json

    previous, sequence = "0" * 64, 0
    selected = []
    try:
        with path.open("rb") as source:
            for line in source:
                if not line.endswith(b"\n"):
                    raise ResearchError("partial_lineage_event")
                event = json.loads(line)
                stated = event.pop("event_hash")
                if (
                    event.get("sequence") != sequence
                    or event.get("previous_event_hash") != previous
                    or fingerprint(event) != stated
                ):
                    raise ResearchError("corrupt_lineage_chain")
                if event.get("run_fingerprint") == run_fingerprint:
                    selected.append(event)
                previous, sequence = stated, sequence + 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ResearchError("invalid_lineage_log") from exc
    return selected
