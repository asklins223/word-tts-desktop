"""2026 新题型整套试卷的解析回归测试。"""

from collections import Counter
from pathlib import Path

import pytest

from question_types import parse_document_auto
from question_types.segmenter import parse_document_once
from wordtts import build_synthesis_segments
from wordtts.progress import build_progress
from workflow.parser import LegacyWordParser


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted(ROOT.glob("examples/documents/*2026新题型*.docx"))


@pytest.mark.skipif(not FIXTURES, reason="2026 新题型试卷未随工作区提供")
@pytest.mark.parametrize(
    "path",
    FIXTURES,
    ids=[path.name for path in FIXTURES],
)
def test_full_2026_exam_is_supported(path):
    results, summary = parse_document_once(path)

    assert summary == "检测到 4 种题型，成功提取 15 条内容"
    assert [result["doc_type"] for result in results] == [
        "听后选择",
        "听后应答",
        "听后记录并转述信息",
        "模仿朗读",
    ]

    by_type = {result["doc_type"]: result for result in results}
    assert by_type["听后选择"]["item_count"] == 6
    assert [
        question["number"]
        for question in by_type["听后选择"]["questions"]
    ] == list(range(1, 9))
    assert by_type["听后应答"]["item_count"] == 7

    progress = build_progress(path.name, str(path), results, {})
    assert [item["filename"] for item in progress["items"]] == [
        "听后选择-1.mp3",
        "听后选择-2.mp3",
        "听后选择-3.mp3",
        "听后选择-4.mp3",
        "听后选择-5.mp3",
        "听后选择-6.mp3",
        "听后应答-1.mp3",
        "听后应答-2.mp3",
        "听后应答-3.mp3",
        "听后应答-4.mp3",
        "听后应答-5.mp3",
        "听后应答-6.mp3",
        "听后应答-7.mp3",
        "听后记录并转述信息-第一节听后记录-1.mp3",
        "模仿朗读-1.mp3",
    ]

    assert [
        item["question_numbers"]
        for item in by_type["听后选择"]["items"]
    ] == [[1], [2], [3], [4], [5, 6], [7, 8]]
    assert [
        item["number"] for item in by_type["听后应答"]["items"]
    ] == list(range(9, 16))

    record_item = by_type["听后记录并转述信息"]["items"][0]
    assert record_item["type_path"] == ["听后记录并转述信息", "第一节听后记录"]
    assert record_item["question_numbers"] == [17, 18, 19]
    assert record_item["audio_filename_stem"] == (
        "听后记录并转述信息-第一节听后记录-1"
    )
    assert record_item["text"]
    assert "计算机" not in record_item["text"]
    assert not any("\u3400" <= char <= "\u9fff" for char in record_item["text"])
    assert build_synthesis_segments(
        record_item["text"], 50, 50, 50
    )[0]["voice_key"] == "amanda"

    imitation_items = by_type["模仿朗读"]["items"]
    assert len(imitation_items) == 1
    assert imitation_items[0]["number"] == 16
    assert imitation_items[0]["audio_filename_stem"] == "模仿朗读-1"
    assert not any("\u3400" <= char <= "\u9fff" for char in imitation_items[0]["text"])

    # 自动分段路径和兼容入口必须保持相同结果，确保桌面工作流不会回退到
    # 旧的“把听后记录表也当成模仿朗读稿”的行为。
    assert parse_document_auto(path) == (results, summary)

    parsed = LegacyWordParser().parse(path)
    assert parsed.item_count == 15
    assert Counter(item.item_type for item in parsed.items) == Counter({
        "听后选择录音稿": 6,
        "听后应答录音稿": 7,
        "听后记录并转述信息录音稿": 1,
        "模仿朗读-框内英文": 1,
    })
