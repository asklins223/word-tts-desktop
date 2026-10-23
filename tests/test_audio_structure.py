"""Tests for ASR row structure detection (ordinals and section instructions)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from workflow.audio_structure import (
    SECTION_HEADER,
    SECTION_LISTENING_QUESTIONS,
    SECTION_LISTENING_SELECTION,
    SECTION_OTHER,
    SECTION_QUESTIONING,
    SECTION_RETELLING,
    RowFacts,
    detect_rows,
    latest_section_hint,
    nearest_ordinal,
    parse_ordinal,
    parse_section_numbers,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "audio_asr_exam_shenzhen_2024_set2.json"
)

# Real SenseVoice rows that act as English question prompts in the fixture.
EXPECTED_FIXTURE_ORDINALS = [
    (52, 1),
    (56, 2),
    (79, 3),
    (83, 4),
    (110, 5),
    (114, 6),
    (147, 7),
    (151, 8),
    (155, 9),
    (159, 10),
]


@pytest.fixture(scope="module")
def speech() -> list[dict[str, Any]]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return payload["speech"]


@pytest.fixture(scope="module")
def rows(speech: list[dict[str, Any]]) -> list[RowFacts]:
    return detect_rows(speech)


def _row(rows: list[RowFacts], index: int) -> RowFacts:
    return rows[index]


def test_fixture_has_212_real_recognition_rows(
    speech: list[dict[str, Any]],
    rows: list[RowFacts],
) -> None:
    assert len(speech) == 212
    assert len(rows) == 212
    assert [fact.row_index for fact in rows] == list(range(212))
    assert all(fact.text == str(entry.get("text") or "") for fact, entry in zip(rows, speech))


def test_fixture_ordinal_prompts_are_exactly_the_expected_rows(
    rows: list[RowFacts],
) -> None:
    detected = [
        (fact.row_index, fact.ordinal)
        for fact in rows
        if fact.ordinal is not None
    ]
    assert detected == EXPECTED_FIXTURE_ORDINALS


@pytest.mark.parametrize("index,ordinal", EXPECTED_FIXTURE_ORDINALS)
def test_fixture_ordinal_prompt_row(
    rows: list[RowFacts],
    index: int,
    ordinal: int,
) -> None:
    fact = _row(rows, index)
    assert fact.ordinal == ordinal
    assert fact.is_chinese is False
    assert fact.is_announcement is False
    assert fact.section_numbers is None
    assert parse_ordinal(fact.text) == ordinal


def test_fixture_chinese_enumeration_marker_is_not_an_ordinal(
    rows: list[RowFacts],
) -> None:
    fact = _row(rows, 163)
    assert fact.text == "3、"
    assert parse_ordinal(fact.text) is None
    assert fact.ordinal is None
    assert fact.is_chinese is False


def test_fixture_english_content_is_not_an_ordinal(
    rows: list[RowFacts],
) -> None:
    fact = _row(rows, 12)
    assert fact.text == "do you enjoy listening to music?"
    assert parse_ordinal(fact.text) is None
    assert fact.ordinal is None
    assert fact.is_chinese is False
    assert fact.is_announcement is False
    # Ordinary short English function words must not fuzzy-match a number.
    for content in ("for", "one night", "the", "to", "you"):
        assert parse_ordinal(content) is None


def test_fixture_section_numbers(
    rows: list[RowFacts],
) -> None:
    first = _row(rows, 36)
    assert parse_section_numbers(first.text) == (1, 2)
    assert first.section_numbers == (1, 2)

    second = _row(rows, 61)
    assert second.text == "回答第3至4两个问题，"
    assert parse_section_numbers(second.text) == (3, 4)
    assert second.section_numbers == (3, 4)

    third = _row(rows, 88)
    assert third.text == "回答第5至6两个问题。"
    assert parse_section_numbers(third.text) == (5, 6)
    assert third.section_numbers == (5, 6)

    # The fixture's real ASR line reads "第7到14个问题", so the expansion is
    # 7..14.  The task's "第7~10四个问题" example is covered separately below.
    fourth = _row(rows, 122)
    assert fourth.text == "请根据所听内容回答第7到14个问题。"
    assert parse_section_numbers(fourth.text) == tuple(range(7, 15))
    assert fourth.section_numbers == tuple(range(7, 15))


def test_task_example_section_instruction_expands_to_four_questions() -> None:
    text = "听下面一段短文，播放两遍。请根据所听内容回答第7~10四个问题。"
    assert parse_section_numbers(text) == (7, 8, 9, 10)
    assert parse_section_numbers("回答第7~10") == (7, 8, 9, 10)
    assert parse_section_numbers("请根据所听内容回答第7至10四个问题。") == (7, 8, 9, 10)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("听第二段对话，回答第3至4两个问题。", (3, 4)),
        ("回答第5至6两个问题。", (5, 6)),
        ("回答第7到14个问题。", tuple(range(7, 15))),
        ("回答第7—10个问题。", (7, 8, 9, 10)),
        ("回答第7-10个问题。", (7, 8, 9, 10)),
        ("回答第七至十四个问题。", tuple(range(7, 15))),
        ("第十至十五个问题。", tuple(range(10, 16))),
        ("回答第七、八两个问题。", (7, 8)),
        ("回答第3、4两个问题。", (3, 4)),
        ("回答第三、四两个问题。", (3, 4)),
        ("回答第3和4两个问题。", (3, 4)),
        ("回答第3、4、5个问题。", (3, 4, 5)),
        ("回答第7个问题。", (7,)),
        # Full-width digits and tilde normalise through NFKC.
        ("回答第７～１０四个问题。", (7, 8, 9, 10)),
        ("请回答第 8 至 9 两个问题。", (8, 9)),
    ],
)
def test_parse_section_numbers_variants(text: str, expected: tuple[int, ...]) -> None:
    assert parse_section_numbers(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "do you enjoy listening to music?",
        "听第一段对话，",
        "第一节",
        "现在你有10秒钟的阅题时间。",
        "各段播放后有两个问题，",
        "请根据以下提示提问",
        "第一段对话",
        "第4至3个问题。",
    ],
)
def test_parse_section_numbers_ignores_non_instructions(text: str) -> None:
    assert parse_section_numbers(text) is None


@pytest.mark.parametrize(
    "word,expected",
    list(
        zip(
            (
                "one",
                "two",
                "three",
                "four",
                "five",
                "six",
                "seven",
                "eight",
                "nine",
                "ten",
                "eleven",
                "twelve",
                "thirteen",
                "fourteen",
                "fifteen",
                "sixteen",
                "seventeen",
                "eighteen",
                "nineteen",
                "twenty",
            ),
            range(1, 21),
        )
    ),
)
def test_parse_ordinal_english_number_words(word: str, expected: int) -> None:
    assert parse_ordinal(word) == expected
    assert parse_ordinal(word.upper()) == expected
    assert parse_ordinal(f" {word}, ") == expected
    assert parse_ordinal(f"{word}.") == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.", 1),
        ("10,", 10),
        ("9", 9),
        ("20.", 20),
        ("ten0,", 10),
        ("thre,", 3),
        ("seve,", 7),
        ("eigh", 8),
        ("ＴＥＮ", 10),
        ("１.", 1),
        ("2nd", 2),
        ("3、", None),
        ("３、", None),
        ("for", None),
        ("twe", None),
        ("fiv", None),
        ("one night", None),
        ("do you enjoy listening to music?", None),
        # Ordinary English words one edit away from a number word must not be
        # mistaken for a question prompt.
        ("night", None),
        ("right", None),
        ("then", None),
        ("tree", None),
        ("tent", None),
        ("weight", None),
        ("hour", None),
        ("your", None),
        ("give", None),
        ("21", None),
        ("0", None),
        ("", None),
        ("twenty-one", None),
    ],
)
def test_parse_ordinal_spelling_and_width_variants(
    text: str,
    expected: int | None,
) -> None:
    assert parse_ordinal(text) == expected


def test_nearest_ordinal_attribution(rows: list[RowFacts]) -> None:
    # No ordinal has been spoken before the first prompt at row 52.
    assert nearest_ordinal(rows, 0) is None
    assert nearest_ordinal(rows, 50) is None

    assert nearest_ordinal(rows, 53) == (52, 1)
    assert nearest_ordinal(rows, 57) == (56, 2)
    # A newer prompt takes over: before row 79 the answer still belongs to 2,
    # after row 79 it belongs to 3 and never falls back to 3->4 incorrectly.
    assert nearest_ordinal(rows, 78) == (56, 2)
    assert nearest_ordinal(rows, 79) == (79, 3)
    assert nearest_ordinal(rows, 80) == (79, 3)
    assert nearest_ordinal(rows, 83) == (83, 4)
    assert nearest_ordinal(rows, 156) == (155, 9)
    assert nearest_ordinal(rows, 160) == (159, 10)


def test_nearest_ordinal_boundaries(rows: list[RowFacts]) -> None:
    assert nearest_ordinal([], 0) is None
    assert nearest_ordinal(rows, -1) is None
    assert nearest_ordinal(rows, len(rows)) is None


def test_nearest_ordinal_with_synthetic_rows() -> None:
    facts = detect_rows(
        [
            {"text": "1.", "error": None},
            {"text": "where does the conversation take place?"},
            {"text": "two,"},
            {"text": "what does the woman want"},
        ]
    )
    assert nearest_ordinal(facts, 0) == (0, 1)
    assert nearest_ordinal(facts, 1) == (0, 1)
    assert nearest_ordinal(facts, 2) == (2, 2)
    assert nearest_ordinal(facts, 3) == (2, 2)


def test_latest_section_hint(rows: list[RowFacts]) -> None:
    assert latest_section_hint(rows, 0) is None
    assert latest_section_hint(rows, 35) is None

    hint = latest_section_hint(rows, 50)
    assert hint is not None
    assert hint.row_index == 36
    assert hint.section_numbers == (1, 2)

    hint = latest_section_hint(rows, 80)
    assert hint is not None
    assert hint.row_index == 61
    assert hint.section_numbers == (3, 4)

    hint = latest_section_hint(rows, 90)
    assert hint is not None
    assert hint.row_index == 88
    assert hint.section_numbers == (5, 6)

    hint = latest_section_hint(rows, 126)
    assert hint is not None
    assert hint.row_index == 122
    assert hint.section_numbers == tuple(range(7, 15))


def test_latest_section_hint_boundaries(rows: list[RowFacts]) -> None:
    assert latest_section_hint([], 0) is None
    assert latest_section_hint(rows, -1) is None
    assert latest_section_hint(rows, len(rows)) is None
    # A hint row is its own latest hint.
    assert latest_section_hint(rows, 61) is rows[61]
    assert latest_section_hint(rows, 122) is rows[122]


def test_detect_rows_synthetic_classification() -> None:
    facts = detect_rows(
        [
            {"text": "听第一段对话，", "error": ""},
            {"text": "回答第1至2两个问题，"},
            {"text": "1."},
            {"text": "do you enjoy listening to music?"},
            {"text": "", "error": "decode failed"},
            {"text": "第一节"},
            {"text": "听选信息，"},
            {"text": "短文复述，"},
            {"text": "提问，"},
            {"text": "开始录音。"},
            {"text": "短文复述及提问"},
            {"text": "回答问题，"},
        ]
    )
    assert [fact.row_index for fact in facts] == list(range(12))

    assert facts[0].is_chinese is True
    assert facts[0].is_announcement is True
    assert facts[0].section_kind == SECTION_LISTENING_SELECTION

    assert facts[1].section_numbers == (1, 2)
    assert facts[1].section_kind == SECTION_LISTENING_QUESTIONS

    assert facts[2].ordinal == 1
    assert facts[2].section_kind is None

    assert facts[3].text == "do you enjoy listening to music?"
    assert facts[3].is_chinese is False
    assert facts[3].is_announcement is False
    assert facts[3].ordinal is None
    assert facts[3].section_kind is None

    assert facts[4].error is True
    assert facts[4].text == ""
    assert facts[4].is_announcement is True

    assert facts[5].section_kind == SECTION_HEADER
    assert facts[6].section_kind == SECTION_LISTENING_SELECTION
    assert facts[7].section_kind == SECTION_RETELLING
    assert facts[8].section_kind == SECTION_QUESTIONING
    assert facts[9].section_kind == SECTION_OTHER
    assert facts[10].section_kind == SECTION_HEADER
    assert facts[11].section_kind == SECTION_LISTENING_QUESTIONS


def test_detect_rows_missing_text_and_error_defaults() -> None:
    facts = detect_rows([{"error": None}, {"error": "boom"}])
    assert facts[0].text == ""
    assert facts[0].is_chinese is False
    assert facts[0].is_announcement is False
    assert facts[0].ordinal is None
    assert facts[0].section_numbers is None
    assert facts[0].section_kind is None
    assert facts[1].is_announcement is True


def test_row_facts_are_frozen_dataclasses(rows: list[RowFacts]) -> None:
    fact = rows[0]
    assert dataclasses.is_dataclass(fact)
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.ordinal = 5  # type: ignore[misc]
