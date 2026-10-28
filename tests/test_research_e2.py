"""Independent decision-cohort and statistical boundaries for bounded E2."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e2_data import ResearchData, attach_outcomes
from bef_redbird.research.e2_experiment import SETTINGS, publish, replay, run_trial
from bef_redbird.research.e2_metrics import freeze_predictions, paired_interval, score_frame
from bef_redbird.research.e2_models import LogisticModel, period_weights
from bef_redbird.research.training_contract import KEYS, atomic_json


def feature_frame(count: int = 30, start: str = "2024-01-08") -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "row_key": [f"key-{start}-{i}" for i in range(count)],
            "package_number": [f"{i:06d}" for i in range(count)],
            "period_index": 1,
            "period_start": pd.Timestamp(start),
            "identity_episode_id": [f"episode-{i}" for i in range(count)],
            "eligible": True,
            "provenance": "{}",
            "aip_ore": np.arange(count) + 100,
            "history_periods": np.arange(count),
            "lag1_delta_ore": np.resize([-1, 0, 1], count),
            "abc": np.resize(["A", "B", None], count),
        }
    )
    return frame


def label_frame(features: pd.DataFrame) -> pd.DataFrame:
    result = features[list(KEYS)].copy()
    result["next_status"] = "observed"
    result["next_direction"] = np.resize([-1, 0, 1], len(result))
    result["next_delta_ore"] = result.next_direction * 10
    result["next_maturity_date"] = result.period_start + pd.Timedelta(days=14)
    result["reversal_status"] = "observed"
    result["reversal_category"] = np.resize(
        ["full_reversal", "partial_reversal", "persistence"], len(result)
    )
    result["reversal_maturity_date"] = result.period_start + pd.Timedelta(days=28)
    return result


def test_candidates_do_not_read_future_labels_and_training_matures(tmp_path: Path) -> None:
    features = feature_frame()
    labels = label_frame(features)
    labels.loc[0, "next_status"] = "gap"
    labels.loc[1, "next_maturity_date"] = pd.Timestamp("2024-02-01")
    for role, frame in (("features", features), ("labels", labels), ("source", features)):
        folder = tmp_path / "part-0000"
        folder.mkdir(exist_ok=True)
        frame.to_parquet(folder / f"{role}.parquet", index=False)
    path = tmp_path / "dataset.json"
    atomic_json(
        path,
        {
            "files": [
                {"path": f"part-0000/{role}.parquet"} for role in ("features", "labels", "source")
            ]
        },
    )
    data = ResearchData(path)
    assert len(data.candidates("2024-01-01", "2024-02-01")) == 30
    train = data.training("2024-01-01", "2024-02-01")
    assert len(train) == 28
    assert train.next_maturity_date.max() < pd.Timestamp("2024-02-01")
    data.connection.execute("DROP TABLE labels")
    assert len(data.candidates("2024-01-01", "2024-02-01")) == 30
    data.close()


def test_unknown_selected_not_replaced_and_rank_precedes_outcomes() -> None:
    features = feature_frame(20)
    probability = np.tile([0.1, 0.8, 0.1], (20, 1))
    probability[:2] = [0.05, 0.05, 0.9]
    frozen = freeze_predictions(features, probability, task="direction", seed=1)
    labels = label_frame(features)
    labels.loc[0, ["next_status", "next_direction", "next_delta_ore"]] = ["gap", np.nan, np.nan]
    labels.loc[1, "next_direction"] = 1
    attached = attach_outcomes(frozen, labels.sample(frac=1, random_state=7))
    assert attached.selected.equals(frozen.selected)
    assert attached.loc[attached.selected, "package_number"].tolist() == ["000000", "000001"]
    aggregate, _ = score_frame(attached, "direction", "test", "model")
    assert aggregate["metrics"]["shortlist_unknown_fraction"] == 0.5
    assert aggregate["metrics"]["rise_precision_top_decile"] == 1
    assert aggregate["metrics"]["shortlist_rise_lower_bound"] == 0.5
    assert aggregate["metrics"]["shortlist_rise_upper_bound"] == 1


def test_alignment_rejects_identity_change_and_duplicate_labels() -> None:
    features = feature_frame()
    labels = label_frame(features)
    labels.loc[0, "identity_episode_id"] = "different"
    with pytest.raises(ResearchError, match="missing_label"):
        attach_outcomes(features, labels)
    with pytest.raises(ResearchError, match="duplicate"):
        attach_outcomes(features, pd.concat([labels, labels.iloc[:1]]))


def test_ties_replay_and_probability_validation() -> None:
    features = feature_frame(50)
    probs = np.tile([0, 1, 0], (50, 1))
    a = freeze_predictions(features, probs, task="direction", seed=7)
    b = freeze_predictions(features, probs, task="direction", seed=7)
    pd.testing.assert_frame_equal(a, b)
    assert a.selected.sum() == 5
    assert a.loc[a.selected, "package_number"].tolist() != features.package_number[:5].tolist()
    with pytest.raises(ResearchError, match="invalid_probabilities"):
        freeze_predictions(features, np.ones((50, 3)), task="direction", seed=7)
    with pytest.raises(ResearchError, match="invalid_magnitude"):
        freeze_predictions(features, np.full(50, np.inf), task="magnitude", seed=7)


def test_logistic_preprocessing_is_training_only_and_period_weights() -> None:
    train = feature_frame()
    model = LogisticModel(["aip_ore", "abc"], ["abc"]).fit(train, np.resize([0, 1, 2], 30))
    state = json.dumps(model.transforms, sort_keys=True)
    future = train.copy()
    future["aip_ore"] = 1e20
    future["abc"] = "FUTURE_ONLY"
    probabilities = model.predict_proba(future)
    assert json.dumps(model.transforms, sort_keys=True) == state
    assert np.allclose(probabilities.sum(axis=1), 1)
    combined = pd.concat([train, feature_frame(10, "2024-01-22")], ignore_index=True)
    weights = period_weights(combined)
    assert np.isclose(weights[:30].sum(), weights[30:].sum())


def test_period_weighted_primary_and_paired_uncertainty() -> None:
    first = feature_frame(30)
    second = feature_frame(3, "2024-01-22")
    candidates = pd.concat([first, second], ignore_index=True)
    probs = np.tile([0, 1, 0], (33, 1))
    outcomes = label_frame(candidates)
    outcomes["next_direction"] = 0
    outcomes.loc[30:, "next_direction"] = 1
    frame = attach_outcomes(
        freeze_predictions(candidates, probs, task="direction", seed=1), outcomes
    )
    aggregate, periods = score_frame(frame, "direction", "test", "no_change")
    assert aggregate["metrics"]["brier"] == 1
    assert np.isclose(aggregate["metrics"]["package_weighted_brier"], 6 / 33)
    assert len(periods) == 2
    interval = paired_interval(np.full(20, -0.02), seed=1)
    assert np.isclose(interval["lower"], -0.02) and np.isclose(interval["upper"], -0.02)


class FakeData:
    def training(self, start: str, end: str, *, target: str) -> pd.DataFrame:
        frame = feature_frame(90, "2021-01-11")
        return attach_outcomes(frame, label_frame(frame))

    def candidates(self, start: str, end: str, *, reversal: bool = False) -> pd.DataFrame:
        return feature_frame()

    def outcomes(self, start: str, end: str) -> pd.DataFrame:
        return label_frame(feature_frame())


@pytest.mark.parametrize(
    "method,task",
    [
        ("catboost_group", "direction"),
        ("logistic_group", "direction"),
        ("catboost_group_mae", "magnitude"),
        ("catboost_group", "reversal"),
    ],
)
def test_actual_small_fit_freeze_attach_and_exact_replay(
    tmp_path: Path, method: str, task: str
) -> None:
    protocol: dict[str, Any] = {
        "features": {"group": ["aip_ore", "lag1_delta_ore", "abc"]},
        "categories": {"group": ["abc"]},
        "models": {"catboost": {**SETTINGS, "iterations": 3, "thread_count": 1}},
    }
    atomic_json(tmp_path / "protocol.json", protocol)
    result = run_trial(
        FakeData(),
        protocol,
        tmp_path,
        trial_id="trial",
        split="test",
        start="2024-01-01",
        end="2025-01-01",
        cutoff="2024-01-01",
        history=1,
        task=task,
        method=method,
    )  # type: ignore[arg-type]
    assert result["status"] == "completed", result.get("traceback")
    assert result["training_rows"] == 90
    replay(tmp_path)
    assert json.loads((tmp_path / "replay.json").read_text())["status"] == "passed"


def test_publish_refuses_unexecuted_declared_trials(tmp_path: Path) -> None:
    atomic_json(
        tmp_path / "protocol.json",
        {
            "history_choices": [1, 3, "expanding"],
            "evaluation": [],
            "fits": {"maximum_learned_fits": 3},
        },
    )
    (tmp_path / "trials").mkdir()
    with pytest.raises(ResearchError, match="inventory_incomplete"):
        publish(tmp_path)
    assert not (tmp_path / "saved/result.json").exists()
