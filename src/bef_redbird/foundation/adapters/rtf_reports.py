"""Versioned archive RTF report interpretations, with explicit physical accounting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .base import AdapterError, Assertion, Parsed, Problem, field_state, identifier, money
from .rtf import Paragraph, paragraphs
from .workbooks import _PACKAGE_FIELDS, _price_context

TITLES = {
    "Prisændringer": "price-changes",
    "Nye pakninger på nye formuleringer": "introductions",
    "Nye pakninger på eksisterende formuleringer": "introductions",
    "Slettede pakninger": "discontinued",
}
CHANGE_FIELDS = dict(
    zip(
        (90, 2955, 5445, 7365, 8170, 11115, 12075, 12960, 13890, 14912, 15002),
        (
            "name",
            "form",
            "strength",
            "identifier",
            "pack_description",
            "new_aip",
            "new_registered",
            "old_aip",
            "old_registered",
            "percentage",
            "percent_sign",
        ),
        strict=True,
    )
)
STOP_FIELDS = dict(
    zip(
        (90, 2386, 4856, 6522, 8894, 11364, 12169),
        ("name", "form", "strength", "company", "holder", "identifier", "pack_description"),
        strict=True,
    )
)
INTRO_FIELDS = {
    341: "identifier",
    371: "identifier",
    536: "identifier",
    1530: "pack_description",
    1605: "pack_description",
    2104: "pack_description",
    6042: "storage_code",
    6685: "storage_code",
    7856: "dispensing",
    8367: "dispensing",
    9134: "traffic",
    9861: "traffic",
    12506: "new_aip",
    13974: "new_registered",
}


@dataclass
class Record:
    paragraph: Paragraph
    values: dict[str, str]
    fragments: list[dict[str, Any]] = field(default_factory=list)
    locators: dict[str, str] = field(default_factory=dict)

    def append(self, paragraph: Paragraph, mapping: dict[int, str], *, join: bool = False) -> None:
        positioned = _positions(paragraph)
        if set(positioned) - set(mapping):
            raise AdapterError("unsupported_rtf_coordinates", paragraph.locator)
        self.fragments.append(
            {
                "locator": paragraph.locator,
                "stops": list(paragraph.stops),
                "cells": list(paragraph.cells),
            }
        )
        for stop, value in positioned.items():
            key = mapping[stop]
            if join and key in self.values:
                # Money is split inside a token; descriptive text wraps between words.
                separator = (
                    ""
                    if key
                    in {"new_aip", "new_registered", "old_aip", "old_registered", "identifier"}
                    else " "
                )
                self.values[key] += separator + value.strip()
            else:
                self.values[key] = value.strip()
            self.locators.setdefault(key, f"{paragraph.locator}!x:{stop}")


@dataclass
class Document:
    family: str
    title: str
    reported_date: date
    records: list[Record]
    accounting: list[dict[str, Any]]
    page_count: int


def _positions(paragraph: Paragraph) -> dict[int, str]:
    """Trailing unprinted fields remain blank; interior alignment must be exact."""
    if len(paragraph.cells) > len(paragraph.stops):
        raise AdapterError("rtf_tab_alignment", paragraph.locator)
    if len(set(paragraph.stops)) != len(paragraph.stops):
        raise AdapterError("rtf_tab_alignment", paragraph.locator)
    return {
        stop: paragraph.cells[i] if i < len(paragraph.cells) else ""
        for i, stop in enumerate(paragraph.stops)
    }


def document(data: bytes) -> Document:
    """Account for every paragraph, including repeated headers and continuations."""
    parts = paragraphs(data)
    if not parts:
        raise AdapterError("empty_rtf_document")
    first = [v.strip() for v in parts[0].cells]
    if len(first) != 2 or first[0] not in TITLES:
        raise AdapterError("unsupported_rtf_title")
    try:
        reported = datetime.strptime(first[1], "%d-%m-%Y").date()
        if reported.strftime("%d-%m-%Y") != first[1]:
            raise ValueError
    except ValueError as exc:
        raise AdapterError("invalid_rtf_report_date") from exc
    title, family = first[0], TITLES[first[0]]
    records: list[Record] = []
    accounting = []
    footers: list[tuple[int, int]] = []
    intro_headers: dict[int, list[str]] = {}
    headers = 0
    company: str | None = None
    intro: Record | None = None
    intro_has_package = True
    for part in parts:
        cells = tuple(v.strip() for v in part.cells)
        kind = ""
        if not part.text.strip():
            kind = "blank_paragraph"
        elif len(cells) == 2 and cells[0] in TITLES:
            if cells != (title, first[1]):
                raise AdapterError("conflicting_rtf_report_context", part.locator)
            kind = "report_title"
        elif len(cells) == 2 and re.fullmatch(r"Side \d+ af \d+", cells[-1]):
            match = re.fullmatch(r"Side (\d+) af (\d+)", cells[-1])
            assert match
            footers.append((int(match[1]), int(match[2])))
            if int(match[1]) != part.page:
                raise AdapterError("rtf_page_sequence_mismatch", part.locator)
            kind = "page_footer"
        elif family == "price-changes" and cells[:1] == ("Firma, Navn",):
            if (
                len(cells) != 10
                or cells[1:6] != ("Form", "Styrke", "Vnr", "Pakningstekst", "Ny AIP")
                or cells[6] not in {"Ny .reg.", "Ny .reg. pris"}
                or cells[7:] != ("Gl.AIP", "Gl. reg. pris", "Prisforskel")
            ):
                raise AdapterError("unsupported_rtf_header", part.locator)
            headers += 1
            kind = "column_header"
        elif family == "discontinued" and cells[:1] == ("Navn",):
            if (
                len(cells) != 7
                or cells[:5] != ("Navn", "Form", "Styrke", "Repræsentant", "MT indehaver")
                or cells[5] not in {"Varenr.", "Varenr"}
                or cells[6] != "Pakningsstr.tekst"
            ):
                raise AdapterError("unsupported_rtf_header", part.locator)
            headers += 1
            kind = "column_header"
        elif family == "introductions" and cells in {
            ("Navn", "Form", "Styrke"),
            ("MF-indehaver", "Repræsentant"),
            (
                "Varenr.",
                "Pakningsstørrelse",
                "Opbevaringsbetingelse",
                "Udlevering",
                "Trafikadvarsel",
                "AIP",
                "Reg.pris",
            ),
        }:
            intro_headers.setdefault(part.page, []).append(cells[0])
            headers += int(cells[0] == "Varenr.")
            kind = "column_header"
        elif family == "price-changes":
            if part.stops == (90,) and re.search(r"\\b\b", part.raw):
                company = cells[0]
                kind = "company_context"
            elif part.stops == tuple(CHANGE_FIELDS):
                if company is None:
                    raise AdapterError("missing_rtf_company_context", part.locator)
                record = Record(part, {"company": company})
                record.append(part, CHANGE_FIELDS)
                records.append(record)
                kind = "record"
            elif records and set(part.stops) <= set(CHANGE_FIELDS):
                records[-1].append(part, CHANGE_FIELDS, join=True)
                kind = "record_continuation"
        elif family == "discontinued":
            if 11364 in part.stops and set(part.stops) <= set(STOP_FIELDS):
                record = Record(part, {})
                record.append(part, STOP_FIELDS)
                records.append(record)
                kind = "record"
        elif family == "introductions":
            if part.stops == (90, 4038, 8555):
                if not intro_has_package:
                    raise AdapterError("rtf_introduction_without_package", part.locator)
                intro = Record(part, {})
                intro.append(part, {90: "name", 4038: "form", 8555: "strength"})
                intro_has_package = False
                kind = "formulation_context"
            elif intro is not None and set(part.stops) <= {296, 4592, 4772, 5102}:
                intro.append(
                    part, {296: "holder", 4592: "company", 4772: "company", 5102: "company"}
                )
                kind = "company_context"
            elif (
                intro is not None
                and set(part.stops) <= set(INTRO_FIELDS)
                and any(stop in part.stops for stop in (341, 371, 536))
            ):
                if "company" not in intro.values:
                    raise AdapterError("missing_rtf_company_context", part.locator)
                record = Record(
                    part, dict(intro.values), list(intro.fragments), dict(intro.locators)
                )
                record.append(part, INTRO_FIELDS)
                records.append(record)
                intro_has_package = True
                kind = "record"
        if not kind:
            raise AdapterError("unaccounted_rtf_paragraph", part.locator)
        accounting.append({"locator": part.locator, "kind": kind})
    if not intro_has_package:
        raise AdapterError("rtf_introduction_without_package")
    totals = {total for _, total in footers}
    if len(totals) != 1:
        raise AdapterError("rtf_missing_page_totals")
    total = next(iter(totals))
    if [n for n, _ in footers] != list(range(1, total + 1)) or headers != total:
        raise AdapterError("rtf_missing_page_or_header")
    if family == "introductions" and intro_headers != {
        page: ["Navn", "MF-indehaver", "Varenr."] for page in range(1, total + 1)
    }:
        raise AdapterError("rtf_missing_page_or_header")
    return Document(family, title, reported, records, accounting, total)


def parse(data: bytes, context: dict[str, Any], family: str) -> Parsed:
    doc = document(data)
    if doc.family != family:
        raise AdapterError("rtf_family_mismatch")
    supplied = context.get("current_period")
    current = date.fromisoformat(supplied) if supplied is not None else None
    if current is not None and current != doc.reported_date:
        raise AdapterError("declared_observed_context_conflict")
    if family != "discontinued" and current is None:
        raise AdapterError("missing_report_date")
    if family == "discontinued" and current is not None:
        raise AdapterError("unestablished_report_period")
    bases = _price_context(context, cito=False) if family != "discontinued" else {}
    parsed = Parsed(records=len(doc.records))
    for record in doc.records:
        values = record.values
        raw_identifier = values.get("identifier", "")
        locator = record.paragraph.locator
        try:
            canonical = identifier(raw_identifier)
        except ValueError:
            parsed.issues.append(
                Problem(
                    record.locators.get("identifier", locator),
                    "invalid_identifier",
                    field="source_identifier",
                )
            )
            continue
        raw = {
            "source_identifier": raw_identifier,
            "report_title": doc.title,
            "reported_date": doc.reported_date.isoformat(),
            "fields": dict(values),
            "fragments": record.fragments,
        }
        attributes = {key: values.get(key) or None for key in _PACKAGE_FIELDS}
        presence = {
            key: field_state(values.get(key)) if key in values else "unreported"
            for key in _PACKAGE_FIELDS
        }
        parsed.assertions.append(
            Assertion(
                "package_observation",
                locator,
                raw_identifier,
                attributes,
                presence,
                raw,
                current,
                current,
            )
        )
        if family in {"price-changes", "introductions"}:
            prices = [
                ("new_aip", "aip", "effective_price"),
                ("new_registered", "registered", "effective_price"),
            ]
            if family == "price-changes":
                prices.extend(
                    [
                        ("old_aip", "aip", "reported_previous_price"),
                        ("old_registered", "registered", "reported_previous_price"),
                    ]
                )
            for key, price_type, role in prices:
                token = values.get(key)
                state = field_state(token, key in values)
                effective = current if role == "effective_price" else None
                price_locator = record.locators.get(key, locator + "!field:" + key)
                try:
                    amount = money(token)
                except ValueError as exc:
                    amount, state = None, "invalid"
                    parsed.issues.append(
                        Problem(
                            price_locator,
                            str(exc),
                            identifier=canonical,
                            field="amount_ore",
                            period=effective,
                        )
                    )
                parsed.assertions.append(
                    Assertion(
                        "price_observation",
                        price_locator,
                        raw_identifier,
                        {
                            **bases,
                            "price_type": price_type,
                            "amount_ore": amount,
                            "price_role": role,
                        },
                        {"amount_ore": state},
                        raw,
                        effective,
                        current,
                        quality_state="invalid" if state == "invalid" else "valid",
                    )
                )
        if family == "introductions":
            parsed.assertions.append(
                Assertion(
                    "lifecycle_event",
                    locator,
                    raw_identifier,
                    {
                        "event_type": "introduction",
                        "original_source_code": doc.title,
                        "interpretation_status": "established",
                    },
                    {"effective_date": "present"},
                    raw,
                    report_period=current,
                    effective_date=doc.reported_date,
                )
            )
        elif family == "discontinued":
            # The older header does not distinguish deactivation/permanent deletion,
            # nor supply row-level event dates. Keep a reported event with unknown
            # interpretation and do not turn the title date into an effective date.
            parsed.issues.append(
                Problem(
                    locator,
                    "undifferentiated_discontinuation_semantics",
                    blocking_scope="none",
                    identifier=canonical,
                    field="event_type",
                    severity="warning",
                )
            )
            parsed.assertions.append(
                Assertion(
                    "lifecycle_event",
                    locator,
                    raw_identifier,
                    {
                        "event_type": "unknown",
                        "original_source_code": doc.title,
                        "interpretation_status": "unknown",
                    },
                    {"effective_date": "unreported"},
                    raw,
                    quality_state="incomplete",
                )
            )
    return parsed


def parse_changes(data: bytes, context: dict[str, Any]) -> Parsed:
    return parse(data, context, "price-changes")


def parse_introductions(data: bytes, context: dict[str, Any]) -> Parsed:
    return parse(data, context, "introductions")


def parse_discontinued(data: bytes, context: dict[str, Any]) -> Parsed:
    return parse(data, context, "discontinued")
