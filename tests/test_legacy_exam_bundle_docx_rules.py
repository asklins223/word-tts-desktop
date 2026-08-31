"""旧题型整套试卷的解析回归测试。"""

from collections import Counter
from pathlib import Path

import pytest

from audio_naming import is_exam_paper_bundle
from question_types import parse_document_auto
from question_types.segmenter import load_document_once, parse_document_once
from question_model import extract_candidate
from wordtts.progress import build_progress
from workflow.parser import LegacyWordParser


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/documents/佛山七上Starter 1.docx"


def test_legacy_bundle_detection_accepts_chinese_score_numbers():
    paragraphs = [
        (0, "信息获取", "Normal"),
        (1, "信息转述及询问", "Normal"),
        (2, "模仿朗读", "Normal"),
        (3, "共：两小题", "Normal"),
        (4, "满分：六分", "Normal"),
    ]

    assert is_exam_paper_bundle(paragraphs)


@pytest.mark.skipif(not FIXTURE.exists(), reason="旧题型套卷样例未随工作区提供")
def test_legacy_exam_bundle_is_parsed_without_cross_section_leaks():
    results, summary = parse_document_once(FIXTURE)

    assert summary == "检测到 3 种题型，成功提取 16 条内容"
    assert [result["doc_type"] for result in results] == [
        "信息获取",
        "信息转述及询问",
        "模仿朗读",
    ]

    by_type = {result["doc_type"]: result for result in results}
    info_items = by_type["信息获取"]["items"]
    assert by_type["信息获取"]["item_count"] == 14
    assert [
        item["number"] for item in info_items
        if item["category"].endswith("题目")
    ] == list(range(1, 11))
    assert [
        item["voice"] for item in info_items
        if item["category"].endswith("题目")
    ] == ["male", "female"] * 5
    assert [
        item["category"] for item in info_items
        if item["category"].endswith("录音稿")
    ] == [
        "听选信息录音稿",
        "听选信息录音稿",
        "听选信息录音稿",
        "回答问题录音稿",
    ]
    assert all(
        "参考答案" not in item["text"]
        for item in info_items
        if item["category"].endswith("录音稿")
    )

    retelling = by_type["信息转述及询问"]
    assert retelling["item_count"] == 1
    assert retelling["items"][0]["text"].startswith("(W) Good morning")
    assert [task["number"] for task in retelling["tasks"]] == [11, 12]
    assert all(task["reference_answer"] for task in retelling["tasks"])

    imitation = by_type["模仿朗读"]
    assert imitation["item_count"] == 1
    assert imitation["items"][0]["category"] == "模仿朗读-试卷正文"
    assert imitation["items"][0]["text"].startswith("Good morning, everyone!")

    # 文本框是思维导图的结构内容，不应重复进入音频正文，但应在一次加载
    # 的结构块流中保留，供后续题目字段/版式定位能力复用。
    loaded = load_document_once(FIXTURE, include_structure=True)
    textboxes = [block for block in loaded[2] if block.kind == "textbox"]
    assert len(textboxes) == 1
    assert len(textboxes[0].fragments) == 5
    assert "How old is Li Ling?" in textboxes[0].fragments[0]

    progress = build_progress(FIXTURE.name, str(FIXTURE), results, {})
    assert progress["total_items"] == 16
    assert [item["filename"] for item in progress["items"]] == [
        "问题1.mp3",
        "问题2.mp3",
        "听选信息-录音稿1.mp3",
        "问题3.mp3",
        "问题4.mp3",
        "听选信息-录音稿2.mp3",
        "问题5.mp3",
        "问题6.mp3",
        "听选信息-录音稿3.mp3",
        "问题7.mp3",
        "问题8.mp3",
        "问题9.mp3",
        "问题10.mp3",
        "回答问题-录音稿1.mp3",
        "信息转述-录音稿1.mp3",
        "模仿朗读-1.mp3",
    ]

    parsed = LegacyWordParser().parse(FIXTURE)
    assert parsed.item_count == 16
    assert Counter(item.item_type for item in parsed.items) == Counter({
        "听选信息题目": 6,
        "听选信息录音稿": 3,
        "回答问题题目": 4,
        "回答问题录音稿": 1,
        "信息转述录音稿": 1,
        "模仿朗读-试卷正文": 1,
    })

    assert parse_document_auto(FIXTURE) == parse_document_once(FIXTURE)


def test_legacy_exam_bundle_candidate_keeps_visual_content_out_of_audio_items():
    if not FIXTURE.exists():
        pytest.skip("旧题型套卷样例未随工作区提供")

    results, _ = parse_document_once(FIXTURE)
    candidates = [
        extract_candidate(result["doc_type"], result, FIXTURE.name)
        for result in results
    ]

    imitation = next(
        candidate for candidate in candidates
        if candidate.type_code == "imitation_reading"
    )
    assert len(imitation.entities) == 1
    assert imitation.entities[0].text.startswith("Good morning, everyone!")
    assert "How old is Li Ling?" not in imitation.entities[0].text
