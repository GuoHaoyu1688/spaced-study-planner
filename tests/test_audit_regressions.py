"""Large-library selection and transactional snapshot integrity regressions."""

import copy
import json
from datetime import timedelta

import pytest
from conftest import AT
from fastapi.testclient import TestClient

from spaced_study_planner.core.interchange import import_library, snapshot
from spaced_study_planner.core.planning import plan, today_plan
from spaced_study_planner.core.scheduler import StudyError, utc
from spaced_study_planner.core.store import Store, encoded
from spaced_study_planner.web.app import create_app


def test_sessions_select_eligible_items_beyond_public_page_limit(tmp_path):
    store = Store(tmp_path)
    store.add_exam("Large original library", (utc().date() + timedelta(days=30)).isoformat())
    store.add_subject("Cards", 2)
    store.add_subject("Questions", 1)
    store.add_items(
        [
            {"id": f"card-{i:04}", "front": f"Original card {i}", "back": "Answer", "seconds": 3600}
            for i in range(501)
        ],
        "Cards",
        at=AT,
    )
    store.add_items(
        [
            {
                "id": "question",
                "type": "mcq",
                "front": "1+1?",
                "options": ["1", "2"],
                "answer": "B",
                "seconds": 30,
            }
        ],
        "Questions",
        at=AT,
    )
    assert all(i["type"] == "card" for i in store.next_items(500, mode="all"))
    assert [i["id"] for i in store.start_session("mock", n=1)["items"]] == ["question"]
    assert [i["id"] for i in store.start_session("micro", minutes=5)["items"]] == ["question"]
    planned = [i["item_id"] for i in today_plan(store)["today"]["tasks"]]
    assert planned == ["question"]
    with TestClient(create_app(store)) as client:
        response = client.get("/api/today-items")
        assert response.status_code == 200
        assert [i["id"] for i in response.json()] == planned
        assert "<p>1+1?</p>" in response.json()[0]["front_html"]
        assert "/api/today-items" in client.get("/static/app.js").text


@pytest.fixture
def complete_snapshot(store):
    session = store.start_session("mock", n=2, minutes=5, at=AT)
    for item in session["items"]:
        store.answer(
            item["id"], answer=item["answer"], elapsed_s=25, session_id=session["id"], at=AT
        )
    store.finish_session(session["id"], at="2026-10-15T00:04:00Z")
    plan(store, at=AT, days=2)
    store.availability("weekday:0", 25)
    return snapshot(store)


def mutate(data, case):
    records = data["records"]
    state_row = next(r for r in records["item_states"] if r["revision"])
    state = json.loads(state_row["state_json"])
    payload = json.loads(records["items"][0]["payload"])
    session = records["sessions"][0]
    review = records["reviews"][0]
    if case == "missing state":
        records["item_states"].pop()
    elif case == "unknown algorithm":
        state["algorithm"] = "unknown"
    elif case == "empty state":
        state = {}
    elif case == "bad FSRS state":
        state["card"]["state"] = 99
    elif case == "bad FSRS stability":
        state["card"]["stability"] = -1
    elif case == "bad FSRS difficulty":
        state["card"]["difficulty"] = 11
    elif case == "bad FSRS step":
        state["card"]["step"] = -1
    elif case == "bad retention":
        state["retention"] = 1.0
    elif case == "naive due":
        state["card"]["due"] = "2026-10-16T00:00:00"
    elif case == "due before review":
        state["card"]["due"] = "2020-01-01T00:00:00Z"
    elif case == "revision gap":
        state_row["revision"] += 1
    elif case == "empty payload":
        payload = {}
    elif case == "wrong answer":
        payload["answer"] = "Z"
    elif case == "bad duration":
        payload["seconds"] = -1
    elif case == "bad tags":
        payload["tags"] = [42]
    elif case == "missing image":
        payload["front"] = "![Missing](asset:" + "0" * 64 + ".png)"
    elif case == "bad exam date":
        records["exams"][0]["date"] = "2026-02-30"
    elif case == "unknown active exam":
        next(r for r in records["metadata"] if r["key"] == "active_exam")["value"] = "unknown"
    elif case == "bad weekday":
        records["availability"][0]["key"] = "weekday:9"
    elif case == "bad phase date":
        next(r for r in records["metadata"] if r["key"].startswith("plan_start:"))["value"] = "bad"
    elif case == "unknown session item":
        session["items_json"] = '["missing"]'
    elif case == "duplicate session item":
        ids = json.loads(session["items_json"])
        session["items_json"] = json.dumps([ids[0], ids[0]])
    elif case == "negative session time":
        session["deadline"] = "2020-01-01T00:00:00Z"
    elif case == "missing finished score":
        session["score_json"] = None
    elif case == "wrong score":
        score = json.loads(session["score_json"])
        score["correct"] = 0
        session["score_json"] = json.dumps(score)
    elif case == "wrong review time":
        review["at"] = "2020-01-01T00:00:00Z"
    elif case == "bad review envelope":
        review["state_json"] = "{}"
    elif case == "bad review hash":
        review["request_hash"] = "bad"
    elif case == "bad response":
        review["response_json"] = "{}"
    elif case == "bad plan reference":
        tasks = json.loads(records["plans"][0]["tasks_json"])
        tasks["tasks"][0]["item_id"] = "missing"
        records["plans"][0]["tasks_json"] = json.dumps(tasks)
    elif case == "wrong table shape":
        records["availability"] = {}
    else:
        raise AssertionError(case)
    state_row["state_json"] = json.dumps(state)
    records["items"][0]["payload"] = json.dumps(payload)


@pytest.mark.parametrize(
    "case",
    [
        "missing state",
        "unknown algorithm",
        "empty state",
        "bad FSRS state",
        "bad FSRS stability",
        "bad FSRS difficulty",
        "bad FSRS step",
        "bad retention",
        "naive due",
        "due before review",
        "revision gap",
        "empty payload",
        "wrong answer",
        "bad duration",
        "bad tags",
        "missing image",
        "bad exam date",
        "unknown active exam",
        "bad weekday",
        "bad phase date",
        "unknown session item",
        "duplicate session item",
        "negative session time",
        "missing finished score",
        "wrong score",
        "wrong review time",
        "bad review envelope",
        "bad review hash",
        "bad response",
        "bad plan reference",
        "wrong table shape",
    ],
)
def test_invalid_snapshot_rolls_back_every_record(complete_snapshot, tmp_path, case):
    data = copy.deepcopy(complete_snapshot)
    mutate(data, case)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data))
    target = Store(tmp_path / "target")
    with pytest.raises(StudyError):
        import_library(target, path)
    assert target.exams() == []
    assert list(target.assets.iterdir()) == []


def test_complete_mock_history_roundtrip(complete_snapshot, tmp_path):
    path = tmp_path / "valid.json"
    path.write_text(json.dumps(complete_snapshot))
    target = Store(tmp_path / "target")
    assert import_library(target, path)["inserted"] > 100
    assert snapshot(target) == complete_snapshot
    assert import_library(target, path)["inserted"] == 0


def test_sm2_roundtrip_and_invalid_ease(tmp_path):
    store = Store(tmp_path / "source", {"algorithm": "sm2"})
    store.add_exam("SM-2 example", "2026-11-29")
    store.add_subject("Original cards")
    store.add_items([{"id": "card", "front": "Front", "back": "Back"}], "Original cards", at=AT)
    store.answer("card", rating="good", at=AT)
    data = snapshot(store)
    path = tmp_path / "sm2.json"
    path.write_text(json.dumps(data))
    target = Store(tmp_path / "target")
    assert import_library(target, path)["inserted"] > 0
    assert target.items() == store.items()
    state = json.loads(data["records"]["item_states"][0]["state_json"])
    state["ease"] = 0
    data["records"]["item_states"][0]["state_json"] = encoded(state)
    path.write_text(json.dumps(data))
    with pytest.raises(StudyError, match="ease"):
        import_library(Store(tmp_path / "invalid"), path)
