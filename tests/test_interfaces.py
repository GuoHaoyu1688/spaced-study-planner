import asyncio
import json
import os
import sys
from datetime import timedelta

from conftest import AT, ROOT
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from spaced_study_planner.cli import main
from spaced_study_planner.mcp_server import make_server
from spaced_study_planner.web.app import create_app


def test_cli_all_commands(store, tmp_path, capsys):
    def run(*args):
        status = main([*args, "--data-dir", str(store.root), "--json"])
        return status, json.loads(capsys.readouterr().out)

    for args in [
        ("exam", "list"),
        ("exam", "use", "exam"),
        ("subject", "list"),
        ("availability",),
        ("plan", "--rebuild", "--at", AT),
        ("today", "--at", AT),
        ("stats", "--at", AT),
        ("forecast", "--days", "30", "--at", AT),
        ("review", "--mode", "new", "--limit", "5", "--at", AT),
        ("backup",),
    ]:
        status, result = run(*args)
        assert status == 0 and result["ok"]
    assert run("exam", "add", "Another", "--date", "2027-01-01")[0] == 0
    assert run("subject", "add", "Extra", "--weight", "3")[0] == 0
    assert run("availability", "--key", "weekday:1", "--minutes", "15")[0] == 0
    assert (
        run("import", str(ROOT / "examples/questions.json"), "--at", AT)[1]["data"]["skipped"] == 50
    )
    item = next(i for i in store.items() if i["type"] == "mcq")
    assert run("answer", item["id"], "--answer", item["answer"], "--at", AT)[0] == 0
    status, result = run("mock", "--n", "2", "--minutes", "5", "--at", AT)
    assert status == 0
    assert run("finish", result["data"]["id"], "--at", "2026-10-15T00:05:00Z")[0] == 0
    assert run("micro", "--minutes", "5", "--at", AT)[0] == 0
    path = tmp_path / "all.json"
    assert run("export", "--out", str(path))[0] == 0
    assert run("import", str(path), "--library")[1]["data"]["inserted"] == 0
    assert run("unknown")[0] == 2
    assert run("answer", "missing", "--rating", "good")[0] == 3
    assert run("serve", "--host", "0.0.0.0")[0] == 2
    assert run("import", str(tmp_path / "missing.json"))[0] == 3
    assert main(["--data-dir", str(store.root), "exam", "list", "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_web_and_guards(store):
    with TestClient(create_app(store), base_url="http://127.0.0.1") as client:
        assert "Study Field" in client.get("/").text
        assert client.get("/sw.js").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/state").json()["stats"]["item_count"] == 50
        rows = client.post("/api/next", json={"mode": "new", "n": 5}).json()
        assert len(rows) == 5 and "<p>" in rows[0]["front_html"]
        result = client.post(
            "/api/answer",
            json={
                "item_id": rows[0]["id"],
                "answer": rows[0]["answer"],
                "expected_revision": 0,
                "request_id": "web",
            },
        ).json()
        assert result["correct"]
        assert (
            client.post("/api/availability", json={"key": "weekday:0", "minutes": 15}).status_code
            == 200
        )
        session = client.post("/api/session", json={"kind": "mock", "n": 2, "minutes": 5}).json()
        assert client.post("/api/finish", json={"id": session["id"]}).json()["score_percent"] == 0
        assert (
            client.post(
                "/api/items",
                json={"subject": "Reasoning", "items": [{"front": "Web card", "back": "Answer"}]},
            ).json()["inserted"]
            == 1
        )
        assert client.post("/api/use-exam", json={"id": "exam"}).status_code == 200
        assert (
            client.post(
                "/api/next", json={}, headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        assert client.get("/api/state", headers={"Host": "evil.invalid"}).status_code == 403
        assert client.post("/api/next", content="{}").status_code == 415
        assert client.get("/media/not-an-asset.png").status_code == 404
        assert client.post("/api/next", json={"n": 0}).status_code == 400


def test_mcp_all_tools(store):
    async def scenario():
        async with create_connected_server_and_client_session(
            make_server(store)._mcp_server
        ) as client:
            assert len((await client.list_tools()).tools) == 5
            for name, args in [
                ("get_today_plan", {}),
                ("next_items", {"n": 2, "mode": "new"}),
                ("get_stats", {"days": 30}),
                (
                    "add_items",
                    {"subject": "Reasoning", "items": [{"front": "MCP card", "back": "Answer"}]},
                ),
            ]:
                assert not (await client.call_tool(name, args)).isError
            item = next(i for i in store.items() if i["type"] == "mcq")
            assert not (
                await client.call_tool(
                    "submit_answer",
                    {
                        "item_id": item["id"],
                        "answer": item["answer"],
                        "elapsed_s": 2,
                        "request_id": "mcp",
                    },
                )
            ).isError
            assert (
                await client.call_tool("submit_answer", {"item_id": "missing", "rating": "good"})
            ).isError

    asyncio.run(scenario())


def test_actual_stdio(store):
    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "spaced_study_planner.cli", "--data-dir", str(store.root), "mcp"],
            env=dict(os.environ),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as client,
        ):
            await client.initialize()
            assert len((await client.list_tools()).tools) == 5
            assert not (await client.call_tool("get_stats", {"days": 30})).isError

    asyncio.run(scenario())
