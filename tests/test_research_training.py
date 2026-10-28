"""Invented full-history records: causal episodes, exact labels and frozen readback."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from catboost import Pool

from bef_redbird.foundation import Store
from bef_redbird.foundation.registry_selection import publish
from bef_redbird.research import training
from bef_redbird.research.catboost_data import build_pool, category_token
from bef_redbird.research.dataset import ResearchError, read_json
from bef_redbird.research.training_contract import (
    CATEGORIES,
    FEATURE_SCHEMA,
    FEATURES,
    LABEL_SCHEMA,
    atomic_json,
    data_dictionary,
    pin,
)
from bef_redbird.research.training_reader import load_bundle
from tests.test_foundation_lms_ftp import members, populated
from tests.test_foundation_registry_selection import configuration, release


def test_catboost_native_numeric_storage_rounding_does_not_change_exact_input() -> None:
    source = np.array([[16777217.0], [999999999.0], [np.nan]], dtype=np.float64)
    pool = Pool(source, feature_names=["synthetic_ore"])
    native = pool.get_features()
    assert native.dtype == np.float32
    assert native[:2, 0].tolist() == [16777216.0, 1000000000.0]
    assert np.isnan(native[2, 0])
    assert source[:2, 0].tolist() == [16777217.0, 999999999.0]
    assert not pool.is_quantized()


def calendar(count: int = 8) -> list[dict[str, Any]]:
    start = date(2008, 1, 14)
    return [
        {
            "index": i,
            "start": (start + timedelta(days=i * 14)).isoformat(),
            "end_exclusive": (start + timedelta(days=(i + 1) * 14)).isoformat(),
        }
        for i in range(count)
    ]


def raw(index: int, amount: int | None, **changes: Any) -> dict[str, Any]:
    return {
        "period_start": date.fromisoformat(calendar()[index]["start"]),
        "package_number": "000007",
        "canonical_identity": "native-episode-one",
        "drug_id": "00000000001",
        "aip_ore": amount,
        "price_state": "present",
        "currency": "DKK",
        "tax_basis": "excluding_vat",
        "fee_basis": "excluding_consumer_dispensing_fees",
        "price_basis": "per_package",
        "price_role": "effective_price",
        "creation_date": date(2008, 1, 14),
        "exit_date": None,
        "dispensing_code": "B",
        "abc": "A",
        "group_code": "0001",
        "classification_state": "observed",
        "provenance": '{"invented":true}',
        **changes,
    }


def test_integer_ore_labels_and_causal_future_exclusion() -> None:
    originals = [raw(i, value) for i, value in enumerate([100, 101, 101, 100, 102, 101, 103, 104])]
    features, labels = training.package_rows(originals, calendar(), [], set())
    assert labels[0]["next_direction"] == 1
    assert labels[0]["next_delta_ore"] == 1
    assert labels[1]["reversal_category"] == "full_reversal"
    assert labels[4]["reversal_category"] == "partial_reversal"
    assert labels[6]["reversal_status"] == "pending"
    assert labels[-1]["next_status"] == "pending"
    assert features[3]["lag1_delta_ore"] == -1
    assert features[3]["history_periods"] == 3
    future_changed = originals[:4] + [raw(i, 9999, atc="FUTURE") for i in range(4, 8)]
    changed, _ = training.package_rows(future_changed, calendar(), [], set())
    assert changed[:4] == features[:4]


@pytest.mark.parametrize("boundary", ["gap", "ineligible", "identity"])
def test_history_and_future_labels_break_at_boundary(boundary: str) -> None:
    rows = [raw(i, 100 + i) for i in range(5)]
    if boundary == "gap":
        rows.pop(2)
    elif boundary == "ineligible":
        rows[2]["aip_ore"] = 0
    else:
        for row in rows[2:]:
            row["canonical_identity"] = "new-identity"
    features, labels = training.package_rows(rows, calendar(), [], set())
    assert (
        labels[1]["next_status"]
        == {"gap": "gap", "ineligible": "ineligible", "identity": "identity_change"}[boundary]
    )
    restart = next(f for f in features if f["period_index"] == (2 if boundary == "identity" else 3))
    assert restart["history_periods"] == 0
    assert restart["lag1_delta_ore"] is None
    assert restart["identity_episode_id"] != features[0]["identity_episode_id"]


def test_abc_scope_does_not_remove_supported_aip_and_group_uncertainty_propagates() -> None:
    exclusions = [{"date": calendar()[0]["start"], "package": "000007", "field": "abc_code"}]
    features, labels = training.package_rows(
        [raw(0, 100), raw(1, 105)], calendar(), exclusions, set()
    )
    assert features[0]["eligible"] and features[0]["abc"] is None
    assert labels[0]["next_delta_ore"] == 5
    assert features[0]["group_state"] == "scoped_source_inequality"
    peers, _ = training.package_rows(
        [raw(0, 100, package_number="000008")],
        calendar(),
        exclusions,
        {(calendar()[0]["start"], "0001")},
    )
    assert peers[0]["group_state"] == "scoped_group_uncertainty"
    assert "group_code" not in FEATURES


def test_old_categorical_values_missingness_and_exact_large_integer_survive() -> None:
    features, labels = training.package_rows(
        [raw(0, 2**53 + 1, atc="OLD"), raw(1, 2**53 + 2, atc="NEW")], calendar(), [], set()
    )
    assert features[0]["atc"] == "OLD"
    assert labels[0]["next_delta_ore"] == 1
    table = pa.Table.from_pylist(features, schema=FEATURE_SCHEMA)
    assert table.to_pylist()[0]["aip_ore"] == 2**53 + 1
    assert (
        len({category_token(None), category_token(""), category_token("M:"), category_token("V:")})
        == 4
    )


@pytest.fixture
def producer_fixture(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    database, archive = tmp_path / "foundation.duckdb", tmp_path / "originals"
    days = [p["start"] for p in calendar(4)]
    with Store(database, archive) as store:
        imports = []
        for i, amount in enumerate([100, 101, 100, 102]):
            native = members()
            package = bytearray(native["lms02.txt"])
            package[73:78] = b"B    "
            package[103:111] = b"20080114"
            native["lms02.txt"] = bytes(package)
            native["lms03.txt"] = native["lms03.txt"].replace(
                b"000012345", f"{amount:09d}".encode()
            )
            native["lms04.txt"] = (
                populated(47, {(1, 4): b"0001", (5, 10): b"000007", (35, 35): b"A"}) + b"\r\n"
            )
            imports.append(
                release(
                    store,
                    tmp_path,
                    f"release-{i}",
                    days[i],
                    price=amount,
                    creation="20080114",
                    extra_members=native,
                )
            )
        config = configuration(store, tmp_path, imports, days)
        config["semantic_profiles"][0]["monetary_context"]["3.aip_ore"]["fee_basis"] = (
            "excluding_consumer_dispensing_fees"
        )
        fields = config["semantic_profiles"][0]["fields"]
        fields["2"].append("dispensing_code")
        fields["4"] = ["package_number", "group_code", "abc_code"]
        result = publish(store, config)
    ledger, recovery = tmp_path / "ledger.json", tmp_path / "recovery.json"
    ledger.write_text('{"records":[]}')
    recovery.write_text('{"recovery":"invented test evidence"}')
    return {
        "database": str(database),
        "dataset_id": result["dataset_id"],
        "partitions": 3,
        "exclusion_ledger": str(ledger),
        "recovery_references": [str(recovery)],
        "windows": [{"name": "development", "start": days[0], "end_exclusive": "2009-01-01"}],
        "exposure": {"accepted_e1_window": "already exposed; no untouched claim"},
    }, tmp_path / "bundle"


def test_actual_producer_reader_pool_and_idempotent_retry(
    producer_fixture: tuple[dict[str, Any], Path],
) -> None:
    config, output = producer_fixture
    with duckdb.connect(config["database"], read_only=True) as connection:
        assert training.raw_key_proof(connection, config["dataset_id"])["safe"]
        for bucket in range(3):
            ordinary, n = training.extraction_sql(bucket, 3)
            raw_pruned, count = training.extraction_sql(bucket, 3, raw_key=True)
            assert n == count
            assert (
                connection.execute(ordinary, [config["dataset_id"]] * n).fetchall()
                == connection.execute(raw_pruned, [config["dataset_id"]] * n).fetchall()
            )
    result = training.produce(config, output)
    assert result["rows"] == 4
    bundle = load_bundle(output / "dataset.json")
    pairs = list(bundle.iter_pairs())
    assert [f["aip_ore"] for f, _ in pairs] == [100, 101, 100, 102]
    assert all(f["eligible"] for f, _ in pairs)
    original = (output / "dataset.json").read_bytes()
    with patch.object(
        training, "extraction_sql", side_effect=AssertionError("completed extraction reran")
    ):
        assert training.produce(config, output)["rows"] == 4
    assert (output / "dataset.json").read_bytes() == original
    pool = build_pool(
        bundle, start="2008-01-14", end_exclusive="2008-02-25", mature_before="2008-03-10"
    )
    assert pool.pool.num_row() == 3
    assert pool.exact_labels == (1, -1, 1)
    assert pool.pool.get_feature_names() == list(FEATURES)
    assert pool.pool.get_cat_feature_indices() == [FEATURES.index(name) for name in CATEGORIES]
    assert pool.report["numeric_null_counts"]["lag1_delta_ore"] == 1
    assert pool.report["model_fitted"] is False


def test_failed_partition_resume_preserves_completed_work(
    producer_fixture: tuple[dict[str, Any], Path],
) -> None:
    config, output = producer_fixture
    original_transform = training.transform_partition
    calls = 0

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("invented interruption")
        return original_transform(*args, **kwargs)

    with patch.object(training, "transform_partition", interrupted), pytest.raises(OSError):
        training.produce(config, output)
    assert not (output / "dataset.json").exists()
    state = read_json(output / "checkpoint.json")
    assert len(state["completed"]) == 1
    pinned = state["completed"]["1"]["files"]
    times = [(output / p["path"]).stat().st_mtime_ns for p in pinned]
    assert training.produce(config, output)["rows"] == 4
    assert [(output / p["path"]).stat().st_mtime_ns for p in pinned] == times


@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "missing",
        "schema",
        "label",
        "feature",
        "categorical",
        "eligibility",
        "inventory",
        "status",
    ],
)
def test_tamper_and_incomplete_fail_closed(
    producer_fixture: tuple[dict[str, Any], Path], mutation: str
) -> None:
    config, output = producer_fixture
    training.produce(config, output)
    manifest_path = output / "dataset.json"
    manifest = read_json(manifest_path)
    fp = next(p for p in manifest["files"] if p["role"] == "features" and p["rows"])
    lp = next(p for p in manifest["files"] if p["role"] == "labels" and p["rows"])
    if mutation == "bytes":
        (output / fp["path"]).write_bytes(b"corrupt")
    elif mutation == "missing":
        (output / fp["path"]).unlink()
    elif mutation in {"schema", "feature", "label", "categorical", "eligibility"}:
        entry = lp if mutation == "label" else fp
        path = output / entry["path"]
        table = pq.read_table(path)
        if mutation == "schema":
            table = table.drop(["abc"])
        else:
            rows = table.to_pylist()
            if mutation == "categorical":
                rows[0]["atc"] = "FUTURE"
            elif mutation == "eligibility":
                rows[0]["eligible"] = False
                rows[0]["exclusion_reason"] = "invented"
            else:
                rows[0]["next_direction" if mutation == "label" else "history_periods"] = (
                    0 if mutation == "label" else 999
                )
            table = pa.Table.from_pylist(
                rows, schema=LABEL_SCHEMA if mutation == "label" else FEATURE_SCHEMA
            )
        pq.write_table(table, path)
        entry.update(pin(path, output, entry["role"], table.num_rows))
    elif mutation == "inventory":
        manifest["files"].remove(lp)
    else:
        manifest["status"] = "incomplete"
    atomic_json(manifest_path, manifest)
    with pytest.raises((ResearchError, pa.ArrowInvalid)):
        load_bundle(manifest_path)


def test_reference_relocation_and_changed_reference_fail(
    producer_fixture: tuple[dict[str, Any], Path], tmp_path: Path
) -> None:
    config, output = producer_fixture
    training.produce(config, output)
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    for name in ("recovery.json", "ledger.json"):
        shutil.move(str(tmp_path / name), relocated / name)
    load_bundle(output / "dataset.json", relocations={str(tmp_path): str(relocated)})
    (relocated / "recovery.json").write_text("changed")
    with pytest.raises(ResearchError, match="reference_changed"):
        load_bundle(output / "dataset.json", relocations={str(tmp_path): str(relocated)})


def test_atomic_checkpoint_interruption_does_not_replace_previous(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    atomic_json(path, {"before": True})
    before = path.read_bytes()
    with (
        patch("bef_redbird.research.training_contract.os.replace", side_effect=OSError),
        pytest.raises(OSError),
    ):
        atomic_json(path, {"after": True})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_refresh_current_pointer_requires_readback_and_pool(
    producer_fixture: tuple[dict[str, Any], Path],
    tmp_path: Path,
) -> None:
    config, _ = producer_fixture
    operation = tmp_path / "operations"
    operation.mkdir()
    atomic_json(
        operation / "publication-state.json", {"result": {"dataset_id": config["dataset_id"]}}
    )
    operating = tmp_path / "operating.json"
    atomic_json(operating, {"database": config["database"], "operation_root": str(operation)})
    template_path = tmp_path / "template.json"
    template = {
        key: value for key, value in config.items() if key not in {"database", "dataset_id"}
    }
    template["pool_window"] = {
        "start": "2008-01-14",
        "end_exclusive": "2008-02-25",
        "mature_before": "2008-03-10",
    }
    atomic_json(template_path, template)
    root = tmp_path / "training"
    with (
        patch(
            "bef_redbird.research.catboost_data.build_pool",
            side_effect=RuntimeError("interrupted Pool"),
        ),
        pytest.raises(RuntimeError),
    ):
        training.refresh(operating, template_path, root)
    assert not (root / "current.json").exists()
    pointer = training.refresh(operating, template_path, root)
    assert pointer["status"] == "verified_readback_and_pool_no_fit"
    previous = (root / "current.json").read_bytes()
    training.refresh(operating, template_path, root)
    assert (root / "current.json").read_bytes() == previous
    assert len(list(root.glob("*/dataset.json"))) == 1


def test_compact_scoped_tsp_relation_is_preserved() -> None:
    item = {
        "field": "reimbursement_basis.per_package",
        "underlying_value_relation": "unequal_positive_exact_amounts",
        "package": "000007",
        "effective_date": "2008-01-14",
        "phase": "final",
        "id": "invented",
    }
    assert len(training.compact_exclusions({"records": [item]})) == 1


def test_evidenced_native_aip_context_is_admitted_without_implicit_fee_aliases() -> None:
    # Matches every current real canonical semantic profile, including early vintages.
    row = raw(0, 100, fee_basis="excluding_consumer_dispensing_fees")
    assert training.exclusion_reason(row) == ""
    for unsupported in (
        "unknown",
        "excluded",
        "excluding_dispensing_fees",
        "including_consumer_dispensing_fees",
    ):
        assert (
            training.exclusion_reason({**row, "fee_basis": unsupported})
            == "unsupported_aip_context"
        )


def test_observed_rise_persistence_and_both_future_prices_required() -> None:
    _, labels = training.package_rows(
        [raw(i, value) for i, value in enumerate([100, 101, 102, 103])], calendar(), [], set()
    )
    assert labels[1]["reversal_category"] == "persistence"
    assert labels[2]["reversal_status"] == "gap"
    assert labels[2]["reversal_category"] is None


def test_raw_key_pruning_requires_every_selected_scope_to_support_key() -> None:
    with duckdb.connect() as connection:
        connection.execute(
            "CREATE TABLE registry_dataset_member(dataset_id VARCHAR,member_number INTEGER,"
            "state VARCHAR,supported_fields VARCHAR[])"
        )
        connection.execute(
            "INSERT INTO registry_dataset_member VALUES "
            "('test',2,'selected',['package_number']),('test',3,'selected',[]),"
            "('test',4,'unsupported',[])"
        )
        proof = training.raw_key_proof(connection, "test")
        assert proof["safe"] is False
        assert proof["members"][1]["unsupported_key_scopes"] == 1


def test_dictionary_exact_order_units_and_future_label_boundary() -> None:
    contract = data_dictionary()
    features = contract["features"]
    assert [entry["name"] for entry in features] == list(FEATURES)
    assert [entry["type"] for entry in features] == [
        str(FEATURE_SCHEMA.field(name).type) for name in FEATURES
    ]
    assert all(entry["units"] and entry["null_meaning"] for entry in features)
    assert features[0]["type"] == "int64" and "100 ore = 1 DKK" in features[0]["units"]
    assert set(entry["name"] for entry in contract["labels"]).isdisjoint(FEATURES)
    assert all(
        entry["predictor"] is False for entry in contract["labels"] + contract["auxiliary_fields"]
    )
    assert contract["horizons"]["observed_rise_reversal"] == 2
    assert "strictly earlier" in contract["horizons"]["training_maturity"]


def test_rehashed_wrong_dictionary_fails_contract_readback(
    producer_fixture: tuple[dict[str, Any], Path],
) -> None:
    config, output = producer_fixture
    training.produce(config, output)
    manifest_path, dictionary_path = output / "dataset.json", output / "contract.json"
    manifest, dictionary = read_json(manifest_path), read_json(dictionary_path)
    dictionary["features"][0]["units"] = "DKK rather than ore"
    atomic_json(dictionary_path, dictionary)
    entry = next(p for p in manifest["files"] if p["path"] == "contract.json")
    entry.update(pin(dictionary_path, output, "data_dictionary"))
    atomic_json(manifest_path, manifest)
    with pytest.raises(ResearchError, match="data_dictionary_changed"):
        load_bundle(manifest_path)


@pytest.mark.parametrize("changed_pin", ["dataset.py", "registry_read_contract"])
def test_used_helper_or_query_contract_change_selects_new_refresh_version_before_export(
    tmp_path: Path,
    changed_pin: str,
) -> None:
    database, ledger, recovery = (
        tmp_path / "unused-database",
        tmp_path / "ledger.json",
        tmp_path / "recovery.json",
    )
    database.write_bytes(b"Not opened: version routing is intercepted before extraction.")
    ledger.write_text('{"records":[]}')
    recovery.write_text('{"retained":"synthetic"}')
    operation = tmp_path / "operations"
    operation.mkdir()
    atomic_json(operation / "publication-state.json", {"result": {"dataset_id": "synthetic"}})
    operating, template = tmp_path / "operating.json", tmp_path / "template.json"
    atomic_json(operating, {"database": str(database), "operation_root": str(operation)})
    atomic_json(template, {"recovery_references": [str(recovery)], "exclusion_ledger": str(ledger)})
    original_pins = training.code_pins()
    assert original_pins["dataset.py"] == training.digest(
        Path(training.__file__).with_name("dataset.py")
    )
    versions: list[Path] = []

    def before_export(_configuration: Any, output: Path, **_kwargs: Any) -> Any:
        versions.append(output)
        raise RuntimeError("intercepted before source read or export")

    with patch.object(training, "produce", before_export):
        with pytest.raises(RuntimeError, match="intercepted"):
            training.refresh(operating, template, tmp_path / "bundles")
        with (
            patch.object(
                training, "code_pins", return_value={**original_pins, changed_pin: "f" * 64}
            ),
            pytest.raises(RuntimeError, match="intercepted"),
        ):
            training.refresh(operating, template, tmp_path / "bundles")
    assert len(versions) == 2 and versions[0] != versions[1]
    assert not any(path.exists() for path in versions)
