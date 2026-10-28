from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from bef_redbird.research import e3_recovery
from bef_redbird.research import e5_delivery as module
from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e4_common import frozen_json, read
from bef_redbird.research.group_features import pin


def publication(tmp_path: Path, *, recovery_bytes: int | None = None) -> Path:
    root = tmp_path / "research"
    root.mkdir()
    shared = tmp_path / "shared.json"
    frozen_json(shared, {"immutable": "shared accepted source"})
    limits = {} if recovery_bytes is None else {"recovery_bytes": recovery_bytes}
    frozen_json(
        root / "protocol.json",
        {"version": "e5-bounded-v1", "resource_limits": limits, "pins": {"shared": pin(shared)}},
    )
    frozen_json(root / "freeze.json", {"protocol": pin(root / "protocol.json")})
    frozen_json(
        root / "results.json",
        {
            "version": "e5-results-v1",
            "selected_configuration": {"quantile_interval_accepted": False},
        },
    )
    frozen_json(root / "selection.json", {"research_only": True})
    frozen_json(
        root / "execution-decisions.json", {"decisions": [], "reason": "synthetic complete matrix"}
    )
    frozen_json(root / "fit-ledger.json", {"trials": []})
    history = pd.DataFrame(
        {
            "row_key": ["h"],
            "package_number": ["000001"],
            "period_start": pd.to_datetime(["2025-06-02"]),
            "aip_ore": [10000],
            "p_rise": [0.5],
            "forecast_delta_dkk": [10.0],
            "selected": [True],
            "rank": [1],
        }
    )
    current = history.assign(
        row_key="c", package_number="000002", period_start=pd.Timestamp("2026-10-26")
    )
    history.to_parquet(root / "historical-review.parquet", index=False)
    history[["row_key", "package_number", "period_start"]].assign(
        next_status="observed", next_aip_ore=11000
    ).to_parquet(root / "historical-outcomes.parquet", index=False)
    frozen_json(
        root / "financial.json",
        {
            "files": {
                name: pin(root / name)
                for name in ("historical-review.parquet", "historical-outcomes.parquet")
            }
        },
    )
    assessment_dir = root / "assessments/current"
    assessment_dir.mkdir(parents=True)
    current.to_parquet(assessment_dir / "predictions.parquet", index=False)
    frozen_json(
        assessment_dir / "assessment.json",
        {
            "version": "e5-assessment-v1",
            "completed_utc": "2026-10-27T12:00:00+00:00",
            "candidate_count": 1,
            "binding": {"results": pin(root / "results.json")},
            "predictions": pin(assessment_dir / "predictions.parquet"),
        },
    )
    current[["row_key", "package_number"]].assign(
        status="pending_target_observation", next_aip_ore=None
    ).to_parquet(assessment_dir / "outcomes.parquet", index=False)
    frozen_json(
        assessment_dir / "outcome.json",
        {
            "binding": {"assessment": pin(assessment_dir / "assessment.json")},
            "rows": pin(assessment_dir / "outcomes.parquet"),
        },
    )
    frozen_json(
        root / "final-assessment.json",
        {
            "assessment": pin(assessment_dir / "assessment.json"),
            "outcome": pin(assessment_dir / "outcome.json"),
        },
    )
    (root / "population").mkdir()
    pd.DataFrame({"row_key": ["h", "c"]}).to_parquet(
        root / "population/population.parquet", index=False
    )
    frozen_json(
        root / "population/report.json",
        {"artifacts": [pin(root / "population/population.parquet")]},
    )
    (root / "inputs").mkdir()
    for name in ("features.parquet", "outcomes.parquet"):
        (root / "inputs" / name).write_bytes(b"explicit synthetic shared-input bytes")
    frozen_json(
        root / "inputs/manifest.json",
        {
            "files": {
                name: pin(root / "inputs" / name)
                for name in ("features.parquet", "outcomes.parquet")
            }
        },
    )
    return root


def source(tmp_path: Path, root: Path) -> Path:
    repo = tmp_path / "captured-repository"
    package = repo / "src/bef_redbird/research"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schema.sql").write_text("-- synthetic source, no database", encoding="utf-8")
    (package / "e5_assessment.py").write_text(
        """
def replay(assessment_path, *, relocations):
    import socket, duckdb, catboost
    checked = 0
    actions=(lambda: socket.create_connection(('example.invalid', 443)),
             lambda: duckdb.connect(),
             lambda: catboost.CatBoostRegressor().fit([[1]], [1]))
    for action in actions:
        try:
            action()
        except RuntimeError as error:
            assert 'forbidden' in str(error)
            checked += 1
    assert checked == 3
    return {'exact_predictions': True, 'exact_queues': True, 'guard_attempts': checked}
""",
        encoding="utf-8",
    )
    for name in ("pyproject.toml", "uv.lock", "research_dashboard.py"):
        (repo / name).write_text("# synthetic source", encoding="utf-8")
    module.capture_final_source(root, repo)
    # The minimal synthetic producer omits the real delivery module. Retain its
    # pinned source exactly, as a reviewed source-history payload member.
    producer_source = Path(module.__file__)
    history = root / "evidence/source-history" / (pin(producer_source)["sha256"] + ".py")
    history.parent.mkdir(parents=True)
    history.write_bytes(producer_source.read_bytes())
    return repo


def capsule(tmp_path: Path, **kwargs: int) -> tuple[Path, Path]:
    root = publication(tmp_path, **kwargs)
    module.publish_index(root)
    source(tmp_path, root)
    (root / "scratch").mkdir()
    (root / "scratch/temporary.bin").write_bytes(b"not scientific payload")
    (root / "server.log").write_text("temporary log", encoding="utf-8")
    inventory = module.create_inventory(root, root / "reviewed-inventory.json")
    bundle = tmp_path / "capsule"
    module.prepare_capsule(root, bundle, inventory)
    return root, bundle


def test_publish_separates_outcomes_retains_source_and_reuses_exact_index(tmp_path: Path) -> None:
    root = publication(tmp_path)
    original = (root / "assessments/current/predictions.parquet").read_bytes()
    index_path = module.publish_index(root)
    before = index_path.read_bytes()
    index = read(index_path)
    review = pd.read_parquet(index["review"]["path"])
    outcomes = pd.read_parquet(index["outcomes"]["path"])
    assert review.package_number.tolist() == ["000001", "000002"]
    assert not any(x.startswith("next_") for x in review)
    assert outcomes.next_status.tolist() == ["observed", "pending_target_observation"]
    assert (root / "assessments/current/predictions.parquet").read_bytes() == original
    assert module.publish_index(root) == index_path and index_path.read_bytes() == before
    current = read(root / "current.json")
    current["acceptance"] = "later owner acceptance"
    (root / "current.json").write_text(json.dumps(current), encoding="utf-8")
    module.publish_index(root)
    assert read(root / "current.json")["acceptance"] == "later owner acceptance"


def test_publish_pins_actual_assessment_model_bundle_without_changing_results(
    tmp_path: Path,
) -> None:
    root = publication(tmp_path)
    bundle = root / "actual-model-bundle.json"
    frozen_json(bundle, {"direction_fit_cutoff": "2026-07-01", "amount_fit_cutoff": "2026-09-01"})
    assessment_path = root / "assessments/current/assessment.json"
    assessment = read(assessment_path)
    assessment["binding"]["models"] = pin(bundle)
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
    outcome_path = root / "assessments/current/outcome.json"
    outcome = read(outcome_path)
    outcome["binding"]["assessment"] = pin(assessment_path)
    outcome_path.write_text(json.dumps(outcome), encoding="utf-8")
    pointer = root / "final-assessment.json"
    value = read(pointer)
    value["assessment"], value["outcome"] = pin(assessment_path), pin(outcome_path)
    pointer.write_text(json.dumps(value), encoding="utf-8")
    original = (root / "results.json").read_bytes()
    index_path = module.publish_index(root)
    assert read(index_path)["model_bundle"] == pin(bundle)
    assert read(index_path)["binding"]["model_bundle"] == pin(bundle)
    assert (root / "results.json").read_bytes() == original
    bundle.write_text("changed", encoding="utf-8")
    with pytest.raises(ResearchError):
        module.publish_index(root)


def test_publish_roundtrips_mixed_python_dates_timestamps_and_missing_target(
    tmp_path: Path,
) -> None:
    root = publication(tmp_path)
    historical_path = root / "historical-outcomes.parquet"
    pd.read_parquet(historical_path).assign(target_period=pd.NaT).to_parquet(
        historical_path, index=False
    )
    current_path = root / "assessments/current/outcomes.parquet"
    pd.read_parquet(current_path).assign(
        period_start=date(2026, 10, 26), target_period=date(2026, 11, 9)
    ).to_parquet(current_path, index=False)
    financial_path = root / "financial.json"
    financial = read(financial_path)
    financial["files"][historical_path.name] = pin(historical_path)
    financial_path.write_text(json.dumps(financial), encoding="utf-8")
    outcome_path = root / "assessments/current/outcome.json"
    outcome = read(outcome_path)
    outcome["rows"] = pin(current_path)
    outcome_path.write_text(json.dumps(outcome), encoding="utf-8")
    pointer_path = root / "final-assessment.json"
    pointer = read(pointer_path)
    pointer["outcome"] = pin(outcome_path)
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    originals = {p: p.read_bytes() for p in (historical_path, current_path)}
    index = read(module.publish_index(root))
    saved = pd.read_parquet(index["outcomes"]["path"])
    assert saved.period_start.tolist() == [pd.Timestamp("2025-06-02"), pd.Timestamp("2026-10-26")]
    assert saved.target_period.isna().tolist() == [True, False]
    assert saved.target_period.iloc[1] == pd.Timestamp("2026-11-09")
    assert saved.next_status.tolist() == ["observed", "pending_target_observation"]
    assert saved.period_start.dtype == saved.target_period.dtype == "datetime64[ns]"
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    original = Path(index["outcomes"]["path"]).read_bytes()
    module.publish_index(root)
    assert Path(index["outcomes"]["path"]).read_bytes() == original


def test_publish_refuses_incomplete_prediction_and_outcomes_in_review(tmp_path: Path) -> None:
    root = publication(tmp_path)
    path = root / "historical-review.parquet"
    pd.read_parquet(path).assign(next_aip_ore=999).to_parquet(path, index=False)
    financial = read(root / "financial.json")
    financial["files"][path.name] = pin(path)
    (root / "financial.json").write_text(json.dumps(financial), encoding="utf-8")
    with pytest.raises(ResearchError, match="outcomes_in_review"):
        module.publish_index(root)
    assert not (root / "index.json").exists()


def test_capsule_contract_exact_restore_and_read_only_retry(tmp_path: Path) -> None:
    root, bundle = capsule(tmp_path)
    manifest = read(bundle / "recovery.json")
    assert manifest["version"] == "e3-research-capsule-v1"
    names = {x["path"] for x in manifest["payload"]}
    assert "scratch/temporary.bin" not in names and "server.log" not in names
    assert "inputs/features.parquet" in names and "producer-final.zip" in names
    before = (bundle / "recovery.json").read_bytes()
    assert (
        module.prepare_capsule(root, bundle, root / "reviewed-inventory.json")
        == bundle / "recovery.json"
    )
    assert (bundle / "recovery.json").read_bytes() == before
    restored = tmp_path / "restored"
    e3_recovery.restore(bundle, restored, tmp_path / "restore.json")
    assert e3_recovery.verify_restored(bundle, restored)["payload_files"] == len(names)
    assert (restored / "results.json").read_bytes() == (root / "results.json").read_bytes()
    with pytest.raises(ResearchError, match="unused_paths"):
        e3_recovery.restore(bundle, restored, tmp_path / "other.json")


def test_capsule_refuses_missing_dependency_forbidden_payload_and_small_budget(
    tmp_path: Path,
) -> None:
    root = publication(tmp_path, recovery_bytes=1)
    module.publish_index(root)
    source(tmp_path, root)
    inventory = module.create_inventory(root, root / "inventory.json")
    with pytest.raises(ResearchError, match="capsule_storage_limit"):
        module.prepare_capsule(root, tmp_path / "too-small", inventory)
    assert not (tmp_path / "too-small/recovery.json").exists()
    value = read(inventory)
    value["files"] = [x for x in value["files"] if x != "assessments/current/predictions.parquet"]
    (tmp_path / "omitted.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ResearchError, match="missing_internal_dependency"):
        module.prepare_capsule(root, tmp_path / "omitted", tmp_path / "omitted.json")
    (root / "scratch").mkdir()
    (root / "scratch/a.bin").write_bytes(b"scratch")
    value = read(inventory)
    value["files"].append("scratch/a.bin")
    (tmp_path / "forbidden.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ResearchError, match="forbidden_inventory"):
        module.prepare_capsule(root, tmp_path / "forbidden", tmp_path / "forbidden.json")


def test_changed_shared_dependency_stops_restore_before_destination(tmp_path: Path) -> None:
    _, bundle = capsule(tmp_path)
    (tmp_path / "shared.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ResearchError, match="pin_changed"):
        e3_recovery.restore(bundle, tmp_path / "unused", tmp_path / "receipt.json")
    assert not (tmp_path / "unused").exists()


def test_captured_subprocess_replay_guards_and_original_absence(tmp_path: Path) -> None:
    root, bundle = capsule(tmp_path)
    restored = tmp_path / "restored"
    e3_recovery.restore(bundle, restored, tmp_path / "restore.json")
    producer = tmp_path / "producer"
    receipt = tmp_path / "replay.json"
    with pytest.raises(ResearchError, match="original_root_must_be_unavailable"):
        module.verify_replay(bundle, restored, producer, receipt)
    hidden = tmp_path / "original-unavailable"
    assert root.resolve().is_relative_to(tmp_path.resolve())
    assert hidden.resolve().is_relative_to(tmp_path.resolve())
    root.rename(hidden)
    proof = module.verify_replay(bundle, restored, producer, receipt)
    assert proof["status"] == "verified" and proof["restored_hashes_unchanged"]
    assert proof["replay"]["proof"]["guard_attempts"] == 3
    assert module.verify_replay(bundle, restored, producer, receipt) == proof


def historical_source_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str = "src/bef_redbird/research/e5_publish.py",
) -> tuple[Path, Path, dict, str]:
    root = tmp_path / "research"
    root.mkdir()
    repository = tmp_path / "repository"
    live = repository / relative
    live.parent.mkdir(parents=True)
    live.write_bytes(b"exact historical source bytes\n")
    ref = pin(live)
    live.write_bytes(b"later tightened completeness guard\n")
    module_location = repository / "src/bef_redbird/research/e5_delivery.py"
    monkeypatch.setattr(module, "__file__", str(module_location))
    frozen_json(root / "protocol.json", {"pins": {"historical": ref}})
    frozen_json(root / "freeze.json", {"protocol": pin(root / "protocol.json")})
    frozen_json(
        root / "producer-final.json",
        {
            "entries": [
                {
                    "relative": relative,
                    "sha256": pin(live)["sha256"],
                    "size_bytes": live.stat().st_size,
                }
            ]
        },
    )
    retained_relative = "evidence/source-history/" + ref["sha256"] + ".py"
    retained = root / retained_relative
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"exact historical source bytes\n")
    return root, live, ref, retained_relative


def test_listed_exact_historical_source_replaces_changed_live_source_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, live, _, retained = historical_source_fixture(tmp_path, monkeypatch)
    files = ["protocol.json", "freeze.json", "producer-final.json", retained]
    assert module.external_dependencies(root, files) == []
    assert live.read_bytes() == b"later tightened completeness guard\n"
    assert (root / retained).read_bytes() == b"exact historical source bytes\n"


@pytest.mark.parametrize("defect", ["missing", "altered", "unlisted", "wrong_size"])
def test_historical_source_requires_listed_exact_hash_and_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    root, _, ref, retained = historical_source_fixture(tmp_path, monkeypatch)
    files = ["protocol.json", "freeze.json", "producer-final.json", retained]
    if defect == "missing":
        (root / retained).unlink()
    elif defect == "altered":
        (root / retained).write_bytes(b"changed historical bytes")
    elif defect == "unlisted":
        files.remove(retained)
    else:
        ref["size_bytes"] += 1
        (root / "protocol.json").write_text(
            json.dumps({"pins": {"historical": ref}}), encoding="utf-8"
        )
        (root / "freeze.json").write_text(
            json.dumps({"protocol": pin(root / "protocol.json")}), encoding="utf-8"
        )
    with pytest.raises(ResearchError):
        module.external_dependencies(root, files)


@pytest.mark.parametrize("relative", ["src/private.parquet", "src/model.cbm", "tools/older.py"])
def test_retained_source_never_substitutes_for_data_models_or_code_outside_src(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    root, _, ref, retained = historical_source_fixture(tmp_path, monkeypatch, relative)
    # Even a matching metadata entry must not waive a live data/model pin check.
    (root / "producer-final.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"relative": relative, "sha256": ref["sha256"], "size_bytes": ref["size_bytes"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ResearchError, match="pin_changed"):
        module.external_dependencies(
            root, ["protocol.json", "freeze.json", "producer-final.json", retained]
        )
