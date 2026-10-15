"""Portable records, assets, and consistent full-library backups."""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import re
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

from fsrs import Card

from .content import render
from .scheduler import RATINGS, StudyError, due, fsrs_scheduler, last_review, utc
from .store import encoded, identity

TABLES = [
    "exams",
    "subjects",
    "items",
    "item_states",
    "sessions",
    "reviews",
    "plans",
    "availability",
    "metadata",
]


def _check(condition, message):
    if not condition:
        raise StudyError("Invalid snapshot: " + message)


def _number(value, low, high, integer=False):
    return (
        type(value) in ({int} if integer else {int, float})
        and math.isfinite(value)
        and low <= value <= high
    )


def _object(value, label):
    result = json.loads(value)
    _check(isinstance(result, dict), label + " must be an object")
    # The normal encoder disallows NaN and Infinity, including nested values.
    encoded(result)
    return result


def _state(value):
    state = _object(value, "scheduler state")
    _check(state.get("algorithm") in {"fsrs", "sm2"}, "unknown scheduler algorithm")
    if state["algorithm"] == "fsrs":
        card = state.get("card")
        _check(isinstance(card, dict), "missing FSRS card")
        fields = {"card_id", "state", "step", "stability", "difficulty", "due", "last_review"}
        _check(set(card) == fields, "invalid FSRS card fields")
        _check(_number(card["card_id"], 0, 2**63 - 1, True), "invalid FSRS card ID")
        _check(type(card["state"]) is int and card["state"] in {1, 2, 3}, "invalid FSRS state")
        _check(card["step"] is None or _number(card["step"], 0, 10000, True), "invalid FSRS step")
        if card["last_review"] is not None:
            _check(_number(card["stability"], 0.000001, 1e12), "invalid FSRS stability")
            _check(_number(card["difficulty"], 1, 10), "invalid FSRS difficulty")
        else:
            _check(
                card["stability"] is None and card["difficulty"] is None,
                "unreviewed FSRS parameters",
            )
            _check(card["state"] == 1 and card["step"] == 0, "invalid unreviewed FSRS stage")
        Card.from_dict(card)
        if "retention" in state:
            fsrs_scheduler(state["retention"])
    else:
        _check(
            set(state) == {"algorithm", "ease", "repetitions", "interval", "due", "last_review"},
            "invalid SM-2 state fields",
        )
        _check(_number(state["ease"], 1.3, 1e6), "invalid SM-2 ease")
        for key in ["repetitions", "interval"]:
            _check(_number(state[key], 0, 1e9, True), "invalid SM-2 " + key)
    deadline = utc(due(state))
    previous = last_review(state)
    if previous is not None:
        _check(deadline >= utc(previous), "due date precedes last review")
    return state


def _validate_library(db, store, incoming_assets):
    """Validate the merged library before committing any imported row or asset."""
    try:
        tables = {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")] for table in TABLES
        }
        for table, rows in tables.items():
            for row in rows:
                for key in ["id", "item_id", "exam_id", "subject_id"]:
                    if key in row:
                        _check(
                            isinstance(row[key], str) and bool(row[key]), f"invalid {table} {key}"
                        )
        exams = {r["id"]: r for r in tables["exams"]}
        for exam in exams.values():
            _check(
                isinstance(exam["name"], str) and 0 < len(exam["name"].strip()) <= 200,
                "invalid exam name",
            )
            date.fromisoformat(exam["date"])
        subjects = {r["id"]: r for r in tables["subjects"]}
        for subject in subjects.values():
            _check(subject["exam_id"] in exams, "unknown subject exam")
            _check(
                isinstance(subject["name"], str) and 0 < len(subject["name"].strip()) <= 100,
                "invalid subject name",
            )
            _check(_number(subject["weight"], 1e-300, 1000), "invalid subject weight")
        items = {}
        for row in tables["items"]:
            _check(row["subject_id"] in subjects, "unknown item subject")
            payload = _object(row["payload"], "item payload")
            required = {
                "type",
                "front",
                "back",
                "options",
                "answer",
                "explanation",
                "tags",
                "difficulty",
                "seconds",
                "source",
            }
            _check(set(payload) == required, "invalid item payload fields")
            _check(payload["type"] in {"card", "mcq"}, "invalid item type")
            for key in ["front", "back", "explanation", "source"]:
                _check(isinstance(payload[key], str), "invalid item " + key)
            _check(0 < len(payload["front"].strip()) <= 100000, "invalid question text")
            _check(_number(payload["difficulty"], 1, 5, True), "invalid difficulty")
            _check(_number(payload["seconds"], 5, 3600, True), "invalid item duration")
            _check(isinstance(payload["tags"], list) and len(payload["tags"]) <= 30, "invalid tags")
            _check(
                all(isinstance(tag, str) and 0 < len(tag) <= 80 for tag in payload["tags"]),
                "invalid tag",
            )
            options = payload["options"]
            _check(isinstance(options, list), "invalid options")
            if payload["type"] == "mcq":
                _check(
                    2 <= len(options) <= 8
                    and all(isinstance(x, str) and x.strip() for x in options),
                    "invalid MCQ options",
                )
                _check(
                    payload["answer"] in [chr(65 + i) for i in range(len(options))],
                    "invalid MCQ answer",
                )
            else:
                _check(bool(payload["back"].strip()), "empty card back")
            for field in ["front", "back", "explanation"]:
                html = render(payload[field])
                for asset in re.findall(r"/media/([0-9a-f]{64}\.png)", html):
                    path = store.assets / asset
                    _check(
                        asset in incoming_assets or (path.is_file() and not path.is_symlink()),
                        "missing item image",
                    )
            items[row["id"]] = {
                **row,
                "payload": payload,
                "exam_id": subjects[row["subject_id"]]["exam_id"],
            }
        states = {r["item_id"]: r for r in tables["item_states"]}
        _check(set(states) == set(items), "each item must have exactly one scheduler state")
        for row in states.values():
            row["parsed"] = _state(row["state_json"])
            _check(_number(row["revision"], 0, 1e12, True), "invalid item revision")
        sessions = {r["id"]: r for r in tables["sessions"]}
        for row in sessions.values():
            _check(row["exam_id"] in exams and row["kind"] in {"mock", "micro"}, "invalid session")
            start, deadline = utc(row["start"]), utc(row["deadline"])
            _check(0 < (deadline - start).total_seconds() <= 14400, "invalid session deadline")
            if row["end"] is not None:
                _check(utc(row["end"]) >= start, "invalid session end")
            ids = json.loads(row["items_json"])
            _check(
                isinstance(ids, list) and 0 < len(ids) <= 500 and len(set(ids)) == len(ids),
                "invalid session items",
            )
            _check(
                all(i in items and items[i]["exam_id"] == row["exam_id"] for i in ids),
                "invalid session item reference",
            )
            if row["kind"] == "mock":
                _check(
                    all(items[i]["payload"]["type"] == "mcq" for i in ids),
                    "mock contains a non-MCQ item",
                )
            row["parsed_items"] = ids
            _check(
                (row["end"] is None) == (row["score_json"] is None), "session score/end mismatch"
            )
        reviews = {item_id: [] for item_id in items}
        for row in tables["reviews"]:
            _check(row["item_id"] in items, "unknown reviewed item")
            reviewed = utc(row["at"])
            _check(
                row["rating"] in RATINGS and row["correct"] in {0, 1},
                "invalid review rating/correctness",
            )
            _check(_number(row["elapsed_s"], 0, 14400), "invalid review duration")
            event = _object(row["state_json"], "review event")
            _check(
                set(event) == {"state", "log"} and isinstance(event["log"], dict),
                "invalid review event",
            )
            state = _state(encoded(event["state"]))
            _check(
                last_review(state) is not None and utc(last_review(state)) == reviewed,
                "review/state date mismatch",
            )
            _check(
                bool(re.fullmatch(r"[0-9a-f]{64}", row["request_hash"])),
                "invalid review request hash",
            )
            response = _object(row["response_json"], "review response")
            _check(
                set(response)
                == {
                    "id",
                    "item_id",
                    "correct",
                    "rating",
                    "answer",
                    "back",
                    "explanation",
                    "due",
                    "revision",
                    "replayed",
                },
                "invalid review response fields",
            )
            _check(
                response.get("id") == row["id"] and response.get("item_id") == row["item_id"],
                "invalid review response identity",
            )
            _check(_number(response.get("revision"), 1, 1e12, True), "invalid response revision")
            _check(
                response.get("rating") == row["rating"]
                and response.get("correct") == bool(row["correct"]),
                "review response mismatch",
            )
            _check(utc(response["due"]) == utc(due(state)), "review response due mismatch")
            item = items[row["item_id"]]["payload"]
            _check(
                all(response[k] == item[k] for k in ["answer", "back", "explanation"])
                and response["replayed"] is False,
                "review response content mismatch",
            )
            if row["session_id"] is not None:
                _check(row["session_id"] in sessions, "unknown review session")
                session = sessions[row["session_id"]]
                _check(row["item_id"] in session["parsed_items"], "review item not in session")
                _check(
                    utc(session["start"]) <= reviewed <= utc(session["deadline"]),
                    "review outside session",
                )
            reviews[row["item_id"]].append({**row, "parsed": state, "response": response})
        for item_id, rows in reviews.items():
            rows.sort(key=lambda r: r["response"]["revision"])
            _check(
                [r["response"]["revision"] for r in rows] == list(range(1, len(rows) + 1)),
                "noncontiguous review revisions",
            )
            _check(states[item_id]["revision"] == len(rows), "review/state revision mismatch")
            if rows:
                _check(
                    states[item_id]["parsed"] == rows[-1]["parsed"], "latest review/state mismatch"
                )
                _check(
                    all(utc(a["at"]) <= utc(b["at"]) for a, b in zip(rows, rows[1:], strict=False)),
                    "reviews are out of order",
                )
            else:
                _check(
                    last_review(states[item_id]["parsed"]) is None, "reviewed state has no history"
                )
        for session in sessions.values():
            if session["score_json"] is not None:
                score = _object(session["score_json"], "session score")
                rows = [
                    r
                    for group in reviews.values()
                    for r in group
                    if r["session_id"] == session["id"]
                ]
                total, answered, correct = (
                    len(session["parsed_items"]),
                    len(rows),
                    sum(r["correct"] for r in rows),
                )
                expected = {
                    "id": session["id"],
                    "kind": session["kind"],
                    "total": total,
                    "answered": answered,
                    "correct": correct,
                    "unanswered": total - answered,
                    "score_percent": round(100 * correct / total, 2),
                }
                _check(
                    all(score.get(k) == v for k, v in expected.items()),
                    "session score does not match reviews",
                )
                _check(_number(score.get("elapsed_s"), 0, 14400), "invalid session elapsed time")
                _check(isinstance(score.get("by_subject"), dict), "invalid session score breakdown")
                breakdown = {}
                by_item = {r["item_id"]: r for r in rows}
                for item_id in session["parsed_items"]:
                    name = subjects[items[item_id]["subject_id"]]["name"]
                    entry = breakdown.setdefault(
                        name, {"total": 0, "answered": 0, "correct": 0, "elapsed_s": 0}
                    )
                    entry["total"] += 1
                    if item_id in by_item:
                        entry["answered"] += 1
                        entry["correct"] += by_item[item_id]["correct"]
                        entry["elapsed_s"] += by_item[item_id]["elapsed_s"]
                for entry in breakdown.values():
                    entry["accuracy"] = entry["correct"] / entry["total"]
                _check(score["by_subject"] == breakdown, "session breakdown does not match reviews")
        for row in tables["plans"]:
            _check(row["exam_id"] in exams, "unknown plan exam")
            date.fromisoformat(row["date"])
            payload = _object(row["tasks_json"], "daily plan")
            _check(
                payload.get("date") == row["date"] and isinstance(payload.get("tasks"), list),
                "invalid daily plan",
            )
            for task in payload["tasks"]:
                _check(isinstance(task, dict) and task.get("item_id") in items, "invalid plan task")
                _check(
                    items[task["item_id"]]["exam_id"] == row["exam_id"],
                    "plan item belongs to another exam",
                )
                _check(_number(task.get("estimated_s"), 5, 3600, True), "invalid plan duration")
        for row in tables["availability"]:
            _check(_number(row["minutes"], 0, 1440, True), "invalid availability minutes")
            if row["key"].startswith("weekday:"):
                _check(row["key"] in {f"weekday:{i}" for i in range(7)}, "invalid weekday")
            else:
                date.fromisoformat(row["key"])
        for row in tables["metadata"]:
            _check(
                isinstance(row["key"], str) and isinstance(row["value"], str), "invalid metadata"
            )
            if row["key"] == "active_exam":
                _check(row["value"] in exams, "unknown active exam")
            elif row["key"].startswith("plan_start:"):
                _check(row["key"][11:] in exams, "unknown phase-plan exam")
                date.fromisoformat(row["value"])
    except StudyError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise StudyError("Invalid snapshot content: " + str(exc)) from exc


def snapshot(store):
    with store.connect() as db:
        data = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table}")] for table in TABLES}
    assets = {
        p.name: base64.b64encode(p.read_bytes()).decode()
        for p in store.assets.glob("*.png")
        if p.is_file() and not p.is_symlink()
    }
    return {"schema_version": 1, "records": data, "assets": assets}


def export_library(store, path, format="json", overwrite=False):
    path = Path(path)
    if path.exists() and not overwrite:
        raise StudyError("Output exists; pass --yes to replace it")
    data = snapshot(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    if format == "json":
        text = json.dumps(data, ensure_ascii=False, indent=2)
    elif format == "csv":
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["record_type", "data"])
        for table, rows in data["records"].items():
            for row in rows:
                writer.writerow([table, encoded(row)])
        for name, content in data["assets"].items():
            writer.writerow(["asset", encoded({"name": name, "base64": content})])
        text = "\ufeff" + stream.getvalue()
    else:
        raise StudyError("Export format must be json or csv")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(text)
        name = temporary.name
    Path(name).replace(path)
    return {
        "path": str(path.resolve()),
        "records": sum(len(x) for x in data["records"].values()),
        "assets": len(data["assets"]),
    }


def import_library(store, path):
    import hashlib

    path = Path(path)
    if path.stat().st_size > 150_000_000:
        raise StudyError("Snapshot exceeds 150 MB")
    if path.suffix.lower() == ".csv":
        data = {"schema_version": 1, "records": {key: [] for key in TABLES}, "assets": {}}
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                value = json.loads(row["data"])
                if row["record_type"] == "asset":
                    data["assets"][value["name"]] = value["base64"]
                elif row["record_type"] in TABLES:
                    data["records"][row["record_type"]].append(value)
                else:
                    raise StudyError("Unknown snapshot record type")
    else:
        data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("records"), dict)
        or set(data["records"]) != set(TABLES)
        or not all(isinstance(rows, list) for rows in data["records"].values())
        or not isinstance(data.get("assets", {}), dict)
    ):
        raise StudyError("Unsupported snapshot schema")
    assets = {}
    for name, content in data.get("assets", {}).items():
        if not re.fullmatch(r"[0-9a-f]{64}\.png", name):
            raise StudyError("Invalid asset name")
        raw = base64.b64decode(content, validate=True)
        if len(raw) > 5_000_000 or hashlib.sha256(raw).hexdigest() + ".png" != name:
            raise StudyError("Asset hash or size mismatch")
        target = store.assets / name
        if target.is_symlink():
            raise StudyError("Asset destination is a symlink")
        if target.exists() and target.read_bytes() != raw:
            raise StudyError("Asset content conflict")
        assets[name] = raw
    inserted = skipped = 0
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for table in TABLES:
            columns = [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]
            primary = [r["name"] for r in db.execute(f"PRAGMA table_info({table})") if r["pk"]]
            for row in data["records"][table]:
                if not isinstance(row, dict) or set(row) != set(columns):
                    raise StudyError(f"Invalid fields in {table}")
                existing = db.execute(
                    f"SELECT * FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in primary),
                    tuple(row[k] for k in primary),
                ).fetchone()
                if existing:
                    if identity(dict(existing)) != identity(row):
                        raise StudyError(f"Conflicting snapshot record in {table}")
                    skipped += 1
                else:
                    db.execute(
                        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                        tuple(row[k] for k in columns),
                    )
                    inserted += 1
        _validate_library(db, store, assets)
        # Asset contents are validated before the transaction. Immutable names permit safe reuse.
        written = []
        try:
            for name, raw in assets.items():
                target = store.assets / name
                if not target.exists():
                    target.write_bytes(raw)
                    written.append(target)
        except OSError:
            for target in written:
                target.unlink(missing_ok=True)
            raise
    return {"inserted": inserted, "skipped": skipped, "assets": len(assets)}


def backup(store, path=None):
    stamp = datetime.now(store.zone).strftime("%Y%m%d-%H%M%S-%f")
    path = Path(path or store.root / "backups" / f"ssp-{stamp}.zip")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise StudyError("Backup destination exists")
    with tempfile.TemporaryDirectory() as folder:
        dbpath = Path(folder) / "study.db"
        with store.connect() as source, sqlite3.connect(dbpath) as destination:
            source.backup(destination)
        with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as archive:
            archive.write(dbpath, "study.db")
            for asset in store.assets.glob("*.png"):
                if asset.is_file() and not asset.is_symlink():
                    archive.write(asset, "assets/" + asset.name)
            archive.writestr("settings.json", encoded(store.settings))
    return {"path": str(path.resolve())}
