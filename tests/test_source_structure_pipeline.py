"""All families share source ownership, identity, and projection boundaries."""

from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document
from docx.shared import RGBColor

from question_model import extract_candidate
from question_types import PARSERS_BY_FAMILY
from question_types.segmenter import parse_source
from workflow.parser import DocumentParser
from workflow.system_input_content import page_input_completeness


DOCX_FAMILIES = tuple(code for code in PARSERS_BY_FAMILY if code != "vocabulary")


def _append_family(document, family, label):
    """Create source content, including colors/tables rather than parser dicts."""
    name = PARSERS_BY_FAMILY[family].DOC_TYPE
    document.add_paragraph(f"一、{name}")
    if family == "listening_choice":
        rows = [
            "（计算机语音提示）听下面一段对话，回答第1小题。现在，你有5秒钟的时间阅读这一小题。",
            f"1. Which colour does {label} like?", "A. Red.", "B. Blue.",
            "【录音原文】", f"W: {label} likes red.",
        ]
    elif family == "listening_response":
        rows = [
            "（计算机语音提示）听下面1个句子。", f"Hello, {label}.",
            "（计算机语音提示）请朗读应答语。", f"1. ★ Hello, {label}. ★ Goodbye.",
        ]
    elif family == "info_acquisition":
        rows = [
            "第一节 听选信息", f"1. What colour does {label} like?",
            "( Red. / Blue. )", f"参考答案：Red./ {label} likes red.",
            "录音稿：", f"W: {label} likes red.",
        ]
    elif family == "info_retelling":
        rows = [
            "第一节 信息转述（满分6分）", "请听短文并转述。",
            f"你的转述可以这样开始：{label} likes red.",
            f"录音稿：W: {label} likes red.", f"参考答案：{label} likes red.",
            "第二节 询问信息（每小题1分）", "请根据提示提问。",
            "11. 你想知道对方喜欢什么颜色。", f"参考答案：11. What colour does {label} like?",
        ]
    elif family == "listening_record_retelling":
        for value in [
            "第一节 听后记录（每小题1分）", "请根据短文完成信息记录表，一空限填一词。",
            "现在，你有60秒钟的时间浏览信息记录表格。", f"(W) {label} likes red.",
        ]:
            document.add_paragraph(value)
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Name"
        table.cell(0, 1).text = "17. ______"
        rows = [
            f"【参考答案】17. {label}", "第二节：信息转述（满分5分）",
            "20. 请根据所听短文进行转述。", f"{label} likes red._______",
            f"参考答案：{label} likes red.",
        ]
    elif family == "listening_retelling":
        rows = [
            "22. 请听短文两遍，完成转述。满分5分。",
            f"你的转述可以这样开始：{label} likes red.",
            f"录音稿：W: {label} likes red.", f"参考答案：{label} likes red.",
        ]
    elif family == "imitation_reading":
        rows = [
            "16. 请听以下短文两遍，听完后你有90秒钟的时间准备，然后模仿朗读。",
            f"{label} likes red. We like blue.",
        ]
    else:
        rows = ["Understanding Idea", "句子跟读", f"1. {label} likes red.", "中文：他喜欢红色。"]
    for value in rows:
        paragraph = document.add_paragraph(value)
        if value.startswith("A.") or "★" in value:
            paragraph.runs[0].font.color.rgb = RGBColor(255, 0, 0)


@pytest.mark.parametrize("family", DOCX_FAMILIES)
def test_repeated_family_keeps_source_identity_and_materials(tmp_path, family):
    document = Document()
    _append_family(document, family, "Amber")
    separator = "listening_choice" if family == "text_reading" else "text_reading"
    _append_family(document, separator, "Silver")
    _append_family(document, family, "Cobalt")
    source = tmp_path / "mixed.docx"
    document.save(source)

    parsed = DocumentParser().parse(source, include_auxiliary_audio=True)
    results = [r for r in parsed.source_parse.results if r["doc_type"] == PARSERS_BY_FAMILY[family].DOC_TYPE]
    assert len(results) == 2
    assert "Amber" in " ".join(item["text"] for item in results[0]["items"])
    assert "Cobalt" in " ".join(item["text"] for item in results[1]["items"])
    assert all("Silver" not in item["text"] for result in results for item in result["items"])
    assert results[0]["source_range"] != results[1]["source_range"]
    assert len({item.identity_key for item in parsed.items}) == len(parsed.items)

    candidates = [extract_candidate(result["doc_type"], result, "mixed") for result in results]
    entity_ids = [
        next(value for key, value in entity.to_dict().items()
             if key in {"stimulus_id", "question_id", "content_unit_id"})
        for candidate in candidates for entity in candidate.entities
    ]
    assert len(set(entity_ids)) == len(entity_ids)
    assert all(result["source_range"] in entity.source_locator
               for result, candidate in zip(results, candidates) for entity in candidate.entities)

    for result, expected, other in ((results[0], "Amber", "Cobalt"), (results[1], "Cobalt", "Amber")):
        candidate = extract_candidate(result["doc_type"], result, "mixed")
        content = str([entity.to_dict() for entity in candidate.entities])
        assert expected in content
        assert other not in content


@pytest.mark.parametrize("family", DOCX_FAMILIES)
def test_empty_earlier_section_does_not_hide_a_later_family(tmp_path, family):
    document = Document()
    document.add_paragraph(f"一、{PARSERS_BY_FAMILY[family].DOC_TYPE}")
    separator = "listening_choice" if family == "text_reading" else "text_reading"
    _append_family(document, separator, "Silver")
    _append_family(document, family, "Cobalt")
    source = tmp_path / "partially-filled.docx"
    document.save(source)

    parsed = parse_source(source)
    results = [r for r in parsed.results if r["doc_type"] == PARSERS_BY_FAMILY[family].DOC_TYPE]
    assert results
    assert any("Cobalt" in item["text"] for result in results for item in result["items"])
    assert parsed.issues
    assert "第 1" in parsed.issues[0]


def test_record_context_ends_at_another_family(tmp_path):
    document = Document()
    _append_family(document, "listening_record_retelling", "Amber")
    _append_family(document, "listening_choice", "Silver")
    document.add_paragraph("第二节 信息转述（满分6分）")
    document.add_paragraph("请听短文并转述。")
    document.add_paragraph("你的转述可以这样开始：Cobalt likes blue.")
    document.add_paragraph("录音稿：W: Cobalt likes blue.")
    document.add_paragraph("参考答案：Cobalt likes blue.")
    source = tmp_path / "context-boundary.docx"
    document.save(source)

    parsed = parse_source(source)
    assert [result["doc_type"] for result in parsed.results] == [
        "听后记录并转述信息", "听后选择", "信息转述及询问",
    ]
    assert parsed.results[-1]["items"][0]["text"] == "W: Cobalt likes blue."


def test_acquisition_number_restarts_keep_own_answers_and_prompt_audio(tmp_path):
    document = Document()
    _append_family(document, "info_acquisition", "Amber")
    for text in (
        "第二节 回答问题", "1. What colour does Cobalt like?",
        "参考答案：Blue./ Cobalt likes blue.", "录音稿：W: Cobalt likes blue.",
    ):
        document.add_paragraph(text)
    source = tmp_path / "restarted-subsections.docx"
    document.save(source)

    parsed = DocumentParser().parse(source)
    facts = [item.metadata["page_input"] for item in parsed.items if item.metadata.get("page_input")]
    first, second = [fact["materials"][0]["questions"][0] for fact in facts]
    assert first["reference_answers"] == ["Red.", "Amber likes red."]
    assert second["reference_answers"] == ["Blue.", "Cobalt likes blue."]
    assert first["prompt_audio_filename_stem"] != second["prompt_audio_filename_stem"]
    [result] = parsed.source_parse.results
    candidate = extract_candidate(result["doc_type"], result, "subsections")
    questions = [entity for entity in candidate.entities if hasattr(entity, "question_type")]
    assert [question.answer.alternatives for question in questions] == [
        ("Red.", "Amber likes red."), ("Blue.", "Cobalt likes blue."),
    ]


@pytest.mark.parametrize("family", ["info_acquisition", "listening_choice"])
def test_question_numbers_can_restart_for_another_material(tmp_path, family):
    document = Document()
    _append_family(document, family, "Amber")
    document.add_paragraph("1. Which colour does Cobalt like?")
    if family == "info_acquisition":
        document.add_paragraph("( Blue. / Red. )")
        document.add_paragraph("参考答案：Blue./ Cobalt likes blue.")
    else:
        correct = document.add_paragraph("A. Blue.")
        correct.runs[0].font.color.rgb = RGBColor(255, 0, 0)
        document.add_paragraph("B. Red.")
    document.add_paragraph("录音稿：W: Cobalt likes blue.")
    source = tmp_path / "material-number-restarts.docx"
    document.save(source)

    parsed = DocumentParser().parse(source)
    [result] = parsed.source_parse.results
    candidate = extract_candidate(result["doc_type"], result, "materials")
    questions = [entity for entity in candidate.entities if hasattr(entity, "question_type")]
    assert len(questions) == 2
    assert len({question.question_id for question in questions}) == 2
    assert len({question.stimulus_id for question in questions}) == 2
    assert all(question.answer.value == "A" for question in questions)
    stems = [item.metadata.get("audio_filename_stem") or item.metadata.get("filename_stem")
             for item in parsed.items]
    stems = [stem for stem in stems if stem]
    assert len(stems) == len(set(stems))


def test_all_word_families_can_share_one_source_without_leaking(tmp_path):
    document = Document()
    for family in reversed(DOCX_FAMILIES):
        _append_family(document, family, family.replace("_", " ").title())
    source = tmp_path / "all-families.docx"
    document.save(source)

    parsed = parse_source(source)
    assert parsed.detected_family_codes == set(DOCX_FAMILIES)
    assert [result["doc_type"] for result in parsed.results] == [
        PARSERS_BY_FAMILY[family].DOC_TYPE for family in reversed(DOCX_FAMILIES)
    ]
    assert all(result["item_count"] for result in parsed.results)


@pytest.mark.parametrize("family", ["info_acquisition", "listening_record_retelling"])
def test_restarted_subsections_start_a_new_owned_material_range(tmp_path, family):
    document = Document()
    _append_family(document, family, "Amber")
    first_element = len(document.element.body) - 1
    _append_family(document, family, "Cobalt")
    document.element.body.remove(document.element.body[first_element])
    source = tmp_path / "restarted-sections.docx"
    document.save(source)

    parsed = parse_source(source)

    assert not parsed.issues
    assert len(parsed.results) == 2
    assert "Amber" in str(parsed.results[0]["items"])
    assert "Cobalt" not in str(parsed.results[0]["items"])
    assert "Cobalt" in str(parsed.results[1]["items"])
    assert "Amber" not in str(parsed.results[1]["items"])


def test_every_registered_family_uses_one_physical_document_read():
    import openpyxl
    import question_types.segmenter as segmenter

    root = Path(__file__).resolve().parents[1] / "examples" / "documents"
    covered = set()
    for source in sorted(root.iterdir()):
        if source.suffix not in {".docx", ".xlsx"}:
            continue
        if source.suffix == ".xlsx":
            target, name = openpyxl, "load_workbook"
        else:
            target, name = segmenter, "load_document_once"
        with patch.object(target, name, wraps=getattr(target, name)) as read:
            parsed = DocumentParser().parse(source)
        assert read.call_count == 1, source.name
        covered.update(parsed.source_parse.detected_family_codes)
        assert "source_parse" not in parsed.as_dict()
    assert covered == set(PARSERS_BY_FAMILY)


def test_repeated_imitation_specials_keep_separate_input_units(tmp_path):
    document = Document()
    for label in ("Amber", "Cobalt"):
        _append_family(document, "imitation_reading", label)
        document.add_paragraph("17. 请听以下短文两遍，然后模仿朗读。")
        document.add_paragraph(f"{label} has another story.")
        _append_family(document, "text_reading", "Silver")
    source = tmp_path / "repeated-imitation-specials.docx"
    document.save(source)

    parsed = DocumentParser().parse(source)
    imitation = [item for item in parsed.items if item.metadata["doc_type"] == "模仿朗读"]

    assert len(imitation) == 4
    assert len({item.metadata["unit_id"] for item in imitation}) == 4
    assert len({item.identity_key for item in imitation}) == 4
    assert [item.metadata["unit_label"] for item in imitation] == [
        "第16题专项卷", "第17题专项卷", "第16题专项卷", "第17题专项卷",
    ]


def test_retelling_accepts_answers_beneath_each_asking_question(tmp_path):
    document = Document()
    _append_family(document, "info_retelling", "Amber")
    document.add_paragraph("12. 你想知道对方的名字。")
    document.add_paragraph("参考答案：12. What is your name?/ May I have your name?")
    source = tmp_path / "inline-asking-answers.docx"
    document.save(source)

    parsed = DocumentParser().parse(source)
    facts = parsed.items[0].metadata["page_input"]

    assert not facts.get("source_issue")
    assert [question["number"] for question in facts["asking"]] == [11, 12]
    assert facts["asking"][0]["reference_answers"] == ["What colour does Amber like?"]
    assert facts["asking"][1]["reference_answers"] == ["What is your name?", "May I have your name?"]


def test_orphan_asking_section_cannot_disappear_from_entry_preflight(tmp_path):
    document = Document()
    _append_family(document, "info_retelling", "Amber")
    document.add_paragraph("第二节 询问信息（每小题1分）")
    document.add_paragraph("12. 你想知道对方的名字。")
    document.add_paragraph("参考答案：12. What is your name?")
    source = tmp_path / "orphan-asking-section.docx"
    document.save(source)

    parsed = DocumentParser().parse(source)
    facts = parsed.items[0].metadata["page_input"]

    assert parsed.source_parse.issues
    assert "找不到对应的转述材料" in facts["source_issue"]
    assert page_input_completeness(facts)["status"] != "complete"
