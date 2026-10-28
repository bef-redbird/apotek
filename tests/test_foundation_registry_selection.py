"""Canonical FTP selection: independent synthetic values and explicit source choices."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from duckdb import ConstraintException

from bef_redbird.foundation import Store
from bef_redbird.foundation.lms_registry import (
    MISSING_FINAL_CRLF_PROFILE,
    ingest_registry,
    verify_registry,
)
from bef_redbird.foundation.records import FoundationError
from bef_redbird.foundation.registry_selection import (
    NATIVE_PRICE_VALUE_POLICY,
    POLICY,
    build_configuration,
    extract,
    native_price_value_contract,
    publish,
    query,
    status,
)
from tests.test_foundation_lms_ftp import delivery_bytes, members, populated

HISTORICAL_LEGACY_AUDITOR = {
    "source_audit.py": "a8e3270cec0c609d018cd7a2f64856f7e444f797edf9a54ba73885179dcbed21",
    "rtf_audit.py": "45f3634a1a337fbbd62272ca0efe854a2bf7dddc1798d63773c318e3f264c8da",
    "pdf_audit.py": "23c126b18b86df016dc325b79cacb8e7a9c8a7f9c7642f4905d7a3667b74ddcf",
}


def shadow_calendar(store: Store) -> None:
    """Expose altered calendar metadata without DuckDB's FK UPDATE limitation."""
    result = store._connection.execute("SELECT current_database()").fetchone()
    assert result is not None
    catalog = result[0].replace('"', '""')
    store._connection.execute(
        "CREATE TEMP VIEW price_period AS SELECT period_start,"
        "period_end_exclusive+1 AS period_end_exclusive "
        f'FROM "{catalog}".main.price_period'
    )


def legacy_pin(
    store: Store,
    old: Any,
    iid: str,
    *,
    calendar_audited: bool = True,
    auditor_code: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Independently encoded bounded proof over synthetic authored assertions."""
    from bef_redbird.foundation.ingestion import TABLE_FIELDS
    from tests.test_foundation_selection import independent_hash

    base = json.loads(store.original_path(old.config()["verification_reports"][0]).read_text())
    batch = store.get_record("import_batch", iid)
    delivery_id = str(batch["delivery_id"])
    delivery = store.get_record("source_delivery", delivery_id)
    proof = next(row for row in base["imports"] if row["import_id"] == iid)
    proof.update(delivery_id=delivery_id, parser_version=batch["parser_version"])
    conditions = {
        "import_batch": ("import_id=?", [iid]),
        "import_issue": ("import_id=?", [iid]),
        "source_delivery": ("delivery_id=?", [delivery_id]),
        "price_period": ("period_start=?", [delivery["current_period"]]),
        "package_identifier": ("evidence_import_id=?", [iid]),
        "package": (
            "package_id IN (SELECT package_id FROM package_identifier WHERE evidence_import_id=?)",
            [iid],
        ),
        **{table: ("import_id=?", [iid]) for table in TABLE_FIELDS},
    }
    if not calendar_audited:
        # The original F4 explicit-import reader omitted this table for every
        # delivery, including dated reports. This recipe preserves that scope.
        del conditions["price_period"]
    baselines, hashes = {}, {}
    for table, (where, parameters) in conditions.items():
        cur = store._connection.execute(f"SELECT * FROM {table} WHERE {where}", parameters)
        names = [col[0] for col in cur.description]
        records = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
        if table in TABLE_FIELDS:
            hashes.update({str(row["observation_id"]): independent_hash(row) for row in records})
        portable = json.loads(
            json.dumps(
                records,
                default=lambda value: (
                    value.isoformat() if hasattr(value, "isoformat") else str(value)
                ),
            )
        )

        def canonical(value: Any) -> str:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        encoded = canonical(sorted(portable, key=canonical))
        baselines[table] = {
            "count": len(records),
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }
    report = old.document(
        "legacy-proof-" + iid,
        {
            "version": 1,
            "complete": True,
            "mismatches": [],
            "quarantined_observation_ids": [],
            "audit_scope": {"kind": "explicit_imports", "import_ids": [iid]},
            "imports": [proof],
            "verified_observation_ids": sorted(hashes),
            "observation_hashes": hashes,
            "baseline_tables": baselines,
            **({"auditor_code": auditor_code} if auditor_code is not None else {}),
        },
    )
    return {
        "import_id": iid,
        "verification_delivery_id": report,
        "context_evidence_delivery_ids": [],
    }


def release(
    store: Store,
    tmp: Path,
    key: str,
    day: str,
    *,
    price: int = 12345,
    drug: str = "28101234567",
    creation: str = "20001231",
    empty: bool = False,
    verify: bool = True,
    extra_members: dict[str, bytes] | None = None,
) -> str:
    files = members()
    for name in ("lms01.txt", "lms02.txt"):
        files[name] = files[name].replace(b"28101234567", drug.encode())
    files["lms02.txt"] = files["lms02.txt"].replace(b"20001231", creation.encode())
    files["lms03.txt"] = files["lms03.txt"].replace(b"000012345", f"{price:09d}".encode())
    if empty:
        files["lms01.txt"] = files["lms02.txt"] = files["lms03.txt"] = b""
    files.update(extra_members or {})
    path = tmp / (key + ".zip")
    path.write_bytes(delivery_bytes(files, release=day.replace("-", "").encode()))
    did = store.register_source(path, source="synthetic-ftp", receipt_key=key)
    result = ingest_registry(
        store,
        did,
        phase="final",
        context={
            "source_role": "registry",
            "remote": "/LMS/" + day + "/lms.zip",
        },
    )
    assert result["status"] == "accepted"
    iid = result["import_id"]
    if verify:
        assert verify_registry(store, iid)["complete"]
    return iid


def configuration(store: Store, tmp: Path, imports: list[str], days: list[str]) -> dict[str, Any]:
    evidence = tmp / "semantic-evidence.json"
    evidence.write_text('{"kind":"independently invented synthetic semantic contract"}')
    did = store.register_source(evidence, source="synthetic-semantics", receipt_key="semantics")
    value_review = tmp / "native-value-review.json"
    value_review.write_text(json.dumps({"policy": native_price_value_contract()}), encoding="utf-8")
    value_did = store.register_source(
        value_review, source="synthetic-value-review", receipt_key="native-value-review"
    )
    for day in days:
        start = date.fromisoformat(day)
        store.add_period(start, start + timedelta(days=14))
    profile = store._connection.execute("SELECT profile FROM registry_release LIMIT 1").fetchone()[
        0
    ]
    return {
        "version": POLICY,
        "release_import_ids": imports,
        "native_price_value_policy": {
            "version": NATIVE_PRICE_VALUE_POLICY,
            "evidence_delivery_id": value_did,
            "evidence_sha256": hashlib.sha256(value_review.read_bytes()).hexdigest(),
            "source_locator": "synthetic:policy",
        },
        "period_bindings": [
            {"period_start": day, "snapshot_date": day, "phase": "final"} for day in days
        ],
        "semantic_profiles": [
            {
                "profile": profile,
                "effective_from": "2000-01-01",
                "effective_to": "2030-01-01",
                "fields": {
                    "1": ["drug_id", "name"],
                    "2": [
                        "package_number",
                        "drug_id",
                        "creation_date",
                        "exit_date",
                        "pack_description",
                    ],
                    "3": ["package_number", "aip_ore", "consumer_ore"],
                    "9": ["company_code", "company_name"],
                },
                "complete_snapshot_members": [1, 2, 3, 9],
                "evidence_delivery_id": did,
                "source_locator": "synthetic:contract",
                "monetary_context": {
                    "3.aip_ore": {
                        "currency": "DKK",
                        "tax_basis": "excluding_vat",
                        "fee_basis": "excluding_dispensing_fees",
                        "price_basis": "per_package",
                        "price_role": "effective_price",
                    },
                    "3.consumer_ore": {
                        "currency": "DKK",
                        "tax_basis": "including_vat",
                        "fee_basis": "including_dispensing_fees",
                        "price_basis": "per_package",
                        "price_role": "effective_price",
                    },
                },
            }
        ],
    }


def native_price_probe(
    monkeypatch: pytest.MonkeyPatch, field: str, amount: int | None, changes: dict[str, Any]
) -> dict[str, Any]:
    """Exercise production SQL over independent in-memory selected-row inputs."""
    import duckdb

    from bef_redbird.foundation import registry_selection as selection

    base = {
        "period_start": "2026-10-12",
        "snapshot_date": "2026-10-12",
        "phase": "final",
        "record_state": "present",
    }
    package = {
        **base,
        "package_number": "000007",
        "drug_id": "28101234567",
        "identity_episode_id": "episode",
        "dispensing_code": "AP",
        "calculation_code": "V",
        "supported_fields": ["dispensing_code", "calculation_code"],
        "native_field_states": "{}",
        **changes.get("package", {}),
    }
    dispensing = {
        **base,
        "code": package["dispensing_code"],
        "description": selection.NO_FIXED_PRICE_CODES.get(package["dispensing_code"], "Other"),
        "supported_fields": ["code", "description"],
        "native_field_states": "{}",
        **changes.get("dispensing", {}),
    }
    calculation = {
        **base,
        "code": package["calculation_code"],
        "description": selection.NOT_CALCULATED_DESCRIPTION,
        "supported_fields": ["code", "description"],
        "native_field_states": "{}",
        **changes.get("calculation", {}),
    }
    fields = list(selection.PRICE_FIELDS)
    prices = {
        **base,
        **{name: 12345 for name in fields},
        field: amount,
        "package_number": "000007",
        "import_id": "synthetic",
        "supporting_import_ids": ["synthetic"],
        "member_id": 3,
        "source_record": 1,
        "source_locator": "lms03.txt:record:1",
        "supported_fields": fields,
        "native_field_states": json.dumps({field: "missing"} if amount is None else {}),
        "monetary_context": json.dumps(
            {
                "3." + name: {
                    "currency": "DKK",
                    "tax_basis": "known",
                    "fee_basis": "known",
                    "price_basis": "per_package",
                    "price_role": "synthetic",
                }
                for name in fields
            }
        ),
        **changes.get("price", {}),
    }
    rows_by_member = {2: package, 3: prices, 13: calculation, 18: dispensing}
    monkeypatch.setattr(selection, "_identity_sql", lambda: "SELECT * FROM native2")
    monkeypatch.setattr(selection, "_member_sql", lambda number: f"SELECT * FROM native{number}")
    native_sql = selection._price_sql()
    monkeypatch.setattr(
        selection, "_legacy_price_sql", lambda: f"SELECT * FROM ({native_sql}) n WHERE false"
    )
    sql, _, _ = selection._query_sql("prices")
    with duckdb.connect(":memory:") as connection:
        for number, record in rows_by_member.items():
            structure = {
                key: "DATE"
                if key in {"period_start", "snapshot_date"}
                else ["VARCHAR"]
                if isinstance(value, list)
                else "BIGINT"
                if key in fields or isinstance(value, int)
                else "VARCHAR"
                for key, value in record.items()
            }
            data = [] if changes.get("absent") == number else [record]
            connection.execute(
                f"CREATE TABLE native{number} AS SELECT r.* FROM (SELECT unnest(from_json(?,?)) r)",
                [json.dumps(data), json.dumps([structure])],
            )
        result = connection.execute(sql)
        names = [column[0] for column in result.description]
        rows = [dict(zip(names, row, strict=True)) for row in result.fetchall()]
    return next(row for row in rows if row["field"] == field)


@pytest.mark.parametrize(
    ("field", "amount", "changes", "state"),
    [
        ("consumer_ore", 0, {}, "no_fixed_price"),
        ("consumer_ore", 100, {}, "price_context_conflict"),
        ("consumer_ore", 0, {"package": {"dispensing_code": "APK"}}, "zero_value_unresolved"),
        ("consumer_ore", 100, {"package": {"dispensing_code": "APK"}}, "present"),
        ("consumer_ore", 0, {"package": {"dispensing_code": "BPK"}}, "zero_value_unresolved"),
        ("consumer_ore", 0, {"package": {"dispensing_code": "HX18"}}, "zero_value_unresolved"),
        ("consumer_ore", 0, {"package": {"dispensing_code": "GH"}}, "zero_value_unresolved"),
        ("aip_ore", 0, {}, "zero_value_unresolved"),
        ("aip_ore", 100, {}, "present"),
        ("registered_ore", 0, {}, "not_calculated"),
        ("registered_ore", 100, {}, "price_context_conflict"),
        ("registered_ore", 0, {"package": {"calculation_code": "A"}}, "zero_value_unresolved"),
        ("hospital_slot_08", 0, {}, "zero_value_unresolved"),
        ("hospital_slot_08", 999999999, {}, "present"),
        ("reimbursement_ore", 0, {}, "present"),
        ("non_reimbursable_ore", 0, {}, "present"),
        ("consumer_ore", None, {}, "missing"),
        ("consumer_ore", 0, {"price": {"supported_fields": []}}, "unsupported"),
        ("consumer_ore", 0, {"price": {"record_state": "conflict"}}, "conflict"),
        ("consumer_ore", 0, {"package": {"identity_episode_id": None}}, "unresolved_identity"),
        ("consumer_ore", 0, {"package": {"supported_fields": []}}, "zero_value_unresolved"),
        (
            "consumer_ore",
            0,
            {"package": {"native_field_states": '{"dispensing_code":"ambiguous_encoding"}'}},
            "zero_value_unresolved",
        ),
        (
            "consumer_ore",
            0,
            {"dispensing": {"supported_fields": ["code"]}},
            "zero_value_unresolved",
        ),
        ("consumer_ore", 0, {"dispensing": {"record_state": "conflict"}}, "zero_value_unresolved"),
        (
            "consumer_ore",
            0,
            {"dispensing": {"description": "New unreviewed description"}},
            "zero_value_unresolved",
        ),
        (
            "consumer_ore",
            0,
            {"dispensing": {"snapshot_date": "2026-10-13"}},
            "zero_value_unresolved",
        ),
        ("consumer_ore", 0, {"package": {"snapshot_date": "2026-10-13"}}, "zero_value_unresolved"),
        ("consumer_ore", 0, {"dispensing": {"phase": "preliminary"}}, "zero_value_unresolved"),
        ("consumer_ore", 0, {"absent": 18}, "zero_value_unresolved"),
        (
            "registered_ore",
            0,
            {"calculation": {"record_state": "conflict"}},
            "zero_value_unresolved",
        ),
        (
            "registered_ore",
            0,
            {"calculation": {"description": "Not calculated"}},
            "zero_value_unresolved",
        ),
        (
            "registered_ore",
            0,
            {"calculation": {"snapshot_date": "2026-10-13"}},
            "zero_value_unresolved",
        ),
        ("registered_ore", 0, {"calculation": {"supported_fields": []}}, "zero_value_unresolved"),
        ("registered_ore", 0, {"absent": 13}, "zero_value_unresolved"),
    ],
)
def test_native_price_value_states_are_field_and_context_specific(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    amount: int | None,
    changes: dict[str, Any],
    state: str,
) -> None:
    row = native_price_probe(monkeypatch, field, amount, changes)
    assert row["amount_ore"] == amount
    assert row["state"] == state
    assert row["usable"] is (state == "present")


@pytest.mark.parametrize("code", ["AP", "BP", "HF", "HP", "HV", "HX", "PRMIX"])
def test_each_no_fixed_price_code_requires_its_exact_native_semantics(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    row = native_price_probe(monkeypatch, "consumer_ore", 0, {"package": {"dispensing_code": code}})
    assert row["state"] == "no_fixed_price"


@pytest.mark.parametrize(
    ("day", "phase", "expected"),
    [
        ("2024-12-23", "final", "zero_value_unresolved"),
        ("2025-01-06", "final", "no_fixed_price"),
        ("2026-10-13", "final", "zero_value_unresolved"),
        ("2026-10-12", "preliminary", "zero_value_unresolved"),
    ],
)
def test_native_named_value_states_require_reviewed_date_and_final_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    day: str,
    phase: str,
    expected: str,
) -> None:
    changes = {
        name: {"period_start": day, "snapshot_date": day, "phase": phase}
        for name in ("price", "package", "dispensing", "calculation")
    }
    assert native_price_probe(monkeypatch, "consumer_ore", 0, changes)["state"] == expected


def test_native_value_policy_is_pinned_and_raw_zero_is_not_rewritten(tmp_path: Path) -> None:
    from tests.test_foundation_selection import PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        authored = [
            {
                "amount_ore": 777,
                "tax_basis": "excluding_vat",
                "fee_basis": "excluding_dispensing_fees",
            }
        ]
        legacy_delivery = old.document("legacy-positive", authored, phase="final")
        old.source("legacy-positive", authored, delivery=legacy_delivery)
        frozen = store.create_selection(old.config())
        iid = release(
            store,
            tmp_path,
            "raw-zero",
            PERIOD,
            price=0,
            extra_members={
                "lms31.txt": populated(
                    36,
                    {
                        (1, 11): b"28101234567",
                        (12, 17): b"000007",
                        (18, 26): b"000000000",
                        (27, 35): b"000000000",
                    },
                )
                + b"\r\n"
            },
        )
        before = store._connection.execute("SELECT * FROM registry_lms03").fetchall()
        config = configuration(store, tmp_path, [iid], [PERIOD])
        config["legacy_selection_ids"] = [frozen["selection_id"]]
        rule = config["semantic_profiles"][0]
        rule["fields"]["31"] = [
            "drug_id",
            "package_number",
            "consumer_unit_token",
            "consumer_ddd_token",
        ]
        rule["complete_snapshot_members"].append(31)
        missing = {
            key: value for key, value in config.items() if key != "native_price_value_policy"
        }
        with pytest.raises(ValueError, match="evidence-pinned native price value policy"):
            publish(store, missing)
        wrong = json.loads(json.dumps(config))
        wrong["native_price_value_policy"]["evidence_sha256"] = "a" * 64
        with pytest.raises(ValueError, match="value policy evidence differs"):
            publish(store, wrong)
        wrong_document = tmp_path / "wrong-value-document.json"
        wrong_document.write_text('{"policy":{"version":"invented"}}')
        wrong_id = store.register_source(
            wrong_document, source="synthetic-review", receipt_key="wrong-value"
        )
        wrong = json.loads(json.dumps(config))
        wrong["native_price_value_policy"].update(
            evidence_delivery_id=wrong_id,
            evidence_sha256=hashlib.sha256(wrong_document.read_bytes()).hexdigest(),
        )
        with pytest.raises(ValueError, match="value policy evidence differs"):
            publish(store, wrong)
        result = publish(store, config)
        assert (
            result["manifest"]["native_price_value_policy"]["version"] == NATIVE_PRICE_VALUE_POLICY
        )
        prices = query(store, result["dataset_id"], "prices")
        aip = [r for r in prices if r["field"] == "aip_ore"]
        assert len(aip) == 1  # Unresolved zero never resurrects the legacy 777.
        row = aip[0]
        assert (row["amount_ore"], row["state"], row["usable"]) == (
            0,
            "zero_value_unresolved",
            False,
        )
        assert store._connection.execute("SELECT * FROM registry_lms03").fetchall() == before
        assert query(store, result["dataset_id"], 3)[0]["aip_ore"] == 0
        unit = query(store, result["dataset_id"], 31)[0]
        assert unit["consumer_unit_token"] == unit["consumer_ddd_token"] == "000000000"
        assert next(r for r in prices if r["field"] == "consumer_ore")["amount_ore"] == 18003
        assert (
            store.inspect_selection(frozen["selection_id"])["selection_id"]
            == frozen["selection_id"]
        )


def test_native_value_states_use_independently_verified_selected_reference_members(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        package = bytearray(members()["lms02.txt"])
        package[73:78], package[127:128] = b"AP   ", b"V"
        prices = bytearray(members()["lms03.txt"])
        prices[15:24] = prices[24:33] = b"000000000"
        iid = release(
            store,
            tmp_path,
            "native-context",
            "2026-10-12",
            extra_members={
                "lms02.txt": bytes(package),
                "lms03.txt": bytes(prices),
                "lms13.txt": populated(
                    52, {(1, 1): b"V", (2, 51): b"AUP ikke beregnet, vet. frihandel".ljust(50)}
                )
                + b"\r\n",
                "lms18.txt": populated(
                    67,
                    {
                        (1, 5): b"AP   ",
                        (6, 6): b"A",
                        (17, 66): b"Udelukkende produktionsdyr, en udlevering".ljust(50),
                    },
                )
                + b"\r\n",
            },
        )
        before = store._connection.execute("SELECT * FROM registry_lms03").fetchall()
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        rule = config["semantic_profiles"][0]
        rule["fields"]["2"].extend(["dispensing_code", "calculation_code"])
        rule["fields"].update({"13": ["code", "description"], "18": ["code", "description"]})
        rule["complete_snapshot_members"].extend([13, 18])
        rule["fields"]["3"].append("registered_ore")
        rule["monetary_context"]["3.registered_ore"] = rule["monetary_context"]["3.consumer_ore"]
        dataset = publish(store, config)["dataset_id"]
        values = {row["field"]: row for row in query(store, dataset, "prices")}
        assert values["aip_ore"]["amount_ore"] == 12345
        assert values["aip_ore"]["state"] == "present"
        assert (values["registered_ore"]["state"], values["registered_ore"]["usable"]) == (
            "not_calculated",
            False,
        )
        assert (values["consumer_ore"]["state"], values["consumer_ore"]["usable"]) == (
            "no_fixed_price",
            False,
        )
        assert store._connection.execute("SELECT * FROM registry_lms03").fetchall() == before


def test_explicit_format_profile_selects_verified_shared_member_values(tmp_path: Path) -> None:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(delivery_bytes(release=b"20261012"))) as source,
        zipfile.ZipFile(output, "w") as target,
    ):
        for name in source.namelist():
            data = source.read(name)
            target.writestr(name, data[:-2] if name == "lms02.txt" else data)
    native = tmp_path / "variant.zip"
    native.write_bytes(output.getvalue())
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = store.register_source(native, source="synthetic-ftp", receipt_key="variant")
        result = ingest_registry(
            store, delivery, phase="final", context={}, parser_version=MISSING_FINAL_CRLF_PROFILE
        )
        iid = result["import_id"]
        assert verify_registry(store, iid)["complete"]
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        dataset = publish(store, config)["dataset_id"]
        rows = query(store, dataset, "prices")
        assert next(row for row in rows if row["price_type"] == "aip")["amount_ore"] == 12345
        assert status(store, dataset, verify=True)["verified"]


def test_repeat_editions_one_typed_value_and_frozen_readback(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        first = release(store, tmp_path, "one", "2026-10-12")
        second = release(store, tmp_path, "two", "2026-10-12")
        config = configuration(store, tmp_path, [first, second], ["2026-10-12"])
        result = publish(store, config)
        did = result["dataset_id"]
        assert publish(store, config)["dataset_id"] == did
        assert len(query(store, did, "editions")) == 2
        assert all(
            row["source_locator"].startswith("lms09.txt:record:") for row in query(store, did, 9)
        )
        rows = query(store, did, "prices")
        aip = next(row for row in rows if row["price_type"] == "aip")
        assert aip["amount_ore"] == 12345
        assert aip["package_number"] == "000007"
        assert aip["state"] == "present"
        assert len(aip["supporting_import_ids"]) == 2
        assert aip["tax_basis"] == "excluding_vat"
        consumer = next(row for row in rows if row["price_type"] == "consumer")
        assert consumer["tax_basis"] == "including_vat"
        assert status(store, did, verify=True)["verified"]
        exported = extract(store, did, "prices", tmp_path / "prices.csv")
        assert exported["rows"] == 6
        assert "12345" in (tmp_path / "prices.csv").read_text()
        assert store._connection.execute("SELECT count(*) FROM frozen_selection").fetchone() == (0,)


def dataset_rows(store: Store) -> dict[str, list[tuple[Any, ...]]]:
    """Snapshot every dataset table, including any previously accepted dataset."""
    return {
        table: store._connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
        for table in (
            "analytical_dataset",
            "registry_dataset_release",
            "registry_dataset_period",
            "registry_dataset_member",
            "registry_dataset_legacy",
            "registry_dataset_supply",
        )
    }


@pytest.mark.parametrize("trusted", [False, True])
def test_publication_and_retry_each_fully_verify_typed_rows_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: bool
) -> None:
    from bef_redbird.foundation import registry_selection as selection

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "one", "2026-10-12")
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        real_verify = selection._verify_typed
        verified_depths = []

        def verify(current_store: Store, inputs: dict[str, dict[str, Any]]) -> None:
            # Execute the real readback, rather than returning a mocked verdict.
            real_verify(current_store, inputs)
            assert current_store._connection.execute(
                "SELECT count(*) FROM analytical_dataset"
            ).fetchone() == (1,)
            verified_depths.append(current_store._depth)

        monkeypatch.setattr(selection, "_verify_typed", verify)
        first = publish(store, config, trusted_verification=trusted)
        assert first["verified"] is True
        assert verified_depths == [1]
        saved = dataset_rows(store)
        assert all(saved[table] for table in list(saved)[:4])
        repeated = publish(store, config, trusted_verification=trusted)
        assert repeated == first
        assert verified_depths == [1, 0]
        assert dataset_rows(store) == saved


@pytest.mark.parametrize("trusted", [False, True])
def test_publication_typed_corruption_rolls_back_every_dataset_table(
    tmp_path: Path, trusted: bool
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "one", "2026-10-12")
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        before = dataset_rows(store)
        store._connection.execute("UPDATE registry_lms03 SET aip_ore=12346")
        with pytest.raises(ValueError, match="typed registry values changed"):
            publish(store, config, trusted_verification=trusted)
        assert dataset_rows(store) == before


@pytest.mark.parametrize("nested", [False, True])
def test_post_insert_choice_verification_rolls_back_and_preserves_prior_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    from bef_redbird.foundation import registry_selection as selection

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "one", "2026-10-12")
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        prior = publish(store, config)
        before = dataset_rows(store)
        config["semantic_profiles"][0]["source_locator"] += ":second-reviewed-scope"
        real_verify = selection._verify_typed
        changed = []

        def alter_inserted_choice(current_store: Store, inputs: dict[str, dict[str, Any]]) -> None:
            real_verify(current_store, inputs)
            inserted = current_store._connection.execute(
                "SELECT dataset_id FROM analytical_dataset WHERE dataset_id<>?",
                [prior["dataset_id"]],
            ).fetchall()
            assert len(inserted) == 1
            current_store._connection.execute(
                "UPDATE registry_dataset_member SET supported_fields=[] "
                "WHERE dataset_id=? AND member_number=3",
                [inserted[0][0]],
            )
            changed.append(str(inserted[0][0]))

        monkeypatch.setattr(selection, "_verify_typed", alter_inserted_choice)
        if nested:
            with pytest.raises(FoundationError, match="failed operation"):
                with store.transaction():
                    with pytest.raises(ValueError, match="choices changed"):
                        publish(store, config)
                    # Catching the inner failure cannot turn it into a commit.
                    assert store._rollback_only is True
        else:
            with pytest.raises(ValueError, match="choices changed"):
                publish(store, config)
        assert len(changed) == 1
        assert dataset_rows(store) == before
        monkeypatch.setattr(selection, "_verify_typed", real_verify)
        assert status(store, prior["dataset_id"], verify=True) == prior


def test_edition_conflict_is_scoped_no_latest_download_authority(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        one = release(store, tmp_path, "one", "2026-10-12")
        two = release(store, tmp_path, "two", "2026-10-12", price=12346)
        config = configuration(store, tmp_path, [one, two], ["2026-10-12"])
        did = publish(store, config)["dataset_id"]
        assert not query(store, did, "prices")
        assert query(store, did, "packages")[0]["identity_episode_id"]
        coverage = query(store, did, "coverage")
        assert next(row for row in coverage if row["member_number"] == 3)["state"] == "conflict"
        config["period_bindings"][0].update(
            chosen_import_id=two,
            choice_evidence_delivery_id=config["semantic_profiles"][0]["evidence_delivery_id"],
            source_locator="synthetic:explicit-correction",
        )
        revised = publish(store, config)["dataset_id"]
        assert (
            next(row for row in query(store, revised, "prices") if row["price_type"] == "aip")[
                "amount_ore"
            ]
            == 12346
        )
        assert not query(store, did, "prices")


def test_identity_changes_omission_and_missing_period_split_episodes(tmp_path: Path) -> None:
    days = ["2026-08-17", "2026-08-31", "2026-09-14", "2026-09-28", "2026-10-12", "2026-10-26"]
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        imports = [
            release(store, tmp_path, "a", days[0]),
            release(store, tmp_path, "b", days[1]),
            release(store, tmp_path, "c", days[2], empty=True),
            release(store, tmp_path, "d", days[3]),
            release(store, tmp_path, "f", days[5], drug="28107654321"),
        ]
        config = configuration(store, tmp_path, imports, days)
        did = publish(store, config)["dataset_id"]
        packages = query(store, did, "packages")
        assert len(packages) == 4
        assert packages[0]["identity_episode_id"] == packages[1]["identity_episode_id"]
        assert len({row["identity_episode_id"] for row in packages}) == 3
        assert not query(store, did, "prices", period_start=days[2])
        assert not query(store, did, "prices", period_start=days[4])


def test_unknown_creation_and_explicit_zero_do_not_become_missing_money(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "unknown", "2026-10-12", price=0, creation="19000101")
        did = publish(store, configuration(store, tmp_path, [iid], ["2026-10-12"]))["dataset_id"]
        row = next(row for row in query(store, did, "prices") if row["price_type"] == "aip")
        assert row["amount_ore"] == 0
        assert row["state"] == "unresolved_identity"
        assert row["identity_episode_id"] is None
        assert query(store, did, 2)[0]["exit_date"] is None


def test_daily_release_is_provenance_not_extra_tariff_example(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        anchor = release(store, tmp_path, "anchor", "2026-10-12")
        daily = release(store, tmp_path, "daily", "2026-10-13", price=99999)
        did = publish(store, configuration(store, tmp_path, [anchor, daily], ["2026-10-12"]))[
            "dataset_id"
        ]
        rows = query(store, did, "prices")
        assert next(row for row in rows if row["price_type"] == "aip")["amount_ore"] == 12345
        assert status(store, did)["manifest"]["input_count"] == 2


def test_unverified_input_rejected_and_frozen_bindings_detect_tampering(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "unverified", "2026-10-12", verify=False)
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        with pytest.raises(ValueError, match="reconciliation"):
            publish(store, config)
        verify_registry(store, iid)
        did = publish(store, config)["dataset_id"]
        store._connection.execute(
            "UPDATE registry_dataset_member SET supported_fields=[] "
            "WHERE dataset_id=? AND member_number=3",
            [did],
        )
        with pytest.raises(ValueError, match="choices changed"):
            status(store, did, verify=True)


def test_configuration_requires_explicit_semantics_and_replacement_evidence(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "one", "2026-10-12")
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        config["period_bindings"][0]["chosen_import_id"] = iid
        with pytest.raises(ValueError, match="precedence"):
            publish(store, config)
        config["period_bindings"][0].pop("chosen_import_id")
        config["semantic_profiles"][0]["fields"]["3"].append("legacy_09")
        with pytest.raises(ValueError, match="opaque"):
            publish(store, config)
        assert json.loads(json.dumps(config))["version"] == POLICY


def test_read_rehashes_typed_values_and_ordering_not_only_proof_strings(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "one", "2026-10-12")
        did = publish(store, configuration(store, tmp_path, [iid], ["2026-10-12"]))["dataset_id"]
        store._connection.execute("UPDATE registry_lms03 SET aip_ore=12346")
        with pytest.raises(ValueError, match="typed registry values changed"):
            query(store, did, "prices")
        store._connection.execute("UPDATE registry_lms03 SET aip_ore=12345")
        with pytest.raises(ConstraintException):
            store._connection.execute(
                "UPDATE registry_member SET row_ids=[],row_count=0 WHERE member_number=3"
            )
        assert status(store, did, verify=True)["verified"]


def test_publication_checks_native_context_even_when_proof_strings_remain_valid(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "one", "2026-10-12")
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        store._connection.execute("UPDATE registry_release SET tariff_week='99'")
        with pytest.raises(ValueError, match="source context changed"):
            publish(store, config)
        assert store._connection.execute("SELECT count(*) FROM analytical_dataset").fetchone() == (
            0,
        )


def test_later_daily_snapshot_cannot_be_relabelled_as_period_start(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "later-daily", "2026-10-13")
        config = configuration(store, tmp_path, [iid], ["2026-10-12"])
        config["period_bindings"][0]["snapshot_date"] = "2026-10-13"
        with pytest.raises(ValueError, match="exact official period start"):
            publish(store, config)


def test_lazy_frozen_values_reject_changed_query_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bef_redbird.foundation import registry_selection as module

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "one", "2026-10-12")
        did = publish(store, configuration(store, tmp_path, [iid], ["2026-10-12"]))["dataset_id"]
        monkeypatch.setitem(module.PRICE_FIELDS, "aip_ore", "silently_changed_meaning")
        with pytest.raises(ValueError, match="original versioned query contract"):
            query(store, did, "prices", verify=False)


def test_legacy_gap_is_supported_without_resurrecting_snapshot_omission(tmp_path: Path) -> None:
    from tests.test_foundation_selection import NEXT, PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        authored = [
            {
                "amount_ore": amount,
                "effective_period": period,
                "tax_basis": "excluding_vat",
                "fee_basis": "excluding_dispensing_fees",
            }
            for amount, period in ((777, PERIOD), (888, NEXT))
        ]
        delivery = old.document("old", authored, phase="final")
        old.source("old", authored, delivery=delivery)
        selected = store.create_selection(old.config())
        iid = release(store, tmp_path, "empty-complete", PERIOD, empty=True)
        config = configuration(store, tmp_path, [iid], [PERIOD, NEXT])
        config["legacy_selection_ids"] = [selected["selection_id"]]
        did = publish(store, config)["dataset_id"]
        rows = query(store, did, "prices")
        assert len(rows) == 1
        assert rows[0]["period_start"] == date.fromisoformat(NEXT)
        assert rows[0]["amount_ore"] == 888
        assert rows[0]["identity_episode_id"].startswith("legacy:")
        assert rows[0]["source_authority"] == "verified_legacy"
        assert (
            store.inspect_selection(selected["selection_id"])["selection_id"]
            == selected["selection_id"]
        )


def test_legacy_fallback_preserves_phase_and_complete_monetary_key(tmp_path: Path) -> None:
    from tests.test_foundation_selection import PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        selected = []
        exact_large_amount = 9_007_199_254_740_993
        alternatives = [
            ("final", 777, {}),
            ("preliminary", 888, {}),
            (None, 999, {}),
            ("unknown", 999, {}),
            ("final", exact_large_amount, {"price_basis": "per_unit"}),
            ("final", 444, {"currency": "EUR"}),
            ("final", 555, {"tax_basis": "including_vat"}),
            ("final", 666, {"fee_basis": "including_dispensing_fees"}),
            ("preliminary", 0, {"currency": "SEK"}),
        ]
        for index, (phase, amount, context) in enumerate(alternatives):
            authored = [
                {
                    "amount_ore": amount,
                    "tax_basis": "excluding_vat",
                    "fee_basis": "excluding_dispensing_fees",
                    **context,
                }
            ]
            label = f"phase-context-{index}"
            delivery = old.document(label, authored, phase=phase)
            iid = old.source(label, authored, delivery=delivery)
            frozen = store.create_selection(
                old.config(
                    import_ids=[iid],
                    derivations={delivery: iid},
                    context_evidence={iid: []},
                )
            )
            selected.append(frozen["selection_id"])
        iid = release(store, tmp_path, "empty-final-snapshot", PERIOD, empty=True)
        config = configuration(store, tmp_path, [iid], [PERIOD])
        config["legacy_selection_ids"] = selected
        dataset = publish(store, config)["dataset_id"]
        rows = query(store, dataset, "prices")
        assert len(rows) == 7
        assert {(row["phase"], row["amount_ore"]) for row in rows} == {
            ("preliminary", 888),
            ("unknown", 999),
            ("final", exact_large_amount),
            ("final", 444),
            ("final", 555),
            ("final", 666),
            ("preliminary", 0),
        }
        assert all(row["state"] == "present" for row in rows)
        frozen_zero = next(row for row in rows if row["currency"] == "SEK")
        assert frozen_zero["amount_ore"] == 0
        assert frozen_zero["usable"] is True
        assert frozen_zero["positive_reference"] is False
        assert (
            next(row for row in rows if row["amount_ore"] == exact_large_amount)["price_basis"]
            == "per_unit"
        )
        assert next(row for row in rows if row["amount_ore"] == 444)["currency"] == "EUR"
        assert next(row for row in rows if row["amount_ore"] == 555)["tax_basis"] == "including_vat"
        assert (
            next(row for row in rows if row["amount_ore"] == 666)["fee_basis"]
            == "including_dispensing_fees"
        )
        # Matching complete final scope suppresses 777 even though its package
        # is absent. Distinct phases/bases survive, with deterministic paging.
        paged = [
            query(store, dataset, "prices", limit=1, offset=offset, verify=False)[0]
            for offset in range(len(rows))
        ]
        assert paged == rows
        assert status(store, dataset, verify=True)["verified"]
        # Removing AIP from the declared FTP field scope permits both legacy
        # bases. They must remain separate and ordered through page boundaries.
        rule = config["semantic_profiles"][0]
        rule["fields"]["3"].remove("aip_ore")
        del rule["monetary_context"]["3.aip_ore"]
        unsupported = publish(store, config)["dataset_id"]
        basis_rows = [
            row
            for row in query(store, unsupported, "prices")
            if row["phase"] == "final"
            and row["currency"] == "DKK"
            and row["tax_basis"] == "excluding_vat"
            and row["fee_basis"] == "excluding_dispensing_fees"
        ]
        assert [(row["price_basis"], row["amount_ore"]) for row in basis_rows] == [
            ("per_package", 777),
            ("per_unit", exact_large_amount),
        ]


def test_legacy_effective_price_is_not_suppressed_by_another_native_price_role(
    tmp_path: Path,
) -> None:
    from tests.test_foundation_selection import PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        authored = [
            {
                "amount_ore": 777,
                "tax_basis": "excluding_vat",
                "fee_basis": "excluding_dispensing_fees",
            }
        ]
        delivery = old.document("effective-final", authored, phase="final")
        old.source("effective-final", authored, delivery=delivery)
        selected = store.create_selection(old.config())
        iid = release(store, tmp_path, "other-price-role", PERIOD, empty=True)
        config = configuration(store, tmp_path, [iid], [PERIOD])
        config["legacy_selection_ids"] = [selected["selection_id"]]
        config["semantic_profiles"][0]["monetary_context"]["3.aip_ore"]["price_role"] = (
            "published_reference_amount"
        )
        dataset = publish(store, config)["dataset_id"]
        rows = query(store, dataset, "prices")
        assert len(rows) == 1
        assert (rows[0]["phase"], rows[0]["price_role"], rows[0]["amount_ore"]) == (
            "final",
            "effective_price",
            777,
        )


def test_supplier_duplicate_original_preference_and_no_negative_or_identity_invention(
    tmp_path: Path,
) -> None:
    from tests.test_foundation_selection import PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        report = [
            {
                "table": "supply_observation",
                "observation_date": PERIOD,
                "wholesaler_namespace": "official-label",
                "wholesaler_identifier": "Synthetic supplier",
                "reported_status": "reported_disruption",
            }
        ]
        did_official = old.document(
            "official-native", {"native": "same original"}, source="official-archive:supply"
        )
        did_previous = old.document("previous-native", {"native": "same original"}, source="supply")
        official = old.source(
            "official", report, delivery=did_official, version="supply-workbook-v1"
        )
        previous_report = [dict(report[0], wholesaler_namespace="older-label")]
        previous = old.source(
            "previous", previous_report, delivery=did_previous, version="supply-workbook-v1"
        )
        proof = old.config()["verification_reports"][0]
        iid = release(store, tmp_path, "one", PERIOD)
        config = configuration(store, tmp_path, [iid], [PERIOD])
        config["supply_imports"] = [
            {"import_id": entry, "verification_delivery_id": proof}
            for entry in (official, previous)
        ]
        config["supply_source_preference"] = ["official-archive:supply", "supply"]
        did = publish(store, config)["dataset_id"]
        rows = query(store, did, "supply")
        assert len(rows) == 1
        assert rows[0]["wholesaler_namespace"] == "official-label"
        assert rows[0]["reported_status"] == "reported_disruption"
        assert rows[0]["identity_episode_id"] is None
        assert rows[0]["absence_means"] == "unknown"
        assert status(store, did)["manifest"]["selected_supply_import_count"] == 1
        store._connection.execute(
            "UPDATE supply_observation SET reported_status='recovered' WHERE import_id=?",
            [official],
        )
        with pytest.raises(ValueError, match="assertion does not match"):
            query(store, did, "supply")


def test_policy_builder_new_and_late_editions_create_new_frozen_selection(tmp_path: Path) -> None:
    days = ["2026-09-14", "2026-09-28", "2026-10-12"]
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "initial", days[0])
        base = configuration(store, tmp_path, [iid], days)
        policy = {
            "version": "registry-canonical-policy-v1",
            "history_from": days[0],
            "history_through": "latest_verified",
            "sources": ["synthetic-ftp"],
            "source_role": "registry",
            "excluded_source_areas": ["/LMS/TEST"],
            "phases": ["final"],
            "semantic_profiles": base["semantic_profiles"],
            "native_price_value_policy": base["native_price_value_policy"],
        }
        first_config = build_configuration(store, policy, destination=tmp_path / "generated-1.json")
        first = publish(store, first_config)["dataset_id"]
        release(store, tmp_path, "later-period", days[2], price=23456)
        second_config = build_configuration(
            store, policy, destination=tmp_path / "generated-2.json"
        )
        assert len(second_config["period_bindings"]) == 3
        second = publish(store, second_config)["dataset_id"]
        assert second != first
        assert len(query(store, first, "packages")) == 1
        assert len(query(store, second, "packages")) == 2
        assert not query(store, second, "prices", period_start=days[1])
        release(store, tmp_path, "late-arrival", days[1], price=22222)
        third = publish(store, build_configuration(store, policy))["dataset_id"]
        assert third not in {first, second}
        packages = query(store, third, "packages")
        assert len({row["identity_episode_id"] for row in packages}) == 1
        assert len({row["identity_episode_id"] for row in query(store, second, "packages")}) == 2
        assert status(store, first, verify=True)["verified"]
        with pytest.raises(FileExistsError):
            build_configuration(store, policy, destination=tmp_path / "generated-1.json")

        # Future source dates cannot silently fall outside the reviewed calendar.
        release(store, tmp_path, "calendar-not-yet-reviewed", "2026-10-26", price=24567)
        with pytest.raises(ValueError, match="lack reviewed tariff-calendar coverage"):
            build_configuration(store, policy)
        assert status(store, third, verify=True)["verified"]
        store.add_period(date(2026, 10, 26), date(2026, 11, 9))
        resumed = build_configuration(store, policy)
        assert len(resumed["period_bindings"]) == 4
        assert resumed["period_bindings"][-1]["period_start"] == "2026-10-26"
        # An edition within a reviewed half-open period does not invent a boundary.
        release(store, tmp_path, "ordinary-daily", "2026-10-27", price=24567)
        assert len(build_configuration(store, policy)["period_bindings"]) == 4


def test_supplements_deduplicate_scope_preserve_undated_editions_and_verify_values(
    tmp_path: Path,
) -> None:
    from bef_redbird.foundation.ftp_supplement_store import ingest_supplement
    from tests.test_foundation_ftp_supplements import _csv, _dok

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        iid = release(store, tmp_path, "registry", "2026-10-12")
        base = configuration(store, tmp_path, [iid], ["2026-10-12"])

        def supplement(key: str, kind: str, content: bytes, *, area: str = "20261020") -> str:
            path = tmp_path / (key + ".txt")
            path.write_bytes(content)
            delivery = store.register_source(
                path,
                source="synthetic-ftp",
                receipt_key=key,
                original_uri=f"ftp://synthetic.invalid/LMS/{area}/{kind}.txt",
            )
            return ingest_supplement(store, delivery, kind)["import_id"]

        dok = supplement("dok", "DOK", _dok())
        supplement("dok-repeat", "DOK", _dok())
        supplement("dok-ambiguous", "DOK", _dok().replace(b"Example", b"Different"))
        supplement("reference", "LMS37", _csv("LMS37", "EXAMPLE;Group;\r\n"))
        supplement("reference-repeat", "LMS37", _csv("LMS37", "EXAMPLE;Group;\r\n"))
        supplement(
            "reference-conflict", "LMS37", _csv("LMS37", "EXAMPLE;Group;\r\nEXAMPLE;Other;\r\n")
        )
        supplement("permission", "LMS36", _csv("LMS36", "00000000001;EXAMPLE;19700101;\r\n"))
        excluded = supplement("test-source", "DOK", _dok(), area="TEST")
        policy = {
            "version": "registry-canonical-policy-v1",
            "history_from": "2026-10-12",
            "history_through": "latest_verified",
            "sources": ["synthetic-ftp"],
            "source_role": "registry",
            "excluded_source_areas": ["/LMS/TEST"],
            "semantic_profiles": base["semantic_profiles"],
            "native_price_value_policy": base["native_price_value_policy"],
        }
        config = build_configuration(store, policy)
        assert excluded not in config["supplement_import_ids"]
        assert len(config["supplement_import_ids"]) == 7
        did = publish(store, config)["dataset_id"]
        rows = query(store, did, "supplements")
        assert len(rows) == 4
        conditional = next(row for row in rows if row["kind"] == "DOK")
        assert conditional["effective_date"] == date(2026, 10, 12)
        assert conditional["issue_date"] == date(2026, 10, 9)
        assert conditional["state"] == "conflict"
        assert conditional["source_description"] is None
        assert conditional["source_occurrences"] == 3
        references = [row for row in rows if row["kind"] == "LMS37"]
        assert {row["state"] for row in references} == {"conflict", "present"}
        assert all(row["cross_edition_disagreement"] for row in references)
        assert all(row["effective_date"] is None for row in references)
        assert (
            next(row for row in references if row["state"] == "present")["source_occurrences"] == 2
        )
        permission = next(row for row in rows if row["kind"] == "LMS36")
        assert permission["valid_from"] == date(1970, 1, 1)
        assert permission["valid_to"] is None
        assert permission["temporal_state"] == "sentinel_validity_unresolved"
        assert all(
            row["identity_episode_id"] is None and not row["training_usable"] for row in rows
        )
        assert len(query(store, did, "supplement_editions")) == 8
        assert extract(store, did, "supplements", tmp_path / "supplements.csv")["rows"] == 4
        # Alter only a non-indexed typed value; cached proof strings stay unchanged.
        store._connection.execute(
            "UPDATE supplement_record SET payload=json_merge_patch(payload,"
            '\'{"source_description":"Corrupted"}\') WHERE import_id=?',
            [dok],
        )
        with pytest.raises(ValueError, match="independent native verification"):
            query(store, did, "supplements")


def test_legacy_source_evidence_is_bounded_deduplicated_and_never_default_model_prices(
    tmp_path: Path,
) -> None:
    from tests.test_foundation_selection import PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        authored = [
            {"table": "package_observation", "name": "Synthetic older package"},
            {
                "table": "lifecycle_event",
                "event_type": "introduction",
                "original_source_code": "new",
                "interpretation_status": "established",
            },
            {"amount_ore": 789, "tax_basis": "unknown", "fee_basis": "unknown"},
        ]
        first = old.source(
            "old-first", authored, source="synthetic-legacy", version="introductions-rtf-v1"
        )
        second = old.source(
            "old-repeat", authored, source="synthetic-legacy", version="introductions-rtf-v1"
        )
        pins = [legacy_pin(store, old, iid) for iid in (first, second)]
        iid = release(store, tmp_path, "registry", PERIOD)
        config = configuration(store, tmp_path, [iid], [PERIOD])
        config["legacy_imports"] = pins
        did = publish(store, config)["dataset_id"]
        evidence = query(store, did, "legacy_evidence")
        assert len(evidence) == 3
        assert all(row["source_occurrences"] == 2 for row in evidence)
        assert all(
            not row["training_usable"] and row["identity_episode_id"] is None for row in evidence
        )
        price = next(row for row in evidence if row["observation_table"] == "price_observation")
        assert (price["amount_ore"], price["tax_basis"], price["fee_basis"]) == (
            789,
            "unknown",
            "unknown",
        )
        assert len(query(store, did, "legacy_editions")) == 6
        assert all(row["source_authority"] == "ftp" for row in query(store, did, "prices"))
        assert extract(store, did, "legacy_evidence", tmp_path / "legacy.csv")["rows"] == 3
        assert store._connection.execute("SELECT count(*) FROM frozen_selection").fetchone() == (0,)
        store._connection.execute(
            "UPDATE package_observation SET name='Changed' WHERE import_id=?", [first]
        )
        with pytest.raises(ValueError, match="Legacy assertion"):
            query(store, did, "legacy_evidence")


def test_legacy_proof_binding_and_conflicting_values_are_explicit(tmp_path: Path) -> None:
    from tests.test_foundation_selection import PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        first = old.source(
            "old-conflict",
            [
                {"table": "package_observation", "name": "One"},
                {"table": "package_observation", "name": "Two"},
            ],
            version="introductions-rtf-v1",
        )
        second = old.source("other-proof", [{"amount_ore": 789}], version="price-changes-rtf-v1")
        first_pin, second_pin = legacy_pin(store, old, first), legacy_pin(store, old, second)
        iid = release(store, tmp_path, "registry", PERIOD)
        config = configuration(store, tmp_path, [iid], [PERIOD])
        config["legacy_imports"] = [
            {**first_pin, "verification_delivery_id": second_pin["verification_delivery_id"]}
        ]
        with pytest.raises(ValueError, match="independently scoped proof"):
            publish(store, config)
        config["legacy_imports"] = [first_pin]
        did = publish(store, config)["dataset_id"]
        evidence = query(store, did, "legacy_evidence")
        assert len(evidence) == 1
        assert evidence[0]["state"] == "conflict"
        assert evidence[0]["name"] is None
        assert evidence[0]["source_occurrences"] == 2
        store._connection.execute(
            "INSERT INTO import_issue VALUES(uuid(),?,'invented',NULL,NULL,NULL,"
            "'changed','warning','none')",
            [first],
        )
        with pytest.raises(ValueError, match="metadata, mapping, issues or calendar changed"):
            status(store, did, verify=True)


@pytest.mark.parametrize("dated", [True, False])
def test_historical_legacy_calendar_scope_and_current_snapshot_are_distinct(
    tmp_path: Path, dated: bool
) -> None:
    from bef_redbird.foundation.registry_selection import _legacy_input
    from tests.test_foundation_selection import NEXT, PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        supplied = [
            {
                "amount_ore": 789,
                "tax_basis": "unknown",
                "fee_basis": "unknown",
                "effective_period": PERIOD if dated else None,
                "report_period": PERIOD if dated else None,
            }
        ]
        delivery = old.document(
            "old-original",
            supplied,
            current_period=date.fromisoformat(PERIOD) if dated else None,
        )
        iid = old.source("old", supplied, delivery=delivery, version="introductions-rtf-v1")
        pin = legacy_pin(
            store, old, iid, calendar_audited=False, auditor_code=HISTORICAL_LEGACY_AUDITOR
        )
        proof_bytes = store.original_path(pin["verification_delivery_id"]).read_bytes()
        native = release(store, tmp_path, "registry", PERIOD)
        config = configuration(store, tmp_path, [native], [PERIOD])
        config["legacy_imports"] = [pin]
        published = publish(store, config)
        bound = published["manifest"]["legacy_inputs"][0]
        assert bound["proof_profile"] == "f4-explicit-import-proof-without-calendar-v1"
        assert bound["calendar_binding"]["original_proof_scope"] == "not_audited"
        assert (
            bound["calendar_binding"]["binding_scope"]
            == "current_referenced_calendar_rows_at_publication"
        )
        expected = [{"period_start": PERIOD, "period_end_exclusive": NEXT}] if dated else []
        assert bound["calendar_binding"]["rows"] == expected
        assert bound["calendar_binding"]["referenced_periods"] == ([PERIOD] if dated else [])
        encoded = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert bound["calendar_binding"]["sha256"] == hashlib.sha256(encoded.encode()).hexdigest()
        assert status(store, published["dataset_id"], verify=True)["verified"]
        assert all(
            row["source_authority"] == "ftp"
            for row in query(store, published["dataset_id"], "prices")
        )
        assert all(
            not row["training_usable"]
            for row in query(store, published["dataset_id"], "legacy_evidence")
        )
        assert store.original_path(pin["verification_delivery_id"]).read_bytes() == proof_bytes
        if dated:
            shadow_calendar(store)
            changed = _legacy_input(store, pin)
            assert changed["input_hash"] != bound["input_hash"]
            assert changed["calendar_binding"]["sha256"] != bound["calendar_binding"]["sha256"]
            with pytest.raises(ValueError, match="Frozen legacy source evidence changed"):
                status(store, published["dataset_id"], verify=True)
            assert published["manifest"]["legacy_inputs"][0] == bound


@pytest.mark.parametrize(
    "changed", ["source_audit.py", "rtf_audit.py", "pdf_audit.py", "missing", "extra"]
)
def test_missing_calendar_legacy_proof_requires_exact_historical_producer(
    tmp_path: Path, changed: str
) -> None:
    from bef_redbird.foundation.registry_selection import _legacy_input
    from tests.test_foundation_selection import Fixture

    auditor = dict(HISTORICAL_LEGACY_AUDITOR)
    if changed == "missing":
        auditor.pop("pdf_audit.py")
    elif changed == "extra":
        auditor["other_reader.py"] = "a" * 64
    else:
        auditor[changed] = "a" * 64
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        iid = old.source("old", [{"amount_ore": 789}], version="introductions-rtf-v1")
        pin = legacy_pin(store, old, iid, calendar_audited=False, auditor_code=auditor)
        with pytest.raises(ValueError, match="Unsupported legacy proof baseline scope or producer"):
            _legacy_input(store, pin)


@pytest.mark.parametrize(
    "table",
    [
        "import_batch",
        "import_issue",
        "source_delivery",
        "package_identifier",
        "package",
        "price_observation",
        "package_observation",
        "classification_observation",
        "supply_observation",
        "lifecycle_event",
        "unknown_extra",
    ],
)
def test_historical_legacy_proof_requires_every_original_table_and_no_unknown_tables(
    tmp_path: Path, table: str
) -> None:
    from bef_redbird.foundation.registry_selection import _legacy_input
    from tests.test_foundation_selection import Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        iid = old.source("old", [{"amount_ore": 789}], version="introductions-rtf-v1")
        pin = legacy_pin(
            store, old, iid, calendar_audited=False, auditor_code=HISTORICAL_LEGACY_AUDITOR
        )
        document = json.loads(store.original_path(pin["verification_delivery_id"]).read_bytes())
        if table == "unknown_extra":
            document["baseline_tables"][table] = {"count": 0, "sha256": "a" * 64}
        else:
            del document["baseline_tables"][table]
        altered = old.document("altered-proof", document)
        with pytest.raises(ValueError, match="Unsupported legacy proof baseline scope or producer"):
            _legacy_input(store, {**pin, "verification_delivery_id": altered})


def test_historical_legacy_metadata_tampering_still_blocks_reuse(tmp_path: Path) -> None:
    from bef_redbird.foundation.registry_selection import _legacy_input
    from tests.test_foundation_selection import Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        iid = old.source("old", [{"amount_ore": 789}], version="introductions-rtf-v1")
        pin = legacy_pin(
            store, old, iid, calendar_audited=False, auditor_code=HISTORICAL_LEGACY_AUDITOR
        )
        store._connection.execute(
            "INSERT INTO import_issue VALUES(uuid(),?,'invented',NULL,NULL,NULL,"
            "'changed','warning','none')",
            [iid],
        )
        with pytest.raises(ValueError, match="metadata, mapping, issues or calendar changed"):
            _legacy_input(store, pin)


def test_modern_legacy_proof_calendar_baseline_remains_mandatory(tmp_path: Path) -> None:
    from bef_redbird.foundation.registry_selection import _legacy_input
    from tests.test_foundation_selection import Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        iid = old.source("old", [{"amount_ore": 789}], version="introductions-rtf-v1")
        pin = legacy_pin(store, old, iid)
        before = _legacy_input(store, pin)
        assert "calendar_binding" not in before and "proof_profile" not in before
        shadow_calendar(store)
        with pytest.raises(ValueError, match="metadata, mapping, issues or calendar changed"):
            _legacy_input(store, pin)


def test_historical_calendar_binding_covers_target_and_observation_periods_and_requires_rows(
    tmp_path: Path,
) -> None:
    from bef_redbird.foundation.registry_selection import _legacy_input
    from tests.test_foundation_selection import NEXT, PERIOD, Fixture

    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        old = Fixture(store, tmp_path)
        delivered = old.document(
            "target-period",
            {},
            current_period=date.fromisoformat(PERIOD),
            target_period=date.fromisoformat(NEXT),
        )
        iid = old.source(
            "old",
            [{"amount_ore": 789, "effective_period": NEXT}],
            delivery=delivered,
            version="introductions-rtf-v1",
        )
        pin = legacy_pin(
            store, old, iid, calendar_audited=False, auditor_code=HISTORICAL_LEGACY_AUDITOR
        )
        bound = _legacy_input(store, pin)
        assert bound["calendar_binding"]["referenced_periods"] == [PERIOD, NEXT]
        result = store._connection.execute("SELECT current_database()").fetchone()
        assert result is not None
        catalog = result[0].replace('"', '""')
        store._connection.execute(
            "CREATE TEMP VIEW price_period AS SELECT * "
            f'FROM "{catalog}".main.price_period WHERE false'
        )
        try:
            with pytest.raises(ValueError, match="Referenced legacy calendar period is missing"):
                _legacy_input(store, pin)
        finally:
            store._connection.execute("DROP VIEW temp.main.price_period")
