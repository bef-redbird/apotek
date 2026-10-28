from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import bef_redbird.research.e5_assessment as module
from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e4_common import frozen_json, read
from bef_redbird.research.group_features import pin


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_key": ["a", "b", "c"],
            "package_number": ["000001", "000002", "000003"],
            "period_start": pd.to_datetime([module.ORIGIN] * 3),
            "period_index": [100] * 3,
            "identity_episode_id": ["i1", "i2", "i3"],
            "aip_ore": [10000, 10000, None],
            "eligible": [True, True, False],
            "exclusion_reason": ["", "", "missing_aip"],
            "target_period": pd.to_datetime([module.TARGET] * 3),
            "advance_abc": [None] * 3,
            "advance_state": ["missing"] * 3,
            "origin_group": ["g"] * 3,
            "pack_size_token": ["20"] * 3,
            "pack_unit": ["TAB"] * 3,
            "peer_count": [2] * 3,
            "peer_feature_state": ["complete_equal_size_peers"] * 3,
            "broad_peer_count": [1] * 3,
            "broad_peer_feature_state": ["complete_same_unit_peers"] * 3,
            "origin_identity_token": ["drug1", "drug2", "drug3"],
            "origin_creation_date": ["2020-01-01"] * 3,
            "origin_exit_date": [None] * 3,
        }
    )


def raw(values: pd.DataFrame, bundle: dict, **kwargs: Any) -> dict[str, np.ndarray]:
    n = len(values)
    result = {}
    for mode, p in (("current", [0.05, 0.05, 0.90]), ("advance", [0.85, 0.1, 0.05])):
        for peer in ("package", "equal", "broad"):
            result[f"cb_{peer}_{mode}"] = np.tile(p, (n, 1))
        result[f"mean_{mode}"] = np.full(n, 30.0 if mode == "current" else 200.0)
        result[f"amount_delta_floor_{mode}"] = np.full(n, 2.0 if mode == "current" else 50.0)
        result[f"quantile_{mode}"] = np.tile(
            [40.0, -20.0, 3.0] if mode == "current" else [-50.0, 100.0, 200.0], (n, 1)
        )
        result[f"persistence_{mode}"] = np.full(n, 0.6 if mode == "current" else 0.9)
    return result


def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    f = frame()
    f.to_parquet(tmp_path / "input.parquet", index=False)
    ref = f.assign(
        p_rise=[0.1, 0.9, None],
        p_fall=[0.1, 0.01, None],
        selected=[False, True, False],
        rank=[2, 1, 0],
    )
    ref.to_parquet(tmp_path / "reference.parquet", index=False)
    f[["row_key", "period_start"]].assign(
        behavior_selected_excluded=[False, True, False],
        lagged_reason=["retained", "excluded", "unknown"],
        package_distributor_name=["Company"] * 3,
        next_aip_ore=[99999] * 3,
    ).to_parquet(tmp_path / "population.parquet", index=False)
    state = {
        "cutoff": "2026-01-01",
        "last_maturity": "2025-12-22",
        "radii_dkk": {"0": 10, "1": 10, "2": 10},
    }
    frozen_json(
        tmp_path / "residual.json", {"residual": {"2026_current": state, "2026_advance": state}}
    )
    model = tmp_path / "model.bin"
    model.write_bytes(b"synthetic only; inference replaced by a fixed independent recipe")
    frozen_json(tmp_path / "trial.json", {"synthetic": True})
    bundle = {
        "models": {
            "synthetic": {
                "model": pin(model),
                "trial": pin(tmp_path / "trial.json"),
                "fit_cutoff": module.FIT,
                "latest_label_maturity": "2026-08-17",
            }
        },
        "amount_interval_calibration": pin(tmp_path / "residual.json"),
        "persistence_enabled": True,
    }
    frozen_json(tmp_path / "bundle.json", bundle)
    temp = {"temperature": 1.0, "cutoff": module.ORIGIN, "last_maturity": "2026-10-12"}
    frozen_json(
        tmp_path / "calibration.json",
        {
            "models": pin(tmp_path / "bundle.json"),
            "temperature": {"current": temp, "hybrid": temp},
            "persistence": {"current": temp, "advance": temp},
        },
    )
    monkeypatch.setattr(module, "_raw", raw)
    return {
        key: tmp_path / name
        for key, name in {
            "features": "input.parquet",
            "reference": "reference.parquet",
            "population": "population.parquet",
            "bundle": "bundle.json",
            "calibration": "calibration.json",
        }.items()
    }


def predict(
    paths: dict[str, Path], f: pd.DataFrame | None = None, accepted: bool = False
) -> tuple[pd.DataFrame, pd.DataFrame]:
    return module.predict(
        frame() if f is None else f,
        paths["bundle"],
        paths["calibration"],
        paths["reference"],
        paths["population"],
        interval_adopted=accepted,
    )


def test_current_fallback_mean_median_quantile_and_original_reference_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = inputs(tmp_path, monkeypatch)
    result, queues = predict(paths)
    assert len(result) == 3 and result.prediction_state.eq("available").sum() == 2
    assert result.selected.tolist() == [False, True, False]  # original E4 reference retained
    assert result.selected_e5_probability.tolist() == [True, False, False]
    assert result.reference_p_rise.iloc[1] == 0.9 and result.p_rise.iloc[0] == pytest.approx(0.9)
    assert result.forecast_delta_dkk.iloc[0] == 30 and result.median_delta_dkk.iloc[0] == 2
    assert result.forecast_percent.iloc[0] == 30
    assert result.lower_delta_dkk.iloc[0] == -8  # retained median2 minus radius10
    assert result.experimental_lower_delta_dkk.iloc[0] == -20
    assert result.quantile_crossed.iloc[0]
    assert result.persistence_probability.iloc[0] == pytest.approx(0.6)
    assert result.information_route.iloc[0] == "current-only fallback"
    assert not result.policy_eligible.iloc[1] and "next_aip_ore" not in result
    assert set(queues.package_id) == {"000001", "000002"}
    assert queues.loc[queues.policy.eq("probability_reference"), "eligible"].eq(False).any()
    assert not any("quantity" in x for x in result)


def test_accepted_quantiles_select_experimental_downside_and_keep_all_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = inputs(tmp_path, monkeypatch)
    result, _ = predict(paths, accepted=True)
    assert result.policy.eq("downside_aware").all()
    assert result.selected.tolist() == [True, False, False]
    assert result.lower_delta_dkk.iloc[0] == -20 and result.accepted_lower_delta_dkk.iloc[0] == -8
    assert result.quantile_crossed.dtype == "boolean"
    assert pd.isna(result.forecast_delta_dkk.iloc[2])


def test_selection_explanations_distinguish_queue_eligibility_and_missing_forecasts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = inputs(tmp_path, monkeypatch)
    reference, _ = predict(paths)
    assert reference.selection_reason.tolist() == [
        "Outside frozen threshold or origin cap: probability_reference",
        "Frozen research selection: probability_reference",
        "Origin ineligible: missing_aip",
    ]
    assert not reference.policy_eligible.iloc[1] and reference.selected.iloc[1]
    downside, _ = predict(paths, accepted=True)
    assert downside.selection_reason.tolist() == [
        "Frozen research selection: downside_aware",
        "Excluded by frozen recommendation policy: excluded_frozen_development_behaviour_policy",
        "Origin ineligible: missing_aip",
    ]

    def missing_mean(values: pd.DataFrame, bundle: dict, **kwargs: Any) -> dict[str, np.ndarray]:
        result = raw(values, bundle, **kwargs)
        result["mean_current"][0] = np.nan
        return result

    monkeypatch.setattr(module, "_raw", missing_mean)
    unavailable, _ = predict(paths, accepted=True)
    assert unavailable.selection_reason.iloc[0] == (
        "Prediction unavailable for frozen policy: downside_aware"
    )
    assert not unavailable.selected.iloc[0] and unavailable["rank"].iloc[0] == 0
    assert unavailable.policy_eligible.iloc[0]


def test_only_usable_advance_codes_switch_all_four_prediction_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = inputs(tmp_path, monkeypatch)
    f = frame().assign(advance_abc=["C", "invalid", None])
    result, _ = predict(paths, f=f)
    assert result.p_rise.iloc[0] == pytest.approx(0.05)
    assert result.p_rise.iloc[1] == pytest.approx(0.90)
    assert result.forecast_delta_dkk.iloc[:2].tolist() == [200.0, 30.0]
    assert result.median_delta_dkk.iloc[:2].tolist() == [50.0, 2.0]
    assert result.experimental_lower_delta_dkk.iloc[:2].tolist() == [-50.0, -20.0]
    assert result.experimental_upper_delta_dkk.iloc[:2].tolist() == [200.0, 40.0]
    assert result.accepted_lower_delta_dkk.iloc[:2].tolist() == [40.0, -8.0]
    assert result.accepted_upper_delta_dkk.iloc[:2].tolist() == [60.0, 12.0]
    assert result.persistence_probability.iloc[:2].tolist() == pytest.approx([0.9, 0.6])
    with pytest.raises(ResearchError, match="outcomes_in_candidate_inputs"):
        predict(paths, f=f.assign(next_aip_ore=123456))


def test_all_missing_advance_uses_current_temperature_and_mixed_uses_hybrid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = inputs(tmp_path, monkeypatch)
    state = read(paths["calibration"])
    state["temperature"]["current"]["temperature"] = 2.0
    state["temperature"]["hybrid"]["temperature"] = 0.5
    paths["calibration"].write_text(json.dumps(state), encoding="utf-8")
    current, _ = predict(paths)
    assert current.calibration_route.iloc[:2].eq("current").all()
    assert current.p_rise.iloc[:2].equals(current.current_only_p_rise.iloc[:2])
    assert current.p_fall.iloc[:2].equals(current.current_only_p_fall.iloc[:2])
    mixed, _ = predict(paths, f=frame().assign(advance_abc=["C", None, None]))
    assert mixed.calibration_route.iloc[:2].eq("hybrid").all()
    assert mixed.p_rise.iloc[1] != mixed.current_only_p_rise.iloc[1]


def test_raw_mean_support_floor_and_future_input_alignment_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = inputs(tmp_path, monkeypatch)

    def negative_raw(values: pd.DataFrame, bundle: dict, **kwargs: Any) -> dict[str, np.ndarray]:
        result = raw(values, bundle, **kwargs)
        result["mean_current"][:] = -150
        return result

    monkeypatch.setattr(module, "_raw", negative_raw)
    result, _ = predict(paths)
    assert result.raw_forecast_delta_dkk.iloc[0] == -150
    assert result.forecast_delta_dkk.iloc[0] == -100 and result.forecast_aip_dkk.iloc[0] == 0
    with pytest.raises(ResearchError, match="incomplete_identity_alignment"):
        module._aligned(frame(), frame().iloc[:1])
    bad = read(paths["calibration"])
    bad["temperature"]["hybrid"]["last_maturity"] = module.ORIGIN
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ResearchError, match="calibration_maturity_boundary"):
        module.predict(
            frame(),
            paths["bundle"],
            tmp_path / "bad.json",
            paths["reference"],
            paths["population"],
            interval_adopted=False,
        )


def root_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = inputs(tmp_path, monkeypatch)
    (tmp_path / "final-assessment").mkdir()
    (tmp_path / "inputs").mkdir()
    frozen_json(tmp_path / "inputs/manifest.json", {"files": {"current": pin(paths["features"])}})
    frozen_json(
        tmp_path / "old-assessment.json",
        {"inputs": pin(paths["features"]), "predictions": pin(paths["reference"])},
    )
    frozen_json(
        tmp_path / "lifecycle.json",
        {
            "assessments": [
                {"origin": module.ORIGIN, "assessment": pin(tmp_path / "old-assessment.json")}
            ]
        },
    )
    frozen_json(tmp_path / "selection.json", {"persistence_policy_enabled": True})
    frozen_json(
        tmp_path / "results.json", {"selected_configuration": {"quantile_interval_accepted": False}}
    )
    (tmp_path / "population").mkdir()
    frozen_json(tmp_path / "population/report.json", {"artifacts": [pin(paths["population"])]})
    monkeypatch.setattr(
        module, "require", lambda root: {"pins": {"e4_lifecycle": pin(tmp_path / "lifecycle.json")}}
    )
    monkeypatch.setattr(module, "model_bundle", lambda root, **kwargs: paths["bundle"])
    monkeypatch.setattr(module, "calibration", lambda root, bundle: paths["calibration"])

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 10, 27, 12, tzinfo=UTC)

    monkeypatch.setattr("bef_redbird.research.e4_assessment.datetime", FrozenDateTime)
    monkeypatch.setattr(module, "utc", lambda: "2026-10-27T12:00:00+00:00")
    return paths


def test_immutable_prepare_exact_replay_and_separate_pending_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = root_fixture(tmp_path, monkeypatch)
    path = module.prepare(tmp_path)
    saved = read(path)
    original = Path(saved["predictions"]["path"]).read_bytes()
    assert saved["candidate_count"] == 3 and saved["predicted"] == 2
    assert saved["context"]["mode"] == "prospective_pending"
    assert module.prepare(tmp_path) == path
    assert module.replay(path)["exact_predictions"]
    outcome = read(Path(read(tmp_path / "final-assessment.json")["outcome"]["path"]))
    assert outcome["states"] == {"pending_target_observation": 2, "origin_ineligible": 1}
    assert Path(saved["predictions"]["path"]).read_bytes() == original
    assert paths["features"].read_bytes() == Path(saved["inputs"]["path"]).read_bytes()
    with pytest.raises(ResearchError, match="changed_adoption_requires_new_version"):
        module.prepare(tmp_path, interval_adopted=True)


def test_new_prepare_cannot_claim_future_after_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "utc", lambda: module.TARGET + "T12:00:00+00:00")
    with pytest.raises(ResearchError, match="target_not_future"):
        module.prepare(tmp_path)


def test_final_calibration_strict_maturity_and_four_fit_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = inputs(tmp_path, monkeypatch)
    from bef_redbird.research.e3_methods import temperature_fit
    from bef_redbird.research.e5_results import binary_temperature

    calls = []

    def checkpoint(
        root: Path,
        fold: str,
        key: str,
        data: pd.DataFrame,
        probability: np.ndarray,
        cutoff: str,
        *,
        binary: bool = False,
    ) -> dict:
        calls.append((fold, key))
        return (
            binary_temperature(data, probability, cutoff)
            if binary
            else temperature_fit(
                data.assign(p0=probability[:, 0], p1=probability[:, 1], p2=probability[:, 2]),
                cutoff,
            )
        )

    monkeypatch.setattr(module, "temperature_once", checkpoint)
    (tmp_path / "final-assessment").mkdir()
    (tmp_path / "inputs").mkdir()
    pieces = []
    for index, date in enumerate(("2026-09-01", "2026-09-15", "2026-10-12")):
        part = frame().iloc[:2].copy()
        part["row_key"] = [f"a{index}", f"b{index}"]
        part["period_start"] = pd.Timestamp(date)
        part["period_index"] = index
        pieces.append(part)
    features = pd.concat(pieces, ignore_index=True)
    labels = features[
        ["row_key", "package_number", "period_start", "period_index", "identity_episode_id"]
    ].copy()
    labels["next_status"] = "observed"
    labels["next_direction"] = [1, 0, 1, 0, -1, -1]
    labels["next_maturity_date"] = pd.to_datetime(
        ["2026-09-15"] * 2 + ["2026-09-29"] * 2 + [module.ORIGIN] * 2
    )
    labels["second_status"] = "observed"
    labels["second_maturity_date"] = pd.to_datetime(
        ["2026-09-29"] * 2 + ["2026-10-13"] * 2 + [module.TARGET] * 2
    )
    labels["persistence_target"] = [1, 0, 1, 0, 0, 0]
    features.to_parquet(tmp_path / "history.parquet", index=False)
    labels.to_parquet(tmp_path / "labels.parquet", index=False)
    frozen_json(
        tmp_path / "inputs/manifest.json",
        {
            "files": {
                "features": pin(tmp_path / "history.parquet"),
                "outcomes": pin(tmp_path / "labels.parquet"),
            }
        },
    )
    path = module.calibration(tmp_path, paths["bundle"])
    saved = read(path)
    assert saved["temperature_fits"] == 4
    assert calls == [
        ("final", "direction_current"),
        ("final", "direction_hybrid"),
        ("final", "persistence_current"),
        ("final", "persistence_advance"),
    ]
    assert all(
        x["rows"] == 4 and x["last_maturity"] == "2026-09-29" for x in saved["temperature"].values()
    )
    assert all(
        x["rows"] == 4 and x["last_maturity"] == "2026-10-13" for x in saved["persistence"].values()
    )
    assert module.calibration(tmp_path, paths["bundle"]) == path
    assert len(calls) == 4


def test_replay_relocates_nested_references_without_original_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original"
    restored = tmp_path / "restored"
    hidden = tmp_path / "hidden-original"
    original.mkdir()
    root_fixture(original, monkeypatch)
    assessment = module.prepare(original)
    shutil.copytree(original, restored)
    # Both endpoints remain within this test's resolved disposable directory.
    assert original.resolve().is_relative_to(tmp_path.resolve())
    assert hidden.resolve().is_relative_to(tmp_path.resolve())
    original.rename(hidden)
    relocated = restored / assessment.relative_to(original)
    proof = module.replay(relocated, relocations={original: restored})
    assert proof["exact_predictions"] and proof["exact_queues"]
    assert not original.exists()


def fallback_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = inputs(tmp_path, monkeypatch)
    base = {
        "model": pin(tmp_path / "model.bin"),
        "trial": pin(tmp_path / "trial.json"),
        "fit_cutoff": "2026-01-01",
        "latest_label_maturity": "2025-12-22",
    }
    models = {}
    for mode in ("current", "advance"):
        schema = {"features": ["aip_ore"], "categories": []}
        if mode == "advance":
            schema = {"features": ["aip_ore", "advance_abc"], "categories": ["advance_abc"]}
        for stem in ("cb_package", "cb_equal", "cb_broad", "amount_delta_floor"):
            models[f"{stem}_{mode}"] = {**base, **schema}
    frozen_json(
        tmp_path / "accepted.json",
        {"models": models, "amount_interval_calibration": pin(tmp_path / "residual.json")},
    )
    protocol = {
        "pins": {"e4_model-bundle": pin(tmp_path / "accepted.json")},
        "settings": {"iterations": 160, "depth": 6, "learning_rate": 0.08, "l2_leaf_reg": 5.0},
        "direction": {
            "tuning": {"shallow": {"depth": 4, "learning_rate": 0.05, "l2_leaf_reg": 10.0}}
        },
        "resource_limits": {"fit_seconds": 1500, "process_rss_bytes": 24 * 1024**3},
    }
    if (tmp_path / "lifecycle.json").exists():
        protocol["pins"]["e4_lifecycle"] = pin(tmp_path / "lifecycle.json")
    frozen_json(tmp_path / "protocol.json", protocol)
    monkeypatch.setattr(module, "require", lambda root: protocol)
    (tmp_path / "selection.json").write_text(
        json.dumps(
            {
                "direction": {"method": "shallow", "iterations": {"current": 2, "advance": 2}},
                "persistence_policy_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    if not (tmp_path / "results.json").exists():
        frozen_json(
            tmp_path / "results.json",
            {"selected_configuration": {"quantile_interval_accepted": False}},
        )
    producer = {"archive": pin(tmp_path / "bundle.json")}
    pair = {}
    common = {
        "status": "completed",
        "protocol": pin(tmp_path / "protocol.json"),
        "inputs": pin(paths["features"]),
        "producer": producer,
        "params": {
            "iterations": 2,
            "depth": 4,
            "learning_rate": 0.05,
            "l2_leaf_reg": 10.0,
            "loss_function": "MultiClass",
            "classes_count": 3,
        },
        "latest_label_maturity": "2026-06-22",
        "training_start": "2023-07-01",
        "trees": 2,
        "exact_reload": True,
        "files": {"model.cbm": base["model"], "predictions.parquet": pin(paths["features"])},
    }
    for mode in ("current", "advance"):
        name = f"summer-tuned-{mode}"
        directory = tmp_path / "trials" / name
        directory.mkdir(parents=True)
        schema = models[f"cb_broad_{mode}"]
        receipt = {
            **common,
            "features": schema["features"],
            "categories": schema["categories"],
            "spec": {
                "id": name,
                "fold": "summer",
                "method": "tuned",
                "task": "direction",
                "feature_route": "broad",
                "information": mode,
                "fit_cutoff": "2026-07-01",
            },
        }
        frozen_json(directory / "receipt.json", receipt)
        pair[mode] = pin(directory / "receipt.json")
    failure = tmp_path / "trials/final-selected_direction-current/receipt.json"
    failure.parent.mkdir()
    frozen_json(
        failure,
        {
            **common,
            "status": "failed",
            "error": "e5_declared_fit_resource_limit",
            "latest_label_maturity": "2026-08-31",
            "training_start": "2023-09-01",
            "features": models["cb_broad_current"]["features"],
            "categories": [],
            "resource": {"elapsed_seconds": 1501, "sampled_peak_rss_bytes": 100},
            "spec": {
                "id": "final-selected_direction-current",
                "fold": "final",
                "method": "selected_direction",
                "task": "direction",
                "feature_route": "broad",
                "information": "current",
                "fit_cutoff": module.FIT,
            },
        },
    )
    decision = {
        "version": "e5-final-artifact-resource-decision-v1",
        "direction_fit_cutoff": "2026-07-01",
        "amount_fit_cutoff": module.FIT,
        "reused_e4_component_fit_cutoff": "2026-01-01",
        "calibration_block_start": module.FIT,
        "calibration_end_exclusive": module.ORIGIN,
        "base_fit_limits_unchanged": True,
        "reserve_attempts_used": 0,
        "no_operational_promotion": True,
        "skipped_trials": ["final-selected_direction-advance"],
        "direction_receipts": pair,
        "failed_attempt": pin(failure),
        "protocol": pin(tmp_path / "protocol.json"),
        "development_selection": pin(tmp_path / "selection.json"),
        "scientific_results": pin(tmp_path / "results.json"),
    }
    frozen_json(tmp_path / "final-artifact-decision.json", decision)
    for method in ("rmse", "quantile"):
        for mode in ("current", "advance"):
            directory = tmp_path / "trials" / f"final-{method}-{mode}"
            directory.mkdir()
            frozen_json(
                directory / "receipt.json",
                {
                    **common,
                    "latest_label_maturity": "2026-08-31",
                    "features": ["aip_ore"],
                    "categories": [],
                    "training_start": "2023-09-01",
                    "spec": {"fit_cutoff": module.FIT},
                },
            )
    return paths


def test_resource_fallback_pins_actual_july_pair_and_september_amounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback_inputs(tmp_path, monkeypatch)
    path = module.model_bundle(tmp_path, persistence_enabled=False)
    saved = read(path)
    assert saved["direction_fit_cutoff"] == "2026-07-01"
    assert saved["amount_fit_cutoff"] == saved["planned_final_fit_cutoff"] == module.FIT
    assert saved["component_fit_cutoffs"]["cb_package_current"] == "2026-01-01"
    assert "cb_broad_current" not in saved["final_trials"]
    assert (
        saved["models"]["cb_broad_current"]["trial"]
        == read(tmp_path / "final-artifact-decision.json")["direction_receipts"]["current"]
    )
    assert not (tmp_path / "trials/final-selected_direction-advance").exists()
    assert module.model_bundle(tmp_path, persistence_enabled=False) == path


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "completed"),
        ("error", "other_failure"),
        ("resource", {"elapsed_seconds": 1499, "sampled_peak_rss_bytes": 100}),
    ],
)
def test_resource_fallback_rejects_nonresource_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    fallback_inputs(tmp_path, monkeypatch)
    decision_path = tmp_path / "final-artifact-decision.json"
    decision = read(decision_path)
    receipt_path = Path(decision["failed_attempt"]["path"])
    failed = read(receipt_path)
    failed[field] = value
    receipt_path.write_text(json.dumps(failed), encoding="utf-8")
    decision["failed_attempt"] = pin(receipt_path)
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    with pytest.raises(ResearchError, match="requires_declared_failure"):
        module.model_bundle(tmp_path, persistence_enabled=False)
    assert not (tmp_path / "final-assessment/model-bundle.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("latest_label_maturity", "2026-07-01"),
        ("status", "failed"),
        ("features", ["aip_ore", "next_delta_ore"]),
        ("params", {"iterations": 480}),
    ],
)
def test_resource_fallback_rejects_wrong_settings_schema_or_maturity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    fallback_inputs(tmp_path, monkeypatch)
    path = tmp_path / "final-artifact-decision.json"
    decision = read(path)
    receipt_path = Path(decision["direction_receipts"]["advance"]["path"])
    receipt = read(receipt_path)
    receipt[field] = value
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    decision["direction_receipts"]["advance"] = pin(receipt_path)
    path.write_text(json.dumps(decision), encoding="utf-8")
    with pytest.raises(ResearchError, match="incompatible_resource_fallback_pair"):
        module.model_bundle(tmp_path, persistence_enabled=False)


def test_without_explicit_decision_failed_september_fit_remains_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback_inputs(tmp_path, monkeypatch)
    decision = tmp_path / "final-artifact-decision.json"
    decision.rename(tmp_path / "not-an-active-decision.json")
    with pytest.raises(ResearchError, match="incompatible_final_fit"):
        module.model_bundle(tmp_path, persistence_enabled=False)


def test_resource_fallback_metadata_and_exact_replay_survive_relocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "o"
    original.mkdir()
    build_bundle = module.model_bundle
    root_fixture(original, monkeypatch)
    paths = fallback_inputs(original, monkeypatch)
    bundle_path = build_bundle(original, persistence_enabled=False)
    calibration = read(paths["calibration"])
    calibration["models"] = pin(bundle_path)
    paths["calibration"].write_text(json.dumps(calibration), encoding="utf-8")
    monkeypatch.setattr(module, "model_bundle", lambda root, **kwargs: bundle_path)
    assessment_path = module.prepare(original)
    saved = read(assessment_path)
    assert saved["fit_cutoff"] == "2026-07-01" and saved["amount_fit_cutoff"] == module.FIT
    assert "before 2026-07-01" in saved["training_scope"]
    restored, hidden = tmp_path / "r", tmp_path / "h"
    shutil.copytree(original, restored)
    assert hidden.resolve().is_relative_to(tmp_path.resolve())
    original.rename(hidden)
    proof = module.replay(
        restored / assessment_path.relative_to(original), relocations={original: restored}
    )
    assert proof["exact_predictions"] and proof["exact_queues"]
