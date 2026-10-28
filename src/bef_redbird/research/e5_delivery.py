"""Publish saved E5 evidence and prepare explicit research-only recovery capsules.

No database copy, fitting, network acquisition, deletion or original-directory
move occurs here. Real recovery is a separate, explicitly invoked delivery step.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import psutil

from . import e3_recovery
from .dataset import ResearchError, digest
from .e4_common import checked, frozen_json, read, utc
from .group_features import pin

REQUIRED = (
    "protocol.json",
    "freeze.json",
    "execution-decisions.json",
    "selection.json",
    "fit-ledger.json",
    "results.json",
    "financial.json",
    "historical-review.parquet",
    "historical-outcomes.parquet",
    "inputs/manifest.json",
    "inputs/features.parquet",
    "inputs/outcomes.parquet",
    "population/report.json",
    "population/population.parquet",
    "final-assessment.json",
    "index.json",
    "current.json",
    "producer-final.zip",
    "producer-final.json",
)
REVIEW_FIELDS = {
    "row_key",
    "package_number",
    "package_id",
    "period_start",
    "aip_ore",
    "abc",
    "advance_abc",
    "advance_state",
    "eligible",
    "exclusion_reason",
    "policy_eligible",
    "recommendation_eligible",
    "eligibility_reason",
    "company",
    "package_distributor",
    "history_periods",
    "peer_count",
    "peer_feature_state",
    "peer_route",
    "information_route",
    "calibration_route",
    "p_rise",
    "p_fall",
    "p_unchanged",
    "reference_p_rise",
    "reference_p_fall",
    "current_only_p_rise",
    "current_only_p_fall",
    "raw_forecast_delta_dkk",
    "forecast_delta_dkk",
    "forecast_aip_dkk",
    "forecast_percent",
    "raw_median_delta_dkk",
    "median_delta_dkk",
    "raw_q05_delta_dkk",
    "raw_q50_delta_dkk",
    "raw_q95_delta_dkk",
    "quantile_crossed",
    "experimental_lower_delta_dkk",
    "experimental_median_delta_dkk",
    "experimental_upper_delta_dkk",
    "accepted_lower_delta_dkk",
    "accepted_upper_delta_dkk",
    "lower_delta_dkk",
    "upper_delta_dkk",
    "interval_method",
    "persistence_probability",
    "selected",
    "rank",
    "policy",
    "selection_reason",
    "prediction_state",
    "ranking_interpretation",
    "fold",
    "identity_reason",
    "lagged_reason",
    "review_scope",
}
OUTCOME_FIELDS = {
    "row_key",
    "package_number",
    "package_id",
    "period_start",
    "target_period",
    "next_status",
    "next_aip_ore",
    "next_delta_ore",
    "next_direction",
    "second_status",
    "second_aip_ore",
    "persistence_target",
    "review_scope",
}


def _protocol(root: Path) -> dict[str, Any]:
    return read(checked(read(root / "freeze.json")["protocol"]))


def _review(frame: pd.DataFrame) -> pd.DataFrame:
    if any(
        str(c).startswith(("next_", "second_", "outcome")) or c == "persistence_target"
        for c in frame
    ):
        raise ResearchError("e5_delivery_outcomes_in_review")
    columns = [
        c
        for c in frame
        if c in REVIEW_FIELDS
        or str(c).endswith(("_rank", "_selected", "_name", "_family"))
        or str(c).startswith(("rank_", "selected_"))
    ]
    return frame[columns].copy()


def _identities(frame: pd.DataFrame, *, review: bool) -> None:
    if "row_key" not in frame or frame.row_key.isna().any() or frame.row_key.duplicated().any():
        raise ResearchError("e5_delivery_duplicate_or_missing_identity")
    if review and (
        "package_number" not in frame
        or not frame.package_number.map(lambda x: isinstance(x, str)).all()
        or not frame.package_number.str.fullmatch(r"[0-9]{6}").all()
    ):
        raise ResearchError("e5_delivery_invalid_package_identity")


def _outcomes(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame[[c for c in frame if c in OUTCOME_FIELDS]].copy()
    for column in ("period_start", "target_period"):
        if column in out:
            missing = out[column].isna()
            out[column] = pd.to_datetime(out[column])
            if not out[column].isna().equals(missing):
                raise ResearchError("e5_delivery_outcome_date_missingness_changed")
    for column in ("next_aip_ore", "next_delta_ore", "second_aip_ore"):
        if column in out:
            values = pd.to_numeric(out[column], errors="coerce")
            try:
                out[column] = values.astype("Int64")
            except (TypeError, ValueError, OverflowError) as exc:
                raise ResearchError("e5_delivery_outcome_money_not_exact_ore") from exc
    return out


def _parquet(path: Path, frame: pd.DataFrame) -> None:
    stream = io.BytesIO()
    frame.to_parquet(stream, index=False)
    payload = stream.getvalue()
    if path.exists():
        if (
            path.stat().st_size != len(payload)
            or digest(path) != hashlib.sha256(payload).hexdigest()
        ):
            raise ResearchError("e5_delivery_immutable_table_conflict")
        return
    with path.open("xb") as target:
        target.write(payload)


def publish_index(root: Path) -> Path:
    """Publish only after the separate final prediction and outcome attachment exist."""
    root = root.resolve()
    _protocol(root)
    index_path = root / "index.json"
    if index_path.exists():
        index = read(index_path)
        if index.get("version") != "e5-index-v1":
            raise ResearchError("e5_delivery_wrong_index_version")
        for key in (
            "protocol",
            "results",
            "review",
            "outcomes",
            "population",
            "fit_ledger",
            "assessment",
            "model_bundle",
            "contrasts",
            "explanations",
        ):
            if index.get(key):
                checked(index[key])
        for ref in index["binding"].values():
            checked(ref)
        _current(root, index_path, index["assessment"])
        return index_path
    result_path = root / "results.json"
    if read(result_path).get("version") != "e5-results-v1":
        raise ResearchError("e5_delivery_results_not_ready")
    final_ref = root / "final-assessment.json"
    final = read(final_ref)
    assessment_path = checked(final["assessment"])
    assessment = read(assessment_path)
    if assessment.get("version") != "e5-assessment-v1" or not assessment.get("completed_utc"):
        raise ResearchError("e5_delivery_final_prediction_not_complete")
    if assessment["binding"]["results"]["sha256"] != digest(result_path):
        raise ResearchError("e5_delivery_assessment_results_changed")
    outcome_path = checked(final["outcome"])
    outcome = read(outcome_path)
    if outcome["binding"]["assessment"]["sha256"] != digest(assessment_path):
        raise ResearchError("e5_delivery_outcome_assessment_mismatch")
    financial_path = root / "financial.json"
    financial = read(financial_path)
    historical = checked(financial["files"]["historical-review.parquet"])
    historical_outcomes = checked(financial["files"]["historical-outcomes.parquet"])
    current_predictions = checked(assessment["predictions"])
    current_outcomes = checked(outcome["rows"])
    population_path = root / "population/report.json"
    ledger_path = root / "fit-ledger.json"
    binding = {
        "protocol": pin(root / "protocol.json"),
        "results": pin(result_path),
        "financial": pin(financial_path),
        "historical_review": pin(historical),
        "historical_outcomes": pin(historical_outcomes),
        "final_pointer": pin(final_ref),
        "assessment": final["assessment"],
        "outcome": final["outcome"],
        "population": pin(population_path),
        "fit_ledger": pin(ledger_path),
        "producer": pin(Path(__file__)),
    }
    if assessment["binding"].get("models"):
        checked(assessment["binding"]["models"])
        binding["model_bundle"] = assessment["binding"]["models"]
    directory = (
        root / "delivery" / hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    )
    directory.mkdir(parents=True, exist_ok=True)
    old = _review(pd.read_parquet(historical)).assign(review_scope="historical_research")
    current = _review(pd.read_parquet(current_predictions)).assign(
        review_scope="current_frozen_assessment"
    )
    if len(current) != assessment["candidate_count"]:
        raise ResearchError("e5_delivery_current_population_changed")
    review = pd.concat([old, current], ignore_index=True)
    _identities(review, review=True)
    review["period_start"] = pd.to_datetime(review.period_start)
    labels = pd.read_parquet(current_outcomes).rename(columns={"status": "next_status"})
    if "period_start" not in labels:
        labels = labels.merge(
            current[["row_key", "period_start"]], on="row_key", how="left", validate="one_to_one"
        )
    labels["review_scope"] = "current_frozen_assessment"
    old_labels = pd.read_parquet(historical_outcomes).assign(review_scope="historical_research")
    labels = pd.concat([_outcomes(old_labels), _outcomes(labels)], ignore_index=True)
    _identities(labels, review=False)
    if set(labels.row_key) != set(review.row_key):
        raise ResearchError("e5_delivery_outcome_population_mismatch")
    _parquet(directory / "review.parquet", review)
    _parquet(directory / "outcomes.parquet", labels)
    value = {
        "version": "e5-index-v1",
        "status": "research_not_promoted",
        "created_utc": utc(),
        "protocol": binding["protocol"],
        "results": binding["results"],
        "review": pin(directory / "review.parquet"),
        "outcomes": pin(directory / "outcomes.parquet"),
        "population": binding["population"],
        "fit_ledger": binding["fit_ledger"],
        "assessment": final["assessment"],
        "binding": binding,
        "scope": (
            "Saved retrospective evidence and separate prospective-pending assessment; "
            "acceptance is a separate receipt"
        ),
    }
    if binding.get("model_bundle"):
        value["model_bundle"] = binding["model_bundle"]
    for name in ("contrasts", "explanations"):
        path = root / (name + ".json")
        if path.exists():
            value[name] = pin(path)
    frozen_json(index_path, value)
    _current(root, index_path, final["assessment"])
    return index_path


def _current(root: Path, index: Path, assessment: dict[str, Any]) -> None:
    path = root / "current.json"
    if path.exists():
        # A later owner-produced acceptance pointer is deliberately preserved.
        if read(path)["index"]["sha256"] != digest(index):
            raise ResearchError("e5_delivery_current_points_elsewhere")
        return
    frozen_json(
        path,
        {
            "version": "e5-current-v1",
            "index": pin(index),
            "assessment": assessment,
            "status": "research_not_promoted",
            "acceptance": "Separate receipt; this pointer does not assert delivery completion",
            "acceptance_path": "acceptance.json",
            "scheduling": "disabled and uninstalled",
        },
    )


def capture_final_source(root: Path, repository: Path) -> Path:
    """Freeze the final complete producer, independently of the earlier fit archive."""
    archive = root / "producer-final.zip"
    files = [
        *sorted((repository / "src").rglob("*.py")),
        *sorted((repository / "src").rglob("*.sql")),
        repository / "pyproject.toml",
        repository / "uv.lock",
        repository / "research_dashboard.py",
    ]
    entries = [
        {
            "relative": f.relative_to(repository).as_posix(),
            "sha256": digest(f),
            "size_bytes": f.stat().st_size,
        }
        for f in files
    ]
    receipt = root / "producer-final.json"
    if receipt.exists():
        prior = read(receipt)
        checked(prior["archive"])
        if prior["entries"] != entries:
            raise ResearchError("e5_delivery_final_producer_changed")
        return archive
    e3_recovery.capture_source(repository, archive)
    frozen_json(
        receipt,
        {
            "version": "e5-final-producer-v1",
            "archive": pin(archive),
            "entries": entries,
            "created_utc": utc(),
        },
    )
    return archive


def _disallowed(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        any(
            p.lower()
            in {
                "scratch",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                "synthetic-view",
            }
            or p.lower().startswith("tmp-")
            for p in parts
        )
        or Path(relative).suffix.lower()
        in {".log", ".pyc", ".duckdb", ".wal", ".db", ".sqlite", ".sqlite3"}
        or "streamlit" in Path(relative).name.lower()
    )


def _pins(value: Any) -> list[dict[str, Any]]:
    result = []
    if isinstance(value, dict):
        if all(k in value for k in ("path", "sha256", "size_bytes")):
            result.append(value)
        else:
            for child in value.values():
                result.extend(_pins(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_pins(child))
    return result


def external_dependencies(root: Path, files: list[str]) -> list[dict[str, Any]]:
    """Bounded dependency catalogue; shared accepted data is verified, never copied."""
    root = root.resolve()
    external: dict[str, dict[str, Any]] = {}
    expanded: set[Path] = set()
    protocol = _protocol(root)
    included = set(files)
    source_receipt = root / "producer-final.json"
    captured = read(source_receipt).get("entries", []) if source_receipt.exists() else []
    repository = Path(__file__).resolve().parents[3]
    captured_code = {x["relative"]: (x["sha256"], x["size_bytes"]) for x in captured}

    def admit(ref: dict[str, Any], parent: Path) -> Path:
        path = Path(ref["path"])
        path = path if path.is_absolute() else parent / path
        if path.suffix.lower() in {".duckdb", ".wal", ".db", ".sqlite", ".sqlite3"}:
            raise ResearchError("e5_delivery_database_dependency_forbidden")
        resolved = path.resolve()
        source_code = resolved.is_relative_to(repository / "src") and path.suffix.lower() in {
            ".py",
            ".sql",
        }
        producer_file = source_code or (
            resolved.parent == repository
            and resolved.name in {"pyproject.toml", "uv.lock", "research_dashboard.py"}
        )
        if producer_file:
            relative_code = resolved.relative_to(repository).as_posix()
            if captured_code.get(relative_code) == (ref["sha256"], ref["size_bytes"]):
                # These exact source bytes are already retained in the full producer.
                return resolved
            if source_code:
                # A historical code pin must not depend on the changed live file.
                relative_history = f"evidence/source-history/{ref['sha256']}.py"
                retained = root / relative_history
                if relative_history not in included:
                    raise ResearchError("e5_delivery_unlisted_historical_source")
                if (
                    not retained.is_file()
                    or retained.is_symlink()
                    or not retained.resolve().is_relative_to(root)
                ):
                    raise ResearchError("e5_delivery_missing_historical_source")
                return checked({**ref, "path": str(retained)}).resolve()
        checked({**ref, "path": str(path)})
        path = path.resolve()
        if path.is_relative_to(root):
            if path.relative_to(root).as_posix() not in included:
                raise ResearchError("e5_delivery_missing_internal_dependency")
        else:
            external[str(path)] = {**ref, "path": str(path)}
        return path

    def document(path: Path, *, follow_e4: bool = False) -> None:
        if path in expanded:
            return
        expanded.add(path)
        value = read(path)
        for ref in _pins(value):
            child = admit(ref, path.parent)
            # Current E4 source/model/calibration chain only, not its full evaluations.
            if (
                follow_e4
                and child.suffix.lower() == ".json"
                and child.is_relative_to(root.parent / "research-e4")
            ):
                if not any(part.startswith("evaluation-") for part in child.parts):
                    document(child, follow_e4=True)

    for relative in files:
        path = root / relative
        if path.is_file() and path.suffix.lower() == ".json":
            document(path)
    refs = protocol.get("pins", {})
    for ref in refs.values():
        admit(ref, root)
    if "e4_lifecycle" in refs:
        lifecycle = read(admit(refs["e4_lifecycle"], root))
        current = next(x for x in lifecycle["assessments"] if x["origin"] == "2026-10-26")
        document(admit(current["assessment"], root), follow_e4=True)
    if "e3_protocol" in refs and "e3_ledger" in refs:
        prior = admit(refs["e3_protocol"], root).parent
        ledger = read(admit(refs["e3_ledger"], root))
        for trial in ledger["trials"]:
            key = trial["trial_id"]
            if (
                trial["status"] == "completed"
                and key.startswith(("evaluation_2025-", "evaluation_2026-"))
                and ("-cb_" in key or "-amount_delta_floor_" in key)
            ):
                path = prior / "trials" / key / "trial.json"
                if read(path) != trial:
                    raise ResearchError("e5_delivery_accepted_trial_changed")
                admit(pin(path), root)
                for ref in trial["files"]:
                    admit(ref, path.parent)
        for year in (2025, 2026):
            manifest = prior / "cohorts" / f"evaluation_{year}" / "manifest.json"
            if manifest.exists():
                admit(pin(manifest), root)
                document(manifest)
    return [external[k] for k in sorted(external)]


def create_inventory(root: Path, output: Path) -> Path:
    """Create a reviewable list; this does not create or approve a capsule."""
    root = root.resolve()
    if output.exists():
        raise ResearchError("e5_delivery_inventory_requires_unused_path")
    files = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not _disallowed(p.relative_to(root).as_posix())
    )
    if output.resolve().is_relative_to(root):
        files.append(output.resolve().relative_to(root).as_posix())
        files = sorted(set(files))
    external = external_dependencies(root, files)
    frozen_json(
        output,
        {
            "version": "e5-reviewed-inventory-draft-v1",
            "source_root": str(root),
            "files": files,
            "external": external,
            "created_utc": utc(),
            "scope": (
                "Explicit review required; no scratch, caches, test/server logs or database "
                "files. Shared protocol, current E4 source chain and E3 2025/26 baseline "
                "dependencies remain external."
            ),
        },
    )
    return output


def _limits(root: Path) -> dict[str, int]:
    declared = _protocol(root).get("resource_limits", {})
    return {
        k: min(int(declared.get(k, v)), v)
        for k, v in {
            "research_bytes": 8 * 1024**3,
            "recovery_bytes": 4 * 1024**3,
            "process_rss_bytes": 24 * 1024**3,
        }.items()
    }


def _rss() -> int:
    memory = psutil.Process().memory_info()
    return max(memory.rss, getattr(memory, "peak_wset", memory.rss))


def _zip_verified(archive_path: Path, payload: list[dict[str, Any]]) -> None:
    expected = {x["path"]: x for x in payload}
    with zipfile.ZipFile(archive_path) as archive:
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != set(expected):
            raise ResearchError("e5_delivery_archive_inventory_changed")
        for name, ref in expected.items():
            h, size = hashlib.sha256(), 0
            with archive.open(name) as source:
                while chunk := source.read(1024 * 1024):
                    h.update(chunk)
                    size += len(chunk)
            if h.hexdigest() != ref["sha256"] or size != ref["size_bytes"]:
                raise ResearchError("e5_delivery_archive_payload_changed")


def prepare_capsule(root: Path, destination: Path, explicit_inventory: Path) -> Path:
    """Build e3-compatible bytes only from the supplied reviewed inventory."""
    root, destination = root.resolve(), destination.resolve()
    if destination.is_relative_to(root):
        raise ResearchError("e5_delivery_capsule_destination_must_be_external")
    inventory = read(explicit_inventory)
    if Path(inventory["source_root"]).resolve() != root:
        raise ResearchError("e5_delivery_inventory_root_mismatch")
    if destination.exists():
        manifest_path = destination / "recovery.json"
        if not manifest_path.exists():
            raise ResearchError("e5_delivery_capsule_destination_incomplete")
        saved = read(manifest_path)
        if saved["inventory"]["sha256"] != digest(explicit_inventory) or saved[
            "source_root"
        ] != str(root):
            raise ResearchError("e5_delivery_capsule_retry_changed_inventory")
        checked({**saved["archive"], "path": str(destination / saved["archive"]["path"])})
        _zip_verified(destination / "payload.zip", saved["payload"])
        checked({**saved["inventory"], "path": str(destination / saved["inventory"]["path"])})
        for item in saved["payload"]:
            checked({**item, "path": str(root / item["path"])})
        for item in saved["external"]:
            checked(item)
        return manifest_path
    limits = _limits(root)
    if sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > limits["research_bytes"]:
        raise ResearchError("e5_delivery_research_storage_limit")
    files = inventory["files"]
    if len(files) != len(set(files)) or set(REQUIRED) - set(files):
        raise ResearchError("e5_delivery_incomplete_or_duplicate_inventory")
    for path in (root / "trials").rglob("*"):
        if path.is_file() and path.name == "receipt.json" and read(path).get("status") == "running":
            raise ResearchError("e5_delivery_active_trial_cannot_be_captured")
        if (
            path.is_file()
            and path.name in {"model.cbm", "predictions.parquet", "curves.json", "receipt.json"}
            and path.relative_to(root).as_posix() not in files
        ):
            raise ResearchError("e5_delivery_trial_omitted_from_inventory")
    payload = []
    for relative in files:
        e3_recovery.safe_relative(relative)
        path = root / relative
        if (
            _disallowed(relative)
            or path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
        ):
            raise ResearchError("e5_delivery_forbidden_inventory_file")
        payload.append({**pin(path), "path": relative})
    external = inventory.get("external", [])
    for item in external:
        path = checked(item)
        if path.resolve().is_relative_to(root) or path.suffix.lower() in {
            ".duckdb",
            ".wal",
            ".db",
            ".sqlite",
            ".sqlite3",
        }:
            raise ResearchError("e5_delivery_invalid_external_dependency")
    needed = external_dependencies(root, files)
    available = {(x["path"], x["sha256"], x["size_bytes"]) for x in external}
    if any((x["path"], x["sha256"], x["size_bytes"]) not in available for x in needed):
        raise ResearchError("e5_delivery_missing_external_dependency")
    destination.mkdir(parents=True)
    peak = _rss()
    if peak > limits["process_rss_bytes"]:
        raise ResearchError("e5_delivery_process_memory_limit")
    with zipfile.ZipFile(
        destination / "payload.zip",
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
        allowZip64=True,
    ) as archive:
        for item in payload:
            archive.write(root / item["path"], item["path"])
            peak = max(peak, _rss())
            if peak > limits["process_rss_bytes"]:
                raise ResearchError("e5_delivery_process_memory_limit")
    if (destination / "payload.zip").stat().st_size > limits["recovery_bytes"]:
        raise ResearchError("e5_delivery_capsule_storage_limit")
    _zip_verified(destination / "payload.zip", payload)
    for item in payload:
        checked({**item, "path": str(root / item["path"])})
    (destination / "reviewed-inventory.json").write_bytes(explicit_inventory.read_bytes())
    value = {
        "version": e3_recovery.VERSION,
        "source_root": str(root),
        "payload": payload,
        "external": external,
        "archive": {**pin(destination / "payload.zip"), "path": "payload.zip"},
        "inventory": {
            **pin(destination / "reviewed-inventory.json"),
            "path": "reviewed-inventory.json",
        },
        "created_utc": utc(),
        "resource": {
            "sampled_peak_rss_bytes": peak,
            "limits": limits,
            "measurement": (
                "Windows whole-process peak working set when available; otherwise "
                "RSS at archive boundaries"
            ),
        },
        "scope": (
            "E5 research files and captured producer only; accepted shared dependencies "
            "remain external; no database copy or operational promotion"
        ),
    }
    frozen_json(destination / "recovery.json", value)
    return destination / "recovery.json"


def _producer_verified(archive_path: Path, directory: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ResearchError("e5_delivery_duplicate_producer_entry")
        for name in names:
            e3_recovery.safe_relative(name)
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != archive.read(name):
                raise ResearchError("e5_delivery_captured_producer_changed")
        if {
            p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()
        } != set(names):
            raise ResearchError("e5_delivery_unexpected_producer_file")


REPLAY_SCRIPT = r"""
import json,sys,socket,threading,time
from pathlib import Path
import psutil,duckdb,sqlite3,catboost
config=json.loads(sys.argv[1])
def forbidden(*args,**kwargs):
    raise RuntimeError('e5_recovery_fit_database_network_forbidden')
socket.create_connection=forbidden
socket.getaddrinfo=forbidden
socket.socket.connect=forbidden
socket.socket.connect_ex=forbidden
duckdb.connect=forbidden
duckdb.sql=forbidden
duckdb.query=forbidden
sqlite3.connect=forbidden
for cls in (catboost.CatBoost,catboost.CatBoostClassifier,catboost.CatBoostRegressor):
    for name in ('fit','grid_search','randomized_search'):
        setattr(cls,name,forbidden)
catboost.train=forbidden
catboost.cv=forbidden
from bef_redbird.research import e5_assessment
active=Path(e5_assessment.__file__).resolve()
if not active.is_relative_to(Path(config['producer']).resolve()/'src'):
    raise RuntimeError('e5_recovery_did_not_import_captured_producer')
peak=[psutil.Process().memory_info().rss]
stop=threading.Event()
def sample():
    while not stop.wait(.1):
        peak[0]=max(peak[0],psutil.Process().memory_info().rss)
thread=threading.Thread(target=sample,daemon=True)
thread.start()
try:
    proof=e5_assessment.replay(Path(config['assessment']),relocations={config['original']:config['restored']})
finally:
    stop.set();thread.join()
if peak[0]>config['rss_limit']:
    raise RuntimeError('e5_recovery_process_memory_limit')
if proof.get('exact_predictions') is not True or proof.get('exact_queues') is not True:
    raise RuntimeError('e5_recovery_exact_assertions_not_proven')
print(json.dumps({'proof':proof,'peak_rss_bytes':peak[0],
    'guards':'fit/database/network blocked','captured_producer':True}))
"""


def verify_replay(
    bundle: Path, restored_root: Path, producer_directory: Path, receipt_path: Path
) -> dict[str, Any]:
    """Fresh captured-code replay. The caller makes the original E5 root unavailable."""
    manifest = read(bundle / "recovery.json")
    e3_recovery.verify_restored(bundle, restored_root)
    binding = {
        "capsule": pin(bundle / "recovery.json"),
        "restored_root": str(restored_root.resolve()),
        "producer": pin(restored_root / "producer-final.zip"),
    }
    if receipt_path.exists():
        prior = read(receipt_path)
        if prior.get("status") != "verified" or prior["binding"] != binding:
            raise ResearchError("e5_delivery_replay_receipt_conflict")
        _producer_verified(restored_root / "producer-final.zip", producer_directory)
        return prior
    original = Path(manifest["source_root"])
    if original.exists():
        raise ResearchError("e5_delivery_original_root_must_be_unavailable")
    if producer_directory.resolve().is_relative_to(restored_root.resolve()):
        raise ResearchError("e5_delivery_producer_directory_must_be_external")
    archive_path = restored_root / "producer-final.zip"
    if not producer_directory.exists():
        e3_recovery.extract_source(archive_path, producer_directory)
    _producer_verified(archive_path, producer_directory)
    before_files = {
        p.relative_to(restored_root).as_posix() for p in restored_root.rglob("*") if p.is_file()
    }
    pointer = read(restored_root / "final-assessment.json")
    relative = Path(pointer["assessment"]["path"]).relative_to(original)
    assessment = restored_root / relative
    config = {
        "producer": str(producer_directory.resolve()),
        "assessment": str(assessment.resolve()),
        "original": str(original),
        "restored": str(restored_root.resolve()),
        "rss_limit": 24 * 1024**3,
    }
    environment = dict(
        os.environ,
        PYTHONPATH=str(producer_directory.resolve() / "src"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", REPLAY_SCRIPT, json.dumps(config)],
        cwd=producer_directory,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    after = e3_recovery.verify_restored(bundle, restored_root)
    if before_files != {
        p.relative_to(restored_root).as_posix() for p in restored_root.rglob("*") if p.is_file()
    }:
        raise ResearchError("e5_delivery_replay_changed_restored_inventory")
    _producer_verified(archive_path, producer_directory)
    value = {
        "version": "e5-recovery-replay-v1",
        "binding": binding,
        "status": "verified" if completed.returncode == 0 else "failed",
        "created_utc": utc(),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "restored_hashes_unchanged": True,
        "byte_verification": after,
        "original_root_unavailable": True,
        "context_timestamps_preserved": True,
    }
    if completed.returncode == 0:
        value["replay"] = json.loads(completed.stdout.strip().splitlines()[-1])
    frozen_json(receipt_path, value)
    if completed.returncode:
        raise ResearchError("e5_delivery_captured_replay_failed_receipt_retained")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["index", "source", "inventory", "capsule", "restore", "verify", "replay"],
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--producer", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.command == "index" and args.root:
        result: Any = publish_index(args.root)
    elif args.command == "source" and args.root and args.repository:
        result = capture_final_source(args.root, args.repository)
    elif args.command == "inventory" and args.root and args.inventory:
        result = create_inventory(args.root, args.inventory)
    elif args.command == "capsule" and args.root and args.destination and args.inventory:
        result = prepare_capsule(args.root, args.destination, args.inventory)
    elif args.command == "restore" and args.bundle and args.destination and args.receipt:
        result = e3_recovery.restore(args.bundle, args.destination, args.receipt)
    elif args.command == "verify" and args.bundle and args.destination:
        result = e3_recovery.verify_restored(args.bundle, args.destination)
    elif args.command == "replay" and args.bundle and args.root and args.producer and args.receipt:
        result = verify_replay(args.bundle, args.root, args.producer, args.receipt)
    else:
        parser.error("Required arguments missing")
    print(result)


if __name__ == "__main__":
    main()
