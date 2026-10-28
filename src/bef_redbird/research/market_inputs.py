"""Advance a pinned causal market checkpoint by one supported tariff period.

This operator path reads frozen artifacts only. It does not acquire sources,
open the operating database, rebuild training data, or execute a model.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from bef_redbird.foundation.archive import external_path

from . import e4_features
from .dataset import ResearchError
from .e4_assessment import validate_features
from .e4_common import checked, frozen_json, read, utc
from .group_features import pin
from .prospective import publish_directory


def validate_step(
    previous: dict[str, Any], snapshot: dict[str, Any], calendar: list[dict[str, Any]]
) -> tuple[str, str]:
    """Require an immediate, evidenced successor; never infer a missing slot."""
    origin = snapshot["period"]
    matches = [p for p in calendar if p["start"] == origin]
    prior = [p for p in calendar if p["start"] == previous["origin"]]
    if (
        len(matches) != 1
        or len(prior) != 1
        or previous["target"] != origin
        or prior[0]["end_exclusive"] != origin
        or matches[0]["index"] != prior[0]["index"] + 1
        or snapshot.get("version") != "e4-official-snapshot-v1"
        or snapshot.get("discrepancies") != 0
        or snapshot["context"]["current_period"] != origin
        or snapshot["context"]["period_end_exclusive"] != matches[0]["end_exclusive"]
        or snapshot["context"]["release_date"] != origin
    ):
        raise ResearchError("market_input_nonconsecutive_or_unsupported_snapshot")
    target = matches[0]["end_exclusive"]
    if date.fromisoformat(origin) >= date.fromisoformat(target):
        raise ResearchError("market_input_invalid_calendar")
    return origin, target


def advance_checkpoint(previous_path: Path, snapshot_path: Path, output: Path) -> Path:
    """Publish an immutable next checkpoint from complete prior sufficient state.

    The caller explicitly supplies a supported calendar pin in the prior state.
    A later calendar admission is a different state manifest, not a mutation of
    the old checkpoint. Receipt clocks are preserved in the source snapshot.
    """
    previous_path, snapshot_path, output = (
        external_path(p) for p in (previous_path, snapshot_path, output)
    )
    previous, snapshot = read(previous_path), read(snapshot_path)
    if previous.get("version") != "causal-market-checkpoint-v1":
        raise ResearchError("market_input_checkpoint_version")
    calendar_path = checked(previous["calendar"])
    calendar = read(calendar_path)
    if isinstance(calendar, dict):
        calendar = calendar["periods"]
    origin, target = validate_step(previous, snapshot, calendar)
    binding = {
        "previous": pin(previous_path),
        "snapshot": pin(snapshot_path),
        "producer": [
            pin(Path(__file__).with_name(name))
            for name in ("e4_features.py", "e3_peers.py", "group_features.py")
        ]
        + [pin(Path(__file__))],
    }
    manifest = output / "checkpoint.json"
    if manifest.exists():
        saved = read(manifest)
        if saved["binding"] != binding:
            raise ResearchError("market_input_retry_context_changed")
        for name in ("raw", "features", "native", "calendar"):
            checked(saved[name])
        return manifest
    checked(snapshot["original"])
    checked(snapshot["rows"])
    prior_raw = pd.read_parquet(checked(previous["raw"]))
    prior_features = pd.read_parquet(checked(previous["features"]))
    prior_native = e4_features.records(pd.read_parquet(checked(previous["native"])))
    if (
        prior_raw.package_number.duplicated().any()
        or prior_features.package_number.duplicated().any()
        or not pd.to_datetime(prior_raw.period_start).eq(pd.Timestamp(previous["origin"])).all()
        or not pd.to_datetime(prior_features.period_start)
        .eq(pd.Timestamp(previous["origin"]))
        .all()
        or set(prior_raw.package_number) != set(prior_features.package_number)
    ):
        raise ResearchError("market_input_prior_population_or_period")
    for column in ("period_start", "creation_date", "exit_date"):
        if column in prior_raw:
            prior_raw[column] = pd.to_datetime(prior_raw[column]).dt.date
    prior_features["period_start"] = pd.to_datetime(prior_features.period_start).dt.date
    raw, native = e4_features.native(snapshot_path, prior_raw)
    base = e4_features.advance(raw, prior_raw, prior_features, calendar)
    features = e4_features.peers(raw, base, native, prior_raw, prior_features, prior_native)
    for name, column in (
        ("origin_creation_date", "creation_date"),
        ("origin_exit_date", "exit_date"),
    ):
        features[name] = (
            raw.set_index("package_number")[column]
            .reindex(features.package_number)
            .map(lambda value: value.isoformat() if pd.notna(value) else None)
            .to_numpy()
        )
    features["target_period"] = target
    features["advance_abc"] = None
    features["advance_state"] = "not_supplied"
    features["advance_source"] = "current_only"
    # Structural validation only; this does not assert historical availability
    # or enrol the origin as a prospective observation.
    validate_features(
        features,
        {
            "origin": origin,
            "target": target,
            "calendar_next_start": target,
            "decision_cutoff": origin + "T00:00:00+00:00",
            "mode": "synthetic",
        },
    )
    stage = output.with_name(output.name + ".stage")
    stage.mkdir(parents=True, exist_ok=False)
    raw.to_parquet(stage / "raw.parquet", index=False)
    features.to_parquet(stage / "features.parquet", index=False)
    pd.DataFrame(native).to_parquet(stage / "native.parquet", index=False)
    artifacts = {}
    for name in ("raw", "features", "native"):
        reference = pin(stage / f"{name}.parquet")
        reference["path"] = str(output / f"{name}.parquet")
        artifacts[name] = reference
    frozen_json(
        stage / "checkpoint.json",
        {
            "version": "causal-market-checkpoint-v1",
            "binding": binding,
            "origin": origin,
            "target": target,
            "built_utc": utc(),
            "calendar": pin(calendar_path),
            **artifacts,
            "rows": len(features),
            "eligible": int(features.eligible.sum()),
            "producer": pin(Path(__file__)),
            "source_snapshot": pin(snapshot_path),
            "availability": "New assembly; source receipt/publication remain separately recorded",
            "future_information": "Only prior sufficient state and same-origin native snapshot",
            "supplier_join": "unavailable; no identity or absence inference",
        },
    )
    publish_directory(stage, output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(advance_checkpoint(args.previous, args.snapshot, args.output))


if __name__ == "__main__":
    main()
