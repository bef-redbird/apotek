"""Exact-signature local report adapters; filenames carry no business meaning.

The introduction titles describe new packages and their explicit Takstdato
establishes the introduction/current-price date. Older quoted prices have no
established effective period. Lifecycle dates retain the source's unresolved
effective-versus-announcement semantics. RTF/PDF and unknown layouts are rejected.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import xlrd
from xlrd.biffh import XLRDError

from bef_redbird.domain import DataError
from bef_redbird.imports import (
    MAX_CELLS,
    MAX_COLUMNS,
    MAX_ROWS,
    _biff_formula_cells,
    _rows,
    _xlsx,
    source_info,
)

from .base import AdapterError, Assertion, Parsed, Problem, field_state
from .workbooks import (
    _PACKAGE_FIELDS,
    _at,
    _Cell,
    _context_period,
    _field,
    _locator,
    _price_context,
    _quality,
    _raw,
    _source_id,
)

PRICE_CHANGES_VERSION = "price-changes-workbook-v1"
INTRODUCTIONS_VERSION = "introductions-workbook-v1"
DISCONTINUED_VERSION = "discontinued-workbook-v1"
SUPPLY_VERSION = "supply-workbook-v1"

_CHANGES = (
    "Firma",
    "Navn",
    "Form",
    "Styrke",
    "Vnr",
    "Pakningstekst",
    "Ny AIP",
    "Ny reg. pris",
    "Gl. AIP",
    "Gl. reg. pris",
    "Prisforskel %",
)
_INTRO = (
    "Navn",
    "Form",
    "Styrke",
    "MF-indehaver",
    "Pakningsejer",
    "Varenr",
    "Pakningsstørrelse",
    "Opbevaringsbetingelse",
    "Udlevering",
    "Trafikadvarsel DK",
    "AIP (Kr.)",
    "Reg. pris",
)
_STOP = (
    "Repræsentant",
    "Navn",
    "Form",
    "Styrke",
    "VareNr",
    "RPk",
    "Stop (Deaktiveret)",
    "Slet (Udgår permanent)",
)
_SUPPLY = (
    "Varenr.",
    "Lægemiddel",
    "Form",
    "Styrke",
    "Pakningsstr.",
    "Firma",
    "Substitutionsgrp.nr.",
    "ABC-markering",
    "Grossist",
    "Ikrafttrædelsesdato",
)
_INTRO_TITLES = (
    "Nye pakninger på eksisterende formuleringer",
    "Nye pakninger på nye formuleringer",
)


@dataclass
class _Grid:
    name: str
    rows: list[list[_Cell]]
    raw: dict[tuple[int, int], Any]
    date_system: int | None = None


def _heading(value: Any) -> str:
    return " ".join(str(value or "").replace("_x000D_", " ").split())


def _grids(data: bytes) -> list[_Grid]:
    """Use bounded application containers, preserving BIFF date serial originals."""
    try:
        if data.startswith(b"PK"):
            book = _xlsx(data, "source.xlsx")
            try:
                return [
                    _Grid(
                        sheet.title,
                        [[_Cell(c.value, c.data_type) for c in row] for _, row in _rows(sheet)],
                        {},
                    )
                    for sheet in book.worksheets
                ]
            finally:
                book.close()
        if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise AdapterError("unsupported_schema")
        source_info(data, "source.xls")
        formulas = _biff_formula_cells(data)
        legacy = xlrd.open_workbook(file_contents=data, on_demand=True, logfile=io.StringIO())
        try:
            grids = []
            for index, sheet in enumerate(legacy.sheets()):
                if (
                    sheet.nrows > MAX_ROWS
                    or sheet.ncols > MAX_COLUMNS
                    or sheet.nrows * sheet.ncols > MAX_CELLS
                ):
                    raise AdapterError("workbook_resource_limit")
                rows = []
                raw = {}
                for r in range(sheet.nrows):
                    row = []
                    for c in range(sheet.ncols):
                        cell = sheet.cell(r, c)
                        value: Any = cell.value
                        kind = "f" if (r, c) in formulas.get(index, set()) else "n"
                        if cell.ctype == xlrd.XL_CELL_BOOLEAN and kind == "n":
                            value = bool(value)
                            kind = "b"
                        if cell.ctype == xlrd.XL_CELL_ERROR:
                            kind = "e"
                        if cell.ctype == xlrd.XL_CELL_DATE and kind == "n":
                            raw[r + 1, c] = _raw(value)
                            try:
                                value = xlrd.xldate_as_datetime(float(value), legacy.datemode)
                            except (ValueError, OverflowError):
                                kind = "e"
                        row.append(_Cell(value, kind))
                    rows.append(row)
                grids.append(_Grid(sheet.name, rows, raw, legacy.datemode))
            return grids
        finally:
            legacy.release_resources()
    except (DataError, XLRDError, OSError, ValueError, IndexError, OverflowError) as exc:
        if isinstance(exc, AdapterError):
            raise
        raise AdapterError("invalid_report_workbook") from exc


def _select(data: bytes, headers: tuple[str, ...], rows: tuple[int, ...]) -> tuple[_Grid, int]:
    matches = []
    for grid in _grids(data):
        for number in rows:
            if number > len(grid.rows):
                continue
            row = grid.rows[number - 1]
            if any(cell.kind in ("f", "e") for cell in row):
                continue
            normalized = tuple(_heading(c.value) for c in row)
            if normalized[: len(headers)] == headers and not any(normalized[len(headers) :]):
                matches.append((grid, number))
    if len(matches) > 1:
        raise AdapterError("ambiguous_report_sheet")
    if not matches:
        raise AdapterError("unsupported_schema")
    return matches[0]


def _date_value(value: Any) -> date:
    if isinstance(value, datetime):
        if value.time() != datetime.min.time():
            raise ValueError("invalid_date")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                result = datetime.strptime(value.strip(), fmt).date()
                if result.strftime(fmt) == value.strip():
                    return result
            except ValueError:
                pass
    raise ValueError("invalid_date")


def _metadata(grid: _Grid, family: str, current: date | None) -> dict[str, Any]:
    title = _at(grid.rows[0], 0)
    titles = ("Prisændringer",) if family == "changes" else _INTRO_TITLES
    if title.kind in ("f", "e") or title.value not in titles:
        raise AdapterError("unsupported_schema")
    # Exact observed layouts: inline metadata in row 1 E/F and L (changes),
    # row 2 G/H/I/J and optional row 3 M (introductions).
    tariff_positions = [(1, 4), (1, 5)] if family == "changes" else [(2, c) for c in (6, 7, 8, 9)]
    run_positions = [(1, 11)] if family == "changes" else [(3, 12)]
    metadata: dict[str, Any] = {"report_title": title.value}
    for key, label, positions, required in (
        ("tariff_date", "Takstdato", tariff_positions, True),
        ("run_date", "Kørselsdato", run_positions, family == "changes"),
    ):
        observed = []
        for rowno, col in positions:
            cell = _at(grid.rows[rowno - 1], col) if rowno <= len(grid.rows) else _Cell()
            if field_state(cell.value) == "blank":
                continue
            if cell.kind in ("f", "e"):
                raise AdapterError("invalid_report_metadata")
            if not isinstance(cell.value, str):
                raise AdapterError("invalid_report_metadata")
            match = re.fullmatch(rf"{label}:\s*(\d{{2}}-\d{{2}}-\d{{4}})", cell.value)
            if match is None:
                raise AdapterError("invalid_report_metadata")
            try:
                observed.append(_date_value(match[1]))
            except ValueError as exc:
                raise AdapterError("invalid_report_metadata") from exc
            metadata[f"{key}_source"] = cell.value
            metadata[f"{key}_locator"] = _locator(grid.name, rowno, col)
        if len(set(observed)) > 1:
            raise AdapterError("declared_observed_context_conflict")
        if not observed and required:
            raise AdapterError("missing_report_date")
        if observed:
            metadata[key] = observed[0].isoformat()
            if key == "tariff_date" and observed[0] != current:
                raise AdapterError("declared_observed_context_conflict", metadata[f"{key}_locator"])
    return metadata


def _records(grid: _Grid, header: int, id_col: int, parsed: Parsed) -> Any:
    for number, row in enumerate(grid.rows[header:], header + 1):
        if all(field_state(cell.value) == "blank" for cell in row):
            continue
        parsed.records += 1
        ids = _source_id(_at(row, id_col), parsed, _locator(grid.name, number, id_col))
        if ids is not None:
            original, canonical = ids
            raw = {
                _heading(cell.value): grid.raw.get((number, col), _raw(_at(row, col).value))
                for col, cell in enumerate(grid.rows[header - 1])
                if _heading(cell.value)
            }
            raw.update(
                source_identifier=_raw(_at(row, id_col).value),
                identifier_cell_type=_at(row, id_col).kind,
            )
            raw["source_cell_types"] = {
                _heading(cell.value): _at(row, col).kind
                for col, cell in enumerate(grid.rows[header - 1])
                if _heading(cell.value)
            }
            raw["source_field_presence"] = {
                _heading(cell.value): (
                    "invalid"
                    if _at(row, col).kind in ("f", "e")
                    else field_state(_at(row, col).value)
                )
                for col, cell in enumerate(grid.rows[header - 1])
                if _heading(cell.value)
            }
            if grid.date_system is not None:
                raw["excel_date_system"] = grid.date_system
            yield number, row, original, canonical, raw


def _attributes(
    parsed: Parsed,
    grid: _Grid,
    number: int,
    row: list[_Cell],
    original: str,
    canonical: str,
    raw: dict[str, Any],
    fields: dict[str, int],
    period: date | None,
) -> None:
    values = dict.fromkeys(_PACKAGE_FIELDS)
    presence = dict.fromkeys(_PACKAGE_FIELDS, "unreported")
    for field, col in fields.items():
        values[field], presence[field] = _field(
            _at(row, col),
            parsed,
            _locator(grid.name, number, col),
            canonical,
            field,
            period,
        )
    parsed.assertions.append(
        Assertion(
            "package_observation",
            _locator(grid.name, number),
            original,
            values,
            presence,
            raw,
            period,
            period,
            quality_state=_quality(presence),
        )
    )


def _prices(
    parsed: Parsed,
    grid: _Grid,
    number: int,
    row: list[_Cell],
    original: str,
    canonical: str,
    raw: dict[str, Any],
    columns: tuple[tuple[int, str, str], ...],
    current: date | None,
    context: dict[str, Any],
) -> None:
    bases = _price_context(context, cito=False)
    for col, price_type, role in columns:
        loc = _locator(grid.name, number, col)
        period = current if role == "effective_price" else None
        amount, state = _field(
            _at(row, col),
            parsed,
            loc,
            canonical,
            "amount_ore",
            period,
            conversion="money",
        )
        parsed.assertions.append(
            Assertion(
                "price_observation",
                loc,
                original,
                {**bases, "price_type": price_type, "price_role": role, "amount_ore": amount},
                {"amount_ore": state},
                raw,
                period,
                current,
                quality_state="invalid" if state == "invalid" else "valid",
            )
        )


def parse_price_changes(data: bytes, context: dict[str, Any]) -> Parsed:
    """Read the explicit new/old AIP and registered-price report layout."""
    if not data.startswith(b"PK"):
        raise AdapterError("unsupported_source_format")
    current = _context_period(context, "current_period", required=True)
    _price_context(context, cito=False)
    grid, header = _select(data, _CHANGES, (4,))
    metadata = _metadata(grid, "changes", current)
    parsed = Parsed()
    for number, row, original, canonical, raw in _records(grid, header, 4, parsed):
        raw = {**raw, **metadata}
        _attributes(
            parsed,
            grid,
            number,
            row,
            original,
            canonical,
            raw,
            {"company": 0, "name": 1, "form": 2, "strength": 3, "pack_description": 5},
            current,
        )
        _prices(
            parsed,
            grid,
            number,
            row,
            original,
            canonical,
            raw,
            (
                (6, "aip", "effective_price"),
                (7, "registered", "effective_price"),
                (8, "aip", "reported_previous_price"),
                (9, "registered", "reported_previous_price"),
            ),
            current,
            context,
        )
        _field(
            _at(row, 10),
            parsed,
            _locator(grid.name, number, 10),
            canonical,
            "reported_registered_percentage_change",
            current,
        )
    return parsed


def parse_introductions(data: bytes, context: dict[str, Any]) -> Parsed:
    """Read either evidenced formulation title and the row-5/6 header variants."""
    if not data.startswith(b"PK"):
        raise AdapterError("unsupported_source_format")
    current = _context_period(context, "current_period", required=True)
    _price_context(context, cito=False)
    # The punctuation variant is exact and otherwise shares the same schema.
    grids = _grids(data)
    candidates = []
    for grid in grids:
        for number in (5, 6):
            if number <= len(grid.rows):
                header = tuple(_heading(c.value) for c in grid.rows[number - 1])
                for id_heading in ("Varenr", "Varenr."):
                    expected = (*_INTRO[:5], id_heading, *_INTRO[6:])
                    if (
                        header[:12] == expected
                        and not any(header[12:])
                        and not any(c.kind in ("f", "e") for c in grid.rows[number - 1])
                    ):
                        candidates.append((grid, number))
    if len(candidates) != 1:
        raise AdapterError("ambiguous_report_sheet" if candidates else "unsupported_schema")
    grid, header_row = candidates[0]
    metadata = _metadata(grid, "introductions", current)
    parsed = Parsed()
    for number, row, original, canonical, raw in _records(grid, header_row, 5, parsed):
        raw = {**raw, **metadata}
        _attributes(
            parsed,
            grid,
            number,
            row,
            original,
            canonical,
            raw,
            {
                "name": 0,
                "form": 1,
                "strength": 2,
                "company": 4,
                "pack_description": 6,
                "storage_code": 7,
            },
            current,
        )
        _prices(
            parsed,
            grid,
            number,
            row,
            original,
            canonical,
            raw,
            (
                (10, "aip", "effective_price"),
                (11, "registered", "effective_price"),
            ),
            current,
            context,
        )
        parsed.assertions.append(
            Assertion(
                "lifecycle_event",
                _locator(grid.name, number),
                original,
                {
                    "event_type": "introduction",
                    "original_source_code": metadata["report_title"],
                    "interpretation_status": "established",
                },
                {"effective_date": "present"},
                raw,
                report_period=current,
                effective_date=current,
            )
        )
    return parsed


def _dated_field(
    cell: _Cell,
    parsed: Parsed,
    locator: str,
    canonical: str,
    field: str,
) -> tuple[date | None, str]:
    state = field_state(cell.value)
    try:
        if cell.kind in ("f", "e"):
            raise ValueError("formula_cell" if cell.kind == "f" else "error_cell")
        if state == "blank":
            return None, state
        return _date_value(cell.value), state
    except ValueError as exc:
        parsed.issues.append(Problem(locator, str(exc), identifier=canonical, field=field))
        return None, "invalid"


def parse_discontinued(data: bytes, context: dict[str, Any]) -> Parsed:
    """Keep independent source-reported stop and permanent-deletion dates."""
    if not data.startswith(b"PK"):
        raise AdapterError("unsupported_source_format")
    current = _context_period(context, "current_period")
    grid, header = _select(data, _STOP, (1,))
    parsed = Parsed()
    for number, row, original, canonical, raw in _records(grid, header, 4, parsed):
        _attributes(
            parsed,
            grid,
            number,
            row,
            original,
            canonical,
            raw,
            {"company": 0, "name": 1, "form": 2, "strength": 3, "pack_description": 5},
            current,
        )
        for col, event in ((6, "deactivation"), (7, "permanent_deletion")):
            loc = _locator(grid.name, number, col)
            effective, state = _dated_field(
                _at(row, col),
                parsed,
                loc,
                canonical,
                "effective_date",
            )
            if effective is None:
                if state == "blank":
                    parsed.issues.append(
                        Problem(
                            loc,
                            "missing_event_date",
                            blocking_scope="none",
                            identifier=canonical,
                            field="effective_date",
                            severity="warning",
                        )
                    )
                continue
            parsed.issues.append(
                Problem(
                    loc,
                    "source_reported_event_date_semantics_unknown",
                    blocking_scope="none",
                    identifier=canonical,
                    field="effective_date",
                    severity="warning",
                )
            )
            parsed.assertions.append(
                Assertion(
                    "lifecycle_event",
                    loc,
                    original,
                    {
                        "event_type": event,
                        "original_source_code": grid.rows[0][col].value,
                        "interpretation_status": "unknown",
                    },
                    {"effective_date": state},
                    raw,
                    report_period=current,
                    effective_date=effective,
                    quality_state="incomplete",
                )
            )
    return parsed


def parse_supply(data: bytes, context: dict[str, Any]) -> Parsed:
    """Read dated positive report assertions; absence never implies recovery."""
    source_date = context.get("date_basis") == "source_effective_date"
    if source_date and context.get("observation_date") is not None:
        raise AdapterError("conflicting_supply_date_context")
    observed = _context_period(context, "observation_date", required=not source_date)
    if not source_date and (
        not isinstance(context.get("observation_date_basis"), str)
        or not context["observation_date_basis"].strip()
    ):
        raise AdapterError("missing_observation_date_basis")
    current = _context_period(context, "current_period")
    if (
        context.get("reported_status") != "reported_disruption"
        or not isinstance(context.get("wholesaler_namespace"), str)
        or not context["wholesaler_namespace"].strip()
    ):
        raise AdapterError("missing_supply_context")
    grid, header = _select(data, _SUPPLY, (1,))
    parsed = Parsed()
    for number, row, original, canonical, raw in _records(grid, header, 0, parsed):
        _attributes(
            parsed,
            grid,
            number,
            row,
            original,
            canonical,
            raw,
            {"name": 1, "form": 2, "strength": 3, "pack_description": 4, "company": 5},
            current,
        )
        loc = _locator(grid.name, number, 8)
        wholesaler, wholesaler_state = _field(
            _at(row, 8),
            parsed,
            loc,
            canonical,
            "wholesaler_identifier",
            current,
        )
        effective, date_state = _dated_field(
            _at(row, 9),
            parsed,
            _locator(grid.name, number, 9),
            canonical,
            "effective_date",
        )
        row_observed = effective if source_date else observed
        raw = {
            **raw,
            "observation_date_role": (
                "source_effective_date" if source_date else "declared_observation_date"
            ),
            "observation_date_basis": (
                "source_effective_date" if source_date else context["observation_date_basis"]
            ),
        }
        presence = {
            "wholesaler_identifier": wholesaler_state,
            "effective_date": date_state,
            "observation_date": "present",
            "reported_status": "present",
        }
        if wholesaler is None:
            parsed.issues.append(
                Problem(
                    loc,
                    "missing_wholesaler_identifier",
                    identifier=canonical,
                    field="wholesaler_identifier",
                )
            )
        if row_observed is None:
            parsed.issues.append(
                Problem(
                    _locator(grid.name, number, 9),
                    "missing_supply_observation_date",
                    identifier=canonical,
                    field="observation_date",
                )
            )
        if row_observed is not None and wholesaler is not None:
            parsed.assertions.append(
                Assertion(
                    "supply_observation",
                    loc,
                    original,
                    {
                        "observation_date": row_observed,
                        "wholesaler_namespace": context["wholesaler_namespace"],
                        "wholesaler_identifier": wholesaler,
                        "reported_status": context["reported_status"],
                    },
                    presence,
                    raw,
                    report_period=current,
                    effective_date=effective,
                    quality_state=_quality(presence),
                )
            )
        if (
            current is not None
            and context.get("classification_context") in ("ordinary", "dose")
            and isinstance(context.get("group_namespace"), str)
            and context["group_namespace"].strip()
        ):
            abc, abc_state = _field(
                _at(row, 7),
                parsed,
                _locator(grid.name, number, 7),
                canonical,
                "abc_code",
                current,
                conversion="abc",
            )
            group, group_state = _field(
                _at(row, 6),
                parsed,
                _locator(grid.name, number, 6),
                canonical,
                "group_code",
                current,
                conversion="group",
            )
            presence = {"abc_code": abc_state, "group_code": group_state}
            parsed.assertions.append(
                Assertion(
                    "classification_observation",
                    _locator(grid.name, number, 6),
                    original,
                    {
                        "classification_context": context["classification_context"],
                        "abc_code": abc,
                        "group_namespace": context["group_namespace"],
                        "group_code": group,
                        "group_scope": current.isoformat(),
                    },
                    presence,
                    raw,
                    current,
                    current,
                    quality_state=_quality(presence),
                )
            )
    return parsed
