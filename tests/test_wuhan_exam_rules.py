"""Regression coverage for the two-section response and standalone retelling."""

from pathlib import Path
from xml.etree import ElementTree as ET

from docx import Document
from docx.shared import RGBColor
from PIL import Image

from application.workflow_service import WorkflowApplicationService
from question_types.segmenter import parse_document_once
from workflow.docx_table_image import _drawing_group_browser_value, _drawing_groups
from workflow.parser import DocumentParser
from workflow.system_input_content import document_entry_support


def _wuhan_document(tmp_path: Path) -> Path:
    image = tmp_path / "picture.png"
    Image.new("RGB", (32, 24), "#6c93bf").save(image)
    doc = Document()
    for line in (
        "二、听后应答（共3小题，每小题1分，满分3分）",
        "（计算机语音和屏幕文字提示）第一节",
        "（计算机语音提示）听下面一个句子。",
        "Look at the picture. What is it?",
        "（计算机语音提示）请看图并朗读你认为正确的应答语。",
        "（计算机屏幕显示5秒倒计时进度条）",
    ):
        doc.add_paragraph(line)
    row = doc.add_paragraph("15.★ Geography. ★ History. ★ ")
    row.add_run("Biology.").font.color.rgb = RGBColor(255, 0, 0)
    doc.add_picture(str(image))
    for line in (
        "（计算机语音和屏幕文字提示）第二节",
        "（计算机语音和屏幕文字提示）听对话，口头回答问题。你将有5秒钟的时间回答每一个问题。",
        "（计算机语音提示）听下面一段对话。",
        "【录音原文】",
        "W: What subject does Tom like?",
        "M: English.",
        "（计算机屏幕显示）Q: What is Tom's favourite subject?",
        "参考答案：",
        "English.",
        "He likes English.",
        "17.（计算机语音提示）听下面一段对话。",
        "【录音原文】",
        "W: Why does Peter like maths?",
        "M: It is important.",
        "（计算机屏幕显示）Q: Why does Peter like maths?",
        "参考答案：",
        "Because it is important.",
        "三、模仿朗读（共1题，满分5分）",
        "四、听后转述（共1题，满分5分）",
        "22.（计算机语音提示）听短文并完成信息转述。",
    ):
        doc.add_paragraph(line)
    doc.add_picture(str(image))
    for line in (
        "John wants to talk about his favourite subject.",
        "（计算机语音提示）请在90秒钟内完成转述。",
        "【录音原文】",
        "Hello, I am John. My favourite subject is IT.",
        "参考答案：",
        "Answer 1:",
        "John likes IT.",
        "Answer 2: IT is John's favourite subject.",
    ):
        doc.add_paragraph(line)
    source = tmp_path / "paper.docx"
    doc.save(source)
    return source


def test_two_section_response_and_retelling_keep_answers_and_images_separate(tmp_path):
    source = _wuhan_document(tmp_path)
    results, summary = parse_document_once(source)
    assert summary == "检测到 2 种题型，成功提取 4 条内容"
    by_type = {result["doc_type"]: result for result in results}
    response = by_type["听后应答"]
    assert [question["number"] for question in response["questions"]] == [15, 16, 17]
    assert response["questions"][1]["number_inferred"] is True
    assert [question["answer_time"] for question in response["questions"]] == [5, 5, 5]
    assert response["questions"][0]["answer"] == "C"
    assert len(response["questions"][0]["options"]) == 3
    assert response["questions"][0]["block_index"] == 0
    assert response["questions"][1]["prompt"] == "What is Tom's favourite subject?"
    assert response["questions"][1]["reference_answers"] == ["English.", "He likes English."]
    assert response["items"][1]["text"] == "W: What subject does Tom like?\nM: English."
    assert "English.\nHe likes English." not in response["items"][1]["text"]

    retelling = by_type["听后转述"]
    assert retelling["recording"]["block_kind"] == "embedded_image"
    assert retelling["recording"]["block_index"] == 1
    assert retelling["retelling"]["prompt"] == "John wants to talk about his favourite subject."
    assert retelling["retelling"]["answer_time"] == 90
    assert retelling["retelling"]["reference_answers"] == [
        "John likes IT.", "IT is John's favourite subject."
    ]
    assert retelling["items"][0]["text"] == "Hello, I am John. My favourite subject is IT."

    parsed = DocumentParser().parse(source)
    assert WorkflowApplicationService._block_image_requests(parsed) == [
        ("embedded_image", 0), ("embedded_image", 1)
    ]
    updated = WorkflowApplicationService._with_block_image_artifacts(
        parsed, {("embedded_image", 0): "image-question", ("embedded_image", 1): "image-retelling"}
    )
    response_fact = updated.items[0].metadata["page_input"]["questions"][0]
    assert response_fact["image_artifact_id"] == "image-question"
    assert updated.items[-1].metadata["page_input"]["recording"]["image_artifact_id"] == "image-retelling"
    assert updated.items[1].metadata["page_input"]["questions"][0]["number_inferred"] is True
    support = document_entry_support(
        [item.metadata.get("page_input") for item in updated.items],
        document_types=[item.metadata.get("doc_type") for item in updated.items],
    )
    assert support["supported"] is False
    assert support["review_supported"] is True


def test_new_paper_survives_managed_import_and_workspace_projection(tmp_path):
    from workflow.artifact_store import ArtifactStore
    from workflow.database import WorkflowDatabase
    from workflow.repositories import WorkflowRepository
    from workflow.source_imports import SourceImportService
    from workflow.system_input import SystemInputService

    source = _wuhan_document(tmp_path)
    database = WorkflowDatabase(tmp_path / "workflow.db", profile="full")
    database.initialize()
    store = ArtifactStore(tmp_path / "artifacts")
    repository = WorkflowRepository(database)
    service = WorkflowApplicationService(
        repository, SourceImportService(database, store), store, parser=DocumentParser(),
    )
    draft = service.create_draft("tts", {})
    imported = service.import_source(
        draft.workflow_id, source.read_bytes(), filename=source.name, request_key="wuhan-paper",
    )
    snapshot = repository.get_workflow(draft.workflow_id)
    result = service.parse(
        draft.workflow_id, expected_state_version=snapshot.state_version,
        source_artifact_id=imported["source_artifact_id"],
    )
    assert len(result["block_image_artifact_ids"]) == 2
    SystemInputService(database, repository).sync_projection(draft.workflow_id)
    projection = repository.get_workspace(draft.workflow_id)["system_input"]
    assert projection["input_type_status"] == "suggested"
    assert projection["document_entry_support"]["review_supported"] is True
    assert projection["document_entry_support"]["supported"] is False
    facts = [segment["page_input"] for segment in projection["content_segments"]]
    response = next(fact for fact in facts if fact["type"] == "听后应答"
                    and fact["questions"][0]["number"] == 15)
    retelling = next(fact for fact in facts if fact["type"] == "听后转述")
    assert response["questions"][0]["image_artifact_id"] in result["block_image_artifact_ids"]
    assert retelling["recording"]["image_artifact_id"] in result["block_image_artifact_ids"]
    assert len(retelling["retelling"]["reference_answers"]) == 2
    database.close()


def test_missing_number_is_not_inferred_when_neighbouring_numbers_conflict(tmp_path):
    source = _wuhan_document(tmp_path)
    doc = Document(source)
    for paragraph in doc.paragraphs:
        if paragraph.text.startswith("17.（计算机语音提示）"):
            paragraph.text = paragraph.text.replace("17.", "19.", 1)
            break
    doc.save(source)

    results, _ = parse_document_once(source)
    response = next(result for result in results if result["doc_type"] == "听后应答")
    assert response["questions"][1].get("number") is None
    assert response["questions"][2]["number"] == 19
    parsed = DocumentParser().parse(source)
    second = parsed.items[1].metadata["page_input"]["questions"]
    assert len(second) == 1
    assert "number" not in second[0]
    assert second[0]["prompt"] == "What is Tom's favourite subject?"


def test_missing_number_requires_more_than_one_number_anchor(tmp_path):
    source = _wuhan_document(tmp_path)
    doc = Document(source)
    for paragraph in doc.paragraphs:
        if paragraph.text.startswith("17.（计算机语音提示）"):
            paragraph.text = paragraph.text.replace("17.", "", 1)
            break
    doc.save(source)

    results, _ = parse_document_once(source)
    response = next(result for result in results if result["doc_type"] == "听后应答")
    assert [question.get("number") for question in response["questions"]] == [15, None, None]


def test_answer_label_after_reference_marker_is_not_part_of_the_answer(tmp_path):
    source = _wuhan_document(tmp_path)
    doc = Document(source)
    reference_markers = [paragraph for paragraph in doc.paragraphs if paragraph.text == "参考答案："]
    reference_markers[-1].text = "参考答案：Answer 1: John likes IT."
    for paragraph in doc.paragraphs:
        if paragraph.text in {"Answer 1:", "John likes IT."}:
            paragraph.text = ""
    doc.save(source)

    results, _ = parse_document_once(source)
    retelling = next(result for result in results if result["doc_type"] == "听后转述")
    assert retelling["retelling"]["reference_answers"] == [
        "John likes IT.", "IT is John's favourite subject."
    ]


def test_dialogue_audio_survives_missing_screen_question_and_plain_section_title(tmp_path):
    source = _wuhan_document(tmp_path)
    doc = Document(source)
    for paragraph in doc.paragraphs:
        if paragraph.text.endswith("第二节"):
            paragraph.text = "第二节"
        if "What is Tom's favourite subject?" in paragraph.text:
            paragraph.text = "（计算机屏幕显示）"
    doc.save(source)

    parsed = DocumentParser().parse(source)
    second = parsed.items[1]
    assert second.normalized_content == "W: What subject does Tom like?\nM: English."
    question = second.metadata["page_input"]["questions"][0]
    assert "prompt" not in question
    assert question["reference_answers"] == ["English.", "He likes English."]


def test_response_does_not_claim_image_from_a_later_major_section(tmp_path):
    from question_types.listening_response import ListeningResponseParser

    picture = tmp_path / "other-section.png"
    Image.new("RGB", (16, 16), "blue").save(picture)
    doc = Document()
    for line in (
        "二、听后应答（每小题1分）", "（计算机语音提示）听下面一个句子。",
        "Hello.", "（计算机语音提示）请朗读应答语。", "1.★ Hi. ★ Goodbye.",
        "三、模仿朗读（满分5分）",
    ):
        doc.add_paragraph(line)
    doc.add_picture(str(picture))
    source = tmp_path / "later-image.docx"
    doc.save(source)
    question = ListeningResponseParser(source).parse()["questions"][0]
    assert "block_image_required" not in question


def test_question_images_do_not_enable_an_older_automatic_entry_flow():
    from copy import deepcopy

    source = Path(__file__).resolve().parents[1] / "examples/documents/七上Starter Unit 1 听说测试题（2026新题型）.docx"
    parsed = DocumentParser().parse(source)
    metadata = [item.metadata for item in parsed.items]
    facts = [deepcopy(row.get("page_input")) for row in metadata]
    kwargs = {
        "document_types": [row.get("doc_type") for row in metadata],
        "major_section_profiles": [row.get("major_section_profile") for row in metadata],
        "entry_profiles": [row.get("entry_profile") for row in metadata],
        "capabilities": [row.get("capabilities") for row in metadata],
    }
    assert document_entry_support(facts, **kwargs)["supported"] is True
    response = next(fact for fact in facts if fact["type"] == "听后应答")
    response["questions"][0]["block_image_required"] = True
    response["questions"][0]["block_kind"] = "embedded_image"
    response["questions"][0]["block_index"] = 0
    support = document_entry_support(facts, **kwargs)
    assert support["supported"] is False
    assert "配图" in support["reason"]


def test_rotated_bracket_and_zero_width_connector_have_correct_capture_bounds():
    root = ET.fromstring('''
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
        xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
        xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
      <w:body><w:p><w:r><w:drawing>
        <wp:anchor><wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>
          <wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>
          <wp:extent cx="0" cy="127000"/><wp:docPr name="line"/>
          <a:graphic><a:graphicData><wps:wsp><wps:spPr><a:xfrm/>
            <a:prstGeom prst="line"/></wps:spPr></wps:wsp></a:graphicData></a:graphic>
        </wp:anchor>
        <wp:anchor><wp:positionH relativeFrom="column"><wp:posOffset>635000</wp:posOffset></wp:positionH>
          <wp:positionV relativeFrom="paragraph"><wp:posOffset>-571500</wp:posOffset></wp:positionV>
          <wp:extent cx="127000" cy="1270000"/><wp:docPr name="bracket"/>
          <a:graphic><a:graphicData><wps:wsp><wps:spPr><a:xfrm rot="16200000"/>
            <a:prstGeom prst="rightBracket"/></wps:spPr></wps:wsp></a:graphicData></a:graphic>
        </wp:anchor>
      </w:drawing></w:r></w:p></w:body>
    </w:document>''')
    groups = _drawing_groups(root, {})
    assert len(groups) == 1
    assert groups[0].shapes[0].width == 0.5
    assert groups[0].shapes[1].rotation == 270
    capture = _drawing_group_browser_value(groups[0], padding_points=0)
    assert 100 <= capture["width"] < 110
    assert 10 <= capture["height"] < 20
