from __future__ import annotations

import pytest

from bef_redbird.foundation.adapters.base import AdapterError
from bef_redbird.foundation.adapters.rtf import paragraphs
from bef_redbird.foundation.adapters.rtf_reports import (
    document,
    parse_changes,
    parse_discontinued,
    parse_introductions,
)


def rtf(*parts: str) -> bytes:
    return ("{\\rtf1\\ansi\\ansicpg1252" + "".join(parts) + "}").encode("cp1252")


def test_paragraph_positions_preserve_absent_stops_and_empty_tokens() -> None:
    result = paragraphs(
        rtf(
            r"\pard\tx100\tx200\tx400{\tab 000007\tab \tab 1.234,50\par}",
            r"\pard\tx100\tx300\tx400{\tab 000008\tab J\tab 0,00\par}",
        )
    )
    assert result[0].positioned() == {100: "000007", 200: "", 400: "1.234,50"}
    assert result[1].positioned() == {100: "000008", 300: "J", 400: "0,00"}
    assert result[0].locator == "rtf:page:1!paragraph:1"


def test_paragraph_decoding_preserves_ansi_unicode_escapes_and_pages() -> None:
    result = paragraphs(
        rtf(
            "\\pard\\tx100{Blå\\par}{\\page}",
            r"\pard\tx100\tx200{\'e6\u248?\tab \{text\}\\\par}",
        )
    )
    assert result[0].text == "Blå"
    assert result[1].cells == ("æø", "{text}\\")
    assert result[1].page == 2


@pytest.mark.parametrize("control", ("trowd", "cell", "row", "object", "field", "pict", "bin"))
def test_other_structures_cannot_silently_lose_material(control: str) -> None:
    with pytest.raises(AdapterError, match="unsupported_rtf_structure"):
        paragraphs(rtf("\\pard\\tx100{\\" + control + " text\\par}"))


def test_unknown_paragraph_and_tab_boundaries_fail_closed() -> None:
    with pytest.raises(AdapterError, match="rtf_paragraph_boundary"):
        paragraphs(rtf(r"\pard\tx100{first}"))
    with pytest.raises(AdapterError, match="rtf_tab_alignment"):
        paragraphs(rtf(r"\pard\tx100{\tab first\tab second\par}"))[0].positioned()
    with pytest.raises(AdapterError, match="unaccounted_rtf_paragraph"):
        paragraphs(rtf(r"{lost\par}\pard\tx100{known\par}"))


def test_inherited_paragraph_stops_are_retained_for_older_exports() -> None:
    result = paragraphs(
        rtf(
            r"\pard\tx100\tx200{\tab 000007\tab One\par"
            r"\tab 000008\tab Two\par}"
        )
    )
    assert [p.positioned() for p in result] == [
        {100: "000007", 200: "One"},
        {100: "000008", 200: "Two"},
    ]


def test_unsupported_codepage_is_not_replacement_decoded() -> None:
    with pytest.raises(AdapterError, match="unsupported_rtf_codepage"):
        paragraphs(b"{\\rtf1\\ansi\\ansicpg932\\pard\\tx100{body\\par}}")


def test_inherited_group_closure_cannot_erase_following_records() -> None:
    result = paragraphs(
        rtf(
            r"\pard\tx100\tx200{\tab 000007\tab One\par}"
            r"{\tab 000008\tab Two\par}"
            r"{\tab 000009\tab Three\par}"
        )
    )
    assert [p.cells for p in result] == [("000007", "One"), ("000008", "Two"), ("000009", "Three")]


def test_document_font_context_is_preserved_across_paragraphs() -> None:
    data = (
        r"{\rtf1\ansi\ansicpg1252\deff0"
        r"{\fonttbl{\f0\fcharset0 Arial;}{\f1\fcharset204 Cyrillic;}}"
        r"\pard\tx100{\f1 \'e0\par}{\f0 \'e0\par}}"
    ).encode("ascii")
    assert [p.text for p in paragraphs(data)] == ["а", "à"]


def test_escaped_control_words_are_literal_source_text() -> None:
    result = paragraphs(rtf(r"\pard\tx100{literal \\par \\pard \\page\par}"))
    assert len(result) == 1
    assert result[0].text == r"literal \par \pard \page"
    assert result[0].page == 1


def test_original_marker_character_cannot_create_phantom_paragraphs() -> None:
    with pytest.raises(AdapterError, match="unaccounted_rtf_paragraph"):
        paragraphs(rtf(r"\pard\tx100{\uc0\u30\par}"))


def paragraph(stops: list[int], cells: list[str], *, bold: bool = False) -> str:
    return (
        r"\pard"
        + "".join(r"\tx" + str(x) for x in stops)
        + "{"
        + (r"\b " if bold else "")
        + "".join(r"\tab " + cell for cell in cells)
        + r"\par}"
    )


def changes_document(*, wrapped: bool = False) -> bytes:
    return rtf(
        paragraph([90, 4759], ["Prisændringer", "05-01-2026"]),
        paragraph(
            [120, 3120, 5430, 7365, 8145, 10950, 12120, 12915, 13860, 14984],
            [
                "Firma, Navn",
                "Form",
                "Styrke",
                "Vnr",
                "Pakningstekst",
                "Ny AIP",
                "Ny .reg. pris",
                "Gl.AIP",
                "Gl. reg. pris",
                "Prisforskel",
            ],
        ),
        paragraph([90], ["Invented company"], bold=True),
        paragraph(
            [90, 2955, 5445, 7365, 8170, 11115, 12075, 12960, 13890, 14912, 15002],
            [
                "Invented medicine",
                "tablets",
                "10 mg",
                "000007",
                "20 stk.",
                "1.234," if wrapped else "1.234,50",
                "2.345,60",
                "0,00",
                "",
                "4,2",
                "%",
            ],
        ),
        paragraph([2955, 11115], ["extended release", "50"]) if wrapped else "",
        paragraph([90, 13946], ["2. januar 2026", "Side 1 af 1"]),
    )


def test_changes_exact_money_wrapped_fields_and_previous_quote_context() -> None:
    parsed = parse_changes(
        changes_document(wrapped=True),
        {
            "current_period": "2026-01-05",
            "tax_basis": "unknown",
            "fee_basis": "unknown",
        },
    )
    assert parsed.records == 1
    assert len(parsed.assertions) == 5
    assert parsed.assertions[0].values["form"] == "tablets extended release"
    assert [a.values["amount_ore"] for a in parsed.assertions[1:]] == [
        123450,
        234560,
        0,
        None,
    ]
    assert parsed.assertions[3].effective_period is None
    assert parsed.assertions[3].report_period.isoformat() == "2026-01-05"
    assert parsed.assertions[4].presence["amount_ore"] == "blank"
    assert len(parsed.assertions[1].raw["fragments"]) == 2
    assert document(changes_document()).page_count == 1


def test_rtf_missing_page_and_mismatched_period_cannot_pass_as_complete() -> None:
    with pytest.raises(AdapterError, match="rtf_missing_page_or_header"):
        document(changes_document().replace(b"Side 1 af 1", b"Side 1 af 2"))
    with pytest.raises(AdapterError, match="declared_observed_context_conflict"):
        parse_changes(changes_document(), {"current_period": "2026-01-19"})


def test_intro_removed_optional_tab_does_not_shift_prices_or_storage() -> None:
    data = rtf(
        paragraph([3428, 12019], ["Nye pakninger på nye formuleringer", "05-01-2026"]),
        paragraph([90, 4038, 8555], ["Navn", "Form", "Styrke"]),
        paragraph([116, 4592], ["MF-indehaver", "Repræsentant"]),
        paragraph(
            [116, 2040, 5612, 7880, 9240, 11956, 13424],
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
        paragraph([90, 4038, 8555], ["Example", "tablet", "10 mg"]),
        paragraph([296, 4592], ["Holder", "Company"]),
        paragraph([536, 1530, 8367, 12506, 13974], ["000007", "10 stk.", "B", "2,05", "3,10"]),
        paragraph([90, 13886], ["2. januar 2026", "Side 1 af 1"]),
    )
    parsed = parse_introductions(
        data,
        {
            "current_period": "2026-01-05",
            "tax_basis": "unknown",
            "fee_basis": "unknown",
        },
    )
    assert len(parsed.assertions) == 4
    assert parsed.assertions[0].values["storage_code"] is None
    assert [a.values.get("amount_ore") for a in parsed.assertions[1:3]] == [205, 310]
    assert parsed.assertions[3].values["event_type"] == "introduction"


def test_old_discontinued_source_does_not_invent_a_specific_event_or_effective_date() -> None:
    data = rtf(
        paragraph([113, 11016], ["Slettede pakninger", "05-01-2026"]),
        paragraph(
            [90, 2386, 4856, 6519, 8900, 11985, 12169],
            [
                "Navn",
                "Form",
                "Styrke",
                "Repræsentant",
                "MT indehaver",
                "Varenr.",
                "Pakningsstr.tekst",
            ],
        ),
        paragraph(
            [90, 2386, 4856, 6522, 8894, 11364, 12169],
            [
                "Example",
                "tablet",
                "10 mg",
                "Company",
                "Holder",
                "000007",
                "10 stk.",
            ],
        ),
        paragraph([90, 13943], ["2. januar 2026", "Side 1 af 1"]),
    )
    parsed = parse_discontinued(data, {})
    assert len(parsed.assertions) == 2
    event = parsed.assertions[1]
    assert event.values["event_type"] == "unknown"
    assert event.effective_date is None
    assert event.report_period is None
    assert event.raw["reported_date"] == "2026-01-05"
