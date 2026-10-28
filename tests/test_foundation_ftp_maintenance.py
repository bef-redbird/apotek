"""The maintenance path admits only checkpointed, independently verified imports."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation import ftp_acquisition as acquisition
from bef_redbird.foundation import ftp_maintenance as maintenance
from bef_redbird.foundation.archive import archive_file
from bef_redbird.foundation.locking import writer_lock
from bef_redbird.foundation.records import ArchiveError, WriterBusy
from tests.test_foundation_lms_ftp import delivery_bytes


def prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    native: bytes | None = None,
    remote: str = "/LMS/20260101/lms.zip",
) -> Path:
    source = tmp_path / "lms.zip"
    source.write_bytes(delivery_bytes() if native is None else native)
    archive = tmp_path / "archive"
    sha, relative, size = archive_file(archive, source)
    operations = tmp_path / "operations"
    operations.mkdir()
    config = acquisition.AcquisitionConfig(
        Path.cwd(),
        archive,
        operations,
        tmp_path / "inventory.json",
    )
    monkeypatch.setattr(acquisition, "load_config", lambda _: config)
    monkeypatch.setattr(
        acquisition,
        "load_inventory",
        lambda _: {"complete": True, "inventory_cutoff_utc": "2026-10-22"},
    )
    config.inventory_path.write_text("{}")
    monkeypatch.setattr(
        acquisition,
        "selected_files",
        lambda _: [{"remote": remote, "size": size, "server_modification": "2026-01-01"}],
    )
    monkeypatch.setattr(acquisition, "acquisition_status", lambda _: {"inventory_complete": True})
    monkeypatch.setattr(acquisition, "acquire", lambda *args, **kwargs: None)
    monkeypatch.setattr(acquisition, "discover", lambda *args, **kwargs: None)
    acquisition.write_json(
        operations / "acquisition-state.json",
        {
            "version": 1,
            "files": {
                remote: {
                    "current_sha256": sha,
                    "inventory_size": size,
                    "inventory_modification": "2026-01-01",
                    "receipts": [
                        {
                            "remote": remote,
                            "sha256": sha,
                            "archive_relative_path": relative,
                            "started_utc": "2026-10-22T10:00:00+00:00",
                            "completed_utc": "2026-10-22T10:01:00+00:00",
                            "server_modification": "2026-01-01",
                        }
                    ],
                }
            },
        },
    )
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"database": str(tmp_path / "db.duckdb"), "archive_root": str(archive)})
    )
    return path


@pytest.mark.parametrize("command", ["update", "resume"])
def test_acquire_only_never_opens_store_or_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    config = prepared(tmp_path, monkeypatch)
    acquired: list[list[str]] = []

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Download-only maintenance reached database or publication")

    monkeypatch.setattr(maintenance, "Store", forbidden)
    monkeypatch.setattr(maintenance, "_publish", forbidden)
    monkeypatch.setattr(
        acquisition, "acquire", lambda _, remotes, **kwargs: acquired.append(remotes)
    )
    assert maintenance.main([command, "--config", str(config), "--acquire-only"]) == 0
    assert acquired == [["/LMS/20260101/lms.zip"]]
    assert not (tmp_path / "db.duckdb").exists()
    assert not (tmp_path / "operations/publication-state.json").exists()
    assert maintenance.status(config)["scheduler"] == (
        "external; check Windows Task Scheduler for installed task state"
    )


def test_maintenance_resume_accepts_equivalent_timestamp_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    config = acquisition.load_config(path)
    retained = acquisition.read_state(config)
    remote = "/LMS/20260101/lms.zip"
    old_stamp, new_stamp = "2014-10-17T10:59:00.000Z", "2014-10-17T10:59:00Z"
    entry = retained["files"][remote]
    entry["inventory_modification"] = old_stamp
    entry["receipts"][0]["server_modification"] = old_stamp
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    item = {"remote": remote, "size": entry["inventory_size"], "server_modification": old_stamp}
    monkeypatch.setattr(acquisition, "selected_files", lambda _: [dict(item)])
    maintenance.update(path, refresh=False)
    before = (config.operation_root / "acquisition-state.json").read_bytes()
    item["server_modification"] = new_stamp

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Equivalent timestamp caused a download or database operation")

    monkeypatch.setattr(acquisition, "acquire", unexpected)
    monkeypatch.setattr(maintenance, "Store", unexpected)
    result = maintenance.update(path, refresh=False, acquire_only=True)
    assert not result["failures"]
    assert (config.operation_root / "acquisition-state.json").read_bytes() == before
    receipt = maintenance._component_receipt(item, retained)
    assert receipt is not None and receipt["server_modification"] == old_stamp


def test_cli_failure_receipt_is_bounded_private_and_preserves_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = prepared(tmp_path, monkeypatch)
    operation = tmp_path / "operations"
    accepted = operation / "publication-state.json"
    accepted.write_bytes(b'{"accepted":"unchanged"}')
    journal = operation / "interpretation-state.json"
    journal.write_bytes(b'{"checkpoint":"unchanged"}')

    def nested_failure(depth: int) -> None:
        if depth:
            nested_failure(depth - 1)
        raise ValueError("sensitive synthetic input must not appear")

    def fail(*args: Any, **kwargs: Any) -> None:
        nested_failure(25)

    monkeypatch.setattr(maintenance, "update", fail)
    for _ in range(2):
        assert maintenance.main(["resume", "--config", str(path)]) == 1
        output = json.loads(capsys.readouterr().out)
        assert output["code"] == "ValueError"
        assert "last-failure.json" in output["detail"]
        receipt = json.loads((operation / "last-failure.json").read_text())
        assert receipt["command"] == "resume"
        assert len(receipt["frames"]) == 16
        assert receipt["frames"][-1]["function"] == "nested_failure"
        assert receipt["frames"][-1]["line"] > 0
        assert "sensitive" not in json.dumps(receipt)
        assert "sensitive" not in json.dumps(output)
    assert len(list(operation.glob("last-failure*"))) == 1
    assert accepted.read_bytes() == b'{"accepted":"unchanged"}'
    assert journal.read_bytes() == b'{"checkpoint":"unchanged"}'


@pytest.mark.parametrize("command", ["status", "dry-run"])
def test_read_only_cli_failure_writes_no_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    path = prepared(tmp_path, monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError("private failure detail")

    monkeypatch.setattr(maintenance, "status", fail)
    assert maintenance.main([command, "--config", str(path)]) == 1
    assert "no private failure receipt" in json.loads(capsys.readouterr().out)["detail"]
    assert not (tmp_path / "operations" / "last-failure.json").exists()


@pytest.mark.parametrize("bad_config", [False, True])
def test_cli_diagnostic_failure_does_not_hide_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_config: bool,
) -> None:
    path = prepared(tmp_path, monkeypatch)

    def fail_update(*args: Any, **kwargs: Any) -> None:
        raise ValueError("original failure")

    def fail_diagnostic(*args: Any, **kwargs: Any) -> None:
        raise OSError("unavailable private receipt")

    monkeypatch.setattr(maintenance, "update", fail_update)
    monkeypatch.setattr(acquisition, "load_config" if bad_config else "write_json", fail_diagnostic)
    assert maintenance.main(["resume", "--config", str(path)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["code"] == "ValueError"
    assert "no private failure receipt" in output["detail"]


def test_auditor_memory_budget_changes_no_import_or_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation import lms_registry_audit

    path = prepared(tmp_path, monkeypatch)
    original_cache = lms_registry_audit._record_cache
    monkeypatch.setattr(lms_registry_audit, "_record_cache", original_cache)
    config = json.loads(path.read_text())
    snapshots = []
    for budget in (32, 128):
        path.write_text(json.dumps({**config, "registry_audit_cache_mib": budget}))
        maintenance.update(path, refresh=False)
        loaded, _ = maintenance.load_maintenance(path)
        snapshots.append(maintenance.read_journal(loaded)["files"])
        assert lms_registry_audit._record_cache.limit_bytes == budget * 1024 * 1024
        assert lms_registry_audit._record_cache.retained_bytes <= budget * 1024 * 1024
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("budget", [True, 31, 513, "128", 128.0])
def test_auditor_memory_budget_is_bounded_before_store_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: Any
) -> None:
    path = prepared(tmp_path, monkeypatch)
    config = json.loads(path.read_text())
    path.write_text(json.dumps({**config, "registry_audit_cache_mib": budget}))
    with pytest.raises(ValueError, match="Registry audit cache"):
        maintenance.update(path, refresh=False)
    assert not Path(config["database"]).exists()


def test_format_upgrade_retries_old_rejection_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(delivery_bytes())) as source,
        zipfile.ZipFile(output, "w") as target,
    ):
        for name in source.namelist():
            data = source.read(name)
            target.writestr(name, data[:-2] if name == "lms02.txt" else data)
    path = prepared(tmp_path, monkeypatch, native=output.getvalue())
    config = acquisition.load_config(path)
    retained = acquisition.read_state(config)
    remote, item = next(iter(retained["files"].items()))
    receipt = item["receipts"][0]
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        did = maintenance._delivery(store, receipt, tmp_path / "lms.zip", "final", "registry")
        rejected = maintenance.ingest_registry(
            store,
            did,
            phase="final",
            context={
                "remote": remote,
                "source_role": "registry",
                "publication_evidence": "unknown",
            },
        )
        assert rejected["reason"] == "native_record_terminator"
        immutable_before = store.get_record("import_batch", rejected["import_id"])
    journal = maintenance.read_journal(config)
    journal["files"][remote + "#" + item["current_sha256"]] = {
        "remote": remote,
        "sha256": item["current_sha256"],
        "action": "registry",
        **rejected,
        "status": "unsupported",
    }
    acquisition.write_json(config.operation_root / "interpretation-state.json", journal)
    assert maintenance.update(path)["dispositions"] == {"independently_verified": 1}
    checkpoint = (config.operation_root / "interpretation-state.json").read_bytes()
    assert maintenance.update(path)["dispositions"] == {"independently_verified": 1}
    assert (config.operation_root / "interpretation-state.json").read_bytes() == checkpoint
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store.get_record("import_batch", rejected["import_id"]) == immutable_before
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == (2,)


def test_independent_failure_rolls_back_new_release_and_retry_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    real_verify = maintenance.verify_registry

    def fail_after_verification(*args: Any, **kwargs: Any) -> dict[str, Any]:
        real_verify(*args, **kwargs)
        raise ValueError("independent-check-failed")

    monkeypatch.setattr(maintenance, "verify_registry", fail_after_verification)
    assert maintenance.update(path)["run_status"] == "failed"
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        for table in (
            "registry_release",
            "registry_member_verification",
            "registry_release_verification",
            "import_batch",
        ):
            assert store._connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (1,)
    monkeypatch.setattr(maintenance, "verify_registry", real_verify)
    assert maintenance.update(path)["dispositions"] == {"independently_verified": 1}


def test_verified_resume_preserves_one_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    first = maintenance.update(path)
    assert first["dispositions"] == {"independently_verified": 1}
    checkpoint = next(
        iter(maintenance.read_journal(acquisition.load_config(path))["files"].values())
    )
    assert checkpoint["independently_verified"] is True
    before = (tmp_path / "operations/interpretation-state.json").read_bytes()
    assert maintenance.update(path) == first
    assert (tmp_path / "operations/interpretation-state.json").read_bytes() == before
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_release_verification"
        ).fetchone() == (1,)


def test_failed_verification_cannot_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    verify = maintenance.verify_registry

    def fail(*args: object) -> None:
        raise ValueError("synthetic independent mismatch")

    monkeypatch.setattr(maintenance, "verify_registry", fail)
    result = maintenance.update(path)
    assert result["run_status"] == "failed"
    journal = json.loads((tmp_path / "operations/interpretation-state.json").read_text())
    assert journal["files"] == {}
    assert result["failures"] == {"/LMS/20260101/lms.zip": "ValueError"}
    monkeypatch.setattr(maintenance, "verify_registry", verify)
    assert maintenance.update(path)["dispositions"] == {"independently_verified": 1}


def test_coordinated_writers_and_transport_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    with writer_lock(tmp_path / "operations/interpretation.writer"), pytest.raises(WriterBusy):
        maintenance.update(path)
    assert maintenance.disposition("/LMS/TEST/LMS36.csv", set())[0] == "retain"
    assert maintenance.disposition("/LMS/20260101/lms03.txt", {"/LMS/20260101"})[0] == "packaged"
    assert maintenance.disposition("/LMS/DOK/exceptions.txt", set())[0] == "DOK"


def additional_sources(path: Path, monkeypatch: pytest.MonkeyPatch, count: int) -> list[str]:
    config = acquisition.load_config(path)
    state = acquisition.read_state(config)
    original = next(iter(state["files"]))
    example = state["files"][original]
    remotes = [original] + [f"/LMS/2026010{index}/lms.zip" for index in range(2, count + 1)]
    for remote in remotes[1:]:
        entry = copy.deepcopy(example)
        entry["receipts"][0]["remote"] = remote
        state["files"][remote] = entry
    acquisition.write_json(config.operation_root / "acquisition-state.json", state)
    size = (config.archive_root / example["receipts"][0]["archive_relative_path"]).stat().st_size
    monkeypatch.setattr(
        acquisition,
        "selected_files",
        lambda _: [
            {"remote": r, "size": size, "server_modification": "2026-01-01"} for r in remotes
        ],
    )
    return remotes


def test_partial_acquisition_continues_other_batches_and_resume_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    remotes = additional_sources(path, monkeypatch, 3)
    config = replace(acquisition.load_config(path), batch_size=1)
    monkeypatch.setattr(acquisition, "load_config", lambda _: config)
    state = acquisition.read_state(config)
    missing = state["files"].pop(remotes[1])
    acquisition.write_json(config.operation_root / "acquisition-state.json", state)
    interpreted = []

    def interpret(*args: Any) -> dict[str, Any]:
        interpreted.append(args[-1]["remote"])
        return {"status": "independently_verified"}

    def acquire(_: Any, selected: list[str], **kwargs: Any) -> None:
        if remotes[1] in selected:
            raise RuntimeError("synthetic bounded transfer failure")

    monkeypatch.setattr(maintenance, "_interpret", interpret)
    monkeypatch.setattr(acquisition, "acquire", acquire)
    result = maintenance.update(path)
    assert result["run_status"] == "failed"
    assert interpreted == [remotes[0], remotes[2]]
    assert result["failures"] == {remotes[1]: "acquisition_failed"}

    def recover(*args: Any, **kwargs: Any) -> None:
        state = acquisition.read_state(config)
        state["files"][remotes[1]] = missing
        acquisition.write_json(config.operation_root / "acquisition-state.json", state)

    monkeypatch.setattr(acquisition, "acquire", recover)
    resumed = maintenance.update(path, refresh=False)
    assert resumed["run_status"] == "complete" and not resumed["failures"]
    assert interpreted == [remotes[0], remotes[2], remotes[1]]


def test_update_discovers_late_source_resume_reuses_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    discoveries = []

    def discover(*args: Any, **kwargs: Any) -> None:
        discoveries.append(kwargs)
        additional_sources(path, monkeypatch, 2)

    monkeypatch.setattr(acquisition, "discover", discover)
    monkeypatch.setattr(
        maintenance, "_interpret", lambda *args: {"status": "independently_verified"}
    )
    assert maintenance.update(path)["dispositions"] == {"independently_verified": 2}
    assert discoveries[0]["refresh"] is True
    maintenance.update(path, refresh=False)
    assert len(discoveries) == 1


def test_recheck_propagates_and_changed_bytes_add_journal_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    config = acquisition.load_config(path)
    monkeypatch.setattr(
        maintenance, "_interpret", lambda *args: {"status": "independently_verified"}
    )
    maintenance.update(path)

    def changed(_: Any, remotes: list[str], **kwargs: Any) -> None:
        assert kwargs["recheck"] is True
        state = acquisition.read_state(config)
        entry = state["files"][remotes[0]]
        entry["current_sha256"] = "f" * 64
        entry["receipts"].append({**entry["receipts"][0], "sha256": "f" * 64})
        acquisition.write_json(config.operation_root / "acquisition-state.json", state)

    monkeypatch.setattr(acquisition, "acquire", changed)
    result = maintenance.update(path, refresh=False, recheck=True)
    assert result["run_status"] == "complete"
    assert len(maintenance.read_journal(config)["files"]) == 2


def test_cancel_is_observed_after_closed_batch_and_explicit_clear_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    remotes = additional_sources(path, monkeypatch, 2)
    config = replace(acquisition.load_config(path), batch_size=1)
    monkeypatch.setattr(acquisition, "load_config", lambda _: config)
    interpreted = []

    def interpret(*args: Any) -> dict[str, Any]:
        interpreted.append(args[-1]["remote"])
        if len(interpreted) == 1:
            maintenance.control(path, cancel=True)
        return {"status": "independently_verified"}

    monkeypatch.setattr(maintenance, "_interpret", interpret)
    assert maintenance.update(path)["run_status"] == "cancelled"
    assert interpreted == remotes[:1]
    assert maintenance.update(path, refresh=False)["run_status"] == "cancelled"
    maintenance.control(path, cancel=False)
    assert maintenance.update(path, refresh=False)["run_status"] == "complete"
    assert interpreted == remotes


def test_publication_requires_configured_terminal_scope_and_no_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation import registry_selection

    path = prepared(tmp_path, monkeypatch)
    selection = tmp_path / "selection.json"
    selection.write_text('{"version":"synthetic-explicit-selection"}')
    settings = json.loads(path.read_text())
    settings["registry_selection_config"] = str(selection)
    path.write_text(json.dumps(settings))
    monkeypatch.setattr(maintenance, "_interpret", lambda *args: {"status": "unsupported"})
    published = []

    def publish(store: Any, config: dict[str, Any]) -> dict[str, Any]:
        published.append(config)
        return {
            "dataset_id": "synthetic-dataset",
            "verified": True,
            "manifest": {"input_count": 1, "legacy_inputs": [{"private_proof": "retained"}]},
        }

    def duplicate_status(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Maintenance must use publish's fully verified result")

    monkeypatch.setattr(registry_selection, "publish", publish)
    monkeypatch.setattr(registry_selection, "status", duplicate_status)
    assert maintenance.update(path)["publication"] is None
    settings["publication_allowed_statuses"] = ["unsupported"]
    path.write_text(json.dumps(settings))
    result = maintenance.update(path, refresh=False)
    assert result["publication"]["result"]["dataset_id"] == "synthetic-dataset"
    assert result["publication"]["result"]["verified"] is True
    assert result["publication"]["result"]["manifest"] == {"input_count": 1}
    saved = json.loads((tmp_path / "operations/publication-state.json").read_text())
    assert saved["result"]["manifest"]["legacy_inputs"] == [{"private_proof": "retained"}]
    assert published == [{"version": "synthetic-explicit-selection"}]
    additional_sources(path, monkeypatch, 2)
    config = acquisition.load_config(path)
    state = acquisition.read_state(config)
    state["files"].pop("/LMS/20260102/lms.zip")
    acquisition.write_json(config.operation_root / "acquisition-state.json", state)
    assert maintenance.update(path, refresh=False)["run_status"] == "failed"
    assert len(published) == 1


def test_reviewed_policy_freezes_generated_configuration_before_atomic_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation import registry_selection

    path = prepared(tmp_path, monkeypatch)
    policy = tmp_path / "policy.json"
    policy.write_text('{"version":"reviewed-test-policy"}')
    settings = json.loads(path.read_text())
    settings["registry_policy_config"] = str(policy)
    path.write_text(json.dumps(settings))
    monkeypatch.setattr(
        maintenance, "_interpret", lambda *args: {"status": "independently_verified"}
    )
    generated = {"version": "synthetic-resolved-selection", "edition": 1}
    monkeypatch.setattr(registry_selection, "build_configuration", lambda *_: dict(generated))
    monkeypatch.setattr(
        registry_selection,
        "publish",
        lambda *_: {"dataset_id": "synthetic-dataset", "verified": True},
    )
    result = maintenance.update(path)
    frozen = Path(result["publication"]["configuration_path"])
    before = frozen.read_bytes()
    assert json.loads(before) == generated
    assert maintenance.update(path, refresh=False)["publication"]["configuration_path"] == str(
        frozen
    )
    pointer = tmp_path / "operations/publication-state.json"
    previous = pointer.read_bytes()
    generated["edition"] = 2

    def invalid(*_: Any, **__: Any) -> None:
        raise ValueError("synthetic persisted-value mismatch")

    monkeypatch.setattr(registry_selection, "publish", invalid)
    with pytest.raises(ValueError, match="persisted-value"):
        maintenance.update(path, refresh=False)
    assert pointer.read_bytes() == previous
    assert frozen.read_bytes() == before


@pytest.mark.parametrize(
    "verdict", [{}, {"verified": False}, {"verified": None}, {"verified": 1}, {"verified": "true"}]
)
def test_publication_pointer_preserved_unless_publish_returns_verified_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: dict[str, Any]
) -> None:
    from bef_redbird.foundation import registry_selection

    path = prepared(tmp_path, monkeypatch)
    selection = tmp_path / "selection.json"
    selection.write_text('{"version":"synthetic-explicit-selection"}')
    settings = json.loads(path.read_text())
    settings["registry_selection_config"] = str(selection)
    path.write_text(json.dumps(settings))
    monkeypatch.setattr(
        maintenance, "_interpret", lambda *args: {"status": "independently_verified"}
    )
    monkeypatch.setattr(
        registry_selection,
        "publish",
        lambda *_: {"dataset_id": "synthetic-dataset", "verified": True},
    )
    maintenance.update(path)
    pointer = tmp_path / "operations/publication-state.json"
    previous = pointer.read_bytes()
    monkeypatch.setattr(
        registry_selection, "publish", lambda *_: {"dataset_id": "next-dataset", **verdict}
    )
    with pytest.raises(ValueError, match="fully verified"):
        maintenance.update(path, refresh=False)
    assert pointer.read_bytes() == previous


def partial_sources(
    path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    directory: str = "/LMS/2008/20080521",
    invalid_system: bool = False,
) -> list[str]:
    from tests.test_foundation_registry_partial import loose_sources

    config = acquisition.load_config(path)
    listed = list(acquisition.selected_files(config))
    retained = acquisition.read_state(config)
    files = loose_sources()
    if invalid_system:
        files[0] = files[0][:47] + b"20080230" + files[0][55:]
    remotes = []
    for number, data in [(-1, b""), *files.items()]:
        name = "lms.zip" if number == -1 else "SYSTEM.TXT" if number == 0 else f"LMS{number:02}.TXT"
        remote = directory + "/" + name
        original = tmp_path / (str(len(listed)) + "-native")
        original.write_bytes(data)
        sha, relative, size = archive_file(config.archive_root, original)
        listed.append({"remote": remote, "size": size, "server_modification": "2008-05-21"})
        retained["files"][remote] = {
            "current_sha256": sha,
            "inventory_size": size,
            "inventory_modification": "2008-05-21",
            "receipts": [
                {
                    "remote": remote,
                    "sha256": sha,
                    "archive_relative_path": relative,
                    "started_utc": "2026-10-22T10:00:00+00:00",
                    "completed_utc": "2026-10-22T10:01:00+00:00",
                    "server_modification": "2008-05-21",
                }
            ],
        }
        remotes.append(remote)
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    monkeypatch.setattr(acquisition, "selected_files", lambda _: listed)
    return remotes


def test_partial_native_maintenance_is_separate_verified_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bef_redbird.foundation.lms_registry_partial import partial_reference_member

    path = prepared(tmp_path, monkeypatch)
    partial_sources(path, tmp_path, monkeypatch)
    first = maintenance.update(path)
    assert first["run_status"] == "complete"
    assert first["partial_registry"]["statuses"] == {"partial_reference_verified": 1}
    config = acquisition.load_config(path)
    journal = maintenance.read_journal(config)
    group = next(iter(journal["partial_groups"].values()))
    assert group["canonical_eligible"] is False and group["independently_verified"] is True
    assert group["expected_components"] == group["available_components"] == 7
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (1,)
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == (2,)
        assert (
            partial_reference_member(store, group["import_id"], 3)["status"] == "empty_unavailable"
        )
        assert partial_reference_member(store, group["import_id"], 3)["rows"] == []
    before = (config.operation_root / "interpretation-state.json").read_bytes()
    assert maintenance.update(path, refresh=False) == first
    assert (config.operation_root / "interpretation-state.json").read_bytes() == before
    assert len(list((config.operation_root / "partial-manifests").glob("*.json"))) == 1


def test_missing_partial_component_waits_then_resumes_without_double_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    remotes = partial_sources(path, tmp_path, monkeypatch)
    config = acquisition.load_config(path)
    missing_remote = next(remote for remote in remotes if remote.endswith("LMS04.TXT"))
    retained = acquisition.read_state(config)
    missing = retained["files"].pop(missing_remote)
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    first = maintenance.update(path)
    assert first["partial_registry"]["statuses"] == {"awaiting_components": 1}
    assert first["partial_registry"]["missing_acquisition_components"] == 1
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)
    retained = acquisition.read_state(config)
    retained["files"][missing_remote] = missing
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    resumed = maintenance.update(path, refresh=False)
    assert resumed["run_status"] == "complete" and not resumed["failures"]
    assert resumed["partial_registry"]["statuses"] == {"partial_reference_verified": 1}
    maintenance.update(path, refresh=False)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (1,)
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == (2,)


def test_bad_partial_group_does_not_starve_other_native_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    partial_sources(path, tmp_path, monkeypatch, invalid_system=True)
    partial_sources(path, tmp_path, monkeypatch, directory="/LMS/2008/20080522")
    result = maintenance.update(path)
    assert result["run_status"] == "failed"
    assert result["partial_registry"]["statuses"] == {"failed": 1, "partial_reference_verified": 1}
    assert result["failures"] == {"partial-native:/LMS/2008/20080521": "PartialRegistryError"}
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (1,)


def test_partial_import_cannot_be_published_as_complete_canonical_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bef_redbird.foundation.registry_selection import (
        NATIVE_PRICE_VALUE_POLICY,
        native_price_value_contract,
    )

    path = prepared(tmp_path, monkeypatch)
    partial_sources(path, tmp_path, monkeypatch)
    maintenance.update(path)
    config = acquisition.load_config(path)
    group = next(iter(maintenance.read_journal(config)["partial_groups"].values()))
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        evidence = tmp_path / "synthetic-semantics.json"
        evidence.write_text(json.dumps({"policy": native_price_value_contract()}))
        delivery = store.register_source(
            evidence, source="synthetic-semantics", receipt_key="partial"
        )
        store.add_period(date(2008, 5, 21), date(2008, 6, 4))
        selection = {
            "version": "registry-canonical-v1",
            "release_import_ids": [group["import_id"]],
            "native_price_value_policy": {
                "version": NATIVE_PRICE_VALUE_POLICY,
                "evidence_delivery_id": delivery,
                "evidence_sha256": store.get_record("source_delivery", delivery)["content_hash"],
                "source_locator": "synthetic:policy",
            },
            "period_bindings": [
                {"period_start": "2008-05-21", "snapshot_date": "2008-05-21", "phase": "final"}
            ],
            "semantic_profiles": [
                {
                    "profile": "lms-native-registry-v1:system-11.0",
                    "effective_from": "2000-01-01",
                    "effective_to": "2030-01-01",
                    "fields": {"1": ["drug_id"]},
                    "complete_snapshot_members": [1],
                    "evidence_delivery_id": delivery,
                    "source_locator": "synthetic:contract",
                    "monetary_context": {},
                }
            ],
        }
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selection))
    settings = json.loads(path.read_text())
    settings["registry_selection_config"] = str(selection_path)
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="Pinned registry release does not exist"):
        maintenance.update(path, refresh=False)
    assert not (config.operation_root / "publication-state.json").exists()
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (
            0,
        )


def test_historical_verified_checkpoint_boolean_is_repaired_from_its_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    maintenance.update(path)
    config = acquisition.load_config(path)
    journal = maintenance.read_journal(config)
    checkpoint = next(iter(journal["files"].values()))
    checkpoint["independently_verified"] = False
    acquisition.write_json(config.operation_root / "interpretation-state.json", journal)
    maintenance.update(path, refresh=False)
    corrected = next(iter(maintenance.read_journal(config)["files"].values()))
    assert corrected["independently_verified"] is True
    assert corrected["verification"] == checkpoint["verification"]


def test_plan_detects_only_unpacked_production_native_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    partial_sources(path, tmp_path, monkeypatch)
    config = acquisition.load_config(path)
    planned = maintenance.plan(config)
    assert sum(item["action"] == "partial_component" for item in planned) == 7
    assert sum(item["action"] == "partial_container" for item in planned) == 1
    listed = [
        item
        for item in acquisition.selected_files(config)
        if item["remote"] != "/LMS/2008/20080521/lms.zip"
    ]
    monkeypatch.setattr(acquisition, "selected_files", lambda _: listed)
    assert sum(item["action"] == "partial_component" for item in maintenance.plan(config)) == 7
    listed.append(
        {"remote": "/LMS/2008/20080521/lms.zip", "size": 100, "server_modification": None}
    )
    assert not maintenance._partial_directories(listed)


def test_corrupt_nonempty_zip_exposes_loose_receipts_on_next_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    directory = "/LMS/2010/20100316"
    remotes = partial_sources(path, tmp_path, monkeypatch, directory=directory)
    config = acquisition.load_config(path)
    listed = acquisition.selected_files(config)
    remote_zip = directory + "/lms.zip"
    corrupt = tmp_path / "corrupt-nonempty.zip"
    corrected_bytes = delivery_bytes()
    corrupt.write_bytes(
        b"!" * len(corrected_bytes)
    )  # Same reported size as a later valid correction.
    sha, relative, size = archive_file(config.archive_root, corrupt)
    next(item for item in listed if item["remote"] == remote_zip)["size"] = size
    retained = acquisition.read_state(config)
    container = retained["files"][remote_zip]
    container.update(current_sha256=sha, inventory_size=size)
    container["receipts"][0].update(sha256=sha, archive_relative_path=relative)
    loose = {remote: retained["files"].pop(remote) for remote in remotes if remote != remote_zip}
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    transfers = []
    monkeypatch.setattr(
        acquisition, "acquire", lambda _, selected, **kwargs: transfers.extend(selected)
    )
    first = maintenance.update(path)
    assert first["run_status"] == "partial"
    assert first["partial_registry"]["statuses"] == {"awaiting_components": 1}
    assert first["partial_registry"]["missing_acquisition_components"] == 7
    assert not set(loose) & set(transfers)  # Exposed after the original pending list.
    journal = maintenance.read_journal(config)
    rejected = journal["files"][remote_zip + "#" + sha]
    assert rejected["status"] == "unsupported" and rejected["reason"] == "invalid_zip"
    rejected_id = rejected["import_id"]
    retained = acquisition.read_state(config)
    retained["files"].update(loose)
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    resumed = maintenance.update(path, refresh=False)
    assert resumed["run_status"] == "complete" and not resumed["failures"]
    assert set(loose) <= set(transfers)
    active = [
        row
        for row in maintenance.read_journal(config)["partial_groups"].values()
        if row["status"] == "partial_reference_verified"
    ]
    assert len(active) == 1
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store.get_record("import_batch", rejected_id)["validation_status"] == "rejected"
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (1,)
        stored = store._connection.execute("SELECT context FROM registry_partial_group").fetchone()
        assert stored is not None
        context = json.loads(stored[0])
        assert context["rejected_containers"][0]["sha256"] == sha
        assert context["rejected_containers"][0]["rejected_import_id"] == rejected_id
        assert "invalid_zip" in context["partial_reason"]
        count = store._connection.execute("SELECT count(*) FROM import_batch").fetchone()
    maintenance.update(path, refresh=False)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == count

    def correct_native_zip(_: Any, selected: list[str], **kwargs: Any) -> None:
        assert kwargs["recheck"] is True
        current = acquisition.read_state(config)
        for remote in selected:
            entry = current["files"][remote]
            receipt = dict(entry["receipts"][-1])
            if remote == remote_zip:
                correction = tmp_path / "corrected-native.zip"
                correction.write_bytes(corrected_bytes)
                good_sha, good_relative, good_size = archive_file(config.archive_root, correction)
                assert good_size == size
                entry["current_sha256"] = good_sha
                receipt.update(sha256=good_sha, archive_relative_path=good_relative)
            entry["receipts"].append(receipt)
        acquisition.write_json(config.operation_root / "acquisition-state.json", current)

    monkeypatch.setattr(acquisition, "acquire", correct_native_zip)
    corrected = maintenance.update(path, refresh=False, recheck=True)
    assert corrected["run_status"] == "complete" and not corrected["failures"]
    assert corrected["partial_registry"]["groups"] == 0
    assert (
        next(item for item in maintenance.plan(config) if item["remote"] == remote_zip)["action"]
        == "registry"
    )
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store.get_record("import_batch", rejected_id)["validation_status"] == "rejected"
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (2,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (1,)


def preliminary_workbooks(path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from tests.test_foundation_reports import STOP, intro_rows, workbook

    config = acquisition.load_config(path)
    listed = list(acquisition.selected_files(config))
    retained = acquisition.read_state(config)
    rows = intro_rows(header_row=6, punctuation=True)
    rows[0] = ["Nye pakninger på nye formuleringer"]
    files = {
        "form2644-1.xlsx": workbook(rows),
        "pak2644-1.xlsx": workbook(intro_rows(header_row=6)),
        "slet2644-1.xlsx": workbook(
            [
                STOP.copy(),
                [
                    "Example",
                    "Medicine",
                    "tablet",
                    "5 mg",
                    "000123",
                    "10 stk",
                    datetime(2026, 1, 5),
                    None,
                ],
            ]
        ),
    }
    remotes = []
    for name, data in files.items():
        original = tmp_path / name
        original.write_bytes(data)
        sha, relative, size = archive_file(config.archive_root, original)
        remote = "/LMS/Foreløbig/" + name
        listed.append({"remote": remote, "size": size, "server_modification": "2026-01-02"})
        retained["files"][remote] = {
            "current_sha256": sha,
            "inventory_size": size,
            "inventory_modification": "2026-01-02",
            "receipts": [
                {
                    "remote": remote,
                    "sha256": sha,
                    "archive_relative_path": relative,
                    "started_utc": "2026-10-22T10:00:00+00:00",
                    "completed_utc": "2026-10-22T10:01:00+00:00",
                    "server_modification": "2026-01-02",
                }
            ],
        }
        remotes.append(remote)
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    monkeypatch.setattr(acquisition, "selected_files", lambda _: listed)
    return remotes


def test_preliminary_workbooks_reuse_existing_adapters_and_native_auditor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    remotes = preliminary_workbooks(path, tmp_path, monkeypatch)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        store.add_period(date(2026, 1, 5), date(2026, 1, 19))
    first = maintenance.update(path)
    assert first["run_status"] == "complete" and not first["failures"]
    config = acquisition.load_config(path)
    journal = maintenance.read_journal(config)
    reports = [row for row in journal["files"].values() if row["remote"] in remotes]
    assert len(reports) == 3
    assert all(
        row["status"] == "independently_verified" and row["independently_verified"] is True
        for row in reports
    )
    assert all(
        row["phase"] == "preliminary" and row["source_role"] == "preliminary_report"
        for row in reports
    )
    assert all(Path(row["verification"]["proof_path"]).is_file() for row in reports)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute("SELECT count(*) FROM price_observation").fetchone() == (
            4,
        )
        assert store._connection.execute(
            "SELECT DISTINCT effective_period,tax_basis,fee_basis FROM price_observation"
        ).fetchall() == [(date(2026, 1, 5), "unknown", "unknown")]
        assert store._connection.execute(
            "SELECT count(*) FROM source_delivery WHERE phase='preliminary'"
        ).fetchone() == (3,)
        assert store._connection.execute(
            "SELECT count(*) FROM lifecycle_event WHERE report_period IS NULL"
        ).fetchone() == (1,)
        count = store._connection.execute("SELECT count(*) FROM import_batch").fetchone()
    maintenance.update(path, refresh=False)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == count


def test_preliminary_introduction_waits_for_evidenced_period_without_inventing_calendar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    preliminary_workbooks(path, tmp_path, monkeypatch)
    first = maintenance.update(path)
    assert first["run_status"] == "partial"
    assert first["dispositions"]["awaiting_evidenced_period"] == 2
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM price_period").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM price_observation").fetchone() == (
            0,
        )
        assert store._connection.execute("SELECT count(*) FROM lifecycle_event").fetchone() == (1,)
        evidence = tmp_path / "calendar-evidence.txt"
        evidence.write_text("Explicit synthetic calendar: 2026-01-05 until 2026-01-19")
        delivery = store.register_source(
            evidence, source="synthetic-calendar", receipt_key="review"
        )
        sha = store.get_record("source_delivery", delivery)["content_hash"]
    reviewed = tmp_path / "calendar-reviewed.json"
    reviewed.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "reviewed-price-calendar",
                "rationale": "Independent synthetic calendar declaration",
                "evidence": [
                    {
                        "delivery_id": delivery,
                        "sha256": sha,
                        "source_locator": "text:synthetic-calendar",
                    }
                ],
                "periods": [
                    {
                        "start": "2026-01-05",
                        "end_exclusive": "2026-01-19",
                        "evidence_delivery_ids": [delivery],
                    }
                ],
            }
        )
    )
    settings = json.loads(path.read_text())
    settings["reviewed_calendar"] = reviewed.name  # Resolve relative to configuration.
    path.write_text(json.dumps(settings))
    resumed = maintenance.update(path, refresh=False)
    assert resumed["run_status"] == "complete"
    assert resumed["reviewed_calendar"]["complete"] is True
    assert resumed["reviewed_calendar"]["last_end_exclusive"] == "2026-01-19"
    assert "awaiting_evidenced_period" not in resumed["dispositions"]
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM price_observation").fetchone() == (
            4,
        )


def test_preliminary_cached_checkpoint_does_not_hide_typed_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    preliminary_workbooks(path, tmp_path, monkeypatch)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        store.add_period(date(2026, 1, 5), date(2026, 1, 19))
    maintenance.update(path)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        store._connection.execute("UPDATE price_observation SET amount_ore=amount_ore+1")
    result = maintenance.update(path, refresh=False)
    assert result["run_status"] == "failed"
    assert len(result["failures"]) == 2
    assert set(result["failures"].values()) == {"preliminary_native_reconciliation_failed"}
    assert result["publication"] is None


def test_new_preliminary_action_interprets_an_existing_raw_only_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = prepared(tmp_path, monkeypatch)
    preliminary_workbooks(path, tmp_path, monkeypatch)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        store.add_period(date(2026, 1, 5), date(2026, 1, 19))
    original = maintenance.disposition

    def earlier(remote: str, zipped: set[str]) -> tuple[str, str]:
        return (
            ("retain", "earlier raw-only checkpoint")
            if remote.endswith(".xlsx")
            else original(remote, zipped)
        )

    monkeypatch.setattr(maintenance, "disposition", earlier)
    maintenance.update(path)
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM price_observation").fetchone() == (
            0,
        )
    monkeypatch.setattr(maintenance, "disposition", original)
    resumed = maintenance.update(path, refresh=False)
    assert resumed["run_status"] == "complete"
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM price_observation").fetchone() == (
            4,
        )


@pytest.mark.parametrize(
    "remote,reason",
    [
        ("/LMS/DOS1/dosering.zip", "clinical dosage XML"),
        ("/LMS/APOLABELTEKST/apolabeltekst.txt", "excludes medicine identity"),
        ("/LMS/APOLABELTEKST/Vejledning.txt", "documentation"),
        ("/LMS/INFO/INFO20260615.TXT", "source schema"),
        ("/LMS/WS_FTP.LOG", "operational transfer log"),
    ],
)
def test_ancillary_retention_has_specific_source_scope(remote: str, reason: str) -> None:
    action, actual = maintenance.disposition(remote, set())
    assert action == "retain" and reason in actual


def count_mismatched_zip() -> bytes:
    from tests.test_foundation_lms_ftp import members

    files = members()
    files["lms09.txt"] = b" " * 60 + b"\r\n"
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(delivery_bytes(files))) as source,
        zipfile.ZipFile(output, "w") as target,
    ):
        for name in source.namelist():
            data = source.read(name)
            if name == "system.txt":
                data = (
                    b"\r\n".join(
                        row[:-5] + b"00000" if row[32:44].strip() == b"lms09.txt" else row
                        for row in data[:-2].split(b"\r\n")
                    )
                    + b"\r\n"
                )
            target.writestr(name, data)
    return output.getvalue()


def test_rejected_zip_partial_members_are_verified_once_without_full_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation.lms_registry_partial import (
        PARTIAL_ZIP_PROFILE,
        partial_reference_member,
    )

    path = prepared(tmp_path, monkeypatch, native=count_mismatched_zip())
    first = maintenance.update(path)
    assert first["run_status"] == "complete" and not first["failures"]
    assert first["dispositions"] == {"independently_verified": 1}
    assert first["partial_registry"]["statuses"] == {"independently_verified": 1}
    config = acquisition.load_config(path)
    journal = maintenance.read_journal(config)
    checkpoint = next(iter(journal["files"].values()))
    assert (
        checkpoint["source_role"] == "registry_partial"
        and checkpoint["canonical_eligible"] is False
    )
    assert checkpoint["action"] == "partial_zip"
    assert checkpoint["partial_interpreter_version"] == PARTIAL_ZIP_PROFILE
    assert checkpoint["format_variant_version"] == maintenance.FIXED_WIDTH_PROFILE
    assert len(checkpoint["prior_interpretation_import_ids"]) == 2
    assert maintenance.plan(config)[0]["action"] == "partial_zip"
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        for rejected in checkpoint["prior_interpretation_import_ids"]:
            assert store.get_record("import_batch", rejected)["validation_status"] == "rejected"
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)
        assert len(partial_reference_member(store, checkpoint["import_id"], 9)["rows"]) == 1
        coverage = next(
            row for row in checkpoint["coverage"]["members"] if row["member_number"] == 9
        )
        assert coverage["declared_records"] == 0 and coverage["available_records"] == 1
        counts = store._connection.execute("SELECT count(*) FROM source_delivery").fetchone()
    maintenance.update(path, refresh=False)
    assert maintenance.read_journal(config) == journal
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert (
            store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == counts
        )


def test_old_structural_rejection_attempts_new_full_variant_before_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch, native=count_mismatched_zip())
    config = acquisition.load_config(path)
    retained = acquisition.read_state(config)
    remote, item = next(iter(retained["files"].items()))
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        delivery = maintenance._delivery(
            store, item["receipts"][0], tmp_path / "lms.zip", "final", "registry"
        )
        rejected = maintenance.ingest_registry(
            store,
            delivery,
            phase="final",
            context={
                "remote": remote,
                "source_role": "registry",
                "publication_evidence": "unknown",
            },
        )
        before = store.get_record("import_batch", rejected["import_id"])
    journal = maintenance.read_journal(config)
    journal["files"][remote + "#" + item["current_sha256"]] = {
        **rejected,
        "action": "registry",
        "status": "unsupported",
        "delivery_id": delivery,
    }
    acquisition.write_json(config.operation_root / "interpretation-state.json", journal)
    assert maintenance._variant_pending(next(iter(journal["files"].values())))
    assert maintenance.update(path)["partial_registry"]["statuses"] == {"independently_verified": 1}
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store.get_record("import_batch", rejected["import_id"]) == before


def test_unknown_system_is_terminal_unsupported_without_partial_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch, native=delivery_bytes(system_change=(2, b"99.0 ")))
    assert maintenance.update(path)["dispositions"] == {"unsupported": 1}
    config = acquisition.load_config(path)
    first = maintenance.read_journal(config)
    assert first.get("partial_groups", {}) == {}
    maintenance.update(path, refresh=False)
    assert maintenance.read_journal(config) == first


def test_preliminary_old_phase_rejection_retries_once_without_invented_control_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation import lms_registry_partial as partial
    from tests.test_foundation_registry_partial import preliminary_zip

    remote = "/LMS/Foreløbig/lms-gros.zip"
    path = prepared(tmp_path, monkeypatch, native=preliminary_zip(), remote=remote)
    config = acquisition.load_config(path)
    receipt = acquisition.read_state(config)["files"][remote]["receipts"][0]
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        delivery = maintenance._delivery(
            store, receipt, tmp_path / "lms.zip", "preliminary", "registry"
        )
        old = maintenance.ingest_registry(
            store,
            delivery,
            phase="preliminary",
            context={
                "remote": remote,
                "source_role": "registry",
                "publication_evidence": "unknown",
            },
        )
        before = store.get_record("import_batch", old["import_id"])
    journal = maintenance.read_journal(config)
    journal["files"][remote + "#" + receipt["sha256"]] = {
        **old,
        "action": "registry",
        "status": "unsupported",
        "delivery_id": delivery,
    }
    acquisition.write_json(config.operation_root / "interpretation-state.json", journal)
    assert maintenance.update(path)["partial_registry"]["statuses"] == {"independently_verified": 1}
    upgraded = maintenance.read_journal(config)
    checkpoint = next(iter(upgraded["files"].values()))
    assert checkpoint["phase"] == "preliminary" and checkpoint["action"] == "partial_zip"
    assert checkpoint["partial_interpreter_version"] == partial.PARTIAL_ZIP_PROFILE
    assert checkpoint["prior_interpretation_import_ids"] == [old["import_id"]]
    assert checkpoint["coverage"]["control_inventory"]["actual_member_count"] == 31
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store.get_record("import_batch", old["import_id"]) == before
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)
        assert partial.partial_reference_member(store, checkpoint["import_id"], 33)["rows"]
        counts = store._connection.execute("SELECT count(*) FROM import_batch").fetchone()
    maintenance.update(path, refresh=False)
    assert maintenance.read_journal(config) == upgraded
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == counts


def test_partial_profile_upgrade_retains_old_group_proofs_and_counts_one_current_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation import lms_registry_partial as partial

    path = prepared(tmp_path, monkeypatch, native=count_mismatched_zip())
    current_parser, current_zip = partial.PARTIAL_PROFILE, partial.PARTIAL_ZIP_PROFILE
    monkeypatch.setattr(partial, "PARTIAL_PROFILE", "lms-native-partial-v2")
    monkeypatch.setattr(partial, "PARTIAL_ZIP_PROFILE", "lms-zip-partial-members-v1")
    monkeypatch.setattr(maintenance, "PARTIAL_ZIP_PROFILE", "lms-zip-partial-members-v1")
    maintenance.update(path)
    config = acquisition.load_config(path)
    old_journal = maintenance.read_journal(config)
    old = next(iter(old_journal["files"].values()))
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        before = store._connection.execute(
            "SELECT * FROM registry_partial_group WHERE import_id=?", [old["import_id"]]
        ).fetchone()
    monkeypatch.setattr(partial, "PARTIAL_PROFILE", current_parser)
    monkeypatch.setattr(partial, "PARTIAL_ZIP_PROFILE", current_zip)
    monkeypatch.setattr(maintenance, "PARTIAL_ZIP_PROFILE", current_zip)
    assert maintenance.update(path, refresh=False)["partial_registry"]["groups"] == 1
    journal = maintenance.read_journal(config)
    new = next(iter(journal["files"].values()))
    assert new["import_id"] != old["import_id"]
    assert new["prior_interpretations"][0]["import_id"] == old["import_id"]
    assert new["prior_interpretations"][0]["evidence_hash"] == old["evidence_hash"]
    assert len(journal["partial_groups"]) == 2
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert (
            store._connection.execute(
                "SELECT * FROM registry_partial_group WHERE import_id=?", [old["import_id"]]
            ).fetchone()
            == before
        )
    maintenance.update(path, refresh=False)
    assert maintenance.read_journal(config) == journal


@pytest.mark.parametrize("unknown_schema", [False, True])
def test_preliminary_uncorroborated_or_unknown_schema_stays_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_schema: bool
) -> None:
    source = delivery_bytes(system_change=(2, b"99.0 ")) if unknown_schema else delivery_bytes()
    path = prepared(tmp_path, monkeypatch, native=source, remote="/LMS/Foreløbig/lms-gros.zip")
    assert maintenance.update(path)["dispositions"] == {"unsupported": 1}
    config = acquisition.load_config(path)
    journal = maintenance.read_journal(config)
    assert journal["partial_groups"] == {}
    maintenance.update(path, refresh=False)
    assert maintenance.read_journal(config) == journal


@pytest.mark.parametrize(
    "directory,is_test",
    [("TEST", True), ("lsc-test", True), ("LATEST", False), ("ARCHIVE-TEST", False)],
)
def test_test_directories_require_exact_path_components(directory: str, is_test: bool) -> None:
    remote = "/LMS/" + directory + "/lms.zip"
    assert maintenance.disposition(remote, set())[0] == ("retain" if is_test else "registry")
    components = [
        {"remote": "/LMS/" + directory + "/system.txt", "size": 12},
        {"remote": "/LMS/" + directory + "/lms01.txt", "size": 12},
    ]
    assert bool(maintenance._partial_directories(components)) is not is_test


@pytest.mark.parametrize("member", ["lms03.txt", "lms04.txt", "lms31.txt", "lms17.txt"])
def test_supported_fixed_framing_is_fully_admitted_before_partial_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    from tests.test_foundation_lms_ftp import members

    files = members()
    if member in {"lms04.txt", "lms31.txt"}:
        files[member] = b" " * (47 if member == "lms04.txt" else 36) + b"\r\n"
    if member == "lms17.txt":
        files[member] = b" " * 55 + b"alpha\r\nbeta".ljust(500) + b"\r\n"
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(delivery_bytes(files))) as source,
        zipfile.ZipFile(output, "w") as target,
    ):
        for name in source.namelist():
            data = source.read(name)
            if name == member and member != "lms17.txt":
                data = data[:-2]
            if name == "system.txt" and member == "lms17.txt":
                data = (
                    b"\r\n".join(
                        row[:-5] + b"00001" if row[32:44].strip() == b"lms17.txt" else row
                        for row in data[:-2].split(b"\r\n")
                    )
                    + b"\r\n"
                )
            target.writestr(name, data)
    path = prepared(tmp_path, monkeypatch, native=output.getvalue())
    result = maintenance.update(path)
    assert result["run_status"] == "complete" and not result["failures"]
    assert result["partial_registry"]["groups"] == 0
    config = acquisition.load_config(path)
    journal = maintenance.read_journal(config)
    entry = next(iter(journal["files"].values()))
    assert entry["format_variant_version"] == maintenance.FIXED_WIDTH_PROFILE
    assert entry["status"] == "independently_verified" and entry["action"] == "registry"
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)
        for old_id in entry["prior_interpretation_import_ids"]:
            assert store.get_record("import_batch", old_id)["validation_status"] == "rejected"
    maintenance.update(path, refresh=False)
    assert maintenance.read_journal(config) == journal


def test_genuinely_truncated_member_retains_other_supported_zip_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_foundation_lms_ftp import members

    files = members()
    files["lms04.txt"] = b"incomplete"
    path = prepared(tmp_path, monkeypatch, native=delivery_bytes(files))
    result = maintenance.update(path)
    assert result["run_status"] == "complete" and not result["failures"]
    config = acquisition.load_config(path)
    entry = next(iter(maintenance.read_journal(config)["files"].values()))
    assert entry["source_role"] == "registry_partial" and entry["canonical_eligible"] is False
    assert entry["reason"] == "native_record_terminator"
    assert entry["full_interpretation_reason"] == "native_record_width"
    assert entry["unsupported_members"][0]["member_number"] == 4
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM registry_lms03").fetchone() == (1,)


def replace_container(path: Path, remote: str, native: bytes) -> None:
    config = acquisition.load_config(path)
    retained = acquisition.read_state(config)
    entry = retained["files"][remote]
    source = path.parent / "replacement.zip"
    source.write_bytes(native)
    sha, relative, size = archive_file(config.archive_root, source)
    receipt = {**entry["receipts"][-1], "sha256": sha, "archive_relative_path": relative}
    entry.update(current_sha256=sha, inventory_size=size)
    entry["receipts"].append(receipt)
    next(item for item in acquisition.selected_files(config) if item["remote"] == remote)[
        "size"
    ] = size
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)


@pytest.mark.parametrize("already_checkpointed", [False, True])
def test_complete_replacement_retires_only_its_stale_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_checkpointed: bool
) -> None:
    path = prepared(tmp_path, monkeypatch)
    directory = "/LMS/2008/20080521"
    partial_sources(path, tmp_path, monkeypatch, invalid_system=True, directory=directory)
    assert maintenance.update(path)["run_status"] == "failed"
    config = acquisition.load_config(path)
    failed = maintenance.read_journal(config)
    failure_key = "partial-native:" + directory
    assert failure_key in failed["failures"]
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        old_imports = store._connection.execute(
            "SELECT * FROM import_batch ORDER BY import_id"
        ).fetchall()
    remote = directory + "/lms.zip"
    replace_container(path, remote, delivery_bytes())
    if already_checkpointed:
        assert maintenance.update(path, refresh=False)["run_status"] == "complete"
        interrupted = maintenance.read_journal(config)
        interrupted["failures"][failure_key] = failed["failures"][failure_key]
        interrupted["failures"]["partial-native:/LMS/unresolved"] = {"code": "unrelated"}
        acquisition.write_json(config.operation_root / "interpretation-state.json", interrupted)

        def unexpected_interpret(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Existing complete checkpoint must not be reimported")

        monkeypatch.setattr(maintenance, "_interpret", unexpected_interpret)
    verified = []
    original = maintenance.verify_registry

    def verify(store: Store, import_id: str, **kwargs: Any) -> dict[str, Any]:
        result = original(store, import_id, **kwargs)
        verified.append(import_id)
        return result

    monkeypatch.setattr(maintenance, "verify_registry", verify)
    result = maintenance.update(path, refresh=False)
    assert result["run_status"] == ("failed" if already_checkpointed else "complete")
    assert result["failures"] == (
        {"partial-native:/LMS/unresolved": "unrelated"} if already_checkpointed else {}
    )
    journal = maintenance.read_journal(config)
    current = acquisition.read_state(config)["files"][remote]["current_sha256"]
    replacement = journal["files"][remote + "#" + current]
    assert verified == [replacement["import_id"]] * (1 if already_checkpointed else 2)
    assert journal["partial_groups"] == failed["partial_groups"]
    assert all(journal["files"][key] == value for key, value in failed["files"].items())
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        imports = store._connection.execute("SELECT * FROM import_batch").fetchall()
        assert all(row in imports for row in old_imports)
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (2,)


@pytest.mark.parametrize("partial_replacement", [False, True])
def test_noncomplete_replacement_keeps_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial_replacement: bool
) -> None:
    path = prepared(tmp_path, monkeypatch)
    directory = "/LMS/2008/20080521"
    partial_sources(path, tmp_path, monkeypatch, invalid_system=True, directory=directory)
    maintenance.update(path)
    config = acquisition.load_config(path)
    failed = maintenance.read_journal(config)
    failure_key = "partial-native:" + directory
    replace_container(
        path,
        directory + "/lms.zip",
        count_mismatched_zip()
        if partial_replacement
        else delivery_bytes(system_change=(2, b"99.0 ")),
    )
    assert maintenance.update(path, refresh=False)["run_status"] == "failed"
    journal = maintenance.read_journal(config)
    assert journal["failures"][failure_key] == failed["failures"][failure_key]
    assert all(
        journal["partial_groups"][key] == value for key, value in failed["partial_groups"].items()
    )


@pytest.mark.parametrize("mismatch", ["receipt", "inventory", "proof", "original"])
def test_partial_failure_retirement_requires_current_receipt_and_independent_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    path = prepared(tmp_path, monkeypatch)
    maintenance.update(path)
    config, settings = maintenance.load_maintenance(path)
    work = maintenance.read_journal(config)
    retained = acquisition.read_state(config)
    items = maintenance.plan(config)
    remote = items[0]["remote"]
    entry = retained["files"][remote]
    checkpoint = work["files"][remote + "#" + entry["current_sha256"]]
    failure_key = "partial-native:" + remote.rsplit("/", 1)[0]
    work["failures"][failure_key] = {"code": "earlier_partial_failure"}
    if mismatch == "receipt":
        entry["receipts"][-1]["server_modification"] = "different edition"
    elif mismatch == "inventory":
        entry["inventory_modification"] = "different edition"
    elif mismatch == "proof":
        checkpoint["verification"]["evidence_hash"] = "0" * 64
    else:
        with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
            store.original_path(checkpoint["delivery_id"]).write_bytes(b"corrupt original")

    def failure(key: str, code: str, error: Exception | None = None) -> None:
        work["failures"][key] = {"code": code}

    maintenance._retire_replaced_partial_failures(
        config, settings, items, retained, work, failure, lambda: False
    )
    assert failure_key in work["failures"]


def test_old_complete_proof_cannot_retire_failure_for_changed_acquisition_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepared(tmp_path, monkeypatch)
    maintenance.update(path)
    config, settings = maintenance.load_maintenance(path)
    work = maintenance.read_journal(config)
    remote = acquisition.selected_files(config)[0]["remote"]
    old = next(iter(work["files"].values()))
    source = io.BytesIO(delivery_bytes())
    with zipfile.ZipFile(source, "a") as archive:
        archive.comment = b"new native transport bytes, same member values"
    replace_container(path, remote, source.getvalue())
    retained = acquisition.read_state(config)
    items = maintenance.plan(config)
    receipt = retained["files"][remote]["receipts"][-1]
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        delivery = maintenance._delivery(
            store, receipt, tmp_path / "replacement.zip", "final", "registry"
        )
    work["files"][remote + "#" + receipt["sha256"]] = {
        **old,
        "sha256": receipt["sha256"],
        "delivery_id": delivery,
    }
    failure_key = "partial-native:" + remote.rsplit("/", 1)[0]
    work["failures"][failure_key] = {"code": "earlier_partial_failure"}

    def unexpected_verify(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Old import is not bound to the new native delivery")

    monkeypatch.setattr(maintenance, "verify_registry", unexpected_verify)
    errors = []
    maintenance._retire_replaced_partial_failures(
        config,
        settings,
        items,
        retained,
        work,
        lambda *args: errors.append(args),
        lambda: False,
    )
    assert work["failures"][failure_key] == {"code": "earlier_partial_failure"}
    assert not errors


def reviewed_alternative_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, empty: bool = False
) -> tuple[Path, dict[str, Any]]:
    from tests.test_foundation_lms_ftp import members, populated

    files = members()
    rows = [
        populated(
            60, {(1, 11): b"28101234567", (12, 17): f"{n:06}".encode(), (22, 30): b"000000123"}
        )
        + b"\r\n"
        for n in (7, 8, 9)
    ]
    files["lms24.txt"] = b"".join(rows)
    native = delivery_bytes(files)
    path = prepared(tmp_path, monkeypatch, native=native)
    config = acquisition.load_config(path)
    listed = list(acquisition.selected_files(config))
    retained = acquisition.read_state(config)
    directory = listed[0]["remote"].rsplit("/", 1)[0]
    with zipfile.ZipFile(io.BytesIO(native)) as archive:
        system = archive.read("system.txt")
    components = []
    for name, body in (("system.txt", system), ("lms24.txt", b"" if empty else rows[0])):
        remote = directory + "/" + name
        source = tmp_path / name
        source.write_bytes(body)
        sha, relative, size = archive_file(config.archive_root, source)
        listed.append({"remote": remote, "size": size, "server_modification": "2026-01-01"})
        retained["files"][remote] = {
            "current_sha256": sha,
            "inventory_size": size,
            "inventory_modification": "2026-01-01",
            "receipts": [
                {
                    "remote": remote,
                    "sha256": sha,
                    "archive_relative_path": relative,
                    "started_utc": "2026-10-22T10:00:00+00:00",
                    "completed_utc": "2026-10-22T10:01:00+00:00",
                    "server_modification": "2026-01-01",
                }
            ],
        }
        components.append({"remote": remote, "sha256": sha})
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    monkeypatch.setattr(acquisition, "selected_files", lambda _: listed)
    evidence = tmp_path / "source-review/comparison.json"
    evidence.parent.mkdir()
    evidence.write_bytes(b'{"scope":"authored alternative transport comparison"}')
    review = {
        "version": 1,
        "scopes": [
            {
                "directory": directory,
                "phase": "final",
                "reason": "Distinct loose alternative; two fewer records; no precedence",
                "evidence": {
                    "path": "source-review/comparison.json",
                    "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                },
                "components": components,
            }
        ],
    }
    pin_alternative_review(path, review)
    return path, review


def pin_alternative_review(path: Path, review: dict[str, Any]) -> None:
    original = path.parent / "source-review/alternatives.json"
    original.write_text(json.dumps(review), encoding="utf-8")
    settings = json.loads(path.read_text())
    settings["reviewed_alternative_transports"] = {
        "path": "source-review/alternatives.json",
        "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
    }
    path.write_text(json.dumps(settings), encoding="utf-8")


def test_reviewed_alternative_coexists_with_complete_zip_and_retries_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation.lms_registry_partial import partial_reference_member

    path, review = reviewed_alternative_sources(tmp_path, monkeypatch)
    config, settings = maintenance.load_maintenance(path)
    plain = maintenance.plan(config)
    assert [item["action"] for item in plain].count("packaged") == 2
    assert maintenance.update(path, refresh=False)["run_status"] == "complete"
    work = maintenance.read_journal(config)
    group = next(iter(work["partial_groups"].values()))
    zip_checkpoint = next(v for v in work["files"].values() if v["action"] == "registry")
    assert zip_checkpoint["status"] == "independently_verified"
    assert group["canonical_eligible"] is False and group["coverage"]["complete_registry"] is False
    member = next(m for m in group["coverage"]["members"] if m["member_number"] == 24)
    assert member["declared_records"] == 3 and member["available_records"] == 1
    assert member["count_matches"] is False
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        partial = partial_reference_member(store, group["import_id"], 24)
        assert len(partial["rows"]) == 1
        assert partial["rows"][0]["minimum_aip_unit_ore"] == 123
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == (2,)
    before = (config.operation_root / "interpretation-state.json").read_bytes()
    maintenance.update(path, refresh=False)
    assert (config.operation_root / "interpretation-state.json").read_bytes() == before
    assert len(list((config.operation_root / "partial-manifests").glob("*.json"))) == 1
    planned = maintenance.plan(config, alternative_scopes=settings["alternative_scopes"])
    assert (
        next(item for item in planned if item["remote"].endswith("lms.zip"))["action"] == "registry"
    )
    assert settings["alternative_scopes"][0]["components"] == review["scopes"][0]["components"]


def test_empty_reviewed_alternative_never_replaces_complete_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation.lms_registry_partial import partial_reference_member

    path, _ = reviewed_alternative_sources(tmp_path, monkeypatch, empty=True)
    maintenance.update(path, refresh=False)
    config = acquisition.load_config(path)
    group = next(iter(maintenance.read_journal(config)["partial_groups"].values()))
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        member = partial_reference_member(store, group["import_id"], 24)
        assert member["status"] == "empty_unavailable" and member["rows"] == []
        assert store._connection.execute(
            "SELECT m.row_count FROM registry_release_member r JOIN registry_member m "
            "USING(member_id) WHERE m.member_number=24"
        ).fetchone() == (3,)


@pytest.mark.parametrize(
    "mutation",
    [
        "unpinned",
        "changed_pin",
        "directory_escape",
        "other_directory",
        "review_escape",
        "evidence_escape",
        "missing_inventory",
    ],
)
def test_alternative_review_refuses_unbound_scope_before_store_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    path, review = reviewed_alternative_sources(tmp_path, monkeypatch)
    scope = review["scopes"][0]
    if mutation == "unpinned":
        scope["components"][1].pop("sha256")
    elif mutation == "changed_pin":
        scope["components"][1]["sha256"] = "0" * 64
    elif mutation == "directory_escape":
        scope["directory"] += "/../elsewhere"
    elif mutation == "other_directory":
        scope["components"][1]["remote"] = "/LMS/elsewhere/lms24.txt"
    elif mutation == "evidence_escape":
        scope["evidence"]["path"] = "source-review/../../outside.json"
    elif mutation == "missing_inventory":
        listed = acquisition.selected_files(acquisition.load_config(path))
        monkeypatch.setattr(acquisition, "selected_files", lambda _: listed[:2])
    pin_alternative_review(path, review)
    if mutation == "review_escape":
        settings = json.loads(path.read_text())
        settings["reviewed_alternative_transports"]["path"] = "source-review/../alternatives.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ValueError):
        maintenance.update(path, refresh=False)
    assert not (tmp_path / "db.duckdb").exists()


def test_changed_reviewed_source_and_evidence_preserve_prior_accepted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, review = reviewed_alternative_sources(tmp_path, monkeypatch)
    maintenance.update(path, refresh=False)
    config = acquisition.load_config(path)
    before = (config.operation_root / "interpretation-state.json").read_bytes()
    retained = acquisition.read_state(config)
    remote = review["scopes"][0]["components"][1]["remote"]
    original = retained["files"][remote]["current_sha256"]
    retained["files"][remote]["current_sha256"] = "0" * 64
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    with pytest.raises(ValueError, match="reviewed SHA256"):
        maintenance.update(path, refresh=False)
    assert (config.operation_root / "interpretation-state.json").read_bytes() == before
    retained["files"][remote]["current_sha256"] = original
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    (tmp_path / "source-review/comparison.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA256"):
        maintenance.update(path, refresh=False)
    assert (config.operation_root / "interpretation-state.json").read_bytes() == before


def test_failed_alternative_is_not_retired_by_coexisting_verified_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = reviewed_alternative_sources(tmp_path, monkeypatch)
    maintenance.update(path, refresh=False)
    config = acquisition.load_config(path)
    prior = maintenance.read_journal(config)
    accepted = next(iter(prior["partial_groups"].values()))
    zip_checkpoints = copy.deepcopy(prior["files"])

    def failed(*_: Any, **__: Any) -> None:
        raise ValueError("authored independent alternative readback failure")

    monkeypatch.setattr(maintenance, "ingest_partial_registry", failed)
    for _ in range(2):
        assert maintenance.update(path, refresh=False)["run_status"] == "failed"
        current = maintenance.read_journal(config)
        assert current["files"] == zip_checkpoints
        group = next(iter(current["partial_groups"].values()))
        assert group["status"] == "failed" and group["import_id"] == accepted["import_id"]
        assert group["evidence_hash"] == accepted["evidence_hash"]
        assert current["failures"]


def test_reviewed_alternative_rechecks_actual_bytes_before_source_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation.archive import archive_path

    path, review = reviewed_alternative_sources(tmp_path, monkeypatch)
    maintenance.update(path, refresh=False)
    config = acquisition.load_config(path)
    before = maintenance.read_journal(config)
    pinned = review["scopes"][0]["components"][1]
    original = archive_path(config.archive_root, pinned["sha256"])
    payload = original.read_bytes()
    original.write_bytes(b"9" + payload[1:])
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        source_count = store._connection.execute("SELECT count(*) FROM source_delivery").fetchone()
    assert maintenance.update(path, refresh=False)["run_status"] == "failed"
    after = maintenance.read_journal(config)
    assert after["files"] == before["files"]
    assert next(iter(after["partial_groups"].values()))["status"] == "failed"
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert (
            store._connection.execute("SELECT count(*) FROM source_delivery").fetchone()
            == source_count
        )
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (1,)


def test_reviewed_alternative_status_before_discovery_defers_inventory_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = reviewed_alternative_sources(tmp_path, monkeypatch)
    config = acquisition.load_config(path)
    inventory_bytes = config.inventory_path.read_bytes()
    config.inventory_path.unlink()
    listed = acquisition.selected_files(config)

    def forbidden_inventory(_: Any) -> Any:
        raise AssertionError("Status must not plan undiscovered inventory")

    monkeypatch.setattr(acquisition, "selected_files", forbidden_inventory)
    result = maintenance.status(path)
    assert result["inventory"] == {
        "inventory_complete": False,
        "file_count": 0,
        "state": "not_discovered",
    }
    assert result["dispositions"] == {} and result["partial_registry"]["groups"] == 0
    assert not (tmp_path / "db.duckdb").exists()
    assert not (config.operation_root / "interpretation-state.json").exists()
    assert not config.inventory_path.exists()

    # Once discovered, both status and update still require the complete exact
    # reviewed component set in the inventory; status did not grant admission.
    config.inventory_path.write_bytes(inventory_bytes)
    monkeypatch.setattr(acquisition, "selected_files", lambda _: listed[:2])
    with pytest.raises(ValueError, match="not inventoried"):
        maintenance.status(path)
    with pytest.raises(ValueError, match="not inventoried"):
        maintenance.update(path, refresh=False)
    assert not (tmp_path / "db.duckdb").exists()


def malformed_loose_alternative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, raw_only: bool = True
) -> tuple[Path, dict[str, Any]]:
    from tests.test_foundation_lms_ftp import members

    files = members()
    rows = [
        f"{number:05}".encode() + b"Label".ljust(50) + b"Description".ljust(500)
        for number in range(56)
    ]
    assert all(len(row) == 555 for row in rows)
    files["lms17.txt"] = b"\r\n".join(rows) + b"\r\n"
    native = delivery_bytes(files)
    path = prepared(tmp_path, monkeypatch, native=native)
    config = acquisition.load_config(path)
    listed = list(acquisition.selected_files(config))
    retained = acquisition.read_state(config)
    directory = listed[0]["remote"].rsplit("/", 1)[0]
    with zipfile.ZipFile(io.BytesIO(native)) as archive:
        system = archive.read("system.txt")
    rows[3] = rows[3][:107] + rows[3][108:]
    assert len(rows[3]) == 554 and len(rows) == 56
    components = []
    for name, body in (("system.txt", system), ("lms17.txt", b"\r\n".join(rows) + b"\r\n")):
        source = tmp_path / name
        source.write_bytes(body)
        sha, relative, size = archive_file(config.archive_root, source)
        remote = directory + "/" + name
        listed.append({"remote": remote, "size": size, "server_modification": "2026-01-01"})
        retained["files"][remote] = {
            "current_sha256": sha,
            "inventory_size": size,
            "inventory_modification": "2026-01-01",
            "receipts": [
                {
                    "remote": remote,
                    "sha256": sha,
                    "archive_relative_path": relative,
                    "started_utc": "2026-10-22T10:00:00+00:00",
                    "completed_utc": "2026-10-22T10:01:00+00:00",
                    "server_modification": "2026-01-01",
                }
            ],
        }
        components.append({"remote": remote, "sha256": sha})
    acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    monkeypatch.setattr(acquisition, "selected_files", lambda _: listed)
    evidence = tmp_path / "source-review/malformed-width.json"
    evidence.parent.mkdir()
    evidence.write_text('{"records":56,"widths":{"555":55,"554":1}}', encoding="utf-8")
    scope = {
        "directory": directory,
        "phase": "final",
        "reason": "One of56 loose LMS17 rows is554bytes instead of555; retained without repair",
        "evidence": {
            "path": "source-review/malformed-width.json",
            "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        },
        "components": components,
    }
    if raw_only:
        scope["interpretation"] = "raw_only"
    review = {"version": 1, "scopes": [scope]}
    pin_alternative_review(path, review)
    return path, review


def test_malformed_reviewed_raw_alternative_retains_without_partial_or_zip_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, review = malformed_loose_alternative(tmp_path, monkeypatch, raw_only=False)
    assert maintenance.update(path, refresh=False)["run_status"] == "failed"
    config = acquisition.load_config(path)
    previous = maintenance.read_journal(config)
    assert previous["failures"] and previous["partial_groups"]
    zip_key = next(key for key, value in previous["files"].items() if value["action"] == "registry")
    zip_checkpoint = copy.deepcopy(previous["files"][zip_key])
    review["scopes"][0]["interpretation"] = "raw_only"
    pin_alternative_review(path, review)
    result = maintenance.update(path, refresh=False)
    assert result["run_status"] == "complete" and result["failures"] == {}
    current = maintenance.read_journal(config)
    assert current["partial_groups"] == previous["partial_groups"]
    assert current["files"][zip_key] == zip_checkpoint
    _, settings = maintenance.load_maintenance(path)
    planned = maintenance.plan(config, alternative_scopes=settings["alternative_scopes"])
    assert (
        maintenance._partial_directories(planned, alternative_scopes=settings["alternative_scopes"])
        == {}
    )
    for component in review["scopes"][0]["components"]:
        checkpoint = current["files"][component["remote"] + "#" + component["sha256"]]
        assert checkpoint["action"] == "retain"
        assert checkpoint["status"] == "retained_without_interpretation"
        assert checkpoint["reason"] == review["scopes"][0]["reason"]
        assert checkpoint["source_role"] == "raw_source" and checkpoint["phase"] == "final"
        assert (
            checkpoint["canonical_eligible"] is False and checkpoint["complete_registry"] is False
        )
        assert checkpoint["alternative_transport"] == settings["alternative_scopes"][0]
        assert not any(
            key in checkpoint for key in ("import_id", "verification", "independently_verified")
        )
        with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
            delivery = store.get_record("source_delivery", checkpoint["delivery_id"])
            assert (
                json.loads(delivery["coverage"])["alternative_transport"]
                == settings["alternative_scopes"][0]
            )
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)
        assert store._connection.execute(
            "SELECT count(*) FROM import_batch WHERE validation_status='accepted'"
        ).fetchone() == (1,)
        assert store._connection.execute(
            "SELECT m.row_count FROM registry_release_member r JOIN registry_member m "
            "USING(member_id) WHERE m.member_number=17"
        ).fetchone() == (56,)
    before = (config.operation_root / "interpretation-state.json").read_bytes()
    maintenance.update(path, refresh=False)
    assert (config.operation_root / "interpretation-state.json").read_bytes() == before


def test_raw_review_phase_and_changed_scope_are_bound_even_after_retain_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, review = malformed_loose_alternative(tmp_path, monkeypatch)
    scope = review["scopes"][0]
    scope["phase"] = "preliminary"
    pin_alternative_review(path, review)
    maintenance.update(path, refresh=False)
    config = acquisition.load_config(path)
    previous = maintenance.read_journal(config)
    component = scope["components"][1]
    key = component["remote"] + "#" + component["sha256"]
    old_delivery = previous["files"][key]["delivery_id"]
    assert previous["files"][key]["phase"] == "preliminary"
    scope.update(phase="final", reason="A new pinned review retains the same malformed bytes")
    pin_alternative_review(path, review)
    maintenance.update(path, refresh=False)
    current = maintenance.read_journal(config)["files"][key]
    assert current["phase"] == "final" and current["reason"] == scope["reason"]
    assert current["delivery_id"] != old_delivery
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        assert store.get_record("source_delivery", old_delivery)["phase"] == "preliminary"
        assert store.get_record("source_delivery", current["delivery_id"])["phase"] == "final"
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "mutation",
    [
        "changed_pin",
        "changed_acquisition",
        "changed_bytes",
        "ordinary_overlap",
        "unknown_mode",
        "null_mode",
        "boolean_mode",
    ],
)
def test_raw_alternative_refuses_changed_or_unreviewed_scope_before_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from bef_redbird.foundation.archive import archive_path

    path, review = malformed_loose_alternative(tmp_path, monkeypatch)
    config = acquisition.load_config(path)
    scope = review["scopes"][0]
    component = scope["components"][1]
    if mutation == "changed_pin":
        component["sha256"] = "0" * 64
    elif mutation == "changed_acquisition":
        retained = acquisition.read_state(config)
        retained["files"][component["remote"]]["current_sha256"] = "0" * 64
        acquisition.write_json(config.operation_root / "acquisition-state.json", retained)
    elif mutation == "changed_bytes":
        source = archive_path(config.archive_root, component["sha256"])
        body = source.read_bytes()
        source.write_bytes(b"9" + body[1:])
    elif mutation == "ordinary_overlap":
        listed = acquisition.selected_files(config)
        listed[0]["size"] = 0
    else:
        scope["interpretation"] = {
            "unknown_mode": "typed",
            "null_mode": None,
            "boolean_mode": True,
        }[mutation]
    pin_alternative_review(path, review)
    with pytest.raises((ValueError, ArchiveError)):
        maintenance.update(path, refresh=False)
    assert not (tmp_path / "db.duckdb").exists()


def test_supported_partial_after_raw_retention_gets_normal_phase_and_source_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, review = reviewed_alternative_sources(tmp_path, monkeypatch)
    scope = review["scopes"][0]
    scope.update(interpretation="raw_only", phase="preliminary")
    pin_alternative_review(path, review)
    assert maintenance.update(path, refresh=False)["run_status"] == "complete"
    config = acquisition.load_config(path)
    raw = maintenance.read_journal(config)
    raw_deliveries = {
        item["remote"]: raw["files"][item["remote"] + "#" + item["sha256"]]["delivery_id"]
        for item in scope["components"]
    }
    scope.pop("interpretation")
    scope["phase"] = "final"
    pin_alternative_review(path, review)
    assert maintenance.update(path, refresh=False)["run_status"] == "complete"
    supported = maintenance.read_journal(config)
    assert len(supported["partial_groups"]) == 1
    assert (
        next(iter(supported["partial_groups"].values()))["status"] == "partial_reference_verified"
    )
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        for item in scope["components"]:
            checkpoint = supported["files"][item["remote"] + "#" + item["sha256"]]
            assert checkpoint["delivery_id"] != raw_deliveries[item["remote"]]
            normal = store.get_record("source_delivery", checkpoint["delivery_id"])
            old = store.get_record("source_delivery", raw_deliveries[item["remote"]])
            assert normal["phase"] == "final" and old["phase"] == "preliminary"
            assert json.loads(normal["coverage"])["family"] == "registry_partial"
            assert "alternative_transport" not in json.loads(normal["coverage"])
            assert json.loads(old["coverage"])["source_role"] == "raw_source"
            receipt = maintenance._component_receipt(
                next(
                    value
                    for value in acquisition.selected_files(config)
                    if value["remote"] == item["remote"]
                ),
                acquisition.read_state(config),
            )
            assert receipt is not None
            assert (
                maintenance._delivery(
                    store,
                    receipt,
                    store.original_path(checkpoint["delivery_id"]),
                    "final",
                    "registry_partial",
                )
                == checkpoint["delivery_id"]
            )
    before = (config.operation_root / "interpretation-state.json").read_bytes()
    maintenance.update(path, refresh=False)
    assert (config.operation_root / "interpretation-state.json").read_bytes() == before
