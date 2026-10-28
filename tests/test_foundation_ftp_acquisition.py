"""Independent synthetic transport, retained originals, and Windows task boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as XML

import pytest

from bef_redbird.foundation import ftp_acquisition as ftp
from bef_redbird.foundation.records import ArchiveError


def test_checkpoint_retries_preserve_old_value_until_atomic_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    ftp.write_json(checkpoint, {"accepted": 1})
    replace_file = Path.replace
    attempts = []
    delays = []

    def transient(source: Path, target: Path) -> Path:
        attempts.append(source)
        assert json.loads(checkpoint.read_text("utf-8")) == {"accepted": 1}
        assert json.loads(source.read_text("utf-8")) == {"accepted": 2}
        if len(attempts) < 3:
            raise PermissionError("temporary sharing conflict")
        return replace_file(source, target)

    monkeypatch.setattr(Path, "replace", transient)
    monkeypatch.setattr(ftp.time, "sleep", delays.append)
    ftp.write_json(checkpoint, {"accepted": 2})
    assert json.loads(checkpoint.read_text("utf-8")) == {"accepted": 2}
    assert len(set(attempts)) == 1
    assert delays == [0.05, 0.1]
    assert list(tmp_path.glob("*.tmp")) == []


def test_checkpoint_permanent_denial_is_bounded_and_preserves_accepted_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    ftp.write_json(checkpoint, {"accepted": 1})
    attempts = []
    delays = []

    def denied(source: Path, target: Path) -> Path:
        attempts.append(source)
        raise PermissionError("persistent denial")

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(ftp.time, "sleep", delays.append)
    with pytest.raises(PermissionError, match="persistent denial"):
        ftp.write_json(checkpoint, {"accepted": 2})
    assert json.loads(checkpoint.read_text("utf-8")) == {"accepted": 1}
    assert len(attempts) == 5
    assert delays == [0.05, 0.1, 0.2, 0.4]
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows file sharing")
def test_checkpoint_survives_native_windows_status_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    ftp.write_json(checkpoint, {"accepted": 1})
    delays = []
    with checkpoint.open("rb") as reader:

        def release_reader(delay: float) -> None:
            delays.append(delay)
            assert json.loads(checkpoint.read_text("utf-8")) == {"accepted": 1}
            reader.close()

        monkeypatch.setattr(ftp.time, "sleep", release_reader)
        ftp.write_json(checkpoint, {"accepted": 2})
    assert delays == [0.05]
    assert json.loads(checkpoint.read_text("utf-8")) == {"accepted": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def configuration(tmp_path: Path) -> ftp.AcquisitionConfig:
    return ftp.AcquisitionConfig(
        Path.cwd(),
        tmp_path / "originals",
        tmp_path / "ops",
        tmp_path / "ops/inventory.json",
        max_attempts=2,
        retry_seconds=0,
    )


def inventory(config: ftp.AcquisitionConfig, *, size: int = 3, modification: str = "old") -> None:
    ftp.write_json(
        config.inventory_path,
        ftp.catalogue(
            [
                {
                    "directory": "/LMS",
                    "entries": [
                        {
                            "filename": "history.zip",
                            "type": "-",
                            "size": str(size),
                            "modification": modification,
                        }
                    ],
                    "evidence": "synthetic-native.xml",
                }
            ],
            "2026-10-22T12:00:00+00:00",
        ),
    )


class Transport:
    def __init__(self, config: ftp.AcquisitionConfig, content: bytes = b"old") -> None:
        self.config, self.content = config, content
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(request)
        root = self.config.operation_root / request["name"]
        root.mkdir(parents=True)
        session = XML.Element("session", xmlns=ftp.NS["w"], start="2026-10-22T12:00:00Z")
        for op in request["operations"]:
            if op["kind"] == "get":
                path = Path(op["local"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.content)
                download = XML.SubElement(session, "download")
                for key, value in {
                    "filename": op["remote"],
                    "destination": str(path),
                    "size": str(len(self.content)),
                }.items():
                    XML.SubElement(download, key, value=value)
                XML.SubElement(download, "result", success="true")
            else:
                listing = XML.SubElement(session, "ls")
                XML.SubElement(listing, "destination", value=op["remote"])
                entries = XML.SubElement(listing, "files")
                if op["remote"] == "/LMS":
                    child = XML.SubElement(entries, "file")
                    XML.SubElement(child, "filename", value="old")
                    XML.SubElement(child, "type", value="D")
                XML.SubElement(listing, "result", success="true")
        XML.ElementTree(session).write(root / "operations.xml", encoding="utf-8")
        result = {
            "exit_code": 0,
            "started_utc": "2026-10-22T12:00:00+00:00",
            "completed_utc": "2026-10-22T12:01:00+00:00",
            "operations": request["operations"],
        }
        ftp.write_json(root / "receipt.json", result)
        return {**result, "receipt_path": str(root / "receipt.json")}


def test_discovery_resume_adopts_successful_listings_and_cutoff(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    transport = Transport(config)
    result = ftp.discover(config, runner=transport)
    assert result["complete"] and len(transport.calls) == 2
    assert result["listings"][0]["observed_at"] == "2026-10-22T12:00:00Z"
    cutoff = result["inventory_cutoff_utc"]
    ftp.discover(config, runner=transport)
    assert len(transport.calls) == 2
    other = configuration(tmp_path / "other")
    adopted = ftp.discover(other, adopt_inventory=config.inventory_path, runner=transport)
    assert len(transport.calls) == 2
    assert adopted["inventory_cutoff_utc"] == cutoff
    assert adopted["adoptions"][0]["sha256"]


def test_partial_inventory_is_checkpointed_and_resume_skips_success(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    transport = Transport(config)

    def fail_second(request: dict[str, Any]) -> dict[str, Any]:
        if request["operations"][0]["remote"] != "/LMS":
            raise OSError("synthetic failure")
        return transport(request)

    with pytest.raises(RuntimeError, match="bounded retries"):
        ftp.discover(config, runner=fail_second)
    assert ftp.load_inventory(config.inventory_path)["pending"] == ["/LMS/old"]
    ftp.discover(config, runner=transport)
    assert [c["operations"][0]["remote"] for c in transport.calls] == ["/LMS", "/LMS/old"]


def directory_tree_transport(config: ftp.AcquisitionConfig, calls: list[list[str]]) -> ftp.Runner:
    def run(request: dict[str, Any]) -> dict[str, Any]:
        directories = [op["remote"] for op in request["operations"]]
        calls.append(directories)
        root = config.operation_root / request["name"]
        root.mkdir(parents=True)
        session = XML.Element("session", xmlns=ftp.NS["w"], start="2026-10-22T12:00:00Z")
        for directory in directories:
            listing = XML.SubElement(session, "ls")
            XML.SubElement(listing, "destination", value=directory)
            entries = XML.SubElement(listing, "files")
            if directory == "/LMS":
                for name in ["a", "b", "Foreløbig"]:
                    member = XML.SubElement(entries, "file")
                    XML.SubElement(member, "filename", value=name)
                    XML.SubElement(member, "type", value="D")
            XML.SubElement(listing, "result", success="true")
        XML.ElementTree(session).write(root / "operations.xml", encoding="utf-8")
        return {"exit_code": 0, "receipt_path": str(root / "receipt.json")}

    return run


def test_refresh_batches_directories_and_preserves_byte_exact_prior_cutoff(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    inventory(config)
    previous = config.inventory_path.read_bytes()
    calls: list[list[str]] = []
    result = ftp.discover(config, refresh=True, runner=directory_tree_transport(config, calls))
    assert calls == [["/LMS"], ["/LMS/Foreløbig", "/LMS/a", "/LMS/b"]]
    assert result["complete"] and len(result["listings"]) == 4
    snapshots = list((config.operation_root / "inventory-history").glob("*.json"))
    assert len(snapshots) == 1 and snapshots[0].read_bytes() == previous
    assert (
        json.loads(snapshots[0].read_bytes())["inventory_cutoff_utc"] == "2026-10-22T12:00:00+00:00"
    )


def test_cancelled_refresh_resumes_checkpoint_without_relisting_root(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    calls: list[list[str]] = []
    runner = directory_tree_transport(config, calls)
    result = ftp.discover(config, refresh=True, runner=runner, cancelled=lambda: bool(calls))
    assert result["cancelled"] and not result["complete"]
    resumed = ftp.discover(config, refresh=True, runner=runner)
    assert resumed["complete"]
    assert calls == [["/LMS"], ["/LMS/Foreløbig", "/LMS/a", "/LMS/b"]]


def test_retries_idempotence_same_name_revisions_and_unchanged_cutoff(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    inventory(config)
    original_inventory = config.inventory_path.read_bytes()
    transport = Transport(config)
    first = ftp.acquire(config, ["/LMS/history.zip"], runner=transport)
    ftp.acquire(config, ["/LMS/history.zip"], runner=transport)
    assert len(transport.calls) == 1
    assert config.inventory_path.read_bytes() == original_inventory
    prior = first["files"]["/LMS/history.zip"]["versions"][0]
    assert (config.archive_root / prior["archive_relative_path"]).read_bytes() == b"old"
    transport.content = b"new"
    changed = ftp.acquire(config, ["/LMS/history.zip"], runner=transport, recheck=True)
    entry = changed["files"]["/LMS/history.zip"]
    assert len(entry["versions"]) == 2
    assert all(r["publication_utc"] is None for r in entry["receipts"])
    assert all(r["server_modification"] == "old" for r in entry["receipts"])
    assert entry["versions"][0]["sha256"] != entry["versions"][1]["sha256"]
    repeated = ftp.acquire(config, ["/LMS/history.zip"], runner=transport, recheck=True)
    assert len(repeated["files"]["/LMS/history.zip"]["versions"]) == 2
    assert len(repeated["files"]["/LMS/history.zip"]["receipts"]) == 3
    assert len(list(config.archive_root.rglob(".part"))) == 0


def test_successful_prior_get_adoption_preserves_original_and_deduplicates_receipt(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    inventory(config)
    transport = Transport(config)
    local = tmp_path / "previous.zip"
    receipt = transport(
        {
            "name": "approved-prior",
            "operations": [{"kind": "get", "remote": "/LMS/history.zip", "local": str(local)}],
        }
    )
    manifest = tmp_path / "adopt.json"
    ftp.write_json(
        manifest,
        {
            "version": 1,
            "downloads": [
                {
                    "remote": "/LMS/history.zip",
                    "path": str(local),
                    "receipt_path": receipt["receipt_path"],
                }
            ],
        },
    )
    ftp.adopt_downloads(config, manifest)
    state = ftp.adopt_downloads(config, manifest)
    assert len(state["files"]["/LMS/history.zip"]["receipts"]) == 1
    assert local.read_bytes() == b"old"


def test_partial_or_missing_success_evidence_never_accepted(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    inventory(config)
    with pytest.raises(RuntimeError, match="bounded retries"):
        ftp.acquire(config, ["/LMS/history.zip"], runner=Transport(config, b"part"))
    assert ftp.read_state(config)["files"] == {}
    assert not config.archive_root.exists()
    assert ftp.acquisition_status(config)["statuses"] == {"pending": 1}


def batch_inventory(config: ftp.AcquisitionConfig, count: int) -> list[str]:
    remotes = [f"/LMS/history-{index}.zip" for index in range(count)]
    ftp.write_json(
        config.inventory_path,
        ftp.catalogue(
            [
                {
                    "directory": "/LMS",
                    "entries": [
                        {
                            "filename": remote.split("/")[-1],
                            "type": "-",
                            "size": "3",
                            "modification": "server-time",
                        }
                        for remote in remotes
                    ],
                    "evidence": "synthetic-native.xml",
                }
            ],
            "2026-10-22T12:00:00+00:00",
        ),
    )
    return remotes


def test_bounded_batches_share_connection_and_distinct_receipts(tmp_path: Path) -> None:
    config = replace(configuration(tmp_path), batch_size=2)
    remotes = batch_inventory(config, 5)
    transport = Transport(config)
    state = ftp.acquire(config, remotes, runner=transport)
    assert [len(call["operations"]) for call in transport.calls] == [2, 2, 1]
    assert len(state["files"]) == 5
    receipts = [state["files"][remote]["receipts"][0] for remote in remotes]
    assert receipts[0]["evidence"] == receipts[1]["evidence"]
    assert receipts[0]["receipt_id"] != receipts[1]["receipt_id"]
    ftp.acquire(config, remotes, runner=transport)
    assert len(transport.calls) == 3


def test_failed_batch_retains_success_and_retries_only_failed_outputs(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    remotes = batch_inventory(config, 3)
    transport = Transport(config)
    calls: list[dict[str, Any]] = []

    def partial_then_success(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(request)
        if len(calls) == 1:
            # WinSCP aborts after one successful transfer and a partial second.
            result = transport({**request, "operations": request["operations"][:1]})
            partial = Path(request["operations"][1]["local"])
            partial.write_bytes(b"p")
            result["exit_code"] = 1
            persisted = {k: v for k, v in result.items() if k != "receipt_path"}
            ftp.write_json(Path(result["receipt_path"]), persisted)
            return result
        return transport(request)

    sleeps: list[float] = []
    state = ftp.acquire(config, remotes, runner=partial_then_success, sleeper=sleeps.append)
    assert [[op["remote"] for op in call["operations"]] for call in calls] == [remotes, remotes[1:]]
    assert sleeps == [0]
    assert state["files"][remotes[0]]["receipts"][0]["batch_exit_code"] == 1
    assert all(len(item["versions"]) == 1 for item in state["files"].values())
    assert Path(calls[0]["operations"][1]["local"]).read_bytes() == b"p"
    assert calls[0]["operations"][1]["local"] != calls[1]["operations"][0]["local"]


def test_exhausted_batch_failure_resumes_successful_checkpoint(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    remotes = batch_inventory(config, 2)
    transport = Transport(config)
    calls = []

    def fail_after_first(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(request)
        successful = [op for op in request["operations"] if op["remote"] == remotes[0]]
        result = transport({**request, "operations": successful})
        result["exit_code"] = 1
        return result

    with pytest.raises(RuntimeError, match="successful gets checkpointed"):
        ftp.acquire(config, remotes, runner=fail_after_first)
    assert set(ftp.read_state(config)["files"]) == {remotes[0]}
    resumed = Transport(config)
    ftp.acquire(config, remotes, runner=resumed)
    assert [op["remote"] for op in resumed.calls[0]["operations"]] == remotes[1:]
    assert len(calls) == config.max_attempts


def test_bounded_backoff_and_partial_attempt_is_not_reused(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    transport = Transport(config)
    calls: list[dict[str, Any]] = []
    sleeps = []

    def fail_once(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(request)
        if len(calls) == 1:
            return {"exit_code": 1}
        return transport(request)

    ftp.attempt(
        config, [{"kind": "get", "remote": "/LMS/history.zip"}], fail_once, sleeper=sleeps.append
    )
    assert len(calls) == 2 and sleeps == [0]
    assert calls[0]["operations"][0]["local"] != calls[1]["operations"][0]["local"]


@pytest.mark.parametrize(
    "remote",
    ["/LMSfoo/x", "/LMS/../secret", "/LMS/x*", "/LMS/a\nb", "/LMS/a%20b", "/LMS//a", '/LMS/a"b'],
)
def test_read_only_remote_boundary(remote: str) -> None:
    with pytest.raises(ValueError):
        ftp.remote_path(remote)


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        ("2014-10-17T10:59:00.000Z", "2014-10-17T10:59:00Z", True),
        ("2014-10-17T11:59:00+01:00", "2014-10-17T10:59:00Z", True),
        ("2014-10-17T10:59:00.123Z", "2014-10-17T10:59:00.123000+00:00", True),
        ("2014-10-17T10:59:00Z", "2014-10-17T10:59:01Z", False),
        ("2014-10-17T10:59:00Z", "2014-10-17T10:59:00.000001Z", False),
        ("2014-10-17T10:59:00.0000001Z", "2014-10-17T10:59:00.0000002Z", False),
        ("2014-10-17T10:59:00", "2014-10-17T10:59:00Z", False),
        ("2014-10-17T10:59:00.000", "2014-10-17T10:59:00", False),
        ("not a date", "another bad date", False),
        (None, "2014-10-17T10:59:00Z", False),
        (None, None, True),
        ("old", "old", True),
        ("", "", True),
    ],
)
def test_server_modification_equality_preserves_exact_raw_evidence(
    left: str | None, right: str | None, equal: bool
) -> None:
    assert ftp.same_modification(left, right) is equal
    assert ftp.same_modification(right, left) is equal


def test_serialization_and_changed_metadata_status(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    with ftp.acquisition_lock(config), pytest.raises(RuntimeError, match="active"):
        with ftp.acquisition_lock(config):
            pass
    inventory(config)
    ftp.acquire(config, ["/LMS/history.zip"], runner=Transport(config))
    inventory(config, modification="new timestamp")
    assert ftp.acquisition_status(config)["statuses"] == {"remote_metadata_changed": 1}
    revised = ftp.acquire(config, ["/LMS/history.zip"], runner=Transport(config, b"new"))
    assert len(revised["files"]["/LMS/history.zip"]["versions"]) == 2
    assert ftp.acquisition_status(config)["statuses"] == {"retained": 1}


@pytest.mark.parametrize("batch_size", [0, 51, True])
def test_external_config_rejects_unbounded_batches(tmp_path: Path, batch_size: Any) -> None:
    config = tmp_path / "config.json"
    ftp.write_json(
        config,
        {
            "workspace": str(Path.cwd()),
            "native_root": str(tmp_path / "native"),
            "operation_root": str(tmp_path / "ops"),
            "batch_size": batch_size,
        },
    )
    with pytest.raises(ValueError, match="batch size"):
        ftp.load_config(config)


def test_invalid_config_and_dry_run_no_network(tmp_path: Path, capsys: Any) -> None:
    config = configuration(tmp_path)
    inventory(config)
    path = tmp_path / "config.json"
    ftp.write_json(
        path,
        {
            "workspace": str(Path.cwd()),
            "operation_root": str(config.operation_root),
            "native_root": str(config.archive_root),
        },
    )
    assert ftp.main(["dry-run", "--config", str(path), "--remote", "/LMS/history.zip"]) == 0
    assert json.loads(capsys.readouterr().out)["network_operations"] == 0
    ftp.write_json(path, {"workspace": "relative"})
    assert ftp.main(["status", "--config", str(path)]) == 1
    assert "relative" not in capsys.readouterr().out


def test_corrupt_retained_object_is_not_replaced_or_redownloaded(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    inventory(config)
    transport = Transport(config)
    state = ftp.acquire(config, ["/LMS/history.zip"], runner=transport)
    version = state["files"]["/LMS/history.zip"]["versions"][0]
    (config.archive_root / version["archive_relative_path"]).write_bytes(b"bad")
    with pytest.raises(ArchiveError, match="Corrupt"):
        ftp.acquire(config, ["/LMS/history.zip"], runner=transport)
    assert len(transport.calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows WinSCP request boundary")
@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "delete", "remote": "/LMS/history.zip"},
        {"kind": "list", "remote": "/LMS/../other"},
        {"kind": "get", "remote": "/LMS/history.zip", "local": "C:\\outside.zip"},
    ],
)
def test_windows_helper_rejects_request_before_session_lookup(
    tmp_path: Path, operation: dict[str, Any]
) -> None:
    request = tmp_path / "request.json"
    ftp.write_json(
        request,
        {
            "name": "ftp-" + "a" * 32,
            "operation_root": str(tmp_path / "ops"),
            "operations": [operation],
            "winscp_path": "never-invoked.exe",
        },
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(Path.cwd() / "scripts/Invoke-FtpRead.ps1"),
            "-RequestFile",
            str(request),
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not (tmp_path / "ops").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows scheduling execution boundary")
def test_windows_runner_disabled_xml_and_serialized_manual_execution(tmp_path: Path) -> None:
    # A synthetic module exercises the exact PowerShell -m entry point without FTP or DB.
    workspace = tmp_path / "Foreløbig-workspace"
    (workspace / "src/bef_redbird").mkdir(parents=True)
    package = workspace / "bef_redbird/foundation"
    package.mkdir(parents=True)
    for directory in [workspace / "bef_redbird", package]:
        (directory / "__init__.py").write_text("")
    (package / "ftp_maintenance.py").write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "assert sys.argv[1] in ['update','resume'] and sys.argv[2]=='--config'\n"
        "c=json.loads(Path(sys.argv[3]).read_text())\n"
        "Path(c['operation_root'],'entered').write_text(json.dumps(sys.argv[1:]))\n"
        "time.sleep(c.get('delay',0))\nsys.exit(c.get('exit',0))\n"
    )
    operations = tmp_path / "ops"
    operations.mkdir()
    config = tmp_path / "config.json"
    payload = {
        "workspace": str(workspace),
        "python_path": sys.executable,
        "operation_root": str(operations),
        "delay": 2,
    }
    ftp.write_json(config, payload)
    runner = Path.cwd() / "scripts/Invoke-F5Maintenance.ps1"
    args = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(runner),
        "-ConfigFile",
        str(config),
    ]
    first = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (operations / "entered").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (operations / "entered").exists()
        assert subprocess.run(args, capture_output=True, timeout=10).returncode == 1
        assert first.wait(timeout=10) == 0
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()
    payload.update({"delay": 0, "exit": 7})
    ftp.write_json(config, payload)
    assert subprocess.run(args, capture_output=True, timeout=10).returncode == 7
    payload.update({"exit": 0})
    ftp.write_json(config, payload)
    assert subprocess.run(args + ["-AcquireOnly"], capture_output=True, timeout=10).returncode == 0
    assert json.loads((operations / "entered").read_text()) == [
        "update",
        "--config",
        str(config),
        "--acquire-only",
    ]
    assert (
        subprocess.run(
            args + ["-Command", "resume", "-AcquireOnly"], capture_output=True, timeout=10
        ).returncode
        == 0
    )
    assert json.loads((operations / "entered").read_text())[0] == "resume"
    assert (
        subprocess.run(
            args + ["-Command", "status", "-AcquireOnly"], capture_output=True, timeout=10
        ).returncode
        == 1
    )
    ftp.write_json(config, {"workspace": "relative"})
    assert subprocess.run(args, capture_output=True, timeout=10).returncode == 1
    ftp.write_json(config, payload)
    xml_path = operations / "task.xml"
    prepare = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(Path.cwd() / "scripts/New-F5ScheduledTask.ps1"),
            "-ConfigFile",
            str(config),
            "-Account",
            r"SYNTHETIC\Collector",
            "-LogonType",
            "Password",
            "-OutputFile",
            str(xml_path),
        ],
        capture_output=True,
        timeout=10,
    )
    assert prepare.returncode == 0
    root = XML.parse(xml_path).getroot()
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.findtext("t:Settings/t:Enabled", namespaces=ns) == "false"
    assert root.findtext("t:Triggers/t:CalendarTrigger/t:Enabled", namespaces=ns) == "false"
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns) == "Password"
    assert "-AcquireOnly" in (root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns) or "")
    assert root.findtext("t:Settings/t:StartWhenAvailable", namespaces=ns) == "true"
    assert (
        root.findtext("t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=ns) or ""
    ).endswith("T18:30:00")
    # The native scheduler parser accepts the XML without registering any task.
    validated = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; $service=New-Object -ComObject Schedule.Service; "
            "$service.Connect(); $definition=$service.NewTask(0); "
            "$definition.XmlText=[IO.File]::ReadAllText($env:F5_TEST_XML); "
            "if ($definition.Settings.Enabled) { exit 1 }",
        ],
        env={**os.environ, "F5_TEST_XML": str(xml_path)},
        capture_output=True,
        timeout=10,
    )
    assert validated.returncode == 0, validated.stderr.decode(errors="replace")
    assert "-m bef_redbird" not in (
        root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns) or ""
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows scheduling definition boundary")
@pytest.mark.parametrize(
    ("mode", "require_ac"),
    [("DownloadOnly", False), ("DownloadOnly", True), ("FullMaintenance", False)],
)
def test_windows_scheduler_explicit_interactive_mode_and_future_boundary(
    tmp_path: Path, mode: str, require_ac: bool
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src/bef_redbird").mkdir(parents=True)
    operations = tmp_path / "ops"
    config = tmp_path / "config.json"
    ftp.write_json(config, {"workspace": str(workspace), "operation_root": str(operations)})
    xml_path = operations / "task.xml"
    args = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(Path.cwd() / "scripts/New-F5ScheduledTask.ps1"),
        "-ConfigFile",
        str(config),
        "-Account",
        r"SYNTHETIC\Interactive",
        "-OutputFile",
        str(xml_path),
        "-LogonType",
        "InteractiveToken",
        "-Mode",
        mode,
        "-StartAt",
        "2099-01-02T06:30:00",
    ]
    if mode == "FullMaintenance":
        args += ["-NoCatchUp"]
    if require_ac:
        args += ["-RequireAcPower"]
    prepared = subprocess.run(args, capture_output=True, timeout=10)
    assert prepared.returncode == 0, prepared.stderr.decode(errors="replace")
    root = XML.parse(xml_path).getroot()
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert (
        root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns) == "InteractiveToken"
    )
    assert (
        root.findtext("t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=ns)
        == "2099-01-02T06:30:00"
    )
    assert root.findtext("t:Settings/t:Enabled", namespaces=ns) == "false"
    assert root.findtext("t:Triggers/t:CalendarTrigger/t:Enabled", namespaces=ns) == "false"
    assert (
        "-AcquireOnly" in (root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns) or "")
    ) == (mode == "DownloadOnly")
    assert root.findtext("t:Settings/t:StartWhenAvailable", namespaces=ns) == (
        "false" if mode == "FullMaintenance" else "true"
    )
    ac_power = require_ac or mode == "FullMaintenance"
    for setting in ["DisallowStartIfOnBatteries", "StopIfGoingOnBatteries"]:
        assert root.findtext(f"t:Settings/t:{setting}", namespaces=ns) == str(ac_power).lower()
    validated = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; $service=New-Object -ComObject Schedule.Service; "
            "$service.Connect(); $definition=$service.NewTask(0); "
            "$definition.XmlText=[IO.File]::ReadAllText($env:F5_TEST_XML); "
            "if ($definition.Settings.Enabled -or "
            "($definition.Principal.LogonType -ne 3) -or "
            "($definition.Settings.DisallowStartIfOnBatteries -ne "
            "[bool]::Parse($env:F5_TEST_REQUIRE_AC)) -or "
            "($definition.Settings.StopIfGoingOnBatteries -ne "
            "[bool]::Parse($env:F5_TEST_REQUIRE_AC))) { exit 1 }",
        ],
        env={
            **os.environ,
            "F5_TEST_XML": str(xml_path),
            "F5_TEST_REQUIRE_AC": str(ac_power).lower(),
        },
        capture_output=True,
        timeout=10,
    )
    assert validated.returncode == 0, validated.stderr.decode(errors="replace")
    original = xml_path.read_bytes()
    assert subprocess.run(args, capture_output=True, timeout=10).returncode != 0
    assert xml_path.read_bytes() == original
    invalid = args.copy()
    invalid[invalid.index(str(xml_path))] = str(operations / "past.xml")
    invalid[invalid.index("2099-01-02T06:30:00")] = "2020-01-01T06:30:00"
    assert subprocess.run(invalid, capture_output=True, timeout=10).returncode != 0
    assert not (operations / "past.xml").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5 UTF8 JSON boundary")
def test_danish_remote_names_survive_helper_json_decode(tmp_path: Path) -> None:
    # Execute the helper's JSON input boundary without touching its private saved site.
    helper = (Path.cwd() / "scripts/Invoke-FtpRead.ps1").read_text(encoding="utf-8")
    decode = next(line for line in helper.splitlines() if line.startswith("$request ="))
    script = tmp_path / "decode.ps1"
    script.write_text(
        "param($RequestFile,$OutputFile)\n"
        + decode
        + "\n[IO.File]::WriteAllText($OutputFile, $request.operations[0].remote, "
        "[Text.UTF8Encoding]::new($false))",
        encoding="ascii",
    )
    request, output = tmp_path / "request.json", tmp_path / "decoded.txt"
    remote = "/LMS/Foreløbig/æøå.txt"
    ftp.write_json(request, {"operations": [{"kind": "list", "remote": remote}]})
    assert ftp.remote_path(remote) == remote
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(script),
            "-RequestFile",
            str(request),
            "-OutputFile",
            str(output),
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert output.read_text(encoding="utf-8") == remote
