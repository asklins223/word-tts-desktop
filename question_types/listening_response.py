"""「听后应答」题型切片：解析器与题型元数据。"""

import re

from audio_naming import audio_filename_stem, is_exam_paper_bundle
from question_types.base import BaseParser
from question_types.text_utils import is_chinese, sanitize


# ============================================================================
# 3. 听后应答解析器
# ============================================================================

class ListeningResponseParser(BaseParser):
    """解析「听后应答」中的计算机语音提示与待朗读句子。

    每个“（计算机语音提示）听下面 N 个句子。”提示开启一个采集块；
    采集块只包含该提示下方、直到“请朗读应答语”/倒计时/下一条提示
    之前的英文内容。内容按非空行输出为独立音频，全部使用默认女声。
    """

    DOC_TYPE = "听后应答"

    RE_SECTION_START = re.compile(r'听后应答')
    RE_PROMPT = re.compile(
        r'计算机语音提示.*?听下面\s*'
        r'(?P<count>[0-9０-９零〇一二两三四五六七八九十百]+)\s*个\s*句子',
        re.I,
    )
    RE_CONTROL = re.compile(
        r'计算机语音提示.*?(?:请朗读应答语|朗读应答语)|'
        r'计算机屏幕.*?(?:倒计时|进度条)|'
        r'(?:参考答案|答案|解析)\s*[：:]?',
        re.I,
    )
    RE_LEADING_MARK = re.compile(r'^[★☆*]\s*')
    RE_LEADING_NUMBER = re.compile(r'^\d+\s*[.．、）)]\s*')
    RE_ANSWER_OPTION_NUMBER = re.compile(
        r'^\s*(?P<number>\d+)\s*[.．、）)]\s*[★☆*]'
    )
    RE_ANSWER_OPTIONS = re.compile(r'^\s*[★☆*]')
    RE_ANSWER_PROMPT = re.compile(r'请朗读应答语|朗读应答语', re.I)
    RE_GRADE = re.compile(
        r'(?<!\d)(?P<grade>[789七八九])\s*(?:年级\s*)?上',
        re.I,
    )

    _CHINESE_DIGITS = {
        '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3,
        '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
    }
    _CHINESE_UNITS = {'十': 10, '百': 100}
    _GRADE_DIGITS = {'七': '7', '八': '8', '九': '9'}

    @classmethod
    def _parse_count(cls, value):
        """将阿拉伯数字或常见中文数字转换为句子数量。"""
        raw = str(value or '').strip().translate(str.maketrans(
            '０１２３４５６７８９', '0123456789'
        ))
        if raw.isdigit():
            return max(1, int(raw))
        if not raw:
            return None

        total = 0
        current = 0
        for char in raw:
            if char in cls._CHINESE_DIGITS:
                current = cls._CHINESE_DIGITS[char]
            elif char in cls._CHINESE_UNITS:
                unit = cls._CHINESE_UNITS[char]
                total += (current or 1) * unit
                current = 0
            else:
                return None
        result = total + current
        return max(1, result) if result > 0 else None

    @classmethod
    def _grade_prefix(cls, filename, paras):
        """从文件名或文档标题中提取 7上/8上/9上命名空间。"""
        haystack = ' '.join([
            str(filename or ''),
            *(str(text or '') for _, text, _ in paras[:8]),
        ])
        match = cls.RE_GRADE.search(haystack)
        if not match:
            return '应答'
        grade = match.group('grade')
        return f"{cls._GRADE_DIGITS.get(grade, grade)}上"

    @classmethod
    def _content_lines(cls, text):
        """清理提示块中的英文内容，并按换行保留音频边界。"""
        lines = []
        for raw_line in str(text or '').splitlines():
            value = sanitize(raw_line)
            if not value or cls.RE_CONTROL.search(value) or cls.RE_PROMPT.search(value):
                continue
            if is_chinese(value):
                continue
            value = cls.RE_LEADING_MARK.sub('', value)
            value = cls.RE_LEADING_NUMBER.sub('', value).strip()
            if value:
                lines.append(value)
        return lines

    def parse(self):
        items = []
        collecting = False
        expected_count = None
        current_lines = []
        response_index = 0
        grade_prefix = self._grade_prefix(self.filename, self.paras)
        awaiting_answer_choices = False
        use_exam_naming = is_exam_paper_bundle(self.paras)
        answer_item_indexes = set()
        answer_numbers = {}

        def apply_exam_number(item, question_number):
            if not use_exam_naming or question_number is None:
                return
            item.update({
                "number": question_number,
                "question_number": question_number,
                "question_numbers": [question_number],
                "type_path": ["听后应答"],
            })

        def flush():
            nonlocal collecting, expected_count, current_lines, response_index
            for line in current_lines:
                response_index += 1
                items.append({
                    "category": "听后应答录音稿",
                    "index": response_index,
                    "number": response_index,
                    "filename_stem": f"{grade_prefix}-应答-{response_index}",
                    "voice": "female",
                    "text": line,
                })
            collecting = False
            expected_count = None
            current_lines = []

        for position, (_, text, _) in enumerate(self.paras):
            value = str(text or '').strip()
            prompt = self.RE_PROMPT.search(value)
            if prompt:
                flush()
                awaiting_answer_choices = False
                collecting = True
                expected_count = self._parse_count(prompt.group('count'))
                continue

            if not collecting:
                # 句数达到提示值时，上一段可能已经提前 flush；仍需记住
                # 后续“请朗读应答语”后的答案选项题号，才能把它绑定回这条音频。
                if (
                    self.RE_CONTROL.search(value)
                    and self.RE_ANSWER_PROMPT.search(value)
                ):
                    awaiting_answer_choices = True
                    continue
                answer_number = self.RE_ANSWER_OPTION_NUMBER.match(value)
                is_answer_options = answer_number or self.RE_ANSWER_OPTIONS.match(value)
                if awaiting_answer_choices and is_answer_options and items:
                    item_index = len(items)
                    answer_item_indexes.add(item_index)
                    if answer_number:
                        answer_numbers[item_index] = int(
                            answer_number.group("number")
                        )
                    awaiting_answer_choices = False
                continue

            if self.RE_CONTROL.search(value):
                is_answer_prompt = bool(self.RE_ANSWER_PROMPT.search(value))
                flush()
                if is_answer_prompt:
                    awaiting_answer_choices = True
                continue

            current_lines.extend(self._content_lines(value))
            # 提示中的句数只作为边界提示，不能直接截断实际内容：题目可能
            # 把多行内容放在多个 Word 段落中，即使提示写着“1 个句子”，
            # 也必须遵循“多行多个音频”的规则。只有已经收集到提示数量，
            # 且下一个有内容的段落明确是控制提示/下一条提示时，才提前结束。
            if expected_count and len(current_lines) >= expected_count:
                next_value = ''
                for _, following_text, _ in self.paras[position + 1:]:
                    following_value = str(following_text or '').strip()
                    if following_value:
                        next_value = following_value
                        break
                if next_value and (
                    self.RE_PROMPT.search(next_value)
                    or self.RE_CONTROL.search(next_value)
                ):
                    flush()

        flush()
        if use_exam_naming and answer_numbers:
            # 部分试卷的某一行答案选项漏写了题号。用同一大题中已出现的
            # 题号反推连续区间；只有区间基准一致时才补号，避免猜错。
            bases = {
                number - item_index + 1
                for item_index, number in answer_numbers.items()
            }
            if len(bases) == 1:
                base = next(iter(bases))
                for item_index in answer_item_indexes:
                    answer_numbers.setdefault(item_index, base + item_index - 1)
            for item_index, number in answer_numbers.items():
                if 1 <= item_index <= len(items):
                    apply_exam_number(items[item_index - 1], number)
        if use_exam_naming:
            for ordinal, item in enumerate(items, start=1):
                filename_stem = audio_filename_stem(["听后应答"], ordinal)
                item.update({
                    "type_path": ["听后应答"],
                    "filename_stem": filename_stem,
                    "audio_filename_stem": filename_stem,
                })
        return self._result(items)
