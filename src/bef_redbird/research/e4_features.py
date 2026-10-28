"""One-period causal extension of accepted R1/E2/E3 meanings, without a database rebuild."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, cast

import duckdb
import numpy as np
import pandas as pd

from bef_redbird.foundation.lms_registry_layout import LAYOUTS

from . import e3_peers, group_features
from .dataset import ResearchError, fingerprint
from .e4_common import checked, frozen_json, read, utc
from .group_features import pin
from .training_contract import CATEGORIES, FEATURES, KEYS, VERSION, exclusion_reason, row_key


def period_read(paths: list[Path], day: str) -> pd.DataFrame:
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        db.execute("SET memory_limit='2GB'")
        return (
            db.execute(
                "SELECT * FROM read_parquet(?) WHERE period_start=? ORDER BY package_number",
                [[str(p) for p in paths], day],
            )
            .fetch_arrow_table()
            .to_pandas(date_as_object=True)
        )


def records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]], frame.astype(object).where(pd.notna(frame), None).to_dict("records")
    )


def money_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Keep exact ore and missing values through pandas and Parquet round trips."""
    frame = pd.DataFrame(rows)
    for name in ("aip_ore", "lag1_delta_ore", "lag2_delta_ore", "lag3_delta_ore"):
        if name in frame:
            # Use original scalars before DataFrame inference can promote to float.
            frame[name] = pd.array([row[name] for row in rows], dtype="Int64")
    return frame


def native(snapshot: Path, previous: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Decode same-edition native attributes; identity uses the accepted causal recipe."""
    saved = read(snapshot)
    day = date.fromisoformat(saved["period"])
    original = checked(saved["original"])
    tables: dict[int, list[dict[str, Any]]] = {}
    with zipfile.ZipFile(original) as archive:
        names = {x.lower(): x for x in archive.namelist()}
        for number in (1, 2, 3, 4, 15):
            width, fields = LAYOUTS[number]
            values = []
            for line in group_features._native_lines(
                archive.read(names[f"lms{number:02}.txt"]), width
            ):
                row = {}
                for name, start, end, kind in fields:
                    token = line[start - 1 : end].decode("cp850").strip()
                    value: Any = token or None
                    if kind == "ore":
                        value = int(token) if token.isdigit() else None
                    elif kind == "date":
                        try:
                            value = date(int(token[:4]), int(token[4:6]), int(token[6:8]))
                        except (ValueError, TypeError):
                            value = None
                    row[name] = value
                values.append(row)
            tables[number] = values
    dictionaries = {}
    for number, key in (
        (1, "drug_id"),
        (2, "package_number"),
        (3, "package_number"),
        (4, "package_number"),
    ):
        dictionaries[number] = {r[key]: r for r in tables[number]}
        if len(dictionaries[number]) != len(tables[number]):
            raise ResearchError("e4_native_ambiguous_key")
    units = {r["code"]: r for r in tables[15] if r["unit_type"] == "4"}
    prior = {r["package_number"]: r for r in records(previous)}
    raw, members = [], []
    for package, p in dictionaries[2].items():
        d, v, g = (
            dictionaries[1].get(p["drug_id"], {}),
            dictionaries[3].get(package, {}),
            dictionaries[4].get(package, {}),
        )
        old = prior.get(package)
        abc_valid = g.get("abc_code") in {"A", "B", "C"}
        group_valid = re.fullmatch(r"[0-9]{4}", g.get("group_code") or "") is not None
        created = p["creation_date"]
        valid = p["drug_id"] and created and created != date(1900, 1, 1) and created <= day
        continuation = bool(
            valid
            and old
            and old["canonical_identity"]
            and old["drug_id"] == p["drug_id"]
            and old["creation_date"] == created
            and (
                not old["exit_date"]
                or old["exit_date"] == date(1900, 1, 1)
                or old["exit_date"] > day
            )
        )
        # SQL json_array serializes dates as ISO strings, with no spaces.
        canonical = (
            old["canonical_identity"]
            if continuation and old
            else hashlib.sha256(
                json.dumps(
                    [package, p["drug_id"], str(created), "final", str(day)], separators=(",", ":")
                ).encode()
            ).hexdigest()
            if valid
            else None
        )
        row = {
            "period_start": day,
            "package_number": package,
            "canonical_identity": canonical,
            "drug_id": p["drug_id"],
            "aip_ore": v.get("aip_ore"),
            "price_state": "zero_value_unresolved"
            if v.get("aip_ore") == 0
            else "present"
            if v.get("aip_ore") is not None
            else "missing",
            "currency": "DKK",
            "tax_basis": "excluding_vat",
            "fee_basis": "excluding_consumer_dispensing_fees",
            "price_basis": "per_package",
            "price_role": "effective_price",
            "abc": g.get("abc_code") if abc_valid else None,
            "group_code": g.get("group_code") if group_valid else None,
            "classification_state": "missing_record"
            if not g
            else "observed"
            if abc_valid and group_valid
            else "invalid",
            **{k: d.get(k) for k in ("atc", "form_code", "strength", "strength_unit")},
            **{
                k: p.get(k)
                for k in (
                    "pack_size_token",
                    "pack_unit",
                    "dispensing_code",
                    "package_distributor",
                    "creation_date",
                    "exit_date",
                )
            },
            "provenance": json.dumps(
                {
                    "original": saved["original"],
                    "members": ["LMS01.TXT", "LMS02.TXT", "LMS03.TXT", "LMS04.TXT"],
                    "snapshot": str(snapshot.resolve()),
                    "package_number": package,
                },
                sort_keys=True,
            ),
        }
        if not canonical:
            row["price_state"] = "unresolved_identity"
        raw.append(row)
        if g:
            unit = units.get(p["pack_unit"], {})
            member = {
                **row,
                "group_pack_size_token": g["pack_size_token"],
                "native_join_complete": bool(d and v),
                **{
                    k: p.get(k)
                    for k in ("pack_description", "component_package", "component_count_token")
                },
                "unit_short_text": unit.get("short_text"),
                "unit_description": unit.get("description"),
            }
            quantity, state = e3_peers.scalar_quantity(member)
            member.update(quantity_token_value=quantity, quantity_state=state)
            members.append(member)
    return money_frame(raw).sort_values("package_number").reset_index(drop=True), members


def advance(
    raw: pd.DataFrame,
    previous_raw: pd.DataFrame,
    previous_features: pd.DataFrame,
    calendar: list[dict[str, Any]],
) -> pd.DataFrame:
    """Exact one-step sufficient-state recurrence; replay checked before future use."""
    index = {p["start"]: p["index"] for p in calendar}
    oldraw = {r["package_number"]: r for r in records(previous_raw)}
    oldfeatures = {r["package_number"]: r for r in records(previous_features)}
    result = []
    for r in records(raw):
        day = r["period_start"].isoformat()
        ix = index[day]
        p, f = oldraw.get(r["package_number"]), oldfeatures.get(r["package_number"])
        reason = exclusion_reason(r)
        continuing = bool(
            p
            and f
            and not reason
            and f["eligible"]
            and f["period_index"] == ix - 1
            and p["canonical_identity"] == r["canonical_identity"]
        )
        h = int(f["history_periods"]) + 1 if continuing and f else 0
        lag: Any = r["aip_ore"] - f["aip_ore"] if continuing and f else None
        frequency = {}
        for name, sign in (("rise", 1), ("fall", -1), ("flat", 0)):
            key = f"past_{name}_frequency"
            frequency[key] = (
                (round((f[key] or 0) * (h - 1)) + int((lag > 0) - (lag < 0) == sign)) / h
                if h and f
                else None
            )
        cats = {k: r.get(k) for k in CATEGORIES}
        cats["supply_state"] = "unavailable_join_unestablished"
        cats["group_state"] = (
            "observed_identity_only_population_unestablished"
            if r.get("group_code")
            else "unobserved"
        )
        if r["classification_state"] != "observed":
            cats["abc"] = None
            cats["group_state"] = "unobserved_or_ambiguous"
        result.append(
            {
                "row_key": row_key(r["package_number"], day),
                "package_number": r["package_number"],
                "period_index": ix,
                "period_start": r["period_start"],
                "identity_episode_id": f["identity_episode_id"]
                if continuing and f
                else fingerprint(
                    {
                        "policy": VERSION,
                        "package": r["package_number"],
                        "canonical_identity": r["canonical_identity"],
                        "start": day,
                    }
                ),
                "eligible": not reason,
                "exclusion_reason": reason,
                "aip_ore": r["aip_ore"],
                "lag1_delta_ore": lag,
                "lag2_delta_ore": f["lag1_delta_ore"] if continuing and f else None,
                "lag3_delta_ore": f["lag2_delta_ore"] if continuing and f else None,
                "history_periods": h,
                **frequency,
                "lifecycle_age_periods": ix - index[str(r["creation_date"])]
                if not reason and str(r["creation_date"]) in index
                else None,
                **cats,
                "provenance": r["provenance"],
            }
        )
    return money_frame(result)


def peers(
    raw: pd.DataFrame,
    features: pd.DataFrame,
    native_rows: list[dict[str, Any]],
    previous_raw: pd.DataFrame,
    previous_features: pd.DataFrame,
    previous_native: list[dict[str, Any]],
) -> pd.DataFrame:
    def joined(r: pd.DataFrame, f: pd.DataFrame) -> list[dict[str, Any]]:
        return records(
            r.merge(
                f[["package_number", "eligible", "row_key", "period_index"]],
                on="package_number",
                validate="one_to_one",
            )
        )

    old = joined(previous_raw, previous_features)
    old_equal, _ = group_features.period_features(old, previous_native, {})
    old_states = {r["package_number"]: r["peer_feature_state"] for r in old_equal}
    for r in old:
        r["peer_feature_state"] = old_states[r["package_number"]]
    _, old_broad = e3_peers.period_features(old, previous_native, {})
    current = joined(raw, features)
    equal, _ = group_features.period_features(
        current, native_rows, {r["package_number"]: r for r in old}
    )
    states = {r["package_number"]: r["peer_feature_state"] for r in equal}
    for r in current:
        r["peer_feature_state"] = states[r["package_number"]]
    broad, _ = e3_peers.period_features(current, native_rows, old_broad)
    out = features.copy()
    for values, names in ((equal, group_features.FEATURES), (broad, e3_peers.FEATURES)):
        out = out.merge(
            pd.DataFrame(values)[["row_key", *names]], on="row_key", validate="one_to_one"
        )
    out["origin_group"] = (
        raw.set_index("package_number").group_code.reindex(out.package_number).to_numpy()
    )
    out["origin_identity_token"] = (
        raw.set_index("package_number").drug_id.reindex(out.package_number).to_numpy()
    )
    return out


def assert_same(actual: pd.DataFrame, expected: pd.DataFrame, columns: list[str]) -> dict[str, Any]:
    a, e = (
        actual.set_index("package_number").sort_index(),
        expected.set_index("package_number").sort_index(),
    )
    if not a.index.equals(e.index):
        raise ResearchError("e4_replay_population_mismatch")
    failures = {}
    for column in columns:
        av, ev = a[column], e[column]
        if pd.api.types.is_numeric_dtype(av) and pd.api.types.is_numeric_dtype(ev):
            same = np.isclose(
                av.astype(float), ev.astype(float), rtol=1e-14, atol=1e-12, equal_nan=True
            )
        else:
            same = av.fillna("<null>").eq(ev.fillna("<null>"))
        if not np.all(same):
            failures[column] = int((~same).sum())
    if failures:
        raise ResearchError("e4_feature_replay_mismatch:" + json.dumps(failures))
    return {
        "rows": len(a),
        "fields": len(columns),
        "mismatches": 0,
        "numeric_tolerance": "rtol=1e-14 atol=1e-12; categories exact",
    }


def initialize(root: Path, training_current: Path, e3_root: Path) -> Path:
    output = root / "features"
    manifest = output / "manifest.json"
    if manifest.exists():
        for item in read(manifest)["artifacts"]:
            checked(item)
        return manifest
    dataset_path = checked(read(training_current)["dataset"])
    dataset = read(dataset_path)
    paths: dict[str, list[Path]] = {"features": [], "source": []}
    for f in dataset["files"]:
        if f["role"] in paths:
            paths[f["role"]].append(checked({**f, "path": str(dataset_path.parent / f["path"])}))
    calendar: Any = read(dataset_path.parent / "calendar.json")
    if isinstance(calendar, dict):
        calendar = calendar["periods"]
    e3p = read(e3_root / "protocol.json")
    e2root = Path(e3p["pins"]["e2_protocol"]["path"]).parent
    gm = read(e2root.parent / "groups-v1/manifest.json")
    gmfiles = {f["role"]: Path(f["path"]) for f in gm["files"]}

    def natives(day: str) -> list[dict[str, Any]]:
        n = period_read([gmfiles["native_members"]], day)
        q = period_read([e3_root.parent / "peers-v1/native-quantities.parquet"], day)
        return records(n.merge(q, on=["package_number", "period_start"], validate="one_to_one"))

    output.mkdir(parents=True, exist_ok=True)
    # Compact, pinned sufficient-state checkpoint: scan small R1 package row groups
    # once per role, then resume without rescanning successful work.
    slices = {}
    for role, files in paths.items():
        cache = output / ("prior-" + role + ".parquet")
        receipt = cache.with_suffix(".json")
        if receipt.exists():
            if read(receipt)["dataset"] != pin(dataset_path):
                raise ResearchError("e4_feature_checkpoint_source_changed")
            checked(read(receipt)["rows"])
        else:
            with duckdb.connect() as db:
                db.execute("SET threads=6")
                db.execute("SET memory_limit='4GB'")
                db.execute("SET enable_progress_bar=false")
                df = (
                    db.execute(
                        (
                            "SELECT * FROM read_parquet(?) WHERE period_start IN (DATE "
                            "'2026-09-28',DATE '2026-10-12') ORDER BY "
                            "package_number,period_start"
                        ),
                        [[str(p) for p in files]],
                    )
                    .fetch_arrow_table()
                    .to_pandas(date_as_object=True)
                )
            df.to_parquet(cache, index=False)
            frozen_json(receipt, {"dataset": pin(dataset_path), "rows": pin(cache)})
        df = pd.read_parquet(cache)
        df["period_start"] = pd.to_datetime(df.period_start).dt.date
        for col in ("creation_date", "exit_date"):
            if col in df:
                df[col] = pd.to_datetime(df[col]).dt.date
        slices[role] = [
            df.loc[df.period_start.eq(date.fromisoformat(day))].reset_index(drop=True)
            for day in ("2026-09-28", "2026-10-12")
        ]
    oldraw, currentraw = slices["source"]
    oldfeatures, currentfeatures = slices["features"]
    replay = advance(currentraw, oldraw, oldfeatures, calendar)
    base_proof = assert_same(
        replay,
        currentfeatures,
        [*FEATURES, *[k for k in KEYS if k != "package_number"], "eligible", "exclusion_reason"],
    )
    reconstructed, _ = native(root / "sources/2026-10-12/snapshot.json", oldraw)
    raw_proof = assert_same(
        reconstructed,
        currentraw,
        [
            c
            for c in currentraw.columns
            if c not in {"package_number", "provenance", "classification_state"}
        ],
    )
    current = peers(
        currentraw,
        currentfeatures,
        natives("2026-10-12"),
        oldraw,
        oldfeatures,
        natives("2026-09-28"),
    )
    expected = period_read([e3_root / "cohorts/evaluation_2026/features.parquet"], "2026-10-12")
    peer_proof = assert_same(
        current.loc[current.eligible], expected, [*group_features.FEATURES, *e3_peers.FEATURES]
    )
    current["target_period"] = "2026-10-26"
    current["advance_abc"] = None
    current["advance_state"] = "not_supplied"
    current["advance_source"] = "current_only"
    newraw, newnative = native(root / "sources/2026-10-26/snapshot.json", currentraw)
    snapshot = read(root / "sources/2026-10-26/snapshot.json")
    if calendar[-1]["start"] != "2026-10-12" or calendar[-1]["end_exclusive"] != "2026-10-26":
        raise ResearchError("e4_feature_calendar_gap")
    calendar = [
        *calendar,
        {
            "index": calendar[-1]["index"] + 1,
            "start": "2026-10-26",
            "end_exclusive": snapshot["context"]["period_end_exclusive"],
        },
    ]
    base = advance(newraw, currentraw, currentfeatures, calendar)
    future = peers(newraw, base, newnative, currentraw, currentfeatures, natives("2026-10-12"))
    future["target_period"] = "2026-11-09"
    future["advance_abc"] = None
    future["advance_state"] = "not_supplied"
    future["advance_source"] = "current_only"
    output.mkdir(parents=True, exist_ok=True)
    for name, frame in (
        ("2026-10-12", current),
        ("2026-10-26", future),
        ("source-2026-10-26", newraw),
    ):
        frame.to_parquet(output / (name + ".parquet"), index=False)
    frozen_json(
        manifest,
        {
            "version": "e4-causal-period-extension-v1",
            "created_utc": utc(),
            "base_replay": base_proof,
            "native_attribute_replay": raw_proof,
            "peer_replay": peer_proof,
            "training": pin(dataset_path),
            "source_snapshots": [
                pin(root / f"sources/{day}/snapshot.json") for day in ("2026-10-12", "2026-10-26")
            ],
            "producer": pin(Path(__file__)),
            "artifacts": [
                pin(output / (name + ".parquet"))
                for name in ("2026-10-12", "2026-10-26", "source-2026-10-26")
            ],
            "future_information": (
                "No next-period prices, future membership, survival or attributes read; "
                "current-only advance state"
            ),
        },
    )
    return manifest
