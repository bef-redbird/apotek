"""Historical definitions, calculated only from the declared period and earlier."""

from datetime import date
from decimal import Decimal
from statistics import median

from bef_redbird.domain import AnalysisSettings, Features, HistoryDataset, RiseEvent


def calculate_features(
    history: HistoryDataset, product_id: str, current: date, settings: AnalysisSettings
) -> Features:
    through = [i for i, period in enumerate(history.periods) if period <= current]
    indices = through[-settings.lookback_periods :]
    dates = [history.periods[i] for i in indices]
    raw = history.prices.get(product_id, (None,) * len(history.periods))
    values = [v if v is not None and v > 0 else None for v in (raw[i] for i in indices)]
    valid = [v for v in values if v is not None]
    now = values[-1] if dates and dates[-1] == current else None
    middle = float(median(valid)) if valid else None
    percentile = (
        (sum(v < now for v in valid) + 0.5 * sum(v == now for v in valid)) / len(valid)
        if valid and now is not None
        else None
    )
    events: list[RiseEvent] = []
    pair_count = 0
    any_count, full_count, denominator = 0, 0, 0
    previous_change = None
    threshold_numerator, threshold_denominator = Decimal(settings.rise_threshold).as_integer_ratio()
    for index in range(1, len(dates)):
        before, peak = values[index - 1], values[index]
        if before is None or peak is None or (dates[index] - dates[index - 1]).days != 14:
            continue
        pair_count += 1
        if dates[index] == current:
            previous_change = peak / before - 1
        if (peak - before) * threshold_denominator <= before * threshold_numerator:
            continue
        following = None
        if index + 1 < len(dates) and (dates[index + 1] - dates[index]).days == 14:
            following = values[index + 1]
        if following is not None:
            denominator += 1
            any_count += following < peak
            full_count += following <= before
        events.append(
            RiseEvent(
                dates[index],
                before,
                peak,
                following,
                following / peak - 1 if following is not None else None,
                (peak - following) / (peak - before) if following is not None else None,
            )
        )
    return Features(
        n_valid=len(valid),
        window_periods=len(dates),
        coverage=len(valid) / len(dates) if dates else None,
        current_aip_ore=now,
        previous_change=previous_change,
        median_ore=middle,
        median_distance_dkk=(middle - now) / 100
        if middle is not None and now is not None
        else None,
        percentile=percentile,
        rise_count=len(events),
        adjacent_pair_count=pair_count,
        rise_frequency=len(events) / pair_count if pair_count else None,
        reversal_denominator=denominator,
        any_reversal_count=any_count,
        full_reversal_count=full_count,
        any_reversal_rate=any_count / denominator if denominator else None,
        full_reversal_rate=full_count / denominator if denominator else None,
        events=tuple(events),
        percentile_midrank_twice=(
            2 * sum(v < now for v in valid) + sum(v == now for v in valid)
            if now is not None
            else None
        ),
    )
