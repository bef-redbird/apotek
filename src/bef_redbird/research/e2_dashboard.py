"""Separate E2 saved-results display, with no experiment execution controls."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from .dataset import ResearchError
from .e2_result_view import E2SavedRun, load_e2_run

TASK_LABELS = {
    "direction": "Price direction and rise ranking",
    "next_period": "Price direction and rise ranking",
    "magnitude": "Price-change magnitude",
    "reversal": "Reversal after an observed rise",
}
METRIC_LABELS = {
    "brier": "Probability error (Brier, lower better)",
    "accuracy": "Correct outcome class (fraction, higher better)",
    "rise_precision_top_decile": "Rises among top 10% (fraction, higher better)",
    "fall_fraction_top_decile": "Falls among top 10% (fraction, lower better)",
    "shortlist_unknown_fraction": "Unknown outcomes among top 10% (fraction)",
    "ece": "Calibration error (lower better)",
    "mae_dkk": "Absolute price error (DKK/package, lower better)",
    "mape": "Absolute percentage error (lower better)",
}
METHOD_LABELS = {
    "no_change": "No price change",
    "historical_frequency": "Historical probabilities",
    "repeat_last": "Repeat last price movement",
    "historical_median": "Historical median price change",
    "persistence": "No reversal",
    "catboost": "CatBoost",
    "logistic": "Simple regression",
    "catboost_own": "CatBoost — price history only",
    "catboost_package": "CatBoost — plus package details",
    "catboost_group": "CatBoost — with competitors",
    "catboost_group_mae": "CatBoost — price-change amount",
    "logistic_own": "Simple regression — price history only",
    "logistic_package": "Simple regression — plus package details",
    "logistic_group": "Simple regression — with competitors",
}
METRIC_GUIDES = {
    "brier": "How good are the outcome probabilities? Lower is better; zero is perfect.",
    "ece": "Does confidence match observed outcomes? Lower calibration error is better.",
    "mae_dkk": "How many kroner does the forecast miss by per package? Lower is better.",
    "accuracy": "How often was the most likely outcome correct? Higher is better.",
    "rise_precision_top_decile": (
        "How often did reviewed packages rise? Higher is better. "
        "The fraction uses observed outcomes in the original top 10%; 0.40 means 40%."
    ),
    "fall_fraction_top_decile": (
        "How often did reviewed packages fall instead? Lower is better. "
        "The fraction uses observed outcomes in the original top 10%; 0.10 means 10%."
    ),
    "shortlist_unknown_fraction": (
        "How much of the review list cannot be checked? Lower means fewer missing outcomes. "
        "The denominator is the entire original top 10%, including missing outcomes."
    ),
    "outcome_coverage": (
        "How much of the eligible population can be checked? Higher means more observed outcomes; "
        "it does not measure forecast quality."
    ),
}


def method_label(value: str) -> str:
    """Name a displayed method without changing its saved identity or filter value."""
    if ": " in value:
        prefix, method = value.split(": ", 1)
        return f"{prefix}: {method_label(method)}"
    return METHOD_LABELS.get(value, value.replace("_", " "))


def _task_guide(task: str) -> None:
    """Keep the decision question close to the corresponding saved scores."""
    if task == "magnitude":
        question = "How close is the forecast to the next AIP change in kroner?"
        tested = (
            "Forecast the next tariff period's AIP change per package, then compare it with "
            "the observed change. The no-change baseline forecasts DKK 0."
        )
        reading = (
            "MAE is the average absolute miss: DKK 2 means the forecast change missed by "
            "DKK 2 per package on average. Lower is better; inspect large errors too."
        )
        use = (
            "Helps assess the possible size of a price movement during candidate review. "
            "Price error is not savings; demand, stock and purchasing terms are still needed."
        )
    elif task == "reversal":
        question = "After a rise has already happened, does the price come back down?"
        tested = (
            "Start only at an already observed rise. Use both following tariff periods "
            "to distinguish full reversal, partial reversal and persistence."
        )
        reading = (
            "Lower Brier means better probabilities across those three outcomes. "
            "Calibration checks whether the stated probabilities match observed frequencies."
        )
        use = (
            "Helps review whether an elevated price may be temporary. It does not predict "
            "the initial rise in advance or establish a return/rebuy opportunity."
        )
    else:
        question = "Which packages are more likely to rise in the next tariff period?"
        tested = (
            "Forecast fall, unchanged and rise probabilities from earlier evidence, "
            "then check the next observed AIP and the original top 10% ranked for rises."
        )
        reading = (
            "Lower Brier means better probabilities overall. Higher rise share in the top "
            "10% means a more concentrated review list; also check falls and missing outcomes."
        )
        use = (
            "Helps prioritize packages for pharmacist review before a possible rise. "
            "A good ranking alone does not determine how much to buy or the financial benefit."
        )
    st.markdown(f"**{question}**")
    for column, title, text in zip(
        st.columns(3),
        ("What was tested", "How to read it", "What it helps with"),
        (tested, reading, use),
        strict=True,
    ):
        column.caption(f"**{title}**  \n{text}")
    st.caption(
        "This experiment used current-period ABC. Advance ABC for the next period was not tested."
    )


def _score_guide(task: str) -> None:
    with st.expander("Score and method guide"):
        st.write(
            "Observed counts include only cases with a usable later outcome. Outcome coverage "
            "is the observed share of all eligible cases for the selected task. Repeated "
            "observations of the same package in different periods are separate cases."
        )
        if task == "magnitude":
            st.write(
                "MAE ignores the sign of each forecast error. Relative amount error divides "
                "that error by current AIP; 0.02 means 2% of current AIP and can exceed 1. "
                "The 95th-error column averages each period's 95th percentile: unusually "
                "large misses, rather than the typical miss. Amounts use AIP DKK/package, "
                "excluding VAT and consumer dispensing fees."
            )
        else:
            st.write(
                "Brier measures squared differences between all three forecast probabilities "
                "and the observed outcome. Zero is perfect; lower is better. A score of 0.10 "
                "is not 10% accuracy, a 10% rise chance or a financial return. Calibration "
                "error measures the gap between forecast probabilities and observed frequencies; "
                "lower is better. The two measures can move in different directions."
            )
        if task == "reversal":
            st.write(
                "Use the lower of the two following prices: full reversal reaches or falls "
                "below the pre-rise price; partial reversal falls below the rise price but "
                "stays above the pre-rise price; persistence does neither. Both future "
                "observations must be available to score the outcome."
            )
        elif task != "magnitude":
            st.write(
                "Rise and fall top 10% use only observed outcomes within the frozen shortlist. "
                "Unknown top 10% uses every shortlisted case. For example, 0.40 rise share "
                "means 40% of observed shortlisted outcomes rose; missing selected outcomes "
                "are reported separately and are never replaced. The top 10% is selected "
                "within each period, not a 10% predicted probability threshold."
            )
        st.write(
            "Method names describe the comparison: own = the package's own price history; "
            "package = own history plus supported package attributes and current-period ABC; "
            "group = package inputs plus dated comparable-peer relationships. Comparable "
            "peers share the recorded package size and unit."
        )
        st.write(
            "In the legends, 'price history only', 'plus package details' and 'with competitors' "
            "name those three input sets. 'With competitors' adds information about comparable "
            "packages in the same recorded group; it does not use sales or market shares."
        )
        st.write(
            "Simple regression (logistic regression) learns a weighted relationship between "
            "inputs and outcome probabilities. CatBoost combines decision trees to learn "
            "more complex relationships; the _mae variant forecasts a median price change. "
            "Both are fitted "
            "locally on earlier observations. No Codex/OpenAI model makes these forecasts."
        )
        st.write(
            "Simple baselines: No price change forecasts an unchanged price; Repeat last price "
            "movement repeats the last observed direction; Historical probabilities uses earlier "
            "outcome shares; Historical median price change uses the earlier median change; "
            "No reversal predicts no "
            "reversal. These provide reference points for the learned methods."
        )


def _table(
    rows: list[dict[str, Any]],
    *,
    columns: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> None:
    if rows:
        frame = pd.json_normalize(rows, sep=" · ")
        for name in ("method", "candidate", "baseline"):
            if name in frame:
                frame[name] = frame[name].map(
                    lambda value: method_label(value) if isinstance(value, str) else value
                )
        if "history" in frame:
            frame["history"] = frame["history"].astype("string")
        if columns is not None:
            frame = frame[[name for name in columns if name in frame]]
        st.dataframe(
            frame,
            hide_index=True,
            width="stretch",
            column_config={
                column: (labels or {}).get(
                    column,
                    METRIC_LABELS.get(
                        column.removeprefix("metrics · "),
                        {"n": "Observed cases"}.get(column, column.replace("_", " ").capitalize()),
                    ),
                )
                for column in frame.columns
            },
        )
    else:
        st.info("No saved observations. Missing outcomes are not zero errors.")


def _selected(rows: list[dict[str, Any]], task: str, split: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("task") == task and row.get("split") == split]


def group_comparison_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Describe saved paired intervals; no overall winner or causal interpretation."""
    results = []
    for row in rows:
        candidate, baseline = row.get("candidate", ""), row.get("baseline", "")
        if (
            not isinstance(candidate, str)
            or not isinstance(baseline, str)
            or not candidate.endswith("_group")
            or baseline != candidate.removesuffix("group") + "package"
        ):
            continue
        metrics = row["metrics"]
        lower, upper = metrics.get("ci_lower"), metrics.get("ci_upper")
        outcome = "Unavailable paired interval"
        if lower is not None and upper is not None and lower <= upper:
            outcome = (
                "Helped on these observed periods"
                if upper < 0
                else "Hurt on these observed periods"
                if lower > 0
                else "Inconclusive across periods"
            )
        results.append(
            {
                "Algorithm": method_label(candidate.removesuffix("_group")),
                "Population": (
                    "Validated comparable peers (at least 1): observed outcomes"
                    if row.get("cohort") == "supported_with_at_least_one_peer"
                    else row.get("population", "All eligible candidates: observed outcomes")
                ),
                "Group contribution": outcome,
                "Probability error difference": metrics.get("brier_difference"),
                "95% lower": lower,
                "95% upper": upper,
                "Observed cases": row["n"],
                "Paired periods": row.get("periods"),
            }
        )
    return results


def summary_columns(task: str) -> list[str]:
    common = ["method", "n", "scored_periods"]
    metrics = (
        ["mae_dkk", "relative_to_current_aip_mae", "p95_absolute_error_dkk"]
        if task == "magnitude"
        else ["brier", "ece", "outcome_coverage"]
        if task == "reversal"
        else [
            "brier",
            "ece",
            "rise_precision_top_decile",
            "fall_fraction_top_decile",
            "shortlist_unknown_fraction",
        ]
    )
    return [*common, *("metrics · " + name for name in metrics)]


def capacity_summary(statuses: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate completed learned trials from attempted capacity and baseline input loads."""
    learned = [
        row for row in statuses if str(row.get("method", "")).startswith(("catboost", "logistic"))
    ]
    completed = [row for row in learned if row.get("status") == "completed"]

    def largest(rows: list[dict[str, Any]], field: str, *, resource: bool = False) -> Any:
        values = [
            (row.get("resource") or {}).get(field) if resource else row.get(field) for row in rows
        ]
        numbers: list[int | float] = []
        for value in values:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                numbers.append(value)
        return max(numbers, default=None)

    return {
        "Completed learned trials": len(completed),
        "Largest completed learned training rows": largest(completed, "training_rows"),
        "Largest attempted learned training rows": largest(learned, "training_rows"),
        "Highest completed learned trial RSS bytes": largest(
            completed, "sampled_peak_rss_bytes", resource=True
        ),
        "Longest completed learned trial seconds": largest(
            completed, "elapsed_seconds", resource=True
        ),
    }


def secondary_comparison_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Explain saved intervals only; favorable positive differences apply only to rise precision."""
    result = []
    labels = {
        "brier": "Probability error ↓",
        "mae_dkk": "Amount error DKK/package ↓",
        "rise_precision_top_decile": "Rise precision ↑",
        "fall_fraction_top_decile": "Falls selected ↓",
        "shortlist_unknown_fraction": "Unavailable selected ↓",
    }
    for row in rows:
        values = row["metrics"]
        low, high = values["ci_lower"], values["ci_upper"]
        outcome = "Unavailable paired interval"
        if low is not None and high is not None:
            outcome = "Inconclusive"
            if high < 0 or low > 0:
                better = low > 0 if row["metric"] == "rise_precision_top_decile" else high < 0
                outcome = "Observed better" if better else "Observed worse"
        result.append(
            {
                "Measure": labels[row["metric"]],
                "Comparison": (
                    f"{method_label(row.get('candidate', row.get('method', '')))} minus "
                    f"{method_label(row['baseline'])}"
                ),
                "Difference": values["difference"],
                "95% lower": low,
                "95% upper": high,
                "Paired result for this measure": outcome,
            }
        )
    return result


def comparison_issues(left: E2SavedRun, right: E2SavedRun, task: str, split: str) -> list[str]:
    """Counts never prove comparable cases; require the producing workflow's pinned proofs."""
    issues = []

    def base(run: E2SavedRun) -> Any:
        value = run.manifest["dataset"].get("base")
        return value.get("sha256") if isinstance(value, dict) else None

    a, b = base(left), base(right)
    if not isinstance(a, str) or not isinstance(b, str) or not re.fullmatch("[0-9a-f]{64}", a):
        issues.append("Base dataset proof unavailable")
    elif a != b:
        issues.append("Base datasets differ")
    if left.manifest["split_dates"] != right.manifest["split_dates"]:
        issues.append("Split dates differ")
    for name in ("targets", "classes", "weighting", "ranking", "cutoff"):
        value = left.artifacts["protocol"].get(name)
        other = right.artifacts["protocol"].get(name)
        if not value or not other or value != other:
            issues.append(f"{name.capitalize()} definitions differ or are unavailable")
    cohorts = []
    proof_fields = (
        "candidate_keys_sha256",
        "observed_outcome_keys_sha256",
        "outcome_values_sha256",
    )
    for run in (left, right):
        binding = run.artifacts["coverage"].get("comparison_binding", {})
        binding = binding if isinstance(binding, dict) else {}
        cohort = binding.get("splits", {})
        for key in (split, task):
            cohort = cohort.get(key, {}) if isinstance(cohort, dict) else {}
        cohort = cohort if isinstance(cohort, dict) else {}
        if binding.get("version") != "e2-comparison-binding-v1" or any(
            not isinstance(cohort.get(field), str)
            or not re.fullmatch("[0-9a-f]{64}", cohort[field])
            for field in proof_fields
        ):
            issues.append("Pinned candidate/outcome proofs unavailable for this task and split")
        cohorts.append({key: cohort.get(key) for key in proof_fields})
        if any(
            row.get("status") != "completed"
            and row.get("task") == task
            and (
                row.get("split") == split
                or (
                    split == "evaluation_all"
                    and str(row.get("split", "")).startswith("evaluation_")
                )
            )
            for row in run.artifacts["coverage"].get("trial_statuses", [])
        ):
            issues.append("Selected evaluation has failed trials; full method coverage is unproved")
    if not issues and cohorts[0] != cohorts[1]:
        issues.append("Candidate keys, observed outcome keys or outcome values differ")
    return list(dict.fromkeys(issues))


def _cross_run(run: E2SavedRun, task: str, split: str) -> None:
    with st.expander("Compare another saved E2 run"):
        path = st.text_input(
            "Optional second E2 result manifest", placeholder="Explicit local JSON path"
        )
        if not path.strip():
            st.info(
                "Only one E2 run selected. E1 has different scope and is not a comparable E2 run."
            )
            return
        try:
            other = load_e2_run(Path(path.strip()).expanduser())
        except (ResearchError, OSError, KeyError, TypeError, AttributeError):
            st.warning(
                "Second result unavailable or unsupported. E1 is a separate evaluation schema."
            )
            return
        issues = comparison_issues(run, other, task, split)
        if issues:
            st.warning("Comparison refused: " + "; ".join(issues) + ".")
            return
        st.caption(
            "Compatible pinned target, cases and outcome values. These are saved scores only. "
            "Separate uncertainty intervals do not establish superiority or development progress."
        )
        rows = [
            {"saved_run": label, **row}
            for label, selected in (("First", run), ("Second", other))
            for row in _selected(selected.artifacts["metrics"]["aggregate"], task, split)
        ]
        _table(rows, columns=["saved_run", *summary_columns(task)])
        controls = st.columns(2)
        records: list[dict[str, Any]] = []
        for index, (label, selected) in enumerate((("First", run), ("Second", other))):
            methods = selected.manifest["methods"].get(task, [])
            if not methods:
                return
            method = controls[index].selectbox(
                f"{label} run method",
                methods,
                index=methods.index("catboost_group") if "catboost_group" in methods else 0,
                format_func=method_label,
            )
            records.extend(
                {**row, "method": f"{label}: {method}"}
                for row in _selected(selected.artifacts["metrics"]["per_period"], task, split)
                if row["method"] == method
            )
        _chart(records, "period_start", "Saved metric across runs", "e2_cross_metric")


def _outcome_detail(detail: dict[str, Any]) -> None:
    rows = detail.get("rows", [])
    if not rows:
        return
    st.subheader("Saved next-observation availability")
    st.caption(
        "Source availability for the original candidates and frozen shortlist. "
        "These are saved coverage diagnostics, separate from reversal prediction scores. "
        "Exploratory filters never rerank candidates or replace unavailable selections."
    )
    methods = list(dict.fromkeys(row["method"] for row in rows))
    method = st.selectbox(
        "Coverage method",
        methods,
        index=methods.index("catboost_group") if "catboost_group" in methods else 0,
        format_func=method_label,
    )
    selected = [row for row in rows if row["method"] == method]
    with st.expander("Exploratory saved coverage filters"):
        filters = st.columns(3)
        for index, (field, default) in enumerate(
            (
                ("regime", "all2024_2026"),
                ("cohort", "all"),
                ("condition", "all_origins"),
            )
        ):
            options = list(dict.fromkeys(row[field] for row in rows if row["method"] == method))
            value = filters[index].selectbox(
                "Coverage " + field,
                options,
                index=options.index(default) if default in options else 0,
                format_func=lambda x: x.replace("_", " "),
            )
            selected = [row for row in selected if row[field] == value]
    summary, reasons = [], []
    for row in selected:
        areas = [
            ("All current candidates", row["all_candidates"]),
            ("Original frozen shortlist", row["original_shortlist"]),
        ]
        for label, value in areas:
            summary.append(
                {
                    "Population": label,
                    "Cases": value.get("rows"),
                    "Observed": value.get("coverage_counts", {}).get("observed"),
                    "Unavailable fraction": value.get("unknown_fraction"),
                }
            )
        states = sorted({key for _, value in areas for key in value.get("coverage_counts", {})})
        for state in states:
            reasons.append(
                {
                    "Retained source reason": state.replace("_", " "),
                    **{
                        label: value.get("coverage_counts", {}).get(state) for label, value in areas
                    },
                }
            )
    _table(summary)
    _table(reasons)
    with st.expander("All saved source-outcome details and limitations"):
        _table(rows)
        st.json(detail.get("limitations", []))
        st.json(detail.get("source", {}))


def _calibration(rows: list[dict[str, Any]], task: str, protocol: dict[str, Any]) -> None:
    if not rows:
        st.info("No saved calibration bins for this selection.")
        return
    controls = st.columns(2)
    methods = list(dict.fromkeys(row["method"] for row in rows))
    method = controls[0].selectbox(
        "Calibration method",
        methods,
        index=methods.index("catboost_group") if "catboost_group" in methods else 0,
        format_func=method_label,
    )
    selected = [row for row in rows if row["method"] == method]
    classes = list(dict.fromkeys(row.get("class", row.get("class_label", 0)) for row in selected))
    class_names = protocol.get("classes", {}).get(task, [])

    def label(value: Any) -> str:
        try:
            return str(class_names[int(value)]).replace("_", " ").capitalize()
        except (IndexError, ValueError, TypeError):
            return f"Class {value}"

    preferred = 0 if task == "reversal" else 2
    outcome = controls[1].selectbox(
        "Calibration outcome",
        classes,
        index=classes.index(preferred) if preferred in classes else 0,
        format_func=label,
    )
    points = [
        {
            "Forecast probability": row.get("predicted", row.get("mean_probability")),
            "Observed frequency": row.get("observed", row.get("observed_frequency")),
            "Observed cases": row.get("n"),
        }
        for row in selected
        if row.get("class", row.get("class_label", 0)) == outcome
    ]
    fig = px.scatter(
        pd.DataFrame(points),
        x="Forecast probability",
        y="Observed frequency",
        hover_data=["Observed cases"],
    )
    fig.add_scatter(
        x=[0, 1],
        y=[0, 1],
        mode="lines",
        name="Ideal calibration",
        line=dict(color="#777777", dash="dash"),
    )
    fig.update_xaxes(range=[0, 1], tickformat=".0%")
    fig.update_yaxes(range=[0, 1], tickformat=".0%")
    fig.update_layout(height=320, margin=dict(t=10, b=20))
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Events forecast at 30% should happen about 30% of the time among observed outcomes. "
        "Above the diagonal, the selected outcome happened more often than forecast; below "
        "it, less often. This checks confidence, rather than which packages rank highest. "
        "These saved bins pool observed package rows within the selected split; they do not give "
        "each period equal weight. No recalibration or new fit is performed. "
        "Sparse bins are uncertain."
    )
    with st.expander("All saved calibration bins"):
        _table(rows)


def _chart(rows: list[dict[str, Any]], x: str, label: str, key: str) -> None:
    names = sorted({name for row in rows for name in row.get("metrics", {})})
    if not names:
        st.info("No executed experiment results available for this selection.")
        return
    preferred = "brier" if "brier" in names else "mae_dkk" if "mae_dkk" in names else names[0]
    metric = st.selectbox(
        label,
        names,
        index=names.index(preferred),
        key=key,
        format_func=lambda value: METRIC_LABELS.get(value, value),
    )
    if metric in METRIC_GUIDES:
        st.caption(METRIC_GUIDES[metric])
    chart = [
        {x: row[x], "Method": method_label(row["method"]), metric: row["metrics"].get(metric)}
        for row in rows
    ]
    fig = px.line(pd.DataFrame(chart), x=x, y=metric, color="Method", markers=True)
    fig.update_xaxes(
        title_text={
            "period_start": "Tariff period start",
            "history_years": "Training history (years)",
        }.get(x, x)
    )
    fig.update_yaxes(title_text=METRIC_LABELS.get(metric, metric))
    fig.update_traces(connectgaps=False)
    fig.update_layout(height=350, margin=dict(t=10, b=20), legend_title_text="")
    st.plotly_chart(fig, width="stretch")


def _results(run: E2SavedRun) -> None:
    manifest, artifacts = run.manifest, run.artifacts
    controls = st.columns(2)
    task = controls[0].selectbox(
        "Prediction task",
        list(manifest["methods"]),
        format_func=lambda value: TASK_LABELS.get(value, value.replace("_", " ")),
    )
    splits = list(manifest["split_dates"])
    split = controls[1].selectbox(
        "Frozen split",
        splits,
        index=splits.index("evaluation_all") if "evaluation_all" in splits else 0,
    )
    dates = manifest["split_dates"][split]
    st.caption(
        f"Decision-origin window: {dates['start']} (inclusive) to {dates['end']} (exclusive). "
        "Charts use the actual tariff start dates inside this window."
    )
    _task_guide(task)
    st.subheader("Frozen baseline and candidate results")
    st.caption(
        f"Declared baseline: {method_label(manifest['baseline_by_task'][task])}. "
        "Scores describe the saved observed cases; compare denominators before comparing methods. "
        "Arrows show the favorable score direction."
    )
    metrics = artifacts["metrics"]
    aggregate = _selected(metrics["aggregate"], task, split)
    _table(
        aggregate,
        columns=summary_columns(task),
        labels={
            "method": "Method",
            "n": "Observed",
            "scored_periods": "Scored periods",
            "metrics · brier": "Brier ↓",
            "metrics · ece": "Calibration error ↓",
            "metrics · rise_precision_top_decile": "Rise top 10% ↑",
            "metrics · fall_fraction_top_decile": "Fall top 10% ↓",
            "metrics · shortlist_unknown_fraction": "Unknown top 10%",
            "metrics · outcome_coverage": "Outcome coverage",
            "metrics · mae_dkk": "MAE DKK/package ↓",
            "metrics · relative_to_current_aip_mae": "Relative amount error ↓",
            "metrics · p95_absolute_error_dkk": "Mean period 95th error DKK ↓",
        },
    )
    st.caption(
        "Rates and errors average the available saved tariff-period values equally. Counts are "
        "totals of observed package-period cases, not unique packages. Rise, fall, unknown and "
        "coverage are fractions: 0.10 means 10%. Unavailable values are not zero; pending "
        "outcomes remain in Coverage and outcomes. Package-weighted scores are in optional details."
    )
    _score_guide(task)
    st.subheader("Performance by tariff period — exploratory view")
    _chart(
        _selected(metrics["per_period"], task, split),
        "period_start",
        "Metric in period chart",
        "e2_period_metric",
    )
    st.caption("Chart selections do not change the frozen aggregate comparison.")
    if task != "magnitude":
        st.subheader("Probability calibration")
        _calibration(_selected(metrics["calibration"], task, split), task, artifacts["protocol"])
    st.subheader("Contribution of group features")
    st.caption(
        "Do peer relationships add useful information? Compare the same algorithm with group "
        "features against package attributes and own history alone. "
        "Negative paired probability-error differences favor group features; intervals crossing "
        "zero are inconclusive. These are exploratory temporal comparisons, not causal effects. "
        "Comparable peers share the recorded size/unit. Zero comparable peers and unknown group "
        "coverage are different states; effects can differ across these populations."
    )
    comparisons = _selected(artifacts["comparisons"]["incremental"], task, split)
    group_rows = group_comparison_rows(comparisons)
    if group_rows:
        st.table(pd.DataFrame(group_rows))
    else:
        st.info("No saved paired group-feature comparison for this task and split.")
    with st.expander("All saved method comparisons and interval sensitivity"):
        _table(comparisons)
    secondary = _selected(artifacts["comparisons"].get("secondary", []), task, split)
    if secondary:
        st.subheader("Other saved paired comparisons")
        st.caption(
            "Candidate-minus-baseline differences average matched tariff periods. Positive favors "
            "the candidate only for rise precision; lower error, falls and unavailable selections "
            "are better. A fraction difference of 0.01 means one percentage point. Ranking methods "
            "retain their original shortlists, whose observed denominators may differ. "
            "These are exploratory comparisons, not an overall model verdict."
        )
        st.table(pd.DataFrame(secondary_comparison_rows(secondary)))
        with st.expander("Saved secondary definitions and interval sensitivity"):
            _table(secondary)
    st.subheader("Actually executed training-history experiments")
    st.caption(
        "Does fitting on more past years help? Each point is an actual fit with a different "
        "training-history length; the feature lookback is unchanged. This is different from "
        "the period chart, which shows performance as the market moves through time. "
        "Validation history selection is separate from later evaluation results; "
        "it does not establish an overall model win. A line between points does not represent "
        "additional fitted models."
    )
    curves = [row for row in artifacts["history_curves"]["experiments"] if row["task"] == task]
    curve_splits = list(dict.fromkeys(row["split"] for row in curves))
    if len(curve_splits) > 1:
        curve_split = st.selectbox("Saved split for history experiments", curve_splits)
        curves = [row for row in curves if row["split"] == curve_split]
    elif curve_splits:
        st.caption(f"History experiments evaluated on saved split: {curve_splits[0]}.")
    _chart(curves, "history_years", "Metric in history chart", "e2_history_metric")
    _table(
        curves,
        columns=[
            "history",
            "history_years",
            "training_rows",
            "n",
            "metrics · brier",
            "metrics · mae_dkk",
            "runtime_seconds",
        ],
        labels={
            "history": "History choice",
            "history_years": "Training years",
            "training_rows": "Training rows",
            "n": "Observed",
            "metrics · brier": "Brier ↓",
            "metrics · mae_dkk": "MAE DKK/package ↓",
            "runtime_seconds": "Runtime seconds",
        },
    )
    with st.expander("All saved history experiment metrics"):
        _table(curves)
    with st.expander("Optional diagnostics and runtime details"):
        st.subheader("All saved aggregate metrics")
        _table(aggregate)
        _table(_selected(metrics["uncertainty"], task, split))
        st.caption(
            "Thread counts in trial records describe fitting settings. They are not a "
            "whole-process or inference thread cap; prediction and preprocessing use "
            "their separate library settings."
        )
        statuses = artifacts["coverage"].get("trial_statuses", [])
        st.subheader("Completed and attempted learned capacity")
        _table([capacity_summary(statuses)])
        st.caption(
            "Completed capacity uses only completed CatBoost/logistic trials. Baseline input loads "
            "are excluded. Sampled RSS covers the whole process, including shared cache, "
            "preparation, "
            "fitting and prediction; it is not isolated fitting memory or an enforced process cap. "
            "A failed larger attempt does not establish completed full-history fitting capacity."
        )
        failed_capacity = [
            {
                "Trial": row.get("trial_id"),
                "Training rows attempted": row.get("training_rows"),
                "Failure": row.get("error"),
                "Elapsed seconds": (row.get("resource") or {}).get("elapsed_seconds"),
                "Sampled process RSS bytes": (row.get("resource") or {}).get(
                    "sampled_peak_rss_bytes"
                ),
            }
            for row in statuses
            if row.get("status") == "failed"
            and str(row.get("method", "")).startswith(("catboost", "logistic"))
        ]
        if failed_capacity:
            st.write("Failed learned attempts and their observed resource limits")
            _table(failed_capacity)
        with st.expander("Raw saved resource records"):
            st.json(manifest.get("runtime", manifest.get("resources", {})))
    _cross_run(run, task, split)


def main(path: Path | None = None) -> None:
    st.set_page_config(page_title="E2 historical research results", layout="wide")
    st.title("E2 historical research results")
    if path is None:
        st.info("No E2 saved result selected. Supply an explicit pinned result manifest.")
        return
    try:
        run = load_e2_run(path)
    except (ResearchError, OSError, KeyError, TypeError, AttributeError):
        st.error(
            "E2 saved result unavailable: missing, corrupt, incomplete, unlocked or unsupported. "
            "No results from this bundle are displayed."
        )
        return
    st.write(
        "Retrospective reconstructed research. Historical publication and pharmacy possession "
        "are unestablished. Results do not establish Wednesday decision readiness, procurement "
        "savings or realised profit. No method is promoted."
    )
    st.write("Exposure and development status", run.manifest["exposure_status"])
    failures = [
        row
        for row in run.artifacts["coverage"].get("trial_statuses", [])
        if row.get("status") != "completed"
    ]
    if failures:
        evaluations = [
            row
            for row in run.artifacts["coverage"].get("trial_statuses", [])
            if str(row.get("split", "")).startswith("evaluation_")
        ]
        if (
            all(row.get("split") == "validation" for row in failures)
            and evaluations
            and all(row.get("status") == "completed" for row in evaluations)
        ):
            st.warning(
                f"{len(failures)} history-screen trial(s) failed; all {len(evaluations)} declared "
                "evaluation trials completed. A failed longer-history attempt is not a completed "
                "full-history fit or score."
            )
        else:
            st.warning(
                f"{len(failures)} declared trial(s) failed. "
                "Saved scores cover completed trials only. "
                "Missing methods or evaluation windows cannot establish model progress. "
                "A failed longer-history trial does not establish longer-history quality."
            )
        _table(
            [
                {
                    **row,
                    "error": "Stopped at declared resource threshold"
                    if row.get("error") == "e2_declared_fit_resource_limit"
                    else row.get("error"),
                }
                for row in failures
            ],
            columns=["trial_id", "history", "training_rows", "error"],
            labels={
                "trial_id": "Failed trial",
                "history": "Training history",
                "training_rows": "Training rows attempted",
                "error": "Stop reason",
            },
        )
    results, coverage, context = st.tabs(["Results", "Coverage and outcomes", "Research context"])
    with results:
        _results(run)
    with coverage:
        st.subheader("Coverage, unavailable outcomes and exits")
        st.caption(
            "Current eligible populations and observed labels are separate. Pending, exit, "
            "identity and coverage-unknown states are not unchanged prices."
        )
        st.caption(
            "A retained recorded_exit exclusion reason is not proof of physical market exit. "
            "An absent next observation does not establish deactivation or supplier disruption."
        )
        detail = run.artifacts["coverage"].get("outcome_detail", {})
        if isinstance(detail, dict) and detail.get("rows"):
            _outcome_detail(detail)
            with st.expander("Original saved period coverage"):
                _table([run.artifacts["coverage"]["overall"]])
                _table(run.artifacts["coverage"]["per_period"])
        else:
            _table([run.artifacts["coverage"]["overall"]])
            _table(run.artifacts["coverage"]["per_period"])
        with st.expander("All declared trial statuses"):
            _table(run.artifacts["coverage"].get("trial_statuses", []))
    with context:
        st.subheader("Frozen research protocol and source basis")
        st.json(run.artifacts["protocol"])
        st.json(run.manifest["dataset"])
        st.json(run.manifest["definitions"])
        st.caption(
            "All result JSON files passed SHA-256 and size verification. This viewer reads only "
            "the selected saved summaries; it does not connect to the foundation or load models."
        )
