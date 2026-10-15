"""Danish temporary-session review workspace; calculations live outside the UI."""

from __future__ import annotations

import hashlib
import json
import logging
import re
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
from bef_redbird.service import FLAG_TEXT

MODES = (
    "Prioriteret liste",
    "Alle prioriterbare",
    "Alle kandidater",
    "Kan ikke prioriteres",
    "Dataproblemer",
)


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


def _label(analysis: AnalysisResult, product_id: str) -> str:
    return f"{product_id} · {analysis.history.labels.get(product_id, 'Navn ikke oplyst')}"


def _open_product(analysis_id: str, product_id: str) -> None:
    st.session_state["review"].inspected_id = product_id
    key = f"table_generation_{analysis_id}"
    # Native table selections cannot be assigned programmatically. Remount only
    # on external navigation so an old row never appears to own the new review.
    st.session_state[key] = st.session_state.get(key, 0) + 1


def _choose_product(analysis_id: str, selector: str) -> None:
    chosen = st.session_state.get(selector)
    if chosen is not None:
        _open_product(analysis_id, chosen)


def _toggle_alternative(analysis_id: str, product_id: str, alternative_id: str) -> None:
    # Keep per-candidate choices outside widget state: Streamlit removes widgets
    # belonging to the previous candidate after navigation.
    choices = st.session_state.setdefault(f"overlay_choices_{analysis_id}", {})
    selected = list(choices.get(product_id, []))
    checked = st.session_state[f"overlay_{analysis_id}_{product_id}_{alternative_id}"]
    if checked and alternative_id not in selected and len(selected) < 5:
        selected.append(alternative_id)
    elif not checked and alternative_id in selected:
        selected.remove(alternative_id)
    choices[product_id] = selected


def _drafts(analysis_id: str) -> dict[str, tuple[str, str, bool]]:
    return st.session_state.setdefault(f"drafts_{analysis_id}", {})


def _capture_draft(analysis_id: str, product_id: str) -> None:
    suffix = f"{analysis_id}_{product_id}"
    _drafts(analysis_id)[product_id] = (
        st.session_state[f"review_status_{suffix}"],
        st.session_state[f"review_note_{suffix}"],
        st.session_state[f"review_selected_{suffix}"],
    )


def _unsaved(review: ReviewState) -> bool:
    return any(
        value != (saved.status, saved.note, pid in review.selected_ids)
        for pid, value in _drafts(review.analysis_id).items()
        for saved in [review.reviews.get(pid, Review())]
    )


def _review(analysis: AnalysisResult) -> ReviewState:
    current = st.session_state.get("review")
    if not isinstance(current, ReviewState) or current.analysis_id != analysis.analysis_id:
        current = ReviewState(analysis.analysis_id)
        st.session_state["review"] = current
    return current


def _reset_view_widgets(*, replacing_analysis: bool = False) -> None:
    prefixes: tuple[str, ...] = ("product_", "review_", "queue_", "table_", "drafts_")
    if replacing_analysis:
        prefixes += ("overlay_", "comparison_", "detail_tabs_")
    for key in list(st.session_state):
        if str(key).startswith(prefixes):
            del st.session_state[key]


def _inputs() -> None:
    from bef_redbird.imports import parse_cito, parse_lms_history
    from bef_redbird.service import build_analysis

    analysis = _analysis()
    with st.expander("Datagrundlag og indstillinger", expanded=analysis is None):
        left, right, dates = st.columns([2, 2, 1])
        cito = left.file_uploader("Cito-rapport", type=["xlsx", "xls"], key="cito_upload")
        history = right.file_uploader("LMS-prishistorik", type=["xlsx"], key="history_upload")
        period = dates.date_input("Bekræftet aktuel periode", value=None, key="period")
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
                    if (
                        review
                        and (review.reviews or review.selected_ids or _unsaved(review))
                        and not replacement_ack
                    ):
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
    st.caption(
        f"Aktuel periode: {analysis.context.current_period:%d-%m-%Y} · "
        f"Kommende kodeperiode: {analysis.context.current_period + timedelta(days=14):%d-%m-%Y}  \n"
        f"{len(analysis.candidates)} kandidater · {len(analysis.ranked_ids)} kan prioriteres · "
        f"{len(analysis.issues)} dataproblemer · "
        + (f"LMS til {through:%d-%m-%Y}" if through else "Ingen LMS-prisperioder")
        + " · Prioritering til gennemgang — ikke dokumenteret købsgevinst."
    )
    if through and through < analysis.context.current_period:
        st.warning("LMS-historikken slutter før den bekræftede periode; aktuelle AIP kan mangle.")
    for issue in analysis.issues:
        if issue.code in ("period_alignment", "older_period_match", "irregular_calendar"):
            st.warning(issue.message)


def _provenance(analysis: AnalysisResult) -> None:
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


def _queue(analysis: AnalysisResult, review: ReviewState) -> tuple[str, ...]:
    st.subheader("1. Vælg en kandidat", anchor="kandidater")
    st.caption("Klik på en række. Grafen og vurderingen følger den valgte kandidat.")
    mode_column, count_column = st.columns([2, 1])
    mode = mode_column.selectbox(
        "Hvilke kandidater vil du se?",
        MODES,
        index=(1 if review.queue.mode == "all_rankable" else 0),
        key=f"queue_mode_{analysis.analysis_id}",
        help=(
            "Prioriteret liste viser det ønskede antal øverst i rækkefølgen. "
            "Alle kandidater omfatter også varer, der mangler grundlag for en prioritet."
        ),
    )
    n = count_column.number_input(
        "Antal kandidater",
        min_value=1,
        max_value=1_000_000_000,
        value=review.queue.top_n,
        step=1,
        key=f"queue_n_{analysis.analysis_id}",
        disabled=mode != "Prioriteret liste",
        help=(
            "Antal i den prioriterede liste. De første 50 vises som udgangspunkt; "
            "skriv fx 17 eller 80."
        ),
    )
    search = st.text_input(
        "Søg i kandidatlisten",
        key="search",
        placeholder="Navn, styrke eller varenummer",
        help="Søger kun blandt varerne i den valgte visning. Ændrer ikke listen til Excel.",
    )
    if type(n) is not int or not 1 <= n <= 1_000_000_000:
        st.warning("Antal kandidater skal være et positivt heltal. Den seneste gyldige kø bevares.")
        n = review.queue.top_n
    else:
        queue_mode = review.queue.mode
        if mode in ("Prioriteret liste", "Alle prioriterbare"):
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
        return ()
    ids = (
        tuple(analysis.candidates)
        if mode == "Alle kandidater"
        else (
            tuple(k for k, value in analysis.candidates.items() if value.rank is None)
            if mode == "Kan ikke prioriteres"
            else queue_ids
        )
    )
    groups = sorted(
        {group_id for k in ids if (group_id := analysis.candidates[k].item.group_id) is not None}
    )
    with st.expander("Afgræns listen"):
        group = st.selectbox(
            "Vis kun denne substitutionsgruppe", ["Alle grupper", *groups], key="group"
        )
        status_filter = st.selectbox("Vis kun denne vurdering", ["Alle vurderinger", *STATUSES])
    ids = tuple(
        k
        for k in ids
        if (
            not search
            or search.casefold() in (k + " " + analysis.history.labels.get(k, "")).casefold()
        )
        and (group == "Alle grupper" or analysis.candidates[k].item.group_id == group)
        and (
            status_filter == "Alle vurderinger"
            or review.reviews.get(k, Review()).status == status_filter
        )
    )
    st.caption(f"{queue_description} {len(ids)} synlige efter søgning og filtre.")
    if not ids:
        st.info("Ingen produkter i denne visning. Se Alle kandidater eller Dataproblemer.")
        return ()
    rows = []
    for product_id in ids:
        candidate = analysis.candidates[product_id]
        rows.append(
            {
                "Prioritet": candidate.rank,
                "Varenr.": product_id,
                "Vare": analysis.history.labels.get(product_id, "Navn ikke oplyst"),
                "Kostpris (DKK)": candidate.item.current_cost_ore / 100,
            }
        )
    rendered = json.dumps(
        [mode, ids, st.session_state.get(f"table_generation_{analysis.analysis_id}", 0)],
        ensure_ascii=False,
    ).encode()
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
            subset=["Kostpris (DKK)"],
        ),
        hide_index=True,
        width="stretch",
        height=600,
        row_height=44,
        selection_mode="single-row",
        on_select=partial(_inspect_table, key, ids, analysis.analysis_id),
        key=key,
        column_config={
            "Prioritet": st.column_config.NumberColumn("Prioritet", width=70),
            "Varenr.": st.column_config.TextColumn(width=80),
            "Vare": st.column_config.TextColumn("Lægemiddel, styrke og pakning", width=160),
            "Kostpris (DKK)": st.column_config.NumberColumn("Kostpris, kr.", width=100),
        },
    )
    st.caption(
        "Prioritet 1 gennemgås først; det er ikke en købsanbefaling. "
        "Klik på en kolonneoverskrift for at sortere. "
        "Søgning og sortering ændrer ikke eksportlisten."
    )
    with st.expander("Vælg fra listen med tastatur"):
        # Streamlit keeps keyed selectboxes mounted when options change. Remount
        # for a new visible list so stale client-side search cannot hide its rows.
        scope = hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:16]
        selector = f"queue_pick_{analysis.analysis_id}_{scope}"
        st.session_state[selector] = review.inspected_id if review.inspected_id in ids else None
        st.selectbox(
            "Kandidat i den viste liste",
            ids,
            index=None,
            placeholder="Vælg en kandidat fra tabellen",
            format_func=lambda pid: _label(analysis, pid),
            key=selector,
            on_change=_choose_product,
            args=(analysis.analysis_id, selector),
            help="Samme kandidater som i tabellen, efter søgning og filtre.",
        )
    st.markdown("[Se den valgte kandidats priser](#priser)")
    return ids


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


def _group_comparison(analysis: AnalysisResult, product_id: str) -> tuple[str, ...]:
    group = analysis.group_members(product_id)
    alternatives = [item for item in group if item.product_id != product_id]
    st.markdown("**Sammenlign med substitutionsalternativer**")
    st.caption(
        "Fundne alternativer i rapporten · "
        f"Gruppe {analysis.candidates[product_id].item.group_id or 'ikke oplyst'}. "
        "Kun den valgte kandidats gruppe i den aktuelle rapport."
    )
    choices = st.session_state.setdefault(f"overlay_choices_{analysis.analysis_id}", {})
    available = {
        item.product_id for item in alternatives if item.product_id in analysis.history.prices
    }
    selected = [pid for pid in choices.get(product_id, []) if pid in available][:5]
    choices[product_id] = selected
    if not alternatives:
        st.caption("Ingen andre varer fundet i rapportens gruppe.")
    else:
        st.caption(
            "Markér de varer, du vil se sammen med kandidaten i grafen. "
            f"{len(selected)} af 5 valgt. "
            "Fjern et flueben for at skjule varen igen."
        )
        if len(selected) == 5:
            st.info("Fem alternativer er valgt. Fjern et flueben for at vælge et andet.")
        periods = analysis.history.periods
        index = (
            periods.index(analysis.context.current_period)
            if analysis.context.current_period in periods
            else -1
        )
        for position, item in enumerate(alternatives):
            # The first few choices are immediately visible; a large group does
            # not turn into an unbounded wall of controls. Every member is retained.
            if position == 3:
                more = st.expander(f"Vis flere alternativer ({len(alternatives) - 3})")
            target = st if position < 3 else more
            pid = item.product_id
            values = analysis.history.prices.get(pid, ())
            aip = values[index] if 0 <= index < len(values) else None
            aip = aip if aip is not None and aip > 0 else None
            key = f"overlay_{analysis.analysis_id}_{product_id}_{pid}"
            st.session_state[key] = pid in selected
            # Checkbox labels use Markdown; source labels must remain literal text.
            label = re.sub(r"([\\`*_{}\[\]()<>#+.!|~>-])", r"\\\1", _label(analysis, pid))
            target.checkbox(
                label,
                key=key,
                disabled=pid not in available or (len(selected) >= 5 and pid not in selected),
                on_change=_toggle_alternative,
                args=(analysis.analysis_id, product_id, pid),
                help="Vis eller skjul dette alternativ i grafen. Den valgte kandidat ændres ikke.",
            )
            detail = (
                f"AIP i valgt periode: {_money(aip)} kr. · "
                f"Cito-kostpris: {_money(item.current_cost_ore)} kr. · "
                f"Prisklasse {item.current_code} → {item.upcoming_code}"
            )
            if pid not in available:
                detail += " · Historik mangler; ingen kurve kan vises."
            elif aip is None:
                detail += " · Aktuel AIP mangler; kun kendte observationer vises."
            if item.storage_code in ("H", "K"):
                detail += f" · Opbevaring {item.storage_code}: fravalgt af indkøbsfilteret."
            target.caption(detail)
            if pid in analysis.candidates and (pid in selected or pid not in available):
                target.button(
                    f"Vurder alternativ {pid}",
                    key=f"open_alternative_{product_id}_{pid}",
                    on_click=_open_product,
                    args=(analysis.analysis_id, pid),
                    help="Åbner dette alternativ som kandidat. Dine vurderinger bevares.",
                )
    with st.expander(f"Gruppedetaljer · {len(group)} fundne varer"):
        st.caption(
            "Gruppefuldstændighed er ikke verificeret. "
            "Gruppen gælder den aktuelle rapport, ikke tidligere perioder."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Vare": _label(analysis, item.product_id),
                        "Rolle": "Valgt kandidat"
                        if item.product_id == product_id
                        else "Alternativ",
                        "Koder": f"{item.current_code} → {item.upcoming_code}",
                        "Kostpris (DKK)": item.current_cost_ore / 100,
                        "Opbevaring": item.storage_code,
                        "H/K-fravalgt": "Ja" if item.storage_code in ("H", "K") else "Nej",
                        "Historik": "Fundet"
                        if item.product_id in analysis.history.prices
                        else "Mangler",
                    }
                    for item in group
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    return tuple(selected)


def _save_review(analysis_id: str, product_id: str, next_id: str | None = None) -> None:
    _capture_draft(analysis_id, product_id)
    status, note, selected = _drafts(analysis_id).pop(product_id)
    review = st.session_state["review"]
    review.reviews[product_id] = Review(status, note)
    if selected:
        review.selected_ids.add(product_id)
    else:
        review.selected_ids.discard(product_id)
    st.session_state["saved_review"] = product_id
    if next_id:
        _open_product(analysis_id, next_id)


def _review_editor(
    analysis: AnalysisResult, review: ReviewState, product_id: str, visible: tuple[str, ...]
) -> None:
    saved = review.reviews.get(product_id, Review())
    status, note, selected = _drafts(analysis.analysis_id).get(
        product_id, (saved.status, saved.note, product_id in review.selected_ids)
    )
    suffix = f"{analysis.analysis_id}_{product_id}"
    with st.container(border=True, key="review_editor"):
        st.subheader("3. Gem din vurdering", anchor="vurdering")
        st.text(f"Din vurdering af {_label(analysis, product_id)}")
        st.caption(
            "Vælg en status og skriv, hvad der taler for et indkøb eller skal afklares først."
        )
        st.selectbox(
            "Vurdering",
            STATUSES,
            index=STATUSES.index(status),
            key=f"review_status_{suffix}",
            on_change=_capture_draft,
            args=(analysis.analysis_id, product_id),
        )
        st.text_area(
            "Note",
            value=note,
            max_chars=4000,
            key=f"review_note_{suffix}",
            placeholder="Fx hvad der skal afklares før indkøb …",
            on_change=_capture_draft,
            args=(analysis.analysis_id, product_id),
        )
        st.checkbox(
            "Føj varen til min eksportliste",
            value=selected,
            key=f"review_selected_{suffix}",
            on_change=_capture_draft,
            args=(analysis.analysis_id, product_id),
        )
        if (status, note, selected) != (
            saved.status,
            saved.note,
            product_id in review.selected_ids,
        ):
            st.caption("Ugemte ændringer. De bevares ved vareskift; gem før eksport.")
        index = visible.index(product_id) if product_id in visible else -1
        next_id = visible[index + 1] if 0 <= index < len(visible) - 1 else None
        left, right = st.columns(2)
        left.button(
            "Gem vurdering",
            type="primary",
            width="stretch",
            on_click=_save_review,
            args=(analysis.analysis_id, product_id),
        )
        right.button(
            "Gem og næste kandidat",
            width="stretch",
            disabled=next_id is None,
            on_click=_save_review,
            args=(analysis.analysis_id, product_id, next_id),
        )
        st.markdown("[Se den valgte kandidats priser](#priser) · [Til kandidatlisten](#kandidater)")
    if st.session_state.pop("saved_review", None):
        st.success("Vurderingen er gemt i denne midlertidige session.")


def _detail(analysis: AnalysisResult, review: ReviewState, visible: tuple[str, ...] = ()) -> None:
    if not analysis.candidates:
        return
    ids = tuple(sorted(analysis.candidates))
    if review.inspected_id not in ids:
        review.inspected_id = analysis.ranked_ids[0] if analysis.ranked_ids else ids[0]
    product_id = review.inspected_id
    with st.container(border=True, key="price_workspace"):
        st.subheader("2. Sammenlign priser", anchor="priser")
        st.text(_label(analysis, product_id))
        index = visible.index(product_id) if product_id in visible else -1
        with st.container(horizontal=True, vertical_alignment="center"):
            st.button(
                "Forrige kandidat",
                disabled=index <= 0,
                on_click=_open_product,
                args=(analysis.analysis_id, visible[max(index - 1, 0)] if visible else product_id),
            )
            st.button(
                "Næste kandidat",
                disabled=index < 0 or index >= len(visible) - 1,
                on_click=_open_product,
                args=(
                    analysis.analysis_id,
                    visible[min(index + 1, len(visible) - 1)] if visible else product_id,
                ),
            )
            if index >= 0:
                st.caption(
                    f"Kandidat {index + 1} af {len(visible)} i listen · følger prioriteringen"
                )
            else:
                st.caption(
                    "Varen er uden for den filtrerede liste. "
                    "Ret søgning eller filtre for at bladre."
                )
        if product_id not in select_queue(analysis, review.queue):
            st.info(
                "Den inspicerede vare er uden for den aktuelle prioriteringskø. "
                "Vurderingen bevares."
            )
        candidate = analysis.candidates[product_id]
        saved = review.reviews.get(product_id, Review())
        st.caption(
            f"{saved.status} · Prioritet {candidate.rank or 'mangler'} · "
            f"Prisklasse nu: {candidate.item.current_code} · "
            f"Kommende prisklasse: {candidate.item.upcoming_code} "
            f"fra {analysis.context.current_period + timedelta(days=14):%d-%m-%Y}"
        )
        if not candidate.flags:
            st.caption(f"Hvorfor denne prioritet? {candidate.reason}")
        if candidate.flags:
            st.warning(" ".join(FLAG_TEXT.get(flag, flag) for flag in candidate.flags))
        if candidate.item.group_id in analysis.snapshot.incomplete_groups:
            st.warning(
                "Der er uafklarede rækker i gruppen. Gruppeoverblikket kan være ufuldstændigt."
            )
        features = candidate.features
        cost, current, middle = st.columns(3)
        cost.metric(
            "Kostpris fra Cito",
            f"{_money(candidate.item.current_cost_ore)} kr.",
            border=True,
            help="Rapportens kostpris for den valgte aktuelle periode.",
        )
        current.metric(
            "AIP i valgt periode",
            f"{_money(features.current_aip_ore)} kr.",
            border=True,
            help="LMS-prisen på præcis den bekræftede prisperiodestart; ikke senest kendte pris.",
        )
        middle.metric(
            "Median i egen historik",
            f"{_money(features.median_ore, 3)} kr.",
            border=True,
            help="Den midterste gyldige AIP i analysevinduet. Ikke en forventet fremtidig pris.",
        )
        distance = features.median_distance_dkk
        if distance is None:
            st.caption(
                "Historisk prisniveau kan ikke sammenlignes: aktuel AIP eller historik mangler."
            )
        else:
            relation = "under" if distance >= 0 else "over"
            st.caption(
                f"Aktuel AIP ligger {_money(abs(distance) * 100, 3)} kr. {relation} egen median "
                f"({features.n_valid} priser / {features.window_periods} perioder). "
                "Historisk forskel, ikke forventet besparelse."
            )
        st.subheader("AIP over tid")
        view = st.radio(
            "Vis prishistorik",
            ["Hele historikken", "Seneste 26 perioder", "Seneste 52 perioder"],
            horizontal=True,
            key="chart_window",
            help=(
                "En prisperiode er 14 dage. Dette ændrer kun grafen, "
                "ikke beregninger eller prioritering."
            ),
        )
        chart_area = st.container()
        st.caption(
            "AIP pr. pakning; kontrollér styrke og pakning før sammenligning. "
            "Ikke enhedspris eller udleveringspris. Gruppefuldstændighed er ikke verificeret; "
            "kun fundne varer i den aktuelle rapport."
        )
        overlays = _group_comparison(analysis, product_id)
        with st.expander("Flere grafvalg"):
            retrospective = st.checkbox(
                "Vis også priser efter den valgte periode",
                key="retrospective",
                help=(
                    "Retrospektiv visning. Senere priser kan ikke bruges "
                    "som viden på beslutningstidspunktet."
                ),
            )
            st.caption("Senere observationer markeres i grafen og indgår aldrig i prioriteringen.")
        limit = {"Hele historikken": None, "Seneste 26 perioder": 26, "Seneste 52 perioder": 52}[
            view
        ]
        with chart_area:
            st.plotly_chart(
                history_chart(
                    analysis, product_id, overlays, retrospective=retrospective, view_periods=limit
                ),
                width="stretch",
                key=f"history_{product_id}",
                config={"displaylogo": False, "scrollZoom": False},
            )
        st.caption(
            "Kommende C kan skyldes billigere alternativer. "
            "Kodeskiftet dokumenterer ikke en stigning i varens egen næste AIP. "
            "Se forklaringer, beregninger og prioritering nederst."
        )
        st.markdown("[Gem din vurdering](#vurdering) · [Beregninger og metode](#metode)")


def _history_details(analysis: AnalysisResult, product_id: str) -> None:
    candidate = analysis.candidates[product_id]
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


def _exports(analysis: AnalysisResult, review: ReviewState) -> None:
    from bef_redbird.export import dump_session, export_analysis, restore_session

    st.subheader("Eksportér din indkøbsgennemgang")
    if _unsaved(review):
        st.warning(
            "Du har ugemte vurderinger. Gem dem under Gem din vurdering før eksport; "
            "kun gemte vurderinger medtages."
        )
    st.caption(
        "Excel åbner med et kort indkøbsoverblik. Beregninger og datakontrol "
        "ligger på separate ark."
    )
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
                "Alle prioriterbare"
                if restored.queue.mode == "all_rankable"
                else "Prioriteret liste"
            )
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
            "Upload Cito-rapport og LMS-prishistorik ovenfor, vælg den bekræftede "
            "aktuelle periode og tryk Indlæs og kontroller."
        )
        return
    review = _review(analysis)
    _header(analysis)
    if analysis.candidates and review.inspected_id not in analysis.candidates:
        review.inspected_id = (
            analysis.ranked_ids[0] if analysis.ranked_ids else min(analysis.candidates)
        )
    left, right = st.columns([1, 1.4], gap="medium")
    with left:
        with st.container(border=True, key="candidate_list"):
            visible = _queue(analysis, review)
    with right:
        _detail(analysis, review, visible)
    if review.inspected_id in analysis.candidates:
        _review_editor(analysis, review, review.inspected_id, visible)
        _history_details(analysis, review.inspected_id)
    with st.expander("Eksport og sessionskopi"):
        _exports(analysis, review)
    _provenance(analysis)
    from bef_redbird.method_guide import render_method_guide

    with st.container(width=1100):
        render_method_guide(analysis)


def main() -> None:
    try:
        _workspace()
    except DataError as exc:
        st.error(str(exc))
    except Exception:
        # Fail visibly without copying untrusted values into a browser or server traceback.
        st.error(_safe_failure())
