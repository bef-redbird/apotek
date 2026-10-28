from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation.ftp_calendar import apply_reviewed_calendar


def review(store: Store, root: Path) -> tuple[Path, dict]:
    source = root / "published-calendar.txt"
    source.write_text("Synthetic independently supplied half-open dates", encoding="utf-8")
    delivery = store.register_source(source, source="synthetic", receipt_key="calendar")
    config = {
        "version": 1,
        "kind": "reviewed-price-calendar",
        "rationale": "Explicit synthetic fixture dates, not source filename dates",
        "evidence": [
            {
                "delivery_id": delivery,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_locator": "line:1",
            }
        ],
        "periods": [
            {
                "start": "2026-10-26",
                "end_exclusive": "2026-11-09",
                "evidence_delivery_ids": [delivery],
            },
            {
                "start": "2026-11-09",
                "end_exclusive": "2026-11-23",
                "evidence_delivery_ids": [delivery],
            },
        ],
    }
    path = root / "review.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path, config


def test_reviewed_calendar_retry_and_native_bounds(tmp_path: Path) -> None:
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        path, _ = review(store, tmp_path)
        first = apply_reviewed_calendar(store, path)
        assert apply_reviewed_calendar(store, path) == first
        assert store._connection.execute("SELECT count(*) FROM price_period").fetchone() == (2,)
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (2,)
        assert first["last_end_exclusive"] == "2026-11-23"


@pytest.mark.parametrize("failure", ["overlap", "evidence", "conflict", "no_pin"])
def test_calendar_rejects_whole_plan_before_adding_dates(tmp_path: Path, failure: str) -> None:
    with Store(tmp_path / "db.duckdb", tmp_path / "archive") as store:
        path, config = review(store, tmp_path)
        before = 0
        if failure == "overlap":
            config["periods"][1]["start"] = "2026-11-08"
        elif failure == "evidence":
            config["evidence"][0]["sha256"] = "0" * 64
        elif failure == "no_pin":
            config["periods"][1]["evidence_delivery_ids"] = []
        else:
            store.add_period(date(2026, 11, 9), date(2026, 11, 24))
            before = 1
        path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(ValueError):
            apply_reviewed_calendar(store, path)
        assert store._connection.execute("SELECT count(*) FROM price_period").fetchone() == (
            before,
        )
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (1,)
