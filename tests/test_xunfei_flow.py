from __future__ import annotations

import unittest

from xunfei_peiyin import XunFeiSession


class _FakeKeyboard:
    def __init__(self):
        self.inserted = []
        self.typed = []

    def insert_text(self, value):
        self.inserted.append(value)

    def type(self, value):
        self.typed.append(value)


class _FakePage:
    def __init__(self):
        self.keyboard = _FakeKeyboard()


class XunfeiFlowTests(unittest.TestCase):
    def test_text_input_is_a_single_paste_like_insert(self):
        page = _FakePage()
        text = "Reporter: " + ("This is a long sentence. " * 40)

        self.assertTrue(XunFeiSession._type_text(page, text))
        self.assertEqual(page.keyboard.inserted, [text])
        self.assertEqual(page.keyboard.typed, [])


if __name__ == "__main__":
    unittest.main()
