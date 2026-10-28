"""Synthetic native supplier intake, retry, revision and admission contracts."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from openpyxl import Workbook

from bef_redbird.foundation import Store, Timing
from bef_redbird.foundation import supplier_intake as intake
from tests.test_foundation_reports import SUPPLY


def native(
    path: Path,
    *,
    title: str = "2026 2-3",
    name: str = "Medicine",
    when: datetime | str = datetime(2026, 1, 5),
    wholesaler: str = "W",
) -> Path:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = title
    sheet.append(SUPPLY)
    sheet.append(
        ["000123", name, "tablet", "5 mg", "20 stk", "Holder", "01", "C", wholesaler, when]
    )
    book.save(path)
    book.close()
    return path


@pytest.fixture
def config(tmp_path: Path) -> Path:
    database = tmp_path / "working.duckdb"
    archive = tmp_path / "archive"
    with Store(database, archive):
        pass
    policy = tmp_path / "policy.json"
    registered = tmp_path / "source-review" / "registered-legacy-evidence.json"
    registered.parent.mkdir()
    policy.write_text(
        json.dumps(
            {
                "supply_imports": [],
                "legacy_imports": [],
                "supply_source_preference": [intake.SOURCE],
            }
        )
    )
    registered.write_text(json.dumps({"version": 1, "supply_imports": [], "legacy_imports": []}))
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "database": str(database),
                "archive_root": str(archive),
                "operation_root": str(tmp_path / "operations"),
                "registry_policy_config": str(policy),
            }
        )
    )
    return path


def cfg(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def test_existing_historical_bytes_get_new_true_receipt_without_duplicate_import(
    tmp_path: Path,
    config: Path,
) -> None:
    path = native(tmp_path / "historical.xlsx")
    settings = cfg(config)
    database, archive = Path(settings["database"]), Path(settings["archive_root"])
    historical = datetime(2025, 12, 30, tzinfo=UTC)
    with Store(database, archive) as store:
        delivery = store.register_source(
            path,
            source=intake.SOURCE,
            receipt_key="synthetic-downloader-receipt",
            received=Timing(
                lower=historical,
                upper=historical,
                precision="instant",
                basis="synthetic earlier receipt",
                source_timezone="UTC",
            ),
        )
        summary = store.ingest(
            delivery,
            adapter="supply",
            version="supply-workbook-v1",
            mapping_version="historical-source-local-v1",
            mapping_policy="delivery",
            context=intake.CONTEXT,
        )
    proof = intake.reconcile(database, archive, [path], import_ids=[summary["import_id"]])
    proof_path = tmp_path / "historical-proof.json.gz"
    proof_path.write_bytes(gzip.compress(json.dumps(proof).encode(), mtime=0))
    with Store(database, archive) as store:
        proof_id = store.register_source(
            proof_path,
            source="foundation:independent-source-verification",
            receipt_key=summary["import_id"]
            + ":"
            + hashlib.sha256(proof_path.read_bytes()).hexdigest(),
        )
    pin = {"import_id": summary["import_id"], "verification_delivery_id": proof_id}
    for policy_path in (
        Path(settings["registry_policy_config"]),
        config.parent / "source-review" / "registered-legacy-evidence.json",
    ):
        doc = cfg(policy_path)
        doc["supply_imports"] = [pin]
        policy_path.write_text(json.dumps(doc))
    before = datetime.now(UTC)
    result = intake.ingest_file(config, path)
    assert result["reused_accepted_import"]
    assert result["import_id"] == summary["import_id"]
    assert result["verification_delivery_id"] == proof_id
    assert result["receipt_delivery_id"] != delivery
    assert datetime.fromisoformat(result["received_at_utc"]) >= before
    assert cfg(Path(settings["registry_policy_config"]))["supply_imports"] == [pin]
    with duckdb.connect(settings["database"], read_only=True) as con:
        assert con.execute("SELECT count(*) FROM import_batch").fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM supply_observation").fetchone() == (1,)


def test_native_dates_ignore_misleading_filename_and_mtime(tmp_path: Path, config: Path) -> None:
    path = native(tmp_path / "wrong-20990101.xlsx")
    before = datetime.now(UTC)
    result = intake.ingest_file(config, path)
    assert result["status"] == "admitted"
    assert result["report_from"] == "2026-01-05"
    assert result["report_to_exclusive"] == "2026-01-19"
    assert result["source_rows"] == 1
    assert result["absence_means"] == "unknown"
    assert result["carry_forward"] is False
    assert datetime.fromisoformat(result["received_at_utc"]) >= before
    settings = cfg(config)
    policy = cfg(Path(settings["registry_policy_config"]))
    evidence = cfg(config.parent / "source-review" / "registered-legacy-evidence.json")
    assert (
        policy["supply_imports"]
        == evidence["supply_imports"]
        == [
            {
                "import_id": result["import_id"],
                "verification_delivery_id": result["verification_delivery_id"],
            }
        ]
    )
    with duckdb.connect(settings["database"], read_only=True) as con:
        row = con.execute(
            "SELECT canonical_identifier, observation_date,wholesaler_namespace,"
            "wholesaler_identifier,report_period FROM supply_observation"
        ).fetchone()
        assert row is not None
        assert (row[0], str(row[1]), row[2], row[3], row[4]) == (
            "000123",
            "2026-01-05",
            intake.CONTEXT["wholesaler_namespace"],
            "W",
            None,
        )
        assert con.execute("SELECT count(*) FROM classification_observation").fetchone() == (0,)


def test_retry_preserves_receipt_bytes_proof_rows_and_pin(tmp_path: Path, config: Path) -> None:
    path = native(tmp_path / "report.xlsx")
    first = intake.ingest_file(config, path)
    batch = tmp_path / "operations" / "supplier-intake" / first["content_hash"]
    manifest, proof = (
        (batch / "manifest.json").read_bytes(),
        (batch / "source-verification.json.gz").read_bytes(),
    )
    retry = intake.ingest_file(config, path)
    assert retry["import_id"] == first["import_id"]
    assert retry["received_at_utc"] == first["received_at_utc"]
    assert retry["reused_accepted_import"]
    assert (batch / "manifest.json").read_bytes() == manifest
    assert (batch / "source-verification.json.gz").read_bytes() == proof
    with duckdb.connect(cfg(config)["database"], read_only=True) as con:
        assert con.execute("SELECT count(*) FROM supply_observation").fetchone() == (1,)


def test_changed_same_name_bytes_retained_verified_unpinned(tmp_path: Path, config: Path) -> None:
    path = native(tmp_path / "report.xlsx")
    original = path.read_bytes()
    first = intake.ingest_file(config, path)
    native(path, name="Revised medicine")
    second = intake.ingest_file(config, path)
    assert second["status"] == "verified_revision_review_required"
    assert second["overlapping_import_ids"] == [first["import_id"]]
    assert second["content_hash"] != first["content_hash"]
    assert cfg(Path(cfg(config)["registry_policy_config"]))["supply_imports"] == [
        {
            "import_id": first["import_id"],
            "verification_delivery_id": first["verification_delivery_id"],
        }
    ]
    kept = (
        tmp_path / "operations" / "supplier-intake" / first["content_hash"] / "original" / path.name
    )
    assert kept.read_bytes() == original
    assert intake.ingest_file(config, path)["status"] == "verified_revision_review_required"


@pytest.mark.parametrize(
    "title,when,error",
    [
        ("Synthetic", datetime(2026, 1, 5), "supplier_report_window_requires_review"),
        ("2026 2-4", datetime(2026, 1, 5), "supplier_report_window_requires_review"),
        ("2026 2-3", datetime(2026, 1, 19), "supplier_effective_date_outside_native_report_window"),
        ("2026 2-3", "invalid", "supplier_cells_require_review"),
        ("2026 2-3", "=DATE(2026,1,5)", "supplier_cells_require_review"),
    ],
)
def test_unsupported_content_retained_without_database_admission(
    tmp_path: Path,
    config: Path,
    title: str,
    when: datetime | str,
    error: str,
) -> None:
    path = native(tmp_path / "report.xlsx", title=title, when=when)
    with pytest.raises(ValueError, match=error):
        intake.ingest_file(config, path)
    assert list((tmp_path / "operations" / "supplier-intake").glob("*/original/report.xlsx"))
    with duckdb.connect(cfg(config)["database"], read_only=True) as con:
        assert con.execute("SELECT count(*) FROM import_batch").fetchone() == (0,)
    assert not cfg(Path(cfg(config)["registry_policy_config"]))["supply_imports"]


def test_later_fortnight_is_admitted_without_carry_or_replacement(
    tmp_path: Path, config: Path
) -> None:
    first = intake.ingest_file(config, native(tmp_path / "first.xlsx"))
    second = intake.ingest_file(
        config,
        native(
            tmp_path / "later.xlsx",
            title="2026 4-5",
            when=datetime(2026, 1, 19),
            wholesaler="Other",
        ),
    )
    assert second["status"] == "admitted"
    assert second["import_id"] != first["import_id"]
    assert not second["overlapping_import_ids"]
    assert len(cfg(Path(cfg(config)["registry_policy_config"]))["supply_imports"]) == 2
    assert intake.ingest_file(config, tmp_path / "first.xlsx")["status"] == "admitted"
    assert len(cfg(Path(cfg(config)["registry_policy_config"]))["supply_imports"]) == 2


def test_independent_failure_never_pins(tmp_path: Path, config: Path, monkeypatch: Any) -> None:
    path = native(tmp_path / "report.xlsx")
    monkeypatch.setattr(intake, "reconcile", lambda *a, **k: {"complete": False})
    with pytest.raises(ValueError, match="supplier_independent_verification_failed"):
        intake.ingest_file(config, path)
    assert not cfg(Path(cfg(config)["registry_policy_config"]))["supply_imports"]


def test_cli_unsupported_source_has_guidance_and_existing_status(
    tmp_path: Path,
    config: Path,
    capsys: Any,
) -> None:
    private = tmp_path / "private-filename.xlsx"
    private.write_text("unsupported schema")
    assert intake.main(["--config", str(config), str(private)]) == 1
    output = capsys.readouterr().out
    error = json.loads(output)
    assert error["status"] == "failed"
    assert error["error_code"] == "supplier_schema_unsupported"
    assert "native layouts" in error["guidance"]
    assert Path(error["status_path"]).is_file()
    assert "private-filename" not in output


@pytest.mark.parametrize("missing", ["config", "report"])
def test_cli_missing_paths_get_actionable_codes_before_a_batch_exists(
    tmp_path: Path,
    config: Path,
    capsys: Any,
    missing: str,
) -> None:
    report = native(tmp_path / "private-report.xlsx")
    selected_config = tmp_path / "absent-config.json" if missing == "config" else config
    selected_report = tmp_path / "absent-report.xlsx" if missing == "report" else report
    assert intake.main(["--config", str(selected_config), str(selected_report)]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error_code"] == (
        "supplier_configuration_not_found" if missing == "config" else "supplier_input_not_found"
    )
    assert error["guidance"]
    assert "status_path" not in error
    assert str(tmp_path) not in json.dumps(error)


def test_cli_corrupt_workbook_reports_download_guidance(
    tmp_path: Path,
    config: Path,
    capsys: Any,
) -> None:
    report = tmp_path / "private-corrupt.xlsx"
    report.write_bytes(b"PKbroken archive")
    assert intake.main(["--config", str(config), str(report)]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error_code"] == "supplier_workbook_unreadable"
    assert "Download" in error["guidance"]
    assert Path(error["status_path"]).is_file()
    assert report.name not in json.dumps(error)


def test_two_file_pin_interruption_repairs_from_durable_intent(
    tmp_path: Path,
    config: Path,
    monkeypatch: Any,
) -> None:
    original = intake._json
    interrupted = False

    def fail_once(path: Path, value: dict[str, Any], **kwargs: Any) -> None:
        nonlocal interrupted
        if path.name == "registered-legacy-evidence.json" and not interrupted:
            interrupted = True
            raise OSError("synthetic interrupted write")
        original(path, value, **kwargs)

    monkeypatch.setattr(intake, "_json", fail_once)
    path = native(tmp_path / "report.xlsx")
    with pytest.raises(OSError, match="synthetic interrupted write"):
        intake.ingest_file(config, path)
    result = intake.ingest_file(config, path)
    assert result["status"] == "admitted"
    assert (
        cfg(Path(cfg(config)["registry_policy_config"]))["supply_imports"]
        == cfg(config.parent / "source-review" / "registered-legacy-evidence.json")[
            "supply_imports"
        ]
    )


def publication_fixture(config: Path, pins: list[dict[str, str]]) -> Path:
    settings = cfg(config)
    operation = Path(settings["operation_root"])
    operation.mkdir(exist_ok=True)
    snapshot = operation / "published-configuration.json"
    snapshot.write_text(json.dumps({"supply_imports": pins}))
    (operation / "publication-state.json").write_text(
        json.dumps(
            {
                "configuration_path": str(snapshot),
                "result": {"dataset_id": "synthetic-dataset"},
            }
        )
    )
    return snapshot


def test_publication_skips_same_historical_pin_set_in_any_order(config: Path) -> None:
    pins = [
        {"import_id": "one", "verification_delivery_id": "proof-one"},
        {"import_id": "two", "verification_delivery_id": "proof-two"},
    ]
    policy = Path(cfg(config)["registry_policy_config"])
    doc = cfg(policy)
    doc["supply_imports"] = pins
    policy.write_text(json.dumps(doc))
    publication_fixture(config, list(reversed(pins)))

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unchanged supplier publication must not run")

    assert intake.publish_pending_supply(config, runner=unexpected) == {
        "status": "already_current",
        "publication_started": False,
    }


def test_cli_known_report_saves_already_current_without_publication(
    tmp_path: Path,
    config: Path,
    capsys: Any,
) -> None:
    path = native(tmp_path / "report.xlsx")
    first = intake.ingest_file(config, path)
    publication_fixture(config, cfg(Path(cfg(config)["registry_policy_config"]))["supply_imports"])
    assert intake.main(["--config", str(config), "--publish", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["publication_status"] == "already_current"
    assert result["publication"]["publication_started"] is False
    status = cfg(
        tmp_path / "operations" / "supplier-intake" / first["content_hash"] / "status.json"
    )
    assert status["publication_status"] == "already_current"


def test_changed_supplier_pins_publish_then_confirm_actual_configuration(config: Path) -> None:
    snapshot = publication_fixture(config, [])
    policy = Path(cfg(config)["registry_policy_config"])
    doc = cfg(policy)
    doc["supply_imports"] = [{"import_id": "one", "verification_delivery_id": "proof"}]
    policy.write_text(json.dumps(doc))
    calls = []

    def publish(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert command[1:] == [
            "-m",
            "bef_redbird.foundation.ftp_maintenance",
            "resume",
            "--config",
            str(config),
        ]
        snapshot.write_text(json.dumps(doc))
        return subprocess.CompletedProcess(command, 0, "synthetic publication", "")

    result = intake.publish_pending_supply(config, runner=publish)
    assert len(calls) == 1
    assert result == {
        "status": "published",
        "publication_started": True,
        "dataset_id": "synthetic-dataset",
    }
    assert intake.publish_pending_supply(config, runner=publish)["status"] == "already_current"
    assert len(calls) == 1


@pytest.mark.parametrize("returncode", [0, 1])
def test_failed_or_noop_publication_does_not_claim_admitted_pins(
    config: Path,
    returncode: int,
) -> None:
    publication_fixture(config, [])
    policy = Path(cfg(config)["registry_policy_config"])
    doc = cfg(policy)
    doc["supply_imports"] = [{"import_id": "one", "verification_delivery_id": "proof"}]
    policy.write_text(json.dumps(doc))

    def failed(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, "", "synthetic failure")

    with pytest.raises(intake.FoundationError, match="supplier_publication"):
        intake.publish_pending_supply(config, runner=failed)


def test_revision_cli_never_runs_publication(config: Path, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(
        intake,
        "ingest_file",
        lambda *a: {
            "status": "verified_revision_review_required",
        },
    )

    def unexpected(*args: Any) -> Any:
        raise AssertionError("unresolved revision must not publish")

    monkeypatch.setattr(intake, "publish_pending_supply", unexpected)
    source = config.parent / "synthetic.xls"
    source.write_bytes(b"synthetic report handled by mock")
    assert intake.main(["--config", str(config), "--publish", str(source)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "verified_revision_review_required"
