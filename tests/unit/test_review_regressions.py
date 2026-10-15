"""Independent regression examples for period-scoped ambiguity and optional metadata."""

from datetime import date
from io import BytesIO
from itertools import permutations

import pytest
from openpyxl import load_workbook

from bef_redbird.charts import history_chart
from bef_redbird.domain import AnalysisContext, AnalysisSettings, DataError, ReviewState
from bef_redbird.export import dump_session, export_analysis
from bef_redbird.imports import parse_cito, parse_lms_history
from bef_redbird.service import build_analysis
from tests.unit.test_imports import CITO_HEADERS, xls_bytes, xlsx_bytes

PERIODS = (date(2026, 1, 5), date(2026, 1, 19), date(2026, 2, 2), date(2026, 2, 16))
HEADERS = ["Varenummer", "Indikator", *[p.strftime("%Y%m%d") for p in PERIODS]]
SETTINGS = AnalysisSettings(lookback_periods=2, minimum_valid_prices=1)


def _history(rows):
    return parse_lms_history(xlsx_bytes(HEADERS, rows, "Medicinpriser data"))


def _snapshot():
    return parse_cito(
        xlsx_bytes(CITO_HEADERS, [[123, "", 100, "A", "C", 12], [124, "", 100, "A", "C", 12]])
    )


def test_later_duplicate_conflict_cannot_change_earlier_features_ranks_or_default_chart():
    snapshot = _snapshot()
    original = [[123, "AIP", 110, 120, 100, 130], [124, "AIP", 100, 100, 100, 100]]
    baseline = build_analysis(snapshot, _history(original), AnalysisContext(PERIODS[2]), SETTINGS)
    revised = build_analysis(
        snapshot,
        _history([*original, [123, "AIP", 110, 120, 100, 140]]),
        AnalysisContext(PERIODS[2]),
        SETTINGS,
    )
    assert baseline.ranked_ids == revised.ranked_ids == ("000123", "000124")
    assert baseline.candidates == revised.candidates
    assert baseline.analysis_id != revised.analysis_id  # Changed bytes retain distinct provenance.
    assert revised.history.prices["000123"] == (11000, 12000, 10000, None)
    assert revised.history.conflicted_periods == {"000123": {PERIODS[3]}}
    assert (
        tuple(history_chart(baseline, "000123").data[0].y)
        == tuple(history_chart(revised, "000123").data[0].y)
        == (110, 120, 100)
    )
    assert tuple(history_chart(revised, "000123", retrospective=True).data[0].y) == (
        110,
        120,
        100,
        None,
    )
    issue = next(i for i in revised.issues if i.code == "history_conflict")
    assert issue.column == PERIODS[3].isoformat() and issue.row == 4


@pytest.mark.parametrize(
    "conflict_index,blocks_rank", [(0, False), (1, True), (2, True), (3, False)]
)
def test_only_conflicts_inside_the_applied_window_block_ranking(conflict_index, blocks_rank):
    values = [110, 120, 100, 130]
    changed = list(values)
    changed[conflict_index] += 1
    analysis = build_analysis(
        _snapshot(),
        _history([[123, "AIP", *values], [123, "AIP", *changed]]),
        AnalysisContext(PERIODS[2]),
        SETTINGS,
    )
    candidate = analysis.candidates["000123"]
    assert ("history_conflict" in candidate.flags) is blocks_rank
    assert (candidate.rank is None) is blocks_rank


def test_conflicts_remain_null_after_further_duplicates_regardless_of_row_order():
    rows = [[123, "AIP", 110, 120, 100, 130], [123, "AIP", 110, 121, 100, 130]]
    for ordered in permutations([*rows, rows[0]]):
        history = _history(list(ordered))
        assert history.prices["000123"] == (11000, None, 10000, 13000)
        assert history.conflicted_periods == {"000123": {PERIODS[1]}}
        assert history.source_rows["000123"] == (2, 3, 4)
        assert len([i for i in history.issues if i.code == "history_conflict"]) == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_invalid_product_retains_known_group_and_invalid_group_still_rejects_product(legacy):
    rows = [
        [123, "", 100, "A", "C", 12],
        ["bad ID", "", 100, "A", "C", 12],
        [124, "", 100, "A", "C", "bad group"],
        [124, "", 100, "A", "C", 12],
    ]
    snapshot = (
        parse_cito(xls_bytes(rows), "cito.xls")
        if legacy
        else parse_cito(xlsx_bytes(CITO_HEADERS, rows))
    )
    assert set(snapshot.items) == {"000123"}
    assert snapshot.incomplete_groups == {"12"}
    assert "000124" in snapshot.conflicted_ids
    analysis = build_analysis(
        snapshot,
        _history([[123, "AIP", 100, 100, 100, 100]]),
        AnalysisContext(PERIODS[2]),
        SETTINGS,
    )
    book = load_workbook(BytesIO(export_analysis(analysis, ReviewState(analysis.analysis_id))))
    assert book["Gruppeoverblik"]["K2"].value == "Uafklarede rækker i gruppen"


def test_unknown_optional_metadata_outside_date_block_is_ignored():
    data = xlsx_bytes(
        ["Ny leverandørtekst", "Varenummer", "Indikator", *HEADERS[2:], "Ekstra pakningsfelt"],
        [["valgfri", 123, "AIP", 110, 120, 100, 130, "=1+1"]],
        "Medicinpriser data",
        hidden=True,
    )
    history = parse_lms_history(data)
    assert history.periods == PERIODS
    assert history.prices == {"000123": (11000, 12000, 10000, 13000)}
    assert history.issues == []


@pytest.mark.parametrize("bad", [None, "invalid period", "20260230", "2026-02-02", "2026020"])
def test_malformed_headers_in_date_block_remain_errors(bad):
    headers = ["Varenummer", "Indikator", "20260105", bad, "20260216"]
    with pytest.raises(DataError):
        parse_lms_history(xlsx_bytes(headers, [[123, "AIP", 1, 2, 3]], "Medicinpriser data"))


def test_unknown_inspected_product_cannot_be_emitted_in_a_session():
    analysis = build_analysis(
        _snapshot(),
        _history([[123, "AIP", 100, 100, 100, 100]]),
        AnalysisContext(PERIODS[2]),
        SETTINGS,
    )
    with pytest.raises(DataError, match="Ukendt vist produkt"):
        dump_session(analysis, ReviewState(analysis.analysis_id, inspected_id="999999"))
