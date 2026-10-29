#!/usr/bin/env python3
"""NCERT → NotebookLM batch pipeline.

Two phases, fully resumable, with per-chapter / per-artifact tracking in a
local CSV (``progress.csv``) that mirrors the source Google Sheet and adds
tracking columns.

    python run.py sync       # pull the sheet into progress.csv (adds new rows)
    python run.py generate   # phase 1: upload PDFs + generate artifacts
    python run.py download   # phase 2: download generated artifacts to disk
    python run.py status     # print a progress summary
    python run.py all        # generate, then download

Phase 1 creates one NotebookLM notebook per (class, subject, chapter), uploads
the chapter PDF as a source, and generates the configured artifacts. Chapters
are processed CONCURRENTLY (bounded by --concurrency) so we never block one
chapter waiting on another. Every step's outcome is written back to
progress.csv immediately, so the run can be interrupted and re-run at any time:
finished work is skipped, failed work is retried up to --max-attempts.

Phase 2 downloads every completed artifact into:

    output/<Class>/<Subject>/<Chapter>/
        source.pdf
        quiz.json
        flashcards.json
        mind_map.json
        slides.pdf
        audio.m4a
        audio_video.mp4       (audio over a title card, only for YouTube)
        cinematic_video.mp4
        infographic.png

With the default --download-now, each artifact is also downloaded the moment
it finishes generating, so `download` is only needed as a catch-up/repair pass.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import mimetypes
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from dotenv import load_dotenv
import audio_video
import drive_upload
import mongo_sync
import youtube_upload
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

from notebooklm import (
    ArtifactType,
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    NotebookLMClient,
    QuizDifficulty,
    QuizQuantity,
    RateLimitError,
    RPCError,
    SlideDeckFormat,
    SlideDeckLength,
    SourceNotFoundError,
    SourceProcessingError,
    SourceStatus,
    SourceTimeoutError,
    UsageActionKind,
    UsageWindowKind,
)

console = Console(highlight=False)  # colours only on a real terminal

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # secrets + settings; see .env.example
OUTPUT_DIR = HERE / "output"
PROGRESS_CSV = HERE / "progress.csv"
LOG_FILE = HERE / "pipeline.log"

LANGUAGE = "en"

# Studio "Customize" defaults — mirror the settings picked in the NotebookLM UI.
QUIZ_QUANTITY = QuizQuantity.MORE
QUIZ_DIFFICULTY = QuizDifficulty.MEDIUM
# The backend stores multiple-choice questions only; anything else is dropped
# when saving to MongoDB, so ask NotebookLM not to make it.
QUIZ_INSTRUCTIONS = ("Only multiple-choice questions, each with exactly four options and exactly "
                     "one correct answer. No multiple-select, fill-in-the-blank or short-answer "
                     "questions.")
FLASHCARDS_QUANTITY = QuizQuantity.MORE
FLASHCARDS_DIFFICULTY = QuizDifficulty.MEDIUM
SLIDE_DECK_FORMAT = SlideDeckFormat.DETAILED_DECK
SLIDE_DECK_LENGTH = SlideDeckLength.DEFAULT
AUDIO_FORMAT = AudioFormat.DEEP_DIVE
AUDIO_LENGTH = AudioLength.DEFAULT
AUDIO_INSTRUCTIONS = """\
The listener is a school-going student from India.

Wherever needed, use Indian examples from Indian context and demography.

The flow of the material should resonate with the flow of the material attached.

For parts which seem critical and relevant from exam point of view, the hosts can emphasize them explicitly.

In the end, include a 2 minute crash course of whatever is covered in the audio."""
INFOGRAPHIC_STYLE = InfographicStyle.INSTRUCTIONAL
INFOGRAPHIC_ORIENTATION = InfographicOrientation.PORTRAIT
INFOGRAPHIC_DETAIL = InfographicDetail.STANDARD

CHAPTER_THUMBNAIL_INSTRUCTIONS = (
    "Create a bold, visually striking 16:9 chapter cover thumbnail. "
    "Focus on the core visual concept or theme of this chapter in a clean, modern, minimalist educational style. "
    "Ensure high contrast, clean visual composition, and prominent, readable chapter theme suited for a course cover."
)

# Which artifacts to generate per chapter. Any subset of ARTIFACT_SPECS keys.
ARTIFACTS: list[str] = [
    "quiz",
    "flashcards",
    "mind_map",
    "slide_deck",
    "audio",
    "cinematic_video",
    "infographic",
]

# Download formats. Each listed format is saved as its own file (e.g. adding
# "html" to quiz saves quiz.json + quiz.html). Downloads are free — they don't
# touch the usage meter. Artifacts not listed get their single default file.
DOWNLOAD_FORMATS: dict[str, list[str]] = {
    "quiz": ["json"],
    "flashcards": ["json"],
    "slide_deck": ["pdf"],
}
FORMAT_EXT = {"json": "json", "html": "html", "markdown": "md", "pdf": "pdf", "pptx": "pptx"}

# Google Drive mirrors output/ for documents and images (PDFs, thumbnails,
# infographics). JSON files, audio, and video files are excluded from Drive uploads
# (videos/audio go to YouTube; JSON quiz/flashcards/mind maps are saved to MongoDB).
# Set DRIVE_EXTS to a set of extensions to upload only those; None = all allowed.
DRIVE_EXTS: set[str] | None = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
DRIVE_MANIFEST = "drive.json"

# File extensions strictly forbidden from Drive uploads
DRIVE_FORBIDDEN_EXTS: set[str] = {
    # JSON files
    ".json",
    # Audio files
    ".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".wma", ".opus",
    # Video files
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".3gp",
}

# Videos go to YouTube (public/unlisted/private from YOUTUBE_PRIVACY in .env);
# each chapter folder gets a local youtube.json with every video's id and links,
# and MongoDB saves them on the matching resource.
# Each upload costs ~1,600 of the 10,000 daily API quota units.
YOUTUBE_FILES: dict[str, str] = {
    "cinematic_video.mp4": "Cinematic Video",
    # audio.m4a over a title card (audio_video.py) — YouTube won't take audio alone.
    audio_video.VIDEO: "Audio Overview",
}
YOUTUBE_MANIFEST = "youtube.json"


# Title suffix telling a chapter's videos apart in the channel's list.
YOUTUBE_LABELS = {"Cinematic Video": "Cinematic", "Audio Overview": "Audio"}


def youtube_playlist(row: dict[str, str]) -> tuple[str, str]:
    """Title and description of the class+subject playlist a chapter's videos go in."""
    cls, subj = row["class"].strip(), row["subject"].strip()
    return (f"NCERT {cls} {subj}",
            f"Chapter-wise audio and video overviews for NCERT {cls} {subj}.")


# Classes up to this one are uploaded as "made for kids" (YouTube's COPPA flag:
# no comments, no personalised ads). YouTube may override it either way.
YOUTUBE_KIDS_MAX_CLASS = int(os.getenv("YOUTUBE_KIDS_MAX_CLASS") or 5)


def youtube_made_for_kids(row: dict[str, str]) -> bool:
    n = re.search(r"\d+", row.get("class", ""))
    return bool(n) and int(n.group()) <= YOUTUBE_KIDS_MAX_CLASS


def _ch_no(text: str) -> int:
    n = re.search(r"\d+", text)
    return int(n.group()) if n else 0


def youtube_sort_key(row: dict[str, str], kind: str) -> tuple[str, int, int]:
    """Playlist order: book, then chapter number, then Video → Cinematic → Audio."""
    book = (row.get("book") or "").strip()
    book = "" if book == row["subject"].strip() else book
    kinds = list(YOUTUBE_LABELS)
    return (book.casefold(), _ch_no(row["chapter"]),
            kinds.index(kind) if kind in kinds else len(kinds))


def youtube_key_of(snippet: dict[str, Any]) -> tuple[str, int, int] | None:
    """Sort key of a playlist item, read back from the description youtube_meta wrote."""
    lines = (snippet.get("description") or "").splitlines()
    chapter = next((ln for ln in lines if ln.startswith("Chapter: ")), None)
    if not lines or chapter is None:
        return None  # not one of ours — leave where it is
    book = next((ln[len("Book: "):] for ln in lines if ln.startswith("Book: ")), "")
    kind = lines[0].rsplit(" — ", 1)[-1]
    kinds = list(YOUTUBE_LABELS)
    return (book.strip().casefold(), _ch_no(chapter.split(" — ")[0]),
            kinds.index(kind) if kind in kinds else len(kinds))


def _hashtag(text: str) -> str:
    """'Chemistry Part-I' → 'ChemistryPartI'; keeps Hindi vowel signs (सूरदास → सूरदास)."""
    words = ("".join(c for c in w if c.isalnum() or unicodedata.category(c).startswith("M"))
             for w in text.split())
    return "".join(w[:1].upper() + w[1:] for w in words)


def youtube_meta(row: dict[str, str], kind: str) -> tuple[str, str, list[str]]:
    """Title, description and tags for one chapter video.

    Title is the chapter title plus where it sits and what it is ("… | Class 11
    · Chemistry · Chapter 1 (Audio)"), shortened to fit YouTube's 100
    characters. The description adds the book and hashtags (no links).
    """
    cls, subj, ch = row["class"].strip(), row["subject"].strip(), row["chapter"].strip()
    book = (row.get("book") or "").strip()
    name = (row.get("chapter_title") or "").strip()
    label = f" ({YOUTUBE_LABELS.get(kind, kind)})"
    place = f"{cls} · {subj} · {ch}"
    ch_no = re.sub(r"\D", "", ch) or ch
    short = f"{cls} {subj} Ch {ch_no}"
    title = next((t for t in (f"{name} | {place}{label}", f"{name} | {short}{label}",
                              f"{name}{label}") if len(t) <= 100),
                 name) if name else place + label
    lines = [f"{name} — {kind}" if name else kind, "",
             f"Class: {cls}", f"Subject: {subj}"]
    if book and book != subj:
        lines.append(f"Book: {book}")
    lines.append(f"Chapter: {ch}" + (f" — {name}" if name else ""))
    lines += ["", " ".join(dict.fromkeys(
        "#" + _hashtag(t) for t in ("NCERT", cls, subj, book, name) if t))]
    tags = [t for t in dict.fromkeys(["NCERT", cls, subj, book, ch, name, kind,
                                      f"NCERT {cls} {subj}"]) if t]
    return title, "\n".join(lines), tags

# Defaults (overridable on the CLI).
DEFAULT_CONCURRENCY = 3          # chapters generated in parallel
DEFAULT_MAX_ATTEMPTS = 3         # retries per row before giving up
SOURCE_READY_TIMEOUT = 600.0     # wait for PDF to finish indexing

# --------------------------------------------------------------------------- #
# Artifact registry: how to generate + download each type.
#   generate: coroutine fn(client, notebook_id, source_ids) -> id_str
#   download: coroutine fn(client, notebook_id, out_path, artifact_id) -> path
#   filename: output file name inside the chapter folder
#   timeout : seconds to wait for generation to complete
# --------------------------------------------------------------------------- #


async def _gen_study_guide(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_study_guide(nb, sids, LANGUAGE)
    return s.task_id


async def _gen_quiz(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_quiz(
        nb, sids, instructions=QUIZ_INSTRUCTIONS, quantity=QUIZ_QUANTITY,
        difficulty=QUIZ_DIFFICULTY,
    )
    return s.task_id


async def _gen_flashcards(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_flashcards(
        nb, sids, quantity=FLASHCARDS_QUANTITY, difficulty=FLASHCARDS_DIFFICULTY
    )
    return s.task_id


async def _gen_audio(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_audio(
        nb, sids, LANGUAGE,
        instructions=AUDIO_INSTRUCTIONS,
        audio_format=AUDIO_FORMAT,
        audio_length=AUDIO_LENGTH,
    )
    return s.task_id


async def _gen_video(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_video(nb, sids, LANGUAGE)
    return s.task_id


async def _gen_cinematic_video(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    # Veo 3 cinematic video — ~30-40 min, requires a Google AI Ultra subscription.
    s = await c.artifacts.generate_cinematic_video(nb, sids, LANGUAGE)
    return s.task_id


async def _gen_infographic(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_infographic(
        nb, sids, LANGUAGE,
        orientation=INFOGRAPHIC_ORIENTATION,
        detail_level=INFOGRAPHIC_DETAIL,
        style=INFOGRAPHIC_STYLE,
    )
    return s.task_id


async def _gen_chapter_thumbnail(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_infographic(
        nb, sids, LANGUAGE,
        instructions=CHAPTER_THUMBNAIL_INSTRUCTIONS,
        orientation=InfographicOrientation.LANDSCAPE,
        detail_level=InfographicDetail.CONCISE,
        style=InfographicStyle.PROFESSIONAL,
    )
    return s.task_id


async def _gen_slide_deck(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_slide_deck(
        nb, sids, LANGUAGE,
        slide_format=SLIDE_DECK_FORMAT,
        slide_length=SLIDE_DECK_LENGTH,
    )
    return s.task_id


async def _gen_data_table(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    s = await c.artifacts.generate_data_table(
        nb, sids, LANGUAGE,
        instructions="Extract the key facts, definitions and figures as a table.",
    )
    return s.task_id


async def _gen_mind_map(c: NotebookLMClient, nb: str, sids: list[str]) -> str:
    # Synchronous-style: returns a MindMapResult, no completion polling needed.
    r = await c.artifacts.generate_mind_map(nb, sids, LANGUAGE)
    return getattr(r, "note_id", None) or "mind_map"


ARTIFACT_SPECS: dict[str, dict[str, Any]] = {
    "study_guide": {
        "generate": _gen_study_guide,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_report(nb, p, aid),
        "filename": "study_guide.md",
        "timeout": 900.0,
        "sync_gen": False,
        "type": ArtifactType.REPORT,
        "report_subtype": "study_guide",
    },
    "quiz": {
        "generate": _gen_quiz,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_quiz(nb, p, aid, fmt or "json"),
        "filename": "quiz.json",
        "timeout": 900.0,
        "sync_gen": False,
        "type": ArtifactType.QUIZ,
    },
    "flashcards": {
        "generate": _gen_flashcards,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_flashcards(
            nb, p, aid, fmt or "json"
        ),
        "filename": "flashcards.json",
        "timeout": 900.0,
        "sync_gen": False,
        "type": ArtifactType.FLASHCARDS,
    },
    "mind_map": {
        "generate": _gen_mind_map,
        # note-backed mind map: auto-pick by passing artifact_id=None
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_mind_map(nb, p, None),
        "filename": "mind_map.json",
        "timeout": 0.0,
        "sync_gen": True,
        "type": ArtifactType.MIND_MAP,
    },
    "audio": {
        "generate": _gen_audio,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_audio(nb, p, aid),
        "filename": "audio.m4a",  # AAC in an MP4 container, not MP3
        "timeout": 1500.0,
        "sync_gen": False,
        "type": ArtifactType.AUDIO,
    },
    "video": {
        "generate": _gen_video,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_video(nb, p, aid),
        "filename": "video.mp4",
        "timeout": 2700.0,
        "sync_gen": False,
        "type": ArtifactType.VIDEO,
    },
    "cinematic_video": {
        "generate": _gen_cinematic_video,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_video(nb, p, aid),
        "filename": "cinematic_video.mp4",
        "timeout": 3600.0,  # Veo 3: ~30-40 min
        "sync_gen": False,
        "type": ArtifactType.VIDEO,
    },
    "infographic": {
        "generate": _gen_infographic,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_infographic(nb, p, aid),
        "filename": "infographic.png",
        "timeout": 1200.0,
        "sync_gen": False,
        "type": ArtifactType.INFOGRAPHIC,
        "filter_artifact": lambda a: not _is_landscape_infographic(a),
    },
    "chapter_thumbnail": {
        "generate": _gen_chapter_thumbnail,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_infographic(nb, p, aid),
        "filename": "chapter_thumbnail.png",
        "timeout": 1200.0,
        "sync_gen": False,
        "type": ArtifactType.INFOGRAPHIC,
        "filter_artifact": lambda a: _is_landscape_infographic(a),
    },
    "slide_deck": {
        "generate": _gen_slide_deck,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_slide_deck(nb, p, aid, fmt or "pdf"),
        "filename": "slides.pdf",
        "timeout": 1200.0,
        "sync_gen": False,
        "type": ArtifactType.SLIDE_DECK,
    },
    "data_table": {
        "generate": _gen_data_table,
        "download": lambda c, nb, p, aid, fmt=None: c.artifacts.download_data_table(nb, p, aid),
        "filename": "data_table.csv",
        "timeout": 900.0,
        "sync_gen": False,
        "type": ArtifactType.DATA_TABLE,
    },
}

# Which live-usage-meter category each artifact is charged against
# (`notebooklm usage --categories`). Used to skip an artifact up front when
# Google reports insufficient quota, instead of firing it and watching it fail.
USAGE_KIND: dict[str, UsageActionKind] = {
    "study_guide": UsageActionKind.REPORTS,
    "quiz": UsageActionKind.QUIZ,
    "flashcards": UsageActionKind.FLASHCARDS,
    "mind_map": UsageActionKind.MINDMAP,
    "audio": UsageActionKind.AUDIO_OVERVIEW,
    "video": UsageActionKind.VIDEO_OVERVIEW,
    "cinematic_video": UsageActionKind.BREAKDOWNS_VIDEO,  # meter code 3 = cinematic
    "infographic": UsageActionKind.INFOGRAPHIC,
    "chapter_thumbnail": UsageActionKind.INFOGRAPHIC,
    "slide_deck": UsageActionKind.SLIDES,
    "data_table": UsageActionKind.TABLES,
}

# Subscription tiers (AccountLimits.tier) that cannot generate cinematic video.
# Opaque ids, not a ranking: 1=Standard/Free, 4=Plus (see docs/quota-limits.md).
NO_CINEMATIC_TIERS = {1, 4}

# --------------------------------------------------------------------------- #
# CSV schema. Tracking columns cover only the ACTIVE artifacts (ARTIFACTS),
# so the tracker stays lean. If you add/remove an artifact from ARTIFACTS the
# schema follows on the next write (old columns are dropped, new ones added).
# --------------------------------------------------------------------------- #

SHEET_COLS = ["class", "subject", "book", "chapter", "pdf_url", "book_index_url", "thumbnail_url"]
BASE_COLS = [
    "notebook_id", "source_id", "chapter_title",
    "gen_status", "gen_attempts", "gen_error",
    "dl_status", "dl_attempts", "dl_error",
    "updated_at",
]


def artifact_cols() -> list[str]:
    cols: list[str] = []
    for name in ARTIFACTS:  # only the artifacts we actually generate
        cols += [f"{name}_id", f"{name}_gen", f"{name}_dl"]
    return cols


FIELDNAMES = SHEET_COLS + BASE_COLS + artifact_cols()

log = logging.getLogger("pipeline")

LOCK_FILE = HERE / ".pipeline.lock"


# --------------------------------------------------------------------------- #
# Single-run lock: prevents two `generate`/`download` processes from sharing
# (and clobbering) progress.csv at the same time.
# --------------------------------------------------------------------------- #


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def acquire_lock() -> None:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip() or "0")
        except ValueError:
            pid = 0
        if _pid_alive(pid):
            sys.exit(
                f"Another pipeline run is active (PID {pid}). Wait for it to finish, "
                f"or stop it. If you're certain none is running, delete {LOCK_FILE}."
            )
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# State store: progress.csv as the single source of truth
# --------------------------------------------------------------------------- #


def row_key(row: dict[str, str]) -> str:
    return f"{row['class'].strip()}|{row['subject'].strip()}|{row['chapter'].strip()}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """In-memory mirror of progress.csv with atomic, lock-guarded writes."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    def load(self) -> None:
        if not PROGRESS_CSV.exists():
            return
        with PROGRESS_CSV.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                # Backfill any new columns added since the file was written.
                for col in FIELDNAMES:
                    r.setdefault(col, "")
                self.rows[row_key(r)] = r

    def sync_from_sheet(self) -> int:
        """Pull the sheet; add new rows, refresh sheet-derived columns. Returns #added."""
        sheet_id = os.getenv("SHEET_ID", "").strip()
        if not sheet_id:
            raise RuntimeError("SHEET_ID is not set — add it to pipeline/.env (see .env.example)")
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        resp = httpx.get(url, follow_redirects=True, timeout=60.0)
        resp.raise_for_status()
        reader = csv.DictReader(resp.text.splitlines())
        added = 0
        for raw in reader:
            row = {
                "class": (raw.get("Class") or "").strip(),
                "subject": (raw.get("Subject") or "").strip(),
                "book": (raw.get("Book") or "").strip(),
                "chapter": (raw.get("Chapter") or "").strip(),
                "pdf_url": (raw.get("PDF URL") or "").strip(),
                "book_index_url": (raw.get("Book Index URL") or "").strip(),
                "thumbnail_url": (raw.get("Thumbnail URL") or raw.get("thumbnail_url") or "").strip(),
            }
            if not row["class"] or not row["chapter"]:
                continue
            key = row_key(row)
            if key in self.rows:
                self.rows[key].update(row)  # refresh sheet columns
                continue
            new = {c: "" for c in FIELDNAMES}
            new.update(row)
            new["gen_status"] = "pending"
            new["dl_status"] = "pending"
            new["gen_attempts"] = "0"
            new["dl_attempts"] = "0"
            self.rows[key] = new
            added += 1
        self._write()
        return added

    def _write(self) -> None:
        PROGRESS_CSV.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROGRESS_CSV.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            w.writeheader()
            for r in self.rows.values():
                w.writerow(r)
        os.replace(tmp, PROGRESS_CSV)

    async def save(self) -> None:
        async with self._lock:
            self._write()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def chapter_dir(row: dict[str, str]) -> Path:
    return OUTPUT_DIR / row["class"] / row["subject"] / row["chapter"]


_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _url_candidates(url: str) -> list[str]:
    """Return host variants to try. Some hosts (e.g. ncert.nic.in) reset the
    bare apex but serve fine on the www. subdomain — try both."""
    if "ncert.nic.in" in url and "://www." not in url:
        return [url.replace("://", "://www.", 1), url]
    cands = [url]
    if "://www." in url:
        cands.append(url.replace("://www.", "://", 1))
    else:
        cands.append(url.replace("://", "://www.", 1))
    return cands


async def download_file(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": _UA}
    last_err: Exception | None = None
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0, headers=headers) as hc:
        for cand in _url_candidates(url):
            try:
                async with hc.stream("GET", cand) as resp:
                    resp.raise_for_status()
                    with tmp.open("wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)
                os.replace(tmp, dest)
                return
            except Exception as e:  # noqa: BLE001 — try next candidate
                last_err = e
                tmp.unlink(missing_ok=True)
    raise RuntimeError(f"Download failed for {url}: {type(last_err).__name__}: {last_err}")


download_pdf = download_file
download_thumbnail = download_file


def extract_video_thumbnail(cdir: Path) -> Path | None:
    """Extract a 16:9 video frame from the best available video in cdir using ffmpeg.

    Only extracts when a cinematic video overview exists (never from audio_video.mp4).
    Saves to cdir / "video_thumbnail.jpg".
    """
    target_vid = cdir / "cinematic_video.mp4"
    if not (target_vid.is_file() and target_vid.stat().st_size > 0):
        return None
    out = cdir / "video_thumbnail.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        audio_video.extract_frame(target_vid, out)
        log.info("🎞 Extracted video frame %s → %s", target_vid.name, out.name)
        return out
    except Exception as e:
        log.debug("Frame extraction from %s failed: %s", target_vid.name, e)
    return None


def extract_chapter_thumbnail(cdir: Path, force: bool = False) -> Path | None:
    """Extract page 1 of the chapter source PDF as chapter_thumbnail.png.

    Uses pypdfium2 for high-fidelity rendering of the chapter title/cover page.
    Saves to cdir / "chapter_thumbnail.png".
    """
    pdf_path = cdir / "source.pdf"
    if not (pdf_path.is_file() and pdf_path.stat().st_size > 0):
        return None
    out = cdir / "chapter_thumbnail.png"
    if out.exists() and out.stat().st_size > 0 and not force:
        return out
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        if len(pdf) < 1:
            return None
        page = pdf[0]
        # scale=2.0 renders at ~144 DPI (sharp, high-DPI rendering for web & mobile)
        image = page.render(scale=2.0).to_pil()
        image.save(out, "PNG", optimize=True)
        log.info("📄 Extracted PDF page 1 → %s (%s)", out.name, f"{out.stat().st_size / 1024:.0f} KB")
        return out
    except Exception as e:
        log.warning("[%s] Failed to extract PDF page 1: %s", cdir.name, e)
        return None


def _is_landscape_infographic(a: Any) -> bool:
    for ig in getattr(a, "infographics", ()):
        if ig.width and ig.height:
            return ig.width > ig.height
    text = f"{getattr(a, 'title', '')} {getattr(a, 'generation_prompt', '')}".lower()
    return "thumbnail" in text or "landscape" in text or "cover" in text


async def find_existing_artifact(
    client: NotebookLMClient, nb_id: str, spec: dict[str, Any], known_id: str | None = None
) -> Any | None:
    """Ask NotebookLM whether this artifact already exists in the notebook.

    Guards against duplicate generation when a previous run was interrupted
    mid-flight (progress.csv not yet updated). Returns the artifact to *adopt*:
    a completed one, or one still processing/pending (so we wait on it instead
    of firing a second generation). Returns None when only failed/no artifacts
    exist, so a fresh generation (i.e. a real retry) proceeds.
    """
    arts = await client.artifacts.list(nb_id, spec["type"])
    if known_id:
        match = next((a for a in arts if a.id == known_id), None)
        if match:
            return match
    sub = spec.get("report_subtype")
    if sub:
        arts = [a for a in arts if getattr(a, "report_subtype", None) == sub]
    filter_fn = spec.get("filter_artifact")
    if filter_fn:
        arts = [a for a in arts if filter_fn(a)]
    if not arts:
        return None
    completed = [a for a in arts if a.is_completed]
    if completed:
        return completed[0]
    in_flight = [a for a in arts if a.status in (1, 2)]  # 1=processing, 2=pending
    return in_flight[0] if in_flight else None


_RL_MARKERS = (
    "rate limit", "ratelimit", "rate-limit", "quota", "resource_exhausted",
    "resource exhausted", "too many requests", "user_displayable_error", "429",
)


TITLE_PROMPT = ("What is the exact title of this chapter as printed in the textbook? "
                "Reply with the title only — no chapter/unit number, no quotes, no explanation.")
_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to",
                "with"}


def clean_title(answer: str) -> str:
    """NotebookLM's chat answer → a bare chapter title ('' if it isn't one)."""
    strip = " \t\"'“”‘’*_."
    t = re.sub(r"\s*\[[\d,\s\-–]+\]", "", answer or "").strip(strip)  # citations: [1, 2]
    t = re.sub(r"^(chapter|unit|lesson)\s+[\dIVXivx]+\s*[:.\-–—]?\s*", "", t, flags=re.IGNORECASE)
    t = t.strip(strip)
    if not t or "\n" in t or len(t) > 120:
        return ""  # a sentence or a refusal, not a title
    if t.isupper():  # printed in capitals in the book
        words = t.lower().split()
        t = " ".join(w if i and w in _SMALL_WORDS else w[:1].upper() + w[1:]
                     for i, w in enumerate(words))
    return t


async def ensure_chapter_title(client: NotebookLMClient, store: Store, row: dict[str, str],
                               key: str) -> None:
    """Ask the notebook for the chapter's real name, once. Never raises — the
    MongoDB save falls back to the mind map's root when this is empty."""
    if row.get("chapter_title") or not row.get("notebook_id"):
        return
    try:
        res = await client.chat.ask(row["notebook_id"], TITLE_PROMPT)
    except Exception as e:  # noqa: BLE001 — retried on the next run
        log.warning("[%s] chapter title: ask failed (%s)", key, e)
        return
    title = clean_title(res.answer)
    if not title:
        log.warning("[%s] chapter title: unusable answer %r", key, (res.answer or "")[:80])
        return
    row["chapter_title"] = title
    await store.save()
    log.info("[%s] chapter title: \"%s\"", key, title)


def _looks_rate_limited(e: Exception) -> bool:
    """Heuristic: did this exception come from a Google rate limit / quota cap?"""
    if isinstance(e, RateLimitError):
        return True
    code = getattr(e, "error_code", None) or getattr(e, "code", None) or ""
    text = f"{code} {e}".lower()
    return any(m in text for m in _RL_MARKERS)


def roll_up(row: dict[str, str], kind: str, active: list[str]) -> str:
    """Overall status from per-artifact cells:
    'done' (all done) > 'partial' (some done) > 'rate_limited' (transient, retryable)
    > 'failed' (a real failure)."""
    field = "_gen" if kind == "gen" else "_dl"
    states = [row.get(f"{a}{field}", "") for a in active]
    if states and all(s == "done" for s in states):
        return "done"
    if any(s == "done" for s in states):
        return "partial"
    if any(s == "rate_limited" for s in states) and not any(s == "failed" for s in states):
        return "rate_limited"
    return "failed"


# --------------------------------------------------------------------------- #
# Live usage meter (quota gate)
# --------------------------------------------------------------------------- #


IST = timezone(timedelta(hours=5, minutes=30))
USAGE_EVERY = 120.0  # seconds between live usage tables during `generate` (0 = off)


def _ist(ts: datetime) -> str:
    """Render a UTC reset time in IST for the log."""
    return ts.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


def _reset_label(ts: datetime) -> str:
    """'09:12 IST · in 4h 10m' today, '3 Oct 04:12 IST · in 6d 23h' otherwise."""
    local, now_ist = ts.astimezone(IST), datetime.now(IST)
    when = local.strftime("%H:%M IST") if local.date() == now_ist.date() \
        else local.strftime("%-d %b %H:%M IST")
    mins = max(0, int((local - now_ist).total_seconds() // 60))
    days, rem = divmod(mins, 1440)
    eta = f"{days}d {rem // 60}h" if days else f"{rem // 60}h {rem % 60:02d}m"
    return f"{when} · in {eta}"


class QuotaGate:
    """Serialises "check the usage meter → kick off a generation" across chapters.

    With --concurrency > 1, several chapters could otherwise read "sufficient"
    at the same moment and all fire an expensive artifact, overdrawing the
    budget together (how three slide decks failed at once). Holding one lock
    around check + kickoff makes each chapter see the meter after the previous
    kickoff. Waiting for completion happens outside the lock.

    Fails open: if the meter is disabled/unavailable or the call errors, the
    artifact is allowed and Google's own refusal handling takes over.
    """

    def __init__(self, client: NotebookLMClient) -> None:
        self.client = client
        self.lock = asyncio.Lock()

    async def insufficient(self, name: str) -> str | None:
        """Return a reason string if Google says there is no quota for `name`.

        Caller must hold ``self.lock``.
        """
        kind = USAGE_KIND.get(name)
        if kind is None:
            return None
        try:
            usage = await self.client.settings.get_usage()
        except Exception as e:  # noqa: BLE001 — meter is advisory; never block on it
            log.debug("usage meter unavailable (%s) — not gating %s", e, name)
            return None
        if not usage.available:
            return None
        action = usage.action(kind)
        if action is None or action.has_sufficient_quota:
            return None
        window = usage.active_window
        cost = action.estimated_cost_percent
        reason = f"insufficient quota for {name}"
        if cost is not None:
            reason += f" (needs ~{cost:.1f}%"
            if window is not None:
                reason += f", {window.remaining_percent:.1f}% left"
            reason += ")"
        if window is not None:
            reason += f"; resets {_ist(window.resets_at)}"
        return reason


def _bar(used: float, width: int = 20) -> Text:
    filled = max(0, min(width, round(used / 100 * width)))
    style = "green" if used < 60 else "yellow" if used < 90 else "red"
    bar = Text("█" * filled, style=style)
    bar.append("░" * (width - filled), style="bright_black")
    return bar


def show_usage(usage: Any, active: list[str]) -> None:
    """Two tables: the usage windows, then what each active artifact costs."""
    windows = Table(title=f"NotebookLM usage · {datetime.now(IST).strftime('%H:%M:%S')}",
                    title_style="bold", header_style="bold", border_style="bright_black")
    windows.add_column("window")
    windows.add_column("used", justify="right")
    windows.add_column("", no_wrap=True)
    windows.add_column("left", justify="right")
    windows.add_column("resets", no_wrap=True)
    plain = []
    for kind in (UsageWindowKind.FIVE_HOUR, UsageWindowKind.WEEKLY):
        w = usage.window(kind)
        if w is None:
            continue
        label = kind.name.lower().replace("_", "-")
        style = "green" if w.used_percent < 60 else "yellow" if w.used_percent < 90 else "bold red"
        windows.add_row(label, Text(f"{w.used_percent:.1f}%", style=style), _bar(w.used_percent),
                        f"{w.remaining_percent:.1f}%", _reset_label(w.resets_at))
        plain.append(f"usage {label}: {w.used_percent:.1f}% used, "
                     f"{w.remaining_percent:.1f}% left, resets {_ist(w.resets_at)}")
    show_table(windows, plain)

    active_w = usage.active_window
    left = active_w.remaining_percent if active_w is not None else None
    costs = Table(header_style="bold", border_style="bright_black")
    costs.add_column("artifact")
    costs.add_column("cost", justify="right")
    costs.add_column("quota", justify="center")
    costs.add_column("fits now", justify="right")
    plain, total = [], 0.0
    for name in active:
        kind = USAGE_KIND.get(name)
        a = usage.action(kind) if kind is not None else None
        if a is None:
            continue
        cost = a.estimated_cost_percent
        total += cost or 0.0
        n = 0 if not cost or left is None else int(left // cost)
        if a.has_sufficient_quota:
            n = max(n, 1)  # Google's own verdict wins over our estimate
        fits = "?" if not cost or left is None else f"×{n}"
        costs.add_row(name, f"{cost:.1f}%" if cost is not None else "?",
                      Text("ok", style="green") if a.has_sufficient_quota
                      else Text("insufficient", style="bold red"),
                      fits)
        plain.append(f"  {name:<16} cost~{cost if cost is not None else '?'}%  "
                     f"{'ok' if a.has_sufficient_quota else 'INSUFFICIENT'}")
    costs.caption = f"1 chapter ≈ {total:.0f}% of window"
    show_table(costs, plain)


async def usage_monitor(client: NotebookLMClient, active: list[str], every: float) -> None:
    """Re-print the usage tables every `every` seconds until cancelled."""
    while True:
        await asyncio.sleep(every)
        try:
            usage = await client.settings.get_usage()
        except Exception as e:  # noqa: BLE001 — a missed refresh is harmless
            log.debug("usage refresh failed: %s", e)
            continue
        if usage.available:
            show_usage(usage, active)


async def preflight(client: NotebookLMClient, active: list[str], n_chapters: int,
                    concurrency: int) -> bool:
    """Log plan tier + live usage before a generate run.

    Returns False (abort the run) only when the meter says the active window
    is fully exhausted — nothing could be generated until it resets.
    """
    fields: list[tuple[str, Any]] = [
        ("chapters", f"{n_chapters}  ({concurrency} in parallel)"),
        ("artifacts", artifact_list(active)),
    ]
    limits = None
    try:
        limits = await client.settings.get_account_limits()
        plan = TIER_NAMES.get(limits.tier or 0, f"tier {limits.tier}")
        fields.append(("account", Text.assemble(
            (plan, "bold green"),
            (f"  ·  {limits.notebook_limit} notebooks  ·  "
             f"{limits.source_limit} sources/notebook", "")
        )))
    except Exception as e:  # noqa: BLE001 — informational only
        fields.append(("account", Text(f"unavailable ({e})", style="yellow")))
    show_header("Generate", fields)

    try:
        if limits is None:
            raise RuntimeError("no limits")
        if "cinematic_video" in active and limits.tier in NO_CINEMATIC_TIERS:
            log.warning("%s plan cannot generate cinematic_video — it will be skipped; "
                        "use 'video' instead (--artifacts ...)",
                        TIER_NAMES.get(limits.tier or 0, limits.tier))
    except RuntimeError:
        pass

    try:
        usage = await client.settings.get_usage()
    except Exception as e:  # noqa: BLE001 — informational only
        log.warning("could not read usage meter: %s", e)
        return True
    if not usage.available:
        log.info("usage meter %s — not gating on quota", usage.status.value)
        return True
    show_usage(usage, active)
    if usage.is_exhausted:
        w = usage.active_window
        log.error("usage window exhausted — nothing can be generated until %s",
                  _ist(w.resets_at) if w is not None else "it resets")
        return False
    return True


# --------------------------------------------------------------------------- #
# Phase 1: generate
# --------------------------------------------------------------------------- #


async def _kickoff(
    client: NotebookLMClient,
    nb_id: str,
    sids: list[str],
    name: str,
    spec: dict[str, Any],
    prev_id: str,
    key: str,
) -> str:
    """Start generation for one artifact and return its id.

    If a previous attempt left an artifact id (and it wasn't adopted as
    completed / in flight, so it failed or vanished), retry it IN PLACE — the
    UI "Retry" action — so the notebook keeps one entry per artifact and the id
    in progress.csv stays stable. A quota refusal raises RateLimitError, which
    the caller records as rate_limited. Any other refusal (not retryable,
    artifact gone) falls back to a fresh generation.
    """
    if prev_id and not spec["sync_gen"]:
        try:
            st = await client.artifacts.retry_failed(nb_id, prev_id)
            log.info("[%s] retrying %s in place (%s)", key, name, sid(st.task_id))
            return st.task_id
        except RateLimitError:
            raise
        except RPCError as e:
            log.info("[%s] %s retry-in-place refused (%s) — generating fresh", key, name, e)
    log.info("[%s] generating %s", key, name)
    return await spec["generate"](client, nb_id, sids)


async def generate_row(
    client: NotebookLMClient,
    store: Store,
    row: dict[str, str],
    active: list[str],
    sem: asyncio.Semaphore,
    max_attempts: int,
    nb_index: dict[str, str],
    gate: QuotaGate,
    download_now: bool = True,
) -> None:
    key = row_key(row)
    async with sem:
        # Skip if everything we want is already generated.
        if all(row.get(f"{a}_gen") == "done" for a in active):
            row["gen_status"] = "done"
            await store.save()
            log.info("[%s] gen already complete — skip", key)
            await ensure_chapter_title(client, store, row, key)
            if download_now and row.get("notebook_id"):
                for a in active:
                    if not download_complete(row, a):
                        await download_artifact(client, store, row, a, key)
                row["dl_status"] = roll_up(row, "dl", active)
                await store.save()
            await sync_backends(row, key)
            return
        # Give up ONLY on a genuinely failed row after max_attempts tries.
        # max_attempts <= 0 means "never give up". rate_limited/partial rows are
        # never skipped here — only a real 'failed'.
        if (
            max_attempts > 0
            and int(row.get("gen_attempts") or 0) >= max_attempts
            and row.get("gen_status") == "failed"
        ):
            log.warning("[%s] gen at max attempts (%s) — skip (raise --max-attempts "
                        "or use 0 for unlimited)", key, max_attempts)
            return

        row["gen_attempts"] = str(int(row.get("gen_attempts") or 0) + 1)
        row["gen_error"] = ""
        row["updated_at"] = now()
        await store.save()

        try:
            # 1. PDF on disk (also lands in the chapter folder for phase 2).
            pdf_path = chapter_dir(row) / "source.pdf"
            log.info("[%s] downloading PDF", key)
            await download_pdf(row["pdf_url"], pdf_path)
            extract_chapter_thumbnail(chapter_dir(row))

            if row.get("thumbnail_url"):
                thumb_path = chapter_dir(row) / "thumbnail.jpg"
                if not (thumb_path.exists() and thumb_path.stat().st_size > 0):
                    try:
                        await download_thumbnail(row["thumbnail_url"], thumb_path)
                    except Exception as e:
                        log.warning("[%s] thumbnail download failed: %s", key, e)

            # 2. Notebook. Reuse our stored id; else adopt an existing notebook
            #    with the same title (recovers from a crash right after create);
            #    else create one.
            title = f"{row['class']} | {row['subject']} | {row['chapter']}"
            if not row.get("notebook_id"):
                if title in nb_index:
                    row["notebook_id"] = nb_index[title]
                    log.info("[%s] adopted existing notebook %s", key, sid(nb_index[title]))
                else:
                    nb = await client.notebooks.create(title)
                    row["notebook_id"] = nb.id
                    nb_index[title] = nb.id
                    log.info("[%s] created notebook %s", key, sid(nb.id))
                await store.save()
            nb_id = row["notebook_id"]

            # 3. Source. Reuse stored id; else adopt an already-uploaded source
            #    in the notebook; else upload. Always confirm it is READY.
            #    If an existing source is in ERROR state or fails waiting,
            #    clean it up and re-upload the PDF.
            source_ready = False
            src_id = row.get("source_id")
            if src_id:
                try:
                    await client.sources.wait_until_ready(
                        nb_id, src_id, timeout=SOURCE_READY_TIMEOUT
                    )
                    source_ready = True
                except (SourceNotFoundError, SourceProcessingError, SourceTimeoutError) as err:
                    log.warning("[%s] stored source %s unusable (%s) — replacing", key, sid(src_id), err)
                    try:
                        await client.sources.delete(nb_id, src_id)
                    except Exception:
                        pass
                    row["source_id"] = ""
                    await store.save()

            if not source_ready:
                existing = await client.sources.list(nb_id)
                for s in existing:
                    if s.status == SourceStatus.READY:
                        row["source_id"] = s.id
                        await store.save()
                        log.info("[%s] adopted existing source %s", key, sid(s.id))
                        source_ready = True
                        break
                    elif s.status == SourceStatus.ERROR:
                        log.warning("[%s] deleting errored source %s", key, sid(s.id))
                        try:
                            await client.sources.delete(nb_id, s.id)
                        except Exception:
                            pass

            if not source_ready:
                log.info("[%s] uploading source", key)
                src = await client.sources.add_file(
                    nb_id, pdf_path, wait=True, wait_timeout=SOURCE_READY_TIMEOUT
                )
                row["source_id"] = src.id
                await store.save()
                await client.sources.wait_until_ready(
                    nb_id, row["source_id"], timeout=SOURCE_READY_TIMEOUT
                )

            log.info("[%s] source ready (%s)", key, sid(row["source_id"]))
            sids = [row["source_id"]]
            await ensure_chapter_title(client, store, row, key)

            # 4. Artifacts (sequential within a chapter; chapters run in parallel).
            #    Before generating, ASK NotebookLM whether the artifact already
            #    exists — adopt a completed one, or wait on one still in flight,
            #    instead of firing a duplicate generation (idempotent on re-run).
            tried_dl: set[str] = set()

            async def download_ready() -> None:
                """--download-now: fetch finished artifacts before starting the next
                one, then mirror any new files to Drive."""
                if download_now:
                    for a in active:
                        if (a not in tried_dl and row.get(f"{a}_gen") == "done"
                                and not download_complete(row, a)):
                            tried_dl.add(a)  # one try per run; `download` phase repairs
                            await download_artifact(client, store, row, a, key)
                await sync_backends(row, key)

            for name in active:
                await download_ready()
                if row.get(f"{name}_gen") == "done":
                    continue
                spec = ARTIFACT_SPECS[name]
                try:
                    existing_art = await find_existing_artifact(client, nb_id, spec, row.get(f"{name}_id"))
                    if existing_art is not None and existing_art.is_completed:
                        row[f"{name}_id"] = existing_art.id
                        row[f"{name}_gen"] = "done"
                        log.info("[%s] %s already present — adopted (%s)",
                                 key, name, sid(existing_art.id))
                        continue

                    if existing_art is not None:
                        art_id = existing_art.id
                        log.info("[%s] %s already in flight — waiting (%s)",
                                 key, name, sid(art_id))
                    else:
                        # Check the meter and kick off under one lock so parallel
                        # chapters can't all spend the same remaining quota.
                        async with gate.lock:
                            reason = await gate.insufficient(name)
                            if reason:
                                row[f"{name}_gen"] = "rate_limited"
                                row["gen_error"] = f"{name}: {reason}"[:500]
                                log.warning("[%s] %s SKIPPED — %s", key, name, reason)
                                continue
                            art_id = await _kickoff(client, nb_id, sids, name, spec,
                                                    row.get(f"{name}_id", ""), key)
                    # Persist the id BEFORE waiting, so a crash mid-wait lets the
                    # next run adopt this artifact instead of regenerating it.
                    row[f"{name}_id"] = art_id or ""
                    await store.save()

                    if not spec["sync_gen"]:
                        final = await client.artifacts.wait_for_completion(
                            nb_id, art_id, timeout=spec["timeout"]
                        )
                        if not final.is_complete:
                            # Rate-limit / quota rejection is transient, not a real
                            # failure — flag it distinctly so it stays retryable and
                            # never counts toward the give-up threshold.
                            if final.is_rate_limited or final.is_removed:
                                row[f"{name}_gen"] = "rate_limited"
                                row["gen_error"] = f"{name}: rate-limited ({final.status})"
                                log.warning("[%s] %s RATE-LIMITED — will retry", key, name)
                                continue
                            raise RuntimeError(
                                f"{name} ended {final.status} "
                                f"({final.error or final.error_code or 'no detail'})"
                            )
                    row[f"{name}_gen"] = "done"
                    log.info("[%s] %s done", key, name)
                except Exception as e:  # noqa: BLE001 — record + continue per artifact
                    if _looks_rate_limited(e):
                        row[f"{name}_gen"] = "rate_limited"
                        row["gen_error"] = f"{name}: rate-limited ({e})"[:500]
                        log.warning("[%s] %s RATE-LIMITED — will retry", key, name)
                    else:
                        row[f"{name}_gen"] = "failed"
                        row[f"{name}_id"] = ""
                        row["gen_error"] = f"{name}: {e}"[:500]
                        log.error("[%s] %s FAILED: %s", key, name, e)
                finally:
                    row["updated_at"] = now()
                    await store.save()

            await download_ready()
            row["gen_status"] = roll_up(row, "gen", active)
            if download_now:
                done_gen = [a for a in active if row.get(f"{a}_gen") == "done"]
                if done_gen:
                    row["dl_status"] = roll_up(row, "dl", done_gen)
        except Exception as e:  # noqa: BLE001 — row-level failure (PDF/notebook/source)
            row["gen_status"] = "failed"
            row["gen_error"] = str(e)[:500]
            log.error("[%s] row FAILED: %s", key, e)
        finally:
            row["updated_at"] = now()
            await store.save()
        log.info("[%s] gen_status=%s", key, row["gen_status"])


# --------------------------------------------------------------------------- #
# Google Drive sync
# --------------------------------------------------------------------------- #


# True when videos go to YouTube (re-set per run by use_youtube).
VIDEOS_ON_YOUTUBE = youtube_upload.configured()


def is_drive_filename(name: str) -> bool:
    """Return True if filename is eligible for Google Drive upload.

    Strictly excludes any .json file, audio file, or video file.
    """
    if name.startswith("."):
        return False
    ext = Path(name).suffix.lower()
    if ext == ".json" or ext in DRIVE_FORBIDDEN_EXTS:
        return False
    mime = mimetypes.guess_type(name)[0] or ""
    if mime.startswith(("audio/", "video/")) or mime == "application/json":
        return False
    if DRIVE_EXTS is not None and ext not in DRIVE_EXTS:
        return False
    return True


def is_drive_file(f: Path) -> bool:
    """Return True if path is a file eligible for Google Drive upload."""
    return f.is_file() and is_drive_filename(f.name)


def drive_files(cdir: Path) -> list[Path]:
    if not cdir.is_dir():
        return []
    skip = {DRIVE_MANIFEST, YOUTUBE_MANIFEST, mongo_sync.MARKER, audio_video.VIDEO}
    return sorted(f for f in cdir.iterdir()
                  if f.name not in skip and is_drive_file(f))


def load_drive_manifest(cdir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((cdir / DRIVE_MANIFEST).read_text(encoding="utf-8"))
        # Only retain entries for allowed Drive files (excludes historical .json, audio, video)
        return {k: v for k, v in manifest.items() if is_drive_filename(k)}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _in_sync(entry: dict[str, Any] | None, f: Path) -> bool:
    if not entry:
        return False
    st = f.stat()
    return entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


class DriveSync:
    """Mirrors a chapter's output folder to Drive, one file at a time.

    Disabled (with one warning) when Drive isn't set up, so the rest of the
    pipeline keeps working. Uploads are serialised because the Google client
    library isn't thread-safe; they run in a worker thread so generation and
    polling carry on meanwhile.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.lock = asyncio.Lock()
        self.drive: drive_upload.Drive | None = None
        # Live bars pinned under the scrolling log: the current file, plus an
        # overall bar during `python run.py drive`. transient → they vanish
        # when finished, leaving just the "✓ ☁ ... done" log line.
        self.progress = Progress(
            TextColumn("[cyan]☁[/] {task.description}", markup=True),
            BarColumn(bar_width=30),
            TextColumn("{task.percentage:>5.1f}%"),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        self.overall: TaskID | None = None

    def _bar_start(self) -> None:
        if not self.progress.live.is_started:
            self.progress.start()

    def _bar_stop_if_idle(self) -> None:
        if self.progress.live.is_started and not self.progress.tasks:
            self.progress.stop()

    def start_overall(self, n_files: int, total_bytes: int) -> None:
        self._bar_start()
        self.overall = self.progress.add_task(f"[bold]all files[/] (0/{n_files})",
                                              total=total_bytes or 1)
        self._overall_files = (0, n_files)

    def finish_overall(self) -> None:
        if self.overall is not None:
            self.progress.remove_task(self.overall)
            self.overall = None
        self._bar_stop_if_idle()

    async def _client(self) -> drive_upload.Drive | None:
        if not self.enabled:
            return None
        if self.drive is None:
            try:
                self.drive = await asyncio.to_thread(drive_upload.Drive)
            except Exception as e:  # noqa: BLE001 — Drive is optional
                self.enabled = False
                log.warning("Google Drive upload is OFF — %s", e)
                return None
        return self.drive

    async def sync_chapter(self, row: dict[str, str], key: str) -> None:
        """Upload any new/changed files in this chapter's folder. Never raises."""
        cdir = chapter_dir(row)
        manifest = load_drive_manifest(cdir)
        pending = [f for f in drive_files(cdir) if not _in_sync(manifest.get(f.name), f)]
        if not pending:
            return
        drive = await self._client()
        if drive is None:
            return
        parts = (row["class"], row["subject"], row["chapter"])
        for f in pending:
            size = f.stat().st_size
            async with self.lock:
                self._bar_start()
                label = f"{short_key(key)} · {f.name}"
                task = self.progress.add_task(label, total=size or 1)
                overall, before = self.overall, 0

                def on_progress(sent: int, total: int) -> None:
                    nonlocal before
                    self.progress.update(task, completed=sent)
                    if overall is not None:
                        self.progress.advance(overall, sent - before)
                        before = sent

                try:
                    info = await asyncio.to_thread(drive.upload, f, parts, None, on_progress)
                except Exception as e:  # noqa: BLE001 — retried on the next sync
                    log.error("[%s] drive upload %s FAILED: %s", key, f.name, e)
                    continue
                finally:
                    self.progress.remove_task(task)
                    if overall is not None:
                        self.progress.advance(overall, size - before)  # skipped/linked files too
                        done, n = self._overall_files
                        self._overall_files = (done + 1, n)
                        self.progress.update(overall,
                                             description=f"[bold]all files[/] ({done + 1}/{n})")
                    self._bar_stop_if_idle()
            manifest[f.name] = info
            tmp = cdir / f".{DRIVE_MANIFEST}.tmp"
            tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            os.replace(tmp, cdir / DRIVE_MANIFEST)
            what = "uploaded" if info["uploaded"] else "already on Drive, linked"
            log.info("[%s] ☁ %s %s (%s) done", key, f.name, what, _mb(info["size"]))


DRIVE = DriveSync(enabled=False)  # replaced per run by the phase functions


def load_youtube_manifest(cdir: Path) -> dict[str, Any]:
    try:
        return json.loads((cdir / YOUTUBE_MANIFEST).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def youtube_pending(cdir: Path) -> list[Path]:
    """Videos to upload, whose privacy no longer matches YOUTUBE_PRIVACY, that
    aren't in their playlist yet, that YouTube hasn't confirmed as processed, or
    whose replaced older version is still listed.

    Includes audio_video.mp4 when it still has to be built from audio.m4a.
    """
    manifest = load_youtube_manifest(cdir)
    return [cdir / n for n in YOUTUBE_FILES
            if (n == audio_video.VIDEO and audio_video.stale(cdir)) or (
                (cdir / n).is_file() and (
                    not _in_sync(manifest.get(n), cdir / n)
                    or manifest[n].get("requested_privacy") != youtube_upload.PRIVACY
                    or not manifest[n].get("playlist_id")
                    or not manifest[n].get("processed")
                    or any(not o.get("retired") for o in manifest[n].get("replaced", []))))]


# Tries per YouTube call within one run (network drops, 5xx) before leaving
# it for the next run; waits 30s, 60s, … between them.
YOUTUBE_ATTEMPTS = max(1, int(os.getenv("YOUTUBE_ATTEMPTS") or 3))
YOUTUBE_RETRY_WAIT = 30.0


def youtube_quota_line() -> Text:
    """'YouTube quota  3,250/10,000 today · ~4 uploads left · resets 12:30 IST · in 5h'.

    Counted by this pipeline (youtube_upload.quota) — the API doesn't report it.
    """
    q = youtube_upload.quota()
    style = "green" if q["uploads_left"] >= 2 else "yellow" if q["uploads_left"] else "red"
    return Text.assemble(
        ("YouTube quota  ", "bold"),
        (f"{q['used']:,}/{q['limit']:,}", style), " today · ",
        (f"~{q['uploads_left']} upload{'s' * (q['uploads_left'] != 1)} left", style),
        f" · resets {_reset_label(q['resets_at'])}")


def save_youtube_manifest(cdir: Path, manifest: dict[str, Any]) -> None:
    tmp = cdir / f".{YOUTUBE_MANIFEST}.tmp"
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, cdir / YOUTUBE_MANIFEST)


# YouTube refuses longer videos until the channel is phone-verified.
YOUTUBE_MAX_UNVERIFIED_SECS = 15 * 60


def use_youtube(enabled: bool) -> "YouTubeSync":
    """YouTube stage for this run."""
    global VIDEOS_ON_YOUTUBE
    if enabled:
        try:
            youtube_upload.check_privacy()
        except youtube_upload.YouTubeNotConfigured as e:
            log.error("%s", e)
            sys.exit(2)
    VIDEOS_ON_YOUTUBE = enabled and youtube_upload.configured()
    if enabled and not VIDEOS_ON_YOUTUBE:
        log.warning("YouTube isn't signed in (`python run.py youtube-login`) — "
                    "video uploads skipped (videos are not uploaded to Drive)")
    return YouTubeSync(VIDEOS_ON_YOUTUBE)


class YouTubeSync:
    """Uploads a chapter's videos to YouTube, one at a time.

    Disabled (with one warning) when YouTube isn't set up, and for the rest of
    the run once the daily quota runs out, so the rest of the pipeline keeps
    working. Uploads are serialised and run in a worker thread.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.lock = asyncio.Lock()
        self.yt: youtube_upload.YouTube | None = None
        self.progress = Progress(
            TextColumn("[red]▶[/] {task.description}", markup=True),
            BarColumn(bar_width=30),
            TextColumn("{task.percentage:>5.1f}%"),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )

    async def _client(self) -> youtube_upload.YouTube | None:
        if not self.enabled:
            return None
        if self.yt is None:
            try:
                self.yt = await asyncio.to_thread(youtube_upload.YouTube)
            except Exception as e:  # noqa: BLE001 — YouTube is optional
                self.enabled = False
                log.warning("YouTube upload is OFF — %s", e)
                return None
        return self.yt

    async def _call(self, key: str, what: str, fn: Callable[..., Any], *args: Any) -> Any:
        """Run one YouTube call in a thread, retrying network/5xx failures with backoff.

        QuotaExceeded switches YouTube off for the run (logging when it resets)
        and is re-raised; anything else is re-raised after the last attempt.
        """
        for attempt in range(1, YOUTUBE_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(fn, *args)
            except youtube_upload.QuotaExceeded as e:
                self.enabled = False
                q = youtube_upload.quota()
                log.warning("YouTube upload is OFF for this run — %s (quota resets %s)",
                            e, _reset_label(q["resets_at"]))
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == YOUTUBE_ATTEMPTS or not youtube_upload.retryable(e):
                    raise
                wait = YOUTUBE_RETRY_WAIT * 2 ** (attempt - 1)
                log.warning("[%s] youtube %s failed (%s) — retry %d/%d in %ds",
                            key, what, e, attempt, YOUTUBE_ATTEMPTS - 1, wait)
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    async def sync_chapter(self, row: dict[str, str], key: str) -> None:
        """Upload any new/changed videos in this chapter's folder. Never raises."""
        cdir = chapter_dir(row)
        pending = youtube_pending(cdir)
        if not pending:
            return
        yt = await self._client()
        if yt is None:
            return
        manifest = load_youtube_manifest(cdir)
        place = f"{row['class']} · {row['subject']} · {row['chapter']}"
        chapter = (row.get("chapter_title") or "").strip() or place
        if audio_video.stale(cdir):
            try:
                await asyncio.to_thread(audio_video.make, cdir, chapter, f"NCERT {place}")
                log.info("[%s] 🎞 %s built from %s done", key, audio_video.VIDEO, audio_video.AUDIO)
            except Exception as e:  # noqa: BLE001 — retried on the next sync
                log.error("[%s] %s FAILED: %s", key, audio_video.VIDEO, e)
                pending = [f for f in pending if f.name != audio_video.VIDEO]
        # Uploads YouTube hasn't confirmed yet: keep the processed ones, and drop the
        # ones it rejected (e.g. over 15 min on an unverified channel) so they go up again.
        unchecked = [n for n, v in manifest.items() if n in YOUTUBE_FILES and not v.get("processed")]
        if unchecked:
            async with self.lock:
                if not self.enabled:
                    return
                try:
                    states = await self._call(key, "status check", yt.video_states,
                                              [manifest[n]["id"] for n in unchecked])
                except youtube_upload.QuotaExceeded:
                    return
                except Exception as e:  # noqa: BLE001 — checked again next sync
                    log.warning("[%s] youtube status check failed: %s", key, e)
                    states = {}
                for n in unchecked:
                    vid = manifest[n]["id"]
                    state, why = states.get(vid, ("", ""))
                    if state == "processed":
                        manifest[n]["processed"] = True
                    elif state in ("rejected", "failed", "deleted", "missing"):
                        log.warning("[%s] ▶ %s (%s) was %s by YouTube%s — removing it to upload again",
                                    key, n, vid, state, f" ({why})" if why else "")
                        if state != "missing":
                            try:
                                await self._call(key, f"delete {n}", yt.delete, vid)
                            except youtube_upload.QuotaExceeded:
                                return
                            except Exception as e:  # noqa: BLE001 — a dead video, harmless
                                log.warning("[%s] couldn't delete %s (%s): %s", key, n, vid, e)
                        del manifest[n]
            save_youtube_manifest(cdir, manifest)
            pending = [f for f in youtube_pending(cdir)
                       if f.is_file() or f.name != audio_video.VIDEO]
        spent = False  # any quota used for this chapter → show what's left
        kids = youtube_made_for_kids(row)
        for f in pending:
            md5 = await asyncio.to_thread(drive_upload.file_md5, f)
            old = manifest.get(f.name)
            kind = YOUTUBE_FILES[f.name]
            sent = changed = False
            if old and old.get("md5") == md5:
                # Same video, only touched on disk — never spend quota on it twice.
                old.update(size=f.stat().st_size, mtime=f.stat().st_mtime)
                info = old
                if (old.get("requested_privacy") != youtube_upload.PRIVACY
                        or old.get("made_for_kids", False) != kids):
                    async with self.lock:
                        if not self.enabled:
                            return
                        try:
                            applied = await self._call(key, f"privacy {f.name}",
                                                       yt.set_privacy, old["id"], kids)
                        except youtube_upload.QuotaExceeded:
                            return
                        except Exception as e:  # noqa: BLE001 — retried on the next sync
                            log.error("[%s] youtube privacy %s FAILED: %s", key, f.name, e)
                            continue
                    old.update(requested_privacy=youtube_upload.PRIVACY, privacy=applied,
                               made_for_kids=kids)
                    changed = True
            else:
                secs = await asyncio.to_thread(audio_video.duration, f)
                if secs and secs > YOUTUBE_MAX_UNVERIFIED_SECS:
                    async with self.lock:
                        try:
                            long_ok = await self._call(key, "channel check", yt.long_uploads)
                        except youtube_upload.QuotaExceeded:
                            return
                        except Exception as e:  # noqa: BLE001 — let YouTube decide
                            log.warning("[%s] youtube channel check failed: %s", key, e)
                            long_ok = "allowed"
                    if long_ok != "allowed":
                        log.error("[%s] ▶ %s is %d min — YouTube only accepts over 15 min from a "
                                  "phone-verified channel (status: %s). Verify at "
                                  "https://www.youtube.com/verify, then re-run. Skipped, no quota used.",
                                  key, f.name, secs // 60, long_ok or "unknown")
                        continue
                title, desc, tags = youtube_meta(row, kind)
                # Earlier versions of this file — never mistaken for this one.
                replaced = (old.get("replaced", []) + [
                    {"id": old["id"], "playlist_item_id": old.get("playlist_item_id")}]) if old else []
                async with self.lock:
                    if not self.enabled:  # quota ran out on another chapter meanwhile
                        return
                    try:
                        # An earlier attempt may have finished on YouTube but died before
                        # youtube.json was written: adopt that video instead of a duplicate.
                        found = await self._call(key, f"lookup {f.name}", yt.find_upload, title,
                                                 {o["id"] for o in replaced})
                    except youtube_upload.QuotaExceeded:
                        return
                    except Exception as e:  # noqa: BLE001 — the lookup is only a safety net
                        log.warning("[%s] youtube lookup %s failed (%s) — uploading", key, f.name, e)
                        found = None
                    if found:
                        st = f.stat()
                        info = {**found, "name": f.name, "requested_privacy": youtube_upload.PRIVACY,
                                "size": st.st_size, "mtime": st.st_mtime, "md5": md5,
                                "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                **youtube_upload.links(found["id"])}
                        log.info("[%s] ▶ %s was already on YouTube (earlier attempt) — recorded, "
                                 "not uploaded again", key, f.name)
                    else:
                        if not self.progress.live.is_started:
                            self.progress.start()
                        task = self.progress.add_task(f"{short_key(key)} · {f.name}",
                                                      total=f.stat().st_size or 1)
                        try:
                            info = await self._call(
                                key, f"upload {f.name}", yt.upload, f, title, desc, md5,
                                lambda sent_b, _t: self.progress.update(task, completed=sent_b),
                                tags, kids)
                        except youtube_upload.QuotaExceeded:
                            return
                        except Exception as e:  # noqa: BLE001 — retried on the next sync
                            log.error("[%s] youtube upload %s FAILED: %s", key, f.name, e)
                            continue
                        finally:
                            self.progress.remove_task(task)
                            if not self.progress.tasks:
                                self.progress.stop()
                info.update(made_for_kids=kids, replaced=replaced)
                sent = changed = True
            if not info.get("playlist_id"):
                async with self.lock:
                    if not self.enabled:
                        return
                    try:
                        pid, item = await self._call(
                            key, f"playlist {f.name}", yt.add_to_playlist, info["id"],
                            *youtube_playlist(row), youtube_sort_key(row, kind), youtube_key_of)
                    except youtube_upload.QuotaExceeded:
                        pid = item = None
                    except Exception as e:  # noqa: BLE001 — retried on the next sync
                        log.error("[%s] youtube playlist %s FAILED: %s", key, f.name, e)
                        pid = item = None
                if pid:
                    info.update(playlist_id=pid, playlist_item_id=item)
                    log.info("[%s] ▶ %s added to playlist \"%s\" done",
                             key, f.name, youtube_playlist(row)[0])
            for o in info.get("replaced", []):
                if o.get("retired") or not self.enabled:
                    continue
                # The older version stays reachable by its link but leaves the
                # channel page, search and the playlist.
                async with self.lock:
                    try:
                        if o.get("playlist_item_id"):
                            await self._call(key, f"retire {f.name}", yt.remove_from_playlist,
                                             o["playlist_item_id"])
                        await self._call(key, f"retire {f.name}", yt.set_privacy, o["id"], kids,
                                         "unlisted")
                    except youtube_upload.QuotaExceeded:
                        break
                    except Exception as e:  # noqa: BLE001 — retried on the next sync
                        log.error("[%s] unlisting old %s (%s) FAILED: %s", key, f.name, o["id"], e)
                        continue
                o["retired"] = True
                log.info("[%s] ▶ old %s %s unlisted and taken out of the playlist done",
                         key, f.name, o["id"])
            manifest[f.name] = info
            save_youtube_manifest(cdir, manifest)
            spent = spent or changed or bool(info.get("playlist_id"))
            if changed:
                note = "" if info["privacy"] == youtube_upload.PRIVACY else \
                    f" — YouTube kept it {info['privacy']} (API project not audited yet)"
                what = "on YouTube" if sent else f"now {youtube_upload.PRIVACY} on YouTube"
                log.info("[%s] ▶ %s %s: %s%s done", key, f.name, what, info["url"], note)
        if spent:
            console.print(youtube_quota_line())


YOUTUBE = YouTubeSync(enabled=False)


class MongoSync:
    """Writes a chapter into the phoenix MongoDB once its content changes.

    Skipped quietly until the chapter has some content on disk; disabled with
    one warning when MONGODB_URI isn't set. Writes are
    serialised and run in a worker thread (pymongo is synchronous).
    """

    def __init__(self, enabled: bool, dry_run: bool = False) -> None:
        self.enabled = enabled
        self.dry_run = dry_run
        self.lock = asyncio.Lock()
        self.db: Any = None

    async def sync_chapter(self, row: dict[str, str], key: str, force: bool = False) -> None:
        if not self.enabled:
            return
        cdir = chapter_dir(row)
        if not (force or self.dry_run or mongo_sync.needs_sync(cdir)):
            return
        if not mongo_sync.has_content(cdir):
            return
        async with self.lock:
            try:
                if self.db is None:
                    self.db = await asyncio.to_thread(mongo_sync.connect)
                s = await asyncio.to_thread(mongo_sync.sync_chapter, self.db, row, cdir,
                                            self.dry_run)
            except mongo_sync.MongoNotConfigured as e:
                self.enabled = False
                log.warning("MongoDB save is OFF — %s", e)
                return
            except Exception as e:  # noqa: BLE001 — retried on the next sync
                log.error("[%s] mongo save FAILED: %s", key, e)
                return
        verb = "would save" if self.dry_run else "🗄 saved to MongoDB"
        mind = f"mind map ({s['mind_map_nodes']} nodes)" if s["mind_map_nodes"] else "no mind map"
        log.info("[%s] %s \"%s\": %d quiz, %d flashcards, %s, %d resources done",
                 key, verb, s["title"], s["quiz"], s["flashcards"], mind, len(s["resources"]))


MONGO = MongoSync(enabled=False)


async def sync_backends(row: dict[str, str], key: str) -> None:
    """After local files change: Drive, YouTube, then MongoDB (it saves their links)."""
    await DRIVE.sync_chapter(row, key)
    await YOUTUBE.sync_chapter(row, key)
    await MONGO.sync_chapter(row, key)


async def phase_generate(args: argparse.Namespace) -> None:
    global DRIVE, YOUTUBE, MONGO
    YOUTUBE = use_youtube(args.youtube)
    DRIVE = DriveSync(args.drive)
    MONGO = MongoSync(args.mongo)
    active = parse_artifacts(args.artifacts)
    store = Store()
    store.load()
    store.sync_from_sheet()

    targets = select_rows(store, args, "gen", active)
    if not targets:
        log.info("nothing to generate")
        return

    sem = asyncio.Semaphore(args.concurrency)
    async with NotebookLMClient.from_storage() as client:
        if not await preflight(client, active, len(targets), args.concurrency):
            return
        gate = QuotaGate(client)
        # Title→id map of existing notebooks, so an interrupted prior run can
        # adopt its notebook instead of creating a duplicate.
        nb_index = {nb.title: nb.id for nb in await client.notebooks.list()}
        monitor = (asyncio.create_task(usage_monitor(client, active, args.usage_every))
                   if args.usage_every > 0 else None)
        try:
            await asyncio.gather(*[
                generate_row(client, store, row, active, sem, args.max_attempts, nb_index, gate,
                             args.download_now)
                for row in targets
            ])
        finally:
            if monitor is not None:
                monitor.cancel()
    summarize(store)


# --------------------------------------------------------------------------- #
# Phase 2: download
# --------------------------------------------------------------------------- #


def artifact_outputs(name: str) -> list[tuple[str, str | None]]:
    """(filename, format) pairs to save for one artifact."""
    spec = ARTIFACT_SPECS[name]
    fmts = DOWNLOAD_FORMATS.get(name)
    if not fmts:
        return [(spec["filename"], None)]
    stem = Path(spec["filename"]).stem
    return [(f"{stem}.{FORMAT_EXT.get(f, f)}", f) for f in fmts]


def validate_download(path: Path, label: str | None = None) -> None:
    """Cheap sanity check that a downloaded file is what it claims to be."""
    label = label or path.name
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"{label} is empty")
    head = path.open("rb").read(16)
    ext = path.suffix.lower()
    ok = {
        ".pdf": head.startswith(b"%PDF"),
        ".pptx": head.startswith(b"PK"),                       # zip container
        ".m4a": head[4:8] == b"ftyp",                          # MP4 container
        ".mp4": head[4:8] == b"ftyp",
        ".png": head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8")
        or head[8:12] == b"WEBP",                              # png / jpeg / webp
    }.get(ext, True)
    if not ok:
        raise ValueError(f"{label} doesn't look like a valid {ext} file (starts {head[:8]!r})")
    if ext == ".json":
        json.loads(path.read_text(encoding="utf-8"))


def _is_downloaded(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        validate_download(path)
        return True
    except Exception:  # noqa: BLE001 — anything wrong means "download again"
        return False


def download_complete(row: dict[str, str], name: str) -> bool:
    return row.get(f"{name}_dl") == "done" and all(
        _is_downloaded(chapter_dir(row) / fn) for fn, _ in artifact_outputs(name))


async def download_artifact(
    client: NotebookLMClient, store: Store, row: dict[str, str], name: str, key: str
) -> bool:
    """Download every configured format of one generated artifact. Never raises.

    Each file is written to a hidden temp name, validated, then renamed into
    place — so a crash or bad download never leaves a truncated file that
    looks real. Files already present and valid are skipped.
    """
    spec = ARTIFACT_SPECS[name]
    cdir = chapter_dir(row)
    cdir.mkdir(parents=True, exist_ok=True)
    art_id = row.get(f"{name}_id") or None
    saved: list[str] = []
    try:
        for filename, fmt in artifact_outputs(name):
            out = cdir / filename
            if _is_downloaded(out):
                continue
            part = out.with_name(f".{out.stem}.part{out.suffix}")
            part.unlink(missing_ok=True)
            try:
                await spec["download"](client, row["notebook_id"], str(part), art_id, fmt)
                validate_download(part, out.name)
                os.replace(part, out)
            finally:
                part.unlink(missing_ok=True)
            saved.append(filename)
        row[f"{name}_dl"] = "done"
        if saved:
            log.info("[%s] downloaded %s → %s done", key, name, ", ".join(saved))
            if name in ("video", "cinematic_video"):
                extract_video_thumbnail(cdir)
        return True
    except Exception as e:  # noqa: BLE001 — record + continue; `download` phase retries
        row[f"{name}_dl"] = "failed"
        row["dl_error"] = f"{name}: {e}"[:500]
        log.error("[%s] download %s FAILED: %s", key, name, e)
        return False
    finally:
        row["updated_at"] = now()
        await store.save()


async def download_row(
    client: NotebookLMClient,
    store: Store,
    row: dict[str, str],
    active: list[str],
    sem: asyncio.Semaphore,
    max_attempts: int,
) -> None:
    key = row_key(row)
    async with sem:
        if not row.get("notebook_id"):
            return
        downloadable = [a for a in active if row.get(f"{a}_gen") == "done"]
        if not downloadable:
            return
        # Everything already on disk and valid: record it and stop — without
        # counting an attempt.
        if all(download_complete(row, a) for a in downloadable):
            row["dl_status"] = roll_up(row, "dl", downloadable)
            await store.save()
            await sync_backends(row, key)
            return
        # retry guard (max_attempts <= 0 means never give up)
        if (
            max_attempts > 0
            and int(row.get("dl_attempts") or 0) >= max_attempts
            and row.get("dl_status") == "failed"
        ):
            log.warning("[%s] dl at max attempts — skip", key)
            return

        row["dl_attempts"] = str(int(row.get("dl_attempts") or 0) + 1)
        row["dl_error"] = ""
        for name in downloadable:
            if not download_complete(row, name):
                await download_artifact(client, store, row, name, key)

        if row.get("thumbnail_url"):
            thumb_path = chapter_dir(row) / "thumbnail.jpg"
            if not (thumb_path.exists() and thumb_path.stat().st_size > 0):
                try:
                    await download_thumbnail(row["thumbnail_url"], thumb_path)
                except Exception as e:
                    log.warning("[%s] thumbnail download failed: %s", key, e)

        extract_chapter_thumbnail(chapter_dir(row))
        extract_video_thumbnail(chapter_dir(row))

        row["dl_status"] = roll_up(row, "dl", downloadable)
        row["updated_at"] = now()
        await store.save()
        log.info("[%s] dl_status=%s", key, row["dl_status"])
        await sync_backends(row, key)


async def phase_download(args: argparse.Namespace) -> None:
    global DRIVE, YOUTUBE, MONGO
    YOUTUBE = use_youtube(args.youtube)
    DRIVE = DriveSync(args.drive)
    MONGO = MongoSync(args.mongo)
    active = parse_artifacts(args.artifacts)
    store = Store()
    store.load()
    if not store.rows:
        store.sync_from_sheet()

    targets = select_rows(store, args, "dl", active)
    targets = [r for r in targets if r.get("notebook_id")]
    if not targets:
        log.info("nothing to download (run `generate` first)")
        return
    show_header("Download", [("chapters", f"{len(targets)}  ({args.concurrency} in parallel)"),
                             ("artifacts", artifact_list(active))])

    sem = asyncio.Semaphore(args.concurrency)
    async with NotebookLMClient.from_storage() as client:
        await asyncio.gather(*[
            download_row(client, store, row, active, sem, args.max_attempts)
            for row in targets
        ])
    summarize(store)


# --------------------------------------------------------------------------- #
# Selection / reporting
# --------------------------------------------------------------------------- #


def parse_artifacts(spec: str | None) -> list[str]:
    if not spec:
        return list(ARTIFACTS)
    names = [s.strip() for s in spec.split(",") if s.strip()]
    bad = [n for n in names if n not in ARTIFACT_SPECS]
    if bad:
        sys.exit(f"unknown artifact(s): {bad}; valid: {list(ARTIFACT_SPECS)}")
    return names


def select_rows(
    store: Store, args: argparse.Namespace, phase: str, active: list[str]
) -> list[dict[str, str]]:
    """Pick rows to process, judged against the *currently active* artifact set
    (not the rolled-up status, which reflects whatever set ran last)."""

    def is_done(r: dict[str, str]) -> bool:
        if phase == "gen":
            return all(r.get(f"{a}_gen") == "done" for a in active)
        # download: every generated artifact in the active set is downloaded
        gen_ok = [a for a in active if r.get(f"{a}_gen") == "done"]
        return bool(gen_ok) and all(r.get(f"{a}_dl") == "done" for a in gen_ok)

    def has_failure(r: dict[str, str]) -> bool:
        field = "_gen" if phase == "gen" else "_dl"
        return any(r.get(f"{a}{field}") in ("failed", "rate_limited") for a in active)

    rows = list(store.rows.values())
    if args.only_failed:
        rows = [r for r in rows if has_failure(r)]
    else:
        rows = [r for r in rows if not is_done(r)]
    if args.limit:
        rows = rows[: args.limit]
    return rows


def summarize(store: Store) -> None:
    """Per-chapter × per-artifact status grid (coloured table on the console)."""
    rows = list(store.rows.values())
    if not rows:
        log.info("summary: no chapters tracked yet")
        return
    wide = console.width >= 110
    table = Table(title="Pipeline status", title_style="bold", header_style="bold",
                  border_style="bright_black", box=box.SIMPLE_HEAVY, pad_edge=False, collapse_padding=True)
    table.add_column("chapter", style="bold", no_wrap=True, min_width=KEY_WIDTH - 3)
    for a in ARTIFACTS:
        head = SHORT_NAME.get(a, a[:5])
        table.add_column(head, justify="center", no_wrap=True, min_width=len(head))
    table.add_column("gen", justify="center", no_wrap=True, min_width=7)
    table.add_column("drive", justify="center", no_wrap=True, min_width=5)
    narrow = console.width < 90
    if not narrow:
        table.add_column("dl", justify="center", no_wrap=True, min_width=7)
    if wide:
        table.add_column("notebook", style="dim", no_wrap=True)

    plain = []
    for r in rows:
        key = short_key(row_key(r))
        cells = [CELL.get(r.get(f"{a}_gen", ""), CELL[""]) for a in ARTIFACTS]
        gen = r.get("gen_status") or "pending"
        dl = r.get("dl_status") or "pending"
        cdir = chapter_dir(r)
        local = drive_files(cdir)
        manifest = load_drive_manifest(cdir)
        up = sum(_in_sync(manifest.get(f.name), f) for f in local)
        drive_cell = Text(f"{up}/{len(local)}" if local else "·",
                          style="green" if local and up == len(local)
                          else "yellow" if local else "bright_black")
        table.add_row(Text(key, style=key_style(key)), *[Text(c[0], style=c[1]) for c in cells],
                      Text(gen, style=STATUS_STYLE.get(gen, "")), drive_cell,
                      *([Text(dl, style=STATUS_STYLE.get(dl, ""))] if not narrow else []),
                      *([(r.get("notebook_id") or "")[:8]] if wide else []))
        plain.append(f"{key:<24} " + " ".join(c[0] for c in cells)
                     + f"  gen={gen} drive={drive_cell.plain} dl={dl}")

    counts: dict[str, int] = {}
    for r in rows:
        k = r.get("gen_status") or "pending"
        counts[k] = counts.get(k, 0) + 1
    table.caption = ("[green]✓ done[/]   [red]✗ failed[/]   [yellow]⏸ no quota / rate-limited[/]"
                     "   [bright_black]· not yet[/]\n"
                     + "  ".join(f"{k}={v}" for k, v in counts.items()))
    show_table(table, plain)
    if youtube_upload.configured():
        console.print(youtube_quota_line())


ENV_VARS = [  # (name, required?, secret?) — keep in sync with .env.example
    ("SHEET_ID", True, False),
    ("GOOGLE_DRIVE_CLIENT_ID", True, False),
    ("GOOGLE_DRIVE_CLIENT_SECRET", True, True),
    ("GOOGLE_DRIVE_REFRESH_TOKEN", False, True),
    ("DRIVE_ROOT_FOLDER", False, False),
    ("YOUTUBE_REFRESH_TOKEN", False, True),
    ("YOUTUBE_PRIVACY", False, False),
    ("YOUTUBE_DAILY_QUOTA", False, False),
    ("YOUTUBE_ATTEMPTS", False, False),
    ("YOUTUBE_CALL_GAP", False, False),
    ("YOUTUBE_UPLOAD_GAP", False, False),
    ("YOUTUBE_KIDS_MAX_CLASS", False, False),
    ("MONGODB_URI", True, True),
    ("MONGODB_DB", False, False),
]


async def cmd_check(_: argparse.Namespace) -> None:
    """Verify .env and every credential, without changing anything."""
    env_file = HERE / ".env"
    vars_t = Table(title="pipeline/.env", title_style="bold", header_style="bold",
                   border_style="bright_black")
    vars_t.add_column("variable")
    vars_t.add_column("value")
    if not env_file.exists():
        log.error(".env not found — run: cp .env.example .env   (in the pipeline folder)")
    for name, required, secret in ENV_VARS:
        val = os.getenv(name, "").strip()
        if not val:
            shown = Text("missing", style="bold red") if required else Text("not set (optional)",
                                                                             style="bright_black")
        elif secret:
            shown = Text(f"set ({len(val)} chars, hidden)", style="green")
        else:
            shown = Text(val, style="green")
        vars_t.add_row(name, shown)
    console.print(vars_t)

    checks = Table(title="Connections", title_style="bold", header_style="bold",
                   border_style="bright_black")
    checks.add_column("service")
    checks.add_column("result")
    ok, bad = Text("✓ ok", style="bold green"), lambda m: Text(f"✗ {m}", style="bold red")

    # NotebookLM
    try:
        async with NotebookLMClient.from_storage() as client:
            limits = await client.settings.get_account_limits()
        checks.add_row("NotebookLM", Text.assemble(
            ok, f"  ({TIER_NAMES.get(limits.tier or 0, limits.tier)} plan)"))
    except Exception as e:  # noqa: BLE001 — report, don't crash
        checks.add_row("NotebookLM", bad(f"{type(e).__name__}: {e} — run `notebooklm login`"))

    # Google Drive
    try:
        drive = await asyncio.to_thread(drive_upload.Drive)
        about = await asyncio.to_thread(
            lambda: drive.svc.about().get(fields="user(emailAddress)").execute())
        checks.add_row("Google Drive", Text.assemble(
            ok, f"  ({about['user']['emailAddress']}, folder \"{drive_upload.ROOT_FOLDER}\")"))
    except Exception as e:  # noqa: BLE001
        checks.add_row("Google Drive", bad(str(e)))

    # YouTube
    try:
        yt = await asyncio.to_thread(youtube_upload.YouTube)
        channel = await asyncio.to_thread(yt.channel_title)
        checks.add_row("YouTube", Text.assemble(
            ok, f"  (channel \"{channel}\", uploads {youtube_upload.PRIVACY})"))
        long_ok = await asyncio.to_thread(yt.long_uploads)
        checks.add_row("", Text("videos over 15 min allowed", style="green") if long_ok == "allowed"
                       else Text(f"videos over 15 min BLOCKED ({long_ok or 'unknown'}) — "
                                 "verify the channel at https://www.youtube.com/verify", style="red"))
        checks.add_row("", youtube_quota_line())
    except Exception as e:  # noqa: BLE001
        checks.add_row("YouTube", bad(str(e)))

    # MongoDB
    try:
        from pymongo import MongoClient

        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            raise RuntimeError("MONGODB_URI is not set in .env")
        mc = MongoClient(uri, serverSelectionTimeoutMS=8000)
        await asyncio.to_thread(mc.admin.command, "ping")
        db = mc[os.getenv("MONGODB_DB", "").strip()] if os.getenv("MONGODB_DB", "").strip() \
            else mc.get_default_database()
        checks.add_row("MongoDB", Text.assemble(ok, f"  (database \"{db.name}\")"))
        mc.close()
    except Exception as e:  # noqa: BLE001
        checks.add_row("MongoDB", bad(f"{type(e).__name__}: {e}"))
    console.print(checks)


async def cmd_mongo(ns: argparse.Namespace) -> None:
    global MONGO
    MONGO = MongoSync(True, dry_run=ns.dry_run)
    store = Store()
    store.load()
    for row in store.rows.values():
        await MONGO.sync_chapter(row, row_key(row), force=ns.force)
    if ns.dry_run:
        log.info("dry run — nothing was written")


async def cmd_titles(_: argparse.Namespace) -> None:
    """Fill chapter_title for every chapter that has a notebook but no title yet."""
    store = Store()
    store.load()
    todo = [r for r in store.rows.values() if r.get("notebook_id") and not r.get("chapter_title")]
    if not todo:
        log.info("every chapter with a notebook already has a title")
        return
    sem = asyncio.Semaphore(DEFAULT_CONCURRENCY)

    async def one(row: dict[str, str]) -> None:
        async with sem:
            await ensure_chapter_title(client, store, row, row_key(row))

    async with NotebookLMClient.from_storage() as client:
        await asyncio.gather(*[one(r) for r in todo])


def cmd_drive_login(_: argparse.Namespace) -> None:
    try:
        drive_upload.login()
    except drive_upload.DriveNotConfigured as e:
        log.error("%s", e)
        sys.exit(1)
    log.info("Drive sign-in saved to %s done", drive_upload.TOKEN_FILE.name)


async def cmd_drive(_: argparse.Namespace) -> None:
    global DRIVE, VIDEOS_ON_YOUTUBE
    DRIVE = DriveSync(True)
    VIDEOS_ON_YOUTUBE = youtube_upload.configured()
    store = Store()
    store.load()
    pending: list[Path] = []
    for row in store.rows.values():
        cdir = chapter_dir(row)
        manifest = load_drive_manifest(cdir)
        pending += [f for f in drive_files(cdir) if not _in_sync(manifest.get(f.name), f)]
    if not pending:
        log.info("Drive is up to date — nothing to upload")
    elif await DRIVE._client() is None:
        pass  # not signed in — the warning already says what to do
    else:
        log.info("uploading %d file(s), %s", len(pending), _mb(sum(f.stat().st_size for f in pending)))
        DRIVE.start_overall(len(pending), sum(f.stat().st_size for f in pending))
        try:
            for row in store.rows.values():
                await DRIVE.sync_chapter(row, row_key(row))
        finally:
            DRIVE.finish_overall()
    summarize(store)


def cmd_youtube_login(_: argparse.Namespace) -> None:
    try:
        youtube_upload.login()
    except youtube_upload.YouTubeNotConfigured as e:
        log.error("%s", e)
        sys.exit(1)
    log.info("YouTube sign-in saved to %s done", youtube_upload.TOKEN_FILE.name)


async def cmd_youtube(_: argparse.Namespace) -> None:
    global YOUTUBE
    YOUTUBE = use_youtube(True)
    if not YOUTUBE.enabled:
        return
    store = Store()
    store.load()
    pending = sum(len(youtube_pending(chapter_dir(r))) for r in store.rows.values())
    if not pending:
        log.info("YouTube is up to date — nothing to upload")
        return
    log.info("syncing %d video(s) to YouTube (%s)", pending, youtube_upload.PRIVACY)
    console.print(youtube_quota_line())
    for row in store.rows.values():
        await YOUTUBE.sync_chapter(row, row_key(row))


async def cmd_upload(ns: argparse.Namespace) -> None:
    """Drive and YouTube first, then MongoDB — the Mongo documents carry their links."""
    if ns.dry_run:
        log.info("dry run — skipping the Drive and YouTube uploads")
    else:
        await cmd_drive(ns)
        await cmd_youtube(ns)
    await cmd_mongo(ns)


def cmd_status(_: argparse.Namespace) -> None:
    store = Store()
    store.load()
    if not store.rows:
        print("No progress.csv yet. Run `python run.py sync`.")
        return
    summarize(store)


def cmd_sync(_: argparse.Namespace) -> None:
    store = Store()
    store.load()
    added = store.sync_from_sheet()
    print(f"Synced sheet → {PROGRESS_CSV} ({added} new row(s), {len(store.rows)} total)")

    downloaded = 0
    for row in store.rows.values():
        thumb_url = (row.get("thumbnail_url") or "").strip()
        if not thumb_url:
            continue
        thumb_dest = chapter_dir(row) / "thumbnail.jpg"
        if not (thumb_dest.exists() and thumb_dest.stat().st_size > 0):
            try:
                asyncio.run(download_thumbnail(thumb_url, thumb_dest))
                downloaded += 1
            except Exception as e:
                log.debug("[%s] thumbnail download deferred: %s", row_key(row), e)
    if downloaded:
        print(f"Downloaded {downloaded} thumbnail(s) to output/")

    cthumb_count = 0
    for row in store.rows.values():
        cth = extract_chapter_thumbnail(chapter_dir(row))
        if cth:
            cthumb_count += 1
    if cthumb_count:
        print(f"Extracted/verified {cthumb_count} chapter PDF thumbnail(s)")

    vthumb_count = 0
    for row in store.rows.values():
        vth = extract_video_thumbnail(chapter_dir(row))
        if vth:
            vthumb_count += 1
    if vthumb_count:
        print(f"Extracted/verified {vthumb_count} video thumbnail(s)")

    try:
        import mongo_sync
        db = mongo_sync.connect()
        updated_books = 0
        now_dt = mongo_sync._now()
        for row in store.rows.values():
            cdir = chapter_dir(row)
            drive_manifest = load_drive_manifest(cdir)
            thumb = (row.get("thumbnail_url") or "").strip()
            book_name = (row.get("book") or row.get("subject") or "").strip()
            chapter_no = mongo_sync._num(row.get("chapter") or "")
            if thumb and book_name:
                res = db[mongo_sync.C["book"]].update_many(
                    {"name": book_name},
                    {"$set": {"thumbnail_url": thumb, "updated_at": now_dt},
                     "$unset": {"image": ""}}
                )
                if res.modified_count > 0:
                    updated_books += res.modified_count
                if chapter_no is not None:
                    ch_entry = drive_manifest.get("chapter_thumbnail.png") or drive_manifest.get("chapter_thumbnail.jpg") or {}
                    ch_thumb = (ch_entry.get("preview_url")
                                or ch_entry.get("view_url")
                                or "").strip()
                    book_doc = db[mongo_sync.C["book"]].find_one({"name": book_name})
                    if book_doc:
                        if ch_thumb:
                            db[mongo_sync.C["chapter"]].update_many(
                                {"book_id": book_doc["_id"], "chapter_number": chapter_no},
                                {"$set": {"thumbnail_url": ch_thumb, "updated_at": now_dt},
                                 "$unset": {"image": ""}}
                            )
                        else:
                            db[mongo_sync.C["chapter"]].update_many(
                                {"book_id": book_doc["_id"], "chapter_number": chapter_no},
                                {"$unset": {"image": "", "thumbnail_url": ""}}
                            )
        if updated_books:
            print(f"Updated {updated_books} book thumbnail(s) in MongoDB")
    except Exception as e:
        log.debug("MongoDB book thumbnail update skipped: %s", e)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help="chapters processed in parallel")
    p.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                   help="tries on a genuinely-failed row before giving up (0 = never give up). "
                        "Does NOT apply to rate-limited rows, which always retry.")
    p.add_argument("--only-failed", action="store_true",
                   help="only (re)process rows in failed/partial state")
    p.add_argument("--limit", type=int, default=0, help="cap number of rows (0 = all)")
    p.add_argument("--artifacts", type=str, default="",
                   help=f"comma list (default: {','.join(ARTIFACTS)}); valid: {','.join(ARTIFACT_SPECS)}")
    p.add_argument("--download-now", action=argparse.BooleanOptionalAction, default=True,
                   help="during `generate`, download each artifact as soon as it is done "
                        "(default on; --no-download-now to disable)")
    p.add_argument("--drive", action=argparse.BooleanOptionalAction, default=True,
                   help="mirror non-media files (PDFs, images) to Google Drive as they are saved "
                        "(default on; skipped with a warning until `drive-login` is done)")
    p.add_argument("--youtube", action=argparse.BooleanOptionalAction, default=True,
                   help="upload each video to YouTube as it is saved "
                        "(default on; skipped with a warning until `youtube-login` is done)")
    p.add_argument("--mongo", action=argparse.BooleanOptionalAction, default=True,
                   help="save each chapter to the phoenix MongoDB when its content changes "
                        "(default on; needs MONGODB_URI in .env)")
    p.add_argument("--usage-every", type=float, default=USAGE_EVERY, metavar="SECONDS",
                   help=f"re-print the live usage tables this often while generating "
                        f"(default {USAGE_EVERY:.0f}; 0 = off)")


# --------------------------------------------------------------------------- #
# Logging: a clean console view + the full detail in pipeline.log
# --------------------------------------------------------------------------- #



SHORT_NAME = {"study_guide": "study", "flashcards": "flash", "mind_map": "mind",
              "slide_deck": "slide", "cinematic_video": "cine", "infographic": "info",
              "chapter_thumbnail": "thumb", "data_table": "table"}
CELL = {"done": ("✓", "bold green"), "failed": ("✗", "bold red"),
        "rate_limited": ("⏸", "yellow"), "": ("·", "bright_black")}
STATUS_STYLE = {"done": "green", "partial": "yellow", "rate_limited": "yellow",
                "failed": "red", "pending": "bright_black"}
_KEY_PALETTE = ["cyan", "magenta", "blue", "green", "bright_cyan", "bright_magenta",
                "bright_blue", "bright_green"]
KEY_WIDTH = 24


def short_key(key: str) -> str:
    """'Class 11|Chemistry|Chapter 1' -> 'C11 · Chemistry · Ch1'."""
    parts = key.split("|")
    if len(parts) != 3:
        return key
    cls, subject, chapter = parts
    return (f"C{cls.replace('Class', '').strip()} · {subject} · "
            f"Ch{chapter.replace('Chapter', '').strip()}")


def key_style(key: str) -> str:
    """A stable colour per chapter, so interleaved lines are easy to follow."""
    return "bold " + _KEY_PALETTE[sum(map(ord, key)) % len(_KEY_PALETTE)]


def show_table(table: Table, plain_lines: list[str]) -> None:
    """Render a table on the console; write a plain-text copy to pipeline.log."""
    console.print(table)
    for line in plain_lines:
        log.info(line, extra={"file_only": True})


TIER_NAMES = {1: "Standard (free)", 2: "Pro", 3: "Ultra 20 TB", 4: "Plus",
              5: "Expanded", 6: "Ultra 30 TB"}


def sid(x: str | None) -> str:
    """Short id for display (full ids stay in progress.csv / pipeline.log)."""
    return (x or "")[:8]


def show_header(title: str, fields: list[tuple[str, Any]]) -> None:
    """A boxed key/value panel at the start of a run."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bright_black", no_wrap=True)
    grid.add_column()
    for label, value in fields:
        grid.add_row(label, value)
    console.print(Panel(grid, title=f"[bold]{title}[/]", title_align="left",
                        border_style="cyan", expand=False))
    for label, value in fields:
        log.info("%s: %s", label, value.plain if isinstance(value, Text) else value,
                 extra={"file_only": True})


def artifact_list(names: list[str]) -> Text:
    out = Text()
    for i, n in enumerate(names):
        if i:
            out.append(" · ", style="bright_black")
        out.append(n, style="bold")
    return out


class ConsoleHandler(logging.Handler):
    """One coloured, aligned line per pipeline event:

        04:56:06  C11 · Chemistry · Ch1    ✓ slide_deck done
        04:50:01  C11 · Chemistry · Ch2    ⚠ slide_deck RATE-LIMITED — will retry
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()  # already scrubbed in place by the redaction filter
            key = ""
            if msg.startswith("[") and "] " in msg:
                raw_key, msg = msg[1:].split("] ", 1)
                key = short_key(raw_key)

            line = Text()
            line.append(datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                        style="bright_black")
            line.append("  ")
            if key:
                line.append(key.ljust(KEY_WIDTH), style=key_style(key))
                line.append(" ")

            if record.levelno >= logging.ERROR:
                icon, style = "✖ ", "bold red"
            elif record.levelno >= logging.WARNING:
                icon, style = "⚠ ", "yellow"
            elif (msg.endswith(" done") or "already present" in msg or msg.endswith("=done")
                  or msg.startswith("source ready")):
                icon, style = "✓ ", "green"
            elif key and msg.startswith(("generating", "retrying", "downloading", "uploading",
                                         "created")):
                icon, style = "▶ ", ""
            elif "waiting" in msg or "in flight" in msg:
                icon, style = "⏳ ", ""
            else:
                icon, style = "  ", ""
            line.append(icon, style=style)

            # Highlight the artifact name wherever it appears first.
            body = Text(msg, style=style)
            for name in sorted(ARTIFACT_SPECS, key=len, reverse=True):  # cinematic_video before video
                idx = msg.find(name)
                if idx != -1:
                    body.stylize("bold " + (style or "white"), idx, idx + len(name))
                    break
            line.append_text(body)
            console.print(line, soft_wrap=True)
        except Exception:  # noqa: BLE001 — logging must never crash the run
            self.handleError(record)


def setup_logging(verbose: bool) -> None:
    """Console: coloured pipeline events only (or everything with -v). File: everything."""
    from notebooklm._logging import apply_redaction

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    file_h = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))

    console_h = ConsoleHandler()
    console_h.addFilter(lambda r: not getattr(r, "file_only", False))
    if not verbose:
        # Library/HTTP chatter (polling requests, RPC errors the pipeline
        # already reports in its own words) stays in pipeline.log only.
        console_h.addFilter(lambda r: r.name == "pipeline" or r.name.startswith("pipeline."))

    # Scrub credentials from both sinks, then drop the library's own stderr
    # handler — it would print its records a second time in another format.
    for h in (file_h, console_h):
        apply_redaction(h)
        root.addHandler(h)
    nb_logger = logging.getLogger("notebooklm")
    for h in list(nb_logger.handlers):
        nb_logger.removeHandler(h)
    nb_logger.setLevel(logging.INFO)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also show library + HTTP request logs on the console")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync", help="pull sheet into progress.csv").set_defaults(fn=cmd_sync)
    sub.add_parser("status", help="print progress summary").set_defaults(fn=cmd_status)
    sub.add_parser("check", help="verify .env + NotebookLM, Drive, YouTube and MongoDB access").set_defaults(
        fn=lambda ns: asyncio.run(cmd_check(ns)))
    m = sub.add_parser("mongo", help="save generated content to the phoenix MongoDB")
    m.add_argument("--dry-run", action="store_true", help="show what would be saved, write nothing")
    m.add_argument("--force", action="store_true", help="re-save chapters even if unchanged")
    m.set_defaults(fn=lambda ns: asyncio.run(cmd_mongo(ns)))
    u = sub.add_parser("upload", help="upload output/ to Drive and YouTube, then save to "
                                      "MongoDB (`drive` + `youtube` + `mongo` in one go)")
    u.add_argument("--dry-run", action="store_true",
                   help="upload nothing; show what would be saved to MongoDB")
    u.add_argument("--force", action="store_true",
                   help="re-save chapters to MongoDB even if unchanged")
    u.set_defaults(fn=lambda ns: asyncio.run(cmd_upload(ns)))
    sub.add_parser("drive-login", help="sign in to Google Drive (one time)").set_defaults(
        fn=cmd_drive_login)
    sub.add_parser("drive", help="upload any local output file not yet on Drive").set_defaults(
        fn=lambda ns: asyncio.run(cmd_drive(ns)))
    sub.add_parser("youtube-login", help="sign in to YouTube (one time)").set_defaults(
        fn=cmd_youtube_login)
    sub.add_parser("youtube", help="upload any local video not yet on YouTube").set_defaults(
        fn=lambda ns: asyncio.run(cmd_youtube(ns)))

    def locked(coro_factory: Callable[[argparse.Namespace], Awaitable[None]]):
        """Run an async phase under the single-run lock."""
        def runner(ns: argparse.Namespace) -> None:
            acquire_lock()
            try:
                asyncio.run(coro_factory(ns))
            except KeyboardInterrupt:
                log.warning("interrupted — progress is saved; re-run the same command to resume")
                sys.exit(130)
            finally:
                release_lock()
        return runner

    sub.add_parser("titles", help="ask each notebook for its chapter's real name "
                                  "(fills chapter_title in progress.csv)").set_defaults(
        fn=locked(cmd_titles))

    g = sub.add_parser("generate", help="phase 1: upload + generate")
    add_common(g)
    g.set_defaults(fn=locked(phase_generate))

    d = sub.add_parser("download", help="phase 2: download artifacts")
    add_common(d)
    d.set_defaults(fn=locked(phase_download))

    a = sub.add_parser("all", help="generate then download")
    add_common(a)

    async def _all(ns: argparse.Namespace) -> None:
        await phase_generate(ns)
        await phase_download(ns)

    a.set_defaults(fn=locked(_all))

    args = parser.parse_args()
    setup_logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
