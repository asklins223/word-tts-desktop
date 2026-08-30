"""实体定位与章节范围的归属（阶段 3④前置）。

把 ``QuestionItem``/``Stimulus``/``ContentUnit`` 的 ``source_locator``
归入 ``SectionRange``，形成 实体 → 章节 → 原文段落 的完整回溯链
（方案 9.1：定位必须可精确回溯）。归属规则：实体 locator 的文本前缀
命中范围标题即归属；未命中归入 "(前导)" 或 "(无标题)" 兜底范围。

与抽取器的组合用法::

    paras, meta = load_document_once(path)
    ranges = slice_sections(paras)
    candidate = extract_candidate(doc_type, result, source_key)
    traced = trace_entities_to_sections(candidate, ranges)
    # 每个 (实体, 范围) 对都可回溯到原文区间
"""

from __future__ import annotations


def _locator_matches(locator: str, title: str) -> bool:
    if not title or title in ("(前导)", "(无标题)"):
        return False
    return title.split("/")[0].strip() in locator


def trace_entities_to_sections(candidate, ranges) -> list[dict]:
    """返回候选中每个实体及其归属章节范围（未命中记 None 兜底）。"""
    fallback = next((r for r in ranges if r.title == "(前导)"), None) or \
        next((r for r in ranges if r.title == "(无标题)"), None)
    traced = []
    for entity in candidate.entities:
        locator = entity.source_locator
        matched = None
        for r in ranges:
            if _locator_matches(locator, r.title):
                matched = r
                break
        if matched is None:
            matched = fallback
        traced.append({
            "entity_id": getattr(entity, "question_id", None)
            or getattr(entity, "stimulus_id", None)
            or getattr(entity, "content_unit_id", None),
            "entity_locator": locator,
            "section_locator": matched.source_locator if matched else None,
            "section_range": (matched.start, matched.end) if matched else None,
        })
    return traced
