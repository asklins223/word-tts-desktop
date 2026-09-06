"""模仿朗读新旧文档规则测试。"""

import json
import re
from pathlib import Path

from question_types import ImitationReadingParser
from question_types.text_utils import load_paragraphs, match_script_marker
from workflow.parser import DocumentParser
from workflow.system_input import _resolve_unit_groups
from workflow.system_input_content import build_page_input_facts, document_entry_support


ROOT = Path(__file__).resolve().parents[1]
NEW_FIXTURE = ROOT / "examples/documents/七上Starter Unit1 Hello模仿朗读专项.docx"
NUMBERED_FIXTURE = ROOT / "examples/documents/九上Unit1模仿朗读(1).docx"
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
    assert [item["score"] for item in result["items"]] == [7, 7]
    assert all(item["major_section_profile"] == "imitation_boxed_special" for item in result["items"])
    assert all(item["entry_profile"] == "imitation_reading_v1" for item in result["items"])
    assert all(item["capabilities"]["external_input"] is True for item in result["items"])
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
    parsed = DocumentParser().parse(NEW_FIXTURE)

    assert parsed.parser_version == "19"
    assert [item.metadata["voice"] for item in parsed.items] == ["female", "female"]
    assert all(item.metadata["entry_profile"] == "imitation_reading_v1" for item in parsed.items)
    assert all(item.metadata["capabilities"]["external_input"] is True for item in parsed.items)
    assert all("page_input" in item.metadata for item in parsed.items)


def test_numbered_exam_rule_is_entry_eligible_after_normalization():
    parsed = DocumentParser().parse(NUMBERED_FIXTURE)

    assert len(parsed.items) == 2
    assert {item.metadata["major_section_profile"] for item in parsed.items} == {
        "imitation_numbered_exam_special",
    }
    assert all(item.metadata["entry_profile"] == "imitation_reading_v1" for item in parsed.items)
    assert all(item.metadata["capabilities"]["external_input"] is True for item in parsed.items)
    assert all("page_input" in item.metadata for item in parsed.items)


def test_boxed_rule_uses_the_structured_preloaded_document():
    paras, metadata, blocks = load_paragraphs(
        NEW_FIXTURE,
        include_metadata=True,
        include_blocks=True,
    )

    result = ImitationReadingParser(
        str(NEW_FIXTURE),
        preloaded_paras=(paras, metadata, blocks),
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


def test_direct_exam_rule_recognizes_repeated_blocks_as_two_special_papers():
    paras = [
        (0, "三、模仿朗读（共1题，满分7分）", "Normal"),
        (1, "16.（计算机语音和屏幕文字提示）请听以下短文两遍，然后模仿朗读。", "Normal"),
        (2, "（计算机语音朗读和屏幕文字显示）", "Normal"),
        (3, "In 2017, the railway was opened in Kenya.", "Normal"),
        (4, "17.（计算机语音和屏幕文字提示）请听以下短文两遍，然后模仿朗读。", "Normal"),
        (5, "（计算机语音朗读和屏幕文字显示）", "Normal"),
        (6, "Great changes have taken place in my hometown.", "Normal"),
    ]
    metadata = [{} for _ in paras]

    result = ImitationReadingParser(
        "synthetic.docx",
        preloaded_paras=(paras, metadata),
    ).parse()

    assert result["item_count"] == 2
    assert [item["number"] for item in result["items"]] == [16, 17]
    assert [item["unit_id"] for item in result["items"]] == [
        "imitation-reading-question-16",
        "imitation-reading-question-17",
    ]
    assert [item["unit_label"] for item in result["items"]] == [
        "第16题专项卷",
        "第17题专项卷",
    ]
    assert all(item["type_path"] == ["模仿朗读"] for item in result["items"])
    assert [item["score"] for item in result["items"]] == [7, 7]
    assert [item["text"] for item in result["items"]] == [
        "In 2017, the railway was opened in Kenya.",
        "Great changes have taken place in my hometown.",
    ]


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


def test_direct_exam_rule_does_not_split_a_section_that_declares_two_questions():
    paras = [
        (0, "三、模仿朗读（共2题，满分14分）", "Normal"),
        (1, "16.（计算机语音和屏幕文字提示）请听短文，然后模仿朗读。", "Normal"),
        (2, "In 2017, the railway was opened in Kenya.", "Normal"),
        (3, "17.（计算机语音和屏幕文字提示）请听短文，然后模仿朗读。", "Normal"),
        (4, "Great changes have taken place in my hometown.", "Normal"),
    ]
    metadata = [{} for _ in paras]

    result = ImitationReadingParser(
        "synthetic.docx",
        preloaded_paras=(paras, metadata),
    ).parse()

    assert result["item_count"] == 2
    assert all("unit_id" not in item for item in result["items"])


def test_unit_source_special_rule_splits_each_recording_script_into_a_paper():
    result = ImitationReadingParser(str(OLD_FIXTURE)).parse()

    assert result["item_count"] == 6
    assert {item["category"] for item in result["items"]} == {"模仿朗读-录音稿"}
    assert {item["source"] for item in result["items"]} == {"外网", "教材"}
    assert all("voice" not in item for item in result["items"])
    assert all(item["major_section_profile"] == "imitation_unit_source_special" for item in result["items"])
    assert all(item["entry_profile"] == "imitation_reading_v1" for item in result["items"])
    assert all(item["capabilities"]["external_input"] is True for item in result["items"])
    assert all(item["number"] == 1 for item in result["items"])
    assert [item["score"] for item in result["items"]] == [6, 6, 6, 6, 6, 6]
    assert [item["unit_id"] for item in result["items"]] == [
        "imitation-reading-script-u5-external-1",
        "imitation-reading-script-u5-external-2",
        "imitation-reading-script-u5-textbook-1",
        "imitation-reading-script-u6-external-1",
        "imitation-reading-script-u6-external-2",
        "imitation-reading-script-u6-textbook-1",
    ]
    assert all(item["type_path"] == ["模仿朗读", "录音稿"] for item in result["items"])


def test_legacy_unit_source_rule_without_a_scored_heading_is_unchanged():
    paras = [
        (0, "U5：", "Normal"),
        (1, "外网：", "Normal"),
        (2, "Legacy passage.", "Normal"),
        (3, "请听录音。", "Normal"),
    ]
    result = ImitationReadingParser(
        "synthetic.docx",
        preloaded_paras=(paras, [{} for _ in paras]),
    ).parse()

    assert result["item_count"] == 1
    item = result["items"][0]
    assert item["category"] == "模仿朗读-外网"
    assert item["major_section_profile"] == "imitation_legacy_unit_source"
    assert item["entry_profile"] is None
    assert item["capabilities"]["external_input"] is False
    assert "score" not in item


def test_unit_source_special_rule_keeps_identical_scripts_as_distinct_items():
    paras = [
        (0, "U5：", "Normal"),
        (1, "一、模仿朗读（共 6 分）", "Normal"),
        (2, "外网：", "Normal"),
        (3, "Same script.", "Normal"),
        (4, "外网：", "Normal"),
        (5, "Same script.", "Normal"),
    ]
    result = ImitationReadingParser(
        "synthetic.docx",
        preloaded_paras=(paras, [{} for _ in paras]),
    ).parse()

    assert [item["unit_id"] for item in result["items"]] == [
        "imitation-reading-script-u5-external-1",
        "imitation-reading-script-u5-external-2",
    ]
    assert [item["identity_key"] for item in result["items"]] == [
        "imitation-reading-script-u5-external-1",
        "imitation-reading-script-u5-external-2",
    ]


def test_imitation_page_input_uses_result_score_when_item_score_is_omitted():
    result = {
        "doc_type": "模仿朗读",
        "score_per_item": 6,
    }
    raw_item = {
        "text": "Read this passage aloud.",
        "major_section_profile": "imitation_unit_source_special",
        "entry_profile": "imitation_reading_v1",
        "capabilities": {"external_input": True},
    }

    page_input = build_page_input_facts("模仿朗读", result, raw_item, 0)

    assert page_input["questions"] == [{
        "listening_text": "Read this passage aloud.",
        "score": 6,
        "reference_answers": ["Read this passage aloud."],
    }]


def test_unit_source_special_rule_is_entry_ready_and_groups_one_paper_per_script():
    parsed = DocumentParser().parse(OLD_FIXTURE)

    assert len(parsed.items) == 6
    assert all(item.metadata["page_input"]["type"] == "模仿朗读" for item in parsed.items)
    assert all(
        item.metadata["page_input"]["questions"] == [{
            "listening_text": item.normalized_content,
            "score": 6,
            "reference_answers": [item.normalized_content],
        }]
        for item in parsed.items
    )
    support = document_entry_support(
        [item.metadata.get("page_input") for item in parsed.items],
        input_type="paper",
        document_types=[item.metadata.get("doc_type") for item in parsed.items],
        major_section_profiles=[item.metadata.get("major_section_profile") for item in parsed.items],
        entry_profiles=[item.metadata.get("entry_profile") for item in parsed.items],
        capabilities=[item.metadata.get("capabilities") for item in parsed.items],
    )
    assert support["supported"] is True

    rows = [
        {
            "item_id": f"item-{index}",
            "sequence": index,
            "item_type": item.item_type,
            "source_locator": item.source_locator,
            "metadata_json": json.dumps(item.metadata, ensure_ascii=False),
        }
        for index, item in enumerate(parsed.items)
    ]
    groups, status, evidence = _resolve_unit_groups(rows, None)
    assert status == "multiple_confirmed"
    assert evidence["strategy"] == "repeated_structure"
    assert len(groups) == 6
    assert all(len(group_items) == 1 for group_items in groups.values())
