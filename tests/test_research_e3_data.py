"""Independent synthetic checks of the explicitly allowed one-period ABC lead."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from bef_redbird.research.dataset import ResearchError
from bef_redbird.research.e3_data import ADVANCE_FEATURES, SCENARIOS, E3Data, join_advance, perturb


def calendar() -> list[dict[str, str]]:
    # Deliberately irregular dates prove that calendar succession, not week math, is used.
    return [
        {"start": "2025-01-06", "end_exclusive": "2025-01-21"},
        {"start": "2025-01-21", "end_exclusive": "2025-02-03"},
    ]


def origins() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "row_key": key,
                "package_number": package,
                "period_start": pd.Timestamp("2025-01-06"),
                "origin_identity_token": drug,
                "origin_group": "group1",
                "abc": "B",
                "current_aip_ore": 10000,
                "next_status": outcome,
                "next_aip_ore": amount,
            }
            for key, package, drug, outcome, amount in [
                ("one", "000001", "drug1", "observed", 10000),
                ("two", "000002", "drug2", "pending", None),
                ("three", "000003", "drug3", "episode_changed", None),
                ("four", "000004", "drug4", "unresolved_zero", 0),
            ]
        ]
    )


def classifications() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "period_start": pd.Timestamp(period),
                "package_number": package,
                "code": code,
                "state": state,
                "identity_token": drug,
                "source": "synthetic-native",
            }
            for period, package, code, state, drug in [
                ("2025-01-21", "000001", "A", "observed", "drug1"),
                ("2025-01-21", "000003", "C", "observed", "drug3"),
                ("2025-01-21", "999999", "A", "observed", "future-entrant"),
                ("2025-02-03", "000001", "C", "observed", "drug1"),
            ]
        ]
    )


def test_only_immediate_next_classification_crosses_the_boundary() -> None:
    source = origins()
    native = classifications()
    expected = join_advance(source, native, calendar())
    assert expected.advance_abc.tolist()[0] == "A"
    assert expected.target_period.eq(pd.Timestamp("2025-01-21")).all()
    # Prices, future memberships and two-period-ahead ABC have no effect whatsoever.
    changed = native.copy()
    changed["next_aip_ore"] = [1, 999999999, 0, -1]
    changed["next_group"] = "forbidden_future_group"
    changed.loc[changed.period_start.eq(pd.Timestamp("2025-02-03")), "code"] = "B"
    assert_frame_equal(expected, join_advance(source, changed, calendar()))
    assert "next_group" not in expected
    assert ADVANCE_FEATURES == ["advance_abc"]


def test_advance_join_preserves_all_origin_candidates_and_missing_outcomes() -> None:
    source = origins()
    joined = join_advance(source, classifications(), calendar())
    assert joined.row_key.tolist() == source.row_key.tolist()
    assert joined.package_number.tolist() == ["000001", "000002", "000003", "000004"]
    assert len(joined) == 4  # Neither future entrant nor surviving-only filtering.
    assert joined.advance_state.tolist() == [
        "observed",
        "missing_final_classification",
        "observed",
        "missing_final_classification",
    ]
    assert joined.loc[0, "next_aip_ore"] == 10000  # An actual unchanged price.
    assert pd.isna(joined.loc[1, "next_aip_ore"])  # Missing never becomes unchanged.
    assert_frame_equal(
        joined[source.columns.difference(["origin_identity_token"])],
        source[source.columns.difference(["origin_identity_token"])],
    )


@pytest.mark.parametrize("missing_side", ["future", "origin", "changed"])
def test_ambiguous_identity_suppresses_only_the_advance_code(missing_side: str) -> None:
    source, native = origins(), classifications()
    if missing_side == "future":
        native.loc[0, "identity_token"] = None
    elif missing_side == "origin":
        source.loc[0, "origin_identity_token"] = None
    else:
        native.loc[0, "identity_token"] = "reused-package-different-drug"
    joined = join_advance(source, native, calendar())
    assert joined.loc[0, "advance_state"] == "ambiguous_identity"
    assert pd.isna(joined.loc[0, "advance_abc"])
    assert joined.loc[0, "abc"] == "B" and joined.loc[0, "current_aip_ore"] == 10000
    assert joined.row_key.tolist() == source.row_key.tolist()


@pytest.mark.parametrize(
    "state,code",
    [
        ("unsupported_code", "D"),
        ("scoped_classification_exclusion", "A"),
        ("missing_code", None),
        ("ambiguous", "B"),
    ],
)
def test_unsupported_and_excluded_classification_states_do_not_supply_proxy(
    state: str, code: str | None
) -> None:
    native = classifications()
    native.loc[0, ["state", "code"]] = [state, code]
    joined = join_advance(origins(), native, calendar())
    assert joined.loc[0, "advance_state"] == state
    assert pd.isna(joined.loc[0, "advance_abc"])
    assert joined.loc[0, "next_status"] == "observed"


@pytest.mark.parametrize(
    "mutation", ["duplicate_origin", "duplicate_classification", "unknown_origin_calendar"]
)
def test_unresolved_keys_and_calendar_fail_explicitly(mutation: str) -> None:
    source, native = origins(), classifications()
    if mutation == "duplicate_origin":
        source.loc[1, "row_key"] = source.loc[0, "row_key"]
    elif mutation == "duplicate_classification":
        native = pd.concat([native, native.iloc[[0]]], ignore_index=True)
    else:
        source.loc[0, "period_start"] = pd.Timestamp("2024-12-01")
    with pytest.raises(ResearchError):
        join_advance(source, native, calendar())


def sensitivity_population() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "row_key": f"row-{index}",
                "period_start": pd.Timestamp("2025-01-06"),
                "origin_group": f"group-{index // 5}",
                "advance_abc": ["A", "B", "C"][index % 3],
                "advance_state": "observed",
                "abc": "C",
                "next_status": "observed",
                "next_aip_ore": 10000 + index,
                "next_direction": "rise",
            }
            for index in range(300)
        ]
    )


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_sensitivity_is_common_deterministic_and_never_changes_outcomes(scenario: str) -> None:
    source = sensitivity_population()
    snapshot = source.copy(deep=True)
    result = perturb(source, scenario)
    assert_frame_equal(source, snapshot)
    assert_frame_equal(result, perturb(source, scenario))
    unchanged = [column for column in source if column not in {"advance_abc", "advance_state"}]
    assert_frame_equal(result[unchanged], source[unchanged])
    reordered = source.sample(frac=1, random_state=7).reset_index(drop=True)
    assert_frame_equal(
        result.sort_values("row_key").reset_index(drop=True),
        perturb(reordered, scenario).sort_values("row_key").reset_index(drop=True),
    )
    if scenario == "perfect":
        assert_frame_equal(result, source)
    elif scenario == "missing100":
        assert result.advance_abc.isna().all()
        assert result.advance_state.eq("missing_final_classification").all()
    else:
        changed = result.advance_abc.ne(source.advance_abc)
        assert changed.any() and not changed.all()
        if scenario == "group_wrong20":
            assert changed.groupby(source.origin_group).nunique().eq(1).all()


def test_sensitivity_preserves_unavailable_codes_and_uses_missing_as_absence() -> None:
    source = sensitivity_population()
    source.loc[0, ["advance_abc", "advance_state"]] = [None, "ambiguous_identity"]
    source.loc[1, ["advance_abc", "advance_state"]] = [None, "scoped_classification_exclusion"]
    result = perturb(source, "missing100")
    assert result.loc[0, "advance_state"] == "ambiguous_identity"
    assert result.loc[1, "advance_state"] == "scoped_classification_exclusion"
    assert result.loc[2:, "advance_state"].eq("missing_final_classification").all()


def sidecar_fixture(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path, list[Any]]:
    """Replace only accepted R1/E2 loading; exercise actual E3 materialization and joins."""
    from bef_redbird.research.e2_data import ResearchData

    features = pd.DataFrame(
        {
            "row_key": ["old", "current-one", "current-two"],
            "package_number": ["000001", "000001", "000002"],
            "period_start": pd.to_datetime(["2018-12-24", "2025-01-06", "2025-01-06"]),
            "eligible": [True, True, True],
            "abc": ["A", "B", "C"],
            "provenance": ["synthetic"] * 3,
        }
    )
    connections: list[Any] = []

    def base_init(self: Any, bundle: Path, groups: Path, *, scratch: Path) -> None:
        self.connection = duckdb.connect()
        connections.append(self.connection)
        self.connection.register("_synthetic_features", features)
        self.connection.execute("CREATE TABLE features AS SELECT * FROM _synthetic_features")
        self.connection.execute("CREATE VIEW predictors AS SELECT * FROM features")
        self.spill = None

    monkeypatch.setattr(ResearchData, "__init__", base_init)
    bundle, groups = root / "bundle.json", root / "groups.json"
    bundle.write_text('{"fixture":"R1"}')
    groups.write_text('{"fixture":"E2"}')

    def pin(path: Path, role: str | None = None) -> dict[str, Any]:
        content = path.read_bytes()
        return {
            "path": path.name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            **({"role": role} if role else {}),
        }

    advance = features.loc[
        features.period_start >= pd.Timestamp("2019-01-01"),
        ["row_key", "package_number", "period_start"],
    ].copy()
    advance["target_period"] = pd.Timestamp("2025-01-21")
    advance["advance_abc"] = ["A", None]
    advance["advance_state"] = ["observed", "missing_final_classification"]
    advance["advance_source"] = "synthetic-final"
    advance["origin_group"] = "group-one"
    broader = features[["row_key", "package_number", "period_start"]].copy()
    broader["broader_peer_count"] = [1, 2, 3]
    paths = []
    for name, frame, binding in [("advance", advance, "groups"), ("broader", broader, "e2_groups")]:
        data_path = root / f"{name}.parquet"
        frame.to_parquet(data_path, index=False)
        manifest = root / f"{name}.json"
        value = {"bundle": pin(bundle), binding: pin(groups), "files": [pin(data_path, "features")]}
        if name == "broader":
            value.update(feature_names=["broader_peer_count"], categorical_names=[])
        manifest.write_text(json.dumps(value))
        paths.append(manifest)
    return bundle, groups, paths[0], paths[1], connections


def test_sidecar_constructor_preserves_all_origins_and_restricts_advance_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, groups, advance, broader, connections = sidecar_fixture(tmp_path, monkeypatch)
    try:
        data = E3Data(bundle, groups, advance, broader, scratch=tmp_path / "scratch")
        frame = data.candidates("2018-01-01", "2026-01-01")
        assert frame.row_key.tolist() == ["old", "current-one", "current-two"]
        assert pd.isna(frame.loc[0, "advance_abc"])
        assert frame.loc[1, "advance_abc"] == "A"
        assert frame.loc[2, "advance_state"] == "missing_final_classification"
        assert frame.broader_peer_count.tolist() == [1, 2, 3]
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("role", ["advance", "broader"])
@pytest.mark.parametrize(
    "mutation", ["bundle", "groups", "missing", "extra", "duplicate", "package", "period"]
)
def test_sidecar_constructor_rejects_wrong_bindings_and_identity_populations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, mutation: str
) -> None:
    bundle, groups, advance, broader, connections = sidecar_fixture(tmp_path, monkeypatch)
    manifest_path = advance if role == "advance" else broader
    value = json.loads(manifest_path.read_text())
    if mutation in {"bundle", "groups"}:
        binding = (
            "bundle" if mutation == "bundle" else ("groups" if role == "advance" else "e2_groups")
        )
        value[binding]["sha256"] = "0" * 64
    else:
        path = tmp_path / value["files"][0]["path"]
        frame = pd.read_parquet(path)
        if mutation == "missing":
            frame = frame.iloc[1:]
        elif mutation == "extra":
            extra = frame.iloc[[0]].copy()
            extra["row_key"] = "unadmitted-future-candidate"
            frame = pd.concat([frame, extra], ignore_index=True)
        elif mutation == "duplicate":
            frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        elif mutation == "package":
            frame.loc[0, "package_number"] = "999999"
        else:
            frame.loc[0, "period_start"] = pd.Timestamp("2026-01-01")
        frame.to_parquet(path, index=False)
        content = path.read_bytes()
        value["files"][0].update(
            sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content)
        )
    manifest_path.write_text(json.dumps(value))
    try:
        with pytest.raises(ResearchError):
            E3Data(bundle, groups, advance, broader, scratch=tmp_path / "scratch")
    finally:
        for connection in connections:
            connection.close()
