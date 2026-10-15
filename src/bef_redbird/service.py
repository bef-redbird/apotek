"""Pure analysis orchestration. No queue preference or UI state enters identity."""

import hashlib
import json
from dataclasses import asdict, replace
from decimal import Decimal
from uuid import uuid4

from bef_redbird.domain import (
    FEATURE_VERSION,
    PARSER_VERSION,
    RANKING_VERSION,
    AnalysisContext,
    AnalysisResult,
    AnalysisSettings,
    Candidate,
    CitoSnapshot,
    HistoryDataset,
    Issue,
    eligibility_policy,
    utc_now,
)
from bef_redbird.features import calculate_features

REASONS = (
    "Lav historisk prisposition og gentagne prisstigninger.",
    "Lav historisk prisposition; begrænset gentagelsesmønster.",
    "Opfylder grundfilter; intet stærkt historisk lavprissignal.",
)
FLAG_TEXT = {
    "missing_current_aip": "AIP mangler eller er nul i den valgte periode.",
    "cost_mismatch": "Cito-kostpris og periodens AIP stemmer ikke overens.",
    "short_window": "Det ønskede historikvindue er ikke tilgængeligt.",
    "few_prices": "For få gyldige priser i historikvinduet.",
    "unaligned_period": "Den valgte periode findes ikke i priskalenderen.",
    "history_conflict": "Modstridende historik kræver afklaring.",
}


def build_analysis(
    snapshot: CitoSnapshot,
    history: HistoryDataset,
    context: AnalysisContext,
    settings: AnalysisSettings | None = None,
) -> AnalysisResult:
    settings = settings or AnalysisSettings()
    versions = {"parser": PARSER_VERSION, "features": FEATURE_VERSION, "ranking": RANKING_VERSION}
    identity = {
        "schema": 1,
        "sources": [snapshot.source.sha256, history.source.sha256],
        "context": {**asdict(context), "current_period": context.current_period.isoformat()},
        "settings": asdict(settings),
        "versions": versions,
        "eligibility": eligibility_policy(),
    }
    analysis_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    items = list(snapshot.items.values())
    counts = {
        "Kilderækker": snapshot.input_rows,
        "Unikke seksfeltsrækker": snapshot.unique_required_rows,
        "Sammenlagte dubletter": snapshot.duplicate_rows,
        "Gyldige entydige produkter": len(items),
    }
    items = [v for v in items if v.storage_code not in ("H", "K")]
    counts["Efter H/K-fravalg"] = len(items)
    items = [v for v in items if v.current_code in ("A", "B") and v.upcoming_code == "C"]
    counts["Efter A/B → C"] = len(items)
    items = [v for v in items if v.current_cost_ore < 100000]
    counts["Efter kostpris < 1.000 kr."] = len(items)
    counts["Kandidatgrupper"] = len({v.group_id for v in items if v.group_id is not None})
    candidates: dict[str, Candidate] = {}
    for item in items:
        features = calculate_features(history, item.product_id, context.current_period, settings)
        flags = []
        if context.current_period not in history.periods:
            flags.append("unaligned_period")
        if item.product_id in history.conflicted_ids:
            flags.append("history_conflict")
        if features.current_aip_ore is None:
            flags.append("missing_current_aip")
        elif features.current_aip_ore != item.current_cost_ore:
            flags.append("cost_mismatch")
        if features.window_periods < settings.lookback_periods:
            flags.append("short_window")
        if features.n_valid < settings.minimum_valid_prices:
            flags.append("few_prices")
        tier = None
        if not flags:
            low_num, low_den = Decimal(settings.low_price_percentile).as_integer_ratio()
            low = (
                features.percentile_midrank_twice is not None
                and features.percentile_midrank_twice * low_den <= low_num * 2 * features.n_valid
                and features.median_distance_dkk is not None
                and features.median_distance_dkk > 0
            )
            tier = (0 if features.rise_count >= settings.repeated_rise_minimum else 1) if low else 2
        reason = " ".join(FLAG_TEXT[f] for f in flags) if flags else REASONS[tier or 0]
        candidates[item.product_id] = Candidate(item, features, None, tier, reason, tuple(flags))
    ordered = sorted(
        (c for c in candidates.values() if not c.flags),
        key=lambda c: (
            c.tier,
            -(c.features.median_distance_dkk or 0),
            c.features.percentile,
            c.item.product_id,
        ),
    )
    for rank, candidate in enumerate(ordered, 1):
        candidates[candidate.item.product_id] = replace(candidate, rank=rank)
    counts["Kan prioriteres"] = len(ordered)
    counts["Kan ikke prioriteres"] = len(items) - len(ordered)
    issues = [*snapshot.issues, *history.issues]
    if context.current_period not in history.periods:
        issues.append(
            Issue(
                "period_alignment",
                "Den bekræftede periode findes ikke i LMS-priskalenderen.",
                "unranked",
            )
        )
    if not history.periods or history.periods[-1] < context.current_period:
        issues.append(
            Issue("stale_history", "Historikken er ældre end den valgte Cito-periode.", "unranked")
        )
    if any("cost_mismatch" in c.flags for c in candidates.values()):
        issues.append(
            Issue(
                "reconciliation",
                "Afstemning kræver kontrol: undersøg rapportperiode og prisdefinition.",
            )
        )
    current_matches = sum(
        c.features.current_aip_ore == c.item.current_cost_ore for c in candidates.values()
    )
    older_matches = [
        (
            sum(
                history.prices.get(item.product_id, (None,) * len(history.periods))[index]
                == item.current_cost_ore
                for item in items
            ),
            period,
        )
        for index, period in enumerate(history.periods)
        if period < context.current_period
    ]
    if older_matches:
        match_count, matched_period = max(older_matches)
        if match_count > current_matches:
            issues.append(
                Issue(
                    "older_period_match",
                    f"Flere kostpriser matcher {matched_period:%d-%m-%Y} ({match_count}) end den "
                    f"bekræftede periode ({current_matches}). "
                    "Undersøg rapportdato og prisdefinition; "
                    "perioden ændres ikke automatisk.",
                )
            )
    return AnalysisResult(
        analysis_id,
        uuid4().hex,
        utc_now(),
        snapshot,
        history,
        context,
        settings,
        candidates,
        tuple(c.item.product_id for c in ordered),
        counts,
        issues,
        versions,
    )
