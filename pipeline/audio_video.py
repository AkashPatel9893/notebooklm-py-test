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
FONTS = (  # first one that exists wins; Pillow's built-in font otherwise
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


class AudioVideoError(RuntimeError):
    """ffmpeg / Pillow missing, or ffmpeg failed."""


def stale(cdir: Path) -> bool:
    """True when audio.m4a exists and audio_video.mp4 is missing or older than it."""
    audio, video = cdir / AUDIO, cdir / VIDEO
    if not audio.is_file():
        return False
    return not video.is_file() or video.stat().st_mtime < audio.stat().st_mtime


def _font(size: int):
    from PIL import ImageFont

    for path in FONTS:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _cover(path: Path, title: str, subtitle: str) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise AudioVideoError("Pillow isn't installed — pip install -r pipeline/requirements.txt") from e

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    x = 96
    d.rectangle((x, 150, x + 120, 158), fill=ACCENT)
    d.text((x, 185), "AUDIO OVERVIEW", font=_font(34), fill=ACCENT)
    title_font = _font(68 if len(title) <= 40 else 54)
    lines = textwrap.wrap(title, width=28 if len(title) <= 40 else 36)[:3]
    y = 250
    for line in lines:
        d.text((x, y), line, font=title_font, fill=FG)
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
