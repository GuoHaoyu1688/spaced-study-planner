from datetime import UTC, datetime, timedelta

import pytest
from fsrs import Card, Rating, Scheduler

from spaced_study_planner.core.scheduler import (
    StudyError,
    advance,
    initial_state,
    retrievability,
    utc,
)


@pytest.mark.parametrize("retention", [0.8, 0.9, 0.95])
@pytest.mark.parametrize(
    "sequence",
    [["good", "good", "again", "hard", "easy"], ["easy", "hard", "good", "good", "again"]],
)
def test_fixed_sequences_equal_official_reference(retention, sequence):
    at = datetime(2026, 1, 1, tzinfo=UTC)
    state = initial_state("fixed-card", at)
    reference = Card(card_id=state["card"]["card_id"], due=at)
    scheduler = Scheduler(
        desired_retention=retention, enable_fuzzing=False, learning_steps=(), relearning_steps=()
    )
    for offset, rating in zip([0, 3, 8, 12, 20], sequence, strict=True):
        when = at + timedelta(days=offset)
        state, _ = advance(state, rating, when, 20, retention)
        reference, _ = scheduler.review_card(
            reference, Rating[rating.title()], review_datetime=when, review_duration=20000
        )
        assert state["card"] == reference.to_dict()
        assert 0 <= retrievability(state, when) <= 1


def test_sm2_independent_arithmetic():
    at = utc("2026-01-01T00:00:00Z")
    state = initial_state("sm2", at, "sm2")
    for expected in [1, 6, 15]:
        state, _ = advance(state, "good", at)
        assert state["interval"] == expected and state["ease"] == 2.5
        at = utc(state["due"])
    state, _ = advance(state, "again", at)
    assert state["interval"] == 1 and state["repetitions"] == 0
    assert state["ease"] == pytest.approx(1.96)
    state, _ = advance(state, "easy", utc(state["due"]))
    assert state["interval"] == 1 and state["ease"] == pytest.approx(2.06)
    assert retrievability(state, at) is None


@pytest.mark.parametrize(
    "rating,elapsed", [("bad", 0), ("good", -1), ("good", float("nan")), ("good", True)]
)
def test_invalid_reviews(rating, elapsed):
    with pytest.raises(StudyError):
        advance(initial_state("a", utc()), rating, utc(), elapsed)


def test_date_and_retention_guards():
    with pytest.raises(StudyError):
        utc("2026-01-01")
    state = initial_state("x", "2026-01-01T00:00:00Z")
    with pytest.raises(StudyError):
        advance(state, "good", utc(), retention=1)
    state, _ = advance(state, "good", "2026-01-02T00:00:00Z")
    with pytest.raises(StudyError):
        advance(state, "good", "2026-01-01T00:00:00Z")
