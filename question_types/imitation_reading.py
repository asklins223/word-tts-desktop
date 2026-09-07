"""「模仿朗读」题型切片：解析器与题型元数据。"""

import re

from audio_naming import audio_filename_stem, is_exam_paper_bundle
from document_profiles import (
    IMITATION_BOXED_SPECIAL_PROFILE,
    IMITATION_LEGACY_UNIT_SOURCE_PROFILE,
    IMITATION_NUMBERED_EXAM_SPECIAL_PROFILE,
    IMITATION_READING_ENTRY_PROFILE,
    IMITATION_UNIT_SOURCE_SPECIAL_PROFILE,
)

from question_types.base import BaseParser
from question_types.text_utils import (
    MAJOR_SECTION_RE,
    is_chinese,
    is_major_section_heading,
    sanitize,
)


# ============================================================================
# 6. 模仿朗读解析器
# ============================================================================

class ImitationReadingParser(BaseParser):
    """
    解析「模仿朗读」文档。
    同时支持旧版每个单元的「外网」/「教材」素材、按录音稿拆卷的新式
    U/来源文档，以及题目边框内的英文短文。

    文档结构示例:
        7年级上学期模仿朗读试题
        U5：
          一、模仿朗读（共 6 分）
          听以下短文一遍...
          外网：
          English text 1
          外网：
          English text 2
          教材：
          English text 3
          请听录音。...
        U6：

    新版文档结构示例:
        16.（计算机语音和屏幕文字提示）...模仿朗读...
        [带边框的英文表格]
          ...

    支持同一文件内包含多个单元。
    """

    DOC_TYPE = "模仿朗读"
    _REQUIRES_DOCUMENT_BLOCKS = True

    @staticmethod
    def _capabilities(*, external_input: bool) -> dict[str, bool]:
        """Expose audio/entry capabilities without changing the TTS text."""

        return {
            "parse": True,
            "audio": True,
            "normalize": True,
            "external_input": bool(external_input),
        }

    # 单元标记：U5 / U5： / u5 / U 5
    RE_UNIT = re.compile(r'^[Uu]\s*(\d+)\s*[：:]?\s*$')
    # 来源标记：外网： / 教材：
    RE_SOURCE = re.compile(r'^(外网|教材)\s*[：:]\s*$')
    # 结束标记
    RE_END = re.compile(r'请听录音|开始录音|停止录音')
    # 新版试卷把题号提示放在普通段落，把真正朗读的英文放在带边框的表格中。
    # 只把含“模仿朗读”的编号段落作为新规则的判定依据，避免普通英文表格
    # 或旧版“外网/教材”文档被误切换到新规则。
    RE_BOXED_QUESTION = re.compile(
        r'^\s*(?P<number>\d+)\s*[.．、）)]\s*.*?模仿朗读',
        re.IGNORECASE,
    )
    # 题型专项文档有一种容易混淆的排版：标题仍写“共1题”，但文件中
    # 连续放入了两个带完整操作提示和正文的编号块。题号本身不是边界，
    # 只有在声明题量与重复完整块同时出现时，才把它们识别为多个专项卷。
    RE_EXAM_QUESTION = RE_BOXED_QUESTION
    # 套卷旧题型通常只有“模仿朗读”大题标题，正文直接跟在操作提示后，
    # 不再使用“外网/教材”标签或表格。标题/边界规则保持题型无关的形态，
    # 具体正文仍由英文载荷判断，避免把中文操作提示做成音频。
    RE_EXAM_SECTION = re.compile(
        r'^\s*(?:(?:\d+|[一二三四五六七八九十百]+)\s*[、.．)]\s*)?'
        r'模仿朗读(?:题型?|试题)?(?:\s*[（(：:]|\s*$)',
        re.IGNORECASE,
    )
    RE_MAJOR_SECTION = MAJOR_SECTION_RE
    RE_EXAM_ANSWER = re.compile(
        r'^\s*[【\[（(]?\s*(?:参考答案|答案|解析)\s*'
        r'(?=\s|[：:【\[（(】\]）)]|$)'
        r'[】\]）)]?\s*[：:]?'
    )
    RE_EXAM_CONTROL = re.compile(
        r'听以下|听下面|准备|开始录音|停止录音|录音播放|'
        r'计算机|信号|时间|模仿朗读',
        re.IGNORECASE,
    )
    RE_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]+')
    RE_SCORE = re.compile(
        r'(?:满分|每(?:道|题|小题)|分值|共\s*[0-9０-９零〇一二两三四五六七八九十百]+\s*题?)'
        r'\s*[：:]?\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.IGNORECASE,
    )
    RE_SCORE_FALLBACK = re.compile(
        r'(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分',
        re.IGNORECASE,
    )
    RE_EXAM_QUESTION_COUNT = re.compile(
        r'共\s*(?P<count>[0-9０-９零〇一二两三四五六七八九十百]+)\s*'
        r'(?:道\s*|小\s*题?\s*|题\s*)',
        re.IGNORECASE,
    )

    def __init__(self, filepath, **kwargs):
        super().__init__(filepath, **kwargs)
        self._boxed_table_texts_cache = None

    def parse(self):
        """按文档结构选择新旧规则；旧规则的输出保持不变。"""
        if self._is_boxed_english_format():
            boxed_result = self._parse_boxed_english_format()
            legacy_result = self._parse_legacy_format()
            # 同一份资料偶尔会把旧版单元素材和新版框选短文放在一起；
            # 两套规则都命中时合并结果，不能因为新版存在就丢掉旧素材。
            if legacy_result["items"]:
                return self._result(legacy_result["items"] + boxed_result["items"])
            return boxed_result
        legacy_result = self._parse_legacy_format()
        unit_source_result = self._parse_unit_source_special_format(legacy_result)
        if unit_source_result is not None:
            return unit_source_result
        if legacy_result["items"]:
            return legacy_result
        return self._parse_exam_paragraph_format()

    def _boxed_question_numbers(self):
        numbers = []
        for _, text, _ in self.paras:
            match = self.RE_BOXED_QUESTION.match(text)
            if match:
                numbers.append(int(match.group('number')))
        return numbers

    @staticmethod
    def _parse_number_token(value):
        """Parse the small Arabic/Chinese number forms used in exam headings."""

        token = str(value or '').strip().translate(
            str.maketrans('０１２３４５６７８９', '0123456789')
        )
        if token.isdigit():
            return int(token)
        digits = {
            '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3,
            '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
        }
        units = {'十': 10, '百': 100}
        if not token or any(char not in digits and char not in units for char in token):
            return None
        section = 0
        current = 0
        for char in token:
            if char in digits:
                current = digits[char]
                continue
            section += (current or 1) * units[char]
            current = 0
        return section + current

    def _declared_exam_question_count(self):
        for _, text, _ in self.paras:
            match = self.RE_EXAM_QUESTION_COUNT.search(str(text or ''))
            if match:
                return self._parse_number_token(match.group('count'))
        return None

    def _should_split_numbered_exam_blocks(self, question_numbers):
        """Detect the bundled-special-paper layout without trusting numbers alone."""

        unique_numbers = list(dict.fromkeys(question_numbers))
        if len(unique_numbers) < 2 or len(unique_numbers) != len(question_numbers):
            return False
        declared_count = self._declared_exam_question_count()
        # A heading that says “共1题” while two complete numbered blocks follow
        # is the reliable signal used by the supplied 九上 Unit 1 document.
        if declared_count == 1:
            return not is_exam_paper_bundle(self.paras)
        # If the heading is omitted, keep the fallback narrow: only a direct
        # paragraph document with unique numbered blocks can use this rule.
        return declared_count is None and not is_exam_paper_bundle(self.paras)

    @classmethod
    def _english_only(cls, text):
        """从表格单元格中去掉中文说明，只保留带英文的内容。"""
        value = sanitize(str(text or ''))
        if not re.search(r'[A-Za-z]', value):
            return ''
        value = sanitize(cls.RE_CJK.sub(' ', value)).strip(' ：:')
        return value if re.search(r'[A-Za-z]', value) else ''

    def _boxed_table_texts(self):
        if self._boxed_table_texts_cache is not None:
            return self._boxed_table_texts_cache

        texts = []
        # 表格本身没有“属于哪道题”的字段。按正文顺序绑定：只有紧跟在
        # “模仿朗读”题目提示之后的第一个英文表格才属于该题，后面的
        # 听后记录表、答题表不能再被误识别成朗读稿。结构块已由基类在
        # 同一次 Document 加载中生成，避免这里重复打开文件。
        blocks = self.document_blocks

        waiting_for_table = False
        for block in blocks:
            if block.kind == 'paragraph':
                if self.RE_BOXED_QUESTION.match(block.text.strip()):
                    waiting_for_table = True
                continue
            if block.kind != 'table' or not waiting_for_table:
                continue
            text = self._english_only(block.text)
            if text:
                texts.append(text)
                waiting_for_table = False
        self._boxed_table_texts_cache = texts
        return texts

    def _is_boxed_english_format(self):
        return bool(self._boxed_question_numbers() and self._boxed_table_texts())

    def _parse_boxed_english_format(self):
        """新规则：每个英文框是一篇模仿朗读稿，默认使用女声。"""
        question_numbers = self._boxed_question_numbers()
        texts = self._boxed_table_texts()
        items = []
        use_exam_naming = is_exam_paper_bundle(self.paras)
        # A boxed imitation-reading专项 is laid out as one complete
        # recording task per numbered passage. Keep each passage as its own
        # input unit even when the heading says “共2题”; that count describes
        # the source document, not one platform paper containing two tasks.
        split_as_units = (
            not use_exam_naming
            or self._should_split_numbered_exam_blocks(question_numbers)
        )
        section_score = None
        questions = []
        for _, paragraph, _ in self.paras:
            value = str(paragraph or '').strip()
            if not self.RE_EXAM_SECTION.match(value):
                continue
            match = self.RE_SCORE.search(value)
            if match is None:
                continue
            raw_score = str(match.group('score')).translate(
                str.maketrans('０１２３４５６７８９', '0123456789')
            )
            try:
                parsed_score = float(raw_score)
            except ValueError:
                continue
            section_score = int(parsed_score) if parsed_score.is_integer() else parsed_score
            break
        for index, text in enumerate(texts):
            number = question_numbers[index] if index < len(question_numbers) else index + 1
            item = {
                "category": "模仿朗读-框内英文",
                "number": number,
                "source": "框内英文",
                "voice": "female",
                "text": text,
                "exam_form": "paper" if use_exam_naming else "special",
                "major_section_profile": IMITATION_BOXED_SPECIAL_PROFILE,
                "entry_profile": IMITATION_READING_ENTRY_PROFILE,
                "capabilities": self._capabilities(external_input=section_score is not None),
            }
            # Only a complete listening exam carries an answer row for
            # imitation reading. A topic-specific special paper records the
            # passage and score only.
            if use_exam_naming:
                item["reference_answers"] = [text]
            if section_score is not None:
                item["score"] = section_score
            if split_as_units:
                unit_label = f"第{number}题专项卷"
                item.update({
                    "unit": unit_label,
                    "unit_id": f"imitation-reading-question-{number}",
                    "unit_label": unit_label,
                    "question_numbers": [number],
                    "type_path": ["模仿朗读"],
                })
            if use_exam_naming:
                filename_stem = audio_filename_stem(["模仿朗读"], index + 1)
                item.update({
                    "question_numbers": [number],
                    "type_path": ["模仿朗读"],
                    "filename_stem": filename_stem,
                    "audio_filename_stem": filename_stem,
                })
            items.append(item)
            question = {
                "number": number,
                "listening_text": text,
                "score": section_score,
            }
            if use_exam_naming:
                question["reference_answers"] = [text]
            questions.append(question)
        result = self._result(items)
        result["exam_form"] = "paper" if use_exam_naming else "special"
        result["questions"] = questions
        computed_score = sum(
            item.get("score", 0)
            for item in items
            if isinstance(item.get("score"), (int, float))
        )
        if section_score is not None:
            result["score_per_item"] = section_score
            result["section_score"] = section_score * len(items)
        else:
            result["section_score"] = computed_score
        result["computed_score"] = computed_score
        return result

    def _parse_legacy_format(self):
        """旧版 U 单元 + 外网/教材规则，保留原有行为。"""
        items = []
        current_unit = ""
        current_source = None    # "外网" 或 "教材"
        current_lines = []

        def flush():
            nonlocal current_lines, current_source
            if current_source and current_lines:
                items.append({
                    "category": f"模仿朗读-{current_source}",
                    "unit": current_unit,
                    "source": current_source,
                    "text": sanitize('\n'.join(current_lines)),
                    "major_section_profile": IMITATION_LEGACY_UNIT_SOURCE_PROFILE,
                    "entry_profile": None,
                    "capabilities": self._capabilities(external_input=False),
                })
            current_lines = []
            current_source = None

        for _, text, _ in self.paras:
            # ---- 单元标记 ----
            m_unit = self.RE_UNIT.match(text)
            if m_unit:
                flush()
                current_unit = f"U{m_unit.group(1)}"
                continue

            # ---- 来源标记 ----
            m_src = self.RE_SOURCE.match(text)
            if m_src:
                flush()
                current_source = m_src.group(1)
                continue

            # ---- 结束标记 ----
            if self.RE_END.search(text):
                flush()
                continue

            # ---- 收集朗读内容 ----
            if current_source:
                # 跳过纯中文说明文字
                if not is_chinese(text):
                    current_lines.append(text)

        flush()
        return self._result(items)

    @classmethod
    def _score_from_section_heading(cls, value):
        """Read a score only from a recognized section heading."""

        match = cls.RE_SCORE.search(str(value or '')) or cls.RE_SCORE_FALLBACK.search(
            str(value or '')
        )
        if match is None:
            return None
        raw_score = str(match.group('score')).translate(
            str.maketrans('０１２３４５６７８９', '0123456789')
        )
        try:
            parsed_score = float(raw_score)
        except ValueError:
            return None
        return int(parsed_score) if parsed_score.is_integer() else parsed_score

    def _unit_source_section_scores(self):
        """Return scores for U/source sections without changing the legacy path."""

        current_unit = None
        scores = {}
        for _, text, _ in self.paras:
            value = str(text or '').strip()
            unit_match = self.RE_UNIT.match(value)
            if unit_match:
                current_unit = f"U{unit_match.group(1)}"
                continue
            if current_unit is None or not self.RE_EXAM_SECTION.match(value):
                continue
            score = self._score_from_section_heading(value)
            if score is not None:
                scores[current_unit] = score
        return scores

    @staticmethod
    def _unit_source_id_slug(unit, source):
        unit_slug = re.sub(r'[^a-z0-9]+', '-', str(unit or '').casefold()).strip('-') or 'unit'
        source_slug = {
            '外网': 'external',
            '教材': 'textbook',
        }.get(str(source or '').strip(), 'source')
        return f"{unit_slug}-{source_slug}"

    def _parse_unit_source_special_format(self, legacy_result):
        """New rule: each U/source recording script becomes one paper.

        The source extraction is deliberately borrowed from the legacy parser,
        but the emitted profile and page-entry facts are different.  That keeps
        the old audio-only U/source rule closed while allowing only documents
        with an explicit scored section heading to enter the new paper flow.
        """

        legacy_items = legacy_result.get("items") if isinstance(legacy_result, dict) else []
        if not legacy_items:
            return None
        section_scores = self._unit_source_section_scores()
        if not section_scores:
            return None
        if any(item.get("unit") not in section_scores for item in legacy_items):
            # Do not partially upgrade a mixed/ambiguous document.
            return None

        counters = {}
        items = []
        for legacy_item in legacy_items:
            unit = legacy_item.get("unit") or ""
            source = legacy_item.get("source") or "录音稿"
            counters[(unit, source)] = counters.get((unit, source), 0) + 1
            ordinal = counters[(unit, source)]
            score = section_scores[unit]
            unit_label = f"{unit} · {source} · 第{ordinal}稿"
            unit_id = (
                f"imitation-reading-script-"
                f"{self._unit_source_id_slug(unit, source)}-{ordinal}"
            )
            item = dict(legacy_item)
            item.update({
                "category": "模仿朗读-录音稿",
                # Each emitted paper contains exactly one platform question;
                # keep the document view's question label meaningful even
                # though the source layout has no printed question number.
                "number": 1,
                "major_section_profile": IMITATION_UNIT_SOURCE_SPECIAL_PROFILE,
                "entry_profile": IMITATION_READING_ENTRY_PROFILE,
                "capabilities": self._capabilities(external_input=True),
                "exam_form": "special",
                "score": score,
                # ``number`` is intentionally 1 in every standalone paper.
                # Use the stable per-script unit id as the normalized identity
                # too, otherwise identical scripts would collide in the
                # workflow item table before unit grouping can separate them.
                "identity_key": unit_id,
                "unit_id": unit_id,
                "unit_label": unit_label,
                "type_path": ["模仿朗读", "录音稿"],
                "source_locator": f"{unit}/{source}/录音稿{ordinal}",
            })
            items.append(item)

        result = self._result(items)
        result["exam_form"] = "special"
        computed_score = sum(
            item.get("score", 0)
            for item in items
            if isinstance(item.get("score"), (int, float))
        )
        unique_scores = {
            item.get("score")
            for item in items
            if isinstance(item.get("score"), (int, float))
        }
        if len(unique_scores) == 1:
            result["score_per_item"] = next(iter(unique_scores))
        result["section_score"] = computed_score
        result["computed_score"] = computed_score
        result["unit_scores"] = dict(section_scores)
        return result

    def _parse_exam_paragraph_format(self):
        """解析套卷中直接排版的单篇模仿朗读正文。"""

        items = []
        in_section = False
        answer_block = False
        current_lines = []
        current_question_number = None
        current_question_score = None
        section_score = None
        use_exam_naming = is_exam_paper_bundle(self.paras)
        question_numbers = [
            int(match.group('number'))
            for _, paragraph, _ in self.paras
            if (match := self.RE_EXAM_QUESTION.match(str(paragraph or '').strip()))
        ]
        split_as_units = self._should_split_numbered_exam_blocks(question_numbers)

        def score_from_text(value):
            match = self.RE_SCORE.search(str(value or ''))
            if match is None:
                # 保守兜底：只在当前题型段落中读取带“分”的数字，避免把
                # 90 秒准备时间等控制参数误认为分值。
                match = self.RE_SCORE_FALLBACK.search(str(value or ''))
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

        def flush():
            nonlocal current_question_number, current_question_score
            if not current_lines:
                current_question_number = None
                current_question_score = None
                return
            question_number = current_question_number or len(items) + 1
            item = {
                'category': '模仿朗读-试卷正文',
                'number': question_number,
                'source': '试卷正文',
                'voice': 'female',
                'text': sanitize('\n'.join(current_lines)),
                'exam_form': 'paper' if use_exam_naming else 'special',
                'major_section_profile': IMITATION_NUMBERED_EXAM_SPECIAL_PROFILE,
                'entry_profile': IMITATION_READING_ENTRY_PROFILE,
            }
            # Directly embedded imitation text in a complete exam is a
            # reference answer; a standalone special paper is not.
            if use_exam_naming:
                item['reference_answers'] = [item['text']]
            score = (
                current_question_score
                if current_question_score is not None
                else section_score
            )
            if score is not None:
                item['score'] = score
            item['capabilities'] = self._capabilities(external_input=score is not None)
            if split_as_units:
                unit_label = f"第{item['number']}题专项卷"
                item.update({
                    'unit': unit_label,
                    'unit_id': f"imitation-reading-question-{item['number']}",
                    'unit_label': unit_label,
                    'question_numbers': [item['number']],
                    'type_path': ['模仿朗读'],
                })
            if use_exam_naming:
                filename_stem = audio_filename_stem(
                    ['模仿朗读'], len(items) + 1
                )
                item.update({
                    'question_numbers': [item['number']],
                    'type_path': ['模仿朗读'],
                    'filename_stem': filename_stem,
                    'audio_filename_stem': filename_stem,
                })
            items.append(item)
            current_lines.clear()
            current_question_number = None
            current_question_score = None

        for _, text, _ in self.paras:
            value = str(text or '').strip()
            if not value:
                continue
            question_match = self.RE_EXAM_QUESTION.match(value)
            if question_match:
                flush()
                in_section = True
                answer_block = False
                current_question_number = int(question_match.group('number'))
                current_question_score = score_from_text(value)
                continue
            if self.RE_EXAM_SECTION.match(value):
                flush()
                in_section = True
                answer_block = False
                section_score = score_from_text(value) or section_score
                continue
            if in_section and is_major_section_heading(value):
                flush()
                in_section = False
                answer_block = False
                continue
            if not in_section:
                continue
            if self.RE_EXAM_ANSWER.match(value):
                flush()
                answer_block = True
                continue
            if answer_block:
                continue
            # 直接排版的试卷正文只保留纯英文段落；混合中文说明和
            # 英文开头提示（如“你可以这样开始：Let me ...”）也不能
            # 被误当成朗读稿。
            if self.RE_EXAM_CONTROL.search(value) or self.RE_CJK.search(value):
                continue
            if re.search(r'[A-Za-z]', value):
                current_lines.append(value)

        flush()
        result = self._result(items)
        result['exam_form'] = 'paper' if use_exam_naming else 'special'
        computed_score = sum(
            item.get("score", 0)
            for item in items
            if isinstance(item.get("score"), (int, float))
        )
        if section_score is not None:
            result["score_per_item"] = section_score
            result["section_score"] = section_score * len(items)
        else:
            result["section_score"] = computed_score
        result["computed_score"] = computed_score
        return result
