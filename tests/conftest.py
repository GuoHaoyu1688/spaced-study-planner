from pathlib import Path

import pytest

from spaced_study_planner.core.store import Store

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-10-15T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "study")
    s.add_exam("Fictional Exam", "2026-11-29", "exam")
    for name, weight in [("Quantitative", 2), ("Reasoning", 1), ("Reading", 1)]:
        s.add_subject(name, weight)
    s.import_items(ROOT / "examples/questions.json", at=AT)
    return s
