"""Small local models with training-only transforms and explicit probability order."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import psutil
from scipy import sparse
from scipy.optimize import minimize
from scipy.special import logsumexp, softmax
from threadpoolctl import threadpool_limits

from .dataset import ResearchError


def period_weights(frame: pd.DataFrame) -> np.ndarray:
    counts = frame.groupby("period_start")["period_start"].transform("size").to_numpy()
    weight = 1.0 / counts
    return weight / weight.mean()


def categorical(series: pd.Series) -> pd.Series:
    return series.map(lambda x: "M:" if pd.isna(x) else "V:" + str(x))


def catboost_frame(frame: pd.DataFrame, features: list[str], categories: list[str]) -> pd.DataFrame:
    result = frame[features].copy()
    for name in features:
        result[name] = (
            categorical(result[name])
            if name in categories
            else pd.to_numeric(result[name]).astype(float)
        )
    return result


@dataclass
class LogisticModel:
    features: list[str]
    categories: list[str]
    penalty: float = 0.001
    maximum_iterations: int = 150
    transforms: dict[str, Any] = field(default_factory=dict)
    coefficients: Any = None
    convergence: dict[str, Any] = field(default_factory=dict)

    def matrix(self, frame: pd.DataFrame, *, fit: bool = False) -> Any:
        blocks: list[Any] = [sparse.csr_matrix(np.ones((len(frame), 1)))]
        for name in self.features:
            if name in self.categories:
                values = categorical(frame[name])
                if fit:
                    # Count-frequency encoding vocabulary is learned on training only.
                    counts = values.value_counts()
                    ordered = sorted(counts.index, key=lambda x: (-counts[x], x))[:128]
                    self.transforms[name] = {"levels": ordered}
                levels = self.transforms[name]["levels"]
                lookup = {x: i for i, x in enumerate(levels)}
                indices = values.map(lookup).fillna(len(levels)).to_numpy(dtype=int)
                blocks.append(
                    sparse.csr_matrix(
                        (np.ones(len(frame)), (np.arange(len(frame)), indices)),
                        shape=(len(frame), len(levels) + 1),
                    )
                )
            else:
                raw = pd.to_numeric(frame[name]).to_numpy(dtype=float)
                missing = ~np.isfinite(raw)
                # Fixed signed-log transform keeps exact money in the source and
                # avoids letting a few extreme package prices dominate regression.
                scaled = np.sign(raw) * np.log1p(np.abs(raw))
                if fit:
                    known = scaled[~missing]
                    median = float(np.median(known)) if len(known) else 0.0
                    filled = np.where(missing, median, scaled)
                    self.transforms[name] = {
                        "median": median,
                        "mean": float(filled.mean()),
                        "scale": max(float(filled.std()), 1e-8),
                    }
                state = self.transforms[name]
                values = (np.where(missing, state["median"], scaled) - state["mean"]) / state[
                    "scale"
                ]
                blocks.append(sparse.csr_matrix(np.column_stack([values, missing.astype(float)])))
        return sparse.hstack(blocks, format="csr")

    def fit(self, frame: pd.DataFrame, y: np.ndarray) -> LogisticModel:
        started = time.monotonic()
        matrix = self.matrix(frame, fit=True)
        weights = period_weights(frame)
        weights /= weights.sum()
        classes = 3
        target = np.eye(classes)[y]

        def objective(flat: np.ndarray) -> tuple[float, np.ndarray]:
            if (
                time.monotonic() - started > 1500
                or psutil.Process().memory_info().rss > 28 * 1024**3
            ):
                raise ResearchError("e2_declared_fit_resource_limit")
            beta = flat.reshape(matrix.shape[1], classes)
            scores = matrix @ beta
            log_probability = scores - logsumexp(scores, axis=1, keepdims=True)
            loss = -float(np.sum(weights[:, None] * target * log_probability))
            regularized = beta.copy()
            regularized[0] = 0
            loss += self.penalty * float(np.sum(regularized**2)) / 2
            gradient = matrix.T @ ((np.exp(log_probability) - target) * weights[:, None])
            gradient += self.penalty * regularized
            return loss, np.asarray(gradient).ravel()

        with threadpool_limits(limits=6):
            result = minimize(
                objective,
                np.zeros(matrix.shape[1] * classes),
                jac=True,
                method="L-BFGS-B",
                options={"maxiter": self.maximum_iterations, "ftol": 1e-8, "gtol": 1e-5},
            )
        self.coefficients = result.x.reshape(matrix.shape[1], classes)
        self.convergence = {
            "converged": bool(result.success),
            "message": str(result.message),
            "iterations": int(result.nit),
            "objective": float(result.fun),
            "columns": int(matrix.shape[1]),
        }
        return self

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        return softmax(self.matrix(frame) @ self.coefficients, axis=1)

    def state(self) -> dict[str, Any]:
        return {
            "features": self.features,
            "categories": self.categories,
            "penalty": self.penalty,
            "transforms": self.transforms,
            "coefficients": self.coefficients.tolist(),
            "convergence": self.convergence,
        }


def frequency_probabilities(frame: pd.DataFrame, y: np.ndarray) -> np.ndarray:
    weight = period_weights(frame)
    # Explicit Laplace smoothing, with equal total weight per tariff period.
    counts = np.bincount(y, weights=weight, minlength=3) + 1.0
    return counts / counts.sum()
