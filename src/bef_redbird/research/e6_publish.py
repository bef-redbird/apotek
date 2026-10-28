"""One saved E6 question index; publication has no fitting or source acquisition."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e4_common import checked, read, utc
from .e6_experiment import ledger
from .e6_protocol import require
from .e6_results import aggregate
from .group_features import pin
from .training_contract import atomic_json

METHODS = {
    "probability_reference": "E5 probability selection",
    "amount_dkk": "DKK opportunity",
    "downside_aware": "Downside-aware monetary selection",
    "original_e4_current": "Original E4 current-only reference",
    "mae": "Absolute-error (median-oriented) model",
    "rmse": "Squared-error (mean-oriented) model",
    "huber": "Huber robust amount model",
    "3y": "Three recent years",
    "6y": "Six recent years",
    "full": "All usable history from 2008",
    "weighted": "Full history, three-year weight half-life",
}
MODES = {
    "current": "Current-only",
    "hybrid": "Conditional upcoming ABC with current-only fallback",
    "advance": "Conditional upcoming ABC component",
}


def model_tables(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    periods, queues = [], []
    for path in sorted((root / "evaluations").glob("*/result.json")):
        result = read(path)
        periods.append(pd.read_parquet(checked(result["files"]["periods.parquet"])))
        queues.extend(result.get("queue_periods", []))
    return pd.concat(periods, ignore_index=True), pd.DataFrame(queues)


def expected_periods(root: Path, protocol: dict[str, Any]) -> set[tuple[str, str]]:
    dataset_path = checked(protocol["pins"]["dataset"])
    calendar_ref = next(x for x in read(dataset_path)["files"] if x["role"] == "calendar")
    calendar = read(
        checked({**calendar_ref, "path": str(dataset_path.parent / calendar_ref["path"])})
    )
    return {
        (fold["name"], period["start"])
        for fold in protocol["folds"]
        if fold["name"] in {"spring", "summer"}
        for period in calendar["periods"]
        if fold["start"] <= period["start"] < fold["end"]
    }


def cohort_proofs(root: Path) -> dict[tuple[str, str, str, str, str], str]:
    """Exact candidate identities and typed labels, independently of metric counts."""
    proofs = {}
    keys = ["row_key", "package_number", "period_index", "period_start", "identity_episode_id"]
    labels = ["next_status", "next_direction", "next_delta_ore", "next_maturity_date"]
    for path in sorted((root / "evaluations").glob("*/result.json")):
        result = read(path)
        if not result["summaries"]:
            continue
        identity = result["summaries"][0]
        task, investigation, fold = (identity[x] for x in ("task", "investigation", "fold"))
        columns = [
            *keys,
            *labels,
            "history",
            "information",
            "route" if task == "direction" else "method",
        ]
        frame = pd.read_parquet(checked(result["files"]["predictions.parquet"]), columns=columns)
        if task == "direction":
            frame = frame.loc[frame.route.eq("routed")]
        arm = "method" if investigation == "C" else "history"
        for (method, mode), part in frame.groupby([arm, "information"], sort=True):
            if part.row_key.duplicated().any():
                raise ResearchError("e6_gate_duplicate_comparison_identity")
            values = part.sort_values("row_key")[[*keys, *labels]]
            digest = hashlib.sha256(
                pd.util.hash_pandas_object(values, index=False).to_numpy().tobytes()
            ).hexdigest()
            proofs[(investigation, task, fold, str(mode), str(method))] = digest
    return proofs


def paired_coverage(
    old: pd.DataFrame,
    new: pd.DataFrame,
    expected: set[tuple[str, str]] | None,
    proofs: dict[tuple[str, str, str, str, str], str] | None,
    investigation: str,
    task: str,
    mode: str,
    reference: str,
    candidate: str,
) -> tuple[pd.DataFrame, dict[str, bool]]:
    columns = ["fold", "period_start"]
    old, new = old.copy(), new.copy()
    for frame in (old, new):
        frame["period_start"] = pd.to_datetime(frame.period_start).dt.strftime("%Y-%m-%d")
    duplicate = old.duplicated(columns).any() or new.duplicated(columns).any()
    old_keys, new_keys = (
        set(map(tuple, old[columns].to_numpy())),
        set(map(tuple, new[columns].to_numpy())),
    )
    joined = (
        old.merge(new, on=columns, suffixes=("_ref", "_new")) if not duplicate else pd.DataFrame()
    )
    counts_equal = (
        not duplicate
        and bool(len(joined))
        and all(
            joined[column + "_ref"].eq(joined[column + "_new"]).all()
            for column in ("candidates", "observed", "unknown")
        )
    )
    counts_valid = counts_equal and all(
        joined["candidates_" + side]
        .eq(joined["observed_" + side] + joined["unknown_" + side])
        .all()
        and joined[[column + "_" + side for column in ("candidates", "observed", "unknown")]]
        .ge(0)
        .all()
        .all()
        for side in ("ref", "new")
    )
    proof_map = proofs or {}
    exact = bool(proof_map) and all(
        proof_map.get((investigation, task, fold, mode, reference)) is not None
        and proof_map.get((investigation, task, fold, mode, reference))
        == proof_map.get((investigation, task, fold, mode, candidate))
        for fold in ("spring", "summer")
    )
    return joined, {
        "unique_period_keys": not duplicate,
        "both_expected_folds": {key[0] for key in old_keys}
        == {"spring", "summer"}
        == {key[0] for key in new_keys},
        "all_frozen_periods_including_unknown": expected is not None
        and old_keys == new_keys == expected,
        "identical_candidate_observed_unknown_counts": bool(counts_valid),
        "exact_candidate_and_label_keys_values": bool(exact),
    }


def direction_gates(
    periods: pd.DataFrame,
    queues: pd.DataFrame,
    expected: set[tuple[str, str]] | None = None,
    proofs: dict[tuple[str, str, str, str, str], str] | None = None,
) -> dict[str, Any]:
    if "route" not in periods:
        return {"reference_retained": True, "reason": "Direction comparisons not completed"}
    frame = periods.loc[
        periods.task.eq("direction")
        & periods.stratum.eq("all")
        & periods.route.eq("routed")
        & periods.calibration.eq("calibrated")
        & periods.fold.isin(["spring", "summer"])
    ]
    results = {}
    for history in ("6y", "full"):
        checks = []
        for mode in ("current", "hybrid"):
            baseline = frame.loc[frame.history.eq("3y") & frame.information.eq(mode)]
            candidate = frame.loc[frame.history.eq(history) & frame.information.eq(mode)]
            joined, coverage = paired_coverage(
                baseline, candidate, expected, proofs, "D", "direction", mode, "3y", history
            )
            known = joined.loc[joined.observed_ref.gt(0)] if len(joined) else joined
            coverage["scored_evidence_in_both_folds"] = (
                set(known.fold) == {"spring", "summer"} if len(known) else False
            )
            coverage["finite_scored_metrics"] = (
                bool(len(known))
                and np.isfinite(
                    known[
                        [name + "_" + side for name in ("brier", "ece") for side in ("ref", "new")]
                    ].to_numpy()
                )
                .all()
                .item()
            )
            complete = all(coverage.values())
            gain = float((known.brier_ref - known.brier_new).mean()) if complete else None
            worst_fold = (
                float((known.brier_new - known.brier_ref).groupby(known.fold).mean().max())
                if complete
                else None
            )
            calibration_ok = (
                bool(known.ece_new.mean() <= known.ece_ref.mean() * 1.05) if complete else False
            )
            q = queues.loc[queues.fold.isin(["spring", "summer"]) & queues.information.eq(mode)]
            oldq = q.loc[q.history.eq("3y")]
            newq = q.loc[q.history.eq(history)]
            queue_rows, queue_coverage = paired_coverage(
                oldq,
                newq,
                expected,
                proofs,
                "D",
                "direction",
                mode,
                "3y",
                history,
            )
            # Queue observed/unknown counts describe selected cases and may differ.
            queue_coverage.pop("identical_candidate_observed_unknown_counts")
            queue_coverage["same_origin_population_counts"] = bool(
                len(queue_rows) and queue_rows.candidates_ref.eq(queue_rows.candidates_new).all()
            )
            queue_coverage["selected_counts_reconcile"] = bool(len(queue_rows)) and all(
                queue_rows["selected_" + side]
                .eq(queue_rows["observed_" + side] + queue_rows["unknown_" + side])
                .all()
                for side in ("ref", "new")
            )
            if len(queue_rows) and len(joined):
                queue_origins = queue_rows.merge(
                    joined[["fold", "period_start", "candidates_ref", "candidates_new"]],
                    on=["fold", "period_start"],
                    suffixes=("_queue", "_cohort"),
                )
                queue_coverage["queue_origin_counts_match_scored_cohort"] = len(
                    queue_origins
                ) == len(joined) and all(
                    queue_origins["candidates_" + side + "_queue"]
                    .eq(queue_origins["candidates_" + side + "_cohort"])
                    .all()
                    for side in ("ref", "new")
                )
            else:
                queue_coverage["queue_origin_counts_match_scored_cohort"] = False
            queue_complete = all(queue_coverage.values()) and complete
            risk_known = (
                queue_rows.loc[queue_rows.observed_ref.gt(0) & queue_rows.observed_new.gt(0)]
                if len(queue_rows)
                else queue_rows
            )
            symmetric_support = bool(len(queue_rows)) and queue_rows.observed_ref.gt(0).equals(
                queue_rows.observed_new.gt(0)
            )
            downside_ok = bool(
                queue_complete
                and symmetric_support
                and len(risk_known)
                and np.isfinite(
                    risk_known[
                        ["downside_dkk_per_observed_new", "downside_dkk_per_observed_ref"]
                    ].to_numpy()
                ).all()
                and risk_known.downside_dkk_per_observed_new.mean()
                <= risk_known.downside_dkk_per_observed_ref.mean() * 1.05
            )
            checks.append(
                {
                    "information": mode,
                    "complete_common_periods": complete,
                    "coverage_checks": coverage,
                    "brier_gain": gain,
                    "largest_fold_brier_worsening": worst_fold,
                    "calibration_within5percent": calibration_ok,
                    "calibration_measure": (
                        "Equal-period mean multiclass expected calibration error; "
                        "candidate <=1.05 times reference"
                    ),
                    "downside_within5percent": downside_ok,
                    "complete_paired_queue_periods": queue_complete,
                    "downside_denominator": (
                        "Equal-period mean downside DKK per observed selected pack; "
                        "both-empty periods omitted, asymmetric missing support fails"
                    ),
                    "queue_diagnostics": {
                        side: {
                            "total_downside_dkk": float(rows.downside_dkk.sum()),
                            "selected": int(rows.selected.sum()),
                            "unknown_selected": int(rows.unknown.sum()),
                            "pooled_unknown_rate": float(rows.unknown.sum() / rows.selected.sum())
                            if rows.selected.sum()
                            else None,
                        }
                        for side, rows in (("reference", oldq), ("candidate", newq))
                    },
                    "passes": bool(
                        complete
                        and gain is not None
                        and gain >= 0.0005
                        and worst_fold is not None
                        and worst_fold <= 0.001
                        and calibration_ok
                        and downside_ok
                    ),
                }
            )
        results[history] = {"checks": checks, "passes": all(c["passes"] for c in checks)}
    passing = [h for h, d in results.items() if d["passes"]]
    return {
        "history_arms": results,
        "eligible_research_candidates": passing,
        "reference_retained": not passing,
        "operational_promotion": False,
        "weighted_scope": "Summer only diagnostic; cannot establish across-fold consistency.",
    }


def amount_gates(
    periods: pd.DataFrame,
    expected: set[tuple[str, str]] | None = None,
    proofs: dict[tuple[str, str, str, str, str], str] | None = None,
) -> dict[str, Any]:
    base = periods.loc[
        periods.task.eq("amount")
        & periods.stratum.eq("all")
        & periods.support.eq("supported")
        & periods.fold.isin(["spring", "summer"])
    ]
    decisions: dict[str, Any] = {}
    for investigation, arm, reference, candidates in [
        ("C", "method", "mae", ["huber"]),
        ("D", "history", "3y", ["6y", "full"]),
    ]:
        part = base.loc[base.investigation.eq(investigation)]
        for candidate in candidates:
            checks = []
            for mode in ("current", "hybrid"):
                old = part.loc[part[arm].eq(reference) & part.information.eq(mode)]
                new = part.loc[part[arm].eq(candidate) & part.information.eq(mode)]
                joined, coverage = paired_coverage(
                    old, new, expected, proofs, investigation, "amount", mode, reference, candidate
                )
                known = joined.loc[joined.observed_ref.gt(0)] if len(joined) else joined
                coverage["scored_evidence_in_both_folds"] = (
                    set(known.fold) == {"spring", "summer"} if len(known) else False
                )
                coverage["finite_scored_metrics"] = (
                    bool(len(known))
                    and np.isfinite(
                        known[
                            [
                                name + "_" + side
                                for name in ("mae_dkk", "rmse_dkk", "bias_dkk")
                                for side in ("ref", "new")
                            ]
                        ].to_numpy()
                    )
                    .all()
                    .item()
                )
                complete = all(coverage.values())

                def ratio(metric: str, frame: pd.DataFrame = known) -> float | None:
                    old_mean = float(frame[metric + "_ref"].mean())
                    new_mean = float(frame[metric + "_new"].mean())
                    if not np.isfinite([old_mean, new_mean]).all() or min(old_mean, new_mean) < 0:
                        return None
                    if old_mean == 0:
                        return 1.0 if new_mean == 0 else None
                    return new_mean / old_mean

                mae_ratio = ratio("mae_dkk") if complete else None
                rmse_ratio = ratio("rmse_dkk") if complete else None
                bias_ok = (
                    bool(abs(known.bias_dkk_new.mean()) <= abs(known.bias_dkk_ref.mean()))
                    if complete
                    else False
                )
                if complete and mae_ratio is not None and rmse_ratio is not None:
                    passes = (
                        (mae_ratio <= 0.99 and rmse_ratio <= 1)
                        if investigation == "C"
                        else (
                            (mae_ratio <= 0.99 or rmse_ratio <= 0.99)
                            and max(mae_ratio, rmse_ratio) <= 1.01
                            and bias_ok
                        )
                    )
                else:
                    passes = False
                checks.append(
                    {
                        "information": mode,
                        "complete": complete,
                        "coverage_checks": coverage,
                        "mae_ratio": mae_ratio,
                        "rmse_ratio": rmse_ratio,
                        "absolute_bias_not_worse": bias_ok,
                        "absolute_bias_is_gate": investigation == "D",
                        "passes": passes,
                    }
                )
            decisions[investigation + "_" + candidate] = {
                "checks": checks,
                "passes_both_modes": all(c["passes"] for c in checks),
            }
    return decisions


def readable_model_rows(
    periods: pd.DataFrame, investigation: str, task: str
) -> list[dict[str, Any]]:
    part = periods.loc[
        periods.investigation.eq(investigation) & periods.task.eq(task) & periods.stratum.eq("all")
    ].copy()
    if task == "amount":
        part = part.loc[part.support.eq("supported")]
    else:
        part = part.loc[part.route.eq("routed") & part.calibration.eq("calibrated")]
    part["evaluation"] = np.where(
        part.fold.eq("earlier"), "June–December 2025 diagnostic", "June–October 2026"
    )
    if investigation == "D" and task == "direction" and part.history.eq("weighted").any():
        summer = part.loc[
            part.fold.eq("summer") & part.history.isin(["3y", "full", "weighted"])
        ].copy()
        summer["evaluation"] = "September–October 2026 weighting comparison"
        part = pd.concat([part.loc[part.history.ne("weighted")], summer], ignore_index=True)
    rows = []
    group_columns = ["evaluation", "information", "method" if investigation == "C" else "history"]
    for keys, group in part.groupby(group_columns, sort=True):
        observed = group.loc[group.observed.gt(0)]
        row: dict[str, Any] = {
            "Evaluation": keys[0],
            "Information": MODES[str(keys[1])],
            "Method": METHODS.get(str(keys[2]), str(keys[2])),
            "Scored tariff periods": len(observed),
            "Observed cases": int(group.observed.sum()),
            "Unknown cases": int(group.unknown.sum()),
        }
        if task == "amount":
            row.update(
                {
                    "Mean absolute error DKK/pack ↓": float(observed.mae_dkk.mean()),
                    "Root mean squared error DKK/pack ↓": float(observed.rmse_dkk.mean()),
                    "Signed bias DKK/pack (zero best)": float(observed.bias_dkk.mean()),
                    "Predicted aggregate movement DKK": float(group.predicted_total_dkk.sum()),
                    "Observed aggregate movement DKK": float(group.observed_total_dkk.sum()),
                }
            )
        else:
            row.update(
                {
                    "Brier probability error ↓": float(observed.brier.mean()),
                    "Calibration error ↓": float(observed.ece.mean()),
                    "Logarithmic probability loss ↓": float(observed.logloss.mean()),
                }
            )
        rows.append(row)
    return rows


def short_answers(
    finance: dict[str, Any],
    cito: dict[str, Any],
    amounts: dict[str, Any],
) -> dict[str, dict[str, str]]:
    passing = [
        METHODS.get(x["policy"], x["policy"])
        for x in finance["decision_gates"]
        if x["research_replacement_gate_passed"]
    ]
    answer_a = (
        f"Qualifying monetary research candidates: {', '.join(passing)}."
        if passing
        else "Probability selection remains the reference; no monetary policy passed "
        "the frozen replacement rule."
    )
    corrected = cito["results"]["corrected_wrong_only"]
    natural = corrected["groups"]["all_eligible"]["queues"]["natural"]
    clean = cito["clean_matched"]["results"]
    matched = clean["corrected_wrong_only"]["groups"]["all_eligible"]["queues"]["matched"]
    actual_natural = cito["results"]["actual"]["groups"]["all_eligible"]["queues"]["natural"][
        "selected"
    ]
    actual_matched = clean["actual"]["groups"]["all_eligible"]["queues"]["matched"]["selected"]
    no_report = clean["no_report_current"]["groups"]["all_eligible"]["queues"]["matched"][
        "selected"
    ]
    report_comparison = (
        "At the same workload, the actual report produced DKK "
        f"{actual_matched['one_pack_price_exposure_dkk']:,.2f} observed one-pack movement "
        f"versus {no_report['one_pack_price_exposure_dkk']:,.2f} without a report. "
        "Downside was DKK "
        f"{actual_matched['one_pack_downside_dkk']:,.2f} versus "
        f"{no_report['one_pack_downside_dkk']:,.2f}. "
        "This is one snapshot's price exposure, not pharmacy savings."
    )
    downside_changes = [
        natural["selected"]["one_pack_downside_dkk"] - actual_natural["one_pack_downside_dkk"],
        matched["selected"]["one_pack_downside_dkk"] - actual_matched["one_pack_downside_dkk"],
    ]
    downside_answer = (
        "Downside was unchanged in both queues."
        if all(abs(x) < 1e-8 for x in downside_changes)
        else "Downside changes differ by queue; inspect the paired amounts."
    )
    answer_b = (
        f"Correcting {cito['correction']['eligible_wrong_codes']} eligible wrong codes changed "
        f"{corrected['changed_predictions']} predictions. Natural queue: "
        f"{natural['removed']['cases']} removed, {natural['added']['cases']} added. "
        f"Same-rule matched queue: {matched['removed']['cases']} removed, "
        f"{matched['added']['cases']} added. {downside_answer}"
    )
    changes = [
        natural["selected"]["one_pack_price_exposure_dkk"]
        - actual_natural["one_pack_price_exposure_dkk"],
        matched["selected"]["one_pack_price_exposure_dkk"]
        - actual_matched["one_pack_price_exposure_dkk"],
    ]
    gate_c = amounts["C_huber"]
    descriptions = []
    for check in gate_c["checks"]:
        label = (
            "Current-only"
            if check["information"] == "current"
            else "Conditional upcoming classifications"
        )
        ratio = check["mae_ratio"]
        rmse = check["rmse_ratio"]
        if not check["complete"] or ratio is None or rmse is None:
            descriptions.append(f"{label}: comparison incomplete.")
            continue
        typical = "improved" if ratio < 1 else "worsened" if ratio > 1 else "was unchanged"
        large = "improved" if rmse < 1 else "worsened" if rmse > 1 else "was unchanged"
        descriptions.append(
            f"{label}: typical absolute error {typical} ({100 * (ratio - 1):+.2f}%); "
            f"root mean squared error {large} ({100 * (rmse - 1):+.2f}%)."
        )
    return {
        "A": {
            "status": "improved" if passing else "mixed",
            "answer": answer_a,
            "practical_meaning": "Qualifying results support further research; "
            "operational promotion remains separate."
            if passing
            else "Keep probability as the reference; inspect monetary and downside "
            "estimates as supporting information.",
        },
        "B": {
            "status": "mixed" if corrected["changed_predictions"] else "unchanged",
            "answer": answer_b,
            "limitations": f"Correction changed natural one-pack movement by DKK {changes[0]:+.2f} "
            f"and same-rule matched movement by DKK {changes[1]:+.2f}. "
            "One snapshot cannot establish future report reliability.",
            "practical_meaning": report_comparison,
        },
        "C": {
            "status": "improved"
            if gate_c["passes_both_modes"]
            else "mixed"
            if all(c["complete"] for c in gate_c["checks"])
            else "inconclusive",
            "answer": " ".join(descriptions),
            "limitations": ("Passes" if gate_c["passes_both_modes"] else "Does not pass")
            + " the frozen both-mode improvement rule. Huber does not automatically estimate "
            "the average future monetary return. No Huber ranking or interval refit was tested.",
            "practical_meaning": "The frozen gate supports further Huber research; "
            "promotion remains separate."
            if gate_c["passes_both_modes"]
            else "Retain existing references; mode-specific findings remain separate "
            "research evidence.",
        },
    }


def resource_summary(
    root: Path,
    protocol: dict[str, Any],
    fit_ledger: dict[str, Any],
    model: dict[str, Any],
) -> dict[str, Any]:
    new_states = sum(
        len(read(path)["states"])
        for path in (root / "evaluations").glob("*/calibration-checkpoint.json")
    )
    reused_states = {
        (path.parent.name, key)
        for path in (root / "evaluations").glob("*/result.json")
        for key, value in read(path).get("calibration", {}).items()
        if value.get("reused")
    }
    full_declared = {spec["id"] for spec in protocol["matrix"] if spec["history"] == "full"}
    full_receipts = [
        trial for trial in fit_ledger["trials"] if trial["spec"]["id"] in full_declared
    ]
    full_completed = {
        trial["spec"]["id"] for trial in full_receipts if trial["status"] == "completed"
    }
    full_terminal = {
        trial["spec"]["id"]
        for trial in full_receipts
        if trial["status"] in {"completed", "failed", "skipped"}
    }
    limits = protocol["resource_limits"]
    accounting_path = root / "review/reuse-accounting.json"
    accounting = read(accounting_path) if accounting_path.exists() else {}
    return {
        "successful_new_base_fits": fit_ledger["completed"],
        "failed_new_base_fits": fit_ledger["failed"],
        "skipped_slots": fit_ledger["skipped"],
        "running_trials": sum(t["status"] == "running" for t in fit_ledger["trials"]),
        "declared_slots": len(protocol["matrix"]),
        "fit_attempts": fit_ledger["attempts"],
        "reused_model_comparison_fits": len(model["reused_fits"]),
        "reused_model_comparison_scope": "C/D comparisons only; shared fits counted once",
        "declared_unique_accepted_models_across_A_D": accounting.get(
            "milestone_unique_declared_accepted_model_hashes"
        ),
        "materialized_C_D_reuse": accounting.get(
            "materialized_evaluation_reuse_unique_model_hashes", {}
        ),
        "fit_hours": fit_ledger["fit_seconds"] / 3600,
        "successful_measured_fit_hours": sum(
            t.get("fit_seconds", 0) for t in fit_ledger["trials"] if t["status"] == "completed"
        )
        / 3600,
        "interrupted_fit_budget_charge_upper_bound_hours": sum(
            t.get("fit_seconds", 0)
            for t in fit_ledger["trials"]
            if t.get("fit_seconds_is_conservative_upper_bound")
        )
        / 3600,
        "fit_hours_scope": "Measured completed fits plus conservative elapsed-time charges "
        "for externally interrupted attempts; a missing interruption score is not a loss.",
        "trial_elapsed_hours": fit_ledger["elapsed_seconds"] / 3600,
        "peak_trial_GiB": max(
            (t.get("resource", {}).get("sampled_peak_rss_bytes", 0) for t in fit_ledger["trials"]),
            default=0,
        )
        / 1024**3,
        "trials_without_final_peak_memory_measurement": sum(
            bool(t.get("resource", {}).get("peak_rss_unavailable")) for t in fit_ledger["trials"]
        ),
        "threads_per_fit": limits["threads"],
        "full_history_successful": len(full_completed),
        "full_history_declared": len(full_declared),
        "full_history_pending": len(full_declared - full_terminal),
        "new_scalar_calibration_fits": new_states,
        "reused_scalar_calibrations": len(reused_states),
        "scalar_calibrations": new_states + len(reused_states),
        "fit_allowance_hours": limits["aggregate_fit_seconds"] / 3600,
        "full_history_fit_allowance_hours": limits["full_fit_seconds"] / 3600,
    }


def history_answer(
    periods: pd.DataFrame,
    direction: dict[str, Any],
    amounts: dict[str, Any],
    resources: dict[str, Any],
) -> str:
    answer = (
        f"{resources['full_history_successful']} of {resources['full_history_declared']} "
        f"declared full-history fits completed; {resources['full_history_pending']} pending. "
    )
    answer += (
        "Primary 2026 direction forecasts retain the three-year reference under the frozen rule. "
        if direction["reference_retained"]
        else "Longer history qualifies for further direction-forecast research. "
    )
    qualifying = [
        METHODS[key.removeprefix("D_")]
        for key, value in amounts.items()
        if key.startswith("D_") and value["passes_both_modes"]
    ]
    answer += (
        "Primary 2026 amount forecasts: "
        + ", ".join(qualifying)
        + " qualify for further research. "
        if qualifying
        else "No longer-history amount arm passes the frozen rule. "
    )
    full_amount = amounts.get("D_full", {})
    for check in full_amount.get("checks", []):
        if check["complete"]:
            mode = "current-only" if check["information"] == "current" else "conditional"
            answer += (
                f"Full-history {mode} absolute error changes "
                f"{100 * (check['mae_ratio'] - 1):+.2f}% and root mean squared error "
                f"{100 * (check['rmse_ratio'] - 1):+.2f}% versus three years. "
            )
    weighted = periods.loc[
        periods.task.eq("direction")
        & periods.fold.eq("summer")
        & periods.stratum.eq("all")
        & periods.route.eq("routed")
        & periods.calibration.eq("calibrated")
        & periods.observed.gt(0)
    ]
    comparisons = []
    calibration_changes = []
    scored_periods: set[Any] = set()
    for mode in ("current", "hybrid"):
        part = weighted.loc[weighted.information.eq(mode)]
        recent = part.loc[part.history.eq("weighted")]
        for baseline in ("full", "3y"):
            reference = part.loc[part.history.eq(baseline)]
            joined = recent.merge(reference, on="period_start", suffixes=("_new", "_ref"))
            if (
                len(joined)
                and not recent.period_start.duplicated().any()
                and not reference.period_start.duplicated().any()
                and set(recent.period_start) == set(reference.period_start)
            ):
                comparisons.append(
                    (mode, baseline, float((joined.brier_new - joined.brier_ref).mean()))
                )
                if baseline == "3y" and "ece_new" in joined and joined.ece_ref.mean() > 0:
                    calibration_changes.append(
                        (mode, float(100 * (joined.ece_new.mean() / joined.ece_ref.mean() - 1)))
                    )
                scored_periods.update(recent.period_start)
    if len(comparisons) == 4:
        descriptions = []
        for baseline in ("full", "3y"):
            changes = [change for _, arm, change in comparisons if arm == baseline]
            effect = (
                "lowered probability error in both modes"
                if all(x < 0 for x in changes)
                else "raised probability error in both modes"
                if all(x > 0 for x in changes)
                else "had mixed probability-error effects across modes"
            )
            descriptions.append(f"{effect} versus {METHODS[baseline].lower()}")
        answer += (
            "Recent-observation weighting " + "; ".join(descriptions) + ". "
            f"That comparison has only {len(scored_periods)} scored summer periods."
        )
        if len(calibration_changes) == 2:
            details = []
            for mode, change in calibration_changes:
                label = "current-only" if mode == "current" else "conditional"
                details.append(f"{change:+.2f}% {label}")
            answer += (
                " Calibration error changes "
                + " and ".join(details)
                + " versus three years; increases are worse. "
                "Weighting remains a limited diagnostic."
            )
    else:
        answer += "The optional recent-observation weighting comparison is incomplete."
    return answer


def earlier_amount_caveat(periods: pd.DataFrame) -> str:
    part = periods.loc[
        periods.investigation.eq("D")
        & periods.task.eq("amount")
        & periods.fold.eq("earlier")
        & periods.stratum.eq("all")
        & periods.support.eq("supported")
        & periods.observed.gt(0)
    ]
    worsened = []
    typical_improved = []
    for mode, label in (("current", "current-only"), ("hybrid", "conditional")):
        old = part.loc[part.information.eq(mode) & part.history.eq("3y")]
        new = part.loc[part.information.eq(mode) & part.history.eq("full")]
        if not len(old) or set(old.period_start) != set(new.period_start):
            return "The earlier 2025 amount diagnostic is incomplete."
        typical_improved.append(new.mae_dkk.mean() < old.mae_dkk.mean())
        for key, name in (
            ("mae_dkk", "average absolute error"),
            ("rmse_dkk", "root mean squared error"),
            ("bias_dkk", "absolute bias"),
        ):
            old_value, new_value = old[key].mean(), new[key].mean()
            if key == "bias_dkk":
                old_value, new_value = abs(old_value), abs(new_value)
            if not np.isfinite([old_value, new_value]).all():
                return "The earlier 2025 amount diagnostic has incomplete metrics."
            if new_value > old_value:
                worsened.append(label + " " + name)
    if not worsened:
        return "Full history also improves or matches these amount measures in the 2025 diagnostic."
    return (
        "The 2025 amount diagnostic is mixed: full history worsens "
        + ", ".join(worsened)
        + (
            ", despite lower average absolute error in both modes."
            if all(typical_improved)
            else "."
        )
    )


def publish(root: Path) -> dict[str, Any]:
    protocol = require(root)
    model = aggregate(root)
    periods, queues = model_tables(root)
    finance = read(root / "investigation-a/results.json")
    cito = read(root / "cito/summary-v2.json")
    fit_ledger = ledger(root)
    expected = expected_periods(root, protocol)
    proofs = cohort_proofs(root)
    direction = direction_gates(periods, queues, expected, proofs)
    amounts = amount_gates(periods, expected, proofs)
    answers = short_answers(finance, cito, amounts)
    a_rows = []
    for row in finance["summary"]:
        if row["fold"] == "all_2026":
            a_rows.append(
                {
                    "Method": METHODS.get(row["policy"], row["policy"]),
                    "Queue": "Natural thresholds"
                    if row["workload"] == "natural"
                    else "Matched workload diagnostic",
                    "Allocation": "One pack"
                    if row["allocation"] == "per_pack"
                    else "DKK 100,000 per period, integer packs",
                    "Selected": row["selected"],
                    "Unknown outcomes": row["unknown_selected"],
                    "Packs in scenario": row["quantity"],
                    "Net observed price exposure DKK": row["pooled_net_exposure_dkk"],
                    "Aggregate downside DKK ↓": -row["pooled_negative_exposure_dkk"],
                    "Equal-period movement DKK/observed pack ↑": row[
                        "equal_period_net_dkk_per_observed_pack"
                    ],
                }
            )
    b_rows = []
    for workload in ("natural", "matched"):
        results = cito["results"] if workload == "natural" else cito["clean_matched"]["results"]
        for scenario, label in [
            ("actual", "Actual Cito report"),
            ("corrected_wrong_only", "Corrected-code retrospective diagnostic"),
            ("no_report_current", "No report, genuine current-only"),
        ]:
            row = results[scenario]["groups"]["all_eligible"]["queues"][workload]["selected"]
            b_rows.append(
                {
                    "Scenario": label,
                    "Queue": "Natural" if workload == "natural" else "Same-rule matched diagnostic",
                    "Selected": row["cases"],
                    "Rises": row["rises"],
                    "Unchanged": row["unchanged"],
                    "Falls": row["falls"],
                    "Unknown": row["unknown"],
                    "One-pack net movement DKK ↑": row["one_pack_price_exposure_dkk"],
                    "One-pack downside DKK ↓": row["one_pack_downside_dkk"],
                }
            )
    resources = resource_summary(root, protocol, fit_ledger, model)
    full_answer = history_answer(periods, direction, amounts, resources)
    questions = [
        {
            "id": "A",
            "title": "Which selection works without upcoming ABC codes?",
            **answers["A"],
            "comparison": "Same current-only E5 forecasts and eligibility; natural "
            "thresholds, matched workloads and integer fixed capital.",
            "limitations": "Fixed capital strongly depends on large quantities of cheap "
            "packs. Matched expansion includes naturally rejected cases. These are "
            "price exposures, not orders or pharmacy profit.",
        },
        {
            "id": "B",
            "title": "What did the actual Cito errors change?",
            **answers["B"],
            "comparison": "Original E4 models/calibration: actual report, only "
            "incorrect codes corrected, and no advance report.",
        },
        {
            "id": "C",
            "title": "Did Huber improve beyond development screening?",
            **answers["C"],
            "comparison": "Four declared outer Huber fits at April/July 2026 cutoffs, "
            "against compatible three-year median and mean-oriented models.",
        },
        {
            "id": "D",
            "title": "Did older or recency-weighted history help?",
            "status": "inconclusive" if resources["full_history_pending"] else "mixed",
            "answer": full_answer,
            "comparison": "Three/six/full history under unchanged 160-tree direction "
            "and squared-error amount recipes; spring/summer 2026 and earlier 2025 "
            "diagnostic. Optional fixed recency weighting is summer-only.",
            "limitations": earlier_amount_caveat(periods)
            + " Advance features are absent before 2019; old cases remain "
            "with missing codes. All years are exposed retrospective research, and "
            "older-year settings were selected later. The best history under this "
            "recipe is not universally optimal.",
            "practical_meaning": "Direction and amount can need different training histories. "
            "A better amount forecast alone does not establish a better purchasing queue. "
            "No operating model, November shortlist or procurement policy changes.",
        },
    ]
    summary = {
        "version": "e6-owner-summary-v1",
        "questions": questions,
        "sections": {
            "A": {
                "title": "Current-only selection",
                "records": a_rows,
                "limitations": questions[0]["limitations"],
            },
            "B": {
                "title": "Actual Cito consequences",
                "records": b_rows,
                "limitations": questions[1]["limitations"],
            },
            "C": {
                "title": "Huber amount forecast errors",
                "records": readable_model_rows(periods, "C", "amount"),
                "limitations": "Errors average tariff periods equally; lower is better. "
                "Mean absolute error is the average kroner miss; root mean squared error "
                "gives larger mistakes more weight. Signed bias is prediction minus "
                "observed movement. Unknowns are unscored.",
            },
            "D": {
                "title": "Training history: direction and amount",
                "records": [
                    *readable_model_rows(periods, "D", "direction"),
                    *readable_model_rows(periods, "D", "amount"),
                ],
                "limitations": questions[3]["limitations"],
            },
        },
        "resources": resources,
        "exposure": protocol["exposure"],
        "decisions": {
            "direction_history": direction,
            "amount": amounts,
            "exact_comparison_cohorts": [
                dict(
                    zip(
                        ("investigation", "task", "fold", "information", "arm"),
                        identity,
                        strict=True,
                    )
                )
                | {"keys_and_labels_sha256": digest}
                for identity, digest in sorted(proofs.items())
            ],
            "operational_promotion": False,
        },
        "created_utc": utc(),
    }
    atomic_json(root / "summary.json", summary)
    atomic_json(
        root / "index.json",
        {
            "version": "e6-index-v1",
            "status": "research_not_promoted",
            "artifacts": {
                "summary": pin(root / "summary.json"),
                "protocol": pin(root / "protocol.json"),
                "models": pin(root / "model-results.json"),
                "A": pin(root / "investigation-a/results.json"),
                "A_diagnostics": pin(root / "investigation-a/diagnostics.json"),
                "B": pin(root / "cito/summary-v2.json"),
            },
        },
    )
    atomic_json(
        root / "current.json",
        {
            "version": "e6-current-v1",
            "index": pin(root / "index.json"),
            "status": "research_not_promoted",
            "acceptance_path": "acceptance.json",
        },
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    print(publish(parser.parse_args().root.resolve())["version"])
