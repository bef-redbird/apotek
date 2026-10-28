"""Finite E5 protocol and fit ledger, frozen before comparative scores."""

from __future__ import annotations

import importlib.metadata
import shutil
from pathlib import Path
from typing import Any

import psutil

from .dataset import ResearchError, digest
from .e2_experiment import SETTINGS, utc
from .e4_common import checked, frozen_json, read
from .group_features import pin

VERSION = "e5-bounded-v1"
TUNING = {
    "early_stop": {"iterations": 480, "depth": 6, "learning_rate": 0.08, "l2_leaf_reg": 5.0},
    "shallow": {"iterations": 480, "depth": 4, "learning_rate": 0.05, "l2_leaf_reg": 10.0},
    "slow": {"iterations": 640, "depth": 6, "learning_rate": 0.04, "l2_leaf_reg": 10.0},
}
FOLDS: list[dict[str, Any]] = [
    {
        "name": "screen",
        "fit": "2025-01-01",
        "early_end": "2025-04-01",
        "cal_start": "2025-04-01",
        "cal_end": "2025-06-01",
        "start": "2025-06-01",
        "end": "2026-01-01",
        "reference_year": 2025,
    },
    {
        "name": "spring",
        "fit": "2026-04-01",
        "cal_start": "2026-04-01",
        "cal_end": "2026-06-01",
        "start": "2026-06-01",
        "end": "2026-09-01",
        "reference_year": 2026,
    },
    {
        "name": "summer",
        "fit": "2026-07-01",
        "cal_start": "2026-07-01",
        "cal_end": "2026-09-01",
        "start": "2026-09-01",
        "end": "2026-10-26",
        "reference_year": 2026,
    },
    {
        "name": "final",
        "fit": "2026-09-01",
        "cal_start": "2026-09-01",
        "cal_end": "2026-10-26",
        "start": "2026-10-26",
        "end": "2026-11-09",
        "reference_year": 2026,
    },
]


def matrix() -> list[dict[str, Any]]:
    out = []
    for fold in FOLDS:
        if fold["name"] == "screen":
            methods = [(x, "direction") for x in TUNING] + [
                ("rmse", "amount"),
                ("huber", "amount"),
                ("quantile", "amount"),
                ("persistence", "persistence"),
            ]
        elif fold["name"] == "final":
            methods = [
                ("selected_direction", "direction"),
                ("rmse", "amount"),
                ("quantile", "amount"),
                ("persistence", "persistence"),
            ]
        else:
            methods = [
                ("fresh", "direction"),
                ("tuned", "direction"),
                ("mae", "amount"),
                ("rmse", "amount"),
                ("quantile", "amount"),
                ("persistence", "persistence"),
            ]
        for method, task in methods:
            for mode in ("current", "advance"):
                out.append(
                    {
                        "id": f"{fold['name']}-{method}-{mode}",
                        "fold": fold["name"],
                        "method": method,
                        "task": task,
                        "information": mode,
                        "feature_route": "broad" if task == "direction" else "equal",
                        "fit_cutoff": fold["fit"],
                        "status": "declared",
                    }
                )
    return out


def freeze(root: Path) -> dict[str, Any]:
    if (root / "protocol.json").exists():
        protocol = read(root / "protocol.json")
        if read(root / "freeze.json")["protocol"]["sha256"] != digest(root / "protocol.json"):
            raise ResearchError("e5_protocol_changed")
        return protocol
    root.mkdir(parents=True, exist_ok=True)
    data = root.parent
    e3 = data / "research-e3/run-v1"
    prior = read(e3 / "protocol.json")
    paths = {k: Path(prior["pins"][k]["path"]) for k in ("dataset", "groups", "advance", "broader")}
    paths.update(
        {
            "e3_protocol": e3 / "protocol.json",
            "e3_ledger": e3 / "trial-ledger.json",
            "e3_result": e3 / "saved/result.json",
        }
    )
    paths.update(
        {
            "e4_" + k: data / "research-e4" / (k + ".json")
            for k in (
                "current",
                "acceptance",
                "protocol",
                "model-bundle",
                "lifecycle",
                "evaluation-inputs",
            )
        }
    )
    for p in paths.values():
        if not p.is_file():
            raise ResearchError("e5_missing_accepted_input")
    protocol = {
        "version": VERSION,
        "frozen_utc": utc(),
        "pins": {k: pin(p) for k, p in paths.items()},
        "exposure": (
            "All historical periods exposed retrospective development; no untouched confirmation."
        ),
        "authority": (
            "Bounded local E5; no promotion, orders, scheduler, uploads or Git publication."
        ),
        "information": (
            "Origin features plus separately named conditional T+1 ABC only; no "
            "future price, T+2 ABC, membership, survival or correction inputs. "
            "Actual report only in matching period; missing codes use genuine "
            "current-only model."
        ),
        "folds": FOLDS,
        "history_years": 3,
        "settings": SETTINGS,
        "versions": {
            x: importlib.metadata.version(x)
            for x in ("catboost", "numpy", "pandas", "pyarrow", "duckdb")
        },
        "direction": {
            "changed_component": (
                "Broader-peer direction only; equal/package branches reused at "
                "reference January cutoff. Show broad-supported matched subset "
                "and full hybrid separately."
            ),
            "freshness": (
                "Frozen January E4 vs unchanged settings at April/July cutoff, "
                "same three-year trailing window, inputs and population. Report "
                "raw and common earlier-held-out calibration plus original E4 "
                "policy."
            ),
            "tuning": TUNING,
            "early_stopping_rounds": 30,
            "selection": (
                "Smallest mean current/advance equal-period Brier on screen "
                "evaluation, with 0.0001 minimum improvement over frozen "
                "unchanged settings; tie unchanged then listed order. Best "
                "iteration chosen on Jan-March2025 only; later fits use that "
                "count, no outer early stopping. No has_time experiment: no "
                "concrete need beyond existing chronological gates."
            ),
        },
        "amount": {
            "objectives": {
                "mae": "MAE",
                "rmse": "RMSE",
                "huber": "Huber:delta=10",
                "quantile": "MultiQuantile:alpha=0.05,0.5,0.95",
            },
            "settings": (
                "Unchanged160/depth6/lr.08/L2=5 for loss isolation; equal peers, "
                "current and advance. Reuse January MAE screen. Huber screened "
                "once, no outer multiplication; RMSE and quantiles receive fixed "
                "outer validation regardless of result."
            ),
            "support": (
                "next=max(0,current+raw_delta), adjusted delta=next-current; raw "
                "retained. Quantiles sorted then nonnegative, crossing frequency "
                "retained; interval not expanded on outer outcomes."
            ),
            "interpretation": (
                "MAE median, RMSE mean-oriented, Huber robust not arithmetic "
                "mean, quantiles not expected returns. Never median times rise "
                "probability labelled profit."
            ),
            "reliability": (
                "Bias, sum predicted/observed DKK, MAE/RMSE/tails/top1%, fixed "
                "price bands <100/100-1000/>=1000, history<6, peer "
                "positive/zero/unknown; q05-q95 empirical coverage/width. No "
                "label clipping."
            ),
            "adoption": (
                "Mean-oriented RMSE is ranking research candidate even if error "
                "loses; no operational promotion. Quantile interval adopted for "
                "review only if outer pooled coverage>=.85 and every "
                "subgroup>=.70; otherwise clearly rejected experimental interval, "
                "retain E4 interval."
            ),
        },
        "persistence": {
            "target": (
                "Unconditional joint event next AIP > origin AIP AND T+2 AIP > "
                "origin AIP, both consecutive eligible same-identity "
                "observations. Missing, identity break and immature are separate "
                "statuses."
            ),
            "model": (
                "Equal-peer CatBoost Logloss160/depth6/lr.08/L2=5, current and "
                "advance; no actual next rise as input. Earlier calibration "
                "scalar binary temperature,0.5..3."
            ),
            "reference": (
                "Existing calibrated p_rise used as transparent joint-event "
                "probability proxy; not another fitted model."
            ),
            "policy_gate": (
                "Enable p_joint>=.5 gate on unchanged downside score only if "
                "screen equal-period binary Brier improves p_rise and screened "
                "matched selected downside DKK/pack does not worsen; else no "
                "persistence policy. No multiplication of correlated mean and "
                "event probability."
            ),
        },
        "calibration": {
            "maximum_temperature_fits": 28,
            "method": (
                "Bounded scalar0.5..3 using equal-period logloss, "
                "fitting/calibration/selection/outer periods disjoint. Direction "
                "fits hybrid and current separately; persistence binary per "
                "information. Label maturity strictly before cal_end. No "
                "parameter grid."
            ),
        },
        "population": {
            "version": "e5-population-v1",
            "min_history": 12,
            "lookback": 12,
            "min_premium_observations": 6,
            "median_premium_min": 0.20,
            "max_change_frequency": 1 / 12,
            "min_prior_flat_run": 6,
            "role": "package_distributor",
            "companies": ["BMS", "Astellas", "Amgen", "Evolan"],
            "policies": ["none", "named_package_distributor", "behaviour20", "behaviour50"],
            "role_sensitivities": ["drug_distributor", "authorisation_holder"],
            "selection": (
                "2025 only: behavior threshold20% or50%, retain>=95% E4 selected "
                "coverage and improve fall DKK/observed pack; tie no filter. "
                "Named companies hypothesis evaluated, not blanket presumed "
                "unsuitable. 2026 confirmation on common retained cases. No "
                "replacement of removed E4 selections."
            ),
            "training_exclusion": False,
            "peer_exclusion": False,
            "missingness": (
                "Retain unresolved dated mapping, missing premium or insufficient "
                "history with reason; no backfill from today's registry. Preserve "
                "full source and peer population."
            ),
        },
        "financial": {
            "policies": [
                "E4 probability threshold",
                "RMSE DKK opportunity",
                "RMSE minus negative q05 downside",
                "conditional supported persistence gate",
            ],
            "natural": (
                "E4 rise>=.35,fall<=.10,ceil10% original population cap; monetary "
                "score>0; empty list valid. E4 sort p_rise, other sort monetary "
                "score."
            ),
            "matched": (
                "Same per-origin E4 workload, rank eligible cases without outcome "
                "access; disclose inability to fill; unknown selections never "
                "replaced."
            ),
            "allocations": {
                "per_pack": 1,
                "fixed_quantity": 4,
                "capital_dkk": 100000,
                "package_cap_fraction": 0.10,
                "group_cap_fraction": 0.25,
                "rounding": "integer floor, unallocated cash explicit",
            },
            "scenarios": [
                "timely demand4",
                "timely demand8",
                "late receipt",
                "early expiry",
                "half compensation capped2",
                "return90% uncompensated acquisition basis",
            ],
            "engine": (
                "Reuse E4 Ledger; shared origin equal-size group demand, initial2 "
                "packs/group, fixed common capacity, identical receipt and demand "
                "assumptions. Unsupported group/unknown price trajectory retained "
                "unscored. Cost/cash/inventory/credits/returns separate; no sales "
                "margin or profit labels."
            ),
        },
        "matrix": matrix(),
        "reuse": (
            "All accepted R1 inputs/E2 equal peers/E3 broader+advance features, "
            "E4 January predictions/routes/calibrators, E4 actual Cito comparison "
            "and untouched287 future selections. No E1-E3 matrix, logistic, "
            "expanding-history or foundation rebuild."
        ),
        "resource_limits": {
            "base_fit_attempts": 48,
            "declared_slots": len(matrix()),
            "failure_reserve": 2,
            "threads": 6,
            "process_rss_bytes": 24 * 1024**3,
            "fit_seconds": 1500,
            "research_bytes": 8 * 1024**3,
            "recovery_bytes": 4 * 1024**3,
            "free_disk_reserve_bytes": 8 * 1024**3,
            "failure_handling": (
                "Sequential heavy fits; count attempt before fit, save failed "
                "receipt; never rerun completed. Two reserve attempts only for "
                "corrected implementation failures with prior receipt, no "
                "score-driven extra fits. Stop optional tuning before mandatory "
                "money/persistence/final reserve if constrained."
            ),
        },
        "machine": {
            "ram_bytes": psutil.virtual_memory().total,
            "available_ram_bytes": psutil.virtual_memory().available,
            "free_disk_bytes": shutil.disk_usage(root).free,
        },
        "selection_and_stopping": (
            "Freeze screen choices before outer scores. Complete all declared "
            "supported comparisons, retain negative results. E4 remains "
            "operational/frozen; selected E5 research is a separate immutable "
            "assessment. Final artifacts optional for rejected branches; no "
            "pointless fits."
        ),
        "explanations": (
            "At most256 deterministic cases for SHAP of selected "
            "direction/amount; ordinary feature importance also saved; "
            "contributions not causal proof."
        ),
        "sources": [
            "https://catboost.ai/docs/en/concepts/parameter-tuning",
            "https://catboost.ai/docs/en/concepts/python-reference_cv",
            "https://catboost.ai/docs/en/concepts/python-reference_catboost_fit",
            "https://catboost.ai/docs/en/concepts/loss-functions-regression",
            "https://nomeco.dk/apoteker",
        ],
    }
    frozen_json(root / "protocol.json", protocol)
    frozen_json(
        root / "freeze.json",
        {
            "protocol": pin(root / "protocol.json"),
            "frozen_utc": utc(),
            "comparative_scores_inspected": False,
        },
    )
    return protocol


def require(root: Path) -> dict[str, Any]:
    checked(read(root / "freeze.json")["protocol"])
    protocol = read(root / "protocol.json")
    if protocol["version"] != VERSION:
        raise ResearchError("e5_wrong_protocol")
    return protocol
