"""Synthetic AppTest checks of view state, independent from workbook calculations."""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.charts import history_chart
from bef_redbird.domain import (
    AnalysisContext,
    AnalysisResult,
    AnalysisSettings,
    Candidate,
    CitoItem,
    CitoSnapshot,
    Features,
    HistoryDataset,
    Review,
    ReviewState,
    SourceInfo,
)
from bef_redbird.ui import _percentage_input

APP = Path(__file__).resolve().parents[2] / "app.py"


def synthetic_analysis(count: int = 120, *, rankable: bool = True) -> AnalysisResult:
    periods = tuple(date(2026, 1, 5) + timedelta(days=14 * n) for n in range(4))
    source = SourceInfo("synthetic.xlsx", "a" * 64, 100, "2026-01-01T12:00:00+00:00")
    items = {
        f"{number:06d}": CitoItem(f"{number:06d}", "", 12000, "A", "C", "001")
        for number in range(1, count + 1)
    }
    features = Features(
        n_valid=3,
        window_periods=4,
        coverage=0.75,
        current_aip_ore=10000,
        previous_change=None,
        median_ore=10000,
        median_distance_dkk=0,
        percentile=0.5,
        rise_count=1,
        adjacent_pair_count=1,
        rise_frequency=1,
        reversal_denominator=0,
        any_reversal_count=0,
        full_reversal_count=0,
        any_reversal_rate=None,
        full_reversal_rate=None,
        events=(),
    )
    candidates = {
        key: Candidate(
            item,
            features,
            index if rankable else None,
            1 if rankable else None,
            "Syntetisk begrundelse",
            ("Kort historik",),
        )
        for index, (key, item) in enumerate(items.items(), start=1)
    }
    return AnalysisResult(
        "synthetic-analysis",
        "synthetic-run",
        "2026-02-16T12:00:00+00:00",
        CitoSnapshot(source, items, [], count, count, 0),
        HistoryDataset(
            source,
            periods,
            {key: (10000, 12000, None, 10000) for key in items},
            {key: f"Syntetisk vare {key}" for key in items},
            {},
            [],
        ),
        AnalysisContext(periods[-1]),
        AnalysisSettings(),
        candidates,
        tuple(candidates) if rankable else (),
        {"eligible": count},
        [],
        {},
    )


def loaded_app(analysis: AnalysisResult | None = None) -> AppTest:
    app = AppTest.from_file(str(APP), default_timeout=20)
    app.session_state["analysis"] = analysis or synthetic_analysis()
    return app.run()


def test_empty_workspace_and_explicit_period() -> None:
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert app.date_input[0].value is None
    assert any("Upload Cito-rapport" in item.value for item in app.info)
    app.button[0].click().run()
    assert any("Vælg en bekræftet" in item.value for item in app.error)


def test_custom_queue_preserves_review_inspection_and_identity() -> None:
    app = loaded_app()
    assert not app.exception
    aid = "synthetic-analysis"
    assert app.number_input(key=f"queue_n_{aid}").value == 50
    app.number_input(key=f"queue_n_{aid}").set_value(80).run()
    assert not app.exception
    assert any("Viser 80 af 80" in item.value for item in app.caption)
    app.selectbox(key=f"product_{aid}").select("000080").run()
    app.text_area(key=f"review_note_{aid}_000080").input("Bevar min note")
    app.selectbox(key=f"review_status_{aid}_000080").select("Interessant")
    app.checkbox(key=f"review_selected_{aid}_000080").check()
    next(b for b in app.button if b.label == "Gem vurdering").click().run()
    app.number_input(key=f"queue_n_{aid}").set_value(17).run()
    review = app.session_state["review"]
    assert review.inspected_id == "000080"
    assert review.reviews["000080"] == Review("Interessant", "Bevar min note")
    assert "000080" in review.selected_ids
    assert app.session_state["analysis"].analysis_id == aid
    assert any("uden for den aktuelle" in item.value for item in app.info)
    app.selectbox(key=f"queue_mode_{aid}").select("Alle prioriterbare").run()
    assert app.session_state["review"].queue.mode == "all_rankable"
    assert any("Alle 120 prioriterbare" in item.value for item in app.caption)
    app.selectbox(key=f"queue_mode_{aid}").select("Alle kandidater").run()
    assert app.session_state["review"].queue.mode == "all_rankable"
    assert not app.exception


def test_search_does_not_promote_outside_queue_and_small_count_is_not_padded() -> None:
    app = loaded_app()
    app.text_input(key="search").input("000080").run()
    assert any("Ingen produkter i denne visning" in item.value for item in app.info)
    assert app.session_state["review"].queue.top_n == 50
    assert app.session_state["analysis"].ranked_ids[49] == "000050"
    small = loaded_app(synthetic_analysis(3))
    assert any("Viser 3 af 50" in item.value for item in small.caption)
    assert small.number_input(key="queue_n_synthetic-analysis").value == 50
    assert not small.exception


def test_failed_change_retains_coherent_analysis_and_reviews() -> None:
    app = loaded_app()
    review = ReviewState("synthetic-analysis", reviews={"000001": Review("Interessant", "Gem")})
    app.session_state["review"] = review
    app.date_input[0].set_value(date(2026, 3, 2))
    next(b for b in app.button if b.label == "Anvend indstillinger").click().run()
    assert any("aktive analyse har vurderinger" in item.value for item in app.error)
    assert app.session_state["analysis"].context.current_period == date(2026, 2, 16)
    assert app.session_state["review"].reviews["000001"].note == "Gem"
    assert any("tidligere, sammenhængende analyse" in item.value for item in app.warning)
    assert not app.exception


def test_unrankable_and_no_candidate_states() -> None:
    app = loaded_app(synthetic_analysis(3, rankable=False))
    assert any("Viser 0 af 50" in item.value for item in app.caption)
    assert any("Ingen produkter i denne visning" in item.value for item in app.info)
    app.selectbox(key="queue_mode_synthetic-analysis").select("Uden prioritet").run()
    assert any("3 synlige" in item.value for item in app.caption)
    assert not app.exception
    empty = loaded_app(synthetic_analysis(0))
    assert not empty.exception
    assert any("ingen produkter" in item.value.lower() for item in empty.info)


def test_chart_keeps_missing_slots_and_excludes_future() -> None:
    analysis = synthetic_analysis(2)
    analysis.context = AnalysisContext(analysis.history.periods[2])
    chart = history_chart(analysis, "000001", ("000002",))
    assert list(chart.data[0].y) == [100.0, 120.0, None]
    assert list(chart.data[0].x) == list(analysis.history.periods[:3])
    assert chart.data[0].connectgaps is False
    assert chart.data[0].line.shape == "hv"
    retrospective = history_chart(analysis, "000001", retrospective=True)
    assert len(retrospective.data[0].x) == 4
    assert len(retrospective.layout.shapes) == 1


def test_chart_inserts_absent_calendar_slot() -> None:
    analysis = synthetic_analysis(1)
    first, missing, third, fourth = analysis.history.periods
    analysis.history.periods = (first, third, fourth)
    analysis.history.prices["000001"] = (10000, 9000, 11000)
    chart = history_chart(analysis, "000001")
    assert list(chart.data[0].x) == [first, missing, third, fourth]
    assert list(chart.data[0].y) == [100.0, None, 90.0, 110.0]


def test_two_workspace_sessions_keep_reviews_isolated() -> None:
    left, right = loaded_app(), loaded_app()
    aid = "synthetic-analysis"
    left.text_area(key=f"review_note_{aid}_000001").input("Kun i venstre session")
    next(b for b in left.button if b.label == "Gem vurdering").click().run()
    right.run()
    assert left.session_state["review"].reviews["000001"].note == "Kun i venstre session"
    assert right.session_state["review"].reviews == {}
    assert not left.exception and not right.exception


def test_screening_precision_and_limited_denominator() -> None:
    assert _percentage_input(2.8) == "0.028"
    analysis = synthetic_analysis(1)
    candidate = analysis.candidates["000001"]
    analysis.candidates["000001"] = replace(
        candidate,
        features=replace(
            candidate.features,
            median_ore=10000.5,
            median_distance_dkk=0.005,
            reversal_denominator=1,
            any_reversal_count=1,
            any_reversal_rate=1.0,
        ),
    )
    analysis.snapshot.incomplete_groups.add("001")
    app = loaded_app(analysis)
    assert any("0,005 DKK" in item.value for item in app.text)
    assert any("Begrænset grundlag" in item.value for item in app.text)
    assert any("uafklarede rækker i gruppen" in item.value for item in app.warning)
    assert not app.exception


def test_invalid_queue_retains_last_valid_view() -> None:
    app = loaded_app()
    app.number_input(key="queue_n_synthetic-analysis").set_value(0).run()
    assert app.session_state["review"].queue.top_n == 50
    assert any("Viser 50 af 50" in item.value for item in app.caption)
    assert not app.exception


def test_unexpected_failure_is_safe_and_preserves_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        raise ValueError("SYNTHETIC_PRIVATE_ROW_MUST_NOT_APPEAR")

    app = loaded_app()
    monkeypatch.setattr("bef_redbird.service.build_analysis", unexpected)
    app.date_input[0].set_value(date(2026, 3, 2))
    next(b for b in app.button if b.label == "Anvend indstillinger").click().run()
    assert app.session_state["analysis"].analysis_id == "synthetic-analysis"
    assert any("Fejlreference" in item.value for item in app.error)
    assert all("SYNTHETIC_PRIVATE_ROW" not in item.value for item in app.error)
    assert not app.exception


def test_restored_review_synchronizes_existing_frontend_preferences() -> None:
    app = loaded_app()
    aid = "synthetic-analysis"
    app.number_input(key=f"queue_n_{aid}").set_value(17).run()
    restored = ReviewState(aid)
    restored.queue = replace(restored.queue, top_n=80)
    restored.reviews["000001"] = Review("Interessant", "Gendannet note")
    restored.inspected_id = "000001"
    app.session_state["review"] = restored
    app.session_state["sync_review_widgets"] = True
    app.run()
    assert app.number_input(key=f"queue_n_{aid}").value == 80
    assert app.text_area(key=f"review_note_{aid}_000001").value == "Gendannet note"
    assert not app.exception


def test_render_failure_cannot_leak_source_values(monkeypatch, caplog) -> None:
    def unexpected(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_VALUE")

    monkeypatch.setattr("bef_redbird.ui._detail", unexpected)
    app = loaded_app()
    assert not app.exception
    assert any("Fejlreference" in item.value for item in app.error)
    assert "SYNTHETIC_PRIVATE_VALUE" not in caplog.text
    assert all("SYNTHETIC_PRIVATE_VALUE" not in item.value for item in app.error)
