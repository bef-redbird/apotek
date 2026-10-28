"""Small explicit metadata contracts shared by registration and future adapters."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5


class FoundationError(Exception):
    """An operation could not preserve the foundation contract."""


class ArchiveError(FoundationError):
    """An original is missing, corrupt or cannot be safely archived."""


class WriterBusy(FoundationError):
    """Another owner holds the local writer lock."""


def required(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected nonempty text")
    return value


def canonical_identifier(namespace: str, value: str) -> str:
    required(namespace)
    required(value)
    if namespace == "dk:varenummer":
        if not re.fullmatch(r"[0-9]{1,6}", value):
            raise ValueError("Varenummer must be one to six ASCII digits supplied as text")
        return value.zfill(6)
    return value


def canonical_json(value: dict[str, Any]) -> str:
    """Reject non-JSON values, non-string keys and nonfinite numbers."""

    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Expected JSON metadata")

    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    validate(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: dict[str, Any]) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def identity(kind: str, value: dict[str, Any]) -> str:
    return str(uuid5(NAMESPACE_URL, "bef-redbird/foundation/v1/" + kind + "/" + digest(value)))


def manifest(value: dict[str, Any] | None) -> str:
    value = {"version": 1, "kind": "unknown"} if value is None else value
    if type(value.get("version")) is not int or value["version"] < 1:
        raise ValueError("Manifest requires a positive integer version")
    return canonical_json(value)


@dataclass(frozen=True)
class Timing:
    """Evidence bounds, not a guessed instant. Unknown is represented by no bounds.

    Non-instant upper bounds are conservative latest-possible availability bounds.
    Callers supply timezone and precision; registration never reads file mtime.
    """

    lower: datetime | None = None
    upper: datetime | None = None
    precision: str = "unknown"
    basis: str | None = None
    source_timezone: str | None = None

    def values(self) -> tuple[datetime | None, datetime | None, str, str | None, str | None]:
        if self.precision == "unknown":
            if self.lower is not None or self.upper is not None:
                raise ValueError("Unknown timing cannot have timestamp bounds")
        else:
            if self.precision not in {"instant", "day", "range"}:
                raise ValueError("Unsupported timing precision")
            if self.lower is None or self.upper is None:
                raise ValueError("Known timing requires both bounds")
            if self.lower.utcoffset() is None or self.upper.utcoffset() is None:
                raise ValueError("Timing bounds require timezones")
            required(self.basis or "")
            required(self.source_timezone or "")
            if self.lower > self.upper:
                raise ValueError("Timing bounds are reversed")
            if (self.lower == self.upper) != (self.precision == "instant"):
                raise ValueError("Only instant precision has equal bounds")
        return (
            self.lower.astimezone(UTC) if self.lower else None,
            self.upper.astimezone(UTC) if self.upper else None,
            self.precision,
            self.basis,
            self.source_timezone,
        )

    def metadata(self) -> list[str | None]:
        return [
            value.isoformat() if isinstance(value, datetime) else value for value in self.values()
        ]
