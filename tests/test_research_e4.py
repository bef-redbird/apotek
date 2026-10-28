from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xlwt

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e4_assessment import (
    attach_outcome,
    bind_report,
    prepare,
    validate_features,
)
from bef_redbird.research.e4_common import frozen_json, read
from bef_redbird.research.e4_evaluate import interval_metrics
from bef_redbird.research.e4_methods import fit_residual, mixed, residual_bounds, route
from bef_redbird.research.e4_scenarios import Ledger, simulate
from bef_redbird.research.e4_sources import intake_report
from bef_redbird.research.group_features import pin


def context() -> dict:
    return {
        "origin": "2026-10-12",
        "target": "2026-10-26",
        "calendar_next_start": "2026-10-26",
        "decision_cutoff": "2026-10-21T12:00:00+02:00",
        "mode": "actual_report_retrospective",
    }


def features() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_key": ["k1", "k2"],
            "package_number": ["000001", "000002"],
            "period_start": pd.to_datetime(["2026-10-12"] * 2),
            "target_period": pd.to_datetime(["2026-10-26"] * 2),
            "identity_episode_id": ["a", "b"],
            "origin_identity_token": ["drug1", "drug2"],
            "origin_creation_date": ["2020-01-01", "2020-01-01"],
            "origin_exit_date": [None, None],
            "aip_ore": [10000, 20000],
            "abc": ["A", "C"],
            "advance_abc": ["C", None],
            "advance_state": ["observed", "missing"],
            "eligible": [True, True],
            "exclusion_reason": ["", ""],
        }
    )


def cito(path: Path, rows: list[list]) -> None:
    book = xlwt.Workbook()
    sheet = book.add_sheet("native")
    for j, label in enumerate(
        ["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"]
    ):
        sheet.write(0, j, label)
    for i, row in enumerate(rows, 1):
        for j, value in enumerate(row):
            if value is not None:
                sheet.write(i, j, value)
    stream = io.BytesIO()
    book.save(stream)
    path.write_bytes(stream.getvalue())


def report(tmp_path: Path, rows: list[list], supersedes: Path | None = None) -> Path:
    path = tmp_path / ("corrected.xls" if supersedes else "original.xls")
    cito(path, rows)
    return intake_report(
        path,
        tmp_path / "archive",
        tmp_path / "reports",
        {
            "current_period": "2026-10-12",
            "target_period": "2026-10-26",
            "next_period_start": "2026-10-26",
            "period_evidence": "synthetic declared calendar",
            "reported_receipt": {"precision": "unknown"},
        },
        supersedes=supersedes,
    )


def test_cito_identifiers_duplicates_conflicts_and_revisions(tmp_path: Path) -> None:
    p = report(
        tmp_path,
        [
            [1, "N", 123, "A", "C", 4],
            ["000001", "N", 123, "A", "C", 4],
            [2, "N", 999, "B", "A", 4],
            [2, "N", 999, "B", "C", 4],
            [3, "N", 999, "C", None, 4],
        ],
    )
    saved = read(p)
    rows = pd.read_parquet(saved["rows"]["path"]).set_index("package_number")
    assert saved["counts"]["physical_records"] == 5
    assert saved["counts"]["unique_valid_identifiers"] == 3
    assert rows.loc["000001", "advance_abc"] == "C"
    assert rows.loc["000001", "advance_assertions"] == 2
    assert rows.loc["000002", "advance_state"] == "conflicting"
    assert rows.loc["000003", "advance_state"] == "missing"
    before = p.read_bytes()
    new = report(tmp_path, [[2, "N", 999, "B", "A", 4]], supersedes=p)
    assert read(new)["supersedes"]["sha256"] == pin(p)["sha256"]
    assert p.read_bytes() == before
    bound = bind_report(features(), p, "2026-10-12", "2026-10-26")
    assert bound.abc.tolist() == ["A", "C"]
    assert bound.advance_abc.iloc[0] == "C" and pd.isna(bound.advance_abc.iloc[1])
    with pytest.raises(ResearchError):
        bind_report(features(), p, "2026-10-26", "2026-11-09")


def test_origin_route_and_missing_information_only() -> None:
    f = pd.DataFrame(
        {
            "peer_count": [2, 2, None],
            "broad_peer_count": [1, 0, 100],
            "broad_peer_feature_state": [
                "partial_same_unit_peers",
                "complete_same_unit_peers",
                "unsupported",
            ],
        }
    )
    p = np.tile([0.1, 0.8, 0.1], (3, 1))
    e = np.tile([0.2, 0.6, 0.2], (3, 1))
    b = np.tile([0.3, 0.4, 0.3], (3, 1))
    values, names = route(f, p, e, b)
    np.testing.assert_array_equal(values, np.array([b[0], e[1], p[2]]))
    assert names.tolist() == ["broader peers", "equal-size peers", "package history"]
    x = mixed(values, p, pd.Series(["A", None, "invalid"]))
    np.testing.assert_array_equal(x, np.array([values[0], p[1], p[2]]))
    f["next_aip_ore"] = [999, 1, 5000]
    np.testing.assert_array_equal(route(f, p, e, b)[0], values)


def test_residual_support_and_strict_maturity() -> None:
    f = pd.DataFrame(
        {
            "aip_ore": [1000] * 402,
            "history_periods": [1] * 201 + [20] * 201,
            "peer_count": [1] * 402,
            "period_start": pd.to_datetime(["2023-01-01"] * 402),
            "next_status": ["observed"] * 402,
            "next_maturity_date": pd.to_datetime(["2023-01-15"] * 401 + ["2024-01-01"]),
            "next_aip_ore": [1500] * 201 + [1100] * 200 + [100000000],
            "next_aip_dkk": [10.0] * 402,
        }
    )
    state = fit_residual(f, "2024-01-01")
    lower, upper = residual_bounds(f, np.full(402, 10.0), state)
    assert state["rows"] == 401
    assert (lower[:201] == 5).all() and (upper[:201] == 15).all()
    assert (lower[201:] == 9).all() and (upper[201:] == 11).all()
    unavailable = interval_metrics(
        np.array([10.0, 10.0]), np.array([9.0, np.nan]), np.array([11.0, np.nan])
    )
    assert unavailable == {
        "interval_observed": 1,
        "interval_unavailable": 1,
        "coverage90": 1.0,
        "width90_dkk": 2.0,
    }


def test_future_column_and_bad_period_are_rejected() -> None:
    f = features()
    validate_features(f, context())
    with pytest.raises(ResearchError):
        validate_features(f.assign(next_aip_ore=1), context())
    with pytest.raises(ResearchError):
        validate_features(f.assign(abc_t2="A"), context())
    with pytest.raises(ResearchError):
        validate_features(f, {**context(), "target": "2026-11-09"})
    with pytest.raises(ResearchError):
        validate_features(
            f, {**context(), "mode": "prospective_pending", "target_outcome_exposed": True}
        )


def test_immutable_assessment_idempotence_and_outcome_versions(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two nested SHA-256 directories need a short root on Windows without long paths.
    tmp_path = tmp_path_factory.mktemp("e4")
    import bef_redbird.research.e4_assessment as module

    f = features()
    p = tmp_path / "input.parquet"
    f.to_parquet(p, index=False)
    model, cal = tmp_path / "model.json", tmp_path / "cal.json"
    frozen_json(model, {"synthetic": True})
    frozen_json(cal, {"synthetic": True})
    monkeypatch.setattr(
        module,
        "predict",
        lambda frame, *args: frame.assign(prediction_state="available", selected=[True, False]),
    )
    a = prepare(tmp_path, p, context(), model, cal)
    original = Path(read(a)["predictions"]["path"]).read_bytes()
    assert prepare(tmp_path, p, context(), model, cal) == a
    pending = attach_outcome(a, None)
    assert attach_outcome(a, None) == pending
    rows = tmp_path / "official.parquet"
    pd.DataFrame(
        {
            "package_number": ["000001", "000002"],
            "aip_ore": [12000, 30000],
            "price_state": ["observed", "observed"],
            "identity_token": ["drug1", "different"],
            "created_date_token": ["20200101", "20200101"],
            "exit_date_token": ["19000101", "19000101"],
            "dispensing_code": ["B", "B"],
        }
    ).to_parquet(rows, index=False)
    official = tmp_path / "official.json"
    frozen_json(official, {"period": "2026-10-26", "rows": pin(rows)})
    completed = attach_outcome(a, official)
    assert read(completed)["supersedes"]["sha256"] == pin(pending)["sha256"]
    assert read(completed)["states"] == {"observed": 1, "identity_discontinuity_or_ambiguity": 1}
    assert attach_outcome(a, official) == completed
    assert Path(read(a)["predictions"]["path"]).read_bytes() == original
    assert len(list((a.parent / "outcomes").glob("*/outcome.json"))) == 2


def test_exact_compensation_return_and_no_double_credit() -> None:
    from fractions import Fraction

    ledger = Ledger("a", 4, 10000)
    ledger.credit(0, {"a": 10000}, {"a": 8000}, Fraction(1), 2)
    assert ledger.compensation == 4000 and ledger.book == 36000
    ledger.demand(7, 1)
    assert ledger.sold_cost == 8000
    ledger.credit(14, {"a": 8000}, {"a": 7000}, Fraction(1), None)
    assert ledger.compensation == 7000  # another 3x10 DKK, not a repeated 30 DKK loss
    ledger.returns(28, Fraction(1, 10))
    assert ledger.returned_basis == 25000 and ledger.return_credit == 22500
    ledger.check()


def test_scenario_same_service_and_late_receipt_price() -> None:
    prices = [{"a": 10000, "b": 11000}, {"a": 15000, "b": 16000}, {"a": 10000, "b": 11000}]
    base, _ = simulate(prices, demand=4, extra_limit=4, compensation="none", extra_package=None)
    extra, _ = simulate(prices, demand=4, extra_limit=4, compensation="none", extra_package="a")
    late, events = simulate(
        prices, demand=4, extra_limit=4, compensation="none", extra_package="a", variant="late"
    )
    assert base["sold"] == extra["sold"] == late["sold"] == 8
    assert base["unmet"] == extra["unmet"] == late["unmet"] == 0
    assert extra["purchases_dkk"] == 600
    assert base["purchases_dkk"] == 700
    assert late["purchases_dkk"] == 800
    late_receipt = [e for e in events if e["event"] == "receipt" and e["day"] == 1][0]
    assert late_receipt["ordered_day"] == -1 and late_receipt["unit_acquisition_ore"] == 15000


def test_expiry_cannot_sell_or_return_written_off_stock() -> None:
    result, _ = simulate(
        [{"a": 10000}] * 3,
        demand=4,
        extra_limit=12,
        compensation="none",
        extra_package="a",
        variant="expiry",
    )
    assert result["expired"] == 10 and result["writeoff_dkk"] == 1000
    assert result["sold"] == 8 and result["remaining"] == 0 and result["returned"] == 0
    assert result["received"] == 16 and result["stock_and_book_checks"]


def test_cito_comparison_blank_exit_sentinel_creation_and_wrong_code(tmp_path: Path) -> None:
    from bef_redbird.research.e4_sources import compare_report

    source = report(tmp_path, [[1, "N", 9, "A", "C", 1], [2, "N", 9, "B", "A", 1]])
    current = pd.DataFrame(
        {
            "package_number": ["000001", "000002"],
            "period_start": ["2026-10-12"] * 2,
            "abc": ["A", "B"],
            "classification_state": ["observed"] * 2,
            "identity_token": ["drug1", "drug2"],
            "created_date_token": ["20200101", "19000101"],
            "exit_date_token": ["        "] * 2,
        }
    )
    target = current.assign(period_start="2026-10-26", abc=["B", "A"])
    candidates = features().assign(selected=True)
    output = compare_report(source, current, target, candidates, tmp_path / "comparison")
    assert read(output)["totals"]["advance"]["states"] == {
        "incorrect": 1,
        "identity_discontinuity_or_ambiguity": 1,
    }
    assert read(output)["totals"]["current"]["states"] == {"correct": 2}
    with pytest.raises(ResearchError, match="period_mismatch"):
        compare_report(
            source, current, target.assign(period_start="2026-11-09"), candidates, tmp_path / "bad"
        )


def test_incremental_features_equal_full_causal_recipe() -> None:
    from datetime import date, timedelta

    from bef_redbird.research.e4_features import advance, assert_same
    from bef_redbird.research.training import package_rows
    from bef_redbird.research.training_contract import FEATURES

    days = [date(2026, 1, 5) + timedelta(days=14 * i) for i in range(4)]
    calendar = [{"index": i, "start": str(day)} for i, day in enumerate(days)]
    raw = [
        {
            "period_start": day,
            "package_number": "000001",
            "canonical_identity": "episode",
            "drug_id": "drug",
            "price_state": "present",
            "aip_ore": amount,
            "currency": "DKK",
            "tax_basis": "excluding_vat",
            "fee_basis": "excluding_consumer_dispensing_fees",
            "price_basis": "per_package",
            "price_role": "effective_price",
            "creation_date": days[0],
            "exit_date": None,
            "dispensing_code": "B",
            "abc": "A",
            "group_code": "0001",
            "classification_state": "observed",
            "provenance": "{}",
        }
        for day, amount in zip(days, [100, 200, 150, 150], strict=True)
    ]
    expected, _ = package_rows(raw, calendar, [], set())
    actual = advance(
        pd.DataFrame(raw[-1:]), pd.DataFrame(raw[-2:-1]), pd.DataFrame(expected[-2:-1]), calendar
    )
    assert_same(actual, pd.DataFrame(expected[-1:]), [*FEATURES, "row_key", "identity_episode_id"])
    broken = {**raw[-1], "canonical_identity": "new-episode"}
    reset = advance(
        pd.DataFrame([broken]), pd.DataFrame(raw[-2:-1]), pd.DataFrame(expected[-2:-1]), calendar
    )
    assert reset.history_periods.iloc[0] == 0 and pd.isna(reset.lag1_delta_ore.iloc[0])


def test_outcome_same_drug_recreated_package_is_unknown(tmp_path: Path) -> None:
    frame = features().assign(selected=True, prediction_state="available")
    frame.to_parquet(tmp_path / "predictions.parquet", index=False)
    assessment = tmp_path / "assessment.json"
    frozen_json(
        assessment, {"context": context(), "predictions": pin(tmp_path / "predictions.parquet")}
    )
    official = pd.DataFrame(
        {
            "package_number": ["000001", "000002"],
            "aip_ore": [15000, 23000],
            "price_state": ["observed"] * 2,
            "identity_token": ["drug1", "drug2"],
            "created_date_token": ["20261026", "20200101"],
            "exit_date_token": ["        "] * 2,
            "dispensing_code": ["B", "B"],
        }
    )
    official.to_parquet(tmp_path / "official.parquet", index=False)
    frozen_json(
        tmp_path / "official.json",
        {"period": "2026-10-26", "rows": pin(tmp_path / "official.parquet")},
    )
    out = attach_outcome(assessment, tmp_path / "official.json")
    rows = pd.read_parquet(read(out)["rows"]["path"])
    assert rows.status.tolist() == ["identity_discontinuity_or_ambiguity", "observed"]
    assert pd.isna(rows.next_aip_ore.iloc[0])


def test_saved_dashboard_missing_and_invalid_index(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest

    missing = tmp_path / "missing.json"
    code = (
        "from pathlib import Path\nfrom bef_redbird.research.e4_dashboard import main\n"
        f"main(Path({str(missing)!r}))"
    )
    app = AppTest.from_string(code).run()
    assert not app.exception and "Choose a saved E4 index" in app.info[0].value
    missing.write_text('{"version":"wrong"}', encoding="utf-8")
    app.run()
    assert not app.exception and "Incompatible" in app.error[0].value


def test_saved_dashboard_pending_and_unavailable(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest

    frame = features().assign(
        prediction_state=["available", "excluded_origin"],
        selected=[True, False],
        p_rise=[0.4, None],
        p_fall=[0.05, None],
        next_aip_dkk=[110, None],
        selection_reason=["Threshold passed", "ineligible"],
    )
    frame.to_parquet(tmp_path / "predictions.parquet", index=False)
    frozen_json(
        tmp_path / "assessment.json",
        {
            "context": {**context(), "mode": "prospective_pending"},
            "created_utc": "2026-10-01T10:00:00Z",
            "fit_cutoff": "2026-01-01",
            "predictions": pin(tmp_path / "predictions.parquet"),
            "predicted": 1,
            "selected": 1,
            "binding": {},
        },
    )
    entry = {"label": "Synthetic pending", "assessment": pin(tmp_path / "assessment.json")}
    code = (
        "from bef_redbird.research.e4_dashboard import candidate_view\ncandidate_view("
        + repr([entry])
        + ")"
    )
    app = AppTest.from_string(code).run()
    assert not app.exception and any("Prospective" in x.value for x in app.info)
    app.radio[0].set_value("Excluded / unavailable").run()
    assert not app.exception and app.dataframe[0].value.iloc[0]["Package"] == "000002"


def test_public_prepare_rejects_unmature_direction_or_interval_state(tmp_path: Path) -> None:
    from bef_redbird.research.e4 import check_prediction_cutoffs

    interval = tmp_path / "interval.json"
    state = {"cutoff": "2026-01-01", "last_maturity": "2025-12-22"}
    frozen_json(interval, {"residual": {"2026_current": state, "2026_advance": state}})
    frozen_json(tmp_path / "bundle.json", {"amount_interval_calibration": pin(interval)})
    calibration = tmp_path / "calibration.json"
    frozen_json(calibration, {"states": {"2026": {"temperature": {"hybrid": state}}}})
    check_prediction_cutoffs(context(), tmp_path / "bundle.json", calibration)
    invalid = tmp_path / "invalid.json"
    frozen_json(
        invalid,
        {"states": {"2026": {"temperature": {"hybrid": {**state, "last_maturity": "2026-01-01"}}}}},
    )
    with pytest.raises(ResearchError, match="not_mature"):
        check_prediction_cutoffs(context(), tmp_path / "bundle.json", invalid)


def test_index_explicit_outcome_binding_and_retry_preserve_choice(tmp_path: Path) -> None:
    from bef_redbird.research.e4 import publish_index

    for name in (
        "protocol.json",
        "summary.json",
        "evaluation.json",
        "cito-comparison/comparison.json",
        "diagnostics-complete.json",
        "scenarios.json",
        "assessment.json",
        "rows.json",
    ):
        frozen_json(tmp_path / name, {"synthetic": name})
    assessment = pin(tmp_path / "assessment.json")
    frozen_json(
        tmp_path / "lifecycle.json",
        {
            "assessments": [
                {
                    "assessment": assessment,
                    "origin": "2026-10-26",
                    "mode": "prospective_pending",
                    "outcome": None,
                }
            ],
            "comparison": {"compatible": False},
        },
    )
    frozen_json(
        tmp_path / "outcome.json",
        {"binding": {"assessment": assessment}, "rows": pin(tmp_path / "rows.json")},
    )
    index = publish_index(tmp_path, tmp_path / "outcome.json")
    expected = read(index)["assessments"][0]["outcome"]
    assert expected == pin(tmp_path / "outcome.json")
    publish_index(tmp_path)
    assert read(index)["assessments"][0]["outcome"] == expected
    frozen_json(tmp_path / "other.json", {"synthetic": "different assessment"})
    frozen_json(
        tmp_path / "wrong-outcome.json",
        {
            "binding": {"assessment": pin(tmp_path / "other.json")},
            "rows": pin(tmp_path / "rows.json"),
        },
    )
    with pytest.raises(ResearchError, match="incompatible"):
        publish_index(tmp_path, tmp_path / "wrong-outcome.json")
