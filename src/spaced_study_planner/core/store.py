"""Transactional study records shared by CLI, MCP, and browser adapters."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .content import normalize_images, read_items, render
from .scheduler import StudyError, advance, due, fsrs_scheduler, initial_state, last_review, utc


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def identity(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def require_text(value, name, limit=100000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StudyError(f"{name} must be nonempty text up to {limit} characters")
    return value.strip()


class Store:
    def __init__(self, root: Path, settings=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings or {}
        self.zone = ZoneInfo(self.settings.get("timezone", "Asia/Shanghai"))
        self.retention = self.settings.get("retention", 0.9)
        fsrs_scheduler(self.retention)
        self.algorithm = self.settings.get("algorithm", "fsrs")
        if self.algorithm not in {"fsrs", "sm2"}:
            raise StudyError("Algorithm must be fsrs or sm2")
        self.assets = self.root / "assets"
        self.assets.mkdir(exist_ok=True)
        self.db = self.root / "study.db"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS exams(id TEXT PRIMARY KEY,name TEXT NOT NULL,date TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS subjects(id TEXT PRIMARY KEY,exam_id TEXT NOT NULL REFERENCES exams(id),name TEXT NOT NULL,weight REAL NOT NULL CHECK(weight>0),UNIQUE(exam_id,name));
            CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,subject_id TEXT NOT NULL REFERENCES subjects(id),payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS item_states(item_id TEXT PRIMARY KEY REFERENCES items(id),state_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,exam_id TEXT NOT NULL REFERENCES exams(id),start TEXT NOT NULL,deadline TEXT NOT NULL,end TEXT,kind TEXT NOT NULL,items_json TEXT NOT NULL,score_json TEXT);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id),at TEXT NOT NULL,rating TEXT NOT NULL,correct INTEGER NOT NULL,elapsed_s REAL NOT NULL,state_json TEXT NOT NULL,request_hash TEXT NOT NULL,response_json TEXT NOT NULL,session_id TEXT REFERENCES sessions(id),UNIQUE(session_id,item_id));
            CREATE TABLE IF NOT EXISTS plans(exam_id TEXT NOT NULL REFERENCES exams(id),date TEXT NOT NULL,tasks_json TEXT NOT NULL,PRIMARY KEY(exam_id,date));
            CREATE TABLE IF NOT EXISTS availability(key TEXT PRIMARY KEY,minutes INTEGER NOT NULL CHECK(minutes>=0 AND minutes<=1440));
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_exam(self, name, exam_date, identifier=None):
        name = require_text(name, "Exam name", 200)
        exam_date = date.fromisoformat(exam_date).isoformat()
        identifier = identifier or identity([name, exam_date])[:20]
        with self.connect() as db:
            existing = db.execute("SELECT * FROM exams WHERE id=?", (identifier,)).fetchone()
            record = {"id": identifier, "name": name, "date": exam_date}
            if existing and dict(existing) != record:
                raise StudyError("Exam ID conflicts with an existing exam")
            db.execute("INSERT OR IGNORE INTO exams VALUES(?,?,?)", (identifier, name, exam_date))
            db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", ("active_exam", identifier))
        return record

    def exams(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM exams ORDER BY date,name")]

    def exam(self, identifier=None):
        with self.connect() as db:
            if identifier is None:
                active = db.execute("SELECT value FROM metadata WHERE key='active_exam'").fetchone()
                identifier = active["value"] if active else None
            rows = db.execute(
                "SELECT * FROM exams WHERE id=? OR name=?", (identifier, identifier)
            ).fetchall()
        if not rows:
            raise StudyError("Exam not found; add an exam first", "NOT_FOUND", 3)
        if len(rows) > 1:
            raise StudyError("Exam name is ambiguous; use its ID")
        return dict(rows[0])

    def use_exam(self, identifier):
        exam = self.exam(identifier)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("active_exam", exam["id"]))
        return exam

    def add_subject(self, name, weight=1, exam=None):
        exam = self.exam(exam)
        name = require_text(name, "Subject name", 100)
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or not 0 < weight <= 1000
        ):
            raise StudyError("Subject weight must be positive and at most 1000")
        identifier = identity([exam["id"], name])[:20]
        with self.connect() as db:
            db.execute(
                "INSERT INTO subjects VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET weight=excluded.weight",
                (identifier, exam["id"], name, weight),
            )
        return {"id": identifier, "exam_id": exam["id"], "name": name, "weight": weight}

    def subjects(self, exam=None):
        exam = self.exam(exam)
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM subjects WHERE exam_id=? ORDER BY name", (exam["id"],)
                )
            ]

    def subject(self, name, exam=None):
        matches = [r for r in self.subjects(exam) if name in {r["id"], r["name"]}]
        if len(matches) != 1:
            raise StudyError("Subject not found or ambiguous", "NOT_FOUND", 3)
        return matches[0]

    def availability(self, key=None, minutes=None):
        if key is not None:
            if key.startswith("weekday:"):
                if key not in {f"weekday:{i}" for i in range(7)}:
                    raise StudyError("Weekday uses Monday=0 through Sunday=6")
            else:
                key = date.fromisoformat(key).isoformat()
            if type(minutes) is not int or not 0 <= minutes <= 1440:
                raise StudyError("Available minutes must be an integer from 0 to 1440")
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO availability VALUES(?,?)", (key, minutes))
        with self.connect() as db:
            return {r["key"]: r["minutes"] for r in db.execute("SELECT * FROM availability")}

    def minutes_on(self, day):
        values = self.availability()
        day = date.fromisoformat(str(day))
        return values.get(day.isoformat(), values.get(f"weekday:{day.weekday()}", 30))

    def add_items(self, rows, subject=None, exam=None, at=None):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
            raise StudyError("Supply one to 10,000 items")
        now = utc(at)
        records = []
        for original in rows:
            if not isinstance(original, dict):
                raise StudyError("Every item must be an object")
            item = dict(original)
            sub = self.subject(subject or item.get("subject_id") or item.get("subject"), exam)
            kind = item.get("type", "card")
            if kind not in {"card", "mcq"}:
                raise StudyError("Item type must be card or mcq")
            front = require_text(item.get("front"), "Front")
            back = str(item.get("back", ""))
            explanation = str(item.get("explanation", ""))
            options = item.get("options", [])
            answer = item.get("answer", "")
            if kind == "mcq":
                if (
                    not isinstance(options, list)
                    or not 2 <= len(options) <= 8
                    or not all(isinstance(x, str) and x.strip() for x in options)
                ):
                    raise StudyError("MCQs require two to eight nonempty option strings")
                answer = str(answer).upper()
                if answer not in [chr(65 + i) for i in range(len(options))]:
                    raise StudyError("MCQ answer must be an option letter such as A")
            elif not back.strip():
                raise StudyError("Cards require back text")
            tags = item.get("tags", [])
            if (
                not isinstance(tags, list)
                or len(tags) > 30
                or not all(isinstance(x, str) and 0 < len(x) <= 80 for x in tags)
            ):
                raise StudyError("Tags must be a list of up to 30 short strings")
            difficulty = int(item.get("difficulty", 3))
            seconds = int(item.get("seconds", 45 if kind == "card" else 90))
            if not 1 <= difficulty <= 5 or not 5 <= seconds <= 3600:
                raise StudyError("Difficulty must be 1–5 and estimated seconds 5–3600")
            for value in [front, back, explanation]:
                normalize_images(value, None, self.assets)
                render(value)  # Reject unsupported equations before persisting the batch.
            record = {
                "type": kind,
                "front": front,
                "back": back,
                "options": options,
                "answer": answer,
                "explanation": explanation,
                "tags": tags,
                "difficulty": difficulty,
                "seconds": seconds,
                "source": str(item.get("source", "User-created material")),
            }
            identifier = item.get("id") or identity([sub["id"], record])[:24]
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 100:
                raise StudyError("Invalid item ID")
            records.append(
                (
                    identifier,
                    sub["id"],
                    encoded(record),
                    encoded(initial_state(identifier, now, self.algorithm)),
                )
            )
        inserted = skipped = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for identifier, sid, payload, state in records:
                old = db.execute("SELECT * FROM items WHERE id=?", (identifier,)).fetchone()
                if old:
                    if old["subject_id"] != sid or old["payload"] != payload:
                        raise StudyError("Conflicting item ID; import aborted")
                    skipped += 1
                else:
                    db.execute("INSERT INTO items VALUES(?,?,?)", (identifier, sid, payload))
                    db.execute("INSERT INTO item_states VALUES(?,?,0)", (identifier, state))
                    inserted += 1
        return {"inserted": inserted, "skipped": skipped}

    def import_items(self, path, subject=None, exam=None, at=None):
        return self.add_items(read_items(Path(path), self.assets), subject, exam, at)

    def items(self, exam=None, subject=None):
        exam = self.exam(exam)
        sid = self.subject(subject, exam["id"])["id"] if subject else None
        with self.connect() as db:
            rows = db.execute(
                """SELECT i.*,s.name AS subject,s.weight,st.state_json,st.revision FROM items i JOIN subjects s ON s.id=i.subject_id JOIN item_states st ON st.item_id=i.id WHERE s.exam_id=? AND (? IS NULL OR i.subject_id=?) ORDER BY i.id""",
                (exam["id"], sid, sid),
            ).fetchall()
        return [
            {
                **json.loads(r["payload"]),
                "id": r["id"],
                "subject_id": r["subject_id"],
                "subject": r["subject"],
                "weight": r["weight"],
                "state": json.loads(r["state_json"]),
                "revision": r["revision"],
                "due": due(json.loads(r["state_json"])),
            }
            for r in rows
        ]

    def next_items(self, n=20, subject=None, mode="due", tag=None, exam=None, at=None):
        if type(n) is not int or not 1 <= n <= 500:
            raise StudyError("Limit must be 1–500")
        if mode not in {"due", "new", "mistakes", "topic", "all"}:
            raise StudyError("Unknown review mode")
        now = utc(at)
        items = self.items(exam, subject)
        with self.connect() as db:
            latest = {
                r["item_id"]: r["correct"]
                for r in db.execute("SELECT item_id,correct FROM reviews ORDER BY at,rowid")
            }
        selected = []
        for item in items:
            seen = bool(last_review(item["state"]))
            if tag and tag not in item["tags"]:
                continue
            if mode == "due" and (not seen or utc(item["due"]) > now):
                continue
            if mode == "new" and seen:
                continue
            if mode == "mistakes" and latest.get(item["id"]) != 0:
                continue
            selected.append(item)
        return sorted(selected, key=lambda r: (r["due"], -r["weight"], r["id"]))[:n]

    def answer(
        self,
        item_id,
        answer=None,
        rating=None,
        elapsed_s=0,
        request_id=None,
        expected_revision=None,
        at=None,
        session_id=None,
    ):
        now = utc(at)
        request_id = request_id or str(uuid.uuid4())
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise StudyError("Invalid request ID")
        request = {
            "item_id": item_id,
            "answer": answer,
            "rating": rating,
            "elapsed_s": elapsed_s,
            "expected_revision": expected_revision,
            "at": utc(at).isoformat() if at is not None else None,
            "session_id": session_id,
        }
        request_hash = identity(request)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM reviews WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise StudyError("Request ID was reused with different review data")
                return {**json.loads(previous["response_json"]), "replayed": True}
            record = db.execute(
                "SELECT i.payload,st.state_json,st.revision FROM items i JOIN item_states st ON st.item_id=i.id WHERE i.id=?",
                (item_id,),
            ).fetchone()
            if not record:
                raise StudyError("Item not found", "NOT_FOUND", 3)
            if expected_revision is not None and expected_revision != record["revision"]:
                raise StudyError(
                    "Card changed since it was loaded; refresh before answering", "CONFLICT", 2
                )
            item = json.loads(record["payload"])
            if session_id:
                session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
                if not session or item_id not in json.loads(session["items_json"]):
                    raise StudyError("Item does not belong to this session")
                if session["end"]:
                    raise StudyError("Session is already finished")
                if now < utc(session["start"]) or now > utc(session["deadline"]):
                    raise StudyError("Answer is outside the session time window")
                if db.execute(
                    "SELECT 1 FROM reviews WHERE session_id=? AND item_id=?", (session_id, item_id)
                ).fetchone():
                    raise StudyError("Item already answered in this session")
            if item["type"] == "mcq":
                if not isinstance(answer, str) or answer.upper() not in [
                    chr(65 + i) for i in range(len(item["options"]))
                ]:
                    raise StudyError("Choose a valid option letter")
                correct = answer.upper() == item["answer"]
                computed = "good" if correct else "again"
                if rating and rating != computed:
                    raise StudyError("MCQ ratings are derived from correctness")
                rating = computed
            else:
                if rating is None:
                    raise StudyError("Cards require a self-assessed rating")
                correct = rating != "again"
            state, log = advance(
                json.loads(record["state_json"]), rating, now, elapsed_s, self.retention
            )
            result = {
                "id": request_id,
                "item_id": item_id,
                "correct": correct,
                "rating": rating,
                "answer": item["answer"],
                "back": item["back"],
                "explanation": item["explanation"],
                "due": due(state),
                "revision": record["revision"] + 1,
                "replayed": False,
            }
            db.execute(
                "UPDATE item_states SET state_json=?,revision=revision+1 WHERE item_id=?",
                (encoded(state), item_id),
            )
            db.execute(
                "INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    item_id,
                    now.isoformat(),
                    rating,
                    int(correct),
                    float(elapsed_s),
                    encoded({"state": state, "log": log}),
                    request_hash,
                    encoded(result),
                    session_id,
                ),
            )
        return result

    def start_session(self, kind="mock", n=15, minutes=30, subject=None, exam=None, at=None):
        if kind not in {"mock", "micro"}:
            raise StudyError("Session kind must be mock or micro")
        if type(minutes) is not int or not 1 <= minutes <= 240:
            raise StudyError("Session minutes must be 1–240")
        if kind == "micro" and minutes not in {5, 10, 15}:
            raise StudyError("Micro practice uses 5, 10, or 15 minutes")
        now = utc(at)
        exam = self.exam(exam)
        # The public review-page limit must not hide eligible items from sessions.
        candidates = sorted(
            self.items(exam["id"], subject),
            key=lambda r: (r["due"], -r["weight"], r["id"]),
        )
        if kind == "mock":
            candidates = [r for r in candidates if r["type"] == "mcq"]
        else:
            candidates = sorted(
                candidates,
                key=lambda r: (
                    0 if last_review(r["state"]) and utc(r["due"]) <= now else 1,
                    r["due"],
                ),
            )
        if type(n) is not int or not 1 <= n <= 500:
            raise StudyError("Session size must be 1–500")
        if kind == "micro":
            selected = []
            budget = minutes * 60
            for row in candidates:
                if len(selected) >= n:
                    break
                if row["seconds"] <= budget:
                    selected.append(row)
                    budget -= row["seconds"]
        else:
            selected = candidates[:n]
        if not selected:
            raise StudyError("No suitable items fit this session", "NOT_FOUND", 3)
        identifier = str(uuid.uuid4())
        deadline = now + timedelta(minutes=minutes)
        with self.connect() as db:
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    exam["id"],
                    now.isoformat(),
                    deadline.isoformat(),
                    None,
                    kind,
                    encoded([r["id"] for r in selected]),
                    None,
                ),
            )
        return {
            "id": identifier,
            "kind": kind,
            "start": now.isoformat(),
            "deadline": deadline.isoformat(),
            "items": selected,
            "requested": n,
            "selected": len(selected),
        }

    def finish_session(self, identifier, at=None):
        now = utc(at)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise StudyError("Session not found", "NOT_FOUND", 3)
            if row["score_json"]:
                return json.loads(row["score_json"])
            if now < utc(row["start"]):
                raise StudyError("Finish time precedes session start")
            ids = json.loads(row["items_json"])
            reviews = {
                r["item_id"]: dict(r)
                for r in db.execute("SELECT * FROM reviews WHERE session_id=?", (identifier,))
            }
            breakdown = {}
            for item_id in ids:
                item = db.execute(
                    "SELECT s.name FROM items i JOIN subjects s ON s.id=i.subject_id WHERE i.id=?",
                    (item_id,),
                ).fetchone()
                target = breakdown.setdefault(
                    item["name"], {"total": 0, "answered": 0, "correct": 0, "elapsed_s": 0}
                )
                target["total"] += 1
                if item_id in reviews:
                    review = reviews[item_id]
                    target["answered"] += 1
                    target["correct"] += review["correct"]
                    target["elapsed_s"] += review["elapsed_s"]
            correct = sum(r["correct"] for r in reviews.values())
            for value in breakdown.values():
                value["accuracy"] = value["correct"] / value["total"]
            result = {
                "id": identifier,
                "kind": row["kind"],
                "total": len(ids),
                "answered": len(reviews),
                "correct": correct,
                "score_percent": round(100 * correct / len(ids), 2),
                "by_subject": breakdown,
                "elapsed_s": min(
                    (now - utc(row["start"])).total_seconds(),
                    (utc(row["deadline"]) - utc(row["start"])).total_seconds(),
                ),
                "timed_out": now >= utc(row["deadline"]),
                "unanswered": len(ids) - len(reviews),
            }
            db.execute(
                "UPDATE sessions SET end=?,score_json=? WHERE id=?",
                (now.isoformat(), encoded(result), identifier),
            )
        return result

    def review_rows(self, exam=None):
        exam = self.exam(exam)
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT r.*,s.name AS subject,i.payload FROM reviews r JOIN items i ON i.id=r.item_id JOIN subjects s ON s.id=i.subject_id WHERE s.exam_id=? ORDER BY r.at,r.id",
                    (exam["id"],),
                )
            ]
