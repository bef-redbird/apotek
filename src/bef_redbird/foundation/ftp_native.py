"""Read-only stdlib FTP transport and versioned native acquisition evidence.

Only newly provisioned CurrentUser DPAPI credentials are opened. No saved client
sessions, plaintext credential files, subprocess secret channels or protocol
downgrades are supported. Legacy source receipts belong to the acquisition reader.
"""

from __future__ import annotations

import ctypes
import ftplib
import hashlib
import json
import os
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KIND = "python-ftplib-acquisition-v1"
MAGIC = b"APOTEK-FTP-DPAPI-V1\n"
ENTROPY = b"apotek-ftp-credentials-v1"


@dataclass(frozen=True)
class NativeConfig:
    host: str
    port: int
    security: str
    credential_file: Path
    encoding: str = "utf-8"
    timeout_seconds: int = 30
    batch_timeout_seconds: int = 300


class NativeFtpError(RuntimeError):
    """Bounded, non-sensitive transport failure."""


class TransferCancelled(NativeFtpError):
    """Cancellation or the absolute batch deadline was reached."""


def configuration(raw: Any) -> NativeConfig | None:
    from .archive import external_path

    if raw is None:
        return None  # Read-only historical status does not require new credentials.
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or raw.get("backend") != "python-ftplib"
    ):
        raise ValueError("Explicit supported native FTP transport configuration required")
    host, port = raw.get("host"), raw.get("port")
    if (
        not isinstance(host, str)
        or not host
        or len(host) > 253
        or re.search(r"[\s/@\\\x00-\x1f]", host)
        or "://" in host
    ):
        raise ValueError("An explicit FTP host without credentials or URL is required")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("An explicit FTP port is required")
    security = raw.get("security", "explicit_tls")
    if security not in {"explicit_tls", "plain"}:
        raise ValueError("Only explicit FTPS and explicitly selected plain FTP are supported")
    credential = raw.get("credential_file")
    if not isinstance(credential, str) or not Path(credential).is_absolute():
        raise ValueError("An absolute private DPAPI credential file is required")
    encoding = raw.get("encoding", "utf-8")
    if encoding not in {"utf-8", "latin-1", "cp1252"}:
        raise ValueError("Unsupported explicit FTP filename encoding")
    timeout = raw.get("timeout_seconds", 30)
    batch_timeout = raw.get("batch_timeout_seconds", 300)
    if type(timeout) is not int or not 5 <= timeout <= 120:
        raise ValueError("FTP socket timeout must be bounded from 5 through 120 seconds")
    if type(batch_timeout) is not int or not timeout <= batch_timeout <= 900:
        raise ValueError(
            "FTP batch deadline must be bounded from socket timeout through 900 seconds"
        )
    return NativeConfig(
        host, port, security, external_path(Path(credential)), encoding, timeout, batch_timeout
    )


def credentials(path: Path) -> tuple[str, str]:
    """Decrypt our own opaque DPAPI blob inside the transfer process only."""
    if os.name != "nt":
        raise NativeFtpError("CurrentUser DPAPI credentials require Windows")
    if path.is_symlink() or path.stat().st_size > 65536:
        raise NativeFtpError("Invalid private DPAPI credential file")
    protected = path.read_bytes()
    if not protected.startswith(MAGIC):
        raise NativeFtpError("Unsupported private DPAPI credential format")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.c_void_p)]

    source = ctypes.create_string_buffer(protected[len(MAGIC) :])
    entropy = ctypes.create_string_buffer(ENTROPY)
    encrypted = Blob(len(protected) - len(MAGIC), ctypes.cast(source, ctypes.c_void_p))
    extra = Blob(len(ENTROPY), ctypes.cast(entropy, ctypes.c_void_p))
    decrypted = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    unprotect = crypt.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(Blob),
    ]
    unprotect.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        if not unprotect(
            ctypes.byref(encrypted),
            None,
            ctypes.byref(extra),
            None,
            None,
            1,
            ctypes.byref(decrypted),
        ):
            raise NativeFtpError("DPAPI credential unavailable to this Windows account")
        raw = json.loads(ctypes.string_at(decrypted.data, decrypted.size).decode("utf-8"))
        username, password = raw.get("username"), raw.get("password")
        if (
            raw.get("version") != 1
            or set(raw) != {"version", "username", "password"}
            or not isinstance(username, str)
            or not username
            or not isinstance(password, str)
            or not password
            or any(c in username + password for c in "\r\n\x00")
        ):
            raise NativeFtpError("Invalid private DPAPI credential payload")
        return username, password
    except (ValueError, TypeError, AttributeError):
        raise NativeFtpError("Invalid private DPAPI credential payload") from None
    finally:
        if decrypted.data:
            ctypes.memset(decrypted.data, 0, decrypted.size)
            kernel.LocalFree(decrypted.data)


def modification(value: str | None) -> str | None:
    """RFC3659 modify facts are UTC metadata, never publication timestamps."""
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]{14}(?:\.[0-9]{1,6})?", value):
        raise NativeFtpError("Unsupported MLSD modification fact")
    stamp = datetime.strptime(value, "%Y%m%d%H%M%S.%f" if "." in value else "%Y%m%d%H%M%S")
    return stamp.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def listing_entry(directory: str, name: str, facts: dict[str, str]) -> dict[str, Any] | None:
    from .ftp_acquisition import remote_path

    kind = facts.get("type", "").lower()
    if kind in {"cdir", "pdir"}:
        return None
    if not name or "/" in name:
        raise NativeFtpError("Invalid MLSD directory member")
    remote_path(directory + "/" + name)
    if kind == "file":
        if not re.fullmatch(r"[0-9]+", facts.get("size", "")):
            raise NativeFtpError("MLSD file lacks a valid exact byte size")
        member_type = "-"
    elif kind == "dir":
        member_type = "D"
    elif kind.startswith("os.unix=slink"):
        member_type = "L"  # Retain identity only; never follow links.
    else:
        raise NativeFtpError("Unsupported MLSD member type")
    entry: dict[str, Any] = {"filename": name, "type": member_type, "raw_mlsd_facts": facts}
    if "size" in facts:
        if not re.fullmatch(r"[0-9]+", facts["size"]):
            raise NativeFtpError("Invalid MLSD byte size")
        entry["size"] = str(int(facts["size"]))
    entry["modification"] = modification(facts.get("modify"))
    return entry


def runner(
    config: Any,
    *,
    credential_reader: Callable[[Path], tuple[str, str]] = credentials,
    client_factory: Callable[[NativeConfig], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create one bounded runner; injected factories are synthetic test boundaries."""
    from .archive import external_path
    from .ftp_acquisition import remote_path, utc_now, write_json

    settings = config.transport
    if settings is None:
        raise NativeFtpError(
            "Configure native FTP and provision local DPAPI credentials before collection"
        )

    def connect(diagnostic: dict[str, str]) -> Any:
        if client_factory is not None:
            client = client_factory(settings)
        elif settings.security == "explicit_tls":
            client = ftplib.FTP_TLS(
                context=ssl.create_default_context(),
                timeout=settings.timeout_seconds,
                encoding=settings.encoding,
            )
        else:
            client = ftplib.FTP(timeout=settings.timeout_seconds, encoding=settings.encoding)
        try:
            diagnostic["phase"] = "credentials"
            username, password = credential_reader(settings.credential_file)
            if (
                not isinstance(username, str)
                or not username
                or not isinstance(password, str)
                or not password
                or any(c in username + password for c in "\r\n\x00")
            ):
                raise NativeFtpError("Invalid native FTP credentials")
            diagnostic["phase"] = "connect"
            client.connect(settings.host, settings.port, timeout=settings.timeout_seconds)
            diagnostic["phase"] = "login"
            client.login(username, password)
            if settings.security == "explicit_tls":
                diagnostic["phase"] = "protect_data"
                client.prot_p()
            return client
        except Exception:
            client.close()
            raise

    def run(request: dict[str, Any]) -> dict[str, Any]:
        name, operations = request.get("name"), request.get("operations")
        if not isinstance(name, str) or not re.fullmatch(r"ftp-[a-f0-9]{32}", name):
            raise ValueError("Invalid native FTP operation identity")
        if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
            raise ValueError("Bounded native FTP operation batch required")
        seen: set[tuple[str, str]] = set()
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("kind") not in {"list", "get"}:
                raise ValueError("Only native list/get operations are supported")
            if set(operation) != (
                {"kind", "remote", "local"} if operation["kind"] == "get" else {"kind", "remote"}
            ):
                raise ValueError("Unsupported native operation fields")
            remote = remote_path(operation["remote"])
            key = (operation["kind"], remote)
            if key in seen:
                raise ValueError("Duplicate native FTP operation")
            seen.add(key)
            if operation["kind"] == "get":
                local = external_path(Path(operation["local"]))
                if (
                    not Path(operation["local"]).is_absolute()
                    or not local.is_relative_to(config.operation_root / "downloads")
                    or local.exists()
                    or local.is_symlink()
                ):
                    raise ValueError("Native download must use unused private staging")
        if any(x["kind"] == "get" for x in operations) and len(operations) > 50:
            raise ValueError("Bounded native download batch required")
        output = config.operation_root / name
        output.mkdir(parents=True, exist_ok=False)
        evidence = output / "receipt.json"
        started, deadline = utc_now(), clock() + settings.batch_timeout_seconds
        result: dict[str, Any] = {
            "version": 2,
            "kind": KIND,
            "name": name,
            "started_utc": started,
            "completed_utc": started,
            "exit_code": 1,
            "operations": operations,
            "results": [],
            "transport": {
                "backend": "python-ftplib",
                "host": settings.host,
                "port": settings.port,
                "security": settings.security,
                "encoding": settings.encoding,
                "credential_mechanism": "windows-dpapi-current-user",
                "tls_certificate_verification": settings.security == "explicit_tls",
                "listing_protocol": "MLSD",
                "batch_timeout_seconds": settings.batch_timeout_seconds,
            },
            "timestamp_scope": "Acquisition intervals and raw server metadata; publication unknown",
        }
        client: Any = None
        diagnostic = {"phase": "before_connect"}

        def check() -> None:
            if (config.operation_root / "cancel-request.json").exists() or clock() >= deadline:
                raise TransferCancelled("Native FTP cancelled or batch deadline reached")
            if client is not None and getattr(client, "sock", None) is not None:
                client.sock.settimeout(min(settings.timeout_seconds, max(0.1, deadline - clock())))
                client.timeout = min(settings.timeout_seconds, max(0.1, deadline - clock()))

        def transfer(command: str, callback: Callable[[bytes], None], *, binary: bool) -> None:
            """Bound active data reads, including a trickling incomplete MLSD line."""
            check()
            client.voidcmd("TYPE I" if binary else "TYPE A")
            check()
            with client.transfercmd(command) as data:
                last_data = clock()
                while True:
                    check()
                    data.settimeout(
                        min(1.0, settings.timeout_seconds, max(0.01, deadline - clock()))
                    )
                    try:
                        data_block = data.recv(8192)
                    except TimeoutError:
                        check()
                        if clock() - last_data >= settings.timeout_seconds:
                            raise NativeFtpError("Native FTP data connection timed out") from None
                        continue
                    if not data_block:
                        break
                    last_data = clock()
                    callback(data_block)
                if isinstance(data, ssl.SSLSocket):
                    check()
                    data.settimeout(min(settings.timeout_seconds, max(0.01, deadline - clock())))
                    data.unwrap()
            check()
            response = client.voidresp()
            if not isinstance(response, str) or not response.startswith("2"):
                raise NativeFtpError("Native FTP transfer lacks final completion reply")
            check()

        try:
            check()
            client = connect(diagnostic)
            for operation in operations:
                check()
                diagnostic["phase"] = operation["kind"]
                record: dict[str, Any] = {
                    **operation,
                    "started_utc": utc_now(),
                    "completed_utc": utc_now(),
                    "success": False,
                }
                result["results"].append(record)
                try:
                    if operation["kind"] == "list":
                        entries: list[dict[str, Any]] = []
                        names: set[str] = set()
                        # No LIST fallback: unsupported MLSD must be resolved explicitly.

                        def listed(
                            line: str,
                            directory: str = operation["remote"],
                            entries: list[dict[str, Any]] = entries,
                            names: set[str] = names,
                        ) -> None:
                            check()
                            raw_facts, separator, filename = line.partition(" ")
                            if not separator or not raw_facts.endswith(";"):
                                raise NativeFtpError("Malformed MLSD fact record")
                            facts: dict[str, str] = {}
                            for item in raw_facts[:-1].split(";"):
                                key, equals, value = item.partition("=")
                                if not equals or not key or key.lower() in facts:
                                    raise NativeFtpError("Malformed or duplicate MLSD fact")
                                facts[key.lower()] = value
                            entry = listing_entry(directory, filename, facts)
                            if entry is not None:
                                if filename in names or len(names) >= 100000:
                                    raise NativeFtpError("Duplicate MLSD directory member")
                                names.add(filename)
                                entries.append(entry)

                        buffered = bytearray()

                        def listing_block(
                            data: bytes,
                            buffered: bytearray = buffered,
                            listed: Callable[[str], None] = listed,
                        ) -> None:
                            check()
                            buffered.extend(data)
                            while b"\n" in buffered:
                                line, _, remainder = buffered.partition(b"\n")
                                if len(line) > 8192:
                                    raise NativeFtpError("MLSD line exceeds native size bound")
                                buffered[:] = remainder
                                listed(line.removesuffix(b"\r").decode(settings.encoding))
                            if len(buffered) > 8192:
                                raise NativeFtpError("MLSD line exceeds native size bound")

                        transfer("MLSD " + operation["remote"], listing_block, binary=False)
                        if buffered:
                            raise NativeFtpError("MLSD response lacks complete line framing")
                        record["entries"] = entries
                    else:
                        destination = Path(operation["local"])
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        digest, size = hashlib.sha256(), 0
                        with destination.open("xb") as stream:

                            def block(data: bytes, digest: Any = digest) -> None:
                                nonlocal size
                                check()
                                stream.write(data)
                                digest.update(data)
                                size += len(data)

                            transfer("RETR " + operation["remote"], block, binary=True)
                            check()
                            stream.flush()
                            os.fsync(stream.fileno())
                        record.update({"bytes": size, "sha256": digest.hexdigest()})
                    check()
                    record["success"] = True
                finally:
                    record["completed_utc"] = utc_now()
                    result["completed_utc"] = utc_now()
                    write_json(evidence, result)  # Preserve earlier per-operation successes.
            result["exit_code"] = 0
        except Exception as error:
            # Never serialize exception messages, FTP replies or credential payloads.
            result["failure_type"] = type(error).__name__
            result["failure_phase"] = diagnostic["phase"]
            if isinstance(error, ftplib.Error):
                code = re.match(r"^([0-9]{3})(?:\s|$)", str(error))
                if code is not None:
                    result["ftp_response_code"] = int(code[1])
            result["cancelled"] = isinstance(error, TransferCancelled)
        finally:
            if client is not None:
                client.close()  # No blocking QUIT/extra control round trip.
            result["completed_utc"] = utc_now()
            write_json(evidence, result)
        return {**result, "receipt_path": str(evidence)}

    return run
