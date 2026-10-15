"""Local item interchange and safe, offline Markdown/MathText rendering."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path
from threading import RLock

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from .scheduler import StudyError

RENDER_LOCK = RLock()

IMAGES = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
MATH = re.compile(r"\$\$(.+?)\$\$|(?<!\$)\$([^\n$]+)\$(?!\$)", re.S)


def copy_image(path: Path, assets: Path):
    from PIL import Image

    if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
        raise StudyError("Image is missing, a symlink, or larger than 5 MB")
    with Image.open(path) as image:
        if image.width * image.height > 20_000_000:
            raise StudyError("Image exceeds pixel limit")
        image.load()
        data = io.BytesIO()
        image.convert("RGB").save(data, format="PNG")
    raw = data.getvalue()
    name = hashlib.sha256(raw).hexdigest() + ".png"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / name).write_bytes(raw)
    return "asset:" + name


def normalize_images(text, base, assets):
    def replace(match):
        path = match[2]
        if path.startswith("asset:"):
            if (
                not re.fullmatch(r"asset:[0-9a-f]{64}\.png", path)
                or not (assets / path[6:]).is_file()
            ):
                raise StudyError("Unknown managed image")
            return match[0]
        if base is None:
            raise StudyError("Images require a local import file or an existing asset reference")
        candidate = (base / path).resolve()
        if not candidate.is_relative_to(base.resolve()):
            raise StudyError("Image escapes the import directory")
        return f"![{match[1]}]({copy_image(candidate, assets)})"

    normalized = IMAGES.sub(replace, text)
    tokens = MarkdownIt("commonmark", {"html": False}).parse(normalized)
    for source in image_sources(tokens):
        target = assets / source[6:]
        if target.is_symlink() or not target.is_file():
            raise StudyError("Unknown managed image")
    return normalized


def image_sources(tokens):
    """Validate parsed images, including reference-style and escaped Markdown syntax."""
    for token in tokens:
        if token.type == "image":
            source = token.attrGet("src") or ""
            if not re.fullmatch(r"asset:[0-9a-f]{64}\.png", source):
                raise StudyError(
                    "Images must use managed assets; import local images with inline Markdown syntax"
                )
            yield source
        if token.children:
            yield from image_sources(token.children)


@lru_cache(maxsize=512)
def render(text: str):
    math_assets = {}

    def formula(match):
        expression = (match[1] or match[2]).strip()
        if len(expression) > 1000:
            raise StudyError("Equation exceeds 1,000 characters")
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib.mathtext import math_to_image

        output = io.BytesIO()
        try:
            with RENDER_LOCK:
                math_to_image(
                    "$" + expression + "$", output, dpi=160, format="png", color="#183b45"
                )
        except (ValueError, RuntimeError) as exc:
            raise StudyError("Unsupported MathText equation") from exc
        token = "SSPMATH" + hashlib.sha256(expression.encode()).hexdigest()
        math_assets[token] = (
            '<img class="equation" alt="Equation" src="data:image/png;base64,'
            + base64.b64encode(output.getvalue()).decode()
            + '">'
        )
        return token

    markdown = MarkdownIt("commonmark", {"html": False})
    tokens = markdown.parse(MATH.sub(formula, text))
    list(image_sources(tokens))
    html = markdown.renderer.render(tokens, markdown.options, {})
    html = re.sub(r'src="asset:([0-9a-f]{64}\.png)"', r'src="/media/\1"', html)
    # Markdown's link syntax cannot create executable links; add an explicit external-link boundary.
    for token, value in math_assets.items():
        html = html.replace(token, value)
    return html


def read_items(path: Path, assets: Path):
    suffix = path.suffix.lower()
    if not path.is_file():
        raise StudyError("Import file not found", "NOT_FOUND", 3)
    if path.stat().st_size > 30_000_000:
        raise StudyError("Import exceeds 30 MB")
    if suffix == ".json":
        data = json.loads(path.read_text())
        rows = data if isinstance(data, list) else data.get("items", [])
    elif suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            for key in ["options", "tags"]:
                if row.get(key):
                    row[key] = json.loads(row[key])
    elif suffix in {".md", ".markdown"}:
        rows = []
        for block in re.split(r"^---\s*$", path.read_text(), flags=re.M):
            block = block.strip()
            if not block:
                continue
            if "\n???\n" not in block:
                raise StudyError(
                    "Markdown cards require front, a ??? line, and back, separated by ---"
                )
            front, back = block.split("\n???\n", 1)
            rows.append({"type": "card", "front": front, "back": back, "tags": []})
    elif suffix == ".apkg":
        return read_apkg(path, assets)
    else:
        raise StudyError("Use JSON, CSV, Markdown, or legacy Anki APKG")
    if not isinstance(rows, list) or not rows:
        raise StudyError("Import must contain a nonempty item list")
    for row in rows:
        for key in ["front", "back", "explanation"]:
            if key in row:
                row[key] = normalize_images(str(row[key]), path.parent, assets)
    return rows


def read_apkg(path, assets):
    with zipfile.ZipFile(path) as archive:
        if (
            sum(x.file_size for x in archive.infolist()) > 100_000_000
            or len(archive.infolist()) > 10000
        ):
            raise StudyError("APKG exceeds expanded size or member limit")
        names = set(archive.namelist())
        collection = "collection.anki21" if "collection.anki21" in names else "collection.anki2"
        if collection not in names:
            raise StudyError(
                "Only uncompressed legacy collection.anki2/anki21 is supported; export an older compatible package"
            )
        media = json.loads(archive.read("media")) if "media" in names else {}
        reverse = {value: key for key, value in media.items()}
        with tempfile.TemporaryDirectory() as folder:
            dbpath = Path(folder) / "collection.sqlite"
            dbpath.write_bytes(archive.read(collection))
            connection = sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)
            try:
                notes = connection.execute("SELECT id,flds,tags FROM notes ORDER BY id").fetchall()
            except sqlite3.Error as exc:
                raise StudyError("Unsupported Anki note database") from exc
            finally:
                connection.close()
            rows = []
            for identifier, fields, tags in notes:
                pieces = fields.split("\x1f")
                if len(pieces) < 2:
                    raise StudyError("Anki notes need at least two fields")

                def plain(value):
                    soup = BeautifulSoup(value, "html.parser")
                    for image in soup.find_all("img"):
                        name = image.get("src", "")
                        if name not in reverse:
                            raise StudyError("Anki image is missing from media")
                        member = reverse[name]
                        temporary = Path(folder) / "image-input"
                        temporary.write_bytes(archive.read(member))
                        image.replace_with(
                            "!["
                            + image.get("alt", "Image")
                            + "]("
                            + copy_image(temporary, assets)
                            + ")"
                        )
                    for br in soup.find_all("br"):
                        br.replace_with("\n")
                    return re.sub(r"\n{2,}", "\n", soup.get_text("\n")).strip()

                rows.append(
                    {
                        "id": "anki-" + str(identifier),
                        "type": "card",
                        "front": plain(pieces[0]),
                        "back": plain(pieces[1]),
                        "tags": tags.split(),
                        "source": "User-supplied Anki basic fields",
                    }
                )
            if not rows:
                raise StudyError("Anki package contains no notes")
            return rows
