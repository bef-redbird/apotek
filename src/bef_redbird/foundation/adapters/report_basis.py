"""Explicit AIP-only interpretation scope; validation is not evidence of meaning.

The caller supplies reviewed source evidence and its applicable period bounds.
Archive integrity and decision-time availability require their separate gates.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from typing import Any
from uuid import UUID

from .base import AdapterError, Parsed

BASIS_OVERRIDE_KEY = "price_basis_override"


def validated_basis_override(context: dict[str, Any]) -> dict[str, Any]:
    """Validate the version-one scope schema, never the source's asserted meaning."""
    profile = context.get(BASIS_OVERRIDE_KEY)
    fixed = {
        "price_type": "aip",
        "price_role": "effective_price",
        "currency": "DKK",
        "price_basis": "per_package",
        "tax_basis": "excluded",
        "fee_basis": "excluded",
    }
    fields = {
        "version",
        *fixed,
        "effective_period_start_from",
        "effective_period_start_through",
        "evidence_delivery_id",
        "evidence_sha256",
        "evidence_locator",
    }
    if (
        not isinstance(profile, dict)
        or set(profile) != fields
        or type(profile["version"]) is not int
        or profile["version"] != 1
        or any(profile[key] != value for key, value in fixed.items())
        or context.get("tax_basis") != "unknown"
        or context.get("fee_basis") != "unknown"
    ):
        raise AdapterError("invalid_price_basis_override")
    try:
        bounds = []
        for key in ("effective_period_start_from", "effective_period_start_through"):
            value = profile[key]
            if not isinstance(value, str):
                raise ValueError
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            bounds.append(parsed)
        evidence_id = profile["evidence_delivery_id"]
        evidence_hash = profile["evidence_sha256"]
        locator = profile["evidence_locator"]
        if (
            bounds[0] > bounds[1]
            or not isinstance(evidence_id, str)
            or str(UUID(evidence_id)) != evidence_id
            or not isinstance(evidence_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence_hash) is None
            or not isinstance(locator, str)
            or not locator.strip()
        ):
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise AdapterError("invalid_price_basis_override") from exc
    return dict(profile)


def with_basis_override(parsed: Parsed, profile: dict[str, Any]) -> Parsed:
    """Change basis strings only for current AIP inside inclusive period-start bounds."""
    start = date.fromisoformat(profile["effective_period_start_from"])
    through = date.fromisoformat(profile["effective_period_start_through"])
    return Parsed(
        assertions=[
            replace(
                assertion,
                values={**assertion.values, "tax_basis": "excluded", "fee_basis": "excluded"},
            )
            if assertion.table == "price_observation"
            and assertion.values.get("price_type") == "aip"
            and assertion.values.get("price_role") == "effective_price"
            and assertion.values.get("currency") == "DKK"
            and assertion.values.get("price_basis") == "per_package"
            and assertion.effective_period is not None
            and start <= assertion.effective_period <= through
            else assertion
            for assertion in parsed.assertions
        ],
        issues=parsed.issues,
        records=parsed.records,
    )
