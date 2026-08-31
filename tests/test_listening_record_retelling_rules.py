"""「听后记录并转述信息」题型与 W/M 音色规则测试。"""

import asyncio
from pathlib import Path
from unittest import mock

from pydub import AudioSegment

from question_model import extract_candidate
from question_types import (
    ListeningRecordRetellingParser,
    detect_doc_type,
    parse_document_auto,
)
from question_types.segmenter import parse_document_once
from wordtts import synthesis
from wordtts.progress import build_progress
from wordtts.synthesis import build_synthesis_segments
from workflow.parser import LegacyWordParser


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / (
    "examples/documents/七上Starter Unit 1 Hello听后记录并转述信息专项-"
    "答案扩展(1).docx"
)


def test_sample_extracts_only_the_first_section_listening_script():
    results, summary = parse_document_auto(FIXTURE)

    assert summary == "检测到 1 种题型，成功提取 1 条内容"
    assert [result["doc_type"] for result in results] == ["听后记录并转述信息"]
    result = results[0]
    assert result["item_count"] == 1
    item = result["items"][0]
    assert item["category"] == "听后记录并转述信息录音稿"
    assert item["index"] == 1
    assert item["text"].startswith("(W)Hello, I'm Amy Lee.")
    assert item["text"].endswith("takes it to his reading group.")
    assert "计算机语音" not in item["text"]
    assert "参考答案" not in item["text"]
    assert "Amy Lee helps make English name cards" not in item["text"]


def test_once_loader_and_auto_loader_are_equivalent_for_new_type():
    assert detect_doc_type(FIXTURE.name) == "听后记录并转述信息"
    assert parse_document_once(FIXTURE) == parse_document_auto(FIXTURE)


def test_new_type_maps_to_one_audio_only_stimulus():
    results, _ = parse_document_once(FIXTURE)
    candidate = extract_candidate(
        results[0]["doc_type"], results[0], FIXTURE.stem)

    assert candidate.type_code == "listening_record_retelling"
    assert candidate.audio_only is True
    assert candidate.diagnostics == ("audio_only_candidate",)
    assert len(candidate.entities) == 1
    stimulus = candidate.entities[0]
    assert stimulus.sub_type_code == "listening_record_retelling"
    assert stimulus.stimulus_type == "listening_script"
    assert stimulus.text.startswith("(W)Hello, I'm Amy Lee.")


def test_workflow_parser_normalizes_the_new_audio_item():
    parsed = LegacyWordParser().parse(FIXTURE)

    assert parsed.item_count == 1
    item = parsed.items[0]
    assert item.item_type == "听后记录并转述信息录音稿"
    assert item.metadata["doc_type"] == "听后记录并转述信息"
    assert item.normalized_content.startswith("(W)Hello, I'm Amy Lee.")


def test_progress_keeps_marked_script_as_one_audio_item_and_maps_gender():
    results, _ = parse_document_once(FIXTURE)
    progress = build_progress(FIXTURE.name, str(FIXTURE), results, {})

    assert progress["total_items"] == 1
    assert progress["items"][0]["filename"] == "听后记录并转述信息-录音稿1.mp3"

    segments = build_synthesis_segments(
        "(W)Female line.\n(M)Male line.", 50, 50, 50)
    assert [(segment["voice_key"], segment["text"]) for segment in segments] == [
        ("amanda", "Female line."),
        ("george", "Male line."),
    ]


def test_single_synthesis_path_never_submits_gender_markers_to_engine():
    calls = []

    async def fake_synth_segment(text, voice, rate, volume, pitch):
        calls.append((text, voice, rate, volume, pitch))
        return AudioSegment.silent(duration=100)

    async def run():
        with mock.patch.object(
            synthesis, "_synth_segment", side_effect=fake_synth_segment
        ):
            await synthesis._synth_item(
                "(W)Female line.\n(M)Male line.", 50, 50, 50
            )

    asyncio.run(run())
    assert [(call[0], call[1]) for call in calls] == [
        ("Female line.", "amanda"),
        ("Male line.", "george"),
    ]


def test_composite_fallback_also_strips_gender_markers_before_ui_input():
    from xunfei import XunFeiSession

    rows, _ = XunFeiSession._composite_ui_rows({
        "items": [{
            "item_id": "audio-1",
            "text": "(W)Female line.\n(M)Male line.",
        }],
    })

    assert [(row["text"], row["voice_key"]) for row in rows] == [
        ("Female line.", "amanda"),
        ("Male line.", "george"),
    ]


def test_parser_accepts_multiple_gender_markers_and_stops_at_controls():
    parser = ListeningRecordRetellingParser(
        "synthetic.docx",
        preloaded_paras=(
            [
                (0, "第一节 听后记录", "Normal"),
                (1, "(W)Female line.", "Normal"),
                (2, "(M)Male line.", "Normal"),
                (3, "（计算机语音和屏幕文字提示）现在，听短文两遍。", "Normal"),
                (4, "参考答案：Female line.", "Normal"),
                (5, "第二节：信息转述", "Normal"),
                (6, "1. Amy retells the information.", "Normal"),
            ],
            [
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
            ],
        ),
    )

    result = parser.parse()
    assert result["item_count"] == 1
    assert result["items"][0]["text"] == "(W)Female line.\n(M)Male line."


def test_parser_accepts_unmarked_script_and_keeps_inline_controls_out():
    parser = ListeningRecordRetellingParser(
        "synthetic.docx",
        preloaded_paras=(
            [
                (0, "第一节 听后记录", "Normal"),
                (1, "（计算机语音提示）现在，听短文两遍。", "Normal"),
                (
                    2,
                    "Hello, everyone. This is a listening script."
                    "（计算机语音提示）现在，请你在答题区域输入答案。",
                    "Normal",
                ),
                (3, "答题区域", "Normal"),
                (4, "【参考答案】 listening script", "Normal"),
                (5, "第二节：信息转述", "Normal"),
            ],
            [
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
                {"heading_hint": False},
            ],
        ),
    )

    result = parser.parse()
    assert result["item_count"] == 1
    assert result["items"][0]["text"] == (
        "Hello, everyone. This is a listening script."
    )
    segments = build_synthesis_segments(
        result["items"][0]["text"], 50, 50, 50
    )
    assert [(segment["voice_key"], segment["text"]) for segment in segments] == [
        ("amanda", "Hello, everyone. This is a listening script."),
    ]
