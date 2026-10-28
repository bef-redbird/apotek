"""Origin-dated, explicitly supported different-size LMS04 AIP peer research.

This supplements, and never replaces, the accepted E2 equal-size sidecar. Native
LMS02 gross-size ratios are used only when each scalar description corroborates
its quantity and its own edition's LMS15 type-4 dictionary corroborates the unit.
No clinical, ABC, future-membership or cross-unit conversion is inferred.
"""

from __future__ import annotations

import argparse
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .dataset import ResearchError, digest, read_json
from .group_features import _native_lines, _records, load_group_features, pin
from .training_contract import atomic_json

VERSION = "lms04-supported-scalar-size-peers-e3-v1"
# Literal source dictionary assertions, not a general unit conversion table.
# (LMS15 type-4 short text, description; LMS02 scalar description spelling).
UNITS = {
    "ST": ("stk.", "stk.", "stk\\."),
    "ML": ("ml", "ml", "ml"),
    "G": ("g", "g", "g"),
    "DO": ("doser", "doser", "doser"),
}
NUMERICS = (
    "broad_peer_count",
    "broad_peer_different_size_count",
    "broad_peer_population_count",
    "broad_peer_unknown_quantity_count",
    "broad_peer_other_unit_count",
    "broad_peer_min_equivalent_aip_ore",
    "broad_peer_median_equivalent_aip_ore",
    "broad_peer_spread_equivalent_aip_ore",
    "broad_own_minus_peer_min_ore",
    "broad_peer_distributor_count",
    "broad_peer_lag1_observed_count",
    "broad_peer_lag1_rise_share",
    "broad_peer_lag1_fall_share",
)
CATEGORIES = ("broad_peer_feature_state",)
FEATURES = (*NUMERICS, *CATEGORIES)
_FLOATS = {name for name in NUMERICS if name.endswith("share") or "_ore" in name}
SCHEMA = pa.schema(
    [
        pa.field("row_key", pa.string(), False),
        pa.field("package_number", pa.string(), False),
        pa.field("period_start", pa.date32(), False),
        *[pa.field(n, pa.float64() if n in _FLOATS else pa.int64()) for n in NUMERICS],
        pa.field("broad_peer_feature_state", pa.string(), False),
        pa.field("own_quantity_state", pa.string(), False),
        # Exact minimum is retained separately from model's float input.
        pa.field("min_equivalent_aip_numerator", pa.int64()),
        pa.field("min_equivalent_aip_denominator", pa.int64()),
    ]
)
NATIVE_SCHEMA = pa.schema(
    [
        pa.field("period_start", pa.date32(), False),
        *[
            pa.field(n, pa.string())
            for n in (
                "package_number",
                "pack_description",
                "component_package",
                "component_count_token",
                "unit_short_text",
                "unit_description",
            )
        ],
        pa.field("quantity_token_value", pa.int64()),
        pa.field("quantity_state", pa.string(), False),
    ]
)


def contract() -> dict[str, Any]:
    return {
        "version": VERSION,
        "feature_names": list(FEATURES),
        "numeric_names": list(NUMERICS),
        "categorical_names": list(CATEGORIES),
        "population": "all selected period LMS04 members across all R1 package partitions",
        "group": "same exact dated ordinary LMS04 group; no ATC/drug-name inference",
        "quantity": (
            "positive eight-digit LMS02 gross numeric token corroborated per record by "
            "simple decimal LMS02 description: token=100*description quantity; same "
            "edition LMS15 type4 exact supported unit dictionary; optional ST '(blister)' only"
        ),
        "units": {k: list(v[:2]) for k, v in UNITS.items()},
        "exclusions": (
            "compound/unknown descriptions, multipack references/count>1, unknown or "
            "unconfirmed unit dictionaries and mismatched gross-size assertions remain unknown"
        ),
        "conversion": (
            "peer AIP integer ore * focal raw size / peer raw size, using exact Fraction "
            "within the same native unit only; no rounding before statistics; float64 "
            "model inputs plus exact minimum numerator/denominator; no ABC reconstruction"
        ),
        "subset": (
            "unsupported members are counted explicitly, never erased; supported peers "
            "are a declared same-unit subset, not a complete economic-substitute claim"
        ),
        "own_exclusion": "focal excluded from every peer statistic/count/lag denominator",
        "lag1": (
            "current supported peers only, consecutive earlier eligible identity, same "
            "dated nominal group code and unchanged raw quantity/unit; no future rows"
        ),
        "time": "origin-period attributes only; reconstructed research, possession unestablished",
        "accepted_e2": "equal-size fields retained separately without modification",
    }


def scalar_quantity(member: dict[str, Any]) -> tuple[int | None, str]:
    """Validate one source scalar, without interpreting a free-form multipack."""
    unit = member.get("pack_unit")
    if unit not in UNITS:
        return None, "unsupported_unit"
    short, description, spelling = UNITS[unit]
    if (member.get("unit_short_text"), member.get("unit_description")) != (short, description):
        return None, "unit_dictionary_unconfirmed"
    component = member.get("component_package") or ""
    count = member.get("component_count_token") or ""
    if component.strip("0 ") or (count and (not count.isdigit() or int(count) > 1)):
        return None, "complex_component_pack"
    token = member.get("pack_size_token") or ""
    if not re.fullmatch(r"[0-9]{8}", token) or int(token) <= 0:
        return None, "invalid_numeric_quantity"
    suffix = r"(?: \(blister\))?" if unit == "ST" else ""
    match = re.fullmatch(
        r"([0-9]+(?:,[0-9]{1,2})?) " + spelling + suffix,
        member.get("pack_description") or "",
    )
    if not match:
        return None, "complex_or_unknown_description"
    quantity = Fraction(match[1].replace(",", "."))
    # Scale is independently corroborated for every admitted record. All price
    # comparisons use a ratio, for which the common scale cancels exactly.
    if quantity <= 0 or quantity * 100 != int(token):
        return None, "description_numeric_mismatch"
    return int(token), "supported_scalar_quantity"


def period_features(
    rows: list[dict[str, Any]],
    native: list[dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Aggregate one whole origin population before any partition/fold filtering."""
    source = {r["package_number"]: r for r in rows}
    if len(source) != len(rows) or len({r["package_number"] for r in native}) != len(native):
        raise ResearchError("e3_peer_duplicate_population_key")
    if len({r["period_start"] for r in [*rows, *native]}) > 1:
        raise ResearchError("e3_peer_mixed_origin_periods")
    membership = {r["package_number"]: r for r in native}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    bad_groups: set[str] = set()
    quantities: dict[str, tuple[int | None, str]] = {}
    for member in native:
        package, group = member["package_number"], member.get("group_code")
        if not group:
            continue
        groups[group].append(member)
        own = source.get(package)
        if (
            not member.get("native_join_complete")
            or own is None
            or not own["eligible"]
            or not own.get("canonical_identity")
            or member.get("group_pack_size_token") != member.get("pack_size_token")
            or any(
                own.get(k) != member.get(k)
                for k in (
                    "group_code",
                    "pack_size_token",
                    "pack_unit",
                    "drug_id",
                    "aip_ore",
                    "package_distributor",
                )
            )
        ):
            bad_groups.add(group)
        quantities[package] = scalar_quantity(member)
    for own in rows:
        group = own.get("group_code")
        if group and (
            own["package_number"] not in membership
            or membership[own["package_number"]].get("group_code") != group
        ):
            bad_groups.add(group)
    result = []
    for own in rows:
        package, group = own["package_number"], own.get("group_code")
        quantity, quantity_state = quantities.get(package, (None, "unobserved_membership"))
        output = {
            **{k: own[k] for k in ("row_key", "package_number", "period_start")},
            **dict.fromkeys(NUMERICS),
            "broad_peer_feature_state": "unobserved_group",
            "own_quantity_state": quantity_state,
            "min_equivalent_aip_numerator": None,
            "min_equivalent_aip_denominator": None,
        }
        accepted_state = own.get("peer_feature_state")
        if not own["eligible"]:
            output["broad_peer_feature_state"] = "own_ineligible"
        elif group in bad_groups or accepted_state in {
            "incomplete_or_uncertain_group",
            "source_population_unestablished",
        }:
            output["broad_peer_feature_state"] = "incomplete_or_uncertain_group"
        elif group in groups:
            peers = [m for m in groups[group] if m["package_number"] != package]
            output["broad_peer_population_count"] = len(peers)
            output["broad_peer_unknown_quantity_count"] = sum(
                quantities[m["package_number"]][0] is None for m in peers
            )
            output["broad_peer_other_unit_count"] = sum(
                quantities[m["package_number"]][0] is not None
                and m["pack_unit"] != own["pack_unit"]
                for m in peers
            )
            if quantity is None:
                output["broad_peer_feature_state"] = "own_quantity_unknown"
            else:
                comparable = [
                    m
                    for m in peers
                    if quantities[m["package_number"]][0] is not None
                    and m["pack_unit"] == own["pack_unit"]
                ]
                prices = sorted(
                    Fraction(m["aip_ore"] * quantity, quantities[m["package_number"]][0])
                    for m in comparable
                )
                count = len(comparable)
                observed, rise, fall = 0, 0, 0
                for peer in comparable:
                    prior = previous.get(peer["package_number"])
                    current = source[peer["package_number"]]
                    if (
                        prior
                        and prior["eligible"]
                        and prior["period_index"] + 1 == own["period_index"]
                    ):
                        if (
                            all(
                                prior.get(k) == current.get(k)
                                for k in (
                                    "canonical_identity",
                                    "group_code",
                                    "pack_size_token",
                                    "pack_unit",
                                )
                            )
                            and prior.get("quantity_state") == "supported_scalar_quantity"
                        ):
                            observed += 1
                            rise += current["aip_ore"] > prior["aip_ore"]
                            fall += current["aip_ore"] < prior["aip_ore"]
                output.update(
                    broad_peer_count=count,
                    broad_peer_different_size_count=sum(
                        quantities[m["package_number"]][0] != quantity for m in comparable
                    ),
                    broad_peer_distributor_count=(
                        len({m["package_distributor"] for m in comparable})
                        if all(m.get("package_distributor") for m in comparable)
                        else None
                    ),
                    broad_peer_lag1_observed_count=observed,
                    broad_peer_lag1_rise_share=rise / observed if observed else None,
                    broad_peer_lag1_fall_share=fall / observed if observed else None,
                    broad_peer_feature_state=(
                        "supported_quantity_singleton"
                        if not count
                        else "complete_same_unit_peers"
                        if count == len(peers)
                        else "partial_same_unit_peers"
                    ),
                )
                if count:
                    minimum = prices[0]
                    output.update(
                        broad_peer_min_equivalent_aip_ore=float(minimum),
                        broad_peer_median_equivalent_aip_ore=float(
                            (prices[(count - 1) // 2] + prices[count // 2]) / 2
                        ),
                        broad_peer_spread_equivalent_aip_ore=float(prices[-1] - minimum),
                        broad_own_minus_peer_min_ore=float(own["aip_ore"] - minimum),
                        min_equivalent_aip_numerator=minimum.numerator,
                        min_equivalent_aip_denominator=minimum.denominator,
                    )
        result.append(output)
    return result, {
        p: {**r, "quantity_state": quantities.get(p, (None, "unknown"))[1]}
        for p, r in source.items()
    }


def _native_quantities(
    e2: dict[str, Any],
    archive_root: Path,
    output: Path,
) -> dict[str, Any]:
    """Reopen exact accepted original-byte pins; no parser, import or DB mutation."""
    receipt_pin = next(p for p in e2["files"] if p["role"] == "native_reconciliation")
    receipts = read_json(Path(receipt_pin["path"]))["receipts"]
    writer = pq.ParquetWriter(
        output / "native-quantities.parquet", NATIVE_SCHEMA, compression="zstd"
    )
    counts: Counter[str] = Counter()
    evidence = []
    try:
        for receipt in receipts:
            sha = receipt["original_sha256"]
            original = archive_root / "sha256" / sha[:2] / sha
            if digest(original) != sha:
                raise ResearchError("e3_peer_original_changed")
            with zipfile.ZipFile(original) as archive:
                names = {n.lower(): n for n in archive.namelist()}
                packages = _native_lines(archive.read(names["lms02.txt"]), 137)
                dictionaries = _native_lines(archive.read(names["lms15.txt"]), 65)
                groups = archive.read(names["lms04.txt"])
            import hashlib

            if hashlib.sha256(groups).hexdigest() != receipt["member_sha256"]:
                raise ResearchError("e3_peer_group_member_changed")
            group_lines = _native_lines(groups, 47)
            members = {line[4:10].decode("ascii") for line in group_lines}
            if len(members) != len(group_lines) or len(members) != receipt["records"]:
                raise ResearchError("e3_peer_native_population_changed")
            units = {}
            for line in dictionaries:
                if line[:1] == b"4":
                    unit = line[1:4].decode("ascii").strip()
                    if unit in units:
                        raise ResearchError("e3_peer_duplicate_native_unit")
                    units[unit] = (
                        line[4:14].decode("cp850").strip(),
                        line[14:64].decode("cp850").strip(),
                    )
            extracted = []
            for line in packages:
                package = line[11:17].decode("ascii")
                if package not in members:
                    continue
                unit = line[67:69].decode("ascii").strip()
                short, description = units.get(unit, (None, None))
                row = {
                    "period_start": date.fromisoformat(receipt["period"]),
                    "package_number": package,
                    "pack_size_token": line[59:67].decode("ascii").strip(),
                    "pack_unit": unit,
                    "pack_description": line[29:59].decode("cp850").strip(),
                    "component_package": line[20:26].decode("ascii").strip(),
                    "component_count_token": line[26:29].decode("ascii").strip(),
                    "unit_short_text": short,
                    "unit_description": description,
                }
                quantity, state = scalar_quantity(row)
                row.update(quantity_token_value=quantity, quantity_state=state)
                extracted.append(row)
                counts[state] += 1
            if len(extracted) != len(members):
                raise ResearchError("e3_peer_native_quantity_identity_missing")
            writer.write_table(pa.Table.from_pylist(extracted, schema=NATIVE_SCHEMA))
            evidence.append(
                {
                    "period": receipt["period"],
                    "original_sha256": sha,
                    "members": len(members),
                    "unit_dictionary": units,
                }
            )
    finally:
        writer.close()
    return {
        "periods": len(receipts),
        "rows": sum(counts.values()),
        "quantity_states": dict(counts),
        "native_receipts": evidence,
    }


def build_broader_peer_features(
    bundle_manifest: Path,
    e2_group_manifest: Path,
    operating_config: Path,
    output_root: Path,
) -> Path:
    """Build immutable full-key sidecar from accepted source/equal-size evidence."""
    from bef_redbird.foundation.archive import external_path

    output = external_path(output_root)
    if output.exists():
        raise ResearchError("e3_peer_output_already_exists")
    output.mkdir(parents=True)
    e2 = load_group_features(e2_group_manifest)
    if e2["bundle"]["sha256"] != digest(bundle_manifest):
        raise ResearchError("e3_peer_training_binding_changed")
    bundle = read_json(bundle_manifest)
    config = read_json(operating_config)
    native_report = _native_quantities(e2, Path(config["archive_root"]), output)
    atomic_json(output / "native-quantity-evidence.json", native_report)
    paths: dict[str, list[str]] = defaultdict(list)
    for item in bundle["files"]:
        if item["role"] in {"source", "features"}:
            path = bundle_manifest.parent / item["path"]
            if digest(path) != item["sha256"]:
                raise ResearchError("e3_peer_base_input_changed")
            paths[item["role"]].append(str(path))
    file_for = {p["role"]: p["path"] for p in e2["files"]}
    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='4GB'")
    connection.execute("SET temp_directory=?", [str(output / "spill")])
    connection.execute("SET max_temp_directory_size='4GiB'")
    connection.execute(
        "CREATE TEMP TABLE source AS SELECT f.row_key,f.period_index,f.eligible,"
        "s.period_start,s.package_number,s.canonical_identity,s.drug_id,s.group_code,"
        "s.pack_size_token,s.pack_unit,s.package_distributor,s.aip_ore,g.peer_feature_state "
        "FROM read_parquet(?) s JOIN read_parquet(?) f USING(package_number,period_start) "
        "JOIN read_parquet(?) g USING(row_key) ORDER BY s.period_start,s.package_number",
        [paths["source"], paths["features"], file_for["features"]],
    )
    connection.execute(
        "CREATE TEMP TABLE native AS SELECT n.*,q.* EXCLUDE(period_start,package_number) "
        "FROM read_parquet(?) n JOIN read_parquet(?) q USING(period_start,package_number) "
        "ORDER BY period_start,package_number",
        [file_for["native_members"], str(output / "native-quantities.parquet")],
    )
    population_count = connection.execute(
        "SELECT count(*),count(DISTINCT row_key) FROM source"
    ).fetchone()
    assert population_count is not None
    total, distinct = population_count
    if total != bundle["rows"] or distinct != total:
        raise ResearchError("e3_peer_source_population_changed")
    native_count = connection.execute("SELECT count(*) FROM native").fetchone()
    if native_count is None or native_count[0] != e2["native_rows"]:
        raise ResearchError("e3_peer_native_join_population_changed")
    writer = pq.ParquetWriter(output / "peer-features.parquet", SCHEMA, compression="zstd")
    states: Counter[str] = Counter()
    periods = []
    previous: dict[str, dict[str, Any]] = {}
    try:
        for (period,) in connection.execute(
            "SELECT DISTINCT period_start FROM source ORDER BY 1"
        ).fetchall():
            rows = list(
                _records(connection.execute("SELECT * FROM source WHERE period_start=?", [period]))
            )
            native = list(
                _records(connection.execute("SELECT * FROM native WHERE period_start=?", [period]))
            )
            features, previous = period_features(rows, native, previous)
            writer.write_table(pa.Table.from_pylist(features, schema=SCHEMA))
            counts = Counter(r["broad_peer_feature_state"] for r in features)
            states.update(counts)
            periods.append(
                {
                    "period": period.isoformat(),
                    "rows": len(features),
                    "states": dict(counts),
                    "origins_with_different_size_peers": sum(
                        (r["broad_peer_different_size_count"] or 0) > 0 for r in features
                    ),
                }
            )
    finally:
        writer.close()
        connection.close()
    atomic_json(
        output / "coverage.json", {"rows": total, "state_counts": dict(states), "periods": periods}
    )
    atomic_json(output / "contract.json", contract())
    result = {
        "contract": VERSION,
        "status": "complete_native_reconciled",
        "rows": total,
        "bundle": pin(bundle_manifest),
        "e2_groups": pin(e2_group_manifest),
        "operating_config": pin(operating_config),
        "feature_names": list(FEATURES),
        "numeric_names": list(NUMERICS),
        "categorical_names": list(CATEGORIES),
        "state_counts": dict(states),
        "native_rows": native_report["rows"],
        "native_periods": native_report["periods"],
        "files": [
            dict(pin(output / name), role=role)
            for name, role in (
                ("peer-features.parquet", "features"),
                ("native-quantities.parquet", "native_quantities"),
                ("native-quantity-evidence.json", "native_evidence"),
                ("coverage.json", "coverage"),
                ("contract.json", "contract"),
            )
        ],
        "producer_sha256": digest(Path(__file__)),
    }
    atomic_json(output / "manifest.json", result)
    return output / "manifest.json"


def load_broader_peer_features(manifest: Path) -> dict[str, Any]:
    document = read_json(manifest)
    if (
        document.get("contract") != VERSION
        or document.get("status") != "complete_native_reconciled"
        or document.get("feature_names") != list(FEATURES)
        or document.get("numeric_names") != list(NUMERICS)
        or document.get("categorical_names") != list(CATEGORIES)
        or Counter(p["role"] for p in document["files"])
        != Counter(
            {
                "features": 1,
                "native_quantities": 1,
                "native_evidence": 1,
                "coverage": 1,
                "contract": 1,
            }
        )
    ):
        raise ResearchError("e3_peer_contract_changed")
    for item in [
        document["bundle"],
        document["e2_groups"],
        document["operating_config"],
        *document["files"],
    ]:
        if digest(Path(item["path"])) != item["sha256"]:
            raise ResearchError("e3_peer_pin_changed")
    files = {p["role"]: Path(p["path"]) for p in document["files"]}
    if read_json(files["contract"]) != contract():
        raise ResearchError("e3_peer_dictionary_changed")
    parquet = pq.ParquetFile(files["features"])
    if parquet.schema_arrow != SCHEMA or parquet.metadata.num_rows != document["rows"]:
        raise ResearchError("e3_peer_schema_or_count_changed")
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--e2-groups", type=Path, required=True)
    parser.add_argument("--operating-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(
        build_broader_peer_features(
            args.bundle, args.e2_groups, args.operating_config, args.output_root
        )
    )


if __name__ == "__main__":
    main()
