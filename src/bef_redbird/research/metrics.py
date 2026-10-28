"""Proper probability scores and temporal-block uncertainty, with explicit counts."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .features import FloatArray, Target


def score_next(
    targets: list[Target], predictions: dict[str, FloatArray], identifiers: list[str]
) -> tuple[dict[str, float], dict[str, int], list[dict[str, Any]]]:
    observed = [index for index, target in enumerate(targets) if target.state == "observed"]
    if not observed:
        return {}, {}, []
    directions = np.array([targets[index].direction for index in observed], dtype=int)
    probabilities = predictions["probabilities"][observed]
    actual = np.stack([directions == value for value in (-1, 0, 1)], axis=1)
    magnitude = np.array([targets[index].log_return for index in observed], dtype=float)
    price = np.array([targets[index].price_dkk for index in observed], dtype=float)
    top_count = max(1, math.ceil(len(observed) / 10))
    ranked = sorted(
        range(len(observed)),
        key=lambda index: (-float(probabilities[index, 2]), identifiers[observed[index]]),
    )[:top_count]
    scores = {
        "brier_multiclass": float(np.square(probabilities - actual).sum(axis=1).mean()),
        "direction_accuracy": float((predictions["direction"][observed] == directions).mean()),
        "mae_log_return": float(np.abs(predictions["log_return"][observed] - magnitude).mean()),
        "mae_price_dkk": float(np.abs(predictions["price_dkk"][observed] - price).mean()),
        "precision_top_decile_rise": float((directions[ranked] == 1).mean()),
    }
    denominators = {key: len(observed) for key in scores}
    denominators["precision_top_decile_rise"] = top_count
    calibration = [
        item
        for index, label in enumerate((-1, 0, 1))
        for item in calibration_bins(probabilities[:, index], actual[:, index], str(label))
    ]
    return scores, denominators, calibration


def score_reversal(
    targets: list[Target], probability: FloatArray
) -> tuple[dict[str, float], dict[str, int], list[dict[str, Any]]]:
    observed = [index for index, target in enumerate(targets) if target.state == "observed"]
    if not observed:
        return {}, {}, []
    actual = np.array([targets[index].full_reversal for index in observed], dtype=float)
    selected = probability[observed]
    scores = {
        "brier_binary": float(np.square(selected - actual).mean()),
        "reversal_accuracy": float(((selected >= 0.5) == actual).mean()),
    }
    return (
        scores,
        {key: len(observed) for key in scores},
        calibration_bins(selected, actual, "full_reversal"),
    )


def calibration_bins(
    probability: FloatArray, actual: FloatArray, label: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    bins = np.minimum((probability * 10).astype(int), 9)
    for index in range(10):
        mask = bins == index
        count = int(mask.sum())
        result.append(
            {
                "class_label": label,
                "bin_lower": index / 10,
                "bin_upper": (index + 1) / 10,
                "n": count,
                "mean_probability": float(probability[mask].mean()) if count else None,
                "observed_frequency": float(actual[mask].mean()) if count else None,
            }
        )
    return result


def summarize(
    per_period: list[dict[str, Any]],
    calibration: list[dict[str, Any]],
    *,
    seed: int,
    replicates: int,
    block_length: int,
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in per_period:
        key = (record["task"], record["split"], record["method"])
        groups.setdefault(key, []).append(record)
    aggregate: list[dict[str, Any]] = []
    uncertainty: list[dict[str, Any]] = []
    for (task, split, method), records in sorted(groups.items()):
        base = {"task": task, "split": split, "method": method}
        names = sorted({name for record in records for name in record["metrics"]})
        metrics: dict[str, float] = {}
        denominators: dict[str, int] = {}
        for name in names:
            weights = np.array([record["metric_denominators"].get(name, 0) for record in records])
            values = np.array([record["metrics"].get(name, 0) for record in records])
            denominator = int(weights.sum())
            denominators[name] = denominator
            metrics[name] = float(np.sum(values * weights) / denominator)
            # Resample consecutive tariff-period blocks; never package rows.
            # Include zero-label periods, preserving the actual calendar sequence.
            generator = np.random.default_rng(seed)
            samples: list[float] = []
            length = len(records)
            for _ in range(replicates):
                starts = generator.integers(0, length, size=math.ceil(length / block_length))
                indices = np.concatenate(
                    [(np.arange(block_length) + start) % length for start in starts]
                )[:length]
                total = int(weights[indices].sum())
                if total:
                    samples.append(float(np.sum(values[indices] * weights[indices]) / total))
            uncertainty.append(
                {
                    **base,
                    "metric": name,
                    "n_periods": length,
                    "n_observed_periods": int((weights > 0).sum()),
                    "lower": float(np.quantile(samples, 0.025))
                    if samples and int((weights > 0).sum()) >= 2
                    else None,
                    "upper": float(np.quantile(samples, 0.975))
                    if samples and int((weights > 0).sum()) >= 2
                    else None,
                    "seed": seed,
                    "replicates": replicates,
                    "usable_replicates": len(samples),
                    "block_length": block_length,
                    "procedure": (
                        "circular_moving_period_block_bootstrap;row_weighted_metric;"
                        "conditional_on_fitted_predictions;not_independent_package_intervals"
                    ),
                }
            )
        aggregate.append(
            {
                **base,
                "n": sum(record["n"] for record in records),
                "metrics": metrics,
                "metric_denominators": denominators,
                "official_comparison": split in {"validation", "final_evaluation"},
            }
        )
    calibration_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in calibration:
        key = tuple(
            record[name]
            for name in ("task", "split", "method", "class_label", "bin_lower", "bin_upper")
        )
        calibration_groups.setdefault(key, []).append(record)
    calibration_aggregate = []
    for key, records in sorted(calibration_groups.items()):
        count = sum(record["n"] for record in records)
        calibration_aggregate.append(
            {
                **dict(
                    zip(
                        ("task", "split", "method", "class_label", "bin_lower", "bin_upper"),
                        key,
                        strict=True,
                    )
                ),
                "n": count,
                "mean_probability": sum(
                    record["mean_probability"] * record["n"] for record in records if record["n"]
                )
                / count
                if count
                else None,
                "observed_frequency": sum(
                    record["observed_frequency"] * record["n"] for record in records if record["n"]
                )
                / count
                if count
                else None,
            }
        )
    return {
        "aggregate": aggregate,
        "per_period": per_period,
        "calibration": calibration_aggregate,
        "calibration_per_period": calibration,
        "uncertainty": uncertainty,
    }
