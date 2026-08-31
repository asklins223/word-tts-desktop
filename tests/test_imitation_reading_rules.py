"""模仿朗读新旧文档规则测试。"""

import re
from pathlib import Path

from question_types import ImitationReadingParser
from question_types.text_utils import load_paragraphs, match_script_marker
from workflow.parser import LegacyWordParser


ROOT = Path(__file__).resolve().parents[1]
NEW_FIXTURE = ROOT / "examples/documents/七上Starter Unit1 Hello模仿朗读专项.docx"
OLD_FIXTURE = ROOT / "examples/documents/模仿朗读-7上-U5-U6.docx"


def test_boxed_english_rule_extracts_only_the_two_passages():
    result = ImitationReadingParser(str(NEW_FIXTURE)).parse()

    assert result["doc_type"] == "模仿朗读"
    assert result["item_count"] == 2
    assert [item["number"] for item in result["items"]] == [16, 17]
    assert [item["category"] for item in result["items"]] == [
        "模仿朗读-框内英文",
        "模仿朗读-框内英文",
    ]
    assert all(item["source"] == "框内英文" for item in result["items"])
    assert all(item["voice"] == "female" for item in result["items"])
    assert all(not re.search(r"[\u3400-\u9fff]", item["text"]) for item in result["items"])
    assert "请在90秒钟内朗读" not in result["items"][0]["text"]
    assert result["items"][0]["text"].startswith("One morning, Teng Fei starts")
    assert result["items"][1]["text"].startswith("Good afternoon, everyone!")


def test_boxed_rule_removes_chinese_from_mixed_cell_text():
    assert ImitationReadingParser._english_only("提示：Hello world.") == "Hello world."
    assert ImitationReadingParser._english_only("只有中文说明") == ""


def test_script_marker_accepts_colon_inside_wrapping_brackets():
    for value in ("【录音原文：】W: Hello.", "录音原文 W: Hello."):
        match = match_script_marker(value)

        assert match is not None
        assert match.group(1) == "W: Hello."


def test_new_rule_voice_survives_workflow_parser_normalization():
    parsed = LegacyWordParser().parse(NEW_FIXTURE)

    assert parsed.parser_version == "18"
    assert [item.metadata["voice"] for item in parsed.items] == ["female", "female"]


def test_boxed_rule_keeps_legacy_two_part_preloaded_api_compatible():
    paras, metadata = load_paragraphs(NEW_FIXTURE, include_metadata=True)

    result = ImitationReadingParser(
        str(NEW_FIXTURE),
        preloaded_paras=(paras, metadata),
    ).parse()

    assert result["item_count"] == 2
    assert [item["number"] for item in result["items"]] == [16, 17]


def test_direct_exam_rule_skips_mixed_language_instruction_lines():
    paras = [
        (0, "16. 模仿朗读", "Normal"),
        (1, "你可以这样开始：Let me tell you about Li Ling.", "Normal"),
        (2, "Good morning, everyone!", "Normal"),
        (3, "信息获取", "Normal"),
        (4, "W: This belongs to another section.", "Normal"),
    ]
    metadata = [{} for _ in paras]

    result = ImitationReadingParser(
        "synthetic.docx",
        preloaded_paras=(paras, metadata),
    ).parse()

    assert result["item_count"] == 1
    assert result["items"][0]["text"] == "Good morning, everyone!"


def test_direct_exam_rule_skips_reference_answers():
    paras = [
        (0, "一、模仿朗读（共1题）", "Normal"),
        (1, "Good morning, everyone!", "Normal"),
        (2, "参考答案：", "Normal"),
        (3, "Good morning, everyone! This is a reference answer.", "Normal"),
        (4, "二、信息获取", "Normal"),
    ]
    metadata = [{} for _ in paras]

    result = ImitationReadingParser(
        "synthetic.docx",
        preloaded_paras=(paras, metadata),
    ).parse()

    assert result["item_count"] == 1
    assert result["items"][0]["text"] == "Good morning, everyone!"


def test_legacy_imitation_reading_rule_is_unchanged():
    result = ImitationReadingParser(str(OLD_FIXTURE)).parse()

    assert result["item_count"] == 6
    assert [item["category"] for item in result["items"]].count("模仿朗读-外网") == 4
    assert [item["category"] for item in result["items"]].count("模仿朗读-教材") == 2
    assert {item["source"] for item in result["items"]} == {"外网", "教材"}
    assert all("voice" not in item for item in result["items"])
