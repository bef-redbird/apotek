from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from bef_redbird.research.e6_result_view import SavedEvidenceError, load_summary, table_frame


def save(path: Path, value: dict) -> dict:
    path.write_text(json.dumps(value), encoding="utf-8")
    return reference(path)


def reference(path: Path) -> dict:
    return {
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def fixture(tmp_path: Path) -> Path:
    pd.DataFrame(
        [
            {"method": "huber", "information": "current", "rmse_dkk": 12.5},
            {"method": "rmse", "information": "advance", "rmse_dkk": 11.0},
        ]
    ).to_parquet(tmp_path / "comparison.parquet")
    summary = {
        "questions": [
            {
                "id": section,
                "title": title,
                "answer": "The result is mixed; no policy was promoted.",
                "status": "research_complete",
                "comparison": [
                    {
                        "workload": "natural",
                        "price_exposure_ore": 125,
                        "method": "original_e4_current",
                    }
                ],
                "limitations": ["Unknown outcomes remain unknown."],
                "practical_meaning": "A higher price exposure does not measure pharmacy profit.",
            }
            for section, title in [
                ("A", "Does current-only selection help?"),
                ("B", "What changed with actual Cito codes?"),
                ("C", "Did robust amount forecasts improve?"),
                ("D", "Does older training history help?"),
            ]
        ],
        "sections": {
            "B": [{"case": "actual"}, {"case": "corrected"}],
            "C": reference(tmp_path / "comparison.parquet"),
            "D": [{"training_years": 3}, {"training_years": 6}, {"training_years": "full"}],
        },
        "exposure": "Exposed retrospective history, with prospective outcomes still pending.",
        "resources": {"new_fits": 0, "reused_fits": 4},
    }
    pin = save(tmp_path / "summary.json", summary)
    save(tmp_path / "index.json", {"version": "e6-index-v1", "artifacts": {"summary": pin}})
    return tmp_path / "index.json"


def app(path: Path | None) -> AppTest:
    argument = "None" if path is None else f"Path({str(path)!r})"
    return AppTest.from_string(
        "from pathlib import Path\nfrom bef_redbird.research.e6_dashboard import main\n"
        f"main({argument})"
    ).run()


def test_questions_explain_mixed_results_and_separate_comparisons(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    value = app(path)
    assert not value.exception and not value.error
    assert len(value.subheader) == 4
    assert all(not item.proto.expanded for item in value.expander)
    assert "retrospective" in value.warning[0].value
    tables = [item.value for item in value.dataframe]
    assert any("Natural selection queue" in frame.to_string() for frame in tables)
    assert any(
        "Original E4 current-only probability reference" in frame.to_string() for frame in tables
    )
    assert any("matched workload" in item.value for item in value.caption)
    cito = next(frame for frame in tables if "Case" in frame)
    assert cito.Case.tolist() == [
        "Actual reported Cito codes · retrospective",
        "Corrected official codes · retrospective counterfactual",
    ]
    amounts = next(frame for frame in tables if any("Large-error sensitive" in c for c in frame))
    assert amounts["Forecast method"].tolist() == [
        "Robust amount forecast (Huber)",
        "Mean-oriented amount forecast",
    ]
    assert "lower is better" in amounts.columns[-1]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_missing_incompatible_and_unfinished_results(tmp_path: Path) -> None:
    assert not app(None).exception
    assert "Choose a saved E6" in app(None).info[0].value
    missing = app(tmp_path / "absent.json")
    assert not missing.exception and missing.error
    index = tmp_path / "index.json"
    save(index, {"version": "e5-index-v1"})
    assert "Incompatible" in app(index).error[0].value
    pin = save(tmp_path / "summary.json", {"questions": [], "sections": {"A": []}})
    save(index, {"version": "e6-index-v1", "summary": pin})
    empty = app(index)
    assert not empty.exception and not empty.error
    assert any("not evidence of success" in item.value for item in empty.info)
    assert any("No saved comparison" in item.value for item in empty.info)


@pytest.mark.parametrize("artifact", ["summary.json", "comparison.parquet"])
def test_corrupt_pin_fails_before_showing_comparisons(tmp_path: Path, artifact: str) -> None:
    path = fixture(tmp_path)
    (tmp_path / artifact).write_bytes(b"corrupt")
    value = app(path)
    assert not value.exception and "checksum" in value.error[0].value
    assert not value.subheader and not value.dataframe


def test_unsupported_model_and_network_references_are_rejected(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    (tmp_path / "model.cbm").write_bytes(b"must never execute")
    summary["sections"]["C"] = reference(tmp_path / "model.cbm")
    pin = save(tmp_path / "summary.json", summary)
    save(path, {"version": "e6-index-v1", "summary": pin})
    with pytest.raises(SavedEvidenceError, match="JSON or Parquet"):
        load_summary(path)
    summary["sections"]["C"] = {"path": "https://example.invalid/model", "sha256": "0"}
    pin = save(tmp_path / "summary.json", summary)
    save(path, {"version": "e6-index-v1", "summary": pin})
    with pytest.raises(SavedEvidenceError, match="Only local"):
        load_summary(path)


def test_missing_comparison_and_exact_money_presentation(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    summary["questions"][0]["comparison"] = None
    pin = save(tmp_path / "summary.json", summary)
    save(path, {"version": "e6-index-v1", "summary": pin})
    value = app(path)
    assert not value.error and not value.exception
    assert any("No saved comparison" in item.value for item in value.info)
    original = [{"price_exposure_ore": 125, "unknown_selected": 2}]
    frame = table_frame(original)
    assert frame["Price exposure · DKK"].tolist() == [1.25]
    assert frame["Selected with unknown outcomes"].tolist() == [2]
    assert original[0]["price_exposure_ore"] == 125


def test_history_direction_and_amount_tables_preserve_rows_without_mixed_empty_metrics(
    tmp_path: Path,
) -> None:
    path = fixture(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    summary["sections"]["D"] = {
        "records": [
            {"Method": "Three recent years", "Brier probability error ↓": 0.2},
            {"Method": "Six recent years", "Mean absolute error DKK/pack ↓": 5.0},
            {"Method": "Unscored history", "Reason": "Fit unavailable"},
        ]
    }
    pin = save(tmp_path / "summary.json", summary)
    save(path, {"version": "e6-index-v1", "summary": pin})
    value = app(path)
    assert not value.exception and not value.error
    direction = next(
        item.value for item in value.dataframe if "Brier probability error ↓" in item.value
    )
    amount = next(
        item.value for item in value.dataframe if "Mean absolute error DKK/pack ↓" in item.value
    )
    assert direction.Method.tolist() == ["Three recent years"]
    assert amount.Method.tolist() == ["Six recent years"]
    assert "Mean absolute error DKK/pack ↓" not in direction
    assert "Brier probability error ↓" not in amount
    assert any("Fit unavailable" in item.value.to_string() for item in value.dataframe)
