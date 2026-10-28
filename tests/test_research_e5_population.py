"""Independent synthetic company-policy and causal-history boundaries."""

from dataclasses import replace
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import duckdb
import pandas as pd
import pytest

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e4_common import frozen_json
from bef_redbird.research.e5_population import (
    HYBRID_POINT,
    EligibilityPolicy,
    _current,
    _history,
    _metrics,
    _role_evidence,
    company_family,
    eligibility,
)
from bef_redbird.research.group_features import pin


def case() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "package_distributor_family": ["BMS", None, None],
            "package_distributor_name": ["BMS", "Other company", None],
            "history_periods": [12, 12, 11],
            "prior_observations": [12, 12, 11],
            "prior_premium_observations": [6, 6, 6],
            "prior_changes_observed": [12, 12, 11],
            "prior_median_premium": [0.20, 0.19, 0.50],
            "prior_change_frequency": [1 / 12, 0, 0],
            "prior_flat_run": [6, 8, 9],
        }
    )


def test_native_text_aliases_do_not_invent_ownership() -> None:
    assert company_family("Bristol-Myers Squibb/Pfizer EEIG") == "BMS"
    assert company_family("BMS") == "BMS"
    assert company_family("Evolan Pharma AB") == "Evolan"
    assert company_family("Bristol Laboratories") is None
    assert company_family("Not Amgen") is None
    assert company_family(None) is None


def test_exact_policy_boundaries_and_missing_retention() -> None:
    result = eligibility(case(), EligibilityPolicy())
    assert result.identity_excluded.tolist() == [True, False, False]
    assert result.lagged_excluded.tolist() == [True, False, False]
    assert result.identity_reason.tolist()[1:] == [
        "retained_other_company",
        "retained_unresolved_company_role",
    ]
    assert result.lagged_reason.iloc[2] == "retained_insufficient_lagged_evidence"
    changed = case()
    changed.loc[0, "prior_premium_observations"] = 5
    assert not eligibility(changed, EligibilityPolicy()).lagged_excluded.iloc[0]
    changed.loc[0, "prior_premium_observations"] = 6
    changed.loc[0, "prior_change_frequency"] = 1 / 12 + 0.000001
    assert not eligibility(changed, EligibilityPolicy()).lagged_excluded.iloc[0]


def test_named_role_is_explicit_and_training_peers_cannot_be_removed() -> None:
    frame = case()
    frame["drug_distributor_family"] = [None, "Amgen", None]
    frame["drug_distributor_name"] = ["Other", "Amgen", None]
    result = eligibility(frame, replace(EligibilityPolicy(), role="drug_distributor"))
    assert result.identity_excluded.tolist() == [False, True, False]
    for kwargs in (
        {"training_exclusion": True},
        {"peer_exclusion": True},
        {"role": "manufacturer"},
    ):
        with pytest.raises(ResearchError):
            EligibilityPolicy(**kwargs)


def test_labels_cannot_change_eligibility() -> None:
    frame = case()
    before = eligibility(frame, EligibilityPolicy())
    frame["next_delta_ore"] = [999999, -999999, 0]
    frame["next_abc"] = ["C", "A", "B"]
    pd.testing.assert_frame_equal(before, eligibility(frame, EligibilityPolicy()))


def test_company_join_uses_its_own_dated_native_dictionary() -> None:
    with duckdb.connect() as db:
        db.execute("ATTACH ':memory:' AS foundation")
        db.execute("""CREATE TABLE foundation.registry_dataset_member(
            period_start DATE,import_id VARCHAR,member_id INT,member_number INT,
            phase VARCHAR,state VARCHAR)""")
        db.execute("""INSERT INTO foundation.registry_dataset_member VALUES
            ('2025-01-06','old',1,1,'final','selected'),
            ('2025-01-20','new',1,1,'final','selected'),
            ('2025-01-06','old',2,9,'final','selected'),
            ('2025-01-20','new',3,9,'final','selected')""")
        db.execute("CREATE TABLE foundation.registry_member(member_id INT,row_ids INT[])")
        db.execute("INSERT INTO foundation.registry_member VALUES(1,[1]),(2,[1]),(3,[2])")
        db.execute("""CREATE TABLE foundation.registry_lms01(row_id INT,drug_id VARCHAR,
            drug_distributor VARCHAR,authorisation_holder VARCHAR)""")
        db.execute("INSERT INTO foundation.registry_lms01 VALUES(1,'drug','CODE','CODE')")
        db.execute("""CREATE TABLE foundation.registry_lms09(row_id INT,company_code VARCHAR,
            company_name VARCHAR,short_name VARCHAR,parallel_import_code VARCHAR)""")
        db.execute("""INSERT INTO foundation.registry_lms09 VALUES
            (1,'CODE','Independent entity',NULL,NULL),(2,'CODE','Amgen',NULL,NULL)""")
        db.execute("""CREATE TABLE history AS SELECT 'old' row_key,'123456' package_number,
            DATE '2025-01-06' period_start,'drug' drug_id,'CODE' package_distributor
            UNION ALL SELECT 'new','123456',DATE '2025-01-20','drug','CODE'""")
        _role_evidence(db)
        result = db.execute("""SELECT period_start,family FROM roles_long
            WHERE role='package_distributor' ORDER BY period_start""").fetchall()
    assert result[0][1] is None
    assert result[1][1] == "Amgen"


def test_fifty_percent_behavior_is_prespecified_alternative() -> None:
    frame = case()
    assert eligibility(
        frame, replace(EligibilityPolicy(), median_premium_min=0.50)
    ).lagged_excluded.tolist() == [False, False, False]


def test_primary_amount_contribution_uses_hybrid_current_fallback_point() -> None:
    frame = pd.DataFrame(
        {
            "package_number": ["123456"],
            "next_status": ["observed"],
            "next_aip_ore": [10000],
            "next_delta_ore": [1000],
            HYBRID_POINT: [110.0],
            "perfect_advance_original_interval_next_aip_dkk": [900.0],
            "perfect_hybrid_p0": [0.1],
            "perfect_hybrid_p1": [0.0],
            "perfect_hybrid_p2": [0.9],
            "peer_count": [1],
            "broad_peer_count": [2],
        }
    )
    result = _metrics(frame)
    assert result["mae_dkk"] == 10
    assert result["sum_brier"] == pytest.approx(0.02)


def test_pending_current_origin_normalizes_dates_and_retains_missing_history(
    tmp_path: Path,
) -> None:
    e4 = tmp_path / "research-e4"
    e4.mkdir()
    assessment_dir = e4 / "assessment"
    assessment_dir.mkdir()
    current = pd.DataFrame(
        {
            "row_key": ["current"],
            "package_number": ["123456"],
            "identity_episode_id": ["new"],
            "period_start": [date(2026, 10, 26)],
            "period_index": [490],
            "history_periods": [0],
            "selected": [True],
            "origin_identity_token": ["00000000001"],
            "package_distributor": ["123456"],
        }
    )
    current.to_parquet(assessment_dir / "predictions.parquet", index=False)
    frozen_json(
        assessment_dir / "assessment.json",
        {"predictions": pin(assessment_dir / "predictions.parquet")},
    )
    frozen_json(
        e4 / "lifecycle.json",
        {
            "assessments": [
                {"origin": "2026-10-26", "assessment": pin(assessment_dir / "assessment.json")}
            ]
        },
    )
    native = tmp_path / "native.zip"
    drug = bytearray(b" " * 180)
    drug[:11] = b"00000000001"
    drug[126:132] = b"123456"
    drug[132:138] = b"123456"
    company = bytearray(b" " * 60)
    company[:6] = b"123456"
    company[26:31] = b"Amgen"
    with ZipFile(native, "w") as archive:
        archive.writestr("lms01.txt", bytes(drug) + b"\r\n")
        archive.writestr("lms09.txt", bytes(company) + b"\r\n")
    snapshot = e4 / "sources/2026-10-26/snapshot.json"
    snapshot.parent.mkdir(parents=True)
    frozen_json(snapshot, {"original": pin(native)})
    history = pd.DataFrame(
        columns=[
            "row_key",
            "package_number",
            "identity_episode_id",
            "period_start",
            "period_index",
            "history_periods",
            "premium",
            "lag1_delta_ore",
            "flat_run",
        ]
    )
    result, proof = _current(tmp_path, history)
    assert pd.api.types.is_datetime64_any_dtype(result.period_start)
    assert result.authorisation_holder_family.tolist() == ["Amgen"]
    assert result.next_status.tolist() == ["pending"]
    assert proof["rows"] == 1
    flags = eligibility(result, EligibilityPolicy())
    assert flags.identity_excluded.tolist() == [True]
    assert flags.lagged_excluded.tolist() == [False]
    result.to_parquet(tmp_path / "verified.parquet", index=False)


def test_history_rolls_strictly_before_origin_and_resets_episode(tmp_path: Path) -> None:
    data = tmp_path
    training = data / "training/bundle"
    peers = data / "research-e2/groups-v1"
    training.mkdir(parents=True)
    peers.mkdir(parents=True)
    periods = pd.date_range("2024-01-01", periods=15, freq="14D")
    features = pd.DataFrame(
        {
            "row_key": [str(i) for i in range(15)],
            "package_number": "123456",
            "period_start": periods,
            "period_index": range(15),
            "identity_episode_id": ["episode1"] * 14 + ["episode2"],
            "eligible": True,
            "history_periods": list(range(14)) + [0],
            "aip_ore": [12000] * 12 + [90000, 12000, 12000],
            "lag1_delta_ore": [None] + [0] * 11 + [78000, -78000, None],
            "abc": "C",
        }
    )
    source = features[["package_number", "period_start"]].assign(
        drug_id="drug", package_distributor="code"
    )
    group = features[["row_key"]].assign(
        peer_count=1, peer_feature_state="complete", peer_min_aip_ore=10000
    )
    features.to_parquet(training / "features.parquet", index=False)
    source.to_parquet(training / "source.parquet", index=False)
    group.to_parquet(peers / "group-features.parquet", index=False)
    frozen_json(
        training / "dataset.json",
        {
            "files": [
                {**pin(training / (role + ".parquet")), "role": role, "path": role + ".parquet"}
                for role in ("source", "features")
            ]
        },
    )
    frozen_json(
        peers / "manifest.json",
        {"files": [{**pin(peers / "group-features.parquet"), "role": "features"}]},
    )
    frozen_json(data / "training/current.json", {"dataset": pin(training / "dataset.json")})
    with duckdb.connect() as db:
        _history(db, data)
        result = db.execute("SELECT * FROM causal ORDER BY period_index").fetchdf()
    assert result.prior_median_premium.iloc[12] == pytest.approx(0.2)
    assert result.prior_change_frequency.iloc[12] == 0
    assert result.prior_premium_observations.iloc[12] == 12
    assert result.prior_flat_run.iloc[12] == 11
    assert result.prior_observations.iloc[14] == 0
    assert pd.isna(result.prior_median_premium.iloc[14])
