"""Optional product descriptions distinguish packs without changing price semantics."""

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
    assert history.prices == {"000001": (10000,)}
