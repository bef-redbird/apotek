"""E3's sole future feature: separately identified next-tariff ABC proxy.

Native classification extraction does not read prices or labels. Origin candidates
are a left-hand invariant. Later native identity is used only to reject ambiguous
classification attribution, never as a predictor or an eligibility filter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .dataset import ResearchError, digest
from .e2_data import ResearchData, verify_pin, verify_training_pointer
from .group_features import _native_lines, _token, pin
from .training_contract import atomic_json

VERSION = "advance-abc-historical-proxy-e3-v1"
# Detailed rejection states are audit metadata, not predictive inputs: an
# identity-rejection reason would otherwise reveal future identity information.
ADVANCE_FEATURES = ["advance_abc"]
SCENARIOS = {
    "perfect": ("none", 0.0),
    "missing10": ("missing", 0.10),
    "missing25": ("missing", 0.25),
    "wrong10": ("wrong", 0.10),
    "wrong25": ("wrong", 0.25),
    "group_wrong20": ("group_wrong", 0.20),
    "missing100": ("missing", 1.0),
}


def join_advance(
    candidates: pd.DataFrame, classifications: pd.DataFrame, calendar: list[dict[str, Any]]
) -> pd.DataFrame:
    """Only allowlisted classification fields can cross the next-period boundary."""
    target = {pd.Timestamp(x["start"]): pd.Timestamp(x["end_exclusive"]) for x in calendar}
    out = candidates.copy().reset_index(drop=True)
    out["target_period"] = out.period_start.map(target)
    if out.target_period.isna().any() or out.row_key.duplicated().any():
        raise ResearchError("e3_invalid_origin_calendar_or_identity")
    columns = ["period_start", "package_number", "code", "state", "identity_token", "source"]
    future = classifications[columns].copy().rename(columns={"period_start": "target_period"})
    if future.duplicated(["target_period", "package_number"]).any():
        raise ResearchError("e3_unresolved_classification_duplicates")
    out = out.merge(
        future,
        on=["target_period", "package_number"],
        how="left",
        validate="many_to_one",
        sort=False,
    )
    if len(out) != len(candidates) or out.row_key.tolist() != candidates.row_key.tolist():
        raise ResearchError("e3_advance_join_changed_candidates")
    out["advance_state"] = out.state.fillna("missing_final_classification")
    ambiguous = out.code.notna() & (
        out.identity_token.isna()
        | out.origin_identity_token.isna()
        | out.identity_token.ne(out.origin_identity_token)
    )
    out.loc[ambiguous, "advance_state"] = "ambiguous_identity"
    out["advance_abc"] = out.code.where(
        out.advance_state.eq("observed") & out.code.isin(["A", "B", "C"])
    )
    out["advance_source"] = out.source.fillna("no_recorded_final_code")
    return out.drop(columns=["code", "state", "identity_token", "source", "origin_identity_token"])


def perturb(frame: pd.DataFrame, scenario: str, seed: int = 20261026) -> pd.DataFrame:
    """Prediction-only, common deterministic errors; never touch current ABC/labels."""
    kind, rate = SCENARIOS[scenario]
    out = frame.copy()
    if kind == "none":
        return out
    if kind == "group_wrong":
        keys = out.period_start.astype(str) + ":" + out.origin_group.fillna(out.row_key)
    else:
        keys = out.row_key
    u = np.array(
        [
            int.from_bytes(hashlib.sha256(f"{seed}:{k}".encode()).digest()[:8], "big") / 2**64
            for k in keys
        ]
    )
    mask = (u < rate) & out.advance_abc.isin(["A", "B", "C"]).to_numpy()
    if kind == "missing":
        out.loc[mask, "advance_abc"] = None
        # Missing report codes use the same missing signal as absent final codes.
        out.loc[mask, "advance_state"] = "missing_final_classification"
    else:
        out.loc[mask, "advance_abc"] = out.loc[mask, "advance_abc"].map(
            {"A": "B", "B": "C", "C": "A"}
        )
    return out


def extract(pointer: Path, group_manifest: Path, config_path: Path, output: Path) -> Path:
    if (output / "manifest.json").exists():
        raise ResearchError("e3_advance_artifact_exists")
    output.mkdir(parents=True, exist_ok=True)
    bundle_path, bundle = verify_training_pointer(pointer)
    group = json.loads(group_manifest.read_text())
    if group["bundle"]["sha256"] != digest(bundle_path):
        raise ResearchError("e3_group_bundle_changed")
    native_pin = next(x for x in group["files"] if x["role"] == "native_reconciliation")
    verify_pin(Path(native_pin["path"]), native_pin)
    accepted = json.loads(Path(native_pin["path"]).read_text())
    accepted_by_period = {x["period"]: x for x in accepted["receipts"]}
    config = json.loads(config_path.read_text())
    db = duckdb.connect(config["database"], read_only=True)
    db.execute("SET threads=2")
    scopes = db.execute(
        """SELECT s.period_start,d.archive_location,d.content_hash,
        s.member_id,s.supported_fields FROM registry_dataset_member s
        JOIN registry_release r ON r.import_id=s.import_id
        JOIN source_delivery d USING(delivery_id)
        WHERE s.dataset_id=? AND s.phase='final' AND s.member_number=4
        AND s.state='selected' AND s.period_start>=DATE '2019-01-01' ORDER BY 1""",
        [bundle["binding"]["dataset_id"]],
    ).fetchall()
    rows, receipts = [], []
    for day, relative, sha, mid, supported in scopes:
        prior = accepted_by_period[str(day)]
        if prior["member_id"] != mid or prior["original_sha256"] != sha:
            raise ResearchError("e3_classification_selection_changed")
        original = Path(config["archive_root"]) / relative
        if digest(original) != sha:
            raise ResearchError("e3_classification_original_changed")
        with zipfile.ZipFile(original) as z:
            names = {n.lower(): n for n in z.namelist()}
            content = z.read(names["lms04.txt"])
            lines = _native_lines(content, 47)
            identity: dict[str | None, str | None] = {}
            seen_identity: set[str | None] = set()
            for record in _native_lines(z.read(names["lms02.txt"]), 137):
                package = _token(record[11:17])
                identity[package] = None if package in seen_identity else _token(record[:11])
                seen_identity.add(package)
        if (
            hashlib.sha256(content).hexdigest() != prior["member_sha256"]
            or len(lines) != prior["records"]
        ):
            raise ResearchError("e3_native_classification_pin_changed")
        direct = [(_token(x[4:10]), _token(x[34:35])) for x in lines]
        # Independent persisted-value readback; no production adapter called.
        stored = db.execute(
            """SELECT package_number,abc_code FROM registry_lms04
            WHERE row_id IN (SELECT unnest(row_ids) FROM registry_member WHERE member_id=?)""",
            [mid],
        ).fetchall()
        if sorted(direct) != sorted(stored):
            raise ResearchError("e3_independent_abc_reconciliation_failed")
        counts = pd.Series([p for p, _ in direct]).value_counts()
        for package, code in direct:
            assert isinstance(package, str)
            if counts[package] != 1:
                state = "ambiguous"
            elif "abc_code" not in supported:
                state = "unsupported_field"
            elif code is None:
                state = "missing_code"
            elif code not in {"A", "B", "C"}:
                state = "unsupported_code"
            else:
                state = "observed"
            rows.append(
                {
                    "period_start": pd.Timestamp(day),
                    "package_number": package,
                    "code": code,
                    "state": state,
                    "identity_token": identity.get(package),
                    "source": f"retained_final_lms04:{sha}",
                }
            )
        receipts.append(
            {
                "period": str(day),
                "original_sha256": sha,
                "member_sha256": prior["member_sha256"],
                "classifications": len(lines),
                "mismatches": 0,
            }
        )
    db.close()
    classes = pd.DataFrame(rows).drop_duplicates(["period_start", "package_number"])
    exclusions_path = next(
        bundle_path.parent / x["path"] for x in bundle["files"] if x["role"] == "exclusions"
    )
    exclusions = json.loads(exclusions_path.read_text())
    # Reuse R1's affected classification exclusions, without propagating them to AIP.
    for item in exclusions["scopes"]:
        if item.get("field") == "abc_code":
            mask = classes.period_start.eq(pd.Timestamp(item["date"])) & classes.package_number.eq(
                item["package"]
            )
            classes.loc[mask, "state"] = "scoped_classification_exclusion"
    classes.to_parquet(output / "classifications.parquet", index=False)
    calendar_path = next(
        bundle_path.parent / x["path"] for x in bundle["files"] if x["role"] == "calendar"
    )
    calendar = json.loads(calendar_path.read_text())["periods"]
    source_paths = [
        str(bundle_path.parent / x["path"]) for x in bundle["files"] if x["role"] == "source"
    ]
    feature_paths = [
        str(bundle_path.parent / x["path"]) for x in bundle["files"] if x["role"] == "features"
    ]
    c = duckdb.connect()
    c.execute("SET threads=2")
    c.execute("SET memory_limit='4GB'")
    own = c.execute(
        """SELECT f.row_key,f.package_number,f.period_start,s.drug_id AS origin_identity_token,
        s.group_code AS origin_group FROM read_parquet(?) f JOIN read_parquet(?) s
        USING(package_number,period_start) WHERE f.period_start>=DATE '2019-01-01'
        ORDER BY f.period_start,f.row_key""",
        [feature_paths, source_paths],
    ).fetchdf()
    c.close()
    joined = join_advance(own, classes, calendar)
    joined.to_parquet(output / "advance.parquet", index=False)
    atomic_json(
        output / "reconciliation.json",
        {
            "version": VERSION,
            "rows": len(joined),
            "classification_rows": len(classes),
            "native_periods": receipts,
            "states": joined.advance_state.value_counts().to_dict(),
            "candidate_keys_unchanged": True,
            "future_price_or_label_reads": 0,
            "exclusion_ledger": pin(exclusions_path),
        },
    )
    atomic_json(
        output / "contract.json",
        {
            "version": VERSION,
            "features": ADVANCE_FEATURES,
            "scope": (
                "Origins 2019 onward; left join all final classifications independent of "
                "price usability or outcomes; actual R1 calendar successor only."
            ),
            "information": (
                "Current ABC unchanged; separately admitted immediate next ABC historical proxy. "
                "No T+2, future membership/price/size/status. Next LMS02 drug token is "
                "comparison-only identity rejection metadata, never predictor."
            ),
            "missingness_limit": (
                "Final-code absence may reveal source coverage or disappearance that an early "
                "report would not reveal. Conditional proxy results do not establish early "
                "availability or Cito accuracy; missing100 and other prediction-only "
                "perturbations disclose dependence."
            ),
            "scenarios": SCENARIOS,
            "perturbation": (
                "Prediction only; all training uses unperturbed conditional proxy. Common "
                "seeded row/group errors across methods, A->B->C->A; labels unchanged. "
                "Synthetic error rates are not measured report accuracy."
            ),
        },
    )
    atomic_json(
        output / "manifest.json",
        {
            "version": VERSION,
            "bundle": pin(bundle_path),
            "groups": pin(group_manifest),
            "config": pin(config_path),
            "rows": len(joined),
            "files": [
                {**pin(output / name), "role": role}
                for name, role in [
                    ("advance.parquet", "features"),
                    ("classifications.parquet", "classifications"),
                    ("contract.json", "contract"),
                    ("reconciliation.json", "reconciliation"),
                ]
            ],
        },
    )
    return output / "manifest.json"


class E3Data(ResearchData):
    def __init__(
        self, bundle: Path, groups: Path, advance: Path, broader: Path, *, scratch: Path
    ) -> None:
        super().__init__(bundle, groups, scratch=scratch)
        self.e3_manifests = []
        for name, manifest_path in [("advance", advance), ("broader", broader)]:
            manifest = json.loads(manifest_path.read_text())
            if manifest["bundle"]["sha256"] != digest(bundle):
                raise ResearchError("e3_sidecar_bundle_binding_changed")
            group_binding = manifest.get(
                "groups", manifest.get("equal_size_groups", manifest.get("e2_groups"))
            )
            if group_binding is None or group_binding["sha256"] != digest(groups):
                raise ResearchError("e3_sidecar_group_binding_changed")
            self.e3_manifests.append(manifest)
            for item in manifest["files"]:
                verify_pin(manifest_path.parent / item["path"], item)
            paths = [
                str(manifest_path.parent / x["path"])
                for x in manifest["files"]
                if x["role"] == "features"
            ]
            self._materialize(name, paths)
            duplicate = self.connection.execute(
                f"SELECT count(*)-count(DISTINCT row_key) FROM {name}"
            ).fetchone()
            if duplicate is None or duplicate[0]:
                raise ResearchError("e3_duplicate_sidecar_keys")
            clause = " WHERE period_start>=DATE '2019-01-01'" if name == "advance" else ""
            mismatch = self.connection.execute(
                f"SELECT count(*) FROM (SELECT * FROM features{clause}) f "
                f"FULL JOIN {name} s USING(row_key) WHERE f.row_key IS NULL OR "
                "s.row_key IS NULL OR f.package_number IS DISTINCT FROM s.package_number OR "
                "f.period_start IS DISTINCT FROM s.period_start"
            ).fetchone()
            if mismatch is None or mismatch[0]:
                raise ResearchError("e3_sidecar_population_or_identity_changed")
        broad_names = self.e3_manifests[1]["feature_names"]
        self.broad_features = broad_names
        self.broad_categories = self.e3_manifests[1]["categorical_names"]
        self.connection.execute("ALTER VIEW predictors RENAME TO e2_predictors")
        names = ",".join('b."' + x + '"' for x in broad_names)
        self.connection.execute(
            "CREATE VIEW predictors AS SELECT p.*,a.target_period,a.advance_abc,a.advance_state,"
            "a.advance_source,a.origin_group,"
            + names
            + " FROM e2_predictors p LEFT JOIN advance a USING(row_key) "
            "LEFT JOIN broader b ON p.row_key=b.row_key"
        )
        bad = self.connection.execute(
            "SELECT (SELECT count(*) FROM predictors)-(SELECT count(*) FROM features)"
        ).fetchone()
        if bad is None or bad[0]:
            raise ResearchError("e3_feature_join_changed_population")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pointer", type=Path, required=True)
    parser.add_argument("--groups", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(extract(args.pointer, args.groups, args.config, args.output))
