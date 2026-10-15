"""Shared contracts. Financial input values are integer øre; derived medians retain halves."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

PARSER_VERSION = "excel-v2-biff"
FEATURE_VERSION = "price-history-v1"
RANKING_VERSION = "historical-priority-v1"
SCHEMA_VERSION = 1
STATUSES = ("Ikke vurderet", "Interessant", "Fravalgt", "Undersøg nærmere")


def eligibility_policy() -> dict[str, Any]:
    return {
        "excluded_storage": ["H", "K"],
        "current": ["A", "B"],
        "upcoming": ["C"],
        "max_cost_ore_exclusive": 100000,
    }


def _canonical_decimal(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class DataError(ValueError):
    """Safe, actionable boundary error; never embed source contents or local paths."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SourceInfo:
    name: str
    sha256: str
    size: int
    uploaded_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    level: str = "warning"
    product_id: str | None = None
    row: int | None = None
    column: str | None = None


@dataclass(frozen=True)
class CitoItem:
    product_id: str
    storage_code: str
    current_cost_ore: int
    current_code: str
    upcoming_code: str
    group_id: str | None
    source_rows: tuple[int, ...] = ()


@dataclass
class CitoSnapshot:
    source: SourceInfo
    items: dict[str, CitoItem]
    issues: list[Issue]
    input_rows: int
    unique_required_rows: int
    duplicate_rows: int
    conflicted_ids: set[str] = field(default_factory=set)
    incomplete_groups: set[str] = field(default_factory=set)
    sheet_name: str = "Table"


@dataclass
class HistoryDataset:
    source: SourceInfo
    periods: tuple[date, ...]
    prices: dict[str, tuple[int | None, ...]]
    labels: dict[str, str]
    source_rows: dict[str, tuple[int, ...]]
    issues: list[Issue]
    conflicted_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class AnalysisSettings:
    lookback_periods: int = 26
    minimum_valid_prices: int = 20
    rise_threshold: str = "0.05"
    low_price_percentile: str = "0.25"
    repeated_rise_minimum: int = 2
    minimum_events_for_rate_display: int = 5

    def __post_init__(self) -> None:
        integers = (
            self.lookback_periods,
            self.minimum_valid_prices,
            self.repeated_rise_minimum,
            self.minimum_events_for_rate_display,
        )
        if any(type(v) is not int for v in integers):
            raise DataError("Indstillinger skal bruge hele tal.")
        if not 2 <= self.lookback_periods <= 520:
            raise DataError("Historikvindue skal være mellem 2 og 520 perioder.")
        if not 1 <= self.minimum_valid_prices <= self.lookback_periods:
            raise DataError("Mindste antal priser skal passe i historikvinduet.")
        if not 0 <= self.repeated_rise_minimum <= 520:
            raise DataError("Antal prisstigninger er ugyldigt.")
        if not 1 <= self.minimum_events_for_rate_display <= 520:
            raise DataError("Mindste antal hændelser er ugyldigt.")
        if any(len(str(v)) > 128 for v in (self.rise_threshold, self.low_price_percentile)):
            raise DataError("Metodetærskler må højst være 128 tegn.")
        try:
            rise, low = Decimal(str(self.rise_threshold)), Decimal(str(self.low_price_percentile))
            valid = rise.is_finite() and low.is_finite() and 0 <= rise <= 10 and 0 <= low <= 1
            valid = valid and all(abs(v.adjusted()) <= 128 for v in (rise, low))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DataError("Ugyldige metodetærskler.") from exc
        if not valid:
            raise DataError("Ugyldige metodetærskler.")
        object.__setattr__(self, "rise_threshold", _canonical_decimal(rise))
        object.__setattr__(self, "low_price_percentile", _canonical_decimal(low))


@dataclass(frozen=True)
class AnalysisContext:
    current_period: date
    cito_received_at: str | None = None
    history_received_at: str | None = None

    def __post_init__(self) -> None:
        for value in (self.cito_received_at, self.history_received_at):
            if value is not None:
                try:
                    parsed = datetime.fromisoformat(value)
                except (TypeError, ValueError) as exc:
                    raise DataError("Modtagelsestid skal være ISO-tid med tidszone.") from exc
                if parsed.tzinfo is None:
                    raise DataError("Modtagelsestid kræver tidszone, fx +02:00.")


@dataclass(frozen=True)
class RiseEvent:
    period: date
    start_ore: int
    peak_ore: int
    next_ore: int | None
    next_change: float | None
    giveback: float | None


@dataclass(frozen=True)
class Features:
    n_valid: int
    window_periods: int
    coverage: float | None
    current_aip_ore: int | None
    previous_change: float | None
    median_ore: float | None
    median_distance_dkk: float | None
    percentile: float | None
    rise_count: int
    adjacent_pair_count: int
    rise_frequency: float | None
    reversal_denominator: int
    any_reversal_count: int
    full_reversal_count: int
    any_reversal_rate: float | None
    full_reversal_rate: float | None
    events: tuple[RiseEvent, ...]
    percentile_midrank_twice: int | None = None


@dataclass(frozen=True)
class Candidate:
    item: CitoItem
    features: Features
    rank: int | None
    tier: int | None
    reason: str
    flags: tuple[str, ...]


@dataclass
class AnalysisResult:
    analysis_id: str
    run_id: str
    created_at: str
    snapshot: CitoSnapshot
    history: HistoryDataset
    context: AnalysisContext
    settings: AnalysisSettings
    candidates: dict[str, Candidate]
    ranked_ids: tuple[str, ...]
    filter_counts: dict[str, int]
    issues: list[Issue]
    versions: dict[str, str]

    def group_members(self, product_id: str) -> list[CitoItem]:
        item = self.snapshot.items[product_id]
        if item.group_id is None:
            return [item]
        return sorted(
            (v for v in self.snapshot.items.values() if v.group_id == item.group_id),
            key=lambda v: v.product_id,
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "analysis_id": self.analysis_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "sources": {"cito": asdict(self.snapshot.source), "lms": asdict(self.history.source)},
            "context": {
                **asdict(self.context),
                "current_period": self.context.current_period.isoformat(),
            },
            "settings": asdict(self.settings),
            "eligibility": eligibility_policy(),
            "history_coverage": {
                "start": self.history.periods[0].isoformat() if self.history.periods else None,
                "end": self.history.periods[-1].isoformat() if self.history.periods else None,
                "period_count": len(self.history.periods),
            },
            "versions": self.versions,
            "filter_counts": self.filter_counts,
        }


@dataclass(frozen=True)
class QueueSettings:
    mode: Literal["top_n", "all_rankable"] = "top_n"
    top_n: int = 50

    def __post_init__(self) -> None:
        if self.mode not in ("top_n", "all_rankable"):
            raise DataError("Ugyldig køvisning.")
        if type(self.top_n) is not int or not 1 <= self.top_n <= 1_000_000_000:
            raise DataError("Antal kandidater skal være et positivt heltal (højst 1 milliard).")


@dataclass(frozen=True)
class Review:
    status: str = "Ikke vurderet"
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES or not isinstance(self.note, str) or len(self.note) > 4000:
            raise DataError("Ugyldig vurdering eller note over 4.000 tegn.")


@dataclass
class ReviewState:
    analysis_id: str
    queue: QueueSettings = field(default_factory=QueueSettings)
    reviews: dict[str, Review] = field(default_factory=dict)
    selected_ids: set[str] = field(default_factory=set)
    inspected_id: str | None = None


def select_queue(analysis: AnalysisResult, queue: QueueSettings) -> tuple[str, ...]:
    return (
        analysis.ranked_ids if queue.mode == "all_rankable" else analysis.ranked_ids[: queue.top_n]
    )
