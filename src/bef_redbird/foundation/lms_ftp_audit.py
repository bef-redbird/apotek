"""Independent original-byte LMS final-release reconciliation.

This reader uses the published fixed-byte field ranges and a separately bounded
observed legacy profile. It imports no adapter, converter or normalized source
rows. ZIP/control integrity does not establish market coverage, source authority,
historical availability, or correction/replacement semantics.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import date
from typing import Any, Never

# Complete final set (LMS33/38 are preliminary-only), specification Nyt rows
# 7–306. Empty members still need to exist and have a matching SYSTEM catalogue.
_LAYOUT = {
    "lms01.txt": 180,
    "lms02.txt": 137,
    "lms03.txt": 96,
    "lms04.txt": 47,
    "lms05.txt": 10,
    "lms07.txt": 11,
    "lms09.txt": 60,
    "lms10.txt": 69,
    "lms11.txt": 63,
    "lms12.txt": 152,
    "lms13.txt": 52,
    "lms14.txt": 65,
    "lms15.txt": 65,
    "lms16.txt": 63,
    "lms17.txt": 555,
    "lms18.txt": 67,
    "lms19.txt": 65,
    "lms20.txt": 61,
    "lms21.txt": 71,
    "lms22.txt": 108,
    "lms23.txt": 24,
    "lms24.txt": 60,
    "lms25.txt": 26,
    "lms26.txt": 164,
    "lms27.txt": 18,
    "lms28.txt": 183,
    "lms29.txt": 46,
    "lms30.txt": 367,
    "lms31.txt": 36,
    "lms32.txt": 30,
    "lms34.txt": 21,
    "lms35.txt": 2003,
}


def _fail(reason: str) -> Never:
    raise ValueError("independent_lms_" + reason)


def _iso(value: Any) -> date:
    if not isinstance(value, str):
        return _fail("context_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return _fail("context_date")
    if parsed.isoformat() != value:
        return _fail("context_date")
    return parsed


def _native_date(value: bytes) -> date:
    if not re.fullmatch(rb"[0-9]{8}", value):
        return _fail("system_date")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return _fail("system_date")


def _integer(value: bytes) -> int:
    if not value or any(c < 48 or c > 57 for c in value):
        return _fail("numeric_control")
    return int(value)


def _read(data: bytes) -> tuple[dict[str, list[bytes]], dict[str, Any]]:
    """Account for every member/record, checking CRCs without extracting files."""
    if len(data) > 128 * 1024 * 1024:
        _fail("resource_limit")
    records: dict[str, list[bytes]] = {}
    accounting: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) != 33 or {x.filename for x in entries} != {*_LAYOUT, "system.txt"}:
                _fail("member_set")
            if sum(x.file_size for x in entries) > 128 * 1024 * 1024:
                _fail("resource_limit")
            for entry in entries:
                name = entry.filename
                file_type = (entry.external_attr >> 16) & 0o170000
                if (
                    entry.flag_bits & 1
                    or entry.compress_type not in (0, 8)
                    or entry.orig_filename != name
                    or file_type not in {0, 0o100000}
                ):
                    _fail("unsupported_zip_member")
                native = archive.read(entry)
                lines = []
                for line in io.BytesIO(native):
                    if not line.endswith(b"\r\n") or b"\r" in line[:-2] or b"\n" in line[:-2]:
                        _fail("record_terminator")
                    row = line[:-2]
                    if name in _LAYOUT and len(row) != _LAYOUT[name]:
                        _fail("record_width")
                    lines.append(row)
                records[name] = lines
                accounting[name] = {
                    "bytes": len(native),
                    "sha256": hashlib.sha256(native).hexdigest(),
                    "physical_records": len(lines),
                    "record_width": _LAYOUT.get(name),
                    "terminator": "CRLF",
                }
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as exc:
        raise ValueError("independent_lms_invalid_zip") from exc
    return records, accounting


def _controls(records: dict[str, list[bytes]], start: date, released: date) -> None:
    """SYSTEM fields from specification Nyt rows309–341, including self-count."""
    system = records["system.txt"]
    if len(system) != 35 or [len(x) for x in system] != [93, 47] + [51] * 33:
        _fail("system_layout")
    control, parameter, *catalogue = system
    if (
        control[:7] != b"0012.0 "
        or control[7:47] != b"LMS-TAKST".ljust(40)
        or control[55:71] != b" " * 16
        or control[71:75] != b"0133"
        or control[75:87] != b"LMS.ZIP".ljust(12)
        or parameter[:2] != b"01"
    ):
        _fail("system_contract")
    if _native_date(control[47:55]) != released:
        _fail("release_date_conflict")
    iso = start.isocalendar()
    if control[87:93] != f"{iso.year:04d}{iso.week:02d}".encode("ascii"):
        _fail("tariff_week_conflict")
    _integer(parameter[2:])
    seen = set()
    for entry in catalogue:
        try:
            name = entry[32:44].rstrip(b" ").decode("ascii").lower()
        except UnicodeDecodeError:
            _fail("catalogue_name")
        if entry[:2] != b"10" or entry[44:46] != b"01" or name not in records or name in seen:
            _fail("catalogue_membership")
        seen.add(name)
        if _integer(entry[46:51]) != len(records[name]):
            _fail("catalogue_record_count")
    if seen != set(records):
        _fail("catalogue_membership")


def _keyed(
    records: dict[str, list[bytes]], member: str, left: int, right: int
) -> dict[str, tuple[int, bytes]]:
    result = {}
    for number, row in enumerate(records[member], 1):
        token = row[left - 1 : right]
        _integer(token)
        identifier = token.decode("ascii")
        if identifier in result:
            _fail("duplicate_key")
        result[identifier] = number, row
    return result


def _fragment(
    member: str, record: int, field: int, left: int, right: int, row: bytes
) -> dict[str, Any]:
    token = row[left - 1 : right]
    text = token.decode("cp865")
    if token.decode("cp850") != text:
        _fail("ambiguous_asserted_encoding")
    return {
        "locator": f"member:{member}!record:{record}!field:{field:02d}[{left}:{right}]",
        "raw_hex": token.hex(),
        "text": text,
    }


def validate_delivery(
    delivery: dict[str, Any],
    context: dict[str, Any],
    periods: list[dict[str, Any]],
    source_rows: int,
) -> None:
    """Independently check trusted phase, population and calendar admission."""
    if (
        delivery["phase"] != "final"
        or not delivery["edition"]
        or delivery["target_period"] is not None
    ):
        _fail("delivery_phase_or_edition")
    intervals = [p for p in periods if p["period_start"] == context["current_period"]]
    if (
        len(intervals) != 1
        or intervals[0]["period_end_exclusive"] != context["period_end_exclusive"]
    ):
        _fail("calendar_interval")
    coverage = json.loads(delivery["coverage"])
    fields = coverage.get("fields")
    expected = {"aip", "registered", "consumer", "reimbursement_basis", "abc_code", "group_code"}
    if (
        coverage.get("kind") not in {"partial_extract", "complete_snapshot"}
        or coverage.get("family") != "lms-ftp"
        or coverage.get("population") != "lms02-package-set"
        or not isinstance(fields, list)
        or any(not isinstance(f, str) for f in fields)
        or len(fields) != len(expected)
        or set(fields) != expected
        or coverage.get("filter")
        or ("record_count" in coverage and coverage["record_count"] != source_rows)
    ):
        _fail("delivery_coverage")


def expected_lms_ftp(data: bytes, adapter: str, context: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct direct assertions independently; never derive from stored rows."""
    from .source_audit import _basis_override

    permitted = {
        "schema_vintage",
        "release_date",
        "current_period",
        "target_period",
        "period_end_exclusive",
        "tax_basis",
        "fee_basis",
        "price_basis_override",
    }
    if (
        adapter != "lms-ftp"
        or set(context) - permitted
        or context.get("tax_basis") != "unknown"
        or context.get("fee_basis") != "unknown"
        or context.get("target_period") is not None
    ):
        _fail("context_contract")
    start = _iso(context.get("current_period"))
    end = _iso(context.get("period_end_exclusive"))
    released = _iso(context.get("release_date"))
    vintage = context.get("schema_vintage")
    supported = (
        vintage == "lms-20260626-final"
        and released >= date(2026, 6, 26)
        or vintage == "lms-final-12-observed-20250106"
        and date(2025, 1, 6) <= released < date(2026, 6, 26)
    )
    if not supported or not start <= released < end:
        _fail("vintage_or_period")
    profile = _basis_override(context, enabled=True) if "price_basis_override" in context else None
    records, members = _read(data)
    _controls(records, start, released)
    drugs = _keyed(records, "lms01.txt", 1, 11)
    packages = _keyed(records, "lms02.txt", 12, 17)
    prices = _keyed(records, "lms03.txt", 1, 6)
    classifications = _keyed(records, "lms04.txt", 5, 10)
    if not packages or prices.keys() != packages.keys():
        _fail("package_price_keys")
    if not classifications.keys() <= packages.keys():
        _fail("classification_package_join")
    for _, package in packages.values():
        if package[:11].decode("ascii", errors="replace") not in drugs:
            _fail("package_drug_join")

    assertions: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    def problem(identifier: str, locator: str, code: str, field: str) -> None:
        issues.append(
            {
                "source_locator": locator,
                "code": code,
                "identifier": identifier,
                "field": field,
                "period": start.isoformat(),
                "severity": "error",
                "blocking_scope": "field",
            }
        )

    def assertion(
        table: str,
        identifier: str,
        locator: str,
        values: dict[str, Any],
        presence: dict[str, str],
        raw: dict[str, Any],
    ) -> None:
        assertions.append(
            {
                "table": table,
                "source_locator": locator,
                "source_identifier": identifier,
                "canonical_identifier": identifier,
                "effective_period": start.isoformat(),
                "report_period": start.isoformat(),
                "effective_date": released.isoformat(),
                "values": values,
                "presence": presence,
                "raw": raw,
                "quality": "invalid" if "invalid" in presence.values() else "valid",
            }
        )

    for identifier, (number, row) in prices.items():
        package_number, package = packages[identifier]
        drug_number, drug = drugs[package[:11].decode("ascii")]
        if any(row[a - 1 : b] != b"000000000" for a, b in [(34, 42), (43, 51), (70, 78), (88, 96)]):
            _fail("reserved_price_field")
        identity = {
            label: _fragment("lms02.txt", package_number, field, left, right, package)
            for label, field, left, right in [
                ("drug_id", 1, 1, 11),
                ("varenummer", 2, 12, 17),
                ("dispensing_code", 10, 74, 78),
                ("created_date", 18, 104, 111),
                ("latest_price_change_date", 19, 112, 119),
                ("exit_date", 20, 120, 127),
            ]
        }
        raw_records = {
            "lms01.txt": {"record": drug_number, "raw_hex": drug.hex()},
            "lms02.txt": {"record": package_number, "raw_hex": package.hex()},
            "lms03.txt": {"record": number, "raw_hex": row.hex()},
        }
        for field, left, right, kind in [
            (2, 7, 15, "aip"),
            (3, 16, 24, "registered"),
            (4, 25, 33, "consumer"),
            (7, 52, 60, "reimbursement_basis"),
        ]:
            price = _fragment("lms03.txt", number, field, left, right, row)
            valid = re.fullmatch(r"[0-9]{9}", price["text"]) is not None
            if not valid:
                problem(identifier, price["locator"], "malformed_lms_money", "amount_ore")
            values = {
                "price_type": kind,
                "amount_ore": int(price["text"]) if valid else None,
                "currency": "DKK",
                "price_basis": "per_package",
                "tax_basis": "vat_included" if kind == "consumer" else "unknown",
                "fee_basis": {
                    "consumer": "includes_prescription_and_preparation_fees",
                    "reimbursement_basis": "reimbursement_basis",
                }.get(kind, "unknown"),
                "price_role": "effective_price",
            }
            if (
                profile is not None
                and kind == "aip"
                and profile["effective_period_start_from"]
                <= start.isoformat()
                <= profile["effective_period_start_through"]
            ):
                values.update(tax_basis="excluded", fee_basis="excluded")
            assertion(
                "price_observation",
                identifier,
                price["locator"],
                values,
                {"amount_ore": "present" if valid else "invalid"},
                {
                    "schema_vintage": vintage,
                    "price": price,
                    "package_identity": identity,
                    "records": raw_records,
                },
            )

    for identifier, (number, row) in classifications.items():
        package_number, package = packages[identifier]
        abc = _fragment("lms04.txt", number, 6, 35, 35, row)
        group = _fragment("lms04.txt", number, 1, 1, 4, row)
        valid_abc = abc["text"] in {"A", "B", "C"}
        valid_group = re.fullmatch(r"[0-9]{4}", group["text"]) is not None
        if not valid_abc:
            problem(identifier, abc["locator"], "unknown_lms_abc_code", "abc_code")
        if not valid_group:
            problem(identifier, group["locator"], "invalid_lms_group_code", "group_code")
        assertion(
            "classification_observation",
            identifier,
            abc["locator"],
            {
                "classification_context": "ordinary",
                "abc_code": abc["text"] if valid_abc else None,
                "group_namespace": "lms:package-substitution",
                "group_code": group["text"] if valid_group else None,
                "group_scope": f"period:{start.isoformat()}/release:{released.isoformat()}",
            },
            {
                "abc_code": "present" if valid_abc else "invalid",
                "group_code": "present" if valid_group else "invalid",
            },
            {
                "schema_vintage": vintage,
                "classification": abc,
                "group": group,
                "records": {
                    "lms04.txt": {"record": number, "raw_hex": row.hex()},
                    "lms02.txt": {"record": package_number, "raw_hex": package.hex()},
                },
            },
        )
    return {
        "assertions": assertions,
        "issues": issues,
        "source_rows": len(prices) + len(classifications),
        "sheet": "LMS ZIP",
        "header_row": None,
        "date_system": None,
        "metadata": {
            "schema_vintage": vintage,
            "release_date": released.isoformat(),
            "current_period": start.isoformat(),
            "period_end_exclusive": end.isoformat(),
        },
        "physical_accounting": {
            "members": members,
            "member_count": len(members),
            "physical_records": sum(len(rows) for rows in records.values()),
            "price_source_records": len(prices),
            "classification_source_records": len(classifications),
            "price_assertions": len(prices) * 4,
            "classification_assertions": len(classifications),
            "unsupported_price_fields_retained_raw": [8, 10],
            "reserved_price_fields_validated_not_asserted": [5, 6, 9, 11],
        },
    }
