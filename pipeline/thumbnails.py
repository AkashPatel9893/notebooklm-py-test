#!/usr/bin/env python3
"""Create lossless WebP previews for NCERT chapter assets.

Run separately with ``python pipeline/thumbnails.py [chapter-directory]``.
Originals are retained. Generated files are resized before lossless encoding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output"
MANIFEST = ".thumbnails.json"
VERSION = 2


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save(image: Image.Image, target: Path, max_width: int) -> None:
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
    if image.width > max_width:
        image.thumbnail((max_width, 10000), Image.Resampling.LANCZOS)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        image.save(temporary, format="WEBP", lossless=True, method=6)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def generate(directory: Path, force: bool = False) -> list[Path]:
    """Generate available previews; return paths produced or already up to date."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    try:
        cache = json.loads((directory / MANIFEST).read_text())
    except (FileNotFoundError, ValueError):
        cache = {}
    produced: list[Path] = []

    def current(source: Path, outputs: list[Path], width: int) -> bool:
        signature = f"{VERSION}:{width}:{_hash(source)}"
        if not force and cache.get(source.name) == signature and all(p.is_file() for p in outputs):
            produced.extend(outputs)
            return True
        cache.pop(source.name, None)
        return False

    def record(source: Path, outputs: list[Path], width: int) -> None:
        cache[source.name] = f"{VERSION}:{width}:{_hash(source)}"
        produced.extend(outputs)

    for filename, output, width in (
        ("thumbnail.jpg", "book_thumbnail.webp", 800),
        ("infographic.png", "infographic_thumbnail.webp", 800),
        ("video_thumbnail.jpg", "video_thumbnail.webp", 800),
    ):
        source, target = directory / filename, directory / output
        if not source.is_file() or current(source, [target], width):
            continue
        try:
            with Image.open(source) as image:
                _save(image, target, width)
            record(source, [target], width)
        except Exception as exc:
            log.warning("Cannot render %s: %s", source, exc)

    for filename, prefix, count in (("source.pdf", "chapter_thumbnail", 1), ("slides.pdf", "slides_page", None)):
        source = directory / filename
        if not source.is_file():
            continue
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(source)
            try:
                page_count = min(count, len(pdf)) if count is not None else len(pdf)
                outputs = [directory / (f"{prefix}.webp" if count == 1 else f"{prefix}_{n + 1}.webp") for n in range(page_count)]
                if outputs and not current(source, outputs, 800):
                    for n, target in enumerate(outputs):
                        page = pdf[n]
                        try:
                            # Render at the requested output resolution, avoiding oversized bitmaps.
                            image = page.render(scale=800 / page.get_width()).to_pil()
                            _save(image, target, 800)
                        finally:
                            page.close()
                    record(source, outputs, 800)
                if count is None:
                    for old in directory.glob("slides_page_*.webp"):
                        number = old.stem.removeprefix("slides_page_")
                        if number.isdigit() and int(number) > len(outputs):
                            old.unlink(missing_ok=True)
            finally:
                pdf.close()
        except Exception as exc:
            log.warning("Cannot render %s: %s", source, exc)

    # The podcast artwork is the chapter cover when no separate artwork exists.
    chapter = directory / "chapter_thumbnail.webp"
    podcast = directory / "podcast_thumbnail.webp"
    if chapter.is_file() and not current(chapter, [podcast], 800):
        try:
            with Image.open(chapter) as image:
                _save(image, podcast, 800)
            record(chapter, [podcast], 800)
        except Exception as exc:
            log.warning("Cannot render podcast artwork in %s: %s", directory, exc)
    temporary = directory / f".{MANIFEST}.tmp"
    temporary.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    os.replace(temporary, directory / MANIFEST)
    return produced


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_OUTPUT, help="Chapter directory or output tree")
    parser.add_argument("--force", action="store_true", help="Regenerate even unchanged previews")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if not args.path.is_dir():
        parser.error(f"directory does not exist: {args.path}")
    directories = {p.parent for name in ("source.pdf", "slides.pdf", "infographic.png", "thumbnail.jpg", "video_thumbnail.jpg") for p in args.path.rglob(name)}
    for directory in sorted(directories):
        print(f"{directory}: {len(generate(directory, force=args.force))} previews")


if __name__ == "__main__":
    main()
