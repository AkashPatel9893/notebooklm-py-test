"""Save a chapter's generated content into the phoenix backend's MongoDB.

Writes documents shaped exactly like the Mongoose models in
``Lernoverse/phoenix/app/models/ncert/`` (collection names taken from
Mongoose's own pluralisation):

    ncertclasses → ncertsubjects → ncertbooks → ncertchapters
    ncertquizquestions, ncertflashcards, ncertmindmaps   (content, per chapter)
    ncertresources                  (one document per chapter: its media files)

Safe to re-run:

* Class / subject / book / chapter are upserted on their natural keys with
  ``$setOnInsert`` only — anything edited later in the backend is never
  overwritten.
* Quiz questions / flashcards are upserted on (chapter, question text / card
  front), so ``_id``s stay stable across runs (safe for anything that
  references them); items NotebookLM no longer produces are removed.
* A chapter's media files (audio, video, PDFs) live in a single
  ``ncertresources`` document keyed on ``chapter_id``, rewritten on each sync.
  Quiz / flashcards / mind map are not repeated there — they already have
  their own collections, queried by ``chapter_id``.

The real NotebookLM downloads differ from the samples in the model docstrings
(mind map is ``{name, children}``, quiz is ``{title, questions}``, flashcards are
``{title, cards: [{front, back}]}``); the converters below handle that.

pymongo is synchronous; callers run these functions in a worker thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PIPELINE = "notebooklm-pipeline"
# Bump when the document shapes written here change, so every chapter re-syncs.
SCHEMA_VERSION = "9"  # 9: + subject image & thumbnail_url
MARKER = "mongo.json"  # local per-chapter record of the last sync
# Local files a sync reads; a chapter is saved once any of them exists.
CONTENT_FILES = ("mind_map.json", "quiz.json", "flashcards.json", "drive.json", "youtube.json")

C = {
    "class": "ncertclasses",
    "subject": "ncertsubjects",
    "book": "ncertbooks",
    "chapter": "ncertchapters",
    "quiz": "ncertquizquestions",
    "flashcard": "ncertflashcards",
    "mindmap": "ncertmindmaps",
    "resource": "ncertresources",
}

# Local file → (key, type, title) of an item in the chapter's NCERTResource.
# Files not listed (slides.pptx, *.json, *.html, *.md) are not written.
FILE_RESOURCES = {
    "audio.m4a": ("audio", "audio", "Audio Overview"),
    "video.mp4": ("video", "video", "Video Overview"),
    "cinematic_video.mp4": ("cinematic_video", "video", "Cinematic Video"),
    "slides.pdf": ("slides", "pdf", "Slides"),
    "source.pdf": ("source_pdf", "pdf", "NCERT Chapter PDF"),
    "infographic.png": ("infographic", "image", "Infographic"),
    "chapter_thumbnail.png": ("chapter_thumbnail", "image", "Chapter Thumbnail"),
    "video_thumbnail.jpg": ("video_thumbnail", "image", "Video Thumbnail"),
    "thumbnail.jpg": ("thumbnail", "image", "Thumbnail"),
}

# Files that reach YouTube as a different local file (audio_video.py).
YOUTUBE_ALIASES = {"audio.m4a": "audio_video.mp4"}

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


class MongoNotConfigured(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _num(text: str) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def _part_number(book: str) -> int | None:
    m = re.search(r"part[\s\-–]*([IVX]+|\d+)\b", book or "", re.IGNORECASE)
    if not m:
        return None
    tok = m.group(1).upper()
    return int(tok) if tok.isdigit() else _ROMAN.get(tok)


def connect() -> Any:
    """Return the pymongo Database named by MONGODB_DB / the URI."""
    from pymongo import MongoClient

    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise MongoNotConfigured("MONGODB_URI is not set in pipeline/.env")
    client = MongoClient(uri, serverSelectionTimeoutMS=10000, appname="ncert-pipeline")
    name = os.getenv("MONGODB_DB", "").strip()
    return client[name] if name else client.get_default_database()


# --------------------------------------------------------------------------- #
# NotebookLM JSON → model shapes
# --------------------------------------------------------------------------- #


def convert_mind_map(raw: dict[str, Any]) -> tuple[str, dict[str, Any], int]:
    """{name, children} → (title, root node, node count) for NCERTMindMap."""
    counter = 0

    def node(n: dict[str, Any], level: int) -> dict[str, Any]:
        nonlocal counter
        nid = f"node-{counter}"
        counter += 1
        return {
            "id": nid,
            "text": str(n.get("name") or n.get("text") or "").strip(),
            "level": level,
            "expanded": level <= 2,
            "children": [node(c, level + 1) for c in n.get("children") or []],
        }

    root = node(raw.get("root") or raw, 1)
    return root["text"], root, counter


def convert_quiz(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """NotebookLM quiz → NCERTQuizQuestion fields. Multiple-choice only.

    The backend models one question shape — options with exactly one correct —
    so the other types NotebookLM mixes in (multiple-select, fill-in-the-blank,
    short-answer) are dropped.
    """
    out = []
    for q in raw.get("questions") or raw.get("quiz") or []:
        text = (q.get("question") or "").strip()
        opts = [{
            "text": o.get("text", ""),
            "is_correct": bool(o.get("isCorrect")),
            "rationale": o.get("rationale"),
        } for o in q.get("answerOptions") or []]
        if (q.get("type") or "multiple_choice") != "multiple_choice":
            continue
        if text and opts and sum(o["is_correct"] for o in opts) == 1:
            out.append({"question": text, "answer_options": opts, "hint": q.get("hint")})
    return out


def convert_flashcards(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in raw.get("cards") or raw.get("flashcards") or []:
        front, back = c.get("front") or c.get("f"), c.get("back") or c.get("b")
        if front and back:
            ctype = c.get("card_type") or c.get("c") or 1
            out.append({"front": front, "back": back,
                        "card_type": ctype if isinstance(ctype, int) and 1 <= ctype <= 5 else 1})
    return out


def _load(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def content_hash(cdir: Path) -> str:
    h = hashlib.sha256(SCHEMA_VERSION.encode())
    for name in CONTENT_FILES:
        p = cdir / name
        h.update(name.encode())
        h.update(p.read_bytes() if p.exists() else b"-")
    return h.hexdigest()


def has_content(cdir: Path) -> bool:
    return any((cdir / n).exists() for n in CONTENT_FILES)


def needs_sync(cdir: Path) -> bool:
    marker = _load(cdir / MARKER) or {}
    return has_content(cdir) and marker.get("hash") != content_hash(cdir)


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def _upsert_insert_only(coll: Any, key: dict[str, Any], fields: dict[str, Any]) -> Any:
    """Find-or-create on `key`; never modifies an existing document."""
    now = _now()
    doc = coll.find_one_and_update(
        key,
        {"$setOnInsert": {**key, **fields, "created_at": now, "updated_at": now}},
        upsert=True,
        return_document=True,  # ReturnDocument.AFTER
    )
    return doc["_id"]


def sync_chapter(db: Any, row: dict[str, str], cdir: Path, dry_run: bool = False) -> dict[str, Any]:
    """Write one chapter. Returns a summary dict. Raises on bad input / DB errors."""
    from pymongo import UpdateOne

    # The mind map is optional: without one, whatever an earlier run saved stays.
    mind_raw = _load(cdir / "mind_map.json")
    mm_title, root, n_nodes = convert_mind_map(mind_raw) if mind_raw else ("", None, 0)
    # The name NotebookLM gave when asked (progress.csv) beats the mind map's root.
    title = (row.get("chapter_title") or "").strip() or mm_title
    if not title:
        raise ValueError("no chapter_title in progress.csv and no mind_map.json to take it from")
    quiz = convert_quiz(_load(cdir / "quiz.json") or {})
    cards = convert_flashcards(_load(cdir / "flashcards.json") or {})
    drive = _load(cdir / "drive.json") or {}
    youtube = _load(cdir / "youtube.json") or {}

    class_no = _num(row["class"])
    chapter_no = _num(row["chapter"])
    if class_no is None or chapter_no is None:
        raise ValueError(f"can't read class/chapter number from {row['class']!r} / {row['chapter']!r}")
    book_name = (row.get("book") or row["subject"]).strip()

    # Only what can't be derived: Drive view/download links and YouTube watch/
    # embed links all follow from the ids.
    items = []
    vthumb_info = drive.get("video_thumbnail.jpg")
    vthumb_url = (vthumb_info.get("preview_url") or vthumb_info.get("view_url") if vthumb_info else "")
    for fname, (key, rtype, label) in FILE_RESOURCES.items():
        info, yt = drive.get(fname), youtube.get(YOUTUBE_ALIASES.get(fname, fname))
        if not (info or yt):
            continue
        # `url` is what the app embeds: the YouTube player for videos, the Drive
        # preview otherwise (audio keeps its Drive file when it has one).
        url = yt.get("embed_url") if yt and (rtype == "video" or not info) else info.get("preview_url")
        item = {"key": key, "type": rtype, "title": label, "url": url}
        if info:
            item["drive_id"] = info.get("id")
        if yt:
            item["youtube_id"] = yt.get("id")
            if rtype == "video":
                item["thumbnail_url"] = f"https://i.ytimg.com/vi/{yt.get('id')}/hqdefault.jpg"
        elif rtype == "video" and vthumb_url:
            item["thumbnail_url"] = vthumb_url
        items.append(item)

    summary = {"title": title, "quiz": len(quiz), "flashcards": len(cards),
               "mind_map_nodes": n_nodes, "resources": [i["key"] for i in items]}
    if dry_run:
        return summary

    now = _now()
    class_id = _upsert_insert_only(db[C["class"]], {"class_number": class_no},
                                   {"name": row["class"].strip(), "display_order": class_no})
    subject_id = _upsert_insert_only(db[C["subject"]],
                                     {"class_id": class_id, "name": row["subject"].strip()},
                                     {"display_order": 0})
    book_id = _upsert_insert_only(db[C["book"]], {"subject_id": subject_id, "name": book_name},
                                  {"part_number": _part_number(book_name),
                                   "display_order": _part_number(book_name) or 0})
    book_thumb_url = (drive.get("thumbnail.jpg", {}).get("preview_url")
                      or drive.get("thumbnail.jpg", {}).get("view_url")
                      or row.get("thumbnail_url") or "").strip()
    if book_thumb_url:
        db[C["book"]].update_one(
            {"_id": book_id},
            {"$set": {"thumbnail_url": book_thumb_url, "updated_at": now},
             "$unset": {"image": ""}}
        )

    # Chapter thumbnail: strictly extracted first page of chapter PDF (never book cover)
    ch_thumb_entry = drive.get("chapter_thumbnail.png") or drive.get("chapter_thumbnail.jpg") or {}
    chapter_thumb_url = (ch_thumb_entry.get("preview_url")
                         or ch_thumb_entry.get("view_url")
                         or "").strip()

    chapter_id = _upsert_insert_only(db[C["chapter"]],
                                     {"book_id": book_id, "chapter_number": chapter_no},
                                     {"title": title, "display_order": chapter_no})
    if chapter_thumb_url:
        db[C["chapter"]].update_one(
            {"_id": chapter_id},
            {"$set": {"thumbnail_url": chapter_thumb_url, "updated_at": now},
             "$unset": {"image": ""}}
        )
    else:
        db[C["chapter"]].update_one(
            {"_id": chapter_id},
            {"$unset": {"image": "", "thumbnail_url": ""}}
        )

    if quiz:
        # Fields schema version 2 wrote for the other question types.
        stale = {f: "" for f in ("question_type", "best_answer", "acceptable_answers",
                                 "rationale", "grading")}
        db[C["quiz"]].bulk_write([UpdateOne(
            {"chapter_id": chapter_id, "question": q["question"]},
            {"$set": {"answer_options": q["answer_options"], "hint": q["hint"], "updated_at": now},
             "$unset": stale, "$setOnInsert": {"created_at": now}},
            upsert=True) for q in quiz], ordered=False)
        db[C["quiz"]].delete_many({"chapter_id": chapter_id,
                                   "question": {"$nin": [q["question"] for q in quiz]}})
    if cards:
        db[C["flashcard"]].bulk_write([UpdateOne(
            {"chapter_id": chapter_id, "front": c["front"]},
            {"$set": {"back": c["back"], "card_type": c["card_type"], "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True) for c in cards], ordered=False)
        db[C["flashcard"]].delete_many({"chapter_id": chapter_id,
                                        "front": {"$nin": [c["front"] for c in cards]}})
    if root:
        db[C["mindmap"]].update_one(
            {"chapter_id": chapter_id},
            {"$set": {"title": title, "root": root, "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True)
    # Schema 3 wrote one document per file / deck; drop those.
    db[C["resource"]].delete_many({"chapter_id": chapter_id, "meta.source": PIPELINE})
    if items:
        db[C["resource"]].update_one(
            {"chapter_id": chapter_id},
            {"$set": {"items": items, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True)
    else:
        db[C["resource"]].delete_one({"chapter_id": chapter_id})

    summary["chapter_id"] = str(chapter_id)
    marker = {"hash": content_hash(cdir), "chapter_id": str(chapter_id),
              "synced_at": now.isoformat(timespec="seconds"), **summary}
    tmp = cdir / f".{MARKER}.tmp"
    tmp.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    os.replace(tmp, cdir / MARKER)
    return summary
