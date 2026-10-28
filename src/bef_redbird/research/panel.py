"""Freeze an explicitly admitted, independently audited LMS research population.

This export is a versioned research selection, not a replacement of the foundation's
original delivery identities or its existing F3 selections. Longitudinal episode
links are causal, conservative and scoped to the exact selected observations.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bef_redbird.foundation.archive import external_path
from bef_redbird.foundation.source_audit import FIELDS, persisted_record_hash, snapshot_imports

POLICY = "e1-lms-positive-pharmacy-aip-v1"
IDENTITY_POLICY = "e1-causal-contiguous-lms-identity-v1"
PRICE_CONTEXT = {
    "price_type": "aip",
    "currency": "DKK",
    "price_basis": "per_package",
    "tax_basis": "excluded",
    "fee_basis": "excluded",
}
PANEL_COLUMNS = [
    "period_index",
    "canonical_identifier",
    "identity_episode_id",
    "amount_ore",
    "eligible",
    "exclusion_reason",
    "abc",
    "group_id",
    "provenance_id",
    "identity_state",
    "lifecycle_age_periods",
    "reported_supply_positive",
    "supply_coverage_unknown",
]


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _native_date(token: str) -> date | None:
    if not token.strip():
        return None
    parsed = datetime.strptime(token, "%Y%m%d").date()
    if parsed.strftime("%Y%m%d") != token:
        raise ValueError("Noncanonical native date")
    return parsed


def eligibility(amount: Any, identity: dict[str, str], period: str) -> str:
    """Return the first explicit exclusion; zero is retained, never called free."""
    try:
        created = _native_date(identity["created_date"])
        exited = _native_date(identity["exit_date"])
    except (ValueError, KeyError):
        return "invalid_lifecycle_date"
    if created is None or created > date.fromisoformat(period):
        return "creation_unestablished_at_period"
    code = identity["dispensing_code"].strip()
    if code.startswith(("HX", "HF")) or code in {"BP", "PRMIX", "HP", "HV"}:
        return "nonfixed_price_context"
    if code == "BEGR":
        return "hospital_scope_excluded"
    if code not in {
        "A",
        "AP",
        "AP4",
        "AP4BG",
        "AP4NB",
        "APK",
        "B",
        "BPK",
        "GH",
        "HA",
        "HA18",
        "NBS",
    }:
        return "unsupported_dispensing_context"
    if exited is not None:
        return "recorded_exit"
    if type(amount) is not int:
        return "invalid_price"
    if amount <= 0:
        return "nonpositive_price"
    return ""


def episode(
    identifier: str,
    token: tuple[str, str],
    period_index: int,
    eligible: bool,
    previous: dict[str, Any] | None,
) -> tuple[str, str]:
    """Past/current evidence only; no future survivors or retroactive relinking."""
    state = "entry_or_left_censored"
    if previous is not None:
        if previous["period_index"] != period_index - 1:
            state = "gap_reentry"
        elif previous["token"] != token:
            state = "identity_change"
        elif not previous["eligible"] or not eligible:
            state = "eligibility_boundary"
        else:
            return str(previous["episode_id"]), "continued"
    return _digest([IDENTITY_POLICY, identifier, token, period_index]), state


def _pin(path: Path, root: Path, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "path": path.relative_to(root).as_posix(),
        "sha256": file_hash(path),
        "size_bytes": path.stat().st_size,
    }


def _copy_pin(source: Path, destination: Path, expected: str) -> None:
    if file_hash(source) != expected:
        raise ValueError("Pinned source evidence changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if file_hash(destination) != expected:
        raise ValueError("Evidence copy mismatch")


def freeze_panel(database: Path, archive: Path, recipe_path: Path, output: Path) -> Path:
    """Publish only after exhaustive source proofs and explicit semantic admission.

    Output is deliberately fail-closed: partial work is left inspectable without a
    manifest. An existing output directory is never replaced or silently resumed.
    The recipe pins assessment/context evidence and one accepted import per period.
    """
    database, archive, output = map(external_path, (database, archive, output))
    recipe_bytes = recipe_path.read_bytes()
    recipe_hash = hashlib.sha256(recipe_bytes).hexdigest()
    producer_hash = file_hash(Path(__file__))
    recipe = json.loads(recipe_bytes)
    if (
        recipe.get("schema_version") != "e1-lms-selection-v1"
        or recipe.get("status") != "source_admission_accepted"
        or recipe.get("population_policy") != POLICY
        or recipe.get("identity_policy") != IDENTITY_POLICY
        or not recipe.get("evidence")
    ):
        raise ValueError("Explicit reviewed source-admission recipe required")
    releases = recipe["releases"]
    if not releases or len({r["import_id"] for r in releases}) != len(releases):
        raise ValueError("Release interpretations must be unique")
    for before, after in zip(releases, releases[1:], strict=False):
        if before["end_exclusive"] != after["period"]:
            raise ValueError("Research calendar is not contiguous")
    if any(
        date.fromisoformat(r["period"]) >= date.fromisoformat(r["end_exclusive"]) for r in releases
    ):
        raise ValueError("Invalid calendar interval")
    output.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, Any]] = []
    recipe_copy = output / "source-selection.json"
    _copy_pin(recipe_path, recipe_copy, recipe_hash)
    files.append(_pin(recipe_copy, output, "source_selection"))
    for index, item in enumerate(recipe["evidence"]):
        original = Path(item["path"])
        copy = output / "evidence" / f"context-{index:03}{''.join(original.suffixes)}"
        _copy_pin(original, copy, item["sha256"])
        files.append(_pin(copy, output, "context_evidence"))
    panel_path = output / "panel.csv.gz"
    provenance_path = output / "provenance.jsonl.gz"
    previous: dict[str, dict[str, Any]] = {}
    calendar_position = {r["period"].replace("-", ""): i for i, r in enumerate(releases)}
    coverage = []
    archived: set[str] = set()
    with (
        panel_path.open("xb") as panel_bytes,
        gzip.GzipFile(fileobj=panel_bytes, mode="wb", mtime=0) as panel_zip,
        io.TextIOWrapper(panel_zip, encoding="utf-8", newline="") as panel_text,
        provenance_path.open("xb") as provenance_bytes,
        gzip.GzipFile(fileobj=provenance_bytes, mode="wb", mtime=0) as provenance_zip,
        io.TextIOWrapper(provenance_zip, encoding="utf-8", newline="\n") as provenance,
    ):
        writer = csv.DictWriter(panel_text, fieldnames=PANEL_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for index, release in enumerate(releases):
            report_path = Path(release["audit_path"])
            if file_hash(report_path) != release["audit_sha256"]:
                raise ValueError("Pinned independent audit changed")
            report = json.loads(gzip.decompress(report_path.read_bytes()))
            if (
                report.get("complete") is not True
                or report.get("mismatches")
                or report.get("quarantined_observation_ids")
                or report.get("audit_scope")
                != {"kind": "explicit_imports", "import_ids": [release["import_id"]]}
            ):
                raise ValueError("Complete scoped independent source proof required")
            current = snapshot_imports(database, [release["import_id"]])
            fingerprints = {
                key: {"count": value["count"], "sha256": value["sha256"]}
                for key, value in current.items()
            }
            if fingerprints != report["baseline_tables"]:
                raise ValueError("Source proof no longer matches persisted records")
            if current["price_period"]["rows"] != [
                {
                    "period_start": release["period"],
                    "period_end_exclusive": release["end_exclusive"],
                }
            ]:
                raise ValueError("Selection interval differs from the verified source calendar")
            verified_rows = {
                row["observation_id"]: persisted_record_hash(row)
                for table in FIELDS
                for row in current[table]["rows"]
            }
            verified_mappings = {
                row["mapping_id"]: persisted_record_hash(
                    row, ("available_lower", "available_upper")
                )
                for row in current["package_identifier"]["rows"]
            }
            if (
                report.get("observation_hashes") != verified_rows
                or report.get("mapping_hashes") != verified_mappings
                or sorted(report.get("verified_observation_ids", [])) != sorted(verified_rows)
            ):
                raise ValueError("Independent proof assertion or mapping hashes disagree")
            audit_copy = output / "evidence" / f"audit-{index:03}.json.gz"
            _copy_pin(report_path, audit_copy, release["audit_sha256"])
            files.append(_pin(audit_copy, output, "source_audit"))
            imports = current["import_batch"]["rows"]
            if (
                len(imports) != 1
                or imports[0]["validation_status"] != "accepted"
                or imports[0]["parser_version"] != "lms-ftp-zip-v1"
                or imports[0]["delivery_id"] != release["delivery_id"]
            ):
                raise ValueError("Unexpected source interpretation")
            deliveries = {d["delivery_id"]: d for d in current["source_delivery"]["rows"]}
            delivery = deliveries[release["delivery_id"]]
            if (
                delivery["phase"] != "final"
                or delivery["current_period"] != release["period"]
                or delivery["content_hash"] != release["native_sha256"]
                or delivery["published_precision"] != "unknown"
            ):
                raise ValueError("This selection requires the reviewed final historical receipts")
            for source in deliveries.values():
                if source["content_hash"] in archived:
                    continue
                native = (archive / source["archive_location"]).resolve()
                if not native.is_relative_to(archive.resolve()):
                    raise ValueError("Archive path escapes archive root")
                copy = output / "originals" / source["content_hash"]
                _copy_pin(native, copy, source["content_hash"])
                files.append(_pin(copy, output, "native_original"))
                archived.add(source["content_hash"])
            prices = [r for r in current["price_observation"]["rows"] if r["price_type"] == "aip"]
            classes = {
                r["canonical_identifier"]: r for r in current["classification_observation"]["rows"]
            }
            if len(classes) != current["classification_observation"]["count"]:
                raise ValueError("Ambiguous current classification")
            if len({r["canonical_identifier"] for r in prices}) != len(prices):
                raise ValueError("Ambiguous current AIP")
            counts: Counter[str] = Counter()
            for row in sorted(prices, key=lambda r: r["canonical_identifier"]):
                if any(row[k] != v for k, v in PRICE_CONTEXT.items()) or (
                    row["price_role"] != "effective_price"
                    or row["effective_period"] != release["period"]
                    or row["effective_date"] != release["period"]
                    or row["quality_state"] not in {"valid", "invalid"}
                    or row["observation_id"] not in report["observation_hashes"]
                    or row["mapping_id"] not in report["mapping_hashes"]
                ):
                    raise ValueError("Unreviewed AIP context or source fidelity")
                raw = json.loads(row["source_values"])
                native_identity = {
                    key: value["text"] for key, value in raw["package_identity"].items()
                }
                identifier = row["canonical_identifier"]
                if native_identity["varenummer"] != identifier:
                    raise ValueError("Native identity mismatch")
                exclusion = eligibility(row["amount_ore"], native_identity, release["period"])
                admitted = not exclusion
                token = (native_identity["drug_id"], native_identity["created_date"])
                episode_id, identity_state = episode(
                    identifier, token, index, admitted, previous.get(identifier)
                )
                previous[identifier] = {
                    "period_index": index,
                    "token": token,
                    "eligible": admitted,
                    "episode_id": episode_id,
                }
                classification = classes.get(identifier)
                if classification is not None and (
                    classification["classification_context"] != "ordinary"
                    or classification["group_namespace"] != "lms:package-substitution"
                    or classification["effective_date"] != release["period"]
                    or classification["quality_state"] not in {"valid", "invalid"}
                    or classification["observation_id"] not in report["observation_hashes"]
                    or classification["mapping_id"] != row["mapping_id"]
                ):
                    raise ValueError("Unreviewed ordinary classification")
                evidence = {
                    "period_index": index,
                    "canonical_identifier": identifier,
                    "identity_episode_id": episode_id,
                    "identity_policy": IDENTITY_POLICY,
                    "native_identity": native_identity,
                    "identity_locators": raw["package_identity"],
                    "price_observation_id": row["observation_id"],
                    "price_observation_hash": report["observation_hashes"][row["observation_id"]],
                    "mapping_id": row["mapping_id"],
                    "mapping_hash": report["mapping_hashes"][row["mapping_id"]],
                    "delivery_id": release["delivery_id"],
                    "import_id": release["import_id"],
                    "native_sha256": release["native_sha256"],
                    "source_locator": row["source_locator"],
                    "classification_observation_id": classification["observation_id"]
                    if classification
                    else None,
                    "classification_observation_hash": report["observation_hashes"][
                        classification["observation_id"]
                    ]
                    if classification
                    else None,
                    "source_audit_sha256": release["audit_sha256"],
                    "supply_state": (
                        "unknown; no cross-source supplier identity and coverage selection admitted"
                    ),
                }
                provenance_id = _digest(evidence)
                provenance.write(
                    json.dumps(
                        {"provenance_id": provenance_id, **evidence},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                writer.writerow(
                    {
                        "period_index": index,
                        "canonical_identifier": identifier,
                        "identity_episode_id": episode_id,
                        "amount_ore": row["amount_ore"],
                        "eligible": "true" if admitted else "false",
                        "exclusion_reason": exclusion,
                        "abc": classification["abc_code"] if classification else "",
                        "group_id": classification["group_code"] if classification else "",
                        "provenance_id": provenance_id,
                        "identity_state": identity_state,
                        "lifecycle_age_periods": (
                            index - calendar_position[native_identity["created_date"]]
                            if admitted and native_identity["created_date"] in calendar_position
                            else ""
                        ),
                        "reported_supply_positive": "",
                        "supply_coverage_unknown": "true",
                    }
                )
                counts[exclusion or "eligible"] += 1
            coverage.append(
                {
                    "period_index": index,
                    "period_start": release["period"],
                    "rows": len(prices),
                    "states": dict(counts),
                }
            )
    files.extend([_pin(panel_path, output, "panel"), _pin(provenance_path, output, "provenance")])
    coverage_path = output / "selection-coverage.json"
    coverage_path.write_text(json.dumps(coverage, indent=2), "utf-8")
    files.append(_pin(coverage_path, output, "selection_coverage"))
    manifest = {
        "schema_version": "e1-panel-v1",
        "dataset_id": _digest({"recipe": recipe_hash, "files": files}),
        "price_context": PRICE_CONTEXT,
        "availability_mode": "reconstructed_research",
        "periods": [
            {"index": i, "start": r["period"], "end_exclusive": r["end_exclusive"]}
            for i, r in enumerate(releases)
        ],
        "files": files,
        "population_definition": POLICY,
        "identity_policy": IDENTITY_POLICY,
        "provenance": {
            "selection_contract": "e1-lms-selection-v1",
            "recipe_sha256": recipe_hash,
            "source_preference": (
                "Explicit audited tariff-start LMS AIP for snapshot denominator; "
                "F4 retained as independent overlap evidence; no global override"
            ),
            "timing": (
                "Acquired in 2026; historical publication unknown. "
                "Effective dates are not decision-time availability."
            ),
            "identity": (
                "Exact selected observation catalogue only; conservative causal episodes, "
                "no permanent cross-source package merge."
            ),
            "supply": (
                "Unknown. No FTP wholesaler coverage or verified F4 "
                "cross-source identity selection."
            ),
            "lifecycle_age": (
                "Calendar-index age only when the current creation date equals a selected "
                "tariff start at or before the decision. Earlier or unbound creation is unknown."
            ),
            "excluded_sources": [
                "Cito features",
                "retrospective bulk price histories",
                "preliminary LMS",
                "daily TSP corrections",
            ],
            "producer_sha256": producer_hash,
            "external_coverage_gaps": recipe.get("external_coverage_gaps", []),
            "population_scope": (
                "The selected LMS02 snapshot population after explicit eligibility exclusions; "
                "not complete market coverage. Known outside-snapshot omissions remain separate."
            ),
        },
    }
    if file_hash(recipe_path) != recipe_hash or file_hash(Path(__file__)) != producer_hash:
        raise ValueError("Selection recipe or producer changed during export")
    manifest_path = output / "dataset.json"
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze admitted audited LMS research data")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    freeze_panel(args.database, args.archive, args.recipe, args.output)
    print("Research panel frozen; model fitting remains a separate operation.")


if __name__ == "__main__":
    main()
