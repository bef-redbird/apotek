from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bef_redbird.research import prospective as p
from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e4_common import read
from bef_redbird.research.group_features import pin
from bef_redbird.research.prospective_report import bins, block_interval, period_metrics


def saved(path: Path, value: dict) -> dict:
    p.write_once(path, value)
    return pin(path)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    root = tmp_path / "operation"
    root.mkdir()
    protocol = {
        "probability_states": {"current": {"cutoff": "2026-09-01"}},
        "milestone": {"blocks": [2, 4]},
    }
    p.write_once(root / "protocol.json", protocol)
    monkeypatch.setattr(p, "require", lambda _: protocol)
    monkeypatch.setattr(p, "now", lambda: datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    frame = pd.DataFrame(
        {
            "row_key": ["a", "b", "c"],
            "package_number": ["000001", "000002", "000003"],
            "period_start": pd.to_datetime(["2026-10-26"] * 3),
            "target_period": pd.to_datetime(["2026-11-09"] * 3),
            "origin_identity_token": ["a", "b", "c"],
            "origin_creation_date": ["2020-01-01"] * 3,
            "origin_exit_date": [None] * 3,
            "aip_ore": [10000, 20000, 30000],
            "eligible": [True, True, False],
            "exclusion_reason": ["", "", "ineligible"],
            "advance_abc": [None] * 3,
        }
    )
    feature = tmp_path / "features.parquet"
    frame.to_parquet(feature, index=False)
    source = tmp_path / "native.bin"
    source.write_bytes(b"native source fixture")
    producer = tmp_path / "producer.py"
    producer.write_text("# causal assembly fixture\n")
    origin, target = "2026-10-26", "2026-11-09"
    built = "2026-11-04T09:00:00Z"
    receipt_pins = []
    for role, artifact in (("features", feature), ("native", source)):
        receipt_pins.append(
            saved(
                tmp_path / f"{role}-receipt.json",
                {
                    "artifact": pin(artifact),
                    "origin": origin,
                    "target": target,
                    "received_utc": built,
                    "published_utc": None,
                    "field_scope": ["causal origin facts"],
                },
            )
        )
    lineage = saved(
        tmp_path / "lineage.json",
        {
            "features": pin(feature),
            "origin": origin,
            "target": target,
            "built_utc": built,
            "sources": [pin(source)],
            "producer": [pin(producer)],
            "field_scope": list(frame.columns),
        },
    )
    calendar = saved(
        tmp_path / "calendar.json",
        {
            "authority": "synthetic official calendar",
            "evidence": [pin(source)],
            "periods": [str(day.date()) for day in pd.date_range(origin, periods=13, freq="14D")],
        },
    )
    exposure = saved(
        tmp_path / "exposure.json",
        {
            "origin": origin,
            "target": target,
            "exposed": False,
            "actor": "synthetic researcher",
            "scope": "all target outcomes and development exposure",
            "checked_utc": "2026-11-04T09:30:00Z",
        },
    )
    context = {
        "origin": origin,
        "target": target,
        "calendar": calendar,
        "decision_cutoff": "2026-11-04T12:00:00+01:00",
        "information_mode": "current",
        "feature_built_utc": built,
        "feature_lineage": lineage,
        "source_receipts": receipt_pins,
        "outcome_exposure": {
            "exposed": False,
            "checked_utc": "2026-11-04T09:30:00Z",
            "evidence": exposure,
        },
    }

    def fake_predict(data: pd.DataFrame, _: dict, mode: str) -> pd.DataFrame:
        out = data.copy()
        out["prediction_state"] = np.where(out.eligible, "available", "excluded_origin")
        out["selected"] = [True, False, False]
        out["rank"] = [1, 2, None]
        for history in p.HISTORIES:
            out[f"amount_{history}_delta_dkk"] = [10.0, -5.0, None]
            out[f"amount_{history}_raw_delta_dkk"] = [10.0, -500.0, None]
        return out

    monkeypatch.setattr(p, "predict", fake_predict)
    return root, feature, context, frame


def test_immutable_freeze_retry_primary_and_crash_registration(setup: tuple) -> None:
    root, feature, context, _ = setup
    prepared = p.prepare(root, feature, context)
    assert p.prepare(root, feature, context) == prepared
    assessment = p.freeze(root, prepared)
    original = assessment.read_bytes()
    assert read(assessment)["primary"] is True
    assert p.freeze(root, prepared) == assessment
    assert assessment.read_bytes() == original
    assert len(read(root / "schedule.json")["periods"]) == 12
    marker = next((root / "primary").glob("*.json"))
    marker.unlink()
    (root / "schedule.json").unlink()
    assert p.freeze(root, prepared) == assessment
    assert marker.exists() and (root / "schedule.json").exists()
    assert p.verify(root)["status"] == "verified"


def test_same_origin_changed_inputs_are_supplementary(setup: tuple, tmp_path: Path) -> None:
    root, feature, context, _ = setup
    first = p.freeze(root, p.prepare(root, feature, context))
    revised = dict(context, comment="explicit second manual assessment")
    second = p.freeze(root, p.prepare(root, feature, revised))
    assert read(first)["primary"]
    assert read(second)["status"] == "supplementary"
    assert read(next((root / "primary").glob("*.json")))["assessment"] == pin(first)


def test_inference_crossing_cutoff_is_late_and_never_enrolls(
    setup: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, feature, context, _ = setup
    prepared = p.prepare(root, feature, context)
    times = iter(
        [datetime(2026, 11, 4, 10, 59, tzinfo=UTC), datetime(2026, 11, 4, 11, 0, 1, tzinfo=UTC)]
    )
    monkeypatch.setattr(p, "now", lambda: next(times))
    result = read(p.freeze(root, prepared))
    assert result["status"] == "late_retrospective"
    assert "completed_after_cutoff" in result["invalid_reasons"]
    assert not (root / "schedule.json").exists()
    assert p.report(root)["freeze_attempts"][0]["status"] == "late_retrospective"


@pytest.mark.parametrize("field", ["context", "inputs"])
def test_prepared_tampering_is_rejected(setup: tuple, field: str) -> None:
    root, feature, context, _ = setup
    prepared = p.prepare(root, feature, context)
    value = read(prepared)
    if field == "context":
        value["context"]["decision_cutoff"] = "2026-11-04T11:00:00+01:00"
    else:
        value["inputs"]["sha256"] = "0" * 64
    prepared.write_text(__import__("json").dumps(value))
    with pytest.raises(ResearchError, match="prepared_.*changed"):
        p.freeze(root, prepared)


def test_sources_after_actual_build_unknown_exposure_and_future_columns_rejected(
    setup: tuple,
) -> None:
    root, feature, context, frame = setup
    context["outcome_exposure"]["exposed"] = None
    with pytest.raises(ResearchError, match="exposure_evidence_required"):
        p.prepare(root, feature, context)
    context["outcome_exposure"]["exposed"] = False
    context["feature_built_utc"] = "2026-11-04T08:00:00Z"
    with pytest.raises(ResearchError, match="received_after_build"):
        p.prepare(root, feature, context)
    context["feature_built_utc"] = "2026-11-04T09:00:00Z"
    frame["next_aip_ore"] = 123
    frame.to_parquet(feature, index=False)
    with pytest.raises(ResearchError):
        p.prepare(root, feature, context)


def test_lineage_receipt_closure_and_report_expiry(setup: tuple, tmp_path: Path) -> None:
    root, feature, context, _ = setup
    context["source_receipts"] = context["source_receipts"][:1]
    with pytest.raises(ResearchError, match="feature_source_receipt_missing"):
        p.prepare(root, feature, context)
    context["information_mode"] = "hybrid"
    context["source_receipts"] += [pin(tmp_path / "native-receipt.json")]
    report = tmp_path / "old-report.json"
    p.write_once(report, {"mode": "historical_proxy"})
    context["advance_report"] = pin(report)
    with pytest.raises(ResearchError, match="actual_report_required"):
        p.prepare(root, feature, context)


def test_copenhagen_offsets_and_earlier_deadline_evidence(setup: tuple) -> None:
    root, feature, context, _ = setup
    with pytest.raises(ResearchError, match="copenhagen_offset"):
        p.cutoff("2026-11-04T12:00:00+02:00")
    assert p.cutoff("2026-07-01T12:00:00+02:00") == pd.Timestamp("2026-07-01T10:00:00Z")
    context["decision_cutoff"] = "2026-11-05T12:00:00+01:00"
    with pytest.raises(ResearchError, match="later_than_frozen_convention"):
        p.prepare(root, feature, context)


def test_outcome_pending_identity_and_corrections(
    setup: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, feature, context, _ = setup
    assessment = p.freeze(root, p.prepare(root, feature, context))
    original = pin(assessment)
    pending = p.attach_outcome(root, assessment, None)
    assert read(pending)["states"] == {"origin_ineligible": 1, "pending_target_observation": 2}
    rows = tmp_path / "official.parquet"
    pd.DataFrame(
        {
            "package_number": ["000001", "000002"],
            "aip_ore": [11000, 19000],
            "price_state": ["observed", "observed"],
            "identity_token": ["a", "changed"],
            "created_date_token": ["20200101"] * 2,
            "exit_date_token": ["19000101"] * 2,
            "dispensing_code": ["B", "B"],
        }
    ).to_parquet(rows, index=False)
    official = tmp_path / "official.json"
    p.write_once(official, {"period": context["target"], "rows": pin(rows)})
    with pytest.raises(ResearchError, match="official_receipt_required"):
        p.attach_outcome(root, assessment, official)
    receipt = tmp_path / "official-receipt.json"
    p.write_once(
        receipt,
        {
            "artifact": pin(official),
            "period": context["target"],
            "basis": "official_aip_ore",
            "status": "authoritative_resolved",
            "received_utc": "2026-11-06T19:00:00Z",
            "published_utc": "2026-11-06T18:00:00Z",
            "precedence": "original authoritative edition",
            "evidence": [pin(rows)],
        },
    )
    monkeypatch.setattr(p, "now", lambda: datetime(2026, 11, 10, tzinfo=UTC))
    observed = p.attach_outcome(root, assessment, official, receipt)
    assert read(observed)["supersedes"] == pin(pending)
    assert read(observed)["states"] == {
        "observed": 1,
        "identity_discontinuity_or_ambiguity": 1,
        "origin_ineligible": 1,
    }
    assert p.attach_outcome(root, assessment, official, receipt) == observed
    assert pin(assessment) == original
    with pytest.raises(ResearchError, match="pending_cannot_demote"):
        p.attach_outcome(root, assessment, None)
    assert p.verify(root)["status"] == "verified"
    receipt.write_text("{}")
    with pytest.raises(ResearchError):
        p.verify(root)


def test_fixed_bins_pairing_unknowns_and_temporal_grid() -> None:
    values = np.array([-21, -20, -1, 1, 20, 100, 101])
    assert list(bins(values, "magnitude")) == [
        "negative_large",
        "negative_small",
        "near_zero",
        "near_zero",
        "positive_small",
        "positive_medium",
        "positive_large",
    ]
    frame = pd.DataFrame(
        {
            "prediction_state": ["available"] * 3,
            "selected": [True] * 3,
            "status": ["observed", "observed", "pending"],
            "next_delta_ore": [200, -200, None],
        }
    )
    for history, forecast in (("3y", [1, -3, 5]), ("6y", [3, -1, 8]), ("full", [2, -2, 100])):
        frame[f"amount_{history}_delta_dkk"] = forecast
        frame[f"amount_{history}_raw_delta_dkk"] = forecast
    result = period_metrics(frame, "2026-10-26", "current")
    overall = next(
        item for item in result if item["history"] == "3y" and item["membership"] == "overall"
    )
    assert overall["bias"] == -1 and overall["mae"] == 1
    assert overall["observed"] == 2 and overall["unknown"] == 1
    common = [
        item
        for item in result
        if item["membership"] == "common_3y"
        and item["bin_kind"] == "magnitude"
        and item["support"] == "supported"
        and item["population"] == "all_eligible"
    ]
    for label in ("negative_large", "near_zero", "positive_small"):
        assert len({item["candidates"] for item in common if item["bin"] == label}) == 1
    interval = block_interval(np.array([1, np.nan, 3, np.nan, 5, np.nan]), 2)
    assert interval["grid_periods"] == 6 and interval["scored_periods"] == 3
    assert interval == block_interval(np.array([1, np.nan, 3, np.nan, 5, np.nan]), 2)
    assert interval["lower"] is None and interval["upper"] is None


def test_independent_model_fallback_and_probability_temperature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {
            "row_key": ["a", "b", "c"],
            "period_start": pd.to_datetime(["2026-10-26"] * 3),
            "aip_ore": [10000, 20000, 30000],
            "eligible": [True, True, False],
            "advance_abc": ["A", None, "C"],
            "peer_count": [1, 0, 0],
            "broad_peer_count": [0, 0, 0],
            "broad_peer_feature_state": ["none"] * 3,
        }
    )
    protocol = {
        "amount_models": {
            history: {mode: {"id": mode, "history": history} for mode in ("current", "advance")}
            for history in p.HISTORIES
        },
        "probability_models": {
            f"{peer}_{mode}": {"id": mode}
            for peer in ("package", "equal", "broad")
            for mode in ("current", "advance")
        },
        "probability_states": {"current": {"temperature": 0.8}, "hybrid": {"temperature": 1.2}},
    }

    def model(data: pd.DataFrame, item: dict, task: str) -> pd.DataFrame:
        result = data[["row_key"]].copy()
        if task == "direction":
            result[p.PROBS] = np.tile(
                [0.05, 0.15, 0.8] if item["id"] == "advance" else [0.1, 0.7, 0.2], (len(data), 1)
            )
        else:
            multiplier = {"3y": 1, "6y": 2, "full": 3}[item["history"]]
            raw = np.full(len(data), (10 if item["id"] == "advance" else -500) * multiplier)
            result["raw_delta_dkk"] = raw
            result["next_price_dkk"] = np.maximum(0, data.aip_ore / 100 + raw)
            result["delta_dkk"] = result.next_price_dkk - data.aip_ore / 100
        return result

    monkeypatch.setattr(p, "model_predict", model)
    output = p.predict(frame, protocol, "hybrid")
    for history, multiplier in (("3y", 1), ("6y", 2), ("full", 3)):
        assert output.loc[0, f"amount_{history}_raw_delta_dkk"] == 10 * multiplier
        assert output.loc[1, f"amount_{history}_raw_delta_dkk"] == -500 * multiplier
        assert output.loc[1, f"amount_{history}_next_price_dkk"] == 0
        assert output.loc[1, f"amount_{history}_delta_dkk"] == -200
        assert output.loc[1, f"amount_{history}_raw_next_price_dkk"] == 200 - 500 * multiplier
    expected = p.calibrate(np.array([[0.1, 0.7, 0.2]]), {"temperature": 1.2})[0]
    np.testing.assert_array_equal(output.loc[1, p.PROBS].to_numpy(dtype=float), expected)
    assert output.loc[2, "prediction_state"] == "excluded_origin"
    assert not output.loc[2, "selected"]


def test_saved_report_missed_periods_and_outside_root_protection(
    setup: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, feature, context, _ = setup
    assessment = p.freeze(root, p.prepare(root, feature, context))
    report = p.report(root)
    assert len(report["periods"]) == 24
    assert (
        len(
            [
                item
                for item in report["periods"]
                if item["state"] in ("not_yet_due", "missed", "advance_unavailable")
            ]
        )
        == 23
    )
    assert all(not item["coverage_condition_met"] for item in report["summaries"])
    outside = tmp_path / "protected" / "assessment.json"
    outside.parent.mkdir()
    outside.write_bytes(assessment.read_bytes())
    with pytest.raises(ResearchError, match="outside_operation"):
        p.attach_outcome(root, outside, None)
    assert not (outside.parent / "outcomes").exists()

    def missing(_: Path) -> dict:
        raise FileNotFoundError("missing protocol")

    monkeypatch.setattr(p, "require", missing)
    assert p.status(tmp_path / "absent")["status"] == "not_ready"


def test_expired_actual_report_rejected_and_partial_report_admitted(
    setup: tuple, tmp_path: Path
) -> None:
    root, feature, context, _ = setup
    rows = tmp_path / "report-rows.parquet"
    pd.DataFrame(
        {
            "package_number": ["000001", "000002"],
            "advance_abc": ["C", None],
            "advance_state": ["observed", "invalid"],
            "current_abc": ["A", "B"],
            "current_state": ["observed"] * 2,
        }
    ).to_parquet(rows, index=False)
    for name, origin, target in (
        ("expired", "2026-10-12", "2026-10-26"),
        ("valid", context["origin"], context["target"]),
    ):
        report = tmp_path / f"{name}-report.json"
        p.write_once(
            report,
            {
                "version": "e4-advance-report-v1",
                "original": pin(rows),
                "report_id": name,
                "context": {"current_period": origin, "target_period": target},
                "rows": pin(rows),
            },
        )
        receipt = saved(
            tmp_path / f"{name}-report-receipt.json",
            {
                "artifact": pin(report),
                "origin": context["origin"],
                "target": context["target"],
                "field_scope": ["upcoming ABC"],
                "received_utc": context["feature_built_utc"],
                "published_utc": None,
            },
        )
        hybrid = dict(
            context,
            information_mode="hybrid",
            advance_report=pin(report),
            advance_report_receipt=receipt,
            source_receipts=[*context["source_receipts"], receipt],
        )
        if name == "expired":
            with pytest.raises(ResearchError, match="stale_or_wrong_report_period"):
                p.prepare(root, feature, hybrid)
        else:
            prepared = read(p.prepare(root, feature, hybrid))
            bound = pd.read_parquet(p.checked(prepared["inputs"]))
            assert bound.package_number.tolist() == ["000001", "000002", "000003"]
            assert bound.advance_abc.tolist()[0] == "C"
            assert pd.isna(bound.advance_abc.iloc[1:]).all()


def test_inference_failure_never_publishes_partial_freeze_and_changed_protocol_rejected(
    setup: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, feature, context, _ = setup
    prepared = p.prepare(root, feature, context)
    original_predict = p.predict

    def failed(*_: object) -> pd.DataFrame:
        raise ResearchError("synthetic interrupted inference")

    monkeypatch.setattr(p, "predict", failed)
    with pytest.raises(ResearchError, match="interrupted inference"):
        p.freeze(root, prepared)
    assert not list(root.glob("assessments/*/assessment.json"))
    assert not (root / "schedule.json").exists() and not (root / ".writer.lock").exists()
    monkeypatch.setattr(p, "predict", original_predict)
    assert read(p.freeze(root, prepared))["primary"]
    (root / "protocol.json").write_text('{"changed_model_or_calibration":true}')
    with pytest.raises(ResearchError, match="prepared_protocol_mismatch"):
        p.freeze(root, prepared)


def test_gapped_calendar_cannot_replace_missed_origin(setup: tuple, tmp_path: Path) -> None:
    root, feature, context, _ = setup
    document = read(p.checked(context["calendar"]))
    days = [str(day.date()) for day in pd.date_range(context["origin"], periods=14, freq="14D")]
    document["periods"] = [day for index, day in enumerate(days) if index != 3]
    context["calendar"] = saved(tmp_path / "gapped-calendar.json", document)
    with pytest.raises(ResearchError, match="calendar_gap"):
        p.prepare(root, feature, context)


def test_schedule_cannot_drop_missed_periods_and_status_distinguishes_due_dates(
    setup: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, feature, context, _ = setup
    p.freeze(root, p.prepare(root, feature, context))
    monkeypatch.setattr(p, "now", lambda: datetime(2026, 11, 20, tzinfo=UTC))
    report = p.report(root)
    current = [row for row in report["periods"] if row["information_mode"] == "current"]
    assert current[1]["state"] == "missed"
    assert current[2]["state"] == "not_yet_due"
    assert all(
        row["state"] == "advance_unavailable"
        for row in report["periods"]
        if row["information_mode"] == "hybrid"
    )
    schedule = read(root / "schedule.json")
    schedule["periods"][1] = schedule["periods"][2]
    (root / "schedule.json").write_text(__import__("json").dumps(schedule))
    with pytest.raises(ResearchError, match="schedule_first_freeze_binding"):
        p.verify(root)
