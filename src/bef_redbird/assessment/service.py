"""Durable local jobs. Viewing saved files never opens the operating database."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import psutil

from bef_redbird.research.e4_assessment import attach_outcome, bind_report, validate_features
from bef_redbird.research.e4_common import checked, identity, read, utc
from bef_redbird.research.e4_sources import intake_report
from bef_redbird.research.group_features import pin
from bef_redbird.research.prospective import predict, require, timestamp, write_once
from bef_redbird.research.training_contract import atomic_json


def config(path: Path) -> dict[str, Any]:
    value = read(path)
    value["periods"] = read(checked(value["market"]))["periods"]
    return value


def top50(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.loc[frame.selected].sort_values("rank", kind="stable").head(50).copy()


def load(path: Path) -> tuple[dict[str, Any], pd.DataFrame]:
    saved = read(path)
    for reference in saved["files"].values():
        checked(reference)
    return saved, pd.read_parquet(checked(saved["files"]["predictions"]))


def request(
    configuration: Path,
    origin: str,
    upload: bytes | None = None,
    context: dict[str, Any] | None = None,
    *,
    retained_report: bool = False,
) -> Path:
    cfg = config(configuration)
    period = next(p for p in cfg["periods"] if p["origin"] == origin)
    if upload is not None:
        if not context or (context.get("current_period"), context.get("target_period")) != (
            origin,
            period["target"],
        ):
            raise ValueError("Cito-perioden stemmer ikke med den valgte vurdering.")
        if not context.get("period_evidence") or not context.get("reported_receipt"):
            raise ValueError("Bekræft rapportperioden og den faktiske modtagelse.")
        if context.get("receipt_precision", "instant") == "instant":
            received = timestamp(context["reported_receipt"])
            if received > pd.Timestamp.now(tz="UTC"):
                raise ValueError("Modtagelsen ligger i fremtiden.")
        elif context["receipt_precision"] not in {"approximate", "unknown"}:
            raise ValueError("Ugyldig præcision for modtagelsen.")
        if len(upload) > 128 * 1024 * 1024:
            raise ValueError("Cito-filen er for stor.")
        # Previously reviewed bytes cannot be relabelled as a new-period report.
        digest = hashlib.sha256(upload).hexdigest()
        for known in cfg["periods"]:
            if known.get("cito"):
                old = read(checked(known["cito"]))
                if old["original"]["sha256"] == digest and known["origin"] != origin:
                    raise ValueError(
                        "Denne Cito-rapport er tidligere verificeret for en anden periode."
                    )
    root = Path(cfg["root"])
    source_code = Path(__file__).read_bytes()
    code_path = root / "producers" / (hashlib.sha256(source_code).hexdigest() + ".py")
    code_path.parent.mkdir(parents=True, exist_ok=True)
    if not code_path.exists():
        with code_path.open("xb") as stream:
            stream.write(source_code)
    binding = {
        "version": "local-diagnostic-v1",
        "period": period,
        "protocol": pin(Path(cfg["protocol_root"]) / "protocol.json"),
        "producer": pin(code_path),
        "upload_sha256": hashlib.sha256(upload).hexdigest() if upload is not None else None,
        "context": context,
        "retained_report": retained_report,
    }
    key = identity(binding)
    job = root / "jobs" / key
    job.mkdir(parents=True, exist_ok=True)
    if upload is not None:
        destination = job / "cito.bin"
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != binding["upload_sha256"]:
                raise ValueError("Den gemte kilde er ændret.")
        else:
            with destination.open("xb") as stream:
                stream.write(upload)
    write_once(
        job / "request.json", {"binding": binding, "configuration": str(configuration.resolve())}
    )
    return job


def launch(job: Path) -> None:
    if (job / "result.json").exists():
        load(checked(read(job / "result.json")["assessment"]))
        return
    # Exclusive per-job spawn token; distinct jobs additionally share a worker lock.
    try:
        fd = os.open(job / "launch.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    os.close(fd)
    try:
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "bef_redbird.assessment.service", str(job)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        atomic_json(
            job / "spawn.json",
            {
                "pid": process.pid,
                "created": psutil.Process(process.pid).create_time(),
            },
        )
    except Exception:
        (job / "launch.lock").unlink(missing_ok=True)
        raise


def state(job: Path) -> dict[str, Any]:
    if (job / "result.json").exists():
        load(checked(read(job / "result.json")["assessment"]))
        return {"state": "completed", "stage": "Vurderingen er gemt"}
    if not (job / "status.json").exists():
        if (job / "spawn.json").exists():
            spawn = read(job / "spawn.json")
            if not alive(spawn):
                return {"stage": "Startprocessen blev afbrudt. Prøv igen.", "state": "failed"}
        elif (job / "launch.lock").exists() and time.time() - (
            job / "launch.lock"
        ).stat().st_mtime > 15:
            return {"stage": "Startprocessen blev afbrudt. Prøv igen.", "state": "failed"}
        return {"stage": "Venter på lokal proces", "state": "waiting"}
    value = read(job / "status.json")
    if value["state"] == "running" and not alive(value):
        return {**value, "state": "failed", "stage": "Processen blev afbrudt. Prøv igen."}
    return value


def alive(value: dict[str, Any]) -> bool:
    try:
        return bool(psutil.Process(value["pid"]).create_time() == value.get("created"))
    except psutil.Error:
        return False


def retry(job: Path) -> None:
    value = state(job)
    if value["state"] != "failed":
        return
    (job / "launch.lock").unlink(missing_ok=True)
    (job / "status.json").unlink(missing_ok=True)
    (job / "spawn.json").unlink(missing_ok=True)
    launch(job)


def execute(job: Path) -> Path:
    req = read(job / "request.json")
    binding = req["binding"]
    cfg = config(Path(req["configuration"]))
    root = Path(cfg["root"])
    # Foundation's native OS lock releases on process death; no database is opened.
    from bef_redbird.foundation.locking import writer_lock

    started = time.perf_counter()
    timings: dict[str, float] = {}

    def progress(stage: str) -> None:
        atomic_json(
            job / "status.json",
            {
                "state": "running",
                "stage": stage,
                "pid": os.getpid(),
                "created": psutil.Process().create_time(),
                "updated_utc": utc(),
            },
        )

    with writer_lock(root / "assessment-worker"):
        result = job / "result.json"
        if result.exists():
            load(checked(read(result)["assessment"]))
            atomic_json(root / "latest.json", read(result))
            atomic_json(
                job / "status.json", {"state": "completed", "stage": "Gemt vurdering gendannet"}
            )
            return result
        if (job / "assessment.json").exists():
            saved, _ = load(job / "assessment.json")
            if saved["binding"] != binding:
                raise ValueError("Vurderingens identitet er ændret.")
            reference = {"assessment": pin(job / "assessment.json")}
            write_once(result, reference)
            atomic_json(root / "latest.json", reference)
            atomic_json(
                job / "status.json", {"state": "completed", "stage": "Gemt vurdering gendannet"}
            )
            return result
        progress("Kontrollerer modeller og forberedte markedsdata")
        protocol = require(Path(cfg["protocol_root"]))
        if binding["protocol"] != pin(Path(cfg["protocol_root"]) / "protocol.json"):
            raise ValueError("Modelprotokollen er ændret.")
        checked(binding["producer"])
        if pin(Path(__file__))["sha256"] != binding["producer"]["sha256"]:
            raise ValueError("Beregningskoden er ændret efter bestillingen. Start en ny vurdering.")
        period = binding["period"]
        frame = pd.read_parquet(checked(period["features"]))
        origin, target = period["origin"], period["target"]
        ctx = {
            "origin": origin,
            "target": target,
            "calendar_next_start": target,
            "mode": "actual_report_retrospective",
            "decision_cutoff": period.get("decision_cutoff", origin + "T00:00:00+00:00"),
        }
        validate_features(frame, ctx)
        frame["advance_abc"] = None
        frame["advance_state"] = "unavailable"
        frame["advance_source"] = "unavailable"
        report = None
        stamp = time.perf_counter()
        if binding["upload_sha256"]:
            progress("Validerer Cito og rapportens periode")
            source = job / "cito.bin"
            if hashlib.sha256(source.read_bytes()).hexdigest() != binding["upload_sha256"]:
                raise ValueError("Cito-kilden er ændret.")
            report = intake_report(source, root / "originals", root / "reports", binding["context"])
        elif binding["retained_report"]:
            report = checked(period["cito"])
        if report:
            hybrid = bind_report(frame, report, origin, target)
            if not hybrid.advance_abc.isin(["A", "B", "C"]).any():
                raise ValueError("Rapporten indeholder ingen brugbare Cito-koder for markedsdata.")
        timings["cito_validation_seconds"] = time.perf_counter() - stamp
        progress("Beregner vurdering uden Cito")
        stamp = time.perf_counter()
        current = predict(frame, protocol, "current")
        timings["current_inference_seconds"] = time.perf_counter() - stamp
        current.to_parquet(job / "current.parquet", index=False)
        primary = current
        if report:
            progress("Beregner vurdering med Cito og tilbagefald uden Cito")
            stamp = time.perf_counter()
            primary = predict(hybrid, protocol, "hybrid")
            timings["hybrid_inference_seconds"] = time.perf_counter() - stamp
        else:
            timings["hybrid_inference_seconds"] = 0.0
        progress("Gemmer vurdering og tilknytter kendte udfald")
        stamp = time.perf_counter()
        primary.to_parquet(job / "raw-predictions.parquet", index=False)
        # Outcome attachment follows prediction ranking, and cannot replace unknowns.
        bridge = root / "outcome-work" / job.name[:20] / "assessment.json"
        write_once(bridge, {"context": ctx, "predictions": pin(job / "raw-predictions.parquet")})
        official = checked(period["official"]) if period.get("official") else None
        outcome = read(attach_outcome(bridge, official))
        outcomes = pd.read_parquet(checked(outcome["rows"]))
        primary = primary.merge(
            outcomes[["row_key", "status", "next_aip_ore", "next_delta_ore"]],
            on="row_key",
            validate="one_to_one",
        )
        primary["outcome_status"] = primary.status
        primary["actual_next_dkk"] = pd.to_numeric(primary.next_aip_ore) / 100
        primary["actual_delta_dkk"] = pd.to_numeric(primary.next_delta_ore) / 100
        primary.to_parquet(job / "predictions.parquet", index=False)
        chosen = top50(primary)
        timings["publication_seconds"] = time.perf_counter() - stamp
        timings["total_seconds"] = time.perf_counter() - started
        memory = psutil.Process().memory_info()
        saved = {
            "assessment_id": job.name,
            "origin": origin,
            "target": target,
            "mode": "hybrid" if report else "current",
            "completed_utc": utc(),
            "diagnostic": True,
            "binding": binding,
            "report": pin(report) if report else None,
            "files": {
                "predictions": pin(job / "predictions.parquet"),
                "current": pin(job / "current.parquet"),
            },
            "counts": {
                "population": len(primary),
                "eligible": int(primary.eligible.sum()),
                "selected": int(primary.selected.sum()),
                "shown": len(chosen),
                "observed": int(chosen.outcome_status.eq("observed").sum()),
                "unknown": int(chosen.outcome_status.ne("observed").sum()),
                "cito_used": int(primary.information_route.eq("advance").sum()),
            },
            "timings": timings,
            "peak_memory_bytes": getattr(memory, "peak_wset", memory.rss),
        }
        write_once(job / "assessment.json", saved)
        write_once(result, {"assessment": pin(job / "assessment.json")})
        atomic_json(root / "latest.json", {"assessment": pin(job / "assessment.json")})
        atomic_json(
            job / "status.json",
            {
                "state": "completed",
                "stage": "Vurderingen er gemt",
                "pid": os.getpid(),
                "updated_utc": utc(),
            },
        )
        return result


def notes(
    root: Path, assessment_id: str, package: str, value: dict[str, str] | None = None
) -> dict[str, str]:
    if (
        len(assessment_id) != 64
        or any(x not in "0123456789abcdef" for x in assessment_id)
        or not package.isdigit()
        or len(package) != 6
    ):
        raise ValueError("Ugyldig vurderingsidentitet")
    path = root / "notes" / assessment_id / f"{package}.json"
    if value is not None:
        if len(value["note"]) > 4000:
            raise ValueError("Noten er for lang")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, {**value, "updated_utc": utc()})
    return read(path) if path.exists() else {"status": "Ikke gennemgået", "note": ""}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", type=Path)
    job = parser.parse_args().job
    try:
        execute(job)
    except Exception as exc:
        # Keep paths/source rows out of the UI. Error class and private traceback aid operation.
        import traceback

        (job / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        atomic_json(
            job / "status.json",
            {
                "state": "failed",
                "stage": "Vurderingen kunne ikke gennemføres",
                "error": type(exc).__name__,
                "pid": os.getpid(),
                "updated_utc": utc(),
            },
        )
    finally:
        (job / "launch.lock").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
