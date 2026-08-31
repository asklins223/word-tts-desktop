"""Exam-paper audio naming shared by legacy and workflow projections."""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any, Iterable


EXAM_PAPER_MARKERS = (
    "听后选择",
    "听后应答",
    "听后记录并转述信息",
    "模仿朗读",
)

# 另一类套卷沿用旧题型名称，但仍然把多道大题放在同一份文档中。
# 这些标记只用于“套卷命名/投影”判断，不参与具体题型检测；题型检测
# 仍由 question_model 的唯一注册表负责。
EXAM_PAPER_LEGACY_MARKERS = (
    "信息获取",
    "信息转述及询问",
)
EXAM_PAPER_SCORE_MARKER = re.compile(
    r"(?:共\s*[：:]?\s*[0-9０-９零〇一二两三四五六七八九十百]+\s*"
    r"(?:小\s*题|道\s*题|题)|"
    r"满分\s*[：:]?\s*[0-9０-９零〇一二两三四五六七八九十百]+\s*分)"
)

# Keep every ZIP entry below one predictable directory.  The layout version
# participates in the deterministic export id so a historical flat ZIP or a
# ZIP produced before duplicate-name allocation is never mistaken for the
# current delivery layout.
ARCHIVE_ROOT_FOLDER = "audio"
ARCHIVE_LAYOUT_VERSION = "folder-v2"


def is_exam_paper_bundle(paragraphs: Iterable[Any]) -> bool:
    """Return whether paragraphs look like a complete exam-paper bundle.

    套卷没有单一固定标题：新题型使用“听后选择/听后应答”等名称，旧题型
    套卷则常见“信息获取/信息转述及询问/模仿朗读”的组合。这里使用“多
    个已知大题标记 + 至少两个题量/分值信号”的保守判定，避免专项资料
    只因文件名或一个标题就切换整卷命名规则。
    """

    texts = []
    for paragraph in paragraphs:
        if isinstance(paragraph, (tuple, list)) and len(paragraph) >= 2:
            texts.append(str(paragraph[1] or ""))
        else:
            texts.append(str(paragraph or ""))
    full_text = "\n".join(texts)
    modern_hits = sum(marker in full_text for marker in EXAM_PAPER_MARKERS)
    legacy_hits = sum(marker in full_text for marker in EXAM_PAPER_LEGACY_MARKERS)
    score_signals = len(EXAM_PAPER_SCORE_MARKER.findall(full_text))
    return (
        all(marker in full_text for marker in EXAM_PAPER_MARKERS)
        or (
            modern_hits + legacy_hits >= 3
            and score_signals >= 2
        )
    )


def normalize_question_numbers(value: Any) -> list[int]:
    """Normalize parser-provided question numbers without inventing numbers."""

    values: list[int] = []
    candidates = value if isinstance(value, (list, tuple, set)) else [value]
    for candidate in candidates:
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, int):
            parsed = [candidate]
        else:
            parsed = [int(match) for match in re.findall(r"\d+", str(candidate or ""))]
        for number in parsed:
            if number > 0 and number not in values:
                values.append(number)
    return values


def format_question_numbers(value: Any) -> str:
    """Format one question or a contiguous question range for a filename."""

    numbers = normalize_question_numbers(value)
    if not numbers:
        return ""
    if len(numbers) == 1:
        return str(numbers[0])
    if numbers == list(range(numbers[0], numbers[-1] + 1)):
        return f"{numbers[0]}-{numbers[-1]}"
    return "-".join(str(number) for number in numbers)


def audio_filename_stem(type_path: Iterable[Any], ordinal: Any) -> str:
    """Build ``大题型-小题型-序号`` without an extension."""

    path = []
    for value in type_path:
        text = str(value or "").strip().strip("-")
        if text:
            path.append(text)
    ordinal_values = normalize_question_numbers(ordinal)
    if not path or len(ordinal_values) != 1:
        return ""
    return f"{'-'.join(path)}-{ordinal_values[0]}"


def audio_type_label(category: Any) -> str:
    """Return the stable filename label for one parser category.

    Legacy parsers describe generated audio as ``<题型>录音稿`` while the
    user-facing filename should still be ``<题型>-<序号>``.  Keep other
    category qualifiers (for example ``题目``) because they distinguish two
    different audio streams in the old exam-paper format.
    """

    label = str(category or "").strip()
    if label.endswith("录音稿"):
        label = label[:-3].strip()
    return label


def audio_filename_stem_for_category(category: Any, ordinal: Any) -> str:
    """Build a safe ``category-ordinal`` stem for legacy/fallback items."""

    return audio_filename_stem([audio_type_label(category)], ordinal)


def safe_audio_filename_stem(value: Any, *, limit: int = 240) -> str:
    """Keep a parser-provided stem as one safe basename."""

    raw = str(value or "").replace("\\", "/")
    stem = PurePath(raw).name
    stem = re.sub(r"[\x00-\x1f\x7f]", "", stem).strip(" .")
    return stem[:limit]


def audio_filename_from_stem(stem: Any, fmt: Any) -> str | None:
    """Append a validated artifact format to a safe parser-provided stem."""

    safe_stem = safe_audio_filename_stem(stem)
    extension = re.sub(r"[^a-z0-9]+", "", str(fmt or "").lower().lstrip("."))
    if not safe_stem or not extension:
        return None
    return f"{safe_stem}.{extension}"


def unique_filename(filename: Any, used_filenames: set[str]) -> str:
    """Return a deterministic, case-insensitive unique filename.

    The caller supplies already-sanitized basenames and keeps the set for one
    ordered export. Keeping this small policy in the shared naming module
    makes the legacy progress path, durable workspace projection, and ZIP
    exporter agree when old parser metadata contains duplicate stems.
    """

    raw = str(filename or "").strip()
    if not raw:
        return ""
    used = {str(value).casefold() for value in used_filenames}
    candidate = raw
    suffix = 2
    if "." in raw:
        base, extension = raw.rsplit(".", 1)
        while candidate.casefold() in used:
            candidate = f"{base}_{suffix}.{extension}"
            suffix += 1
    else:
        while candidate.casefold() in used:
            candidate = f"{raw}_{suffix}"
            suffix += 1
    used_filenames.add(candidate.casefold())
    return candidate


__all__ = [
    "ARCHIVE_LAYOUT_VERSION",
    "ARCHIVE_ROOT_FOLDER",
    "EXAM_PAPER_MARKERS",
    "EXAM_PAPER_LEGACY_MARKERS",
    "audio_filename_stem_for_category",
    "audio_filename_stem",
    "audio_filename_from_stem",
    "audio_type_label",
    "format_question_numbers",
    "is_exam_paper_bundle",
    "normalize_question_numbers",
    "safe_audio_filename_stem",
    "unique_filename",
]
