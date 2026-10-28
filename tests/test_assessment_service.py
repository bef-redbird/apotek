"""Durable local assessment lifecycle against independent synthetic market data."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from openpyxl import Workbook

from bef_redbird.assessment import service
from bef_redbird.research.e4_common import read
from bef_redbird.research.group_features import pin


@pytest.fixture
def market(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, list[str]]:
    # Nested content hashes need a short fixture root on Windows without long paths.
    tmp_path = tmp_path_factory.mktemp("svc")
    root = tmp_path / "a"
    protocol = tmp_path / "protocol"
    protocol.mkdir()
    (protocol / "protocol.json").write_text('{"version":"synthetic"}')
    numbers = [f"{n:06d}" for n in range(1, 62)]
    frame = (
        pd.DataFrame(
            {
                "row_key": [f"origin:{n}" for n in numbers],
                "package_number": numbers,
                "period_start": pd.Timestamp("2026-10-12"),
                "target_period": pd.Timestamp("2026-10-26"),
                "aip_ore": [10000] * 61,
                "eligible": [True] * 61,
                "origin_identity_token": ["continuous"] * 61,
                "origin_creation_date": ["2020-01-01"] * 61,
                "origin_exit_date": ["1900-01-01"] * 61,
            }
        )
        .sample(frac=1, random_state=21)
        .reset_index(drop=True)
    )
    features = tmp_path / "features.parquet"
    frame.to_parquet(features, index=False)
    official_rows = tmp_path / "official.parquet"
    pd.DataFrame(
        {
            "package_number": numbers,
            "aip_ore": [10000 + n * 100 for n in range(1, 62)],
            "price_state": ["observed"] * 61,
            "identity_token": ["continuous"] * 49 + ["changed"] + ["continuous"] * 11,
            "created_date_token": ["20200101"] * 61,
            "exit_date_token": ["19000101"] * 61,
            "dispensing_code": ["B"] * 61,
        }
    ).to_parquet(official_rows, index=False)
    official = tmp_path / "official.json"
    official.write_text(json.dumps({"period": "2026-10-26", "rows": pin(official_rows)}))
    period = {
        "origin": "2026-10-12",
        "target": "2026-10-26",
        "features": pin(features),
        "official": pin(official),
    }
    published = tmp_path / "market.json"
    published.write_text(json.dumps({"periods": [period]}))
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"root": str(root), "protocol_root": str(protocol), "market": pin(published)})
    )
    calls: list[str] = []
    retained = {"retained_protocol": True}
    monkeypatch.setattr(service, "require", lambda _: retained)

    def predict(data: pd.DataFrame, protocol: dict, mode: str) -> pd.DataFrame:
        assert protocol is retained
        assert not any(name.startswith("next_aip") or name == "status" for name in data)
        if mode == "current":
            assert data.advance_abc.isna().all()
        else:
            assert data.advance_abc.isin(["A", "B", "C"]).any()
        calls.append(mode)
        out = data.copy()
        out["rank"] = out.package_number.astype(int)
        out["selected"] = out["rank"].le(55)
        out["information_route"] = data.advance_abc.map(
            lambda code: "advance" if code in {"A", "B", "C"} else "current-only fallback"
        )
        out["prediction_state"] = "available"
        out["p_rise"] = 0.7
        out["p_fall"] = 0.03
        out["amount_3y_next_price_dkk"] = 112.0
        out["amount_3y_delta_dkk"] = 12.0
        return out

    monkeypatch.setattr(service, "predict", predict)
    return cfg, root, calls


def test_request_validates_report_context_before_retaining_bytes(market: tuple) -> None:
    cfg, root, _ = market
    context = {
        "current_period": "2026-10-12",
        "target_period": "2026-10-26",
        "next_period_start": "2026-10-26",
        "period_evidence": "Synthetic operator confirmation",
        "reported_receipt": "2026-10-17T11:00:00+02:00",
    }
    for invalid in (
        dict(context, target_period="2026-11-09"),
        dict(context, period_evidence=""),
        dict(context, reported_receipt="2099-01-01T11:00:00Z"),
        dict(context, reported_receipt="2026-10-17T11:00:00"),
    ):
        with pytest.raises(ValueError):
            service.request(cfg, "2026-10-12", b"synthetic bytes", invalid)
    assert not root.exists()
    first = service.request(cfg, "2026-10-12", b"synthetic bytes", context)
    assert service.request(cfg, "2026-10-12", b"synthetic bytes", context) == first
    assert (first / "cito.bin").read_bytes() == b"synthetic bytes"
    (first / "cito.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="ændret"):
        service.request(cfg, "2026-10-12", b"synthetic bytes", context)


def test_saved_lifecycle_immutable_ranking_and_durable_notes(market: tuple) -> None:
    cfg, root, calls = market
    job = service.request(cfg, "2026-10-12")
    assert service.request(cfg, "2026-10-12") == job
    result = service.execute(job)
    manifest, frame = service.load(Path(read(result)["assessment"]["path"]))
    chosen = service.top50(frame)
    assert calls == ["current"]
    assert chosen.package_number.tolist() == [f"{n:06d}" for n in range(1, 51)]
    assert manifest["counts"] == {
        "population": 61,
        "eligible": 61,
        "selected": 55,
        "shown": 50,
        "observed": 49,
        "unknown": 1,
        "cito_used": 0,
    }
    assert chosen.loc[chosen.package_number.eq("000001"), "actual_delta_dkk"].item() == 1
    last = chosen.loc[chosen.package_number.eq("000050")].iloc[0]
    assert last.outcome_status == "identity_discontinuity_or_ambiguity"
    assert pd.isna(last.actual_next_dkk) and pd.isna(last.actual_delta_dkk)
    assert service.state(job)["state"] == "completed"
    immutable = {path.name: path.read_bytes() for path in job.glob("*.parquet")}
    saved_bytes = (job / "assessment.json").read_bytes()
    assert service.execute(job) == result
    service.launch(job)
    assert calls == ["current"]
    assert (job / "assessment.json").read_bytes() == saved_bytes
    assert {path.name: path.read_bytes() for path in job.glob("*.parquet")} == immutable
    assert service.notes(root, job.name, "000001")["note"] == ""
    service.notes(root, job.name, "000001", {"status": "Gennemgået", "note": "Kontroller levering"})
    assert service.notes(root, job.name, "000001")["note"] == "Kontroller levering"
    assert service.notes(root, job.name, "000002")["status"] == "Ikke gennemgået"
    with pytest.raises(ValueError):
        service.notes(root, "../escape", "000001")
    with pytest.raises(ValueError, match="lang"):
        service.notes(root, job.name, "000001", {"status": "Gennemgået", "note": "x" * 4001})
    assert (job / "assessment.json").read_bytes() == saved_bytes
    (job / "predictions.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError):
        service.load(job / "assessment.json")


def test_duplicate_launch_and_dead_worker_retry(
    market: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, _ = market
    job = service.request(cfg, "2026-10-12")
    processes = []

    def spawn(*args: object, **kwargs: object) -> SimpleNamespace:
        processes.append((args, kwargs))
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(service.subprocess, "Popen", spawn)
    service.launch(job)
    service.launch(job)
    assert len(processes) == 1
    (job / "status.json").write_text(
        json.dumps({"state": "running", "stage": "calculating", "pid": 99999999})
    )
    monkeypatch.setattr(service.psutil, "pid_exists", lambda _: False)
    assert service.state(job)["state"] == "failed"
    service.retry(job)
    assert len(processes) == 2


def test_interrupted_launch_before_status_can_be_retried(market: tuple) -> None:
    cfg, _, _ = market
    job = service.request(cfg, "2026-10-12")
    lock = job / "launch.lock"
    lock.write_bytes(b"")
    old = time.time() - 600
    os.utime(lock, (old, old))
    assert service.state(job)["state"] == "failed"


def test_publication_crash_recovers_completed_snapshot_without_inference(
    market: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, calls = market
    job = service.request(cfg, "2026-10-12")
    original = service.write_once

    def interrupted(path: Path, value: object) -> None:
        if path.name == "result.json":
            raise OSError("synthetic interruption after assessment publication")
        original(path, value)

    monkeypatch.setattr(service, "write_once", interrupted)
    with pytest.raises(OSError, match="synthetic interruption"):
        service.execute(job)
    original_manifest = (job / "assessment.json").read_bytes()
    files = {path.name: path.read_bytes() for path in job.glob("*.parquet")}
    monkeypatch.setattr(service, "write_once", original)
    result = service.execute(job)
    assert result.exists()
    assert calls == ["current"]
    assert (job / "assessment.json").read_bytes() == original_manifest
    assert {path.name: path.read_bytes() for path in job.glob("*.parquet")} == files
    assert service.state(job)["state"] == "completed"


def test_partial_cito_keeps_same_origin_population_and_current_comparison(
    market: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, root, calls = market
    rows_path = cfg.parent / "cito-rows.parquet"
    pd.DataFrame(
        {
            "package_number": ["000001", "000002"],
            "advance_abc": ["C", "A"],
            "advance_state": ["observed", "invalid"],
            "current_abc": ["B", "B"],
            "current_state": ["observed", "observed"],
        }
    ).to_parquet(rows_path, index=False)
    report_path = cfg.parent / "cito.json"
    report_path.write_text(
        json.dumps(
            {
                "context": {"current_period": "2026-10-12", "target_period": "2026-10-26"},
                "rows": pin(rows_path),
                "report_id": "synthetic",
            }
        )
    )
    intake_contexts = []

    def intake(source: Path, archive: Path, output: Path, context: dict) -> Path:
        assert source.read_bytes() == b"synthetic Cito"
        intake_contexts.append(context)
        return report_path

    monkeypatch.setattr(service, "intake_report", intake)
    context = {
        "current_period": "2026-10-12",
        "target_period": "2026-10-26",
        "next_period_start": "2026-10-26",
        "period_evidence": "Synthetic pharmacist confirmation",
        "reported_receipt": "2026-10-17T11:00:00+02:00",
    }
    job = service.request(cfg, "2026-10-12", b"synthetic Cito", context)
    service.execute(job)
    saved, frame = service.load(job / "assessment.json")
    current = pd.read_parquet(service.checked(saved["files"]["current"]))
    assert calls == ["current", "hybrid"] and intake_contexts == [context]
    assert saved["mode"] == "hybrid" and saved["counts"]["cito_used"] == 1
    assert len(frame) == len(current) == 61
    assert set(frame.row_key) == set(current.row_key)
    assert current.advance_abc.isna().all()
    assert frame.loc[frame.package_number.eq("000001"), "advance_abc"].item() == "C"
    assert pd.isna(frame.loc[frame.package_number.eq("000002"), "advance_abc"].item())
    assert service.top50(frame).row_key.tolist() == service.top50(current).row_key.tolist()
    (job / "current.parquet").write_bytes(b"tampered current comparison")
    with pytest.raises(ValueError):
        service.load(job / "assessment.json")
    assert not (root / "notes").exists()


def test_process_id_reuse_is_not_treated_as_live_worker(
    market: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, _ = market
    job = service.request(cfg, "2026-10-12")
    (job / "status.json").write_text(
        json.dumps({"state": "running", "stage": "calculating", "pid": 123, "created": 10.0})
    )
    monkeypatch.setattr(
        service.psutil, "Process", lambda _: SimpleNamespace(create_time=lambda: 20.0)
    )
    assert service.state(job)["state"] == "failed"


def test_result_receipt_recovers_status_after_final_status_interruption(
    market: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, root, calls = market
    job = service.request(cfg, "2026-10-12")
    original = service.atomic_json

    def interrupted(path: Path, value: dict) -> None:
        if path == root / "latest.json":
            raise OSError("synthetic interruption before final status")
        original(path, value)

    monkeypatch.setattr(service, "atomic_json", interrupted)
    with pytest.raises(OSError, match="before final status"):
        service.execute(job)
    assert (job / "result.json").exists()
    monkeypatch.setattr(service, "atomic_json", original)
    service.execute(job)
    assert calls == ["current"]
    assert service.state(job)["state"] == "completed"


def test_wrong_feature_period_and_future_columns_never_reach_models(market: tuple) -> None:
    cfg, _, calls = market
    configuration = read(cfg)
    published_path = Path(configuration["market"]["path"])
    published = read(published_path)
    features = Path(published["periods"][0]["features"]["path"])
    original = pd.read_parquet(features)
    for changed in (
        original.assign(period_start=pd.Timestamp("2026-09-28")),
        original.assign(next_aip_ore=999999),
    ):
        changed.to_parquet(features, index=False)
        published["periods"][0]["features"] = pin(features)
        published_path.write_text(json.dumps(published))
        configuration["market"] = pin(published_path)
        cfg.write_text(json.dumps(configuration))
        job = service.request(cfg, "2026-10-12")
        with pytest.raises(ValueError):
            service.execute(job)
        assert not (job / "assessment.json").exists()
    assert calls == []


def test_known_cito_bytes_cannot_be_relabelled_as_new_period(market: tuple) -> None:
    cfg, root, _ = market
    source = cfg.parent / "known-cito.bin"
    source.write_bytes(b"independently retained Cito bytes")
    report = cfg.parent / "known-cito.json"
    report.write_text(json.dumps({"original": pin(source)}))
    configuration = read(cfg)
    published_path = Path(configuration["market"]["path"])
    published = read(published_path)
    published["periods"][0]["cito"] = pin(report)
    published["periods"].append(
        dict(published["periods"][0], origin="2026-10-26", target="2026-11-09", cito=None)
    )
    published_path.write_text(json.dumps(published))
    configuration["market"] = pin(published_path)
    cfg.write_text(json.dumps(configuration))
    context = {
        "current_period": "2026-10-26",
        "target_period": "2026-11-09",
        "next_period_start": "2026-11-09",
        "reported_receipt": "2026-10-17T11:00:00+02:00",
        "period_evidence": "A new label cannot override known byte evidence",
    }
    with pytest.raises(ValueError, match="anden periode"):
        service.request(cfg, "2026-10-26", source.read_bytes(), context)
    assert not root.exists()


def test_worker_cannot_execute_changed_code_under_an_older_producer_identity(
    market: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, calls = market
    job = service.request(cfg, "2026-10-12")
    changed = cfg.parent / "changed-service.py"
    changed.write_text("# Different code after the job was requested\n", encoding="utf-8")
    monkeypatch.setattr(service, "__file__", str(changed))
    with pytest.raises(ValueError):
        service.execute(job)
    assert calls == []
    assert not (job / "assessment.json").exists()


def test_uploaded_cito_period_is_retained_without_market_index_registration(market: tuple) -> None:
    cfg, root, calls = market
    configuration = read(cfg)
    published_path = Path(configuration["market"]["path"])
    published = read(published_path)
    later_features = cfg.parent / "later.parquet"
    pd.read_parquet(published["periods"][0]["features"]["path"]).assign(
        period_start=pd.Timestamp("2026-10-26"), target_period=pd.Timestamp("2026-11-09")
    ).to_parquet(later_features, index=False)
    published["periods"].append(
        {
            "origin": "2026-10-26",
            "target": "2026-11-09",
            "features": pin(later_features),
        }
    )
    published_path.write_text(json.dumps(published))
    configuration["market"] = pin(published_path)
    cfg.write_text(json.dumps(configuration))
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Table"
    sheet.append(["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"])
    sheet.append(["000001", "N", 100, "B", "C", "1"])
    stream = io.BytesIO()
    book.save(stream)
    upload = stream.getvalue()
    context = {
        "current_period": "2026-10-12",
        "target_period": "2026-10-26",
        "next_period_start": "2026-10-26",
        "period_evidence": "Synthetic confirmed period",
        "reported_receipt": "2026-10-17T11:00:00+02:00",
    }
    later = dict(
        context,
        current_period="2026-10-26",
        target_period="2026-11-09",
        next_period_start="2026-11-09",
    )
    first = service.request(cfg, "2026-10-12", upload, context)
    queued = service.request(cfg, "2026-10-26", upload, later)
    service.execute(first)
    assert calls == ["current", "hybrid"]
    assert len(list((root / "reports").glob("*/report.json"))) == 1
    assert all(not period.get("cito") for period in read(published_path)["periods"])
    original = (first / "assessment.json").read_bytes()
    with pytest.raises(ValueError, match="anden periode"):
        service.request(cfg, "2026-10-26", upload, later)
    with pytest.raises(ValueError, match="anden periode"):
        service.execute(queued)
    assert calls == ["current", "hybrid"]
    assert not (queued / "assessment.json").exists()
    assert (first / "assessment.json").read_bytes() == original
    assert service.request(cfg, "2026-10-12", upload, context) == first
    revised = service.request(
        cfg, "2026-10-12", upload, dict(context, reported_receipt="2026-10-17T11:01:00+02:00")
    )
    assert revised != first
