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


def load_document_once(filepath, *, include_structure=False):
    """一次读取文档段落、元数据，以及可选的结构块流。

    默认返回值仍是 ``(paragraphs, metadata)``，兼容现有调用方；解析主
    链路请求 ``include_structure=True`` 时额外返回第三项 ``DocumentBlock``
    序列，供文本框/表格题型复用同一次 Word 加载。
    """

    return load_paragraphs(
        filepath,
        include_metadata=True,
        include_blocks=include_structure,
    )


def _order_detected_types_by_source(paras, detected_types):
    """按文档中的首次题型标记排列解析结果。

    ``detect_types_in_content`` 的注册顺序是稳定的检测优先级，不能直接
    改成文档顺序，否则会影响专项识别和外部调用方的兼容契约。套卷解析
    需要的却是试卷顺序：解析结果会继续进入 ``LegacyWordParser``，并由
    它分配持久化 sequence，最终决定核对页、交付页和 ZIP 中的顺序。
    因此把“识别顺序”和“输出顺序”分成两个明确步骤。

    题型标记按段落扫描而不是把全文拼成一个大字符串，避免跨段匹配
    或段落中的换行改变排序；未找到标记的扩展题型稳定地排在原检测顺序
    的末尾。
    """
    if not detected_types:
        return []

    from . import QUESTION_TYPE_MAP

    paragraph_count = len(paras)
    positions = {}
    for doc_type in detected_types:
        position = paragraph_count
        question_type = QUESTION_TYPE_MAP.get(doc_type)
        markers = getattr(question_type, "content_markers", ()) if question_type else ()
        for index, paragraph in enumerate(paras):
            text = str(paragraph[1] or "")
            if any(marker.search(text) for marker in markers):
                position = index
                break
        positions[doc_type] = position

    return [
        doc_type
        for _, doc_type in sorted(
            enumerate(detected_types),
            key=lambda pair: (positions.get(pair[1], paragraph_count), pair[0]),
        )
    ]


def _build_parser(parser_cls, filepath, preloaded):
    """优先复用已加载段落；解析器未迁移（自定义 __init__）时照旧构建。"""
    if preloaded is not None:
        try:
            params = inspect.signature(parser_cls.__init__).parameters
        except (TypeError, ValueError):
            # 某些扩展解析器（例如 C 扩展包装类）没有可反射的签名；
            # 此时沿用旧的直接构造路径。构造器本身抛出的异常不能在这里
            # 吞掉，否则真实 bug 会被伪装成一次兼容性回退并重复加载文件。
            return parser_cls(filepath)

        preloaded_param = params.get("preloaded_paras")
        accepts_kwargs = any(
            star.kind is inspect.Parameter.VAR_KEYWORD
            for star in params.values()
        )
        if preloaded_param is None and not accepts_kwargs:
            return parser_cls(filepath)

        # 三元组是结构块的扩展协议。未声明需要结构块的旧解析器
        # 继续收到原来的二元组，避免自定义解析器用二元解包时被
        # 新字段破坏；需要表格/文本框的解析器显式开启该能力。
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


def parse_document_once(filepath):
    """与 parse_document_auto 等价，但文档只加载一次。

    返回 (results_list, summary_str)，结构与 auto 路径完全一致。
    """
    import os

    from . import PARSER_MAP, detect_doc_type, detect_types_in_content

    # xlsx 词汇走专用分支（与 auto 路径一致，不经过 Word 结构读取）
    if str(filepath).lower().endswith(".xlsx"):
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
        loaded = load_document_once(filepath, include_structure=True)
        paras, metadata = loaded[:2]
        blocks = loaded[2] if len(loaded) > 2 else ()
    except Exception as exc:
        return [], f"文档加载失败: {exc}"
    if not paras:
        return [], "文档内容为空"

    detected_types = detect_types_in_content(paras)
    if not detected_types:
        return [], "未识别到任何题型内容"
    detected_types = _order_detected_types_by_source(paras, detected_types)
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
