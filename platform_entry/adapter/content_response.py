"""Visible-page handler for the listening-response question family."""

from __future__ import annotations

from .page_shared import *  # noqa: F403,F401


class PlatformInputResponseContentMixin:
    """Fill each response recording card and its spoken reference answer."""

    def _fill_response_groups(
        self,
        groups_by_type: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, int]:
        groups = groups_by_type["听后应答"]
        response_count = sum(
            len(group.get("questions", ()))
            for group in groups
        )
        cards, editors = self._activate_group_section(
            "听后应答",
            "录音题",
            response_count,
            response_count,
        )

        recording_cursor = 0
        original_audio_cursor = 0
        listening_cursor = 0
        number_cursor = 0
        response_cursor = 0
        for group in groups:
            for question in group["questions"]:
                response_cursor += 1
                description = f"听后应答第{response_cursor}题"
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
                # The response prompt is a recording card, but its source
                # audio belongs to the material block beside “听力原文”.
                # Do not put it in the card-level “题干音频” field: that
                # field must remain empty for this template.
                self._upload_original_audio(
                    question["audio_path"],
                    original_audio_cursor,
                    description,
                )
                original_audio_cursor += 1
                self._fill_material_numbers(question, number_cursor, description)
                number_cursor += 1
                if recording_cursor >= len(cards):
                    raise PlatformInputUiError("听后应答小题数量超过页面录音题卡片数量")
                self._fill_recording_card(
                    cards[recording_cursor],
                    question,
                    description=description,
                    prompt=question["prompt"],
                )
                recording_cursor += 1

        if recording_cursor != response_count or original_audio_cursor != response_count:
            raise PlatformInputUiError("听后应答输入没有被完整消费")
        return {"录音题": recording_cursor}


__all__ = ["PlatformInputResponseContentMixin"]
