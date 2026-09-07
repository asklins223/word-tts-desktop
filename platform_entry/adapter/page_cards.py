"""Question-card discovery and question-level field actions."""

from __future__ import annotations

from .page_shared import *  # noqa: F403,F401


# Resolve the whole "climb up to 10 ancestors and test the card conditions"
# walk in one round-trip.  The checks mirror the Python loop below exactly:
# heading kind in inner text, an 添加选项/添加答案 control somewhere below,
# the editor/input caps, and at least one mounted control.
_QUESTION_CARD_LEVEL_JS = """
(node, kind) => {
    let parent = node;
    for (let level = 0; level < 10; level += 1) {
        parent = parent.parentElement;
        if (!parent) {
            return -1;
        }
        const text = String(parent.innerText || '');
        if (!text.includes(kind)) {
            continue;
        }
        const editors = parent.querySelectorAll(
            '.rich-text-editor .editor-content[contenteditable="true"]'
        ).length;
        const inputs = Array.from(parent.querySelectorAll('input')).filter(
            (el) => String(el.getAttribute('type') || '') !== 'file'
        ).length;
        if (!/添加选项|添加答案/.test(text)) {
            continue;
        }
        if (inputs > 24 || editors > 24) {
            continue;
        }
        if (editors > 0 || inputs > 0) {
            return level + 1;
        }
    }
    return -1;
}
"""

_EDITOR_PLACEHOLDERS_JS = """
(nodes) => nodes.map((node) => String(node.getAttribute('data-placeholder') || ''))
"""

_QUESTION_HEADING_SNAPSHOT_JS = """
(nodes) => nodes.map((node, index) => {
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return {
        index,
        text: String(node.innerText || ''),
        visible: style.display !== 'none'
            && style.visibility !== 'hidden'
            && rect.width > 0
            && rect.height > 0,
    };
})
"""

_QUESTION_CARD_CONFIRM_JS = """
(node, kind) => {
    const text = String(node.innerText || '');
    const editors = node.querySelectorAll(
        '.rich-text-editor .editor-content[contenteditable="true"]'
    ).length;
    const inputs = Array.from(node.querySelectorAll('input')).filter(
        (element) => String(element.getAttribute('type') || '') !== 'file'
    ).length;
    return text.includes(kind)
        && /添加选项|添加答案/.test(text)
        && inputs <= 24
        && editors <= 24
        && (editors > 0 || inputs > 0);
}
"""

_ANSWER_CONTROL_SNAPSHOT_JS = """
(nodes) => nodes.map((node, index) => {
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return {
        index,
        text: String(node.innerText || ''),
        className: String(node.getAttribute('class') || ''),
        visible: style.display !== 'none'
            && style.visibility !== 'hidden'
            && rect.width > 0
            && rect.height > 0,
    };
})
"""


class PlatformInputCardMixin:
    """Discover cards and fill text, options, answers, and scores."""

    def _text_editors(self) -> list[Any]:
        editors = self.page.locator(
            '.rich-text-editor .editor-content[contenteditable="true"]'
        )
        result: list[Any] = []
        primary: list[Any] = []
        direct_question: list[Any] = []
        fallback: list[Any] = []
        placeholders = None
        batch_read = getattr(editors, "evaluate_all", None)
        if callable(batch_read):
            try:
                snapshot = batch_read(_EDITOR_PLACEHOLDERS_JS)
            except Exception:
                snapshot = None
            if isinstance(snapshot, list):
                placeholders = [str(value or "") for value in snapshot]
        if placeholders is None:
            try:
                count = editors.count()
            except Exception:
                count = 0
            placeholders = []
            for index in range(count):
                try:
                    placeholders.append(
                        str(
                            editors.nth(index).get_attribute(
                                "data-placeholder"
                            )
                            or ""
                        )
                    )
                except Exception:
                    placeholders.append("")
        for index, placeholder in enumerate(placeholders):
            editor = editors.nth(index)
            if any(token in placeholder for token in _SECTION_TEXT_PLACEHOLDERS):
                continue
            fallback.append(editor)
            if any(token in placeholder for token in _PRIMARY_QUESTION_TEXT_PLACEHOLDERS):
                primary.append(editor)
            if any(token in placeholder for token in _DIRECT_QUESTION_TEXT_PLACEHOLDERS):
                direct_question.append(editor)
            if any(token in placeholder for token in _QUESTION_TEXT_PLACEHOLDERS):
                result.append(editor)
        if len(direct_question) >= len(self.spec.items):
            return direct_question
        if len(primary) >= len(self.spec.items):
            return primary
        return result if len(result) >= len(self.spec.items) else fallback

    def _labeled_text_editors(self, label: str) -> list[Any]:
        """按字段标签找到富文本编辑器，不依赖模板编辑器的固定序号。"""

        matches: list[Any] = []
        nodes = self.page.get_by_text(label, exact=True)
        try:
            count = nodes.count()
        except Exception:
            count = 0
        editor_selector = (
            '.rich-text-editor .editor-content[contenteditable="true"]:visible'
        )
        for index in range(count):
            try:
                node = nodes.nth(index)
                if not node.is_visible():
                    continue
                hit_level = _closest_visible_level(node, editor_selector, max_level=8)
                if hit_level > 0:
                    editors = _ancestor_at_level(node, hit_level).locator(
                        editor_selector
                    )
                    # Engine-side confirmation keeps the batch probe from
                    # ever picking a different level than the plain loop.
                    if editors.count() == 1:
                        matches.append(editors.first)
                        continue
                parent = node
                for _level in range(8):
                    parent = parent.locator("xpath=..")
                    editors = parent.locator(editor_selector)
                    if editors.count() == 1:
                        matches.append(editors.first)
                        break
            except Exception:
                continue
        return matches

    def _replace_rich_text(
        self,
        editor: Any,
        value: str,
        *,
        field: str = "题干",
    ) -> None:
        expected = _normalise_text(_text_without_html(value))
        keyboard = getattr(self.page, "keyboard", None)
        insert_text = (
            getattr(keyboard, "insert_text", None) if keyboard is not None else None
        )
        try:
            # 该富文本控件的持久化状态依赖键盘输入事件；直接 fill() 可能
            # 只改变 DOM，保存后又被页面状态覆盖。按用户操作替换内容，
            # 让编辑器真正收到选中和删除键盘事件。
            _select_all_editable_text(
                editor,
                timeout_ms=self.action_timeout_ms,
            )
            _press_focused_key(
                self.page,
                editor,
                "Backspace",
                timeout_ms=self.action_timeout_ms,
            )
            # 逐字符 type 每个字符都是一次驱动往返，长文本在 Windows 上
            # 尤其慢。一次性 insertText 触发同样的 input 事件链；若个别
            # 编辑器只认逐字击键，回读不一致时再退回原路径兜底。
            if callable(insert_text):
                insert_text(str(value))
            else:
                editor.type(value, timeout=self.action_timeout_ms)
            _press_focused_key(
                self.page,
                editor,
                "Tab",
                timeout_ms=self.action_timeout_ms,
            )
            actual = _normalise_text(editor.inner_text(timeout=self.action_timeout_ms))
            if expected and expected not in actual:
                # Replay the original char-by-char path before failing; some
                # editor builds only persist state on real keystrokes.
                editor.click(timeout=self.action_timeout_ms)
                editor.press("ControlOrMeta+A", timeout=self.action_timeout_ms)
                _press_focused_key(
                    self.page,
                    editor,
                    "Backspace",
                    timeout_ms=self.action_timeout_ms,
                )
                editor.type(value, timeout=self.action_timeout_ms)
                _press_focused_key(
                    self.page,
                    editor,
                    "Tab",
                    timeout_ms=self.action_timeout_ms,
                )
                actual = _normalise_text(
                    editor.inner_text(timeout=self.action_timeout_ms)
                )
        except Exception as exc:
            raise PlatformInputUiError(f"填写{field}失败: {exc}") from exc
        if expected and expected not in actual:
            raise PlatformInputUiError(
                f"{field}回读不一致：期望包含 {value!r}，实际为 {actual!r}"
            )

    def _clear_rich_text(self, editor: Any, *, field: str) -> None:
        """按页面键盘操作清空富文本字段，并确认字段确实为空。"""

        try:
            _select_all_editable_text(
                editor,
                timeout_ms=self.action_timeout_ms,
            )
            _press_focused_key(
                self.page,
                editor,
                "Backspace",
                timeout_ms=self.action_timeout_ms,
            )
            _press_focused_key(
                self.page,
                editor,
                "Tab",
                timeout_ms=self.action_timeout_ms,
            )
            actual = _normalise_text(
                _text_without_html(editor.inner_text(timeout=self.action_timeout_ms))
            )
        except Exception as exc:
            raise PlatformInputUiError(f"清空{field}失败: {exc}") from exc
        if actual:
            raise PlatformInputUiError(
                f"{field}清空后仍有内容：实际为 {actual!r}"
            )

    def _labeled_inputs(self, labels: Sequence[str]) -> list[Any]:
        for label in labels:
            matches: list[Any] = []
            nodes = self.page.get_by_text(label, exact=True)
            try:
                count = nodes.count()
            except Exception:
                count = 0
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    if not node.is_visible():
                        continue
                    hit_level = _closest_visible_level(
                        node, "input:visible", max_level=7
                    )
                    if hit_level > 0:
                        inputs = _ancestor_at_level(node, hit_level).locator(
                            "input:visible"
                        )
                        if inputs.count() == 1:
                            matches.append(inputs.first)
                            continue
                    parent = node
                    for _level in range(7):
                        parent = parent.locator("xpath=..")
                        inputs = parent.locator("input:visible")
                        if inputs.count() == 1:
                            matches.append(inputs.first)
                            break
                except Exception:
                    continue
            if matches:
                return matches
        return []

    def _labeled_input_in_scope(self, scope: Any, label: str) -> Any | None:
        """在一个小题卡片内找字段，避免把相邻小题的输入串位。"""

        input_selector = 'input:visible:not([type="file"])'
        try:
            nodes = scope.get_by_text(label, exact=True)
            count = nodes.count()
        except Exception:
            return None
        for index in range(count):
            try:
                node = nodes.nth(index)
                if not node.is_visible():
                    continue
                hit_level = _closest_visible_level(
                    node, input_selector, max_level=8
                )
                if hit_level > 0:
                    inputs = _ancestor_at_level(node, hit_level).locator(
                        input_selector
                    )
                    if inputs.count() == 1:
                        return inputs.first
                parent = node
                for _level in range(8):
                    parent = parent.locator("xpath=..")
                    inputs = parent.locator(input_selector)
                    if inputs.count() == 1:
                        return inputs.first
            except Exception:
                continue
        return None

    def _labeled_text_editor_in_scope(self, scope: Any, label: str) -> Any | None:
        editor_selector = (
            '.rich-text-editor .editor-content[contenteditable="true"]:visible'
        )
        try:
            nodes = scope.get_by_text(label, exact=True)
            count = nodes.count()
        except Exception:
            return None
        for index in range(count):
            try:
                node = nodes.nth(index)
                if not node.is_visible():
                    continue
                hit_level = _closest_visible_level(
                    node, editor_selector, max_level=8
                )
                if hit_level > 0:
                    editors = _ancestor_at_level(node, hit_level).locator(
                        editor_selector
                    )
                    if editors.count() == 1:
                        return editors.first
                parent = node
                for _level in range(8):
                    parent = parent.locator("xpath=..")
                    editors = parent.locator(editor_selector)
                    if editors.count() == 1:
                        return editors.first
            except Exception:
                continue
        return None

    @staticmethod
    def _visible_content_editors(scope: Any) -> list[Any]:
        editors = scope.locator(
            '.rich-text-editor .editor-content[contenteditable="true"]:visible'
        )
        result: list[Any] = []
        try:
            count = editors.count()
        except Exception:
            count = 0
        for index in range(count):
            result.append(editors.nth(index))
        return result

    @staticmethod
    def _normalise_question_heading(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "")).strip()

    def _question_card_from_heading(self, node: Any, question_kind: str) -> Any | None:
        """从侧栏/卡片同名标题中选出真正包含控件的题目卡片。"""

        batch_probe = getattr(node, "evaluate", None)
        if callable(batch_probe):
            try:
                hit_level = int(batch_probe(_QUESTION_CARD_LEVEL_JS, question_kind))
            except Exception:
                hit_level = -1
            if hit_level > 0:
                card = _ancestor_at_level(node, hit_level)
                try:
                    # Confirm all conditions in one browser operation.  The
                    # previous confirmation performed inner_text plus three
                    # separate counts for every question card.
                    if card.evaluate(
                        _QUESTION_CARD_CONFIRM_JS,
                        question_kind,
                    ):
                        return card
                except Exception:
                    pass

        parent = node
        for _level in range(10):
            try:
                parent = parent.locator("xpath=..")
                text = str(parent.inner_text())
                if question_kind not in text:
                    continue
                # 题目表单位于独立的可滚动面板内。Chromium 在新开的
                # 持久化窗口中可能把尚未滚到视口的子节点报告为不可见，
                # 即使它们已经挂载并可通过 scroll_into_view 操作。题卡
                # 识别阶段使用已挂载控件，真正填写时仍会逐控件滚动。
                editors = parent.locator(
                    '.rich-text-editor .editor-content[contenteditable="true"]'
                )
                inputs = parent.locator('input:not([type="file"])')
                controls = parent.get_by_text(
                    re.compile(r"添加选项|添加答案")
                )
                control_count = controls.count()
                editor_count = editors.count()
                input_count = inputs.count()
                if not control_count:
                    continue
                # 页面外层会包含几十个小题；真正卡片的编辑器/输入框数量
                # 较小。这个上限也能排除左侧导航同名标题的祖先节点。
                if input_count > 24 or editor_count > 24:
                    continue
                if editor_count or input_count:
                    return parent
            except Exception:
                continue
        return None

    def _question_cards(self, question_kind: str) -> list[Any]:
        pattern = re.compile(
            r"^(?:小题|第)\s*\d+\s*[（(]\s*"
            + re.escape(question_kind)
            + r"\s*[）)]$"
        )
        try:
            nodes = self.page.get_by_text(pattern)
        except Exception:
            return []
        candidate_indices = None
        batch_snapshot_used = False
        batch_read = getattr(nodes, "evaluate_all", None)
        if callable(batch_read):
            try:
                snapshot = batch_read(_QUESTION_HEADING_SNAPSHOT_JS)
            except Exception:
                snapshot = None
            if isinstance(snapshot, list):
                batch_snapshot_used = True
                candidate_indices = []
                for item in snapshot:
                    if not isinstance(item, Mapping) or not item.get("visible"):
                        continue
                    heading = self._normalise_question_heading(item.get("text"))
                    if re.fullmatch(
                        r"(?:小题|第)\d+[（(]"
                        + re.escape(question_kind)
                        + r"[）)]",
                        heading,
                    ):
                        candidate_indices.append(int(item["index"]))
        if candidate_indices is None:
            try:
                candidate_indices = list(range(nodes.count()))
            except Exception:
                return []
        result: list[Any] = []
        for index in candidate_indices:
            try:
                node = nodes.nth(index)
                # Switching from 听后选择 to 听后应答 leaves the previous
                # lazy-loaded panel in the DOM, but hidden.  Counting those
                # headings makes the response handler wait for 15 cards
                # instead of the 7 cards in the active panel.  Off-screen
                # cards inside the active scroll area still report visible
                # here and remain eligible for scroll_into_view later.
                if not batch_snapshot_used:
                    if not node.is_visible():
                        continue
                    heading = self._normalise_question_heading(node.inner_text())
                    if not re.fullmatch(
                        r"(?:小题|第)\d+[（(]"
                        + re.escape(question_kind)
                        + r"[）)]",
                        heading,
                    ):
                        continue
                card = self._question_card_from_heading(node, question_kind)
                if card is not None:
                    result.append(card)
            except Exception:
                continue
        return result

    def _fill_labeled_input_at(
        self,
        labels: Sequence[str],
        occurrence: int,
        value: Any,
        description: str,
    ) -> None:
        inputs = self._labeled_inputs(labels)
        if occurrence >= len(inputs):
            raise PlatformInputUiError(
                f"页面没有第 {occurrence + 1} 个“{labels[0]}”输入框，无法填写{description}"
            )
        self._type_input_value(inputs[occurrence], value, description)

    def _fill_card_number(
        self,
        card: Any,
        label: str,
        value: Any,
        description: str,
        *,
        required: bool = False,
    ) -> None:
        if value is None:
            if required:
                raise PlatformInputUiError(f"{description}缺少“{label}”")
            return
        input_locator = self._labeled_input_in_scope(card, label)
        if input_locator is None:
            raise PlatformInputUiError(f"页面没有{description}的“{label}”输入框")
        self._type_input_value(input_locator, value, description)

    def _option_row_editors(self, card: Any) -> list[Any]:
        """按编辑器占位字段筛出选项行，不依赖卡片在视口中的位置。

        该页面的选择题卡片编辑器顺序是“题干、若干选项、答案解析”；
        选项和空白题干都可能使用相同的 A/B/C 行文案，因此占位字段比
        祖先节点更稳定，也能兼容编辑既有记录时已经存在的选项。
        """

        editors = card.locator(
            '.rich-text-editor .editor-content[contenteditable="true"]:visible'
        )
        placeholders = None
        batch_read = getattr(editors, "evaluate_all", None)
        if callable(batch_read):
            try:
                values = batch_read(_EDITOR_PLACEHOLDERS_JS)
            except Exception:
                values = None
            if isinstance(values, list):
                placeholders = [str(value or "") for value in values]
        if placeholders is None:
            visible_editors = self._visible_content_editors(card)
            editors = None
            placeholders = []
            for editor in visible_editors:
                try:
                    placeholders.append(
                        str(editor.get_attribute("data-placeholder") or "")
                    )
                except Exception:
                    placeholders.append("")
        else:
            visible_editors = []

        result: list[Any] = []
        for index, placeholder in enumerate(placeholders):
            editor = (
                editors.nth(index)
                if editors is not None
                else visible_editors[index]
            )
            if "题干" in placeholder or "解析" in placeholder:
                continue
            if placeholder == "请输入" or "选项" in placeholder:
                result.append(editor)
        return result

    def _ensure_option_count(self, card: Any, expected: int) -> list[Any]:
        editors = self._option_row_editors(card)

        # 编辑重试可能遇到上一次失败留下的多余选项；按页面“删除”控件
        # 从末尾裁剪，保留 A、B、C... 的前 expected 行，避免每次重试
        # 又继续向同一试卷追加选项。
        while len(editors) > expected:
            deletes = card.get_by_text("删除", exact=True)
            delete_candidates: list[Any] = []
            try:
                delete_count = deletes.count()
            except Exception:
                delete_count = 0
            for delete_index in range(delete_count):
                try:
                    candidate = deletes.nth(delete_index)
                    if candidate.is_visible():
                        delete_candidates.append(candidate)
                except Exception:
                    continue
            if not delete_candidates:
                raise PlatformInputUiError(
                    f"题目卡片有 {len(editors)} 个选项但没有可用的“删除”控件，无法整理为 {expected} 个"
                )
            before = len(editors)
            try:
                delete_candidates[-1].click(timeout=self.action_timeout_ms)
            except Exception as exc:
                raise PlatformInputUiError(f"删除多余选项失败: {exc}") from exc
            self._wait_until(
                lambda: len(self._option_row_editors(card)) < before,
                "删除多余选项后页面没有更新",
                timeout_seconds=10,
                interval_ms=50,
            )
            editors = self._option_row_editors(card)

        attempts = 0
        while len(editors) < expected and attempts < expected + 3:
            add = self._first_visible(card.get_by_text("添加选项", exact=True))
            if add is None:
                raise PlatformInputUiError(
                    f"题目卡片没有“添加选项”按钮，无法补齐 {expected} 个选项"
                )
            before_total = len(self._visible_content_editors(card))
            try:
                add.click(timeout=self.action_timeout_ms)
            except Exception as exc:
                raise PlatformInputUiError(f"点击“添加选项”失败: {exc}") from exc
            attempts += 1
            self._wait_until(
                lambda: len(self._option_row_editors(card)) > len(editors)
                or len(self._visible_content_editors(card)) > before_total,
                "点击“添加选项”后页面没有出现新的选项编辑器",
                timeout_seconds=10,
                interval_ms=50,
            )
            editors = self._option_row_editors(card)
        if len(editors) < expected:
            raise PlatformInputUiError(
                f"题目卡片只找到 {len(editors)} 个选项编辑器，期望 {expected} 个"
            )
        return editors[:expected]

    def _select_correct_answer(self, card: Any, answer: str) -> None:
        # 页面把“正确答案”按钮渲染为 answerNormal/answerSelected，
        # 而选项行中的 A/B/C 只是普通文本。直接按这两个实际控件类
        # 定位，避免同名选项字母在某些小题卡片中抢到点击事件。
        controls = card.locator(".answerNormal, .answerSelected")

        def matching_control() -> Any | None:
            batch_read = getattr(controls, "evaluate_all", None)
            if callable(batch_read):
                try:
                    snapshot = batch_read(_ANSWER_CONTROL_SNAPSHOT_JS)
                except Exception:
                    snapshot = None
                if isinstance(snapshot, list):
                    wanted = _normalise_text(answer).casefold()
                    for item in snapshot:
                        if not isinstance(item, Mapping) or not item.get("visible"):
                            continue
                        if (
                            _normalise_text(item.get("text")).casefold()
                            == wanted
                        ):
                            return controls.nth(int(item["index"]))
                    return None
            try:
                count = controls.count()
            except Exception:
                count = 0
            for index in range(count):
                try:
                    node = controls.nth(index)
                    if node.is_visible() and _normalise_text(node.inner_text()).casefold() == _normalise_text(answer).casefold():
                        return node
                except Exception:
                    continue
            return None

        # 编辑同一张既有试卷时，正确答案可能已经正确；重复点击选中项
        # 在部分前端版本中会触发反选，所以先确认后再操作。
        if self._answer_is_selected(card, answer):
            return

        def click_and_confirm(node: Any) -> None:
            # 答案按钮常位于卡片滚动容器边缘，普通点击可能被相邻
            # 编辑器的透明层拦截；这是实际控件，强制点击后仍用 DOM
            # 选中态回读确认，避免把“点击调用成功”误当成答案已写入。
            node.click(force=True, timeout=self.action_timeout_ms)
            self._wait_until(
                lambda: self._answer_is_selected(card, answer),
                f"选择正确答案“{answer}”后页面没有显示选中状态",
                timeout_seconds=10,
                interval_ms=50,
            )

        node = matching_control()
        if node is None:
            raise PlatformInputUiError(f"题目卡片没有正确答案控件“{answer}”")
        try:
            click_and_confirm(node)
        except Exception as exc:
            raise PlatformInputUiError(f"选择正确答案“{answer}”失败: {exc}") from exc

    def _answer_is_selected(self, card: Any, answer: str) -> bool:
        """读取页面“正确答案”控件的选中样式，而不是猜当前答案。"""

        nodes = card.locator(".answerNormal, .answerSelected")
        batch_read = getattr(nodes, "evaluate_all", None)
        if callable(batch_read):
            try:
                snapshot = batch_read(_ANSWER_CONTROL_SNAPSHOT_JS)
            except Exception:
                snapshot = None
            if isinstance(snapshot, list):
                wanted = _normalise_text(answer).casefold()
                return any(
                    isinstance(item, Mapping)
                    and item.get("visible")
                    and _normalise_text(item.get("text")).casefold() == wanted
                    and "answerSelected" in str(item.get("className") or "")
                    for item in snapshot
                )
        try:
            count = nodes.count()
        except Exception:
            return False
        for index in range(count):
            current = nodes.nth(index)
            for _level in range(5):
                try:
                    if _normalise_text(current.inner_text()).casefold() != _normalise_text(answer).casefold():
                        break
                    class_name = str(current.get_attribute("class") or "")
                    if "answerSelected" in class_name:
                        return True
                    break
                except Exception:
                    break
        return False

    def _fill_choice_card(
        self,
        card: Any,
        question: Mapping[str, Any],
        *,
        fill_prompt: bool,
        description: str,
    ) -> None:
        # 输入规格没有提供题干音频/答案解析音频；已有试卷中的残留音频
        # 必须通过当前题卡的删除控件清空，不能让旧模板内容继续保存。
        self._clear_audio_in_scope(card, "题干音频", description)
        self._clear_audio_in_scope(card, "答案解析音频", description)
        if fill_prompt:
            stem = self._labeled_text_editor_in_scope(card, "题干")
            if stem is None:
                raise PlatformInputUiError(f"{description}没有“题干”编辑器")
            self._replace_rich_text(stem, question["prompt"], field=f"{description}题干")
        else:
            stem = self._labeled_text_editor_in_scope(card, "题干")
            if stem is None:
                raise PlatformInputUiError(f"{description}没有“题干”编辑器")
            answer_prompt = question.get("answer_prompt")
            if answer_prompt:
                self._replace_rich_text(
                    stem,
                    answer_prompt,
                    field=f"{description}题干",
                )
            else:
                self._clear_rich_text(stem, field=f"{description}题干")

        options = question["options"]
        option_editors = self._ensure_option_count(card, len(options))
        for option_index, option in enumerate(options):
            self._replace_rich_text(
                option_editors[option_index],
                option["text"],
                field=f"{description}选项{option['option_id']}",
            )
        self._fill_card_number(
            card,
            "分数",
            question.get("score"),
            f"{description}分数",
            required=True,
        )
        self._fill_card_number(
            card,
            "答题时长(秒)",
            question.get("answer_time"),
            f"{description}答题时长",
        )
        self._select_correct_answer(card, question["answer"])

    def _answer_inputs_in_card(self, card: Any) -> list[Any]:
        excluded_ids: set[str] = set()
        for label in ("题干播放次数(次)", "分数", "答题时长(秒)"):
            input_locator = self._labeled_input_in_scope(card, label)
            if input_locator is not None:
                try:
                    identifier = str(input_locator.get_attribute("id") or "")
                except Exception:
                    identifier = ""
                if identifier:
                    excluded_ids.add(identifier)
        try:
            inputs = card.locator('input:visible:not([type="file"])')
            count = inputs.count()
        except Exception:
            return []
        result: list[Any] = []
        for index in range(count):
            candidate = inputs.nth(index)
            try:
                if str(candidate.get_attribute("id") or "") in excluded_ids:
                    continue
                result.append(candidate)
            except Exception:
                continue
        return result

    def _fill_answer_rows(
        self,
        card: Any,
        answers: Sequence[str],
        description: str,
    ) -> None:
        expected = len(answers)
        current = self._answer_inputs_in_card(card)

        # Editing an existing paper must be exact. If the previous attempt
        # had more reference answers, merely overwriting the first N rows
        # leaves stale answers in the saved paper and makes retries
        # non-idempotent. Delete from the end until the row count matches.
        while len(current) > expected:
            deletes = card.get_by_text("删除", exact=True)
            delete_candidates: list[Any] = []
            try:
                delete_count = deletes.count()
            except Exception:
                delete_count = 0
            for delete_index in range(delete_count):
                try:
                    candidate = deletes.nth(delete_index)
                    if candidate.is_visible():
                        delete_candidates.append(candidate)
                except Exception:
                    continue
            if not delete_candidates:
                raise PlatformInputUiError(
                    f"{description}有 {len(current)} 个参考答案但没有可用的“删除”控件，"
                    f"无法整理为 {expected} 个"
                )
            before = len(current)
            try:
                delete_candidates[-1].click(timeout=self.action_timeout_ms)
            except Exception as exc:
                raise PlatformInputUiError(f"删除多余参考答案失败: {exc}") from exc
            self._wait_until(
                lambda: len(self._answer_inputs_in_card(card)) < before,
                "删除多余参考答案后页面没有更新",
                timeout_seconds=10,
                interval_ms=50,
            )
            current = self._answer_inputs_in_card(card)

        attempts = 0
        while len(current) < expected and attempts < expected + 3:
            add = self._first_visible(card.get_by_text("添加答案", exact=True))
            if add is None:
                raise PlatformInputUiError(
                    f"{description}没有“添加答案”按钮，无法补齐 {expected} 个参考答案"
                )
            before = len(current)
            try:
                add.click(timeout=self.action_timeout_ms)
            except Exception as exc:
                raise PlatformInputUiError(f"点击“添加答案”失败: {exc}") from exc
            attempts += 1
            self._wait_until(
                lambda: len(self._answer_inputs_in_card(card)) > before,
                "点击“添加答案”后页面没有出现新的答案输入框",
                timeout_seconds=10,
                interval_ms=50,
            )
            current = self._answer_inputs_in_card(card)
        if len(current) < expected:
            raise PlatformInputUiError(
                f"{description}只找到 {len(current)} 个答案输入框，期望 {expected} 个"
            )
        if len(current) != expected:
            raise PlatformInputUiError(
                f"{description}当前有 {len(current)} 个答案输入框，期望精确为 {expected} 个"
            )
        for index, answer in enumerate(answers):
            self._type_input_value(
                current[index],
                answer,
                f"{description}答案{index + 1}",
            )

    def _fill_recording_card(
        self,
        card: Any,
        question: Mapping[str, Any],
        *,
        description: str,
        prompt: str | None = None,
        answers_key: str = "reference_answers",
        prompt_audio_path: str | None = None,
        fill_answer_time: bool = True,
    ) -> None:
        self._clear_audio_in_scope(card, "题干音频", description)
        self._clear_audio_in_scope(card, "答案解析音频", description)
        if prompt is not None:
            stem = self._labeled_text_editor_in_scope(card, "题干")
            if stem is None:
                raise PlatformInputUiError(f"{description}没有“题干”编辑器")
            self._replace_rich_text(stem, prompt, field=f"{description}题干")
        else:
            stem = self._labeled_text_editor_in_scope(card, "题干")
            if stem is None:
                raise PlatformInputUiError(f"{description}没有“题干”编辑器")
            self._clear_rich_text(stem, field=f"{description}题干")
        if prompt_audio_path:
            self._upload_audio_in_scope(
                card,
                prompt_audio_path,
                label="题干音频",
                description=description,
            )
        self._fill_card_number(
            card,
            "分数",
            question.get("score"),
            f"{description}分数",
            required=True,
        )
        if fill_answer_time:
            self._fill_card_number(
                card,
                "答题时长(秒)",
                question.get("answer_time"),
                f"{description}答题时长",
            )
        self._fill_answer_rows(
            card,
            question.get(answers_key, ()),
            description,
        )

    def _fill_item_numbers(self) -> None:
        for key, labels in _ITEM_NUMBER_LABELS.items():
            values = [item.get(key) for item in self.spec.items]
            if not any(value is not None for value in values):
                continue
            inputs = self._labeled_inputs(labels)
            highest_index = max(
                index for index, value in enumerate(values) if value is not None
            )
            if len(inputs) <= highest_index:
                raise PlatformInputUiError(
                    f"页面没有足够的“{labels[0]}”输入框，无法填写第 {highest_index + 1} 题"
                )
            for index, value in enumerate(values):
                if value is None:
                    continue
                self._type_input_value(
                    inputs[index],
                    value,
                    f"第 {index + 1} 题的“{labels[0]}”",
                )
