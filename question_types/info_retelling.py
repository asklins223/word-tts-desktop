"""「信息转述及询问」题型切片：解析器与题型元数据。"""

import re

from question_types.base import BaseParser, QuestionType
from question_types.text_utils import sanitize


# ============================================================================
# 5. 信息转述及询问解析器
# ============================================================================

class InfoRetellingParser(BaseParser):
    """
    解析「信息转述及询问」文档。
    提取「第一节 信息转述」的录音稿。

    文档结构示例:
        第一节 信息转述
          ... (题目描述)
          录音稿：(W) I'm Emma...     ← 录音稿可能与标记在同一行
          参考答案：...               ← 到此结束
        第二节 询问信息               ← 或到这里结束

    支持同一文件内包含多篇试题。
    """

    DOC_TYPE = "信息转述及询问"

    RE_SECTION_START = re.compile(r'第一节\s*信息转述')
    RE_SECTION_END = re.compile(r'第二节|参考答案')
    # 任何「第X节」标记（用于检测其他题型的章节边界）
    RE_ANY_SECTION = re.compile(r'第[一二三四五六七八九十]+节')
    RE_SCRIPT = re.compile(r'录音稿\s*[：:]\s*(.*)')

    def parse(self):
        items = []
        in_section = False
        collecting = False
        current_lines = []
        script_idx = 0

        def flush():
            nonlocal collecting, current_lines, script_idx
            if collecting and current_lines:
                script_idx += 1
                items.append({
                    "category": "信息转述录音稿",
                    "index": script_idx,
                    "text": sanitize('\n'.join(current_lines)),
                })
            collecting = False
            current_lines = []

        for _, text, _ in self.paras:
            # ---- 章节开始 ----
            if self.RE_SECTION_START.search(text):
                flush()
                in_section = True
                continue

            # ---- 其他「第X节」→ 遇到其他题型的章节，停止采集 ----
            if in_section and self.RE_ANY_SECTION.search(text):
                if not self.RE_SECTION_START.search(text):
                    flush()
                    in_section = False
                    continue

            # ---- 章节结束（第二节 / 参考答案） ----
            if in_section and self.RE_SECTION_END.search(text):
                flush()
                in_section = False
                continue

            if not in_section:
                continue

            # ---- 录音稿标记 ----
            m = self.RE_SCRIPT.match(text)
            if m:
                flush()
                collecting = True
                remainder = m.group(1).strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            # ---- 收集录音稿内容 ----
            if collecting:
                current_lines.append(text)

        flush()
        return self._result(items)


QUESTION_TYPE = QuestionType(
    key="信息转述及询问",
    parser=InfoRetellingParser,
    color="#b45309",
    filename_keywords=("信息转述",),
    content_markers=(
        re.compile(r'第一节\s*信息转述'),
        re.compile(r'信息转述'),
    ),
)
