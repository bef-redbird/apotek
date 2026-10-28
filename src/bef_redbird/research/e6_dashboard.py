"""Question-led, read-only viewer for checksum-pinned E6 evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from .e6_result_view import SavedEvidenceError, label, load_summary, table_frame

SECTION_NAMES = {
    "A": "Current-only selection",
    "B": "Consequences of actual Cito classification codes",
    "C": "Robust amount forecasts in outer evaluation periods",
    "D": "Three years, six years or full training history",
}


def show_evidence(value: Any) -> None:
    """Display flexible saved comparisons without inventing a result or ranking."""
    if value is None or value == [] or value == {} or value == "":
        st.info("No saved comparison is available for this question yet.")
    elif isinstance(value, list):
        if all(isinstance(row, dict) for row in value):
            st.dataframe(table_frame(value), hide_index=True, width="stretch")
        else:
            for item in value:
                show_evidence(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            st.markdown(f"**{label(key)}**")
            show_evidence(item)
    else:
        st.write(value)


def show_section(section_id: str, value: Any) -> None:
    """Keep saved direction and amount measures in separate history tables."""
    if (
        section_id != "D"
        or not isinstance(value, dict)
        or not isinstance(value.get("records"), list)
    ):
        show_evidence(value)
        return
    groups: dict[str, list[dict[str, Any]]] = {
        "Direction forecasts": [],
        "Price-change amount forecasts": [],
        "Other saved history evidence": [],
    }
    for row in value["records"]:
        if not isinstance(row, dict):
            show_evidence(value)
            return
        names = [str(key).lower() for key, item in row.items() if item is not None]
        direction = any("brier" in name for name in names)
        amount = any(
            "absolute error" in name
            or "mae_dkk" in name
            or "rmse_dkk" in name
            or "root mean squared" in name
            for name in names
        )
        group = (
            "Direction forecasts"
            if direction and not amount
            else (
                "Price-change amount forecasts"
                if amount and not direction
                else "Other saved history evidence"
            )
        )
        groups[group].append(row)
    if not groups["Direction forecasts"] and not groups["Price-change amount forecasts"]:
        show_evidence(value)
        return
    for key, item in value.items():
        if key != "records":
            show_evidence({key: item})
    for title, rows in groups.items():
        if rows:
            st.markdown(f"**{title}**")
            if title == "Direction forecasts":
                st.caption(
                    "Brier error and logarithmic loss measure probability mistakes; lower is "
                    "better. Calibration error measures how closely predicted chances match "
                    "observed frequencies."
                )
            elif title == "Price-change amount forecasts":
                st.caption(
                    "Mean absolute error is the average kroner miss per pack. Root mean "
                    "squared error gives larger mistakes more weight. Lower is better for "
                    "both; signed bias should be near zero."
                )
            st.dataframe(
                table_frame(rows).dropna(axis=1, how="all"), hide_index=True, width="stretch"
            )


def main(index_path: Path | None = None) -> None:
    st.set_page_config(page_title="E6 research answers", layout="wide")
    st.title("E6 · What did the next comparisons show?")
    st.caption(
        "Saved historical research. These findings support pharmacist review; "
        "they do not change the purchasing policy."
    )
    if index_path is None:
        st.info("Choose a saved E6 index with --e6 <index.json>.")
        return
    try:
        index, summary = load_summary(index_path)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, SavedEvidenceError) as exc:
        st.error(f"Saved E6 results could not be read safely: {exc}")
        st.info("Restore compatible pinned evidence. No live fallback or model execution is used.")
        return

    st.warning(
        "These comparisons use exposed retrospective history. They do not establish future "
        "performance, realised pharmacy savings or an operational purchasing policy."
    )
    if summary.get("exposure"):
        show_evidence(summary["exposure"])
    st.caption(
        "Current-only forecasts use no next-period classification. Conditional ABC comparisons "
        "assume next-period classification is available. Actual Cito codes and corrected official "
        "codes are separate retrospective cases. Natural queues show each rule's own workload; "
        "matched workload is a diagnostic comparison. AIP means pharmacy purchase price. "
        "DKK means Danish kroner. Price movement is an exposure, not realised profit."
    )
    questions = summary.get("questions", [])
    if not questions:
        st.info("No question answers have been saved yet. This is not evidence of success.")
    for question in questions:
        st.subheader(str(question["title"]))
        st.caption(f"Saved status: {str(question['status']).replace('_', ' ')}")
        st.write(question["answer"])
        if question.get("practical_meaning"):
            st.markdown("**What this means in practice**")
            show_evidence(question["practical_meaning"])
        if question.get("limitations"):
            st.markdown("**Limits on this answer**")
            show_evidence(question["limitations"])
        with st.expander(f"Evidence and technical details · {question['id']}"):
            show_evidence(question.get("comparison"))
            section = summary.get("sections", {}).get(str(question["id"]))
            if section:
                show_section(str(question["id"]), section)
    # Preserve saved sections even when a producer has not written its answer yet.
    answered = {str(question["id"]) for question in questions}
    for section_id, section in summary.get("sections", {}).items():
        if section_id not in answered:
            with st.expander(SECTION_NAMES.get(section_id, label(section_id))):
                show_section(section_id, section)
    with st.expander("Resources, protocol and saved provenance"):
        st.caption(
            "This viewer only reads saved artifacts. It does not fit models, access the live "
            "database, acquire files, place orders or promote models. Earlier E4 and E5 views "
            "remain available through --e4 and --e5."
        )
        show_evidence(summary.get("resources"))
        if summary.get("limitations"):
            show_evidence(summary["limitations"])
        st.caption("Artifact references identify saved evidence; this viewer does not load models.")
        st.json(index)
