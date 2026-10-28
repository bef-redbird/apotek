from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from bef_redbird.research.e6_volatility_dashboard import VolatilityEvidenceError, load_evidence


def pin(path: Path) -> dict:
    return {
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def fixture(tmp_path: Path) -> Path:
    (tmp_path / "report.md").write_text("Saved twelve-period evidence.", encoding="utf-8")
    Image.new("RGB", (20, 10), "white").save(tmp_path / "chart.png")
    (tmp_path / "examples.json").write_text(
        json.dumps(
            {
                "version": "e6-volatility-examples-v1",
                "records": [
                    {
                        "title": "Synthetic package",
                        "markdown": "A gap remains unknown.",
                        "chart": pin(tmp_path / "chart.png"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "version": "e6-volatility-index-v1",
                "status": "research_not_promoted",
                "artifacts": {
                    "report": pin(tmp_path / "report.md"),
                    "examples": pin(tmp_path / "examples.json"),
                },
            }
        ),
        encoding="utf-8",
    )
    return tmp_path / "index.json"


def app(path: Path | None) -> AppTest:
    argument = "None" if path is None else f"Path({str(path)!r})"
    return AppTest.from_string(
        "from pathlib import Path\n"
        "from bef_redbird.research.e6_volatility_dashboard import main\n"
        f"main({argument})"
    ).run()


def test_saved_report_examples_and_no_mutation(tmp_path: Path) -> None:
    index = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    value = app(index)
    assert not value.exception and not value.error
    assert any("twelve-period" in element.value for element in value.markdown)
    assert any("gap remains unknown" in element.value for element in value.markdown)
    assert "retrospective" in value.warning[0].value
    assert len(value.get("image")) == 1
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("name", ["report.md", "examples.json", "chart.png"])
def test_corruption_fails_closed(tmp_path: Path, name: str) -> None:
    index = fixture(tmp_path)
    (tmp_path / name).write_bytes(b"changed")
    value = app(index)
    assert not value.exception
    assert "checksum" in value.error[0].value
    assert not value.markdown and not value.get("image")


@pytest.mark.parametrize("target", ["../outside.md", "https://example.invalid/report.md"])
def test_escaping_and_remote_references_rejected(tmp_path: Path, target: str) -> None:
    index = fixture(tmp_path)
    document = json.loads(index.read_text())
    document["artifacts"]["report"]["path"] = target
    index.write_text(json.dumps(document))
    with pytest.raises(VolatilityEvidenceError):
        load_evidence(index)


def test_missing_incompatible_and_embedded_media(tmp_path: Path) -> None:
    assert not app(None).exception
    assert "--e6-volatility" in app(None).info[0].value
    missing = app(tmp_path / "absent.json")
    assert not missing.exception and missing.error
    index = fixture(tmp_path)
    document = json.loads(index.read_text())
    document["version"] = "e6-index-v1"
    index.write_text(json.dumps(document))
    assert "Incompatible" in app(index).error[0].value
    index = fixture(tmp_path)
    (tmp_path / "report.md").write_text("![remote](https://example.invalid/image.png)")
    document = json.loads(index.read_text())
    document["artifacts"]["report"] = pin(tmp_path / "report.md")
    index.write_text(json.dumps(document))
    assert "separately pinned" in app(index).error[0].value
