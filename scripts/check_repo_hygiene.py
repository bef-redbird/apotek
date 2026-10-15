"""Reject accidental private/generated content using Git's index, never source logs."""

from __future__ import annotations

import argparse
import ast
import csv
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

MAX_BYTES = 2 * 1024 * 1024
FORBIDDEN_DIRS = {
    ".agents",
    ".codex",
    "data",
    "runtime",
    "uploads",
    "exports",
    "reports",
    "artifacts",
    "logs",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "build",
    "dist",
    "playwright-report",
    "test-results",
    "catboost_info",
}
FORBIDDEN_SUFFIXES = {
    ".xlsx",
    ".xls",
    ".xlsm",
    ".xlsb",
    ".duckdb",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".parquet",
    ".arrow",
    ".pkl",
    ".pickle",
    ".joblib",
    ".cbm",
    ".log",
    ".tmp",
    ".bak",
    ".pyc",
    ".pyo",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".exe",
    ".dll",
    ".coverage",
}
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")


def has_source_header(text: str, *, python_source: bool = False) -> bool:
    """Match complete delimited rows, not header constants embedded in source syntax."""
    cito = {"varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"}
    authored_lines: set[int] = set()
    if python_source:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            pass
        else:
            # Real assignments/calls may span a line containing only quoted names.
            # A bare CSV header parses as a tuple expression and gets no exemption.
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Assert, ast.Call)):
                    authored_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    for lineno, line in enumerate(text.splitlines()[:100], 1):
        if lineno in authored_lines:
            continue
        for delimiter in ("\t", ";", ",", "|"):
            try:
                values = next(
                    csv.reader([line], delimiter=delimiter, skipinitialspace=True, strict=True)
                )
            except (csv.Error, StopIteration):
                continue
            cells = [value.strip().strip("\"'").lower() for value in values]
            # A code assignment/list/tuple has punctuation outside its quoted cells.
            # Do not partially match its middle fields as if they were a data row.
            if not all(not cell or re.fullmatch(r"[\w ./%:-]+", cell) for cell in cells):
                continue
            headers = set(cells)
            if len(cito & headers) >= 4 or {"varenummer", "indikator"} <= headers:
                return True
    return False


def git(root: Path, *args: str) -> bytes:
    """Run Git without interpolating content into a shell or exposing errors."""
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Git index inspection failed")
    return result.stdout


def content_issues(path: str, data: bytes, mode: str = "100644") -> list[str]:
    """Return safe reason codes; never return matched content or source values."""
    item = PurePosixPath(path.lower())
    reasons: list[str] = []
    if any(part in FORBIDDEN_DIRS for part in item.parts[:-1]):
        reasons.append("forbidden-directory")
    name = item.name
    if (
        item.suffix in FORBIDDEN_SUFFIXES
        or name.startswith(("~$", ".coverage", "coverage.xml"))
        or any(token in name for token in (".duckdb.", ".db-wal", ".db-shm", ".log."))
        or name in {"secrets.toml", "thumbs.db", ".ds_store", "agents.md", "codex_handover.md"}
        or (name.startswith(".env") and name != ".env.example")
    ):
        reasons.append("forbidden-artifact")
    if mode not in {"100644", "100755"}:
        reasons.append("unsupported-index-entry")
    if len(data) > MAX_BYTES:
        reasons.append("file-too-large")
    if data.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0")) or b"\x00" in data:
        reasons.append("binary-content")
    if PRIVATE_KEY.search(data):
        reasons.append("private-key")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        reasons.append("non-utf8-content")
    else:
        if has_source_header(text, python_source=item.suffix == ".py"):
            reasons.append("source-data-header")
    return reasons


def inspect_index(root: Path, *, staged: bool) -> list[tuple[str, list[str]]]:
    """Inspect staged blobs even if their working-tree copies have been replaced."""
    selected: set[bytes] | None = None
    if staged:
        selected = set(
            git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
            .rstrip(b"\x00")
            .split(b"\x00")
        )
    failures = []
    for entry in git(root, "ls-files", "--stage", "-z").split(b"\x00"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        if selected is not None and raw_path not in selected:
            continue
        mode, object_id, stage = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8", errors="backslashreplace")
        if stage != "0":
            failures.append((path, ["unmerged-index-entry"]))
            continue
        size = int(git(root, "cat-file", "-s", object_id).strip())
        if size > MAX_BYTES:
            failures.append((path, ["file-too-large"]))
            continue
        data = git(root, "cat-file", "blob", object_id)
        reasons = content_issues(path, data, mode)
        if reasons:
            failures.append((path, reasons))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true", help="inspect commit index changes")
    group.add_argument("--tracked", action="store_true", help="inspect every tracked index blob")
    args = parser.parse_args(argv)
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        failures = inspect_index(root, staged=args.staged)
    except (RuntimeError, OSError, ValueError):
        print("Repository hygiene failed: index-inspection-error", file=sys.stderr)
        return 2
    for position, (_, reasons) in enumerate(failures, start=1):
        # Even filenames may contain private information. Review git diff locally.
        print(f"Rejected index item {position}: {', '.join(reasons)}", file=sys.stderr)
    if failures:
        print(
            "Review staged/tracked files locally; no matched content was logged.", file=sys.stderr
        )
        return 1
    print("Repository hygiene passed (index content checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
