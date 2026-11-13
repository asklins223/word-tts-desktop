from pathlib import Path

from docx import Document
from PIL import Image

from question_model import extract_candidate
from question_types.segmenter import parse_document_once
from workflow.parser import DocumentParser
from workflow.system_input_content import page_input_completeness


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/documents/信息转述及询问信息 7上- U1.docx"


def test_info_retelling_marks_the_positioned_drawing_group():
    results, summary = parse_document_once(FIXTURE)

    assert summary == "检测到 1 种题型，成功提取 1 条内容"
    assert len(results) == 1
    recording = results[0]["recording"]
    assert recording["block_image_required"] is True
    assert recording["block_kind"] == "drawing_group"
    assert recording["block_index"] == 0

def test_workflow_page_facts_keep_the_drawing_selector():
    parsed = DocumentParser().parse(FIXTURE)
    recordings = [item.metadata["page_input"]["recording"] for item in parsed.items]

    assert [recording["block_image_required"] for recording in recordings] == [True]
    assert [recording["block_kind"] for recording in recordings] == ["drawing_group"]
    assert [recording["block_index"] for recording in recordings] == [0]
    assert "(W)" not in recordings[0]["listening_text"]
    assert parsed.items[0].metadata["page_input"]["asking"][0]["reference_answers"] == [
        "What problem may you have with your plan?"
    ]
    recording = parsed.items[0].metadata["page_input"]["recording"]
    assert recording["asking_instruction_text"].startswith("你希望了解更多")
    assert recording["asking_instruction_occurrence"] == 0
    assert "asking_instruction_audio_filename_stem" not in recording


def test_retelling_selector_prefers_inline_image_in_its_subsection(tmp_path):
    source = tmp_path / "inline-retelling.docx"
    image_source = tmp_path / "mind-map.png"
    Image.new("RGB", (32, 24), "#2f7e69").save(image_source, format="PNG")
    document = Document()
    document.add_paragraph("第一节 信息转述")
    document.add_paragraph("请根据思维导图复述。")
    document.add_picture(str(image_source))
    document.add_paragraph("录音稿：My favourite hobby is reading.")
    document.add_paragraph("第二节 询问信息")
    document.save(source)

    parsed = DocumentParser().parse(source)
    recording = parsed.items[0].metadata["page_input"]["recording"]
    assert (recording["block_kind"], recording["block_index"]) == ("embedded_image", 0)


def test_workflow_preserves_three_guide_audio_items_and_entry_facts(tmp_path):
    source = tmp_path / "three-guides.docx"
    document = Document()
    intro = "你将听到一篇 Peter 讲述去阳朔旅行的短文，录音播放两遍。"
    reading = "现在，你有15秒钟的时间阅读思维导图。"
    preparation = "下面你有60秒钟的时间准备，你的复述可以这样开始：Peter visited Yangshuo ..."
    for text in (
        "第一节 短文复述（共8分）", intro, reading, preparation,
        "听力原文：", "I'm Peter. I visited Yangshuo last August.",
        "参考答案：", "Peter visited Yangshuo last August.",
    ):
        document.add_paragraph(text)
    document.save(source)

    parsed = DocumentParser().parse(source, include_auxiliary_audio=True)
    guides = [item for item in parsed.items if item.metadata.get("audio_only_auxiliary")]
    assert [item.normalized_content for item in guides] == [intro, reading, preparation]
    assert len(parsed.items) == 4
    recording = parsed.items[0].metadata["page_input"]["recording"]
    assert recording["reading_instruction_text"] == reading
    assert recording["reading_instruction_audio_filename_stem"] == "信息转述阅图指导文字-1"
    assert recording["reading_instruction_occurrence"] == 1
    assert all(item.role == "题干音色" for item in guides)


def test_multiline_answer_blocks_keep_numbered_rows_apart(tmp_path):
    """A DOCX paragraph may carry several numbered answer rows in one run.

    The two attached papers put all retelling versions in one 参考答案
    paragraph and the 11/12 asking answers in another.  Each row must keep
    its own number instead of being appended to the recording text or being
    paired with the wrong question as a plain answer.
    """

    source = tmp_path / "multiline-answers.docx"
    document = Document()
    retelling_answers = "\n".join(
        f"{index}. Jack wants to talk about version {index}."
        for index in range(1, 4)
    )
    asking_answers = (
        "11. When was Helen born? / What year was Helen born?\n"
        "12. Which college did Helen graduate from? / What college did she graduate from?"
    )
    for text in (
        "第一节 信息转述（6 分）",
        "你将听到 Jack 介绍海伦凯勒的故事，录音播放两遍。请根据所听到的内容转述。",
        "下面你有 50 秒钟的时间准备。你的介绍可以这样开始：Jack wants to talk about Helen Keller.",
        "录音稿：Helen Keller lost her sight at 19 months old.\n参考答案：" + retelling_answers,
        "第二节 询问信息（3 分）",
        "你希望了解更多，请根据以下提示向提问者提两个问题。",
        "11. 海伦是哪一年出生的？",
        "12. 海伦毕业于哪所大学？",
        "参考答案：",
        asking_answers,
    ):
        document.add_paragraph(text)
    document.save(source)

    results, _summary = parse_document_once(source)
    result = results[0]

    assert result["recording"]["listening_text"] == (
        "Helen Keller lost her sight at 19 months old."
    )
    assert result["retelling"]["reference_answers"] == [
        f"Jack wants to talk about version {index}." for index in range(1, 4)
    ]
    assert result["asking"][0]["reference_answers"] == [
        "When was Helen born?",
        "What year was Helen born?",
    ]
    assert result["asking"][1]["reference_answers"] == [
        "Which college did Helen graduate from?",
        "What college did she graduate from?",
    ]

    parsed = DocumentParser().parse(source)
    facts = parsed.items[0].metadata["page_input"]
    assert page_input_completeness(facts) == {"status": "complete", "reason": None}


def test_repeated_materials_keep_own_answers_diagrams_and_audio(tmp_path):
    document = Document()
    for index, name in enumerate(("Mia", "Jack"), start=1):
        diagram = tmp_path / f"diagram-{index}.png"
        Image.new("RGB", (32, 24), ("blue" if index == 1 else "red")).save(diagram)
        for text in ("三、信息转述及询问（共9分）", "第一节 信息转述（6分）",
                     f"请根据思维导图转述 {name} 的故事。"):
            document.add_paragraph(text)
        document.add_picture(str(diagram))
        for text in (
            f"你可以这样开始：Let me tell you about {name}.",
            f"录音稿：My name is {name}.\n参考答案：1. This is {name}.\n2. It is {name}.",
            "第二节 询问信息（3分）",
            f"请向 {name} 提两个问题。",
            "11. 你最喜欢什么？",
            "12. 你住在哪里？",
            f"参考答案：11. What do you like, {name}?\n12. Where do you live, {name}?",
        ):
            document.add_paragraph(text)
    source = tmp_path / "two-retellings.docx"
    document.save(source)

    parsed = DocumentParser().parse(source, include_auxiliary_audio=True)
    materials = [item for item in parsed.items if item.item_type == "信息转述录音稿"]
    assert [item.normalized_content for item in materials] == ["My name is Mia.", "My name is Jack."]
    assert len({item.identity_key for item in parsed.items}) == parsed.item_count
    for index, (item, name) in enumerate(zip(materials, ("Mia", "Jack"))):
        facts = item.metadata["page_input"]
        assert facts["recording"]["block_index"] == index
        assert facts["retelling"]["reference_answers"] == [f"This is {name}.", f"It is {name}."]
        assert [question["number"] for question in facts["asking"]] == [11, 12]
        assert [question["reference_answers"] for question in facts["asking"]] == [
            [f"What do you like, {name}?"], [f"Where do you live, {name}?"],
        ]
        audio = [clip for clip in parsed.items if clip.metadata["unit_id"] == item.metadata["unit_id"]]
        prompt, = [clip for clip in audio if clip.item_type == "信息转述题干"]
        assert prompt.metadata["audio_filename_stem"] == facts["retelling"]["prompt_audio_filename_stem"]
        assert name in prompt.normalized_content

    result, = parsed.source_parse.results
    candidate = extract_candidate(result["doc_type"], result, "two-retellings")
    questions = [entity for entity in candidate.entities if hasattr(entity, "question_type")]
    assert len({question.question_id for question in questions}) == 6
    for name in ("Mia", "Jack"):
        material, = [entity for entity in candidate.entities if getattr(entity, "text", "") == f"My name is {name}."]
        linked = [question for question in questions if question.stimulus_id == material.stimulus_id]
        assert len(linked) == 3
        assert all(name in question.answer.value for question in linked)


def test_ambiguous_answers_point_to_the_question_without_guessing(tmp_path):
    source = tmp_path / "ambiguous-key.docx"
    document = Document()
    for text in (
        "第一节 信息转述（6分）", "请听短文并转述。",
        "起始语：Let me tell you about Mia.", "录音稿：My name is Mia.",
        "参考答案：This is Mia.", "第二节 询问信息（3分）", "请根据提示提问。",
        "11. 她喜欢什么？", "12. 她住在哪里？",
        "参考答案：11. What does she like? / What is her hobby?",
        "Where does she live?",
    ):
        document.add_paragraph(text)
    document.save(source)

    material = DocumentParser().parse(source).items[0]
    facts = material.metadata["page_input"]
    assert len(facts["asking"][0]["reference_answers"]) == 2
    assert not facts["asking"][1].get("reference_answers")
    readiness = page_input_completeness(facts)
    assert readiness["status"] == "incomplete"
    assert "第 9 段/询问信息/第 12 题" in readiness["reason"]
