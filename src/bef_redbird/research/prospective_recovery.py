"""Small local capsule with shared pinned models and captured-code guarded replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from .e4_common import checked, read
from .e6_delivery import pins
from .group_features import pin

RUNNER = r"""
import builtins, io, json, socket, sys, os, subprocess
from pathlib import Path
original, restored, repository = map(lambda x: Path(x).resolve(), sys.argv[1:4])
code = restored / 'producer'
sys.path.insert(0, str(code / 'src'))
import catboost, duckdb, sqlite3
def forbidden(*a, **kw): raise RuntimeError('guarded replay: forbidden operation')
catboost.CatBoost.fit = forbidden
catboost.CatBoostClassifier.fit = forbidden
catboost.CatBoostRegressor.fit = forbidden
catboost.train = catboost.cv = forbidden
duckdb.connect = sqlite3.connect = forbidden
for name in ('sql', 'query', 'execute', 'read_csv', 'read_json', 'read_parquet',
             'from_csv_auto', 'from_parquet', 'register', 'cursor'):
    if hasattr(duckdb, name): setattr(duckdb, name, forbidden)
socket.create_connection = socket.socket.connect = forbidden
from bef_redbird.research import prospective, prospective_report
from bef_redbird.research.e4_common import checked as original_checked
from bef_redbird.research.group_features import pin as original_pin
module_origins = {name: str(Path(module.__file__).resolve())
    for name, module in sys.modules.items()
    if name.startswith('bef_redbird') and getattr(module, '__file__', None)}
assert all(Path(path).is_relative_to(code) for path in module_origins.values())
def relocate(path):
    p = Path(path).resolve()
    if p.is_relative_to(original): return restored/'operation'/p.relative_to(original)
    if p.is_relative_to(repository): return code/p.relative_to(repository)
    return p
def canonical(path):
    p = Path(path).resolve()
    if p.is_relative_to(restored/'operation'): return original/p.relative_to(restored/'operation')
    if p.is_relative_to(code): return repository/p.relative_to(code)
    return p
dependencies = {}
def checked(ref):
    actual = relocate(ref['path'])
    result = original_checked({**ref, 'path': str(actual)})
    if not actual.is_relative_to(restored): dependencies[str(actual)] = original_pin(actual)
    return result
def pin(path):
    ref = original_pin(path)
    ref['path'] = str(canonical(path))
    return ref
for name, module in list(sys.modules.items()):
    if name.startswith('bef_redbird.'):
        if hasattr(module, 'checked'): module.checked = checked
        if hasattr(module, 'pin'): module.pin = pin
def audit(event, args):
    if event.startswith(('socket.', 'sqlite3.')) or event in (
            'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.spawn'):
        raise RuntimeError('guarded replay: forbidden operation')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        p = Path(args[0]).resolve()
        if not p.is_relative_to(restored) and not p.is_relative_to(repository/'.venv') and (
                p.is_relative_to(original) or p.is_relative_to(repository)):
            raise RuntimeError('original operation/code access blocked')
        if p.suffix.lower() in {'.duckdb', '.wal', '.db', '.sqlite', '.sqlite3'}:
            raise RuntimeError('database access blocked')
probe_socket = socket.socket()
sys.addaudithook(audit)
probes = [lambda: catboost.train(), lambda: catboost.cv(),
    lambda: catboost.CatBoostRegressor().fit(None),
    lambda: duckdb.connect(), lambda: duckdb.sql('SELECT 1'),
    lambda: sqlite3.dbapi2.connect(':memory:'),
    lambda: socket.getaddrinfo('localhost', 1),
    lambda: probe_socket.connect_ex(('127.0.0.1', 1)),
    lambda: probe_socket.sendto(b'', ('127.0.0.1', 1)),
    lambda: subprocess.run([sys.executable, '-V']), lambda: os.system(''),
    lambda: (original/'config.json').read_bytes(),
    lambda: (repository/'src/bef_redbird/research/prospective.py').read_bytes(),
    lambda: (original.parent/'.databases/working/foundation.duckdb').read_bytes()]
for probe in probes:
    try: probe()
    except RuntimeError: pass
    else: raise RuntimeError('guard failed a negative probe')
probe_socket.close()
root = restored/'operation'
proof = prospective.verify(root)
receipt = prospective.replay(root)
print(json.dumps({'verification': proof, 'replay': str(receipt),
    'shared_dependencies': list(dependencies.values()),
    'negative_guard_probes_passed': len(probes), 'captured_module_origins': module_origins,
    'fits_network_database_original_code_blocked': True}))
"""


def create(root: Path, repository: Path, bundle: Path) -> Path:
    root, repository, bundle = root.resolve(), repository.resolve(), bundle.resolve()
    if bundle.exists():
        raise ValueError("Use an unused capsule destination.")
    protocol = read(root / "protocol.json")
    dependencies = pins(protocol)
    for pair in protocol["amount_models"].values():
        for model in pair.values():
            receipt = read(checked(model["receipt"]))
            dependencies.append(receipt["files"]["predictions.parquet"])
    direction = read(checked(protocol["direction_reference"]))
    dependencies.extend(direction["files"].values())
    for ref in dependencies:
        checked(ref)
    excluded = {"scratch", "staging", "recovery", "adapter-verification", "__pycache__"}
    operation = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not set(path.relative_to(root).parts) & excluded
        and path.suffix not in {".pyc", ".log", ".zip"}
        and not path.name.startswith(".")
    ]
    source = [
        *repository.joinpath("src").rglob("*.py"),
        *repository.joinpath("src").rglob("*.sql"),
        repository / "uv.lock",
        repository / "pyproject.toml",
        repository / "research_dashboard.py",
        repository / "docs/prospective-comparison.md",
    ]
    bundle.mkdir(parents=True)
    inventory = []
    with zipfile.ZipFile(bundle / "payload.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for parent, prefix, files in (
            (root, "operation", operation),
            (repository, "producer", source),
        ):
            for path in sorted(files):
                if path.suffix.lower() in {".duckdb", ".wal", ".db", ".sqlite"}:
                    raise ValueError("Database files are outside this recovery scope.")
                name = f"{prefix}/{path.relative_to(parent).as_posix()}"
                archive.write(path, name)
                inventory.append({**pin(path), "member": name})
    manifest = {
        "version": "prospective-capsule-v1",
        "original": str(root),
        "repository": str(repository),
        "archive": pin(bundle / "payload.zip"),
        "files": inventory,
        "shared_dependencies": dependencies,
        "limits": "Local restore with existing interpreter; no operating database copied",
    }
    (bundle / "recovery.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return bundle / "recovery.json"


def verify(bundle: Path, destination: Path) -> dict[str, Any]:
    manifest = read(bundle / "recovery.json")
    for ref in manifest["files"]:
        path = (destination / ref["member"]).resolve()
        if not path.is_relative_to(destination.resolve()):
            raise ValueError("Capsule path escapes destination.")
        checked({**ref, "path": str(path)})
    return {"verified_files": len(manifest["files"]), "all_hashes_match": True}


def restore(bundle: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise ValueError("Restore destination must be unused.")
    manifest = read(bundle / "recovery.json")
    archive = checked(manifest["archive"])
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as payload:
        if set(payload.namelist()) != {x["member"] for x in manifest["files"]}:
            raise ValueError("Unexpected archive inventory.")
        for ref in manifest["files"]:
            path = (destination / ref["member"]).resolve()
            if not path.is_relative_to(destination.resolve()):
                raise ValueError("Archive path escapes destination.")
            data = payload.read(ref["member"])
            if hashlib.sha256(data).hexdigest() != ref["sha256"]:
                raise ValueError("Archive bytes differ.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return verify(bundle, destination)


def replay(bundle: Path, destination: Path, receipt: Path) -> dict[str, Any]:
    if receipt.exists():
        raise ValueError("Replay receipt must be unused.")
    before = verify(bundle, destination)
    manifest = read(bundle / "recovery.json")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            RUNNER,
            manifest["original"],
            str(destination.resolve()),
            manifest["repository"],
        ],
        cwd=destination,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    result = {
        "status": "passed" if process.returncode == 0 else "failed",
        "before": before,
        "after": verify(bundle, destination),
        "returncode": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
        "guard_producer": pin(Path(__file__)),
        "runner_sha256": hashlib.sha256(RUNNER.encode()).hexdigest(),
    }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if process.returncode:
        raise ValueError("Guarded replay failed; receipt retained.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "restore", "verify", "replay"])
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.command == "create" and args.root and args.repository:
        result: Any = str(create(args.root, args.repository, args.bundle))
    elif args.command == "restore" and args.destination:
        result = restore(args.bundle, args.destination)
    elif args.command == "verify" and args.destination:
        result = verify(args.bundle, args.destination)
    elif args.command == "replay" and args.destination and args.receipt:
        result = replay(args.bundle, args.destination, args.receipt)
    else:
        parser.error("Required command arguments are missing.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
