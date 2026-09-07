"""旧题型整套试卷的解析回归测试。"""

from collections import Counter
from pathlib import Path

import pytest

from audio_naming import is_exam_paper_bundle
from platform_entry.adapter.normalizers.legacy_exam import normalise_info_acquisition_group
from question_types.segmenter import load_document_once, parse_document_once
from question_model import extract_candidate
from wordtts.progress import build_progress
from workflow.parser import DocumentParser


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


def test_legacy_info_acquisition_normalizer_builds_inline_prompt_and_keeps_prompt_audio(tmp_path):
    original_audio = tmp_path / "original.mp3"
    prompt_audio = tmp_path / "prompt.mp3"
    original_audio.write_bytes(b"original")
    prompt_audio.write_bytes(b"prompt")

    normalized = normalise_info_acquisition_group(
        {
            "materials": [
                {
                    "audio_path": str(original_audio),
                    "listening_text": "source",
                    "questions": [
                        {
                            "prompt": "How many subjects does Mary have at school?",
                            "options": [
                                {"option_id": "A", "text": "Four."},
                                {"option_id": "B", "text": "Five."},
                                {"option_id": "C", "text": "Six."},
                            ],
                            "score": 1.5,
                            "answer": "B",
                            "prompt_audio_path": str(prompt_audio),
                        }
                    ],
                }
            ]
        },
        0,
        base_dir=None,
    )

    question = normalized["materials"][0]["questions"][0]
    assert question["prompt"] == "How many subjects does Mary have at school? (Four. / Five. / Six.)"
    assert question["text"] == question["prompt"]
    assert question["prompt_audio_path"] == str(prompt_audio.resolve())


@pytest.mark.skipif(not FIXTURE.exists(), reason="旧题型套卷样例未随工作区提供")
def test_legacy_exam_bundle_is_parsed_without_cross_section_leaks():
    results, summary = parse_document_once(FIXTURE)

    assert summary == "检测到 3 种题型，成功提取 16 条内容"
    assert [result["doc_type"] for result in results] == [
        "模仿朗读",
        "信息获取",
        "信息转述及询问",
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

    imitation = by_type["模仿朗读"]
    imitation_item = next(
        item for item in imitation["items"]
        if item["category"] == "模仿朗读-试卷正文"
    )
    assert imitation_item["reference_answers"] == [imitation_item["text"]]
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
        "模仿朗读-1.mp3",
        "听选信息题目-1.mp3",
        "听选信息题目-2.mp3",
        "听选信息-1.mp3",
        "听选信息题目-3.mp3",
        "听选信息题目-4.mp3",
        "听选信息-2.mp3",
        "听选信息题目-5.mp3",
        "听选信息题目-6.mp3",
        "听选信息-3.mp3",
        "回答问题题目-1.mp3",
        "回答问题题目-2.mp3",
        "回答问题题目-3.mp3",
        "回答问题题目-4.mp3",
        "回答问题-1.mp3",
        "信息转述-1.mp3",
    ]

    questions = [
        item for item in info_items if item["category"].endswith("题目")
    ]
    assert questions[0]["text"] == "How many subjects does Mary have at school? (Four. / Five. / Six.)"
    assert questions[0]["voice"] == "male"
    assert not questions[0]["text"].startswith(("M:", "W:"))

    semantic_questions = by_type["信息获取"]["questions"]
    assert semantic_questions[0]["reference_answers_source"] == "document"
    assert semantic_questions[0]["reference_answers"] == [
        "Five.",
        "Five subjects.",
        "She has five subjects.",
        "Mary has five subjects.",
        "She has five subjects at school.",
        "Mary has five subjects at school.",
    ]
    assert semantic_questions[1]["reference_answers_source"] == "document"
    assert semantic_questions[1]["reference_answers"] == [
        "Science.",
        "He likes science best.",
        "Bill likes science best.",
        "His favourite subject is science.",
        "Bill’s favourite subject is science.",
    ]

    parsed = DocumentParser().parse(FIXTURE)
    assert parsed.item_count == 16
    assert Counter(item.item_type for item in parsed.items) == Counter({
        "听选信息题目": 6,
        "听选信息录音稿": 3,
        "回答问题题目": 4,
        "回答问题录音稿": 1,
        "信息转述录音稿": 1,
        "模仿朗读-试卷正文": 1,
    })



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


@pytest.mark.skipif(not FIXTURE.exists(), reason="旧题型套卷样例未随工作区提供")
def test_legacy_page_facts_separate_tts_markers_from_visible_text_and_split_answers():
    parsed = DocumentParser().parse(FIXTURE, include_auxiliary_audio=True)

    response_item = next(item for item in parsed.items if item.item_type == "回答问题录音稿")
    response_page = response_item.metadata["page_input"]
    response_text = response_page["materials"][0]["listening_text"]
    assert "(M)" not in response_text
    assert response_text.startswith("Hello, everyone!")

    retelling_item = next(item for item in parsed.items if item.item_type == "信息转述录音稿")
    retelling_page = retelling_item.metadata["page_input"]
    retelling_text = retelling_page["recording"]["listening_text"]
    assert "(W)" not in retelling_text
    assert retelling_text.startswith("Good morning, everyone!")
    assert retelling_page["retelling"]["prompt"] == (
        "你可以这样开始：Let me tell you about Li Ling."
    )
    assert len(retelling_page["retelling"]["reference_answers"]) == 4
    assert retelling_page["recording"]["asking_instruction_text"].startswith(
        "你希望了解更多关于Li Ling的信息"
    )
    assert retelling_page["recording"]["asking_instruction_audio_filename_stem"] == (
        "询问信息题干-1"
    )

    info_recording_items = [
        item for item in parsed.items
        if item.item_type in {"听选信息录音稿", "回答问题录音稿"}
    ]
    assert [
        item.metadata["page_input"]["materials"][0]["section"]
        for item in info_recording_items
    ] == ["听选信息", "听选信息", "听选信息", "回答问题"]
    for item in info_recording_items:
        visible_text = item.metadata["page_input"]["materials"][0]["listening_text"]
        assert "(W)" not in visible_text
        assert "(M)" not in visible_text
    assert info_recording_items[0].metadata["page_input"]["materials"][0]["listening_text"].startswith(
        "M: What subjects do you have at school, Mary?"
    )
    assert info_recording_items[1].metadata["page_input"]["materials"][0]["listening_text"].startswith(
        "W: Hi, Jack."
    )
    assert info_recording_items[2].metadata["page_input"]["materials"][0]["listening_text"].startswith(
        "M: Amy, what are you going to wear"
    )

    asking = retelling_page["asking"]
    assert [item["number"] for item in asking] == [11, 12]
    assert asking[0]["reference_answers"] == [
        "What colour do you like best?",
        "What’s your favourite colour?",
        "What is your favourite colour?",
    ]
    assert asking[1]["reference_answers"] == [
        "How many classrooms are there in your new school?",
        "How many classrooms does your new school have?",
    ]

    question_audio = next(item for item in parsed.items if item.item_type == "听选信息题目")
    assert question_audio.normalized_content == (
        "How many subjects does Mary have at school? (Four. / Five. / Six.)"
    )
