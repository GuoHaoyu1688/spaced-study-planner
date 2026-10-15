"""Capacity-bounded rolling plans and transparent, conditional forecasts."""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from datetime import datetime, time, timedelta

from .scheduler import StudyError, advance, last_review, utc
from .store import encoded


def stats(store, days=30, exam=None, at=None):
    if type(days) is not int or not 1 <= days <= 3660:
        raise StudyError("Statistics window must be 1–3660 days")
    now = utc(at)
    start = now.astimezone(store.zone).date() - timedelta(days=days - 1)
    exam = store.exam(exam)
    items = store.items(exam["id"])
    reviews = store.review_rows(exam["id"])
    daily = {}
    subjects = {}
    tags = {}
    for row in reviews:
        timestamp = utc(row["at"])
        day = timestamp.astimezone(store.zone).date()
        if day < start or timestamp > now:
            continue
        d = daily.setdefault(day.isoformat(), {"reviews": 0, "correct": 0, "elapsed_s": 0})
        d["reviews"] += 1
        d["correct"] += row["correct"]
        d["elapsed_s"] += row["elapsed_s"]
        s = subjects.setdefault(row["subject"], {"reviews": 0, "correct": 0, "elapsed_s": 0})
        s["reviews"] += 1
        s["correct"] += row["correct"]
        s["elapsed_s"] += row["elapsed_s"]
        for tag in json.loads(row["payload"])["tags"]:
            t = tags.setdefault(tag, {"reviews": 0, "correct": 0})
            t["reviews"] += 1
            t["correct"] += row["correct"]
    for group in [subjects, tags]:
        for v in group.values():
            v["observed_accuracy"] = v["correct"] / v["reviews"]
    with store.connect() as db:
        mocks = [
            {"at": r["end"], **json.loads(r["score_json"])}
            for r in db.execute(
                "SELECT * FROM sessions WHERE exam_id=? AND kind='mock' AND score_json IS NOT NULL ORDER BY end",
                (exam["id"],),
            )
            if utc(r["end"]) <= now and utc(r["end"]).astimezone(store.zone).date() >= start
        ]
    return {
        "exam": exam,
        "days_to_exam": (
            datetime.fromisoformat(exam["date"]).date() - now.astimezone(store.zone).date()
        ).days,
        "item_count": len(items),
        "reviewed_items": sum(bool(last_review(r["state"])) for r in items),
        "reviews": sum(d["reviews"] for d in daily.values()),
        "elapsed_s": sum(d["elapsed_s"] for d in daily.values()),
        "daily": daily,
        "subjects": subjects,
        "tag_mastery": tags,
        "mock_scores": mocks,
        "note": "Observed practice accuracy and self-rated recall, not a calibrated exam-readiness estimate.",
    }


def phase(index, total):
    progress = index / max(1, total)
    return (
        "foundation"
        if progress < 0.35
        else "reinforcement"
        if progress < 0.65
        else "sprint"
        if progress < 0.85
        else "mock"
    )


def plan(store, exam=None, at=None, days=None, persist=True):
    now = utc(at)
    today = now.astimezone(store.zone).date()
    exam = store.exam(exam)
    remaining = (datetime.fromisoformat(exam["date"]).date() - today).days
    if remaining > 3660:
        raise StudyError("Exam is more than 3,660 days away")
    if days is not None and (type(days) is not int or not 1 <= days <= 365):
        raise StudyError("Forecast horizon must be 1–365 days")
    horizon = max(0, remaining) if days is None else min(max(0, remaining), days)
    if remaining <= 0:
        return {
            "exam": exam,
            "days": [],
            "warnings": ["Exam date has arrived or passed; choose a future exam for a new plan."],
            "assumption": "No work is planned on or after exam day.",
        }
    with store.connect() as db:
        key = "plan_start:" + exam["id"]
        stored = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        start_date = datetime.fromisoformat(stored["value"]).date() if stored else today
        if not stored:
            db.execute("INSERT INTO metadata VALUES(?,?)", (key, today.isoformat()))
    phase_total = max(1, (datetime.fromisoformat(exam["date"]).date() - start_date).days)
    items = copy.deepcopy(store.items(exam["id"]))
    if any(last_review(r["state"]) and utc(last_review(r["state"])) > now for r in items):
        raise StudyError("Planning cannot rewind current card states before their last review")
    seen = {r["id"] for r in items if last_review(r["state"])}
    initial_new = len(items) - len(seen)
    history = store.review_rows(exam["id"])
    subject_observations = defaultdict(list)
    spent_today = 0
    for row in history:
        when = utc(row["at"])
        if when <= now:
            subject_observations[row["subject"]].append(row["correct"])
            if when.astimezone(store.zone).date() == today:
                spent_today += row["elapsed_s"]
    priorities = {
        s["name"]: s["weight"]
        * max(
            0.15,
            1
            - (
                sum(subject_observations[s["name"]][-20:])
                / len(subject_observations[s["name"]][-20:])
                if subject_observations[s["name"]]
                else 0
            ),
        )
        for s in store.subjects(exam["id"])
    }
    result = []
    warnings = []
    total_budget = 0
    for offset in range(horizon):
        day = today + timedelta(days=offset)
        local_noon = datetime.combine(day, time(12), store.zone)
        when = max(now, utc(local_noon)) if offset == 0 else utc(local_noon)
        end = utc(datetime.combine(day + timedelta(days=1), time.min, store.zone))
        capacity = store.minutes_on(day) * 60
        budget = max(0, capacity - spent_today) if offset == 0 else capacity
        total_budget += budget
        tasks = []
        allocated = defaultdict(int)
        due_items = [r for r in items if r["id"] in seen and utc(r["due"]) < end]
        new_items = [r for r in items if r["id"] not in seen]
        due_items.sort(key=lambda r: (r["due"], -priorities[r["subject"]], r["id"]))
        candidates = due_items[:]
        while new_items:
            choice = min(
                new_items,
                key=lambda r: (
                    allocated[r["subject"]] / priorities[r["subject"]],
                    -priorities[r["subject"]],
                    r["id"],
                ),
            )
            candidates.append(choice)
            allocated[choice["subject"]] += choice["seconds"]
            new_items.remove(choice)
        unmet = 0
        for item in candidates:
            if item["seconds"] > budget:
                if item["id"] in seen:
                    unmet += 1
                continue
            kind = "review" if item["id"] in seen else "new"
            tasks.append(
                {
                    "item_id": item["id"],
                    "subject": item["subject"],
                    "kind": kind,
                    "estimated_s": item["seconds"],
                    "tags": item["tags"],
                }
            )
            budget -= item["seconds"]
            seen.add(item["id"])
            review_time = max(when, utc(item["due"])) if kind == "review" else when
            item["state"], _ = advance(item["state"], "good", review_time, 0, store.retention)
            item["due"] = (
                item["state"]["card"]["due"]
                if item["state"]["algorithm"] == "fsrs"
                else item["state"]["due"]
            )
        row = {
            "date": day.isoformat(),
            "phase": phase((day - start_date).days, phase_total),
            "available_minutes": capacity // 60,
            "already_studied_s": spent_today if offset == 0 else 0,
            "planned_s": sum(t["estimated_s"] for t in tasks),
            "unused_s": budget,
            "overdue_not_fitted": unmet,
            "tasks": tasks,
        }
        result.append(row)
    unintroduced = len(items) - len(seen)
    if unintroduced:
        seconds = sum(r["seconds"] for r in items if r["id"] not in seen)
        warnings.append(
            f"{unintroduced} items cannot receive a first pass within this horizon. At least {math_ceil_minutes(seconds)} extra minutes are needed before accounting for further repetitions."
        )
    if any(r["overdue_not_fitted"] for r in result):
        warnings.append(
            "Some due reviews do not fit the available time; the next rebuild carries them forward. Reduce new material or increase time."
        )
    if not items:
        warnings.append(
            "No items imported; add original or authorized material before interpreting the plan."
        )
    if persist:
        with store.connect() as db:
            db.execute(
                "DELETE FROM plans WHERE exam_id=? AND date>=?", (exam["id"], today.isoformat())
            )
            db.executemany(
                "INSERT INTO plans VALUES(?,?,?)",
                [(exam["id"], r["date"], encoded(r)) for r in result],
            )
    return {
        "exam": exam,
        "days": result,
        "initial_new_items": initial_new,
        "unintroduced_items": unintroduced,
        "remaining_capacity_minutes": round(total_budget / 60, 2),
        "warnings": warnings,
        "compression_options": [
            "Prioritize higher-weight subjects and weak tags.",
            "Pause low-priority new material; preserve overdue reviews.",
            "Add dated availability only when that time is genuinely available.",
        ]
        if warnings
        else [],
        "assumption": "Future reviews are simulated once per item per calendar day with Good ratings, no sub-day learning steps, and current estimated item durations. This is a conditional workload forecast, not guaranteed retention.",
    }


def math_ceil_minutes(seconds):
    return (seconds + 59) // 60


def today_plan(store, exam=None, at=None):
    value = plan(store, exam, at, days=1, persist=False)
    return {**value, "today": value["days"][0] if value["days"] else None}
