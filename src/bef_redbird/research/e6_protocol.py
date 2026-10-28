"""Finite, outcome-blind E6 research contract and accepted-input bindings."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

import psutil

from .dataset import ResearchError
from .e4_common import checked, frozen_json, read, utc
from .group_features import pin

FOLDS: list[dict[str, Any]] = [
    dict(
        name="spring",
        fit="2026-04-01",
        cal_start="2026-04-01",
        start="2026-06-01",
        end="2026-09-01",
        reference_year=2026,
    ),
    dict(
        name="summer",
        fit="2026-07-01",
        cal_start="2026-07-01",
        start="2026-09-01",
        end="2026-10-26",
        reference_year=2026,
    ),
    dict(
        name="earlier",
        fit="2025-01-01",
        cal_start="2025-04-01",
        start="2025-06-01",
        end="2026-01-01",
        reference_year=2025,
    ),
]


def freeze(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    if (root / "protocol.json").exists():
        return require(root)
    e5 = root.parent / "research-e5"
    prior = read(e5 / "protocol.json")
    accepted = read(e5 / "acceptance.json")
    for key in ("protocol.json", "index.json", "fit-ledger.json", "inputs/manifest.json"):
        checked(accepted["artifacts"][key])
    pins = dict(prior["pins"])
    for name in (
        "protocol.json",
        "index.json",
        "fit-ledger.json",
        "inputs/manifest.json",
        "selection.json",
        "results.json",
        "financial.json",
        "acceptance.json",
    ):
        pins["e5_" + name.replace("/", "_").replace(".json", "")] = pin(e5 / name)
    for name in ("calibration.json", "index.json", "lifecycle.json"):
        pins["e4_" + name.replace(".json", "")] = pin(root.parent / "research-e4" / name)
    for name in (
        "population/report.json",
        "evaluations/spring/result.json",
        "evaluations/summer/result.json",
    ):
        pins["e5_" + name.replace("/", "_").replace(".json", "")] = pin(e5 / name)
    for fold_name in ("screen", "spring", "summer"):
        for method in ("fresh", "mae", "rmse", "quantile", "huber", "tuned"):
            for mode in ("current", "advance"):
                receipt = e5 / "trials" / f"{fold_name}-{method}-{mode}" / "receipt.json"
                if receipt.exists():
                    pins[f"e5_trial_{fold_name}-{method}-{mode}"] = pin(receipt)
    pins["e2_protocol"] = pin(root.parent / "research-e2/run-v1/protocol.json")
    pins["e5_recovery"] = pin(root.parent / "recovery/research-e5-v1/recovery.json")
    matrix = []
    for fold in FOLDS[:2]:
        for mode in ("current", "advance"):
            matrix.append(
                dict(
                    id=f"{fold['name']}-huber-{mode}",
                    fold=fold["name"],
                    task="amount",
                    objective="huber",
                    history="3y",
                    mode=mode,
                    investigation="C",
                    optional=False,
                )
            )
    for fold in FOLDS:
        for history in ("6y", "full"):
            for task in ("direction", "amount"):
                for mode in ("current", "advance"):
                    matrix.append(
                        dict(
                            id=f"{fold['name']}-{task}-{history}-{mode}",
                            fold=fold["name"],
                            task=task,
                            objective="multiclass" if task == "direction" else "rmse",
                            history=history,
                            mode=mode,
                            investigation="D",
                            optional=False,
                        )
                    )
    for mode in ("current", "advance"):
        matrix.append(
            dict(
                id=f"summer-direction-weighted-{mode}",
                fold="summer",
                task="direction",
                objective="multiclass",
                history="weighted",
                mode=mode,
                investigation="D",
                optional=True,
            )
        )
    protocol = {
        "version": "e6-bounded-v1",
        "frozen_utc": utc(),
        "pins": pins,
        "folds": FOLDS,
        "matrix": matrix,
        "settings": prior["settings"],
        "objectives": {"huber": "Huber:delta=10", "rmse": "RMSE", "multiclass": "MultiClass"},
        "information": "Current-only feature allowlist excludes every advance field. Conditional "
        "advance permits only separately named immediate-next ABC; missing codes route to genuine "
        "current model. No unpublished prices, future membership/identity/survival or T+2 ABC.",
        "data": "Reuse accepted R1 exact keys, integer-ore prices/labels, E2 equal peers, "
        "E3 broader "
        "peers and advance sidecars through pins. LEFT joins preserve all origin candidates. "
        "No operating database access or new source ingestion. Historical feature "
        "lookback unchanged.",
        "availability": "Reconstructed retrospective history. Advance sidecar begins 2019; earlier "
        "advance is missing, not a cohort filter. Supported old current/peer features remain. "
        "Historical final-code proxy does not prove early receipt; source definition gaps remain.",
        "exposure": "All periods are exposed retrospective research. The 2025 diagnostic applies "
        "later selected E5 methods retrospectively, with every fitted/calibrated component obeying "
        "its own cutoff. Neither 2025 nor 2026 is untouched or independent confirmation.",
        "chronology": "Whole tariff periods; observed training labels mature strictly before fit. "
        "Fixed 160 iterations, no early stopping or E6 tuning. Calibration uses only cal_start <= "
        "origin < evaluation start and labels strictly mature before evaluation start. Evaluation "
        "labels attach after prediction. 2025 package/equal fallback uses January 2025 models.",
        "A": {
            "hypothesis": "Monetary ranking adds value when no upcoming ABC exists anywhere.",
            "predictions": "E5 spring/summer tuned current routed calibrated probabilities; exact "
            "current RMSE amount and q05 lower price predictions. No hybrid output or advance "
            "ranking field. Same E5 probability control essential; E4 current-only policy "
            "separate.",
            "population": "Same feature-eligible population and E5 lagged behaviour50 "
            "recommendation "
            "mask for all main policies. Separate original unfiltered E4 reference coverage.",
            "queues": "Unchanged >=.35 rise, <=.10 fall, positive score, ceil10% population cap. "
            "Probability score=p_rise; DKK=RMSE supported delta; downside=delta-max(0,-q05 delta). "
            "Matched counts fixed per period to E5 probability natural count, seeded SHA256 ties "
            "20261026:row_key. Monetary expansion may admit naturally rejected rows: "
            "diagnostic only.",
            "allocation": prior["financial"]["allocations"],
            "unknowns": "Retain selected unknown without replacement; no zero "
            "price-change imputation.",
            "decision": "Research replacement requires >=5% equal-period net "
            "movement/observed pack "
            "gain at matched workload, no aggregate downside/pack or unknown-rate increase, and no "
            "fixed-capital net exposure loss in either fold. Otherwise retain probability "
            "reference. "
            "Report concentration and integer pack quantities; no new demand scenarios or profit.",
        },
        "B": {
            "hypothesis": "The actual 27 comparable Cito errors changed predictions or "
            "recommendations.",
            "source": "Accepted original NyeSubgrupperABC_update_43_26.xls; owner receipt "
            "2026-10-17 around 11:00 Europe/Copenhagen; current Oct12, target Oct26. No "
            "file-time inference.",
            "arms": [
                "actual report with original E4 hybrid calibration",
                "only comparable incorrect codes corrected with same calibration",
                "no report genuine current-only with E4 current calibration",
                "no report hybrid-temperature isolation diagnostic",
            ],
            "correction": "Change only accepted incorrect comparable advance codes to reconciled "
            "values. Preserve coverage, identity exclusions, missing/unusable states, "
            "prices, peers, "
            "company and labels. Recompute only contract-dependent derived advance "
            "features if any.",
            "models": "Original E4 January2026 components and earlier-mature calibration; "
            "never E5.",
            "population": "Fixed all eligible October12 origins and unchanged accepted outcomes. "
            "Natural coverage separate; matched actual-report workload diagnostic with "
            "deterministic ties.",
            "reporting": "Prediction changes, added/removed, rank shift >=20 percentile points; "
            "rise/flat/fall/unknown, one-pack net and downside, substitution group "
            "clusters. No row bootstrap.",
            "examples": "Deterministic case selection across unchanged-selection harmless errors, "
            "threshold crossings and largest probability changes, with group context. "
            "Show each category "
            "when present; no policy tuning. One snapshot cannot establish general error "
            "rate/value.",
            "decision": "Any membership change is operationally relevant; material "
            "aggregate consequence "
            "requires >=5% change in selected one-pack net or downside, or >=5% "
            "membership turnover. "
            "Report absolute values as well; no operational promotion from this one snapshot.",
        },
        "C": {
            "hypothesis": "Exact screened Huber improves outer amount errors beyond development.",
            "recipe": "Huber:delta=10, next_delta_ore/100 DKK, 160/depth6/lr.08/L2=5, equal-peer "
            "features and native categorical preprocessing, seed20261026, equal-period "
            "training weights.",
            "reuse": "Exact E5 spring/summer three-year MAE and RMSE fits. Four new Huber "
            "fits only.",
            "metrics": "Equal-period MAE primary, RMSE, signed bias, predicted/observed "
            "totals, top1% "
            "absolute/squared-error concentration, price bands <100/100-1000/>=1000 DKK, "
            "history<6, "
            "positive/zero/unknown peers. Raw and max(0,current+delta) supported prices "
            "both retained.",
            "decision": "Further research merit requires >=1% equal-period MAE gain against MAE "
            "reference and RMSE no worse in both information modes; report fold "
            "consistency and bias. "
            "No Huber ranking experiment or interval refit. Robust location is not "
            "expected returns.",
        },
        "D": {
            "hypothesis": "Six years/all usable history or recency weights improve recent "
            "predictions.",
            "history": ["trailing three years", "trailing six years", "all usable from 2008"],
            "recipe": "Unchanged E5 fresh 160/depth6/lr.08/L2=5 broad direction and "
            "equal-peer RMSE "
            "amount in both modes. Not the E5 tuned480 recipe. Other routed direction "
            "branches fixed "
            "at the corresponding year's January components. Raw component and complete routed "
            "output scored separately, each with earlier temperature calibration; conditional mode "
            "also reports genuine current-only fallback.",
            "reuse": "Three-year spring/summer E5 fresh/RMSE; earlier2025 E3 broad160 direction "
            "and E5 screen RMSE only after exact cohort/recipe/keys verification. No "
            "refit compatible controls.",
            "weighting": "Optional summer full-history direction pair: equal-period weights times "
            "2**(-age_days/(3*365.25)), age measured fitting cutoff minus origin. "
            "Normalize mean weight "
            "to one. Report effective sample size and weighted age shares. Evaluation "
            "weights unchanged.",
            "metrics": "Equal-period raw/calibrated multiclass Brier primary; logloss/calibration, "
            "candidate rates, rise/fall/unknown/downside, per-period/fold differences, "
            "short-history and "
            "weak peers. RMSE amount MAE/RMSE/bias/tails. Actual rows/dates/time/RSS/model bytes.",
            "decision": "Research direction preference needs >=.0005 equal-period "
            "calibrated routed "
            "Brier gain in both information modes, no fold deterioration >.001, no "
            "adverse calibration "
            "or selected downside increase >5%, coverage unchanged. Amount preference needs >=1% "
            "MAE or RMSE improvement without >1% worsening in the other or larger absolute bias. "
            "Mixed findings retain reference; best under this recipe is not universally "
            "optimal history.",
        },
        "uncertainty": "Equal-period primary and pooled diagnostics; paired circular tariff-period "
        "block bootstrap 2000 repetitions seed20261027, lengths 2 and 4 for ten-period2026 and 4/8 "
        "for 2025 where supported. Exclude wholly pending periods from scored means, "
        "retain coverage. "
        "No row-level independence claim; one Cito report has no sampling precision interval.",
        "resource_limits": {
            "threads": 6,
            "process_rss_bytes": 28 * 1024**3,
            "full_fit_seconds": 7200,
            "other_fit_seconds": 3600,
            "aggregate_fit_seconds": 16 * 3600,
            "declared_slots": 30,
            "base_fit_attempts": 32,
            "technical_retry_reserve": 2,
            "free_disk_reserve_bytes": 12 * 1024**3,
            "research_bytes": 12 * 1024**3,
            "recovery_bytes": 8 * 1024**3,
        },
        "stopping": "Run four Huber then primary2026 history, earlier2025 diagnostic, "
        "optional weighted "
        "last. One heavy process. Reuse completed receipts, preserve failures. Optional "
        "pair only if "
        "remaining aggregate budget >=2 times max observed full direction elapsed plus 30min. "
        "Technical retry only for corrected implementation failure, same scientific "
        "recipe, at most2; "
        "no score-driven retries or recipe changes. Infeasible arms remain unanswered, "
        "never losers.",
        "machine": {
            "total_ram_bytes": psutil.virtual_memory().total,
            "available_ram_bytes": psutil.virtual_memory().available,
            "logical_cpus": psutil.cpu_count(),
            "free_disk_bytes": shutil.disk_usage(root).free,
        },
        "preservation": "No latest-period fit/November replacement, no accepted artifact mutation, "
        "no model/policy promotion, scheduler, order, deployment, commit, push, upload or "
        "external AI. "
        "Keep editable meeting checklist. New results/recovery only under one E6 private "
        "root/capsule.",
    }
    frozen_json(root / "protocol.json", protocol)
    frozen_json(
        root / "freeze.json",
        {
            "protocol": pin(root / "protocol.json"),
            "created_utc": utc(),
            "comparative_e6_scores_inspected": False,
            "probe_fits": 0,
            "producer": pin(Path(__file__)),
        },
    )
    return protocol


def require(root: Path) -> dict[str, Any]:
    receipt = read(root / "freeze.json")
    path = checked(receipt["protocol"])
    if path.resolve() != (root / "protocol.json").resolve():
        raise ResearchError("e6_protocol_path_changed")
    return read(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    print(freeze(parser.parse_args().root.resolve())["version"])
