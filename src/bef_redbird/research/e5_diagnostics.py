"""Small temporal contrasts and bounded explanations of saved E5 models."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor, Pool

from .e2_metrics import paired_interval
from .e2_models import catboost_frame
from .e4_common import checked, frozen_json, read, utc
from .e5_assessment import ORIGIN, _check_models
from .e5_results import align
from .group_features import pin


def contrasts(root: Path) -> dict[str, Any]:
    path = root / "contrasts.json"
    if path.exists():
        return read(path)
    rows = []
    for fold in ("spring", "summer"):
        result = read(root / "evaluations" / fold / "result.json")
        p = pd.read_parquet(checked(result["files"]["predictions.parquet"]))
        o = align(p, pd.read_parquet(checked(result["files"]["outcomes.parquet"])))
        valid = o.next_status.eq("observed").to_numpy()
        y = o.loc[valid, "next_direction"].to_numpy(dtype=int) + 1
        data = p.loc[valid].copy()
        for method in ("frozen", "fresh", "tuned"):
            names = [f"{method}_hybrid_p{k}" for k in range(3)]
            if not all(x in data for x in names):
                continue
            score = ((data[names].to_numpy() - np.eye(3)[y]) ** 2).sum(axis=1)
            for day, value in (
                pd.Series(score, index=data.period_start).groupby(level=0).mean().items()
            ):
                rows.append(
                    {
                        "period_start": str(day)[:10],
                        "fold": fold,
                        "method": method,
                        "brier": float(value),
                    }
                )
    frame = pd.DataFrame(rows)
    wide = frame.pivot(index="period_start", columns="method", values="brier")
    compared = []
    for candidate, reference in (("fresh", "frozen"), ("tuned", "fresh")):
        if candidate not in wide:
            continue
        delta = (wide[candidate] - wide[reference]).to_numpy()
        compared.append(
            {
                "candidate": candidate,
                "reference": reference,
                "periods": len(delta),
                "mean_brier_difference": float(delta.mean()),
                "better_periods": int((delta < 0).sum()),
                "worse_periods": int((delta > 0).sum()),
                "best_difference": float(delta.min()),
                "worst_difference": float(delta.max()),
                "block4": paired_interval(delta, seed=20261027, block=4),
                "block8": paired_interval(delta, seed=20261027, block=8),
            }
        )
    value = {
        "version": "e5-temporal-contrasts-v1",
        "created_utc": utc(),
        "periods": rows,
        "contrasts": compared,
        "claim": (
            "Descriptive paired circular-block intervals on exposed history, "
            "not prospective confirmation."
        ),
    }
    frozen_json(path, value)
    return value


def explanations(root: Path) -> dict[str, Any]:
    path = root / "explanations.json"
    if path.exists():
        return read(path)
    inputs = read(root / "inputs/manifest.json")
    bundle_path = root / "final-assessment/model-bundle.json"
    bundle = read(bundle_path)
    _check_models(bundle, ORIGIN)
    frame = pd.read_parquet(checked(inputs["files"]["current"]))
    frame = frame.loc[frame.eligible].copy().reset_index(drop=True)
    amounts = read(checked(bundle["models"]["mean_current"]["trial"]))
    predicted = align(frame, pd.read_parquet(checked(amounts["files"]["predictions.parquet"])))
    frame["sampling_hash"] = [hashlib.sha256(f"e5:{x}".encode()).hexdigest() for x in frame.row_key]
    indices = list(frame.sort_values("sampling_hash").head(128).index)
    indices += list(frame.sort_values("aip_ore", ascending=False).head(64).index)
    indices += list(predicted.sort_values("delta_dkk", ascending=False).head(64).index)
    sample = frame.loc[list(dict.fromkeys(indices))].reset_index(drop=True)
    summaries, cases = [], []
    for name, kind in (("selected_direction", "direction"), ("rmse", "amount")):
        item = bundle["models"]["cb_broad_current" if kind == "direction" else "mean_current"]
        model: Any = CatBoostClassifier() if kind == "direction" else CatBoostRegressor()
        model.load_model(str(checked(item["model"])))
        features, categories = item["features"], item["categories"]
        explained = sample
        if kind == "direction":
            explained = sample.loc[
                sample.broad_peer_count.ge(1)
                & sample.broad_peer_feature_state.isin(
                    ["complete_same_unit_peers", "partial_same_unit_peers"]
                )
            ].reset_index(drop=True)
        pool = Pool(
            catboost_frame(explained, features, categories), cat_features=categories, thread_count=6
        )
        shap = np.asarray(model.get_feature_importance(pool, type="ShapValues", thread_count=6))
        raw = np.asarray(model.predict(pool, prediction_type="RawFormulaVal", thread_count=6))
        if shap.ndim == 3:
            # Rise-class logit contributions, not percentage probability changes.
            contributions = shap[:, 2, :-1]
            summed = contributions.sum(axis=1) + shap[:, 2, -1]
            direct = raw[:, 2]
            units = "rise-class raw logit contribution"
        else:
            contributions = shap[:, :-1]
            summed = contributions.sum(axis=1) + shap[:, -1]
            direct = raw.reshape(-1)
            units = "raw DKK delta contribution before nonnegative support adjustment"
        if not np.allclose(summed, direct, rtol=1e-8, atol=1e-8):
            raise ValueError("SHAP additivity failed against raw model output")
        importance = model.get_feature_importance(type="PredictionValuesChange")
        summary = [
            {
                "feature": feature,
                "mean_absolute_contribution": float(np.abs(contributions[:, i]).mean()),
                "prediction_change_importance": float(importance[i]),
            }
            for i, feature in enumerate(features)
        ]
        summary.sort(key=lambda x: x["mean_absolute_contribution"], reverse=True)
        summaries.append(
            {
                "model": name,
                "information": "current",
                "sample_count": len(explained),
                "scope": "Applied broader component only"
                if kind == "direction"
                else "Equal-peer amount model",
                "maximum_raw_additivity_error": float(np.max(np.abs(summed - direct))),
                "units": units,
                "model_pin": item["model"],
                "fit_cutoff": item["fit_cutoff"],
                "source_receipt": item["trial"],
                "features": summary,
            }
        )
        for row, values in zip(explained.to_dict("records"), contributions, strict=True):
            top = np.argsort(np.abs(values))[-3:][::-1]
            cases.append(
                {
                    "row_key": row["row_key"],
                    "model": name,
                    "top_contributions": [
                        {"feature": features[i], "contribution": float(values[i])} for i in top
                    ],
                }
            )
    advance = bundle["models"]["cb_broad_advance"]
    model = CatBoostClassifier()
    model.load_model(str(checked(advance["model"])))
    importance = model.get_feature_importance(type="PredictionValuesChange")
    value = {
        "version": "e5-bounded-explanations-v1",
        "created_utc": utc(),
        "model_bundle": pin(bundle_path),
        "resource_fallback": bundle.get("resource_fallback"),
        "unique_cases": len(sample),
        "sampling": (
            "128 stable hashes plus 64 highest current AIP plus 64 greatest adjusted mean "
            "opportunity; deduplicated, <=256. No outcome-based sample. Direction "
            "explanations further require that the broader route actually applies."
        ),
        "summaries": summaries,
        "cases": cases,
        "advance_direction_importance": [
            {"feature": x, "importance": float(v)}
            for x, v in zip(advance["features"], importance, strict=True)
        ],
        "advance_direction_model": {
            "model_pin": advance["model"],
            "source_receipt": advance["trial"],
            "fit_cutoff": advance["fit_cutoff"],
        },
        "claim": (
            "Feature contributions explain model behavior, "
            "not causal effects or procurement profit."
        ),
        "current_features": inputs["files"]["current"],
    }
    frozen_json(path, value)
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["contrasts", "explain"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "contrasts":
        contrasts(args.root.resolve())
    else:
        explanations(args.root.resolve())
