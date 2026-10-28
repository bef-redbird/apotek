"""Read pinned E2 JSON summaries only; never import fitting or database code."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .dataset import ResearchError, pinned_files, read_json


@dataclass(frozen=True)
class E2SavedRun:
    manifest: dict[str, Any]
    artifacts: dict[str, dict[str, Any]]


def _rows(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = value.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ResearchError("invalid_e2_rows")
    return rows


def _scores(row: dict[str, Any]) -> None:
    if type(row.get("n")) is not int or row["n"] < 0 or not isinstance(row.get("metrics"), dict):
        raise ResearchError("invalid_e2_score")
    if any(
        not isinstance(key, str)
        or (value is not None and (type(value) not in {int, float} or not math.isfinite(value)))
        for key, value in row["metrics"].items()
    ):
        raise ResearchError("invalid_e2_score")


def _scope(row: dict[str, Any], manifest: dict[str, Any]) -> None:
    task, split, method = row.get("task"), row.get("split"), row.get("method")
    if (
        not isinstance(task, str)
        or task not in manifest["methods"]
        or not isinstance(split, str)
        or split not in manifest["split_dates"]
        or method not in manifest["methods"][task]
    ):
        raise ResearchError("invalid_e2_result_scope")


def load_e2_run(path: Path) -> E2SavedRun:
    """Reject incomplete results and verify every byte before displaying any result."""
    manifest = read_json(path)
    if (
        manifest.get("schema_version") != "e2-saved-results-v1"
        or manifest.get("status") != "completed"
        or manifest.get("evaluation_locked") is not True
    ):
        raise ResearchError("unsupported_or_incomplete_e2")
    for key in ("dataset", "definitions", "split_dates", "methods", "baseline_by_task"):
        if not isinstance(manifest.get(key), dict) or not manifest[key]:
            raise ResearchError("invalid_e2_manifest")
    if not isinstance(manifest.get("exposure_status"), str) or not manifest["exposure_status"]:
        raise ResearchError("missing_e2_exposure")
    candidates = manifest.get("candidate_methods")
    if (
        not isinstance(candidates, list)
        or not candidates
        or any(not isinstance(value, str) for value in candidates)
    ):
        raise ResearchError("invalid_e2_candidates")
    for split, dates in manifest["split_dates"].items():
        if not isinstance(split, str) or not isinstance(dates, dict):
            raise ResearchError("invalid_e2_split")
        try:
            start, end = (date.fromisoformat(dates[key]) for key in ("start", "end"))
        except (ValueError, KeyError, TypeError) as exc:
            raise ResearchError("invalid_e2_split") from exc
        if start > end:
            raise ResearchError("invalid_e2_split")
    for task, methods in manifest["methods"].items():
        if (
            not isinstance(task, str)
            or not isinstance(methods, list)
            or not methods
            or any(not isinstance(method, str) for method in methods)
            or manifest["baseline_by_task"].get(task) not in methods
        ):
            raise ResearchError("invalid_e2_methods")
    files = manifest.get("files")
    if not isinstance(files, list) or any(not isinstance(pin, dict) for pin in files):
        raise ResearchError("invalid_e2_roles")
    roles = [pin.get("role") for pin in files]
    expected = {"metrics", "coverage", "history_curves", "comparisons", "protocol"}
    if set(roles) != expected or len(roles) != len(expected):
        raise ResearchError("invalid_e2_roles")
    # JSON is the entire UI boundary. Models/pickle/code cannot be added as a role.
    if any(
        not isinstance(pin.get("path"), str) or Path(pin["path"]).suffix != ".json" for pin in files
    ):
        raise ResearchError("invalid_e2_artifact_type")
    pinned_files(path.parent, files)
    artifacts = {pin["role"]: read_json(path.parent / pin["path"]) for pin in manifest["files"]}
    for key in ("aggregate", "per_period", "calibration", "uncertainty"):
        seen = set()
        for row in _rows(artifacts["metrics"], key):
            task, split, method = row.get("task"), row.get("split"), row.get("method")
            _scope(row, manifest)
            if key in {"aggregate", "per_period"}:
                _scores(row)
                identity = (
                    task,
                    split,
                    method,
                    row.get("period_start") if key == "per_period" else "",
                )
                if identity in seen:
                    raise ResearchError("duplicate_e2_result")
                seen.add(identity)
            if key == "per_period":
                try:
                    date.fromisoformat(row["period_start"])
                except (ValueError, TypeError, KeyError) as exc:
                    raise ResearchError("invalid_e2_period") from exc
    if not isinstance(artifacts["coverage"].get("overall"), dict):
        raise ResearchError("invalid_e2_coverage")
    _rows(artifacts["coverage"], "per_period")
    if "trial_statuses" in artifacts["coverage"]:
        for row in _rows(artifacts["coverage"], "trial_statuses"):
            if row.get("status") not in {"completed", "failed"}:
                raise ResearchError("unfinished_e2_trial")
    for row in _rows(artifacts["history_curves"], "experiments"):
        _scope(row, manifest)
        _scores(row)
        if type(row.get("history_years")) not in {int, float} or row["history_years"] <= 0:
            raise ResearchError("invalid_e2_history")
    for row in _rows(artifacts["comparisons"], "incremental"):
        _scores(row)
    if "secondary" in artifacts["comparisons"]:
        seen_secondary = set()
        for row in _rows(artifacts["comparisons"], "secondary"):
            _scores(row)
            candidate = row.get("candidate", row.get("method"))
            _scope({**row, "method": candidate}, manifest)
            if (
                row.get("method", candidate) != candidate
                or row.get("baseline") not in manifest["methods"][row["task"]]
                or row.get("metric")
                not in {
                    "brier",
                    "mae_dkk",
                    "rise_precision_top_decile",
                    "fall_fraction_top_decile",
                    "shortlist_unknown_fraction",
                }
                or type(row.get("periods")) is not int
                or row["periods"] < 0
                or not {
                    "difference",
                    "ci_lower",
                    "ci_upper",
                    "sensitivity_lower",
                    "sensitivity_upper",
                }.issubset(row["metrics"])
            ):
                raise ResearchError("invalid_e2_secondary_comparison")
            for lower, upper in (
                ("ci_lower", "ci_upper"),
                ("sensitivity_lower", "sensitivity_upper"),
            ):
                low, high = row["metrics"][lower], row["metrics"][upper]
                if (low is None) != (high is None) or (low is not None and low > high):
                    raise ResearchError("invalid_e2_secondary_interval")
            secondary_identity = (
                row["task"],
                row["split"],
                candidate,
                row["baseline"],
                row["metric"],
            )
            if secondary_identity in seen_secondary:
                raise ResearchError("duplicate_e2_secondary_comparison")
            seen_secondary.add(secondary_identity)
    return E2SavedRun(manifest, artifacts)
