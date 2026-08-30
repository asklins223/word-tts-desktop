#!/usr/bin/env python3
"""Generate 小猪wordTTS PNG, macOS ICNS, and Windows ICO assets from a full-bleed source."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from PIL import Image


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT_DIR / "electron" / "build" / "icon-source.png"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "electron" / "build"
DEFAULT_RENDERER_ICON = ROOT_DIR / "electron" / "renderer" / "assets" / "app-icon.png"


def resized(image: Image.Image, size: int) -> Image.Image:
    return image.resize((size, size), Image.Resampling.LANCZOS)


def build_icons(source: Path, output_dir: Path, renderer_icon_path: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Icon source not found: {source}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        if opened.width != opened.height:
            raise ValueError(f"Icon source must be square, got {opened.size}")
        # The source is intentionally full-bleed: preserve its sky-blue corners
        # instead of flood-filling the edge color into transparency.
        icon = resized(opened.convert("RGBA"), 1024)

    png_path = output_dir / "icon.png"
    ico_path = output_dir / "icon.ico"
    icns_path = output_dir / "icon.icns"
    renderer_icon_path.parent.mkdir(parents=True, exist_ok=True)
    icon.save(png_path, format="PNG", optimize=True)
    resized(icon, 128).save(renderer_icon_path, format="PNG", optimize=True)
    icon.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    # Pillow writes the standard 32/64/128/256/512/1024 ICNS representations,
    # so the same script works on Windows CI and macOS without iconutil.
    icon.save(icns_path, format="ICNS")

    for asset in (png_path, renderer_icon_path, icns_path, ico_path):
        print(f"[icon] {asset.relative_to(ROOT_DIR)} ({os.path.getsize(asset)} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--renderer-icon", type=Path, default=DEFAULT_RENDERER_ICON)
    args = parser.parse_args()
    build_icons(args.source.resolve(), args.output_dir.resolve(), args.renderer_icon.resolve())


if __name__ == "__main__":
    main()
