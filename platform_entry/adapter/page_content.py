"""Shared content orchestration and page-independent content actions."""

from __future__ import annotations

from .handlers import get_question_type_handler, registered_question_types
from .page_shared import *  # noqa: F403,F401


class PlatformInputContentMixin:
    """Coordinate registered type handlers and shared save/readback actions."""

    @staticmethod
    def _group_requested_card_counts(
        groups: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Count visible cards through the code-owned handler registry."""

        counts = {"选择题": 0, "填空题": 0, "录音题": 0}
        for group in groups:
            handler = get_question_type_handler(group["type"])
            if handler is None:
                raise PlatformInputUiError(f"题型处理器未注册：{group['type']}")
            for card_kind, count in handler.card_counter(group).items():
                if card_kind not in counts:
                    raise PlatformInputUiError(
                        f"题型处理器 {handler.canonical_type} 使用了未注册卡片类别：{card_kind}"
                    )
                counts[card_kind] += int(count)
        return counts

    def _fill_instruction_editor(
        self,
        text: str,
        occurrence: int | None,
        description: str,
    ) -> None:
        if occurrence is None:
            raise PlatformInputUiError(
                f"{description}指定了 instruction_text，但没有指定 instruction_occurrence"
            )
        editors = self._labeled_text_editors("题目指导文字")
        if occurrence >= len(editors):
            raise PlatformInputUiError(
                f"页面没有第 {occurrence + 1} 个“题目指导文字”编辑器"
            )
        self._replace_rich_text(editors[occurrence], text, field=description)

    def _activate_group_section(
        self,
        group_type: str,
        question_kind: str,
        expected_cards: int,
        expected_listening_editors: int,
        *,
        outline_target: tuple[str, int] | None = None,
    ) -> tuple[list[Any], list[Any]]:
        """Activate one lazy-loaded family and validate its visible card set."""

        self._activate_outline_section(group_type, outline_target=outline_target)
        cards: list[Any] = []

        def cards_ready() -> bool:
            nonlocal cards
            cards = self._question_cards(question_kind)
            return len(cards) == expected_cards

        try:
            self._wait_until(
                cards_ready,
                f"切换到“{group_type}”后题卡没有完整挂载",
                timeout_seconds=60,
                interval_ms=50,
            )
        except PlatformInputUiError:
            _debug_dom_snapshot(
                self,
                f"mount-timeout:{group_type}:expected-{question_kind}-{expected_cards}",
            )
            raise
        if len(cards) != expected_cards:
            raise PlatformInputUiError(
                f"“{group_type}”当前有 {len(cards)} 个{question_kind}，"
                f"输入/模板要求 {expected_cards} 个，拒绝留下未录入的小题"
            )
        if not expected_listening_editors:
            return cards, []

        listening_editors: list[Any] = []

        def listening_editors_ready() -> bool:
            nonlocal listening_editors
            listening_editors = self._labeled_text_editors("听力原文")
            return len(listening_editors) >= expected_listening_editors

        self._wait_until(
            listening_editors_ready,
            f"“{group_type}”没有完整挂载“听力原文”编辑器",
            timeout_seconds=60,
            interval_ms=50,
        )
        if len(listening_editors) < expected_listening_editors:
            raise PlatformInputUiError(
                f"“{group_type}”只有 {len(listening_editors)} 个“听力原文”编辑器，"
                f"期望 {expected_listening_editors} 个"
            )
        return cards, listening_editors[:expected_listening_editors]

    @staticmethod
    def _next_listening_editor(
        editors: Sequence[Any],
        occurrence: int,
        description: str,
    ) -> Any:
        if occurrence >= len(editors):
            raise PlatformInputUiError(
                f"页面没有足够的“听力原文”编辑器，无法填写{description}"
            )
        return editors[occurrence]

    def _upload_original_audio(
        self,
        path: str,
        occurrence: int,
        description: str,
    ) -> None:
        self._upload_audio(path, occurrence, label="原文音频")
        self._wait_audio_rendered(
            occurrence,
            label="原文音频",
            expected_path=path,
        )

    def _fill_instruction_audio(
        self,
        path: str,
        occurrence: int | None,
        description: str,
    ) -> None:
        if occurrence is None:
            raise PlatformInputUiError(
                f"{description}指定了指导文字音频，但没有指定 occurrence"
            )
        self._upload_audio(
            path,
            occurrence,
            label="题目指导文字音频",
        )
        self._wait_audio_rendered(
            occurrence,
            label="题目指导文字音频",
            expected_path=path,
        )

    def _fill_material_numbers(
        self,
        material: Mapping[str, Any],
        occurrence: int,
        description: str,
    ) -> None:
        if material.get("times") is not None:
            self._fill_labeled_input_at(
                ("原文播放次数(次)", "音频播放次数", "播放次数"),
                occurrence,
                material["times"],
                f"{description}播放次数",
            )
        if material.get("gap") is not None:
            self._fill_labeled_input_at(
                ("原文播放间隔(秒)", "题间间隔时间", "播放间隔"),
                occurrence,
                material["gap"],
                f"{description}播放间隔",
            )

    def _fill_grouped_content(self) -> None:
        """Run each normalized group through its registered page handler."""

        handlers = registered_question_types()
        groups_by_type: dict[str, list[Mapping[str, Any]]] = {
            handler.canonical_type: [] for handler in handlers
        }
        for group in self.spec.groups:
            group_type = group["type"]
            if group_type not in groups_by_type:
                raise PlatformInputUiError(f"题型处理器未注册：{group_type}")
            groups_by_type[group_type].append(group)

        requested_counts = self._group_requested_card_counts(self.spec.groups)
        rule = next(
            (
                candidate
                for candidate in PAPER_BUNDLE_RULES
                if candidate.key == self.spec.bundle_rule
            ),
            None,
        )
        rule_counts = rule.card_counts() if rule is not None else {}
        for card_kind, requested in requested_counts.items():
            expected = rule_counts.get(card_kind, requested)
            if rule is not None and requested != expected:
                raise PlatformInputUiError(
                    f"当前套卷模板要求 {expected} 个{card_kind}，输入只提供 {requested} 个；"
                    "请把所有小题补齐后再执行"
                )

        consumed = {"选择题": 0, "填空题": 0, "录音题": 0}
        for handler in handlers:
            groups = groups_by_type[handler.canonical_type]
            if not groups:
                continue
            method = getattr(self, handler.page_method, None)
            if not callable(method):
                raise PlatformInputUiError(
                    f"题型处理器 {handler.canonical_type} 没有页面处理方法："
                    f"{handler.page_method}"
                )
            result = method(groups_by_type)
            if not isinstance(result, Mapping):
                raise PlatformInputUiError(
                    f"题型处理器 {handler.canonical_type} 未返回消费计数"
                )
            for card_kind, count in result.items():
                if card_kind not in consumed:
                    raise PlatformInputUiError(
                        f"题型处理器 {handler.canonical_type} 返回了未注册卡片类别：{card_kind}"
                    )
                consumed[card_kind] += int(count)

        for card_kind, requested in requested_counts.items():
            if consumed[card_kind] != requested:
                raise PlatformInputUiError(f"{card_kind}输入没有被完整消费")

    def fill_content(self) -> None:
        if self.spec.groups:
            self._fill_grouped_content()
            return
        if _is_imitation_template(self.spec.template_name):
            self._fill_imitation_content()
            return

        editors = self._text_editors()
        if not editors:
            raise PlatformInputUiError("第二步没有找到题干编辑器")
        for item_index, item in enumerate(self.spec.items):
            editor_index = item.get("editor_index", item_index)
            if editor_index >= len(editors):
                raise PlatformInputUiError(
                    f"第 {item_index + 1} 题的 editor_index={editor_index} 超出页面编辑器数量"
                )
            self._replace_rich_text(editors[editor_index], item["text"])

        audio_occurrence = 0
        for item in self.spec.items:
            audio_path = item.get("audio_path")
            if audio_path:
                self._upload_audio(audio_path, audio_occurrence)
                self._wait_audio_rendered(
                    audio_occurrence,
                    expected_path=audio_path,
                )
                audio_occurrence += 1
        self._fill_item_numbers()

    def save_content(self) -> None:
        before = len(self.observer.write_responses_observed)
        self._click_exact("保存试卷")

        def saved() -> bool:
            if self._has_visible_text("保存成功"):
                return True
            events = self.observer.write_responses_observed[before:]
            return any(
                event["path"] in {CONTENT_CREATE_PATH, CONTENT_UPDATE_PATH}
                and isinstance(event.get("status"), int)
                and 200 <= event["status"] < 300
                for event in events
            )

        self._wait_until(
            saved,
            "点击“保存试卷”后没有观察到页面保存成功反馈",
            timeout_seconds=60,
            interval_ms=50,
        )

    def return_to_list(self) -> None:
        # BaseAddEdit 的关闭图标是 w-9 h-9；预览按钮内的图标只有 w-4
        # h-4，因此这个定位不会碰“预览”。当前页面从第二步关闭后会先
        # 回到第一步，再关闭一次才回到列表；两次都是页面关闭控件。
        for _attempt in range(2):
            if self._is_paper_list():
                break
            close_icon = self._first_visible(
                self.page.locator("img.w-9.h-9.cursor-pointer:visible")
            )
            if close_icon is None:
                raise PlatformInputUiError("编辑页没有找到返回控件")
            try:
                close_icon.click(timeout=self.action_timeout_ms)
            except Exception as exc:
                raise PlatformInputUiError(f"返回试卷列表失败: {exc}") from exc
            self._wait_until(
                lambda: self._is_paper_list()
                or self._has_visible_text("第一步：配置基础属性与题型")
                or self._has_visible_text("确认退出"),
                "保存后页面没有返回上一级",
                timeout_seconds=30,
                interval_ms=50,
            )
            if self._has_visible_text("确认退出"):
                self._click_exact("确认退出")
                self._wait_until(
                    lambda: self._is_paper_list()
                    or self._has_visible_text("第一步：配置基础属性与题型"),
                    "确认退出后页面没有返回上一级",
                    timeout_seconds=30,
                    interval_ms=50,
                )
        if not self._is_paper_list():
            raise PlatformInputUiError("保存后没有返回试卷列表")

        before = len(self.observer.read_only_requests)
        query = self._first_visible(self.page.get_by_text("查询", exact=True))
        if query is None:
            return
        self._click_exact("查询")
        self._wait_until(
            lambda: any(
                event["path"] == PAPER_PAGE_PATH
                for event in self.observer.read_only_requests[before:]
            ),
            "刷新试卷列表后没有观察到只读列表反馈",
            timeout_seconds=30,
            interval_ms=50,
        )

    def feedback(self) -> dict[str, Any]:
        return self.observer.snapshot()


__all__ = ["PlatformInputContentMixin"]
