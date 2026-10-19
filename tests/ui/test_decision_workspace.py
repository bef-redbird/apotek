"""Decision-oriented navigation must preserve product identity and human work."""

from dataclasses import replace
from datetime import timedelta

import pytest

from bef_redbird.charts import history_chart
from bef_redbird.domain import Review
from bef_redbird.service import build_analysis
from tests.fixtures import load_analysis
from tests.ui.test_workspace import (
    candidate_table,
    loaded_app,
    select_table_product,
    synthetic_analysis,
)


def test_comparison_is_the_default_and_assessment_is_opt_in() -> None:
    analysis = synthetic_analysis(3)
    app = loaded_app(analysis, show_review=False)
    aid = analysis.analysis_id
    assert not app.toggle(key="show_review").value
    assert not any(widget.label == "Kandidat i den viste liste" for widget in app.selectbox)
    assert not any(item.label == "Vælg fra listen med tastatur" for item in app.expander)
    assert not app.text_area
    assert not any("Gem" in button.label for button in app.button)
    app.checkbox(key=f"overlay_{aid}_000001_000002").check().run()
    app.button(key="open_alternative_000001_000002").click().run()
    assert app.session_state["review"].inspected_id == "000002"
    assert not app.text_area
    assert app.session_state["review"].reviews == {}
    app.toggle(key="show_review").set_value(True).run()
    assert app.text_area(key=f"review_note_{aid}_000002").value == ""
    assert app.session_state["analysis"].analysis_id == aid
    assert not app.exception


def test_hiding_assessment_preserves_saved_reviews_and_unsaved_drafts() -> None:
    app = loaded_app(synthetic_analysis(3))
    aid = "synthetic-analysis"
    app.text_area(key=f"review_note_{aid}_000001").input("Gemt vurdering").run()
    app.checkbox(key=f"review_selected_{aid}_000001").check().run()
    next(b for b in app.button if b.label == "Gem og næste kandidat").click().run()
    app.text_area(key=f"review_note_{aid}_000002").input("Ugemt vurdering").run()
    app.selectbox(key=f"review_status_{aid}_000002").select("Undersøg nærmere").run()
    app.checkbox(key=f"review_selected_{aid}_000002").check().run()
    app.checkbox(key=f"overlay_{aid}_000002_000003").check().run()
    app.toggle(key="show_review").set_value(False).run()
    assert not app.text_area
    app.radio(key="chart_window").set_value("Seneste 26 perioder").run()
    next(b for b in app.button if b.label == "Forrige kandidat").click().run()
    app.toggle(key="show_review").set_value(True).run()
    assert app.text_area(key=f"review_note_{aid}_000001").value == "Gemt vurdering"
    next(b for b in app.button if b.label == "Næste kandidat").click().run()
    assert app.text_area(key=f"review_note_{aid}_000002").value == "Ugemt vurdering"
    assert app.selectbox(key=f"review_status_{aid}_000002").value == "Undersøg nærmere"
    assert app.checkbox(key=f"review_selected_{aid}_000002").value
    assert app.checkbox(key=f"overlay_{aid}_000002_000003").value
    review = app.session_state["review"]
    assert review.reviews == {"000001": Review("Ikke vurderet", "Gemt vurdering")}
    assert review.selected_ids == {"000001"}
    assert app.session_state["analysis"].analysis_id == aid
    assert not app.exception


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        ((10001, 10000, 10001, 10000), ("100,00 kr.", "100,01 kr.", "0,01 kr.")),
        ((10000, 10001, 10000, 10001), ("100,01 kr.", "100,01 kr.", "-0,01 kr.")),
        ((10029, 10000, 10029, 10000), ("100,00 kr.", "100,15 kr.", "0,15 kr.")),
        ((10000, 10029, 10000, 10029), ("100,29 kr.", "100,15 kr.", "-0,15 kr.")),
        ((9000, 10000, 10000, 11000), ("110,00 kr.", "100,00 kr.", "-10,00 kr.")),
        ((10000, 10000, 10000, 10000), ("100,00 kr.", "100,00 kr.", "0,00 kr.")),
        ((9000, 10000, 11000, None), ("Mangler kr.", "100,00 kr.", "Mangler kr.")),
        ((None, None, None, None), ("Mangler kr.", "Mangler kr.", "Mangler kr.")),
    ],
)
def test_comparison_uses_selected_lms_price_and_signed_median_gap(
    prices: tuple[int | None, ...], expected: tuple[str, str, str]
) -> None:
    source = synthetic_analysis(1)
    # Cito says 120 kr.; a later LMS period says 999.99 kr. Neither is a fallback.
    history = replace(
        source.history,
        periods=(*source.history.periods, source.context.current_period + timedelta(days=14)),
        prices={"000001": (*prices, 99999)},
    )
    analysis = build_analysis(source.snapshot, history, source.context, source.settings)
    features = analysis.candidates["000001"].features
    app = loaded_app(analysis)
    assert [metric.label for metric in app.metric] == [
        "Kostpris fra LMS",
        "Median i egen historik",
        "Afstand til median",
    ]
    assert tuple(metric.value for metric in app.metric) == expected
    assert app.session_state["analysis"].candidates["000001"].features == features
    if prices in ((10001, 10000, 10001, 10000), (10000, 10001, 10000, 10001)):
        assert features.median_ore == 10000.5
        assert abs(features.median_distance_dkk) == 0.005
    if prices[-1] is not None:
        assert any("Cito-kostpris og periodens AIP" in warning.value for warning in app.warning)
    else:
        assert any("aktuel AIP eller historik mangler" in caption.value for caption in app.caption)
    assert not app.exception


def test_queue_is_compact_and_searches_display_metadata() -> None:
    analysis = synthetic_analysis(3)
    analysis.history.labels["000002"] = "Eksempel 20 mg · 100 stk. · Firma B"
    app = loaded_app(analysis)
    assert any(
        "Prioritering til gennemgang — ikke dokumenteret købsgevinst." in c.value
        for c in app.caption
    )
    app.text_input(key="search").input("20 mg").run()
    queue = next(frame.value for frame in app.dataframe if "Vare" in frame.value.columns)
    assert list(queue.columns) == ["Prioritet", "Varenr.", "Vare", "Kostpris (DKK)"]
    assert len(queue) == 1
    assert queue.iloc[0]["Varenr."] == "000002"
    assert "Firma B" in queue.iloc[0]["Vare"]
    assert app.session_state["review"].queue.top_n == 50
    assert app.session_state["analysis"].analysis_id == analysis.analysis_id
    assert not app.exception


def test_unsaved_draft_survives_navigation_but_is_not_exported_as_saved() -> None:
    app = loaded_app(synthetic_analysis(3))
    aid = "synthetic-analysis"
    app.text_area(key=f"review_note_{aid}_000001").input("Afklar levering").run()
    app.selectbox(key=f"review_status_{aid}_000001").select("Undersøg nærmere").run()
    next(b for b in app.button if b.label == "Næste kandidat").click().run()
    assert app.session_state["review"].inspected_id == "000002"
    assert app.text_area(key=f"review_note_{aid}_000002").value == ""
    assert app.session_state["review"].reviews == {}
    assert any("ugemte vurderinger" in w.value for w in app.warning)
    next(b for b in app.button if b.label == "Forrige kandidat").click().run()
    assert app.text_area(key=f"review_note_{aid}_000001").value == "Afklar levering"
    assert app.selectbox(key=f"review_status_{aid}_000001").value == "Undersøg nærmere"
    next(b for b in app.button if b.label == "Gem og næste kandidat").click().run()
    review = app.session_state["review"]
    assert review.reviews == {"000001": Review("Undersøg nærmere", "Afklar levering")}
    assert review.inspected_id == "000002"
    assert not app.tabs
    assert not app.exception


def test_navigation_uses_visible_sequence_without_changing_queue() -> None:
    app = loaded_app(synthetic_analysis(3))
    app.text_input(key="search").input("000002").run()
    assert next(b for b in app.button if b.label == "Næste kandidat").disabled
    select_table_product(app, "000002")
    assert next(b for b in app.button if b.label == "Forrige kandidat").disabled
    assert next(b for b in app.button if b.label == "Gem og næste kandidat").disabled
    assert app.session_state["analysis"].ranked_ids == ("000001", "000002", "000003")
    app.text_input(key="search").input("").run()
    next(b for b in app.button if b.label == "Næste kandidat").click().run()
    assert app.session_state["review"].inspected_id == "000003"
    assert not app.exception


def test_native_selection_maps_combined_group_and_saved_status_filters() -> None:
    analysis = synthetic_analysis(4)
    item = replace(analysis.snapshot.items["000003"], group_id="002")
    analysis.snapshot.items["000003"] = item
    analysis.candidates["000003"] = replace(analysis.candidates["000003"], item=item)
    app = loaded_app(analysis)
    aid = analysis.analysis_id
    for pid in ("000002", "000003"):
        select_table_product(app, pid)
        app.selectbox(key=f"review_status_{aid}_{pid}").select("Interessant")
        app.text_area(key=f"review_note_{aid}_{pid}").input(f"Note {pid}")
        next(b for b in app.button if b.label == "Gem vurdering").click().run()
    next(s for s in app.selectbox if s.label == "Vis kun denne vurdering").select(
        "Interessant"
    ).run()
    assert tuple(candidate_table(app).value["Varenr."]) == ("000002", "000003")
    status_table_key = candidate_table(app).key
    select_table_product(app, "000003")
    assert app.session_state["review"].inspected_id == "000003"
    app.selectbox(key="group").select("001").run()
    assert candidate_table(app).key != status_table_key
    assert tuple(candidate_table(app).value["Varenr."]) == ("000002",)
    assert app.session_state["review"].inspected_id == "000003"
    select_table_product(app, "000002")
    assert app.text_area(key=f"review_note_{aid}_000002").value == "Note 000002"
    assert app.session_state["review"].reviews["000003"].note == "Note 000003"
    assert app.session_state["analysis"].ranked_ids == tuple(analysis.candidates)
    assert not app.exception


def test_all_group_members_are_comparable_without_changing_review_owner() -> None:
    analysis = load_analysis(3)
    app = loaded_app(analysis)
    aid = analysis.analysis_id
    app.text_area(key=f"review_note_{aid}_000001").input("Bevar beslutningsgrundlaget").run()
    next(b for b in app.button if b.label == "Gem vurdering").click().run()
    # Noncandidate H/K products are available in the same group checklist.
    app.checkbox(key=f"overlay_{aid}_000001_900001").check().run()
    assert app.session_state["review"].inspected_id == "000001"
    assert app.checkbox(key=f"overlay_{aid}_000001_900001").label == "900001"
    assert any(t.value.startswith("Opb. H · ") for t in app.text)
    assert "Opbevaring H" in app.checkbox(key=f"overlay_{aid}_000001_900001").help
    assert not any(b.key == "open_alternative_000001_900001" for b in app.button)
    assert app.checkbox(key=f"overlay_{aid}_000001_900002").disabled
    assert "Historik mangler" in app.checkbox(key=f"overlay_{aid}_000001_900002").help
    assert "ingen kurve kan vises" in app.checkbox(key=f"overlay_{aid}_000001_900002").help
    app.checkbox(key=f"overlay_{aid}_000001_000002").check().run()
    app.button(key="open_alternative_000001_000002").click().run()
    assert app.session_state["review"].inspected_id == "000002"
    # Missing-history candidates can still be explicitly opened for review.
    app.button(key="open_alternative_000002_900004").click().run()
    assert app.text_area(key=f"review_note_{aid}_900004").value == ""
    assert "900004" not in tuple(candidate_table(app).value["Varenr."])
    assert app.session_state["review"].inspected_id == "900004"
    select_table_product(app, "000001")
    assert app.text_area(key=f"review_note_{aid}_000001").value == "Bevar beslutningsgrundlaget"
    assert app.session_state["review"].reviews == {
        "000001": Review("Ikke vurderet", "Bevar beslutningsgrundlaget")
    }
    assert analysis.ranked_ids == ("000001", "000002", "000003")
    assert not app.exception


def test_empty_name_fallback_and_chart_identity_are_safe() -> None:
    analysis = synthetic_analysis(2)
    analysis.history.labels = {"000002": "<b>Same name</b> · 20 mg"}
    analysis.history.companies = {"000002": "<b>Firma</b>"}
    app = loaded_app(analysis)
    assert candidate_table(app).value.iloc[0]["Vare"] == "Navn ikke oplyst"
    chart = history_chart(analysis, "000001", ("000002",))
    assert [trace.meta for trace in chart.data] == ["000001", "000002"]
    assert chart.data[0].name == "000001"
    assert "&lt;b&gt;" in chart.data[1].name
    assert "&lt;b&gt;Firma&lt;/b&gt;" in chart.data[1].hovertemplate
    assert "000002" in chart.data[1].hovertemplate
    assert "<b>Firma</b>" not in chart.data[1].hovertemplate
    assert "<b>Same name</b>" not in chart.data[1].hovertemplate
    assert chart.data[0].line.dash != chart.data[1].line.dash
    assert chart.data[0].marker.symbol != chart.data[1].marker.symbol
    assert list(chart.data[1].y) == [100, 120, None, 100]


def test_six_overlapping_series_keep_distinct_styles_and_exact_source_evidence() -> None:
    analysis = synthetic_analysis(6)
    ids = tuple(analysis.candidates)
    # Equal prices must remain equal: visual separation cannot change source data.
    prices = (10001, 12029, None, 10001)
    analysis.history.prices = dict.fromkeys(ids, prices)
    analysis.history.labels = dict.fromkeys(ids, '<b>Samme navn</b> & "pakning"')
    analysis.history.companies = dict.fromkeys(ids, '<b>Firma</b> & "A"')
    analysis.history.companies[ids[-1]] = "Langt firmanavn med ekstra tekst"
    chart = history_chart(analysis, ids[0], ids[1:])
    assert len(chart.data) == 6
    assert [trace.meta for trace in chart.data] == list(ids)
    assert len({trace.line.color for trace in chart.data}) == 6
    assert len({trace.line.dash for trace in chart.data}) == 6
    assert len({trace.marker.symbol for trace in chart.data}) == 6
    # A single hover shows the identities of coincident series at that period.
    assert chart.layout.hovermode == "x unified"
    assert chart.layout.xaxis.unifiedhovertitle.text == "Periode %{x|%d-%m-%Y} · LMS"
    assert chart.layout.showlegend
    for pid, trace in zip(ids, chart.data, strict=True):
        assert pid in trace.name and pid in trace.hovertemplate
        expected_company = (
            "Langt firmanavn med e…"
            if pid == ids[-1]
            else "&lt;b&gt;Firma&lt;/b&gt; &amp; &quot;A&quot;"
        )
        assert expected_company in trace.hovertemplate
        assert "<b>Firma</b>" not in trace.hovertemplate
        assert "<b>Samme navn</b>" not in trace.hovertemplate
        assert "%{y:.2f}" in trace.hovertemplate
        assert list(trace.x) == list(analysis.history.periods)
        assert list(trace.y) == [100.01, 120.29, None, 100.01]
        assert trace.connectgaps is False
        assert trace.line.shape == "hv"
    assert analysis.history.prices == dict.fromkeys(ids, prices)


def test_unsaved_only_replacement_requires_acknowledgement() -> None:
    app = loaded_app(synthetic_analysis(3))
    app.text_area(key="review_note_synthetic-analysis_000001").input("Ikke gemt endnu").run()
    app.date_input[0].set_value(app.session_state["analysis"].context.current_period)
    next(n for n in app.number_input if n.label == "Historikvindue (perioder)").set_value(30)
    next(b for b in app.button if b.label == "Anvend indstillinger").click().run()
    assert any("aktive analyse har vurderinger" in e.value for e in app.error)
    assert app.text_area(key="review_note_synthetic-analysis_000001").value == "Ikke gemt endnu"
    assert not app.exception


def test_singleton_group_still_shows_source_context() -> None:
    app = loaded_app(synthetic_analysis(1))
    assert any("Ingen andre varer" in c.value for c in app.caption)
    assert any("000001 · Valgt kandidat" in m.value for m in app.markdown)
    assert any(t.value == "Syntetisk vare 000001" for t in app.text)
    assert any(t.value == "Historik: Fundet" for t in app.text)
    assert not any("Rolle" in frame.value.columns for frame in app.dataframe)
    assert not app.exception


def test_substitutes_use_product_id_and_company_with_full_descriptions_in_details() -> None:
    analysis = synthetic_analysis(3)
    description = "Samme lægemiddel · 20 mg · tabletter · 100 stk."
    analysis.history.labels = dict.fromkeys(analysis.history.prices, description)
    analysis.history.companies = {"000001": "Firma A", "000002": "Firma B", "000003": "Firma C"}
    app = loaded_app(analysis)
    alternatives = [c for c in app.checkbox if str(c.key).startswith("overlay_")]
    assert [c.label for c in alternatives] == ["000002", "000003"]
    assert all(description in c.help for c in alternatives)
    assert {"Firma B", "Firma C"}.issubset({t.value for t in app.text})
    assert sum(t.value == description for t in app.text) == 3
    assert any(t.value == "Cito-kostpris: 120,00 kr. · Prisklasse: A → C" for t in app.text)
    chart = history_chart(analysis, "000001", ("000002", "000003"))
    assert [trace.name for trace in chart.data] == [
        "000001 · Firma A",
        "000002 · Firma B",
        "000003 · Firma C",
    ]
    assert app.session_state["analysis"].analysis_id == analysis.analysis_id
    assert app.session_state["review"].inspected_id == "000001"
    assert not app.exception


def test_live_session_without_new_metadata_keeps_comparison_and_draft() -> None:
    analysis = synthetic_analysis(3)
    # Simulate an analysis retained in session_state across a source-code reload.
    del analysis.history.companies
    app = loaded_app(analysis)
    aid = analysis.analysis_id
    note_key = f"review_note_{aid}_000001"
    app.text_area(key=note_key).input("Bevar min åbne kladde").run()
    app.checkbox(key=f"overlay_{aid}_000001_000002").check().run()
    select_table_product(app, "000002")
    select_table_product(app, "000001")
    assert app.text_area(key=note_key).value == "Bevar min åbne kladde"
    assert app.checkbox(key=f"overlay_{aid}_000001_000002").value
    assert any(t.value == "Firma ikke oplyst" for t in app.text)
    assert app.session_state["analysis"].analysis_id == aid
    assert app.session_state["review"].inspected_id == "000001"
    assert app.session_state["review"].reviews == {}
    assert [t.name for t in history_chart(analysis, "000001", ("000002",)).data] == [
        "000001",
        "000002",
    ]
    assert not app.exception


def test_comparison_choices_survive_product_switches() -> None:
    app = loaded_app(synthetic_analysis(3))
    aid = "synthetic-analysis"
    app.checkbox(key=f"overlay_{aid}_000001_000002").check().run()
    app.checkbox(key=f"overlay_{aid}_000001_000003").check().run()
    select_table_product(app, "000002")
    assert not app.checkbox(key=f"overlay_{aid}_000002_000001").value
    assert not app.checkbox(key=f"overlay_{aid}_000002_000003").value
    select_table_product(app, "000001")
    assert app.checkbox(key=f"overlay_{aid}_000001_000002").value
    assert app.checkbox(key=f"overlay_{aid}_000001_000003").value
    app.checkbox(key=f"overlay_{aid}_000001_000002").uncheck().run()
    assert app.session_state[f"overlay_choices_{aid}"]["000001"] == ["000003"]
    assert not app.exception


def test_alternatives_are_group_scoped_and_limit_is_explicit() -> None:
    from dataclasses import replace

    analysis = synthetic_analysis(8)
    analysis.snapshot.items["000008"] = replace(analysis.snapshot.items["000008"], group_id="other")
    app = loaded_app(analysis)
    aid = analysis.analysis_id
    assert not any(widget.key == f"overlay_{aid}_000001_000008" for widget in app.checkbox)
    assert not any(str(widget.key).startswith("product_") for widget in app.selectbox)
    assert not app.multiselect
    for pid in ("000002", "000003", "000004", "000005", "000006"):
        app.checkbox(key=f"overlay_{aid}_000001_{pid}").check().run()
    assert app.checkbox(key=f"overlay_{aid}_000001_000007").disabled
    assert not app.checkbox(key=f"overlay_{aid}_000001_000006").disabled
    assert any(
        "Substitutionsalternativer: 6" in item.value
        and "5 valgt" in item.value
        and "Fem alternativer er valgt" in item.help
        for item in app.markdown
        if item.help
    )
    app.checkbox(key=f"overlay_{aid}_000001_000003").uncheck().run()
    assert not app.checkbox(key=f"overlay_{aid}_000001_000007").disabled
    app.checkbox(key=f"overlay_{aid}_000001_000007").check().run()
    assert app.session_state[f"overlay_choices_{aid}"]["000001"] == [
        "000002",
        "000004",
        "000005",
        "000006",
        "000007",
    ]
    assert app.session_state["review"].inspected_id == "000001"
    assert not app.exception


@pytest.mark.parametrize("members", [1, 3, 6, 8])
def test_alternative_count_is_actual_group_size_not_chart_limit(members: int) -> None:
    analysis = synthetic_analysis(members)
    app = loaded_app(analysis)
    aid = analysis.analysis_id
    # Narrowing the candidate queue must not narrow the substitute group.
    app.text_input(key="search").input("000001").run()
    heading = next(m for m in app.markdown if "Substitutionsalternativer:" in m.value)
    assert heading.value == f"**Substitutionsalternativer: {members - 1}** · 0 valgt"
    assert "Højst fem alternativkurver" in heading.help
    alternatives = [c for c in app.checkbox if str(c.key).startswith(f"overlay_{aid}_")]
    assert len(alternatives) == members - 1
    if alternatives:
        alternatives[0].check().run()
        heading = next(m for m in app.markdown if "Substitutionsalternativer:" in m.value)
        assert heading.value == f"**Substitutionsalternativer: {members - 1}** · 1 valgt"
    assert app.session_state["review"].inspected_id == "000001"
    assert not app.exception


def test_direct_graph_controls_preserve_the_active_review() -> None:
    app = loaded_app(synthetic_analysis(3))
    aid = "synthetic-analysis"
    note_key = f"review_note_{aid}_000001"
    app.text_area(key=note_key).input("Kladde med synlige grafvalg").run()
    app.radio(key="chart_window").set_value("Seneste 26 perioder").run()
    app.checkbox(key="retrospective").check().run()
    assert app.radio(key="chart_window").value == "Seneste 26 perioder"
    assert app.checkbox(key="retrospective").value
    assert app.text_area(key=note_key).value == "Kladde med synlige grafvalg"
    assert app.session_state["review"].inspected_id == "000001"
    assert app.session_state["analysis"].analysis_id == aid
    for text in (
        "[Se den valgte kandidats priser]",
        "[Gem din vurdering]",
        "[Til kandidatlisten]",
    ):
        assert not any(text in m.value for m in app.markdown)
    assert not app.exception


def test_native_table_selection_uses_only_visible_rows_and_preserves_outside_owner() -> None:
    app = loaded_app(synthetic_analysis(3))
    app.text_area(key="review_note_synthetic-analysis_000001").input("Bevar uden for listen").run()
    app.text_input(key="search").input("no match").run()
    assert not any(widget.label == "Kandidat i den viste liste" for widget in app.selectbox)
    assert not any(item.label == "Vælg fra listen med tastatur" for item in app.expander)
    assert not any("Vare" in frame.value.columns for frame in app.dataframe)
    app.text_input(key="search").input("000002").run()
    assert tuple(candidate_table(app).value["Varenr."]) == ("000002",)
    assert app.session_state["review"].inspected_id == "000001"
    select_table_product(app, "000002")
    assert app.session_state["review"].inspected_id == "000002"
    app.text_input(key="search").input("").run()
    select_table_product(app, "000001")
    assert (
        app.text_area(key="review_note_synthetic-analysis_000001").value == "Bevar uden for listen"
    )
    assert not app.exception


def test_prepared_export_survives_inspection_but_not_saved_review_change() -> None:
    app = loaded_app(synthetic_analysis(3))
    app.button(key="prepare_excel").click().run()
    select_table_product(app, "000002")
    assert any(item.label == "Hent Excel" for item in app.get("download_button"))
    app.text_area(key="review_note_synthetic-analysis_000002").input("Nyt grundlag").run()
    next(b for b in app.button if b.label == "Gem vurdering").click().run()
    assert not any(item.label == "Hent Excel" for item in app.get("download_button"))
    assert not app.exception
