"""「信息转述及询问」题型切片。

这类旧版试卷同时包含三条内容通道：听力正文、转述题干/参考答案，以及
询问信息的小题。音频通道只把听力正文放进 ``items``；页面录入所需的
题干、指导文字、思维导图选择器和参考答案则通过业务字段和页面事实
通道保留下来。
"""

from __future__ import annotations

import re
from collections import Counter

from audio_naming import audio_filename_stem_for_category
from question_types.base import BaseParser
from question_types.text_utils import (
    answer_rows,
    is_major_section_heading,
    match_answer_marker,
    match_script_marker,
    sanitize,
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
        r"第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*(?:信息转述|短文复述)"
    )
    RE_ASKING_SECTION = re.compile(
        r"^\s*(?:[一二三四五六七八九十百]+\s*[、.．)]\s*)?"
        r"第[一二三四五六七八九十百\d０-９]+节\s*[：:]?\s*(?:询问信息|提问)"
    )
    RE_PART = re.compile(
        r"^\s*(?:[一二三四五六七八九十百\d]+\s*[、.．)]\s*)?"
        r"(?:信息转述及询问|短文复述及提问)(?=题|\s|[（(：:]|$)"
    )
    RE_PAGE_PAREN_SPEAKER_MARKER = re.compile(
        r'(?im)^[ \t]*\([WwMm]\)(?:[ \t]*[:：])?[ \t]*'
    )
    RE_TASK_STEM = re.compile(r"^(\d+)\s*[.．、）)]\s*(.+)$")
    # A standalone preparation row belongs to the guidance slot that is spoken
    # after the listening material, not to the pre-listening guidance.
    RE_PREPARATION_PROMPT = re.compile(
        r"^下面你有\s*[0-9０-９]+\s*秒钟的时间准备\s*[。.]?$"
    )
    RE_SCORE_PER_ITEM = re.compile(
        r"每(?:小题|题|道题)\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分"
    )
    RE_FULL_SCORE = re.compile(
        r"满分\s*(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分"
    )
    RE_BARE_SCORE = re.compile(
        r"[（(【]\s*(?:共\s*)?(?P<score>[0-9０-９]+(?:[.]\d+)?)\s*分\s*[）)】]"
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

    def _recording_image(self, heading, first_script):
        """Resolve the diagram anchored inside this recording's preamble."""

        start = heading[0]
        stop = first_script
        anchors = [
            index for index, text, _ in self.paras
            if start <= index <= stop and "思维导图" in text
        ]
        blocks = [
            block for block in self.document_blocks
            if start <= block.metadata.get("paragraph_index", -1) <= stop
        ]
        embedded = {}
        drawings = set()
        tables = set()
        for block in blocks:
            position = block.metadata["paragraph_index"]
            if block.kind == "embedded_image":
                index = block.metadata["embedded_image_index"]
                distances = [
                    (position < anchor, abs(position - anchor))
                    for anchor in anchors if -2 <= position - anchor <= 8
                ]
                if distances:
                    embedded[index] = min(distances)
            if block.kind == "textbox":
                drawings.add(block.metadata["drawing_group_index"])
            if block.kind == "table" and any(0 <= position - anchor <= 8 for anchor in anchors):
                tables.add(block.metadata["table_index"])
        if embedded:
            nearest = min(embedded.values())
            matches = [index for index, distance in embedded.items() if distance == nearest]
            if len(matches) == 1:
                return ("embedded_image", matches[0]), None
        else:
            candidates = [("drawing_group", index) for index in drawings]
            candidates.extend(("table", index) for index in tables)
            if len(candidates) == 1:
                return candidates[0], None
            if not candidates:
                return (None, f"第 {anchors[0] + 1} 段的思维导图未找到") if anchors else (None, None)
        return None, f"第 {start + 1} 段开始的转述材料有多张候选配图"

    @staticmethod
    def _is_english_payload(value):
        return bool(re.search(r"[A-Za-z]", str(value or "")))

    @classmethod
    def _page_listening_text(cls, value):
        """Hide ``(W)/(M)`` but preserve ``W:``/``M:`` for system input."""

        return cls.RE_PAGE_PAREN_SPEAKER_MARKER.sub(
            "", sanitize(str(value or ""))
        ).strip()

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
    def _section_scores(cls, value):
        return (
            cls._score(cls.RE_SCORE_PER_ITEM, value),
            cls._score(cls.RE_FULL_SCORE, value)
            or cls._score(cls.RE_BARE_SCORE, value),
        )

    def _sections(self):
        """Yield the retelling and asking ranges within the owned source section."""

        current = None
        for row in self.paras:
            _, text, _ = row
            if self.RE_SECTION_START.match(text):
                kind = "retelling"
            elif self.RE_ASKING_SECTION.match(text):
                kind = "asking"
            else:
                kind = None
            if kind or self.RE_PART.match(text) or is_major_section_heading(text):
                if current is not None:
                    yield current
                current = (kind, row, []) if kind else None
                if kind is None:
                    yield None, row, []
            elif current is not None:
                current[2].append(row)
        if current is not None:
            yield current

    def _read_material(self, heading, rows):
        preamble = []
        script = []
        references = []
        script_start = None
        mode = preamble
        for row in rows:
            index, value, _ = row
            if marker := match_script_marker(value):
                mode = script
                if script_start is None:
                    script_start = index
            elif marker := match_answer_marker(value):
                mode = references
            if marker:
                value = marker.group(1).strip()
            if value:
                mode.append(value)
        if not script:
            return None

        instruction = None
        prompt = None
        reading = []
        preparation = []
        if preamble:
            first, *tail = preamble
            embedded_prompt = self._embedded_prompt_from_instruction(first)
            if embedded_prompt == sanitize(first):
                prompt = embedded_prompt
            else:
                instruction = sanitize(first)
                prompt = embedded_prompt
            if tail and not prompt:
                prompt_index = next(
                    (i for i in range(len(tail) - 1, -1, -1)
                     if self._is_english_payload(tail[i]) or "_" in tail[i]),
                    len(tail) - 1,
                )
                prompt = self._prompt_from_preamble_line(tail[prompt_index])
                for line in tail[:prompt_index]:
                    if self.RE_PREPARATION_PROMPT.match(line):
                        preparation.append(line)
                    else:
                        reading.append(line)

        item_score, full_score = self._section_scores(heading[1])
        selector, issue = self._recording_image(heading, script_start)
        return {
            "source_locator": f"第 {heading[0] + 1} 段/信息转述",
            "script": sanitize("\n".join(script)),
            "score": full_score if full_score is not None else item_score,
            "instruction": instruction,
            "reading": sanitize("\n".join(reading)),
            "preparation": sanitize("\n".join(preparation)),
            "prompt": prompt or "",
            "references": [answer for _, answers in answer_rows(references) for answer in answers],
            "image": selector,
            "issues": [issue] if issue else [],
            "asking": [],
        }

    def _read_asking(self, heading, rows):
        records = []
        answers = []
        instruction = None
        in_answers = False
        for index, value, _ in rows:
            if marker := match_answer_marker(value):
                in_answers = True
                answers.append(marker.group(1))
                continue
            task = self.RE_TASK_STEM.match(value)
            if task and re.search(r"[\u3400-\u9fff]", task.group(2)) and (
                not in_answers or re.match(r"^[^A-Za-z]*[\u3400-\u9fff]", task.group(2))
            ):
                in_answers = False
                number = int(task.group(1))
                records.append({
                    "task_kind": "asking",
                    "number": number,
                    "prompt": sanitize(task.group(2)),
                    "source_locator": f"第 {index + 1} 段/询问信息/第 {number} 题",
                })
            elif in_answers:
                answers.append(value)
            elif not instruction and not records:
                instruction = sanitize(value)

        item_score, full_score = self._section_scores(heading[1])
        if item_score is None and full_score is not None and records:
            item_score = full_score / len(records)

        question_counts = Counter(record["number"] for record in records)
        rows_by_number = {}
        unnumbered = []
        for number, alternatives in answer_rows(answers):
            if number is None:
                unnumbered.append(alternatives)
            else:
                rows_by_number.setdefault(number, []).extend(alternatives)

        # A completely unnumbered key with exactly one row per question is
        # an established format. Mixed or incomplete keys need explicit review.
        positional = not rows_by_number and len(unnumbered) == len(records)
        for index, record in enumerate(records):
            number = record["number"]
            references = []
            if question_counts[number] == 1:
                if positional:
                    references = unnumbered[index]
                else:
                    references = rows_by_number.get(number, [])
            record.update(
                score=item_score,
                reference_answers=references,
                reference_answer=references[0] if references else None,
            )

        issues = [
            f'{record["source_locator"]}的参考答案归属未确认'
            for record in records if not record["reference_answers"]
        ]
        if (rows_by_number.keys() - question_counts.keys()) or (unnumbered and not positional):
            issues.append(f"第 {heading[0] + 1} 段开始的询问部分有未匹配的答案")
        return instruction, records, issues

    @staticmethod
    def _material_items(material, index, *, exam_naming):
        locator = material["source_locator"]
        script = material["script"]
        score = material["score"]
        category = "信息转述录音稿"
        stem = audio_filename_stem_for_category(category, index)
        recording = {
            "listening_text": InfoRetellingParser._page_listening_text(script),
            "audio_filename_stem": stem,
            "questions": [],
        }
        item = {
            "category": category,
            "index": index,
            "text": script,
            "listening_text": recording["listening_text"],
            "score": score if score is not None else 0,
            "source_locator": locator,
            "audio_filename_stem": stem,
        }
        if exam_naming:
            item["type_path"] = [stem.rsplit("-", 1)[0]]
        if material["image"]:
            kind, image_index = material["image"]
            recording.update(block_image_required=True, block_kind=kind, block_index=image_index)
            item.update(block_image_required=True, block_kind=kind, block_index=image_index)

        retelling = {
            "prompt": material["prompt"],
            "score": score,
            "reference_answers": material["references"],
        }
        audio = []
        for key, text, target, field, occurrence in (
            ("信息转述题目指导文字", material["instruction"], recording, "instruction", 0),
            ("信息转述阅图指导文字", material["reading"], recording, "reading_instruction", 1),
            ("信息转述准备指导文字", material["preparation"], recording, "preparation_instruction", 2),
            ("信息转述题干", material["prompt"], retelling, "prompt", None),
        ):
            if not text:
                continue
            audio_stem = f"{key}-{index}"
            target[f"{field}_audio_filename_stem"] = audio_stem
            if occurrence is not None:
                target[f"{field}_text"] = text
                target[f"{field}_occurrence"] = occurrence
            audio.append({
                "category": key,
                "index": index,
                "text": text,
                "role": QUESTION_STEM_ROLE,
                "voice": "female",
                "audio_only_auxiliary": True,
                "audio_filename_stem": audio_stem,
                "type_path": [key],
                "source_locator": f"{locator}/{key}",
            })

        if material.get("asking_instruction"):
            recording.update(
                asking_instruction_text=material["asking_instruction"],
                asking_instruction_occurrence=0,
            )
        if material["asking"]:
            recording["asking"] = material["asking"]
        item["page_input"] = {
            "type": InfoRetellingParser.DOC_TYPE,
            "recording": recording,
            "retelling": retelling,
            "asking": material["asking"],
        }
        if material["issues"]:
            item["page_input"]["source_issue"] = "；".join(material["issues"])
        return item, audio

    def parse(self):
        materials = []
        asking_records = []
        orphan_issues = []
        current = None
        for kind, heading, rows in self._sections():
            if kind is None:
                current = None
                continue
            if kind == "retelling":
                current = self._read_material(heading, rows)
                if current is not None:
                    materials.append(current)
            else:
                instruction, records, issues = self._read_asking(heading, rows)
                asking_records.extend(records)
                if current is not None:
                    if current["asking"]:
                        issues.append(f"第 {heading[0] + 1} 段开始的询问部分找不到唯一的转述材料")
                    else:
                        current["asking"] = records
                        current["asking_instruction"] = instruction
                    current["issues"].extend(issues)
                elif records:
                    orphan_issues.append(f"第 {heading[0] + 1} 段开始的询问部分找不到对应的转述材料")
                current = None

        items = []
        audio_items = []
        for index, material in enumerate(materials, start=1):
            item, audio = self._material_items(material, index, exam_naming=self.use_exam_naming)
            if len(materials) > 1 and not self.use_exam_naming:
                unit = f"第 {index} 组信息转述"
                for entry in [item, *audio]:
                    entry.update(unit_id=material["source_locator"], unit_label=unit)
            items.append(item)
            audio_items.extend(audio)

        result = self._result(items)
        result["tasks"] = asking_records
        if orphan_issues:
            result["source_issue"] = "；".join(orphan_issues)
        if not items:
            return result
        first = items[0]["page_input"]
        result["recording"] = first["recording"]
        result["retelling"] = first["retelling"]
        if first["asking"]:
            result["asking"] = first["asking"]
        if audio_items:
            result["audio_items"] = audio_items
        score = sum(item["score"] for item in items)
        if score:
            result["computed_score"] = score
            result["section_score"] = score + sum(record["score"] or 0 for record in asking_records)
        return result


__all__ = ["InfoRetellingParser", "QUESTION_STEM_ROLE"]
