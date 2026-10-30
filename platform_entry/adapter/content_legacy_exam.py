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
        structure = self._read_outline_structure() if hasattr(self, "_read_outline_structure") else None
        if structure is not None:
            resolved = structure.subsection_target(group_type, subsection)
            if resolved is not None:
                return resolved
        if rule is None:
            return fallback
        target = rule.outline_subsection_target(group_type, subsection) or fallback
        # The same 14-card bundle is also used by the Shenzhen paper, whose
        # visible outline numbers run from 1 to 14. Only use global numbers
        # after the complete DOM outline confirms that layout; older Foshan
        # pages repeat local numbers and must keep their registered targets.
        page = getattr(self, "page", None)
        if page is None:
            return target
        nodes = page.get_by_text(
            re.compile(r"^\s*第\s*\d+\s*题\s*[（(]\s*录音题\s*[）)]\s*$")
        )
        numbers: list[int] = []
        global_targets: dict[int, tuple[str, int]] = {}
        for index in range(nodes.count()):
            node = nodes.nth(index)
            if node.is_visible():
                text = _fast_inner_text(node).strip()
                match = re.search(r"第\s*(\d+)", text)
                if match:
                    number = int(match.group(1))
                    numbers.append(number)
                    global_targets[number] = (text, 0)
        if numbers != list(range(1, self.spec.question_count + 1)):
            return target
        first_number = self._group_first_question_number(group_type)
        if first_number is None:
            return target
        if group_type == "信息获取":
            for group in self.spec.groups:
                if group.get("type") != group_type:
                    continue
                for material in group.get("materials", ()):
                    if material.get("section") == subsection:
                        return global_targets.get(first_number, target)
                    first_number += len(material.get("questions", ()))
        elif group_type == "信息转述及询问":
            if subsection == "信息转述":
                return global_targets.get(first_number, target)
            if subsection == "询问信息":
                first_number += sum(
                    bool(group.get("retelling"))
                    for group in self.spec.groups
                    if group.get("type") == group_type
                )
                return global_targets.get(first_number, target)
        return target

    def _retelling_page_instruction_scopes(self) -> tuple[Any, Any] | None:
        """Find the introduction and reading guidance in their own pages.

        Page 1 has the introduction. Page 2 has reading guidance before the
        listening material. The starter belongs to the recording card, not
        the separate preparation field after the material. Each page guide
        keeps text and audio in the same textAudioContent scope.
        """

        page = getattr(self, "page", None)
        if page is None:
            return None
        pages = page.locator(".rightMiddleContent:visible")
        page_scopes = []
        for number in (1, 2):
            candidates = pages.filter(
                has=page.get_by_text(re.compile(rf"^页面\s*{number}$"))
            )
            if candidates.count() == 0:
                return None
            if candidates.count() != 1:
                raise PlatformInputUiError(f"信息转述有多个“页面{number}”，无法确定指导语位置")
            page_scopes.append(candidates.first)
        label = page.get_by_text(re.compile(r"^题目指导文字\s*[*＊]?$"))
        first = page_scopes[0].locator(".textAudioContent:visible").filter(has=label)
        second_page = page_scopes[1].locator(".textAudioContent:visible").filter(has=label)
        before_listening = []
        for index in range(second_page.count()):
            scope = second_page.nth(index)
            position = scope.evaluate("""(el) => {
                const page = el.closest('.rightMiddleContent');
                const nodes = [...page.querySelectorAll('.textAudioContent, .textWord')];
                const labels = nodes.filter(node => node.textContent.trim() === '听力原文');
                return labels.length === 1 ? Math.sign(nodes.indexOf(el) - nodes.indexOf(labels[0])) : 0;
            }""")
            if position == -1:
                before_listening.append(scope)
        if not before_listening or first.count() == 0:
            return None
        if first.count() != 1 or len(before_listening) != 1:
            raise PlatformInputUiError("信息转述的中文指导语位置不唯一，拒绝按整页序号录入")
        scopes = (first.first, before_listening[0])
        for number, scope in enumerate(scopes, start=1):
            if (
                scope.locator('.editor-content[contenteditable="true"]:visible').count() != 1
                or self._audio_region_in_scope(scope, "题目指导文字音频") is None
            ):
                raise PlatformInputUiError(f"信息转述第{number}条指导文字与音频字段不完整")
        return scopes

    def _fill_retelling_page_instruction(
        self,
        scope: Any,
        text: str,
        audio_path: str | None,
        description: str,
    ) -> None:
        editor = scope.locator('.editor-content[contenteditable="true"]:visible')
        self._replace_rich_text(editor, text, field=description)
        if audio_path:
            self._upload_audio_in_scope(
                scope,
                audio_path,
                label="题目指导文字音频",
                description=description,
            )

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
        instruction_scopes = self._retelling_page_instruction_scopes()
        if instruction_scopes is not None and len(groups) != 1:
            raise PlatformInputUiError("当前指导语页面只能录入一组信息转述材料")
        has_reading_instruction = any(group["recording"].get("reading_instruction_text") for group in groups)
        if has_reading_instruction:
            if instruction_scopes is None or len(instruction_scopes) != 2:
                raise PlatformInputUiError("当前文档有独立阅图指导语，页面没有对应的指导文字与音频位置")
            for card in cards:
                if (
                    self._labeled_text_editor_in_scope(card, "题干") is None
                    or self._audio_region_in_scope(card, "题干音频") is None
                ):
                    raise PlatformInputUiError("信息转述小题没有完整的题干与题干音频位置")

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

            if group_index == 0 and recording.get("instruction_text") is not None:
                instruction_audio = recording.get("instruction_audio_path")
                if instruction_scopes is not None:
                    self._fill_retelling_page_instruction(
                        instruction_scopes[0],
                        recording["instruction_text"],
                        instruction_audio,
                        "信息转述页面1指导文字",
                    )
                else:
                    self._fill_instruction_editor(
                        recording["instruction_text"],
                        recording.get("instruction_occurrence"),
                        "信息转述题目指导文字",
                    )
                    if instruction_audio:
                        self._fill_instruction_audio(
                            instruction_audio,
                            recording.get("instruction_occurrence"),
                            "信息转述题目指导文字",
                        )

            if recording.get("reading_instruction_text"):
                self._fill_retelling_page_instruction(
                    instruction_scopes[1],
                    recording["reading_instruction_text"],
                    recording.get("reading_instruction_audio_path"),
                    "信息转述页面2阅图指导文字",
                )

            retelling = group.get("retelling")
            if isinstance(retelling, Mapping):
                if card_cursor >= len(cards):
                    raise PlatformInputUiError("信息转述题数量超过页面录音题卡片数量")
                prompt_audio = retelling.get("prompt_audio_path")
                self._fill_recording_card(
                    cards[card_cursor],
                    retelling,
                    description="信息转述",
                    prompt=retelling["prompt"],
                    prompt_audio_path=prompt_audio,
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
        # The page outline decides whether retelling and asking are separate
        # lazy panels.  Template-name rules are only the fallback for pages
        # whose outline could not be read.
        structure = self._read_outline_structure() if hasattr(self, "_read_outline_structure") else None
        if structure is not None:
            split_legacy_sections = bool(
                structure.subsection_target("信息转述及询问", "信息转述")
                and structure.subsection_target("信息转述及询问", "询问信息")
            )
        has_reading_instruction = any(
            group["recording"].get("reading_instruction_text") for group in groups
        )
        if split_legacy_sections and (
            has_reading_instruction or any(group.get("asking") for group in groups)
        ):
            return self._fill_split_info_retelling_groups(
                groups,
                retelling_target=retelling_target,
                asking_target=asking_target,
            )

        if has_reading_instruction:
            raise PlatformInputUiError("当前模板尚未配置三条中文指导语的对应位置")

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
