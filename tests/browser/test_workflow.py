"""Real Chromium proof with wholly invented workbooks and isolated sessions."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from openpyxl import load_workbook

from tests.fixtures import synthetic_workbooks

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Locator, Page
playwright = pytest.importorskip(
    "playwright.sync_api", reason="Install the browser dependency group"
)
expect = playwright.expect
pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def workspace_url() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app.py",
            "--server.address=127.0.0.1",
            f"--server.port={port}",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    address = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Synthetic Streamlit test server stopped before becoming ready.")
            try:
                with urlopen(f"{address}/_stcore/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                time.sleep(0.1)
        else:
            pytest.fail("Synthetic Streamlit test server did not become ready.")
        yield address
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _settled(page: Page) -> None:
    expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
    # Widget values update before queued server reruns and streamed DOM updates.
    # Require actual render quiescence before interacting with the next control.
    page.wait_for_function("""() => new Promise(resolve => {
        const app = document.querySelector('[data-testid="stApp"]');
        let timer;
        const observer = new MutationObserver(check);
        function check() {
            clearTimeout(timer);
            if (app.getAttribute('data-test-script-state') === 'notRunning') {
                timer = setTimeout(() => {observer.disconnect(); resolve(true)}, 200);
            }
        }
        observer.observe(app, {attributes:true, childList:true, characterData:true, subtree:true});
        check();
    })""")


def _select(page: Page, label: str, value: str) -> None:
    _settled(page)
    control = page.get_by_role("combobox", name=label, exact=True)
    control.evaluate("e => e.scrollIntoView({block:'center', behavior:'instant'})")
    expect(control).to_be_in_viewport(ratio=1)
    control.click()
    if label == "Find en kandidat (navn eller varenummer)":
        control.fill(value)
    formatted = label in ("Find en kandidat (navn eller varenummer)", "Sammenlign med alternativ")
    option = (
        page.get_by_role("option", name=re.compile(rf"^{re.escape(value)} · "))
        if formatted
        else page.get_by_role("option", name=value, exact=True)
    )
    option.click()
    if formatted:
        expect(control).to_have_value(re.compile(rf"^{re.escape(value)} · "))
    else:
        expect(control).to_have_value(value)
    if formatted:
        expect(
            page.get_by_test_id("stText").filter(has_text=re.compile(rf"^{re.escape(value)} · "))
        ).to_be_visible()
    _settled(page)


def _load(page: Page, workspace_url: str, tmp_path: Path, count: int = 120) -> None:
    cito, history, period = synthetic_workbooks(count=count, periods=40)
    page.goto(workspace_url)
    expect(page.get_by_text("Upload Cito-rapport og LMS-prishistorik", exact=False)).to_be_visible()
    page.screenshot(path=str(tmp_path / "empty-laptop.png"))
    uploads = page.locator('input[type="file"]')
    uploads.nth(0).set_input_files(
        {
            "name": "synthetic-cito.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": cito,
        }
    )
    expect(
        page.get_by_role("button", name="Remove synthetic-cito.xlsx", exact=True)
    ).to_be_visible()
    _settled(page)
    uploads.nth(1).set_input_files(
        {
            "name": "synthetic-lms.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": history,
        }
    )
    expect(page.get_by_role("button", name="Remove synthetic-lms.xlsx", exact=True)).to_be_visible()
    _settled(page)
    date_input = page.get_by_test_id("stDateInput").locator("input")
    date_input.fill(period.isoformat())
    date_input.press("Enter")
    _settled(page)
    page.get_by_role("button", name="Indlæs og kontroller", exact=True).click()
    expect(
        page.get_by_text(f"Viser {min(50, count)} af 50 ønskede kandidater", exact=False)
    ).to_be_visible(timeout=20000)


def _download(page: Page, label: str, tmp_path: Path) -> Path:
    with page.expect_download() as event:
        page.get_by_role("button", name=label, exact=True).click()
    result = event.value
    target = tmp_path / result.suggested_filename
    result.save_as(target)
    return target


def _visible_height(locator: Locator) -> float:
    """Measure visible height after viewport and ancestor clipping."""
    return float(
        locator.evaluate("""element => {
        const rect = element.getBoundingClientRect();
        let top = Math.max(64, rect.top), bottom = Math.min(innerHeight, rect.bottom);
        for (let parent = element.parentElement; parent; parent = parent.parentElement) {
            if (/(auto|scroll|hidden)/.test(getComputedStyle(parent).overflowY)) {
                const clip = parent.getBoundingClientRect();
                top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom);
            }
        }
        return Math.max(0, bottom - top);
    }""")
    )


def _linked_layout_evidence(page: Page, tmp_path: Path) -> None:
    _settled(page)
    queue = page.get_by_role("heading", name="Kandidater", exact=True)
    detail = page.get_by_role("heading", name="Pris og beslutning", exact=True)
    queue_column = queue.locator("xpath=ancestor::div[@data-testid='stColumn'][1]")
    detail_column = detail.locator("xpath=ancestor::div[@data-testid='stColumn'][1]")
    grid = page.locator('[data-testid="stDataFrame"]:visible').first
    chart = page.get_by_test_id("stPlotlyChart").first
    form = page.locator(".st-key-review_editor")
    for size, width, height in (("laptop", 1366, 768), ("desktop", 1920, 1080)):
        page.set_viewport_size({"width": width, "height": height})
        left, right = queue_column.bounding_box(), detail_column.bounding_box()
        assert left is not None and right is not None
        assert left["x"] + left["width"] <= right["x"], "List/detail must be side by side"
        page.get_by_role("tab", name="Pris og alternativer", exact=True).click()
        queue.evaluate("e => e.scrollIntoView({block:'start'})")
        chart.scroll_into_view_if_needed()
        assert _visible_height(grid) >= 120, "Candidate rows must remain visible with chart"
        assert _visible_height(chart) >= 250, "Chart must be usable alongside the candidate list"
        page.screenshot(path=str(tmp_path / f"linked-chart-{size}.png"))
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        form.scroll_into_view_if_needed()
        assert _visible_height(grid) >= 120, "Candidate rows must remain visible during review"
        assert _visible_height(form) >= 180, "Review must be usable alongside the candidate list"
        page.screenshot(path=str(tmp_path / f"linked-review-{size}.png"))
    page.set_viewport_size({"width": 600, "height": 900})
    left, right = queue_column.bounding_box(), detail_column.bounding_box()
    assert left is not None and right is not None
    assert abs(left["x"] - right["x"]) <= 2, "Native columns must stack on narrow screens"
    assert right["y"] > left["y"]
    assert left["width"] > 450 and right["width"] > 450
    detail.evaluate("e => e.scrollIntoView({block:'start'})")
    page.screenshot(path=str(tmp_path / "decision-narrow.png"))
    page.get_by_role("tab", name="Pris og alternativer", exact=True).click()
    chart.scroll_into_view_if_needed()
    assert _visible_height(chart) >= 250
    page.screenshot(path=str(tmp_path / "stacked-narrow.png"))
    page.set_viewport_size({"width": 1366, "height": 768})


def test_decision_navigation(browser: Browser, workspace_url: str, tmp_path: Path) -> None:
    context = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(10000)
    try:
        _load(page, workspace_url, tmp_path, count=6)
        selector = page.get_by_role(
            "combobox", name="Find en kandidat (navn eller varenummer)", exact=True
        )
        # Human names are searchable while product IDs remain the review owner.
        selector.click()
        selector.fill("Eksempelpakning 2")
        page.get_by_role("option", name="000002 · Eksempelpakning 2", exact=True).click()
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        expect(
            page.get_by_test_id("stText").filter(has_text=re.compile(r"^000002 · "))
        ).to_be_visible()
        _settled(page)
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        note = page.get_by_role("textbox", name="Note", exact=True)
        note.fill("Ugemt kladde til vare 2")
        note.press("Tab")
        expect(page.get_by_text("Ugemte ændringer.", exact=False)).to_be_visible()
        page.get_by_role("button", name="Næste", exact=True).click()
        expect(selector).to_have_value("000003 · Eksempelpakning 3")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(note).to_have_value("")
        page.get_by_role("button", name="Forrige", exact=True).click()
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(note).to_have_value("Ugemt kladde til vare 2")
        page.get_by_role("button", name="Gem og næste", exact=True).click()
        expect(selector).to_have_value("000003 · Eksempelpakning 3")
        expect(page.get_by_role("tab", name="Pris og alternativer", exact=True)).to_have_attribute(
            "aria-selected", "true"
        )
        _select(page, "Find en kandidat (navn eller varenummer)", "000002")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(note).to_have_value("Ugemt kladde til vare 2")
        expect(page.get_by_text("Ugemte ændringer.", exact=False)).to_have_count(0)
        page.get_by_role("tab", name="Pris og alternativer", exact=True).click()
        # Compare a noncandidate with history without moving the review owner.
        _select(page, "Sammenlign med alternativ", "900001")
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        expect(page.get_by_text("Opbevaring H:", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Åbn alternativ som kandidat")).to_have_count(0)
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[0].meta==='000002' "
            "&& p.data[1].meta==='900001' && p.data[0].line.dash!==p.data[1].line.dash "
            "&& p.data[0].marker.symbol!==p.data[1].marker.symbol}"
        )
        # Missing-history group records remain selectable and explain missingness.
        _select(page, "Sammenlign med alternativ", "900002")
        expect(
            page.get_by_text("Historik mangler for dette alternativ.", exact=False)
        ).to_be_visible()
        expect(page.get_by_text("Aktuel AIP Mangler kr.", exact=False)).to_be_visible()
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        _select(page, "Sammenlign med alternativ", "900004")
        page.get_by_role("button", name="Åbn alternativ som kandidat", exact=True).click()
        expect(selector).to_have_value("900004 · Navn ikke oplyst")
        expect(
            page.get_by_text("Historisk prisniveau kan ikke sammenlignes", exact=False)
        ).to_be_visible()
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(note).to_have_value("")
        _select(page, "Find en kandidat (navn eller varenummer)", "000002")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(note).to_have_value("Ugemt kladde til vare 2")
        page.get_by_role("tab", name="Pris og alternativer", exact=True).click()
        expect(
            page.get_by_role("combobox", name="Sammenlign med alternativ", exact=True)
        ).to_have_value("900004 · Navn ikke oplyst")
        _select(page, "Sammenlign med alternativ", "000001")
        page.get_by_role("button", name="Åbn alternativ som kandidat", exact=True).click()
        expect(selector).to_have_value("000001 · Eksempelpakning 1")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(note).to_have_value("")
        page.get_by_text("Eksport og sessionskopi", exact=True).click()
        saved = _download(page, "Hent sessionskopi (JSON)", tmp_path)
        payload = json.loads(saved.read_bytes())
        assert payload["reviews"]["000002"]["note"] == "Ugemt kladde til vare 2"
        assert "000001" not in payload["reviews"]
        assert "900004" not in payload["reviews"]
    except Exception:
        page.screenshot(path=str(tmp_path / "decision-failure.png"), full_page=True)
        (tmp_path / "decision-failure.html").write_text(page.content(), encoding="utf-8")
        print(f"Synthetic decision artifacts: {tmp_path}")
        raise
    finally:
        context.close()


def test_real_workflow(browser: Browser, workspace_url: str, tmp_path: Path) -> None:
    context = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(10000)
    try:
        _load(page, workspace_url, tmp_path)
        requested = page.get_by_role("spinbutton", name="Antal kandidater", exact=True)
        requested.fill("80")
        requested.press("Enter")
        expect(page.get_by_text("Viser 80 af 80 ønskede kandidater", exact=False)).to_be_visible()
        # Glide's supported native dataframe is canvas-rendered. Its first header/row
        # coordinates are measured from the visible grid, never fixed screen positions.
        grid = page.locator('[data-testid="stDataFrame"]:visible').first
        grid.scroll_into_view_if_needed()
        canvas = grid.locator("canvas").first
        expect(canvas).to_be_visible()
        box = canvas.bounding_box()
        assert box is not None
        grid.click(position={"x": 16, "y": 58})
        selector = page.get_by_role(
            "combobox", name="Find en kandidat (navn eller varenummer)", exact=True
        )
        expect(selector).to_have_value("000001 · Eksempelpakning 1")
        expect(page.get_by_role("button", name="Clear selection", exact=True)).to_be_visible()
        page.get_by_role("button", name="Næste", exact=True).click()
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        expect(page.get_by_role("button", name="Clear selection", exact=True)).to_have_count(0)
        grid.click(position={"x": 16, "y": 58})
        expect(selector).to_have_value("000001 · Eksempelpakning 1")
        # The column's native menu exposes accessible sorting commands.
        grid.click(position={"x": 75, "y": 15})
        page.get_by_text("Sort descending", exact=False).click()
        grid.click(position={"x": 16, "y": 58})
        expect(selector).to_have_value("000080 · Eksempelpakning 80")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        page.get_by_role("textbox", name="Note", exact=True).fill("=Syntetisk gennemgang")
        page.get_by_text("Medtag i valgt eksport", exact=True).click()
        _select(page, "Vurdering", "Interessant")
        page.get_by_role("button", name="Gem vurdering", exact=True).click()
        expect(page.get_by_text("Vurderingen er gemt", exact=False)).to_be_visible()
        requested.fill("17")
        requested.press("Enter")
        expect(page.get_by_text("Viser 17 af 17 ønskede kandidater", exact=False)).to_be_visible()
        expect(selector).to_have_value("000080 · Eksempelpakning 80")
        expect(page.get_by_role("textbox", name="Note", exact=True)).to_have_value(
            "=Syntetisk gennemgang"
        )
        expect(
            page.get_by_text("uden for den aktuelle prioriteringskø", exact=False)
        ).to_be_visible()
        search = page.get_by_role("textbox", name="Søg i den valgte visning", exact=True)
        search.fill("000080")
        search.press("Enter")
        expect(page.get_by_text("Ingen produkter i denne visning", exact=False)).to_be_visible()
        search.fill("")
        search.press("Enter")
        _select(page, "Visning", "Alle prioriterbare")
        expect(page.get_by_text("Alle 120 prioriterbare kandidater.", exact=False)).to_be_visible()
        _settled(page)
        page.get_by_role("tab", name="Pris og alternativer", exact=True).click()
        page.get_by_text("Grafvisning", exact=True).click()
        page.get_by_text("Seneste 26 perioder", exact=True).click()
        expect(page.get_by_role("radio", name="Seneste 26 perioder", exact=True)).to_be_checked()
        page.get_by_text("Retrospektiv visning (også senere priser)", exact=True).click()
        _select(page, "Find en kandidat (navn eller varenummer)", "000001")
        expect(
            page.get_by_test_id("stText").filter(
                has_text=re.compile(r"^000001 · Eksempelpakning 1$")
            )
        ).to_be_visible()
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data[0].meta==='000001' && p.data[0].x.length===26}"
        )
        _settled(page)
        page.get_by_text("Alle 7 varer i gruppen / flere kurver", exact=True).click()
        _settled(page)
        overlay = page.get_by_role(
            "combobox", name="Vis fundne gruppealternativer i grafen (højst 5)"
        )
        overlay.click()
        overlay.fill("900001")
        page.get_by_role(
            "option", name="900001 · Opbevaringsfravalgt alternativ", exact=True
        ).click()
        page.keyboard.press("Escape")
        expect(page.locator('[data-testid="stPlotlyChart"]').first).to_be_visible()
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[0].x.length===26 "
            "&& p.data[1].meta==='900001'}"
        )
        page.get_by_text("Grafvisning", exact=True).click()
        expect(
            page.get_by_text("Gruppefuldstændighed er ikke verificeret", exact=False)
        ).to_be_visible()
        expect(
            page.get_by_role("tabpanel", name="Pris og alternativer")
            .get_by_test_id("stDataFrame")
            .first
        ).to_be_visible()
        page.get_by_text("Alle 7 varer i gruppen / flere kurver", exact=True).click()
        _select(page, "Visning", "Top N")
        requested.fill("80")
        requested.press("Enter")
        page.get_by_text("Eksport og sessionskopi", exact=True).click()
        page.get_by_role("button", name="Klargør Excel", exact=True).click()
        workbook_path = _download(page, "Hent Excel", tmp_path)
        workbook = load_workbook(BytesIO(workbook_path.read_bytes()))
        assert workbook.sheetnames[0] == "Indkøbsoverblik"
        rows = list(workbook["Kandidater"].iter_rows(min_row=2, values_only=True))
        assert [row[1] for row in rows] == [f"{i:06d}" for i in range(1, 81)]
        assert rows[0][3] == 100 and isinstance(rows[0][3], (int, float))
        assert rows[79][25:27] == ("Interessant", "=Syntetisk gennemgang")
        assert workbook["Kandidater"]["AA81"].data_type == "s"
        saved = _download(page, "Hent sessionskopi (JSON)", tmp_path)
        payload = json.loads(saved.read_bytes())
        assert payload["queue"] == {"mode": "top_n", "top_n": 80}
        assert payload["reviews"]["000080"]["note"] == "=Syntetisk gennemgang"
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        note = page.get_by_role("textbox", name="Note", exact=True)
        note.fill("Midlertidig ugemt kladde før gendannelse")
        note.press("Tab")
        expect(page.get_by_text("Ugemte ændringer.", exact=False)).to_be_visible()
        requested.fill("17")
        requested.press("Enter")
        page.locator('input[type="file"]').nth(2).set_input_files(saved)
        page.get_by_role("button", name="Gendan vurderinger", exact=True).click()
        expect(page.get_by_text("Sessionskopien er gendannet", exact=False)).to_be_visible()
        expect(requested).to_have_value("80")
        expect(note).to_have_value("")
        expect(page.get_by_text("Ugemte ændringer.", exact=False)).to_have_count(0)
        page.get_by_role("tab", name="Pris og alternativer", exact=True).click()
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[1].meta==='900001'}"
        )
        _select(page, "Find en kandidat (navn eller varenummer)", "000080")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(note).to_have_value("=Syntetisk gennemgang")
        _select(page, "Find en kandidat (navn eller varenummer)", "000001")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(note).to_have_value("")
        page.get_by_role("tab", name="Pris og alternativer", exact=True).click()
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[0].meta==='000001' "
            "&& p.data[1].meta==='900001'}"
        )
        _linked_layout_evidence(page, tmp_path)
        for size, width, height in (("laptop", 1366, 768), ("desktop", 1920, 1080)):
            page.set_viewport_size({"width": width, "height": height})
            for section, heading in (
                ("workspace", "Redbird · indkøbsgennemgang"),
                ("table", "Kandidater"),
                ("detail", "Pris og beslutning"),
            ):
                page.get_by_role("heading", name=heading, exact=True).evaluate(
                    "e => e.scrollIntoView({block:'start'})"
                )
                page.screenshot(path=str(tmp_path / f"{section}-{size}.png"))
            page.get_by_test_id("stPlotlyChart").first.evaluate(
                "e => e.scrollIntoView({block:'start'})"
            )
            page.screenshot(path=str(tmp_path / f"chart-{size}.png"))
        page.set_viewport_size({"width": 1366, "height": 768})
        # A failed replacement may not overwrite the loaded sources or saved notes.
        page.get_by_text("Datagrundlag og indstillinger", exact=True).click()
        page.get_by_text("Jeg accepterer at erstatte den aktive vurdering", exact=False).click()
        page.locator('input[type="file"]').nth(0).set_input_files(
            {
                "name": "synthetic-broken.xlsx",
                "mimeType": "application/octet-stream",
                "buffer": b"invalid workbook",
            }
        )
        # Upload completion and the server rerun are separate browser events. The
        # pending-input notice proves Streamlit has consumed the changed source.
        expect(
            page.get_by_role("button", name="Remove synthetic-broken.xlsx", exact=True)
        ).to_be_visible()
        expect(page.get_by_text("Der er ændrede input.", exact=False)).to_be_visible()
        _settled(page)
        page.get_by_role("button", name="Anvend indstillinger", exact=True).click()
        expect(page.get_by_text("tidligere, sammenhængende analyse", exact=False)).to_be_visible()
        _select(page, "Find en kandidat (navn eller varenummer)", "000080")
        page.get_by_role("tab", name="Vurdering", exact=True).click()
        expect(page.get_by_role("textbox", name="Note", exact=True)).to_have_value(
            "=Syntetisk gennemgang"
        )
        page.get_by_role("heading", name="Redbird · indkøbsgennemgang", exact=True).evaluate(
            "e => e.scrollIntoView({block:'start'})"
        )
        page.screenshot(path=str(tmp_path / "error-laptop.png"))
        print(f"Synthetic browser screenshots: {tmp_path}")
        other = browser.new_context(viewport={"width": 1366, "height": 768})
        try:
            isolated = other.new_page()
            isolated.goto(workspace_url)
            expect(
                isolated.get_by_text("Upload Cito-rapport og LMS-prishistorik", exact=False)
            ).to_be_visible()
            expect(isolated.get_by_role("textbox", name="Note", exact=True)).to_have_count(0)
            expect(isolated.get_by_text("synthetic-cito.xlsx", exact=False)).to_have_count(0)
            _load(isolated, workspace_url, tmp_path, count=3)
            isolated.get_by_role("tab", name="Vurdering", exact=True).click()
            expect(isolated.get_by_role("textbox", name="Note", exact=True)).to_have_value("")
            isolated.get_by_role("textbox", name="Note", exact=True).fill("Anden session")
            isolated.get_by_role("button", name="Gem vurdering", exact=True).click()
            expect(isolated.get_by_text("Vurderingen er gemt", exact=False)).to_be_visible()
            _select(page, "Find en kandidat (navn eller varenummer)", "000001")
            expect(page.get_by_role("textbox", name="Note", exact=True)).to_have_value("")
            _select(page, "Find en kandidat (navn eller varenummer)", "000080")
            expect(page.get_by_role("textbox", name="Note", exact=True)).to_have_value(
                "=Syntetisk gennemgang"
            )
            expect(page.get_by_text("120 kan prioriteres", exact=False).first).to_be_visible()
        finally:
            other.close()
    except Exception:
        page.screenshot(path=str(tmp_path / "failure.png"), full_page=True)
        (tmp_path / "failure.html").write_text(page.content(), encoding="utf-8")
        print(f"Synthetic browser failure artifacts: {tmp_path}")
        raise
    finally:
        context.close()
