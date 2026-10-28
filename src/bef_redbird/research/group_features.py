"""Conservative, period-complete LMS04 peer AIP research sidecar.

The accepted training bundle is never rewritten. Groups are assembled across
all its package partitions. AIP comparisons are exact equal-native-size/unit
package comparisons, and do not reconstruct the dispensing-price ABC rules.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import time
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .dataset import ResearchError, digest, read_json
from .training_contract import atomic_json

VERSION = "lms04-equal-native-size-peers-v1"


def pin(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": digest(path), "size_bytes": path.stat().st_size}


NUMERICS = (
    "peer_count",
    "peer_min_aip_ore",
    "own_minus_peer_min_aip_ore",
    "peer_median_aip_twice_ore",
    "peer_spread_aip_ore",
    "peer_distributor_count",
    "peer_lag1_observed_count",
    "peer_lag1_rise_share",
    "peer_lag1_fall_share",
)
CATEGORIES = ("peer_feature_state",)
FEATURES = (*NUMERICS, *CATEGORIES)
SCHEMA = pa.schema(
    [
        pa.field("row_key", pa.string(), False),
        pa.field("package_number", pa.string(), False),
        pa.field("period_start", pa.date32(), False),
        *[
            pa.field(name, pa.float64() if name.endswith("share") else pa.int64())
            for name in NUMERICS
        ],
        pa.field("peer_feature_state", pa.string(), False),
        pa.field("group_code", pa.string()),
        pa.field("source_group_member_count", pa.int32()),
        pa.field("comparable_stratum_member_count", pa.int32()),
    ]
)
NATIVE_SCHEMA = pa.schema(
    [
        pa.field("period_start", pa.date32(), False),
        *[
            pa.field(k, pa.string())
            for k in (
                "package_number",
                "group_code",
                "group_pack_size_token",
                "pack_size_token",
                "pack_unit",
                "package_distributor",
                "drug_id",
            )
        ],
        pa.field("aip_ore", pa.int64()),
        pa.field("native_join_complete", pa.bool_(), False),
    ]
)


def contract() -> dict[str, Any]:
    return {
        "version": VERSION,
        "feature_names": list(FEATURES),
        "numeric_names": list(NUMERICS),
        "categorical_names": list(CATEGORIES),
        "grain": "one row_key for every frozen training source/feature row",
        "group_key": "period_start + ordinary LMS04 group_code; never code alone",
        "stratum": (
            "exact nonempty native LMS02 pack_size_token and pack_unit; no numeric conversion"
        ),
        "population": "all complete selected native LMS04 records, before package partition joins",
        "incomplete": (
            "all peer predictors null for uncertain groups, incomplete native/source "
            "identity or price joins, unsupported group/member evidence, or ineligible "
            "own observations"
        ),
        "focal": (
            "focal package excluded from every peer numerator, denominator, price "
            "statistic and distributor count"
        ),
        "money": (
            "integer DKK ore per package; AIP excludes VAT and dispensing fees; same "
            "native size/unit only"
        ),
        "median": (
            "peer_median_aip_twice_ore is twice the median in ore, preserving half-ore "
            "values exactly"
        ),
        "lag1": (
            "current comparable peers' current minus previous AIP; consecutive eligible "
            "period indices, identical canonical identity, group code, native size and "
            "unit required; only observed peers enter shares' explicit count "
            "denominator"
        ),
        "distributor": (
            "distinct native LMS02 package_distributor among peers; null if any peer "
            "distributor is missing"
        ),
        "singleton": "peer_count=0; no peer prices; observed lag count=0 and shares null",
        "classification": (
            "observed ABC retained by base bundle only; no ABC is inferred from AIP; "
            "current official guidance uses dispensing price per unit"
        ),
        "namespaces": (
            "LMS01 drug substitution, LMS05 nonfixed-price substitution, LMS07 "
            "reimbursement and LMS24 dose context excluded"
        ),
        "temporal": (
            "same-period attributes/membership plus causal histories only; "
            "reconstructed research, historical possession unestablished"
        ),
        "supply": "unavailable, no supplier-disruption feature or absence inference",
    }


def period_features(
    rows: list[dict[str, Any]],
    native: list[dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    uncertain: set[str] | None = None,
    *,
    complete_snapshot: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """One complete period population, independent of partition assignment."""
    uncertain = uncertain or set()
    source = {r["package_number"]: r for r in rows}
    if len(source) != len(rows):
        raise ResearchError("duplicate_group_source_key")
    membership: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    bad: set[str] = set(uncertain)
    for member in native:
        package, group = member["package_number"], member["group_code"]
        if not group:
            continue
        if package in membership:
            bad.add(group)
            bad.add(membership[package]["group_code"])
        membership[package] = member
        groups[group].append(member)
        own = source.get(package)
        if (
            not member["native_join_complete"]
            or own is None
            or not own["eligible"]
            or not own.get("canonical_identity")
            or any(not member.get(k) for k in ("pack_size_token", "pack_unit"))
            or (
                own is not None
                and any(
                    own.get(k) != member.get(k)
                    for k in ("group_code", "pack_size_token", "pack_unit", "drug_id", "aip_ore")
                )
            )
            or member["group_pack_size_token"] != member["pack_size_token"]
        ):
            bad.add(group)
    # A group observed in the bundle but absent/different in its complete native
    # population cannot silently become a smaller apparently complete group.
    for own in rows:
        group = own.get("group_code")
        if group and (
            own["package_number"] not in membership
            or membership[own["package_number"]]["group_code"] != group
        ):
            bad.add(group)
    strata: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for own in rows:
        if own.get("group_code") in groups and own["group_code"] not in bad:
            strata[(own["group_code"], own["pack_size_token"], own["pack_unit"])].append(own)
    summaries = {}
    for key, members in strata.items():
        amounts = sorted(m["aip_ore"] for m in members)
        distributors = Counter(m.get("package_distributor") for m in members)
        directions: dict[str, int] = {}
        for member in members:
            prior = previous.get(member["package_number"])
            if (
                prior
                and prior["eligible"]
                and (
                    prior["period_index"] + 1 == member["period_index"]
                    and all(
                        prior.get(k) == member.get(k)
                        for k in (
                            "canonical_identity",
                            "group_code",
                            "pack_size_token",
                            "pack_unit",
                        )
                    )
                )
            ):
                delta = member["aip_ore"] - prior["aip_ore"]
                directions[member["package_number"]] = (delta > 0) - (delta < 0)
        summaries[key] = (amounts, distributors, directions, Counter(directions.values()))
    result = []
    for own in rows:
        package, group = own["package_number"], own.get("group_code")
        output = {
            "row_key": own["row_key"],
            "package_number": package,
            "period_start": own["period_start"],
            "group_code": group,
            "source_group_member_count": len(groups[group]) if group in groups else None,
            "comparable_stratum_member_count": None,
            **dict.fromkeys(NUMERICS),
            "peer_feature_state": "unobserved_group",
        }
        if not own["eligible"]:
            output["peer_feature_state"] = "own_ineligible"
        elif not complete_snapshot:
            output["peer_feature_state"] = "source_population_unestablished"
        elif group in bad:
            output["peer_feature_state"] = "incomplete_or_uncertain_group"
        elif group in groups:
            key = (group, own["pack_size_token"], own["pack_unit"])
            amounts, distributors, directions, counts = summaries[key]
            peers = amounts.copy()
            peers.pop(bisect.bisect_left(peers, own["aip_ore"]))
            count = len(peers)
            own_direction = directions.get(package)
            observed = len(directions) - (own_direction is not None)
            peer_dist = distributors.copy()
            peer_dist[own.get("package_distributor")] -= 1
            output.update(
                peer_count=count,
                comparable_stratum_member_count=count + 1,
                peer_distributor_count=(
                    sum(v > 0 for v in peer_dist.values())
                    if peer_dist.get(None, 0) == 0 and peer_dist.get("", 0) == 0
                    else None
                ),
                peer_lag1_observed_count=observed,
                peer_lag1_rise_share=(counts[1] - (own_direction == 1)) / observed
                if observed
                else None,
                peer_lag1_fall_share=(counts[-1] - (own_direction == -1)) / observed
                if observed
                else None,
                peer_feature_state="complete_equal_size_peers"
                if count
                else "complete_equal_size_singleton",
            )
            if count:
                output.update(
                    peer_min_aip_ore=peers[0],
                    own_minus_peer_min_aip_ore=own["aip_ore"] - peers[0],
                    peer_median_aip_twice_ore=peers[(count - 1) // 2] + peers[count // 2],
                    peer_spread_aip_ore=peers[-1] - peers[0],
                )
        result.append(output)
    return result, source


def _native_lines(content: bytes, width: int) -> list[bytes]:
    # Framing permits an exact final unterminated record (known LMS02 variant).
    lines = content.split(b"\r\n")
    if lines and not lines[-1]:
        lines.pop()
    if any(len(line) != width for line in lines):
        raise ResearchError("group_native_width_or_framing")
    return lines


def _token(content: bytes) -> str | None:
    token = content.strip()
    if not token:
        return None
    if token.decode("cp850") != token.decode("cp865"):
        raise ResearchError("group_native_ambiguous_encoding")
    return token.decode("cp850")


def _money(content: bytes) -> int | None:
    return int(content) if content and content.isdigit() else None


def reconcile_native(
    connection: Any, dataset_id: str, archive_root: Path, output: Path
) -> dict[str, Any]:
    """Independent byte transcription; never calls a production registry parser.

    Reopen every selected LMS04 original, reconcile every ordered persisted
    membership row, then independently join its native LMS02/03 own values.
    """
    sql = """SELECT s.period_start,s.member_id,s.import_id::VARCHAR,s.complete_snapshot,
      s.supported_fields,d.archive_location,d.content_hash,rm.native_name,
      m.raw_sha256,m.row_count,p.import_id::VARCHAR,v.import_id::VARCHAR,dp.snapshot_date
      FROM registry_dataset_member s JOIN registry_release r ON r.import_id=s.import_id
      JOIN source_delivery d USING(delivery_id)
      JOIN registry_release_member rm ON rm.import_id=s.import_id AND rm.member_number=4
      JOIN registry_member m ON m.member_id=s.member_id
      JOIN registry_dataset_period dp ON dp.dataset_id=s.dataset_id
        AND dp.period_start=s.period_start AND dp.phase=s.phase
      JOIN registry_dataset_member p ON p.dataset_id=s.dataset_id AND p.period_start=s.period_start
        AND p.phase=s.phase AND p.member_number=2
      JOIN registry_dataset_member v ON v.dataset_id=s.dataset_id AND v.period_start=s.period_start
        AND v.phase=s.phase AND v.member_number=3
      WHERE s.dataset_id=? AND s.phase='final' AND s.member_number=4
        AND s.state='selected' ORDER BY s.period_start"""
    scopes = connection.execute(sql, [dataset_id]).fetchall()
    receipts, total = [], 0
    required = {"package_number", "group_code", "pack_size_token"}
    writer = pq.ParquetWriter(output / "native-members.parquet", NATIVE_SCHEMA, compression="zstd")
    try:
        for (
            period,
            mid,
            iid,
            complete,
            supported,
            relative,
            original_hash,
            name,
            raw_hash,
            expected_count,
            pi,
            vi,
            snapshot_date,
        ) in scopes:
            if not complete or not required.issubset(supported) or pi != iid or vi != iid:
                raise ResearchError("group_native_population_contract")
            original = archive_root / relative
            if digest(original) != original_hash:
                raise ResearchError("group_native_original_changed")
            with zipfile.ZipFile(original) as archive:
                names = {n.lower(): n for n in archive.namelist()}
                content = archive.read(names[name.lower()])
                package_lines = _native_lines(archive.read(names["lms02.txt"]), 137)
                price_lines = _native_lines(archive.read(names["lms03.txt"]), 96)
                system = archive.read(names["system.txt"]).split(b"\r\n")
            if hashlib.sha256(content).hexdigest() != raw_hash:
                raise ResearchError("group_native_member_changed")
            lines = _native_lines(content, 47)
            # Source-native control date and catalogue counts independently
            # establish this complete population, not the stored row_count.
            control = system[0]
            if (
                len(control) != 93
                or control[:2] != b"00"
                or control[75:87].strip().lower() != b"lms.zip"
                or control[47:55].decode("ascii") != snapshot_date.strftime("%Y%m%d")
            ):
                raise ResearchError("group_native_control_context")
            catalogue = {}
            for record in system[2:]:
                if not record:
                    continue
                if len(record) != 51 or record[:2] != b"10":
                    raise ResearchError("group_native_system_catalogue")
                key = record[32:44].strip().lower().decode("ascii")
                if key in catalogue or not record[46:51].isdigit():
                    raise ResearchError("group_native_system_catalogue")
                catalogue[key] = int(record[46:51])
            if any(
                catalogue.get(key) != count
                for key, count in (
                    ("lms02.txt", len(package_lines)),
                    ("lms03.txt", len(price_lines)),
                    ("lms04.txt", len(lines)),
                )
            ):
                raise ResearchError("group_native_population_count")
            # Independent literal boundaries from retained official Nyt!77-86.
            expected = [
                (
                    _token(line[:4]),
                    _token(line[4:10]),
                    _token(line[10:18]),
                    _token(line[34:35]),
                    _token(line[35:41]),
                )
                for line in lines
            ]
            row_ids = connection.execute(
                "SELECT row_ids FROM registry_member WHERE member_id=?", [mid]
            ).fetchone()[0]
            persisted = dict(
                (row[0], row[1:])
                for row in connection.execute(
                    """SELECT row_id,group_code,package_number,pack_size_token,
                      abc_code,cheapest_package FROM registry_lms04
                      WHERE row_id IN (SELECT unnest(row_ids) FROM registry_member
                        WHERE member_id=?)""",
                    [mid],
                ).fetchall()
            )
            actual = [persisted[row_id] for row_id in row_ids]
            if len(lines) != expected_count or expected != actual:
                raise ResearchError("group_native_ordered_reconciliation_failed")
            if len({e[1] for e in expected}) != len(expected):
                raise ResearchError("group_native_duplicate_package")
            packages = {_token(line[11:17]): line for line in package_lines}
            prices = {_token(line[:6]): line for line in price_lines}
            if len(packages) != len(package_lines) or len(prices) != len(price_lines):
                raise ResearchError("group_native_duplicate_identity_or_price")
            records = []
            missing = 0
            for group, package, size, _, _ in expected:
                p, v = packages.get(package), prices.get(package)
                good = p is not None and v is not None
                missing += not good
                records.append(
                    {
                        "period_start": period,
                        "package_number": package,
                        "group_code": group,
                        "group_pack_size_token": size,
                        "pack_size_token": _token(p[59:67]) if p else None,
                        "pack_unit": _token(p[67:69]) if p else None,
                        "package_distributor": _token(p[131:137]) if p else None,
                        "drug_id": _token(p[:11]) if p else None,
                        "aip_ore": _money(v[6:15]) if v else None,
                        "native_join_complete": good,
                    }
                )
            writer.write_table(pa.Table.from_pylist(records, schema=NATIVE_SCHEMA))
            total += len(lines)
            receipts.append(
                {
                    "period": period.isoformat(),
                    "import_id": iid,
                    "member_id": mid,
                    "original_sha256": original_hash,
                    "member_sha256": raw_hash,
                    "records": len(lines),
                    "native_system_counts_verified": True,
                    "native_join_missing": missing,
                }
            )
    finally:
        writer.close()
    report = {
        "version": VERSION,
        "scope": (
            "all selected final LMS04 snapshots; ordered "
            "group/package/size/ABC/cheapest assertions checked against original bytes; "
            "native LMS02/03 joined independently"
        ),
        "periods": len(scopes),
        "rows": total,
        "mismatches": 0,
        "receipts": receipts,
    }
    atomic_json(output / "native-reconciliation.json", report)
    return report


def _records(cursor: Any) -> Iterator[dict[str, Any]]:
    names = [col[0] for col in cursor.description]
    while records := cursor.fetchmany(16384):
        for record in records:
            yield dict(zip(names, record, strict=True))


def build_group_features(bundle_manifest: Path, operating_config: Path, output_root: Path) -> Path:
    """Build and reconcile the full frozen sidecar. Canonical DB is read-only."""
    from bef_redbird.foundation.archive import external_path

    started = time.monotonic()
    output = external_path(output_root)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise ResearchError("group_sidecar_already_exists")
    bundle = read_json(bundle_manifest)
    config = read_json(operating_config)
    binding = bundle["binding"]
    paths: dict[str, list[str]] = defaultdict(list)
    pins = []
    for item in bundle["files"]:
        if item["role"] in {"source", "features", "exclusions"}:
            path = bundle_manifest.parent / item["path"]
            if digest(path) != item["sha256"]:
                raise ResearchError("group_base_bundle_changed")
            paths[item["role"]].append(str(path))
            pins.append(pin(path))
    exclusions = read_json(Path(paths["exclusions"][0]))
    uncertain: dict[date, set[str]] = defaultdict(set)
    for period, group in exclusions["uncertain_groups"]:
        uncertain[date.fromisoformat(period)].add(group)
    connection = duckdb.connect(config["database"], read_only=True)
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='4GB'")
    native = reconcile_native(
        connection, binding["dataset_id"], Path(config["archive_root"]), output
    )
    connection.close()
    # Global input join precedes period grouping. No partition-local denominator.
    analytical = duckdb.connect()
    analytical.execute("SET threads=2")
    analytical.execute("SET memory_limit='4GB'")
    analytical.execute("SET temp_directory=?", [str(output / "spill")])
    analytical.execute("SET max_temp_directory_size='4GiB'")
    analytical.execute(
        "CREATE TEMP TABLE source AS SELECT f.row_key,f.eligible,f.period_index,"
        "s.period_start,s.package_number,s.canonical_identity,s.drug_id,s.group_code,"
        "s.pack_size_token,s.pack_unit,s.package_distributor,s.aip_ore "
        "FROM read_parquet(?) s JOIN read_parquet(?) f USING(package_number,period_start) "
        "ORDER BY period_start,package_number",
        [paths["source"], paths["features"]],
    )
    analytical.execute(
        "CREATE TEMP TABLE native AS SELECT * FROM read_parquet(?) "
        "ORDER BY period_start,package_number",
        [str(output / "native-members.parquet")],
    )
    mismatch_row = analytical.execute("""SELECT count(*)
      FROM native n JOIN source s USING(period_start,package_number)
      WHERE n.group_code IS DISTINCT FROM s.group_code
        OR n.pack_size_token IS DISTINCT FROM s.pack_size_token
        OR n.pack_unit IS DISTINCT FROM s.pack_unit
        OR n.drug_id IS DISTINCT FROM s.drug_id
        OR n.aip_ore IS DISTINCT FROM s.aip_ore
        OR n.package_distributor IS DISTINCT FROM s.package_distributor""").fetchone()
    assert mismatch_row is not None
    mismatch = mismatch_row[0]
    if mismatch:
        raise ResearchError("group_native_to_frozen_source_mismatch")
    periods = analytical.execute("SELECT DISTINCT period_start FROM source ORDER BY 1").fetchall()
    previous: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    coverage = []
    target = output / "group-features.parquet"
    writer = pq.ParquetWriter(target, SCHEMA, compression="zstd")
    try:
        for (period,) in periods:
            rows = list(
                _records(
                    analytical.execute(
                        "SELECT * FROM source WHERE period_start=? ORDER BY package_number",
                        [period],
                    )
                )
            )
            members = list(
                _records(
                    analytical.execute(
                        "SELECT * FROM native WHERE period_start=? ORDER BY package_number",
                        [period],
                    )
                )
            )
            features, previous = period_features(
                rows, members, previous, uncertain[period], complete_snapshot=bool(members)
            )
            writer.write_table(pa.Table.from_pylist(features, schema=SCHEMA))
            state_counts = Counter(r["peer_feature_state"] for r in features)
            counts.update(state_counts)
            coverage.append(
                {
                    "period": period.isoformat(),
                    "source_rows": len(rows),
                    "native_group_member_rows": len(members),
                    "source_groups": len({r["group_code"] for r in members}),
                    "states": dict(state_counts),
                }
            )
    finally:
        writer.close()
    verification = analytical.execute(
        """SELECT count(*),count(distinct g.row_key),
      count(*) FILTER(WHERE s.row_key IS NULL) FROM read_parquet(?) g
      FULL JOIN source s USING(row_key)""",
        [str(target)],
    ).fetchone()
    if verification != (bundle["rows"], bundle["rows"], 0):
        raise ResearchError("group_full_row_accounting_failed")
    atomic_json(output / "contract.json", contract())
    atomic_json(
        output / "coverage.json",
        {
            "rows": bundle["rows"],
            "periods": coverage,
            "state_counts": dict(counts),
            "native_to_bundle_mismatches": mismatch,
        },
    )
    result = {
        "contract": VERSION,
        "status": "complete_native_reconciled",
        "rows": bundle["rows"],
        "bundle": pin(bundle_manifest),
        "base_inputs": pins,
        "dataset_id": binding["dataset_id"],
        "feature_names": list(FEATURES),
        "numeric_names": list(NUMERICS),
        "categorical_names": list(CATEGORIES),
        "files": [
            dict(pin(path), role=role)
            for path, role in (
                (target, "features"),
                (output / "native-members.parquet", "native_members"),
                (output / "native-reconciliation.json", "native_reconciliation"),
                (output / "coverage.json", "coverage"),
                (output / "contract.json", "contract"),
            )
        ],
        "native_periods": native["periods"],
        "native_rows": native["rows"],
        "native_mismatches": mismatch,
        "state_counts": dict(counts),
        "producer_sha256": digest(Path(__file__)),
        "elapsed_seconds": time.monotonic() - started,
    }
    atomic_json(output / "manifest.json", result)
    analytical.close()
    return output / "manifest.json"


def load_group_features(manifest: Path) -> dict[str, Any]:
    document = read_json(manifest)
    if (
        document["contract"] != VERSION
        or document.get("status") != "complete_native_reconciled"
        or document["feature_names"] != list(FEATURES)
        or document.get("numeric_names") != list(NUMERICS)
        or document.get("categorical_names") != list(CATEGORIES)
        or Counter(item["role"] for item in document["files"])
        != Counter(
            {
                "features": 1,
                "native_members": 1,
                "native_reconciliation": 1,
                "coverage": 1,
                "contract": 1,
            }
        )
    ):
        raise ResearchError("unsupported_group_feature_contract")
    for item in [document["bundle"], *document["base_inputs"], *document["files"]]:
        if digest(Path(item["path"])) != item["sha256"]:
            raise ResearchError("group_feature_pin_changed")
    dictionary = next(item for item in document["files"] if item["role"] == "contract")
    if read_json(Path(dictionary["path"])) != contract():
        raise ResearchError("group_feature_dictionary_changed")
    feature_file = next(item for item in document["files"] if item["role"] == "features")
    parquet = pq.ParquetFile(feature_file["path"])
    if parquet.schema_arrow != SCHEMA or parquet.metadata.num_rows != document["rows"]:
        raise ResearchError("group_feature_schema_or_count_changed")
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--operating-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(build_group_features(args.bundle, args.operating_config, args.output_root))


if __name__ == "__main__":
    main()
