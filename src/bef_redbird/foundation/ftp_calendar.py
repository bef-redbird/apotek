"""Apply an explicitly reviewed calendar through the existing Store contract.

Source dates, published cadence and the review's exact anchors belong in the
archived review document. This helper never derives dates from a file name,
server clock, ISO week number or download order.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .records import canonical_json, digest

if TYPE_CHECKING:
    from .store import Store


def apply_reviewed_calendar(store: Store, path: Path) -> dict[str, Any]:
    store._check_owner()
    document = json.loads(path.read_text("utf-8"))
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "kind", "rationale", "evidence", "periods"}
        or document["version"] != 1
        or document["kind"] != "reviewed-price-calendar"
        or not isinstance(document["rationale"], str)
        or not document["rationale"].strip()
        or not isinstance(document["periods"], list)
        or not 0 < len(document["periods"]) <= 2000
        or not isinstance(document["evidence"], list)
        or not document["evidence"]
    ):
        raise ValueError("Explicit reviewed calendar and evidence required")
    evidence_ids = set()
    for item in document["evidence"]:
        if (
            set(item) != {"delivery_id", "sha256", "source_locator"}
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
            or not isinstance(item["source_locator"], str)
            or not item["source_locator"].strip()
        ):
            raise ValueError("Located original calendar evidence required")
        delivery_id = str(UUID(item["delivery_id"]))
        if delivery_id in evidence_ids:
            raise ValueError("Duplicate calendar evidence")
        evidence_ids.add(delivery_id)
        with store.original_path(delivery_id).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Calendar evidence bytes changed")
    periods = []
    for item in document["periods"]:
        if set(item) != {"start", "end_exclusive", "evidence_delivery_ids"}:
            raise ValueError("Explicit half-open periods required")
        start, end = date.fromisoformat(item["start"]), date.fromisoformat(item["end_exclusive"])
        pins = item["evidence_delivery_ids"]
        if (
            start.isoformat() != item["start"]
            or end.isoformat() != item["end_exclusive"]
            or start >= end
            or not isinstance(pins, list)
            or not pins
            or len(set(pins)) != len(pins)
            or not set(pins) <= evidence_ids
        ):
            raise ValueError("Invalid or unevidenced calendar period")
        periods.append((start, end))
    periods.sort()
    for index, (start, end) in enumerate(periods):
        if index and periods[index - 1][1] > start:
            raise ValueError("Reviewed periods overlap")
        overlapping = store._connection.execute(
            "SELECT period_start,period_end_exclusive FROM price_period "
            "WHERE period_start<? AND period_end_exclusive>?",
            [end, start],
        ).fetchall()
        if overlapping and overlapping != [(start, end)]:
            raise ValueError("Reviewed calendar conflicts with accepted periods")
    # Validate every entry and every original before adding anything. Each valid
    # insertion uses the established idempotent Store transaction.
    calendar_hash = digest(document)
    delivery = store.register_source(
        path,
        source="foundation:reviewed-price-calendar",
        receipt_key=calendar_hash,
        original_uri="reviewed-calendar:" + calendar_hash,
    )
    for start, end in periods:
        store.add_period(start, end)
    return {
        "complete": True,
        "review_delivery_id": delivery,
        "configuration_hash": calendar_hash,
        "reviewed_period_count": len(periods),
        "first_start": periods[0][0].isoformat(),
        "last_end_exclusive": periods[-1][1].isoformat(),
        "evidence_delivery_ids": sorted(evidence_ids),
        "availability": "No historical publication or possession inferred",
        "configuration": json.loads(canonical_json(document)),
    }
