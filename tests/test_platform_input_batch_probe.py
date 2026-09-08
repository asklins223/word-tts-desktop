"""Coverage for the batch-probe fast paths added to the page adapters.

The visible-page tests use lightweight shims without ``evaluate_all`` /
``evaluate``, so every batch fast path silently falls back to the original
loop there.  These tests stub the batch APIs to drive the fast paths
themselves, including the engine-side confirmation and the fallback when the
in-browser probe disagrees with the stubbed engine calls.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from platform_entry.adapter.page_cards import (
        PlatformInputCardMixin,
        _QUESTION_CARD_LEVEL_JS,
    )
    from platform_entry.adapter.page_forms import PlatformInputFormMixin
    from platform_entry.adapter.page_navigation import PlatformInputNavigationMixin
    from platform_entry.adapter.page_shared import _closest_visible_level
    from platform_entry.adapter.page_shared import _closest_count_level
    from platform_entry.adapter.page_shared import _ancestor_at_level
    from platform_entry.adapter.errors import PlatformInputUiError
except (ImportError, SyntaxError) as exc:
    raise unittest.SkipTest("可选页面录入适配器未解锁，跳过批量探测测试") from exc


class _FakeNode:
    """Element stub with controllable visibility, text, and evaluate."""

    def __init__(
        self,
        *,
        visible: bool = True,
        text: str = "",
        evaluate_result: object = None,
        evaluate_error: Exception | None = None,
    ) -> None:
        self.visible = visible
        self.text = text
        self._evaluate_result = evaluate_result
        self._evaluate_error = evaluate_error
        self.evaluate_calls = 0
        self.locator_calls: list[str] = []

    def is_visible(self) -> bool:
        return self.visible

    def inner_text(self, timeout: object = None) -> str:
        return self.text

    def locator(self, selector: str) -> "_FakeNode":
        self.locator_calls.append(selector)
        return self

    def evaluate(self, script: str, arg: object = None) -> object:
        self.evaluate_calls += 1
        if self._evaluate_error is not None:
            raise self._evaluate_error
        return self._evaluate_result


class _FakeFirstVisibleLocator:
    """Locator stub driving PlatformInputNavigationMixin._first_visible."""

    def __init__(
        self,
        nodes: list[_FakeNode],
        *,
        evaluate_all_result: object = None,
        evaluate_all_error: Exception | None = None,
    ) -> None:
        self._nodes = nodes
        self._evaluate_all_result = evaluate_all_result
        self._evaluate_all_error = evaluate_all_error
        self.evaluate_all_calls = 0
        self.count_calls = 0

    def evaluate_all(self, script: str) -> object:
        self.evaluate_all_calls += 1
        if self._evaluate_all_error is not None:
            raise self._evaluate_all_error
        return self._evaluate_all_result

    def nth(self, index: int) -> _FakeNode:
        return self._nodes[index]

    def count(self) -> int:
        self.count_calls += 1
        return len(self._nodes)


class FirstVisibleBatchTests(unittest.TestCase):
    def _run(self, locator: object) -> object:
        owner = SimpleNamespace()
        return PlatformInputNavigationMixin._first_visible(owner, locator)

    def test_batch_hit_returns_confirmed_candidate(self) -> None:
        nodes = [
            _FakeNode(visible=False),
            _FakeNode(visible=True),
            _FakeNode(visible=True),
        ]
        locator = _FakeFirstVisibleLocator(nodes, evaluate_all_result=1)
        self.assertIs(self._run(locator), nodes[1])
        self.assertEqual(locator.evaluate_all_calls, 1)
        self.assertEqual(locator.count_calls, 0)

    def test_batch_reject_rescans_from_the_first_element(self) -> None:
        # The probe claims index 2 is visible but the engine disagrees; the
        # rescan must start at 0 so earlier engine-visible candidates still
        # win, exactly like the plain loop.
        nodes = [
            _FakeNode(visible=True),
            _FakeNode(visible=False),
            _FakeNode(visible=False),
        ]
        locator = _FakeFirstVisibleLocator(nodes, evaluate_all_result=2)
        self.assertIs(self._run(locator), nodes[0])

    def test_batch_negative_result_still_runs_plain_loop(self) -> None:
        nodes = [_FakeNode(visible=False), _FakeNode(visible=True)]
        locator = _FakeFirstVisibleLocator(nodes, evaluate_all_result=-1)
        self.assertIs(self._run(locator), nodes[1])
        self.assertEqual(locator.count_calls, 1)

    def test_probe_error_falls_back_to_plain_loop(self) -> None:
        nodes = [_FakeNode(visible=True)]
        locator = _FakeFirstVisibleLocator(
            nodes,
            evaluate_all_error=RuntimeError("page navigating"),
        )
        self.assertIs(self._run(locator), nodes[0])

    def test_locator_without_evaluate_all_uses_plain_loop(self) -> None:
        nodes = [_FakeNode(visible=False), _FakeNode(visible=True)]

        class _PlainLocator:
            def __init__(self, nodes: list[_FakeNode]) -> None:
                self._nodes = nodes

            def nth(self, index: int) -> _FakeNode:
                return self._nodes[index]

            def count(self) -> int:
                return len(self._nodes)

        self.assertIs(self._run(_PlainLocator(nodes)), nodes[1])


class _FakeOptionsLocator:
    """Locator stub driving PlatformInputFormMixin._find_dropdown_option."""

    def __init__(
        self,
        *,
        batch_texts: object = None,
        nodes: list[_FakeNode] | None = None,
    ) -> None:
        self._batch_texts = batch_texts
        self._nodes = nodes or []
        self.evaluate_all_calls = 0
        self.count_calls = 0

    def evaluate_all(self, script: str) -> object:
        self.evaluate_all_calls += 1
        if isinstance(self._batch_texts, Exception):
            raise self._batch_texts
        return self._batch_texts

    def nth(self, index: int) -> _FakeNode:
        return self._nodes[index]

    def count(self) -> int:
        self.count_calls += 1
        return len(self._nodes)


class _FakeDropdownPage:
    def __init__(self, selectors: dict[str, _FakeOptionsLocator]) -> None:
        self._selectors = selectors

    def locator(self, selector: str) -> _FakeOptionsLocator:
        return self._selectors[selector]


class FindDropdownOptionBatchTests(unittest.TestCase):
    def test_batch_snapshot_match_is_confirmed_and_returned(self) -> None:
        options = _FakeOptionsLocator(
            batch_texts=["广东省", "江苏省"],
            nodes=[
                _FakeNode(text="广东省"),
                _FakeNode(text="江苏省"),
            ],
        )
        page = _FakeDropdownPage(
            {
                ".el-select-dropdown:visible .el-select-dropdown__item:visible": options,
                '[role="option"]:visible': _FakeOptionsLocator(),
            }
        )
        owner = SimpleNamespace(page=page)
        option = PlatformInputFormMixin._find_dropdown_option(owner, "江苏省")
        self.assertIs(option, options._nodes[1])
        self.assertEqual(options.evaluate_all_calls, 1)
        self.assertEqual(options.count_calls, 0)

    def test_stale_snapshot_skips_stale_selector(self) -> None:
        # The batch snapshot claims a match, but the engine re-read no longer
        # agrees (dropdown re-rendered).  The stale selector is skipped and
        # the remaining selectors decide the answer.
        options = _FakeOptionsLocator(
            batch_texts=["江苏省"],
            nodes=[_FakeNode(text="浙江省")],
        )
        page = _FakeDropdownPage(
            {
                ".el-select-dropdown:visible .el-select-dropdown__item:visible": options,
                '[role="option"]:visible': _FakeOptionsLocator(),
            }
        )
        owner = SimpleNamespace(page=page)
        self.assertIsNone(
            PlatformInputFormMixin._find_dropdown_option(owner, "江苏省")
        )
        self.assertEqual(options.count_calls, 0)

    def test_batch_miss_skips_to_next_selector(self) -> None:
        first = _FakeOptionsLocator(batch_texts=["广东省"])
        second = _FakeOptionsLocator(
            batch_texts=["江苏省"],
            nodes=[_FakeNode(text="江苏省")],
        )
        page = _FakeDropdownPage(
            {
                ".el-select-dropdown:visible .el-select-dropdown__item:visible": first,
                '[role="option"]:visible': second,
            }
        )
        owner = SimpleNamespace(page=page)
        option = PlatformInputFormMixin._find_dropdown_option(owner, "江苏省")
        self.assertIs(option, second._nodes[0])
        self.assertEqual(first.count_calls, 0)

    def test_probe_error_uses_plain_loop(self) -> None:
        options = _FakeOptionsLocator(
            batch_texts=RuntimeError("nav"),
            nodes=[_FakeNode(text="江苏省")],
        )
        page = _FakeDropdownPage(
            {
                ".el-select-dropdown:visible .el-select-dropdown__item:visible": options,
                '[role="option"]:visible': _FakeOptionsLocator(),
            }
        )
        owner = SimpleNamespace(page=page)
        option = PlatformInputFormMixin._find_dropdown_option(owner, "江苏省")
        self.assertIs(option, options._nodes[0])
        self.assertEqual(options.count_calls, 1)


class ClosestVisibleLevelTests(unittest.TestCase):
    def test_returns_probe_level(self) -> None:
        node = _FakeNode(evaluate_result=3)
        self.assertEqual(_closest_visible_level(node, "input:visible", max_level=7), 3)

    def test_negative_and_error_results_become_minus_one(self) -> None:
        self.assertEqual(
            _closest_visible_level(
                _FakeNode(evaluate_result=-1), "input:visible", max_level=7
            ),
            -1,
        )
        self.assertEqual(
            _closest_visible_level(
                _FakeNode(evaluate_error=RuntimeError("x")),
                "input:visible",
                max_level=7,
            ),
            -1,
        )

    def test_node_without_evaluate_returns_minus_one(self) -> None:
        self.assertEqual(
            _closest_visible_level(object(), "input:visible", max_level=7), -1
        )

    def test_ancestor_at_level_hop_count(self) -> None:
        node = _FakeNode()
        self.assertIs(_ancestor_at_level(node, 0), node)
        self.assertEqual(node.locator_calls, [])
        _ancestor_at_level(node, 2)
        self.assertEqual(node.locator_calls, ["xpath=..", "xpath=.."])

    def test_hidden_control_probe_uses_browser_ancestor_walk(self) -> None:
        node = _FakeNode(evaluate_result=4)
        self.assertEqual(
            _closest_count_level(
                node,
                'input[type="file"]',
                max_level=8,
            ),
            4,
        )
        self.assertEqual(node.evaluate_calls, 1)


class _FakeAncestorLocator:
    """Chainable ancestor stub for the labeled-editor batch paths."""

    def __init__(self, *, matches: int) -> None:
        self._matches = matches
        self.count_calls = 0

    def locator(self, selector: str) -> "_FakeAncestorLocator":
        return self

    def count(self) -> int:
        self.count_calls += 1
        return self._matches

    @property
    def first(self) -> object:
        return self


class _FakeClimbNode:
    def __init__(
        self,
        *,
        visible: bool = True,
        evaluate_result: object,
        ancestor_matches: int,
    ) -> None:
        self.visible = visible
        self._evaluate_result = evaluate_result
        self._ancestor = _FakeAncestorLocator(matches=ancestor_matches)
        self.evaluate_calls = 0

    def is_visible(self) -> bool:
        return self.visible

    def evaluate(self, script: str, arg: object = None) -> object:
        self.evaluate_calls += 1
        return self._evaluate_result

    def locator(self, selector: str) -> object:
        return self._ancestor


class _FakeScope:
    def __init__(self, nodes: list[_FakeClimbNode]) -> None:
        self._nodes = nodes

    def get_by_text(self, label: str, exact: bool = False) -> object:
        return SimpleNamespace(count=lambda: len(self._nodes), nth=self._nodes.__getitem__)


class LabeledEditorBatchTests(unittest.TestCase):
    def _run(self, scope: object) -> object:
        owner = SimpleNamespace()
        return PlatformInputCardMixin._labeled_text_editor_in_scope(
            owner, scope, "题干"
        )

    def test_batch_hit_is_confirmed_with_engine_count(self) -> None:
        node = _FakeClimbNode(evaluate_result=2, ancestor_matches=1)
        editor = self._run(_FakeScope([node]))
        self.assertIsNotNone(editor)
        self.assertEqual(node.evaluate_calls, 1)
        self.assertEqual(node._ancestor.count_calls, 1)

    def test_batch_confirm_fails_and_plain_loop_misses(self) -> None:
        # The probe points at a level whose engine count is not 1; the plain
        # loop then runs over all 8 levels and finds nothing, so the scope
        # answer is None.  1 confirm + 8 loop levels = 9 engine counts.
        node = _FakeClimbNode(evaluate_result=2, ancestor_matches=3)
        self.assertIsNone(self._run(_FakeScope([node])))
        self.assertEqual(node._ancestor.count_calls, 9)

    def test_probe_miss_runs_plain_loop(self) -> None:
        node = _FakeClimbNode(evaluate_result=-1, ancestor_matches=0)
        self.assertIsNone(self._run(_FakeScope([node])))


class _FakeKeyboard:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.insert_text_calls: list[str] = []
        self._error = error

    def insert_text(self, text: str) -> None:
        self.insert_text_calls.append(text)
        if self._error is not None:
            raise self._error


class _FakeRichEditor:
    def __init__(self, readback_values: list[str]) -> None:
        self._readback_values = list(readback_values)
        self.type_calls: list[str] = []
        self.click_calls = 0

    def scroll_into_view_if_needed(self, timeout: object = None) -> None:
        return None

    def click(self, timeout: object = None) -> None:
        self.click_calls += 1

    def press(self, key: str, timeout: object = None) -> None:
        return None

    def type(self, text: str, timeout: object = None) -> None:
        self.type_calls.append(text)

    def inner_text(self, timeout: object = None) -> str:
        return self._readback_values.pop(0)


class _PageWithoutKeyboard:
    pass


class ReplaceRichTextTests(unittest.TestCase):
    def _run(self, page: object, editor: object) -> None:
        owner = SimpleNamespace(page=page, action_timeout_ms=5_000)
        PlatformInputCardMixin._replace_rich_text(owner, editor, "目标内容")

    def test_insert_text_first_and_type_fallback_on_mismatch(self) -> None:
        keyboard = _FakeKeyboard()
        editor = _FakeRichEditor(readback_values=["旧内容", "目标内容"])
        page = SimpleNamespace(keyboard=keyboard)
        self._run(page, editor)
        self.assertEqual(keyboard.insert_text_calls, ["目标内容"])
        # The char-by-char path replays exactly once after the mismatch.
        self.assertEqual(editor.type_calls, ["目标内容"])
        self.assertEqual(editor.click_calls, 2)

    def test_missing_keyboard_attribute_still_fills_via_type(self) -> None:
        editor = _FakeRichEditor(readback_values=["目标内容"])
        self._run(_PageWithoutKeyboard(), editor)
        self.assertEqual(editor.type_calls, ["目标内容"])
        self.assertEqual(editor.click_calls, 1)

    def test_persistent_mismatch_raises_ui_error(self) -> None:
        keyboard = _FakeKeyboard()
        editor = _FakeRichEditor(readback_values=["旧内容", "仍然不符"])
        page = SimpleNamespace(keyboard=keyboard)
        owner = SimpleNamespace(page=page, action_timeout_ms=5_000)
        with self.assertRaises(PlatformInputUiError):
            PlatformInputCardMixin._replace_rich_text(
                owner, editor, "目标内容"
            )


class QuestionCardBatchProbeTests(unittest.TestCase):
    def test_probe_script_is_self_contained(self) -> None:
        # The template must not reference anything outside its own scope.
        for token in ("querySelectorAll", "parentElement", "添加选项|添加答案"):
            self.assertIn(token, _QUESTION_CARD_LEVEL_JS)


if __name__ == "__main__":
    unittest.main()
