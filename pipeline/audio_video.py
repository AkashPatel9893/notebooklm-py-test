"""Turn a chapter's audio overview into an MP4 YouTube will take.

YouTube rejects audio-only uploads, so ``audio.m4a`` is paired with a still
title card (chapter name + class/subject) into ``audio_video.mp4``. The audio
stream is copied as-is (no re-encode) and the picture runs at 1 fps, so the
MP4 is barely bigger than the audio and takes seconds to build.

Needs ``ffmpeg`` on PATH and Pillow (``pip install -r requirements.txt``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

AUDIO = "audio.m4a"
VIDEO = "audio_video.mp4"

W, H = 1280, 720
BG, FG, MUTED, ACCENT = "#0f172a", "#f8fafc", "#94a3b8", "#f59e0b"
# Per script, first (path, face index) that exists wins; Pillow's built-in font
# otherwise. Hindi/Sanskrit/Urdu titles also need Pillow built with libraqm
# (brew install libraqm) to place vowel signs — without it the card falls back
# to the English "Subject · Chapter" heading.
FONTS = {
    "latin": (("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
              ("/System/Library/Fonts/Helvetica.ttc", 1),
              ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
              ("C:/Windows/Fonts/arialbd.ttf", 0)),
    "devanagari": (("/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc", 1),
                   ("/System/Library/Fonts/Kohinoor.ttc", 0),
                   ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 0),
                   ("C:/Windows/Fonts/Nirmala.ttc", 1)),
    "arabic": (("/System/Library/Fonts/NotoNastaliq.ttc", 0),
               ("/System/Library/Fonts/GeezaPro.ttc", 1),
               ("/usr/share/fonts/truetype/noto/NotoNastaliqUrdu-Regular.ttf", 0),
               ("C:/Windows/Fonts/Nirmala.ttc", 1)),
}


class AudioVideoError(RuntimeError):
    """ffmpeg / Pillow missing, or ffmpeg failed."""


def stale(cdir: Path) -> bool:
    """True when audio.m4a exists and audio_video.mp4 is missing or older than it."""
    audio, video = cdir / AUDIO, cdir / VIDEO
    if not audio.is_file():
        return False
    return not video.is_file() or video.stat().st_mtime < audio.stat().st_mtime


def duration(path: Path) -> float | None:
    """Length in seconds (ffprobe), or None when it can't be read."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def _script(text: str) -> str:
    if any("\u0900" <= c <= "\u097f" for c in text):
        return "devanagari"
    if any("\u0600" <= c <= "\u06ff" for c in text):
        return "arabic"
    return "latin"


def _font(size: int, script: str = "latin"):
    from PIL import ImageFont

    for path, index in FONTS[script]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size, index=index)
    return ImageFont.load_default(size=size)


def _cover(path: Path, title: str, subtitle: str) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise AudioVideoError("Pillow isn't installed — pip install -r pipeline/requirements.txt") from e

    from PIL import features

    script = _script(title)
    if script != "latin" and not features.check("raqm"):
        title, script = subtitle.removeprefix("NCERT ").split(" · ", 1)[-1], "latin"
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    x = 96
    d.rectangle((x, 150, x + 120, 158), fill=ACCENT)
    d.text((x, 185), "AUDIO OVERVIEW", font=_font(34), fill=ACCENT)
    title_font = _font(68 if len(title) <= 40 else 54, script)
    lines = textwrap.wrap(title, width=28 if len(title) <= 40 else 36)[:3]
    y = 250
    for line in lines:
        d.text((x, y), line, font=title_font, fill=FG)  # libraqm picks the direction
        y += title_font.size + 18
    d.text((x, H - 140), subtitle, font=_font(36), fill=MUTED)
    img.save(path)


def make(cdir: Path, title: str, subtitle: str) -> Path:
    """Build cdir/audio_video.mp4 from cdir/audio.m4a; returns its path."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioVideoError("ffmpeg isn't installed (brew install ffmpeg)")
    out = cdir / VIDEO
    tmp = cdir / f".{VIDEO}.tmp.mp4"
    with tempfile.TemporaryDirectory() as td:
        cover = Path(td) / "cover.png"
        _cover(cover, title, subtitle)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-loop", "1", "-framerate", "1", "-i", str(cover),
               "-i", str(cdir / AUDIO),
               "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "1",
               "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise AudioVideoError(f"ffmpeg failed: {r.stderr.strip()[-500:]}")
    os.replace(tmp, out)
    return out


def extract_frame(video_path: Path, out_path: Path, timestamp: str = "00:00:03") -> Path:
    """Extract a single frame from video_path and save to out_path using ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioVideoError("ffmpeg isn't installed (brew install ffmpeg)")
    tmp = out_path.parent / f".{out_path.name}.tmp.jpg"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", timestamp, "-i", str(video_path),
        "-vframes", "1", "-q:v", "2", str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        # Try at 00:00:01 in case the video is short
        cmd[4] = "00:00:01"
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise AudioVideoError(f"ffmpeg frame extract failed: {r.stderr.strip()[-500:]}")
    os.replace(tmp, out_path)
    return out_path
