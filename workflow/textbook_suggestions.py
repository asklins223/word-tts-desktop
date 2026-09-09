"""课文跟读录入配置建议。

从源文件名和解析条目元数据推断平台“新增课文”分类信息字段的候选值。
建议只作为配置表单的默认值：用户可以修改任何字段，识别不出时保持
为空，用户修改后的值永远优先于本模块的输出。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_GRADE_TEXT_TO_NAME = {
    "7": "七年级",
    "七": "七年级",
    "8": "八年级",
    "八": "八年级",
    "9": "九年级",
    "九": "九年级",
}

# “7上/七上/G7/七年级/9下”等常见写法。年级数字前不能紧跟字母或数字，
# 避免 “U1” 之类标记被误读成年级；结尾只排除数字，让 “9上U1” 这种
# 紧连写法仍然能取到册别。
_GRADE_RE = re.compile(r"(?<![0-9A-Za-z])(?:G|g)?(?P<grade>[789七八九])(?:\s*年级)?(?P<volume>[上下])?册?(?![0-9])")
_VOLUME_RE = re.compile(r"(?<![0-9A-Za-z])(?P<volume>[上下])册?(?![0-9A-Za-z])")
_UNIT_RE = re.compile(r"(?P<unit>(?:starter\s*)?unit\s*0?(?P<unit_number>\d{1,2}))", re.IGNORECASE)
_SHORT_UNIT_RE = re.compile(r"(?<![0-9A-Za-z])U\s*0?(?P<unit_number>\d{1,2})(?![0-9A-Za-z])")
_VERSION_MARKERS: tuple[tuple[str, str], ...] = (
    ("人教", "人教版"),
    ("外研", "外研版"),
    ("仁爱", "仁爱版"),
    ("译林", "译林版"),
    ("冀教", "冀教版"),
    ("北师大", "北师大版"),
    ("牛津", "牛津版"),
)

_GRADE_TO_STAGE = {
    "七年级": "初中",
    "八年级": "初中",
    "九年级": "初中",
}

_FORM_ROLEPLAY = "角色扮演"
_FORM_SYNC = "同步课文"


def _field(value: str, source: str, confidence: str) -> dict[str, str]:
    return {"value": value, "source": source, "confidence": confidence}


def _normalize_unit_display(raw: str, number: str) -> str:
    stripped = re.sub(r"\s+", " ", raw).strip()
    if re.fullmatch(r"[Ss]tarter\s+[Uu]nit\s*\d{1,2}", stripped):
        return re.sub(r"unit", "Unit", stripped, count=1) if "unit" in stripped else stripped
    return f"Unit {int(number)}"


def _suggest_from_filename(filename: str) -> dict[str, dict[str, str]]:
    suggestions: dict[str, dict[str, str]] = {}
    text = str(filename or "")
    stem = re.sub(r"\.(docx?|xlsx?)\s*$", "", text, flags=re.IGNORECASE)

    for marker, label in _VERSION_MARKERS:
        if marker in stem:
            suggestions["textbookVersion"] = _field(label, "filename", "HIGH")
            break

    grade_match = _GRADE_RE.search(stem)
    grade_name = ""
    if grade_match:
        grade_name = _GRADE_TEXT_TO_NAME.get(grade_match.group("grade"), "")
        if grade_name:
            suggestions["textbookGrade"] = _field(grade_name, "filename", "HIGH")
            stage = _GRADE_TO_STAGE.get(grade_name)
            if stage:
                suggestions["textbookStage"] = _field(stage, "filename", "HIGH")
    volume = ""
    if grade_match and grade_match.group("volume"):
        volume = grade_match.group("volume")
    else:
        volume_match = _VOLUME_RE.search(stem)
        if volume_match:
            volume = volume_match.group("volume")
    if volume:
        suggestions["textbookVolume"] = _field(f"{volume}册", "filename", "HIGH")

    unit_match = _UNIT_RE.search(stem)
    if unit_match:
        suggestions["textbookUnit"] = _field(
            _normalize_unit_display(unit_match.group("unit"), unit_match.group("unit_number")),
            "filename",
            "HIGH",
        )
    else:
        short_match = _SHORT_UNIT_RE.search(stem)
        if short_match:
            suggestions["textbookUnit"] = _field(
                f"Unit {int(short_match.group('unit_number'))}",
                "filename",
                "MEDIUM",
            )

    # 文件名里单元标记之后的尾段作为英文课文名候选，例如
    # “课文跟读-7上-Unit1 You and Me” → “You and Me”。
    if unit_match:
        tail = stem[unit_match.end():].strip(" -—_·")
        tail = re.sub(r"\s+", " ", tail)
        if tail:
            suggestions["textbookNameEn"] = _field(tail, "filename", "MEDIUM")
    return suggestions


def _suggest_from_metadata(metadata_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    suggestions: dict[str, dict[str, str]] = {}
    sections: list[str] = []
    has_roleplay = False
    for row in metadata_rows:
        if not isinstance(row, Mapping):
            continue
        section = str(row.get("section") or "").strip()
        if section and section not in sections:
            sections.append(section)
        category = str(row.get("category") or row.get("doc_type") or "").strip()
        if (
            category in {"句子跟读", "对话跟读"}
            or str(row.get("role") or "").strip()
            or str(row.get("conversation_number") or "").strip()
            or str(row.get("entry_form") or "").strip() == _FORM_ROLEPLAY
        ):
            has_roleplay = True

    if len(sections) == 1:
        suggestions["textbookLesson"] = _field(sections[0], "document", "HIGH")
    elif len(sections) > 1:
        # 文档包含多个课时节；单条课文记录的“课时”选择存在歧义，留空
        # 由用户确认，避免把一个章节的值悄悄套到整份文档上。
        suggestions["textbookLesson"] = {
            "value": "",
            "source": "document",
            "confidence": "LOW",
            "candidates": sections[:12],
        }

    form = _FORM_ROLEPLAY if has_roleplay else _FORM_SYNC
    suggestions["textbookForm"] = _field(form, "document", "MEDIUM")
    return suggestions


def suggest_textbook_configuration(
    source_filename: str | None,
    metadata_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """Build per-field textbook configuration suggestions.

    返回的字段名与课文录入配置键一致，每个值携带
    ``value/source/confidence``；``value`` 为空表示识别失败，应保持
    配置字段为空。文件名证据优先级低于文档结构证据不存在冲突时直接
    合并；同一字段同时出现时文档结构证据胜出（它来自正文事实）。
    """

    suggestions = _suggest_from_filename(source_filename)
    for key, value in _suggest_from_metadata(metadata_rows).items():
        suggestions[key] = value
    return {key: value for key, value in suggestions.items() if value.get("value") or value.get("candidates")}


__all__ = ["suggest_textbook_configuration"]
