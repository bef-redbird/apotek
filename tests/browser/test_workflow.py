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


def _keyboard_picker(page: Page) -> None:
    details = page.get_by_text("Vælg fra listen med tastatur", exact=True).locator(
        "xpath=ancestor::details[1]"
    )
    if details.get_attribute("open") is None:
        details.locator("summary").click()


def _alternative(page: Page, product_id: str) -> Locator:
    _settled(page)
    checkbox = page.get_by_role("checkbox", name=re.compile(rf"^{product_id} · "))
    if not checkbox.is_visible():
        page.get_by_text(re.compile(r"^Vis flere alternativer ")).click()
    checkbox.evaluate("e => e.scrollIntoView({block:'center', behavior:'instant'})")
    return checkbox


def _toggle_alternative(page: Page, product_id: str, *, checked: bool = True) -> None:
    checkbox = _alternative(page, product_id)
    expect(checkbox).to_be_enabled()
    # Native Streamlit's checkbox input is covered by its associated label.
    # Exercise the accessible keyboard interaction without bypassing event handlers.
    if checkbox.is_checked() != checked:
        checkbox.press("Space")
    expect(checkbox).to_be_checked(checked=checked)
    _settled(page)


def _select(page: Page, label: str, value: str) -> None:
    _settled(page)
    if label == "Kandidat i den viste liste":
        _keyboard_picker(page)
    control = page.get_by_role("combobox", name=label, exact=True)
    control.evaluate("e => e.scrollIntoView({block:'center', behavior:'instant'})")
    expect(control).to_be_in_viewport(ratio=1)
    control.click()
    if label == "Kandidat i den viste liste":
        control.fill(value)
    formatted = label in ("Kandidat i den viste liste",)
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


def _load(
    page: Page,
    workspace_url: str,
    tmp_path: Path,
    count: int = 120,
    *,
    descriptive_labels: bool = False,
    large_group: bool = False,
) -> None:
    cito, history, period = synthetic_workbooks(count=count, periods=40)
    if large_group:
        workbook = load_workbook(BytesIO(cito))
        for row in workbook["Table"].iter_rows(min_row=2):
            row[5].value = "1"
        output = BytesIO()
        workbook.save(output)
        cito = output.getvalue()
    if descriptive_labels:
        # Display-only metadata exercises ordinary long pharmacy descriptions.
        workbook = load_workbook(BytesIO(history))
        sheet = workbook["Medicinpriser data"]
        for row in sheet.iter_rows(min_row=2):
            if row[1].value == "AIP":
                row[2].value = (
                    "Eksempellægemiddel med langt navn 100 mg filmovertrukne tabletter · "
                    "100 stk. blisterpakning · Syntetisk medicinalfirma Danmark"
                )
        output = BytesIO()
        workbook.save(output)
        history = output.getvalue()
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
    page.get_by_role("heading", name="Redbird · indkøbsgennemgang", exact=True).evaluate(
        "e => e.scrollIntoView({block:'start'})"
    )
    expect(
        page.get_by_text(
            "Prioritering til gennemgang — ikke dokumenteret købsgevinst.", exact=False
        )
    ).to_be_in_viewport(ratio=1)


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
    queue = page.locator(".st-key-candidate_list")
    prices = page.locator(".st-key-price_workspace")
    form = page.locator(".st-key-review_editor")
    grid = queue.get_by_test_id("stDataFrame")
    chart = prices.get_by_test_id("stPlotlyChart").first
    documentation = page.get_by_text("Kilder, metode og datakontrol", exact=True)
    metrics = {}
    expect(page.get_by_role("tab")).to_have_count(0)
    for size, width, height in (
        ("laptop", 1366, 768),
        ("desktop", 1920, 1080),
        ("narrow", 600, 900),
    ):
        page.set_viewport_size({"width": width, "height": height})
        # Plotly resizes asynchronously after the native container has reflowed.
        page.wait_for_function("""() => {
            const plot = document.querySelector('.js-plotly-plot');
            return plot && Math.abs(plot._fullLayout.width - plot.clientWidth) < 3;
        }""")
        sections = [section.bounding_box() for section in (queue, prices, form)]
        assert all(sections)
        candidate_box, price_box, review_box = sections
        assert candidate_box is not None and price_box is not None and review_box is not None
        if width > 600:
            assert abs(candidate_box["y"] - price_box["y"]) <= 2, sections
            assert candidate_box["x"] + candidate_box["width"] < price_box["x"], sections
            assert price_box["width"] > candidate_box["width"], sections
            assert candidate_box["width"] >= 450, sections
            assert review_box["width"] > candidate_box["width"] + price_box["width"], sections
        else:
            assert candidate_box["y"] + candidate_box["height"] <= price_box["y"], sections
            assert abs(candidate_box["x"] - price_box["x"]) <= 2, sections
            assert abs(candidate_box["width"] - price_box["width"]) <= 2, sections
            assert candidate_box["width"] >= 450, sections
        assert review_box["y"] >= max(
            candidate_box["y"] + candidate_box["height"],
            price_box["y"] + price_box["height"],
        ), sections
        assert abs(review_box["x"] - candidate_box["x"]) <= 2, sections
        grid_box = grid.bounding_box()
        assert grid_box is not None and grid_box["width"] >= candidate_box["width"] - 40
        assert grid_box["height"] >= 590, "Candidate rows must have a readable table viewport"
        dimensions = chart.locator(".js-plotly-plot").evaluate("""plot => ({
            width: plot._fullLayout._size.w,
            height: plot._fullLayout._size.h,
            font: plot._fullLayout.font.size,
        })""")
        assert dimensions["height"] >= 320, dimensions
        assert dimensions["width"] >= (500 if width > 600 else 300), dimensions
        assert dimensions["font"] >= 12, dimensions
        method_box = page.get_by_role(
            "heading", name="Sådan læses analysen", exact=True
        ).bounding_box()
        assert method_box is not None
        assert method_box["y"] > review_box["y"] + review_box["height"]
        assert method_box["width"] <= min(1100, width - 32) + 1, method_box
        documentation_box = documentation.bounding_box()
        assert documentation_box is not None
        assert documentation_box["y"] > review_box["y"] + review_box["height"]
        overflow = page.evaluate("""() => ({
            viewport: innerWidth,
            page: document.documentElement.scrollWidth,
            controls: [...document.querySelectorAll('input,textarea,button')]
                .filter(element => {
                    const rect = element.getBoundingClientRect();
                    return element.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})
                        && rect.width && rect.height
                        && (rect.left < -1 || rect.right > innerWidth+1);
                }).map(element => element.getAttribute('aria-label') || element.textContent),
        })""")
        assert overflow["page"] <= overflow["viewport"] + 1, overflow
        assert not overflow["controls"], overflow
        grid_scroll = grid.evaluate("""element => [...element.querySelectorAll('*')]
            .filter(node => /(auto|scroll)/.test(getComputedStyle(node).overflowX))
            .map(node => ({width:node.clientWidth, content:node.scrollWidth}))""")
        assert grid_scroll, "The native table scroll viewport must be measurable"
        assert all(item["content"] <= item["width"] + 2 for item in grid_scroll), grid_scroll
        expect(grid.locator("th")).to_have_text(
            ["Prioritet", "Varenr.", "Lægemiddel, styrke og pakning", "Kostpris, kr."]
        )
        metrics[size] = {
            "sections": sections,
            "grid": grid_box,
            "plot": dimensions,
            "grid_scroll": grid_scroll,
        }
        for section, target in (
            ("controls", queue),
            ("table", grid),
            ("chart", chart),
            ("review", form),
        ):
            target.evaluate("""e => {
                e.scrollIntoView({block:'start'});
                e.closest('[data-testid="stMain"]').scrollBy(0, -80);
            }""")
            if section == "chart":
                assert _visible_height(chart) >= 440
            page.screenshot(path=str(tmp_path / f"linked-{section}-{size}.png"))
    (tmp_path / "layout-metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    page.set_viewport_size({"width": 1366, "height": 768})


def test_descriptive_layout(browser: Browser, workspace_url: str, tmp_path: Path) -> None:
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    page.set_default_timeout(10000)
    try:
        _load(page, workspace_url, tmp_path, descriptive_labels=True)
        expect(
            page.get_by_test_id("stText").filter(
                has_text=re.compile(r"^000001 · Eksempellægemiddel")
            )
        ).to_be_visible()
        _linked_layout_evidence(page, tmp_path)
        for size, width, height in (("laptop", 1366, 768), ("desktop", 1920, 1080)):
            page.set_viewport_size({"width": width, "height": height})
            for section, label in (
                ("flow", "Fra Cito-kandidat til din vurdering"),
                ("settings", "Anvendte indstillinger og tidsgrænse"),
                ("calculations", "Beregninger og præcise grænser"),
                ("sources", "Kilder, metode og datakontrol"),
            ):
                documentation = page.get_by_text(label, exact=True)
                documentation.click()
                _settled(page)
                # Native details expansion animates independently of Streamlit reruns.
                documentation.locator("xpath=ancestor::details[1]").evaluate("""element =>
                    new Promise(resolve => {
                        let previous = -1, stable = 0;
                        function check() {
                            const height = element.getBoundingClientRect().height;
                            stable = Math.abs(height - previous) < 0.1 ? stable + 1 : 0;
                            previous = height;
                            if (stable >= 8) resolve(true);
                            else requestAnimationFrame(check);
                        }
                        requestAnimationFrame(check);
                    })
                """)
                documentation.evaluate("""e => {
                    e.scrollIntoView({block:'start'});
                    e.closest('[data-testid="stMain"]').scrollBy(0, -80);
                }""")
                page.screenshot(path=str(tmp_path / f"documentation-{section}-{size}.png"))
                documentation.click()
                _settled(page)
        print(f"Synthetic long-label browser screenshots and dimensions: {tmp_path}")
    except Exception:
        page.screenshot(path=str(tmp_path / "layout-failure.png"), full_page=True)
        (tmp_path / "layout-failure.html").write_text(page.content(), encoding="utf-8")
        print(f"Synthetic layout failure artifacts: {tmp_path}")
        raise
    finally:
        context.close()


def test_decision_navigation(browser: Browser, workspace_url: str, tmp_path: Path) -> None:
    context = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(10000)
    try:
        _load(page, workspace_url, tmp_path, count=6)
        _keyboard_picker(page)
        selector = page.get_by_role("combobox", name="Kandidat i den viste liste", exact=True)
        # Human names are searchable while product IDs remain the review owner.
        selector.click()
        selector.fill("Eksempelpakning 2")
        page.get_by_role("option", name="000002 · Eksempelpakning 2", exact=True).click()
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        expect(
            page.get_by_test_id("stText").filter(has_text=re.compile(r"^000002 · "))
        ).to_be_visible()
        _settled(page)
        note = page.get_by_role("textbox", name="Note", exact=True)
        note.fill("Ugemt kladde til vare 2")
        note.press("Tab")
        expect(page.get_by_text("Ugemte ændringer.", exact=False)).to_be_visible()
        _settled(page)
        # Native anchor navigation changes only the scroll target, preserving the draft owner.
        for section, label, anchor in (
            (".st-key-review_editor", "Til kandidatlisten", "kandidater"),
            (".st-key-candidate_list", "Se den valgte kandidats priser", "priser"),
            (".st-key-price_workspace", "Gem din vurdering", "vurdering"),
            (".st-key-price_workspace", "Beregninger og metode", "metode"),
        ):
            page.locator(section).get_by_role("link", name=label, exact=True).click()
            expect(page).to_have_url(re.compile(rf"#{anchor}$"))
            expect(selector).to_have_value("000002 · Eksempelpakning 2")
            expect(note).to_have_value("Ugemt kladde til vare 2")
        page.get_by_role("button", name="Næste kandidat", exact=True).click()
        expect(selector).to_have_value("000003 · Eksempelpakning 3")
        _settled(page)
        expect(note).to_have_value("")
        page.get_by_role("button", name="Forrige kandidat", exact=True).click()
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        _settled(page)
        expect(note).to_have_value("Ugemt kladde til vare 2")
        page.get_by_role("button", name="Gem og næste kandidat", exact=True).click()
        expect(selector).to_have_value("000003 · Eksempelpakning 3")
        _settled(page)
        expect(page.get_by_role("tab")).to_have_count(0)
        expect(page.get_by_test_id("stPlotlyChart").first).to_be_visible()
        _select(page, "Kandidat i den viste liste", "000002")
        expect(note).to_have_value("Ugemt kladde til vare 2")
        expect(page.get_by_text("Ugemte ændringer.", exact=False)).to_have_count(0)
        # Comparison only toggles traces; the candidate and its draft retain ownership.
        _toggle_alternative(page, "900001")
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        expect(page.get_by_text("Opbevaring H:", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Vurder alternativ 900001")).to_have_count(0)
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[0].meta==='000002' "
            "&& p.data[1].meta==='900001' && p.data[0].line.dash!==p.data[1].line.dash "
            "&& p.data[0].marker.symbol!==p.data[1].marker.symbol}"
        )
        _toggle_alternative(page, "900001", checked=False)
        page.wait_for_function("() => document.querySelector('.js-plotly-plot').data.length===1")
        _toggle_alternative(page, "900001")
        expect(_alternative(page, "900002")).to_be_disabled()
        expect(
            page.get_by_text("Historik mangler; ingen kurve kan vises.", exact=False).first
        ).to_be_visible()
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        expect(page.get_by_role("button", name="Vurder alternativ 900002")).to_have_count(0)
        page.get_by_role("button", name="Vurder alternativ 900004", exact=True).click()
        _settled(page)
        expect(selector).to_have_value("")
        expect(
            page.get_by_test_id("stText").filter(has_text=re.compile(r"^900004 · "))
        ).to_be_visible()
        expect(
            page.get_by_text("Historisk prisniveau kan ikke sammenlignes", exact=False)
        ).to_be_visible()
        expect(note).to_have_value("")
        _select(page, "Kandidat i den viste liste", "000002")
        expect(note).to_have_value("Ugemt kladde til vare 2")
        expect(_alternative(page, "900001")).to_be_checked()
        _toggle_alternative(page, "000001")
        page.locator(".st-key-price_workspace").get_by_text(
            "Sammenlign med substitutionsalternativer", exact=True
        ).evaluate("""e => {
            e.scrollIntoView({block:'center'});
        }""")
        page.screenshot(path=str(tmp_path / "two-selected-alternatives-laptop.png"))
        page.get_by_role("button", name="Vurder alternativ 000001", exact=True).click()
        _settled(page)
        expect(selector).to_have_value("000001 · Eksempelpakning 1")
        expect(note).to_have_value("")
        expect(_alternative(page, "900001")).not_to_be_checked()
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


def test_group_checkbox_limit(browser: Browser, workspace_url: str, tmp_path: Path) -> None:
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    page.set_default_timeout(10000)
    try:
        _load(page, workspace_url, tmp_path, count=9, large_group=True)
        panel = page.locator(".st-key-price_workspace")
        # Exactly one control per alternative, including disabled missing-history rows.
        expect(_alternative(page, "900002")).to_be_disabled()
        expect(panel.get_by_role("checkbox", name=re.compile(r"^\d{6} · "))).to_have_count(12)
        expect(_alternative(page, "000009")).to_be_enabled()
        for pid in ("000002", "000003", "000004", "000005", "000006"):
            _toggle_alternative(page, pid)
        _settled(page)
        expect(_alternative(page, "000007")).to_be_disabled()
        expect(_alternative(page, "000002")).to_be_enabled()
        page.wait_for_function("() => document.querySelector('.js-plotly-plot').data.length===6")
        _toggle_alternative(page, "000002", checked=False)
        _settled(page)
        expect(_alternative(page, "000007")).to_be_enabled()
        _toggle_alternative(page, "000007")
        _settled(page)
        page.wait_for_function(
            "() => {const ids=document.querySelector('.js-plotly-plot').data.map(t=>t.meta);"
            "return ids.length===6 && ids.includes('000007') && !ids.includes('000002')}"
        )
        page.screenshot(path=str(tmp_path / "five-alternatives-laptop.png"), full_page=True)
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
        grid.click(position={"x": 16, "y": 56})
        _keyboard_picker(page)
        selector = page.get_by_role("combobox", name="Kandidat i den viste liste", exact=True)
        expect(selector).to_have_value("000001 · Eksempelpakning 1")
        expect(page.get_by_role("button", name="Clear selection", exact=True)).to_be_visible()
        _settled(page)
        page.get_by_role("button", name="Næste kandidat", exact=True).click()
        expect(selector).to_have_value("000002 · Eksempelpakning 2")
        expect(page.get_by_role("button", name="Clear selection", exact=True)).to_have_count(0)
        grid.click(position={"x": 16, "y": 56})
        expect(selector).to_have_value("000001 · Eksempelpakning 1")
        _settled(page)
        # Native header sorting changes visible rows without changing canonical ranks.
        grid.evaluate("""e => {
            e.scrollIntoView({block:'start'});
            e.closest('[data-testid="stMain"]').scrollBy(0, -80);
        }""")
        grid.click(position={"x": 65, "y": 15})
        ascending = page.get_by_role("menuitem", name="Sort ascending", exact=True)
        if ascending.is_visible():
            ascending.click()
        expect(grid.locator("th").first).to_have_text("↑ Prioritet")
        grid.click(position={"x": 65, "y": 15})
        descending = page.get_by_role("menuitem", name="Sort descending", exact=True)
        if descending.is_visible():
            descending.click()
        expect(grid.locator("th").first).to_have_text("↓ Prioritet")
        grid.click(position={"x": 16, "y": 56})
        expect(selector).to_have_value("000080 · Eksempelpakning 80")
        expect(
            page.get_by_test_id("stText").filter(
                has_text=re.compile(r"^000080 · Eksempelpakning 80$")
            )
        ).to_be_visible()
        _settled(page)
        page.get_by_role("textbox", name="Note", exact=True).fill("=Syntetisk gennemgang")
        page.get_by_text("Føj varen til min eksportliste", exact=True).click()
        _select(page, "Vurdering", "Interessant")
        page.get_by_role("button", name="Gem vurdering", exact=True).click()
        expect(page.get_by_text("Vurderingen er gemt", exact=False)).to_be_visible()
        requested.fill("17")
        requested.press("Enter")
        expect(page.get_by_text("Viser 17 af 17 ønskede kandidater", exact=False)).to_be_visible()
        expect(selector).to_have_value("")
        expect(page.get_by_role("textbox", name="Note", exact=True)).to_have_value(
            "=Syntetisk gennemgang"
        )
        expect(
            page.get_by_text("uden for den aktuelle prioriteringskø", exact=False)
        ).to_be_visible()
        search = page.get_by_role("textbox", name="Søg i kandidatlisten", exact=True)
        search.fill("000080")
        search.press("Enter")
        expect(page.get_by_text("Ingen produkter i denne visning", exact=False)).to_be_visible()
        search.fill("000002")
        search.press("Enter")
        expect(page.get_by_text("1 synlige efter søgning og filtre.", exact=False)).to_be_visible()
        _settled(page)
        _keyboard_picker(page)
        selector.evaluate("e => e.scrollIntoView({block:'center', behavior:'instant'})")
        page.get_by_test_id("stSelectbox").filter(has=selector).get_by_role(
            "button", name="Open", exact=True
        ).click()
        expect(page.get_by_role("option")).to_have_count(1)
        expect(page.get_by_role("option")).to_have_text("000002 · Eksempelpakning 2")
        page.keyboard.press("Escape")
        expect(page.get_by_role("textbox", name="Note", exact=True)).to_have_value(
            "=Syntetisk gennemgang"
        )
        search.fill("")
        search.press("Enter")
        _select(page, "Hvilke kandidater vil du se?", "Alle prioriterbare")
        expect(page.get_by_text("Alle 120 prioriterbare kandidater.", exact=False)).to_be_visible()
        _settled(page)
        page.get_by_text("Seneste 26 perioder", exact=True).click()
        expect(page.get_by_role("radio", name="Seneste 26 perioder", exact=True)).to_be_checked()
        page.get_by_text("Flere grafvalg", exact=True).click()
        page.get_by_text("Vis også priser efter den valgte periode", exact=True).click()
        _select(page, "Kandidat i den viste liste", "000001")
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
        _toggle_alternative(page, "900001")
        expect(page.locator('[data-testid="stPlotlyChart"]').first).to_be_visible()
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[0].x.length===26 "
            "&& p.data[1].meta==='900001'}"
        )
        page.get_by_text("Gruppedetaljer · 7 fundne varer", exact=True).click()
        expect(
            page.get_by_text("Gruppefuldstændighed er ikke verificeret", exact=False).last
        ).to_be_visible()
        expect(
            page.locator(".st-key-price_workspace").get_by_test_id("stDataFrame").first
        ).to_be_visible()
        page.get_by_text("Gruppedetaljer · 7 fundne varer", exact=True).click()
        _select(page, "Hvilke kandidater vil du se?", "Prioriteret liste")
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
        page.wait_for_function(
            "() => {const p=document.querySelector('.js-plotly-plot');"
            "return p && p.data.length===2 && p.data[1].meta==='900001'}"
        )
        _select(page, "Kandidat i den viste liste", "000080")
        expect(note).to_have_value("=Syntetisk gennemgang")
        _select(page, "Kandidat i den viste liste", "000001")
        expect(note).to_have_value("")
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
                ("table", "1. Vælg en kandidat"),
                ("detail", "2. Sammenlign priser"),
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
        _select(page, "Kandidat i den viste liste", "000080")
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
            _select(page, "Kandidat i den viste liste", "000001")
            expect(page.get_by_role("textbox", name="Note", exact=True)).to_have_value("")
            _select(page, "Kandidat i den viste liste", "000080")
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
