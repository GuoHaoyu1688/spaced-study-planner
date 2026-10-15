import csv
import json
import sqlite3
import zipfile

import pytest
from conftest import AT
from PIL import Image

from spaced_study_planner.core.content import read_items, render
from spaced_study_planner.core.interchange import export_library, import_library
from spaced_study_planner.core.scheduler import StudyError
from spaced_study_planner.core.store import Store


def test_csv_markdown_image_and_math(store, tmp_path):
    Image.new("RGB", (30, 20), "#6699aa").save(tmp_path / "image.png")
    source = tmp_path / "cards.md"
    source.write_text(
        "Identify the sample. ![Sample](image.png)\n???\nA generated color block with $x^2$.\n---\nSecond card\n???\nSecond answer\n"
    )
    assert store.import_items(source, "Reasoning", at=AT)["inserted"] == 2
    card = next(i for i in store.items() if i["front"].startswith("Identify"))
    assert "asset:" in card["front"]
    assert "/media/" in render(card["front"])
    assert "data:image/png;base64," in render(card["back"])
    assert "&lt;script&gt;" in render("<script>alert(1)</script>")
    csvfile = tmp_path / "items.csv"
    with csvfile.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, ["type", "front", "back", "options", "answer", "tags"])
        writer.writeheader()
        writer.writerow(
            {
                "type": "mcq",
                "front": "Original sum: 1+1?",
                "options": '["1","2"]',
                "answer": "B",
                "tags": '["small-sums"]',
            }
        )
    assert store.import_items(csvfile, "Quantitative", at=AT)["inserted"] == 1
    exported = tmp_path / "library.json"
    export_library(store, exported)
    clone = Store(tmp_path / "clone")
    import_library(clone, exported)
    assert len(list(clone.assets.glob("*.png"))) == 1
    assert clone.items() == store.items()


def test_legacy_anki_basic_fields_and_embedded_media(store, tmp_path):
    database = tmp_path / "anki.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE notes(id INTEGER,flds TEXT,tags TEXT)")
        db.execute(
            "INSERT INTO notes VALUES(1,?,?)",
            ('<b>Front</b><img src="picture.png">\x1fBack<br>Answer', "tag-a tag-b"),
        )
    Image.new("RGB", (10, 10), "white").save(tmp_path / "image.png")
    package = tmp_path / "basic.apkg"
    with zipfile.ZipFile(package, "w") as archive:
        archive.write(database, "collection.anki2")
        archive.write(tmp_path / "image.png", "0")
        archive.writestr("media", json.dumps({"0": "picture.png"}))
    result = store.import_items(package, "Reasoning", at=AT)
    assert result["inserted"] == 1
    item = next(i for i in store.items() if i["id"] == "anki-1")
    assert "asset:" in item["front"] and item["back"] == "Back\nAnswer"
    assert item["tags"] == ["tag-a", "tag-b"]
    assert store.import_items(package, "Reasoning", at=AT)["skipped"] == 1


def test_import_bounds_and_unsupported_formats(store, tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("Front without separator")
    with pytest.raises(StudyError):
        store.import_items(path, "Reasoning", at=AT)
    path.write_text("![Remote](https://example.invalid/x.png)\n???\nAnswer")
    with pytest.raises(StudyError):
        store.import_items(path, "Reasoning", at=AT)
    path.write_text("![Escape](../outside.png)\n???\nAnswer")
    with pytest.raises(StudyError, match="escapes"):
        store.import_items(path, "Reasoning", at=AT)
    with pytest.raises(StudyError):
        render("$\\unsupportedcommand{x}$")
    package = tmp_path / "modern.apkg"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("collection.anki21b", "compressed unsupported format")
    with pytest.raises(StudyError, match="legacy"):
        read_items(package, store.assets)


def test_snapshot_asset_tamper_rolls_back(store, tmp_path):
    path = tmp_path / "snapshot.json"
    export_library(store, path)
    data = json.loads(path.read_text())
    data["assets"] = {"../escape.png": "aGVsbG8="}
    path.write_text(json.dumps(data))
    clone = Store(tmp_path / "clone")
    with pytest.raises(StudyError):
        import_library(clone, path)
    assert clone.exams() == []


@pytest.mark.parametrize(
    "text",
    [
        "![Remote][image]\n\n[image]: https://example.invalid/tracker.png",
        "![image][]\n\n[image]: //example.invalid/tracker.png",
        '![Remote](https://example.invalid/tracker.png "title")',
    ],
)
def test_reference_images_cannot_bypass_offline_boundary(store, text):
    before = len(store.items())
    with pytest.raises(StudyError):
        store.add_items([{"type": "card", "front": text, "back": "Answer"}], "Reasoning", at=AT)
    assert len(store.items()) == before
    with pytest.raises(StudyError, match="managed assets"):
        render(text)


def test_managed_reference_image_renders_without_network(store, tmp_path):
    from spaced_study_planner.core.content import copy_image

    image = tmp_path / "image.png"
    Image.new("RGB", (10, 10), "white").save(image)
    asset = copy_image(image, store.assets)
    text = f"![Sample][image]\n\n[image]: {asset}"
    assert (
        store.add_items([{"type": "card", "front": text, "back": "Answer"}], "Reasoning", at=AT)[
            "inserted"
        ]
        == 1
    )
    assert "/media/" in render(text) and 'src="asset:' not in render(text)
