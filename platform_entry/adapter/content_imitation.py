"""Visible-page handler for the imitation-reading question family."""

from __future__ import annotations

from .page_shared import *  # noqa: F403,F401


class PlatformInputImitationContentMixin:
    """Fill the source script, source audio, score, and reference answers."""

    def _fill_imitation_groups(
        self,
        groups_by_type: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, int]:
        groups = groups_by_type["模仿朗读"]
        imitation_count = sum(
            len(group.get("questions", ()))
            for group in groups
        )
        cards, editors = self._activate_group_section(
            "模仿朗读",
            "录音题",
            imitation_count,
            imitation_count,
        )

        recording_cursor = 0
        original_audio_cursor = 0
        listening_cursor = 0
        for group in groups:
            for question_index, question in enumerate(group["questions"]):
                description = f"模仿朗读第{question_index + 1}题"
                self._replace_rich_text(
                    self._next_listening_editor(
                        editors,
                        listening_cursor,
                        description,
                    ),
                    question["listening_text"],
                    field=f"{description}听力原文",
                )
                listening_cursor += 1
                # The source recording belongs to the material block beside
                # “听力原文”.  The recording card's “题干音频” field is not
                # used by the imitation-reading template and must stay empty.
                self._upload_original_audio(
                    question["audio_path"],
                    original_audio_cursor,
                    description,
                )
                original_audio_cursor += 1
                if recording_cursor >= len(cards):
                    raise PlatformInputUiError("模仿朗读小题数量超过页面卡片数量")
                self._fill_recording_card(
                    cards[recording_cursor],
                    question,
                    description=description,
                    fill_answer_time=False,
                )
                recording_cursor += 1

        if recording_cursor != imitation_count or original_audio_cursor != imitation_count:
            raise PlatformInputUiError("模仿朗读输入没有被完整消费")
        return {"录音题": recording_cursor}

    def _fill_imitation_content(self) -> None:
        """Fill the legacy flat imitation-reading input shape."""

        editors = self._labeled_text_editors("听力原文")
        if len(editors) < len(self.spec.items):
            raise PlatformInputUiError(
                f"页面没有足够的“听力原文”编辑器，期望 {len(self.spec.items)} 个"
            )

        for item_index, item in enumerate(self.spec.items):
            listening_text = item.get("listening_text")
            if not listening_text:
                raise PlatformInputUiError(
                    f"第 {item_index + 1} 题缺少 listening_text（听力原文）"
                )
            self._replace_rich_text(
                editors[item_index],
                listening_text,
                field="听力原文",
            )

            audio_path = item.get("original_audio_path")
            if not audio_path:
                raise PlatformInputUiError(
                    f"第 {item_index + 1} 题缺少 original_audio_path（原文音频）"
                )
            self._upload_audio(audio_path, item_index, label="原文音频")
            self._wait_audio_rendered(
                item_index,
                label="原文音频",
                expected_path=audio_path,
            )

        missing_scores = [
            index + 1
            for index, item in enumerate(self.spec.items)
            if item.get("score") is None
        ]
        if missing_scores:
            raise PlatformInputUiError(
                "模仿朗读每道题都必须提供 score（分数），缺少第 "
                + ", ".join(str(index) for index in missing_scores)
                + " 题"
            )
        # Legacy专项 pages expose score fields outside the lazily loaded
        # recording cards.  Validation alone used to let a blank score be
        # saved; fill and read back every score explicitly.
        for item_index, item in enumerate(self.spec.items):
            self._fill_labeled_input_at(
                ("分数",),
                item_index,
                item["score"],
                f"模仿朗读第{item_index + 1}题分数",
            )
        self._fill_item_numbers()

        # Flat ``items`` remains compatible for old专项输入.  The imitation
        # special-paper template does not need reference-answer rows.  On a
        # retry, still open the cards once so rows left by an earlier run are
        # removed; a brand-new paper can return after the score is filled.
        has_reference_answers = any(
            item.get("reference_answers") is not None
            for item in self.spec.items
        )
        if not has_reference_answers and not getattr(self, "existing_paper_id", None):
            return
        self._activate_outline_section("模仿朗读")
        self._wait_until(
            lambda: len(self._question_cards("录音题")) == len(self.spec.items),
            "切换到“模仿朗读”后录音题卡没有完整挂载",
            timeout_seconds=60,
            interval_ms=200,
        )
        cards = self._question_cards("录音题")
        if len(cards) != len(self.spec.items):
            raise PlatformInputUiError(
                f"模仿朗读当前有 {len(cards)} 个录音题，输入要求 {len(self.spec.items)} 个"
            )
        for index, item in enumerate(self.spec.items):
            self._fill_recording_card(
                cards[index],
                item,
                description=f"模仿朗读第{index + 1}题",
                fill_answer_time=False,
            )


__all__ = ["PlatformInputImitationContentMixin"]
