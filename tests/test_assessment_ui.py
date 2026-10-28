"""Synthetic interaction coverage of the saved pharmacist workspace."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pymupdf
import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.assessment import service
from bef_redbird.assessment.ui import preferred
from bef_redbird.research.group_features import pin


def save(path: Path, value: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return pin(path)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    periods = []
    for letter, origin, target, mode, observed in (
        ("a", "2026-09-28", "2026-10-12", "current", True),
        ("b", "2026-10-12", "2026-10-26", "hybrid", True),
        ("c", "2026-10-26", "2026-11-09", "current", False),
    ):
        key = letter * 64
        job = tmp_path / "jobs" / key
        job.mkdir(parents=True)
        frame = pd.DataFrame(
            {
                "row_key": ["first", "second", "outside"],
                "package_number": ["001234", "005678", "999999"],
                "period_start": pd.Timestamp(origin),
                "aip_ore": [10000, 20000, 50000],
                "eligible": [True] * 3,
                "selected": [True, True, False],
                "rank": [1, 2, 3],
                "p_rise": [0.70, 0.50, 0.20],
                "p_fall": [0.03, 0.04, 0.20],
                "p_unchanged": [0.27, 0.46, 0.60],
                "information_route": ["advance" if mode == "hybrid" else "current"] * 3,
                "peer_count": [1, 1, 0],
                "input_quality": ["synthetic verified"] * 3,
                "fallback_reason": ["none"] * 3,
                "origin_group": ["G", "G", "OTHER"],
                "pack_size_token": ["20", "20", "40"],
                "pack_unit": ["stk"] * 3,
                "abc": ["A", "B", "C"],
                "peer_feature_state": ["complete_equal_size_peers"] * 3,
                "outcome_status": ["observed", "observed", "observed"]
                if observed
                else ["pending_target_observation"] * 3,
                "actual_next_dkk": [125.0, 190.0, 1000.0] if observed else [None] * 3,
                "actual_delta_dkk": [25.0, -10.0, 500.0] if observed else [None] * 3,
            }
        )
        for history, delta in (("3y", 10.0), ("6y", 12.0), ("full", 8.0)):
            frame[f"amount_{history}_delta_dkk"] = delta
            frame[f"amount_{history}_next_price_dkk"] = frame.aip_ore / 100 + delta
        frame.to_parquet(job / "predictions.parquet", index=False)
        current = frame.copy()
        current["p_rise"] = [0.40, 0.30, 0.10]
        current["p_fall"] = [0.10, 0.15, 0.25]
        current.to_parquet(job / "current.parquet", index=False)
        history = tmp_path / f"history-{letter}.parquet"
        pd.DataFrame(
            {
                "package_number": ["001234", "001234", "005678"],
                "period_start": [
                    pd.Timestamp(date.fromisoformat(origin) - timedelta(days=14)),
                    pd.Timestamp(origin),
                    pd.Timestamp(origin),
                ],
                "aip_ore": [9500, 10000, 20000],
            }
        ).to_parquet(history, index=False)
        period = {
            "origin": origin,
            "target": target,
            "history": pin(history),
            "features": pin(job / "current.parquet"),
        }
        periods.append(period)
        assessment = save(
            job / "assessment.json",
            {
                "assessment_id": key,
                "origin": origin,
                "target": target,
                "mode": mode,
                "completed_utc": origin + "T12:00:00Z",
                "diagnostic": True,
                "binding": {"period": period},
                "counts": {
                    "eligible": 3,
                    "population": 3,
                    "selected": 2,
                    "shown": 2,
                    "observed": 2 if observed else 0,
                    "unknown": 0 if observed else 2,
                    "cito_used": 2 if mode == "hybrid" else 0,
                },
                "files": {
                    "predictions": pin(job / "predictions.parquet"),
                    "current": pin(job / "current.parquet"),
                },
            },
        )
        save(job / "result.json", {"assessment": assessment})
    market = save(tmp_path / "market.json", {"periods": periods})
    config = tmp_path / "config.json"
    save(config, {"root": str(tmp_path), "market": market})

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Viewing saved results must not launch inference")

    for name in ("require", "predict", "execute", "launch", "request"):
        monkeypatch.setattr(service, name, forbidden)
    return config, tmp_path


def app(path: Path | None) -> AppTest:
    argument = "None" if path is None else f"Path({str(path)!r})"
    return AppTest.from_string(
        "from pathlib import Path\nfrom bef_redbird.assessment.ui import main\n"
        + f"main({argument})"
    ).run(timeout=30)


def test_missing_and_unverifiable_configuration(tmp_path: Path) -> None:
    missing = app(None)
    assert not missing.exception and missing.warning
    assert "konfigurere" in missing.warning[0].value
    invalid = app(tmp_path / "missing.json")
    assert not invalid.exception and invalid.error
    assert not invalid.dataframe


def test_current_history_comparison_and_reconnect_do_not_recompute(workspace: tuple) -> None:
    config, root = workspace
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    view = app(config)
    assert not view.exception and not view.error
    assert [tab.label for tab in view.tabs] == [
        "Næste periode",
        "De seneste to perioder",
        "Sådan beregnes vurderingen",
    ]
    current = view.dataframe[0].value
    assert current.Varenummer.tolist() == ["001234", "005678"]
    assert current["Udfald"].tolist() == ["Ukendt", "Ukendt"]
    assert current["Faktisk ændring · kr."].isna().all()
    assert current["Forventet AIP · kr."].tolist() == [110.0, 210.0]
    assert len(view.get("plotly_chart")) == 3
    plots = [json.loads(item.proto.spec) for item in view.get("plotly_chart")]
    actual = [
        trace for plot in plots for trace in plot["data"] if trace.get("name") == "Faktisk AIP"
    ]
    assert len(actual) == 1 and actual[0]["y"] == [125.0]
    assert actual[0]["marker"]["symbol"] == "triangle-up"
    scatter = next(
        plot
        for plot in plots
        if plot["data"][0]["type"] == "scatter"
        and plot["layout"].get("xaxis", {}).get("title", {}).get("text")
        == "Forventet ændring · kr."
    )
    assert scatter["layout"]["xaxis"]["range"] == [0, 30]
    assert scatter["layout"]["yaxis"]["range"] == [-20, 45]
    assert view.multiselect[0].value == ["005678"]
    assert view.multiselect[0].options == ["005678"]
    peer_traces = [
        trace
        for plot in plots
        for trace in plot["data"]
        if trace.get("name") == "005678 · faktisk historik"
    ]
    assert len(peer_traces) == 2
    assert all(trace["connectgaps"] is False for trace in peer_traces)
    assert all(trace.get("marker", {}).get("symbol") != "triangle-up" for trace in peer_traces)
    assert peer_traces[1]["x"][-1].startswith("2026-10-12")
    assert view.button(key="previous-" + "c" * 64 + "-analysis").disabled
    view.button(key="next-" + "c" * 64 + "-analysis").click().run()
    assert view.selectbox(key="package-" + "c" * 64 + "-analysis").value == "005678"
    assert view.button(key="next-" + "c" * 64 + "-analysis").disabled
    view.button(key="previous-" + "c" * 64 + "-analysis").click().run()
    assert view.selectbox(key="package-" + "c" * 64 + "-analysis").value == "001234"
    view.radio[0].set_value("2026-10-12").run()
    compare = next(item for item in view.checkbox if item.label == "Sammenlign uden Cito")
    compare.check().run()
    assert not view.exception
    comparison = next(item.value for item in view.dataframe if "Rang med Cito" in item.value)
    assert comparison.Varenummer.tolist() == ["001234", "005678"]
    delta = comparison["Ændring i stigningssandsynlighed · procentpoint"]
    assert delta.tolist() == pytest.approx([30.0, 20.0])
    view.text_input(key="find-" + "c" * 64 + "-analysis").set_value("005").run()
    assert view.dataframe[0].value.Varenummer.tolist() == ["005678"]
    # Historical mode also appears in the analysis tab: distinct widget ownership.
    next(item for item in view.selectbox if item.label == "Vurderingsperiode").set_value(
        "2026-10-12"
    ).run()
    assert not view.exception and not view.error
    assert len([item for item in view.checkbox if item.label == "Sammenlign uden Cito"]) == 2
    assert before == {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_durable_notes_and_explicit_pdf_without_inference(workspace: tuple) -> None:
    config, root = workspace
    view = app(config)
    assert not (root / "exports").exists()
    next(item for item in view.selectbox if item.label == "Gennemgang").set_value("Til opfølgning")
    view.text_area[0].set_value("Spørg om levering")
    next(item for item in view.button if item.label == "Gem note").click().run()
    assert not view.exception
    stored = service.notes(root, "c" * 64, "001234")
    assert stored["status"] == "Til opfølgning" and stored["note"] == "Spørg om levering"
    fresh = app(config)
    assert fresh.text_area[0].value == "Spørg om levering"
    next(item for item in fresh.button if item.label == "Opret PDF-rapport").click().run()
    assert not fresh.exception
    pdfs = list((root / "exports").glob("*/report.pdf"))
    assert len(pdfs) == 1
    original = pdfs[0].read_bytes()
    with pymupdf.open(pdfs[0]) as document:
        assert len(document) == 6
        assert sum("Skøn og udfald" in page.get_text() for page in document) == 2
    next(item for item in fresh.button if item.label == "Opret PDF-rapport").click().run()
    assert not fresh.exception and pdfs[0].read_bytes() == original
    assert len(fresh.get("download_button")) == 1
    assert len(list((root / "exports").glob("*/report.pdf"))) == 1


def test_submit_is_only_launch_boundary(workspace: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    config, root = workspace
    requested = []
    launched = []
    job = root / "queued-job"

    def request(*args: object) -> Path:
        requested.append(args)
        return job

    monkeypatch.setattr(service, "request", request)
    monkeypatch.setattr(service, "launch", lambda path: launched.append(path))
    view = app(config)
    assert requested == launched == []
    next(item for item in view.button if item.label == "Analysér").click().run()
    assert not view.exception and not view.error
    assert requested == [(config, "2026-10-26", None, None)]
    assert launched == [job]
    view.run()
    assert len(requested) == len(launched) == 1


def test_changed_saved_comparison_is_hidden_without_inference(workspace: tuple) -> None:
    config, root = workspace
    (root / "jobs" / ("b" * 64) / "current.parquet").write_bytes(b"changed")
    view = app(config)
    view.radio[0].set_value("2026-10-12").run()
    assert not view.exception
    assert any("kunne ikke verificeres" in item.value for item in view.warning)
    assert not any(item.label == "Sammenlign uden Cito" for item in view.checkbox)
    assert any("historiske vurdering er endnu ikke" in item.value for item in view.info)


def test_primary_prefers_cito_only_for_the_latest_market_snapshot() -> None:
    def item(mode: str, completed: str, snapshot: str) -> tuple[dict, pd.DataFrame]:
        return (
            {
                "mode": mode,
                "completed_utc": completed,
                "binding": {"period": {"features": {"sha256": snapshot}}},
            },
            pd.DataFrame(),
        )

    old_cito = item("hybrid", "2026-10-28T10:00:00Z", "older-market")
    latest_current = item("current", "2026-10-28T12:00:00Z", "latest-market")
    assert preferred([old_cito, latest_current]) is latest_current
    matching_cito = item("hybrid", "2026-10-28T11:00:00Z", "latest-market")
    assert preferred([old_cito, matching_cito, latest_current]) is matching_cito
