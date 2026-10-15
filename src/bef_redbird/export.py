"""One result drives Excel and validated review-session JSON; no spreadsheet engine."""

from __future__ import annotations

import io
import json
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
    header = book.add_format({"bold": True, "bg_color": "#E6EEEB", "font_color": "#193A32"})
    decimal = book.add_format({"num_format": "#,##0.00####"})

    def table(name: str, headers: list[str], rows: list[list[Any]]) -> None:
        sheet = book.add_worksheet(name)
        for col, label in enumerate(headers):
            sheet.write_string(0, col, label, header)
        for rowno, row in enumerate(rows, 1):
            for col, value in enumerate(row):
                if value is None:
                    sheet.write_blank(rowno, col, None)
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    sheet.write_number(
                        rowno, col, value, decimal if isinstance(value, float) else None
                    )
                else:
                    if len(str(value)) > 32767:
                        raise DataError(
                            "Et eksportfelt overskrider Excels tekstgrænse; "
                            "brug et mindre eksportområde."
                        )
                    sheet.write_string(rowno, col, str(value))
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, len(rows), len(headers) - 1)
        sheet.set_column(0, len(headers) - 1, 18)
        sheet.set_default_row(19)

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
    table("Kandidater", headers, rows)
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
                f"Gruppe {member.group_id}" if member.group_id is not None else f"Varenummer {pid}",
                member.current_code,
                member.upcoming_code,
                member.current_cost_ore / 100,
                member.storage_code,
                "Ja" if member.storage_code in ("H", "K") else "Nej",
                "Fundet" if pid in analysis.history.prices else "Mangler",
                "Uafklarede rækker i gruppen"
                if member.group_id in analysis.snapshot.incomplete_groups
                else "Fuldstændighed ikke verificeret",
            ]
        )
    table(
        "Gruppeoverblik",
        [
            "Varenummer",
            "Gruppe",
            "Forbindelse til Kandidater",
            "Nuværende kode",
            "Kommende kode",
            "Kostpris (DKK)",
            "Opbevaringskode",
            "H/K-fravalgt",
            "Historik",
            "Gruppeforbehold",
        ],
        groups,
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
    table("Metode og datakontrol", ["Felt", "Værdi"], metadata)
    book.get_worksheet_by_name("Metode og datakontrol").set_column(1, 1, 95)
    book.close()
    return output.getvalue()
