"""Saved-only viewer for the separately frozen E6 volatility follow-up."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import streamlit as st


class VolatilityEvidenceError(ValueError):
    """Missing, incompatible or changed saved evidence."""


def _read_pin(root: Path, reference: Any, suffixes: set[str]) -> bytes:
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise VolatilityEvidenceError("A saved artifact reference is missing.")
    name = reference["path"]
    if "://" in name:
        raise VolatilityEvidenceError("Only local saved evidence is supported.")
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path.suffix.lower() not in suffixes:
        raise VolatilityEvidenceError(
            "Artifact must use a supported format inside the saved folder."
        )
    data = path.read_bytes()
    if len(data) != reference.get("size_bytes") or hashlib.sha256(
        data
    ).hexdigest() != reference.get("sha256"):
        raise VolatilityEvidenceError("Saved evidence checksum does not match; no results shown.")
    return data


def _markdown(value: Any) -> str:
    if not isinstance(value, str):
        raise VolatilityEvidenceError("Saved explanatory text is missing.")
    # Images are displayed only from separately verified local byte references.
    if re.search(r"!\s*\[|<\s*(?:img|video|audio|iframe|object)\b", value, re.IGNORECASE):
        raise VolatilityEvidenceError("Markdown media must use a separately pinned local chart.")
    return value


def load_evidence(index_path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Validate the entire bundle before displaying any result; never open a database."""
    try:
        root = index_path.resolve().parent
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if index.get("version") != "e6-volatility-index-v1":
            raise VolatilityEvidenceError("Incompatible saved volatility index.")
        if index.get("status") != "research_not_promoted":
            raise VolatilityEvidenceError("Saved research status is missing or incompatible.")
        artifacts = index["artifacts"]
        report = _markdown(_read_pin(root, artifacts["report"], {".md"}).decode("utf-8"))
        examples = json.loads(_read_pin(root, artifacts["examples"], {".json"}))
        if examples.get("version") != "e6-volatility-examples-v1":
            raise VolatilityEvidenceError("Incompatible saved examples.")
        if not isinstance(examples.get("records"), list):
            raise VolatilityEvidenceError("Saved example records are missing.")
        # Verify supporting machine-readable evidence even when it is not shown.
        for name, reference in artifacts.items():
            if name not in {"report", "examples"}:
                _read_pin(root, reference, {".json", ".parquet", ".csv"})
        records = []
        for record in examples["records"]:
            title = record["title"]
            if not isinstance(title, str):
                raise VolatilityEvidenceError("Saved example title is missing.")
            chart = _read_pin(root, record["chart"], {".png"})
            if not chart.startswith(b"\x89PNG\r\n\x1a\n"):
                raise VolatilityEvidenceError("Saved chart is not a PNG image.")
            records.append(
                {"title": title, "markdown": _markdown(record["markdown"]), "chart": chart}
            )
        return report, records
    except VolatilityEvidenceError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise VolatilityEvidenceError(
            "Saved volatility evidence is missing or unreadable."
        ) from exc


def main(index_path: Path | None = None) -> None:
    st.set_page_config(page_title="E6 historical volatility", layout="wide")
    st.title("E6 · Historical price volatility")
    st.caption("Saved research follow-up · original predictions and selections remain frozen.")
    if index_path is None:
        st.info("Choose saved evidence with --e6-volatility <index.json>.")
        return
    try:
        report, examples = load_evidence(index_path)
    except VolatilityEvidenceError as exc:
        st.error(str(exc))
        return
    st.warning("Exposed retrospective research; no model or purchasing policy was promoted.")
    st.markdown(report)
    st.subheader("Source-verified examples")
    if not examples:
        st.info("No saved examples are available.")
    for record in examples:
        st.subheader(record["title"])
        st.markdown(record["markdown"])
        st.image(record["chart"], width="stretch")
