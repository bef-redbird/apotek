"""Position-preserving reader for the archive's paragraph-based RTF reports.

Tab stops belong to each paragraph: absent source fields can remove a tab stop,
so tab-delimited plain text alone is not a field grid. Originals remain the
authority; this module never launches a document application or follows links.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from striprtf.striprtf import rtf_to_text

from .base import AdapterError

_CONTROL = re.compile(r"\\([A-Za-z]+)(-?\d+)? ?|\\(?:'[0-9A-Fa-f]{2}|.)")


def _split_control(source: str, word: str) -> list[str]:
    boundaries = [match for match in _CONTROL.finditer(source) if match[1] == word]
    result = []
    start = 0
    for match in boundaries:
        result.append(source[start : match.start()])
        start = match.end()
    result.append(source[start:])
    return result


def _count_control(source: str, word: str) -> int:
    return sum(match[1] == word for match in _CONTROL.finditer(source))


@dataclass(frozen=True)
class Paragraph:
    number: int
    page: int
    stops: tuple[int, ...]
    cells: tuple[str, ...]
    text: str
    raw: str

    @property
    def locator(self) -> str:
        return f"rtf:page:{self.page}!paragraph:{self.number}"

    def positioned(self) -> dict[int, str]:
        """Only the observed one-token-per-explicit-stop layouts are supported."""
        if len(self.cells) != len(self.stops) or len(set(self.stops)) != len(self.stops):
            raise AdapterError("rtf_tab_alignment", self.locator)
        return dict(zip(self.stops, self.cells, strict=True))


def paragraphs(data: bytes) -> list[Paragraph]:
    """Decode text without losing paragraph, page, tab-stop or escape context."""
    if not data.startswith(b"{\\rtf1") or len(data) > 128 * 1024 * 1024:
        raise AdapterError("invalid_rtf_container")
    header = data[:512].decode("ascii", errors="ignore")
    codepage = re.search(r"\\ansicpg(\d+)\b", header)
    encoding = f"cp{codepage[1]}" if codepage else "cp1252"
    if encoding not in {"cp1252", "cp65001"}:
        raise AdapterError("unsupported_rtf_codepage")
    try:
        source = data.decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise AdapterError("invalid_rtf_encoding") from exc
    if any(
        match[1] in {"bin", "object", "pict", "field", "trowd", "intbl", "nestrow", "cell", "row"}
        for match in _CONTROL.finditer(source)
    ):
        raise AdapterError("unsupported_rtf_structure")
    chunks = _split_control(source, "pard")
    if len(chunks) == 1:
        raise AdapterError("missing_rtf_paragraphs")
    if any(_count_control(chunk, "par") == 0 for chunk in chunks[1:]):
        raise AdapterError("rtf_paragraph_boundary")
    # Decode the complete document, retaining font tables and group state.
    # Rewrapping a fragment can make its inherited closing brace terminate a
    # synthetic document early and silently erase later records.
    marker = "\x1e"
    marked_source = _CONTROL.sub(
        lambda match: r"{\uc0\u30}" + match[0] if match[1] == "par" else match[0], source
    )
    try:
        decoded = rtf_to_text(marked_source, errors="strict").split(marker)
    except (UnicodeError, ValueError, IndexError) as exc:
        raise AdapterError("invalid_rtf_text") from exc
    expected_paragraphs = _count_control(source, "par")
    if len(decoded) != expected_paragraphs + 1:
        raise AdapterError("unaccounted_rtf_paragraph")
    if decoded[-1].strip():
        raise AdapterError("unaccounted_rtf_text")
    result: list[Paragraph] = []
    page = 1
    for chunk in chunks[1:]:
        prefix = chunk.split("{", 1)[0]
        stops = tuple(int(value) for value in re.findall(r"\\tx(\d+)\b", prefix))
        segments = _split_control(chunk, "par")
        if len(segments) == 1:
            raise AdapterError("rtf_paragraph_boundary", f"rtf:paragraph:{len(result) + 1}")
        for segment in segments[:-1]:
            number = len(result) + 1
            page += _count_control(segment, "page")
            text = decoded[number - 1].strip("\r\n")
            # A leading tab moves to the first declared stop, rather than
            # denoting an empty field. Interior empty cells remain unchanged.
            cells = text.split("\t")
            if not cells[0].strip() and len(cells) > 1:
                cells = cells[1:]
            result.append(Paragraph(number, page, stops, tuple(cells), text, segment))
        tail = segments[-1]
        page += _count_control(tail, "page")
    if expected_paragraphs != len(result):
        raise AdapterError("unaccounted_rtf_paragraph")
    return result
