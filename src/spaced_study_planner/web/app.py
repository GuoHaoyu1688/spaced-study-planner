"""Offline-capable loopback browser API."""

import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.content import render
from ..core.planning import plan, stats, today_plan
from ..core.scheduler import StudyError

STATIC = Path(__file__).parent / "static"


def rendered(item):
    return {
        **item,
        "front_html": render(item["front"]),
        "back_html": render(item["back"]),
        "explanation_html": render(item["explanation"]),
    }


def create_app(store):
    app = FastAPI(title="Study Field", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def guard(request, next_handler):
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return JSONResponse({"error": "Loopback host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"error": "Same-origin request required"}, status_code=403)
        if request.method == "POST" and "application/json" not in request.headers.get(
            "content-type", ""
        ):
            return JSONResponse({"error": "JSON required"}, status_code=415)
        try:
            return await next_handler(request)
        except (StudyError, ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/sw.js")
    def worker():
        return FileResponse(
            STATIC / "sw.js",
            media_type="text/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/media/{name}")
    def media(name: str):
        if (
            not re.fullmatch(r"[0-9a-f]{64}\.png", name)
            or not (store.assets / name).is_file()
            or (store.assets / name).is_symlink()
        ):
            return JSONResponse({"error": "Image not found"}, status_code=404)
        return FileResponse(store.assets / name, media_type="image/png")

    @app.get("/api/state")
    def state():
        exams = store.exams()
        if not exams:
            return {"exams": [], "subjects": [], "today": None, "stats": None}
        return {
            "exams": exams,
            "active_exam": store.exam(),
            "subjects": store.subjects(),
            "today": today_plan(store),
            "stats": stats(store),
            "forecast": plan(store, days=30, persist=False),
            "availability": store.availability(),
        }

    @app.post("/api/setup")
    async def setup(request: Request):
        data = await request.json()
        exam = store.add_exam(data["name"], data["date"])
        store.use_exam(exam["id"])
        for subject in data.get("subjects", []):
            store.add_subject(subject["name"], float(subject.get("weight", 1)))
        return exam

    @app.post("/api/use-exam")
    async def use_exam(request: Request):
        return store.use_exam((await request.json())["id"])

    @app.post("/api/items")
    async def add(request: Request):
        data = await request.json()
        return store.add_items(data["items"], data.get("subject"))

    @app.post("/api/availability")
    async def availability(request: Request):
        data = await request.json()
        return store.availability(data["key"], int(data["minutes"]))

    @app.post("/api/next")
    async def next_items(request: Request):
        data = await request.json()
        return [
            rendered(r)
            for r in store.next_items(
                data.get("n", 20), data.get("subject"), data.get("mode", "due"), data.get("tag")
            )
        ]

    @app.get("/api/today-items")
    def today_items():
        current = today_plan(store)
        tasks = (current["today"] or {}).get("tasks", [])
        indexed = {item["id"]: item for item in store.items(current["exam"]["id"])}
        return [rendered(indexed[task["item_id"]]) for task in tasks]

    @app.post("/api/answer")
    async def answer(request: Request):
        result = store.answer(**await request.json())
        result["back_html"] = render(result["back"])
        result["explanation_html"] = render(result["explanation"])
        return result

    @app.post("/api/session")
    async def session(request: Request):
        result = store.start_session(**await request.json())
        result["items"] = [rendered(r) for r in result["items"]]
        return result

    @app.post("/api/finish")
    async def finish(request: Request):
        return store.finish_session((await request.json())["id"])

    return app
