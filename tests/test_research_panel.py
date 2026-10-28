"""Conservative cohort/identity boundaries, independent of future outcomes."""

import csv
import gzip
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation.ingestion import ingest
from bef_redbird.foundation.source_audit import reconcile
from bef_redbird.research.panel import (
    IDENTITY_POLICY,
    POLICY,
    eligibility,
    episode,
    file_hash,
    freeze_panel,
)
from tests.test_foundation_lms_ftp import COVERAGE, delivery_bytes, members


def identity(**changes: str) -> dict[str, str]:
    return {
        "created_date": "20240101",
        "exit_date": "        ",
        "dispensing_code": "B    ",
        **changes,
    }


def test_positive_price_is_not_enough_for_pharmacy_cohort() -> None:
    assert eligibility(100, identity(), "2025-01-06") == ""
    assert (
        eligibility(100, identity(dispensing_code="BEGR "), "2025-01-06")
        == "hospital_scope_excluded"
    )
    for code in ["HX", "HX18", "HF", "BP", "PRMIX", "HP", "HV"]:
        assert (
            eligibility(100, identity(dispensing_code=code), "2025-01-06")
            == "nonfixed_price_context"
        )
    assert eligibility(0, identity(), "2025-01-06") == "nonpositive_price"
    assert eligibility(None, identity(), "2025-01-06") == "invalid_price"
    assert (
        eligibility(100, identity(dispensing_code="NEW"), "2025-01-06")
        == "unsupported_dispensing_context"
    )


def test_lifecycle_does_not_infer_free_price_or_future_creation() -> None:
    assert eligibility(100, identity(exit_date="20241231"), "2025-01-06") == "recorded_exit"
    assert (
        eligibility(100, identity(created_date="20250120"), "2025-01-06")
        == "creation_unestablished_at_period"
    )
    assert (
        eligibility(100, identity(created_date="        "), "2025-01-06")
        == "creation_unestablished_at_period"
    )
    assert (
        eligibility(100, identity(created_date="20241301"), "2025-01-06")
        == "invalid_lifecycle_date"
    )


def test_continuity_requires_adjacent_eligible_same_identity() -> None:
    token = ("28100000001", "20240101")
    first, _ = episode("000001", token, 0, True, None)
    previous = {"period_index": 0, "token": token, "eligible": True, "episode_id": first}
    assert episode("000001", token, 1, True, previous) == (first, "continued")
    assert episode("000001", token, 2, True, previous)[1] == "gap_reentry"
    assert episode("000001", (token[0], "20250120"), 1, True, previous)[1] == "identity_change"
    assert episode("000001", token, 1, False, previous)[0] != first
    previous["eligible"] = False
    assert episode("000001", token, 1, True, previous)[1] == "eligibility_boundary"


def prepare(
    tmp_path: Path, *, invalid_first: bool = False
) -> tuple[Path, Path, Path, list[dict[str, Any]]]:
    """Native invented positive/zero/reactivation history, audited independently."""
    database, archive = tmp_path / "foundation.duckdb", tmp_path / "originals"
    evidence = tmp_path / "invented-context.txt"
    evidence.write_text("Invented test context; no real source claim.", "utf-8")
    with Store(database, archive) as store:
        evidence_id = store.register_source(evidence, source="synthetic", receipt_key="basis")
    profile = {
        "version": 1,
        "price_type": "aip",
        "price_role": "effective_price",
        "currency": "DKK",
        "price_basis": "per_package",
        "tax_basis": "excluded",
        "fee_basis": "excluded",
        "effective_period_start_from": "2026-10-12",
        "effective_period_start_through": "2026-11-09",
        "evidence_delivery_id": evidence_id,
        "evidence_sha256": file_hash(evidence),
        "evidence_locator": "synthetic:definition",
    }
    releases = []
    for index, amount in enumerate([100, 0, 125]):
        start = date(2026, 10, 12) + timedelta(days=14 * index)
        end = start + timedelta(days=14)
        files = members()
        package = bytearray(files["lms02.txt"])
        package[73:78] = b"B    "
        package[103:111] = b"20261012"
        package[119:127] = b"        "
        files["lms02.txt"] = bytes(package)
        prices = bytearray(files["lms03.txt"])
        prices[6:15] = b"XXXXXXXXX" if invalid_first and index == 0 else f"{amount:09}".encode()
        files["lms03.txt"] = bytes(prices)
        native = tmp_path / f"release-{index}.zip"
        native.write_bytes(
            delivery_bytes(
                files,
                release=start.strftime("%Y%m%d").encode(),
                week=f"{start.isocalendar().year}{start.isocalendar().week:02}".encode(),
            )
        )
        with Store(database, archive) as store:
            store.add_period(start, end)
            delivery = store.register_source(
                native,
                source="synthetic-lms",
                receipt_key=f"release-{index}",
                phase="final",
                edition=f"synthetic-{index}",
                current_period=start,
                coverage=COVERAGE,
            )
            result = ingest(
                store,
                delivery,
                adapter="lms-ftp",
                version="lms-ftp-zip-v1",
                mapping_version="synthetic-delivery-v1",
                mapping_policy="delivery",
                context={
                    "schema_vintage": "lms-20260626-final",
                    "release_date": start.isoformat(),
                    "period_end_exclusive": end.isoformat(),
                    "tax_basis": "unknown",
                    "fee_basis": "unknown",
                    "price_basis_override": profile,
                },
            )
        report = reconcile(database, archive, [native], import_ids=[result["import_id"]])
        assert report["complete"]
        proof = tmp_path / f"proof-{index}.json.gz"
        proof.write_bytes(gzip.compress(json.dumps(report).encode(), mtime=0))
        releases.append(
            {
                "period": start.isoformat(),
                "end_exclusive": end.isoformat(),
                "delivery_id": delivery,
                "import_id": result["import_id"],
                "native_sha256": file_hash(native),
                "audit_path": str(proof),
                "audit_sha256": file_hash(proof),
            }
        )
    recipe = tmp_path / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": "e1-lms-selection-v1",
                "status": "source_admission_accepted",
                "population_policy": POLICY,
                "identity_policy": IDENTITY_POLICY,
                "evidence": [{"path": str(evidence), "sha256": file_hash(evidence)}],
                "releases": releases,
            }
        ),
        "utf-8",
    )
    return database, archive, recipe, releases


def test_freeze_keeps_zero_and_cuts_reactivation_with_exact_provenance(tmp_path: Path) -> None:
    database, archive, recipe, _ = prepare(tmp_path)
    output = tmp_path / "frozen"
    path = freeze_panel(database, archive, recipe, output)
    with gzip.open(output / "panel.csv.gz", "rt", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["amount_ore"] for r in rows] == ["100", "0", "125"]
    assert [r["eligible"] for r in rows] == ["true", "false", "true"]
    assert rows[1]["exclusion_reason"] == "nonpositive_price"
    assert rows[0]["identity_episode_id"] != rows[2]["identity_episode_id"]
    assert all(r["canonical_identifier"] == "000007" for r in rows)
    assert [r["lifecycle_age_periods"] for r in rows] == ["0", "", "2"]
    with gzip.open(output / "provenance.jsonl.gz", "rt", encoding="utf-8") as stream:
        proofs = [json.loads(line) for line in stream]
    assert [r["provenance_id"] for r in proofs] == [r["provenance_id"] for r in rows]
    manifest = json.loads(path.read_text("utf-8"))
    assert manifest["price_context"]["tax_basis"] == "excluded"
    assert manifest["availability_mode"] == "reconstructed_research"
    replay = freeze_panel(database, archive, recipe, tmp_path / "replay")
    assert replay.read_bytes() == path.read_bytes()
    with pytest.raises(FileExistsError):
        freeze_panel(database, archive, recipe, output)


def test_stale_independent_proof_blocks_freeze(tmp_path: Path) -> None:
    database, archive, recipe, releases = prepare(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "UPDATE price_observation SET amount_ore=999 WHERE import_id=? AND price_type='aip'",
            [releases[0]["import_id"]],
        )
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="Source proof no longer matches"):
        freeze_panel(database, archive, recipe, output)
    assert not (output / "dataset.json").exists()


def test_corrupt_record_proof_cannot_hide_behind_correct_table_fingerprint(tmp_path: Path) -> None:
    database, archive, recipe, releases = prepare(tmp_path)
    proof_path = Path(releases[0]["audit_path"])
    proof = json.loads(gzip.decompress(proof_path.read_bytes()))
    first = next(iter(proof["observation_hashes"]))
    proof["observation_hashes"][first] = "0" * 64
    proof_path.write_bytes(gzip.compress(json.dumps(proof).encode(), mtime=0))
    configuration = json.loads(recipe.read_text("utf-8"))
    configuration["releases"][0]["audit_sha256"] = file_hash(proof_path)
    recipe.write_text(json.dumps(configuration), "utf-8")
    with pytest.raises(ValueError, match="proof assertion or mapping hashes disagree"):
        freeze_panel(database, archive, recipe, tmp_path / "rejected-proof")


def test_faithfully_audited_invalid_money_is_retained_as_exclusion(tmp_path: Path) -> None:
    database, archive, recipe, _ = prepare(tmp_path, invalid_first=True)
    output = tmp_path / "frozen"
    freeze_panel(database, archive, recipe, output)
    with gzip.open(output / "panel.csv.gz", "rt", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["amount_ore"] == ""
    assert rows[0]["eligible"] == "false"
    assert rows[0]["exclusion_reason"] == "invalid_price"
    assert rows[2]["eligible"] == "true"


def test_concurrent_recipe_change_cannot_publish_inconsistent_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bef_redbird.research.panel as panel

    database, archive, recipe, _ = prepare(tmp_path)
    original = panel.snapshot_imports

    def changed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        recipe.write_bytes(recipe.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(panel, "snapshot_imports", changed)
    output = tmp_path / "rejected-concurrent"
    with pytest.raises(ValueError, match="recipe or producer changed during export"):
        freeze_panel(database, archive, recipe, output)
    assert not (output / "dataset.json").exists()


def test_final_interval_cannot_drift_from_audited_calendar(tmp_path: Path) -> None:
    database, archive, recipe, _ = prepare(tmp_path)
    configuration = json.loads(recipe.read_text("utf-8"))
    configuration["releases"][-1]["end_exclusive"] = "2026-11-24"
    recipe.write_text(json.dumps(configuration), "utf-8")
    with pytest.raises(ValueError, match="interval differs from the verified source calendar"):
        freeze_panel(database, archive, recipe, tmp_path / "rejected-calendar")
