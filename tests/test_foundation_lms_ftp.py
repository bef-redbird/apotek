"""Independent invented fixed-byte recipes; never contain private source rows."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from bef_redbird.foundation import ArchiveError, Store
from bef_redbird.foundation.adapters.base import AdapterError
from bef_redbird.foundation.adapters.lms_ftp import parse_lms_ftp

CONTEXT = {
    "schema_vintage": "lms-20260626-final",
    "current_period": "2026-10-12",
    "release_date": "2026-10-13",
    "period_end_exclusive": "2026-10-26",
    "tax_basis": "unknown",
    "fee_basis": "unknown",
}
COVERAGE = {
    "version": 1,
    "kind": "partial_extract",
    "family": "lms-ftp",
    "population": "lms02-package-set",
    "fields": ["aip", "registered", "consumer", "reimbursement_basis", "abc_code", "group_code"],
}


def populated(width: int, fields: dict[tuple[int, int], bytes]) -> bytes:
    row = bytearray(b" " * width)
    for (first, last), value in fields.items():
        assert len(value) == last - first + 1
        row[first - 1 : last] = value
    return bytes(row)


def members() -> dict[str, bytes]:
    # Independently specified from the public June2026 field contract. All33
    # members exist; only invented data needed by this interpretation is populated.
    result = {
        f"lms{n:02}.txt": b""
        for n in [
            1,
            2,
            3,
            4,
            5,
            7,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            17,
            18,
            19,
            20,
            21,
            22,
            23,
            24,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            34,
            35,
        ]
    }
    result["lms01.txt"] = populated(180, {(1, 11): b"28101234567"}) + b"\r\n"
    result["lms02.txt"] = (
        populated(
            137,
            {
                (1, 11): b"28101234567",
                (12, 17): b"000007",
                (74, 78): b"BP   ",
                (104, 111): b"20001231",
                (112, 119): b"20261012",
                (120, 127): b"00000000",
            },
        )
        + b"\r\n"
    )
    result["lms03.txt"] = (
        b"000007"
        + b"000012345"
        + b"000013579"
        + b"000018003"
        + b"000000000" * 2
        + b"000015001"
        + b"000000456"
        + b"000000000"
        + b"000000789"
        + b"000000000"
        + b"\r\n"
    )
    # Ambiguous CP850/CP865 bytes in unused historical names must not be decoded.
    result["lms10.txt"] = b" " * 68 + b"\xf3\r\n"
    return result


def delivery_bytes(
    files: dict[str, bytes] | None = None,
    *,
    release: bytes = b"20261013",
    week: bytes = b"202642",
    system_change: tuple[int, bytes] | None = None,
    duplicate: str | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    files = dict(members() if files is None else files)
    control = bytearray(
        populated(
            93,
            {
                (1, 2): b"00",
                (3, 7): b"12.0 ",
                (8, 47): b"LMS-TAKST".ljust(40),
                (48, 55): release,
                (72, 73): b"01",
                (74, 75): b"33",
                (76, 87): b"LMS.ZIP".ljust(12),
                (88, 93): week,
            },
        )
    )
    if system_change:
        offset, value = system_change
        control[offset : offset + len(value)] = value
    records = [bytes(control), b"01" + b"00000" * 9]
    all_names = sorted([*files, "system.txt"])
    for name in all_names:
        count = len(files[name].split(b"\r\n")) - 1 if name != "system.txt" else 35
        records.append(
            b"10"
            + b"Invented control".ljust(30)
            + name.encode().ljust(12)
            + b"01"
            + f"{count:05}".encode()
        )
    files["system.txt"] = b"\r\n".join(records) + b"\r\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        if duplicate:
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr(duplicate, files[duplicate])
    return stream.getvalue()


def test_direct_prices_identity_dates_leading_zero_and_unused_values() -> None:
    parsed = parse_lms_ftp(delivery_bytes(), CONTEXT)
    assert parsed.records == 1 and parsed.issues == []
    assert [(a.values["price_type"], a.values["amount_ore"]) for a in parsed.assertions] == [
        ("aip", 12345),
        ("registered", 13579),
        ("consumer", 18003),
        ("reimbursement_basis", 15001),
    ]
    for item in parsed.assertions:
        assert item.source_identifier == "000007"
        assert item.effective_date == date(2026, 10, 13)
        assert item.effective_period == item.report_period == date(2026, 10, 12)
        assert item.values["tax_basis"] == (
            "vat_included" if item.values["price_type"] == "consumer" else "unknown"
        )
        assert item.presence == {"amount_ore": "present"}
        raw = item.raw["package_identity"]
        assert raw["drug_id"]["text"] == "28101234567"
        assert raw["dispensing_code"]["text"] == "BP   "
        assert raw["created_date"]["text"] == "20001231"
        assert raw["latest_price_change_date"]["text"] == "20261012"
        assert raw["exit_date"]["text"] == "00000000"
        assert (
            bytes.fromhex(item.raw["records"]["lms02.txt"]["raw_hex"])
            == members()["lms02.txt"][:-2]
        )
    aip = parsed.assertions[0]
    assert aip.source_locator == "member:lms03.txt!record:1!field:02[7:15]"
    assert aip.raw["price"] == {
        "locator": aip.source_locator,
        "raw_hex": b"000012345".hex(),
        "text": "000012345",
    }
    assert parsed.assertions[2].values["fee_basis"] == "includes_prescription_and_preparation_fees"
    assert parsed.assertions[3].values["fee_basis"] == "reimbursement_basis"


@pytest.mark.parametrize("token,expected", [(b"000000000", 0), (b"999999999", 999999999)])
def test_exact_integer_ore_and_real_zero(token: bytes, expected: int) -> None:
    files = members()
    files["lms03.txt"] = files["lms03.txt"][:6] + token + files["lms03.txt"][15:]
    item = parse_lms_ftp(delivery_bytes(files), CONTEXT).assertions[0]
    assert item.values["amount_ore"] == expected and item.quality_state == "valid"


@pytest.mark.parametrize("token", [b"         ", b"000001.00", b"-00000100", b"0000x0000"])
def test_bad_price_is_scoped_with_raw_evidence(token: bytes) -> None:
    files = members()
    files["lms03.txt"] = files["lms03.txt"][:6] + token + files["lms03.txt"][15:]
    parsed = parse_lms_ftp(delivery_bytes(files), CONTEXT)
    assert len(parsed.assertions) == 4 and len(parsed.issues) == 1
    assert parsed.issues[0].blocking_scope == "field"
    assert parsed.issues[0].identifier == "000007"
    assert parsed.assertions[0].values["amount_ore"] is None
    assert parsed.assertions[0].presence == {"amount_ore": "invalid"}
    assert parsed.assertions[0].raw["price"]["raw_hex"] == token.hex()
    assert all(a.quality_state == "valid" for a in parsed.assertions[1:])


@pytest.mark.parametrize("name", ["lms01.txt", "lms02.txt", "lms03.txt", "lms35.txt"])
def test_missing_or_duplicate_member_fails(name: str) -> None:
    files = members()
    del files[name]
    with pytest.raises(AdapterError, match="lms_zip_member_set"):
        parse_lms_ftp(delivery_bytes(files), CONTEXT)
    with pytest.raises(AdapterError, match="lms_duplicate_zip_member"):
        parse_lms_ftp(delivery_bytes(duplicate=name), CONTEXT)


@pytest.mark.parametrize("name", ["../lms01.txt", "/lms01.txt", "LMS01.TXT", "lms33.txt"])
def test_alias_extra_and_preliminary_members_fail(name: str) -> None:
    files = members()
    files[name] = b""
    with pytest.raises(AdapterError, match="lms_zip_member_set"):
        parse_lms_ftp(delivery_bytes(files), CONTEXT)


@pytest.mark.parametrize("name", ["lms01.txt", "lms02.txt", "lms03.txt", "lms35.txt"])
def test_exact_width_controls_even_uninterpreted_member(name: str) -> None:
    files = members()
    files[name] = b"bad-width\r\n"
    with pytest.raises(AdapterError, match="lms_record_width"):
        parse_lms_ftp(delivery_bytes(files), CONTEXT)


@pytest.mark.parametrize("ending", [b"\n", b"\r", b"", b"\r\n\r\n"])
def test_crlf_is_required(ending: bytes) -> None:
    files = members()
    files["lms03.txt"] = files["lms03.txt"][:-2] + ending
    with pytest.raises(AdapterError, match="lms_record_(terminator|width)"):
        parse_lms_ftp(delivery_bytes(files), CONTEXT)


@pytest.mark.parametrize(
    "offset,value,code",
    [
        (2, b"13.0 ", "unsupported_lms_system_schema"),
        (7, b"BAD", "unsupported_lms_system_schema"),
        (55, b"20261026", "unsupported_lms_system_schema"),
        (73, b"32", "lms_system_member_count"),
        (75, b"BAD.ZIP", "unsupported_lms_system_schema"),
        (87, b"202643", "lms_tariff_week_conflict"),
    ],
)
def test_system_schema_and_context_controls(offset: int, value: bytes, code: str) -> None:
    with pytest.raises(AdapterError, match=code):
        parse_lms_ftp(delivery_bytes(system_change=(offset, value)), CONTEXT)


def test_system_catalogue_counts_names_and_own_count() -> None:
    def tampered(which: str) -> bytes:
        original = delivery_bytes()
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        rows = files["system.txt"].split(b"\r\n")
        if which == "count":
            rows[2] = rows[2][:46] + b"00002"
        elif which == "own-count":
            rows[-2] = rows[-2][:46] + b"00034"
        else:
            rows[2] = rows[2][:32] + b"UNKNOWN.TXT " + rows[2][44:]
        files["system.txt"] = b"\r\n".join(rows)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return out.getvalue()

    for which in ["count", "own-count", "name"]:
        with pytest.raises(AdapterError, match="lms_system_(record_count|catalogue)"):
            parse_lms_ftp(tampered(which), CONTEXT)


@pytest.mark.parametrize("name", ["lms01.txt", "lms02.txt", "lms03.txt"])
def test_global_key_ambiguity_is_fatal(name: str) -> None:
    files = members()
    files[name] *= 2
    with pytest.raises(AdapterError, match="lms_duplicate_key"):
        parse_lms_ftp(delivery_bytes(files), CONTEXT)


def test_drug_and_package_joins_are_checked_before_publication() -> None:
    for name, offset, code in [
        ("lms02.txt", 0, "lms_package_drug_join"),
        ("lms03.txt", 0, "lms_package_price_key_set"),
    ]:
        files = members()
        files[name] = files[name][:offset] + b"9" + files[name][offset + 1 :]
        with pytest.raises(AdapterError, match=code):
            parse_lms_ftp(delivery_bytes(files), CONTEXT)


@pytest.mark.parametrize("number", [5, 6, 9, 11])
def test_reserved_fields_cannot_become_values(number: int) -> None:
    files = members()
    offset = 6 + (number - 2) * 9
    files["lms03.txt"] = (
        files["lms03.txt"][:offset] + b"000000001" + files["lms03.txt"][offset + 9 :]
    )
    with pytest.raises(AdapterError, match="lms_reserved_price_field"):
        parse_lms_ftp(delivery_bytes(files), CONTEXT)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"schema_vintage": "12.0"}, "unsupported_lms_context"),
        ({"release_date": "2026-10-12"}, "lms_release_date_conflict"),
        ({"release_date": "2026-10-26"}, "lms_release_outside_tariff_period"),
        ({"current_period": "2026-10-14"}, "lms_release_outside_tariff_period"),
        ({"tax_basis": "excluded"}, "unsupported_lms_context"),
        ({"fee_basis": "excluded"}, "unsupported_lms_context"),
        ({"phase": "final"}, "unsupported_lms_context"),
        ({"release_date": "20261013"}, "invalid_lms_period_context"),
        (
            {
                "current_period": "2016-10-10",
                "release_date": "2016-10-10",
                "period_end_exclusive": "2016-10-24",
            },
            "unsupported_lms_schema_vintage",
        ),
    ],
)
def test_context_is_explicit_bounded_and_does_not_guess(change: dict[str, Any], code: str) -> None:
    with pytest.raises(AdapterError, match=code):
        parse_lms_ftp(delivery_bytes(), CONTEXT | change)


def test_encryption_compression_and_uncompressed_resource_limits(monkeypatch: Any) -> None:
    with pytest.raises(AdapterError, match="lms_zip_compression"):
        parse_lms_ftp(delivery_bytes(compression=zipfile.ZIP_BZIP2), CONTEXT)
    encrypted = bytearray(delivery_bytes())
    central = encrypted.index(b"PK\x01\x02")
    flags = struct.unpack_from("<H", encrypted, central + 8)[0]
    struct.pack_into("<H", encrypted, central + 8, flags | 1)
    with pytest.raises(AdapterError, match="lms_encrypted_zip_member"):
        parse_lms_ftp(bytes(encrypted), CONTEXT)
    monkeypatch.setattr("bef_redbird.foundation.adapters.lms_ftp.MAX_UNCOMPRESSED", 5)
    with pytest.raises(AdapterError, match="source_resource_limit"):
        parse_lms_ftp(delivery_bytes(), CONTEXT)


def test_zip_symlinks_and_nul_names_cannot_alias_regular_members() -> None:
    with zipfile.ZipFile(io.BytesIO(delivery_bytes())) as original:
        files = {info.filename: original.read(info) for info in original.infolist()}
    for special in ("symlink", "nul"):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for name, data in files.items():
                info = zipfile.ZipInfo(name)
                if name == "lms01.txt" and special == "symlink":
                    info.create_system = 3
                    info.external_attr = 0o120777 << 16
                archive.writestr(info, data)
        data = out.getvalue()
        if special == "nul":
            # ZIP name lengths remain the same; NUL truncation must not turn an
            # unsafe native name into the accepted logical spelling.
            data = data.replace(b"lms01.txt", b"lms01.tx\x00")
        with pytest.raises(AdapterError, match="lms_(unsafe_zip_member|zip_member_set)"):
            parse_lms_ftp(data, CONTEXT)


def ingest(store: Store, delivery: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return store.ingest(
        delivery,
        adapter="lms-ftp",
        version="lms-ftp-zip-v1",
        mapping_version="invented-map-v1",
        mapping_policy="delivery",
        context={
            key: value
            for key, value in (CONTEXT if context is None else context).items()
            if key != "current_period"
        },
    )


def register(store: Store, path: Path, **changes: Any) -> str:
    store.add_period(date(2026, 10, 12), date(2026, 10, 26))
    return store.register_source(
        path,
        **{
            "source": "invented-lms",
            "receipt_key": "one",
            "phase": "final",
            "edition": "invented-release",
            "current_period": date(2026, 10, 12),
            "coverage": COVERAGE,
            **changes,
        },
    )


def test_regular_publication_retry_and_delivery_scoped_identity(tmp_path: Path) -> None:
    original = tmp_path / "misleading-19990101.zip"
    data = delivery_bytes()
    original.write_bytes(data)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, original)
        result = ingest(store, delivery)
        assert result["validation_status"] == "accepted" and result["accepted_count"] == 4
        assert result["observations"]["price_observation"] == 4
        observations = store.observations(result["import_id"], "price_observation")
        assert ingest(store, delivery) == result
        assert store.observations(result["import_id"], "price_observation") == observations
        assert {row["effective_date"] for row in observations} == {date(2026, 10, 13)}
        assert len({row["package_id"] for row in observations}) == 1
        other = register(store, original, receipt_key="independent-receipt")
        other_result = ingest(store, other)
        other_prices = store.observations(other_result["import_id"], "price_observation")
        assert observations[0]["package_id"] != other_prices[0]["package_id"]
        assert original.read_bytes() == data


@pytest.mark.parametrize(
    "change,context,code",
    [
        ({"phase": "preliminary"}, {}, "unsupported_lms_release_phase"),
        ({"phase": None}, {}, "unsupported_lms_release_phase"),
        ({"edition": None}, {}, "invalid_lms_release_binding"),
        ({"coverage": {"version": 1, "kind": "unknown"}}, {}, "lms_explicit_coverage_required"),
        ({"coverage": COVERAGE | {"fields": ["aip"]}}, {}, "lms_explicit_coverage_required"),
        (
            {"coverage": COVERAGE | {"population": "whole-market"}},
            {},
            "lms_explicit_coverage_required",
        ),
        ({}, {"period_end_exclusive": "2026-10-27"}, "lms_calendar_interval_conflict"),
    ],
)
def test_trusted_phase_coverage_and_calendar_gate(
    tmp_path: Path,
    change: dict[str, Any],
    context: dict[str, Any],
    code: str,
) -> None:
    source = tmp_path / "invented.zip"
    source.write_bytes(delivery_bytes())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source, **change)
        result = ingest(store, delivery, CONTEXT | context)
        assert result["validation_status"] == "rejected"
        assert result["observations"]["price_observation"] == 0
        assert result["issues"][0]["code"] == code
        assert ingest(store, delivery, CONTEXT | context) == result


def basis_profile(delivery: str, sha256: str) -> dict[str, Any]:
    return {
        "version": 1,
        "price_type": "aip",
        "price_role": "effective_price",
        "currency": "DKK",
        "price_basis": "per_package",
        "tax_basis": "excluded",
        "fee_basis": "excluded",
        "effective_period_start_from": "2026-10-12",
        "effective_period_start_through": "2026-10-12",
        "evidence_delivery_id": delivery,
        "evidence_sha256": sha256,
        "evidence_locator": "section:invented-definition",
    }


def test_optional_basis_changes_only_aip_and_pins_actual_evidence(tmp_path: Path) -> None:
    source = tmp_path / "invented.zip"
    source.write_bytes(delivery_bytes())
    evidence = tmp_path / "invented-definition.txt"
    evidence.write_bytes(b"Invented test evidence; not an actual source definition.")
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        evidence_id = store.register_source(evidence, source="invented", receipt_key="definition")
        delivery = register(store, source)
        profile = basis_profile(evidence_id, hashlib.sha256(evidence.read_bytes()).hexdigest())
        declared = CONTEXT | {"price_basis_override": profile}
        result = ingest(store, delivery, declared)
        assert result["validation_status"] == "accepted"
        rows = store.observations(result["import_id"], "price_observation")
        assert [(row["price_type"], row["tax_basis"]) for row in rows] == [
            ("aip", "excluded"),
            ("registered", "unknown"),
            ("consumer", "vat_included"),
            ("reimbursement_basis", "unknown"),
        ]
        assert json.loads(rows[0]["source_values"])["price"]["text"] == "000012345"
        assert ingest(store, delivery, declared) == result
        bad = CONTEXT | {"price_basis_override": profile | {"evidence_sha256": "0" * 64}}
        rejected = ingest(store, delivery, bad)
        assert rejected["validation_status"] == "rejected"
        assert rejected["issues"][0]["code"] == "lms_basis_evidence_hash_conflict"
        store.original_path(evidence_id).write_bytes(b"corrupt")
        with pytest.raises(ArchiveError, match="Corrupt archive object"):
            ingest(store, delivery, declared)


def test_ordinary_classification_is_direct_current_scoped_evidence(tmp_path: Path) -> None:
    files = members()
    files["lms04.txt"] = (
        populated(47, {(1, 4): b"0012", (5, 10): b"000007", (35, 35): b"B", (36, 41): b"000007"})
        + b"\r\n"
    )
    data = delivery_bytes(files)
    parsed = parse_lms_ftp(data, CONTEXT)
    assert parsed.records == 2 and len(parsed.assertions) == 5
    item = parsed.assertions[-1]
    assert item.table == "classification_observation"
    assert item.source_locator == "member:lms04.txt!record:1!field:06[35:35]"
    assert item.values == {
        "classification_context": "ordinary",
        "abc_code": "B",
        "group_namespace": "lms:package-substitution",
        "group_code": "0012",
        "group_scope": "period:2026-10-12/release:2026-10-13",
    }
    assert item.presence == {"abc_code": "present", "group_code": "present"}
    assert item.raw["group"]["locator"] == "member:lms04.txt!record:1!field:01[1:4]"
    assert item.raw["group"]["raw_hex"] == b"0012".hex()
    assert item.effective_date == date(2026, 10, 13)
    source = tmp_path / "invented.zip"
    source.write_bytes(data)
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source)
        result = ingest(store, delivery)
        assert result["accepted_count"] == 5
        classes = store.observations(result["import_id"], "classification_observation")
        assert len(classes) == 1 and classes[0]["group_code"] == "0012"
        assert ingest(store, delivery) == result


@pytest.mark.parametrize(
    "abc,group,issue",
    [
        (b"?", b"0012", "unknown_lms_abc_code"),
        (b" ", b"0012", "unknown_lms_abc_code"),
        (b"A", b"    ", "invalid_lms_group_code"),
        (b"A", b"00x2", "invalid_lms_group_code"),
    ],
)
def test_unknown_classification_is_a_scoped_issue(abc: bytes, group: bytes, issue: str) -> None:
    files = members()
    files["lms04.txt"] = populated(47, {(1, 4): group, (5, 10): b"000007", (35, 35): abc}) + b"\r\n"
    parsed = parse_lms_ftp(delivery_bytes(files), CONTEXT)
    assert len(parsed.issues) == 1 and parsed.issues[0].code == issue
    assert parsed.issues[0].blocking_scope == "field"
    assert parsed.assertions[-1].quality_state == "invalid"
    assert all(item.quality_state == "valid" for item in parsed.assertions[:-1])


def test_duplicate_or_orphan_classification_is_fatal() -> None:
    files = members()
    row = populated(47, {(1, 4): b"0012", (5, 10): b"000007", (35, 35): b"A"}) + b"\r\n"
    files["lms04.txt"] = row * 2
    with pytest.raises(AdapterError, match="lms_duplicate_key"):
        parse_lms_ftp(delivery_bytes(files), CONTEXT)
    files["lms04.txt"] = row[:4] + b"999999" + row[10:]
    with pytest.raises(AdapterError, match="lms_classification_package_join"):
        parse_lms_ftp(delivery_bytes(files), CONTEXT)


@pytest.mark.parametrize(
    "released,profile,accepted",
    [
        ("2025-01-05", "lms-final-12-observed-20250106", False),
        ("2025-01-06", "lms-final-12-observed-20250106", True),
        ("2026-06-25", "lms-final-12-observed-20250106", True),
        ("2026-06-26", "lms-final-12-observed-20250106", False),
        ("2026-06-25", "lms-20260626-final", False),
        ("2026-06-26", "lms-20260626-final", True),
    ],
)
def test_explicit_observed_layout_profile_has_hard_date_bounds(
    released: str,
    profile: str,
    accepted: bool,
) -> None:
    when = date.fromisoformat(released)
    iso = when.isocalendar()
    data = delivery_bytes(
        release=released.replace("-", "").encode(),
        week=f"{iso.year:04}{iso.week:02}".encode(),
    )
    context = CONTEXT | {
        "release_date": released,
        "current_period": released,
        "period_end_exclusive": "2026-07-01",
        "schema_vintage": profile,
    }
    if accepted:
        parsed = parse_lms_ftp(data, context)
        assert parsed.assertions[0].raw["schema_vintage"] == profile
        assert parsed.assertions[0].values["tax_basis"] == "unknown"
    else:
        with pytest.raises(AdapterError, match="unsupported_lms_schema_vintage"):
            parse_lms_ftp(data, context)


def test_complete_snapshot_declares_only_source_population(tmp_path: Path) -> None:
    source = tmp_path / "invented.zip"
    source.write_bytes(delivery_bytes())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, source, coverage=COVERAGE | {"kind": "complete_snapshot"})
        result = ingest(store, delivery)
        assert result["validation_status"] == "accepted"
        assert result["coverage_assessment"] == "declaration_only"


def test_missing_price_control_cannot_pass_explicit_partial_coverage(tmp_path: Path) -> None:
    files = members()
    files["lms03.txt"] = b""
    source = tmp_path / "invented.zip"
    source.write_bytes(delivery_bytes(files))
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        result = ingest(store, register(store, source))
        assert result["validation_status"] == "rejected"
        assert result["issues"][0]["code"] == "lms_package_price_key_set"


def test_basis_scope_and_profile_validation() -> None:
    profile = basis_profile("00000000-0000-0000-0000-000000000001", "a" * 64)
    parsed = parse_lms_ftp(
        delivery_bytes(),
        CONTEXT
        | {
            "price_basis_override": profile
            | {
                "effective_period_start_from": "2026-10-13",
                "effective_period_start_through": "2026-10-26",
            }
        },
    )
    assert parsed.assertions[0].values["tax_basis"] == "unknown"
    with pytest.raises(AdapterError, match="invalid_price_basis_override"):
        parse_lms_ftp(
            delivery_bytes(),
            CONTEXT
            | {
                "price_basis_override": profile
                | {
                    "evidence_locator": "",
                }
            },
        )
