"""题型基类与 QuestionType 元数据定义。"""

import os
from dataclasses import dataclass

from question_types.text_utils import load_paragraphs


@dataclass(frozen=True)
class QuestionType:
    """一个题型的全部静态定义。

    新增题型时在对应切片模块中定义解析器和 QUESTION_TYPE，并到
    question_types/__init__.py 注册；解析映射、内容识别、展示颜色、
    文件名识别和音色策略都从这里派生，不再散落在多个文件中。
    """

    key: str                          # 题型名，如 "信息获取"
    parser: type                      # BaseParser 子类
    color: str                        # 前端展示颜色
    filename_keywords: tuple = ()     # detect_doc_type 的文件名包含关键词
    filename_extensions: tuple = ()   # detect_doc_type 的扩展名匹配（优先于关键词）
    content_markers: tuple = ()       # detect_types_in_content 的内容标记正则
    force_female_categories: tuple = ()  # 强制默认女声的 category（如词汇的 单词/例句）


# ============================================================================
# 解析器基类
# ============================================================================

class BaseParser:
    """文档解析器基类，子类需实现 parse() 方法。"""

    DOC_TYPE = "未知"
    # 子类可设为 True 以跳过 load_paragraphs（如 Excel 解析器）
    _SKIP_LOAD_PARAGRAPHS = False

    def __init__(self, filepath):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        if self._SKIP_LOAD_PARAGRAPHS:
            self.paras = []
            self.paragraph_metadata = []
        else:
            self.paras, self.paragraph_metadata = load_paragraphs(
                filepath,
                include_metadata=True,
            )

    def parse(self):
        """子类实现：返回解析结果字典"""
        raise NotImplementedError

    def _result(self, items):
        """构造标准输出结构"""
        return {
            "source_file": self.filename,
            "doc_type": self.DOC_TYPE,
            "item_count": len(items),
            "items": items,
        }
