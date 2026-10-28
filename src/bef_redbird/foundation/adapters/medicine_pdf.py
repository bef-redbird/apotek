"""Geometry-preserving reader for the printed Medicinpriser catalogue.

The native PDF table has two text bands per package. Columns are positioned,
not delimited: both text and right-aligned amounts can wrap. Baselines matter
because the reimbursement symbols use a different font and bounding-box height.
Printed ATC headings provide attributes, never substitution-group identities.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

import pymupdf

from .base import AdapterError, Assertion, Parsed, Problem, money
from .workbooks import _PACKAGE_FIELDS, _context_period

MEDICINE_PDF_VERSION = "medicine-prices-pdf-v1"
_DATE = re.compile(r"Priser for den (\d{2}\.\d{2}\.\d{4})")
_ATC = re.compile(r"Q?[A-Z]\d{2}[A-Z]{0,2}\d{0,2}")
_ID = re.compile(r"\d{6}")
_DISPENSING = {
    "A",
    "AP",
    "APK",
    "AP4",
    "AP4BG",
    "AP4NB",
    "B",
    "BEGR",
    "BP",
    "BPK",
    "GA",
    "GB",
    "GH",
    "HA",
    "HA18",
    "HF",
    "HP",
    "HPK",
    "HV",
    "HX",
    "HX18",
    "NBS",
    "PRMIX",
}
_MISSING_PRICE = {"-,--", "–,––", "–,– –"}


def _near(left: float, right: float) -> bool:
    return abs(left - right) <= 0.25


def _text(spans: list[dict[str, Any]], *, amount: bool = False) -> str:
    ordered = sorted(spans, key=lambda s: (s["origin"][1], s["origin"][0]))
    return ("" if amount else " ").join(s["text"] for s in ordered).strip()


def _position(span: dict[str, Any]) -> list[Any]:
    """Compact lossless span provenance; source indices identify the native text."""
    return [
        span["text"],
        span["bbox"],
        span["origin"],
        [span["block"], span["line"], span["span"]],
    ]


def _spans(page: Any) -> list[dict[str, Any]]:
    return [
        {
            "text": span["text"],
            "bbox": list(span["bbox"]),
            "origin": list(span["origin"]),
            "font": span["font"],
            "size": span["size"],
            "block": b,
            "line": n,
            "span": j,
        }
        for b, block in enumerate(page.get_text("dict")["blocks"])
        for n, line in enumerate(block.get("lines", []))
        for j, span in enumerate(line["spans"])
        if span["text"].strip()
    ]


def _period(text: str, locator: str) -> date:
    matches = _DATE.findall(text)
    if not matches:
        raise AdapterError("missing_report_date", locator)
    try:
        periods = {datetime.strptime(value, "%d.%m.%Y").date() for value in matches}
    except ValueError as exc:
        raise AdapterError("invalid_report_date", locator) from exc
    if len(periods) != 1:
        raise AdapterError("conflicting_report_dates", locator)
    return next(iter(periods))


def _document(data: bytes) -> Any:
    if not data.startswith(b"%PDF-"):
        raise AdapterError("unsupported_source_format")
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError) as exc:
        raise AdapterError("invalid_pdf") from exc
    if document.is_encrypted or document.page_count < 4:
        document.close()
        raise AdapterError("unsupported_pdf_catalogue")
    return document


def medicine_pdf_period(data: bytes) -> date:
    """Read the printed price date; this does not establish a tariff period.

    The name is retained for caller compatibility. Some catalogues print a date
    outside the week labelled on their cover, so callers must separately evidence
    an official tariff-period binding before supplying ``current_period``.
    """
    with _document(data) as document:
        text = document[0].get_text()
        if "Medicinpriser" not in text:
            raise AdapterError("unsupported_schema", "pdf:page:1")
        return _period(text, "pdf:page:1")


def _header(spans: list[dict[str, Any]]) -> bool:
    headings = {
        span["text"].strip() for span in spans if span["origin"][1] < 45 and span["origin"][0] > 430
    }
    return {"ESP pr. enhed", "ESP", "TSP"} <= headings


def _page_records(
    spans: list[dict[str, Any]], page_number: int
) -> Iterator[tuple[str, list[dict[str, Any]], dict[str, Any] | None]]:
    """Yield every left-column candidate; headings delimit the preceding row."""
    body = [span for span in spans if 40 <= span["origin"][1] < 790]
    starts = sorted(
        [
            span
            for span in body
            if _near(span["origin"][0], 35) and not _ATC.fullmatch(span["text"])
        ],
        key=lambda span: span["origin"][1],
    )
    headings = [
        span for span in body if _near(span["origin"][0], 35) and _ATC.fullmatch(span["text"])
    ]
    for index, start in enumerate(starts):
        baseline = start["origin"][1]
        stop = starts[index + 1]["origin"][1] if index + 1 < len(starts) else 790.0
        next_headings = [span["origin"][1] for span in headings if span["origin"][1] > baseline]
        if next_headings:
            stop = min(stop, min(next_headings))
        # A heading's larger letters can share a baseline only with its own text.
        record = [span for span in body if baseline - 0.1 <= span["origin"][1] < stop - 0.1]
        preceding = [span for span in headings if span["origin"][1] < baseline]
        heading = max(preceding, key=lambda span: span["origin"][1]) if preceding else None
        yield f"pdf:page:{page_number}:record:{index + 1}", record, heading


def _issue(
    parsed: Parsed, locator: str, code: str, identifier: str, field: str, period: date | None
) -> None:
    parsed.issues.append(Problem(locator, code, identifier=identifier, field=field, period=period))


def _record(
    parsed: Parsed,
    locator: str,
    spans: list[dict[str, Any]],
    atc: dict[str, Any] | None,
    printed_date: date,
    period: date | None,
    context: dict[str, Any],
) -> None:
    parsed.records += 1
    first = next(span for span in spans if _near(span["origin"][0], 35))
    source_id = first["text"]
    if not _ID.fullmatch(source_id):
        parsed.issues.append(
            Problem(locator, "invalid_identifier", blocking_scope="row", field="identifier")
        )
        return
    secondary = [
        span
        for span in spans
        if _near(span["origin"][0], 438.68)
        and span["origin"][1] > first["origin"][1] + 1
        and "Wingdings" not in span["font"]
    ]
    if len(secondary) != 1:
        parsed.issues.append(
            Problem(
                locator,
                "ambiguous_pdf_record_bands",
                blocking_scope="row",
                identifier=source_id,
                period=period,
            )
        )
        return
    split = secondary[0]["origin"][1] - 0.75
    main = [span for span in spans if span["origin"][1] < split]
    detail = [span for span in spans if span["origin"][1] >= split]
    columns = {
        "name": [span for span in main if _near(span["origin"][0], 82.96)],
        "form": [span for span in main if _near(span["origin"][0], 174.13)],
        "strength": [span for span in main if _near(span["origin"][0], 253.76)],
        "pack_description": [span for span in main if _near(span["origin"][0], 340.66)],
        "company": [span for span in detail if _near(span["origin"][0], 82.96)],
        "storage_code": [span for span in detail if _near(span["origin"][0], 174.13)],
        "ESP_per_unit": [span for span in main if _near(span["bbox"][2], 487.22)],
        "ESP": [span for span in main if _near(span["bbox"][2], 524.11)],
        "TSP": [span for span in main if _near(span["bbox"][2], 561)],
        "reimbursement_marker": [
            span
            for span in main
            if span["text"].strip() in {"l", "n", "U"} and 435 <= span["origin"][0] <= 440
        ],
        "dispensing": secondary,
        "prescribing_specialties": [span for span in detail if _near(span["bbox"][2], 435.68)],
        "abc": [
            span for span in detail if _near(span["bbox"][2], 485.02) and span["origin"][0] >= 450
        ],
        "dose_dispensable": [
            span for span in detail if _near(span["origin"][0], 554) and span["text"] == "D"
        ],
    }
    claimed = {id(span) for column in columns.values() for span in column}
    raw: dict[str, Any] = {
        "source_identifier": source_id,
        "source_columns": {field: _text(column) for field, column in columns.items()},
        "span_format": "text,bbox,origin,block-line-span-v1",
        "source_spans": [_position(span) for span in spans],
        "unmapped_spans": [
            [span["block"], span["line"], span["span"]]
            for span in spans
            if id(span) not in claimed and span is not first
        ],
        "atc_heading": (
            {"position": _position(atc), "source_locator": atc["source_locator"]} if atc else None
        ),
        "printed_price_date": printed_date.isoformat(),
        "printed_price_date_locator": "pdf:page:1",
        "price_definition_locator": "pdf:pages:2-3",
        "group_identity": "not_printed",
    }
    values: dict[str, Any] = dict.fromkeys(_PACKAGE_FIELDS)
    presence = dict.fromkeys(_PACKAGE_FIELDS, "blank")
    for field in _PACKAGE_FIELDS:
        if field == "atc":
            values[field] = atc["text"] if atc else None
            presence[field] = "present" if atc else "unreported"
        else:
            values[field] = _text(columns[field]) or None
            presence[field] = "present" if values[field] is not None else "blank"
    parsed.assertions.append(
        Assertion(
            "package_observation",
            locator,
            source_id,
            values,
            presence,
            raw,
            period,
            period,
            quality_state="incomplete" if period is None else "valid",
        )
    )
    for field, price_type, basis in (
        ("ESP_per_unit", "consumer", "per_unit"),
        ("ESP", "consumer", "per_package"),
        ("TSP", "reimbursement_basis", "per_package"),
    ):
        token = _text(columns[field], amount=True)
        state = "present"
        amount = None
        price_locator = f"{locator}:{field}"
        if not token:
            state = "missing"
            _issue(parsed, price_locator, "missing_pdf_price", source_id, "amount_ore", period)
        elif token in _MISSING_PRICE:
            state = "unreported"
        else:
            try:
                amount = money(token)
            except ValueError as exc:
                state = "invalid"
                _issue(parsed, price_locator, str(exc), source_id, "amount_ore", period)
        price_values = {
            "price_type": price_type,
            "amount_ore": amount,
            "currency": "DKK",
            "price_basis": basis,
            "tax_basis": (
                context.get("reimbursement_tax_basis", context["tax_basis"])
                if price_type == "reimbursement_basis"
                else context["tax_basis"]
            ),
            "fee_basis": (
                "includes_prescription_and_preparation_fees"
                if price_type == "consumer"
                else "reimbursement_basis"
            ),
            "price_role": "effective_price",
        }
        parsed.assertions.append(
            Assertion(
                "price_observation",
                price_locator,
                source_id,
                price_values,
                {"amount_ore": state},
                {
                    "source_identifier": source_id,
                    "identifier_span": [first["block"], first["line"], first["span"]],
                    "amount_source": token,
                    "amount_column": field,
                    "span_format": "text,bbox,origin,block-line-span-v1",
                    "amount_spans": [_position(span) for span in columns[field]],
                    "printed_price_date": printed_date.isoformat(),
                    "printed_price_date_locator": "pdf:page:1",
                    "price_definition_locator": "pdf:pages:2-3",
                },
                period,
                period,
                quality_state=(
                    "invalid" if state == "invalid" else "incomplete" if period is None else "valid"
                ),
            )
        )
    abc = _text(columns["abc"])
    abc_state = "present" if re.fullmatch("[ABC]-pris", abc) else "blank"
    if abc and abc_state == "blank":
        abc_state = "invalid"
        _issue(parsed, locator, "invalid_abc_code", source_id, "abc_code", period)
    parsed.assertions.append(
        Assertion(
            "classification_observation",
            locator,
            source_id,
            {
                "classification_context": "ordinary",
                "abc_code": abc[0] if abc_state == "present" else None,
                "group_namespace": None,
                "group_code": None,
                "group_scope": None,
            },
            {
                "abc_code": abc_state,
                "group_namespace": "unreported",
                "group_code": "unreported",
                "group_scope": "unreported",
            },
            {
                "source_identifier": source_id,
                "identifier_span": [first["block"], first["line"], first["span"]],
                "abc_source": abc,
                "span_format": "text,bbox,origin,block-line-span-v1",
                "abc_spans": [_position(span) for span in columns["abc"]],
                "group_identity": "not_printed",
                "printed_price_date": printed_date.isoformat(),
                "printed_price_date_locator": "pdf:page:1",
                "classification_definition_locator": "pdf:page:3",
            },
            period,
            period,
            quality_state=(
                "invalid" if abc_state == "invalid" else "incomplete" if period is None else "valid"
            ),
        )
    )
    if secondary[0]["text"] not in _DISPENSING:
        _issue(parsed, locator, "unknown_dispensing_code", source_id, "dispensing", period)


def parse_medicine_pdf(data: bytes, context: dict[str, Any]) -> Parsed:
    """Read all pages; an optional evidenced period must match the printed date.

    An omitted/None period retains undated, incomplete assertions. The printed
    price date is not promoted to either an effective date or a tariff period.
    """
    current = _context_period(context, "current_period")
    if not isinstance(context.get("tax_basis"), str) or not context["tax_basis"].strip():
        raise AdapterError("missing_price_context")
    if "reimbursement_tax_basis" in context and (
        not isinstance(context["reimbursement_tax_basis"], str)
        or not context["reimbursement_tax_basis"].strip()
    ):
        raise AdapterError("missing_price_context")
    parsed = Parsed()
    with _document(data) as document:
        first = document[0].get_text()
        if "Medicinpriser" not in first:
            raise AdapterError("unsupported_schema", "pdf:page:1")
        period = _period(first, "pdf:page:1")
        if current is not None and current != period:
            raise AdapterError("declared_observed_context_conflict", "pdf:page:1")
        if current is None:
            parsed.issues.append(
                Problem(
                    "pdf:page:1",
                    "unresolved_pdf_tariff_period",
                    blocking_scope="none",
                    field="effective_period",
                    severity="warning",
                )
            )
        guide = " ".join(" ".join(document[index].get_text().split()) for index in (1, 2))
        if not all(
            token in guide
            for token in ("LÆSEVEJLEDNING", "ESP pr. enhed", "TSP", "inkl. recepturgebyr")
        ):
            raise AdapterError("unsupported_pdf_guide", "pdf:pages:2-3")
        atc: dict[str, Any] | None = None
        table_pages = 0
        for number, page in enumerate(document, 1):
            if number == 1:
                continue
            text = page.get_text()
            if _period(text, f"pdf:page:{number}") != period:
                raise AdapterError("declared_observed_context_conflict", f"pdf:page:{number}")
            if number < 4:
                continue
            spans = _spans(page)
            if not _header(spans):
                if "INDEKS" in text and all(label in text for label in ("Navn", "ATC", "Side")):
                    continue
                # The catalogue's final body page omits the running price
                # header. Its boundary is evidenced by the following index.
                final_body = (
                    table_pages > 0
                    and number < document.page_count
                    and "INDEKS" in document[number].get_text()
                )
                if not final_body:
                    raise AdapterError("unsupported_pdf_page", f"pdf:page:{number}")
            if not (_near(page.rect.width, 595) and _near(page.rect.height, 842)):
                raise AdapterError("unsupported_pdf_geometry", f"pdf:page:{number}")
            table_pages += 1
            candidates = list(_page_records(spans, number))
            if not candidates:
                raise AdapterError("empty_pdf_table_page", f"pdf:page:{number}")
            for locator, row, heading in candidates:
                if heading is not None:
                    atc = {**heading, "source_locator": f"pdf:page:{number}"}
                _record(parsed, locator, row, atc, period, current, context)
            # A new heading can appear below the page's last package and applies
            # to the next page. Preserve its printed location with that attribute.
            headings = [
                span
                for span in spans
                if _near(span["origin"][0], 35) and _ATC.fullmatch(span["text"])
            ]
            if headings:
                atc = {
                    **max(headings, key=lambda span: span["origin"][1]),
                    "source_locator": f"pdf:page:{number}",
                }
        if not table_pages:
            raise AdapterError("missing_pdf_table")
    return parsed
