"""Base-form, template-selection, and page-field actions."""

from __future__ import annotations

import re

from .page_shared import *  # noqa: F403,F401


PAPER_TITLE_PLACEHOLDER = (
    "请输入完整试卷名称，例如：2026年佛山市南海区初二上学期英语听说期中考试"
)

# One round-trip instead of count + nth + inner_text per option; matching
# still happens in Python with the shared _normalise_text helper.
_DROPDOWN_TEXTS_JS = "(nodes) => nodes.map((node) => String(node.innerText || ''))"


def _comparable_input_text(raw: Any) -> str:
    """Compare page input values without deleting meaningful inner spaces."""

    return re.sub(r"\s+", " ", str(raw or "")).strip()


class PlatformInputFormMixin:
    """Owns reusable labelled inputs and dropdown controls."""

    def _field_component(self, title: str) -> Any | None:
        for selector in (
            ".selectContent:visible",
            ".inputContent:visible",
            ".scoreInputContent:visible",
            ".resolveInputContent:visible",
        ):
            try:
                roots = self.page.locator(selector).filter(has_text=title)
                candidate = self._first_visible(roots)
                if candidate is not None:
                    return candidate
            except Exception:
                continue
        return None

    def _input_near_label(self, label: str, occurrence: int = 0) -> Any | None:
        labels = self.page.get_by_text(label, exact=True)
        matches: list[Any] = []
        try:
            count = labels.count()
        except Exception:
            count = 0
        for label_index in range(count):
            try:
                node = labels.nth(label_index)
                if not node.is_visible():
                    continue
                hit_level = _closest_visible_level(node, "input:visible", max_level=7)
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
        if occurrence < len(matches):
            return matches[occurrence]
        return None

    def _input_in_labeled_group(
        self,
        component: Any,
        label: str,
    ) -> Any | None:
        """从同一标签组取输入，避免年份/时长等并排字段串位。"""

        groups = component.locator("div.flex.flex-col")
        try:
            count = groups.count()
        except Exception:
            count = 0
        for index in range(count):
            group = groups.nth(index)
            try:
                if self._first_visible(group.get_by_text(label, exact=True)) is None:
                    continue
                input_locator = self._first_visible(group.locator("input:visible"))
                if input_locator is not None:
                    return input_locator
            except Exception:
                continue
        return None

    def _type_input_value(self, input_locator: Any, value: Any, label: str) -> None:
        text = str(value)

        try:
            input_locator.scroll_into_view_if_needed(timeout=self.action_timeout_ms)
            input_locator.click(timeout=self.action_timeout_ms)
            input_locator.press("ControlOrMeta+A", timeout=self.action_timeout_ms)
            # ``fill`` dispatches the native input event that the Vue model
            # listens for, while still preserving an internal space such as
            # the one in “Starter Unit1”.  Some older controls additionally
            # depend on keydown/keyup, so the keyboard path remains a bounded
            # fallback instead of being the only way to set the value.
            input_locator.fill(text, timeout=self.action_timeout_ms)
            input_locator.press("Tab", timeout=self.action_timeout_ms)
            actual = input_locator.input_value(timeout=self.action_timeout_ms)
            if _comparable_input_text(actual) != _comparable_input_text(text):
                input_locator.click(timeout=self.action_timeout_ms)
                input_locator.press("ControlOrMeta+A", timeout=self.action_timeout_ms)
                input_locator.press("Backspace", timeout=self.action_timeout_ms)
                input_locator.type(text, timeout=self.action_timeout_ms)
                input_locator.press("Tab", timeout=self.action_timeout_ms)
                actual = input_locator.input_value(timeout=self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(f"填写“{label}”失败: {exc}") from exc
        if _comparable_input_text(actual) != _comparable_input_text(text):
            raise PlatformInputUiError(
                f"填写“{label}”后回读不一致：期望 {text!r}，实际为 {actual!r}"
            )

    def _fill_input(self, label: str, value: Any, *, placeholder: str = "") -> None:
        input_locator = None
        if placeholder:
            input_locator = self._first_visible(
                self.page.locator(f'input[placeholder="{placeholder}"]:visible')
            )
            if input_locator is None and label == "试卷名称":
                # The placeholder text has changed slightly between platform
                # deployments. Its stable prefix is enough to identify this
                # one field without relying on a page-specific component class.
                input_locator = self._first_visible(
                    self.page.locator(
                        'input[placeholder^="请输入完整试卷名称"]:visible'
                    )
                )
        if input_locator is None:
            component = self._field_component(label)
            if component is not None:
                input_locator = self._input_in_labeled_group(component, label)
                if input_locator is None:
                    input_locator = self._first_visible(
                        component.locator("input:visible")
                    )
        if input_locator is None:
            input_locator = self._input_near_label(label)
        if input_locator is None:
            raise PlatformInputUiError(f"页面上没有“{label}”输入框")

        # 该管理端部分输入框除了 input 事件外还依赖键盘事件更新
        # Vue 状态；统一按页面用户输入方式替换并输入。
        self._type_input_value(input_locator, value, label)

    def ensure_paper_title(self) -> None:
        """Re-assert the title after dependent form controls have rerendered."""

        self._fill_input(
            "试卷名称",
            self.spec.paper["title"],
            placeholder=PAPER_TITLE_PLACEHOLDER,
        )

    def _open_select(self, title: str) -> Any:
        component = self._field_component(title)
        if component is None:
            raise PlatformInputUiError(f"页面上没有“{title}”下拉框")
        select = self._first_visible(component.locator(".el-select"))
        target = select or component
        try:
            target.click(timeout=self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(f"打开“{title}”下拉框失败: {exc}") from exc
        return component

    def _search_select(self, component: Any, value: str) -> None:
        """在页面下拉框内搜索显示值，避免依赖完整选项列表的即时加载。"""

        search = self._first_visible(
            component.locator(
                'input.el-select__input:visible, '
                'input[role="combobox"]:visible'
            )
        )
        if search is None:
            return
        try:
            search.fill(value)
        except Exception as exc:
            raise PlatformInputUiError(f"搜索下拉选项“{value}”失败: {exc}") from exc
        # The caller immediately polls for the exact option.  A fixed sleep
        # here charged every dropdown even when Element Plus rendered the
        # filtered list synchronously, which accumulated noticeably on the
        # Windows driver transport.

    def _find_dropdown_option(self, name: str) -> Any | None:
        wanted = _normalise_text(name)
        selectors = (
            ".el-select-dropdown:visible .el-select-dropdown__item:visible",
            '[role="option"]:visible',
        )
        for selector in selectors:
            options = self.page.locator(selector)
            # Fast path: one round-trip collects every option text, then the
            # exact same Python normalisation decides the match.  The winner
            # is re-read through the engine before returning, so a stale
            # snapshot can never return an option that no longer matches.
            batch_texts = getattr(options, "evaluate_all", None)
            if callable(batch_texts):
                try:
                    texts = batch_texts(_DROPDOWN_TEXTS_JS)
                except Exception:
                    texts = None
                if isinstance(texts, list):
                    matched = False
                    for index, text in enumerate(texts):
                        if _normalise_text(text) != wanted:
                            continue
                        matched = True
                        option = options.nth(index)
                        try:
                            if _normalise_text(option.inner_text()) == wanted:
                                return option
                        except Exception:
                            continue
                    if matched or texts:
                        continue
            try:
                count = options.count()
            except Exception:
                count = 0
            for index in range(count):
                try:
                    option = options.nth(index)
                    if _normalise_text(option.inner_text()) == wanted:
                        return option
                except Exception:
                    continue
        return None

    @staticmethod
    def _selected_multi_count(component: Any) -> int:
        """读取 Element Plus 多选框的已选数量，包含折叠的“+ N”标签。"""

        if component is None:
            return 0
        selectors = (
            ".el-select__selected-item .el-select__tags-text:visible",
            ".el-select__tags-text:visible",
            ".el-tag__content:visible",
        )
        labels: list[str] = []
        for selector in selectors:
            try:
                candidate = component.locator(selector).all_inner_texts()
            except Exception:
                continue
            if candidate:
                labels = [str(value) for value in candidate]
                break
        if not labels:
            return 0

        count = 0
        for label in labels:
            text = str(label).strip()
            collapsed = re.fullmatch(r"\+\s*(\d+)", text)
            count += int(collapsed.group(1)) if collapsed else bool(text)
        return count

    @staticmethod
    def _selected_multi_labels(component: Any) -> tuple[set[str], int, bool]:
        """读取多选框标签；第三项表示是否没有被“+ N”折叠。"""

        if component is None:
            return set(), 0, True
        selectors = (
            ".el-select__selected-item .el-select__tags-text:visible",
            ".el-select__tags-text:visible",
            ".el-tag__content:visible",
        )
        labels: list[str] = []
        for selector in selectors:
            try:
                candidate = component.locator(selector).all_inner_texts()
            except Exception:
                continue
            if candidate:
                labels = [str(value) for value in candidate]
                break
        selected: set[str] = set()
        count = 0
        complete = True
        for label in labels:
            text = str(label).strip()
            collapsed = re.fullmatch(r"\+\s*(\d+)", text)
            if collapsed:
                count += int(collapsed.group(1))
                complete = False
                continue
            normalized = _normalise_text(text).casefold()
            if normalized:
                selected.add(normalized)
                count += 1
        return selected, count, complete

    @staticmethod
    def _option_is_selected(option: Any) -> bool:
        """读取下拉选项的真实选中态，避免重复点击导致反选。"""

        if option is None:
            return False
        try:
            if str(option.get_attribute("aria-selected") or "").lower() == "true":
                return True
            class_tokens = set(
                str(option.get_attribute("class") or "").split()
            )
            if class_tokens.intersection({"is-selected", "selected", "is-checked"}):
                return True
            return option.locator(
                ".el-checkbox.is-checked, .el-checkbox__input.is-checked"
            ).count() > 0
        except Exception:
            return False

    def _multi_selection_matches(
        self,
        title: str,
        wanted_names: Sequence[str],
    ) -> bool:
        component = self._field_component(title)
        if component is None:
            return False
        labels, count, complete = self._selected_multi_labels(component)
        wanted = {
            _normalise_text(name).casefold()
            for name in wanted_names
            if _normalise_text(name)
        }
        if count != len(wanted):
            return False
        # Element Plus may collapse the visible tags into “+ N”。此时数量仍
        # 可验证，但标签集合不可见；等所有选项都展开时再做集合校验。
        return not complete or labels == wanted

    def _clear_multi_selection(self, title: str) -> None:
        """通过页面清除控件把多选值归零，供重试时做精确同步。"""

        component = self._field_component(title)
        if component is None:
            raise PlatformInputUiError(f"页面上没有“{title}”多选框")
        if self._selected_multi_count(component) == 0:
            return

        clear_selectors = (
            ".el-select__clear:visible",
            ".el-icon-circle-close:visible",
            '[aria-label="清除"]:visible',
            '[aria-label="Clear"]:visible',
        )
        for selector in clear_selectors:
            try:
                clear = self._first_visible(component.locator(selector))
            except Exception:
                clear = None
            if clear is None:
                continue
            try:
                clear.click(timeout=self.action_timeout_ms)
            except Exception:
                continue
            try:
                self._wait_until(
                    lambda: self._selected_multi_count(
                        self._field_component(title)
                    ) == 0,
                    f"清空“{title}”多选框后页面仍有旧选项",
                    timeout_seconds=10,
                    interval_ms=100,
                )
                return
            except PlatformInputUiError:
                # 某些 Element Plus 版本会同时把搜索框清除图标和总清除
                # 图标挂在同一组件内；点击前者没有改变选中标签时，继续
                # 尝试逐标签删除，而不是把一次误命中当成成功。
                pass

        # 没有总清除图标时，Element Plus 会在每个标签上提供关闭按钮。
        # 从末尾删除并逐次回读，避免一次点击只改变视觉而没有更新状态。
        max_attempts = max(4, self._selected_multi_count(component) * 2 + 2)
        for _ in range(max_attempts):
            current = self._field_component(title)
            if self._selected_multi_count(current) == 0:
                return
            if current is None:
                break
            delete = None
            for selector in (
                ".el-tag__close:visible",
                ".el-select__selected-item .el-icon-close:visible",
                '[aria-label="关闭"]:visible',
                '[aria-label="Close"]:visible',
            ):
                delete = self._first_visible(current.locator(selector))
                if delete is not None:
                    break
            if delete is None:
                break
            before = self._selected_multi_count(current)
            try:
                delete.click(timeout=self.action_timeout_ms)
            except Exception as exc:
                raise PlatformInputUiError(f"清除“{title}”旧选项失败: {exc}") from exc
            self._wait_until(
                lambda: self._selected_multi_count(
                    self._field_component(title)
                ) < before,
                f"清除“{title}”旧选项后页面没有更新",
                timeout_seconds=10,
                interval_ms=100,
            )
        if self._selected_multi_count(self._field_component(title)) != 0:
            raise PlatformInputUiError(
                f"无法通过页面控件清空“{title}”已有选项；为避免重试反选，本次停止"
            )

    def _select_one(self, title: str, choice: Mapping[str, Any]) -> None:
        name = _require_nonempty_text(choice.get("name"), f"{title}.name")
        # 编辑既有试卷时，第一步通常已经带回当前选中值。先读页面显示值，
        # 相同就复用，避免无意义地再次打开下拉层触发异步选项加载。
        current = self._field_component(title)
        if current is not None:
            try:
                if _normalise_text(name) in _normalise_text(current.inner_text()):
                    return
            except Exception:
                pass
        component = self._open_select(title)
        self._search_select(component, name)
        # Element Plus 的下拉层先挂载、后异步渲染选项；不能在 click 后
        # 立即读取，否则会把尚未出现的合法选项误判为不存在。
        self._wait_until(
            lambda: self._find_dropdown_option(name) is not None,
            f"等待“{title}”下拉选项“{name}”超时",
            timeout_seconds=10,
            interval_ms=100,
        )
        option = self._find_dropdown_option(name)
        if option is None:
            raise PlatformInputUiError(f"“{title}”下拉框中没有选项“{name}”")
        try:
            option.click(timeout=self.action_timeout_ms)
        except Exception as exc:
            # Element Plus 的 teleport 下拉层在表单布局变化时可能持续触发
            # “element is not stable”。先确认是否已经选中；如果没有，
            # 用页面控件对应的 force click 完成同一个用户点击动作。
            try:
                current = self._field_component(title)
                if current is not None and _normalise_text(name) in _normalise_text(
                    current.inner_text()
                ):
                    return
                option.click(force=True, timeout=self.action_timeout_ms)
            except Exception as force_exc:
                raise PlatformInputUiError(
                    f"选择“{title}={name}”失败: {force_exc}"
                ) from exc

        def selected() -> bool:
            component = self._field_component(title)
            return component is not None and _normalise_text(name) in _normalise_text(
                component.inner_text()
            )

        self._wait_until(
            selected,
            f"选择“{title}={name}”后页面没有显示已选值",
            timeout_seconds=10,
        )

    def _select_many(self, title: str, choices: Sequence[Mapping[str, Any]]) -> None:
        wanted_names: list[str] = []
        seen: set[str] = set()
        for choice in choices:
            name = _require_nonempty_text(choice.get("name"), f"{title}.name")
            key = _normalise_text(name).casefold()
            if key in seen:
                raise PlatformInputUiError(f"“{title}”输入包含重复选项“{name}”")
            seen.add(key)
            wanted_names.append(name)

        if self._multi_selection_matches(title, wanted_names):
            self.page.keyboard.press("Escape")
            return
        if self._selected_multi_count(self._field_component(title)):
            self._clear_multi_selection(title)

        # Element Plus 多选框在某些版本点击一个选项后会关闭下拉层，因此每
        # 个值都重新打开；这是页面点击，不是批量构造请求。
        for name in wanted_names:
            dropdown = self._first_visible(
                self.page.locator(".el-select-dropdown:visible")
            )
            component = (
                self._field_component(title)
                if dropdown is not None
                else self._open_select(title)
            )
            if component is not None:
                self._search_select(component, name)
            self._wait_until(
                lambda: self._find_dropdown_option(name) is not None,
                f"等待“{title}”下拉选项“{name}”超时",
                timeout_seconds=10,
                interval_ms=100,
            )
            option = self._find_dropdown_option(name)
            if option is None:
                raise PlatformInputUiError(f"“{title}”下拉框中没有选项“{name}”")
            before = self._selected_multi_count(self._field_component(title))
            if self._option_is_selected(option):
                # 该值可能是页面回填的既有选择；重复点击会把它取消。
                self.page.keyboard.press("Escape")
                continue
            try:
                option.click(timeout=self.action_timeout_ms)
            except Exception as exc:
                try:
                    option.click(force=True, timeout=self.action_timeout_ms)
                except Exception as force_exc:
                    raise PlatformInputUiError(
                        f"选择“{title}={name}”失败: {force_exc}"
                    ) from exc

            def selected() -> bool:
                option = self._find_dropdown_option(name)
                if self._option_is_selected(option):
                    return True
                current = self._field_component(title)
                if current is None:
                    return False
                labels, _count, complete = self._selected_multi_labels(current)
                if _normalise_text(name).casefold() in labels:
                    return True
                # If the UI collapses labels, only the count is available. It
                # must increase relative to the state immediately before this
                # click, not relative to a stale count from a previous run.
                return self._selected_multi_count(current) > before

            self._wait_until(
                selected,
                f"选择“{title}={name}”后页面没有显示已选值",
                timeout_seconds=10,
            )
        self.page.keyboard.press("Escape")
        self._wait_until(
            lambda: self._multi_selection_matches(title, wanted_names),
            f"填写“{title}”后页面选中值与输入不一致",
            timeout_seconds=10,
            interval_ms=100,
        )

    def fill_base_form(self) -> None:
        paper = self.spec.paper
        # 切换试卷分类会重建后续表单并清空已经填写的名称，因此分类
        # 必须先选；之后再按页面顺序填写名称和地区等字段。
        if self.existing_paper_id:
            current_category = self._field_component("试卷分类")
            if current_category is not None:
                try:
                    current_text = _normalise_text(current_category.inner_text())
                except Exception:
                    current_text = ""
                wanted_category = _normalise_text(self.spec.paper_category)
                if current_text and wanted_category not in current_text:
                    raise PlatformInputExistingPaperIncompatibleError(
                        "既有试卷的分类与本次录入目标不兼容："
                        f"当前为“{current_text}”，目标为“{self.spec.paper_category}”；"
                        "平台编辑页不能把题型专项试卷转换为听说考试试卷"
                    )
        self._select_one(
            "试卷分类",
            {"name": self.spec.paper_category},
        )
        self._select_one("省份", paper["province"])
        self._select_one("城市", paper["city"])
        self._select_many("区/县", paper["districts"])
        self._select_one("学段", paper["stage"])
        self._select_one("年级", paper["grade"])
        if self.spec.paper_category == "听说考试":
            self._select_one("试卷类型", paper["paper_type"])
        if paper.get("year") is not None:
            self._fill_input("年份", paper["year"])
        if paper.get("duration") is not None:
            self._fill_input("大约答题时长（分钟）", paper["duration"])
        # Province/city/district dependencies can rebuild the form after the
        # category is selected. Fill the title last so the final visible
        # base-form state contains the exact configured name.
        self.ensure_paper_title()

    def _search_template_if_needed(self) -> None:
        search = self._first_visible(
            self.page.locator('input[placeholder="搜索题型模板..."]:visible')
        )
        if search is None:
            search = self._first_visible(
                self.page.locator('input[placeholder="搜索试卷模板..."]:visible')
            )
        if search is None:
            return
        try:
            search.fill(self.spec.template_name)
            wrapper = search.locator(
                "xpath=ancestor::*[contains(@class,'inputBack')][1]"
            )
            icon = self._first_visible(wrapper.locator("img.cursor-pointer"))
            if icon is not None:
                icon.click(timeout=self.action_timeout_ms)
            else:
                search.press("Enter")
        except Exception:
            # 初始列表已经有目标卡片时，搜索只是优化定位，失败不直接
            # 中断；后续仍会按卡片名称查找。
            return

    def select_template(self) -> None:
        target = _normalise_text(self.spec.template_name)

        def find_card() -> Any | None:
            cards = self.page.locator(".cardContent:visible")
            try:
                count = cards.count()
            except Exception:
                count = 0
            for index in range(count):
                try:
                    card = cards.nth(index)
                    name = self._first_visible(card.locator(".nameFont"))
                    if name is not None and _normalise_text(name.inner_text()) == target:
                        return card
                except Exception:
                    continue
            return None

        # 模板列表也按页面搜索框定位；即使目标卡片当前已经在首屏，
        # 也先输入搜索词，避免依赖模板列表是否一次性加载完整。
        self._search_template_if_needed()
        card = find_card()
        if card is None:
            self._wait_until(
                lambda: find_card() is not None,
                f"没有找到题型模板“{self.spec.template_name}”",
                timeout_seconds=20,
            )
            card = find_card()
        if card is None:
            raise PlatformInputUiError(f"没有找到题型模板“{self.spec.template_name}”")

        def card_is_selected() -> bool:
            """Read the page's actual radio/card selection state."""

            def compact_state(value: Any) -> str:
                # The platform has emitted both ``is-selected`` and
                # ``isSelected`` over time.  Comparing compact tokens handles
                # both forms without depending on a particular CSS naming
                # convention.
                return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

            selected_tokens = {
                "selected",
                "isselected",
                "active",
                "isactive",
                "checked",
                "ischecked",
                "chosen",
                "ischosen",
                "choose",
                "ischoose",
                "current",
                "iscurrent",
            }

            def class_has_selected_state(value: Any) -> bool:
                return any(
                    compact_state(token) in selected_tokens
                    for token in str(value or "").split()
                )

            def attribute_has_selected_state(node: Any) -> bool:
                for attribute in (
                    "aria-selected",
                    "aria-checked",
                    "data-selected",
                    "data-checked",
                    "data-active",
                    "data-state",
                    "selected",
                    "checked",
                ):
                    try:
                        value = node.get_attribute(attribute)
                    except Exception:
                        continue
                    if value is None:
                        continue
                    if compact_state(value) in {
                        "true",
                        "1",
                        "yes",
                        "on",
                        "selected",
                        "checked",
                        "active",
                        "chosen",
                        "choose",
                    }:
                        return True
                    # Boolean HTML attributes are selected merely by being
                    # present, even when the driver returns an empty string.
                    if attribute in {"selected", "checked"} and value == "":
                        return True
                return False

            try:
                card_classes = card.get_attribute("class")
            except Exception:
                card_classes = ""
            if class_has_selected_state(card_classes) or attribute_has_selected_state(card):
                return True

            # A few builds put the state on a descendant wrapper rather than
            # the card itself.  Check the common semantic markers directly so
            # an ``isSelected`` class is not mistaken for an unavailable
            # template.
            for selector in (
                '[aria-selected="true"]',
                '[aria-checked="true"]',
                '[data-selected="true"]',
                '[data-checked="true"]',
                '[data-active="true"]',
                ".selected",
                ".is-selected",
                ".isSelected",
                ".checked",
                ".is-checked",
                ".isChecked",
                ".active",
                ".is-active",
                ".isActive",
            ):
                try:
                    marked = self._first_visible(card.locator(selector))
                except Exception:
                    marked = None
                if marked is not None:
                    return True

            # Element Plus radios expose both a checked input and an
            # ``is-checked`` marker.  The platform has used both shapes over
            # time, so inspect the semantic state instead of relying on one
            # CSS class or on the outer card click event.
            selectors = (
                'input[type="radio"]',
                'input[type="checkbox"]',
                '[role="radio"]',
                ".el-radio",
                ".el-radio__input",
                ".el-radio__inner",
            )
            for selector in selectors:
                controls = card.locator(selector)
                try:
                    count = controls.count()
                except Exception:
                    count = 0
                for index in range(count):
                    control = controls.nth(index)
                    try:
                        is_checked = getattr(control, "is_checked", None)
                        if callable(is_checked) and is_checked():
                            return True
                    except Exception:
                        pass
                    try:
                        if str(control.get_attribute("aria-checked") or "").lower() == "true":
                            return True
                        if str(control.get_attribute("checked") or "").lower() in {"", "true", "checked"} and selector.startswith("input"):
                            # A checked boolean property is preferred above;
                            # this attribute fallback covers lightweight page
                            # drivers that do not expose ``is_checked``.
                            if control.get_attribute("checked") is not None:
                                return True
                        if class_has_selected_state(control.get_attribute("class")):
                            return True
                        if attribute_has_selected_state(control):
                            return True
                    except Exception:
                        continue

            # The first-step button is enabled only after the form has a
            # valid template choice.  It is a reliable page-level fallback
            # for platform builds whose visual blue selection is not exposed
            # through a stable class/ARIA marker.  This page is not
            # consistently rendered as a native ``button``: some builds use
            # a clickable div/span, so checking only get_by_role("button")
            # falsely reports the visibly-blue button as unavailable.
            def control_is_disabled(node: Any) -> bool:
                try:
                    is_enabled = getattr(node, "is_enabled", None)
                    if callable(is_enabled) and not is_enabled():
                        return True
                except Exception:
                    pass
                for attribute in ("disabled", "aria-disabled", "data-disabled"):
                    try:
                        value = node.get_attribute(attribute)
                    except Exception:
                        value = None
                    if value is None:
                        continue
                    if attribute == "disabled" or compact_state(value) in {
                        "true", "1", "yes", "on", "disabled",
                    }:
                        return True
                try:
                    classes = compact_state(node.get_attribute("class"))
                    if any(token in classes for token in ("disabled", "disable")):
                        return True
                    style = str(node.get_attribute("style") or "").replace(" ", "").casefold()
                    if "pointer-events:none" in style or "opacity:0" in style:
                        return True
                except Exception:
                    pass
                return False

            def visible_next_control_ready() -> bool:
                candidates: list[Any] = []
                for locator_factory in (
                    lambda: self.page.get_by_role(
                        "button", name="下一步：录入试题内容", exact=True
                    ),
                    lambda: self.page.locator("button:visible").filter(
                        has_text="下一步：录入试题内容"
                    ),
                    lambda: self.page.get_by_text(
                        "下一步：录入试题内容", exact=True
                    ),
                ):
                    try:
                        locator = locator_factory()
                        count = locator.count()
                    except Exception:
                        continue
                    for index in range(count):
                        try:
                            node = locator.nth(index)
                            if not node.is_visible():
                                continue
                            # Walk from the text node to the Vue click target;
                            # disabled state is often placed on the wrapper.
                            current = node
                            disabled = False
                            for _ in range(6):
                                if control_is_disabled(current):
                                    disabled = True
                                    break
                                parent = current.locator("xpath=..")
                                try:
                                    if parent.count() == 0:
                                        break
                                except Exception:
                                    break
                                current = parent
                            if not disabled:
                                return True
                        except Exception:
                            continue
                return False

            if visible_next_control_ready():
                return True
            return False

        def click_template_control() -> None:
            # The visible radio/label is the platform's actual selection
            # control.  Clicking only ``.cardContent`` can focus the card
            # without updating Vue's selected-template state, which leaves
            # the first-step page looking unchanged (the reported failure).
            control = None
            for selector in (
                ".el-radio__inner:visible",
                ".el-radio__input:visible",
                '[role="radio"]:visible',
                'input[type="radio"]:visible',
                "label:visible",
            ):
                control = self._first_visible(card.locator(selector))
                if control is not None:
                    break
            if control is None:
                inputs = card.locator('input[type="radio"]')
                try:
                    if inputs.count() > 0:
                        control = inputs.first
                except Exception:
                    control = None
            control = control or card
            control.scroll_into_view_if_needed()
            check = getattr(control, "check", None)
            if callable(check):
                try:
                    check(force=True, timeout=self.action_timeout_ms)
                    return
                except Exception:
                    # Some page versions expose a styled radio wrapper rather
                    # than a checkable input; fall through to the normal UI
                    # click for the same visible control.
                    pass
            try:
                control.click(timeout=self.action_timeout_ms)
            except Exception as exc:
                try:
                    control.click(force=True, timeout=self.action_timeout_ms)
                except Exception as force_exc:
                    raise PlatformInputUiError(
                        f"选择题型模板“{self.spec.template_name}”失败: {force_exc}"
                    ) from exc

        try:
            click_template_control()
        except Exception as exc:
            if isinstance(exc, PlatformInputUiError):
                raise
            raise PlatformInputUiError(
                f"选择题型模板“{self.spec.template_name}”失败: {exc}"
            ) from exc

        self._wait_until(
            card_is_selected,
            f"选择题型模板“{self.spec.template_name}”后页面没有显示已选状态",
            timeout_seconds=10,
            interval_ms=100,
        )

    def next_to_content(self) -> None:
        # Selecting the template or a dependent dropdown may recreate the
        # first-step form. Re-assert the configured title immediately before
        # the durable page transition so a late Vue render cannot leave the
        # external “试卷名称” field blank.
        self.ensure_paper_title()
        self._click_exact("下一步：录入试题内容")
        self._wait_until(
            lambda: self._has_visible_text("第二步：录入试卷内容"),
            "页面没有进入第二步录入试题内容",
            timeout_seconds=60,
        )
        # 第二步 mounted 时会由页面自己发起只读 content/get；这里只等
        # 页面编辑器出现，不主动补发读取请求。
        self._wait_until(
            lambda: self.page.locator(
                '.rich-text-editor .editor-content[contenteditable="true"]'
            ).count()
            > 0,
            "第二步页面没有出现可编辑内容",
            timeout_seconds=60,
        )
