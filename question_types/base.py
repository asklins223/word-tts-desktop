"""题型基类与 QuestionType 元数据定义。"""

import os
from dataclasses import dataclass

from audio_naming import is_exam_paper_bundle
from question_types.text_utils import load_paragraphs, split_marker_paragraphs


@dataclass(frozen=True)
class QuestionType:
    """一个题型的全部静态定义。

    题型语义来自 question_model 的注册表，本类是绑定解析器后的兼容
    视图；解析映射、展示颜色和音色策略共用同一份注册事实。
    """

    key: str                          # 题型名，如 "信息获取"
    parser: type                      # BaseParser 子类
    color: str                        # 前端展示颜色
    content_markers: tuple = ()       # 结构检测的注册表候选标记（非单独判据）
    force_female_categories: tuple = ()  # 声线策略中的默认女声 category


# ============================================================================
# 解析器基类
# ============================================================================

class BaseParser:
    """文档解析器基类，子类需实现 parse() 方法。"""

    DOC_TYPE = "未知"
    # 子类可设为 True 以跳过 load_paragraphs（如 Excel 解析器）
    _SKIP_LOAD_PARAGRAPHS = False
    # 只有需要读取表格/文本框的解析器才在独立构造时建立结构块；
    # 统一分段器会把结构块注入所有解析器，避免普通专项解析重复扫描。
    _REQUIRES_DOCUMENT_BLOCKS = False
    _REPEAT_MAJOR_SECTIONS = False

    def __init__(self, filepath, *, preloaded_paras=None):
        # 统一分段器：可注入已加载的段落（文档只读一次），缺省时自行
        # 加载。预加载协议支持
        # (段落列表, 元数据列表) 与
        # (段落列表, 元数据列表, DocumentBlock 列表)。
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        if preloaded_paras is not None:
            self.paras = preloaded_paras[0]
            self.paragraph_metadata = preloaded_paras[1]
            self.document_blocks = (
                preloaded_paras[2]
                if len(preloaded_paras) > 2
                else ()
            )
        elif self._SKIP_LOAD_PARAGRAPHS:
            self.paras = []
            self.paragraph_metadata = []
            self.document_blocks = ()
        else:
            loaded = load_paragraphs(
                filepath,
                include_metadata=True,
                include_blocks=self._REQUIRES_DOCUMENT_BLOCKS,
            )
            self.paras, self.paragraph_metadata = loaded[:2]
            self.document_blocks = loaded[2] if len(loaded) > 2 else ()
            self.paras, self.paragraph_metadata = split_marker_paragraphs(
                self.paras, self.paragraph_metadata,
            )
        self.document_header = self.paras[:8]
        self.use_exam_naming = is_exam_paper_bundle(self.paras)

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
