"""Save a chapter's generated content into the phoenix backend's MongoDB.

Writes documents shaped exactly like the Mongoose models in
``Lernoverse/phoenix/app/models/ncert/`` (collection names taken from
Mongoose's own pluralisation):

    ncertclasses → ncertsubjects → ncertbooks → ncertchapters
    ncertquizquestions, ncertflashcards, ncertmindmaps   (content, per chapter)
    ncertresources                                        (files + decks, per chapter)

Safe to re-run:

* Class / subject / book / chapter are upserted on their natural keys with
  ``$setOnInsert`` only — anything edited later in the backend is never
  overwritten.
* Quiz questions / flashcards are upserted on (chapter, question text / card
  front), so ``_id``s stay stable across runs (safe for anything that
  references them); items NotebookLM no longer produces are removed.
* Resources written here carry ``meta.source = "notebooklm-pipeline"``; only
  those are ever updated — resources added by hand are left alone.

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
SCHEMA_VERSION = "3"  # 3: multiple-choice quiz only, no infographic resource (2 saved both)
MARKER = "mongo.json"  # local per-chapter record of the last sync
# Local files a sync reads; a chapter is saved once any of them exists.
CONTENT_FILES = ("mind_map.json", "quiz.json", "flashcards.json", "drive.json")

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

# Local file → NCERTResource. resource_type must be one of the model's enum:
# video, pdf, quiz, notes, flashcard, audio, mindmap. Files with no matching
# type (slides.pptx, infographic.png, *.html, *.md) are not written.
FILE_RESOURCES = {
    "audio.m4a": ("audio", "Audio Overview"),
    "cinematic_video.mp4": ("video", "Cinematic Video"),
    "video.mp4": ("video", "Video Overview"),
    "slides.pdf": ("pdf", "Slides"),
    "source.pdf": ("pdf", "NCERT Chapter PDF"),
}

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

    class_no = _num(row["class"])
    chapter_no = _num(row["chapter"])
    if class_no is None or chapter_no is None:
        raise ValueError(f"can't read class/chapter number from {row['class']!r} / {row['chapter']!r}")
    book_name = (row.get("book") or row["subject"]).strip()

    resources = []
    for fname, (rtype, label) in FILE_RESOURCES.items():
        info = drive.get(fname)
        if info:
            resources.append((fname, rtype, f"{title} – {label}", info.get("preview_url"), {
                "file": fname, "drive_file_id": info.get("id"), "view_url": info.get("view_url"),
                "preview_url": info.get("preview_url"), "download_url": info.get("download_url"),
                "size": info.get("size"), "mime": info.get("mime"),
            }))
    if quiz:
        resources.append(("quiz", "quiz", f"{title} – Quiz", None, {"question_count": len(quiz)}))
    if cards:
        resources.append(("flashcards", "flashcard", f"{title} – Flashcard Deck", None,
                          {"card_count": len(cards)}))
    if root:
        resources.append(("mind_map", "mindmap", f"{title} – Mind Map", None,
                          {"node_count": n_nodes}))

    summary = {"title": title, "quiz": len(quiz), "flashcards": len(cards),
               "mind_map_nodes": n_nodes, "resources": [r[0] for r in resources]}
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
    chapter_id = _upsert_insert_only(db[C["chapter"]],
                                     {"book_id": book_id, "chapter_number": chapter_no},
                                     {"title": title, "display_order": chapter_no})

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
    if resources:
        db[C["resource"]].bulk_write([UpdateOne(
            {"chapter_id": chapter_id, "meta.source": PIPELINE, "meta.key": key},
            {"$set": {"title": rtitle, "resource_type": rtype, "url": url,
                      "meta": {"source": PIPELINE, "key": key, **meta}, "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True) for key, rtype, rtitle, url, meta in resources], ordered=False)
    # Drop our own resources this run no longer produces (never hand-added ones).
    keep = [r[0] for r in resources] + ([] if root else ["mind_map"])
    db[C["resource"]].delete_many({"chapter_id": chapter_id, "meta.source": PIPELINE,
                                   "meta.key": {"$nin": keep}})

    summary["chapter_id"] = str(chapter_id)
    marker = {"hash": content_hash(cdir), "chapter_id": str(chapter_id),
              "synced_at": now.isoformat(timespec="seconds"), **summary}
    tmp = cdir / f".{MARKER}.tmp"
    tmp.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    os.replace(tmp, cdir / MARKER)
    return summary
