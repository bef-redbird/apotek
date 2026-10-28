"""Model-neutral full-history training contract; independent of the live foundation."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa

from .dataset import ResearchError, digest, fingerprint

VERSION = "full-history-training-v1"
CATEGORIES = (
    "abc",
    "group_state",
    "atc",
    "form_code",
    "strength",
    "strength_unit",
    "pack_size_token",
    "pack_unit",
    "dispensing_code",
    "package_distributor",
    "supply_state",
)
NUMERICS = (
    "aip_ore",
    "lag1_delta_ore",
    "lag2_delta_ore",
    "lag3_delta_ore",
    "history_periods",
    "past_rise_frequency",
    "past_fall_frequency",
    "past_flat_frequency",
    "lifecycle_age_periods",
)
FEATURES = (*NUMERICS, *CATEGORIES)
KEYS = ("row_key", "package_number", "period_index", "period_start", "identity_episode_id")
KEY_SCHEMA = [
    pa.field("row_key", pa.string(), False),
    pa.field("package_number", pa.string(), False),
    pa.field("period_index", pa.int32(), False),
    pa.field("period_start", pa.date32(), False),
    pa.field("identity_episode_id", pa.string()),
]
FEATURE_SCHEMA = pa.schema(
    [
        *KEY_SCHEMA,
        pa.field("eligible", pa.bool_(), False),
        pa.field("exclusion_reason", pa.string(), False),
        *[
            pa.field(
                name,
                pa.int64()
                if name.endswith("_ore") or name in {"history_periods", "lifecycle_age_periods"}
                else pa.float64(),
            )
            for name in NUMERICS
        ],
        *[pa.field(name, pa.string()) for name in CATEGORIES],
        pa.field("provenance", pa.string(), False),
    ]
)
LABEL_SCHEMA = pa.schema(
    [
        *KEY_SCHEMA,
        pa.field("next_status", pa.string(), False),
        pa.field("next_maturity_index", pa.int32(), False),
        pa.field("next_maturity_date", pa.date32()),
        pa.field("next_aip_ore", pa.int64()),
        pa.field("next_delta_ore", pa.int64()),
        pa.field("next_direction", pa.int8()),
        pa.field("reversal_status", pa.string(), False),
        pa.field("reversal_maturity_index", pa.int32()),
        pa.field("reference_aip_ore", pa.int64()),
        pa.field("reversal_maturity_date", pa.date32()),
        pa.field("horizon1_aip_ore", pa.int64()),
        pa.field("horizon2_aip_ore", pa.int64()),
        pa.field("reversal_category", pa.string()),
    ]
)


RAW_STRINGS = (
    "package_number",
    "canonical_identity",
    "drug_id",
    "price_state",
    "tax_basis",
    "fee_basis",
    "currency",
    "price_basis",
    "price_role",
    "abc",
    "group_code",
    "classification_state",
    "atc",
    "form_code",
    "strength",
    "strength_unit",
    "pack_size_token",
    "pack_unit",
    "dispensing_code",
    "package_distributor",
    "provenance",
)
RAW_SCHEMA = pa.schema(
    [
        pa.field("period_start", pa.date32(), False),
        *[
            pa.field(name, pa.string(), name not in {"package_number", "provenance"})
            for name in RAW_STRINGS
        ],
        pa.field("aip_ore", pa.int64()),
        pa.field("creation_date", pa.date32()),
        pa.field("exit_date", pa.date32()),
    ]
)


ALLOWED_DISPENSING = {
    "A",
    "AP",
    "AP4",
    "AP4BG",
    "AP4NB",
    "APK",
    "B",
    "BPK",
    "GH",
    "HA",
    "HA18",
    "NBS",
}


def data_dictionary() -> dict[str, Any]:
    """Deterministic descriptions of the exact frozen schema and predictor boundary."""
    ore = "DKK ore per package; 100 ore = 1 DKK; VAT and consumer dispensing fees excluded"
    feature_meanings = {
        "aip_ore": (
            ore,
            "Current own AIP source assertion.",
            "Unavailable source amount; literal zero is retained but origin is excluded.",
        ),
        "lag1_delta_ore": (
            ore,
            "Current AIP minus the immediately preceding eligible AIP in this episode.",
            "No immediately preceding eligible episode observation.",
        ),
        "lag2_delta_ore": (
            ore,
            "Previous eligible AIP minus the eligible AIP two periods before origin.",
            "Fewer than two preceding eligible episode observations.",
        ),
        "lag3_delta_ore": (
            ore,
            "AIP two periods before origin minus AIP three periods before origin.",
            "Fewer than three preceding eligible episode observations.",
        ),
        "history_periods": (
            "tariff observations",
            (
                "Number of preceding contiguous eligible observations in the episode; "
                "zero at entry or excluded origin."
            ),
            "Not used; the producer emits a count.",
        ),
        "past_rise_frequency": (
            "fraction in [0,1]",
            (
                "Fraction of observed strict rises among episode AIP changes through "
                "the current origin."
            ),
            "No observed episode AIP changes.",
        ),
        "past_fall_frequency": (
            "fraction in [0,1]",
            (
                "Fraction of observed strict falls among episode AIP changes through "
                "the current origin."
            ),
            "No observed episode AIP changes.",
        ),
        "past_flat_frequency": (
            "fraction in [0,1]",
            (
                "Fraction of exact unchanged amounts among episode AIP changes through "
                "the current origin."
            ),
            "No observed episode AIP changes.",
        ),
        "lifecycle_age_periods": (
            "actual calendar tariff intervals",
            (
                "Origin calendar index minus creation-date calendar index; no "
                "interpolation of creation dates."
            ),
            (
                "Excluded origin or creation does not bind to an admitted calendar "
                "start; older creation is left-censored."
            ),
        ),
        "abc": (
            "native ordinary ABC category",
            "Current ordinary same-edition classification; directly disputed values suppressed.",
            "Missing, unsupported, ambiguous, or scoped source inequality; never no-change.",
        ),
        "group_state": (
            "declared uncertainty category",
            "State of ordinary ABC group evidence; no peer population count or TSP feature.",
            (
                "Not used; observed/unobserved/ambiguous/scoped-uncertainty states are "
                "explicit strings."
            ),
        ),
        "atc": (
            "native ATC code",
            "Historical same-edition drug ATC, without later backfill.",
            "Missing, unsupported or conflicted drug attribute.",
        ),
        "form_code": (
            "native form code",
            "Historical same-edition drug formulation code.",
            "Missing, unsupported or conflicted drug attribute.",
        ),
        "strength": (
            "native strength text",
            "Historical same-edition strength description; no numeric scale inferred.",
            "Missing, unsupported or conflicted drug attribute.",
        ),
        "strength_unit": (
            "native strength-unit token",
            "Historical same-edition strength unit; no unit conversion.",
            "Missing, unsupported or conflicted drug attribute.",
        ),
        "pack_size_token": (
            "native lexical package-size token",
            "Historical package size token, preserving leading zeros; no numeric scale inferred.",
            "Missing, unsupported or conflicted package attribute.",
        ),
        "pack_unit": (
            "native package-unit code",
            "Historical same-edition package unit; no unit conversion.",
            "Missing, unsupported or conflicted package attribute.",
        ),
        "dispensing_code": (
            "native dispensing code",
            (
                "Historical same-edition dispensing context; supported values also "
                "determine eligibility."
            ),
            "Missing, unsupported or conflicted package attribute; origin excluded.",
        ),
        "package_distributor": (
            "native distributor code",
            "Historical same-edition package distributor; no current company-name backfill.",
            "Missing, unsupported or conflicted package attribute.",
        ),
        "supply_state": (
            "declared availability category",
            "Always unavailable_join_unestablished; absent reports never mean no disruption.",
            "Not used; unknown supply is represented by the explicit state.",
        ),
    }
    auxiliary = {
        "row_key": (
            "SHA256 of final-phase package and actual period-start key; auxiliary join identity."
        ),
        "package_number": "Six ASCII digits with leading zeros preserved; auxiliary package key.",
        "period_index": (
            "Zero-based index into the actual frozen calendar, not an elapsed fortnight estimate."
        ),
        "period_start": "Actual tariff start date from the selected calendar.",
        "identity_episode_id": (
            "Causal episode key; gaps, ineligibility and native identity changes split continuity."
        ),
        "eligible": (
            "Whether this origin satisfies the fixed-price pharmacy "
            "AIP/context/identity/lifecycle policy."
        ),
        "exclusion_reason": (
            "Empty string means eligible; otherwise the explicit origin exclusion reason."
        ),
        "provenance": (
            "JSON string of package/drug/price import IDs, members, source locators"
            " and corroborating imports; not a predictor."
        ),
    }
    label_meanings = {
        "next_status": (
            "status",
            "observed, pending, gap, ineligible, identity_change, or origin_ineligible",
            "Required explicit state.",
        ),
        "next_maturity_index": (
            "calendar index",
            "Origin index + 1, including pending or excluded origins.",
            "Required integer; may exceed the frozen calendar tail.",
        ),
        "next_maturity_date": (
            "calendar date",
            "Actual start of origin + 1 tariff period.",
            "Horizon is beyond the frozen calendar.",
        ),
        "next_aip_ore": (
            ore,
            "Own AIP at the next actual tariff period, within the same eligible episode.",
            "Next target is not observed.",
        ),
        "next_delta_ore": (
            ore,
            "Next own AIP minus origin own AIP, using exact integers.",
            "Next target is not observed.",
        ),
        "next_direction": (
            "integer sign {-1,0,1}",
            "Sign of exact next_delta_ore with zero tolerance.",
            "Next target is not observed.",
        ),
        "reversal_status": (
            "status",
            "observed, pending, gap, ineligible, identity_change, or not_observed_rise",
            "Required explicit state.",
        ),
        "reversal_maturity_index": (
            "calendar index",
            "Origin index + 2 for an already observed strict eligible rise.",
            "Origin is not an observed strict rise.",
        ),
        "reference_aip_ore": (
            ore,
            (
                "Immediately pre-rise own AIP in the same episode, retained even when "
                "the future horizon is unavailable."
            ),
            "Origin is not an observed strict rise.",
        ),
        "reversal_maturity_date": (
            "calendar date",
            "Actual start of origin + 2 tariff period.",
            "Not an observed rise or horizon is beyond the frozen calendar.",
        ),
        "horizon1_aip_ore": (
            ore,
            (
                "First following own AIP; populated only when both following periods "
                "are observed in the same eligible episode."
            ),
            "Both-period comparable horizon is unavailable or origin is not an observed rise.",
        ),
        "horizon2_aip_ore": (
            ore,
            (
                "Second following own AIP; populated only when both following periods "
                "are observed in the same eligible episode."
            ),
            "Both-period comparable horizon is unavailable or origin is not an observed rise.",
        ),
        "reversal_category": (
            "category",
            (
                "Minimum of both following prices: full_reversal <= pre-rise reference;"
                " partial_reversal < rise price but > reference; otherwise persistence."
            ),
            "Comparable reversal target is not observed.",
        ),
    }

    def schema_fields(schema: Any) -> list[dict[str, Any]]:
        return [
            {"name": field.name, "type": str(field.type), "nullable": field.nullable}
            for field in schema
        ]

    return {
        "contract": VERSION,
        "grain": (
            "One final-phase canonical package observation at an actual tariff "
            "start; excluded origins retained."
        ),
        "features": [
            {
                "name": name,
                "type": str(FEATURE_SCHEMA.field(name).type),
                "nullable": FEATURE_SCHEMA.field(name).nullable,
                "kind": "categorical" if name in CATEGORIES else "numeric",
                "units": feature_meanings[name][0],
                "meaning": feature_meanings[name][1],
                "null_meaning": feature_meanings[name][2],
            }
            for name in FEATURES
        ],
        "auxiliary_fields": [
            {
                "name": name,
                "type": str(FEATURE_SCHEMA.field(name).type),
                "nullable": FEATURE_SCHEMA.field(name).nullable,
                "predictor": False,
                "meaning": meaning,
            }
            for name, meaning in auxiliary.items()
        ],
        "labels": [
            {
                "name": name,
                "type": str(LABEL_SCHEMA.field(name).type),
                "nullable": LABEL_SCHEMA.field(name).nullable,
                "predictor": False,
                "units": meaning[0],
                "meaning": meaning[1],
                "null_meaning": meaning[2],
            }
            for name, meaning in label_meanings.items()
        ],
        "schemas": {
            "features": schema_fields(FEATURE_SCHEMA),
            "labels": schema_fields(LABEL_SCHEMA),
            "source": schema_fields(RAW_SCHEMA),
        },
        "predictor_policy": (
            "Only ordered features are predictors; keys, eligibility, exclusions, "
            "provenance, all raw source fields and all future labels remain outside"
            " the model feature matrix."
        ),
        "horizons": {
            "next": 1,
            "observed_rise_reversal": 2,
            "unit": (
                "actual calendar tariff periods; never bridge missing or ineligible "
                "observations or identity changes"
            ),
            "training_maturity": (
                "Observed target and actual maturity date strictly earlier than mature_before."
            ),
        },
        "conversion_policy": {
            "model_neutral": (
                "Parquet preserves exact integer ore, native categorical strings and "
                "nulls; no fitted preprocessing."
            ),
            "categorical_adapter": (
                "None -> M:; any native string s -> V:+s, preserving empty strings and "
                "literal sentinels injectively."
            ),
            "numeric_adapter": (
                "Predictors -> float64; numerical null -> NaN, never zero imputation. "
                "Native nine-digit price values are exactly representable; arbitrary "
                "integers above 2**53 may not be."
            ),
            "label_adapter": (
                "CatBoost numeric labels -> float32 and verified after conversion; "
                "categorical labels remain class strings. Exact labels remain in keyed "
                "Parquet and PoolInput.exact_labels."
            ),
            "learning_boundary": (
                "Any learned preprocessing belongs inside later chronological training "
                "folds; this bundle fits no model or transformation."
            ),
        },
    }


def exclusion_reason(row: dict[str, Any]) -> str:
    if not row["canonical_identity"] or not row["drug_id"]:
        return "unresolved_identity"
    if row["price_state"] != "present":
        return "aip_" + (row["price_state"] or "missing")
    if tuple(
        row[x] for x in ("currency", "tax_basis", "fee_basis", "price_basis", "price_role")
    ) != (
        "DKK",
        "excluding_vat",
        "excluding_consumer_dispensing_fees",
        "per_package",
        "effective_price",
    ):
        return "unsupported_aip_context"
    if type(row["aip_ore"]) is not int or row["aip_ore"] <= 0:
        return "nonpositive_aip"
    if not row["creation_date"] or row["creation_date"] > row["period_start"]:
        return "creation_unestablished_at_period"
    if row["exit_date"] and row["exit_date"] != date(1900, 1, 1):
        return "recorded_exit"
    code = row["dispensing_code"]
    if code == "BEGR":
        return "hospital_scope_excluded"
    if code not in ALLOWED_DISPENSING:
        return "unsupported_or_nonfixed_dispensing_context"
    return ""


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".training-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def pin(path: Path, root: Path, role: str, rows: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": path.relative_to(root).as_posix(),
        "sha256": digest(path),
        "size_bytes": path.stat().st_size,
        "role": role,
    }
    if rows is not None:
        result["rows"] = rows
    return result


def validate_calendar(calendar: Any) -> list[dict[str, Any]]:
    from datetime import date

    if not isinstance(calendar, list) or not calendar:
        raise ResearchError("training_calendar_required")
    previous = None
    for index, period in enumerate(calendar):
        start = date.fromisoformat(period["start"])
        end = date.fromisoformat(period["end_exclusive"])
        if period["index"] != index or start >= end or (previous and start != previous):
            raise ResearchError("invalid_training_calendar")
        previous = end
    return calendar


def row_key(package: str, period_start: str) -> str:
    return fingerprint({"package": package, "period_start": period_start, "phase": "final"})
