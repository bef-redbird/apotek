"""Saved-only prospective comparison view; no inference or operation imports."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

HISTORY = {
    "3y": "Three years of training history",
    "6y": "Six years of training history",
    "full": "All supported history from 2008",
}


class SavedComparisonError(ValueError):
    """Missing or corrupt publications must never look accepted."""


def saved_bytes(root: Path, ref: dict[str, Any]) -> bytes:
    name = ref["path"]
    if not isinstance(name, str) or "://" in name:
        raise SavedComparisonError("Only local saved artifacts are supported.")
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise SavedComparisonError("Display artifacts must be inside the saved operation root.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref["sha256"] or len(data) != ref["size_bytes"]:
        raise SavedComparisonError("Saved evidence checksum differs; no results shown.")
    return data


def load_saved(path: Path) -> dict[str, Any]:
    """Validate every displayed byte before returning any results."""
    try:
        root = path.resolve().parent
        index = json.loads(path.read_bytes())
        if index["version"] != "prospective-index-v1":
            raise SavedComparisonError("Incompatible comparison index.")
        result = {
            key: json.loads(saved_bytes(root, index["artifacts"][key]))
            for key in ("protocol", "readiness", "report")
        }
        forecasts = []
        for item in index.get("forecasts", []):
            assessment = json.loads(saved_bytes(root, item["assessment"]))
            predictions = pd.read_parquet(io.BytesIO(saved_bytes(root, item["predictions"])))
            if assessment.get("predictions", {}).get("sha256") != item["predictions"]["sha256"]:
                raise SavedComparisonError("Forecast publication does not bind its predictions.")
            record = {"assessment": assessment, "predictions": predictions}
            if item.get("outcome"):
                record["outcome"] = json.loads(saved_bytes(root, item["outcome"]))
                record["outcome_rows"] = pd.read_parquet(
                    io.BytesIO(saved_bytes(root, item["outcome_rows"]))
                )
                if record["outcome"]["rows"]["sha256"] != item["outcome_rows"]["sha256"]:
                    raise SavedComparisonError("Outcome publication does not bind its rows.")
            forecasts.append(record)
        result["forecasts"] = forecasts
        return result
    except SavedComparisonError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SavedComparisonError("Saved comparison is missing, incomplete or corrupt.") from exc


def table(records: Any) -> None:
    if not records:
        st.info("No saved observations yet.")
        return
    frame = pd.DataFrame(records)
    if "history" in frame:
        frame["history"] = frame.history.map(lambda x: HISTORY.get(x, x))
    frame = frame.rename(
        columns={
            "bias_dkk": "Average forecast overstatement/understatement (DKK)",
            "bias": "Average forecast overstatement/understatement (DKK)",
            "mean_prediction": "Mean forecast change (DKK)",
            "mean_actual": "Mean observed change (DKK)",
            "mae": "Mean absolute error (DKK)",
            "rmse": "Mean period root mean squared error (DKK)",
            "unknown": "Observed outcomes still missing",
            "mae_dkk": "Mean absolute error (DKK)",
            "rmse_dkk": "Period root mean squared error (DKK)",
        }
    )
    st.dataframe(frame, hide_index=True, width="stretch")


def main(index_path: Path | None = None) -> None:
    st.set_page_config(page_title="Prospective amount comparison", layout="wide")
    st.title("Prospective amount comparison")
    st.caption("Saved research evidence · fixed probability shortlist · manual operation")
    if index_path is None:
        st.info("Choose saved evidence with --prospective <index.json>.")
        return
    try:
        data = load_saved(index_path)
    except SavedComparisonError as exc:
        st.error(str(exc))
        return
    readiness, protocol, report = (data[k] for k in ("readiness", "protocol", "report"))
    st.warning(
        "Implementation checks are separate from future performance. "
        "Price forecasts are not pharmacy savings or purchase quantities."
    )
    st.subheader("Readiness and observation schedule")
    status_label = str(readiness.get("status", "Readiness not established"))
    st.write(
        {
            "blocked_external_inputs": "Not ready for prospective enrolment",
            "not_ready": "Not ready: saved evidence needs attention",
            "manual_ready": "Ready for manual operation",
        }.get(status_label, status_label)
    )
    missing = readiness.get("missing", readiness.get("blockers", []))
    for reason in missing:
        st.info(str(reason))
    st.caption(
        "Research cutoff: Wednesday in tariff week two at 12:00 Europe/Copenhagen. "
        "This is not a confirmed final purchasing deadline."
    )
    schedule = (
        report.get("schedule")
        or readiness.get("schedule")
        or readiness.get("candidate_schedule", [])
    )
    if isinstance(schedule, dict):
        schedule = schedule.get("origins", schedule.get("rows", schedule.get("periods", [])))
    table(schedule)
    st.write(
        "Six scheduled origins: descriptive interim only. Twelve consecutive scheduled origins: "
        "reporting milestone. Missed origins and missing outcomes remain in the denominator."
    )
    st.subheader("Frozen comparison")
    for label in HISTORY.values():
        st.write(label)
    st.caption(
        "All three histories use the same selected rows within each information mode. "
        "Model disagreement is not a calibrated confidence interval."
    )
    with st.expander("Protocol, model identities and saved calibration"):
        st.json(protocol)
    forecasts = data["forecasts"]
    if not forecasts:
        st.info("No prospective forecast has been registered. Advance unavailable until verified.")
    else:
        names = [
            f"{x['assessment'].get('context', {}).get('origin', '?')} → "
            f"{x['assessment'].get('context', {}).get('target', '?')} · "
            f"{x['assessment'].get('context', {}).get('information_mode', '?')} · "
            f"{x['assessment'].get('status', 'unclassified')}"
            for x in forecasts
        ]
        chosen = st.selectbox("Saved forecast", range(len(names)), format_func=lambda i: names[i])
        item = forecasts[chosen]
        with st.expander("Freeze, sources and availability"):
            st.json(item["assessment"])
        frame = item["predictions"]
        selected_only = st.checkbox("Show fixed reference shortlist only", value=True)
        query = st.text_input("Find package identifier")
        limit = st.number_input("Rows to display", min_value=1, max_value=100000, value=50)
        shown = frame.loc[frame.selected.fillna(False)] if selected_only else frame
        if query:
            shown = shown.loc[shown.package_number.astype(str).str.contains(query, regex=False)]
        if "rank" in shown:
            shown = shown.sort_values("rank", kind="stable")
        amounts = [f"amount_{h}_delta_dkk" for h in HISTORY]
        cols = [c for c in ["package_number", "rank", "selected", "p2", *amounts] if c in shown]
        display = shown[cols].head(int(limit)).copy()
        if set(amounts).issubset(display.columns):
            display["Forecast disagreement (DKK)"] = display[amounts].max(axis=1) - display[
                amounts
            ].min(axis=1)
        display = display.rename(
            columns={
                "p2": "Probability of a rise",
                **{
                    f"amount_{h}_delta_dkk": f"{label} · change (DKK)"
                    for h, label in HISTORY.items()
                },
            }
        )
        st.dataframe(display, hide_index=True, width="stretch")
        with st.expander("Raw forecasts, supported prices, routing and exclusion reasons"):
            st.dataframe(shown.head(int(limit)), hide_index=True, width="stretch")
        if "outcome" in item:
            st.caption("Outcome version and as-of time are retained separately from this forecast.")
            st.json(item["outcome"])
            st.dataframe(item["outcome_rows"], hide_index=True, width="stretch")
        else:
            st.info("Observed outcomes still missing. No performance conclusion is supported.")
    st.subheader("Per-period results")
    table(report.get("periods", []))
    st.subheader("Provisional accumulated findings")
    table(report.get("summaries", []))
    st.caption("Bias means forecast minus observed change; positive is overstatement.")
    blocks = report.get("temporal_blocks")
    if blocks:
        st.json(blocks)
    else:
        st.info("Insufficient evidence for temporal uncertainty or a performance conclusion.")
    st.caption(
        "Viewing saved artifacts performs no inference, fitting, acquisition or database writes."
    )
