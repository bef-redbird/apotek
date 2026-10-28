from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e4_common import frozen_json, read
from bef_redbird.research.e6_delivery import (
    Locations,
    capture_source,
    compare_frames,
    create_inventory,
    prepare_capsule,
    ready,
    reconstruct_evaluation,
    replay_a,
    replay_models,
    replay_scores,
    restore,
    verify_replay,
    verify_restored,
)
from bef_redbird.research.group_features import pin


def fixture(tmp_path: Path, *, limit: int = 8 * 1024**3) -> tuple[Path, Path]:
    root = tmp_path / "research-e6"
    root.mkdir()
    shared = tmp_path / "shared.json"
    frozen_json(shared, {"scientific_source": "synthetic"})
    frozen_json(
        root / "protocol.json",
        {
            "matrix": [],
            "pins": {"accepted": pin(shared)},
            "resource_limits": {"recovery_bytes": limit},
        },
    )
    frozen_json(root / "freeze.json", {"protocol": pin(root / "protocol.json")})
    for name in (
        "index.json",
        "summary.json",
        "model-results.json",
        "fit-ledger.json",
        "inputs/manifest.json",
        "investigation-a/results.json",
        "cito/summary.json",
    ):
        frozen_json(root / name, {"source": pin(shared)})
    (root / "scratch").mkdir()
    (root / "scratch/not-evidence.bin").write_bytes(b"excluded scratch")
    (root / "server.log").write_bytes(b"excluded log")
    repository = tmp_path / "repository"
    package = repository / "src/bef_redbird/research"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "e6_delivery.py").write_text(
        "def replay_local(root, original):\n"
        "    import catboost, duckdb, socket\n"
        "    from bef_redbird.research import e3_methods\n"
        "    assert not original.exists()\n"
        "    guards = 0\n"
        "    for call in (lambda: catboost.CatBoostRegressor().fit([], []),\n"
        "                 lambda: duckdb.connect(':memory:'),\n"
        "                 lambda: socket.create_connection(('localhost', 9)),\n"
        "                 lambda: e3_methods.temperature_fit(None, None)):\n"
        "        try: call()\n"
        "        except RuntimeError as exc:\n"
        "            assert 'forbidden' in str(exc)\n"
        "            guards += 1\n"
        "    assert guards == 4\n"
        "    return {'guard_attempts': guards}\n",
        encoding="utf-8",
    )
    for name in ("e3_methods.py", "e4_methods.py", "e5_results.py"):
        (package / name).write_text(
            "def temperature_fit(*args):\n    return None\n", encoding="utf-8"
        )
    for name in ("pyproject.toml", "uv.lock", "requirements.txt", "research_dashboard.py"):
        (repository / name).write_text("synthetic producer fixture\n", encoding="utf-8")
    return root, repository


def capsule(tmp_path: Path, *, limit: int = 8 * 1024**3) -> tuple[Path, Path]:
    root, repository = fixture(tmp_path, limit=limit)
    capture_source(root, repository)
    inventory = create_inventory(root, root / "inventory.json")
    bundle = tmp_path / "recovery/research-e6-v1"
    prepare_capsule(root, bundle, inventory)
    return root, bundle


def test_compact_capsule_exact_unused_restore_and_shared_pin_guard(tmp_path: Path) -> None:
    root, bundle = capsule(tmp_path)
    manifest = read(bundle / "recovery.json")
    assert manifest["version"] == "e6-research-capsule-v1"
    paths = {ref["path"] for ref in manifest["payload"]}
    assert "producer-final.zip" in paths and "inputs/manifest.json" in paths
    assert "inventory.json" not in paths
    assert not any("scratch" in path or path.endswith(".log") for path in paths)
    restored = tmp_path / "restored"
    proof = restore(bundle, restored, tmp_path / "restore.json")
    assert proof["status"] == "byte_verified"
    assert (restored / "summary.json").read_bytes() == (root / "summary.json").read_bytes()
    with pytest.raises(ResearchError, match="unused_paths"):
        restore(bundle, restored, tmp_path / "other.json")
    (restored / "summary.json").write_bytes(b"corrupted")
    with pytest.raises(ResearchError, match="pin_changed"):
        verify_restored(bundle, restored)
    (tmp_path / "shared.json").write_bytes(b"changed dependency")
    with pytest.raises(ResearchError, match="pin_changed"):
        restore(bundle, tmp_path / "unused", tmp_path / "unused.json")
    assert not (tmp_path / "unused").exists()


def test_pinned_older_audit_failure_logs_remain_recoverable(tmp_path: Path) -> None:
    root, repository = fixture(tmp_path)
    log = root / "review/older-feature-alignment-v2.log"
    log.parent.mkdir()
    log.write_bytes(b"Preserved technical resource failure, not a predictive score.\n")
    frozen_json(root / "review/failure.json", {"original_log": pin(log)})
    capture_source(root, repository)
    inventory = create_inventory(root, root / "inventory.json")
    bundle = tmp_path / "recovery/research-e6-v1"
    prepare_capsule(root, bundle, inventory)
    paths = {item["path"] for item in read(bundle / "recovery.json")["payload"]}
    assert "review/older-feature-alignment-v2.log" in paths
    assert "server.log" not in paths
    restored = tmp_path / "restored"
    restore(bundle, restored, tmp_path / "restore.json")
    assert (restored / "review/older-feature-alignment-v2.log").read_bytes() == log.read_bytes()


def test_explicit_relocation_preserves_shared_paths_and_detects_changed_money(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    restored = tmp_path / "restored"
    original.mkdir()
    restored.mkdir()
    frozen_json(original / "prices.json", {"price_ore": 125})
    ref = pin(original / "prices.json")
    (restored / "prices.json").write_bytes((original / "prices.json").read_bytes())
    shared = tmp_path / "shared.json"
    frozen_json(shared, {"label": "shared"})
    locations = Locations(original, restored)
    assert locations.path(ref) == restored / "prices.json"
    assert locations.path(pin(shared)) == shared
    (restored / "prices.json").write_text('{"price_ore":126}', encoding="utf-8")
    with pytest.raises(ResearchError, match="pin_changed"):
        locations.path(ref)


def test_capture_preserves_historical_producer_from_exact_archive(tmp_path: Path) -> None:
    root, repository = fixture(tmp_path)
    path = repository / "src/bef_redbird/research/old.py"
    path.write_bytes(b"initial producer bytes\n")
    historical = pin(path)
    with zipfile.ZipFile(root / "producer-fit.zip", "x") as archive:
        archive.write(path, "src/bef_redbird/research/old.py")
    path.write_bytes(b"final corrected producer bytes\n")
    frozen_json(root / "historical-source.json", {"producer": historical})
    capture_source(root, repository)
    receipt = read(root / "producer-final.json")
    aliases = receipt["historical_source_aliases"]
    assert len(aliases) == 1 and aliases[0]["original"] == historical
    assert Path(aliases[0]["retained"]["path"]).read_bytes() == b"initial producer bytes\n"
    inventory = read(create_inventory(root, root / "inventory.json"))
    assert not any(ref["path"] == str(path) for ref in inventory["external"])
    assert path.read_bytes() == b"final corrected producer bytes\n"


def test_active_trials_omitted_inventory_database_and_ceiling_are_rejected(tmp_path: Path) -> None:
    root, repository = fixture(tmp_path)
    protocol = read(root / "protocol.json")
    protocol["matrix"] = [{"id": "pending"}]
    (root / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    (root / "freeze.json").write_text(
        json.dumps({"protocol": pin(root / "protocol.json")}), encoding="utf-8"
    )
    with pytest.raises(ResearchError, match="trial_not_terminal"):
        ready(root)
    frozen_json(root / "trials/pending/receipt.json", {"status": "completed"})
    capture_source(root, repository)
    inventory = create_inventory(root, root / "inventory.json")
    (root / "new-result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ResearchError, match="missing_or_new_artifacts"):
        prepare_capsule(root, tmp_path / "recovery/research-e6-v1", inventory)
    forbidden = tmp_path / "operating.duckdb"
    forbidden.write_bytes(b"must never be copied")
    frozen_json(root / "bad-dependency.json", {"source": pin(forbidden)})
    with pytest.raises(ResearchError, match="operating_database"):
        create_inventory(root, root / "another-inventory.json")
    small = tmp_path / "small"
    small.mkdir()
    small_root, repo = fixture(small, limit=1)
    capture_source(small_root, repo)
    small_inventory = create_inventory(small_root, small_root / "inventory.json")
    with pytest.raises(ResearchError, match="storage_limit_failure_retained"):
        prepare_capsule(small_root, small / "recovery/research-e6-v1", small_inventory)
    assert not (small / "recovery/research-e6-v1/recovery.json").exists()


def test_exact_keyed_comparison_rejects_omission_duplicates_raw_and_quantities() -> None:
    frame = pd.DataFrame(
        {
            "row_key": ["a", "b"],
            "raw_delta_dkk": [1.25, -2.0],
            "delta_dkk": [1.25, -1.0],
            "quantity": [1, 9],
        }
    )
    compare_frames(frame, frame.iloc[::-1], ["row_key"])
    for column in ("raw_delta_dkk", "delta_dkk", "quantity"):
        changed = frame.copy()
        changed.loc[0, column] += 1
        with pytest.raises(AssertionError):
            compare_frames(changed, frame, ["row_key"])
    with pytest.raises(ResearchError, match="identity_changed"):
        compare_frames(pd.concat([frame, frame.iloc[:1]]), frame, ["row_key"])
    with pytest.raises(AssertionError):
        compare_frames(frame.iloc[:1], frame, ["row_key"])


def test_fresh_captured_subprocess_replay_blocks_fit_network_and_database(tmp_path: Path) -> None:
    root, bundle = capsule(tmp_path)
    restored = tmp_path / "restored"
    restore(bundle, restored, tmp_path / "restore.json")
    with pytest.raises(ResearchError, match="original_root_must_be_unavailable"):
        verify_replay(bundle, restored, tmp_path / "producer", tmp_path / "replay.json")
    # Both resolved paths remain in this test workspace before the reversible move.
    hidden = tmp_path / "original-unavailable"
    assert root.resolve().is_relative_to(tmp_path.resolve())
    assert hidden.resolve().is_relative_to(tmp_path.resolve())
    root.rename(hidden)
    proof = verify_replay(bundle, restored, tmp_path / "producer", tmp_path / "replay.json")
    assert proof["status"] == "verified" and proof["restored_hashes_unchanged"]
    assert proof["original_root_unavailable"]
    assert proof["replay"]["proof"]["guard_attempts"] == 4
    assert proof["replay"]["captured_producer"]


def test_model_replay_runs_original_prediction_transform_without_fitting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import catboost

    original, restored = tmp_path / "original", tmp_path / "restored"
    restored.mkdir()
    cohort = pd.DataFrame(
        {
            "row_key": ["a", "b"],
            "package_number": ["000001", "000002"],
            "period_index": [0, 0],
            "period_start": pd.to_datetime(["2026-06-01"] * 2),
            "identity_episode_id": ["a", "b"],
            "aip_ore": [200, 400],
        }
    )
    cohort.to_parquet(restored / "features.parquet", index=False)

    def ref(name: str) -> dict:
        return {**pin(restored / name), "path": str(original / name)}

    frozen_json(
        restored / "inputs/manifest.json",
        {"partitions": [{"year": 2026, "features": ref("features.parquet")}]},
    )
    frozen_json(
        restored / "protocol.json",
        {"folds": [{"name": "spring", "cal_start": "2026-04-01", "end": "2026-09-01"}]},
    )
    (restored / "model.cbm").write_bytes(b"synthetic inference stub")
    expected = pd.DataFrame(
        {
            "row_key": ["a", "b"],
            "raw_delta_dkk": [-5.0, 3.0],
            "next_price_dkk": [0.0, 7.0],
            "delta_dkk": [-2.0, 3.0],
        }
    )
    expected.to_parquet(restored / "predictions.parquet", index=False)
    receipt = {
        "status": "completed",
        "spec": {
            "id": "spring-huber-current",
            "fold": "spring",
            "task": "amount",
            "objective": "huber",
        },
        "features": ["aip_ore"],
        "categories": [],
        "files": {name: ref(name) for name in ("model.cbm", "predictions.parquet")},
    }
    frozen_json(restored / "trials/t/receipt.json", receipt)

    class InferenceOnly:
        def load_model(self, path: str) -> None:
            assert Path(path).read_bytes() == b"synthetic inference stub"

        def predict(self, features: pd.DataFrame, thread_count: int) -> np.ndarray:
            assert features.aip_ore.tolist() == [200.0, 400.0]
            return np.array([-5.0, 3.0])

        def fit(self, *args: object) -> None:
            raise AssertionError("Recovery must never fit")

    monkeypatch.setattr(catboost, "CatBoostRegressor", InferenceOnly)
    proofs = replay_models(restored, Locations(original, restored))
    assert proofs == [{"trial": "spring-huber-current", "rows": 2, "exact_raw_and_supported": True}]
    expected.loc[0, "raw_delta_dkk"] = -4
    expected.to_parquet(restored / "predictions.parquet", index=False)
    receipt["files"]["predictions.parquet"] = ref("predictions.parquet")
    (restored / "trials/t/receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(AssertionError):
        replay_models(restored, Locations(original, restored))


def test_scientific_score_replay_recomputes_metrics_and_detects_false_summary(
    tmp_path: Path,
) -> None:
    from bef_redbird.research.e6_results import evaluate_values

    original, restored = tmp_path / "original", tmp_path / "restored"
    restored.mkdir()
    frame = pd.DataFrame(
        {
            "row_key": ["a", "b"],
            "period_start": pd.to_datetime(["2026-06-01"] * 2),
            "aip_ore": [10000, 20000],
            "history_periods": [8, 8],
            "peer_count": [1, 1],
            "advance_abc": ["A", None],
            "next_status": ["observed", "observed"],
            "next_delta_ore": [100, -300],
            "raw_delta_dkk": [2.0, -1.0],
            "delta_dkk": [2.0, -1.0],
            "history": ["3y", "3y"],
            "method": ["huber", "huber"],
            "information": ["current", "current"],
        }
    )
    frame[
        ["row_key", "aip_ore", "history_periods", "peer_count", "period_start", "advance_abc"]
    ].to_parquet(restored / "origin.parquet", index=False)

    def ref(name: str) -> dict:
        return {**pin(restored / name), "path": str(original / name)}

    frozen_json(
        restored / "inputs/manifest.json",
        {"partitions": [{"year": 2026, "features": ref("origin.parquet")}]},
    )
    frozen_json(
        restored / "protocol.json",
        {
            "folds": [
                {
                    "name": "spring",
                    "cal_start": "2026-04-01",
                    "start": "2026-06-01",
                    "end": "2026-09-01",
                }
            ]
        },
    )
    all_summaries, all_periods = [], []
    for support in ("raw", "supported"):
        summary, periods = evaluate_values(
            frame,
            frame.raw_delta_dkk.to_numpy(),
            "amount",
            {
                "investigation": "C",
                "fold": "spring",
                "task": "amount",
                "history": "3y",
                "method": "huber",
                "information": "current",
                "support": support,
            },
        )
        all_summaries.extend(summary)
        all_periods.extend(periods)
    # Independent expected errors are +1 and +2 DKK.
    assert all_summaries[0]["mae_dkk"] == 1.5
    assert all_summaries[0]["rmse_dkk"] == pytest.approx(np.sqrt(2.5))
    frame.drop(columns=["aip_ore", "history_periods", "peer_count"]).to_parquet(
        restored / "predictions.parquet", index=False
    )
    pd.DataFrame(all_periods).to_parquet(restored / "periods.parquet", index=False)
    result = {
        "version": "e6-amount-results-v1",
        "summaries": all_summaries,
        "files": {name: ref(name) for name in ("predictions.parquet", "periods.parquet")},
    }
    frozen_json(restored / "evaluations/C-spring/result.json", result)
    reproduced = {
        f"spring-huber-{mode}": frame[["row_key", "raw_delta_dkk", "delta_dkk"]]
        for mode in ("current", "advance")
    }
    proof = replay_scores(restored, Locations(original, restored), reproduced)
    assert proof["recomputed_summaries"] == 18
    result["summaries"][0]["mae_dkk"] = 0
    (restored / "evaluations/C-spring/result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(AssertionError):
        replay_scores(restored, Locations(original, restored), reproduced)


def test_a_replay_reconstructs_queues_and_integer_quantities_with_original_absent(
    tmp_path: Path,
) -> None:
    from bef_redbird.research.e6_finance import (
        current_only_frame,
        decision_gates,
        evaluate_fold,
        summaries,
    )

    original, restored = tmp_path / "original", tmp_path / "restored"
    restored.mkdir()
    predictions = pd.DataFrame(
        {
            "row_key": [f"r{i}" for i in range(10)],
            "package_number": [f"{i:06d}" for i in range(10)],
            "period_start": pd.Timestamp("2026-06-01"),
            "aip_ore": 10000,
            "eligible": True,
            "tuned_current_p0": 0.05,
            "tuned_current_p1": 0.35,
            "tuned_current_p2": 0.6,
            "origin_group": "g",
            "pack_size_token": "20",
            "pack_unit": "TAB",
            "peer_count": 9,
            "peer_feature_state": "complete_equal_size_peers",
            "history_periods": 8,
            "abc": "A",
            "package_distributor": "D",
        }
    )
    amount = predictions[["row_key"]].assign(next_price_dkk=110.0)
    quantile = predictions[["row_key"]].assign(q05_price_dkk=95.0)
    population = predictions[["row_key"]].assign(behavior50_excluded=False)
    predictions.to_parquet(tmp_path / "e5-predictions.parquet", index=False)
    population.to_parquet(tmp_path / "population.parquet", index=False)
    frozen_json(tmp_path / "population.json", {"artifacts": [pin(tmp_path / "population.parquet")]})
    receipts = {}
    for method, table in (("rmse", amount), ("quantile", quantile)):
        table.to_parquet(tmp_path / (method + ".parquet"), index=False)
        frozen_json(
            tmp_path / (method + ".json"),
            {
                "spec": {"information": "current"},
                "features": ["aip_ore"],
                "files": {"predictions.parquet": pin(tmp_path / (method + ".parquet"))},
            },
        )
        receipts[method] = pin(tmp_path / (method + ".json"))
    frame = current_only_frame(predictions, amount, quantile, population)
    e4 = predictions[["row_key"]].assign(
        perfect_current_p0=0.05, perfect_current_p1=0.35, perfect_current_p2=0.6
    )
    e4.to_parquet(tmp_path / "e4.parquet", index=False)
    labels = predictions[["row_key", "period_start", "package_number"]].assign(
        next_status="observed", next_aip_ore=11000
    )
    labels.to_parquet(tmp_path / "labels.parquet", index=False)
    frozen_json(
        tmp_path / "evaluation.json",
        {
            "files": {
                "outcomes.parquet": pin(tmp_path / "labels.parquet"),
                "predictions.parquet": pin(tmp_path / "e5-predictions.parquet"),
            }
        },
    )
    output = evaluate_fold(frame, e4, labels, "spring")
    result = {
        "binding": {
            "e4_predictions": pin(tmp_path / "e4.parquet"),
            "population": pin(tmp_path / "population.json"),
            "folds": {
                "spring": {
                    "evaluation": pin(tmp_path / "evaluation.json"),
                    "current_receipts": receipts,
                }
            },
        },
        "files": {},
    }
    for name in ("forecasts", "queues", "allocations", "periods"):
        output[name].to_parquet(restored / (name + ".parquet"), index=False)
        result["files"][name] = {
            **pin(restored / (name + ".parquet")),
            "path": str(original / (name + ".parquet")),
        }
    summary = summaries(output["periods"])
    result.update(summary=summary, decision_gates=decision_gates(summary))
    frozen_json(restored / "investigation-a/results.json", result)
    proof = replay_a(restored, Locations(original, restored))
    assert proof["exact_queues"] and proof["exact_quantities_and_exposure"]
    assert not original.exists()
    allocated = pd.read_parquet(restored / "allocations.parquet")
    allocated.loc[0, "quantity"] += 1
    allocated.to_parquet(restored / "allocations.parquet", index=False)
    result["files"]["allocations"] = {
        **pin(restored / "allocations.parquet"),
        "path": str(original / "allocations.parquet"),
    }
    (restored / "investigation-a/results.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(AssertionError):
        replay_a(restored, Locations(original, restored))


def test_direction_replay_links_components_peer_routes_missing_codes_and_temperature(
    tmp_path: Path,
) -> None:
    from bef_redbird.research.e3_methods import calibrate
    from bef_redbird.research.e6_results import queue_diagnostics
    from bef_redbird.research.training_contract import KEYS

    original, restored = tmp_path / "original", tmp_path / "restored"
    restored.mkdir()
    origin = pd.DataFrame(
        {
            "row_key": ["a", "b", "c"],
            "package_number": ["000001", "000002", "000003"],
            "period_index": 0,
            "period_start": pd.Timestamp("2026-06-01"),
            "identity_episode_id": ["a", "b", "c"],
            "aip_ore": 10000,
            "history_periods": 8,
            "peer_count": [2, 2, 0],
            "broad_peer_count": [2, 0, 0],
            "broad_peer_feature_state": "complete_same_unit_peers",
            "advance_abc": ["A", None, "C"],
        }
    )
    origin.to_parquet(restored / "origin.parquet", index=False)

    def local_ref(name: str) -> dict:
        return {**pin(restored / name), "path": str(original / name)}

    frozen_json(
        restored / "inputs/manifest.json",
        {"partitions": [{"year": 2026, "features": local_ref("origin.parquet")}]},
    )
    frozen_json(
        restored / "protocol.json",
        {
            "folds": [
                {
                    "name": "spring",
                    "reference_year": 2026,
                    "cal_start": "2026-04-01",
                    "start": "2026-06-01",
                    "end": "2026-09-01",
                }
            ]
        },
    )
    broad = {
        "current": np.array([[0.1, 0.2, 0.7], [0.2, 0.3, 0.5], [0.3, 0.3, 0.4]]),
        "advance": np.array([[0.3, 0.3, 0.4], [0.4, 0.1, 0.5], [0.1, 0.1, 0.8]]),
    }
    package = {
        "current": np.tile([0.2, 0.7, 0.1], (3, 1)),
        "advance": np.tile([0.6, 0.2, 0.2], (3, 1)),
    }
    equal = {
        "current": np.tile([0.05, 0.05, 0.9], (3, 1)),
        "advance": np.tile([0.7, 0.2, 0.1], (3, 1)),
    }
    result = {
        "version": "e6-direction-results-v1",
        "summaries": [{"fold": "spring"}],
        "reused": {},
        "calibration": {},
        "files": {},
    }
    for information in ("current", "advance"):
        for name, arrays in (("package", package), ("equal", equal)):
            frame = pd.DataFrame(arrays[information], columns=[f"p{k}_perfect" for k in range(3)])
            frame["row_key"] = origin.row_key
            path = tmp_path / f"{name}-{information}" / "predictions.parquet"
            path.parent.mkdir()
            frame.to_parquet(path, index=False)
            result["reused"][f"evaluation_2026-cb_{name}_{information}"] = {"files": [pin(path)]}
    routed_current = np.stack([broad["current"][0], equal["current"][1], package["current"][2]])
    routed_hybrid = np.stack([broad["advance"][0], equal["current"][1], package["advance"][2]])
    values = {
        ("component", "current"): broad["current"],
        ("component", "advance"): broad["advance"],
        ("routed", "current"): routed_current,
        ("routed", "hybrid"): routed_hybrid,
    }
    rows, queues, queue_periods = [], [], []
    for (route, information), probability in values.items():
        temperature = 0.8 if information == "current" else 1.2
        result["calibration"][f"D_6y_{route}_{information}"] = {
            "temperature": temperature,
            "cutoff": "2026-06-01",
        }
        independent = np.exp(np.log(probability) / temperature)
        independent /= independent.sum(axis=1, keepdims=True)
        transformed = calibrate(probability, {"temperature": temperature})
        np.testing.assert_allclose(transformed, independent, rtol=1e-15, atol=1e-15)
        frame = origin[list(KEYS)].assign(
            next_status="observed", next_delta_ore=100, next_direction=1
        )
        frame[[f"raw_p{k}" for k in range(3)]] = probability
        frame[[f"p{k}" for k in range(3)]] = transformed
        frame["history"], frame["route"], frame["information"] = "6y", route, information
        rows.append(frame)
        if route == "routed":
            queue, per_period = queue_diagnostics(frame, transformed)
            queue["history"], queue["information"] = "6y", information
            queues.append(queue)
            queue_periods.extend(
                [
                    {**row, "fold": "spring", "history": "6y", "information": information}
                    for row in per_period
                ]
            )
    expected = pd.concat(rows, ignore_index=True)
    pd.concat(queues, ignore_index=True).to_parquet(restored / "queues.parquet", index=False)
    result["files"]["queues.parquet"] = local_ref("queues.parquet")
    result["queue_periods"] = queue_periods
    reproduced = {}
    for mode in ("current", "advance"):
        frame = pd.DataFrame(broad[mode], columns=[f"p{k}" for k in range(3)])
        frame["row_key"] = origin.row_key
        reproduced[f"spring-direction-6y-{mode}"] = frame
    proof = reconstruct_evaluation(
        restored, Locations(original, restored), result, expected, reproduced
    )
    assert proof["exact_evaluation_vectors"] and proof["exact_direction_queues"]
    tampered = expected.copy()
    tampered.loc[0, "raw_p0"] += 0.01
    with pytest.raises(AssertionError):
        reconstruct_evaluation(
            restored, Locations(original, restored), result, tampered, reproduced
        )
    with pytest.raises(ResearchError, match="missing_reinferred_or_accepted_component"):
        reconstruct_evaluation(restored, Locations(original, restored), result, expected, {})


def test_amount_vectors_replay_current_hybrid_fallback_and_nonnegative_price_support(
    tmp_path: Path,
) -> None:
    original, restored = tmp_path / "original", tmp_path / "restored"
    restored.mkdir()
    origin = pd.DataFrame(
        {
            "row_key": ["a", "b"],
            "period_start": pd.Timestamp("2026-06-01"),
            "aip_ore": [10000, 20000],
            "advance_abc": ["A", None],
        }
    )
    origin.to_parquet(restored / "origin.parquet", index=False)
    reference = {**pin(restored / "origin.parquet"), "path": str(original / "origin.parquet")}
    frozen_json(
        restored / "inputs/manifest.json", {"partitions": [{"year": 2026, "features": reference}]}
    )
    frozen_json(
        restored / "protocol.json",
        {
            "folds": [
                {
                    "name": "spring",
                    "cal_start": "2026-04-01",
                    "start": "2026-06-01",
                    "end": "2026-09-01",
                }
            ]
        },
    )
    current = pd.DataFrame({"row_key": ["a", "b"], "raw_delta_dkk": [-200.0, 3.0]})
    advance = pd.DataFrame({"row_key": ["a", "b"], "raw_delta_dkk": [4.0, -300.0]})
    saved = pd.DataFrame(
        {
            "row_key": ["a", "b", "a", "b"],
            "history": "3y",
            "method": "huber",
            "information": ["current", "current", "hybrid", "hybrid"],
            "raw_delta_dkk": [-200.0, 3.0, 4.0, 3.0],
            "delta_dkk": [-100.0, 3.0, 4.0, 3.0],
            "next_price_dkk": [0.0, 203.0, 104.0, 203.0],
            "support_adjusted": [True, False, False, False],
        }
    )
    result = {"version": "e6-amount-results-v1", "summaries": [{"fold": "spring"}]}
    reproduced = {"spring-huber-current": current, "spring-huber-advance": advance}
    proof = reconstruct_evaluation(
        restored, Locations(original, restored), result, saved, reproduced
    )
    assert proof["exact_evaluation_vectors"]
    # A next-ABC leak would use -300 rather than the true current +3 for missing row b.
    changed = saved.copy()
    changed.loc[3, "raw_delta_dkk"] = -300
    with pytest.raises(AssertionError):
        reconstruct_evaluation(restored, Locations(original, restored), result, changed, reproduced)
