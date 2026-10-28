"""Expanding chronological offline evaluation over a verified frozen panel.

Only two pending feature periods and O(p²) fitting statistics are retained in
addition to the source panel. Every mature training row is accumulated once.
Fixed hyperparameters and methods apply before and throughout the final window.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from .artifacts import append_exposure, filepin, jsonlines, outside_git, versions, write_json
from .dataset import (
    Dataset,
    ResearchError,
    Row,
    canonical,
    digest,
    fingerprint,
    load_dataset,
    pinned_files,
)
from .features import (
    FEATURE_NAMES,
    FloatArray,
    Target,
    next_target,
    period_features,
    reversal_target,
)
from .metrics import score_next, score_reversal, summarize
from .models import (
    METHOD_VERSION,
    NEXT_METHODS,
    REVERSAL_METHODS,
    Statistics,
    next_predictions,
    reversal_predictions,
)


@dataclass(frozen=True)
class EvaluationConfig:
    initial_train: tuple[int, int] = (0, 23)
    validation: tuple[int, int] = (24, 31)
    final_evaluation: tuple[int, int] = (32, 44)
    diagnostic: tuple[int, int] = (45, 46)
    cutoff_hour: int = 12
    cutoff_minute: int = 0
    bootstrap_seed: int = 20261022
    bootstrap_replicates: int = 1000
    bootstrap_block_length: int = 3

    def splits(self) -> dict[str, tuple[int, int]]:
        return {
            name: getattr(self, name)
            for name in ("initial_train", "validation", "final_evaluation", "diagnostic")
        }

    def validate(self, periods: int) -> None:
        integer_settings = (
            self.cutoff_hour,
            self.cutoff_minute,
            self.bootstrap_seed,
            self.bootstrap_replicates,
            self.bootstrap_block_length,
        )
        if any(type(value) is not int for value in integer_settings):
            raise ResearchError("integer_configuration_required")
        expected = 0
        for bounds in self.splits().values():
            if (
                len(bounds) != 2
                or any(type(value) is not int for value in bounds)
                or bounds[0] != expected
                or bounds[1] < bounds[0]
            ):
                raise ResearchError("splits_must_be_nonoverlapping_contiguous_and_complete")
            expected = bounds[1] + 1
        if expected != periods or self.final_evaluation[1] + 2 >= periods:
            raise ResearchError("calendar_must_cover_splits_and_complete_final_horizon")
        if not (0 <= self.cutoff_hour <= 23 and 0 <= self.cutoff_minute <= 59):
            raise ResearchError("invalid_cutoff_time")
        if (
            self.bootstrap_replicates < 1
            or self.bootstrap_block_length < 1
            or self.bootstrap_seed < 0
        ):
            raise ResearchError("invalid_bootstrap_configuration")

    def split(self, index: int) -> str:
        return next(name for name, (start, end) in self.splits().items() if start <= index <= end)


DEFINITIONS: dict[str, Any] = {
    "feature_version": "causal-aip-history-v1",
    "next_period": (
        "Exact positive integer-ore AIP direction -1/0/+1 at next declared calendar index; "
        "zero tolerance; log(future/current); future AIP DKK=ore/100."
    ),
    "reversal": (
        "Only after observed strict current>previous rise within same episode; "
        "full reversal=min(next1,next2)<=pre-rise AIP, tolerance0ore; both future calendar "
        "prices must be observed. Partial=min future<current but >pre-rise; "
        "persistence=all future>=current. Never condition next-price candidate eligibility "
        "on a future rise."
    ),
    "maturity": (
        "At origin t, next-price training origins j+1<=t; reversal origins j+2<=t. "
        "Only observed complete labels; all training-origin features were frozen at j. "
        "Expanding updates include matured validation/final-period observations under "
        "the frozen algorithm; no method selection/tuning on final outcomes."
    ),
    "population": (
        "All eligible current-period observations, independently per origin; no survivor "
        "or future-outcome cohort. Same observed target cases for every method. Top10% rise "
        "precision ranks those same observed cases, ties by canonical identifier, ceil(n/10), "
        "conditional on observable outcomes."
    ),
    "features": (
        "Current log AIP DKK; three calendar lag returns/missing flags within episode; "
        "rise/fall/flat frequency over all contiguous past known episode transitions; "
        "history length/missing; current lifecycle age/missing; current ordinary ABC "
        "fixed one-hot/unknown; current eligible same-basis group count and own/min AIP "
        "ratio/missing; positive supply-report observed/unobserved and coverage-unknown "
        "flags. Missing numeric values zero with explicit indicator. Absent supply is "
        "unknown, never evidence of no disruption."
    ),
    "preprocessing": (
        "Train-matured-row mean/population standard deviation; scale<1e-8 set1. "
        "Encodings fixed before fitting. Ridge sum squared error+1*sum standardized "
        "coefficient squared; unpenalized intercept. Probability regression to three "
        "one-hots and binary reversal; clip negatives and normalize multiclass, clip "
        "binary[0,1]. Magnitude log return bounded to +/-log100 for every method."
    ),
    "fallback": (
        "Ridge with <feature_count+2 matured rows falls back to historical. Next class "
        "probabilities Dirichlet(1,1,1), magnitude mean matured log return or0. Reversal "
        "Beta(1,1). No-change probability flat1/logreturn0; last-change repeats observed "
        "lag1 direction and magnitude or flat0 when missing. No-reversal probability0. "
        "No model or threshold tuning."
    ),
    "uncertainty": (
        "95% percentile circular moving blocks of tariff periods, default length3, "
        "fixedseed1000replicates, aggregating per-period score numerators/denominators. "
        "Conditional on fitted forecasts; limited calendar and overlap remain material. "
        "Fewer than2 observed periods yields null bounds. Not package-independent intervals."
    ),
    "financial_scope": (
        "Forecast accuracy only. No demand, purchasing policy, receipt-price, procurement "
        "savings or realised-profit estimate."
    ),
    "tuning_budget": 0,
}


def decision_cutoff(dataset: Dataset, index: int, config: EvaluationConfig) -> str:
    start = dataset.periods[index]
    # Actual declared start date anchors the second tariff week, including a
    # non-Monday official start; no inference of future price-release timing.
    second_week_start = start + timedelta(days=7)
    wednesday = second_week_start + timedelta(days=(2 - second_week_start.weekday()) % 7)
    result = datetime.combine(
        wednesday, time(config.cutoff_hour, config.cutoff_minute), ZoneInfo("Europe/Copenhagen")
    )
    end = dataset.manifest["periods"][index].get("end_exclusive")
    if (end and result.date().isoformat() >= end) or (
        index + 1 < len(dataset.periods) and result.date() >= dataset.periods[index + 1]
    ):
        raise ResearchError("decision_cutoff_outside_tariff_period")
    return result.isoformat()


def _coverage(
    dataset: Dataset,
    index: int,
    config: EvaluationConfig,
    rows: tuple[Row, ...],
    next_labels: list[Target],
    reversal_labels: list[Target],
) -> dict[str, Any]:
    return {
        "period_index": index,
        "period_start": dataset.periods[index].isoformat(),
        "split": config.split(index),
        "total_rows": len(dataset.by_period[index]),
        "eligible_candidates": len(rows),
        "excluded": dict(
            sorted(
                Counter(
                    row.exclusion_reason for row in dataset.by_period[index] if not row.eligible
                ).items()
            )
        ),
        "next_period_labels": dict(sorted(Counter(target.state for target in next_labels).items())),
        "reversal_labels": dict(
            sorted(Counter(target.state for target in reversal_labels).items())
        ),
        "observed_rise_candidates": sum(
            target.state != "not_observed_rise" for target in reversal_labels
        ),
    }


def evaluate(
    manifest_path: Path,
    output_dir: Path,
    *,
    config: EvaluationConfig | None = None,
    lineage_path: Path | None = None,
    development_parent: str | None = None,
) -> dict[str, Any]:
    """Publish one new immutable result bundle and append holdout exposure.

    output_dir must not exist. Input/output/lineage stay outside Git. The source
    manifest and every source file are verified before computation. Interrupted
    computation cannot publish a completed directory. This method does not fit
    private data implicitly: the caller explicitly selects the frozen manifest.
    """
    config = config or EvaluationConfig()
    if development_parent is not None and not re.fullmatch("[0-9a-f]{64}", development_parent):
        raise ResearchError("invalid_development_parent_fingerprint")
    manifest_path = Path(manifest_path).resolve()
    output_dir = Path(output_dir).resolve()
    outside_git(manifest_path)
    outside_git(output_dir)
    if output_dir.exists():
        raise ResearchError("output_directory_already_exists")
    lineage_path = (
        Path(lineage_path).resolve()
        if lineage_path
        else output_dir.parent / "holdout-lineage.jsonl"
    )
    outside_git(lineage_path)
    if lineage_path.is_relative_to(output_dir):
        raise ResearchError("lineage_must_be_outside_immutable_bundle")
    dataset = load_dataset(manifest_path)
    config.validate(len(dataset.periods))
    # Verify cutoff assumptions for the entire actual calendar before any fit.
    cutoffs = [decision_cutoff(dataset, index, config) for index in range(len(dataset.periods))]
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = output_dir.with_name("." + output_dir.name + ".staging-" + uuid.uuid4().hex)
    stage.mkdir()
    try:
        code_versions = versions()
        evaluation_input_fingerprint = fingerprint(
            {
                "manifest_sha256": dataset.manifest_hash,
                "method_version": METHOD_VERSION,
                "configuration": asdict(config),
                "versions": code_versions,
                "development_parent": development_parent,
            }
        )
        # Record exposure intent before any model fitting or outcome scoring.
        # An interrupted attempt remains visible even without a completed run.
        append_exposure(
            lineage_path,
            {
                "event": "final_holdout_evaluation_started",
                "evaluation_input_fingerprint": evaluation_input_fingerprint,
                "dataset_id": dataset.manifest["dataset_id"],
                "manifest_sha256": dataset.manifest_hash,
                "final_evaluation": config.final_evaluation,
                "development_parent": development_parent,
            },
        )
        return _evaluate(
            dataset,
            output_dir,
            stage,
            config,
            lineage_path,
            development_parent,
            cutoffs,
            code_versions,
            evaluation_input_fingerprint,
        )
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _evaluate(
    dataset: Dataset,
    output_dir: Path,
    stage: Path,
    config: EvaluationConfig,
    lineage_path: Path,
    development_parent: str | None,
    cutoffs: list[str],
    code_versions: dict[str, Any],
    evaluation_input_fingerprint: str,
) -> dict[str, Any]:
    statistics = {task: Statistics(task) for task in ("next_period", "reversal")}
    pending: dict[int, tuple[FloatArray, list[Target], list[Target]]] = {}
    coverage: list[dict[str, Any]] = []
    period_metrics: list[dict[str, Any]] = []
    calibration: list[dict[str, Any]] = []
    prediction_path, model_path = stage / "predictions.jsonl.gz", stage / "models.jsonl.gz"
    with jsonlines(prediction_path) as prediction_file, jsonlines(model_path) as model_file:
        for index in range(len(dataset.periods)):
            for task, horizon, label_position in (("next_period", 1, 1), ("reversal", 2, 2)):
                prior = pending.get(index - horizon)
                if prior:
                    prior_labels = prior[1] if label_position == 1 else prior[2]
                    statistics[task].add(prior[0], prior_labels, index - horizon, index)
            pending.pop(index - 3, None)
            rows, features = period_features(dataset, index)
            next_labels = [next_target(dataset, row) for row in rows]
            reversal_labels = [reversal_target(dataset, row) for row in rows]
            pending[index] = (features, next_labels, reversal_labels)
            fitted = {task: value.fit() for task, value in statistics.items()}
            current = np.array([(row.amount_ore or 0) / 100 for row in rows])
            next_forecasts = next_predictions(fitted["next_period"], features, current)
            reversal_forecasts = reversal_predictions(fitted["reversal"], features)
            coverage.append(_coverage(dataset, index, config, rows, next_labels, reversal_labels))
            for task, model in fitted.items():
                model_file.write(
                    canonical(
                        {
                            "task": task,
                            "period_index": index,
                            "period_start": dataset.periods[index].isoformat(),
                            "decision_cutoff": cutoffs[index],
                            "feature_names": FEATURE_NAMES,
                            **model.serial(),
                        }
                    )
                    + b"\n"
                )
            for task, methods in (("next_period", NEXT_METHODS), ("reversal", REVERSAL_METHODS)):
                for method in methods:
                    labels = next_labels if task == "next_period" else reversal_labels
                    if task == "next_period":
                        scores, denominators, bins = score_next(
                            labels,
                            next_forecasts[method],
                            [row.canonical_identifier for row in rows],
                        )
                    else:
                        scores, denominators, bins = score_reversal(
                            labels, reversal_forecasts[method]
                        )
                    base = {
                        "task": task,
                        "method": method,
                        "split": config.split(index),
                        "period_index": index,
                        "period_start": dataset.periods[index].isoformat(),
                    }
                    period_metrics.append(
                        {
                            **base,
                            "n": sum(label.state == "observed" for label in labels),
                            "metrics": scores,
                            "metric_denominators": denominators,
                            "training_rows": fitted[task].n,
                            "fallback": method == "ridge" and fitted[task].fallback,
                        }
                    )
                    calibration.extend({**base, **record} for record in bins)
            # Ineligible current rows are retained in coverage AND the complete
            # assessment stream, so all catalogue exclusions remain recoverable.
            positions = {row.canonical_identifier: position for position, row in enumerate(rows)}
            for row in dataset.by_period[index]:
                record: dict[str, Any] = {
                    "period_index": index,
                    "period_start": dataset.periods[index].isoformat(),
                    "split": config.split(index),
                    "decision_cutoff": cutoffs[index],
                    "canonical_identifier": row.canonical_identifier,
                    "identity_episode_id": row.identity_episode_id,
                    "identity_state": row.identity_state,
                    "provenance_id": row.provenance_id,
                    "amount_ore": row.amount_ore,
                    "eligible": row.eligible,
                    "exclusion_reason": row.exclusion_reason,
                }
                if row.eligible:
                    position = positions[row.canonical_identifier]
                    record.update(
                        {
                            "features": features[position].tolist(),
                            "targets": {
                                "next_period": next_labels[position].serial(),
                                "reversal": reversal_labels[position].serial(),
                            },
                            "next_period": {
                                method: {
                                    key: value[position].tolist() for key, value in forecast.items()
                                }
                                for method, forecast in next_forecasts.items()
                            },
                            "reversal": {
                                method: float(value[position])
                                for method, value in reversal_forecasts.items()
                            }
                            if reversal_labels[position].state != "not_observed_rise"
                            else {},
                        }
                    )
                else:
                    record.update(
                        {"features": None, "targets": {}, "next_period": {}, "reversal": {}}
                    )
                prediction_file.write(canonical(record) + b"\n")
    write_json(
        stage / "metrics.json",
        summarize(
            period_metrics,
            calibration,
            seed=config.bootstrap_seed,
            replicates=config.bootstrap_replicates,
            block_length=config.bootstrap_block_length,
        ),
    )
    write_json(
        stage / "coverage.json",
        {
            "per_period": coverage,
            "overall": {
                "total_rows": len(dataset.rows),
                "eligible_candidates": sum(row.eligible for row in dataset.rows),
                "excluded": dict(
                    sorted(
                        Counter(
                            row.exclusion_reason for row in dataset.rows if not row.eligible
                        ).items()
                    )
                ),
            },
        },
    )
    # Copy exact input-manifest bytes; source-file hashes and provenance are
    # retained there rather than duplicating the potentially huge frozen panel.
    with (stage / "input-manifest.json").open("xb") as target:
        target.write(dataset.manifest_bytes)
    roles = {
        "input-manifest.json": "input_manifest",
        "predictions.jsonl.gz": "predictions",
        "models.jsonl.gz": "models",
        "metrics.json": "metrics",
        "coverage.json": "coverage",
    }
    run: dict[str, Any] = {
        "schema_version": "e1-run-v1",
        "status": "completed",
        "evaluation_locked": True,
        "dataset": {
            key: dataset.manifest[key]
            for key in (
                "dataset_id",
                "price_context",
                "availability_mode",
                "population_definition",
                "identity_policy",
                "provenance",
            )
        },
        "method_version": METHOD_VERSION,
        "feature_names": FEATURE_NAMES,
        "definitions": DEFINITIONS,
        "splits": config.splits(),
        "split_dates": {
            name: {
                "start": dataset.periods[start].isoformat(),
                "end": dataset.periods[end].isoformat(),
            }
            for name, (start, end) in config.splits().items()
        },
        "cutoff_policy": {
            "timezone": "Europe/Copenhagen",
            "weekday": "Wednesday",
            "tariff_week": 2,
            "hour": config.cutoff_hour,
            "minute": config.cutoff_minute,
            "status": (
                "configurable_working_assumption;not_evidenced_publication_or_supplier_contract"
            ),
        },
        "methods": {"next_period": NEXT_METHODS, "reversal": REVERSAL_METHODS},
        "versions": code_versions,
        "evaluation_input_fingerprint": evaluation_input_fingerprint,
        "configuration": asdict(config),
        "development_parent": development_parent,
        "files": [filepin(stage / name, role) for name, role in roles.items()],
    }
    run["dataset"]["manifest_sha256"] = dataset.manifest_hash
    run["run_fingerprint"] = fingerprint(run)
    # Reverify all input bytes before publication, detecting concurrent damage.
    if digest(dataset.manifest_path) != dataset.manifest_hash:
        raise ResearchError("input_manifest_changed_during_evaluation")
    pinned_files(dataset.manifest_path.parent, dataset.manifest["files"])
    if versions() != code_versions:
        raise ResearchError("code_or_dependencies_changed_during_evaluation")
    if digest(dataset.manifest_path) != dataset.manifest_hash:
        raise ResearchError("input_manifest_changed_during_evaluation")
    if development_parent is not None:
        append_exposure(
            lineage_path,
            {
                "event": "used_for_development",
                "run_fingerprint": development_parent,
                "downstream_run_fingerprint": run["run_fingerprint"],
                "dataset_id": dataset.manifest["dataset_id"],
                "manifest_sha256": dataset.manifest_hash,
                "independent_confirmation": "requires_a_genuinely_new_final_window",
            },
        )
    run["lineage_event_hash"] = append_exposure(
        lineage_path,
        {
            "event": "final_holdout_evaluation_locked",
            "evaluation_input_fingerprint": evaluation_input_fingerprint,
            "run_fingerprint": run["run_fingerprint"],
            "dataset_id": dataset.manifest["dataset_id"],
            "manifest_sha256": dataset.manifest_hash,
            "development_parent": development_parent,
            "final_evaluation": config.final_evaluation,
            "final_split_dates": run["split_dates"]["final_evaluation"],
            "tuning_budget": 0,
        },
    )
    run["created_at"] = datetime.now(UTC).isoformat()
    write_json(stage / "run.json", run)
    # Same-filesystem atomic directory publication. No completed partial path.
    os.rename(stage, output_dir)
    return run
