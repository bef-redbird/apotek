"""Bounded source intake: retained originals and explicit report/official snapshots.

This uses the existing content-addressed source archive and protected FTP transport.
It publishes research artifacts only, never a parallel operating database.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from bef_redbird.foundation import ftp_acquisition as ftp
from bef_redbird.foundation.adapters.base import Assertion, Parsed, field_state, identifier
from bef_redbird.foundation.adapters.lms_ftp import parse_lms_ftp
from bef_redbird.foundation.adapters.workbooks import (
    _at,
    _cito_rows,
    _field,
    _locator,
    _source_id,
)
from bef_redbird.foundation.archive import archive_file, archive_path
from bef_redbird.foundation.lms_ftp_audit import expected_lms_ftp

from .dataset import ResearchError, digest
from .e4_common import checked, frozen_json, identity, read, utc
from .group_features import pin


def report_assertions(data: bytes, origin: str, target: str) -> Parsed:
    """Reuse the established workbook framing/cell rules without interpreting cost."""
    parsed = Parsed()
    sheet, mapping, records = _cito_rows(data)
    for rowno, row in records:
        if all(field_state(_at(row, col).value) == "blank" for col in mapping.values()):
            continue
        parsed.records += 1
        ids = _source_id(
            _at(row, mapping["varenr"]), parsed, _locator(sheet, rowno, mapping["varenr"])
        )
        if ids is None:
            continue
        original, package = ids
        for heading, period in (("subkode", origin), ("nysubkode", target)):
            day = date.fromisoformat(period)
            locator = _locator(sheet, rowno, mapping[heading])
            code, state = _field(
                _at(row, mapping[heading]),
                parsed,
                locator,
                package,
                "abc_code",
                day,
                conversion="abc",
            )
            group = None
            if heading == "subkode":
                group, _ = _field(
                    _at(row, mapping["subgrp"]),
                    parsed,
                    _locator(sheet, rowno, mapping["subgrp"]),
                    package,
                    "group_code",
                    day,
                    conversion="group",
                )
            parsed.assertions.append(
                Assertion(
                    "classification_observation",
                    locator,
                    original,
                    {"abc_code": code, "group_code": group},
                    {"abc_code": state},
                    {},
                    day,
                )
            )
    return parsed


def intake_report(
    original: Path,
    archive: Path,
    output: Path,
    context: dict[str, Any],
    *,
    supersedes: Path | None = None,
) -> Path:
    """One normalized row/identifier, with duplicate and conflict evidence retained."""
    original, archive, output = original.resolve(), archive.resolve(), output.resolve()
    origin, target = context["current_period"], context["target_period"]
    if origin >= target or context.get("next_period_start") != target:
        raise ResearchError("e4_report_period_binding")
    if not context.get("period_evidence") or "reported_receipt" not in context:
        raise ResearchError("e4_report_context_incomplete")
    prior = pin(supersedes) if supersedes else None
    if supersedes:
        parent = read(supersedes)
        if (parent["context"]["current_period"], parent["context"]["target_period"]) != (
            origin,
            target,
        ):
            raise ResearchError("e4_revision_period_mismatch")
    binding = {"original_sha256": digest(original), "context": context, "supersedes": prior}
    report_id = identity(binding)
    destination = output / report_id
    manifest = destination / "report.json"
    if manifest.exists():
        saved = read(manifest)
        checked(saved["original"])
        checked(saved["rows"])
        return manifest
    destination.mkdir(parents=True, exist_ok=True)
    # Existing immutable archive implements exact byte identity and atomic retention.
    retained = archive_file(archive, original)
    native = archive_path(archive, str(binding["original_sha256"]))
    if digest(native) != binding["original_sha256"]:
        raise ResearchError("e4_report_original_retention")
    parsed = report_assertions(native.read_bytes(), origin, target)
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for assertion in parsed.assertions:
        if assertion.table != "classification_observation":
            continue
        role = "current" if str(assertion.effective_period) == origin else "advance"
        buckets[identifier(assertion.source_identifier)][role].append(
            {
                "code": assertion.values["abc_code"],
                "state": assertion.presence["abc_code"],
                "locator": assertion.source_locator,
                "raw_identifier": assertion.source_identifier,
                "group": assertion.values.get("group_code"),
            }
        )
    rows = []
    for package, roles in sorted(buckets.items()):
        row: dict[str, Any] = {"package_number": package}
        for role in ("current", "advance"):
            values = roles.get(role, [])
            distinct = {(x["code"], x["state"]) for x in values}
            state = (
                "conflicting"
                if len(distinct) > 1
                else (
                    "missing"
                    if not values or values[0]["state"] in {"blank", "unreported"}
                    else "observed"
                    if values[0]["state"] == "present" and values[0]["code"] in {"A", "B", "C"}
                    else "invalid"
                )
            )
            row[role + "_abc"] = values[0]["code"] if state == "observed" else None
            row[role + "_state"] = state
            row[role + "_locators"] = json.dumps([x["locator"] for x in values])
            row[role + "_assertions"] = len(values)
        row["duplicate_state"] = "identical_codes" if len(roles["current"]) > 1 else "unique"
        if "conflicting" in (row["current_state"], row["advance_state"]):
            row["duplicate_state"] = "conflicting_codes"
        groups = {x["group"] for x in roles["current"]}
        row["reported_current_group"] = next(iter(groups)) if len(groups) == 1 else None
        row["group_state"] = (
            "observed" if len(groups) == 1 and None not in groups else "ambiguous_or_missing"
        )
        rows.append(row)
    frame = pd.DataFrame(rows)
    frame.to_parquet(destination / "rows.parquet", index=False)
    issues = [
        {"code": x.code, "locator": x.source_locator, "identifier": x.identifier, "field": x.field}
        for x in parsed.issues
    ]
    frozen_json(
        manifest,
        {
            "version": "e4-advance-report-v1",
            "report_id": report_id,
            "binding": binding,
            "context": context,
            "supersedes": prior,
            "original": pin(native),
            "source_locator": str(original.resolve()),
            "retention": str(retained),
            "intake_utc": utc(),
            "actual_acquisition": context.get(
                "actual_acquisition", {"status": "unknown_before_intake"}
            ),
            "file_modification_utc": datetime.fromtimestamp(
                original.stat().st_mtime, UTC
            ).isoformat(),
            "mtime_is_receipt": False,
            "rows": pin(destination / "rows.parquet"),
            "counts": {
                "physical_records": parsed.records,
                "unique_valid_identifiers": len(frame),
                "duplicate_excess": parsed.records - len(frame),
                "invalid_identifier_records": sum(
                    x.code == "invalid_source_identifier" for x in parsed.issues
                ),
                "states": {
                    r: frame[r + "_state"].value_counts().to_dict() for r in ("current", "advance")
                },
            },
            "issues": issues,
            "cost_field": "Not used: Cito kostpris is not established official AIP",
            "revision_selection": (
                "Explicit supersedes link only; changed unrelated reports remain separate"
            ),
        },
    )
    return manifest


def acquire_official(config_path: Path, period: str, evidence_root: Path) -> Path:
    """Targeted discovery/acquisition through F5 transport, no full-history re-audit."""
    settings = read(config_path)
    calendar = read(Path(settings["reviewed_calendar"]))
    matching = [x for x in calendar["periods"] if x["start"] == period]
    if len(matching) != 1:
        raise ResearchError("e4_official_period_needs_reviewed_calendar")
    output = evidence_root / period
    manifest = output / "acquisition.json"
    if manifest.exists():
        checked(read(manifest)["original"])
        return manifest
    config = ftp.load_config(config_path)
    transport = ftp.native_runner(config)
    directory = "/LMS/" + period.replace("-", "")
    # A successful listing establishes existence; native SYSTEM independently binds dates.
    result = transport(
        {"name": "ftp-" + uuid4().hex, "operations": [{"kind": "list", "remote": directory}]}
    )
    listing = ftp.read_listings(result)
    if len(listing) != 1 or listing[0]["directory"] != directory:
        raise ResearchError("e4_official_release_unavailable")
    files = [
        e
        for e in listing[0]["entries"]
        if e["type"] == "-" and e["filename"].lower().endswith(".zip")
    ]
    if len(files) != 1:
        raise ResearchError("e4_official_edition_ambiguous")
    remote = directory + "/" + files[0]["filename"]
    # Preserve old inventory bytes and cutoff. Only this dated directory was refreshed.
    with ftp.acquisition_lock(config):
        before = config.inventory_path.read_bytes()
        sha = digest(config.inventory_path)
        historical = config.operation_root / "inventory-history" / (sha + ".json")
        historical.parent.mkdir(parents=True, exist_ok=True)
        if not historical.exists():
            historical.write_bytes(before)
        old = ftp.load_inventory(config.inventory_path)
        updated = ftp.catalogue(
            [x for x in old["listings"] if x["directory"] != directory] + listing,
            old["inventory_cutoff_utc"],
            old["adoptions"]
            + [
                {
                    "path": str(historical),
                    "sha256": sha,
                    "scope": (
                        "E4 targeted dated-directory refresh; all other listing cutoffs unchanged"
                    ),
                    "receipt": result["receipt_path"],
                    "utc": utc(),
                }
            ],
        )
        ftp.write_json(config.inventory_path, updated)
    ftp.acquire(config, [remote])
    acquired = ftp.read_state(config)["files"][remote]
    native = archive_path(config.archive_root, acquired["current_sha256"])
    output.mkdir(parents=True, exist_ok=True)
    frozen_json(
        manifest,
        {
            "version": "e4-official-acquisition-v1",
            "period": period,
            "end_exclusive": matching[0]["end_exclusive"],
            "calendar": pin(Path(settings["reviewed_calendar"])),
            "remote": remote,
            "original": pin(native),
            "acquisition": acquired,
            "listing_receipt": result,
            "listing": listing[0],
            "authority": (
                "Only exact dated complete final edition; native control date must "
                "agree; no download-time correction precedence"
            ),
            "database_changed": False,
        },
    )
    return manifest


def parse_official(acquisition_manifest: Path, output: Path) -> Path:
    manifest = output / "snapshot.json"
    if manifest.exists():
        checked(read(manifest)["rows"])
        return manifest
    acquired = read(acquisition_manifest)
    native = checked(acquired["original"])
    period = acquired["period"]
    context = {
        "schema_vintage": "lms-20260626-final",
        "release_date": period,
        "current_period": period,
        "period_end_exclusive": acquired["end_exclusive"],
        "tax_basis": "unknown",
        "fee_basis": "unknown",
    }
    parsed = parse_lms_ftp(native.read_bytes(), context)
    independent = expected_lms_ftp(native.read_bytes(), "lms-ftp", context)
    # Full direct-assertion comparison uses the existing independent native reader.
    actual = {
        (a.table, a.source_identifier, a.source_locator): (a.values, a.presence)
        for a in parsed.assertions
    }
    expected = {
        (a["table"], a["source_identifier"], a["source_locator"]): (a["values"], a["presence"])
        for a in independent["assertions"]
    }
    if actual != expected:
        raise ResearchError("e4_new_native_reconciliation_failed")
    rows: dict[str, dict[str, Any]] = {}
    for a in parsed.assertions:
        package = a.source_identifier
        row = rows.setdefault(
            package,
            {
                "package_number": package,
                "period_start": period,
                "abc": None,
                "group_code": None,
                "classification_state": "missing_record",
            },
        )
        if a.table == "price_observation" and a.values["price_type"] == "aip":
            row.update(
                aip_ore=a.values["amount_ore"],
                price_locator=a.source_locator,
                price_state="observed"
                if a.presence["amount_ore"] == "present" and (a.values["amount_ore"] or 0) > 0
                else "unresolved_or_invalid",
            )
            raw = a.raw["package_identity"]
            row["identity_token"] = raw["drug_id"]["text"]
            row["dispensing_code"] = raw["dispensing_code"]["text"].strip()
            row["created_date_token"] = raw["created_date"]["text"]
            row["exit_date_token"] = raw["exit_date"]["text"]
        elif a.table == "classification_observation":
            row.update(
                abc=a.values["abc_code"],
                group_code=a.values["group_code"],
                classification_state="observed"
                if a.presence["abc_code"] == "present"
                else "missing_or_invalid",
                classification_locator=a.source_locator,
            )
    output.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(list(rows.values())).sort_values("package_number").reset_index(drop=True)
    frame.to_parquet(output / "rows.parquet", index=False)
    frozen_json(
        manifest,
        {
            "version": "e4-official-snapshot-v1",
            "period": period,
            "acquisition": pin(acquisition_manifest),
            "original": pin(native),
            "context": context,
            "rows": pin(output / "rows.parquet"),
            "packages": len(frame),
            "classification_counts": frame.classification_state.value_counts().to_dict(),
            "independent_assertions": len(expected),
            "discrepancies": 0,
            "claim": (
                "Direct ABC/AIP assertions reconciled; positive AIP price semantics "
                "inherit accepted modern LMS native contract; zero unsupported"
            ),
        },
    )
    return manifest


def compare_report(
    report_path: Path,
    origin: pd.DataFrame,
    target: pd.DataFrame,
    candidates: pd.DataFrame,
    output: Path,
    *,
    evidence: list[dict[str, Any]] | None = None,
) -> Path:
    """Keep missing codes, unmatched identity and wrong observed codes separate."""
    report = read(report_path)
    for ref in evidence or []:
        checked(ref)
    for frame, period in (
        (origin, report["context"]["current_period"]),
        (target, report["context"]["target_period"]),
    ):
        if not pd.to_datetime(frame.period_start).eq(pd.Timestamp(period)).all():
            raise ResearchError("e4_official_comparison_period_mismatch")
    cito = pd.read_parquet(checked(report["rows"]))
    if origin.package_number.duplicated().any() or target.package_number.duplicated().any():
        raise ResearchError("e4_ambiguous_official_identifiers")
    frame = cito.copy()
    for role, official in (("current", origin), ("advance", target)):
        columns = [
            "package_number",
            "abc",
            "classification_state",
            "identity_token",
            "created_date_token",
            "exit_date_token",
        ]
        renamed = official[columns].rename(
            columns={c: f"official_{role}_{c}" for c in columns if c != "package_number"}
        )
        frame = frame.merge(renamed, on="package_number", how="left", validate="one_to_one")
        comparison = []
        for row in frame.to_dict("records"):
            if row[role + "_state"] != "observed":
                state = "report_" + row[role + "_state"]
            elif pd.isna(row[f"official_{role}_identity_token"]):
                state = "official_identifier_unmatched"
            elif role == "advance" and (
                pd.isna(row["official_current_identity_token"])
                or row["official_current_identity_token"] != row["official_advance_identity_token"]
                or row["official_current_created_date_token"]
                != row["official_advance_created_date_token"]
                or str(row["official_current_created_date_token"] or "").strip()
                in {"", "19000101", "00000000"}
                or str(row["official_current_exit_date_token"] or "").strip()
                not in {"19000101", "00000000", ""}
            ):
                state = "identity_discontinuity_or_ambiguity"
            elif row[f"official_{role}_classification_state"] != "observed":
                state = "official_code_missing_or_unusable"
            else:
                state = (
                    "correct" if row[role + "_abc"] == row[f"official_{role}_abc"] else "incorrect"
                )
            comparison.append(state)
        frame[role + "_comparison"] = comparison
    context = [
        x
        for x in (
            "package_number",
            "selected",
            "origin_group",
            "aip_ore",
            "history_periods",
            "peer_count",
        )
        if x in candidates
    ]
    if candidates.package_number.duplicated().any():
        raise ResearchError("e4_comparison_requires_one_origin")
    frame = frame.merge(
        candidates[context].assign(in_origin_universe=True),
        on="package_number",
        how="left",
        validate="one_to_one",
    )
    frame["in_origin_universe"] = frame.in_origin_universe.fillna(False).astype(bool)
    if "selected" in frame:
        frame["selected"] = frame.selected.fillna(False).astype(bool)
    output.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(output / "comparison.parquet", index=False)
    totals = {}
    for role in ("current", "advance"):
        status = frame[role + "_comparison"]
        comparable = status.isin(["correct", "incorrect"])
        totals[role] = {
            "unique_report_identifiers": len(frame),
            "states": status.value_counts().to_dict(),
            "comparable": int(comparable.sum()),
            "accuracy_among_comparable": float(status[comparable].eq("correct").mean())
            if comparable.any()
            else None,
        }
    confusion = (
        frame.groupby(
            [
                "current_abc",
                "advance_abc",
                "official_current_abc",
                "official_advance_abc",
                "advance_comparison",
            ],
            dropna=False,
        )
        .size()
        .reset_index(name="count")
        .fillna("missing")
        .to_dict("records")
    )
    groups = []
    if "origin_group" in frame:
        for group, part in frame.groupby("origin_group", dropna=False):
            groups.append(
                {
                    "origin_group": None if pd.isna(group) else str(group),
                    "report_identifiers": len(part),
                    "correct": int(part.advance_comparison.eq("correct").sum()),
                    "incorrect": int(part.advance_comparison.eq("incorrect").sum()),
                    "unmatched_or_unusable": int(
                        (~part.advance_comparison.isin(["correct", "incorrect"])).sum()
                    ),
                }
            )
    strata = []
    for name, mask in {
        "all_report": pd.Series(True, index=frame.index),
        "origin_candidates": frame.in_origin_universe,
        "selected_candidates": frame.get("selected", pd.Series(False, index=frame.index)),
        "history_under6": frame.get(
            "history_periods", pd.Series(float("nan"), index=frame.index)
        ).lt(6),
        "equal_peers_positive": frame.get(
            "peer_count", pd.Series(float("nan"), index=frame.index)
        ).ge(1),
        "price_under100": frame.get("aip_ore", pd.Series(float("nan"), index=frame.index)).lt(
            10000
        ),
        "price_1000plus": frame.get("aip_ore", pd.Series(float("nan"), index=frame.index)).ge(
            100000
        ),
    }.items():
        part = frame.loc[mask]
        strata.append(
            {
                "stratum": name,
                "count": len(part),
                "states": part.advance_comparison.value_counts().to_dict(),
            }
        )
    result = {
        "version": "e4-cito-comparison-v1",
        "report": pin(report_path),
        "evidence": evidence or [],
        "rows": pin(output / "comparison.parquet"),
        "totals": totals,
        "origin_candidates": len(candidates),
        "origin_candidates_in_report": int(frame.in_origin_universe.sum()),
        "origin_candidates_without_report": int(
            (~candidates.package_number.isin(cito.package_number)).sum()
        ),
        "selected_candidates": int(candidates.selected.sum()) if "selected" in candidates else None,
        "selected_candidates_in_report": int(frame.selected.sum()) if "selected" in frame else None,
        "transition_confusion": confusion,
        "groups": groups,
        "strata": strata,
        "limit": (
            "One owner-period-bound snapshot; not general Cito accuracy, early-report "
            "publication proof, or recurring release schedule"
        ),
    }
    frozen_json(output / "comparison.json", result)
    return output / "comparison.json"
