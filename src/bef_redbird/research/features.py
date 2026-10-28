"""Causal features and separately joined outcomes on the declared calendar."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .dataset import Dataset, Row

FEATURE_NAMES = (
    "log_aip_dkk",
    "lag1_log_return",
    "lag1_missing",
    "lag2_log_return",
    "lag2_missing",
    "lag3_log_return",
    "lag3_missing",
    "past_rise_frequency",
    "past_fall_frequency",
    "past_flat_frequency",
    "history_missing",
    "history_periods",
    "lifecycle_age_periods",
    "lifecycle_age_missing",
    "abc_A",
    "abc_B",
    "abc_C",
    "abc_unknown",
    "group_count",
    "own_to_group_min_aip",
    "group_missing",
    "reported_supply_positive_observed",
    "supply_report_unobserved",
    "supply_coverage_unknown",
)
FloatArray = NDArray[np.float64]


def _past(dataset: Dataset, row: Row, offset: int) -> Row | None:
    previous = dataset.lookup.get((row.period_index - offset, row.canonical_identifier))
    if (
        previous is None
        or previous.identity_episode_id != row.identity_episode_id
        or not previous.eligible
    ):
        return None
    return previous


def period_features(dataset: Dataset, index: int) -> tuple[tuple[Row, ...], FloatArray]:
    """Use only <=index information; no future identity-based cohort selection."""
    rows = tuple(row for row in dataset.by_period[index] if row.eligible)
    groups: dict[str, list[int]] = {}
    for row in rows:
        if row.group_id:
            assert row.amount_ore is not None
            groups.setdefault(row.group_id, []).append(row.amount_ore)
    group_stats = {key: (len(values), min(values)) for key, values in groups.items()}
    features = np.zeros((len(rows), len(FEATURE_NAMES)), dtype=np.float64)
    for position, row in enumerate(rows):
        assert row.amount_ore is not None
        returns: list[float | None] = []
        for lag in range(1, 4):
            newer = _past(dataset, row, lag - 1)
            older = _past(dataset, row, lag)
            returns.append(
                math.log(newer.amount_ore / older.amount_ore)
                if newer and older and newer.amount_ore and older.amount_ore
                else None
            )
        changes: list[int] = []
        offset = 0
        newer = row
        while (older := _past(dataset, row, offset + 1)) is not None:
            assert older.amount_ore is not None and newer.amount_ore is not None
            changes.append(
                (newer.amount_ore > older.amount_ore) - (newer.amount_ore < older.amount_ore)
            )
            newer = older
            offset += 1
        count, minimum = group_stats.get(row.group_id, (0, row.amount_ore))
        features[position] = [
            math.log(row.amount_ore / 100),
            *[value for change in returns for value in (change or 0.0, float(change is None))],
            *[
                changes.count(direction) / len(changes) if changes else 0.0
                for direction in (1, -1, 0)
            ],
            float(not changes),
            float(len(changes)),
            float(row.lifecycle_age_periods or 0),
            float(row.lifecycle_age_periods is None),
            *[float(row.abc == category) for category in ("A", "B", "C")],
            float(row.abc not in {"A", "B", "C"}),
            float(count),
            row.amount_ore / minimum if count else 0.0,
            float(not count),
            float(row.reported_supply_positive is True),
            float(row.reported_supply_positive is None),
            float(row.supply_coverage_unknown),
        ]
    return rows, features


@dataclass(frozen=True, slots=True)
class Target:
    state: str
    direction: int | None = None
    log_return: float | None = None
    price_dkk: float | None = None
    full_reversal: int | None = None
    reversal_category: str | None = None
    reference_price_ore: int | None = None
    maturity_period: int | None = None

    def serial(self) -> dict[str, Any]:
        return asdict(self)


def _future(dataset: Dataset, row: Row, offset: int) -> tuple[Row | None, str]:
    index = row.period_index + offset
    if index >= len(dataset.periods):
        return None, "pending"
    future = dataset.lookup.get((index, row.canonical_identifier))
    if future is None:
        return None, "gap"
    if not future.eligible:
        return None, future.exclusion_reason
    if future.identity_episode_id != row.identity_episode_id:
        return None, "identity_change"
    return future, "observed"


def next_target(dataset: Dataset, row: Row) -> Target:
    future, state = _future(dataset, row, 1)
    if future is None:
        return Target(state, maturity_period=row.period_index + 1)
    assert row.amount_ore is not None and future.amount_ore is not None
    return Target(
        "observed",
        (future.amount_ore > row.amount_ore) - (future.amount_ore < row.amount_ore),
        math.log(future.amount_ore / row.amount_ore),
        future.amount_ore / 100,
        maturity_period=row.period_index + 1,
    )


def reversal_target(dataset: Dataset, row: Row) -> Target:
    previous = _past(dataset, row, 1)
    assert row.amount_ore is not None
    if previous is None or previous.amount_ore is None or row.amount_ore <= previous.amount_ore:
        return Target("not_observed_rise")
    horizon = [_future(dataset, row, offset) for offset in (1, 2)]
    # Missing first period is not hidden by a later pending calendar tail.
    unavailable = next((state for future, state in horizon if future is None), None)
    if unavailable is not None:
        return Target(
            unavailable,
            reference_price_ore=previous.amount_ore,
            maturity_period=row.period_index + 2,
        )
    amounts = [
        future.amount_ore
        for future, _ in horizon
        if future is not None and future.amount_ore is not None
    ]
    minimum = min(amounts)
    full = minimum <= previous.amount_ore
    category = (
        "full_reversal"
        if full
        else "partial_reversal"
        if minimum < row.amount_ore
        else "persistence"
    )
    return Target(
        "observed",
        full_reversal=int(full),
        reversal_category=category,
        reference_price_ore=previous.amount_ore,
        maturity_period=row.period_index + 2,
    )
