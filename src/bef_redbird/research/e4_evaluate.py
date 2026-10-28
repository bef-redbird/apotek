"""Finite saved-prediction E4 comparisons; no base fitting and no source refresh."""

from __future__ import annotations

import gc
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e2_experiment import ResourceSample
from .e2_metrics import paired_interval, probability_metrics
from .e3_data import perturb
from .e3_methods import calibrate, intervals, queue_periods, select_queue, temperature_fit
from .e4_common import checked, frozen_json, read, require_protocol, utc
from .e4_methods import calibrated_routes, fit_residual, mixed, residual_bounds, route
from .group_features import pin

PROBS = ["p0", "p1", "p2"]
SCENARIOS = ["perfect", "missing25", "wrong10", "group_wrong20", "missing100"]
LABELS = {
    "e2": "E2 current ABC, equal-size peers",
    "e3": "E3 advance ABC, equal-size peers",
    "routed": "Advance ABC with broader/equal/package routing",
    "hybrid": "Routed advance ABC with current-only fallback",
    "current": "Current ABC with broader/equal/package routing",
}


def execution_binding(root: Path) -> dict[str, Any]:
    return {
        "protocol": pin(root / "protocol.json"),
        "clarification": pin(root / "protocol-clarification-v1.json"),
        "producer": [
            pin(Path(__file__).with_name(name))
            for name in (
                "e4_evaluate.py",
                "e4_methods.py",
                "e3_methods.py",
                "e2_metrics.py",
                "e3_data.py",
            )
        ],
    }


class SavedInputs:
    def __init__(self, root: Path, accepted_result: dict[str, Any]) -> None:
        self.root = root
        self.pins: dict[str, dict[str, Any]] = {}
        self.accepted = read(self.verify(accepted_result))
        self.binding = self.accepted["source_binding"]
        self.ledger = {
            x["trial_id"]: x for x in read(self.verify(self.binding["ledger"]))["trials"]
        }

    def verify(self, ref: dict[str, Any]) -> Path:
        path = str(ref["path"])
        if path not in self.pins:
            checked(ref)
            self.pins[path] = ref
        elif self.pins[path]["sha256"] != ref["sha256"]:
            raise ResearchError("e4_source_binding_changed")
        return Path(path)

    def cohort(self, fold: str, *, labels: bool = False) -> pd.DataFrame:
        manifest = self.root / "cohorts" / fold / "manifest.json"
        self.verify(self.binding["cohorts"][fold])
        ref = read(manifest)["outcomes" if labels else "features"]
        frame = pd.read_parquet(self.verify(ref))
        for col in ("period_start", "target_period", "next_maturity_date"):
            if col in frame:
                frame[col] = pd.to_datetime(frame[col])
        return frame

    def prediction(self, fold: str, method: str, keys: pd.Series) -> pd.DataFrame:
        directory = self.root / "trials" / (fold + "-" + method)
        receipt_path = directory / "trial.json"
        receipt = read(receipt_path)
        if receipt != self.ledger[fold + "-" + method]:
            raise ResearchError("e4_trial_not_accepted_ledger")
        self.verify(pin(receipt_path))
        expected_cutoff = "2022-01-01" if fold == "validation" else fold[-4:] + "-01-01"
        if (
            receipt["status"] != "completed"
            or receipt["latest_training_label_maturity"] >= receipt["fit_cutoff"]
            or receipt["fit_cutoff"] != expected_cutoff
            or receipt["training_start"] != str(int(expected_cutoff[:4]) - 3) + "-01-01"
            or receipt["fold"] != fold
        ):
            raise ResearchError("e4_invalid_accepted_training_maturity")
        ref = next(x for x in receipt["files"] if x["path"] == "predictions.parquet")
        ref = {**ref, "path": str((directory / ref["path"]).resolve())}
        frame = pd.read_parquet(self.verify(ref))
        if not frame.row_key.equals(keys):
            raise ResearchError("e4_prediction_cohort_changed")
        return frame

    def arrays(self, fold: str, keys: pd.Series) -> dict[str, pd.DataFrame]:
        return {
            method: self.prediction(fold, method, keys)
            for method in (
                "cb_package_current",
                "cb_equal_current",
                "cb_broad_current",
                "cb_package_advance",
                "cb_equal_advance",
                "cb_broad_advance",
                "amount_delta_floor_current",
                "amount_delta_floor_advance",
            )
        }


def probabilities(saved: pd.DataFrame, scenario: str) -> np.ndarray:
    key = scenario if f"p0_{scenario}" in saved else "perfect"
    return saved[[f"p{k}_{key}" for k in range(3)]].to_numpy(dtype=float)


def routed(
    frame: pd.DataFrame, arrays: dict[str, pd.DataFrame], info: str, scenario: str
) -> tuple[np.ndarray, np.ndarray]:
    return route(
        frame,
        *[
            probabilities(arrays[f"cb_{peer}_{info}"], scenario)
            for peer in ("package", "equal", "broad")
        ],
    )


def align_outcomes(features: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    # E3 keeps the full source outcome catalogue, including excluded origins.
    # Select only already-frozen candidate keys; never use labels to create candidates.
    if outcomes.row_key.duplicated().any() or not features.row_key.isin(outcomes.row_key).all():
        raise ResearchError("e4_missing_or_duplicate_outcome_keys")
    aligned = outcomes.set_index("row_key").loc[features.row_key].reset_index()
    for name in ("package_number", "period_start", "identity_episode_id"):
        if not aligned[name].reset_index(drop=True).equals(features[name].reset_index(drop=True)):
            raise ResearchError("e4_outcome_origin_context_changed")
    return aligned


def calibrations(root: Path, source: SavedInputs) -> dict[str, Any]:
    path = root / "calibration.json"
    if path.exists():
        saved = read(path)
        if saved["binding"] != execution_binding(root):
            raise ResearchError("e4_calibration_resume_binding_changed")
        for ref in saved["sources"]:
            source.verify(ref)
        return saved
    output: dict[str, Any] = {
        "version": "e4-calibration-v1",
        "states": {},
        "sources": [],
        "binding": execution_binding(root),
    }
    for year in (2024, 2025, 2026):
        fold = "validation" if year == 2024 else f"evaluation_{year - 1}"
        features, labels = source.cohort(fold), source.cohort(fold, labels=True)
        labels = align_outcomes(features, labels)
        arrays = source.arrays(fold, features.row_key)
        previous = features.period_start.dt.year.eq(year - 1).to_numpy()
        frame = features.loc[previous].reset_index(drop=True)
        labels = labels.loc[previous].reset_index(drop=True)
        frame = frame.merge(
            labels[
                ["row_key", "next_status", "next_maturity_date", "next_aip_ore", "next_direction"]
            ],
            on="row_key",
            validate="one_to_one",
        )
        arrays = {k: v.loc[previous].reset_index(drop=True) for k, v in arrays.items()}
        advance, _ = routed(frame, arrays, "advance", "perfect")
        current, _ = routed(frame, arrays, "current", "perfect")
        states: dict[str, Any] = {"temperature": {}, "residual": {}}
        for method, values in {
            "routed": advance,
            "hybrid": mixed(advance, current, frame.advance_abc),
            "current": current,
        }.items():
            frame[PROBS] = values
            states["temperature"][method] = temperature_fit(frame, f"{year}-01-01")
        for info in ("advance", "current"):
            frame["next_aip_dkk"] = arrays[
                f"amount_delta_floor_{info}"
            ].next_aip_dkk_perfect.to_numpy()
            states["residual"][info] = fit_residual(frame, f"{year}-01-01")
        states["source_fold"] = fold
        states["base_fit_cutoff"] = "2022-01-01" if year == 2024 else f"{year - 1}-01-01"
        output["states"][str(year)] = states
        del features, labels, arrays, frame
        gc.collect()
    output["sources"] = list(source.pins.values())
    output["created_utc"] = utc()
    frozen_json(path, output)
    return output


def _score_direction(
    frame: pd.DataFrame, p: np.ndarray, method: str, scenario: str, calibrated: bool
) -> list[dict[str, Any]]:
    result = []
    for day, index in frame.groupby("period_start", sort=True).indices.items():
        part = frame.iloc[index]
        known = part.next_status.eq("observed").to_numpy()
        result.append(
            {
                "period_start": str(day)[:10],
                "method": method,
                "scenario": scenario,
                "calibrated": calibrated,
                "candidates": len(part),
                "observed": int(known.sum()),
                **probability_metrics(
                    part.loc[known, "next_direction"].to_numpy(dtype=int) + 1, p[index][known]
                ),
            }
        )
    return result


def interval_metrics(actual: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> dict[str, Any]:
    available = np.isfinite(lower) & np.isfinite(upper)
    if ((lower[available] < 0) | (upper[available] < lower[available])).any():
        raise ResearchError("e4_invalid_interval")
    return {
        "interval_observed": int(available.sum()),
        "interval_unavailable": int((~available).sum()),
        "coverage90": float(
            (
                (actual[available] >= lower[available]) & (actual[available] <= upper[available])
            ).mean()
        )
        if available.any()
        else None,
        "width90_dkk": float((upper[available] - lower[available]).mean())
        if available.any()
        else None,
    }


def _aggregate(
    rows: list[dict[str, Any]], groups: list[str], metrics: list[str]
) -> list[dict[str, Any]]:
    data = pd.DataFrame(rows)
    out = []
    for key, part in data.groupby(groups, dropna=False, sort=True):
        record: dict[str, Any] = dict(
            zip(groups, key if isinstance(key, tuple) else (key,), strict=True)
        )
        record["periods"] = len(part)
        for name in metrics:
            if name in part:
                values = part[name].dropna()
                record[name] = float(values.mean()) if len(values) else None
        for name in (
            "candidates",
            "selected",
            "observed",
            "rises",
            "falls",
            "interval_observed",
            "interval_unavailable",
        ):
            if name in part:
                record[name] = int(part[name].sum())
        if "selected" in part:
            s, o = record["selected"], record["observed"]
            record.update(
                pooled_unknown_fraction=(s - o) / s if s else None,
                pooled_rise_rate=record["rises"] / o if o else None,
                pooled_fall_rate=record["falls"] / o if o else None,
                rise_lower_bound=record["rises"] / s if s else None,
                rise_upper_bound=(record["rises"] + s - o) / s if s else None,
                fall_lower_bound=record["falls"] / s if s else None,
                fall_upper_bound=(record["falls"] + s - o) / s if s else None,
            )
        elif "brier" in part:
            record["pooled_brier"] = float(
                (part.brier.fillna(0) * part.observed).sum() / part.observed.sum()
            )
        elif "mae_dkk" in part:
            for metric in ("mae_dkk", "bias_dkk", "coverage90", "width90_dkk"):
                weight = (
                    part.interval_observed
                    if metric in {"coverage90", "width90_dkk"}
                    else part.observed
                )
                record["pooled_" + metric] = (
                    float((part[metric].fillna(0) * weight).sum() / weight.sum())
                    if weight.sum()
                    else None
                )
        out.append(record)
    return out


def evaluate_year(
    root: Path, source: SavedInputs, year: int, calibration: dict[str, Any]
) -> dict[str, Any]:
    output = root / f"evaluation-{year}"
    if (output / "result.json").exists():
        result = read(output / "result.json")
        if result["binding"] != execution_binding(root) or result["calibration"] != pin(
            root / "calibration.json"
        ):
            raise ResearchError("e4_evaluation_resume_binding_changed")
        for ref in result["artifacts"]:
            checked(ref)
        for ref in result["inputs"]:
            source.verify(ref)
        return result
    output.mkdir(parents=True, exist_ok=True)
    fold = f"evaluation_{year}"
    feature = source.cohort(fold)
    outcomes = align_outcomes(feature, source.cohort(fold, labels=True))
    arrays = source.arrays(fold, feature.row_key)
    fields = [
        "row_key",
        "package_number",
        "period_start",
        "target_period",
        "identity_episode_id",
        "aip_ore",
        "abc",
        "advance_abc",
        "advance_state",
        "origin_group",
        "history_periods",
        "peer_count",
        "peer_feature_state",
        "broad_peer_count",
        "broad_peer_feature_state",
        "pack_size_token",
        "pack_unit",
    ]
    compact = feature[fields].copy()
    selections = feature[["row_key", "period_start"]].copy()
    score = compact.merge(
        outcomes[
            ["row_key", "next_status", "next_maturity_date", "next_aip_ore", "next_direction"]
        ],
        on="row_key",
        validate="one_to_one",
    )
    prior = read(source.verify(source.accepted["calibration"]))
    states = calibration["states"][str(year)]
    direction_rows, queue_rows, money_rows = [], [], []
    current, route_names = routed(feature, arrays, "current", "perfect")
    compact["peer_route"] = route_names
    for scenario in SCENARIOS:
        changed = perturb(feature, scenario)
        advance, _ = routed(feature, arrays, "advance", scenario)
        pred = calibrated_routes(changed, advance, current, states["temperature"])
        pred["e2"] = probabilities(arrays["cb_equal_current"], "perfect")
        pred["e3"] = calibrate(
            probabilities(arrays["cb_equal_advance"], scenario), prior["temperature"][str(year)]
        )
        for method, raw in {
            "routed": advance,
            "hybrid": mixed(advance, current, changed.advance_abc),
            "current": current,
        }.items():
            direction_rows.extend(_score_direction(score, raw, method, scenario, False))
        queues = {}
        for method, p in pred.items():
            direction_rows.extend(_score_direction(score, p, method, scenario, method != "e2"))
            for k in range(3):
                compact[f"{scenario}_{method}_p{k}"] = p[:, k]
            base = compact[["row_key", "period_start"]].copy()
            base[PROBS] = p
            for policy, parameter in (
                ("rise", None),
                ("risk_rank", 0.5),
                ("threshold", (0.35, 0.10)),
            ):
                if method in {"e2", "current"} and policy != "rise":
                    continue
                queue = select_queue(base, policy, parameter)
                queues[method + "_" + policy] = queue
        workload = queues["hybrid_threshold"].groupby("period_start").selected.sum()
        counts = {str(day)[:10]: int(n) for day, n in workload.items()}
        for method in pred:
            base = compact[["row_key", "period_start"]].copy()
            base[PROBS] = pred[method]
            for policy, parameter in (("rise", None), ("risk_rank", 0.5)):
                if method in {"e2", "current"} and policy != "rise":
                    continue
                queues[method + "_" + policy + "_matched"] = select_queue(
                    base, policy, parameter, workload=counts
                )
        routed_counts = {
            str(day)[:10]: int(n)
            for day, n in queues["routed_threshold"].groupby("period_start").selected.sum().items()
        }
        for method in ("e2", "e3", "routed"):
            base = compact[["row_key", "period_start"]].copy()
            base[PROBS] = pred[method]
            policy, parameter = ("rise", None) if method == "e2" else ("risk_rank", 0.5)
            queues[method + "_" + policy + "_matched_routed"] = select_queue(
                base, policy, parameter, workload=routed_counts
            )
        for name, queue in queues.items():
            selections[scenario + "_" + name] = queue.selected.to_numpy()
            if scenario == "perfect" and name == "hybrid_threshold":
                compact["rank"] = queue["rank"].to_numpy()
                compact["selected"] = queue.selected.to_numpy()
            attached = queue.merge(
                outcomes[["row_key", "next_status", "next_direction"]],
                on="row_key",
                validate="one_to_one",
            )
            queue_rows.extend(
                [{**r, "policy": name, "scenario": scenario} for r in queue_periods(attached)]
            )
        pa = arrays["amount_delta_floor_advance"]["next_aip_dkk_" + scenario].to_numpy()
        pc = arrays["amount_delta_floor_current"].next_aip_dkk_perfect.to_numpy()
        valid = changed.advance_abc.isin(["A", "B", "C"]).to_numpy()
        ph = np.where(valid, pa, pc)
        old = score.copy()
        old["next_aip_dkk"] = pa
        old = intervals(old, prior["residual"][f"{year}_advance"])
        la, ua = residual_bounds(score, pa, states["residual"]["advance"])
        lc, uc = residual_bounds(score, pc, states["residual"]["current"])
        lh, uh = np.where(valid, la, lc), np.where(valid, ua, uc)
        compact[f"{scenario}_advance_usable"] = valid
        for name, p, lower, upper in (
            (
                "advance_original_interval",
                pa,
                old.lower90_dkk.to_numpy(),
                old.upper90_dkk.to_numpy(),
            ),
            ("advance_conditional_interval", pa, la, ua),
            ("hybrid_conditional_interval", ph, lh, uh),
            ("current_conditional_interval", pc, lc, uc),
        ):
            compact[f"{scenario}_{name}_next_aip_dkk"] = p
            compact[f"{scenario}_{name}_lower90_dkk"] = lower
            compact[f"{scenario}_{name}_upper90_dkk"] = upper
            for day, index in score.groupby("period_start", sort=True).indices.items():
                part = score.iloc[index]
                masks = {"all": np.ones(len(part), bool)}
                if scenario == "perfect":
                    masks.update(
                        history_under6=part.history_periods.lt(6).to_numpy(),
                        history_established=part.history_periods.ge(6).to_numpy(),
                        equal_positive=part.peer_count.ge(1).fillna(False).to_numpy(dtype=bool),
                        equal_zero=part.peer_count.eq(0).fillna(False).to_numpy(dtype=bool),
                        equal_unsupported=part.peer_count.isna().to_numpy(),
                        price_under100=part.aip_ore.lt(10000).to_numpy(),
                        price_100_1000=(
                            (part.aip_ore >= 10000) & (part.aip_ore < 100000)
                        ).to_numpy(),
                        price_1000plus=part.aip_ore.ge(100000).to_numpy(),
                    )
                for stratum, mask in masks.items():
                    obs = mask & part.next_status.eq("observed").to_numpy()
                    actual = part.loc[obs, "next_aip_ore"].to_numpy(dtype=float) / 100
                    error = p[index][obs] - actual
                    money_rows.append(
                        {
                            "period_start": str(day)[:10],
                            "method": name,
                            "scenario": scenario,
                            "stratum": stratum,
                            "candidates": int(mask.sum()),
                            "observed": len(error),
                            "mae_dkk": float(np.abs(error).mean()) if len(error) else None,
                            "bias_dkk": float(error.mean()) if len(error) else None,
                            **interval_metrics(actual, lower[index][obs], upper[index][obs]),
                        }
                    )
        del changed, queues
    # One compact prediction and selection table per annual cohort, labels remain separate.
    compact.to_parquet(output / "predictions.parquet", index=False)
    selections.to_parquet(output / "selections.parquet", index=False)
    frozen_json(
        output / "metrics.json",
        {"direction": direction_rows, "queues": queue_rows, "money": money_rows},
    )
    result = {
        "version": "e4-evaluation-year-v1",
        "binding": execution_binding(root),
        "calibration": pin(root / "calibration.json"),
        "inputs": list(source.pins.values()),
        "year": year,
        "candidates": len(compact),
        "observed": int(outcomes.next_status.eq("observed").sum()),
        "created_utc": utc(),
        "artifacts": [
            pin(output / x) for x in ("predictions.parquet", "selections.parquet", "metrics.json")
        ],
        "outcomes": pin(source.root / "cohorts" / fold / "outcomes.parquet"),
    }
    frozen_json(output / "result.json", result)
    return result


def _paired_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = pd.DataFrame(rows)
    out = []
    for scenario in SCENARIOS:
        for candidate, baseline in [
            ("hybrid_risk_rank", "e2_rise"),
            ("hybrid_risk_rank", "e3_risk_rank"),
            ("hybrid_risk_rank", "hybrid_rise"),
            ("routed_risk_rank", "e3_risk_rank"),
            ("hybrid_threshold", "e2_rise_matched"),
            ("hybrid_threshold", "e3_risk_rank_matched"),
            ("hybrid_threshold", "hybrid_rise_matched"),
            ("routed_risk_rank_matched", "e3_risk_rank_matched"),
            ("routed_threshold", "e2_rise_matched_routed"),
            ("routed_threshold", "e3_risk_rank_matched_routed"),
        ]:
            a = data.loc[data.scenario.eq(scenario) & data.policy.eq(candidate)].set_index(
                "period_start"
            )
            b = data.loc[data.scenario.eq(scenario) & data.policy.eq(baseline)].set_index(
                "period_start"
            )
            if not a.index.equals(b.index) or not a.selected.equals(b.selected):
                raise ResearchError("e4_matched_workload_changed")
            for metric in ("rise_rate", "fall_rate", "unknown_fraction"):
                delta = (a[metric] - b[metric]).dropna().to_numpy()
                for block in (4, 8):
                    ci = (
                        paired_interval(delta, block=block, seed=20261027)
                        if len(delta) >= 2
                        else {"status": "insufficient_paired_periods", "periods": len(delta)}
                    )
                    out.append(
                        {
                            "scenario": scenario,
                            "candidate": candidate,
                            "baseline": baseline,
                            "metric": metric,
                            "block": block,
                            "difference": float(delta.mean()) if len(delta) else None,
                            **ci,
                        }
                    )
    return out


def evaluate(root: Path) -> Path:
    protocol = require_protocol(root)
    if not (root / "protocol-clarification-v1.json").exists():
        raise ResearchError("e4_protocol_clarification_required")
    result_path = root / "evaluation.json"
    if result_path.exists():
        for ref in read(result_path)["artifacts"]:
            checked(ref)
        return result_path
    source = SavedInputs(Path(protocol["e3_root"]), protocol["accepted_e3"]["display_result"])
    sample = ResourceSample()
    started = time.monotonic()
    try:
        calibration = calibrations(root, source)
        annual = []
        for year in (2024, 2025, 2026):
            annual.append(evaluate_year(root, source, year, calibration))
            gc.collect()
            used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            limits = protocol["resource_limits"]
            if (
                sample.peak > limits["process_rss_bytes"]
                or time.monotonic() - started > limits["evaluation_seconds"]
                or used > limits["incremental_research_bytes"]
                or shutil.disk_usage(root).free < limits["free_disk_reserve_bytes"]
            ):
                raise ResearchError("e4_declared_resource_ceiling")
        all_rows: dict[str, list[dict[str, Any]]] = {
            k: [] for k in ("direction", "queues", "money")
        }
        for year in (2024, 2025, 2026):
            metrics = read(root / f"evaluation-{year}" / "metrics.json")
            for key in all_rows:
                all_rows[key].extend(metrics[key])
        summary = {
            "direction": _aggregate(
                all_rows["direction"],
                ["scenario", "method", "calibrated"],
                ["brier", "accuracy", "ece"],
            ),
            "queues": _aggregate(
                all_rows["queues"],
                ["scenario", "policy"],
                ["rise_rate", "fall_rate", "unknown_fraction", "selection_rate"],
            ),
            "money": _aggregate(
                all_rows["money"],
                ["scenario", "method", "stratum"],
                ["mae_dkk", "bias_dkk", "coverage90", "width90_dkk"],
            ),
            "paired_queues": _paired_queue(all_rows["queues"]),
        }
        frozen_json(root / "summary.json", summary)
        frozen_json(
            root / "evaluation-inputs.json",
            {
                "pins": list(source.pins.values()),
                "base_fits": 0,
                "calibration": pin(root / "calibration.json"),
                "protocol": pin(root / "protocol.json"),
                "clarification": pin(root / "protocol-clarification-v1.json"),
            },
        )
        frozen_json(
            result_path,
            {
                "version": "e4-evaluation-v1",
                "status": "completed_retrospective_not_promoted",
                "created_utc": utc(),
                "annual": annual,
                "labels": LABELS,
                "base_fits": 0,
                "temperature_fits": 9,
                "residual_states": 6,
                "resource": sample.finish(),
                "artifacts": [
                    pin(root / x)
                    for x in ("summary.json", "evaluation-inputs.json", "calibration.json")
                ],
                "scope": (
                    "Immutable E3 cohort and labels; new October outcome check is "
                    "separate and excluded from these comparisons"
                ),
            },
        )
    except Exception as exc:
        frozen_json(
            root / ("evaluation-failure-" + str(time.time_ns()) + ".json"),
            {"error": str(exc), "resource": sample.finish(), "created_utc": utc()},
        )
        raise
    return result_path
