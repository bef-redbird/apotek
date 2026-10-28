"""Lean offline research dashboard; frozen metrics are never recomputed here."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from .dataset import ResearchError
from .result_view import SavedRun, incompatibilities, lineage_events, load_saved_run


def configured_paths(arguments: list[str] | None = None) -> list[Path]:
    """Explicit CLI paths, or an OS-path-separated environment list; never discover runs."""
    values = sys.argv[1:] if arguments is None else arguments
    if "--" in values:
        values = values[values.index("--") + 1 :]
    if not values:
        values = [value for value in os.environ.get("BEF_E1_RUNS", "").split(os.pathsep) if value]
    return list(dict.fromkeys(Path(value).expanduser() for value in values))


def _table(rows: list[dict[str, Any]]) -> None:
    if rows:
        frame = pd.json_normalize(rows, sep=" · ")
        priority = ["saved_run", "period_start", "method", "n"]
        priority += [name for name in frame.columns if name.startswith("metrics · ")]
        priority += [
            "metric",
            "lower",
            "upper",
            "n_observed_periods",
            "class_label",
            "bin_lower",
            "bin_upper",
            "mean_probability",
            "observed_frequency",
        ]
        ordered = [name for name in priority if name in frame]
        ordered += [name for name in frame.columns if name not in ordered]
        labels = {
            "brier_multiclass": "Brier (lower better)",
            "brier_binary": "Brier (lower better)",
            "direction_accuracy": "Direction accuracy",
            "reversal_accuracy": "Reversal accuracy",
            "mae_log_return": "Log-return MAE",
            "mae_price_dkk": "Price MAE (DKK)",
            "precision_top_decile_rise": "Top-decile rise precision",
            "n": "Observed cases",
            "lower": "95% lower",
            "upper": "95% upper",
            "n_observed_periods": "Observed periods",
        }
        st.dataframe(
            frame,
            hide_index=True,
            width="stretch",
            column_order=ordered,
            column_config={
                name: labels.get(
                    name.removeprefix("metrics · "), name.replace("_", " ").capitalize()
                )
                for name in frame.columns
            },
        )
    else:
        st.info("No saved observations for this selection. Missing outcomes are not zero errors.")


def _select(rows: list[dict[str, Any]], task: str, split: str) -> list[dict[str, Any]]:
    return [row for row in rows if row["task"] == task and row["split"] == split]


METHOD_LABELS = {
    "no_change": "No change",
    "no_reversal": "No reversal",
    "historical_frequency": "Historical frequency",
    "last_change": "Repeat last change",
    "ridge": "Fixed ridge",
}


def comparison_rows(run: SavedRun, task: str, split: str) -> list[dict[str, Any]]:
    """Describe saved point estimates; never infer paired significance from marginal CIs."""
    rows = {row["method"]: row for row in _select(run.metrics["aggregate"], task, split)}
    baseline = "no_change" if task == "next_period" else "no_reversal"
    specifications = (
        [
            ("brier_multiclass", "Direction probability error", "score", False),
            ("precision_top_decile_rise", "Rises among top 10% ranked", "%", True),
            ("mae_price_dkk", "Average absolute price error", "DKK/package", False),
        ]
        if task == "next_period"
        else [
            ("brier_binary", "Reversal probability error", "score", False),
            ("reversal_accuracy", "Correct reversal decisions", "%", True),
        ]
    )
    result = []
    for key, label, unit, higher in specifications:
        base, model = rows.get(baseline), rows.get("ridge")
        left = base["metrics"].get(key) if base else None
        right = model["metrics"].get(key) if model else None
        comparable = (
            left is not None
            and right is not None
            and base is not None
            and model is not None
            and base["n"] == model["n"]
        )
        outcome = "Not comparable"
        if comparable and left is not None and right is not None:
            outcome = (
                "Same observed result"
                if left == right
                else ("Observed better" if (right > left) == higher else "Observed worse")
            )

        def display(value: float | None, unit: str = unit) -> str:
            if value is None:
                return "Unavailable"
            return (
                f"{value * 100:.2f}%"
                if unit == "%"
                else f"{value:.4f}"
                if unit == "score"
                else f"{value:.2f}"
            )

        result.append(
            {
                "Measure": label,
                "Unit / direction": f"{unit} · {'higher' if higher else 'lower'} is better",
                METHOD_LABELS[baseline]: display(left),
                "Fixed ridge": display(right),
                "Compared with baseline": outcome,
            }
        )
    return result


def _results(run: SavedRun, all_runs: list[SavedRun], selected: int) -> None:
    controls = st.columns(2)
    task = controls[0].selectbox(
        "Prediction task",
        list(run.manifest["methods"]),
        format_func=lambda value: {
            "next_period": "Next-period AIP",
            "reversal": "Reversal after a rise",
        }[value],
    )
    splits = list(run.manifest["splits"])
    preferred = "final_evaluation" if "final_evaluation" in splits else "validation"
    split = controls[1].selectbox(
        "Frozen split",
        splits,
        index=splits.index(preferred) if preferred in splits else 0,
        format_func=lambda value: value.replace("_", " ").capitalize(),
    )
    dates = run.manifest["split_dates"][split]
    st.caption(f"Decision periods: {dates['start']} to {dates['end']}.")
    official = split in {"validation", "final_evaluation"}
    st.subheader("Official frozen comparison" if official else "Descriptive saved results")
    st.caption(
        "Saved aggregate metrics on the declared eligible cases."
        if official
        else "Training and diagnostic results are outside the official evaluation comparison."
    )
    selected_rows = _select(run.metrics["aggregate"], task, split)
    if not selected_rows:
        st.info("No saved observations for this selection. Missing outcomes are not zero errors.")
    else:
        st.table(pd.DataFrame(comparison_rows(run, task, split)))
        observed = next(
            (row["n"] for row in selected_rows if row["method"] == "ridge"), selected_rows[0]["n"]
        )
        coverage = [row for row in run.coverage["per_period"] if row["split"] == split]
        total_key = "eligible_candidates" if task == "next_period" else "observed_rise_candidates"
        candidates = sum(row[total_key] for row in coverage)
        st.caption(
            f"{observed:,} observed outcomes from {candidates:,} current candidate-periods; "
            f"{candidates - observed:,} outcomes unavailable. "
            "All methods use the same observed cases."
        )
    if task == "next_period":
        st.write(
            "Probability error is the multiclass Brier score for fall, unchanged or rise. "
            "Price error is the mean absolute difference from the next AIP, in DKK per package."
        )
        st.caption(
            "Top-10% precision counts actual rises among the highest ranked observed cases. "
            "Constant-probability baseline ties use package identifier order; this is neither "
            "random selection nor the market rise rate."
        )
    else:
        st.write(
            "Full reversal reaches or falls below the pre-rise AIP within two tariff periods "
            "after an already observed rise. Both future prices must be observed. "
            "Binary Brier measures probability error; accuracy uses the frozen decision threshold."
        )
    st.caption(
        "Uncertain: point estimates describe this saved sample. Separate 95% intervals are "
        "not paired tests of improvement; few tariff periods limit temporal evidence. "
        "Calibration is not established. No procurement savings or method promotion is implied."
    )
    st.subheader("Performance by tariff period — exploratory view")
    records = _select(run.metrics["per_period"], task, split)
    metrics = (
        ("mae_price_dkk", "brier_multiclass", "precision_top_decile_rise")
        if task == "next_period"
        else ("brier_binary", "reversal_accuracy")
    )
    names = [name for name in metrics if any(name in row["metrics"] for row in records)]
    if names:
        labels = {
            "mae_price_dkk": "Average absolute price error (DKK/package, lower better)",
            "brier_multiclass": "Direction probability error (Brier, lower better)",
            "precision_top_decile_rise": "Top-10% rise precision (fraction, higher better)",
            "brier_binary": "Reversal probability error (Brier, lower better)",
            "reversal_accuracy": "Reversal accuracy (fraction, higher better)",
        }
        metric = st.selectbox(
            "Metric in period chart", names, format_func=lambda value: labels[value]
        )
        baseline = "no_change" if task == "next_period" else "no_reversal"
        chart = [
            {
                "Tariff period": row["period_start"],
                "Method": METHOD_LABELS.get(row["method"], row["method"]),
                labels[metric]: row["metrics"].get(metric),
            }
            for row in records
            if row["method"] in {baseline, "ridge"}
        ]
        if chart:
            fig = px.line(
                pd.DataFrame(chart),
                x="Tariff period",
                y=labels[metric],
                color="Method",
                markers=True,
                color_discrete_sequence=["#0072B2", "#D55E00"],
            )
            fig.update_traces(connectgaps=False)
            fig.update_layout(legend_title_text="", height=350, margin=dict(t=15, b=20))
            st.plotly_chart(fig, width="stretch")
    st.caption(
        "Saved period metrics only. Changing this chart leaves the official comparison unchanged."
    )
    with st.expander("Optional diagnostics: all methods, uncertainty and calibration"):
        st.subheader("All saved aggregate metrics")
        _table(selected_rows)
        st.subheader("Uncertainty across tariff periods")
        st.caption(
            "95% intervals resample tariff periods, conditional on saved fitted predictions. "
            "Package rows are not independent temporal replications."
        )
        _table(_select(run.metrics["uncertainty"], task, split))
        st.subheader("Probability calibration")
        st.caption(
            "Mean forecast probability and observed frequency, by probability bin and class."
        )
        _table(_select(run.metrics["calibration"], task, split))
        methods = st.multiselect(
            "Methods in exploratory period view",
            run.manifest["methods"][task],
            default=run.manifest["methods"][task],
            format_func=lambda value: METHOD_LABELS.get(value, value),
        )
        _table([row for row in records if row["method"] in methods])
    if len(all_runs) < 2:
        return
    st.subheader("Comparison across development runs")
    st.caption(
        "Run comparisons use frozen aggregate metrics, separately from tariff-period charts. "
        "No training-window learning experiment is available."
    )
    others = [index for index in range(len(all_runs)) if index != selected]
    if not others:
        st.info("Supply another explicit saved run to compare development results.")
        return
    comparison = st.selectbox(
        "Compare with saved run",
        others,
        format_func=lambda index: _run_label(index, all_runs[index]),
    )
    other = all_runs[comparison]
    differences = incompatibilities(run, other)
    if differences:
        st.warning(
            "Incompatible runs: "
            + ", ".join(name.replace("_", " ") for name in differences)
            + ". Differences in cases or research definitions cannot establish model progress."
        )
    else:
        st.success(
            "Compatible frozen target, horizon, price basis, availability, splits and cases."
        )
        rows: list[dict[str, Any]] = []
        for index in (selected, comparison):
            rows.extend(
                {"saved_run": _run_label(index, all_runs[index]), **row}
                for row in _select(all_runs[index].metrics["aggregate"], task, split)
            )
        _table(rows)


def _run_label(index: int, run: SavedRun) -> str:
    return (
        f"Run {index + 1} · {run.manifest.get('created_at', 'date unavailable')} · "
        + str(run.manifest["run_fingerprint"])[:10]
    )


def _context(run: SavedRun) -> None:
    dataset = run.manifest["dataset"]
    st.subheader("Research basis")
    st.write("Price basis", dataset["price_context"])
    st.write("Availability", dataset["availability_mode"])
    st.write("Eligible population", dataset["population_definition"])
    st.write("Identity assumptions", dataset["identity_policy"])
    st.write(
        "Population scope",
        dataset["provenance"].get("population_scope", "See population definition"),
    )
    gaps = dataset["provenance"].get("external_coverage_gaps", [])
    if gaps:
        st.warning(
            f"{len(gaps)} known source-coverage exception(s) sit outside the snapshot panel. "
            "They remain unresolved; the panel does not establish complete market coverage."
        )
    st.write("Decision cutoff assumptions", run.manifest["cutoff_policy"])
    st.subheader("Frozen target and metric definitions")
    st.json(run.manifest["definitions"])
    st.subheader("Chronological split dates")
    _table([{"split": name, **dates} for name, dates in run.manifest["split_dates"].items()])
    st.write("Method version", run.manifest["method_version"])
    st.write("Admitted features", ", ".join(run.manifest.get("feature_names", [])))
    st.caption(
        "Input manifest, predictions, coefficients, coverage and metrics passed SHA-256 and "
        "size verification. Predictions and coefficients are not executed or loaded by this viewer."
    )
    with st.expander("Dataset provenance and reproducibility identifiers"):
        st.json(dataset)
        st.json(run.manifest.get("versions", {}))
    st.subheader("Holdout exposure and development lineage")
    st.write("Producing workflow: completed; evaluation locked; final holdout evaluated.")
    if run.manifest.get("development_parent"):
        st.write("The producer records an earlier development parent.")
    supplied = os.environ.get("BEF_E1_LINEAGE", "")
    if supplied:
        try:
            events = lineage_events(Path(supplied), run.manifest["run_fingerprint"])
            _table(
                [
                    {key: event.get(key) for key in ("event", "recorded_at", "sequence")}
                    for event in events
                ]
            )
            if not events:
                st.warning("The supplied exposure log has no event for this run.")
        except ResearchError:
            st.warning(
                "The supplied exposure log is missing or invalid; "
                "later development status is unknown."
            )
    else:
        st.info("No external exposure log supplied. Later use as development evidence is unknown.")
    st.caption(
        "This read-only display reports workflow records; it cannot enforce future tuning choices. "
        "Exposure records do not establish independent confirmation after subsequent tuning."
    )


def main(paths: list[Path] | None = None) -> None:
    st.set_page_config(page_title="Historical research results", layout="wide")
    st.title("Historical research results")
    paths = configured_paths() if paths is None else paths
    if not paths:
        st.info(
            "No saved runs selected. Launch with one or more explicit run.json paths, "
            "or set BEF_E1_RUNS to an OS-path-separated list."
        )
        return
    runs = []
    for index, path in enumerate(paths):
        try:
            runs.append(load_saved_run(path))
        except (ResearchError, OSError, KeyError, TypeError, AttributeError):
            st.error(
                f"Saved run {index + 1} is unavailable: missing, corrupt, incomplete, unlocked "
                "or unsupported artifacts. No results from this bundle are displayed."
            )
    if not runs:
        return
    selected = 0
    if len(runs) > 1:
        selected = st.selectbox(
            "Saved run", range(len(runs)), format_func=lambda index: _run_label(index, runs[index])
        )
    run = runs[selected]
    st.write(
        "E1 compares AIP forecasts for eligible historical LMS packages. "
        "Reconstructed research; historical publication and pharmacy possession are unknown."
    )
    comparisons = comparison_rows(run, "next_period", "final_evaluation")
    outcomes = [row["Compared with baseline"] for row in comparisons]
    if outcomes == ["Observed better", "Observed better", "Observed worse"]:
        st.write(
            "Mixed final results: fixed ridge improved probability scores and rise ranking against "
            "no change; price-amount forecasts were worse. No method was promoted."
        )
    else:
        st.caption(
            "The saved comparisons below show which measures improved, worsened "
            "or cannot be compared. No method promotion is implied."
        )
    st.caption(
        "Database expansion did not retrain this saved evaluation. "
        "Verified completed run; final evaluation locked."
    )
    price = run.manifest["dataset"]["price_context"]
    if all(
        price.get(key) == value
        for key, value in {
            "currency": "DKK",
            "price_basis": "per_package",
            "price_type": "aip",
            "tax_basis": "excluded",
            "fee_basis": "excluded",
        }.items()
    ):
        st.caption(
            "AIP in DKK per package, excluding VAT and fees. "
            "Forecast quality does not establish procurement savings."
        )
    results, coverage, context = st.tabs(["Results", "Coverage and outcomes", "Research context"])
    with results:
        _results(run, runs, selected)
    with coverage:
        st.subheader("Coverage and outcome attrition")
        st.caption(
            "All periods and current candidates, including exclusions and pending horizons. "
            "Missing and pending labels remain separate from observed outcomes."
        )
        _table([run.coverage["overall"]])
        _table(run.coverage["per_period"])
    with context:
        _context(run)
