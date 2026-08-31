"""「听后记录并转述信息」题型切片。

这类文档的可配音内容是「第一节 听后记录」中的英文听力短文，
通常以 ``(W)`` / ``(M)`` 标记说话人。中文操作提示、表格答案和
第二节的转述参考答案都不是音频正文，因此只在明确的听力正文边界
内收集带说话人标记的英文段落。
"""

import re

from audio_naming import audio_filename_stem, is_exam_paper_bundle
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
        r"|^\s*[【\[（(]?\s*(?:参考答案|答题区域)"
    )
    RE_ANY_SECTION = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第[一二三四五六七八九十]+节"
    )
    RE_SCRIPT_PREFIX = re.compile(r"^\s*录音稿\s*[：:]\s*(.*)$")
    RE_SPEAKER = re.compile(r"^\s*(?:[WwMm]\s*[:：]|\([WwMm]\))")
    RE_PLAIN_SCRIPT_TRIGGER = re.compile(
        r"(?:听短文|短文听)(?:一|两|三|四|五|\d+)?遍"
    )
    RE_INLINE_CONTROL = re.compile(
        r"(?:[（(【\[]\s*)?(?:计算机|屏幕显示|答题区域|参考答案|"
        r"开始答题|停止转述)"
    )
    RE_TYPE_TITLE = re.compile(r"听后记录并转述信息")
    RE_CONTROL = re.compile(
        r"计算机|屏幕|答题区域|参考答案|答题时间|倒计时|"
        r"开始答题|停止转述|听短文|转述准备|完成转述",
        re.I,
    )
    RE_QUESTION_NUMBER = re.compile(r"(?<!\d)(\d+)\s*[.．、）)]")

    @classmethod
    def _is_script_boundary(cls, value: str) -> bool:
        """判断当前段落是否是中文操作提示，而不是英文听力正文。"""
        return bool(cls.RE_CONTROL.search(value) or is_chinese(value))

    @classmethod
    def _script_payload_before_control(cls, value: str) -> str:
        """截取同一段落中控制提示之前的英文听力正文。"""
        match = cls.RE_INLINE_CONTROL.search(value)
        if match is None or not value[:match.start()].strip():
            return value.strip()
        # 英文正文末尾的句号属于朗读内容，不能和控制提示的分隔符一起删掉。
        return value[:match.start()].rstrip()

    def parse(self):
        items = []
        in_section = False
        collecting = False
        plain_script_pending = False
        current_lines = []
        script_idx = 0
        record_question_numbers = []
        use_exam_naming = is_exam_paper_bundle(self.paras)

        def flush():
            nonlocal collecting, plain_script_pending, current_lines, script_idx
            if collecting and current_lines:
                script_idx += 1
                items.append({
                    "category": "听后记录并转述信息录音稿",
                    "index": script_idx,
                    "text": sanitize("\n".join(current_lines)),
                })
            collecting = False
            plain_script_pending = False
            current_lines = []

        for _, text, _ in self.paras:
            value = str(text or "").strip()
            if not value:
                continue

            # “答题区域”本身会先结束第一节，参考答案常在下一段才出现；
            # 只要已经提取到本题听力稿，就继续读取这一行中的实际题号。
            if items and "参考答案" in value:
                for match in self.RE_QUESTION_NUMBER.finditer(value):
                    number = int(match.group(1))
                    if number not in record_question_numbers:
                        record_question_numbers.append(number)

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
                plain_script_pending = False
                remainder = script_prefix.group(1).strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            if self.RE_PLAIN_SCRIPT_TRIGGER.search(value):
                flush()
                plain_script_pending = True
                continue

            if collecting:
                payload = self._script_payload_before_control(value)
                if payload != value.strip():
                    if payload and not is_chinese(payload):
                        current_lines.append(payload)
                    flush()
                    continue
                if self._is_script_boundary(value):
                    flush()
                    continue
                current_lines.append(value)
                continue

            # 示例文档没有“录音稿：”标签，正文直接从 (W)/(M) 开始。
            if self.RE_SPEAKER.match(value):
                collecting = True
                plain_script_pending = False
                current_lines.append(value)
                continue

            if plain_script_pending:
                payload = self._script_payload_before_control(value)
                if payload and not self._is_script_boundary(payload):
                    collecting = True
                    plain_script_pending = False
                    current_lines.append(payload)

        flush()
        if use_exam_naming and items:
            type_path = ["听后记录并转述信息", "第一节听后记录"]
            for ordinal, item in enumerate(items, start=1):
                filename_stem = audio_filename_stem(type_path, ordinal)
                item.update({
                    "type_path": type_path,
                    "filename_stem": filename_stem,
                    "audio_filename_stem": filename_stem,
                })
            if record_question_numbers:
                items[-1]["question_numbers"] = list(record_question_numbers)
        return self._result(items)
