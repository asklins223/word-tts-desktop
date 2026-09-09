from __future__ import annotations

import unittest
from unittest import mock

from platform_entry.adapter import textbook_page


class TextbookPageInputTests(unittest.TestCase):
    def test_page_text_removes_selected_role_prefix(self) -> None:
        self.assertEqual(
            textbook_page._textbook_page_text(
                "Reporter： How has life changed?\nThe follow-up sentence.",
                "Reporter",
            ),
            "How has life changed?\nThe follow-up sentence.",
        )
        self.assertEqual(
            textbook_page._textbook_page_text("An idea hits me: why?", "Reporter"),
            "An idea hits me: why?",
        )

    def test_role_key_normalizes_whitespace(self) -> None:
        self.assertEqual(
            textbook_page._role_key("  Teng   Fei "),
            "teng fei",
        )

    def test_role_volume_match_uses_version_grade_and_volume(self) -> None:
        class Row:
            def inner_text(self, **_kwargs: object) -> str:
                return "人教版 · 初中 · 七年级 · 上册"

        record = {
            "version": "人教版",
            "stage": "初中",
            "grade": "七年级",
            "volume": "上册",
        }
        self.assertTrue(textbook_page._role_volume_matches(Row(), record))
        self.assertFalse(
            textbook_page._role_volume_matches(
                Row(),
                {
                    "version": "人教版",
                    "stage": "高中",
                    "grade": "七年级",
                    "volume": "上册",
                },
            )
        )

    def test_open_role_list_waits_for_data_response_before_matching(self) -> None:
        """页面外壳先出现时，不能把异步中的空表误判成无册别。"""

        class Request:
            method = "GET"

        class Response:
            request = Request()
            url = (
                f"{textbook_page.API_BASE_URL}"
                f"{textbook_page.TEXT_ROLE_PAGE_PATH}?pageNo=1&pageSize=10"
            )
            status = 200

        class Page:
            url = textbook_page.TEXT_ROLE_URL

            def __init__(self) -> None:
                self.listeners: dict[str, list[object]] = {}
                self.wait_count = 0

            def on(self, event: str, listener: object) -> None:
                self.listeners.setdefault(event, []).append(listener)

            def remove_listener(self, event: str, listener: object) -> None:
                self.listeners[event].remove(listener)

            def goto(self, *_args: object, **_kwargs: object) -> None:
                return None

            def wait_for_timeout(self, _milliseconds: int) -> None:
                self.wait_count += 1
                for listener in list(self.listeners.get("response", [])):
                    listener(Response())

        page = Page()
        with mock.patch.object(textbook_page, "_configure_page_timeouts"), mock.patch.object(
            textbook_page, "_role_list_ready", return_value=True
        ), mock.patch.object(textbook_page, "_visible_exact", return_value=None), mock.patch.object(
            textbook_page, "_body_text", return_value=""
        ), mock.patch.object(
            textbook_page,
            "_role_list_data_ready",
            side_effect=lambda _page: page.wait_count > 0,
        ):
            textbook_page._open_role_list(page, 1)

        self.assertEqual(page.wait_count, 1)
        self.assertEqual(page.listeners.get("response"), [])

    def test_open_role_list_accepts_rendered_rows_when_response_was_cached(self) -> None:
        class Page:
            url = textbook_page.TEXT_ROLE_URL

            def __init__(self) -> None:
                self.listeners: dict[str, list[object]] = {}

            def on(self, event: str, listener: object) -> None:
                self.listeners.setdefault(event, []).append(listener)

            def remove_listener(self, event: str, listener: object) -> None:
                self.listeners[event].remove(listener)

            def goto(self, *_args: object, **_kwargs: object) -> None:
                return None

            def wait_for_timeout(self, _milliseconds: int) -> None:
                raise AssertionError("已渲染角色行时不应继续等待列表响应")

        page = Page()
        with mock.patch.object(textbook_page, "_configure_page_timeouts"), mock.patch.object(
            textbook_page, "_role_list_ready", return_value=True
        ), mock.patch.object(
            textbook_page, "_role_list_data_ready", return_value=True
        ), mock.patch.object(
            textbook_page, "_role_list_rows_ready", return_value=True
        ), mock.patch.object(textbook_page, "_visible_exact", return_value=None), mock.patch.object(
            textbook_page, "_body_text", return_value=""
        ):
            textbook_page._open_role_list(page, 1)

        self.assertEqual(page.listeners.get("response"), [])

    def test_wait_for_role_list_accepts_rendered_rows_without_new_response(self) -> None:
        """保存后复用缓存列表时，不能因没有新的 GET 响应而死等。"""

        class Page:
            def __init__(self) -> None:
                self.listeners: dict[str, list[object]] = {}

            def on(self, event: str, listener: object) -> None:
                self.listeners.setdefault(event, []).append(listener)

            def remove_listener(self, event: str, listener: object) -> None:
                self.listeners[event].remove(listener)

            def wait_for_timeout(self, _milliseconds: int) -> None:
                raise AssertionError("已回到有数据的角色列表时不应继续等待 GET")

        page = Page()
        observer = textbook_page.TextRoleListResponseObserver(page)
        try:
            with mock.patch.object(textbook_page, "_role_list_ready", return_value=True), mock.patch.object(
                textbook_page, "_role_list_data_ready", return_value=True
            ), mock.patch.object(textbook_page, "_role_list_rows_ready", return_value=True):
                textbook_page._wait_for_role_list(page, observer=observer)
        finally:
            observer.close()

        self.assertEqual(page.listeners.get("response"), [])

    def test_replace_editor_uses_single_insert_before_typing_fallback(self) -> None:
        class Keyboard:
            def __init__(self) -> None:
                self.inserted: list[str] = []

            def insert_text(self, value: str) -> None:
                self.inserted.append(value)

        class Editor:
            def __init__(self, readbacks: list[str]) -> None:
                self.readbacks = list(readbacks)
                self.typed: list[str] = []

            def scroll_into_view_if_needed(self, **_kwargs: object) -> None:
                return None

            def click(self, **_kwargs: object) -> None:
                return None

            def press(self, _key: str, **_kwargs: object) -> None:
                return None

            def type(self, value: str, **_kwargs: object) -> None:
                self.typed.append(value)

            def inner_text(self, **_kwargs: object) -> str:
                return self.readbacks.pop(0)

        page = type("Page", (), {"keyboard": Keyboard()})()
        editor = Editor(["一段很长的课文"])

        textbook_page._replace_editor(page, editor, "一段很长的课文", "原文")

        self.assertEqual(page.keyboard.inserted, ["一段很长的课文"])
        self.assertEqual(editor.typed, [])

    def test_replace_editor_replays_typing_only_after_mismatch(self) -> None:
        class Keyboard:
            def insert_text(self, _value: str) -> None:
                return None

        class Editor:
            def __init__(self) -> None:
                self.readbacks = ["旧内容", "目标内容"]
                self.typed: list[str] = []

            def scroll_into_view_if_needed(self, **_kwargs: object) -> None:
                return None

            def click(self, **_kwargs: object) -> None:
                return None

            def press(self, _key: str, **_kwargs: object) -> None:
                return None

            def type(self, value: str, **_kwargs: object) -> None:
                self.typed.append(value)

            def inner_text(self, **_kwargs: object) -> str:
                return self.readbacks.pop(0)

        editor = Editor()
        page = type("Page", (), {"keyboard": Keyboard()})()

        textbook_page._replace_editor(page, editor, "目标内容", "原文")

        self.assertEqual(editor.typed, ["目标内容"])

    def test_select_option_accepts_platform_case_difference_on_readback(self) -> None:
        class Locator:
            def __init__(self, *, count: int = 0, text: str = "") -> None:
                self._count = count
                self._text = text

            def count(self) -> int:
                return self._count

            def nth(self, _index: int) -> "Locator":
                return self

            def is_visible(self) -> bool:
                return True

            def scroll_into_view_if_needed(self, **_kwargs: object) -> None:
                return None

            def click(self, **_kwargs: object) -> None:
                return None

            def inner_text(self, **_kwargs: object) -> str:
                return self._text

            def filter(self, **_kwargs: object) -> "Locator":
                return Locator()

        class Page:
            def locator(self, selector: str) -> Locator:
                if selector == ".el-select__wrapper:visible":
                    return Locator(count=1, text="Reading plus")
                return Locator(count=1, text="Reading plus")

            def get_by_role(self, *_args: object, **_kwargs: object) -> Locator:
                return Locator()

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        textbook_page._select_option(Page(), 0, "Reading Plus")

    def test_fill_card_role_clicks_real_multi_select_role_buttons(self) -> None:
        class Button:
            def __init__(self, text: str) -> None:
                self.text = text
                self.class_name = "characterItem px-4 characterNormal"
                self.click_count = 0

            def get_attribute(self, name: str) -> str:
                return self.class_name if name == "class" else ""

            def click(self, **_kwargs: object) -> None:
                self.click_count += 1
                self.class_name = "characterItem px-4 characterSelected"

        class Buttons:
            def __init__(self, buttons: list[Button]) -> None:
                self.buttons = buttons

            def count(self) -> int:
                return len(self.buttons)

            def filter(self, *, has_text: object) -> "Buttons":
                pattern = has_text
                return Buttons([
                    button
                    for button in self.buttons
                    if getattr(pattern, "search", lambda _value: None)(button.text)
                ])

            @property
            def last(self) -> Button:
                return self.buttons[-1]

        class Card:
            def __init__(self, buttons: Buttons) -> None:
                self.buttons = buttons

            def locator(self, selector: str) -> Buttons:
                if selector in {".characterItem:visible", ".characterItem"}:
                    return self.buttons
                return Buttons([])

        class Page:
            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        teng_fei = Button("Teng Fei")
        peter = Button("Peter")
        textbook_page._fill_card_role(
            Page(),
            Card(Buttons([teng_fei, peter])),
            ["Teng Fei", "Peter"],
            0,
        )

        self.assertEqual(teng_fei.click_count, 1)
        self.assertEqual(peter.click_count, 1)
        self.assertIn("characterSelected", teng_fei.class_name)
        self.assertIn("characterSelected", peter.class_name)

    def test_fill_card_role_waits_for_async_role_buttons(self) -> None:
        class Button:
            def __init__(self, text: str) -> None:
                self.text = text
                self.class_name = "characterItem px-4 characterNormal"

            def get_attribute(self, name: str) -> str:
                return self.class_name if name == "class" else ""

            def click(self, **_kwargs: object) -> None:
                self.class_name = "characterItem px-4 characterSelected"

        class Buttons:
            def __init__(self, buttons: list[Button]) -> None:
                self.buttons = buttons

            def count(self) -> int:
                return len(self.buttons)

            def filter(self, *, has_text: object) -> "Buttons":
                pattern = has_text
                return Buttons([
                    button
                    for button in self.buttons
                    if getattr(pattern, "search", lambda _value: None)(button.text)
                ])

            @property
            def last(self) -> Button:
                return self.buttons[-1]

        class Card:
            def __init__(self, buttons: Buttons) -> None:
                self.buttons = buttons
                self.locator_calls = 0

            def locator(self, selector: str) -> Buttons:
                if selector in {".characterItem:visible", ".characterItem"}:
                    self.locator_calls += 1
                    if self.locator_calls < 3:
                        return Buttons([])
                    return self.buttons
                return Buttons([])

        class Page:
            def __init__(self) -> None:
                self.wait_count = 0

            def wait_for_timeout(self, _milliseconds: int) -> None:
                self.wait_count += 1

        page = Page()
        card = Card(Buttons([Button("Ms Gao")]))
        textbook_page._fill_card_role(page, card, ["Ms Gao"], 0)

        self.assertGreaterEqual(page.wait_count, 1)


if __name__ == "__main__":
    unittest.main()
