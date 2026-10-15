"""Generate original fictional practice questions. No exam papers are copied."""

import json
from pathlib import Path

ROOT = Path(__file__).parent
items = []
for i in range(20):
    a = 12 + i * 3
    b = 4 + i
    answer = a + b
    items.append(
        {
            "id": f"quant-{i + 1:02}",
            "subject": "Quantitative",
            "type": "mcq",
            "front": f"A fictional workshop completes {a} blue tokens and {b} green tokens. How many tokens does it complete in total?",
            "options": [str(answer - 2), str(answer), str(answer + 3), str(a - b)],
            "answer": "B",
            "explanation": f"Add the two independent counts: {a} + {b} = {answer}.",
            "tags": ["addition", "quantity"],
            "difficulty": 1,
            "seconds": 60,
            "source": "Original fictional practice question",
        }
    )
for i in range(15):
    name = f"Group {i + 1}"
    unit = 2 + i
    items.append(
        {
            "id": f"logic-{i + 1:02}",
            "subject": "Reasoning",
            "type": "mcq",
            "front": f"In a fictional sorting system, every item in {name} has a square label. Item K is in {name}. Which conclusion must follow?",
            "options": [
                "Item K has a square label.",
                "Every square-labeled item belongs to this group.",
                "Item K is the only item in the group.",
                "Item K has a circular label.",
            ],
            "answer": "A",
            "explanation": "Membership in the group implies a square label. The reverse implication and uniqueness are not established.",
            "tags": ["deduction", "necessary-conclusion"],
            "difficulty": 2,
            "seconds": 75,
            "source": "Original fictional practice question",
        }
    )
for i in range(15):
    start = 8 + i % 4
    duration = 2 + i % 3
    items.append(
        {
            "id": f"reading-{i + 1:02}",
            "subject": "Reading",
            "type": "mcq",
            "front": f"Fictional notice {i + 1}: “The practice room opens at {start}:00. The first session lasts {duration} hours. A break starts immediately after that session.” According to the notice, when does the break start?",
            "options": [
                f"{start}:00",
                f"{start + duration + 1}:00",
                f"{start + duration}:00",
                "The notice does not say.",
            ],
            "answer": "C",
            "explanation": f"The session starts at {start}:00 and lasts {duration} hours, so the break starts at {start + duration}:00.",
            "tags": ["explicit-information", "time"],
            "difficulty": 1,
            "seconds": 60,
            "source": "Original fictional practice question",
        }
    )
(ROOT / "questions.json").write_text(json.dumps(items, indent=2) + "\n")
(ROOT / "cards.md").write_text(
    "What is the square of $x$?\n???\nThe square is $x^2$.\n---\nWhat distinguishes a necessary conclusion?\n???\nIt must follow from the stated premises, without adding new assumptions.\n---\nWhat should a study forecast be treated as?\n???\nA conditional plan that can change with actual performance and available time.\n"
)
