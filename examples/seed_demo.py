"""Create a fictional exam and import 50 original questions plus three cards."""

import json
import os
from datetime import date, timedelta
from pathlib import Path

from spaced_study_planner.core.store import Store

root = Path(__file__).parent
store = Store(Path(os.environ.get("SSP_DATA_DIR", "data/demo")))
existing = next((e for e in store.exams() if e["id"] == "fictional-exam"), None)
exam = existing or store.add_exam(
    "Fictional Skills Review", (date.today() + timedelta(days=45)).isoformat(), "fictional-exam"
)
store.use_exam(exam["id"])
for name, weight in [("Quantitative", 2), ("Reasoning", 1), ("Reading", 1)]:
    store.add_subject(name, weight)
questions = store.import_items(root / "questions.json")
cards = store.import_items(root / "cards.md", subject="Reasoning")
store.availability("weekday:5", 60)
store.availability("weekday:6", 45)
print(
    json.dumps(
        {"ok": True, "data": {"exam": exam, "questions": questions, "cards": cards}, "warnings": []}
    )
)
