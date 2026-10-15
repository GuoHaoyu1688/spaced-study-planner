"""Deterministic FSRS adapter and an explicitly defined SM-2 scheduling variant."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta

from fsrs import Card, Rating, Scheduler


class StudyError(Exception):
    def __init__(self, message, code="INVALID_INPUT", exit_code=2):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


RATINGS = {"again": Rating.Again, "hard": Rating.Hard, "good": Rating.Good, "easy": Rating.Easy}


def utc(value=None):
    if value is None:
        return datetime.now(UTC)
    result = (
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if not isinstance(value, datetime)
        else value
    )
    if result.tzinfo is None:
        raise StudyError("Review timestamps require an explicit UTC offset")
    return result.astimezone(UTC)


def fsrs_scheduler(retention=0.9):
    if (
        not isinstance(retention, (int, float))
        or isinstance(retention, bool)
        or not 0.1 <= retention < 1
    ):
        raise StudyError("Desired retention must be at least 0.1 and below 1")
    return Scheduler(
        desired_retention=retention, learning_steps=(), relearning_steps=(), enable_fuzzing=False
    )


def initial_state(identifier, at, algorithm="fsrs"):
    now = utc(at)
    if algorithm == "fsrs":
        numeric = int(hashlib.sha256(identifier.encode()).hexdigest()[:13], 16)
        return {"algorithm": "fsrs", "card": Card(card_id=numeric, due=now).to_dict()}
    if algorithm == "sm2":
        return {
            "algorithm": "sm2",
            "ease": 2.5,
            "repetitions": 0,
            "interval": 0,
            "due": now.isoformat(),
            "last_review": None,
        }
    raise StudyError("Algorithm must be fsrs or sm2")


def due(state):
    return state["card"]["due"] if state["algorithm"] == "fsrs" else state["due"]


def last_review(state):
    return state["card"]["last_review"] if state["algorithm"] == "fsrs" else state["last_review"]


def advance(state, rating, at, elapsed_s=0, retention=0.9):
    if rating not in RATINGS:
        raise StudyError("Rating must be again, hard, good, or easy")
    at = utc(at)
    previous = last_review(state)
    if previous and at < utc(previous):
        raise StudyError("Review time precedes the last accepted review")
    if (
        not isinstance(elapsed_s, (int, float))
        or isinstance(elapsed_s, bool)
        or not math.isfinite(elapsed_s)
        or not 0 <= elapsed_s <= 14400
    ):
        raise StudyError("Elapsed seconds must be finite and between 0 and 14,400")
    if state["algorithm"] == "fsrs":
        card, log = fsrs_scheduler(retention).review_card(
            Card.from_dict(state["card"]),
            RATINGS[rating],
            review_datetime=at,
            review_duration=round(elapsed_s * 1000),
        )
        return {"algorithm": "fsrs", "card": card.to_dict(), "retention": retention}, log.to_dict()
    q = {"again": 1, "hard": 3, "good": 4, "easy": 5}[rating]
    repetitions = state["repetitions"]
    if q < 3:
        repetitions, interval = 0, 1
    else:
        interval = (
            1
            if repetitions == 0
            else 6
            if repetitions == 1
            else math.ceil(state["interval"] * state["ease"])
        )
        repetitions += 1
    ease = max(1.3, state["ease"] + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    result = {
        "algorithm": "sm2",
        "ease": round(ease, 10),
        "repetitions": repetitions,
        "interval": interval,
        "due": (at + timedelta(days=interval)).isoformat(),
        "last_review": at.isoformat(),
    }
    return result, {
        "rating": rating,
        "quality": q,
        "review_datetime": at.isoformat(),
        "elapsed_s": elapsed_s,
    }


def retrievability(state, at):
    if not last_review(state):
        return 0.0
    if state["algorithm"] == "fsrs":
        return fsrs_scheduler().get_card_retrievability(Card.from_dict(state["card"]), utc(at))
    return None
