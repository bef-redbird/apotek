"""Synthetic supplement fixtures; no private native source values."""

from copy import deepcopy

import pytest

from bef_redbird.foundation.ftp_supplements import independent_audit, parse_supplement


def _dok(rows: str = "000001, Example, tablets, 2,5 mg, 20 tablets, A01AA01") -> bytes:
    return (
        "Laegemiddelstyrelsen, den 09-10-2026\n\n\n"
        "Aendringer i tilskudspriser per 12-10-2026\n\n"
        "Ved substitution udleveres det billigste laegemiddel i en substitutionsgruppe "
        "(A-produktet). Hvis apoteket paa grund af leveringssvigt \n"
        "ikke kan skaffe det billigste laegemiddel i gruppen, vil der den 12-10-2026 "
        "ogsaa vaere fuldt tilskud til nedenstaaende varenumre, som nu er \n"
        "bestemmende for tilskudsprisen i deres respektive tilskudsgrupper paa trods af, "
        "at de ikke er gruppens billigste. \n\n"
        "Varenummer, navn, form, styrke, pakningsstoerrelse, ATC-kode \n\n" + rows + "\n"
    ).encode("cp865")


def _csv(kind: str, rows: str = "") -> bytes:
    header = {
        "LMS36": "DrugID;UdvidetOrdinationskode;GældendeFra;GældendeTil",
        "LMS37": "UdvidetOrdinationskode;Faggruppe;Specialenavn",
    }[kind]
    return (header + "\r\n" + rows).encode("cp1252")


def test_dok_exact_dates_unambiguous_fields_and_conditional_semantics() -> None:
    data = _dok()
    result = parse_supplement(data, "DOK")
    row = result["records"][0]
    assert row == {
        "kind": "DOK",
        "source_line": 12,
        "byte_offset": data.index(b"000001"),
        "byte_length": len(b"000001, Example, tablets, 2,5 mg, 20 tablets, A01AA01"),
        "package_code": "000001",
        "source_description": "Example, tablets, 2,5 mg, 20 tablets",
        "atc_code": "A01AA01",
        "issue_date": "2026-10-09",
        "effective_date": "2026-10-12",
        "assertion": "conditional_reimbursement_exception",
    }
    assert result["metadata"]["supply_evidence"] is False
    assert result["metadata"]["availability"] == "unknown"
    assert result["coverage"]["absence_means"] == "unknown"
    assert result["coverage"]["status"] == "complete"
    assert independent_audit(data, "DOK", result["records"])["accepted"]


def test_dok_oem_text_and_duplicate_assertions_preserved() -> None:
    line = "000001, Blåbær, tablets, 1 mg, 20 tablets, A01AA01"
    data = _dok(line + "\n" + line)
    result = parse_supplement(data, "DOK")
    assert len(result["records"]) == 2
    assert result["records"][0]["source_description"].startswith("Blåbær")
    assert result["metadata"]["duplicate_record_count"] == 1
    assert result["metadata"]["duplicate_package_count"] == 1
    assert result["metadata"]["distinct_package_count"] == 1
    assert independent_audit(data, "DOK", result["records"])["accepted"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"12-10-2026", b"1-10-2026"),
        (b"12-10-2026", b"31-02-2026"),
        (b"09-10-2026", b"99-10-2026"),
        (b"fuldt tilskud", b"lavt tilskud"),
        (b"Varenummer, navn", b"Pakkenummer, navn"),
    ],
)
def test_unknown_dok_layout_is_raw_only(old: bytes, new: bytes) -> None:
    data = _dok().replace(old, new)
    result = parse_supplement(data, "DOK")
    assert result["records"] == []
    assert result["coverage"]["status"] == "raw_only"
    assert not independent_audit(data, "DOK", [])["accepted"]


def test_ambiguous_oem_bytes_are_raw_only() -> None:
    data = _dok().replace(b"Example", b"\x9e")
    result = parse_supplement(data, "DOK")
    assert result["records"] == []
    assert result["issues"] == [{"code": "ambiguous_oem_text", "source_line": None}]
    assert not independent_audit(data, "DOK", [])["accepted"]


@pytest.mark.parametrize("kind", ["LMS36", "LMS37"])
def test_header_only_csv_has_explicit_zero_coverage(kind: str) -> None:
    data = _csv(kind)
    result = parse_supplement(data, kind)
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["accepted_rows"] == 0
    assert independent_audit(data, kind, result["records"])["accepted"]


def test_lms36_dates_identifiers_and_unknown_end() -> None:
    data = _csv("LMS36", "00000000001;EXAMPLE;20240229;\r\n")
    result = parse_supplement(data, "LMS36")
    assert result["records"] == [
        {
            "kind": "LMS36",
            "source_line": 2,
            "byte_offset": data.index(b"00000000001"),
            "byte_length": len(b"00000000001;EXAMPLE;20240229;"),
            "drug_id": "00000000001",
            "extended_prescribing_code": "EXAMPLE",
            "valid_from": "2024-02-29",
            "valid_to": None,
            "assertion": "prescribing_permission",
        }
    ]
    assert result["metadata"]["effective_date"] is None
    assert result["metadata"]["issue_date"] is None
    assert result["metadata"]["blank_end_date"] == "unknown"
    assert independent_audit(data, "LMS36", result["records"])["accepted"]


def test_lms36_explicit_end_preserved() -> None:
    data = _csv("LMS36", "00000000001;EXAMPLE;20240229;20240301\r\n")
    result = parse_supplement(data, "LMS36")
    assert result["records"][0]["valid_to"] == "2024-03-01"
    assert independent_audit(data, "LMS36", result["records"])["accepted"]


def test_lms37_reference_danish_text_and_optional_specialty() -> None:
    data = _csv("LMS37", "EXAMPLE;Læge;\r\nOTHER;Læge;Eksempel\r\n")
    result = parse_supplement(data, "LMS37")
    assert result["records"][0]["professional_group"] == "Læge"
    assert result["records"][0]["specialty_name"] is None
    assert result["records"][1]["specialty_name"] == "Eksempel"
    assert all(r["assertion"] == "prescribing_reference" for r in result["records"])
    assert independent_audit(data, "LMS37", result["records"])["accepted"]


def test_quoted_csv_semicolons_and_escaped_quote() -> None:
    data = _csv("LMS37", 'EXAMPLE;"Læge; eksempel";"A ""quote"""\r\n')
    result = parse_supplement(data, "LMS37")
    assert result["records"][0]["professional_group"] == "Læge; eksempel"
    assert result["records"][0]["specialty_name"] == 'A "quote"'
    assert independent_audit(data, "LMS37", result["records"])["accepted"]


@pytest.mark.parametrize(
    "row",
    [
        "00000000001;EXAMPLE;20230229;",
        "00000000001;EXAMPLE;2024-02-29;",
        "00000000001;EXAMPLE;2024011;",
        "00000000001;EXAMPLE;20240101;00000000",
        "00000000001;EXAMPLE;20240229;20240228",
        "00000000001;EXAMPLE;;",
        "0000000001;EXAMPLE;20240229;",
        "00000000001;EXAMPLE;20240229;;",
        "00000000001;EXAMPLE;20240229",
        "00000000001;;20240229;",
        "00000000001; EXAMPLE;20240229;",
        '00000000001;"EXAMPLE;20240229;',
    ],
)
def test_bad_csv_row_quarantined_audit_fails(row: str) -> None:
    data = _csv("LMS36", row + "\r\n")
    result = parse_supplement(data, "LMS36")
    assert result["records"] == []
    assert result["coverage"] == {
        "status": "partial",
        "candidate_rows": 1,
        "accepted_rows": 0,
        "rejected_rows": 1,
        "scope": "prescribing_permission",
        "absence_means": "unknown",
    }
    assert not independent_audit(data, "LMS36", result["records"])["accepted"]


@pytest.mark.parametrize("data", [b"", b"DrugID,Other\n", b"\xef\xbb\xbfDrugID;Other\n"])
def test_unsupported_csv_header_is_raw_only(data: bytes) -> None:
    result = parse_supplement(data, "LMS36")
    assert result["coverage"]["status"] == "raw_only"
    assert result["records"] == []
    assert not independent_audit(data, "LMS36", [])["accepted"]


def test_undefined_cp1252_byte_is_raw_only() -> None:
    data = _csv("LMS37", "EXAMPLE;Group;\r\n").replace(b"Group", b"\x81")
    result = parse_supplement(data, "LMS37")
    assert result["coverage"]["status"] == "raw_only"
    assert result["records"] == []
    assert not independent_audit(data, "LMS37", [])["accepted"]


def test_duplicate_csv_rows_retained_and_counted() -> None:
    data = _csv("LMS37", "EXAMPLE;Group;\r\nEXAMPLE;Group;\r\n")
    result = parse_supplement(data, "LMS37")
    assert len(result["records"]) == 2
    assert result["metadata"]["duplicate_record_count"] == 1
    assert independent_audit(data, "LMS37", result["records"])["accepted"]


@pytest.mark.parametrize("kind", ["DOK", "LMS36", "LMS37"])
@pytest.mark.parametrize("corruption", ["value", "locator", "missing", "extra"])
def test_audit_independently_detects_corruption(kind: str, corruption: str) -> None:
    data = {
        "DOK": _dok(),
        "LMS36": _csv("LMS36", "00000000001;EXAMPLE;20240229;\r\n"),
        "LMS37": _csv("LMS37", "EXAMPLE;Group;\r\n"),
    }[kind]
    records = deepcopy(parse_supplement(data, kind)["records"])
    if corruption == "value":
        records[0]["assertion"] = "supply_disruption"
    elif corruption == "locator":
        records[0]["byte_offset"] += 1
    elif corruption == "missing":
        records.clear()
    else:
        records.append(deepcopy(records[0]))
    assert not independent_audit(data, kind, records)["accepted"]


def test_unsupported_kind_has_no_implicit_supply_adapter() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        parse_supplement(b"", "SUPPLY")
    with pytest.raises(ValueError, match="Unsupported"):
        independent_audit(b"", "SUPPLY", [])
