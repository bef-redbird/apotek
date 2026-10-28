"""E6 A: strictly current-only ranking and integer price-exposure diagnostics.

Saved forecasts enter through an explicit allowlist. Outcomes attach only after
queues and quantities are frozen. No model fitting or operating-store access.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e4_common import checked, frozen_json, read
from .e5_finance import allocate, price_exposure, queue_policies
from .e6_protocol import require
from .group_features import pin

KEYS = ["period_start", "package_id"]
VARIANTS = ["fold", "policy", "workload", "allocation"]
ORIGIN_FIELDS = [
    "row_key",
    "package_number",
    "period_start",
    "aip_ore",
    "eligible",
    "origin_group",
    "pack_size_token",
    "pack_unit",
    "peer_count",
    "peer_feature_state",
    "history_periods",
    "abc",
    "package_distributor",
]


def align(frame: pd.DataFrame, saved: pd.DataFrame) -> pd.DataFrame:
    if any(
        "row_key" not in value or value.row_key.isna().any() or value.row_key.duplicated().any()
        for value in (frame, saved)
    ):
        raise ResearchError("e6_a_invalid_row_identity")
    if not frame.row_key.isin(saved.row_key).all():
        raise ResearchError("e6_a_missing_saved_prediction_keys")
    return saved.set_index("row_key").reindex(frame.row_key).reset_index()


def current_only_frame(
    predictions: pd.DataFrame,
    current_rmse: pd.DataFrame,
    current_quantile: pd.DataFrame,
    population: pd.DataFrame,
) -> pd.DataFrame:
    """Never accept saved hybrid monetary fields or reference probabilities."""
    required = [
        "row_key",
        "package_number",
        "period_start",
        "aip_ore",
        "eligible",
        "tuned_current_p0",
        "tuned_current_p1",
        "tuned_current_p2",
    ]
    if any(column not in predictions for column in required):
        raise ResearchError("e6_a_missing_current_only_columns")
    base = predictions.reset_index(drop=True)
    amount, quantile, policy = (
        align(base, source) for source in (current_rmse, current_quantile, population)
    )
    if "behavior50_excluded" not in policy or policy.behavior50_excluded.isna().any():
        raise ResearchError("e6_a_missing_dated_recommendation_mask")
    if not policy.behavior50_excluded.map(lambda value: isinstance(value, (bool, np.bool_))).all():
        raise ResearchError("e6_a_invalid_recommendation_mask")
    out = base[[c for c in ORIGIN_FIELDS if c in base]].copy()
    out["package_id"] = out.package_number
    out["origin_eligible"] = out.eligible
    out["behavior50_excluded"] = policy.behavior50_excluded.to_numpy()
    out["eligible"] = out.origin_eligible & ~out.behavior50_excluded
    out["p_fall"] = base.tuned_current_p0
    out["p_unchanged"] = base.tuned_current_p1
    out["p_rise"] = base.tuned_current_p2
    probabilities = out[["p_fall", "p_unchanged", "p_rise"]].to_numpy(dtype=float)
    if not (
        np.isfinite(probabilities).all()
        and (probabilities >= 0).all()
        and (probabilities <= 1).all()
        and np.allclose(probabilities.sum(axis=1), 1, atol=1e-10, rtol=0)
    ):
        raise ResearchError("e6_a_invalid_current_only_probabilities")
    out["forecast_delta_dkk"] = amount.next_price_dkk.to_numpy() - out.aip_ore / 100
    out["lower_delta_dkk"] = quantile.q05_price_dkk.to_numpy() - out.aip_ore / 100
    out["information"] = "current_only"
    return out


def original_e4_frame(frame: pd.DataFrame, saved: pd.DataFrame) -> pd.DataFrame:
    original = align(frame, saved)
    out = frame.copy()
    out["eligible"] = out.origin_eligible
    out["p_fall"] = original.perfect_current_p0.to_numpy()
    out["p_rise"] = original.perfect_current_p2.to_numpy()
    out["p_unchanged"] = original.perfect_current_p1.to_numpy()
    # E4 is a probability-policy coverage reference, without E5 amount forecasts.
    out[["forecast_delta_dkk", "lower_delta_dkk"]] = np.nan
    return out


def make_queues(frame: pd.DataFrame, e4: pd.DataFrame) -> pd.DataFrame:
    main = queue_policies(frame)
    main["comparison_scope"] = "same_e5_current_probabilities_and_behavior50_population"
    original = queue_policies(original_e4_frame(frame, e4))
    original = original.loc[
        original.policy.eq("probability_reference") & original.workload.eq("natural")
    ].copy()
    original["policy"] = "original_e4_current"
    original["comparison_scope"] = "original_e4_current_unfiltered_coverage_reference"
    queues = pd.concat([main, original], ignore_index=True)
    queues["matched_expansion"] = (
        queues.workload.eq("matched")
        & queues.selected
        & (
            queues.policy_probability_rise.lt(0.35)
            | queues.policy_probability_fall.gt(0.10)
            | queues.score.le(0)
        )
    )
    queues["diagnostic_only"] = queues.workload.eq("matched")
    return queues


def joined_allocations(allocations: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    labels = outcomes.copy()
    if "package_id" not in labels:
        labels["package_id"] = labels.package_number
    labels["period_start"] = pd.to_datetime(labels.period_start)
    if labels.duplicated(KEYS).any():
        raise ResearchError("e6_a_duplicate_outcome_identity")
    joined = allocations.merge(
        labels[[*KEYS, "next_status", "next_aip_ore"]], on=KEYS, how="left", validate="many_to_one"
    )
    next_price = pd.to_numeric(joined.next_aip_ore, errors="coerce")
    if (
        next_price.notna() & (~np.isfinite(next_price) | next_price.ne(np.floor(next_price)))
    ).any():
        raise ResearchError("e6_a_nonexact_outcome_money")
    joined["outcome_observed"] = joined.next_status.eq("observed") & next_price.gt(0)
    joined["unknown_reason"] = joined.next_status.fillna("missing_outcome_identity")
    joined.loc[joined.next_status.eq("observed") & ~joined.outcome_observed, "unknown_reason"] = (
        "invalid_observed_price"
    )
    joined["observed_delta_ore"] = (next_price - joined.aip_ore).where(joined.outcome_observed)
    joined["price_band"] = pd.cut(
        joined.aip_ore,
        [0, 10000, 100000, np.inf],
        labels=["under100_dkk", "100_to1000_dkk", "1000_plus_dkk"],
        right=False,
    ).astype(str)
    return joined


def grouped_diagnostics(joined: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    """Small exact grouped ledgers reveal dependence on cheap/expensive packs."""
    result = {}
    for dimension in ("price_band", "finance_group_supported"):
        rows = []
        for keys, part in joined.groupby([*VARIANTS, dimension], dropna=False, sort=True):
            known = part.loc[part.outcome_observed]
            delta = known.observed_delta_ore
            rows.append(
                dict(zip([*VARIANTS, dimension], keys, strict=True))
                | {
                    "selected": len(part),
                    "observed": len(known),
                    "unknown_selected": int((~part.outcome_observed).sum()),
                    "quantity": int(part.quantity.sum()),
                    "observed_quantity": int(known.quantity.sum()),
                    "allocated_ore": int(part.allocated_ore.sum()),
                    "net_exposure_ore": int((known.quantity * delta).sum()),
                    "positive_exposure_ore": int((known.quantity * delta.clip(lower=0)).sum()),
                    "negative_exposure_ore": int((known.quantity * delta.clip(upper=0)).sum()),
                    "rises": int(delta.gt(0).sum()),
                    "unchanged": int(delta.eq(0).sum()),
                    "falls": int(delta.lt(0).sum()),
                }
            )
        result[dimension] = rows
    return result


def summaries(periods: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    expanded = pd.concat([periods, periods.assign(fold="all_2026")], ignore_index=True)
    for keys, part in expanded.groupby(VARIANTS, sort=True):
        observed = int(part.observed_selected.sum())
        selected = int(part.selected.sum())
        quantity = int(part.observed_funded_quantity.sum())
        known_period = part.observed_funded_quantity.gt(0)
        known = part.loc[known_period]
        net = int(part.observed_price_exposure_ore.sum())
        downside = -int(part.observed_negative_exposure_ore.sum())
        rows.append(
            dict(zip(VARIANTS, keys, strict=True))
            | {
                "periods": len(part),
                "observed_periods": len(known),
                "selected": selected,
                "observed_selected": observed,
                "unknown_selected": int(part.unknown_selected.sum()),
                "unknown_rate": float(part.unknown_selected.sum() / selected) if selected else None,
                "quantity": int(part.quantity.sum()),
                "observed_quantity": quantity,
                "unknown_quantity": int(part.unknown_quantity.sum()),
                "observed_rises": int(part.observed_rises.sum()),
                "observed_falls": int(part.observed_falls.sum()),
                "observed_unchanged": observed
                - int(part.observed_rises.sum() + part.observed_falls.sum()),
                "pooled_net_exposure_dkk": net / 100,
                "pooled_positive_exposure_dkk": float(
                    part.observed_positive_exposure_ore.sum() / 100
                ),
                "pooled_negative_exposure_dkk": -downside / 100,
                "pooled_net_dkk_per_observed_pack": net / (100 * quantity) if quantity else None,
                "pooled_downside_dkk_per_observed_pack": downside / (100 * quantity)
                if quantity
                else None,
                "equal_period_net_dkk_per_observed_pack": float(
                    (
                        known.observed_price_exposure_ore / (100 * known.observed_funded_quantity)
                    ).mean()
                )
                if len(known)
                else None,
                "equal_period_downside_dkk_per_observed_pack": float(
                    (
                        -known.observed_negative_exposure_ore
                        / (100 * known.observed_funded_quantity)
                    ).mean()
                )
                if len(known)
                else None,
                "matched_shortfall": int(part.matched_shortfall.sum()),
            }
        )
    return rows


def decision_gates(summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lookup = {(r["fold"], r["policy"], r["workload"], r["allocation"]): r for r in summary}
    decisions = []
    for policy in ("amount_dkk", "downside_aware"):
        reference = lookup[("all_2026", "probability_reference", "matched", "per_pack")]
        candidate = lookup[("all_2026", policy, "matched", "per_pack")]
        gain_key = "equal_period_net_dkk_per_observed_pack"
        down_key = "pooled_downside_dkk_per_observed_pack"
        complete = all(
            r.get(key) is not None
            for r in (reference, candidate)
            for key in (gain_key, down_key, "unknown_rate")
        )
        capital_checks = []
        for fold in sorted({r["fold"] for r in summary} - {"all_2026"}):
            baseline = lookup[(fold, "probability_reference", "matched", "fixed_capital")]
            other = lookup[(fold, policy, "matched", "fixed_capital")]
            comparable = baseline["observed_quantity"] > 0 and other["observed_quantity"] > 0
            capital_checks.append(
                {
                    "fold": fold,
                    "comparable": comparable,
                    "no_net_exposure_loss": comparable
                    and other["pooled_net_exposure_dkk"] >= baseline["pooled_net_exposure_dkk"],
                }
            )
        checks = {
            "at_least_five_percent_equal_period_gain": bool(
                complete
                and reference[gain_key] > 0
                and candidate[gain_key] >= reference[gain_key] * 1.05
            ),
            "no_aggregate_downside_per_pack_increase": bool(
                complete and candidate[down_key] <= reference[down_key]
            ),
            "no_unknown_rate_increase": bool(
                complete and candidate["unknown_rate"] <= reference["unknown_rate"]
            ),
            "no_matched_workload_shortfall": candidate["matched_shortfall"] == 0,
            "no_fixed_capital_loss_in_each_fold": bool(capital_checks)
            and all(r["no_net_exposure_loss"] for r in capital_checks),
        }
        decisions.append(
            {
                "policy": policy,
                "checks": checks,
                "fixed_capital_checks": capital_checks,
                "research_replacement_gate_passed": all(checks.values()),
                "operational_promotion": False,
                "interpretation": (
                    "Matched monetary expansion is diagnostic; price exposure is not profit."
                ),
            }
        )
    return decisions


def evaluate_fold(
    frame: pd.DataFrame,
    e4: pd.DataFrame,
    outcomes: pd.DataFrame,
    fold: str,
) -> dict[str, Any]:
    if any(
        str(c).startswith(("advance", "next_", "second_", "outcome")) or "hybrid" in str(c)
        for c in frame
    ):
        raise ResearchError("e6_a_noncurrent_decision_fields")
    queues = make_queues(frame, e4).assign(fold=fold)
    allocations, periods = [], []
    labels = outcomes.copy()
    if "package_id" not in labels:
        labels["package_id"] = labels.package_number
    for mode in ("per_pack", "fixed_capital"):
        allocation = allocate(queues, mode).assign(fold=fold)
        # price_exposure's coverage reference must be evaluated independently for E4.
        for scope in queues.comparison_scope.unique():
            q = queues.loc[queues.comparison_scope.eq(scope)].copy()
            a = allocation.loc[allocation.comparison_scope.eq(scope)].copy()
            if scope.startswith("original_e4"):
                q["policy"] = "probability_reference"
                a["policy"] = "probability_reference"
            scored = price_exposure(a, labels, queues=q, allocation=mode)
            if scope.startswith("original_e4"):
                scored["policy"] = "original_e4_current"
            scored["comparison_scope"] = scope
            periods.append(scored.assign(fold=fold))
        allocations.append(allocation)
    allocated = pd.concat(allocations, ignore_index=True)
    joined = joined_allocations(allocated, labels)
    unknown = joined.loc[~joined.outcome_observed]
    reasons = (
        unknown.groupby([*VARIANTS, "unknown_reason"], dropna=False)
        .agg(selected=("row_key", "size"), quantity=("quantity", "sum"))
        .reset_index()
    )
    return {
        "forecasts": frame.assign(fold=fold),
        "queues": queues,
        "allocations": allocated,
        "periods": pd.DataFrame.from_records(
            [row for part in periods for row in part.to_dict("records")]
        ),
        "unknown_reasons": reasons,
        "grouped": grouped_diagnostics(joined),
    }


def records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(frame.to_json(orient="records", date_format="iso", double_precision=15))


def diagnostic_tables(periods: pd.DataFrame, allocations: pd.DataFrame) -> dict[str, Any]:
    """Period-paired sensitivity and origin-supported group concentration only."""
    concentration = []
    for key, part in allocations.groupby([*VARIANTS, "period_start"], sort=True):
        supported = part.loc[part.finance_group_supported]
        groups = supported.groupby("finance_group").allocated_ore.sum()
        spend = int(part.allocated_ore.sum())
        concentration.append(
            dict(zip([*VARIANTS, "period_start"], key, strict=True))
            | {
                "allocated_ore": spend,
                "supported_groups": len(groups),
                "unsupported_selected": int((~part.finance_group_supported).sum()),
                "unsupported_allocated_ore": int(
                    part.loc[~part.finance_group_supported, "allocated_ore"].sum()
                ),
                "max_supported_group_allocated_ore": int(groups.max()) if len(groups) else 0,
                "max_supported_group_capital_share": float(groups.max() / spend)
                if spend and len(groups)
                else None,
                "top3_supported_group_capital_share": float(groups.nlargest(3).sum() / spend)
                if spend
                else None,
                "zero_quantity_selected": int(part.quantity.eq(0).sum()),
            }
        )
    paired = []
    keys = ["fold", "period_start", "allocation"]
    base = periods.loc[periods.policy.eq("probability_reference") & periods.workload.eq("matched")]
    for policy in ("amount_dkk", "downside_aware"):
        candidate = periods.loc[periods.policy.eq(policy) & periods.workload.eq("matched")]
        joined = base.merge(
            candidate, on=keys, suffixes=("_reference", "_candidate"), validate="one_to_one"
        ).sort_values("period_start")
        for mode, part in joined.groupby("allocation", sort=True):
            for metric, numerator, denominator in (
                (
                    "net_dkk_per_observed_pack",
                    "observed_price_exposure_ore",
                    "observed_funded_quantity",
                ),
                (
                    "downside_dkk_per_observed_pack",
                    "observed_negative_exposure_ore",
                    "observed_funded_quantity",
                ),
                ("unknown_selected_rate", "unknown_selected", "selected"),
            ):
                support = part[f"{denominator}_reference"].gt(0) & part[
                    f"{denominator}_candidate"
                ].gt(0)
                valid = part.loc[support]
                scale = (
                    -100
                    if metric.startswith("downside")
                    else 100
                    if metric.startswith("net")
                    else 1
                )
                difference = (
                    valid[f"{numerator}_candidate"] / valid[f"{denominator}_candidate"]
                    - valid[f"{numerator}_reference"] / valid[f"{denominator}_reference"]
                ).to_numpy(dtype=float) / scale
                for length in (2, 4):
                    item = {
                        "policy": policy,
                        "workload": "matched",
                        "allocation": mode,
                        "metric": metric,
                        "difference": "candidate minus probability reference",
                        "paired_periods": len(valid),
                        "excluded_periods": int((~support).sum()),
                        "block_length": length,
                        "repetitions": 2000,
                        "seed": 20261027,
                        "mean_difference": float(difference.mean()) if len(valid) else None,
                        "lower95": None,
                        "upper95": None,
                        "status": "insufficient_periods",
                    }
                    if len(valid) >= 2 * length:
                        rng = np.random.default_rng(20261027)
                        starts = rng.integers(
                            0, len(valid), size=(2000, int(np.ceil(len(valid) / length)))
                        )
                        draws = (starts[:, :, None] + np.arange(length)) % len(valid)
                        draws = draws.reshape(2000, -1)[:, : len(valid)]
                        interval = np.quantile(difference[draws].mean(axis=1), [0.025, 0.975])
                        item.update(
                            lower95=float(interval[0]),
                            upper95=float(interval[1]),
                            status="exposed_history_block_sensitivity",
                        )
                    paired.append(item)
    return {
        "group_concentration": concentration,
        "paired_period_bootstrap": paired,
        "interpretation": "Circular tariff-period blocks describe exposed-history variation; "
        "they are not independent prospective confirmation or row-level precision.",
    }


def diagnostics(root: Path) -> dict[str, Any]:
    require(root)
    directory = root / "investigation-a"
    source = read(directory / "results.json")
    if source["binding"]["protocol"] != pin(root / "protocol.json"):
        raise ResearchError("e6_a_diagnostic_protocol_changed")
    periods = pd.read_parquet(checked(source["files"]["periods"]), use_threads=False)
    allocations = pd.read_parquet(checked(source["files"]["allocations"]), use_threads=False)
    result = diagnostic_tables(periods, allocations)
    result["binding"] = {
        "results": pin(directory / "results.json"),
        "protocol": pin(root / "protocol.json"),
    }
    # Reuse JSON timestamp/null handling for the compact concentration records.
    result["group_concentration"] = records(pd.DataFrame(result["group_concentration"]))
    frozen_json(directory / "diagnostics.json", result)
    return result


def run(root: Path) -> dict[str, Any]:
    protocol = require(root)
    destination = root / "investigation-a"
    result_path = destination / "results.json"
    if result_path.exists():
        result = read(result_path)
        if result["binding"]["protocol"] != pin(root / "protocol.json"):
            raise ResearchError("e6_a_protocol_changed")
        for artifact in result["files"].values():
            checked(artifact)
        return result
    accepted = read(checked(protocol["pins"]["e5_acceptance"]))
    index = read(checked(accepted["artifacts"]["index.json"]))
    population_report = read(checked(protocol["pins"]["e5_population_report"]))
    population = pd.read_parquet(
        checked(population_report["artifacts"][0]),
        columns=["row_key", "behavior50_excluded"],
        use_threads=False,
    )
    e4_index = read(checked(protocol["pins"]["e4_index"]))
    e4_evaluation = read(checked(e4_index["evaluation"]))
    annual = next(v for v in e4_evaluation["annual"] if v["year"] == 2026)
    refs = (
        annual["artifacts"].values()
        if isinstance(annual["artifacts"], dict)
        else annual["artifacts"]
    )
    e4_ref = next(v for v in refs if Path(v["path"]).name == "predictions.parquet")
    e4 = pd.read_parquet(
        checked(e4_ref),
        columns=["row_key", *[f"perfect_current_p{k}" for k in range(3)]],
        use_threads=False,
    )
    binding: dict[str, Any] = {
        "protocol": pin(root / "protocol.json"),
        "population": index["population"],
        "e4_predictions": e4_ref,
        "folds": {},
    }
    outputs = []
    for fold in ("spring", "summer"):
        metadata_path = checked(protocol["pins"][f"e5_evaluations_{fold}_result"])
        metadata = read(metadata_path)
        predictions = pd.read_parquet(
            checked(metadata["files"]["predictions.parquet"]),
            columns=[*ORIGIN_FIELDS, *[f"tuned_current_p{k}" for k in range(3)]],
            use_threads=False,
        )
        outcomes = pd.read_parquet(
            checked(metadata["files"]["outcomes.parquet"]),
            columns=["row_key", "period_start", "package_number", "next_status", "next_aip_ore"],
            use_threads=False,
        )
        amounts = []
        receipt_pins = {}
        for task in ("rmse", "quantile"):
            receipt_path = checked(protocol["pins"][f"e5_trial_{fold}-{task}-current"])
            receipt = read(receipt_path)
            if receipt["status"] != "completed":
                raise ResearchError("e6_a_incomplete_current_amount")
            if (
                receipt["spec"]["information"] != "current"
                or receipt["spec"]["fold"] != fold
                or receipt["spec"]["method"] != task
                or any(str(c).startswith("advance") for c in receipt["features"])
            ):
                raise ResearchError("e6_a_noncurrent_amount_receipt")
            amounts.append(
                pd.read_parquet(checked(receipt["files"]["predictions.parquet"]), use_threads=False)
            )
            receipt_pins[task] = pin(receipt_path)
        frame = current_only_frame(predictions, amounts[0], amounts[1], population)
        outputs.append(evaluate_fold(frame, e4, outcomes, fold))
        binding["folds"][fold] = {
            "evaluation": pin(metadata_path),
            "current_receipts": receipt_pins,
        }
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("forecasts", "queues", "allocations", "periods", "unknown_reasons"):
        path = destination / f"{name}.parquet"
        pd.concat([o[name] for o in outputs], ignore_index=True).to_parquet(path, index=False)
        files[name] = pin(path)
    period_frame = pd.concat([o["periods"] for o in outputs], ignore_index=True)
    summary = summaries(period_frame)
    result = {
        "version": "e6-a-current-only-v1",
        "binding": binding,
        "files": files,
        "summary": summary,
        "decision_gates": decision_gates(summary),
        "grouped": {
            key: sum([o["grouped"][key] for o in outputs], [])
            for key in ("price_band", "finance_group_supported")
        },
        "unknown_reasons": records(pd.concat([o["unknown_reasons"] for o in outputs])),
        "exposure": protocol["exposure"],
        "operational_promotion": False,
        "limits": [
            "Matched monetary expansion is diagnostic only.",
            "Unknown selected outcomes remain without replacement.",
            "Price movement is not realised savings or a procurement policy.",
            "Original E4 coverage is a separate unfiltered reference.",
        ],
    }
    frozen_json(result_path, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    print(run(parser.parse_args().root.resolve())["version"])
