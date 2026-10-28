"""Independently specified historical workbook variants; original versions stay pinned."""

from __future__ import annotations

from datetime import datetime

import pytest

from bef_redbird.foundation.adapters.base import AdapterError
from bef_redbird.foundation.adapters.reports import (
    parse_discontinued,
    parse_discontinued_v2,
    parse_introductions,
    parse_introductions_v2,
)
from tests.test_foundation_reports import PRICE_CONTEXT, intro_rows, workbook


@pytest.mark.parametrize("tariff_col", range(4, 12))
@pytest.mark.parametrize("run_row,run_col", [(2, 12), (2, 13), (3, 12)])
def test_archive_introduction_metadata_coordinates(
    tariff_col: int, run_row: int, run_col: int
) -> None:
    rows = intro_rows()
    rows[1] = [None] * 14
    rows[1][tariff_col] = "Takstdato: 05-01-2026"
    rows[2] = [None] * 14
    rows[3] = [None] * 14
    rows[run_row][run_col] = "Kørselsdato: 02-01-2026"
    parsed = parse_introductions_v2(workbook(rows), PRICE_CONTEXT)
    assert len(parsed.assertions) == 4
    raw = parsed.assertions[0].raw
    assert raw["tariff_date"] == "2026-01-05"
    assert raw["run_date"] == "2026-01-02"
    assert raw["run_date_locator"] == f"sheet:Synthetic!{chr(65 + run_col)}{run_row + 1}"
    if tariff_col not in (6, 7, 8, 9):
        with pytest.raises(AdapterError, match="missing_report_date"):
            parse_introductions(workbook(rows), PRICE_CONTEXT)


def test_unrecognized_metadata_position_is_explicitly_unsupported() -> None:
    rows = intro_rows()
    rows[0].extend([None] * 13)
    rows[0][13] = "Kørselsdato: 02-01-2026"
    with pytest.raises(AdapterError, match="unsupported_report_metadata_position"):
        parse_introductions_v2(workbook(rows), PRICE_CONTEXT)


def test_row_two_lifecycle_variant_does_not_date_attributes_from_unlabelled_date() -> None:
    data = workbook(
        [
            [datetime(2026, 1, 5)],
            [
                "Repræsentant",
                "Lægemiddel",
                "Lægemiddelform",
                "Styrke",
                "Varenummer",
                "RPk",
                "Stop (deaktiveret)",
                "Slet (udgår permanent)",
            ],
            [
                "Example",
                "Invented",
                "tablet",
                "5 mg",
                "000007",
                "10 stk.",
                datetime(2025, 8, 4),
                datetime(2026, 1, 5),
            ],
        ]
    )
    with pytest.raises(AdapterError, match="unsupported_schema"):
        parse_discontinued(data, {})
    parsed = parse_discontinued_v2(data, {})
    assert len(parsed.assertions) == 3
    attributes, stop, deletion = parsed.assertions
    assert attributes.effective_period is None
    assert attributes.report_period is None
    assert attributes.raw["unlabelled_date"] == "2026-01-05T00:00:00"
    assert attributes.raw["unlabelled_date_semantics"] == "unknown"
    assert stop.effective_date == datetime(2025, 8, 4).date()
    assert deletion.effective_date == datetime(2026, 1, 5).date()
    assert stop.values["original_source_code"] == "Stop (deaktiveret)"
    with pytest.raises(AdapterError, match="unestablished_report_period"):
        parse_discontinued_v2(data, {"current_period": "2026-01-05"})
