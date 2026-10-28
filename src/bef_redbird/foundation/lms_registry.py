"""Compact, immutable registry storage behind the existing foundation Store.

Values are deduplicated at native-row and ordered-member granularity. Each
delivery/import retains its own member bindings, record ordinals and clocks.
Independent reconciliation is a separate gate before analytical publication.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from .lms_registry_layout import LAYOUTS, PROFILE
from .records import canonical_json, digest, identity

MISSING_FINAL_CRLF_PROFILE = "lms-native-registry-lms02-final-record-no-crlf-v2"
FIXED_WIDTH_PROFILE = "lms-native-registry-fixed-width-framing-v3"

if TYPE_CHECKING:
    from .store import Store


class RegistryError(ValueError):
    """Unsupported or inconsistent native evidence; never an empty snapshot."""


@dataclass(frozen=True)
class NativeRelease:
    effective_date: date
    tariff_week: str
    system_version: str
    phase: str
    members: dict[str, bytes]
    member_names: dict[str, str]
    parameters: bytes


def records(
    data: bytes, *, final_width: int | None = None, fixed_width: int | None = None
) -> list[bytes]:
    if not data:
        return []
    if fixed_width is not None:
        result = []
        for offset in range(0, len(data), fixed_width + 2):
            row = data[offset : offset + fixed_width]
            if len(row) != fixed_width:
                raise RegistryError("native_record_width")
            boundary = offset + fixed_width
            if boundary != len(data) and data[boundary : boundary + 2] != b"\r\n":
                raise RegistryError("native_record_terminator")
            result.append(row)
        return result
    terminated = data.endswith(b"\r\n")
    if not terminated and final_width is None:
        raise RegistryError("native_record_terminator")
    rows = (data[:-2] if terminated else data).split(b"\r\n")
    if any(b"\n" in row or b"\r" in row for row in rows):
        raise RegistryError("native_record_terminator")
    if not terminated and len(rows[-1]) != final_width:
        raise RegistryError("native_record_width")
    return rows


def read_release(path: Path, phase: str, *, parser_version: str = PROFILE) -> NativeRelease:
    if parser_version not in {PROFILE, MISSING_FINAL_CRLF_PROFILE, FIXED_WIDTH_PROFILE}:
        raise RegistryError("unsupported_registry_parser")
    if phase not in {"final", "preliminary"}:
        raise RegistryError("explicit_release_phase_required")
    if parser_version != PROFILE and phase != "final":
        raise RegistryError("unsupported_registry_variant_phase")
    try:
        with zipfile.ZipFile(path) as source:
            info = source.infolist()
            names = [entry.filename.lower() for entry in info]
            if (
                len(names) != len(set(names))
                or sum(entry.file_size for entry in info) > 128 * 1024 * 1024
                or any(
                    re.fullmatch(r"(?:lms[0-9]{2}|system)\.txt", entry.filename, re.I) is None
                    or entry.orig_filename != entry.filename
                    or entry.flag_bits & 1
                    or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                    for entry in info
                )
            ):
                raise RegistryError("unsupported_zip_members")
            members = {entry.filename.lower(): source.read(entry) for entry in info}
            originals = {entry.filename.lower(): entry.filename for entry in info}
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise RegistryError("invalid_zip") from exc
    if "system.txt" not in members:
        raise RegistryError("missing_system")
    system = records(members["system.txt"])
    if (
        len(system) < 3
        or len(system[0]) != 93
        or len(system[1]) != 47
        or system[0][:2] != b"00"
        or system[1][:2] != b"01"
        or any(len(row) != 51 or row[:2] != b"10" for row in system[2:])
    ):
        raise RegistryError("unsupported_system_layout")
    control = system[0]
    version = control[2:7].decode("ascii").strip()
    if version not in {"11.0", "12.0"} or control[7:47].strip() != b"LMS-TAKST":
        raise RegistryError("unsupported_system_version")
    packed = control[75:87].strip().lower()
    if packed != (b"lms.zip" if phase == "final" else b"lms-gros.zip"):
        raise RegistryError("release_phase_conflict")
    if control[55:71].strip() or control[71:73] != b"01":
        raise RegistryError("unsupported_system_context")
    try:
        effective = datetime.strptime(control[47:55].decode("ascii"), "%Y%m%d").date()
        week = control[87:93].decode("ascii")
        if not re.fullmatch(r"[0-9]{6}", week):
            raise ValueError
        if int(control[73:75]) != len(members):
            raise RegistryError("system_member_count")
    except (ValueError, UnicodeError) as exc:
        raise RegistryError("invalid_system_context") from exc
    catalogue: dict[str, int] = {}
    for row in system[2:]:
        name = row[32:44].decode("ascii").strip().lower()
        if name in catalogue or row[44:46] != b"01" or not row[46:51].isdigit():
            raise RegistryError("system_catalogue")
        catalogue[name] = int(row[46:51])
    if set(catalogue) != set(members):
        raise RegistryError("system_catalogue_members")
    fixed_variant = False
    for name, payload in members.items():
        number = None if name == "system.txt" else int(name[3:5])
        if number is not None and number not in LAYOUTS:
            raise RegistryError("unsupported_member_layout")
        lines = records(
            payload,
            final_width=137
            if parser_version == MISSING_FINAL_CRLF_PROFILE and name == "lms02.txt"
            else None,
            fixed_width=LAYOUTS[number][0]
            if parser_version == FIXED_WIDTH_PROFILE and number is not None
            else None,
        )
        if len(lines) != catalogue[name]:
            raise RegistryError("system_record_count")
        if name == "system.txt":
            continue
        assert number is not None
        if number not in LAYOUTS or any(len(row) != LAYOUTS[number][0] for row in lines):
            raise RegistryError("unsupported_member_layout")
        fixed_variant |= bool(payload) and (
            not payload.endswith(b"\r\n") or any(b"\r" in row or b"\n" in row for row in lines)
        )
    if parser_version == MISSING_FINAL_CRLF_PROFILE and (
        not members.get("lms02.txt") or members["lms02.txt"].endswith(b"\r\n")
    ):
        raise RegistryError("native_variant_not_present")
    if parser_version == FIXED_WIDTH_PROFILE and not fixed_variant:
        raise RegistryError("native_variant_not_present")
    required = {f"lms{n:02}.txt" for n in LAYOUTS if n <= 32} | {"system.txt"}
    if phase == "final" and set(members) not in (
        required,
        required | {"lms34.txt", "lms35.txt"},
    ):
        raise RegistryError("unsupported_final_member_set")
    # Preliminary admission is separately reviewed; no final-price projection.
    if phase == "preliminary" and not {"lms01.txt", "lms02.txt"} <= set(members):
        raise RegistryError("incomplete_preliminary_release")
    return NativeRelease(effective, week, version, phase, members, originals, system[1])


def decode_row(number: int, raw: bytes) -> dict[str, Any]:
    width, fields = LAYOUTS[number]
    if len(raw) != width:
        raise RegistryError("native_record_width")
    result: dict[str, Any] = {"raw_sha256": hashlib.sha256(raw).hexdigest()}
    states: dict[str, str] = {}
    for name, start, end, kind in fields:
        token = raw[start - 1 : end].strip()
        value: Any = None
        if not token:
            states[name] = "blank"
        elif kind == "date" and token == b"00000000":
            states[name] = "unknown_date"
        elif kind in {"digits", "ore"}:
            if re.fullmatch(rb"[0-9]+", token):
                value = int(token) if kind == "ore" else token.decode("ascii")
            else:
                states[name] = "invalid"
        elif kind == "date":
            try:
                if re.fullmatch(rb"[0-9]{8}", token) is None:
                    raise ValueError
                value = datetime.strptime(token.decode("ascii"), "%Y%m%d").date()
            except ValueError:
                states[name] = "invalid"
        else:
            first, second = token.decode("cp850"), token.decode("cp865")
            if first == second:
                value = first
            else:
                states[name] = "ambiguous_encoding"
        result[name] = value
    result["field_states"] = canonical_json(states)
    return result


def _insert_member(
    store: Store,
    number: int,
    data: bytes,
    profile: str,
    *,
    final_width: int | None = None,
    fixed_width: int | None = None,
) -> int:
    con = store._connection
    source_hash = hashlib.sha256(data).hexdigest()
    existing = con.execute(
        "SELECT member_id FROM registry_member "
        "WHERE member_number=? AND profile=? AND raw_sha256=?",
        [number, profile, source_hash],
    ).fetchone()
    if existing:
        return int(existing[0])
    native = records(data, final_width=final_width, fixed_width=fixed_width)
    hashes = [hashlib.sha256(row).hexdigest() for row in native]
    table = f"registry_lms{number:02}"
    unique = dict(zip(hashes, native, strict=True))
    if hashes:
        keys = pd.DataFrame({"raw_sha256": list(unique)}, dtype=object)
        con.register("_registry_keys", keys)
        try:
            found = dict(
                con.execute(
                    f"SELECT r.raw_sha256,r.row_id FROM {table} r "
                    "JOIN _registry_keys k USING(raw_sha256) WHERE r.profile=?",
                    [profile],
                ).fetchall()
            )
        finally:
            con.unregister("_registry_keys")
        pending = [decode_row(number, raw) for sha, raw in unique.items() if sha not in found]
        if pending:
            frame = pd.DataFrame(pending, dtype=object)
            for name, _, _, kind in LAYOUTS[number][1]:
                if kind == "ore":
                    frame[name] = pd.array([row[name] for row in pending], dtype="Int64")
            con.register("_registry_rows", frame)
            try:
                columns = ",".join(frame.columns)
                con.execute(
                    f"INSERT INTO {table} (row_id,profile,{columns}) "
                    f"SELECT nextval('registry_row_sequence'),?,{columns} FROM _registry_rows",
                    [profile],
                )
            finally:
                con.unregister("_registry_rows")
            con.register("_registry_keys", keys)
            try:
                found = dict(
                    con.execute(
                        f"SELECT r.raw_sha256,r.row_id FROM {table} r "
                        "JOIN _registry_keys k USING(raw_sha256) WHERE r.profile=?",
                        [profile],
                    ).fetchall()
                )
            finally:
                con.unregister("_registry_keys")
        row_ids = [found[sha] for sha in hashes]
    else:
        row_ids = []
    inserted = con.execute(
        "INSERT INTO registry_member VALUES(nextval('registry_member_sequence'),?,?,?,?,?,?) "
        "RETURNING member_id",
        [number, profile, source_hash, len(native), row_ids, digest({"row_ids": row_ids})],
    ).fetchone()
    assert inserted is not None
    return int(inserted[0])


def ingest_registry(
    store: Store,
    delivery_id: str,
    *,
    phase: str,
    context: dict[str, Any],
    parser_version: str = PROFILE,
) -> dict[str, Any]:
    """Use the existing delivery, archive, import and writer transaction contracts."""
    store._check_owner()
    if parser_version not in {PROFILE, MISSING_FINAL_CRLF_PROFILE, FIXED_WIDTH_PROFILE}:
        raise RegistryError("unsupported_registry_parser")
    native_path = store.original_path(delivery_id)
    import_id = store.begin_import(
        delivery_id,
        parser_version=parser_version,
        schema_version="registry-1",
        mapping_version="edition-local-native-keys-v1",
        context={**context, "phase": phase},
    )
    prior = store.get_record("import_batch", import_id)
    if prior["validation_status"] != "running":
        return {"import_id": import_id, "status": prior["validation_status"], "retry": True}
    try:
        release = read_release(native_path, phase, parser_version=parser_version)
    except RegistryError as exc:
        with store.transaction():
            store.add_issue(
                import_id,
                source_locator="container:zip",
                code=str(exc),
                severity="error",
                blocking_scope="import",
            )
            store.finish_import(
                import_id, status="rejected", record_count=1, accepted_count=0, rejected_count=1
            )
        return {"import_id": import_id, "status": "rejected", "reason": str(exc)}
    profile = parser_version + ":system-" + release.system_version
    # Framing changes no typed field semantics. Reuse v1 dictionaries and rows.
    member_profile = PROFILE + ":system-" + release.system_version
    final_width = 137 if parser_version == MISSING_FINAL_CRLF_PROFILE else None
    fixed_framing = parser_version == FIXED_WIDTH_PROFILE

    def member_records(number: int) -> list[bytes]:
        return records(
            release.members[f"lms{number:02}.txt"],
            final_width=final_width if number == 2 else None,
            fixed_width=LAYOUTS[number][0] if fixed_framing else None,
        )

    # Identity key validation is independent of text/numeric-attribute usability.
    package_rows = member_records(2)
    drug_rows = member_records(1)
    packages = [row[11:17] for row in package_rows]
    drugs = {row[:11] for row in drug_rows}
    valid = (
        len(set(packages)) == len(packages)
        and len(drugs) == len(drug_rows)
        and all(re.fullmatch(rb"[0-9]{6}", key) for key in packages)
        and all(re.fullmatch(rb"[0-9]{11}", key) for key in drugs)
        and all(row[:11] in drugs for row in package_rows)
    )
    if phase == "final":
        price_keys = [row[:6] for row in member_records(3)]
        valid = (
            valid and len(set(price_keys)) == len(price_keys) and set(price_keys) == set(packages)
        )
    if not valid:
        with store.transaction():
            store.add_issue(
                import_id,
                source_locator="members:lms01/lms02/lms03",
                code="registry_key_coverage",
                severity="error",
                blocking_scope="import",
            )
            store.finish_import(
                import_id, status="rejected", record_count=1, accepted_count=0, rejected_count=1
            )
        return {"import_id": import_id, "status": "rejected", "reason": "registry_key_coverage"}
    count = 0
    with store.transaction():
        con = store._connection
        con.execute(
            "INSERT INTO registry_release VALUES(?,?,?,?,?,?,?,?,?)",
            [
                import_id,
                delivery_id,
                release.effective_date,
                release.tariff_week,
                release.system_version,
                phase,
                profile,
                canonical_json(context),
                release.parameters.decode("ascii"),
            ],
        )
        for name, data in sorted(release.members.items()):
            if name == "system.txt":
                continue
            number = int(name[3:5])
            member_id = _insert_member(
                store,
                number,
                data,
                member_profile,
                final_width=final_width if number == 2 else None,
                fixed_width=LAYOUTS[number][0] if fixed_framing else None,
            )
            con.execute(
                "INSERT INTO registry_release_member VALUES(?,?,?,?)",
                [import_id, number, member_id, release.member_names[name]],
            )
            count += len(member_records(number))
        store.finish_import(
            import_id, status="accepted", record_count=count, accepted_count=count, rejected_count=0
        )
    return {
        "import_id": import_id,
        "status": "accepted",
        "source_records": count,
        "independently_verified": False,
    }


def member_rows(store: Store, member_id: int) -> list[dict[str, Any]]:
    """Ordered occurrences, including duplicates; source ordinal is list index+1."""
    store._check_owner()
    con = store._connection
    member = con.execute(
        "SELECT member_number,row_ids FROM registry_member WHERE member_id=?", [member_id]
    ).fetchone()
    if member is None:
        raise ValueError("Unknown member")
    number, ids = member
    result = con.execute(
        "SELECT r.* FROM unnest(?::BIGINT[]) WITH ORDINALITY AS u(row_id,ordinal) "
        f"JOIN registry_lms{number:02} r USING(row_id) ORDER BY u.ordinal",
        [ids],
    )
    names = [column[0] for column in result.description]
    return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def verify_registry(
    store: Store,
    import_id: str,
    *,
    recheck: bool = False,
    checked_members: set[int] | None = None,
) -> dict[str, Any]:
    """Independently verify native bytes, typed values and complete edition bindings.

    Previously verified immutable member dictionaries can be reused during one
    backfill. A final recheck or dataset readback rehashes persisted typed values.
    A cached proof never admits a different byte/profile/member binding.
    """
    from . import lms_registry_audit

    store._check_owner()
    auditor = lms_registry_audit.AUDITOR_VERSION
    con = store._connection
    result = con.execute("SELECT * FROM registry_release WHERE import_id=?", [import_id])
    names = [col[0] for col in result.description]
    values = result.fetchone()
    if values is None:
        raise RegistryError("no_registry_release")
    release = dict(zip(names, values, strict=True))
    imported = store.get_record("import_batch", import_id)
    parser_version = imported["parser_version"]
    context = json.loads(imported["interpretation_context"])
    identity_fields = {
        key: imported[key]
        for key in (
            "parser_version",
            "schema_version",
            "mapping_version",
            "interpretation_context_hash",
        )
    }
    identity_fields["delivery_id"] = str(imported["delivery_id"])
    if (
        imported["validation_status"] != "accepted"
        or str(imported["delivery_id"]) != str(release["delivery_id"])
        or parser_version not in {PROFILE, MISSING_FINAL_CRLF_PROFILE, FIXED_WIDTH_PROFILE}
        or imported["schema_version"] != "registry-1"
        or imported["mapping_version"] != "edition-local-native-keys-v1"
        or digest(context) != imported["interpretation_context_hash"]
        or identity("import", identity_fields) != str(import_id)
        or context != {**json.loads(release["context"]), "phase": release["phase"]}
        or release["profile"] != parser_version + ":system-" + release["system_version"]
    ):
        raise RegistryError("independent_import_lineage_mismatch")
    path = store.original_path(str(release["delivery_id"]))
    variant = parser_version == MISSING_FINAL_CRLF_PROFILE
    fixed_framing = parser_version == FIXED_WIDTH_PROFILE
    native = lms_registry_audit.audit_release(
        path, allow_final_record=variant, fixed_width_framing=fixed_framing
    )
    if variant and native.get("unterminated_members") != ["lms02.txt"]:
        raise RegistryError("independent_release_variant_mismatch")
    if fixed_framing and not native.get("fixed_width_members"):
        raise RegistryError("independent_release_variant_mismatch")
    if (
        native["phase"] != release["phase"]
        or native["schema_version"] != release["system_version"]
        or native["effective_token"] != release["effective_date"].strftime("%Y%m%d")
        or native["period_week_token"] != release["tariff_week"]
        or native["parameter_record"] != release["system_parameters"]
    ):
        raise RegistryError("independent_release_context_mismatch")
    bindings = con.execute(
        "SELECT b.member_number,b.member_id,b.native_name,m.member_number,m.profile,"
        "m.raw_sha256,m.row_count,m.row_ids,m.ordering_hash "
        "FROM registry_release_member b JOIN registry_member m USING(member_id) "
        "WHERE b.import_id=? ORDER BY b.member_number",
        [import_id],
    ).fetchall()
    expected_names = set(native["members"]) - {"system.txt"}
    if {row[2].lower() for row in bindings} != expected_names:
        raise RegistryError("independent_release_members_mismatch")
    expected_count = sum(row[6] for row in bindings)
    if (imported["record_count"], imported["accepted_count"], imported["rejected_count"]) != (
        expected_count,
        expected_count,
        0,
    ):
        raise RegistryError("independent_import_count_mismatch")
    verified = []
    with zipfile.ZipFile(path) as source:
        for number, member_id, name, actual_number, profile, sha, count, ids, ordering in bindings:
            expected = native["members"][name.lower()]
            if (
                number != actual_number
                or profile != PROFILE + ":system-" + release["system_version"]
                or expected["sha256"] != sha
                or expected["physical_records"] != count
                or len(ids) != count
                or digest({"row_ids": ids}) != ordering
            ):
                raise RegistryError("independent_member_binding_mismatch")
            previous = con.execute(
                "SELECT original_sha256,typed_sha256 FROM registry_member_verification "
                "WHERE member_id=? AND auditor_version=?",
                [member_id, auditor],
            ).fetchone()
            already_checked = checked_members is not None and member_id in checked_members
            if previous and (not recheck or already_checked):
                if previous[0] != sha:
                    raise RegistryError("stale_independent_member_proof")
                typed_hash = previous[1]
            else:
                proof = lms_registry_audit.audit_member(
                    number,
                    source.read(name),
                    member_rows(store, member_id),
                    allow_final_record=variant and number == 2,
                    fixed_width_framing=fixed_framing,
                )
                if not proof["complete"]:
                    raise RegistryError("independent_typed_reconciliation_failed:" + str(number))
                typed_hash = proof["actual_sha256"]
                if previous and previous[1] != typed_hash:
                    raise RegistryError("changed_independently_verified_member")
                if not previous:
                    with store.transaction():
                        con.execute(
                            "INSERT INTO registry_member_verification VALUES(?,?,?,?,?,?)",
                            [
                                member_id,
                                auditor,
                                sha,
                                typed_hash,
                                canonical_json(proof),
                                datetime.now(UTC),
                            ],
                        )
            if checked_members is not None:
                checked_members.add(member_id)
            verified.append(
                {
                    "member_number": number,
                    "member_id": member_id,
                    "raw_sha256": sha,
                    "typed_sha256": typed_hash,
                    "source_records": count,
                    "ordering_hash": ordering,
                }
            )
    proof = {
        "version": 1,
        "complete": True,
        "import_id": str(import_id),
        "auditor": auditor,
        "native": native,
        "members": verified,
        "scope": "all declared member records and typed fields",
    }
    evidence_hash = digest(proof)
    existing = con.execute(
        "SELECT evidence_hash FROM registry_release_verification "
        "WHERE import_id=? AND auditor_version=?",
        [import_id, auditor],
    ).fetchone()
    if existing and existing[0] != evidence_hash:
        raise RegistryError("changed_release_verification")
    if not existing:
        with store.transaction():
            con.execute(
                "INSERT INTO registry_release_verification VALUES(?,?,?,?,?)",
                [import_id, auditor, evidence_hash, canonical_json(proof), datetime.now(UTC)],
            )
    return {
        "complete": True,
        "import_id": import_id,
        "evidence_hash": evidence_hash,
        "source_records": sum(row["source_records"] for row in verified),
        "members": len(verified),
    }
