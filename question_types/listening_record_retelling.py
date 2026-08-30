"""「听后记录并转述信息」题型切片。

这类文档的可配音内容是「第一节 听后记录」中的英文听力短文，
通常以 ``(W)`` / ``(M)`` 标记说话人。中文操作提示、表格答案和
第二节的转述参考答案都不是音频正文，因此只在明确的听力正文边界
内收集带说话人标记的英文段落。
"""

import re

from question_types.base import BaseParser
from question_types.text_utils import is_chinese, sanitize


class ListeningRecordRetellingParser(BaseParser):
    """提取「听后记录并转述信息」第一节的听力短文。"""

    DOC_TYPE = "听后记录并转述信息"

    # 标题可能带有“（共...）”等说明，但“第一节 听后记录”是稳定边界。
    RE_SECTION_START = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第一节\s*[：:]?\s*听后记录(?:\s|[：:（(【]|$)"
    )
    # 第二节的标题和“参考答案/答题区域”都表示听力正文已经结束。
    RE_SECTION_END = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第二节(?:\s*[：:]?\s*信息转述)?"
        r"|^\s*(?:参考答案|答题区域)"
    )
    RE_ANY_SECTION = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第[一二三四五六七八九十]+节"
    )
    RE_SCRIPT_PREFIX = re.compile(r"^\s*录音稿\s*[：:]\s*(.*)$")
    RE_SPEAKER = re.compile(r"^\s*(?:[WwMm]\s*[:：]|\([WwMm]\))")
    RE_TYPE_TITLE = re.compile(r"听后记录并转述信息")
    RE_CONTROL = re.compile(
        r"计算机|屏幕|答题区域|参考答案|答题时间|倒计时|"
        r"开始答题|停止转述|听短文|转述准备|完成转述",
        re.I,
    )

    @classmethod
    def _is_script_boundary(cls, value: str) -> bool:
        """判断当前段落是否是中文操作提示，而不是英文听力正文。"""
        return bool(cls.RE_CONTROL.search(value) or is_chinese(value))

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
                    "category": "听后记录并转述信息录音稿",
                    "index": script_idx,
                    "text": sanitize("\n".join(current_lines)),
                })
            collecting = False
            current_lines = []

        for _, text, _ in self.paras:
            value = str(text or "").strip()
            if not value:
                continue

            if self.RE_SECTION_START.search(value):
                flush()
                in_section = True
                continue

            if self.RE_SECTION_END.search(value):
                flush()
                in_section = False
                continue

            # 没有规范章节编号的同类资料仍可通过标题进入正文扫描，
            # 但不会把“第二节”的转述参考答案带进音频。
            if self.RE_TYPE_TITLE.search(value):
                if not in_section:
                    in_section = True
                continue

            if in_section and self.RE_ANY_SECTION.search(value):
                flush()
                in_section = False
                continue

            if not in_section:
                continue

            script_prefix = self.RE_SCRIPT_PREFIX.match(value)
            if script_prefix:
                flush()
                collecting = True
                remainder = script_prefix.group(1).strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            if collecting:
                if self._is_script_boundary(value):
                    flush()
                    continue
                current_lines.append(value)
                continue

            # 示例文档没有“录音稿：”标签，正文直接从 (W)/(M) 开始。
            if self.RE_SPEAKER.match(value):
                collecting = True
                current_lines.append(value)

        flush()
        return self._result(items)
