"""Visible-page handler for the listening-selection question family."""

from __future__ import annotations

from .page_shared import *  # noqa: F403,F401


class PlatformInputSelectionContentMixin:
    """Fill selection cards while preserving one audio per material."""

    def _fill_selection_groups(
        self,
        groups_by_type: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, int]:
        groups = groups_by_type["听后选择"]
        selection_count = sum(
            len(material["questions"])
            for group in groups
            for material in group.get("materials", ())
        )
        cards, editors = self._activate_group_section(
            "听后选择",
            "选择题",
            selection_count,
            sum(len(group.get("materials", ())) for group in groups),
        )

        choice_cursor = 0
        listening_cursor = 0
        audio_cursor = 0
        number_cursor = 0
        for group in groups:
            for material_index, material in enumerate(group["materials"]):
                description = f"听后选择第{material_index + 1}段录音"
                self._replace_rich_text(
                    self._next_listening_editor(
                        editors,
                        listening_cursor,
                        description,
                    ),
                    material["listening_text"],
                    field=f"{description}听力原文",
                )
                listening_cursor += 1
                self._upload_original_audio(
                    material["audio_path"],
                    audio_cursor,
                    description,
                )
                audio_cursor += 1
                self._fill_material_numbers(material, number_cursor, description)
                number_cursor += 1
                for question in material["questions"]:
                    if choice_cursor >= len(cards):
                        raise PlatformInputUiError("听后选择小题数量超过页面卡片数量")
                    self._fill_choice_card(
                        cards[choice_cursor],
                        question,
                        fill_prompt=True,
                        description=f"听后选择第{choice_cursor + 1}题",
                    )
                    choice_cursor += 1

        if choice_cursor != selection_count:
            raise PlatformInputUiError("听后选择输入没有被完整消费")
        return {"选择题": choice_cursor}


__all__ = ["PlatformInputSelectionContentMixin"]
