"""Decision-first rankings and paired temporal evaluation for E2."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .training_contract import KEYS


def freeze_predictions(
    candidates: pd.DataFrame, predictions: np.ndarray, *, task: str, seed: int
) -> pd.DataFrame:
    columns = list(KEYS) + ["aip_ore", "history_periods", "lag1_delta_ore"]
    columns += [str(x) for x in candidates if str(x).startswith(("peer_", "group_feature"))]
    result = candidates[list(dict.fromkeys(columns))].copy().reset_index(drop=True)
    if len(predictions) != len(result):
        raise ResearchError("e2_prediction_length_changed")
    if task == "magnitude":
        values = np.asarray(predictions)
        if values.shape not in {(len(result),), (len(result), 1)} or not np.isfinite(values).all():
            raise ResearchError("e2_invalid_magnitude_predictions")
        result["prediction_dkk"] = values.reshape(-1)
    else:
        probabilities = np.asarray(predictions)
        if (
            probabilities.shape != (len(result), 3)
            or not np.isfinite(probabilities).all()
            or (probabilities < 0).any()
            or not np.allclose(probabilities.sum(axis=1), 1, atol=1e-8)
        ):
            raise ResearchError("e2_invalid_probabilities")
        for index in range(3):
            result[f"p{index}"] = probabilities[:, index]
        # A seeded hash is a declared random tie policy, not numeric package-ID order.
        result["tie_key"] = [
            hashlib.sha256(f"{seed}:{key}".encode()).hexdigest() for key in result.row_key
        ]
        ordered = result.sort_values(
            ["period_start", "p2", "tie_key"], ascending=[True, False, True]
        )
        ranked = ordered.groupby("period_start", sort=False).cumcount() + 1
        result["rank"] = ranked.reindex(result.index)
        count = result.groupby("period_start")["row_key"].transform("size")
        result["selected"] = result["rank"] <= np.ceil(count * 0.1)
    return result


def probability_metrics(y: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    if not len(y):
        return {}
    onehot = np.eye(3)[y]
    brier = float(np.mean(np.sum((probabilities - onehot) ** 2, axis=1)))
    ece = 0.0
    for cls in range(3):
        bins = np.minimum((probabilities[:, cls] * 10).astype(int), 9)
        for index in range(10):
            selected = bins == index
            if selected.any():
                ece += (
                    float(
                        selected.mean()
                        * abs(probabilities[selected, cls].mean() - (y[selected] == cls).mean())
                    )
                    / 3
                )
    return {
        "brier": brier,
        "accuracy": float((probabilities.argmax(axis=1) == y).mean()),
        "ece": ece,
    }


def score_period(frame: pd.DataFrame, task: str) -> dict[str, Any]:
    if frame.empty:
        raise ResearchError("e2_empty_prediction_cohort")
    target = "reversal" if task == "reversal" else "next"
    observed = frame[target + "_status"] == "observed"
    known = frame.loc[observed]
    metrics: dict[str, Any] = {
        "outcome_coverage": float(observed.mean()),
        "unknown_fraction": float(1 - observed.mean()),
    }
    if task == "magnitude":
        if len(known):
            actual = known.next_delta_ore.to_numpy(dtype=float) / 100
            prediction = known.prediction_dkk.to_numpy(dtype=float)
            error = np.abs(prediction - actual)
            metrics.update(
                {
                    "mae_dkk": float(error.mean()),
                    "median_absolute_error_dkk": float(np.median(error)),
                    "p95_absolute_error_dkk": float(np.quantile(error, 0.95)),
                    "rmse_dkk": float(np.sqrt(np.mean(error**2))),
                    "relative_to_current_aip_mae": float(
                        np.mean(error / (known.aip_ore.to_numpy(dtype=float) / 100))
                    ),
                    "large_error_share": float(
                        np.sum(np.sort(error)[-max(1, math.ceil(len(error) * 0.01)) :])
                        / error.sum()
                    )
                    if error.sum()
                    else 0.0,
                }
            )
        return {"n": len(known), "candidates": len(frame), "metrics": metrics}
    mapping = {"full_reversal": 0, "partial_reversal": 1, "persistence": 2}
    y = (
        known.reversal_category.map(mapping).to_numpy(dtype=int)
        if task == "reversal"
        else known.next_direction.to_numpy(dtype=int) + 1
    )
    metrics.update(probability_metrics(y, known[["p0", "p1", "p2"]].to_numpy(dtype=float)))
    selected = frame["selected"].astype(bool)
    selected_known = frame.loc[selected & observed]
    unknown_n = int((selected & ~observed).sum())
    metrics["shortlist_unknown_fraction"] = unknown_n / max(1, int(selected.sum()))
    metrics["shortlist_observed"] = len(selected_known)
    metrics["shortlist_selected"] = int(selected.sum())
    if task == "direction":
        rises = int((selected_known.next_direction == 1).sum())
        metrics.update(
            {
                "rise_precision_top_decile": rises / len(selected_known)
                if len(selected_known)
                else None,
                "fall_fraction_top_decile": float((selected_known.next_direction == -1).mean())
                if len(selected_known)
                else None,
                "shortlist_rise_lower_bound": rises / max(1, int(selected.sum())),
                "shortlist_rise_upper_bound": (rises + unknown_n) / max(1, int(selected.sum())),
                "random_selection_rise_expectation": float((known.next_direction == 1).mean())
                if len(known)
                else None,
                "random_selection_unknown_expectation": float(1 - observed.mean()),
                "unknown_after_current_rise": float((~observed[frame.lag1_delta_ore > 0]).mean())
                if (frame.lag1_delta_ore > 0).any()
                else None,
            }
        )
    return {"n": len(known), "candidates": len(frame), "metrics": metrics}


def score_frame(
    frame: pd.DataFrame, task: str, split: str, method: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    periods = []
    for day, part in frame.groupby("period_start", sort=True):
        periods.append(
            {
                "task": task,
                "split": split,
                "method": method,
                "period_start": str(day)[:10],
                **score_period(part, task),
            }
        )
    pooled = score_period(frame, task)
    names = {k for row in periods for k in row["metrics"]}
    macro = {
        k: float(
            np.mean([row["metrics"][k] for row in periods if row["metrics"].get(k) is not None])
        )
        for k in names
        if any(row["metrics"].get(k) is not None for row in periods)
    }
    for name in ("shortlist_observed", "shortlist_selected"):
        if name in macro:
            macro[name + "_total"] = sum(row["metrics"].get(name, 0) for row in periods)
            del macro[name]
    if task == "magnitude":
        macro["package_weighted_mae_dkk"] = pooled["metrics"].get("mae_dkk")
    else:
        macro["package_weighted_brier"] = pooled["metrics"].get("brier")
    aggregate = {
        "task": task,
        "split": split,
        "method": method,
        "n": pooled["n"],
        "candidates": len(frame),
        "periods": len(periods),
        "scored_periods": sum(bool(row["n"]) for row in periods),
        "metrics": macro,
    }
    return aggregate, periods


def paired_interval(
    differences: np.ndarray, *, seed: int, block: int = 4, repetitions: int = 2000
) -> dict[str, Any]:
    values = np.asarray(differences, dtype=float)
    if len(values) < 2 or not np.isfinite(values).all():
        raise ResearchError("e2_insufficient_paired_periods")
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(values), size=(repetitions, math.ceil(len(values) / block)))
    indices = (starts[:, :, None] + np.arange(block)) % len(values)
    means = values[indices.reshape(repetitions, -1)[:, : len(values)]].mean(axis=1)
    return {
        "mean_difference": float(values.mean()),
        "lower": float(np.quantile(means, 0.025)),
        "upper": float(np.quantile(means, 0.975)),
        "periods": len(values),
        "block_length": block,
        "replications": repetitions,
        "seed": seed,
        "interval_method": "paired circular temporal block bootstrap",
    }


def calibration_rows(
    frame: pd.DataFrame, task: str, split: str, method: str
) -> list[dict[str, Any]]:
    target = "reversal" if task == "reversal" else "next"
    known = frame.loc[frame[target + "_status"] == "observed"]
    y = (
        known.reversal_category.map(
            {"full_reversal": 0, "partial_reversal": 1, "persistence": 2}
        ).to_numpy(dtype=int)
        if task == "reversal"
        else known.next_direction.to_numpy(dtype=int) + 1
    )
    result = []
    for cls in range(3):
        probabilities = known[f"p{cls}"].to_numpy(dtype=float)
        bins = np.minimum((probabilities * 10).astype(int), 9)
        for index in range(10):
            mask = bins == index
            if mask.any():
                result.append(
                    {
                        "task": task,
                        "split": split,
                        "method": method,
                        "class": cls,
                        "bin": index,
                        "n": int(mask.sum()),
                        "predicted": float(probabilities[mask].mean()),
                        "observed": float((y[mask] == cls).mean()),
                    }
                )
    return result
