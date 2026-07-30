from __future__ import annotations

import unittest
from unittest import mock

from ttsmaker.ttsmaker import TTSMakerSession


class FakePage:
    def __init__(self, usable=True):
        self.usable = usable
        self.reload_calls = 0
        self.goto_calls = 0
        self.waited_for = []

    def is_closed(self):
        return False

    def wait_for_selector(self, selector, timeout=None):
        self.waited_for.append((selector, timeout))
        if not self.usable:
            raise RuntimeError("form unavailable")

    def wait_for_timeout(self, _milliseconds):
        return None

    def reload(self, **_kwargs):
        self.reload_calls += 1
        self.usable = True

    def goto(self, *_args, **_kwargs):
        self.goto_calls += 1
        self.usable = True


class TTSMakerFormRecoveryTests(unittest.TestCase):
    @staticmethod
    def _session():
        session = TTSMakerSession("alfie")
        session._remember_form_state(
            "Keep these settings.",
            speed="1.2",
            volume="1.1",
            pitch="0.9",
            pause_time="500",
        )
        return session

    def test_usable_form_recovers_without_refreshing_page(self):
        session = self._session()
        page = FakePage(usable=True)

        with mock.patch.object(session, "_restore_form_state") as restore, mock.patch.object(
            session, "_clear_previous_audio"
        ) as clear:
            recovered = session._recover_form(page)

        self.assertTrue(recovered)
        self.assertEqual(page.reload_calls, 0)
        self.assertEqual(page.goto_calls, 0)
        restore.assert_called_once_with(page)
        clear.assert_called_once_with(page)

    def test_unusable_form_refreshes_once_and_restores_all_settings(self):
        session = self._session()
        page = FakePage(usable=False)

        with mock.patch.object(session, "_restore_form_state") as restore, mock.patch.object(
            session, "_clear_previous_audio"
        ) as clear:
            recovered = session._recover_form(page)

        self.assertTrue(recovered)
        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(page.goto_calls, 0)
        restore.assert_called_once_with(page)
        clear.assert_called_once_with(page)

    def test_form_state_keeps_text_and_every_advanced_option(self):
        session = self._session()

        self.assertEqual(
            session._last_form_state,
            {
                "text": "Keep these settings.",
                "speed": "1.2",
                "volume": "1.1",
                "pitch": "0.9",
                "pause_time": "500",
            },
        )


if __name__ == "__main__":
    unittest.main()
