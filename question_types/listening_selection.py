"""「听后选择」题型切片：解析器与题型元数据。"""

import re

from audio_naming import audio_filename_stem, is_exam_paper_bundle
from question_types.base import BaseParser
from question_types.text_utils import (
    MAJOR_SECTION_RE,
    SCRIPT_MARKER_RE,
    has_red_text_in_range,
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
    # Some Word sources put the red correct option on the same paragraph as
    # the question stem, then wrap the rest of the stem onto the next line.
    # The red-span metadata is the authority for deciding whether this is an
    # inline option rather than ordinary prose containing “A./B./C.”.
    RE_INLINE_OPTION = re.compile(
        r'(?P<stem>.+?)\s+(?P<option>[A-CＡ-Ｃ])\s*[.．、）)]\s*(?P<text>.+)$'
    )
    RE_ANSWER = re.compile(r'^(?:参考答案|答案|解析)\s*[：:]?')
    RE_SCORE = re.compile(
        r'每(?:小题|道题|题)\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.IGNORECASE,
    )
    RE_FULL_SCORE = re.compile(
        r'满分\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.IGNORECASE,
    )
    # Word puts the reading-time prompt before the question.  A single
    # recording may cover two questions, so the value in the document is the
    # total for that prompt (for example, 10 seconds for questions 7 and 8),
    # not the value that belongs in either individual platform card.
    RE_ANSWER_TIME = re.compile(
        r'(?P<seconds>[0-9０-９]+)\s*秒(?:钟)?',
        re.IGNORECASE,
    )
    RE_QUESTION_RANGE = re.compile(
        r'第\s*(?P<start>[0-9０-９]+)\s*'
        r'(?:至|到|[-—~～])\s*第?\s*'
        r'(?P<end>[0-9０-９]+)\s*小题',
        re.IGNORECASE,
    )
    RE_QUESTION_SINGLE = re.compile(
        r'第\s*(?P<number>[0-9０-９]+)\s*小题',
        re.IGNORECASE,
    )
    RE_QUESTION_COUNT = re.compile(
        r'(?P<count>[0-9０-９一二两三四五六七八九十百]+)\s*道?\s*小题',
        re.IGNORECASE,
    )

    @staticmethod
    def _normalize_option_id(raw):
        # 全角Ａ-Ｃ 归一为 ASCII
        return chr(ord(raw) - 0xFEE0) if ord(raw) > 127 else raw

    @staticmethod
    def _parse_number(raw):
        value = str(raw or '').strip().translate(
            str.maketrans('０１２３４５６７８９', '0123456789')
        )
        if value.isdigit():
            return int(value)
        chinese_digits = {
            '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3,
            '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
        }
        chinese_units = {'十': 10, '百': 100}
        if not value:
            return None
        total = 0
        current = 0
        for char in value:
            if char in chinese_digits:
                current = chinese_digits[char]
            elif char in chinese_units:
                total += (current or 1) * chinese_units[char]
                current = 0
            else:
                return None
        result = total + current
        return result if result > 0 else None

    @classmethod
    def _prompt_question_count(cls, value):
        """Return how many small questions a reading prompt covers."""

        text = str(value or '')
        range_match = cls.RE_QUESTION_RANGE.search(text)
        if range_match:
            start = cls._parse_number(range_match.group('start'))
            end = cls._parse_number(range_match.group('end'))
            if start is not None and end is not None and end >= start:
                return end - start + 1

        if cls.RE_QUESTION_SINGLE.search(text):
            return 1

        # Check a plain “两道小题” phrase only after the explicit “第N小题”
        # form.  Otherwise the loose count regex would read the N in
        # “第N小题” as the number of questions (q2 => 2, q3 => 3, ...).
        count_match = cls.RE_QUESTION_COUNT.search(text)
        if count_match:
            count = cls._parse_number(count_match.group('count'))
            if count is not None:
                return count
        return None

    @classmethod
    def _prompt_answer_time(cls, value):
        """Extract the total reading time from a selection prompt."""

        text = str(value or '')
        if not any(marker in text for marker in ('阅读', '小题')):
            return None
        match = cls.RE_ANSWER_TIME.search(text)
        if match is None:
            return None
        return cls._parse_number(match.group('seconds'))

    def _auto_numbered_stem(self, position, value):
        """读取 Word 自动编号题干，避免把编号写回正文文本。"""
        if not re.match(r'^[A-Za-z]', value) or self.RE_OPTION_FULL.match(value):
            return None
        if position >= len(self.paragraph_metadata):
            return None
        number = self.paragraph_metadata[position].get("numbering_number")
        return int(number) if number is not None else None

    @classmethod
    def _inline_red_option(cls, value, metadata, *, source_offset=0):
        """Extract a red option embedded in a question paragraph.

        ``source_offset`` maps the question-stem substring back to the full
        Word paragraph because the numbering prefix is not part of the
        stored stem.  Requiring the option span itself to be red prevents
        ordinary sentences such as “Choose A. or B.” from becoming options.
        """

        for match in cls.RE_INLINE_OPTION.finditer(str(value or '')):
            option_start = source_offset + match.start('option')
            option_end = source_offset + match.end('text')
            if not has_red_text_in_range(metadata, option_start, option_end):
                continue
            return {
                "option_id": cls._normalize_option_id(match.group('option')),
                "text": sanitize(match.group('text')),
                "stem": sanitize(match.group('stem')),
            }
        return None

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
        section_score = None
        declared_item_scores = []
        declared_section_scores = []
        pending_answer_time = None
        pending_answer_count = None

        def parse_score(value):
            match = self.RE_SCORE.search(str(value or ''))
            if match is None:
                return None
            raw_score = str(match.group('score')).translate(
                str.maketrans('０１２３４５６７８９', '0123456789')
            )
            try:
                score = float(raw_score)
            except ValueError:
                return None
            return int(score) if score.is_integer() else score

        def parse_full_score(value):
            match = self.RE_FULL_SCORE.search(str(value or ''))
            if match is None:
                return None
            raw_score = str(match.group('score')).translate(
                str.maketrans('０１２３４５６７８９', '0123456789')
            )
            try:
                score = float(raw_score)
            except ValueError:
                return None
            return int(score) if score.is_integer() else score

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

        def set_pending_reading_prompt(value):
            """Remember a prompt and split its total time across its questions."""

            nonlocal pending_answer_time, pending_answer_count
            count = self._prompt_question_count(value)
            if count is not None:
                pending_answer_count = count
            answer_time = self._prompt_answer_time(value)
            if answer_time is None:
                return
            pending_answer_time = answer_time
            count = pending_answer_count or len(pending_questions) or 1
            per_question = answer_time / count
            if isinstance(per_question, float) and per_question.is_integer():
                per_question = int(per_question)
            # Some source files place the time line after the numbered
            # questions.  Apply it to those already collected as well as to
            # questions that will be read below the prompt.
            for question in pending_questions:
                question["answer_time"] = per_question

        def pending_time_per_question():
            if pending_answer_time is None:
                return None
            count = pending_answer_count or 1
            per_question = pending_answer_time / count
            if isinstance(per_question, float) and per_question.is_integer():
                return int(per_question)
            return per_question

        def clear_pending_reading_prompt():
            nonlocal pending_answer_time, pending_answer_count
            pending_answer_time = None
            pending_answer_count = None

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
                clear_pending_reading_prompt()
                in_section = True
                section_score = parse_score(value)
                if section_score is not None:
                    declared_item_scores.append(section_score)
                full_score = self.RE_FULL_SCORE.search(value)
                if full_score:
                    parsed_full_score = parse_full_score(full_score.group(0))
                    if parsed_full_score is not None:
                        declared_section_scores.append(parsed_full_score)
                continue

            if not in_section:
                continue

            # 下一大节属于其他题型时，停止采集，避免把后续录音原文混入。
            if is_major_section_heading(value):
                flush()
                flush_questions(None)
                clear_pending_reading_prompt()
                in_section = False
                continue

            script_match = match_script_marker(value)
            if script_match:
                flush()
                flush_questions(script_index + 1)
                clear_pending_reading_prompt()
                collecting = True
                remainder = (script_match.group(1) or '').strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            if not collecting:
                # The prompt may be one paragraph or two.  Record the
                # question range first, then the following “现在，你有…秒”
                # paragraph can reuse that range.  A 10-second prompt for
                # questions 5–6 therefore becomes 5 seconds on each card.
                set_pending_reading_prompt(value)

                # 业务字段抽取：题干行与选项行（不属于任何录音稿）
                stem_match = self.RE_STEM_FULL.match(value)
                if stem_match:
                    paragraph_metadata = (
                        self.paragraph_metadata[position]
                        if position < len(self.paragraph_metadata)
                        else {}
                    )
                    stem_value = stem_match.group(2)
                    question = {
                        "number": int(stem_match.group(1)),
                        "stem": sanitize(stem_value),
                        "options": [],
                        "answer": None,
                    }
                    inline_option = self._inline_red_option(
                        stem_value,
                        paragraph_metadata,
                        source_offset=stem_match.start(2),
                    )
                    if inline_option:
                        question["stem"] = inline_option["stem"]
                        question["options"].append({
                            "option_id": inline_option["option_id"],
                            "text": inline_option["text"],
                        })
                        question["answer"] = inline_option["option_id"]
                    answer_time = pending_time_per_question()
                    if answer_time is not None:
                        question["answer_time"] = answer_time
                    if section_score is not None:
                        question["score"] = section_score
                    pending_questions.append(question)
                    continue
                auto_number = self._auto_numbered_stem(position, value)
                if auto_number is not None:
                    question = {
                        "number": auto_number,
                        "stem": sanitize(value),
                        "options": [],
                        "answer": None,
                    }
                    answer_time = pending_time_per_question()
                    if answer_time is not None:
                        question["answer_time"] = answer_time
                    if section_score is not None:
                        question["score"] = section_score
                    pending_questions.append(question)
                    continue
                option_match = self.RE_OPTION_FULL.match(value)
                if option_match and pending_questions:
                    option_id = self._normalize_option_id(option_match.group(1))
                    pending_questions[-1]["options"].append({
                        "option_id": option_id,
                        "text": sanitize(option_match.group(2)),
                    })
                    paragraph_metadata = (
                        self.paragraph_metadata[position]
                        if position < len(self.paragraph_metadata)
                        else {}
                    )
                    if has_red_text_in_range(
                        paragraph_metadata,
                        0,
                        len(value),
                    ):
                        existing_answer = pending_questions[-1].get("answer")
                        if existing_answer not in (None, option_id):
                            # A single-choice question with two red options
                            # is ambiguous.  Do not silently select the last
                            # one; leave it incomplete for the page start gate.
                            pending_questions[-1]["answer"] = None
                            pending_questions[-1]["answer_status"] = "ambiguous_red"
                        elif pending_questions[-1].get("answer_status") != "ambiguous_red":
                            pending_questions[-1]["answer"] = option_id
                    continue
                # Word may wrap a long question stem before the A/B/C option
                # paragraphs.  Keep that continuation attached to the latest
                # question while its option list is still being collected.
                if (
                    pending_questions
                    and value
                    and not self.RE_ANSWER.match(value)
                    and len(pending_questions[-1].get("options", [])) < 3
                ):
                    current_question = pending_questions[-1]
                    current_question["stem"] = sanitize(
                        f"{current_question.get('stem', '')} {value}"
                    )
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
                if self.RE_PROMPT.search(value):
                    set_pending_reading_prompt(value)
                continue

            current_lines.append(value)

        flush()
        flush_questions(None)
        clear_pending_reading_prompt()
        option_order = {"A": 0, "B": 1, "C": 2}
        for question in questions:
            question["options"] = sorted(
                question.get("options", []),
                key=lambda option: (
                    option_order.get(option.get("option_id"), 99),
                    option.get("option_id", ""),
                ),
            )
        result = self._result(items)
        # A content segment may contain multiple choice questions that share
        # one recording. Keep the segment-level score as the sum of its
        # question scores while retaining each question's own score for the
        # page form.
        for item in result["items"]:
            ordinal = item.get("index")
            scores = [
                question.get("score")
                for question in questions
                if question.get("script_ordinal") == ordinal
                and isinstance(question.get("score"), (int, float))
            ]
            if scores:
                item["score"] = sum(scores)
        computed_score = sum(
            score for score in (
                question.get("score") for question in questions
            ) if isinstance(score, (int, float))
        )
        if declared_item_scores:
            result["score_per_item"] = declared_item_scores[0] if len(set(declared_item_scores)) == 1 else None
        result["section_score"] = (
            sum(declared_section_scores)
            if declared_section_scores
            else computed_score
        )
        result["computed_score"] = computed_score
        result["questions"] = questions
        return result
