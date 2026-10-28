"""Independent native-byte reconciliation of persisted LMS registry records.

The byte boundaries below are transcribed from the official LMS specification
(Nyt rows 7–338), independently of the importing adapter. The observed 11.0/12.0
profiles are structural admissions, not approval of historical business meanings.
No production layout, parser, converter or normalized record supplies expectations.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from collections import Counter, OrderedDict
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

# Capture provenance once with this loaded implementation. A running writer must
# never attribute cached code to source bytes changed after module import.
AUDITOR_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

# Independently transcribed inclusive END positions. Every preceding field ends
# immediately before the next begins. d=ASCII identifier; o=exact integer ore;
# c=calendar date; omitted suffix=unscaled lexical text. Names are the persistence
# interface; boundaries and conversion expectations are independently specified.
_SPEC: dict[int, tuple[int, str]] = {
    1: (
        180,
        "drug_id:11:d product_type:13 product_subtype:15 alphabetic_sequence:24:d "
        "speciality_number:29 name:59 form:79 form_code:86 additional_form_token:93 "
        "strength:113 strength_numeric_token:123 strength_unit:126 authorisation_holder:132 "
        "drug_distributor:138 atc:146 administration_codes:154 traffic_warning:155 "
        "substitution_flag:156 renewal_token:157 legacy_20:158 legacy_21:159 "
        "drug_substitution_group:163 dose_suitable:164 deregistration_date:172:c "
        "quarantine_date:180:c",
    ),
    2: (
        137,
        "drug_id:11:d package_number:17:d alphabetic_sequence:20 component_package:26 "
        "component_count_token:29 pack_description:59 pack_size_token:67 pack_unit:69 "
        "container_code:73 dispensing_code:78 speciality_code:83 reimbursement_code:85 "
        "reimbursement_clause:90 ddd_token:99 storage_time_token:101 storage_time_unit:102 "
        "storage_code:103 creation_date:111:c last_price_change_date:119:c exit_date:127:c "
        "calculation_code:128 reimbursement_group_flag:129 preparation_fee_flag:130 "
        "safety_features_token:131 package_distributor:137",
    ),
    3: (
        96,
        "package_number:6:d aip_ore:15:o registered_ore:24:o consumer_ore:33:o "
        "legacy_05:42 legacy_06:51 reimbursement_ore:60:o hospital_slot_08:69:o "
        "legacy_09:78 non_reimbursable_ore:87:o legacy_11:96",
    ),
    4: (
        47,
        "group_code:4 package_number:10:d pack_size_token:18 alphabetic_sequence:27 "
        "legacy_05:34 abc_code:35 cheapest_package:41 legacy_08:47",
    ),
    5: (10, "group_code:4 package_number:10:d"),
    7: (11, "group_code:4 package_number:10:d legacy_03:11"),
    9: (60, "company_code:6 short_name:26 company_name:58 parallel_import_code:60"),
    10: (69, "drug_id:11:d change_date:19:c previous_name:69"),
    11: (63, "code:2 short_text:12 description:62 legacy_04:63"),
    12: (152, "atc:8 description:80 legacy_07:152"),
    13: (52, "code:1 description:51 legacy_03:52"),
    14: (65, "code:4 short_text:14 description:64 legacy_04:65"),
    15: (65, "unit_type:1 code:4 short_text:14 description:64 legacy_05:65"),
    16: (63, "code:2 short_text:12 description:62 legacy_04:63"),
    17: (555, "code:5 short_text:55 description:555"),
    18: (67, "code:5 dispensing_group:6 short_text:16 description:66 legacy_05:67"),
    19: (65, "code:5 short_text:15 description:65"),
    20: (61, "code:1 short_text:11 description:61"),
    21: (71, "drug_id:11:d full_name:71"),
    22: (108, "code:7 description:107 active_code:108"),
    23: (24, "patient_type:2 level_code:3 lower_ore:11:o upper_ore:19:o percent_token:24"),
    24: (
        60,
        "drug_id:11:d package_number:17:d drug_group:21 minimum_aip_unit_ore:30:o "
        "minimum_registered_unit_ore:39:o reimbursement_unit_ore:48:o abc_code:49 "
        "cheapest_drug:60",
    ),
    25: (26, "atc:8 indication_code:15 drug_id:26:d"),
    26: (164, "code:7 description:85 line_1:111 line_2:137 line_3:163 active_code:164"),
    27: (18, "drug_id:11:d dosage_code:18"),
    28: (
        183,
        "code:7 short_text:17 description:95 units_per_day_token:104 line_1:130 "
        "line_2:156 line_3:182 active_code:183",
    ),
    29: (46, "recommendation_group:4 drug_id:15:d package_number:21:d recommendation:46"),
    30: (
        367,
        "drug_id:11:d package_number:17:d substance_class:117 substance_group:217 substance:367",
    ),
    31: (
        36,
        "drug_id:11:d package_number:17:d consumer_unit_token:26 consumer_ddd_token:35 "
        "cheapest_flag:36",
    ),
    32: (
        30,
        "ordered_package:6:d substitute_package:12:d alternative_package:18:d "
        "package_count_token:20 combination_consumer_token:29 information_flag:30",
    ),
    33: (
        30,
        "ordered_package:6:d substitute_package:12:d alternative_package:18:d "
        "package_count_token:20 reserved_price_token:29 information_flag:30",
    ),
    34: (21, "drug_id:11:d package_number:17:d patented_flag:18 indication_group:21"),
    35: (2003, "indication_group:3 description:2003"),
    38: (
        60,
        "drug_id:11:d package_number:17:d drug_group:21 reserved_minimum_aip_unit_ore:30 "
        "reserved_minimum_registered_unit_ore:39 reserved_reimbursement_unit_ore:48 "
        "abc_code:49 cheapest_drug:60",
    ),
}


def _compile_fields(width: int, specification: str) -> tuple[tuple[str, int, int, str], ...]:
    """Compile only this auditor's independent transcription once per module."""
    fields = []
    start = 0
    for field in specification.split():
        pieces = field.split(":")
        name, end = pieces[0], int(pieces[1])
        fields.append((name, start, end, pieces[2] if len(pieces) == 3 else "t"))
        start = end
    assert start == width
    return tuple(fields)


_FIELDS = {number: _compile_fields(*definition) for number, definition in _SPEC.items()}


def _lines(data: bytes, *, final_width: int | None = None) -> list[bytes]:
    """Split native CRLF boundaries; an admitted full final record needs no suffix."""
    if not data:
        return []
    terminated = data[-2:] == b"\r\n"
    if not terminated and final_width is None:
        raise ValueError("registry_audit_record_terminator")
    lines = data.split(b"\r\n")
    if terminated:
        lines.pop()
    if any(b"\r" in line or b"\n" in line for line in lines):
        raise ValueError("registry_audit_record_terminator")
    if not terminated and len(lines[-1]) != final_width:
        raise ValueError("registry_audit_record_width")
    return lines


def _fixed_lines(data: bytes, number: int) -> list[bytes]:
    """Frame source bytes using this auditor's independent width transcription."""
    width = _SPEC[number][0]
    position = 0
    output = []
    while position < len(data):
        end = position + width
        if end > len(data):
            raise ValueError("registry_audit_record_width")
        output.append(data[position:end])
        if end == len(data):
            break
        if data[end : end + 2] != b"\r\n":
            raise ValueError("registry_audit_record_terminator")
        position = end + 2
    return output


def _ascii_number(token: bytes) -> bool:
    return token.isdigit()


def _calendar(token: bytes) -> date:
    if len(token) != 8 or not _ascii_number(token):
        raise ValueError("registry_audit_calendar")
    try:
        return date(int(token[:4]), int(token[4:6]), int(token[6:]))
    except ValueError as exc:
        raise ValueError("registry_audit_calendar") from exc


# Results contain only immutable scalar values/states. Expected row dictionaries
# are always rebuilt, and the cache retains at most this many native field tokens.
@lru_cache(maxsize=65536)
def _interpret(token: bytes, kind: str) -> tuple[Any, str | None]:
    stripped = token.strip()
    if not stripped:
        return None, "blank"
    if kind == "c" and stripped == b"00000000":
        return None, "unknown_date"
    if kind in {"d", "o", "c"}:
        if _ascii_number(stripped):
            if kind == "d":
                return stripped.decode("ascii"), None
            if kind == "o":
                return int(stripped), None
            try:
                return _calendar(stripped).isoformat(), None
            except ValueError:
                pass
        return None, "invalid"
    first = stripped.decode("cp850")
    second = stripped.decode("cp865")
    if first != second:
        return None, "ambiguous_encoding"
    return first, None


def _expected(number: int, native: bytes) -> dict[str, Any]:
    width, _ = _SPEC[number]
    if len(native) != width:
        raise ValueError("registry_audit_record_width")
    record: dict[str, Any] = {"raw_sha256": hashlib.sha256(native).hexdigest()}
    states = {}
    for name, start, end, kind in _FIELDS[number]:
        value, state = _interpret(native[start:end], kind)
        record[name] = value
        if state is not None:
            states[name] = state
    record["field_states"] = states
    return record


def _canonical(record: dict[str, Any]) -> bytes:
    return (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def _actual(record: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    result = {field: record.get(field) for field in fields}
    for field, value in result.items():
        if isinstance(value, date) and not isinstance(value, datetime):
            result[field] = value.isoformat()
    states = result["field_states"]
    if isinstance(states, str):
        try:
            states = json.loads(states)
        except (ValueError, TypeError):
            states = {"__invalid_json__": True}
    result["field_states"] = states
    return result


_Record = tuple[tuple[Any, ...], bytes]
_RECORD_FIELDS = {
    number: ("raw_sha256", *(field[0] for field in fields), "field_states")
    for number, fields in _FIELDS.items()
}


def _cache_size(value: Any) -> int:
    # Type objects are shared globals, not allocations retained by an entry.
    if isinstance(value, type):
        return 0
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(_cache_size(key) + _cache_size(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return size + sum(_cache_size(item) for item in value)
    return size


class _RecordCache:
    """Private LRU with conservative key/value charging and entry overhead.

    Cached state dictionaries are created inside the auditor, never returned to
    callers or mutated. Mutable persisted inputs bypass this cache entirely.
    """

    def __init__(self, limit_bytes: int = 96 * 1024 * 1024) -> None:
        self.limit_bytes = limit_bytes
        self.retained_bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self._entries: OrderedDict[tuple[Any, ...], tuple[_Record, int]] = OrderedDict()

    def get(self, key: tuple[Any, ...]) -> _Record | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry[0]

    def put(self, key: tuple[Any, ...], value: _Record) -> None:
        charge = _cache_size((key, value)) + 160
        if charge > self.limit_bytes:
            return
        while self.retained_bytes + charge > self.limit_bytes:
            _, (_, removed) = self._entries.popitem(last=False)
            self.retained_bytes -= removed
            self.evictions += 1
        self._entries[key] = value, charge
        self.retained_bytes += charge


_record_cache = _RecordCache()


def _expected_record(number: int, native: bytes, fields: tuple[str, ...]) -> _Record:
    key = ("expected", number, native)
    cached = _record_cache.get(key)
    if cached is None:
        record = _expected(number, native)
        cached = tuple(record[field] for field in fields), _canonical(record)
        _record_cache.put(key, cached)
    return cached


def _actual_record(number: int, persisted: dict[str, Any], fields: tuple[str, ...]) -> _Record:
    values = tuple(persisted.get(field) for field in fields)
    # Exact scalar types make the complete input immutable. In particular bool,
    # float, subclasses and mutable states must not collide with valid integers.
    cacheable = all(type(value) in (str, int, date, type(None)) for value in values)
    key = ("actual", number, values, tuple(type(value) for value in values))
    cached = _record_cache.get(key) if cacheable else None
    if cached is None:
        record = _actual(persisted, list(fields))
        cached = tuple(record[field] for field in fields), _canonical(record)
        if cacheable:
            _record_cache.put(key, cached)
    return cached


def audit_member(
    number: int,
    data: bytes,
    rows: list[dict[str, Any]],
    *,
    allow_final_record: bool = False,
    fixed_width_framing: bool = False,
) -> dict[str, Any]:
    """Compare every ordered source occurrence with persisted typed values.

    ``rows`` must expand the persisted member's ordered row-ID occurrence list;
    passing only deduplicated physical rows fails. Digests bind full typed values,
    exceptional states and raw row hashes without duplicating raw evidence.
    """
    if number not in _SPEC:
        raise ValueError("registry_audit_unsupported_member")
    native = (
        _fixed_lines(data, number)
        if fixed_width_framing
        else _lines(data, final_width=137 if allow_final_record and number == 2 else None)
    )
    expected_hash, actual_hash = hashlib.sha256(), hashlib.sha256()
    mismatches: Counter[str] = Counter()
    counts: Counter[str] = Counter(source_records=len(native), persisted_records=len(rows))
    raw_ids: dict[str, int] = {}
    id_raws: dict[int, str] = {}
    fields = _RECORD_FIELDS[number]
    for index in range(max(len(native), len(rows))):
        expected = None
        if index < len(native):
            expected, encoded = _expected_record(number, native[index], fields)
            expected_hash.update(encoded)
        actual = None
        if index < len(rows):
            persisted = rows[index]
            actual, encoded = _actual_record(number, persisted, fields)
            actual_hash.update(encoded)
            identity = persisted.get("row_id")
            raw_hash = persisted.get("raw_sha256")
            if type(identity) is not int or not isinstance(raw_hash, str):
                mismatches["row_identity"] += 1
            else:
                if raw_hash in raw_ids and raw_ids[raw_hash] != identity:
                    mismatches["deduplicated_row_identity"] += 1
                if identity in id_raws and id_raws[identity] != raw_hash:
                    mismatches["row_identity_collision"] += 1
                raw_ids[raw_hash] = identity
                id_raws[identity] = raw_hash
        if expected is None or actual is None:
            mismatches["record_occurrence"] += 1
            continue
        for field, value, observed in zip(fields, expected, actual, strict=True):
            if field not in rows[index]:
                mismatches["missing_column:" + field] += 1
            elif observed != value or type(observed) is not type(value):
                mismatches[field] += 1
            counts["compared_fields"] += 1
    counts["distinct_source_records"] = len({hashlib.sha256(line).digest() for line in native})
    return {
        "complete": not mismatches,
        "mismatches": dict(sorted(mismatches.items())),
        "counts": dict(sorted(counts.items())),
        "expected_sha256": expected_hash.hexdigest(),
        "actual_sha256": actual_hash.hexdigest(),
        "member_sha256": hashlib.sha256(data).hexdigest(),
    }


def _control_integer(token: bytes) -> int:
    if not _ascii_number(token):
        raise ValueError("registry_audit_system_numeric")
    return int(token)


def _core_keys(files: dict[str, list[bytes]], phase: str) -> dict[str, int]:
    """Independently bind native drug/package identifiers and final price grain."""
    if not {"lms01.txt", "lms02.txt"} <= files.keys():
        raise ValueError("registry_audit_core_keys")
    drug_keys = [row[0:11] for row in files["lms01.txt"]]
    package_keys = [row[11:17] for row in files["lms02.txt"]]
    package_drugs = [row[0:11] for row in files["lms02.txt"]]
    prices = [row[0:6] for row in files.get("lms03.txt", [])]
    drugs, packages, priced = set(drug_keys), set(package_keys), set(prices)
    counts = {
        "drug_records": len(drug_keys),
        "unique_drug_ids": len(drugs),
        "package_records": len(package_keys),
        "unique_package_numbers": len(packages),
        "price_records": len(prices),
        "unique_priced_package_numbers": len(priced),
        "package_missing_drug": sum(key not in drugs for key in package_drugs),
        "price_missing_package": len(priced - packages),
        "package_missing_price": len(packages - priced),
    }
    if (
        len(drug_keys) != len(drugs)
        or len(package_keys) != len(packages)
        or any(len(key) != 11 or not _ascii_number(key) for key in drugs)
        or any(len(key) != 6 or not _ascii_number(key) for key in packages)
        or counts["package_missing_drug"]
        or (
            phase == "final"
            and ("lms03.txt" not in files or len(prices) != len(priced) or priced != packages)
        )
    ):
        raise ValueError("registry_audit_core_keys")
    return counts


def audit_release(
    path: str | Path, *, allow_final_record: bool = False, fixed_width_framing: bool = False
) -> dict[str, Any]:
    """Read native ZIP/SYSTEM completeness independently of the importer.

    This asserts release structure and native tokens only. The SYSTEM effective
    token is retained verbatim; it is not equated to a tariff start or publication.
    Invalid structure raises a stable error without printing private source data.
    """
    files: dict[str, list[bytes]] = {}
    members: dict[str, dict[str, Any]] = {}
    unterminated = []
    fixed_variants = []
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 64 or sum(entry.file_size for entry in entries) > 256 * 1024 * 1024:
            raise ValueError("registry_audit_zip_limit")
        for entry in entries:
            name = entry.filename.lower()
            if (
                entry.filename != entry.orig_filename
                or name in files
                or re.fullmatch(r"(?:system|lms[0-9]{2})\.txt", name) is None
                or entry.flag_bits & 1
                or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or ((entry.external_attr >> 16) & 0o170000) not in {0, 0o100000}
            ):
                raise ValueError("registry_audit_zip_member")
            content = archive.read(entry)  # zipfile independently verifies CRC.
            number = None if name == "system.txt" else int(name[3:5])
            if number is not None and number not in _SPEC:
                raise ValueError("registry_audit_record_width")
            lines = (
                _fixed_lines(content, number)
                if fixed_width_framing and number is not None
                else _lines(
                    content, final_width=137 if allow_final_record and name == "lms02.txt" else None
                )
            )
            if content and content[-2:] != b"\r\n":
                unterminated.append(name)
            if (
                fixed_width_framing
                and number is not None
                and content
                and (content[-2:] != b"\r\n" or any(b"\r" in row or b"\n" in row for row in lines))
            ):
                fixed_variants.append(name)
            if number is not None and (
                number not in _SPEC or any(len(line) != _SPEC[number][0] for line in lines)
            ):
                raise ValueError("registry_audit_record_width")
            files[name] = lines
            members[name] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "physical_records": len(lines),
                "record_width": None if number is None else _SPEC[number][0],
            }
    system = files.get("system.txt", [])
    if len(system) < 2 or len(system[0]) != 93 or len(system[1]) != 47:
        raise ValueError("registry_audit_system_layout")
    control, parameter, *catalogue = system
    version = control[2:7].strip().decode("ascii")
    packed_name = control[75:87].strip().decode("ascii").lower()
    phase = "preliminary" if packed_name == "lms-gros.zip" else "final"
    if (
        version not in {"11.0", "12.0"}
        or control[:2] != b"00"
        or control[7:47] != b"LMS-TAKST".ljust(40)
        or control[71:73] != b"01"
        or packed_name not in {"lms.zip", "lms-gros.zip"}
        or parameter[:2] != b"01"
        or _control_integer(control[73:75]) != len(files)
        or len(catalogue) != len(files)
    ):
        raise ValueError("registry_audit_system_contract")
    _calendar(control[47:55])
    _control_integer(control[87:93])
    parameter_slots = [_control_integer(parameter[start : start + 5]) for start in range(2, 47, 5)]
    seen = set()
    for catalogue_entry in catalogue:
        if len(catalogue_entry) != 51:
            raise ValueError("registry_audit_system_catalogue")
        name = catalogue_entry[32:44].strip().decode("ascii").lower()
        if (
            catalogue_entry[:2] != b"10"
            or catalogue_entry[44:46] != b"01"
            or name not in files
            or name in seen
            or _control_integer(catalogue_entry[46:51]) != len(files[name])
        ):
            raise ValueError("registry_audit_system_catalogue")
        seen.add(name)
    if seen != set(files):
        raise ValueError("registry_audit_system_catalogue")
    # Final 31/33-member observed profiles differ by patent-indication files.
    common = {1, 2, 3, 4, 5, 7, *range(9, 33)}
    observed = {int(name[3:5]) for name in files if name != "system.txt"}
    supported = [common, common | {34, 35}]
    if phase == "final" and (
        observed not in supported or (version == "11.0" and observed != supported[0])
    ):
        raise ValueError("registry_audit_member_set")
    result = {
        "complete": True,
        "schema_version": version,
        "phase": phase,
        "effective_token": control[47:55].decode("ascii"),
        "period_week_token": control[87:93].decode("ascii"),
        "previous_effective_token": control[55:63].decode("ascii"),
        "next_effective_token": control[63:71].decode("ascii"),
        "packed_name": packed_name,
        "parameter_record": parameter.decode("ascii"),
        "parameter_slots": parameter_slots,
        "member_count": len(files),
        "key_coverage": _core_keys(files, phase),
        "members": members,
        "system_sha256": members["system.txt"]["sha256"],
    }
    if unterminated:
        result["unterminated_members"] = sorted(unterminated)
    if fixed_width_framing:
        result["fixed_width_members"] = sorted(fixed_variants)
    return result
