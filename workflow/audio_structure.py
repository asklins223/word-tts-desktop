"""Structural anchors for ASR rows: English question ordinals and Chinese section cues.

This module only classifies individual recognition rows and parses the Chinese
instructions around them.  It deliberately performs no question-text matching;
the audio import pipeline uses these facts to attribute recognized lines to
question numbers and to the section they belong to.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


# Chinese ideographs (including the compatibility and extension-A ranges) are
# the signal for a "Chinese" row; kana/Latin text stays non-Chinese.
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Ordinal prompt lines are expected to be a single ASCII token; strip ASCII
# whitespace/punctuation from both ends before looking at the token itself.
_ASCII_STRIP = string.whitespace + string.punctuation

# ASR often appends a digit to a spelled-out number ("ten0", "thre0").
_LETTERS_WITH_TRAILING_DIGITS_RE = re.compile(r"([a-z]+)[0-9]+")

# English number words understood as question prompts.  The public contract is
# 1..20; hyphenated compounds are parsed but fall outside that range.
_ORDINAL_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

_ORDINAL_SUFFIXES = ("st", "nd", "rd", "th")

# Chinese numerals used inside instructions (whole numbers up to 99).
_CHINESE_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

# A number token: Arabic digits or a Chinese numeral (十/十五/二十一 ...).
_SECTION_NUMBER_RE = r"[0-9]+|[零〇一二两三四五六七八九十]+"
_SECTION_RANGE_SEPARATOR_RE = re.compile(r"[至~～\-—–到]")
_SECTION_LIST_SEPARATOR_RE = re.compile(r"[、,，和及]")
# A trailing count phrase, e.g. "四个问题" / "个问题" / "两个问题".
_SECTION_COUNT_RE = re.compile(r"(?:[一二两三四五六七八九十]+)?个?问题")
_SECTION_HEADER_RE = re.compile(r"^(?:[一二三四五六七八九十]+[、,，.．]|第[一二三四五六七八九十]+节)$")
_SECTION_TITLES = frozenset({"模仿朗读", "信息获取", "短文复述及提问"})
_LISTEN_SELECTION_RE = re.compile(r"听[第一二三四五六七八九十0-9两]+段对话")

# Score values suggested for RowFacts.section_kind.
SECTION_LISTENING_SELECTION = "listening_selection"
SECTION_LISTENING_QUESTIONS = "listening_questions"
SECTION_RETELLING = "retelling"
SECTION_QUESTIONING = "questioning"
SECTION_HEADER = "section_header"
SECTION_OTHER = "other"

MAX_ORDINAL = 20


@dataclass(frozen=True)
class RowFacts:
    """Per-row classification facts derived from the raw recognition output."""

    row_index: int
    text: str
    error: bool
    is_chinese: bool
    is_announcement: bool
    ordinal: int | None
    section_numbers: tuple[int, ...] | None
    section_kind: str | None


def _chinese_char_count(text: str) -> int:
    return len(_CJK_RE.findall(text))


def _ordinal_from_word(token: str) -> int | None:
    """Resolve a single alphabetic token to an English number word."""

    if token in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[token]
    # ASR sometimes drops the final letter of a spoken prompt ("thre", "seve").
    # Only accept that one -1 edit: looser fuzzy matching would turn ordinary
    # words such as "night", "right", "then" into question numbers.
    if len(token) <= 3:
        return None
    candidates = [
        number
        for word, number in _ORDINAL_WORDS.items()
        if len(word) == len(token) + 1 and word.startswith(token)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _ordinal_from_hyphenated(token: str) -> int | None:
    """Resolve hyphenated number words such as ``twenty-one``."""

    left, separator, right = token.partition("-")
    if not separator or not left or not right:
        return None
    tens = _ORDINAL_WORDS.get(left)
    units = _ORDINAL_WORDS.get(right)
    if tens is None or units is None or tens != 20 or not 1 <= units <= 9:
        return None
    value = tens + units
    return value if 1 <= value <= MAX_ORDINAL else None


def parse_ordinal(text: str) -> int | None:
    """Return the question number when ``text`` is an English ordinal prompt row.

    Only a single pure-ASCII token is accepted, so ordinary English content
    ("do you enjoy listening to music?", "one night") and Chinese rows such as
    "3、" never look like question prompts.
    """

    if not isinstance(text, str):
        return None
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    if not normalized or any(ord(char) > 127 for char in normalized):
        return None
    token = normalized.strip(_ASCII_STRIP).strip()
    if not token or any(char in string.whitespace for char in token):
        return None
    trailing_digits = _LETTERS_WITH_TRAILING_DIGITS_RE.fullmatch(token)
    if trailing_digits is not None:
        token = trailing_digits.group(1)
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= MAX_ORDINAL else None
    for suffix in _ORDINAL_SUFFIXES:
        if token.endswith(suffix) and token[: -len(suffix)].isdigit():
            value = int(token[: -len(suffix)])
            return value if 1 <= value <= MAX_ORDINAL else None
    if "-" in token:
        return _ordinal_from_hyphenated(token)
    if not token.isalpha():
        return None
    return _ordinal_from_word(token)


def _parse_chinese_number(token: str) -> int | None:
    """Parse an Arabic or Chinese whole number no greater than 99."""

    if not token:
        return None
    if token.isdigit():
        return int(token)
    if "十" in token:
        parts = token.split("十")
        if len(parts) != 2:
            return None
        tens_text, units_text = parts
        if tens_text == "":
            tens = 1
        elif len(tens_text) == 1 and tens_text in _CHINESE_DIGITS and tens_text != "零":
            tens = _CHINESE_DIGITS[tens_text]
        else:
            return None
        if units_text == "":
            units = 0
        elif len(units_text) == 1 and units_text in _CHINESE_DIGITS:
            units = _CHINESE_DIGITS[units_text]
        else:
            return None
        return tens * 10 + units
    if len(token) == 1:
        return _CHINESE_DIGITS.get(token)
    return None


def _match_section_number(text: str, position: int) -> tuple[int, int] | None:
    """Match one number token and return ``(value, next_position)``.

    A trailing "两" followed by "个" belongs to the count phrase ("四两个问题")
    rather than to the number itself, so it is left for the count matcher.
    """

    match = re.match(_SECTION_NUMBER_RE, text[position:])
    if match is None:
        return None
    token = match.group(0)
    consumed = match.end()
    if (
        len(token) > 1
        and token.endswith("两")
        and position + consumed < len(text)
        and text[position + consumed] == "个"
    ):
        token = token[:-1]
        consumed -= 1
    value = _parse_chinese_number(token)
    if value is None:
        return None
    return value, position + consumed


def _section_numbers_after(
    text: str, start: int
) -> tuple[int, ...] | None:
    """Parse a number expression starting right after a ``第`` marker."""

    length = len(text)

    def skip(position: int) -> int:
        while position < length and text[position].isspace():
            position += 1
        return position

    first_match = _match_section_number(text, skip(start))
    if first_match is None:
        return None
    first, position = first_match
    position = skip(position)

    range_match = _SECTION_RANGE_SEPARATOR_RE.match(text[position:])
    if range_match is not None:
        second_position = skip(position + range_match.end())
        second_match = _match_section_number(text, second_position)
        if second_match is not None:
            second, second_end = second_match
            if second >= first:
                numbers = tuple(range(first, second + 1))
                window = text[position : second_end + 20]
                if "问题" in window or "回答" in text:
                    return numbers

    items = [first]
    cursor = position
    while True:
        separator = _SECTION_LIST_SEPARATOR_RE.match(text[cursor:])
        if separator is None:
            break
        item_position = skip(cursor + separator.end())
        marker = re.match("第", text[item_position:])
        if marker is not None:
            item_position = skip(item_position + marker.end())
        item_match = _match_section_number(text, item_position)
        if item_match is None:
            break
        item, cursor = item_match
        items.append(item)
        cursor = skip(cursor)
        if _SECTION_COUNT_RE.match(text[cursor:]):
            break
    if len(items) > 1 and "问题" in text[position : cursor + 10]:
        return tuple(items)

    # "回答第7个问题" also carries a usable single question number.
    if _SECTION_COUNT_RE.match(text[position:]):
        return (first,)
    return None


def parse_section_numbers(text: str) -> tuple[int, ...] | None:
    """Expand the question numbers named by a Chinese listening instruction.

    Ranges such as "第7~10"/"第7至10"/"第7到14" are expanded inclusively and
    lists such as "第3、4个问题" become a tuple in the order they are spoken.
    """

    if not isinstance(text, str) or not text:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    if not _CJK_RE.search(normalized):
        return None
    for marker in re.finditer("第", normalized):
        numbers = _section_numbers_after(normalized, marker.end())
        if numbers is not None:
            return numbers
    return None


def _section_kind(
    text: str,
    is_chinese: bool,
    section_numbers: tuple[int, ...] | None,
) -> str | None:
    """Classify one Chinese row into the suggested section categories."""

    if not is_chinese:
        return None
    normalized = unicodedata.normalize("NFKC", text).strip()
    if _SECTION_HEADER_RE.fullmatch(normalized) or normalized in _SECTION_TITLES:
        return SECTION_HEADER
    if "复述" in normalized or "转述" in normalized:
        return SECTION_RETELLING
    if "提问" in normalized:
        return SECTION_QUESTIONING
    if section_numbers is not None or "回答问题" in normalized:
        return SECTION_LISTENING_QUESTIONS
    if "听选信息" in normalized or _LISTEN_SELECTION_RE.search(normalized):
        return SECTION_LISTENING_SELECTION
    return SECTION_OTHER


def detect_rows(recognized: Sequence[Mapping[str, Any]]) -> list[RowFacts]:
    """Classify each recognition row, preserving its positional index."""

    rows: list[RowFacts] = []
    for index, entry in enumerate(recognized):
        text = str(entry.get("text") or "")
        error = bool(entry.get("error"))
        chinese_count = _chinese_char_count(text)
        is_chinese = chinese_count > 0
        section_numbers = parse_section_numbers(text)
        rows.append(
            RowFacts(
                row_index=index,
                text=text,
                error=error,
                is_chinese=is_chinese,
                is_announcement=error or chinese_count >= 4,
                ordinal=parse_ordinal(text),
                section_numbers=section_numbers,
                section_kind=_section_kind(text, is_chinese, section_numbers),
            )
        )
    return rows


def _clamp_index(rows: Sequence[RowFacts], index: int) -> int | None:
    if not rows or index < 0 or index >= len(rows):
        return None
    return index


def nearest_ordinal(rows: Sequence[RowFacts], index: int) -> tuple[int, int] | None:
    """Return the closest ordinal row at or before ``index``.

    The search includes ``index`` itself, so asking about a question-prompt row
    returns that row; asking about a question-text row returns the prompt that
    introduced it.  Only the nearest ordinal matters, so a newer prompt always
    takes over attribution.
    """

    start = _clamp_index(rows, index)
    if start is None:
        return None
    for position in range(start, -1, -1):
        row = rows[position]
        if row.ordinal is not None:
            return (row.row_index, row.ordinal)
    return None


def latest_section_hint(rows: Sequence[RowFacts], index: int) -> RowFacts | None:
    """Return the nearest row carrying section numbers at or before ``index``."""

    start = _clamp_index(rows, index)
    if start is None:
        return None
    for position in range(start, -1, -1):
        row = rows[position]
        if row.section_numbers:
            return row
    return None
