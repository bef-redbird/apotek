"""Bounded, explicitly frozen E2 experiments; never scheduler or promotion code."""

from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
import threading
import time
import traceback
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psutil
from catboost import CatBoostClassifier, CatBoostRegressor

from .dataset import ResearchError, digest
from .e2_assessment import assess
from .e2_data import (
    OWN_FEATURES,
    ResearchData,
    attach_outcomes,
    verify_pin,
    verify_training_manifest,
    verify_training_pointer,
)
from .e2_metrics import calibration_rows, freeze_predictions, paired_interval, score_frame
from .e2_models import LogisticModel, catboost_frame, frequency_probabilities, period_weights
from .training_contract import CATEGORIES, FEATURES, atomic_json, pin

SEED = 20261026
SETTINGS = {
    "iterations": 160,
    "depth": 6,
    "learning_rate": 0.08,
    "l2_leaf_reg": 5.0,
    "random_seed": SEED,
    "thread_count": 6,
    "allow_writing_files": False,
    "verbose": False,
    "task_type": "CPU",
    "used_ram_limit": "20gb",
}


def utc() -> str:
    return datetime.now(UTC).isoformat()


class ResourceSample:
    """Sample total process memory, including loading, fitting and prediction."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.process = psutil.Process()
        self.initial = self.process.memory_info().rss
        self.peak = self.initial
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()

    def _sample(self) -> None:
        while not self.stop.wait(0.1):
            self.peak = max(self.peak, self.process.memory_info().rss)

    def finish(self) -> dict[str, Any]:
        self.stop.set()
        self.thread.join()
        self.peak = max(self.peak, self.process.memory_info().rss)
        return {
            "elapsed_seconds": time.monotonic() - self.started,
            "initial_rss_bytes": self.initial,
            "sampled_peak_rss_bytes": self.peak,
            "threads": 6,
            "memory_scope": (
                "whole process including loading, preprocessing, model fitting, "
                "prediction and saved-model readback; sampled every 100ms"
            ),
        }


class FitLimit:
    def __init__(self, started: float) -> None:
        self.started = started
        self.exceeded = False

    def after_iteration(self, info: Any) -> bool:
        self.exceeded = (
            time.monotonic() - self.started > 1500
            or psutil.Process().memory_info().rss > 28 * 1024**3
        )
        return not self.exceeded


def _reference(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": digest(path), "size_bytes": path.stat().st_size}


def freeze_protocol(pointer: Path, group: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if any(
        (output / name).exists() for name in ("protocol.json", "trials", "selection.json", "saved")
    ):
        raise ResearchError("e2_protocol_already_frozen")
    bundle, manifest = verify_training_pointer(pointer)
    data = ResearchData(bundle, group, scratch=output.parent / "scratch")
    assessment = assess(data)
    atomic_json(output / "development-assessment.json", assessment)
    group_manifest = json.loads(group.read_text(encoding="utf-8"))
    source = Path(__file__).parent
    code = {
        str(path.name): digest(path)
        for path in sorted(source.glob("e2_*.py"))
        if "dashboard" not in path.name and "result_view" not in path.name
    }
    repository = source.parents[2]
    with zipfile.ZipFile(output / "producer.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        producer_files = [
            *sorted((repository / "src").rglob("*.py")),
            *sorted((repository / "src").rglob("*.sql")),
            repository / "pyproject.toml",
            repository / "uv.lock",
        ]
        for file in producer_files:
            archive.write(file, file.relative_to(repository).as_posix())
    protocol = {
        "version": "historical-group-research-e2-v1",
        "frozen_utc": utc(),
        "authorization": (
            "Owner explicitly authorized this bounded local research on "
            "2026-10-26; no scheduling, promotion, ordering, deployment, "
            "commit, push or upload."
        ),
        "dataset": _reference(bundle),
        "groups": _reference(group),
        "development_assessment": _reference(output / "development-assessment.json"),
        "base_contract": manifest["contract"],
        "group_contract": group_manifest["contract"],
        "code": code,
        "producer": _reference(output / "producer.zip"),
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in (
                "numpy",
                "scipy",
                "pandas",
                "duckdb",
                "pyarrow",
                "catboost",
                "psutil",
                "threadpoolctl",
            )
        },
        "question": (
            "Do dated historical package attributes and supported substitution-"
            "peer relationships improve next-period AIP direction probabilities"
            " and candidate ranking?"
        ),
        "population": (
            "All eligible R1 origins at actual tariff dates, selected from "
            "features only. Same keys for every primary direction method. "
            "Unknown group features never exclude valid own-price origins."
        ),
        "cutoff": (
            "R1 assumed Wednesday second tariff week noon Europe/Copenhagen; "
            "source publication/possession historically unestablished, so "
            "reconstructed research only."
        ),
        "targets": {
            "direction": (
                "sign of exact next_delta_ore: decrease(-1), unchanged(0), "
                "increase(1); no tolerance"
            ),
            "magnitude": (
                "next_delta_ore/100 DKK per package, MAE loss and MAE score, "
                "conditional median forecast, not expected financial gain"
            ),
            "reversal": (
                "At an already observed rise, full/partial/persistence over both "
                "next actual tariff periods, exact R1 definitions and explicit "
                "unknown horizons"
            ),
        },
        "classes": {
            "direction": ["decrease", "unchanged", "increase"],
            "reversal": ["full_reversal", "partial_reversal", "persistence"],
        },
        "development": {
            "start": "2008-01-01",
            "end": "2022-01-01",
            "label_mature_before": "2022-01-01",
        },
        "validation": {
            "start": "2022-01-01",
            "end": "2024-01-01",
            "label_mature_before": "2024-01-01",
            "fit_cutoff": "2022-01-01",
        },
        "evaluation": [
            {
                "name": f"evaluation_{year}",
                "start": f"{year}-01-01",
                "end": f"{year + 1}-01-01" if year < 2026 else "2026-10-26",
                "fit_cutoff": f"{year}-01-01",
            }
            for year in (2024, 2025, 2026)
        ],
        "split_reason": (
            "Retains R1 development/validation/2024 evaluation boundaries; "
            "splits previously exposed E1 2025/2026 into successive fixed "
            "annual fits. No untouched holdout claim. No calendar extension."
        ),
        "history_choices": [1, 3, "expanding"],
        "history_changes": (
            "Fitting rows only; causal feature lookback remains all supported "
            "history inside the current episode for every alternative."
        ),
        "selection": (
            "Screen CatBoost package+group on validation only, first one-year "
            "trial is actual measured fitting canary. Choose shortest history "
            "within 0.001 equal-period multiclass Brier of best; expanding "
            "comes last. Freeze selection before evaluation. No second "
            "configuration or post-evaluation tuning. A measured resource-limit "
            "failure excludes that history from selection, retains its failure "
            "and capacity limit explicitly, and never counts as a shorter-history "
            "success. A non-resource failure stops comparison for correction."
        ),
        "features": {
            "own": list(OWN_FEATURES),
            "package": list(FEATURES),
            "group": [*FEATURES, *data.group_features],
        },
        "categories": {
            "own": [],
            "package": list(CATEGORIES),
            "group": [*CATEGORIES, *data.group_categories],
        },
        "preprocessing": (
            "CatBoost: injective missing category M:/native V:, numeric null "
            "NaN, learned quantization/encoding on fitting rows only. Logistic:"
            " signed log1p numeric, training median/mean/scale plus missing "
            "flags; top128 training categorical levels, separate unknown; "
            "L2=0.001, L-BFGS-B max150, intercept unpenalized. No IDs as "
            "predictors."
        ),
        "calibration": (
            "Identity/no learned recalibration. Reliability bins fixed at 0.1 "
            "intervals for each class. Calibration is measured, not assumed."
        ),
        "models": {
            "catboost": SETTINGS,
            "logistic": {"penalty": 0.001, "maximum_iterations": 150},
            "no_change": [0, 1, 0],
            "repeat_last": "one-hot sign of observed lag1; missing lag chooses unchanged",
            "historical_frequency": (
                "Laplace-smoothed 3-class frequencies on same selected fitting "
                "history, equal total weight per period"
            ),
            "magnitude_baselines": "zero change and weighted training median DKK change",
            "reversal_baselines": "all persistence and same-window historical frequencies",
        },
        "fits": {
            "development_descriptive_estimates": 2,
            "development_note": (
                "Two pooled linear probability association estimates use development only "
                "before protocol freeze; separate from the 27 predictive model trials."
            ),
            "screen": 3,
            "primary": 18,
            "secondary": 6,
            "maximum_learned_fits": 27,
            "optional_configurations": 0,
            "ordering": (
                "sequential only; 3 history screens then 3 folds x 3 feature sets x"
                " 2 learned classifiers, then each fold one MAE regressor and one "
                "reversal classifier"
            ),
            "failures": (
                "Every attempted fit retained with failure reason. Do not "
                "substitute shorter history for expanding or erase unsuccessful "
                "trials."
            ),
        },
        "resources": {
            "threads": 6,
            "catboost_advisory_ram": "20gb",
            "fit_abort_rss_bytes": 28 * 1024**3,
            "fit_seconds_limit": 1500,
            "overall_fit_budget_seconds": 27 * 1500,
            "note": (
                "CatBoost iteration callback and logistic objective enforce fit stop; "
                "logistic BLAS threads are limited to six. Total process RSS "
                "including loading/prediction is measured separately. No claim that"
                " used_ram_limit is an OS memory cap."
            ),
        },
        "weighting": (
            "Fit weights: equal total weight per tariff period. Primary score: "
            "mean of period multiclass Brier (sum of three squared probability "
            "errors, range0..2). Also package-row weighted score. No sales "
            "weights."
        ),
        "ranking": (
            "Freeze top ceil(10% of eligible candidates) per actual period by "
            "rise probability, seeded SHA256 row-key ties. Attach outcomes "
            "afterward; never replace unknown selected items. Report observed "
            "precision/fall fraction, selected unknown fraction, all-selected "
            "lower/upper rise bounds. Uniform random-selection expectation uses"
            " observed rise and unknown fractions, never package-ID advantage."
        ),
        "uncertainty": (
            "Paired model-minus-baseline equal-period differences, circular "
            "moving temporal blocks length4, 2000 resamples seed20261026, "
            "percentile95%; sensitivity length8. Do not treat package rows as "
            "independent experiments. Modest number of prespecified "
            "comparisons, exploratory without multiplicity-adjusted "
            "confirmatory claims."
        ),
        "secondary": (
            "Use selected history and group features; CatBoost same160/depth6 "
            "settings, loss MAE for DKK magnitude; MultiClass for already-"
            "observed-rise reversal. No tuning."
        ),
        "diagnostics": (
            "Saved predictions only: per year/period, history<6 versus "
            "established, group support/size, calibration, top1% error "
            "concentration, supported-group matched population and "
            "own/package/group incremental ablations."
        ),
        "exposure": (
            "All history retrospective exploratory. E1 2025-2026 already "
            "exposed. Development outcomes inspected only before2022; "
            "validation used for history selection. Evaluation inspected once "
            "after selection; any bug correction must retain lineage and cannot"
            " create an untouched claim."
        ),
        "future_confirmation": (
            "Reserve origins from2026-11-09 only conditionally: new "
            "independently evidenced calendar/publication/cutoffs, freeze "
            "selected model+population+metrics before actual exposure, log each"
            " source/prediction/outcome access. Reservation alone is not "
            "unexposed confirmation and execution is outside E2."
        ),
        "stopping": (
            "Stop after declared27 learned trials, secondary comparisons, "
            "saved-prediction diagnostics, review/replay/recovery/dashboard and"
            " cleanup. No search for a winner or automatic model promotion."
        ),
        "limitations": [
            "No sales, stock, expiry or supplier terms; no quantities or realised-profit claims.",
            (
                "Unknown historical publication timing prevents proven Wednesday "
                "operational readiness."
            ),
            "Supply-disruption interactions unavailable.",
            (
                "Equal-native-size AIP peers do not reproduce dispensing-price per-"
                "unit ABC rules or cover all economic substitutes."
            ),
        ],
    }
    atomic_json(output / "protocol.json", protocol)
    atomic_json(
        output / "freeze-receipt.json",
        {
            "protocol": _reference(output / "protocol.json"),
            "created_utc": utc(),
            "comparative_scores_inspected": False,
        },
    )
    data.close()


def _history_start(choice: Any, cutoff: str) -> str:
    return "2008-01-01" if choice == "expanding" else f"{int(cutoff[:4]) - int(choice)}-01-01"


def run_trial(
    data: ResearchData,
    protocol: dict[str, Any],
    root: Path,
    *,
    trial_id: str,
    split: str,
    start: str,
    end: str,
    cutoff: str,
    history: Any,
    task: str,
    method: str,
    feature_set: str = "group",
    validation: bool = False,
) -> dict[str, Any]:
    path = root / "trials" / trial_id
    receipt_path = path / "trial.json"
    if receipt_path.exists():
        previous_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        expected = {
            "trial_id": trial_id,
            "task": task,
            "method": method,
            "feature_set": feature_set,
            "split": split,
            "history": history,
            "fit_cutoff": cutoff,
            "prediction_start": start,
            "prediction_end": end,
            "protocol_sha256": digest(root / "protocol.json"),
        }
        if any(previous_receipt.get(key) != value for key, value in expected.items()):
            raise ResearchError("e2_retry_binding_changed")
        if previous_receipt["status"] == "running":
            raise ResearchError("e2_interrupted_trial_requires_explicit_failure_receipt")
        if previous_receipt["status"] == "completed":
            for item in previous_receipt["files"]:
                if digest(path / item["path"]) != item["sha256"]:
                    raise ResearchError("e2_trial_artifact_changed")
        return previous_receipt
    path.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    resource = ResourceSample()
    receipt: dict[str, Any] = {
        "trial_id": trial_id,
        "task": task,
        "method": method,
        "feature_set": feature_set,
        "split": split,
        "history": history,
        "training_start": _history_start(history, cutoff),
        "fit_cutoff": cutoff,
        "prediction_start": start,
        "prediction_end": end,
        "started_utc": utc(),
        "status": "running",
        "protocol_sha256": digest(root / "protocol.json"),
    }
    atomic_json(receipt_path, receipt)
    try:
        target = "reversal" if task == "reversal" else "next"
        training = data.training(receipt["training_start"], cutoff, target=target)
        candidates = data.candidates(start, end, reversal=task == "reversal")
        receipt.update(
            {
                "training_rows": len(training),
                "candidate_rows": len(candidates),
                "training_periods": int(training.period_start.nunique()),
                "latest_training_label_maturity": str(training[target + "_maturity_date"].max())[
                    :10
                ],
            }
        )
        if training.empty or candidates.empty:
            raise ResearchError("e2_empty_training_or_candidates")
        if training[target + "_maturity_date"].max() >= pd.Timestamp(cutoff):
            raise ResearchError("e2_immature_training_labels")
        features = protocol["features"][feature_set]
        categories = protocol["categories"][feature_set]
        y = (
            training.reversal_category.map(
                {"full_reversal": 0, "partial_reversal": 1, "persistence": 2}
            ).to_numpy(dtype=int)
            if task == "reversal"
            else training.next_direction.to_numpy(dtype=int) + 1
        )
        receipt["training_class_counts"] = np.bincount(y, minlength=3).tolist()
        files = []
        fit_started = time.monotonic()
        if method.startswith("logistic"):
            model = LogisticModel(features, categories).fit(training, y)
            prediction = model.predict_proba(candidates)
            atomic_json(path / "model.json", model.state())
            state = json.loads((path / "model.json").read_text())
            restored_logistic = LogisticModel(
                state["features"], state["categories"], state["penalty"]
            )
            restored_logistic.transforms = state["transforms"]
            restored_logistic.coefficients = np.asarray(state["coefficients"])
            if not np.array_equal(prediction, restored_logistic.predict_proba(candidates)):
                raise ResearchError("e2_logistic_model_readback_changed")
            files.append(pin(path / "model.json", path, "model"))
            receipt["convergence"] = model.convergence
            del model, restored_logistic
        elif method.startswith("catboost"):
            x = catboost_frame(training, features, categories)
            xp = catboost_frame(candidates, features, categories)
            limit = FitLimit(started)
            settings = dict(protocol["models"]["catboost"])
            if task == "magnitude":
                model = CatBoostRegressor(**settings, loss_function="MAE")
                labels = training.next_delta_ore.to_numpy(dtype=float) / 100
            else:
                model = CatBoostClassifier(**settings, loss_function="MultiClass", classes_count=3)
                labels = y.astype(float)
            model.fit(
                x,
                labels,
                cat_features=categories,
                sample_weight=period_weights(training),
                callbacks=[limit],
            )
            if limit.exceeded:
                raise ResearchError("e2_declared_fit_resource_limit")
            prediction = model.predict(xp) if task == "magnitude" else model.predict_proba(xp)
            model.save_model(str(path / "model.cbm"))
            restored = CatBoostRegressor() if task == "magnitude" else CatBoostClassifier()
            restored.load_model(str(path / "model.cbm"))
            checked = restored.predict(xp) if task == "magnitude" else restored.predict_proba(xp)
            if not np.array_equal(prediction, checked):
                raise ResearchError("e2_model_prediction_readback_changed")
            files.append(pin(path / "model.cbm", path, "model"))
            receipt["trees"] = model.tree_count_
            del x, xp, model, restored
        elif task == "magnitude":
            value = 0.0
            if method == "historical_median":
                amounts = training.next_delta_ore.to_numpy(dtype=float) / 100
                order = np.argsort(amounts, kind="stable")
                weights = period_weights(training)[order]
                value = float(
                    amounts[order[np.searchsorted(np.cumsum(weights), weights.sum() / 2)]]
                )
            prediction = np.full(len(candidates), value)
            receipt["fitted_value_dkk"] = value
        elif method == "historical_frequency":
            frequencies = frequency_probabilities(training, y)
            prediction = np.tile(frequencies, (len(candidates), 1))
            receipt["fitted_probabilities"] = frequencies.tolist()
        elif method == "repeat_last":
            directions = np.sign(candidates.lag1_delta_ore.fillna(0).to_numpy()).astype(int) + 1
            prediction = np.eye(3)[directions]
        else:
            prediction = np.tile(
                [0, 0, 1] if task == "reversal" else [0, 1, 0], (len(candidates), 1)
            )
        receipt["fit_and_inference_seconds"] = time.monotonic() - fit_started
        frozen = freeze_predictions(candidates, np.asarray(prediction), task=task, seed=SEED)
        # Persist the full feature-only prediction population BEFORE reading labels.
        frozen.to_parquet(path / "predictions.parquet", index=False)
        files.append(pin(path / "predictions.parquet", path, "predictions", len(frozen)))
        receipt["predictions_frozen_utc"] = utc()
        receipt["prediction_keys_sha256"] = (
            __import__("hashlib").sha256("\n".join(frozen.row_key).encode()).hexdigest()
        )
        del training, candidates
        gc.collect()
        evaluated = attach_outcomes(frozen, data.outcomes(start, end))
        if validation:
            late = evaluated.next_maturity_date.isna() | (
                evaluated.next_maturity_date >= pd.Timestamp(end)
            )
            evaluated.loc[late, "next_status"] = "pending_validation_cutoff"
            evaluated.loc[late, ["next_direction", "next_delta_ore"]] = np.nan
        evaluated.to_parquet(path / "outcomes.parquet", index=False)
        files.append(pin(path / "outcomes.parquet", path, "outcomes", len(evaluated)))
        aggregate, per_period = score_frame(evaluated, task, split, method)
        summary = {
            "aggregate": aggregate,
            "per_period": per_period,
            "calibration": []
            if task == "magnitude"
            else calibration_rows(evaluated, task, split, method),
        }
        atomic_json(path / "metrics.json", summary)
        files.append(pin(path / "metrics.json", path, "metrics"))
        receipt.update({"status": "completed", "files": files, "aggregate": aggregate})
    except Exception as exc:
        receipt.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    receipt["resource"] = resource.finish()
    receipt["finished_utc"] = utc()
    atomic_json(receipt_path, receipt)
    gc.collect()
    print(
        json.dumps(
            {
                k: receipt.get(k)
                for k in (
                    "trial_id",
                    "status",
                    "training_rows",
                    "candidate_rows",
                    "resource",
                    "error",
                )
            }
        ),
        flush=True,
    )
    return receipt


def execute(root: Path) -> None:
    execution_resource = ResourceSample()
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    if (
        digest(root / "protocol.json")
        != json.loads((root / "freeze-receipt.json").read_text())["protocol"]["sha256"]
    ):
        raise ResearchError("e2_frozen_protocol_changed")
    for name, expected in protocol["code"].items():
        if digest(Path(__file__).parent / name) != expected:
            raise ResearchError("e2_producer_changed_after_freeze")
    for reference in (protocol["dataset"], protocol["groups"]):
        verify_pin(Path(reference["path"]), reference)
    verify_training_manifest(Path(protocol["dataset"]["path"]))
    data = ResearchData(
        Path(protocol["dataset"]["path"]),
        Path(protocol["groups"]["path"]),
        scratch=root.parent / "scratch",
    )
    print(json.dumps({"stage": "verified_consumer_tables_ready", "utc": utc()}), flush=True)
    screens = []
    for history in protocol["history_choices"]:
        trial = run_trial(
            data,
            protocol,
            root,
            trial_id=f"screen-{history}",
            split="validation",
            start="2022-01-01",
            end="2024-01-01",
            cutoff="2022-01-01",
            history=history,
            task="direction",
            method="catboost_group",
            validation=True,
        )
        if trial["status"] == "completed":
            screens.append(trial)
        elif trial.get("error") != "e2_declared_fit_resource_limit":
            raise ResearchError("e2_screen_failed_before_selection")
    if not screens:
        raise ResearchError("e2_all_canary_screens_failed")
    best = min(x["aggregate"]["metrics"]["brier"] for x in screens)
    chosen = next(
        x["history"] for x in screens if x["aggregate"]["metrics"]["brier"] <= best + 0.001
    )
    selection = {
        "history": chosen,
        "rule": protocol["selection"],
        "screen_trials": [x["trial_id"] for x in screens],
        "selected_before_evaluation": True,
        "created_utc": utc(),
    }
    if (root / "selection.json").exists():
        if json.loads((root / "selection.json").read_text())["history"] != chosen:
            raise ResearchError("e2_selection_changed")
    else:
        atomic_json(root / "selection.json", selection)
    for fold in protocol["evaluation"]:
        args = {
            "data": data,
            "protocol": protocol,
            "root": root,
            "split": fold["name"],
            "start": fold["start"],
            "end": fold["end"],
            "cutoff": fold["fit_cutoff"],
            "history": chosen,
        }
        for method in ("no_change", "historical_frequency", "repeat_last"):
            run_trial(
                **args,
                trial_id=fold["name"] + "-direction-" + method,
                task="direction",
                method=method,
            )
        for feature_set in ("own", "package", "group"):
            for algorithm in ("logistic", "catboost"):
                method = algorithm + "_" + feature_set
                run_trial(
                    **args,
                    trial_id=fold["name"] + "-direction-" + method,
                    task="direction",
                    method=method,
                    feature_set=feature_set,
                )
        for task, methods in (
            ("magnitude", ("no_change", "historical_median", "catboost_group_mae")),
            ("reversal", ("persistence", "historical_frequency", "catboost_group")),
        ):
            for method in methods:
                run_trial(
                    **args,
                    trial_id=fold["name"] + "-" + task + "-" + method,
                    task=task,
                    method=method,
                )
    data.close()
    atomic_json(root / "execution-resource.json", execution_resource.finish())
    publish(root)


def publish(root: Path) -> None:
    protocol = json.loads((root / "protocol.json").read_text())
    trials = [json.loads(p.read_text()) for p in sorted((root / "trials").glob("*/trial.json"))]
    protocol_hash = digest(root / "protocol.json")
    for trial in trials:
        if trial.get("protocol_sha256") != protocol_hash:
            raise ResearchError("e2_publish_protocol_binding_changed")
        if trial["status"] == "completed":
            for item in trial["files"]:
                verify_pin(root / "trials" / trial["trial_id"] / item["path"], item)
    ledger = {
        "trials": trials,
        "learned_attempts": sum(x["method"].startswith(("catboost", "logistic")) for x in trials),
        "failures": sum(x["status"] != "completed" for x in trials),
    }
    atomic_json(root / "trial-ledger.json", ledger)
    expected = {f"screen-{choice}" for choice in protocol["history_choices"]}
    for fold in protocol["evaluation"]:
        for task, names in (
            (
                "direction",
                (
                    "no_change",
                    "historical_frequency",
                    "repeat_last",
                    "logistic_own",
                    "logistic_package",
                    "logistic_group",
                    "catboost_own",
                    "catboost_package",
                    "catboost_group",
                ),
            ),
            ("magnitude", ("no_change", "historical_median", "catboost_group_mae")),
            ("reversal", ("persistence", "historical_frequency", "catboost_group")),
        ):
            expected.update(fold["name"] + "-" + task + "-" + name for name in names)
    if {x["trial_id"] for x in trials} != expected or any(
        x["status"] not in {"completed", "failed"} for x in trials
    ):
        raise ResearchError("e2_declared_trial_inventory_incomplete")
    if ledger["learned_attempts"] != protocol["fits"]["maximum_learned_fits"]:
        raise ResearchError("e2_fit_budget_changed")
    metrics: dict[str, Any] = {
        "aggregate": [],
        "per_period": [],
        "calibration": [],
        "uncertainty": [],
    }
    histories = []
    frames: dict[tuple[str, str], list[Path]] = {}
    methods: dict[str, list[str]] = {}
    coverage_rows = []
    prediction_keys: dict[str, str] = {}
    for trial in trials:
        if trial["status"] != "completed":
            continue
        path = root / "trials" / trial["trial_id"]
        summary = json.loads((path / "metrics.json").read_text())
        if trial["method"] not in methods.setdefault(trial["task"], []):
            methods[trial["task"]].append(trial["method"])
        if trial["split"] == "validation":
            histories.append(
                {
                    **summary["aggregate"],
                    "history_years": 14 if trial["history"] == "expanding" else trial["history"],
                    "history": trial["history"],
                    "training_rows": trial["training_rows"],
                    "runtime_seconds": trial["resource"]["elapsed_seconds"],
                }
            )
            continue
        if trial["task"] == "direction":
            previous_keys = prediction_keys.setdefault(
                trial["split"], trial["prediction_keys_sha256"]
            )
            if previous_keys != trial["prediction_keys_sha256"]:
                raise ResearchError("e2_unfair_prediction_populations")
        metrics["aggregate"].append(summary["aggregate"])
        metrics["per_period"].extend(summary["per_period"])
        metrics["calibration"].extend(summary["calibration"])
        frames.setdefault((trial["task"], trial["method"]), []).append(path / "outcomes.parquet")
        if trial["task"] == "direction" and trial["method"] == "no_change":
            frame = pd.read_parquet(path / "outcomes.parquet")
            for period, part in frame.groupby("period_start"):
                coverage_rows.append(
                    {
                        "period_start": str(period)[:10],
                        "split": trial["split"],
                        "candidates": len(part),
                        "observed": int((part.next_status == "observed").sum()),
                        "status_counts": part.next_status.value_counts().to_dict(),
                        "peer_feature_state": part.peer_feature_state.value_counts().to_dict()
                        if "peer_feature_state" in part
                        else {},
                    }
                )
    diagnostics = []
    for (task, method), pieces in frames.items():
        frame = pd.concat([pd.read_parquet(piece) for piece in pieces], ignore_index=True)
        aggregate, all_periods = score_frame(frame, task, "evaluation_all", method)
        metrics["aggregate"].append(aggregate)
        metrics["per_period"].extend(all_periods)
        if task != "magnitude":
            metrics["calibration"].extend(calibration_rows(frame, task, "evaluation_all", method))
        for name, mask in (
            ("new_under6", frame.history_periods < 6),
            ("established6plus", frame.history_periods >= 6),
            (
                "group_supported",
                frame.peer_count.notna()
                if "peer_count" in frame
                else pd.Series(False, index=frame.index),
            ),
            (
                "group_unknown",
                frame.peer_count.isna()
                if "peer_count" in frame
                else pd.Series(True, index=frame.index),
            ),
            (
                "multi_peer_3plus",
                frame.peer_count >= 3
                if "peer_count" in frame
                else pd.Series(False, index=frame.index),
            ),
        ):
            if mask.any():
                result, _ = score_frame(frame.loc[mask], task, "evaluation_all", method)
                diagnostics.append(
                    {
                        **result,
                        "subgroup": name,
                        "ranking_note": (
                            "Original full-cohort shortlist membership retained; subgroup "
                            "filtering never reranks."
                        ),
                    }
                )
    pairs = [
        ("catboost_group", "catboost_package"),
        ("catboost_package", "catboost_own"),
        ("logistic_group", "logistic_package"),
        ("logistic_package", "logistic_own"),
        ("catboost_group", "logistic_group"),
        ("catboost_group", "historical_frequency"),
        ("catboost_group", "no_change"),
        ("logistic_group", "historical_frequency"),
    ]
    comparisons = []
    for candidate, baseline in pairs:
        a = {
            x["period_start"]: x
            for x in metrics["per_period"]
            if x["task"] == "direction"
            and x["method"] == candidate
            and x["split"] == "evaluation_all"
        }
        b = {
            x["period_start"]: x
            for x in metrics["per_period"]
            if x["task"] == "direction"
            and x["method"] == baseline
            and x["split"] == "evaluation_all"
        }
        common = sorted(
            day
            for day in a.keys() & b.keys()
            if "brier" in a[day]["metrics"] and "brier" in b[day]["metrics"]
        )
        if len(common) < 2:
            continue
        differences = np.array(
            [a[day]["metrics"]["brier"] - b[day]["metrics"]["brier"] for day in common]
        )
        interval = paired_interval(differences, seed=SEED)
        sensitivity = paired_interval(differences, seed=SEED, block=8)
        comparisons.append(
            {
                "task": "direction",
                "split": "evaluation_all",
                "candidate": candidate,
                "baseline": baseline,
                "n": sum(a[d]["n"] for d in common),
                "metrics": {
                    "brier_difference": interval["mean_difference"],
                    "ci_lower": interval["lower"],
                    "ci_upper": interval["upper"],
                    "sensitivity_lower": sensitivity["lower"],
                    "sensitivity_upper": sensitivity["upper"],
                },
                "periods": len(common),
                "interpretation": (
                    "Negative difference favors candidate; paired temporal interval, exploratory."
                ),
            }
        )
        metrics["uncertainty"].append(
            {
                "task": "direction",
                "split": "evaluation_all",
                "method": candidate,
                "baseline": baseline,
                **interval,
                "sensitivity_block8": sensitivity,
            }
        )
    output = root / "saved"
    output.mkdir(exist_ok=True)
    atomic_json(output / "metrics.json", metrics)
    atomic_json(
        output / "coverage.json",
        {
            "overall": {
                "candidate_observations": sum(x["candidates"] for x in coverage_rows),
                "observed_outcomes": sum(x["observed"] for x in coverage_rows),
                "periods": len(coverage_rows),
                "unknown_not_scored_as_failure": True,
            },
            "per_period": coverage_rows,
            "trial_statuses": [
                {
                    key: trial.get(key)
                    for key in (
                        "trial_id",
                        "task",
                        "method",
                        "split",
                        "history",
                        "status",
                        "error",
                        "training_rows",
                        "candidate_rows",
                        "resource",
                    )
                }
                for trial in trials
            ],
        },
    )
    atomic_json(output / "history_curves.json", {"experiments": histories})
    atomic_json(output / "comparisons.json", {"incremental": comparisons})
    atomic_json(output / "protocol.json", protocol)
    atomic_json(root / "diagnostics.json", {"subgroups": diagnostics})
    atomic_json(
        root / "trial-ledger.json",
        {
            "trials": trials,
            "learned_attempts": sum(
                x["method"].startswith(("catboost", "logistic")) for x in trials
            ),
            "failures": sum(x["status"] != "completed" for x in trials),
        },
    )
    splits = {f["name"]: {"start": f["start"], "end": f["end"]} for f in protocol["evaluation"]}
    splits.update(
        {
            "evaluation_all": {"start": "2024-01-01", "end": "2026-10-26"},
            "validation": {"start": "2022-01-01", "end": "2024-01-01"},
        }
    )
    result = {
        "schema_version": "e2-saved-results-v1",
        "status": "completed",
        "evaluation_locked": True,
        "execution_status": "all_declared_trials_completed"
        if not ledger["failures"]
        else "declared_trials_finished_with_failures",
        "failed_trials": [
            {"trial_id": x["trial_id"], "error": x.get("error")}
            for x in trials
            if x["status"] != "completed"
        ],
        "dataset": {
            "base": protocol["dataset"],
            "groups": protocol["groups"],
            "contract": protocol["version"],
        },
        "definitions": {
            "primary": protocol["weighting"],
            "ranking": protocol["ranking"],
            "magnitude": protocol["targets"]["magnitude"],
            "reversal": protocol["targets"]["reversal"],
            "limitations": protocol["limitations"],
        },
        "split_dates": splits,
        "methods": methods,
        "baseline_by_task": {
            "direction": "historical_frequency",
            "magnitude": "no_change",
            "reversal": "historical_frequency",
        },
        "candidate_methods": ["catboost_group", "logistic_group", "catboost_group_mae"],
        "exposure_status": protocol["exposure"],
        "files": [
            pin(output / (name + ".json"), output, name)
            for name in ("metrics", "coverage", "history_curves", "comparisons", "protocol")
        ],
        "resources": {
            "total_trial_seconds": sum(x["resource"]["elapsed_seconds"] for x in trials),
            "largest_training_rows": max(x.get("training_rows", 0) for x in trials),
            "peak_rss_bytes": max(x["resource"]["sampled_peak_rss_bytes"] for x in trials),
            "threads": 6,
            "trials": len(trials),
            "failed_trials": sum(x["status"] != "completed" for x in trials),
        },
        "created_utc": utc(),
    }
    atomic_json(output / "result.json", result)


def replay(root: Path) -> None:
    checked = []
    for path in sorted((root / "trials").glob("*/trial.json")):
        receipt = json.loads(path.read_text())
        if receipt["status"] != "completed":
            continue
        for item in receipt["files"]:
            if digest(path.parent / item["path"]) != item["sha256"]:
                raise ResearchError("e2_replay_pin_changed")
        predictions = pd.read_parquet(path.parent / "predictions.parquet")
        outcomes = pd.read_parquet(path.parent / "outcomes.parquet")
        if not predictions.row_key.equals(outcomes.row_key):
            raise ResearchError("e2_replay_population_changed")
        if "selected" in predictions and not predictions.selected.equals(outcomes.selected):
            raise ResearchError("e2_replay_shortlist_changed")
        expected = json.loads((path.parent / "metrics.json").read_text())
        aggregate, per_period = score_frame(
            outcomes, receipt["task"], receipt["split"], receipt["method"]
        )
        actual = {
            "aggregate": aggregate,
            "per_period": per_period,
            "calibration": []
            if receipt["task"] == "magnitude"
            else calibration_rows(outcomes, receipt["task"], receipt["split"], receipt["method"]),
        }
        if actual != expected:
            raise ResearchError("e2_replay_metrics_changed")
        checked.append(receipt["trial_id"])
    atomic_json(
        root / "replay.json",
        {
            "status": "passed",
            "trials": checked,
            "created_utc": utc(),
            "mode": "exact saved predictions/labels/shortlist/metrics, no refitting",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "run", "publish", "replay"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--training-pointer", type=Path)
    parser.add_argument("--groups", type=Path)
    args = parser.parse_args()
    if args.command == "freeze":
        if args.training_pointer is None or args.groups is None:
            parser.error("freeze requires --training-pointer and --groups")
        freeze_protocol(args.training_pointer, args.groups, args.root)
    elif args.command == "run":
        execute(args.root)
    elif args.command == "publish":
        publish(args.root)
    else:
        replay(args.root)


if __name__ == "__main__":
    main()
