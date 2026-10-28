from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research import e5_diagnostics as module
from bef_redbird.research.e4_common import frozen_json, read
from bef_redbird.research.group_features import pin


def test_explanations_use_actual_bundle_direction_and_september_amount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "inputs").mkdir()
    frame = pd.DataFrame(
        {
            "row_key": ["a", "b", "c", "d"],
            "eligible": [True] * 4,
            "aip_ore": [10000, 20000, 30000, 40000],
            "broad_peer_count": [1, 2, 0, 0],
            "broad_peer_feature_state": [
                "complete_same_unit_peers",
                "partial_same_unit_peers",
                "no_peers",
                "unknown",
            ],
        }
    )
    frame.to_parquet(tmp_path / "inputs/current.parquet", index=False)
    frozen_json(
        tmp_path / "inputs/manifest.json",
        {"files": {"current": pin(tmp_path / "inputs/current.parquet")}},
    )
    models = {}
    for name, cutoff in (
        ("cb_broad_current", "2026-07-01"),
        ("cb_broad_advance", "2026-07-01"),
        ("mean_current", "2026-09-01"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "model.cbm").write_bytes(name.encode())
        frame[["row_key"]].assign(delta_dkk=[1, 2, 3, 4]).to_parquet(
            directory / "predictions.parquet", index=False
        )
        frozen_json(
            directory / "receipt.json",
            {"files": {"predictions.parquet": pin(directory / "predictions.parquet")}},
        )
        models[name] = {
            "model": pin(directory / "model.cbm"),
            "trial": pin(directory / "receipt.json"),
            "features": ["aip_ore"],
            "categories": [],
            "fit_cutoff": cutoff,
            "latest_label_maturity": "2026-06-22",
        }
    frozen_json(tmp_path / "residual.json", {})
    (tmp_path / "final-assessment").mkdir()
    frozen_json(
        tmp_path / "final-assessment/model-bundle.json",
        {
            "models": models,
            "direction_fit_cutoff": "2026-07-01",
            "amount_fit_cutoff": "2026-09-01",
            "amount_interval_calibration": pin(tmp_path / "residual.json"),
        },
    )
    loaded = []

    class SyntheticPool:
        def __init__(self, matrix: pd.DataFrame, **kwargs: Any) -> None:
            self.rows = len(matrix)

    class Classifier:
        direction = True

        def load_model(self, path: str) -> None:
            loaded.append(Path(path).parent.name)

        def get_feature_importance(
            self, pool: SyntheticPool | None = None, *, type: str = "", **kwargs: Any
        ) -> np.ndarray:
            if type == "PredictionValuesChange":
                return np.array([100.0])
            assert pool is not None
            shape = (pool.rows, 3, 2) if self.direction else (pool.rows, 2)
            values = np.ones(shape)
            values[..., 0] = 2
            return values

        def predict(self, pool: SyntheticPool, **kwargs: Any) -> np.ndarray:
            return np.full((pool.rows, 3) if self.direction else pool.rows, 3.0)

    class Regressor(Classifier):
        direction = False

    monkeypatch.setattr(module, "Pool", SyntheticPool)
    monkeypatch.setattr(module, "CatBoostClassifier", Classifier)
    monkeypatch.setattr(module, "CatBoostRegressor", Regressor)
    result = module.explanations(tmp_path)
    assert loaded == ["cb_broad_current", "mean_current", "cb_broad_advance"]
    assert result["unique_cases"] == 4
    summary = {r["model"]: r for r in result["summaries"]}
    assert summary["selected_direction"]["fit_cutoff"] == "2026-07-01"
    assert summary["selected_direction"]["sample_count"] == 2
    assert summary["rmse"]["fit_cutoff"] == "2026-09-01"
    assert all(r["maximum_raw_additivity_error"] == 0 for r in result["summaries"])
    assert (
        result["advance_direction_model"]["source_receipt"] == models["cb_broad_advance"]["trial"]
    )
    assert read(tmp_path / "explanations.json") == result
    # No final September direction receipt exists: implicit path fallback would fail.
    assert not (tmp_path / "trials/final-selected_direction-current").exists()
