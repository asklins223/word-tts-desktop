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

# 通用标题形态（题型无关）：
#   一、二、… / （一）（二）… / 第一节… / Section A…
RE_CN_ORDINAL = re.compile(r"^[一二三四五六七八九十]+[、.．]")
RE_CN_PAREN = re.compile(r"^[（(][一二三四五六七八九十]+[)）]")
RE_SESSION = re.compile(r"^第[一二三四五六七八九十\d]+节")
RE_SECTION_AB = re.compile(r"^Section\s+[A-Z]\b", re.IGNORECASE)

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
