"""「听后选择」题型切片：解析器与题型元数据。"""

import re

from question_types.base import BaseParser, QuestionType
from question_types.text_utils import sanitize


# ============================================================================
# 2. 听后选择解析器
# ============================================================================

class ListeningSelectionParser(BaseParser):
    """只提取「听后选择」题型中的录音原文对话。

    这类试卷同时包含题干、选项和计算机提示，但配音任务只需要
    ``【录音原文】`` 后面的对话。W/w 使用默认女声，M/m 使用默认男声；
    标记本身保留在结构化文本中，后续合成阶段会负责切换音色并移除标记。
    每一块「录音原文」对应一个最终音频，不能按说话人轮次拆成多个音频。
    """

    DOC_TYPE = "听后选择"

    # 题型标题可能带中文序号、题量说明或括号；只接受行首标题形态，
    # 避免正文里偶然提到“听后选择”时误启动解析状态机。
    RE_SECTION_START = re.compile(
        r'^(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?'
        r'听后选择(?:题型?|[（(【\s:：]|$)',
        re.I | re.M,
    )
    # 混合试卷中下一大节出现时，结束当前「听后选择」范围；同时兼容
    # 旧题型的「第X节」和新版 Section 标题。
    RE_MAJOR_SECTION = re.compile(
        r'^(?:[一二三四五六七八九十百]+\s*[、.．)]|'
        r'第[一二三四五六七八九十百]+节|'
        r'Section\s+[A-Z](?:\s*[：:]|$))',
        re.I,
    )
    # 支持【录音原文】、[录音原文]、（录音原文）以及普通冒号写法。
    RE_SCRIPT = re.compile(
        r'^[【\[（(]?\s*录音原文\s*[】\]）)]?\s*[：:]?\s*(.*)$'
    )
    RE_PROMPT = re.compile(
        r'计算机语音提示|语音提示|录音播放|现在，你有|听下面|请听录音|开始录音|停止录音'
    )
    RE_NUMBERED_QUESTION = re.compile(r'^\d+\s*[.．、）)]\s*[^A-Za-z]?')
    RE_OPTION = re.compile(r'^[A-CＡ-Ｃ]\s*[.．、）)]\s*')
    RE_ANSWER = re.compile(r'^(?:参考答案|答案|解析)\s*[：:]?')

    def parse(self):
        items = []
        in_section = False
        collecting = False
        current_lines = []
        script_index = 0

        def flush():
            nonlocal collecting, current_lines, script_index
            text = sanitize('\n'.join(current_lines))
            if collecting and text:
                script_index += 1
                items.append({
                    "category": "听后选择录音稿",
                    "index": script_index,
                    "question_index": script_index,
                    "text": text,
                })
            collecting = False
            current_lines = []

        for _, text, _ in self.paras:
            value = str(text or '').strip()

            # 先处理题型标题，允许同一文档中出现多组听后选择。
            if self.RE_SECTION_START.search(value):
                flush()
                in_section = True
                continue

            if not in_section:
                continue

            # 下一大节属于其他题型时，停止采集，避免把后续录音原文混入。
            if self.RE_MAJOR_SECTION.match(value):
                flush()
                in_section = False
                continue

            script_match = self.RE_SCRIPT.match(value)
            if script_match:
                flush()
                collecting = True
                remainder = script_match.group(1).strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            if not collecting:
                continue

            # 下一道题的提示、题干、选项或答案都不是录音内容。
            if (
                self.RE_PROMPT.search(value)
                or self.RE_NUMBERED_QUESTION.match(value)
                or self.RE_OPTION.match(value)
                or self.RE_ANSWER.match(value)
            ):
                flush()
                continue

            current_lines.append(value)

        flush()
        return self._result(items)


QUESTION_TYPE = QuestionType(
    key="听后选择",
    parser=ListeningSelectionParser,
    color="#2563eb",
    filename_keywords=("听后选择",),
    content_markers=(ListeningSelectionParser.RE_SECTION_START,),
)
