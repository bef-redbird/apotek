"""Explicit, local E4 commands. No scheduler, automatic fitting or promotion."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

from .e4_common import checked, frozen_json, read, utc
from .group_features import pin


def check_prediction_cutoffs(context: dict[str, Any], bundle: Path, calibration: Path) -> None:
    """Public command boundary for all fitted direction and monetary uncertainty states."""
    from .dataset import ResearchError

    cutoff = min(context["origin"], str(pd.Timestamp(context["decision_cutoff"]).date()))
    saved = read(bundle)
    states = list(read(calibration)["states"]["2026"]["temperature"].values())
    intervals = read(checked(saved["amount_interval_calibration"]))["residual"]
    states.extend(intervals[key] for key in ("2026_current", "2026_advance"))
    for state in states:
        if state["cutoff"] > cutoff or state["last_maturity"] >= state["cutoff"]:
            raise ResearchError("e4_prediction_calibration_not_mature_at_cutoff")


def check_feature_dependencies(root: Path, data: Path) -> Path:
    """Compact immutable input catalogue for one-period replay/extension and resume."""
    from .e4_common import require_protocol

    receipt = root / "features/input-catalogue.json"
    if receipt.exists():
        for item in read(receipt)["pins"]:
            checked(item)
        return receipt
    accepted = require_protocol(root)
    e3 = read(checked(accepted["accepted_e3"]["protocol"]))
    refs = [e3["pins"][role] for role in ("dataset", "groups", "broader")]
    dataset_path = checked(e3["pins"]["dataset"])
    for item in read(dataset_path)["files"]:
        if item["role"] in {"features", "source", "calendar", "exclusions"}:
            refs.append({**item, "path": str(dataset_path.parent / item["path"])})
    for role in ("groups", "broader"):
        manifest = checked(e3["pins"][role])
        for item in read(manifest)["files"]:
            path = Path(item["path"])
            refs.append(
                {**item, "path": str(path if path.is_absolute() else manifest.parent / path)}
            )
    feature = read(root / "features/manifest.json")
    refs.extend(feature["source_snapshots"])
    refs.extend(feature["artifacts"])
    for item in refs:
        checked(item)
    frozen_json(
        receipt,
        {
            "version": "e4-feature-inputs-v1",
            "pins": refs,
            "existing_replay": pin(root / "features/manifest.json"),
            "scope": (
                "Hash verification of necessary accepted inputs only; no repeat "
                "native-source audit or foundation rebuild"
            ),
        },
    )
    return receipt


def initialize_assessments(root: Path, e3: Path) -> Path:
    from .e4_assessment import (
        attach_outcome,
        bind_report,
        compare_assessments,
        model_bundle,
        prepare,
    )
    from .e4_sources import compare_report

    manifest = root / "lifecycle.json"
    if manifest.exists():
        for item in read(manifest)["assessments"]:
            checked(item["assessment"])
            checked(item["outcome"])
        return manifest
    bundle = model_bundle(e3, root / "model-bundle.json")
    calibration = root / "calibration.json"
    report = next((root / "reports").glob("*/report.json"))
    snapshots = {day: root / f"sources/{day}/snapshot.json" for day in ("2026-10-12", "2026-10-26")}
    assessments: list[dict[str, Any]] = []
    retry = []
    for origin, target, mode in (
        ("2026-10-12", "2026-10-26", "actual_report_retrospective"),
        ("2026-10-26", "2026-11-09", "prospective_pending"),
    ):
        input_path = root / "features" / (origin + "-assessment.parquet")
        context_path = root / "features" / (origin + "-context.json")
        if not context_path.exists():
            frame = pd.read_parquet(root / "features" / (origin + ".parquet"))
            official = pd.read_parquet(checked(read(snapshots[origin])["rows"]))
            identity = official[["package_number", "created_date_token", "exit_date_token"]].copy()
            identity["origin_creation_date"] = pd.to_datetime(
                identity.created_date_token, format="%Y%m%d", errors="coerce"
            ).dt.strftime("%Y-%m-%d")
            identity["origin_exit_date"] = pd.to_datetime(
                identity.exit_date_token, format="%Y%m%d", errors="coerce"
            ).dt.strftime("%Y-%m-%d")
            frame = frame.merge(
                identity[["package_number", "origin_creation_date", "origin_exit_date"]],
                on="package_number",
                validate="one_to_one",
                how="left",
            )
            if mode == "actual_report_retrospective":
                frame = bind_report(frame, report, origin, target)
            frame.to_parquet(input_path, index=False)
            sources = [pin(root / "features/manifest.json"), pin(snapshots[origin])]
            if mode == "actual_report_retrospective":
                sources.append(pin(report))
            now = utc()
            context: dict[str, Any] = {
                "origin": origin,
                "target": target,
                "calendar_next_start": target,
                "decision_cutoff": "2026-10-21T12:00:00+02:00"
                if mode == "actual_report_retrospective"
                else now,
                "mode": mode,
                "sources": sources,
                "target_outcome_exposed": mode != "prospective_pending",
                "availability": (
                    "Actual report receipt owner-reported; historical official possession "
                    "not established"
                )
                if mode != "prospective_pending"
                else "Current-only official snapshot actually acquired; no next ABC or AIP used",
                "availability_evidence": [
                    {
                        "artifact": reference,
                        "available_utc": now,
                        "basis": (
                            "Pinned local artifact existed before this assessment cutoff; "
                            "source acquisition remains separately recorded"
                        ),
                    }
                    for reference in [pin(input_path), pin(bundle), pin(calibration), *sources]
                ],
            }
            frozen_json(context_path, context)
        context = read(context_path)
        check_prediction_cutoffs(context, bundle, calibration)
        assessment = prepare(root, input_path, context, bundle, calibration)
        pending = attach_outcome(assessment, None)
        outcome = attach_outcome(assessment, snapshots[target]) if target in snapshots else pending
        same_assessment = prepare(root, input_path, context, bundle, calibration)
        same_outcome = (
            attach_outcome(assessment, snapshots[target])
            if target in snapshots
            else attach_outcome(assessment, None)
        )
        retry.append(
            {
                "origin": origin,
                "assessment_idempotent": same_assessment == assessment,
                "outcome_idempotent": same_outcome == outcome,
                "pending_check": pin(pending),
                "outcome_versions": len(
                    list((assessment.parent / "outcomes").glob("*/outcome.json"))
                ),
                "inputs_and_predictions": [
                    pin(checked(read(assessment)[name])) for name in ("inputs", "predictions")
                ],
            }
        )
        assessments.append(
            {
                "label": "12 October · actual Cito report · retrospective"
                if mode != "prospective_pending"
                else "26 October · current-only · prospective pending",
                "mode": mode,
                "origin": origin,
                "target": target,
                "assessment": pin(assessment),
                "outcome": pin(outcome),
            }
        )
    actual = read(checked(assessments[0]["assessment"]))
    candidates = pd.read_parquet(checked(actual["predictions"]))
    compare_report(
        report,
        pd.read_parquet(checked(read(snapshots["2026-10-12"])["rows"])),
        pd.read_parquet(checked(read(snapshots["2026-10-26"])["rows"])),
        candidates.loc[candidates.eligible],
        root / "cito-comparison",
        evidence=[pin(p) for p in snapshots.values()] + [assessments[0]["assessment"]],
    )
    # A later check for E3's originally pending tail is separate from accepted E3 labels.
    e3_saved = pd.read_parquet(
        root / "evaluation-2026/predictions.parquet",
        columns=["row_key", "period_start", "selected"],
    )
    october = e3_saved.loc[pd.to_datetime(e3_saved.period_start).eq(pd.Timestamp("2026-10-12"))]
    later = pd.read_parquet(checked(read(checked(assessments[0]["outcome"]))["rows"]))
    joined = october.merge(later, on="row_key", validate="one_to_one")
    checks = []
    for name, part in (
        ("all_e4_historical_candidates", joined),
        ("selected_e4_historical_queue", joined.loc[joined.selected]),
    ):
        observed = part.loc[part.status.eq("observed")]
        checks.append(
            {
                "population": name,
                "candidates": len(part),
                "states": part.status.value_counts().to_dict(),
                "observed_rise_rate": float(observed.next_direction.eq(1).mean()),
                "observed_fall_rate": float(observed.next_direction.eq(-1).mean()),
            }
        )
    frozen_json(
        root / "october-later-check.json",
        {
            "version": "e4-later-outcome-check-v1",
            "checks": checks,
            "original_e3_labels_changed": False,
            "outcome": assessments[0]["outcome"],
            "scope": (
                "E4 saved historical policy on E3 origin population; no alteration to "
                "original retrospective comparison scores"
            ),
        },
    )
    frozen_json(
        manifest,
        {
            "version": "e4-lifecycle-v1",
            "created_utc": utc(),
            "assessments": assessments,
            "retries": retry,
            "comparison": compare_assessments(
                checked(assessments[0]["assessment"]), checked(assessments[1]["assessment"])
            ),
            "later_check": pin(root / "october-later-check.json"),
        },
    )
    return manifest


def publish_index(root: Path, outcome_path: Path | None = None) -> Path:
    from .training_contract import atomic_json

    lifecycle = read(root / "lifecycle.json")
    if (root / "index.json").exists():
        previous = {
            item["assessment"]["sha256"]: item for item in read(root / "index.json")["assessments"]
        }
        for entry in lifecycle["assessments"]:
            if entry["assessment"]["sha256"] in previous:
                entry["outcome"] = previous[entry["assessment"]["sha256"]]["outcome"]
    if outcome_path:
        from .dataset import ResearchError

        outcome = read(outcome_path)
        checked(outcome["rows"])
        assessment_ref = outcome["binding"]["assessment"]
        checked(assessment_ref)
        matches = [
            entry
            for entry in lifecycle["assessments"]
            if entry["assessment"]["sha256"] == assessment_ref["sha256"]
        ]
        if len(matches) != 1:
            raise ResearchError("e4_index_outcome_assessment_incompatible")
        matches[0]["outcome"] = pin(outcome_path)
    for entry in lifecycle["assessments"]:
        entry["label"] = f"{entry['origin']} - {entry['mode'].replace('_', ' ')}"
    index = {
        "version": "e4-result-index-v1",
        "status": "research_not_promoted",
        "created_utc": utc(),
        "protocol": pin(root / "protocol.json"),
        "summary": pin(root / "summary.json"),
        "evaluation": pin(root / "evaluation.json"),
        "comparison": pin(root / "cito-comparison/comparison.json"),
        "diagnostics": pin(root / "diagnostics-complete.json"),
        "scenarios": pin(root / "scenarios.json"),
        "assessments": lifecycle["assessments"],
        "compatibility": lifecycle["comparison"],
    }
    atomic_json(root / "index.json", index)
    atomic_json(
        root / "current.json",
        {
            "version": "e4-current-v1",
            "index": pin(root / "index.json"),
            "status": "research_not_promoted",
            "scheduling": "disabled and uninstalled",
        },
    )
    return root / "index.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "evaluate",
            "features",
            "initialize",
            "scenarios",
            "diagnostics",
            "index",
            "prepare",
            "outcome",
            "intake",
            "acquire",
        ],
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--features", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--assessment", type=Path)
    parser.add_argument("--official", type=Path)
    parser.add_argument("--outcome", type=Path)
    parser.add_argument("--original", type=Path)
    parser.add_argument("--supersedes", type=Path)
    parser.add_argument("--period")
    args = parser.parse_args()
    data = args.data or args.root.parent
    result: Any = None
    if args.command == "evaluate":
        from .e4_evaluate import evaluate

        result = evaluate(args.root)
    elif args.command == "features":
        from .e4_features import initialize

        result = initialize(args.root, data / "training/current.json", data / "research-e3/run-v1")
        check_feature_dependencies(args.root, data)
    elif args.command == "initialize":
        result = initialize_assessments(args.root, data / "research-e3/run-v1")
    elif args.command == "scenarios":
        from .e4_scenarios import execute

        result = execute(args.root)
    elif args.command == "diagnostics":
        from .e4_diagnostics import complete_details
        from .e4_diagnostics import execute as diagnose

        result = diagnose(args.root, data)
        if (args.root / "lifecycle.json").exists():
            result = complete_details(args.root, data)
    elif args.command == "index":
        result = publish_index(args.root, args.outcome)
    elif args.command == "prepare":
        from .e4_assessment import prepare

        if not args.features or not args.context:
            parser.error("prepare needs --features and --context")
        check_prediction_cutoffs(
            read(args.context), args.root / "model-bundle.json", args.root / "calibration.json"
        )
        result = prepare(
            args.root,
            args.features,
            read(args.context),
            args.root / "model-bundle.json",
            args.root / "calibration.json",
        )
    elif args.command == "outcome":
        from .e4_assessment import attach_outcome

        if not args.assessment:
            parser.error("outcome needs --assessment; omit --official for a pending check")
        result = attach_outcome(args.assessment, args.official)
    elif args.command == "intake":
        from .e4_sources import intake_report

        if not args.original or not args.context:
            parser.error("intake needs --original and --context")
        result = intake_report(
            args.original,
            data / "sources",
            args.root / "reports",
            read(args.context),
            supersedes=args.supersedes,
        )
    elif args.command == "acquire":
        from .e4_sources import acquire_official, parse_official

        if not args.period:
            parser.error("acquire needs --period from the reviewed calendar")
        acquired = acquire_official(
            data / "foundation-operations/config.json", args.period, args.root / "sources"
        )
        result = parse_official(acquired, acquired.parent)
    print(result)


if __name__ == "__main__":
    main()
