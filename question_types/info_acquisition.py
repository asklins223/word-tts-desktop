"""「信息获取」题型切片：解析器与题型元数据。"""

import re

from audio_naming import audio_filename_stem_for_category, is_exam_paper_bundle
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

    RE_SECTION_START = re.compile(
        r'第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*听选信息'
    )
    RE_SECTION2_START = re.compile(r'第[二2２]节\s*[：:]?\s*回答问题')
    RE_ANY_SECTION = re.compile(r'第[一二三四五六七八九十百\d０-９]+节')
    RE_RECORDING_PROMPT = re.compile(r'(?:听下面|听第.+段|录音播放|各段播放|每段播放)')
    RE_ANSWER_RANGE = re.compile(r'第\s*(\d+)\s*[-—~～至到]\s*(\d+)')
    RE_SCRIPT = SCRIPT_MARKER_RE
    RE_QUESTION = re.compile(r'^(\d+)\s*[.．、）)]\s*(.+)')
    RE_SPEAKER_PREFIX = re.compile(r'^\s*(?:([WwMm])\s*[:：]|\(([WwMm])\))')
    RE_PAGE_SPEAKER_MARKER = re.compile(
        r'(?im)^[ \t]*(?:\([WwMm]\)(?:[ \t]*[:：])?|[WwMm][ \t]*[:：])[ \t]*'
    )
    RE_SCORE = re.compile(
        r'每(?:小题|道题|题)\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.IGNORECASE,
    )
    RE_FULL_SCORE = re.compile(
        r'满分\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.IGNORECASE,
    )

    @staticmethod
    def _number_value(value):
        raw = str(value or '').translate(
            str.maketrans('０１２３４５６７８９', '0123456789')
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

    @staticmethod
    def _question_prompt_with_options(prompt, options):
        """Return the one-line prompt used by the legacy recording card.

        The old page has no separate option editors for 信息获取.  Its
        recording-card stem is one rich-text line in the form
        ``Question? (Four. / Five. / Six.)``.  Keep the structured options on the
        page-input question as well; this string is specifically the TTS and
        legacy-card display form.
        """

        stem = sanitize(str(prompt or "")).strip()
        option_texts = []
        for option in options:
            if not isinstance(option, dict):
                continue
            option_text = sanitize(str(option.get("text") or "")).strip()
            if option_text:
                option_texts.append(option_text)
        return f"{stem} ({' / '.join(option_texts)})" if option_texts else stem

    @classmethod
    def _page_listening_text(cls, value):
        """Remove only parenthesized speaker labels from page-visible text.

        The raw ``text`` remains untouched for TTS speaker parsing.  The
        separate ``listening_text`` field is consumed by page input and the
        document review view, where ``(W)/(M)`` must not be shown.
        """

        return cls.RE_PAGE_SPEAKER_MARKER.sub('', sanitize(str(value or ''))).strip()

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
        use_exam_naming = is_exam_paper_bundle(self.paras)
        question_audio_items = {}

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
                if use_exam_naming:
                    script_idx_by_cat[category] = script_idx_by_cat.get(category, 0) + 1
                    filename_stem = audio_filename_stem_for_category(
                        category, script_idx_by_cat[category]
                    )
                    if filename_stem:
                        item.update({
                            "audio_filename_stem": filename_stem,
                            "type_path": [filename_stem.rsplit("-", 1)[0]],
                        })
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
            })

            category = f"{current_section_name}题目"
            idx_by_cat[category] = idx_by_cat.get(category, 0) + 1
            question_item = {
                "category": category,
                "number": question_number,
                "filename_stem": f"问题{question_number}",
                "voice": "male" if speaker == "M" else "female",
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
            question["prompt_audio_filename_stem"] = (
                question_item.get("audio_filename_stem")
                or question_item.get("filename_stem")
            )
            question_audio_items[id(question)] = question_item
            items.append(question_item)

        for position, (_, text, _) in enumerate(self.paras):
            value = str(text or '').strip()
            script_marker = match_script_marker(value)
            answer_marker = match_answer_marker(value)

            if self.RE_SECTION_START.search(value):
                flush()
                flush_questions(None)
                in_section = True
                current_category = "听选信息录音稿"
                current_section_name = "听选信息"
                question_order = 0
                last_qnum = 0
                next_qnum = None
                answer_block = False
                section_score = self._score(self.RE_SCORE, value)
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
                answer_block = False
                section_score = self._score(self.RE_SCORE, value)
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

            if collecting:
                if answer_marker:
                    flush()
                    answer_block = True
                    inline = (answer_marker.group(1) or '').strip()
                    number, answers = self._answer_line(inline)
                    if number is not None and answers:
                        answer_map[number] = answers
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
                if number is not None and answers:
                    answer_map[number] = answers
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
                        answer_map[number] = answers
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
                    audio_item = question_audio_items.get(id(question))
                    if audio_item is not None:
                        audio_item["text"] = self._question_prompt_with_options(
                            question["prompt"],
                            options,
                        )

        flush()
        flush_questions(None)

        for question in questions:
            question_number = question.get("number")
            references = answer_map.get(question_number)
            if question_number in answer_map:
                question["reference_answers"] = references
                question["reference_answers_source"] = "document"
            elif question.get("answer"):
                fallback_references = [
                    option["text"]
                    for option in question.get("options", [])
                    if option.get("option_id") == question.get("answer")
                ]
                if fallback_references:
                    question["reference_answers"] = fallback_references
                    question["reference_answers_source"] = "red_option"

        result = self._result(items)
        result["questions"] = questions
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
