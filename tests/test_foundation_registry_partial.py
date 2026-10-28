"""Incomplete loose originals retain typed references and explicit missing prices."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from bef_redbird.foundation import ArchiveError, Store, migrations
from bef_redbird.foundation import lms_registry_partial as partial
from bef_redbird.foundation.lms_registry import ingest_registry
from bef_redbird.foundation.records import Timing
from tests.test_foundation_lms_ftp import delivery_bytes, members


@pytest.fixture(autouse=True)
def partial_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    # No production migration registration while the main backfill remains on9.
    path = Path(partial.__file__).with_name("010_partial_registry.sql")
    if not any(version == 10 for version, _, _ in migrations.MIGRATIONS):
        monkeypatch.setattr(
            migrations, "MIGRATIONS", (*migrations.MIGRATIONS, (10, "partial_registry", path))
        )


def native(width: int, *cells: tuple[int, bytes]) -> bytes:
    result = bytearray(b" " * width)
    for first, value in cells:
        result[first - 1 : first - 1 + len(value)] = value
    assert len(result) == width
    return bytes(result) + b"\r\n"


def loose_sources() -> dict[int, bytes]:
    # Independent source recipe: one declared price source is empty, another
    # declared member is absent, and a nonempty member has fewer rows than SYSTEM.
    files = {
        1: native(180, (1, b"28100000001"), (30, b"Synthetic medicine")),
        2: native(
            137,
            (1, b"28100000001"),
            (12, b"000042"),
            (30, b"20 tablets"),
            (104, b"20000102"),
            (112, b"00000000"),
            (120, b"20230229"),
        ),
        3: b"",
        4: native(47, (1, b"0042"), (5, b"000042"), (35, b"B")),
        10: native(69, (1, b"28100000001"), (12, b"20070101"), (20, b"Prior \xf3 name")) * 2,
        24: native(
            60,
            (1, b"28100000001"),
            (12, b"000042"),
            (18, b"0042"),
            (22, b"000012345"),
            (31, b"999999999"),
            (40, b"000000000"),
            (49, b"A"),
            (50, b"28100000001"),
        ),
    }
    counts = {0: 10, 1: 1, 2: 1, 3: 2, 4: 3, 10: 2, 24: 1, 35: 1}
    control = native(
        93,
        (1, b"00"),
        (3, b"11.0 "),
        (8, b"LMS-TAKST"),
        (48, b"20080521"),
        (56, b"20080505"),
        (64, b"20080602"),
        (72, b"01"),
        (74, b"08"),
        (76, b"LMS.ZIP"),
        (88, b"200821"),
    )
    parameters = (
        b"01"
        + b"".join(f"{n:05d}".encode() for n in [2500, 377, 1234, 5678, 0, 0, 9, 0, 0])
        + b"\r\n"
    )
    catalogue = b"".join(
        native(
            51,
            (1, b"10"),
            (3, f"Synthetic {number}".encode()),
            (33, ("SYSTEM.TXT" if number == 0 else f"LMS{number:02}.TXT").encode()),
            (45, b"01"),
            (47, f"{count:05}".encode()),
        )
        for number, count in counts.items()
    )
    files[0] = control + parameters + catalogue
    return files


def registered_group(
    store: Store,
    directory: Path,
    *,
    changed: dict[int, bytes] | None = None,
    uri_override: tuple[int, str] | None = None,
    receipt: str = "group",
) -> tuple[str, dict[int | str, str]]:
    files = {**loose_sources(), **(changed or {})}
    components: dict[int | str, str] = {}
    for number, data in files.items():
        name = "SYSTEM.TXT" if number == 0 else f"LMS{number:02}.TXT"
        path = directory / name
        path.write_bytes(data)
        uri = "ftp://synthetic.invalid/LMS/2008/20080521/" + name
        if uri_override and uri_override[0] == number:
            uri = uri_override[1]
        components[number] = store.register_source(
            path,
            source="synthetic-native-loose",
            receipt_key=f"{receipt}-{number}",
            original_uri=uri,
        )
    manifest = partial.partial_registry_manifest(
        components,
        phase="final",
        context={
            "partial_reason": "native ZIP and price member are empty on the server",
            "remote_directory": "/LMS/2008/20080521",
            "source_role": "registry_partial",
        },
    )
    path = directory / f"{receipt}.json"
    path.write_bytes(manifest)
    delivery = store.register_source(path, source=partial.MANIFEST_SOURCE, receipt_key=receipt)
    return delivery, components


def test_partial_fields_system_and_explicit_price_gap(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        manifest, components = registered_group(store, tmp_path)
        result = partial.ingest_partial_registry(store, manifest)
        assert result["status"] == "accepted" and result["independently_verified"]
        assert result["complete_registry"] is False and result["canonical_eligible"] is False
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (
            0,
        )
        assert store._connection.execute("SELECT count(*) FROM registry_lms03").fetchone() == (0,)
        coverage = {row["member_number"]: row for row in result["coverage"]["members"]}
        assert coverage[3] == {
            "member_number": 3,
            "native_name": "lms03.txt",
            "declared_records": 2,
            "available_records": 0,
            "status": "empty_unavailable",
            "count_matches": False,
        }
        assert coverage[4]["available_records"] == 1 and not coverage[4]["count_matches"]
        assert coverage[35]["status"] == "missing" and coverage[35]["available_records"] is None
        prices = partial.partial_reference_member(store, result["import_id"], 3)
        assert prices["rows"] == [] and prices["status"] == "empty_unavailable"
        assert prices["source_delivery_id"] == components[3]
        package = partial.partial_reference_member(store, result["import_id"], 2)["rows"][0]
        assert package["package_number"] == "000042" and package["pack_description"] == "20 tablets"
        assert package["creation_date"].isoformat() == "2000-01-02"
        assert json.loads(package["field_states"])["last_price_change_date"] == "unknown_date"
        assert json.loads(package["field_states"])["exit_date"] == "invalid"
        old_names = partial.partial_reference_member(store, result["import_id"], 10)["rows"]
        assert len(old_names) == 2 and old_names[0]["row_id"] == old_names[1]["row_id"]
        assert old_names[0]["previous_name"] is None
        assert json.loads(old_names[0]["field_states"])["previous_name"] == "ambiguous_encoding"
        dose = partial.partial_reference_member(store, result["import_id"], 24)["rows"][0]
        assert (
            dose["minimum_aip_unit_ore"],
            dose["minimum_registered_unit_ore"],
            dose["reimbursement_unit_ore"],
        ) == (12345, 999999999, 0)
        system = partial.partial_reference_member(store, result["import_id"], 0)["system"]
        assert system["schema_version"] == "11.0" and system["effective_token"] == "20080521"
        assert system["period_week_token"] == "200821" and system["publication_status"] == "unknown"
        assert system["previous_effective_token"] == "20080505"
        assert system["next_effective_token"] == "20080602"
        assert system["parameter_slots"] == [2500, 377, 1234, 5678, 0, 0, 9, 0, 0]
        assert system["catalogue"]["lms03.txt"]["declared_records"] == 2
        assert result["source_records"] == 16


def test_idempotent_retry_and_new_receipt_reuse_values(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        manifest, _ = registered_group(store, tmp_path)
        first = partial.ingest_partial_registry(store, manifest)
        rows = store._connection.execute("SELECT count(*) FROM registry_member").fetchone()
        second = partial.ingest_partial_registry(store, manifest)
        assert second["retry"] and first["evidence_hash"] == second["evidence_hash"]
        other, _ = registered_group(store, tmp_path, receipt="another")
        third = partial.ingest_partial_registry(store, other)
        assert third["import_id"] != first["import_id"]
        assert store._connection.execute("SELECT count(*) FROM registry_member").fetchone() == rows


def test_partial_publication_rolls_back_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        manifest, _ = registered_group(store, tmp_path)
        original = partial._insert_member

        def interrupted(store: Store, number: int, data: bytes, profile: str) -> int:
            if number == 4:
                raise OSError("synthetic interrupted partial import")
            return original(store, number, data, profile)

        monkeypatch.setattr(partial, "_insert_member", interrupted)
        with pytest.raises(OSError, match="synthetic interrupted"):
            partial.ingest_partial_registry(store, manifest)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM registry_member").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM registry_lms01").fetchone() == (0,)
        monkeypatch.setattr(partial, "_insert_member", original)
        assert partial.ingest_partial_registry(store, manifest)["status"] == "accepted"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("UPDATE registry_lms02 SET pack_description='corrupted'", "partial_typed_reconciliation"),
        ("UPDATE registry_lms02 SET profile='corrupted'", "partial_row_profile"),
        (
            "UPDATE registry_lms24 SET minimum_aip_unit_ore=minimum_aip_unit_ore+1",
            "partial_typed_reconciliation",
        ),
        (
            "UPDATE registry_partial_group SET verification_report='{}'",
            "partial_cached_proof_changed",
        ),
        ("UPDATE registry_partial_group SET tariff_week='200822'", "partial_group_lineage"),
        (
            "UPDATE import_batch SET accepted_count=accepted_count-1,record_count=record_count-1",
            "partial_import_counts",
        ),
    ],
)
def test_cached_proof_never_hides_mutation(tmp_path: Path, mutation: str, reason: str) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        manifest, _ = registered_group(store, tmp_path)
        result = partial.ingest_partial_registry(store, manifest)
        assert partial.verify_partial_registry(store, result["import_id"])["independently_verified"]
        store._connection.execute(mutation)
        with pytest.raises(partial.PartialRegistryError, match=reason):
            partial.ingest_partial_registry(store, manifest)


def test_reused_corrupt_dictionary_blocks_new_group_atomically(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        manifest, _ = registered_group(store, tmp_path)
        partial.ingest_partial_registry(store, manifest)
        store._connection.execute("UPDATE registry_lms01 SET name='tampered shared dictionary'")
        new, _ = registered_group(store, tmp_path, receipt="new")
        with pytest.raises(partial.PartialRegistryError, match="partial_typed_reconciliation"):
            partial.ingest_partial_registry(store, new)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "uri,reason",
    [
        ("ftp://synthetic.invalid/LMS/2008/20080521/LMS01.TXT", "partial_native_uri_member"),
        (
            "ftp://synthetic.invalid/LMS/2008/20080522/LMS02.TXT",
            "partial_native_directory_conflict",
        ),
    ],
)
def test_loose_component_names_and_directory_are_bound_to_original_uri(
    tmp_path: Path, uri: str, reason: str
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        manifest, _ = registered_group(store, tmp_path, uri_override=(2, uri))
        with pytest.raises(partial.PartialRegistryError, match=reason):
            partial.ingest_partial_registry(store, manifest)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)


def test_sql_prevents_partial_canonical_eligibility(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        manifest, _ = registered_group(store, tmp_path)
        partial.ingest_partial_registry(store, manifest)
        with pytest.raises(duckdb.ConstraintException):
            store._connection.execute("UPDATE registry_partial_group SET canonical_eligible=TRUE")


def test_malformed_available_member_is_not_hidden_as_missing(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        manifest, _ = registered_group(store, tmp_path, changed={4: native(46)})
        with pytest.raises(partial.RegistryError, match="native_record_width"):
            partial.ingest_partial_registry(store, manifest)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)


def test_manifest_context_and_system_phase_contract() -> None:
    with pytest.raises(partial.PartialRegistryError, match="partial_reason_required"):
        partial.partial_registry_manifest({}, phase="final", context={})
    context: dict[str, Any] = {"partial_reason": "synthetic gap", "source_role": "complete"}
    with pytest.raises(partial.PartialRegistryError, match="partial_source_role"):
        partial.partial_registry_manifest({}, phase="final", context=context)


def rejected_zip(
    store: Store, directory: Path, *, malformed: bool = False, framed: bool = False
) -> tuple[str, str]:
    files = members()
    files["lms04.txt"] = native(46 if malformed else 47, (1, b"0042"), (5, b"000007"))
    if framed:
        files["lms17.txt"] = b" " * 55 + b"alpha\r\nbeta".ljust(500) + b"\r\n"
    source = delivery_bytes(files)
    if not malformed:
        output = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(source)) as original,
            zipfile.ZipFile(output, "w") as target,
        ):
            for name in original.namelist():
                payload = original.read(name)
                if name == "system.txt":
                    rows = payload[:-2].split(b"\r\n")
                    rows = [
                        row[:-5] + b"00000" if row[32:44].strip().lower() == b"lms04.txt" else row
                        for row in rows
                    ]
                    payload = b"\r\n".join(rows) + b"\r\n"
                target.writestr(name, payload)
        source = output.getvalue()
    path = directory / "lms.zip"
    path.write_bytes(source)
    delivery = store.register_source(
        path,
        source="synthetic-native-zip",
        receipt_key="broken-full",
        original_uri="ftp://synthetic.invalid/LMS/20261013/lms.zip",
        phase="final",
        received=Timing(
            datetime(2026, 10, 22, tzinfo=UTC),
            datetime(2026, 10, 22, tzinfo=UTC),
            "instant",
            "synthetic acquisition",
            "UTC",
        ),
    )
    rejected = ingest_registry(store, delivery, phase="final", context={})
    assert rejected["status"] == "rejected"
    return delivery, rejected["import_id"]


def test_partial_zip_keeps_count_mismatched_records_and_exact_derived_provenance(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        parent, rejected = rejected_zip(store, tmp_path)
        immutable_rejection = store.get_record("import_batch", rejected)
        first = partial.ingest_partial_zip_registry(
            store,
            parent,
            phase="final",
            reason="system_record_count",
            rejected_import_ids=[rejected],
        )
        assert first["independently_verified"] and first["canonical_eligible"] is False
        row = partial.partial_reference_member(store, first["import_id"], 4)
        assert len(row["rows"]) == 1 and row["rows"][0]["package_number"] == "000007"
        coverage = next(item for item in first["coverage"]["members"] if item["member_number"] == 4)
        assert (
            coverage["declared_records"] == 0
            and coverage["available_records"] == 1
            and not coverage["count_matches"]
        )
        price = partial.partial_reference_member(store, first["import_id"], 3)["rows"][0]
        assert price["aip_ore"] == 12345
        manifest = json.loads(store.original_path(first["manifest_delivery_id"]).read_bytes())
        context = manifest["context"]
        assert context["parent_zip"]["source_delivery_id"] == parent
        assert context["parent_zip"]["received_evidence"]["received_precision"] == "instant"
        assert context["parent_zip"]["received_evidence"]["received_lower"].startswith("2026-10-22")
        assert context["rejected_import_ids"] == [rejected]
        with zipfile.ZipFile(store.original_path(parent)) as archive:
            for child in context["derived_children"]:
                metadata = store.get_record("source_delivery", child["source_delivery_id"])
                assert metadata["source"] == partial.ZIP_MEMBER_SOURCE
                assert metadata["received_precision"] == "unknown"
                assert (
                    metadata["original_uri"]
                    == "ftp://synthetic.invalid/LMS/20261013/lms.zip!/" + child["exact_member_name"]
                )
                assert store.original_path(
                    child["source_delivery_id"]
                ).read_bytes() == archive.read(child["exact_member_name"])
        before = store._connection.execute("SELECT count(*) FROM source_delivery").fetchone()
        again = partial.ingest_partial_zip_registry(
            store,
            parent,
            phase="final",
            reason="system_record_count",
            rejected_import_ids=[rejected],
        )
        assert again["retry"] and again["import_id"] == first["import_id"]
        assert (
            store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == before
        )
        assert store.get_record("import_batch", rejected) == immutable_rejection
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)
        assert not list((tmp_path / "archive" / ".staging").iterdir())


def test_partial_zip_malformed_member_stays_raw_and_other_fields_remain_verified(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        parent, rejected = rejected_zip(store, tmp_path, malformed=True)
        result = partial.ingest_partial_zip_registry(
            store,
            parent,
            phase="final",
            reason="unsupported_member_layout",
            rejected_import_ids=[rejected],
        )
        gap = partial.partial_reference_member(store, result["import_id"], 4)
        assert gap["status"] == "unsupported" and gap["rows"] == []
        assert gap["unsupported_reason"] == "unsupported_member_layout"
        assert (
            partial.partial_reference_member(store, result["import_id"], 2)["rows"][0][
                "package_number"
            ]
            == "000007"
        )
        assert result["unsupported_members"][0]["byte_count"] == 48
        children = json.loads(store.original_path(result["manifest_delivery_id"]).read_bytes())[
            "context"
        ]["derived_children"]
        assert 4 not in {child["member_number"] for child in children}
        assert store._connection.execute("SELECT count(*) FROM registry_lms04").fetchone() == (0,)
        assert partial.verify_partial_registry(store, result["import_id"])["independently_verified"]
        store.original_path(parent).write_bytes(b"corrupted original parent")
        with pytest.raises(ArchiveError, match="Corrupt archive"):
            partial.verify_partial_registry(store, result["import_id"])


def test_partial_zip_unknown_system_is_not_admitted(tmp_path: Path) -> None:
    source = tmp_path / "lms.zip"
    source.write_bytes(delivery_bytes(system_change=(2, b"99.0 ")))
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        parent = store.register_source(
            source,
            source="synthetic-native-zip",
            receipt_key="unknown",
            original_uri="ftp://synthetic.invalid/lms.zip",
        )
        rejected = ingest_registry(store, parent, phase="final", context={})
        with pytest.raises(partial.PartialRegistryError, match="partial_system_contract"):
            partial.ingest_partial_zip_registry(
                store,
                parent,
                phase="final",
                reason="unsupported_member_layout",
                rejected_import_ids=[rejected["import_id"]],
            )
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)


def test_partial_zip_independently_reconciles_fixed_width_text_with_embedded_crlf(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        parent, rejected = rejected_zip(store, tmp_path, framed=True)
        result = partial.ingest_partial_zip_registry(
            store,
            parent,
            phase="final",
            reason="system_record_count",
            rejected_import_ids=[rejected],
        )
        rows = partial.partial_reference_member(store, result["import_id"], 17)["rows"]
        assert len(rows) == 1 and rows[0]["description"] == "alpha\r\nbeta"
        coverage = next(
            item for item in result["coverage"]["members"] if item["member_number"] == 17
        )
        assert coverage["declared_records"] == 2 and coverage["available_records"] == 1
        store._connection.execute("UPDATE registry_lms17 SET description='tampered'")
        with pytest.raises(partial.PartialRegistryError, match="partial_typed_reconciliation"):
            partial.verify_partial_registry(store, result["import_id"])


def preliminary_zip(*, common_count_mismatch: bool = False) -> bytes:
    """Invented native shape: final SYSTEM catalogue retained in preliminary ZIP."""
    declared = members()
    declared["lms24.txt"] = native(60)
    declared["lms32.txt"] = native(30) * 2
    final = delivery_bytes(declared, release=b"20261026", week=b"202644")
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(final)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            if name in {"lms03.txt", "lms24.txt", "lms31.txt", "lms32.txt"}:
                continue
            data = source.read(name)
            if common_count_mismatch and name == "system.txt":
                data = (
                    b"\r\n".join(
                        row[:-5] + b"00003" if row[32:44].strip() == b"lms02.txt" else row
                        for row in data[:-2].split(b"\r\n")
                    )
                    + b"\r\n"
                )
            target.writestr(name, data)
        target.writestr(
            "lms33.txt",
            native(30, (1, b"000007"), (7, b"000008"), (21, b"999999999")) * 2,
        )
        target.writestr(
            "lms38.txt",
            native(60, (1, b"28101234567"), (12, b"000007"), (22, b"999999999"), (49, b"A")),
        )
    return output.getvalue()


def register_preliminary(store: Store, directory: Path, data: bytes) -> tuple[str, str]:
    source = directory / "preliminary.zip"
    source.write_bytes(data)
    parent = store.register_source(
        source,
        source="synthetic-native-zip",
        receipt_key="preliminary",
        original_uri="ftp://synthetic.invalid/LMS/Foreløbig/lms-gros.zip",
        phase="preliminary",
    )
    rejected = ingest_registry(store, parent, phase="preliminary", context={})
    assert rejected["reason"] == "release_phase_conflict"
    return parent, rejected["import_id"]


@pytest.mark.parametrize("common_count_mismatch", [False, True])
def test_preliminary_all_actual_members_keep_unknown_declarations_and_missing_prices(
    tmp_path: Path, common_count_mismatch: bool
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        parent, rejected = register_preliminary(
            store, tmp_path, preliminary_zip(common_count_mismatch=common_count_mismatch)
        )
        original_rejection = store.get_record("import_batch", rejected)
        result = partial.ingest_partial_zip_registry(
            store,
            parent,
            phase="preliminary",
            reason="release_phase_conflict",
            rejected_import_ids=[rejected],
        )
        assert result["independently_verified"] and not result["complete_registry"]
        assert not result["canonical_eligible"]
        coverage = {row["member_number"]: row for row in result["coverage"]["members"]}
        assert len(coverage) == 35  # 33 declarations plus2 actual undeclared members.
        control = result["coverage"]["control_inventory"]
        assert control["declared_member_count"] == 33 and control["actual_member_count"] == 31
        assert control["actual_undeclared_members"] == ["lms33.txt", "lms38.txt"]
        for number in (3, 24, 31, 32):
            assert coverage[number]["status"] == "missing"
            assert coverage[number]["available_records"] is None
            assert (
                partial.partial_reference_member(store, result["import_id"], number)["rows"] == []
            )
        for number, count in ((33, 2), (38, 1)):
            assert coverage[number]["declared_records"] is None
            assert coverage[number]["declaration_state"] == "undeclared"
            assert coverage[number]["available_records"] == count
            assert coverage[number]["count_matches"] is None
            assert (
                len(partial.partial_reference_member(store, result["import_id"], number)["rows"])
                == count
            )
        assert coverage[2]["count_matches"] is not common_count_mismatch
        row33 = partial.partial_reference_member(store, result["import_id"], 33)["rows"][0]
        row38 = partial.partial_reference_member(store, result["import_id"], 38)["rows"][0]
        assert row33["reserved_price_token"] == "999999999"
        assert row38["reserved_minimum_aip_unit_ore"] == "999999999"
        assert store._connection.execute("SELECT count(*) FROM registry_lms03").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM registry_release").fetchone() == (0,)
        assert store._connection.execute(
            "SELECT member_number,declared_records FROM registry_partial_member "
            "WHERE import_id=? AND member_number IN (33,38) ORDER BY member_number",
            [result["import_id"]],
        ).fetchall() == [(33, None), (38, None)]
        assert store.get_record("import_batch", rejected) == original_rejection
        assert (
            partial.verify_partial_registry(store, result["import_id"])["evidence_hash"]
            == result["evidence_hash"]
        )


@pytest.mark.parametrize(
    "mutation", ["typed", "null_declaration", "invented_declaration", "omission"]
)
def test_preliminary_verifier_rejects_mutations(tmp_path: Path, mutation: str) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        parent, rejected = register_preliminary(store, tmp_path, preliminary_zip())
        result = partial.ingest_partial_zip_registry(
            store,
            parent,
            phase="preliminary",
            reason="release_phase_conflict",
            rejected_import_ids=[rejected],
        )
        if mutation == "typed":
            store._connection.execute("UPDATE registry_lms38 SET abc_code='B'")
        elif mutation == "null_declaration":
            store._connection.execute(
                "UPDATE registry_partial_member SET declared_records=NULL WHERE member_number=2"
            )
        elif mutation == "invented_declaration":
            store._connection.execute(
                "UPDATE registry_partial_member SET declared_records=2 WHERE member_number=33"
            )
        else:
            store._connection.execute("DELETE FROM registry_partial_member WHERE member_number=33")
        with pytest.raises(partial.PartialRegistryError):
            partial.verify_partial_registry(store, result["import_id"])


def test_preliminary_variant_cannot_be_admitted_with_final_phase(tmp_path: Path) -> None:
    path = tmp_path / "native.zip"
    path.write_bytes(preliminary_zip())
    with pytest.raises(partial.PartialRegistryError, match="partial_zip_system_inventory"):
        partial._inspect_partial_zip(path, "final")


def test_new_partial_derivation_preserves_old_proof_and_requires_historical_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = partial.PARTIAL_PROFILE
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery, _ = registered_group(store, tmp_path)
        monkeypatch.setattr(partial, "PARTIAL_PROFILE", "lms-native-partial-v2")
        old = partial.ingest_partial_registry(store, delivery)
        before = store._connection.execute(
            "SELECT * FROM registry_partial_group WHERE import_id=?", [old["import_id"]]
        ).fetchone()
        monkeypatch.setattr(partial, "PARTIAL_PROFILE", current)
        new = partial.ingest_partial_registry(store, delivery)
        assert new["import_id"] != old["import_id"]
        assert (
            store._connection.execute(
                "SELECT * FROM registry_partial_group WHERE import_id=?", [old["import_id"]]
            ).fetchone()
            == before
        )
        with pytest.raises(
            partial.PartialRegistryError, match="partial_historical_reader_required"
        ):
            partial.verify_partial_registry(store, old["import_id"])
        assert partial.ingest_partial_registry(store, delivery)["import_id"] == new["import_id"]


def test_unknown_declaration_migration_preserves_existing_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", full[:-1])
    database, archive = tmp_path / "store.duckdb", tmp_path / "archive"
    with Store(database, archive) as store:
        delivery, _ = registered_group(store, tmp_path)
        result = partial.ingest_partial_registry(store, delivery)
        before = store._connection.execute(
            "SELECT * FROM registry_partial_member ORDER BY member_number"
        ).fetchall()
        proof = store._connection.execute(
            "SELECT evidence_hash FROM registry_partial_group"
        ).fetchone()
    monkeypatch.setattr(migrations, "MIGRATIONS", full)
    with Store(database, archive) as store:
        assert (
            store._connection.execute(
                "SELECT * FROM registry_partial_member ORDER BY member_number"
            ).fetchall()
            == before
        )
        assert (
            store._connection.execute("SELECT evidence_hash FROM registry_partial_group").fetchone()
            == proof
        )
        assert partial.verify_partial_registry(store, result["import_id"])["independently_verified"]
        nullable = store._connection.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='registry_partial_member' AND column_name='declared_records'"
        ).fetchone()
        assert nullable == ("YES",)
        with pytest.raises(duckdb.ConstraintException):
            store._connection.execute(
                "UPDATE registry_partial_member SET declared_records=-1 WHERE member_number=2"
            )


def test_preliminary_partial_failure_rolls_back_typed_members_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        parent, rejected = register_preliminary(store, tmp_path, preliminary_zip())
        original = partial._insert_member

        def fail_late(*args: Any, **kwargs: Any) -> int:
            if args[1] == 33:
                raise OSError("synthetic preliminary interruption")
            return original(*args, **kwargs)

        monkeypatch.setattr(partial, "_insert_member", fail_late)
        with pytest.raises(OSError, match="synthetic preliminary interruption"):
            partial.ingest_partial_zip_registry(
                store,
                parent,
                phase="preliminary",
                reason="release_phase_conflict",
                rejected_import_ids=[rejected],
            )
        assert store._connection.execute("SELECT count(*) FROM registry_member").fetchone() == (0,)
        assert store._connection.execute(
            "SELECT count(*) FROM registry_partial_group"
        ).fetchone() == (0,)
        monkeypatch.setattr(partial, "_insert_member", original)
        assert partial.ingest_partial_zip_registry(
            store,
            parent,
            phase="preliminary",
            reason="release_phase_conflict",
            rejected_import_ids=[rejected],
        )["independently_verified"]
