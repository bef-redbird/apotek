"""Compact E6 byte recovery and captured-producer inference/scoring replay.

Creation, unused restore and guarded replay are separate explicit commands. This
module never moves/deletes the original research directory or opens a live store.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import e3_recovery
from .dataset import ResearchError, digest
from .e4_common import checked, frozen_json, read, utc
from .group_features import pin

VERSION = "e6-research-capsule-v1"
DATABASE_SUFFIXES = {".duckdb", ".wal", ".db", ".sqlite", ".sqlite3"}
PROOF_LOGS = {
    "execution-d-retry1-stderr.log",
    "review/older-feature-alignment-v1.log",
    "review/older-feature-alignment-v2.log",
    "review/older-feature-alignment-v3.log",
    "review/older-feature-alignment-v4.log",
    "review/scientific-audit-cd-final.log",
    "review/completion-audit-final.log",
}
REQUIRED = {
    "protocol.json",
    "freeze.json",
    "index.json",
    "summary.json",
    "fit-ledger.json",
    "model-results.json",
    "inputs/manifest.json",
    "investigation-a/results.json",
    "producer-final.zip",
    "producer-final.json",
}


def pins(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if {"path", "sha256", "size_bytes"} <= value.keys():
            return [value]
        return [ref for child in value.values() for ref in pins(child)]
    if isinstance(value, list):
        return [ref for child in value for ref in pins(child)]
    return []


def disallowed(relative: str) -> bool:
    path = Path(relative)
    return (
        path.suffix.lower() in DATABASE_SUFFIXES | {".pyc"}
        or (path.suffix.lower() == ".log" and path.as_posix() not in PROOF_LOGS)
        or any(
            part.lower()
            in {
                "scratch",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                "synthetic-view",
            }
            or part.lower().startswith("tmp-")
            for part in path.parts
        )
        or "streamlit" in path.name.lower()
    )


class Locations:
    """Relocate only explicit original-E6 descendants; shared pins stay shared."""

    def __init__(self, original: Path, restored: Path) -> None:
        self.original, self.restored = original.resolve(), restored.resolve()

    def path(self, reference: dict[str, Any]) -> Path:
        path = Path(reference["path"])
        if not path.is_absolute():
            raise ResearchError("e6_recovery_expected_absolute_artifact_pin")
        if path.is_relative_to(self.original):
            path = self.restored / path.relative_to(self.original)
        return checked({**reference, "path": str(path)})


def ready(root: Path) -> dict[str, Any]:
    protocol = read(checked(read(root / "freeze.json")["protocol"]))
    for name in REQUIRED - {"producer-final.zip", "producer-final.json"}:
        if not (root / name).is_file():
            raise ResearchError("e6_recovery_scientific_delivery_not_ready:" + name)
    if not any((root / "cito" / name).is_file() for name in ("summary-v2.json", "summary.json")):
        raise ResearchError("e6_recovery_cito_not_ready")
    for spec in protocol["matrix"]:
        path = root / "trials" / spec["id"] / "receipt.json"
        if not path.is_file() or read(path).get("status") not in {"completed", "failed", "skipped"}:
            raise ResearchError("e6_recovery_declared_trial_not_terminal")
    for path in (root / "trials").glob("*/receipt.json"):
        if read(path).get("status") not in {"completed", "failed", "skipped"}:
            raise ResearchError("e6_recovery_active_trial")
    for fold in protocol.get("folds", []):
        names = ["D-" + fold["name"], "direction-" + fold["name"]]
        if fold["name"] != "earlier":
            names.append("C-" + fold["name"])
        for name in names:
            if not (root / "evaluations" / name / "result.json").is_file():
                raise ResearchError("e6_recovery_evaluation_not_ready:" + name)
    return protocol


def capture_source(root: Path, repository: Path) -> Path:
    """Capture final code/lock and preserve exact historical producer references."""
    root, repository = root.resolve(), repository.resolve()
    ready(root)
    source_files = [
        *sorted((repository / "src").rglob("*.py")),
        *sorted((repository / "src").rglob("*.sql")),
        *[
            repository / name
            for name in ("pyproject.toml", "uv.lock", "requirements.txt", "research_dashboard.py")
        ],
    ]
    entries = [
        {**pin(path), "relative": path.relative_to(repository).as_posix()} for path in source_files
    ]
    receipt_path = root / "producer-final.json"
    if receipt_path.exists():
        receipt = read(receipt_path)
        if receipt["entries"] != entries:
            raise ResearchError("e6_recovery_final_producer_changed")
        checked(receipt["archive"])
        for alias in receipt["historical_source_aliases"]:
            checked(alias["retained"])
        return root / "producer-final.zip"
    archive_path = root / "producer-final.zip"
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, entry in zip(source_files, entries, strict=True):
            archive.write(path, entry["relative"])
    _verify_archive(archive_path, [{**entry, "path": entry["relative"]} for entry in entries])
    available = {entry["sha256"] for entry in entries}
    aliases = []
    references = [
        ref
        for path in root.rglob("*.json")
        if not disallowed(path.relative_to(root).as_posix())
        for ref in pins(read(path))
        if Path(ref["path"]).suffix.lower() in {".py", ".sql"}
    ]
    archives = [archive_path, *sorted(root.glob("producer-*.zip"))]
    for ref in references:
        if ref["sha256"] in available:
            continue
        payload = None
        live = Path(ref["path"])
        if live.is_file() and digest(live) == ref["sha256"]:
            payload = live.read_bytes()
        for candidate in archives:
            if payload is not None:
                break
            with zipfile.ZipFile(candidate) as archive:
                for name in archive.namelist():
                    if Path(name).name == live.name:
                        value = archive.read(name)
                        if hashlib.sha256(value).hexdigest() == ref["sha256"]:
                            payload = value
                            break
        # A separately preserved Cito producer is an ordinary included file.
        if payload is None:
            for candidate in root.rglob("*.py"):
                if digest(candidate) == ref["sha256"]:
                    payload = candidate.read_bytes()
                    break
        if payload is None or len(payload) != ref["size_bytes"]:
            raise ResearchError("e6_recovery_exact_historical_producer_unavailable:" + live.name)
        retained = root / "evidence/source-history" / (ref["sha256"] + live.suffix)
        retained.parent.mkdir(parents=True, exist_ok=True)
        if retained.exists() and retained.read_bytes() != payload:
            raise ResearchError("e6_recovery_historical_source_conflict")
        if not retained.exists():
            retained.write_bytes(payload)
        aliases.append({"original": ref, "retained": pin(retained)})
        available.add(ref["sha256"])
    frozen_json(
        receipt_path,
        {
            "version": "e6-final-producer-v1",
            "archive": pin(archive_path),
            "entries": entries,
            "historical_source_aliases": aliases,
            "created_utc": utc(),
        },
    )
    return archive_path


def dependencies(root: Path, files: list[str]) -> list[dict[str, Any]]:
    """Direct research pins, plus the explicitly consumed shared evaluation files."""
    root = root.resolve()
    listed = set(files)
    producer = read(root / "producer-final.json")
    checked(producer["archive"])
    for alias in producer["historical_source_aliases"]:
        checked(alias["retained"])
    code_hashes = {x["sha256"] for x in producer["entries"]}
    code_hashes |= {x["original"]["sha256"] for x in producer["historical_source_aliases"]}
    external: dict[str, dict[str, Any]] = {}

    def admit(reference: dict[str, Any], parent: Path) -> None:
        path = Path(reference["path"])
        path = path if path.is_absolute() else parent / path
        if path.suffix.lower() in DATABASE_SUFFIXES:
            raise ResearchError("e6_recovery_operating_database_dependency_forbidden")
        if reference["sha256"] in code_hashes:
            return
        path = checked({**reference, "path": str(path)}).resolve()
        if path.is_relative_to(root):
            if path.relative_to(root).as_posix() not in listed:
                raise ResearchError("e6_recovery_missing_internal_dependency")
            return
        external[str(path)] = {**reference, "path": str(path)}

    for relative in files:
        path = root / relative
        if path.suffix.lower() != ".json":
            continue
        for ref in pins(read(path)):
            admit(ref, path.parent)
    # A replay attaches these accepted saved outcomes. Do not follow unrelated
    # foundation/recovery/source graphs from the accepted protocol metadata.
    protocol = read(root / "protocol.json")
    for fold in ("spring", "summer"):
        ref = protocol.get("pins", {}).get(f"e5_evaluations_{fold}_result")
        if ref:
            path = checked(ref)
            for child in read(path)["files"].values():
                admit(child, path.parent)
        for method in ("rmse", "quantile"):
            trial = protocol.get("pins", {}).get(f"e5_trial_{fold}-{method}-current")
            if trial:
                path = checked(trial)
                admit(read(path)["files"]["predictions.parquet"], path.parent)
    population = protocol.get("pins", {}).get("e5_population_report")
    if population:
        path = checked(population)
        admit(read(path)["artifacts"][0], path.parent)
    return [external[key] for key in sorted(external)]


def create_inventory(root: Path, output: Path) -> Path:
    root = root.resolve()
    ready(root)
    if output.exists():
        raise ResearchError("e6_recovery_inventory_requires_unused_path")
    files = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not disallowed(p.relative_to(root).as_posix())
    )
    if REQUIRED - set(files):
        raise ResearchError("e6_recovery_final_source_capture_required")
    external = dependencies(root, files)
    # The inventory is outside its own payload to avoid self-referential hashes.
    frozen_json(
        output,
        {
            "version": "e6-explicit-inventory-v1",
            "source_root": str(root),
            "files": files,
            "external": external,
            "created_utc": utc(),
        },
    )
    return output


def prepare_capsule(root: Path, destination: Path, inventory_path: Path) -> Path:
    root, destination = root.resolve(), destination.resolve()
    protocol = ready(root)
    expected_destination = root.parent / "recovery/research-e6-v1"
    if destination != expected_destination.resolve():
        raise ResearchError("e6_recovery_requires_declared_capsule_destination")
    inventory = read(inventory_path)
    if Path(inventory["source_root"]).resolve() != root:
        raise ResearchError("e6_recovery_inventory_root_mismatch")
    if destination.exists():
        raise ResearchError("e6_recovery_capsule_requires_unused_destination")
    files = inventory["files"]
    if len(files) != len(set(files)) or REQUIRED - set(files):
        raise ResearchError("e6_recovery_incomplete_or_duplicate_inventory")
    expected_files = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
        and not disallowed(p.relative_to(root).as_posix())
        and p.resolve() != inventory_path.resolve()
    }
    if set(files) != expected_files:
        raise ResearchError("e6_recovery_inventory_missing_or_new_artifacts")
    payload = []
    for relative in files:
        e3_recovery.safe_relative(relative)
        path = root / relative
        if disallowed(relative) or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ResearchError("e6_recovery_forbidden_payload")
        payload.append({**pin(path), "path": relative})
    external = dependencies(root, files)
    if inventory["external"] != external:
        raise ResearchError("e6_recovery_dependency_inventory_changed")
    limit = min(protocol["resource_limits"]["recovery_bytes"], 8 * 1024**3)
    destination.mkdir(parents=True)
    archive_path = destination / "payload.zip"
    with zipfile.ZipFile(
        archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True
    ) as archive:
        for item in payload:
            archive.write(root / item["path"], item["path"])
            if archive_path.stat().st_size > limit:
                raise ResearchError("e6_recovery_capsule_storage_limit_failure_retained")
    if archive_path.stat().st_size > limit:
        raise ResearchError("e6_recovery_capsule_storage_limit_failure_retained")
    _verify_archive(archive_path, payload)
    (destination / "inventory.json").write_bytes(inventory_path.read_bytes())
    manifest = {
        "version": VERSION,
        "source_root": str(root),
        "payload": payload,
        "external": external,
        "archive": {**pin(archive_path), "path": "payload.zip"},
        "inventory": {**pin(destination / "inventory.json"), "path": "inventory.json"},
        "limits": {"recovery_bytes": limit},
        "created_utc": utc(),
        "scope": (
            "Local existing-runtime E6 research recovery; shared dependencies pinned; "
            "no operating database, fresh-machine or off-device claim."
        ),
    }
    manifest_size = len(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()) + 1
    total_bytes = archive_path.stat().st_size + (destination / "inventory.json").stat().st_size
    if total_bytes + manifest_size > limit:
        raise ResearchError("e6_recovery_capsule_storage_limit_failure_retained")
    frozen_json(destination / "recovery.json", manifest)
    return destination / "recovery.json"


def _verify_archive(path: Path, payload: list[dict[str, Any]]) -> None:
    expected = {item["path"]: item for item in payload}
    if len(expected) != len(payload):
        raise ResearchError("e6_recovery_duplicate_payload")
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != set(expected):
            raise ResearchError("e6_recovery_archive_inventory_changed")
        for name, ref in expected.items():
            e3_recovery.safe_relative(name)
            hasher, size = hashlib.sha256(), 0
            with archive.open(name) as stream:
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
                    size += len(chunk)
            if hasher.hexdigest() != ref["sha256"] or size != ref["size_bytes"]:
                raise ResearchError("e6_recovery_archive_bytes_changed")


def verify_restored(bundle: Path, restored: Path) -> dict[str, Any]:
    manifest = read(bundle / "recovery.json")
    if manifest["version"] != VERSION:
        raise ResearchError("e6_recovery_wrong_version")
    checked({**manifest["archive"], "path": str(bundle / manifest["archive"]["path"])})
    for ref in manifest["external"]:
        checked(ref)
    expected = {item["path"] for item in manifest["payload"]}
    actual = {p.relative_to(restored).as_posix() for p in restored.rglob("*") if p.is_file()}
    if expected != actual:
        raise ResearchError("e6_recovery_restored_inventory_changed")
    for item in manifest["payload"]:
        e3_recovery.safe_relative(item["path"])
        path = restored / item["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(restored.resolve()):
            raise ResearchError("e6_recovery_restored_path_escape")
        checked({**item, "path": str(path)})
    return {
        "status": "byte_verified",
        "payload_files": len(expected),
        "shared_dependencies": len(manifest["external"]),
    }


def restore(bundle: Path, restored: Path, receipt: Path) -> dict[str, Any]:
    if restored.exists() or receipt.exists():
        raise ResearchError("e6_recovery_restore_requires_unused_paths")
    manifest = read(bundle / "recovery.json")
    if manifest["version"] != VERSION:
        raise ResearchError("e6_recovery_wrong_version")
    archive_path = checked(
        {**manifest["archive"], "path": str(bundle / manifest["archive"]["path"])}
    )
    for item in manifest["external"]:
        checked(item)
    _verify_archive(archive_path, manifest["payload"])
    restored.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        for item in manifest["payload"]:
            path = restored / item["path"]
            if not path.resolve().is_relative_to(restored.resolve()):
                raise ResearchError("e6_recovery_restore_path_escape")
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item["path"]) as source, path.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
    result = verify_restored(bundle, restored)
    result.update(bundle=pin(bundle / "recovery.json"), restored_root=str(restored.resolve()))
    frozen_json(receipt, result)
    return result


def window(
    root: Path, locations: Locations, start: str, end: str, columns: list[str] | None = None
) -> pd.DataFrame:
    if columns is not None:
        columns = list(dict.fromkeys([*columns, "period_start"]))
    frames = []
    for part in read(root / "inputs/manifest.json")["partitions"]:
        if int(start[:4]) <= part["year"] <= int(end[:4]):
            frame = pd.read_parquet(
                locations.path(part["features"]), columns=columns, use_threads=False
            )
            frames.append(frame.loc[frame.period_start.ge(start) & frame.period_start.lt(end)])
    if not frames:
        raise ResearchError("e6_recovery_no_cohort_for_window")
    return pd.concat(frames, ignore_index=True)


def compare_frames(
    actual: pd.DataFrame, expected: pd.DataFrame, keys: list[str], *, exact: bool = True
) -> None:
    if (
        set(actual) != set(expected)
        or actual.duplicated(keys).any()
        or expected.duplicated(keys).any()
    ):
        raise ResearchError("e6_recovery_comparison_columns_or_identity_changed")
    pd.testing.assert_frame_equal(
        actual.sort_values(keys).reset_index(drop=True)[expected.columns],
        expected.sort_values(keys).reset_index(drop=True),
        check_exact=exact,
        check_dtype=False,
        rtol=1e-12,
        atol=1e-10,
    )


def replay_models(
    root: Path, locations: Locations, reproduced: dict[str, pd.DataFrame] | None = None
) -> list[dict[str, Any]]:
    from catboost import CatBoostClassifier, CatBoostRegressor

    from .e5_experiment import predict
    from .training_contract import KEYS

    protocol = read(root / "protocol.json")
    proofs = []
    for path in sorted((root / "trials").glob("*/receipt.json")):
        receipt = read(path)
        if receipt["status"] != "completed":
            continue
        spec = receipt["spec"]
        fold = next(f for f in protocol["folds"] if f["name"] == spec["fold"])
        names, cats = receipt["features"], receipt["categories"]
        columns = list(dict.fromkeys([*KEYS, "aip_ore", *names]))
        candidates = window(root, locations, fold["cal_start"], fold["end"], columns)
        model: Any = CatBoostClassifier() if spec["task"] == "direction" else CatBoostRegressor()
        model.load_model(str(locations.path(receipt["files"]["model.cbm"])))
        prediction = predict(
            model, candidates, {"task": spec["task"], "method": spec["objective"]}, names, cats
        )
        saved = pd.read_parquet(
            locations.path(receipt["files"]["predictions.parquet"]), use_threads=False
        )
        compare_frames(prediction, saved, ["row_key"])
        if reproduced is not None:
            reproduced[spec["id"]] = prediction
        proofs.append({"trial": spec["id"], "rows": len(saved), "exact_raw_and_supported": True})
    return proofs


def replay_a(root: Path, locations: Locations) -> dict[str, Any]:
    from .e6_finance import (
        ORIGIN_FIELDS,
        current_only_frame,
        decision_gates,
        diagnostic_tables,
        evaluate_fold,
        records,
        summaries,
    )

    result = read(root / "investigation-a/results.json")
    forecasts = pd.read_parquet(locations.path(result["files"]["forecasts"]), use_threads=False)
    e4 = pd.read_parquet(
        locations.path(result["binding"]["e4_predictions"]),
        use_threads=False,
        columns=["row_key", *[f"perfect_current_p{k}" for k in range(3)]],
    )
    expected = {
        name: pd.read_parquet(locations.path(result["files"][name]), use_threads=False)
        for name in ("queues", "allocations", "periods")
    }
    periods = []
    population = read(locations.path(result["binding"]["population"]))
    flags = pd.read_parquet(
        locations.path(population["artifacts"][0]),
        columns=["row_key", "behavior50_excluded"],
        use_threads=False,
    )
    for fold, part in forecasts.groupby("fold", sort=True):
        fold = str(fold)
        metadata = read(locations.path(result["binding"]["folds"][fold]["evaluation"]))
        labels = pd.read_parquet(
            locations.path(metadata["files"]["outcomes.parquet"]), use_threads=False
        )
        source = pd.read_parquet(
            locations.path(metadata["files"]["predictions.parquet"]),
            columns=[*ORIGIN_FIELDS, *[f"tuned_current_p{k}" for k in range(3)]],
            use_threads=False,
        )
        points = []
        for method in ("rmse", "quantile"):
            receipt = read(
                locations.path(result["binding"]["folds"][fold]["current_receipts"][method])
            )
            if receipt["spec"]["information"] != "current" or any(
                str(c).startswith("advance") for c in receipt["features"]
            ):
                raise ResearchError("e6_recovery_a_noncurrent_source")
            points.append(
                pd.read_parquet(
                    locations.path(receipt["files"]["predictions.parquet"]), use_threads=False
                )
            )
        assembled = current_only_frame(source, points[0], points[1], flags)
        compare_frames(assembled.assign(fold=fold), part, ["row_key"])
        proof = evaluate_fold(assembled, e4, labels, fold)
        for name in expected:
            keys = ["fold", "policy", "workload", "period_start"]
            if name in {"queues", "allocations"}:
                keys += ["row_key"]
            if name in {"allocations", "periods"}:
                keys += ["allocation"]
            compare_frames(proof[name], expected[name].loc[expected[name].fold.eq(fold)], keys)
        periods.append(proof["periods"])
    summary = summaries(pd.concat(periods, ignore_index=True))
    compare_frames(
        pd.DataFrame(summary),
        pd.DataFrame(result["summary"]),
        ["fold", "policy", "workload", "allocation"],
        exact=False,
    )
    if decision_gates(summary) != result["decision_gates"]:
        raise ResearchError("e6_recovery_a_decision_changed")
    diagnostics_path = root / "investigation-a/diagnostics.json"
    if diagnostics_path.exists():
        diagnostic = diagnostic_tables(
            pd.concat(periods, ignore_index=True), expected["allocations"]
        )
        saved_diagnostic = read(diagnostics_path)
        if diagnostic["paired_period_bootstrap"] != saved_diagnostic["paired_period_bootstrap"]:
            raise ResearchError("e6_recovery_a_paired_diagnostic_changed")
        if (
            records(pd.DataFrame(diagnostic["group_concentration"]))
            != saved_diagnostic["group_concentration"]
        ):
            raise ResearchError("e6_recovery_a_group_concentration_changed")
    return {
        "exact_queues": True,
        "exact_current_only_assembly": True,
        "exact_quantities_and_exposure": True,
        "summary_tolerance": {"rtol": 1e-12, "atol": 1e-10},
        "rows": len(forecasts),
    }


def replay_b(root: Path, locations: Locations) -> dict[str, Any]:
    from .e3_methods import select_queue
    from .e4_assessment import predict
    from .e6_cito import REPLAY_COLUMNS, SCENARIOS, corrected_inputs, no_report_inputs, summarize

    path = root / "cito/summary-v2.json"
    if not path.exists():
        path = root / "cito/summary.json"
    summary = read(path)
    sources = summary["binding"]["sources"]
    inputs = pd.read_parquet(locations.path(sources["inputs"]), use_threads=False)
    comparison = pd.read_parquet(locations.path(sources["comparison_rows"]), use_threads=False)
    corrected, _ = corrected_inputs(inputs, comparison)
    compare_frames(
        corrected,
        pd.read_parquet(
            locations.path(summary["files"]["corrected-inputs.parquet"]), use_threads=False
        ),
        ["row_key"],
    )
    compare_frames(
        no_report_inputs(inputs),
        pd.read_parquet(
            locations.path(summary["files"]["no-report-inputs.parquet"]), use_threads=False
        ),
        ["row_key"],
    )
    current_calibration = copy.deepcopy(read(locations.path(sources["calibration"])))
    temperature = current_calibration["states"]["2026"]["temperature"]
    temperature["hybrid"] = copy.deepcopy(temperature["current"])
    if current_calibration != read(locations.path(summary["files"]["calibration-current.json"])):
        raise ResearchError("e6_recovery_b_current_calibration_derivation_changed")
    proofs = []
    clean_actual = None
    actual_count = None
    for scenario in SCENARIOS:
        reference = (
            sources["inputs"]
            if scenario == "actual"
            else summary["files"][
                "corrected-inputs.parquet"
                if scenario == "corrected_wrong_only"
                else "no-report-inputs.parquet"
            ]
        )
        calibration = (
            summary["files"]["calibration-current.json"]
            if scenario == "no_report_current"
            else sources["calibration"]
        )
        frame = pd.read_parquet(locations.path(reference), use_threads=False)
        predicted = predict(frame, locations.path(sources["models"]), locations.path(calibration))
        expected = pd.read_parquet(
            locations.path(summary["files"][scenario + "_predictions"]), use_threads=False
        )
        compare_frames(predicted[REPLAY_COLUMNS], expected[REPLAY_COLUMNS], ["row_key"])
        scored = pd.read_parquet(
            locations.path(summary["files"][scenario + "_outcomes"]), use_threads=False
        )
        if summarize(scored) != summary["results"][scenario]:
            raise ResearchError("e6_recovery_b_saved_summary_changed")
        if "clean_matched" in summary:
            clean = pd.read_parquet(
                locations.path(summary["files"]["clean_matched_" + scenario]), use_threads=False
            )
            eligible = predicted.prediction_state.eq("available")
            if actual_count is None:
                actual_count = int(predicted.selected.sum())
            use = predicted.loc[
                eligible, ["row_key", "period_start", "p_fall", "p_unchanged", "p_rise"]
            ]
            use = use.rename(columns={"p_fall": "p0", "p_unchanged": "p1", "p_rise": "p2"})
            workload = {str(day)[:10]: actual_count for day in use.period_start.unique()}
            selected = select_queue(use, "rise_rank", workload=workload)
            chosen = pd.Series(False, index=predicted.index)
            chosen.loc[eligible] = selected.selected.to_numpy()
            if clean_actual is None:
                clean_actual = chosen.copy()
            comparison = pd.DataFrame(
                {
                    "row_key": predicted.row_key,
                    "selected_matched_same_rule": chosen,
                    "actual_selected_matched_same_rule": clean_actual,
                    "added_matched_same_rule": chosen & ~clean_actual,
                    "removed_matched_same_rule": ~chosen & clean_actual,
                    "retained_matched_same_rule": chosen & clean_actual,
                }
            )
            compare_frames(comparison, clean[list(comparison)], ["row_key"])
            for role in ("selected", "added", "removed", "retained"):
                clean[role + "_matched"] = clean[role + "_matched_same_rule"]
            clean_summary = summarize(clean)
            for group in clean_summary["groups"].values():
                del group["queues"]["natural"]
            if clean_summary != summary["clean_matched"]["results"][scenario]:
                raise ResearchError("e6_recovery_b_clean_matched_summary_changed")
        proofs.append({"scenario": scenario, "rows": len(frame), "exact_inference": True})
    return {"arms": proofs, "saved_outcome_summaries_verified": True}


def align_saved(frame: pd.DataFrame, saved: pd.DataFrame) -> pd.DataFrame:
    if frame.row_key.duplicated().any() or saved.row_key.duplicated().any():
        raise ResearchError("e6_recovery_duplicate_prediction_identity")
    if not frame.row_key.isin(saved.row_key).all():
        raise ResearchError("e6_recovery_missing_prediction_identity")
    return saved.set_index("row_key").reindex(frame.row_key).reset_index()


def reconstruct_evaluation(
    root: Path,
    locations: Locations,
    result: dict[str, Any],
    saved: pd.DataFrame,
    reproduced: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Link evaluated vectors to re-inferred models/shared accepted predictions."""
    from .e3_methods import calibrate
    from .e4_methods import mixed, route
    from .e6_results import queue_diagnostics

    fold_name = result["summaries"][0]["fold"]
    fold = next(f for f in read(root / "protocol.json")["folds"] if f["name"] == fold_name)
    base = window(root, locations, fold["cal_start"], fold["end"])
    accepted_cache: dict[str, pd.DataFrame] = {}

    def source(trial: str) -> pd.DataFrame:
        if trial in reproduced:
            return align_saved(base, reproduced[trial])
        if trial not in accepted_cache:
            reused = result.get("reused", {}).get(trial)
            if reused is None:
                raise ResearchError("e6_recovery_missing_reinferred_or_accepted_component:" + trial)
            references = reused["files"]
            if isinstance(references, dict):
                references = references.values()
            reference = next(
                ref for ref in references if Path(ref["path"]).name == "predictions.parquet"
            )
            accepted_cache[trial] = pd.read_parquet(locations.path(reference), use_threads=False)
        return align_saved(base, accepted_cache[trial])

    direction = result["version"] == "e6-direction-results-v1"
    grouping = (
        ["history", "route", "information"] if direction else ["history", "method", "information"]
    )
    caches: dict[tuple[str, str], dict[str, np.ndarray]] = {}
    queue_frames, queue_periods = [], []
    for tags, part in saved.groupby(grouping, sort=True):
        history, method, mode = (str(tag) for tag in tags)
        cache_key = (history, "direction" if direction else method)
        if cache_key not in caches:
            values: dict[str, np.ndarray] = {}
            for information in ("current", "advance"):
                if history == "3y":
                    if direction and fold_name == "earlier":
                        trial = f"evaluation_{fold['reference_year']}-cb_broad_{information}"
                    else:
                        vintage = "screen" if fold_name == "earlier" else fold_name
                        trial = f"{vintage}-{'fresh' if direction else method}-{information}"
                else:
                    task = "direction" if direction else "amount"
                    trial = f"{fold_name}-{task}-{history}-{information}"
                if not direction and method == "huber":
                    trial = f"{fold_name}-huber-{information}"
                prediction = source(trial)
                if direction:
                    columns = (
                        [f"p{k}_perfect" for k in range(3)]
                        if trial.startswith("evaluation_")
                        else [f"p{k}" for k in range(3)]
                    )
                    broad = prediction[columns].to_numpy()
                    fallback = [
                        source(f"evaluation_{fold['reference_year']}-cb_{name}_{information}")[
                            [f"p{k}_perfect" for k in range(3)]
                        ].to_numpy()
                        for name in ("package", "equal")
                    ]
                    values["component_" + information] = broad
                    values["routed_" + information] = route(base, fallback[0], fallback[1], broad)[
                        0
                    ]
                else:
                    raw: np.ndarray = prediction.raw_delta_dkk.to_numpy()
                    values["raw_" + information] = raw
                    values["supported_" + information] = (
                        np.maximum(0, base.aip_ore.to_numpy() / 100 + raw)
                        - base.aip_ore.to_numpy() / 100
                    )
            if direction:
                values["routed_hybrid"] = mixed(
                    values["routed_advance"], values["routed_current"], base.advance_abc
                )
            else:
                for support in ("raw", "supported"):
                    values[support + "_hybrid"] = np.where(
                        base.advance_abc.isin(["A", "B", "C"]),
                        values[support + "_advance"],
                        values[support + "_current"],
                    )
            caches[cache_key] = values
        values = caches[cache_key]
        if direction:
            raw = values[method + "_" + mode]
            state = result["calibration"][f"D_{history}_{method}_{mode}"]
            if state.get("cutoff") != fold["start"]:
                raise ResearchError("e6_recovery_temperature_cutoff_changed")
            calibrated = calibrate(raw, state)
            reconstructed = pd.DataFrame({"row_key": base.row_key})
            reconstructed[[f"raw_p{k}" for k in range(3)]] = raw
            reconstructed[[f"p{k}" for k in range(3)]] = calibrated
        else:
            reconstructed = pd.DataFrame(
                {
                    "row_key": base.row_key,
                    "raw_delta_dkk": values["raw_" + mode],
                    "delta_dkk": values["supported_" + mode],
                }
            )
            reconstructed["next_price_dkk"] = (
                base.aip_ore.to_numpy() / 100 + reconstructed.delta_dkk
            )
            reconstructed["support_adjusted"] = ~np.isclose(
                reconstructed.raw_delta_dkk, reconstructed.delta_dkk, rtol=0, atol=1e-12
            )
        reconstructed = align_saved(part, reconstructed)
        columns = [str(column) for column in reconstructed if column in part]
        compare_frames(reconstructed[columns], part[columns], ["row_key"])
        if direction and method == "routed":
            queue, per_period = queue_diagnostics(
                part.reset_index(drop=True), reconstructed[[f"p{k}" for k in range(3)]].to_numpy()
            )
            queue["history"], queue["information"] = history, mode
            queue_frames.append(queue)
            queue_periods.extend(
                [
                    {**row, "history": history, "information": mode, "fold": fold_name}
                    for row in per_period
                ]
            )
    if direction:
        expected_queues = pd.read_parquet(
            locations.path(result["files"]["queues.parquet"]), use_threads=False
        )
        compare_frames(
            pd.concat(queue_frames, ignore_index=True),
            expected_queues,
            ["row_key", "history", "information"],
        )
        compare_frames(
            pd.DataFrame(queue_periods),
            pd.DataFrame(result["queue_periods"]),
            ["fold", "history", "information", "period_start"],
            exact=False,
        )
    return {
        "rows": len(saved),
        "exact_evaluation_vectors": True,
        "exact_direction_queues": direction,
        "frozen_calibration_reused": direction,
    }


def replay_scores(
    root: Path, locations: Locations, reproduced: dict[str, pd.DataFrame] | None = None
) -> dict[str, Any]:
    from .e6_results import evaluate_values

    count = 0
    if reproduced is None:
        reproduced = {}
        replay_models(root, locations, reproduced)
    all_periods = []
    all_summaries = []
    vector_proofs = []
    for path in sorted((root / "evaluations").glob("*/result.json")):
        result = read(path)
        saved = pd.read_parquet(
            locations.path(result["files"]["predictions.parquet"]), use_threads=False
        )
        if saved.empty:
            if result["summaries"]:
                raise ResearchError("e6_recovery_empty_scores_have_summaries")
            continue
        vector_proofs.append(reconstruct_evaluation(root, locations, result, saved, reproduced))
        fold_name = result["summaries"][0]["fold"]
        fold = next(f for f in read(root / "protocol.json")["folds"] if f["name"] == fold_name)
        origin = window(
            root,
            locations,
            fold["start"],
            fold["end"],
            ["row_key", "aip_ore", "history_periods", "peer_count"],
        )
        direction = result["version"] == "e6-direction-results-v1"
        grouping = (
            ["history", "route", "information"]
            if direction
            else ["history", "method", "information"]
        )
        summaries, periods = [], []
        for arm, part in saved.groupby(grouping, sort=True):
            # Saved outcomes remain separately dated; join only origin strata.
            frame = part.merge(
                origin.drop(columns=[c for c in origin if c != "row_key" and c in part]),
                on="row_key",
                how="left",
                validate="many_to_one",
            ).reset_index(drop=True)
            if frame.history_periods.isna().any():
                raise ResearchError("e6_recovery_score_origin_missing")
            identity = {
                "investigation": result["summaries"][0]["investigation"],
                "fold": fold_name,
                "task": "direction" if direction else "amount",
                **dict(zip(grouping, arm, strict=True)),
            }
            choices = (
                (
                    ("raw", [f"raw_p{k}" for k in range(3)]),
                    ("calibrated", [f"p{k}" for k in range(3)]),
                )
                if direction
                else (("raw", "raw_delta_dkk"), ("supported", "delta_dkk"))
            )
            for kind, columns in choices:
                identity["calibration" if direction else "support"] = kind
                values = frame[columns].to_numpy()
                summary_rows, period_rows = evaluate_values(
                    frame, values, identity["task"], identity
                )
                summaries.extend(summary_rows)
                periods.extend(period_rows)
        keys = [
            key
            for key in pd.DataFrame(summaries)
            if key
            in {
                "investigation",
                "fold",
                "task",
                "history",
                "information",
                "route",
                "method",
                "calibration",
                "support",
                "stratum",
            }
        ]
        compare_frames(
            pd.DataFrame(summaries), pd.DataFrame(result["summaries"]), keys, exact=False
        )
        saved_periods = pd.read_parquet(
            locations.path(result["files"]["periods.parquet"]), use_threads=False
        )
        compare_frames(pd.DataFrame(periods), saved_periods, [*keys, "period_start"], exact=False)
        all_periods.append(pd.DataFrame(periods))
        all_summaries.extend(summaries)
        count += len(summaries)
    contrasts = replay_contrasts(root, all_summaries, all_periods)
    return {
        "recomputed_summaries": count,
        "reconstructed_prediction_vectors": vector_proofs,
        "recomputed_model_contrasts": contrasts,
        "rtol": 1e-12,
        "atol": 1e-10,
        "scope": "All C/D fold/stratum metrics recomputed from saved keyed predictions.",
    }


def replay_contrasts(
    root: Path, summaries: list[dict[str, Any]], periods: list[pd.DataFrame]
) -> int:
    from .e6_results import paired_interval

    if not (root / "model-results.json").exists() or not periods:
        return 0
    combined = read(root / "model-results.json")
    if "summaries" in combined:
        actual = pd.DataFrame(summaries)
        keys = [
            name
            for name in (
                "investigation",
                "fold",
                "task",
                "history",
                "information",
                "route",
                "method",
                "calibration",
                "support",
                "stratum",
            )
            if name in actual
        ]
        compare_frames(actual, pd.DataFrame(combined["summaries"]), keys, exact=False)
    table = pd.concat(periods, ignore_index=True)
    table = table.loc[table.stratum.eq("all")]
    count = 0
    for contrast in combined.get("contrasts", []):
        part = table.loc[
            table.investigation.eq(contrast["investigation"])
            & table.task.eq(contrast["task"])
            & table.information.eq(contrast["information"])
            & table.fold.isin(["spring", "summer"] if contrast["era"] == "2026" else ["earlier"])
        ]
        if contrast["task"] == "amount":
            part = part.loc[part.support.eq("supported")]
        else:
            part = part.loc[part.route.eq("routed") & part.calibration.eq("calibrated")]
        arm = "method" if contrast["investigation"] == "C" else "history"
        metric = contrast["metric"]
        reference = part.loc[part[arm].eq(contrast["reference"])].set_index("period_start")
        candidate = part.loc[part[arm].eq(contrast["candidate"])].set_index("period_start")
        joined = (
            reference[[metric]].join(candidate[[metric]], lsuffix="_ref", rsuffix="_new").dropna()
        )
        difference = (joined[metric + "_new"] - joined[metric + "_ref"]).to_numpy()
        actual_intervals = [
            paired_interval(difference, item["block"]) for item in contrast["intervals"]
        ]
        if actual_intervals != contrast["intervals"]:
            raise ResearchError("e6_recovery_paired_model_contrast_changed")
        count += 1
    return count


def verify_producer(archive_path: Path, producer: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        actual = {p.relative_to(producer).as_posix() for p in producer.rglob("*") if p.is_file()}
        if len(names) != len(set(names)) or actual != set(names):
            raise ResearchError("e6_recovery_captured_producer_inventory_changed")
        for name in names:
            e3_recovery.safe_relative(name)
            path = producer / name
            if path.is_symlink() or path.read_bytes() != archive.read(name):
                raise ResearchError("e6_recovery_captured_producer_bytes_changed")


def replay_local(root: Path, original: Path) -> dict[str, Any]:
    if original.exists():
        raise ResearchError("e6_recovery_original_root_must_be_unavailable")
    locations = Locations(original, root)
    reproduced: dict[str, pd.DataFrame] = {}
    models = replay_models(root, locations, reproduced)
    return {
        "models": models,
        "A": replay_a(root, locations),
        "B": replay_b(root, locations),
        "scores": replay_scores(root, locations, reproduced),
    }


REPLAY_SCRIPT = r"""
import json,sys,socket,sqlite3,time,threading
from pathlib import Path
import psutil,duckdb,catboost
config=json.loads(sys.argv[1])
def forbidden(*args,**kwargs):
    raise RuntimeError('e6_recovery_fit_database_network_forbidden')
socket.create_connection=forbidden
socket.getaddrinfo=forbidden
socket.socket.connect=forbidden
socket.socket.connect_ex=forbidden
duckdb.connect=forbidden
duckdb.sql=forbidden
duckdb.query=forbidden
sqlite3.connect=forbidden
for cls in (catboost.CatBoost,catboost.CatBoostClassifier,catboost.CatBoostRegressor):
    for name in ('fit','grid_search','randomized_search'):
        setattr(cls,name,forbidden)
catboost.train=forbidden
catboost.cv=forbidden
from bef_redbird.research import e3_methods,e4_methods,e5_results
for module in (e3_methods,e4_methods,e5_results):
    for name in ('temperature_fit','residual_fit','fit_residual',
                 'temperature_once','binary_temperature'):
        if hasattr(module,name):
            setattr(module,name,forbidden)
from bef_redbird.research import e6_delivery
if not Path(e6_delivery.__file__).resolve().is_relative_to(Path(config['producer'])/'src'):
    raise RuntimeError('e6_recovery_did_not_import_captured_code')
peak=[psutil.Process().memory_info().rss]
stop=threading.Event()
def sample():
    while not stop.wait(.1):
        peak[0]=max(peak[0],psutil.Process().memory_info().rss)
thread=threading.Thread(target=sample,daemon=True)
thread.start()
try:
    proof=e6_delivery.replay_local(Path(config['restored']),Path(config['original']))
finally:
    stop.set();thread.join()
if peak[0]>config['rss_limit']:
    raise RuntimeError('e6_recovery_memory_limit')
print(json.dumps({'proof':proof,'peak_rss_bytes':peak[0],
    'guards':'CatBoost fitting/search, socket networking, DuckDB and SQLite blocked',
    'captured_producer':True}))
"""


def verify_replay(bundle: Path, restored: Path, producer: Path, receipt: Path) -> dict[str, Any]:
    manifest = read(bundle / "recovery.json")
    original = Path(manifest["source_root"])
    if original.exists():
        raise ResearchError("e6_recovery_original_root_must_be_unavailable")
    if receipt.exists() or producer.exists():
        raise ResearchError("e6_recovery_replay_requires_unused_producer_and_receipt")
    before = verify_restored(bundle, restored)
    archive_path = restored / "producer-final.zip"
    e3_recovery.extract_source(archive_path, producer)
    verify_producer(archive_path, producer)
    config = {
        "original": str(original),
        "restored": str(restored.resolve()),
        "producer": str(producer.resolve()),
        "rss_limit": 28 * 1024**3,
    }
    environment = dict(
        os.environ,
        PYTHONPATH=str(producer.resolve() / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-c", REPLAY_SCRIPT, json.dumps(config)],
            cwd=producer,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        output = {"returncode": -1, "stdout": str(exc.stdout), "stderr": "Replay timed out"}
    unchanged = verify_restored(bundle, restored)
    verify_producer(archive_path, producer)
    if before != unchanged:
        raise ResearchError("e6_recovery_replay_changed_restore")
    result = {
        "version": "e6-guarded-replay-v1",
        "status": "verified" if output["returncode"] == 0 else "failed",
        "binding": {"capsule": pin(bundle / "recovery.json"), "producer": pin(archive_path)},
        "original_root_unavailable": True,
        "restored_hashes_unchanged": True,
        "created_utc": utc(),
        **output,
    }
    if output["returncode"] == 0:
        result["replay"] = json.loads(str(output["stdout"]).strip().splitlines()[-1])
    frozen_json(receipt, result)
    if output["returncode"]:
        raise ResearchError("e6_recovery_guarded_replay_failed_receipt_retained")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["source", "inventory", "capsule", "restore", "verify", "replay"]
    )
    for name in ("root", "repository", "destination", "inventory", "bundle", "producer", "receipt"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.command == "source" and args.root and args.repository:
        result: Any = capture_source(args.root, args.repository)
    elif args.command == "inventory" and args.root and args.inventory:
        result = create_inventory(args.root, args.inventory)
    elif args.command == "capsule" and args.root and args.destination and args.inventory:
        result = prepare_capsule(args.root, args.destination, args.inventory)
    elif args.command == "restore" and args.bundle and args.destination and args.receipt:
        result = restore(args.bundle, args.destination, args.receipt)
    elif args.command == "verify" and args.bundle and args.destination:
        result = verify_restored(args.bundle, args.destination)
    elif args.command == "replay" and args.bundle and args.root and args.producer and args.receipt:
        result = verify_replay(args.bundle, args.root, args.producer, args.receipt)
    else:
        parser.error("Required arguments missing")
    print(result)


if __name__ == "__main__":
    main()
