"""Independent native-source expectations for printed medicine catalogues.

No production adapter, normalized row or converter is used here. A sweep of
the native page geometry partitions every text span into a printed record,
heading or page furniture. Native extraction is shared infrastructure; record
recognition, omission checks and exact integer money conversion are independent.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from collections import Counter
from datetime import datetime
from typing import Any

import pymupdf

_ATTRIBUTES = ("name", "form", "strength", "pack_description", "company", "storage_code", "atc")
_COLUMNS = (
    "name",
    "form",
    "strength",
    "pack_description",
    "company",
    "storage_code",
    "ESP_per_unit",
    "ESP",
    "TSP",
    "reimbursement_marker",
    "dispensing",
    "prescribing_specialties",
    "abc",
    "dose_dispensable",
)
_SPAN_FORMAT = "text,bbox,origin,block-line-span-v1"
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


def _amount(token: str) -> int:
    """Parse decimal digits directly; neither float nor Decimal rounding is involved."""
    if not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+)(?:,[0-9]+)?", token):
        raise ValueError("malformed_money")
    negative = token.startswith("-")
    whole, separator, fraction = token.removeprefix("-").replace(".", "").partition(",")
    if negative and any(c != "0" for c in whole + fraction):
        raise ValueError("negative_money")
    if separator and any(c != "0" for c in fraction[2:]):
        raise ValueError("unsupported_money_precision")
    result = int(whole) * 100 + int((fraction + "00")[:2])
    if result > 9223372036854775807:
        raise ValueError("money_overflow")
    return result


def _native(page: Any) -> tuple[list[dict[str, Any]], int]:
    result = []
    blocks = page.get_text("dict")["blocks"]
    for block_index, block in enumerate(blocks):
        for line_index, line in enumerate(block.get("lines", [])):
            for span_index, span in enumerate(line["spans"]):
                if span["text"].strip():
                    result.append(
                        {
                            "text": span["text"],
                            "bbox": list(span["bbox"]),
                            "origin": list(span["origin"]),
                            "font": span["font"],
                            "index": [block_index, line_index, span_index],
                        }
                    )
    return result, sum(block.get("type") != 0 for block in blocks)


def _position(span: dict[str, Any]) -> list[Any]:
    return [span["text"], span["bbox"], span["origin"], span["index"]]


def _join(spans: list[dict[str, Any]], *, numeric: bool = False) -> str:
    return (
        ("" if numeric else " ")
        .join(
            span["text"] for span in sorted(spans, key=lambda s: (s["origin"][1], s["origin"][0]))
        )
        .strip()
    )


def _date(text: str) -> str:
    dates = re.findall(r"Priser for den ([0-9]{2}\.[0-9]{2}\.[0-9]{4})", text)
    if not dates or len(set(dates)) != 1:
        raise ValueError("independent_pdf_missing_or_conflicting_date")
    try:
        return datetime.strptime(dates[0], "%d.%m.%Y").date().isoformat()
    except ValueError as exc:
        raise ValueError("independent_pdf_invalid_date") from exc


def _atc(text: str) -> bool:
    return re.fullmatch(r"Q?[A-Z][0-9]{2}[A-Z]{0,2}[0-9]{0,2}", text) is not None


def _close(value: float, anchor: float) -> bool:
    return abs(value - anchor) < 0.3


def _columns(
    row: list[dict[str, Any]], first: dict[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], list[list[int]]]:
    # The narrow dispensing cell identifies the second band, including rows
    # with no fixed price. Symbol-font reimbursement marks belong to band one.
    detail = [
        s
        for s in row
        if 438.4 < s["origin"][0] < 438.95
        and s["origin"][1] > first["origin"][1] + 1
        and "Wingdings" not in s["font"]
    ]
    if len(detail) != 1:
        raise ValueError("ambiguous_pdf_record_bands")
    band_boundary = detail[0]["origin"][1] - 0.75
    columns: dict[str, list[dict[str, Any]]] = {key: [] for key in _COLUMNS}
    unclaimed = []
    for span in row:
        if span is first:
            continue
        x, y = span["origin"]
        right = span["bbox"][2]
        key = None
        if y < band_boundary:
            for anchor, field in (
                (82.96, "name"),
                (174.13, "form"),
                (253.76, "strength"),
                (340.66, "pack_description"),
            ):
                if _close(x, anchor):
                    key = field
            for edge, field in ((487.22, "ESP_per_unit"), (524.11, "ESP"), (561, "TSP")):
                if _close(right, edge):
                    key = field
            if 435 <= x <= 440 and span["text"].strip() in {"l", "n", "U"}:
                key = "reimbursement_marker"
        else:
            if _close(x, 82.96):
                key = "company"
            elif _close(x, 174.13):
                key = "storage_code"
            elif span is detail[0]:
                key = "dispensing"
            elif _close(right, 435.68):
                key = "prescribing_specialties"
            elif _close(right, 485.02) and x >= 450:
                key = "abc"
            elif _close(x, 554) and span["text"] == "D":
                key = "dose_dispensable"
        if key is None:
            unclaimed.append(span["index"])
        else:
            columns[key].append(span)
    return columns, unclaimed


def expected_pdf(data: bytes, adapter: str, context: dict[str, Any]) -> dict[str, Any]:
    """Read all source pages and derive the independent source-audit contract."""
    if adapter != "medicine-prices" or not data.startswith(b"%PDF-"):
        raise ValueError("independent_pdf_unsupported_family")
    if not isinstance(context.get("tax_basis"), str) or not context["tax_basis"].strip():
        raise ValueError("independent_pdf_missing_price_context")
    reimbursement_tax = context.get("reimbursement_tax_basis", context["tax_basis"])
    if not isinstance(reimbursement_tax, str) or not reimbursement_tax.strip():
        raise ValueError("independent_pdf_missing_price_context")
    try:
        document: Any = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError) as exc:
        raise ValueError("independent_pdf_invalid_document") from exc
    assertions: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    accounting: list[dict[str, Any]] = []
    identifiers: Counter[str] = Counter()
    record_count = 0
    atc: dict[str, Any] | None = None
    phase = "preamble"
    table_pages = 0
    with document:
        if document.is_encrypted or len(document) < 4:
            raise ValueError("independent_pdf_unsupported_document")
        if "Medicinpriser" not in document[0].get_text():
            raise ValueError("independent_pdf_missing_title")
        printed_date = _date(document[0].get_text())
        period = context.get("current_period")
        if period is not None and period != printed_date:
            raise ValueError("independent_pdf_report_context_conflict")
        if period is None:
            issues.append(
                {
                    "source_locator": "pdf:page:1",
                    "code": "unresolved_pdf_tariff_period",
                    "identifier": None,
                    "field": "effective_period",
                    "period": None,
                    "severity": "warning",
                    "blocking_scope": "none",
                }
            )
        guide = " ".join(" ".join(document[p].get_text().split()) for p in (1, 2))
        if not all(
            label in guide
            for label in ("LÆSEVEJLEDNING", "ESP pr. enhed", "TSP", "inkl. recepturgebyr")
        ):
            raise ValueError("independent_pdf_unsupported_guide")
        for number, page in enumerate(document, 1):
            locator = f"pdf:page:{number}"
            spans, nontext_blocks = _native(page)
            text = page.get_text()
            if not spans or _date(text) != printed_date:
                raise ValueError("independent_pdf_page_date_conflict:" + locator)
            if abs(page.rect.width - 595) > 0.3 or abs(page.rect.height - 842) > 0.3:
                raise ValueError("independent_pdf_page_geometry:" + locator)
            if number > 1:
                folios = [
                    s["text"].strip()
                    for s in spans
                    if s["origin"][1] > 815 and s["origin"][0] > 500
                ]
                if folios != [str(number)]:
                    raise ValueError("independent_pdf_page_sequence:" + locator)
            entry: dict[str, Any] = {
                "locator": locator,
                "native_spans": len(spans),
                "nontext_blocks": nontext_blocks,
                "native_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "native_geometry_sha256": hashlib.sha256(
                    json.dumps([_position(s) for s in spans], ensure_ascii=False).encode()
                ).hexdigest(),
            }
            if number <= 3:
                entry["kind"] = "cover" if number == 1 else "reading_guide"
                accounting.append(entry)
                continue
            if nontext_blocks:
                raise ValueError("independent_pdf_nontext_body:" + locator)
            labels = {
                s["text"].strip() for s in spans if s["origin"][1] < 45 and s["origin"][0] > 430
            }
            header = {"ESP pr. enhed", "ESP", "TSP"} <= labels
            is_index = "INDEKS" in text and all(label in text for label in ("Navn", "ATC", "Side"))
            if is_index:
                if header or any(
                    40 <= s["origin"][1] < 790
                    and 438.4 < s["origin"][0] < 438.95
                    and s["text"] in _DISPENSING
                    for s in spans
                ):
                    raise ValueError("independent_pdf_mixed_index_and_table:" + locator)
                if phase == "preamble":
                    raise ValueError("independent_pdf_index_before_catalogue")
                phase = "index"
                entry["kind"] = "name_index"
                accounting.append(entry)
                continue
            if phase == "index":
                raise ValueError("independent_pdf_table_after_index")
            if not header and not (
                table_pages and number < len(document) and "INDEKS" in document[number].get_text()
            ):
                raise ValueError("independent_pdf_unknown_page:" + locator)
            phase = "table"
            table_pages += 1
            if any(
                34.7 < s["origin"][0] < 35.3
                and re.fullmatch(r"[0-9]{6}", s["text"])
                and not 40 <= s["origin"][1] < 790
                for s in spans
            ):
                raise ValueError("independent_pdf_record_outside_body:" + locator)
            body = [s for s in spans if 40 <= s["origin"][1] < 790]
            # All left-margin entries, including malformed identifiers, delimit
            # intervals. Each other body span is assigned once by binary search.
            boundaries = sorted(
                [s for s in body if 34.7 < s["origin"][0] < 35.3],
                key=lambda s: s["origin"][1],
            )
            heights = [s["origin"][1] for s in boundaries]
            if not boundaries or len(set(heights)) != len(heights):
                raise ValueError("independent_pdf_missing_or_overlapping_rows:" + locator)
            groups: list[list[dict[str, Any]]] = [[] for _ in boundaries]
            for span in body:
                index = bisect_right(heights, span["origin"][1] + 0.1) - 1
                if index < 0:
                    raise ValueError("independent_pdf_unaccounted_body:" + locator)
                groups[index].append(span)
            counts: Counter[str] = Counter()
            ordinal = 0
            for first, row in zip(boundaries, groups, strict=True):
                if _atc(first["text"]):
                    if any(abs(s["origin"][1] - first["origin"][1]) > 1 for s in row):
                        raise ValueError("independent_pdf_unaccounted_heading_content:" + locator)
                    atc = {"position": _position(first), "source_locator": locator}
                    counts["heading_spans"] += len(row)
                    continue
                ordinal += 1
                record_count += 1
                where = f"{locator}:record:{ordinal}"
                identifier = first["text"]
                counts["record_spans"] += len(row)

                def problem(
                    code: str,
                    field: str | None,
                    location: str = where,
                    *,
                    scope: str = "field",
                    known_id: str | None = identifier,
                    known_period: str | None = period,
                ) -> None:
                    issues.append(
                        {
                            "source_locator": location,
                            "code": code,
                            "identifier": known_id,
                            "field": field,
                            "period": known_period,
                            "severity": "error",
                            "blocking_scope": scope,
                        }
                    )

                if not re.fullmatch(r"[0-9]{6}", identifier):
                    counts["rejected_records"] += 1
                    problem(
                        "invalid_identifier",
                        "identifier",
                        scope="row",
                        known_id=None,
                        known_period=None,
                    )
                    continue
                try:
                    columns, unclaimed = _columns(row, first)
                except ValueError:
                    counts["rejected_records"] += 1
                    problem("ambiguous_pdf_record_bands", None, scope="row")
                    continue
                identifiers[identifier] += 1
                counts["accepted_records"] += 1
                counts["unmapped_record_spans"] += len(unclaimed)

                def emit(
                    table: str,
                    location: str,
                    values: dict[str, Any],
                    presence: dict[str, str],
                    raw: dict[str, Any],
                    quality: str = "valid",
                    source_identifier: str = identifier,
                ) -> None:
                    assertions.append(
                        {
                            "table": table,
                            "source_locator": location,
                            "source_identifier": source_identifier,
                            "canonical_identifier": source_identifier,
                            "effective_period": period,
                            "report_period": period,
                            "effective_date": None,
                            "values": values,
                            "presence": presence,
                            "quality": "incomplete"
                            if period is None and quality == "valid"
                            else quality,
                            "raw": raw,
                        }
                    )

                common = {
                    "source_identifier": identifier,
                    "printed_price_date": printed_date,
                    "printed_price_date_locator": "pdf:page:1",
                }
                values = {field: _join(columns[field]) or None for field in _ATTRIBUTES[:-1]}
                values["atc"] = atc["position"][0] if atc else None
                emit(
                    "package_observation",
                    where,
                    values,
                    {
                        field: "present"
                        if values[field] is not None
                        else "unreported"
                        if field == "atc"
                        else "blank"
                        for field in _ATTRIBUTES
                    },
                    {
                        **common,
                        "source_columns": {k: _join(v) for k, v in columns.items()},
                        "span_format": _SPAN_FORMAT,
                        "source_spans": [_position(s) for s in row],
                        "unmapped_spans": unclaimed,
                        "atc_heading": atc,
                        "price_definition_locator": "pdf:pages:2-3",
                        "group_identity": "not_printed",
                    },
                )
                for key, price_type, basis in (
                    ("ESP_per_unit", "consumer", "per_unit"),
                    ("ESP", "consumer", "per_package"),
                    ("TSP", "reimbursement_basis", "per_package"),
                ):
                    token = _join(columns[key], numeric=True)
                    amount = None
                    state = "present"
                    price_locator = where + ":" + key
                    if not token:
                        state = "missing"
                        problem("missing_pdf_price", "amount_ore", price_locator)
                    elif token in {"-,--", "–,––", "–,– –"}:
                        state = "unreported"
                    else:
                        try:
                            amount = _amount(token)
                        except ValueError as exc:
                            state = "invalid"
                            problem(str(exc), "amount_ore", price_locator)
                    emit(
                        "price_observation",
                        price_locator,
                        {
                            "price_type": price_type,
                            "amount_ore": amount,
                            "currency": "DKK",
                            "price_basis": basis,
                            "tax_basis": (
                                reimbursement_tax
                                if price_type == "reimbursement_basis"
                                else context["tax_basis"]
                            ),
                            "fee_basis": "includes_prescription_and_preparation_fees"
                            if price_type == "consumer"
                            else "reimbursement_basis",
                            "price_role": "effective_price",
                        },
                        {"amount_ore": state},
                        {
                            **common,
                            "identifier_span": first["index"],
                            "amount_source": token,
                            "amount_column": key,
                            "span_format": _SPAN_FORMAT,
                            "amount_spans": [_position(s) for s in columns[key]],
                            "price_definition_locator": "pdf:pages:2-3",
                        },
                        "invalid" if state == "invalid" else "valid",
                    )
                abc = _join(columns["abc"])
                state = (
                    "present"
                    if abc in {"A-pris", "B-pris", "C-pris"}
                    else ("invalid" if abc else "blank")
                )
                if state == "invalid":
                    problem("invalid_abc_code", "abc_code")
                emit(
                    "classification_observation",
                    where,
                    {
                        "classification_context": "ordinary",
                        "abc_code": abc[0] if state == "present" else None,
                        "group_namespace": None,
                        "group_code": None,
                        "group_scope": None,
                    },
                    {
                        "abc_code": state,
                        "group_namespace": "unreported",
                        "group_code": "unreported",
                        "group_scope": "unreported",
                    },
                    {
                        **common,
                        "identifier_span": first["index"],
                        "abc_source": abc,
                        "span_format": _SPAN_FORMAT,
                        "abc_spans": [_position(s) for s in columns["abc"]],
                        "group_identity": "not_printed",
                        "classification_definition_locator": "pdf:page:3",
                    },
                    "invalid" if state == "invalid" else "valid",
                )
                if _join(columns["dispensing"]) not in _DISPENSING:
                    problem("unknown_dispensing_code", "dispensing")
            if not ordinal:
                raise ValueError("independent_pdf_empty_table:" + locator)
            if counts["heading_spans"] + counts["record_spans"] != len(body):
                raise ValueError("independent_pdf_unaccounted_spans:" + locator)
            entry.update(
                kind="medicine_table",
                candidates=ordinal,
                **dict(counts),
                margin_spans=len(spans) - len(body),
            )
            accounting.append(entry)
        if not table_pages:
            raise ValueError("independent_pdf_missing_table")
        if phase != "index":
            raise ValueError("independent_pdf_missing_index_boundary")
    return {
        "assertions": assertions,
        "issues": issues,
        "source_rows": record_count,
        "sheet": "PDF",
        "header_row": None,
        "date_system": None,
        "metadata": {
            "printed_date": printed_date,
            "pages": len(accounting),
            "table_pages": table_pages,
            "duplicate_identifier_records": sum(n - 1 for n in identifiers.values()),
            "independent_layout": "native-span-sweep-v1",
        },
        "physical_accounting": accounting,
    }
