"""Prespecified E3 monetary constraints, calibration, routing and review policies."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from scipy.special import softmax

from .dataset import ResearchError
from .e2_metrics import paired_interval, probability_metrics
from .e2_models import period_weights

SEED = 20261026
LAMBDAS = [0.25, 0.5, 1.0]
THRESHOLDS = [(rise, fall) for rise in (0.15, 0.25, 0.35) for fall in (0.10, 0.20)]


def monetary_target(frame: pd.DataFrame, formulation: str) -> tuple[np.ndarray, dict[str, Any]]:
    current = frame.aip_ore.to_numpy(dtype=float) / 100
    future = frame.next_aip_ore.to_numpy(dtype=float) / 100
    if formulation not in {"delta_floor", "log_ratio"}:
        raise ResearchError("e3_unknown_monetary_formulation")
    if not (
        np.isfinite(current).all()
        and np.isfinite(future).all()
        and (current > 0).all()
        and (future > 0).all()
    ):
        raise ResearchError("e3_unusable_price_label")
    if formulation == "delta_floor":
        return future - current, {
            "formulation": formulation,
            "constraint": "max(0,current+raw_delta), fixed before evaluation",
        }
    values = np.log(future / current)
    return values, {
        "formulation": formulation,
        "minimum_log_ratio": float(values.min()),
        "maximum_log_ratio": float(values.max()),
        "constraint": "exp(log-ratio bounded by training support), no evaluated-tail deletion",
    }


def next_price(raw: np.ndarray, current: np.ndarray, state: dict[str, Any]) -> np.ndarray:
    if not np.isfinite(raw).all() or not np.isfinite(current).all() or (current <= 0).any():
        raise ResearchError("e3_invalid_raw_price_prediction")
    if state["formulation"] not in {"delta_floor", "log_ratio"}:
        raise ResearchError("e3_unknown_monetary_formulation")
    if state["formulation"] == "delta_floor":
        out = np.maximum(0, current + np.asarray(raw).reshape(-1))
    else:
        out = current * np.exp(
            np.clip(
                np.asarray(raw).reshape(-1), state["minimum_log_ratio"], state["maximum_log_ratio"]
            )
        )
    if not np.isfinite(out).all() or (out < 0).any():
        raise ResearchError("e3_invalid_next_price")
    return out


def temperature_fit(frame: pd.DataFrame, cutoff: str) -> dict[str, Any]:
    known = frame.loc[
        (frame.next_status == "observed") & (frame.next_maturity_date < pd.Timestamp(cutoff))
    ]
    if known.empty or (known.period_start >= pd.Timestamp(cutoff)).any():
        raise ResearchError("e3_calibration_not_chronological")
    logits = np.log(np.maximum(known[["p0", "p1", "p2"]].to_numpy(), 1e-15))
    y = known.next_direction.to_numpy(dtype=int) + 1
    weights = period_weights(known)
    weights /= weights.sum()

    def objective(log_temperature: float) -> float:
        p = softmax(logits / np.exp(log_temperature), axis=1)
        return float(np.sum(weights * -np.log(np.maximum(p[np.arange(len(y)), y], 1e-15))))

    fitted = minimize_scalar(
        objective, bounds=(math.log(0.5), math.log(3.0)), method="bounded", options={"xatol": 1e-5}
    )
    return {
        "temperature": float(np.exp(fitted.x)),
        "cutoff": cutoff,
        "rows": len(known),
        "last_origin": str(known.period_start.max())[:10],
        "last_maturity": str(known.next_maturity_date.max())[:10],
        "objective": float(fitted.fun),
        "converged": bool(fitted.success),
    }


def calibrate(probability: np.ndarray, state: dict[str, Any]) -> np.ndarray:
    return softmax(np.log(np.maximum(probability, 1e-15)) / state["temperature"], axis=1)


def residual_fit(frame: pd.DataFrame, cutoff: str) -> dict[str, Any]:
    known = frame.loc[
        (frame.next_status == "observed") & (frame.next_maturity_date < pd.Timestamp(cutoff))
    ]
    if known.empty or (known.period_start >= pd.Timestamp(cutoff)).any():
        raise ResearchError("e3_residual_calibration_not_chronological")
    error = np.abs(known.next_aip_dkk.to_numpy() - known.next_aip_ore.to_numpy(dtype=float) / 100)
    # Fixed current-price strata. Empirical marginal intervals, not a guarantee.
    bins = np.digitize(known.aip_ore.to_numpy(dtype=float) / 100, [100, 1000])
    radii = {}
    for k in range(3):
        selected = error[bins == k]
        radii[str(k)] = float(
            np.quantile(selected if len(selected) >= 100 else error, 0.90, method="higher")
        )
    return {
        "radii_dkk": radii,
        "rows": len(known),
        "cutoff": cutoff,
        "last_maturity": str(known.next_maturity_date.max())[:10],
        "nominal": 0.90,
        "method": (
            "earlier held-out absolute residual q90 in fixed current-AIP bins "
            "<100,100-1000,>=1000; empirical coverage only"
        ),
    }


def intervals(frame: pd.DataFrame, state: dict[str, Any]) -> pd.DataFrame:
    out = frame.copy()
    bins = np.digitize(out.aip_ore.to_numpy(dtype=float) / 100, [100, 1000])
    radius = np.array([state["radii_dkk"][str(k)] for k in bins])
    out["lower90_dkk"] = np.maximum(0, out.next_aip_dkk - radius)
    out["upper90_dkk"] = out.next_aip_dkk + radius
    return out


def fallback(package: pd.DataFrame, peers: pd.DataFrame, support: np.ndarray) -> pd.DataFrame:
    key_columns = [
        x
        for x in (
            "row_key",
            "package_number",
            "period_start",
            "period_index",
            "identity_episode_id",
        )
        if x in package or x in peers
    ]
    if any(
        x not in package or x not in peers or not package[x].equals(peers[x]) for x in key_columns
    ) or len(support) != len(package):
        raise ResearchError("e3_fallback_cohort_changed")
    out = peers.copy()
    cols = ["p0", "p1", "p2"]
    out.loc[~support, cols] = package.loc[~support, cols].to_numpy()
    out["route"] = np.where(support, "peer_model", "package_fallback")
    return out


def select_queue(
    frame: pd.DataFrame,
    policy: str,
    parameter: Any = None,
    *,
    workload: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Uses only keys and predictions; unknown later outcomes cannot change membership."""
    out = frame.copy().reset_index(drop=True)
    out["tie_key"] = [hashlib.sha256(f"{SEED}:{k}".encode()).hexdigest() for k in out.row_key]
    score = out.p2 - float(parameter) * out.p0 if policy == "risk_rank" else out.p2
    out["queue_score"] = score
    out["selected"] = False
    out["rank"] = 0
    for day, part in out.groupby("period_start", sort=True):
        ordered = part.sort_values(["queue_score", "tie_key"], ascending=[False, True])
        out.loc[ordered.index, "rank"] = np.arange(1, len(ordered) + 1)
        if workload is not None:
            n = workload[str(day)[:10]]
            if type(n) is not int or not 0 <= n <= len(part):
                raise ResearchError("e3_invalid_matched_workload")
            chosen = ordered.index[:n]
        elif policy == "threshold":
            rise, fall = parameter
            chosen = ordered.index[(ordered.p2 >= rise) & (ordered.p0 <= fall)]
            chosen = chosen[: math.ceil(len(part) * 0.1)]
        else:
            chosen = ordered.index[: math.ceil(len(part) * 0.1)]
        out.loc[chosen, "selected"] = True
    out["policy"] = policy
    out["selection_reason"] = np.where(
        out.selected, f"{policy}; fixed validation parameter={parameter}", "outside frozen policy"
    )
    return out


def queue_periods(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for day, part in frame.groupby("period_start", sort=True):
        selected = part.loc[part.selected]
        observed = selected.loc[selected.next_status == "observed"]
        rows.append(
            {
                "period_start": str(day)[:10],
                "candidates": len(part),
                "selected": len(selected),
                "selection_rate": len(selected) / len(part),
                "observed": len(observed),
                "empty": not len(selected),
                "rise_rate": float(observed.next_direction.eq(1).mean()) if len(observed) else None,
                "fall_rate": float(observed.next_direction.eq(-1).mean())
                if len(observed)
                else None,
                "unknown_fraction": 1 - len(observed) / len(selected) if len(selected) else None,
                "rises": int(observed.next_direction.eq(1).sum()),
                "falls": int(observed.next_direction.eq(-1).sum()),
            }
        )
    return rows


def queue_summary(frame: pd.DataFrame) -> dict[str, Any]:
    periods = queue_periods(frame)
    result = {
        "candidates": len(frame),
        "selected": int(frame.selected.sum()),
        "selection_rate": float(frame.selected.mean()),
        "empty_periods": sum(x["empty"] for x in periods),
        "periods": len(periods),
        "observed": sum(x["observed"] for x in periods),
        "rises": sum(x["rises"] for x in periods),
        "falls": sum(x["falls"] for x in periods),
    }
    for key in ("rise_rate", "fall_rate", "unknown_fraction"):
        values = np.array([x[key] for x in periods if x[key] is not None])
        result[key] = float(values.mean()) if len(values) else None
        if len(values) > 1:
            ci = paired_interval(values, seed=SEED)
            result[key + "_lower"], result[key + "_upper"] = ci["lower"], ci["upper"]
    return result


def select_policies(validation: pd.DataFrame) -> dict[str, Any]:
    # Selection uses 2023 only; 2022 is held out for calibration.
    data = validation.loc[
        (validation.period_start >= pd.Timestamp("2023-01-01"))
        & (validation.period_start < pd.Timestamp("2024-01-01"))
    ]
    choices = []
    policy_grid: list[tuple[str, list[Any]]] = [("risk_rank", LAMBDAS), ("threshold", THRESHOLDS)]
    for kind, parameters in policy_grid:
        scored = []
        for parameter in parameters:
            summary = queue_summary(select_queue(data, kind, parameter))
            # Freeze useful coverage before results: >=2% overall and >=1% in 80% periods.
            per = queue_periods(select_queue(data, kind, parameter))
            useful = (
                summary["selection_rate"] >= 0.02
                and np.mean([x["selection_rate"] >= 0.01 for x in per]) >= 0.8
            )
            summary.update({"policy": kind, "parameter": parameter, "useful": bool(useful)})
            scored.append(summary)
        feasible = [x for x in scored if x["useful"]]
        # Fixed risk criterion: rises minus falls among observed selected, not profit.
        best = (
            max(feasible, key=lambda x: (x["rise_rate"] - x["fall_rate"], x["selection_rate"]))
            if feasible
            else None
        )
        choices.append(
            {
                "policy": kind,
                "selected_parameter": best["parameter"] if best else None,
                "status": "selected" if best else "no_useful_validation_policy",
                "grid": scored,
            }
        )
    return {
        "choices": choices,
        "selection_origins": "2023",
        "minimum_coverage": "2% overall; >=1% in >=80% validation periods; max10% per period",
        "criterion": (
            "maximize observed rise fraction minus fall fraction; neither expected gain nor profit"
        ),
    }


def amount_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    known = frame.loc[frame.next_status == "observed"]
    current = known.aip_ore.to_numpy(dtype=float) / 100
    actual = known.next_aip_ore.to_numpy(dtype=float) / 100
    prediction = known.next_aip_dkk.to_numpy(dtype=float)
    error = prediction - actual
    absolute = np.abs(error)
    result: dict[str, Any] = {
        "candidates": len(frame),
        "observed": len(known),
        "invalid": int((~np.isfinite(frame.next_aip_dkk) | (frame.next_aip_dkk < 0)).sum()),
    }
    if not len(known):
        return result
    result.update(
        {
            "mae_dkk": float(absolute.mean()),
            "bias_dkk": float(error.mean()),
            "relative_current_mae": float((absolute / current).mean()),
            "relative_actual_mae": float((absolute / actual).mean()),
            "p95_dkk": float(np.quantile(absolute, 0.95)),
            "p99_dkk": float(np.quantile(absolute, 0.99)),
            "max_error_dkk": float(absolute.max()),
            "top1_error_share": float(
                np.sort(absolute)[-max(1, math.ceil(len(known) * 0.01)) :].sum()
                / max(absolute.sum(), 1e-15)
            ),
        }
    )
    if "lower90_dkk" in known:
        result.update(
            {
                "coverage90": float(
                    ((actual >= known.lower90_dkk) & (actual <= known.upper90_dkk)).mean()
                ),
                "width90_dkk": float((known.upper90_dkk - known.lower90_dkk).mean()),
            }
        )
    return result


def direction_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    known = frame.loc[frame.next_status == "observed"]
    return {
        "candidates": len(frame),
        "observed": len(known),
        **probability_metrics(
            known.next_direction.to_numpy(dtype=int) + 1,
            known[["p0", "p1", "p2"]].to_numpy(dtype=float),
        ),
    }
