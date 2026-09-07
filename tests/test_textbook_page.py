from __future__ import annotations

import unittest

from platform_entry.adapter import textbook_page


class TextbookPageInputTests(unittest.TestCase):
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
                self.selected = 0

            def select_text(self, **_kwargs: object) -> None:
                self.selected += 1

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
        self.assertEqual(editor.selected, 1)

    def test_replace_editor_replays_typing_only_after_mismatch(self) -> None:
        class Keyboard:
            def insert_text(self, _value: str) -> None:
                return None

        class Editor:
            def __init__(self) -> None:
                self.readbacks = ["旧内容", "目标内容"]
                self.typed: list[str] = []
                self.selected = 0

            def select_text(self, **_kwargs: object) -> None:
                self.selected += 1

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
        self.assertEqual(editor.selected, 1)

    def test_select_option_accepts_platform_case_difference_on_readback(self) -> None:
        class Locator:
            def __init__(self, *, count: int = 0, text: str = "") -> None:
                self._count = count
                self._text = text

            @property
            def last(self) -> "Locator":
                return self

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

            def wait_for(self, **_kwargs: object) -> None:
                if not self._count:
                    raise RuntimeError("not visible")

            def inner_text(self, **_kwargs: object) -> str:
                return self._text

            def filter(self, **kwargs: object) -> "Locator":
                pattern = kwargs.get("has_text")
                matches = bool(pattern.search(self._text)) if pattern else True
                return Locator(count=self._count if matches else 0, text=self._text)

        class Page:
            def __init__(self) -> None:
                self.timeout_calls = 0

            def locator(self, selector: str) -> Locator:
                if selector == ".el-select__wrapper:visible":
                    return Locator(count=1, text="Reading plus")
                return Locator(count=1, text="Reading plus")

            def get_by_role(self, *_args: object, **_kwargs: object) -> Locator:
                return Locator()

            def wait_for_timeout(self, _milliseconds: int) -> None:
                self.timeout_calls += 1

        page = Page()
        textbook_page._select_option(page, 0, "Reading Plus")

        self.assertEqual(page.timeout_calls, 0)


if __name__ == "__main__":
    unittest.main()
