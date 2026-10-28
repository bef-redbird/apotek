"""Lossless assertions between versioned source readers and the publication boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any


class AdapterError(ValueError):
    """A safe code describing a fatal source/schema/context failure."""

    def __init__(self, code: str, source_locator: str = "file") -> None:
        self.code = code
        self.source_locator = source_locator
        super().__init__(code)


@dataclass(frozen=True)
class Assertion:
    table: str
    source_locator: str
    source_identifier: str
    values: dict[str, Any]
    presence: dict[str, str]
    raw: dict[str, Any]
    effective_period: date | None = None
    report_period: date | None = None
    effective_date: date | None = None
    quality_state: str = "valid"


@dataclass(frozen=True)
class Problem:
    source_locator: str
    code: str
    blocking_scope: str = "field"
    identifier: str | None = None
    field: str | None = None
    period: date | None = None
    severity: str = "error"


@dataclass
class Parsed:
    assertions: list[Assertion] = field(default_factory=list)
    issues: list[Problem] = field(default_factory=list)
    records: int = 0


def identifier(value: Any) -> str:
    """Canonicalize integer-like spreadsheet IDs without changing source representations."""
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid_identifier")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,6}(?:\.0+)?", text):
        raise ValueError("invalid_identifier")
    return text.split(".")[0].zfill(6)


def money(value: Any) -> int | None:
    """Exact Danish kroner to signed BIGINT øre; no floating point noise allowance."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("malformed_money")
    try:
        if isinstance(value, str):
            if not re.fullmatch(
                r"-?(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+)(?:,[0-9]+)?", value.strip()
            ):
                raise ValueError("malformed_money")
            amount = Decimal(value.strip().replace(".", "").replace(",", "."))
        else:
            amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError("nonfinite_money")
        if amount < 0:
            raise ValueError("negative_money")
        # Decimal arithmetic uses a process context; increase precision before scaling
        # so a long higher-precision token cannot be rounded into valid integer øre.
        with localcontext() as ctx:
            ctx.prec = max(28, len(amount.as_tuple().digits) + 3)
            cents = amount * 100
        if cents != cents.to_integral_value():
            raise ValueError("unsupported_money_precision")
        if cents > 9223372036854775807:
            raise ValueError("money_overflow")
        return int(cents)
    except InvalidOperation as exc:
        raise ValueError("malformed_money") from exc


def field_state(value: Any, present: bool = True) -> str:
    if not present:
        return "unreported"
    if value is None or (isinstance(value, str) and not value.strip()):
        return "blank"
    return "present"
