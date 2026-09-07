from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path.cwd()
if (REPO_ROOT / "platform_entry").is_dir():
    sys.path.insert(0, str(REPO_ROOT))

from platform_entry.adapter.textbook_page import (  # noqa: E402
    _ensure_card_count,
    _find_dropdown_option,
    _replace_editor,
    _select_option,
)
from platform_entry.adapter.automation import PlatformInputPageAutomation  # noqa: E402


ITEMS = [
    ("This is the first sentence.", "这是第一句话。"),
    ("Please listen and repeat.", "请听并跟读。"),
    ("We practice the dialogue together.", "我们一起练习对话。"),
    ("The lesson is finished.", "课文学习结束。"),
]

FORM_SINGLE_CHOICES = [
    ("试卷分类", "听说考试"),
    ("省份", "广东省"),
    ("城市", "广州市"),
    ("学段", "初中"),
    ("年级", "九年级"),
    ("试卷类型", "中考模拟"),
]
FORM_MULTI_CHOICES = ("区/县", ("天河区", "越秀区"))


def _record(name_zh: str, name_en: str, audio_path: str) -> dict:
    return {
        "name_zh": name_zh,
        "name_en": name_en,
        "form": "同步课文",
        "version": "人教版",
        "stage": "初中",
        "grade": "九年级",
        "volume": "上册",
        "unit": "Unit 1",
        "lesson": "Reading Plus",
        "items": [
            {"original": original, "translation": translation, "audio_path": audio_path}
            for original, translation in ITEMS
        ],
    }


def _html() -> str:
    values = ["同步课文", "人教版", "初中", "九年级", "上册", "Unit 1", "Reading plus"]
    controls = []
    for index, value in enumerate(values):
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
    cards = []
    for index in range(len(ITEMS)):
        cards.append(
            f"""
            <article class="expandContent" data-card="{index}">
              <div class="rich-text-editor">
                <div class="editor-content" contenteditable="true" data-placeholder="原文">旧原文 {index}</div>
              </div>
              <div class="rich-text-editor">
                <div class="editor-content" contenteditable="true" data-placeholder="译文">旧译文 {index}</div>
              </div>
              <input type="file" accept="audio/*">
              <span class="audio-name"></span>
            </article>
            """
        )
    return """
    <!doctype html>
    <style>
      .field { margin: 8px; }
      .el-select__wrapper { border: 1px solid #888; padding: 8px; width: 180px; }
      .el-select-dropdown { margin: 0; padding: 0; list-style: none; }
      .el-select-dropdown__item { padding: 8px; }
      .expandContent { margin: 12px; border: 1px solid #aaa; padding: 8px; }
      .editor-content { min-height: 24px; border: 1px solid #ccc; margin: 4px; }
    </style>
    <input placeholder="请输入课文名称（中文）">
    <input placeholder="请输入课文名称（英文）">
    <section id="classification">""" + "".join(controls) + """</section>
    <button id="next">下一步:录入课文内容</button>
    <div id="step-label" hidden>第二步：录入课文句子内容</div>
    <section id="content">""" + "".join(cards) + """</section>
    <button id="save">保存课文句子</button>
    <div id="saved" hidden>保存成功</div>
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
      document.querySelector('#next').addEventListener('click', () => {
        document.querySelector('#step-label').hidden = false;
      });
      document.querySelector('#save').addEventListener('click', () => {
        setTimeout(() => { document.querySelector('#saved').hidden = false; }, 20);
      });
      document.querySelectorAll('input[type=file]').forEach((input) => {
        input.addEventListener('change', () => {
          const name = input.files && input.files[0] ? input.files[0].name.replace(/\\.mp3$/i, '') : '';
          input.parentElement.querySelector('.audio-name').textContent = name;
        });
      });
      window.resetDiagnostic = () => {
        document.querySelectorAll('input:not([type=file])').forEach((input) => { input.value = ''; });
        wrappers.forEach((wrapper) => { wrapper.textContent = '请选择'; });
        dropdowns.forEach((dropdown) => { dropdown.hidden = true; });
        document.querySelectorAll('.editor-content').forEach((editor, index) => {
          editor.textContent = index % 2 ? '旧译文' : '旧原文';
        });
        document.querySelectorAll('input[type=file]').forEach((input) => { input.value = ''; });
        document.querySelectorAll('.audio-name').forEach((node) => { node.textContent = ''; });
        document.querySelector('#step-label').hidden = true;
        document.querySelector('#saved').hidden = true;
      };
    </script>
    """


def _form_html() -> str:
    controls = []
    dropdowns = []
    for index, (title, value) in enumerate(FORM_SINGLE_CHOICES):
        controls.append(
            f"""
            <div class="selectContent" data-field="{index}">
              <span>{title}</span>
              <div class="el-select"><div class="el-select__wrapper">请选择</div></div>
            </div>
            """
        )
        dropdowns.append(
            f"""
            <div class="el-select-dropdown" data-field="{index}" hidden>
              <div class="el-select-dropdown__item" role="option" aria-selected="false">{value}</div>
            </div>
            """
        )
    multi_index = len(FORM_SINGLE_CHOICES)
    multi_title, multi_values = FORM_MULTI_CHOICES
    controls.append(
        f"""
        <div class="selectContent" data-field="{multi_index}" data-multi="true">
          <span>{multi_title}</span>
          <div class="el-select"><div class="el-select__wrapper">请选择</div></div>
        </div>
        """
    )
    dropdowns.append(
        f"""
        <div class="el-select-dropdown" data-field="{multi_index}" data-multi="true" hidden>
          {''.join(f'<div class="el-select-dropdown__item" role="option" aria-selected="false">{value}</div>' for value in multi_values)}
        </div>
        """
    )
    return f"""
    <!doctype html>
    <style>
      .selectContent {{ margin: 8px; }}
      .el-select__wrapper {{ border: 1px solid #888; min-height: 24px; padding: 8px; width: 220px; }}
      .el-select-dropdown__item {{ padding: 8px; }}
    </style>
    <section id="base-form">{''.join(controls)}</section>
    <section id="dropdowns">{''.join(dropdowns)}</section>
    <script>
      const formFields = [...document.querySelectorAll('.selectContent')];
      const formDropdowns = [...document.querySelectorAll('.el-select-dropdown')];
      const hideDropdowns = () => formDropdowns.forEach((dropdown) => {{ dropdown.hidden = true; }});
      const renderField = (index) => {{
        const wrapper = formFields[index].querySelector('.el-select__wrapper');
        const selected = [...formDropdowns[index].querySelectorAll('[aria-selected=true]')];
        wrapper.innerHTML = selected.length
          ? selected.map((option) => `<span class="el-select__selected-item"><span class="el-select__tags-text">${{option.textContent}}</span></span>`).join('')
          : '请选择';
      }};
      formFields.forEach((field, index) => {{
        const wrapper = field.querySelector('.el-select__wrapper');
        const dropdown = formDropdowns[index];
        wrapper.addEventListener('click', () => {{ hideDropdowns(); dropdown.hidden = false; }});
        dropdown.querySelectorAll('[role=option]').forEach((option) => {{
          option.addEventListener('click', () => {{
            const multi = dropdown.dataset.multi === 'true';
            if (!multi) {{
              dropdown.querySelectorAll('[role=option]').forEach((node) => node.setAttribute('aria-selected', 'false'));
            }}
            option.setAttribute('aria-selected', option.getAttribute('aria-selected') === 'true' && multi ? 'false' : 'true');
            renderField(index);
            if (!multi) dropdown.hidden = true;
          }});
        }});
      }});
      document.addEventListener('keydown', (event) => {{ if (event.key === 'Escape') hideDropdowns(); }});
      window.resetFormDiagnostic = () => {{
        hideDropdowns();
        formDropdowns.forEach((dropdown, index) => {{
          dropdown.querySelectorAll('[role=option]').forEach((option) => option.setAttribute('aria-selected', 'false'));
          renderField(index);
        }});
      }};
    </script>
    """


def _type_input_before_optimization(locator, value: str) -> None:
    locator.click()
    locator.press("ControlOrMeta+A")
    locator.type(str(value))
    locator.press("Tab")


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
    selected_deadline = time.monotonic() + 3
    while time.monotonic() < selected_deadline:
        shown = "".join(str(selector.inner_text() or "").split())
        if expected in shown.casefold():
            return
        page.wait_for_timeout(50)
    raise RuntimeError(f"readback mismatch for {value!r}")


def _replace_editor_before_optimization(editor, value: str) -> None:
    editor.click()
    editor.press("ControlOrMeta+A")
    editor.press("Backspace")
    if value:
        editor.type(value)
    editor.press("Tab")


def _fill_content_before_optimization(page, record: dict) -> None:
    cards = _ensure_card_count(page, len(record["items"]))
    for index, item in enumerate(record["items"]):
        card = cards.nth(index)
        card.scroll_into_view_if_needed()
        editors = card.locator(
            '.rich-text-editor .editor-content[contenteditable="true"]'
        )
        _replace_editor_before_optimization(editors.nth(0), str(item["original"]))
        _replace_editor_before_optimization(editors.nth(1), str(item["translation"]))
        file_inputs = card.locator('input[type="file"]')
        file_inputs.first.set_input_files(str(item["audio_path"]))
        stem = Path(str(item["audio_path"])).stem
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if stem in str(card.inner_text() or ""):
                break
            page.wait_for_timeout(250)
        else:
            raise RuntimeError(f"audio readback failed for {stem}")


def _fill_classification_with_stages(
    page,
    record: dict,
    optimized: bool,
) -> dict[str, float]:
    names_started = time.perf_counter()
    chinese_name = page.get_by_placeholder("请输入课文名称（中文）", exact=True)
    english_name = page.get_by_placeholder("请输入课文名称（英文）", exact=True)
    if optimized:
        chinese_name.fill(str(record["name_zh"]))
        english_name.fill(str(record["name_en"]))
    else:
        _type_input_before_optimization(chinese_name, record["name_zh"])
        _type_input_before_optimization(english_name, record["name_en"])
    names_ms = (time.perf_counter() - names_started) * 1000

    dropdown_started = time.perf_counter()
    values = [
        record["form"],
        record["version"],
        record["stage"],
        record["grade"],
        record["volume"],
        record["unit"],
        record["lesson"],
    ]
    for index, value in enumerate(values):
        if optimized:
            _select_option(page, index, str(value))
        else:
            _select_option_before_optimization(page, index, str(value))
    dropdown_ms = (time.perf_counter() - dropdown_started) * 1000
    return {"names_ms": names_ms, "dropdowns_ms": dropdown_ms}


def _fill_content_with_stages(
    page,
    record: dict,
    optimized: bool,
) -> dict[str, float]:
    cards = _ensure_card_count(page, len(record["items"]))
    editors_ms = 0.0
    uploads_ms = 0.0
    for index, item in enumerate(record["items"]):
        card = cards.nth(index)
        if not optimized:
            # The optimized production path lets select_text() scroll the
            # editor only when needed; retain the old explicit scroll in the
            # comparison path so the A/B measures the shipped change.
            card.scroll_into_view_if_needed()
        editors = card.locator(
            '.rich-text-editor .editor-content[contenteditable="true"]'
        )
        editor_started = time.perf_counter()
        if optimized:
            _replace_editor(page, editors.nth(0), str(item["original"]), "原文")
            _replace_editor(page, editors.nth(1), str(item["translation"]), "译文")
        else:
            _replace_editor_before_optimization(editors.nth(0), str(item["original"]))
            _replace_editor_before_optimization(editors.nth(1), str(item["translation"]))
        editors_ms += (time.perf_counter() - editor_started) * 1000

        upload_started = time.perf_counter()
        file_inputs = card.locator('input[type="file"]')
        file_inputs.first.set_input_files(str(item["audio_path"]))
        stem = Path(str(item["audio_path"])).stem
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if stem in str(card.inner_text() or ""):
                break
            page.wait_for_timeout(250)
        else:
            raise RuntimeError(f"audio readback failed for {stem}")
        uploads_ms += (time.perf_counter() - upload_started) * 1000
    return {"editors_ms": editors_ms, "uploads_ms": uploads_ms}


def _fill_classification_before_optimization(page, record: dict) -> None:
    _type_input_before_optimization(
        page.get_by_placeholder("请输入课文名称（中文）", exact=True), record["name_zh"]
    )
    _type_input_before_optimization(
        page.get_by_placeholder("请输入课文名称（英文）", exact=True), record["name_en"]
    )
    values = [
        record["form"],
        record["version"],
        record["stage"],
        record["grade"],
        record["volume"],
        record["unit"],
        record["lesson"],
    ]
    for index, value in enumerate(values):
        _select_option_before_optimization(page, index, str(value))


def _run_record(page, record: dict, optimized: bool) -> dict[str, float]:
    started = time.perf_counter()
    classification = _fill_classification_with_stages(page, record, optimized)

    page.get_by_text("下一步:录入课文内容", exact=True).click()
    page.get_by_text("第二步：录入课文句子内容", exact=True).wait_for(state="visible")
    content = _fill_content_with_stages(page, record, optimized)

    save_started = time.perf_counter()
    page.get_by_text("保存课文句子", exact=True).click()
    page.get_by_text("保存成功", exact=True).wait_for(state="visible", timeout=20_000)
    save_ms = (time.perf_counter() - save_started) * 1000
    return {
        "names_ms": classification["names_ms"],
        "dropdowns_ms": classification["dropdowns_ms"],
        "editors_ms": content["editors_ms"],
        "uploads_ms": content["uploads_ms"],
        "content_ms": content["editors_ms"] + content["uploads_ms"],
        "save_ms": save_ms,
        "total_ms": (time.perf_counter() - started) * 1000,
    }


def _run_suite(page, records: list[dict], optimized: bool, rounds: int) -> dict[str, float]:
    totals = {
        "names_ms": 0.0,
        "dropdowns_ms": 0.0,
        "editors_ms": 0.0,
        "uploads_ms": 0.0,
        "content_ms": 0.0,
        "save_ms": 0.0,
        "total_ms": 0.0,
    }
    count = 0
    for _ in range(rounds):
        for record in records:
            page.evaluate("window.resetDiagnostic()")
            result = _run_record(page, record, optimized)
            for key, value in result.items():
                totals[key] += value
            count += 1
    return {key: round(value / count, 2) for key, value in totals.items()}


def _form_automation(page) -> PlatformInputPageAutomation:
    automation = object.__new__(PlatformInputPageAutomation)
    automation.page = page
    automation.action_timeout_ms = 10_000
    automation._control_check = None
    return automation


def _select_form_choice_before_optimization(
    automation: PlatformInputPageAutomation,
    title: str,
    name: str,
    *,
    multi: bool = False,
) -> None:
    component = automation._open_select(title, force_real=True)
    option = automation._wait_for_dropdown_option(title, name)
    before = automation._selected_multi_count(component) if multi else 0
    option.click(timeout=automation.action_timeout_ms)
    if multi:
        automation._wait_until(
            lambda: automation._selected_multi_count(component) > before,
            f"baseline multi select failed: {title}={name}",
            timeout_seconds=10,
            interval_ms=50,
        )
    else:
        automation._wait_until(
            lambda: name in component.inner_text(),
            f"baseline select failed: {title}={name}",
            timeout_seconds=10,
            interval_ms=50,
        )


def _run_form_once(page, optimized: bool) -> dict[str, float]:
    automation = _form_automation(page)
    started = time.perf_counter()
    single_started = time.perf_counter()
    for title, name in FORM_SINGLE_CHOICES:
        if optimized:
            automation._select_one(title, {"name": name})
        else:
            _select_form_choice_before_optimization(automation, title, name)
    single_ms = (time.perf_counter() - single_started) * 1000

    multi_started = time.perf_counter()
    title, names = FORM_MULTI_CHOICES
    if optimized:
        automation._select_many(title, [{"name": name} for name in names])
    else:
        for name in names:
            _select_form_choice_before_optimization(
                automation,
                title,
                name,
                multi=True,
            )
        page.keyboard.press("Escape")
    multi_ms = (time.perf_counter() - multi_started) * 1000
    return {
        "single_ms": single_ms,
        "multi_ms": multi_ms,
        "total_ms": (time.perf_counter() - started) * 1000,
    }


def _run_form_suite(page, optimized: bool, rounds: int) -> dict[str, float]:
    totals = {"single_ms": 0.0, "multi_ms": 0.0, "total_ms": 0.0}
    for _ in range(rounds):
        page.evaluate("window.resetFormDiagnostic()")
        result = _run_form_once(page, optimized)
        for key, value in result.items():
            totals[key] += value
    return {key: round(value / rounds, 2) for key, value in totals.items()}


def main() -> None:
    output_path = Path(os.environ.get("DIAGNOSTIC_OUTPUT", "windows-speed-results.json"))
    trace_path = Path(os.environ.get("DIAGNOSTIC_TRACE", "windows-speed-trace.zip"))
    rounds = int(os.environ.get("DIAGNOSTIC_ROUNDS", "5"))
    with tempfile.TemporaryDirectory(prefix="wordtts-composite-") as temp_dir:
        audio_path = Path(temp_dir) / "diagnostic-audio.mp3"
        audio_path.write_bytes(b"ID3\x04\x00diagnostic")
        records = [
            _record("九上课文诊断一", "A lesson for speed test", str(audio_path)),
            _record("九上课文诊断二", "Another lesson for speed test", str(audio_path)),
        ]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            old_page = context.new_page()
            new_page = context.new_page()
            old_form_page = context.new_page()
            new_form_page = context.new_page()
            old_page.set_content(_html())
            new_page.set_content(_html())
            old_form_page.set_content(_form_html())
            new_form_page.set_content(_form_html())
            _run_suite(old_page, records, False, 1)
            _run_suite(new_page, records, True, 1)
            _run_form_suite(old_form_page, False, 1)
            _run_form_suite(new_form_page, True, 1)
            old_result = _run_suite(old_page, records, False, rounds)
            old_form_result = _run_form_suite(old_form_page, False, rounds)
            context.tracing.start(screenshots=False, snapshots=True, sources=False)
            new_result = _run_suite(new_page, records, True, rounds)
            new_form_result = _run_form_suite(new_form_page, True, rounds)
            context.tracing.stop(path=str(trace_path))
            browser.close()

    result = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version,
        "rounds": rounds,
        "records_per_round": 2,
        "items_per_record": len(ITEMS),
        "before": old_result,
        "optimized": new_result,
        "speedup_total": round(old_result["total_ms"] / new_result["total_ms"], 2),
        "stage_speedups": {
            key: round(old_result[key] / new_result[key], 2)
            for key in old_result
            if new_result[key]
        },
        "general_form": {
            "single_choices": len(FORM_SINGLE_CHOICES),
            "multi_choices": len(FORM_MULTI_CHOICES[1]),
            "before": old_form_result,
            "optimized": new_form_result,
            "speedup_total": round(
                old_form_result["total_ms"] / new_form_result["total_ms"],
                2,
            ),
        },
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
