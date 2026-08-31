"""「信息获取」题型切片：解析器与题型元数据。"""

import re

from question_types.base import BaseParser
from question_types.text_utils import (
    MAJOR_SECTION_RE,
    SCRIPT_MARKER_RE,
    is_chinese,
    is_major_section_heading,
    match_answer_marker,
    match_script_marker,
    sanitize,
)


# ============================================================================
# 1. 信息获取解析器
# ============================================================================

class InfoAcquisitionParser(BaseParser):
    """
    解析「信息获取」文档。
    提取「第一节 听选信息」和「第二节 回答问题」中的题目及每段录音稿。

    题目规则：
      - 两节共用文档中的连续题号，题号不会在第二节重新开始
      - 每道题单独生成一条结果，文本去掉题号
      - 按题目出现顺序使用男声、女声交替朗读（第一题为男声）
      - filename_stem 使用"问题x"，供 TTS 阶段生成问题1.mp3等文件

    文档结构示例:
        第一节 听选信息
          听第一段对话，回答第1—2两个问题。
          1. Question?
          (Options)
          录音稿：
          W: ...
          M: ...
          听第二段对话，回答第3—4两个问题。
          ...
        第二节 回答问题
          听下面一段独白，录音播放两遍。
          7. Question?
          ...
          录音稿：
          (W) ...
        参考答案（独立一行）                ← 进入答案区，直到下一组题目
        （新格式中每题后可能紧跟「参考答案：xxx」行内答案，
          该行不结束采集，会被跳过）
        （「回答问题」题目也可能漏编号，按上一个题号自动顺延）

    支持同一文件内包含多篇试题（多次出现「第一节 听选信息」）。
    """

    DOC_TYPE = "信息获取"

    # 第一节 听选信息
    RE_SECTION_START = re.compile(
        r'第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*听选信息'
    )
    # 第二节 回答问题
    RE_SECTION2_START = re.compile(
        r'第[二2２]节\s*[：:]?\s*回答问题'
    )
    # 参考答案（仅独立一行）→ 进入答案区；套卷中每段录音后都可能出现。
    RE_SECTION_END = re.compile(r'^参考答案\s*[：:]?\s*$')
    # 行内参考答案（每题附带答案）→ 直接跳过
    RE_INLINE_ANSWER = re.compile(r'^参考答案\s*[：:]')
    # 答案区之后的下一组录音提示，允许一个套卷包含多组对话/独白。
    RE_RECORDING_PROMPT = re.compile(
        r'(?:听下面|听第.+段|录音播放|各段播放|每段播放)'
    )
    # 漏编号的英文题目（新格式「回答问题」中 7-9 题可能没有题号）
    RE_UNNUMBERED_QUESTION = re.compile(r'[?？]\s*$')
    # 题号区间（如「回答第 7-10 个问题」「回答第1—2两个问题」），
    # 用于给漏编号题目确定起始题号
    RE_ANSWER_RANGE = re.compile(r'第\s*(\d+)\s*[-—~～至到]\s*(\d+)')
    # 任何「第X节」标记（用于检测其他题型的章节边界）
    RE_ANY_SECTION = re.compile(r'第[一二三四五六七八九十百\d０-９]+节')
    # 新旧题型混排时，兼容没有使用「第X节」而改用中文序号、Section
    # 或独立题型标题的边界；按结构识别，不把某个 Section 名称写死。
    RE_OTHER_MAJOR_HEADING = MAJOR_SECTION_RE
    # 录音稿/听力原文：（可能后面紧跟内容，也可能单独一行）
    RE_SCRIPT = SCRIPT_MARKER_RE
    # 听第X段对话 → 上一段录音稿结束
    RE_DIALOG = re.compile(r'听第.+段对话')
    # 题目编号：1. / 1． / 1、 / 1) / 1）
    RE_QUESTION = re.compile(r'^(\d+)\s*[.．、）)]\s*(.+)')

    def parse(self):
        items = []
        in_section = False       # 是否在任一节内
        current_category = ""    # 当前节的 category
        collecting = False       # 是否正在收集录音稿
        current_lines = []       # 当前录音稿的文本行
        question_order = 0       # 当前试题内的题目顺序；第二节不重置
        last_qnum = 0            # 上一个题目编号（用于漏编号题目顺延）
        next_qnum = None         # 下一个待分配的题号（由说明行的题号区间设定）
        answer_block = False     # 跳过每组录音后面的参考答案区
        # 每节独立编号
        idx_by_cat = {}          # {category: count}

        def flush():
            nonlocal collecting, current_lines
            if collecting and current_lines:
                cat = current_category
                idx_by_cat[cat] = idx_by_cat.get(cat, 0) + 1
                items.append({
                    "category": cat,
                    "index": idx_by_cat[cat],
                    "text": sanitize('\n'.join(current_lines)),
                })
            collecting = False
            current_lines = []

        for _, text, _ in self.paras:
            script_marker = match_script_marker(text)
            answer_marker = match_answer_marker(text)

            # ---- 第一节 听选信息 开始 ----
            if self.RE_SECTION_START.search(text):
                flush()
                in_section = True
                current_category = "听选信息录音稿"
                question_order = 0
                last_qnum = 0
                next_qnum = None
                answer_block = False
                continue

            # ---- 第二节 回答问题 开始 ----
            if self.RE_SECTION2_START.search(text):
                flush()
                in_section = True
                current_category = "回答问题录音稿"
                answer_block = False
                continue

            # ---- 其他「第X节」→ 遇到其他题型的章节，停止采集 ----
            if in_section and self.RE_ANY_SECTION.search(text):
                # 不是自己的章节标记，停止采集
                if not self.RE_SECTION_START.search(text) and not self.RE_SECTION2_START.search(text):
                    flush()
                    in_section = False
                    current_category = ""
                    continue

            if in_section and is_major_section_heading(text):
                flush()
                in_section = False
                current_category = ""
                continue

            if not in_section:
                continue

            # ---- 参考答案/答案/解析 → 跳过答案内容 ----
            # 独立标签会开启答案区；录音后的行内答案也会开启答案区，
            # 防止后面的“1. xxx”被误识别为下一道题。题目前置的行内
            # 答案只跳过当前行，不影响随后真正的题干。
            if answer_marker:
                was_collecting = collecting
                answer_text = (answer_marker.group(1) or '').strip()
                flush()
                if not answer_text or was_collecting:
                    answer_block = True
                continue

            # 套卷中“参考答案：”会重复出现，不能把后续大题静默丢掉。
            # 优先使用明确的下一组录音提示恢复；没有提示时，只有比上一题
            # 更大的显式题号才可能是下一组题目，答案行则继续跳过。
            if answer_block:
                if script_marker or self.RE_RECORDING_PROMPT.search(text):
                    answer_block = False
                else:
                    numbered = self.RE_QUESTION.match(text)
                    if numbered is None or int(numbered.group(1)) <= last_qnum:
                        continue
                    answer_block = False

            # ---- 题号区间说明行（如「回答第 7-10 个问题」）→ 设定起始题号 ----
            m_range = self.RE_ANSWER_RANGE.search(text)
            if m_range:
                next_qnum = int(m_range.group(1))

            # ---- 题目：去题号，按出现顺序男/女交替 ----
            # 只在非录音稿状态识别，避免把录音稿中偶然以数字开头的句子误判为题目。
            question_number = None
            question_text = ""
            if not collecting:
                m_q = self.RE_QUESTION.match(text)
                if m_q:
                    question_number = int(m_q.group(1))
                    question_text = sanitize(m_q.group(2))
                    # 有编号的题目推进待分配题号
                    next_qnum = max(next_qnum or 0, question_number + 1)
                elif self.RE_UNNUMBERED_QUESTION.search(text) and not is_chinese(text):
                    # 新格式中题目可能漏编号（如 U2 的 7-9 题）：
                    # 以英文问句（末尾 ?）兜底识别，题号按说明行区间顺延
                    question_number = (
                        next_qnum if next_qnum is not None else last_qnum + 1
                    )
                    if next_qnum is not None:
                        next_qnum += 1
                    question_text = sanitize(text)
            if question_number is not None and question_text:
                question_order += 1
                last_qnum = question_number
                speaker = "M" if question_order % 2 == 1 else "W"
                section_name = (
                    "听选信息" if current_category == "听选信息录音稿" else "回答问题"
                )
                items.append({
                    "category": f"{section_name}题目",
                    "number": question_number,
                    "filename_stem": f"问题{question_number}",
                    "voice": "male" if speaker == "M" else "female",
                    "text": f"{speaker}: {question_text}",
                })
                continue

            # ---- 录音稿标记 ----
            if script_marker:
                flush()  # 先保存上一段录音稿
                collecting = True
                remainder = (script_marker.group(1) or '').strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            # ---- 新对话开始 → 当前录音稿结束 ----
            if collecting and (
                self.RE_DIALOG.search(text)
                or self.RE_RECORDING_PROMPT.search(text)
            ):
                flush()
                continue

            # ---- 收集录音稿行 ----
            if collecting:
                current_lines.append(text)

        # 文档末尾兜底
        flush()

        return self._result(items)
