"""Saved E6 evidence reader; no model, database or acquisition dependencies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


class SavedEvidenceError(ValueError):
    """Saved evidence is missing, incompatible or no longer matches its pin."""


def pinned(reference: Any, root: Path) -> Path:
    if not isinstance(reference, dict) or not {"path", "sha256"} <= reference.keys():
        raise SavedEvidenceError("Invalid saved artifact reference")
    raw = reference["path"]
    if not isinstance(raw, str) or "://" in raw:
        raise SavedEvidenceError("Only local saved evidence can be read")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise SavedEvidenceError("A pinned saved artifact is missing; restore its original bytes")
    if "size_bytes" in reference and path.stat().st_size != reference["size_bytes"]:
        raise SavedEvidenceError("Saved artifact size or checksum has changed")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != reference["sha256"]:
        raise SavedEvidenceError("Saved artifact checksum has changed")
    return path


def read_json(path: Path) -> dict[str, Any]:
    if path.suffix.lower() != ".json":
        raise SavedEvidenceError("Saved metadata must be JSON")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise SavedEvidenceError("Saved metadata must be an object")
    return value


def resolve_tables(value: Any, root: Path) -> Any:
    """Resolve only explicit JSON/Parquet pins, before showing any comparison."""
    if isinstance(value, dict):
        if {"path", "sha256"} <= value.keys():
            path = pinned(value, root)
            if path.suffix.lower() == ".parquet":
                return pd.read_parquet(path).to_dict("records")
            if path.suffix.lower() != ".json":
                raise SavedEvidenceError("Comparisons must be saved JSON or Parquet")
            decoded = json.loads(path.read_text(encoding="utf-8-sig"))
            # A pinned JSON comparison is data, never a further execution instruction.
            return resolve_tables(decoded, path.parent)
        return {key: resolve_tables(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_tables(item, root) for item in value]
    return value


def load_summary(index_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    index = read_json(index_path)
    if index.get("version") != "e6-index-v1":
        raise SavedEvidenceError("Incompatible saved E6 index")
    reference = index.get("artifacts", {}).get("summary", index.get("summary"))
    summary_path = pinned(reference, index_path.parent)
    summary = read_json(summary_path)
    questions = summary.get("questions", [])
    if not isinstance(questions, list) or any(not isinstance(q, dict) for q in questions):
        raise SavedEvidenceError("Saved questions must be a list of question objects")
    sections = summary.get("sections", {})
    if not isinstance(sections, dict):
        raise SavedEvidenceError("Saved comparison sections must be an object")
    for question in questions:
        if not all(key in question for key in ("id", "title", "answer", "status")):
            raise SavedEvidenceError("A saved question is missing its identity, answer or status")
    # Resolve only display evidence. Protocol/model pins remain provenance, never loaded models.
    for key in ("questions", "sections", "resources", "exposure", "limitations"):
        if key in summary:
            summary[key] = resolve_tables(summary[key], summary_path.parent)
    return index, summary


LABELS = {
    "brier": "Probability error · lower is better",
    "brier_score": "Probability error · lower is better",
    "mae_dkk": "Typical absolute error · DKK / pack · lower is better",
    "rmse_dkk": "Large-error sensitive error · DKK / pack · lower is better",
    "bias_dkk": "Mean forecast error · DKK / pack · zero is ideal",
    "rise_rate": "Observed rise share · higher favours rise selection",
    "fall_rate": "Observed fall share · lower favours fall avoidance",
    "coverage90": "90% interval coverage · target 90%",
    "width90_dkk": "90% interval width · DKK / pack",
    "observed": "Observed outcomes",
    "unknown_selected": "Selected with unknown outcomes",
    "matched_shortfall": "Matched workload gap",
    "method": "Forecast method",
    "information": "Information available",
    "workload": "Selection workload",
    "training_years": "Years of training history",
}
VALUES = {
    "probability_reference": "E5 probability selection",
    "amount_dkk": "Expected price-movement selection",
    "downside_aware": "Price-movement selection with a downside penalty",
    "original_e4_current": "Original E4 current-only probability reference",
    "e4_probability_original": "Original E4 current-only probability reference",
    "3y": "Three recent years of training",
    "6y": "Six recent years of training",
    "weighted": "Full history with more weight on recent periods",
    "current": "Current information only",
    "current_only": "Current information only",
    "advance": "Conditional next-period ABC classification",
    "hybrid": "Advance ABC with current-only fallback",
    "natural": "Natural selection queue",
    "matched": "Matched workload diagnostic",
    "actual": "Actual reported Cito codes · retrospective",
    "corrected": "Corrected official codes · retrospective counterfactual",
    "huber": "Robust amount forecast (Huber)",
    "rmse": "Mean-oriented amount forecast",
    "mae": "Median amount forecast",
    "no_change": "No price change reference",
    "full": "Full available training history",
}


def label(value: Any) -> str:
    name = str(value)
    text = name.replace("_dkk", " · DKK").replace("_", " ")
    return LABELS.get(name, text[:1].upper() + text[1:])


def table_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """View-only naming and øre conversion preserve exact saved values."""
    frame = pd.DataFrame(rows).copy()
    for column in list(frame):
        if str(column).endswith("_ore"):
            target = str(column)[:-4] + "_dkk"
            if target not in frame:
                frame[target] = pd.to_numeric(frame[column], errors="coerce") / 100
            frame = frame.drop(columns=[column])
    for column in frame:
        frame[column] = frame[column].map(
            lambda value: (
                VALUES.get(value, value.replace("_", " "))
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else value
            )
        )
        if frame[column].map(lambda value: isinstance(value, str)).any():
            frame[column] = frame[column].map(lambda value: None if pd.isna(value) else str(value))
    identity_names = {
        "method",
        "policy",
        "scenario",
        "report",
        "queue",
        "information",
        "evaluation",
        "workload",
    }
    leading = [column for name in identity_names for column in frame if str(column).lower() == name]
    priority = {
        name: i
        for i, name in enumerate(
            (
                "method",
                "policy",
                "scenario",
                "report",
                "queue",
                "information",
                "evaluation",
                "workload",
            )
        )
    }
    leading.sort(key=lambda column: priority[str(column).lower()])
    return frame[leading + [column for column in frame if column not in leading]].rename(
        columns=label
    )
