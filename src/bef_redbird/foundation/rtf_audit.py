"""Independent native RTF evidence reader for the historical report audit.

No production adapter, text decoder or converter is used here. Text is interpreted
as a stream of RTF tokens, including inherited paragraph properties. Coordinates
refer to actual tab stops, so an omitted stop cannot shift subsequent fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)")
_DESTINATIONS = {
    "fonttbl",
    "colortbl",
    "stylesheet",
    "info",
    "pict",
    "object",
    "header",
    "footer",
    "filetbl",
    "listtable",
    "listoverridetable",
    "generator",
    "xmlnstbl",
    "datastore",
}
_TITLES = {
    "Prisændringer": "price-changes",
    "Nye pakninger på nye formuleringer": "introductions",
    "Nye pakninger på eksisterende formuleringer": "introductions",
    "Slettede pakninger": "discontinued",
}
_ATTRS = ("name", "form", "strength", "pack_description", "company", "storage_code", "atc")


@dataclass(frozen=True)
class NativeParagraph:
    number: int
    page: int
    stops: tuple[int, ...]
    cells: tuple[str, ...]
    bold: bool

    @property
    def locator(self) -> str:
        return f"rtf:page:{self.page}!paragraph:{self.number}"


def read_native_rtf(data: bytes) -> list[NativeParagraph]:
    """Decode the original token stream; reject unsupported structural content."""
    if not data.startswith(b"{\\rtf"):
        raise ValueError("independent_not_rtf")
    source = data.decode("cp1252")
    state: dict[str, Any] = {"skip": False, "uc": 1, "stops": (), "bold": False}
    stack: list[dict[str, Any]] = []
    result: list[NativeParagraph] = []
    chars: list[str] = []
    page = 1
    paragraph_page = 1
    active = False
    body_started = False
    pending_spaces = ""
    bold = False
    fallback = 0
    stops: tuple[int, ...] = ()

    def append(value: str) -> None:
        nonlocal active, paragraph_page, bold, stops, pending_spaces
        if not active:
            active, paragraph_page, stops = True, page, state["stops"]
        bold |= state["bold"]
        if pending_spaces:
            chars.append(pending_spaces)
            pending_spaces = ""
        chars.append(value)

    for token in _TOKEN.finditer(source):
        word, arg, hexbyte, symbol, brace, literal = token.groups()
        if brace == "{":
            stack.append(dict(state))
        elif brace == "}":
            if not stack:
                raise ValueError("independent_rtf_unbalanced_groups")
            state = stack.pop()
        elif symbol == "*":
            state["skip"] = True
        elif word in _DESTINATIONS:
            state["skip"] = True
        elif state["skip"]:
            continue
        elif word == "ansicpg" and arg != "1252":
            raise ValueError("independent_rtf_unsupported_codepage")
        elif word == "uc":
            state["uc"] = int(arg or "1")
        elif word == "u":
            append(chr(int(arg or "0") % 65536))
            fallback = state["uc"]
        elif word == "pard":
            if chars:
                raise ValueError("independent_rtf_unterminated_paragraph")
            state["stops"] = ()
            active = False
            body_started = True
        elif word == "tx":
            state["stops"] += (int(arg or "0"),)
        elif word == "b":
            state["bold"] = arg != "0"
        elif word == "plain":
            state["bold"] = False
        elif word == "page":
            if chars:
                raise ValueError("independent_rtf_page_inside_paragraph")
            page += 1
        elif word == "par":
            if not active:
                paragraph_page, stops = page, state["stops"]
            try:
                text = (
                    ("".join(chars) + pending_spaces)
                    .encode("utf-16-le", "surrogatepass")
                    .decode("utf-16-le")
                )
            except UnicodeError as exc:
                raise ValueError("independent_rtf_invalid_unicode") from exc
            cells = text.split("\t")
            if len(cells) > 1 and not cells[0].strip():
                cells.pop(0)
            result.append(
                NativeParagraph(len(result) + 1, paragraph_page, stops, tuple(cells), bold)
            )
            chars, active, bold = [], False, False
            pending_spaces = ""
        elif word in {"tab", "line"}:
            append("\t" if word == "tab" else "\n")
        elif word in {"bin", "trowd", "cell", "row"}:
            raise ValueError("independent_rtf_unsupported_structure")
        elif hexbyte or symbol in {"\\", "{", "}", "~", "_", "-"} or literal:
            value = (
                bytes.fromhex(hexbyte).decode("cp1252")
                if hexbyte
                else {"~": "\u00a0", "_": "\u2011", "-": "\u00ad"}.get(symbol, symbol)
                if symbol
                else (literal or "").replace("\r", "").replace("\n", "")
            )
            if fallback:
                consumed = min(fallback, len(value))
                fallback -= consumed
                value = value[consumed:]
            if value:
                if not active and not value.strip():
                    # Literal spaces survive intervening formatting controls.
                    # A control-word delimiter was already consumed by _TOKEN.
                    if body_started:
                        pending_spaces += value
                    continue
                append(value)
    if stack or chars:
        raise ValueError("independent_rtf_unterminated_document")
    if not result:
        raise ValueError("independent_rtf_empty_document")
    return result


def _ore(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    if not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+)(?:,[0-9]+)?", value):
        raise ValueError("malformed_money")
    try:
        number = Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("malformed_money") from exc
    if number < 0:
        raise ValueError("negative_money")
    with localcontext() as decimal_context:
        decimal_context.prec = max(28, len(number.as_tuple().digits) + 3)
        amount = number * 100
    if amount != amount.to_integral_value():
        raise ValueError("unsupported_money_precision")
    if amount > 2**63 - 1:
        raise ValueError("money_overflow")
    return int(amount)


def expected_rtf(data: bytes, adapter: str, context: dict[str, Any]) -> dict[str, Any]:
    """Derive every expected assertion and account for every native paragraph."""
    paragraphs = read_native_rtf(data)
    first = tuple(c.strip() for c in paragraphs[0].cells)
    if len(first) != 2 or _TITLES.get(first[0]) != adapter:
        raise ValueError("independent_rtf_title_family_mismatch")
    title, date_token = first
    try:
        reported = datetime.strptime(date_token, "%d-%m-%Y").date()
        if reported.strftime("%d-%m-%Y") != date_token:
            raise ValueError
    except ValueError as exc:
        raise ValueError("independent_rtf_invalid_date") from exc
    current = context.get("current_period")
    if adapter == "discontinued":
        if current is not None:
            raise ValueError("independent_unestablished_report_period")
    elif current != reported.isoformat():
        raise ValueError("independent_rtf_report_context_conflict")
    change_positions = {
        90: "name",
        2955: "form",
        5445: "strength",
        7365: "identifier",
        8170: "pack_description",
        11115: "new_aip",
        12075: "new_registered",
        12960: "old_aip",
        13890: "old_registered",
        14912: "percentage",
        15002: "percent_sign",
    }
    discontinued_positions = {
        90: "name",
        2386: "form",
        4856: "strength",
        6522: "company",
        8894: "holder",
        11364: "identifier",
        12169: "pack_description",
    }
    introduction_positions = {
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
    records: list[dict[str, Any]] = []
    accounting: list[dict[str, str]] = []
    footers: list[tuple[int, int]] = []
    header_pages: list[int] = []
    introduction_headers: dict[str, list[int]] = {"Navn": [], "MF-indehaver": []}
    company = None
    formulation: dict[str, Any] | None = None
    awaiting_package = False

    def record(part: NativeParagraph) -> dict[str, Any]:
        return {"locator": part.locator, "fields": {}, "fragments": [], "locators": {}}

    def add(
        target: dict[str, Any], part: NativeParagraph, columns: dict[int, str], wrap: bool = False
    ) -> None:
        if len(part.cells) > len(part.stops) or len(set(part.stops)) != len(part.stops):
            raise ValueError("independent_rtf_tab_geometry")
        if set(part.stops) - columns.keys():
            raise ValueError("independent_rtf_unknown_coordinate")
        target["fragments"].append(
            {"locator": part.locator, "stops": list(part.stops), "cells": list(part.cells)}
        )
        for index, stop in enumerate(part.stops):
            key = columns[stop]
            value = part.cells[index].strip() if index < len(part.cells) else ""
            if wrap and key in target["fields"]:
                separator = (
                    ""
                    if key
                    in {"identifier", "new_aip", "old_aip", "new_registered", "old_registered"}
                    else " "
                )
                target["fields"][key] += separator + value
            else:
                target["fields"][key] = value
            target["locators"].setdefault(key, f"{part.locator}!x:{stop}")

    for part in paragraphs:
        cells = tuple(c.strip() for c in part.cells)
        kind = ""
        if not any(cells):
            kind = "blank_paragraph"
        elif cells == first:
            kind = "report_title"
        elif cells[0] in _TITLES:
            raise ValueError("independent_rtf_conflicting_title")
        elif re.fullmatch(r"Side [0-9]+ af [0-9]+", cells[-1]):
            if len(cells) != 2:
                raise ValueError("independent_rtf_footer_geometry")
            match = re.fullmatch(r"Side ([0-9]+) af ([0-9]+)", cells[-1])
            assert match
            number, total = int(match[1]), int(match[2])
            if number != part.page:
                raise ValueError("independent_rtf_page_sequence")
            footers.append((number, total))
            kind = "page_footer"
        elif adapter == "price-changes" and cells[:1] == ("Firma, Navn",):
            if (
                cells[:6] != ("Firma, Navn", "Form", "Styrke", "Vnr", "Pakningstekst", "Ny AIP")
                or cells[6:7] not in {("Ny .reg.",), ("Ny .reg. pris",)}
                or cells[7:] != ("Gl.AIP", "Gl. reg. pris", "Prisforskel")
            ):
                raise ValueError("independent_rtf_column_header")
            header_pages.append(part.page)
            kind = "column_header"
        elif adapter == "discontinued" and cells[:1] == ("Navn",):
            if (
                cells[:5] != ("Navn", "Form", "Styrke", "Repræsentant", "MT indehaver")
                or cells[5:6] not in {("Varenr.",), ("Varenr",)}
                or cells[6:] != ("Pakningsstr.tekst",)
            ):
                raise ValueError("independent_rtf_column_header")
            header_pages.append(part.page)
            kind = "column_header"
        elif adapter == "introductions" and cells in {
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
            if cells[0] == "Varenr.":
                header_pages.append(part.page)
            else:
                introduction_headers[cells[0]].append(part.page)
            kind = "column_header"
        elif adapter == "price-changes":
            if part.stops == (90,) and part.bold:
                company, kind = cells[0], "company_context"
            elif part.stops == tuple(change_positions):
                if company is None:
                    raise ValueError("independent_rtf_missing_company")
                item = record(part)
                item["fields"]["company"] = company
                add(item, part, change_positions)
                records.append(item)
                kind = "record"
            elif records and set(part.stops) <= change_positions.keys():
                add(records[-1], part, change_positions, True)
                kind = "record_continuation"
        elif adapter == "discontinued":
            if 11364 in part.stops:
                item = record(part)
                add(item, part, discontinued_positions)
                records.append(item)
                kind = "record"
        elif adapter == "introductions":
            if part.stops == (90, 4038, 8555):
                if awaiting_package:
                    raise ValueError("independent_rtf_orphan_formulation")
                formulation = record(part)
                add(formulation, part, {90: "name", 4038: "form", 8555: "strength"})
                awaiting_package, kind = True, "formulation_context"
            elif formulation is not None and set(part.stops) <= {296, 4592, 4772, 5102}:
                add(
                    formulation,
                    part,
                    {296: "holder", 4592: "company", 4772: "company", 5102: "company"},
                )
                kind = "company_context"
            elif (
                formulation is not None
                and set(part.stops) <= introduction_positions.keys()
                and set(part.stops) & {341, 371, 536}
            ):
                if "company" not in formulation["fields"]:
                    raise ValueError("independent_rtf_missing_introduction_company_context")
                item = record(part)
                for key in ("fields", "locators"):
                    item[key] = dict(formulation[key])
                item["fragments"] = list(formulation["fragments"])
                add(item, part, introduction_positions)
                records.append(item)
                awaiting_package, kind = False, "record"
        if not kind:
            raise ValueError("independent_rtf_unaccounted_paragraph:" + part.locator)
        accounting.append({"locator": part.locator, "kind": kind})
    if awaiting_package:
        raise ValueError("independent_rtf_orphan_formulation")
    if not footers or len({total for _, total in footers}) != 1:
        raise ValueError("independent_rtf_missing_page_totals")
    total = footers[0][1]
    if [n for n, _ in footers] != list(range(1, total + 1)) or header_pages != list(
        range(1, total + 1)
    ):
        raise ValueError("independent_rtf_missing_page_or_header")
    if adapter == "introductions" and any(
        pages != list(range(1, total + 1)) for pages in introduction_headers.values()
    ):
        raise ValueError("independent_rtf_missing_introduction_header")
    assertions, issues = [], []
    for item in records:
        fields, locator = item["fields"], item["locator"]
        original = fields.get("identifier", "")
        if not re.fullmatch(r"[0-9]{1,6}", original):
            issues.append(
                {
                    "source_locator": item["locators"].get("identifier", locator),
                    "code": "invalid_identifier",
                    "identifier": None,
                    "field": "source_identifier",
                    "period": None,
                    "severity": "error",
                    "blocking_scope": "field",
                }
            )
            continue
        canonical = original.zfill(6)
        raw = {
            "source_identifier": original,
            "report_title": title,
            "reported_date": reported.isoformat(),
            "fields": dict(fields),
            "fragments": item["fragments"],
        }

        def assertion(
            table: str,
            where: str,
            values: dict[str, Any],
            presence: dict[str, str],
            effective: str | None = None,
            effective_date: str | None = None,
            quality: str = "valid",
            original_identifier: str = original,
            canonical_identifier: str = canonical,
            evidence: dict[str, Any] = raw,
        ) -> None:
            assertions.append(
                {
                    "table": table,
                    "source_locator": where,
                    "source_identifier": original_identifier,
                    "canonical_identifier": canonical_identifier,
                    "effective_period": effective,
                    "report_period": current,
                    "effective_date": effective_date,
                    "values": values,
                    "presence": presence,
                    "quality": quality,
                    "raw": evidence,
                }
            )

        assertion(
            "package_observation",
            locator,
            {key: fields.get(key) or None for key in _ATTRS},
            {
                key: "unreported" if key not in fields else "present" if fields[key] else "blank"
                for key in _ATTRS
            },
            current,
        )
        if adapter != "discontinued":
            for key, price_type, role in [
                ("new_aip", "aip", "effective_price"),
                ("new_registered", "registered", "effective_price"),
            ] + (
                [
                    ("old_aip", "aip", "reported_previous_price"),
                    ("old_registered", "registered", "reported_previous_price"),
                ]
                if adapter == "price-changes"
                else []
            ):
                token = fields.get(key)
                state = "unreported" if token is None else "present" if token else "blank"
                effective = current if role == "effective_price" else None
                where = item["locators"].get(key, locator + "!field:" + key)
                try:
                    amount = _ore(token)
                except ValueError as exc:
                    amount, state = None, "invalid"
                    issues.append(
                        {
                            "source_locator": where,
                            "code": str(exc),
                            "identifier": canonical,
                            "field": "amount_ore",
                            "period": effective,
                            "severity": "error",
                            "blocking_scope": "field",
                        }
                    )
                assertion(
                    "price_observation",
                    where,
                    {
                        "price_type": price_type,
                        "amount_ore": amount,
                        "currency": "DKK",
                        "price_basis": "per_package",
                        "tax_basis": context["tax_basis"],
                        "fee_basis": context["fee_basis"],
                        "price_role": role,
                    },
                    {"amount_ore": state},
                    effective,
                    quality="invalid" if state == "invalid" else "valid",
                )
        if adapter == "introductions":
            assertion(
                "lifecycle_event",
                locator,
                {
                    "event_type": "introduction",
                    "original_source_code": title,
                    "interpretation_status": "established",
                },
                {"effective_date": "present"},
                effective_date=reported.isoformat(),
            )
        elif adapter == "discontinued":
            issues.append(
                {
                    "source_locator": locator,
                    "code": "undifferentiated_discontinuation_semantics",
                    "identifier": canonical,
                    "field": "event_type",
                    "period": None,
                    "severity": "warning",
                    "blocking_scope": "none",
                }
            )
            assertion(
                "lifecycle_event",
                locator,
                {
                    "event_type": "unknown",
                    "original_source_code": title,
                    "interpretation_status": "unknown",
                },
                {"effective_date": "unreported"},
                quality="incomplete",
            )
    return {
        "assertions": assertions,
        "issues": issues,
        "source_rows": len(records),
        "sheet": "RTF",
        "header_row": None,
        "date_system": None,
        "metadata": {"report_title": title, "reported_date": reported.isoformat(), "pages": total},
        "physical_accounting": accounting,
    }
