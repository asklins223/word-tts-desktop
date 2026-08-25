from __future__ import annotations

import asyncio
import threading
import unittest

import xunfei_peiyin as xunfei
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


class _HealthyFakeSession:
    _logged_in = True
    _page = object()
    _ctx = object()


class XunfeiFlowTests(unittest.TestCase):
    def test_text_input_is_a_single_paste_like_insert(self):
        page = _FakePage()
        text = "Reporter: " + ("This is a long sentence. " * 40)

        self.assertTrue(XunFeiSession._type_text(page, text))
        self.assertEqual(page.keyboard.inserted, [text])
        self.assertEqual(page.keyboard.typed, [])

    def test_existing_sync_session_is_checked_off_the_asyncio_loop(self):
        original_session = xunfei._session
        original_available = xunfei.is_available
        original_health = xunfei._session_is_healthy
        seen_threads = []
        fake_session = _HealthyFakeSession()
        xunfei._session = fake_session
        xunfei.is_available = lambda: True

        def health_check(session):
            seen_threads.append(threading.current_thread())
            return session is fake_session

        xunfei._session_is_healthy = health_check
        try:
            result = asyncio.run(xunfei.ensure_session())
        finally:
            xunfei._session = original_session
            xunfei.is_available = original_available
            xunfei._session_is_healthy = original_health

        self.assertIs(result, fake_session)
        self.assertEqual(len(seen_threads), 1)
        self.assertIsNot(seen_threads[0], threading.current_thread())

    def test_concurrent_first_session_creation_reuses_one_sync_session(self):
        original_session = xunfei._session
        original_available = xunfei.is_available
        original_health = xunfei._session_is_healthy
        original_session_class = xunfei.XunFeiSession
        created = []

        class FakeSession:
            _logged_in = True
            _page = object()
            _ctx = object()

            def __init__(self, voice_key="amanda"):
                self.voice_key = voice_key
                created.append(self)

            def login(self, login_timeout=300):
                return None

        xunfei._session = None
        xunfei.is_available = lambda: True
        xunfei._session_is_healthy = lambda session: session is not None
        xunfei.XunFeiSession = FakeSession
        try:
            async def create_both():
                return await asyncio.gather(
                    xunfei.ensure_session("amanda"),
                    xunfei.ensure_session("george"),
                )

            first, second = asyncio.run(create_both())
        finally:
            xunfei._session = original_session
            xunfei.is_available = original_available
            xunfei._session_is_healthy = original_health
            xunfei.XunFeiSession = original_session_class

        self.assertIs(first, second)
        self.assertEqual(len(created), 1)


if __name__ == "__main__":
    unittest.main()
