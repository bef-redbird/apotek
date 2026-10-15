"""One result drives Excel and validated review-session JSON; no spreadsheet engine."""

from __future__ import annotations

import io
import json
import math
from dataclasses import asdict
from typing import Any

import xlsxwriter

from bef_redbird.domain import (
    AnalysisResult,
    AnalysisSettings,
    DataError,
    QueueSettings,
    Review,
    ReviewState,
    select_queue,
)

MAX_SESSION_BYTES = 2 * 1024 * 1024
MAX_SESSION_REVIEWS = 25_000

LIMITATIONS = (
    "Prioritering til gennemgang — ikke dokumenteret købsgevinst.",
    "Fundne alternativer i rapporten; gruppens fuldstændighed er ikke verificeret.",
    "Gruppemedlemskab stammer fra den aktuelle rapport, ikke historiske perioder.",
    "AIP pr. pakning er ikke en sammenligning af enhedspris eller ekspeditionspris.",
    "Lav historisk pris kan skyldes varigt prisfald; kommende C er ikke en kendt AIP-stigning.",
    "Retrospektiv historik dokumenterer ikke oprindelig tilgængelighed eller fravær af revisioner.",
)


def export_ids(analysis: AnalysisResult, state: ReviewState, scope: str) -> tuple[str, ...]:
    _match(analysis, state)
    if scope == "queue":
        return select_queue(analysis, state.queue)
    if scope == "selected":
        return tuple(sorted(state.selected_ids))
    if scope == "all_eligible":
        return (*analysis.ranked_ids, *sorted(set(analysis.candidates) - set(analysis.ranked_ids)))
    raise DataError("Ukendt eksportvalg.")


def _match(analysis: AnalysisResult, state: ReviewState) -> None:
    if state.analysis_id != analysis.analysis_id:
        raise DataError("Vurderinger tilhører en anden analyse.")
    if not (set(state.reviews) | state.selected_ids) <= analysis.snapshot.items.keys():
        raise DataError("Vurderinger indeholder ukendte produkter.")
    if state.inspected_id is not None and state.inspected_id not in analysis.snapshot.items:
        raise DataError("Ukendt vist produkt.")


def dump_session(analysis: AnalysisResult, state: ReviewState) -> bytes:
    _match(analysis, state)
    if len(state.reviews) > MAX_SESSION_REVIEWS:
        raise DataError(
            "JSON-sikkerhedskopien overstiger 25.000 vurderinger. "
            "Eksportér vurderingerne til Excel."
        )
    payload = {
        "session_schema": 1,
        "analysis_id": analysis.analysis_id,
        "sources": [analysis.snapshot.source.sha256, analysis.history.source.sha256],
        "context": analysis.manifest()["context"],
        "settings": asdict(analysis.settings),
        "versions": analysis.versions,
        "queue": asdict(state.queue),
        "reviews": {pid: asdict(review) for pid, review in sorted(state.reviews.items())},
        "selected_ids": sorted(state.selected_ids),
        "inspected_id": state.inspected_id,
    }
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(data) > MAX_SESSION_BYTES:
        raise DataError(
            "JSON-sikkerhedskopien overstiger 2 MB. "
            "Eksportér vurderingerne til Excel, eller forkort noter, før du prøver igen."
        )
    return data


def restore_session(data: bytes, analysis: AnalysisResult) -> ReviewState:
    if len(data) > MAX_SESSION_BYTES:
        raise DataError("Vurderingsfilen overstiger 2 MB.")
    try:
        payload = json.loads(data)
        if type(payload.get("session_schema")) is not int:
            raise DataError("Ugyldig sessionsversion.")
        restored_settings = AnalysisSettings(**payload["settings"])
        if asdict(restored_settings) != payload["settings"]:
            raise DataError("Indstillinger i vurderingsfilen er ikke kanoniske.")
        expected = json.loads(dump_session(analysis, ReviewState(analysis.analysis_id)))
        for field in (
            "session_schema",
            "analysis_id",
            "sources",
            "context",
            "settings",
            "versions",
        ):
            if payload[field] != expected[field]:
                raise DataError(
                    "Vurderingsfilen matcher ikke kilder, periode og anvendte indstillinger."
                )
        if set(payload) != set(expected):
            raise DataError("Vurderingsfilens felter er ugyldige.")
        queue = QueueSettings(**payload["queue"])
        if (
            not isinstance(payload["reviews"], dict)
            or len(payload["reviews"]) > MAX_SESSION_REVIEWS
        ):
            raise DataError("Ugyldig vurderingssamling.")
        reviews = {pid: Review(**value) for pid, value in payload["reviews"].items()}
        selected = payload["selected_ids"]
        if not isinstance(selected, list) or any(not isinstance(v, str) for v in selected):
            raise DataError("Ugyldig produktmarkering.")
        inspected = payload["inspected_id"]
        if inspected is not None and inspected not in analysis.snapshot.items:
            raise DataError("Ukendt vist produkt.")
        state = ReviewState(analysis.analysis_id, queue, reviews, set(selected), inspected)
        _match(analysis, state)
        return state
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("Vurderingsfilen er ugyldig JSON eller har forkert format.") from exc


def export_analysis(analysis: AnalysisResult, state: ReviewState, scope: str = "queue") -> bytes:
    ids = export_ids(analysis, state, scope)
    if not ids:
        raise DataError("Ingen produkter i det valgte eksportområde.")
    output = io.BytesIO()
    book = xlsxwriter.Workbook(
        output, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False}
    )
    base = {"font_name": "Arial", "font_size": 10, "valign": "top"}
    header = book.add_format(
        {
            **base,
            "bold": True,
            "bg_color": "#E9EDF0",
            "font_color": "#243645",
            "text_wrap": True,
            "align": "center",
            "valign": "vcenter",
        }
    )
    body = book.add_format(base)
    wrapped = book.add_format({**base, "text_wrap": True})
    decimal = book.add_format({**base, "num_format": "#,##0.00####"})
    percent = book.add_format({**base, "num_format": "0.0%"})
    judgment = book.add_format({**base, "text_wrap": True, "bg_color": "#FFF8E8"})
    context = book.add_format({**base, "font_color": "#52616B"})

    def table(
        name: str,
        headers: list[str],
        rows: list[list[Any]],
        *,
        widths: list[int] | None = None,
        wrap_columns: tuple[int, ...] = (),
        percent_columns: tuple[int, ...] = (),
        judgment_columns: tuple[int, ...] = (),
        start_row: int = 0,
        freeze_columns: int = 2,
    ) -> Any:
        sheet = book.add_worksheet(name)
        widths = widths or [18] * len(headers)
        for col, label in enumerate(headers):
            sheet.write_string(start_row, col, label, header)
            sheet.set_column(col, col, widths[col])
        for rowno, row in enumerate(rows, start_row + 1):
            lines = 1
            for col, value in enumerate(row):
                cell_format = (
                    judgment
                    if col in judgment_columns
                    else wrapped
                    if col in wrap_columns
                    else body
                )
                if value is None:
                    sheet.write_blank(rowno, col, None, cell_format)
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    sheet.write_number(
                        rowno,
                        col,
                        value,
                        percent
                        if col in percent_columns
                        else decimal
                        if isinstance(value, float)
                        else body,
                    )
                else:
                    if len(str(value)) > 32767:
                        raise DataError(
                            "Et eksportfelt overskrider Excels tekstgrænse; "
                            "brug et mindre eksportområde."
                        )
                    sheet.write_string(rowno, col, str(value), cell_format)
                    if col in (*wrap_columns, *judgment_columns):
                        # Excel does not autofit wrapped rows written by XlsxWriter.
                        # Allow for word boundaries as well as explicit line breaks.
                        lines = max(
                            lines,
                            sum(
                                max(1, math.ceil(len(line) / max(1, widths[col] - 4)))
                                for line in str(value).split("\n")
                            ),
                        )
            if lines > 1:
                sheet.set_row(rowno, min(409, 15 * lines + 6))
        sheet.freeze_panes(start_row + 1, freeze_columns)
        sheet.autofilter(start_row, 0, start_row + len(rows), len(headers) - 1)
        sheet.set_default_row(19)
        sheet.set_row(start_row, 48)
        sheet.hide_gridlines(2)
        sheet.set_landscape()
        sheet.set_paper(9)  # A4; wide evidence sheets may span several pages horizontally.
        sheet.repeat_rows(start_row, start_row)
        sheet.repeat_columns(0, min(1, len(headers) - 1))
        sheet.print_area(0, 0, start_row + len(rows), len(headers) - 1)
        sheet.set_margins(0.3, 0.3, 0.4, 0.4)
        sheet.set_footer("&L" + name + "&RSide &P af &N")
        return sheet

    overview: list[list[Any]] = []
    for pid in ids:
        item = analysis.snapshot.items[pid]
        candidate = analysis.candidates.get(pid)
        f = candidate.features if candidate else None
        review = state.reviews.get(pid, Review())
        clarification = f"{item.current_code} til {item.upcoming_code}."
        if item.group_id is None:
            clarification += "\nGruppe mangler; alternativer kan ikke samles."
        if candidate is None:
            clarification += "\nUden for kandidatfilteret."
        elif candidate.flags:
            clarification += "\n" + candidate.reason
        if item.group_id in analysis.snapshot.incomplete_groups:
            clarification += "\nUafklarede kilderækker i gruppen."
        overview.append(
            [
                pid,
                analysis.history.labels.get(pid) or f"Varenummer {pid}",
                item.current_cost_ore / 100,
                f.current_aip_ore / 100 if f and f.current_aip_ore is not None else None,
                f.median_ore / 100 if f and f.median_ore is not None else None,
                review.status,
                (review.note + "\n\n" if review.note else "") + clarification,
            ]
        )
    sheet = table(
        "Indkøbsoverblik",
        [
            "Varenummer",
            "Produkt / beskrivelse",
            "Cito-kostpris (DKK)",
            "Aktuel AIP (DKK)",
            "Egen historisk AIP-median (DKK)",
            "Vurdering",
            "Note / afklaring",
        ],
        overview,
        widths=[11, 26, 12, 12, 16, 18, 27],
        wrap_columns=(1,),
        judgment_columns=(5, 6),
        start_row=2,
        freeze_columns=2,
    )
    sheet.set_tab_color("#243645")
    sheet.write_string(0, 0, f"Aktuel periode: {analysis.context.current_period:%d-%m-%Y}", context)
    periods = [p for p in analysis.history.periods if p <= analysis.context.current_period][
        -analysis.settings.lookback_periods :
    ]
    window = (
        f"Historik: {periods[0]:%d-%m-%Y} til {periods[-1]:%d-%m-%Y}, {len(periods)} perioder."
        if periods
        else "Ingen historikperioder frem til den aktuelle periode."
    )
    sheet.write_string(0, 2, window, context)
    sheet.write_string(
        1,
        0,
        "AIP pr. pakning. Historiske niveauer er ikke en besparelse. Alternativer: Gruppeoverblik.",
        context,
    )
    sheet.set_row(1, 19)

    headers = [
        "Prioritet",
        "Varenummer",
        "Opbevaringskode",
        "Kostpris (DKK)",
        "Nuværende kode",
        "Kommende kode",
        "Gruppe",
        "Aktuel AIP (DKK)",
        "Gyldige priser",
        "Perioder",
        "Dækning",
        "Forrige ændring",
        "Median (DKK)",
        "Afstand til egen historiske median",
        "Prispercentil",
        "Stigninger",
        "Gyldige nabopar",
        "Stigningsfrekvens",
        "Observerbare efterperioder",
        "Delvise/fulde tilbagefald",
        "Fulde tilbagefald",
        "Tilbagefaldsrate",
        "Fuld tilbagefaldsrate",
        "Datakoder",
        "Begrundelse",
        "Vurdering",
        "Note",
        "Cito-kilderækker",
        "LMS-kilderækker",
    ]
    rows: list[list[Any]] = []
    for pid in ids:
        item = analysis.snapshot.items[pid]
        candidate = analysis.candidates.get(pid)
        review = state.reviews.get(pid, Review())
        f = candidate.features if candidate else None
        rows.append(
            [
                candidate.rank if candidate else None,
                pid,
                item.storage_code,
                item.current_cost_ore / 100,
                item.current_code,
                item.upcoming_code,
                item.group_id,
                f.current_aip_ore / 100 if f and f.current_aip_ore is not None else None,
                f.n_valid if f else None,
                f.window_periods if f else None,
                f.coverage if f else None,
                f.previous_change if f else None,
                f.median_ore / 100 if f and f.median_ore is not None else None,
                f.median_distance_dkk if f else None,
                f.percentile if f else None,
                f.rise_count if f else None,
                f.adjacent_pair_count if f else None,
                f.rise_frequency if f else None,
                f.reversal_denominator if f else None,
                f.any_reversal_count if f else None,
                f.full_reversal_count if f else None,
                f.any_reversal_rate if f else None,
                f.full_reversal_rate if f else None,
                ", ".join(candidate.flags) if candidate else "outside_eligibility",
                candidate.reason if candidate else "Uden for kandidatfilteret.",
                review.status,
                review.note,
                ",".join(map(str, item.source_rows)),
                ",".join(map(str, analysis.history.source_rows.get(pid, ()))),
            ]
        )
    detail_widths = [
        11,
        13,
        18,
        18,
        18,
        18,
        14,
        18,
        14,
        14,
        14,
        18,
        18,
        24,
        15,
        14,
        17,
        19,
        21,
        21,
        18,
        19,
        20,
        30,
        55,
        20,
        45,
        20,
        20,
    ]
    table(
        "Kandidater",
        headers,
        rows,
        widths=detail_widths,
        wrap_columns=(23, 24, 26),
        judgment_columns=(25,),
        percent_columns=(10, 11, 14, 17, 21, 22),
    )
    selected_set = set(ids)
    selected_groups = {analysis.snapshot.items[pid].group_id for pid in ids} - {None}
    groups = []
    for pid, member in sorted(analysis.snapshot.items.items()):
        if member.group_id not in selected_groups and pid not in selected_set:
            continue
        groups.append(
            [
                pid,
                member.group_id,
                analysis.history.labels.get(pid) or f"Varenummer {pid}",
                "Ja" if pid in selected_set else "Nej",
                member.current_code,
                member.upcoming_code,
                member.current_cost_ore / 100,
                member.storage_code,
                "Ja" if member.storage_code in ("H", "K") else "Nej",
                "Fundet" if pid in analysis.history.prices else "Mangler",
                "Uafklarede rækker i gruppen"
                if member.group_id in analysis.snapshot.incomplete_groups
                else "Fuldstændighed ikke verificeret",
                f"Gruppe {member.group_id}" if member.group_id is not None else f"Varenummer {pid}",
            ]
        )
    table(
        "Gruppeoverblik",
        [
            "Varenummer",
            "Gruppe",
            "Produkt / beskrivelse",
            "Med i Indkøbsoverblik",
            "Nuværende kode",
            "Kommende kode",
            "Kostpris (DKK)",
            "Opbevaringskode",
            "H/K-fravalgt",
            "Historik",
            "Gruppeforbehold",
            "Forbindelse til Kandidater",
        ],
        groups,
        widths=[13, 14, 45, 21, 18, 18, 18, 18, 18, 16, 38, 26],
        wrap_columns=(2, 10, 11),
        freeze_columns=3,
    )
    manifest = analysis.manifest()
    manifest["queue_export"] = {
        **asdict(state.queue),
        "effective_n": len(select_queue(analysis, state.queue)),
        "scope": scope,
        "exported_count": len(ids),
    }
    metadata: list[list[Any]] = []
    for key, value in manifest.items():
        metadata.append(
            [
                key,
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, dict)
                else value,
            ]
        )
    # One ID per row avoids Excel's cell-text limit for all-rankable exports.
    metadata.extend([["ordered_exported_id", pid] for pid in ids])
    metadata.extend(
        [
            [
                "selected_group_relation",
                json.dumps({"product_id": pid, "group_id": analysis.snapshot.items[pid].group_id}),
            ]
            for pid in ids
        ]
    )
    metadata.extend([["Forbehold", text] for text in LIMITATIONS])
    metadata.extend(
        [
            ["Datakontrol", json.dumps(asdict(issue), ensure_ascii=False)]
            for issue in analysis.issues
        ]
    )
    table(
        "Metode og datakontrol",
        ["Felt", "Værdi"],
        metadata,
        widths=[28, 110],
        wrap_columns=(1,),
        freeze_columns=1,
    )
    book.close()
    return output.getvalue()
