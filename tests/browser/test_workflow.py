"""Real Chromium proof with wholly invented workbooks and isolated sessions."""

from __future__ import annotations

import json
import os
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
    from playwright.sync_api import Browser, Page

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


def _select(page: Page, label: str, value: str) -> None:
    control = page.get_by_role("combobox", name=label, exact=True)
    control.click()
    control.fill(value)
    page.get_by_role("option", name=value, exact=True).click()


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
    uploads.nth(1).set_input_files(
        {
            "name": "synthetic-lms.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": history,
        }
    )
    date_input = page.get_by_test_id("stDateInput").locator("input")
    date_input.fill(period.isoformat())
    date_input.press("Enter")
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
        selector = page.get_by_role("combobox", name="Vælg varenr. (alle kandidater)", exact=True)
        expect(selector).to_have_value("000001")

        # The column's native menu exposes accessible sorting commands.
        grid.click(position={"x": 125, "y": 15})
        page.get_by_text("Sort descending", exact=False).click()
        grid.click(position={"x": 16, "y": 58})
        expect(selector).to_have_value("000080")
        page.get_by_role("textbox", name="Note", exact=True).fill("=Syntetisk gennemgang")
        page.get_by_text("Medtag i valgt eksport", exact=True).click()
        _select(page, "Vurdering", "Interessant")
        page.get_by_role("button", name="Gem vurdering", exact=True).click()
        expect(page.get_by_text("Vurderingen er gemt", exact=False)).to_be_visible()

        requested.fill("17")
        requested.press("Enter")
        expect(page.get_by_text("Viser 17 af 17 ønskede kandidater", exact=False)).to_be_visible()
        expect(selector).to_have_value("000080")
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
        _select(page, "Grafens visningsvindue", "Seneste 26 perioder")
        page.get_by_text("Retrospektiv visning (også senere priser)", exact=True).click()
        _select(page, "Vælg varenr. (alle kandidater)", "000001")
        expect(page.get_by_text("000001 · Eksempelpakning 1", exact=True)).to_be_visible()
        overlay = page.get_by_role(
            "combobox", name="Vis fundne gruppealternativer i grafen (højst 5)"
        )
        overlay.click()
        overlay.fill("900001")
        page.get_by_role("option", name="900001", exact=True).click()
        page.keyboard.press("Escape")
        expect(page.locator('[data-testid="stPlotlyChart"]').first).to_be_visible()
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[0].x.length===26 "
            "&& p.data[1].name==='900001'}"
        )

        _select(page, "Visning", "Top N")
        requested.fill("80")
        requested.press("Enter")
        page.get_by_role("button", name="Klargør Excel", exact=True).click()
        workbook_path = _download(page, "Hent Excel", tmp_path)
        workbook = load_workbook(BytesIO(workbook_path.read_bytes()))
        rows = list(workbook["Kandidater"].iter_rows(min_row=2, values_only=True))
        assert [row[1] for row in rows] == [f"{i:06d}" for i in range(1, 81)]
        assert rows[0][3] == 100 and isinstance(rows[0][3], (int, float))
        assert rows[79][25:27] == ("Interessant", "=Syntetisk gennemgang")
        assert workbook["Kandidater"]["AA81"].data_type == "s"
        saved = _download(page, "Hent sessionskopi (JSON)", tmp_path)
        payload = json.loads(saved.read_bytes())
        assert payload["queue"] == {"mode": "top_n", "top_n": 80}
        assert payload["reviews"]["000080"]["note"] == "=Syntetisk gennemgang"

        requested.fill("17")
        requested.press("Enter")
        page.locator('input[type="file"]').nth(2).set_input_files(saved)
        page.get_by_role("button", name="Gendan vurderinger", exact=True).click()
        expect(page.get_by_text("Sessionskopien er gendannet", exact=False)).to_be_visible()
        expect(requested).to_have_value("80")
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[1].name==='900001'}"
        )

        for size, width, height in (("laptop", 1366, 768), ("desktop", 1920, 1080)):
            page.set_viewport_size({"width": width, "height": height})
            for section, heading in (
                ("workspace", "Redbird · indkøbsgennemgang"),
                ("table", "Kandidater til gennemgang"),
                ("detail", "Vare og vurdering"),
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
        page.get_by_text("Jeg accepterer at erstatte den aktive vurdering", exact=False).click()
        page.locator('input[type="file"]').nth(0).set_input_files(
            {
                "name": "synthetic-broken.xlsx",
                "mimeType": "application/octet-stream",
                "buffer": b"invalid workbook",
            }
        )
        page.get_by_role("button", name="Anvend indstillinger", exact=True).click()
        expect(page.get_by_text("tidligere, sammenhængende analyse", exact=False)).to_be_visible()
        _select(page, "Vælg varenr. (alle kandidater)", "000080")
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
            expect(isolated.get_by_role("textbox", name="Note", exact=True)).to_have_value("")
            isolated.get_by_role("textbox", name="Note", exact=True).fill("Anden session")
            isolated.get_by_role("button", name="Gem vurdering", exact=True).click()
            expect(isolated.get_by_text("Vurderingen er gemt", exact=False)).to_be_visible()
            _select(page, "Vælg varenr. (alle kandidater)", "000001")
            expect(page.get_by_role("textbox", name="Note", exact=True)).to_have_value("")
            _select(page, "Vælg varenr. (alle kandidater)", "000080")
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
