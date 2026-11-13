"""Visible-page handlers for the 外研旧版信息获取/转述套卷。"""

from __future__ import annotations

from collections.abc import Callable

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
        target = (
            rule.outline_subsection_target(group_type, subsection) if rule is not None else None
        ) or fallback
        # Both standalone specials and Shenzhen papers can number the outline
        # globally. Only use those numbers after the complete DOM outline
        # confirms that layout, including for renamed/unregistered templates;
        # older Foshan pages must keep their registered local targets.
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

    def _retelling_panel_spans_pages(self) -> bool:
        """Return whether the open card renders as more than one page block.

        The reviewed standalone template keeps 信息转述 on its own lazy panel
        and renders that card as two right-hand pages (页面1/页面2); 询问信息
        then mounts from the next recording-card outline entry.  A
        single-panel layout mounts every card of the group at once, so the
        multi-page shape is what tells the two layouts apart.
        """

        try:
            return self.page.locator(".rightMiddleContent:visible").count() >= 2
        except Exception:
            return False

    def _split_retelling_shape_ready(self, retelling_only: int) -> Callable[[], bool]:
        """Accept the retelling-only panel once its mount has settled."""

        streak = {"hits": 0}

        def ready() -> bool:
            matched = (
                retelling_only > 0
                and len(self._question_cards("录音题")) == retelling_only
                and self._retelling_panel_spans_pages()
            )
            streak["hits"] = streak["hits"] + 1 if matched else 0
            return streak["hits"] >= 3

        return ready

    def _next_recording_outline_target(self) -> tuple[str, int] | None:
        """Return the next recording entry when the outline has no subsections.

        The outline is the page's own statement of where the next lazy panel
        lives.  Reading it keeps the asking questions from being guessed from
        a template name or a hard-coded occurrence.
        """

        page = getattr(self, "page", None)
        if page is None:
            return None
        try:
            boxes = page.locator(".qBox")
            count = boxes.count()
        except Exception:
            return None
        entries: list[tuple[str, bool]] = []
        for index in range(count):
            try:
                box = boxes.nth(index)
                if not box.is_visible():
                    continue
                text = re.sub(
                    r"\s+", "", _fast_inner_text(box, timeout_ms=300)
                )
                if re.fullmatch(r"第\s*(\d+)\s*题?\s*[（(]\s*录音题\s*[）)]", text) is None:
                    continue
                entries.append(
                    (text, "isSelectBox" in str(box.get_attribute("class") or ""))
                )
            except Exception:
                continue
        for position, (_text, selected) in enumerate(entries):
            if not selected:
                continue
            if position + 1 >= len(entries):
                return None
            next_text = entries[position + 1][0]
            occurrence = sum(
                1 for value, _selected in entries[: position + 1] if value == next_text
            )
            return next_text, occurrence
        return None

    def _locate_retelling_page_instructions(
        self,
    ) -> tuple[Any, list[Any], list[Any]] | None:
        """Locate the three Chinese guidance fields of the split retelling page.

        Page 1 has the introduction.  Page 2 has the guidance spoken before
        the listening material and, separately, the one spoken after it.  The
        starter belongs to the recording card, not to those page fields.  Each
        page guide keeps text and audio in the same textAudioContent scope.
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
        after_listening = []
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
            elif position == 1:
                after_listening.append(scope)
        return first, before_listening, after_listening

    def _retelling_page_instruction_scopes(self) -> tuple[Any, Any] | None:
        """Find the introduction and reading guidance in their own pages."""

        located = self._locate_retelling_page_instructions()
        if located is None:
            return None
        first, before_listening, _ = located
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

    def _retelling_page_preparation_scope(self) -> Any | None:
        """Find the guidance field the page speaks after the listening material."""

        located = self._locate_retelling_page_instructions()
        if located is None:
            return None
        _, _, after_listening = located
        if not after_listening:
            return None
        if len(after_listening) != 1:
            raise PlatformInputUiError("信息转述录音后的指导文字位置不唯一，拒绝按整页序号录入")
        scope = after_listening[0]
        if (
            scope.locator('.editor-content[contenteditable="true"]:visible').count() != 1
            or self._audio_region_in_scope(scope, "题目指导文字音频") is None
        ):
            raise PlatformInputUiError("信息转述录音后的指导文字与音频字段不完整")
        return scope

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

    @staticmethod
    def _shared_retelling_recording(groups):
        """Resolve guidance on a page with one shared slot per subsection."""

        shared = dict(groups[0]["recording"]) if groups else {}
        for key in (
            "instruction_text", "reading_instruction_text",
            "preparation_instruction_text", "asking_instruction_text",
        ):
            recordings = [group["recording"] for group in groups if group["recording"].get(key)]
            if len({recording[key] for recording in recordings}) > 1:
                raise PlatformInputUiError("多组材料的中文指导语不同，当前共享指导语页面无法逐组对应")
            if recordings:
                prefix = key.removesuffix("text")
                for field in (key, prefix + "audio_path", prefix + "occurrence"):
                    if field in recordings[0]:
                        shared[field] = recordings[0][field]
        return shared

    def _combined_retelling_instruction_plan(self, groups):
        requests = []
        for group in groups:
            recording = group["recording"]
            for prefix, description, offset in (
                ("", "信息转述题目指导文字", 0),
                ("asking_", "询问信息题目指导文字", 1),
            ):
                text = recording.get(prefix + "instruction_text")
                if text is None:
                    continue
                occurrence = recording.get(prefix + "instruction_occurrence")
                if occurrence is not None:
                    occurrence += offset
                requests.append((occurrence, text, recording.get(prefix + "instruction_audio_path"), description))
        if len(groups) <= 1:
            return requests

        slots = {}
        for request in requests:
            occurrence, text, *_ = request
            if occurrence in slots and slots[occurrence][1] != text:
                break
            slots[occurrence] = request
        else:
            return list(slots.values())

        # Repeated material panels can expose one guide pair per material.
        # A shared pair cannot represent different source guidance.
        if len(self._labeled_text_editors("题目指导文字")) != len(requests):
            raise PlatformInputUiError("多组材料的指导语数量与页面位置不一致，无法逐组对应")
        return [(index, *request[1:]) for index, request in enumerate(requests)]

    def _fill_split_info_retelling_groups(
        self,
        groups: Sequence[Mapping[str, Any]],
        *,
        retelling_target: tuple[str, int],
        asking_target: tuple[str, int],
        preactivated: tuple[Sequence[Any], Sequence[Any]] | None = None,
    ) -> dict[str, int]:
        """Fill the two lazy panels used by the 外研旧版转述套卷.

        The parser keeps the recording material, one retelling card, and the
        asking cards in one semantic group. The page, however, mounts them
        under separate outline entries, so each panel gets its own card and
        editor cursors.  ``preactivated`` carries the retelling panel that a
        caller already opened while deciding the layout, so the card is not
        clicked a second time.
        """

        retelling_count = sum(1 for group in groups if group.get("retelling"))
        asking_count = sum(len(group.get("asking", ())) for group in groups)
        shared_recording = self._shared_retelling_recording(groups)
        if asking_count and retelling_target == asking_target:
            raise PlatformInputUiError("信息转述和询问信息指向同一个导航，无法区分两组题卡")
        if preactivated is None:
            cards, editors = self._activate_group_section(
                "信息转述及询问",
                "录音题",
                retelling_count,
                len(groups),
                outline_target=retelling_target,
            )
        else:
            cards, editors = list(preactivated[0]), list(preactivated[1])
        instruction_scopes = self._retelling_page_instruction_scopes()
        has_extra_instruction = any(
            group["recording"].get("reading_instruction_text")
            or group["recording"].get("preparation_instruction_text")
            for group in groups
        )
        if has_extra_instruction:
            if instruction_scopes is None or len(instruction_scopes) != 2:
                raise PlatformInputUiError("当前文档有独立中文指导语，页面没有对应的指导文字与音频位置")
            for card in cards:
                if (
                    self._labeled_text_editor_in_scope(card, "题干") is None
                    or self._audio_region_in_scope(card, "题干音频") is None
                ):
                    raise PlatformInputUiError("信息转述小题没有完整的题干与题干音频位置")
        has_preparation_instruction = any(
            group["recording"].get("preparation_instruction_text") for group in groups
        )
        preparation_scope = (
            self._retelling_page_preparation_scope() if has_preparation_instruction else None
        )
        if has_preparation_instruction and preparation_scope is None:
            raise PlatformInputUiError(
                "当前文档有录音后的准备指导语，页面没有对应的指导文字与音频位置"
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

            guidance = shared_recording
            if group_index == 0 and guidance.get("instruction_text") is not None:
                instruction_audio = guidance.get("instruction_audio_path")
                if instruction_scopes is not None:
                    self._fill_retelling_page_instruction(
                        instruction_scopes[0],
                        guidance["instruction_text"],
                        instruction_audio,
                        "信息转述页面1指导文字",
                    )
                else:
                    self._fill_instruction_editor(
                        guidance["instruction_text"],
                        guidance.get("instruction_occurrence"),
                        "信息转述题目指导文字",
                    )
                    if instruction_audio:
                        self._fill_instruction_audio(
                            instruction_audio,
                            guidance.get("instruction_occurrence"),
                            "信息转述题目指导文字",
                        )

            if group_index == 0 and guidance.get("reading_instruction_text"):
                self._fill_retelling_page_instruction(
                    instruction_scopes[1],
                    guidance["reading_instruction_text"],
                    guidance.get("reading_instruction_audio_path"),
                    "信息转述页面2阅图指导文字",
                )

            if group_index == 0 and guidance.get("preparation_instruction_text"):
                self._fill_retelling_page_instruction(
                    preparation_scope,
                    guidance["preparation_instruction_text"],
                    guidance.get("preparation_instruction_audio_path"),
                    "信息转述页面2准备指导文字",
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
        first_recording = shared_recording
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
            group["recording"].get("reading_instruction_text")
            or group["recording"].get("preparation_instruction_text")
            for group in groups
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

        retelling_only = sum(1 for group in groups if group.get("retelling"))
        asking_count = sum(len(group.get("asking", ())) for group in groups)
        retelling_count = retelling_only + asking_count
        # 页面把转述卡单独渲染成两页、询问信息留在下一题入口时，先按合并
        # 页去等 3 张卡会白等到超时。用一个页面自身可读的形态接受这种
        # 分面板挂载，再把已经打开的转述面板交给分面板流程。
        alternate_ready = (
            self._split_retelling_shape_ready(retelling_only)
            if asking_count and retelling_only < retelling_count
            else None
        )
        cards, editors = self._activate_group_section(
            "信息转述及询问",
            "录音题",
            retelling_count,
            len(groups),
            alternate_ready=alternate_ready,
        )
        if len(cards) != retelling_count:
            asking_target = self._next_recording_outline_target()
            if asking_target is None:
                raise PlatformInputUiError(
                    f"“信息转述及询问”当前有 {len(cards)} 个录音题，"
                    f"输入/模板要求 {retelling_count} 个，且找不到询问信息的题目导航"
                )
            return self._fill_split_info_retelling_groups(
                groups,
                retelling_target=retelling_target,
                asking_target=asking_target,
                preactivated=(cards, editors),
            )
        instructions = self._combined_retelling_instruction_plan(groups)
        for occurrence, text, audio_path, description in instructions:
            self._fill_instruction_editor(text, occurrence, description)
            if audio_path:
                self._fill_instruction_audio(audio_path, occurrence, description)
        card_cursor = 0
        listening_cursor = 0
        audio_cursor = 0
        image_cursor = 0
        for group in groups:
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
