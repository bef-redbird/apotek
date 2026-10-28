"""Independent source expectations and adversarial reconciliation checks."""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import duckdb
import pytest

from bef_redbird.foundation import Store, Timing, source_audit
from bef_redbird.foundation.source_audit import (
    compare_baseline,
    digest,
    expected_report,
    generated_id,
    reconcile,
    snapshot,
)
from tests.test_foundation_reports import STOP, changes_rows, intro_rows, supply_xls, workbook

PRICE = {"tax_basis": "unknown", "fee_basis": "unknown"}
CURRENT = date(2026, 1, 5)


def test_baseline_preserves_composite_keys_and_nested_identifiers(tmp_path: Path) -> None:
    database = tmp_path / "synthetic.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute(
            "CREATE TABLE evidence_01 (edition INTEGER, member INTEGER, value UUID[], "
            "PRIMARY KEY(edition,member))"
        )
        con.execute(
            "INSERT INTO evidence_01 VALUES (1,1,['00000000-0000-0000-0000-000000000001']),"
            "(1,2,['00000000-0000-0000-0000-000000000002'])"
        )
    baseline = snapshot(database)
    assert compare_baseline(database, baseline)["preserved"]
    with duckdb.connect(str(database)) as con:
        con.execute("UPDATE evidence_01 SET value=[] WHERE edition=1 AND member=2")
        con.execute("INSERT INTO evidence_01 VALUES(2,2,[])")
    report = compare_baseline(database, baseline)
    assert not report["preserved"]
    assert report["tables"]["evidence_01"] == {"missing_or_changed": 1, "additional": 2}


def make_store(tmp_path: Path, reports: list[tuple[str, bytes, dict[str, Any]]]) -> Path:
    database = tmp_path / "source.duckdb"
    with Store(database, tmp_path / "archive") as store:
        store.add_period(CURRENT, date(2026, 1, 19))
        for index, (adapter, data, context) in enumerate(reports):
            source = tmp_path / f"input-{index}.xlsx"
            source.write_bytes(data)
            delivery = store.register_source(
                source, source="synthetic", receipt_key=str(index), current_period=CURRENT
            )
            store.ingest(
                delivery,
                adapter=adapter,
                version=adapter + "-workbook-v1",
                mapping_version="synthetic-v1",
                mapping_policy="delivery",
                context=context,
            )
    return database


def test_independent_money_missingness_identifiers_and_context() -> None:
    data = changes_rows(new="12,345")
    data.append(
        ["Holder", "Other", "tablet", "5 mg", "000001", "20 stk", 0, None, "1.000,01", "bad", None]
    )
    result = expected_report(
        workbook(data), "price-changes", {**PRICE, "current_period": "2026-01-05"}
    )
    prices = [a for a in result["assertions"] if a["table"] == "price_observation"]
    assert len(prices) == 8
    assert [(a["values"]["amount_ore"], a["presence"]["amount_ore"]) for a in prices] == [
        (None, "invalid"),
        (140025, "present"),
        (1001, "present"),
        (1230, "present"),
        (0, "present"),
        (None, "blank"),
        (100001, "present"),
        (None, "invalid"),
    ]
    assert prices[-1]["canonical_identifier"] == "000001"
    assert prices[-1]["source_locator"] == "sheet:Synthetic!J6"
    assert prices[-1]["effective_period"] is None
    assert prices[-1]["report_period"] == "2026-01-05"
    assert [(i["code"], i["source_locator"]) for i in result["issues"]] == [
        ("unsupported_money_precision", "sheet:Synthetic!G5"),
        ("malformed_money", "sheet:Synthetic!J6"),
    ]


def test_all_actual_report_contracts_and_all_persisted_fields(tmp_path: Path) -> None:
    stop = workbook(
        [
            STOP,
            [
                "Holder",
                "Medicine",
                "tablet",
                "5 mg",
                "000123",
                "20 stk",
                datetime(2020, 2, 3),
                datetime(2026, 1, 5),
            ],
        ]
    )
    database = make_store(
        tmp_path,
        [
            ("price-changes", workbook(changes_rows()), PRICE),
            ("introductions", workbook(intro_rows()), PRICE),
            ("discontinued", stop, {}),
            (
                "supply",
                supply_xls(),
                {
                    "date_basis": "source_effective_date",
                    "reported_status": "reported_disruption",
                    "wholesaler_namespace": "synthetic:wholesaler",
                },
            ),
        ],
    )
    before = database.read_bytes()
    report = reconcile(database, tmp_path / "archive")
    assert report["complete"]
    assert report["verified_count"] == 14
    assert report["quarantined_count"] == 0
    assert sum(i["expected_issues"] for i in report["imports"]) == 2
    assert database.read_bytes() == before
    baseline = snapshot(database)
    for table in (
        "price_observation",
        "package_observation",
        "supply_observation",
        "lifecycle_event",
    ):
        for row in baseline[table]["rows"]:
            assert report["observation_hashes"][row["observation_id"]] == digest(row)
    assert compare_baseline(database, baseline)["preserved"]


@pytest.mark.parametrize(
    "column,value",
    [
        ("amount_ore", 999),
        ("source_identifier", "123"),
        ("source_locator", "sheet:Synthetic!G99"),
        ("source_values", json.dumps({"tampered": "raw evidence"})),
        ("field_presence", json.dumps({"amount_ore": "blank"})),
        ("report_period", None),
    ],
)
def test_any_value_context_locator_or_raw_evidence_change_is_quarantined(
    tmp_path: Path, column: str, value: Any
) -> None:
    database = make_store(tmp_path, [("price-changes", workbook(changes_rows()), PRICE)])
    baseline = snapshot(database)
    with duckdb.connect(str(database)) as con:
        con.execute(
            f"UPDATE price_observation SET {column} = ? WHERE source_locator = ?",
            [value, "sheet:Synthetic!G5"],
        )
    report = reconcile(database, tmp_path / "archive")
    assert not report["complete"]
    assert report["quarantined_count"] == 1
    assert report["verified_count"] == 4
    assert not compare_baseline(database, baseline)["preserved"]


def test_duplicate_conflicts_and_invalid_identifier_rows_are_fully_accounted(
    tmp_path: Path,
) -> None:
    records = changes_rows()
    records.append([*records[-1][:6], "1,00", *records[-1][7:]])
    records.append([*records[-1][:4], "not-id", *records[-1][5:]])
    database = make_store(tmp_path, [("price-changes", workbook(records), PRICE)])
    report = reconcile(database, tmp_path / "archive")
    assert report["complete"]
    assert report["verified_count"] == 10
    assert report["imports"][0]["source_rows"] == 3
    assert report["imports"][0]["duplicate_business_key_groups"] == 5
    assert report["imports"][0]["expected_issues"] == 3


def test_archive_hash_failure_cannot_pass_even_if_workbook_still_opens(tmp_path: Path) -> None:
    database = make_store(tmp_path, [("price-changes", workbook(changes_rows()), PRICE)])
    member = next(p for p in (tmp_path / "archive" / "sha256").rglob("*") if p.is_file())
    member.write_bytes(member.read_bytes() + b"changed archive bytes")
    report = reconcile(database, tmp_path / "archive")
    assert not report["complete"]
    assert {m["kind"] for m in report["mismatches"]} == {"archive_hash", "archive_size"}


def test_unknown_independent_contract_is_explicit() -> None:
    with pytest.raises(ValueError, match="independent_contract_unavailable"):
        expected_report(workbook(changes_rows()), "cito", {})


@pytest.mark.parametrize("tariff_column", range(4, 12))
@pytest.mark.parametrize("run_row,run_column", [(2, 12), (2, 13), (3, 12)])
def test_independent_introduction_v2_metadata_positions_and_exact_evidence(
    tariff_column: int, run_row: int, run_column: int
) -> None:
    rows = intro_rows(header_row=6)
    rows[1] = [None] * 12
    rows[1][tariff_column] = "Takstdato: 05-01-2026"
    rows[run_row] = [None] * 14
    rows[run_row][run_column] = "Kørselsdato: 02-01-2026"
    result = expected_report(
        workbook(rows),
        "introductions",
        {**PRICE, "current_period": "2026-01-05"},
        parser_version="introductions-workbook-v2",
    )
    assert result["source_rows"] == 1
    assert result["metadata"]["tariff_date"] == "2026-01-05"
    assert result["metadata"]["run_date"] == "2026-01-02"
    assert result["metadata"]["tariff_date_locator"] == (
        f"sheet:Synthetic!{chr(ord('A') + tariff_column)}2"
    )
    assert result["metadata"]["run_date_locator"] == (
        f"sheet:Synthetic!{chr(ord('A') + run_column)}{run_row + 1}"
    )
    assert result["issues"] == []
    assert len(result["assertions"]) == 4
    assert result["assertions"][1]["values"]["amount_ore"] == 0
    assert result["assertions"][2]["values"]["amount_ore"] == 2005
    assert result["assertions"][3]["effective_date"] == "2026-01-05"
    assert all(a["source_identifier"] == "000123" for a in result["assertions"])
    assert all(
        a["raw"]["run_date_source"] == "Kørselsdato: 02-01-2026" for a in result["assertions"]
    )


def test_independent_v2_does_not_change_default_v1_metadata_contract() -> None:
    rows = intro_rows(header_row=6)
    rows[1] = [None] * 12
    rows[1][11] = "Takstdato: 05-01-2026"
    with pytest.raises(ValueError, match="independent_context_conflict"):
        expected_report(workbook(rows), "introductions", {**PRICE, "current_period": "2026-01-05"})
    rows[1][11] = None
    rows[1][9] = "Takstdato: 05-01-2026"
    rows[3] = [None] * 13
    rows[3][12] = "Kørselsdato: 02-01-2026"
    result = expected_report(
        workbook(rows), "introductions", {**PRICE, "current_period": "2026-01-05"}
    )
    assert "run_date" not in result["metadata"]


@pytest.mark.parametrize(
    "position,value,error",
    [
        ((1, 3), "Takstdato: 05-01-2026", "independent_unrecognized_metadata_location"),
        ((1, 9), "Kørselsdato: 02-01-2026", "independent_metadata_mismatch"),
        ((2, 12), "Takstdato: 05-01-2026", "independent_metadata_mismatch"),
        ((1, 9), "Takstdato: 19-01-2026", "independent_context_conflict"),
        ((1, 9), "=DATE(2026,1,5)", "independent_metadata_mismatch"),
        ((2, 12), "=DATE(2026,1,2)", "independent_metadata_mismatch"),
        ((2, 12), "#VALUE!", "independent_metadata_mismatch"),
        ((1, 10), "unrelated preamble text", "independent_metadata_mismatch"),
    ],
)
def test_independent_v2_rejects_unknown_locations_labels_conflicts_and_cell_types(
    position: tuple[int, int], value: str, error: str
) -> None:
    rows = intro_rows(header_row=6)
    row, column = position
    rows[row] = [*rows[row], *([None] * max(0, column + 1 - len(rows[row])))]
    rows[row][column] = value
    with pytest.raises(ValueError, match=error):
        expected_report(
            workbook(rows),
            "introductions",
            {**PRICE, "current_period": "2026-01-05"},
            parser_version="introductions-workbook-v2",
        )


def historical_stop_rows() -> list[list[Any]]:
    return [
        [datetime(2026, 1, 5)],
        [
            "Repræsentant",
            "Lægemiddel",
            "Lægemiddelform",
            "Styrke",
            "Varenummer",
            "RPk",
            "Stop (deaktiveret)",
            "Slet (udgår permanent)",
        ],
        ["Holder", "Medicine", "tablet", "5 mg", "000123", "20 stk", "03-02-2020", None],
    ]


def test_independent_discontinued_v2_keeps_unlabelled_date_undated_and_event_evidence() -> None:
    data = workbook(historical_stop_rows())
    result = expected_report(data, "discontinued", {}, parser_version="discontinued-workbook-v2")
    assert result["source_rows"] == 1
    assert result["header_row"] == 2
    assert result["metadata"] == {
        "unlabelled_date": "2026-01-05T00:00:00",
        "unlabelled_date_locator": "sheet:Synthetic!A1",
        "unlabelled_date_semantics": "unknown",
    }
    package, event = result["assertions"]
    assert package["report_period"] is None
    assert package["effective_period"] is None
    assert package["source_locator"] == "sheet:Synthetic!row:3"
    assert event["source_locator"] == "sheet:Synthetic!G3"
    assert event["effective_date"] == "2020-02-03"
    assert event["report_period"] is None
    assert event["values"] == {
        "event_type": "deactivation",
        "original_source_code": "Stop (deaktiveret)",
        "interpretation_status": "unknown",
    }
    assert event["raw"]["unlabelled_date"] == "2026-01-05T00:00:00"
    assert [(i["code"], i["source_locator"]) for i in result["issues"]] == [
        ("source_reported_event_date_semantics_unknown", "sheet:Synthetic!G3"),
        ("missing_event_date", "sheet:Synthetic!H3"),
    ]
    with pytest.raises(ValueError, match="independent_schema_mismatch"):
        expected_report(data, "discontinued", {})
    with pytest.raises(ValueError, match="independent_unestablished_report_period"):
        expected_report(
            data,
            "discontinued",
            {"current_period": "2026-01-05"},
            parser_version="discontinued-workbook-v2",
        )


@pytest.mark.parametrize("value", [None, "2026-01-05", "=DATE(2026,1,5)", datetime(2026, 1, 5, 12)])
def test_independent_discontinued_v2_requires_exact_typed_unlabelled_date(value: Any) -> None:
    rows = historical_stop_rows()
    rows[0][0] = value
    with pytest.raises(ValueError):
        expected_report(
            workbook(rows), "discontinued", {}, parser_version="discontinued-workbook-v2"
        )


def test_independent_v2_accounts_for_duplicate_invalid_and_blank_physical_rows() -> None:
    rows = intro_rows(header_row=6)
    rows += [rows[-1].copy(), [*rows[-1][:5], "invalid", *rows[-1][6:]], []]
    result = expected_report(
        workbook(rows),
        "introductions",
        {**PRICE, "current_period": "2026-01-05"},
        parser_version="introductions-workbook-v2",
    )
    assert result["source_rows"] == 3
    assert len(result["assertions"]) == 8
    assert [
        a["source_locator"] for a in result["assertions"] if a["table"] == "package_observation"
    ] == [
        "sheet:Synthetic!row:7",
        "sheet:Synthetic!row:8",
    ]
    assert [(i["code"], i["source_locator"]) for i in result["issues"]] == [
        ("invalid_identifier", "sheet:Synthetic!F9")
    ]


def test_independent_v2_unknown_version_is_quarantined() -> None:
    with pytest.raises(ValueError, match="independent_version_contract_unavailable"):
        expected_report(
            workbook(intro_rows()),
            "introductions",
            PRICE,
            parser_version="introductions-workbook-v999",
        )


def test_independent_reader_uses_actual_cells_when_xml_dimension_omits_event_column() -> None:
    original = workbook(
        [STOP, ["Holder", "Medicine", "tablet", "5 mg", "000123", "20 stk", None, "05-01-2026"]]
    )
    altered = io.BytesIO()
    with (
        ZipFile(io.BytesIO(original)) as source,
        ZipFile(altered, "w", ZIP_DEFLATED) as destination,
    ):
        for member in source.infolist():
            content = source.read(member)
            if member.filename == "xl/worksheets/sheet1.xml":
                content = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:G2"', content)
            destination.writestr(member, content)
    result = expected_report(altered.getvalue(), "discontinued", {})
    assert result["source_rows"] == 1
    assert len(result["assertions"]) == 2
    event = result["assertions"][1]
    assert event["source_locator"] == "sheet:Synthetic!H2"
    assert event["effective_date"] == "2026-01-05"
    assert event["values"]["event_type"] == "permanent_deletion"


@pytest.mark.parametrize("adapter", ["introductions", "discontinued"])
def test_independent_reconciliation_accepts_only_explicit_v2_derivations(
    tmp_path: Path, adapter: str
) -> None:
    rows = historical_stop_rows() if adapter == "discontinued" else intro_rows(header_row=6)
    context = {} if adapter == "discontinued" else PRICE
    current = None if adapter == "discontinued" else CURRENT
    if adapter == "introductions":
        rows[1] = [None] * 12
        rows[1][11] = "Takstdato: 05-01-2026"
        rows[3] = [None] * 13
        rows[3][12] = "Kørselsdato: 02-01-2026"
    source = tmp_path / "synthetic-source.xlsx"
    source.write_bytes(workbook(rows))
    database = tmp_path / "source.duckdb"
    with Store(database, tmp_path / "archive") as store:
        store.add_period(CURRENT, date(2026, 1, 19))
        delivery = store.register_source(
            source, source="synthetic", receipt_key="v2", current_period=current
        )
        store.ingest(
            delivery,
            adapter=adapter,
            version=adapter + "-workbook-v2",
            mapping_version="synthetic-v1",
            mapping_policy="delivery",
            context=context,
        )
    before = database.read_bytes()
    result = reconcile(database, tmp_path / "archive")
    assert result["complete"]
    assert result["verified_count"] == (2 if adapter == "discontinued" else 4)
    assert result["imports"][0]["source_rows"] == 1
    assert result["quarantined_count"] == 0
    assert database.read_bytes() == before


def test_generated_bounded_mapping_document_is_verified_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = make_store(tmp_path, [("price-changes", workbook(changes_rows()), PRICE)])
    derivation: dict[str, Any] = {
        "kind": "synthetic-shared-package",
        "value": {"identifier": "000123"},
    }
    package_id = generated_id(derivation["kind"], derivation["value"])
    timestamp = datetime(2026, 10, 20, 12, 1, 24, 589525, tzinfo=UTC)
    available = Timing(timestamp, timestamp, "instant", "synthetic actual receipt", "UTC")
    with Store(database, tmp_path / "archive") as store:
        source_rows = store._connection.execute(
            "SELECT observation_id,delivery_id FROM package_observation"
        ).fetchall()
        entry = {
            "package_id": package_id,
            "package_id_derivation": derivation,
            "canonical_identifier": "000123",
            "namespace": "dk:varenummer",
            "validity_kind": "scope",
            "scope_key": "synthetic-bounded",
            "mapping_version": "synthetic-bounded-v1",
            "source_locator": "entries:0",
            "supporting_observation_ids": [str(source_rows[0][0])],
            "available": {
                "lower": timestamp.isoformat(),
                "upper": timestamp.isoformat(),
                "precision": "instant",
                "basis": "synthetic actual receipt",
                "timezone": "UTC",
            },
        }
        document = {
            "version": 1,
            "kind": "f3_identity_evidence",
            "entries": [entry],
            "supporting_delivery_ids": [str(source_rows[0][1])],
        }
        path = tmp_path / "identity-evidence.json"
        path.write_text(json.dumps(document))
        delivery = store.register_source(
            path, source="synthetic-mapping", receipt_key="mapping", received=available
        )
        iid = store.begin_import(
            delivery,
            parser_version="f3-identity-evidence-v1",
            schema_version="f3-identity-evidence-v1",
            mapping_version="synthetic-bounded-v1",
            context={"version": 1, "kind": "identity_evidence"},
        )
        store.create_package(package_id)
        store.add_identifier(
            package_id,
            namespace="dk:varenummer",
            identifier_value="000123",
            validity_kind="scope",
            scope_key="synthetic-bounded",
            evidence_import_id=iid,
            source_locator="entries:0",
            mapping_version="synthetic-bounded-v1",
            available=available,
        )
        store.finish_import(
            iid, status="accepted", record_count=1, accepted_count=1, rejected_count=0
        )
    connect = duckdb.connect

    def local_timezone_connection(*args: Any, **kwargs: Any) -> duckdb.DuckDBPyConnection:
        connection = connect(*args, **kwargs)
        connection.execute("SET TimeZone='Europe/Copenhagen'")
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(duckdb, "connect", local_timezone_connection)
        report = reconcile(database, tmp_path / "archive")
    assert report["complete"]
    assert report["verified_count"] == 5
    assert len(report["mapping_hashes"]) == 2
    with duckdb.connect(str(database), read_only=True) as connection:
        connection.execute("SET TimeZone='UTC'")
        for table, key, hashes in (
            ("package_identifier", "mapping_id", report["mapping_hashes"]),
            (
                "import_batch",
                "import_id",
                {i["import_id"]: i["import_record_hash"] for i in report["imports"]},
            ),
        ):
            cursor = connection.execute(f"SELECT * FROM {table}")
            columns = [col[0] for col in cursor.description]
            for sql_row in cursor.fetchall():
                raw_record = dict(zip(columns, sql_row, strict=True))
                expected_hash = digest(json.loads(json.dumps(raw_record, default=str)))
                assert hashes[str(raw_record[key])] == expected_hash
    assert len(report["imports"]) == 2
    assert (
        next(i for i in report["imports"] if i["import_id"] == iid)["expected_mapping_assertions"]
        == 1
    )
    with duckdb.connect(str(database)) as con:
        con.execute(
            "UPDATE package_identifier SET source_locator='entries:wrong' "
            "WHERE evidence_import_id=?",
            [iid],
        )
    changed = reconcile(database, tmp_path / "archive")
    assert not changed["complete"]
    assert not changed["mapping_hashes"]
    assert any(m["kind"] == "mapping_all_fields" for m in changed["mismatches"])


def test_cli_success_and_failed_reconciliation_have_distinct_exit_statuses_and_safe_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = make_store(tmp_path, [("price-changes", workbook(changes_rows()), PRICE)])
    options = ["--database", str(database), "--archive", str(tmp_path / "archive")]
    accepted = tmp_path / "accepted-private-report.json"
    assert source_audit.main([*options, "--output", str(accepted)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "complete": True,
        "verified_count": 5,
        "quarantined_count": 0,
    }
    assert json.loads(accepted.read_text())["complete"]
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "UPDATE price_observation SET amount_ore=999 WHERE source_locator='sheet:Synthetic!G5'"
        )
    rejected = tmp_path / "rejected-private-report.json"
    assert source_audit.main([*options, "--output", str(rejected)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "complete": False,
        "verified_count": 4,
        "quarantined_count": 1,
    }
    assert not captured.err
    assert str(tmp_path) not in captured.out
    assert "Synthetic" not in captured.out
    assert json.loads(rejected.read_text())["mismatches"]


def test_cli_private_exception_detail_is_saved_only_outside_git(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    private_detail = f"private row token and path {tmp_path}"

    def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(private_detail)

    monkeypatch.setattr(source_audit, "reconcile", fail)
    output = tmp_path / "failure-private-report.json"
    assert (
        source_audit.main(
            [
                "--database",
                str(tmp_path / "source.duckdb"),
                "--archive",
                str(tmp_path / "archive"),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"complete": False, "error": "audit_failed"}
    assert private_detail not in captured.err
    assert json.loads(output.read_text())["audit_error"]["detail"] == private_detail


def test_cli_rejects_other_git_worktree_output_and_never_overwrites_private_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "another-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: elsewhere")
    options = [
        "--database",
        str(tmp_path / "source.duckdb"),
        "--archive",
        str(tmp_path / "archive"),
    ]

    def unexpected(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("Output safety must be checked before reading source data")

    monkeypatch.setattr(source_audit, "reconcile", unexpected)
    output = worktree / "private-report.json"
    assert source_audit.main([*options, "--output", str(output)]) == 2
    assert not output.exists()
    existing = tmp_path / "existing-private-report.json"
    existing.write_text("preserved")
    assert source_audit.main([*options, "--output", str(existing)]) == 2
    assert existing.read_text() == "preserved"
    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.out + captured.err
    assert "another-worktree" not in captured.out + captured.err


def test_cli_argument_errors_do_not_echo_private_tokens(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        source_audit.main(["private-argument-row-token"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "private-argument-row-token" not in captured.err
    assert captured.err == "source-audit: invalid arguments\n"
