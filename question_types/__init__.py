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

from question_model.model import FAMILY_REGISTRY  # 唯一权威注册表

from .base import BaseParser, QuestionType  # noqa: F401
from .text_utils import (  # noqa: F401
    DocumentBlock,
    ANSWER_MARKER_RE,
    MAJOR_SECTION_RE,
    MAJOR_TYPE_HEADING_RE,
    SCRIPT_MARKER_RE,
    clean_whitespace,
    is_chinese,
    is_major_section_heading,
    load_paragraphs,
    match_answer_marker,
    match_script_marker,
    remove_zero_width,
    sanitize,
    split_sentences,
)

from . import (  # noqa: F401
    imitation_reading,
    info_acquisition,
    info_retelling,
    listening_record_retelling,
    listening_response,
    listening_selection,
    text_reading,
    vocabulary,
)

from .imitation_reading import ImitationReadingParser  # noqa: F401
from .info_acquisition import InfoAcquisitionParser  # noqa: F401
from .info_retelling import InfoRetellingParser  # noqa: F401
from .listening_record_retelling import ListeningRecordRetellingParser  # noqa: F401
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
    "listening_record_retelling": ListeningRecordRetellingParser,
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
    """自动检测并解析文档的兼容入口。

    ``parse_document_once`` 现在是唯一的 Word 结构读取与题型路由实现；
    保留本函数名，确保旧调用方和工作流 API 不需要迁移，也避免自动入口
    与一次加载入口在套卷、文本框等新格式上产生分叉。
    """

    from .segmenter import parse_document_once

    return parse_document_once(filepath)
