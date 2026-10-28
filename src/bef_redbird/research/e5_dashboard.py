"""Read-only E5 evidence viewer. Only explicit, checksum-pinned saved files are read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from .dataset import ResearchError, digest

LABELS = {
    "package_number": "Package",
    "package_id": "Package ID",
    "period_start": "Origin period",
    "aip_ore": "Current AIP · øre",
    "current_aip_dkk": "Current AIP · DKK",
    "p_rise": "E5 rise probability",
    "p_fall": "E5 fall probability",
    "reference_p_rise": "Original E4 rise probability",
    "reference_p_fall": "Original E4 fall probability",
    "forecast_delta_dkk": "Mean change · DKK / pack",
    "median_delta_dkk": "Median change · DKK / pack",
    "forecast_percent": "Forecast change · % of current AIP",
    "lower_delta_dkk": "Change interval lower · DKK / pack",
    "upper_delta_dkk": "Change interval upper · DKK / pack",
    "persistence_probability": "Joint rise + persistence probability",
    "calibration_route": "Probability calibration route",
    "information_route": "Information used / fallback",
    "peer_route": "Peer support used",
    "peer_count": "Equal-size peers at origin",
    "peer_feature_state": "Equal-size peer evidence state",
    "selection_reason": "Selection explanation",
    "interval_method": "Displayed interval method",
    "eligibility_reason": "Eligibility explanation",
    "eligible": "Eligible at origin",
    "policy_eligible": "Current recommendation policy allows",
    "recommendation_eligible": "Historical recommendation policy allows",
    "recommendation_policy_eligible": "Recommendation policy allows",
    "recommendation_policy_flagged": "Flagged by recommendation policy",
    "company": "Company",
    "package_distributor": "Package distributor",
    "package_distributor_name": "Package distributor name at origin",
    "drug_distributor_name": "Drug distributor name at origin",
    "authorisation_holder_name": "Authorisation holder name at origin",
    "policy": "Policy",
    "rank": "Frozen rank",
    "selected": "Selected",
    "next_status": "Later next-price status",
    "next_aip_ore": "Later next AIP · øre",
    "next_aip_dkk": "Later next AIP · DKK",
    "second_status": "Later second-price status",
    "second_aip_ore": "Later second AIP · øre",
    "status": "Status",
    "fold": "Evaluation fold",
    "method": "Method",
    "information": "Information available",
    "population": "Population",
    "brier": "Brier score · lower is better",
    "equal_period_brier": "Equal-period Brier score · lower is better",
    "mae_dkk": "Mean absolute error · DKK / pack",
    "rmse_dkk": "RMSE · DKK / pack",
    "bias_dkk": "Mean forecast error · DKK / pack",
    "coverage90": "90% interval coverage",
    "width90_dkk": "90% interval width · DKK / pack",
    "observed": "Observed outcomes",
    "unknown_selected": "Selected with unknown outcomes",
    "quantity": "Packs allocated",
    "workload": "Workload comparison",
    "allocation": "Allocation assumption",
    "origin_population": "Original origin population",
    "matched_shortfall": "Matched workload gap",
    "forecast_unknown_quantity": "Packs without amount forecast",
    "observed_price_exposure_ore": "Observed price movement · øre",
    "observed_price_exposure_dkk": "Observed price movement · DKK",
    "forecast_price_exposure_dkk": "Forecast price movement · DKK",
    "allocated_ore": "Acquisition allocation · øre",
    "allocated_dkk": "Acquisition allocation · DKK",
    "unallocated_capital_ore": "Capital left as cash · øre",
    "unallocated_capital_dkk": "Capital left as cash · DKK",
    "worst_period_price_exposure_dkk": "Lowest observed period price movement · DKK",
    "mean_acquisition_price_per_allocated_pack_dkk": (
        "Mean acquisition price · DKK / allocated pack"
    ),
}
STATUS_LABELS = {
    "scientific_evaluation_complete_delivery_record_separate": (
        "Historical evaluation saved · research only"
    ),
    "research_evaluated_delivery_pending": "Historical evaluation saved · delivery checks pending",
    "completed": "Saved research evaluation is complete",
    "pending": "Research evaluation is pending",
    "failed": "Research evaluation is incomplete",
}
METHOD_LABELS = {
    "accepted_e4": "Accepted E4 probabilities · original calibration",
    "fresh": "Refreshed, unchanged settings",
    "tuned": "Refreshed, tuned settings",
    "early_stop": "Original settings with early stopping",
    "shallow": "Shallower trees, slower learning",
    "slow": "Slower learning, stronger regularisation",
    "frozen_mae": "Retained E4 median (MAE)",
    "mae": "Refreshed median (MAE)",
    "rmse": "Mean-oriented forecast (RMSE)",
    "huber": "Robust forecast (Huber)",
    "quantile": "5th / 50th / 95th quantiles",
    "no_change": "No price change reference",
    "joint_persistence": "Joint rise and persistence model",
    "rise_probability_proxy": "Rise probability proxy",
}
VALUE_LABELS = {
    "information": {
        "current": "Current information",
        "advance": "Conditional next-ABC proxy",
        "hybrid": "Advance with current fallback",
    },
    "comparison": {
        "frozen_E4_reference": "Frozen E4 probability reference",
        "same_E5_probabilities": "Same E5 probabilities: ranking comparison",
    },
    "policy": {
        "probability_reference": "Probability reference",
        "amount_dkk": "Mean price change",
        "downside_aware": "Mean change minus downside",
        "persistence_aware": "Persistence gate on downside ranking",
    },
    "workload": {"natural": "Natural queue", "matched": "Matched reference workload"},
    "allocation": {
        "per_pack": "One pack per candidate",
        "four_pack": "Four packs per candidate",
        "fixed_capital": "DKK 100,000 per origin with concentration caps",
    },
}


def display_values(frame: pd.DataFrame) -> pd.DataFrame:
    """Names affect presentation only; saved identifiers and scores stay unchanged."""
    out = frame.copy()
    for method_column in ("method", "candidate", "reference"):
        if method_column not in out:
            continue
        names = []
        for row in out.to_dict("records"):
            method = str(row[method_column])
            if method == "frozen":
                raw = "raw" in str(row.get("population", ""))
                names.append(
                    "Frozen E4 models · raw"
                    if raw
                    else "Frozen E4 reference calibration"
                    if row.get("fold") == "screen"
                    else "Frozen E4 models · common recalibration"
                )
            else:
                names.append(METHOD_LABELS.get(method, method.replace("_", " ")))
        out[method_column] = names
    for column, mapping in VALUE_LABELS.items():
        if column in out:
            out[column] = out[column].map(lambda x, names=mapping: names.get(x, x))
    if "feature" in out:
        out["feature"] = out.feature.map(label)
    return out


def label(column: Any) -> str:
    name = str(column)
    if name in LABELS:
        return LABELS[name]
    text = name.replace("_dkk", " · DKK").replace("_ore", " · øre").replace("_", " ")
    return text[:1].upper() + text[1:]


def pinned(reference: dict[str, Any]) -> Path:
    if not isinstance(reference, dict) or not all(
        key in reference for key in ("path", "sha256", "size_bytes")
    ):
        raise ResearchError("Invalid saved artifact reference")
    path = Path(reference["path"])
    if (
        not path.is_file()
        or path.stat().st_size != reference["size_bytes"]
        or digest(path) != reference["sha256"]
    ):
        raise ResearchError("Saved artifact is missing or its checksum has changed")
    return path


def read_json(reference: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(pinned(reference).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ResearchError("Saved metadata must be an object")
    return value


def read_rows(reference: dict[str, Any]) -> pd.DataFrame:
    path = pinned(reference)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, dict):
        rows = value.get("rows", [])
        if isinstance(rows, dict) and "sha256" in rows:
            return read_rows(rows)
        value = rows
    if not isinstance(value, list):
        raise ResearchError("Saved rows must be a table or pinned row reference")
    return pd.DataFrame(value)


def show_table(values: Any, empty: str = "No saved rows are available for this section.") -> None:
    frame = values.copy() if isinstance(values, pd.DataFrame) else pd.DataFrame(values)
    if frame.empty:
        st.info(empty)
        return
    # Exact saved money remains in øre; the presentation uses familiar DKK units.
    for column in list(frame):
        if str(column).endswith("_ore"):
            displayed = str(column)[:-4] + "_dkk"
            if displayed not in frame:
                frame[displayed] = pd.to_numeric(frame[column], errors="coerce") / 100
            frame = frame.drop(columns=[column])
    if "period_start" in frame:
        frame["period_start"] = frame.period_start.astype(str).str[:10]
    st.dataframe(display_values(frame).rename(columns=label), hide_index=True, width="stretch")


def metric_section(title: str, rows: list[dict[str, Any]], metrics: tuple[str, ...] = ()) -> None:
    st.subheader(title)
    show_table(rows)
    if not rows:
        return
    frame = pd.DataFrame(rows)
    metric = next(
        (x for x in metrics if x in frame and pd.api.types.is_numeric_dtype(frame[x])), None
    )
    if metric and "method" in frame:
        if "population" in frame:
            populations = frame.population.dropna().unique().tolist()
            preferred = next(
                (x for x in ("full_routes_calibrated", "all") if x in populations), None
            )
            population = (
                preferred if preferred is not None else populations[0] if populations else None
            )
            if population is not None:
                frame = frame.loc[frame.population.eq(population)]
                st.caption(
                    f"Chart population: {label(population)}. "
                    "The table retains every saved comparison."
                )
        colors = "information" if "information" in frame else None
        facet = "fold" if "fold" in frame and frame.fold.nunique() <= 4 else None
        frame = display_values(frame)
        figure = px.bar(
            frame,
            x="method",
            y=metric,
            color=colors,
            barmode="group",
            facet_col=facet,
            labels={c: label(c) for c in frame},
        )
        st.plotly_chart(figure, width="stretch")


def recommendation_flags(frame: pd.DataFrame) -> pd.DataFrame:
    """A view-only flag; missing policy facts never become automatic exclusions."""
    out = frame.copy()
    allowed = pd.Series(pd.NA, index=out.index, dtype="boolean")
    for column in ("policy_eligible", "recommendation_eligible"):
        if column in out:
            allowed = allowed.combine_first(out[column].astype("boolean"))
    origin = (
        out.eligible.astype("boolean").fillna(False)
        if "eligible" in out
        else pd.Series(False, index=out.index)
    )
    out["recommendation_policy_eligible"] = allowed
    out["recommendation_policy_flagged"] = origin & allowed.eq(False).fillna(False)
    return out


def default_candidate_origin(frame: pd.DataFrame, assessment: dict[str, Any]) -> str | None:
    if "review_scope" in frame:
        current = frame.loc[frame.review_scope.eq("current_frozen_assessment"), "period_start"]
        if len(current):
            return str(pd.to_datetime(current).max())[:10]
    origin = assessment.get("context", {}).get("origin")
    return str(origin)[:10] if origin is not None else None


def current_training_scope(index: dict[str, Any]) -> dict[str, Any]:
    """Read actual delivered cutoffs separately from the frozen historical plan."""
    assessment = read_json(index["assessment"]) if index.get("assessment") else {}
    bundle = read_json(index["model_bundle"]) if index.get("model_bundle") else {}
    if bundle and assessment:
        expected = assessment.get("binding", {}).get("models")
        if expected and expected != index["model_bundle"]:
            raise ResearchError("Current model bundle does not match the saved assessment")
        for bundle_key, assessment_key in (
            ("direction_fit_cutoff", "fit_cutoff"),
            ("amount_fit_cutoff", "amount_fit_cutoff"),
            ("component_fit_cutoffs", "component_fit_cutoffs"),
        ):
            if (
                bundle_key in bundle
                and assessment_key in assessment
                and bundle[bundle_key] != assessment[assessment_key]
            ):
                raise ResearchError("Current assessment and model bundle training cutoffs disagree")
    return {
        "direction": bundle.get("direction_fit_cutoff", assessment.get("fit_cutoff")),
        "amount": bundle.get("amount_fit_cutoff", assessment.get("amount_fit_cutoff")),
        "retained": assessment.get("retained_component_fit_cutoff", "2026-01-01"),
        "planned": bundle.get(
            "planned_final_fit_cutoff", assessment.get("planned_final_fit_cutoff")
        ),
        "resource_fallback": bundle.get("resource_fallback", assessment.get("resource_fallback")),
    }


def show_current_training_scope(index: dict[str, Any]) -> None:
    scope = current_training_scope(index)
    if not scope["direction"] and not scope["amount"]:
        return
    st.subheader("Current assessment · actual model cutoffs")
    if scope["resource_fallback"]:
        pinned(scope["resource_fallback"])
        st.warning(
            f"The planned {scope['planned']} direction fit reached the unchanged resource limit. "
            f"The current assessment reuses the completed {scope['direction']} direction pair. "
            "No retry or extra model search was used. The historical results retain the "
            "earlier plan; actual delivered cutoffs are shown here."
        )
    st.dataframe(
        pd.DataFrame(
            {
                "Model component": [
                    "Broader-peer direction",
                    "Mean and quantile amounts",
                    "Retained package / equal-peer direction and median",
                ],
                "Training cutoff": [scope["direction"], scope["amount"], scope["retained"]],
            }
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption("Labels mature strictly before each component's cutoff; research only.")


def candidate_view(index: dict[str, Any]) -> None:
    if not index.get("review"):
        st.info("No saved candidate review is available yet.")
        return
    frame = read_rows(index["review"])
    if frame.empty:
        st.info("The saved assessment has no candidates.")
        return
    if "row_key" not in frame or "period_start" not in frame:
        raise ResearchError("Candidate table is missing its frozen identity or origin period")
    if any(str(x).startswith(("next_", "second_", "outcome")) for x in frame):
        raise ResearchError("Later outcomes must be saved separately from candidate inputs")
    st.caption(
        "These are frozen research candidates. Filters below are exploratory and do not change "
        "official scores, ranks or selections. The mean-oriented forecast estimates "
        "price movement; "
        "the median and interval describe different quantities. None establishes pharmacy profit."
    )
    assessment = read_json(index["assessment"]) if index.get("assessment") else {}
    if assessment:
        with st.expander("Assessment timing and input provenance"):
            st.caption(
                "This timing describes the current frozen assessment. Historical rows retain "
                "their own origin periods and evaluation folds."
            )
            st.json(assessment)
    has_recommendation = any(x in frame for x in ("policy_eligible", "recommendation_eligible"))
    frame = recommendation_flags(frame)
    reveal = st.checkbox("Reveal separately saved later outcomes", value=False)
    if reveal:
        if not index.get("outcomes"):
            st.info("No later outcome attachment is saved. Pending outcomes remain unknown.")
        else:
            outcomes = read_rows(index["outcomes"])
            if outcomes.empty:
                st.info("The saved later-outcome attachment has no rows yet.")
            elif "row_key" not in outcomes or outcomes.row_key.duplicated().any():
                raise ResearchError("Later outcomes do not have unique assessment row identities")
            else:
                columns = [x for x in outcomes if x == "row_key" or x not in frame]
                frame = frame.merge(
                    outcomes[columns], on="row_key", how="left", validate="many_to_one"
                )
                st.caption(
                    "Later observations are a separate attachment. Unknown outcomes are "
                    "retained without replacements."
                )
    periods = sorted(frame.period_start.astype(str).str[:10].unique())
    latest = default_candidate_origin(frame, assessment)
    initial_period = periods.index(latest) + 1 if latest in periods else 0
    period_control, view_control = st.columns([1, 2])
    with period_control:
        chosen = st.selectbox("Origin period", ["All periods", *periods], index=initial_period)
    if chosen != "All periods":
        frame = frame.loc[frame.period_start.astype(str).str[:10].eq(chosen)]
    a, b = st.columns(2)
    a.metric("Candidates in origin view", f"{len(frame):,}")
    if "selected" in frame:
        b.metric("Saved selections in origin view", f"{int(frame.selected.fillna(False).sum()):,}")
    options = ["All candidates"]
    if "selected" in frame:
        options.append("Selected candidates")
    if has_recommendation:
        options.append("Flagged by recommendation policy")
    if "eligible" in frame:
        options.append("Excluded at origin")
    with view_control:
        view = st.radio("Exploratory candidate filter", options, horizontal=True)
    if view == "Selected candidates":
        frame = frame.loc[frame.selected.fillna(False)]
    elif view == "Flagged by recommendation policy":
        frame = frame.loc[frame.recommendation_policy_flagged]
    elif view == "Excluded at origin":
        frame = frame.loc[~frame.eligible.fillna(False)]
    package_column = "package_number" if "package_number" in frame else "package_id"
    query_control, limit_control = st.columns([3, 1])
    with query_control:
        query = st.text_input("Package search (leading zeros retained)")
    with limit_control:
        maximum = st.number_input(
            "Maximum candidates shown", min_value=1, max_value=1000000, value=100, step=1
        )
    if query and package_column in frame:
        frame = frame.loc[frame[package_column].astype(str).str.contains(query, regex=False)]
    if "policy" in frame and frame.policy.nunique() > 1:
        policy = st.selectbox(
            "Saved policy", ["All policies", *sorted(frame.policy.dropna().unique())]
        )
        if policy != "All policies":
            frame = frame.loc[frame.policy.eq(policy)]
    all_rows = st.checkbox("Show all matching candidates", value=False)
    sort = [x for x in ("period_start", "rank", package_column) if x in frame]
    if sort:
        frame = frame.sort_values(sort, kind="stable")
    st.caption(f"{len(frame):,} matching candidates; display limit does not change saved workload.")
    if "aip_ore" in frame:
        frame = frame.assign(current_aip_dkk=pd.to_numeric(frame.aip_ore, errors="coerce") / 100)
    if frame.empty:
        st.info("No candidates match this filter. The saved population is unchanged.")
        return
    shown = frame if all_rows else frame.head(int(maximum))
    forecasts, rationale, companies = st.tabs(
        ["Forecasts and control", "Selection rationale", "Dated company roles"]
    )
    identity_columns = [package_column, "period_start"]
    with forecasts:
        forecast_identity = identity_columns if chosen == "All periods" else [package_column]
        st.caption("Mean-oriented price-change forecast · DKK per pack and % of current AIP.")
        mean_columns = [
            *forecast_identity,
            "current_aip_dkk",
            "p_rise",
            "forecast_delta_dkk",
            "forecast_percent",
        ]
        show_table(shown[[c for c in mean_columns if c in shown]])
        methods = (
            shown.interval_method.dropna().astype(str).unique().tolist()
            if "interval_method" in shown
            else []
        )
        st.caption(
            "Median and bounds describe different quantities from the mean forecast. "
            + (
                "Displayed interval method: " + "; ".join(methods) + "."
                if methods
                else "No interval method is recorded for this saved view."
            )
        )
        if methods and shown.interval_method.isna().any():
            st.caption(
                "Some rows have no saved interval-method explanation; those remain unspecified."
            )
        uncertainty_columns = [
            *forecast_identity,
            "median_delta_dkk",
            "lower_delta_dkk",
            "upper_delta_dkk",
            "persistence_probability",
        ]
        show_table(shown[[c for c in uncertainty_columns if c in shown]])
        st.caption(
            "Original E4 probabilities belong to the saved reference selection. E5 research "
            "probabilities are separate forecasts; a reference selection can differ from "
            "the new model's preference."
        )
        control_columns = [
            *forecast_identity,
            "reference_p_rise",
            "reference_p_fall",
            "p_rise",
            "p_fall",
        ]
        show_table(shown[[c for c in control_columns if c in shown]])
        if reveal:
            status = "next_status" if "next_status" in shown else "status"
            next_price = "next_aip_dkk" if "next_aip_dkk" in shown else "next_aip_ore"
            outcome_columns = [
                *forecast_identity,
                status,
                next_price,
                "second_status",
                "second_aip_ore",
            ]
            if any(c in shown for c in outcome_columns[len(forecast_identity) :]):
                st.caption(
                    "Revealed later outcomes · separate from frozen forecasts and selections."
                )
                show_table(shown[[c for c in outcome_columns if c in shown]])
    with rationale:
        st.caption(
            "Recommendation eligibility uses the current policy field, falling back to the "
            "historical recommendation field when missing. Origin-ineligible rows are "
            "separate from recommendation-policy flags."
        )
        ranking_columns = [*identity_columns, "rank", "selected", "policy"]
        show_table(shown[[c for c in ranking_columns if c in shown]])
        eligibility_columns = [
            *identity_columns,
            "eligible",
            "recommendation_policy_eligible",
            "recommendation_policy_flagged",
        ]
        show_table(shown[[c for c in eligibility_columns if c in shown]])
        reason_columns = [*identity_columns, "eligibility_reason", "selection_reason"]
        show_table(shown[[c for c in reason_columns if c in shown]])
        route_columns = [
            *identity_columns,
            "information_route",
            "peer_route",
            "peer_count",
        ]
        if any(c in shown for c in route_columns[2:]):
            st.caption("Blank fields mean the saved explanation is unavailable.")
            show_table(shown[[c for c in route_columns if c in shown]])
        interval_columns = [
            *identity_columns,
            "peer_feature_state",
            "calibration_route",
            "interval_method",
        ]
        if any(c in shown for c in interval_columns[2:]):
            show_table(shown[[c for c in interval_columns if c in shown]])
    with companies:
        st.caption(
            "Company names retain their dated origin roles. Unknown names stay blank; "
            "company roles are distinct."
        )
        role_columns = [
            "package_distributor_name",
            "drug_distributor_name",
            "authorisation_holder_name",
        ]
        if any(c in shown for c in role_columns):
            show_table(shown[[*identity_columns, *[c for c in role_columns if c in shown]]])
        else:
            st.info("No dated company role names are saved for these candidates.")
    with st.expander("All saved candidate fields"):
        show_table(shown)


def main(index_path: Path | None) -> None:
    st.set_page_config(page_title="Procurement research · E5", layout="wide")
    st.title("Procurement research · E5")
    st.caption(
        "Saved evidence · forecast reliability · candidate review · hypothetical procurement"
    )
    if index_path is None or not index_path.is_file():
        st.info("Choose a saved E5 index with --e5 PATH. Research execution is a separate command.")
        return
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
        if not isinstance(index, dict) or index.get("version") != "e5-index-v1":
            raise ResearchError("Incompatible E5 result index version")
        result = read_json(index["results"])
        if result.get("version") != "e5-results-v1":
            raise ResearchError("Incompatible E5 saved results version")
        # Validate every declared top-level pin, including hidden later outcomes.
        for key in (
            "protocol",
            "review",
            "outcomes",
            "population",
            "fit_ledger",
            "assessment",
            "model_bundle",
        ):
            if index.get(key):
                pinned(index[key])
        tabs = st.tabs(
            [
                "Overview",
                "Freshness and tuning",
                "Money and reliability",
                "Eligibility",
                "Persistence and scenarios",
                "Candidate review",
            ]
        )
        with tabs[0]:
            st.subheader("What the saved comparisons establish")
            status = str(result.get("status", "pending"))
            st.write(f"**{STATUS_LABELS.get(status, status.replace('_', ' '))}**")
            st.info(
                str(
                    result.get(
                        "exposure",
                        "Historical research is exposed retrospective development; "
                        "prospective confirmation is pending.",
                    )
                )
            )
            show_current_training_scope(index)
            for conclusion in result.get("conclusions", []):
                st.write(conclusion)
            if not result.get("conclusions"):
                st.info("Conclusions have not been saved yet.")
            st.caption(
                "Rates use their reported observed denominator. Unknown selections remain "
                "selected. Different populations, dates or workloads require separate comparisons."
            )
            with st.expander("Frozen selection and research limits"):
                st.json(result.get("selection", {}))
                for limitation in result.get("limitations", []):
                    st.write(limitation)
            with st.expander("Protocol and artifact provenance"):
                st.json(
                    {
                        key: result[key]
                        for key in ("version", "status", "created_utc")
                        if key in result
                    }
                )
                if index.get("protocol"):
                    st.json(read_json(index["protocol"]))
                st.json(index)
        with tabs[1]:
            st.caption(
                "Fresh fitting and tuning are separate comparisons. Lower Brier scores indicate "
                "better probability forecasts on the stated population and dates."
            )
            metric_section(
                "Direction forecasts",
                result.get("direction", []),
                ("equal_period_brier", "brier", "brier_score"),
            )
            if index.get("contrasts"):
                st.subheader("Paired temporal comparisons")
                contrast = read_json(index["contrasts"])
                st.caption(
                    "Saved paired comparisons use the same origin periods and populations. "
                    "Intervals describe variation in exposed history, "
                    "not new prospective confirmation."
                )
                for key in ("rows", "contrasts", "summary"):
                    if isinstance(contrast.get(key), list):
                        show_table(contrast[key])
                if isinstance(contrast.get("periods"), list):
                    with st.expander("Results by origin period"):
                        show_table(contrast["periods"])
                with st.expander("Temporal comparison definitions"):
                    st.json(contrast)
            with st.expander("Saved fit attempts and reuse"):
                if index.get("fit_ledger"):
                    st.json(read_json(index["fit_ledger"]))
                else:
                    st.info("No fit ledger is saved yet.")
        with tabs[2]:
            st.caption(
                "RMSE fits are mean-oriented; MAE fits estimate a conditional median. Huber is a "
                "robust forecast. Quantile intervals require empirical coverage and width checks, "
                "including subgroups."
            )
            metric_section(
                "Amount error and monetary tails", result.get("amount", []), ("rmse_dkk", "mae_dkk")
            )
            metric_section(
                "Interval reliability", result.get("intervals", []), ("coverage90", "coverage")
            )
            metric_section("Price exposure and allocated cash", result.get("financial", []))
            st.caption(
                "At matched workload, the same E5 probabilities comparison isolates ranking "
                "from changes in direction forecasts. The lowest observed period movement "
                "is not a loss bound for unresolved outcomes."
            )
            if index.get("explanations"):
                st.subheader("What the models used")
                explanation = read_json(index["explanations"])
                st.caption(
                    explanation.get(
                        "claim",
                        "Feature contributions describe model behaviour; "
                        "they are not causal effects.",
                    )
                )
                summaries = explanation.get("summaries", [])
                for item in summaries:
                    st.write(f"**{str(item.get('model', 'Saved model')).replace('_', ' ')}**")
                    if item.get("units"):
                        st.caption(str(item["units"]))
                    show_table(item.get("features", []))
                if explanation.get("advance_direction_importance"):
                    with st.expander("Direction feature importance"):
                        show_table(explanation["advance_direction_importance"])
                with st.expander("Saved example contributions and sampling"):
                    st.write(explanation.get("sampling", ""))
                    cases = []
                    for case in explanation.get("cases", []):
                        row = {k: v for k, v in case.items() if k != "top_contributions"}
                        for number, contribution in enumerate(case.get("top_contributions", []), 1):
                            row[f"feature_{number}"] = label(contribution["feature"])
                            row[f"contribution_{number}"] = contribution["contribution"]
                        cases.append(row)
                    show_table(cases)
            st.info(
                "Price movement is a purchasing research exposure. Pharmacy sales, realised "
                "savings and optimal order quantities are unmeasured."
            )
        with tabs[3]:
            st.subheader("Origin eligibility and company hypotheses")
            st.caption(
                "Named-company and behaviour filters are hypotheses. Company roles, missing "
                "mappings and origin eligibility reasons must remain visible; later survival "
                "cannot determine eligibility."
            )
            if index.get("population"):
                population = read_json(index["population"])
                for key in ("summary", "rows", "comparisons"):
                    if isinstance(population.get(key), list):
                        show_table(population[key])
                with st.expander("Population evidence and missingness"):
                    st.json(population)
            else:
                st.info("No saved population evidence is available yet.")
        with tabs[4]:
            metric_section("Joint rise and persistence", result.get("persistence", []), ("brier",))
            st.caption(
                "Persistence means next and second-next prices both exceed the origin price. "
                "Its probability estimates a joint event. Financial selection uses a separate "
                "probability gate with the unchanged downside score."
            )
            metric_section("Hypothetical shared-group scenarios", result.get("scenarios", []))
            st.info(
                "All policies share group demand, initial stock, capacity and timing. Cost proxy, "
                "cash, stock valuation, compensation and return credits are separate. Unknown "
                "group trajectories remain unscored. Modern credit assumptions do not establish "
                "account-specific or historical entitlement."
            )
        with tabs[5]:
            candidate_view(index)
    except (OSError, ValueError, KeyError, TypeError, ResearchError) as exc:
        st.error(f"Saved E5 results could not be read safely: {exc}")
        st.info(
            "Choose compatible pinned evidence or restore the original artifact. "
            "No live fallback is used."
        )
