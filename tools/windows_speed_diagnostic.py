from __future__ import annotations

import json
import os
import platform
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path.cwd()
if (REPO_ROOT / "platform_entry").is_dir():
    sys.path.insert(0, str(REPO_ROOT))

from platform_entry.adapter.textbook_page import _find_dropdown_option, _select_option


VALUES = [
    "同步课文",
    "人教版",
    "初中",
    "九年级",
    "上册",
    "Unit 1",
    "Reading Plus",
]


def _html() -> str:
    controls = []
    for index, value in enumerate(VALUES):
        controls.append(
            f"""
            <div class="field">
              <div class="el-select__wrapper" data-index="{index}">请选择</div>
              <ul class="el-select-dropdown" data-index="{index}" hidden>
                <li class="el-select-dropdown__item" role="option">{value}</li>
              </ul>
            </div>
            """
        )
    return """
    <!doctype html>
    <style>
      .field { margin: 8px; }
      .el-select__wrapper { border: 1px solid #888; padding: 8px; width: 180px; }
      .el-select-dropdown { margin: 0; padding: 0; list-style: none; }
      .el-select-dropdown__item { padding: 8px; }
    </style>
    <main>""" + "".join(controls) + """</main>
    <script>
      const wrappers = [...document.querySelectorAll('.el-select__wrapper')];
      const dropdowns = [...document.querySelectorAll('.el-select-dropdown')];
      wrappers.forEach((wrapper, index) => {
        const dropdown = dropdowns[index];
        const option = dropdown.querySelector('[role=option]');
        wrapper.addEventListener('click', () => {
          dropdowns.forEach((candidate) => { candidate.hidden = true; });
          dropdown.hidden = false;
        });
        option.addEventListener('click', () => {
          setTimeout(() => {
            wrapper.textContent = option.textContent;
            dropdown.hidden = true;
          }, 20);
        });
      });
      window.resetDiagnostic = () => {
        wrappers.forEach((wrapper) => { wrapper.textContent = '请选择'; });
        dropdowns.forEach((dropdown) => { dropdown.hidden = true; });
      };
    </script>
    """


def _reset(page) -> None:
    page.evaluate("window.resetDiagnostic()")


def _select_option_before_optimization(page, index: int, value: str) -> None:
    selectors = page.locator(".el-select__wrapper:visible")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and selectors.count() <= index:
        page.wait_for_timeout(200)
    if selectors.count() <= index:
        raise RuntimeError(f"missing select {index}")

    selector = selectors.nth(index)
    selector.click()
    option = None
    open_deadline = time.monotonic() + 12
    next_retry = time.monotonic() + 2.5
    while time.monotonic() < open_deadline:
        option = _find_dropdown_option(page, value)
        if option is not None:
            break
        if time.monotonic() >= next_retry:
            selector.click()
            next_retry = time.monotonic() + 2.5
        page.wait_for_timeout(250)
    if option is None:
        raise RuntimeError(f"missing option {value}")
    option.click()
    expected = "".join(value.split()).casefold()
    shown = ""
    selected_deadline = time.monotonic() + 3
    while time.monotonic() < selected_deadline:
        shown = "".join(str(selector.inner_text() or "").split())
        if expected in shown.casefold():
            return
        page.wait_for_timeout(50)
    raise RuntimeError(f"readback mismatch: {value!r} -> {shown!r}")


def _run(page, chooser, rounds: int) -> float:
    started = time.perf_counter()
    for _ in range(rounds):
        _reset(page)
        for index, value in enumerate(VALUES):
            chooser(page, index, value)
    return (time.perf_counter() - started) * 1000


def main() -> None:
    output_path = Path(os.environ.get("DIAGNOSTIC_OUTPUT", "windows-speed-results.json"))
    trace_path = Path(os.environ.get("DIAGNOSTIC_TRACE", "windows-speed-trace.zip"))
    rounds = int(os.environ.get("DIAGNOSTIC_ROUNDS", "30"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        old_page = context.new_page()
        new_page = context.new_page()
        old_page.set_content(_html())
        new_page.set_content(_html())
        _run(old_page, _select_option_before_optimization, 2)
        _run(new_page, _select_option, 2)

        old_ms = _run(old_page, _select_option_before_optimization, rounds)
        context.tracing.start(screenshots=False, snapshots=True, sources=False)
        new_ms = _run(new_page, _select_option, rounds)
        context.tracing.stop(path=str(trace_path))
        browser.close()

    result = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version,
        "rounds": rounds,
        "selects_per_round": len(VALUES),
        "before_ms": round(old_ms, 2),
        "optimized_ms": round(new_ms, 2),
        "before_per_select_ms": round(old_ms / (rounds * len(VALUES)), 2),
        "optimized_per_select_ms": round(new_ms / (rounds * len(VALUES)), 2),
        "speedup": round(old_ms / new_ms, 2) if new_ms else None,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
