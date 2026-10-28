"""Independent synthetic quantities and dated whole-population expectations."""

import hashlib
import json
import zipfile
from copy import deepcopy
from datetime import date, timedelta

import pyarrow.parquet as pq
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e3_peers import (
    NUMERICS,
    _native_quantities,
    period_features,
    scalar_quantity,
)


def population(*, index=0, prices=(101, 200, 601), sizes=(30, 60, 90)):
    rows, native = [], []
    for i, (price, size) in enumerate(zip(prices, sizes, strict=True)):
        row = {
            "row_key": f"{index}-{i}",
            "package_number": f"{i + 1:06}",
            "period_index": index,
            "period_start": date(2020, 1, 6) + timedelta(days=14 * index),
            "canonical_identity": f"identity-{i}",
            "drug_id": f"drug-{i}",
            "eligible": True,
            "group_code": "0042",
            "pack_size_token": f"{size * 100:08}",
            "pack_unit": "ST",
            "package_distributor": "001" if i < 2 else "002",
            "aip_ore": price,
            "peer_feature_state": "complete_equal_size_singleton",
        }
        rows.append(row)
        native.append(
            {
                **row,
                "native_join_complete": True,
                "group_pack_size_token": row["pack_size_token"],
                "pack_description": f"{size} stk. (blister)",
                "component_package": "",
                "component_count_token": "1",
                "unit_short_text": "stk.",
                "unit_description": "stk.",
            }
        )
    return rows, native


def test_exact_own_size_normalization_does_not_count_focal():
    rows, native = population()
    result, _ = period_features(rows, native, {})
    first = result[0]
    assert first["broad_peer_count"] == 2
    assert first["broad_peer_different_size_count"] == 2
    assert first["broad_peer_min_equivalent_aip_ore"] == 100
    assert first["broad_own_minus_peer_min_ore"] == 1
    assert first["min_equivalent_aip_numerator"] == 100
    assert first["min_equivalent_aip_denominator"] == 1
    assert first["broad_peer_median_equivalent_aip_ore"] == pytest.approx((100 + 601 / 3) / 2)
    # For the 90-unit focal the cheapest peer is the 60-unit package, 200*90/60.
    assert result[2]["broad_peer_min_equivalent_aip_ore"] == 300
    assert result[2]["broad_peer_distributor_count"] == 1
    assert first["broad_peer_feature_state"] == "complete_same_unit_peers"


def test_minimum_fraction_remains_exact_until_model_float_boundary():
    rows, native = population(prices=(1, 100, 1))
    result, _ = period_features(rows, native, {})
    assert result[0]["min_equivalent_aip_numerator"] == 1
    assert result[0]["min_equivalent_aip_denominator"] == 3
    assert result[0]["broad_peer_min_equivalent_aip_ore"] == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    "change,state",
    [
        ({"pack_description": "3 x 10 stk. (blister)"}, "complex_or_unknown_description"),
        ({"pack_description": "30 stk. (unknown device)"}, "complex_or_unknown_description"),
        ({"component_package": "012345"}, "complex_component_pack"),
        ({"component_count_token": "3"}, "complex_component_pack"),
        ({"pack_size_token": "00003001"}, "description_numeric_mismatch"),
        ({"pack_size_token": "00000000"}, "invalid_numeric_quantity"),
        ({"unit_description": "other meaning"}, "unit_dictionary_unconfirmed"),
        ({"pack_unit": "SE"}, "unsupported_unit"),
    ],
)
def test_source_meanings_are_required_and_complex_packs_remain_unknown(change, state):
    _, native = population()
    native[0].update(change)
    assert scalar_quantity(native[0]) == (None, state)


def test_simple_decimal_source_quantity_supported_without_cross_unit_conversion():
    rows, native = population(sizes=(3, 6, 9))
    for collection in (rows, native):
        for row in collection:
            row.update(pack_unit="ML", pack_size_token="00000250")
    for member in native:
        member.update(
            pack_description="2,5 ml",
            unit_short_text="ml",
            unit_description="ml",
            group_pack_size_token="00000250",
        )
    assert scalar_quantity(native[0]) == (250, "supported_scalar_quantity")
    # ST and ML never share a denominator despite dated group membership.
    rows[2].update(pack_unit="ST", pack_size_token="00000900")
    native[2].update(
        pack_unit="ST",
        pack_size_token="00000900",
        group_pack_size_token="00000900",
        pack_description="9 stk.",
        unit_short_text="stk.",
        unit_description="stk.",
    )
    result, _ = period_features(rows, native, {})
    assert result[0]["broad_peer_count"] == 1
    assert result[0]["broad_peer_other_unit_count"] == 1
    assert result[0]["broad_peer_feature_state"] == "partial_same_unit_peers"


def test_partial_supported_subset_keeps_unknown_members_and_every_origin():
    rows, native = population()
    native[2]["pack_description"] = "3 x 30 stk."
    result, _ = period_features(rows, native, {})
    assert len(result) == 3
    assert result[0]["broad_peer_population_count"] == 2
    assert result[0]["broad_peer_unknown_quantity_count"] == 1
    assert result[0]["broad_peer_count"] == 1
    assert result[0]["broad_peer_feature_state"] == "partial_same_unit_peers"
    assert result[2]["broad_peer_feature_state"] == "own_quantity_unknown"
    assert result[2]["broad_peer_count"] is None


def test_all_partitions_must_be_aggregated_before_projection():
    rows, native = population()
    result, _ = period_features(rows, native, {})
    assert [r["broad_peer_count"] for part in [result[:1], result[1:]] for r in part] == [2, 2, 2]
    partial, _ = period_features(rows[:1], native, {})
    assert partial[0]["broad_peer_feature_state"] == "incomplete_or_uncertain_group"
    assert all(partial[0][k] is None for k in NUMERICS)


def test_uncertain_membership_and_missing_join_fail_closed_without_dropping_rows():
    for fault in ("native_join", "accepted_uncertain", "no_identity", "mismatched_size"):
        rows, native = population()
        if fault == "native_join":
            native[2]["native_join_complete"] = False
        elif fault == "accepted_uncertain":
            for row in rows:
                row["peer_feature_state"] = "incomplete_or_uncertain_group"
        elif fault == "no_identity":
            rows[2]["canonical_identity"] = None
        else:
            native[2]["group_pack_size_token"] = "00000100"
        result, _ = period_features(rows, native, {})
        assert len(result) == 3
        assert all(r["broad_peer_count"] is None for r in result)


def test_lag_history_requires_origin_identity_continuity_and_own_exclusion():
    old, old_native = population(prices=(100, 199, 602))
    _, previous = period_features(old, old_native, {})
    rows, native = population(index=1)
    result, _ = period_features(rows, native, previous)
    assert result[0]["broad_peer_lag1_observed_count"] == 2
    assert result[0]["broad_peer_lag1_rise_share"] == 0.5
    assert result[0]["broad_peer_lag1_fall_share"] == 0.5
    previous["000003"]["canonical_identity"] = "later-reuse"
    result, _ = period_features(rows, native, previous)
    assert result[0]["broad_peer_lag1_observed_count"] == 1
    assert result[0]["broad_peer_lag1_rise_share"] == 1


def test_future_mutations_cannot_redefine_origin_quantities_or_membership():
    rows, native = population()
    before, previous = period_features(rows, native, {})
    future, future_native = population(index=1, prices=(999, 1, 200))
    for member in future_native:
        member["pack_description"] = "future unknown"
    period_features(future, future_native, deepcopy(previous))
    after, _ = period_features(rows, native, {})
    assert after == before


def test_duplicate_and_cross_period_populations_rejected():
    rows, native = population()
    with pytest.raises(ResearchError, match="duplicate_population"):
        period_features(rows + rows[:1], native, {})
    native[0]["period_start"] = date(2030, 1, 1)
    with pytest.raises(ResearchError, match="mixed_origin"):
        period_features(rows, native, {})


@pytest.mark.parametrize("fault", [None, "wrong_original_pin", "wrong_membership_pin"])
def test_native_quantity_and_dictionary_are_transcribed_from_pinned_original(tmp_path, fault):
    # Independent literal byte boundaries; the registry producer is not used.
    package = bytearray(b" " * 137)
    package[:17] = b"00000000001000001"
    package[26:29] = b"  1"
    package[29:59] = b"30 stk. (blister)".ljust(30)
    package[59:69] = b"00003000ST"
    group = b"004200000100003000" + b" " * 29
    assert len(group) == 47
    unit = b"4ST " + b"stk.".ljust(10) + b"stk.".ljust(50) + b" "
    assert len(unit) == 65
    original = tmp_path / "native.zip"
    with zipfile.ZipFile(original, "w") as writer:
        writer.writestr("LMS02.txt", bytes(package) + b"\r\n")
        writer.writestr("LMS04.txt", group + b"\r\n")
        writer.writestr("LMS15.txt", unit + b"\r\n")
    sha = hashlib.sha256(original.read_bytes()).hexdigest()
    retained = tmp_path / "archive" / "sha256" / sha[:2] / sha
    retained.parent.mkdir(parents=True)
    retained.write_bytes(original.read_bytes())
    member_sha = hashlib.sha256(group + b"\r\n").hexdigest()
    receipt = tmp_path / "native-reconciliation.json"
    receipt.write_text(
        json.dumps(
            {
                "receipts": [
                    {
                        "period": "2020-01-06",
                        "original_sha256": sha,
                        "member_sha256": "wrong" if fault == "wrong_membership_pin" else member_sha,
                        "records": 1,
                    }
                ]
            }
        )
    )
    if fault == "wrong_original_pin":
        retained.write_bytes(b"changed original")
    output = tmp_path / "output"
    output.mkdir()
    e2 = {"files": [{"role": "native_reconciliation", "path": str(receipt)}]}
    if fault:
        with pytest.raises(ResearchError, match="changed"):
            _native_quantities(e2, tmp_path / "archive", output)
    else:
        report = _native_quantities(e2, tmp_path / "archive", output)
        assert report["quantity_states"] == {"supported_scalar_quantity": 1}
        actual = pq.read_table(output / "native-quantities.parquet").to_pylist()[0]
        assert actual["quantity_token_value"] == 3000
        assert actual["unit_description"] == "stk."
        assert actual["pack_description"] == "30 stk. (blister)"
