"""题型注册表：按题型竖切组织的解析器与元数据。

每个题型一个切片模块（解析器 + QUESTION_TYPE 元数据）。新增题型：
新建切片模块，在下方导入并把 QUESTION_TYPE 加入 QUESTION_TYPES；
解析映射、内容识别标记、展示颜色、文件名识别全部自动派生。
"""

import os

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

# 注册顺序即解析与文件名识别的固定优先级。
QUESTION_TYPES = (
    info_acquisition.QUESTION_TYPE,
    listening_selection.QUESTION_TYPE,
    listening_response.QUESTION_TYPE,
    text_reading.QUESTION_TYPE,
    info_retelling.QUESTION_TYPE,
    imitation_reading.QUESTION_TYPE,
    vocabulary.QUESTION_TYPE,
)

QUESTION_TYPE_MAP = {qt.key: qt for qt in QUESTION_TYPES}
PARSER_MAP = {qt.key: qt.parser for qt in QUESTION_TYPES}
TYPE_COLORS = {qt.key: qt.color for qt in QUESTION_TYPES}
CONTENT_MARKERS = {qt.key: tuple(qt.content_markers) for qt in QUESTION_TYPES}


def detect_doc_type(filename):
    """根据文件名自动识别文档类型，返回类型名或 None。

    Excel 文件统一归为词汇类型；其余按切片声明的关键词匹配。
    """
    lower = filename.lower()
    for question_type in QUESTION_TYPES:
        if any(lower.endswith(ext) for ext in question_type.filename_extensions):
            return question_type.key
    for question_type in QUESTION_TYPES:
        if any(keyword in filename for keyword in question_type.filename_keywords):
            return question_type.key
    return None


def detect_types_in_content(paras):
    """
    根据文档内容自动识别包含的题型。
    返回检测到的题型名称列表，保持固定顺序。
    """
    full_text = '\n'.join(text for _, text, _ in paras)
    detected = []
    for doc_type in PARSER_MAP:  # 按 PARSER_MAP 的固定顺序
        markers = CONTENT_MARKERS.get(doc_type, [])
        for marker in markers:
            if marker.search(full_text):
                detected.append(doc_type)
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
