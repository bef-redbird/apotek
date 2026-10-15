"""Strict input boundaries; Decimal conversion accepts at most 1e-7 øre noise."""

import math
import re
from decimal import Decimal, InvalidOperation

from bef_redbird.domain import DataError


def product_id(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise DataError("Varenummer skal være højst seks cifre.")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,6}(?:\.0+)?", text):
        raise DataError("Varenummer skal være et helt, ikke-negativt tal med højst seks cifre.")
    return text.split(".")[0].zfill(6)


def group_id(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise DataError("Ugyldigt substitutionsgruppenummer.")
    if isinstance(value, (float, int)):
        if not math.isfinite(value) or value < 0 or int(value) != value:
            raise DataError("Ugyldigt substitutionsgruppenummer.")
        return str(int(value))
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,32}(?:\.0+)?", text):
        raise DataError("Ugyldigt substitutionsgruppenummer.")
    return text.split(".")[0]


def price_ore(value: object) -> int | None:
    """Numeric cells or Danish text: 1234 / 1234,56 / 1.234,56; no dot-decimal text."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise DataError("Pris er ikke et gyldigt beløb.")
    try:
        if isinstance(value, str):
            text = value.strip()
            if not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+)(?:,[0-9]+)?", text):
                raise DataError("Tekstpriser skal bruge dansk talformat, fx 1.234,56.")
            amount = Decimal(text.replace(".", "").replace(",", "."))
        else:
            amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000"):
            raise DataError(
                "Pris skal være et endeligt, ikke-negativt beløb inden for beløbsgrænsen."
            )
        cents = amount * 100
        integral = cents.to_integral_value()
        if abs(cents - integral) > Decimal("0.0000001"):
            raise DataError("Pris indeholder mere end to betydende decimaler.")
        return int(integral)
    except (InvalidOperation, TypeError, ValueError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("Pris er ikke et gyldigt beløb.") from exc


def normal_header(value: object) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return "nysubkode" if text == "ny subkode" else text
