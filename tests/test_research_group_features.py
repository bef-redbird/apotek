"""Independent small population expectations for the optional peer sidecar."""

import hashlib
import zipfile
from copy import deepcopy
from datetime import date, timedelta

import duckdb
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.group_features import NUMERICS, period_features, reconcile_native


def population(prices=(100, 200, 301), *, index=0):
    rows, native = [], []
    for i, amount in enumerate(prices):
        package = f"{i + 1:06d}"
        row = {
            "row_key": f"row-{index}-{package}",
            "package_number": package,
            "period_index": index,
            "period_start": date(2020, 1, 6) + timedelta(days=14 * index),
            "canonical_identity": f"identity-{package}",
            "drug_id": f"drug-{package}",
            "eligible": True,
            "group_code": "0042",
            "pack_size_token": "00100000",
            "pack_unit": "ST",
            "package_distributor": "000001" if i < 2 else "000002",
            "aip_ore": amount,
        }
        rows.append(row)
        native.append(
            {**row, "group_pack_size_token": row["pack_size_token"], "native_join_complete": True}
        )
    return rows, native


def test_exact_peer_prices_median_distributors_and_own_exclusion():
    rows, native = population()
    features, _ = period_features(rows, native, {})
    first, second, last = features
    assert first["peer_count"] == 2
    assert first["peer_min_aip_ore"] == 200
    assert first["own_minus_peer_min_aip_ore"] == -100
    assert first["peer_median_aip_twice_ore"] == 501  # 250.5 ore, exact
    assert first["peer_spread_aip_ore"] == 101
    assert first["peer_distributor_count"] == 2
    assert last["peer_distributor_count"] == 1
    assert second["peer_min_aip_ore"] == 100
    assert first["peer_lag1_observed_count"] == 0
    assert first["peer_lag1_rise_share"] is None


def test_complete_group_built_before_partition_distribution():
    rows, native = population()
    full, _ = period_features(rows, native, {})
    # Hash partitions may divide all three members; distribution happens after
    # this population operation, and cannot change either denominator or prices.
    partitions = [[full[0]], [full[1], full[2]]]
    assert {r["peer_count"] for part in partitions for r in part} == {2}
    partial, _ = period_features(rows[:1], native, {})
    assert partial[0]["peer_feature_state"] == "incomplete_or_uncertain_group"
    assert all(partial[0][name] is None for name in NUMERICS)


def test_mixed_size_and_unit_never_share_aip_comparison():
    rows, native = population((100, 200, 300, 400))
    for collection in (rows, native):
        collection[2]["pack_size_token"] = "00200000"
        collection[3]["pack_unit"] = "ML"
    native[2]["group_pack_size_token"] = "00200000"
    output, _ = period_features(rows, native, {})
    assert [r["peer_count"] for r in output] == [1, 1, 0, 0]
    assert output[0]["peer_min_aip_ore"] == 200
    assert output[2]["peer_min_aip_ore"] is None
    assert {r["source_group_member_count"] for r in output} == {4}


def test_lag1_denominator_uses_only_continuous_comparable_peer_histories():
    old, old_native = population()
    _, previous = period_features(old, old_native, {})
    rows, native = population((100, 201, 300), index=1)
    output, _ = period_features(rows, native, previous)
    assert output[0]["peer_lag1_observed_count"] == 2
    assert output[0]["peer_lag1_rise_share"] == 0.5
    assert output[0]["peer_lag1_fall_share"] == 0.5
    # Own rise does not enter its own numerator or denominator.
    assert output[1]["peer_lag1_rise_share"] == 0
    assert output[1]["peer_lag1_fall_share"] == 0.5
    previous["000003"]["canonical_identity"] = "reused-package"
    output, _ = period_features(rows, native, previous)
    assert output[0]["peer_lag1_observed_count"] == 1
    assert output[0]["peer_lag1_rise_share"] == 1
    previous["000002"]["period_index"] = -1
    output, _ = period_features(rows, native, previous)
    assert output[0]["peer_lag1_observed_count"] == 0
    assert output[0]["peer_lag1_rise_share"] is None


def test_group_ids_reused_at_later_date_do_not_create_historical_peers():
    rows, native = population((100, 200))
    original, previous = period_features(rows, native, {})
    later, later_native = population((900, 800), index=4)
    for r in later:
        r["canonical_identity"] = "later-use"
    future, _ = period_features(later, later_native, previous)
    assert future[0]["peer_min_aip_ore"] == 800
    assert future[0]["peer_lag1_observed_count"] == 0
    assert original[0]["peer_min_aip_ore"] == 200


def test_future_mutation_cannot_change_earlier_current_or_lag_features():
    old, native = population()
    before, previous = period_features(old, native, {})
    future, future_native = population((999, 1, 25), index=1)
    for r in future_native:
        r["group_code"] = "9999"
    period_features(future, future_native, deepcopy(previous))
    replay, _ = period_features(old, native, {})
    assert replay == before


def test_unknown_incomplete_scoped_uncertainty_and_ineligible_do_not_drop_own_rows():
    rows, native = population()
    for modifier in (
        "missing_member",
        "unknown_unit",
        "bad_price",
        "uncertainty",
        "missing_snapshot",
    ):
        own, membership = deepcopy(rows), deepcopy(native)
        kwargs = {}
        uncertain = set()
        if modifier == "missing_member":
            own.pop()
        elif modifier == "unknown_unit":
            own[2]["pack_unit"] = membership[2]["pack_unit"] = None
        elif modifier == "bad_price":
            own[2]["eligible"] = False
        elif modifier == "uncertainty":
            uncertain.add("0042")
        else:
            kwargs["complete_snapshot"] = False
        output, _ = period_features(own, membership, {}, uncertain, **kwargs)
        assert len(output) == len(own)
        assert all(r["peer_count"] is None for r in output)
    own, membership = population((100,))
    output, _ = period_features(own, membership, {})
    assert output[0]["peer_count"] == 0
    own[0]["group_code"] = None
    output, _ = period_features(own, [], {})
    assert output[0]["peer_feature_state"] == "unobserved_group"


def test_unknown_peer_distributor_is_null_and_focal_missing_distributor_excluded():
    rows, native = population()
    rows[0]["package_distributor"] = None
    native[0]["package_distributor"] = None
    output, _ = period_features(rows, native, {})
    assert output[0]["peer_distributor_count"] == 2
    assert output[1]["peer_distributor_count"] is None


def test_missing_native_group_identifiers_never_form_a_shared_group():
    for missing in (None, ""):
        rows, native = population()
        for collection in (rows, native):
            for row in collection:
                row["group_code"] = missing
        output, _ = period_features(rows, native, {})
        assert all(row["peer_count"] is None for row in output)
        assert all(row["peer_feature_state"] == "unobserved_group" for row in output)


@pytest.mark.parametrize("fault", [None, "native_count", "persisted_group"])
def test_independent_native_scope_checks_system_counts_and_membership(tmp_path, fault):
    # Literal independently expected LMS04 and own package/price bytes; none of
    # the foundation parsers or record layout helpers produces this fixture.
    group = b"004200000100100000" + b" " * 16 + b"A000001" + b" " * 6
    assert len(group) == 47
    package = bytearray(b" " * 137)
    package[:17] = b"00000000001000001"
    package[59:69] = b"00100000ST"
    package[131:137] = b"000001"
    price = b"000001000000100" + b"0" * 81
    control = bytearray(b" " * 93)
    control[:7] = b"0012.0 "
    control[47:55] = b"20200106"
    control[75:87] = b"LMS.ZIP     "
    system = [bytes(control), b" " * 47]
    for name in (b"LMS02.txt", b"LMS03.txt", b"LMS04.txt"):
        row = bytearray(b" " * 51)
        row[:2], row[32:44], row[46:51] = b"10", name.ljust(12), b"00001"
        if fault == "native_count" and name == b"LMS04.txt":
            row[46:51] = b"00002"
        system.append(bytes(row))
    archive = tmp_path / "original.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("system.txt", b"\r\n".join(system) + b"\r\n")
        writer.writestr("lms02.txt", bytes(package) + b"\r\n")
        writer.writestr("lms03.txt", price + b"\r\n")
        writer.writestr("lms04.txt", group + b"\r\n")
    d = duckdb.connect()
    d.execute("""CREATE TABLE registry_dataset_member(dataset_id VARCHAR,period_start DATE,
      phase VARCHAR,member_number INTEGER,member_id BIGINT,import_id VARCHAR,
      complete_snapshot BOOLEAN,supported_fields VARCHAR[],state VARCHAR)""")
    for member in (2, 3, 4):
        d.execute(
            """INSERT INTO registry_dataset_member VALUES('dataset','2020-01-06','final',?,?,
          'import',true,['package_number','group_code','pack_size_token'],'selected')""",
            [member, member],
        )
    d.execute("CREATE TABLE registry_release(import_id VARCHAR,delivery_id VARCHAR)")
    d.execute("INSERT INTO registry_release VALUES('import','delivery')")
    d.execute(
        "CREATE TABLE source_delivery(delivery_id VARCHAR,archive_location VARCHAR,"
        "content_hash VARCHAR)"
    )
    d.execute(
        "INSERT INTO source_delivery VALUES('delivery','original.zip',?)",
        [hashlib.sha256(archive.read_bytes()).hexdigest()],
    )
    d.execute(
        "CREATE TABLE registry_release_member(import_id VARCHAR,member_number INTEGER,"
        "native_name VARCHAR)"
    )
    d.execute("INSERT INTO registry_release_member VALUES('import',4,'lms04.txt')")
    d.execute(
        "CREATE TABLE registry_member(member_id BIGINT,raw_sha256 VARCHAR,"
        "row_count BIGINT,row_ids BIGINT[])"
    )
    d.execute(
        "INSERT INTO registry_member VALUES(4,?,1,[1])",
        [hashlib.sha256(group + b"\r\n").hexdigest()],
    )
    d.execute(
        "CREATE TABLE registry_dataset_period(dataset_id VARCHAR,period_start DATE,"
        "phase VARCHAR,snapshot_date DATE)"
    )
    d.execute(
        "INSERT INTO registry_dataset_period VALUES('dataset','2020-01-06','final','2020-01-06')"
    )
    d.execute(
        "CREATE TABLE registry_lms04(row_id BIGINT,group_code VARCHAR,package_number VARCHAR,"
        "pack_size_token VARCHAR,abc_code VARCHAR,cheapest_package VARCHAR)"
    )
    d.execute(
        "INSERT INTO registry_lms04 VALUES(1,?,'000001','00100000','A','000001')",
        ["9999" if fault == "persisted_group" else "0042"],
    )
    if fault:
        with pytest.raises(ResearchError, match="population_count|ordered_reconciliation"):
            reconcile_native(d, "dataset", tmp_path, tmp_path)
    else:
        report = reconcile_native(d, "dataset", tmp_path, tmp_path)
        assert report["periods"] == report["rows"] == 1
        assert report["mismatches"] == 0
