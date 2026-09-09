#!/usr/bin/env python3
"""Standalone visible-page importer for text-reading Word documents.

This module is the 课文录入 (text entry) half of ``platform_entry``.  It does
two things:

* parses the confirmed Section A/B structure from one Word document and
  validates the matching audio files; and
* when ``--execute`` is supplied, enters the resulting records through the
  visible platform browser page.

The default mode is a local plan/dry run.  No platform API is called by this
module; execute mode only drives the rendered page and its file inputs.  All
page driving is shared with the system-input adapter in
``platform_entry/adapter/textbook_page.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from question_types.text_utils import sanitize, split_sentences


DEFAULT_DOCUMENT = Path("/Users/asklins/Downloads/课文跟读-7上-Unit1 You and Me.docx")
DEFAULT_AUDIO_DIR = Path("/Users/asklins/Downloads/audio")
DEFAULT_PROFILE_DIR = ROOT / ".runtime" / "text_entry_chrome_profile"

NAME_SUFFIX = "11"
NO_TEST_WORD = "测试"

NUMBERED_RE = re.compile(r"^(?P<number>\d+)\s*[.、)）]\s*(?P<text>.+?)\s*$")
TRANSLATION_RE = re.compile(r"^中文\s*[：:]\s*(?P<text>.*)$")
ROLE_LINE_RE = re.compile(r"^(?P<role>[^:：\n]{1,60}?)\s*[:：]\s*(?P<text>.+?)\s*$")
SECTION_RE = re.compile(r"^Section\s+(?P<letter>[AB])\s*$", re.I)


class TextInputPlanError(ValueError):
    """The document/audio pair cannot be converted into a safe input plan."""


def _clean(value: str) -> str:
    """Normalise Word paragraph whitespace without changing punctuation."""

    return sanitize(str(value or "")).strip()


def _read_paragraphs(document_path: Path) -> list[str]:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - environment diagnostic
        raise TextInputPlanError(
            "当前 Python 环境缺少 python-docx，无法读取 Word 文档。"
        ) from exc

    try:
        document = Document(str(document_path))
    except Exception as exc:
        raise TextInputPlanError(f"无法读取 Word 文档 {document_path}: {exc}") from exc

    paragraphs = [_clean(paragraph.text) for paragraph in document.paragraphs]
    return [paragraph for paragraph in paragraphs if paragraph]


def _find_exact(paragraphs: list[str], value: str, start: int = 0) -> int:
    expected = value.casefold()
    for index in range(max(0, start), len(paragraphs)):
        if paragraphs[index].casefold() == expected:
            return index
    raise TextInputPlanError(f"文档中没有找到结构标记：{value}")


def _find_section(paragraphs: list[str], letter: str) -> int:
    expected = letter.casefold()
    for index, paragraph in enumerate(paragraphs):
        match = SECTION_RE.match(paragraph)
        if match and match.group("letter").casefold() == expected:
            return index
    raise TextInputPlanError(f"文档中没有找到 Section {letter}")


def _parse_numbered_sentences(
    paragraphs: list[str],
    start: int,
    stop_values: Iterable[str],
) -> tuple[list[dict[str, Any]], int]:
    stop_set = {value.casefold() for value in stop_values}
    result: list[dict[str, Any]] = []
    index = start
    expected_number = 1
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        if paragraph.casefold() in stop_set:
            break
        match = NUMBERED_RE.match(paragraph)
        if not match:
            raise TextInputPlanError(
                f"句子跟读中出现无法识别的段落（第 {index + 1} 段）：{paragraph}"
            )
        number = int(match.group("number"))
        if number != expected_number:
            raise TextInputPlanError(
                f"句子编号不连续：期望 {expected_number}，实际 {number}（{paragraph}）"
            )
        original = match.group("text").strip()
        translation = ""
        if index + 1 < len(paragraphs):
            translation_match = TRANSLATION_RE.match(paragraphs[index + 1])
            if translation_match:
                translation = translation_match.group("text").strip()
                index += 1
        result.append(
            {
                "original": original,
                "translation": translation,
                "number": number,
            }
        )
        expected_number += 1
        index += 1
    if not result:
        raise TextInputPlanError("句子跟读区没有提取到句子")
    return result, index


def _parse_conversation_items(
    paragraphs: list[str],
    start: int,
    stop_values: Iterable[str],
    stem_prefix: str,
    first_audio_number: int,
) -> tuple[dict[int, list[dict[str, Any]]], int]:
    stop_set = {value.casefold() for value in stop_values}
    conversations: dict[int, list[dict[str, Any]]] = {}
    current_number: int | None = None
    index = start
    audio_number = first_audio_number
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        if paragraph.casefold() in stop_set:
            break
        conversation_match = re.match(
            r"^Conversation\s*(?P<number>\d+)\s*$", paragraph, re.I
        )
        if conversation_match:
            current_number = int(conversation_match.group("number"))
            conversations.setdefault(current_number, [])
            index += 1
            continue
        if current_number is None:
            raise TextInputPlanError(f"对话内容出现在 Conversation 标记之前：{paragraph}")
        role_match = ROLE_LINE_RE.match(paragraph)
        if not role_match:
            raise TextInputPlanError(f"对话行缺少角色前缀：{paragraph}")
        conversations[current_number].append(
            {
                # 角色名作为独立的页面选择项提交；原文编辑器只填写台词。
                "original": role_match.group("text").strip(),
                "translation": "",
                "role": role_match.group("role").strip(),
                "audio_stem": f"{stem_prefix}{current_number}-{audio_number}",
            }
        )
        audio_number += 1
        index += 1

    if not conversations:
        raise TextInputPlanError("段落跟读区没有提取到 Conversation")
    return conversations, index


def _items_from_paragraph(
    paragraph: str,
    stem_prefix: str,
    first_audio_number: int,
    *,
    paragraph_title: str = "",
) -> list[dict[str, Any]]:
    sentences = split_sentences(paragraph)
    if not sentences:
        sentences = [paragraph]
    return [
        {
            "original": sentence,
            "translation": "",
            "audio_stem": f"{stem_prefix}{first_audio_number + index}",
            "paragraph_title": paragraph_title,
        }
        for index, sentence in enumerate(sentences)
    ]


def _with_audio(items: list[dict[str, Any]], audio_dir: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        stem = str(item.get("audio_stem") or "").strip()
        if not stem:
            raise TextInputPlanError(f"音频命名为空：{item}")
        path = (audio_dir / f"{stem}.mp3").resolve()
        if not path.is_file():
            raise TextInputPlanError(f"找不到音频文件：{path}")
        copied = dict(item)
        copied["audio_filename"] = path.name
        copied["audio_path"] = str(path)
        result.append(copied)
    return result


def _record(
    *,
    name_zh: str,
    name_en: str,
    form: str,
    lesson: str,
    items: list[dict[str, Any]],
    audio_dir: Path,
    roles: list[str] | None = None,
    paragraphs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if NO_TEST_WORD in name_zh or NO_TEST_WORD in name_en:
        raise TextInputPlanError("课文名称不能包含“测试”")
    rendered_items = _with_audio(items, audio_dir)
    record = {
        "name_zh": f"{name_zh}{NAME_SUFFIX}",
        "name_en": f"{name_en}{NAME_SUFFIX}",
        "form": form,
        "version": "人教版",
        "stage": "初中",
        "grade": "七年级",
        "volume": "上册",
        "unit": "Unit 1",
        "lesson": lesson,
        "roles": list(dict.fromkeys(role for role in (roles or []) if str(role).strip())),
        "items": rendered_items,
    }
    if paragraphs is not None:
        rendered_paragraphs = []
        flattened: list[dict[str, Any]] = []
        for paragraph in paragraphs:
            paragraph_items = _with_audio(
                list(paragraph.get("items") or []),
                audio_dir,
            )
            rendered = {
                "title": str(paragraph.get("title") or "").strip(),
                "items": paragraph_items,
            }
            rendered_paragraphs.append(rendered)
            flattened.extend(paragraph_items)
        record["paragraphs"] = rendered_paragraphs
        record["items"] = flattened
    return record


def _validate_plan(plan: dict[str, Any]) -> None:
    records = plan.get("records") or []
    if not records:
        raise TextInputPlanError("没有生成可录入的课文记录")
    total_items = sum(len(record.get("items") or []) for record in records)
    if total_items <= 0:
        raise TextInputPlanError("没有生成可录入的课文内容")

    names: list[str] = []
    audio_paths: list[str] = []
    for record in records:
        for field in ("name_zh", "name_en"):
            value = str(record.get(field) or "")
            if not value.endswith(NAME_SUFFIX):
                raise TextInputPlanError(f"名称没有追加 {NAME_SUFFIX}：{value}")
            if NO_TEST_WORD in value:
                raise TextInputPlanError(f"名称包含禁用词：{value}")
            names.append(value)
        if record.get("form") == "段落":
            paragraphs = record.get("paragraphs")
            if not isinstance(paragraphs, list) or not paragraphs:
                raise TextInputPlanError("段落类型课文必须包含至少一个段落")
            for paragraph_index, paragraph in enumerate(paragraphs, 1):
                if not isinstance(paragraph, dict):
                    raise TextInputPlanError(
                        f"段落类型课文的第 {paragraph_index} 段格式无效"
                    )
                paragraph_items = paragraph.get("items") or []
                if not paragraph_items:
                    raise TextInputPlanError(
                        f"段落类型课文的第 {paragraph_index} 段没有句子"
                    )
        for item in record.get("items") or []:
            audio_paths.append(str(item.get("audio_path") or ""))
            if record.get("form") == "角色扮演" and item.get("role"):
                if str(item.get("original") or "").startswith(f"{item['role']}:"):
                    raise TextInputPlanError("角色扮演原文不应重复包含角色名前缀")
    if len(audio_paths) != len(set(audio_paths)):
        raise TextInputPlanError("同一个音频文件被多个录入项重复使用")


def build_plan(document_path: Path, audio_dir: Path) -> dict[str, Any]:
    """Parse the confirmed Section A/B document into page-ready records."""

    document_path = document_path.expanduser().resolve()
    audio_dir = audio_dir.expanduser().resolve()
    if not document_path.is_file():
        raise TextInputPlanError(f"找不到 Word 文档：{document_path}")
    if not audio_dir.is_dir():
        raise TextInputPlanError(f"找不到音频目录：{audio_dir}")

    paragraphs = _read_paragraphs(document_path)
    section_a = _find_section(paragraphs, "A")
    section_b = _find_section(paragraphs, "B")
    if section_b <= section_a:
        raise TextInputPlanError("Section A/B 顺序异常")

    a_sentence_heading = _find_exact(paragraphs, "句子跟读", section_a + 1)
    a_paragraph_heading = _find_exact(paragraphs, "段落跟读", a_sentence_heading + 1)
    a_sentences, _ = _parse_numbered_sentences(
        paragraphs,
        a_sentence_heading + 1,
        {"段落跟读", "Section B"},
    )
    a_conversations, _ = _parse_conversation_items(
        paragraphs,
        a_paragraph_heading + 1,
        {"Section B"},
        "SA-段-C",
        1,
    )
    if sorted(a_conversations) != [1, 2]:
        raise TextInputPlanError(f"Section A 对话编号异常：{sorted(a_conversations)}")
    if [len(a_conversations[1]), len(a_conversations[2])] != [9, 9]:
        raise TextInputPlanError("Section A 的 Conversation 句数不是 9 + 9")
    for index, item in enumerate(a_sentences, 1):
        item["audio_stem"] = f"SA句子{index}"

    b_sentence_heading = _find_exact(paragraphs, "句子跟读", section_b + 1)
    b_discourse_heading = _find_exact(paragraphs, "语篇跟读", b_sentence_heading + 1)
    b_sentences, _ = _parse_numbered_sentences(
        paragraphs,
        b_sentence_heading + 1,
        {"语篇跟读"},
    )
    if len(b_sentences) != 10:
        raise TextInputPlanError(f"Section B 句子跟读预期 10 条，实际 {len(b_sentences)} 条")
    for index, item in enumerate(b_sentences, 1):
        item["audio_stem"] = f"SB句子{index}"

    reading_plus_heading = _find_exact(paragraphs, "Reading Plus", b_discourse_heading + 1)
    reading_plus_title = _find_exact(paragraphs, "Making New Friends at School", reading_plus_heading + 1)
    making_title = "Making new friends"
    making_title_index = b_discourse_heading + 1
    if paragraphs[making_title_index] != making_title:
        raise TextInputPlanError(
            f"语篇标题异常：预期 {making_title}，实际 {paragraphs[making_title_index]}"
        )
    if paragraphs[making_title_index + 1] != "Pauline Lee":
        raise TextInputPlanError("没有找到 Pauline Lee 语篇")
    pauline_body = paragraphs[making_title_index + 2]
    if paragraphs[making_title_index + 3] != "Peter Brown":
        raise TextInputPlanError("没有找到 Peter Brown 语篇")
    peter_body = paragraphs[making_title_index + 4]
    reading_bodies = paragraphs[reading_plus_title + 1 :]
    if len(reading_bodies) != 5:
        raise TextInputPlanError(
            f"Reading Plus 正文段落预期 5 段，实际 {len(reading_bodies)} 段"
        )

    pauline_items = _items_from_paragraph(
        pauline_body,
        "SB语篇",
        3,
        paragraph_title="Pauline Lee",
    )
    peter_items = _items_from_paragraph(
        peter_body,
        "SB语篇",
        13,
        paragraph_title="Peter Brown",
    )
    reading_paragraphs: list[dict[str, Any]] = []
    next_number = 23
    for paragraph in reading_bodies:
        items = _items_from_paragraph(paragraph, "SB语篇", next_number)
        reading_paragraphs.append({"title": "", "items": items})
        next_number += len(items)
    reading_item_count = sum(len(paragraph["items"]) for paragraph in reading_paragraphs)
    if len(pauline_items) != 9 or len(peter_items) != 8 or reading_item_count != 23:
        raise TextInputPlanError(
            "语篇切割数量异常："
            f"Pauline={len(pauline_items)}, Peter={len(peter_items)}, "
            f"Reading Plus={reading_item_count}"
        )
    if next_number != 46:
        raise TextInputPlanError(f"Reading Plus 音频编号应结束于 45，实际结束于 {next_number - 1}")

    records = [
        _record(
            name_zh="Section A",
            name_en="How do we get to know each other?",
            form="角色扮演",
            lesson="Section A",
            items=a_sentences,
            audio_dir=audio_dir,
        ),
        _record(
            name_zh="Section A 1b/1c",
            name_en="Conversation 1",
            form="角色扮演",
            lesson="Section A",
            items=a_conversations[1],
            audio_dir=audio_dir,
            roles=[item["role"] for item in a_conversations[1]],
        ),
        _record(
            name_zh="Section A 1b/1c",
            name_en="Conversation 2",
            form="角色扮演",
            lesson="Section A",
            items=a_conversations[2],
            audio_dir=audio_dir,
            roles=[item["role"] for item in a_conversations[2]],
        ),
        _record(
            name_zh="Section B",
            name_en="What do we need to know about a new friend?",
            form="角色扮演",
            lesson="Section B",
            items=b_sentences,
            audio_dir=audio_dir,
        ),
        _record(
            name_zh="Making new friends",
            name_en="Pauline Lee",
            form="同步课文",
            lesson="Section B",
            items=pauline_items,
            audio_dir=audio_dir,
        ),
        _record(
            name_zh="Making new friends",
            name_en="Peter Brown",
            form="同步课文",
            lesson="Section B",
            items=peter_items,
            audio_dir=audio_dir,
        ),
    ]
    reading_form = "同步课文" if len(reading_paragraphs) == 1 else "段落"
    records.append(
        _record(
            name_zh="Reading Plus",
            name_en="Making New Friends at School",
            form=reading_form,
            lesson="Reading plus",
            items=[],
            paragraphs=reading_paragraphs,
            audio_dir=audio_dir,
        )
    )
    plan = {
        "source_document": str(document_path),
        "audio_dir": str(audio_dir),
        "name_suffix": NAME_SUFFIX,
        "record_count": len(records),
        "item_count": sum(len(record["items"]) for record in records),
        "records": records,
    }
    _validate_plan(plan)
    return plan


def execute_plan(
    plan: dict[str, Any],
    *,
    profile_dir: Path,
    login_timeout_seconds: int,
    keep_browser_open: bool = False,
) -> dict[str, Any]:
    """Run the plan through the visible browser UI."""

    try:
        from playwright.sync_api import sync_playwright
        from platform_entry.adapter.runtime import _launch_browser
        from platform_entry.adapter.textbook_page import (
            TextbookRecordObserver,
            _create_textbook_record,
            _ensure_textbook_roles,
            _open_text_list,
        )
        from platform_entry.adapter.constants import API_BASE_URL
    except ImportError as exc:  # pragma: no cover - environment diagnostic
        raise RuntimeError(
            "执行模式需要 Playwright；请使用项目 .venv/bin/python 运行此脚本。"
        ) from exc

    profile_dir = profile_dir.expanduser().resolve()
    with sync_playwright() as playwright:
        context = _launch_browser(playwright, profile_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _ensure_textbook_roles(page, plan["records"], login_timeout_seconds)
            _open_text_list(page, login_timeout_seconds)
            records = plan["records"]
            observer = TextbookRecordObserver(page, api_base=API_BASE_URL)
            for index, record in enumerate(records, 1):
                _create_textbook_record(page, record, index, len(records), observer)
            result = {
                "status": "completed",
                "record_count": len(records),
                "item_count": plan["item_count"],
                "names": [
                    {"name_zh": record["name_zh"], "name_en": record["name_en"]}
                    for record in records
                ],
            }
            print(
                f"[browser] 完成：{result['record_count']} 篇，{result['item_count']} 条。",
                flush=True,
            )
            if keep_browser_open:
                print("[browser] Chrome 保持打开；按 Ctrl+C 结束脚本。", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
            return result
        finally:
            context.close()


def execute_plan_over_cdp(
    plan: dict[str, Any],
    *,
    cdp_endpoint: str,
    login_timeout_seconds: int,
    keep_browser_open: bool = False,
) -> dict[str, Any]:
    """Run the plan through an already-running Chrome via CDP.

    用于平台会把独立 profile 会话踢下线的情况：直接复用用户自己
    已登录的浏览器会话。退出时只断开连接，不关闭用户的浏览器。
    """

    try:
        from playwright.sync_api import sync_playwright
        from platform_entry.adapter.textbook_page import (
            TextbookRecordObserver,
            _create_textbook_record,
            _ensure_textbook_roles,
            _open_text_list,
        )
        from platform_entry.adapter.constants import API_BASE_URL
    except ImportError as exc:  # pragma: no cover - environment diagnostic
        raise RuntimeError(
            "执行模式需要 Playwright；请使用项目 .venv/bin/python 运行此脚本。"
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_endpoint)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            # 永远新开标签页，不抢占用户已打开的页面。
            page = context.new_page()
            _ensure_textbook_roles(page, plan["records"], login_timeout_seconds)
            _open_text_list(page, login_timeout_seconds)
            records = plan["records"]
            observer = TextbookRecordObserver(page, api_base=API_BASE_URL)
            for index, record in enumerate(records, 1):
                _create_textbook_record(page, record, index, len(records), observer)
            result = {
                "status": "completed",
                "mode": "cdp",
                "record_count": len(records),
                "item_count": plan["item_count"],
                "names": [
                    {"name_zh": record["name_zh"], "name_en": record["name_en"]}
                    for record in records
                ],
            }
            print(
                f"[browser] 完成：{result['record_count']} 篇，{result['item_count']} 条。",
                flush=True,
            )
            if keep_browser_open:
                print("[browser] 脚本完成；浏览器保持打开。", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
            return result
        finally:
            # 只断开 CDP 连接；用户自己的浏览器保持原样。
            browser.close()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _print_summary(plan: dict[str, Any]) -> None:
    print(
        f"[plan] {plan['record_count']} 篇，{plan['item_count']} 条；"
        f"名称后缀={plan['name_suffix']}；未包含“{NO_TEST_WORD}”。"
    )
    for index, record in enumerate(plan["records"], 1):
        print(
            f"  {index}. {record['name_zh']} / {record['name_en']}"
            f" | {record['form']} | {record['lesson']} | {len(record['items'])} 条"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", nargs="?", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--plan-out", type=Path, help="把解析计划写入 JSON 文件")
    parser.add_argument("--execute", action="store_true", help="通过可见浏览器实际录入")
    parser.add_argument(
        "--cdp",
        type=str,
        default="",
        help="连接已运行的 Chrome（需带 --remote-debugging-port 启动）替代独立 profile",
    )
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--login-timeout", type=int, default=600)
    parser.add_argument("--result-out", type=Path, help="把执行结果写入 JSON 文件")
    parser.add_argument("--keep-browser-open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        plan = build_plan(args.document, args.audio_dir)
        _print_summary(plan)
        if args.plan_out:
            _write_json(args.plan_out, plan)
            print(f"[plan] 已写入 {args.plan_out.expanduser().resolve()}")
        if not args.execute:
            return 0
        common = {
            "login_timeout_seconds": max(1, args.login_timeout),
            "keep_browser_open": args.keep_browser_open,
        }
        if args.cdp:
            result = execute_plan_over_cdp(plan, cdp_endpoint=args.cdp, **common)
        else:
            result = execute_plan(plan, profile_dir=args.profile_dir, **common)
        if args.result_out:
            _write_json(args.result_out, result)
            print(f"[browser] 已写入 {args.result_out.expanduser().resolve()}")
        return 0
    except (TextInputPlanError, RuntimeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
