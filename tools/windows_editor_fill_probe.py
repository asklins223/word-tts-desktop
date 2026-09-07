from __future__ import annotations

import json
import os
import platform
import time

from playwright.sync_api import sync_playwright

from windows_composite_diagnostic import ITEMS, _ensure_card_count, _html
from platform_entry.adapter.textbook_page import _replace_editor


def _bench(page, strategy: str, rounds: int) -> float:
    total = 0.0
    for _ in range(rounds):
        page.evaluate("window.resetDiagnostic()")
        cards = _ensure_card_count(page, len(ITEMS))
        for index, (original, translation) in enumerate(ITEMS):
            editors = cards.nth(index).locator(
                '.editor-content[contenteditable="true"]'
            )
            for editor_index, value in enumerate((original, translation)):
                editor = editors.nth(editor_index)
                started = time.perf_counter()
                if strategy == "fill":
                    editor.fill(value)
                else:
                    _replace_editor(page, editor, value, "编辑器")
                actual = str(editor.inner_text() or "")
                if value not in actual:
                    raise RuntimeError(
                        f"{strategy} readback mismatch: {value!r} != {actual!r}"
                    )
                total += (time.perf_counter() - started) * 1000
    return total / (rounds * len(ITEMS) * 2)


def main() -> None:
    rounds = int(os.environ.get("DIAGNOSTIC_ROUNDS", "5"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_content(_html())
        current = _bench(page, "current", rounds)
        fill = _bench(page, "fill", rounds)
        browser.close()
    result = {
        "platform": platform.platform(),
        "rounds": rounds,
        "editors_per_record": len(ITEMS) * 2,
        "current_ms_per_editor": round(current, 2),
        "fill_ms_per_editor": round(fill, 2),
        "fill_speedup": round(current / fill, 2),
    }
    with open("windows-editor-fill-results.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
