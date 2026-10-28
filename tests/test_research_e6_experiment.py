"""Independent admission and weighting examples for older-history experiments."""

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research import e6_experiment as experiment
from bef_redbird.research.e4_common import frozen_json
from bef_redbird.research.e6_protocol import FOLDS


def test_recency_weight_is_cutoff_based_and_period_balanced() -> None:
    frame = pd.DataFrame(
        {"period_start": pd.to_datetime(["2023-07-02", "2023-07-02", "2026-07-01"])}
    )
    plain, _ = experiment.training_weights(frame, "2026-07-01", False)
    weighted, state = experiment.training_weights(frame, "2026-07-01", True)
    assert plain[0] + plain[1] == pytest.approx(plain[2])
    expected_ratio = 2 ** (-1095 / (3 * 365.25))
    assert (weighted[0] + weighted[1]) / weighted[2] == pytest.approx(expected_ratio)
    assert weighted.mean() == pytest.approx(1)
    assert state["effective_rows"] < 3
    assert np.isfinite(weighted).all()


def test_strict_maturity_and_old_disappeared_package_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features = pd.DataFrame(
        {
            "row_key": ["old", "recent", "equal-cutoff", "unknown"],
            "package_number": ["000001", "000002", "000003", "000004"],
            "period_index": [1, 450, 451, 452],
            "period_start": pd.to_datetime(
                ["2008-01-14", "2026-03-02", "2026-03-16", "2026-03-16"]
            ),
            "identity_episode_id": ["old-disappeared", "new", "new2", "new3"],
            "aip_ore": [100, 200, 300, 400],
        }
    )
    labels = features[list(experiment.KEYS)].assign(
        next_status=["observed", "observed", "observed", "pending"],
        next_maturity_date=pd.to_datetime(["2008-01-28", "2026-03-16", "2026-04-01", None]),
        next_direction=[1, 0, -1, None],
        next_delta_ore=[10, 0, -1, None],
        next_aip_ore=[110, 200, 299, None],
    )

    def load(
        _root: Path, start: str, end: str, columns: list[str], *, outcomes: bool = False
    ) -> pd.DataFrame:
        assert start == "2008-01-01"
        assert end == "2026-04-01"
        return (labels if outcomes else features)[columns].copy()

    monkeypatch.setattr(experiment, "load_window", load)
    spec: dict[str, Any] = {"fold": "spring", "history": "full"}
    result = experiment.training_cohort(Path("unused"), spec, ["aip_ore"])
    assert result.row_key.tolist() == ["old", "recent"]
    assert "old-disappeared" in result.identity_episode_id.tolist()


def test_additional_year_has_no_2026_fallback_cutoff() -> None:
    earlier = next(f for f in FOLDS if f["name"] == "earlier")
    assert earlier["fit"] == "2025-01-01"
    assert earlier["reference_year"] == 2025
    assert earlier["fit"] < earlier["cal_start"] < earlier["start"] < earlier["end"]


@pytest.mark.parametrize("history", ["full", "weighted"])
def test_optional_budget_uses_whole_trial_elapsed_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: str
) -> None:
    """Loading/prediction costs cannot disappear from optional admission."""
    monkeypatch.setattr(
        experiment,
        "require",
        lambda root: {"resource_limits": {"aggregate_fit_seconds": 6000, "full_fit_seconds": 7200}},
    )
    monkeypatch.setattr(
        experiment,
        "ledger",
        lambda root: {
            "fit_seconds": 1000,
            "trials": [
                {
                    "spec": {"history": history, "task": "direction"},
                    "fit_seconds": 1000,
                    "resource": {"elapsed_seconds": 2000},
                }
            ],
        },
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Optional budget rejection must happen before data loading or a model fit")

    monkeypatch.setattr(experiment, "training_cohort", forbidden)
    spec = {"id": "optional-insufficient", "optional": True}
    result = experiment.run_trial(tmp_path, spec)
    assert result["status"] == "skipped"
    assert result["remaining_fit_seconds"] == 5000
    assert not (tmp_path / "trials" / spec["id"] / "model.cbm").exists()


def test_technical_interruption_remains_charged_after_successful_retry(tmp_path: Path) -> None:
    for folder, status, seconds in [
        ("attempts/interrupted-1", "failed", 1200),
        ("trials/summer-direction-weighted-advance", "completed", 2600),
    ]:
        path = tmp_path / folder / "receipt.json"
        path.parent.mkdir(parents=True)
        frozen_json(path, {"status": status, "fit_seconds": seconds})
    value = experiment.ledger(tmp_path)
    assert (value["attempts"], value["completed"], value["failed"]) == (2, 1, 1)
    assert value["fit_seconds"] == 3800
