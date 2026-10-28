"""Bounded sequential E5 fitting on shared immutable research inputs."""

from __future__ import annotations

import argparse
import gc
import json
import shutil
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psutil
from catboost import CatBoostClassifier, CatBoostRegressor, Pool

from .dataset import ResearchError
from .e2_data import attach_outcomes, verify_training_manifest
from .e2_experiment import ResourceSample, utc
from .e2_models import catboost_frame, period_weights
from .e3_data import E3Data
from .e4_common import checked, frozen_json, read
from .e5_protocol import FOLDS, TUNING, freeze, require
from .group_features import pin
from .training_contract import KEYS, atomic_json


def persistence_labels(features: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Future joins create labels only; no future row is required for origin eligibility."""
    base = attach_outcomes(features[list(KEYS) + ["aip_ore"]], labels)
    following = labels[
        [
            "package_number",
            "period_index",
            "identity_episode_id",
            "next_status",
            "next_aip_ore",
            "next_maturity_date",
        ]
    ].copy()
    eligible_keys = (
        features.loc[features.eligible, "row_key"] if "eligible" in features else features.row_key
    )
    following["second_origin_eligible"] = labels.row_key.isin(eligible_keys).to_numpy()
    following["period_index"] -= 1
    following = following.rename(
        columns={
            "identity_episode_id": "second_episode",
            "next_status": "second_following_status",
            "next_aip_ore": "second_aip_ore",
            "next_maturity_date": "second_maturity_date",
        }
    )
    result = base.merge(
        following,
        on=["package_number", "period_index"],
        how="left",
        validate="one_to_one",
        sort=False,
    )
    if not result.row_key.equals(base.row_key):
        raise ResearchError("e5_horizon_join_changed_origins")
    result["second_status"] = "observed"
    result.loc[result.second_following_status.isna(), "second_status"] = "missing_next_origin"
    result.loc[result.period_index.add(2).gt(labels.period_index.max()), "second_status"] = (
        "pending_horizon"
    )
    result.loc[
        result.second_following_status.notna() & result.second_following_status.ne("observed"),
        "second_status",
    ] = result.second_following_status
    result.loc[
        result.second_episode.notna() & result.second_episode.ne(result.identity_episode_id),
        "second_status",
    ] = "identity_discontinuity"
    result.loc[result.second_origin_eligible.eq(False), "second_status"] = "ineligible_next_origin"
    result.loc[result.next_status.ne("observed"), "second_status"] = "first_" + result.next_status
    observed = result.second_status.eq("observed")
    result["persistence_target"] = np.where(
        observed,
        ((result.next_aip_ore > result.aip_ore) & (result.second_aip_ore > result.aip_ore)).astype(
            float
        ),
        np.nan,
    )
    result.loc[~observed, "second_aip_ore"] = np.nan
    return result.drop(
        columns=["aip_ore", "second_episode", "second_following_status", "second_origin_eligible"]
    )


def prepare(root: Path) -> None:
    protocol = require(root)
    dest = root / "inputs"
    manifest = dest / "manifest.json"
    if manifest.exists():
        for p in read(manifest)["files"].values():
            checked(p)
        return
    dest.mkdir(exist_ok=True)
    for p in protocol["pins"].values():
        checked(p)
    paths = {k: Path(v["path"]) for k, v in protocol["pins"].items()}
    verify_training_manifest(paths["dataset"])
    resource = ResourceSample()
    data = E3Data(
        paths["dataset"],
        paths["groups"],
        paths["advance"],
        paths["broader"],
        scratch=root / "scratch",
    )
    features = data.candidates("2022-01-01", "2026-10-26")
    labels = data.outcomes("2022-01-01", "2026-10-26")
    outcomes = persistence_labels(features, labels)
    data.close()
    del data, labels
    # Exhaustively verify common origin inputs against accepted saved cohorts.
    aligned = []
    for year in (2025, 2026):
        mpath = paths["e3_protocol"].parent / "cohorts" / f"evaluation_{year}" / "manifest.json"
        old = read(mpath)
        saved = pd.read_parquet(checked(old["features"]))
        part = features.loc[features.period_start.dt.year.eq(year)].reset_index(drop=True)
        pd.testing.assert_frame_equal(part[saved.columns], saved, check_dtype=False)
        aligned.append(
            {
                "year": year,
                "rows": len(saved),
                "features": pin(checked(old["features"])),
                "exact_values": True,
            }
        )
    features.to_parquet(dest / "features.parquet", index=False)
    outcomes.to_parquet(dest / "outcomes.parquet", index=False)
    current_path = root.parent / "research-e4/features/2026-10-26-assessment.parquet"
    current = pd.read_parquet(current_path)
    frozen_json(
        manifest,
        {
            "version": "e5-shared-inputs-v1",
            "protocol": pin(root / "protocol.json"),
            "files": {
                "features": pin(dest / "features.parquet"),
                "outcomes": pin(dest / "outcomes.parquet"),
                "current": pin(current_path),
            },
            "rows": len(features),
            "current_origins": len(current),
            "aligned": aligned,
            "horizon_status": outcomes.second_status.value_counts().to_dict(),
            "resource": resource.finish(),
            "created_utc": utc(),
        },
    )


def read_inputs(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    manifest = read(root / "inputs/manifest.json")
    return (
        pd.read_parquet(checked(manifest["files"]["features"])),
        pd.read_parquet(checked(manifest["files"]["outcomes"])),
    )


def training_rows(
    features: pd.DataFrame, outcomes: pd.DataFrame, cutoff: str, *, persistence: bool = False
) -> pd.DataFrame:
    end = pd.Timestamp(cutoff)
    start = end - pd.DateOffset(years=3)
    selected = features.loc[
        features.period_start.ge(start) & features.period_start.lt(end)
    ].reset_index(drop=True)
    frame = attach_outcomes(selected, outcomes)
    status, maturity = (
        ("second_status", "second_maturity_date")
        if persistence
        else ("next_status", "next_maturity_date")
    )
    return frame.loc[frame[status].eq("observed") & frame[maturity].lt(end)].reset_index(drop=True)


def bounded_support(raw: np.ndarray, current: np.ndarray) -> np.ndarray:
    if not np.isfinite(raw).all() or not np.isfinite(current).all() or (current <= 0).any():
        raise ResearchError("e5_invalid_money_prediction")
    return (
        np.maximum(0.0, current[:, None] + raw) if raw.ndim == 2 else np.maximum(0.0, current + raw)
    )


class Limit:
    def __init__(self, limits: dict[str, Any]) -> None:
        self.limits = limits
        self.started = time.monotonic()
        self.exceeded = False

    def after_iteration(self, info: Any) -> bool:
        self.exceeded = (
            time.monotonic() - self.started > self.limits["fit_seconds"]
            or psutil.Process().memory_info().rss > self.limits["process_rss_bytes"]
        )
        return not self.exceeded


def spec_features(protocol: dict[str, Any], task: str, mode: str) -> tuple[list[str], list[str]]:
    bundle = read(checked(protocol["pins"]["e4_model-bundle"]))
    key = f"cb_broad_{mode}" if task == "direction" else f"amount_delta_floor_{mode}"
    model = bundle["models"][key]
    return model["features"], model["categories"]


def model_params(root: Path, spec: dict[str, Any], protocol: dict[str, Any]) -> dict[str, Any]:
    params = dict(protocol["settings"])
    if spec["task"] == "direction":
        method = spec["method"]
        if method in TUNING:
            params.update(TUNING[method])
        elif method in {"tuned", "selected_direction"}:
            selected = read(root / "selection.json")["direction"]
            if selected["method"] != "unchanged":
                params.update(TUNING[selected["method"]])
                params["iterations"] = selected["iterations"][spec["information"]]
        params.update(loss_function="MultiClass", classes_count=3)
    elif spec["task"] == "persistence":
        params.update(loss_function="Logloss")
    else:
        params.update(loss_function=protocol["amount"]["objectives"][spec["method"]])
    return params


def ledger(root: Path) -> dict[str, Any]:
    receipts = [read(p) for p in sorted((root / "trials").glob("*/receipt.json"))]
    return {
        "version": "e5-fit-ledger-v1",
        "attempts": len(receipts),
        "trials": receipts,
        "completed": sum(x["status"] == "completed" for x in receipts),
        "failed": sum(x["status"] == "failed" for x in receipts),
    }


def capture_producer(root: Path) -> dict[str, Any]:
    source = Path(__file__).parent
    repo = source.parents[2]
    code = {
        name: pin(source / name)
        for name in ("e5_experiment.py", "e5_protocol.py", "e2_models.py", "training_contract.py")
    }
    path = root / "fit-producer.json"
    if path.exists():
        prior = read(path)
        if prior["code"] != code:
            raise ResearchError("e5_fit_producer_changed")
        checked(prior["archive"])
        return prior
    archive_path = root / "producer-fit.zip"
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in [
            *sorted((repo / "src").rglob("*.py")),
            *sorted((repo / "src").rglob("*.sql")),
            repo / "pyproject.toml",
            repo / "uv.lock",
        ]:
            archive.write(file, file.relative_to(repo).as_posix())
    value = {"code": code, "archive": pin(archive_path), "created_utc": utc()}
    frozen_json(path, value)
    return value


def predict(
    model: Any, features: pd.DataFrame, spec: dict[str, Any], names: list[str], cats: list[str]
) -> pd.DataFrame:
    output = features[["row_key"]].copy()
    x = catboost_frame(features, names, cats)
    if spec["task"] == "direction":
        p = model.predict_proba(x, thread_count=6)
        if list(model.classes_) != [0, 1, 2]:
            raise ResearchError("e5_class_order")
        if not np.isfinite(p).all() or (p < 0).any() or not np.allclose(p.sum(axis=1), 1):
            raise ResearchError("e5_invalid_probabilities")
        output[["p0", "p1", "p2"]] = p
    elif spec["task"] == "persistence":
        if list(model.classes_) != [0, 1]:
            raise ResearchError("e5_binary_class_order")
        output["persistence_probability"] = model.predict_proba(x, thread_count=6)[:, 1]
        if not output.persistence_probability.between(0, 1).all():
            raise ResearchError("e5_invalid_persistence_probability")
    else:
        raw = np.asarray(model.predict(x, thread_count=6))
        current = features.aip_ore.to_numpy(dtype=float) / 100
        if spec["method"] == "quantile":
            output[["raw_q05_delta_dkk", "raw_q50_delta_dkk", "raw_q95_delta_dkk"]] = raw
            output["crossed"] = (np.diff(raw, axis=1) < 0).any(axis=1)
            prices = bounded_support(np.sort(raw, axis=1), current)
            output[["q05_price_dkk", "q50_price_dkk", "q95_price_dkk"]] = prices
        else:
            output["raw_delta_dkk"] = raw
            output["next_price_dkk"] = bounded_support(raw, current)
            output["delta_dkk"] = output.next_price_dkk - current
    return output


def run_trial(
    root: Path, spec: dict[str, Any], features: pd.DataFrame, outcomes: pd.DataFrame
) -> dict[str, Any]:
    protocol = require(root)
    folder = root / "trials" / spec["id"]
    receipt_path = folder / "receipt.json"
    if receipt_path.exists():
        prior = read(receipt_path)
        if prior["status"] == "running":
            raise ResearchError("e5_interrupted_attempt_requires_explicit_failure_record")
        for p in prior.get("files", {}).values():
            checked(p)
        return prior
    limits = protocol["resource_limits"]
    if ledger(root)["attempts"] >= limits["base_fit_attempts"]:
        raise ResearchError("e5_fit_budget_exhausted")
    if shutil.disk_usage(root).free < limits["free_disk_reserve_bytes"]:
        raise ResearchError("e5_disk_reserve")
    if sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > limits["research_bytes"]:
        raise ResearchError("e5_storage_limit")
    fold = next(x for x in FOLDS if x["name"] == spec["fold"])
    folder.mkdir(parents=True)
    resource = ResourceSample()
    receipt: dict[str, Any] = {
        "spec": spec,
        "status": "running",
        "protocol": pin(root / "protocol.json"),
        "started_utc": utc(),
        "producer": capture_producer(root),
        "inputs": pin(root / "inputs/manifest.json"),
    }
    atomic_json(receipt_path, receipt)  # Attempt counted before any fitting work.
    try:
        training = training_rows(
            features, outcomes, fold["fit"], persistence=spec["task"] == "persistence"
        )
        names, cats = spec_features(protocol, spec["task"], spec["information"])
        params = model_params(root, spec, protocol)
        y = (
            training.next_direction.to_numpy(dtype=int) + 1
            if spec["task"] == "direction"
            else training.persistence_target.to_numpy(dtype=int)
            if spec["task"] == "persistence"
            else training.next_delta_ore.to_numpy(dtype=float) / 100
        )
        maturity = "second_maturity_date" if spec["task"] == "persistence" else "next_maturity_date"
        receipt.update(
            {
                "training_rows": len(training),
                "training_periods": int(training.period_start.nunique()),
                "training_start": str(pd.Timestamp(fold["fit"]) - pd.DateOffset(years=3))[:10],
                "latest_label_maturity": str(training[maturity].max())[:10],
                "params": params,
                "features": names,
                "categories": cats,
            }
        )
        if not len(training) or training[maturity].max() >= pd.Timestamp(fold["fit"]):
            raise ResearchError("e5_training_maturity")
        x = catboost_frame(training, names, cats)
        fit_pool = Pool(
            x, label=y, cat_features=cats, weight=period_weights(training), thread_count=6
        )
        del x, training
        gc.collect()
        extra: dict[str, Any] = {}
        if spec["fold"] == "screen" and spec["task"] == "direction":
            dev = features.loc[
                features.period_start.ge(fold["fit"]) & features.period_start.lt(fold["early_end"])
            ].reset_index(drop=True)
            dev = attach_outcomes(dev, outcomes)
            dev = dev.loc[
                dev.next_status.eq("observed") & dev.next_maturity_date.lt(fold["early_end"])
            ].reset_index(drop=True)
            extra = {
                "eval_set": Pool(
                    catboost_frame(dev, names, cats),
                    label=dev.next_direction.to_numpy(dtype=int) + 1,
                    cat_features=cats,
                    weight=period_weights(dev),
                    thread_count=6,
                ),
                "early_stopping_rounds": 30,
                "use_best_model": True,
            }
            receipt["early_stopping"] = {
                "start": fold["fit"],
                "end_exclusive": fold["early_end"],
                "last_maturity": str(dev.next_maturity_date.max())[:10],
                "rows": len(dev),
            }
            del dev
        model: Any = (
            CatBoostRegressor(**params)
            if spec["task"] == "amount"
            else CatBoostClassifier(**params)
        )
        if psutil.Process().memory_info().rss > limits["process_rss_bytes"]:
            raise ResearchError("e5_pool_resource_limit")
        limit = Limit(limits)
        model.fit(fit_pool, callbacks=[limit], **extra)
        if limit.exceeded:
            raise ResearchError("e5_declared_fit_resource_limit")
        model.save_model(str(folder / "model.cbm"))
        frozen_json(folder / "curves.json", model.get_evals_result())
        receipt.update(
            {
                "trees": model.tree_count_,
                "best_iteration": model.get_best_iteration(),
                "captured_params": model.get_all_params(),
            }
        )
        del fit_pool, extra
        gc.collect()
        prediction_start = (
            fold.get("early_end", fold["cal_start"])
            if spec["fold"] == "screen"
            else fold["cal_start"]
        )
        candidates = features.loc[
            features.period_start.ge(prediction_start) & features.period_start.lt(fold["end"])
        ].reset_index(drop=True)
        if spec["fold"] == "final":
            current = pd.read_parquet(
                checked(read(root / "inputs/manifest.json")["files"]["current"])
            )
            candidates = pd.concat([candidates, current.loc[current.eligible]], ignore_index=True)
        predicted = predict(model, candidates, spec, names, cats)
        restored: Any = CatBoostRegressor() if spec["task"] == "amount" else CatBoostClassifier()
        restored.load_model(str(folder / "model.cbm"))
        check = predict(restored, candidates, spec, names, cats)
        pd.testing.assert_frame_equal(predicted, check, check_exact=True)
        predicted.to_parquet(folder / "predictions.parquet", index=False)
        receipt.update(
            {
                "status": "completed",
                "candidate_rows": len(candidates),
                "exact_reload": True,
                "files": {
                    x: pin(folder / x) for x in ("model.cbm", "predictions.parquet", "curves.json")
                },
            }
        )
        del model, restored, candidates, predicted, check
    except Exception as exc:
        receipt.update({"status": "failed", "error": str(exc), "traceback": traceback.format_exc()})
    receipt["resource"] = resource.finish()
    if receipt["resource"]["sampled_peak_rss_bytes"] > limits["process_rss_bytes"]:
        receipt.update(status="failed", error="e5_total_process_rss_limit")
    if sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > limits["research_bytes"]:
        receipt.update(status="failed", error="e5_postwrite_storage_limit")
    receipt["finished_utc"] = utc()
    atomic_json(receipt_path, receipt)
    atomic_json(root / "fit-ledger.json", ledger(root))
    gc.collect()
    print(
        json.dumps(
            {
                "trial": spec["id"],
                "status": receipt["status"],
                "resource": receipt["resource"],
                "error": receipt.get("error"),
            }
        ),
        flush=True,
    )
    return receipt


def execute(root: Path, stage: str) -> None:
    protocol = require(root)
    prepare(root)
    features, outcomes = read_inputs(root)
    for spec in protocol["matrix"]:
        if spec["fold"] != stage:
            continue
        if stage != "screen" and not (root / "selection.json").exists():
            raise ResearchError("e5_selection_not_frozen")
        # If tuning loses, the unchanged fresh model is already the exact comparator.
        if (
            spec["method"] == "tuned"
            and read(root / "selection.json")["direction"]["method"] == "unchanged"
        ):
            continue
        if (
            stage == "final"
            and spec["task"] == "persistence"
            and not read(root / "selection.json")["persistence_policy_enabled"]
        ):
            continue
        receipt = run_trial(root, spec, features, outcomes)
        if receipt["status"] == "failed":
            raise ResearchError("e5_fit_failed_preserved_stop_for_diagnosis")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["freeze", "prepare", "screen", "spring", "summer", "final"]
    )
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "freeze":
        freeze(root)
    elif args.command == "prepare":
        prepare(root)
    else:
        execute(root, args.command)


if __name__ == "__main__":
    main()
