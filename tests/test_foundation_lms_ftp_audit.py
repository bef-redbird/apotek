"""Independently invented native releases and adversarial persisted evidence."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import date
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import duckdb
import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation.lms_ftp_audit import expected_lms_ftp, validate_delivery
from bef_redbird.foundation.source_audit import auditor_fingerprint, reconcile, snapshot_imports

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


def row(width: int, cells: list[tuple[int, bytes]]) -> bytes:
    result = bytearray(b" " * width)
    for first, value in cells:
        result[first - 1 : first - 1 + len(value)] = value
    return bytes(result) + b"\r\n"


def native_members() -> dict[str, bytes]:
    # This recipe is authored from published ranges, independently of adapter
    # constants, production normalization, other tests and all private source rows.
    widths = [
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
    ]
    numbers = [1, 2, 3, 4, 5, 7, *range(9, 33), 34, 35]
    result = {f"lms{n:02d}.txt": row(width, []) for n, width in zip(numbers, widths, strict=True)}
    result["lms01.txt"] = row(180, [(1, b"28101111111"), (30, b"Invented medicine")])
    result["lms02.txt"] = b"".join(
        row(
            137,
            [
                (1, b"28101111111"),
                (12, identifier),
                (74, b"A    "),
                (104, b"20200101"),
                (112, b"20261012"),
            ],
        )
        for identifier in (b"000042", b"654321")
    )
    result["lms03.txt"] = b"".join(
        identifier + b"".join(f"{amount:09d}".encode("ascii") for amount in amounts) + b"\r\n"
        for identifier, amounts in [
            (b"000042", [0, 12301, 21234, 0, 0, 34567, 456, 0, 789, 0]),
            (b"654321", [999999999, 1, 2, 0, 0, 3, 0, 0, 0, 0]),
        ]
    )
    result["lms04.txt"] = row(47, [(1, b"0042"), (5, b"000042"), (35, b"B")])
    # LMS10 byteF3 differs under CP850/CP865; no assertion decodes it.
    result["lms10.txt"] = row(69, [(20, b"\xf3")])
    result["lms34.txt"] = b""  # Empty is explicit and independently counted.
    return result


def release(
    members: dict[str, bytes] | None = None,
    *,
    effective: bytes = b"20261013",
    week: bytes = b"202642",
    control_change: tuple[int, bytes] | None = None,
) -> bytes:
    members = dict(native_members() if members is None else members)
    control = row(
        93,
        [
            (1, b"00"),
            (3, b"12.0 "),
            (8, b"LMS-TAKST"),
            (48, effective),
            (72, b"01"),
            (74, b"33"),
            (76, b"LMS.ZIP"),
            (88, week),
        ],
    )
    if control_change:
        offset, token = control_change
        control = control[:offset] + token + control[offset + len(token) :]
    lines = [control, b"01" + b"00000" * 9 + b"\r\n"]
    for member in [*members, "system.txt"]:
        count = members[member].count(b"\n") if member != "system.txt" else 35
        lines.append(
            row(
                51,
                [
                    (1, b"10"),
                    (3, b"Invented catalogue"),
                    (33, member.upper().encode("ascii")),
                    (45, b"01"),
                    (47, f"{count:05d}".encode("ascii")),
                ],
            )
        )
    members["system.txt"] = b"".join(lines)
    return pack(members)


def pack(members: dict[str, bytes]) -> bytes:
    result = io.BytesIO()
    with ZipFile(result, "w", compression=ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return result.getvalue()


def decoded(data: bytes, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return expected_lms_ftp(data, "lms-ftp", CONTEXT if context is None else context)


def test_native_expected_zero_identity_daily_prices_classification_and_all_members() -> None:
    result = decoded(release())
    prices = result["assertions"][:8]
    assert result["source_rows"] == 3 and result["issues"] == []
    assert [p["values"]["amount_ore"] for p in prices] == [
        0,
        12301,
        21234,
        34567,
        999999999,
        1,
        2,
        3,
    ]
    assert [p["values"]["price_type"] for p in prices[:4]] == [
        "aip",
        "registered",
        "consumer",
        "reimbursement_basis",
    ]
    assert {p["effective_date"] for p in prices} == {"2026-10-13"}
    assert {p["effective_period"] for p in prices} == {"2026-10-12"}
    assert {p["report_period"] for p in prices} == {"2026-10-12"}
    assert prices[0]["canonical_identifier"] == prices[0]["source_identifier"] == "000042"
    assert prices[0]["source_locator"] == "member:lms03.txt!record:1!field:02[7:15]"
    assert prices[0]["raw"]["price"] == {
        "locator": prices[0]["source_locator"],
        "raw_hex": b"000000000".hex(),
        "text": "000000000",
    }
    assert prices[0]["raw"]["package_identity"]["drug_id"]["text"] == "28101111111"
    assert prices[0]["raw"]["package_identity"]["exit_date"]["text"] == " " * 8
    assert (
        prices[0]["raw"]["records"]["lms01.txt"]["raw_hex"]
        == native_members()["lms01.txt"][:-2].hex()
    )
    assert [p["values"]["tax_basis"] for p in prices[:4]] == [
        "unknown",
        "unknown",
        "vat_included",
        "unknown",
    ]
    assert [p["values"]["fee_basis"] for p in prices[:4]] == [
        "unknown",
        "unknown",
        "includes_prescription_and_preparation_fees",
        "reimbursement_basis",
    ]
    classification = result["assertions"][-1]
    assert classification["values"] == {
        "classification_context": "ordinary",
        "abc_code": "B",
        "group_namespace": "lms:package-substitution",
        "group_code": "0042",
        "group_scope": "period:2026-10-12/release:2026-10-13",
    }
    accounting = result["physical_accounting"]
    assert accounting["member_count"] == 33
    assert accounting["physical_records"] == 68
    assert accounting["members"]["system.txt"]["physical_records"] == 35
    assert accounting["members"]["lms34.txt"]["physical_records"] == 0
    assert accounting["price_assertions"] == 8 and accounting["classification_assertions"] == 1
    assert accounting["unsupported_price_fields_retained_raw"] == [8, 10]


@pytest.mark.parametrize("member", list(native_members()))
def test_every_member_width_is_independently_checked(member: str) -> None:
    members = native_members()
    members[member] = b"short\r\n"
    with pytest.raises(ValueError, match="independent_lms_record_width"):
        decoded(release(members))


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "count", "system_count", "lf", "key", "join", "reserved"]
)
def test_source_control_failures_cannot_reconcile(mutation: str) -> None:
    members = native_members()
    if mutation == "missing":
        members.pop("lms34.txt")
    elif mutation == "extra":
        members["unknown.txt"] = b""
    elif mutation == "lf":
        members["lms01.txt"] = members["lms01.txt"].replace(b"\r\n", b"\n")
    elif mutation == "key":
        members["lms03.txt"] += members["lms03.txt"][:98]
    elif mutation == "join":
        members["lms02.txt"] = b"00000000000" + members["lms02.txt"][11:]
    elif mutation == "reserved":
        members["lms03.txt"] = members["lms03.txt"][:33] + b"000000001" + members["lms03.txt"][42:]
    data = release(members)
    if mutation in {"count", "system_count"}:
        with ZipFile(io.BytesIO(data)) as archive:
            packed = {name: archive.read(name) for name in archive.namelist()}
        system = packed["system.txt"]
        if mutation == "count":
            system = system[: 95 + 49 + 46] + b"99999" + system[95 + 49 + 51 :]
        else:
            system = system[:-7] + b"00034\r\n"
        packed["system.txt"] = system
        data = pack(packed)
    with pytest.raises(ValueError, match="independent_lms_"):
        decoded(data)


@pytest.mark.parametrize(
    "change",
    [
        {"release_date": "2026-10-14"},
        {"release_date": "2026-10-26"},
        {"period_end_exclusive": "2026-10-13"},
        {"current_period": "2026-09-28"},
        {"tax_basis": "excluded"},
        {"fee_basis": "excluded"},
        {"schema_vintage": "guess"},
    ],
)
def test_context_daily_date_and_tariff_week_conflicts_fail(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="independent_lms_"):
        decoded(release(), {**CONTEXT, **change})


def test_legacy_is_bounded_by_explicit_vintage_and_date() -> None:
    old = {
        **CONTEXT,
        "schema_vintage": "lms-final-12-observed-20250106",
        "current_period": "2025-01-06",
        "release_date": "2025-01-06",
        "period_end_exclusive": "2025-01-20",
    }
    data = release(effective=b"20250106", week=b"202502")
    assert decoded(data, old)["assertions"][0]["effective_period"] == "2025-01-06"
    with pytest.raises(ValueError, match="independent_lms_vintage_or_period"):
        decoded(data, {**old, "schema_vintage": "lms-20260626-final"})
    with pytest.raises(ValueError, match="independent_lms_vintage_or_period"):
        decoded(data, {**old, "release_date": "2024-12-31", "current_period": "2024-12-23"})


def test_invalid_money_and_classification_have_exact_scoped_issues() -> None:
    members = native_members()
    members["lms03.txt"] = members["lms03.txt"][:6] + b"         " + members["lms03.txt"][15:]
    members["lms04.txt"] = row(47, [(1, b"x042"), (5, b"000042"), (35, b"?")])
    result = decoded(release(members))
    assert result["assertions"][0]["values"]["amount_ore"] is None
    assert result["assertions"][0]["presence"] == {"amount_ore": "invalid"}
    assert [p["code"] for p in result["issues"]] == [
        "malformed_lms_money",
        "unknown_lms_abc_code",
        "invalid_lms_group_code",
    ]
    assert all(
        p["identifier"] == "000042" and p["blocking_scope"] == "field" for p in result["issues"]
    )


def setup_store(
    tmp_path: Path,
    *,
    basis: bool = False,
    data: bytes | None = None,
) -> tuple[Path, Path, str, Path]:
    database, archive = tmp_path / "test.duckdb", tmp_path / "archive"
    source = tmp_path / "invented.zip"
    source.write_bytes(release() if data is None else data)
    context: dict[str, Any] = {
        key: value for key, value in CONTEXT.items() if key != "current_period"
    }
    with Store(database, archive) as store:
        store.add_period(date(2026, 10, 12), date(2026, 10, 26))
        if basis:
            evidence = tmp_path / "invented-definition.json"
            evidence.write_text('{"definition":"invented reviewed basis"}', encoding="utf-8")
            evidence_id = store.register_source(
                evidence, source="invented-basis", receipt_key="basis"
            )
            context["price_basis_override"] = {
                "version": 1,
                "price_type": "aip",
                "price_role": "effective_price",
                "currency": "DKK",
                "price_basis": "per_package",
                "tax_basis": "excluded",
                "fee_basis": "excluded",
                "effective_period_start_from": "2026-10-12",
                "effective_period_start_through": "2026-10-12",
                "evidence_delivery_id": evidence_id,
                "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "evidence_locator": "section:invented-basis",
            }
        delivery = store.register_source(
            source,
            source="invented-lms",
            receipt_key="release",
            phase="final",
            edition="invented-daily",
            current_period=date(2026, 10, 12),
            coverage=COVERAGE,
        )
        result = store.ingest(
            delivery,
            adapter="lms-ftp",
            version="lms-ftp-zip-v1",
            mapping_version="invented-v1",
            mapping_policy="delivery",
            context=context,
        )
        assert result["validation_status"] == "accepted"
    return database, archive, result["import_id"], source


def test_complete_independent_reconciliation_and_scoped_snapshot(tmp_path: Path) -> None:
    database, archive, iid, source = setup_store(tmp_path)
    before = database.read_bytes()
    for scope in ({}, {"import_ids": [iid]}):
        report = reconcile(database, archive, [source], **scope)
        assert report["complete"], report["mismatches"]
        assert report["verified_count"] == 9 and report["quarantined_count"] == 0
        assert report["imports"][0]["physical_accounting"]["member_count"] == 33
    assert database.read_bytes() == before
    assert "lms_ftp_audit.py" in auditor_fingerprint()
    assert snapshot_imports(database, [iid])["price_period"]["count"] == 1


@pytest.mark.parametrize(
    "column,value",
    [
        ("amount_ore", 54321),
        ("source_identifier", "42"),
        ("source_locator", "member:lms03.txt!record:99!field:02[7:15]"),
        ("source_values", '{"raw_hex":"tampered"}'),
        ("effective_date", "2026-10-14"),
        ("field_presence", '{"amount_ore":"blank"}'),
        ("tax_basis", "excluded"),
    ],
)
def test_any_stored_money_identity_date_raw_or_provenance_tamper_fails(
    tmp_path: Path,
    column: str,
    value: Any,
) -> None:
    database, archive, iid, source = setup_store(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            f"UPDATE price_observation SET {column}=? "
            "WHERE canonical_identifier='000042' AND price_type='aip'",
            [value],
        )
    result = reconcile(database, archive, [source], import_ids=[iid])
    assert not result["complete"]
    assert result["quarantined_count"] == 1 and result["verified_count"] == 8


def test_native_hash_tamper_fails(tmp_path: Path) -> None:
    database, archive, iid, source = setup_store(tmp_path)
    member = next(p for p in (archive / "sha256").rglob("*") if p.is_file())
    member.write_bytes(member.read_bytes() + b"changed")
    result = reconcile(database, archive, [source], import_ids=[iid])
    assert not result["complete"]
    assert {x["kind"] for x in result["mismatches"]} >= {"archive_hash", "archive_size"}


def test_reviewed_aip_basis_requires_independent_archive_pin(tmp_path: Path) -> None:
    database, archive, iid, source = setup_store(tmp_path, basis=True)
    assert snapshot_imports(database, [iid])["source_delivery"]["count"] == 2
    valid = reconcile(database, archive, [source], import_ids=[iid])
    assert valid["complete"], valid["mismatches"]
    assert (
        valid["imports"][0]["basis_context_evidence"]["source_locator"] == "section:invented-basis"
    )
    with duckdb.connect(str(database)) as connection:
        prices = connection.execute(
            "SELECT price_type,tax_basis,fee_basis FROM price_observation"
        ).fetchall()
        assert all(tax == fee == "excluded" for kind, tax, fee in prices if kind == "aip")
        basis_location = connection.execute(
            "SELECT archive_location FROM source_delivery WHERE source='invented-basis'"
        ).fetchone()
    assert basis_location is not None
    (archive / basis_location[0]).write_bytes(b"changed evidence")
    invalid = reconcile(database, archive, [source], import_ids=[iid])
    assert not invalid["complete"] and not invalid["verified_observation_ids"]
    assert invalid["imports"][0]["reason"] == "independent_price_basis_evidence_archive_mismatch"


def test_independent_reader_does_not_call_production_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bef_redbird.foundation.adapters import lms_ftp, report_basis

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Production reader must not provide expected values")

    monkeypatch.setattr(lms_ftp, "parse_lms_ftp", forbidden)
    monkeypatch.setattr(report_basis, "validated_basis_override", forbidden)
    assert len(decoded(release())["assertions"]) == 9


def test_independently_detects_conflicting_calendar_with_untouched_observations(
    tmp_path: Path,
) -> None:
    database, archive, iid, source = setup_store(tmp_path)
    baseline = snapshot_imports(database, [iid])
    delivery = baseline["source_delivery"]["rows"][0]
    periods = baseline["price_period"]["rows"]
    assert reconcile(database, archive, [source], import_ids=[iid])["complete"]
    periods[0]["period_end_exclusive"] = "2026-10-27"
    with pytest.raises(ValueError, match="independent_lms_calendar_interval"):
        validate_delivery(delivery, CONTEXT, periods, 3)


def test_invalid_native_fields_are_reconciled_with_all_exact_issue_records(tmp_path: Path) -> None:
    members = native_members()
    members["lms03.txt"] = members["lms03.txt"][:6] + b" 00000000" + members["lms03.txt"][15:]
    members["lms04.txt"] = row(47, [(1, b"x042"), (5, b"000042"), (35, b"?")])
    database, archive, iid, source = setup_store(tmp_path, data=release(members))
    result = reconcile(database, archive, [source], import_ids=[iid])
    assert result["complete"], result["mismatches"]
    assert result["verified_count"] == 9 and result["imports"][0]["expected_issues"] == 3


@pytest.mark.parametrize(
    "column,value",
    [
        ("abc_code", "A"),
        ("group_code", "9999"),
        ("group_scope", "period:2026-10-12/release:2026-10-12"),
    ],
)
def test_classification_or_group_tamper_fails(tmp_path: Path, column: str, value: str) -> None:
    database, archive, iid, source = setup_store(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(f"UPDATE classification_observation SET {column}=?", [value])
    result = reconcile(database, archive, [source], import_ids=[iid])
    assert not result["complete"]
    assert result["quarantined_count"] == 1 and result["verified_count"] == 8


def test_ambiguous_asserted_text_cannot_choose_a_codepage() -> None:
    members = native_members()
    original = members["lms02.txt"]
    members["lms02.txt"] = original[:73] + b"\xf3" + original[74:]
    with pytest.raises(ValueError, match="independent_lms_ambiguous_asserted_encoding"):
        decoded(release(members))


@pytest.mark.parametrize(
    "field,value",
    [
        ("price_type", "registered"),
        ("tax_basis", "unknown"),
        ("version", True),
        ("effective_period_start_from", "2026-1-1"),
        ("evidence_sha256", "f" * 63),
        ("evidence_locator", " "),
    ],
)
def test_lms_override_contract_independently_rejected(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    database, _, iid, _ = setup_store(tmp_path, basis=True)
    with duckdb.connect(str(database), read_only=True) as connection:
        record = connection.execute(
            "SELECT interpretation_context FROM import_batch WHERE import_id=?", [iid]
        ).fetchone()
    assert record is not None
    context = json.loads(record[0])["adapter_context"]
    context["price_basis_override"][field] = value
    with pytest.raises(ValueError, match="independent_invalid_price_basis_override"):
        decoded(release(), context)


@pytest.mark.parametrize(
    "change",
    [
        {"phase": "preliminary"},
        {"edition": ""},
        {"coverage": json.dumps({**COVERAGE, "population": "whole-market"})},
        {"coverage": json.dumps({**COVERAGE, "fields": ["aip"]})},
        {"coverage": json.dumps({**COVERAGE, "record_count": 999})},
    ],
)
def test_independent_delivery_admission_requires_phase_edition_coverage(
    tmp_path: Path,
    change: dict[str, Any],
) -> None:
    database, _, iid, _ = setup_store(tmp_path)
    baseline = snapshot_imports(database, [iid])
    delivery = {**baseline["source_delivery"]["rows"][0], **change}
    with pytest.raises(ValueError, match="independent_lms_delivery_"):
        validate_delivery(delivery, CONTEXT, baseline["price_period"]["rows"], 3)
