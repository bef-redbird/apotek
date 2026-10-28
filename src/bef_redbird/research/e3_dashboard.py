"""Read-only, explicitly indexed historical E3 research results."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from .dataset import ResearchError
from .e3_result_view import (
    E3SavedRun,
    load_candidate_review,
    load_e3_index,
    load_indexed_e3_run,
)

LABELS = {
    "method": "Method",
    "information": "Information available",
    "peer_set": "Peer evidence",
    "candidates": "All candidates",
    "observed": "Observed outcomes",
    "periods": "Eligible periods",
    "scored_periods": "Scored periods",
    "brier": "Probability error (Brier; lower better)",
    "ece": "Calibration error (lower better)",
    "mae": "Mean absolute error (DKK/package)",
    "mae_dkk": "Mean absolute error (DKK/package)",
    "bias_dkk": "Mean signed error (DKK/package)",
    "p95_dkk": "Period 95th percentile absolute error (DKK/package)",
    "p99_dkk": "Period 99th percentile absolute error (DKK/package)",
    "max_error_dkk": "Period largest absolute error (DKK/package)",
    "mean_period_p95_dkk": "Mean of period 95th percentile errors (DKK/package)",
    "mean_period_p99_dkk": "Mean of period 99th percentile errors (DKK/package)",
    "mean_period_max_error_dkk": "Mean of period largest errors (DKK/package)",
    "mean_period_top1_error_share": "Mean of period worst-1% error shares",
    "pooled_p95_dkk": "Pooled 95th percentile absolute error (DKK/package)",
    "pooled_p99_dkk": "Pooled 99th percentile absolute error (DKK/package)",
    "pooled_max_error_dkk": "Largest error across all observed cases (DKK/package)",
    "pooled_top1_error_share": "Pooled error contributed by worst 1% of cases",
    "p95": "95th percentile absolute error (DKK/package)",
    "p99": "99th percentile absolute error (DKK/package)",
    "coverage90": "Observed coverage of 90% interval",
    "width90": "Mean 90% interval width (DKK/package)",
    "width90_dkk": "Mean 90% interval width (DKK/package)",
    "top1_error_share": "Period error contributed by worst 1%",
    "pooled_coverage90": "Pooled observed coverage of 90% interval",
    "pooled_width90_dkk": "Pooled mean 90% interval width (DKK/package)",
    "mean_difference": "Candidate minus baseline",
    "probability_error_reduction": "Brier reduction versus named baseline (higher better)",
    "lower": "Lower paired interval",
    "upper": "Upper paired interval",
    "selected": "Reviewed cases",
    "selection_rate": "Share of candidates reviewed",
    "rise_rate": "Rises among observed reviews",
    "fall_rate": "Falls among observed reviews",
    "unknown_fraction": "Unknown outcomes among reviews",
    "workload_mode": "Review workload",
    "empty_periods": "Periods with no selections",
    "peak_rss_bytes": "Sampled peak process memory (bytes)",
    "runtime_seconds": "Elapsed time (seconds)",
    "next_aip_dkk": "Predicted next AIP (DKK/package)",
    "delta_dkk": "Predicted AIP change (DKK/package)",
    "next_aip_ore": "Actual next AIP (øre/package)",
    "next_direction": "Actual next direction",
    "next_status": "Actual outcome availability",
    "p_increase": "Predicted rise probability",
    "p_unchanged": "Predicted unchanged probability",
    "p_decrease": "Predicted fall probability",
    "lower90_dkk": "Predicted 90% lower AIP (DKK/package)",
    "upper90_dkk": "Predicted 90% upper AIP (DKK/package)",
    "parameter_value": "Fixed policy parameter",
    "curve_family": "Policy curve",
    "outcome_coverage": "Observed share of all candidates",
}
SCENARIO_LABELS = {
    "perfect": "Final-code proxy",
    "missing10": "10% codes missing",
    "missing25": "25% codes missing",
    "wrong10": "10% codes wrong",
    "wrong25": "25% codes wrong",
    "group_wrong20": "20% groups wrong",
    "missing100": "All advance codes missing",
}


def method_label(value: str) -> str:
    """Readable names for saved identities; never change the identities or source rows."""
    special = {
        "abc_transition_rule": "ABC transition rule (hypothesis)",
        "e2": "E2 original ranking",
        "raw": "Raw advance-ABC ranking",
        "calibrated": "Calibrated advance-ABC ranking",
    }
    if value in special:
        return special[value]
    if value.startswith("e2_direction_") or value.startswith("e2_magnitude_"):
        baseline = value.removeprefix("e2_direction_").removeprefix("e2_magnitude_")
        return "E2 · " + {
            "no_change": "No price change",
            "historical_frequency": "Historical probabilities",
            "repeat_last": "Repeat last movement",
            "historical_median": "Historical price change",
            "catboost_group": "CatBoost with comparable peers",
            "catboost_group_mae": "CatBoost price change with comparable peers",
        }.get(baseline, baseline.replace("_", " "))
    calibrated = value.endswith("_calibrated")
    stem = value.removesuffix("_calibrated")
    info = "advance ABC proxy" if stem.endswith("_advance") else "current ABC"
    core = stem.removesuffix("_advance").removesuffix("_current")
    if core.startswith("amount_"):
        formulation = {"delta_floor": "nonnegative next price", "log_ratio": "log price ratio"}.get(
            core.removeprefix("amount_"), core.removeprefix("amount_").replace("_", " ")
        )
        return f"Price model · {formulation} · {info}"
    peer = next((key for key in ("package", "equal", "broad") if core.endswith("_" + key)), None)
    if peer is None:
        return value.replace("_", " ")
    if core.startswith("fallback_"):
        family = (
            "Comparable peers → package fallback"
            if peer == "equal"
            else "Broader → comparable → package fallback"
        )
    else:
        algorithm = "Simple regression" if core.startswith("logistic_") else "CatBoost"
        peers = {
            "package": "package details",
            "equal": "comparable peers",
            "broad": "broader peers",
        }
        family = f"{algorithm} · {peers[peer]}"
    return f"{family} · {info}" + (" · calibrated" if calibrated else "")


def policy_label(value: str) -> str:
    labels = {
        "e2_top10": "E2 original top 10%",
        "raw_top10": "Raw advance top 10%",
        "calibrated_top10": "Calibrated advance top 10%",
        "risk_rank": "Validated risk ranking",
        "threshold": "Validated probability thresholds",
    }
    if "_matched_" in value:
        policy, reference = value.split("_matched_", 1)
        return f"{method_label(reference)} at {policy_label(policy)} workload"
    return labels.get(value, value.replace("_", " "))


def information_label(value: str) -> str:
    if "advance" in value.lower() or "proxy" in value.lower():
        return "Advance ABC (historical proxy)"
    if value in {"current", "current_abc", "current_only", "current_abc_only"}:
        return "Current ABC only"
    return value.replace("_", " ")


def _names(run: E3SavedRun) -> dict[str, str]:
    return {
        row["method"]: (
            method_label(row["method"])
            if row["method"].startswith(("cb_", "logistic_", "fallback_", "amount_", "e2_"))
            or row["method"] == "abc_transition_rule"
            else row.get("label", method_label(row["method"]))
        )
        for row in run.artifacts["summary"]["rows"]
        if isinstance(row.get("method"), str)
    }


def _frame(rows: list[dict[str, Any]], names: dict[str, str]) -> pd.DataFrame:
    display_rows = []
    for row in rows:
        displayed = row.copy()
        if row.get("split") in {"all", "evaluation/all"} or str(row.get("split", "")).startswith(
            "evaluation_"
        ):
            for field in ("p95_dkk", "p99_dkk", "max_error_dkk", "top1_error_share"):
                if field in displayed:
                    displayed["mean_period_" + field] = displayed.pop(field)
        display_rows.append(displayed)
    frame = pd.json_normalize(display_rows, sep=" · ")
    for key in ("method", "baseline", "candidate"):
        if key in frame:
            frame[key] = frame[key].map(
                lambda value: (
                    names.get(value, method_label(value)) if isinstance(value, str) else value
                )
            )
    if "information" in frame:
        frame["information"] = frame["information"].map(information_label)
    if "policy" in frame:
        frame["policy"] = frame["policy"].map(policy_label)
    if "parameter" in frame:
        frame["parameter"] = frame["parameter"].map(
            lambda value: json.dumps(value) if isinstance(value, (list, tuple)) else str(value)
        )
    if "scenario" in frame:
        frame["scenario"] = frame["scenario"].map(
            lambda value: (
                SCENARIO_LABELS.get(value, value.replace("_", " "))
                if isinstance(value, str)
                else value
            )
        )
    return frame


def _table(frame: pd.DataFrame, *, labels: dict[str, str] | None = None) -> None:
    if frame.empty:
        st.info("No saved observations for this view. Missing outcomes are not zero errors.")
    else:
        st.dataframe(
            frame, hide_index=True, width="stretch", column_config={**LABELS, **(labels or {})}
        )


def _chart(
    frame: pd.DataFrame,
    x: str,
    y: str,
    *,
    color: str,
    title: str,
    reference: float | None = None,
    show_legend: bool = True,
) -> None:
    if frame.empty or not {x, y}.issubset(frame.columns):
        st.info("No saved curve for this view.")
        return
    figure = px.line(
        frame,
        x=x,
        y=y,
        color=color if color in frame else None,
        markers=True,
        labels=LABELS,
        title=title,
    )
    if reference is not None:
        figure.add_hline(y=reference, line_dash="dash", annotation_text="Nominal 90% coverage")
    if y == "selection_rate":
        figure.update_yaxes(range=[0, 1])
    figure.update_layout(showlegend=show_legend)
    st.plotly_chart(figure, width="stretch")


def _limits(run: E3SavedRun, role: str) -> None:
    if run.artifacts[role]["limitations"]:
        with st.expander("Saved limitations · " + role.replace("_", " ")):
            for item in run.artifacts[role]["limitations"]:
                st.caption(item)


def _aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep original aggregate rows; annual and subgroup views never replace them."""
    return [row for row in rows if row.get("split") in {"evaluation_all", "evaluation/all", "all"}]


def primary_comparisons(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("scenario", "perfect") == "perfect"
        and row.get("subgroup", "all") == "all"
        and row.get("block_length", 4) == 4
        and row.get("split", "evaluation_all") == "evaluation_all"
    ]


def _forecasts(run: E3SavedRun, names: dict[str, str]) -> None:
    st.subheader("Next-period price forecasts")
    st.write(
        "Use previous/current prices and ABC to forecast the next AIP value, its change and "
        "direction. The advance scenario also admits next-period ABC; ABC two or more periods "
        "ahead and unpublished next prices are excluded."
    )
    st.caption(
        "Current ABC only and advance ABC are different information sets. Historical final "
        "next-period ABC is a conditional proxy: it does not prove early availability or "
        "preliminary-code accuracy. Gains can reflect extra information "
        "rather than a better algorithm."
    )
    rows = _aggregate(run.artifacts["summary"]["rows"])
    tasks = list(dict.fromkeys(row["task"] for row in rows if isinstance(row.get("task"), str)))
    task = (
        st.selectbox(
            "Forecast task", tasks, index=tasks.index("direction") if "direction" in tasks else 0
        )
        if tasks
        else None
    )
    frame = _frame([row for row in rows if task is None or row.get("task") == task], names)
    columns = ["method", "information", "candidates", "observed"]
    columns += (
        ["mae_dkk", "mae", "bias_dkk", "pooled_p95_dkk"]
        if task == "magnitude"
        else ["brier", "ece"]
    )
    _table(
        frame[[key for key in columns if key in frame]],
        labels={
            "candidates": "Candidates",
            "observed": "Observed",
            "brier": "Brier ↓",
            "ece": "Calibration error ↓",
            "mae_dkk": "MAE (DKK) ↓",
            "mae": "MAE (DKK) ↓",
            "bias_dkk": "Bias (DKK)",
            "pooled_p95_dkk": "Pooled 95th error (DKK)",
        },
    )
    st.caption(
        "Brier measures squared probability error across fall/unchanged/rise; zero is perfect. "
        "It is not accuracy or profit. MAE is the average absolute price miss in DKK/package; "
        "lower is better. AIP excludes VAT and consumer dispensing fees."
    )
    st.caption(
        "Primary Brier, MAE, calibration error and bias give each tariff period equal weight. "
        "Pooled diagnostics give each observed package-period case equal weight."
    )
    _limits(run, "summary")
    st.subheader("Reconciled E2 / E3 and information comparisons")
    st.caption(
        "Saved paired comparisons use reconciled target, tariff periods, candidate keys and "
        "outcome values. Negative candidate-minus-baseline error means lower error. An interval "
        "crossing zero is inconclusive; ranking measures can have a different preferred direction. "
        "The E2 reference used current ABC only. No overall winner is implied."
    )
    comparison_rows = primary_comparisons(run.artifacts["comparisons"]["rows"])
    metrics = {"mae_dkk"} if task == "magnitude" else {"brier"}
    comparison_frame = _frame(
        [row for row in comparison_rows if row.get("metric") in metrics], names
    )
    columns = ["candidate", "baseline", "mean_difference", "lower", "upper", "periods"]
    _table(
        comparison_frame[[key for key in columns if key in comparison_frame]],
        labels={
            "candidate": "Candidate",
            "baseline": "Reference",
            "mean_difference": "Difference",
            "lower": "Lower 95%",
            "upper": "Upper 95%",
            "periods": "Periods",
        },
    )
    _limits(run, "comparisons")
    with st.expander("All saved paired comparisons and block-length sensitivity"):
        st.caption(
            "Includes subgroups, advance-code scenarios, queue comparisons and block length 8. "
            "The default table above keeps the whole cohort, final-code proxy and block length 4."
        )
        _table(_frame(run.artifacts["comparisons"]["rows"], names))
    with st.expander("Annual and detailed saved forecast results"):
        st.caption(
            "Exploratory view of saved results; the predefined aggregate above stays unchanged."
        )
        _table(_frame(run.artifacts["summary"]["rows"], names))


def _scenarios(run: E3SavedRun, names: dict[str, str]) -> None:
    st.subheader("Advance-ABC proxy sensitivity")
    st.write(
        "Compare the saved full-code proxy with missing/error scenarios. These scenarios "
        "measure dependence on the assumed advance signal; actual Cito/SQL snapshots still "
        "need dated availability and accuracy checks."
    )
    scenario_rows = _aggregate(run.artifacts["scenarios"]["rows"])
    if not scenario_rows:
        scenario_rows = run.artifacts["scenarios"]["rows"]
    gains = [
        {**row, "probability_error_reduction": -row["mean_difference"]}
        for row in run.artifacts["comparisons"]["rows"]
        if row.get("metric") == "brier"
        and isinstance(row.get("scenario"), str)
        and type(row.get("mean_difference")) in {int, float}
        and row.get("subgroup", "all") == "all"
        and row.get("block_length", 4) == 4
        and row.get("comparison", "advance_information") == "advance_information"
    ]
    methods = list(dict.fromkeys(row["candidate"] for row in gains)) or list(
        dict.fromkeys(
            row["method"] for row in scenario_rows if row.get("task", "direction") == "direction"
        )
    )
    method = (
        st.selectbox(
            "Advance-information comparison method",
            methods,
            index=methods.index("cb_equal_advance") if "cb_equal_advance" in methods else 0,
            format_func=lambda value: names.get(value, method_label(value)),
        )
        if methods
        else None
    )
    gains = [row for row in gains if row["candidate"] == method]
    frame = _frame([row for row in scenario_rows if row.get("method") == method], names)
    if gains:
        gain_frame = _frame(gains, names)
        gain_frame["scenario"] = gain_frame["scenario"].map(
            lambda value: SCENARIO_LABELS.get(value, value.replace("_", " "))
        )
        gain_frame["Comparison"] = (
            gain_frame["candidate"].astype(str) + " versus " + gain_frame["baseline"].astype(str)
        )
        _chart(
            gain_frame,
            "scenario",
            "probability_error_reduction",
            color="Comparison",
            title="Saved scenario gain against the named baseline",
            show_legend=False,
        )
        st.caption(
            "Positive reduction means lower probability error on the reconciled cases. "
            "Use the saved paired comparison intervals to assess uncertainty; "
            "this is not money saved."
        )
        _table(
            gain_frame[
                [
                    key
                    for key in (
                        "scenario",
                        "candidate",
                        "baseline",
                        "mean_difference",
                        "lower",
                        "upper",
                        "periods",
                    )
                    if key in gain_frame
                ]
            ]
        )
    else:
        measure = "brier" if "brier" in frame else "mean_difference"
        _chart(
            frame,
            "scenario",
            measure,
            color="method",
            title="Saved error across advance-ABC scenarios",
        )
    columns = ["scenario", "observed", "brier", "ece", "mae_dkk"]
    _table(
        frame[[key for key in columns if key in frame]],
        labels={
            "observed": "Observed",
            "brier": "Brier ↓",
            "ece": "Calibration error ↓",
        },
    )
    with st.expander("Detailed saved advance-code scenario scores"):
        _table(frame)
    _limits(run, "scenarios")
    st.subheader("Price uncertainty and large misses")
    st.caption(
        "A 90% prediction interval is a range for a package's next AIP, not a confidence "
        "interval for average model performance. Compare observed coverage with 0.90 and "
        "inspect width, bias, invalid intervals and extreme errors together. Missing outcomes "
        "stay unavailable. Forecast error and inventory revaluation are not realised savings."
    )
    uncertainty = run.artifacts["price_uncertainty"]["rows"]
    scenarios = list(dict.fromkeys(row.get("scenario", "perfect") for row in uncertainty))
    scenario = (
        st.selectbox(
            "Price-interval scenario",
            scenarios,
            index=scenarios.index("perfect") if "perfect" in scenarios else 0,
            format_func=lambda value: SCENARIO_LABELS.get(value, value.replace("_", " ")),
        )
        if scenarios
        else None
    )
    selected = [row for row in uncertainty if row.get("scenario", "perfect") == scenario]
    aggregate = _frame(
        _aggregate(selected) or [row for row in selected if "split" not in row], names
    )
    st.caption(
        "MAE and bias average period scores equally. Tail diagnostics below pool all observed "
        "package-period cases, preserving the actual largest miss. Detailed mean-of-period "
        "quantiles, maxima and worst-1% shares are labelled separately."
    )
    columns = ["method", "observed", "mae_dkk", "mae", "bias_dkk", "invalid"]
    _table(
        aggregate[[key for key in columns if key in aggregate]],
        labels={
            "observed": "Observed",
            "mae_dkk": "MAE (DKK)",
            "mae": "MAE (DKK)",
            "bias_dkk": "Bias (DKK)",
            "invalid": "Invalid prices",
        },
    )
    columns = [
        "method",
        "pooled_p95_dkk",
        "pooled_p99_dkk",
        "pooled_max_error_dkk",
        "pooled_top1_error_share",
    ]
    if any(key in aggregate for key in columns[1:]):
        _table(
            aggregate[[key for key in columns if key in aggregate]],
            labels={
                "pooled_p95_dkk": "Pooled 95th error (DKK)",
                "pooled_p99_dkk": "Pooled 99th error (DKK)",
                "pooled_max_error_dkk": "Largest observed error (DKK)",
                "pooled_top1_error_share": "Pooled error from worst 1%",
            },
        )
    else:
        st.info("Pooled tail diagnostics were not saved. Mean-of-period tails remain in details.")
    columns = [
        "method",
        "coverage90",
        "width90_dkk",
        "pooled_coverage90",
        "pooled_width90_dkk",
        "invalid_intervals90",
    ]
    _table(
        aggregate[[key for key in columns if key in aggregate]],
        labels={
            "coverage90": "Coverage · equal periods",
            "width90_dkk": "Width · equal periods (DKK)",
            "pooled_coverage90": "Coverage · pooled cases",
            "pooled_width90_dkk": "Width · pooled cases (DKK)",
            "invalid_intervals90": "Invalid intervals",
        },
    )
    st.caption(
        "Aggregate interval coverage and width use equal period weights; pooled columns use "
        "observed cases. The timelines show each period separately, against nominal 90% coverage."
    )
    per_period = _frame([row for row in selected if row.get("split") == "per_period"], names)
    if not per_period.empty:
        for metric, title in (
            ("coverage90", "Observed coverage of the saved 90% AIP interval by tariff period"),
            ("width90_dkk", "Saved 90% AIP interval width by tariff period"),
        ):
            curve = (
                per_period.loc[per_period[metric].notna()]
                if metric in per_period
                else pd.DataFrame()
            )
            _chart(
                curve,
                "period_start",
                metric,
                color="method",
                title=title,
                reference=0.90 if metric == "coverage90" else None,
            )
    with st.expander("Detailed saved price uncertainty"):
        _table(_frame(selected, names))
    _limits(run, "price_uncertainty")


def _queues(run: E3SavedRun, names: dict[str, str]) -> None:
    st.subheader("Review workload, rises and downside")
    st.write(
        "Natural workload shows each frozen policy's own review count. Matched workload "
        "compares policies at the same declared review budget. A shorter queue can look more "
        "precise while missing more opportunities; inspect falls, unknown outcomes "
        "and empty periods."
    )
    rows = run.artifacts["queue_tradeoffs"]["rows"]
    scenarios = list(dict.fromkeys(row.get("scenario", "perfect") for row in rows))
    scenario = (
        st.selectbox(
            "Review-queue sensitivity scenario",
            scenarios,
            index=scenarios.index("perfect") if "perfect" in scenarios else 0,
            format_func=lambda value: SCENARIO_LABELS.get(value, value.replace("_", " ")),
        )
        if scenarios
        else None
    )
    rows = [row for row in rows if row.get("scenario", "perfect") == scenario]
    aggregate = [row for row in rows if row.get("split", "evaluation_all") == "evaluation_all"]
    modes = list(
        dict.fromkeys(
            str(row["workload_mode"])
            for row in aggregate
            if row.get("workload_mode") in {"natural", "matched_count", "matched"}
        )
    )
    mode = (
        st.selectbox(
            "Saved review workload",
            modes,
            format_func=lambda value: {
                "natural": "Each policy's own workload",
                "matched_count": "Matched review counts",
            }.get(value, value),
        )
        if modes
        else None
    )
    selected = [row for row in aggregate if row.get("workload_mode") == mode] if mode else []
    frame = _frame(selected, names)
    if not frame.empty and "method" in frame and "policy" in frame:
        frame["Queue"] = frame["method"].astype(str) + " · " + frame["policy"].astype(str)
    x = "selection_rate" if "selection_rate" in frame else "selected"
    for metric, title in (
        ("rise_rate", "Rises versus review workload"),
        ("fall_rate", "Falls versus review workload"),
    ):
        _chart(frame, x, metric, color="Queue", title=title)
    columns = [
        "policy",
        "method",
        "selected",
        "observed",
        "selection_rate",
        "rise_rate",
        "fall_rate",
        "unknown_fraction",
        "empty_periods",
    ]
    _table(frame[[key for key in columns if key in frame]])
    grid = queue_curve_rows(aggregate)
    if grid:
        st.subheader("Exploratory fixed policy curves")
        families = list(dict.fromkeys(row["policy_family"] for row in grid))
        family = st.selectbox("Fixed policy curve", families)
        curve = _frame([row for row in grid if row["policy_family"] == family], names)
        for metric, title in (
            ("rise_rate", "Rises as the prespecified policy parameter changes"),
            ("fall_rate", "Falls as the prespecified policy parameter changes"),
            ("selection_rate", "Review workload as the prespecified policy parameter changes"),
        ):
            _chart(
                curve.sort_values("parameter_value"),
                "parameter_value",
                metric,
                color="curve_family",
                title=title,
            )
        st.caption(
            "Risk curves connect the fixed fall-penalty values. Threshold curves connect "
            "rise cutoffs at the same fall cap. These evaluated curves do not select a new policy."
        )
    with st.expander("All saved queue counts, periods, intervals and membership differences"):
        _table(_frame(rows, names))
    _limits(run, "queue_tradeoffs")
    st.caption(
        "Curves are saved policy sensitivity, not controls for tuning on exposed outcomes. "
        "A review selection is not an order; quantities, demand, expiry and supplier terms "
        "are needed to estimate purchasing value."
    )


def queue_curve_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group saved grid points by the policy that generated them, not by grid index."""

    def numeric(value: Any) -> bool:
        return type(value) in {int, float} and math.isfinite(value)

    result = []
    for row in rows:
        if row.get("workload_mode") != "descriptive_grid" or row.get("split") != "evaluation_all":
            continue
        policy, parameter = row.get("policy", ""), row.get("parameter")
        if not isinstance(policy, str):
            continue
        # The publisher serializes parameter for tables and separately declares typed
        # numeric curve coordinates. Never evaluate the display string as Python.
        risk = row.get("lambda", parameter)
        if policy.startswith("grid_risk_rank_") and numeric(risk):
            result.append(
                {
                    **row,
                    "policy_family": "Risk ranking",
                    "curve_family": "Fall penalty λ",
                    "parameter_value": risk,
                }
            )
        elif policy.startswith("grid_threshold_"):
            rise, fall = row.get("rise_threshold"), row.get("fall_threshold")
            if (
                "rise_threshold" not in row
                and "fall_threshold" not in row
                and isinstance(parameter, list)
                and len(parameter) == 2
            ):
                rise, fall = parameter
            if numeric(rise) and numeric(fall):
                result.append(
                    {
                        **row,
                        "policy_family": "Probability thresholds",
                        "curve_family": f"Fall probability cap {fall:g}",
                        "parameter_value": rise,
                    }
                )
    return result


def _peers(run: E3SavedRun, names: dict[str, str]) -> None:
    st.subheader("Comparable peers, broader peers and fallback")
    st.write(
        "Comparable peers share supported native package size/unit tokens. Broader group "
        "evidence has a different comparability assumption. Zero comparable peers is not "
        "necessarily a singleton group. Missing peer evidence must remain visible beside "
        "fallback results, rather than exclude otherwise valid own-price observations."
    )
    rows = run.artifacts["peer_coverage"]["rows"]
    subgroups = list(dict.fromkeys(row["subgroup"] for row in rows if "subgroup" in row))
    subgroup = (
        st.selectbox(
            "Saved peer subgroup",
            subgroups,
            index=subgroups.index("equal_zero") if "equal_zero" in subgroups else 0,
            format_func=lambda value: {
                "equal_supported": "Comparable peers available",
                "equal_zero": "Zero comparable peers",
                "equal_unsupported": "Comparable peer evidence unsupported",
                "broad_supported": "Broader peers available",
                "broad_different_size": "Different-size peers",
                "matched_equal_broad": "Both comparable and broader peers available",
            }.get(value, value.replace("_", " ")),
        )
        if subgroups
        else None
    )
    selected = [row for row in rows if row.get("subgroup") == subgroup]
    primary = [
        row
        for row in selected
        if row.get("split", "evaluation_all") == "evaluation_all"
        and row.get("scenario", "perfect") == "perfect"
    ]
    frame = _frame(primary, names)
    columns = ["method", "candidates", "observed", "outcome_coverage", "brier", "ece", "mae_dkk"]
    _table(frame[[key for key in columns if key in frame]])
    with st.expander("Detailed peer-subgroup scenarios and periods"):
        _table(_frame(selected, names))
    _limits(run, "peer_coverage")


def _review(run: E3SavedRun) -> None:
    st.subheader("Historical candidate review")
    st.caption(
        "Read-only inspection of frozen historical candidates. Saved selection and exclusions "
        "do not change with search or sorting. Predictions are distinct from later outcomes; "
        "this is not a current operational purchasing queue."
    )
    if not st.checkbox("Load saved historical candidates"):
        st.info("Candidate rows are loaded only when requested.")
        return
    reveal = st.checkbox("Reveal later historical outcomes", value=False)
    if reveal:
        st.warning(
            "Later outcomes are visible for retrospective review; they were not decision inputs."
        )
    try:
        frame = load_candidate_review(run, reveal_outcomes=reveal)
    except (ResearchError, OSError, ValueError):
        st.error("Saved candidate evidence is unavailable, corrupt or incompatible.")
        return
    controls = st.columns(3)
    filter_values = []
    for column, key in zip(controls, ("period_start", "scenario", "policy"), strict=True):
        values = sorted(frame[key].dropna().astype(str).unique())
        selected = column.selectbox(key.replace("_", " ").capitalize(), ["All", *values])
        filter_values.append(selected)
        if selected != "All":
            frame = frame[frame[key].astype(str) == selected]
    query = st.text_input("Find package number", placeholder="Exact package number")
    if query:
        frame = frame[frame["package_number"].astype(str) == query.strip()]
    selection = st.selectbox("Saved selection", ["All", "Selected", "Not selected"])
    if selection != "All":
        frame = frame[frame["selected"].eq(selection == "Selected")]
    # Filters and page navigation preserve saved membership and source row order.
    signature = (
        str(run.root),
        tuple(filter_values),
        query.strip(),
        selection,
    )
    page_key, filter_key = "e3_review_page", "e3_review_filters"
    pages = max(1, (len(frame) + 499) // 500)
    previous = int(st.session_state.get(page_key, 1))
    if st.session_state.get(filter_key) != signature:
        previous = 1
    st.session_state[filter_key] = signature
    st.session_state[page_key] = max(1, min(previous, pages))
    page = st.number_input(
        "Results page",
        min_value=1,
        max_value=pages,
        step=1,
        key=page_key,
        help="500 saved rows per page, in their original order.",
    )
    start = (int(page) - 1) * 500
    displayed = frame.iloc[start : start + 500]
    st.caption(
        f"{len(frame):,} saved rows match. Showing {start + 1 if len(frame) else 0:,}–"
        f"{start + len(displayed):,}; page {int(page):,} of {pages:,}. Saved order is unchanged."
    )
    preferred = [
        "package_number",
        "period_start",
        "selected",
        "current_aip_dkk",
        "next_aip_dkk",
        "delta_dkk",
        "p_increase",
        "lower90_dkk",
        "upper90_dkk",
        "next_status",
        "next_aip_ore",
        "next_direction",
    ]
    _table(
        displayed[[key for key in preferred if key in displayed]],
        labels={"selected": "Selected for review"},
    )
    with st.expander("Full saved candidate evidence"):
        _table(displayed)


def _details(run: E3SavedRun, names: dict[str, str]) -> None:
    st.subheader("Executed trials and resource limits")
    rows = run.artifacts["trials"]["rows"]
    failures = [row for row in rows if row["status"] == "failed"]
    if failures:
        st.warning(
            f"{len(failures)} declared trial(s) failed. Failed attempts have no accepted score."
        )
    _table(_frame(rows, names))
    st.caption(
        "Completed and attempted capacity are separate. Sampled process RSS is not an isolated "
        "model-memory measurement or a guaranteed memory cap. "
        "Failed trials remain part of the evidence."
    )
    _limits(run, "trials")
    st.subheader("Saved scope and definitions")
    st.json(
        {"split_dates": run.manifest["split_dates"], "definitions": run.manifest["definitions"]}
    )
    st.caption(
        "The original E2 viewer remains available through "
        "research_dashboard.py --e2 <result.json>. "
        "E3 uses the producer's reconciled saved comparison and preserves the E2 comparison guard."
    )


def main(index_path: Path | None = None) -> None:
    st.set_page_config(page_title="E3 historical research", layout="wide")
    st.title("E3 historical research")
    st.caption(
        "Pinned saved results only · no live database, model execution or purchasing controls"
    )
    if index_path is None:
        st.info("Supply an explicit pinned run index: research_dashboard.py --e3 <index.json>.")
        return
    try:
        index = load_e3_index(index_path)
        labels = {entry["run_id"]: entry["label"] for entry in index.runs}
        selected = st.selectbox("Saved research run", list(labels), format_func=labels.__getitem__)
        run = load_indexed_e3_run(index, selected)
    except (ResearchError, OSError, KeyError, TypeError, ValueError):
        st.error(
            "Saved E3 results are unavailable, incomplete, corrupt "
            "or lack reconciled comparison proof."
        )
        return
    st.markdown(f"**Exposure:** {run.manifest['exposure_status']}")
    st.warning(
        "Reconstructed historical research. Advance ABC uses a historical final-code proxy. "
        "No operational availability, realised savings or promoted method is established."
    )
    failures = [row for row in run.artifacts["trials"]["rows"] if row["status"] == "failed"]
    if failures:
        st.warning(
            f"{len(failures)} failed trial(s) are retained in Run details; "
            "capacity claims exclude them."
        )
    names = _names(run)
    tabs = st.tabs(
        [
            "Forecasts",
            "Scenarios and uncertainty",
            "Review workload",
            "Peer evidence",
            "Historical review",
            "Run details",
        ]
    )
    with tabs[0]:
        _forecasts(run, names)
    with tabs[1]:
        _scenarios(run, names)
    with tabs[2]:
        _queues(run, names)
    with tabs[3]:
        _peers(run, names)
    with tabs[4]:
        _review(run)
    with tabs[5]:
        _details(run, names)
