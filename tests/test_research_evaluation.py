"""Synthetic E1 acceptance with independently specified causal expectations."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from bef_redbird.research import artifacts as artifacts_module
from bef_redbird.research import dataset as dataset_module
from bef_redbird.research.artifacts import append_exposure, replay_fingerprint, verify_run
from bef_redbird.research.dataset import ResearchError, load_dataset, validate_dataset
from bef_redbird.research.evaluation import EvaluationConfig, decision_cutoff, evaluate
from bef_redbird.research.features import (
    FEATURE_NAMES,
    next_target,
    period_features,
    reversal_target,
)


def config() -> EvaluationConfig:
    return EvaluationConfig(
        initial_train=(0, 3),
        validation=(4, 5),
        final_evaluation=(6, 9),
        diagnostic=(10, 11),
        bootstrap_replicates=40,
    )


def recipe(*, periods: int = 12, packages: int = 4) -> list[dict[str, Any]]:
    return [
        {
            "period_index": str(index),
            "canonical_identifier": f"{package + 1:06d}",
            "identity_episode_id": f"identity-{package}",
            "amount_ore": str(1000 + package * 100),
            "eligible": "true",
            "exclusion_reason": "",
            "abc": "ABC"[package % 3],
            "group_id": "same-current-group",
            "provenance_id": f"source-{index}-{package}",
        }
        for index in range(periods)
        for package in range(packages)
    ]


def freeze(
    root: Path,
    rows: list[dict[str, Any]],
    *,
    periods: int = 12,
    extra: dict[str, Any] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    text = io.StringIO(newline="")
    columns = sorted({name for row in rows for name in row})
    writer = csv.DictWriter(text, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    panel = root / "panel.csv.gz"
    panel.write_bytes(gzip.compress(text.getvalue().encode(), mtime=0))
    pin = {
        "role": "panel",
        "path": panel.name,
        "sha256": hashlib.sha256(panel.read_bytes()).hexdigest(),
        "size_bytes": panel.stat().st_size,
    }
    manifest = {
        "schema_version": "e1-panel-v1",
        "dataset_id": "invented-market",
        "price_context": {
            "price_type": "aip",
            "currency": "DKK",
            "price_basis": "per_package",
            "tax_basis": "excluded",
            "fee_basis": "excluded",
        },
        "availability_mode": "reconstructed_research",
        "periods": [
            {
                "index": index,
                "start": (date(2025, 1, 6) + timedelta(days=14 * index)).isoformat(),
                "end_exclusive": (date(2025, 1, 6) + timedelta(days=14 * (index + 1))).isoformat(),
            }
            for index in range(periods)
        ],
        "files": [pin],
        "population_definition": "All synthetic current rows, independently by period",
        "identity_policy": "Explicit synthetic episodes split before unknown continuity",
        "provenance": {"kind": "invented fixture", "version": 1},
    }
    manifest.update(extra or {})
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path


def records(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return [json.loads(line) for line in source]


def test_no_change_denominator_and_exact_scores(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe())
    run = evaluate(path, tmp_path / "result", config=config())
    assert run["evaluation_locked"] is True
    assert verify_run(tmp_path / "result")["run_fingerprint"] == run["run_fingerprint"]
    metrics = json.loads((tmp_path / "result" / "metrics.json").read_text())
    selected = [
        row
        for row in metrics["aggregate"]
        if row["task"] == "next_period" and row["split"] == "final_evaluation"
    ]
    assert {row["n"] for row in selected} == {16}
    no_change = next(row for row in selected if row["method"] == "no_change")
    assert no_change["metrics"] == {
        "brier_multiclass": 0,
        "direction_accuracy": 1,
        "mae_log_return": 0,
        "mae_price_dkk": 0,
        "precision_top_decile_rise": 0,
    }
    assert no_change["metric_denominators"]["precision_top_decile_rise"] == 4
    assert [
        row
        for row in metrics["aggregate"]
        if row["task"] == "reversal" and row["split"] == "final_evaluation"
    ][0]["n"] == 0
    calibration = [
        row
        for row in metrics["calibration"]
        if row["method"] == "no_change"
        and row["split"] == "final_evaluation"
        and row["class_label"] == "0"
        and row["n"]
    ]
    assert calibration == [
        {
            "task": "next_period",
            "split": "final_evaluation",
            "method": "no_change",
            "class_label": "0",
            "bin_lower": 0.9,
            "bin_upper": 1.0,
            "n": 16,
            "mean_probability": 1.0,
            "observed_frequency": 1.0,
        }
    ]


def test_exact_one_ore_direction_price_and_multiclass_score(tmp_path: Path) -> None:
    rows = recipe(packages=1)
    for row in rows:
        index = int(row["period_index"])
        row["amount_ore"] = str(1000 + (index % 2))
    path = freeze(tmp_path / "input", rows)
    evaluate(path, tmp_path / "result", config=config())
    predictions = records(tmp_path / "result" / "predictions.jsonl.gz")
    assert predictions[0]["targets"]["next_period"]["direction"] == 1
    assert predictions[1]["targets"]["next_period"]["direction"] == -1
    metrics = json.loads((tmp_path / "result" / "metrics.json").read_text())
    score = next(
        row
        for row in metrics["aggregate"]
        if row["task"] == "next_period"
        and row["method"] == "no_change"
        and row["split"] == "final_evaluation"
    )
    assert score["n"] == 4
    assert score["metrics"]["brier_multiclass"] == 2
    assert score["metrics"]["direction_accuracy"] == 0
    assert score["metrics"]["mae_price_dkk"] == pytest.approx(0.01)


def test_future_prices_attributes_groups_and_supply_do_not_change_earlier_predictions(
    tmp_path: Path,
) -> None:
    original = recipe(packages=8)
    for row in original:
        row.update(reported_supply_positive="", supply_coverage_unknown="true")
    modified = [row.copy() for row in original]
    for row in modified:
        if int(row["period_index"]) >= 8:
            row.update(
                amount_ore="999999",
                abc="C",
                group_id="future-only-group",
                reported_supply_positive="true",
                supply_coverage_unknown="false",
                identity_episode_id="future-identity-" + row["canonical_identifier"],
            )
    first = freeze(tmp_path / "first", original)
    second = freeze(tmp_path / "second", modified)
    evaluate(first, tmp_path / "a", config=config())
    evaluate(second, tmp_path / "b", config=config())
    before = records(tmp_path / "a" / "predictions.jsonl.gz")
    after = records(tmp_path / "b" / "predictions.jsonl.gz")
    for left, right in zip(before, after, strict=True):
        if left["period_index"] < 8:
            assert left["features"] == right["features"]
            assert left["next_period"] == right["next_period"]
            assert left["reversal"] == right["reversal"]
    for left, right in zip(
        records(tmp_path / "a" / "models.jsonl.gz"),
        records(tmp_path / "b" / "models.jsonl.gz"),
        strict=True,
    ):
        if left["period_index"] < 8:
            assert left == right


def test_future_label_maturity_and_train_only_preprocessing(tmp_path: Path) -> None:
    rows = recipe(packages=1)
    amounts = [100, 200, 1000, 150, 400, 400, 500, 450, 900, 800, 700, 600]
    for row, amount in zip(rows, amounts, strict=True):
        row["amount_ore"] = str(amount)
    path = freeze(tmp_path / "input", rows)
    evaluate(path, tmp_path / "result", config=config())
    models = records(tmp_path / "result" / "models.jsonl.gz")
    next2 = next(row for row in models if row["period_index"] == 2 and row["task"] == "next_period")
    assert next2["n"] == 2
    assert next2["max_training_origin"] == 1
    assert next2["max_training_maturity"] == 2
    assert next2["mean"][0] == pytest.approx(np.log(2) / 2)  # Train AIP1DKK and2DKK only.
    assert next2["scale"][0] == pytest.approx(np.log(2) / 2)
    reversal2 = next(
        row for row in models if row["period_index"] == 2 and row["task"] == "reversal"
    )
    reversal3 = next(
        row for row in models if row["period_index"] == 3 and row["task"] == "reversal"
    )
    assert reversal2["n"] == 0  # Rise observed at1 but two-period outcome not mature.
    assert reversal3["n"] == 1
    assert reversal3["max_training_origin"] == 1
    assert reversal3["max_training_maturity"] == 3
    assert all(
        row["max_training_maturity"] is None or row["max_training_maturity"] <= row["period_index"]
        for row in models
    )


def test_observed_rise_full_partial_and_persistence_require_complete_horizon(
    tmp_path: Path,
) -> None:
    rows = recipe(packages=3)
    series = [[100, 200, 100, 250], [100, 200, 175, 180], [100, 200, 200, 250]]
    for row in rows:
        index = int(row["period_index"])
        package = int(row["canonical_identifier"]) - 1
        row["amount_ore"] = str(series[package][min(index, 3)])
    dataset = load_dataset(freeze(tmp_path / "input", rows))
    targets = [reversal_target(dataset, row) for row in dataset.by_period[1]]
    assert [target.reversal_category for target in targets] == [
        "full_reversal",
        "partial_reversal",
        "persistence",
    ]
    assert [target.full_reversal for target in targets] == [1, 0, 0]
    assert all(target.reference_price_ore == 100 for target in targets)
    assert reversal_target(dataset, dataset.by_period[0][0]).state == "not_observed_rise"
    # A full reversal after one period is still unevaluable if second is missing.
    rows = [
        row
        for row in rows
        if not (row["period_index"] == "3" and row["canonical_identifier"] == "000001")
    ]
    for row in rows:
        if int(row["period_index"]) >= 4 and row["canonical_identifier"] == "000001":
            row["identity_episode_id"] = "after-gap"
    missing = load_dataset(freeze(tmp_path / "missing", rows))
    assert reversal_target(missing, missing.lookup[1, "000001"]).state == "gap"


@pytest.mark.parametrize(
    "reason,amount",
    [
        ("zero_price", "0"),
        ("lifecycle_censoring", ""),
        ("conflict", ""),
        ("identity_unresolved", ""),
        ("unsupported_price_basis", "999"),
    ],
)
def test_ineligible_future_states_not_encoded_as_no_change(
    tmp_path: Path, reason: str, amount: str
) -> None:
    rows = recipe(packages=1)
    rows[3]["amount_ore"] = "1500"
    rows[4].update(eligible="false", exclusion_reason=reason, amount_ore=amount)
    for row in rows[5:]:
        row["identity_episode_id"] = "new-after-unavailable"
    dataset = load_dataset(freeze(tmp_path / "input", rows))
    current = dataset.lookup[3, "000001"]
    assert next_target(dataset, current).state == reason
    assert next_target(dataset, current).direction is None
    assert reversal_target(dataset, current).state == reason
    assert reversal_target(dataset, current).full_reversal is None
    assert next_target(dataset, dataset.by_period[-1][0]).state == "pending"


def test_identity_change_and_calendar_gap_are_separate(tmp_path: Path) -> None:
    rows = recipe(packages=1)
    for row in rows[4:]:
        row["identity_episode_id"] = "new-context"
    dataset = load_dataset(freeze(tmp_path / "identity", rows))
    assert next_target(dataset, dataset.by_period[3][0]).state == "identity_change"
    _, values = period_features(dataset, 4)
    assert values[0, FEATURE_NAMES.index("lag1_missing")] == 1
    assert values[0, FEATURE_NAMES.index("history_periods")] == 0
    gap = [row for row in rows if row["period_index"] != "4"]
    dataset = load_dataset(freeze(tmp_path / "gap", gap))
    assert next_target(dataset, dataset.by_period[3][0]).state == "gap"


def test_contemporaneous_group_and_abc_features_and_missing_supply(tmp_path: Path) -> None:
    rows = recipe(packages=3)
    rows[0]["amount_ore"] = "200"
    rows[1]["amount_ore"] = "100"
    rows[2].update(eligible="false", exclusion_reason="unsupported_basis", amount_ore="1")
    for row in rows[3:]:
        if row["canonical_identifier"] == "000003":
            row["identity_episode_id"] = "after-exclusion"
    dataset = load_dataset(freeze(tmp_path / "input", rows))
    _, features = period_features(dataset, 0)
    assert features[0, FEATURE_NAMES.index("group_count")] == 2
    assert features[0, FEATURE_NAMES.index("own_to_group_min_aip")] == 2
    assert features[0, FEATURE_NAMES.index("abc_A")] == 1
    assert features[0, FEATURE_NAMES.index("supply_report_unobserved")] == 1
    assert features[0, FEATURE_NAMES.index("supply_coverage_unknown")] == 1


def test_no_survivor_cohort_and_complete_exclusion_assessment(tmp_path: Path) -> None:
    rows = recipe(packages=2)
    rows = [
        row
        for row in rows
        if not (row["canonical_identifier"] == "000002" and int(row["period_index"]) > 4)
    ]
    rows[0].update(eligible="false", exclusion_reason="zero_price", amount_ore="0")
    for row in rows:
        if row["canonical_identifier"] == "000001" and int(row["period_index"]) > 0:
            row["identity_episode_id"] = "post-zero"
    path = freeze(tmp_path / "input", rows)
    evaluate(path, tmp_path / "result", config=config())
    predictions = records(tmp_path / "result" / "predictions.jsonl.gz")
    assert len(predictions) == len(rows)
    assert predictions[0]["eligible"] is False and predictions[0]["features"] is None
    exited = next(
        row
        for row in predictions
        if row["period_index"] == 4 and row["canonical_identifier"] == "000002"
    )
    assert exited["targets"]["next_period"]["state"] == "gap"
    coverage = json.loads((tmp_path / "result" / "coverage.json").read_text())
    assert coverage["per_period"][1]["eligible_candidates"] == 2
    assert coverage["per_period"][5]["eligible_candidates"] == 1


def test_replay_artifact_bytes_and_semantic_fingerprint_are_deterministic(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe(packages=8))
    evaluate(path, tmp_path / "one", config=config())
    evaluate(path, tmp_path / "two", config=config())
    assert replay_fingerprint(tmp_path / "one") == replay_fingerprint(tmp_path / "two")
    for name in (
        "predictions.jsonl.gz",
        "models.jsonl.gz",
        "metrics.json",
        "coverage.json",
        "input-manifest.json",
    ):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()
    lineage = [
        json.loads(line) for line in (tmp_path / "holdout-lineage.jsonl").read_text().splitlines()
    ]
    assert [event["sequence"] for event in lineage] == [0, 1, 2, 3]
    assert lineage[1]["previous_event_hash"] == lineage[0]["event_hash"]
    assert lineage[0]["event"] == "final_holdout_evaluation_started"
    locked = [event for event in lineage if event["event"] == "final_holdout_evaluation_locked"]
    assert locked[0]["run_fingerprint"] == locked[1]["run_fingerprint"]
    model_rows = records(tmp_path / "one" / "models.jsonl.gz")
    assert any(row["task"] == "next_period" and not row["fallback"] for row in model_rows)


def test_input_and_output_tampering_fail_closed(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe())
    panel = path.parent / "panel.csv.gz"
    original = panel.read_bytes()
    panel.write_bytes(original + b"tamper")
    with pytest.raises(ResearchError, match="hash_mismatch"):
        evaluate(path, tmp_path / "bad", config=config())
    assert not (tmp_path / "bad").exists()
    panel.write_bytes(original)
    evaluate(path, tmp_path / "good", config=config())
    target = tmp_path / "good" / "metrics.json"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError):
        verify_run(tmp_path / "good")


def test_all_additional_provenance_pins_verified(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe())
    evidence = path.parent / "provenance.jsonl.gz"
    evidence.write_bytes(gzip.compress(b'{"id":"invented-evidence"}\n'))
    manifest = json.loads(path.read_text())
    manifest["files"].append(
        {
            "role": "provenance",
            "path": evidence.name,
            "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            "size_bytes": evidence.stat().st_size,
        }
    )
    path.write_text(json.dumps(manifest))
    assert validate_dataset(path)["rows"] == 48
    evidence.write_bytes(b"not-the-same-provenance")
    with pytest.raises(ResearchError, match="hash_mismatch"):
        validate_dataset(path)


@pytest.mark.parametrize(
    "mutation", ["gap", "ineligible", "duplicate", "zero_eligible", "fractional", "supply_false"]
)
def test_panel_contract_rejects_ambiguous_or_corrupt_rows(tmp_path: Path, mutation: str) -> None:
    rows = recipe(packages=1)
    if mutation == "gap":
        rows.pop(4)
    elif mutation == "ineligible":
        rows[4].update(eligible="false", exclusion_reason="conflict")
    elif mutation == "duplicate":
        rows.append(rows[0].copy())
    elif mutation == "zero_eligible":
        rows[4]["amount_ore"] = "0"
    elif mutation == "fractional":
        rows[4]["amount_ore"] = "1.5"
    elif mutation == "supply_false":
        for row in rows:
            row["reported_supply_positive"] = "false"
    with pytest.raises(ResearchError):
        load_dataset(freeze(tmp_path / "input", rows))


def test_splits_actual_calendar_and_cutoff_assumption(tmp_path: Path) -> None:
    dataset = load_dataset(freeze(tmp_path / "input", recipe(periods=47), periods=47))
    EvaluationConfig().validate(47)
    assert decision_cutoff(dataset, 0, EvaluationConfig()) == "2025-01-15T12:00:00+01:00"
    assert decision_cutoff(dataset, 12, EvaluationConfig()) == "2025-07-02T12:00:00+02:00"
    with pytest.raises(ResearchError, match="nonoverlapping"):
        EvaluationConfig(validation=(23, 31)).validate(47)
    with pytest.raises(ResearchError, match="calendar"):
        EvaluationConfig().validate(46)


def test_lineage_corruption_blocks_completed_publication(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe())
    lineage = tmp_path / "lineage.jsonl"
    append_exposure(lineage, {"event": "synthetic-test"})
    lineage.write_bytes(lineage.read_bytes().replace(b"synthetic-test", b"tampered-event"))
    with pytest.raises(ResearchError, match="corrupt_lineage"):
        evaluate(path, tmp_path / "result", config=config(), lineage_path=lineage)
    assert not (tmp_path / "result").exists()
    assert not list(tmp_path.glob(".result.staging-*"))


def test_existing_run_never_overwritten(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe())
    evaluate(path, tmp_path / "result", config=config())
    before = (tmp_path / "result" / "run.json").read_bytes()
    with pytest.raises(ResearchError, match="already_exists"):
        evaluate(path, tmp_path / "result", config=config())
    assert (tmp_path / "result" / "run.json").read_bytes() == before


@pytest.mark.parametrize(
    "field,value", [("tax_basis", "unknown"), ("fee_basis", ""), ("fee_basis", "invented")]
)
def test_unknown_or_unsupported_price_context_is_not_comparable(
    tmp_path: Path, field: str, value: str
) -> None:
    path = freeze(tmp_path / "input", recipe())
    manifest = json.loads(path.read_text())
    manifest["price_context"][field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ResearchError, match="monetary_context"):
        load_dataset(path)


def test_negative_lifecycle_age_and_escaping_file_pin_rejected(tmp_path: Path) -> None:
    rows = recipe()
    for row in rows:
        row["lifecycle_age_periods"] = "-1"
    path = freeze(tmp_path / "negative", rows)
    with pytest.raises(ResearchError, match="integer"):
        load_dataset(path)
    path = freeze(tmp_path / "escape", recipe())
    manifest = json.loads(path.read_text())
    manifest["files"][0]["path"] = "../negative/panel.csv.gz"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ResearchError, match="escape"):
        load_dataset(path)


def test_exposed_parent_is_recorded_as_development_evidence(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe())
    original = evaluate(path, tmp_path / "first", config=config())
    downstream = evaluate(
        path,
        tmp_path / "second",
        config=config(),
        development_parent=original["run_fingerprint"],
    )
    assert downstream["development_parent"] == original["run_fingerprint"]
    events = [
        json.loads(line) for line in (tmp_path / "holdout-lineage.jsonl").read_text().splitlines()
    ]
    development = [event for event in events if event["event"] == "used_for_development"]
    assert len(development) == 1
    assert development[0]["run_fingerprint"] == original["run_fingerprint"]
    assert development[0]["downstream_run_fingerprint"] == downstream["run_fingerprint"]
    assert verify_run(tmp_path / "first")["run_fingerprint"] == original["run_fingerprint"]


def test_unsupported_strict_availability_claim_rejected(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe(), extra={"availability_mode": "strict_replay"})
    with pytest.raises(ResearchError, match="availability_mode"):
        load_dataset(path)


@pytest.mark.parametrize("changed_file", ["manifest", "panel"])
def test_input_recipe_changed_while_panel_is_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_file: str
) -> None:
    path = freeze(tmp_path / "input", recipe())
    original_boolean = dataset_module._boolean
    changed = False

    def change_after_manifest_parsed(value: str) -> bool:
        nonlocal changed
        if not changed:
            changed = True
            if changed_file == "manifest":
                manifest = json.loads(path.read_text())
                manifest["population_definition"] = "Changed context while reading panel"
                path.write_text(json.dumps(manifest))
            else:
                # The gzip reader already has its original bytes buffered. A
                # second verification must still notice a concurrent append.
                with (path.parent / "panel.csv.gz").open("ab") as target:
                    target.write(b"concurrent-change")
        return original_boolean(value)

    monkeypatch.setattr(dataset_module, "_boolean", change_after_manifest_parsed)
    with pytest.raises(ResearchError, match="changed_during_load|hash_mismatch|unreadable_panel"):
        evaluate(path, tmp_path / "result", config=config())
    assert not (tmp_path / "result").exists()
    assert not (tmp_path / "holdout-lineage.jsonl").exists()


def test_loaded_manifest_hash_and_copied_bytes_are_exact_same_capture(tmp_path: Path) -> None:
    path = freeze(tmp_path / "input", recipe())
    # Preserve deliberate noncanonical JSON whitespace in the frozen evidence.
    path.write_bytes(b"  " + path.read_bytes() + b"\n")
    dataset = load_dataset(path)
    assert dataset.manifest_bytes == path.read_bytes()
    assert dataset.manifest_hash == hashlib.sha256(dataset.manifest_bytes).hexdigest()
    evaluate(path, tmp_path / "result", config=config())
    assert (tmp_path / "result" / "input-manifest.json").read_bytes() == dataset.manifest_bytes


def test_dependency_lock_pin_has_explicit_checkout_and_wheel_cases(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    source_root = checkout / "src" / "bef_redbird" / "research"
    source_root.mkdir(parents=True)
    lock_bytes = b"version = 1\n# synthetic lock\n"
    (checkout / "uv.lock").write_bytes(lock_bytes)
    assert artifacts_module._dependency_lock(source_root) == {
        "status": "present",
        "filename": "uv.lock",
        "sha256": hashlib.sha256(lock_bytes).hexdigest(),
    }
    wheel_root = tmp_path / "site-packages" / "bef_redbird" / "research"
    wheel_root.mkdir(parents=True)
    # An unrelated nearby uv.lock must not be mistaken for the source lock.
    (tmp_path / "uv.lock").write_bytes(b"unrelated")
    assert artifacts_module._dependency_lock(wheel_root) == {
        "status": "absent",
        "reason": "no_source_checkout_lock",
    }


def test_timezone_pin_uses_actual_ordered_system_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    for root, data, release in (
        (first, b"first synthetic TZif", "2099a"),
        (second, b"other TZif", "2099b"),
    ):
        (root / "Europe").mkdir(parents=True)
        (root / "Europe" / "Copenhagen").write_bytes(data)
        (root / "tzdata.zi").write_text("# version " + release + "\n")
    monkeypatch.setattr(artifacts_module.zoneinfo, "TZPATH", (str(first), str(second)))
    assert artifacts_module._timezone_version() == {
        "key": "Europe/Copenhagen",
        "source": "system_tzdb",
        "iana_version": "2099a",
        "tzif_sha256": hashlib.sha256(b"first synthetic TZif").hexdigest(),
    }


def test_runtime_versions_pin_platform_packages_timezone_and_lock_without_private_paths() -> None:
    version = artifacts_module.versions()
    assert version["packages"]["numpy"] == version["numpy"]
    assert version["python_implementation"]
    assert set(version["platform"]) == {"system", "release", "machine"}
    assert version["dependency_lock"]["status"] == "present"
    assert len(version["timezone_data"]["tzif_sha256"]) == 64
    assert version["timezone_data"]["iana_version"]
    assert str(Path(__file__).resolve().parents[1]) not in json.dumps(version)
