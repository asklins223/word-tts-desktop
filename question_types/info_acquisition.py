"""「信息获取」题型切片：解析器与题型元数据。"""

import re
from collections import Counter

from audio_naming import audio_filename_stem_for_category
from question_types.base import BaseParser
from question_types.text_utils import (
    MAJOR_SECTION_RE,
    SCRIPT_MARKER_RE,
    has_red_text_in_range,
    is_chinese,
    is_major_section_heading,
    match_answer_marker,
    match_script_marker,
    sanitize,
)


class InfoAcquisitionParser(BaseParser):
    """Parse the two sections of the legacy 信息获取 paper layout.

    The source places questions before their listening script.  ``questions``
    is therefore a semantic side channel: each question carries the ordinal
    of the following script, while ``items`` remains the audio list used by
    the existing audio view.
    """

    DOC_TYPE = "信息获取"
    _REPEAT_MAJOR_SECTIONS = True

    RE_SECTION_START = re.compile(
        r'第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*听选信息'
    )
    RE_SECTION2_START = re.compile(r'第[二2２]节\s*[：:]?\s*回答问题')
    RE_ANY_SECTION = re.compile(r'第[一二三四五六七八九十百\d０-９]+节')
    RE_RECORDING_PROMPT = re.compile(r'(?:听下面|听第.+段|录音播放|各段播放|每段播放)')
    RE_QUESTION_READING_PROMPT = re.compile(
        r'[\d０-９零〇一二两三四五六七八九十百]+\s*秒(?:钟)?(?:的)?'
        r'(?:\s*时间)?\s*阅读.+(?:问题|小题)'
    )
    RE_ANSWER_RANGE = re.compile(r'第\s*(\d+)\s*[-—~～至到]\s*(\d+)')
    RE_SCRIPT = SCRIPT_MARKER_RE
    RE_QUESTION = re.compile(r'^(\d+)\s*[.．、）)]\s*(.+)')
    RE_SPEAKER_PREFIX = re.compile(r'^\s*(?:([WwMm])\s*[:：]|\(([WwMm])\))')
    RE_PAGE_PAREN_SPEAKER_MARKER = re.compile(
        r'(?im)^[ \t]*\([WwMm]\)(?:[ \t]*[:：])?[ \t]*'
    )
    RE_SCORE = re.compile(
        r'每(?:小题|道题|题)\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.IGNORECASE,
    )
    RE_SCORE_FORMULA = re.compile(
        r'(?P<score>[0-9０-９]+(?:[.．][0-9０-９]+)?)\s*分\s*[×xX*＊]\s*[0-9０-９]+'
    )
    RE_FULL_SCORE = re.compile(
        r'满分\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.IGNORECASE,
    )

    @staticmethod
    def _number_value(value):
        raw = str(value or '').translate(
            str.maketrans('０１２３４５６７８９．', '0123456789.')
        )
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        return int(number) if number.is_integer() else number

    @classmethod
    def _options_from_line(cls, value):
        text = str(value or '').strip()
        if len(text) < 2 or text[0] not in '（(' or text[-1] not in '）)':
            return []
        body = text[1:-1].strip()
        choices = [sanitize(part) for part in re.split(r'\s*/\s*', body)]
        choices = [choice for choice in choices if choice]
        return [
            {"option_id": chr(ord('A') + index), "text": choice}
            for index, choice in enumerate(choices)
        ]

    @classmethod
    def _page_listening_text(cls, value):
        """Remove only parenthesized speaker labels from page-visible text.

        The raw ``text`` remains untouched for TTS speaker parsing.  The
        separate ``listening_text`` field is consumed by page input and the
        document review view, where ``(W)/(M)`` must not be shown while
        ``W:``/``M:`` must remain available for system input.
        """

        return cls.RE_PAGE_PAREN_SPEAKER_MARKER.sub(
            '', sanitize(str(value or ''))
        ).strip()

    @classmethod
    def _answer_line(cls, value):
        match = cls.RE_QUESTION.match(str(value or '').strip())
        if match is None:
            return None, []
        number = int(match.group(1))
        answers = [sanitize(part) for part in re.split(r'\s*/\s*', match.group(2))]
        return number, [answer for answer in answers if answer]

    @classmethod
    def _score(cls, pattern, value):
        match = pattern.search(str(value or ''))
        return cls._number_value(match.group('score')) if match else None

    def parse(self):
        items = []
        questions = []
        pending_questions = []
        answer_map = {}
        section_scope = 0
        in_section = False
        current_category = ""
        current_section_name = ""
        collecting = False
        current_lines = []
        script_idx = 0
        question_order = 0
        last_qnum = 0
        next_qnum = None
        answer_block = False
        section_score = None
        declared_item_scores = []
        declared_section_scores = []
        idx_by_cat = {}
        script_idx_by_cat = {}
        use_exam_naming = self.use_exam_naming

        def flush_questions(ordinal=None):
            nonlocal pending_questions
            for question in pending_questions:
                question["script_ordinal"] = ordinal
                questions.append(question)
            pending_questions = []

        def flush():
            nonlocal collecting, current_lines, script_idx
            if collecting and current_lines:
                script_idx += 1
                category = current_category
                item = {
                    "category": category,
                    "index": script_idx,
                    "text": sanitize('\n'.join(current_lines)),
                }
                if category == "回答问题录音稿":
                    item["listening_text"] = self._page_listening_text(item["text"])
                script_questions = [
                    question for question in questions
                    if question.get("script_ordinal") == script_idx
                ]
                if script_questions:
                    item["question_numbers"] = [
                        question["number"]
                        for question in script_questions
                        if question.get("number") is not None
                    ]
                    item["score"] = sum(
                        question.get("score", 0)
                        for question in script_questions
                        if isinstance(question.get("score"), (int, float))
                    )
                script_idx_by_cat[category] = script_idx_by_cat.get(category, 0) + 1
                if use_exam_naming:
                    filename_stem = audio_filename_stem_for_category(
                        category, script_idx_by_cat[category]
                    )
                    if filename_stem:
                        item.update({
                            "audio_filename_stem": filename_stem,
                            "type_path": [filename_stem.rsplit("-", 1)[0]],
                        })
                else:
                    # Keep the existing standalone audio names explicit so
                    # page facts and durable audio delivery share the same stem.
                    item["filename_stem"] = (
                        f"{current_section_name}-录音稿{script_idx_by_cat[category]}"
                    )
                items.append(item)
            collecting = False
            current_lines = []

        def add_question(value, number=None):
            nonlocal question_order, last_qnum, next_qnum
            question_order += 1
            question_number = number
            if question_number is None:
                question_number = next_qnum if next_qnum is not None else last_qnum + 1
            last_qnum = question_number
            next_qnum = question_number + 1
            stem = sanitize(value)
            speaker_match = self.RE_SPEAKER_PREFIX.match(stem)
            speaker = next(
                (
                    candidate.upper()
                    for candidate in (
                        speaker_match.group(1) if speaker_match else None,
                        speaker_match.group(2) if speaker_match else None,
                    )
                    if candidate
                ),
                "M" if question_order % 2 else "W",
            )
            pending_questions.append({
                "number": question_number,
                "prompt": stem,
                "stem": stem,
                "options": [],
                "answer": None,
                "score": section_score,
                "script_ordinal": None,
                "section_scope": section_scope,
                "source_locator": f"第 {source_index + 1} 段/{current_section_name}/第 {question_number} 题",
            })

            category = f"{current_section_name}题目"
            idx_by_cat[category] = idx_by_cat.get(category, 0) + 1
            question_item = {
                "category": category,
                "number": question_number,
                "filename_stem": f"问题{question_number}",
                "voice": "male" if speaker == "M" else "female",
                # Options are page-entry facts kept on the semantic question;
                # this item is the TTS projection and must contain only the
                # question stem.
                "text": stem,
                "audio_only_auxiliary": True,
            }
            if use_exam_naming:
                filename_stem = audio_filename_stem_for_category(
                    category, idx_by_cat[category]
                )
                if filename_stem:
                    question_item.update({
                        "audio_filename_stem": filename_stem,
                        "type_path": [filename_stem.rsplit("-", 1)[0]],
                    })
            question = pending_questions[-1]
            question_item["acquisition_question"] = question
            question_item["source_locator"] = question["source_locator"]
            question["prompt_audio_filename_stem"] = (
                question_item.get("audio_filename_stem")
                or question_item.get("filename_stem")
            )
            items.append(question_item)

        for position, (source_index, text, _) in enumerate(self.paras):
            value = str(text or '').strip()
            script_marker = match_script_marker(value)
            answer_marker = match_answer_marker(value)

            if self.RE_SECTION_START.search(value):
                flush()
                flush_questions(None)
                in_section = True
                current_category = "听选信息录音稿"
                current_section_name = "听选信息"
                section_scope += 1
                question_order = 0
                last_qnum = 0
                next_qnum = None
                answer_block = False
                section_score = self._score(self.RE_SCORE, value)
                if section_score is None:
                    section_score = self._score(self.RE_SCORE_FORMULA, value)
                if section_score is not None:
                    declared_item_scores.append(section_score)
                full_score = self._score(self.RE_FULL_SCORE, value)
                if full_score is not None:
                    declared_section_scores.append(full_score)
                continue

            if self.RE_SECTION2_START.search(value):
                flush()
                flush_questions(None)
                in_section = True
                current_category = "回答问题录音稿"
                current_section_name = "回答问题"
                section_scope += 1
                answer_block = False
                section_score = self._score(self.RE_SCORE, value)
                if section_score is None:
                    section_score = self._score(self.RE_SCORE_FORMULA, value)
                if section_score is not None:
                    declared_item_scores.append(section_score)
                full_score = self._score(self.RE_FULL_SCORE, value)
                if full_score is not None:
                    declared_section_scores.append(full_score)
                continue

            if in_section and self.RE_ANY_SECTION.search(value):
                if not self.RE_SECTION_START.search(value) and not self.RE_SECTION2_START.search(value):
                    flush()
                    flush_questions(None)
                    in_section = False
                    continue
            if in_section and is_major_section_heading(value):
                flush()
                flush_questions(None)
                in_section = False
                continue
            if not in_section:
                continue

            if collecting and self.RE_QUESTION.match(value) and re.search(r"[?？]\s*$", value):
                flush()

            if collecting:
                if answer_marker:
                    flush()
                    answer_block = True
                    inline = (answer_marker.group(1) or '').strip()
                    number, answers = self._answer_line(inline)
                    if number is not None and answers:
                        answer_map.setdefault((section_scope, number), []).append(answers)
                    continue
                if script_marker or self.RE_RECORDING_PROMPT.search(value):
                    flush()
                    answer_block = False
                    continue
                current_lines.append(value)
                continue

            if answer_marker:
                answer_block = True
                inline = (answer_marker.group(1) or '').strip()
                number, answers = self._answer_line(inline)
                if inline and pending_questions and (number is None or number == pending_questions[-1]["number"]):
                    # Per-question answers omit the number because they sit
                    # directly below the prompt/options. A trailing numbered
                    # answer block still uses its explicit question numbers.
                    if number is None:
                        answers = [sanitize(part) for part in re.split(r'\s*/\s*', inline)]
                        answers = [answer for answer in answers if answer]
                    pending_questions[-1]["reference_answers"] = answers
                    pending_questions[-1]["reference_answers_source"] = "document"
                    answer_block = False
                elif number is not None and answers:
                    answer_map.setdefault((section_scope, number), []).append(answers)
                continue

            if answer_block:
                if script_marker or self.RE_RECORDING_PROMPT.search(value):
                    answer_block = False
                elif (
                    (question_match := self.RE_QUESTION.match(value)) is not None
                    and re.search(r'[?？]\s*$', question_match.group(2))
                ):
                    # A numbered English prompt can follow an answer marker
                    # on the next line.  Answer rows in the trailing answer
                    # block normally do not end in a question mark.
                    answer_block = False
                else:
                    number, answers = self._answer_line(value)
                    if number is not None and answers:
                        answer_map.setdefault((section_scope, number), []).append(answers)
                        continue
                    # Inline/standalone answer blocks can be followed directly
                    # by the next question.  Leave the current line for the
                    # normal question matcher instead of swallowing it as an
                    # answer-block line.
                    answer_block = False

            if script_marker:
                flush()
                flush_questions(script_idx + 1)
                collecting = True
                remainder = (script_marker.group(1) or '').strip()
                if remainder:
                    current_lines.append(remainder)
                continue

            if self.RE_RECORDING_PROMPT.search(value):
                # Introductory instructions are template prose, not a prompt
                # card. The following numbered rows are still collected.
                m_range = self.RE_ANSWER_RANGE.search(value)
                if m_range:
                    next_qnum = int(m_range.group(1))
                continue

            question_match = self.RE_QUESTION.match(value)
            if question_match:
                add_question(question_match.group(2), int(question_match.group(1)))
                continue

            if (
                current_section_name == "回答问题"
                and self.RE_QUESTION_READING_PROMPT.search(value)
            ):
                # Reading-time instructions precede the numbered questions in
                # some exam papers. They are not an unnumbered question; if
                # emitted here they consume next_qnum and collide with the
                # first real question's filename_stem (for example, 问题7).
                continue

            # The 回答问题 section commonly numbers the range in its
            # instruction while leaving each following English prompt
            # unnumbered.  Once that range has supplied the next number, treat
            # the prose line as the next question and advance the sequence.
            if current_section_name == "回答问题" and next_qnum is not None:
                add_question(value)
                continue

            if pending_questions:
                options = self._options_from_line(value)
                if options:
                    question = pending_questions[-1]
                    question["options"] = options
                    # Options belong to the page stem; the prompt audio reads the question only.
                    metadata = (
                        self.paragraph_metadata[position]
                        if position < len(self.paragraph_metadata)
                        else {}
                    )
                    search_start = 0
                    red_option_ids = []
                    for option in options:
                        start = value.find(option["text"], search_start)
                        if start < 0:
                            start = search_start
                        end = start + len(option["text"])
                        if has_red_text_in_range(metadata, start, end):
                            red_option_ids.append(option["option_id"])
                        search_start = max(end, search_start)
                    if len(red_option_ids) == 1:
                        question["answer"] = red_option_ids[0]
                        question["reference_answers"] = [
                            option["text"]
                            for option in options
                            if option["option_id"] == red_option_ids[0]
                        ]
        flush()
        flush_questions(None)

        question_keys = Counter((question["section_scope"], question.get("number")) for question in questions)
        number_counts = Counter(question.get("number") for question in questions)
        issues = []
        for question in questions:
            key = (question["section_scope"], question.get("number"))
            answer_rows = answer_map.get(key, ())
            if not answer_rows and number_counts[key[1]] == 1:
                # Whole-section answer keys can follow both subsections.
                # The displayed number is sufficient only when it is unique
                # across the owned source range.
                answer_rows = [row for (_, number), rows in answer_map.items() if number == key[1] for row in rows]
            if answer_rows and question_keys[key] == 1:
                question["reference_answers"] = list(dict.fromkeys(answer for row in answer_rows for answer in row))
                question["reference_answers_source"] = "document"
            elif not question.get("reference_answers") and answer_rows:
                issues.append(f'{question["source_locator"]}的参考答案归属未确认')
            elif question.get("reference_answers_source") != "document" and question.get("answer"):
                fallback_references = [
                    option["text"]
                    for option in question.get("options", [])
                    if option.get("option_id") == question.get("answer")
                ]
                if fallback_references:
                    question["reference_answers"] = fallback_references
                    question["reference_answers_source"] = "red_option"

        # A spoken prompt keeps its material's answer object. Numbering may
        # restart between subsections, so neither answers nor audio may use
        # the displayed question number as a global identity.
        stems = Counter(item.get("filename_stem") for item in items if item.get("filename_stem"))
        for index, item in enumerate(items):
            stem = item.get("filename_stem")
            if stem and stems[stem] > 1:
                item["filename_stem"] = f"{stem}-q{index + 1}"
                if question := item.get("acquisition_question"):
                    question["prompt_audio_filename_stem"] = item.get("audio_filename_stem") or item["filename_stem"]

        result = self._result(items)
        result["questions"] = questions
        if issues:
            result["source_issue"] = "；".join(issues)
        computed_score = sum(
            question.get("score", 0)
            for question in questions
            if isinstance(question.get("score"), (int, float))
        )
        result["computed_score"] = computed_score
        result["section_score"] = (
            sum(declared_section_scores)
            if declared_section_scores
            else computed_score
        )
        if declared_item_scores:
            result["score_per_item"] = (
                declared_item_scores[0]
                if len(set(declared_item_scores)) == 1
                else None
            )
        return result
