"""Finite E3 execution. All fits are local, sequential and explicitly authorized."""

from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor

from .dataset import ResearchError, digest
from .e2_data import attach_outcomes, verify_pin, verify_training_pointer
from .e2_experiment import SETTINGS, FitLimit, ResourceSample, utc
from .e2_models import LogisticModel, catboost_frame, period_weights
from .e3_data import ADVANCE_FEATURES, SCENARIOS, E3Data, perturb
from .e3_methods import (
    amount_metrics,
    calibrate,
    monetary_target,
    next_price,
    select_policies,
    temperature_fit,
)
from .group_features import pin
from .training_contract import CATEGORIES, FEATURES, KEYS, atomic_json

FOLDS = [
    {"name": "validation", "start": "2022-01-01", "end": "2024-01-01", "cutoff": "2022-01-01"},
    *[
        {
            "name": f"evaluation_{year}",
            "start": f"{year}-01-01",
            "end": f"{year + 1}-01-01" if year < 2026 else "2026-10-26",
            "cutoff": f"{year}-01-01",
        }
        for year in (2024, 2025, 2026)
    ],
]
DIRECTION = [
    {
        "method": f"cb_{peer}_{info}",
        "algorithm": "catboost",
        "peer": peer,
        "advance": info == "advance",
    }
    for peer in ("package", "equal", "broad")
    for info in ("current", "advance")
] + [
    {
        "method": f"logistic_equal_{info}",
        "algorithm": "logistic",
        "peer": "equal",
        "advance": info == "advance",
    }
    for info in ("current", "advance")
]


def feature_names(
    spec: dict[str, Any], group: dict[str, Any], broad: dict[str, Any]
) -> tuple[list[str], list[str]]:
    features, categories = list(FEATURES), list(CATEGORIES)
    if spec["peer"] in {"equal", "broad"}:
        features += group["feature_names"]
        categories += group["categorical_names"]
    if spec["peer"] == "broad":
        features += broad["feature_names"]
        categories += broad["categorical_names"]
    if spec["advance"]:
        features += ADVANCE_FEATURES
        categories += ADVANCE_FEATURES
    return features, categories


def cached_id(fold: dict[str, Any], spec: dict[str, Any], task: str) -> str | None:
    if task == "direction":
        if spec["advance"] or spec["peer"] == "broad":
            return None
        method = ("catboost_" if spec["algorithm"] == "catboost" else "logistic_") + (
            "group" if spec["peer"] == "equal" else "package"
        )
        if fold["name"] == "validation":
            return "screen-3" if method == "catboost_group" else None
        return f"{fold['name']}-direction-{method}"
    if (
        fold["name"] != "validation"
        and not spec["advance"]
        and spec["formulation"] == "delta_floor"
    ):
        return f"{fold['name']}-magnitude-catboost_group_mae"
    return None


def freeze(root: Path, data_root: Path) -> None:
    if root.exists() and any(root.iterdir()):
        raise ResearchError("e3_freeze_requires_unused_run")
    root.mkdir(parents=True, exist_ok=True)
    current = json.loads((data_root / "research-e2/current.json").read_text())
    for item in current.values():
        if isinstance(item, dict) and {"path", "sha256", "size_bytes"} <= item.keys():
            verify_pin(Path(item["path"]), item)
    e2_root = Path(current["protocol"]["path"]).parent
    e2 = json.loads((e2_root / "protocol.json").read_text())
    bundle, _ = verify_training_pointer(data_root / "training/current.json")
    if digest(bundle) != e2["dataset"]["sha256"]:
        raise ResearchError("e3_e2_training_comparison_changed")
    paths = {
        "dataset": bundle,
        "groups": Path(e2["groups"]["path"]),
        "advance": root.parent / "advance-v2/manifest-model-v2.json",
        "broader": root.parent / "peers-v1/manifest.json",
        "e2_protocol": e2_root / "protocol.json",
        "e2_ledger": e2_root / "trial-ledger.json",
        "e2_display": Path(current["display_result"]["path"]),
    }
    group = json.loads(paths["groups"].read_text())
    broad = json.loads(paths["broader"].read_text())
    source = Path(__file__).parent
    repo = source.parents[2]
    files = [
        source / name
        for name in (
            "e3_data.py",
            "e3_methods.py",
            "e3_experiment.py",
            "e3_peers.py",
            "e2_data.py",
            "e2_models.py",
            "e2_metrics.py",
            "e2_experiment.py",
            "training_contract.py",
        )
    ]
    with zipfile.ZipFile(root / "producer.zip", "x", compression=zipfile.ZIP_DEFLATED) as z:
        for file in [
            *sorted((repo / "src").rglob("*.py")),
            *sorted((repo / "src").rglob("*.sql")),
            repo / "pyproject.toml",
            repo / "uv.lock",
        ]:
            z.write(file, file.relative_to(repo).as_posix())
    calendar_path = bundle.parent / "calendar.json"
    calendar = json.loads(calendar_path.read_text())["periods"]
    protocol = {
        "version": "bounded-e3-v1",
        "frozen_utc": utc(),
        "pins": {k: pin(v) for k, v in paths.items()},
        "producer": pin(root / "producer.zip"),
        "code": {x.name: digest(x) for x in files},
        "dependencies": {k: importlib.metadata.version(k) for k in e2["dependencies"]},
        "authorization": (
            "Bounded local E3 research; no "
            "scheduling/retraining/promotion/order/deployment/commit/push/upload/external"
            " AI/Cito access."
        ),
        "information_boundary": (
            "At origin T: earlier/current AIP and ABC plus separately named historical "
            "final T+1 ABC proxy. No T+1 price or T+2 ABC/future "
            "membership/attributes/existence status. All candidates originate in R1 "
            "features; labels separate. Proxy assumes early report equals final "
            "classification and establishes neither availability nor Cito accuracy."
        ),
        "missingness": (
            "Final-code absence can reveal later source coverage/disappearance not known "
            "early. Explicit missing states, full candidate retention and prediction-only"
            " missing-code scenarios; do not interpret proxy absence as operationally "
            "established."
        ),
        "folds": FOLDS,
        "exact_origins": {
            f["name"]: [x for x in calendar if f["start"] <= x["start"] < f["end"]] for f in FOLDS
        },
        "history_years": 3,
        "maturity": (
            "Observed next labels strictly mature before fitting/calibration cutoff; "
            "validation labels strictly before 2024-01-01. Feature history remains causal"
            " full within episode."
        ),
        "exposure": (
            "E2's 2022-2026 scores/history already inspected; all E3 history "
            "retrospective development evidence, never a newly untouched holdout."
        ),
        "direction": [
            {
                **s,
                "features": feature_names(s, group, broad)[0],
                "categories": feature_names(s, group, broad)[1],
            }
            for s in DIRECTION
        ],
        "baselines": (
            "Exact compatible E2 current-ABC package/equal/logistic predictions/models "
            "and no-change/frequency/repeat-last references. Additional one-hot A/B->C "
            "rise,C->A fall,otherwise unchanged transition reference, missing fallback "
            "no-change; evaluated hypothesis only."
        ),
        "monetary": {
            "formulations": ["delta_floor", "log_ratio"],
            "feature_set": "equal",
            "selection": (
                "Choose formulation with smallest mean of current/advance equal-period "
                "DKK MAE on 2023 validation; tie delta_floor. Two formulations x two "
                "information sets on validation; selected formulation x two x three "
                "annual folds. Delta floor current annual reuses exact compatible E2 raw "
                "forecasts under newly declared nonnegative constraint. No retrospective "
                "alteration of E2."
            ),
            "support": (
                "delta_floor=max(0,current+raw delta); log_ratio trained "
                "log(next/current), bound raw prediction to training-only min/max then "
                "current*exp. Derived delta always next-current. Both MAE loss "
                "conditional medians, not expected gains."
            ),
            "interval": (
                "90% earlier held-out absolute-residual radius in fixed current-price "
                "bins <100,100-1000,>=1000 DKK; min100 observations else pooled. 2023 "
                "calibrates 2024,2024 calibrates 2025,2025 calibrates2026. Empirical "
                "temporal coverage only."
            ),
        },
        "scenarios": {k: list(v) for k, v in SCENARIOS.items()},
        "scenario_training": (
            "All training perfect conditional proxy; perturb prediction only, same "
            "seed/keys across methods; wrong code cyclic A->B->C->A. group_wrong20 "
            "correlated on origin-period/current LMS04 group, standalone key if missing. "
            "True outcomes never change. Synthetic rates do not estimate report accuracy."
        ),
        "calibration": (
            "Four scalar temperature fits for equal-peer advance: 2022 heldout->2023 "
            "policy validation;2023->2024;2024->2025;2025->2026. Mature-only "
            "period-weighted logloss, T bounds0.5..3. Raw/calibrated direction scores "
            "both reported. No calibration selection on evaluated outcomes."
        ),
        "queues": {
            "reference": "original top ceil10% rise ranking, seeded SHA256 tie",
            "candidate": "calibrated equal-peer advance, fixed before evaluation",
            "policies": (
                "risk_rank p(rise)-lambda*p(fall),lambda0.25/0.5/1,top10%; threshold "
                "p(rise)>=0.15/0.25/0.35 and p(fall)<=0.1/0.2,max10%"
            ),
            "selection": (
                "2023 only maximize observed rise fraction minus fall fraction; neither "
                "expected profit nor monetary score. Useful minimum >=2% overall and >=1%"
                " in >=80% periods before selection; if no feasible grid preserve failed "
                "policy. No replacement for missing outcomes."
            ),
            "comparison": (
                "natural workload plus per-origin matched count rise ranking; original E2"
                " group and raw E3 rise reference; selections saved before outcome "
                "attachment; Jaccard and changed package counts; tradeoff all fixed grid "
                "values labelled exploratory evaluated curves, no new selection."
            ),
        },
        "fallback": (
            "Fixed origin routing: equal peer_count>=1 uses equal model else package; "
            "broad count>=1 and supported complete/partial state uses broad model, "
            "else equal peer_count>=1 uses equal model, else package model. "
            "Both current and advance; all candidates plus prespecified matched/subgroup "
            "evaluations; never outcome-based routing."
        ),
        "metrics": (
            "Primary equal-period 3-class Brier; pooled Brier,ECE10 "
            "bins,accuracy,ranking/rise/fall/unknown/coverage. Money "
            "MAE,bias,relative-current/actual error,p95/p99/max,top1% error share,invalid"
            " count,90% interval coverage/width; retain every observed extreme. Queue "
            "counts/rates,empty periods,observed rise/fall,unknown,matched count and "
            "selected differences."
        ),
        "subgroups": (
            "equal peers>=1,zero peers,unsupported; broad supported,different-size "
            "peers,matched equal+broader supported; current AIP<100/100-1000/>=1000; "
            "history<6/established; annual periods. No selection within evaluation "
            "subgroups."
        ),
        "uncertainty": (
            "Paired circular temporal-block bootstrap length4 and sensitivity8,2000 "
            "repetitions,seed20261026,95% percentile; equal-period differences and queue "
            "rates. Retrospective unadjusted exploratory intervals; not independent "
            "package samples."
        ),
        "fit_weights": (
            "equal total weight each training period; logistic transforms/levels fitted "
            "training only; CatBoost train quantization/categories only"
        ),
        "settings": SETTINGS,
        "budget": {
            "direction_new_fits": 22,
            "monetary_validation_fits": 4,
            "monetary_annual_max_fits": 6,
            "maximum_main_fits": 32,
            "conditional_exact_count": (
                "32 if log_ratio selected;29 if delta_floor selected because3 current "
                "annual predictions reuse E2"
            ),
            "temperature_fits": 4,
            "residual_calibrations": 6,
            "fit_seconds": 1500,
            "rss_abort_bytes": 28 * 1024**3,
            "overall_seconds": 18000,
            "threads": 6,
            "disk_budget_bytes": 12 * 1024**3,
        },
        "stopping": (
            "Finish this fixed matrix and saved "
            "diagnostics/review/replay/dashboard/cleanup. Retain failed attempts; no "
            "silent retry/new configuration or expanding-history search. Resource "
            "failures have no quality score."
        ),
    }
    atomic_json(root / "protocol.json", protocol)
    atomic_json(
        root / "freeze-receipt.json",
        {
            "protocol": pin(root / "protocol.json"),
            "comparative_e3_scores_inspected": False,
            "utc": utc(),
        },
    )


def read_prediction(
    root: Path, fold: str, method: str, scenario: str = "perfect", *, outcomes: bool = False
) -> pd.DataFrame:
    if scenario not in SCENARIOS:
        raise ResearchError("e3_unknown_saved_scenario")
    cohort_manifest = json.loads((root / "cohorts" / fold / "manifest.json").read_text())
    for role in ("features", "outcomes") if outcomes else ("features",):
        verify_pin(root / "cohorts" / fold / f"{role}.parquet", cohort_manifest[role])
    cohort = pd.read_parquet(root / "cohorts" / fold / "features.parquet")
    trial = root / "trials" / f"{fold}-{method}"
    receipt = json.loads((trial / "trial.json").read_text())
    if receipt["status"] != "completed":
        raise ResearchError("e3_failed_trial_has_no_predictions")
    for file in receipt["files"]:
        verify_pin(trial / file["path"], file)
    predictions = pd.read_parquet(trial / "predictions.parquet")
    if not predictions.row_key.equals(cohort.row_key):
        raise ResearchError("e3_saved_prediction_alignment_changed")
    for column in ("p0", "p1", "p2", "next_aip_dkk"):
        source = f"{column}_{scenario}"
        if source not in predictions and scenario != "perfect" and not receipt["spec"]["advance"]:
            source = f"{column}_perfect"
        if source in predictions:
            cohort[column] = predictions[source].to_numpy()
        elif column in (["next_aip_dkk"] if receipt["task"] == "magnitude" else ["p0", "p1", "p2"]):
            raise ResearchError("e3_saved_scenario_missing")
    if "next_aip_dkk" in cohort:
        cohort["prediction_dkk"] = cohort.next_aip_dkk - cohort.aip_ore / 100
    if outcomes:
        cohort = attach_outcomes(
            cohort, pd.read_parquet(root / "cohorts" / fold / "outcomes.parquet")
        )
    return cohort


def same_values(left: pd.Series, right: pd.Series) -> bool:
    """Compare exact values/order while permitting Parquet timestamp resolution."""
    if len(left) != len(right):
        return False
    a, b = left.reset_index(drop=True), right.reset_index(drop=True)
    return bool(((a == b).fillna(False) | (a.isna() & b.isna())).all())


def same_frame(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    return left.columns.tolist() == right.columns.tolist() and all(
        same_values(left[name], right[name]) for name in left
    )


def trial_run(
    data: E3Data,
    protocol: dict[str, Any],
    root: Path,
    fold: dict[str, Any],
    spec: dict[str, Any],
    *,
    task: str,
) -> dict[str, Any]:
    trial_id = f"{fold['name']}-{spec['method']}"
    path = root / "trials" / trial_id
    if (path / "trial.json").exists():
        previous = json.loads((path / "trial.json").read_text())
        if (
            previous["protocol_sha256"] != digest(root / "protocol.json")
            or previous["status"] == "running"
        ):
            raise ResearchError("e3_unsafe_trial_resume")
        for p in previous.get("files", []):
            verify_pin(path / p["path"], p)
        return previous
    path.mkdir(parents=True)
    resource = ResourceSample()
    receipt: dict[str, Any] = {
        "trial_id": trial_id,
        "method": spec["method"],
        "task": task,
        "fold": fold["name"],
        "spec": spec,
        "fit_cutoff": fold["cutoff"],
        "training_start": f"{int(fold['cutoff'][:4]) - 3}-01-01",
        "protocol_sha256": digest(root / "protocol.json"),
        "started_utc": utc(),
        "status": "running",
        "new_fit": False,
    }
    atomic_json(path / "trial.json", receipt)
    try:
        candidates = data.candidates(fold["start"], fold["end"])
        output = candidates[["row_key"]].copy()
        source_id = cached_id(fold, spec, task)
        if source_id:
            old_root = Path(protocol["pins"]["e2_protocol"]["path"]).parent
            prior = next(
                x
                for x in json.loads((old_root / "trial-ledger.json").read_text())["trials"]
                if x["trial_id"] == source_id
            )
            if (
                prior["status"] != "completed"
                or prior["history"] != 3
                or prior["fit_cutoff"] != fold["cutoff"]
            ):
                raise ResearchError("e3_cached_fit_contract_mismatch")
            old_file = old_root / "trials" / source_id / "predictions.parquet"
            p = next(x for x in prior["files"] if x["role"] == "predictions")
            verify_pin(old_file, p)
            cached = pd.read_parquet(old_file)
            for key in [*KEYS, "aip_ore"]:
                if not same_values(cached[key], candidates[key]):
                    raise ResearchError("e3_cached_candidate_contract_mismatch")
            receipt.update(
                {
                    "reuse": pin(old_file),
                    "source_trial": source_id,
                    "training_rows": prior["training_rows"],
                    "latest_training_label_maturity": prior["latest_training_label_maturity"],
                }
            )
            if task == "direction":
                output[["p0_perfect", "p1_perfect", "p2_perfect"]] = cached[["p0", "p1", "p2"]]
            else:
                output["next_aip_dkk_perfect"] = next_price(
                    cached.prediction_dkk.to_numpy(),
                    candidates.aip_ore.to_numpy() / 100,
                    {"formulation": "delta_floor"},
                )
                receipt["constraint_applied"] = (
                    "Predeclared E3 delta_floor transform; E2 forecast untouched"
                )
        else:
            training = data.training(receipt["training_start"], fold["cutoff"])
            if training.next_maturity_date.max() >= pd.Timestamp(fold["cutoff"]):
                raise ResearchError("e3_immature_training_labels")
            receipt.update(
                {
                    "new_fit": True,
                    "training_rows": len(training),
                    "training_periods": int(training.period_start.nunique()),
                    "latest_training_label_maturity": str(training.next_maturity_date.max())[:10],
                }
            )
            features, categories = spec["features"], spec["categories"]
            y = training.next_direction.to_numpy(dtype=int) + 1
            limit = FitLimit(time.monotonic())
            model: Any
            if spec["algorithm"] == "logistic":
                model = LogisticModel(features, categories).fit(training, y)
                atomic_json(path / "model.json", model.state())
                receipt["convergence"] = model.convergence
                saved_state = json.loads((path / "model.json").read_text())
                restored: Any = LogisticModel(
                    saved_state["features"], saved_state["categories"], saved_state["penalty"]
                )
                restored.transforms = saved_state["transforms"]
                restored.coefficients = np.asarray(saved_state["coefficients"])
            else:
                x = catboost_frame(training, features, categories)
                if task == "magnitude":
                    y, support = monetary_target(training, spec["formulation"])
                    atomic_json(path / "support.json", support)
                    model = CatBoostRegressor(**protocol["settings"], loss_function="MAE")
                else:
                    model = CatBoostClassifier(
                        **protocol["settings"], loss_function="MultiClass", classes_count=3
                    )
                model.fit(
                    x,
                    y,
                    cat_features=categories,
                    sample_weight=period_weights(training),
                    callbacks=[limit],
                )
                if limit.exceeded:
                    raise ResearchError("e3_declared_fit_resource_limit")
                model.save_model(str(path / "model.cbm"))
                restored = CatBoostRegressor() if task == "magnitude" else CatBoostClassifier()
                restored.load_model(str(path / "model.cbm"))
                receipt["trees"] = model.tree_count_
                del x
            del training
            gc.collect()
            for scenario in SCENARIOS if spec["advance"] else ["perfect"]:
                frame = perturb(candidates, scenario)
                if spec["algorithm"] == "logistic":
                    predicted = model.predict_proba(frame)
                    check = restored.predict_proba(frame) if scenario == "perfect" else predicted
                else:
                    xp = catboost_frame(frame, features, categories)
                    predicted = (
                        model.predict(xp, thread_count=6)
                        if task == "magnitude"
                        else model.predict_proba(xp, thread_count=6)
                    )
                    check = (
                        (
                            restored.predict(xp, thread_count=6)
                            if task == "magnitude"
                            else restored.predict_proba(xp, thread_count=6)
                        )
                        if scenario == "perfect"
                        else predicted
                    )
                    del xp
                if not np.array_equal(predicted, check):
                    raise ResearchError("e3_model_reload_changed_predictions")
                if task == "magnitude":
                    output[f"next_aip_dkk_{scenario}"] = next_price(
                        predicted, candidates.aip_ore.to_numpy(dtype=float) / 100, support
                    )
                else:
                    if (
                        not np.isfinite(predicted).all()
                        or (predicted < 0).any()
                        or not np.allclose(predicted.sum(axis=1), 1)
                    ):
                        raise ResearchError("e3_invalid_direction_probabilities")
                    for k in range(3):
                        output[f"p{k}_{scenario}"] = predicted[:, k]
            del model, restored
        output.to_parquet(path / "predictions.parquet", index=False)
        receipt.update(
            {
                "status": "completed",
                "candidate_rows": len(candidates),
                "predictions_frozen_utc": utc(),
                "files": [
                    {**pin(x), "path": x.name}
                    for x in sorted(path.iterdir())
                    if x.name != "trial.json"
                ],
            }
        )
        del candidates, output
    except Exception as exc:
        receipt.update(
            {
                "status": "failed",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
            }
        )
    receipt["resource"] = resource.finish()
    receipt["finished_utc"] = utc()
    atomic_json(path / "trial.json", receipt)
    gc.collect()
    print(
        json.dumps(
            {k: receipt.get(k) for k in ("trial_id", "status", "new_fit", "resource", "error")}
        ),
        flush=True,
    )
    return receipt


def choose(root: Path) -> dict[str, Any]:
    validation = read_prediction(root, "validation", "cb_equal_advance", outcomes=True)
    state = temperature_fit(
        validation.loc[validation.period_start < pd.Timestamp("2023-01-01")], "2023-01-01"
    )
    validation[["p0", "p1", "p2"]] = calibrate(validation[["p0", "p1", "p2"]].to_numpy(), state)
    policies = select_policies(validation)
    amounts = []
    for formulation in ("delta_floor", "log_ratio"):
        scores = []
        for info in ("current", "advance"):
            value = read_prediction(
                root, "validation", f"amount_{formulation}_{info}", outcomes=True
            )
            value = value.loc[value.period_start >= pd.Timestamp("2023-01-01")]
            per = [
                amount_metrics(part)["mae_dkk"]
                for _, part in value.groupby("period_start")
                if part.next_status.eq("observed").any()
            ]
            scores.append(float(np.mean(per)))
        amounts.append(
            {
                "formulation": formulation,
                "current_mae": scores[0],
                "advance_mae": scores[1],
                "selection_mae": float(np.mean(scores)),
            }
        )
    selected = min(amounts, key=lambda x: x["selection_mae"])["formulation"]
    result = {
        "utc": utc(),
        "protocol_sha256": digest(root / "protocol.json"),
        "selected_before_evaluation": True,
        "monetary_formulation": selected,
        "monetary_validation": amounts,
        "queue_policies": policies,
        "calibration_2023": state,
        "main_fit_count": 29 if selected == "delta_floor" else 32,
    }
    atomic_json(root / "selection.json", result)
    atomic_json(
        root / "selection-receipt.json",
        {
            "selection": pin(root / "selection.json"),
            "protocol_sha256": digest(root / "protocol.json"),
        },
    )
    return result


def run(root: Path) -> None:
    overall = ResourceSample()
    protocol = json.loads((root / "protocol.json").read_text())
    verify_pin(
        root / "protocol.json", json.loads((root / "freeze-receipt.json").read_text())["protocol"]
    )
    for name, sha in protocol["code"].items():
        if digest(Path(__file__).parent / name) != sha:
            raise ResearchError("e3_producer_changed_after_freeze")
    for p in protocol["pins"].values():
        verify_pin(Path(p["path"]), p)
    data = E3Data(
        *[Path(protocol["pins"][x]["path"]) for x in ("dataset", "groups", "advance", "broader")],
        scratch=root.parent / "scratch",
    )
    print(json.dumps({"stage": "verified_e3_features_ready", "utc": utc()}), flush=True)
    trials = []
    selection = (
        json.loads((root / "selection.json").read_text())
        if (root / "selection.json").exists()
        else None
    )
    if selection is not None:
        verify_pin(
            root / "selection.json",
            json.loads((root / "selection-receipt.json").read_text())["selection"],
        )
        if (
            selection["protocol_sha256"] != digest(root / "protocol.json")
            or not selection["selected_before_evaluation"]
        ):
            raise ResearchError("e3_selection_binding_changed")
    for fold in FOLDS:
        path = root / "cohorts" / fold["name"]
        path.mkdir(parents=True, exist_ok=True)
        candidates = data.candidates(fold["start"], fold["end"])
        if not (path / "features.parquet").exists():
            candidates.to_parquet(path / "features.parquet", index=False)
        else:
            saved = pd.read_parquet(path / "features.parquet")
            if not same_frame(candidates, saved):
                raise ResearchError("e3_resumed_features_changed")
        del candidates
        for spec in protocol["direction"]:
            if time.monotonic() - overall.started > protocol["budget"]["overall_seconds"]:
                raise ResearchError("e3_overall_execution_limit")
            trials.append(trial_run(data, protocol, root, fold, spec, task="direction"))
            atomic_json(root / "trial-ledger.json", {"trials": trials})
        if fold["name"] != "validation" and selection is None:
            raise ResearchError("e3_evaluation_requires_selection")
        formulations = (
            ["delta_floor", "log_ratio"]
            if fold["name"] == "validation"
            else [selection["monetary_formulation"]]
            if selection is not None
            else []
        )
        for formulation in formulations:
            for info in ("current", "advance"):
                if time.monotonic() - overall.started > protocol["budget"]["overall_seconds"]:
                    raise ResearchError("e3_overall_execution_limit")
                base = next(s for s in protocol["direction"] if s["method"] == f"cb_equal_{info}")
                spec = {
                    **base,
                    "method": f"amount_{formulation}_{info}",
                    "formulation": formulation,
                }
                trials.append(trial_run(data, protocol, root, fold, spec, task="magnitude"))
                atomic_json(root / "trial-ledger.json", {"trials": trials})
        # First outcomes access for this scored origin cohort follows frozen predictions.
        labels = data.outcomes(fold["start"], fold["end"])
        if fold["name"] == "validation":
            late = labels.next_maturity_date.isna() | (
                labels.next_maturity_date >= pd.Timestamp(fold["end"])
            )
            labels.loc[late, "next_status"] = "pending_validation_cutoff"
            labels.loc[late, ["next_aip_ore", "next_delta_ore", "next_direction"]] = np.nan
        if not (path / "outcomes.parquet").exists():
            labels.to_parquet(path / "outcomes.parquet", index=False)
            atomic_json(
                path / "manifest.json",
                {
                    "features": pin(path / "features.parquet"),
                    "outcomes": pin(path / "outcomes.parquet"),
                    "outcomes_attached_utc": utc(),
                },
            )
        else:
            cohort_manifest = json.loads((path / "manifest.json").read_text())
            for role in ("features", "outcomes"):
                verify_pin(path / f"{role}.parquet", cohort_manifest[role])
            if not same_frame(labels, pd.read_parquet(path / "outcomes.parquet")):
                raise ResearchError("e3_resumed_outcomes_changed")
        if fold["name"] == "validation":
            if any(t["status"] != "completed" for t in trials):
                raise ResearchError("e3_validation_failure_selection_unavailable")
            if selection is None:
                if time.monotonic() - overall.started > protocol["budget"]["overall_seconds"]:
                    raise ResearchError("e3_overall_execution_limit")
                selection = choose(root)
            print(
                json.dumps(
                    {
                        "stage": "validation_selection_frozen",
                        "formulation": selection["monetary_formulation"],
                        "main_fit_count": selection["main_fit_count"],
                    }
                ),
                flush=True,
            )
    data.close()
    atomic_json(
        root / "execution.json",
        {
            "status": "completed",
            "resource": overall.finish(),
            "trials": len(trials),
            "new_main_fits": sum(t["new_fit"] for t in trials),
            "failures": [t["trial_id"] for t in trials if t["status"] != "completed"],
            "limits": protocol["budget"],
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["freeze", "run"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path)
    args = parser.parse_args()
    if args.command == "freeze":
        if args.data_root is None:
            parser.error("--data-root required to freeze")
        freeze(args.root, args.data_root)
    else:
        run(args.root)
