"""Bounded final LMS ZIP interpretation; controls establish integrity, not authority.

Only the June 2026 fixed-byte layout and an explicitly bounded observed older
profile are supported. A caller must bind a daily release date to an independently
established official tariff interval. SYSTEM's
effective date is preserved separately from that interval's start. No identity,
lifecycle, supply or unchanged-price assertion is inferred from omission.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from datetime import date, datetime
from typing import Any

from .base import AdapterError, Assertion, Parsed, Problem
from .report_basis import BASIS_OVERRIDE_KEY, validated_basis_override, with_basis_override

LMS_FTP_VERSION = "lms-ftp-zip-v1"
SCHEMA_VINTAGE = "lms-20260626-final"
OBSERVED_SCHEMA_VINTAGE = "lms-final-12-observed-20250106"
# Widths exclude the mandatory CRLF terminator. This is a complete final release,
# including members whose fields are deliberately not interpreted by this reader.
WIDTHS = {
    1: 180,
    2: 137,
    3: 96,
    4: 47,
    5: 10,
    7: 11,
    9: 60,
    10: 69,
    11: 63,
    12: 152,
    13: 52,
    14: 65,
    15: 65,
    16: 63,
    17: 555,
    18: 67,
    19: 65,
    20: 61,
    21: 71,
    22: 108,
    23: 24,
    24: 60,
    25: 26,
    26: 164,
    27: 18,
    28: 183,
    29: 46,
    30: 367,
    31: 36,
    32: 30,
    34: 21,
    35: 2003,
}
MEMBERS = {f"lms{number:02}.txt": width for number, width in WIDTHS.items()}
PRICE_FIELDS = {2: "aip", 3: "registered", 4: "consumer", 7: "reimbursement_basis"}
MAX_UNCOMPRESSED = 128 * 1024 * 1024


def _iso(value: Any) -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except (TypeError, ValueError) as exc:
        raise AdapterError("invalid_lms_period_context", "manifest:context") from exc


def _context(context: dict[str, Any]) -> tuple[date, date, date]:
    allowed = {
        "schema_vintage",
        "release_date",
        "current_period",
        "target_period",
        "period_end_exclusive",
        "tax_basis",
        "fee_basis",
        BASIS_OVERRIDE_KEY,
    }
    if (
        set(context) - allowed
        or not isinstance(context.get("schema_vintage"), str)
        or context["schema_vintage"] not in {SCHEMA_VINTAGE, OBSERVED_SCHEMA_VINTAGE}
        or context.get("tax_basis") != "unknown"
        or context.get("fee_basis") != "unknown"
        or context.get("target_period") is not None
    ):
        raise AdapterError("unsupported_lms_context", "manifest:context")
    start = _iso(context.get("current_period"))
    end = _iso(context.get("period_end_exclusive"))
    released = _iso(context.get("release_date"))
    if not start <= released < end:
        raise AdapterError("lms_release_outside_tariff_period", "manifest:context")
    # The older profile describes observed identical fixed-byte layouts using a
    # later interpretation. It makes no historical documentation/availability
    # claim and supplies no reviewed monetary basis by itself.
    supported = (
        released >= date(2026, 6, 26)
        if context["schema_vintage"] == SCHEMA_VINTAGE
        else date(2025, 1, 6) <= released < date(2026, 6, 26)
    )
    if not supported:
        raise AdapterError("unsupported_lms_schema_vintage", "manifest:context")
    return start, end, released


def _lines(data: bytes, member: str, width: int | None = None) -> list[bytes]:
    if not data:
        return []
    if not data.endswith(b"\r\n"):
        raise AdapterError("lms_record_terminator", f"member:{member}")
    records = data[:-2].split(b"\r\n")
    if any(b"\r" in row or b"\n" in row for row in records):
        raise AdapterError("lms_record_terminator", f"member:{member}")
    if width is not None and any(len(row) != width for row in records):
        raise AdapterError("lms_record_width", f"member:{member}")
    return records


def _zip(data: bytes) -> dict[str, list[bytes]]:
    if len(data) > MAX_UNCOMPRESSED:
        raise AdapterError("source_resource_limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            # Exact lowercase flat names are part of this bounded schema. Reject
            # directory traversals, aliases, duplicate entries and extra members.
            if len(set(names)) != len(names):
                raise AdapterError("lms_duplicate_zip_member")
            if set(names) != {*MEMBERS, "system.txt"}:
                raise AdapterError("lms_zip_member_set")
            if any(
                info.orig_filename != info.filename
                or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}
                for info in infos
            ):
                raise AdapterError("lms_unsafe_zip_member")
            if any(info.flag_bits & 1 for info in infos):
                raise AdapterError("lms_encrypted_zip_member")
            if any(
                info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                for info in infos
            ):
                raise AdapterError("lms_zip_compression")
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED:
                raise AdapterError("source_resource_limit")
            return {
                member: _lines(archive.read(member), member, MEMBERS.get(member))
                for member in sorted(names)
            }
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as exc:
        raise AdapterError("invalid_lms_zip") from exc


def _digits(raw: bytes, locator: str, code: str = "lms_invalid_key") -> str:
    if re.fullmatch(rb"[0-9]+", raw) is None:
        raise AdapterError(code, locator)
    return raw.decode("ascii")


def _system(rows: dict[str, list[bytes]], start: date, released: date) -> None:
    system = rows["system.txt"]
    loc = "member:system.txt"
    if (
        len(system) != len(MEMBERS) + 3
        or len(system[0]) != 93
        or len(system[1]) != 47
        or system[0][:2] != b"00"
        or system[1][:2] != b"01"
        or any(len(row) != 51 or row[:2] != b"10" for row in system[2:])
    ):
        raise AdapterError("lms_system_layout", loc)
    control = system[0]
    if (
        control[2:7] != b"12.0 "
        or control[7:47].rstrip() != b"LMS-TAKST"
        or control[55:71] != b" " * 16
        or control[71:73] != b"01"
        or control[75:87].rstrip() != b"LMS.ZIP"
    ):
        raise AdapterError("unsupported_lms_system_schema", loc)
    if _digits(control[73:75], loc) != "33":
        raise AdapterError("lms_system_member_count", loc)
    try:
        native_date = datetime.strptime(control[47:55].decode("ascii"), "%Y%m%d").date()
    except (UnicodeDecodeError, ValueError) as exc:
        raise AdapterError("lms_system_date", loc) from exc
    if native_date != released:
        raise AdapterError("lms_release_date_conflict", loc)
    iso = start.isocalendar()
    if control[87:93] != f"{iso.year:04}{iso.week:02}".encode("ascii"):
        raise AdapterError("lms_tariff_week_conflict", loc)
    # Parameter record is retained in the original; numeric controls do not imply
    # a tax/fee definition for a particular interpreted price field.
    _digits(system[1][2:], loc, "lms_system_parameter")
    catalogued = set()
    for row in system[2:]:
        try:
            member = row[32:44].decode("ascii").rstrip().lower()
        except UnicodeDecodeError as exc:
            raise AdapterError("lms_system_catalogue", loc) from exc
        if member not in rows or member in catalogued or row[44:46] != b"01":
            raise AdapterError("lms_system_catalogue", loc)
        catalogued.add(member)
        if int(_digits(row[46:51], loc, "lms_system_record_count")) != len(rows[member]):
            raise AdapterError("lms_system_record_count", f"member:{member}")
    if catalogued != set(rows):
        raise AdapterError("lms_system_catalogue", loc)


def _index(rows: list[bytes], member: str, start: int, end: int) -> dict[str, tuple[int, bytes]]:
    indexed = {}
    for record, row in enumerate(rows, 1):
        key = _digits(row[start:end], f"member:{member}!record:{record}")
        if key in indexed:
            raise AdapterError("lms_duplicate_key", f"member:{member}!record:{record}")
        indexed[key] = record, row
    return indexed


def _field(
    member: str, record: int, number: int, start: int, end: int, row: bytes
) -> dict[str, Any]:
    token = row[start - 1 : end]
    # Do not silently choose one of the two legacy encodings for ambiguous core
    # text. LMS10 and all other uninterpreted text remain immutable raw bytes.
    text = token.decode("cp850")
    if text != token.decode("cp865"):
        raise AdapterError("lms_ambiguous_core_encoding", f"member:{member}!record:{record}")
    return {
        "locator": f"member:{member}!record:{record}!field:{number:02}[{start}:{end}]",
        "raw_hex": token.hex(),
        "text": text,
    }


def parse_lms_ftp(data: bytes, context: dict[str, Any]) -> Parsed:
    """Interpret only four direct per-package LMS03 fields after full ZIP controls.

    Records counts interpreted LMS03 and LMS04 rows, not emitted assertions.
    A malformed amount is a field issue; ambiguous keys/joins/schema are fatal.
    The ingestion boundary independently checks trusted phase/calendar/coverage
    and any optional reviewed monetary evidence delivery.
    """
    start, _, released = _context(context)
    profile = validated_basis_override(context) if BASIS_OVERRIDE_KEY in context else None
    rows = _zip(data)
    _system(rows, start, released)
    drugs = _index(rows["lms01.txt"], "lms01.txt", 0, 11)
    packages = _index(rows["lms02.txt"], "lms02.txt", 11, 17)
    prices = _index(rows["lms03.txt"], "lms03.txt", 0, 6)
    if not packages or set(packages) != set(prices):
        raise AdapterError("lms_package_price_key_set")
    for record, row in packages.values():
        drug_id = _digits(row[:11], f"member:lms02.txt!record:{record}")
        if drug_id not in drugs:
            raise AdapterError("lms_package_drug_join", f"member:lms02.txt!record:{record}")
    classes = _index(rows["lms04.txt"], "lms04.txt", 4, 10)
    if set(classes) - set(packages):
        raise AdapterError("lms_classification_package_join", "member:lms04.txt")
    parsed = Parsed(records=len(prices) + len(classes))
    for package, (record, row) in prices.items():
        package_record, package_row = packages[package]
        drug_id = package_row[:11].decode("ascii")
        drug_record, drug_row = drugs[drug_id]
        # Reserved fields are schema controls, never inferred zero-valued prices.
        if any(
            row[6 + (number - 2) * 9 : 6 + (number - 1) * 9] != b"000000000"
            for number in (5, 6, 9, 11)
        ):
            raise AdapterError("lms_reserved_price_field", f"member:lms03.txt!record:{record}")
        identity_fields = {
            "drug_id": _field("lms02.txt", package_record, 1, 1, 11, package_row),
            "varenummer": _field("lms02.txt", package_record, 2, 12, 17, package_row),
            "dispensing_code": _field("lms02.txt", package_record, 10, 74, 78, package_row),
            "created_date": _field("lms02.txt", package_record, 18, 104, 111, package_row),
            "latest_price_change_date": _field(
                "lms02.txt", package_record, 19, 112, 119, package_row
            ),
            "exit_date": _field("lms02.txt", package_record, 20, 120, 127, package_row),
        }
        source_records = {
            "lms01.txt": {"record": drug_record, "raw_hex": drug_row.hex()},
            "lms02.txt": {"record": package_record, "raw_hex": package_row.hex()},
            "lms03.txt": {"record": record, "raw_hex": row.hex()},
        }
        for number, price_type in PRICE_FIELDS.items():
            begin = 7 + (number - 2) * 9
            native = _field("lms03.txt", record, number, begin, begin + 8, row)
            token = native["text"]
            valid = re.fullmatch(r"[0-9]{9}", token) is not None
            if not valid:
                parsed.issues.append(
                    Problem(
                        native["locator"],
                        "malformed_lms_money",
                        identifier=package,
                        field="amount_ore",
                        period=start,
                    )
                )
            values = {
                "price_type": price_type,
                "amount_ore": int(token) if valid else None,
                "currency": "DKK",
                "price_basis": "per_package",
                "tax_basis": "vat_included" if price_type == "consumer" else "unknown",
                "fee_basis": (
                    "includes_prescription_and_preparation_fees"
                    if price_type == "consumer"
                    else "reimbursement_basis"
                    if price_type == "reimbursement_basis"
                    else "unknown"
                ),
                "price_role": "effective_price",
            }
            parsed.assertions.append(
                Assertion(
                    "price_observation",
                    native["locator"],
                    package,
                    values,
                    {"amount_ore": "present" if valid else "invalid"},
                    {
                        "schema_vintage": context["schema_vintage"],
                        "price": native,
                        "package_identity": identity_fields,
                        "records": source_records,
                    },
                    effective_period=start,
                    report_period=start,
                    effective_date=released,
                    quality_state="valid" if valid else "invalid",
                )
            )
    for package, (record, row) in classes.items():
        abc = _field("lms04.txt", record, 6, 35, 35, row)
        group = _field("lms04.txt", record, 1, 1, 4, row)
        abc_valid = abc["text"] in {"A", "B", "C"}
        group_valid = re.fullmatch(r"[0-9]{4}", group["text"]) is not None
        for valid, field, native, code in (
            (abc_valid, "abc_code", abc, "unknown_lms_abc_code"),
            (group_valid, "group_code", group, "invalid_lms_group_code"),
        ):
            if not valid:
                parsed.issues.append(
                    Problem(
                        native["locator"],
                        code,
                        identifier=package,
                        field=field,
                        period=start,
                    )
                )
        package_record, package_row = packages[package]
        parsed.assertions.append(
            Assertion(
                "classification_observation",
                abc["locator"],
                package,
                {
                    "classification_context": "ordinary",
                    "abc_code": abc["text"] if abc_valid else None,
                    "group_namespace": "lms:package-substitution",
                    "group_code": group["text"] if group_valid else None,
                    "group_scope": f"period:{start.isoformat()}/release:{released.isoformat()}",
                },
                {
                    "abc_code": "present" if abc_valid else "invalid",
                    "group_code": "present" if group_valid else "invalid",
                },
                {
                    "schema_vintage": context["schema_vintage"],
                    "classification": abc,
                    "group": group,
                    "records": {
                        "lms04.txt": {"record": record, "raw_hex": row.hex()},
                        "lms02.txt": {"record": package_record, "raw_hex": package_row.hex()},
                    },
                },
                effective_period=start,
                report_period=start,
                effective_date=released,
                quality_state="valid" if abc_valid and group_valid else "invalid",
            )
        )
    return with_basis_override(parsed, profile) if profile else parsed
