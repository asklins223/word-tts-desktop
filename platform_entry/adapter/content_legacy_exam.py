"""Visible-page handlers for the 外研旧版信息获取/转述套卷。"""

from __future__ import annotations

from .page_shared import *  # noqa: F403,F401


class PlatformInputLegacyExamContentMixin:
    """Fill legacy recording cards while leaving template timing defaults intact."""

    def _fill_info_acquisition_groups(
        self,
        groups_by_type: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, int]:
        groups = groups_by_type["信息获取"]
        # 外研旧版页面把“信息获取”拆成两个懒加载面板：第一节只挂载
        # 6 张录音题卡，第二节只挂载 4 张录音题卡。不能一次按 10 张卡
        # 激活，否则页面永远不会满足题卡数量校验。
        materials_by_section: dict[str, list[Mapping[str, Any]]] = {}
        section_order: list[str] = []
        for group in groups:
            for material in group.get("materials", ()):
                section = str(material.get("section") or "").strip() or "信息获取"
                if section not in materials_by_section:
                    materials_by_section[section] = []
                    section_order.append(section)
                materials_by_section[section].append(material)

        section_specs = {
            "听选信息": (
                "第一节听选信息",
                self._legacy_subsection_target(
                    "信息获取", "听选信息", ("第1题(录音题)", 1)
                ),
            ),
            "回答问题": (
                "第二节回答问题",
                self._legacy_subsection_target(
                    "信息获取", "回答问题", ("第7题(录音题)", 0)
                ),
            ),
        }
        total_cards = 0
        for section in section_order:
            materials = materials_by_section[section]
            section_title, outline_target = section_specs.get(
                section,
                ("信息获取", None),
            )
            expected_cards = sum(
                len(material.get("questions", ())) for material in materials
            )
            cards, editors = self._activate_group_section(
                "信息获取",
                "录音题",
                expected_cards,
                len(materials),
                outline_target=outline_target,
            )
            card_cursor = 0
            section_material_cursors = 0
            for material in materials:
                section_material_cursors += 1
                description = f"{section_title}第{section_material_cursors}段录音"
                self._replace_rich_text(
                    self._next_listening_editor(editors, section_material_cursors - 1, description),
                    material["listening_text"],
                    field=f"{description}听力原文",
                )
                self._upload_original_audio(
                    material["audio_path"],
                    section_material_cursors - 1,
                    description,
                )
                # 该旧版套卷的播放次数、间隔和答题时长都是页面模板默认
                # 配置；解析器没有这些字段，因此这里不会覆写模板值。
                for question_index, question in enumerate(material.get("questions", ()), start=1):
                    if card_cursor >= len(cards):
                        raise PlatformInputUiError("信息获取小题数量超过当前分节录音题卡片数量")
                    self._fill_recording_card(
                        cards[card_cursor],
                        question,
                        description=f"{section_title}第{question_index}题",
                        prompt=question["prompt"],
                        prompt_audio_path=question.get("prompt_audio_path"),
                        fill_answer_time=False,
                    )
                    card_cursor += 1
            if card_cursor != expected_cards:
                raise PlatformInputUiError(f"{section_title}输入没有被完整消费")
            total_cards += card_cursor
        return {"录音题": total_cards}

    def _legacy_subsection_target(
        self,
        group_type: str,
        subsection: str,
        fallback: tuple[str, int],
    ) -> tuple[str, int]:
        rule = next(
            (
                candidate
                for candidate in PAPER_BUNDLE_RULES
                if candidate.key == self.spec.bundle_rule
            ),
            None,
        )
        if rule is None:
            return fallback
        return rule.outline_subsection_target(group_type, subsection) or fallback

    def _fill_split_info_retelling_groups(
        self,
        groups: Sequence[Mapping[str, Any]],
        *,
        retelling_target: tuple[str, int],
        asking_target: tuple[str, int],
    ) -> dict[str, int]:
        """Fill the two lazy panels used by the 外研旧版转述套卷.

        The parser keeps the recording material, one retelling card, and the
        asking cards in one semantic group. The page, however, mounts them
        under separate outline entries, so each panel gets its own card and
        editor cursors.
        """

        retelling_count = sum(1 for group in groups if group.get("retelling"))
        asking_count = sum(len(group.get("asking", ())) for group in groups)
        cards, editors = self._activate_group_section(
            "信息转述及询问",
            "录音题",
            retelling_count,
            len(groups),
            outline_target=retelling_target,
        )

        card_cursor = 0
        listening_cursor = 0
        audio_cursor = 0
        image_cursor = 0
        for group_index, group in enumerate(groups):
            recording = group["recording"]
            description = "信息转述"
            self._replace_rich_text(
                self._next_listening_editor(editors, listening_cursor, description),
                recording["listening_text"],
                field="信息转述听力原文",
            )
            listening_cursor += 1
            self._upload_original_audio(recording["audio_path"], audio_cursor, description)
            audio_cursor += 1
            if recording.get("image_path") is not None:
                self._upload_image(recording["image_path"], image_cursor)
                image_cursor += 1

            # 只替换第一节的第一组指导文字和指导音频；后续指导内容
            # 继续保留模板原值。
            if group_index == 0 and recording.get("instruction_text") is not None:
                self._fill_instruction_editor(
                    recording["instruction_text"],
                    recording.get("instruction_occurrence"),
                    "信息转述题目指导文字",
                )
                instruction_audio = recording.get("instruction_audio_path")
                if instruction_audio:
                    self._fill_instruction_audio(
                        instruction_audio,
                        recording.get("instruction_occurrence"),
                        "信息转述题目指导文字",
                    )

            retelling = group.get("retelling")
            if isinstance(retelling, Mapping):
                if card_cursor >= len(cards):
                    raise PlatformInputUiError("信息转述题数量超过页面录音题卡片数量")
                self._fill_recording_card(
                    cards[card_cursor],
                    retelling,
                    description="信息转述",
                    prompt=retelling["prompt"],
                    prompt_audio_path=retelling.get("prompt_audio_path"),
                    fill_answer_time=False,
                )
                card_cursor += 1
        if card_cursor != retelling_count:
            raise PlatformInputUiError("信息转述第一节输入没有被完整消费")

        if not asking_count:
            return {"录音题": retelling_count}

        asking_cards, _ = self._activate_group_section(
            "询问信息",
            "录音题",
            asking_count,
            0,
            outline_target=asking_target,
        )
        first_recording = groups[0].get("recording") if groups else None
        if (
            isinstance(first_recording, Mapping)
            and first_recording.get("asking_instruction_text") is not None
        ):
            self._fill_instruction_editor(
                first_recording["asking_instruction_text"],
                first_recording.get("asking_instruction_occurrence"),
                "询问信息题目指导文字",
            )
            asking_instruction_audio = first_recording.get(
                "asking_instruction_audio_path"
            )
            if asking_instruction_audio:
                self._fill_instruction_audio(
                    asking_instruction_audio,
                    first_recording.get("asking_instruction_occurrence"),
                    "询问信息题目指导文字",
                )
        card_cursor = 0
        for group in groups:
            for question_index, question in enumerate(group.get("asking", ())):
                if card_cursor >= len(asking_cards):
                    raise PlatformInputUiError("询问信息题数量超过页面录音题卡片数量")
                self._fill_recording_card(
                    asking_cards[card_cursor],
                    question,
                    description=f"询问信息第{question_index + 1}题",
                    prompt=question["prompt"],
                    fill_answer_time=False,
                )
                card_cursor += 1
        if card_cursor != asking_count:
            raise PlatformInputUiError("询问信息第二节输入没有被完整消费")
        return {"录音题": retelling_count + asking_count}

    def _fill_info_retelling_groups(
        self,
        groups_by_type: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, int]:
        groups = groups_by_type["信息转述及询问"]
        retelling_target = self._legacy_subsection_target(
            "信息转述及询问", "信息转述", ("第1题(录音题)", 2)
        )
        asking_target = self._legacy_subsection_target(
            "信息转述及询问", "询问信息", ("第2题(录音题)", 1)
        )
        rule = next(
            (
                candidate
                for candidate in PAPER_BUNDLE_RULES
                if candidate.key == self.spec.bundle_rule
            ),
            None,
        )
        split_legacy_sections = bool(
            rule
            and rule.outline_subsection_target("信息转述及询问", "信息转述")
            and rule.outline_subsection_target("信息转述及询问", "询问信息")
        )
        if split_legacy_sections and any(
            group.get("asking") for group in groups
        ):
            return self._fill_split_info_retelling_groups(
                groups,
                retelling_target=retelling_target,
                asking_target=asking_target,
            )

        retelling_count = sum(
            (1 if group.get("retelling") else 0) + len(group.get("asking", ()))
            for group in groups
        )
        cards, editors = self._activate_group_section(
            "信息转述及询问",
            "录音题",
            retelling_count,
            len(groups),
        )
        card_cursor = 0
        listening_cursor = 0
        audio_cursor = 0
        image_cursor = 0
        for group_index, group in enumerate(groups):
            recording = group["recording"]
            description = "信息转述"
            self._replace_rich_text(
                self._next_listening_editor(editors, listening_cursor, description),
                recording["listening_text"],
                field="信息转述听力原文",
            )
            listening_cursor += 1
            self._upload_original_audio(recording["audio_path"], audio_cursor, description)
            audio_cursor += 1
            if recording.get("image_path") is not None:
                self._upload_image(recording["image_path"], image_cursor)
                image_cursor += 1

            # 只改第一组的题目指导文字及其音频；后续同类控件保留模板/原值。
            if group_index == 0 and recording.get("instruction_text") is not None:
                self._fill_instruction_editor(
                    recording["instruction_text"],
                    recording.get("instruction_occurrence"),
                    "信息转述题目指导文字",
                )
                instruction_audio = recording.get("instruction_audio_path")
                if instruction_audio:
                    self._fill_instruction_audio(
                        instruction_audio,
                        recording.get("instruction_occurrence"),
                        "信息转述题目指导文字",
                    )

            if (
                group_index == 0
                and recording.get("asking_instruction_text") is not None
            ):
                # In the combined legacy layout, the retelling guidance is
                # the first “题目指导文字” editor and the asking guidance is
                # the next one.  The parser occurrence is relative to the
                # asking subsection, so offset it by the preceding retelling
                # editor only in this non-split layout.
                asking_occurrence = recording.get("asking_instruction_occurrence")
                if asking_occurrence is not None:
                    asking_occurrence += 1
                self._fill_instruction_editor(
                    recording["asking_instruction_text"],
                    asking_occurrence,
                    "询问信息题目指导文字",
                )
                asking_instruction_audio = recording.get(
                    "asking_instruction_audio_path"
                )
                if asking_instruction_audio:
                    self._fill_instruction_audio(
                        asking_instruction_audio,
                        asking_occurrence,
                        "询问信息题目指导文字",
                    )

            retelling = group.get("retelling")
            if isinstance(retelling, Mapping):
                if card_cursor >= len(cards):
                    raise PlatformInputUiError("信息转述题数量超过页面录音题卡片数量")
                self._fill_recording_card(
                    cards[card_cursor],
                    retelling,
                    description="信息转述",
                    prompt=retelling["prompt"],
                    prompt_audio_path=retelling.get("prompt_audio_path"),
                    fill_answer_time=False,
                )
                card_cursor += 1
            for question_index, question in enumerate(group.get("asking", ())):
                if card_cursor >= len(cards):
                    raise PlatformInputUiError("询问信息小题数量超过页面录音题卡片数量")
                self._fill_recording_card(
                    cards[card_cursor],
                    question,
                    description=f"询问信息第{question_index + 1}题",
                    prompt=question["prompt"],
                    fill_answer_time=False,
                )
                card_cursor += 1
        if card_cursor != retelling_count:
            raise PlatformInputUiError("信息转述及询问输入没有被完整消费")
        return {"录音题": card_cursor}


__all__ = ["PlatformInputLegacyExamContentMixin"]
