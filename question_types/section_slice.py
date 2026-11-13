"""章节范围切片（阶段 3 第③步）。

在统一结构读取之上，把段落序列切分为带 ``source_locator`` 的大题/
章节范围（对应 v0006 ``major_sections`` 与 ParseCandidate.claimed_blocks
的原文定位）：

- 切分器只认**通用**标题形态（中文序号、“第N节”、Section 字母），
  不内置任何题型的私有规则——题型归属交给裁决层；
- 切分结果是从原文事实到 ``ParseCandidate`` 范围声明的桥：
  ``SectionRange`` 即未来的 claimed_blocks 单位；
- 纯函数、无 IO，可对基线样例做快照测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from question_model.model import FAMILY_REGISTRY, SUB_TYPE_REGISTRY

from .text_utils import MAJOR_TYPE_HEADING_RE, is_major_section_heading

# 通用标题形态（题型无关）：
#   一、二、… / （一）（二）… / 第一节… / Section A…
RE_CN_ORDINAL = re.compile(r"^[一二三四五六七八九十百]+\s*[、.．)]")
RE_CN_PAREN = re.compile(r"^[（(][一二三四五六七八九十]+[)）]")
RE_SESSION = re.compile(r"^第[一二三四五六七八九十\d]+节")
RE_SECTION_AB = re.compile(r"^Section\s+[A-Z]\b", re.IGNORECASE)
_MAJOR_ORDINAL = re.compile(r"^(?:\d+|[一二三四五六七八九十百]+)\s*[、.．)]\s*")

HEADING_PATTERNS = (RE_SESSION, RE_CN_ORDINAL, RE_CN_PAREN, RE_SECTION_AB)


@dataclass(frozen=True)
class SectionRange:
    """一个章节范围：原文定位 + 段落区间 [start, end)。"""

    index: int                 # 文档顺序，从 1 开始
    title: str
    source_locator: str
    start: int
    end: int

    def contains(self, para_index: int) -> bool:
        return self.start <= para_index < self.end


def is_heading(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 40:
        return False
    return any(p.match(stripped) for p in HEADING_PATTERNS)


def slice_sections(paras: list[tuple]) -> list[SectionRange]:
    """把 (段落序号, 文本, 元数据) 序列切分为章节范围。

    无任何标题的文档整体作为一个范围（locator 指向全文），
    保证“每个段落都归属于恰好一个范围”。
    """
    if not paras:
        return []
    heading_indexes = [
        i for i, (_, text, _) in enumerate(paras) if is_heading(text)
    ]
    if not heading_indexes:
        locator = f"全文[0-{len(paras)})"
        return [SectionRange(index=1, title="(无标题)", source_locator=locator,
                             start=0, end=len(paras))]

    ranges: list[SectionRange] = []
    # 首个标题之前的前导段落数入第一个范围，避免内容遗漏
    bounds = [0] + heading_indexes
    for order, start in enumerate(bounds):
        end = bounds[order + 1] if order + 1 < len(bounds) else len(paras)
        if end <= start and order != 0:
            continue
        title = paras[start][1].strip() if order != 0 else "(前导)"
        ranges.append(SectionRange(
            index=order + 1, title=title,
            source_locator=f"{title}[{start}-{end})",
            start=start, end=end,
        ))
    return ranges


@dataclass(frozen=True)
class FamilySection:
    """A continuous range owned by one detected family."""

    family_code: str
    start: int
    end: int


def family_sections(paragraphs, evidences, *, repeat_major=()) -> list[FamilySection]:
    """Assign ranges before parsers inspect their contents.

    A detected family owns its headings and the intervening paragraphs. A
    different family or an unclaimed major heading closes that range.
    Subsection headings stay with their parent family. The first family
    receives the document's leading context (unit, title, or reading section).
    """

    starts = {}
    family_names = {}
    for evidence in evidences:
        family_names[evidence.code] = evidence.doc_type
        for position in evidence.start_positions:
            starts.setdefault(position, set()).add(evidence.code)
    titles = {family.display_name: code for code, family in FAMILY_REGISTRY.items()}
    titles.update({subtype.display_name: subtype.family for subtype in SUB_TYPE_REGISTRY.values()})
    titles = sorted(titles.items(), key=lambda entry: len(entry[0]), reverse=True)
    sections = []
    owner = None
    start = 0
    prefix_available = True
    parent_heading_seen = False
    first_subsection_seen = False

    def close(end):
        if owner is not None and end > start:
            sections.append(FamilySection(owner, start, end))

    for position, (_, text, _) in enumerate(paragraphs):
        title = text.splitlines()[0].strip()
        label = _MAJOR_ORDINAL.sub("", title)
        major = bool(MAJOR_TYPE_HEADING_RE.match(title)) or (
            bool(RE_CN_ORDINAL.match(title)) and is_major_section_heading(title)
        )
        candidates = starts.get(position, ())
        if len(candidates) == 1:
            family = next(iter(candidates))
            parent_heading = major and label.startswith(family_names[family])
            first_subsection = bool(re.match(r"^第(?:一|1)节", label))
            repeated = family in repeat_major and (
                (parent_heading and parent_heading_seen)
                or (first_subsection and first_subsection_seen)
            )
            if family == owner and not repeated:
                parent_heading_seen = parent_heading_seen or parent_heading
                first_subsection_seen = first_subsection_seen or first_subsection
                continue
            boundary = position
            if position and re.fullmatch(
                r"(?:Unit\s*|U)\d+\s*[：:]?", paragraphs[position - 1][1], re.I
            ):
                boundary -= 1
            close(boundary)
            start = 0 if prefix_available else boundary
            owner = family
            parent_heading_seen = parent_heading
            first_subsection_seen = first_subsection
        elif candidates or major:
            heading_family = next(
                (family for name, family in titles if label.startswith(name)),
                None,
            )
            if not candidates and owner is not None and owner == heading_family:
                continue
            close(position)
            owner = None
            parent_heading_seen = False
            first_subsection_seen = False
        else:
            continue
        prefix_available = False
    close(len(paragraphs))
    return sections
