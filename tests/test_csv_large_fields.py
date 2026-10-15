"""Native CSV snapshots preserve large managed images within the existing file cap."""

import csv
import hashlib
import random

import pytest
from PIL import Image

from spaced_study_planner.core.interchange import (
    SNAPSHOT_IMPORT_BYTES,
    export_library,
    import_library,
)
from spaced_study_planner.core.scheduler import StudyError
from spaced_study_planner.core.store import Store


def test_large_png_native_csv_roundtrip(store, tmp_path):
    image = tmp_path / "original-generated-pattern.png"
    Image.frombytes("RGB", (256, 256), random.Random(123).randbytes(256 * 256 * 3)).save(image)
    assert image.stat().st_size > 131072
    cards = tmp_path / "cards.md"
    cards.write_text(
        "![Original pattern](original-generated-pattern.png)\n???\nAn original generated pattern."
    )
    store.import_items(cards, "Reasoning")
    archive = tmp_path / "library.csv"
    export_library(store, archive, "csv")
    assert archive.stat().st_size > 131072
    previous_limit = csv.field_size_limit()
    target = Store(tmp_path / "restored")
    result = import_library(target, archive)
    assert result["assets"] == 1 and result["inserted"] > 100
    assert csv.field_size_limit() == previous_limit
    assert target.items() == store.items()
    original = next(store.assets.glob("*.png"))
    restored = target.assets / original.name
    assert (
        hashlib.sha256(restored.read_bytes()).digest()
        == hashlib.sha256(original.read_bytes()).digest()
    )
    assert import_library(target, archive)["inserted"] == 0


def test_large_invalid_csv_restores_limit_and_is_atomic(store, tmp_path):
    previous_limit = csv.field_size_limit()
    path = tmp_path / "malformed.csv"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["record_type", "data"])
        writer.writerow(["asset", "x" * 140000])
    target = Store(tmp_path / "restored")
    with pytest.raises(ValueError):
        import_library(target, path)
    assert csv.field_size_limit() == previous_limit
    assert target.exams() == []


def test_csv_snapshot_keeps_existing_file_size_cap(store, tmp_path):
    path = tmp_path / "oversized.csv"
    with path.open("wb") as stream:
        stream.truncate(SNAPSHOT_IMPORT_BYTES + 1)
    with pytest.raises(StudyError, match="150 MB"):
        import_library(store, path)
