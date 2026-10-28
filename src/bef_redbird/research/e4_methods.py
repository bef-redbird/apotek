"""Prespecified E4 routing and one conditional residual interval refinement."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e3_methods import calibrate


def route(
    frame: pd.DataFrame, package: np.ndarray, equal: np.ndarray, broad: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    n = len(frame)
    if any(x.shape != (n, 3) for x in (package, equal, broad)):
        raise ResearchError("e4_route_alignment")
    has_equal = frame.peer_count.ge(1).fillna(False).to_numpy(dtype=bool)
    has_broad = (
        (
            frame.broad_peer_count.ge(1)
            & frame.broad_peer_feature_state.isin(
                ["complete_same_unit_peers", "partial_same_unit_peers"]
            )
        )
        .fillna(False)
        .to_numpy(dtype=bool)
    )
    out = np.where(has_broad[:, None], broad, np.where(has_equal[:, None], equal, package))
    names = np.where(
        has_broad, "broader peers", np.where(has_equal, "equal-size peers", "package history")
    )
    return out, names


def mixed(advance: np.ndarray, current: np.ndarray, codes: pd.Series) -> np.ndarray:
    return np.where(codes.isin(["A", "B", "C"]).to_numpy()[:, None], advance, current)


def stratum(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    price = np.digitize(frame.aip_ore.to_numpy(dtype=float) / 100, [100, 1000])
    history = frame.history_periods.lt(6).to_numpy(dtype=int)
    peers = np.where(
        frame.peer_count.ge(1).fillna(False),
        2,
        np.where(frame.peer_count.eq(0).fillna(False), 1, 0),
    )
    return price, price * 6 + history * 3 + peers


def fit_residual(frame: pd.DataFrame, cutoff: str, minimum: int = 200) -> dict[str, Any]:
    known = frame.loc[
        frame.next_status.eq("observed") & frame.next_maturity_date.lt(pd.Timestamp(cutoff))
    ].copy()
    if not known.empty and (known.period_start >= pd.Timestamp(cutoff)).any():
        raise ResearchError("e4_residual_not_earlier")
    if known.empty:
        return {"status": "unavailable", "rows": 0, "cutoff": cutoff}
    error = np.abs(known.next_aip_dkk.to_numpy() - known.next_aip_ore.to_numpy(dtype=float) / 100)
    price, cell = stratum(known)
    radii = {}
    for key in range(18):
        fine, coarse = error[cell == key], error[price == key // 6]
        use = fine if len(fine) >= minimum else coarse if len(coarse) >= minimum else error
        radii[str(key)] = {
            "radius_dkk": float(np.quantile(use, 0.9, method="higher")),
            "cell_support": len(fine),
            "used_support": len(use),
            "source": "cell"
            if len(fine) >= minimum
            else "price"
            if len(coarse) >= minimum
            else "pooled",
        }
    return {
        "status": "available",
        "rows": len(known),
        "excluded_unmature_or_unknown": len(frame) - len(known),
        "cutoff": cutoff,
        "last_origin": str(known.period_start.max())[:10],
        "last_maturity": str(known.next_maturity_date.max())[:10],
        "minimum_support": minimum,
        "quantile": "row-weighted q90 higher",
        "radii": radii,
    }


def residual_bounds(
    frame: pd.DataFrame, prediction: np.ndarray, state: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    if state["status"] != "available":
        return np.full(len(frame), np.nan), np.full(len(frame), np.nan)
    _, cell = stratum(frame)
    radius = np.array([state["radii"][str(x)]["radius_dkk"] for x in cell])
    return np.maximum(0, prediction - radius), prediction + radius


def calibrated_routes(
    frame: pd.DataFrame, advance: np.ndarray, current: np.ndarray, states: dict[str, Any]
) -> dict[str, np.ndarray]:
    return {
        "routed": calibrate(advance, states["routed"]),
        "hybrid": calibrate(mixed(advance, current, frame.advance_abc), states["hybrid"]),
        "current": calibrate(current, states["current"]),
    }
