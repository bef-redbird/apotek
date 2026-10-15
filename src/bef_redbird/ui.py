"""Danish temporary-session review workspace; calculations live outside the UI."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from functools import partial
from uuid import uuid4

import pandas as pd
import streamlit as st

from bef_redbird.charts import history_chart
from bef_redbird.domain import (
    STATUSES,
    AnalysisContext,
    AnalysisResult,
    AnalysisSettings,
    DataError,
    QueueSettings,
    Review,
    ReviewState,
    select_queue,
)

MODES = ("Top N", "Alle prioriterbare", "Alle kandidater", "Uden prioritet", "Dataproblemer")


def _percentage_input(value: float) -> str:
    return str(Decimal(str(value)) / Decimal(100))


def _safe_failure() -> str:
    reference = uuid4().hex[:8]
    logging.getLogger(__name__).warning("workspace_operation_failed correlation_id=%s", reference)
    return f"Handlingen kunne ikke gennemføres. Prøv igen. Fejlreference: {reference}"


def _money(value: int | float | None, decimals: int = 2) -> str:
    return (
        "Mangler"
        if value is None
        else f"{value / 100:,.{decimals}f}".translate(str.maketrans(",.", ".,"))
    )


def _percent(value: float | None) -> str:
    return "Mangler" if value is None else f"{value:.1%}".replace(".", ",")


def _analysis() -> AnalysisResult | None:
    return st.session_state.get("analysis")


def _review(analysis: AnalysisResult) -> ReviewState:
    current = st.session_state.get("review")
    if not isinstance(current, ReviewState) or current.analysis_id != analysis.analysis_id:
        current = ReviewState(analysis.analysis_id)
        st.session_state["review"] = current
    return current


def _reset_view_widgets(*, replacing_analysis: bool = False) -> None:
    prefixes: tuple[str, ...] = ("product_", "review_", "queue_", "table_")
    if replacing_analysis:
        prefixes += ("overlay_", "detail_tabs_")
    for key in list(st.session_state):
        if str(key).startswith(prefixes):
            del st.session_state[key]


def _inputs() -> None:
    from bef_redbird.imports import parse_cito, parse_lms_history
    from bef_redbird.service import build_analysis

    analysis = _analysis()
    with st.sidebar:
        st.header("Datagrundlag")
        st.caption("Midlertidig session · ingen permanent lagring")
        cito = st.file_uploader("Cito-rapport", type=["xlsx", "xls"], key="cito_upload")
        history = st.file_uploader("LMS-prishistorik", type=["xlsx"], key="history_upload")
        period = st.date_input("Bekræftet aktuel periode", value=None, key="period")
        st.caption("Vælg rapportens prisperiodestart. Dagens dato udfyldes ikke automatisk.")
        with st.expander("Modtagelsestid og metode"):
            received_c = st.text_input("Cito modtaget (ISO-tid med tidszone)")
            received_h = st.text_input("LMS modtaget (ISO-tid med tidszone)")
            window = st.number_input("Historikvindue (perioder)", 2, 520, 26, step=1)
            minimum = st.number_input("Mindste antal gyldige priser", 1, 520, 20, step=1)
            rise = st.number_input("Grænse for prisstigning (%)", 0.0, 1000.0, 5.0)
            percentile = st.number_input("Lav prispercentil (%)", 0.0, 100.0, 25.0)
            repeated = st.number_input("Mindste antal prisstigninger", 0, 520, 2, step=1)
            events = st.number_input("Mindste hændelser før visning af andel", 1, 520, 5, step=1)
        replacement_ack = (
            st.checkbox(
                "Jeg accepterer at erstatte den aktive vurdering efter at have hentet en "
                "sessionskopi eller bevidst kassere den",
                key="replace_ack",
            )
            if analysis
            else False
        )
        apply = st.button(
            "Anvend indstillinger" if analysis else "Indlæs og kontroller",
            type="primary",
            width="stretch",
        )
        if apply:
            try:
                if period is None or not isinstance(period, date):
                    raise DataError("Vælg en bekræftet aktuel periode før indlæsning.")
                if analysis is None and (cito is None or history is None):
                    raise DataError("Upload både Cito-rapport og LMS-prishistorik.")
                settings = AnalysisSettings(
                    lookback_periods=int(window),
                    minimum_valid_prices=int(minimum),
                    rise_threshold=_percentage_input(rise),
                    low_price_percentile=_percentage_input(percentile),
                    repeated_rise_minimum=int(repeated),
                    minimum_events_for_rate_display=int(events),
                )
                context = AnalysisContext(period, received_c or None, received_h or None)
                c_hash = (
                    hashlib.sha256(cito.getbuffer()).hexdigest()
                    if cito
                    else (analysis.snapshot.source.sha256 if analysis else "")
                )
                h_hash = (
                    hashlib.sha256(history.getbuffer()).hexdigest()
                    if history
                    else (analysis.history.source.sha256 if analysis else "")
                )
                identical = bool(
                    analysis
                    and c_hash == analysis.snapshot.source.sha256
                    and h_hash == analysis.history.source.sha256
                    and context == analysis.context
                    and settings == analysis.settings
                )
                if identical:
                    st.session_state.pop("import_error", None)
                    st.success("De samme kilder og indstillinger er allerede aktive.")
                else:
                    review = _review(analysis) if analysis else None
                    if review and (review.reviews or review.selected_ids) and not replacement_ack:
                        raise DataError(
                            "Den aktive analyse har vurderinger eller eksportvalg. Hent en "
                            "sessionskopi og bekræft erstatning, eller vælg bevidst at kassere dem."
                        )
                    with st.spinner("Kontrollerer kilder og beregner analyse …"):
                        if analysis and c_hash == analysis.snapshot.source.sha256:
                            snapshot = analysis.snapshot
                        else:
                            if cito is None:
                                raise DataError("Upload en Cito-rapport.")
                            snapshot = parse_cito(cito.getvalue(), cito.name)
                        if analysis and h_hash == analysis.history.source.sha256:
                            dataset = analysis.history
                        else:
                            if history is None:
                                raise DataError("Upload LMS-prishistorik.")
                            dataset = parse_lms_history(history.getvalue(), history.name)
                        result = build_analysis(snapshot, dataset, context, settings)
                    st.session_state["analysis"] = result
                    st.session_state["review"] = ReviewState(
                        result.analysis_id, queue=review.queue if review else QueueSettings()
                    )
                    st.session_state.pop("import_error", None)
                    st.session_state["reset_ack"] = True
                    st.session_state.pop("prepared_export", None)
                    _reset_view_widgets(replacing_analysis=True)
                    st.rerun()
            except DataError as exc:
                st.session_state["import_error"] = str(exc)
            except Exception:
                st.session_state["import_error"] = _safe_failure()
        if st.session_state.get("import_error"):
            st.error(st.session_state["import_error"])
        if analysis and not st.session_state.get("import_error"):
            different = (
                (
                    cito is not None
                    and hashlib.sha256(cito.getbuffer()).hexdigest()
                    != analysis.snapshot.source.sha256
                )
                or (
                    history is not None
                    and hashlib.sha256(history.getbuffer()).hexdigest()
                    != analysis.history.source.sha256
                )
                or (period is not None and period != analysis.context.current_period)
                or asdict(analysis.settings)
                != asdict(
                    AnalysisSettings(
                        int(window),
                        min(int(minimum), int(window)),
                        _percentage_input(rise),
                        _percentage_input(percentile),
                        int(repeated),
                        int(events),
                    )
                )
                or (received_c or None) != analysis.context.cito_received_at
                or (received_h or None) != analysis.context.history_received_at
            )
            st.session_state["pending_inputs"] = different


def _header(analysis: AnalysisResult) -> None:
    st.caption("Prioritering til gennemgang — ikke dokumenteret købsgevinst.")
    if st.session_state.get("import_error"):
        st.warning(
            "Ny indlæsning mislykkedes. Du ser stadig den tidligere, sammenhængende analyse "
            "med kilderne nedenfor."
        )
    elif st.session_state.get("pending_inputs"):
        st.warning(
            "Der er ændrede input. Den viste analyse bruger fortsat de anvendte kilder "
            "og indstillinger; tryk Anvend indstillinger for at anvende ændringerne."
        )
    through = max(analysis.history.periods) if analysis.history.periods else None
    groups = len(
        {c.item.group_id for c in analysis.candidates.values() if c.item.group_id is not None}
    )
    st.caption(
        f"Aktuel periode: {analysis.context.current_period:%d-%m-%Y} · "
        f"Kommende kodeperiode: {analysis.context.current_period + timedelta(days=14):%d-%m-%Y}  \n"
        f"{len(analysis.candidates)} kandidater · {len(analysis.ranked_ids)} kan prioriteres · "
        f"{groups} grupper · {len(analysis.issues)} dataproblemer · "
        + (f"LMS til {through:%d-%m-%Y}" if through else "Ingen LMS-prisperioder")
    )
    if through and through < analysis.context.current_period:
        st.warning("LMS-historikken slutter før den bekræftede periode; aktuelle AIP kan mangle.")
    for issue in analysis.issues:
        if issue.code in ("period_alignment", "older_period_match", "irregular_calendar"):
            st.warning(issue.message)
    with st.expander("Kilder, metode og datakontrol"):
        st.caption(
            "Uvaliderede screeningindstillinger. Lav historisk pris kan skyldes "
            "et varigt prisfald. Kommende C er ikke dokumentation for en stigning "
            "i varens egen AIP."
        )
        st.caption(f"Dateret Cito-snapshot: {analysis.context.current_period:%d-%m-%Y}.")
        for label, source in (("Cito", analysis.snapshot.source), ("LMS", analysis.history.source)):
            st.text(f"{label}: {source.name} · {source.size:,} bytes")
            st.code(source.sha256, language=None)
        st.text(
            f"Cito-ark: {analysis.snapshot.sheet_name} · "
            f"{analysis.snapshot.input_rows} kilderækker · "
            f"{analysis.snapshot.duplicate_rows} ens dubletter"
        )
        if analysis.history.periods:
            st.write(
                f"LMS-periodeinterval: {min(analysis.history.periods):%d-%m-%Y} til "
                f"{max(analysis.history.periods):%d-%m-%Y}"
            )
        st.json(analysis.manifest(), expanded=False)
        st.dataframe(
            pd.DataFrame(
                [
                    {"Kontrol": label, "Antal": count}
                    for label, count in analysis.filter_counts.items()
                ]
            ),
            hide_index=True,
            width="stretch",
        )


def _inspect_table(key: str, ids: tuple[str, ...], analysis_id: str) -> None:
    review = st.session_state.get("review")
    if not isinstance(review, ReviewState) or review.analysis_id != analysis_id:
        return
    event = st.session_state.get(key, {})
    rows = event.get("selection", {}).get("rows", [])
    if rows and 0 <= rows[0] < len(ids):
        review.inspected_id = ids[rows[0]]
        st.session_state[f"product_{analysis_id}"] = review.inspected_id


def _queue(analysis: AnalysisResult, review: ReviewState) -> None:
    st.subheader("Kandidater")
    mode_column, count_column = st.columns([2, 1])
    mode = mode_column.selectbox(
        "Visning",
        MODES,
        index=(1 if review.queue.mode == "all_rankable" else 0),
        key=f"queue_mode_{analysis.analysis_id}",
    )
    n = count_column.number_input(
        "Antal kandidater",
        min_value=1,
        max_value=1_000_000_000,
        value=review.queue.top_n,
        step=1,
        key=f"queue_n_{analysis.analysis_id}",
        help="Bevares som ønske, også hvis færre produkter kan prioriteres.",
    )
    if type(n) is not int or not 1 <= n <= 1_000_000_000:
        st.warning("Antal kandidater skal være et positivt heltal. Den seneste gyldige kø bevares.")
        n = review.queue.top_n
    else:
        queue_mode = review.queue.mode
        if mode in ("Top N", "Alle prioriterbare"):
            queue_mode = "all_rankable" if mode == "Alle prioriterbare" else "top_n"
        review.queue = QueueSettings(queue_mode, n)
    queue_ids = select_queue(analysis, review.queue)
    queue_description = (
        f"Viser {len(queue_ids)} af {n} ønskede kandidater — "
        f"{len(analysis.ranked_ids)} kan prioriteres."
        if review.queue.mode == "top_n"
        else f"Alle {len(queue_ids)} prioriterbare kandidater."
    )
    if mode == "Dataproblemer":
        st.caption(queue_description)
        _issues(analysis)
        return
    ids = (
        tuple(analysis.candidates)
        if mode == "Alle kandidater"
        else (
            tuple(k for k, value in analysis.candidates.items() if value.rank is None)
            if mode == "Uden prioritet"
            else queue_ids
        )
    )
    search_column, group_column = st.columns([2, 1])
    search = search_column.text_input("Søg i den valgte visning", key="search")
    groups = sorted(
        {group_id for k in ids if (group_id := analysis.candidates[k].item.group_id) is not None}
    )
    group = group_column.selectbox(
        "Gruppefilter i visningen", ["Alle grupper", *groups], key="group"
    )
    ids = tuple(
        k
        for k in ids
        if (
            not search
            or search.casefold() in (k + " " + analysis.history.labels.get(k, "")).casefold()
        )
        and (group == "Alle grupper" or analysis.candidates[k].item.group_id == group)
    )
    st.caption(
        f"{queue_description} {len(ids)} synlige efter søgning og gruppefilter. "
        "Søgning ændrer ikke eksportkøen."
    )
    if not ids:
        st.info("Ingen produkter i denne visning. Se Alle kandidater eller Dataproblemer.")
        return
    rows = []
    for product_id in ids:
        candidate = analysis.candidates[product_id]
        rows.append(
            {
                "Prioritet": candidate.rank,
                "Varenr.": product_id,
                "Navn": analysis.history.labels.get(product_id, ""),
                "Kostpris (DKK)": candidate.item.current_cost_ore / 100,
                "Koder": f"{candidate.item.current_code}→{candidate.item.upcoming_code}",
                "Afstand median (DKK)": candidate.features.median_distance_dkk,
                "Vurdering": review.reviews.get(product_id, Review()).status,
            }
        )
    rendered = json.dumps([mode, ids], ensure_ascii=False).encode()
    generation = hashlib.sha256(rendered).hexdigest()[:16]
    key = f"table_{analysis.analysis_id}_{generation}"
    frame = pd.DataFrame(rows)
    frame["Prioritet"] = frame["Prioritet"].astype("Int64")
    # The callback captures exactly the IDs supplied to this rendered dataframe.
    st.dataframe(
        frame.style.format(
            precision=2,
            decimal=",",
            thousands=".",
            na_rep="Mangler",
            subset=["Kostpris (DKK)", "Afstand median (DKK)"],
        ),
        hide_index=True,
        width="stretch",
        height=320,
        selection_mode="single-row",
        on_select=partial(_inspect_table, key, ids, analysis.analysis_id),
        key=key,
        column_config={
            "Prioritet": st.column_config.NumberColumn(width=85),
            "Varenr.": st.column_config.TextColumn(width=90),
            "Navn": st.column_config.TextColumn(width=150),
            "Kostpris (DKK)": st.column_config.NumberColumn(width=110),
            "Koder": st.column_config.TextColumn(width=70),
            "Afstand median (DKK)": st.column_config.NumberColumn(width=120),
            "Vurdering": st.column_config.TextColumn(width=145),
        },
    )


def _issues(analysis: AnalysisResult) -> None:
    if not analysis.issues:
        st.info("Ingen registrerede dataproblemer.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Kode": issue.code,
                    "Varenr.": issue.product_id,
                    "Besked": issue.message,
                    "Niveau": issue.level,
                    "Række": issue.row,
                    "Kolonne": issue.column,
                }
                for issue in analysis.issues
            ]
        ),
        hide_index=True,
        width="stretch",
    )


def _detail(analysis: AnalysisResult, review: ReviewState) -> None:
    if not analysis.candidates:
        return
    st.subheader("Vare og vurdering")
    ids = tuple(sorted(analysis.candidates))
    if review.inspected_id not in ids:
        review.inspected_id = analysis.ranked_ids[0] if analysis.ranked_ids else ids[0]
    selector = f"product_{analysis.analysis_id}"
    if selector not in st.session_state:
        st.session_state[selector] = review.inspected_id
    product_id = st.selectbox(
        "Vælg varenr. (alle kandidater)",
        ids,
        key=selector,
        help="Tast et varenummer. Valget virker også uden tabelmarkering.",
    )
    review.inspected_id = product_id
    if product_id not in select_queue(analysis, review.queue):
        st.info(
            "Den inspicerede vare er uden for den aktuelle prioriteringskø. Vurderingen bevares."
        )
    candidate = analysis.candidates[product_id]
    st.text(f"{product_id} · {analysis.history.labels.get(product_id, 'Intet navn leveret')}")
    st.caption(
        f"Prioritet: {candidate.rank or 'Uden prioritet'} · "
        f"{candidate.item.current_code}→{candidate.item.upcoming_code} · "
        f"Kostpris {_money(candidate.item.current_cost_ore)} DKK · "
        f"Gruppe {candidate.item.group_id or 'mangler'}"
    )
    st.write(candidate.reason)
    if candidate.flags:
        st.warning("Datamærker: " + ", ".join(candidate.flags))
    if candidate.item.group_id in analysis.snapshot.incomplete_groups:
        st.warning("Der er uafklarede rækker i gruppen. Gruppeoverblikket kan være ufuldstændigt.")
    group = analysis.group_members(product_id)
    found = [
        item.product_id
        for item in group
        if item.product_id != product_id and item.product_id in analysis.history.prices
    ]
    history_tab, alternatives_tab, review_tab = st.tabs(
        ["Prishistorik", "Alternativer", "Vurdering"],
        key=f"detail_tabs_{analysis.analysis_id}",
        on_change="rerun",
    )
    with history_tab:
        with st.expander("Grafvisning"):
            overlays = st.multiselect(
                "Vis fundne gruppealternativer i grafen (højst 5)",
                found,
                max_selections=5,
                key=f"overlay_{analysis.analysis_id}_{product_id}",
            )
            view = st.radio(
                "Grafens visningsvindue",
                ["Hele historikken", "Seneste 26 perioder", "Seneste 52 perioder"],
                horizontal=True,
                key="chart_window",
            )
            retrospective = st.checkbox(
                "Retrospektiv visning (også senere priser)", key="retrospective"
            )
        limit = {"Hele historikken": None, "Seneste 26 perioder": 26, "Seneste 52 perioder": 52}[
            view
        ]
        st.plotly_chart(
            history_chart(
                analysis,
                product_id,
                tuple(overlays),
                retrospective=retrospective,
                view_periods=limit,
            ),
            width="stretch",
            key=f"history_{product_id}",
        )
        st.caption(
            "AIP pr. pakning er ikke dokumentation for sammenlignelig enhedspris eller "
            "udleveringspris. Gruppefuldstændighed er ikke verificeret. "
            "Kun fundne medlemmer fra den aktuelle Cito-rapport vises."
        )
        with st.expander("Historiske nøgletal og hændelser"):
            features = candidate.features
            st.write(
                f"Gyldige priser: {features.n_valid}/{features.window_periods} · "
                f"Dækning: {_percent(features.coverage)} · "
                f"Aktuel AIP: {_money(features.current_aip_ore)} DKK"
            )
            st.write(
                f"Egen median: {_money(features.median_ore, 3)} DKK · "
                f"Prispercentil: {_percent(features.percentile)} · "
                f"Seneste ændring: {_percent(features.previous_change)}"
            )
            distance = features.median_distance_dkk
            distance_text = "Mangler" if distance is None else f"{distance:.3f}".replace(".", ",")
            st.text(f"Afstand til egen historiske median: {distance_text} DKK")
            st.write(
                f"Prisstigninger: {features.rise_count}/{features.adjacent_pair_count} gyldige "
                f"nabopar. Fald efter stigning: {features.any_reversal_count}/"
                f"{features.reversal_denominator}; fuldt tilbagefald: "
                f"{features.full_reversal_count}/{features.reversal_denominator}."
            )
            st.caption("Små nævnere giver usikre historiske andele; tallene er ikke en prognose.")
            if features.reversal_denominator == 0:
                st.text("Ingen observerbare efterperioder; andele kan ikke beregnes (nævner 0).")
            elif features.reversal_denominator < analysis.settings.minimum_events_for_rate_display:
                st.text(
                    "Begrænset grundlag: andele skjules under den anvendte mindstegrænse "
                    f"på {analysis.settings.minimum_events_for_rate_display} "
                    "observerbare hændelser."
                )
            else:
                st.write(
                    f"Andel med fald: {_percent(features.any_reversal_rate)} · "
                    f"Fuldt tilbagefald: {_percent(features.full_reversal_rate)}"
                )
            if features.events:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Periode": event.period.strftime("%d-%m-%Y"),
                                "AIP før (DKK)": event.start_ore / 100,
                                "AIP efter (DKK)": event.peak_ore / 100,
                                "AIP næste (DKK)": event.next_ore / 100
                                if event.next_ore is not None
                                else None,
                                "Næste ændring": event.next_change,
                                "Tilbageført andel": event.giveback,
                            }
                            for event in features.events
                        ]
                    ).style.format(
                        precision=3,
                        decimal=",",
                        na_rep="Mangler",
                        subset=[
                            "AIP før (DKK)",
                            "AIP efter (DKK)",
                            "AIP næste (DKK)",
                            "Næste ændring",
                            "Tilbageført andel",
                        ],
                    ),
                    hide_index=True,
                    width="stretch",
                )
    with alternatives_tab:
        st.caption(
            "Gruppefuldstændighed er ikke verificeret; tabellen viser kun fundne "
            "medlemmer af den aktuelle Cito-rapport."
        )
        with st.expander("Fundne alternativer i rapporten"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Varenr.": item.product_id,
                            "Aktuel vare": item.product_id == product_id,
                            "Koder": f"{item.current_code}→{item.upcoming_code}",
                            "Kostpris (DKK)": item.current_cost_ore / 100,
                            "Opbevaring": item.storage_code,
                            "Lagerudelukket": item.storage_code in ("H", "K"),
                            "Historik": "Fundet"
                            if item.product_id in analysis.history.prices
                            else "Mangler",
                        }
                        for item in group
                    ]
                ).style.format(precision=2, decimal=",", thousands=".", subset=["Kostpris (DKK)"]),
                hide_index=True,
                width="stretch",
            )
    with review_tab:
        saved = review.reviews.get(product_id, Review())
        suffix = f"{analysis.analysis_id}_{product_id}"
        with st.form(f"review_form_{suffix}"):
            status = st.selectbox(
                "Vurdering",
                STATUSES,
                index=STATUSES.index(saved.status),
                key=f"review_status_{suffix}",
            )
            note = st.text_area(
                "Note", value=saved.note, max_chars=4000, key=f"review_note_{suffix}"
            )
            selected = st.checkbox(
                "Medtag i valgt eksport",
                value=product_id in review.selected_ids,
                key=f"review_selected_{suffix}",
            )
            if st.form_submit_button("Gem vurdering"):
                review.reviews[product_id] = Review(status, note)
                if selected:
                    review.selected_ids.add(product_id)
                else:
                    review.selected_ids.discard(product_id)
                st.session_state["saved_review"] = suffix
                st.rerun()
        if st.session_state.pop("saved_review", None) == suffix:
            st.success("Vurderingen er gemt i denne midlertidige session.")


def _exports(analysis: AnalysisResult, review: ReviewState) -> None:
    from bef_redbird.export import dump_session, export_analysis, restore_session

    st.subheader("Eksport og sessionskopi")
    scope_label = st.selectbox(
        "Eksportomfang",
        ["Aktuel prioriteringskø", "Valgte produkter", "Alle kandidater"],
        key="export_scope",
    )
    scope = {
        "Aktuel prioriteringskø": "queue",
        "Valgte produkter": "selected",
        "Alle kandidater": "all_eligible",
    }[scope_label]
    count = (
        len(select_queue(analysis, review.queue))
        if scope == "queue"
        else (len(review.selected_ids) if scope == "selected" else len(analysis.candidates))
    )
    st.caption(f"{count} produkter i eksporten. Tabelsortering og søgning ændrer ikke omfanget.")
    if count:
        filename = f"redbird_{analysis.context.current_period}_{scope}_{count}.xlsx"
        signature = (
            analysis.analysis_id,
            scope,
            review.queue,
            tuple(sorted(review.reviews.items())),
            frozenset(review.selected_ids),
            review.inspected_id,
        )
        if st.button("Klargør Excel", key="prepare_excel"):
            st.session_state["prepared_export"] = (
                signature,
                export_analysis(analysis, review, scope),
                filename,
            )
        prepared = st.session_state.get("prepared_export")
        if prepared and prepared[0] == signature:
            st.download_button(
                "Hent Excel",
                prepared[1],
                prepared[2],
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                on_click="ignore",
            )
    else:
        st.info("Der er ingen produkter i det valgte eksportomfang.")
    try:
        session_data = dump_session(analysis, review)
    except DataError as exc:
        st.error(str(exc))
    else:
        st.download_button(
            "Hent sessionskopi (JSON)",
            session_data,
            f"redbird_session_{analysis.context.current_period}.json",
            "application/json",
            on_click="ignore",
        )
    session = st.file_uploader("Gendan sessionskopi (JSON)", type=["json"], key="session_upload")
    if st.button("Gendan vurderinger", disabled=session is None):
        try:
            if session is not None:
                st.session_state["review"] = restore_session(session.getvalue(), analysis)
                _reset_view_widgets()
                st.session_state["sync_review_widgets"] = True
                st.session_state["restore_success"] = True
                st.rerun()
        except DataError as exc:
            st.error(str(exc))
        except Exception:
            st.error(_safe_failure())
    if st.session_state.pop("restore_success", False):
        st.success("Sessionskopien er gendannet for den matchende analyse.")


def _workspace() -> None:
    st.set_page_config(page_title="Redbird · indkøbsgennemgang", layout="wide")
    st.header("Redbird · indkøbsgennemgang")
    st.caption(
        "Midlertidig session. Kilder og noter kan forsvinde ved genstart eller afbrydelse. "
        "Hent en sessionskopi for at bevare vurderingerne."
    )
    if st.session_state.pop("reset_ack", False):
        st.session_state["replace_ack"] = False
    if st.session_state.pop("sync_review_widgets", False):
        restored_analysis = _analysis()
        if restored_analysis is not None:
            restored = _review(restored_analysis)
            aid = restored_analysis.analysis_id
            st.session_state[f"queue_n_{aid}"] = restored.queue.top_n
            st.session_state[f"queue_mode_{aid}"] = (
                "Alle prioriterbare" if restored.queue.mode == "all_rankable" else "Top N"
            )
            if restored.inspected_id in restored_analysis.candidates:
                st.session_state[f"product_{aid}"] = restored.inspected_id
            restored_ids = set(restored.reviews) | restored.selected_ids
            if restored.inspected_id is not None:
                restored_ids.add(restored.inspected_id)
            for pid in restored_ids:
                saved = restored.reviews.get(pid, Review())
                st.session_state[f"review_status_{aid}_{pid}"] = saved.status
                st.session_state[f"review_note_{aid}_{pid}"] = saved.note
                st.session_state[f"review_selected_{aid}_{pid}"] = pid in restored.selected_ids
    _inputs()
    analysis = _analysis()
    if analysis is None:
        st.info(
            "Upload Cito-rapport og LMS-prishistorik i sidepanelet, vælg den bekræftede "
            "aktuelle periode og tryk Indlæs og kontroller."
        )
        return
    review = _review(analysis)
    _header(analysis)
    queue_column, detail_column = st.columns([2, 3], gap="medium")
    with queue_column:
        _queue(analysis, review)
    with detail_column:
        _detail(analysis, review)
    _exports(analysis, review)


def main() -> None:
    try:
        _workspace()
    except DataError as exc:
        st.error(str(exc))
    except Exception:
        # Fail visibly without copying untrusted values into a browser or server traceback.
        st.error(_safe_failure())
