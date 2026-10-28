"""One resumable local path: discover, acquire, archive, interpret and reconcile.

Publication is an explicit versioned dataset operation. No model execution is
reachable from this module. Detailed source receipts remain in private storage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
import traceback
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bef_redbird.imports import _xlsx

from . import ftp_acquisition as acquisition
from .archive import archive_path, external_path, verify
from .ftp_supplement_store import ingest_supplement
from .lms_registry import (
    FIXED_WIDTH_PROFILE,
    MISSING_FINAL_CRLF_PROFILE,
    ingest_registry,
    verify_registry,
)
from .lms_registry_partial import (
    MANIFEST_SOURCE,
    PARTIAL_PROFILE,
    PARTIAL_ZIP_PROFILE,
    PARTIAL_ZIP_REASONS,
    PartialRegistryError,
    ingest_partial_registry,
    ingest_partial_zip_registry,
    partial_registry_manifest,
)
from .locking import writer_lock
from .records import Timing, canonical_json, digest
from .store import Store


def _variant_pending(previous: dict[str, Any]) -> bool:
    return (
        previous.get("action") == "registry"
        and previous.get("status") == "unsupported"
        and previous.get("reason") in PARTIAL_ZIP_REASONS
        and previous.get("format_variant_version") != FIXED_WIDTH_PROFILE
    )


def _partial_zip_pending(previous: dict[str, Any]) -> bool:
    return (
        previous.get("action") in {"registry", "partial_zip"}
        and (
            previous.get("status") == "unsupported"
            and previous.get("reason") in PARTIAL_ZIP_REASONS
            or previous.get("action") == "partial_zip"
            and previous.get("status") == "independently_verified"
        )
        and not _variant_pending(previous)
        and previous.get("partial_interpreter_version") != PARTIAL_ZIP_PROFILE
    )


def _preliminary_pending(previous: dict[str, Any], remote: str) -> bool:
    return (
        "FOREL" in remote.upper()
        and previous.get("action") == "registry"
        and previous.get("status") == "unsupported"
        and previous.get("reason") == "release_phase_conflict"
        and previous.get("partial_interpreter_version") != PARTIAL_ZIP_PROFILE
    )


def _interpretation_pending(previous: dict[str, Any], remote: str) -> bool:
    return (
        _variant_pending(previous)
        or _partial_zip_pending(previous)
        or _preliminary_pending(previous, remote)
    )


def _prior_partial(prior: dict[str, Any], current_id: str) -> list[dict[str, Any]]:
    history = list(prior.get("prior_interpretations", []))
    if prior.get("import_id") and prior["import_id"] != current_id:
        entry = {
            "import_id": prior["import_id"],
            "evidence_hash": prior.get("evidence_hash"),
            "partial_interpreter_version": prior.get("partial_interpreter_version"),
        }
        if entry not in history:
            history.append(entry)
    return history


def _test_directory(directory: str) -> bool:
    return bool({"TEST", "LSC-TEST"} & set(directory.upper().split("/")))


def disposition(remote: str, directories_with_zip: set[str]) -> tuple[str, str]:
    """Transport choice is distinct from verified byte equivalence."""
    directory, name = remote.rsplit("/", 1)
    upper = remote.upper()
    if _test_directory(directory):
        return "retain", "explicit source test area; not production market evidence"
    if name.upper() == "WS_FTP.LOG":
        return "retain", "server operational transfer log; no market interpretation"
    if "/APOLABELTEKST/" in upper:
        return "retain", (
            "dispensing-label documentation; retained as source guidance"
            if name.lower() == "vejledning.txt"
            else "latest-only generic dispensing-label text; guide excludes medicine identity, "
            "strength and form; outside market observations"
        )
    if "/DOS1/" in upper:
        return (
            "retain",
            "clinical dosage XML templates and supporting documentation; "
            "outside market data foundation",
        )
    if "FOREL" in upper and re.fullmatch(r"(?:form|pak|slet)[0-9][0-9-]*\.xlsx", name, re.I):
        return (
            "preliminary_discontinued"
            if name.lower().startswith("slet")
            else "preliminary_introductions",
            "preliminary production source report; exact native report schema and dates required",
        )
    if name.lower().endswith(".zip"):
        if _test_directory(directory) or "/DOS1/" in upper:
            return "retain", "test or dosage distribution; outside supported market registry"
        return "registry", "native packaged edition; explicit phase requires source corroboration"
    if (
        re.fullmatch(r"(?:lms[0-9]{2}|system)\.txt", name, re.I)
        and directory in directories_with_zip
    ):
        return (
            "packaged",
            "business member acquired within native ZIP; "
            "loose transport bytes not independently claimed equal",
        )
    if "/DOK/" in upper and name.lower().endswith(".txt"):
        return "DOK", "conditional reimbursement exceptions; not wholesaler supply reports"
    if name.upper() in {"LMS36.CSV", "LMS37.CSV"}:
        return name.upper()[
            :-4
        ], "source-reported extended prescribing references; not supply status"
    if (
        "/INFO/" in upper
        or name.upper() in {"DFDGINST.TXT", "SYSKRAV4.TXT"}
        or re.fullmatch(r"INFO[0-9]{8}\.TXT", name, re.I)
    ):
        return (
            "retain",
            "source schema, encoding or release documentation; "
            "retained as evidence, no market observations",
        )
    return (
        "retain",
        "documentation, preliminary ancillary, label, dosage or test evidence "
        "retained outside default market facts",
    )


def _partial_directories(
    files: list[dict[str, Any]],
    rejected_containers: set[str] | None = None,
    alternative_scopes: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Incomplete loose groups and separately pinned alternative transport scopes."""
    rejected = (
        rejected_containers
        if rejected_containers is not None
        else {item["remote"] for item in files if item.get("container_failure") == "invalid_zip"}
    )
    directories: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        directory = item["remote"].rsplit("/", 1)[0]
        directories.setdefault(directory, []).append(item)
    groups: dict[str, dict[str, Any]] = {}
    for directory, members in directories.items():
        if _test_directory(directory) or any(
            token in directory.upper() for token in ("/DOS1", "/INFO", "/DOK")
        ):
            continue
        containers = [
            item
            for item in members
            if item["remote"].rsplit("/", 1)[1].lower() in {"lms.zip", "lms-gros.zip"}
        ]
        if any(item["size"] > 0 and item["remote"] not in rejected for item in containers):
            continue
        components = [
            item
            for item in members
            if re.fullmatch(r"(?:system|lms[0-9]{2})\.txt", item["remote"].rsplit("/", 1)[1], re.I)
        ]
        if len(components) > 1 and any(
            item["remote"].lower().endswith("/system.txt") for item in components
        ):
            groups[directory] = {
                "components": sorted(components, key=lambda item: item["remote"]),
                "containers": containers,
            }
    indexed = {item["remote"]: item for item in files}
    for scope in alternative_scopes or []:
        directory = scope["directory"]
        if directory in groups:
            raise ValueError("Alternative transport cannot replace an ordinary partial group")
        components = []
        for component in scope["components"]:
            if component["remote"] not in indexed:
                raise ValueError("Reviewed alternative component is not inventoried")
            components.append(indexed[component["remote"]])
        if scope.get("interpretation") == "raw_only":
            continue
        groups[directory] = {
            "components": sorted(components, key=lambda item: item["remote"]),
            "containers": [],
            "alternative_transport": scope,
        }
    return groups


def plan(
    config: acquisition.AcquisitionConfig,
    *,
    alternative_scopes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    files = acquisition.selected_files(config)
    retained = acquisition.read_state(config)
    work = read_journal(config)
    rejected = set()
    for item in files:
        if (
            item["remote"].rsplit("/", 1)[1].lower() not in {"lms.zip", "lms-gros.zip"}
            or _component_receipt(item, retained) is None
        ):
            continue
        entry = retained["files"].get(item["remote"])
        key = item["remote"] + "#" + entry["current_sha256"] if entry else ""
        checkpoint = work["files"].get(key, {})
        if checkpoint.get("container_failure") == "invalid_zip" or (
            checkpoint.get("status") == "unsupported" and checkpoint.get("reason") == "invalid_zip"
        ):
            rejected.add(item["remote"])
    indexed = {item["remote"]: item for item in files}
    raw_components = {}
    for scope in alternative_scopes or []:
        for component in scope["components"]:
            if component["remote"] not in indexed:
                raise ValueError("Reviewed alternative component is not inventoried")
            entry = retained["files"].get(component["remote"])
            if entry and entry["current_sha256"] != component["sha256"]:
                raise ValueError("Acquired alternative bytes differ from the reviewed SHA256")
            if scope.get("interpretation") == "raw_only":
                raw_components[component["remote"]] = scope
                item = indexed[component["remote"]]
                if _component_receipt(item, retained) is not None:
                    verify(
                        archive_path(config.archive_root, component["sha256"]),
                        component["sha256"],
                        item["size"],
                    )
    partials = _partial_directories(files, rejected, alternative_scopes)
    zipped = {
        x["remote"].rsplit("/", 1)[0]
        for x in files
        if x["remote"].rsplit("/", 1)[1].lower() in {"lms.zip", "lms-gros.zip"}
        and x["size"] > 0
        and x["remote"] not in rejected
    }
    result = []
    for item in files:
        directory, name = item["remote"].rsplit("/", 1)
        action, reason = disposition(item["remote"], zipped)
        entry = retained["files"].get(item["remote"])
        checkpoint = (
            work["files"].get(item["remote"] + "#" + entry["current_sha256"], {}) if entry else {}
        )
        if action == "registry" and (
            _partial_zip_pending(checkpoint)
            or checkpoint.get("action") == "partial_zip"
            or _preliminary_pending(checkpoint, item["remote"])
        ):
            action, reason = (
                "partial_zip",
                "valid members derived from a rejected native ZIP; no full replacement",
            )
        if directory in partials:
            if item in partials[directory]["components"]:
                action, reason = (
                    "partial_component",
                    "loose partial native reference; requires separate group reconciliation",
                )
            elif "alternative_transport" not in partials[directory] and name.lower() in {
                "lms.zip",
                "lms-gros.zip",
            }:
                action, reason = (
                    "partial_container",
                    "corrupt native ZIP retained as a source gap; no complete release"
                    if item["remote"] in rejected
                    else "empty native ZIP retained as a source gap; no complete release",
                )
        raw_scope = raw_components.get(item["remote"])
        if raw_scope:
            action, reason = "retain", raw_scope["reason"]
        result.append(
            {
                **item,
                "action": action,
                "reason": reason,
                **(
                    {
                        "alternative_transport": raw_scope,
                        "phase": raw_scope["phase"],
                        "source_role": "raw_source",
                        "canonical_eligible": False,
                        "complete_registry": False,
                    }
                    if raw_scope
                    else {}
                ),
                **(
                    {
                        "container_failure": "invalid_zip",
                        "container_failure_sha256": retained["files"][item["remote"]][
                            "current_sha256"
                        ],
                    }
                    if item["remote"] in rejected
                    else {}
                ),
            }
        )
    return result


def _component_receipt(item: dict[str, Any], retained: dict[str, Any]) -> dict[str, Any] | None:
    entry = retained["files"].get(item["remote"])
    if not entry or (
        entry.get("inventory_size") != item["size"]
        or not acquisition.same_modification(
            entry.get("inventory_modification"), item["server_modification"]
        )
    ):
        return None
    return next(
        (
            receipt
            for receipt in entry.get("receipts", [])
            if receipt.get("sha256") == entry.get("current_sha256")
            and receipt.get("remote") == item["remote"]
        ),
        None,
    )


def _partial_key(directory: str, group: dict[str, Any], retained: dict[str, Any]) -> str:
    return (
        directory
        + "#"
        + digest(
            {
                "components": [
                    {
                        "remote": item["remote"],
                        "size": item["size"],
                        "server_modification": item["server_modification"],
                        "sha256": receipt["sha256"]
                        if (receipt := _component_receipt(item, retained))
                        else None,
                    }
                    for item in group["components"]
                ],
                "containers": group["containers"],
                **(
                    {"alternative_transport": group["alternative_transport"]}
                    if "alternative_transport" in group
                    else {}
                ),
            }
        )
    )


def _alternative_review(path: Path, binding: Any) -> list[dict[str, Any]]:
    """Read exact, source-only scopes; the config pins the review and its evidence."""
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise ValueError("Alternative transport review requires path and SHA256")

    def reviewed_file(value: dict[str, Any]) -> tuple[Path, str]:
        relative, expected = value.get("path"), value.get("sha256")
        if (
            not isinstance(relative, str)
            or any(char in relative for char in "\\:")
            or not relative.startswith("source-review/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            raise ValueError("Invalid alternative transport review path or SHA256")
        config_root = external_path(path).parent
        root = config_root / "source-review"
        candidate = config_root / relative
        if any(
            part.is_symlink() or part.is_junction()
            for part in (candidate, *candidate.parents)
            if part.is_relative_to(config_root)
        ):
            raise ValueError("Linked alternative transport evidence is forbidden")
        target = external_path(candidate)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("Alternative transport evidence escapes source-review or is missing")
        if target.stat().st_size > 1024 * 1024:
            raise ValueError("Alternative transport review exceeds size bound")
        raw = target.read_bytes()
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Alternative transport review bytes differ from their SHA256")
        return target, raw.decode("utf-8-sig")

    _review_path, text = reviewed_file(binding)
    document = json.loads(text)
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "scopes"}
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise ValueError("Unsupported alternative transport review")
    scopes = document["scopes"]
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 32:
        raise ValueError("Alternative transport review requires bounded explicit scopes")
    seen = set()
    result = []
    for scope in scopes:
        required = {
            "directory",
            "phase",
            "reason",
            "evidence",
            "components",
        }
        if (
            not isinstance(scope, dict)
            or not required <= set(scope)
            or set(scope) - required - {"interpretation"}
            or ("interpretation" in scope and scope["interpretation"] != "raw_only")
        ):
            raise ValueError("Invalid alternative transport scope")
        directory = acquisition.remote_path(scope["directory"])
        if (
            directory in seen
            or _test_directory(directory)
            or any(token in directory.upper() for token in ("/DOS1", "/INFO", "/DOK"))
        ):
            raise ValueError("Invalid alternative transport directory")
        seen.add(directory)
        if (
            scope["phase"] not in {"final", "preliminary"}
            or not isinstance(scope["reason"], str)
            or not scope["reason"].strip()
        ):
            raise ValueError("Alternative transport phase and reason are required")
        evidence = scope["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}:
            raise ValueError("Alternative transport evidence requires path and SHA256")
        reviewed_file(evidence)
        components = scope["components"]
        if not isinstance(components, list) or not 2 <= len(components) <= 37:
            raise ValueError("Alternative transport requires SYSTEM and native components")
        names = set()
        for component in components:
            if not isinstance(component, dict) or set(component) != {"remote", "sha256"}:
                raise ValueError("Alternative transport components require exact pins")
            remote = acquisition.remote_path(component["remote"])
            parent, name = remote.rsplit("/", 1)
            name = name.lower()
            if (
                parent != directory
                or not re.fullmatch(r"(?:system|lms[0-9]{2})\.txt", name)
                or name in names
                or not isinstance(component["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", component["sha256"])
            ):
                raise ValueError("Invalid alternative transport component identity")
            names.add(name)
        if "system.txt" not in names:
            raise ValueError("Alternative transport requires native SYSTEM")
        result.append({**scope, "review": dict(binding)})
    return result


def load_maintenance(path: Path) -> tuple[acquisition.AcquisitionConfig, dict[str, Any]]:
    config = acquisition.load_config(path)
    raw = json.loads(external_path(path).read_text("utf-8-sig"))
    cache_mib = raw.get("registry_audit_cache_mib", 128)
    if type(cache_mib) is not int or not 32 <= cache_mib <= 512:
        raise ValueError("Registry audit cache must be an integer from 32 through 512 MiB")
    raw["registry_audit_cache_mib"] = cache_mib
    for key in ("database", "archive_root"):
        value = raw.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Explicit operating database/archive required")
        raw[key] = external_path(Path(value))
    calendar = raw.get("reviewed_calendar")
    if calendar is not None:
        if not isinstance(calendar, str) or not calendar.strip():
            raise ValueError("Reviewed calendar requires an explicit private path")
        calendar_path = Path(calendar)
        if not calendar_path.is_absolute():
            calendar_path = path.resolve().parent / calendar_path
        raw["reviewed_calendar"] = external_path(calendar_path)
    raw["_config_path"] = external_path(path)
    raw["alternative_scopes"] = (
        _alternative_review(path, raw["reviewed_alternative_transports"])
        if "reviewed_alternative_transports" in raw
        else []
    )
    return config, raw


def _apply_reviewed_calendar(store: Store, settings: dict[str, Any], work: dict[str, Any]) -> None:
    if settings.get("reviewed_calendar") is not None:
        from .ftp_calendar import apply_reviewed_calendar

        work["reviewed_calendar"] = apply_reviewed_calendar(store, settings["reviewed_calendar"])


def read_journal(config: acquisition.AcquisitionConfig) -> dict[str, Any]:
    path = config.operation_root / "interpretation-state.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {"version": 1, "files": {}}


def status(path: Path) -> dict[str, Any]:
    config, settings = load_maintenance(path)
    has_inventory = config.inventory_path.exists()
    # A valid review can precede discovery, including in a relocated restore.
    # Scope-to-inventory binding belongs to planning once inventory exists.
    scopes = settings["alternative_scopes"] if has_inventory else []
    items = plan(config, alternative_scopes=scopes) if has_inventory else []
    source = acquisition.read_state(config)
    work = read_journal(config)
    counts: Counter[str] = Counter()
    for item in items:
        remote = item["remote"]
        if item["action"] == "packaged":
            counts["packaged_member_transport"] += 1
        elif remote not in source["files"]:
            counts["awaiting_acquisition"] += 1
        else:
            key = remote + "#" + source["files"][remote]["current_sha256"]
            checkpoint = work["files"].get(key, {})
            counts[
                checkpoint.get("status", "awaiting_interpretation")
                if checkpoint.get("action") == item["action"]
                else "awaiting_interpretation"
            ] += 1
    group_counts: Counter[str] = Counter()
    missing_components = 0
    for directory, group in _partial_directories(items, alternative_scopes=scopes).items():
        missing = sum(_component_receipt(item, source) is None for item in group["components"])
        missing_components += missing
        checkpoint = work.get("partial_groups", {}).get(_partial_key(directory, group, source), {})
        group_counts[
            checkpoint.get(
                "status", "awaiting_components" if missing else "awaiting_interpretation"
            )
        ] += 1
    for group in work.get("partial_groups", {}).values():
        if group.get("transport") != "derived_zip" or group.get("current_interpretation") is False:
            continue
        current = source["files"].get(group["remote"], {}).get("current_sha256")
        if current == group["parent_sha256"]:
            group_counts[group["status"]] += 1
    calendar = work.get("reviewed_calendar")
    if calendar:
        calendar = {key: value for key, value in calendar.items() if key != "configuration"}
    publication_path = config.operation_root / "publication-state.json"
    publication = (
        json.loads(publication_path.read_text("utf-8")) if publication_path.is_file() else None
    )
    if publication and "manifest" in publication.get("result", {}):
        manifest = publication["result"]["manifest"]
        publication["result"]["manifest"] = {
            key: manifest[key]
            for key in (
                "version",
                "mode",
                "training_authorized",
                "input_count",
                "period_count",
                "member_states",
                "selected_supply_import_count",
                "absence_means",
            )
            if key in manifest
        }
    return {
        "inventory": acquisition.acquisition_status(config)
        if has_inventory
        else {"inventory_complete": False, "file_count": 0, "state": "not_discovered"},
        "dispositions": dict(counts),
        "partial_registry": {
            "groups": sum(group_counts.values()),
            "statuses": dict(group_counts),
            "missing_acquisition_components": missing_components,
            "canonical_eligible": False,
            "source_role": "registry_partial",
        },
        "reviewed_calendar": calendar,
        "training": "paused; no training calls in maintenance",
        "scheduler": "external; check Windows Task Scheduler for installed task state",
        "failures": {key: value["code"] for key, value in work.get("failures", {}).items()},
        "cancel_requested": (config.operation_root / "cancel-request.json").exists(),
        "publication": publication,
    }


def control(path: Path, *, cancel: bool) -> dict[str, Any]:
    """A durable request is observed only outside source/database transactions."""
    config, _ = load_maintenance(path)
    marker = config.operation_root / "cancel-request.json"
    if cancel:
        acquisition.write_json(
            marker,
            {
                "version": 1,
                "requested_utc": acquisition.utc_now(),
                "scope": "stop between maintenance batches",
            },
        )
    else:
        marker.unlink(missing_ok=True)
    return {"cancel_requested": cancel, "database_access": False}


def _delivery(
    store: Store,
    receipt: dict[str, Any],
    native: Path,
    phase: str,
    kind: str,
    *,
    raw_scope: dict[str, Any] | None = None,
) -> str:
    remote, sha = receipt["remote"], receipt["sha256"]
    uri = "ftp://ftp.medicinpriser.dk" + remote
    # Reuse one unambiguous accepted earlier receipt rather than duplicate E1.
    matches = (
        store._connection.execute(
            "SELECT delivery_id::VARCHAR FROM source_delivery "
            "WHERE original_uri=? AND content_hash=? AND source=? "
            "AND coalesce(json_extract_string(coverage,'$.source_role'),'')<>'raw_source' "
            "AND coalesce(json_extract_string(coverage,"
            "'$.alternative_transport.interpretation'),'')<>'raw_only'",
            [uri, sha, "official-lms-ftp"],
        ).fetchall()
        if raw_scope is None
        else []
    )
    if len(matches) == 1:
        return str(matches[0][0])
    return store.register_source(
        native,
        source="official-lms-ftp",
        receipt_key=(
            "f5:raw-alternative:" + remote + ":" + sha + ":" + digest(raw_scope)
            if raw_scope is not None
            else "f5:" + remote + ":" + sha
        ),
        original_uri=uri,
        edition="sha256:" + sha,
        phase=phase,
        received=Timing(
            datetime.fromisoformat(receipt["started_utc"]),
            datetime.fromisoformat(receipt["completed_utc"]),
            "instant" if receipt["started_utc"] == receipt["completed_utc"] else "range",
            "observed successful native FTP acquisition interval",
            "UTC",
        ),
        coverage={
            "version": 1,
            "kind": "unknown",
            "family": kind,
            "scope": "native edition; reconciliation and selection establish admitted scope",
            "observed_server_modification": receipt["server_modification"],
            **(
                {
                    "alternative_transport": raw_scope,
                    "source_role": "raw_source",
                    "canonical_eligible": False,
                    "complete_registry": False,
                    "scope": "Exact reviewed raw original; no typed assertions or replacement",
                }
                if raw_scope is not None
                else {}
            ),
        },
    )


def update(
    path: Path,
    *,
    limit: int | None = None,
    refresh: bool = True,
    acquire_only: bool = False,
    recheck: bool = False,
) -> dict[str, Any]:
    config, _ = load_maintenance(path)
    config.operation_root.mkdir(parents=True, exist_ok=True)
    with writer_lock(config.operation_root / "interpretation.writer"):
        return _update(
            path, limit=limit, refresh=refresh, acquire_only=acquire_only, recheck=recheck
        )


def _update(
    path: Path,
    *,
    limit: int | None = None,
    refresh: bool = True,
    acquire_only: bool = False,
    recheck: bool = False,
) -> dict[str, Any]:
    config, settings = load_maintenance(path)

    def cancelled() -> bool:
        return (config.operation_root / "cancel-request.json").exists()

    if cancelled():
        return {**status(path), "run_status": "cancelled"}
    if not acquire_only:
        from . import lms_registry_audit

        # Resource capacity changes no interpreted value or proof. Keep the
        # complete-input cache bounded; a daily working set must fit to benefit.
        lms_registry_audit._record_cache = lms_registry_audit._RecordCache(
            settings["registry_audit_cache_mib"] * 1024 * 1024
        )
    work = read_journal(config)
    work.setdefault("failures", {})
    work.setdefault("partial_groups", {})
    for checkpoint in work["files"].values():
        if (
            checkpoint.get("status") == "independently_verified"
            and checkpoint.get("verification", {}).get("complete") is True
        ):
            checkpoint["independently_verified"] = True

    def failure(remote: str, code: str, error: Exception | None = None) -> None:
        prior = work["failures"].get(remote, {})
        work["failures"][remote] = {
            "code": code,
            "attempts": prior.get("attempts", 0) + 1,
            "last_utc": acquisition.utc_now(),
        }
        if error is not None:
            error_type = re.sub(r"[^A-Za-z0-9_]", "_", type(error).__name__)
            message = (
                str(error).encode("utf-8", errors="replace")[:2048].decode("utf-8", errors="ignore")
            )
            acquisition.write_json(
                config.operation_root / "failure-details" / (error_type + ".json"),
                {
                    "version": 1,
                    "remote": remote,
                    "code": code,
                    "last_utc": acquisition.utc_now(),
                    "message": message,
                    "scope": "Latest private error per type; message capped at 2048 UTF8 bytes",
                },
            )
        acquisition.write_json(config.operation_root / "interpretation-state.json", work)

    if refresh or not config.inventory_path.exists():
        try:
            acquisition.discover(config, refresh=refresh, cancelled=cancelled)
            work["failures"].pop("discovery", None)
        except RuntimeError as error:
            failure("discovery", type(error).__name__, error)
    elif not acquisition.load_inventory(config.inventory_path)["complete"]:
        try:
            acquisition.discover(config, cancelled=cancelled)
            work["failures"].pop("discovery", None)
        except RuntimeError as error:
            failure("discovery", type(error).__name__, error)
    inventory = acquisition.load_inventory(config.inventory_path)
    if inventory["complete"]:
        work["failures"].pop("discovery", None)
    if cancelled():
        return {**status(path), "run_status": "cancelled"}
    items = [
        item
        for item in plan(config, alternative_scopes=settings["alternative_scopes"])
        if item["action"] != "packaged"
    ]
    # Source releases are processed chronologically by locator, not for precedence.
    items.sort(key=lambda item: (item["action"] != "registry", item["remote"]))
    retained = acquisition.read_state(config)
    pending = []
    for item in items:
        remote = item["remote"]
        existing = retained["files"].get(remote)
        key = remote + "#" + existing["current_sha256"] if existing else ""
        if (
            existing
            and not recheck
            and existing.get("inventory_size") == item["size"]
            and acquisition.same_modification(
                existing.get("inventory_modification"), item["server_modification"]
            )
            and key in work["files"]
            and work["files"][key].get("action") == item["action"]
            and work["files"][key].get("alternative_transport") == item.get("alternative_transport")
            and work["files"][key].get("status") != "awaiting_evidenced_period"
            and not _interpretation_pending(work["files"][key], remote)
            and remote not in work["failures"]
        ):
            continue
        pending.append(item)
    if limit is not None:
        if limit < 1:
            raise ValueError("Limit must be positive")
        pending = pending[:limit]
    for offset in range(0, len(pending), config.batch_size):
        if cancelled():
            break
        batch = pending[offset : offset + config.batch_size]
        if shutil.disk_usage(settings["database"].parent).free < 8 * 1024**3:
            raise RuntimeError("Insufficient safety reserve; accepted data unchanged")
        previous_receipts = {
            item["remote"]: len(retained["files"].get(item["remote"], {}).get("receipts", []))
            for item in batch
        }
        acquire_failed = False
        acquisition_error: Exception | None = None
        isolated_errors: dict[str, Exception] = {}
        failed_remotes: set[str] = set()
        actual_recheck = recheck or any(
            work["failures"].get(item["remote"], {}).get("code") == "recheck_failed"
            for item in batch
        )
        try:
            acquisition.acquire(config, [item["remote"] for item in batch], recheck=actual_recheck)
        except Exception as error:
            # acquire checkpoints successful native gets before reporting bounded failure.
            acquire_failed = True
            acquisition_error = error
            if not isinstance(error, RuntimeError):
                # A corrupt earlier retained object must not starve unrelated batch members.
                for item in batch:
                    try:
                        acquisition.acquire(config, [item["remote"]], recheck=actual_recheck)
                    except Exception as isolated_error:
                        failed_remotes.add(item["remote"])
                        isolated_errors[item["remote"]] = isolated_error
        retained = acquisition.read_state(config)
        available = []
        for item in batch:
            entry = retained["files"].get(item["remote"])
            fresh = (
                not actual_recheck
                or entry is not None
                and len(entry.get("receipts", [])) > previous_receipts[item["remote"]]
            )
            if (
                entry
                and item["remote"] not in failed_remotes
                and fresh
                and entry.get("inventory_size") == item["size"]
                and acquisition.same_modification(
                    entry.get("inventory_modification"), item["server_modification"]
                )
            ):
                available.append(item)
                work["failures"].pop(item["remote"], None)
            else:
                failure(
                    item["remote"],
                    "recheck_failed"
                    if actual_recheck
                    else "acquisition_failed"
                    if acquire_failed
                    else "missing_native_receipt",
                    isolated_errors.get(item["remote"], acquisition_error),
                )
        if acquire_only:
            continue
        if cancelled():
            break
        if not available:
            continue
        started = time.monotonic()
        if any(item.get("alternative_transport") for item in available):
            if (
                _alternative_review(
                    settings["_config_path"], settings["reviewed_alternative_transports"]
                )
                != settings["alternative_scopes"]
            ):
                raise ValueError("Alternative transport review changed before source retention")
        with Store(settings["database"], settings["archive_root"]) as store:
            _apply_reviewed_calendar(store, settings, work)
            store._connection.execute("SET checkpoint_threshold='256MB'")
            for item in available:
                remote = item["remote"]
                entry = retained["files"][remote]
                sha = entry["current_sha256"]
                key = remote + "#" + sha
                if item.get("container_failure_sha256") not in {None, sha}:
                    item = next(
                        current
                        for current in plan(
                            config, alternative_scopes=settings["alternative_scopes"]
                        )
                        if current["remote"] == remote
                    )
                if (
                    key in work["files"]
                    and work["files"][key].get("action") == item["action"]
                    and work["files"][key].get("alternative_transport")
                    == item.get("alternative_transport")
                    and work["files"][key].get("status") != "awaiting_evidenced_period"
                    and not _interpretation_pending(work["files"][key], remote)
                ):
                    continue
                receipt = _component_receipt(item, retained)
                if receipt is None:
                    failure(remote, "missing_native_receipt")
                    continue
                native = archive_path(config.archive_root, sha)
                try:
                    result = _interpret(store, receipt, native, item)
                except Exception as error:
                    failure(remote, type(error).__name__, error)
                    continue
                work["failures"].pop(remote, None)
                previous = work["files"].get(key, {})
                if item.get("container_failure") == "invalid_zip":
                    result["container_failure"] = "invalid_zip"
                    result["rejected_import_id"] = previous.get(
                        "rejected_import_id", previous.get("import_id")
                    )
                partial_group = result.pop("partial_group", None)
                if partial_group is not None:
                    history = _prior_partial(previous, result["import_id"])
                    if history:
                        result["prior_interpretations"] = history
                        partial_group["prior_interpretations"] = history
                    for old_key, old_group in work["partial_groups"].items():
                        if (
                            old_key != key + "#" + PARTIAL_ZIP_PROFILE
                            and old_group.get("remote") == remote
                            and old_group.get("parent_sha256") == sha
                        ):
                            old_group["current_interpretation"] = False
                            old_group["superseded_by_import_id"] = result["import_id"]
                    partial_group["current_interpretation"] = True
                    work["partial_groups"][key + "#" + PARTIAL_ZIP_PROFILE] = partial_group
                work["files"][key] = {
                    "remote": remote,
                    "sha256": sha,
                    "action": item["action"],
                    **result,
                }
                acquisition.write_json(config.operation_root / "interpretation-state.json", work)
        print(
            json.dumps(
                {
                    "checkpointed": offset + len(batch),
                    "of": len(pending),
                    "seconds": round(time.monotonic() - started, 2),
                }
            ),
            flush=True,
        )
    # A newly rejected container can expose loose members after pending was
    # frozen. Account for them now; acquire them on the next resumable pass.
    items = [
        item
        for item in plan(config, alternative_scopes=settings["alternative_scopes"])
        if item["action"] != "packaged"
    ]
    if not acquire_only and not cancelled():
        _maintain_partial_groups(config, settings, items, retained, work, failure, cancelled)
        _reconcile_preliminary_workbooks(
            config, settings, items, retained, work, failure, cancelled
        )
    acquisition.write_json(config.operation_root / "interpretation-state.json", work)
    result = status(path)
    outstanding = (
        not inventory["complete"]
        or any(
            result["dispositions"].get(key, 0)
            for key in (
                "awaiting_acquisition",
                "awaiting_interpretation",
                "awaiting_evidenced_period",
                "awaiting_native_reconciliation",
            )
        )
        or any(
            result["partial_registry"]["statuses"].get(key, 0)
            for key in ("awaiting_components", "awaiting_interpretation")
        )
    )
    result["run_status"] = (
        "cancelled"
        if cancelled()
        else "failed"
        if work["failures"]
        else "partial"
        if outstanding
        else "complete"
    )
    if not acquire_only and not cancelled() and not outstanding:
        _publish(config, settings, items, retained, work, inventory)
        result = {**status(path), "run_status": result["run_status"]}
    return result


def _interpret(
    store: Store, receipt: dict[str, Any], native: Path, item: dict[str, Any]
) -> dict[str, Any]:
    remote = receipt["remote"]
    candidate = item.get("alternative_transport")
    raw_scope = candidate if candidate and candidate.get("interpretation") == "raw_only" else None
    phase = (
        raw_scope["phase"] if raw_scope else "preliminary" if "FOREL" in remote.upper() else "final"
    )
    if raw_scope:
        expected = next(
            component["sha256"]
            for component in raw_scope["components"]
            if component["remote"] == remote
        )
        if item["action"] != "retain" or receipt["sha256"] != expected:
            raise ValueError("Raw alternative retention requires exact reviewed component bytes")
        verify(native, expected, item["size"])
    if item["action"] in {"preliminary_introductions", "preliminary_discontinued"}:
        return _interpret_preliminary_workbook(store, receipt, native, item)
    delivery = _delivery(
        store,
        receipt,
        native,
        phase,
        "registry_partial" if item["action"].startswith("partial_") else item["action"],
        raw_scope=raw_scope,
    )
    if item["action"] in {"registry", "partial_zip"}:
        context = {"remote": remote, "source_role": "registry", "publication_evidence": "unknown"}
        # Full interpretations remain immutable; independently verified partial
        # references have a separate import and cannot replace registry releases.
        with store.transaction():

            def interpret(parser_version: str | None = None) -> dict[str, Any]:
                arguments = {"parser_version": parser_version} if parser_version else {}
                answer = ingest_registry(store, delivery, phase=phase, context=context, **arguments)
                if answer["status"] == "rejected" and "reason" not in answer:
                    codes = sorted({issue["code"] for issue in store.issues(answer["import_id"])})
                    if len(codes) == 1:
                        answer["reason"] = codes[0]
                return answer

            imported = interpret()
            preliminary_reason = (
                phase == "preliminary" and imported.get("reason") == "release_phase_conflict"
            )
            structural_reason = (
                imported.get("reason") if imported.get("reason") in PARTIAL_ZIP_REASONS else None
            )
            rejected_ids = []
            variant_metadata: dict[str, Any] = {}
            if imported["status"] == "rejected":
                rejected_ids.append(imported["import_id"])
            if imported.get("reason") == "native_record_terminator":
                variant_metadata["format_variant_version"] = MISSING_FINAL_CRLF_PROFILE
                imported = interpret(MISSING_FINAL_CRLF_PROFILE)
                if imported["status"] == "rejected":
                    rejected_ids.append(imported["import_id"])
            if imported["status"] == "rejected" and imported.get("reason") in PARTIAL_ZIP_REASONS:
                variant_metadata["format_variant_version"] = FIXED_WIDTH_PROFILE
                imported = interpret(FIXED_WIDTH_PROFILE)
                if imported["status"] == "rejected":
                    rejected_ids.append(imported["import_id"])
            if rejected_ids:
                variant_metadata.update(
                    prior_interpretation_import_id=rejected_ids[0],
                    prior_interpretation_import_ids=rejected_ids,
                )
            if imported["status"] == "accepted":
                verified = verify_registry(store, imported["import_id"])
                result = {
                    **imported,
                    "status": "independently_verified",
                    "delivery_id": delivery,
                    "verification": verified,
                    "independently_verified": True,
                }
            else:
                result = {**imported, "status": "unsupported", "delivery_id": delivery}
                partial_reason = (
                    "release_phase_conflict"
                    if preliminary_reason
                    else imported["reason"]
                    if imported.get("reason") in PARTIAL_ZIP_REASONS
                    else structural_reason
                    if imported.get("reason") == "native_record_width"
                    else None
                )
                if partial_reason:
                    result["partial_interpreter_version"] = PARTIAL_ZIP_PROFILE
                    try:
                        partial = ingest_partial_zip_registry(
                            store,
                            delivery,
                            phase=phase,
                            reason=partial_reason,
                            rejected_import_ids=rejected_ids,
                        )
                    except PartialRegistryError as error:
                        if not str(error).startswith(
                            (
                                "partial_system_",
                                "partial_native_terminator",
                                "partial_zip_system_",
                                "partial_zip_missing_system",
                                "partial_zip_member_contract",
                                "partial_zip_invalid",
                                "partial_zip_no_supported_business_members",
                            )
                        ):
                            raise
                        # Unknown SYSTEM/profile/layout scope stays unsupported;
                        # this exact interpreter is not retried indefinitely.
                        result["partial_unsupported_reason"] = str(error)
                    else:
                        partial.pop("retry", None)
                        result.update(partial)
                        result.update(
                            action="partial_zip",
                            status="independently_verified",
                            rejected_import_id=rejected_ids[0],
                            reason=partial_reason,
                            full_interpretation_reason=imported.get("reason"),
                            verification={
                                "complete": True,
                                "complete_registry": False,
                                "canonical_eligible": False,
                                "evidence_hash": partial["evidence_hash"],
                            },
                            partial_group={
                                **partial,
                                "remote": remote,
                                "parent_sha256": receipt["sha256"],
                                "transport": "derived_zip",
                                "status": "independently_verified",
                            },
                        )
            result.update(variant_metadata)
            result["phase"] = phase
    elif item["action"] in {"DOK", "LMS36", "LMS37"}:
        imported = ingest_supplement(store, delivery, item["action"])
        result = {
            **imported,
            "delivery_id": delivery,
            "status": "independently_verified"
            if imported["status"] == "accepted"
            else "unsupported",
        }
    else:
        result = {
            "status": "retained_without_interpretation",
            "delivery_id": delivery,
            "reason": item["reason"],
        }
        if item["action"].startswith("partial_"):
            result.update(source_role="registry_partial", canonical_eligible=False)
        if raw_scope:
            result.update(
                alternative_transport=raw_scope,
                phase=phase,
                source_role="raw_source",
                canonical_eligible=False,
                complete_registry=False,
            )
    return result


def _native_workbook_period(data: bytes) -> tuple[date, str]:
    """Read only the supported introduction metadata positions, never filenames."""
    book = _xlsx(data, "source.xlsx")
    observed = []
    try:
        for sheet in book.worksheets:
            for row in sheet.iter_rows(min_row=2, max_row=2, min_col=5, max_col=12):
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("Takstdato"):
                        match = re.fullmatch(
                            r"Takstdato:\s*([0-9]{2}-[0-9]{2}-[0-9]{4})", cell.value
                        )
                        if match is None or cell.data_type in {"f", "e"}:
                            raise ValueError("Invalid native introduction tariff date")
                        value = datetime.strptime(match[1], "%d-%m-%Y").date()
                        if value.strftime("%d-%m-%Y") != match[1]:
                            raise ValueError("Invalid native introduction tariff date")
                        observed.append((value, f"sheet:{sheet.title}!{cell.coordinate}"))
    finally:
        book.close()
    if not observed or len({value for value, _ in observed}) != 1:
        raise ValueError("Unestablished or conflicting native introduction tariff date")
    return observed[0]


def _interpret_preliminary_workbook(
    store: Store,
    receipt: dict[str, Any],
    native: Path,
    item: dict[str, Any],
) -> dict[str, Any]:
    family = item["action"].removeprefix("preliminary_")
    period, locator = None, None
    if family == "introductions":
        period, locator = _native_workbook_period(native.read_bytes())
        if not store._connection.execute(
            "SELECT 1 FROM price_period WHERE period_start=?", [period]
        ).fetchone():
            delivery = _delivery(store, receipt, native, "preliminary", family)
            return {
                "status": "awaiting_evidenced_period",
                "delivery_id": delivery,
                "source_role": "preliminary_report",
                "phase": "preliminary",
                "native_period": period.isoformat(),
                "native_period_locator": locator,
                "reason": "Native tariff start requires an existing evidenced price_period; "
                "no end date inferred",
                "independently_verified": False,
            }
    delivery = store.register_source(
        native,
        source="official-lms-ftp",
        receipt_key="f5:preliminary-report-v2:" + receipt["remote"] + ":" + receipt["sha256"],
        original_uri="ftp://ftp.medicinpriser.dk" + receipt["remote"],
        edition="sha256:" + receipt["sha256"],
        phase="preliminary",
        current_period=period,
        received=Timing(
            datetime.fromisoformat(receipt["started_utc"]),
            datetime.fromisoformat(receipt["completed_utc"]),
            "instant" if receipt["started_utc"] == receipt["completed_utc"] else "range",
            "observed successful native FTP acquisition interval",
            "UTC",
        ),
        coverage={
            "version": 1,
            "kind": "changes",
            "family": family,
            "source_role": "preliminary_report",
            "scope": "preliminary source assertions; not a complete registry snapshot",
            "observed_server_modification": receipt["server_modification"],
        },
    )
    imported = store.ingest(
        delivery,
        adapter=family,
        version=family + "-workbook-v2",
        mapping_version="ftp-preliminary-report-delivery-v1",
        mapping_policy="delivery",
        context={
            "tax_basis": "unknown",
            "fee_basis": "unknown",
            "phase": "preliminary",
            "source_role": "preliminary_report",
            "publication_evidence": "unknown",
            "period_role": "native_report_effective_period",
            "native_period_locator": locator,
        },
    )
    return {
        **imported,
        "delivery_id": delivery,
        "source_role": "preliminary_report",
        "phase": "preliminary",
        "status": "awaiting_native_reconciliation"
        if imported["validation_status"] == "accepted"
        else "unsupported",
        "independently_verified": False,
        "reason_codes": sorted({issue["code"] for issue in store.issues(imported["import_id"])}),
    }


def _reconcile_preliminary_workbooks(
    config: acquisition.AcquisitionConfig,
    settings: dict[str, Any],
    items: list[dict[str, Any]],
    retained: dict[str, Any],
    work: dict[str, Any],
    failure: Callable[[str, str, Exception | None], None],
    cancelled: Callable[[], bool],
) -> None:
    # Independent OOXML reconciliation opens a scoped read-only connection;
    # all Store writers are closed before this stage starts.
    from .source_audit import reconcile

    for item in items:
        if cancelled():
            break
        if item["action"] not in {"preliminary_introductions", "preliminary_discontinued"}:
            continue
        remote = item["remote"]
        entry = retained["files"].get(remote)
        key = remote + "#" + entry["current_sha256"] if entry else ""
        checkpoint = work["files"].get(key, {})
        if checkpoint.get("status") not in {
            "awaiting_native_reconciliation",
            "independently_verified",
        }:
            continue
        try:
            report = reconcile(
                settings["database"], settings["archive_root"], import_ids=[checkpoint["import_id"]]
            )
            source = (canonical_json(report) + "\n").encode("utf-8")
            proof_path = (
                config.operation_root
                / "preliminary-workbook-audits"
                / (hashlib.sha256(source).hexdigest() + ".json")
            )
            proof_path.parent.mkdir(parents=True, exist_ok=True)
            if proof_path.exists():
                if proof_path.read_bytes() != source:
                    raise ValueError("Immutable preliminary workbook proof changed")
            else:
                with proof_path.open("xb") as stream:
                    stream.write(source)
            if not report["complete"]:
                raise ValueError("Independent preliminary workbook reconciliation failed")
            checkpoint.update(
                status="independently_verified",
                independently_verified=True,
                verification={
                    "complete": True,
                    "proof_path": str(proof_path),
                    "proof_sha256": hashlib.sha256(source).hexdigest(),
                    "auditor_code": report["auditor_code"],
                    "verified_count": report["verified_count"],
                },
            )
            work["failures"].pop(remote, None)
        except Exception as error:
            checkpoint.update(status="awaiting_native_reconciliation", independently_verified=False)
            failure(remote, "preliminary_native_reconciliation_failed", error)
        acquisition.write_json(config.operation_root / "interpretation-state.json", work)


def _retire_replaced_partial_failures(
    config: acquisition.AcquisitionConfig,
    settings: dict[str, Any],
    items: list[dict[str, Any]],
    retained: dict[str, Any],
    work: dict[str, Any],
    failure: Callable[[str, str, Exception | None], None],
    cancelled: Callable[[], bool],
) -> None:
    pending = [key for key in work["failures"] if key.startswith("partial-native:")]
    if not pending:
        return
    partials = _partial_directories(items, alternative_scopes=settings.get("alternative_scopes"))
    for failure_key in sorted(pending):
        if cancelled():
            break
        directory = failure_key.removeprefix("partial-native:")
        if directory in partials:
            continue
        for item in items:
            remote = item["remote"]
            parent, name = remote.rsplit("/", 1)
            if (
                parent != directory
                or name.lower() not in {"lms.zip", "lms-gros.zip"}
                or item["action"] != "registry"
            ):
                continue
            receipt = _component_receipt(item, retained)
            if receipt is None or not acquisition.same_modification(
                receipt["server_modification"], item["server_modification"]
            ):
                continue
            checkpoint = work["files"].get(remote + "#" + receipt["sha256"], {})
            expected = checkpoint.get("verification", {})
            if (
                checkpoint.get("action") != "registry"
                or checkpoint.get("status") != "independently_verified"
                or checkpoint.get("independently_verified") is not True
                or checkpoint.get("remote") != remote
                or checkpoint.get("sha256") != receipt["sha256"]
                or expected.get("complete") is not True
                or expected.get("import_id") != checkpoint.get("import_id")
            ):
                continue
            try:
                with (
                    Store(settings["database"], settings["archive_root"]) as store,
                    store.transaction(),
                ):
                    delivery = store.get_record("source_delivery", checkpoint["delivery_id"])
                    imported = store.get_record("import_batch", checkpoint["import_id"])
                    if (
                        delivery["source"] != "official-lms-ftp"
                        or delivery["original_uri"] != "ftp://ftp.medicinpriser.dk" + remote
                        or delivery["content_hash"] != receipt["sha256"]
                        or delivery["byte_count"] != item["size"]
                        or str(imported["delivery_id"]) != checkpoint["delivery_id"]
                    ):
                        continue
                    # Reuse immutable member proofs, but freshly verify this one
                    # complete release's originals, bindings and import lineage.
                    if verify_registry(store, checkpoint["import_id"]) != expected:
                        raise ValueError("Complete replacement checkpoint proof changed")
                work["failures"].pop(failure_key, None)
                acquisition.write_json(config.operation_root / "interpretation-state.json", work)
                break
            except Exception as error:
                failure(failure_key, "partial_replacement_verification_failed", error)


def _maintain_partial_groups(
    config: acquisition.AcquisitionConfig,
    settings: dict[str, Any],
    items: list[dict[str, Any]],
    retained: dict[str, Any],
    work: dict[str, Any],
    failure: Callable[[str, str, Exception | None], None],
    cancelled: Callable[[], bool],
) -> None:
    """Separate reference imports; every known component needs its native receipt."""
    _retire_replaced_partial_failures(config, settings, items, retained, work, failure, cancelled)
    scopes = settings.get("alternative_scopes")
    if settings.get("reviewed_alternative_transports") is not None:
        if (
            _alternative_review(
                settings["_config_path"], settings["reviewed_alternative_transports"]
            )
            != scopes
        ):
            raise ValueError("Alternative transport review changed during maintenance")
    for directory, group in sorted(_partial_directories(items, alternative_scopes=scopes).items()):
        if cancelled():
            break
        key = _partial_key(directory, group, retained)
        failure_key = "partial-native:" + directory
        missing = [
            item["remote"]
            for item in group["components"]
            if _component_receipt(item, retained) is None
        ]
        if missing:
            work["partial_groups"][key] = {
                "directory": directory,
                "status": "awaiting_components",
                "missing_components": missing,
                "expected_components": len(group["components"]),
                "available_components": len(group["components"]) - len(missing),
                "source_role": "registry_partial",
                "canonical_eligible": False,
            }
            continue
        alternative = group.get("alternative_transport")
        phase = (
            alternative["phase"]
            if alternative
            else ("preliminary" if "FOREL" in directory.upper() else "final")
        )
        try:
            if alternative:
                # Prove the whole reviewed component set before Store or any
                # source registration can write. Each is checked again below.
                for item in group["components"]:
                    receipt = _component_receipt(item, retained)
                    assert receipt is not None
                    expected = next(
                        component["sha256"]
                        for component in alternative["components"]
                        if component["remote"] == item["remote"]
                    )
                    if receipt["sha256"] != expected:
                        raise ValueError(
                            "Alternative transport acquisition differs from reviewed SHA256"
                        )
                    verify(archive_path(config.archive_root, expected), expected, item["size"])
            with Store(settings["database"], settings["archive_root"]) as store:
                _apply_reviewed_calendar(store, settings, work)
                components: dict[int | str, str] = {}
                for item in group["components"]:
                    receipt = _component_receipt(item, retained)
                    assert receipt is not None
                    if alternative and receipt["sha256"] != next(
                        component["sha256"]
                        for component in alternative["components"]
                        if component["remote"] == item["remote"]
                    ):
                        raise ValueError(
                            "Alternative transport acquisition differs from reviewed SHA256"
                        )
                    native = archive_path(config.archive_root, receipt["sha256"])
                    verify(native, receipt["sha256"], item["size"])
                    name = item["remote"].rsplit("/", 1)[1].lower()
                    number = 0 if name == "system.txt" else int(name[3:5])
                    if number in components:
                        raise ValueError("Duplicate case-insensitive partial native component")
                    components[number] = _delivery(
                        store, receipt, native, phase, "registry_partial"
                    )
                rejected = [
                    item
                    for item in group["containers"]
                    if item.get("container_failure") == "invalid_zip"
                ]
                reason = (
                    "native ZIP rejected as invalid_zip; "
                    "loose originals retained as partial references"
                    if rejected
                    else "native ZIP is zero bytes on server"
                    if group["containers"]
                    else "native ZIP absent from observed directory"
                )
                if any(
                    item["remote"].lower().endswith("/lms03.txt") and item["size"] == 0
                    for item in group["components"]
                ):
                    reason += "; native LMS03 price member is zero bytes"
                context: dict[str, Any] = {
                    "partial_reason": reason,
                    "source_role": "registry_partial",
                    "remote_directory": directory,
                    "publication_evidence": "unknown",
                }
                if alternative:
                    context.update(
                        partial_reason=alternative["reason"],
                        alternative_transport=alternative,
                        canonical_eligible=False,
                        correction_precedence="unestablished; no replacement or union",
                    )
                if rejected:
                    evidence = []
                    for container in rejected:
                        entry = retained["files"][container["remote"]]
                        checkpoint = work["files"][
                            container["remote"] + "#" + entry["current_sha256"]
                        ]
                        evidence.append(
                            {
                                "remote": container["remote"],
                                "sha256": entry["current_sha256"],
                                "code": "invalid_zip",
                                "source_delivery_id": checkpoint["delivery_id"],
                                "rejected_import_id": checkpoint.get(
                                    "rejected_import_id", checkpoint.get("import_id")
                                ),
                            }
                        )
                    context["rejected_containers"] = evidence
                source = partial_registry_manifest(components, phase=phase, context=context)
                sha = hashlib.sha256(source).hexdigest()
                path = config.operation_root / "partial-manifests" / (sha + ".json")
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    if path.read_bytes() != source:
                        raise ValueError("Immutable partial native receipt changed")
                else:
                    with path.open("xb") as stream:
                        stream.write(source)
                delivery = store.register_source(
                    path,
                    source=MANIFEST_SOURCE,
                    receipt_key="partial-native:" + directory + ":" + sha,
                    coverage={
                        "version": 1,
                        "kind": "derived_receipt",
                        "source_role": "registry_partial",
                    },
                )
                imported = ingest_partial_registry(store, delivery)
            prior = work["partial_groups"].get(key, {})
            prior_ids = list(prior.get("prior_interpretation_import_ids", []))
            if prior.get("import_id") and prior["import_id"] != imported["import_id"]:
                prior_ids.append(prior["import_id"])
            work["partial_groups"][key] = {
                **{name: value for name, value in imported.items() if name != "retry"},
                **(
                    {"prior_interpretation_import_ids": sorted(set(prior_ids))} if prior_ids else {}
                ),
                **(
                    {"prior_interpretations": _prior_partial(prior, imported["import_id"])}
                    if _prior_partial(prior, imported["import_id"])
                    else {}
                ),
                "partial_parser_version": PARTIAL_PROFILE,
                "status": "partial_reference_verified",
                "directory": directory,
                "manifest_delivery_id": delivery,
                "source_role": "registry_partial",
                "expected_components": len(group["components"]),
                "available_components": len(group["components"]),
                "missing_components": [],
            }
            work["failures"].pop(failure_key, None)
        except Exception as error:
            work["partial_groups"][key] = {
                **work["partial_groups"].get(key, {}),
                "directory": directory,
                "status": "failed",
                "code": type(error).__name__,
                "source_role": "registry_partial",
                "canonical_eligible": False,
                "expected_components": len(group["components"]),
                "available_components": len(group["components"]),
            }
            failure(failure_key, type(error).__name__, error)
        acquisition.write_json(config.operation_root / "interpretation-state.json", work)


def _publish(
    config: acquisition.AcquisitionConfig,
    settings: dict[str, Any],
    items: list[dict[str, Any]],
    retained: dict[str, Any],
    work: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    selection = settings.get("registry_selection_config")
    policy = settings.get("registry_policy_config")
    if selection and policy:
        raise ValueError("Choose one explicit selection or one reviewed selection policy")
    if not (selection or policy) or not inventory["complete"] or work["failures"]:
        return
    allowed = settings.get(
        "publication_allowed_statuses",
        ["independently_verified", "retained_without_interpretation"],
    )
    terminal = {"independently_verified", "retained_without_interpretation", "unsupported"}
    if not isinstance(allowed, list) or not set(allowed) <= terminal:
        raise ValueError("Publication requires explicit supported terminal statuses")
    for item in items:
        entry = retained["files"].get(item["remote"])
        key = item["remote"] + "#" + entry["current_sha256"] if entry else ""
        if work["files"].get(key, {}).get("status") not in allowed:
            return
    from .registry_selection import build_configuration, publish

    with Store(settings["database"], settings["archive_root"]) as store:
        if policy:
            if not isinstance(policy, str):
                raise ValueError("Explicit private policy path required")
            policy_path = external_path(Path(policy))
            configuration = build_configuration(store, json.loads(policy_path.read_text("utf-8")))
        else:
            if not isinstance(selection, str):
                raise ValueError("Explicit private selection path required")
            selection_path = external_path(Path(selection))
            configuration = json.loads(selection_path.read_text("utf-8"))
        source = (canonical_json(configuration) + "\n").encode("utf-8")
        selection_path = config.operation_root / "selections" / (digest(configuration) + ".json")
        selection_path.parent.mkdir(parents=True, exist_ok=True)
        if selection_path.exists():
            if selection_path.read_bytes() != source:
                raise ValueError("Frozen generated selection configuration changed")
        else:
            with selection_path.open("xb") as stream:
                stream.write(source)
        result = publish(store, configuration)
        if result.get("verified") is not True:
            raise ValueError("Publication requires a fully verified dataset")
    acquisition.write_json(
        config.operation_root / "publication-state.json",
        {
            "version": 1,
            "configuration_sha256": hashlib.sha256(source).hexdigest(),
            "configuration_path": str(selection_path),
            "completed_utc": acquisition.utc_now(),
            "result": result,
        },
    )


def _failure_result(path: Path, command: str, error: Exception) -> dict[str, Any]:
    """Keep one bounded diagnostic without serializing messages, locals or inputs."""
    result: dict[str, Any] = {
        "run_status": "failed",
        "code": type(error).__name__,
        "detail": "Maintenance failed; no private failure receipt was written",
    }
    # A failed read-only command must remain read-only, including diagnostics.
    if command in {"status", "dry-run"}:
        return result
    try:
        config = acquisition.load_config(path)
        frames = traceback.extract_tb(error.__traceback__, limit=-16)
        acquisition.write_json(
            config.operation_root / "last-failure.json",
            {
                "version": 1,
                "failed_utc": acquisition.utc_now(),
                "command": command,
                "code": type(error).__name__,
                "frames": [
                    {
                        "module_file": Path(frame.filename).name,
                        "function": frame.name,
                        "line": frame.lineno,
                    }
                    for frame in frames
                ],
                "scope": "Diagnostic only; does not change accepted publication or checkpoints",
            },
        )
    except Exception:
        # A bad configuration or unwritable diagnostic path must not hide the
        # original failure or claim that a receipt exists.
        return result
    result["detail"] = "Maintenance failed; inspect last-failure.json in the operation directory"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["update", "resume", "status", "dry-run", "discover", "cancel", "clear-cancel"],
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--acquire-only", action="store_true")
    parser.add_argument("--recheck", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in {"update", "resume"}:
            result = update(
                args.config,
                limit=args.limit,
                refresh=args.refresh or args.command == "update",
                acquire_only=args.acquire_only,
                recheck=args.recheck,
            )
        elif args.command == "discover":
            config = acquisition.load_config(args.config)
            acquisition.discover(
                config,
                refresh=True,
                cancelled=lambda: (config.operation_root / "cancel-request.json").exists(),
            )
            result = status(args.config)
        elif args.command in {"cancel", "clear-cancel"}:
            result = control(args.config, cancel=args.command == "cancel")
        else:
            result = status(args.config)
            result["mutations"] = False
    except Exception as error:
        print(json.dumps(_failure_result(args.config, args.command, error)))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("run_status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
