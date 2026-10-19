"""Optional product descriptions distinguish packs without changing price semantics."""

import pytest

from bef_redbird.domain import HistoryDataset, SourceInfo
from bef_redbird.imports import parse_lms_history
from tests.unit.test_imports import xlsx_bytes


def test_display_metadata_optional_ordered_and_prices_unchanged() -> None:
    base = ["Varenummer", "Indikator", "20260105", "20260119"]
    minimal = parse_lms_history(xlsx_bytes(base, [[1, "AIP", 100, 120]], "Medicinpriser data"))
    rich = parse_lms_history(
        xlsx_bytes(
            ["Firma", *base, "Pakning", "Form", "Styrke", "Lægemiddel"],
            [["Firma B", 1, "AIP", 100, 120, "100 stk.", "tabletter", "20 mg", "Eksempel"]],
            "Medicinpriser data",
        )
    )
    assert rich.labels == {"000001": "Eksempel · 20 mg · tabletter · 100 stk. · Firma B"}
    assert minimal.labels == {}
    assert rich.companies == {"000001": "Firma B"}
    assert minimal.companies == {}
    assert rich.prices == minimal.prices == {"000001": (10000, 12000)}
    assert rich.periods == minimal.periods
    assert rich.issues == minimal.issues == []


def test_optional_formula_error_and_ambiguous_heading_are_not_used_as_labels() -> None:
    data = xlsx_bytes(
        [
            "Varenummer",
            "Indikator",
            "Lægemiddel",
            "Styrke",
            " styrke ",
            "Form",
            "Firma",
            "20260105",
        ],
        [[1, "AIP", "Eksempel", "20 mg", "40 mg", "=1+1", "#VALUE!", 100]],
        "Medicinpriser data",
    )
    history = parse_lms_history(data)
    assert history.labels == {"000001": "Eksempel"}
    assert history.companies == {}
    assert history.prices == {"000001": (10000,)}


def test_companies_default_preserves_positional_history_construction() -> None:
    history = HistoryDataset(SourceInfo("synthetic", "hash", 0), (), {}, {}, {}, [])
    assert history.companies == {}


@pytest.mark.parametrize("company", ["=1+1", "#VALUE!", None, "   "])
def test_invalid_or_empty_company_is_ignored(company: str | None) -> None:
    history = parse_lms_history(
        xlsx_bytes(
            ["Varenummer", "Indikator", "Firma", "20260105"],
            [[1, "AIP", company, 100]],
            "Medicinpriser data",
        )
    )
    assert history.companies == {}
    assert history.prices == {"000001": (10000,)}
    assert history.issues == []


def test_duplicate_company_headings_are_ignored() -> None:
    history = parse_lms_history(
        xlsx_bytes(
            ["Varenummer", "Indikator", "Firma", " firma ", "20260105"],
            [[1, "AIP", "Firma A", "Firma A", 100]],
            "Medicinpriser data",
        )
    )
    assert history.companies == {}
    assert history.labels == {}
    assert history.prices == {"000001": (10000,)}


def test_company_uses_only_exact_aip_rows_and_never_guesses_from_labels() -> None:
    history = parse_lms_history(
        xlsx_bytes(
            ["Firma", "Lægemiddel", "20260105", "Indikator", "Varenummer"],
            [
                ["Wrong company", "Other", 200, "ESP", 1],
                ["Also wrong", "Other", 200, "aip", 1],
                ["Also wrong", "Other", 200, " AIP ", 1],
                [None, "Example · Firma A", 100, "AIP", 1],
                ["Only non-AIP", "Other", 200, "ESP", 2],
            ],
            "Medicinpriser data",
        )
    )
    assert history.companies == {}
    assert history.labels == {"000001": "Example · Firma A"}
    assert history.prices == {"000001": (10000,)}
    assert history.source_rows == {"000001": (5,)}


def test_company_is_whitespace_normalized_and_bounded() -> None:
    history = parse_lms_history(
        xlsx_bytes(
            ["Varenummer", "Indikator", "20260105", " Firma "],
            [[1, "AIP", 100, "  Firma\n  A\t "], [2, "AIP", 200, "X" * 250]],
            "Medicinpriser data",
        )
    )
    assert history.companies == {"000001": "Firma A", "000002": "X" * 200}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("names", [("Firma A", "Firma B"), ("X" * 200 + "A", "X" * 200 + "B")])
def test_duplicate_conflicting_companies_are_suppressed_in_either_order(
    reverse: bool, names: tuple[str, str]
) -> None:
    rows = [[1, "AIP", names[0], 100], [1, "AIP", names[1], 100]]
    if reverse:
        rows.reverse()
    rows += [[1, "AIP", names[0], 100], [1, "AIP", None, 100]]
    history = parse_lms_history(
        xlsx_bytes(
            ["Varenummer", "Indikator", "Firma", "20260105"],
            rows,
            "Medicinpriser data",
        )
    )
    assert history.companies == {}
    assert history.prices == {"000001": (10000,)}
    assert history.source_rows == {"000001": (2, 3, 4, 5)}
    assert history.conflicted_ids == set()
    assert history.conflicted_periods == {}
    assert history.issues == []


def test_duplicate_blank_and_equivalent_companies_keep_unambiguous_value() -> None:
    history = parse_lms_history(
        xlsx_bytes(
            ["Varenummer", "Indikator", "Firma", "20260105"],
            [
                [1, "AIP", None, 100],
                [1, "AIP", " Firma A ", 100],
                [1, "AIP", "Firma\nA", 100],
                [1, "AIP", "=1+1", 100],
                [1, "AIP", "#VALUE!", 100],
            ],
            "Medicinpriser data",
        )
    )
    assert history.companies == {"000001": "Firma A"}
    assert history.prices == {"000001": (10000,)}
