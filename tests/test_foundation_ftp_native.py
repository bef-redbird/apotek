"""Native FTP contract without private credentials or external network access."""

from __future__ import annotations

import copy
import ftplib
import json
import os
import ssl
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bef_redbird.foundation import ftp_acquisition as acquisition
from bef_redbird.foundation import ftp_native as native
from tests.test_foundation_ftp_acquisition import Transport, configuration, inventory


def prepared(tmp_path: Path, **changes: Any) -> acquisition.AcquisitionConfig:
    return replace(
        configuration(tmp_path),
        transport=replace(
            native.NativeConfig("source.invalid", 21, "explicit_tls", tmp_path / "credential.bin"),
            **changes,
        ),
    )


class Client:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.content = b"old"
        self.reply = "226 transfer complete"
        self.lines = [
            "type=cdir; .",
            "type=pdir; ..",
            "type=file;size=3;modify=20261022120000; Forel\u00f8big.zip",
        ]
        self.failure: Exception | None = None
        self.on_block = lambda: None
        self.on_line = lambda: None

    def connect(self, host: str, port: int, **kwargs: Any) -> None:
        self.calls.append(("connect", host, port))

    def login(self, username: str, password: str) -> None:
        self.calls.append("login")
        if self.failure is not None:
            raise self.failure

    def prot_p(self) -> None:
        self.calls.append("prot_p")

    def voidcmd(self, command: str) -> str:
        return "200 type set"

    def transfercmd(self, command: str) -> Any:
        self.calls.append(command)
        source = self

        class Data:
            def __init__(self) -> None:
                self.done = False

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *args: Any) -> None:
                source.calls.append("data_closed")

            def settimeout(self, timeout: float) -> None:
                assert 0 < timeout <= 1

            def recv(self, size: int) -> bytes:
                if self.done:
                    return b""
                self.done = True
                if command.startswith("MLSD"):
                    source.on_line()
                    return ("\r\n".join(source.lines) + "\r\n").encode("utf-8")
                source.on_block()
                return source.content

        return Data()

    def voidresp(self) -> str:
        return self.reply

    def close(self) -> None:
        self.calls.append("close")


def runner(config: acquisition.AcquisitionConfig, client: Client, **kwargs: Any) -> Any:
    return native.runner(
        config,
        credential_reader=lambda _: ("synthetic-user", "synthetic-password"),
        client_factory=lambda _: client,
        **kwargs,
    )


def request(config: acquisition.AcquisitionConfig, *, name: str = "a", kind: str = "list") -> Any:
    operation = {"kind": kind, "remote": "/LMS" if kind == "list" else "/LMS/history.zip"}
    if kind == "get":
        operation["local"] = str(config.operation_root / "downloads" / (name + ".part"))
    return {"name": "ftp-" + name * 32, "operations": [operation]}


def test_mlsd_raw_facts_danish_names_and_legacy_utc_format(tmp_path: Path) -> None:
    config, client = prepared(tmp_path), Client()
    result = runner(config, client)(request(config))
    assert result["exit_code"] == 0
    assert client.calls == [
        ("connect", "source.invalid", 21),
        "login",
        "prot_p",
        "MLSD /LMS",
        "data_closed",
        "close",
    ]
    listing = acquisition.read_listings(result)[0]
    entry = listing["entries"][0]
    assert entry["filename"] == "Forel\u00f8big.zip"
    assert entry["modification"] == "2026-10-22T12:00:00Z"
    assert entry["raw_mlsd_facts"]["modify"] == "20261022120000"
    assert result["transport"]["credential_mechanism"] == "windows-dpapi-current-user"
    assert "synthetic-password" not in Path(result["receipt_path"]).read_text()


@pytest.mark.parametrize("security", ["sftp", "implicit_tls", "auto", "", None])
def test_unsupported_protocol_fails_before_credentials(tmp_path: Path, security: Any) -> None:
    with pytest.raises(ValueError, match="supported"):
        native.configuration(
            {
                "version": 1,
                "backend": "python-ftplib",
                "host": "source.invalid",
                "port": 21,
                "security": security,
                "credential_file": str(tmp_path / "blob"),
            }
        )


def test_missing_transport_has_no_client_or_credential_fallback(tmp_path: Path) -> None:
    with pytest.raises(native.NativeFtpError, match="Configure native"):
        native.runner(configuration(tmp_path))


def test_verified_tls_and_no_downgrade_on_certificate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, contexts = Client(), []
    client.failure = ssl.SSLCertVerificationError("sensitive server reply synthetic-password")

    def tls(**kwargs: Any) -> Client:
        contexts.append(kwargs["context"])
        return client

    monkeypatch.setattr(native.ftplib, "FTP_TLS", tls)
    monkeypatch.setattr(native.ftplib, "FTP", lambda **kwargs: pytest.fail("TLS downgraded"))
    config = prepared(tmp_path)
    result = native.runner(config, credential_reader=lambda _: ("u", "p"))(request(config))
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED and contexts[0].check_hostname
    assert result["failure_type"] == "SSLCertVerificationError"
    assert result["exit_code"] == 1
    assert "sensitive server" not in json.dumps(result)
    assert client.calls[-1] == "close"


def test_plain_is_explicit_and_does_not_call_tls(tmp_path: Path) -> None:
    config, client = prepared(tmp_path, security="plain"), Client()
    result = runner(config, client)(request(config))
    assert result["exit_code"] == 0 and "prot_p" not in client.calls
    assert result["transport"]["tls_certificate_verification"] is False


@pytest.mark.parametrize(("phase", "code"), [("login", 530), ("list", 500)])
def test_failure_diagnostics_only_include_phase_and_numeric_reply(
    tmp_path: Path, phase: str, code: int
) -> None:
    config, client = prepared(tmp_path), Client()
    failure = ftplib.error_perm(f"{code} synthetic-private-server-message")
    if phase == "login":
        client.failure = failure
    else:

        def unsupported(command: str) -> Any:
            raise failure

        client.transfercmd = unsupported  # type: ignore[method-assign]
    result = runner(config, client)(request(config))
    assert result["failure_phase"] == phase
    assert result["ftp_response_code"] == code
    assert "synthetic-private-server-message" not in json.dumps(result)


@pytest.mark.parametrize(
    "line",
    [
        "type=file;size=3; ../escape",
        "type=unknown;size=3; x",
        "type=file; x",
        "type=file;size=oops; x",
        "type=file;type=dir;size=3; x",
        "bad-facts x",
    ],
)
def test_invalid_mlsd_fails_without_inventing_inventory(tmp_path: Path, line: str) -> None:
    config, client = prepared(tmp_path), Client()
    client.lines = [line]
    result = runner(config, client)(request(config))
    assert result["exit_code"] == 1
    assert acquisition.read_listings(result) == []
    assert not config.inventory_path.exists()


@pytest.mark.parametrize("kind", ["list", "get"])
def test_active_transfer_cancel_and_deadline_close_client(tmp_path: Path, kind: str) -> None:
    config, client = prepared(tmp_path), Client()

    def cancel() -> None:
        (config.operation_root / "cancel-request.json").write_text("{}")

    client.on_line = cancel
    client.on_block = cancel
    result = runner(config, client)(request(config, kind=kind))
    assert result["cancelled"] is True and result["exit_code"] == 1
    assert client.calls[-1] == "close"
    assert all(not op["success"] for op in result["results"])
    (config.operation_root / "cancel-request.json").unlink()
    ticks = iter([0.0, 0.0, 301.0])
    result = runner(config, Client(), clock=lambda: next(ticks))(
        request(config, name="b", kind=kind)
    )
    assert result["cancelled"] is True


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_trickling_incomplete_mlsd_line_is_bounded(tmp_path: Path, stop: str) -> None:
    config, client, now = prepared(tmp_path), Client(), [0.0]
    received: list[float] = []

    class Trickle:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            client.calls.append("data_closed")

        def settimeout(self, timeout: float) -> None:
            assert 0 < timeout <= 1

        def recv(self, size: int) -> bytes:
            # No completed MLSD line ever reaches the parser.
            now[0] += 180
            received.append(now[0])
            if stop == "cancel":
                (config.operation_root / "cancel-request.json").write_text("{}")
            return b"t"

    client.transfercmd = lambda command: Trickle()  # type: ignore[method-assign]
    result = runner(config, client, clock=lambda: now[0])(request(config))
    assert result["cancelled"] is True
    assert len(received) == (1 if stop == "cancel" else 2)
    assert client.calls[-2:] == ["data_closed", "close"]
    assert acquisition.read_listings(result) == []


@pytest.mark.parametrize("remote", ["/LMS/../escape", "/other/x", "/LMS/a\r\nDELE x"])
def test_request_rejected_before_credential_access(tmp_path: Path, remote: str) -> None:
    config = prepared(tmp_path)
    run = native.runner(config, credential_reader=lambda _: pytest.fail("opened credentials"))
    value = request(config)
    value["operations"][0]["remote"] = remote
    with pytest.raises(ValueError):
        run(value)
    assert not config.operation_root.exists()


def test_final_reply_required_and_partial_success_retained(tmp_path: Path) -> None:
    config, client = prepared(tmp_path), Client()
    inventory(config)
    client.reply = "426 transfer aborted"
    value = request(config, kind="get")
    result = runner(config, client)(value)
    with pytest.raises(ValueError, match="successful acquisition"):
        acquisition.retain_download(
            config,
            "/LMS/history.zip",
            Path(value["operations"][0]["local"]),
            result,
            {"version": 1, "files": {}},
        )
    assert Path(value["operations"][0]["local"]).read_bytes() == b"old"
    client.reply = "226 complete"
    value = request(config, name="b", kind="get")
    value["operations"].append({"kind": "list", "remote": "/LMS"})
    client.lines = ["unsupported listing"]
    result = runner(config, client)(value)
    assert result["exit_code"] == 1
    state: dict[str, Any] = {"version": 1, "files": {}}
    acquisition.retain_download(
        config,
        "/LMS/history.zip",
        Path(value["operations"][0]["local"]),
        result,
        state,
    )
    receipt = state["files"]["/LMS/history.zip"]["receipts"][0]
    assert receipt["batch_exit_code"] == 1 and receipt["publication_utc"] is None
    assert receipt["started_utc"] == result["results"][0]["started_utc"]


def test_native_receipt_binding_hash_and_mixed_legacy_retry(tmp_path: Path) -> None:
    config, client = prepared(tmp_path), Client()
    inventory(config)
    state = acquisition.acquire(config, ["/LMS/history.zip"], runner=Transport(config))
    old = copy.deepcopy(state["files"]["/LMS/history.zip"]["receipts"])
    state = acquisition.acquire(
        config, ["/LMS/history.zip"], runner=runner(config, client), recheck=True
    )
    assert len(state["files"]["/LMS/history.zip"]["versions"]) == 1
    assert state["files"]["/LMS/history.zip"]["receipts"][:1] == old
    value = request(config, kind="get")
    result = runner(config, client)(value)
    mutated = copy.deepcopy(result)
    mutated["operations"][0]["remote"] = "/LMS/different.zip"
    with pytest.raises(ValueError, match="binding"):
        acquisition.native_receipt(mutated)
    stage = Path(value["operations"][0]["local"])
    stage.write_bytes(b"new")
    with pytest.raises(ValueError, match="hash differs"):
        acquisition.retain_download(config, "/LMS/history.zip", stage, result, state)


def test_native_discovery_resume_keeps_legacy_listing_evidence(tmp_path: Path) -> None:
    config, client = prepared(tmp_path), Client()
    legacy = {
        "directory": "/LMS",
        "entries": [{"filename": "next", "type": "D"}],
        "evidence": "retained-legacy-operations.xml",
        "observed_at": "2026-10-22T12:00:00Z",
    }
    acquisition.write_json(config.inventory_path, acquisition.catalogue([legacy], "older"))
    result = acquisition.discover(config, runner=runner(config, client))
    assert result["complete"] and result["listings"][0] == legacy
    assert result["listings"][1]["directory"] == "/LMS/next"
    assert result["listings"][1]["evidence"].endswith("receipt.json")


def test_legacy_to_native_timestamp_format_retry_does_not_download_or_rewrite(
    tmp_path: Path,
) -> None:
    config, client = prepared(tmp_path), Client()
    legacy_stamp = "2014-10-17T10:59:00.000Z"
    inventory(config, modification=legacy_stamp)
    acquisition.acquire(config, ["/LMS/history.zip"], runner=Transport(config))
    checkpoint = config.operation_root / "acquisition-state.json"
    original_state = checkpoint.read_bytes()
    inventory(config, modification="2014-10-17T10:59:00Z")
    original_listing = config.inventory_path.read_bytes()
    assert acquisition.acquisition_status(config)["statuses"] == {"retained": 1}
    acquisition.acquire(config, ["/LMS/history.zip"], runner=runner(config, client))
    assert client.calls == []
    assert checkpoint.read_bytes() == original_state
    assert config.inventory_path.read_bytes() == original_listing
    inventory(config, modification="2014-10-17T10:59:01Z")
    assert acquisition.acquisition_status(config)["statuses"] == {"remote_metadata_changed": 1}
    acquisition.acquire(config, ["/LMS/history.zip"], runner=runner(config, client))
    assert client.calls.count("RETR /LMS/history.zip") == 1
    receipts = acquisition.read_state(config)["files"]["/LMS/history.zip"]["receipts"]
    assert len(receipts) == 2 and receipts[0]["server_modification"] == legacy_stamp


@pytest.mark.skipif(os.name != "nt", reason="Real CurrentUser DPAPI and Windows ACL roundtrip")
def test_local_powershell_dpapi_roundtrip_and_acl(tmp_path: Path) -> None:
    blob = tmp_path / "credentials/test.bin"
    script = tmp_path / "protect.ps1"
    setup = (Path.cwd() / "scripts/Initialize-FtpConnection.ps1").read_text()
    prefix = setup.split("$form = $null", 1)[0]
    script.write_text(
        prefix
        + """
Add-Type -AssemblyName System.Security
$folder = [IO.Path]::GetDirectoryName($env:FTP_TEST_BLOB)
[void][IO.Directory]::CreateDirectory($folder)
Protect-LocalPath $folder $true
$login = @{
    version=1
    username='synthetic-user'
    password='synthetic-password' # pragma: allowlist secret
}
$json = $login | ConvertTo-Json
$plain = [Text.Encoding]::UTF8.GetBytes($json)
$entropy = [Text.Encoding]::UTF8.GetBytes('apotek-ftp-credentials-v1')
$scope = [Security.Cryptography.DataProtectionScope]::CurrentUser
$bytes = [Security.Cryptography.ProtectedData]::Protect($plain, $entropy, $scope)
$header = [Text.Encoding]::ASCII.GetBytes("APOTEK-FTP-DPAPI-V1`n")
[IO.File]::WriteAllBytes($env:FTP_TEST_BLOB, [byte[]]($header + $bytes))
Protect-LocalPath $env:FTP_TEST_BLOB $false
$acl = [IO.File]::GetAccessControl($env:FTP_TEST_BLOB)
if (-not $acl.AreAccessRulesProtected -or $acl.Access.Count -ne 3) { exit 1 }
""",
        encoding="ascii",
    )
    process = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-ConfigFile",
            str(tmp_path / "unused"),
        ],
        env={**os.environ, "FTP_TEST_BLOB": str(blob)},
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    assert process.stdout == b""
    assert native.credentials(blob) == ("synthetic-user", "synthetic-password")
    assert b"synthetic-password" not in blob.read_bytes()
    blob.write_bytes(blob.read_bytes()[:-1] + b"x")
    with pytest.raises(native.NativeFtpError, match="unavailable"):
        native.credentials(blob)


@pytest.mark.skipif(
    os.name != "nt", reason="Native Windows Forms setup without displaying a dialog"
)
@pytest.mark.parametrize("save", [True, False])
@pytest.mark.parametrize("existing_encoding", [None, "latin-1"])
def test_setup_dialog_synthetic_save_cancel_without_network(
    tmp_path: Path, save: bool, existing_encoding: str | None
) -> None:
    original: dict[str, Any] = {"version": 1, "database": "unchanged", "other": {"preserve": True}}
    if existing_encoding is not None:
        original["ftp_transport"] = {
            "version": 1,
            "backend": "python-ftplib",
            "host": "synthetic.invalid",
            "port": 21,
            "security": "explicit_tls",
            "encoding": existing_encoding,
            "credential_file": str(tmp_path / "previous-credential-never-opened.bin"),
        }
    config = tmp_path / "config.json"
    acquisition.write_json(config, original)
    before = config.read_bytes()
    setup = (Path.cwd() / "scripts/Initialize-FtpConnection.ps1").read_text()
    selected = "OK" if save else "Cancel"
    setup = setup.replace(
        "if ($form.ShowDialog() -ne [Windows.Forms.DialogResult]::OK)",
        "$fields['Host'].Text='synthetic.invalid'; $fields['Username'].Text='synthetic-user'; "
        "$fields['Password'].Text='synthetic-password'; "  # pragma: allowlist secret
        f"if ([Windows.Forms.DialogResult]::{selected} -ne [Windows.Forms.DialogResult]::OK)",
    )
    script = tmp_path / "setup-synthetic.ps1"
    script.write_text(setup, encoding="ascii")
    process = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-ConfigFile",
            str(config),
        ],
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == (0 if save else 2), process.stderr.decode(errors="replace")
    if save:
        after = json.loads(config.read_text())
        assert {key: after[key] for key in original if key != "ftp_transport"} == {
            key: value for key, value in original.items() if key != "ftp_transport"
        }
        settings = native.configuration(after["ftp_transport"])
        assert settings is not None and settings.security == "explicit_tls"
        assert settings.encoding == (existing_encoding or "utf-8")
        if existing_encoding is not None:
            assert {
                key: after["ftp_transport"][key]
                for key in original["ftp_transport"]
                if key != "credential_file"
            } == {
                key: value
                for key, value in original["ftp_transport"].items()
                if key != "credential_file"
            }
        assert native.credentials(settings.credential_file) == (
            "synthetic-user",
            "synthetic-password",
        )
        assert next(tmp_path.glob("config.json.before-native-*.json")).read_bytes() == before
        assert "synthetic-password" not in process.stdout.decode()
        assert "synthetic-password" not in config.read_text()
    else:
        assert config.read_bytes() == before
        assert not (tmp_path / "credentials").exists()
