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


if __name__ == "__main__":
    unittest.main()
