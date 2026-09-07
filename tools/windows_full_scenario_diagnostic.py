"""Run a provider-free, real Chromium regression matrix on Windows.

This is deliberately a browser scenario, not another collection of locator
unit tests.  The fixtures implement the delayed Vue/React DOM transitions
that have caused the Windows-only regressions, then call the shipped page
adapters against that DOM.  No account, network, or external provider is
needed, so the release workflow can run it before packaging.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_entry.adapter.automation import PlatformInputPageAutomation  # noqa: E402
from platform_entry.adapter.models import PlatformInputSpec  # noqa: E402
from xunfei.session import XunFeiSession  # noqa: E402


class _Observer:
    """Small observer surface used by save/readback methods in the fixture."""

    def __init__(self) -> None:
        self.write_responses_observed: list[dict[str, Any]] = []
        self.read_only_requests: list[dict[str, Any]] = []


def _automation(page: Any, spec: PlatformInputSpec) -> PlatformInputPageAutomation:
    owner = object.__new__(PlatformInputPageAutomation)
    owner.page = page
    owner.spec = spec
    owner.observer = _Observer()
    owner.action_timeout_ms = 5_000
    owner._control_check = None
    owner.existing_paper_id = None
    return owner


def _asset_files(folder: Path) -> dict[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    # The browser only needs a file for set_input_files; the download fixture
    # below emits a real MP3 signature and is checked separately.
    names = (
        "choice-a.mp3",
        "choice-b.mp3",
        "response.mp3",
        "imitation.mp3",
        "record.mp3",
        "legacy-a.mp3",
        "legacy-b.mp3",
        "record-table.png",
    )
    result: dict[str, str] = {}
    for name in names:
        path = folder / name
        path.write_bytes(b"ID3\x04\x00synthetic-browser-scenario")
        result[name] = str(path)
    return result


def _editor(label: str, placeholder: str = "题干", value: str = "旧内容") -> str:
    return (
        '<div class="field editor-field">'
        f'<label>{label}</label>'
        '<div class="rich-text-editor">'
        f'<div class="editor-content" contenteditable="true" '
        f'data-placeholder="{placeholder}">{value}</div>'
        "</div></div>"
    )


def _audio(label: str, index: str) -> str:
    return (
        f'<div class="examAudioContent" data-audio="{index}">'
        f'<label>{label}</label>'
        '<input type="file" accept=".mp3,.wav">'
        '<span class="showNameContent"></span>'
        "</div>"
    )


def _number(label: str, index: str, value: str = "") -> str:
    return (
        '<div class="field number-field">'
        f'<label>{label}</label><input id="{index}" value="{value}">'
        "</div>"
    )


def _image(index: str) -> str:
    return (
        f'<div class="image-upload" data-image="{index}">'
        "<label>点击上传 支持 JPG/PNG</label>"
        '<input type="file" accept="image/png,image/jpeg">'
        "</div>"
    )


def _choice_card(index: int, option_count: int = 2) -> str:
    options = "".join(
        '<div class="option-row">'
        + _editor("选项", "请输入", f"旧选项 {index}-{item}")
        + '<button class="delete-option">删除</button></div>'
        for item in range(option_count)
    )
    answers = "".join(
        f'<button type="button" class="answerNormal">{letter}</button>'
        for letter in ("A", "B", "C", "D")
    )
    return (
        f'<article class="optionBack question-card" data-card="choice-{index}">'
        f'<div class="question-heading">小题{index + 1}(选择题)</div>'
        + _editor("题干", "题干", "旧题干")
        + _audio("题干音频", f"choice-prompt-{index}")
        + _audio("答案解析音频", f"choice-analysis-{index}")
        + options
        + '<button type="button" class="add-option">添加选项</button>'
        + _number("分数", f"choice-score-{index}")
        + _number("答题时长(秒)", f"choice-time-{index}")
        + f'<div class="answer-controls">{answers}</div>'
        + "</article>"
    )


def _recording_card(kind: str, index: int, answers: int = 1) -> str:
    rows = "".join(
        f'<div class="answer-row"><input class="answer-input" '
        f'value="旧答案 {index}-{row}"><button class="delete-answer">删除</button></div>'
        for row in range(max(answers, 1))
    )
    return (
        f'<article class="optionBack question-card" data-card="{kind}-{index}">'
        f'<div class="question-heading">小题{index + 1}({kind})</div>'
        + _editor("题干", "题干", "旧题干")
        + _audio("题干音频", f"prompt-{kind}-{index}")
        + _audio("答案解析音频", f"analysis-{kind}-{index}")
        + _number("分数", f"record-score-{kind}-{index}")
        + _number("答题时长(秒)", f"record-time-{kind}-{index}")
        + '<div class="answers">'
        + rows
        + '<button type="button" class="add-answer">添加答案</button></div>'
        + "</article>"
    )


def _section(
    section_id: str,
    kind: str,
    *,
    card_kind: str,
    card_count: int,
    listening_count: int,
    choice: bool = False,
    source_audio_prefix: str = "source",
    with_image: bool = False,
    with_instruction: bool = False,
    initial_option_counts: tuple[int, ...] = (),
) -> str:
    materials = []
    for index in range(listening_count):
        materials.append(
            '<div class="material">'
            + _editor("听力原文", "听力原文", "旧听力原文")
            + _audio("原文音频", f"{source_audio_prefix}-{index}")
            + _number("原文播放次数(次)", f"times-{section_id}-{index}")
            + _number("原文播放间隔(秒)", f"gap-{section_id}-{index}")
            + (_image(f"{section_id}-{index}") if with_image and index == 0 else "")
            + "</div>"
        )
    instruction = ""
    if with_instruction:
        instruction = _editor("题目指导文字", "题目指导文字", "旧指导文字")
    cards = []
    for index in range(card_count):
        if choice:
            option_count = (
                initial_option_counts[index]
                if index < len(initial_option_counts)
                else 2
            )
            cards.append(_choice_card(index, option_count))
        else:
            cards.append(_recording_card(card_kind, index, answers=2 if index == 0 else 1))
    return (
        f'<section id="{section_id}" class="content-section" style="display:none">'
        + "".join(materials)
        + instruction
        + "".join(cards)
        + "</section>"
    )


def _content_fixture(sections: list[dict[str, Any]]) -> str:
    nav = "".join(
        f'<button class="outline" data-target="{item["id"]}">'
        f'{item["nav"]}</button>'
        for item in sections
    )
    body = "".join(item["html"] for item in sections)
    return f"""
    <!doctype html><style>
      body {{ font: 14px sans-serif; margin: 20px; }}
      .outline {{ margin: 3px; padding: 6px; }}
      .content-section {{ border: 1px solid #aaa; padding: 12px; margin-top: 8px; }}
      .question-card {{ border: 1px solid #888; padding: 10px; margin: 10px; }}
      .field {{ margin: 5px; }}
      label {{ display: block; }}
      .editor-content {{ min-height: 24px; border: 1px solid #bbb; padding: 4px; }}
      .examAudioContent, .image-upload {{ border: 1px dashed #aaa; padding: 5px; margin: 5px; }}
      .examAudioContent input, .image-upload input {{ display: block; }}
      .option-row, .answer-row {{ display: flex; gap: 4px; align-items: center; }}
      .answerSelected {{ background: #1683ff; color: white; }}
    </style>
    <nav id="outline">{nav}</nav><main>{body}</main>
    <button id="save-paper">保存试卷</button><span id="saved-paper" style="display:none">保存成功</span>
    <script>
      var show = (id) => {{
        document.querySelectorAll('.content-section').forEach((node) => node.style.display = 'none');
        const target = document.getElementById(id); if (target) target.style.display = 'block';
      }};
      document.querySelectorAll('.outline').forEach((button) => button.addEventListener('click', () => show(button.dataset.target)));
      var addEditor = (card, placeholder='请输入') => {{
        const row = document.createElement('div'); row.className = 'option-row';
        row.innerHTML = '<div class="field editor-field"><label>选项</label><div class="rich-text-editor"><div class="editor-content" contenteditable="true" data-placeholder="' + placeholder + '"></div></div></div><button class="delete-option">删除</button>';
        card.querySelector('.add-option').before(row);
      }};
      document.addEventListener('click', (event) => {{
        const addOption = event.target.closest('.add-option');
        if (addOption) {{ addEditor(addOption.closest('.question-card')); return; }}
        const delOption = event.target.closest('.delete-option');
        if (delOption) {{ delOption.closest('.option-row').remove(); return; }}
        const addAnswer = event.target.closest('.add-answer');
        if (addAnswer) {{
          const row = document.createElement('div'); row.className = 'answer-row';
          row.innerHTML = '<input class="answer-input" value=""><button class="delete-answer">删除</button>';
          addAnswer.before(row); return;
        }}
        const delAnswer = event.target.closest('.delete-answer');
        if (delAnswer) {{ delAnswer.closest('.answer-row').remove(); return; }}
        const answer = event.target.closest('.answerNormal, .answerSelected');
        if (answer) {{
          answer.closest('.answer-controls').querySelectorAll('button').forEach((node) => node.className = 'answerNormal');
          answer.className = 'answerSelected';
        }}
      }});
      document.addEventListener('change', (event) => {{
        const input = event.target;
        if (input.type === 'file' && input.files && input.files[0]) {{
          const region = input.closest('.examAudioContent');
          if (region) region.querySelector('.showNameContent').textContent = input.files[0].name;
          const image = input.closest('.image-upload');
          if (image) {{
            const border = document.createElement('div'); border.className = 'contentImgBorder';
            border.innerHTML = '<img src="data:image/png;base64,iVBORw0KGgo=">'; image.append(border);
          }}
        }}
      }});
      document.querySelector('#save-paper').addEventListener('click', () => document.querySelector('#saved-paper').style.display = 'inline');
    </script>
    """


def _form_fixture() -> str:
    fields = (
        ("试卷分类", ("听说考试",)),
        ("省份", ("广东省",)),
        ("城市", ("广州市",)),
        ("区/县", ("天河区", "越秀区")),
        ("学段", ("初中",)),
        ("年级", ("九年级",)),
        ("试卷类型", ("中考模拟",)),
    )
    controls = []
    dropdowns = []
    for index, (title, values) in enumerate(fields):
        multi = title == "区/县"
        controls.append(
            f'<div class="selectContent" data-field="{index}" data-multi="{str(multi).lower()}">'
            f'<span>{title}</span><div class="el-select"><div class="el-select__wrapper">请选择</div></div></div>'
        )
        dropdowns.append(
            f'<div class="el-select-dropdown" data-field="{index}" hidden>'
            + "".join(
                f'<div class="el-select-dropdown__item" role="option" aria-selected="false">{value}</div>'
                for value in values
            )
            + "</div>"
        )
    return f"""
    <!doctype html><style>
      .selectContent {{ margin: 6px; }} .el-select__wrapper {{ border: 1px solid #888; padding: 7px; width: 240px; }}
      .el-select-dropdown {{ border: 1px solid #aaa; padding: 4px; }}
      .el-select-dropdown__item {{ padding: 4px; }} .el-select__selected-item {{ display: inline-block; margin: 2px; }}
    </style>
    <input placeholder="请输入完整试卷名称，例如：2026年佛山市南海区初二上学期英语听说期中考试">
    <div class="inputContent"><label>年份</label><input id="year"></div>
    <div class="inputContent"><label>大约答题时长（分钟）</label><input id="duration"></div>
    <section id="base">{''.join(controls)}</section><section id="dropdowns">{''.join(dropdowns)}</section>
    <input placeholder="搜索题型模板...">
    <article class="cardContent"><div class="nameFont">人教版听说测试题模板</div><label><input type="radio" name="template">选择</label></article>
    <button id="next" disabled>下一步：录入试题内容</button><div id="next-state"></div>
    <div id="content-editor" style="display:none"><div class="rich-text-editor"><div class="editor-content" contenteditable="true" data-placeholder="题干">挂载内容</div></div></div>
    <script>
      const fields = [...document.querySelectorAll('.selectContent')];
      const lists = [...document.querySelectorAll('.el-select-dropdown')];
      const render = (i) => {{ const w = fields[i].querySelector('.el-select__wrapper'); const selected = [...lists[i].querySelectorAll('[aria-selected=true]')]; w.innerHTML = selected.map((n) => '<span class="el-select__selected-item"><span class="el-select__tags-text">'+n.textContent+'</span></span>').join('') || '请选择'; }};
      fields.forEach((field, i) => {{ const w = field.querySelector('.el-select__wrapper'); w.addEventListener('click', () => {{ lists.forEach((x) => x.hidden = true); lists[i].hidden = false; }}); lists[i].querySelectorAll('[role=option]').forEach((option) => option.addEventListener('click', () => {{ if (field.dataset.multi !== 'true') lists[i].querySelectorAll('[role=option]').forEach((x) => x.setAttribute('aria-selected','false')); option.setAttribute('aria-selected', field.dataset.multi === 'true' && option.getAttribute('aria-selected') === 'true' ? 'false' : 'true'); render(i); if (field.dataset.multi !== 'true') lists[i].hidden = true; }})); }});
      const card = document.querySelector('.cardContent'); card.addEventListener('click', () => {{ card.classList.add('is-selected'); card.querySelector('input').checked = true; document.querySelector('#next').disabled = false; }});
      document.querySelector('#next').addEventListener('click', () => {{ document.querySelector('#next-state').textContent = '第二步：录入试卷内容'; document.querySelector('#content-editor').style.display = 'block'; }});
    </script>
    """


def _make_group_data(paths: dict[str, str]) -> dict[str, dict[str, Any]]:
    option = lambda a, b, c: [
        {"option_id": "A", "text": a},
        {"option_id": "B", "text": b},
        {"option_id": "C", "text": c},
    ]
    return {
        "选择": {
            "type": "听后选择",
            "materials": [{
                "listening_text": "The first listening material.",
                "audio_path": paths["choice-a.mp3"], "times": 2, "gap": 4,
                "questions": [{
                    "prompt": "Where did the student go?", "options": option("A park.", "A shop.", "A school."),
                    "answer": "B", "score": 1, "answer_time": 5,
                }],
            }],
        },
        "应答": {
            "type": "听后应答",
            "questions": [{
                "listening_text": "Where did the student go?", "audio_path": paths["response.mp3"],
                "prompt": "Please answer the question.", "score": 1, "answer_time": 6,
                "reference_answers": ["The student went to a park."],
            }],
        },
        "模仿": {
            "type": "模仿朗读",
            "questions": [{
                "listening_text": "Read this sentence aloud.", "audio_path": paths["imitation.mp3"],
                "score": 7, "reference_answers": ["Read this sentence aloud."],
            }],
        },
        "记录": {
            "type": "听后记录并转述信息",
            "recording": {
                "listening_text": "Emma keeps her room tidy.", "audio_path": paths["record.mp3"],
                "image_path": paths["record-table.png"], "times": 2, "gap": 3,
                "instruction_text": "Listen and complete the table.", "instruction_occurrence": 0,
                "questions": [{"score": 1, "answers": ["tidy"]}],
            },
            "retelling": {"prompt": "This is Emma's room.", "score": 5, "answer_time": 60,
                          "reference_answers": ["This is Emma's room." ]},
        },
        "获取": {
            "type": "信息获取",
            "materials": [{"section": "听选信息", "listening_text": "Listen for a name.",
                           "audio_path": paths["legacy-a.mp3"], "questions": [{
                               "prompt": "What is the name?", "score": 1,
                               "reference_answers": ["Amy"],
                           }]},
                          {"section": "回答问题", "listening_text": "Listen for a place.",
                           "audio_path": paths["legacy-b.mp3"], "questions": [{
                               "prompt": "Where is it?", "score": 1,
                               "reference_answers": ["At school"],
                           }]}],
        },
        "转述": {
            "type": "信息转述及询问",
            "recording": {"listening_text": "A student describes a hobby.",
                           "audio_path": paths["legacy-a.mp3"],
                           "instruction_text": "Listen and retell.", "instruction_occurrence": 0,
                           "asking_instruction_text": "Ask one question.", "asking_instruction_occurrence": 0},
            "retelling": {"prompt": "The student likes music.", "score": 5,
                          "reference_answers": ["The student likes music."]},
            "asking": [{"prompt": "What music does the student like?", "score": 2,
                         "reference_answers": ["Pop music."]}],
        },
    }


def _run_form_and_template(page: Any) -> dict[str, Any]:
    paper = {
        "title": "Windows full matrix",
        "province": {"name": "广东省"}, "city": {"name": "广州市"},
        "districts": [{"name": "天河区"}, {"name": "越秀区"}],
        "stage": {"name": "初中"}, "grade": {"name": "九年级"},
        "paper_type": {"name": "中考模拟"}, "year": 2026, "duration": 60,
    }
    spec = PlatformInputSpec(
        paper=paper, items=(), paper_category="听说考试",
        template_name="人教版听说测试题模板",
    )
    owner = _automation(page, spec)
    owner.fill_base_form()
    owner.select_template()
    owner.next_to_content()
    assert page.locator("#next-state").inner_text() == "第二步：录入试卷内容"
    assert page.locator('input[placeholder^="请输入完整试卷名称"]').input_value() == paper["title"]
    assert owner._selected_multi_count(owner._field_component("区/县")) == 2
    return {"base_form": True, "template": True, "multi_select_count": 2}


def _activate_by_button(owner: PlatformInputPageAutomation, sections: list[str]):
    """Use the same visible click/readback contract for legacy sub-panels."""
    queue = iter(sections)

    def activate(_group: str, question_kind: str, expected_cards: int, expected_editors: int, **_kwargs):
        section_id = next(queue)
        owner.page.locator(f'[data-target="{section_id}"]').click()
        owner._wait_until(
            lambda: len(owner._question_cards(question_kind)) == expected_cards,
            f"synthetic section {section_id} cards not mounted",
            timeout_seconds=10,
            interval_ms=20,
        )
        cards = owner._question_cards(question_kind)
        editors = owner._labeled_text_editors("听力原文")
        if len(editors) < expected_editors:
            raise AssertionError(f"synthetic section {section_id} listening editor mismatch")
        return cards, editors[:expected_editors]

    return activate


def _run_content_matrix(page: Any, paths: dict[str, str]) -> dict[str, Any]:
    groups = _make_group_data(paths)
    reports: dict[str, Any] = {}

    # The first three and the record section use the production outline
    # selector.  This covers the real numbered-heading click path as well as
    # the card/edit/upload operations.
    simple = [
        ("选择", "choice", "第1题(选择题)", "选择题", 1, 1, True),
        ("应答", "response", "第1题(录音题)", "录音题", 1, 1, False),
        ("模仿", "imitation", "第1题(录音题)", "录音题", 1, 1, False),
        ("记录", "record", "第1题(填空题)", "填空题", 1, 1, False),
    ]
    for key, section_id, nav, card_kind, card_count, editor_count, choice in simple:
        extra = ""
        if key == "记录":
            extra = _section("retelling", "retelling", card_kind="录音题", card_count=1, listening_count=1)
        html = _section(
            section_id, key, card_kind=card_kind, card_count=card_count,
            listening_count=editor_count, choice=choice,
            with_image=key == "记录", with_instruction=key == "记录",
            initial_option_counts=(4,) if choice else (),
        ) + extra
        sections = [{"id": section_id, "nav": nav, "html": html}]
        if key == "记录":
            sections[0]["html"] += ""  # keep the first section's DOM stable
            sections.append({"id": "retelling", "nav": "第2题(录音题)", "html": ""})
            # The retelling section must be a separate visible panel.
            sections[0]["html"] = _section(
                section_id, key, card_kind=card_kind, card_count=card_count,
                listening_count=editor_count, with_image=True, with_instruction=True,
            )
            sections[1]["html"] = _section("retelling", "retelling", card_kind="录音题", card_count=1, listening_count=1)
        page.set_content(_content_fixture(sections))
        spec = PlatformInputSpec(
            paper={}, items=(), paper_category="题型专项", template_name="synthetic",
            groups=(groups[key],),
        )
        owner = _automation(page, spec)
        if key in {"应答", "模仿"}:
            # Keep the production numbered-outline path covered by the choice
            # case above.  These two provider templates deliberately use the
            # same visual recording heading; an explicit data-target click
            # makes their independent lazy panels deterministic in the
            # provider-free fixture while retaining real DOM/card discovery.
            owner._activate_group_section = _activate_by_button(owner, [section_id])
        if key == "记录":
            # Production _activate_retelling_outline selects the last numbered
            # recording heading.  It is intentionally left intact.
            pass
        method = {
            "选择": owner._fill_selection_groups,
            "应答": owner._fill_response_groups,
            "模仿": owner._fill_imitation_groups,
            "记录": owner._fill_record_retelling_groups,
        }[key]
        method({groups[key]["type"]: (groups[key],)})
        reports[key] = {"cards": card_count, "browser_actions": True}

    # The two legacy layouts have multiple lazy panels.  The helper still
    # clicks real outline controls and uses production card discovery; only
    # the old template's unusual numbering is supplied by the fixture.
    for key, section_ids, specs in (
        ("获取", ["acq-one", "acq-two"], [
            ("第1题(录音题)", "acq-one"), ("第7题(录音题)", "acq-two")
        ]),
        ("转述", ["retell-one", "ask-one"], [
            ("第1题(录音题)", "retell-one"), ("第2题(录音题)", "ask-one")
        ]),
    ):
        group = groups[key]
        if key == "获取":
            html = _section("acq-one", key, card_kind="录音题", card_count=1, listening_count=1)
            html += _section("acq-two", key, card_kind="录音题", card_count=1, listening_count=1)
        else:
            html = _section("retell-one", key, card_kind="录音题", card_count=1, listening_count=1, with_instruction=True)
            html += _section("ask-one", key, card_kind="录音题", card_count=1, listening_count=0)
        sections = [{"id": section_ids[0], "nav": specs[0][0], "html": ""}, {"id": section_ids[1], "nav": specs[1][0], "html": ""}]
        sections[0]["html"] = _section(section_ids[0], key, card_kind="录音题", card_count=1, listening_count=1, with_instruction=key == "转述")
        sections[1]["html"] = _section(
            section_ids[1], key, card_kind="录音题", card_count=1,
            listening_count=1 if key == "获取" else 0,
            with_instruction=key == "转述",
        )
        page.set_content(_content_fixture(sections))
        spec = PlatformInputSpec(
            paper={}, items=(), paper_category="题型专项", template_name="synthetic",
            groups=(group,),
            bundle_rule=(
                "platform_input-info-retelling-special-v1"
                if key == "转述" else None
            ),
        )
        owner = _automation(page, spec)
        owner._activate_group_section = _activate_by_button(owner, section_ids)
        if key == "获取":
            result = owner._fill_info_acquisition_groups({group["type"]: (group,)})
        else:
            result = owner._fill_info_retelling_groups({group["type"]: (group,)})
        assert result["录音题"] == (2 if key == "获取" else 2)
        reports[key] = {"cards": 2, "browser_actions": True}

    owner.save_content()
    reports["save_content"] = True
    return reports


def _generation_fixture() -> str:
    return """
    <!doctype html><style>
      .ssml-editor { border:1px solid #888; padding:8px; min-height:80px; }
      .ssml-editor p { margin:4px; } .ant-modal { border:1px solid #444; padding:12px; background:white; }
      .ant-modal[hidden] { display:none; } button { margin:3px; }
    </style>
    <div class="ssml-editor" contenteditable="true"><p>旧文本</p></div>
    <input class="voice-search h-full w-full" placeholder="搜索音色">
    <button id="voice" aria-selected="false"><p>英语-Amanda</p></button>
    <input class="w-12" value="50"><input class="w-12" value="50"><input class="w-12" value="50">
    <button id="generate">生成音频</button><div id="order" hidden>去下载</div>
    <div id="settings" class="ant-modal" hidden>
      <strong>作品设置</strong><input placeholder="作品名称">
      <label><input type="radio" name="exportFormat" value="wav" checked>WAV</label>
      <label><input type="radio" name="exportFormat" value="mp3">MP3</label>
      <div><span>AI标识</span><button id="ai" role="switch" aria-checked="false">off</button></div>
      <button id="confirm">确认合成</button>
    </div>
    <script>
      const search = document.querySelector('.voice-search'), voice = document.querySelector('#voice');
      search.addEventListener('input', () => {{ voice.hidden = !voice.textContent.includes(search.value); }});
      voice.addEventListener('click', () => voice.setAttribute('aria-selected','true'));
      document.querySelector('#generate').addEventListener('click', () => document.querySelector('#settings').hidden = false);
      document.querySelector('#confirm').addEventListener('click', () => {{
        document.querySelector('#settings').hidden = true;
        setTimeout(() => document.querySelector('#order').hidden = false, 80);
      }});
    </script>
    """


def _download_fixture() -> str:
    mp3 = base64.b64encode(b"ID3\x04\x00synthetic-mp3").decode()
    return f"""
    <!doctype html><style>
      .works__item {{ border:1px solid #aaa; padding:8px; margin:4px; min-height:24px; }}
      #confirm-modal {{ display:none; border:1px solid #444; padding:12px; background:white; }}
    </style><h3>作品名称</h3><div>审核通过</div><main id="list"></main>
    <button id="download" disabled>下载</button>
    <div id="confirm-modal" class="ant-modal"><span>下载确认</span><button id="confirm-download">下载</button></div>
    <script>
      const records = [{{id:'work-a', name:'batch-a', order:'order-a'}}, {{id:'work-b', name:'batch-b', order:'order-b'}}];
      const render = () => {{
        document.querySelector('#list').innerHTML = records.map((r) => '<div class="works__item" data-id="'+r.id+'"><input class="ant-checkbox-input" type="checkbox"><span class="works__name">'+r.name+'</span><span>'+r.order+' 审核通过</span></div>').join('');
        document.querySelectorAll('.works__item input').forEach((input) => input.addEventListener('change', () => setTimeout(() => {{ document.querySelector('#download').disabled = ![...document.querySelectorAll('.works__item input')].some((x) => x.checked); }}, 180)));
      }};
      setTimeout(render, 120);
      document.querySelector('#download').addEventListener('click', () => {{ document.querySelector('#confirm-modal').style.display='block'; }});
      document.querySelector('#confirm-download').addEventListener('click', () => {{
        document.querySelector('#confirm-modal').style.display='none';
        [...document.querySelectorAll('.works__item input:checked')].forEach((input, index) => setTimeout(() => {{
          const name = input.closest('.works__item').querySelector('.works__name').textContent;
          const link = document.createElement('a'); link.download = name + '.mp3'; link.href = 'data:audio/mpeg;base64,{mp3}'; link.click();
        }}, index * 40));
      }});
    </script>
    """


def _run_audio_matrix(context: Any, temp: Path) -> dict[str, Any]:
    session = XunFeiSession("amanda")
    session._logged_in = True
    page = context.new_page()
    page.set_content(_generation_fixture())
    session._page = page
    started = time.perf_counter()
    session._input_text(page, "A short sentence for generation.")
    session._select_voice(page, session.voice_name, voice_key="amanda")
    session._apply_params(page, 45, 50, 55)
    session._click_generate(page)
    assert session._ensure_mp3_format(page, timeout=5)
    status = session._confirm_synth(page, works_name="windows-matrix")
    assert status == "ok"

    # Exercise the composite editor's real multi-row selection path too; this
    # catches the Windows keyboard/selection transport without a provider login.
    page.set_content('<div class="ssml-editor" contenteditable="true"><p>one</p><p>two</p><p>three</p></div>')
    rows = [{"text": "one"}, {"text": "two"}, {"text": "three"}]
    session._input_composite_text(page, rows)
    session._select_editor_rows(page, rows, 0, 1)

    download_page = context.new_page()
    download_page.set_content(_download_fixture())
    session._wait_download_page_ready(download_page, timeout=5)
    targets = [
        {"works_id": "work-a", "order_no": "order-a", "works_name": "batch-a"},
        {"works_id": "work-b", "order_no": "order-b", "works_name": "batch-b"},
    ]
    selected, missing = session._select_download_rows(download_page, targets, timeout=8)
    assert not missing and set(selected) == {"work-a", "work-b"}
    downloads = session._download_selected_rows(download_page, targets)
    assert len(downloads) == 2
    saved = []
    for download in downloads:
        output = temp / download.suggested_filename
        download.save_as(str(output))
        data = output.read_bytes()
        assert data.startswith(b"ID3")
        saved.append(output.name)
    assert {"batch-a.mp3", "batch-b.mp3"} == set(saved)
    return {
        "generation_status": status,
        "composite_rows": len(rows),
        "selected_downloads": len(downloads),
        "download_files": sorted(saved),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def main() -> None:
    output = Path(os.environ.get("FULL_SCENARIO_OUTPUT", "windows-full-scenario-results.json"))
    with tempfile.TemporaryDirectory(prefix="wordtts-full-scenario-") as folder:
        temp = Path(folder)
        paths = _asset_files(temp / "assets")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            form_page = context.new_page()
            form_page.set_content(_form_fixture())
            form_report = _run_form_and_template(form_page)
            content_page = context.new_page()
            content_report = _run_content_matrix(content_page, paths)
            audio_report = _run_audio_matrix(context, temp / "downloads")
            browser.close()

    result = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "scenarios": {
            "base_form_and_template": form_report,
            "content_handlers": content_report,
            "audio_generation_and_download": audio_report,
        },
        "coverage": [
            "single-select", "multi-select", "text fill", "template click",
            "choice cards/options/answer", "recording cards/reference answers",
            "lazy sections", "audio upload", "image upload", "save/readback",
            "voice search", "speed/pitch/volume", "MP3 confirmation",
            "composite row selection", "delayed download DOM", "batch download",
        ],
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
