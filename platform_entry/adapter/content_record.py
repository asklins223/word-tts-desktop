"""Visible-page handler for listening-record and retelling."""

from __future__ import annotations

from .page_shared import *  # noqa: F403,F401


class PlatformInputRecordContentMixin:
    """Fill the table/blank section and its separate retelling section."""

    def _fill_record_retelling_groups(
        self,
        groups_by_type: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, int]:
        groups = groups_by_type["听后记录并转述信息"]
        record_fill_count = sum(
            len((group.get("recording") or {}).get("questions", ()))
            for group in groups
        )
        retelling_count = sum(bool(group.get("retelling")) for group in groups)
        cards, editors = self._activate_group_section(
            "听后记录并转述信息",
            "填空题",
            record_fill_count,
            len(groups),
        )

        listening_cursor = 0
        audio_cursor = 0
        number_cursor = 0
        image_cursor = 0
        fill_cursor = 0
        retelling_questions: list[Mapping[str, Any]] = []
        retelling_sources: list[Mapping[str, Any]] = []
        for group in groups:
            recording = group["recording"]
            description = "听后记录"
            self._replace_rich_text(
                self._next_listening_editor(editors, listening_cursor, description),
                recording["listening_text"],
                field="听后记录听力原文",
            )
            listening_cursor += 1
            self._upload_original_audio(
                recording["audio_path"],
                audio_cursor,
                description,
            )
            audio_cursor += 1
            self._fill_material_numbers(recording, number_cursor, description)
            number_cursor += 1
            if recording.get("image_path") is not None:
                self._upload_image(recording["image_path"], image_cursor)
                image_cursor += 1
            if recording.get("instruction_text") is not None:
                self._fill_instruction_editor(
                    recording["instruction_text"],
                    recording.get("instruction_occurrence"),
                    "听后记录题目指导文字",
                )
            for question_index, question in enumerate(recording["questions"]):
                description = f"听后记录第{question_index + 1}题"
                if fill_cursor >= len(cards):
                    raise PlatformInputUiError("听后记录小题数量超过页面卡片数量")
                self._fill_recording_card(
                    cards[fill_cursor],
                    question,
                    description=description,
                    answers_key="answers",
                )
                fill_cursor += 1
            if group.get("retelling") is not None:
                retelling_questions.append(group["retelling"])
                # The retelling page has its own material-level audio/text
                # fields.  The reviewed template now requires those fields
                # to reuse the first-section listening-record source, not the
                # separate text/audio that may have been parsed for the
                # retelling prompt.
                retelling_sources.append(recording)
        if fill_cursor != record_fill_count:
            raise PlatformInputUiError("听后记录输入没有被完整消费")

        if retelling_count:
            # 第二节有独立的懒加载入口；点击最后一个“第 N 题(录音题)”
            # 后，才会把信息转述卡片挂到页面。
            self._activate_retelling_outline()
            self._wait_until(
                lambda: len(self._question_cards("录音题")) == retelling_count,
                "切换到“听后记录并转述信息”后信息转述题卡没有完整挂载",
                timeout_seconds=60,
                interval_ms=50,
            )
            self._wait_until(
                lambda: len(self._labeled_text_editors("听力原文"))
                >= retelling_count,
                "信息转述没有完整挂载“听力原文”编辑器",
                timeout_seconds=60,
                interval_ms=50,
            )
            retelling_cards = self._question_cards("录音题")
            retelling_listening_editors = self._labeled_text_editors("听力原文")
            if len(retelling_cards) != retelling_count:
                raise PlatformInputUiError(
                    f"信息转述当前有 {len(retelling_cards)} 个录音题，"
                    f"输入/模板要求 {retelling_count} 个"
                )
            for index, retelling in enumerate(retelling_questions):
                if index >= len(retelling_cards):
                    raise PlatformInputUiError("信息转述小题数量超过页面卡片数量")
                source = retelling_sources[index]
                self._replace_rich_text(
                    retelling_listening_editors[index],
                    source["listening_text"],
                    field="信息转述听力原文",
                )
                self._upload_original_audio(
                    source["audio_path"],
                    index,
                    "信息转述",
                )
                self._fill_recording_card(
                    retelling_cards[index],
                    retelling,
                    description="信息转述",
                    prompt=retelling["prompt"],
                )

        if len(retelling_questions) != retelling_count:
            raise PlatformInputUiError("信息转述输入没有被完整消费")
        return {
            "填空题": fill_cursor,
            "录音题": len(retelling_questions),
        }


__all__ = ["PlatformInputRecordContentMixin"]
