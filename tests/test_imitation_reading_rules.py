"""模仿朗读新旧文档规则测试。"""

import re
from pathlib import Path

from question_types import ImitationReadingParser
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


def test_new_rule_voice_survives_workflow_parser_normalization():
    parsed = LegacyWordParser().parse(NEW_FIXTURE)

    assert parsed.parser_version == "16"
    assert [item.metadata["voice"] for item in parsed.items] == ["female", "female"]


def test_legacy_imitation_reading_rule_is_unchanged():
    result = ImitationReadingParser(str(OLD_FIXTURE)).parse()

    assert result["item_count"] == 6
    assert [item["category"] for item in result["items"]].count("模仿朗读-外网") == 4
    assert [item["category"] for item in result["items"]].count("模仿朗读-教材") == 2
    assert {item["source"] for item in result["items"]} == {"外网", "教材"}
    assert all("voice" not in item for item in result["items"])
