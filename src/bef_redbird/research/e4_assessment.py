"""Freeze complete candidate assessments, then append separately versioned outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor

from . import e3_peers, group_features
from .dataset import ResearchError
from .e2_models import catboost_frame
from .e3_methods import intervals, select_queue
from .e4_common import checked, frozen_json, identity, read, utc
from .e4_methods import calibrated_routes, route
from .group_features import pin
from .training_contract import ALLOWED_DISPENSING, FEATURES, KEYS

MODES = {"historical_proxy", "actual_report_retrospective", "prospective_pending", "synthetic"}
AUXILIARY = {
    "eligible",
    "exclusion_reason",
    "target_period",
    "advance_abc",
    "advance_state",
    "advance_source",
    "origin_group",
    "origin_identity_token",
    "origin_creation_date",
    "origin_exit_date",
    "provenance",
    "input_quality",
    "report_current_abc",
    "report_current_state",
    "report_id",
}


def model_bundle(e3_root: Path, output: Path) -> Path:
    if output.exists():
        for item in read(output)["models"].values():
            checked(item["model"])
        return output
    accepted = read(checked(read(e3_root.parent / "current.json")["display_result"]))
    e3_protocol = read(checked(accepted["source_binding"]["protocol"]))
    e2_ledger = {
        x["trial_id"]: x for x in read(checked(e3_protocol["pins"]["e2_ledger"]))["trials"]
    }
    ledger = {
        x["trial_id"]: x for x in read(checked(accepted["source_binding"]["ledger"]))["trials"]
    }
    e2_root = Path(e3_protocol["pins"]["e2_protocol"]["path"]).parent
    models = {}
    for method in [
        f"cb_{peer}_{info}"
        for info in ("current", "advance")
        for peer in ("package", "equal", "broad")
    ] + ["amount_delta_floor_current", "amount_delta_floor_advance"]:
        directory = e3_root / "trials" / ("evaluation_2026-" + method)
        trial = read(directory / "trial.json")
        if trial != ledger["evaluation_2026-" + method]:
            raise ResearchError("e4_model_not_accepted_trial")
        if trial["status"] != "completed" or trial["fit_cutoff"] != "2026-01-01":
            raise ResearchError("e4_incompatible_model_trial")
        if trial["new_fit"]:
            model = directory / "model.cbm"
            expected = next(x for x in trial["files"] if x["path"] == "model.cbm")
        else:
            previous = e2_root / "trials" / trial["source_trial"]
            old = read(previous / "trial.json")
            if old != e2_ledger[trial["source_trial"]]:
                raise ResearchError("e4_reused_model_not_accepted_e2_trial")
            model = previous / "model.cbm"
            expected = next(x for x in old["files"] if x["path"] == "model.cbm")
        checked({**expected, "path": str(model.resolve())})
        models[method] = {
            "model": pin(model),
            "trial": pin(directory / "trial.json"),
            "features": trial["spec"]["features"],
            "categories": trial["spec"]["categories"],
            "task": trial["task"],
            "fit_cutoff": trial["fit_cutoff"],
            "training_start": trial["training_start"],
            "latest_label_maturity": trial["latest_training_label_maturity"],
        }
    frozen_json(
        output,
        {
            "version": "e4-accepted-model-bundle-v1",
            "models": models,
            "amount_interval_calibration": accepted["calibration"],
            "status": "Reused accepted 2026-vintage research models; not newly trained or promoted",
        },
    )
    return output


def bind_report(
    features: pd.DataFrame, report_path: Path, origin: str, target: str
) -> pd.DataFrame:
    report = read(report_path)
    if (report["context"]["current_period"], report["context"]["target_period"]) != (
        origin,
        target,
    ):
        raise ResearchError("e4_stale_or_wrong_report_period")
    rows = pd.read_parquet(checked(report["rows"]))
    if rows.package_number.duplicated().any():
        raise ResearchError("e4_unresolved_report_duplicates")
    out = features.drop(
        columns=[x for x in ("advance_abc", "advance_state", "advance_source") if x in features]
    ).copy()
    use = rows[
        ["package_number", "advance_abc", "advance_state", "current_abc", "current_state"]
    ].rename(columns={"current_abc": "report_current_abc", "current_state": "report_current_state"})
    out = out.merge(use, on="package_number", how="left", sort=False, validate="many_to_one")
    if not out.row_key.equals(features.row_key.reset_index(drop=True)):
        raise ResearchError("e4_report_changed_origin_candidates")
    out["advance_state"] = out.advance_state.fillna("report_identifier_absent")
    out["advance_abc"] = out.advance_abc.where(out.advance_state.eq("observed"))
    out["advance_source"] = "actual_report:" + report["report_id"]
    out["report_id"] = report["report_id"]
    return out


def validate_features(frame: pd.DataFrame, context: dict[str, Any]) -> None:
    admitted = (
        set(FEATURES)
        | set(KEYS)
        | set(group_features.FEATURES)
        | set(e3_peers.FEATURES)
        | AUXILIARY
    )
    if set(frame.columns) - admitted:
        raise ResearchError("e4_unadmitted_input_column")
    if (
        frame.row_key.isna().any()
        or frame.row_key.duplicated().any()
        or frame.package_number.duplicated().any()
        or not frame.package_number.str.fullmatch(r"[0-9]{6}").all()
    ):
        raise ResearchError("e4_candidate_identity_invalid")
    if not pd.to_datetime(frame.period_start).eq(pd.Timestamp(context["origin"])).all():
        raise ResearchError("e4_origin_binding")
    if (
        context["origin"] >= context["target"]
        or context["calendar_next_start"] != context["target"]
    ):
        raise ResearchError("e4_target_not_immediate_next")
    if (
        "target_period" in frame
        and not pd.to_datetime(frame.target_period).eq(pd.Timestamp(context["target"])).all()
    ):
        raise ResearchError("e4_target_binding")
    if context["mode"] not in MODES:
        raise ResearchError("e4_unknown_assessment_mode")
    cutoff = pd.Timestamp(context["decision_cutoff"])
    if cutoff.tzinfo is None or cutoff.date().isoformat() >= context["target"]:
        raise ResearchError("e4_invalid_decision_cutoff")
    if context["mode"] == "prospective_pending":
        now = datetime.now(UTC)
        if context["target"] <= now.date().isoformat() or context.get(
            "target_outcome_exposed", True
        ):
            raise ResearchError("e4_cannot_claim_prospective")
        if not context.get("availability_evidence"):
            raise ResearchError("e4_prospective_availability_missing")
        if cutoff > pd.Timestamp(now):
            raise ResearchError("e4_prospective_cutoff_in_future")
        for item in context["availability_evidence"]:
            checked(item["artifact"])
            available = pd.Timestamp(item["available_utc"])
            if available.tzinfo is None or available > cutoff:
                raise ResearchError("e4_source_not_available_by_cutoff")


def predict(frame: pd.DataFrame, bundle_path: Path, calibration_path: Path) -> pd.DataFrame:
    bundle = read(bundle_path)
    state = read(calibration_path)["states"]["2026"]
    out = frame.copy()
    eligible = out.eligible.fillna(False).astype(bool) & out.aip_ore.gt(0)
    use = out.loc[eligible].copy().reset_index(drop=True)
    if use.empty:
        raise ResearchError("e4_no_predictable_candidates")
    predictions = {}
    for name, item in bundle["models"].items():
        if item["latest_label_maturity"] >= item["fit_cutoff"]:
            raise ResearchError("e4_model_label_maturity")
        model: Any = CatBoostRegressor() if item["task"] == "magnitude" else CatBoostClassifier()
        model.load_model(str(checked(item["model"])))
        if model.feature_names_ != item["features"]:
            raise ResearchError("e4_model_feature_contract_changed")
        x = catboost_frame(use, item["features"], item["categories"])
        predictions[name] = (
            model.predict(x, thread_count=6)
            if item["task"] == "magnitude"
            else model.predict_proba(x, thread_count=6)
        )
    current, names = route(
        use, *[predictions[f"cb_{peer}_current"] for peer in ("package", "equal", "broad")]
    )
    advance, _ = route(
        use, *[predictions[f"cb_{peer}_advance"] for peer in ("package", "equal", "broad")]
    )
    p = calibrated_routes(use, advance, current, state["temperature"])["hybrid"]
    valid = use.advance_abc.isin(["A", "B", "C"]).to_numpy()
    # Both amount artifacts predict DKK delta; accepted E3 support transform is unchanged.
    pc = np.maximum(
        0, use.aip_ore.to_numpy(dtype=float) / 100 + predictions["amount_delta_floor_current"]
    )
    pa = np.maximum(
        0, use.aip_ore.to_numpy(dtype=float) / 100 + predictions["amount_delta_floor_advance"]
    )
    next_price = np.where(valid, pa, pc)
    old_calibration = read(checked(bundle["amount_interval_calibration"]))["residual"]
    advance_interval = intervals(use.assign(next_aip_dkk=pa), old_calibration["2026_advance"])
    current_interval = intervals(use.assign(next_aip_dkk=pc), old_calibration["2026_current"])
    la, ua = advance_interval.lower90_dkk.to_numpy(), advance_interval.upper90_dkk.to_numpy()
    lc, uc = current_interval.lower90_dkk.to_numpy(), current_interval.upper90_dkk.to_numpy()
    use[["p0", "p1", "p2"]] = p
    queue = select_queue(
        use[["row_key", "period_start", "p0", "p1", "p2"]], "threshold", (0.35, 0.10)
    )
    risk = select_queue(use[["row_key", "period_start", "p0", "p1", "p2"]], "risk_rank", 0.5)
    values = {
        "p_fall": p[:, 0],
        "p_unchanged": p[:, 1],
        "p_rise": p[:, 2],
        "next_aip_dkk": next_price,
        "change_dkk": next_price - use.aip_ore.to_numpy(dtype=float) / 100,
        "lower90_dkk": np.where(valid, la, lc),
        "upper90_dkk": np.where(valid, ua, uc),
        "peer_route": names,
        "information_route": np.where(valid, "advance ABC", "current-only fallback"),
        "selected": queue.selected.to_numpy(),
        "selected_risk_decile": risk.selected.to_numpy(),
        "rank": queue["rank"].to_numpy(),
        "risk_score": p[:, 2] - 0.5 * p[:, 0],
        "selection_reason": queue.selection_reason.to_numpy(),
    }
    for name, values_array in values.items():
        out[name] = None
        out.loc[eligible, name] = values_array
    out["prediction_state"] = np.where(eligible, "available", "excluded_origin")
    out.loc[~eligible, "selection_reason"] = out.loc[~eligible, "exclusion_reason"]
    for key in ("selected", "selected_risk_decile"):
        out[key] = out[key].fillna(False).astype(bool)
    for key in (
        "p_fall",
        "p_unchanged",
        "p_rise",
        "next_aip_dkk",
        "change_dkk",
        "lower90_dkk",
        "upper90_dkk",
        "risk_score",
    ):
        out[key] = pd.to_numeric(out[key])
    return out


def prepare(
    root: Path,
    features_path: Path,
    context: dict[str, Any],
    bundle_path: Path,
    calibration_path: Path,
) -> Path:
    feature = pd.read_parquet(features_path)
    validate_features(feature, context)
    if (
        context["origin"] < "2026-01-01"
        or pd.Timestamp(context["decision_cutoff"]).date().isoformat() < "2026-01-01"
    ):
        raise ResearchError("e4_model_fit_after_decision")
    for source in context.get("sources", []):
        checked(source)
    if context["mode"] == "prospective_pending":
        admitted = {item["artifact"]["sha256"] for item in context["availability_evidence"]}
        required = [
            pin(features_path),
            pin(bundle_path),
            pin(calibration_path),
            *context.get("sources", []),
        ]
        if not {item["sha256"] for item in required}.issubset(admitted):
            raise ResearchError("e4_incomplete_prospective_availability")
    bundle = read(bundle_path)
    for model in bundle.get("models", {}).values():
        checked(model["model"])
        checked(model["trial"])
        if (
            model["fit_cutoff"] > context["origin"]
            or model["latest_label_maturity"] >= model["fit_cutoff"]
        ):
            raise ResearchError("e4_model_maturity_boundary")
    if "amount_interval_calibration" in bundle:
        checked(bundle["amount_interval_calibration"])
    calibration = read(calibration_path)
    for state in calibration.get("states", {}).get("2026", {}).get("residual", {}).values():
        if state["last_maturity"] >= state["cutoff"] or state["cutoff"] > context["origin"]:
            raise ResearchError("e4_calibration_maturity_boundary")
    binding: dict[str, Any] = {
        "version": "e4-assessment-v1",
        "inputs": pin(features_path),
        "context": context,
        "models": pin(bundle_path),
        "calibration": pin(calibration_path),
        "policy": "hybrid routed; risk=lambda.5/top10%; threshold>=.35 rise<=.10 fall/top10% cap",
        "uncertainty": (
            "Retained E3 price-band residual interval, nominal 90%, conditional failures "
            "remain; rejected E4 conditional alternative not used"
        ),
        "producer": {
            p.name: pin(p) for p in (Path(__file__), Path(__file__).with_name("e4_methods.py"))
        },
    }
    key = identity(binding)
    destination = root / "assessments" / key
    manifest = destination / "assessment.json"
    if manifest.exists():
        saved = read(manifest)
        for role in ("inputs", "predictions"):
            checked(saved[role])
        return manifest
    destination.mkdir(parents=True, exist_ok=True)
    # Inputs are pinned before inference; output attachment never mutates them.
    prepared = destination / "prepared.json"
    if prepared.exists():
        if read(prepared)["binding"] != binding:
            raise ResearchError("e4_prepared_binding_changed")
    else:
        frozen_json(prepared, {"assessment_id": key, "binding": binding, "created_utc": utc()})
    frozen_inputs = destination / "inputs.parquet"
    if not frozen_inputs.exists():
        frozen_inputs.write_bytes(features_path.read_bytes())
    checked({**binding["inputs"], "path": str(frozen_inputs)})
    predictions = predict(feature, bundle_path, calibration_path)
    predictions.to_parquet(destination / "predictions.parquet", index=False)
    frozen_json(
        manifest,
        {
            "version": "e4-assessment-v1",
            "assessment_id": key,
            "created_utc": read(destination / "prepared.json")["created_utc"],
            "completed_utc": utc(),
            "binding": binding,
            "context": context,
            "inputs": pin(frozen_inputs),
            "predictions": pin(destination / "predictions.parquet"),
            "candidate_count": len(feature),
            "predicted": int(predictions.prediction_state.eq("available").sum()),
            "selected": int(predictions.selected.sum()),
            "fit_cutoff": "2026-01-01",
            "training_scope": "2023-01-01 through labels mature strictly before 2026-01-01",
            "status": "prospective_pending"
            if context["mode"] == "prospective_pending"
            else "retrospective_frozen",
        },
    )
    return manifest


def attach_outcome(
    assessment_path: Path, official_path: Path | None, *, method: str = "official-aip-v1"
) -> Path:
    assessment = read(assessment_path)
    predictions = pd.read_parquet(checked(assessment["predictions"]))
    official = read(official_path) if official_path else None
    target = assessment["context"]["target"]
    if official and official["period"] != target:
        raise ResearchError("e4_outcome_period_binding")
    binding = {
        "assessment": pin(assessment_path),
        "official": pin(official_path) if official_path else None,
        "method": method,
        "producer": pin(Path(__file__)),
    }
    key = identity(binding)
    destination = assessment_path.parent / "outcomes" / key
    manifest = destination / "outcome.json"
    if manifest.exists():
        checked(read(manifest)["rows"])
        return manifest
    frame = predictions[
        [
            "row_key",
            "package_number",
            "aip_ore",
            "origin_identity_token",
            "origin_creation_date",
            "origin_exit_date",
            "eligible",
        ]
    ].copy()
    if official:
        rows = pd.read_parquet(checked(official["rows"]))
        if rows.package_number.duplicated().any():
            raise ResearchError("e4_outcome_ambiguous_identifiers")
        frame = frame.merge(
            rows[
                [
                    "package_number",
                    "aip_ore",
                    "price_state",
                    "identity_token",
                    "created_date_token",
                    "exit_date_token",
                    "dispensing_code",
                ]
            ].rename(columns={"aip_ore": "next_aip_ore"}),
            on="package_number",
            how="left",
            validate="one_to_one",
            sort=False,
        )
        created = pd.to_datetime(frame.created_date_token, format="%Y%m%d", errors="coerce")
        origin_created = pd.to_datetime(frame.origin_creation_date, errors="coerce")
        prior_exit = pd.to_datetime(frame.origin_exit_date, errors="coerce")
        target_exit = pd.to_datetime(frame.exit_date_token, format="%Y%m%d", errors="coerce")
        identity_bad = (
            created.isna()
            | origin_created.isna()
            | created.ne(origin_created)
            | (
                prior_exit.notna()
                & prior_exit.ne(pd.Timestamp("1900-01-01"))
                & prior_exit.le(pd.Timestamp(target))
            )
        )
        target_unsupported = ~frame.dispensing_code.isin(ALLOWED_DISPENSING) | (
            target_exit.notna() & target_exit.ne(pd.Timestamp("1900-01-01"))
        )
        frame["status"] = np.where(
            frame.identity_token.isna(),
            "identifier_unmatched",
            np.where(
                frame.identity_token.ne(frame.origin_identity_token)
                | frame.origin_identity_token.isna()
                | identity_bad,
                "identity_discontinuity_or_ambiguity",
                np.where(
                    frame.price_state.eq("observed") & ~target_unsupported,
                    "observed",
                    "unsupported_or_unresolved_target",
                ),
            ),
        )
        frame.loc[frame.status.ne("observed"), "next_aip_ore"] = None
    else:
        frame["status"] = "pending_target_observation"
        frame["next_aip_ore"] = None
    frame.loc[~frame.eligible, "status"] = "origin_ineligible"
    frame.loc[frame.status.ne("observed"), "next_aip_ore"] = None
    frame["next_delta_ore"] = pd.to_numeric(frame.next_aip_ore) - frame.aip_ore
    frame["next_direction"] = np.sign(frame.next_delta_ore)
    frame["target_period"] = target
    previous = (
        sorted((assessment_path.parent / "outcomes").glob("*/outcome.json"))
        if (assessment_path.parent / "outcomes").exists()
        else []
    )
    previous = sorted(previous, key=lambda p: read(p)["evaluated_utc"])
    destination.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(destination / "outcomes.parquet", index=False)
    frozen_json(
        manifest,
        {
            "version": "e4-outcome-v1",
            "outcome_id": key,
            "binding": binding,
            "supersedes": pin(previous[-1]) if previous else None,
            "supersession_semantics": (
                "Previous outcome check for this assessment, not source-edition "
                "precedence. Official edition supplied explicitly; no latest-download "
                "selection."
            ),
            "evaluated_utc": utc(),
            "rows": pin(destination / "outcomes.parquet"),
            "states": frame.status.value_counts().to_dict(),
            "candidate_count": len(frame),
            "original_predictions_unchanged": pin(checked(assessment["predictions"])),
        },
    )
    return manifest


def compare_assessments(left: Path, right: Path) -> dict[str, Any]:
    a, b = read(left), read(right)
    reasons = []
    for key in ("origin", "target", "decision_cutoff", "mode"):
        if a["context"][key] != b["context"][key]:
            reasons.append(key + " differs")
    af, bf = pd.read_parquet(checked(a["inputs"])), pd.read_parquet(checked(b["inputs"]))
    if not af.row_key.equals(bf.row_key):
        reasons.append("candidate population differs")
    for name in ("abc", "advance_abc", "advance_state"):
        if name in af and name in bf and not af[name].equals(bf[name]):
            reasons.append(name + " differs")
    return {
        "compatible": not reasons,
        "reasons": reasons,
        "left": pin(left),
        "right": pin(right),
        "claim": (
            "Compatible input populations only; later outcome versions must also match for scores"
        ),
    }
