"""「信息转述及询问」题型切片：解析器与题型元数据。"""

import re

from audio_naming import audio_filename_stem_for_category, is_exam_paper_bundle
from question_types.base import BaseParser
from question_types.text_utils import (
    MAJOR_TYPE_HEADING_RE,
    SCRIPT_MARKER_RE,
    is_major_section_heading,
    match_answer_marker,
    match_script_marker,
    sanitize,
)


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

    RE_SECTION_START = re.compile(
        r'第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*信息转述'
    )
    RE_SECTION_END = re.compile(r'第二节|参考答案')
    RE_MAJOR_SECTION = MAJOR_TYPE_HEADING_RE
    # 任何「第X节」标记（用于检测其他题型的章节边界）
    RE_ANY_SECTION = re.compile(r'第[一二三四五六七八九十百\d０-９]+节')
    RE_SCRIPT = SCRIPT_MARKER_RE
    # 业务字段（阶段3⑤c）：子节标题 / 参考答案 / 询问问题题干
    RE_SUB_SECTION = re.compile(
        r'第([一二12１２])节\s*[：:]?\s*(信息转述|询问信息)'
    )
    RE_ANSWER_LINE = re.compile(r'^参考答案\s*[：:]\s*(.*)$')
    RE_TASK_STEM = re.compile(r'^(\d+)\s*[.．、）)]\s*(.+)$')

    def parse(self):
        items = []
        tasks = []                # 业务字段通道：转述/询问任务（不影响 items）
        current_section = None    # retelling / asking
        answers_region = False    # 询问节「参考答案：」之后的英文问句区
        asking_by_number = {}
        in_section = False
        collecting = False
        current_lines = []
        script_idx = 0
        use_exam_naming = is_exam_paper_bundle(self.paras)

        def flush():
            nonlocal collecting, current_lines, script_idx
            if collecting and current_lines:
                script_idx += 1
                item = {
                    "category": "信息转述录音稿",
                    "index": script_idx,
                    "text": sanitize('\n'.join(current_lines)),
                }
                if use_exam_naming:
                    filename_stem = audio_filename_stem_for_category(
                        item["category"], script_idx
                    )
                    if filename_stem:
                        item.update({
                            "audio_filename_stem": filename_stem,
                            "type_path": [filename_stem.rsplit("-", 1)[0]],
                        })
                items.append(item)
            collecting = False
            current_lines = []

        for _, text, _ in self.paras:
            value = str(text or '').strip()
            answer_marker = match_answer_marker(value)
            # ---- 子节标题（信息转述 / 询问信息） ----
            sub = self.RE_SUB_SECTION.match(value)
            if sub:
                flush()
                current_section = ('retelling' if sub.group(2) == '信息转述'
                                   else 'asking')
                answers_region = False
                in_section = current_section == 'retelling'
                continue

            # 旧题型资料常省略“第X节”，直接以另一大题名称衔接；不在
            # 这里结束会把后续题型的“录音稿”错误收进本题型。
            if is_major_section_heading(value):
                if not self.RE_SECTION_START.search(value):
                    flush()
                    current_section = None
                    answers_region = False
                    in_section = False
                    continue

            # ---- 询问信息节：中文提示问句 + 英文参考问句 ----
            if current_section == 'asking':
                if answer_marker:
                    answers_region = True
                    continue
                task = self.RE_TASK_STEM.match(value)
                if task:
                    number = int(task.group(1))
                    if answers_region:
                        if number in asking_by_number:
                            asking_by_number[number]["reference_answer"] = \
                                sanitize(task.group(2))
                    else:
                        record = {
                            "task_kind": "asking",
                            "number": number,
                            "prompt": sanitize(task.group(2)),
                            "reference_answer": None,
                        }
                        asking_by_number[number] = record
                        tasks.append(record)
                    continue
                continue

            # ---- 信息转述节：转述参考答案（评分参考） ----
            if in_section and current_section == 'retelling':
                answer_text = (
                    (answer_marker.group(1) or '').strip()
                    if answer_marker else ''
                )
                if answer_marker:
                    if answer_text:
                        tasks.append({
                            "task_kind": "retelling",
                            "reference_answer": sanitize(answer_text),
                        })
                    flush()
                    current_section = None
                    in_section = False
                    answers_region = False
                    continue
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
            m = match_script_marker(text)
            if m:
                flush()
                collecting = True
                remainder = (m.group(1) or '').strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            # ---- 收集录音稿内容 ----
            if collecting:
                current_lines.append(text)

        flush()
        result = self._result(items)
        result["tasks"] = tasks
        return result
