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
        questions = [question for group in groups for question in group.get("questions", ())]
        if any(question.get("section") in {"第一节", "第二节"} for question in questions):
            return self._fill_split_response_questions(questions)
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

    def _split_response_panels(
        self, questions: Sequence[Mapping[str, Any]],
    ) -> list[tuple[Sequence[Mapping[str, Any]], Any, list[int]]]:
        """Resolve the two sections using the actual paper outline."""
        if [question.get("section") for question in questions] != ["第一节"] * 5 + ["第二节"] * 5:
            raise PlatformInputUiError("听后应答新题型需要按顺序提供第一节和第二节各5题")
        structure = self._read_outline_structure()
        if structure is None:
            raise PlatformInputUiError("无法读取听后应答分节导航，拒绝猜测题目位置")
        section = structure.section_for("听后应答")
        outline_cards = tuple(section.cards) if section is not None else ()
        numbers = [card.number for card in outline_cards]
        if len(numbers) != len(questions):
            numbers = list(range(1, len(questions) + 1))
        targets = [structure.subsection_target("听后应答", section) for section in ("第一节", "第二节")]
        if all(target is not None for target in targets):
            return [(questions[index * 5:(index + 1) * 5], target,
                     numbers[index * 5:(index + 1) * 5])
                    for index, target in enumerate(targets)]
        elif all(target is None for target in targets):
            # Some paper templates put both response subsections under one
            # heading and expose their ten question cards in document order.
            # Only use this layout when the outline itself confirms ten
            # distinct, consecutive recording cards; otherwise don't guess.
            cards = outline_cards
            numbers = [card.number for card in cards]
            if (len(cards) != 10 or any(card.kind != "录音题" for card in cards)
                    or numbers != list(range(numbers[0], numbers[0] + 10))):
                raise PlatformInputUiError("当前模板缺少可确认的听后应答题卡顺序")
            return [(questions, None, numbers)]
        else:
            raise PlatformInputUiError("当前模板的听后应答分节导航不完整")

    def _response_screen_question_scope(self, scope: Any, description: str) -> Any:
        """Find the template's screen-only Q: guidance in this question page."""
        candidates = scope.locator(".textAudioContent:visible")
        matches = []
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            editor = self._labeled_text_editor_in_scope(candidate, "题目指导文字")
            if editor is not None and re.match(r"^\s*Q\s*[:：]", _fast_inner_text(editor), re.I):
                matches.append(candidate)
        if len(matches) != 1:
            raise PlatformInputUiError(f"{description}无法唯一定位屏幕问题（Q:）指导文字")
        return matches[0]

    def _preflight_split_response_questions(
        self, questions: Sequence[Mapping[str, Any]],
    ) -> None:
        """Check every question before editing any of the response fields."""
        problems: list[str] = []
        for section_questions, target, numbers in self._split_response_panels(questions):
            cards, _ = self._activate_group_section(
                "听后应答", "录音题", len(section_questions), 0,
                outline_target=target,
            )
            for card, question, number in zip(cards, section_questions, numbers):
                description = f"听后应答第{number}题"
                scope = self._question_page_scope(card, description)
                editor = self._labeled_text_editor_in_scope(scope, "听力原文")
                audio_region = self._audio_region_in_scope(scope, "原文音频")
                picture_only = bool(question.get("image_path")) and editor is None and audio_region is None
                if editor is None and not picture_only:
                    problems.append(f"{description}缺少“听力原文”编辑器")
                if audio_region is None and not picture_only:
                    problems.append(f"{description}缺少“原文音频”上传控件")
                image = self._saved_image(0, scope=scope)
                region = self._image_region(0, scope=scope)
                if question.get("image_path") and image is None and region is None:
                    problems.append(f"{description}需要配图，但页面没有图片控件")
                if not question.get("image_path") and image is not None:
                    problems.append(f"{description}没有文档配图，但模板含示例图")
                if question.get("section") == "第二节":
                    try:
                        self._response_screen_question_scope(scope, description)
                    except PlatformInputUiError as exc:
                        problems.append(str(exc))
        if problems:
            raise PlatformInputUiError("；".join(problems) + "，请先核对并补齐模板")

    def _fill_response_material_numbers(
        self, scope: Any, question: Mapping[str, Any], description: str,
    ) -> None:
        for key, labels, field in (
            ("times", ("原文播放次数(次)", "音频播放次数", "播放次数"), "播放次数"),
            ("gap", ("原文播放间隔(秒)", "题间间隔时间", "播放间隔"), "播放间隔"),
        ):
            value = question.get(key)
            if value is None:
                continue
            input_locator = next(
                (locator for label in labels
                 if (locator := self._labeled_input_in_scope(scope, label)) is not None),
                None,
            )
            if input_locator is None:
                raise PlatformInputUiError(f"{description}缺少{field}输入框")
            self._type_input_value(input_locator, value, f"{description}{field}")

    def _fill_split_response_questions(
        self, questions: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Fill both response sections in the layout exposed by the outline."""
        self._preflight_split_response_questions(questions)
        panels = self._split_response_panels(questions)
        consumed = 0
        for section_questions, target, numbers in panels:
            cards, _ = self._activate_group_section(
                "听后应答", "录音题", len(section_questions), 0,
                outline_target=target,
            )
            for question, card, number in zip(section_questions, cards, numbers):
                description = f"听后应答第{number}题"
                scope = self._question_page_scope(card, description)
                editor = self._labeled_text_editor_in_scope(scope, "听力原文")
                audio_region = self._audio_region_in_scope(scope, "原文音频")
                picture_only = bool(question.get("image_path")) and editor is None and audio_region is None
                if not picture_only:
                    if editor is None or audio_region is None:
                        raise PlatformInputUiError(f"{description}缺少听力原文或原文音频控件")
                    self._replace_rich_text(
                        editor, question["listening_text"], field=f"{description}听力原文",
                    )
                    self._upload_audio_in_scope(
                        scope, question["audio_path"], label="原文音频", description=description,
                    )
                    self._fill_response_material_numbers(scope, question, description)
                if question.get("image_path"):
                    self._upload_image(question["image_path"], 0, scope=scope)
                prompt = question["prompt"]
                if question.get("section") == "第二节":
                    guidance = self._response_screen_question_scope(scope, description)
                    guidance_editor = self._labeled_text_editor_in_scope(guidance, "题目指导文字")
                    clean_prompt = re.sub(r"^\s*Q\s*[:：]\s*", "", prompt, flags=re.I)
                    self._replace_rich_text(
                        guidance_editor, f"Q: {clean_prompt}",
                        field=f"{description}屏幕问题",
                    )
                    self._clear_audio_in_scope(guidance, "题目指导文字音频", description)
                    prompt = None
                self._fill_recording_card(
                    card, question, description=description, prompt=prompt,
                )
                consumed += 1
        return {"录音题": consumed}


__all__ = ["PlatformInputResponseContentMixin"]
