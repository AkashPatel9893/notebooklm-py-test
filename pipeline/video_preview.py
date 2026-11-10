#!/usr/bin/env python3
"""Create a looping GIF from short chronological samples throughout a video."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image


def create_preview(video: Path, output: Path, *, width: int = 320) -> Path:
    if not video.is_file():
        raise FileNotFoundError(f"Video not found: {video}")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe are required to create a video preview")
    if width <= 0:
        raise ValueError("Width must be positive")
    try:
        info = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", str(video)],
            check=True, capture_output=True, text=True,
        )
        duration = float(info.stdout.strip())
        if not 0 < duration < float("inf"):
            raise ValueError("Video duration must be positive and finite")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ncert-gif-frames-") as temp:
            directory = Path(temp)
            # Ten short scenes, selected in order between 5% and 95% of the video.
            for scene in range(10):
                start = min(duration * (0.05 + scene * 0.10), max(0, duration - 0.6))
                subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(start),
                     "-i", str(video), "-t", "0.6", "-vf", f"fps=8,scale={width}:-2:flags=lanczos",
                     "-frames:v", "4", "-y", str(directory / f"{scene:02d}-%02d.png")],
                    check=True, capture_output=True, text=True,
                )
            frames = []
            for path in sorted(directory.glob("*.png")):
                with Image.open(path) as image:
                    frames.append(image.convert("RGB"))
            if len(frames) < 2:
                raise RuntimeError("Could not extract enough frames for an animated preview")
            temporary = output.with_name(f".{output.stem}.tmp.gif")
            try:
                frames[0].save(temporary, format="GIF", save_all=True, append_images=frames[1:],
                               duration=125, loop=0, disposal=2, optimize=False)
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
    except (subprocess.CalledProcessError, ValueError) as exc:
        detail = exc.stderr.strip()[-500:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise RuntimeError(f"Unable to create GIF preview: {detail}") from exc
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(create_preview(args.video, args.output))


if __name__ == "__main__":
    main()
