"""Independent precision, session-schema and export provenance regressions."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from bef_redbird.domain import (
    AnalysisContext,
    AnalysisSettings,
    CitoItem,
    CitoSnapshot,
    DataError,
    HistoryDataset,
    ReviewState,
    SourceInfo,
)
from bef_redbird.export import dump_session, export_analysis, restore_session
from bef_redbird.features import calculate_features
from bef_redbird.service import build_analysis


def example_analysis(settings: AnalysisSettings | None = None):
    source = SourceInfo("synthetic.xlsx", "a" * 64, 1)
    periods = (date(2026, 1, 5), date(2026, 1, 19), date(2026, 2, 2))
    history = HistoryDataset(source, periods, {"000001": (20000, 30000, 10000)}, {}, {}, [])
    item = CitoItem("000001", "N", 10000, "A", "C", "12", (2,))
    snapshot = CitoSnapshot(source, {"000001": item}, [], 1, 1, 0)
    return build_analysis(
        snapshot,
        history,
        AnalysisContext(periods[-1]),
        settings or AnalysisSettings(3, 3),
    )


def test_exact_rise_threshold_cannot_round_onto_excluded_boundary() -> None:
    threshold = "0.0499999999999999999999999999999"
    settings = AnalysisSettings(2, 2, rise_threshold=threshold)
    assert Decimal(settings.rise_threshold) == Decimal(threshold)
    source = SourceInfo("synthetic.xlsx", "a" * 64, 1)
    current = date(2026, 1, 19)
    history = HistoryDataset(
        source,
        (current - timedelta(days=14), current),
        {"000001": (10000, 10500)},
        {},
        {},
        [],
    )
    # 5% is strictly greater than the configured decimal, by exact arithmetic.
    assert calculate_features(history, "000001", current, settings).rise_count == 1


def test_percentile_tier_boundary_uses_exact_midrank_fraction() -> None:
    # Current is the unique minimum: (0 + 0.5 * 1) / 3 = 1/6.
    # The configured threshold is strictly below 1/6, so low-price tier is false.
    analysis = example_analysis(
        AnalysisSettings(
            3,
            3,
            low_price_percentile="0.1666666666666666666666666666",
        )
    )
    assert analysis.candidates["000001"].tier == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_schema", True),
        ("session_schema", 1.0),
        ("settings", {"lookback_periods": 3.0}),
        ("settings", {"minimum_valid_prices": 3.0}),
    ],
)
def test_restore_rejects_wrong_json_types_even_if_python_values_compare_equal(
    field: str,
    value: object,
) -> None:
    analysis = example_analysis()
    payload = json.loads(dump_session(analysis, ReviewState(analysis.analysis_id)))
    if field == "settings":
        payload[field].update(value)
    else:
        payload[field] = value
    with pytest.raises(DataError):
        restore_session(json.dumps(payload).encode(), analysis)


def test_export_manifest_records_baseline_eligibility_and_history_coverage() -> None:
    analysis = example_analysis()
    book = load_workbook(io.BytesIO(export_analysis(analysis, ReviewState(analysis.analysis_id))))
    metadata = dict(list(book["Metode og datakontrol"].values)[1:])
    # Replay settings must include the baseline policy, not only feature parameters.
    assert "eligibility" in metadata
    policy = json.loads(metadata["eligibility"])
    assert policy["max_cost_ore_exclusive"] == 100000
    assert policy["excluded_storage"] == ["H", "K"]
    # Source hashes alone do not tell a reviewer the available history coverage.
    assert "history_coverage" in metadata
    coverage = json.loads(metadata["history_coverage"])
    assert coverage["start"] == "2026-01-05"
    assert coverage["end"] == "2026-02-02"


def test_reference_order_uses_tier_then_gap_then_percentile_then_product_id() -> None:
    source = SourceInfo("synthetic.xlsx", "a" * 64, 1)
    periods = tuple(date(2026, 1, 5) + timedelta(days=14 * i) for i in range(5))
    values = {
        "000001": (8000, 12000, 10000, 12000, 8000),  # tier 0, gap 20, percentile .2
        "000002": (20000, 50000, 40000, 30000, 10000),  # tier 1, gap 200
        "000003": (10000, 12000, 10000, 12000, 8000),  # tier 0, gap 20, percentile .1
        "000004": (10000, 12000, 10000, 12000, 8000),  # exact tie resolved by ID
        "000005": (10000, 10000, 10000, 10000, 10000),  # tier 2
    }
    items = {
        pid: CitoItem(pid, "N", prices[-1], "A", "C", "12")
        for pid, prices in reversed(list(values.items()))
    }
    snapshot = CitoSnapshot(source, items, [], 5, 5, 0)
    history = HistoryDataset(source, periods, values, {}, {}, [])
    analysis = build_analysis(
        snapshot, history, AnalysisContext(periods[-1]), AnalysisSettings(5, 5)
    )
    assert analysis.ranked_ids == ("000003", "000004", "000001", "000002", "000005")
    assert [analysis.candidates[pid].tier for pid in analysis.ranked_ids] == [0, 0, 0, 1, 2]


def test_large_group_export_preserves_every_relation_and_missing_groups_are_separate() -> None:
    base = example_analysis()
    template = base.snapshot.items["000001"]
    group_ids = tuple(f"{i:06d}" for i in range(1, 4701))
    items = {pid: replace(template, product_id=pid) for pid in group_ids}
    # Two selected products without group membership and an unrelated unselected
    # product must never form a fictitious common "missing" group.
    for pid in ("900001", "900002", "900003"):
        items[pid] = replace(template, product_id=pid, group_id=None)
    items["900004"] = replace(template, product_id="900004", storage_code="H")
    snapshot = replace(
        base.snapshot,
        items=items,
        input_rows=4704,
        unique_required_rows=4704,
    )
    values = base.history.prices["000001"]
    history = replace(base.history, prices={pid: values for pid in group_ids})
    analysis = build_analysis(snapshot, history, base.context, base.settings)
    selected_ids = (*group_ids, "900001", "900002")
    state = ReviewState(analysis.analysis_id, selected_ids=set(selected_ids))
    book = load_workbook(io.BytesIO(export_analysis(analysis, state, "selected")))

    candidate_rows = list(book["Kandidater"].values)[1:]
    assert tuple(row[1] for row in candidate_rows) == selected_ids
    group_rows = list(book["Gruppeoverblik"].values)[1:]
    expected_members = {*group_ids, "900001", "900002", "900004"}
    assert len(group_rows) == len(expected_members)
    assert {row[0] for row in group_rows} == expected_members
    missing_rows = {row[0]: row for row in group_rows if row[1] is None}
    assert set(missing_rows) == {"900001", "900002"}
    assert missing_rows["900001"][2] == "Varenummer 900001"
    assert missing_rows["900002"][2] == "Varenummer 900002"

    metadata = list(book["Metode og datakontrol"].values)[1:]
    relations = [json.loads(value) for key, value in metadata if key == "selected_group_relation"]
    assert relations == [
        {"product_id": pid, "group_id": "12" if pid in group_ids else None} for pid in selected_ids
    ]
    assert tuple(value for key, value in metadata if key == "ordered_exported_id") == selected_ids
