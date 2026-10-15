"""Independent Git-index and content-safety cases; never use private fixtures."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_repo_hygiene.py"
spec = importlib.util.spec_from_file_location("hygiene", SCRIPT)
assert spec and spec.loader
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


@pytest.mark.parametrize(
    "path",
    [
        "uploads/report.txt",
        "report.xlsx",
        "data/sample.csv",
        "db.duckdb.wal",
        ".env",
        ".streamlit/secrets.toml",
        "screen.png",
        ".codex/sessions/session.jsonl",
        ".codex/hooks.json",
        ".agents/skills/review/SKILL.md",
        "AGENTS.md",
        "CODEX_HANDOVER.md",
        "src/AGENTS.md",
    ],
)
def test_forbidden_artifacts(path: str) -> None:
    assert hygiene.content_issues(path, b"synthetic")


@pytest.mark.parametrize(
    "path",
    [
        "src/bef_redbird/domain.py",
        ".github/workflows/ci.yml",
        ".env.example",
        "docs/calculations.md",
        "tests/fixtures/recipe.py",
        "uv.lock",
    ],
)
def test_authored_source_allowed(path: str) -> None:
    assert hygiene.content_issues(path, b"# Synthetic authored content\n") == []


def test_disguised_data_and_binary_detected() -> None:
    header = "\t".join(["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"])
    assert "source-data-header" in hygiene.content_issues("innocent.txt", header.encode())
    assert "source-data-header" in hygiene.content_issues(
        "innocent.txt", b"Varenummer;Indikator;20260105"
    )
    assert "binary-content" in hygiene.content_issues("innocent.txt", b"PK\x03\x04fake zip")
    assert "private-key" in hygiene.content_issues(
        "innocent.txt", b"-----BEGIN " + b"RSA PRIVATE KEY-----\nsynthetic"
    )
    assert "unsupported-index-entry" in hygiene.content_issues("link", b"target", "120000")


def run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_index_not_working_tree_and_no_sensitive_output(tmp_path: Path) -> None:
    run_git(tmp_path, "init", "-q")
    filename = "sensitive-file-name.txt"
    target = tmp_path / filename
    target.write_bytes(b"-----BEGIN " + b"PRIVATE KEY-----\nSYNTHETIC_SECRET")
    run_git(tmp_path, "add", "--", filename)
    target.write_text("safe replacement", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--staged"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "private-key" in result.stderr
    assert "SYNTHETIC_SECRET" not in result.stderr
    assert filename not in result.stderr
    run_git(tmp_path, "add", "--", filename)
    assert hygiene.inspect_index(tmp_path, staged=False) == []


def test_tracked_guard_catches_previously_committed_data(tmp_path: Path) -> None:
    run_git(tmp_path, "init", "-q")
    (tmp_path / "disguised.txt").write_text("Varenummer;Indikator;20260105\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(
        tmp_path,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    assert hygiene.inspect_index(tmp_path, staged=True) == []
    assert hygiene.inspect_index(tmp_path, staged=False) == [
        ("disguised.txt", ["source-data-header"])
    ]


def test_size_limit_does_not_depend_on_extension() -> None:
    assert "file-too-large" in hygiene.content_issues("source.txt", b"x" * (hygiene.MAX_BYTES + 1))


@pytest.mark.parametrize(
    "source",
    [
        'CITO_HEADERS = ["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"]\n',
        'REQUIRED_CITO = ("varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp")\n',
        'assert set(headers) == {"varenr", "opbevkode", "kostpris", '
        '"subkode", "nysubkode", "subgrp"}\n',
        'rows.append(["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"])\n',
        'HEADERS = [\n    "varenr", "opbevkode", "kostpris", '
        '"subkode", "nysubkode", "subgrp",\n]\n',
    ],
)
def test_authored_python_header_constants_are_not_tabular_data(source: str) -> None:
    assert hygiene.content_issues("tests/fixtures/recipe.py", source.encode()) == []


@pytest.mark.parametrize("filename", ["innocent.py", "innocent.txt", "innocent.csv", "innocent.md"])
@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_full_source_header_is_rejected_regardless_of_extension(
    filename: str,
    delimiter: str,
) -> None:
    header = delimiter.join(["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"])
    assert "source-data-header" in hygiene.content_issues(filename, header.encode())


def test_quoted_csv_header_is_still_detected() -> None:
    header = ",".join(
        '"' + value + '"'
        for value in [
            "varenr",
            "opbevkode",
            "kostpris",
            "subkode",
            "nysubkode",
            "subgrp",
        ]
    )
    assert hygiene.content_issues("disguised.py", header.encode()) == ["source-data-header"]
