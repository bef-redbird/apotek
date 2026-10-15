"""Decision-oriented navigation must preserve product identity and human work."""

from bef_redbird.charts import history_chart
from bef_redbird.domain import Review
from tests.fixtures import load_analysis
from tests.ui.test_workspace import loaded_app, synthetic_analysis


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
    assert "Firma B" in app.selectbox(key="product_synthetic-analysis").options[1]
    assert app.session_state["review"].queue.top_n == 50
    assert app.session_state["analysis"].analysis_id == analysis.analysis_id
    assert not app.exception


def test_unsaved_draft_survives_navigation_but_is_not_exported_as_saved() -> None:
    app = loaded_app(synthetic_analysis(3))
    aid = "synthetic-analysis"
    app.text_area(key=f"review_note_{aid}_000001").input("Afklar levering").run()
    app.selectbox(key=f"review_status_{aid}_000001").select("Undersøg nærmere").run()
    next(b for b in app.button if b.label == "Næste").click().run()
    assert app.session_state["review"].inspected_id == "000002"
    assert app.text_area(key=f"review_note_{aid}_000002").value == ""
    assert app.session_state["review"].reviews == {}
    assert any("ugemte vurderinger" in w.value for w in app.warning)
    next(b for b in app.button if b.label == "Forrige").click().run()
    assert app.text_area(key=f"review_note_{aid}_000001").value == "Afklar levering"
    assert app.selectbox(key=f"review_status_{aid}_000001").value == "Undersøg nærmere"
    next(b for b in app.button if b.label == "Gem og næste").click().run()
    review = app.session_state["review"]
    assert review.reviews == {"000001": Review("Undersøg nærmere", "Afklar levering")}
    assert review.inspected_id == "000002"
    assert app.session_state[f"detail_tabs_{aid}"] == "Pris og alternativer"
    assert not app.exception


def test_navigation_uses_visible_sequence_without_changing_queue() -> None:
    app = loaded_app(synthetic_analysis(3))
    app.text_input(key="search").input("000002").run()
    assert next(b for b in app.button if b.label == "Næste").disabled
    app.selectbox(key="product_synthetic-analysis").select("000002").run()
    assert next(b for b in app.button if b.label == "Forrige").disabled
    assert next(b for b in app.button if b.label == "Gem og næste").disabled
    assert app.session_state["analysis"].ranked_ids == ("000001", "000002", "000003")
    app.text_input(key="search").input("").run()
    next(b for b in app.button if b.label == "Næste").click().run()
    assert app.session_state["review"].inspected_id == "000003"
    assert not app.exception


def test_all_group_members_are_comparable_without_changing_review_owner() -> None:
    analysis = load_analysis(3)
    app = loaded_app(analysis)
    aid = analysis.analysis_id
    comparison = f"comparison_{aid}_000001"
    app.text_area(key=f"review_note_{aid}_000001").input("Bevar beslutningsgrundlaget").run()
    next(b for b in app.button if b.label == "Gem vurdering").click().run()
    assert any("900002" in option for option in app.selectbox(key=comparison).options)
    app.selectbox(key=comparison).select("900001").run()
    assert app.session_state["review"].inspected_id == "000001"
    assert any("Opbevaring H" in c.value for c in app.caption)
    assert not any(b.label == "Åbn alternativ som kandidat" for b in app.button)
    app.selectbox(key=comparison).select("900002").run()
    assert any("Historik mangler for dette alternativ" in w.value for w in app.warning)
    app.selectbox(key=comparison).select("000002").run()
    next(b for b in app.button if b.label == "Åbn alternativ som kandidat").click().run()
    assert app.session_state["review"].inspected_id == "000002"
    app.selectbox(key=f"product_{aid}").select("900004").run()
    assert app.text_area(key=f"review_note_{aid}_900004").value == ""
    app.selectbox(key=f"product_{aid}").select("000001").run()
    assert app.text_area(key=f"review_note_{aid}_000001").value == "Bevar beslutningsgrundlaget"
    assert app.session_state["review"].reviews == {
        "000001": Review("Ikke vurderet", "Bevar beslutningsgrundlaget")
    }
    assert analysis.ranked_ids == ("000001", "000002", "000003")
    assert not app.exception


def test_empty_name_fallback_and_chart_identity_are_safe() -> None:
    analysis = synthetic_analysis(2)
    analysis.history.labels = {"000002": "<b>Same name</b> · 20 mg"}
    app = loaded_app(analysis)
    assert "Navn ikke oplyst" in app.selectbox(key="product_synthetic-analysis").options[0]
    chart = history_chart(analysis, "000001", ("000002",))
    assert [trace.meta for trace in chart.data] == ["000001", "000002"]
    assert "&lt;b&gt;" in chart.data[1].name
    assert "20 mg" in chart.data[1].name
    assert chart.data[0].line.dash != chart.data[1].line.dash
    assert chart.data[0].marker.symbol != chart.data[1].marker.symbol
    assert list(chart.data[1].y) == [100, 120, None, 100]


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
    group = next(frame.value for frame in app.dataframe if "Rolle" in frame.value.columns)
    assert len(group) == 1
    assert group.iloc[0]["Rolle"] == "Valgt kandidat"
    assert group.iloc[0]["Vare"].startswith("000001 · ")
    assert group.iloc[0]["Historik"] == "Fundet"
    assert not app.exception


def test_comparison_choices_survive_product_switches() -> None:
    app = loaded_app(synthetic_analysis(3))
    aid = "synthetic-analysis"
    app.selectbox(key=f"comparison_{aid}_000001").select("000002").run()
    app.multiselect(key=f"overlay_{aid}_000001").select("000003").run()
    app.selectbox(key=f"product_{aid}").select("000002").run()
    assert app.selectbox(key=f"comparison_{aid}_000002").value is None
    assert app.multiselect(key=f"overlay_{aid}_000002").value == []
    app.selectbox(key=f"product_{aid}").select("000001").run()
    assert app.selectbox(key=f"comparison_{aid}_000001").value == "000002"
    assert app.multiselect(key=f"overlay_{aid}_000001").value == ["000003"]
    assert not app.exception


def test_prepared_export_survives_inspection_but_not_saved_review_change() -> None:
    app = loaded_app(synthetic_analysis(3))
    app.button(key="prepare_excel").click().run()
    app.selectbox(key="product_synthetic-analysis").select("000002").run()
    assert any(item.label == "Hent Excel" for item in app.get("download_button"))
    app.text_area(key="review_note_synthetic-analysis_000002").input("Nyt grundlag").run()
    next(b for b in app.button if b.label == "Gem vurdering").click().run()
    assert not any(item.label == "Hent Excel" for item in app.get("download_button"))
    assert not app.exception
