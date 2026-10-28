"""Bounded consequences of accepted Cito discrepancies; no fitting or source intake."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e3_methods import select_queue
from .e4_assessment import predict
from .e4_common import checked, frozen_json, read, utc
from .e5_results import json_records
from .e6_protocol import require
from .group_features import pin

SCENARIOS = ("actual", "corrected_wrong_only", "no_report_current", "no_report_hybrid_temperature")
REPLAY_COLUMNS = [
    "row_key",
    "prediction_state",
    "p_fall",
    "p_unchanged",
    "p_rise",
    "next_aip_dkk",
    "change_dkk",
    "lower90_dkk",
    "upper90_dkk",
    "peer_route",
    "information_route",
    "selected",
    "selected_risk_decile",
    "rank",
    "risk_score",
    "selection_reason",
]


def align(frame: pd.DataFrame, other: pd.DataFrame) -> pd.DataFrame:
    if frame.row_key.duplicated().any() or other.row_key.duplicated().any():
        raise ResearchError("e6_cito_duplicate_keys")
    if set(frame.row_key) != set(other.row_key):
        raise ResearchError("e6_cito_population_changed")
    return other.set_index("row_key").reindex(frame.row_key).reset_index()


def corrected_inputs(
    frame: pd.DataFrame, comparison: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Only independently wrong comparable codes change; current facts remain identical."""
    if frame.package_number.duplicated().any() or comparison.package_number.duplicated().any():
        raise ResearchError("e6_cito_ambiguous_package")
    wrong = comparison.loc[comparison.advance_comparison.eq("incorrect")].copy()
    if not (
        wrong.advance_state.eq("observed").all()
        and wrong.advance_abc.isin(["A", "B", "C"]).all()
        and wrong.official_advance_abc.isin(["A", "B", "C"]).all()
        and wrong.advance_abc.ne(wrong.official_advance_abc).all()
    ):
        raise ResearchError("e6_cito_unproved_correction")
    mapped = frame.package_number.map(wrong.set_index("package_number").official_advance_abc)
    mask = mapped.notna()
    original = frame.package_number.map(wrong.set_index("package_number").advance_abc)
    if not frame.loc[mask, "advance_abc"].eq(original[mask]).all():
        raise ResearchError("e6_cito_correction_source_mismatch")
    out = frame.copy()
    out.loc[mask, "advance_abc"] = mapped[mask]
    pd.testing.assert_frame_equal(
        frame.drop(columns="advance_abc"), out.drop(columns="advance_abc"), check_exact=True
    )
    if not out.loc[~mask, "advance_abc"].equals(frame.loc[~mask, "advance_abc"]):
        raise ResearchError("e6_cito_unrelated_code_changed")
    changes = frame.loc[mask, ["row_key", "package_number", "eligible", "advance_abc"]].copy()
    changes = changes.rename(columns={"advance_abc": "reported_code"})
    changes["corrected_code"] = mapped[mask].to_numpy()
    return out, changes.reset_index(drop=True)


def no_report_inputs(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["advance_abc"] = None
    out["advance_state"] = "e6_counterfactual_no_report"
    return out


def decisions(predictions: pd.DataFrame, actual: pd.DataFrame, scenario: str) -> pd.DataFrame:
    """Finish both queues using predictions alone, before attaching outcome columns."""
    out = predictions.copy().reset_index(drop=True)
    reference = align(out, actual)
    eligible = out.prediction_state.eq("available")
    if not eligible.equals(reference.prediction_state.eq("available")):
        raise ResearchError("e6_cito_eligibility_changed")
    out["selected_natural"] = out.selected.astype(bool)
    out["selected_matched"] = False
    count = int(reference.selected.sum())
    use = out.loc[eligible, ["row_key", "period_start", "p_fall", "p_unchanged", "p_rise"]]
    use = use.rename(columns={"p_fall": "p0", "p_unchanged": "p1", "p_rise": "p2"})
    workload = {str(day)[:10]: count for day in use.period_start.unique()}
    if len(workload) != 1:
        raise ResearchError("e6_cito_requires_one_origin")
    matched = select_queue(use, "rise_rank", workload=workload)
    out.loc[eligible, "selected_matched"] = matched.selected.to_numpy()
    if scenario == "actual":
        out["selected_matched"] = out.selected_natural
    out["actual_selected"] = reference.selected.astype(bool)
    out["actual_rank"] = pd.to_numeric(reference["rank"])
    out["actual_p_rise"] = reference.p_rise
    out["actual_p_fall"] = reference.p_fall
    out["rank_percentile_change"] = np.where(
        eligible, (pd.to_numeric(out["rank"]) - out.actual_rank) / int(eligible.sum()), np.nan
    )
    out["substantially_reranked"] = out.rank_percentile_change.abs().ge(0.20)
    difference = out[["p_fall", "p_unchanged", "p_rise"]].to_numpy(dtype=float) - reference[
        ["p_fall", "p_unchanged", "p_rise"]
    ].to_numpy(dtype=float)
    out["max_probability_change"] = np.where(eligible, np.abs(difference).max(axis=1), np.nan)
    out["price_prediction_change_dkk"] = out.next_aip_dkk - reference.next_aip_dkk
    out["prediction_changed"] = eligible & (
        out.max_probability_change.gt(0) | out.price_prediction_change_dkk.ne(0)
    )
    out["scenario"] = scenario
    for workload_name in ("natural", "matched"):
        chosen = out["selected_" + workload_name]
        out["added_" + workload_name] = chosen & ~out.actual_selected
        out["removed_" + workload_name] = ~chosen & out.actual_selected
    return out


def attach_labels(decision: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    labels = align(decision, outcomes)
    for name in ("package_number", "aip_ore", "eligible", "origin_identity_token"):
        if name in decision and name in labels:
            if not decision[name].equals(labels[name]):
                raise ResearchError("e6_cito_outcome_context_changed")
    out = decision.copy()
    for name in ("status", "next_aip_ore", "next_delta_ore", "next_direction", "target_period"):
        out["outcome_status" if name == "status" else "outcome_" + name] = labels[name].to_numpy()
    return out


def exposure(frame: pd.DataFrame) -> dict[str, Any]:
    known = frame.loc[frame.outcome_status.eq("observed")]
    delta = pd.to_numeric(known.outcome_next_delta_ore)
    return {
        "cases": len(frame),
        "observed": len(known),
        "rises": int(known.outcome_next_direction.eq(1).sum()),
        "unchanged": int(known.outcome_next_direction.eq(0).sum()),
        "falls": int(known.outcome_next_direction.eq(-1).sum()),
        "unknown": len(frame) - len(known),
        "unknown_reasons": frame.loc[~frame.outcome_status.eq("observed"), "outcome_status"]
        .value_counts()
        .to_dict(),
        "one_pack_price_exposure_dkk": float(delta.sum() / 100),
        "one_pack_upside_dkk": float(delta.clip(lower=0).sum() / 100),
        "one_pack_downside_dkk": float(-delta.clip(upper=0).sum() / 100),
    }


def summarize(frame: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_origins": len(frame),
        "predictions": int(frame.prediction_state.eq("available").sum()),
        "changed_predictions": int(frame.prediction_changed.sum()),
        "substantially_reranked": int(frame.substantially_reranked.sum()),
        "max_probability_change": float(frame.max_probability_change.max()),
        "max_absolute_price_prediction_change_dkk": float(
            frame.price_prediction_change_dkk.abs().max()
        ),
        "groups": {},
    }
    for group, mask in {
        "all_eligible": frame.prediction_state.eq("available"),
        "wrong_report_code": frame.report_comparison.eq("incorrect")
        & frame.prediction_state.eq("available"),
        "correct_report_code": frame.report_comparison.eq("correct")
        & frame.prediction_state.eq("available"),
        "missing_or_unusable_report_comparison": ~frame.report_comparison.isin(
            ["correct", "incorrect"]
        )
        & frame.prediction_state.eq("available"),
    }.items():
        part = frame.loc[mask]
        values: dict[str, Any] = {"population": exposure(part), "queues": {}}
        for workload in ("natural", "matched"):
            values["queues"][workload] = {
                key: exposure(part.loc[part[column]])
                for key, column in {
                    "selected": "selected_" + workload,
                    "added": "added_" + workload,
                    "removed": "removed_" + workload,
                    "retained": "retained_" + workload,
                }.items()
            }
        result["groups"][group] = values
    return result


def examples(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """First three row keys per disjoint consequence class; no outcome selection."""
    wrong = frame.report_comparison.eq("incorrect") & frame.prediction_state.eq("available")
    crossing = frame.added_natural | frame.removed_natural
    rows = []
    for name, mask in {
        "selection_crossing": wrong & crossing,
        "harmless_for_selection_and_large_rank": wrong & ~crossing & ~frame.substantially_reranked,
    }.items():
        chosen = frame.loc[mask].sort_values("row_key").head(3)
        columns = [
            "row_key",
            "package_number",
            "reported_code",
            "advance_abc",
            "p_rise",
            "p_fall",
            "actual_p_rise",
            "actual_p_fall",
            "max_probability_change",
            "actual_rank",
            "rank",
            "rank_percentile_change",
            "actual_selected",
            "selected_natural",
            "selected_matched",
            "outcome_status",
            "outcome_next_delta_ore",
            "origin_group",
        ]
        rows.append(
            {"group": name, "available": int(mask.sum()), "examples": json_records(chosen[columns])}
        )
    largest = (
        frame.loc[wrong]
        .sort_values(["max_probability_change", "row_key"], ascending=[False, True])
        .head(3)
    )
    rows.append(
        {
            "group": "largest_absolute_probability_changes",
            "available": int(wrong.sum()),
            "examples": json_records(largest[columns]),
        }
    )
    return rows


def consequence_decisions(
    results: dict[str, Any], workloads: tuple[str, ...] = ("natural", "matched")
) -> dict[str, Any]:
    """Apply the frozen five-percent descriptive rule; never choose a new policy."""
    baseline = results["actual"]["groups"]["all_eligible"]["queues"]
    decisions = {}
    for scenario in SCENARIOS[1:]:
        queues = results[scenario]["groups"]["all_eligible"]["queues"]
        values = {}
        for workload in workloads:
            reference = baseline[workload]["selected"]
            candidate = queues[workload]["selected"]
            additions = queues[workload]["added"]["cases"]
            removals = queues[workload]["removed"]["cases"]
            count = reference["cases"]
            turnover = (additions + removals) / count if count else None
            fractions = {}
            absolute = {}
            zero_baseline_change = False
            for metric in ("one_pack_price_exposure_dkk", "one_pack_downside_dkk"):
                difference = candidate[metric] - reference[metric]
                absolute[metric] = difference
                fractions[metric] = (
                    difference / abs(reference[metric]) if reference[metric] else None
                )
                zero_baseline_change |= bool(not reference[metric] and difference)
            values[workload] = {
                "membership_changed": bool(additions or removals),
                "membership_symmetric_difference_over_actual_count": turnover,
                "exposure_difference_dkk": absolute,
                "exposure_difference_fraction_of_absolute_actual": fractions,
                "relative_materiality_undefined_at_changed_zero_baseline": zero_baseline_change,
                "material_under_five_percent_rule": bool(
                    (turnover is not None and turnover >= 0.05)
                    or any(x is not None and abs(x) >= 0.05 for x in fractions.values())
                ),
            }
        decisions[scenario] = values
    return decisions


def _sources(root: Path, protocol: dict[str, Any]) -> dict[str, Any]:
    current_ref = protocol.get("pins", {}).get("e4_current")
    current_path = checked(current_ref) if current_ref else root.parent / "research-e4/current.json"
    current = read(current_path)
    checked(current["acceptance"])
    index = read(checked(current["index"]))
    entry = next(x for x in index["assessments"] if x["origin"] == "2026-10-12")
    assessment = read(checked(entry["assessment"]))
    outcome = read(checked(entry["outcome"]))
    comparison = read(checked(index["comparison"]))
    binding = assessment["binding"]
    sources = {
        "e4_current": pin(current_path),
        "e4_acceptance": current["acceptance"],
        "e4_index": current["index"],
        "assessment": entry["assessment"],
        "inputs": assessment["inputs"],
        "accepted_predictions": assessment["predictions"],
        "outcome": entry["outcome"],
        "outcome_rows": outcome["rows"],
        "comparison": index["comparison"],
        "comparison_rows": comparison["rows"],
        "report": comparison["report"],
        "models": binding["models"],
        "calibration": binding["calibration"],
    }
    bundle = read(checked(sources["models"]))
    sources["amount_interval_calibration"] = bundle["amount_interval_calibration"]
    for name, model in bundle["models"].items():
        if (
            model["latest_label_maturity"] >= model["fit_cutoff"]
            or model["fit_cutoff"] > "2026-10-12"
        ):
            raise ResearchError("e6_cito_model_chronology")
        sources[name + "_model"] = model["model"]
        sources[name + "_trial"] = model["trial"]
    calibration = read(checked(sources["calibration"]))["states"]["2026"]
    for state in calibration["temperature"].values():
        if state["last_maturity"] >= state["cutoff"] or state["cutoff"] > "2026-10-12":
            raise ResearchError("e6_cito_calibration_chronology")
    for ref in sources.values():
        checked(ref)
    return sources


def complete_matched(root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    """Add a same-rule paired diagnostic; preserve every initial anchored output."""
    destination = root / "cito"
    prior = read(destination / "summary.json")
    preservation = read(destination / "code-preservation.json")
    original_code = checked(preservation["preserved"])
    if pin(original_code)["sha256"] != prior["binding"]["producer"]["sha256"]:
        raise ResearchError("e6_cito_initial_producer_not_preserved")
    clarification_path = destination / "matched-clarification.json"
    clarification = read(clarification_path)
    if (
        checked(clarification["initial_summary"]).resolve()
        != (destination / "summary.json").resolve()
    ):
        raise ResearchError("e6_cito_clarification_summary_changed")
    if (
        prior["binding"]["sources"] != binding["sources"]
        or prior["binding"]["protocol"] != binding["protocol"]
    ):
        raise ResearchError("e6_cito_clarification_sources_changed")
    for reference in prior["files"].values():
        checked(reference)
    frames = {
        scenario: pd.read_parquet(checked(prior["files"][scenario + "_predictions"]))
        for scenario in SCENARIOS
    }
    actual = frames["actual"]
    eligible = actual.prediction_state.eq("available")
    count = int(actual.selected_natural.sum())
    selected_actual = pd.Series(False, index=actual.index)
    results = {}
    files = dict(prior["files"])
    outcomes = pd.read_parquet(checked(binding["sources"]["outcome_rows"]))
    for scenario, saved in frames.items():
        rows = align(actual, saved)
        use = rows.loc[eligible, ["row_key", "period_start", "p_fall", "p_unchanged", "p_rise"]]
        use = use.rename(columns={"p_fall": "p0", "p_unchanged": "p1", "p_rise": "p2"})
        workload = {str(day)[:10]: count for day in use.period_start.unique()}
        chosen = select_queue(use, "rise_rank", workload=workload)
        rows["selected_matched_same_rule"] = False
        rows.loc[eligible, "selected_matched_same_rule"] = chosen.selected.to_numpy()
        if scenario == "actual":
            selected_actual = rows.selected_matched_same_rule.copy()
        rows["actual_selected_matched_same_rule"] = selected_actual.to_numpy()
        chosen_now = rows.selected_matched_same_rule
        rows["added_matched_same_rule"] = chosen_now & ~selected_actual
        rows["removed_matched_same_rule"] = ~chosen_now & selected_actual
        rows["retained_matched_same_rule"] = chosen_now & selected_actual
        scored = attach_labels(rows, outcomes)
        path = destination / f"clean-matched-{scenario}.parquet"
        scored.to_parquet(path, index=False)
        files["clean_matched_" + scenario] = pin(path)
        for role in ("selected", "added", "removed", "retained"):
            scored[role + "_matched"] = scored[role + "_matched_same_rule"]
        value = summarize(scored)
        for group in value["groups"].values():
            del group["queues"]["natural"]
        results[scenario] = value
    result = copy.deepcopy(prior)
    files["initial_summary"] = pin(destination / "summary.json")
    files["initial_code_preservation"] = pin(destination / "code-preservation.json")
    files["initial_producer"] = preservation["preserved"]
    files["matched_clarification"] = pin(clarification_path)
    result.update(
        version="e6-cito-consequence-v2",
        created_utc=utc(),
        binding=binding,
        files=files,
        clean_matched={
            "policy": (
                "All arms top rise-probability at actual threshold workload; "
                "same accepted deterministic ties, no threshold gate. Expansion diagnostic."
            ),
            "results": results,
            "descriptive_decisions": consequence_decisions(results, ("matched",)),
        },
        anchored_matched_interpretation=(
            "Original matched fields compare actual threshold control with counterfactual "
            "unconstrained rise rank. Their differences mix queue rule and information; "
            "they cannot establish a code-correction benefit. Use natural or clean_matched."
        ),
    )
    frozen_json(destination / "summary-v2.json", result)
    return result


def run(root: Path) -> dict[str, Any]:
    protocol = require(root)
    source = _sources(root, protocol)
    destination = root / "cito"
    result_path = destination / "summary.json"
    binding = {
        "protocol": pin(root / "protocol.json"),
        "sources": source,
        "producer": pin(Path(__file__)),
    }
    final_path = destination / "summary-v2.json"
    if final_path.exists():
        final = read(final_path)
        if final["binding"] != binding:
            raise ResearchError("e6_cito_retry_binding_changed")
        for ref in final["files"].values():
            checked(ref)
        return final
    if result_path.exists():
        prior = read(result_path)
        if prior["binding"] != binding:
            return complete_matched(root, binding)
        for ref in prior["files"].values():
            checked(ref)
        return prior
    destination.mkdir(parents=True, exist_ok=True)
    inputs = pd.read_parquet(checked(source["inputs"]))
    actual = pd.read_parquet(checked(source["accepted_predictions"]))
    comparison = pd.read_parquet(checked(source["comparison_rows"]))
    outcomes = pd.read_parquet(checked(source["outcome_rows"]))
    corrected, changes = corrected_inputs(inputs, comparison)
    contract = {
        "version": "e6-cito-wrong-code-correction-v1",
        "inputs": source["inputs"],
        "comparison": source["comparison_rows"],
        "rule": (
            "Replace only independently incorrect comparable A/B/C codes; all other cells unchanged"
        ),
        "report_wrong_codes": int(comparison.advance_comparison.eq("incorrect").sum()),
        "origin_wrong_codes": len(changes),
        "eligible_wrong_codes": int(changes.eligible.sum()),
        "changes": json_records(changes),
    }
    frozen_json(destination / "correction-contract.json", contract)
    corrected.to_parquet(destination / "corrected-inputs.parquet", index=False)
    no_report = no_report_inputs(inputs)
    no_report.to_parquet(destination / "no-report-inputs.parquet", index=False)
    current_calibration = copy.deepcopy(read(checked(source["calibration"])))
    states = current_calibration["states"]["2026"]["temperature"]
    states["hybrid"] = copy.deepcopy(states["current"])
    current_cal_path = destination / "calibration-current.json"
    frozen_json(current_cal_path, current_calibration)
    files = {
        name: pin(destination / name)
        for name in (
            "correction-contract.json",
            "corrected-inputs.parquet",
            "no-report-inputs.parquet",
            "calibration-current.json",
        )
    }
    results = {}
    cases = []
    for scenario in SCENARIOS:
        feature = (
            inputs
            if scenario == "actual"
            else corrected
            if scenario == "corrected_wrong_only"
            else no_report
        )
        cal_path = (
            current_cal_path if scenario == "no_report_current" else checked(source["calibration"])
        )
        predicted = predict(feature, checked(source["models"]), cal_path)
        if scenario == "actual":
            expected = align(predicted, actual)
            pd.testing.assert_frame_equal(
                predicted[REPLAY_COLUMNS],
                expected[REPLAY_COLUMNS],
                check_exact=True,
                check_dtype=False,
            )
        rows = decisions(predicted, actual, scenario)
        comparison_state = comparison.set_index("package_number").advance_comparison
        rows["report_comparison"] = rows.package_number.map(comparison_state).fillna(
            "report_absent"
        )
        rows["reported_code"] = inputs.advance_abc.to_numpy()
        for workload in ("natural", "matched"):
            rows["retained_" + workload] = rows["selected_" + workload] & rows.actual_selected
        # Freeze all decisions before a separately pinned official outcome is read into them.
        prediction_path = destination / f"{scenario}-predictions.parquet"
        rows.to_parquet(prediction_path, index=False)
        files[scenario + "_predictions"] = pin(prediction_path)
        scored = attach_labels(rows, outcomes)
        scored_path = destination / f"{scenario}-outcomes.parquet"
        scored.to_parquet(scored_path, index=False)
        files[scenario + "_outcomes"] = pin(scored_path)
        results[scenario] = summarize(scored)
        clusters = []
        grouped = scored.assign(
            cluster=scored.origin_group.where(
                scored.origin_group.notna(), "ungrouped:" + scored.row_key
            )
        )
        for cluster, part in grouped.groupby("cluster", sort=True):
            for workload in ("natural", "matched"):
                selected_exposure = exposure(part.loc[part["selected_" + workload]])
                selected_exposure["unknown_reasons"] = json.dumps(
                    selected_exposure["unknown_reasons"], sort_keys=True
                )
                clusters.append(
                    {
                        "scenario": scenario,
                        "cluster": cluster,
                        "origin_group": part.origin_group.iloc[0],
                        "workload": workload,
                        "wrong_codes": int(part.report_comparison.eq("incorrect").sum()),
                        "added": int(part["added_" + workload].sum()),
                        "removed": int(part["removed_" + workload].sum()),
                        **selected_exposure,
                    }
                )
        cluster_path = destination / f"{scenario}-group-clusters.parquet"
        pd.DataFrame(clusters).to_parquet(cluster_path, index=False)
        files[scenario + "_group_clusters"] = pin(cluster_path)
        if scenario == "corrected_wrong_only":
            cases = examples(scored)
    frozen_json(
        destination / "examples.json",
        {
            "selection": "first three row keys per disjoint class, no outcome ordering",
            "groups": cases,
        },
    )
    text = [
        "# Cito wrong-code consequences",
        "",
        "Retrospective one-report evidence. Examples use first row keys per consequence class "
        "and largest probability changes; outcomes did not select examples. "
        "Amounts are one-pack AIP exposure, not savings or profit.",
        "",
    ]
    for group in cases:
        text.extend([f"## {group['group']} ({group['available']} cases)", ""])
        for case in group["examples"]:
            text.append(
                f"- Package {case['package_number']} (group {case['origin_group']}): "
                f"code {case['reported_code']} → {case['advance_abc']}; "
                f"rise probability {case['actual_p_rise']:.4f} → {case['p_rise']:.4f}; "
                f"rank {case['actual_rank']} → {case['rank']}; "
                f"selected {case['actual_selected']} → {case['selected_natural']}; "
                f"outcome {case['outcome_status']}; "
                f"observed change øre {case['outcome_next_delta_ore']}."
            )
        if not group["examples"]:
            text.append("No case in this predeclared class.")
        text.append("")
    (destination / "examples.md").write_text("\n".join(text), encoding="utf-8")
    files["examples"] = pin(destination / "examples.json")
    files["readable_examples"] = pin(destination / "examples.md")
    result = {
        "version": "e6-cito-consequence-v1",
        "created_utc": utc(),
        "binding": binding,
        "actual_exact_replay": True,
        "correction": contract,
        "scenario_calibration": {
            "actual": "E4 hybrid",
            "corrected_wrong_only": "same E4 hybrid",
            "no_report_current": "E4 current",
            "no_report_hybrid_temperature": "E4 hybrid; calibration-isolation diagnostic",
        },
        "matched_policy": (
            "Actual accepted threshold selection retained; other arms top rise-probability "
            "at identical actual count with accepted deterministic tie rule"
        ),
        "substantial_rank_change": "Absolute rank difference / eligible origins >=.20",
        "claim": (
            "One retrospective report; exact AIP price exposure is not realised savings, "
            "profit, or general advance-code reliability"
        ),
        "results": results,
        "descriptive_decisions": consequence_decisions(results),
        "files": files,
    }
    frozen_json(result_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.root)
    print(f"E6 Cito consequences complete; exact actual replay={result['actual_exact_replay']}")


if __name__ == "__main__":
    main()
