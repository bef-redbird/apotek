"""Fixed ridge fits from sufficient statistics, with mature-label-only transforms."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .features import FEATURE_NAMES, FloatArray, Target

METHOD_VERSION = "e1-fixed-ridge-v1"
NEXT_METHODS = ("no_change", "historical_frequency", "last_change", "ridge")
REVERSAL_METHODS = ("no_reversal", "historical_frequency", "ridge")
L2 = 1.0
LOG_RETURN_BOUND = math.log(100)


@dataclass
class Fitted:
    n: int
    mean: FloatArray
    scale: FloatArray
    intercept: FloatArray
    coefficients: FloatArray
    historical: FloatArray
    fallback: bool
    max_training_origin: int | None
    max_training_maturity: int | None

    def predict(self, features: FloatArray) -> FloatArray:
        if self.fallback:
            return np.tile(self.historical, (len(features), 1))
        return (features - self.mean) / self.scale @ self.coefficients + self.intercept

    def serial(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "intercept": self.intercept.tolist(),
            "coefficients": self.coefficients.tolist(),
            "historical": self.historical.tolist(),
            "fallback": self.fallback,
            "max_training_origin": self.max_training_origin,
            "max_training_maturity": self.max_training_maturity,
            "objective": (
                "sum_squared_error_plus_1_times_squared_standardized_coefficients;"
                "unpenalized_intercept"
            ),
            "minimum_training_rows": len(FEATURE_NAMES) + 2,
        }


class Statistics:
    """Accumulate rows once as each fixed horizon matures; bounded O(p²) fitting."""

    def __init__(self, task: str) -> None:
        self.task = task
        self.n = 0
        width = len(FEATURE_NAMES)
        outputs = 4 if task == "next_period" else 1
        self.sx = np.zeros(width)
        self.sxx = np.zeros((width, width))
        self.sy = np.zeros(outputs)
        self.sxy = np.zeros((width, outputs))
        self.max_training_origin: int | None = None
        self.max_training_maturity: int | None = None

    def add(self, features: FloatArray, targets: list[Target], origin: int, cutoff: int) -> None:
        indices = [
            index
            for index, target in enumerate(targets)
            if target.state == "observed"
            and target.maturity_period is not None
            and target.maturity_period <= cutoff
        ]
        if not indices:
            return
        selected = features[indices]
        if self.task == "next_period":
            values = np.array(
                [
                    [float(targets[index].direction == label) for label in (-1, 0, 1)]
                    + [float(targets[index].log_return or 0)]
                    for index in indices
                ],
                dtype=np.float64,
            )
        else:
            values = np.array(
                [[targets[index].full_reversal] for index in indices], dtype=np.float64
            )
        self.n += len(indices)
        self.sx += selected.sum(axis=0)
        self.sxx += selected.T @ selected
        self.sy += values.sum(axis=0)
        self.sxy += selected.T @ values
        self.max_training_origin = origin
        self.max_training_maturity = max(targets[index].maturity_period or 0 for index in indices)

    def fit(self) -> Fitted:
        width = len(FEATURE_NAMES)
        historical = (
            (self.sy + np.array([1, 1, 1, 0])) / np.array([self.n + 3] * 3 + [max(self.n, 1)])
            if self.task == "next_period"
            else (self.sy + 1) / (self.n + 2)
        )
        mean = self.sx / max(self.n, 1)
        centered = self.sxx - np.outer(self.sx, mean)
        scale = np.sqrt(np.maximum(np.diag(centered) / max(self.n, 1), 0))
        scale[scale < 1e-8] = 1
        fallback = self.n < width + 2
        coefficients = np.zeros((width, len(self.sy)))
        intercept = self.sy / max(self.n, 1)
        if not fallback:
            gram = centered / np.outer(scale, scale)
            rhs = (self.sxy - np.outer(mean, self.sy)) / scale[:, None]
            coefficients = np.linalg.solve(gram + L2 * np.eye(width), rhs)
        return Fitted(
            self.n,
            mean,
            scale,
            intercept,
            coefficients,
            historical,
            fallback,
            self.max_training_origin,
            self.max_training_maturity,
        )


def next_predictions(
    model: Fitted, features: FloatArray, current_dkk: FloatArray
) -> dict[str, dict[str, FloatArray]]:
    length = len(features)
    unchanged = np.tile([0.0, 1.0, 0.0], (length, 1))
    previous = features[:, FEATURE_NAMES.index("lag1_log_return")]
    rule = np.stack((previous < 0, previous == 0, previous > 0), axis=1).astype(float)
    learned = model.predict(features)
    probabilities = np.maximum(learned[:, :3], 0)
    totals = probabilities.sum(axis=1, keepdims=True)
    probabilities = np.divide(
        probabilities, totals, out=np.tile(model.historical[:3], (length, 1)), where=totals > 0
    )
    raw = {
        "no_change": (unchanged, np.zeros(length)),
        "historical_frequency": (
            np.tile(model.historical[:3], (length, 1)),
            np.full(length, model.historical[3]),
        ),
        "last_change": (rule, previous),
        "ridge": (probabilities, learned[:, 3]),
    }
    result: dict[str, dict[str, FloatArray]] = {}
    for method, (probs, returns) in raw.items():
        bounded = np.clip(returns, -LOG_RETURN_BOUND, LOG_RETURN_BOUND)
        # Flat wins a tie, then fall, then rise. No future-dependent threshold.
        choice = np.argmax(probs[:, [1, 0, 2]], axis=1)
        direction = np.array([0, -1, 1])[choice]
        result[method] = {
            "probabilities": probs,
            "direction": direction,
            "log_return": bounded,
            "price_dkk": current_dkk * np.exp(bounded),
            "magnitude_clipped": (returns != bounded).astype(float),
        }
    return result


def reversal_predictions(model: Fitted, features: FloatArray) -> dict[str, FloatArray]:
    return {
        "no_reversal": np.zeros(len(features)),
        "historical_frequency": np.full(len(features), model.historical[0]),
        "ridge": np.clip(model.predict(features)[:, 0], 0, 1),
    }
