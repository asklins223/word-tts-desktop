"""「听后选择」题型切片：解析器与题型元数据。"""

import re

from audio_naming import audio_filename_stem, is_exam_paper_bundle
from question_types.base import BaseParser
from question_types.text_utils import (
    MAJOR_SECTION_RE,
    SCRIPT_MARKER_RE,
    is_major_section_heading,
    match_script_marker,
    sanitize,
)


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
    RE_MAJOR_SECTION = MAJOR_SECTION_RE
    # 支持【录音原文】、[录音原文]、（录音原文）以及普通冒号写法。
    RE_SCRIPT = SCRIPT_MARKER_RE
    RE_PROMPT = re.compile(
        r'计算机语音提示|语音提示|录音播放|现在，你有|听下面|请听录音|开始录音|停止录音'
    )
    RE_NUMBERED_QUESTION = re.compile(r'^\d+\s*[.．、）)]\s*[^A-Za-z]?')
    RE_OPTION = re.compile(r'^[A-CＡ-Ｃ]\s*[.．、）)]\s*')
    # 业务字段抽取（阶段3⑤）：题干与选项的完整形态
    RE_STEM_FULL = re.compile(r'^(\d+)\s*[.．、）)]\s*(.+)$')
    RE_OPTION_FULL = re.compile(r'^([A-CＡ-Ｃ])\s*[.．、）)]\s*(.+)$')
    RE_ANSWER = re.compile(r'^(?:参考答案|答案|解析)\s*[：:]?')

    @staticmethod
    def _normalize_option_id(raw):
        # 全角Ａ-Ｃ 归一为 ASCII
        return chr(ord(raw) - 0xFEE0) if ord(raw) > 127 else raw

    def _auto_numbered_stem(self, position, value):
        """读取 Word 自动编号题干，避免把编号写回正文文本。"""
        if not re.match(r'^[A-Za-z]', value) or self.RE_OPTION_FULL.match(value):
            return None
        if position >= len(self.paragraph_metadata):
            return None
        number = self.paragraph_metadata[position].get("numbering_number")
        return int(number) if number is not None else None

    def parse(self):
        items = []
        questions = []           # 业务字段通道：题干+选项（不影响 items）
        pending_questions = []   # 等待归属到下一段录音稿的题干组
        in_section = False
        collecting = False
        current_lines = []
        script_index = 0
        question_numbers_by_script = {}
        use_exam_naming = is_exam_paper_bundle(self.paras)

        def flush_questions(ordinal=None):
            """收集到的题干组归属到指定录音稿序号。

            - 【录音原文】处：归属到即将开始的录音稿（script_index+1）；
            - 节尾/解析结束：无主题干组置 None（保留实体、不猜测归属），
              绝不跨题型组错误归属（方案 5.3：归属不确定不得静默合并）。
            """
            nonlocal pending_questions
            for question in pending_questions:
                question["script_ordinal"] = ordinal
                questions.append(question)
                if ordinal is not None:
                    question_numbers_by_script.setdefault(ordinal, []).append(
                        question["number"]
                    )
            pending_questions = []

        def flush():
            nonlocal collecting, current_lines, script_index
            text = sanitize('\n'.join(current_lines))
            if collecting and text:
                script_index += 1
                item = {
                    "category": "听后选择录音稿",
                    "index": script_index,
                    "question_index": script_index,
                    "text": text,
                }
                question_numbers = question_numbers_by_script.get(script_index, [])
                if use_exam_naming:
                    filename_stem = audio_filename_stem(
                        ["听后选择"], script_index
                    )
                    item.update({
                        "question_numbers": list(question_numbers),
                        "type_path": ["听后选择"],
                        "filename_stem": filename_stem,
                        "audio_filename_stem": filename_stem,
                    })
                items.append(item)
            collecting = False
            current_lines = []

        for position, (_, text, _) in enumerate(self.paras):
            value = str(text or '').strip()

            # 先处理题型标题，允许同一文档中出现多组听后选择。
            if self.RE_SECTION_START.search(value):
                flush()
                flush_questions(None)
                in_section = True
                continue

            if not in_section:
                continue

            # 下一大节属于其他题型时，停止采集，避免把后续录音原文混入。
            if is_major_section_heading(value):
                flush()
                flush_questions(None)
                in_section = False
                continue

            script_match = match_script_marker(value)
            if script_match:
                flush()
                flush_questions(script_index + 1)
                collecting = True
                remainder = (script_match.group(1) or '').strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            if not collecting:
                # 业务字段抽取：题干行与选项行（不属于任何录音稿）
                stem_match = self.RE_STEM_FULL.match(value)
                if stem_match:
                    pending_questions.append({
                        "number": int(stem_match.group(1)),
                        "stem": sanitize(stem_match.group(2)),
                        "options": [],
                        "answer": None,
                    })
                    continue
                auto_number = self._auto_numbered_stem(position, value)
                if auto_number is not None:
                    pending_questions.append({
                        "number": auto_number,
                        "stem": sanitize(value),
                        "options": [],
                        "answer": None,
                    })
                    continue
                option_match = self.RE_OPTION_FULL.match(value)
                if option_match and pending_questions:
                    pending_questions[-1]["options"].append({
                        "option_id": self._normalize_option_id(
                            option_match.group(1)),
                        "text": sanitize(option_match.group(2)),
                    })
                    continue
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
        flush_questions(None)
        result = self._result(items)
        result["questions"] = questions
        return result
