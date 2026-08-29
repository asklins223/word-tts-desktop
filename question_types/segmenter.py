"""统一结构读取与分段（阶段 3 第一步）。

现状：每个题型的 Parser 各自调用 ``load_paragraphs`` 重复加载并扫描
全文（方案风险 13）。本模块先收口"结构读取"这一层：

- ``load_document_once``：文档只加载一次，产出段落与元数据；
- ``parse_document_once``：与 ``parse_document_auto`` 同判型、同解析器、
  同输出结构，但所有解析器复用同一次加载结果（输出必须与既有路径
  逐字节一致，测试以解析基线为对照）；
- 后续分段切片（大题/材料范围划分 + 一次 owner 裁决）在此模块上继续
  演进，最终替代各 Parser 对全文的独立重扫。
"""

from __future__ import annotations

import inspect

from .text_utils import load_paragraphs


def load_document_once(filepath):
    """一次读取文档段落与元数据。"""
    return load_paragraphs(filepath, include_metadata=True)


def _build_parser(parser_cls, filepath, preloaded):
    """优先复用已加载段落；解析器未迁移（自定义 __init__）时照旧构建。"""
    if preloaded is not None:
        try:
            params = inspect.signature(parser_cls.__init__).parameters
            if "preloaded_paras" in params or any(
                star.kind is inspect.Parameter.VAR_KEYWORD
                for star in params.values()
            ):
                return parser_cls(filepath, preloaded_paras=preloaded)
        except (TypeError, ValueError):
            pass
    return parser_cls(filepath)


def parse_document_once(filepath):
    """与 parse_document_auto 等价，但文档只加载一次。

    返回 (results_list, summary_str)，结构与 auto 路径完全一致。
    """
    import os

    from . import PARSER_MAP, detect_doc_type, detect_types_in_content

    # xlsx 词汇走专用分支（与 auto 路径一致，不经过 Word 结构读取）
    if filepath.lower().endswith(".xlsx"):
        doc_type = detect_doc_type(os.path.basename(filepath))
        if doc_type is None:
            return [], "未识别到任何题型内容"
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
        paras, metadata = load_document_once(filepath)
    except Exception as exc:
        return [], f"文档加载失败: {exc}"
    if not paras:
        return [], "文档内容为空"

    detected_types = detect_types_in_content(paras)
    if not detected_types:
        return [], "未识别到任何题型内容"
    results = []
    errors = []
    preloaded = (paras, metadata)
    for doc_type in detected_types:
        parser_cls = PARSER_MAP.get(doc_type)
        if parser_cls is None:
            continue
        try:
            parser = _build_parser(parser_cls, filepath, preloaded)
            result = parser.parse()
            if result["item_count"] > 0:
                results.append(result)
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
