"""Dated company-role evidence and recommendation-only E5 eligibility.

Accepted source/peer populations remain intact. No fitting, foundation writes or
current-company-name backfill is performed by this module.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from .dataset import ResearchError, digest
from .e4_common import checked, frozen_json, read, require_protocol, utc
from .group_features import pin

VERSION = "e5-population-v1"
ROLES = ("package_distributor", "drug_distributor", "authorisation_holder")
COMPANIES = ("BMS", "Astellas", "Amgen", "Evolan")
HYBRID_POINT = "perfect_hybrid_conditional_interval_next_aip_dkk"


def company_family(name: str | None) -> str | None:
    """Textual native aliases only; this is not a corporate-ownership inference."""
    token = (name or "").strip().casefold()
    if re.match(r"^bms(?:\b|$)|^bristol[ -]myers squibb(?:\b|$)", token):
        return "BMS"
    for company in COMPANIES[1:]:
        if re.match(r"^" + company.casefold() + r"(?:\b|$)", token):
            return company
    return None


@dataclass(frozen=True)
class EligibilityPolicy:
    version: str = VERSION
    role: str = "package_distributor"
    excluded_companies: tuple[str, ...] = COMPANIES
    min_history: int = 12
    lookback: int = 12
    min_premium_observations: int = 6
    median_premium_min: float = 0.20
    max_change_frequency: float = 1 / 12
    min_prior_flat_run: int = 6
    training_exclusion: bool = False
    peer_exclusion: bool = False

    def __post_init__(self) -> None:
        if (
            self.version != VERSION
            or self.role not in ROLES
            or self.training_exclusion
            or self.peer_exclusion
            or self.min_history < self.lookback
            or not 1 <= self.min_premium_observations <= self.lookback
            or not 0 <= self.max_change_frequency <= 1
            or self.min_prior_flat_run < 0
            or self.median_premium_min < 0
        ):
            raise ResearchError("e5_invalid_recommendation_eligibility_policy")


def eligibility(frame: pd.DataFrame, policy: EligibilityPolicy) -> pd.DataFrame:
    """Origin features only; short/missing evidence retains the recommendation.

    Callers supply the named, dated company role and strictly earlier rolling
    statistics. Labels and prediction amounts are deliberately absent here.
    """
    out = pd.DataFrame(index=frame.index)
    family = frame[policy.role + "_family"]
    out["identity_excluded"] = family.isin(policy.excluded_companies)
    out["identity_reason"] = "retained_other_company"
    unresolved = frame[policy.role + "_name"].isna()
    out.loc[unresolved, "identity_reason"] = "retained_unresolved_company_role"
    out.loc[out.identity_excluded, "identity_reason"] = "excluded_named_" + policy.role
    sufficient = (
        frame.history_periods.ge(policy.min_history)
        & frame.prior_observations.eq(policy.lookback)
        & frame.prior_premium_observations.ge(policy.min_premium_observations)
        & frame.prior_changes_observed.eq(policy.lookback)
        & frame.prior_flat_run.notna()
    )
    excluded = (
        sufficient
        & frame.prior_median_premium.ge(policy.median_premium_min)
        & frame.prior_change_frequency.le(policy.max_change_frequency)
        & frame.prior_flat_run.ge(policy.min_prior_flat_run)
    ).fillna(False)
    out["lagged_excluded"] = excluded
    out["lagged_reason"] = "retained_below_fixed_criteria"
    out.loc[~sufficient.fillna(False), "lagged_reason"] = "retained_insufficient_lagged_evidence"
    out.loc[excluded, "lagged_reason"] = "excluded_lagged_premium_and_stability"
    return out


def _rows(db: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    return json.loads(db.execute(sql).fetchdf().to_json(orient="records", date_format="iso"))


def _selected_members(db: duckdb.DuckDBPyConnection, number: int, columns: str) -> None:
    db.execute(
        f"""CREATE TEMP TABLE m{number} AS
        SELECT dm.period_start, dm.import_id, t.row_id, {columns}
        FROM foundation.registry_dataset_member dm
        JOIN foundation.registry_member m USING(member_id),
        UNNEST(m.row_ids) ids(row_id) JOIN foundation.registry_lms{number:02} t USING(row_id)
        WHERE dm.phase='final' AND dm.state='selected' AND dm.member_number={number}"""
    )


def _native_checks(
    db: duckdb.DuckDBPyConnection, archive_root: Path, aliases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Independently reopen each newly used company code/name in a dated original.

    This checks only new mapping values, not an audit of accepted prices/features.
    Native role witnesses are checked in the same edition for every family/role.
    """
    checks = []
    witnesses = _rows(
        db,
        """SELECT * EXCLUDE(n) FROM (
        SELECT *,row_number() OVER(PARTITION BY role,family ORDER BY period_start,package_number) n
        FROM roles_long WHERE family IS NOT NULL) WHERE n=1""",
    )
    requests = [
        {
            "period_start": a["first_period"],
            "company_code": a["company_code"],
            "company_name": a["company_name"],
            "kind": "alias",
        }
        for a in aliases
    ] + [{**r, "kind": "role"} for r in witnesses]
    for request in requests:
        day = str(request["period_start"])[:10]
        delivery = db.execute(
            """SELECT s.archive_location,s.content_hash FROM foundation.registry_dataset_member dm
            JOIN foundation.registry_release r USING(import_id)
            JOIN foundation.source_delivery s USING(delivery_id)
            WHERE dm.phase='final' AND dm.state='selected' AND dm.member_number=9
            AND dm.period_start=?""",
            [day],
        ).fetchone()
        if not delivery:
            raise ResearchError("e5_native_company_source_missing")
        original = archive_root / delivery[0]
        if digest(original) != delivery[1]:
            raise ResearchError("e5_native_company_original_changed")
        with zipfile.ZipFile(original) as archive:
            names = {n.lower(): n for n in archive.namelist()}
            lines = archive.read(names["lms09.txt"]).splitlines()
            match = [
                (i + 1, line)
                for i, line in enumerate(lines)
                if line[:6].decode("cp850").strip() == request["company_code"]
                and line[26:58].decode("cp850").strip() == request["company_name"]
            ]
            if len(match) != 1:
                raise ResearchError("e5_native_company_mapping_mismatch")
            proof: dict[str, Any] = {
                "kind": request["kind"],
                "period": day,
                "company_code": request["company_code"],
                "company_name": request["company_name"],
                "original_sha256": delivery[1],
                "member": "LMS09.TXT",
                "line": match[0][0],
                "matched": True,
            }
            if request["kind"] == "role":
                role = request["role"]
                number, start, end = (
                    (2, 131, 137)
                    if role == ROLES[0]
                    else ((1, 132, 138) if role == ROLES[1] else (1, 126, 132))
                )
                native = archive.read(names[f"lms{number:02}.txt"]).splitlines()
                key = request["package_number"] if number == 2 else request["drug_id"]
                keyed = (
                    [
                        (i + 1, line)
                        for i, line in enumerate(native)
                        if line[11:17].decode("cp850").strip() == key
                    ]
                    if number == 2
                    else [
                        (i + 1, line)
                        for i, line in enumerate(native)
                        if line[:11].decode("cp850").strip() == key
                    ]
                )
                if (
                    len(keyed) != 1
                    or keyed[0][1][start:end].decode("cp850").strip() != request["company_code"]
                ):
                    raise ResearchError("e5_native_company_role_mismatch")
                proof.update(role=role, role_member=f"LMS{number:02}.TXT", role_line=keyed[0][0])
            checks.append(proof)
    return checks


def _history(db: duckdb.DuckDBPyConnection, data: Path) -> None:
    dataset_path = checked(read(data / "training/current.json")["dataset"])
    dataset = read(dataset_path)
    for role in ("source", "features"):
        paths = [
            str(checked({**f, "path": str(dataset_path.parent / f["path"])}))
            for f in dataset["files"]
            if f["role"] == role
        ]
        db.read_parquet(paths).create_view("r1_" + role)
    manifest = read(data / "research-e2/groups-v1/manifest.json")
    peer_ref = next(f for f in manifest["files"] if f["role"] == "features")
    db.read_parquet(str(checked(peer_ref))).create_view("peers")
    db.execute("""CREATE TEMP TABLE history AS
        SELECT f.row_key,f.package_number,f.period_start,f.period_index,f.identity_episode_id,
        f.eligible,f.history_periods,f.aip_ore,f.lag1_delta_ore,f.abc,s.drug_id,
        s.package_distributor,p.peer_count,p.peer_feature_state,p.peer_min_aip_ore,
        CASE WHEN f.eligible AND p.peer_min_aip_ore>0 THEN
        (f.aip_ore-p.peer_min_aip_ore)::DOUBLE/p.peer_min_aip_ore END premium,
        sum(CASE WHEN coalesce(f.lag1_delta_ore,1)!=0 THEN 1 ELSE 0 END)
        OVER(PARTITION BY f.identity_episode_id ORDER BY f.period_index) run_group
        FROM r1_features f JOIN r1_source s USING(package_number,period_start)
        LEFT JOIN peers p USING(row_key)""")
    db.execute("""CREATE TEMP TABLE history_runs AS SELECT *,
        row_number() OVER(PARTITION BY identity_episode_id,run_group
        ORDER BY period_index)-1 flat_run
        FROM history""")
    db.execute("""CREATE TEMP TABLE causal AS SELECT *,
        count(*) OVER w prior_observations,
        count(premium) OVER w prior_premium_observations,
        median(premium) OVER w prior_median_premium,
        count(lag1_delta_ore) OVER w prior_changes_observed,
        avg(CASE WHEN lag1_delta_ore IS NOT NULL THEN (lag1_delta_ore!=0)::INT END)
        OVER w prior_change_frequency,
        lag(flat_run) OVER(PARTITION BY identity_episode_id ORDER BY period_index) prior_flat_run
        FROM history_runs WINDOW w AS (PARTITION BY identity_episode_id ORDER BY period_index
        ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING)""")


def _role_evidence(db: duckdb.DuckDBPyConnection) -> None:
    _selected_members(db, 1, "drug_id,drug_distributor,authorisation_holder")
    _selected_members(db, 9, "company_code,company_name,short_name,parallel_import_code")
    # R1's same-edition package distributor is already independently accepted.
    db.execute("""CREATE TEMP TABLE roles_long AS WITH role_codes AS (
        SELECT h.row_key,h.package_number,h.period_start,h.drug_id,r.role,r.company_code
        FROM history h LEFT JOIN m1 d USING(period_start,drug_id)
        CROSS JOIN LATERAL (VALUES ('package_distributor',h.package_distributor),
        ('drug_distributor',d.drug_distributor),('authorisation_holder',d.authorisation_holder))
        r(role,company_code))
        SELECT q.*,c.company_name,c.short_name,c.parallel_import_code,
        CASE WHEN regexp_matches(lower(c.company_name),'^(bms\\b|bristol[- ]myers squibb\\b)')
            THEN 'BMS'
            WHEN regexp_matches(lower(c.company_name),'^astellas\\b') THEN 'Astellas'
            WHEN regexp_matches(lower(c.company_name),'^amgen\\b') THEN 'Amgen'
            WHEN regexp_matches(lower(c.company_name),'^evolan\\b') THEN 'Evolan' END AS family
        FROM role_codes q LEFT JOIN m9 c
        ON c.period_start=q.period_start AND c.company_code=q.company_code""")


def _evaluation(db: duckdb.DuckDBPyConnection, data: Path) -> pd.DataFrame:
    frames = []
    accepted = read(data / "research-e4/evaluation.json")
    refs = {f["path"]: f for annual in accepted["annual"] for f in annual["artifacts"]}
    for year in (2024, 2025, 2026):
        e4 = data / f"research-e4/evaluation-{year}"
        e3 = data / f"research-e3/run-v1/cohorts/evaluation_{year}"
        for filename in ("predictions.parquet", "selections.parquet"):
            checked(refs[str((e4 / filename).resolve())])
        predictions = pd.read_parquet(
            e4 / "predictions.parquet",
            columns=[
                "row_key",
                "perfect_hybrid_p0",
                "perfect_hybrid_p1",
                "perfect_hybrid_p2",
                "perfect_advance_original_interval_next_aip_dkk",
                HYBRID_POINT,
                "broad_peer_count",
            ],
        )
        selections = pd.read_parquet(
            e4 / "selections.parquet", columns=["row_key", "perfect_hybrid_threshold"]
        )
        outcomes = pd.read_parquet(
            checked(read(e3 / "manifest.json")["outcomes"]),
            columns=["row_key", "next_status", "next_aip_ore", "next_delta_ore", "next_direction"],
        )
        frames.append(
            predictions.merge(selections, validate="one_to_one").merge(
                outcomes, validate="one_to_one"
            )
        )
    scores = pd.concat(frames, ignore_index=True)
    db.register("scores", scores)
    return db.execute("""SELECT c.*,s.* EXCLUDE(row_key),
        r.company_code package_distributor_code,r.company_name package_distributor_name,
        r.family package_distributor_family,r.parallel_import_code package_parallel_import_code,
        CASE WHEN r.company_code IS NULL OR r.company_name IS NULL THEN 'unresolved'
        ELSE 'dated_native_name' END package_distributor_mapping_state,
        d.company_code drug_distributor_code,d.company_name drug_distributor_name,
        d.family drug_distributor_family,d.parallel_import_code drug_parallel_import_code,
        a.company_code authorisation_holder_code,a.company_name authorisation_holder_name,
        a.family authorisation_holder_family,a.parallel_import_code holder_parallel_import_code
        FROM causal c JOIN scores s USING(row_key)
        LEFT JOIN roles_long r ON r.row_key=c.row_key AND r.role='package_distributor'
        LEFT JOIN roles_long d ON d.row_key=c.row_key AND d.role='drug_distributor'
        LEFT JOIN roles_long a ON a.row_key=c.row_key AND a.role='authorisation_holder'
        ORDER BY c.period_start,c.package_number""").fetchdf()


def _hybrid_point(data: Path, frame: pd.DataFrame) -> pd.DataFrame:
    """Attach the unchanged hybrid point to a successful older extraction checkpoint."""
    if HYBRID_POINT not in frame:
        accepted = read(data / "research-e4/evaluation.json")
        refs = {f["path"]: f for annual in accepted["annual"] for f in annual["artifacts"]}
        points = []
        for year in (2024, 2025, 2026):
            path = data / f"research-e4/evaluation-{year}/predictions.parquet"
            points.append(
                pd.read_parquet(
                    checked(refs[str(path.resolve())]), columns=["row_key", HYBRID_POINT]
                )
            )
        frame = frame.merge(
            pd.concat(points, ignore_index=True), on="row_key", how="left", validate="one_to_one"
        )
    if (
        not frame[HYBRID_POINT].notna().all()
        or not frame[HYBRID_POINT].abs().lt(float("inf")).all()
    ):
        raise ResearchError("e5_missing_hybrid_amount_point")
    return frame


def _current(data: Path, history: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Extend mapping to accepted October26 inputs without refreshing foundation."""
    lifecycle = read(data / "research-e4/lifecycle.json")
    entry = next(x for x in lifecycle["assessments"] if x["origin"] == "2026-10-26")
    assessment = read(checked(entry["assessment"]))
    current = pd.read_parquet(checked(assessment["predictions"]))
    snapshot = read(data / "research-e4/sources/2026-10-26/snapshot.json")
    original = checked(snapshot["original"])
    with zipfile.ZipFile(original) as archive:
        names = {n.lower(): n for n in archive.namelist()}
        drugs = {}
        for line in archive.read(names["lms01.txt"]).splitlines():
            drugs[line[:11].decode("cp850").strip()] = {
                "authorisation_holder": line[126:132].decode("cp850").strip(),
                "drug_distributor": line[132:138].decode("cp850").strip(),
            }
        companies = {}
        for line in archive.read(names["lms09.txt"]).splitlines():
            companies[line[:6].decode("cp850").strip()] = (
                line[26:58].decode("cp850").strip() or None,
                line[58:60].decode("cp850").strip() or None,
            )
    prior = {
        key: group.sort_values("period_index").tail(12)
        for key, group in history.groupby(["package_number", "identity_episode_id"], sort=False)
    }
    records = []
    for r in current.to_dict("records"):
        past = prior.get((r["package_number"], r["identity_episode_id"]), history.iloc[0:0])
        if not past.empty:
            past = past.loc[past.period_index.lt(r["period_index"])].tail(12)
            contiguous = len(past) and past.period_index.max() == r["period_index"] - 1
        else:
            contiguous = False
        result = {k: r.get(k) for k in history.columns}
        result.update(
            drug_id=r.get("origin_identity_token"),
            perfect_hybrid_threshold=bool(r["selected"]),
            perfect_hybrid_p0=r.get("p_fall"),
            perfect_hybrid_p1=r.get("p_unchanged"),
            perfect_hybrid_p2=r.get("p_rise"),
            perfect_advance_original_interval_next_aip_dkk=r.get("next_aip_dkk"),
            next_status="pending",
            next_aip_ore=None,
            next_delta_ore=None,
            prior_observations=len(past) if contiguous else 0,
            prior_premium_observations=int(past.premium.notna().sum()) if contiguous else 0,
            prior_median_premium=(
                past.premium.median() if contiguous and past.premium.notna().any() else None
            ),
            prior_changes_observed=int(past.lag1_delta_ore.notna().sum()) if contiguous else 0,
            prior_change_frequency=(
                past.lag1_delta_ore.dropna().ne(0).mean() if contiguous else None
            ),
            prior_flat_run=past.flat_run.iloc[-1] if contiguous else None,
        )
        result[HYBRID_POINT] = r.get("next_aip_dkk")
        drug = drugs.get(str(r.get("origin_identity_token") or ""), {})
        for role in ROLES:
            code = r.get(role) if role == "package_distributor" else drug.get(role)
            name, token = companies.get(str(code or ""), (None, None))
            result.update(
                {role + "_code": code, role + "_name": name, role + "_family": company_family(name)}
            )
            token_column = {
                ROLES[0]: "package_parallel_import_code",
                ROLES[1]: "drug_parallel_import_code",
                ROLES[2]: "holder_parallel_import_code",
            }[role]
            result[token_column] = token
        result["package_distributor_mapping_state"] = (
            "dated_native_name" if result["package_distributor_name"] else "unresolved"
        )
        records.append(result)
    frame = pd.DataFrame(records)
    frame["period_start"] = pd.to_datetime(frame.period_start)
    return frame, {
        "assessment": entry["assessment"],
        "snapshot": pin(data / "research-e4/sources/2026-10-26/snapshot.json"),
        "original": snapshot["original"],
        "rows": len(frame),
        "role_members": ["LMS01.TXT", "LMS09.TXT"],
        "history": "Last12 accepted eligible origins of same identity episode; absent retained",
    }


def _metrics(frame: pd.DataFrame) -> dict[str, Any]:
    known = frame.loc[frame.next_status.eq("observed")]
    errors = (known[HYBRID_POINT] - known.next_aip_ore / 100).abs()
    direction = known.next_delta_ore.gt(0).astype(int) + known.next_delta_ore.ge(0).astype(int)
    brier = pd.Series(0.0, index=known.index)
    for category in range(3):
        brier += (known[f"perfect_hybrid_p{category}"] - direction.eq(category).astype(int)) ** 2
    return {
        "rows": len(frame),
        "packages": int(frame.package_number.nunique()),
        "observed": len(known),
        "unknown": len(frame) - len(known),
        "rises": int(known.next_delta_ore.gt(0).sum()),
        "falls": int(known.next_delta_ore.lt(0).sum()),
        "flat": int(known.next_delta_ore.eq(0).sum()),
        "sum_absolute_error_dkk": float(errors.sum()),
        "sum_brier": float(brier.sum()),
        "brier": float(brier.mean()) if len(known) else None,
        "mae_dkk": float(errors.mean()) if len(known) else None,
        "downside_dkk_per_one_pack": float((-known.next_delta_ore.clip(upper=0) / 100).sum()),
        "rise_rate_observed": float(known.next_delta_ore.gt(0).mean()) if len(known) else None,
        "fall_rate_observed": float(known.next_delta_ore.lt(0).mean()) if len(known) else None,
        "equal_peer_observed": int(frame.peer_count.ge(1).sum()),
        "broad_peer_observed": int(frame.broad_peer_count.ge(1).sum()),
    }


def run(data: Path, root: Path) -> Path:
    """Run only after the parent E5 finite protocol has been frozen."""
    protocol = require_protocol(root)
    for reference in protocol["pins"].values():
        checked(reference)
    population = protocol["population"]
    policy = EligibilityPolicy()
    for key, value in asdict(policy).items():
        if key in population and population[key] != (
            list(value) if isinstance(value, tuple) else value
        ):
            raise ResearchError("e5_population_frozen_policy_mismatch:" + key)
    output = root / "population"
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    if report_path.exists():
        for item in read(report_path)["artifacts"]:
            checked(item)
        return report_path
    started = time.monotonic()
    checkpoint = output / "extraction-checkpoint.json"
    cache_reused = checkpoint.exists()
    if cache_reused:
        saved = read(checkpoint)
        if saved["protocol"] != pin(root / "protocol.json"):
            raise ResearchError("e5_population_checkpoint_protocol_changed")
        aliases, checks, descriptive = saved["aliases"], saved["checks"], saved["history"]
        frame = pd.read_parquet(checked(saved["rows"]))
    else:
        config = read(data / "foundation-operations/config.json")
        # Only the private in-memory catalog is writable; foundation is attached read-only.
        with duckdb.connect() as db:
            literal = str(config["database"]).replace("'", "''")
            db.execute(f"ATTACH '{literal}' AS foundation (READ_ONLY)")
            db.execute("SET threads=4")
            db.execute("SET memory_limit='4GB'")
            db.execute("SET enable_progress_bar=false")
            spill = output / "spill"
            db.execute("SET temp_directory=?", [str(spill)])
            print(json.dumps({"stage": "history"}), flush=True)
            _history(db, data)
            print(json.dumps({"stage": "company_roles"}), flush=True)
            _role_evidence(db)
            aliases = _rows(
                db,
                """SELECT company_code,company_name,short_name,parallel_import_code,
                family,min(period_start) first_period,max(period_start) last_period,
                count(distinct period_start) observed_periods
                FROM roles_long WHERE family IS NOT NULL
                GROUP BY ALL ORDER BY family,company_code""",
            )
            checks = _native_checks(db, Path(config["archive_root"]), aliases)
            print(json.dumps({"stage": "native_company_checks", "checks": len(checks)}), flush=True)
            descriptive = _rows(
                db,
                """SELECT r.role,coalesce(r.family,'other_or_unresolved') AS family,
                count(*) observations,count(distinct h.package_number) packages,
                min(h.period_start) first_period,max(h.period_start) last_period,
                count(h.lag1_delta_ore) observed_changes,
                avg((h.lag1_delta_ore!=0)::INT) change_frequency,
                avg(abs(h.lag1_delta_ore)/100.0) mean_abs_change_dkk,
                median(abs(h.lag1_delta_ore)/100.0) median_abs_change_dkk,
                median(h.flat_run) median_flat_run,max(h.flat_run) maximum_flat_run,
                count(h.premium) comparable_premium_observations,
                median(h.premium) median_equal_peer_premium,
                avg((h.peer_count>=1)::INT) equal_peer_coverage,
                count(*) FILTER(WHERE h.abc='A') abc_a,
                count(*) FILTER(WHERE h.abc='B') abc_b,
                count(*) FILTER(WHERE h.abc='C') abc_c,
                count(*) FILTER(WHERE h.abc IS NULL) abc_missing,
                count(*) FILTER(WHERE r.company_code IS NULL OR r.company_name IS NULL)
                unresolved_mapping
                FROM history_runs h JOIN roles_long r USING(row_key)
                GROUP BY ALL ORDER BY r.role,family""",
            )
            frame = _evaluation(db, data)
        frame.to_parquet(output / "extraction-checkpoint.parquet", index=False)
        frozen_json(
            checkpoint,
            {
                "protocol": pin(root / "protocol.json"),
                "aliases": aliases,
                "checks": checks,
                "history": descriptive,
                "rows": pin(output / "extraction-checkpoint.parquet"),
            },
        )
    frame = _hybrid_point(data, frame)
    flags = eligibility(frame, policy)
    frame = pd.concat([frame, flags], axis=1)
    frame["behavior20_excluded"] = frame.lagged_excluded
    behavior50 = eligibility(frame, EligibilityPolicy(median_premium_min=0.50))
    frame["behavior50_excluded"] = behavior50.lagged_excluded
    selected = frame.perfect_hybrid_threshold
    comparisons = {"all_candidates": _metrics(frame), "e4_selected": _metrics(frame.loc[selected])}
    for name in ("identity", "lagged"):
        excluded = frame[name + "_excluded"]
        comparisons[name + "_excluded_candidates"] = _metrics(frame.loc[excluded])
        comparisons[name + "_removed_selections"] = _metrics(frame.loc[selected & excluded])
        comparisons[name + "_retained_selections"] = _metrics(frame.loc[selected & ~excluded])
    years = frame.period_start.dt.year
    development = frame.loc[selected & years.eq(2025)]
    development_scores = []
    baseline = _metrics(development)
    baseline_downside = (
        baseline["downside_dkk_per_one_pack"] / baseline["observed"]
        if baseline["observed"]
        else None
    )
    chosen, best_downside = "no_filter", baseline_downside
    for name in ("behavior20", "behavior50"):
        retained = development.loc[~development[name + "_excluded"]]
        score = _metrics(retained)
        coverage = len(retained) / len(development) if len(development) else 0
        downside = (
            score["downside_dkk_per_one_pack"] / score["observed"] if score["observed"] else None
        )
        valid = coverage >= 0.95 and downside is not None
        development_scores.append(
            {
                "policy": name,
                "coverage": coverage,
                "valid": valid,
                "downside_per_observed_pack_dkk": downside,
                **score,
            }
        )
        if valid and best_downside is not None and downside < best_downside:
            chosen, best_downside = name, downside
    frame["behavior_selected_excluded"] = (
        False if chosen == "no_filter" else frame[chosen + "_excluded"]
    )
    annual = []
    for year in (2024, 2025, 2026):
        for name in ("no_filter", "identity", "behavior20", "behavior50", "behavior_selected"):
            keep = (
                pd.Series(True, index=frame.index)
                if name == "no_filter"
                else ~frame[name + "_excluded"]
            )
            annual.append(
                {
                    "year": year,
                    "policy": name,
                    "use": "development"
                    if year == 2025
                    else "confirmation"
                    if year == 2026
                    else "descriptive",
                    **_metrics(frame.loc[selected & years.eq(year) & keep]),
                }
            )
    by_company = []
    for role in ROLES:
        for company in COMPANIES:
            subset = frame[role + "_family"].eq(company)
            by_company.append(
                {
                    "role": role,
                    "company": company,
                    "all": _metrics(frame.loc[subset]),
                    "selected": _metrics(frame.loc[subset & selected]),
                }
            )
    current, current_proof = _current(data, frame)
    current_flags = eligibility(current, policy)
    for column in current_flags:
        current[column] = current_flags[column]
    current["behavior20_excluded"] = current.lagged_excluded
    current["behavior50_excluded"] = eligibility(
        current, EligibilityPolicy(median_premium_min=0.50)
    ).lagged_excluded
    current["behavior_selected_excluded"] = (
        False if chosen == "no_filter" else current[chosen + "_excluded"]
    )
    current_summary = {
        "proof": current_proof,
        "all": _metrics(current),
        "selected": _metrics(current.loc[current.perfect_hybrid_threshold]),
    }
    for name in ("identity", "behavior20", "behavior50", "behavior_selected"):
        current_summary[name + "_removed_selections"] = _metrics(
            current.loc[current.perfect_hybrid_threshold & current[name + "_excluded"]]
        )
    cross_roles = []
    for company in COMPANIES:
        holder_rows = frame.loc[frame.authorisation_holder_family.eq(company)]
        for values, rows in holder_rows.groupby(
            ["package_distributor_name", "package_parallel_import_code"], dropna=False
        ):
            name_value: Any = values[0]
            token_value: Any = values[1]
            cross_roles.append(
                {
                    "holder_family": company,
                    "package_distributor_name": None if pd.isna(name_value) else name_value,
                    "native_parallel_token": None if pd.isna(token_value) else token_value,
                    "origin_rows": len(rows),
                }
            )
    frame["population_scope"] = "e4_retrospective_evaluation"
    current["population_scope"] = "e4_october26_pending"
    combined = pd.concat([frame, current], ignore_index=True)
    combined["period_start"] = pd.to_datetime(combined.period_start)
    combined.to_parquet(output / "population.parquet", index=False)
    summary = {
        "version": VERSION,
        "created_utc": utc(),
        "elapsed_seconds": time.monotonic() - started,
        "policy": asdict(policy),
        "protocol": pin(root / "protocol.json"),
        "source": (
            "Accepted R1/E2 history and same-period final native registry roles; "
            "E4 saved predictions"
        ),
        "price_basis": "DKK AIP excluding VAT/consumer dispensing fees, per equal-size package",
        "causal_features": (
            "Prior 12 origins only, same identity episode; current origin "
            "excluded from rolling summaries"
        ),
        "descriptive_scope": (
            "Full R1 accepted history; not decision-time company features or causal effects"
        ),
        "outcome_scope": (
            "E4 2024-2026 exposed retrospective evaluation; no new fits or threshold search"
        ),
        "queue": "Unchanged E4 perfect_hybrid_threshold selections; filter with no replacement",
        "money": (
            "Absolute errors of unchanged E4 hybrid amount point with genuine current "
            "fallback; conditional-interval point is unchanged, interval not adopted; "
            "one-pack AIP downside, not financial loss/profit"
        ),
        "amount_point_column": HYBRID_POINT,
        "reused_extraction_checkpoint": cache_reused,
        "original_full_population_pass_seconds": read(checkpoint).get(
            "full_population_pass_seconds"
        ),
        "mapping_limits": [
            "Textual aliases are dated native names; code reuse never backfills earlier names",
            "Bristol Laboratories is not established as BMS and is excluded from its alias family",
            "Joint BMS/Pfizer and BMS/AstraZeneca names remain explicitly visible",
            "Blank parallel-import codes are unknown, not evidence of direct marketing",
        ],
        "aliases": aliases,
        "native_checks": checks,
        "history": descriptive,
        "comparisons": comparisons,
        "company_contributions": by_company,
        "behavior_selection": {
            "development_year": 2025,
            "minimum_selected_coverage": 0.95,
            "criterion": "lower fall DKK per observed selected pack; tie no_filter",
            "baseline": baseline,
            "alternatives": development_scores,
            "chosen": chosen,
        },
        "annual_policy_results": annual,
        "current_assessment": current_summary,
        "cross_role_parallel_evidence": cross_roles,
        "artifacts": [pin(output / "population.parquet")],
        "producer": pin(Path(__file__)),
        "base_fits": 0,
        "database_writes": 0,
    }
    frozen_json(report_path, summary)
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    path = run(args.data.resolve(), args.root.resolve())
    print(json.dumps({"report": str(path), "version": VERSION}))


if __name__ == "__main__":
    main()
