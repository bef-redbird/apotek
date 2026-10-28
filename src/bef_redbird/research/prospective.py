"""Manual, immutable twelve-origin amount comparison. Never fits or opens a database."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor

from .dataset import ResearchError
from .e3_methods import calibrate, select_queue
from .e4_assessment import attach_outcome as e4_attach_outcome
from .e4_assessment import bind_report, validate_features
from .e4_common import checked, identity, read, utc
from .e4_methods import mixed, route
from .e5_experiment import predict as component_predict
from .group_features import pin

HISTORIES = ("3y", "6y", "full")
MODES = ("current", "hybrid")
PROBS = ["p0", "p1", "p2"]
VERSION = "manual-prospective-comparison-v1"


def now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: str) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if result.tzinfo is None:
        raise ResearchError("prospective_timezone_required")
    return result.tz_convert("UTC")


def cutoff(value: str) -> pd.Timestamp:
    """An explicit offset must agree with Copenhagen, including DST boundaries."""
    stamp = pd.Timestamp(value)
    timestamp(value)
    local = stamp.tz_convert(ZoneInfo("Europe/Copenhagen"))
    if stamp.utcoffset() != local.utcoffset():
        raise ResearchError("prospective_cutoff_not_copenhagen_offset")
    return stamp.tz_convert("UTC")


def write_once(path: Path, value: Any) -> None:
    """Exclusive publication: interrupted temporary bytes can never become a receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    if path.exists():
        if read(path) != value:
            raise ResearchError("prospective_immutable_conflict")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    # Hard-link creation is exclusive and atomic on the supported local filesystem.
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def publish_directory(source: Path, destination: Path) -> None:
    def native(path: Path) -> str:
        absolute = str(path.resolve())
        return "\\\\?\\" + absolute if os.name == "nt" else absolute

    os.rename(native(source), native(destination))


@contextmanager
def writer(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".writer.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ResearchError("prospective_writer_busy_or_interrupted_inspect_lock") from exc
    try:
        os.write(descriptor, json.dumps({"pid": os.getpid(), "started_utc": utc()}).encode())
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "pandas", "pyarrow", "catboost", "scipy")
        },
    }


def producer() -> dict[str, Any]:
    directory = Path(__file__).parent
    names = (
        "prospective.py",
        "prospective_report.py",
        "e4_assessment.py",
        "e4_methods.py",
        "e3_methods.py",
        "e5_experiment.py",
        "e2_models.py",
        "e4_common.py",
        "group_features.py",
        "training_contract.py",
        "e2_data.py",
    )
    return {name: pin(directory / name) for name in names}


def require(root: Path) -> dict[str, Any]:
    config = read(root / "config.json")
    protocol = read(checked(config["protocol"]))
    for item in protocol["producer"].values():
        checked(item)
    if protocol["environment"] != environment():
        raise ResearchError("prospective_environment_changed")
    for item in protocol["amount_models"].values():
        for mode in ("current", "advance"):
            checked(item[mode]["model"])
            checked(item[mode]["receipt"])
    for item in protocol["probability_models"].values():
        checked(item["model"])
        checked(item["receipt"])
    checked(protocol["direction_reference"])
    if protocol.get("proposed_calendar"):
        checked(protocol["proposed_calendar"])
    for state in protocol["probability_states"].values():
        checked(state["source"])
        if state["last_maturity"] >= state["cutoff"]:
            raise ResearchError("prospective_calibration_maturity")
    return protocol


def initialize(root: Path, data: Path) -> Path:
    if (root / "config.json").exists():
        require(root)
        return root / "config.json"
    with writer(root):
        prerequisites = data / "research-e6-calibration-20261028/prospective-prerequisites.json"
        source = read(prerequisites)
        amounts: dict[str, Any] = {history: {} for history in HISTORIES}
        for item in source["models"]:
            normalized = dict(item)
            for role in ("model", "receipt"):
                reference = item[role]
                path = data.parent / reference["path"]
                if pin(path)["sha256"] != reference["sha256"]:
                    raise ResearchError("prospective_prerequisite_hash_changed")
                normalized[role] = pin(path)
            receipt = read(checked(normalized["receipt"]))
            if (
                receipt["status"] != "completed"
                or item["cutoff"] != "2026-07-01"
                or receipt["latest_label_maturity"] >= item["cutoff"]
                or receipt["features"] != item["features"]
                or receipt["categories"] != item["categories"]
                or receipt["params"]["loss_function"] != "RMSE"
            ):
                raise ResearchError("prospective_amount_recipe_mismatch")
            checked(receipt["files"]["model.cbm"])
            if receipt["files"]["model.cbm"]["sha256"] != normalized["model"]["sha256"]:
                raise ResearchError("prospective_amount_receipt_mismatch")
            amounts[item["history"]][item["mode"]] = normalized
        if any(set(pair) != {"current", "advance"} for pair in amounts.values()):
            raise ResearchError("prospective_six_amount_models_required")
        direction_path = data / "research-e6/evaluations/direction-summer/result.json"
        if pin(direction_path)["sha256"] != source["direction_reference"]["sha256"]:
            raise ResearchError("prospective_direction_reference_hash_changed")
        direction = read(direction_path)
        bundle = read(data / "research-e4/model-bundle.json")
        probability = {}
        for mode in ("current", "advance"):
            for peer in ("package", "equal"):
                old = bundle["models"][f"cb_{peer}_{mode}"]
                retained = direction["reused"][f"evaluation_2026-cb_{peer}_{mode}"]
                checked(retained["receipt"])
                if retained["receipt"]["sha256"] != old["trial"]["sha256"]:
                    raise ResearchError("prospective_probability_component_mismatch")
                probability[f"{peer}_{mode}"] = {
                    "model": old["model"],
                    "receipt": old["trial"],
                    "features": old["features"],
                    "categories": old["categories"],
                    "cutoff": old["fit_cutoff"],
                    "reference_predictions": next(
                        item
                        for item in retained["files"]
                        if item["path"].endswith("predictions.parquet")
                    ),
                }
            retained = direction["reused"][f"summer-fresh-{mode}"]
            receipt = read(checked(retained["receipt"]))
            probability[f"broad_{mode}"] = {
                "model": receipt["files"]["model.cbm"],
                "receipt": retained["receipt"],
                "features": receipt["features"],
                "categories": receipt["categories"],
                "cutoff": "2026-07-01",
                "reference_predictions": receipt["files"]["predictions.parquet"],
            }
        states = {mode: direction["calibration"][f"D_3y_routed_{mode}"] for mode in MODES}
        if states != {mode: source["probability_states"][f"D_3y_routed_{mode}"] for mode in MODES}:
            raise ResearchError("prospective_probability_state_mismatch")
        protocol = {
            "version": VERSION,
            "frozen_utc": utc(),
            "amount_models": amounts,
            "probability_models": probability,
            "probability_states": states,
            "direction_reference": pin(direction_path),
            "prerequisites": pin(prerequisites),
            "retained_features": pin(data / "research-e6/inputs/2026-features.parquet"),
            "probability_policy": {
                "method": "threshold",
                "rise_min": 0.35,
                "fall_max": 0.10,
                "cap": 0.10,
            },
            "bins": read(data / "research-e6-calibration-20261028/protocol.json")["bins"],
            "milestone": {
                "origins": 12,
                "interim": 6,
                "blocks": [2, 4],
                "draws": 2000,
                "seed": 20261028,
            },
            "producer": producer(),
            "environment": environment(),
            "scope": "Manual shadow research; zero fits/recalibrations; no promotion/savings claim",
            "deadline": "Second tariff Wednesday noon Copenhagen: proposed research convention",
            "primary_rule": "First valid completed origin/mode freeze; later freezes supplementary",
            "corrections": "Linked versions; explicit precedence; never infer from download order",
            "outcomes": "Official target AIP, exact ore, identity continuity; retain unknowns",
            "proposed_calendar": pin(root / "calendar-admission.json")
            if (root / "calendar-admission.json").exists()
            else None,
        }
        write_once(root / "protocol.json", protocol)
        write_once(
            root / "config.json",
            {
                "version": VERSION,
                "protocol": pin(root / "protocol.json"),
                "root": str(root.resolve()),
                "created_utc": utc(),
            },
        )
    require(root)
    return root / "config.json"


def model_predict(frame: pd.DataFrame, item: dict[str, Any], task: str) -> pd.DataFrame:
    model: Any = CatBoostClassifier() if task == "direction" else CatBoostRegressor()
    model.load_model(str(checked(item["model"])))
    if model.feature_names_ != item["features"]:
        raise ResearchError("prospective_model_feature_order")
    return component_predict(
        model, frame, {"task": task, "method": "rmse"}, item["features"], item["categories"]
    )


def predict(frame: pd.DataFrame, protocol: dict[str, Any], mode: str) -> pd.DataFrame:
    if mode not in MODES:
        raise ResearchError("prospective_information_mode")
    eligible = frame.eligible.fillna(False).astype(bool) & frame.aip_ore.gt(0)
    use = frame.loc[eligible].reset_index(drop=True).copy()
    out = frame.copy()
    out["prediction_state"] = np.where(eligible, "available", "excluded_origin")
    out["selected"] = False
    if use.empty:
        raise ResearchError("prospective_no_eligible_candidates")
    if mode == "current":
        use["advance_abc"] = None
    valid = use.advance_abc.isin(["A", "B", "C"]).to_numpy()
    probability = {}
    for information in ("current", "advance") if mode == "hybrid" else ("current",):
        component_values = [
            model_predict(
                use, protocol["probability_models"][f"{peer}_{information}"], "direction"
            )[PROBS].to_numpy()
            for peer in ("package", "equal", "broad")
        ]
        probability[information], route_names = route(use, *component_values)
    raw = (
        mixed(probability["advance"], probability["current"], use.advance_abc)
        if mode == "hybrid"
        else probability["current"]
    )
    calibrated = calibrate(raw, protocol["probability_states"][mode])
    use[PROBS] = calibrated
    queue = select_queue(use[["row_key", "period_start", *PROBS]], "threshold", (0.35, 0.10))
    values_out: dict[str, Any] = {
        **{name: calibrated[:, k] for k, name in enumerate(PROBS)},
        **{f"raw_{name}": raw[:, k] for k, name in enumerate(PROBS)},
        "p_fall": calibrated[:, 0],
        "p_unchanged": calibrated[:, 1],
        "p_rise": calibrated[:, 2],
        "rank": queue["rank"].to_numpy(),
        "selected": queue.selected.to_numpy(),
        "peer_route": route_names,
        "information_route": np.where(valid, "advance", "current-only fallback"),
        "fallback_reason": np.where(
            valid, "none", "current_mode" if mode == "current" else "absent_or_unusable_report_code"
        ),
    }
    for history in HISTORIES:
        pair = protocol["amount_models"][history]
        current = model_predict(use, pair["current"], "amount")
        advance = model_predict(use, pair["advance"], "amount") if mode == "hybrid" else current
        if not current.row_key.equals(use.row_key) or not advance.row_key.equals(use.row_key):
            raise ResearchError("prospective_component_key_alignment")
        for column in ("raw_delta_dkk", "delta_dkk", "next_price_dkk"):
            values = np.where(valid, advance[column], current[column])
            if not np.isfinite(values).all():
                raise ResearchError("prospective_invalid_amount_prediction")
            values_out[f"amount_{history}_{column}"] = values
        values_out[f"amount_{history}_raw_next_price_dkk"] = (
            use.aip_ore.to_numpy(dtype=float) / 100 + values_out[f"amount_{history}_raw_delta_dkk"]
        )
    for name, values in values_out.items():
        if name != "selected":
            out[name] = None
        out.loc[eligible, name] = values
    out["selected"] = out.selected.astype(bool)
    return out


def calendar(context: dict[str, Any], count: int = 12) -> list[dict[str, str]]:
    """Actual supported calendar, never a fabricated fourteen-day extrapolation."""
    document = read(checked(context["calendar"]))
    if not document.get("authority") or not document.get("evidence"):
        raise ResearchError("prospective_authoritative_calendar_evidence_required")
    for reference in document["evidence"]:
        checked(reference)
    dates = document["periods"]
    if dates != sorted(set(dates)) or any(
        pd.Timestamp(day).date().isoformat() != day for day in dates
    ):
        raise ResearchError("prospective_calendar_order")
    actual_dates = pd.DatetimeIndex(pd.to_datetime(dates))
    if not (actual_dates.dayofweek == 0).all() or any(
        (right - left).days != 14
        for left, right in zip(actual_dates[:-1], actual_dates[1:], strict=True)
    ):
        raise ResearchError("prospective_calendar_gap_or_unsupported_cadence")
    try:
        index = dates.index(context["origin"])
    except ValueError as exc:
        raise ResearchError("prospective_origin_absent_from_calendar") from exc
    if len(dates[index:]) < count + 1 or dates[index + 1] != context["target"]:
        raise ResearchError("prospective_calendar_requires_twelve_origins_and_targets")
    return [{"origin": dates[k], "target": dates[k + 1]} for k in range(index, index + count)]


def validate_context(context: dict[str, Any], feature: dict[str, Any], at: datetime) -> list[str]:
    deadline = cutoff(context["decision_cutoff"])
    actual = pd.Timestamp(at)
    if (
        not context["origin"]
        <= deadline.tz_convert("Europe/Copenhagen").date().isoformat()
        < context["target"]
    ):
        raise ResearchError("prospective_decision_period_binding")
    built = timestamp(context["feature_built_utc"])
    if built > actual:
        raise ResearchError("prospective_feature_build_in_future")
    proposed_local = (datetime.fromisoformat(context["origin"]) + timedelta(days=9)).replace(
        hour=12, tzinfo=ZoneInfo("Europe/Copenhagen")
    )
    proposed = pd.Timestamp(proposed_local).tz_convert("UTC")
    if deadline > proposed:
        raise ResearchError("prospective_cutoff_later_than_frozen_convention")
    if deadline < proposed:
        deviation = read(checked(context["cutoff_deviation"]))
        if (
            deviation["origin"] != context["origin"]
            or cutoff(deviation["decision_cutoff"]) != deadline
            or timestamp(deviation["recorded_utc"]) > built
            or not deviation.get("reason")
        ):
            raise ResearchError("prospective_cutoff_deviation_not_prefrozen")
    exposure = context.get("outcome_exposure", {})
    if exposure.get("exposed") not in (True, False) or not exposure.get("evidence"):
        raise ResearchError("prospective_outcome_exposure_evidence_required")
    declaration = read(checked(exposure["evidence"]))
    if (
        declaration.get("origin") != context["origin"]
        or declaration.get("target") != context["target"]
        or declaration.get("exposed") is not exposure["exposed"]
        or declaration.get("checked_utc") != exposure["checked_utc"]
        or not declaration.get("scope")
        or not declaration.get("actor")
    ):
        raise ResearchError("prospective_exposure_evidence_binding")
    assessed = timestamp(exposure["checked_utc"])
    if assessed > actual or assessed < built:
        raise ResearchError("prospective_exposure_check_clock")
    sources = context.get("source_receipts", [])
    if not sources:
        raise ResearchError("prospective_source_receipts_required")
    admitted_features = False
    source_hashes = set()
    reasons = []
    for reference in sources:
        receipt = read(checked(reference))
        source = receipt["artifact"]
        checked(source)
        source_hashes.add(source["sha256"])
        if not receipt.get("field_scope") or receipt["origin"] != context["origin"]:
            raise ResearchError("prospective_source_scope_or_origin")
        received = timestamp(receipt["received_utc"])
        if received > actual or received > built:
            raise ResearchError("prospective_source_received_after_build_or_now")
        if received > deadline:
            reasons.append("source_received_after_cutoff")
        # Publication may be unknown; receipt establishes actual possession, not publication.
        if receipt.get("published_utc"):
            published = timestamp(receipt["published_utc"])
            if published > actual or published > built or published > received:
                raise ResearchError("prospective_source_publication_clock")
            if published > deadline:
                reasons.append("source_published_after_cutoff")
        admitted_features |= source["sha256"] == feature["sha256"]
    if not admitted_features:
        raise ResearchError("prospective_feature_receipt_required")
    lineage = read(checked(context["feature_lineage"]))
    if (
        lineage["features"]["sha256"] != feature["sha256"]
        or lineage["built_utc"] != context["feature_built_utc"]
        or lineage["origin"] != context["origin"]
        or lineage["target"] != context["target"]
        or not lineage.get("sources")
        or not lineage.get("producer")
        or not lineage.get("field_scope")
    ):
        raise ResearchError("prospective_feature_lineage_binding")
    for reference in lineage["sources"]:
        checked(reference)
        if reference["sha256"] not in source_hashes:
            raise ResearchError("prospective_feature_source_receipt_missing")
    for reference in lineage["producer"]:
        checked(reference)
    if built > deadline:
        reasons.append("feature_built_after_cutoff")
    if exposure["exposed"]:
        reasons.append("target_outcome_already_exposed")
    if actual > deadline:
        reasons.append("completed_after_cutoff")
    if actual.tz_convert("Europe/Copenhagen").date().isoformat() >= context["target"]:
        reasons.append("target_already_started")
    return sorted(set(reasons))


def prepare(root: Path, features_path: Path, context: dict[str, Any] | Path) -> Path:
    protocol = require(root)
    ctx: dict[str, Any] = read(context) if isinstance(context, Path) else context
    ctx = json.loads(json.dumps(ctx))
    if ctx["information_mode"] not in MODES:
        raise ResearchError("prospective_information_mode")
    feature_pin = pin(features_path)
    validate_context(ctx, feature_pin, now())
    if (root / "schedule.json").exists():
        calendar(ctx, count=1)
        sequence = read(root / "schedule.json")["periods"]
    else:
        sequence = calendar(ctx)
    frame = pd.read_parquet(features_path)
    validation = {
        **ctx,
        "mode": "actual_report_retrospective",
        "calendar_next_start": ctx["target"],
    }
    validate_features(frame, validation)
    required = {"origin_identity_token", "origin_creation_date", "origin_exit_date"}
    if not required.issubset(frame.columns):
        raise ResearchError("prospective_origin_identity_evidence_required")
    if ctx["origin"] < max(state["cutoff"] for state in protocol["probability_states"].values()):
        raise ResearchError("prospective_model_or_calibration_after_origin")
    if ctx["information_mode"] == "current":
        if ctx.get("advance_report") or (
            "advance_abc" in frame and frame.advance_abc.notna().any()
        ):
            raise ResearchError("prospective_current_contains_advance_inputs")
        frame["advance_abc"] = None
    else:
        report_path = checked(ctx["advance_report"])
        report = read(report_path)
        actual_native = report.get("version") == "e4-advance-report-v1"
        if actual_native:
            checked(report["original"])
        if not actual_native and report.get("mode") not in (
            "actual_report",
            "actual_report_retrospective",
        ):
            raise ResearchError("prospective_actual_report_required")
        receipt_pin = ctx["advance_report_receipt"]
        receipt = read(checked(receipt_pin))
        if receipt["artifact"]["sha256"] != ctx["advance_report"]["sha256"]:
            raise ResearchError("prospective_advance_receipt_mismatch")
        if receipt_pin not in ctx["source_receipts"] or receipt["target"] != ctx["target"]:
            raise ResearchError("prospective_advance_receipt_scope")
        frame = bind_report(frame, report_path, ctx["origin"], ctx["target"])
    binding = {
        "protocol": pin(root / "protocol.json"),
        "features": feature_pin,
        "context": ctx,
        "candidate_schedule": sequence,
    }
    key = identity(binding)
    destination = root / "prepared" / key
    manifest = destination / "prepared.json"
    with writer(root):
        if manifest.exists():
            checked(read(manifest)["inputs"])
            return manifest
        stage = root / "staging" / uuid.uuid4().hex
        stage.mkdir(parents=True)
        frame.to_parquet(stage / "inputs.parquet", index=False)
        inputs = {
            **pin(stage / "inputs.parquet"),
            "path": str((destination / "inputs.parquet").resolve()),
        }
        binding["prepared_inputs"] = {name: inputs[name] for name in ("sha256", "size_bytes")}
        key = identity(binding)
        destination = root / "prepared" / key
        manifest = destination / "prepared.json"
        inputs["path"] = str((destination / "inputs.parquet").resolve())
        if manifest.exists():
            checked(read(manifest)["inputs"])
            return manifest
        write_once(
            stage / "prepared.json",
            {
                "version": VERSION,
                "assessment_id": key,
                "binding": binding,
                "context": ctx,
                "inputs": inputs,
                "prepared_utc": utc(),
                "candidate_count": len(frame),
            },
        )
        destination.parent.mkdir(exist_ok=True)
        publish_directory(stage, destination)
    return manifest


def freeze(root: Path, prepared_path: Path) -> Path:
    protocol = require(root)
    prepared = read(prepared_path)
    if prepared["binding"]["protocol"] != pin(root / "protocol.json"):
        raise ResearchError("prospective_prepared_protocol_mismatch")
    key = prepared["assessment_id"]
    if identity(prepared["binding"]) != key:
        raise ResearchError("prospective_prepared_identity_mismatch")
    context = prepared["context"]
    if context != prepared["binding"]["context"]:
        raise ResearchError("prospective_prepared_context_changed")
    if {name: prepared["inputs"][name] for name in ("sha256", "size_bytes")} != prepared["binding"][
        "prepared_inputs"
    ]:
        raise ResearchError("prospective_prepared_inputs_changed")
    destination = root / "assessments" / key
    result = destination / "assessment.json"
    with writer(root):
        repair_registrations(root)
        if result.exists():
            saved = read(result)
            checked(saved["inputs"])
            checked(saved["predictions"])
            return result
        validate_context(context, prepared["binding"]["features"], now())
        inputs_path = checked(prepared["inputs"])
        frame = pd.read_parquet(inputs_path)
        validate_features(
            frame,
            {
                **context,
                "mode": "actual_report_retrospective",
                "calendar_next_start": context["target"],
            },
        )
        stage = root / "staging" / uuid.uuid4().hex
        stage.mkdir(parents=True)
        shutil.copyfile(inputs_path, stage / "inputs.parquet")
        predictions = predict(frame, protocol, context["information_mode"])
        predictions.to_parquet(stage / "predictions.parquet", index=False)
        # Completion is sampled only after all input/output bytes have been written and hashed.
        files = {
            name: {
                **pin(stage / f"{name}.parquet"),
                "path": str((destination / f"{name}.parquet").resolve()),
            }
            for name in ("inputs", "predictions")
        }
        completed = now()
        reasons = validate_context(context, prepared["binding"]["features"], completed)
        primary_path = root / "primary" / f"{context['origin']}-{context['information_mode']}.json"
        if primary_path.exists():
            reasons.append("supplementary_after_primary_freeze")
        schedule_path = root / "schedule.json"
        if schedule_path.exists():
            schedule = read(schedule_path)
            if {"origin": context["origin"], "target": context["target"]} not in schedule[
                "periods"
            ]:
                reasons.append("outside_frozen_schedule")
        primary = not reasons
        assessment = {
            "version": VERSION,
            "assessment_id": key,
            "binding": prepared["binding"],
            "context": context,
            "prepared": pin(prepared_path),
            **files,
            "completed_utc": completed.isoformat(),
            "primary": primary,
            "primary_assessment": read(primary_path)["assessment"]
            if primary_path.exists()
            else None,
            "status": (
                "prospective_pending"
                if primary
                else "supplementary"
                if "supplementary_after_primary_freeze" in reasons
                else "exposed_diagnostic"
                if "target_outcome_already_exposed" in reasons
                else "late_retrospective"
            ),
            "invalid_reasons": reasons,
            "candidate_count": len(predictions),
            "predicted": int(predictions.prediction_state.eq("available").sum()),
            "selected": int(predictions.selected.sum()),
        }
        write_once(stage / "assessment.json", assessment)
        destination.parent.mkdir(exist_ok=True)
        publish_directory(stage, destination)
        if primary:
            if not schedule_path.exists():
                write_once(
                    schedule_path,
                    {
                        "version": VERSION,
                        "calendar": context["calendar"],
                        "periods": prepared["binding"]["candidate_schedule"],
                        "frozen_utc": completed.isoformat(),
                        "first_assessment": pin(result),
                    },
                )
            write_once(
                primary_path, {"assessment": pin(result), "completed_utc": completed.isoformat()}
            )
    return result


def repair_registrations(root: Path) -> None:
    """Recover completed publications interrupted before their derived primary markers."""
    completed = sorted(
        (path for path in root.glob("assessments/*/assessment.json") if read(path).get("primary")),
        key=lambda path: (read(path)["completed_utc"], str(path)),
    )
    for path in completed:
        saved = read(path)
        checked(saved["inputs"])
        checked(saved["predictions"])
        context = saved["context"]
        schedule = root / "schedule.json"
        if not schedule.exists():
            write_once(
                schedule,
                {
                    "version": VERSION,
                    "calendar": context["calendar"],
                    "periods": saved["binding"]["candidate_schedule"],
                    "frozen_utc": saved["completed_utc"],
                    "first_assessment": pin(path),
                },
            )
        marker = root / "primary" / f"{context['origin']}-{context['information_mode']}.json"
        write_once(marker, {"assessment": pin(path), "completed_utc": saved["completed_utc"]})


def validate_official(
    saved: dict[str, Any], official_path: Path, receipt_path: Path | None
) -> None:
    if receipt_path is None:
        raise ResearchError("prospective_official_receipt_required")
    receipt = read(receipt_path)
    official = read(official_path)
    received = timestamp(receipt["received_utc"])
    published = timestamp(receipt["published_utc"])
    if (
        receipt["artifact"] != pin(official_path)
        or receipt["period"] != saved["context"]["target"]
        or receipt["basis"] != "official_aip_ore"
        or receipt["status"] != "authoritative_resolved"
        or not receipt.get("precedence")
        or not receipt.get("evidence")
        or published > received
        or received > pd.Timestamp(now())
    ):
        raise ResearchError("prospective_official_admission_failed")
    if saved["primary"] and published <= timestamp(saved["completed_utc"]):
        raise ResearchError("prospective_official_available_before_primary_freeze")
    for reference in receipt["evidence"]:
        checked(reference)
    official_rows = pd.read_parquet(checked(official["rows"]))
    usable = official_rows.price_state.eq("observed")
    money = pd.to_numeric(official_rows.aip_ore, errors="coerce")
    if (
        money.loc[usable].isna().any()
        or (money.loc[usable] <= 0).any()
        or (money.loc[usable] % 1 != 0).any()
    ):
        raise ResearchError("prospective_official_nonpositive_or_inexact_money")


def attach_outcome(
    root: Path, assessment_path: Path, official_path: Path | None, receipt_path: Path | None = None
) -> Path:
    require(root)
    saved = read(assessment_path)
    require_owned_assessment(root, assessment_path, saved)
    checked(saved["predictions"])
    checked(saved["inputs"])
    if official_path:
        validate_official(saved, official_path, receipt_path)
    elif any(
        read(path)["binding"]["official"] is not None
        for path in assessment_path.parent.glob("outcomes/*/outcome.json")
    ):
        raise ResearchError("prospective_pending_cannot_demote_official_outcome")
    # Existing E4 identity-continuity and exact-period semantics, published in a staging tree.
    with writer(root):
        stage = root / "staging" / uuid.uuid4().hex
        stage.mkdir(parents=True)
        # The bridge contains immutable original pins, never copied mutable predictions.
        bridge = {
            "context": saved["context"],
            "predictions": saved["predictions"],
            "assessment": pin(assessment_path),
        }
        bridge_path = stage / "assessment.json"
        write_once(bridge_path, bridge)
        generated = e4_attach_outcome(bridge_path, official_path)
        outcome = read(generated)
        binding = {
            "assessment": pin(assessment_path),
            "official": pin(official_path) if official_path else None,
            "receipt": pin(receipt_path) if receipt_path else None,
            "method": "official-aip-v1",
            "producer": producer(),
        }
        key = identity(binding)
        # Short filesystem component avoids legacy Windows MAX_PATH; full identity stays in receipt.
        destination = assessment_path.parent / "outcomes" / key[:24]
        target = destination / "outcome.json"
        if target.exists():
            existing = read(target)
            if existing["binding"] != binding or existing["outcome_id"] != key:
                raise ResearchError("prospective_outcome_identity_collision")
            checked(existing["rows"])
            return target
        publication = stage / "publication"
        publication.mkdir()
        shutil.copyfile(checked(outcome["rows"]), publication / "outcomes.parquet")
        previous = sorted(
            assessment_path.parent.glob("outcomes/*/outcome.json"),
            key=lambda path: read(path)["evaluated_utc"],
        )
        outcome.update(
            {
                "binding": binding,
                "outcome_id": key,
                "supersedes": pin(previous[-1]) if previous else None,
                "rows": {
                    **pin(publication / "outcomes.parquet"),
                    "path": str((destination / "outcomes.parquet").resolve()),
                },
            }
        )
        write_once(publication / "outcome.json", outcome)
        destination.parent.mkdir(exist_ok=True)
        publish_directory(publication, destination)
    return target


def require_owned_assessment(root: Path, path: Path, saved: dict[str, Any]) -> None:
    if (
        path.resolve()
        != (root / "assessments" / saved["assessment_id"] / "assessment.json").resolve()
    ):
        raise ResearchError("prospective_assessment_outside_operation")
    if (
        identity(saved["binding"]) != saved["assessment_id"]
        or saved["binding"]["protocol"] != pin(root / "protocol.json")
        or saved["context"] != saved["binding"]["context"]
        or {name: saved["inputs"][name] for name in ("sha256", "size_bytes")}
        != saved["binding"]["prepared_inputs"]
    ):
        raise ResearchError("prospective_assessment_binding_changed")
    prepared = read(checked(saved["prepared"]))
    if prepared["binding"] != saved["binding"]:
        raise ResearchError("prospective_assessment_prepared_changed")


def verify(root: Path) -> dict[str, Any]:
    require(root)
    assessments = []
    for path in sorted(root.glob("assessments/*/assessment.json")):
        item = read(path)
        require_owned_assessment(root, path, item)
        if item.get("primary_assessment"):
            original = read(checked(item["primary_assessment"]))
            if (
                not original["primary"]
                or original["context"]["origin"] != item["context"]["origin"]
                or original["context"]["information_mode"] != item["context"]["information_mode"]
            ):
                raise ResearchError("prospective_supplementary_primary_binding")
        if identity(item["binding"]) != item["assessment_id"]:
            raise ResearchError("prospective_assessment_identity_mismatch")
        for name in ("inputs", "predictions", "prepared"):
            checked(item[name])
        frame = pd.read_parquet(checked(item["predictions"]))
        inputs = pd.read_parquet(checked(item["inputs"]))
        if not frame.row_key.equals(inputs.row_key) or frame.row_key.duplicated().any():
            raise ResearchError("prospective_forecast_key_mismatch")
        if item["primary"]:
            reasons = validate_context(
                item["context"],
                item["binding"]["features"],
                datetime.fromisoformat(item["completed_utc"]),
            )
            if reasons:
                raise ResearchError("prospective_invalid_primary_freeze")
            marker = read(
                root
                / "primary"
                / f"{item['context']['origin']}-{item['context']['information_mode']}.json"
            )
            if marker["assessment"] != pin(path):
                raise ResearchError("prospective_primary_marker_mismatch")
        for outcome_path in path.parent.glob("outcomes/*/outcome.json"):
            outcome = read(outcome_path)
            if identity(outcome["binding"]) != outcome["outcome_id"]:
                raise ResearchError("prospective_outcome_identity_changed")
            checked(outcome["rows"])
            if outcome["binding"]["assessment"] != pin(path):
                raise ResearchError("prospective_outcome_assessment_mismatch")
            if outcome["binding"]["official"]:
                official_path = checked(outcome["binding"]["official"])
                receipt_path = checked(outcome["binding"]["receipt"])
                validate_official(item, official_path, receipt_path)
            for reference in outcome["binding"]["producer"].values():
                checked(reference)
        assessments.append(pin(path))
    schedule = read(root / "schedule.json") if (root / "schedule.json").exists() else None
    if schedule:
        checked(schedule["calendar"])
        if len(schedule["periods"]) != 12:
            raise ResearchError("prospective_schedule_length")
        first_path = checked(schedule["first_assessment"])
        primaries = sorted(
            (
                checked(reference)
                for reference in assessments
                if read(checked(reference))["primary"]
            ),
            key=lambda path: (read(path)["completed_utc"], str(path)),
        )
        if (
            not primaries
            or first_path.resolve() != primaries[0].resolve()
            or schedule["periods"] != read(first_path)["binding"]["candidate_schedule"]
            or schedule["calendar"] != read(first_path)["context"]["calendar"]
        ):
            raise ResearchError("prospective_schedule_first_freeze_binding")
    return {
        "status": "verified",
        "assessments": assessments,
        "schedule": schedule,
        "verified_utc": utc(),
        "fits": 0,
        "recalibrations": 0,
    }


def status(root: Path) -> dict[str, Any]:
    try:
        verified = verify(root)
    except (ResearchError, OSError, ValueError, KeyError, TypeError) as exc:
        return {
            "version": VERSION,
            "status": "not_ready",
            "missing": [type(exc).__name__],
            "reason": str(exc),
            "schedule": None,
            "assessments": [],
            "prospective_enrolled": False,
            "fits": 0,
            "recalibrations": 0,
        }
    missing = []
    if not verified["schedule"]:
        missing += ["on-time completed freeze"]
    if not list(root.glob("replay/*/replay.json")):
        missing.append("retained prediction replay")
    readiness_path = root / "readiness-plan.json"
    readiness = read(readiness_path) if readiness_path.exists() else None
    if readiness and not verified["schedule"]:
        missing.extend(readiness.get("missing", []))
    elif not readiness and not verified["schedule"]:
        missing.extend(
            [
                "validated calendar and received feature lineage",
                "explicit actual outcome exposure declaration",
            ]
        )
    proposed = root / "calendar-admission.json"
    candidate_schedule = []
    if not verified["schedule"] and proposed.exists():
        dates = read(proposed)["periods"]
        candidate_schedule = [
            {"origin": origin, "target": target, "state": "not_enrolled"}
            for origin, target in zip(dates[:12], dates[1:13], strict=True)
        ]
    return {
        "version": VERSION,
        "status": "blocked_external_inputs" if missing else "manual_ready",
        "missing": missing,
        "schedule": verified["schedule"],
        "assessments": verified["assessments"],
        "prospective_enrolled": bool(verified["schedule"]),
        "fits": 0,
        "recalibrations": 0,
        "readiness": readiness,
        "candidate_schedule": candidate_schedule,
    }


def replay(root: Path) -> Path:
    """Exact retained summer inference/queue parity, explicitly exposed research only."""
    protocol = require(root)
    source = pd.read_parquet(checked(protocol["retained_features"]))
    source = source.loc[source.period_start.ge("2026-09-01") & source.period_start.lt("2026-11-01")]
    source = source.reset_index(drop=True)
    use = source.loc[source.eligible.fillna(False).astype(bool) & source.aip_ore.gt(0)].reset_index(
        drop=True
    )
    component_comparisons = []
    for history in HISTORIES:
        for mode in ("current", "advance"):
            item = protocol["amount_models"][history][mode]
            actual = model_predict(use, item, "amount")
            receipt = read(checked(item["receipt"]))
            expected = pd.read_parquet(checked(receipt["files"]["predictions.parquet"]))
            expected = expected.set_index("row_key").loc[use.row_key]
            for column in ("raw_delta_dkk", "delta_dkk", "next_price_dkk"):
                np.testing.assert_array_equal(
                    actual[column].to_numpy(), expected[column].to_numpy()
                )
            component_comparisons.append(
                {
                    "task": "amount",
                    "history": history,
                    "component": mode,
                    "rows": len(use),
                    "exact": True,
                }
            )
    for name, item in protocol["probability_models"].items():
        actual = model_predict(use, item, "direction")
        expected = pd.read_parquet(checked(item["reference_predictions"]))
        expected = expected.set_index("row_key").loc[use.row_key]
        columns = PROBS if "p0" in expected else [f"p{k}_perfect" for k in range(3)]
        np.testing.assert_array_equal(actual[PROBS].to_numpy(), expected[columns].to_numpy())
        component_comparisons.append(
            {"task": "direction", "component": name, "rows": len(use), "exact": True}
        )
    accepted = read(checked(protocol["direction_reference"]))
    saved_probability = pd.read_parquet(checked(accepted["files"]["predictions.parquet"]))
    queues = pd.read_parquet(checked(accepted["files"]["queues.parquet"]))
    comparisons = []
    predictions = {}
    for mode in MODES:
        predicted = predict(source, protocol, mode)
        predictions[mode] = predicted
        known = predicted.loc[predicted.prediction_state.eq("available")].reset_index(drop=True)
        expected = saved_probability.loc[
            saved_probability.history.eq("3y")
            & saved_probability.route.eq("routed")
            & saved_probability.information.eq(mode)
        ].reset_index(drop=True)
        if not known.row_key.equals(expected.row_key):
            raise ResearchError("prospective_replay_probability_keys")
        np.testing.assert_array_equal(
            known[PROBS].to_numpy(dtype=float), expected[PROBS].to_numpy()
        )
        selected = queues.loc[queues.history.eq("3y") & queues.information.eq(mode)].reset_index(
            drop=True
        )
        if not known.row_key.equals(selected.row_key):
            raise ResearchError("prospective_replay_queue_keys")
        np.testing.assert_array_equal(known.selected.to_numpy(), selected.selected.to_numpy())
        np.testing.assert_array_equal(known["rank"].to_numpy(), selected["rank"].to_numpy())
        for history in HISTORIES:
            pair = {}
            for component in ("current", "advance"):
                receipt = read(checked(protocol["amount_models"][history][component]["receipt"]))
                retained = pd.read_parquet(checked(receipt["files"]["predictions.parquet"]))
                retained = retained.set_index("row_key").loc[known.row_key]
                pair[component] = retained
            valid = (
                source.loc[source.row_key.isin(known.row_key)]
                .advance_abc.isin(["A", "B", "C"])
                .to_numpy()
                if mode == "hybrid"
                else np.zeros(len(known), dtype=bool)
            )
            for name in ("raw_delta_dkk", "delta_dkk", "next_price_dkk"):
                expected_amount = np.where(valid, pair["advance"][name], pair["current"][name])
                np.testing.assert_array_equal(
                    known[f"amount_{history}_{name}"].to_numpy(dtype=float), expected_amount
                )
            comparisons.append(
                {
                    "mode": mode,
                    "history": history,
                    "rows": len(known),
                    "amount_exact": True,
                    "probability_exact": True,
                    "selected_and_rank_exact": True,
                }
            )
    binding = {
        "protocol": pin(root / "protocol.json"),
        "features": protocol["retained_features"],
        "reference": protocol["direction_reference"],
    }
    destination = root / "replay" / identity(binding)
    target = destination / "replay.json"
    with writer(root):
        if target.exists():
            for reference in read(target)["files"].values():
                checked(reference)
            return target
        destination.mkdir(parents=True)
        files = {}
        for mode, frame in predictions.items():
            columns = [
                name
                for name in map(str, frame.columns)
                if name.startswith(("amount_", "raw_p"))
                or name
                in (
                    "row_key",
                    "period_start",
                    "package_number",
                    "prediction_state",
                    "selected",
                    "rank",
                    "peer_route",
                    "information_route",
                    *PROBS,
                )
            ]
            frame[columns].to_parquet(destination / f"{mode}.parquet", index=False)
            files[mode] = pin(destination / f"{mode}.parquet")
        write_once(
            target,
            {
                "version": VERSION,
                "status": "exact_retained_parity",
                "binding": binding,
                "comparisons": comparisons,
                "component_comparisons": component_comparisons,
                "files": files,
                "completed_utc": utc(),
                "exposure": "retrospective; not prospective enrollment",
                "fits": 0,
                "recalibrations": 0,
            },
        )
    return target


def report(root: Path) -> dict[str, Any]:
    from .prospective_report import report as build_report

    return build_report(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--data", type=Path, required=True)
    for name in ("status", "verify", "replay", "report"):
        commands.add_parser(name)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--features", type=Path, required=True)
    preparing.add_argument("--context", type=Path, required=True)
    freezing = commands.add_parser("freeze")
    freezing.add_argument("--prepared", type=Path, required=True)
    outcome = commands.add_parser("attach-outcome")
    outcome.add_argument("--assessment", type=Path, required=True)
    outcome.add_argument("--official", type=Path)
    outcome.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.command == "init":
        result: Any = str(initialize(args.root, args.data))
    elif args.command == "prepare":
        result = str(prepare(args.root, args.features, args.context))
    elif args.command == "freeze":
        result = str(freeze(args.root, args.prepared))
    elif args.command == "attach-outcome":
        result = str(attach_outcome(args.root, args.assessment, args.official, args.receipt))
    else:
        result = globals()[args.command](args.root)
        if isinstance(result, Path):
            result = str(result)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
