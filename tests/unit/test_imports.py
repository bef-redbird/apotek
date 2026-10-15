"""Hand-authored synthetic workbook contracts, including legacy BIFF preflight."""

from __future__ import annotations

import hashlib
import io
import re
import struct
from collections.abc import Callable
from datetime import date
from zipfile import ZIP_DEFLATED, ZipFile

import olefile
import pytest
import xlwt
from openpyxl import Workbook
from openpyxl.workbook.defined_name import DefinedName

from bef_redbird.domain import DataError
from bef_redbird.imports import parse_cito, parse_lms_history, source_info
from bef_redbird.validation import group_id, price_ore, product_id

CITO_HEADERS = ["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"]
HISTORY_HEADERS = ["Varenummer", "Indikator", "20260105", "20260119", "20260202"]
VALID_CITO = [123, "N", 100.25, "A", "C", 12]


def xlsx_bytes(
    headers: list[object],
    rows: list[list[object]],
    sheet: str = "Table",
    *,
    hidden: bool = False,
    stale: bool = False,
) -> bytes:
    book = Workbook()
    table = book.active
    assert table is not None
    table.title = sheet
    table.append(headers)
    for row in rows:
        table.append(row)
    if hidden:
        table.row_dimensions[2].hidden = True
        table.column_dimensions["B"].hidden = True
    if stale:
        book.defined_names.add(DefinedName("stale", attr_text=f"'{sheet}'!$A$1:$B$2"))
        book.defined_names.add(DefinedName("broken", attr_text="#REF!"))
    result = io.BytesIO()
    book.save(result)
    data = result.getvalue()
    if stale:
        data = rewrite_zip(
            data,
            "xl/worksheets/sheet1.xml",
            lambda raw: re.sub(
                rb'<dimension ref="[^"]+"',
                b'<dimension ref="A1:B2"',
                raw,
            ),
        )
    return data


def rewrite_zip(data: bytes, member: str, change: Callable[[bytes], bytes]) -> bytes:
    output = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as source, ZipFile(output, "w", ZIP_DEFLATED) as dest:
        for info in source.infolist():
            raw = source.read(info.filename)
            if info.filename == member:
                raw = change(raw)
            dest.writestr(info, raw)
    return output.getvalue()


def xls_bytes(
    rows: list[list[object]],
    *,
    sheets: tuple[str, ...] = ("Legacy Cito",),
    headers: list[object] | None = None,
) -> bytes:
    book = xlwt.Workbook()
    unrelated = book.add_sheet("Unrelated")
    unrelated.write(0, 0, "Not a Cito schema")
    for name in sheets:
        table = book.add_sheet(name)
        for rowno, row in enumerate([headers or CITO_HEADERS, *rows]):
            for colno, value in enumerate(row):
                if value is not None:
                    table.write(rowno, colno, value)
    result = io.BytesIO()
    book.save(result)
    return result.getvalue()


def mutate_biff(
    data: bytes,
    wanted: int,
    transform: Callable[[bytearray, int, int], None],
) -> bytes:
    """Replace one BIFF record in place without changing OLE stream lengths."""
    output = io.BytesIO(data)
    with olefile.OleFileIO(output, write_mode=True) as archive:
        stream = bytearray(archive.openstream("Workbook").read())
        offset = 0
        while offset + 4 <= len(stream):
            record, length = struct.unpack_from("<HH", stream, offset)
            if record == wanted:
                transform(stream, offset, length)
                archive.write_stream("Workbook", bytes(stream))
                return output.getvalue()
            offset += length + 4
    raise AssertionError("Synthetic BIFF fixture lacks the requested record")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (123, "000123"),
        (123.0, "000123"),
        ("000123", "000123"),
        ("123.0", "000123"),
        (0, "000000"),
        (999999, "999999"),
    ],
)
def test_product_id_preserves_six_digits(value: object, expected: str) -> None:
    assert product_id(value) == expected


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "12x", "1e3", 1000000, "1234567"])
def test_product_id_rejects_invalid_without_repair(value: object) -> None:
    with pytest.raises(DataError):
        product_id(value)


def test_group_ids_are_not_product_ids_and_missing_stays_missing() -> None:
    assert group_id(12) == "12"
    assert group_id("0012") == "0012"
    assert group_id(None) is None
    assert group_id(" ") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("999,99", 99999),
        ("1.234,56", 123456),
        ("1234", 123400),
        (100.25, 10025),
        (0, 0),
        (0.30000000000000004, 30),
        (None, None),
        (" ", None),
    ],
)
def test_price_boundary_exact_cents(value: object, expected: int | None) -> None:
    assert price_ore(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "1,234.56",
        "12.34",
        "NaN",
        float("inf"),
        float("nan"),
        -1,
        "-0,01",
        1.001,
        "1,001",
        True,
    ],
)
def test_price_invalid_or_meaningful_subcent_precision_rejected(value: object) -> None:
    with pytest.raises(DataError):
        price_ore(value)


def test_xlsx_cito_hidden_reordered_alias_optional_missing() -> None:
    headers = [" SubGrp ", " NY SUBKODE ", " KOSTPRIS ", "VARENR", "SUBKODE", "OPBEVKODE"]
    data = xlsx_bytes(headers, [[12, "C", "100,25", "000123", "A", "N"]], hidden=True)
    snapshot = parse_cito(data, "arbitrary-valid-name.xlsx")
    assert snapshot.input_rows == snapshot.unique_required_rows == 1
    assert snapshot.duplicate_rows == 0
    assert snapshot.items["000123"].current_cost_ore == 10025
    assert snapshot.items["000123"].group_id == "12"
    assert snapshot.items["000123"].source_rows == (2,)


@pytest.mark.parametrize("headers", [CITO_HEADERS[:-1], [*CITO_HEADERS, "ny subkode"]])
def test_xlsx_missing_or_ambiguous_canonical_header_blocks(headers: list[object]) -> None:
    with pytest.raises(DataError):
        parse_cito(xlsx_bytes(headers, [VALID_CITO]))


def test_six_required_duplicates_collapse_and_conflicts_never_choose_winner() -> None:
    rows = [
        [*VALID_CITO, "optional one"],
        [*VALID_CITO, "different discarded value"],
        [124, "N", 10, "B", "C", 12],
        [124, "N", 11, "B", "C", 13],
        [125, "K", 50, "A", "A", 12],
    ]
    snapshot = parse_cito(xlsx_bytes([*CITO_HEADERS, "tekst"], rows))
    assert snapshot.input_rows == 5
    assert snapshot.duplicate_rows == 1
    assert snapshot.unique_required_rows == 4
    assert set(snapshot.items) == {"000123", "000125"}
    assert snapshot.items["000123"].source_rows == (2, 3)
    assert snapshot.items["000125"].storage_code == "K"
    assert snapshot.conflicted_ids == {"000124"}
    assert snapshot.incomplete_groups == {"12", "13"}


@pytest.mark.parametrize("field", range(6))
def test_formula_in_each_required_cito_column_blocks(field: int) -> None:
    row = list(VALID_CITO)
    row[field] = "=1+1"
    data = rewrite_zip(
        xlsx_bytes(CITO_HEADERS, [row]),
        "xl/worksheets/sheet1.xml",
        lambda raw: raw.replace(b"<f>1+1</f><v></v>", b"<f>1+1</f><v>100</v>"),
    )
    with pytest.raises(DataError):
        parse_cito(data)


def test_irrelevant_formula_is_not_a_dependency() -> None:
    parsed = parse_cito(xlsx_bytes([*CITO_HEADERS, "optional"], [[*VALID_CITO, "=1+1"]]))
    assert set(parsed.items) == {"000123"}


def test_xlsx_stale_dimensions_named_ranges_and_hidden_rows_do_not_truncate() -> None:
    data = xlsx_bytes(
        HISTORY_HEADERS,
        [
            [123, "AIP", 100, 120, 118],
            [124, "AIP", 80, None, 90],
        ],
        sheet="Medicinpriser data",
        stale=True,
        hidden=True,
    )
    parsed = parse_lms_history(data)
    assert parsed.periods == (date(2026, 1, 5), date(2026, 1, 19), date(2026, 2, 2))
    assert parsed.prices == {"000123": (10000, 12000, 11800), "000124": (8000, None, 9000)}


def test_lms_only_exact_aip_rows_date_order_and_missingness() -> None:
    headers = ["20260202", "Indikator", "Varenummer", "20260105", "20260119"]
    data = xlsx_bytes(
        headers,
        [
            [118, "AIP", "000123", 100, 120],
            [999, "AUP", "000123", 999, 999],
            [1, "DDD", "000123", 1, 1],
            [999, "aip", "000124", 999, 999],
            [999, "AIP ", "000125", 999, 999],
            [0, "AIP", "000126", None, "bad"],
        ],
        sheet="Medicinpriser data",
    )
    parsed = parse_lms_history(data)
    assert parsed.prices == {"000123": (10000, 12000, 11800), "000126": (None, None, 0)}
    assert {issue.code for issue in parsed.issues} == {"invalid_aip", "zero_aip"}


def test_lms_duplicates_preserve_provenance_and_only_ambiguous_observations_are_removed() -> None:
    data = xlsx_bytes(
        HISTORY_HEADERS,
        [
            [123, "AIP", 100, 120, 118],
            [123, "AIP", 100, 120, 118],
            [124, "AIP", 100, 120, 118],
            [124, "AIP", 100, 121, 118],
        ],
        sheet="Medicinpriser data",
    )
    parsed = parse_lms_history(data)
    assert parsed.prices == {
        "000123": (10000, 12000, 11800),
        "000124": (10000, None, 11800),
    }
    assert parsed.source_rows["000123"] == (2, 3)
    assert parsed.conflicted_ids == {"000124"}
    assert parsed.conflicted_periods == {"000124": {date(2026, 1, 19)}}
    assert any(issue.code == "history_conflict" for issue in parsed.issues)


@pytest.mark.parametrize("header", ["20260230", "not a date", "2026-01-05", "20260105"])
def test_lms_bad_or_duplicate_date_header_blocks(header: str) -> None:
    headers = ["Varenummer", "Indikator", "20260105", header, "20260202"]
    with pytest.raises(DataError):
        parse_lms_history(xlsx_bytes(headers, [[123, "AIP", 1, 2, 3]], "Medicinpriser data"))


@pytest.mark.parametrize("field", [0, 1, 2])
def test_lms_required_formula_is_not_a_cached_observation(field: int) -> None:
    row: list[object] = [123, "AIP", 100, 120, 118]
    row[field] = "=1+1"
    with pytest.raises(DataError):
        parse_lms_history(xlsx_bytes(HISTORY_HEADERS, [row], "Medicinpriser data"))


def test_lms_irregular_calendar_warning_preserves_actual_dates() -> None:
    parsed = parse_lms_history(
        xlsx_bytes(
            ["Varenummer", "Indikator", "20260105", "20260120"],
            [[123, "AIP", 100, 110]],
            "Medicinpriser data",
        )
    )
    assert parsed.periods == (date(2026, 1, 5), date(2026, 1, 20))
    assert [issue.code for issue in parsed.issues] == ["irregular_calendar"]


def test_legacy_xls_unique_matching_sheet_without_table_name() -> None:
    parsed = parse_cito(xls_bytes([VALID_CITO]), "arbitrary.xls")
    assert parsed.sheet_name == "Legacy Cito"
    assert parsed.items["000123"].current_cost_ore == 10025
    assert parsed.items["000123"].source_rows == (2,)


@pytest.mark.parametrize("sheets", [(), ("First match", "Second match")])
def test_legacy_xls_requires_exactly_one_schema_match(sheets: tuple[str, ...]) -> None:
    with pytest.raises(DataError):
        parse_cito(xls_bytes([VALID_CITO], sheets=sheets), "arbitrary.xls")


def test_legacy_xls_duplicate_alias_headers_block() -> None:
    with pytest.raises(DataError):
        parse_cito(xls_bytes([VALID_CITO], headers=[*CITO_HEADERS, "ny subkode"]), "arbitrary.xls")


@pytest.mark.parametrize("field", range(6))
def test_legacy_xls_required_formulas_block_even_cached_empty(field: int) -> None:
    row = list(VALID_CITO)
    row[field] = xlwt.Formula("1+1")
    with pytest.raises(DataError, match="Formler"):
        parse_cito(xls_bytes([row]), "legacy.xls")


def test_legacy_xls_optional_formula_is_ignored() -> None:
    parsed = parse_cito(
        xls_bytes(
            [[*VALID_CITO, xlwt.Formula("1+1")]],
            headers=[*CITO_HEADERS, "optional"],
        ),
        "legacy.xls",
    )
    assert set(parsed.items) == {"000123"}


def test_chart_sheet_before_data_cannot_shift_formula_detection() -> None:
    def chart_record(stream: bytearray, offset: int, length: int) -> None:
        assert length >= 6
        stream[offset + 4 + 5] = 2

    row = list(VALID_CITO)
    row[2] = xlwt.Formula("1+1")
    # The recipe places its unrelated sheet first; relabel that BOUNDSHEET as a chart.
    data = mutate_biff(xls_bytes([row]), 0x0085, chart_record)
    with pytest.raises(DataError, match="Formler"):
        parse_cito(data, "legacy.xls")


def test_legacy_xls_reordered_normalised_headers_and_alias() -> None:
    data = xls_bytes(
        [[12, "C", "100,25", "000123", "A", "N"]],
        headers=[" SubGrp ", " NY SUBKODE ", " KOSTPRIS ", "VARENR", "SUBKODE", "OPBEVKODE"],
    )
    parsed = parse_cito(data, "legacy.xls")
    assert parsed.items["000123"].current_cost_ore == 10025
    assert parsed.items["000123"].group_id == "12"


def test_legacy_xls_filepass_rejected_before_xlrd() -> None:
    def encrypt_record(stream: bytearray, offset: int, length: int) -> None:
        del length
        struct.pack_into("<H", stream, offset, 0x002F)

    data = mutate_biff(xls_bytes([VALID_CITO]), 0x0042, encrypt_record)
    with pytest.raises(DataError, match="Krypterede"):
        parse_cito(data, "legacy.xls")


def test_legacy_xls_macro_sheet_rejected_before_xlrd() -> None:
    def macro_record(stream: bytearray, offset: int, length: int) -> None:
        assert length >= 6
        stream[offset + 4 + 5] = 1

    data = mutate_biff(xls_bytes([VALID_CITO]), 0x0085, macro_record)
    with pytest.raises(DataError, match="Makroark"):
        parse_cito(data, "legacy.xls")


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"", "empty.xlsx"),
        (b"not an Excel file", "fake.xlsx"),
        (b"not an OLE file", "fake.xls"),
        (b"123", "fake.xlsm"),
    ],
)
def test_corrupt_or_unsupported_upload_becomes_safe_domain_error(data: bytes, name: str) -> None:
    with pytest.raises(DataError):
        parse_cito(data, name)


def test_wrong_sheet_does_not_fall_back_to_presentation_sheet() -> None:
    with pytest.raises(DataError):
        parse_cito(xlsx_bytes(CITO_HEADERS, [VALID_CITO], "Wrong"))
    with pytest.raises(DataError):
        parse_lms_history(xlsx_bytes(HISTORY_HEADERS, [[123, "AIP", 1, 2, 3]], "Medicinpriser"))


@pytest.mark.parametrize("macro_type", ["vba", "content-type", "macro-sheet"])
def test_xlsx_macros_rejected_even_with_xlsx_extension(macro_type: str) -> None:
    data = xlsx_bytes(CITO_HEADERS, [VALID_CITO])
    if macro_type == "content-type":
        data = rewrite_zip(
            data,
            "[Content_Types].xml",
            lambda raw: raw.replace(
                b"spreadsheetml.sheet.main+xml",
                b"ms-excel.sheet.macroEnabled.main+xml",
            ),
        )
    else:
        output = io.BytesIO(data)
        with ZipFile(output, "a") as archive:
            archive.writestr(
                "xl/vbaProject.bin" if macro_type == "vba" else "xl/macrosheets/sheet1.xml",
                b"synthetic",
            )
        data = output.getvalue()
    with pytest.raises(DataError, match="Makro"):
        parse_cito(data)


@pytest.mark.parametrize("member", ["xl/workbook.xml", "xl/worksheets/sheet1.xml"])
def test_malformed_xml_is_a_safe_domain_error(member: str) -> None:
    data = rewrite_zip(xlsx_bytes(CITO_HEADERS, [VALID_CITO]), member, lambda _: b"<not-closed")
    with pytest.raises(DataError):
        parse_cito(data)


def test_lazily_malformed_sheet_data_is_a_safe_domain_error() -> None:
    # Dimensions remain valid, so the corruption is encountered only in row iteration.
    data = rewrite_zip(
        xlsx_bytes(CITO_HEADERS, [VALID_CITO]),
        "xl/worksheets/sheet1.xml",
        lambda raw: raw.replace(b"</sheetData>", b"</broken>"),
    )
    with pytest.raises(DataError):
        parse_cito(data)


@pytest.mark.parametrize(
    "limit", ["MAX_UPLOAD_BYTES", "MAX_EXPANDED_BYTES", "MAX_ROWS", "MAX_COLUMNS", "MAX_CELLS"]
)
def test_resource_limits_fail_safely_without_large_fixtures(
    limit: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bef_redbird.imports as imports

    data = xlsx_bytes(CITO_HEADERS, [VALID_CITO, VALID_CITO])
    monkeypatch.setattr(imports, limit, 2)
    with pytest.raises(DataError):
        parse_cito(data)


@pytest.mark.parametrize("legacy", [False, True])
def test_excel_error_cell_in_storage_cannot_become_valid_cito_row(legacy: bool) -> None:
    if legacy:
        # BOOLERR (0x0205): turn a harmless bool in storage into an Excel error.
        def error_record(stream: bytearray, offset: int, length: int) -> None:
            assert length == 8
            stream[offset + 4 + 6] = 0x2A  # #N/A
            stream[offset + 4 + 7] = 1  # error, not boolean

        row = list(VALID_CITO)
        row[1] = True
        data = mutate_biff(xls_bytes([row]), 0x0205, error_record)
        parsed = parse_cito(data, "legacy.xls")
    else:
        row = list(VALID_CITO)
        row[1] = "#N/A"  # openpyxl emits Excel error cell type e
        parsed = parse_cito(xlsx_bytes(CITO_HEADERS, [row]))
    assert "000123" not in parsed.items
    assert any(issue.code == "invalid_cito" for issue in parsed.issues)


def test_provenance_hashes_bytes_and_discards_uploaded_directory() -> None:
    data = b"synthetic bytes"
    info = source_info(data, "C:\\private\\folder\\sample.xlsx")
    assert info.name == "sample.xlsx"
    assert info.sha256 == hashlib.sha256(data).hexdigest()
    assert info.size == 15
