"""「听后应答」题型切片：解析器与题型元数据。"""

import re

from audio_naming import audio_filename_stem, is_exam_paper_bundle
from document_profiles import (
    RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE,
    RESPONSE_ENTRY_PROFILE,
    RESPONSE_UNKNOWN_PROFILE,
    RESPONSE_TWO_SECTION_PROFILE,
    RESPONSE_TWO_SECTION_ENTRY_PROFILE,
)
from question_types.base import BaseParser
from question_types.text_utils import is_chinese, is_major_section_heading, sanitize
from question_types.text_utils import has_red_text_in_range


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
    _REQUIRES_DOCUMENT_BLOCKS = True

    @staticmethod
    def _capabilities(*, external_input: bool) -> dict[str, bool]:
        return {
            "parse": True,
            "audio": True,
            "normalize": True,
            "external_input": bool(external_input),
        }

    RE_SECTION_START = re.compile(r'听后应答')
    RE_PROMPT = re.compile(
        r'计算机语音提示.*?听下面\s*'
        r'(?P<count>[0-9０-９零〇一二两三四五六七八九十百]+)\s*个\s*句子',
        re.I,
    )
    RE_CONTROL = re.compile(
        r'计算机语音提示.*?朗读.*?应答语|'
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
    RE_ANSWER_PROMPT = re.compile(r'朗读.*?应答语', re.I)
    RE_SECOND_SECTION = re.compile(r'第\s*[二2２]\s*节')
    RE_RESPONSE_SUBSECTION = re.compile(r'^\s*第\s*[一二12１２]\s*节')
    RE_DIALOGUE_START = re.compile(r'^\s*(?:(\d+)\s*[.．、）)]\s*)?.*?听下面.*?对话')
    RE_SCREEN_QUESTION = re.compile(r'(?:屏幕(?:文字)?显示\s*[）)]\s*)?\s*Q\s*[：:]\s*(.+)', re.I)
    RE_SCORE = re.compile(
        r'每小题\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.I,
    )
    RE_FULL_SCORE = re.compile(
        r'满分\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.I,
    )
    RE_ANSWER_TIME = re.compile(
        r'(?P<seconds>[0-9０-９]+)\s*秒(?:钟)?',
        re.I,
    )
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

    @staticmethod
    def _answer_options(text, *, paragraph_metadata=None):
        """Extract the visible answer choices from a starred option row.

        The response documents use both ``★ A  ★ B`` and
        ``10.★ A  ★ B``.  The star is a display bullet, not a correctness
        marker.  Keep it in every option's visible text because the platform
        page renders these response choices with the star prefix.  Explicit
        red font is used only to identify the separate correct option.
        """

        value = str(text or '').strip()
        if not re.search(r'[★☆*]', value):
            return [], []
        markers = list(re.finditer(r'[★☆*]', value))
        options = []
        red_option_indexes = []
        for marker_index, marker in enumerate(markers):
            start = marker.end()
            end = markers[marker_index + 1].start() if marker_index + 1 < len(markers) else len(value)
            raw_chunk = value[start:end]
            left_trimmed = raw_chunk.lstrip()
            chunk_start = start + (len(raw_chunk) - len(left_trimmed))
            right_trimmed = left_trimmed.rstrip()
            chunk_end = chunk_start + len(right_trimmed)
            chunk = right_trimmed
            if chunk:
                option_text = sanitize(chunk)
                if not option_text:
                    continue
                options.append({
                    'option_id': chr(ord('A') + len(options)),
                    'text': f'★ {option_text}',
                })
                if has_red_text_in_range(
                    paragraph_metadata,
                    chunk_start,
                    chunk_end,
                ):
                    red_option_indexes.append(len(options) - 1)
        return options, red_option_indexes

    def _dialogue_response_items(self, *, prior_number, score, answer_time):
        """Parse the second response section: one dialogue and question each."""

        from question_types.text_utils import is_major_section_heading, match_answer_marker, match_script_marker

        section_pos = None
        response_seen = False
        for pos, (_, raw, _) in enumerate(self.paras):
            value = str(raw or '').strip()
            if self.RE_SECTION_START.search(value):
                response_seen = True
                continue
            if not response_seen:
                continue
            if is_major_section_heading(value) and not self.RE_RESPONSE_SUBSECTION.match(value):
                break
            if not self.RE_SECOND_SECTION.search(value):
                continue
            following = ' '.join(str(row[1] or '') for row in self.paras[pos + 1:pos + 3])
            if '对话' in following and ('回答问题' in following or '口头回答' in following or '问题作答' in following):
                section_pos = pos + 1
                break
        if section_pos is None:
            return [], []

        blocks = []
        current = None
        mode = ''

        def finish():
            nonlocal current, mode
            if current and current['lines']:
                blocks.append(current)
            current = None
            mode = ''

        for _, raw, _ in self.paras[section_pos:]:
            value = str(raw or '').strip()
            if not value:
                continue
            if is_major_section_heading(value):
                break
            start = self.RE_DIALOGUE_START.match(value)
            if start:
                finish()
                current = {'number': int(start.group(1)) if start.group(1) else None,
                           'lines': [], 'prompt': '', 'answers': []}
                continue
            if current is None:
                continue
            script = match_script_marker(value)
            if script:
                mode = 'script'
                if script.group(1):
                    current['lines'].append(script.group(1))
                continue
            answer = match_answer_marker(value)
            if answer:
                mode = 'answers'
                if answer.group(1):
                    current['answers'].append(sanitize(answer.group(1)))
                continue
            screen_question = self.RE_SCREEN_QUESTION.search(value)
            if screen_question:
                current['prompt'] = sanitize(screen_question.group(1))
                mode = ''
                continue
            if '计算机' in value or '屏幕' in value:
                mode = ''
                continue
            if mode == 'script':
                current['lines'].append(value)
            elif mode == 'answers' and re.search(r'[A-Za-z]', value):
                current['answers'].append(sanitize(value))
        finish()

        # Only infer a missing source number when the visible numbers, and
        # the preceding subsection if available, agree on one sequence.
        bases = {block['number'] - index for index, block in enumerate(blocks)
                 if block['number'] is not None}
        numbered_blocks = sum(block['number'] is not None for block in blocks)
        if prior_number is not None:
            bases.add(prior_number + 1)
        anchor_count = numbered_blocks + (prior_number is not None)
        base = next(iter(bases)) if len(bases) == 1 and anchor_count >= 2 else None
        items, questions = [], []
        for index, block in enumerate(blocks):
            number = block['number'] if block['number'] is not None else (base + index if base is not None else None)
            text = sanitize('\n'.join(block['lines']))
            item = {'category': '听后应答录音稿', 'text': text, 'section': '第二节'}
            question = {'prompt': block['prompt'], 'listening_text': text,
                        'reference_answers': block['answers'],
                        'reference_answers_source': 'document', 'section': '第二节'}
            if number is not None:
                item.update(number=number, question_number=number, question_numbers=[number])
                question['number'] = number
                if block['number'] is None:
                    question['number_inferred'] = True
            if score is not None:
                item['score'] = question['score'] = score
            if answer_time is not None:
                item['answer_time'] = question['answer_time'] = answer_time
            items.append(item)
            questions.append(question)
        return items, questions

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
        questions = []
        section_score = None
        declared_item_scores = []
        declared_section_scores = []
        response_section_seen = False
        answer_time = None
        response_prompt_positions = {}
        current_prompt_position = None

        def parse_score_value(raw):
            normalized = str(raw or '').translate(
                str.maketrans('０１２３４５６７８９', '0123456789')
            )
            try:
                score = float(normalized)
            except ValueError:
                return None
            return int(score) if score.is_integer() else score

        def apply_source_number(item, question_number):
            if question_number is None:
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
                item = {
                    "category": "听后应答录音稿",
                    "index": response_index,
                    "number": response_index,
                    "filename_stem": f"{grade_prefix}-应答-{response_index}",
                    "voice": "female",
                    "text": line,
                }
                if section_score is not None:
                    item["score"] = section_score
                if answer_time is not None:
                    item["answer_time"] = answer_time
                items.append(item)
                question = {
                    "number": response_index,
                    "prompt": line,
                    "listening_text": line,
                }
                if section_score is not None:
                    question["score"] = section_score
                if answer_time is not None:
                    question["answer_time"] = answer_time
                questions.append(question)
            collecting = False
            expected_count = None
            current_lines = []

        for position, (_, text, _) in enumerate(self.paras):
            value = str(text or '').strip()
            if (response_section_seen and is_major_section_heading(value)
                    and not self.RE_SECTION_START.search(value)
                    and not self.RE_RESPONSE_SUBSECTION.match(value)):
                flush()
                break
            if self.RE_SECTION_START.search(value):
                response_section_seen = True
            score_match = self.RE_SCORE.search(value)
            if response_section_seen and score_match:
                section_score = parse_score_value(score_match.group('score'))
                if section_score is not None:
                    declared_item_scores.append(section_score)
            full_score_match = self.RE_FULL_SCORE.search(value)
            if response_section_seen and full_score_match:
                parsed_full_score = parse_score_value(full_score_match.group('score'))
                if parsed_full_score is not None and not declared_section_scores:
                    declared_section_scores.append(parsed_full_score)
            time_match = self.RE_ANSWER_TIME.search(value)
            if response_section_seen and time_match and any(
                marker in value for marker in ('朗读', '倒计时', '答题')
            ):
                parsed_answer_time = parse_score_value(time_match.group('seconds'))
                if parsed_answer_time is not None:
                    answer_time = parsed_answer_time
                    # Some documents put the countdown paragraph after the
                    # answer prompt has already flushed the sentence. Fill
                    # that just-created question retroactively, but never
                    # overwrite an explicit earlier duration.
                    if questions and '倒计时' in value:
                        questions[-1].setdefault('answer_time', answer_time)
            prompt = self.RE_PROMPT.search(value)
            if prompt:
                flush()
                awaiting_answer_choices = False
                collecting = True
                current_prompt_position = self.paras[position][0]
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
                    paragraph_metadata = (
                        self.paragraph_metadata[position]
                        if position < len(self.paragraph_metadata)
                        else {}
                    )
                    options, red_option_indexes = self._answer_options(
                        value,
                        paragraph_metadata=paragraph_metadata,
                    )
                    if options and 1 <= item_index <= len(questions):
                        question = questions[item_index - 1]
                        response_prompt_positions[item_index] = current_prompt_position
                        question["options"] = options
                        if len(red_option_indexes) == 1:
                            question["answer"] = options[red_option_indexes[0]]["option_id"]
                        elif len(red_option_indexes) > 1:
                            question["answer"] = None
                            question["answer_status"] = "ambiguous_red"
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
        if answer_numbers:
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
                    # 专项卷的录音文件仍按 1..N 命名，但文稿核对必须保留
                    # Word 原文的题号（本画像为 9..15），不能显示成 1..7。
                    apply_source_number(items[item_index - 1], number)
                    if item_index <= len(questions):
                        questions[item_index - 1]["number"] = number
        # Associate only images inside this question's own prompt window.
        # A bitmap elsewhere in the paper must never be borrowed by proximity.
        for item_index, start in response_prompt_positions.items():
            if start is None:
                continue
            end = next((source_index for source_index, value, _ in self.paras
                        if source_index > start and (self.RE_PROMPT.search(value)
                           or self.RE_SECOND_SECTION.search(value)
                           or is_major_section_heading(value))), None)
            images = [block for block in self.document_blocks
                      if block.kind == 'embedded_image'
                      and start <= block.metadata.get('paragraph_index', -1)
                      and (end is None or block.metadata.get('paragraph_index', -1) < end)]
            if len(images) == 1:
                questions[item_index - 1].update({
                    'block_image_required': True,
                    'block_kind': 'embedded_image',
                    'block_index': images[0].metadata['embedded_image_index'],
                })

        prior_number = questions[-1].get('number') if answer_numbers and questions else None
        dialogue_items, dialogue_questions = self._dialogue_response_items(
            prior_number=prior_number, score=section_score, answer_time=answer_time,
        )
        if dialogue_items:
            for item, question in zip(items, questions):
                item['section'] = question['section'] = '第一节'
            for item in dialogue_items:
                item['index'] = len(items) + 1
                items.append(item)
            questions.extend(dialogue_questions)
        if use_exam_naming:
            for ordinal, item in enumerate(items, start=1):
                filename_stem = audio_filename_stem(["听后应答"], ordinal)
                item.update({
                    "type_path": ["听后应答"],
                    "filename_stem": filename_stem,
                    "audio_filename_stem": filename_stem,
                })
        profile_confirmed = bool(
            response_section_seen
            and len(items) == 7
            and len(questions) == 7
            and all(
                isinstance(question.get("options"), list)
                and len(question["options"]) == 2
                and question.get("block_image_required") is not True
                and question.get("answer") in {
                    option.get("option_id") for option in question["options"]
                }
                for question in questions
            )
        )
        external_input_ready = bool(
            profile_confirmed
            and all(
                isinstance(question.get("score"), (int, float))
                and not isinstance(question.get("score"), bool)
                and isinstance(question.get("answer_time"), (int, float))
                and not isinstance(question.get("answer_time"), bool)
                for question in questions
            )
        )
        two_section_confirmed = bool(
            len(questions) == len(items) == 10
            and [question.get("section") for question in questions] == ["第一节"] * 5 + ["第二节"] * 5
            and all(
                isinstance(question.get("options"), list)
                and len(question["options"]) == 3
                and question.get("answer") in {option.get("option_id") for option in question["options"]}
                for question in questions[:5]
            )
            and all(question.get("prompt") and question.get("listening_text")
                    and question.get("reference_answers") for question in questions[5:])
        )
        two_section_ready = bool(two_section_confirmed and all(
            isinstance(question.get("score"), (int, float))
            and not isinstance(question.get("score"), bool)
            and isinstance(question.get("answer_time"), (int, float))
            and not isinstance(question.get("answer_time"), bool)
            for question in questions
        ))
        for item in items:
            item.update({
                "major_section_profile": (
                    RESPONSE_TWO_SECTION_PROFILE if two_section_confirmed else RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE
                    if profile_confirmed
                    else RESPONSE_UNKNOWN_PROFILE
                ),
                "entry_profile": (
                    RESPONSE_TWO_SECTION_ENTRY_PROFILE if two_section_ready else RESPONSE_ENTRY_PROFILE
                    if external_input_ready
                    else None
                ),
                "capabilities": self._capabilities(
                    external_input=external_input_ready or two_section_ready,
                ),
            })
        result = self._result(items)
        computed_score = sum(
            score for score in (
                question.get("score") for question in questions
            ) if isinstance(score, (int, float))
        )
        if declared_item_scores:
            result["score_per_item"] = declared_item_scores[0] if len(set(declared_item_scores)) == 1 else None
        result["section_score"] = (
            declared_section_scores[0]
            if declared_section_scores
            else computed_score
        )
        result["computed_score"] = computed_score
        result["questions"] = questions
        return result
