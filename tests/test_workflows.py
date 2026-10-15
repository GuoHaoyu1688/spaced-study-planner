import concurrent.futures
import zipfile
from pathlib import Path

import pytest
from conftest import AT, ROOT

from spaced_study_planner.core.interchange import backup, export_library, import_library
from spaced_study_planner.core.planning import plan, stats, today_plan
from spaced_study_planner.core.scheduler import StudyError
from spaced_study_planner.core.store import Store


def test_fifty_original_questions_complete_mock_and_manual_score(store):
    session = store.start_session(n=50, minutes=120, at=AT)
    assert len(session["items"]) == 50
    for i, item in enumerate(session["items"]):
        answer = (
            item["answer"]
            if i < 40
            else next(x for x in ["A", "B", "C", "D"] if x != item["answer"])
        )
        result = store.answer(
            item["id"],
            answer=answer,
            elapsed_s=30,
            at="2026-10-15T00:01:00Z",
            session_id=session["id"],
        )
        assert result["correct"] == (i < 40)
    score = store.finish_session(session["id"], at="2026-10-15T01:00:00Z")
    assert score["correct"] == 40 and score["score_percent"] == 80 and score["unanswered"] == 0
    assert sum(s["correct"] for s in score["by_subject"].values()) == 40
    assert sum(s["elapsed_s"] for s in score["by_subject"].values()) == 1500
    assert store.finish_session(session["id"], at="2026-10-15T02:00:00Z") == score
    assert len(store.next_items(20, mode="mistakes", at="2026-10-15T02:00:00Z")) == 10
    result = stats(store, 30, at="2026-10-15T02:00:00Z")
    assert result["reviews"] == 50 and result["elapsed_s"] == 1500
    assert result["mock_scores"][0]["score_percent"] == 80
    assert result["daily"]["2026-10-15"]["correct"] == 40


def test_retry_stale_revision_and_concurrent_request(store):
    item = store.next_items(1, mode="new", at=AT)[0]
    kwargs = dict(answer=item["answer"], at=AT, request_id="retry", expected_revision=0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.answer(item["id"], **kwargs), range(2)))
    assert sorted(r["replayed"] for r in results) == [False, True]
    assert len(store.review_rows()) == 1
    with pytest.raises(StudyError, match="reused"):
        store.answer(item["id"], answer="D", at=AT, request_id="retry")
    with pytest.raises(StudyError, match="changed"):
        store.answer(item["id"], answer=item["answer"], at=AT, expected_revision=0)
    with pytest.raises(StudyError, match="derived"):
        store.answer(item["id"], answer=item["answer"], rating="again", at=AT)


def test_timed_session_unanswered_and_duplicate_rejection(store):
    session = store.start_session(n=3, minutes=5, at=AT)
    item = session["items"][0]
    store.answer(
        item["id"], answer=item["answer"], session_id=session["id"], at="2026-10-15T00:01:00Z"
    )
    with pytest.raises(StudyError, match="already answered"):
        store.answer(
            item["id"], answer=item["answer"], session_id=session["id"], at="2026-10-15T00:02:00Z"
        )
    with pytest.raises(StudyError, match="time window"):
        store.answer(
            session["items"][1]["id"],
            answer="A",
            session_id=session["id"],
            at="2026-10-15T00:06:00Z",
        )
    score = store.finish_session(session["id"], at="2026-10-15T00:06:00Z")
    assert score["timed_out"] and score["unanswered"] == 2 and score["score_percent"] == 33.33
    assert score["elapsed_s"] == 300


def test_plan_capacity_travel_catchup_and_phase_progress(store):
    store.availability("2026-10-15", 0)
    first = plan(store, at=AT)
    assert first["days"][0]["tasks"] == []
    assert sum(len(d["tasks"]) for d in first["days"][1:]) > 0
    for day in first["days"]:
        assert day["planned_s"] <= day["available_minutes"] * 60
    for day in ["2026-11-25", "2026-11-26", "2026-11-27"]:
        store.availability(day, 5)
    later = plan(store, at="2026-11-25T00:00:00Z", days=3)
    assert later["days"][0]["phase"] == "mock"
    assert later["warnings"]  # Fifty unseen items no longer fit the short remaining horizon.
    assert later["compression_options"]
    assert today_plan(store, at="2026-10-15T00:00:00Z")["today"]["available_minutes"] == 0


def test_time_shortfall_and_weighted_new_items(tmp_path):
    s = Store(tmp_path)
    s.add_exam("Example", "2026-10-17")
    for name, weight in [("A", 2), ("B", 1)]:
        s.add_subject(name, weight)
        s.add_items(
            [
                {"front": f"{name}{i}", "back": "Answer", "seconds": 30, "type": "card"}
                for i in range(6)
            ],
            name,
            at=AT,
        )
    s.availability("2026-10-15", 2)
    s.availability("2026-10-16", 0)
    result = plan(s, at=AT)
    tasks = result["days"][0]["tasks"]
    assert len(tasks) == 4 and [t["subject"] for t in tasks[:3]] == ["A", "B", "A"]
    assert result["unintroduced_items"] == 8
    assert "4 extra minutes" in result["warnings"][0]
    assert plan(s, at="2026-10-17T00:00:00Z")["days"] == []


def test_micro_budget_and_cards(store):
    store.import_items(ROOT / "examples/cards.md", "Reasoning", at=AT)
    session = store.start_session("micro", n=100, minutes=5, at=AT)
    assert sum(i["seconds"] for i in session["items"]) <= 300
    card = next(i for i in store.items() if i["type"] == "card")
    result = store.answer(card["id"], rating="good", elapsed_s=45, at=AT)
    assert result["correct"] and result["rating"] == "good"
    with pytest.raises(StudyError):
        store.start_session("micro", minutes=7, at=AT)


@pytest.mark.parametrize("format", ["json", "csv"])
def test_library_roundtrip_and_backup(store, tmp_path, format):
    item = store.next_items(1, mode="new", at=AT)[0]
    store.answer(item["id"], answer=item["answer"], at=AT)
    plan(store, at=AT, days=3)
    path = tmp_path / f"library.{format}"
    export_library(store, path, format)
    clone = Store(tmp_path / "clone")
    assert import_library(clone, path)["inserted"] > 100
    assert clone.items() == store.items()
    assert clone.review_rows() == store.review_rows()
    assert import_library(clone, path)["inserted"] == 0
    if format == "csv":
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with zipfile.ZipFile(Path(backup(store)["path"])) as archive:
        assert archive.testzip() is None
        archive.extractall(tmp_path / "restored")
    restored = Store(tmp_path / "restored")
    assert restored.items() == store.items()


def test_import_conflict_rolls_back(store):
    rows = [
        {"id": "new-record", "front": "New", "back": "Answer", "type": "card"},
        {"id": "quant-01", "front": "Conflict", "back": "Answer", "type": "card"},
    ]
    with pytest.raises(StudyError, match="Conflicting"):
        store.add_items(rows, "Quantitative", at=AT)
    assert len(store.items()) == 50
    assert store.import_items(ROOT / "examples/questions.json", at=AT)["skipped"] == 50
