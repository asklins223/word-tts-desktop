"""题型注册表：解析器绑定与派生视图。

题型元数据的**唯一权威**在 ``question_model.model`` 的
``FAMILY_REGISTRY``（大题型展示/检测/兼容视图）与 ``SUB_TYPE_REGISTRY``
（小题型能力/音色/命名/状态）。本模块只做一件事：把解析器类绑定到
family code，其余（QuestionType、颜色表、内容标记、文件名/内容检测、
旧链路女声兼容）全部派生——不存在需要多处同步、漏改即报错的
第二份注册表（方案 2A category 解耦；方案目标 5：新增题型只改一处）。

新增大题型的完整步骤：
1. ``question_model/model.py`` 注册 ``QuestionFamily`` + 小题型
   ``QuestionSubType``（能力/音色/命名）；
2. 新建解析器切片（``BaseParser`` 子类）；
3. 在下方 ``PARSERS_BY_FAMILY`` 加一行绑定。
检测、颜色、旧链路音色、原子模型候选抽取（补 EXTRACTORS 一行或
享受温和降级）全部自动生效，无需改动任何下游代码。
"""

import os

from question_model.model import FAMILY_REGISTRY  # 唯一权威注册表

from .base import BaseParser, QuestionType  # noqa: F401
from .text_utils import (  # noqa: F401
    clean_whitespace,
    is_chinese,
    load_paragraphs,
    remove_zero_width,
    sanitize,
    split_sentences,
)

from . import (  # noqa: F401
    imitation_reading,
    info_acquisition,
    info_retelling,
    listening_response,
    listening_selection,
    text_reading,
    vocabulary,
)

from .imitation_reading import ImitationReadingParser  # noqa: F401
from .info_acquisition import InfoAcquisitionParser  # noqa: F401
from .info_retelling import InfoRetellingParser  # noqa: F401
from .listening_response import ListeningResponseParser  # noqa: F401
from .listening_selection import ListeningSelectionParser  # noqa: F401
from .text_reading import TextReadingParser  # noqa: F401
from .vocabulary import ExcelVocabularyParser  # noqa: F401

# 解析器绑定：family code → 解析器类（唯一的手写 join 点）
PARSERS_BY_FAMILY = {
    "info_acquisition": InfoAcquisitionParser,
    "listening_choice": ListeningSelectionParser,
    "listening_response": ListeningResponseParser,
    "text_reading": TextReadingParser,
    "info_retelling": InfoRetellingParser,
    "imitation_reading": ImitationReadingParser,
    "vocabulary": ExcelVocabularyParser,
}

# ---- 以下全部由 FAMILY_REGISTRY 派生；注册顺序即检测优先级 ----

QUESTION_TYPES = tuple(
    QuestionType(
        key=family.display_name,
        parser=PARSERS_BY_FAMILY[code],
        color=family.color,
        filename_keywords=family.filename_keywords,
        filename_extensions=family.filename_extensions,
        content_markers=family.content_markers,
        # 旧链路 force_female 兼容视图（新代码用 SUB_TYPE_REGISTRY.voice_policy）
        force_female_categories=family.female_categories,
    )
    for code, family in FAMILY_REGISTRY.items()
    if code in PARSERS_BY_FAMILY
)

QUESTION_TYPE_MAP = {qt.key: qt for qt in QUESTION_TYPES}
PARSER_MAP = {qt.key: qt.parser for qt in QUESTION_TYPES}
TYPE_COLORS = {qt.key: qt.color for qt in QUESTION_TYPES}
CONTENT_MARKERS = {qt.key: tuple(qt.content_markers) for qt in QUESTION_TYPES}


def detect_doc_type(filename):
    """根据文件名自动识别文档类型，返回类型名或 None。

    Excel 文件统一归为词汇类型；其余按 family 注册表的扩展名/关键词匹配。
    """
    lower = filename.lower()
    for family in FAMILY_REGISTRY.values():
        if any(lower.endswith(ext) for ext in family.filename_extensions):
            return family.display_name
    for family in FAMILY_REGISTRY.values():
        if any(keyword in filename for keyword in family.filename_keywords):
            return family.display_name
    return None


def detect_types_in_content(paras):
    """
    根据文档内容自动识别包含的题型。
    返回检测到的题型名称列表，保持固定顺序。
    """
    full_text = '\n'.join(text for _, text, _ in paras)
    detected = []
    for family in FAMILY_REGISTRY.values():  # 注册顺序即固定优先级
        for marker in family.content_markers:
            if marker.search(full_text):
                detected.append(family.display_name)
                break
    return detected


def parse_document_auto(filepath):
    """
    自动检测文档类型并解析，支持包含多个题型的文档。

    对上传的文档运行所有匹配的解析器，收集非空结果。
    返回 (results_list, summary_str)。

    对于 .xlsx 文件，直接使用 ExcelVocabularyParser 解析。
    """
    filename = os.path.basename(filepath)

    # ---- Excel 文件直接走词汇解析器 ----
    if filename.lower().endswith('.xlsx'):
        doc_type = detect_doc_type(filename)
        if doc_type is None:
            return [], "未识别到任何题型内容"
        parser_cls = PARSER_MAP.get(doc_type)
        if parser_cls is None:
            return [], f"未找到题型 {doc_type} 的解析器"
        try:
            parser = parser_cls(filepath)
            result = parser.parse()
        except Exception as e:
            return [], f"解析失败: {e}"
        if result["item_count"] == 0:
            return [], "未提取到任何内容"
        return [result], f"检测到 1 种题型，成功提取 {result['item_count']} 条内容"

    # ---- Word 文档走原有逻辑 ----
    try:
        paras = load_paragraphs(filepath)
    except Exception as e:
        return [], f"文档加载失败: {e}"

    if not paras:
        return [], "文档内容为空"

    detected_types = detect_types_in_content(paras)

    if not detected_types:
        return [], "未识别到任何题型内容"

    results = []
    errors = []
    for doc_type in detected_types:
        parser_cls = PARSER_MAP.get(doc_type)
        if parser_cls is None:
            continue
        try:
            parser = parser_cls(filepath)
            result = parser.parse()
            if result["item_count"] > 0:
                results.append(result)
        except Exception as e:
            errors.append(f"{doc_type}: {e}")

    total = sum(r["item_count"] for r in results)
    parts = [f"检测到 {len(detected_types)} 种题型"]
    if results:
        parts.append(f"成功提取 {total} 条内容")
    if errors:
        parts.append(f"{len(errors)} 种解析出错")
    summary = "，".join(parts)

    return results, summary
