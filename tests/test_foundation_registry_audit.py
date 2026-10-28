"""Independently invented bytes/typed expectations, including corrupt persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from bef_redbird.foundation import lms_registry_audit as auditor
from bef_redbird.foundation.lms_registry_audit import audit_member, audit_release


def native(width: int, *cells: tuple[int, bytes]) -> bytes:
    result = bytearray(b" " * width)
    for first, value in cells:
        result[first - 1 : first - 1 + len(value)] = value
    assert len(result) == width
    return bytes(result) + b"\r\n"


def persisted(raw: bytes, values: dict[str, Any], *, identity: int = 101) -> dict[str, Any]:
    return {
        "row_id": identity,
        "raw_sha256": hashlib.sha256(raw[:-2]).hexdigest(),
        "field_states": {name: "blank" for name, value in values.items() if value is None},
        **values,
    }


def price_example() -> tuple[bytes, dict[str, Any]]:
    # Spec LMS03 positions 1–6 + ten nine-byte fields, with differing slot values.
    raw = b"000042" + b"".join(f"{n:09d}".encode() for n in range(10)) + b"\r\n"
    return raw, persisted(
        raw,
        {
            "package_number": "000042",
            "aip_ore": 0,
            "registered_ore": 1,
            "consumer_ore": 2,
            "legacy_05": "000000003",
            "legacy_06": "000000004",
            "reimbursement_ore": 5,
            "hospital_slot_08": 6,
            "legacy_09": "000000007",
            "non_reimbursable_ore": 8,
            "legacy_11": "000000009",
        },
    )


def test_exact_ore_lexical_slots_and_leading_zero_identity() -> None:
    raw, row = price_example()
    result = audit_member(3, raw, [row])
    assert result["complete"]
    assert result["expected_sha256"] == result["actual_sha256"]
    assert result["member_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["counts"]["compared_fields"] == 13


def test_ascii_number_classification_covers_every_byte() -> None:
    from bef_redbird.foundation.lms_registry_audit import _ascii_number

    assert not _ascii_number(b"")
    for value in range(256):
        expected = 48 <= value <= 57
        assert _ascii_number(bytes([value])) is expected
        assert _ascii_number(b"0123" + bytes([value]) + b"9876") is expected


def test_interpretation_cache_is_bounded_and_keeps_field_kinds_distinct() -> None:
    from bef_redbird.foundation.lms_registry_audit import _interpret

    _interpret.cache_clear()
    try:
        assert _interpret(b"000042", "d") == ("000042", None)
        assert _interpret(b"000042", "o") == (42, None)
        assert _interpret(b"000042", "t") == ("000042", None)
        assert _interpret(b"000042", "c") == (None, "invalid")
        assert _interpret(b"20240229", "c") == ("2024-02-29", None)
        assert _interpret(b"20230229", "c") == (None, "invalid")
        assert _interpret(b"00000000", "c") == (None, "unknown_date")
        assert _interpret(b"\xf3", "t") == (None, "ambiguous_encoding")
        assert _interpret(b"\t ", "t") == (None, "blank")
        before = _interpret.cache_info().hits
        assert _interpret(b"000042", "d") == ("000042", None)
        assert _interpret.cache_info().hits == before + 1
        for index in range(70000):
            value, state = _interpret(str(index).encode(), "o")
            assert value == index and state is None
        assert _interpret.cache_info().maxsize == 65536
        assert _interpret.cache_info().currsize == 65536
    finally:
        _interpret.cache_clear()


def test_expected_records_and_nested_states_do_not_share_cached_mutable_values() -> None:
    from bef_redbird.foundation.lms_registry_audit import _canonical, _expected

    raw = native(69, (1, b"28100000001"), (12, b"00000000"), (20, b"Original name"))[:-2]
    first = _expected(10, raw)
    canonical = _canonical(first)
    first["previous_name"] = "Changed outside the auditor"
    first["field_states"]["change_date"] = "blank"
    first["field_states"]["invented"] = "invalid"
    second = _expected(10, raw)
    assert second["previous_name"] == "Original name"
    assert second["field_states"] == {"change_date": "unknown_date"}
    assert _canonical(second) == canonical
    assert first is not second
    assert first["field_states"] is not second["field_states"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("aip_ore", False),
        ("aip_ore", 0.0),
        ("registered_ore", True),
        ("registered_ore", 1.0),
        ("aip_ore", None),
        ("raw_sha256", "0" * 64),
        ("field_states", '{"aip_ore":"invalid"}'),
        ("row_id", "101"),
    ],
)
def test_warm_record_cache_preserves_complete_typed_inputs(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    raw, row = price_example()
    row["field_states"] = json.dumps(row["field_states"])
    changed = {**row, field: value}
    monkeypatch.setattr(auditor, "_record_cache", auditor._RecordCache(0))
    uncached = audit_member(3, raw, [changed])
    cache = auditor._RecordCache()
    monkeypatch.setattr(auditor, "_record_cache", cache)
    accepted = audit_member(3, raw, [row])
    assert accepted["complete"]
    assert audit_member(3, raw, [row]) == accepted
    assert cache.hits >= 2
    checked = audit_member(3, raw, [changed])
    assert checked == uncached
    assert not checked["complete"]
    if field != "row_id":
        assert checked["actual_sha256"] != accepted["actual_sha256"]


def test_record_cache_evicts_without_losing_occurrences_or_proofs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = auditor._RecordCache(16 * 1024)
    monkeypatch.setattr(auditor, "_record_cache", cache)
    raw, row = price_example()
    row["field_states"] = json.dumps(row["field_states"])
    first = audit_member(3, raw + raw, [row, row])
    for index in range(100):
        other = f"{index:06d}".encode() + raw[6:]
        changed = {**row, "package_number": f"{index:06d}"}
        changed["raw_sha256"] = hashlib.sha256(other[:-2]).hexdigest()
        assert audit_member(3, other, [changed])["complete"]
        assert cache.retained_bytes <= cache.limit_bytes
    assert cache.evictions > 0
    assert audit_member(3, raw + raw, [row, row]) == first
    assert first["counts"]["source_records"] == 2
    assert first["counts"]["compared_fields"] == 26


def test_warm_record_cache_preserves_missingness_dates_and_mutable_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auditor, "_record_cache", auditor._RecordCache())
    raw = native(69, (1, b"28100000001"), (12, b"20240229"))
    row = persisted(
        raw, {"drug_id": "28100000001", "change_date": date(2024, 2, 29), "previous_name": None}
    )
    row["field_states"] = json.dumps(row["field_states"])
    accepted = audit_member(10, raw, [row])
    assert accepted["complete"]
    assert audit_member(10, raw, [row]) == accepted
    assert audit_member(10, raw, [{**row, "change_date": "2024-02-29"}]) == accepted
    assert not audit_member(10, raw, [{**row, "change_date": date(2024, 3, 1)}])["complete"]
    omitted = dict(row)
    del omitted["previous_name"]
    assert audit_member(10, raw, [omitted])["mismatches"]["missing_column:previous_name"] == 1
    row["field_states"] = {"previous_name": "blank"}
    assert audit_member(10, raw, [row]) == accepted
    row["field_states"]["previous_name"] = "unknown_date"
    assert audit_member(10, raw, [row])["mismatches"]["field_states"] == 1
    changed_native = raw.replace(b"20240229", b"20240301")
    assert audit_member(10, changed_native, [row])["expected_sha256"] != accepted["expected_sha256"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("aip_ore", 1),
        ("registered_ore", 1.0),
        ("consumer_ore", True),
        ("hospital_slot_08", "000000006"),
        ("legacy_09", None),
        ("package_number", "42"),
        ("raw_sha256", "0" * 64),
    ],
)
def test_wrong_persisted_money_type_identifier_and_legacy_value_fail(
    field: str, value: Any
) -> None:
    raw, row = price_example()
    row[field] = value
    result = audit_member(3, raw, [row])
    assert result["mismatches"][field] == 1
    assert not result["complete"]
    assert result["expected_sha256"] != result["actual_sha256"]


def test_omitted_null_column_is_not_treated_as_persisted_null() -> None:
    raw = native(69, (1, b"28100000001"), (12, b"00000000"))
    row = persisted(raw, {"drug_id": "28100000001", "change_date": None, "previous_name": None})
    row["field_states"]["change_date"] = "unknown_date"
    del row["previous_name"]
    assert audit_member(10, raw, [row])["mismatches"]["missing_column:previous_name"] == 1


@pytest.mark.parametrize(
    "token,expected,state",
    [
        (b"20240229", "2024-02-29", None),
        (b"20230229", None, "invalid"),
        (b"2024-229", None, "invalid"),
        (b"00000000", None, "unknown_date"),
        (b"        ", None, "blank"),
    ],
)
def test_calendar_missingness_and_native_name_offset(
    token: bytes,
    expected: str | None,
    state: str | None,
) -> None:
    raw = native(69, (1, b"28100000001"), (12, token), (20, b"Old name"))
    row = persisted(
        raw, {"drug_id": "28100000001", "change_date": expected, "previous_name": "Old name"}
    )
    if state:
        row["field_states"]["change_date"] = state
    result = audit_member(10, raw, [row])
    assert result["complete"]
    row["change_date"] = "2024-03-01"
    assert audit_member(10, raw, [row])["mismatches"]["change_date"] == 1


def test_native_date_object_and_serialized_state_readback() -> None:
    raw = native(69, (1, b"28100000001"), (12, b"20240229"))
    row = persisted(
        raw, {"drug_id": "28100000001", "change_date": date(2024, 2, 29), "previous_name": None}
    )
    row["field_states"] = json.dumps(row["field_states"])
    assert audit_member(10, raw, [row])["complete"]


@pytest.mark.parametrize(
    "name,expected,state",
    [
        (b"Synthetic \x91 name", "Synthetic æ name", None),
        (b"Ambiguous \xf3 name", None, "ambiguous_encoding"),
        (b"\t ", None, "blank"),
    ],
)
def test_encoding_ambiguity_and_blank_have_distinct_persisted_states(
    name: bytes,
    expected: str | None,
    state: str | None,
) -> None:
    raw = native(69, (1, b"28100000001"), (12, b"00000000"), (20, name))
    row = persisted(raw, {"drug_id": "28100000001", "change_date": None, "previous_name": expected})
    row["field_states"]["change_date"] = "unknown_date"
    if state:
        row["field_states"]["previous_name"] = state
    assert audit_member(10, raw, [row])["complete"]
    if state == "ambiguous_encoding":
        row["previous_name"] = "Ambiguous ¾ name"
        assert not audit_member(10, raw, [row])["complete"]
        row["previous_name"] = None
        row["field_states"]["previous_name"] = "blank"
        assert audit_member(10, raw, [row])["mismatches"]["field_states"] == 1


def test_invalid_digits_and_money_do_not_become_zero() -> None:
    raw, row = price_example()
    raw = b"12\xb2345" + b"  -123   " + raw[15:]
    row["raw_sha256"] = hashlib.sha256(raw[:-2]).hexdigest()
    row.update(package_number=None, aip_ore=None)
    row["field_states"] = {"package_number": "invalid", "aip_ore": "invalid"}
    assert audit_member(3, raw, [row])["complete"]
    row["aip_ore"] = 0
    assert not audit_member(3, raw, [row])["complete"]


def test_all_drug_field_boundaries_and_unscaled_attributes() -> None:
    raw = native(
        180,
        (1, b"28100000001"),
        (12, b"SP"),
        (14, b"LM"),
        (16, b"000000123"),
        (25, b"01234"),
        (30, b"Invented product"),
        (60, b"Form description"),
        (80, b"FORM001"),
        (87, b"EXTRA01"),
        (94, b"12.34 mg"),
        (114, b"0000012345"),
        (124, b"mg "),
        (127, b"001234"),
        (133, b"005678"),
        (139, b"A01BC02 "),
        (147, b"01020304"),
        (155, b"J"),
        (156, b"G"),
        (157, b"B"),
        (158, b"X"),
        (159, b"Y"),
        (160, b"0042"),
        (164, b"D"),
        (165, b"20240229"),
        (173, b"00000000"),
    )
    row = persisted(
        raw,
        {
            "drug_id": "28100000001",
            "product_type": "SP",
            "product_subtype": "LM",
            "alphabetic_sequence": "000000123",
            "speciality_number": "01234",
            "name": "Invented product",
            "form": "Form description",
            "form_code": "FORM001",
            "additional_form_token": "EXTRA01",
            "strength": "12.34 mg",
            "strength_numeric_token": "0000012345",
            "strength_unit": "mg",
            "authorisation_holder": "001234",
            "drug_distributor": "005678",
            "atc": "A01BC02",
            "administration_codes": "01020304",
            "traffic_warning": "J",
            "substitution_flag": "G",
            "renewal_token": "B",
            "legacy_20": "X",
            "legacy_21": "Y",
            "drug_substitution_group": "0042",
            "dose_suitable": "D",
            "deregistration_date": "2024-02-29",
            "quarantine_date": None,
        },
    )
    row["field_states"]["quarantine_date"] = "unknown_date"
    assert audit_member(1, raw, [row])["complete"]
    row["authorisation_holder"], row["drug_distributor"] = (
        row["drug_distributor"],
        row["authorisation_holder"],
    )
    assert set(audit_member(1, raw, [row])["mismatches"]) == {
        "authorisation_holder",
        "drug_distributor",
    }


def test_all_package_field_boundaries_and_distinct_dates() -> None:
    raw = native(
        137,
        (1, b"28100000001"),
        (12, b"000042"),
        (18, b"003"),
        (21, b"000024"),
        (27, b"005"),
        (30, b"5 x 2 tablets"),
        (60, b"00000100"),
        (68, b"ST"),
        (70, b"BOX1"),
        (74, b"DISP1"),
        (79, b"SPEC1"),
        (84, b"AA"),
        (86, b"CLA01"),
        (91, b"000001234"),
        (100, b"24"),
        (102, b"M"),
        (103, b"C"),
        (104, b"20200102"),
        (112, b"00000000"),
        (120, b"20230229"),
        (128, b"A"),
        (129, b"F"),
        (130, b"B"),
        (131, b"2"),
        (132, b"005678"),
    )
    row = persisted(
        raw,
        {
            "drug_id": "28100000001",
            "package_number": "000042",
            "alphabetic_sequence": "003",
            "component_package": "000024",
            "component_count_token": "005",
            "pack_description": "5 x 2 tablets",
            "pack_size_token": "00000100",
            "pack_unit": "ST",
            "container_code": "BOX1",
            "dispensing_code": "DISP1",
            "speciality_code": "SPEC1",
            "reimbursement_code": "AA",
            "reimbursement_clause": "CLA01",
            "ddd_token": "000001234",
            "storage_time_token": "24",
            "storage_time_unit": "M",
            "storage_code": "C",
            "creation_date": "2020-01-02",
            "last_price_change_date": None,
            "exit_date": None,
            "calculation_code": "A",
            "reimbursement_group_flag": "F",
            "preparation_fee_flag": "B",
            "safety_features_token": "2",
            "package_distributor": "005678",
        },
    )
    row["field_states"].update(last_price_change_date="unknown_date", exit_date="invalid")
    assert audit_member(2, raw, [row])["complete"]
    assert audit_member(2, raw[:-2], [row], allow_final_record=True)["complete"]
    with pytest.raises(ValueError, match="registry_audit_record_terminator"):
        audit_member(2, raw[:-2], [row])
    row["pack_size_token"] = 1
    assert audit_member(2, raw, [row])["mismatches"]["pack_size_token"] == 1


def test_preliminary_placeholder_prices_stay_lexical_and_offsets_match() -> None:
    raw = native(
        60,
        (1, b"28100000001"),
        (12, b"000042"),
        (18, b"0042"),
        (22, b"000000000"),
        (31, b"000000000"),
        (40, b"000000000"),
        (49, b"A"),
        (50, b"28100000002"),
    )
    row = persisted(
        raw,
        {
            "drug_id": "28100000001",
            "package_number": "000042",
            "drug_group": "0042",
            "reserved_minimum_aip_unit_ore": "000000000",
            "reserved_minimum_registered_unit_ore": "000000000",
            "reserved_reimbursement_unit_ore": "000000000",
            "abc_code": "A",
            "cheapest_drug": "28100000002",
        },
    )
    assert audit_member(38, raw, [row])["complete"]
    row["reserved_minimum_aip_unit_ore"] = 0
    assert not audit_member(38, raw, [row])["complete"]


def test_long_indication_description_keeps_its_full_native_extent() -> None:
    text = "A" + "b" * 1998 + "Z"
    raw = native(2003, (1, b"007"), (4, text.encode()))
    row = persisted(raw, {"indication_group": "007", "description": text})
    assert audit_member(35, raw, [row])["complete"]
    row["description"] = text[:-1]
    assert audit_member(35, raw, [row])["mismatches"]["description"] == 1


def test_repeated_occurrences_are_counted_and_order_is_bound() -> None:
    first, first_row = price_example()
    second = b"654321" + first[6:]
    second_row = dict(
        first_row,
        row_id=102,
        package_number="654321",
        raw_sha256=hashlib.sha256(second[:-2]).hexdigest(),
    )
    result = audit_member(3, first + second + first, [first_row, second_row, first_row])
    assert result["complete"]
    assert result["counts"]["source_records"] == 3
    assert result["counts"]["distinct_source_records"] == 2
    assert not audit_member(3, first + second + first, [first_row, second_row])["complete"]
    assert not audit_member(3, first + second + first, [second_row, first_row, first_row])[
        "complete"
    ]
    wrong_identity = dict(first_row, row_id=103)
    assert (
        audit_member(3, first + first, [first_row, wrong_identity])["mismatches"][
            "deduplicated_row_identity"
        ]
        == 1
    )


def test_empty_member_and_extra_persisted_record() -> None:
    assert audit_member(35, b"", [])["complete"]
    _, row = price_example()
    assert audit_member(3, b"", [row])["mismatches"]["record_occurrence"] == 1


@pytest.mark.parametrize(
    "invalid", [b"a" * 96, b"a" * 96 + b"\n", b"\r\n", b"a\nb" + b" " * 93 + b"\r\n"]
)
def test_bad_native_width_or_line_break_cannot_be_audited(invalid: bytes) -> None:
    with pytest.raises(ValueError, match="registry_audit_record_"):
        audit_member(3, invalid, [])


def release_fixture(
    target: Path,
    *,
    vintage: str = "12.0",
    patents: bool = True,
    preliminary: bool = False,
    source_changes: dict[str, bytes] | None = None,
    control_changes: tuple[int, bytes] | None = None,
) -> Path:
    # Separate source recipe. All expected control counts come from synthetic
    # physical originals, never a production layout/parser/normalizer.
    widths = dict(
        zip(
            [1, 2, 3, 4, 5, 7, *range(9, 33), 34, 35],
            [
                180,
                137,
                96,
                47,
                10,
                11,
                60,
                69,
                63,
                152,
                52,
                65,
                65,
                63,
                555,
                67,
                65,
                61,
                71,
                108,
                24,
                60,
                26,
                164,
                18,
                183,
                46,
                367,
                36,
                30,
                21,
                2003,
            ],
            strict=True,
        )
    )
    if not patents:
        del widths[34], widths[35]
    if preliminary:
        for number in (3, 4, 24, 31, 32):
            del widths[number]
        widths[33] = 30
        widths[38] = 60
    files = {f"lms{number:02}.txt": native(width) for number, width in widths.items()}
    files["lms01.txt"] = native(180, (1, b"28100000001"))
    files["lms02.txt"] = native(137, (1, b"28100000001"), (12, b"000042"))
    if "lms03.txt" in files:
        files["lms03.txt"] = native(96, (1, b"000042"))
    files["lms09.txt"] = b""
    files.update(source_changes or {})
    count = len(files) + 1
    cells = [
        (1, b"00"),
        (3, vintage.encode().ljust(5)),
        (8, b"LMS-TAKST"),
        (48, b"20261023"),
        (72, b"01"),
        (74, f"{count:02}".encode()),
        (76, b"LMS-GROS.ZIP" if preliminary else b"LMS.ZIP"),
        (88, b"202642"),
    ]
    if control_changes:
        cells.append(control_changes)
    control = native(93, *cells)
    parameter = b"01" + b"00000" * 9 + b"\r\n"
    catalogue = b"".join(
        native(51, (1, b"10"), (33, name.encode()), (45, b"01"), (47, f"{records:05}".encode()))
        for name, records in [
            *((name, len(data.splitlines())) for name, data in files.items()),
            ("system.txt", count + 2),
        ]
    )
    files["system.txt"] = control + parameter + catalogue
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return target


@pytest.mark.parametrize(
    "vintage,patents,count", [("11.0", False, 31), ("12.0", False, 31), ("12.0", True, 33)]
)
def test_release_native_vintage_structure_and_hashes(
    tmp_path: Path,
    vintage: str,
    patents: bool,
    count: int,
) -> None:
    source = release_fixture(tmp_path / "synthetic.zip", vintage=vintage, patents=patents)
    result = audit_release(source)
    assert result["complete"]
    assert result["schema_version"] == vintage
    assert result["member_count"] == count
    assert result["phase"] == "final"
    # Synthetic release token intentionally differs from the tariff-week token.
    assert result["effective_token"] == "20261023"
    assert result["period_week_token"] == "202642"
    assert result["members"]["lms09.txt"]["physical_records"] == 0
    assert (
        result["members"]["lms03.txt"]["sha256"]
        == hashlib.sha256(native(96, (1, b"000042"))).hexdigest()
    )


def test_preliminary_phase_is_native_control_not_outer_filename(tmp_path: Path) -> None:
    result = audit_release(release_fixture(tmp_path / "lms.zip", preliminary=True))
    assert result["phase"] == "preliminary"
    assert "lms38.txt" in result["members"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ((3, b"13.0"), "system_contract"),
        ((48, b"20260230"), "calendar"),
        ((74, b"99"), "system_contract"),
        ((88, b"20x642"), "system_numeric"),
    ],
)
def test_release_control_corruption_fails(
    tmp_path: Path,
    change: tuple[int, bytes],
    reason: str,
) -> None:
    with pytest.raises(ValueError, match="registry_audit_" + reason):
        audit_release(release_fixture(tmp_path / "bad.zip", control_changes=change))


def test_release_checks_all_member_widths_and_count_completeness(tmp_path: Path) -> None:
    bad = release_fixture(tmp_path / "bad-width.zip", source_changes={"lms35.txt": native(2002)})
    with pytest.raises(ValueError, match="registry_audit_record_width"):
        audit_release(bad)
    bad = release_fixture(tmp_path / "bad-set.zip", source_changes={"lms06.txt": b""})
    with pytest.raises(ValueError, match="registry_audit_record_width"):
        audit_release(bad)


def test_independent_unterminated_package_record_requires_opt_in_and_exact_bytes(
    tmp_path: Path,
) -> None:
    raw = native(137, (1, b"28100000001"), (12, b"000042"))
    source = release_fixture(tmp_path / "variant.zip", source_changes={"lms02.txt": raw[:-2]})
    with pytest.raises(ValueError, match="registry_audit_record_terminator"):
        audit_release(source)
    result = audit_release(source, allow_final_record=True)
    assert result["unterminated_members"] == ["lms02.txt"]
    assert result["members"]["lms02.txt"]["physical_records"] == 1
    assert result["members"]["lms02.txt"]["sha256"] == hashlib.sha256(raw[:-2]).hexdigest()
    assert result["key_coverage"]["package_missing_price"] == 0
    assert result["key_coverage"]["package_missing_drug"] == 0


@pytest.mark.parametrize("suffix", [b"\n", b"\r", b"\r\npartial"])
def test_variant_auditor_rejects_stray_line_breaks_and_partial_record(
    tmp_path: Path, suffix: bytes
) -> None:
    raw = native(137, (1, b"28100000001"), (12, b"000042"))[:-2]
    source = release_fixture(tmp_path / "bad.zip", source_changes={"lms02.txt": raw + suffix})
    with pytest.raises(ValueError, match="registry_audit_record_"):
        audit_release(source, allow_final_record=True)


def test_variant_auditor_still_requires_system_record_count(tmp_path: Path) -> None:
    source = release_fixture(
        tmp_path / "bad-count.zip",
        source_changes={"lms02.txt": native(137, (1, b"28100000001"), (12, b"000042"))[:-2]},
    )
    with ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    control = files["system.txt"].split(b"\r\n")
    control = [
        row[:46] + b"00002" if row[32:44].strip() == b"lms02.txt" else row for row in control
    ]
    files["system.txt"] = b"\r\n".join(control)
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="registry_audit_system_catalogue"):
        audit_release(source, allow_final_record=True)
    from bef_redbird.foundation.lms_registry import (
        MISSING_FINAL_CRLF_PROFILE,
        RegistryError,
        read_release,
    )

    with pytest.raises(RegistryError, match="system_record_count"):
        read_release(source, "final", parser_version=MISSING_FINAL_CRLF_PROFILE)
