"""Navigation and lifecycle actions for the visible input page."""

from __future__ import annotations

from .page_shared import *  # noqa: F403,F401


# One-shot visibility probe: instead of paying one protocol round-trip per
# candidate (count + nth + is_visible), evaluate_all resolves every match
# inside the browser and returns the first plausibly visible index.  The
# Python side still verifies the winner with the real Playwright
# ``is_visible`` so the effective semantics cannot drift from the
# per-candidate loop on engines whose visibility rules differ.
_FIRST_VISIBLE_INDEX_JS = """
(nodes) => {
    for (let index = 0; index < nodes.length; index += 1) {
        const node = nodes[index];
        if (!node || typeof node.getBoundingClientRect !== 'function') {
            continue;
        }
        const rect = node.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) {
            continue;
        }
        if (window.getComputedStyle(node).visibility === 'hidden') {
            continue;
        }
        return index;
    }
    return -1;
}
"""


class PlatformInputNavigationMixin:
    """Open new/existing papers and activate lazy-loaded page sections."""

    def __init__(
        self,
        page: Any,
        spec: PlatformInputSpec,
        observer: ReadOnlyFeedbackObserver,
        *,
        action_timeout_ms: int = 15_000,
        existing_paper_id: str | None = None,
    ) -> None:
        self.page = page
        self.spec = spec
        self.observer = observer
        self.action_timeout_ms = max(1_000, int(action_timeout_ms))
        self.existing_paper_id = (
            str(existing_paper_id).strip()
            if existing_paper_id not in (None, "")
            else None
        )

        # A few page mixins intentionally use short, user-like locator calls
        # without repeating ``timeout=...`` on every keyboard action.  The
        # Playwright default is otherwise 30 seconds per call, and a stalled
        # renderer/driver can leave one such action waiting forever.  Install
        # the same bounded timeout on the real page once at construction time;
        # lightweight test page shims may not expose these methods, so this is
        # best-effort configuration rather than a page capability requirement.
        set_default_timeout = getattr(self.page, "set_default_timeout", None)
        if callable(set_default_timeout):
            try:
                set_default_timeout(self.action_timeout_ms)
            except Exception:
                pass
        set_default_navigation_timeout = getattr(
            self.page,
            "set_default_navigation_timeout",
            None,
        )
        if callable(set_default_navigation_timeout):
            try:
                set_default_navigation_timeout(
                    max(self.action_timeout_ms, 30_000)
                )
            except Exception:
                pass

    def _first_visible(self, locator: Any) -> Any | None:
        # Fast path: resolve the first visible candidate in a single
        # round-trip, then confirm it with the real ``is_visible``.  Test
        # shims and engines without ``evaluate_all`` simply fall through to
        # the original per-candidate loop below.
        batch_probe = getattr(locator, "evaluate_all", None)
        start_index = 0
        if callable(batch_probe):
            try:
                found = int(batch_probe(_FIRST_VISIBLE_INDEX_JS))
            except Exception:
                found = -1
            if found >= 0:
                try:
                    candidate = locator.nth(found)
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    pass
                # The in-browser approximation disagreed with the engine, so
                # it cannot be trusted to have skipped only invisible earlier
                # candidates; rescan from the very first element to keep the
                # returned node identical to the plain loop's choice.
                start_index = 0
        try:
            count = locator.count()
        except Exception:
            return None
        for index in range(start_index, count):
            try:
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    def _has_visible_text(self, text: str, scope: Any | None = None) -> bool:
        owner = scope or self.page
        return self._first_visible(owner.get_by_text(text, exact=True)) is not None

    def _control_checkpoint(self) -> None:
        """Run the durable stop/pause check when a page wait is polling.

        ``run_page_input`` still checks at page-action boundaries, but a
        visible page can spend several seconds waiting for a lazy-loaded
        card, route, or editor.  The executor installs this callback on the
        automation object for controlled runs; keeping the hook here makes
        every shared wait responsive without interrupting a Playwright click
        that is already in flight.
        """

        check = getattr(self, "_control_check", None)
        if callable(check):
            check()

    def _is_paper_list(self) -> bool:
        """区分试卷列表和新增/编辑页的同名“新增试卷”标题。"""

        return self._has_visible_text("新增试卷") and self._has_visible_text("导出")

    def _click_exact(
        self,
        text: str,
        *,
        scope: Any | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        owner = scope or self.page
        # Prefer the actual button node.  On the exam page the visible label
        # is rendered by a nested span; clicking that span usually bubbles,
        # but some Vue handlers are attached to the button component itself.
        # Keep the exact-text fallback for page versions that do not expose a
        # button role (and for the lightweight test drivers).
        candidate = None
        try:
            candidate = self._first_visible(
                owner.get_by_role("button", name=text, exact=True)
            )
        except Exception:
            candidate = None
        if candidate is None:
            try:
                candidate = self._first_visible(
                    owner.locator("button:visible").filter(has_text=text)
                )
            except Exception:
                candidate = None
        if candidate is None:
            candidate = self._first_visible(owner.get_by_text(text, exact=True))
        if candidate is None:
            raise PlatformInputUiError(f"页面上没有可点击的“{text}”")
        try:
            candidate.click(timeout=timeout_ms or self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(f"点击“{text}”失败: {exc}") from exc

    def _wait_until(
        self,
        predicate: Any,
        message: str,
        *,
        timeout_seconds: float = 30,
        interval_ms: int = 200,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            # Keep this outside the predicate's broad defensive catch: a
            # durable stop is a control signal, not a transient DOM error.
            self._control_checkpoint()
            try:
                if predicate():
                    return
            except Exception:
                pass
            self._control_checkpoint()
            self.page.wait_for_timeout(interval_ms)
        self._control_checkpoint()
        raise PlatformInputUiError(message)

    def wait_until_ready(self, admin_url: str, timeout_seconds: float) -> None:
        try:
            self.page.goto(admin_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            if not getattr(self.page, "url", ""):
                raise PlatformInputLoginError(f"无法打开外部平台管理页面: {exc}") from exc

        # 登录成功后，管理端经常先把当前标签重定向到首页；如果只等
        # “新增试卷”，就会一直卡在首页。通过页面路由把当前标签带回试卷
        # 管理页，仍然是浏览器页面导航，不构造任何接口请求。
        deadline = time.monotonic() + timeout_seconds
        last_url = ""
        last_route_attempt = 0.0
        while time.monotonic() < deadline:
            self._control_checkpoint()
            if self._has_visible_text("新增试卷"):
                return

            current_url = str(getattr(self.page, "url", "") or "")
            if current_url != last_url:
                last_url = current_url
                last_route_attempt = 0.0
            if (
                current_url
                and "#/resource/exam" not in current_url
                and time.monotonic() - last_route_attempt >= 1.0
            ):
                last_route_attempt = time.monotonic()
                try:
                    self._open_paper_list_from_sidebar()
                except Exception:
                    # 登录页尚未完成或菜单正在切换时可能暂时不能点击；
                    # 下一轮会继续通过页面菜单尝试。
                    pass
            self.page.wait_for_timeout(200)

        raise PlatformInputLoginError(
            "未进入外部平台试卷管理页；本次等待已结束，请重新发起录入并在打开的 Chrome 窗口完成登录"
        )

    def wait_until_preflight_phase(self, phase: str, timeout_seconds: float = 30) -> None:
        """确认预检留下的当前页面仍在可复用的阶段。

        预检和正式执行共享同一个持久化浏览器会话。预检已经通过页面
        控件打开了既有试卷（或选好了新试卷模板），正式执行不能再次
        ``goto`` 管理列表，否则会把已打开的编辑页丢掉并重新等待列表
        控件。这里只等待页面上的阶段标题，不做任何导航或写操作。
        """

        phase_text = {
            "existing_content_ready": "第二步：录入试卷内容",
            "new_paper_template_selected": "第一步：配置基础属性与题型",
        }.get(str(phase or "").strip())
        if not phase_text:
            raise PlatformInputUiError(f"无法复用未知的预检页面阶段：{phase}")
        self._wait_until(
            lambda: self._has_visible_text(phase_text),
            f"预检后的页面没有停留在“{phase_text}”",
            timeout_seconds=timeout_seconds,
        )

    def _open_paper_list_from_sidebar(self) -> bool:
        """通过可见侧栏菜单回到试卷列表，返回是否执行了点击。"""

        menu = self._first_visible(
            self.page.get_by_text("试卷管理", exact=True)
        )
        if menu is not None:
            menu.click(timeout=self.action_timeout_ms)
            return True

        # 首页默认只展开到“资源管理”；先点击它展开子菜单，下一轮再点
        # “试卷管理”。整个恢复动作仍然走页面控件。
        resource = self._first_visible(
            self.page.get_by_text("资源管理", exact=True)
        )
        if resource is not None:
            resource.click(timeout=self.action_timeout_ms)
            return True
        return False

    def _group_first_question_number(self, group_type: str) -> int | None:
        """Return the first global outline number for one normalized group."""

        number = 1
        for group in self.spec.groups:
            current_type = str(group.get("type") or "")
            if current_type == group_type:
                return number

            if current_type == "听后选择":
                materials = group.get("materials") or ()
                number += sum(
                    len(material.get("questions") or ())
                    for material in materials
                    if isinstance(material, Mapping)
                )
            elif current_type in {"听后应答", "模仿朗读"}:
                number += len(group.get("questions") or ())
            elif current_type == "信息获取":
                number += sum(
                    len(material.get("questions") or ())
                    for material in group.get("materials") or ()
                    if isinstance(material, Mapping)
                )
            elif current_type == "信息转述及询问":
                number += (1 if group.get("retelling") else 0)
                number += len(group.get("asking") or ())
            elif current_type == "听后记录并转述信息":
                recording = group.get("recording") or {}
                number += len(recording.get("questions") or ())
                if group.get("retelling"):
                    number += 1
        return None

    def search_paper_list(self) -> None:
        """Run a visible list search for the configured paper title.

        Read-only verification uses the same visible search controls as a
        manual user: fill the title into the list search box and press 查询.
        No row is clicked and no form is opened, so the platform receives
        nothing but the ordinary list-query requests the page itself issues.
        """

        title = self.spec.paper["title"]
        search = self._first_visible(
            self.page.locator('input[placeholder="请输入试卷名称"]:visible')
        )
        if search is None:
            raise PlatformInputUiError("试卷列表没有找到搜索输入框，无法执行只读核验")
        try:
            search.fill(title)
        except Exception as exc:
            raise PlatformInputUiError(f"填写试卷搜索条件失败: {exc}") from exc
        self._click_exact("查询")
        # Callers wait for the fresh list response/row explicitly; a fixed
        # delay here only serialised that existing readiness wait.

    def start_new_paper(self) -> None:
        self._click_exact("新增试卷")
        self._wait_until(
            lambda: self._has_visible_text("第一步：配置基础属性与题型"),
            "点击“新增试卷”后没有进入第一步配置页面",
        )

    def start_existing_paper(self) -> str:
        """从列表打开同名且可编辑的既有试卷。

        重试录入时必须沿用已经创建的试卷。列表中可能同时存在已发布/已
        下架的记录，因此只选择包含“编辑”操作的那一行；找不到可编辑记录
        时直接失败，绝不退回“新增试卷”流程。
        """

        title = self.spec.paper["title"]

        def edit_from_scope(scope: Any) -> Any | None:
            try:
                return self._first_visible(scope.get_by_text("编辑", exact=True))
            except Exception:
                return None

        def find_edit_action_by_id() -> Any | None:
            wanted_id = self.existing_paper_id
            if not wanted_id:
                return None

            # Prefer stable row/data attributes when the page exposes them.
            # Values are compared in Python instead of interpolated into a CSS
            # selector, so an unexpected external ID cannot alter a locator.
            candidates = self.page.locator(
                "[data-paper-id], [data-paperid], [data-id], "
                "[data-row-key], [data-key]"
            )
            try:
                candidate_count = candidates.count()
            except Exception:
                candidate_count = 0
            for candidate_index in range(candidate_count):
                try:
                    candidate = candidates.nth(candidate_index)
                    if not candidate.is_visible():
                        continue
                    matched = any(
                        str(candidate.get_attribute(attribute) or "").strip() == wanted_id
                        for attribute in (
                            "data-paper-id",
                            "data-paperid",
                            "data-id",
                            "data-row-key",
                            "data-key",
                        )
                    )
                    if not matched:
                        continue
                    scope = candidate
                    for _level in range(8):
                        edit = edit_from_scope(scope)
                        if edit is not None:
                            return edit
                        scope = scope.locator("xpath=..")
                except Exception:
                    continue

            # Some table implementations render the ID as a visible cell but
            # do not add a data attribute.  It is still safe to use only an
            # exact visible ID match, never a substring match.
            try:
                id_nodes = self.page.get_by_text(wanted_id, exact=True)
                id_count = id_nodes.count()
            except Exception:
                id_count = 0
            for id_index in range(id_count):
                try:
                    node = id_nodes.nth(id_index)
                    if not node.is_visible():
                        continue
                    scope = node
                    for _level in range(8):
                        edit = edit_from_scope(scope)
                        if edit is not None:
                            return edit
                        scope = scope.locator("xpath=..")
                except Exception:
                    continue
            return None

        def find_edit_action() -> Any | None:
            exact_id_action = find_edit_action_by_id()
            if self.existing_paper_id:
                if exact_id_action is not None:
                    return exact_id_action
                # Some versions of the platform table expose the record ID
                # only in the read-only list response, not as a DOM/data
                # attribute. The observer already matched that response by
                # exact title and captured its ID. In that narrow case allow
                # a title-row fallback only when the title is unique in the
                # visible table; duplicate titles remain fail-closed.
                observed_id = str(getattr(self.observer, "paper_id", "") or "").strip()
                if observed_id != str(self.existing_paper_id).strip():
                    return None
                titles = self.page.get_by_text(title, exact=True)
                try:
                    title_count = titles.count()
                except Exception:
                    title_count = 0
                if title_count == 0:
                    return None
                try:
                    # The platform table does not expose the paper id in the
                    # DOM, while the read-only list response does.  When
                    # duplicate titles exist, use the response order captured
                    # by the observer to select the exact row instead of
                    # failing closed merely because the title is repeated.
                    title_index = None
                    for index, match in enumerate(
                        getattr(self.observer, "paper_matches", ()) or ()
                    ):
                        if str(match.get("paperId") or "").strip() == str(
                            self.existing_paper_id
                        ).strip():
                            title_index = index
                            break
                    if title_index is None or title_index >= title_count:
                        return None
                    title_node = titles.nth(title_index)
                    if not title_node.is_visible():
                        return None
                    parent = title_node
                    for _level in range(8):
                        parent = parent.locator("xpath=..")
                        edit = self._first_visible(parent.get_by_text("编辑", exact=True))
                        if edit is not None:
                            return edit
                except Exception:
                    return None
                return None
            titles = self.page.get_by_text(title, exact=True)
            try:
                title_count = titles.count()
            except Exception:
                title_count = 0
            # 列表通常按更新时间倒序；较新的记录可能是之前中断脚本留下
            # 的空结构。优先从同名记录的较早一条开始，尽量复用用户演示
            # 时已经建立的原始试卷。
            for title_index in range(title_count - 1, -1, -1):
                try:
                    title_node = titles.nth(title_index)
                    if not title_node.is_visible():
                        continue
                    parent = title_node
                    for _level in range(8):
                        parent = parent.locator("xpath=..")
                        edit = self._first_visible(
                            parent.get_by_text("编辑", exact=True)
                        )
                        if edit is not None:
                            return edit
                except Exception:
                    continue
            return None

        target_description = (
            f"ID={self.existing_paper_id}"
            if self.existing_paper_id
            else f"名称={title}"
        )

        # The platform list is paginated (10 rows by default).  A retry must
        # not silently inspect only the first page: the original special
        # paper can be several pages back even when its exact ID is known.
        # Search is performed through the visible list controls so the
        # observer can still capture the resulting read-only list response.
        if self.existing_paper_id:
            search = self._first_visible(
                self.page.locator(
                    'input[placeholder="请输入试卷名称"]:visible'
                )
            )
            if search is not None:
                try:
                    search.fill(title)
                except Exception as exc:
                    raise PlatformInputUiError(
                        f"填写既有试卷搜索条件失败: {exc}"
                    ) from exc
                self._click_exact("查询")

        self._wait_until(
            lambda: find_edit_action() is not None,
            f"试卷列表中没有可编辑的既有试卷（{target_description}）；为避免重复创建，本次不新增试卷",
            timeout_seconds=30,
            interval_ms=200,
        )
        edit = find_edit_action()
        if edit is None:
            raise PlatformInputUiError(
                f"试卷列表中没有可编辑的既有试卷（{target_description}）；为避免重复创建，本次不新增试卷"
            )
        try:
            edit.click(timeout=self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(f"打开既有试卷“{title}”失败: {exc}") from exc

        self._wait_until(
            lambda: self._has_visible_text("第一步：配置基础属性与题型")
            or self._has_visible_text("第二步：录入试卷内容"),
            f"打开既有试卷“{title}”后没有进入编辑页面",
            timeout_seconds=60,
        )
        if self._has_visible_text("第二步：录入试卷内容"):
            return "content"
        return "base"

    def _activate_outline_section(
        self,
        group_type: str,
        *,
        outline_target: tuple[str, int] | None = None,
    ) -> None:
        """点击左侧题目导航，让对应题型内容挂载到页面。

        第二步页面按左侧导航懒加载题型区域；未点击的题型虽然出现在
        导航文字里，但其编辑控件不在 DOM 中。每种题型的导航序号来自
        当前完整听说测试模板：选择题有听后选择和听后应答两个同名入口。
        """

        rule = next(
            (candidate for candidate in PAPER_BUNDLE_RULES if candidate.key == self.spec.bundle_rule),
            None,
        )
        target = outline_target or (
            rule.outline_target(group_type)
            if rule is not None
            else _DEFAULT_OUTLINE_TARGETS.get(group_type)
        )
        if target is None:
            raise PlatformInputUiError(f"没有配置题型“{group_type}”的页面导航定位")
        target_text, wanted_occurrence = target

        # In the reviewed full listening-test template, "听后应答" is a
        # choice-card family but its outline entries are rendered as
        # ``第1题(录音题)``.  The first seven recording entries belong to the
        # response prompts; the following recording entry belongs to
        # "模仿朗读".  The generic default target used to describe both
        # families as ``选择题`` and therefore clicked the second selection
        # entry, leaving the response cards unmounted.  Select the recording
        # outline family explicitly and derive the occurrence from the
        # normalized groups.  A standalone imitation paper still has one
        # recording entry, so its occurrence remains zero.
        if outline_target is None and self.spec.paper_category == "听说考试" and group_type in {
            "听后应答",
            "模仿朗读",
        }:
            target_text = "第1题(录音题)"
            if group_type == "听后应答":
                wanted_occurrence = 0
            else:
                wanted_occurrence = sum(
                    len(group.get("questions") or ())
                    for group in self.spec.groups
                    if group.get("type") == "听后应答"
                )

        # The reviewed listening-exam template uses one global question
        # outline.  Prefer the number derived from the normalized groups;
        # retain the registered text/occurrence as a compatibility fallback
        # for older special-paper pages.
        question_kind_match = re.search(
            r"[（(]\s*(选择题|填空题|录音题)\s*[）)]",
            target_text,
        )
        # The 外研旧版套卷 restarts numbering in each section.  In that layout
        # the registered text/occurrence is the reliable selector; deriving a
        # global number would point at a different recording card.
        first_number = (
            self._group_first_question_number(group_type)
            if rule is None or rule.outline_numbering != "local"
            else None
        )
        dynamic_locator = None
        if first_number is not None and question_kind_match is not None:
            question_kind = question_kind_match.group(1)
            dynamic_locator = self.page.get_by_text(
                re.compile(
                    rf"^\s*第\s*{first_number}\s*题?\s*[（(]\s*"
                    rf"{re.escape(question_kind)}\s*[）)]$"
                )
            )

        def normalised_outline_text(node: Any) -> str:
            try:
                return re.sub(r"\s+", "", str(node.inner_text() or ""))
            except Exception:
                return ""

        def question_outline_nodes(kind: str) -> list[Any]:
            """Return visible numbered outline entries for either bracket style."""

            nodes = self.page.get_by_text(
                re.compile(
                    rf"^\s*第\s*\d+\s*题?\s*[（(]\s*"
                    rf"{re.escape(kind)}\s*[）)]\s*$"
                )
            )
            result: list[Any] = []
            try:
                count = nodes.count()
            except Exception:
                count = 0
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    if node.is_visible():
                        result.append(node)
                except Exception:
                    continue
            return result

        def matches_number(node: Any, number: int, kind: str) -> bool:
            value = normalised_outline_text(node)
            return re.fullmatch(
                rf"第{number}题?[（(]{re.escape(kind)}[）)]",
                value,
            ) is not None

        def visible_target() -> Any | None:
            if dynamic_locator is not None:
                dynamic_target = self._first_visible(dynamic_locator)
                if dynamic_target is not None:
                    return dynamic_target

            # Playwright's text locator can miss the dynamic entry when the
            # platform renders the Chinese brackets, inserts an invisible
            # line break, or labels the node as “小题9” instead of “第9题”.
            # Scan the visible numbered entries and compare their normalized
            # text in Python before falling back to the legacy occurrence.
            if question_kind_match is not None and first_number is not None:
                question_kind = question_kind_match.group(1)
                candidates = question_outline_nodes(question_kind)
                for candidate in candidates:
                    if matches_number(candidate, first_number, question_kind):
                        return candidate
                if candidates and 0 <= wanted_occurrence < len(candidates):
                    return candidates[wanted_occurrence]

            # Keep the exact registered target as the final compatibility
            # path for older special-paper pages whose outline has no
            # numbered-question pattern.
            nodes = self.page.get_by_text(target_text, exact=True)
            try:
                count = nodes.count()
            except Exception:
                count = 0
            visible_index = 0
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    if not node.is_visible():
                        continue
                    if visible_index == wanted_occurrence:
                        return node
                    visible_index += 1
                except Exception:
                    continue
            return None

        self._wait_until(
            lambda: visible_target() is not None,
            f"第二步没有找到“{group_type}”题目导航入口",
            timeout_seconds=30,
        )
        target = visible_target()
        if target is None:
            raise PlatformInputUiError(f"第二步没有找到“{group_type}”题目导航入口")
        try:
            _debug_dom_snapshot(self, f"before-click:{group_type}")
            target.click(timeout=self.action_timeout_ms)
            self.page.wait_for_timeout(300)
            _debug_dom_snapshot(self, f"after-click:{group_type}")
        except Exception as exc:
            try:
                target.click(force=True, timeout=self.action_timeout_ms)
                self.page.wait_for_timeout(300)
                _debug_dom_snapshot(self, f"after-force-click:{group_type}")
            except Exception as force_exc:
                raise PlatformInputUiError(
                    f"点击“{group_type}”题目导航失败: {force_exc}"
                ) from exc

    def _activate_retelling_outline(self) -> None:
        """点击记录并转述部分最后一个录音题导航。

        该模板把模仿朗读显示为“第1题(录音题)”，把信息转述显示为
        “第4题(录音题)”。两者是不同的懒加载面板，不能只按题型取第一
        个录音题入口。
        """

        nodes = self.page.get_by_text(
            re.compile(r"^第\s*\d+\s*题\s*[（(]\s*录音题\s*[）)]$")
        )
        candidates: list[Any] = []
        try:
            count = nodes.count()
        except Exception:
            count = 0
        for index in range(count):
            try:
                node = nodes.nth(index)
                if node.is_visible():
                    candidates.append(node)
            except Exception:
                continue
        if not candidates:
            raise PlatformInputUiError("第二步没有找到信息转述录音题导航入口")
        target = candidates[-1]
        try:
            target.click(timeout=self.action_timeout_ms)
        except Exception as exc:
            try:
                target.click(force=True, timeout=self.action_timeout_ms)
            except Exception as force_exc:
                raise PlatformInputUiError(
                    f"点击信息转述录音题导航失败: {force_exc}"
                ) from exc
