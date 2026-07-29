from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest import mock

from pydub import AudioSegment
from pydub.generators import Sine

import word_tts_app as core


class AudioSpacingTests(unittest.TestCase):
    @staticmethod
    def _tone(duration_ms: int = 500) -> AudioSegment:
        return Sine(440).to_audio_segment(duration=duration_ms).apply_gain(-6)

    def test_strip_edge_silence_removes_tts_padding(self):
        padded = (
            AudioSegment.silent(duration=200)
            + self._tone()
            + AudioSegment.silent(duration=900)
        )

        trimmed = core._strip_edge_silence(padded)

        self.assertAlmostEqual(len(trimmed), 500, delta=20)

    def test_voice_switch_spacing_is_independent_of_direction(self):
        female_raw = (
            AudioSegment.silent(duration=200)
            + self._tone()
            + AudioSegment.silent(duration=900)
        )
        male_raw = self._tone()

        async def fake_synth_segment(
            text, voice, rate, volume, pitch, proxy, tmp_dir, pause=0
        ):
            raw = female_raw if voice == core.FEMALE_VOICE else male_raw
            return core._strip_edge_silence(raw)

        async def synth(text: str) -> AudioSegment:
            with tempfile.TemporaryDirectory() as tmp_dir:
                with mock.patch.object(
                    core, "_synth_segment", side_effect=fake_synth_segment
                ):
                    return await core._synth_item(
                        text,
                        rate=1,
                        volume=1,
                        pitch=1,
                        pause=0,
                        proxy="",
                        tmp_dir=tmp_dir,
                    )

        female_then_male = asyncio.run(synth("W: hello\nM: hello"))
        male_then_female = asyncio.run(synth("M: hello\nW: hello"))

        # pause=0 对应产品里的默认 300ms。两段语音各 500ms，
        # 因此两个方向都应为 500 + 300 + 500，不能叠加引擎自带静音。
        self.assertAlmostEqual(len(female_then_male), 1300, delta=20)
        self.assertAlmostEqual(len(male_then_female), 1300, delta=20)
        self.assertAlmostEqual(len(female_then_male), len(male_then_female), delta=5)


if __name__ == "__main__":
    unittest.main()
