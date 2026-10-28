"""Compact saved E5 comparisons, financial scenarios and candidate review."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e4_common import checked, frozen_json, read, utc
from .e5_finance import allocate, compare_group_scenarios, price_exposure, queue_policies
from .e5_protocol import require
from .e5_results import finance_frame, json_records, score_fold
from .group_features import pin


def financial_summary(rows: pd.DataFrame) -> list[dict[str, Any]]:
    result = []
    keys = ["comparison", "policy", "workload", "allocation"]
    for key, part in rows.groupby(keys, sort=True):
        sums = {
            x: float(part[x].sum())
            for x in (
                "selected",
                "observed_selected",
                "unknown_selected",
                "quantity",
                "unknown_quantity",
                "funded_selected",
                "funded_unknown",
                "observed_funded_quantity",
                "allocated_ore",
                "observed_acquisition_basis_ore",
                "observed_price_exposure_ore",
                "observed_positive_exposure_ore",
                "observed_negative_exposure_ore",
                "observed_rises",
                "observed_falls",
            )
            if x in part
        }
        observed = sums["observed_selected"]
        funded_quantity = sums.get("observed_funded_quantity", 0)
        out = dict(zip(keys, key, strict=True)) | {
            "information": "conditional next-ABC hybrid with current-only fallback",
            "periods": int(part.period_start.nunique()),
            **sums,
        }
        out.update(
            {
                "rise_rate_observed": sums["observed_rises"] / observed if observed else None,
                "fall_rate_observed": sums["observed_falls"] / observed if observed else None,
                "unknown_fraction_selected": sums["unknown_selected"] / sums["selected"]
                if sums["selected"]
                else None,
                "observed_price_exposure_dkk": sums["observed_price_exposure_ore"] / 100,
                "observed_upside_dkk": sums.get("observed_positive_exposure_ore", 0) / 100,
                "observed_downside_dkk": -sums.get("observed_negative_exposure_ore", 0) / 100,
                "observed_net_dkk_per_funded_pack": sums["observed_price_exposure_ore"]
                / (100 * funded_quantity)
                if funded_quantity
                else None,
                "observed_return_on_known_acquisition": sums["observed_price_exposure_ore"]
                / sums.get("observed_acquisition_basis_ore", 0)
                if sums.get("observed_acquisition_basis_ore", 0)
                else None,
                "allocated_dkk": sums["allocated_ore"] / 100,
                "mean_acquisition_price_per_allocated_pack_dkk": sums["allocated_ore"]
                / (100 * sums["quantity"])
                if sums["quantity"]
                else None,
                "unallocated_capital_dkk": float(part.unallocated_capital_ore.sum() / 100)
                if key[-1] == "fixed_capital"
                else None,
                "worst_period_price_exposure_dkk": float(
                    part.observed_price_exposure_ore.min() / 100
                ),
                "maximum_package_budget_share": float(part.max_package_budget_share.max())
                if key[-1] == "fixed_capital" and part.max_package_budget_share.notna().any()
                else None,
                "mean_top10_allocated_share": float(part.top10_allocated_capital_share.mean())
                if part.top10_allocated_capital_share.notna().any()
                else None,
            }
        )
        result.append(out)
    return result


def scenario_summary(rows: pd.DataFrame) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if rows.empty:
        return result
    for key, part in rows.groupby(["name", "allocation", "workload", "policy"], sort=True):
        out: dict[str, Any] = dict(
            zip(["scenario", "allocation", "workload", "policy"], key, strict=True)
        )
        out.update(
            {
                "scored_group_episodes": len(part),
                "periods": int(part.period_start.nunique()),
                "extra_packs": int(part.extra_quantity.sum()),
                "incremental_unmet": int(part.incremental_unmet.sum()),
            }
        )
        for metric in (
            "economic_cost_proxy",
            "net_procurement_cash",
            "ending_market",
            "capital_cost",
            "compensation",
            "return_credit",
            "writeoff",
        ):
            out["incremental_" + metric + "_dkk"] = float(
                part["incremental_" + metric + "_dkk"].sum()
            )
        period = part.groupby("period_start").incremental_economic_cost_proxy_dkk.sum()
        out["worst_period_incremental_cost_dkk"] = float(period.max())
        out["break_even_total_extra_fee_dkk"] = max(
            0.0, -out["incremental_economic_cost_proxy_dkk"]
        )
        result.append(out)
    return result


def finance(root: Path) -> dict[str, Any]:
    path = root / "financial.json"
    if path.exists():
        for p in read(path)["files"].values():
            checked(p)
        return read(path)
    protocol = require(root)
    choice = read(root / "selection.json")
    method = "fresh" if choice["direction"]["method"] == "unchanged" else "tuned"
    population_report = read(root / "population/report.json")
    pop = pd.read_parquet(root / "population/population.parquet")
    safe_columns = [
        x
        for x in pop
        if x
        in {
            "row_key",
            "identity_excluded",
            "identity_reason",
            "lagged_reason",
            "behavior_selected_excluded",
            "prior_median_premium",
            "prior_change_frequency",
            "prior_flat_run",
        }
        or str(x).endswith(("_name", "_family", "_code"))
    ]
    pop = pop[safe_columns]
    if pop.row_key.duplicated().any():
        raise ValueError("Duplicate population policy keys")
    e4current = read(checked(protocol["pins"]["e4_current"]))
    e4index = read(checked(e4current["index"]))
    scenarios = read(checked(e4index["scenarios"]))
    # Accepted E4's origin-only sample is reused unchanged, restricted by E5 origin dates.
    sample_ref = next(
        x for x in scenarios["artifacts"] if Path(x["path"]).name == "scenario-population.json"
    )
    sample = read(checked(sample_ref))["groups"]
    sampled_keys = {member["row_key"] for group in sample for member in group["members"]}
    exposures, reviews, outcome_tables, scenario_rows, unknown_rows = [], [], [], [], []
    for fold in ("spring", "summer"):
        result = score_fold(root, fold)
        p = pd.read_parquet(checked(result["files"]["predictions.parquet"]))
        o = pd.read_parquet(checked(result["files"]["outcomes.parquet"]))
        labels = o[
            [
                x
                for x in o
                if x
                in {
                    "row_key",
                    "package_number",
                    "period_start",
                    "next_status",
                    "next_aip_ore",
                    "next_delta_ore",
                    "next_direction",
                    "second_status",
                    "second_aip_ore",
                    "persistence_target",
                }
            ]
        ].copy()
        labels["package_id"] = labels.package_number
        outcome_tables.append(labels)
        base = finance_frame(p, method)
        base = base.merge(pop, on="row_key", how="left", validate="one_to_one", sort=False)
        base["recommendation_eligible"] = ~base.behavior_selected_excluded.fillna(False)
        base["eligibility_reason"] = np.where(
            base.behavior_selected_excluded.fillna(False),
            "Flagged by lagged policy; original E4 comparison population preserved",
            "Eligible; full market peers retained",
        )
        base["information_route"] = np.where(
            base.advance_abc.isin(["A", "B", "C"]),
            "conditional advance ABC proxy",
            "genuine current-only fallback",
        )
        base["fold"] = fold
        for day, frame in base.groupby("period_start", sort=True):
            day_outcomes = labels.loc[labels.period_start.eq(day)]
            # Separate changed ranking from changed direction and calibration.
            for comparison in ("frozen_E4_reference", "same_E5_probabilities"):
                f = frame.copy().reset_index(drop=True)
                if comparison == "same_E5_probabilities":
                    f["reference_p_rise"], f["reference_p_fall"] = f.p_rise, f.p_fall
                q = queue_policies(f, include_persistence=choice["persistence_policy_enabled"])
                day_allocations = []
                for mode in ("per_pack", "four_pack", "fixed_capital"):
                    allocation = allocate(q, mode=mode)
                    e = price_exposure(allocation, day_outcomes, queues=q, allocation=mode)
                    exposures.append(e.assign(comparison=comparison, fold=fold))
                    if comparison == "frozen_E4_reference":
                        day_allocations.append(allocation)
                if comparison == "frozen_E4_reference":
                    review = f.copy()
                    for policy in q.policy.unique():
                        selected = q.loc[q.policy.eq(policy) & q.workload.eq("natural")].set_index(
                            "row_key"
                        )
                        review[policy + "_selected"] = review.row_key.map(selected.selected)
                        review[policy + "_rank"] = review.row_key.map(selected["rank"])
                    review["selected"] = review.probability_reference_selected
                    review["rank"] = review.probability_reference_rank
                    review["selection_reason"] = (
                        "Saved E4 probability reference; compare fixed monetary rankings"
                    )
                    reviews.append(review)
                    origin_sample = f.loc[f.row_key.isin(sampled_keys)]
                    allocation_sample = pd.concat(day_allocations, ignore_index=True)
                    if len(origin_sample):
                        scenario, unknown = compare_group_scenarios(
                            origin_sample,
                            allocation_sample,
                            day_outcomes,
                            variants=pd.concat(
                                [
                                    q[["workload", "policy"]]
                                    .drop_duplicates()
                                    .assign(allocation=mode)
                                    for mode in ("per_pack", "four_pack", "fixed_capital")
                                ],
                                ignore_index=True,
                            ),
                        )
                        if len(scenario):
                            scenario_rows.append(scenario)
                        if len(unknown):
                            unknown_rows.append(unknown)
    all_exposure = pd.concat(exposures, ignore_index=True)
    all_scenarios = pd.concat(scenario_rows, ignore_index=True) if scenario_rows else pd.DataFrame()
    all_unknown = pd.concat(unknown_rows, ignore_index=True) if unknown_rows else pd.DataFrame()
    pd.concat(reviews, ignore_index=True).to_parquet(
        root / "historical-review.parquet", index=False
    )
    pd.concat(outcome_tables, ignore_index=True).to_parquet(
        root / "historical-outcomes.parquet", index=False
    )
    all_exposure.to_parquet(root / "financial-periods.parquet", index=False)
    # Exact fractional-ore identities remain JSON strings rather than rounded reconstruction.
    frozen_json(
        root / "scenario-details.json",
        {"rows": json_records(all_scenarios), "unknown": json_records(all_unknown)},
    )
    result = {
        "version": "e5-financial-v1",
        "protocol": pin(root / "protocol.json"),
        "created_utc": utc(),
        "population_policy": pin(root / "population/report.json"),
        "selection": pin(root / "selection.json"),
        "population_choice": population_report.get("behavior_selection"),
        "ranking_population": (
            "Full supported origin population; exclusions evaluated separately to "
            "avoid attributing easier cases to modelling."
        ),
        "reference_sample": sample_ref,
        "financial": financial_summary(all_exposure),
        "scenarios": scenario_summary(all_scenarios),
        "scenario_coverage": {
            "scored_rows": len(all_scenarios),
            "unknown_group_episodes": len(all_unknown),
            "unknown_reasons": all_unknown.reason.value_counts().to_dict()
            if len(all_unknown)
            else {},
        },
        "files": {
            x: pin(root / x)
            for x in (
                "historical-review.parquet",
                "historical-outcomes.parquet",
                "financial-periods.parquet",
                "scenario-details.json",
            )
        },
    }
    frozen_json(path, result)
    return result


def compile_results(root: Path) -> dict[str, Any]:
    path = root / "results.json"
    if path.exists():
        return read(path)
    choice = read(root / "selection.json")
    folds = [
        read(root / f"evaluations/{name}/result.json") for name in ("screen", "spring", "summer")
    ]
    financial = finance(root)
    outer_intervals = [
        x
        for f in folds[1:]
        for x in f["intervals"]
        if x["method"] == "quantile" and "coverage90" in x
    ]
    validation = {}
    for information in ("current", "hybrid"):
        rows = [x for x in outer_intervals if x["information"] == information]
        overall = [x for x in rows if x["population"] == "all"]
        if not overall or not sum(x["observed"] for x in overall):
            raise ResearchError("e5_quantile_validation_missing_supported_information_mode")
        if len(overall) != 2 or {x["fold"] for x in overall} != {"spring", "summer"}:
            raise ResearchError("e5_quantile_validation_incomplete_outer_modes")
        pooled = sum(x["coverage90"] * x["observed"] for x in overall) / sum(
            x["observed"] for x in overall
        )
        weakest_mode = min(x["coverage90"] for x in rows if x["observed"])
        validation[information] = {
            "pooled_coverage": pooled,
            "weakest_fold_subgroup_coverage": weakest_mode,
            "observed": sum(x["observed"] for x in overall),
            "accepted": pooled >= 0.85 and weakest_mode >= 0.70,
        }
    coverage = validation["hybrid"]["pooled_coverage"]
    current_coverage = validation["current"]["pooled_coverage"]
    weakest = min(x["weakest_fold_subgroup_coverage"] for x in validation.values())
    accepted = all(x["accepted"] for x in validation.values())
    outer_direction = [
        x for f in folds[1:] for x in f["direction"] if x["population"] == "full_routes_calibrated"
    ]

    def direction_score(method: str, information: str = "hybrid") -> float:
        rows = [
            x for x in outer_direction if x["method"] == method and x["information"] == information
        ]
        if not rows or any(not x.get("observed_periods") for x in rows):
            raise ResearchError("e5_outer_direction_requires_mature_observations")
        return sum(x["equal_period_brier"] * x["observed_periods"] for x in rows) / sum(
            x["observed_periods"] for x in rows
        )

    def change_word(before: float, after: float) -> str:
        return "better" if after < before else "worse" if after > before else "unchanged"

    baseline, fresh = direction_score("frozen"), direction_score("fresh")
    conclusions = [
        "Historical monetary rankings below use conditional next-period ABC, with current-only "
        "fallback. This does not establish early historical access. The separate current "
        "assessment has no matching advance report and uses current-only forecasts.",
        f"Fresher training: equal-period Brier changed from {baseline:.4f} to {fresh:.4f} "
        f"under common earlier calibration ({change_word(baseline, fresh)}; "
        "lower is better). "
        f"The original saved E4 probability reference scored {direction_score('accepted_e4'):.4f}.",
    ]
    if choice["direction"]["method"] == "unchanged":
        conclusions.append(
            "Tuning: no complete tuning candidate passed the frozen development rule; "
            "the selected direction settings remain unchanged. "
            "Resource-limited trials are retained."
        )
    else:
        tuned = direction_score("tuned")
        conclusions.append(
            f"Tuning beyond freshness: Brier changed from {fresh:.4f} to {tuned:.4f} "
            f"({change_word(fresh, tuned)}) on the same exposed outer periods."
        )
    current_method = "fresh" if choice["direction"]["method"] == "unchanged" else "tuned"
    conclusions.append(
        f"Current-only Brier: frozen models {direction_score('frozen', 'current'):.4f}; "
        f"refreshed unchanged settings {direction_score('fresh', 'current'):.4f}; "
        f"selected research settings {direction_score(current_method, 'current'):.4f}. "
        "These comparisons also use common earlier calibration."
    )
    outer_amount = [
        x
        for f in folds[1:]
        for x in f["amount"]
        if x["information"] == "hybrid" and x["population"] == "all"
    ]
    for method, title in (("mae", "Refreshed median forecast"), ("rmse", "Mean-oriented forecast")):
        rows = [x for x in outer_amount if x["method"] == method]
        n = sum(x["observed"] for x in rows)
        mae = sum(x["mae_dkk"] * x["observed"] for x in rows) / n
        rmse = np.sqrt(sum(x["rmse_dkk"] ** 2 * x["observed"] for x in rows) / n)
        bias = sum(x["bias_dkk"] * x["observed"] for x in rows) / n
        conclusions.append(
            f"{title}: MAE DKK {mae:.2f}, RMSE DKK {rmse:.2f}, signed forecast error "
            f"DKK {bias:+.2f} per pack over {n:,} observed outcomes. These are price forecasts."
        )
    conclusions.append(
        f"Nominal 90% quantile coverage was {coverage:.1%} with conditional advance ABC "
        f"and {current_coverage:.1%} with current-only information; "
        f"the weakest declared fold/subgroup across these modes covered {weakest:.1%}. "
        + (
            "They passed the frozen review threshold."
            if accepted
            else "They failed the frozen threshold; E4 intervals remain the review reference."
        )
    )
    financial_rows = financial["financial"]
    for allocation, title in (
        ("per_pack", "One pack per selected package"),
        ("fixed_capital", "DKK 100,000 per origin with integer packs and caps"),
    ):
        rows = [
            x
            for x in financial_rows
            if x["comparison"] == "same_E5_probabilities"
            and x["workload"] == "matched"
            and x["allocation"] == allocation
        ]
        parts = []
        for method, name in (
            ("probability_reference", "probability"),
            ("amount_dkk", "DKK opportunity"),
            ("downside_aware", "downside-aware"),
        ):
            item = next(x for x in rows if x["policy"] == method)
            parts.append(
                f"{name}: DKK {item['observed_price_exposure_dkk']:,.0f} "
                "observed net price movement, "
                f"DKK {item['observed_downside_dkk']:,.0f} downside; "
                f"{int(item['unknown_selected']):,}/{int(item['selected']):,} "
                "selected outcomes unknown"
            )
        conclusions.append(
            title + ". Matched workload, same E5 probabilities. " + "; ".join(parts) + "."
        )
    conclusions.extend(
        [
            "Persistence policy "
            + ("passed" if choice["persistence_policy_enabled"] else "did not pass")
            + " its frozen development gate. Its joint-event probability is not expected profit.",
            "No blanket company ban is supported. The lagged behaviour flag has only a tiny "
            "historical benefit; full-population model and ranking comparisons remain "
            "separate from exclusions.",
            "Frozen E4 and its 287 November selections remain unchanged.",
            "Scenario financial effects depend on assumed demand, receipt, expiry and credits; "
            "they are not pharmacy profit.",
        ]
    )
    result = {
        "version": "e5-results-v1",
        "status": "scientific_evaluation_complete_delivery_record_separate",
        "created_utc": utc(),
        "exposure": (
            "Exposed retrospective development and rolling evaluation; no "
            "independent prospective confirmation."
        ),
        **{
            key: [x for f in folds for x in f[key]]
            for key in ("direction", "amount", "intervals", "persistence")
        },
        "financial": financial["financial"],
        "scenarios": financial["scenarios"],
        "scenario_coverage": financial["scenario_coverage"],
        "selection": choice,
        "selected_configuration": {
            "direction": choice["direction"],
            "amount": "RMSE mean-oriented research ranking; accepted MAE median kept separately",
            "quantile_interval_accepted": bool(accepted),
            "quantile_outer_coverage": coverage,
            "quantile_current_only_outer_coverage": current_coverage,
            "quantile_weakest_subgroup_coverage": weakest,
            "quantile_validation_by_information": validation,
            "persistence_policy_enabled": choice["persistence_policy_enabled"],
            "operational_promotion": False,
            "final_fitting_cutoff": "2026-09-01",
            "reused_route_cutoff": "2026-01-01",
        },
        "conclusions": conclusions,
        "limitations": [
            (
                "No pharmacy sales, inventory, expiry or verified commercial "
                "terms; Nomeco meeting pending."
            ),
            (
                "Conditional advance ABC proxy does not establish historical "
                "early access or recurring Cito accuracy."
            ),
            (
                "Latest October historical labels preserve accepted E4 comparison "
                "snapshot, including pending/unknown outcomes."
            ),
            (
                "Broader direction refreshed; equal/package route components "
                "remain at their explicitly pinned January cutoff."
            ),
            (
                "RMSE is mean-oriented, Huber is robust, MAE is median-oriented; "
                "quantiles are not expected returns."
            ),
            "No unsupported purchase quantities, orders, scheduling, promotion or deployment.",
        ],
    }
    frozen_json(path, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["finance", "results"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "finance":
        finance(args.root.resolve())
    else:
        compile_results(args.root.resolve())
