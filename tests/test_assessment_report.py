"""The portable report preserves the frozen queue and explicit missingness."""

from pathlib import Path
from unicodedata import normalize

import pandas as pd
import pymupdf
import pytest

from bef_redbird.assessment.report import export_pdf


def sample() -> tuple[dict, pd.DataFrame]:
    manifest = {
        "origin": "2026-10-12",
        "target": "2026-10-26",
        "mode": "hybrid",
        "assessment_id": "a" * 64,
        "completed_utc": "2026-10-28T15:13:20+00:00",
        "binding": {"protocol": {"sha256": "b" * 64}},
        "files": {"predictions": {"path": "C:/Private/do-not-render.parquet"}},
    }
    frame = pd.DataFrame(
        {
            "package_number": [f"{number:06d}" for number in range(1, 62)],
            "rank": range(1, 62),
            "selected": [True] * 60 + [False],
            "aip_ore": [12500] * 61,
            "p_rise": [0.67] * 61,
            "p_fall": [0.04] * 61,
            "amount_3y_next_price_dkk": [137.5] * 61,
            "amount_3y_delta_dkk": [12.5] * 61,
            "outcome_status": ["observed", "observed", "observed"] + ["pending"] * 58,
            "actual_delta_dkk": [25.0, 0.0, -12.0] + [999.0] * 58,
            "provenance": ["C:/Private/do-not-render.json"] * 61,
        }
    ).sample(frac=1, random_state=7)
    return manifest, frame


def test_saved_pdf_uses_prediction_rank_and_omits_private_metadata(tmp_path: Path) -> None:
    manifest, frame = sample()
    original = frame.copy(deep=True)
    path = export_pdf([(manifest, frame)], tmp_path / "review.pdf")
    pd.testing.assert_frame_equal(frame, original)
    with pymupdf.open(path) as document:
        text = "\n".join(page.get_text() for page in document)
        assert len(document) == 4
        assert "Med periodens Cito" in text
        assert "12.10.2026 til 26.10.2026" in text
        assert "50 viste kandidater. 3 observerede udfald" in text
        assert "1 stigninger, 1 uændrede, 1 fald" in text
        assert "000001" in text and "000050" in text
        assert "000051" not in text and "000061" not in text
        assert text.index("000001") < text.index("000002") < text.index("000050")
        assert "999,00" not in text
        assert "C:/Private" not in text and "do-not-render" not in text
        assert "137,50" in text and "67,0%" in text
        assert "1 / 4" in text and "4 / 4" in text
        assert "a" * 64 in text and "b" * 64 in text
        assert "2026-10-28T15:13:20Z" in text
        assert "Der findes ikke et valideret prisinterval" in normalize(
            "NFKC", " ".join(text.split())
        )
        assert "1. juli 2026" in text and "1. september 2026" in text
        chart_text = document[0].get_text()
        assert "Skøn og udfald" in chart_text
        assert "3 observerede af 50" in chart_text
        assert "absolut beløbsfejl: 16,50 kr." in chart_text
        assert "skøn minus udfald: 8,17 kr." in chart_text
        assert "999" not in chart_text
        assert len([item for item in document[0].get_drawings() if item["fill"]]) == 3
        for page in document:
            # Full-size output must remain on page and render successfully.
            assert page.get_pixmap().width == page.rect.width
            for word in page.get_text("words"):
                assert 0 <= word[0] < word[2] <= page.rect.width
                assert 0 <= word[1] < word[3] <= page.rect.height


def test_three_assessments_pending_empty_and_existing_destination(tmp_path: Path) -> None:
    manifest, frame = sample()
    pending = frame.drop(columns=["outcome_status", "actual_delta_dkk"])
    empty = frame.assign(selected=False)
    path = export_pdf(
        [(dict(manifest, mode="current"), pending), (manifest, frame), (manifest, empty)],
        tmp_path / "three.pdf",
    )
    with pymupdf.open(path) as document:
        assert len(document) == 7
        text = "\n".join(page.get_text() for page in document)
        assert "Uden Cito" in text and "Udfald afventer" in text
        assert "Ingen kandidater opfylder udvælgelsen" in text
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        export_pdf([(manifest, empty)], path)
    assert path.read_bytes() == original


def test_invalid_rank_or_metadata_never_creates_report(tmp_path: Path) -> None:
    manifest, frame = sample()
    destination = tmp_path / "invalid.pdf"
    with pytest.raises(ValueError, match="invalid_selected_ranking"):
        export_pdf([(manifest, frame.assign(rank=1))], destination)
    with pytest.raises(ValueError):
        export_pdf([(dict(manifest, origin="C:/Private/value"), frame)], destination)
    with pytest.raises(ValueError, match="no_assessments"):
        export_pdf([], destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "metadata",
    [
        {"assessment_id": "C:/Private/path"},
        {"completed_utc": "2026-10-28T15:13:20"},
        {"completed_utc": "arbitrary private value"},
        {"binding": {"protocol": {"sha256": "C:/Private/model"}}},
    ],
)
def test_metadata_must_be_validated_before_export(tmp_path: Path, metadata: dict) -> None:
    manifest, frame = sample()
    with pytest.raises(ValueError):
        export_pdf([(manifest | metadata, frame)], tmp_path / "invalid.pdf")
    assert not (tmp_path / "invalid.pdf").exists()


def test_fixed_landscape_cells_keep_realistic_numeric_extremes_on_every_page(
    tmp_path: Path,
) -> None:
    manifest, frame = sample()
    frame["aip_ore"] = [1325, 166889, 241636, 999999999, 1] * 12 + [1]
    frame["amount_3y_next_price_dkk"] = 9_999_999.99
    frame["amount_3y_delta_dkk"] = -9_999_999.99
    frame["actual_delta_dkk"] = 9_999_999.99
    frame["outcome_status"] = "identity_discontinuity_or_ambiguity"
    path = export_pdf([(manifest, frame)], tmp_path / "wide.pdf")
    with pymupdf.open(path) as document:
        tables = [page for page in document if page.rect.width == 842]
        assert len(tables) == 2
        all_text = "".join(page.get_text() for page in tables)
        for number in range(1, 51):
            assert all_text.count(f"{number:06d}") == 1
        for page in tables:
            text = page.get_text()
            assert "Med periodens Cito" in text
            assert "Faktisk ændring" in text and "Udfald" in text
            assert "9.999.999,99" in text and "-9.999.999,99" in text
            assert "Identitet uafklaret" in text
            assert "Vurdering aaaaaaaaaaaaaaaa" in text
            assert "Gemt 2026-10-28T15:13:20Z" in text
            for word in page.get_text("words"):
                assert 29 <= word[0] < word[2] <= 813
                assert 10 <= word[1] < word[3] <= 590
            assert page.get_pixmap().samples == page.get_pixmap().samples


@pytest.mark.parametrize("predicted,expected_x", [(10.0, 242.333333), (-40.0, 204.5)])
def test_scatter_axes_have_independent_currency_margins(
    tmp_path: Path, predicted: float, expected_x: float
) -> None:
    manifest, frame = sample()
    frame["amount_3y_delta_dkk"] = predicted
    frame.loc[frame.package_number.eq("000001"), "actual_delta_dkk"] = 1000.0
    path = export_pdf([(manifest, frame)], tmp_path / "axes.pdf")
    with pymupdf.open(path) as document:
        chart = document[0]
        circles = [item for item in chart.get_drawings() if item["fill"]]
        assert len(circles) == 3
        # Positive predictions span [0, 30]; negative ones [-60, 20]. The
        # observed 1000 DKK tail must never expand the horizontal range.
        assert [(item["rect"].x0 + item["rect"].x1) / 2 for item in circles] == pytest.approx(
            [expected_x] * 3, abs=0.001
        )
        # Actual outcomes span [-22, 1020], including the full negative value
        # and the 1000 DKK outlier with the requested 10/20 DKK margins.
        topmost = min((item["rect"].y0 + item["rect"].y1) / 2 for item in circles)
        assert topmost == pytest.approx(225 + 20 / 1042 * 420, abs=0.001)
        text = chart.get_text()
        assert "1.020,00" in text and "-22,00" in text
        assert "30,00" in text if predicted > 0 else "-60,00" in text
        # Equality is clipped to both independently scaled axes, rather than
        # a decorative corner-to-corner diagonal implying equal screen scales.
        equality = next(item for item in chart.get_drawings() if item["dashes"] != "[] 0")
        assert equality["rect"].height < 33
