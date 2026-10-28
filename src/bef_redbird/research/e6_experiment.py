"""Sequential E6 fits on immutable research cohorts; never opens the live store."""

from __future__ import annotations

import argparse
import gc
import hashlib
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
from .e2_experiment import ResourceSample
from .e2_models import catboost_frame, period_weights
from .e3_data import E3Data
from .e4_common import checked, frozen_json, identity, read, utc
from .e5_experiment import Limit, predict, spec_features
from .e6_protocol import FOLDS, require
from .group_features import pin
from .training_contract import KEYS, atomic_json

LABELS = [
    *KEYS,
    "next_status",
    "next_maturity_date",
    "next_direction",
    "next_delta_ore",
    "next_aip_ore",
]


def fingerprint(frame: pd.DataFrame) -> str:
    return hashlib.sha256(
        pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes()
    ).hexdigest()


def prepare(root: Path) -> dict[str, Any]:
    protocol = require(root)
    directory = root / "inputs"
    directory.mkdir(exist_ok=True)
    target = directory / "manifest.json"
    if target.exists():
        manifest = read(target)
        for part in manifest["partitions"]:
            checked(part["features"])
            checked(part["outcomes"])
        return manifest
    sample = ResourceSample()
    paths = {k: checked(protocol["pins"][k]) for k in ("dataset", "groups", "advance", "broader")}
    verify_training_manifest(paths["dataset"])
    data = E3Data(
        **{k if k != "dataset" else "bundle": v for k, v in paths.items()}, scratch=root / "scratch"
    )
    coverage = data.connection.execute("""
        SELECT year(p.period_start) AS year, count(*) AS available_rows,
          sum(p.eligible::INT) AS eligible_rows,
          sum((p.aip_ore > 0)::INT) AS positive_price_rows,
          sum((p.abc IS NOT NULL)::INT) AS current_abc_rows,
          sum((p.advance_abc IS NOT NULL)::INT) AS advance_abc_rows,
          sum((p.peer_count >= 1)::INT) AS equal_peer_rows,
          sum((p.broad_peer_count >= 1)::INT) AS broad_peer_rows,
          min(p.period_start)::VARCHAR AS oldest_origin,
          max(p.period_start)::VARCHAR AS latest_origin,
          sum((l.next_status='observed' AND p.eligible)::INT) AS observed_eligible_labels,
          count(DISTINCT p.package_number) AS packages
        FROM predictors p LEFT JOIN labels l USING(row_key)
        GROUP BY year(p.period_start) ORDER BY year
    """).fetchdf()
    parts = []
    e5_inputs = read(checked(protocol["pins"]["e5_inputs_manifest"]))
    e5_features = pd.read_parquet(checked(e5_inputs["files"]["features"]))
    for year in range(2008, 2027):
        part_path = directory / f"{year}.json"
        if part_path.exists():
            part = read(part_path)
            checked(part["features"])
            checked(part["outcomes"])
            parts.append(part)
            continue
        features = data.candidates(f"{year}-01-01", f"{year + 1}-01-01")
        labels = data.outcomes(f"{year}-01-01", f"{year + 1}-01-01")
        labels = labels.loc[labels.row_key.isin(features.row_key), LABELS].reset_index(drop=True)
        if not labels.row_key.equals(features.row_key):
            raise ResearchError("e6_preparation_key_alignment")
        if year >= 2022:
            prior = e5_features.loc[e5_features.period_start.dt.year.eq(year)].reset_index(
                drop=True
            )
            pd.testing.assert_frame_equal(features[prior.columns], prior, check_dtype=False)
        features.to_parquet(directory / f"{year}-features.parquet", index=False)
        labels.to_parquet(directory / f"{year}-outcomes.parquet", index=False)
        part = {
            "year": year,
            "rows": len(features),
            "features": pin(directory / f"{year}-features.parquet"),
            "outcomes": pin(directory / f"{year}-outcomes.parquet"),
            "keys_sha256": fingerprint(features[list(KEYS)]),
            "aligned_e5_exact_values": year >= 2022,
        }
        frozen_json(part_path, part)
        parts.append(part)
        del features, labels
        gc.collect()
        print(json.dumps({"prepared_year": year, "rows": part["rows"]}), flush=True)
    data.close()
    del data, e5_features
    manifest = {
        "version": "e6-frozen-cohorts-v1",
        "protocol": pin(root / "protocol.json"),
        "partitions": parts,
        "rows": sum(p["rows"] for p in parts),
        "source_rows": int(coverage.available_rows.sum()),
        "coverage": json.loads(coverage.to_json(orient="records")),
        "limitations": [
            "Advance sidecar absent before 2019, left joined as missing.",
            "Identity eligibility and peer support follow accepted era-specific source contracts.",
            "No historical present-day-survival filter; disappeared packages remain.",
            "Unsupported supply features remain unavailable; final ABC is conditional proxy.",
        ],
        "resource": sample.finish(),
        "created_utc": utc(),
    }
    frozen_json(target, manifest)
    return manifest


def load_window(
    root: Path, start: str, end: str, columns: list[str] | None = None, *, outcomes: bool = False
) -> pd.DataFrame:
    manifest = read(root / "inputs/manifest.json")
    frames = []
    for part in manifest["partitions"]:
        if int(start[:4]) <= part["year"] <= int(end[:4]):
            path = checked(part["outcomes" if outcomes else "features"])
            frame = pd.read_parquet(path, columns=columns)
            frames.append(frame.loc[frame.period_start.ge(start) & frame.period_start.lt(end)])
    return pd.concat(frames, ignore_index=True)


def training_cohort(root: Path, spec: dict[str, Any], names: list[str]) -> pd.DataFrame:
    fold = next(f for f in FOLDS if f["name"] == spec["fold"])
    cutoff = pd.Timestamp(fold["fit"])
    start = (
        str(cutoff - pd.DateOffset(years=int(spec["history"][0])))[:10]
        if spec["history"] in ("3y", "6y")
        else "2008-01-01"
    )
    columns = list(dict.fromkeys([*KEYS, *names]))
    features = load_window(root, start, fold["fit"], columns)
    outcomes = load_window(root, start, fold["fit"], LABELS, outcomes=True)
    frame = attach_outcomes(features, outcomes)
    frame = frame.loc[frame.next_status.eq("observed") & frame.next_maturity_date.lt(cutoff)]
    return frame.reset_index(drop=True)


def training_weights(
    frame: pd.DataFrame, cutoff: str, weighted: bool
) -> tuple[np.ndarray, dict[str, Any]]:
    weights = period_weights(frame)
    age = (pd.Timestamp(cutoff) - frame.period_start).dt.days.to_numpy(dtype=float)
    if weighted:
        weights *= np.exp2(-age / (3 * 365.25))
        weights /= weights.mean()
    return weights, {
        "mean": float(weights.mean()),
        "sum": float(weights.sum()),
        "effective_rows": float(weights.sum() ** 2 / np.square(weights).sum()),
        "weight_share_last3years": float(weights[age <= 3 * 365.25].sum() / weights.sum()),
        "weighted_mean_age_years": float(np.average(age / 365.25, weights=weights)),
        "age_from": cutoff,
        "half_life_years": 3 if weighted else None,
    }


def ledger(root: Path) -> dict[str, Any]:
    paths = [
        *sorted((root / "trials").glob("*/receipt.json")),
        *sorted((root / "attempts").glob("*/receipt.json")),
    ]
    trials = [read(p) for p in paths]
    return {
        "version": "e6-fit-ledger-v1",
        "trials": trials,
        "attempts": sum(t["status"] != "skipped" for t in trials),
        "completed": sum(t["status"] == "completed" for t in trials),
        "failed": sum(t["status"] == "failed" for t in trials),
        "skipped": sum(t["status"] == "skipped" for t in trials),
        "fit_seconds": sum(t.get("fit_seconds", 0) for t in trials),
        "elapsed_seconds": sum(t.get("resource", {}).get("elapsed_seconds", 0) for t in trials),
    }


def producer(root: Path) -> dict[str, Any]:
    source = Path(__file__).parent
    repo = source.parents[2]
    names = [
        "e6_experiment.py",
        "e6_protocol.py",
        "e5_experiment.py",
        "e2_models.py",
        "e3_data.py",
        "e2_data.py",
        "training_contract.py",
    ]
    code = [pin(source / name) for name in names]
    # Technical fixes get another immutable producer; earlier attempts retain theirs.
    version = identity(code)[:16]
    receipt = root / f"fit-producer-{version}.json"
    if receipt.exists():
        prior = read(receipt)
        checked(prior["archive"])
        if prior["code"] != code:
            raise ResearchError("e6_producer_identity_collision")
        return prior
    archive = root / f"producer-fit-{version}.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as z:
        for path in (repo / "src").rglob("*.py"):
            z.write(path, path.relative_to(repo).as_posix())
        for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
            z.write(repo / name, name)
    result = {
        "archive": pin(archive),
        "code": code,
        "created_utc": utc(),
    }
    frozen_json(receipt, result)
    return result


def run_trial(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    protocol = require(root)
    folder = root / "trials" / spec["id"]
    path = folder / "receipt.json"
    if path.exists():
        prior = read(path)
        if prior["status"] == "running":
            raise ResearchError("e6_interrupted_attempt_requires_failure_receipt")
        for p in prior.get("files", {}).values():
            checked(p)
        return prior
    limits = protocol["resource_limits"]
    previous = ledger(root)
    remaining = limits["aggregate_fit_seconds"] - previous["fit_seconds"]
    full_times = [
        t.get("resource", {}).get("elapsed_seconds", limits["full_fit_seconds"])
        for t in previous["trials"]
        if t["spec"]["history"] in {"full", "weighted"} and t["spec"]["task"] == "direction"
    ]
    needed = 2 * max(full_times, default=7200) + 1800 if spec["optional"] else 60
    if remaining < needed:
        folder.mkdir(parents=True)
        result = {
            "spec": spec,
            "status": "skipped",
            "reason": "frozen aggregate budget priority",
            "remaining_fit_seconds": remaining,
            "created_utc": utc(),
        }
        frozen_json(path, result)
        return result
    if previous["attempts"] >= limits["base_fit_attempts"]:
        raise ResearchError("e6_attempt_limit")
    if shutil.disk_usage(root).free < limits["free_disk_reserve_bytes"]:
        raise ResearchError("e6_disk_reserve")
    folder.mkdir(parents=True)
    sample = ResourceSample()
    receipt: dict[str, Any] = {
        "spec": spec,
        "status": "running",
        "started_utc": utc(),
        "protocol": pin(root / "protocol.json"),
        "inputs": pin(root / "inputs/manifest.json"),
        "producer": producer(root),
    }
    atomic_json(path, receipt)
    try:
        fold = next(f for f in FOLDS if f["name"] == spec["fold"])
        names, cats = spec_features(protocol, spec["task"], spec["mode"])
        if spec["mode"] == "current" and any("advance" in n for n in names):
            raise ResearchError("e6_current_information_leak")
        training = training_cohort(root, spec, names)
        params = dict(protocol["settings"])
        params["loss_function"] = protocol["objectives"][spec["objective"]]
        if spec["task"] == "direction":
            params["classes_count"] = 3
        weights, weight_state = training_weights(
            training, fold["fit"], spec["history"] == "weighted"
        )
        receipt.update(
            training_rows=len(training),
            training_periods=int(training.period_start.nunique()),
            actual_oldest_origin=str(training.period_start.min())[:10],
            actual_latest_origin=str(training.period_start.max())[:10],
            latest_label_maturity=str(training.next_maturity_date.max())[:10],
            training_keys_sha256=fingerprint(training[list(KEYS)]),
            training_features_sha256=fingerprint(training[names]),
            params=params,
            features=names,
            categories=cats,
            weights=weight_state,
        )
        if training.empty or training.next_maturity_date.max() >= pd.Timestamp(fold["fit"]):
            raise ResearchError("e6_training_maturity")
        y = (
            training.next_direction.to_numpy(dtype=int) + 1
            if spec["task"] == "direction"
            else training.next_delta_ore.to_numpy(dtype=float) / 100
        )
        fit_pool = Pool(
            catboost_frame(training, names, cats),
            label=y,
            cat_features=cats,
            weight=weights,
            thread_count=6,
        )
        del training, weights, y
        gc.collect()
        if psutil.Process().memory_info().rss > limits["process_rss_bytes"]:
            raise ResearchError("e6_pool_memory_limit")
        model: Any = (
            CatBoostClassifier(**params)
            if spec["task"] == "direction"
            else CatBoostRegressor(**params)
        )
        allowance = (
            limits["full_fit_seconds"]
            if spec["history"] in ("full", "weighted")
            else limits["other_fit_seconds"]
        )
        callback = Limit(
            {
                "fit_seconds": min(allowance, remaining),
                "process_rss_bytes": limits["process_rss_bytes"],
            }
        )
        started = time.monotonic()
        try:
            model.fit(fit_pool, callbacks=[callback])
        finally:
            receipt["fit_seconds"] = time.monotonic() - started
        receipt["trees"] = model.tree_count_
        if callback.exceeded:
            raise ResearchError("e6_declared_fit_resource_limit")
        model.save_model(str(folder / "model.cbm"))
        receipt["captured_params"] = model.get_all_params()
        frozen_json(folder / "curves.json", model.get_evals_result())
        del fit_pool
        gc.collect()
        candidates = load_window(
            root, fold["cal_start"], fold["end"], list(dict.fromkeys([*KEYS, "aip_ore", *names]))
        )
        prediction_spec = {"task": spec["task"], "method": spec["objective"]}
        predicted = predict(model, candidates, prediction_spec, names, cats)
        restored: Any = CatBoostClassifier() if spec["task"] == "direction" else CatBoostRegressor()
        restored.load_model(str(folder / "model.cbm"))
        check = predict(restored, candidates, prediction_spec, names, cats)
        pd.testing.assert_frame_equal(predicted, check, check_exact=True)
        predicted.to_parquet(folder / "predictions.parquet", index=False)
        receipt.update(
            status="completed",
            exact_reload=True,
            candidate_rows=len(candidates),
            files={n: pin(folder / n) for n in ("model.cbm", "curves.json", "predictions.parquet")},
        )
        del model, restored, candidates, predicted, check
    except Exception as exc:
        receipt.update(status="failed", error=str(exc), traceback=traceback.format_exc())
    receipt["resource"] = sample.finish()
    if receipt["resource"]["sampled_peak_rss_bytes"] > limits["process_rss_bytes"]:
        receipt.update(status="failed", error="e6_total_process_memory_limit")
    receipt["finished_utc"] = utc()
    atomic_json(path, receipt)
    atomic_json(root / "fit-ledger.json", ledger(root))
    gc.collect()
    print(
        json.dumps(
            {
                "trial": spec["id"],
                "status": receipt["status"],
                "fit_seconds": receipt.get("fit_seconds"),
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
    for spec in protocol["matrix"]:
        if stage != "all" and stage not in (spec["investigation"], spec["id"]):
            continue
        result = run_trial(root, spec)
        resource_failure = any(
            token in result.get("error", "") for token in ("resource", "memory_limit")
        )
        if result["status"] == "failed" and not resource_failure:
            raise ResearchError("e6_technical_failure_preserved_stop_for_review")
    atomic_json(root / "fit-ledger.json", ledger(root))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", default="all")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.root.resolve())
    else:
        execute(args.root.resolve(), args.stage)
