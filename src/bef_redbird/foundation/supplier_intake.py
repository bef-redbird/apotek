"""Direct, resumable supplier-report intake; publication remains an explicit step."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4
from zipfile import BadZipFile

import duckdb
from openpyxl.utils.exceptions import InvalidFileException
from xlrd.biffh import XLRDError

from . import Store, Timing, WriterBusy
from .adapters.reports import parse_supply
from .archive import external_path
from .locking import writer_lock
from .records import FoundationError, canonical_json
from .registry_selection import _supply_input
from .source_audit import expected_report, read_grids, reconcile

SOURCE = "official-archive:supply"
CONTEXT = {
    "date_basis": "source_effective_date",
    "fee_basis": "unknown",
    "reported_status": "reported_disruption",
    "tax_basis": "unknown",
    "wholesaler_namespace": "archive_report_grossist_source_code",
}


def _read(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _write(path: Path, data: bytes, *, immutable: bool = False) -> None:
    """Flush then atomically publish; immutable artifacts can never be replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex[:12]}.part")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ValueError("Immutable intake evidence changed") from None
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path: Path, value: dict[str, Any], *, immutable: bool = False) -> None:
    _write(path, canonical_json(value).encode("utf-8"), immutable=immutable)


def inspect_native(data: bytes) -> dict[str, Any]:
    """Use native headers, ISO-week sheet title and cells, never path or mtime."""
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("supplier_resource_limit")
    try:
        expected = expected_report(data, "supply", CONTEXT)
        parsed = parse_supply(data, CONTEXT)
    except (BadZipFile, InvalidFileException, XLRDError):
        raise ValueError("supplier_workbook_unreadable") from None
    supplies = [a for a in expected["assertions"] if a["table"] == "supply_observation"]
    actual = [a for a in parsed.assertions if a.table == "supply_observation"]
    if not supplies or expected["issues"] or parsed.issues:
        raise ValueError("supplier_cells_require_review")
    expected_dates = sorted(a["values"]["observation_date"] for a in supplies)
    actual_dates = sorted(a.values["observation_date"].isoformat() for a in actual)
    if expected_dates != actual_dates or len(supplies) != parsed.records:
        raise ValueError("supplier_native_interpretations_disagree")
    sheets = read_grids(data)
    titles = {a["source_locator"].split("!", 1)[0].removeprefix("sheet:") for a in supplies}
    if len(titles) != 1:
        raise ValueError("supplier_report_window_requires_review")
    title = next(iter(titles))
    if not any(grid["name"] == title for grid in sheets):
        raise ValueError("supplier_native_sheet_missing")
    match = re.fullmatch(r"(\d{4})\s+(\d{1,2})\s*-\s*(\d{1,2})", title.strip())
    if match is None:
        raise ValueError("supplier_report_window_requires_review")
    year, first_week, last_week = map(int, match.groups())
    start = date.fromisocalendar(year, first_week, 1)
    if (start + timedelta(days=7)).isocalendar().week != last_week:
        raise ValueError("supplier_report_window_requires_review")
    end = start + timedelta(days=14)
    if any(not start <= date.fromisoformat(day) < end for day in expected_dates):
        raise ValueError("supplier_effective_date_outside_native_report_window")
    return {
        "native_sheet": title,
        "report_from": start.isoformat(),
        "report_to_exclusive": end.isoformat(),
        "date_basis": "native ISO-week sheet title and row effective-date cells",
        "effective_dates": sorted(set(expected_dates)),
        "source_rows": len(supplies),
        "coverage_kind": "unknown",
        "absence_means": "unknown",
        "carry_forward": False,
        "identity": "source_only_no_longitudinal_identity",
    }


def _pins(policy_path: Path, registered_path: Path, batch: Path, pin: dict[str, str]) -> None:
    """A durable intent repairs interruption between the two atomic policy writes."""
    policy, registered = _read(policy_path), _read(registered_path)
    current = policy.get("supply_imports", [])
    # An older completed intake can retry after later reports have been admitted.
    if current == registered.get("supply_imports", []) and pin in current:
        if [item for item in current if item["import_id"] == pin["import_id"]] != [pin]:
            raise ValueError("Supplier import has ambiguous proof pins")
        return
    intent_path = batch / "pin-intent.json"
    if intent_path.exists():
        intent = _read(intent_path)
        if intent["pin"] != pin:
            raise ValueError("Intake pin differs from its durable intent")
    else:
        before = policy.get("supply_imports", [])
        if before != registered.get("supply_imports", []):
            raise ValueError("Supplier policy and evidence pins disagree")
        existing = [p for p in before if p["import_id"] == pin["import_id"]]
        if existing and existing != [pin]:
            raise ValueError("Supplier import already has another proof pin")
        after = before if existing else [*before, pin]
        intent = {"version": 1, "pin": pin, "before": before, "after": after}
        _json(intent_path, intent, immutable=True)
    for path in (policy_path, registered_path):
        document = _read(path)
        if document.get("supply_imports", []) not in (intent["before"], intent["after"]):
            raise ValueError("Supplier policy changed during interrupted intake")
        if document.get("supply_imports", []) != intent["after"]:
            document["supply_imports"] = intent["after"]
            _json(path, document)


def ingest_file(config_path: Path, source_path: Path) -> dict[str, Any]:
    """Retain bytes and true intake receipt, verify, then pin supported new evidence."""
    config_path = external_path(config_path)
    config = _read(config_path)
    if any(
        not isinstance(config.get(key), str) or not config[key].strip()
        for key in ("database", "archive_root", "operation_root", "registry_policy_config")
    ):
        raise ValueError("invalid_supplier_intake_configuration")
    database = external_path(Path(config["database"]))
    archive = external_path(Path(config["archive_root"]))
    operation_root = external_path(Path(config["operation_root"]))
    policy_path = external_path(Path(config["registry_policy_config"]))
    registered_path = external_path(
        Path(
            config.get(
                "registered_legacy_evidence",
                policy_path.parent / "source-review" / "registered-legacy-evidence.json",
            )
        )
    )
    source_path = external_path(source_path)
    # Avoid accidentally initializing an empty operational database on a typo.
    if not database.is_file():
        raise ValueError("supplier_store_not_initialized")
    operation_root.mkdir(parents=True, exist_ok=True)
    with writer_lock(operation_root / "interpretation.writer"):
        if source_path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("supplier_resource_limit")
        data = source_path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        batch = operation_root / "supplier-intake" / content_hash
        batch.mkdir(parents=True, exist_ok=True)
        manifest_path = batch / "manifest.json"
        if manifest_path.exists():
            receipt = _read(manifest_path)
            if receipt["content_hash"] != content_hash:
                raise ValueError("Supplier receipt hash changed")
        else:
            receipt = {
                "version": 1,
                "content_hash": content_hash,
                "byte_count": len(data),
                "received_at_utc": datetime.now(UTC).isoformat(),
                "receipt_basis": "first direct-file intake; not website download/publication time",
                "original_filename": source_path.name,
                "original_uri": source_path.as_uri(),
            }
            _json(manifest_path, receipt, immutable=True)
        original = batch / "original" / receipt["original_filename"]
        _write(original, data, immutable=True)
        try:
            native = inspect_native(data)
            _json(batch / "native-validation.json", native, immutable=True)
            return _admit(
                database,
                archive,
                policy_path,
                registered_path,
                batch,
                original,
                receipt,
                native,
            )
        except (ValueError, OSError, FoundationError, duckdb.Error) as error:
            _json(
                batch / "status.json",
                {
                    "version": 1,
                    "status": "failed",
                    "error_code": str(error)
                    if re.fullmatch(r"[a-z_]+", str(error))
                    else "supplier_intake_requires_review",
                    "content_hash": content_hash,
                },
            )
            raise


def _admit(
    database: Path,
    archive: Path,
    policy_path: Path,
    registered_path: Path,
    batch: Path,
    original: Path,
    receipt: dict[str, Any],
    native: dict[str, Any],
) -> dict[str, Any]:
    policy = _read(policy_path)
    pinned = policy.get("supply_imports", [])
    stamp = datetime.fromisoformat(receipt["received_at_utc"])
    reused = None
    with Store(database, archive) as store:
        delivery_id = store.register_source(
            original,
            source=SOURCE,
            receipt_key="supplier-direct:" + receipt["content_hash"],
            original_uri=receipt["original_uri"],
            received=Timing(
                lower=stamp,
                upper=stamp,
                precision="instant",
                basis=receipt["receipt_basis"],
                source_timezone="UTC",
            ),
            coverage={"version": 1, "kind": "unknown", "family": "supply", **native},
        )
        for pin in pinned:
            row = store._connection.execute(
                "SELECT d.content_hash FROM import_batch i "
                "JOIN source_delivery d USING(delivery_id) "
                "WHERE i.import_id=?",
                [pin["import_id"]],
            ).fetchone()
            if row and row[0] == receipt["content_hash"]:
                _supply_input(store, pin)
                reused = pin
                break
        if reused:
            import_id = reused["import_id"]
        else:
            summary = store.ingest(
                delivery_id,
                adapter="supply",
                version="supply-workbook-v1",
                mapping_version="supplier-source-local-v1",
                mapping_policy="delivery",
                context=CONTEXT,
            )
            if summary["validation_status"] != "accepted":
                raise ValueError("supplier_import_not_accepted")
            import_id = summary["import_id"]
    # Source auditor owns a fresh read-only connection; it never trusts adapter output.
    proof = reconcile(database, archive, [original], import_ids=[import_id])
    if not proof["complete"] or proof["mismatches"] or proof["quarantined_observation_ids"]:
        raise ValueError("supplier_independent_verification_failed")
    proof_path = batch / "source-verification.json.gz"
    proof_bytes = gzip.compress(canonical_json(proof).encode("utf-8"), mtime=0)
    _write(proof_path, proof_bytes, immutable=True)
    with Store(database, archive) as store:
        if reused:
            pin = reused
        else:
            proof_hash = hashlib.sha256(proof_bytes).hexdigest()
            proof_delivery_id = store.register_source(
                proof_path,
                source="foundation:independent-source-verification",
                receipt_key=f"{import_id}:{proof_hash}",
            )
            pin = {"import_id": import_id, "verification_delivery_id": proof_delivery_id}
        _supply_input(store, pin)
        overlapping = []
        for old in pinned:
            if old["import_id"] == import_id:
                continue
            count = store._connection.execute(
                "SELECT count(*) FROM supply_observation WHERE import_id=? "
                "AND observation_date>=? AND observation_date<?",
                [old["import_id"], native["report_from"], native["report_to_exclusive"]],
            ).fetchone()
            if count and count[0]:
                overlapping.append(old["import_id"])
    status = "verified_revision_review_required" if overlapping and not reused else "admitted"
    if status == "admitted":
        _pins(policy_path, registered_path, batch, pin)
    result = {
        "version": 1,
        "status": status,
        "publication_status": "pending_explicit_publication",
        "content_hash": receipt["content_hash"],
        "received_at_utc": receipt["received_at_utc"],
        "receipt_delivery_id": delivery_id,
        **pin,
        "reused_accepted_import": bool(reused),
        "source_rows": native["source_rows"],
        "report_from": native["report_from"],
        "report_to_exclusive": native["report_to_exclusive"],
        "overlapping_import_ids": overlapping,
        "absence_means": "unknown",
        "carry_forward": False,
        "identity": "source_only_no_longitudinal_identity",
    }
    _json(batch / "status.json", result)
    return result


def _pin_pairs(document: dict[str, Any]) -> list[tuple[str, str]]:
    pins = document.get("supply_imports", [])
    pairs = [(str(pin["import_id"]), str(pin["verification_delivery_id"])) for pin in pins]
    if len({pair[0] for pair in pairs}) != len(pairs):
        raise ValueError("ambiguous_supplier_publication_pins")
    return sorted(pairs)


def publish_pending_supply(
    config_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Publish changed verified supply pins; identical retries avoid full-history work."""
    config_path = external_path(config_path)
    config = _read(config_path)
    operation_root = external_path(Path(config["operation_root"]))
    policy = external_path(Path(config["registry_policy_config"]))
    expected = _pin_pairs(_read(policy))
    publication_path = operation_root / "publication-state.json"

    def current() -> tuple[list[tuple[str, str]] | None, dict[str, Any]]:
        if not publication_path.exists():
            return None, {}
        publication = _read(publication_path)
        path = Path(publication["configuration_path"])
        if not path.is_absolute():
            path = operation_root / path
        configuration = _read(external_path(path))
        return _pin_pairs(configuration), publication

    actual, publication = current()
    if actual == expected:
        return {"status": "already_current", "publication_started": False}
    command = [
        sys.executable,
        "-m",
        "bef_redbird.foundation.ftp_maintenance",
        "resume",
        "--config",
        str(config_path),
    ]
    result = runner(command, check=False, capture_output=True, text=True)
    _write(
        operation_root / "supplier-publication-last.log",
        (result.stdout + result.stderr).encode("utf-8"),
    )
    if result.returncode:
        raise FoundationError("supplier_publication_failed")
    actual, publication = current()
    if actual != expected or _pin_pairs(_read(policy)) != expected:
        raise FoundationError("supplier_publication_did_not_admit_pins")
    return {
        "status": "published",
        "publication_started": True,
        "dataset_id": publication.get("result", {}).get("dataset_id"),
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid_supplier_intake_arguments")


def _failure(error: Exception, args: argparse.Namespace | None) -> dict[str, Any]:
    guidance = {
        "invalid_supplier_intake_arguments": "Use --config <config.json> <report.xls> [--publish].",
        "supplier_configuration_not_found": "Select an existing operating config with --config.",
        "supplier_input_not_found": "Select an existing downloaded report; check its location.",
        "invalid_supplier_intake_configuration": (
            "Check configured database, archive, operation and policy paths."
        ),
        "supplier_store_not_initialized": (
            "Use the initialized operating config; do not create an empty database."
        ),
        "supplier_resource_limit": "Use a workbook <=128 MiB; review larger sources separately.",
        "supplier_workbook_unreadable": (
            "Download the original report again; the workbook container is unreadable."
        ),
        "supplier_cells_require_review": (
            "Review original dates, identifiers and wholesaler codes; keep retained bytes intact."
        ),
        "supplier_report_window_requires_review": (
            "Review the native ISO-week sheet title; filenames cannot establish dates."
        ),
        "supplier_effective_date_outside_native_report_window": (
            "Review row effective dates against the native report window."
        ),
        "supplier_independent_verification_failed": (
            "Review the independent source proof before admitting this report."
        ),
        "supplier_import_not_accepted": "Review import status and the original before retrying.",
        "supplier_publication_failed": (
            "The report was retained. Review supplier-publication-last.log, "
            "resolve the maintenance failure and retry the same command."
        ),
        "supplier_publication_did_not_admit_pins": (
            "Review the publication configuration and supplier pins; admission was not confirmed."
        ),
    }
    code = str(error) if str(error) in guidance else "supplier_intake_requires_review"
    if isinstance(error, WriterBusy):
        code = "supplier_writer_busy"
        action = "Wait for the active foundation writer to finish, then retry the same command."
    elif isinstance(error, duckdb.Error):
        code = "supplier_database_unavailable"
        action = "Stop the viewer/other writers and check the operating database before retrying."
    elif isinstance(error, OSError):
        code = "supplier_io_unavailable"
        action = (
            "Check that the report and configured storage are available and writable, then retry."
        )
    elif str(error) in {
        "unsupported_source_format",
        "independent_unsupported_source_format",
        "independent_schema_mismatch",
    }:
        code = "supplier_schema_unsupported"
        action = "Use the original supplier workbook; changed native layouts need adapter review."
    else:
        action = guidance.get(
            code,
            "Check operating config and retained evidence; resolve review issues before retrying.",
        )
    result: dict[str, Any] = {"status": "failed", "error_code": code, "guidance": action}
    if args is not None:
        try:
            settings = _read(args.config)
            operation_root = external_path(Path(settings["operation_root"]))
            if args.file.is_file() and args.file.stat().st_size <= 128 * 1024 * 1024:
                content_hash = hashlib.sha256(args.file.read_bytes()).hexdigest()
                status = operation_root / "supplier-intake" / content_hash / "status.json"
                if status.is_file():
                    result["status_path"] = str(status)
        except (ValueError, OSError, KeyError, TypeError):
            pass
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish changed supply pins through existing maintenance",
    )
    parser.add_argument("file", type=Path)
    args = None
    try:
        args = parser.parse_args(argv)
        if not args.config.is_file():
            raise ValueError("supplier_configuration_not_found")
        if not args.file.is_file():
            raise ValueError("supplier_input_not_found")
        result = ingest_file(args.config, args.file)
        if args.publish and result["status"] == "admitted":
            publication = publish_pending_supply(args.config)
            result["publication"] = publication
            result["publication_status"] = publication["status"]
            operation_root = external_path(Path(_read(args.config)["operation_root"]))
            _json(
                operation_root / "supplier-intake" / result["content_hash"] / "status.json", result
            )
    except (ValueError, OSError, FoundationError, duckdb.Error, KeyError, TypeError) as error:
        print(json.dumps(_failure(error, args), sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "admitted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
