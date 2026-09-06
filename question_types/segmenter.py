"""统一结构读取与分段（阶段 3 第一步）。

现状：每个题型的 Parser 各自调用 ``load_paragraphs`` 重复加载并扫描
全文（方案风险 13）。本模块收口“结构读取”和题型路由：

- ``load_document_once``：文档只加载一次，产出段落与元数据；
- ``parse_document_once``：所有解析器复用同一次加载结果，并按结构证据
  路由题型；
- 后续分段切片（大题/材料范围划分 + 一次 owner 裁决）在此模块上继续
  演进，最终替代各 Parser 对全文的独立重扫。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping

from .detection import PaperCategoryEvidence, classify_paper_category
from .text_utils import load_paragraphs


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


def parse_document_once(filepath):
    """一次加载、结构判型并解析文档。

    返回 ``(results_list, summary_str)``。
    """
    from . import PARSER_MAP
    from .detection import detect_document_types

    # xlsx 词汇走专用分支（不经过 Word 结构读取）
    if str(filepath).lower().endswith(".xlsx"):
        detected = detect_document_types(filepath)
        if len(detected) != 1:
            return [], "未识别到任何题型内容"
        doc_type = detected[0].doc_type
        parser = PARSER_MAP.get(doc_type)
        if parser is None:
            return [], f"未找到题型 {doc_type} 的解析器"
        try:
            result = parser(filepath).parse()
        except Exception as exc:
            return [], f"解析失败: {exc}"
        if result["item_count"] == 0:
            return [], "未提取到任何内容"
        return [result], f"检测到 1 种题型，成功提取 {result['item_count']} 条内容"

    try:
        loaded = load_document_once(filepath, include_structure=True)
        paras, metadata = loaded[:2]
        blocks = loaded[2] if len(loaded) > 2 else ()
    except Exception as exc:
        return [], f"文档加载失败: {exc}"
    if not paras:
        return [], "文档内容为空"

    detected_evidence = detect_document_types(
        paragraphs=paras,
        paragraph_metadata=metadata,
        blocks=blocks,
    )
    if not detected_evidence:
        return [], "未识别到任何题型内容"
    detected_types = [evidence.doc_type for evidence in detected_evidence]
    paper_category = classify_paper_category(
        paras,
        evidences=detected_evidence,
    )
    results = []
    errors = []
    preloaded = (paras, metadata, blocks)
    for doc_type in detected_types:
        parser_cls = PARSER_MAP.get(doc_type)
        if parser_cls is None:
            continue
        try:
            parser = _build_parser(parser_cls, filepath, preloaded)
            result = parser.parse()
            if result["item_count"] > 0:
                results.append(_annotate_paper_category(result, paper_category))
        except Exception as exc:
            errors.append(f"{doc_type}: {exc}")

    total = sum(r["item_count"] for r in results)
    parts = [f"检测到 {len(detected_types)} 种题型"]
    if results:
        parts.append(f"成功提取 {total} 条内容")
    if errors:
        parts.append(f"{len(errors)} 种解析出错")
    summary = "，".join(parts)
    return results, summary
