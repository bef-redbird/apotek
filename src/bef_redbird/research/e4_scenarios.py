"""Exact lot accounting for explicitly hypothetical procurement scenarios.

No sales margin is invented. One demand stream is shared across an origin-frozen,
equal-size substitution group. Fractional ore remain exact until presentation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pandas as pd

from .dataset import ResearchError
from .e4_common import checked, frozen_json, read, require_protocol, utc
from .group_features import pin


@dataclass
class Lot:
    package: str
    quantity: int
    unit_cost: Fraction
    credit_per_unit: Fraction
    received_day: int
    sequence: int

    @property
    def book_unit(self) -> Fraction:
        return self.unit_cost - self.credit_per_unit


class Ledger:
    """Single group; stock/flow and cash/book identities are checked after every event."""

    def __init__(self, initial_package: str, initial_quantity: int, price_ore: int) -> None:
        if initial_quantity < 0 or price_ore <= 0:
            raise ResearchError("e4_invalid_initial_stock")
        self.lots = [
            Lot(initial_package, initial_quantity, Fraction(price_ore), Fraction(0), -1, 0)
        ]
        self.orders: list[dict[str, Any]] = []
        self.initial_quantity = initial_quantity
        self.initial_cost = Fraction(initial_quantity * price_ore)
        self.purchases = Fraction(0)
        self.compensation = Fraction(0)
        self.return_credit = Fraction(0)
        self.sold_cost = Fraction(0)
        self.writeoff_cost = Fraction(0)
        self.returned_basis = Fraction(0)
        self.return_fee = Fraction(0)
        self.capital_area = Fraction(0)
        self.ordered = 0
        self.received = 0
        self.sold = 0
        self.unmet = 0
        self.expired = 0
        self.returned = 0
        self.day = -1
        self.sequence = 0
        self.peak_book = self.book
        self.peak_outstanding = 0
        self.events: list[dict[str, Any]] = []

    @property
    def stock(self) -> int:
        return sum(lot.quantity for lot in self.lots)

    @property
    def outstanding(self) -> int:
        return sum(x["quantity"] for x in self.orders)

    @property
    def book(self) -> Fraction:
        return sum((lot.book_unit * lot.quantity for lot in self.lots), Fraction(0))

    def advance(self, day: int) -> None:
        if day < self.day:
            raise ResearchError("e4_accounting_clock_reversed")
        self.capital_area += self.book * (day - self.day)
        self.day = day

    def check(self) -> None:
        if (
            self.initial_quantity + self.received
            != self.sold + self.expired + self.returned + self.stock
        ):
            raise ResearchError("e4_stock_conservation")
        if self.ordered != self.received + self.outstanding:
            raise ResearchError("e4_order_conservation")
        if (
            self.initial_cost + self.purchases - self.compensation
            != self.sold_cost + self.writeoff_cost + self.returned_basis + self.book
        ):
            raise ResearchError("e4_book_conservation")
        if any(lot.quantity < 0 or lot.book_unit < 0 for lot in self.lots):
            raise ResearchError("e4_negative_stock_basis")

    def event(self, kind: str, **values: Any) -> None:
        self.check()
        self.peak_book = max(self.peak_book, self.book)
        self.peak_outstanding = max(self.peak_outstanding, self.outstanding)
        self.events.append(
            {
                "day": self.day,
                "event": kind,
                **values,
                "on_hand": self.stock,
                "outstanding": self.outstanding,
                "book_ore_exact": str(self.book),
                "cash_outflow_ore_exact": str(
                    self.purchases - self.compensation - self.return_credit
                ),
            }
        )

    def order(self, package: str, quantity: int, receipt_day: int) -> None:
        if quantity < 0 or receipt_day < self.day:
            raise ResearchError("e4_invalid_order")
        if not quantity:
            return
        self.orders.append(
            {
                "package": package,
                "quantity": quantity,
                "ordered_day": self.day,
                "receipt_day": receipt_day,
            }
        )
        self.ordered += quantity
        self.event("order", package=package, quantity=quantity, receipt_day=receipt_day)

    def receive(self, day: int, prices: dict[str, int]) -> None:
        self.advance(day)
        for order in list(self.orders):
            if order["receipt_day"] != day:
                continue
            package, quantity = order["package"], order["quantity"]
            price = prices[package]
            if price <= 0:
                raise ResearchError("e4_unsupported_receipt_price")
            self.sequence += 1
            self.lots.append(
                Lot(package, quantity, Fraction(price), Fraction(0), day, self.sequence)
            )
            self.purchases += quantity * price
            self.received += quantity
            self.orders.remove(order)
            self.event(
                "receipt",
                package=package,
                quantity=quantity,
                unit_acquisition_ore=price,
                ordered_day=order["ordered_day"],
            )

    def credit(
        self,
        day: int,
        previous: dict[str, int],
        current: dict[str, int],
        rate: Fraction,
        cap: int | None,
    ) -> None:
        self.advance(day)
        if not 0 <= rate <= 1 or cap is not None and cap < 0:
            raise ResearchError("e4_invalid_compensation")
        used: dict[str, int] = {}
        lots = []
        credit = Fraction(0)
        for lot in sorted(self.lots, key=lambda x: (x.received_day, x.sequence)):
            available = lot.quantity if cap is None else max(0, cap - used.get(lot.package, 0))
            count = min(lot.quantity, available)
            unit = min(lot.book_unit, max(0, previous[lot.package] - current[lot.package]) * rate)
            if count and unit:
                lots.append(
                    replace(lot, quantity=count, credit_per_unit=lot.credit_per_unit + unit)
                )
                if count < lot.quantity:
                    lots.append(replace(lot, quantity=lot.quantity - count))
                credit += count * unit
                used[lot.package] = used.get(lot.package, 0) + count
            else:
                lots.append(lot)
        self.lots = lots
        self.compensation += credit
        self.event("compensation", credit_ore_exact=str(credit), rate=str(rate), cap=cap)

    def demand(self, day: int, quantity: int) -> None:
        self.advance(day)
        if quantity < 0:
            raise ResearchError("e4_negative_demand")
        remaining = quantity
        for lot in sorted(self.lots, key=lambda x: (x.received_day, x.sequence, x.package)):
            count = min(lot.quantity, remaining)
            lot.quantity -= count
            remaining -= count
            self.sold += count
            self.sold_cost += count * lot.book_unit
        self.lots = [lot for lot in self.lots if lot.quantity]
        self.unmet += remaining
        self.event("group_demand", demand=quantity, unmet=remaining)

    def expire(self, day: int, received_before: int) -> None:
        self.advance(day)
        expired = [lot for lot in self.lots if lot.received_day <= received_before]
        count = sum(lot.quantity for lot in expired)
        cost = sum((lot.quantity * lot.book_unit for lot in expired), Fraction(0))
        self.lots = [lot for lot in self.lots if lot.received_day > received_before]
        self.expired += count
        self.writeoff_cost += cost
        self.event("expiry", quantity=count, writeoff_ore_exact=str(cost))

    def returns(self, day: int, fee_rate: Fraction) -> None:
        self.advance(day)
        if not 0 <= fee_rate <= 1:
            raise ResearchError("e4_invalid_return_fee")
        basis = self.book
        count = self.stock
        fee = basis * fee_rate
        self.returned_basis += basis
        self.return_credit += basis - fee
        self.return_fee += fee
        self.returned += count
        self.lots = []
        self.event(
            "return", quantity=count, credit_ore_exact=str(basis - fee), fee_ore_exact=str(fee)
        )

    def finish(self, prices: dict[str, int]) -> dict[str, Any]:
        self.advance(28)
        self.check()
        market = sum(
            (Fraction(prices[lot.package]) * lot.quantity for lot in self.lots), Fraction(0)
        )
        capital = self.capital_area * Fraction(5, 100 * 365)
        cash = self.purchases - self.compensation - self.return_credit
        economic = self.initial_cost + cash - market + capital
        values = {
            "purchases": self.purchases,
            "compensation": self.compensation,
            "return_credit": self.return_credit,
            "return_fee": self.return_fee,
            "net_procurement_cash": cash,
            "initial_book": self.initial_cost,
            "sold_book_cost": self.sold_cost,
            "writeoff": self.writeoff_cost,
            "returned_basis": self.returned_basis,
            "ending_book": self.book,
            "ending_market": market,
            "capital_cost": capital,
            "average_book": self.capital_area / 29,
            "peak_book": self.peak_book,
            "inventory_valuation_difference": market - self.book,
            "economic_cost_proxy": economic,
        }
        return {
            **{k + "_dkk": float(v / 100) for k, v in values.items()},
            "exact_ore": {k: str(v) for k, v in values.items()},
            "ordered": self.ordered,
            "received": self.received,
            "sold": self.sold,
            "unmet": self.unmet,
            "remaining": self.stock,
            "outstanding": self.outstanding,
            "peak_outstanding": self.peak_outstanding,
            "expired": self.expired,
            "returned": self.returned,
            "stock_and_book_checks": True,
        }


def simulate(
    prices: list[dict[str, int]],
    *,
    demand: int,
    extra_limit: int,
    compensation: str,
    extra_package: str | None,
    variant: str = "normal",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if len(prices) != 3 or any(set(p) != set(prices[0]) for p in prices):
        raise ResearchError("e4_scenario_common_population")
    if demand < 1 or extra_limit < 0 or any(v <= 0 for p in prices for v in p.values()):
        raise ResearchError("e4_invalid_scenario_inputs")
    if variant not in {"normal", "late", "expiry", "returns"}:
        raise ResearchError("e4_unknown_scenario")
    initial = min(prices[0], key=lambda p: (prices[0][p], p))
    ledger = Ledger(initial, 2, prices[0][initial])
    stock_limit = 2 + extra_limit
    if extra_package is not None:
        if extra_package not in prices[0]:
            raise ResearchError("e4_extra_package_outside_origin")
        ledger.order(extra_package, extra_limit, 1 if variant == "late" else -1)
        ledger.receive(-1, prices[0])
    rate, cap = {
        "none": (Fraction(0), None),
        "half_cap2": (Fraction(1, 2), 2),
        "full": (Fraction(1), None),
    }[compensation]
    for period, day in ((1, 0), (2, 14)):
        if variant == "expiry" and period == 2:
            ledger.expire(day, 0)
        ledger.credit(day, prices[period - 1], prices[period], rate, cap)
        order = max(0, min(demand, stock_limit) - ledger.stock - ledger.outstanding)
        package = min(prices[period], key=lambda p: (prices[period][p], p))
        ledger.order(package, order, day)
        ledger.receive(day, prices[period])
        if period == 1 and variant == "late":
            ledger.receive(1, prices[1])
        ledger.demand(day + 7, demand)
    if variant == "returns":
        ledger.returns(28, Fraction(1, 10))
    return ledger.finish(prices[2]), ledger.events


def scenario_matrix() -> list[dict[str, Any]]:
    rows = [
        {"demand": d, "extra_limit": k, "compensation": c, "variant": "normal"}
        for d in (4, 8)
        for k in (4, 12)
        for c in ("none", "half_cap2", "full")
    ]
    rows += [
        {"demand": 4, "extra_limit": 12, "compensation": "none", "variant": v}
        for v in ("late", "expiry", "returns")
    ]
    return rows


def execute(root: Path) -> Path:
    require_protocol(root)
    result = root / "scenarios.json"
    if result.exists():
        for ref in read(result)["artifacts"]:
            checked(ref)
        return result
    evaluations = []
    outcomes = []
    for year in (2024, 2025, 2026):
        annual = read(root / f"evaluation-{year}/result.json")
        evaluations.append(pd.read_parquet(checked(annual["artifacts"][0])))
        outcomes.append(pd.read_parquet(checked(annual["outcomes"])))
    predictions = pd.concat(evaluations, ignore_index=True)
    labels = pd.concat(outcomes, ignore_index=True)
    # Freeze origin sample before looking at future prices or survival.
    origin_columns = [
        "row_key",
        "package_number",
        "period_start",
        "identity_episode_id",
        "aip_ore",
        "abc",
        "advance_abc",
        "origin_group",
        "pack_size_token",
        "pack_unit",
        "peer_count",
        "peer_feature_state",
        "selected",
        "perfect_hybrid_p0",
        "perfect_hybrid_p2",
    ]
    sample: list[dict[str, Any]] = []
    for day, part in predictions[origin_columns].groupby("period_start", sort=True):
        supported = part.loc[
            part.peer_count.ge(1) & part.peer_feature_state.eq("complete_equal_size_peers")
        ]
        groups = []
        for key, members in supported.groupby(
            ["origin_group", "pack_size_token", "pack_unit"], dropna=True
        ):
            if len(members) < 2 or not members.peer_count.eq(len(members) - 1).all():
                continue
            group_key = str(day)[:10] + ":" + ":".join(str(x) for x in key)
            groups.append(
                (hashlib.sha256(("20261027:" + group_key).encode()).hexdigest(), group_key, members)
            )
        for _, group_key, members in sorted(groups, key=lambda x: x[0])[:50]:
            rule = members.loc[
                members.abc.isin(["A", "B"]) & members.advance_abc.eq("C")
            ].sort_values(["aip_ore", "package_number"])
            model = members.loc[members.selected].copy()
            model["score"] = model.perfect_hybrid_p2 - 0.5 * model.perfect_hybrid_p0
            model = model.sort_values(["score", "package_number"], ascending=[False, True])
            sample.append(
                {
                    "group_key": group_key,
                    "period_start": str(day)[:10],
                    "members": members[
                        ["row_key", "package_number", "identity_episode_id", "aip_ore"]
                    ].to_dict("records"),
                    "rule_package": str(rule.iloc[0].package_number) if len(rule) else None,
                    "model_package": str(model.iloc[0].package_number) if len(model) else None,
                }
            )
    if not sample:
        raise ResearchError("e4_no_supported_scenario_groups")
    frozen_json(
        root / "scenario-population.json",
        {"version": "e4-scenario-origin-population-v1", "groups": sample, "created_utc": utc()},
    )
    lookup: Any = labels.set_index("row_key")
    # A following-origin label gives the second target price; keyed by origin episode.
    by_origin: Any = predictions.set_index(["package_number", "period_start"])
    rows = []
    unknown = []
    event_examples: list[dict[str, Any]] = []
    matrix = scenario_matrix()
    for item in sample:
        prices: list[dict[str, int]] = [{}, {}, {}]
        reason = None
        day = pd.Timestamp(item["period_start"])
        for member in item["members"]:
            package = member["package_number"]
            prices[0][package] = int(member["aip_ore"])
            first = lookup.loc[member["row_key"]]
            if first.next_status != "observed":
                reason = "first_target_" + first.next_status
                break
            if pd.Timestamp(first.next_maturity_date) != day + pd.Timedelta(days=14):
                raise ResearchError("e4_scenario_requires_14_day_first_target")
            prices[1][package] = int(first.next_aip_ore)
            try:
                second_origin = by_origin.loc[(package, day + pd.Timedelta(days=14))]
            except KeyError:
                reason = "second_origin_unavailable"
                break
            if second_origin.identity_episode_id != member["identity_episode_id"]:
                reason = "identity_discontinuity"
                break
            second = lookup.loc[second_origin.row_key]
            if second.next_status != "observed":
                reason = "second_target_" + second.next_status
                break
            if pd.Timestamp(second.next_maturity_date) != day + pd.Timedelta(days=28):
                raise ResearchError("e4_scenario_requires_14_day_second_target")
            prices[2][package] = int(second.next_aip_ore)
        if reason:
            unknown.append(
                {
                    "group_key": item["group_key"],
                    "period_start": item["period_start"],
                    "reason": reason,
                    "rule_signal": item["rule_package"] is not None,
                    "model_signal": item["model_package"] is not None,
                }
            )
            continue
        for number, scenario in enumerate(matrix):
            results = {}
            for policy, extra in (
                ("ordinary", None),
                ("rule", item["rule_package"]),
                ("model", item["model_package"]),
            ):
                values, events = simulate(prices, **scenario, extra_package=extra)
                results[policy] = values
                row = {
                    "group_key": item["group_key"],
                    "period_start": item["period_start"],
                    "scenario": number,
                    **scenario,
                    "policy": policy,
                    "extra_selected": extra is not None,
                    **{k: v for k, v in values.items() if k != "exact_ore"},
                }
                rows.append(row)
                if len(event_examples) < 6 and extra is not None:
                    event_examples.append(
                        {
                            "group_key": item["group_key"],
                            "scenario": number,
                            "policy": policy,
                            "prices_ore": prices,
                            "events": events,
                            "exact_final_ore": values["exact_ore"],
                        }
                    )
            for row in rows[-3:]:
                for metric in (
                    "economic_cost_proxy_dkk",
                    "net_procurement_cash_dkk",
                    "ending_market_dkk",
                    "capital_cost_dkk",
                    "unmet",
                    "remaining",
                    "expired",
                ):
                    row["incremental_" + metric] = row[metric] - results["ordinary"][metric]
    frame = pd.DataFrame(rows)
    frame.to_parquet(root / "scenario-results.parquet", index=False)
    frozen_json(
        root / "scenario-events.json", {"examples": event_examples, "unknown_trajectories": unknown}
    )
    summary = []
    for (scenario_number, policy_name), part in frame.groupby(["scenario", "policy"], sort=True):
        periods = part.groupby("period_start").incremental_economic_cost_proxy_dkk.sum()
        extra_count = int(part.extra_selected.sum())
        saved = {
            "scenario": int(str(scenario_number)),
            "policy": str(policy_name),
            **matrix[int(str(scenario_number))],
            "groups": len(part),
            "signal_groups": extra_count,
            "unknown_groups": len(unknown),
            "origin_groups": len(sample),
            "incremental_cost_dkk": float(part.incremental_economic_cost_proxy_dkk.sum()),
            "incremental_cash_dkk": float(part.incremental_net_procurement_cash_dkk.sum()),
            "incremental_terminal_inventory_dkk": float(part.incremental_ending_market_dkk.sum()),
            "incremental_capital_cost_dkk": float(part.incremental_capital_cost_dkk.sum()),
            "incremental_unmet": int(part.incremental_unmet.sum()),
            "remaining_packs": int(part.remaining.sum()),
            "expired_packs": int(part.expired.sum()),
            "compensation_dkk": float(part.compensation_dkk.sum()),
            "losing_group_fraction": float(part.incremental_economic_cost_proxy_dkk.gt(0).mean()),
            "worst_period_incremental_cost_dkk": float(periods.max()),
            "mean_period_incremental_cost_dkk": float(periods.mean()),
            "break_even_extra_fee_dkk_per_extra_pack": float(
                -part.incremental_economic_cost_proxy_dkk.sum()
                / (extra_count * int(part.extra_limit.iloc[0]))
            )
            if extra_count and part.incremental_economic_cost_proxy_dkk.sum() < 0
            else None,
        }
        summary.append(saved)
    frozen_json(
        result,
        {
            "version": "e4-scenarios-v1",
            "created_utc": utc(),
            "matrix": matrix,
            "origin_groups": len(sample),
            "scored_groups": len(sample) - len(unknown),
            "unknown_groups": len(unknown),
            "summary": summary,
            "artifacts": [
                pin(root / x)
                for x in (
                    "scenario-population.json",
                    "scenario-results.parquet",
                    "scenario-events.json",
                )
            ],
            "interpretation": (
                "Hypothetical procurement cost plus terminal market inventory and capital "
                "costs. No sales margins, pharmacy profit or optimal quantities. Public "
                "modern terms are motivation, not established historical eligibility."
            ),
        },
    )
    return result
