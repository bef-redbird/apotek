"""Saved-prediction E3 publication. No model construction, fitting or database access.

The publisher freezes its specification/code before opening evaluated outcomes.
Prediction routing and queue membership are written before outcome attachment.
An unused replay destination reuses the pinned chronological calibration states.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import time
import zipfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .dataset import ResearchError, digest
from .e2_data import attach_outcomes, verify_pin
from .e2_experiment import ResourceSample
from .e2_metrics import paired_interval
from .e3_data import SCENARIOS, perturb
from .e3_experiment import read_prediction, same_values
from .e3_methods import (
    LAMBDAS,
    SEED,
    THRESHOLDS,
    amount_metrics,
    calibrate,
    direction_metrics,
    fallback,
    intervals,
    queue_periods,
    residual_fit,
    select_queue,
    temperature_fit,
)
from .group_features import pin
from .training_contract import KEYS, atomic_json

PROBABILITIES = ["p0", "p1", "p2"]
OUTCOMES = ["next_status", "next_aip_ore", "next_direction"]
FREEZE_NAME = "publisher-freeze-v3.json"
ARCHIVE_NAME = "publisher-producer-v3.zip"
AMENDMENT_NAME = "publisher-amendment-v3.json"
RESOURCE_NAME = "publication-resource-v3.json"
ORIGIN_COLUMNS = [
    *KEYS,
    "aip_ore",
    "history_periods",
    "peer_count",
    "peer_feature_state",
    "broad_peer_count",
    "broad_peer_different_size_count",
    "broad_peer_feature_state",
]
SPECIFICATION: dict[str, Any] = {
    "version": "e3-publisher-v3",
    "metrics": "Frozen E3 protocol; equal-period primary and pooled diagnostics separately",
    "calibration": "2023->2024,2024->2025,2025->2026; mature strictly before cutoff",
    "routing": "equal count>=1; broad count>=1 complete/partial same-unit; else equal/package",
    "comparison": "candidate-minus-baseline; exact origins/keys/outcomes; block4+8,2000,20261026",
    "queues": "Saved before evaluated outcome attachment; natural and same-origin matched count",
    "review": "One row/origin, perfect proxy, validated risk/threshold/top10; outcomes separate",
    "intervals": "Fixed current-price bins, earlier heldout absolute residual q90",
    "selection": "Only selection.json choices; grid curves descriptive; no new selection",
    "replay": "Pinned saved predictions and calibrators only; unused destination required",
    "subgroups": "Perfect proxy: full fixed subgroups; other sensitivities: all candidates",
    "metric_aggregation": (
        "Unprefixed aggregate scores and p95/p99/max/top1 fields are arithmetic means "
        "of period statistics; pooled_* fields describe all observed package outcomes together. "
        "Use pooled_p95_dkk,pooled_p99_dkk,pooled_max_error_dkk,pooled_top1_error_share "
        "for overall tails/extremes. Counts remain totals."
    ),
    "relocation": (
        "Explicit original E3 root maps to the physical run parent; renamed run directory "
        "maps separately. Verify exact bytes and preserve canonical paths; R1/E2 unchanged."
    ),
    "scenario_input_coverage": (
        "Perturb known advance codes only; wrong code mapping A->B->C->A. Group errors "
        "share the seeded origin-period/current-group hash and affect only known codes. "
        "Rates are hash thresholds, not exact changed counts or measured report accuracy."
    ),
    "prepared_finish": (
        "Score exact pinned v2 preparation without fitting, calibration, preparation or queue "
        "selection. Preserve its failed 12GiB resource receipt and exact v2 calibration binding; "
        "publish the separate v3 binding and preparation provenance."
    ),
    "publisher_budget": {
        "seconds": 7200,
        "rss_bytes": 16 * 1024**3,
        "result_bytes": 14 * 1024**3,
        "enforcement": "phase/method checkpoints; process RSS sampled every100ms",
    },
}
LIMITATIONS = [
    "All history is exposed retrospective development evidence; no untouched confirmation.",
    "Final next-period ABC is a conditional advance-report proxy, not proved early availability.",
    "Synthetic missing/error rates are sensitivity assumptions, not measured Cito accuracy.",
    "AIP excludes VAT/consumer fees; forecasts and review queues are not orders or profit.",
    "Intervals are empirical temporal coverage; paired uncertainty is exploratory and unadjusted.",
]


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative_pin(path: Path, root: Path, **extra: Any) -> dict[str, Any]:
    return {**pin(path), "path": path.relative_to(root).as_posix(), **extra}


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Relocation:
    """Resolve only explicitly relocated E3 evidence; never fall back to live originals."""

    def __init__(self, root: Path, relocate_from: Path | None = None) -> None:
        self.root = root.resolve()
        self.logical_run = Path(_json(root / "freeze-receipt.json")["protocol"]["path"]).parent
        self.logical_e3 = self.logical_run.parent
        if relocate_from is not None and relocate_from.resolve() != self.logical_e3.resolve():
            raise ResearchError("e3_relocation_original_root_mismatch")
        if self.root != self.logical_run.resolve() and relocate_from is None:
            raise ResearchError("e3_relocation_requires_explicit_root")
        self.physical_e3 = self.root.parent

    def physical(self, path: Path) -> Path:
        path = path.resolve()
        if path.is_relative_to(self.logical_run):
            return self.root / path.relative_to(self.logical_run)
        if path.is_relative_to(self.logical_e3):
            return self.physical_e3 / path.relative_to(self.logical_e3)
        return path

    def canonical_pin(self, path: Path) -> dict[str, Any]:
        result = pin(path)
        physical = path.resolve()
        if physical.is_relative_to(self.root):
            result["path"] = str(self.logical_run / physical.relative_to(self.root))
        elif physical.is_relative_to(self.physical_e3):
            result["path"] = str(self.logical_e3 / physical.relative_to(self.physical_e3))
        return result

    def verify(self, reference: dict[str, Any], *, e3: bool = True) -> Path:
        path = self.physical(Path(reference["path"])) if e3 else Path(reference["path"])
        verify_pin(path, reference)
        return path


def _amendment(root: Path, location: Relocation) -> Path:
    path = root / AMENDMENT_NAME
    if path.exists():
        value = _json(path)
        if value.get("schema_version") != "e3-publisher-amendment-v3" or value.get(
            "protocol"
        ) != location.canonical_pin(root / "protocol.json"):
            raise ResearchError("e3_publisher_amendment_changed")
        for reference in value["retained_prior"].values():
            location.verify(reference)
        if value.get("preparation") is not None:
            _prepared_snapshot(root, location, expected=value["preparation"])
    else:
        prior = {}
        for name in (
            "publisher-freeze.json",
            "publisher-producer.zip",
            "publisher-freeze-v2.json",
            "publisher-producer-v2.zip",
            "publisher-amendment-v2.json",
        ):
            if (root / name).exists():
                prior[name] = location.canonical_pin(root / name)
        prepared = None
        if (root / "derived/publication-resource.json").exists():
            prepared = _prepared_snapshot(root, location)
        value = {
            "schema_version": "e3-publisher-amendment-v3",
            "retained_prior": prior,
            "protocol": location.canonical_pin(root / "protocol.json"),
            "publisher_storage_limit_bytes": 14 * 1024**3,
            "authorization": (
                "Owner explicitly authorized publisher-only increase to 14GiB; all v2 "
                "preparation is scientific, with no disposable scratch to remove."
            ),
            "change": (
                "Add exact prepared-scoring/replay; preserve formulas, choices, counts and pins."
            ),
            "preparation": prepared,
            "exposure_statement": (
                "Before actual annual scoring; no additional fitting, calibration or queue "
                "selection; preserve the failed 12GiB attempt."
            ),
        }
        atomic_json(path, value)
    return path


class PublicationBudget:
    def __init__(
        self,
        output: Path,
        derived: Path,
        *,
        inputs: set[Path] | None = None,
        generated: set[Path] | None = None,
        receipt_path: Path | None = None,
        phase: str = "full_saved_publication",
        inherited: dict[str, Any] | None = None,
    ) -> None:
        self.output, self.derived = output, derived
        self.inputs = inputs
        self.generated = generated or set()
        self.receipt_path = receipt_path or derived / RESOURCE_NAME
        if self.receipt_path.exists():
            raise ResearchError("e3_publisher_resource_receipt_exists")
        self.phase, self.inherited = phase, inherited
        self.resource = ResourceSample()
        self.limits = dict(SPECIFICATION["publisher_budget"])
        self.result_bytes = 0
        self.input_bytes = 0
        self.generated_bytes = 0

    def __enter__(self) -> PublicationBudget:
        return self

    def check(self, *, files: bool = False) -> None:
        if (
            time.monotonic() - self.resource.started > self.limits["seconds"]
            or self.resource.peak > self.limits["rss_bytes"]
        ):
            raise ResearchError("e3_publisher_resource_budget_exceeded")
        if files:
            roots = (
                {self.output.resolve()}
                if self.inputs is not None
                else {self.output.resolve(), self.derived.resolve()}
            )
            paths = {path.resolve() for root in roots for path in root.rglob("*") if path.is_file()}
            paths.update(path.resolve() for path in self.generated if path.is_file())
            inputs = {path.resolve() for path in self.inputs or set()}
            paths.update(inputs)
            self.input_bytes = sum(path.stat().st_size for path in inputs)
            self.generated_bytes = sum(path.stat().st_size for path in paths - inputs)
            self.result_bytes = sum(path.stat().st_size for path in paths)
            if self.result_bytes > self.limits["result_bytes"]:
                raise ResearchError("e3_publisher_result_budget_exceeded")

    def __exit__(self, kind: Any, error: Any, traceback: Any) -> None:
        resource = self.resource.finish()
        resource.pop("threads", None)
        atomic_json(
            self.receipt_path,
            {
                "status": "failed" if error is not None else "completed",
                "error": str(error) if error else None,
                "limits": self.limits,
                "result_bytes": self.result_bytes,
                "execution_phase": self.phase,
                "referenced_prepared_bytes": self.input_bytes,
                "generated_bytes": self.generated_bytes,
                "accounting": (
                    "Each resolved prepared/provenance input and generated output path once; "
                    "shared source/model files excluded; final metadata has a 1MiB reserve."
                ),
                "inherited_preparation": self.inherited,
                "resource": {
                    **resource,
                    "memory_scope": "Saved-only publisher process; 100ms samples; no models/DB",
                },
            },
        )


@lru_cache(maxsize=256)
def _bootstrap_indices(length: int, block: int) -> np.ndarray:
    rng = np.random.default_rng(SEED)
    starts = rng.integers(0, length, size=(2000, math.ceil(length / block)))
    indices = ((starts[:, :, None] + np.arange(block)) % length).reshape(2000, -1)[:, :length]
    indices.flags.writeable = False
    return indices


def _interval(values: np.ndarray, block: int) -> dict[str, Any]:
    values = np.asarray(values, dtype=float)
    if len(values) < 2 or not np.isfinite(values).all():
        return paired_interval(values, seed=SEED, block=block)
    means = values[_bootstrap_indices(len(values), block)].mean(axis=1)
    return {
        "mean_difference": float(values.mean()),
        "lower": float(np.quantile(means, 0.025)),
        "upper": float(np.quantile(means, 0.975)),
        "periods": len(values),
        "block_length": block,
        "replications": 2000,
        "seed": SEED,
        "interval_method": "paired circular temporal block bootstrap",
    }


def freeze_publisher(root: Path, *, relocate_from: Path | None = None) -> Path:
    """This is safe while fitting runs: it reads protocol/code, never trial results."""
    location = Relocation(root, relocate_from)
    receipt_path = root / FREEZE_NAME
    protocol_path = root / "protocol.json"
    verify_pin(protocol_path, _json(root / "freeze-receipt.json")["protocol"])
    source = Path(__file__).parent
    for name, expected_hash in _json(protocol_path).get("code", {}).items():
        if digest(source / name) != expected_hash:
            raise ResearchError("e3_frozen_producer_changed")
    code = {
        name: digest(source / name)
        for name in (
            "e3_publish.py",
            "e3_methods.py",
            "e3_experiment.py",
            "e3_data.py",
            "e2_metrics.py",
        )
    }
    amendment = _amendment(root, location)
    expected = {
        "protocol": location.canonical_pin(protocol_path),
        "specification": SPECIFICATION,
        "code": code,
        "amendment": location.canonical_pin(amendment),
    }
    archive = root / ARCHIVE_NAME
    if receipt_path.exists():
        verify_pin(archive, _json(receipt_path)["publisher_source"])
        expected["publisher_source"] = location.canonical_pin(archive)
        if _json(receipt_path) != expected:
            raise ResearchError("e3_publisher_changed_after_freeze")
    else:
        if archive.exists():
            raise ResearchError("e3_incomplete_publisher_freeze")
        repo = source.parents[2]
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as saved:
            for file in [
                *sorted((repo / "src").rglob("*.py")),
                *sorted((repo / "src").rglob("*.sql")),
                repo / "pyproject.toml",
                repo / "uv.lock",
            ]:
                saved.write(file, file.relative_to(repo).as_posix())
        expected["publisher_source"] = location.canonical_pin(archive)
        atomic_json(receipt_path, expected)
    return receipt_path


def origin_masks(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    """No later outcome, future code, or model score participates in a subgroup."""
    equal = frame.peer_count.ge(1).fillna(False).to_numpy(dtype=bool)
    zero = frame.peer_count.eq(0).fillna(False).to_numpy(dtype=bool)
    broad = (
        (
            frame.broad_peer_count.ge(1)
            & frame.broad_peer_feature_state.isin(
                ["complete_same_unit_peers", "partial_same_unit_peers"]
            )
        )
        .fillna(False)
        .to_numpy(dtype=bool)
    )
    price = frame.aip_ore.to_numpy(dtype=float) / 100
    return {
        "all": np.ones(len(frame), dtype=bool),
        "equal_supported": equal,
        "equal_zero": zero,
        "equal_unsupported": ~(equal | zero),
        "broad_supported": broad,
        "broad_different_size": broad
        & frame.broad_peer_different_size_count.ge(1).fillna(False).to_numpy(dtype=bool),
        "matched_equal_broad": equal & broad,
        "price_under100": price < 100,
        "price_100_1000": (price >= 100) & (price < 1000),
        "price_1000plus": price >= 1000,
        "history_under6": frame.history_periods.lt(6).to_numpy(dtype=bool),
        "history_established": frame.history_periods.ge(6).to_numpy(dtype=bool),
    }


def route_predictions(
    package: pd.DataFrame, equal: pd.DataFrame, broad: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    masks = origin_masks(package)
    routed_equal = fallback(package, equal, masks["equal_supported"])
    routed_equal["route"] = np.where(masks["equal_supported"], "equal", "package")
    routed_broad = fallback(routed_equal, broad, masks["broad_supported"])
    routed_broad["route"] = np.where(masks["broad_supported"], "broad", routed_equal.route)
    return routed_equal, routed_broad


def transition_reference(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    direction = np.where(
        out.abc.isin(["A", "B"]) & out.advance_abc.eq("C"),
        1,
        np.where(out.abc.eq("C") & out.advance_abc.eq("A"), -1, 0),
    )
    out[PROBABILITIES] = np.eye(3)[direction + 1]
    return out


def _dates(frame: pd.DataFrame) -> pd.DataFrame:
    for column in ("period_start", "target_period", "next_maturity_date"):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column])
    return frame


def _cohort(root: Path, fold: str, role: str) -> pd.DataFrame:
    directory = root / "cohorts" / fold
    manifest = _json(directory / "manifest.json")
    verify_pin(directory / f"{role}.parquet", manifest[role])
    result = _dates(pd.read_parquet(directory / f"{role}.parquet"))
    if result.row_key.isna().any() or result.row_key.duplicated().any():
        raise ResearchError("e3_duplicate_cohort_keys")
    return result


def _source_binding(
    root: Path,
    protocol: dict[str, Any],
    location: Relocation | None = None,
    *,
    publisher_name: str | None = None,
) -> dict[str, Any]:
    location = location or Relocation(root)
    verify_pin(root / "selection.json", _json(root / "selection-receipt.json")["selection"])
    selection = _json(root / "selection.json")
    if (
        selection["protocol_sha256"] != digest(root / "protocol.json")
        or selection["selected_before_evaluation"] is not True
    ):
        raise ResearchError("e3_publish_selection_binding_changed")
    for role, reference in protocol["pins"].items():
        physical = location.verify(reference, e3=role in {"advance", "broader"})
        if role in {"advance", "broader"}:
            for file in _json(physical).get("files", []):
                logical = Path(file["path"])
                if not logical.is_absolute():
                    logical = Path(reference["path"]).parent / logical
                verify_pin(location.physical(logical), file)
    if "producer" in protocol:
        location.verify(protocol["producer"])
    ledger = _json(root / "trial-ledger.json")["trials"]
    expected = {
        f"{fold['name']}-{spec['method']}"
        for fold in protocol["folds"]
        for spec in protocol["direction"]
    }
    for fold in protocol["folds"]:
        formulations = (
            ("delta_floor", "log_ratio")
            if fold["name"] == "validation"
            else (selection["monetary_formulation"],)
        )
        expected.update(
            f"{fold['name']}-amount_{formulation}_{info}"
            for formulation in formulations
            for info in ("current", "advance")
        )
    if {trial["trial_id"] for trial in ledger} != expected or len(ledger) != len(expected):
        raise ResearchError("e3_publish_trial_inventory_incomplete")
    for trial in ledger:
        actual = _json(root / "trials" / trial["trial_id"] / "trial.json")
        if trial != actual or trial["protocol_sha256"] != digest(root / "protocol.json"):
            raise ResearchError("e3_publish_trial_binding_changed")
        if trial["status"] not in {"completed", "failed"}:
            raise ResearchError("e3_publish_unfinished_trial")
    execution = _json(root / "execution.json")
    if execution["status"] != "completed" or execution["trials"] != len(ledger):
        raise ResearchError("e3_publish_execution_incomplete")
    return {
        "protocol": location.canonical_pin(root / "protocol.json"),
        "selection": location.canonical_pin(root / "selection.json"),
        "ledger": location.canonical_pin(root / "trial-ledger.json"),
        "execution": location.canonical_pin(root / "execution.json"),
        "cohorts": {
            fold["name"]: location.canonical_pin(root / "cohorts" / fold["name"] / "manifest.json")
            for fold in protocol["folds"]
        },
        "publisher": location.canonical_pin(root / (publisher_name or FREEZE_NAME)),
    }


def _previous(root: Path, year: int, method: str) -> pd.DataFrame:
    prior = year - 1
    fold = "validation" if prior == 2023 else f"evaluation_{prior}"
    frame = _dates(read_prediction(root, fold, method, outcomes=True))
    return frame.loc[frame.period_start.dt.year == prior].copy()


def _prepared_expected_paths(
    protocol: dict[str, Any], selection: dict[str, Any]
) -> tuple[set[str], set[str]]:
    predictions: set[str] = set()
    queues: set[str] = set()
    chosen = ["e2_top10", "raw_top10", "calibrated_top10"] + [
        item["policy"]
        for item in selection["queue_policies"]["choices"]
        if item["status"] == "selected"
    ]
    for fold in protocol["folds"][1:]:
        name = fold["name"]
        methods = [
            ("e2_direction_" + method, "perfect")
            for method in ("no_change", "historical_frequency", "repeat_last")
        ]
        methods += [
            ("e2_magnitude_" + method, "perfect")
            for method in ("no_change", "historical_median", "catboost_group_mae")
        ]
        for spec in protocol["direction"]:
            methods += [
                (spec["method"], scenario)
                for scenario in (SCENARIOS if spec["advance"] else ("perfect",))
            ]
        for info in ("current", "advance"):
            scenarios = SCENARIOS if info == "advance" else ("perfect",)
            for scenario in scenarios:
                methods += [(f"fallback_{peer}_{info}", scenario) for peer in ("equal", "broad")]
                methods.append((f"amount_{selection['monetary_formulation']}_{info}", scenario))
        for scenario in SCENARIOS:
            methods += [
                ("cb_equal_advance_calibrated", scenario),
                ("abc_transition_rule", scenario),
            ]
            for policy in chosen:
                queues.add(f"queues/{name}--{scenario}--{policy}--natural.parquet")
                for reference in ("e2", "raw", "calibrated"):
                    queues.add(
                        f"queues/{name}--{scenario}--{policy}_matched_{reference}--matched_count.parquet"
                    )
            for kind, grid in (("risk_rank", LAMBDAS), ("threshold", THRESHOLDS)):
                queues.update(
                    f"queues/{name}--{scenario}--grid_{kind}_{index}--descriptive_grid.parquet"
                    for index in range(len(grid))
                )
        predictions.update(
            f"predictions/{name}--{method}--{scenario}.parquet" for method, scenario in methods
        )
    return predictions, queues


def _prepared_member(derived: Path, item: dict[str, Any], prefix: str) -> Path:
    relative = item.get("path")
    if (
        not isinstance(relative, str)
        or "\\" in relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ResearchError("e3_prepared_path_escape")
    path = derived / relative
    if (
        not relative.startswith(prefix + "/")
        or not path.resolve().is_relative_to((derived / prefix).resolve())
        or path.suffix != ".parquet"
    ):
        raise ResearchError("e3_prepared_path_escape")
    return path


def _prepared_snapshot(
    root: Path, location: Relocation, *, expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    derived = root / "derived"
    v2 = _json(root / "publisher-freeze-v2.json")
    if v2.get("specification", {}).get("version") != "e3-publisher-v2":
        raise ResearchError("e3_prepared_v2_freeze_missing")
    location.verify(v2["publisher_source"])
    if "amendment" in v2:
        location.verify(v2["amendment"])
    verify_pin(root / "protocol.json", v2["protocol"])
    core_names = {
        "e3_publish.py",
        "e3_methods.py",
        "e3_experiment.py",
        "e3_data.py",
        "e2_metrics.py",
    }
    if set(v2.get("code", {})) != core_names or any(
        digest(Path(__file__).parent / name) != value
        for name, value in v2["code"].items()
        if name != "e3_publish.py"
    ):
        raise ResearchError("e3_prepared_scientific_core_changed")
    failed = _json(derived / "publication-resource.json")
    if (
        failed.get("status") != "failed"
        or failed.get("error") != "e3_publisher_result_budget_exceeded"
        or failed.get("limits", {}).get("result_bytes") != 12 * 1024**3
    ):
        raise ResearchError("e3_prepared_failure_not_storage_only_v2")
    protocol, selection = _json(root / "protocol.json"), _json(root / "selection.json")
    binding_v2 = _source_binding(
        root, protocol, location, publisher_name="publisher-freeze-v2.json"
    )
    calibration_receipt = _json(derived / "calibration-receipt.json")
    if calibration_receipt.get("source_binding") != binding_v2:
        raise ResearchError("e3_prepared_original_calibration_binding_changed")
    verify_pin(derived / "calibration.json", calibration_receipt["calibration"])
    roles = {
        "v2_freeze": root / "publisher-freeze-v2.json",
        "v2_source": location.physical(Path(v2["publisher_source"]["path"])),
        "failed_resource": derived / "publication-resource.json",
        "prediction_inventory": derived / "prediction-inventory.json",
        "queue_inventory": derived / "queue-inventory.json",
        "calibration": derived / "calibration.json",
        "calibration_receipt": derived / "calibration-receipt.json",
        "scenario_input_coverage": derived / "scenario-input-coverage.json",
        "candidate_review": root / "saved/candidate_review.parquet",
    }
    snapshot = {
        "schema_version": "e3-v2-prepared-inputs-v1",
        "files": {role: location.canonical_pin(path) for role, path in roles.items()},
        "original_v2_binding": binding_v2,
    }
    if expected is not None and snapshot != expected:
        raise ResearchError("e3_prepared_seal_changed")
    inventories = [_json(roles["prediction_inventory"]), _json(roles["queue_inventory"])]
    if inventories[1].get("outcomes_attached") is not False:
        raise ResearchError("e3_prepared_queue_outcomes_attached")
    wanted = _prepared_expected_paths(protocol, selection)
    for inventory, prefix, paths in zip(
        inventories, ("predictions", "queues"), wanted, strict=True
    ):
        items = inventory.get("files", [])
        if {item.get("path") for item in items} != paths or len(items) != len(paths):
            raise ResearchError("e3_prepared_inventory_incomplete")
        actual = {path.relative_to(derived).as_posix() for path in (derived / prefix).iterdir()}
        if actual != paths:
            raise ResearchError("e3_prepared_unlisted_file")
        for item in items:
            verify_pin(_prepared_member(derived, item, prefix), item)
    return snapshot


def _prepared_input_paths(root: Path, location: Relocation, snapshot: dict[str, Any]) -> set[Path]:
    paths = {location.physical(Path(item["path"])) for item in snapshot["files"].values()}
    for name, prefix in (
        ("prediction-inventory.json", "predictions"),
        ("queue-inventory.json", "queues"),
    ):
        paths.update(
            _prepared_member(root / "derived", item, prefix)
            for item in _json(root / "derived" / name)["files"]
        )
    paths.update(root / name for name in (FREEZE_NAME, ARCHIVE_NAME, AMENDMENT_NAME))
    return paths


def _prepared_queues(derived: Path) -> list[dict[str, Any]]:
    queues = _json(derived / "queue-inventory.json")["files"]
    # v2's threshold grid used tuples in memory; JSON stores arrays. Restore
    # that presentation type only so its saved parameter strings stay exact.
    for item in queues:
        if item["workload_mode"] == "descriptive_grid" and item["policy"].startswith(
            "grid_threshold_"
        ):
            item["parameter"] = tuple(item["parameter"])
    return queues


def calibration_states(
    root: Path, derived: Path, selection: dict[str, Any], binding: dict[str, Any]
) -> dict[str, Any]:
    original = root / "derived" / "calibration.json"
    receipt = root / "derived" / "calibration-receipt.json"
    if original.exists():
        saved = _json(receipt)
        verify_pin(original, saved["calibration"])
        if saved["source_binding"] != binding:
            raise ResearchError("e3_calibration_replay_binding_changed")
        state = _json(original)
    else:
        state = {
            "temperature_2023": selection["calibration_2023"],
            "temperature": {},
            "residual": {},
        }
        for year in (2024, 2025, 2026):
            cutoff = f"{year}-01-01"
            state["temperature"][str(year)] = temperature_fit(
                _previous(root, year, "cb_equal_advance"), cutoff
            )
            for info in ("current", "advance"):
                method = f"amount_{selection['monetary_formulation']}_{info}"
                state["residual"][f"{year}_{info}"] = residual_fit(
                    _previous(root, year, method), cutoff
                )
    atomic_json(derived / "calibration.json", state)
    atomic_json(
        derived / "calibration-receipt.json",
        {
            "calibration": pin(derived / "calibration.json"),
            "source_binding": binding,
            "temperature_fits_total": 4,
            "residual_calibrations": 6,
            "replay_reuses_saved_states": original.exists()
            and original.resolve() != (derived / "calibration.json").resolve(),
        },
    )
    return state


def _e2_trial(
    protocol: dict[str, Any], fold: str, task: str, method: str
) -> tuple[Path, dict[str, Any]]:
    ledger_pin = protocol["pins"]["e2_ledger"]
    verify_pin(Path(ledger_pin["path"]), ledger_pin)
    trial_id = f"{fold}-{task}-{method}"
    matches = [
        trial
        for trial in _json(Path(ledger_pin["path"]))["trials"]
        if trial["trial_id"] == trial_id
    ]
    if len(matches) != 1 or matches[0]["status"] != "completed":
        raise ResearchError("e3_missing_e2_reference_trial")
    trial = matches[0]
    directory = Path(ledger_pin["path"]).parent / "trials" / trial_id
    if _json(directory / "trial.json") != trial:
        raise ResearchError("e3_e2_trial_receipt_changed")
    return directory, trial


def _read_e2(
    protocol: dict[str, Any], fold: str, task: str, method: str, role: str = "predictions"
) -> pd.DataFrame:
    directory, trial = _e2_trial(protocol, fold, task, method)
    file = next(item for item in trial["files"] if item["role"] == role)
    path = directory / file["path"]
    verify_pin(path, file)
    frame = _dates(pd.read_parquet(path))
    if task == "magnitude" and role == "predictions":
        # Preserve original E2 including its negative implied next prices.
        frame["next_aip_dkk"] = frame.aip_ore / 100 + frame.prediction_dkk
    return frame


def _assert_alignment(left: pd.DataFrame, right: pd.DataFrame, columns: list[str]) -> None:
    if any(
        column not in left or column not in right or not same_values(left[column], right[column])
        for column in columns
    ):
        raise ResearchError("e3_comparison_cohort_or_outcomes_changed")


def reconcile_e2(root: Path, protocol: dict[str, Any]) -> dict[str, Any]:
    keys, outcomes, periods = hashlib.sha256(), hashlib.sha256(), []
    for fold in protocol["folds"][1:]:
        name = fold["name"]
        features, labels = _cohort(root, name, "features"), _cohort(root, name, "outcomes")
        joined = attach_outcomes(features[list(KEYS)], labels)
        if set(features.period_start.dt.strftime("%Y-%m-%d")) != {
            row["start"] for row in protocol["exact_origins"][name]
        }:
            raise ResearchError("e3_declared_origins_changed")
        for task, names in (
            ("direction", ["no_change", "historical_frequency", "repeat_last", "catboost_group"]),
            ("magnitude", ["no_change", "historical_median", "catboost_group_mae"]),
        ):
            for method in names:
                old = _read_e2(protocol, name, task, method, "outcomes")
                _assert_alignment(
                    joined,
                    old,
                    [
                        *KEYS,
                        *OUTCOMES,
                        "next_delta_ore",
                        "next_maturity_index",
                        "next_maturity_date",
                    ],
                )
                _assert_alignment(features, old, [*KEYS, "aip_ore"])
        for digest_state, values in (
            (keys, features[list(KEYS)]),
            (
                outcomes,
                joined[
                    [
                        "row_key",
                        *OUTCOMES,
                        "next_delta_ore",
                        "next_maturity_index",
                        "next_maturity_date",
                    ]
                ],
            ),
        ):
            for row in values.astype(str).itertuples(index=False, name=None):
                digest_state.update(json.dumps(row, separators=(",", ":")).encode())
                digest_state.update(b"\n")
        periods.extend(sorted(features.period_start.dt.strftime("%Y-%m-%d").unique()))
    return {
        "target": "next actual calendar-period AIP DKK/package and decrease/unchanged/increase",
        "periods_sha256": _hash(periods),
        "candidate_keys_sha256": keys.hexdigest(),
        "outcomes_sha256": outcomes.hexdigest(),
        "verified": True,
        "e2_manifest": protocol["pins"]["e2_display"],
    }


def _store_prediction(
    derived: Path,
    inventory: list[dict[str, Any]],
    fold: str,
    method: str,
    scenario: str,
    task: str,
    frame: pd.DataFrame,
    *,
    information: str = "current",
    peer: str = "equal",
) -> None:
    # Identity/date fields live once in the pinned origin cohort. The immutable
    # row key binds these compact derived values without repeating wide keys.
    columns = ["row_key", *(PROBABILITIES if task == "direction" else ["next_aip_dkk"])]
    if task == "direction":
        if "selected" not in frame:
            frame = select_queue(frame, "top10")
        columns.append("selected")
    columns += [column for column in ("route", "lower90_dkk", "upper90_dkk") if column in frame]
    if any(column in frame for column in OUTCOMES):
        raise ResearchError("e3_prediction_contains_outcomes")
    destination = derived / "predictions" / f"{fold}--{method}--{scenario}.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame[columns].to_parquet(destination, index=False)
    inventory.append(
        _relative_pin(
            destination,
            derived,
            fold=fold,
            method=method,
            scenario=scenario,
            task=task,
            information="advance_abc_historical_proxy"
            if information == "advance"
            else "current_abc",
            peer_set=peer,
        )
    )


def _store_queue(
    derived: Path,
    inventory: list[dict[str, Any]],
    fold: str,
    method: str,
    scenario: str,
    policy: str,
    frame: pd.DataFrame,
    *,
    parameter: Any = None,
    mode: str = "natural",
    reference: str | None = None,
    chosen: bool = True,
) -> None:
    if any(column in frame for column in OUTCOMES):
        raise ResearchError("e3_queue_contains_outcomes")
    destination = derived / "queues" / f"{fold}--{scenario}--{policy}--{mode}.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The probability evidence is separately frozen in prediction-inventory.
    columns = ["row_key", "selected", "selection_reason"]
    frame[columns].to_parquet(destination, index=False)
    inventory.append(
        _relative_pin(
            destination,
            derived,
            fold=fold,
            method=method,
            scenario=scenario,
            policy=policy,
            parameter=parameter,
            workload_mode=mode,
            reference=reference,
            chosen=chosen,
        )
    )


def _review(
    frame: pd.DataFrame, amount: pd.DataFrame, queue: pd.DataFrame, scenario: str, policy: str
) -> pd.DataFrame:
    _assert_alignment(frame, amount, list(KEYS))
    _assert_alignment(frame, queue, list(KEYS))
    out = frame[
        [
            "row_key",
            "package_number",
            "period_start",
            "target_period",
            "abc",
            "advance_abc",
            "advance_state",
            "advance_source",
            "aip_ore",
            "peer_feature_state",
        ]
    ].copy()
    out.rename(
        columns={"advance_source": "source", "peer_feature_state": "peer_support"}, inplace=True
    )
    out["current_aip_dkk"] = out.pop("aip_ore") / 100
    out[["p_decrease", "p_unchanged", "p_increase"]] = frame[PROBABILITIES].to_numpy()
    for column in ("next_aip_dkk", "lower90_dkk", "upper90_dkk"):
        out[column] = amount[column].to_numpy()
    out["delta_dkk"] = out.next_aip_dkk - out.current_aip_dkk
    out["scenario"], out["policy"] = scenario, policy
    out["selected"] = queue.selected.to_numpy()
    out["selection_reason"] = queue.selection_reason.to_numpy()
    out["limitations"] = (
        "Historical final next ABC proxy; predicted AIP conditional median; review only"
    )
    return out


def scenario_input_coverage(
    original: pd.DataFrame, changed: pd.DataFrame, *, fold: str, scenario: str
) -> dict[str, Any]:
    _assert_alignment(original, changed, list(KEYS))
    available = original.advance_abc.isin(["A", "B", "C"])
    retained = changed.advance_abc.isin(["A", "B", "C"])
    removed = available & ~retained
    incorrect = available & retained & original.advance_abc.ne(changed.advance_abc)
    return {
        "split": fold,
        "scenario": scenario,
        "candidates": len(original),
        "original_available_advance_codes": int(available.sum()),
        "original_unavailable_advance_codes": int((~available).sum()),
        "retained_codes": int((available & retained).sum()),
        "removed_codes": int(removed.sum()),
        "incorrect_codes": int(incorrect.sum()),
        "unavailable_codes": int((~retained).sum()),
        "changed_fraction_among_original_available": int((removed | incorrect).sum())
        / int(available.sum())
        if available.any()
        else None,
    }


def prepare_predictions(
    root: Path,
    derived: Path,
    output: Path,
    protocol: dict[str, Any],
    selection: dict[str, Any],
    states: dict[str, Any],
    checkpoint: Callable[[], None] = lambda: None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    predictions: list[dict[str, Any]] = []
    queues: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    writer: pq.ParquetWriter | None = None
    trials = {trial["trial_id"]: trial for trial in _json(root / "trial-ledger.json")["trials"]}

    def load(fold: str, method: str, scenario: str = "perfect") -> pd.DataFrame:
        if trials[f"{fold}-{method}"]["status"] != "completed":
            raise ResearchError("e3_required_review_or_comparison_trial_failed")
        return _dates(read_prediction(root, fold, method, scenario))

    try:
        for fold in protocol["folds"][1:]:
            name, year = fold["name"], fold["start"][:4]
            # Exact original E2 references remain independent of new constraints/ranking.
            for task, names in (
                ("direction", ["no_change", "historical_frequency", "repeat_last"]),
                ("magnitude", ["no_change", "historical_median", "catboost_group_mae"]),
            ):
                for method in names:
                    value = _read_e2(protocol, name, task, method)
                    _store_prediction(
                        derived, predictions, name, f"e2_{task}_{method}", "perfect", task, value
                    )
            for spec in protocol["direction"]:
                if not spec["advance"]:
                    value = load(name, spec["method"])
                    _store_prediction(
                        derived,
                        predictions,
                        name,
                        spec["method"],
                        "perfect",
                        "direction",
                        value,
                        peer=spec["peer"],
                    )
            current = [load(name, f"cb_{peer}_current") for peer in ("package", "equal", "broad")]
            for peer, value in zip(("equal", "broad"), route_predictions(*current), strict=True):
                _store_prediction(
                    derived,
                    predictions,
                    name,
                    f"fallback_{peer}_current",
                    "perfect",
                    "direction",
                    value,
                    peer=peer,
                )
            current_amount = load(name, f"amount_{selection['monetary_formulation']}_current")
            current_amount = intervals(current_amount, states["residual"][f"{year}_current"])
            _store_prediction(
                derived,
                predictions,
                name,
                f"amount_{selection['monetary_formulation']}_current",
                "perfect",
                "magnitude",
                current_amount,
            )
            e2_reference = _read_e2(protocol, name, "direction", "catboost_group")
            for scenario in SCENARIOS:
                checkpoint()
                values: dict[str, pd.DataFrame] = {}
                for spec in protocol["direction"]:
                    if not spec["advance"]:
                        continue
                    value = load(name, spec["method"], scenario)
                    values[spec["method"]] = value
                    _store_prediction(
                        derived,
                        predictions,
                        name,
                        spec["method"],
                        scenario,
                        "direction",
                        value,
                        information="advance",
                        peer=spec["peer"],
                    )
                advance = [values[f"cb_{peer}_advance"] for peer in ("package", "equal", "broad")]
                for peer, value in zip(
                    ("equal", "broad"), route_predictions(*advance), strict=True
                ):
                    _store_prediction(
                        derived,
                        predictions,
                        name,
                        f"fallback_{peer}_advance",
                        scenario,
                        "direction",
                        value,
                        information="advance",
                        peer=peer,
                    )
                calibrated = perturb(values["cb_equal_advance"], scenario)
                coverage.append(
                    scenario_input_coverage(
                        values["cb_equal_advance"], calibrated, fold=name, scenario=scenario
                    )
                )
                calibrated[PROBABILITIES] = calibrate(
                    calibrated[PROBABILITIES].to_numpy(), states["temperature"][year]
                )
                _store_prediction(
                    derived,
                    predictions,
                    name,
                    "cb_equal_advance_calibrated",
                    scenario,
                    "direction",
                    calibrated,
                    information="advance",
                    peer="equal",
                )
                _store_prediction(
                    derived,
                    predictions,
                    name,
                    "abc_transition_rule",
                    scenario,
                    "direction",
                    transition_reference(calibrated),
                    information="advance",
                    peer="package",
                )
                amount = load(name, f"amount_{selection['monetary_formulation']}_advance", scenario)
                amount = intervals(amount, states["residual"][f"{year}_advance"])
                _store_prediction(
                    derived,
                    predictions,
                    name,
                    f"amount_{selection['monetary_formulation']}_advance",
                    scenario,
                    "magnitude",
                    amount,
                    information="advance",
                    peer="equal",
                )
                original = e2_reference.copy()
                original["selection_reason"] = np.where(
                    original.selected,
                    "Original pinned E2 top10 rise ranking",
                    "Outside original E2 top10",
                )
                chosen = [
                    ("e2_top10", "cb_equal_current", original, None),
                    (
                        "raw_top10",
                        "cb_equal_advance",
                        select_queue(values["cb_equal_advance"], "top10"),
                        None,
                    ),
                    (
                        "calibrated_top10",
                        "cb_equal_advance_calibrated",
                        select_queue(calibrated, "top10"),
                        None,
                    ),
                ]
                for choice in selection["queue_policies"]["choices"]:
                    if choice["status"] == "selected":
                        chosen.append(
                            (
                                choice["policy"],
                                "cb_equal_advance_calibrated",
                                select_queue(
                                    calibrated, choice["policy"], choice["selected_parameter"]
                                ),
                                choice["selected_parameter"],
                            )
                        )
                policy_queues = {policy: queue for policy, _, queue, _ in chosen}
                review_policy = next(
                    policy
                    for policy in ("risk_rank", "threshold", "calibrated_top10")
                    if policy in policy_queues
                )
                for policy, method, queue, parameter in chosen:
                    _store_queue(
                        derived, queues, name, method, scenario, policy, queue, parameter=parameter
                    )
                    if scenario == "perfect" and policy == review_policy:
                        review = _review(calibrated, amount, queue, scenario, policy)
                        review["review_policy_basis"] = (
                            "2023 validation; risk, threshold, then top10"
                        )
                        if review_policy == "calibrated_top10":
                            review["selection_reason"] = (
                                "No useful validated risk/threshold policy; "
                                + review.selection_reason
                            )
                        for auxiliary, source_policy in (
                            ("original_top10_selected", "e2_top10"),
                            ("raw_top10_selected", "raw_top10"),
                            ("calibrated_top10_selected", "calibrated_top10"),
                            ("risk_selected", "risk_rank"),
                            ("threshold_selected", "threshold"),
                        ):
                            review[auxiliary] = (
                                policy_queues[source_policy].selected.to_numpy()
                                if source_policy in policy_queues
                                else pd.array([None] * len(review), dtype="boolean")
                            )
                        table = pa.Table.from_pandas(review, preserve_index=False)
                        if writer is None:
                            writer = pq.ParquetWriter(
                                output / "candidate_review.parquet",
                                table.schema,
                                compression="zstd",
                            )
                        writer.write_table(table.cast(writer.schema))
                    workload = {
                        str(day)[:10]: int(part.selected.sum())
                        for day, part in queue.groupby("period_start")
                    }
                    for reference, source in (
                        ("e2", e2_reference),
                        ("raw", values["cb_equal_advance"]),
                        ("calibrated", calibrated),
                    ):
                        matched = select_queue(source, "top10", workload=workload)
                        _store_queue(
                            derived,
                            queues,
                            name,
                            reference,
                            scenario,
                            policy + "_matched_" + reference,
                            matched,
                            parameter=parameter,
                            mode="matched_count",
                            reference=policy,
                        )
                for kind, grid in (("risk_rank", LAMBDAS), ("threshold", THRESHOLDS)):
                    for index, parameter in enumerate(grid):
                        queue = select_queue(calibrated, kind, parameter)
                        _store_queue(
                            derived,
                            queues,
                            name,
                            "cb_equal_advance_calibrated",
                            scenario,
                            f"grid_{kind}_{index}",
                            queue,
                            parameter=parameter,
                            mode="descriptive_grid",
                            chosen=False,
                        )
    finally:
        if writer is not None:
            writer.close()
    atomic_json(derived / "prediction-inventory.json", {"files": predictions})
    atomic_json(derived / "queue-inventory.json", {"files": queues, "outcomes_attached": False})
    for scenario in SCENARIOS:
        parts = [row for row in coverage if row["scenario"] == scenario]
        total = {
            key: sum(row[key] for row in parts)
            for key in (
                "candidates",
                "original_available_advance_codes",
                "original_unavailable_advance_codes",
                "retained_codes",
                "removed_codes",
                "incorrect_codes",
                "unavailable_codes",
            )
        }
        available = total["original_available_advance_codes"]
        coverage.append(
            {
                "split": "evaluation_all",
                "scenario": scenario,
                **total,
                "changed_fraction_among_original_available": (
                    total["removed_codes"] + total["incorrect_codes"]
                )
                / available
                if available
                else None,
            }
        )
    atomic_json(
        derived / "scenario-input-coverage.json",
        {
            "rows": coverage,
            "definitions": SPECIFICATION["scenario_input_coverage"],
            "feature_only": True,
            "outcomes_read": False,
        },
    )
    return predictions, queues


def _load_saved(derived: Path, entries: list[dict[str, Any]]) -> pd.DataFrame:
    parts = []
    for entry in entries:
        path = derived / entry["path"]
        verify_pin(path, entry)
        parts.append(_dates(pd.read_parquet(path)))
    return pd.concat(parts, ignore_index=True)


def metric_rows(
    frame: pd.DataFrame,
    task: str,
    metadata: dict[str, Any],
    *,
    period_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Primary metrics weight tariff periods equally, never package rows."""

    def score(value: pd.DataFrame) -> dict[str, Any]:
        if task != "direction":
            metrics = amount_metrics(value)
            if "lower90_dkk" in value:
                lower, upper = value.lower90_dkk, value.upper90_dkk
                metrics["invalid_intervals90"] = int(
                    (
                        ~np.isfinite(lower) | ~np.isfinite(upper) | (lower < 0) | (lower > upper)
                    ).sum()
                )
            return metrics
        metrics = direction_metrics(value)
        if "selected" in value:
            selected = value.loc[value.selected]
            known = selected.loc[selected.next_status.eq("observed")]
            metrics.update(
                {
                    "shortlist_selected": len(selected),
                    "shortlist_observed": len(known),
                    "rise_precision_top10": float(known.next_direction.eq(1).mean())
                    if len(known)
                    else None,
                    "fall_fraction_top10": float(known.next_direction.eq(-1).mean())
                    if len(known)
                    else None,
                    "shortlist_unknown_fraction": 1 - len(known) / len(selected)
                    if len(selected)
                    else None,
                }
            )
        return metrics

    per = period_rows
    if per is None:
        per = []
        for day, part in frame.groupby("period_start", sort=True):
            per.append(
                {**metadata, "split": "per_period", "period_start": str(day)[:10], **score(part)}
            )
    pooled = score(frame)
    metrics = {
        key
        for row in per
        for key in row
        if key
        not in {
            *metadata,
            "period_start",
            "split",
            "candidates",
            "observed",
            "invalid",
            "invalid_intervals90",
            "shortlist_selected",
            "shortlist_observed",
        }
    }
    aggregate = {
        **metadata,
        "candidates": len(frame),
        "observed": pooled["observed"],
        "periods": len(per),
        "scored_periods": sum(row["observed"] > 0 for row in per),
        **{
            key: float(np.mean([row[key] for row in per if row.get(key) is not None]))
            for key in sorted(metrics)
            if any(row.get(key) is not None for row in per)
        },
        **{
            "pooled_" + key: value
            for key, value in pooled.items()
            if key not in {"candidates", "observed"}
        },
    }
    if "invalid" in pooled:
        aggregate["invalid"] = pooled["invalid"]
    for count in ("invalid_intervals90", "shortlist_selected", "shortlist_observed"):
        if count in pooled:
            aggregate[count] = pooled[count]
    aggregate["outcome_coverage"] = pooled["observed"] / len(frame) if len(frame) else None
    return [aggregate, *per]


def paired_comparison(
    candidate: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    *,
    metric: str,
    metadata: dict[str, Any],
    allow_missing_rates: bool = False,
) -> list[dict[str, Any]]:
    a = {
        row["period_start"]: row[metric]
        for row in candidate
        if "period_start" in row and row.get(metric) is not None
    }
    b = {
        row["period_start"]: row[metric]
        for row in baseline
        if "period_start" in row and row.get(metric) is not None
    }
    if set(a) != set(b) and not allow_missing_rates:
        raise ResearchError("e3_paired_scored_origins_changed")
    common = set(a) & set(b)
    if allow_missing_rates:
        origins = {row["period_start"] for row in [*candidate, *baseline] if "period_start" in row}
        metadata = {
            **metadata,
            "total_origin_periods": len(origins),
            "joint_rate_periods": len(common),
            "periods_without_joint_rate": len(origins) - len(common),
            "rate_denominator": "joint periods with observed selections; other rates unavailable",
        }
    difference = np.array([a[day] - b[day] for day in sorted(common)], dtype=float)
    if len(difference) < 2:
        return [
            {
                **metadata,
                "metric": metric,
                "status": "insufficient_paired_periods",
                "periods": len(difference),
                "lower": None,
                "upper": None,
            }
        ]
    return [
        {
            **metadata,
            "metric": metric,
            **_interval(difference, block),
        }
        for block in (4, 8)
    ]


def score_predictions(
    root: Path,
    derived: Path,
    protocol: dict[str, Any],
    inventory: list[dict[str, Any]],
    checkpoint: Callable[[], None] = lambda: None,
    *,
    outcome_destination: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str], list[dict[str, Any]]]]:
    origin = pd.concat(
        [_cohort(root, fold["name"], "features")[ORIGIN_COLUMNS] for fold in protocol["folds"][1:]],
        ignore_index=True,
    )
    labels = pd.concat(
        [_cohort(root, fold["name"], "outcomes") for fold in protocol["folds"][1:]],
        ignore_index=True,
    )
    labels = attach_outcomes(origin[list(KEYS)], labels)
    # Separate outcomes role is one row per candidate regardless of scenario/policy.
    labels[["row_key", *OUTCOMES]].to_parquet(
        outcome_destination or derived / "candidate_outcomes.parquet", index=False
    )
    rows: list[dict[str, Any]] = []
    scored: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    identities = sorted({(item["method"], item["scenario"], item["task"]) for item in inventory})
    for method, scenario, task in identities:
        checkpoint()
        entries = [
            item
            for item in inventory
            if (item["method"], item["scenario"], item["task"]) == (method, scenario, task)
        ]
        predictions = _load_saved(derived, entries)
        _assert_alignment(origin, predictions, ["row_key"])
        frame = origin.merge(predictions, on="row_key", validate="one_to_one", sort=False)
        frame = attach_outcomes(frame, labels)
        metadata = {
            "method": method,
            "label": method.replace("_", " "),
            "scenario": scenario,
            "task": task,
            "information": entries[0]["information"],
            "peer_set": entries[0]["peer_set"],
        }
        for subgroup, mask in origin_masks(origin).items():
            if scenario != "perfect" and subgroup != "all":
                continue
            part = frame.loc[mask]
            if part.empty:
                continue
            result = metric_rows(
                part, task, {**metadata, "split": "evaluation_all", "subgroup": subgroup}
            )
            scored[(method, scenario, subgroup)] = result
            rows.extend(result)
            # Annual macro and pooled results supplement, never replace, the overall row.
            for year, annual in part.groupby(part.period_start.dt.year, sort=True):
                rows.append(
                    metric_rows(
                        annual,
                        task,
                        {**metadata, "split": f"evaluation_{year}", "subgroup": subgroup},
                        period_rows=[
                            row for row in result[1:] if row["period_start"].startswith(str(year))
                        ],
                    )[0]
                )
    return rows, scored


def comparison_rows(
    scored: dict[tuple[str, str, str], list[dict[str, Any]]], formulation: str
) -> list[dict[str, Any]]:
    rows = []

    def compare(
        candidate: str,
        baseline: str,
        scenario: str,
        metric: str,
        kind: str,
        baseline_scenario: str = "perfect",
    ) -> None:
        for subgroup in sorted({key[2] for key in scored}):
            a = scored.get((candidate, scenario, subgroup))
            b = scored.get((baseline, baseline_scenario, subgroup))
            if a is None or b is None:
                continue
            rows.extend(
                paired_comparison(
                    a,
                    b,
                    metric=metric,
                    metadata={
                        "candidate": candidate,
                        "baseline": baseline,
                        "scenario": scenario,
                        "subgroup": subgroup,
                        "split": "evaluation_all",
                        "comparison": kind,
                    },
                )
            )

    for scenario in SCENARIOS:
        for stem in (
            "cb_package",
            "cb_equal",
            "cb_broad",
            "logistic_equal",
            "fallback_equal",
            "fallback_broad",
        ):
            compare(stem + "_advance", stem + "_current", scenario, "brier", "advance_information")
        compare(
            "cb_equal_advance",
            "logistic_equal_advance",
            scenario,
            "brier",
            "same_information_algorithm",
            scenario,
        )
        compare(
            "cb_equal_advance",
            "cb_package_advance",
            scenario,
            "brier",
            "equal_peer_increment",
            scenario,
        )
        compare(
            "cb_broad_advance",
            "cb_equal_advance",
            scenario,
            "brier",
            "broader_peer_increment",
            scenario,
        )
        compare(
            "cb_equal_advance_calibrated",
            "cb_equal_advance",
            scenario,
            "brier",
            "chronological_calibration",
            scenario,
        )
        for peer in ("equal", "broad"):
            compare(
                f"fallback_{peer}_advance",
                f"cb_{peer}_advance",
                scenario,
                "brier",
                "fixed_fallback",
                scenario,
            )
        amount = f"amount_{formulation}_advance"
        for baseline in (
            f"amount_{formulation}_current",
            "e2_magnitude_no_change",
            "e2_magnitude_historical_median",
            "e2_magnitude_catboost_group_mae",
        ):
            compare(amount, baseline, scenario, "mae_dkk", "magnitude")
    compare(
        "cb_equal_current",
        "logistic_equal_current",
        "perfect",
        "brier",
        "same_information_algorithm",
    )
    for peer, baseline in (("equal", "package"), ("broad", "equal")):
        compare(
            f"cb_{peer}_current",
            f"cb_{baseline}_current",
            "perfect",
            "brier",
            "current_peer_increment",
        )
        compare(
            f"fallback_{peer}_current", f"cb_{peer}_current", "perfect", "brier", "fixed_fallback"
        )
    for baseline in (
        "e2_direction_no_change",
        "e2_direction_historical_frequency",
        "e2_direction_repeat_last",
    ):
        compare("cb_equal_advance", baseline, "perfect", "brier", "original_e2_reference")
    for baseline in (
        "e2_magnitude_no_change",
        "e2_magnitude_historical_median",
        "e2_magnitude_catboost_group_mae",
    ):
        compare(
            f"amount_{formulation}_current", baseline, "perfect", "mae_dkk", "magnitude_current"
        )
    return rows


def score_queues(
    root: Path,
    derived: Path,
    protocol: dict[str, Any],
    inventory: list[dict[str, Any]],
    checkpoint: Callable[[], None] = lambda: None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    labels = pd.concat(
        [_cohort(root, fold["name"], "outcomes") for fold in protocol["folds"][1:]],
        ignore_index=True,
    )
    origins = pd.concat(
        [_cohort(root, fold["name"], "features")[list(KEYS)] for fold in protocol["folds"][1:]],
        ignore_index=True,
    )
    labels = attach_outcomes(origins, labels)
    rows, comparisons = [], []
    cache: dict[tuple[str, str], tuple[pd.DataFrame, list[dict[str, Any]]]] = {}
    for scenario in SCENARIOS:
        identities = sorted({item["policy"] for item in inventory if item["scenario"] == scenario})
        for policy in identities:
            checkpoint()
            entries = [
                item
                for item in inventory
                if item["scenario"] == scenario and item["policy"] == policy
            ]
            saved = _load_saved(derived, entries)
            _assert_alignment(saved, labels, ["row_key"])
            frame = labels.merge(saved, on="row_key", validate="one_to_one", sort=False)
            per = queue_periods(frame)
            observed = sum(row["observed"] for row in per)
            selected = sum(row["selected"] for row in per)
            parameter = entries[0]["parameter"]
            summary: dict[str, Any] = {
                "method": entries[0]["method"],
                "policy": policy,
                "scenario": scenario,
                "split": "evaluation_all",
                "workload_mode": entries[0]["workload_mode"],
                "parameter": str(parameter) if parameter is not None else None,
                "lambda": float(parameter) if "risk_rank" in policy else None,
                "rise_threshold": float(parameter[0]) if "threshold" in policy else None,
                "fall_threshold": float(parameter[1]) if "threshold" in policy else None,
                "chosen_before_evaluation": entries[0]["chosen"],
                "candidates": len(frame),
                "selected": selected,
                "observed": observed,
                "periods": len(per),
                "selection_rate": float(frame.selected.mean()),
                "empty_periods": sum(row["empty"] for row in per),
                "rises": sum(row["rises"] for row in per),
                "falls": sum(row["falls"] for row in per),
                "pooled_rise_rate": sum(row["rises"] for row in per) / observed
                if observed
                else None,
                "pooled_fall_rate": sum(row["falls"] for row in per) / observed
                if observed
                else None,
                "pooled_unknown_fraction": 1 - observed / selected if selected else None,
            }
            for metric in ("rise_rate", "fall_rate", "unknown_fraction"):
                values = np.array([row[metric] for row in per if row[metric] is not None])
                summary[metric] = float(values.mean()) if len(values) else None
                for block in (4, 8):
                    if len(values) > 1:
                        interval = _interval(values, block)
                        summary[f"{metric}_lower_block{block}"] = interval["lower"]
                        summary[f"{metric}_upper_block{block}"] = interval["upper"]
            rows.append(summary)
            rows.extend({**summary, **row, "split": "per_period"} for row in per)
            if entries[0]["workload_mode"] != "descriptive_grid":
                cache[(scenario, policy)] = (saved[["row_key", "selected"]], per)
        # Keep only boolean selections and 73 period rows; discard probabilities between policies.
        for policy in identities:
            if (scenario, policy) not in cache or policy == "e2_top10" or "_matched_" in policy:
                continue
            saved, per = cache[(scenario, policy)]
            original, original_per = cache[(scenario, "e2_top10")]
            _assert_alignment(saved, original, ["row_key"])
            a, b = saved.selected.to_numpy(dtype=bool), original.selected.to_numpy(dtype=bool)
            union = int((a | b).sum())
            changed = {
                "changed_selected_keys": int((a != b).sum()),
                "jaccard": int((a & b).sum()) / union if union else 1.0,
            }
            for metric in ("rise_rate", "fall_rate", "unknown_fraction"):
                comparisons.extend(
                    paired_comparison(
                        per,
                        original_per,
                        metric=metric,
                        metadata={
                            "candidate": policy,
                            "baseline": "e2_top10",
                            "scenario": scenario,
                            "split": "evaluation_all",
                            "subgroup": "all",
                            "comparison": "natural_queue_workload",
                            **changed,
                        },
                        allow_missing_rates=True,
                    )
                )
            rows.append(
                {
                    **changed,
                    "method": "cb_equal_advance_calibrated",
                    "scenario": scenario,
                    "policy": policy,
                    "reference": "e2_top10",
                    "workload_mode": "natural_membership",
                    "split": "evaluation_all",
                }
            )
        for policy in identities:
            entries = [
                item
                for item in inventory
                if item["scenario"] == scenario and item["policy"] == policy
            ]
            reference = entries[0].get("reference")
            if reference is None:
                continue
            saved, per = cache[(scenario, policy)]
            original, original_per = cache[(scenario, reference)]
            _assert_alignment(saved, original, ["row_key"])
            a, b = saved.selected.to_numpy(dtype=bool), original.selected.to_numpy(dtype=bool)
            union = int((a | b).sum())
            changed = {
                "changed_selected_keys": int((a != b).sum()),
                "jaccard": int((a & b).sum()) / union if union else 1.0,
            }
            for metric in ("rise_rate", "fall_rate", "unknown_fraction"):
                comparisons.extend(
                    paired_comparison(
                        original_per,
                        per,
                        metric=metric,
                        metadata={
                            "candidate": reference,
                            "baseline": policy,
                            "scenario": scenario,
                            "split": "evaluation_all",
                            "subgroup": "all",
                            "comparison": "matched_queue_workload",
                            **changed,
                        },
                        allow_missing_rates=True,
                    )
                )
            rows.append(
                {
                    **changed,
                    "method": entries[0]["method"],
                    "scenario": scenario,
                    "policy": policy,
                    "reference": reference,
                    "workload_mode": "matched_membership",
                    "split": "evaluation_all",
                }
            )
        cache.clear()
    return rows, comparisons


def _flat_trials(root: Path) -> list[dict[str, Any]]:
    rows = []
    for trial in _json(root / "trial-ledger.json")["trials"]:
        rows.append(
            {
                key: value
                for key, value in trial.items()
                if key != "traceback"
                and (value is None or isinstance(value, (str, int, float, bool)))
            }
            | {
                "runtime_seconds": trial.get("resource", {}).get("elapsed_seconds"),
                "peak_rss_bytes": trial.get("resource", {}).get("sampled_peak_rss_bytes"),
                "algorithm": trial.get("spec", {}).get("algorithm"),
            }
        )
    return rows


def publish(root: Path, output: Path | None = None, *, relocate_from: Path | None = None) -> Path:
    root = root.resolve()
    output = (output or root / "saved").resolve()
    if output.exists():
        raise ResearchError("e3_publish_requires_unused_output")
    location = Relocation(root, relocate_from)
    freeze_publisher(root, relocate_from=relocate_from)
    protocol = _json(root / "protocol.json")
    binding = _source_binding(root, protocol, location)
    selection = _json(root / "selection.json")
    primary = output == root / "saved"
    index_path = root.parent / "index.json" if primary else output / "index.json"
    if index_path.exists():
        raise ResearchError("e3_publish_refuses_existing_index")
    derived = root / "derived" if primary else output / "derived"
    if derived.exists():
        raise ResearchError("e3_publish_requires_unused_derived")
    output.mkdir(parents=True)
    derived.mkdir(parents=True)
    with PublicationBudget(output, derived) as budget:
        return _publish(root, output, derived, protocol, selection, binding, index_path, budget)


def _publish(
    root: Path,
    output: Path,
    derived: Path,
    protocol: dict[str, Any],
    selection: dict[str, Any],
    binding: dict[str, Any],
    index_path: Path,
    budget: PublicationBudget,
) -> Path:
    budget.check()
    failures = [
        trial
        for trial in _json(root / "trial-ledger.json")["trials"]
        if trial["status"] == "failed"
    ]
    if failures:
        atomic_json(
            derived / "unavailable-comparisons.json",
            {
                "rows": [
                    {
                        "candidate": trial["method"],
                        "split": trial["fold"],
                        "task": trial["task"],
                        "status": "not_scored",
                        "reason": "required saved trial failed; no replacement or retry",
                        "trial_id": trial["trial_id"],
                        "error": trial.get("error"),
                        "lower": None,
                        "upper": None,
                    }
                    for trial in failures
                ],
                "limitations": LIMITATIONS,
            },
        )
        raise ResearchError("e3_failed_required_trials_unscored")
    states = calibration_states(root, derived, selection, binding)
    budget.check()
    inventory, queues = prepare_predictions(
        root, derived, output, protocol, selection, states, budget.check
    )
    budget.check(files=True)
    return _score_and_publish(
        root, output, derived, protocol, selection, binding, index_path, budget, inventory, queues
    )


def _score_and_publish(
    root: Path,
    output: Path,
    derived: Path,
    protocol: dict[str, Any],
    selection: dict[str, Any],
    binding: dict[str, Any],
    index_path: Path,
    budget: PublicationBudget,
    inventory: list[dict[str, Any]],
    queues: list[dict[str, Any]],
    *,
    outcome_destination: Path | None = None,
    preparation: dict[str, Any] | None = None,
    prepared_location: Relocation | None = None,
) -> Path:
    run_id = Path(binding["protocol"]["path"]).parent.name
    # First evaluated-cohort score attachment comes after all frozen selections.
    reconciliation = reconcile_e2(root, protocol)
    summary, scored = score_predictions(
        root, derived, protocol, inventory, budget.check, outcome_destination=outcome_destination
    )
    queue_rows, queue_comparisons = score_queues(root, derived, protocol, queues, budget.check)
    budget.check()
    comparisons = comparison_rows(scored, selection["monetary_formulation"]) + queue_comparisons
    roles: dict[str, dict[str, Any]] = {
        "summary": {
            "rows": [
                row for row in summary if row["scenario"] == "perfect" and row["subgroup"] == "all"
            ]
        },
        "comparisons": {"rows": comparisons, "binding": reconciliation},
        "scenarios": {
            "rows": [
                row
                for row in summary
                if row["information"] == "advance_abc_historical_proxy" and row["subgroup"] == "all"
            ],
            "input_coverage": _json(derived / "scenario-input-coverage.json"),
        },
        "price_uncertainty": {
            "rows": [
                row for row in summary if row["task"] == "magnitude" and row["subgroup"] == "all"
            ]
        },
        "queue_tradeoffs": {"rows": queue_rows, "frozen_selection": selection["queue_policies"]},
        "peer_coverage": {"rows": [row for row in summary if row["subgroup"] != "all"]},
        "trials": {"rows": _flat_trials(root), "execution": _json(root / "execution.json")},
    }
    files = []
    for role, value in roles.items():
        path = output / f"{role}.json"
        atomic_json(path, {**value, "limitations": LIMITATIONS})
        files.append(_relative_pin(path, output, role=role))
    # The outcome table remains a physically separate role, never a review column.
    outcome_path = output / "candidate_outcomes.parquet"
    source_outcome = outcome_destination or derived / "candidate_outcomes.parquet"
    if source_outcome.resolve() != outcome_path.resolve():
        shutil.copyfile(source_outcome, outcome_path)
    for role in ("candidate_review", "candidate_outcomes"):
        files.append(_relative_pin(output / f"{role}.parquet", output, role=role))
    budget.check(files=True)
    if budget.result_bytes + 1024**2 > budget.limits["result_bytes"]:
        raise ResearchError("e3_publisher_result_budget_exceeded")
    path = output / "result.json"
    prepared_pin = prepared_location.canonical_pin if prepared_location is not None else pin
    atomic_json(
        path,
        {
            "schema_version": "e3-saved-results-v1",
            "run_id": run_id,
            "label": "E3 advance ABC and peer research",
            "status": "completed",
            "evaluation_locked": True,
            "exposure_status": protocol["exposure"],
            "definitions": {
                **SPECIFICATION,
                "information_boundary": protocol["information_boundary"],
                "price_basis": "AIP DKK/package; excluding VAT and consumer fees",
                "selection": selection["monetary_formulation"],
            },
            "split_dates": protocol["folds"],
            "files": files,
            "source_binding": binding,
            "derived_inventory": prepared_pin(derived / "prediction-inventory.json"),
            "queue_inventory": prepared_pin(derived / "queue-inventory.json"),
            "calibration": prepared_pin(derived / "calibration.json"),
            **({"preparation_provenance": preparation} if preparation is not None else {}),
        },
    )
    atomic_json(
        index_path,
        {
            "schema_version": "e3-run-index-v1",
            "runs": [
                _relative_pin(
                    path,
                    index_path.parent,
                    run_id=run_id,
                    label="E3 advance ABC and peer research",
                ),
            ],
        },
    )
    return path


def finish_prepared(
    root: Path, output: Path | None = None, *, relocate_from: Path | None = None
) -> Path:
    """Score the sealed v2 preparation; never fit, calibrate, prepare or select queues."""
    preflight_started = time.monotonic()
    root = root.resolve()
    output = (output or root / "saved").resolve()
    primary = output == root / "saved"
    index_path = root.parent / "index.json" if primary else output / "index.json"
    if index_path.exists():
        raise ResearchError("e3_prepared_existing_result_or_index")
    if primary:
        if not output.is_dir() or {path.name for path in output.iterdir()} != {
            "candidate_review.parquet"
        }:
            raise ResearchError("e3_prepared_primary_output_not_review_only")
        if (root / "derived/candidate_outcomes.parquet").exists() or (
            root / "derived" / RESOURCE_NAME
        ).exists():
            raise ResearchError("e3_prepared_partial_finish_exists")
    else:
        if output.exists():
            raise ResearchError("e3_publish_requires_unused_output")
        if any(
            output.is_relative_to(root / name) for name in ("derived", "saved", "trials", "cohorts")
        ):
            raise ResearchError("e3_prepared_output_overlaps_inputs")
    location = Relocation(root, relocate_from)
    freeze_publisher(root, relocate_from=relocate_from)
    snapshot = _json(root / AMENDMENT_NAME).get("preparation")
    if snapshot is None:
        raise ResearchError("e3_prepared_v2_inputs_not_sealed")
    protocol, selection = _json(root / "protocol.json"), _json(root / "selection.json")
    binding_v3 = _source_binding(root, protocol, location)
    derived = root / "derived"
    failed = _json(derived / "publication-resource.json")
    inputs = _prepared_input_paths(root, location, snapshot)
    inherited = {
        "failed_receipt": snapshot["files"]["failed_resource"],
        "failed_attempt": failed,
        "preflight_seconds": time.monotonic() - preflight_started,
    }
    output.mkdir(parents=True, exist_ok=primary)
    outcome = (
        derived / "candidate_outcomes.parquet" if primary else output / "candidate_outcomes.parquet"
    )
    receipt = derived / RESOURCE_NAME if primary else output / RESOURCE_NAME
    provenance = {
        "schema_version": "e3-prepared-scoring-v3",
        "amendment": location.canonical_pin(root / AMENDMENT_NAME),
        "original_v2_binding": snapshot["original_v2_binding"],
        "publication_v3_binding": binding_v3,
        "prepared_files": snapshot["files"],
        "regenerated_preparation": False,
        "additional_fitting_calibration_or_selection": False,
    }
    with PublicationBudget(
        output,
        derived,
        inputs=inputs,
        generated={outcome, index_path},
        receipt_path=receipt,
        phase="finish_prepared_scoring_only_after_byte_verification",
        inherited=inherited,
    ) as budget:
        budget.check(files=True)
        if not primary:
            source_review = location.physical(Path(snapshot["files"]["candidate_review"]["path"]))
            shutil.copyfile(source_review, output / "candidate_review.parquet")
            verify_pin(output / "candidate_review.parquet", snapshot["files"]["candidate_review"])
        return _score_and_publish(
            root,
            output,
            derived,
            protocol,
            selection,
            binding_v3,
            index_path,
            budget,
            _json(derived / "prediction-inventory.json")["files"],
            _prepared_queues(derived),
            outcome_destination=outcome,
            preparation=provenance,
            prepared_location=location,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--finish-prepared",
        action="store_true",
        help="Score exact sealed v2 preparation without any preparation/calibration/selection",
    )
    parser.add_argument(
        "--relocate-from",
        type=Path,
        help="Original E3 evidence root; exact E3 bytes resolve under the physical run parent",
    )
    args = parser.parse_args()
    operation = finish_prepared if args.finish_prepared else publish
    print(operation(args.root, args.output, relocate_from=args.relocate_from))
