"""Wholly invented recipes: no private source cells are used."""

from datetime import date, timedelta
from io import BytesIO

from openpyxl import Workbook

from bef_redbird.domain import AnalysisContext, AnalysisResult, AnalysisSettings
from bef_redbird.imports import parse_cito, parse_lms_history
from bef_redbird.service import build_analysis


def synthetic_workbooks(count: int = 120, periods: int = 26) -> tuple[bytes, bytes, date]:
    calendar = [date(2025, 1, 6) + timedelta(days=14 * i) for i in range(periods)]
    cito = Workbook()
    c = cito.active
    c.title = "Table"
    c.append(["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"])
    history = Workbook()
    h = history.active
    h.title = "Medicinpriser data"
    h.append(["Varenummer", "Indikator", "Lægemiddel", *[d.strftime("%Y%m%d") for d in calendar]])
    for i in range(1, count + 1):
        pid = f"{i:06d}"
        c.append([pid, "", 100, "A", "C", str((i - 1) // 3 + 1)])
        prices = [120 if j % 4 == 1 else 100 for j in range(periods)]
        prices[-1] = 100
        h.append([pid, "AIP", f"Eksempelpakning {i}", *prices])
        h.append([pid, "AUP", "Ignoreret indikator", *[999] * periods])
    c.append(["900001", "H", 100, "A", "C", "1"])
    c.append(["900002", "", 1000, "A", "C", "1"])
    c.append(["900003", "", 100, "A", "A", "1"])
    c.append(["900004", "", 100, "B", "C", "1"])  # eligible, missing history
    c.append(["900005", "", 100, "A", "C", None])
    c.append(["900005", "", 101, "A", "C", "2"])  # conflict
    c.append(["000001", "", 100, "A", "C", "1"]) if count else None
    h.append(["900001", "AIP", "Opbevaringsfravalgt alternativ", *[100] * periods])
    output_c, output_h = BytesIO(), BytesIO()
    cito.save(output_c)
    history.save(output_h)
    return output_c.getvalue(), output_h.getvalue(), calendar[-1]


def load_analysis(count: int = 120, periods: int = 26) -> AnalysisResult:
    c, h, current = synthetic_workbooks(count, periods)
    return build_analysis(
        parse_cito(c), parse_lms_history(h), AnalysisContext(current), AnalysisSettings()
    )
