"""Saved E4 artifact reader. No acquisition, database, inference or outcome resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from .dataset import ResearchError, digest

METHODS = {
    "e2_rise_matched": "Current information reference",
    "e3_risk_rank_matched": "Advance ABC: equal peers, fall-aware ranking",
    "hybrid_rise_matched": "Routed + current fallback: rise ranking",
    "hybrid_threshold": "Routed + current fallback: threshold queue",
    "routed_threshold": "Routed advance · smaller threshold queue",
}
SCENARIOS = {
    "perfect": "Recorded next ABC proxy",
    "missing25": "Synthetic 25% missing codes",
    "missing100": "All advance codes missing",
    "wrong10": "Synthetic 10% incorrect codes",
    "group_wrong20": "Synthetic group-correlated incorrect codes",
}


def load(reference: dict[str, Any]) -> Path:
    path = Path(reference["path"])
    if (
        not path.is_file()
        or path.stat().st_size != reference["size_bytes"]
        or digest(path) != reference["sha256"]
    ):
        raise ResearchError("Saved artifact is missing or its checksum has changed")
    return path


def read(reference: dict[str, Any]) -> dict[str, Any]:
    return json.loads(load(reference).read_text(encoding="utf-8-sig"))


def table(values: Any) -> None:
    st.dataframe(pd.DataFrame(values), hide_index=True, width="stretch")


def candidate_view(entries: list[dict[str, Any]]) -> None:
    if not entries:
        st.info("No saved assessment is available. Preparing a run is a separate command.")
        return
    chosen = st.selectbox("Saved assessment", entries, format_func=lambda x: x["label"])
    assessment = read(chosen["assessment"])
    frame = pd.read_parquet(load(assessment["predictions"]))
    context = assessment["context"]
    st.write(f"**{chosen['label']}** · {context['origin']} → {context['target']}")
    st.caption(
        f"Created {assessment['created_utc']} · claimed decision cutoff "
        f"{context['decision_cutoff']} · model fitted before "
        f"{assessment['fit_cutoff']}. Research candidate; no operational promotion."
    )
    if context["mode"] == "prospective_pending":
        has_observed = bool(
            chosen.get("outcome") and read(chosen["outcome"])["states"].get("observed", 0)
        )
        st.info(
            (
                "Prospective prediction is frozen. Later official observations are "
                "attached as a separate check; this does not establish general "
                "prospective performance."
            )
            if has_observed
            else (
                "Prospective prediction is frozen. Target outcomes have not been "
                "attached; prospective performance is not established."
            )
        )
    else:
        st.info(
            "Actual report retrospective assessment. Predictions were created after "
            "the target prices became available."
        )
    if chosen.get("outcome"):
        outcome = read(chosen["outcome"])
        values = pd.read_parquet(load(outcome["rows"]))
        frame = frame.merge(
            values[["row_key", "status", "next_aip_ore", "next_delta_ore"]],
            on="row_key",
            validate="one_to_one",
        )
        frame["actual_next_aip_dkk"] = pd.to_numeric(frame.next_aip_ore) / 100
        st.caption(
            "Later outcomes are a separately versioned attachment. Original ranks, "
            "inputs and selections remain frozen."
        )
    a, b, c = st.columns(3)
    a.metric("Origin population", f"{len(frame):,}")
    b.metric("Predictions available", f"{assessment['predicted']:,}")
    c.metric("Selected", f"{assessment['selected']:,}")
    view = st.radio(
        "Candidate filter", ["Selected", "All", "Excluded / unavailable"], horizontal=True
    )
    if view == "Selected":
        frame = frame.loc[frame.selected]
    elif view == "Excluded / unavailable":
        frame = frame.loc[frame.prediction_state.ne("available")]
    query = st.text_input("Package number (leading zeros retained)")
    if query:
        frame = frame.loc[frame.package_number.str.contains(query, regex=False)]
    frame = frame.copy()
    frame["current_aip_dkk"] = frame.aip_ore / 100
    labels = {
        "package_number": "Package",
        "current_aip_dkk": "Current AIP · DKK",
        "abc": "Current official ABC",
        "report_current_abc": "Report current ABC",
        "advance_abc": "Advance ABC",
        "advance_state": "Advance input state",
        "p_rise": "Rise probability",
        "p_fall": "Fall probability",
        "p_unchanged": "Unchanged probability",
        "next_aip_dkk": "Forecast next AIP · DKK",
        "change_dkk": "Forecast change · DKK",
        "lower90_dkk": "Residual interval lower · DKK",
        "upper90_dkk": "Residual interval upper · DKK",
        "input_quality": "Input quality",
        "peer_route": "Direction support",
        "information_route": "Information route / fallback",
        "selection_reason": "Selection explanation",
        "prediction_state": "Prediction state",
        "exclusion_reason": "Exclusion reason",
        "status": "Later outcome state",
        "actual_next_aip_dkk": "Official next AIP · DKK",
        "rank": "Rank",
    }
    st.caption(
        "The amount forecast is a conditional median, not expected profit. The "
        "residual interval describes forecast error under earlier data; it is not a "
        "guaranteed future-price range or monetary benefit. Classifier direction and "
        "amount median can disagree."
    )
    if frame.empty:
        st.info(
            "No candidates match this filter. Missing predictions are retained in the "
            "saved population."
        )
    else:
        table(frame[[c for c in labels if c in frame]].rename(columns=labels))
    with st.expander("Input, model and outcome provenance"):
        st.json(
            {"context": context, "binding": assessment["binding"], "outcome": chosen.get("outcome")}
        )


def main(index_path: Path | None) -> None:
    st.set_page_config(page_title="Procurement research · E4", layout="wide")
    st.title("Procurement research · E4")
    st.caption("Saved research evidence · candidate review · hypothetical purchasing scenarios")
    if index_path is None or not index_path.is_file():
        st.info(
            "Choose a saved E4 index with --e4 PATH. This viewer never creates or resolves a run."
        )
        return
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
        if index.get("version") != "e4-result-index-v1":
            raise ResearchError("Incompatible result index version")
        summary, comparison = read(index["summary"]), read(index["comparison"])
        diagnostics, scenarios = read(index["diagnostics"]), read(index["scenarios"])
        tabs = st.tabs(
            ["Findings", "Cito report", "Candidates", "Purchasing scenarios", "Diagnostics"]
        )
        with tabs[0]:
            st.subheader("Fewer falls, with a rise-rate trade-off")
            st.write(
                "The smaller routed queue is the research candidate for prospective "
                "observation. Historical comparisons use an explicitly conditional "
                "next-ABC proxy and already exposed history. No financial return or "
                "prospective success is established."
            )
            scenario = st.selectbox(
                "Information sensitivity",
                list(SCENARIOS),
                format_func=lambda value: SCENARIOS[value],
            )
            selected = [
                r
                for r in summary["queues"]
                if r["scenario"] == scenario
                and r["policy"]
                in {
                    "e2_rise_matched",
                    "e3_risk_rank_matched",
                    "hybrid_rise_matched",
                    "hybrid_threshold",
                }
            ]
            chart = pd.DataFrame(
                [
                    {"Method": METHODS[r["policy"]], "Rise": r["rise_rate"], "Fall": r["fall_rate"]}
                    for r in selected
                ]
            ).melt(id_vars="Method", var_name="Observed outcome", value_name="Period-mean rate")
            fig = px.bar(
                chart,
                x="Period-mean rate",
                y="Method",
                orientation="h",
                color="Observed outcome",
                barmode="group",
                color_discrete_sequence=["#386cb0", "#e69528"],
                pattern_shape="Observed outcome",
            )
            fig.update_layout(legend_title_text="Observed outcome", margin=dict(b=20))
            fig.update_xaxes(tickformat=".0%")
            st.plotly_chart(fig, width="stretch")
            st.caption(
                "What this answers: among observed selected outcomes at identical "
                "per-period workloads, how often did prices rise or fall? Unknown "
                "outcomes stay selected, without replacements. Rates are not "
                "purchasing returns. Synthetic error rates are not measured Cito "
                "reliability."
            )
            table(
                [
                    {
                        "Method": METHODS[r["policy"]],
                        "Selected": r["selected"],
                        "Observed": r["observed"],
                        "Period-mean rise": r["rise_rate"],
                        "Period-mean fall": r["fall_rate"],
                        "Period-mean unknown": r["unknown_fraction"],
                        "Pooled unknown": r["pooled_unknown_fraction"],
                        "All-selected rise lower bound": r["rise_lower_bound"],
                        "All-selected rise upper bound": r["rise_upper_bound"],
                    }
                    for r in selected
                ]
            )
            st.warning(
                "The one prespecified uncertainty adjustment failed: overall coverage "
                "fell while interval width increased. It is retained as a negative "
                "result; the earlier price-band residual method remains the displayed "
                "research interval, with its known conditional weaknesses."
            )
            st.caption(
                "Candidate populations, target dates and availability modes must "
                "agree before comparing runs. The October actual-report run and "
                "November prospective run are incompatible for an improvement "
                "percentage."
            )
        with tabs[1]:
            st.subheader("One Cito snapshot, independently checked")
            st.write(
                "Current and advance codes are evaluated separately against the dated "
                "official editions. Missing codes and unmatched identifiers are "
                "distinct from incorrect codes. Cito kostpris is not used as official "
                "AIP."
            )
            table([{"Code context": name, **r} for name, r in comparison["totals"].items()])
            table(
                [
                    {
                        "Origin candidates": comparison["origin_candidates"],
                        "Report coverage": comparison["origin_candidates_in_report"],
                        "Without report": comparison["origin_candidates_without_report"],
                        "Selected": comparison["selected_candidates"],
                        "Selected covered": comparison["selected_candidates_in_report"],
                    }
                ]
            )
            st.caption(comparison["limit"])
            with st.expander("Transitions, groups and source receipt details"):
                table(comparison["transition_confusion"])
                table(comparison["groups"])
                st.json(read(comparison["report"]))
        with tabs[2]:
            candidate_view(index.get("assessments", []))
        with tabs[3]:
            st.subheader("Hypothetical procurement costs, stock and service")
            st.write(
                "All policies share origin-selected groups, group demand, starting "
                "stock and capacity. Demand is allocated FIFO within a supported "
                "equal-size group; clinical interchangeability and actual pharmacy "
                "sell-through remain unproved."
            )
            matrix = scenarios["matrix"]
            chosen = st.selectbox(
                "Business assumptions",
                range(len(matrix)),
                format_func=lambda n: (
                    f"Demand {matrix[n]['demand']} / fortnight · extra cap "
                    f"{matrix[n]['extra_limit']} · compensation "
                    f"{matrix[n]['compensation']} · {matrix[n]['variant']}"
                ),
            )
            rows = [r for r in scenarios["summary"] if r["scenario"] == chosen]
            names = {
                "ordinary": "Ordinary replenishment",
                "rule": "Deterministic ABC transition rule",
                "model": "Frozen model-informed policy",
            }
            table(
                [
                    {
                        "Policy": names[r["policy"]],
                        "Incremental cost proxy · DKK": r["incremental_cost_dkk"],
                        "Incremental cash outflow · DKK": r["incremental_cash_dkk"],
                        "Terminal inventory difference · DKK": r[
                            "incremental_terminal_inventory_dkk"
                        ],
                        "Worst-period cost difference · DKK": r[
                            "worst_period_incremental_cost_dkk"
                        ],
                        "Extra unmet demand": r["incremental_unmet"],
                        "Expired packs": r["expired_packs"],
                        "Break-even extra fee per pack · DKK": r[
                            "break_even_extra_fee_dkk_per_extra_pack"
                        ],
                    }
                    for r in rows
                ]
            )
            st.caption(
                f"{scenarios['scored_groups']:,} complete group trajectories; "
                f"{scenarios['unknown_groups']:,} origin-selected trajectories remain "
                f"unscored without replacements. Cost proxy subtracts terminal market "
                f"inventory once; cash flow and inventory are shown separately. No "
                f"sales margin is invented."
            )
            st.info(
                "Compensation cases are hypothetical: none, half with a two-pack cap, "
                "or full eligible credit. Returns are disabled except the explicit "
                "returns case (90% remaining acquisition basis after prior credits). "
                "Modern public Nomeco terms motivate sensitivities; they are not "
                "evidence of this pharmacy's coverage or historical credits."
            )
            st.markdown(
                "[Nomeco pharmacy information](https://nomeco.dk/apoteker) · [Public "
                "discount "
                "terms](https://nomeco.dk/sites/nomeco.dk/files/2025-08/Rabatskilt%201%20oktober%202025.pdf)"
            )
        with tabs[4]:
            st.subheader("Unknown outcomes and monetary tails")
            table(diagnostics["unknown"])
            table(diagnostics["tail_concentration"])
            with st.expander("Conditional amount error and source-traced extremes"):
                table(diagnostics["money_strata"])
                st.json(read(diagnostics["cases"]))
            with st.expander("Uncertainty coverage and width"):
                table([r for r in summary["money"] if r["scenario"] == "perfect"])
            with st.expander("Protocol and saved result index"):
                st.json(index)
    except (OSError, ValueError, KeyError, ResearchError) as exc:
        st.error(f"Saved E4 results could not be read safely: {exc}")
        st.info(
            "Restore the pinned artifact or choose a compatible index. No live fallback is used."
        )
