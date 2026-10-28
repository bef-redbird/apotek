"""Typed references from incomplete loose LMS originals, never full releases.

A derived receipt groups separately archived native components. SYSTEM supplies
native date/week tokens and expected counts; disagreement remains explicit. No
ZIP is synthesized and no partial group enters registry_release or its selection
policy. Fresh readback reconciles every available typed occurrence to its own
original, including when a member or an import already has a stored proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

from . import lms_registry_audit
from .lms_registry import RegistryError, _insert_member, member_rows
from .lms_registry_layout import LAYOUTS, PROFILE
from .records import canonical_json, digest, identity

if TYPE_CHECKING:
    from .store import Store

PARTIAL_PROFILE = "lms-native-partial-v3"
MANIFEST_SOURCE = "foundation:partial-native-manifest"
PARTIAL_ZIP_PROFILE = "lms-zip-partial-members-v2"
PRELIMINARY_PROFILE = "lms-preliminary-control-inventory-v1"
ZIP_MEMBER_SOURCE = "foundation:derived-native-zip-member"
PARTIAL_ZIP_REASONS = frozenset(
    {"system_record_count", "unsupported_member_layout", "native_record_terminator"}
)
_KIND = "derived-partial-native-registry-receipt"
_READER_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class PartialRegistryError(RegistryError):
    """An incomplete native reference cannot be reconciled safely."""


def _name(number: int) -> str:
    return "system.txt" if number == 0 else f"lms{number:02}.txt"


def partial_registry_manifest(
    member_delivery_ids: dict[int | str, str], *, phase: str, context: dict[str, Any]
) -> bytes:
    """Create a derived JSON receipt for the caller to register separately.

    Register these bytes with source=MANIFEST_SOURCE. Component keys are native
    member numbers; 0 or 'SYSTEM' identifies the mandatory SYSTEM original.
    Context must describe partial_reason; source_role is fixed to registry_partial.
    """
    if phase not in {"final", "preliminary"}:
        raise PartialRegistryError("partial_phase_required")
    if not isinstance(context.get("partial_reason"), str) or not context["partial_reason"].strip():
        raise PartialRegistryError("partial_reason_required")
    if context.get("source_role", "registry_partial") != "registry_partial":
        raise PartialRegistryError("partial_source_role")
    components = {}
    for key, delivery in member_delivery_ids.items():
        number = 0 if isinstance(key, str) and key.upper() == "SYSTEM" else key
        if (
            type(number) is not int
            or number in components
            or (number != 0 and number not in LAYOUTS)
        ):
            raise PartialRegistryError("partial_component_key")
        components[number] = str(UUID(delivery))
    if 0 not in components or len(components) < 2 or len(components) > 64:
        raise PartialRegistryError("partial_components_required")
    return (
        canonical_json(
            {
                "version": 1,
                "kind": _KIND,
                "phase": phase,
                "context": {**context, "source_role": "registry_partial"},
                "components": [
                    {"member_number": number, "source_delivery_id": components[number]}
                    for number in sorted(components)
                ],
            }
        )
        + "\n"
    ).encode("utf-8")


def _lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    if not data.endswith(b"\r\n"):
        raise PartialRegistryError("partial_native_terminator")
    result = data[:-2].split(b"\r\n")
    if any(b"\r" in row or b"\n" in row for row in result):
        raise PartialRegistryError("partial_native_terminator")
    return result


def _native_rows(number: int, data: bytes, *, fixed_framing: bool = False) -> list[bytes]:
    if not fixed_framing or number == 0:
        return _lines(data)
    width = LAYOUTS[number][0]
    rows = []
    offset = 0
    while offset < len(data):
        boundary = offset + width
        if boundary > len(data):
            raise PartialRegistryError("partial_native_width")
        rows.append(data[offset:boundary])
        if boundary == len(data):
            break
        if data[boundary : boundary + 2] != b"\r\n":
            raise PartialRegistryError("partial_native_terminator")
        offset = boundary + 2
    return rows


def _ascii(token: bytes) -> str:
    try:
        return token.decode("ascii")
    except UnicodeError as exc:
        raise PartialRegistryError("partial_system_ascii") from exc


def _integer(token: bytes) -> int:
    if not re.fullmatch(rb"[0-9]+", token):
        raise PartialRegistryError("partial_system_integer")
    return int(token)


def _native_system(data: bytes, phase: str) -> dict[str, Any]:
    """Standalone SYSTEM byte ranges from official specification rows309–338."""
    lines = _lines(data)
    if len(lines) < 3 or [len(row) for row in lines[:2]] != [93, 47]:
        raise PartialRegistryError("partial_system_layout")
    control, parameter = lines[:2]
    version = _ascii(control[2:7]).strip()
    if (
        control[:2] != b"00"
        or control[7:47] != b"LMS-TAKST".ljust(40)
        or version not in {"11.0", "12.0"}
        or control[71:73] != b"01"
        or parameter[:2] != b"01"
        # Specification row317 defines a fixed packed-name literal, not phase.
        or control[75:87].strip().lower() != b"lms.zip"
    ):
        raise PartialRegistryError("partial_system_contract")
    effective = _ascii(control[47:55])
    if not re.fullmatch(r"[0-9]{8}", effective):
        raise PartialRegistryError("partial_system_date")
    try:
        date(int(effective[:4]), int(effective[4:6]), int(effective[6:]))
    except ValueError as exc:
        raise PartialRegistryError("partial_system_date") from exc
    week = _ascii(control[87:93])
    if not re.fullmatch(r"[0-9]{6}", week):
        raise PartialRegistryError("partial_system_week")
    catalogue = {}
    for row in lines[2:]:
        if len(row) != 51 or row[:2] != b"10" or row[44:46] != b"01":
            raise PartialRegistryError("partial_system_catalogue")
        name = _ascii(row[32:44]).strip().lower()
        if name == "system.txt":
            number = 0
        elif re.fullmatch(r"lms[0-9]{2}\.txt", name) and int(name[3:5]) in LAYOUTS:
            number = int(name[3:5])
        else:
            raise PartialRegistryError("partial_system_catalogue_name")
        if name in catalogue:
            raise PartialRegistryError("partial_system_catalogue_duplicate")
        first, second = row[2:32].strip().decode("cp850"), row[2:32].strip().decode("cp865")
        catalogue[name] = {
            "member_number": number,
            "declared_records": _integer(row[46:51]),
            "description": first if first == second else None,
            "description_state": "known" if first == second else "ambiguous_encoding",
        }
    if "system.txt" not in catalogue or _integer(control[73:75]) != len(catalogue):
        raise PartialRegistryError("partial_system_catalogue_count")
    return {
        "schema_version": version,
        "effective_token": effective,
        "period_week_token": week,
        "previous_effective_token": _ascii(control[55:63]),
        "next_effective_token": _ascii(control[63:71]),
        "packed_name": _ascii(control[75:87]).strip(),
        "declared_member_count": _integer(control[73:75]),
        "parameter_record": _ascii(parameter),
        "parameter_slots": [_integer(parameter[first : first + 5]) for first in range(2, 47, 5)],
        "physical_records": len(lines),
        "catalogue": catalogue,
        "publication_status": "unknown",
    }


def _zip_parent(store: Store, delivery_id: str) -> dict[str, Any]:
    metadata = store.get_record("source_delivery", delivery_id)
    if (
        identity("delivery", {"source": metadata["source"], "receipt_key": metadata["receipt_key"]})
        != delivery_id
    ):
        raise PartialRegistryError("partial_zip_parent_identity")
    return {
        "source_delivery_id": delivery_id,
        "sha256": metadata["content_hash"],
        "metadata_hash": metadata["metadata_hash"],
        "original_uri": metadata["original_uri"],
        "received_evidence": {
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in metadata.items()
            if key.startswith("received_")
        },
    }


def _inspect_partial_zip(path: Path, phase: str) -> dict[str, Any]:
    """Independent ZIP/SYSTEM/width inspection; never invoke the release parser."""
    if phase not in {"final", "preliminary"}:
        raise PartialRegistryError("partial_phase_required")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [item.filename.lower() for item in entries]
            if (
                len(entries) > 64
                or len(names) != len(set(names))
                or sum(item.file_size for item in entries) > 128 * 1024 * 1024
                or any(
                    not re.fullmatch(r"(?:system|lms[0-9]{2})\.txt", item.filename, re.I)
                    or item.orig_filename != item.filename
                    or item.flag_bits & 1
                    or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}
                    for item in entries
                )
            ):
                raise PartialRegistryError("partial_zip_member_contract")
            members = {
                item.filename.lower(): (item.filename, archive.read(item)) for item in entries
            }
    except (zipfile.BadZipFile, RuntimeError, EOFError) as error:
        raise PartialRegistryError("partial_zip_invalid") from error
    if "system.txt" not in members:
        raise PartialRegistryError("partial_zip_missing_system")
    system = _native_system(members["system.txt"][1], phase)
    if system["previous_effective_token"].strip() or system["next_effective_token"].strip():
        raise PartialRegistryError("partial_zip_system_context")
    catalogue = system["catalogue"]
    missing = set(catalogue) - set(members)
    undeclared = set(members) - set(catalogue)
    preliminary_variant = phase == "preliminary" and (
        system["schema_version"] == "12.0"
        and missing == {"lms03.txt", "lms24.txt", "lms31.txt", "lms32.txt"}
        and undeclared == {"lms33.txt", "lms38.txt"}
    )
    if (
        phase == "preliminary"
        and not preliminary_variant
        or (missing or undeclared)
        and not preliminary_variant
        or system["physical_records"] != catalogue["system.txt"]["declared_records"]
    ):
        raise PartialRegistryError("partial_zip_system_inventory")
    valid = {0: members["system.txt"]}
    unsupported = []
    for name, (exact_name, data) in sorted(members.items()):
        number = 0 if name == "system.txt" else int(name[3:5])
        if not number:
            continue
        if number not in LAYOUTS:
            raise PartialRegistryError("partial_zip_member_contract")
        declared = catalogue.get(name)
        reason = None
        try:
            _native_rows(number, data, fixed_framing=True)
        except PartialRegistryError:
            try:
                _lines(data)
            except PartialRegistryError:
                reason = "native_record_terminator"
            else:
                reason = "unsupported_member_layout"
        if reason:
            unsupported.append(
                {
                    "member_number": number,
                    "native_name": name,
                    "exact_member_name": exact_name,
                    "reason": reason,
                    "raw_sha256": hashlib.sha256(data).hexdigest(),
                    "byte_count": len(data),
                    "declared_records": declared["declared_records"] if declared else None,
                    "physical_crlf_records": data.count(b"\r\n"),
                }
            )
        else:
            valid[number] = (exact_name, data)
    if len(valid) < 2:
        raise PartialRegistryError("partial_zip_no_supported_business_members")
    return {
        "system": system,
        "valid": valid,
        "unsupported": unsupported,
        "inventory_profile": PRELIMINARY_PROFILE if preliminary_variant else None,
    }


def ingest_partial_zip_registry(
    store: Store,
    parent_delivery_id: str,
    *,
    phase: str,
    reason: str,
    rejected_import_ids: list[str],
) -> dict[str, Any]:
    """Retain valid native members of a rejected ZIP as explicitly derived references."""
    if (
        reason
        not in PARTIAL_ZIP_REASONS
        | ({"release_phase_conflict"} if phase == "preliminary" else set())
        or not rejected_import_ids
    ):
        raise PartialRegistryError("partial_zip_rejection_required")
    parent = _zip_parent(store, parent_delivery_id)
    if (
        phase == "preliminary"
        and store.get_record("source_delivery", parent_delivery_id)["phase"] != phase
    ):
        raise PartialRegistryError("partial_zip_parent_phase")
    for import_id in rejected_import_ids:
        rejected = store.get_record("import_batch", import_id)
        if (
            str(rejected["delivery_id"]) != parent_delivery_id
            or rejected["validation_status"] != "rejected"
        ):
            raise PartialRegistryError("partial_zip_rejection_lineage")
    inspected = _inspect_partial_zip(store.original_path(parent_delivery_id), phase)
    if not any(
        issue["code"] == reason
        for import_id in rejected_import_ids
        for issue in store.issues(import_id)
    ):
        raise PartialRegistryError("partial_zip_rejection_reason")
    if not parent["original_uri"]:
        raise PartialRegistryError("partial_zip_parent_uri")
    staging = store.archive_root / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="partial-zip-", dir=staging) as temporary:
        components: dict[int | str, str] = {}
        children = []
        for number, (exact_name, data) in sorted(inspected["valid"].items()):
            path = Path(temporary) / exact_name
            path.write_bytes(data)
            lineage = {
                "interpreter": PARTIAL_ZIP_PROFILE,
                "parent": parent,
                "exact_member_name": exact_name,
            }
            delivery = store.register_source(
                path,
                source=ZIP_MEMBER_SOURCE,
                receipt_key=PARTIAL_ZIP_PROFILE + ":" + digest(lineage),
                original_uri=parent["original_uri"] + "!/" + exact_name,
                phase=phase,
                edition="derived-from-sha256:" + parent["sha256"],
                coverage={
                    "version": 1,
                    "kind": "derived_member",
                    "source_role": "registry_partial",
                    **lineage,
                },
            )
            components[number] = delivery
            metadata = store.get_record("source_delivery", delivery)
            children.append(
                {
                    "member_number": number,
                    "source_delivery_id": delivery,
                    "exact_member_name": exact_name,
                    "sha256": metadata["content_hash"],
                    "metadata_hash": metadata["metadata_hash"],
                }
            )
        context = {
            "partial_reason": "Rejected native ZIP: " + reason,
            "source_role": "registry_partial",
            "publication_evidence": "unknown",
            "partial_zip_interpreter": PARTIAL_ZIP_PROFILE,
            "parent_zip": parent,
            "derived_children": children,
            "unsupported_members": inspected["unsupported"],
            "rejected_import_ids": rejected_import_ids,
            "rejection_reason": reason,
            **(
                {"inventory_profile": inspected["inventory_profile"]}
                if inspected["inventory_profile"]
                else {}
            ),
        }
        source = partial_registry_manifest(components, phase=phase, context=context)
        path = Path(temporary) / "partial-native-manifest.json"
        path.write_bytes(source)
        delivery = store.register_source(
            path,
            source=MANIFEST_SOURCE,
            receipt_key=PARTIAL_ZIP_PROFILE + ":" + hashlib.sha256(source).hexdigest(),
            coverage={"version": 1, "kind": "derived_receipt", "source_role": "registry_partial"},
        )
    return {
        **ingest_partial_registry(store, delivery),
        "manifest_delivery_id": delivery,
        "parent_delivery_id": parent_delivery_id,
        "partial_interpreter_version": PARTIAL_ZIP_PROFILE,
        "source_role": "registry_partial",
        "partial_parser_version": PARTIAL_PROFILE,
        "unsupported_members": inspected["unsupported"],
    }


def _verify_zip_children(store: Store, manifest: dict[str, Any], sources: dict[int, Any]) -> None:
    context = manifest["context"]
    parent = context["parent_zip"]
    parent_id = parent["source_delivery_id"]
    if (
        context["partial_zip_interpreter"] != PARTIAL_ZIP_PROFILE
        or _zip_parent(store, parent_id) != parent
    ):
        raise PartialRegistryError("partial_zip_parent_lineage")
    # Re-open and checksum the parent and every ZIP member on each verification.
    inspected = _inspect_partial_zip(store.original_path(parent_id), manifest["phase"])
    if (
        manifest["phase"] == "preliminary"
        and store.get_record("source_delivery", parent_id)["phase"] != manifest["phase"]
        or inspected["inventory_profile"] != context.get("inventory_profile")
    ):
        raise PartialRegistryError("partial_zip_parent_phase")
    if inspected["unsupported"] != context["unsupported_members"] or set(inspected["valid"]) != set(
        sources
    ):
        raise PartialRegistryError("partial_zip_member_inventory")
    children = context["derived_children"]
    if len(children) != len(sources) or {item["member_number"] for item in children} != set(
        sources
    ):
        raise PartialRegistryError("partial_zip_child_mapping")
    for item in children:
        number = item["member_number"]
        source = sources[number]
        exact_name, native = inspected["valid"][number]
        metadata = store.get_record("source_delivery", source["source_delivery_id"])
        lineage = {
            "interpreter": PARTIAL_ZIP_PROFILE,
            "parent": parent,
            "exact_member_name": exact_name,
        }
        coverage = {
            "version": 1,
            "kind": "derived_member",
            "source_role": "registry_partial",
            **lineage,
        }
        if (
            source["data"] != native
            or item
            != {
                "member_number": number,
                "source_delivery_id": source["source_delivery_id"],
                "exact_member_name": exact_name,
                "sha256": hashlib.sha256(native).hexdigest(),
                "metadata_hash": metadata["metadata_hash"],
            }
            or metadata["source"] != ZIP_MEMBER_SOURCE
            or metadata["receipt_key"] != PARTIAL_ZIP_PROFILE + ":" + digest(lineage)
            or metadata["original_uri"] != parent["original_uri"] + "!/" + exact_name
            or metadata["phase"] != manifest["phase"]
            or metadata["edition"] != "derived-from-sha256:" + parent["sha256"]
            or metadata["received_precision"] != "unknown"
            or metadata["published_precision"] != "unknown"
            or json.loads(metadata["coverage"]) != coverage
        ):
            raise PartialRegistryError("partial_zip_child_lineage")
    reasons = PARTIAL_ZIP_REASONS | (
        {"release_phase_conflict"} if manifest["phase"] == "preliminary" else set()
    )
    if context["rejection_reason"] not in reasons or not any(
        issue["code"] == context["rejection_reason"]
        for import_id in context["rejected_import_ids"]
        for issue in store.issues(import_id)
    ):
        raise PartialRegistryError("partial_zip_rejection_reason")
    for import_id in context["rejected_import_ids"]:
        rejected = store.get_record("import_batch", import_id)
        if str(rejected["delivery_id"]) != parent_id or rejected["validation_status"] != "rejected":
            raise PartialRegistryError("partial_zip_rejection_lineage")


def _inputs(store: Store, manifest_delivery_id: str) -> dict[str, Any]:
    receipt = store.get_record("source_delivery", manifest_delivery_id)
    if receipt["source"] != MANIFEST_SOURCE or receipt["published_precision"] != "unknown":
        raise PartialRegistryError("partial_derived_receipt_source")
    raw = store.original_path(manifest_delivery_id).read_bytes()
    if len(raw) > 1024 * 1024:
        raise PartialRegistryError("partial_manifest_limit")
    try:
        manifest = json.loads(raw)
        components = manifest["components"]
        mapping = {item["member_number"]: item["source_delivery_id"] for item in components}
        expected = partial_registry_manifest(
            mapping, phase=manifest["phase"], context=manifest["context"]
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise PartialRegistryError("partial_manifest_contract") from exc
    if len(mapping) != len(components) or raw != expected or manifest["kind"] != _KIND:
        raise PartialRegistryError("partial_manifest_contract")
    sources = {}
    total_bytes = 0
    for number, delivery in sorted(mapping.items()):
        metadata = store.get_record("source_delivery", delivery)
        if (
            identity(
                "delivery", {"source": metadata["source"], "receipt_key": metadata["receipt_key"]}
            )
            != delivery
        ):
            raise PartialRegistryError("partial_component_delivery_identity")
        uri = urlsplit(metadata["original_uri"] or "")
        path = unquote(uri.path)
        if path.rsplit("/", 1)[-1].lower() != _name(number):
            raise PartialRegistryError("partial_native_uri_member")
        total_bytes += metadata["byte_count"]
        if total_bytes > 256 * 1024 * 1024:
            raise PartialRegistryError("partial_native_limit")
        data = store.original_path(delivery).read_bytes()
        sources[number] = {
            "source_delivery_id": delivery,
            "data": data,
            "raw_sha256": metadata["content_hash"],
            "metadata_hash": metadata["metadata_hash"],
            "original_uri": metadata["original_uri"],
            "directory": (uri.scheme, uri.netloc, path.rsplit("/", 1)[0]),
            "fixed_framing": "partial_zip_interpreter" in manifest["context"],
        }
    if any(source["directory"] != sources[0]["directory"] for source in sources.values()):
        raise PartialRegistryError("partial_native_directory_conflict")
    if "partial_zip_interpreter" in manifest["context"]:
        _verify_zip_children(store, manifest, sources)
    elif any(
        store.get_record("source_delivery", source["source_delivery_id"])["source"]
        == ZIP_MEMBER_SOURCE
        for source in sources.values()
    ):
        raise PartialRegistryError("partial_zip_lineage_required")
    system = _native_system(sources[0]["data"], manifest["phase"])
    coverage = []
    unsupported = {
        row["member_number"]: row for row in manifest["context"].get("unsupported_members", [])
    }
    catalogue = system["catalogue"]
    preliminary_variant = manifest["context"].get("inventory_profile") == PRELIMINARY_PROFILE
    if preliminary_variant and (
        manifest["phase"] != "preliminary"
        or manifest["context"].get("partial_zip_interpreter") != PARTIAL_ZIP_PROFILE
    ):
        raise PartialRegistryError("partial_preliminary_inventory_lineage_required")
    actual_names = {_name(number) for number in sources} | {_name(number) for number in unsupported}
    inventory = set(catalogue) | (actual_names if preliminary_variant else set())
    for name in sorted(inventory):
        declared = catalogue.get(name)
        number = 0 if name == "system.txt" else int(name[3:5])
        source = sources.get(number)
        actual = (
            len(_native_rows(number, source["data"], fixed_framing=source["fixed_framing"]))
            if source is not None
            else None
        )
        status = (
            "missing"
            if source is None
            else "system"
            if number == 0
            else "empty_unavailable"
            if actual == 0 and declared is not None and declared["declared_records"] > 0
            else "available_empty"
            if actual == 0
            else "available"
        )
        if number in unsupported:
            status = "unsupported"
        coverage.append(
            {
                "member_number": number,
                "native_name": name,
                "declared_records": declared["declared_records"] if declared else None,
                "available_records": actual,
                "status": status,
                "count_matches": actual == declared["declared_records"] if declared else None,
                **({"declaration_state": "undeclared"} if declared is None else {}),
                **(
                    {
                        "unsupported_reason": unsupported[number]["reason"],
                        "raw_sha256": unsupported[number]["raw_sha256"],
                        "byte_count": unsupported[number]["byte_count"],
                    }
                    if number in unsupported
                    else {}
                ),
            }
        )
    if set(sources) - {row["member_number"] for row in coverage}:
        raise PartialRegistryError("partial_undeclared_component")
    return {
        "manifest": manifest,
        "manifest_sha256": receipt["content_hash"],
        "sources": sources,
        "system": system,
        "coverage": {
            "complete_registry": False,
            "canonical_eligible": False,
            "members": coverage,
            "publication_status": "unknown",
            **(
                {
                    "inventory_profile": PRELIMINARY_PROFILE,
                    "control_inventory": {
                        "state": "unresolved_native_inventory_disagreement",
                        "declared_member_count": system["declared_member_count"],
                        "actual_member_count": len(actual_names),
                        "declared_absent_members": sorted(set(catalogue) - actual_names),
                        "actual_undeclared_members": sorted(actual_names - set(catalogue)),
                        "control_mapping": "unestablished; no inferred substitutions",
                    },
                }
                if preliminary_variant
                else {}
            ),
        },
        "profile": PROFILE + ":system-" + system["schema_version"],
    }


def _member_proof(
    store: Store, number: int, source: dict[str, Any], member_id: int, profile: str
) -> dict[str, Any]:
    metadata = store._connection.execute(
        "SELECT member_number,profile,raw_sha256,row_count,row_ids,ordering_hash "
        "FROM registry_member WHERE member_id=?",
        [member_id],
    ).fetchone()
    if metadata is None:
        raise PartialRegistryError("partial_member_missing")
    actual_number, actual_profile, sha, count, ids, ordering = metadata
    if (
        actual_number != number
        or actual_profile != profile
        or sha != source["raw_sha256"]
        or count != len(_native_rows(number, source["data"], fixed_framing=source["fixed_framing"]))
        or len(ids) != count
        or digest({"row_ids": ids}) != ordering
    ):
        raise PartialRegistryError("partial_member_binding")
    rows = member_rows(store, member_id)
    if any(row["profile"] != profile for row in rows):
        raise PartialRegistryError("partial_row_profile")
    report = lms_registry_audit.audit_member(
        number, source["data"], rows, fixed_width_framing=source["fixed_framing"]
    )
    if not report["complete"]:
        raise PartialRegistryError("partial_typed_reconciliation:" + str(number))
    return {
        "member_id": member_id,
        "typed_sha256": report["actual_sha256"],
        "ordering_hash": ordering,
        "audited_fields": report["counts"].get("compared_fields", 0),
    }


def _report(
    store: Store,
    import_id: str,
    manifest_delivery_id: str,
    bundle: dict[str, Any],
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_coverage = {item["member_number"]: item for item in bundle["coverage"]["members"]}
    expected_members = []
    if {row["member_number"] for row in bindings} != set(bundle["sources"]):
        raise PartialRegistryError("partial_component_binding_set")
    for binding in bindings:
        number = binding["member_number"]
        source = bundle["sources"][number]
        coverage = expected_coverage[number]
        if any(
            binding[key] != coverage[key]
            for key in ("native_name", "declared_records", "available_records", "status")
        ) or (
            str(binding["source_delivery_id"]) != source["source_delivery_id"]
            or binding["raw_sha256"] != source["raw_sha256"]
        ):
            raise PartialRegistryError("partial_component_binding")
        typed = {}
        if number and coverage["status"] != "empty_unavailable":
            if binding["member_id"] is None:
                raise PartialRegistryError("partial_member_binding")
            typed = _member_proof(store, number, source, binding["member_id"], bundle["profile"])
        elif binding["member_id"] is not None:
            raise PartialRegistryError("partial_unavailable_member_binding")
        expected_members.append(
            {
                **coverage,
                "source_delivery_id": source["source_delivery_id"],
                "raw_sha256": source["raw_sha256"],
                "metadata_hash": source["metadata_hash"],
                "original_uri": source["original_uri"],
                **typed,
            }
        )
    return {
        "version": 1,
        "scope": "partial_native_reference",
        "complete_registry": False,
        "canonical_eligible": False,
        "publication_status": "unknown",
        "import_id": str(import_id),
        "manifest_delivery_id": str(manifest_delivery_id),
        "manifest_sha256": bundle["manifest_sha256"],
        "system": bundle["system"],
        "coverage": bundle["coverage"],
        "members": expected_members,
        "auditor": lms_registry_audit.AUDITOR_VERSION,
        "partial_reader": _READER_VERSION,
    }


def _read_group(store: Store, import_id: str) -> dict[str, Any]:
    cursor = store._connection.execute(
        "SELECT * FROM registry_partial_group WHERE import_id=?", [import_id]
    )
    row = cursor.fetchone()
    if row is None:
        raise PartialRegistryError("partial_group_missing")
    return dict(zip([column[0] for column in cursor.description], row, strict=True))


def verify_partial_registry(store: Store, import_id: str) -> dict[str, Any]:
    """Fresh original/typed reconciliation; cached reports never substitute it."""
    store._check_owner()
    if store.get_record("import_batch", import_id)["parser_version"] != PARTIAL_PROFILE:
        raise PartialRegistryError("partial_historical_reader_required")
    group = _read_group(store, import_id)
    delivery_id = str(group["manifest_delivery_id"])
    bundle = _inputs(store, delivery_id)
    imported = store.get_record("import_batch", import_id)
    context = {**bundle["manifest"]["context"], "phase": bundle["manifest"]["phase"]}
    lineage = {
        "delivery_id": delivery_id,
        "parser_version": PARTIAL_PROFILE,
        "schema_version": "partial-registry-1",
        "mapping_version": "loose-native-members-v1",
        "interpretation_context_hash": digest(context),
    }
    if any(str(imported[key]) != value for key, value in lineage.items()) or (
        identity("import", lineage) != str(import_id)
        or imported["validation_status"] != "accepted"
        or json.loads(imported["interpretation_context"]) != context
        or str(group["system_delivery_id"]) != bundle["sources"][0]["source_delivery_id"]
        or group["effective_date"].strftime("%Y%m%d") != bundle["system"]["effective_token"]
        or group["tariff_week"] != bundle["system"]["period_week_token"]
        or group["system_version"] != bundle["system"]["schema_version"]
        or group["phase"] != bundle["manifest"]["phase"]
        or group["profile"] != bundle["profile"]
        or json.loads(group["context"]) != bundle["manifest"]["context"]
        or json.loads(group["system_data"]) != bundle["system"]
        or json.loads(group["coverage"]) != bundle["coverage"]
        or group["source_role"] != "registry_partial"
        or group["canonical_eligible"] is not False
    ):
        raise PartialRegistryError("partial_group_lineage")
    cursor = store._connection.execute(
        "SELECT * FROM registry_partial_member WHERE import_id=? ORDER BY member_number",
        [import_id],
    )
    names = [column[0] for column in cursor.description]
    bindings = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    proof = _report(store, import_id, delivery_id, bundle, bindings)
    count = sum(row["available_records"] for row in bindings)
    if (imported["record_count"], imported["accepted_count"], imported["rejected_count"]) != (
        count,
        count,
        0,
    ):
        raise PartialRegistryError("partial_import_counts")
    if digest(proof) != group["evidence_hash"] or json.loads(group["verification_report"]) != proof:
        raise PartialRegistryError("partial_cached_proof_changed")
    return {
        "import_id": str(import_id),
        "status": "accepted",
        "source_records": count,
        "complete_registry": False,
        "canonical_eligible": False,
        "independently_verified": True,
        "coverage": bundle["coverage"],
        "evidence_hash": group["evidence_hash"],
    }


def ingest_partial_registry(store: Store, manifest_delivery_id: str) -> dict[str, Any]:
    """Import a registered derived receipt; retain incomplete native references."""
    store._check_owner()
    bundle = _inputs(store, manifest_delivery_id)
    manifest = bundle["manifest"]
    import_id = store.begin_import(
        manifest_delivery_id,
        parser_version=PARTIAL_PROFILE,
        schema_version="partial-registry-1",
        mapping_version="loose-native-members-v1",
        context={**manifest["context"], "phase": manifest["phase"]},
    )
    previous = store.get_record("import_batch", import_id)
    if previous["validation_status"] == "accepted":
        return {**verify_partial_registry(store, import_id), "retry": True}
    if previous["validation_status"] != "running":
        raise PartialRegistryError("partial_completed_import")
    bindings = []
    coverage = {row["member_number"]: row for row in bundle["coverage"]["members"]}
    with store.transaction():
        for number, source in sorted(bundle["sources"].items()):
            member_id = None
            if number and coverage[number]["status"] != "empty_unavailable":
                if source["fixed_framing"]:
                    member_id = _insert_member(
                        store,
                        number,
                        source["data"],
                        bundle["profile"],
                        fixed_width=LAYOUTS[number][0],
                    )
                else:
                    member_id = _insert_member(store, number, source["data"], bundle["profile"])
            bindings.append(
                {
                    **coverage[number],
                    "member_id": member_id,
                    "source_delivery_id": source["source_delivery_id"],
                    "raw_sha256": source["raw_sha256"],
                }
            )
        proof = _report(store, import_id, manifest_delivery_id, bundle, bindings)
        system = bundle["system"]
        store._connection.execute(
            "INSERT INTO registry_partial_group VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                import_id,
                manifest_delivery_id,
                bundle["sources"][0]["source_delivery_id"],
                date(
                    int(system["effective_token"][:4]),
                    int(system["effective_token"][4:6]),
                    int(system["effective_token"][6:]),
                ),
                system["period_week_token"],
                system["schema_version"],
                manifest["phase"],
                bundle["profile"],
                canonical_json(manifest["context"]),
                canonical_json(system),
                canonical_json(bundle["coverage"]),
                canonical_json(proof),
                digest(proof),
                "registry_partial",
                False,
            ],
        )
        for binding in bindings:
            store._connection.execute(
                "INSERT INTO registry_partial_member VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    import_id,
                    binding["member_number"],
                    binding["member_id"],
                    binding["source_delivery_id"],
                    binding["native_name"],
                    binding["declared_records"],
                    binding["available_records"],
                    binding["raw_sha256"],
                    binding["status"],
                ],
            )
        count = sum(row["available_records"] for row in bindings)
        store.finish_import(
            import_id, status="accepted", record_count=count, accepted_count=count, rejected_count=0
        )
    return {
        "import_id": import_id,
        "status": "accepted",
        "source_records": count,
        "complete_registry": False,
        "canonical_eligible": False,
        "independently_verified": True,
        "coverage": bundle["coverage"],
        "evidence_hash": digest(proof),
    }


def partial_reference_member(store: Store, import_id: str, number: int) -> dict[str, Any]:
    """Explicit reference lookup; missing prices return an empty list plus a gap."""
    verified = verify_partial_registry(store, import_id)
    cursor = store._connection.execute(
        "SELECT member_id,source_delivery_id,status FROM registry_partial_member "
        "WHERE import_id=? AND member_number=?",
        [import_id, number],
    )
    binding = cursor.fetchone()
    if binding is None:
        gap: dict[str, Any] = next(
            (row for row in verified["coverage"]["members"] if row["member_number"] == number), {}
        )
        return {
            "import_id": import_id,
            "member_number": number,
            "status": gap.get("status", "missing"),
            "unsupported_reason": gap.get("unsupported_reason"),
            "rows": [],
            "canonical_eligible": False,
            "coverage": verified["coverage"],
        }
    member_id, delivery, status = binding
    return {
        "import_id": import_id,
        "member_number": number,
        "status": status,
        "source_delivery_id": str(delivery),
        "canonical_eligible": False,
        "rows": [] if member_id is None else member_rows(store, member_id),
        "system": json.loads(_read_group(store, import_id)["system_data"]) if number == 0 else None,
        "coverage": verified["coverage"],
    }
