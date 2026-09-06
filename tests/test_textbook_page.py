from __future__ import annotations

import unittest

from platform_entry.adapter import textbook_page


class TextbookPageInputTests(unittest.TestCase):
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
