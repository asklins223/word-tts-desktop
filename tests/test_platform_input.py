from __future__ import annotations

import pathlib
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import platform_entry.paper_input as platform_input
    from platform_entry.adapter.content_imitation import PlatformInputImitationContentMixin
    from platform_entry.adapter.content_legacy_exam import PlatformInputLegacyExamContentMixin
    from platform_entry.adapter.content_record import PlatformInputRecordContentMixin
    from platform_entry.adapter.content_response import PlatformInputResponseContentMixin
    from platform_entry.adapter.page_forms import PlatformInputFormMixin
    from platform_entry.adapter.page_navigation import PlatformInputNavigationMixin
    from platform_entry.paper_input import (
        CONTENT_CREATE_PATH,
        CONTENT_UPDATE_PATH,
        PlatformInputPageAutomation,
        PaperBundleRule,
        PAPER_CREATE_PATH,
        PAPER_CONTENT_GET_PATH,
        PAPER_PAGE_PATH,
        ReadOnlyFeedbackObserver,
        PlatformInputError,
        build_dry_run_result,
        normalize_spec,
        resolve_paper_bundle_rule,
        run_page_input,
    )
    from platform_entry.adapter.runtime import _default_profile_dir
    from platform_entry.adapter.runtime import PersistentBrowserSession, _launch_browser
    from workflow.system_input_executor import _profile_execution_lock
except (ImportError, SyntaxError) as exc:
    raise unittest.SkipTest("可选页面录入适配器未解锁，跳过私有适配器测试") from exc


def _raw_spec(*, category: str = "题型专项", with_paper_type: bool = False) -> dict:
    paper = {
        "title": "七年级上学期-U5-模仿朗读",
        "province": {"id": 440000, "name": "广东省"},
        "city": {"id": 440600, "name": "佛山市"},
        "districts": [{"id": 440605, "name": "南海区"}],
        "stage": {"id": 2, "name": "初中"},
        "grade": {"id": 7, "name": "七年级"},
        "year": 2026,
        "duration": 20,
    }
    if with_paper_type:
        paper["paper_type"] = {"id": 1, "name": "阶段测试题"}
    return {
        "paper_category": category,
        "paper": paper,
        "template_name": "模仿朗读",
        "items": [
            {"text": "The weather is fine today."},
            {"text": "We can read aloud together."},
        ],
    }


def _grouped_raw_spec(asset_dir: pathlib.Path) -> dict:
    """完整套卷的页面语义输入：第一段听后选择稿挂两道小题。"""

    for name in (
        "choice-1.mp3",
        "choice-2.mp3",
        "response-1.mp3",
        "response-2.mp3",
        "imitation.mp3",
        "record.mp3",
        "record-table.png",
    ):
        (asset_dir / name).write_bytes(b"asset")

    def options(*values: str) -> list[dict[str, str]]:
        return [
            {"option_id": chr(ord("A") + index), "text": value}
            for index, value in enumerate(values)
        ]

    return {
        "paper_category": "听说考试",
        "paper": {
            "title": "人教七上-S9",
            "province": {"name": "湖北省"},
            "city": {"name": "武汉市"},
            "districts": [],
            "stage": {"name": "初中"},
            "grade": {"name": "七年级"},
            "paper_type": {"name": "单元测试题"},
            "year": 2026,
            "duration": 60,
        },
        "template_name": "人教版 听说测试题模板",
        "question_groups": [
            {
                "type": "听后选择",
                "materials": [
                    {
                        "audio_path": "choice-1.mp3",
                        "listening_text": "M: Where did you go? W: I went to Yunnan.",
                        "times": 2,
                        "gap": 5,
                        "questions": [
                            {
                                "prompt": "Where did the boy go?",
                                "options": options("To Yunnan.", "To Beijing.", "To Wuhan."),
                                "answer": "A",
                                "score": 1,
                                "answer_time": 5,
                            },
                            {
                                "prompt": "Who went with him?",
                                "options": options("His family.", "His teacher.", "His friend."),
                                "answer": "A",
                                "score": 1,
                                "answer_time": 5,
                            },
                        ],
                    },
                    {
                        "audio_path": "choice-2.mp3",
                        "listening_text": "W: What colour is the bag? M: It is green.",
                        "questions": [
                            {
                                "prompt": "What colour is the bag?",
                                "options": options("Green.", "Blue.", "Red."),
                                "answer": "A",
                                "score": 1,
                            }
                        ],
                    },
                ],
            },
            {
                "type": "听后应答",
                "questions": [
                    {
                        "listening_text": "Where did Lisa spend her vacation?",
                        "audio_path": "response-1.mp3",
                        "options": options("In Beijing.", "In Wuhan."),
                        "answer": "A",
                        "score": 1,
                    },
                    {
                        "listening_text": "How long did Mary stay?",
                        "audio_path": "response-2.mp3",
                        "options": options("For a week.", "For a month."),
                        "answer": "A",
                        "score": 1,
                    },
                ],
            },
            {
                "type": "模仿朗读",
                "questions": [
                    {
                        "listening_text": "Please read this short passage.",
                        "audio_path": "imitation.mp3",
                        "score": 7,
                        "reference_answers": ["Please read this short passage."],
                    }
                ],
            },
            {
                "type": "听后记录并转述信息",
                "recording": {
                    "audio_path": "record.mp3",
                    "image_path": "record-table.png",
                    "listening_text": "Emma is an exchange student in our class.",
                    "questions": [
                        {"score": 1, "answers": ["tidy"]},
                        {"score": 1, "answers": ["Things"]},
                        {"score": 1, "answers": ["bottle"]},
                    ],
                },
                "retelling": {
                    "prompt": "This is Cindy's room.",
                    "score": 5,
                    "answer_time": 90,
                    "reference_answers": ["This is Cindy's room. It is tidy."],
                },
            },
        ],
    }
class _FakeRequest:
    def __init__(self, method: str, resource_type: str = "xhr") -> None:
        self.method = method
        self.resource_type = resource_type


class _FakeResponse:
    def __init__(
        self,
        url: str,
        method: str,
        body,
        *,
        status: int = 200,
        resource_type: str = "xhr",
    ) -> None:
        self.url = url
        self.status = status
        self.request = _FakeRequest(method, resource_type)
        self._body = body

    def json(self):
        return self._body


class _FakeObserver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin_saved_feedback_capture(self) -> None:
        self.calls.append("begin_saved_feedback_capture")

    def snapshot(self) -> dict:
        return {
            "paperId": "paper-123",
            "status": "已下架",
            "contentReadback": True,
            "readOnlyRequests": [
                {"method": "GET", "path": PAPER_CONTENT_GET_PATH, "status": 200}
            ],
            "writeResponsesObserved": [
                {"method": "POST", "path": PAPER_CREATE_PATH, "status": 200},
                {"method": "POST", "path": CONTENT_CREATE_PATH, "status": 200},
                {"method": "PUT", "path": CONTENT_UPDATE_PATH, "status": 200},
            ],
        }


class _FakeAutomation:
    def __init__(self, spec) -> None:
        self.spec = spec
        self.observer = _FakeObserver()
        self.calls: list[str] = []
        self.existing_paper_id = None

    def _record(self, name: str) -> None:
        self.calls.append(name)

    def wait_until_ready(self, admin_url: str, timeout_seconds: float) -> None:
        self._record("wait_until_ready")

    def wait_until_preflight_phase(self, phase: str, timeout_seconds: float = 30) -> None:
        self._record("wait_until_preflight_phase")

    def start_new_paper(self) -> None:
        self._record("start_new_paper")

    def start_existing_paper(self) -> str:
        self._record("start_existing_paper")
        return "content"

    def fill_base_form(self) -> None:
        self._record("fill_base_form")

    def select_template(self) -> None:
        self._record("select_template")

    def next_to_content(self) -> None:
        self._record("next_to_content")

    def fill_content(self) -> None:
        self._record("fill_content")

    def save_content(self) -> None:
        self._record("save_content")

    def return_to_list(self) -> None:
        self._record("return_to_list")

    def feedback(self) -> dict:
        return self.observer.snapshot()


class _FakeTemplateNode:
    def __init__(self, text: str = "", *, class_name: str = "") -> None:
        self.text = text
        self.class_name = class_name
        self.checked = False
        self.click_calls = 0
        self.scroll_calls = 0

    def is_visible(self) -> bool:
        return True

    def inner_text(self) -> str:
        return self.text

    def get_attribute(self, name: str):
        if name == "class":
            return self.class_name
        if name == "aria-checked":
            return "true" if self.checked else None
        if name == "checked":
            return "checked" if self.checked else None
        return None

    def is_checked(self) -> bool:
        return self.checked

    def scroll_into_view_if_needed(self) -> None:
        self.scroll_calls += 1

    def click(self, **_kwargs) -> None:
        self.click_calls += 1
        self.checked = True

    def locator(self, _selector: str) -> "_FakeTemplateLocator":
        return _FakeTemplateLocator([])


class _FakeTemplateLocator:
    def __init__(self, nodes: list[_FakeTemplateNode] | None = None) -> None:
        self.nodes = list(nodes or [])

    @property
    def first(self):
        return self.nodes[0]

    def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int):
        return self.nodes[index]


class _FakeTemplateCard(_FakeTemplateNode):
    def __init__(self, name: str, radio: _FakeTemplateNode) -> None:
        super().__init__(class_name="cardContent")
        self.name_node = _FakeTemplateNode(name, class_name="nameFont")
        self.radio = radio

    def locator(self, selector: str) -> _FakeTemplateLocator:
        if selector == ".nameFont":
            return _FakeTemplateLocator([self.name_node])
        if selector == ".el-radio__inner:visible":
            return _FakeTemplateLocator([self.radio])
        if selector in {
            'input[type="radio"]',
            'input[type="checkbox"]',
            '[role="radio"]',
            ".el-radio",
            ".el-radio__input",
            ".el-radio__inner",
        }:
            return _FakeTemplateLocator([self.radio])
        return _FakeTemplateLocator([])


class _FakeTemplatePage:
    def __init__(self, card: _FakeTemplateCard) -> None:
        self.card = card

    def locator(self, selector: str) -> _FakeTemplateLocator:
        if selector == ".cardContent:visible":
            return _FakeTemplateLocator([self.card])
        return _FakeTemplateLocator([])


class _FakeVisualOnlyRadio(_FakeTemplateNode):
    """A radio whose visual click is not exposed as a checked property."""

    def click(self, **_kwargs) -> None:
        self.click_calls += 1


class _FakeCustomNextPage(_FakeTemplatePage):
    """Model the platform build whose next control is a clickable div."""

    def __init__(self, card: _FakeTemplateCard) -> None:
        super().__init__(card)
        self.next_node = _FakeTemplateNode(
            "下一步：录入试题内容",
            class_name="custom-next-control",
        )

    def get_by_role(self, _role: str, name: str, exact: bool = False) -> _FakeTemplateLocator:
        return _FakeTemplateLocator([])

    def get_by_text(self, text: str, exact: bool = False) -> _FakeTemplateLocator:
        if text == self.next_node.text:
            return _FakeTemplateLocator([self.next_node])
        return _FakeTemplateLocator([])


class PlatformInputTests(unittest.TestCase):
    def test_page_input_preserves_internal_spaces_in_paper_title(self) -> None:
        class FakeInput:
            def __init__(self) -> None:
                self.value = "old title"
                self.events: list[tuple[str, str]] = []

            def scroll_into_view_if_needed(self, **_kwargs) -> None:
                self.events.append(("scroll", ""))

            def click(self, **_kwargs) -> None:
                self.events.append(("click", ""))

            def press(self, key: str, **_kwargs) -> None:
                self.events.append(("press", key))

            def fill(self, value: str, **_kwargs) -> None:
                self.events.append(("fill", value))
                self.value = value

            def type(self, value: str, **_kwargs) -> None:
                self.events.append(("type", value))
                self.value = value

            def input_value(self, **_kwargs) -> str:
                return self.value

        input_node = FakeInput()
        automation = object.__new__(PlatformInputFormMixin)
        automation.action_timeout_ms = 1234

        automation._type_input_value(
            input_node,
            "人教版七上-Starter Unit1-1",
            "试卷名称",
        )

        self.assertEqual(
            input_node.events,
            [
                ("fill", "人教版七上-Starter Unit1-1"),
                ("press", "Tab"),
            ],
        )
        self.assertEqual(input_node.value, "人教版七上-Starter Unit1-1")

    def test_launch_browser_clears_profile_and_retries_once(self) -> None:
        class Lifecycle:
            clear_calls = []
            terminate_calls = []

            @classmethod
            def _clear_stale_profile_lock(cls, profile_dir):
                cls.clear_calls.append(profile_dir)

            @classmethod
            def _terminate_profile_owner(cls, profile_dir, expected_pid=None):
                cls.terminate_calls.append((profile_dir, expected_pid))

        class Chromium:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, **options):
                self.calls.append(options)
                if len(self.calls) == 1:
                    raise RuntimeError("SingletonLock is already held")
                return object()

        class Playwright:
            def __init__(self):
                self.chromium = Chromium()

        with tempfile.TemporaryDirectory() as temp:
            profile_dir = pathlib.Path(temp) / "profile"
            playwright = Playwright()
            with patch("platform_entry.adapter.runtime._profile_lock_lifecycle", return_value=Lifecycle), \
                    patch("xunfei.config.configure_playwright_runtime"), \
                    patch("xunfei.config._find_chrome", return_value=None), \
                    patch("xunfei.config._find_bundled_chromium", return_value=None):
                context = _launch_browser(playwright, profile_dir)

        self.assertIsNotNone(context)
        self.assertEqual(len(playwright.chromium.calls), 2)
        self.assertEqual(len(Lifecycle.clear_calls), 2)
        self.assertEqual(len(Lifecycle.terminate_calls), 1)
        self.assertEqual(Lifecycle.terminate_calls[0][1], None)

    def test_persistent_browser_session_reuses_context_until_closed(self) -> None:
        class Context:
            def __init__(self):
                self.pages = []
                self.closed = False
                self.close_calls = 0

            def is_closed(self):
                return self.closed

            def new_page(self):
                page = object()
                self.pages.append(page)
                return page

            def close(self):
                self.close_calls += 1
                self.closed = True

        class Manager:
            def __init__(self, playwright):
                self.playwright = playwright
                self.stop_calls = 0

            def start(self):
                return self.playwright

            def stop(self):
                self.stop_calls += 1

        context = Context()
        playwright = object()
        manager = Manager(playwright)
        lifecycle = Mock()
        lifecycle._profile_lock_owner_pid.return_value = 1234

        launch_calls = []

        def launch(_playwright, profile_dir):
            launch_calls.append((playwright, profile_dir))
            return context

        with tempfile.TemporaryDirectory() as temp:
            with patch("playwright.sync_api.sync_playwright", return_value=manager), \
                    patch("platform_entry.adapter.runtime._profile_lock_lifecycle", return_value=lifecycle):
                session = PersistentBrowserSession(pathlib.Path(temp) / "profile", launch_browser=launch)
                self.assertIs(session.open(), context)
                self.assertIs(session.open(), context)
                self.assertIs(session.page(), context.pages[0])
                session.close()

        self.assertEqual(len(launch_calls), 1)
        self.assertEqual(context.close_calls, 1)
        self.assertEqual(manager.stop_calls, 1)
        lifecycle._terminate_profile_owner.assert_called_once()
        self.assertEqual(lifecycle._terminate_profile_owner.call_args.kwargs["expected_pid"], 1234)

    def test_profile_execution_lock_is_reentrant_for_a_reusable_input_run(self) -> None:
        # begin_run() holds the profile lock across the whole browser session,
        # while preflight and each page operation acquire it again.  A plain
        # Lock would self-deadlock the worker before the first page action.
        with tempfile.TemporaryDirectory() as temp:
            lock = _profile_execution_lock(pathlib.Path(temp) / "profile")
            self.assertTrue(lock.acquire(timeout=0.2))
            try:
                self.assertTrue(lock.acquire(timeout=0.2))
                lock.release()
            finally:
                lock.release()

    def test_page_automation_installs_bounded_playwright_timeouts(self) -> None:
        spec = normalize_spec(_raw_spec())
        page = Mock()
        PlatformInputPageAutomation(page, spec, _FakeObserver())

        page.set_default_timeout.assert_called_once_with(15_000)
        page.set_default_navigation_timeout.assert_called_once_with(30_000)

    def test_select_template_clicks_radio_and_waits_for_selected_state(self) -> None:
        spec = normalize_spec(_raw_spec())
        radio = _FakeTemplateNode(class_name="el-radio__inner")
        card = _FakeTemplateCard("模仿朗读", radio)
        page = _FakeTemplatePage(card)
        automation = PlatformInputPageAutomation(page, spec, _FakeObserver())

        automation.select_template()

        self.assertEqual(radio.click_calls, 1)
        self.assertTrue(radio.checked)
        self.assertEqual(card.click_calls, 0)

    def test_select_template_accepts_camel_case_selected_card_marker(self) -> None:
        spec = normalize_spec(_raw_spec())
        radio = _FakeTemplateNode(class_name="el-radio__inner")
        card = _FakeTemplateCard("模仿朗读", radio)
        card.class_name = "cardContent isSelected"
        page = _FakeTemplatePage(card)
        automation = PlatformInputPageAutomation(page, spec, _FakeObserver())

        automation.select_template()

        self.assertEqual(radio.click_calls, 1)

    def test_select_template_reuses_card_found_by_wait(self) -> None:
        spec = normalize_spec(_raw_spec())
        radio = _FakeTemplateNode(class_name="el-radio__inner")
        card = _FakeTemplateCard("模仿朗读", radio)

        class DelayedTemplatePage(_FakeTemplatePage):
            def __init__(self) -> None:
                super().__init__(card)
                self.card_lookups = 0

            def locator(self, selector: str) -> _FakeTemplateLocator:
                if selector == ".cardContent:visible":
                    self.card_lookups += 1
                    return _FakeTemplateLocator(
                        [] if self.card_lookups == 1 else [self.card]
                    )
                return super().locator(selector)

        page = DelayedTemplatePage()
        automation = PlatformInputPageAutomation(page, spec, _FakeObserver())
        automation._wait_until = lambda predicate, *_args, **_kwargs: predicate()

        automation.select_template()

        self.assertEqual(page.card_lookups, 2)

    def test_select_template_accepts_enabled_custom_next_control(self) -> None:
        spec = normalize_spec(_raw_spec())
        radio = _FakeVisualOnlyRadio(class_name="el-radio__inner")
        card = _FakeTemplateCard("模仿朗读", radio)
        page = _FakeCustomNextPage(card)
        automation = PlatformInputPageAutomation(page, spec, _FakeObserver())

        automation.select_template()

        self.assertEqual(radio.click_calls, 1)

    def test_wait_until_runs_control_checkpoint_before_long_page_wait(self) -> None:
        navigation = object.__new__(PlatformInputNavigationMixin)
        navigation.page = Mock()
        navigation.page.wait_for_timeout = Mock()

        def stop() -> None:
            raise RuntimeError("stop requested")

        navigation._control_check = stop
        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            navigation._wait_until(
                lambda: False,
                "should not reach timeout",
                timeout_seconds=30,
            )
        navigation.page.wait_for_timeout.assert_not_called()

    def test_group_activation_reuses_dom_results_from_successful_poll(self) -> None:
        automation = object.__new__(PlatformInputPageAutomation)
        cards = [object()]
        editors = [object()]
        calls = {"cards": 0, "editors": 0}
        automation._activate_outline_section = Mock()

        def question_cards(_kind: str):
            calls["cards"] += 1
            return cards

        def labeled_editors(_label: str):
            calls["editors"] += 1
            return editors

        automation._question_cards = question_cards
        automation._labeled_text_editors = labeled_editors
        automation._wait_until = lambda predicate, *_args, **_kwargs: predicate()

        result = automation._activate_group_section("听后选择", "选择题", 1, 1)

        self.assertEqual(result, (cards, editors))
        self.assertEqual(calls, {"cards": 1, "editors": 1})

    def test_run_page_input_resumes_after_preflight_template_selection(self) -> None:
        spec = normalize_spec(_raw_spec())
        automation = _FakeAutomation(spec)

        result = run_page_input(
            automation,
            admin_url="https://admin.example.test/#/resource/exam",
            login_timeout=5,
            resume_phase="new_paper_template_selected",
        )

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(
            automation.calls,
            [
                "wait_until_preflight_phase",
                "next_to_content",
                "fill_content",
                "save_content",
                "return_to_list",
            ],
        )

    def test_desktop_profile_env_is_shared_with_textbook_sync(self) -> None:
        with tempfile.TemporaryDirectory() as preferred, tempfile.TemporaryDirectory() as legacy:
            with patch.dict(
                os.environ,
                {
                    "WORDTTS_PLATFORM_INPUT_PROFILE_DIR": preferred,
                    "PLATFORM_INPUT_PROFILE_DIR": legacy,
                },
                clear=False,
            ):
                self.assertEqual(_default_profile_dir(), pathlib.Path(os.path.abspath(preferred)))

    def test_legacy_profile_env_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as legacy:
            with patch.dict(
                os.environ,
                {
                    "WORDTTS_PLATFORM_INPUT_PROFILE_DIR": "",
                    "PLATFORM_INPUT_PROFILE_DIR": legacy,
                },
                clear=False,
            ):
                self.assertEqual(_default_profile_dir(), pathlib.Path(os.path.abspath(legacy)))

    def test_deployment_urls_restore_without_manual_url_options(self) -> None:
        self.assertTrue(platform_input.ADMIN_URL.startswith("https://"))
        self.assertTrue(platform_input.API_BASE_URL.startswith("https://"))
        self.assertTrue(platform_input.ADMIN_URL.endswith("/#/resource/exam"))

        option_strings = {
            option
            for action in platform_input.build_parser()._actions
            for option in action.option_strings
        }
        self.assertNotIn("--admin-url", option_strings)
        self.assertNotIn("--api-base", option_strings)

    def test_registered_bundle_rule_is_selected_by_platform_template_name(self) -> None:
        rule = resolve_paper_bundle_rule("人教版 听说测试题模板")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.key, "platform_input-listening-exam-v1")
        self.assertEqual(rule.card_counts(), {"选择题": 8, "填空题": 3, "录音题": 9})

        foreign = resolve_paper_bundle_rule(
            "外研版 听说测试题模板",
            paper_name="佛山七上Starter 1",
            template_id="2094610981895405568",
        )
        self.assertIsNotNone(foreign)
        self.assertEqual(
            foreign.key,
            "platform_input-foreign-legacy-listening-exam-v1",
        )
        self.assertEqual(foreign.card_counts(), {"录音题": 14})
        self.assertEqual(foreign.outline_numbering, "local")
        self.assertEqual(foreign.outline_target("模仿朗读"), ("第1题(录音题)", 0))
        self.assertEqual(foreign.outline_target("信息获取"), ("第1题(录音题)", 1))
        self.assertEqual(
            foreign.outline_target("信息转述及询问"),
            ("第1题(录音题)", 2),
        )

    def test_standalone_info_retelling_rule_reuses_two_lazy_subsections(self) -> None:
        rule = resolve_paper_bundle_rule(
            "信息转述及询问",
            template_id="2085639857241194496",
        )

        self.assertIsNotNone(rule)
        self.assertEqual(rule.key, "platform_input-info-retelling-special-v1")
        self.assertEqual(rule.card_counts(), {"录音题": 3})
        self.assertEqual(
            rule.outline_subsection_target("信息转述及询问", "信息转述"),
            ("第1题(录音题)", 0),
        )
        self.assertEqual(
            rule.outline_subsection_target("信息转述及询问", "询问信息"),
            ("第2题(录音题)", 0),
        )
        self.assertIsNone(resolve_paper_bundle_rule("未来平台模板"))

        with self.assertRaises(PlatformInputError):
            resolve_paper_bundle_rule(
                "人教版 听说测试题模板",
                explicit_key="unregistered-rule",
            )

    def test_ambiguous_bundle_rule_match_fails_closed(self) -> None:
        first = PaperBundleRule(key="rule-a", template_name_tokens=("同名模板",))
        second = PaperBundleRule(key="rule-b", template_name_tokens=("同名模板",))
        with patch.object(platform_input, "PAPER_BUNDLE_RULES", (first, second)):
            with self.assertRaisesRegex(PlatformInputError, "同时匹配多个套卷规则"):
                resolve_paper_bundle_rule("同名模板")

    def test_bundle_template_id_requires_an_exact_registered_match(self) -> None:
        rule = PaperBundleRule(key="rule-a", template_ids=("template-1",))
        with patch.object(platform_input, "PAPER_BUNDLE_RULES", (rule,)):
            self.assertEqual(
                resolve_paper_bundle_rule("未提供名称", template_id="TEMPLATE-1").key,
                "rule-a",
            )
            self.assertIsNone(
                resolve_paper_bundle_rule("未提供名称", template_id="template-10")
            )

    def test_run_page_input_retries_an_existing_paper_without_new_flow(self) -> None:
        spec = normalize_spec(_raw_spec())
        automation = _FakeAutomation(spec)

        result = run_page_input(
            automation,
            admin_url="https://admin.example.test/#/resource/exam",
            login_timeout=5,
            edit_existing=True,
            existing_paper_id="paper-123",
        )

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(
            automation.calls,
            [
                "wait_until_ready",
                "start_existing_paper",
                "fill_content",
                "save_content",
                "return_to_list",
            ],
        )
        self.assertNotIn("start_new_paper", automation.calls)
        self.assertNotIn("select_template", automation.calls)

    def test_run_page_input_checks_run_control_around_each_page_action(self) -> None:
        spec = normalize_spec(_raw_spec())
        automation = _FakeAutomation(spec)
        checkpoints: list[str] = []

        result = run_page_input(
            automation,
            admin_url="https://admin.example.test/#/resource/exam",
            login_timeout=5,
            control_check=lambda: checkpoints.append("checkpoint"),
        )

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(checkpoints), len(automation.calls) * 2)
        self.assertEqual(
            automation.calls,
            [
                "wait_until_ready",
                "start_new_paper",
                "fill_base_form",
                "select_template",
                "next_to_content",
                "fill_content",
                "save_content",
                "return_to_list",
            ],
        )

    def test_special_paper_uses_page_values_without_api_payload_fields(self) -> None:
        spec = normalize_spec(_raw_spec())

        self.assertEqual(spec.paper_category, "题型专项")
        self.assertEqual(spec.paper["province"]["name"], "广东省")
        self.assertIsNone(spec.paper["paper_type"])
        self.assertEqual(spec.question_count, 2)

    def test_listening_exam_requires_page_paper_type(self) -> None:
        with self.assertRaises(PlatformInputError):
            normalize_spec(_raw_spec(category="听说考试"))

        spec = normalize_spec(
            _raw_spec(category="听说考试", with_paper_type=True)
        )
        self.assertEqual(spec.paper["paper_type"]["name"], "阶段测试题")

    def test_old_api_ready_payload_is_rejected(self) -> None:
        raw = _raw_spec()
        raw["paper"]["provinceId"] = 440000
        with self.assertRaises(PlatformInputError):
            normalize_spec(raw)

        raw = _raw_spec()
        raw["pages"] = [{"platform": "opaque"}]
        with self.assertRaises(PlatformInputError):
            normalize_spec(raw)

        raw = _raw_spec()
        raw["content"] = {"json_content": {"platform": "opaque"}}
        with self.assertRaises(PlatformInputError):
            normalize_spec(raw)

        raw = _raw_spec()
        raw["question_groups"] = [{"type": "模仿朗读", "pages": []}]
        with self.assertRaises(PlatformInputError):
            normalize_spec(raw)

        raw = _raw_spec()
        raw["api_payload"] = {"paperId": "must-not-be-accepted"}
        with self.assertRaises(PlatformInputError):
            normalize_spec(raw)

    def test_template_and_items_are_required(self) -> None:
        raw = _raw_spec()
        raw.pop("template_name")
        with self.assertRaises(PlatformInputError):
            normalize_spec(raw)

        raw = _raw_spec()
        raw["items"] = []
        with self.assertRaises(PlatformInputError):
            normalize_spec(raw)

    def test_grouped_bundle_keeps_shared_audio_above_multiple_subquestions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = pathlib.Path(temp_dir)
            spec = normalize_spec(
                _grouped_raw_spec(asset_dir),
                base_dir=asset_dir,
            )

        self.assertEqual(spec.question_count, 10)
        choice = spec.groups[0]
        self.assertEqual(choice["type"], "听后选择")
        self.assertEqual(len(choice["materials"][0]["questions"]), 2)
        self.assertEqual(
            choice["materials"][0]["questions"][1]["prompt"],
            "Who went with him?",
        )
        self.assertEqual(
            choice["materials"][0]["questions"][0].get("audio_path"),
            None,
        )
        dry_run = build_dry_run_result(spec)
        # 同一段听力材料下的两道小题只对应一次原文音频上传。
        self.assertEqual(len(dry_run["audio_files"]), 6)
        self.assertEqual(
            dry_run["audio_files"].count(str((asset_dir / "choice-1.mp3").resolve())),
            1,
        )
        self.assertEqual(
            dry_run["image_files"],
            [str((asset_dir / "record-table.png").resolve())],
        )

    def test_info_acquisition_keeps_two_subsections_through_page_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = pathlib.Path(temp_dir)
            for name in ("info-selection.mp3", "info-response.mp3"):
                (asset_dir / name).write_bytes(b"asset")
            raw = {
                "paper_category": "听说考试",
                "paper": {
                    "title": "外研七上-S1",
                    "province": {"name": "广东省"},
                    "city": {"name": "佛山市"},
                    "districts": [],
                    "stage": {"name": "初中"},
                    "grade": {"name": "七年级"},
                    "paper_type": {"name": "单元测试题"},
                    "year": 2026,
                    "duration": 60,
                },
                "template_name": "外研版听说测试题模板",
                "question_groups": [{
                    "type": "信息获取",
                    "materials": [
                        {
                            "section": "第一节 听选信息",
                            "audio_path": "info-selection.mp3",
                            "listening_text": "M: What subject do you like?",
                            "questions": [{
                                "prompt": "What subject do you like?",
                                "options": [
                                    {"option_id": "A", "text": "Maths."},
                                    {"option_id": "B", "text": "Science."},
                                ],
                                "answer": "B",
                                "score": 1.5,
                                "reference_answers": ["Science."],
                            }],
                        },
                        {
                            "section": "第二节 回答问题",
                            "audio_path": "info-response.mp3",
                            "listening_text": "The school is big.",
                            "questions": [{
                                "prompt": "What is the school like?",
                                "score": 1.5,
                                "reference_answers": ["It is big."],
                            }],
                        },
                    ],
                }],
            }
            spec = normalize_spec(raw, base_dir=asset_dir)

        info_group = next(group for group in spec.groups if group["type"] == "信息获取")
        self.assertEqual(
            [material["section"] for material in info_group["materials"]],
            ["听选信息", "回答问题"],
        )

        automation = object.__new__(PlatformInputPageAutomation)
        automation.spec = spec
        automation._activate_group_section = lambda *_args, **_kwargs: (
            [object(), object()],
            [object(), object()],
        )
        descriptions: list[str] = []
        automation._replace_rich_text = (
            lambda _editor, _value, *, field: descriptions.append(field)
        )
        automation._upload_original_audio = (
            lambda _path, _occurrence, description: descriptions.append(description)
        )
        automation._fill_recording_card = (
            lambda _card, _question, *, description, **_kwargs: descriptions.append(description)
        )

        result = PlatformInputLegacyExamContentMixin._fill_info_acquisition_groups(
            automation,
            {"信息获取": (info_group,)},
        )

        self.assertEqual(result, {"录音题": 2})
        self.assertIn("第一节听选信息第1段录音听力原文", descriptions)
        self.assertIn("第一节听选信息第1题", descriptions)
        self.assertIn("第二节回答问题第1段录音听力原文", descriptions)
        self.assertIn("第二节回答问题第1题", descriptions)

    def test_group_navigation_uses_global_question_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec = normalize_spec(
                _grouped_raw_spec(pathlib.Path(temp_dir)),
                base_dir=pathlib.Path(temp_dir),
            )
        navigation = object.__new__(PlatformInputNavigationMixin)
        navigation.spec = spec

        self.assertEqual(navigation._group_first_question_number("听后选择"), 1)
        self.assertEqual(navigation._group_first_question_number("听后应答"), 4)
        self.assertEqual(navigation._group_first_question_number("模仿朗读"), 6)
        self.assertEqual(
            navigation._group_first_question_number("听后记录并转述信息"),
            7,
        )

    def test_grouped_choice_requires_complete_options_and_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw = _grouped_raw_spec(pathlib.Path(temp_dir))
            raw["question_groups"][0]["materials"][0]["questions"][0]["options"] = [
                {"option_id": "A", "text": "Only one"}
            ]
            with self.assertRaises(PlatformInputError):
                normalize_spec(raw, base_dir=pathlib.Path(temp_dir))

    def test_selection_does_not_treat_listening_text_as_a_question_stem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw = _grouped_raw_spec(pathlib.Path(temp_dir))
            question = raw["question_groups"][0]["materials"][0]["questions"][0]
            question.pop("prompt")
            question["listening_text"] = "This is not the question stem."
            with self.assertRaises(PlatformInputError):
                normalize_spec(raw, base_dir=pathlib.Path(temp_dir))

    def test_flat_imitation_items_keep_reference_answers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = pathlib.Path(temp_dir)
            (asset_dir / "imitation.mp3").write_bytes(b"asset")
            raw = _raw_spec()
            raw["items"] = [{
                "text": "Read this passage.",
                "audio_path": "imitation.mp3",
                "score": 7,
                "reference_answers": ["Read this passage.", "Read it aloud."],
            }]
            spec = normalize_spec(raw, base_dir=asset_dir)

        self.assertEqual(
            spec.items[0]["reference_answers"],
            ("Read this passage.", "Read it aloud."),
        )

    def test_flat_imitation_content_writes_each_score(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = pathlib.Path(temp_dir)
            (asset_dir / "imitation.mp3").write_bytes(b"asset")
            raw = _raw_spec()
            raw["items"] = [{
                "text": "Read this passage.",
                "audio_path": "imitation.mp3",
                "score": 7,
            }]
            spec = normalize_spec(raw, base_dir=asset_dir)

        automation = object.__new__(PlatformInputPageAutomation)
        automation.spec = spec
        automation._labeled_text_editors = lambda _label: [object()]
        automation._replace_rich_text = lambda *args, **kwargs: None
        automation._upload_audio = lambda *args, **kwargs: None
        automation._wait_audio_rendered = lambda *args, **kwargs: None
        scores: list[tuple[tuple[str, ...], int, object]] = []
        automation._fill_labeled_input_at = (
            lambda labels, occurrence, value, _description: scores.append(
                (tuple(labels), occurrence, value)
            )
        )
        automation._fill_item_numbers = lambda: None

        PlatformInputImitationContentMixin._fill_imitation_content(automation)

        self.assertEqual(scores, [(('分数',), 0, 7)])

    def test_response_options_become_stem_and_answer_strips_star(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = pathlib.Path(temp_dir)
            raw = _grouped_raw_spec(asset_dir)
            question = raw["question_groups"][1]["questions"][0]
            question["prompt"] = "This explicit prompt is not used by the card."
            question["listening_text"] = "The teacher asks where Lisa spent her vacation."
            question["options"] = [
                {"option_id": "A", "text": "★ In the pencil box."},
                {"option_id": "B", "text": "★ Purple."},
            ]
            question["answer"] = "B"
            spec = normalize_spec(raw, base_dir=asset_dir)

        response_question = spec.groups[1]["questions"][0]
        self.assertEqual(
            response_question["prompt"],
            "★ In the pencil box. ★ Purple.",
        )
        self.assertEqual(response_question["text"], response_question["prompt"])
        self.assertEqual(response_question["reference_answers"], ("Purple.",))
        self.assertEqual(
            response_question["listening_text"],
            "The teacher asks where Lisa spent her vacation.",
        )

    def test_response_audio_targets_material_original_audio_not_question_stem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = pathlib.Path(temp_dir)
            spec = normalize_spec(
                _grouped_raw_spec(asset_dir),
                base_dir=asset_dir,
            )

        response_group = spec.groups[1]
        automation = object.__new__(PlatformInputPageAutomation)
        automation.spec = spec
        automation._activate_group_section = lambda *_args: (
            [object(), object()],
            [object(), object()],
        )
        automation._replace_rich_text = lambda *args, **kwargs: None
        automation._fill_material_numbers = lambda *args, **kwargs: None
        automation._fill_recording_card = lambda *args, **kwargs: None
        original_audio_calls: list[tuple[str, int, str]] = []
        automation._upload_original_audio = (
            lambda path, occurrence, description: original_audio_calls.append(
                (path, occurrence, description)
            )
        )

        def unexpected_card_audio(*_args, **_kwargs):
            self.fail("听后应答音频不应上传到题干音频")

        automation._upload_audio_in_scope = unexpected_card_audio

        result = PlatformInputResponseContentMixin._fill_response_groups(
            automation,
            {"听后应答": (response_group,)},
        )

        self.assertEqual(result, {"录音题": 2})
        self.assertEqual(
            [(pathlib.Path(path).name, occurrence) for path, occurrence, _ in original_audio_calls],
            [("response-1.mp3", 0), ("response-2.mp3", 1)],
        )

    def test_imitation_audio_targets_material_original_audio_not_question_stem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec = normalize_spec(
                _grouped_raw_spec(pathlib.Path(temp_dir)),
                base_dir=pathlib.Path(temp_dir),
            )

        imitation_group = spec.groups[2]
        automation = object.__new__(PlatformInputPageAutomation)
        automation.spec = spec
        automation._activate_group_section = lambda *_args: ([object()], [object()])
        automation._replace_rich_text = lambda *args, **kwargs: None
        automation._fill_recording_card = lambda *args, **kwargs: None
        original_audio_calls: list[tuple[str, int, str]] = []
        automation._upload_original_audio = (
            lambda path, occurrence, description: original_audio_calls.append(
                (path, occurrence, description)
            )
        )

        def unexpected_card_audio(*_args, **_kwargs):
            self.fail("模仿朗读音频不应上传到题干音频")

        automation._upload_audio_in_scope = unexpected_card_audio

        result = PlatformInputImitationContentMixin._fill_imitation_groups(
            automation,
            {"模仿朗读": (imitation_group,)},
        )

        self.assertEqual(result, {"录音题": 1})
        self.assertEqual(
            [(pathlib.Path(path).name, occurrence) for path, occurrence, _ in original_audio_calls],
            [("imitation.mp3", 0)],
        )

    def test_retelling_reuses_recording_text_and_reuploads_the_same_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec = normalize_spec(
                _grouped_raw_spec(pathlib.Path(temp_dir)),
                base_dir=pathlib.Path(temp_dir),
            )

        record_group = spec.groups[3]
        automation = object.__new__(PlatformInputPageAutomation)
        automation.spec = spec
        recording_cards = [object(), object(), object()]
        recording_editor = object()
        retelling_card = object()
        retelling_editor = object()
        automation._activate_group_section = lambda *_args: (
            recording_cards,
            [recording_editor],
        )
        automation._activate_retelling_outline = lambda: None
        automation._question_cards = lambda kind: [retelling_card] if kind == "录音题" else []
        automation._labeled_text_editors = (
            lambda label: [retelling_editor] if label == "听力原文" else []
        )
        automation._wait_until = lambda predicate, *_args, **_kwargs: self.assertTrue(predicate())
        written_text: list[tuple[object, str, str]] = []
        automation._replace_rich_text = (
            lambda editor, value, *, field: written_text.append((editor, value, field))
        )
        original_audio_calls: list[tuple[str, int, str]] = []
        automation._upload_original_audio = (
            lambda path, occurrence, description: original_audio_calls.append(
                (path, occurrence, description)
            )
        )
        automation._fill_material_numbers = lambda *_args, **_kwargs: None
        automation._upload_image = lambda *_args, **_kwargs: None
        automation._fill_recording_card = lambda *_args, **_kwargs: None

        result = PlatformInputRecordContentMixin._fill_record_retelling_groups(
            automation,
            {"听后记录并转述信息": (record_group,)},
        )

        self.assertEqual(result, {"填空题": 3, "录音题": 1})
        self.assertEqual(
            [(pathlib.Path(path).name, occurrence) for path, occurrence, _ in original_audio_calls],
            [("record.mp3", 0), ("record.mp3", 0)],
        )
        self.assertEqual(
            [(editor, value) for editor, value, _field in written_text],
            [
                (recording_editor, record_group["recording"]["listening_text"]),
                (retelling_editor, record_group["recording"]["listening_text"]),
            ],
        )

    def test_grouped_items_alias_is_normalized_as_question_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw = _grouped_raw_spec(pathlib.Path(temp_dir))
            raw["items"] = raw.pop("question_groups")
            spec = normalize_spec(raw, base_dir=pathlib.Path(temp_dir))
        self.assertEqual(len(spec.groups), 4)
        self.assertEqual(spec.items, ())

    def test_legacy_items_with_category_are_not_misclassified_as_groups(self) -> None:
        raw = _raw_spec()
        raw["items"][0]["category"] = "朗读文本"
        spec = normalize_spec(raw)
        self.assertEqual(spec.items[0]["text"], "The weather is fine today.")
        self.assertEqual(spec.groups, ())

    def test_registered_bundle_rejects_flat_items(self) -> None:
        raw = _raw_spec(category="听说考试", with_paper_type=True)
        raw["template_name"] = "人教版 听说测试题模板"
        with self.assertRaises(PlatformInputError):
            normalize_spec(raw)

    def test_read_only_observer_reads_id_and_status_but_ignores_write_body(self) -> None:
        observer = ReadOnlyFeedbackObserver(
            page=object(),
            api_base="https://api.example.test",
            paper_title="七年级上学期-U5-模仿朗读",
        )
        observer._on_response(
            _FakeResponse(
                "https://api.example.test.attacker.example" + PAPER_PAGE_PATH,
                "GET",
                {"data": {"list": [{"id": "attacker-paper"}]}},
            )
        )
        self.assertEqual(observer.read_only_requests, [])

        observer._on_response(
            _FakeResponse(
                "https://api.example.test" + PAPER_CREATE_PATH,
                "POST",
                {"data": {"id": "must-not-be-used"}},
            )
        )
        self.assertIsNone(observer.paper_id)
        self.assertEqual(observer.write_responses_observed[0]["path"], PAPER_CREATE_PATH)

        observer._on_response(
            _FakeResponse(
                "https://api.example.test"
                + PAPER_CONTENT_GET_PATH
                + "?paperId=paper-123",
                "GET",
                {"data": {"pages": []}},
            )
        )
        observer._on_response(
            _FakeResponse(
                "https://api.example.test" + PAPER_PAGE_PATH + "?pageNo=1",
                "GET",
                {
                    "data": {
                        "list": [
                            {
                                "id": "paper-123",
                                "title": "七年级上学期-U5-模仿朗读",
                                "statusName": "已下架",
                            }
                        ]
                    }
                },
            )
        )

        feedback = observer.snapshot()
        self.assertEqual(feedback["paperId"], "paper-123")
        self.assertEqual(feedback["status"], "已下架")
        self.assertTrue(feedback["contentReadback"])
        self.assertEqual(
            [event["method"] for event in feedback["readOnlyRequests"]],
            ["GET", "GET"],
        )

    def test_page_flow_contains_no_preview_step_and_returns_readonly_feedback(self) -> None:
        spec = normalize_spec(_raw_spec())
        automation = _FakeAutomation(spec)

        result = run_page_input(
            automation,
            admin_url="https://admin.example.test/#/resource/exam",
            login_timeout=5,
        )

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["side_effect_policy"], "PAGE_UI_ONLY")
        self.assertEqual(result["paperId"], "paper-123")
        self.assertNotIn("preview", automation.calls)
        self.assertEqual(
            automation.calls,
            [
                "wait_until_ready",
                "start_new_paper",
                "fill_base_form",
                "select_template",
                "next_to_content",
                "fill_content",
                "save_content",
                "return_to_list",
            ],
        )
        self.assertEqual(
            [step["mode"] for step in result["steps"]],
            ["PAGE_UI"] * 8,
        )

    def test_save_content_accepts_create_response_from_a_new_paper(self) -> None:
        automation = object.__new__(PlatformInputPageAutomation)
        automation.observer = type(
            "Observer",
            (),
            {"write_responses_observed": [{"path": CONTENT_CREATE_PATH, "status": 201}]},
        )()
        def click_save(_text):
            automation.observer.write_responses_observed.append(
                {"path": CONTENT_CREATE_PATH, "status": 201},
            )

        automation._click_exact = click_save
        automation._has_visible_text = lambda _text: False

        def wait_until(predicate, _message, **_kwargs):
            assert predicate()

        automation._wait_until = wait_until
        automation.save_content()

    def test_dry_run_is_network_free_and_lists_only_read_feedback_paths(self) -> None:
        spec = normalize_spec(_raw_spec())
        result = build_dry_run_result(spec)

        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["side_effect_policy"], "NO_NETWORK")
        self.assertEqual(
            result["read_only_feedback_paths"],
            [PAPER_PAGE_PATH, PAPER_CONTENT_GET_PATH],
        )
        self.assertEqual(result["preview"], "SKIPPED")
        self.assertNotIn(PAPER_CREATE_PATH, result["read_only_feedback_paths"])
        self.assertNotIn(CONTENT_UPDATE_PATH, result["read_only_feedback_paths"])

    def test_script_has_no_direct_browser_network_client(self) -> None:
        source_path = pathlib.Path(__file__).parents[1] / "platform_entry" / "paper_input.py"
        source = source_path.read_text(encoding="utf-8")

        self.assertNotIn("page.evaluate", source)
        self.assertNotIn("page.request(", source)
        self.assertNotIn("fetch(", source)

    def test_audio_feedback_must_match_the_uploaded_filename(self) -> None:
        self.assertTrue(
            PlatformInputPageAutomation._audio_display_matches(
                "1788404971484_ttsmaker-file-2026-9-3-11-16-29.mp3",
                "/tmp/ttsmaker-file-2026-9-3-11-16-29.mp3",
            )
        )
        self.assertFalse(
            PlatformInputPageAutomation._audio_display_matches(
                "1785811088753_22m6jm4p_模仿朗读_七上u1_01.mp3",
                "/tmp/ttsmaker-file-2026-9-3-11-16-29.mp3",
            )
        )

    def test_local_asset_paths_reject_unsupported_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = pathlib.Path(temp_dir)
            invalid = asset_dir / "not-an-audio.txt"
            invalid.write_text("not audio", encoding="utf-8")

            with self.assertRaises(PlatformInputError):
                platform_input._resolve_audio_path(
                    str(invalid),
                    "audio_path",
                    asset_dir,
                )

            invalid_image = asset_dir / "not-an-image.gif"
            invalid_image.write_bytes(b"GIF89a")
            with self.assertRaises(PlatformInputError):
                platform_input._resolve_image_path(
                    str(invalid_image),
                    "image_path",
                    asset_dir,
                )

    def test_existing_image_editor_uses_hover_without_clicking(self) -> None:
        class FakeImage:
            def __init__(self) -> None:
                self.events: list[tuple[str, dict]] = []

            def scroll_into_view_if_needed(self) -> None:
                self.events.append(("scroll", {}))

            def hover(self, **kwargs) -> None:
                self.events.append(("hover", kwargs))

            def click(self, **_kwargs) -> None:
                raise AssertionError("图片上传不能点击图片来打开系统文件选择器")

        image = FakeImage()
        automation = object.__new__(PlatformInputPageAutomation)
        automation.action_timeout_ms = 1234
        automation._saved_image = lambda _occurrence=0: image
        automation._image_region = lambda _occurrence=0: object()

        def wait_until(predicate, _message, **_kwargs) -> None:
            self.assertTrue(predicate())

        automation._wait_until = wait_until
        automation._activate_saved_image_editor()

        self.assertEqual(
            image.events,
            [("hover", {"force": True, "timeout": 1234})],
        )

    def test_reference_answer_rows_are_shrunk_before_values_are_replaced(self) -> None:
        class FakeAnswer:
            def __init__(self, card, index: int) -> None:
                self.card = card
                self.index = index

        class FakeDelete:
            def __init__(self, card) -> None:
                self.card = card

            def is_visible(self) -> bool:
                return True

            def scroll_into_view_if_needed(self) -> None:
                return None

            def click(self, **_kwargs) -> None:
                self.card.answers.pop()

        class FakeDeletes:
            def __init__(self, card) -> None:
                self.card = card

            def count(self) -> int:
                return len(self.card.answers)

            def nth(self, _index: int) -> FakeDelete:
                return FakeDelete(self.card)

        class FakeCard:
            def __init__(self) -> None:
                self.answers = ["old-1", "old-2", "old-3"]

            def get_by_text(self, text: str, *, exact: bool):
                self.assert_exact = exact
                if text == "删除":
                    return FakeDeletes(self)
                raise AssertionError(f"unexpected text lookup: {text}")

        card = FakeCard()
        automation = object.__new__(PlatformInputPageAutomation)
        automation.action_timeout_ms = 1234
        automation._answer_inputs_in_card = lambda owner: [
            FakeAnswer(owner, index) for index in range(len(owner.answers))
        ]
        automation._type_input_value = (
            lambda input_node, value, _description: card.answers.__setitem__(
                input_node.index,
                value,
            )
        )
        automation._wait_until = (
            lambda predicate, _message, **_kwargs: self.assertTrue(predicate())
        )

        automation._fill_answer_rows(card, ["new-answer"], "模仿朗读")

        self.assertEqual(card.answers, ["new-answer"])


if __name__ == "__main__":
    unittest.main()
