from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.research.prospective_dashboard import SavedComparisonError, load_saved


def fixture(root: Path, populated: bool = False) -> Path:
    def save(name: str, value: dict) -> dict:
        path = root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return pin(path)

    def pin(path: Path) -> dict:
        return {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }

    index = {
        "version": "prospective-index-v1",
        "artifacts": {
            "protocol": save("protocol.json", {"version": "synthetic", "fits": 0}),
            "readiness": save(
                "readiness.json",
                {"status": "Not ready", "missing": ["Verified receipt missing"], "schedule": []},
            ),
            "report": save("report.json", {"periods": [], "summaries": []}),
        },
        "forecasts": [],
    }
    if populated:
        frame = pd.DataFrame(
            {
                "package_number": ["001234", "999999"],
                "selected": [True, False],
                "rank": [1, 2],
                "eligible": [True, True],
                "amount_3y_delta_dkk": [2.0, -1.0],
                "amount_6y_delta_dkk": [3.0, -2.0],
                "amount_full_delta_dkk": [1.0, -3.0],
            }
        )
        frame.to_parquet(root / "predictions.parquet", index=False)
        predictions = pin(root / "predictions.parquet")
        assessment = save(
            "assessment.json",
            {
                "context": {
                    "origin": "2027-01-04",
                    "target": "2027-01-18",
                    "information_mode": "current",
                },
                "status": "synthetic fixture",
                "predictions": predictions,
            },
        )
        index["forecasts"] = [{"assessment": assessment, "predictions": predictions}]
    save("index.json", index)
    return root / "index.json"


def app(path: Path | None) -> AppTest:
    argument = "None" if path is None else f"Path({str(path)!r})"
    return AppTest.from_string(
        "from pathlib import Path\nfrom bef_redbird.research.prospective_dashboard import main\n"
        + f"main({argument})"
    ).run()


def test_readiness_empty_and_no_write(tmp_path: Path) -> None:
    index = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    view = app(index)
    assert not view.exception and not view.error
    assert any("Verified receipt missing" in x.value for x in view.info)
    assert any("No prospective forecast" in x.value for x in view.info)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert not app(None).exception


def test_populated_pending_filter_and_identifiers(tmp_path: Path) -> None:
    view = app(fixture(tmp_path, True))
    assert not view.exception and not view.error
    assert view.dataframe[0].value.package_number.tolist() == ["001234"]
    view.checkbox[0].uncheck().run()
    assert len(view.dataframe[0].value) == 2
    view.text_input[0].set_value("001").run()
    assert view.dataframe[0].value.package_number.tolist() == ["001234"]
    view.number_input[0].set_value(100).run()
    assert not view.exception
    assert any("Observed outcomes still missing" in x.value for x in view.info)


@pytest.mark.parametrize(
    "name",
    ["protocol.json", "readiness.json", "report.json", "predictions.parquet", "assessment.json"],
)
def test_corrupt_publication_hidden(tmp_path: Path, name: str) -> None:
    index = fixture(tmp_path, True)
    (tmp_path / name).write_bytes(b"corrupt")
    view = app(index)
    assert not view.exception and view.error and not view.dataframe


def test_missing_and_escaping(tmp_path: Path) -> None:
    assert app(tmp_path / "missing.json").error
    index = fixture(tmp_path)
    value = json.loads(index.read_text())
    value["artifacts"]["protocol"]["path"] = "../escape.json"
    index.write_text(json.dumps(value))
    with pytest.raises(SavedComparisonError):
        load_saved(index)
