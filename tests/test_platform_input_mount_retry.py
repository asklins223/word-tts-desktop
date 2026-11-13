"""Regression coverage for lazy-panel mount failures.

A swallowed navigation click used to surface only as "实际 0 个题卡" after a
minute, with no page facts.  These tests pin the bounded retry, the card
heading forms the page actually renders, and the page state that now travels
with the durable error.
"""

from unittest.mock import Mock

import pytest

from platform_entry.adapter.automation import PlatformInputPageAutomation
from platform_entry.adapter.errors import PlatformInputUiError


class _Node:
    def __init__(self, text, class_name="", visible=True):
        self._text = text
        self._class = class_name
        self._visible = visible

    def is_visible(self):
        return self._visible

    def inner_text(self, timeout=0):
        return self._text

    def get_attribute(self, name):
        return self._class if name == "class" else ""


class _Nodes:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def count(self):
        return len(self._nodes)

    def nth(self, index):
        return self._nodes[index]


class _Page:
    def __init__(self, *, boxes=(), part_names=(), panels=(), card_labels=()):
        self._by_selector = {
            ".qBox": list(boxes),
            ".partName:visible": list(part_names),
            ".rightMiddleContent:visible": list(panels),
        }
        self._card_labels = list(card_labels)
        self.url = "https://admin.example/#/resource/exam"

    def locator(self, selector):
        return _Nodes(self._by_selector.get(selector, ()))

    def get_by_text(self, pattern):
        nodes = [_Node(text) for text in self._card_labels]
        return _Nodes(
            [
                node
                for node in nodes
                if pattern.fullmatch(node._text.replace(" ", "")) is not None
            ]
        )

    def wait_for_timeout(self, _milliseconds):
        return None


def _owner(page=None):
    owner = object.__new__(PlatformInputPageAutomation)
    owner.page = page or Mock()
    return owner


def _install_waits(owner, state):
    """Evaluate each mount wait once against the shared card count."""

    timeouts = []

    def wait(predicate, message, *, timeout_seconds=30, interval_ms=200):
        timeouts.append(timeout_seconds)
        if not predicate():
            raise PlatformInputUiError(message)

    owner._wait_until = wait
    owner._question_cards = lambda _kind: [Mock()] * state["cards"]
    return timeouts


def test_swallowed_click_is_repeated_once_when_the_entry_is_not_selected():
    owner = _owner()
    state = {"cards": 0}
    clicks = []

    def activate(*_args, **_kwargs):
        clicks.append("click")
        if len(clicks) == 2:
            state["cards"] = 1
        return "第12题(录音题)"

    owner._activate_outline_section = activate
    owner._outline_entry_selected = lambda _text: False
    owner._labeled_text_editors = lambda _label: []
    timeouts = _install_waits(owner, state)

    cards, editors = owner._activate_group_section("信息转述及询问", "录音题", 1, 0)

    assert len(clicks) == 2
    assert len(cards) == 1 and editors == []
    assert timeouts == [15.0, 45.0]


def test_selected_entry_keeps_waiting_without_clicking_again():
    owner = _owner()
    state = {"cards": 0}
    owner._activate_outline_section = Mock(return_value="第12题(录音题)")
    owner._outline_entry_selected = lambda _text: True
    owner._page_mount_state = lambda _kind, _target: "导航已选中；右栏[]"
    _install_waits(owner, state)

    with pytest.raises(PlatformInputUiError) as excinfo:
        owner._activate_group_section("信息转述及询问", "录音题", 1, 0)

    message = str(excinfo.value)
    assert owner._activate_outline_section.call_count == 1
    assert "期望 1 个录音题，实际 0 个，导航“第12题(录音题)”" in message
    assert "已再次点击" not in message
    assert "页面状态：导航已选中；右栏[]" in message


def test_partial_mount_is_not_clicked_again_and_reports_the_real_count():
    owner = _owner()
    state = {"cards": 2}
    owner._activate_outline_section = Mock(return_value="第3题(选择题)")
    owner._outline_entry_selected = lambda _text: False
    owner._page_mount_state = lambda _kind, _target: ""
    _install_waits(owner, state)

    with pytest.raises(PlatformInputUiError) as excinfo:
        owner._activate_group_section("听后选择", "选择题", 3, 0)

    assert owner._activate_outline_section.call_count == 1
    assert "期望 3 个选择题，实际 2 个" in str(excinfo.value)


def test_retry_is_named_in_the_durable_message_when_it_also_fails():
    owner = _owner()
    state = {"cards": 0}
    owner._activate_outline_section = Mock(return_value="第12题(录音题)")
    owner._outline_entry_selected = lambda _text: False
    owner._page_mount_state = lambda _kind, _target: "导航未选中；右栏[]"
    _install_waits(owner, state)

    with pytest.raises(PlatformInputUiError) as excinfo:
        owner._activate_group_section("信息转述及询问", "录音题", 1, 0)

    message = str(excinfo.value)
    assert owner._activate_outline_section.call_count == 2
    assert "已再次点击该导航仍未挂载" in message
    assert "页面状态：导航未选中；右栏[]" in message


def test_numbered_card_heading_counts_only_inside_the_right_panel():
    owner = _owner()
    owner.page = Mock()
    headings = Mock()
    headings.evaluate_all = Mock(
        return_value=[
            {"index": 0, "text": "第12题(录音题)", "visible": True, "inRightPanel": True},
            {"index": 1, "text": "第12题(录音题)", "visible": True, "inRightPanel": False},
        ]
    )
    owner.page.get_by_text = Mock(return_value=headings)
    owner._question_card_from_heading = Mock(return_value="card")

    assert owner._question_cards("录音题") == ["card"]
    assert owner._question_card_from_heading.call_count == 1


def test_plain_card_heading_keeps_working_outside_the_panel_marker():
    owner = _owner()
    owner.page = Mock()
    headings = Mock()
    headings.evaluate_all = Mock(
        return_value=[
            {"index": 0, "text": "小题 12(录音题)", "visible": True, "inRightPanel": False},
        ]
    )
    owner.page.get_by_text = Mock(return_value=headings)
    owner._question_card_from_heading = Mock(return_value="card")

    assert owner._question_cards("录音题") == ["card"]


def test_navigation_only_headings_are_not_counted_as_cards():
    owner = _owner()
    owner.page = Mock()
    headings = Mock()
    headings.evaluate_all = Mock(
        return_value=[
            {"index": 0, "text": "第12题(录音题)", "visible": True, "inRightPanel": False},
            {"index": 1, "text": "第1题(录音题)", "visible": True, "inRightPanel": False},
        ]
    )
    owner.page.get_by_text = Mock(return_value=headings)
    owner._question_card_from_heading = Mock(return_value="card")

    assert owner._question_cards("录音题") == []
    owner._question_card_from_heading.assert_not_called()


def test_outline_entry_selection_reads_the_page_own_selected_class():
    selected = _owner(
        _Page(boxes=[_Node("第12题(录音题)", "w-full qBox isSelectBox")])
    )
    assert selected._outline_entry_selected("第12题(录音题)") is True

    plain = _owner(_Page(boxes=[_Node("第12题(录音题)", "w-full qBox")]))
    assert plain._outline_entry_selected("第12题(录音题)") is False

    unknown = _owner(_Page(boxes=[_Node("第11题(录音题)", "qBox")]))
    assert unknown._outline_entry_selected("第12题(录音题)") is None


def test_mount_state_reports_navigation_sections_and_visible_cards():
    owner = _owner(
        _Page(
            boxes=[_Node("第12题(录音题)", "w-full qBox")],
            part_names=[_Node("三、信息转述及询问。", "partName")],
            panels=[_Node("信息获取（共10小题） 页面1 页面2", "rightMiddleContent")],
            card_labels=["小题 8(录音题)", "小题 9(录音题)"],
        )
    )

    state = owner._page_mount_state("录音题", "第12题(录音题)")

    assert state == (
        "导航未选中；大题[三、信息转述及询问。]；"
        "右栏[信息获取（共10小题） 页面1 页面2]；"
        "可见录音题标题[小题8(录音题)、小题9(录音题)]"
    )


def test_mount_state_without_a_page_stays_empty():
    owner = object.__new__(PlatformInputPageAutomation)
    owner.page = None

    assert owner._page_mount_state("录音题", "第12题(录音题)") == ""


def test_alternate_page_shape_is_returned_instead_of_a_partial_mount_error():
    owner = _owner()
    state = {"cards": 1}
    owner._activate_outline_section = Mock(return_value="第1题(录音题)")
    owner._outline_entry_selected = lambda _text: True
    owner._labeled_text_editors = lambda _label: [Mock()]
    _install_waits(owner, state)

    cards, editors = owner._activate_group_section(
        "信息转述及询问",
        "录音题",
        3,
        1,
        alternate_ready=lambda: True,
    )

    assert len(cards) == 1
    assert len(editors) == 1


def test_next_recording_outline_target_follows_the_selected_entry():
    owner = _owner(
        _Page(boxes=[
            _Node("第1题(录音题)", "w-full qBox isSelectBox"),
            _Node("第2题(录音题)", "w-full qBox"),
            _Node("第3题(录音题)", "w-full qBox"),
        ])
    )

    assert owner._next_recording_outline_target() == ("第2题(录音题)", 0)


def test_next_recording_outline_target_keeps_repeated_entry_occurrences():
    owner = _owner(
        _Page(boxes=[
            _Node("第1题(录音题)", "w-full qBox"),
            _Node("第1题(录音题)", "w-full qBox"),
            _Node("第2题(录音题)", "w-full qBox isSelectBox"),
            _Node("第2题(录音题)", "w-full qBox"),
            _Node("第3题(录音题)", "w-full qBox"),
        ])
    )

    assert owner._next_recording_outline_target() == ("第2题(录音题)", 1)


def test_split_retelling_shape_needs_one_card_and_two_right_pages():
    owner = _owner(
        _Page(panels=[
            _Node("页面1 小节指导文字", "rightMiddleContent"),
            _Node("页面2 考点", "rightMiddleContent"),
        ])
    )
    owner._question_cards = lambda _kind: [Mock()]

    ready = owner._split_retelling_shape_ready(1)
    assert ready() is False
    assert ready() is False
    assert ready() is True

    assert owner._split_retelling_shape_ready(2)() is False
