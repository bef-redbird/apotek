"""Frozen E5 ranking, integer budget allocation and hypothetical group accounting.

Forecasts describe price exposure, never sales profit. Outcomes are a separate
argument and cannot affect queues, allocation, group population or capacity.
The exact E4 lot ledger supplies stock, cash, credit and valuation identities.
"""

from __future__ import annotations

import hashlib
import math
from fractions import Fraction
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e4_scenarios import Ledger

KEYS = ["period_start", "package_id"]
POLICIES = ("probability_reference", "amount_dkk", "downside_aware")
CAPITAL_ORE = 10_000_000
PACKAGE_CAP_ORE = 1_000_000
GROUP_CAP_ORE = 2_500_000
SCENARIOS: tuple[dict[str, Any], ...] = (
    {"name": "timely_demand4", "demand": 4, "variant": "normal", "compensation": "none"},
    {"name": "timely_demand8", "demand": 8, "variant": "normal", "compensation": "none"},
    {"name": "late_receipt", "demand": 4, "variant": "late", "compensation": "none"},
    {"name": "early_expiry", "demand": 4, "variant": "expiry", "compensation": "none"},
    {"name": "limited_compensation", "demand": 4, "variant": "normal", "compensation": "half_cap2"},
    {"name": "explicit_returns", "demand": 4, "variant": "returns", "compensation": "none"},
)


def _keys(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy().reset_index(drop=True)
    if any(x not in out for x in KEYS) or out[KEYS].isna().any().any():
        raise ResearchError("e5_finance_missing_keys")
    out["period_start"] = pd.to_datetime(out.period_start)
    if not out.package_id.map(lambda x: isinstance(x, str) and bool(x)).all():
        raise ResearchError("e5_finance_package_id_requires_string")
    if out.duplicated(KEYS).any():
        raise ResearchError("e5_finance_duplicate_keys")
    return out


def origin_groups(frame: pd.DataFrame) -> pd.DataFrame:
    """Origin-only support. Incomplete equal-size populations are explicit gaps."""
    out = _keys(frame)
    out["finance_group"] = ""
    out["finance_group_supported"] = False
    out["finance_scenario_supported"] = False
    columns = ["origin_group", "pack_size_token", "pack_unit"]
    if any(x not in out for x in [*columns, "peer_count", "peer_feature_state"]):
        return out
    for key, part in out.groupby(["period_start", *columns], dropna=True, sort=True):
        supported: bool = bool(
            part.peer_feature_state.eq("complete_equal_size_peers").all()
            and part.peer_count.ge(1).all()
        )
        if "aip_ore" in part:
            price = pd.to_numeric(part.aip_ore, errors="coerce")
            supported = bool(
                supported & (np.isfinite(price) & price.gt(0) & price.eq(np.floor(price))).all()
            )
        # Hash a structured representation: concatenation cannot alias group keys.
        token = hashlib.sha256(repr(tuple(str(x) for x in key)).encode()).hexdigest()
        out.loc[part.index, "finance_group"] = token
        out.loc[part.index, "finance_group_supported"] = bool(supported)
        out.loc[part.index, "finance_scenario_supported"] = bool(
            supported and len(part) >= 2 and part.peer_count.eq(len(part) - 1).all()
        )
    return out


def queue_policies(predictions: pd.DataFrame, *, include_persistence: bool = False) -> pd.DataFrame:
    """Complete deterministic queues; monetary point must be a mean estimate.

    Natural: common rise>=.35/fall<=.10 screen and 10% original-population cap.
    Reference ranks rise probability exactly as E4; other policies need score>0.
    Optional reference_p_rise/reference_p_fall retain frozen E4 probabilities
    while the monetary policies use the new p_rise/p_fall gates.
    Matched reference rows retain the exact natural threshold selections.
    Matched monetary policies rank every scoring-supported candidate for that
    per-origin workload, including negative scores. Missing score support produces
    an explicit shortfall, never an outcome-dependent replacement.
    """
    forbidden = [x for x in predictions if str(x).startswith(("next_", "second_", "outcome"))]
    if forbidden:
        raise ResearchError("e5_finance_outcomes_in_decision_inputs")
    required = [
        "row_key",
        "aip_ore",
        "eligible",
        "p_rise",
        "p_fall",
        "forecast_delta_dkk",
        "lower_delta_dkk",
    ]
    if any(x not in predictions for x in required):
        raise ResearchError("e5_finance_missing_predictions")
    out = origin_groups(predictions)
    probability_columns = ["p_rise", "p_fall"]
    reference_columns = ["reference_p_rise", "reference_p_fall"]
    if any(x in out for x in reference_columns):
        if not all(x in out for x in reference_columns):
            raise ResearchError("e5_finance_incomplete_reference_probabilities")
        probability_columns += reference_columns
    for column in [*probability_columns, "forecast_delta_dkk", "lower_delta_dkk"]:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype(float)
    price = pd.to_numeric(out.aip_ore, errors="coerce")
    valid_price = np.isfinite(price) & price.gt(0) & price.eq(np.floor(price))
    if (out.eligible & ~valid_price).any():
        raise ResearchError("e5_finance_requires_exact_positive_origin_ore")
    if not out.eligible.map(lambda x: isinstance(x, (bool, np.bool_))).all():
        raise ResearchError("e5_finance_invalid_eligibility")
    out["tie_key"] = [hashlib.sha256(f"20261026:{key}".encode()).hexdigest() for key in out.row_key]
    reference_rise = out.get("reference_p_rise", out.p_rise)
    reference_fall = out.get("reference_p_fall", out.p_fall)
    scores = {
        "probability_reference": reference_rise,
        "amount_dkk": out.forecast_delta_dkk,
        "downside_aware": out.forecast_delta_dkk - np.maximum(0, -out.lower_delta_dkk),
    }
    if include_persistence:
        if "persistence_probability" not in out:
            raise ResearchError("e5_finance_missing_persistence")
        persistence = pd.to_numeric(out.persistence_probability, errors="coerce").astype(float)
        scores["persistence_aware"] = (scores["downside_aware"]).where(persistence.between(0.5, 1))
    records = []
    workloads: dict[Any, int] = {}
    for policy, score in scores.items():
        part = out.assign(policy=policy, score=score, rank=0, selected=False)
        policy_eligible = (
            out.get("reference_eligible", out.eligible)
            if policy == "probability_reference"
            else out.eligible
        )
        if not policy_eligible.map(lambda x: isinstance(x, (bool, np.bool_))).all():
            raise ResearchError("e5_finance_invalid_reference_eligibility")
        part["policy_eligible"] = policy_eligible
        rise = reference_rise if policy == "probability_reference" else out.p_rise
        fall = reference_fall if policy == "probability_reference" else out.p_fall
        probability = (
            np.isfinite(rise)
            & np.isfinite(fall)
            & rise.between(0, 1)
            & fall.between(0, 1)
            & (rise + fall).le(1 + 1e-12)
        )
        part["policy_probability_rise"] = rise
        part["policy_probability_fall"] = fall
        part["score_supported"] = probability & policy_eligible & np.isfinite(score)
        for day, group in part.groupby("period_start", sort=True):
            ordered = group.loc[group.score_supported].sort_values(
                ["score", "tie_key"], ascending=[False, True]
            )
            part.loc[ordered.index, "rank"] = np.arange(1, len(ordered) + 1)
            natural = ordered.loc[
                ordered.policy_probability_rise.ge(0.35)
                & ordered.policy_probability_fall.le(0.10)
                & ordered.score.gt(0)
            ]
            count = math.ceil(len(group) * 0.10)
            chosen = natural.index[:count]
            part.loc[chosen, "selected"] = True
            if policy == "probability_reference":
                workloads[day] = len(chosen)
        part["workload"] = "natural"
        records.append(part.copy())
        if policy != "probability_reference":
            part["selected"] = False
            for day, group in part.groupby("period_start", sort=True):
                ordered = group.loc[group.score_supported].sort_values("rank")
                part.loc[ordered.index[: workloads[day]], "selected"] = True
        part["workload"] = "matched"
        records.append(part)
    result = pd.concat(records, ignore_index=True)
    result["selection_reason"] = np.select(
        [~result.policy_eligible, ~result.score_supported, result.selected],
        ["ineligible_origin", "prediction_unavailable", "selected_frozen_policy"],
        default="outside_frozen_queue",
    )
    return result


def queue_summary(queues: pd.DataFrame) -> pd.DataFrame:
    """Every origin, including empty natural queues and matched-support gaps."""
    reference = (
        queues.loc[queues.policy.eq("probability_reference") & queues.workload.eq("natural")]
        .groupby("period_start")
        .selected.sum()
        .to_dict()
    )
    rows = []
    for key, part in queues.groupby(["period_start", "policy", "workload"], sort=True):
        requested = int(reference[key[0]]) if key[2] == "matched" else None
        count = int(part.selected.sum())
        rows.append(
            {
                "period_start": key[0],
                "policy": key[1],
                "workload": key[2],
                "origin_population": len(part),
                "eligible": int(part.policy_eligible.sum()),
                "scoring_supported": int(part.score_supported.sum()),
                "selected": count,
                "matched_requested": requested,
                "matched_shortfall": max(0, requested - count) if requested is not None else 0,
            }
        )
    return pd.DataFrame(rows)


def allocate(queues: pd.DataFrame, mode: str = "fixed_capital") -> pd.DataFrame:
    """Return selected identities, including zero-pack capital-constrained rows."""
    if mode not in {"per_pack", "four_pack", "fixed_capital"}:
        raise ResearchError("e5_finance_unknown_allocation")
    rows = []
    for _, part in queues.groupby(["period_start", "policy", "workload"], sort=True):
        left = CAPITAL_ORE
        group_spend: dict[str, int] = {}
        selected = part.loc[part.selected].sort_values(["rank", "tie_key"])
        origin_rows = []
        for row in selected.to_dict("records"):
            price = int(row["aip_ore"])
            supported = bool(row["finance_group_supported"])
            group = str(row["finance_group"])
            if mode == "fixed_capital":
                allowed = min(left, PACKAGE_CAP_ORE)
                if supported:
                    allowed = min(allowed, GROUP_CAP_ORE - group_spend.get(group, 0))
                quantity = allowed // price
            else:
                quantity = 1 if mode == "per_pack" else 4
            spent = quantity * price
            if mode == "fixed_capital":
                left -= spent
                group_spend[group] = group_spend.get(group, 0) + spent
            origin_rows.append(
                {
                    **row,
                    "allocation": mode,
                    "quantity": quantity,
                    "allocated_ore": spent,
                    "allocation_reason": "allocated"
                    if quantity
                    else "integer_or_concentration_limit",
                }
            )
        for row in origin_rows:
            row["unallocated_capital_ore"] = left if mode == "fixed_capital" else None
            rows.append(row)
    columns = [
        *queues.columns,
        "allocation",
        "quantity",
        "allocated_ore",
        "allocation_reason",
        "unallocated_capital_ore",
    ]
    return pd.DataFrame(rows, columns=columns)


def price_exposure(
    allocations: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    queues: pd.DataFrame | None = None,
    allocation: str | None = None,
) -> pd.DataFrame:
    """Observed price movement, with unknown selected packs retained separately."""
    labels = _keys(outcomes)
    if any(x not in labels for x in ["next_status", "next_aip_ore"]):
        raise ResearchError("e5_finance_missing_outcome_columns")
    joined = allocations.merge(
        labels[[*KEYS, "next_status", "next_aip_ore"]], on=KEYS, how="left", validate="many_to_one"
    )
    amounts = pd.to_numeric(joined.next_aip_ore, errors="coerce")
    if (amounts.notna() & ~amounts.eq(np.floor(amounts))).any():
        raise ResearchError("e5_finance_requires_exact_outcome_ore")
    rows = []
    for key, part in joined.groupby(
        ["period_start", "policy", "workload", "allocation"], sort=True
    ):
        known = (
            part.next_status.eq("observed") & part.next_aip_ore.notna() & part.next_aip_ore.gt(0)
        )
        use = part.loc[known]
        realized_ore = sum(
            int(r["quantity"]) * (int(r["next_aip_ore"]) - int(r["aip_ore"]))
            for r in use.to_dict("records")
        )
        positive_ore = sum(
            int(r["quantity"]) * max(0, int(r["next_aip_ore"]) - int(r["aip_ore"]))
            for r in use.to_dict("records")
        )
        negative_ore = sum(
            int(r["quantity"]) * min(0, int(r["next_aip_ore"]) - int(r["aip_ore"]))
            for r in use.to_dict("records")
        )
        funded = part.quantity.gt(0)
        spend = int(part.allocated_ore.sum())
        maximum = int(part.allocated_ore.max()) if len(part) else 0
        rows.append(
            dict(zip(["period_start", "policy", "workload", "allocation"], key, strict=True))
            | {
                "selected": len(part),
                "observed_selected": int(known.sum()),
                "unknown_selected": int((~known).sum()),
                "funded_selected": int(funded.sum()),
                "funded_unknown": int((funded & ~known).sum()),
                "observed_funded_quantity": int(part.loc[known & funded, "quantity"].sum()),
                "observed_acquisition_basis_ore": int(part.loc[known, "allocated_ore"].sum()),
                "quantity": int(part.quantity.sum()),
                "unknown_quantity": int(part.loc[~known, "quantity"].sum()),
                "allocated_ore": int(part.allocated_ore.sum()),
                "unallocated_capital_ore": part.unallocated_capital_ore.iloc[0],
                "forecast_supported_selected": int(part.forecast_delta_dkk.notna().sum()),
                "forecast_unknown_quantity": int(
                    part.loc[part.forecast_delta_dkk.isna(), "quantity"].sum()
                ),
                "forecast_price_exposure_dkk": float(
                    (part.quantity * part.forecast_delta_dkk).sum(min_count=1)
                ),
                "observed_price_exposure_ore": realized_ore,
                "observed_positive_exposure_ore": positive_ore,
                "observed_negative_exposure_ore": negative_ore,
                "max_package_budget_share": maximum / CAPITAL_ORE
                if key[3] == "fixed_capital"
                else None,
                "max_package_allocated_capital_share": maximum / spend if spend else None,
                "top10_allocated_capital_share": float(
                    part.allocated_ore.nlargest(10).sum() / spend
                )
                if spend
                else None,
                "observed_rises": int(use.next_aip_ore.gt(use.aip_ore).sum()),
                "observed_falls": int(use.next_aip_ore.lt(use.aip_ore).sum()),
            }
        )
    result = pd.DataFrame(rows)
    if queues is not None:
        if allocation not in {"per_pack", "four_pack", "fixed_capital"}:
            raise ResearchError("e5_finance_empty_queue_requires_allocation")
        coverage = queue_summary(queues).assign(allocation=allocation)
        columns = ["period_start", "policy", "workload", "allocation", "selected"]
        if not result.empty:
            coverage = coverage.merge(result, on=columns, how="left", validate="one_to_one")
        zero_columns = [
            "observed_selected",
            "unknown_selected",
            "funded_selected",
            "funded_unknown",
            "observed_funded_quantity",
            "observed_acquisition_basis_ore",
            "quantity",
            "unknown_quantity",
            "allocated_ore",
            "forecast_supported_selected",
            "forecast_unknown_quantity",
            "observed_price_exposure_ore",
            "observed_positive_exposure_ore",
            "observed_negative_exposure_ore",
            "observed_rises",
            "observed_falls",
        ]
        for column in zero_columns:
            coverage[column] = coverage[column].fillna(0) if column in coverage else 0
        if "forecast_price_exposure_dkk" not in coverage:
            coverage["forecast_price_exposure_dkk"] = np.nan
        coverage.loc[coverage.selected.eq(0), "forecast_price_exposure_dkk"] = 0.0
        if "unallocated_capital_ore" not in coverage:
            coverage["unallocated_capital_ore"] = None
        if allocation == "fixed_capital":
            coverage["unallocated_capital_ore"] = CAPITAL_ORE - coverage.allocated_ore
        for column in (
            "max_package_budget_share",
            "max_package_allocated_capital_share",
            "top10_allocated_capital_share",
        ):
            if column not in coverage:
                coverage[column] = None
        result = coverage
    return result


def simulate_group(
    prices: list[dict[str, int]],
    extras: dict[str, int],
    *,
    capacity_extra: int,
    demand: int,
    variant: str = "normal",
    compensation: str = "none",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One group demand stream, including multiple selected packages, E4 accounting."""
    if (
        len(prices) != 3
        or not prices[0]
        or any(set(p) != set(prices[0]) for p in prices)
        or any(type(v) is not int or v <= 0 for p in prices for v in p.values())
        or any(p not in prices[0] or type(q) is not int or q < 0 for p, q in extras.items())
        or type(capacity_extra) is not int
        or capacity_extra < sum(extras.values())
        or type(demand) is not int
        or demand < 1
    ):
        raise ResearchError("e5_finance_invalid_group_scenario")
    if variant not in {"normal", "late", "expiry", "returns"} or compensation not in {
        "none",
        "half_cap2",
    }:
        raise ResearchError("e5_finance_unknown_scenario")
    initial = min(prices[0], key=lambda p: (prices[0][p], p))
    ledger = Ledger(initial, 2, prices[0][initial])
    for package, quantity in sorted(extras.items()):
        ledger.order(package, quantity, 1 if variant == "late" else -1)
    ledger.receive(-1, prices[0])
    rate, cap = (Fraction(1, 2), 2) if compensation == "half_cap2" else (Fraction(0), None)
    for period, day in ((1, 0), (2, 14)):
        if variant == "expiry" and period == 2:
            ledger.expire(day, 0)
        ledger.credit(day, prices[period - 1], prices[period], rate, cap)
        quantity = max(0, min(demand, 2 + capacity_extra) - ledger.stock - ledger.outstanding)
        package = min(prices[period], key=lambda p: (prices[period][p], p))
        ledger.order(package, quantity, day)
        ledger.receive(day, prices[period])
        if period == 1 and variant == "late":
            ledger.receive(1, prices[1])
        ledger.demand(day + 7, demand)
    if variant == "returns":
        ledger.returns(28, Fraction(1, 10))
    return ledger.finish(prices[2]), ledger.events


def compare_group_scenarios(
    origins: pd.DataFrame,
    allocations: pd.DataFrame,
    trajectories: pd.DataFrame,
    *,
    variants: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Same frozen groups/capacity for every policy, unknown paths never replaced.

    Trajectory statuses must already enforce episode identity and 14/28-day
    calendar continuity. A missing member makes the whole group unscored.
    Unsupported origin groups are retained in returned unknown rows.
    """
    groups = origin_groups(origins)
    labels = _keys(trajectories)
    required = ["next_status", "next_aip_ore", "second_status", "second_aip_ore"]
    if any(x not in labels for x in required):
        raise ResearchError("e5_finance_missing_trajectory_columns")
    for column in ("next_aip_ore", "second_aip_ore"):
        values = pd.to_numeric(labels[column], errors="coerce")
        if (values.notna() & ~values.eq(np.floor(values))).any():
            raise ResearchError("e5_finance_requires_exact_trajectory_ore")
    groups = groups.merge(labels[[*KEYS, *required]], on=KEYS, how="left", validate="one_to_one")
    rows: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    comparison_keys = ["allocation", "workload", "policy"]
    variant_frame = allocations if variants is None else variants
    comparisons = [
        {str(k): v for k, v in item.items()}
        for item in variant_frame[comparison_keys].drop_duplicates().to_dict("records")
    ]
    for (day, group_key), part in groups.groupby(["period_start", "finance_group"], sort=True):
        group_day = part.period_start.iloc[0]
        if not part.finance_scenario_supported.all():
            unknown.append(
                {
                    "period_start": day,
                    "finance_group": group_key,
                    "reason": "unsupported_origin_group",
                    "members": len(part),
                }
            )
            continue
        status = part.next_status.eq("observed") & part.second_status.eq("observed")
        positive = part.next_aip_ore.gt(0) & part.second_aip_ore.gt(0)
        if not (status & positive).all():
            unknown.append(
                {
                    "period_start": day,
                    "finance_group": group_key,
                    "reason": "unknown_group_trajectory",
                    "members": len(part),
                    "first_states": part.next_status.fillna("missing").value_counts().to_dict(),
                    "second_states": part.second_status.fillna("missing").value_counts().to_dict(),
                }
            )
            continue
        prices = [
            dict(zip(part.package_id, part[c].astype(int), strict=True))
            for c in ("aip_ore", "next_aip_ore", "second_aip_ore")
        ]
        selected = allocations.loc[
            allocations.period_start.eq(group_day)
            & allocations.finance_group.eq(str(group_key))
            & allocations.finance_group_supported
        ]
        # One capacity per origin group across every allocation/policy comparison.
        capacity = max(4 * len(part), GROUP_CAP_ORE // int(part.aip_ore.min()))
        for scenario in SCENARIOS:
            arguments = {k: scenario[k] for k in ("demand", "variant", "compensation")}
            base, _ = simulate_group(prices, {}, capacity_extra=capacity, **arguments)
            for variant in comparisons:
                use = selected
                for k, value in variant.items():
                    use = use.loc[use[k].eq(value)]
                extras = dict(zip(use.package_id, use.quantity.astype(int), strict=True))
                result, _ = simulate_group(prices, extras, capacity_extra=capacity, **arguments)
                row: dict[str, Any] = {
                    "period_start": day,
                    "finance_group": group_key,
                    **scenario,
                    **variant,
                    "extra_quantity": sum(extras.values()),
                    "capacity_extra": capacity,
                    **{k: v for k, v in result.items() if k != "exact_ore"},
                    "exact_ore": result["exact_ore"],
                    "ordinary_exact_ore": base["exact_ore"],
                }
                for metric in (
                    "economic_cost_proxy",
                    "net_procurement_cash",
                    "ending_market",
                    "capital_cost",
                    "compensation",
                    "return_credit",
                    "writeoff",
                ):
                    delta = Fraction(result["exact_ore"][metric]) - Fraction(
                        base["exact_ore"][metric]
                    )
                    row["incremental_" + metric + "_dkk"] = float(delta / 100)
                row["incremental_unmet"] = result["unmet"] - base["unmet"]
                rows.append(row)
    return pd.DataFrame(rows), pd.DataFrame(unknown)
