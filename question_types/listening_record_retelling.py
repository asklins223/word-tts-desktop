"""「听后记录并转述信息」题型切片。

这类文档的可配音内容是「第一节 听后记录」中的英文听力短文，
通常以 ``(W)`` / ``(M)`` 标记说话人。中文操作提示、表格答案和
第二节的转述参考答案都不是音频正文，因此只在明确的听力正文边界
内收集带说话人标记的英文段落。
"""

import re
from collections.abc import Mapping

from audio_naming import audio_filename_stem, is_exam_paper_bundle
from document_profiles import (
    RECORD_RETELLING_ENTRY_PROFILE,
    RECORD_RETELLING_TABLE_SPECIAL_PROFILE,
    RECORD_RETELLING_UNKNOWN_PROFILE,
)
from question_types.base import BaseParser
from question_types.text_utils import (
    MAJOR_TYPE_HEADING_RE,
    SCRIPT_MARKER_RE,
    is_chinese,
    is_major_section_heading,
    match_script_marker,
    sanitize,
)


class ListeningRecordRetellingParser(BaseParser):
    """提取「听后记录并转述信息」第一节的听力短文。"""

    DOC_TYPE = "听后记录并转述信息"
    _REQUIRES_DOCUMENT_BLOCKS = True

    # 标题可能带有“（共...）”等说明，但“第一节 听后记录”是稳定边界。
    RE_SECTION_START = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*"
        r"听后记录(?:\s|[：:（(【]|$)"
    )
    # 第二节的标题和“参考答案/答题区域”都表示听力正文已经结束。
    RE_SECTION_END = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第二节(?:\s*[：:]?\s*信息转述)?"
        r"|^\s*[【\[（(]?\s*(?:参考答案|答题区域)"
    )
    RE_ANY_SECTION = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第[一二三四五六七八九十百\d０-９]+节"
    )
    RE_SCRIPT_PREFIX = SCRIPT_MARKER_RE
    RE_MAJOR_SECTION = MAJOR_TYPE_HEADING_RE
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
    # Only the record/retelling paper's second section is a retelling section.
    # A mixed paper may contain an earlier legacy heading such as
    # ``第二节 询问信息``; accepting any bare ``第二节`` here would switch the
    # parser into retelling-answer mode too early and hide the later record
    # table and listening script.
    RE_RETELLING_SECTION = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第二节\s*[：:]?\s*信息转述"
        r"(?:\s*[（(【\[].*?[）)】\]]\s*)?$"
    )
    RE_SCORE_PER_ITEM = re.compile(
        r"每小题\s*([0-9０-９]+(?:[.]\d+)?)\s*分"
    )
    RE_FULL_SCORE = re.compile(
        r"满分\s*([0-9０-９]+(?:[.]\d+)?)\s*分"
    )
    RE_RETELLING_TIME = re.compile(
        r"(?:答题(?:时间|时长)\s*(?:为|是)?|在)\s*"
        r"([0-9０-９]+)\s*秒(?:钟)?"
    )
    RE_NUMBERED_ANSWER = re.compile(
        r"(?P<number>\d+)\s*[.．、）)]\s*(?P<answer>.*?)"
        r"(?=(?:\s+\d+\s*[.．、）)]\s*)|$)",
        re.S,
    )

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

    def _recording_table_index(self):
        """Return the first table in the first recording section, if present.

        The bundled paper places the listening-record table after the first
        section's instructions and before the second section.  The document
        block stream already carries the source table index, so the workflow
        can later render and crop exactly this table instead of guessing from
        all tables in the document (the imitation-reading table appears
        earlier in the same file).
        """

        if not self.document_blocks:
            return None
        in_recording_section = False
        for block in self.document_blocks:
            value = str(block.text or '').strip()
            if block.kind == 'paragraph':
                if self.RE_SECTION_START.search(value):
                    in_recording_section = True
                    continue
                if in_recording_section and self.RE_SECTION_END.search(value):
                    break
                continue
            if not in_recording_section or block.kind != 'table':
                continue
            table_index = block.metadata.get('table_index') if isinstance(block.metadata, Mapping) else None
            try:
                table_index = int(table_index)
            except (TypeError, ValueError):
                table_index = None
            if table_index is not None and table_index >= 0:
                return table_index
        return None

    @classmethod
    def _retelling_prompt_candidate(cls, value):
        """从转述区的一行或一个结构块中提取英文题干。

        Word 的横线可能是实际下划线、全角下划线、段落底边框或单独的
        空白段落，不能把某一种排版形式当成题干存在的必要条件。
        """

        raw = str(value or '').strip()
        if not raw:
            return None

        # 没有中文控制文字时保留同一段落中的换行，兼容题干与横线在同一
        # 段落、以及英文题干被显式换行的情况；含中文的结构块则逐行筛选。
        candidates = [raw] if not is_chinese(raw) else raw.splitlines()
        for candidate in candidates:
            candidate = str(candidate or '').strip()
            if not candidate:
                continue
            if '参考答案' in candidate or cls.RE_CONTROL.search(candidate):
                continue
            if match_script_marker(candidate):
                continue

            prompt = re.sub(r'^\s*\d+\s*[.．、）)]\s*', '', candidate)
            prompt = re.sub(r'[_＿]+', '', prompt)
            prompt = sanitize(prompt).strip()
            prompt = re.sub(r'\s+([.,!?;:])', r'\1', prompt)
            # 清理题干与版式横线/换行拼接出的重复句号。
            prompt = re.sub(r'(?:\.\s*){2,}$', '.', prompt)
            if re.search(r'[A-Za-z]', prompt):
                return prompt
        return None

    def _retelling_prompt_from_blocks(self):
        """在普通段落未覆盖的表格/文本框中兜底寻找转述题干。"""

        active = False
        for block in self.document_blocks or ():
            for value in str(block.text or '').splitlines() or ['']:
                value = value.strip()
                if not value:
                    continue
                if self.RE_RETELLING_SECTION.match(value):
                    active = True
                    continue
                if not active:
                    continue
                if '参考答案' in value:
                    return None
                if (
                    self.RE_ANY_SECTION.match(value)
                    or is_major_section_heading(value)
                ):
                    return None
                prompt = self._retelling_prompt_candidate(value)
                if prompt:
                    return prompt
        return None

    def parse(self):
        items = []
        in_section = False
        collecting = False
        plain_script_pending = False
        current_lines = []
        script_idx = 0
        record_question_numbers = []
        record_answers = {}
        record_score = None
        retelling_prompt = None
        retelling_answers = []
        retelling_score = None
        retelling_answer_time = None
        in_retelling_section = False
        collecting_retelling_answers = False
        use_exam_naming = is_exam_paper_bundle(self.paras)
        recording_table_index = self._recording_table_index()
        record_section_score = None
        major_section_score = None

        def parse_score_value(raw):
            normalized = str(raw or '').translate(
                str.maketrans('０１２３４５６７８９', '0123456789')
            )
            try:
                score = float(normalized)
            except ValueError:
                return None
            return int(score) if score.is_integer() else score

        def parse_numbered_answers(value):
            answers = {}
            for match in self.RE_NUMBERED_ANSWER.finditer(str(value or "")):
                answer = sanitize(match.group("answer")).strip(" /")
                if answer:
                    answers[int(match.group("number"))] = answer
            return answers

        def parse_number(value):
            raw = str(value or "").translate(
                str.maketrans("０１２３４５６７８９", "0123456789")
            )
            try:
                number = float(raw)
            except (TypeError, ValueError):
                return None
            return int(number) if number.is_integer() else number

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

            score_match = self.RE_SCORE_PER_ITEM.search(value)
            if score_match and record_score is None and (
                in_section and not in_retelling_section
                or self.RE_SECTION_START.search(value)
            ):
                record_score = parse_number(score_match.group(1))

            full_score = self.RE_FULL_SCORE.search(value)
            if full_score:
                parsed_full_score = parse_score_value(full_score.group(1))
                if self.RE_RETELLING_SECTION.match(value):
                    if parsed_full_score is not None:
                        retelling_score = parsed_full_score
                elif self.RE_SECTION_START.search(value):
                    if parsed_full_score is not None:
                        record_section_score = parsed_full_score
                elif self.RE_TYPE_TITLE.search(value) and parsed_full_score is not None:
                    major_section_score = parsed_full_score

            if self.RE_RETELLING_SECTION.match(value):
                flush()
                in_section = False
                in_retelling_section = True
                collecting_retelling_answers = False
                full_score = self.RE_FULL_SCORE.search(value)
                if full_score:
                    retelling_score = parse_number(full_score.group(1))
                continue

            if in_retelling_section:
                # 转述区也必须在下一节/下一题型开始时停止，否则后续英文
                # 内容会覆盖题干或被错误地收进参考答案。
                if (
                    self.RE_ANY_SECTION.match(value)
                    or is_major_section_heading(value)
                ):
                    in_retelling_section = False
                    collecting_retelling_answers = False
                    continue
                time_match = self.RE_RETELLING_TIME.search(value)
                if time_match and retelling_answer_time is None:
                    retelling_answer_time = parse_number(time_match.group(1))
                if "参考答案" in value or match_script_marker(value):
                    collecting_retelling_answers = "参考答案" in value
                    remainder = re.sub(r"^.*?参考答案\s*[：:]?", "", value).strip()
                    if collecting_retelling_answers and remainder:
                        parsed = parse_numbered_answers(remainder)
                        if parsed:
                            retelling_answers.extend(parsed[index] for index in sorted(parsed))
                        elif re.search(r"[A-Za-z]", remainder):
                            retelling_answers.append(sanitize(remainder).strip(" /"))
                    continue
                if collecting_retelling_answers:
                    answer_line = re.sub(r"^\s*/", "", value).strip()
                    parsed = parse_numbered_answers(answer_line)
                    if parsed:
                        retelling_answers.extend(
                            parsed[index] for index in sorted(parsed)
                        )
                    else:
                        answer_line = re.sub(
                            r"^\s*\d+\s*[.．、）)]\s*", "", answer_line
                        )
                        if answer_line and re.search(r"[A-Za-z]", answer_line):
                            retelling_answers.append(sanitize(answer_line))
                # The prompt is a structural slot in the retelling section;
                # blank-line characters are only optional layout markers.
                elif retelling_prompt is None:
                    retelling_prompt = self._retelling_prompt_candidate(value)
                continue

            # “答题区域”本身会先结束第一节，参考答案常在下一段才出现；
            # 只要已经提取到本题听力稿，就继续读取这一行中的实际题号。
            if items and "参考答案" in value:
                parsed_answers = parse_numbered_answers(value)
                for number, answer in parsed_answers.items():
                    record_answers[number] = answer
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

            # 有些套卷不写“第二节/第三节”，而是直接切换到下一大题名；
            # 这种标题必须先结束本题型的正文扫描。
            if in_section and is_major_section_heading(value):
                flush()
                in_section = False
                continue

            if in_section and self.RE_ANY_SECTION.search(value):
                flush()
                in_section = False
                continue

            if not in_section:
                continue

            script_prefix = match_script_marker(value)
            if script_prefix:
                flush()
                collecting = True
                plain_script_pending = False
                remainder = (script_prefix.group(1) or '').strip()
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
        if retelling_prompt is None:
            retelling_prompt = self._retelling_prompt_from_blocks()
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
        if items:
            recording_total = (
                (record_score if record_score is not None else 1)
                * len(record_answers)
                if record_answers
                else 0
            )
            retelling_total = (
                retelling_score
                if retelling_score is not None and (retelling_prompt or retelling_answers)
                else 0
            )
            if recording_total or retelling_total:
                # The audio item is the shared source for both sections, so
                # its segment-level score is the total of the page facts.
                items[-1]["score"] = recording_total + retelling_total
            # A standalone专项 is opened for external entry only when it has
            # the exact source facts required by the confirmed page flow:
            # one table image source, three scored blanks, and a scored
            # retelling prompt with reference answers.  Incomplete documents
            # remain usable for audio generation but fail closed at the entry
            # profile gate.
            external_input_ready = bool(
                recording_table_index is not None
                and len(record_answers) == 3
                and record_score is not None
                and retelling_prompt
                and retelling_score is not None
                and retelling_answers
            )
            for item in items:
                item.update({
                    "major_section_profile": (
                        RECORD_RETELLING_TABLE_SPECIAL_PROFILE
                        if recording_table_index is not None
                        else RECORD_RETELLING_UNKNOWN_PROFILE
                    ),
                    "entry_profile": (
                        RECORD_RETELLING_ENTRY_PROFILE
                        if external_input_ready
                        else None
                    ),
                    "capabilities": {
                        "parse": True,
                        "audio": True,
                        "normalize": True,
                        "external_input": external_input_ready,
                    },
                })
        result = self._result(items)
        if record_answers:
            result["recording_questions"] = [
                {
                    "number": number,
                    "score": record_score if record_score is not None else 1,
                    "answers": [answer],
                }
                for number, answer in sorted(record_answers.items())
            ]
        if items:
            recording = {
                "listening_text": items[0].get("text", ""),
                "questions": result.get("recording_questions", []),
            }
            if recording_table_index is not None:
                recording["table_image_required"] = True
                recording["table_index"] = recording_table_index
            result["recording"] = recording
        if retelling_prompt or retelling_answers:
            result["retelling"] = {
                "prompt": retelling_prompt or "",
                "score": retelling_score if retelling_score is not None else 0,
                "answer_time": retelling_answer_time,
                "reference_answers": retelling_answers,
            }
        computed_recording_score = sum(
            question.get("score", 0)
            for question in result.get("recording_questions", [])
            if isinstance(question, Mapping)
            and isinstance(question.get("score"), (int, float))
        )
        computed_retelling_score = (
            retelling_score
            if retelling_prompt or retelling_answers
            else 0
        )
        if record_section_score is not None or computed_recording_score:
            result["section_scores"] = {
                "第一节听后记录": (
                    record_section_score
                    if record_section_score is not None
                    else computed_recording_score
                ),
                "第二节信息转述": (
                    computed_retelling_score
                    if computed_retelling_score
                    else 0
                ),
            }
        if major_section_score is not None or computed_recording_score or computed_retelling_score:
            result["section_score"] = (
                major_section_score
                if major_section_score is not None
                else computed_recording_score + computed_retelling_score
            )
            result["computed_score"] = computed_recording_score + computed_retelling_score
        return result
