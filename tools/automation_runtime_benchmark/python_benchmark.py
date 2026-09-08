"""Cross-runtime Playwright benchmark for the same visible-page workflow.

The fixture is local and deterministic.  Each round exercises the operations
that are expensive in the real app: text fields, seven single-selects, one
multi-select, a template choice, six lazy sections with editor/input/file and
answer actions, then delayed audio-result selection and download.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


SELECTS = (
    ("category", "听说考试"),
    ("province", "广东省"),
    ("city", "广州市"),
    ("district", "天河区"),
    ("stage", "初中"),
    ("grade", "九年级"),
    ("kind", "中考模拟"),
)
MULTI_SELECT = ("districts", ("天河区", "越秀区", "海珠区"))


def _wait_for_downloads(page: Any, downloads: list[Any], expected: int) -> None:
    deadline = time.monotonic() + 10
    while len(downloads) < expected and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    if len(downloads) != expected:
        raise RuntimeError(f"expected {expected} downloads, got {len(downloads)}")


def _run_round(
    page: Any,
    fixture: Path,
    audio_path: Path,
    output_dir: Path,
    round_no: int,
) -> dict[str, float]:
    page.set_content(fixture.read_text(encoding="utf-8"))
    total_started = time.perf_counter()

    started = time.perf_counter()
    page.locator('[data-testid="paper-title"]').fill(f"benchmark-{round_no}")
    page.locator('[data-testid="paper-year"]').fill("2026")
    page.locator('[data-testid="paper-duration"]').fill("45")
    for key, value in SELECTS:
        field = page.locator(f'[data-testid="select-{key}"]')
        field.locator(".select-trigger").click()
        option = field.locator(".option").filter(has_text=value).last
        option.wait_for(state="visible")
        option.click()
        field.locator(".select-trigger").filter(has_text=value).wait_for(state="visible")
    field = page.locator('[data-testid="select-districts"]')
    for value in MULTI_SELECT[1]:
        field.locator(".select-trigger").click()
        option = field.locator(".option").filter(has_text=value).last
        option.wait_for(state="visible")
        option.click()
    field.locator(".select-trigger").filter(has_text="海珠区").wait_for(state="visible")
    page.locator('[data-testid="template-choice"]').check()
    page.locator('[data-testid="next"]').click()
    page.locator("#content-stage").wait_for(state="visible")
    base_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    for section_index in range(6):
        page.locator(f'.outline[data-section="{section_index}"]').click()
        section = page.locator(f'.content-section[data-section="{section_index}"]')
        section.wait_for(state="visible")
        cards = section.locator(".question-card")
        if cards.count() != 3:
            raise RuntimeError(f"section {section_index} card count mismatch")
        for card_index in range(3):
            card = cards.nth(card_index)
            card.locator('.editor[data-testid="prompt"]').fill(
                f"section {section_index} prompt {card_index}"
            )
            card.locator('.editor[data-testid="translation"]').fill(
                f"section {section_index} translation {card_index}"
            )
            card.locator('[data-testid="score"]').fill("1")
            card.locator('[data-testid="duration"]').fill("5")
            card.locator('[data-testid="audio"]').set_input_files(str(audio_path))
            card.locator(".audio-name").filter(has_text=audio_path.name).wait_for(state="visible")
            card.locator('.answer[data-answer="B"]').click()
    page.locator('[data-testid="save"]').click()
    page.locator('[data-testid="save-status"]').filter(has_text="保存成功").wait_for(state="visible")
    content_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    page.locator('[data-testid="generate"]').click()
    works = page.locator("#works")
    works.wait_for(state="visible")
    rows = works.locator('input[type="checkbox"]')
    for index in (0, 2, 4):
        rows.nth(index).check()
    download_button = page.locator('[data-testid="download"]')
    download_button.wait_for(state="visible")
    page.wait_for_function(
        "selector => !document.querySelector(selector).disabled",
        arg='[data-testid="download"]',
    )
    downloads: list[Any] = []
    page.on("download", lambda download: downloads.append(download))
    download_button.click()
    page.locator("#download-modal").wait_for(state="visible")
    page.locator('[data-testid="confirm-download"]').click()
    _wait_for_downloads(page, downloads, 3)
    for download in downloads:
        download.save_as(str(output_dir / download.suggested_filename))
    audio_ms = (time.perf_counter() - started) * 1000

    return {
        "round": round_no,
        "base_ms": round(base_ms, 2),
        "content_ms": round(content_ms, 2),
        "audio_ms": round(audio_ms, 2),
        "total_ms": round((time.perf_counter() - total_started) * 1000, 2),
    }


def _summary(records: list[dict[str, float]]) -> dict[str, float]:
    return {
        key: round(statistics.median(float(record[key]) for record in records), 2)
        for key in ("base_ms", "content_ms", "audio_ms", "total_ms")
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--fixture", type=Path, default=Path(__file__).with_name("fixture.html"))
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1:
        raise SystemExit("--rounds must be positive")
    fixture = args.fixture.resolve()
    if not fixture.is_file():
        raise SystemExit(f"fixture not found: {fixture}")

    with tempfile.TemporaryDirectory(prefix="automation-runtime-") as temporary:
        temporary_path = Path(temporary)
        audio_path = temporary_path / "benchmark-audio.mp3"
        audio_path.write_bytes(b"ID3\x04\x00runtime-benchmark")
        output_dir = temporary_path / "downloads"
        output_dir.mkdir()
        with sync_playwright() as playwright:
            started = time.perf_counter()
            browser = playwright.chromium.launch(
                headless=True,
                args=("--disable-background-networking", "--no-first-run", "--disable-dev-shm-usage"),
            )
            launch_ms = (time.perf_counter() - started) * 1000
            records: list[dict[str, float]] = []
            try:
                for round_no in range(1, args.rounds + 1):
                    page = browser.new_page(viewport={"width": 1280, "height": 720})
                    try:
                        records.append(
                            _run_round(
                                page,
                                fixture,
                                audio_path,
                                output_dir,
                                round_no,
                            )
                        )
                    finally:
                        page.close()
            finally:
                browser.close()

    result = {
        "runtime": args.runtime,
        "host_platform": platform.platform(),
        "python": platform.python_version(),
        "playwright": importlib.metadata.version("playwright"),
        "rounds": args.rounds,
        "launch_ms": round(launch_ms, 2),
        "records": records,
        "median_ms": _summary(records),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
