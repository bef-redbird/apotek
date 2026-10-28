"""Saved-only paired amount diagnostics on the fixed chronological origin grid."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .dataset import ResearchError
from .e4_common import checked, identity, read, utc
from .group_features import pin
from .training_contract import atomic_json

METRICS = ("mean_prediction", "mean_actual", "bias", "mae", "rmse")
MAGNITUDE = (
    "negative_large",
    "negative_small",
    "near_zero",
    "positive_small",
    "positive_medium",
    "positive_large",
)
DIRECTION = ("negative", "near_zero", "positive")


def bins(values: np.ndarray, kind: str) -> np.ndarray:
    if kind == "direction":
        return np.where(values < -1, "negative", np.where(values > 1, "positive", "near_zero"))
    return np.select(
        [values < -20, values < -1, values <= 1, values <= 20, values <= 100],
        MAGNITUDE[:-1],
        default="positive_large",
    )


def metric(predicted: np.ndarray, actual: np.ndarray) -> dict[str, float | None]:
    if not len(actual):
        return dict.fromkeys(METRICS)
    error = predicted - actual
    return {
        "mean_prediction": float(predicted.mean()),
        "mean_actual": float(actual.mean()),
        "bias": float(error.mean()),
        "mae": float(np.abs(error).mean()),
        "rmse": float(np.sqrt(np.square(error).mean())),
    }


def period_metrics(frame: pd.DataFrame, origin: str, mode: str) -> list[dict[str, Any]]:
    """Every fixed bin, including empties; common bins share 3y membership."""
    records = []
    eligible = frame.prediction_state.eq("available").to_numpy()
    observed = frame.status.eq("observed").to_numpy()
    actual = pd.to_numeric(frame.next_delta_ore).to_numpy(dtype=float) / 100
    for support in ("supported", "raw"):
        column = "delta_dkk" if support == "supported" else "raw_delta_dkk"
        forecasts = {
            history: pd.to_numeric(frame[f"amount_{history}_{column}"]).to_numpy(dtype=float)
            for history in ("3y", "6y", "full")
        }
        forecasts["no_change"] = np.zeros(len(frame))
        for population in ("all_eligible", "fixed_shortlist"):
            admitted = eligible & (
                frame.selected.to_numpy(dtype=bool) if population == "fixed_shortlist" else True
            )
            for history, forecast in forecasts.items():
                memberships = [("overall", "all", "all", admitted)]
                if history != "no_change":
                    for membership in ("own", "common_3y"):
                        reference = forecast if membership == "own" else forecasts["3y"]
                        for kind, names in (("direction", DIRECTION), ("magnitude", MAGNITUDE)):
                            labels = bins(reference, kind)
                            memberships += [
                                (membership, kind, label, admitted & (labels == label))
                                for label in names
                            ]
                for membership, kind, label, mask in memberships:
                    known = mask & observed
                    record = {
                        "origin": origin,
                        "information_mode": mode,
                        "support": support,
                        "population": population,
                        "history": history,
                        "membership": membership,
                        "bin_kind": kind,
                        "bin": label,
                        "candidates": int(mask.sum()),
                        "observed": int(known.sum()),
                        "unknown": int((mask & ~observed).sum()),
                        "all_origin_mean_prediction": float(forecast[mask].mean())
                        if mask.any()
                        else None,
                        "states": frame.loc[mask, "status"].value_counts().to_dict(),
                        **metric(forecast[known], actual[known]),
                    }
                    if history in ("6y", "full") and membership != "own":
                        baseline = forecasts["3y"][known]
                        error = forecast[known] - actual[known]
                        base_error = baseline - actual[known]
                        record["paired_mae_minus_3y"] = (
                            float((np.abs(error) - np.abs(base_error)).mean())
                            if known.any()
                            else None
                        )
                        record["paired_bias_minus_3y"] = (
                            float((error - base_error).mean()) if known.any() else None
                        )
                    records.append(record)
    return records


def block_interval(
    values: np.ndarray, block: int, draws: int = 2000, seed: int = 20261028
) -> dict[str, Any]:
    """Resample whole scheduled periods; unknown origins stay on the grid."""
    observed = np.isfinite(values)
    if observed.sum() < 4:
        return {
            "block": block,
            "lower": None,
            "upper": None,
            "scored_periods": int(observed.sum()),
            "grid_periods": len(values),
            "valid_draws": 0,
            "status": "insufficient_evidence",
        }
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(values), size=(draws, int(np.ceil(len(values) / block))))
    indices = ((starts[:, :, None] + np.arange(block)) % len(values)).reshape(draws, -1)
    samples = values[indices[:, : len(values)]]
    counts = np.isfinite(samples).sum(axis=1)
    means = np.nansum(samples, axis=1)[counts > 0] / counts[counts > 0]
    lower, upper = np.quantile(means, [0.025, 0.975])
    return {
        "block": block,
        "lower": float(lower),
        "upper": float(upper),
        "scored_periods": int(observed.sum()),
        "grid_periods": len(values),
        "valid_draws": len(means),
        "draws": draws,
        "seed": seed,
        "status": "descriptive_inconclusive" if observed.sum() < 4 else "descriptive",
    }


def report(root: Path) -> dict[str, Any]:
    from .prospective import now, require, status, verify, write_once, writer

    protocol = require(root)
    verification = verify(root)
    schedule = verification["schedule"]
    snapshots, rows, diagnostics, periods = [], [], [], []
    primary: dict[tuple[str, str], Path] = {}
    attempts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for reference in verification["assessments"]:
        saved_assessment = read(checked(reference))
        context = saved_assessment["context"]
        attempts.setdefault((context["origin"], context["information_mode"]), []).append(
            {
                "assessment": reference,
                "status": saved_assessment["status"],
                "primary": saved_assessment["primary"],
                "completed_utc": saved_assessment["completed_utc"],
                "invalid_reasons": saved_assessment["invalid_reasons"],
            }
        )
    for marker_path in root.glob("primary/*.json"):
        assessment_path = checked(read(marker_path)["assessment"])
        assessment = read(assessment_path)
        context = assessment["context"]
        primary[(context["origin"], context["information_mode"])] = assessment_path
    for scheduled in schedule["periods"] if schedule else []:
        for mode in ("current", "hybrid"):
            candidate_path = primary.get((scheduled["origin"], mode))
            if candidate_path is None:
                deadline_day = pd.Timestamp(scheduled["origin"]) + timedelta(days=9)
                deadline = deadline_day.replace(hour=12).tz_localize("Europe/Copenhagen")
                failed = attempts.get((scheduled["origin"], mode), [])
                freeze_state = "missed" if pd.Timestamp(now()) > deadline else "not_yet_due"
                state = (
                    sorted(failed, key=lambda item: item["completed_utc"])[0]["status"]
                    if failed
                    else "advance_unavailable"
                    if mode == "hybrid"
                    else freeze_state
                )
                periods.append(
                    {
                        **scheduled,
                        "information_mode": mode,
                        "state": state,
                        "freeze_state": freeze_state,
                        "attempts": failed,
                        "observed": 0,
                        "candidates": 0,
                        "unknown": 0,
                        "selected": 0,
                    }
                )
                continue
            assessment_path = candidate_path
            assessment = read(assessment_path)
            frame = pd.read_parquet(checked(assessment["predictions"]))
            outcomes = sorted(
                assessment_path.parent.glob("outcomes/*/outcome.json"),
                key=lambda path: read(path)["evaluated_utc"],
            )
            snapshot = {
                "assessment": pin(assessment_path),
                "predictions": assessment["predictions"],
            }
            if outcomes:
                outcome = read(outcomes[-1])
                snapshot.update({"outcome": pin(outcomes[-1]), "outcome_rows": outcome["rows"]})
                labels = pd.read_parquet(checked(outcome["rows"]))
                if not frame.row_key.equals(labels.row_key):
                    raise ResearchError("prospective_report_outcome_keys")
                frame["status"] = labels.status.to_numpy()
                frame["next_delta_ore"] = labels.next_delta_ore.to_numpy()
            else:
                frame["status"] = np.where(
                    frame.eligible, "pending_target_observation", "origin_ineligible"
                )
                frame["next_delta_ore"] = np.nan
            snapshots.append(snapshot)
            eligible = frame.prediction_state.eq("available")
            observed = eligible & frame.status.eq("observed")
            periods.append(
                {
                    **scheduled,
                    "information_mode": mode,
                    "state": assessment["status"],
                    "candidates": int(eligible.sum()),
                    "observed": int(observed.sum()),
                    "unknown": int((eligible & ~observed).sum()),
                    "selected": int(frame.selected.sum()),
                    "selected_observed": int((frame.selected & observed).sum()),
                    "states": frame.status.value_counts().to_dict(),
                }
            )
            rows.extend(period_metrics(frame, scheduled["origin"], mode))
            for history in ("3y", "6y", "full"):
                forecast = pd.to_numeric(frame[f"amount_{history}_delta_dkk"])
                actual = pd.to_numeric(frame.next_delta_ore) / 100
                error = forecast - actual
                for band, mask in (
                    ("rank_1_25", frame["rank"].le(25)),
                    ("rank_26_100", frame["rank"].gt(25) & frame["rank"].le(100)),
                    ("rank_over_100", frame["rank"].gt(100)),
                ):
                    known = eligible & observed & mask
                    diagnostics.append(
                        {
                            "origin": scheduled["origin"],
                            "information_mode": mode,
                            "history": history,
                            "population": "all_eligible",
                            "rank_band": band,
                            "candidates": int((eligible & mask).sum()),
                            "observed": int(known.sum()),
                            **metric(
                                forecast[known].to_numpy(dtype=float),
                                actual[known].to_numpy(dtype=float),
                            ),
                        }
                    )
                    selected_known = known & frame.selected
                    diagnostics.append(
                        {
                            "origin": scheduled["origin"],
                            "information_mode": mode,
                            "history": history,
                            "population": "fixed_shortlist",
                            "rank_band": band,
                            "candidates": int((eligible & mask & frame.selected).sum()),
                            "observed": int(selected_known.sum()),
                            **metric(
                                forecast[selected_known].to_numpy(dtype=float),
                                actual[selected_known].to_numpy(dtype=float),
                            ),
                        }
                    )
                for index in (
                    error.loc[observed]
                    .abs()
                    .sort_values(ascending=False, kind="stable")
                    .head(5)
                    .index
                ):
                    diagnostics.append(
                        {
                            "origin": scheduled["origin"],
                            "information_mode": mode,
                            "history": history,
                            "kind": "largest_absolute_residual",
                            "row_key": str(frame.loc[index, "row_key"]),
                            "rank": int(frame.loc[index, "rank"]),
                            "selected": bool(frame.loc[index, "selected"]),
                            "forecast_dkk": float(forecast.loc[index]),
                            "actual_dkk": float(actual.loc[index]),
                            "residual_dkk": float(error.loc[index]),
                        }
                    )
    summaries, temporal = [], []
    dimensions = [
        "information_mode",
        "support",
        "population",
        "history",
        "membership",
        "bin_kind",
        "bin",
    ]
    # Future periods are present in the grid, and are never substituted by later good periods.
    grid = [item["origin"] for item in schedule["periods"]] if schedule else []
    if rows:
        table = pd.DataFrame(rows)
        for key, group in table.groupby(dimensions, sort=True):
            result = dict(zip(dimensions, key, strict=True))
            result.update(
                {
                    "scheduled_periods": len(grid),
                    "frozen_periods": len(group),
                    "scored_periods": int(group.observed.gt(0).sum()),
                    "candidates": int(group.candidates.sum()),
                    "observed": int(group.observed.sum()),
                    "unknown": int(group.unknown.sum()),
                }
            )
            for name in (*METRICS, "paired_mae_minus_3y", "paired_bias_minus_3y"):
                if name in group:
                    finite = pd.to_numeric(group[name]).dropna()
                    result[name] = float(finite.mean()) if len(finite) else None
            result["coverage_condition_met"] = (
                result["frozen_periods"] == 12 and result["scored_periods"] == 12
            )
            summaries.append(result)
            # Intervals are released only at the six/twelve origin reporting milestone.
            elapsed = sum(
                pd.Timestamp(item["target"]).date()
                <= pd.Timestamp.now(tz="Europe/Copenhagen").date()
                for item in schedule["periods"]
            )
            count = 12 if elapsed >= 12 else 6 if elapsed >= 6 else 0
            if count:
                for name in ("bias", "mae", "paired_mae_minus_3y", "paired_bias_minus_3y"):
                    if name not in group:
                        continue
                    values = (
                        pd.to_numeric(group.set_index("origin")[name])
                        .reindex(grid[:count])
                        .to_numpy(dtype=float)
                    )
                    temporal.extend(
                        [
                            {
                                **dict(zip(dimensions, key, strict=True)),
                                "metric": name,
                                "milestone": count,
                                **block_interval(values, block),
                            }
                            for block in protocol["milestone"]["blocks"]
                        ]
                    )
                    if int(group.observed.sum()) < 100:
                        for entry in temporal[-len(protocol["milestone"]["blocks"]) :]:
                            entry.update(lower=None, upper=None, status="insufficient_evidence")
    binding = {
        "protocol": pin(root / "protocol.json"),
        "schedule": pin(root / "schedule.json") if schedule else None,
        "assessments": snapshots,
        "report_as_of_date": pd.Timestamp.now(tz="Europe/Copenhagen").date().isoformat(),
    }
    value = {
        "version": "prospective-report-v1",
        "binding": binding,
        "schedule": schedule,
        "assessments": snapshots,
        "periods": periods,
        "period_metrics": rows,
        "summaries": summaries,
        "temporal_blocks": temporal,
        "diagnostics": diagnostics,
        "freeze_attempts": [item for group in attempts.values() for item in group],
        "status": "awaiting_first_valid_freeze" if not schedule else "provisional_shadow_research",
        "limits": (
            "Equal-period means; supported primary/raw sensitivity; unchanged/falls/tails "
            "retained. No annual stability, promotion criterion or realised savings claim."
        ),
        "generated_utc": utc(),
    }
    destination = root / "reports" / identity({**binding, "periods": periods}) / "report.json"
    with writer(root):
        if destination.exists():
            value = read(destination)
        else:
            write_once(destination, value)
        atomic_json(root / "report.json", value)
        readiness = status(root)
        atomic_json(root / "status-report.json", readiness)

        def local(reference: dict[str, Any]) -> dict[str, Any]:
            path = checked(reference).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ResearchError("prospective_display_artifact_outside_operation")
            return {**reference, "path": path.relative_to(root.resolve()).as_posix()}

        forecasts = []
        for reference in verification["assessments"]:
            path = checked(reference)
            assessment = read(path)
            entry = {
                "assessment": local(reference),
                "predictions": local(assessment["predictions"]),
            }
            outcomes = sorted(
                path.parent.glob("outcomes/*/outcome.json"), key=lambda p: read(p)["evaluated_utc"]
            )
            if outcomes:
                outcome = read(outcomes[-1])
                entry.update(outcome=local(pin(outcomes[-1])), outcome_rows=local(outcome["rows"]))
            forecasts.append(entry)
        atomic_json(
            root / "index.json",
            {
                "version": "prospective-index-v1",
                "artifacts": {
                    name: local(pin(root / filename))
                    for name, filename in (
                        ("protocol", "protocol.json"),
                        ("readiness", "status-report.json"),
                        ("report", "report.json"),
                    )
                },
                "forecasts": forecasts,
            },
        )
    return value
