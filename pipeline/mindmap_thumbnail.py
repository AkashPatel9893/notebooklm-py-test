#!/usr/bin/env python3
"""Render a mind-map JSON document as a lossless WebP preview."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PALETTE = [('#A780B9', '#F1E8F5'), ('#C49857', '#F7EEDC'),
           ('#729F90', '#E4F0E9'), ('#779BB6', '#E6EFF6'),
           ('#BE8790', '#F7E8EB'), ('#999B65', '#EFF0DF')]


def font(size: int, bold: bool = False):
    candidates = [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def fit(draw: ImageDraw.ImageDraw, text: str, f, max_width: int) -> str:
    text = ' '.join(text.split())
    if draw.textlength(text, font=f) <= max_width:
        return text
    while text and draw.textlength(text + '…', font=f) > max_width:
        text = text[:-1]
    return text.rstrip() + '…'


def two_lines(draw: ImageDraw.ImageDraw, text: str, f, max_width: int) -> str:
    words = text.split()
    lines = ['']
    for word in words:
        candidate = f'{lines[-1]} {word}'.strip()
        if draw.textlength(candidate, font=f) <= max_width:
            lines[-1] = candidate
        elif len(lines) == 1:
            lines.append(word)
        else:
            lines[-1] += ' ' + word
    return '\n'.join(fit(draw, line, f, max_width) for line in lines)


def render(root: dict, target: Path) -> Path:
    if not isinstance(root, dict):
        raise ValueError('Mind map must have a root node')
    root_text = str(root.get('text') or root.get('name') or '').strip()
    if not root_text:
        raise ValueError('Mind map root has no title')
    children = [child for child in (root.get('children') or []) if isinstance(child, dict)]
    children = [child for child in children if child.get('text') or child.get('name')][:6]
    image = Image.new('RGB', (960, 560), '#F5F0E5')
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((16, 16, 944, 544), radius=38, fill='#FFFCF5', outline='#EBE2D2', width=3)
    label_font, root_font, caption_font = font(24, True), font(25, True), font(16)
    left_count = (len(children) + 1) // 2
    for index, child in enumerate(children):
        left = index < left_count
        row = index if left else index - left_count
        count = left_count if left else len(children) - left_count
        y = int(270 + (row - (count - 1) / 2) * 136)
        x1, x2 = (42, 325) if left else (635, 918)
        stroke, fill = PALETTE[index]
        draw.line([(480, 270), ((x2 if left else x1) + (28 if left else -28), y), (x2 if left else x1, y)], fill=stroke, width=6, joint='curve')
        draw.rounded_rectangle((x1, y - 41, x2, y + 41), radius=17, fill=fill, outline=stroke, width=3)
        label = two_lines(draw, str(child.get('text') or child.get('name') or ''), label_font, x2-x1-28)
        draw.multiline_text(((x1+x2)/2, y), label, font=label_font, fill='#40382F', anchor='mm', align='center', spacing=4)
    draw.rounded_rectangle((340, 216, 620, 324), radius=30, fill='#8A5A2B', outline='#FFF8EA', width=5)
    draw.multiline_text((480, 269), two_lines(draw, root_text, root_font, 245), font=root_font, fill='white', anchor='mm', align='center', spacing=5)
    if len(root.get('children') or []) > len(children):
        draw.text((480, 503), f"+{len(root['children']) - len(children)} more concepts", font=caption_font, fill='#766B5A', anchor='mm')
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f'.{target.name}.tmp')
    try:
        image.save(temp, format='WEBP', lossless=True, method=6)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    doc = json.loads(args.input.read_text(encoding='utf-8'))
    print(render(doc.get('root') or doc, args.output))


if __name__ == '__main__':
    main()
