"""Chronological scoring and frozen development selection for E5."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from scipy.special import expit, logit

from .dataset import ResearchError
from .e2_data import attach_outcomes
from .e2_metrics import probability_metrics
from .e2_models import period_weights
from .e3_methods import amount_metrics, calibrate, intervals, temperature_fit
from .e4_common import checked, frozen_json, read, utc
from .e4_methods import mixed, route
from .e5_experiment import read_inputs
from .e5_finance import allocate, price_exposure, queue_policies, queue_summary
from .e5_protocol import FOLDS, TUNING, require
from .group_features import pin
from .training_contract import atomic_json

PROBS = ["p0", "p1", "p2"]


def align(frame: pd.DataFrame, saved: pd.DataFrame) -> pd.DataFrame:
    if saved.row_key.duplicated().any() or frame.row_key.duplicated().any():
        raise ResearchError("e5_duplicate_comparison_key")
    if not frame.row_key.isin(saved.row_key).all():
        raise ResearchError("e5_missing_prediction_keys")
    result = saved.set_index("row_key").reindex(frame.row_key).reset_index()
    if not result.row_key.equals(frame.row_key.reset_index(drop=True)):
        raise ResearchError("e5_comparison_alignment")
    return result


def json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Explicit timestamp/null representation; no NaN tokens in saved JSON."""
    import json

    return json.loads(frame.to_json(orient="records", date_format="iso", double_precision=15))


class Predictions:
    def __init__(self, root: Path, year: int) -> None:
        self.root = root
        self.year = year
        self.cache: dict[str, pd.DataFrame] = {}
        self.protocol = require(root)
        self.prior = Path(self.protocol["pins"]["e3_protocol"]["path"]).parent
        self.old_ledger = {
            x["trial_id"]: x for x in read(checked(self.protocol["pins"]["e3_ledger"]))["trials"]
        }

    def old(self, frame: pd.DataFrame, method: str) -> pd.DataFrame:
        key = f"evaluation_{self.year}-{method}"
        if key not in self.cache:
            receipt = self.old_ledger[key]
            directory = self.prior / "trials" / key
            if read(directory / "trial.json") != receipt:
                raise ResearchError("e5_old_trial_changed")
            ref = next(x for x in receipt["files"] if x["path"] == "predictions.parquet")
            self.cache[key] = pd.read_parquet(
                checked({**ref, "path": str(directory / ref["path"])})
            )
        return align(frame, self.cache[key])

    def new(self, frame: pd.DataFrame, fold: str, method: str, mode: str) -> pd.DataFrame:
        key = f"{fold}-{method}-{mode}"
        if key not in self.cache:
            receipt = read(self.root / "trials" / key / "receipt.json")
            if receipt["status"] != "completed":
                raise ResearchError("e5_incomplete_fit")
            self.cache[key] = pd.read_parquet(checked(receipt["files"]["predictions.parquet"]))
        return align(frame, self.cache[key])

    def raw(
        self, frame: pd.DataFrame, fold: str, method: str, mode: str, *, component: bool = False
    ) -> np.ndarray:
        broad = (
            self.old(frame, f"cb_broad_{mode}")[[f"p{k}_perfect" for k in range(3)]].to_numpy()
            if method == "frozen"
            else self.new(frame, fold, method, mode)[PROBS].to_numpy()
        )
        if component:
            return broad
        p = self.old(frame, f"cb_package_{mode}")[[f"p{k}_perfect" for k in range(3)]].to_numpy()
        e = self.old(frame, f"cb_equal_{mode}")[[f"p{k}_perfect" for k in range(3)]].to_numpy()
        return route(frame, p, e, broad)[0]

    def e4(self, frame: pd.DataFrame) -> pd.DataFrame:
        key = "e4"
        if key not in self.cache:
            current = read(checked(self.protocol["pins"]["e4_current"]))
            index = read(checked(current["index"]))
            evaluation = read(checked(index["evaluation"]))
            result = next(x for x in evaluation["annual"] if x["year"] == self.year)
            refs = result["artifacts"]
            values = refs.values() if isinstance(refs, dict) else refs
            ref = next(x for x in values if Path(x["path"]).name == "predictions.parquet")
            self.cache[key] = pd.read_parquet(checked(ref))
        return align(frame, self.cache[key])


def probability_score(frame: pd.DataFrame, p: np.ndarray) -> dict[str, Any]:
    mask = frame.next_status.eq("observed").to_numpy()
    known = frame.loc[mask]
    if not len(known):
        return {"candidates": len(frame), "observed": 0, "unknown": len(frame)}
    y = known.next_direction.to_numpy(dtype=int) + 1
    values = probability_metrics(y, p[mask])
    loss = ((p[mask] - np.eye(3)[y]) ** 2).sum(axis=1)
    period = (
        pd.DataFrame({"period": known.period_start.to_numpy(), "brier": loss})
        .groupby("period")
        .mean()
    )
    return {
        "candidates": len(frame),
        "observed": len(known),
        "unknown": len(frame) - len(known),
        "periods": int(frame.period_start.nunique()),
        "observed_periods": int(known.period_start.nunique()),
        "equal_period_brier": float(period.brier.mean()),
        **values,
    }


def binary_score(frame: pd.DataFrame, p: np.ndarray) -> dict[str, Any]:
    mask = frame.second_status.eq("observed").to_numpy()
    part = frame.loc[mask]
    if not len(part):
        return {"candidates": len(frame), "observed": 0}
    y = part.persistence_target.to_numpy(dtype=float)
    q = np.clip(p[mask], 1e-15, 1 - 1e-15)
    loss = (q - y) ** 2
    periods = (
        pd.DataFrame({"period": part.period_start.to_numpy(), "brier": loss})
        .groupby("period")
        .mean()
    )
    return {
        "candidates": len(frame),
        "observed": len(part),
        "unknown": len(frame) - len(part),
        "observed_periods": int(part.period_start.nunique()),
        "brier": float(loss.mean()),
        "equal_period_brier": float(periods.brier.mean()),
        "mean_probability": float(q.mean()),
        "event_rate": float(y.mean()),
        "logloss": float(-(y * np.log(q) + (1 - y) * np.log1p(-q)).mean()),
    }


def binary_temperature(frame: pd.DataFrame, probability: np.ndarray, cutoff: str) -> dict[str, Any]:
    mask = frame.second_status.eq("observed") & frame.second_maturity_date.lt(cutoff)
    part = frame.loc[mask]
    if part.empty or part.period_start.ge(cutoff).any():
        raise ResearchError("e5_binary_calibration_boundary")
    logits = logit(np.clip(probability[mask], 1e-12, 1 - 1e-12))
    y = part.persistence_target.to_numpy()
    weights = period_weights(part)
    weights /= weights.sum()

    def objective(log_t: float) -> float:
        score = logits / np.exp(log_t)
        return float(np.sum(weights * (np.logaddexp(0, score) - y * score)))

    fitted = minimize_scalar(objective, bounds=(np.log(0.5), np.log(3)), method="bounded")
    return {
        "temperature": float(np.exp(fitted.x)),
        "rows": len(part),
        "cutoff": cutoff,
        "last_maturity": str(part.second_maturity_date.max())[:10],
    }


def temperature_once(
    root: Path,
    fold: str,
    key: str,
    frame: pd.DataFrame,
    probability: np.ndarray,
    cutoff: str,
    *,
    binary: bool = False,
) -> dict[str, Any]:
    """Checkpoint each completed calibration; a publication retry never refits it."""
    directory = root / "evaluations" / fold
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "calibration-checkpoint.json"
    checkpoint = read(path) if path.exists() else {"states": {}}
    columns = (
        ["second_status", "second_maturity_date", "persistence_target"]
        if binary
        else ["next_status", "next_maturity_date", "next_direction"]
    )
    fingerprint = hashlib.sha256(
        pd.util.hash_pandas_object(frame[["row_key", "period_start", *columns]], index=False)
        .to_numpy()
        .tobytes()
        + np.asarray(probability, dtype=np.float64).tobytes()
    ).hexdigest()
    binding = {
        "input_sha256": fingerprint,
        "cutoff": cutoff,
        "binary": binary,
        "protocol": pin(root / "protocol.json"),
        "method": "bounded-temperature-v1",
    }
    if key in checkpoint["states"]:
        prior = checkpoint["states"][key]
        if prior["binding"] != binding:
            raise ResearchError("e5_calibration_retry_changed_inputs")
        return prior["state"]
    count = sum(
        len(read(p)["states"]) for p in (root / "evaluations").glob("*/calibration-checkpoint.json")
    )
    if count >= require(root)["calibration"]["maximum_temperature_fits"]:
        raise ResearchError("e5_calibration_budget_exhausted")
    if binary:
        state = binary_temperature(frame, probability, cutoff)
    else:
        data = frame.copy()
        data[PROBS] = probability
        state = temperature_fit(data, cutoff)
    checkpoint["states"][key] = {"binding": binding, "state": state, "created_utc": utc()}
    atomic_json(path, checkpoint)
    return state


def groups(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "all": np.ones(len(frame), dtype=bool),
        "price_under100": frame.aip_ore.lt(10000).to_numpy(),
        "price100_to1000": frame.aip_ore.ge(10000).to_numpy() & frame.aip_ore.lt(100000).to_numpy(),
        "price1000_plus": frame.aip_ore.ge(100000).to_numpy(),
        "history_under6": frame.history_periods.lt(6).to_numpy(),
        "history6_plus": frame.history_periods.ge(6).to_numpy(),
        "positive_peers": frame.peer_count.ge(1).fillna(False).to_numpy(dtype=bool),
        "zero_peers": frame.peer_count.eq(0).fillna(False).to_numpy(dtype=bool),
        "unsupported_peers": frame.peer_count.isna().to_numpy(),
    }


def money_score(
    frame: pd.DataFrame,
    price: np.ndarray,
    low: np.ndarray | None = None,
    high: np.ndarray | None = None,
) -> dict[str, Any]:
    data = frame.assign(next_aip_dkk=price)
    if low is not None and high is not None:
        data["lower90_dkk"], data["upper90_dkk"] = low, high
    score = amount_metrics(data)
    part = data.loc[data.next_status.eq("observed")]
    if len(part):
        error = part.next_aip_dkk.to_numpy() - part.next_aip_ore.to_numpy(dtype=float) / 100
        score.update(
            {
                "rmse_dkk": float(np.sqrt(np.mean(error**2))),
                "predicted_total_delta_dkk": float((part.next_aip_dkk - part.aip_ore / 100).sum()),
                "observed_total_delta_dkk": float((part.next_aip_ore - part.aip_ore).sum() / 100),
                "top1_squared_error_share": float(
                    np.sort(error**2)[-max(1, int(np.ceil(len(error) * 0.01))) :].sum()
                    / max(float((error**2).sum()), 1e-15)
                ),
            }
        )
    return score


def screen_candidates(root: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Resource failures cannot become a score-driven partial-pair selection."""
    decision = root / "execution-decisions.json"
    skipped = set(read(decision).get("skipped_trials", [])) if decision.exists() else set()
    available, unavailable = [], []
    for method in TUNING:
        statuses = {}
        for mode in ("current", "advance"):
            trial = f"screen-{method}-{mode}"
            path = root / "trials" / trial / "receipt.json"
            status = (
                read(path)["status"]
                if path.exists()
                else "resource_skipped"
                if trial in skipped
                else "missing"
            )
            if status not in {"completed", "failed", "resource_skipped"}:
                raise ResearchError("e5_screen_not_terminal")
            statuses[mode] = status
        if all(status == "completed" for status in statuses.values()):
            available.append(method)
        else:
            unavailable.append({"method": method, "information_status": statuses})
    return available, unavailable


def score_fold(root: Path, fold_name: str) -> dict[str, Any]:
    destination = root / "evaluations" / fold_name
    result_path = destination / "result.json"
    if result_path.exists():
        for p in read(result_path)["files"].values():
            checked(p)
        return read(result_path)
    require(root)
    fold = next(x for x in FOLDS if x["name"] == fold_name)
    features, outcomes = read_inputs(root)
    frame = features.loc[
        features.period_start.ge(fold["cal_start"]) & features.period_start.lt(fold["end"])
    ].reset_index(drop=True)
    labelled = attach_outcomes(frame, outcomes)
    evaluation = frame.period_start.ge(fold["start"]).to_numpy()
    cal_mask = frame.period_start.lt(fold["cal_end"]).to_numpy()
    test = labelled.loc[evaluation].reset_index(drop=True)
    store = Predictions(root, fold["reference_year"])
    tuning, unavailable = screen_candidates(root) if fold_name == "screen" else ([], [])
    methods = ["frozen", *tuning] if fold_name == "screen" else ["frozen", "fresh", "tuned"]
    if (
        fold_name != "screen"
        and read(root / "selection.json")["direction"]["method"] == "unchanged"
    ):
        methods.remove("tuned")
    direction, amounts, uncertainty, persistence = [], [], [], []
    states: dict[str, Any] = {}
    old = store.e4(frame)
    kept = frame.loc[evaluation].copy().reset_index(drop=True)
    kept["reference_p_rise"] = old.loc[evaluation, "perfect_hybrid_p2"].to_numpy()
    kept["reference_p_fall"] = old.loc[evaluation, "perfect_hybrid_p0"].to_numpy()
    if fold_name != "screen":
        # Preserve the accepted calibration as well as the common-calibration
        # model-freshness contrast. This reads saved predictions and fits nothing.
        for mode in ("current", "hybrid"):
            accepted = old.loc[evaluation, [f"perfect_{mode}_p{k}" for k in range(3)]].to_numpy()
            direction.append(
                {
                    "fold": fold_name,
                    "method": "accepted_e4",
                    "information": mode,
                    "population": "full_routes_calibrated",
                    **probability_score(test, accepted),
                }
            )
    probabilities: dict[str, dict[str, np.ndarray]] = {}
    for item in unavailable:
        for mode, status in item["information_status"].items():
            if status == "completed":
                partial = store.raw(frame, fold_name, item["method"], mode, component=True)
                direction.append(
                    {
                        "fold": fold_name,
                        "method": item["method"],
                        "information": mode,
                        "population": "partial_pair_component_raw_not_selectable",
                        **probability_score(test, partial[evaluation]),
                    }
                )
    for method in methods:
        raw = {mode: store.raw(frame, fold_name, method, mode) for mode in ("current", "advance")}
        if fold_name == "screen":
            for mode in ("current", "advance"):
                p = store.raw(frame, fold_name, method, mode, component=True)
                direction.append(
                    {
                        "fold": fold_name,
                        "method": method,
                        "information": mode,
                        "population": "broad_model_all_cases_raw",
                        **probability_score(test, p[evaluation]),
                    }
                )
        raw["hybrid"] = mixed(raw["advance"], raw["current"], frame.advance_abc)
        probabilities[method] = {}
        for mode in ("current", "hybrid"):
            p = raw[mode]
            key = f"{method}_{mode}"
            if fold_name == "screen" and method == "frozen":
                # Reference calibrated on disjoint 2024; tuning uses raw component scores.
                fitted = {"reused_e4": True, "cutoff": "2025-01-01"}
                cp = old[[f"perfect_{mode}_p{k}" for k in range(3)]].to_numpy()
            else:
                calibration_frame = labelled.loc[cal_mask].copy()
                calibration_frame[PROBS] = p[cal_mask]
                fitted = temperature_once(
                    root, fold_name, key, calibration_frame, p[cal_mask], fold["cal_end"]
                )
                cp = calibrate(p, fitted)
            states[key] = fitted
            probabilities[method][mode] = cp[evaluation]
            for kind, prediction in (("raw", p[evaluation]), ("calibrated", cp[evaluation])):
                direction.append(
                    {
                        "fold": fold_name,
                        "method": method,
                        "information": mode,
                        "population": "full_routes_" + kind,
                        **probability_score(test, prediction),
                    }
                )
                support = (
                    (
                        test.broad_peer_count.ge(1)
                        & test.broad_peer_feature_state.isin(
                            ["complete_same_unit_peers", "partial_same_unit_peers"]
                        )
                    )
                    .fillna(False)
                    .to_numpy(dtype=bool)
                )
                direction.append(
                    {
                        "fold": fold_name,
                        "method": method,
                        "information": mode,
                        "population": "same_broad_supported_" + kind,
                        **probability_score(
                            test.loc[support].reset_index(drop=True), prediction[support]
                        ),
                    }
                )
            for k in range(3):
                kept[f"{method}_{mode}_p{k}"] = cp[evaluation, k]
    money_methods = (
        ["frozen_mae", "rmse", "huber", "quantile"]
        if fold_name == "screen"
        else ["frozen_mae", "mae", "rmse", "quantile"]
    )
    money: dict[str, dict[str, np.ndarray]] = {}
    quantiles: dict[str, np.ndarray] = {}
    model_bundle = read(checked(store.protocol["pins"]["e4_model-bundle"]))
    residual = read(checked(model_bundle["amount_interval_calibration"]))["residual"]
    for method in money_methods:
        money[method] = {}
        lows, highs = {}, {}
        for mode in ("current", "advance"):
            if method == "frozen_mae":
                prediction = store.old(
                    frame, f"amount_delta_floor_{mode}"
                ).next_aip_dkk_perfect.to_numpy()
                bands = intervals(
                    frame.assign(next_aip_dkk=prediction),
                    residual[f"{fold['reference_year']}_{mode}"],
                )
                lows[mode], highs[mode] = bands.lower90_dkk.to_numpy(), bands.upper90_dkk.to_numpy()
            else:
                saved = store.new(frame, fold_name, method, mode)
                if method == "quantile":
                    prediction = saved.q50_price_dkk.to_numpy()
                    lows[mode], highs[mode] = (
                        saved.q05_price_dkk.to_numpy(),
                        saved.q95_price_dkk.to_numpy(),
                    )
                    uncertainty.append(
                        {
                            "fold": fold_name,
                            "method": method,
                            "information": mode,
                            "population": "all_crossings",
                            "candidates": int(evaluation.sum()),
                            "crossings": int(saved.loc[evaluation, "crossed"].sum()),
                        }
                    )
                else:
                    prediction = saved.next_price_dkk.to_numpy()
            money[method][mode] = prediction
        usable = frame.advance_abc.isin(["A", "B", "C"]).to_numpy()
        money[method]["hybrid"] = np.where(
            usable, money[method]["advance"], money[method]["current"]
        )
        if lows:
            lows["hybrid"] = np.where(usable, lows["advance"], lows["current"])
            highs["hybrid"] = np.where(usable, highs["advance"], highs["current"])
        for mode in ("current", "advance", "hybrid"):
            price = money[method][mode][evaluation]
            for group, mask in groups(test).items():
                amounts.append(
                    {
                        "fold": fold_name,
                        "method": method,
                        "information": mode,
                        "population": group,
                        **money_score(test.loc[mask].reset_index(drop=True), price[mask]),
                    }
                )
                if lows:
                    uncertainty.append(
                        {
                            "fold": fold_name,
                            "method": method,
                            "information": mode,
                            "population": group,
                            **money_score(
                                test.loc[mask].reset_index(drop=True),
                                price[mask],
                                lows[mode][evaluation][mask],
                                highs[mode][evaluation][mask],
                            ),
                        }
                    )
            if mode == "hybrid":
                kept[f"{method}_price_dkk"] = price
                if lows:
                    kept[f"{method}_lower_price_dkk"] = lows[mode][evaluation]
                    kept[f"{method}_upper_price_dkk"] = highs[mode][evaluation]
            if method == "quantile":
                quantiles[mode] = np.column_stack([lows[mode][evaluation], highs[mode][evaluation]])
    for mode in ("current", "advance"):
        persistence_raw = store.new(
            frame, fold_name, "persistence", mode
        ).persistence_probability.to_numpy()
        state = temperature_once(
            root,
            fold_name,
            f"persistence_{mode}",
            labelled.loc[cal_mask],
            persistence_raw[cal_mask],
            fold["cal_end"],
            binary=True,
        )
        states[f"persistence_{mode}"] = state
        cp = expit(logit(np.clip(persistence_raw, 1e-12, 1 - 1e-12)) / state["temperature"])
        persistence.append(
            {
                "fold": fold_name,
                "method": "joint_persistence",
                "information": mode,
                "population": "all",
                **binary_score(test, cp[evaluation]),
            }
        )
        kept[f"persistence_{mode}"] = cp[evaluation]
    kept["persistence_probability"] = np.where(
        kept.advance_abc.isin(["A", "B", "C"]), kept.persistence_advance, kept.persistence_current
    )
    persistence.append(
        {
            "fold": fold_name,
            "method": "joint_persistence",
            "information": "hybrid",
            "population": "all",
            **binary_score(test, kept.persistence_probability.to_numpy()),
        }
    )
    persistence.append(
        {
            "fold": fold_name,
            "method": "rise_probability_proxy",
            "information": "hybrid",
            "population": "all",
            **binary_score(test, probabilities["frozen"]["hybrid"][:, 2]),
        }
    )
    amounts.append(
        {
            "fold": fold_name,
            "method": "no_change",
            "information": "current",
            "population": "all",
            **money_score(test, test.aip_ore.to_numpy() / 100),
        }
    )
    destination.mkdir(parents=True, exist_ok=True)
    kept.to_parquet(destination / "predictions.parquet", index=False)
    test.to_parquet(destination / "outcomes.parquet", index=False)
    frozen_json(destination / "calibration.json", states)
    result = {
        "version": "e5-fold-results-v1",
        "fold": fold_name,
        "created_utc": utc(),
        "protocol": pin(root / "protocol.json"),
        "direction": direction,
        "amount": amounts,
        "intervals": uncertainty,
        "persistence": persistence,
        "unavailable_tuning": unavailable,
        "files": {
            x: pin(destination / x)
            for x in ("predictions.parquet", "outcomes.parquet", "calibration.json")
        },
    }
    frozen_json(result_path, result)
    return result


def finance_frame(predictions: pd.DataFrame, method: str) -> pd.DataFrame:
    keep = [
        x
        for x in predictions
        if x
        in [
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
            "history_periods",
            "abc",
            "advance_abc",
            "package_distributor",
            "reference_p_rise",
            "reference_p_fall",
            "persistence_probability",
        ]
    ]
    out = predictions[keep].copy()
    out["package_id"] = out.package_number
    out["p_rise"] = predictions[f"{method}_hybrid_p2"]
    out["p_fall"] = predictions[f"{method}_hybrid_p0"]
    out["forecast_delta_dkk"] = predictions.rmse_price_dkk - out.aip_ore / 100
    median = "mae_price_dkk" if "mae_price_dkk" in predictions else "frozen_mae_price_dkk"
    out["median_delta_dkk"] = predictions[median] - out.aip_ore / 100
    out["lower_delta_dkk"] = predictions.quantile_lower_price_dkk - out.aip_ore / 100
    out["upper_delta_dkk"] = predictions.quantile_upper_price_dkk - out.aip_ore / 100
    out["forecast_percent"] = 100 * out.forecast_delta_dkk / (out.aip_ore / 100)
    return out


def select(root: Path) -> dict[str, Any]:
    path = root / "selection.json"
    if path.exists():
        return read(path)
    result = score_fold(root, "screen")
    scores = {}
    tuning, unavailable = screen_candidates(root)
    for method in ["frozen", *tuning]:
        selected = [
            x["equal_period_brier"]
            for x in result["direction"]
            if x["method"] == method and x["population"] == "broad_model_all_cases_raw"
        ]
        scores[method] = float(np.mean(selected))
    winner = min(tuning, key=lambda x: scores[x]) if tuning else "unchanged"
    if winner != "unchanged" and scores[winner] > scores["frozen"] - 0.0001:
        winner = "unchanged"
    trees = {
        mode: int(read(root / "trials" / f"screen-{winner}-{mode}" / "receipt.json")["trees"])
        if winner != "unchanged"
        else 160
        for mode in ("current", "advance")
    }
    p = pd.read_parquet(checked(result["files"]["predictions.parquet"]))
    o = pd.read_parquet(checked(result["files"]["outcomes.parquet"]))
    f = finance_frame(p, winner if winner != "unchanged" else "frozen")
    q = queue_policies(f, include_persistence=True)
    exposure = price_exposure(
        allocate(q, mode="per_pack"),
        o.assign(package_id=o.package_number),
        queues=q,
        allocation="per_pack",
    )
    # Compare aggregate downside at the identical matched workload, not a smaller successful subset.
    cols = exposure.columns.tolist()
    frozen_json(
        root / "screen-finance.json",
        {
            "columns": cols,
            "queues": json_records(queue_summary(q)),
            "exposure": json_records(exposure),
        },
    )
    binary = {
        x["method"]: x["equal_period_brier"]
        for x in result["persistence"]
        if x["information"] == "hybrid"
    }
    # Missing exact workload support is a valid failed persistence-policy result.
    selected_counts = q.loc[q.workload.eq("matched")].groupby("policy").selected.sum()
    comparable = selected_counts.get("persistence_aware", -1) == selected_counts.get(
        "downside_aware", -2
    )
    loss = {}
    for name in ("downside_aware", "persistence_aware"):
        selected_keys = q.loc[q.workload.eq("matched") & q.policy.eq(name) & q.selected, "row_key"]
        known = o.loc[o.row_key.isin(selected_keys) & o.next_status.eq("observed")]
        loss[name] = (
            float(np.maximum(0, -known.next_delta_ore.to_numpy(dtype=float) / 100).mean())
            if len(known)
            else float("inf")
        )
    enabled = (
        binary["joint_persistence"] < binary["rise_probability_proxy"]
        and comparable
        and loss["persistence_aware"] <= loss["downside_aware"]
    )
    choice = {
        "version": "e5-development-selection-v1",
        "created_utc": utc(),
        "protocol": pin(root / "protocol.json"),
        "direction": {
            "method": winner,
            "iterations": trees,
            "development_scores": scores,
            "unavailable_candidates": unavailable,
            "criterion": (
                "Mean current/advance equal-period raw broad-model Brier, all "
                "identical eligible cases; minimum .0001 gain"
            ),
        },
        "amount": "RMSE mean-oriented ranking candidate; Huber reported only, MAE median retained",
        "persistence_policy_enabled": bool(enabled),
        "persistence_selection": {
            "brier": binary,
            "matched_counts_equal": bool(comparable),
            "downside_dkk_per_observed_pack": {
                k: v if np.isfinite(v) else None for k, v in loss.items()
            },
        },
        "outer_scores_inspected": False,
        "research_only": True,
    }
    frozen_json(path, choice)
    return choice


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["select", "screen", "spring", "summer"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(
        select(args.root.resolve())
        if args.command == "select"
        else score_fold(args.root.resolve(), args.command)["fold"]
    )
