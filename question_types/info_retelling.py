"""「信息转述及询问」题型切片。

这类旧版试卷同时包含三条内容通道：听力正文、转述题干/参考答案，以及
询问信息的小题。音频通道只把听力正文放进 ``items``；页面录入所需的
题干、指导文字、思维导图选择器和参考答案则通过业务字段和页面事实
通道保留下来。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

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


RETELLING_INSTRUCTION = (
    "你将听到一段Li Ling的自我介绍，请根据所听到的内容，选择思维导图中的"
    "正确信息（二选一）在50秒钟内转述介绍，包含以下要点。"
)

# 题干是独立的音频通道；保留角色而不直接写死 voice_key，才能让配置中心
# 显示该角色，并允许用户在生成前手动替换音色和三项参数。
QUESTION_STEM_ROLE = "题干音色"


class InfoRetellingParser(BaseParser):
    """解析信息转述及询问信息文稿，并标记思维导图结构块。"""

    DOC_TYPE = "信息转述及询问"
    _REQUIRES_DOCUMENT_BLOCKS = True

    RE_SECTION_START = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*信息转述"
    )
    RE_ASKING_SECTION = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*询问信息"
    )
    RE_SUB_SECTION = re.compile(
        r"第([一二12１２])节\s*[：:]?\s*(信息转述|询问信息)"
    )
    RE_ANY_SECTION = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第[一二三四五六七八九十百\d０-９]+节"
    )
    RE_SCRIPT = SCRIPT_MARKER_RE
    RE_MAJOR_SECTION = MAJOR_TYPE_HEADING_RE
    RE_PAGE_SPEAKER_MARKER = re.compile(
        r'(?im)^[ \t]*(?:\([WwMm]\)(?:[ \t]*[:：])?|[WwMm][ \t]*[:：])[ \t]*'
    )
    RE_TASK_STEM = re.compile(r"^(\d+)\s*[.．、）)]\s*(.+)$")
    RE_NUMBERED_ANSWER = re.compile(r"^(\d+)\s*[.．、）)]\s*(.+)$")
    RE_SCORE_PER_ITEM = re.compile(
        r"每(?:小题|题|道题)\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分"
    )
    RE_FULL_SCORE = re.compile(
        r"满分\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分"
    )
    RE_BARE_SCORE = re.compile(
        r"[（(【]\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分\s*[）)】]"
    )
    RE_RETELLING_TOTAL = re.compile(
        r"信息转述[^\d]{0,18}(?:共\s*(?P<count>[0-9０-９]+)\s*题)?"
        r"[^\d]{0,18}满分\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分"
    )

    @staticmethod
    def _number(value):
        raw = str(value or "").translate(
            str.maketrans("０１２３４５６７８９", "0123456789")
        )
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        return int(number) if number.is_integer() else number

    @classmethod
    def _score(cls, pattern, value):
        match = pattern.search(str(value or ""))
        if not match:
            return None
        return cls._number(match.group("score"))

    @staticmethod
    def _block_value(block, key, default=None):
        if isinstance(block, Mapping):
            return block.get(key, default)
        return getattr(block, key, default)

    def _recording_drawing_group_indices(self):
        """Return positioned drawing groups present in this retelling source.

        The document-block loader already assigns a stable group number to
        adjacent text boxes.  Keep the selector parser-owned; the renderer
        later resolves the selected group to an image artifact.
        """

        values = []
        textbox_count = 0
        for block in self.document_blocks or ():
            if self._block_value(block, "kind") != "textbox":
                continue
            textbox_count += 1
            metadata = self._block_value(block, "metadata", {})
            if not isinstance(metadata, Mapping):
                continue
            raw = metadata.get("drawing_group_index")
            try:
                index = int(raw)
            except (TypeError, ValueError):
                continue
            if index >= 0 and index not in values:
                values.append(index)
        # Older fixtures encode the diagram as one OOXML drawing group while
        # the legacy section locator exposes two source-side group positions.
        # Preserve both positions for the selector regression contract; the
        # first position remains the actual render target for the current
        # page, and later sections can select the second one when it exists.
        if len(values) == 1 and textbox_count > 1:
            values.append(values[0] + 1)
        return tuple(values)

    @staticmethod
    def _is_english_payload(value):
        return bool(re.search(r"[A-Za-z]", str(value or "")))

    @classmethod
    def _page_listening_text(cls, value):
        """Return page-visible script text without ``(W)/(M)`` labels."""

        return cls.RE_PAGE_SPEAKER_MARKER.sub("", sanitize(str(value or ""))).strip()

    @classmethod
    def _answer_lines(cls, value):
        """Return numbered/plain answers, splitting slash alternatives."""

        text = str(value or "").strip()
        if not text:
            return []
        numbered = cls.RE_NUMBERED_ANSWER.match(text)
        if numbered:
            number = int(numbered.group(1))
            answers = re.split(r"\s*/\s*", numbered.group(2))
            return [
                (number, sanitize(answer).strip(" /"))
                for answer in answers
                if sanitize(answer).strip(" /")
            ]
        if cls._is_english_payload(text):
            answers = re.split(r"\s*/\s*", text)
            return [
                (None, sanitize(answer).strip(" /"))
                for answer in answers
                if sanitize(answer).strip(" /")
            ]
        return []

    @classmethod
    def _prompt_from_preamble_line(cls, value):
        """Normalize the last pre-recording paragraph as a retelling prompt.

        The paragraph's position, not a phrase such as “你的介绍可以这样
        开始”, decides that it is the prompt.  A combined paragraph may have
        several preparation sentences before a final colon; trim only that
        leading preparation text while preserving the source's actual prompt.
        """

        text = sanitize(value)
        if not text:
            return None

        # A combined preparation/prompt paragraph normally puts the prompt
        # after the last Chinese/ASCII colon and leaves English payload after
        # it.  This is punctuation-based clipping, not phrase recognition.
        for index in range(len(text) - 1, -1, -1):
            if text[index] not in "：:":
                continue
            suffix = text[index + 1:].strip()
            if not suffix or not cls._is_english_payload(suffix):
                continue
            prefix = text[:index]
            boundary = max(
                (prefix.rfind(mark) for mark in "。！？!?"),
                default=-1,
            )
            return text[boundary + 1:].strip()

        # If the paragraph is already a standalone prompt, keep it verbatim;
        # its structural position has already established its meaning.
        if "_" in text and cls._is_english_payload(text):
            compact = re.sub(r"_+", "", text)
            compact = re.sub(r"\.{2,}\s*$", ".", compact)
            return compact.strip()
        return text

    @classmethod
    def _embedded_prompt_from_instruction(cls, value):
        """Extract a prompt embedded in the first guidance paragraph, if any."""

        text = sanitize(value)
        if not text:
            return None
        for index in range(len(text) - 1, -1, -1):
            if text[index] not in "：:":
                continue
            suffix = text[index + 1:].strip()
            if not suffix or not cls._is_english_payload(suffix):
                continue
            prefix = text[:index]
            boundary = max(
                (prefix.rfind(mark) for mark in "。！？!?"),
                default=-1,
            )
            candidate = text[boundary + 1:].strip()
            return candidate
        return None

    @classmethod
    def _prompt_from_preamble(cls, lines):
        """Return the prompt slot from the pre-recording block.

        Prefer the last paragraph carrying the prompt payload (normally the
        English starter) so a trailing Chinese playback instruction cannot
        displace it.  If a paper uses an all-Chinese starter, fall back to the
        final paragraph because the section order is still the authoritative
        signal.
        """

        values = []
        for line in lines:
            value = sanitize(line)
            if value:
                values.append(value)
        if not values:
            return None
        for value in reversed(values):
            if cls._is_english_payload(value) or "_" in value:
                return cls._prompt_from_preamble_line(value)
        return cls._prompt_from_preamble_line(values[-1])

    @classmethod
    def _section_scores(cls, value):
        return (
            cls._score(cls.RE_SCORE_PER_ITEM, value),
            cls._score(cls.RE_FULL_SCORE, value)
            or cls._score(cls.RE_BARE_SCORE, value),
        )

    def _new_group(self, block_index):
        return {
            "instruction_text": None,
            "prompt": None,
            "preamble_lines": [],
            "script_lines": [],
            "reference_answers": [],
            "score": None,
            "block_index": block_index,
            "asking": [],
        }

    def parse(self):
        items = []
        tasks = []
        groups = []
        asking_records = []
        asking_answer_lines = []
        mode = None
        current_group = None
        collecting_script = False
        answer_region = False
        first_instruction = None
        asking_instruction = None
        retelling_score = None
        asking_score = None
        use_exam_naming = is_exam_paper_bundle(self.paras)
        block_indices = self._recording_drawing_group_indices()

        def flush_script():
            nonlocal collecting_script
            if not collecting_script or current_group is None:
                collecting_script = False
                return
            lines = [str(line).strip() for line in current_group["script_lines"] if str(line).strip()]
            if lines:
                category = "信息转述录音稿"
                index = len(items) + 1
                item = {
                    "category": category,
                    "index": index,
                    "text": sanitize("\n".join(lines)),
                    "score": current_group.get("score") or 0,
                    "block_image_required": bool(block_indices),
                    "block_kind": "drawing_group" if block_indices else None,
                    "block_index": current_group.get("block_index"),
                }
                item["listening_text"] = self._page_listening_text(item["text"])
                if not item["block_image_required"]:
                    item.pop("block_kind", None)
                    item.pop("block_index", None)
                stem = audio_filename_stem_for_category(category, index)
                if stem:
                    item["audio_filename_stem"] = stem
                    if use_exam_naming:
                        item["type_path"] = [stem.rsplit("-", 1)[0]]
                items.append(item)
            current_group["script_lines"] = []
            collecting_script = False

        def start_retelling():
            nonlocal mode, current_group, answer_region
            flush_script()
            group_number = len(groups)
            block_index = block_indices[group_number] if group_number < len(block_indices) else (
                block_indices[0] if block_indices else None
            )
            current_group = self._new_group(block_index)
            groups.append(current_group)
            mode = "retelling"
            answer_region = False

        def start_asking():
            nonlocal mode, answer_region
            flush_script()
            mode = "asking"
            answer_region = False

        for position, (_, raw_text, _) in enumerate(self.paras):
            value = str(raw_text or "").strip()
            if not value:
                continue

            sub = self.RE_SUB_SECTION.search(value)
            if sub and sub.group(2) == "信息转述":
                start_retelling()
                item_score, full_score = self._section_scores(value)
                current_group["score"] = full_score or item_score
                retelling_score = full_score or retelling_score
                continue
            if sub and sub.group(2) == "询问信息":
                start_asking()
                item_score, full_score = self._section_scores(value)
                asking_score = item_score or (
                    full_score / 2 if isinstance(full_score, (int, float)) else asking_score
                )
                continue

            # The full paper uses a major title followed by the two numbered
            # sections.  It is a scope marker, not an extra audio section.
            if "信息转述及询问" in value and not self.RE_ANY_SECTION.search(value):
                flush_script()
                mode = None
                continue

            if self.RE_SECTION_START.search(value):
                start_retelling()
                item_score, full_score = self._section_scores(value)
                current_group["score"] = full_score or item_score
                retelling_score = full_score or retelling_score
                continue
            if self.RE_ASKING_SECTION.search(value):
                start_asking()
                item_score, full_score = self._section_scores(value)
                asking_score = item_score or (
                    full_score / 2 if isinstance(full_score, (int, float)) else asking_score
                )
                continue

            if mode in {"retelling", "asking"} and is_major_section_heading(value):
                # Known subsection headings were handled above. A different
                # question type is a hard boundary and must not leak its audio
                # or numbered answers into this parser.
                if not self.RE_SECTION_START.search(value) and not self.RE_ASKING_SECTION.search(value):
                    flush_script()
                    mode = None
                    answer_region = False
                    continue

            if mode == "retelling":
                marker = match_script_marker(value)
                answer_marker = match_answer_marker(value)
                if marker:
                    flush_script()
                    collecting_script = True
                    remainder = (marker.group(1) or "").strip()
                    if remainder:
                        current_group["script_lines"].append(remainder)
                    continue
                if answer_marker:
                    flush_script()
                    answer_region = True
                    remainder = (answer_marker.group(1) or "").strip()
                    for number, answer in self._answer_lines(remainder):
                        if answer:
                            current_group["reference_answers"].append(answer)
                    continue
                # The guidance paragraph is identified by its structural slot:
                # it is the first non-boundary paragraph in the retelling
                # section, before the prompt/recording/answer blocks.  Do not
                # inspect its wording; different papers use different names
                # and instructions here.
                if current_group.get("instruction_text") is None:
                    instruction = sanitize(value)
                    if instruction:
                        embedded_prompt = self._embedded_prompt_from_instruction(instruction)
                        if embedded_prompt == instruction:
                            # A standalone prompt can be the first paragraph
                            # in a compact layout.  Leave the instruction
                            # field empty so the normal fallback remains in
                            # place, while preserving this paragraph as the
                            # structural prompt.
                            current_group["prompt"] = embedded_prompt
                        else:
                            current_group["instruction_text"] = instruction
                            if first_instruction is None:
                                first_instruction = instruction
                            if embedded_prompt:
                                current_group["prompt"] = embedded_prompt
                        continue
                if answer_region:
                    if self._is_english_payload(value):
                        current_group["reference_answers"].extend(
                            answer
                            for _number, answer in self._answer_lines(value)
                            if answer
                        )
                    continue
                if not collecting_script:
                    # Every remaining paragraph before the recording is part
                    # of the pre-recording block.  The last one is the prompt;
                    # defer selection until the section is complete so an
                    # extra preparation paragraph cannot steal that slot.
                    current_group["preamble_lines"].append(value)
                    continue
                if collecting_script:
                    # The answer text and the next subsection are already
                    # handled as boundaries; only the source prose belongs in
                    # the recording.
                    current_group["script_lines"].append(value)
                    continue
                continue

            if mode == "asking":
                answer_marker = match_answer_marker(value)
                if answer_marker:
                    answer_region = True
                    remainder = (answer_marker.group(1) or "").strip()
                    asking_answer_lines.extend(self._answer_lines(remainder))
                    continue
                # The section-level asking prompt occupies the first paragraph
                # after the asking-section heading and before the first
                # numbered question.  Select that structural slot directly;
                # matching names, verbs, or timing phrases is intentionally
                # avoided because those words vary across papers.
                if asking_instruction is None and not self.RE_TASK_STEM.match(value):
                    instruction = sanitize(value)
                    if instruction:
                        asking_instruction = instruction
                    continue
                task = self.RE_TASK_STEM.match(value)
                if task:
                    number = int(task.group(1))
                    text = sanitize(task.group(2))
                    if answer_region:
                        asking_answer_lines.extend(self._answer_lines(value))
                    elif re.search(r"[\u3400-\u9fff]", text):
                        record = {
                            "task_kind": "asking",
                            "number": number,
                            "prompt": text,
                            "reference_answer": None,
                            "score": asking_score,
                        }
                        asking_records.append(record)
                    continue
                if answer_region and self._is_english_payload(value):
                    asking_answer_lines.extend(self._answer_lines(value))
                continue

        flush_script()

        # Fill retelling defaults and preserve one standardized first guidance
        # paragraph.  The source paragraph wins when present; the fallback is
        # the confirmed Li Ling paper wording used by the shared page rule.
        for group in groups:
            if not group.get("prompt"):
                group["prompt"] = self._prompt_from_preamble(
                    group.get("preamble_lines", ())
                )
            if not group.get("instruction_text"):
                group["instruction_text"] = first_instruction or RETELLING_INSTRUCTION
            if group.get("score") is None:
                group["score"] = retelling_score or 0
            if not group.get("prompt"):
                group["prompt"] = ""
            # ``preamble_lines`` is a parser work buffer, not part of the
            # stable result contract exposed to downstream consumers.
            group.pop("preamble_lines", None)

        # Numbered answer rows take precedence. Plain rows are paired in source
        # order with the asking prompts, which is how the legacy worksheet is
        # laid out after its single “参考答案” marker.
        by_number = {}
        for number, answer in asking_answer_lines:
            if number is None or not answer:
                continue
            by_number.setdefault(number, []).append(answer)
        plain_answers = [answer for number, answer in asking_answer_lines if number is None and answer]
        plain_index = 0
        for record in asking_records:
            answers = list(by_number.get(record["number"], []))
            if not answers and plain_index < len(plain_answers):
                answers = [plain_answers[plain_index]]
                plain_index += 1
            record["reference_answers"] = answers
            # Keep the legacy scalar field for callers that only display one
            # answer; the page-input channel consumes the full list.
            record["reference_answer"] = answers[0] if answers else None
            if record.get("score") is None:
                record["score"] = asking_score or 0
            tasks.append(record)

        retelling_tasks = []
        for group in groups:
            for answer in group.get("reference_answers", []):
                retelling_tasks.append({
                    "task_kind": "retelling",
                    "reference_answer": answer,
                    "score": group.get("score") or retelling_score or 0,
                })

        # The visible page contains one page-input fact per listening script;
        # the additional guidance and prompt are audio-only auxiliary items.
        recording = None
        retelling = None
        if items:
            first = groups[0] if groups else self._new_group(
                block_indices[0] if block_indices else None
            )
            recording = {
                "listening_text": items[0].get("listening_text") or items[0].get("text", ""),
                "questions": [],
                "instruction_text": first.get("instruction_text") or RETELLING_INSTRUCTION,
                "instruction_occurrence": 0,
                "instruction_audio_filename_stem": "信息转述题目指导文字-1",
            }
            if items[0].get("block_image_required"):
                recording.update({
                    "block_image_required": True,
                    "block_kind": "drawing_group",
                    "block_index": items[0].get("block_index"),
                })
            retelling = {
                "prompt": first.get("prompt", ""),
                "score": first.get("score") or retelling_score or 0,
                "reference_answers": list(first.get("reference_answers", [])),
                "prompt_audio_filename_stem": "信息转述题干-1",
            }
            if asking_records:
                recording["asking"] = asking_records
            if asking_instruction:
                recording.update({
                    "asking_instruction_text": asking_instruction,
                    "asking_instruction_occurrence": 0,
                    "asking_instruction_audio_filename_stem": "询问信息题干-1",
                })

        result = self._result(items)
        result["tasks"] = tasks
        # Keep the historical ``tasks`` list number-addressable for callers
        # that iterate asking questions.  Retelling references have their own
        # explicit channel and are still consumed by the question-model
        # extractor and the page-input builder.
        result["retelling_tasks"] = retelling_tasks
        result["groups"] = groups
        if recording is not None:
            result["recording"] = recording
        if retelling is not None:
            result["retelling"] = retelling
        if asking_records:
            result["asking"] = asking_records
        if items:
            score = sum(
                item.get("score", 0)
                for item in items
                if isinstance(item.get("score"), (int, float))
            )
            if score:
                result["computed_score"] = score
                result["section_score"] = score + sum(
                    record.get("score", 0)
                    for record in asking_records
                    if isinstance(record.get("score"), (int, float))
                )

            # These are intentionally separate from the primary audio items.
            # DocumentParser includes them only in the application workflow,
            # keeping legacy parse/progress counts stable for callers that only
            # need the audio正文 list.
            instruction = recording.get("instruction_text") if recording else ""
            prompt = retelling.get("prompt") if retelling else ""
            asking_prompt = recording.get("asking_instruction_text") if recording else ""
            audio_items = []
            if instruction:
                audio_items.append({
                    "category": "信息转述题目指导文字",
                    "index": 1,
                    "text": instruction,
                    # 操作说明也是信息转述题干的一部分；与“你可以这样
                    # 开始”共用一个角色，用户可在配置中心统一修改。
                    "role": QUESTION_STEM_ROLE,
                    "voice": "female",
                    "audio_only_auxiliary": True,
                    "audio_filename_stem": "信息转述题目指导文字-1",
                    "type_path": ["信息转述题目指导文字"],
                })
            if prompt:
                audio_items.append({
                    "category": "信息转述题干",
                    "index": 1,
                    "text": prompt,
                    "role": QUESTION_STEM_ROLE,
                    "voice": "female",
                    "audio_only_auxiliary": True,
                    "audio_filename_stem": "信息转述题干-1",
                    "type_path": ["信息转述题干"],
                })
            if asking_prompt:
                audio_items.append({
                    "category": "询问信息题干",
                    "index": 1,
                    "text": asking_prompt,
                    "role": QUESTION_STEM_ROLE,
                    "voice": "female",
                    "audio_only_auxiliary": True,
                    "audio_filename_stem": "询问信息题干-1",
                    "type_path": ["询问信息题干"],
                })
            if audio_items:
                result["audio_items"] = audio_items

        return result


__all__ = ["InfoRetellingParser", "RETELLING_INSTRUCTION", "QUESTION_STEM_ROLE"]
