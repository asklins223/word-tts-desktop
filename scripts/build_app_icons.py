#!/usr/bin/env python3
"""Generate WordTTS PNG, macOS ICNS, and Windows ICO assets from one source."""

from __future__ import annotations

import argparse
import math
import os
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT_DIR / "electron" / "build" / "icon-source.png"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "electron" / "build"


def remove_connected_light_background(image: Image.Image) -> Image.Image:
    """Remove only the light backdrop connected to the canvas edge.

    Image-generated icon sources often contain an opaque white area outside the
    rounded-square artwork. A connected flood fill avoids erasing cream-colored
    details inside the pig, desk, or laptop.
    """

    rgba = image.convert("RGBA")
    rgb = rgba.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    alpha = Image.new("L", (width, height), 255)
    alpha_pixels = alpha.load()
    visited = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()

    corner_samples = [
        pixels[0, 0],
        pixels[width - 1, 0],
        pixels[0, height - 1],
        pixels[width - 1, height - 1],
    ]
    key = tuple(round(sum(sample[channel] for sample in corner_samples) / 4) for channel in range(3))
    threshold = 48.0

    def is_background(x: int, y: int) -> bool:
        color = pixels[x, y]
        distance = math.sqrt(sum((color[channel] - key[channel]) ** 2 for channel in range(3)))
        return distance <= threshold

    def enqueue(x: int, y: int) -> None:
        index = y * width + x
        if visited[index] or not is_background(x, y):
            return
        visited[index] = 1
        queue.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while queue:
        x, y = queue.popleft()
        alpha_pixels[x, y] = 0
        if x > 0:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y > 0:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)

    alpha = alpha.filter(ImageFilter.GaussianBlur(0.55))
    rgba.putalpha(alpha)
    return rgba


def resized(image: Image.Image, size: int) -> Image.Image:
    return image.resize((size, size), Image.Resampling.LANCZOS)


def build_icons(source: Path, output_dir: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Icon source not found: {source}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        if opened.width != opened.height:
            raise ValueError(f"Icon source must be square, got {opened.size}")
        icon = resized(remove_connected_light_background(opened), 1024)

    png_path = output_dir / "icon.png"
    ico_path = output_dir / "icon.ico"
    icns_path = output_dir / "icon.icns"
    icon.save(png_path, format="PNG", optimize=True)
    icon.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    # Pillow writes the standard 32/64/128/256/512/1024 ICNS representations,
    # so the same script works on Windows CI and macOS without iconutil.
    icon.save(icns_path, format="ICNS")

    for asset in (png_path, icns_path, ico_path):
        print(f"[icon] {asset.relative_to(ROOT_DIR)} ({os.path.getsize(asset)} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    build_icons(args.source.resolve(), args.output_dir.resolve())


if __name__ == "__main__":
    main()
