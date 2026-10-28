"""Small immutable-file contracts shared by E4 producers and saved-only readers."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .dataset import ResearchError
from .e2_data import verify_pin
from .group_features import pin
from .training_contract import atomic_json


def utc() -> str:
    return datetime.now(UTC).isoformat()


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def identity(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def frozen_json(path: Path, value: Any) -> None:
    def scalar(item: Any) -> Any:
        import numpy as np

        if isinstance(item, np.generic):
            return item.item()
        raise TypeError(f"Unsupported immutable JSON value: {type(item).__name__}")

    value = json.loads(json.dumps(value, default=scalar, allow_nan=False))
    if path.exists():
        if read(path) != value:
            raise ResearchError("e4_immutable_artifact_conflict")
        return
    atomic_json(path, value)


def checked(reference: dict[str, Any]) -> Path:
    path = Path(reference["path"])
    verify_pin(path, reference)
    return path


def require_protocol(root: Path) -> dict[str, Any]:
    receipt = read(root / "freeze.json")
    verify_pin(root / "protocol.json", receipt["protocol"])
    return read(root / "protocol.json")


def freeze(root: Path, data: Path) -> Path:
    """Freeze methods before new comparison results; accepted history remains exposed."""
    path = root / "protocol.json"
    if path.exists():
        require_protocol(root)
        return path
    e3 = data / "research-e3"
    current = read(e3 / "current.json")
    for role in ("acceptance", "protocol", "display_result", "run_index", "owner_report"):
        checked(current[role])
    import psutil

    machine = {
        "physical_ram_bytes": psutil.virtual_memory().total,
        "available_ram_bytes": psutil.virtual_memory().available,
        "free_volume_bytes": psutil.disk_usage(str(root.resolve().anchor)).free,
    }
    protocol = {
        "version": "e4-bounded-v1",
        "frozen_utc": utc(),
        "e3_root": str((e3 / "run-v1").resolve()),
        "accepted_e3": {
            k: current[k]
            for k in ("acceptance", "protocol", "display_result", "run_index", "owner_report")
        },
        "exposure": "All E2/E3 evaluation history exposed; retrospective research only",
        "population": (
            "Exact E3 origin candidates, 2024-01-08 through 2026-10-12; preserve unknowns"
        ),
        "information": (
            "Origin-only attributes/groups/prices; separate T+1 ABC only; no T+1 AIP or"
            " T+2 ABC in predictors"
        ),
        "training": (
            "Reuse accepted three-year models at 2024/2025/2026 annual cutoffs; no new base fits"
        ),
        "routing": "Origin broader supported count>=1, else equal count>=1, else package",
        "policies": {
            "risk": {"lambda": 0.5, "maximum_fraction": 0.10},
            "threshold": {"rise_min": 0.35, "fall_max": 0.10, "maximum_fraction": 0.10},
            "selection": "Reuse E3 fixed parameters; no E4 threshold search",
            "references": [
                "E2 current equal-peer rise ranking",
                "E3 calibrated equal-peer risk and threshold",
                "same-information routed rise ranking",
            ],
            "workloads": (
                "Natural lists plus exact per-origin E4 threshold count for all "
                "rankings and risk-decile count; unknowns never replaced"
            ),
        },
        "calibration": {
            "method": (
                "E3 bounded scalar temperature, equal-period log loss; separate routed,"
                " hybrid and current-only distributions"
            ),
            "source_years": {"2024": 2023, "2025": 2024, "2026": 2025},
            "maturity": (
                "Observed label maturity strictly before annual cutoff; earlier held-"
                "out origins only"
            ),
            "maximum_temperature_fits": 9,
            "scenario_use": (
                "Fit on unperturbed earlier predictions; reuse state for fixed stress "
                "cases; no calibration on evaluation outcomes"
            ),
        },
        "missing": {
            "route": (
                "Usable decision-time advance code uses advance route; "
                "missing/invalid/conflicting/unmatched code uses genuine current-only "
                "route"
            ),
            "comparison": (
                "Compare with advance-trained missing-code state on identical "
                "candidates, including naturally missing"
            ),
            "scenarios": ["perfect", "missing25", "wrong10", "group_wrong20", "missing100"],
            "perturbation": (
                "Reuse E3 deterministic perturbations and saved predictions; synthetic "
                "rates are not measured Cito error"
            ),
            "money": (
                "Equal-peer current model for missing code versus equal-peer advance "
                "missing state; no monetary peer routing"
            ),
        },
        "uncertainty": {
            "point": "Unchanged accepted equal-peer constrained-delta conditional median",
            "one_improvement": (
                "q90 absolute earlier held-out residual by fixed current price "
                "(<100,100-1000,>=1000 DKK) x history (<6,>=6) x equal-peer "
                "(positive,zero,unsupported)"
            ),
            "minimum_support": 200,
            "fallback": (
                "Price-only stratum with >=200, otherwise pooled; higher empirical "
                "quantile; lower bound max(0,median-radius)"
            ),
            "maximum_residual_calibrations": 6,
            "report": (
                "Overall and fixed conditional empirical coverage and width, no nominal guarantee"
            ),
        },
        "metrics": [
            "equal-period and pooled Brier",
            "observed selected rise/fall rates",
            "unknown exposure and all-selected lower/upper bounds",
            "natural and matched queue counts",
            "MAE/bias/p95/p99/max",
            "interval coverage and width",
        ],
        "paired_uncertainty": {
            "method": "circular temporal blocks",
            "blocks": [4, 8],
            "draws": 2000,
            "seed": 20261027,
            "claim": "descriptive unadjusted retrospective intervals",
        },
        "diagnostics": {
            "unknowns": (
                "Pending, unresolved/unsupported prices, identity episode "
                "discontinuity/ambiguity and other evidenced reasons; new outcome "
                "versions never rewrite E3"
            ),
            "tails": (
                "Fixed current-price/history/peer/ABC-transition strata; largest 1% "
                "contribution and six largest errors traced to original native fields "
                "and prediction inputs; no clipping or deletion"
            ),
        },
        "cito": {
            "origin": "2026-10-12",
            "target": "2026-10-26",
            "reported_receipt": (
                "2026-10-17 approximately 11:00 Europe/Copenhagen, owner report; "
                "precision approximate, no invented bounds"
            ),
            "claims": (
                "Single snapshot reliability only; cost field not official AIP; "
                "independently selected exact final tariff-start classifications"
            ),
            "denominators": (
                "Raw rows, unique identifiers, duplicates/conflicts, official "
                "match/missing/incorrect separately; origin coverage, selected "
                "candidates, groups and transition confusion"
            ),
        },
        "scenario": {
            "scope": (
                "Hypothetical modern-terms-inspired assumptions on historical AIP; no "
                "pharmacy savings/profit claim"
            ),
            "origin_window": (
                "2024-01-08 through 2026-10-12; two subsequent tariff observations; "
                "unknown trajectories retained and unscored"
            ),
            "population": (
                "At most 50 origin supported equal-size LMS04 groups per origin, stable"
                " SHA256 sample, >=2 origin members, no future survival selection"
            ),
            "policies": [
                "ordinary replenishment",
                "A/B to C deterministic extra purchase",
                "hybrid routed threshold extra purchase",
            ],
            "shared_demand": (
                "One group demand, perfect substitution of admitted identical-size "
                "packages is an explicit hypothetical allocation; FIFO oldest stock "
                "first, then origin-price/package tie"
            ),
            "initial_stock": (
                "Two packs per group of origin lowest-AIP member; no outstanding initial orders"
            ),
            "matrix": (
                "Demand 4 or 8 packs/period x max extra stock 4 or 12 packs x "
                "compensation none/half capped at2 packs/full eligible =12; plus late "
                "speculative receipt (target price), early expiry (end first target), "
                "and explicit return variants at demand4/cap12/no compensation =3"
            ),
            "ordering": (
                "Ordinary: replenish each period to expected demand, arrive at period "
                "start; speculative ordered before target, normally arrives before "
                "target at origin AIP; late variant arrives after boundary at target "
                "AIP"
            ),
            "demand_timing": (
                "Two periods; demand distributed at midpoint; second-period price can "
                "reverse first rise; equal service assumptions across policies"
            ),
            "compensation": (
                "At each boundary, eligible on-hand packs only, per-package cap each "
                "event, price fall times eligible quantity times rate; cash credited "
                "once and book cost reduced once"
            ),
            "capital": (
                "5% annual simple capital cost, actual assumed days, average time-"
                "weighted book stock; report cash, book/market inventory, procurement "
                "cost and service separately"
            ),
            "returns": (
                "Disabled except explicit variant: unsold terminal stock returned at "
                "original acquisition cost less prior stock compensation and 10% fee, "
                "capped below at zero; no automatic rebuy"
            ),
            "expiry": (
                "No expiry base; explicit early-expiry variant writes off all remaining"
                " target-opening lots at end first target before second replenishment"
            ),
            "unknowns": (
                "No speculative substitute/replacement for unknown price paths; exclude"
                " only financial scoring with full origin-selected denominator retained"
            ),
            "interface": (
                "Typed stock/order/receipt/demand/credit/return events; future pharmacy"
                " exports can supply these, no ERP or multi-tenant integration"
            ),
        },
        "resource_limits": {
            "base_fits": 0,
            "contingency_fit_maximum": 8,
            "contingency_requires_prior_written_need": True,
            "threads": 6,
            "process_rss_bytes": 12 * 1024**3,
            "evaluation_seconds": 7200,
            "incremental_research_bytes": 4 * 1024**3,
            "recovery_bytes": 4 * 1024**3,
            "free_disk_reserve_bytes": 8 * 1024**3,
            "enforcement": (
                "Sampled RSS every100ms plus stage checkpoints; sequential heavy tasks;"
                " resumable successful stages; failures retained, no silent ceiling "
                "change"
            ),
        },
        "machine_at_freeze": machine,
        "stopping": (
            "Complete finite comparisons including negative results; future "
            "observations and pharmacy inputs remain external dependencies; scheduling "
            "disabled/uninstalled"
        ),
    }
    frozen_json(path, protocol)
    frozen_json(
        root / "freeze.json",
        {"protocol": pin(path), "new_comparative_results_inspected": False, "created_utc": utc()},
    )
    return path
