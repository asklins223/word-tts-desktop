"""Read source structure once and dispatch ranges to their owning parsers."""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from audio_naming import is_exam_paper_bundle
from .detection import PaperCategoryEvidence, classify_paper_category, detect_document_types
from .section_slice import family_sections
from .text_utils import load_paragraphs, split_marker_paragraphs


# Keep workflow snapshots, the atomic model, and the legacy cache on the same version.
PARSER_VERSION = 26


@dataclass(frozen=True)
class SourceParse:
    results: list[dict]
    summary: str
    detected_family_codes: frozenset[str]
    issues: tuple[str, ...] = ()


def load_document_once(filepath, *, include_structure=False):
    """一次读取文档段落、元数据，以及可选的结构块流。

    默认返回 ``(paragraphs, metadata)``；请求
    ``include_structure=True`` 时额外返回第三项 ``DocumentBlock`` 序列，
    供文本框/表格题型复用同一次 Word 加载。
    """

    return load_paragraphs(
        filepath,
        include_metadata=True,
        include_blocks=include_structure,
    )


def _build_parser(parser_cls, filepath, preloaded):
    """优先复用已加载段落；扩展解析器可声明自己的构造协议。"""
    if preloaded is not None:
        try:
            params = inspect.signature(parser_cls.__init__).parameters
        except (TypeError, ValueError):
            # 某些扩展解析器（例如 C 扩展包装类）没有可反射的签名；
            # 此时直接构造。构造器本身抛出的异常不能在这里吞掉，否则
            # 真实 bug 会被伪装成一次静默回退并重复加载文件。
            return parser_cls(filepath)

        preloaded_param = params.get("preloaded_paras")
        accepts_kwargs = any(
            star.kind is inspect.Parameter.VAR_KEYWORD
            for star in params.values()
        )
        if preloaded_param is None and not accepts_kwargs:
            return parser_cls(filepath)

        # 三元组是结构块的扩展协议。未声明需要结构块的解析器仍收到
        # 二元组；需要表格/文本框的解析器显式开启该能力。
        parser_preloaded = preloaded
        if (
            len(preloaded) > 2
            and not getattr(parser_cls, "_REQUIRES_DOCUMENT_BLOCKS", False)
        ):
            parser_preloaded = preloaded[:2]

        if (
            preloaded_param is not None
            and preloaded_param.kind is inspect.Parameter.POSITIONAL_ONLY
        ):
            return parser_cls(filepath, parser_preloaded)
        return parser_cls(
            filepath,
            preloaded_paras=parser_preloaded,
        )
    return parser_cls(filepath)


def _annotate_paper_category(
    result: Mapping[str, object],
    decision: PaperCategoryEvidence,
) -> dict[str, object]:
    """Attach one document-level category decision to every parsed item."""

    if decision.status == "not_applicable":
        return dict(result)

    evidence = decision.to_dict()
    annotated = dict(result)
    annotated["exam_form"] = decision.exam_form
    annotated["paper_category_status"] = decision.status
    annotated["paper_category_evidence"] = evidence
    if decision.paper_category is not None:
        annotated["paper_category"] = decision.paper_category

    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        return annotated
    items: list[object] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            items.append(raw_item)
            continue
        item = dict(raw_item)
        item["exam_form"] = decision.exam_form
        item["paper_category_status"] = decision.status
        item["paper_category_evidence"] = evidence
        if decision.paper_category is not None:
            item["paper_category"] = decision.paper_category
        items.append(item)
    annotated["items"] = items
    return annotated


def _source_range(result, first, last, *, disambiguate):
    """Preserve global source positions and scope any reused item IDs/stems."""

    locator = f"段落 {first + 1}–{last + 1}"
    result["source_range"] = locator
    if disambiguate:
        result["scope_key"] = f"p{first + 1}"
        suffix = f"-{result['scope_key']}"
        visited = set()

        def scope_identities(value):
            if isinstance(value, dict):
                if id(value) in visited:
                    return
                visited.add(id(value))
                for key, entry in value.items():
                    if key.endswith("filename_stem") and isinstance(entry, str) and entry:
                        value[key] = entry[:240 - len(suffix)] + suffix
                    elif key == "unit_id" and isinstance(entry, str) and entry:
                        value[key] = f"{result['scope_key']}:{entry}"[:256]
                    else:
                        scope_identities(entry)
            elif isinstance(value, (tuple, list)):
                for entry in value:
                    scope_identities(entry)

        # Compatibility views can share dicts with individual items.
        scope_identities(result)
    for collection in ("items", "audio_items"):
        for index, item in enumerate(result.get(collection, ()), start=1):
            item.setdefault("source_locator", f"{locator}/{item.get('category') or collection}/{index}")
    return result


def parse_document_once(filepath):
    """Compatibility view used by the CLI and existing callers."""

    parsed = parse_source(filepath)
    return parsed.results, parsed.summary


def parse_source(filepath) -> SourceParse:
    from . import PARSERS_BY_FAMILY

    if str(filepath).lower().endswith(".xlsx"):
        parser = PARSERS_BY_FAMILY["vocabulary"]
        try:
            result = parser(filepath).parse(require_headers=False)
        except Exception as exc:
            return SourceParse([], f"解析失败: {exc}", frozenset())
        if result is None:
            return SourceParse([], "未识别到任何题型内容", frozenset())
        codes = frozenset({"vocabulary"})
        if result["item_count"] == 0:
            return SourceParse([], "未提取到任何内容", codes)
        return SourceParse([result], f"检测到 1 种题型，成功提取 {result['item_count']} 条内容", codes)

    try:
        paras, metadata, blocks = load_document_once(filepath, include_structure=True)
        paras, metadata = split_marker_paragraphs(paras, metadata)
    except Exception as exc:
        return SourceParse([], f"文档加载失败: {exc}", frozenset())
    if not paras:
        return SourceParse([], "文档内容为空", frozenset())

    detected_evidence = detect_document_types(
        paragraphs=paras,
        paragraph_metadata=metadata,
        blocks=blocks,
    )
    if not detected_evidence:
        return SourceParse([], "未识别到任何题型内容", frozenset())
    codes = frozenset(evidence.code for evidence in detected_evidence)
    paper_category = classify_paper_category(
        paras,
        evidences=detected_evidence,
    )
    results = []
    errors = []
    issues = []
    sections = family_sections(
        paras, detected_evidence,
        repeat_major={
            code for code, parser in PARSERS_BY_FAMILY.items()
            if parser._REPEAT_MAJOR_SECTIONS
        },
    )
    section_counts = Counter(section.family_code for section in sections)
    use_exam_naming = is_exam_paper_bundle(paras)
    block_positions = [(block.metadata.get("paragraph_index", -1), block) for block in blocks]
    for section in sections:
        parser_cls = PARSERS_BY_FAMILY.get(section.family_code)
        if parser_cls is None:
            continue
        first = paras[section.start][0]
        end = paras[section.end][0] if section.end < len(paras) else float("inf")
        scoped_blocks = tuple(block for index, block in block_positions if first <= index < end)
        preloaded = (paras[section.start:section.end], metadata[section.start:section.end], scoped_blocks)
        try:
            parser = _build_parser(parser_cls, filepath, preloaded)
            parser.document_header = paras[:8]
            parser.use_exam_naming = use_exam_naming
            result = parser.parse()
            if result.get("source_issue"):
                issues.append(str(result["source_issue"]))
            if result["item_count"] > 0:
                result = _source_range(
                    result, first, paras[section.end - 1][0],
                    disambiguate=section_counts[section.family_code] > 1,
                )
                results.append(_annotate_paper_category(result, paper_category))
            else:
                issues.append(f"第 {first + 1}–{paras[section.end - 1][0] + 1} 段（{parser_cls.DOC_TYPE}）未提取到内容")
        except Exception as exc:
            issue = f"第 {first + 1} 段开始的{parser_cls.DOC_TYPE}解析失败：{exc}"
            errors.append(issue)
            issues.append(issue)

    total = sum(r["item_count"] for r in results)
    parts = [f"检测到 {len(codes)} 种题型"]
    if results:
        parts.append(f"成功提取 {total} 条内容")
    if errors:
        parts.append(f"{len(errors)} 种解析出错")
    elif issues:
        parts.append(f"{len(issues)} 处内容需核对")
    summary = "，".join(parts)
    return SourceParse(results, summary, codes, tuple(issues))
