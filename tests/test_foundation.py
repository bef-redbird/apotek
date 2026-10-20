"""Independent synthetic acceptance for F1 storage, identity and provenance.

All bytes and identifiers are invented here. Temporary databases and archives
live in pytest's external temporary directory, never in the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from bef_redbird.foundation import ArchiveError, FoundationError, Store, Timing, WriterBusy


@pytest.fixture
def storage(tmp_path):
    return tmp_path / "foundation.duckdb", tmp_path / "originals"


@pytest.fixture
def original(tmp_path):
    path = tmp_path / "synthetic-source.txt"
    path.write_bytes(b"invented fixture\n000007\n")
    return path


def receipt(store, original, key="receipt-1", **context):
    return store.register_source(original, source="synthetic", receipt_key=key, **context)


def imported(store, original):
    delivery = receipt(store, original)
    batch = store.begin_import(
        delivery, parser_version="parser-v1", schema_version="schema-v1", mapping_version="map-v1"
    )
    store.finish_import(
        batch, status="accepted", record_count=2, accepted_count=2, rejected_count=0
    )
    return batch


def mapping(store, package, batch, **kwargs):
    return store.add_identifier(
        package,
        namespace="varenummer",
        identifier_value="000007",
        evidence_import_id=batch,
        source_locator="sheet:invented!A2",
        mapping_version="map-v1",
        **kwargs,
    )


def test_repeated_initialization_has_one_migration_and_stable_uuid(storage):
    database, archive = storage
    with Store(database, archive) as store:
        package = store.create_package()
        assert str(UUID(package)) == package
        migrations = store._connection.execute(
            "SELECT version, name, checksum FROM schema_migration ORDER BY version"
        ).fetchall()
        assert migrations
        assert all(len(row[2]) == 64 for row in migrations)
    with Store(database, archive) as store:
        assert store.create_package(package) == package
        assert store._connection.execute(
            "SELECT CAST(package_id AS VARCHAR) FROM package"
        ).fetchall() == [(package,)]
        assert (
            store._connection.execute(
                "SELECT version, name, checksum FROM schema_migration ORDER BY version"
            ).fetchall()
            == migrations
        )


def test_restore_preserves_ids_provenance_and_archive_relocation(storage, original, tmp_path):
    database, archive = storage
    with Store(database, archive) as store:
        package = store.create_package()
        batch = imported(store, original)
        mapping_id = mapping(store, package, batch, validity_kind="scope", scope_key="edition-a")
        delivery = receipt(store, original)
    restored_db = tmp_path / "restored.duckdb"
    restored_archive = tmp_path / "relocated-originals"
    shutil.copy2(database, restored_db)
    shutil.copytree(archive, restored_archive)
    with Store(restored_db, restored_archive) as store:
        assert (
            store.resolve_identifier(
                "varenummer", "000007", mapping_version="map-v1", scope_key="edition-a"
            )
            == package
        )
        assert store._connection.execute(
            "SELECT CAST(mapping_id AS VARCHAR), CAST(evidence_import_id AS VARCHAR) "
            "FROM package_identifier"
        ).fetchall() == [(mapping_id, batch)]
        assert store.original_path(delivery).read_bytes() == b"invented fixture\n000007\n"
        assert store.original_path(delivery).is_relative_to(restored_archive)


def test_migration_checksum_tampering_is_rejected(storage):
    database, archive = storage
    with Store(database, archive) as store:
        store._connection.execute("UPDATE schema_migration SET checksum = ?", ["0" * 64])
    with pytest.raises(ValueError):
        with Store(database, archive):
            pass


def test_delivery_retry_is_idempotent_but_same_bytes_can_have_distinct_receipts(storage, original):
    with Store(*storage) as store:
        first = receipt(store, original)
        assert receipt(store, original) == first
        second = receipt(store, original, "receipt-2", edition="later-release")
        assert second != first
        assert store.original_path(first) == store.original_path(second)
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (2,)
        report = store.verify_archive()
        assert report["ok"] == 1
        assert report["missing"] == report["corrupt"] == 0


def test_conflicting_receipt_retry_rejects_changed_bytes_or_metadata(storage, original):
    with Store(*storage) as store:
        delivery = receipt(store, original, edition="edition-a")
        with pytest.raises((ValueError, FoundationError)):
            receipt(store, original, edition="edition-b")
        original.write_bytes(b"different invented bytes")
        with pytest.raises((ValueError, FoundationError)):
            receipt(store, original, edition="edition-a")
        assert store.original_path(delivery).read_bytes() == b"invented fixture\n000007\n"
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (1,)


def test_import_identity_includes_every_interpretation_version_and_context(storage, original):
    with Store(*storage) as store:
        delivery = receipt(store, original)
        base = dict(parser_version="p1", schema_version="s1", mapping_version="m1")
        first = store.begin_import(delivery, **base, context={"b": 2, "a": 1})
        assert store.begin_import(delivery, **base, context={"a": 1, "b": 2}) == first
        alternatives = [
            store.begin_import(delivery, **(base | {key: "v2"}), context={"a": 1, "b": 2})
            for key in base
        ]
        alternatives.append(store.begin_import(delivery, **base, context={"a": 2, "b": 2}))
        assert len({first, *alternatives}) == 5
        later_receipt = receipt(store, original, "receipt-2")
        assert store.begin_import(later_receipt, **base, context={"a": 1, "b": 2}) not in {
            first,
            *alternatives,
        }


def test_unknown_source_timing_stays_unknown_after_processing(storage, original):
    with Store(*storage) as store:
        batch = imported(store, original)
        assert store._connection.execute(
            "SELECT received_lower, received_upper, received_precision, "
            "published_lower, published_upper, published_precision FROM source_delivery"
        ).fetchone() == (None, None, "unknown", None, None, "unknown")
        processing, completed, status = store._connection.execute(
            "SELECT processing_at, completed_at, validation_status "
            "FROM import_batch WHERE import_id=?",
            [batch],
        ).fetchone()
        assert processing is not None
        assert completed >= processing
        assert status == "accepted"


def test_source_timing_has_typed_utc_bounds_and_original_precision(storage, original):
    local = timezone(timedelta(hours=2))
    lower = datetime(2025, 6, 2, 0, tzinfo=local)
    upper = datetime(2025, 6, 3, 0, tzinfo=local)
    published = Timing(
        lower=lower,
        upper=upper,
        precision="day",
        basis="dated release manifest",
        source_timezone="Europe/Copenhagen",
    )
    with Store(*storage) as store:
        receipt(store, original, published=published)
        row = store._connection.execute(
            "SELECT published_lower, published_upper, published_precision, "
            "published_basis, published_timezone FROM source_delivery"
        ).fetchone()
        assert row == (
            datetime(2025, 6, 1, 22, tzinfo=UTC),
            datetime(2025, 6, 2, 22, tzinfo=UTC),
            "day",
            "dated release manifest",
            "Europe/Copenhagen",
        )
        types = {
            row[0]: row[1]
            for row in store._connection.execute("DESCRIBE source_delivery").fetchall()
        }
        assert "TIMESTAMP" in types["published_lower"]
        assert "TIMESTAMP" in types["received_lower"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lower": datetime(2025, 1, 1), "upper": datetime(2025, 1, 1), "precision": "instant"},
        {
            "lower": datetime(2025, 1, 2, tzinfo=UTC),
            "upper": datetime(2025, 1, 1, tzinfo=UTC),
            "precision": "range",
        },
        {"lower": datetime(2025, 1, 1, tzinfo=UTC), "precision": "unknown"},
    ],
)
def test_invalid_timing_is_rejected(storage, original, kwargs):
    with pytest.raises(ValueError):
        with Store(*storage) as store:
            receipt(store, original, published=Timing(**kwargs))


def test_period_calendar_is_half_open_without_synthesized_slots(storage):
    with Store(*storage) as store:
        store.add_period(date(2025, 1, 6), date(2025, 1, 20))
        store.add_period(date(2025, 2, 3), date(2025, 2, 17))
        assert store._connection.execute(
            "SELECT period_start, period_end_exclusive FROM price_period ORDER BY period_start"
        ).fetchall() == [
            (date(2025, 1, 6), date(2025, 1, 20)),
            (date(2025, 2, 3), date(2025, 2, 17)),
        ]
        with pytest.raises(ValueError):
            store.add_period(date(2025, 1, 6), date(2025, 1, 6))
        with pytest.raises(ValueError):
            store.add_period(date(2025, 1, 13), date(2025, 1, 27))


def test_identifier_leading_zeros_unknown_validity_and_unresolved_identity(storage, original):
    with Store(*storage) as store:
        batch = imported(store, original)
        package = store.create_package()
        mapping(store, package, batch)
        assert store._connection.execute(
            "SELECT identifier_value, validity_kind FROM package_identifier"
        ).fetchall() == [("000007", "unknown")]
        for on_date in [None, date(1999, 1, 1), date(2025, 1, 6)]:
            assert (
                store.resolve_identifier(
                    "varenummer", "000007", mapping_version="map-v1", on_date=on_date
                )
                is None
            )
        mapping(store, None, batch, validity_kind="scope", scope_key="unresolved-release")
        assert (
            store.resolve_identifier(
                "varenummer", "000007", mapping_version="map-v1", scope_key="unresolved-release"
            )
            is None
        )


def test_interval_mapping_resolves_only_evidenced_dates_and_version(storage, original):
    with Store(*storage) as store:
        batch = imported(store, original)
        package = store.create_package()
        mapping(
            store,
            package,
            batch,
            validity_kind="interval",
            valid_from=date(2025, 1, 6),
            valid_to=date(2025, 1, 20),
        )
        for on_date, expected in [
            (date(2025, 1, 5), None),
            (date(2025, 1, 6), package),
            (date(2025, 1, 19), package),
            (date(2025, 1, 20), None),
        ]:
            assert (
                store.resolve_identifier(
                    "varenummer", "000007", mapping_version="map-v1", on_date=on_date
                )
                == expected
            )
        assert (
            store.resolve_identifier(
                "varenummer", "7", mapping_version="map-v1", on_date=date(2025, 1, 6)
            )
            is None
        )
        assert (
            store.resolve_identifier(
                "varenummer", "000007", mapping_version="map-v2", on_date=date(2025, 1, 6)
            )
            is None
        )


def test_scope_mapping_is_exact_and_conflicting_overlap_is_rejected(storage, original):
    with Store(*storage) as store:
        batch = imported(store, original)
        first, second = store.create_package(), store.create_package()
        mapping(store, first, batch, validity_kind="scope", scope_key="release-a")
        mapping(store, second, batch, validity_kind="scope", scope_key="release-b")
        assert (
            store.resolve_identifier(
                "varenummer", "000007", mapping_version="map-v1", scope_key="release-a"
            )
            == first
        )
        assert store.resolve_identifier("varenummer", "000007", mapping_version="map-v1") is None
        with pytest.raises(ValueError):
            mapping(store, second, batch, validity_kind="scope", scope_key="release-a")
        mapping(
            store,
            first,
            batch,
            validity_kind="interval",
            valid_from=date(2025, 1, 6),
            valid_to=date(2025, 1, 20),
        )
        with pytest.raises(ValueError):
            mapping(
                store,
                second,
                batch,
                validity_kind="interval",
                valid_from=date(2025, 1, 19),
                valid_to=date(2025, 2, 3),
            )
        mapping(
            store,
            second,
            batch,
            validity_kind="interval",
            valid_from=date(2025, 1, 20),
            valid_to=date(2025, 2, 3),
        )


def test_issue_retains_locator_and_limits_its_blocking_scope(storage, original):
    with Store(*storage) as store:
        store.add_period(date(2025, 1, 6), date(2025, 1, 20))
        delivery = receipt(store, original)
        batch = store.begin_import(
            delivery, parser_version="p1", schema_version="s1", mapping_version="m1"
        )
        issue = store.add_issue(
            batch,
            source_locator="invented!B3",
            code="unresolved_identifier",
            severity="error",
            blocking_scope="identifier",
            identifier="000007",
            field="varenummer",
            period=date(2025, 1, 6),
        )
        assert str(UUID(issue)) == issue
        assert store._connection.execute(
            "SELECT source_locator, code, severity, blocking_scope, identifier, field, period "
            "FROM import_issue"
        ).fetchone() == (
            "invented!B3",
            "unresolved_identifier",
            "error",
            "identifier",
            "000007",
            "varenummer",
            date(2025, 1, 6),
        )
        store.finish_import(
            batch, status="rejected", record_count=2, accepted_count=1, rejected_count=1
        )


def test_transaction_rollback_leaves_safe_archive_orphan_and_retry_recovers(storage, original):
    database, archive = storage
    with Store(database, archive) as store:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            with store.transaction():
                store.create_package()
                receipt(store, original)
                raise RuntimeError("synthetic interruption")
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM package").fetchone() == (0,)
        assert store.verify_archive()["orphan"] == 1
        delivery = receipt(store, original)
        assert store.original_path(delivery).read_bytes() == original.read_bytes()
        assert store.verify_archive()["orphan"] == 0


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_original_verification_detects_missing_or_corrupt_bytes(storage, original, damage):
    with Store(*storage) as store:
        delivery = receipt(store, original)
        archived = store.original_path(delivery)
        archived.chmod(0o600)
        if damage == "missing":
            archived.unlink()
        else:
            archived.write_bytes(b"damaged archive fixture")
        with pytest.raises(ArchiveError):
            store.original_path(delivery)
        report = store.verify_archive()
        assert report[damage] == 1
        assert report["ok"] == 0


def test_archive_uses_content_address_and_preserves_original_bytes(storage, original):
    with Store(*storage) as store:
        delivery = receipt(store, original)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        assert store.original_path(delivery).relative_to(storage[1]).as_posix() == (
            f"sha256/{digest[:2]}/{digest}"
        )
        original.write_bytes(b"source was changed after registration")
        assert store.original_path(delivery).read_bytes() == b"invented fixture\n000007\n"


def test_same_process_cannot_open_second_writer_and_can_reopen_after_close(storage):
    with Store(*storage):
        with pytest.raises(WriterBusy):
            with Store(*storage):
                pass
    with Store(*storage) as store:
        assert store.create_package()


def test_process_crash_releases_writer_lock_and_rolls_back_database(storage, tmp_path):
    ready = tmp_path / "writer-ready"
    script = textwrap.dedent("""
        import sys
        import time
        from pathlib import Path
        from bef_redbird.foundation import Store
        with Store(Path(sys.argv[1]), Path(sys.argv[2])) as store:
            with store.transaction():
                store.create_package()
                Path(sys.argv[3]).write_text('ready', encoding='utf-8')
                while True:
                    time.sleep(0.05)
    """)
    environment = os.environ.copy()
    src = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [src, environment.get("PYTHONPATH")]))
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(storage[0]), str(storage[1]), str(ready)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "synthetic writer failed to initialize"
        with pytest.raises(WriterBusy):
            with Store(*storage):
                pass
        process.kill()
        process.wait(timeout=10)
        # Windows can finish releasing file locks shortly after process.wait().
        # Recovery must need neither lock-file deletion nor manual intervention.
        deadline = time.monotonic() + 5
        while True:
            try:
                with Store(*storage) as store:
                    assert store._connection.execute("SELECT count(*) FROM package").fetchone() == (
                        0,
                    )
                    store.create_package()
                break
            except WriterBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.communicate(timeout=10)


def test_database_enforces_period_scalar_check_even_without_public_api(storage):
    with Store(*storage) as store:
        with pytest.raises(duckdb.ConstraintException):
            store._connection.execute(
                "INSERT INTO price_period VALUES (?, ?)", [date(2025, 1, 20), date(2025, 1, 6)]
            )


def test_short_varenummer_keeps_source_literal_and_canonical_six_digits(storage, original):
    with Store(*storage) as store:
        batch = imported(store, original)
        package = store.create_package()
        store.add_identifier(
            package,
            namespace="dk:varenummer",
            identifier_value="7",
            evidence_import_id=batch,
            source_locator="invented!A2",
            mapping_version="map-v1",
            validity_kind="scope",
            scope_key="release-a",
        )
        assert store._connection.execute(
            "SELECT identifier_value, canonical_value FROM package_identifier"
        ).fetchall() == [("7", "000007")]
        assert (
            store.resolve_identifier(
                "dk:varenummer", "000007", mapping_version="map-v1", scope_key="release-a"
            )
            == package
        )


def test_failed_archive_publication_never_registers_delivery(storage, original, monkeypatch):
    import bef_redbird.foundation.archive as archive_module

    def fail_publication(*args, **kwargs):
        raise OSError("synthetic filesystem failure")

    with Store(*storage) as store:
        with monkeypatch.context() as patch:
            patch.setattr(archive_module.os, "link", fail_publication)
            with pytest.raises(ArchiveError):
                receipt(store, original)
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (0,)
        assert original.read_bytes() == b"invented fixture\n000007\n"
        assert store.verify_archive()["staging"] == 0
        assert store.original_path(receipt(store, original)).read_bytes() == original.read_bytes()


def test_database_enforces_delivery_period_foreign_key_and_required_source(storage, original):
    with Store(*storage) as store:
        receipt(store, original)
        with pytest.raises(duckdb.ConstraintException):
            store._connection.execute(
                "UPDATE source_delivery SET current_period = DATE '2025-01-06'"
            )
        with pytest.raises(duckdb.ConstraintException):
            store._connection.execute("UPDATE source_delivery SET source = ''")


def test_database_enforces_mapping_evidence_foreign_key_and_import_counts(storage, original):
    with Store(*storage) as store:
        batch = imported(store, original)
        mapping(store, store.create_package(), batch)
        with pytest.raises(duckdb.ConstraintException):
            store._connection.execute(
                "UPDATE package_identifier SET evidence_import_id = ?",
                ["00000000-0000-0000-0000-000000000000"],
            )
        with pytest.raises(duckdb.ConstraintException):
            store._connection.execute("UPDATE import_batch SET accepted_count = -1")


def test_import_completion_is_idempotent_and_terminal_state_cannot_be_changed(storage, original):
    with Store(*storage) as store:
        batch = imported(store, original)
        store.finish_import(
            batch, status="accepted", record_count=2, accepted_count=2, rejected_count=0
        )
        with pytest.raises(ValueError):
            store.finish_import(
                batch, status="failed", record_count=2, accepted_count=0, rejected_count=2
            )


def test_corrupt_existing_object_blocks_reregistration_without_overwrite(storage, original):
    with Store(*storage) as store:
        archived = store.original_path(receipt(store, original))
        archived.chmod(0o600)
        archived.write_bytes(b"damaged existing object")
        with pytest.raises(ArchiveError):
            receipt(store, original, "second-receipt")
        assert archived.read_bytes() == b"damaged existing object"
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (1,)


@pytest.mark.parametrize("inside", ["database", "archive", "source"])
def test_git_worktree_cannot_be_used_for_private_storage_or_originals(tmp_path, inside):
    worktree = tmp_path / "invented-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: invented\n", encoding="utf-8")
    database = tmp_path / "outside.duckdb"
    archive = tmp_path / "outside-archive"
    if inside == "database":
        database = worktree / "private.duckdb"
    elif inside == "archive":
        archive = worktree / "private-archive"
    if inside == "source":
        with Store(database, archive) as store:
            original = worktree / "source.txt"
            original.write_bytes(b"invented forbidden original")
            with pytest.raises(ValueError):
                receipt(store, original)
    else:
        with pytest.raises(ValueError):
            with Store(database, archive):
                pass


def test_mapping_revision_preserves_prior_version_and_availability(storage, original):
    known = datetime(2025, 1, 7, 10, tzinfo=UTC)
    with Store(*storage) as store:
        batch = imported(store, original)
        first, second = store.create_package(), store.create_package()
        first_mapping = mapping(
            store,
            first,
            batch,
            validity_kind="scope",
            scope_key="release-a",
            available=Timing(
                lower=known,
                upper=known,
                precision="instant",
                basis="synthetic receipt",
                source_timezone="UTC",
            ),
        )
        second_mapping = store.add_identifier(
            second,
            namespace="varenummer",
            identifier_value="000007",
            evidence_import_id=batch,
            source_locator="invented!A2",
            mapping_version="map-v2",
            validity_kind="scope",
            scope_key="release-a",
        )
        assert second_mapping != first_mapping
        assert (
            store.resolve_identifier(
                "varenummer", "000007", mapping_version="map-v1", scope_key="release-a"
            )
            == first
        )
        assert (
            store.resolve_identifier(
                "varenummer", "000007", mapping_version="map-v2", scope_key="release-a"
            )
            == second
        )
        assert store._connection.execute(
            "SELECT available_lower, available_upper, available_precision "
            "FROM package_identifier WHERE mapping_id = ?",
            [first_mapping],
        ).fetchone() == (known, known, "instant")


def test_receipt_context_preserves_explicit_coverage_and_replacement(storage, original):
    with Store(*storage) as store:
        store.add_period(date(2025, 1, 6), date(2025, 1, 20))
        store.add_period(date(2025, 1, 20), date(2025, 2, 3))
        coverage = {
            "version": 1,
            "kind": "partial_extract",
            "fields": ["AIP"],
            "population": "invented",
        }
        replacement = {"version": 1, "kind": "patch", "keys": ["000007"]}
        delivery = receipt(
            store,
            original,
            original_uri="synthetic://edition-a",
            edition="edition-a",
            phase="preliminary",
            current_period=date(2025, 1, 6),
            target_period=date(2025, 1, 20),
            coverage=coverage,
            replacement=replacement,
        )
        row = store._connection.execute(
            "SELECT original_uri, edition, phase, current_period, target_period, "
            "coverage, replacement FROM source_delivery WHERE delivery_id=?",
            [delivery],
        ).fetchone()
        assert row[:5] == (
            "synthetic://edition-a",
            "edition-a",
            "preliminary",
            date(2025, 1, 6),
            date(2025, 1, 20),
        )
        assert json.loads(row[5]) == coverage
        assert json.loads(row[6]) == replacement


def test_caught_nested_failure_makes_whole_transaction_roll_back(storage):
    with Store(*storage) as store:
        with pytest.raises(FoundationError):
            with store.transaction():
                store.create_package()
                try:
                    with store.transaction():
                        store.create_package()
                        raise RuntimeError("synthetic inner failure")
                except RuntimeError:
                    pass
        assert store._connection.execute("SELECT count(*) FROM package").fetchone() == (0,)


def test_cli_initialization_registration_verification_and_sanitized_failure(
    storage, original, capsys
):
    from bef_redbird.foundation.__main__ import main

    base = ["--database", str(storage[0]), "--archive", str(storage[1])]
    assert main([*base, "init"]) == 0
    assert "migrations verified" in capsys.readouterr().out
    register = [*base, "register", str(original), "--source", "synthetic", "--receipt-key", "r1"]
    assert main(register) == 0
    first = json.loads(capsys.readouterr().out)
    assert str(UUID(first["delivery_id"])) == first["delivery_id"]
    assert main(register) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert main([*base, "verify"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] == 1
    assert report["missing"] == report["corrupt"] == 0
    missing = original.with_name("PRIVATE-SYNTHETIC-MARKER-NOT-TO-PRINT.txt")
    assert (
        main([*base, "register", str(missing), "--source", "synthetic", "--receipt-key", "r2"]) == 1
    )
    failure = capsys.readouterr()
    assert failure.out == ""
    assert "operation failed" in failure.err
    assert "PRIVATE-SYNTHETIC-MARKER" not in failure.err
    assert str(missing) not in failure.err
    assert "Traceback" not in failure.err


@pytest.mark.parametrize("status", ["running", "rejected", "failed"])
def test_mapping_from_unaccepted_interpretation_never_resolves(storage, original, status):
    with Store(*storage) as store:
        batch = store.begin_import(
            receipt(store, original),
            parser_version="p1",
            schema_version="s1",
            mapping_version="map-v1",
        )
        mapping(store, store.create_package(), batch, validity_kind="scope", scope_key="release-a")
        if status != "running":
            store.finish_import(
                batch, status=status, record_count=1, accepted_count=0, rejected_count=1
            )
        assert (
            store.resolve_identifier(
                "varenummer", "000007", mapping_version="map-v1", scope_key="release-a"
            )
            is None
        )


@pytest.mark.parametrize("status", ["rejected", "failed"])
def test_failed_mapping_interpretation_does_not_block_valid_replacement(storage, original, status):
    with Store(*storage) as store:
        delivery = receipt(store, original)
        bad_batch = store.begin_import(
            delivery, parser_version="p1", schema_version="s1", mapping_version="map-v1"
        )
        bad_mapping = mapping(
            store, store.create_package(), bad_batch, validity_kind="scope", scope_key="release-a"
        )
        store.finish_import(
            bad_batch, status=status, record_count=1, accepted_count=0, rejected_count=1
        )
        valid_batch = store.begin_import(
            delivery, parser_version="p2", schema_version="s1", mapping_version="map-v1"
        )
        valid_package = store.create_package()
        valid_mapping = mapping(
            store, valid_package, valid_batch, validity_kind="scope", scope_key="release-a"
        )
        store.finish_import(
            valid_batch, status="accepted", record_count=1, accepted_count=1, rejected_count=0
        )
        assert (
            store.resolve_identifier(
                "varenummer", "000007", mapping_version="map-v1", scope_key="release-a"
            )
            == valid_package
        )
        assert {
            row[0]
            for row in store._connection.execute(
                "SELECT CAST(mapping_id AS VARCHAR) FROM package_identifier"
            ).fetchall()
        } == {bad_mapping, valid_mapping}


def test_missing_archive_object_is_recreated_from_exact_original_retry(storage, original):
    with Store(*storage) as store:
        delivery = receipt(store, original)
        archived = store.original_path(delivery)
        archived.chmod(0o600)
        archived.unlink()
        assert store.verify_archive()["missing"] == 1
        assert receipt(store, original) == delivery
        assert store.original_path(delivery).read_bytes() == b"invented fixture\n000007\n"
        assert store.verify_archive()["missing"] == 0
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (1,)


def test_failing_migration_rolls_back_schema_and_ledger(storage, tmp_path, monkeypatch):
    from bef_redbird.foundation import migrations

    with Store(*storage) as store:
        package = store.create_package()
        before = store._connection.execute(
            "SELECT version, name, checksum FROM schema_migration ORDER BY version"
        ).fetchall()
    failing_sql = tmp_path / "synthetic-failing-migration.sql"
    failing_sql.write_text(
        "CREATE TABLE synthetic_uncommitted_migration (value INTEGER); "
        "SELECT * FROM synthetic_nonexistent_table;",
        encoding="utf-8",
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            migrations,
            "MIGRATIONS",
            (*migrations.MIGRATIONS, (2, "synthetic_failure", failing_sql)),
        )
        with pytest.raises(duckdb.Error):
            with Store(*storage):
                pass
    with Store(*storage) as store:
        assert (
            store._connection.execute(
                "SELECT version, name, checksum FROM schema_migration ORDER BY version"
            ).fetchall()
            == before
        )
        assert store._connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'synthetic_uncommitted_migration'"
        ).fetchone() == (0,)
        assert store.create_package(package) == package
