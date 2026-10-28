"""Fixed unknown-outcome and monetary-tail diagnostics from immutable predictions."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e4_common import checked, frozen_json, read, utc
from .group_features import _native_lines, pin
from .training_contract import ALLOWED_DISPENSING


def complete_details(root: Path, data: Path) -> Path:
    """Add fixed tail quantiles and native reasons; never recompute predictions."""
    output = root / "diagnostics-complete.json"
    if output.exists():
        checked(read(output)["unknown_rows"])
        return output
    prior = read(root / "diagnostics.json")
    frames = []
    for year in (2024, 2025, 2026):
        saved = read(root / f"evaluation-{year}/result.json")
        predictions = pd.read_parquet(
            checked(saved["artifacts"][0]),
            columns=[
                "row_key",
                "package_number",
                "period_start",
                "aip_ore",
                "history_periods",
                "peer_count",
                "abc",
                "advance_abc",
                "selected",
                "perfect_advance_original_interval_next_aip_dkk",
            ],
        )
        o = pd.read_parquet(
            checked(saved["outcomes"]),
            columns=[
                "row_key",
                "next_status",
                "next_aip_ore",
                "next_maturity_date",
                "next_direction",
            ],
        )
        q = pd.read_parquet(
            checked(saved["artifacts"][1]),
            columns=[
                "row_key",
                "perfect_e3_risk_rank",
                "perfect_e3_threshold",
                "perfect_routed_threshold",
            ],
        )
        frames.append(
            predictions.merge(o, on="row_key", validate="one_to_one").merge(
                q, on="row_key", validate="one_to_one"
            )
        )
    frame = pd.concat(frames, ignore_index=True)
    observed = frame.loc[frame.next_status.eq("observed")].copy()
    observed["error"] = (
        observed.perfect_advance_original_interval_next_aip_dkk - observed.next_aip_ore / 100
    ).abs()
    observed["price_band"] = pd.cut(
        observed.aip_ore / 100,
        [0, 100, 1000, np.inf],
        labels=["under100", "100to1000", "1000plus"],
        right=False,
    ).astype(str)
    observed["history_band"] = np.where(observed.history_periods.lt(6), "under6", "6plus")
    observed["peer_support"] = np.where(
        observed.peer_count.isna(),
        "unsupported",
        np.where(observed.peer_count.eq(0).fillna(False), "singleton", "positive"),
    )
    observed["abc_transition"] = (
        observed.abc.fillna("missing") + " to " + observed.advance_abc.fillna("missing")
    )
    for item in prior["money_strata"]:
        errors = (
            observed.error
            if item["dimension"] == "all"
            else observed.loc[observed[item["dimension"]].eq(item["stratum"]), "error"]
        )
        item.update(
            p95_absolute_error_dkk=float(errors.quantile(0.95)),
            p99_absolute_error_dkk=float(errors.quantile(0.99)),
            maximum_absolute_error_dkk=float(errors.max()),
        )
    selected = (
        frame.selected
        | frame.perfect_e3_risk_rank
        | frame.perfect_e3_threshold
        | frame.perfect_routed_threshold
    )
    unknown = frame.loc[selected & frame.next_status.ne("observed")].copy()
    receipts = {
        r["period"]: r
        for r in read(data / "research-e2/groups-v1/native-reconciliation.json")["receipts"]
    }
    details = []
    for maturity, rows in unknown.groupby("next_maturity_date", dropna=False):
        day = str(maturity)[:10]
        prices: dict[str, bytes] = {}
        packages: dict[str, bytes] = {}
        ref = None
        if day in receipts:
            sha = receipts[day]["original_sha256"]
            path = data / "sources/sha256" / sha[:2] / sha
            ref = pin(path)
            if ref["sha256"] != sha:
                raise ResearchError("e4_unknown_source_changed")
            with zipfile.ZipFile(path) as archive:
                names = {n.lower(): n for n in archive.namelist()}
                prices = {
                    line[:6].decode(): line
                    for line in _native_lines(archive.read(names["lms03.txt"]), 96)
                }
                packages = {
                    line[11:17].decode(): line
                    for line in _native_lines(archive.read(names["lms02.txt"]), 137)
                }
        for row in rows.to_dict("records"):
            package = row["package_number"]
            p, price = packages.get(package), prices.get(package)
            status = row["next_status"]
            reason, evidence = "other_evidenced_reason", status
            if status == "pending":
                reason, evidence = (
                    "pending_target_observation",
                    "Pending in pinned E3 evidence; later October check separate",
                )
            elif status in {"gap", "identity_change"}:
                reason, evidence = (
                    "identity_discontinuity_or_ambiguity",
                    "Absent target package" if p is None else "Accepted causal episode changed",
                )
            elif price is None or not price[6:15].isdigit() or int(price[6:15]) <= 0:
                reason, evidence = (
                    "unsupported_or_unresolved_price",
                    "Missing or nonpositive native AIP",
                )
            elif p is not None and p[119:127].strip() not in (b"", b"19000101", b"00000000"):
                evidence = "Recorded target exit: " + p[119:127].decode()
            elif p is not None and p[73:78].decode().strip() not in ALLOWED_DISPENSING:
                evidence = "Target dispensing scope excluded: " + p[73:78].decode().strip()
            elif p is None or p[103:111].strip() in (b"", b"19000101", b"00000000"):
                reason, evidence = (
                    "identity_discontinuity_or_ambiguity",
                    "Target creation/identity unresolved",
                )
            else:
                evidence = (
                    "Accepted target ineligibility; no narrower cause established "
                    "by fixed native fields"
                )
            details.append(
                {
                    "row_key": row["row_key"],
                    "package_number": package,
                    "period_start": str(row["period_start"])[:10],
                    "target": day,
                    "accepted_status": status,
                    "reason": reason,
                    "evidence": evidence,
                    "original_sha256": ref["sha256"] if ref else None,
                    **{
                        name: bool(row[name])
                        for name in (
                            "selected",
                            "perfect_e3_risk_rank",
                            "perfect_e3_threshold",
                            "perfect_routed_threshold",
                        )
                    },
                }
            )
    pd.DataFrame(details).to_parquet(root / "unknown-outcomes.parquet", index=False)
    detail = pd.DataFrame(details)
    finer = []
    for policy, field in (
        ("hybrid_threshold", "selected"),
        ("e3_risk_rank", "perfect_e3_risk_rank"),
        ("e3_threshold", "perfect_e3_threshold"),
        ("routed_threshold", "perfect_routed_threshold"),
    ):
        subset = detail.loc[detail[field]]
        population = frame.loc[frame[field]]
        per_period = population.groupby("period_start").next_status.apply(
            lambda s: s.ne("observed").mean()
        )
        finer.append(
            {
                "policy": policy,
                "selected": len(population),
                "unknown": len(subset),
                "reason_counts": subset.reason.value_counts().to_dict(),
                "evidence_counts": subset.evidence.value_counts().to_dict(),
                "period_mean_unknown_fraction": float(per_period.mean()),
                "pooled_unknown_fraction": len(subset) / len(population),
            }
        )
    later = read(root / "october-later-check.json")
    outcome = pd.read_parquet(checked(read(checked(later["outcome"]))["rows"]))
    october = frame.loc[pd.to_datetime(frame.period_start).eq(pd.Timestamp("2026-10-12"))].merge(
        outcome[["row_key", "status", "next_direction"]].rename(
            columns={"next_direction": "later_direction"}
        ),
        on="row_key",
        validate="one_to_one",
    )
    later_checks = []
    for policy, field in (
        ("hybrid_threshold", "selected"),
        ("e3_risk_rank", "perfect_e3_risk_rank"),
        ("e3_threshold", "perfect_e3_threshold"),
    ):
        part = october.loc[october[field]]
        known = part.loc[part.status.eq("observed")]
        later_checks.append(
            {
                "policy": policy,
                "originally_pending_selected": len(part),
                "new_states": part.status.value_counts().to_dict(),
                "observed_rise_rate": float(known.later_direction.eq(1).mean()),
                "observed_fall_rate": float(known.later_direction.eq(-1).mean()),
            }
        )
    frozen_json(
        output,
        {
            **prior,
            "version": "e4-fixed-diagnostics-complete-v1",
            "prior_diagnostic": pin(root / "diagnostics.json"),
            "created_utc": utc(),
            "unknown": finer,
            "unknown_rows": pin(root / "unknown-outcomes.parquet"),
            "later_october_checks": later_checks,
            "later_outcome": later["outcome"],
            "unchanged_original_e3": True,
        },
    )
    return output


def execute(root: Path, data: Path) -> Path:
    result = root / "diagnostics.json"
    if result.exists():
        checked(read(result)["cases"])
        return result
    frames = []
    for year in (2024, 2025, 2026):
        saved = read(root / f"evaluation-{year}/result.json")
        prediction = pd.read_parquet(
            checked(saved["artifacts"][0]),
            columns=[
                "row_key",
                "package_number",
                "period_start",
                "aip_ore",
                "history_periods",
                "peer_count",
                "abc",
                "advance_abc",
                "selected",
                "perfect_advance_original_interval_next_aip_dkk",
            ],
        )
        labels = pd.read_parquet(
            checked(saved["outcomes"]),
            columns=["row_key", "next_status", "next_aip_ore", "next_maturity_date"],
        )
        selection = pd.read_parquet(
            checked(saved["artifacts"][1]),
            columns=["row_key", "perfect_e3_risk_rank", "perfect_routed_threshold"],
        )
        frame = prediction.merge(labels, on="row_key", validate="one_to_one").merge(
            selection, on="row_key", validate="one_to_one"
        )
        frames.append(frame)
    all_rows = pd.concat(frames, ignore_index=True)
    reason_map = {
        "pending": "pending_target_observation",
        "ineligible": "unsupported_or_unresolved_target",
        "identity_change": "identity_discontinuity",
        "gap": "target_identifier_absent",
        "observed": "observed",
    }
    all_rows["reason"] = all_rows.next_status.map(reason_map).fillna(
        "other_evidenced_status:" + all_rows.next_status
    )
    unknown = []
    for policy, field in (
        ("hybrid_threshold", "selected"),
        ("e3_risk_rank", "perfect_e3_risk_rank"),
        ("routed_threshold", "perfect_routed_threshold"),
    ):
        selected = all_rows.loc[all_rows[field]]
        periods = selected.groupby("period_start").reason.apply(lambda s: s.ne("observed").mean())
        unknown.append(
            {
                "policy": policy,
                "selected": len(selected),
                "states": selected.reason.value_counts().to_dict(),
                "period_mean_unknown_fraction": float(periods.mean()),
                "pooled_unknown_fraction": float(selected.reason.ne("observed").mean()),
            }
        )
    observed = all_rows.loc[all_rows.next_status.eq("observed")].copy()
    observed["prediction_dkk"] = observed.perfect_advance_original_interval_next_aip_dkk
    observed["outcome_dkk"] = observed.next_aip_ore / 100
    observed["error_dkk"] = observed.prediction_dkk - observed.outcome_dkk
    observed["absolute_error_dkk"] = observed.error_dkk.abs()
    observed["squared_error_dkk"] = observed.error_dkk**2
    observed["price_band"] = pd.cut(
        observed.aip_ore / 100,
        [0, 100, 1000, np.inf],
        labels=["under100", "100to1000", "1000plus"],
        right=False,
    ).astype(str)
    observed["history_band"] = np.where(observed.history_periods.lt(6), "under6", "6plus")
    observed["peer_support"] = np.where(
        observed.peer_count.isna(),
        "unsupported",
        np.where(observed.peer_count.eq(0).fillna(False), "singleton", "positive"),
    )
    observed["abc_transition"] = (
        observed.abc.fillna("missing") + " to " + observed.advance_abc.fillna("missing")
    )

    def metric(part: pd.DataFrame) -> dict[str, Any]:
        periods = part.groupby("period_start").agg(
            mae=("absolute_error_dkk", "mean"),
            mse=("squared_error_dkk", "mean"),
            bias=("error_dkk", "mean"),
        )
        return {
            "observed": len(part),
            "periods": len(periods),
            "pooled_mae_dkk": float(part.absolute_error_dkk.mean()),
            "pooled_rmse_dkk": float(np.sqrt(part.squared_error_dkk.mean())),
            "pooled_bias_dkk": float(part.error_dkk.mean()),
            "period_mean_mae_dkk": float(periods.mae.mean()),
            "period_mean_rmse_dkk": float(np.sqrt(periods.mse).mean()),
            "period_mean_bias_dkk": float(periods.bias.mean()),
            "share_absolute_error": float(
                part.absolute_error_dkk.sum() / observed.absolute_error_dkk.sum()
            ),
            "share_squared_error": float(
                part.squared_error_dkk.sum() / observed.squared_error_dkk.sum()
            ),
        }

    strata = [{"dimension": "all", "stratum": "all", **metric(observed)}]
    for column in ("price_band", "history_band", "peer_support", "abc_transition"):
        strata.extend(
            {"dimension": column, "stratum": str(name), **metric(part)}
            for name, part in observed.groupby(column)
        )
    ranked = observed.sort_values("absolute_error_dkk", ascending=False)
    concentration = [
        {
            "top_rows": n,
            "share_absolute_error": float(
                ranked.head(n).absolute_error_dkk.sum() / observed.absolute_error_dkk.sum()
            ),
            "share_squared_error": float(
                ranked.head(n).squared_error_dkk.sum() / observed.squared_error_dkk.sum()
            ),
        }
        for n in (1, 10, 100, max(1, len(observed) // 100))
    ]
    receipts = {
        r["period"]: r
        for r in read(data / "research-e2/groups-v1/native-reconciliation.json")["receipts"]
    }
    traces = []
    for row in ranked.head(6).to_dict("records"):
        trace: dict[str, Any] = {
            "row_key": row["row_key"],
            "package_number": row["package_number"],
            "period_start": str(row["period_start"])[:10],
            "target": str(row["next_maturity_date"])[:10],
            "current_aip_dkk": row["aip_ore"] / 100,
            "forecast_dkk": row["prediction_dkk"],
            "actual_dkk": row["outcome_dkk"],
            "error_dkk": row["error_dkk"],
            "history_periods": row["history_periods"],
            "peer_count": None if pd.isna(row["peer_count"]) else row["peer_count"],
            "abc_transition": row["abc_transition"],
            "sources": [],
        }
        drugs = []
        for day, expected in (
            (trace["period_start"], int(row["aip_ore"])),
            (trace["target"], int(row["next_aip_ore"])),
        ):
            sha = receipts[day]["original_sha256"]
            path = data / "sources/sha256" / sha[:2] / sha
            ref = pin(path)
            if ref["sha256"] != sha:
                raise ResearchError("e4_tail_source_changed")
            with zipfile.ZipFile(path) as archive:
                names = {n.lower(): n for n in archive.namelist()}
                price = [
                    (i + 1, line)
                    for i, line in enumerate(_native_lines(archive.read(names["lms03.txt"]), 96))
                    if line[:6].decode() == row["package_number"]
                ]
                package = [
                    line
                    for line in _native_lines(archive.read(names["lms02.txt"]), 137)
                    if line[11:17].decode() == row["package_number"]
                ]
            if len(price) != 1 or len(package) != 1 or int(price[0][1][6:15]) != expected:
                raise ResearchError("e4_tail_native_price_discrepancy")
            drugs.append(package[0][:11].decode())
            trace["sources"].append(
                {
                    "period": day,
                    "original": ref,
                    "locator": "LMS03.TXT:record:" + str(price[0][0]),
                    "aip_token": price[0][1][6:15].decode(),
                    "drug_id": drugs[-1],
                    "creation_date_token": package[0][103:111].decode(),
                }
            )
        trace["finding"] = (
            (
                "Large official market move and forecast error; both native AIP tokens "
                "agree, same DrugID. No parser discrepancy found in this bounded trace."
            )
            if drugs[0] == drugs[1]
            else "Identity discrepancy requires exclusion review"
        )
        traces.append(trace)
    frozen_json(
        root / "tail-cases.json",
        {
            "cases": traces,
            "selection": (
                "Six largest absolute monetary errors; no outlier exclusion or label clipping"
            ),
        },
    )
    frozen_json(
        result,
        {
            "version": "e4-fixed-diagnostics-v1",
            "created_utc": utc(),
            "unknown": unknown,
            "money_strata": strata,
            "tail_concentration": concentration,
            "cases": pin(root / "tail-cases.json"),
            "interpretation": (
                "Retrospective error diagnostics; conditional median is not expected "
                "profit. Gap establishes absence in the selected target snapshot, not a "
                "clinical lifecycle conclusion."
            ),
        },
    )
    return result
