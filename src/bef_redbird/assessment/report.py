"""Portable pharmacist review of saved predictions; no model or source access."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pymupdf

_CSS = """
body {font-family: sans-serif; font-size: 9pt; color: #183436;}
h1 {font-size: 24pt; margin: 0 0 8pt; color: #075e58;}
h2 {font-size: 15pt; margin: 5pt 0 9pt;}
p {margin: 6pt 0; line-height: 1.35;}
.eyebrow {font-size: 9pt; color: #48706e;}
.notice {background: #edf6f2; padding: 10pt; margin: 10pt 0;}
.muted {color: #526766; font-size: 8pt;}
table {border-collapse: collapse; width: 100%; font-size: 8pt; margin-top: 10pt;}
th {background: #075e58; color: white; text-align: right; padding: 7pt 4pt;}
td {border-bottom: 0.5pt solid #dbe6e2; text-align: right; padding: 3pt 4pt;}
.left {text-align: left;}
.alternate {background: #f2f7f5;}
"""


def _number(value: Any, *, percent: bool = False) -> str:
    if value is None or pd.isna(value):
        return "-"
    number = float(value)
    if not math.isfinite(number):
        return "-"
    return (f"{number * 100:.1f}%" if percent else f"{number:,.2f}").translate(
        str.maketrans(",.", ".,")
    )


def _period(value: Any) -> str:
    # Parse, rather than rendering arbitrary metadata that could contain a path.
    return date.fromisoformat(str(value)).strftime("%d.%m.%Y")


def _status(row: pd.Series) -> str:
    status = row.get("outcome_status")
    if pd.isna(status) or status is None:
        return "Afventer"
    return {
        "observed": "Observeret",
        "pending_target_observation": "Afventer",
        "pending": "Afventer",
        "identity_discontinuity_or_ambiguity": "Identitet uafklaret",
        "origin_ineligible": "Udelukket",
        "target_missing": "Mangler",
        "missing": "Mangler",
    }.get(str(status), "Ikke observeret")


def _selected(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "package_number",
        "selected",
        "rank",
        "aip_ore",
        "p_rise",
        "p_fall",
        "amount_3y_next_price_dkk",
        "amount_3y_delta_dkk",
    }
    if not required <= set(frame):
        raise ValueError("assessment_pdf_missing_prediction_columns")
    chosen = frame.loc[frame.selected.eq(True)].copy()  # noqa: E712
    if not chosen.package_number.astype(str).str.fullmatch(r"[0-9]{6}").all():
        raise ValueError("assessment_pdf_invalid_package_number")
    ranks = pd.to_numeric(chosen["rank"], errors="coerce")
    if ranks.isna().any() or not ranks.gt(0).all() or ranks.duplicated().any():
        raise ValueError("assessment_pdf_invalid_selected_ranking")
    return chosen.assign(rank=ranks).sort_values("rank", kind="stable").head(50)


def _identity(manifest: dict[str, Any]) -> tuple[str, str]:
    identifier = manifest.get("assessment_id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("assessment_pdf_invalid_assessment_identity")
    try:
        completed = datetime.fromisoformat(manifest["completed_utc"])
        if completed.tzinfo is None or completed.utcoffset() is None:
            raise ValueError("timezone required")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("assessment_pdf_invalid_completion_time") from exc
    return identifier, completed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _metadata(page: Any, manifest: dict[str, Any]) -> None:
    identifier, completed = _identity(manifest)
    page.insert_text(
        (30, page.rect.height - 28), f"Vurdering {identifier[:16]} | Gemt {completed}", fontsize=7.5
    )


def _table_page(
    document: Any, manifest: dict[str, Any], rows: pd.DataFrame, summary: str, part: int, parts: int
) -> None:
    """Fixed vector cells cannot expand the page or shift later page origins."""
    page = document.new_page(width=842, height=595)
    period = f"{_period(manifest['origin'])} til {_period(manifest['target'])}"
    mode = "Med periodens Cito" if manifest["mode"] == "hybrid" else "Uden Cito"
    page.insert_text((30, 23), "APOTEK / BESLUTNINGSSTØTTE", fontsize=8.5, color=(0.28, 0.44, 0.43))
    page.insert_text((30, 47), "Prisblik", fontsize=20, color=(0.03, 0.37, 0.35), fontname="hebo")
    page.insert_text((30, 68), f"{period} | {mode}", fontsize=12, fontname="hebo")
    page.insert_text(
        (30, 86), "Top 50 fra den faste udvælgelse - rækkefølge fastlagt før udfald", fontsize=9
    )
    page.insert_text((30, 103), summary, fontsize=9)
    widths = (55, 85, 70, 60, 100, 110, 120, 182)
    headers = (
        ("Varenr.", ""),
        ("AIP nu", "kr."),
        ("Stigning", "%"),
        ("Fald", "%"),
        ("AIP skøn", "kr."),
        ("Ændring skøn", "kr."),
        ("Faktisk ændring", "kr."),
        ("Udfald", ""),
    )
    top, header_height, row_height = 117.0, 28.0, 14.4
    left = 30.0
    for width, labels in zip(widths, headers, strict=True):
        page.draw_rect(
            (left, top, left + width, top + header_height), color=None, fill=(0.03, 0.37, 0.35)
        )
        for number, label in enumerate(labels):
            page.insert_text(
                (left + 6, top + 11 + number * 11),
                label,
                fontsize=9,
                fontname="hebo",
                color=(1, 1, 1),
            )
        left += width
    for index, (_, row) in enumerate(rows.iterrows()):
        y = top + header_height + index * row_height
        if index % 2:
            page.draw_rect((30, y, 812, y + row_height), color=None, fill=(0.94, 0.97, 0.96))
        values = (
            str(row.package_number),
            _number(float(row.aip_ore) / 100),
            _number(row.p_rise, percent=True),
            _number(row.p_fall, percent=True),
            _number(row.amount_3y_next_price_dkk),
            _number(row.amount_3y_delta_dkk),
            _number(row.get("actual_delta_dkk"))
            if str(row.get("outcome_status")) == "observed"
            else "-",
            _status(row),
        )
        left = 30.0
        for column, (width, value) in enumerate(zip(widths, values, strict=True)):
            text_width = pymupdf.get_text_length(value, fontname="helv", fontsize=9)
            if text_width > width - 12:
                raise ValueError("assessment_pdf_cell_overflow")
            x = left + 6 if column in (0, 7) else left + width - 6 - text_width
            page.insert_text((x, y + 10.5), value, fontsize=9)
            left += width
        page.draw_line(
            (30, y + row_height), (812, y + row_height), color=(0.85, 0.90, 0.88), width=0.3
        )
    if rows.empty:
        page.insert_text(
            (36, top + header_height + 20), "Ingen kandidater opfylder udvælgelsen.", fontsize=10
        )
    footer = (
        "Beløb: DKK pr. pakning (AIP). Skøn: beholdt 3-års CatBoost RMSE-model. "
        "Intet valideret prisinterval.",
        "Fast filter: stigning mindst 35%, fald højst 10%, højst 10% af periodens kandidater. "
        f"Tabeldel {part} af {parts}. Se metode og sporbarhed sidst i rapporten.",
        "Diagnostisk gennemgang; historiske udfald er rekonstrueret efterfølgende. "
        "Ingen garanti, målt besparelse eller automatisk bestilling.",
    )
    for index, line in enumerate(footer):
        page.insert_text((30, 523 + index * 12), line, fontsize=8.5)
    _metadata(page, manifest)


def _methods_page(document: Any, assessments: list[tuple[dict[str, Any], pd.DataFrame]]) -> None:
    page = document.new_page(width=595, height=842)
    html = "<h1>Metode og sporbarhed</h1>"
    for manifest, _ in assessments:
        identifier, completed = _identity(manifest)
        protocol = manifest.get("binding", {}).get("protocol", {}).get("sha256")
        if protocol is not None and not re.fullmatch(r"[0-9a-f]{64}", str(protocol)):
            raise ValueError("assessment_pdf_invalid_protocol_identity")
        html += (
            f"<h2>{_period(manifest['origin'])} til {_period(manifest['target'])}</h2>"
            f"<p>Vurderings-ID: {identifier}<br>Gemt: {completed}<br>"
            + (
                f"Modelprotokol SHA-256: {protocol}"
                if protocol
                else "Modelprotokollens hash er ikke med i dette gemte resultat."
            )
            + "</p>"
        )
    html += (
        "<h2>Beholdte modeller</h2><p>Beløb: CatBoost RMSE, tre års træningshistorik, "
        "træningsstop 1. juli 2026. Næste AIP begrænses til mindst nul. "
        "Sandsynligheder: E6 D summer, treårs-reference med understøttede bredere konkurrenter "
        "(1. juli 2026), ellers equal-size/package-modeller (1. januar 2026). "
        "Kalibrering bruger modne udfald fra før 1. september 2026.</p>"
        "<p>Periodens gyldige Cito-koder bruges i Cito-visningen; manglende eller ubrugbare koder "
        "bruger current-only-komponenten. Med og uden Cito bruger hver sin beholdte kalibrering. "
        "Der trænes eller tilpasses ingen modeller ved visning eller eksport.</p>"
        "<h2>Fortolkning og begrænsninger</h2><p>Der findes ikke et valideret prisinterval for "
        "denne præcise modelvisning. Forskelle mellem 3-, 6-års- og fuldhistoriemodeller er ikke "
        "et konfidensinterval. Beløbsskøn er forventet ændring, "
        "ikke beløb betinget af en stigning.</p>"
        "<p>Toplisten rangeres fra prognoser før udfald tilknyttes. Ukendte eller ikke "
        "sammenlignelige udfald tæller ikke som uændrede. Historiske resultater er efterfølgende "
        "rekonstruktion, ikke dokumentation for samtidige prognoser. "
        "Fremtidige udfald er ukendte.</p>"
        "<p>Prisstigning er ikke fortjeneste, leveringsgaranti, "
        "realiseret besparelse eller en ordre. "
        "Lager, salg, udløb, levering og aftalevilkår indgår ikke i en målt indkøbsgevinst. "
        "Rapporten er lokal diagnostisk beslutningsstøtte til faglig gennemgang.</p>"
    )
    spare, _ = page.insert_htmlbox((30, 25, 565, 805), html, css=_CSS, scale_low=1)
    if spare < 0:
        raise ValueError("assessment_pdf_method_page_overflow")


def _summary(rows: pd.DataFrame) -> str:
    if "outcome_status" not in rows or "actual_delta_dkk" not in rows:
        return f"{len(rows)} viste kandidater. Udfald afventer."
    delta = pd.to_numeric(rows.actual_delta_dkk, errors="coerce")
    observed = rows.outcome_status.eq("observed") & delta.notna()
    known = delta.loc[observed]
    return (
        f"{len(rows)} viste kandidater. {len(known)} observerede udfald: "
        f"{int(known.gt(0).sum())} stigninger, {int(known.eq(0).sum())} uændrede, "
        f"{int(known.lt(0).sum())} fald. "
        f"{int((~observed).sum())} afventer eller kan ikke sammenlignes."
    )


def _outcome_chart(document: Any, rows: pd.DataFrame, period: str, mode: str) -> None:
    if "outcome_status" not in rows or "actual_delta_dkk" not in rows:
        return
    observed = rows.loc[rows.outcome_status.eq("observed")].copy()
    x = pd.to_numeric(observed.amount_3y_delta_dkk, errors="coerce")
    y = pd.to_numeric(observed.actual_delta_dkk, errors="coerce")
    finite = x.map(math.isfinite) & y.map(math.isfinite)
    observed, x, y = observed.loc[finite], x.loc[finite], y.loc[finite]
    if observed.empty:
        return
    page = document.new_page(width=595, height=842)
    heading = (
        '<p class="eyebrow">APOTEK / HISTORISK GENNEMGANG</p>'
        "<h1>Skøn og udfald</h1>"
        f"<h2>{period}</h2><p><b>{mode}</b></p>"
        f"<p>{len(observed)} observerede af {len(rows)} viste kandidater. "
        f"{len(rows) - len(observed)} ukendte eller ikke sammenlignelige er ikke tegnet.</p>"
    )
    page.insert_htmlbox((30, 25, 565, 190), heading, css=_CSS, scale_low=1)
    left, top, right, bottom = 91.0, 225.0, 545.0, 645.0
    x_min, y_min = min(0.0, float(x.min())), min(0.0, float(y.min()))
    x_low, x_high = x_min - 20 if x_min < 0 else 0.0, max(0.0, float(x.max())) + 20
    y_low, y_high = y_min - 10, max(0.0, float(y.max())) + 20

    def horizontal(value: float) -> float:
        return left + (value - x_low) / (x_high - x_low) * (right - left)

    def vertical(value: float) -> float:
        return bottom - (value - y_low) / (y_high - y_low) * (bottom - top)

    for index in range(5):
        x_value = x_low + (x_high - x_low) * index / 4
        y_value = y_low + (y_high - y_low) * index / 4
        hx, vy = horizontal(x_value), vertical(y_value)
        page.draw_line((hx, top), (hx, bottom), color=(0.87, 0.91, 0.90), width=0.5)
        page.draw_line((left, vy), (right, vy), color=(0.87, 0.91, 0.90), width=0.5)
        page.insert_textbox(
            (hx - 38, bottom + 7, hx + 38, bottom + 25), _number(x_value), fontsize=8, align=1
        )
        page.insert_textbox((32, vy - 5, left - 6, vy + 12), _number(y_value), fontsize=8, align=2)
    page.draw_rect((left, top, right, bottom), color=(0.37, 0.49, 0.47), width=0.7)
    page.draw_line((horizontal(0), top), (horizontal(0), bottom), color=(0.55, 0.64, 0.62))
    page.draw_line((left, vertical(0)), (right, vertical(0)), color=(0.55, 0.64, 0.62))
    overlap_low, overlap_high = max(x_low, y_low), min(x_high, y_high)
    page.draw_line(
        (horizontal(overlap_low), vertical(overlap_low)),
        (horizontal(overlap_high), vertical(overlap_high)),
        color=(0.48, 0.55, 0.54),
        dashes="[4 3] 0",
        width=1,
    )
    for key in observed.index:
        colour = (0.04, 0.49, 0.44) if y.loc[key] >= 0 else (0.78, 0.36, 0.16)
        page.draw_circle(
            (horizontal(float(x.loc[key])), vertical(float(y.loc[key]))),
            3.5,
            color=colour,
            fill=colour,
            fill_opacity=0.65,
            width=0.5,
        )
    page.insert_textbox(
        (left, bottom + 31, right, bottom + 50),
        "Forventet ændring - DKK pr. pakning",
        fontsize=10,
        align=1,
    )
    page.insert_text((21, 555), "Faktisk ændring - DKK pr. pakning", fontsize=10, rotate=90)
    error = x - y
    caption = (
        "<p>Hver prik er en pakning. Den stiplede linje viser præcis overensstemmelse. "
        "Prikker over linjen steg mere end forventet; prikker under linjen steg mindre "
        "eller faldt mere. Alle observerede beløb er med, også store fejl.</p>"
        f"<p><b>Gennemsnitlig absolut beløbsfejl: {_number(error.abs().mean())} kr.</b><br>"
        f"Gennemsnitligt skøn minus udfald: {_number(error.mean())} kr.</p>"
        '<p class="muted">Historisk, efterfølgende gennemgang af den faste top-50-liste. '
        "Beløbsfejl og retningssandsynlighed er forskellige mål. "
        "Dette er ikke målt fortjeneste eller en garanti for kommende perioder.</p>"
    )
    spare, _ = page.insert_htmlbox((30, 705, 565, 805), caption, css=_CSS, scale_low=1)
    if spare < 0:
        raise ValueError("assessment_pdf_chart_caption_overflow")


def export_pdf(assessments: list[tuple[dict[str, Any], pd.DataFrame]], destination: Path) -> Path:
    """Export explicit saved assessment snapshots, without changing inputs or inference.

    Existing destinations are preserved. Only an allowlist of dated metadata and
    prediction columns is rendered; private artifact paths never enter the PDF.
    """
    if not assessments:
        raise ValueError("assessment_pdf_no_assessments")
    if destination.suffix.lower() != ".pdf":
        raise ValueError("assessment_pdf_extension_required")
    document = pymupdf.open()
    try:
        for manifest, frame in assessments:
            if manifest.get("mode") not in {"current", "hybrid"}:
                raise ValueError("assessment_pdf_unknown_information_mode")
            period = f"{_period(manifest['origin'])} til {_period(manifest['target'])}"
            mode = "Med periodens Cito" if manifest["mode"] == "hybrid" else "Uden Cito"
            _identity(manifest)
            rows = _selected(frame)
            _outcome_chart(document, rows, period, mode)
            if len(document) and "Skøn og udfald" in document[-1].get_text():
                _metadata(document[-1], manifest)
            summary = _summary(rows)
            chunks = [rows.iloc[start : start + 25] for start in range(0, len(rows), 25)] or [rows]
            for part, chunk in enumerate(chunks, 1):
                _table_page(document, manifest, chunk, summary, part, len(chunks))
        # Keep metadata readable as the saved history grows; each page retains
        # the method alongside at most three complete assessment identities.
        for start in range(0, len(assessments), 3):
            _methods_page(document, assessments[start : start + 3])
        for index in range(len(document)):
            page = document[index]
            page.insert_text(
                (30, page.rect.height - 12),
                f"Apotek | Lokal gennemgang | {index + 1} / {len(document)}",
                fontsize=8,
            )
        document.set_metadata({"title": "Prisblik - lokal apoteksgennemgang", "author": "Apotek"})
        payload = document.tobytes(garbage=4, deflate=True)
    finally:
        document.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(payload)
    return destination
