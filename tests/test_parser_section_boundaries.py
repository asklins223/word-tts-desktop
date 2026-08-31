"""题型解析器在省略“第X节”时仍要互相隔离。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from docx import Document
from docx.oxml import parse_xml

from question_types import (
    InfoAcquisitionParser,
    InfoRetellingParser,
    ImitationReadingParser,
    ListeningRecordRetellingParser,
    ListeningSelectionParser,
)
from question_types.segmenter import _build_parser
from question_types.text_utils import load_paragraphs


def _parse(parser_cls, texts):
    paragraphs = [(index, text, "Normal") for index, text in enumerate(texts)]
    metadata = [{} for _ in paragraphs]
    return parser_cls(
        "synthetic.docx",
        preloaded_paras=(paragraphs, metadata),
    ).parse()


def test_info_acquisition_stops_at_plain_next_type_heading():
    result = _parse(InfoAcquisitionParser, [
        "第一节 听选信息",
        "1. What?",
        "录音稿 W: first",
        "信息转述及询问",
        "录音稿 W: leaked",
    ])

    assert [item["text"] for item in result["items"]] == [
        "M: What?",
        "W: first",
    ]


def test_info_acquisition_does_not_treat_ordinal_instruction_as_boundary():
    result = _parse(InfoAcquisitionParser, [
        "第一节 听选信息",
        "一、请根据所听内容选择正确答案。",
        "1. What?",
        "录音稿：W: first",
    ])

    assert [item["text"] for item in result["items"]] == [
        "M: What?",
        "W: first",
    ]


def test_listening_selection_stops_at_plain_next_type_heading():
    result = _parse(ListeningSelectionParser, [
        "听后选择",
        "【录音原文】",
        "W: first",
        "信息转述及询问",
        "【录音原文】",
        "W: leaked",
    ])

    assert [item["text"] for item in result["items"]] == ["W: first"]


def test_listening_selection_does_not_treat_ordinal_instruction_as_boundary():
    result = _parse(ListeningSelectionParser, [
        "听后选择",
        "一、请根据所听内容选择正确答案。",
        "【录音原文】",
        "W: first",
    ])

    assert [item["text"] for item in result["items"]] == ["W: first"]


def test_listening_record_retelling_stops_at_plain_next_type_heading():
    result = _parse(ListeningRecordRetellingParser, [
        "第一节 听后记录",
        "(W) first",
        "信息转述及询问",
        "(M) leaked",
    ])

    assert [item["text"] for item in result["items"]] == ["(W) first"]


def test_info_retelling_stops_at_plain_next_type_heading():
    result = _parse(InfoRetellingParser, [
        "第一节 信息转述",
        "录音稿 W: first",
        "模仿朗读",
        "录音稿 W: leaked",
    ])

    assert [item["text"] for item in result["items"]] == ["W: first"]


def test_imitation_reading_does_not_treat_ordinal_instruction_as_boundary():
    result = _parse(ImitationReadingParser, [
        "一、模仿朗读",
        "一、请听短文并准备。",
        "Good morning.",
    ])

    assert result["item_count"] == 1
    assert result["items"][0]["text"] == "Good morning."


def test_info_retelling_asking_tasks_stop_at_plain_next_type_heading():
    result = _parse(InfoRetellingParser, [
        "第二节 询问信息",
        "1. 你最喜欢什么颜色？",
        "参考答案：",
        "1. What colour do you like best?",
        "模仿朗读",
        "2. This is not an asking task.",
    ])

    assert [task["number"] for task in result["tasks"]] == [1]
    assert result["tasks"][0]["reference_answer"] == (
        "What colour do you like best?"
    )


def test_info_retelling_asking_tasks_allow_chinese_ordinal_prompt():
    result = _parse(InfoRetellingParser, [
        "第二节 询问信息",
        "一、请根据提示提问",
        "1. 你最喜欢什么颜色？",
        "参考答案：",
        "1. What colour do you like best?",
    ])

    assert [task["prompt"] for task in result["tasks"]] == [
        "你最喜欢什么颜色？"
    ]
    assert result["tasks"][0]["reference_answer"] == (
        "What colour do you like best?"
    )


@pytest.mark.parametrize(
    "parser_cls,texts",
    [
        (
            InfoAcquisitionParser,
            [
                "第1节 听选信息",
                "1. What is it?",
                "录音稿：W: first",
                "第2节 回答问题",
                "2. Why?",
                "录音稿：M: second",
            ],
        ),
        (
            InfoRetellingParser,
            [
                "第1节 信息转述",
                "录音稿：W: first",
                "第2节 询问信息",
                "1. 你喜欢什么？",
                "答案：",
                "1. What do you like?",
            ],
        ),
    ],
)
def test_arabic_numbered_sections_are_supported(parser_cls, texts):
    result = _parse(parser_cls, texts)

    if parser_cls is InfoAcquisitionParser:
        assert [item["text"] for item in result["items"]] == [
            "M: What is it?",
            "W: first",
            "W: Why?",
            "M: second",
        ]
    else:
        assert [item["text"] for item in result["items"]] == ["W: first"]
        assert result["tasks"][0]["reference_answer"] == (
            "What do you like?"
        )


def test_builder_does_not_retry_after_constructor_type_error():
    calls = []

    class BrokenParser:
        def __init__(self, filepath, **kwargs):
            calls.append((filepath, kwargs))
            raise TypeError("constructor bug")

    with pytest.raises(TypeError, match="constructor bug"):
        _build_parser(
            BrokenParser,
            "synthetic.docx",
            ([(0, "text", "Normal")], [{}], ()),
        )
    assert len(calls) == 1


def test_builder_supports_positional_only_preloaded_contract():
    expected = ([(0, "text", "Normal")], [{}], ())

    class PositionalParser:
        def __init__(self, filepath, preloaded_paras, /):
            self.filepath = filepath
            self.preloaded_paras = preloaded_paras

    parser = _build_parser(PositionalParser, "synthetic.docx", expected)

    assert parser.filepath == "synthetic.docx"
    assert parser.preloaded_paras == expected[:2]


def test_document_blocks_keep_each_alternate_content_textbox_once():
    first_xml = (
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/'
        'markup-compatibility/2006" '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<mc:Choice Requires="wps"><w:txbxContent><w:p><w:r><w:t>First box</w:t>'
        '</w:r></w:p></w:txbxContent></mc:Choice>'
        '<mc:Fallback><w:txbxContent><w:p><w:r><w:t>First fallback</w:t>'
        '</w:r></w:p></w:txbxContent></mc:Fallback>'
        '</mc:AlternateContent>'
    )
    second_xml = (
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/'
        'markup-compatibility/2006" '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<mc:Choice Requires="wps"><w:txbxContent><w:p><w:r><w:t>Second box</w:t>'
        '</w:r></w:p></w:txbxContent></mc:Choice>'
        '<mc:Fallback><w:txbxContent><w:p><w:r><w:t>Second fallback</w:t>'
        '</w:r></w:p></w:txbxContent></mc:Fallback>'
        '</mc:AlternateContent>'
    )

    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "multiple-textboxes.docx"
        document = Document()
        paragraph = document.add_paragraph()
        paragraph._p.append(parse_xml(first_xml))
        paragraph._p.append(parse_xml(second_xml))
        document.save(path)
        _, _, blocks = load_paragraphs(
            path,
            include_metadata=True,
            include_blocks=True,
        )

    textboxes = [block for block in blocks if block.kind == "textbox"]
    assert len(textboxes) == 1
    assert textboxes[0].fragments == ("First box", "Second box")
