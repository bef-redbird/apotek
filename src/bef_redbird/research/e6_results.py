"""Saved, paired E6 amount/history evaluation with explicit component lineage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e2_data import attach_outcomes
from .e2_metrics import probability_metrics
from .e3_methods import calibrate, select_queue, temperature_fit
from .e4_common import checked, frozen_json, read, utc
from .e4_methods import mixed, route
from .e5_results import Predictions, align
from .e6_experiment import LABELS, fingerprint, load_window, training_cohort
from .e6_protocol import FOLDS, require
from .group_features import pin
from .training_contract import KEYS, atomic_json

PROBS = ["p0", "p1", "p2"]


def temperature_once(
    root: Path, fold: str, key: str, frame: pd.DataFrame, probability: np.ndarray, cutoff: str
) -> dict[str, Any]:
    """Forty scalar calibrations implied by the frozen 3-fold history matrix."""
    directory = root / "evaluations" / fold
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "calibration-checkpoint.json"
    data = frame[
        ["row_key", "period_start", "next_status", "next_maturity_date", "next_direction"]
    ].copy()
    data[PROBS] = probability
    binding = {
        "input_sha256": fingerprint(data),
        "cutoff": cutoff,
        "protocol": pin(root / "protocol.json"),
        "method": "bounded-temperature-v1",
    }
    checkpoint = read(path) if path.exists() else {"states": {}}
    if key in checkpoint["states"]:
        saved = checkpoint["states"][key]
        if saved["binding"] != binding:
            raise ResearchError("e6_calibration_inputs_changed")
        return saved["state"]
    count = sum(
        len(read(p)["states"]) for p in (root / "evaluations").glob("*/calibration-checkpoint.json")
    )
    if count >= 40:
        raise ResearchError("e6_calibration_matrix_limit")
    state = temperature_fit(data, cutoff)
    checkpoint["states"][key] = {"binding": binding, "state": state, "created_utc": utc()}
    atomic_json(path, checkpoint)
    return state


def strata(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "all": np.ones(len(frame), dtype=bool),
        "price_below100": frame.aip_ore.lt(10000).to_numpy(),
        "price_100to1000": (frame.aip_ore.ge(10000) & frame.aip_ore.lt(100000)).to_numpy(),
        "price_atleast1000": frame.aip_ore.ge(100000).to_numpy(),
        "history_below6": frame.history_periods.lt(6).to_numpy(),
        "history_atleast6": frame.history_periods.ge(6).to_numpy(),
        "peers_positive": frame.peer_count.ge(1).fillna(False).to_numpy(dtype=bool),
        "peers_zero": frame.peer_count.eq(0).fillna(False).to_numpy(dtype=bool),
        "peers_unknown": frame.peer_count.isna().to_numpy(),
    }


def amount_one(frame: pd.DataFrame, delta: np.ndarray) -> dict[str, Any]:
    observed = frame.next_status.eq("observed").to_numpy()
    actual = frame.loc[observed, "next_delta_ore"].to_numpy(dtype=float) / 100
    predicted = delta[observed]
    result: dict[str, Any] = {
        "candidates": len(frame),
        "observed": int(observed.sum()),
        "unknown": int((~observed).sum()),
    }
    if not len(actual):
        return result
    error = predicted - actual
    absolute = np.abs(error)
    top = max(1, int(np.ceil(len(error) * 0.01)))
    result.update(
        mae_dkk=float(absolute.mean()),
        rmse_dkk=float(np.sqrt(np.mean(error**2))),
        bias_dkk=float(error.mean()),
        predicted_total_dkk=float(predicted.sum()),
        observed_total_dkk=float(actual.sum()),
        maximum_error_dkk=float(absolute.max()),
        p99_error_dkk=float(np.quantile(absolute, 0.99)),
        top1_absolute_error_share=float(
            np.sort(absolute)[-top:].sum() / max(absolute.sum(), 1e-15)
        ),
        top1_squared_error_share=float(
            np.sort(error**2)[-top:].sum() / max((error**2).sum(), 1e-15)
        ),
    )
    return result


def direction_one(frame: pd.DataFrame, probability: np.ndarray) -> dict[str, Any]:
    observed = frame.next_status.eq("observed").to_numpy()
    result: dict[str, Any] = {
        "candidates": len(frame),
        "observed": int(observed.sum()),
        "unknown": int((~observed).sum()),
    }
    if observed.any():
        y = frame.loc[observed, "next_direction"].to_numpy(dtype=int) + 1
        p = probability[observed]
        result.update(probability_metrics(y, p))
        result["logloss"] = float(-np.log(np.maximum(p[np.arange(len(y)), y], 1e-15)).mean())
    return result


def evaluate_values(
    frame: pd.DataFrame, values: np.ndarray, task: str, identity: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scorer = amount_one if task == "amount" else direction_one
    periods, summaries = [], []
    for name, mask in strata(frame).items():
        part = frame.loc[mask].reset_index(drop=True)
        v = values[mask]
        local = []
        for day, rows in part.groupby("period_start", sort=True):
            score = scorer(rows, v[rows.index])
            local.append({**identity, "stratum": name, "period_start": str(day)[:10], **score})
        periods.extend(local)
        overall = scorer(part, v)
        metrics = (
            ["mae_dkk", "rmse_dkk", "bias_dkk"] if task == "amount" else ["brier", "ece", "logloss"]
        )
        for metric in metrics:
            observed = [x[metric] for x in local if metric in x]
            overall["equal_period_" + metric] = float(np.mean(observed)) if observed else None
        summaries.append(
            {
                **identity,
                "stratum": name,
                "periods": len(local),
                "observed_periods": sum(x["observed"] > 0 for x in local),
                **overall,
            }
        )
    return summaries, periods


def queue_diagnostics(
    frame: pd.DataFrame, p: np.ndarray
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    decision = frame[list(KEYS)].copy()
    decision[PROBS] = p
    selected = select_queue(decision, "threshold", (0.35, 0.10))
    selected = selected.merge(
        frame[["row_key", "next_status", "next_delta_ore", "next_direction"]],
        on="row_key",
        how="left",
        validate="one_to_one",
    )
    records = []
    for day, all_rows in selected.groupby("period_start", sort=True):
        chosen = all_rows.loc[all_rows.selected]
        known = chosen.loc[chosen.next_status.eq("observed")]
        delta = known.next_delta_ore.to_numpy(dtype=float) / 100
        records.append(
            {
                "period_start": str(day)[:10],
                "candidates": len(all_rows),
                "selected": len(chosen),
                "candidate_rate": len(chosen) / len(all_rows),
                "observed": len(known),
                "unknown": len(chosen) - len(known),
                "unknown_rate": (len(chosen) - len(known)) / len(chosen) if len(chosen) else None,
                "rise_rate": float((delta > 0).mean()) if len(delta) else None,
                "flat_rate": float((delta == 0).mean()) if len(delta) else None,
                "fall_rate": float((delta < 0).mean()) if len(delta) else None,
                "net_dkk": float(delta.sum()),
                "downside_dkk": float(-np.minimum(delta, 0).sum()),
                "net_dkk_per_observed": float(delta.mean()) if len(delta) else None,
                "downside_dkk_per_observed": float(-np.minimum(delta, 0).mean())
                if len(delta)
                else None,
                "unknown_reasons": chosen.loc[chosen.next_status.ne("observed"), "next_status"]
                .value_counts()
                .to_dict(),
            }
        )
    return selected, records


class Store:
    def __init__(self, root: Path, fold: dict[str, Any], frame: pd.DataFrame) -> None:
        self.root, self.fold, self.frame = root, fold, frame
        self.protocol = require(root)
        self.e5 = checked(self.protocol["pins"]["e5_protocol"]).parent
        self.prior = Predictions(self.e5, fold["reference_year"])
        self.reused: dict[str, Any] = {}

    def old(self, method: str) -> pd.DataFrame:
        key = f"evaluation_{self.fold['reference_year']}-{method}"
        receipt = self.prior.old_ledger[key]
        if receipt["fit_cutoff"] >= self.fold["start"]:
            raise ResearchError("e6_reused_component_chronology")
        directory = self.prior.prior / "trials" / key
        self.reused[key] = {
            "receipt": pin(directory / "trial.json"),
            "fit_cutoff": receipt["fit_cutoff"],
            "files": [{**x, "path": str(directory / x["path"])} for x in receipt["files"]],
        }
        return self.prior.old(self.frame, method)

    def e5_prediction(self, method: str, mode: str) -> pd.DataFrame:
        fold = "screen" if self.fold["name"] == "earlier" else self.fold["name"]
        path = checked(self.protocol["pins"][f"e5_trial_{fold}-{method}-{mode}"])
        receipt = read(path)
        if receipt["spec"]["fit_cutoff"] != self.fold["fit"]:
            raise ResearchError("e6_reused_fit_cutoff")
        self.reused[path.parent.name] = {
            "receipt": pin(path),
            "fit_cutoff": self.fold["fit"],
            "files": list(receipt["files"].values()),
        }
        return self.prior.new(self.frame, fold, method, mode)

    def get(
        self, task: str, history: str, mode: str, *, objective: str = "rmse"
    ) -> pd.DataFrame | None:
        if history == "3y" and objective != "huber":
            if task == "direction" and self.fold["name"] == "earlier":
                saved = self.old(f"cb_broad_{mode}")
                return saved.rename(columns={f"p{k}_perfect": f"p{k}" for k in range(3)})
            return self.e5_prediction("fresh" if task == "direction" else objective, mode)
        name = (
            f"{self.fold['name']}-huber-{mode}"
            if objective == "huber"
            else f"{self.fold['name']}-{task}-{history}-{mode}"
        )
        path = self.root / "trials" / name / "receipt.json"
        if not path.exists() or read(path)["status"] != "completed":
            return None
        return align(
            self.frame, pd.read_parquet(checked(read(path)["files"]["predictions.parquet"]))
        )


def require_terminal_trials(root: Path, fold: str, investigation: str, task: str) -> None:
    """Do not freeze pending arms as if they were terminal resource failures."""
    for spec in require(root)["matrix"]:
        if (spec["fold"], spec["investigation"], spec["task"]) != (fold, investigation, task):
            continue
        path = root / "trials" / spec["id"] / "receipt.json"
        if not path.exists() or read(path).get("status") not in {"completed", "failed", "skipped"}:
            raise ResearchError("e6_evaluation_declared_trial_pending:" + spec["id"])


def amount_fold(root: Path, fold: dict[str, Any], investigation: str) -> dict[str, Any]:
    require_terminal_trials(root, fold["name"], investigation, "amount")
    dest = root / "evaluations" / f"{investigation}-{fold['name']}"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "result.json"
    if target.exists():
        result = read(target)
        for p in result["files"].values():
            checked(p)
        return result
    frame = load_window(root, fold["cal_start"], fold["end"])
    labels = load_window(root, fold["cal_start"], fold["end"], LABELS, outcomes=True)
    store = Store(root, fold, frame)
    evaluation = frame.period_start.ge(fold["start"]).to_numpy()
    test = attach_outcomes(frame, labels).loc[evaluation].reset_index(drop=True)
    methods = (
        [("3y", objective) for objective in ("mae", "rmse", "huber")]
        if investigation == "C"
        else [(history, "rmse") for history in ("3y", "6y", "full")]
    )
    summaries, periods, saved_frames, missing = [], [], [], []
    for history, objective in methods:
        pair = {
            mode: store.get("amount", history, mode, objective=objective)
            for mode in ("current", "advance")
        }
        if any(value is None for value in pair.values()):
            missing.append(
                {
                    "investigation": investigation,
                    "task": "amount",
                    "fold": fold["name"],
                    "history": history,
                    "objective": objective,
                    "reason": "fit unavailable; predictive question unanswered",
                }
            )
            continue
        raw, supported = {}, {}
        for mode, value in pair.items():
            assert value is not None
            raw[mode] = value.raw_delta_dkk.to_numpy()
            supported[mode] = value.delta_dkk.to_numpy()
        usable = frame.advance_abc.isin(["A", "B", "C"]).to_numpy()
        for mode_values in (raw, supported):
            mode_values["hybrid"] = np.where(usable, mode_values["advance"], mode_values["current"])
        for mode in ("current", "hybrid"):
            for support, values in (("raw", raw[mode]), ("supported", supported[mode])):
                identity = {
                    "investigation": investigation,
                    "fold": fold["name"],
                    "task": "amount",
                    "method": objective,
                    "history": history,
                    "information": mode,
                    "support": support,
                }
                summary, by_period = evaluate_values(test, values[evaluation], "amount", identity)
                summaries.extend(summary)
                periods.extend(by_period)
            saved = test[
                [
                    *KEYS,
                    "aip_ore",
                    "next_status",
                    "next_direction",
                    "next_delta_ore",
                    "next_maturity_date",
                ]
            ].copy()
            saved["raw_delta_dkk"] = raw[mode][evaluation]
            saved["delta_dkk"] = supported[mode][evaluation]
            saved["next_price_dkk"] = saved.aip_ore / 100 + saved.delta_dkk
            if (saved.next_price_dkk < -1e-10).any():
                raise ResearchError("e6_invalid_supported_price")
            saved["support_adjusted"] = ~np.isclose(
                saved.raw_delta_dkk, saved.delta_dkk, rtol=0, atol=1e-12
            )
            for key, tag in {
                "history": history,
                "method": objective,
                "information": mode,
            }.items():
                saved[key] = tag
            saved_frames.append(saved)
    pd.concat(saved_frames, ignore_index=True).to_parquet(dest / "predictions.parquet", index=False)
    pd.DataFrame(periods).to_parquet(dest / "periods.parquet", index=False)
    result = {
        "version": "e6-amount-results-v1",
        "protocol": pin(root / "protocol.json"),
        "summaries": summaries,
        "missing": missing,
        "reused": store.reused,
        "files": {n: pin(dest / n) for n in ("predictions.parquet", "periods.parquet")},
        "created_utc": utc(),
        "uncertainty_intervals": "E5 retained unchanged; no intervals recentered on Huber.",
    }
    frozen_json(target, result)
    return result


def direction_fold(root: Path, fold: dict[str, Any]) -> dict[str, Any]:
    require_terminal_trials(root, fold["name"], "D", "direction")
    dest = root / "evaluations" / f"direction-{fold['name']}"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "result.json"
    if target.exists():
        result = read(target)
        for p in result["files"].values():
            checked(p)
        return result
    frame = load_window(root, fold["cal_start"], fold["end"])
    labels = load_window(root, fold["cal_start"], fold["end"], LABELS, outcomes=True)
    labelled = attach_outcomes(frame, labels)
    evaluation = frame.period_start.ge(fold["start"]).to_numpy()
    calibration = ~evaluation
    test = labelled.loc[evaluation].reset_index(drop=True)
    store = Store(root, fold, frame)
    fallbacks = {
        mode: [
            store.old(f"cb_{route_name}_{mode}")[[f"p{k}_perfect" for k in range(3)]].to_numpy()
            for route_name in ("package", "equal")
        ]
        for mode in ("current", "advance")
    }
    summaries, periods, saved_frames, queues, queue_periods, missing, states = (
        [],
        [],
        [],
        [],
        [],
        [],
        {},
    )
    histories = ["3y", "6y", "full"] + (["weighted"] if fold["name"] == "summer" else [])
    for history in histories:
        pair = {mode: store.get("direction", history, mode) for mode in ("current", "advance")}
        if any(value is None for value in pair.values()):
            missing.append(
                {
                    "investigation": "D",
                    "task": "direction",
                    "fold": fold["name"],
                    "history": history,
                    "reason": "fit unavailable; predictive question unanswered",
                }
            )
            continue
        raw: dict[str, np.ndarray] = {}
        for mode, value in pair.items():
            assert value is not None
            p = value[PROBS].to_numpy()
            if not np.isfinite(p).all() or (p < 0).any() or not np.allclose(p.sum(axis=1), 1):
                raise ResearchError("e6_invalid_probability")
            raw[f"component_{mode}"] = p
            raw[f"routed_{mode}"] = route(frame, fallbacks[mode][0], fallbacks[mode][1], p)[0]
        raw["routed_hybrid"] = mixed(
            raw["routed_advance"], raw["routed_current"], frame.advance_abc
        )
        del raw["routed_advance"]
        for name, p in raw.items():
            route_name, mode = name.split("_")
            key = f"D_{history}_{name}"
            cal = labelled.loc[calibration].copy()
            if history == "3y" and route_name == "routed" and fold["name"] != "earlier":
                accepted_result = read(
                    checked(store.protocol["pins"][f"e5_evaluations_{fold['name']}_result"])
                )
                state_pin = accepted_result["files"]["calibration.json"]
                state = read(checked(state_pin))[f"fresh_{mode}"]
                mature = cal.next_status.eq("observed") & cal.next_maturity_date.lt(fold["start"])
                if (
                    state["cutoff"] != fold["start"]
                    or state["rows"] != int(mature.sum())
                    or state["last_maturity"]
                    != str(cal.loc[mature, "next_maturity_date"].max())[:10]
                ):
                    raise ResearchError("e6_reused_calibration_chronology")
                saved_reference = align(
                    test, pd.read_parquet(checked(accepted_result["files"]["predictions.parquet"]))
                )
                np.testing.assert_array_equal(
                    calibrate(p, state)[evaluation],
                    saved_reference[[f"fresh_{mode}_p{k}" for k in range(3)]].to_numpy(),
                )
                state = {
                    **state,
                    "reused": True,
                    "source": state_pin,
                    "exact_saved_reference_predictions": True,
                }
            else:
                state = temperature_once(
                    root, fold["name"], key, cal, p[calibration], fold["start"]
                )
            states[key] = state
            calibrated = calibrate(p, state)
            for kind, values in (("raw", p), ("calibrated", calibrated)):
                identity = {
                    "investigation": "D",
                    "fold": fold["name"],
                    "task": "direction",
                    "history": history,
                    "information": mode,
                    "route": route_name,
                    "calibration": kind,
                }
                summary, by_period = evaluate_values(
                    test, values[evaluation], "direction", identity
                )
                summaries.extend(summary)
                periods.extend(by_period)
            saved = test[
                [*KEYS, "next_status", "next_direction", "next_delta_ore", "next_maturity_date"]
            ].copy()
            saved[[f"raw_p{k}" for k in range(3)]] = p[evaluation]
            saved[PROBS] = calibrated[evaluation]
            saved["history"], saved["route"], saved["information"] = history, route_name, mode
            saved_frames.append(saved)
            if route_name == "routed":
                queue, qperiod = queue_diagnostics(test, calibrated[evaluation])
                queue["history"], queue["information"] = history, mode
                queues.append(queue)
                queue_periods.extend(
                    [
                        {**r, "history": history, "information": mode, "fold": fold["name"]}
                        for r in qperiod
                    ]
                )
    pd.concat(saved_frames, ignore_index=True).to_parquet(dest / "predictions.parquet", index=False)
    pd.concat(queues, ignore_index=True).to_parquet(dest / "queues.parquet", index=False)
    pd.DataFrame(periods).to_parquet(dest / "periods.parquet", index=False)
    result = {
        "version": "e6-direction-results-v1",
        "protocol": pin(root / "protocol.json"),
        "summaries": summaries,
        "queue_periods": queue_periods,
        "calibration": states,
        "missing": missing,
        "reused": store.reused,
        "files": {
            n: pin(dest / n) for n in ("predictions.parquet", "queues.parquet", "periods.parquet")
        },
        "created_utc": utc(),
    }
    frozen_json(target, result)
    return result


def reuse_audit(root: Path) -> dict[str, Any]:
    """Check exact cohort/recipe identity before accepting the three-year controls."""
    target = root / "reuse-audit.json"
    if target.exists():
        return read(target)
    protocol = require(root)
    bundle = read(checked(protocol["pins"]["e4_model-bundle"]))
    e3_protocol = read(checked(protocol["pins"]["e3_protocol"]))
    if e3_protocol["settings"] != protocol["settings"]:
        raise ResearchError("e6_earlier_reference_recipe_mismatch")
    prior_inputs = read(checked(protocol["pins"]["e5_inputs_manifest"]))
    old_labels = pd.read_parquet(checked(prior_inputs["files"]["outcomes"]), columns=LABELS)
    new_labels = load_window(root, "2022-01-01", "2026-10-26", LABELS, outcomes=True)
    pd.testing.assert_frame_equal(new_labels, old_labels, check_dtype=False, check_exact=True)
    label_check = {
        "rows": len(new_labels),
        "exact_keys_status_amount_direction_maturity": True,
        "e5_outcomes": prior_inputs["files"]["outcomes"],
    }
    del old_labels, new_labels
    records = []
    for fold in FOLDS:
        for task in ("direction", "amount"):
            for mode in ("current", "advance"):
                spec = bundle["models"][
                    f"cb_broad_{mode}" if task == "direction" else f"amount_delta_floor_{mode}"
                ]
                cohort = training_cohort(
                    root, {"fold": fold["name"], "history": "3y"}, spec["features"]
                )
                if task == "direction" and fold["name"] == "earlier":
                    directory = (
                        checked(protocol["pins"]["e3_protocol"]).parent
                        / "trials"
                        / f"evaluation_2025-cb_broad_{mode}"
                    )
                    receipt = read(directory / "trial.json")
                    model_ref = next(x for x in receipt["files"] if x["path"] == "model.cbm")
                    model_path = checked({**model_ref, "path": str(directory / "model.cbm")})
                    actual_cutoff = receipt["fit_cutoff"]
                    actual_maturity = receipt["latest_training_label_maturity"]
                    if (
                        receipt["spec"]["features"] != spec["features"]
                        or receipt["spec"]["categories"] != spec["categories"]
                    ):
                        raise ResearchError("e6_reference_feature_contract_mismatch")
                else:
                    name = "screen" if fold["name"] == "earlier" else fold["name"]
                    method = "fresh" if task == "direction" else "rmse"
                    receipt = read(checked(protocol["pins"][f"e5_trial_{name}-{method}-{mode}"]))
                    model_path = checked(receipt["files"]["model.cbm"])
                    actual_cutoff = receipt["spec"]["fit_cutoff"]
                    actual_maturity = receipt["latest_label_maturity"]
                    if (
                        receipt["features"] != spec["features"]
                        or receipt["categories"] != spec["categories"]
                    ):
                        raise ResearchError("e6_reference_feature_contract_mismatch")
                if len(cohort) != receipt["training_rows"]:
                    raise ResearchError("e6_reference_cohort_count_mismatch")
                if (
                    actual_cutoff != fold["fit"]
                    or actual_maturity != str(cohort.next_maturity_date.max())[:10]
                ):
                    raise ResearchError("e6_reference_cutoff_or_maturity_mismatch")
                if (
                    receipt["training_start"]
                    != str(pd.Timestamp(fold["fit"]) - pd.DateOffset(years=3))[:10]
                ):
                    raise ResearchError("e6_reference_training_start_mismatch")
                objective = "MultiClass" if task == "direction" else "RMSE"
                model_check = verify_model_recipe(model_path, spec, protocol["settings"], objective)
                records.append(
                    {
                        "fold": fold["name"],
                        "task": task,
                        "mode": mode,
                        "rows": len(cohort),
                        "first": str(cohort.period_start.min())[:10],
                        "last": str(cohort.period_start.max())[:10],
                        "last_maturity": str(cohort.next_maturity_date.max())[:10],
                        "features_equal": True,
                        "settings_equal": True,
                        "actual_model_check": model_check,
                    }
                )
    result = {
        "status": "passed",
        "checks": records,
        "label_alignment": label_check,
        "feature_alignment": "Every 2022+ feature row checked exactly against E5 "
        "during preparation.",
        "created_utc": utc(),
    }
    frozen_json(target, result)
    return result


def verify_model_recipe(
    path: Path, spec: dict[str, Any], settings: dict[str, Any], objective: str
) -> dict[str, Any]:
    from catboost import CatBoost

    model = CatBoost()
    model.load_model(str(path))
    params = model.get_all_params()
    if model.feature_names_ != spec["features"]:
        raise ResearchError("e6_actual_model_feature_names_mismatch")
    categories = [model.feature_names_[i] for i in model.get_cat_feature_indices()]
    if categories != spec["categories"]:
        raise ResearchError("e6_actual_model_categories_mismatch")
    for name in ("iterations", "depth", "learning_rate", "l2_leaf_reg", "random_seed"):
        if not np.isclose(params[name], settings[name], rtol=1e-7, atol=1e-9):
            raise ResearchError("e6_actual_model_recipe_mismatch:" + name)
    if model.tree_count_ != settings["iterations"] or params["loss_function"] != objective:
        raise ResearchError("e6_actual_model_loss_or_iterations_mismatch")
    return {
        "model": pin(path),
        "trees": model.tree_count_,
        "objective": objective,
        "verified_numeric_conversion_tolerance": "CatBoost stored float parameters rtol1e-7",
    }


def paired_interval(differences: np.ndarray, block: int) -> dict[str, Any]:
    values = np.asarray(differences, dtype=float)
    if len(values) <= block:
        # One complete circular block only rotates the same observations: its
        # zero-width interval would not express sampling uncertainty.
        return {"status": "insufficient_periods", "periods": len(values), "block": block}
    rng = np.random.default_rng(20261027)
    starts = rng.integers(0, len(values), size=(2000, int(np.ceil(len(values) / block))))
    indices = (starts[:, :, None] + np.arange(block)) % len(values)
    means = values[indices.reshape(2000, -1)[:, : len(values)]].mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return {
        "difference": float(values.mean()),
        "lower": float(low),
        "upper": float(high),
        "periods": len(values),
        "block": block,
        "repetitions": 2000,
        "interpretation": "Paired exposed retrospective temporal-block diagnostic",
    }


def aggregate(root: Path) -> dict[str, Any]:
    require(root)
    results = [read(p) for p in sorted((root / "evaluations").glob("*/result.json"))]
    summaries = [r for result in results for r in result["summaries"]]
    periods = pd.concat(
        [pd.read_parquet(checked(result["files"]["periods.parquet"])) for result in results],
        ignore_index=True,
    )
    contrasts = []
    all_cases = periods.loc[periods.stratum.eq("all")]
    for investigation, task in (("C", "amount"), ("D", "amount"), ("D", "direction")):
        part = all_cases.loc[all_cases.investigation.eq(investigation) & all_cases.task.eq(task)]
        if part.empty:
            continue
        if task == "amount":
            part = part.loc[part.support.eq("supported")]
        else:
            part = part.loc[part.route.eq("routed") & part.calibration.eq("calibrated")]
        for era, folds in (("2026", ["spring", "summer"]), ("2025", ["earlier"])):
            blockpart = part.loc[part.fold.isin(folds)]
            for mode in ("current", "hybrid"):
                modepart = blockpart.loc[blockpart.information.eq(mode)]
                arm = "method" if investigation == "C" else "history"
                reference = "mae" if investigation == "C" else "3y"
                arms = set(modepart[arm].dropna())
                comparisons = [(reference, candidate) for candidate in sorted(arms - {reference})]
                if task == "direction" and {"weighted", "full"} <= arms:
                    # Same full cohort isolates age weighting from changing history length.
                    comparisons.append(("full", "weighted"))
                for reference, candidate in comparisons:
                    baseline = modepart.loc[modepart[arm].eq(reference)].set_index("period_start")
                    other = modepart.loc[modepart[arm].eq(candidate)].set_index("period_start")
                    for metric in (
                        ["mae_dkk", "rmse_dkk", "bias_dkk"]
                        if task == "amount"
                        else ["brier", "ece", "logloss"]
                    ):
                        joined = (
                            baseline[[metric]]
                            .join(other[[metric]], how="inner", lsuffix="_ref", rsuffix="_new")
                            .dropna()
                        )
                        if len(joined) == 0:
                            continue
                        diff = (joined[metric + "_new"] - joined[metric + "_ref"]).to_numpy()
                        contrasts.append(
                            {
                                "investigation": investigation,
                                "task": task,
                                "era": era,
                                "information": mode,
                                "candidate": candidate,
                                "reference": reference,
                                "metric": metric,
                                "reference_equal_period": float(joined[metric + "_ref"].mean()),
                                "candidate_equal_period": float(joined[metric + "_new"].mean()),
                                "intervals": [
                                    paired_interval(diff, b)
                                    for b in ([2, 4] if era == "2026" else [4, 8])
                                ],
                            }
                        )
    reused = {k: v for result in results for k, v in result["reused"].items()}
    combined = {
        "version": "e6-model-results-v1",
        "protocol": pin(root / "protocol.json"),
        "summaries": summaries,
        "contrasts": contrasts,
        "reused_fits": reused,
        "evaluations": [pin(p) for p in sorted((root / "evaluations").glob("*/result.json"))],
        "missing": [m for r in results for m in r.get("missing", [])],
        "created_utc": utc(),
    }
    atomic_json(root / "model-results.json", combined)
    atomic_json(
        root / "reuse-ledger.json", {"reused_unique_base_fits": len(reused), "fits": reused}
    )
    return combined


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["reuse", "C", "D", "aggregate"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--fold", choices=[f["name"] for f in FOLDS])
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "reuse":
        print(json.dumps(reuse_audit(root)))
    elif args.command == "aggregate":
        print(json.dumps({"comparisons": len(aggregate(root)["contrasts"])}))
    else:
        for fold in FOLDS:
            if args.fold and args.fold != fold["name"]:
                continue
            if args.command == "C" and fold["name"] == "earlier":
                continue
            amount_fold(root, fold, args.command)
            if args.command == "D":
                direction_fold(root, fold)
            print(json.dumps({"completed": args.command, "fold": fold["name"]}), flush=True)
