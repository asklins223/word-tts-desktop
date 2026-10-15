from pathlib import Path

from docx import Document
from PIL import Image

from question_types.info_retelling import InfoRetellingParser
from question_types.segmenter import parse_document_once
from question_types.text_utils import load_paragraphs
from workflow.parser import DocumentParser


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
    assert recording["asking_instruction_audio_filename_stem"] == "询问信息题干-1"


def test_retelling_sections_keep_distinct_group_indexes_before_deduplication():
    loaded = load_paragraphs(FIXTURE, include_metadata=True, include_blocks=True)
    parser = InfoRetellingParser(FIXTURE, preloaded_paras=loaded)

    assert parser._recording_drawing_group_indices() == (0, 1)


def test_retelling_selector_prefers_inline_image_in_its_subsection(tmp_path):
    source = tmp_path / "inline-retelling.docx"
    image_source = tmp_path / "mind-map.png"
    Image.new("RGB", (32, 24), "#2f7e69").save(image_source, format="PNG")
    document = Document()
    document.add_paragraph("第一节 信息转述")
    document.add_paragraph("请根据思维导图复述。")
    document.add_picture(str(image_source))
    document.add_paragraph("第二节 询问信息")
    document.save(source)

    loaded = load_paragraphs(source, include_metadata=True, include_blocks=True)
    parser = InfoRetellingParser(str(source), preloaded_paras=loaded)

    assert parser._recording_block_image_selector() == ("embedded_image", (0,))
