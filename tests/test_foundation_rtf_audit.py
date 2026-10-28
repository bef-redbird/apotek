"""Synthetic expectations for the independent native RTF auditor."""

from __future__ import annotations

import pytest

from bef_redbird.foundation.rtf_audit import expected_rtf, read_native_rtf

CONTEXT = {"current_period": "2020-01-06", "tax_basis": "unknown", "fee_basis": "unknown"}
CHANGE_STOPS = (90, 2955, 5445, 7365, 8170, 11115, 12075, 12960, 13890, 14912, 15002)


def paragraph(stops: tuple[int, ...], cells: list[str], *, bold: bool = False) -> str:
    return (
        "\\pard"
        + "".join(f"\\tx{x}" for x in stops)
        + "{"
        + ("\\b " if bold else "")
        + "\\tab "
        + "\\tab ".join(cells)
        + "\\par}"
    )


def document(parts: list[str]) -> bytes:
    return (r"{\rtf1\ansi\ansicpg1252" + "".join(parts) + "}").encode("cp1252")


def changes(*body: str, footer: bool = True) -> bytes:
    return document(
        [
            paragraph((90, 3847), ["Prisændringer", "06-01-2020"]),
            paragraph(
                tuple(range(10)),
                [
                    "Firma, Navn",
                    "Form",
                    "Styrke",
                    "Vnr",
                    "Pakningstekst",
                    "Ny AIP",
                    "Ny .reg.",
                    "Gl.AIP",
                    "Gl. reg. pris",
                    "Prisforskel",
                ],
            ),
            paragraph((90,), ["Example Company"], bold=True),
            *body,
            *([paragraph((90, 13946), ["", "Side 1 af 1"])] if footer else []),
        ]
    )


def full_record(*, identifier: str = "123", amount: str = "12,50") -> str:
    return paragraph(
        CHANGE_STOPS,
        [
            "Medicine",
            "film",
            "1 mg",
            identifier,
            "20 stk",
            amount,
            "20,00",
            "0,00",
            "19,00",
            "5",
            "%",
        ],
    )


def test_native_tokens_unicode_escaped_braces_and_ignored_destinations() -> None:
    data = rb"{\rtf1\ansi\ansicpg1252{\fonttbl fake;}\pard\tx90{\tab\uc1 \u230?\'f8\{x\}\\\par}}"
    parts = read_native_rtf(data)
    assert len(parts) == 1
    assert parts[0].cells == ("æø{x}\\",)
    assert parts[0].stops == (90,)


def test_unicode_surrogate_pairs_and_malformed_group_are_accounted() -> None:
    parts = read_native_rtf(rb"{\rtf1\pard\tx90\tab\u-10179?\u-8704?\par}")
    assert parts[0].cells == ("😀",)
    with pytest.raises(ValueError, match="invalid_unicode"):
        read_native_rtf(rb"{\rtf1\pard\tx90\tab\u-10179?\par}")
    with pytest.raises(ValueError, match="unterminated_document"):
        read_native_rtf(rb"{\rtf1\pard\tx90\tab missing close\par")


def test_inherited_paragraph_stops_and_global_locators() -> None:
    data = document(
        [r"\pard\tx90\tx200{\tab A\tab B\par\tab C\tab D\par}\page", paragraph((90,), ["E"])]
    )
    parts = read_native_rtf(data)
    assert [p.cells for p in parts] == [("A", "B"), ("C", "D"), ("E",)]
    assert [p.stops for p in parts] == [(90, 200), (90, 200), (90,)]
    assert parts[-1].locator == "rtf:page:2!paragraph:3"


def test_real_leading_space_survives_intervening_formatting_controls() -> None:
    parts = read_native_rtf(rb"{\rtf1\pard \tx90{\tab One\par\plain  \b Two\par}}")
    assert [part.cells for part in parts] == [("One",), (" Two",)]


def test_change_quotes_company_raw_pieces_and_exact_money() -> None:
    result = expected_rtf(changes(full_record(amount="1.234,50")), "price-changes", CONTEXT)
    assert result["source_rows"] == 1
    assert len(result["physical_accounting"]) == 5
    package, *prices = result["assertions"]
    assert package["canonical_identifier"] == "000123"
    assert package["values"]["company"] == "Example Company"
    assert package["presence"]["atc"] == "unreported"
    assert [p["values"]["amount_ore"] for p in prices] == [123450, 2000, 0, 1900]
    assert [p["effective_period"] for p in prices] == ["2020-01-06", "2020-01-06", None, None]
    assert prices[0]["source_locator"] == "rtf:page:1!paragraph:4!x:11115"
    assert prices[0]["raw"]["fragments"][0]["cells"][5] == "1.234,50"


def test_wrapped_money_joins_digits_and_text_joins_words() -> None:
    data = changes(full_record(amount="12"), paragraph((2955, 11115), ["coated", ",50"]))
    result = expected_rtf(data, "price-changes", CONTEXT)
    package, aip, *_ = result["assertions"]
    assert package["values"]["form"] == "film coated"
    assert aip["values"]["amount_ore"] == 1250
    assert len(aip["raw"]["fragments"]) == 2
    assert aip["raw"]["fragments"][1]["locator"] == "rtf:page:1!paragraph:5"
    assert result["physical_accounting"][4]["kind"] == "record_continuation"


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        ("-0,01", "negative_money"),
        ("12,345", "unsupported_money_precision"),
        ("x", "malformed_money"),
        ("92233720368547758,08", "money_overflow"),
    ],
)
def test_malformed_money_keeps_assertion_and_scoped_issue(amount: str, code: str) -> None:
    result = expected_rtf(changes(full_record(amount=amount)), "price-changes", CONTEXT)
    aip = result["assertions"][1]
    assert aip["values"]["amount_ore"] is None
    assert aip["presence"] == {"amount_ore": "invalid"}
    assert aip["quality"] == "invalid"
    assert result["issues"][0]["code"] == code
    assert result["issues"][0]["period"] == "2020-01-06"


def introductions(
    *, omitted_storage: bool = False, blank_storage: bool = False, no_prices: bool = False
) -> bytes:
    stops = [536, 1530, 6685, 8367, 12506, 13974]
    cells = ["000007", "10 stk", "" if blank_storage else "cold", "B", "0,00", "1,00"]
    if omitted_storage:
        stops.remove(6685)
        cells.pop(2)
    if no_prices:
        stops, cells = stops[:-2], cells[:-2]
    return document(
        [
            paragraph((3428, 12019), ["Nye pakninger på nye formuleringer", "06-01-2020"]),
            paragraph((90, 4038, 8555), ["Navn", "Form", "Styrke"]),
            paragraph((116, 4592), ["MF-indehaver", "Repræsentant"]),
            paragraph(
                tuple(range(7)),
                [
                    "Varenr.",
                    "Pakningsstørrelse",
                    "Opbevaringsbetingelse",
                    "Udlevering",
                    "Trafikadvarsel",
                    "AIP",
                    "Reg.pris",
                ],
            ),
            paragraph((90, 4038, 8555), ["Medicine", "tablet", "1 mg"]),
            paragraph((296, 4592), ["Holder", "Company"]),
            paragraph(tuple(stops), cells),
            paragraph((90, 13886), ["", "Side 1 af 1"]),
        ]
    )


@pytest.mark.parametrize(
    ("kwargs", "state", "value"),
    [
        ({}, "present", "cold"),
        ({"omitted_storage": True}, "unreported", None),
        ({"blank_storage": True}, "blank", None),
    ],
)
def test_introduction_removed_tab_stop_preserves_field_missingness(
    kwargs: dict[str, bool], state: str, value: str | None
) -> None:
    result = expected_rtf(introductions(**kwargs), "introductions", CONTEXT)
    package, aip, registered, lifecycle = result["assertions"]
    assert package["presence"]["storage_code"] == state
    assert package["values"]["storage_code"] == value
    assert package["values"]["company"] == "Company"
    assert package["values"]["name"] == "Medicine"
    assert aip["values"]["amount_ore"] == 0
    assert registered["values"]["amount_ore"] == 100
    assert lifecycle["effective_date"] == "2020-01-06"
    assert len(package["raw"]["fragments"]) == 3


def test_missing_price_coordinates_are_unreported() -> None:
    result = expected_rtf(introductions(no_prices=True), "introductions", CONTEXT)
    assert [a["presence"] for a in result["assertions"][1:3]] == [{"amount_ore": "unreported"}] * 2


def test_blank_price_coordinate_and_missing_company_context_are_distinct() -> None:
    result = expected_rtf(changes(full_record(amount="")), "price-changes", CONTEXT)
    assert result["assertions"][1]["presence"] == {"amount_ore": "blank"}
    data = introductions().replace(
        paragraph((296, 4592), ["Holder", "Company"]).encode("cp1252"), b""
    )
    with pytest.raises(ValueError, match="missing_introduction_company_context"):
        expected_rtf(data, "introductions", CONTEXT)


def test_every_part_of_repeated_introduction_header_is_required() -> None:
    data = introductions().replace(
        paragraph((116, 4592), ["MF-indehaver", "Repræsentant"]).encode("cp1252"), b""
    )
    with pytest.raises(ValueError, match="missing_introduction_header"):
        expected_rtf(data, "introductions", CONTEXT)


def test_discontinued_inherited_records_have_no_invented_date() -> None:
    row = paragraph(
        (90, 2386, 4856, 6522, 8894, 11364, 12169),
        ["Medicine", "tablet", "1 mg", "Company", "Holder", "000001", "20 stk"],
    )
    second = r"\tab Other\tab tablet\tab 2 mg\tab Company\tab Holder\tab 000002\tab 30 stk\par"
    row = row[:-1] + second + "}"
    data = document(
        [
            paragraph((113, 6810), ["Slettede pakninger", "06-01-2020"]),
            paragraph(
                tuple(range(7)),
                [
                    "Navn",
                    "Form",
                    "Styrke",
                    "Repræsentant",
                    "MT indehaver",
                    "Varenr",
                    "Pakningsstr.tekst",
                ],
            ),
            row,
            paragraph((90, 13943), ["", "Side 1 af 1"]),
        ]
    )
    result = expected_rtf(data, "discontinued", {})
    assert result["source_rows"] == 2
    events = [a for a in result["assertions"] if a["table"] == "lifecycle_event"]
    assert len(events) == 2
    assert all(a["effective_date"] is None and a["report_period"] is None for a in events)
    assert all(a["values"]["event_type"] == "unknown" for a in events)
    assert all(a["blocking_scope"] == "none" for a in result["issues"])


def test_invalid_identifier_is_counted_and_rejected() -> None:
    result = expected_rtf(changes(full_record(identifier="1234567")), "price-changes", CONTEXT)
    assert result["source_rows"] == 1
    assert result["assertions"] == []
    assert result["issues"][0]["code"] == "invalid_identifier"


def test_missing_page_and_unaccounted_text_are_rejected() -> None:
    with pytest.raises(ValueError, match="missing_page"):
        expected_rtf(changes(full_record(), footer=False), "price-changes", CONTEXT)
    with pytest.raises(ValueError, match="unaccounted_paragraph"):
        expected_rtf(
            changes(full_record(), paragraph((999,), ["unclassified evidence"])),
            "price-changes",
            CONTEXT,
        )


def test_no_production_reader_dependency() -> None:
    from pathlib import Path

    from bef_redbird.foundation import rtf_audit

    source = Path(rtf_audit.__file__).read_text()
    assert "from .adapters" not in source
    assert "striprtf" not in source
