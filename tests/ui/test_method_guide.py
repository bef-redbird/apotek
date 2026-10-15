"""Rendered method guidance must describe the applied result in every data state."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.domain import (
    AnalysisContext,
    AnalysisResult,
    AnalysisSettings,
    CitoItem,
    CitoSnapshot,
    HistoryDataset,
    SourceInfo,
)
from bef_redbird.service import build_analysis


def _show_guide() -> None:
    import streamlit as st

    from bef_redbird.method_guide import render_method_guide

    render_method_guide(st.session_state["analysis"])


def _analysis(*, empty: bool = False, missing_current: bool = False) -> AnalysisResult:
    periods = tuple(date(2026, 1, 5) + timedelta(days=14 * n) for n in range(5))
    source = SourceInfo("synthetic.xlsx", "a" * 64, 100, "2026-01-01T12:00:00+00:00")
    item = CitoItem("000001", "", 10000, "A", "C", "001")
    snapshot = CitoSnapshot(source, {} if empty else {item.product_id: item}, [], 1, 1, 0)
    history = HistoryDataset(
        source,
        () if empty else periods,
        {}
        if empty
        else {item.product_id: (12000, 13000, 14000, None if missing_current else 10000, 18000)},
        {},
        {},
        [],
    )
    return build_analysis(
        snapshot,
        history,
        AnalysisContext(periods[-2]),
        AnalysisSettings(4, 3),
    )


def _render(analysis: AnalysisResult) -> AppTest:
    app = AppTest.from_function(_show_guide)
    app.session_state["analysis"] = analysis
    return app.run()


def _body(app: AppTest) -> str:
    return "\n".join(element.value for element in (*app.markdown, *app.text, *app.caption))


def test_guide_tracks_applied_settings_instead_of_pending_controls() -> None:
    analysis = _analysis()
    app = _render(analysis)
    assert not app.exception
    assert "Historikvindue: 4 perioder; 4 tilgængelige" in _body(app)

    # Simulate pending controls disagreeing with a newly applied result.
    changed = replace(
        analysis,
        settings=AnalysisSettings(3, 2, "0.07", "0.4", 3, 4),
    )
    app.session_state["analysis"] = changed
    app.session_state["lookback_periods"] = 26
    app.session_state["minimum_valid_prices"] = 20
    app.session_state["rise_threshold"] = "0.05"
    app.run()
    assert not app.exception
    body = _body(app)
    assert "Historikvindue: 3 perioder; 3 tilgængelige" in body
    assert "Mindst 2 gyldige priser" in body
    assert "Væsentlig stigning: strengt over 7 %" in body
    assert "Lav prisposition: percentil højst 40 %" in body
    assert "Gentagne stigninger: mindst 3" in body
    assert "Procentvis tilbagefaldsvisning: mindst 4 observerbare efterperioder" in body
    assert "Anvendt vindue: 19-01-2026 – 16-02-2026." in body
    assert "Historikvindue: 26" not in body


def test_boundary_definitions_and_relationships_are_available_in_native_disclosures() -> None:
    app = _render(_analysis())
    assert not app.exception
    assert len(app.expander) == 5
    body = _body(app)
    for definition in (
        "Varenummer forbinder til egen LMS-historik",
        "præcis 5 % ikke",
        "præcis 14 dage",
        "0,5 × antal priser lig aktuel AIP",
        "en konstant serie giver 50 %",
        "Afledte halve øre",
        "Præcis 1.000 kr. er udenfor",
        "kun stigninger med en observeret, gyldig efterperiode",
        "ikke 0 %",
        "(B − C) / (B − A)",
        "C ≤ A",
        "medianafstand faldende",
        "prispercentil stigende",
        "varenummer stigende",
        "Retrospektiv diagramvisning",
        "Søgning og tabelsortering ændrer ikke eksportomfanget",
    ):
        assert definition in body


@pytest.mark.parametrize("empty", [False, True])
def test_guidance_survives_no_rankable_candidates_and_empty_analysis(empty: bool) -> None:
    analysis = _analysis(empty=empty, missing_current=True)
    assert not analysis.ranked_ids
    assert bool(analysis.candidates) == (not empty)
    app = _render(analysis)
    assert not app.exception
    assert len(app.expander) == 5
    assert "Kan ikke prioriteres" in _body(app)
    assert "AIP i den aktuelle periode findes og er positiv" in _body(app)
    if empty:
        assert "0 tilgængelige" in _body(app)


def test_dynamic_method_metadata_is_plain_text() -> None:
    unsafe = '<img src=x onerror="alert(1)">[external](https://invalid.example)'
    analysis = replace(_analysis(), versions={"ranking": unsafe})
    app = _render(analysis)
    assert not app.exception
    assert any(unsafe in element.value for element in app.text)
    assert all(unsafe not in element.value for element in app.markdown)
