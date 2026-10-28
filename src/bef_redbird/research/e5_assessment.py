"""Separate immutable E5 assessment and exact replay; E4 artifacts stay untouched."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor
from scipy.special import expit, logit

from .dataset import ResearchError
from .e2_data import attach_outcomes
from .e2_models import catboost_frame
from .e3_methods import calibrate, intervals
from .e4_assessment import attach_outcome, validate_features
from .e4_common import checked, frozen_json, identity, read, utc
from .e4_methods import mixed, route
from .e5_finance import queue_policies
from .e5_protocol import require
from .e5_results import temperature_once
from .group_features import pin

VERSION = "e5-assessment-v1"
FIT = "2026-09-01"
ORIGIN = "2026-10-26"
TARGET = "2026-11-09"
Relocations = dict[str | Path, str | Path]


def _resolve(reference: dict[str, Any], relocations: Relocations | None = None) -> Path:
    """Relocate references at read time, leaving every immutable JSON byte intact."""
    path = Path(reference["path"])
    for original, restored in sorted((relocations or {}).items(), key=lambda x: -len(str(x[0]))):
        try:
            relative = path.relative_to(Path(original))
        except ValueError:
            continue
        path = Path(restored) / relative
        break
    return checked({**reference, "path": str(path)})


def _aligned(frame: pd.DataFrame, saved: pd.DataFrame) -> pd.DataFrame:
    if (
        frame.row_key.duplicated().any()
        or saved.row_key.duplicated().any()
        or not frame.row_key.isin(saved.row_key).all()
    ):
        raise ResearchError("e5_assessment_incomplete_identity_alignment")
    return saved.set_index("row_key").loc[frame.row_key].reset_index()


def _direction_fallback(
    root: Path, protocol: dict[str, Any], accepted: dict[str, Any]
) -> tuple[dict[str, Path], dict[str, Any] | None]:
    """Admit only the explicitly frozen July pair after the named resource failure."""
    path = root / "final-artifact-decision.json"
    if not path.exists():
        return {}, None
    decision = read(path)
    if (
        decision.get("version") != "e5-final-artifact-resource-decision-v1"
        or decision.get("direction_fit_cutoff") != "2026-07-01"
        or decision.get("amount_fit_cutoff") != FIT
        or decision.get("reused_e4_component_fit_cutoff") != "2026-01-01"
        or decision.get("calibration_block_start") != FIT
        or decision.get("calibration_end_exclusive") != ORIGIN
        or decision.get("base_fit_limits_unchanged") is not True
        or decision.get("reserve_attempts_used") != 0
        or decision.get("no_operational_promotion") is not True
        or decision.get("skipped_trials") != ["final-selected_direction-advance"]
        or set(decision.get("direction_receipts", {})) != {"current", "advance"}
    ):
        raise ResearchError("e5_assessment_invalid_resource_fallback_decision")
    for key, expected in (
        ("protocol", root / "protocol.json"),
        ("development_selection", root / "selection.json"),
        ("scientific_results", root / "results.json"),
        ("failed_attempt", root / "trials/final-selected_direction-current/receipt.json"),
    ):
        if checked(decision[key]).resolve() != expected.resolve():
            raise ResearchError("e5_assessment_resource_fallback_binding")
    selection = read(checked(decision["development_selection"]))["direction"]

    def parameters(mode: str) -> dict[str, Any]:
        expected = dict(protocol["settings"])
        if selection["method"] != "unchanged":
            expected.update(protocol["direction"]["tuning"][selection["method"]])
            expected["iterations"] = selection["iterations"][mode]
        expected.update(loss_function="MultiClass", classes_count=3)
        return expected

    failed = read(checked(decision["failed_attempt"]))
    failed_spec = failed.get("spec", {})
    resource = failed.get("resource", {})
    limits = protocol["resource_limits"]
    if (
        failed.get("status") != "failed"
        or failed.get("error") != "e5_declared_fit_resource_limit"
        or failed_spec.get("id") != "final-selected_direction-current"
        or failed_spec.get("fold") != "final"
        or failed_spec.get("method") != "selected_direction"
        or failed_spec.get("task") != "direction"
        or failed_spec.get("feature_route") != "broad"
        or failed_spec.get("information") != "current"
        or failed_spec.get("fit_cutoff") != FIT
        or failed.get("params") != parameters("current")
        or failed.get("features") != accepted["models"]["cb_broad_current"]["features"]
        or failed.get("categories") != accepted["models"]["cb_broad_current"]["categories"]
        or failed.get("latest_label_maturity", FIT) >= FIT
        or failed.get("training_start") != "2023-09-01"
        or not failed.get("producer")
        or not (
            resource.get("elapsed_seconds", 0) > limits["fit_seconds"]
            or resource.get("sampled_peak_rss_bytes", 0) > limits["process_rss_bytes"]
        )
        or failed.get("protocol") != decision["protocol"]
    ):
        raise ResearchError("e5_assessment_resource_fallback_requires_declared_failure")
    checked(failed["inputs"])
    paths = {}
    for mode, reference in decision["direction_receipts"].items():
        receipt_path = checked(reference)
        receipt = read(receipt_path)
        spec = receipt.get("spec", {})
        expected_method = "fresh" if selection["method"] == "unchanged" else "tuned"
        expected_id = f"summer-{expected_method}-{mode}"
        schema = accepted["models"][f"cb_broad_{mode}"]
        if (
            receipt_path.resolve() != (root / "trials" / expected_id / "receipt.json").resolve()
            or receipt.get("status") != "completed"
            or spec.get("id") != expected_id
            or spec.get("fold") != "summer"
            or spec.get("method") != expected_method
            or spec.get("task") != "direction"
            or spec.get("feature_route") != "broad"
            or spec.get("information") != mode
            or spec.get("fit_cutoff") != "2026-07-01"
            or receipt.get("latest_label_maturity", "2026-07-01") >= "2026-07-01"
            or receipt.get("training_start") != "2023-07-01"
            or receipt.get("params") != parameters(mode)
            or receipt.get("trees") != parameters(mode)["iterations"]
            or receipt.get("features") != schema["features"]
            or receipt.get("categories") != schema["categories"]
            or receipt.get("exact_reload") is not True
            or receipt.get("protocol") != decision["protocol"]
            or receipt.get("inputs") != failed["inputs"]
            or receipt.get("producer") != failed["producer"]
        ):
            raise ResearchError("e5_assessment_incompatible_resource_fallback_pair")
        for item in receipt["files"].values():
            checked(item)
        paths[mode] = receipt_path
    return paths, pin(path)


def model_bundle(root: Path, *, persistence_enabled: bool) -> Path:
    protocol = require(root)
    directory = root / "final-assessment"
    directory.mkdir(exist_ok=True)
    accepted_path = checked(protocol["pins"]["e4_model-bundle"])
    accepted = read(accepted_path)
    fallback, fallback_ref = _direction_fallback(root, protocol, accepted)
    models = {k: v for k, v in accepted["models"].items() if not k.startswith("cb_broad_")}
    receipts = {}
    tasks = [("selected_direction", "cb_broad"), ("rmse", "mean"), ("quantile", "quantile")]
    if persistence_enabled:
        tasks.append(("persistence", "persistence"))
    for method, name in tasks:
        for mode in ("current", "advance"):
            receipt_path = (
                fallback[mode]
                if method == "selected_direction" and fallback
                else root / "trials" / f"final-{method}-{mode}" / "receipt.json"
            )
            receipt = read(receipt_path)
            cutoff = "2026-07-01" if method == "selected_direction" and fallback else FIT
            if (
                receipt["status"] != "completed"
                or receipt["spec"]["fit_cutoff"] != cutoff
                or receipt["latest_label_maturity"] >= cutoff
                or receipt["protocol"]["sha256"] != pin(root / "protocol.json")["sha256"]
            ):
                raise ResearchError("e5_assessment_incompatible_final_fit")
            for reference in receipt["files"].values():
                checked(reference)
            models[f"{name}_{mode}"] = {
                "model": receipt["files"]["model.cbm"],
                "trial": pin(receipt_path),
                "features": receipt["features"],
                "categories": receipt["categories"],
                "task": "direction" if name == "cb_broad" else name,
                "fit_cutoff": cutoff,
                "training_start": receipt["training_start"],
                "latest_label_maturity": receipt["latest_label_maturity"],
            }
            receipts[f"{name}_{mode}"] = pin(receipt_path)
    value = {
        "version": "e5-final-model-bundle-v1",
        "models": models,
        "accepted_e4_bundle": pin(accepted_path),
        "model_trials": receipts,
        "final_trials": {k: v for k, v in receipts.items() if models[k]["fit_cutoff"] == FIT},
        "direction_fit_cutoff": "2026-07-01" if fallback else FIT,
        "amount_fit_cutoff": FIT,
        "planned_final_fit_cutoff": FIT,
        "component_fit_cutoffs": {k: v["fit_cutoff"] for k, v in models.items()},
        "amount_interval_calibration": accepted["amount_interval_calibration"],
        "persistence_enabled": persistence_enabled,
        "protocol": pin(root / "protocol.json"),
    }
    if fallback_ref is not None:
        value["resource_fallback"] = fallback_ref
    path = directory / "model-bundle.json"
    _check_models(value, ORIGIN)
    frozen_json(path, value)
    return path


def _check_models(
    bundle: dict[str, Any], origin: str, relocations: Relocations | None = None
) -> None:
    if "component_fit_cutoffs" in bundle and bundle["component_fit_cutoffs"] != {
        k: v["fit_cutoff"] for k, v in bundle["models"].items()
    }:
        raise ResearchError("e5_assessment_component_cutoffs_changed")
    if bundle.get("resource_fallback"):
        decision = read(_resolve(bundle["resource_fallback"], relocations))
        for key in ("protocol", "development_selection", "scientific_results", "failed_attempt"):
            _resolve(decision[key], relocations)
        for mode, reference in decision["direction_receipts"].items():
            _resolve(reference, relocations)
            item = bundle["models"][f"cb_broad_{mode}"]
            if (
                item["trial"] != reference
                or item["fit_cutoff"] != decision["direction_fit_cutoff"]
                or bundle["direction_fit_cutoff"] != decision["direction_fit_cutoff"]
                or bundle["amount_fit_cutoff"] != decision["amount_fit_cutoff"]
            ):
                raise ResearchError("e5_assessment_resource_fallback_bundle_changed")
    for item in bundle["models"].values():
        _resolve(item["model"], relocations)
        _resolve(item["trial"], relocations)
        if item["fit_cutoff"] > origin or item["latest_label_maturity"] >= item["fit_cutoff"]:
            raise ResearchError("e5_assessment_model_maturity_boundary")
    _resolve(bundle["amount_interval_calibration"], relocations)


def _raw(
    frame: pd.DataFrame,
    bundle: dict[str, Any],
    *,
    calibration_only: bool = False,
    relocations: Relocations | None = None,
) -> dict[str, np.ndarray]:
    out = {}
    for name, item in bundle["models"].items():
        direction = name.startswith("cb_")
        persistence = name.startswith("persistence_")
        if calibration_only and not direction and not persistence:
            continue
        model: Any = CatBoostClassifier() if direction or persistence else CatBoostRegressor()
        model.load_model(str(_resolve(item["model"], relocations)))
        if model.feature_names_ != item["features"]:
            raise ResearchError("e5_assessment_model_feature_contract")
        matrix = catboost_frame(frame, item["features"], item["categories"])
        if direction or persistence:
            if list(model.classes_) != ([0, 1, 2] if direction else [0, 1]):
                raise ResearchError("e5_assessment_class_order")
            values = model.predict_proba(matrix, thread_count=6)
            if (
                not np.isfinite(values).all()
                or (values < 0).any()
                or not np.allclose(values.sum(axis=1), 1)
            ):
                raise ResearchError("e5_assessment_invalid_probability")
            out[name] = values if direction else values[:, 1]
        else:
            values = np.asarray(model.predict(matrix, thread_count=6))
            if not np.isfinite(values).all():
                raise ResearchError("e5_assessment_invalid_amount")
            out[name] = values
    return out


def _routes(
    frame: pd.DataFrame, raw: dict[str, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    current, names = route(
        frame, *[raw[f"cb_{peer}_current"] for peer in ("package", "equal", "broad")]
    )
    advance, _ = route(
        frame, *[raw[f"cb_{peer}_advance"] for peer in ("package", "equal", "broad")]
    )
    return current, mixed(advance, current, frame.advance_abc), names


def calibration(root: Path, bundle_path: Path) -> Path:
    path = root / "final-assessment/calibration.json"
    bundle = read(bundle_path)
    if path.exists():
        saved = read(path)
        if saved["models"]["sha256"] != pin(bundle_path)["sha256"]:
            raise ResearchError("e5_assessment_calibration_models_changed")
        for reference in saved["inputs"].values():
            checked(reference)
        return path
    manifest = read(root / "inputs/manifest.json")
    features = pd.read_parquet(checked(manifest["files"]["features"]))
    frame = features.loc[
        features.period_start.ge(FIT) & features.period_start.lt(ORIGIN)
    ].reset_index(drop=True)
    labels = pd.read_parquet(checked(manifest["files"]["outcomes"]))
    labelled = attach_outcomes(frame, labels)
    if frame.empty or not frame.eligible.all():
        raise ResearchError("e5_assessment_invalid_calibration_population")
    _check_models(bundle, FIT)
    raw = _raw(frame, bundle, calibration_only=True)
    current, hybrid, _ = _routes(frame, raw)
    temperature = {}
    for mode, probability in (("current", current), ("hybrid", hybrid)):
        temperature[mode] = temperature_once(
            root, "final", f"direction_{mode}", labelled, probability, ORIGIN
        )
    binary = {}
    if bundle["persistence_enabled"]:
        for mode in ("current", "advance"):
            binary[mode] = temperature_once(
                root,
                "final",
                f"persistence_{mode}",
                labelled,
                raw[f"persistence_{mode}"],
                ORIGIN,
                binary=True,
            )
    frozen_json(
        path,
        {
            "version": "e5-final-calibration-v1",
            "models": pin(bundle_path),
            "inputs": {k: manifest["files"][k] for k in ("features", "outcomes")},
            "origin_start": FIT,
            "origin_end_exclusive": ORIGIN,
            "temperature": temperature,
            "persistence": binary,
            "temperature_fits": 2 + len(binary),
            "created_utc": utc(),
        },
    )
    return path


def _population(frame: pd.DataFrame, path: Path) -> pd.DataFrame:
    saved = pd.read_parquet(path)
    saved = saved.loc[pd.to_datetime(saved.period_start).eq(pd.Timestamp(ORIGIN))]
    saved = _aligned(frame, saved)
    if "behavior_selected_excluded" not in saved:
        raise ResearchError("e5_assessment_population_decision_missing")
    # Explicit allowlist: population artifacts also contain E4 outcomes/forecasts.
    admitted = {"identity_reason", "lagged_reason"} | {
        role + suffix
        for role in ("package_distributor", "drug_distributor", "authorisation_holder")
        for suffix in ("_name", "_family")
    }
    columns = [x for x in saved if x in admitted]
    columns += [
        x for x in ("behavior_selected_excluded", "package_parallel_import_code") if x in saved
    ]
    out = frame.copy()
    for column in columns:
        out[column] = saved[column].to_numpy()
    out["policy_eligible"] = out.eligible & ~saved.behavior_selected_excluded.fillna(
        False
    ).to_numpy(dtype=bool)
    out["eligibility_reason"] = np.where(
        ~out.eligible,
        out.exclusion_reason,
        np.where(
            ~out.policy_eligible,
            "excluded_frozen_development_behaviour_policy",
            out.get("lagged_reason", "retained_origin_policy"),
        ),
    )
    return out


def predict(
    frame: pd.DataFrame,
    bundle_path: Path,
    calibration_path: Path,
    reference_path: Path,
    population_path: Path,
    *,
    interval_adopted: bool,
    relocations: Relocations | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if any(
        str(x).startswith(("next_", "second_", "outcome")) or x == "persistence_target"
        for x in frame
    ):
        raise ResearchError("e5_assessment_outcomes_in_candidate_inputs")
    bundle, states = read(bundle_path), read(calibration_path)
    _check_models(bundle, ORIGIN, relocations)
    if states["models"]["sha256"] != pin(bundle_path)["sha256"]:
        raise ResearchError("e5_assessment_calibration_binding")
    for state in [*states["temperature"].values(), *states["persistence"].values()]:
        if (
            state["last_maturity"] >= state["cutoff"]
            or state["cutoff"] > ORIGIN
            or not np.isfinite(state["temperature"])
            or not 0.5 <= state["temperature"] <= 3
        ):
            raise ResearchError("e5_assessment_calibration_maturity_boundary")
    reference = _aligned(frame, pd.read_parquet(reference_path))
    for column in ("package_number", "aip_ore", "eligible", "identity_episode_id"):
        pd.testing.assert_series_equal(
            frame[column].reset_index(drop=True),
            reference[column],
            check_names=False,
            check_dtype=False,
        )
    out = _population(frame, population_path)
    predictable = frame.eligible & frame.aip_ore.gt(0)
    use = frame.loc[predictable].copy().reset_index(drop=True)
    if use.empty:
        raise ResearchError("e5_assessment_no_predictions")
    raw = _raw(use, bundle, relocations=relocations)
    current, hybrid, names = _routes(use, raw)
    p_current = calibrate(current, states["temperature"]["current"])
    valid = use.advance_abc.isin(["A", "B", "C"]).to_numpy()
    calibration_route = "hybrid" if valid.any() else "current"
    p = calibrate(hybrid, states["temperature"]["hybrid"]) if valid.any() else p_current
    price = use.aip_ore.to_numpy(dtype=float) / 100

    def choose(stem: str) -> np.ndarray:
        return np.where(valid, raw[f"{stem}_advance"], raw[f"{stem}_current"])

    mean_raw = choose("mean")
    median_raw = choose("amount_delta_floor")
    mean = np.maximum(0, price + mean_raw)
    median = np.maximum(0, price + median_raw)
    quantile_raw = np.where(valid[:, None], raw["quantile_advance"], raw["quantile_current"])
    quantile = np.maximum(0, price[:, None] + np.sort(quantile_raw, axis=1))
    residual = read(_resolve(bundle["amount_interval_calibration"], relocations))["residual"]
    accepted_bounds = {}
    for mode in ("current", "advance"):
        state = residual[f"2026_{mode}"]
        if state["last_maturity"] >= state["cutoff"] or state["cutoff"] > ORIGIN:
            raise ResearchError("e5_assessment_accepted_interval_maturity")
        bounds = intervals(
            use.assign(next_aip_dkk=np.maximum(0, price + raw[f"amount_delta_floor_{mode}"])), state
        )
        accepted_bounds[mode] = (bounds.lower90_dkk.to_numpy(), bounds.upper90_dkk.to_numpy())
    accepted_low = (
        np.where(valid, accepted_bounds["advance"][0], accepted_bounds["current"][0]) - price
    )
    accepted_high = (
        np.where(valid, accepted_bounds["advance"][1], accepted_bounds["current"][1]) - price
    )
    values = {
        "p_fall": p[:, 0],
        "p_unchanged": p[:, 1],
        "p_rise": p[:, 2],
        "current_only_p_fall": p_current[:, 0],
        "current_only_p_rise": p_current[:, 2],
        "raw_forecast_delta_dkk": mean_raw,
        "forecast_delta_dkk": mean - price,
        "forecast_aip_dkk": mean,
        "forecast_percent": 100 * (mean - price) / price,
        "median_delta_dkk": median - price,
        "raw_median_delta_dkk": median_raw,
        "raw_q05_delta_dkk": quantile_raw[:, 0],
        "raw_q50_delta_dkk": quantile_raw[:, 1],
        "raw_q95_delta_dkk": quantile_raw[:, 2],
        "quantile_crossed": (np.diff(quantile_raw, axis=1) < 0).any(axis=1),
        "experimental_lower_delta_dkk": quantile[:, 0] - price,
        "experimental_median_delta_dkk": quantile[:, 1] - price,
        "experimental_upper_delta_dkk": quantile[:, 2] - price,
        "accepted_lower_delta_dkk": accepted_low,
        "accepted_upper_delta_dkk": accepted_high,
        "lower_delta_dkk": quantile[:, 0] - price if interval_adopted else accepted_low,
        "upper_delta_dkk": quantile[:, 2] - price if interval_adopted else accepted_high,
        "peer_route": names,
        "information_route": np.where(valid, "advance ABC", "current-only fallback"),
        "calibration_route": np.full(len(use), calibration_route),
    }
    if bundle["persistence_enabled"]:
        probabilities = {}
        for mode in ("current", "advance"):
            probabilities[mode] = expit(
                logit(np.clip(raw[f"persistence_{mode}"], 1e-12, 1 - 1e-12))
                / states["persistence"][mode]["temperature"]
            )
        values["persistence_probability"] = np.where(
            valid, probabilities["advance"], probabilities["current"]
        )
    else:
        values["persistence_probability"] = np.full(len(use), np.nan)
    for column, value in values.items():
        # Reindexing a typed column retains native numeric missingness and booleans.
        typed = pd.Series(value, index=out.index[predictable])
        if column == "quantile_crossed":
            typed = typed.astype("boolean")
        elif column in {"peer_route", "information_route", "calibration_route"}:
            typed = typed.astype("string")
        out[column] = typed.reindex(out.index)
    out["prediction_state"] = np.where(predictable, "available", "excluded_origin")
    out["interval_method"] = (
        "E5 experimental quantiles accepted for research review"
        if interval_adopted
        else "Retained E4 price-band residual interval; E5 quantiles rejected"
    )
    out["reference_p_rise"] = reference.p_rise
    out["reference_p_fall"] = reference.p_fall
    out["selected_e4_reference"] = reference.selected.fillna(False).astype(bool)
    out["rank_e4_reference"] = reference["rank"]
    finance_columns = [
        "row_key",
        "package_number",
        "period_start",
        "aip_ore",
        "eligible",
        "origin_group",
        "pack_size_token",
        "pack_unit",
        "peer_count",
        "peer_feature_state",
        "p_rise",
        "p_fall",
        "forecast_delta_dkk",
        "lower_delta_dkk",
        "persistence_probability",
        "reference_p_rise",
        "reference_p_fall",
    ]
    finance = out.loc[predictable, [x for x in finance_columns if x in out]].copy()
    finance["package_id"] = finance.package_number
    finance["eligible"] = out.loc[predictable, "policy_eligible"]
    finance["reference_eligible"] = True
    queues = queue_policies(finance, include_persistence=bundle["persistence_enabled"])
    for policy in queues.policy.unique():
        selected = _aligned(
            out.loc[predictable],
            queues.loc[queues.policy.eq(policy) & queues.workload.eq("natural")],
        )
        out[f"rank_{policy}"] = 0
        out[f"selected_{policy}"] = False
        out.loc[predictable, f"rank_{policy}"] = selected["rank"].to_numpy()
        out.loc[predictable, f"selected_{policy}"] = selected.selected.to_numpy()
    same_information = queue_policies(
        finance.drop(columns=["reference_p_rise", "reference_p_fall", "reference_eligible"])
    )
    probability_queue = _aligned(
        out.loc[predictable],
        same_information.loc[
            same_information.policy.eq("probability_reference")
            & same_information.workload.eq("natural")
        ],
    )
    out["rank_e5_probability"] = 0
    out["selected_e5_probability"] = False
    out.loc[predictable, "rank_e5_probability"] = probability_queue["rank"].to_numpy()
    out.loc[predictable, "selected_e5_probability"] = probability_queue.selected.to_numpy()
    # The frozen E4 reference remains its original population and selections.
    out["rank_probability_reference"] = out.rank_e4_reference
    out["selected_probability_reference"] = out.selected_e4_reference
    primary = "downside_aware" if interval_adopted else "probability_reference"
    out["policy"] = primary
    out["rank"] = out[f"rank_{primary}"]
    out["selected"] = out[f"selected_{primary}"]
    supported_keys = queues.loc[
        queues.policy.eq(primary) & queues.workload.eq("natural") & queues.score_supported,
        "row_key",
    ]
    # The reference keeps its original eligibility, even when the new
    # recommendation policy flags a row. These explanations do not change queues.
    primary_eligible = out.eligible if primary == "probability_reference" else out.policy_eligible
    out["selection_reason"] = np.select(
        [
            out.selected,
            ~out.eligible,
            ~primary_eligible,
            ~out.row_key.isin(supported_keys),
        ],
        [
            f"Frozen research selection: {primary}",
            "Origin ineligible: " + out.eligibility_reason.astype(str),
            "Excluded by frozen recommendation policy: " + out.eligibility_reason.astype(str),
            f"Prediction unavailable for frozen policy: {primary}",
        ],
        default=f"Outside frozen threshold or origin cap: {primary}",
    )
    out["ranking_interpretation"] = (
        "Experimental price-exposure ranking; no pharmacy profit or purchase quantity"
    )
    return out, queues


def _producer() -> dict[str, Any]:
    directory = Path(__file__).parent
    names = [
        "e5_assessment.py",
        "e5_finance.py",
        "e4_assessment.py",
        "e4_methods.py",
        "e3_methods.py",
        "e2_models.py",
        "e5_results.py",
    ]
    return {name: pin(directory / name) for name in names}


def prepare(root: Path, *, interval_adopted: bool | None = None) -> Path:
    """Call only after final fits, population report and aggregate results exist."""
    pointer = root / "final-assessment.json"
    if pointer.exists():
        manifest = checked(read(pointer)["assessment"])
        saved = read(manifest)
        if (
            interval_adopted is not None
            and saved["binding"]["interval_adopted"] != interval_adopted
        ):
            raise ResearchError("e5_assessment_changed_adoption_requires_new_version")
        for key in ("inputs", "predictions", "queues"):
            checked(saved[key])
        checked(read(pointer)["outcome"])
        for key in (
            "models",
            "calibration",
            "reference",
            "population",
            "population_report",
            "results",
            "selection",
        ):
            checked(saved["binding"][key])
        _check_models(read(checked(saved["binding"]["models"])), ORIGIN)
        return manifest
    protocol = require(root)
    selection = read(root / "selection.json")
    results_path = root / "results.json"
    result = read(results_path)
    if interval_adopted is None:
        interval_adopted = result["selected_configuration"]["quantile_interval_accepted"]
    if type(interval_adopted) is not bool:
        raise ResearchError("e5_assessment_interval_adoption_not_boolean")
    bundle_path = model_bundle(root, persistence_enabled=selection["persistence_policy_enabled"])
    bundle = read(bundle_path)
    direction_cutoff = bundle.get("direction_fit_cutoff", FIT)
    amount_cutoff = bundle.get("amount_fit_cutoff", FIT)
    component_cutoffs = bundle.get(
        "component_fit_cutoffs", {k: v["fit_cutoff"] for k, v in bundle["models"].items()}
    )
    calibration_path = calibration(root, bundle_path)
    population_report_path = root / "population/report.json"
    population_report = read(population_report_path)
    population_path = checked(
        next(
            x
            for x in population_report["artifacts"]
            if Path(x["path"]).name == "population.parquet"
        )
    )
    feature_ref = read(root / "inputs/manifest.json")["files"]["current"]
    features_path = checked(feature_ref)
    feature = pd.read_parquet(features_path)
    lifecycle = read(checked(protocol["pins"]["e4_lifecycle"]))
    reference_assessment_ref = next(
        x["assessment"] for x in lifecycle["assessments"] if x["origin"] == ORIGIN
    )
    reference_assessment = read(checked(reference_assessment_ref))
    if reference_assessment["inputs"]["sha256"] != feature_ref["sha256"]:
        raise ResearchError("e5_assessment_changed_original_population")
    reference_path = checked(reference_assessment["predictions"])
    source_refs = [
        pin(bundle_path),
        pin(calibration_path),
        pin(features_path),
        pin(population_report_path),
        pin(population_path),
        pin(results_path),
        pin(root / "selection.json"),
        reference_assessment_ref,
    ]
    context_path = root / "final-assessment/context.json"
    if context_path.exists():
        context = read(context_path)
    else:
        now = utc()
        if now[:10] >= TARGET:
            raise ResearchError("e5_assessment_target_not_future_at_actual_preparation")
        context = {
            "origin": ORIGIN,
            "target": TARGET,
            "calendar_next_start": TARGET,
            "decision_cutoff": now,
            "mode": "prospective_pending",
            "target_outcome_exposed": False,
            "sources": source_refs,
            "availability": (
                "Actual newly frozen E5 current-only assessment; source intake and "
                "original E4 creation clocks remain separate"
            ),
            "availability_evidence": [
                {
                    "artifact": ref,
                    "available_utc": now,
                    "basis": "Pinned local evidence existed at actual E5 preparation cutoff",
                }
                for ref in source_refs
            ],
        }
        frozen_json(context_path, context)
    validate_features(feature, context)
    if feature.advance_abc.isin(["A", "B", "C"]).any():
        raise ResearchError("e5_assessment_current_period_has_unapproved_advance_report")
    binding = {
        "version": VERSION,
        "inputs": feature_ref,
        "context": context,
        "models": pin(bundle_path),
        "calibration": pin(calibration_path),
        "reference": reference_assessment["predictions"],
        "population": pin(population_path),
        "population_report": pin(population_report_path),
        "results": pin(results_path),
        "selection": pin(root / "selection.json"),
        "interval_adopted": interval_adopted,
        "producer": _producer(),
        "policy": "downside_aware" if interval_adopted else "probability_reference",
        "scope": "Research assessment only; no operational promotion or quantities",
    }
    key = identity(binding)
    destination = root / "assessments" / key
    destination.mkdir(parents=True, exist_ok=True)
    prepared = destination / "prepared.json"
    frozen_json(
        prepared,
        {"assessment_id": key, "binding": binding, "created_utc": context["decision_cutoff"]},
    )
    inputs = destination / "inputs.parquet"
    if not inputs.exists():
        inputs.write_bytes(features_path.read_bytes())
    checked({**feature_ref, "path": str(inputs)})
    predictions, queues = predict(
        feature,
        bundle_path,
        calibration_path,
        reference_path,
        population_path,
        interval_adopted=interval_adopted,
    )
    if context["mode"] == "prospective_pending" and utc()[:10] >= TARGET:
        raise ResearchError("e5_assessment_target_passed_during_preparation")
    predictions.to_parquet(destination / "predictions.parquet", index=False)
    queues.to_parquet(destination / "queues.parquet", index=False)
    manifest = destination / "assessment.json"
    frozen_json(
        manifest,
        {
            "version": VERSION,
            "assessment_id": key,
            "binding": binding,
            "context": context,
            "created_utc": context["decision_cutoff"],
            "completed_utc": utc(),
            "inputs": pin(inputs),
            "predictions": pin(destination / "predictions.parquet"),
            "queues": pin(destination / "queues.parquet"),
            "candidate_count": len(feature),
            "predicted": int(predictions.prediction_state.eq("available").sum()),
            "selected": int(predictions.selected.sum()),
            "fit_cutoff": direction_cutoff,
            "amount_fit_cutoff": amount_cutoff,
            "planned_final_fit_cutoff": FIT,
            "component_fit_cutoffs": component_cutoffs,
            "resource_fallback": bundle.get("resource_fallback"),
            "retained_component_fit_cutoff": "2026-01-01",
            "training_scope": (
                f"Broader direction: trailing three years before {direction_cutoff}; "
                f"mean and quantiles before {amount_cutoff}; retained package/equal/median "
                "models before 2026-01-01. Label maturity is strictly before each cutoff."
            ),
            "status": context["mode"],
            "e4_reference_selections_unchanged": True,
        },
    )
    outcome = attach_outcome(manifest, None, method="e5-official-aip-v1")
    frozen_json(
        pointer,
        {
            "version": "e5-final-assessment-pointer-v1",
            "assessment": pin(manifest),
            "outcome": pin(outcome),
        },
    )
    return manifest


def replay(assessment_path: Path, *, relocations: Relocations | None = None) -> dict[str, Any]:
    """Use only the assessment's captured pins; reproduce complete saved tables."""
    saved = read(assessment_path)
    binding = saved["binding"]
    if saved["version"] != VERSION:
        raise ResearchError("e5_assessment_wrong_replay_version")
    for name, reference in binding["producer"].items():
        # The active module directory is the captured producer during recovery.
        checked({**reference, "path": str(Path(__file__).with_name(name))})
    for key in ("population_report", "results", "selection"):
        _resolve(binding[key], relocations)
    feature = pd.read_parquet(_resolve(saved["inputs"], relocations))
    actual, queues = predict(
        feature,
        _resolve(binding["models"], relocations),
        _resolve(binding["calibration"], relocations),
        _resolve(binding["reference"], relocations),
        _resolve(binding["population"], relocations),
        interval_adopted=binding["interval_adopted"],
        relocations=relocations,
    )
    pd.testing.assert_frame_equal(
        actual, pd.read_parquet(_resolve(saved["predictions"], relocations)), check_exact=True
    )
    pd.testing.assert_frame_equal(
        queues, pd.read_parquet(_resolve(saved["queues"], relocations)), check_exact=True
    )
    return {
        "version": "e5-exact-assessment-replay-v1",
        "assessment": pin(assessment_path),
        "rows": len(actual),
        "queue_rows": len(queues),
        "exact_predictions": True,
        "exact_queues": True,
        "created_utc": utc(),
    }
