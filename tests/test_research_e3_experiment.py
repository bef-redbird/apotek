"""Bounded E3 runner/protocol regressions using tiny synthetic data and model doubles."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from bef_redbird.research import e3_experiment as experiment
from bef_redbird.research.dataset import ResearchError, digest
from bef_redbird.research.e3_data import SCENARIOS
from bef_redbird.research.group_features import pin
from bef_redbird.research.training_contract import KEYS, atomic_json


def features(day: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_key": [f"{day}-{i}" for i in range(3)],
            "package_number": ["000001", "000002", "000003"],
            "period_index": 1,
            "period_start": pd.Timestamp(day),
            "identity_episode_id": ["one", "two", "three"],
            "aip_ore": [10000, 5000, 2000],
            "abc": ["A", "B", "C"],
            "advance_abc": ["B", "C", "A"],
            "advance_state": "observed",
            "origin_group": "g",
        }
    )


def labels(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame[list(KEYS)].copy()
    out["next_status"] = "observed"
    out["next_direction"] = [-1, 0, 1]
    out["next_aip_ore"] = frame.aip_ore + [-100, 0, 100]
    out["next_delta_ore"] = [-100, 0, 100]
    out["next_maturity_date"] = pd.to_datetime(
        [date.fromisoformat(str(day)[:10]) + timedelta(days=14) for day in out.period_start]
    )
    return out


class TinyData:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.training_override: pd.DataFrame | None = None

    def candidates(self, start: str, end: str) -> pd.DataFrame:
        self.events.append(("candidates", start, end))
        return features(start)

    def training(self, start: str, end: str) -> pd.DataFrame:
        self.events.append(("training", start, end))
        if self.training_override is not None:
            return self.training_override
        frame = features(start)
        return frame.merge(labels(frame), on=list(KEYS), validate="one_to_one")

    def outcomes(self, start: str, end: str) -> pd.DataFrame:
        raise AssertionError("trial_run must not access scored-cohort outcomes")


class TinyResource:
    def __init__(self) -> None:
        self.started = time.monotonic()

    def finish(self) -> dict[str, Any]:
        return {"elapsed_seconds": 0.01, "sampled_peak_rss_bytes": 1000}


class TinyClassifier:
    fits = 0

    def __init__(self, **settings: Any) -> None:
        self.tree_count_ = 1

    def fit(self, frame: pd.DataFrame, y: np.ndarray, **kwargs: Any) -> None:
        type(self).fits += 1
        assert len(frame) == len(y)

    def save_model(self, path: str) -> None:
        Path(path).write_text('{"synthetic_model":true}')

    def load_model(self, path: str) -> None:
        assert json.loads(Path(path).read_text())["synthetic_model"] is True

    def predict_proba(self, frame: pd.DataFrame, **kwargs: Any) -> np.ndarray:
        codes = frame.advance_abc.astype(str)
        rise = np.where(codes.eq("V:A"), 0.7, np.where(codes.eq("M:"), 0.1, 0.3))
        return np.column_stack([np.repeat(0.1, len(frame)), 0.9 - rise, rise])


class TinyRegressor(TinyClassifier):
    def predict(self, frame: pd.DataFrame, **kwargs: Any) -> np.ndarray:
        return np.zeros(len(frame))


@pytest.fixture
def runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any], TinyData]:
    root = tmp_path / "run"
    root.mkdir()
    atomic_json(root / "protocol.json", {"fixture": "synthetic immutable protocol"})
    monkeypatch.setattr(experiment, "ResourceSample", TinyResource)
    monkeypatch.setattr(experiment, "CatBoostClassifier", TinyClassifier)
    monkeypatch.setattr(experiment, "CatBoostRegressor", TinyRegressor)
    monkeypatch.setattr(experiment.gc, "collect", lambda: None)
    TinyClassifier.fits = TinyRegressor.fits = 0
    return root, {"settings": {}, "pins": {}}, TinyData()


def advance_spec(*, amount: bool = False) -> dict[str, Any]:
    return {
        "method": "amount_log_ratio_advance" if amount else "cb_equal_advance",
        "algorithm": "catboost",
        "peer": "equal",
        "advance": True,
        "features": ["aip_ore", "advance_abc"],
        "categories": ["advance_abc"],
        **({"formulation": "log_ratio"} if amount else {}),
    }


def test_declared_fit_matrix_is_exactly_bounded_and_reuses_only_current_e2() -> None:
    assert len(experiment.FOLDS) == 4 and len(experiment.DIRECTION) == 8
    direction_new = sum(
        experiment.cached_id(fold, spec, "direction") is None
        for fold in experiment.FOLDS
        for spec in experiment.DIRECTION
    )
    assert direction_new == 22
    for formulation, expected in [("delta_floor", 29), ("log_ratio", 32)]:
        amount_new = sum(
            experiment.cached_id(
                fold, {"advance": advance, "formulation": formulation}, "magnitude"
            )
            is None
            for fold in experiment.FOLDS[1:]
            for advance in [False, True]
        )
        assert direction_new + 4 + amount_new == expected
    for fold in experiment.FOLDS:
        for spec in experiment.DIRECTION:
            if spec["advance"] or spec["peer"] == "broad":
                assert experiment.cached_id(fold, spec, "direction") is None


def test_only_next_abc_code_is_a_future_predictor_and_identity_state_stays_audit_only() -> None:
    equal = {"feature_names": ["peer_count"], "categorical_names": ["peer_feature_state"]}
    broad = {"feature_names": ["broad_count"], "categorical_names": ["broad_state"]}
    for spec in experiment.DIRECTION:
        features, categories = experiment.feature_names(spec, equal, broad)
        assert "advance_state" not in features and "advance_state" not in categories
        assert "advance_source" not in features and "origin_identity_token" not in features
        assert ("advance_abc" in features) is spec["advance"]
        assert ("advance_abc" in categories) is spec["advance"]


@pytest.mark.parametrize("task", ["direction", "magnitude"])
def test_trial_freezes_all_scenarios_without_accessing_scored_outcomes(
    runner: tuple[Path, dict[str, Any], TinyData], task: str
) -> None:
    root, protocol, data = runner
    spec = advance_spec(amount=task == "magnitude")
    receipt = experiment.trial_run(data, protocol, root, experiment.FOLDS[0], spec, task=task)
    assert receipt["status"] == "completed", receipt.get("error")
    assert receipt["new_fit"] and receipt["fit_cutoff"] == "2022-01-01"
    assert receipt["latest_training_label_maturity"] < receipt["fit_cutoff"]
    assert data.events[1] == ("training", "2019-01-01", "2022-01-01")
    path = root / "trials" / receipt["trial_id"] / "predictions.parquet"
    saved = pd.read_parquet(path)
    prefix = "next_aip_dkk" if task == "magnitude" else "p2"
    assert all(f"{prefix}_{scenario}" in saved for scenario in SCENARIOS)
    assert not {"next_status", "next_direction", "next_aip_ore"} & set(saved)
    assert saved.row_key.tolist() == features("2022-01-01").row_key.tolist()
    if task == "direction":
        assert not saved.p2_perfect.equals(saved.p2_missing100)
    before = path.read_bytes()
    previous_fits = TinyClassifier.fits + TinyRegressor.fits
    assert (
        experiment.trial_run(data, protocol, root, experiment.FOLDS[0], spec, task=task) == receipt
    )
    assert path.read_bytes() == before
    assert TinyClassifier.fits + TinyRegressor.fits == previous_fits


def test_immature_training_label_fails_before_fit(
    runner: tuple[Path, dict[str, Any], TinyData],
) -> None:
    root, protocol, data = runner
    frame = features("2021-12-20")
    data.training_override = frame.merge(labels(frame), on=list(KEYS))
    data.training_override["next_maturity_date"] = pd.Timestamp("2022-01-01")
    receipt = experiment.trial_run(
        data, protocol, root, experiment.FOLDS[0], advance_spec(), task="direction"
    )
    assert receipt["status"] == "failed" and receipt["error"] == "e3_immature_training_labels"
    assert TinyClassifier.fits == 0
    assert not (root / "trials" / receipt["trial_id"] / "predictions.parquet").exists()


def test_fit_failure_is_retained_and_never_automatically_retried(
    runner: tuple[Path, dict[str, Any], TinyData], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, protocol, data = runner

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ResearchError("synthetic_limit")

    monkeypatch.setattr(TinyClassifier, "fit", fail)
    receipt = experiment.trial_run(
        data, protocol, root, experiment.FOLDS[0], advance_spec(), task="direction"
    )
    assert receipt["status"] == "failed" and receipt["error"] == "synthetic_limit"
    assert receipt["new_fit"] is True and receipt["resource"]
    assert (
        experiment.trial_run(
            data, protocol, root, experiment.FOLDS[0], advance_spec(), task="direction"
        )
        == receipt
    )


@pytest.mark.parametrize("mutation", ["running", "protocol", "file"])
def test_unsafe_or_modified_trial_resume_is_refused(
    runner: tuple[Path, dict[str, Any], TinyData], mutation: str
) -> None:
    root, protocol, data = runner
    receipt = experiment.trial_run(
        data, protocol, root, experiment.FOLDS[0], advance_spec(), task="direction"
    )
    path = root / "trials" / receipt["trial_id"]
    if mutation == "file":
        (path / "predictions.parquet").write_bytes(b"changed")
    elif mutation == "protocol":
        atomic_json(root / "protocol.json", {"changed": True})
    else:
        receipt["status"] = "running"
        atomic_json(path / "trial.json", receipt)
    with pytest.raises(ResearchError):
        experiment.trial_run(
            data, protocol, root, experiment.FOLDS[0], advance_spec(), task="direction"
        )


def cached_fixture(
    root: Path, candidates: pd.DataFrame, *, task: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    fold = experiment.FOLDS[1]
    spec = {
        "method": "cb_equal_current" if task == "direction" else "amount_delta_floor_current",
        "peer": "equal",
        "algorithm": "catboost",
        "advance": False,
        "formulation": "delta_floor",
    }
    source_id = experiment.cached_id(fold, spec, task)
    old = root.parent / "e2"
    old.mkdir(exist_ok=True)
    atomic_json(old / "protocol.json", {"fixture": "accepted E2"})
    trial = old / "trials" / str(source_id)
    trial.mkdir(parents=True)
    cached = candidates[[*KEYS, "aip_ore"]].copy()
    if task == "direction":
        cached[["p0", "p1", "p2"]] = [0.1, 0.7, 0.2]
    else:
        cached["prediction_dkk"] = [-1000.0, 1.0, 0.0]
    path = trial / "predictions.parquet"
    cached.to_parquet(path, index=False)
    atomic_json(
        old / "trial-ledger.json",
        {
            "trials": [
                {
                    "trial_id": source_id,
                    "status": "completed",
                    "history": 3,
                    "fit_cutoff": fold["cutoff"],
                    "training_rows": 100,
                    "latest_training_label_maturity": "2023-12-18",
                    "files": [{**pin(path), "role": "predictions"}],
                }
            ]
        },
    )
    return {"settings": {}, "pins": {"e2_protocol": pin(old / "protocol.json")}}, spec


@pytest.mark.parametrize("task", ["direction", "magnitude"])
def test_cached_e2_predictions_are_reused_without_fit_and_money_is_constrained(
    runner: tuple[Path, dict[str, Any], TinyData], task: str
) -> None:
    root, _, data = runner
    protocol, spec = cached_fixture(root, features("2024-01-01"), task=task)
    receipt = experiment.trial_run(data, protocol, root, experiment.FOLDS[1], spec, task=task)
    assert receipt["status"] == "completed", receipt.get("error")
    assert receipt["new_fit"] is False
    assert not any(event[0] == "training" for event in data.events)
    assert TinyClassifier.fits + TinyRegressor.fits == 0
    saved = pd.read_parquet(root / "trials" / receipt["trial_id"] / "predictions.parquet")
    if task == "magnitude":
        assert saved.next_aip_dkk_perfect.tolist() == [0.0, 51.0, 20.0]


@pytest.mark.parametrize("mutation", ["package", "date", "amount", "order"])
def test_cached_e2_mismatched_candidate_evidence_is_rejected(
    runner: tuple[Path, dict[str, Any], TinyData], mutation: str
) -> None:
    root, _, data = runner
    cached = features("2024-01-01")
    if mutation == "package":
        cached.loc[0, "package_number"] = "999999"
    elif mutation == "date":
        cached.loc[0, "period_start"] = pd.Timestamp("2024-01-02")
    elif mutation == "amount":
        cached.loc[0, "aip_ore"] += 1
    else:
        cached = cached.iloc[::-1].reset_index(drop=True)
    protocol, spec = cached_fixture(root, cached, task="direction")
    receipt = experiment.trial_run(
        data, protocol, root, experiment.FOLDS[1], spec, task="direction"
    )
    assert receipt["status"] == "failed" and "candidate_contract_mismatch" in receipt["error"]


def test_protocol_freeze_pins_budget_before_any_e3_comparative_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    e2 = data_root / "research-e2"
    e2.mkdir(parents=True)
    bundle = data_root / "dataset.json"
    bundle.write_text("{}")
    atomic_json(
        data_root / "calendar.json",
        {
            "periods": [
                {"start": "2022-01-03", "end_exclusive": "2022-01-17"},
                {"start": "2024-01-08", "end_exclusive": "2024-01-22"},
            ]
        },
    )
    group = data_root / "groups.json"
    atomic_json(group, {"feature_names": ["equal_peer_count"], "categorical_names": []})
    root = data_root / "research-e3/run-v1"
    for folder in ("advance-v2", "peers-v1"):
        target = (
            root.parent
            / folder
            / ("manifest-model-v2.json" if folder == "advance-v2" else "manifest.json")
        )
        target.parent.mkdir(parents=True)
        atomic_json(target, {"feature_names": ["broad_peer_count"], "categorical_names": []})
    atomic_json(
        e2 / "protocol.json", {"dataset": pin(bundle), "groups": pin(group), "dependencies": {}}
    )
    atomic_json(e2 / "trial-ledger.json", {"trials": []})
    atomic_json(e2 / "display.json", {"fixture": "accepted E2 display"})
    atomic_json(
        e2 / "current.json",
        {"protocol": pin(e2 / "protocol.json"), "display_result": pin(e2 / "display.json")},
    )
    monkeypatch.setattr(experiment, "verify_training_pointer", lambda pointer: (bundle, {}))
    experiment.freeze(root, data_root)
    protocol = json.loads((root / "protocol.json").read_text())
    receipt = json.loads((root / "freeze-receipt.json").read_text())
    assert receipt["comparative_e3_scores_inspected"] is False
    assert receipt["protocol"]["sha256"] == digest(root / "protocol.json")
    assert protocol["budget"]["maximum_main_fits"] == 32
    assert protocol["budget"]["direction_new_fits"] == 22
    assert len(protocol["direction"]) == 8
    assert protocol["exact_origins"]["evaluation_2024"][0]["start"] == "2024-01-08"
    assert "T+2" in protocol["information_boundary"]
    assert (root / "producer.zip").is_file()
    with pytest.raises(ResearchError, match="unused_run"):
        experiment.freeze(root, data_root)


def save_cohort(root: Path, *, fold: str = "validation") -> Path:
    path = root / "cohorts" / fold
    path.mkdir(parents=True)
    frame = features("2022-01-01")
    frame.to_parquet(path / "features.parquet", index=False)
    labels(frame).to_parquet(path / "outcomes.parquet", index=False)
    atomic_json(
        path / "manifest.json",
        {"features": pin(path / "features.parquet"), "outcomes": pin(path / "outcomes.parquet")},
    )
    return path


def test_prediction_reader_keeps_future_outcomes_separate_until_explicit_attachment(
    runner: tuple[Path, dict[str, Any], TinyData],
) -> None:
    root, protocol, data = runner
    receipt = experiment.trial_run(
        data, protocol, root, experiment.FOLDS[0], advance_spec(), task="direction"
    )
    assert receipt["status"] == "completed"
    save_cohort(root)
    frame = experiment.read_prediction(root, "validation", "cb_equal_advance")
    assert "next_status" not in frame and "next_direction" not in frame
    attached = experiment.read_prediction(root, "validation", "cb_equal_advance", outcomes=True)
    assert attached.next_direction.tolist() == [-1, 0, 1]
    assert_frame_equal(frame, attached[frame.columns])


@pytest.mark.parametrize(
    "mutation", ["unknown_scenario", "missing_scenario", "corrupt_prediction", "corrupt_cohort"]
)
def test_prediction_reader_rejects_missing_scenarios_and_changed_saved_bytes(
    runner: tuple[Path, dict[str, Any], TinyData], mutation: str
) -> None:
    root, protocol, data = runner
    receipt = experiment.trial_run(
        data, protocol, root, experiment.FOLDS[0], advance_spec(), task="direction"
    )
    cohort = save_cohort(root)
    trial = root / "trials" / receipt["trial_id"]
    scenario = "missing100"
    if mutation == "unknown_scenario":
        scenario = "typo"
    elif mutation in {"missing_scenario", "corrupt_prediction"}:
        path = trial / "predictions.parquet"
        value = pd.read_parquet(path)
        if mutation == "missing_scenario":
            value = value.drop(columns=["p2_missing100"])
        else:
            value.loc[0, "p2_perfect"] = 0.999
        value.to_parquet(path, index=False)
        if mutation == "missing_scenario":
            next(item for item in receipt["files"] if item["path"] == path.name).update(
                pin(path), path=path.name
            )
            atomic_json(trial / "trial.json", receipt)
    else:
        path = cohort / "features.parquet"
        value = pd.read_parquet(path)
        value.loc[0, "aip_ore"] += 1
        value.to_parquet(path, index=False)
    with pytest.raises(ResearchError):
        experiment.read_prediction(root, "validation", "cb_equal_advance", scenario)


def mock_full_runner(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """Exercise orchestration with frozen prediction stubs; never train a real model."""
    events: list[tuple[str, str]] = []
    marker = root / "input.json"
    atomic_json(marker, {"fixture": "pinned synthetic input"})
    protocol = {
        "pins": {key: pin(marker) for key in ("dataset", "groups", "advance", "broader")},
        "code": {},
        "settings": {},
        "direction": [
            {**spec, "features": ["aip_ore"], "categories": []} for spec in experiment.DIRECTION
        ],
        "budget": {"overall_seconds": 18000},
    }
    atomic_json(root / "protocol.json", protocol)
    atomic_json(root / "freeze-receipt.json", {"protocol": pin(root / "protocol.json")})

    class FullData:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def candidates(self, start: str, end: str) -> pd.DataFrame:
            return features(start)

        def outcomes(self, start: str, end: str) -> pd.DataFrame:
            fold = next(fold for fold in experiment.FOLDS if fold["start"] == start)
            paths = list((root / "trials").glob(f"{fold['name']}-*/predictions.parquet"))
            assert len(paths) == (12 if fold["name"] == "validation" else 10)
            events.append(("outcomes", fold["name"]))
            return labels(features(start))

        def close(self) -> None:
            events.append(("closed", "data"))

    def freeze_prediction(
        data: Any,
        protocol: Any,
        root: Path,
        fold: dict[str, Any],
        spec: dict[str, Any],
        *,
        task: str,
    ) -> dict[str, Any]:
        path = root / "trials" / f"{fold['name']}-{spec['method']}"
        path.mkdir(parents=True, exist_ok=True)
        features(fold["start"])[["row_key"]].to_parquet(path / "predictions.parquet", index=False)
        events.append(("prediction", fold["name"]))
        return {
            "trial_id": path.name,
            "status": "completed",
            "new_fit": experiment.cached_id(fold, spec, task) is None,
        }

    def choose(root: Path) -> dict[str, Any]:
        events.append(("selection", "validation"))
        assert (root / "cohorts/validation/outcomes.parquet").exists()
        assert not (root / "cohorts/evaluation_2024").exists()
        value = {
            "monetary_formulation": "delta_floor",
            "selected_before_evaluation": True,
            "protocol_sha256": digest(root / "protocol.json"),
            "main_fit_count": 29,
        }
        atomic_json(root / "selection.json", value)
        atomic_json(
            root / "selection-receipt.json",
            {
                "selection": pin(root / "selection.json"),
                "protocol_sha256": digest(root / "protocol.json"),
            },
        )
        return value

    monkeypatch.setattr(experiment, "E3Data", FullData)
    monkeypatch.setattr(experiment, "trial_run", freeze_prediction)
    monkeypatch.setattr(experiment, "choose", choose)
    return events, protocol


def test_full_orchestration_freezes_predictions_before_outcomes_and_selection_before_evaluation(
    runner: tuple[Path, dict[str, Any], TinyData], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = runner
    events, _ = mock_full_runner(root, monkeypatch)
    experiment.run(root)
    result = json.loads((root / "execution.json").read_text())
    assert result["status"] == "completed" and result["new_main_fits"] == 29
    assert result["trials"] == 42 and result["failures"] == []
    assert events.index(("outcomes", "validation")) == 12
    assert events.index(("selection", "validation")) < events.index(
        ("prediction", "evaluation_2024")
    )
    assert events[-1] == ("closed", "data")
    assert "next_status" not in pd.read_parquet(root / "cohorts/validation/features.parquet")


@pytest.mark.parametrize("mutation", ["selection", "outcomes"])
def test_resumption_rejects_changed_selection_or_saved_outcomes(
    runner: tuple[Path, dict[str, Any], TinyData], monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root, _, _ = runner
    mock_full_runner(root, monkeypatch)
    experiment.run(root)
    if mutation == "selection":
        path = root / "selection.json"
        value = json.loads(path.read_text())
        value["monetary_formulation"] = "log_ratio"
        atomic_json(path, value)
    else:
        path = root / "cohorts/validation/outcomes.parquet"
        value = pd.read_parquet(path)
        value.loc[0, "next_aip_ore"] += 1
        value.to_parquet(path, index=False)
    with pytest.raises(ResearchError):
        experiment.run(root)
