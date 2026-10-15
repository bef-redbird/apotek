"""Generated backups obey the actual UTF-8 restore boundary without losing notes."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from bef_redbird.domain import (
    AnalysisContext,
    AnalysisResult,
    AnalysisSettings,
    CitoItem,
    CitoSnapshot,
    DataError,
    HistoryDataset,
    QueueSettings,
    Review,
    ReviewState,
    SourceInfo,
)
from bef_redbird.export import (
    MAX_SESSION_BYTES,
    MAX_SESSION_REVIEWS,
    dump_session,
    restore_session,
)
from bef_redbird.service import build_analysis


@pytest.fixture(scope="module")
def analysis() -> AnalysisResult:
    source = SourceInfo("synthetic.xlsx", "a" * 64, 1)
    periods = (date(2026, 1, 5), date(2026, 1, 19))
    items = {f"{i:06d}": CitoItem(f"{i:06d}", "N", 10000, "A", "C", "1") for i in range(1, 601)}
    snapshot = CitoSnapshot(source, items, [], len(items), len(items), 0)
    history = HistoryDataset(source, periods, dict.fromkeys(items, (20000, 10000)), {}, {}, [])
    return build_analysis(snapshot, history, AnalysisContext(periods[-1]), AnalysisSettings(2, 2))


def state_at_size(analysis: AnalysisResult, size: int, character: str) -> ReviewState:
    """Fill valid notes to an exact encoded size; Unicode and JSON escaping both count."""
    state = ReviewState(
        analysis.analysis_id,
        QueueSettings("all_rankable", 80),
        dict.fromkeys(analysis.snapshot.items, Review()),
        set(analysis.snapshot.items),
        "000600",
    )
    remaining = size - len(dump_session(analysis, state))
    character_bytes = len(json.dumps(character, ensure_ascii=False).encode("utf-8")) - 2
    for pid in state.reviews:
        count = min(4000, remaining // character_bytes)
        # ASCII padding reaches byte boundaries between multibyte characters.
        padding = min(4000 - count, remaining - count * character_bytes)
        state.reviews[pid] = Review(note=character * count + "a" * padding)
        remaining -= count * character_bytes + padding
        if remaining == 0:
            break
    assert remaining == 0, "Synthetic notes must have enough character capacity"
    return state


@pytest.mark.parametrize("character", ["a", "æ", "€", "😀", "\x00"])
@pytest.mark.parametrize("offset", [-1, 0])
def test_backup_at_or_below_actual_byte_limit_round_trips_every_note(
    analysis: AnalysisResult, character: str, offset: int
) -> None:
    state = state_at_size(analysis, MAX_SESSION_BYTES + offset, character)
    data = dump_session(analysis, state)
    assert MAX_SESSION_BYTES == 2_097_152
    assert len(data) == MAX_SESSION_BYTES + offset
    assert restore_session(data, analysis) == state
    assert any(len(review.note) == 4000 for review in state.reviews.values())


@pytest.mark.parametrize("character", ["a", "æ", "€", "😀", "\x00"])
def test_one_byte_over_limit_is_rejected_without_truncation_or_state_mutation(
    analysis: AnalysisResult, character: str
) -> None:
    state = state_at_size(analysis, MAX_SESSION_BYTES + 1, character)
    original = replace(state, reviews=dict(state.reviews), selected_ids=set(state.selected_ids))
    with pytest.raises(DataError, match="2 MB.*Excel.*forkort noter"):
        dump_session(analysis, state)
    assert state == original


def test_restore_enforces_same_inclusive_byte_limit(analysis: AnalysisResult) -> None:
    state = state_at_size(analysis, MAX_SESSION_BYTES, "😀")
    exact = dump_session(analysis, state)
    # Trailing whitespace is valid JSON, so rejection is due solely to byte size.
    assert json.loads(exact + b" ") == json.loads(exact)
    with pytest.raises(DataError, match="overstiger 2 MB"):
        restore_session(exact + b" ", analysis)


def test_near_limit_backup_retains_matching_analysis_guard(analysis: AnalysisResult) -> None:
    state = state_at_size(analysis, MAX_SESSION_BYTES, "😀")
    other = replace(analysis, analysis_id="b" * 64)
    with pytest.raises(DataError, match="anden analyse"):
        dump_session(other, state)
    with pytest.raises(DataError, match="matcher ikke"):
        restore_session(dump_session(analysis, state), other)


@pytest.mark.parametrize("invalid_review", [{"note": "a" * 4001}, {"status": "Ukendt"}])
def test_byte_policy_does_not_relax_review_validation(
    analysis: AnalysisResult, invalid_review: dict[str, str]
) -> None:
    payload = json.loads(dump_session(analysis, ReviewState(analysis.analysis_id)))
    payload["reviews"] = {"000001": invalid_review}
    data = json.dumps(payload).encode("utf-8")
    assert len(data) < MAX_SESSION_BYTES
    with pytest.raises(DataError, match="Ugyldig vurdering"):
        restore_session(data, analysis)


def test_review_count_limit_matches_restore_even_when_notes_fit_byte_policy(
    analysis: AnalysisResult,
) -> None:
    assert MAX_SESSION_REVIEWS == 25_000
    template = analysis.snapshot.items["000001"]
    items = {
        f"{i:06d}": replace(template, product_id=f"{i:06d}")
        for i in range(1, MAX_SESSION_REVIEWS + 2)
    }
    snapshot = replace(
        analysis.snapshot, items=items, input_rows=len(items), unique_required_rows=len(items)
    )
    many = build_analysis(snapshot, analysis.history, analysis.context, analysis.settings)
    state = ReviewState(
        many.analysis_id,
        reviews={pid: Review() for pid in list(items)[:MAX_SESSION_REVIEWS]},
    )
    data = dump_session(many, state)
    assert restore_session(data, many) == state

    extra_id = f"{MAX_SESSION_REVIEWS + 1:06d}"
    state.reviews[extra_id] = Review()
    payload = json.loads(data)
    payload["reviews"][extra_id] = {"status": "Ikke vurderet", "note": ""}
    oversized_count = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert len(oversized_count) < MAX_SESSION_BYTES
    with pytest.raises(DataError, match="25.000 vurderinger.*Excel"):
        dump_session(many, state)
    assert len(state.reviews) == MAX_SESSION_REVIEWS + 1
    with pytest.raises(DataError, match="Ugyldig vurderingssamling"):
        restore_session(oversized_count, many)
