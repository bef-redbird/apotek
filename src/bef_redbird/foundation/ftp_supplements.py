"""Scoped native FTP supplements; these facts do not describe wholesale supply.

DOK is a conditional reimbursement-exception report. Its comma-separated prose
does not delimit name/form/strength reliably, so that portion remains one source
description. LMS36/37 are prescribing-permission and reference assertions. Neither
directory names nor filenames supply dates. Unknown layouts remain raw-only.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from datetime import datetime
from typing import Any

VERSION = "ftp-supplements-v1"
_HEADERS = {
    "LMS36": ["DrugID", "UdvidetOrdinationskode", "GældendeFra", "GældendeTil"],
    "LMS37": ["UdvidetOrdinationskode", "Faggruppe", "Specialenavn"],
}
_DOK_COLUMNS = "Varenummer, navn, form, styrke, pakningsstoerrelse, ATC-kode"
_DOK_CONDITION = (
    "Ved substitution udleveres det billigste laegemiddel i en substitutionsgruppe "
    "(A-produktet). Hvis apoteket paa grund af leveringssvigt "
    "ikke kan skaffe det billigste laegemiddel i gruppen, vil der den "
    "{effective} ogsaa vaere fuldt tilskud til nedenstaaende varenumre, som nu er "
    "bestemmende for tilskudsprisen i deres respektive tilskudsgrupper paa trods af, "
    "at de ikke er gruppens billigste."
)


def _date(token: str, form: str) -> str:
    pattern = r"[0-9]{8}" if form == "%Y%m%d" else r"[0-9]{2}-[0-9]{2}-[0-9]{4}"
    if not re.fullmatch(pattern, token):
        raise ValueError("Date does not have the documented lexical format")
    return datetime.strptime(token, form).date().isoformat()


def _issue(code: str, line: int | None = None) -> dict[str, Any]:
    return {"code": code, "source_line": line}


def _lines(data: bytes) -> list[tuple[int, int, bytes]]:
    """Physical line, byte offset, content without CR/LF; never strip other bytes."""
    result = []
    offset = 0
    for number, raw in enumerate(data.splitlines(keepends=True), 1):
        result.append((number, offset, raw.removesuffix(b"\n").removesuffix(b"\r")))
        offset += len(raw)
    return result


def parse_supplement(data: bytes, kind: str) -> dict[str, Any]:
    """Return native source assertions, scoped issues and explicit coverage.

    Invalid source headers/dates/encoding are raw-only. Invalid individual data
    rows are quarantined with physical locators, yielding partial coverage.
    All duplicate source assertions are retained as distinct located records.
    """
    if kind not in ("DOK", "LMS36", "LMS37"):
        raise ValueError("Unsupported supplementary source kind")
    records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "version": VERSION,
        "kind": kind,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "availability": "unknown",
        "issue_date": None,
        "effective_date": None,
        "supply_evidence": False,
    }
    lines = _lines(data)
    candidates: list[tuple[int, int, bytes]] = []
    if kind == "DOK":
        # No exact OEM page is proved. Only text identical under both observed
        # candidate pages is admitted; unsupported differing bytes stay raw-only.
        metadata.update(
            encoding="cp865",
            encoding_caveat="Exact OEM page unproven; cp850/cp865 must agree on these bytes",
            semantics="conditional_reimbursement_exception",
            description_fields="unsplit_name_form_strength_package_description",
            negative_evidence=False,
        )
        if data.decode("cp865") != data.decode("cp850"):
            issues.append(_issue("ambiguous_oem_text"))
        else:
            decoded = [raw.decode("cp865") for _, _, raw in lines]
            try:
                issue_match = re.fullmatch(
                    r"Laegemiddelstyrelsen, den ([0-9]{2}-[0-9]{2}-[0-9]{4})", decoded[0]
                )
                title_match = re.fullmatch(
                    r"Aendringer i tilskudspriser per ([0-9]{2}-[0-9]{2}-[0-9]{4})",
                    decoded[3],
                )
                if not issue_match or not title_match:
                    raise ValueError("Unrecognized DOK dates/header")
                metadata["issue_date"] = _date(issue_match[1], "%d-%m-%Y")
                metadata["effective_date"] = _date(title_match[1], "%d-%m-%Y")
                if decoded[1:3] != ["", ""] or decoded[4] or decoded[8]:
                    raise ValueError("Unsupported DOK header layout")
                condition = " ".join(" ".join(decoded[5:8]).split())
                if condition != _DOK_CONDITION.format(effective=title_match[1]):
                    raise ValueError("Unsupported DOK condition")
                if decoded[9].strip() != _DOK_COLUMNS or decoded[10]:
                    raise ValueError("Unsupported DOK columns")
                candidates = lines[11:]
            except (ValueError, IndexError):
                issues.append(_issue("unsupported_dok_header_or_date"))
        for number, offset, raw in candidates:
            text = raw.decode("cp865")
            match = re.fullmatch(r"([0-9]{6}), (.+), ([A-Z][0-9]{2}[A-Z]{2}[0-9]{2})", text)
            if not match:
                issues.append(_issue("invalid_dok_record", number))
                continue
            records.append(
                {
                    "kind": kind,
                    "source_line": number,
                    "byte_offset": offset,
                    "byte_length": len(raw),
                    "package_code": match[1],
                    "source_description": match[2],
                    "atc_code": match[3],
                    "issue_date": metadata["issue_date"],
                    "effective_date": metadata["effective_date"],
                    "assertion": "conditional_reimbursement_exception",
                }
            )
    else:
        metadata.update(
            encoding="cp1252",
            encoding_caveat="Observed bytes also compatible with ISO-8859-1; exact page unproven",
            semantics="prescribing_permission" if kind == "LMS36" else "prescribing_reference",
            blank_end_date="unknown" if kind == "LMS36" else None,
        )
        try:
            decoded_lines = [raw.decode("cp1252", errors="strict") for _, _, raw in lines]
            headers = next(csv.reader([decoded_lines[0]], delimiter=";", strict=True))
            if headers != _HEADERS[kind]:
                raise ValueError("Unsupported supplement columns")
            candidates = lines[1:]
        except (UnicodeError, csv.Error, IndexError, StopIteration, ValueError):
            issues.append(_issue("unsupported_csv_header_or_encoding"))
        for number, offset, raw in candidates:
            try:
                fields = next(csv.reader([raw.decode("cp1252")], delimiter=";", strict=True))
                if len(fields) != len(_HEADERS[kind]) or any(x != x.strip() for x in fields):
                    raise ValueError("Unsupported CSV field count/whitespace")
                if not fields[0] or not fields[1]:
                    raise ValueError("Missing required identifier/text")
                record: dict[str, Any] = {
                    "kind": kind,
                    "source_line": number,
                    "byte_offset": offset,
                    "byte_length": len(raw),
                }
                if kind == "LMS36":
                    if not re.fullmatch(r"[0-9]{11}", fields[0]):
                        raise ValueError("Invalid drug ID")
                    valid_from = _date(fields[2], "%Y%m%d")
                    valid_to = _date(fields[3], "%Y%m%d") if fields[3] else None
                    if valid_to is not None and valid_to < valid_from:
                        raise ValueError("Reversed validity interval")
                    record.update(
                        drug_id=fields[0],
                        extended_prescribing_code=fields[1],
                        valid_from=valid_from,
                        valid_to=valid_to,
                        assertion="prescribing_permission",
                    )
                else:
                    record.update(
                        extended_prescribing_code=fields[0],
                        professional_group=fields[1],
                        specialty_name=fields[2] or None,
                        assertion="prescribing_reference",
                    )
                records.append(record)
            except (ValueError, UnicodeError, csv.Error, StopIteration):
                issues.append(_issue("invalid_csv_record", number))

    keys = [
        tuple((key, value) for key, value in record.items() if key not in _LOCATORS)
        for record in records
    ]
    metadata["duplicate_record_count"] = sum(count - 1 for count in Counter(keys).values())
    if kind == "DOK":
        metadata["distinct_package_count"] = len({row["package_code"] for row in records})
        metadata["duplicate_package_count"] = sum(
            count - 1 for count in Counter(row["package_code"] for row in records).values()
        )
    status = "raw_only" if not candidates and issues else "partial" if issues else "complete"
    return {
        "records": records,
        "issues": issues,
        "metadata": metadata,
        "coverage": {
            "status": status,
            "candidate_rows": len(candidates),
            "accepted_rows": len(records),
            "rejected_rows": len(candidates) - len(records),
            "scope": metadata["semantics"],
            "absence_means": "unknown",
        },
    }


_LOCATORS = {"source_line", "byte_offset", "byte_length"}


def _reference_cells(raw: bytes) -> list[str]:
    """Independent, single-native-line CSV state machine (not csv.reader)."""
    text = raw.decode("cp1252", errors="strict")
    fields: list[str] = []
    cell = ""
    quoted = False
    closed = False
    index = 0
    while index < len(text):
        char = text[index]
        if quoted:
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    cell += '"'
                    index += 1
                else:
                    quoted = False
                    closed = True
            else:
                cell += char
        elif char == ";":
            fields.append(cell)
            cell = ""
            closed = False
        elif char == '"' and not cell and not closed:
            quoted = True
        elif char == '"' or closed:
            raise ValueError("Invalid quoting")
        else:
            cell += char
        index += 1
    if quoted:
        raise ValueError("Unclosed quote")
    return [*fields, cell]


def independent_audit(data: bytes, kind: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-read original lines independently, rejecting omitted/altered/extra rows.

    No importing parser, line locator, date converter or header constant supplies
    the expectations. Header/layout failures cannot earn an acceptance result.
    No source values appear in the returned diagnostics.
    """
    if kind not in ("DOK", "LMS36", "LMS37"):
        raise ValueError("Unsupported supplementary source kind")
    expected: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    # Read LF framing directly, retaining offsets independently of _lines().
    physical: list[tuple[int, int, bytes]] = []
    offset = 0
    for number, raw in enumerate(data.split(b"\n"), 1):
        if number > 1 and offset == len(data):
            break
        content = raw[:-1] if raw.endswith(b"\r") else raw
        physical.append((number, offset, content))
        offset += len(raw) + 1
    try:
        if kind == "DOK":
            if data.decode("cp850") != data.decode("cp865"):
                raise ValueError("Unsupported OEM bytes")
            header = [line.decode("cp850") for _, _, line in physical[:11]]
            if len(header) != 11:
                raise ValueError("Missing header")
            first_prefix = "Laegemiddelstyrelsen, den "
            title_prefix = "Aendringer i tilskudspriser per "
            if not header[0].startswith(first_prefix) or not header[3].startswith(title_prefix):
                raise ValueError("Missing dates")
            issued = datetime.strptime(header[0][len(first_prefix) :], "%d-%m-%Y").date()
            effective = datetime.strptime(header[3][len(title_prefix) :], "%d-%m-%Y").date()
            # Exact round trips independently prevent datetime's lenient padding.
            if header[0] != first_prefix + issued.strftime("%d-%m-%Y"):
                raise ValueError("Invalid issue date syntax")
            if header[3] != title_prefix + effective.strftime("%d-%m-%Y"):
                raise ValueError("Invalid effective date syntax")
            if any(header[index] for index in (1, 2, 4, 8, 10)):
                raise ValueError("Unexpected header lines")
            condition = " ".join(" ".join(header[5:8]).split())
            reference_condition = (
                "Ved substitution udleveres det billigste laegemiddel i en substitutionsgruppe "
                "(A-produktet). Hvis apoteket paa grund af leveringssvigt ikke kan skaffe det "
                "billigste laegemiddel i gruppen, vil der den "
                + effective.strftime("%d-%m-%Y")
                + " ogsaa vaere fuldt tilskud til nedenstaaende varenumre, som nu er bestemmende "
                "for tilskudsprisen i deres respektive tilskudsgrupper paa trods af, at de ikke "
                "er gruppens billigste."
            )
            if condition != reference_condition:
                raise ValueError("Unverified reimbursement condition")
            if header[9].strip() != "Varenummer, navn, form, styrke, pakningsstoerrelse, ATC-kode":
                raise ValueError("Unrecognized column declaration")
            start = 11
        else:
            reference_header = (
                b"DrugID;UdvidetOrdinationskode;G\xe6ldendeFra;G\xe6ldendeTil"
                if kind == "LMS36"
                else b"UdvidetOrdinationskode;Faggruppe;Specialenavn"
            )
            if not physical or physical[0][2] != reference_header:
                raise ValueError("Unrecognized native CSV header")
            start = 1
    except (ValueError, IndexError, UnicodeError):
        return {
            "accepted": False,
            "expected_rows": 0,
            "actual_rows": len(records),
            "issues": [{"code": "native_header_not_admitted", "source_line": None}],
        }
    for number, offset, raw in physical[start:]:
        try:
            record: dict[str, Any] = {
                "kind": kind,
                "source_line": number,
                "byte_offset": offset,
                "byte_length": len(raw),
            }
            if kind == "DOK":
                # First six characters and final seven independently locate the
                # only unambiguous typed tokens; embedded commas remain prose.
                if raw[6:8] != b", " or raw[-9:-7] != b", ":
                    raise ValueError("Invalid record separators")
                package = raw[:6].decode("ascii")
                atc = raw[-7:].decode("ascii")
                if len(package) != 6 or not all("0" <= c <= "9" for c in package):
                    raise ValueError("Invalid package token")
                if len(atc) != 7 or not (
                    "A" <= atc[0] <= "Z"
                    and atc[1:3].isascii()
                    and atc[1:3].isdigit()
                    and all("A" <= c <= "Z" for c in atc[3:5])
                    and atc[5:].isascii()
                    and atc[5:].isdigit()
                ):
                    raise ValueError("Invalid ATC token")
                description = raw[8:-9].decode("cp850")
                if not description:
                    raise ValueError("Missing source description")
                record.update(
                    package_code=package,
                    source_description=description,
                    atc_code=atc,
                    issue_date=issued.isoformat(),
                    effective_date=effective.isoformat(),
                    assertion="conditional_reimbursement_exception",
                )
            else:
                cells = _reference_cells(raw)
                if len(cells) != (4 if kind == "LMS36" else 3):
                    raise ValueError("Unexpected field count")
                if not cells[0] or not cells[1] or any(x != x.strip() for x in cells):
                    raise ValueError("Invalid mandatory cells")
                if kind == "LMS36":
                    if len(cells[0]) != 11 or not all("0" <= c <= "9" for c in cells[0]):
                        raise ValueError("Invalid drug identifier")
                    dates: list[str | None] = []
                    for index in (2, 3):
                        token = cells[index]
                        if index == 3 and token == "":
                            dates.append(None)
                            continue
                        if len(token) != 8 or not all("0" <= c <= "9" for c in token):
                            raise ValueError("Invalid date syntax")
                        dates.append(
                            datetime(int(token[:4]), int(token[4:6]), int(token[6:]))
                            .date()
                            .isoformat()
                        )
                    if dates[1] is not None and dates[0] is not None and dates[1] < dates[0]:
                        raise ValueError("Reversed interval")
                    record.update(
                        drug_id=cells[0],
                        extended_prescribing_code=cells[1],
                        valid_from=dates[0],
                        valid_to=dates[1],
                        assertion="prescribing_permission",
                    )
                else:
                    record.update(
                        extended_prescribing_code=cells[0],
                        professional_group=cells[1],
                        specialty_name=cells[2] or None,
                        assertion="prescribing_reference",
                    )
            expected.append(record)
        except (ValueError, UnicodeError, IndexError):
            failures.append({"code": "invalid_native_row", "source_line": number})
    if len(records) != len(expected):
        failures.append({"code": "record_count_mismatch", "source_line": None})
    for index, (actual, reference) in enumerate(zip(records, expected, strict=False)):
        if actual != reference:
            failures.append({"code": "record_mismatch", "record_index": index})
    return {
        "accepted": not failures,
        "expected_rows": len(expected),
        "actual_rows": len(records),
        "issues": failures,
    }
