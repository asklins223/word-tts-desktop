from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydub import AudioSegment
from pydub.generators import Sine
from pydub.silence import detect_nonsilent

import word_tts_app as core


class AudioSpacingTests(unittest.TestCase):
    @staticmethod
    def _tone(duration_ms: int = 500) -> AudioSegment:
        return Sine(440).to_audio_segment(duration=duration_ms).apply_gain(-6)

    @staticmethod
    def _silence(duration_ms: int) -> AudioSegment:
        return AudioSegment.silent(duration=duration_ms, frame_rate=44100)

    def test_strip_edge_silence_removes_long_padding_and_keeps_guards(self):
        padded = (
            self._silence(200)
            + self._tone()
            + self._silence(900)
        )

        trimmed = core._strip_edge_silence(padded)

        expected_ms = (
            core._EDGE_LEADING_GUARD_MS
            + 500
            + core._EDGE_TRAILING_GUARD_MS
        )
        self.assertAlmostEqual(len(trimmed), expected_ms, delta=10)

    def test_strip_edge_silence_preserves_low_energy_tail_at_any_volume(self):
        # 低能量尾音后仍有足够长的真实数字静音；整体降低音量也不能让尾音
        # 被绝对阈值误判。裁剪后应保留完整 180ms 尾音和尾部 guard。
        raw = (
            self._tone()
            + Sine(440).to_audio_segment(duration=180).apply_gain(-45)
            + self._silence(500)
        )

        for gain_db in (0, -20, -30):
            with self.subTest(gain_db=gain_db):
                trimmed = core._strip_edge_silence(raw.apply_gain(gain_db))
                self.assertAlmostEqual(
                    len(trimmed),
                    500 + 180 + core._EDGE_TRAILING_GUARD_MS,
                    delta=10,
                )
                self.assertNotEqual(trimmed[500:680].dBFS, float("-inf"))

    def test_strip_edge_silence_does_not_touch_short_natural_tail(self):
        raw = self._tone() + self._silence(200)

        trimmed = core._strip_edge_silence(raw)

        self.assertEqual(len(trimmed), len(raw))

    def test_pause_values_are_owned_by_the_application(self):
        self.assertEqual(core._pause_value_to_ms(-1), 0)
        self.assertEqual(core._pause_value_to_ms(0), 300)
        self.assertEqual(core._pause_value_to_ms(50), 50)
        self.assertEqual(core._pause_value_to_ms(500), 500)

    def test_all_voice_switch_directions_keep_the_requested_spacing(self):
        female_raw = self._silence(20) + self._tone() + self._silence(50)
        male_raw = self._silence(30) + self._tone() + self._silence(80)

        async def fake_synth_segment(
            text, voice, rate, volume, pitch, proxy, tmp_dir, pause=0
        ):
            return female_raw if voice == core.FEMALE_VOICE else male_raw

        async def synth(text: str, pause) -> AudioSegment:
            with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
                core, "_synth_segment", side_effect=fake_synth_segment
            ):
                return await core._synth_item(
                    text,
                    rate=1,
                    volume=1,
                    pitch=1,
                    pause=pause,
                    proxy="",
                    tmp_dir=tmp_dir,
                )

        cases = (
            ("W: hello\nM: hello", 20),
            ("M: hello\nW: hello", 30),
            ("W: hello\nW: hello", 20),
            ("M: hello\nM: hello", 30),
        )

        async def exercise():
            for pause in (-1, 50, 100, 200, 0, 500):
                expected_pause_ms = core._pause_value_to_ms(pause)
                for text, first_leading_ms in cases:
                    with self.subTest(pause=pause, text=text):
                        result = await synth(text, pause)
                        self.assertAlmostEqual(
                            len(result),
                            first_leading_ms
                            + 500
                            + expected_pause_ms
                            + 500
                            + core._FINAL_POST_ROLL_MS,
                            delta=10,
                        )
                        if expected_pause_ms:
                            ranges = detect_nonsilent(
                                result,
                                min_silence_len=5,
                                silence_thresh=-30,
                            )
                            self.assertEqual(len(ranges), 2)
                            self.assertAlmostEqual(
                                ranges[1][0] - ranges[0][1],
                                expected_pause_ms,
                                delta=10,
                            )

        asyncio.run(exercise())

    def test_three_part_dialogue_normalizes_each_boundary(self):
        female_raw = self._silence(20) + self._tone() + self._silence(50)
        male_raw = self._silence(30) + self._tone() + self._silence(80)

        async def fake_synth_segment(
            text, voice, rate, volume, pitch, proxy, tmp_dir, pause=0
        ):
            return female_raw if voice == core.FEMALE_VOICE else male_raw

        async def synth(pause) -> AudioSegment:
            with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
                core,
                "_synth_segment",
                side_effect=fake_synth_segment,
            ):
                return await core._synth_item(
                    "W: one\nM: two\nW: three",
                    rate=1,
                    volume=1,
                    pitch=1,
                    pause=pause,
                    proxy="",
                    tmp_dir=tmp_dir,
                )

        for pause in (-1, 50, 100, 500):
            with self.subTest(pause=pause):
                expected_pause_ms = core._pause_value_to_ms(pause)
                result = asyncio.run(synth(pause))
                self.assertAlmostEqual(
                    len(result),
                    20
                    + (3 * 500)
                    + (2 * expected_pause_ms)
                    + core._FINAL_POST_ROLL_MS,
                    delta=10,
                )
                if expected_pause_ms:
                    ranges = detect_nonsilent(
                        result,
                        min_silence_len=5,
                        silence_thresh=-30,
                    )
                    self.assertEqual(len(ranges), 3)
                    self.assertEqual(
                        [
                            ranges[index + 1][0] - ranges[index][1]
                            for index in range(2)
                        ],
                        [expected_pause_ms, expected_pause_ms],
                    )

    def test_last_sentence_gets_post_roll_instead_of_ending_on_audio(self):
        async def fake_synth_segment(*_args, **_kwargs):
            return self._tone()

        async def synth() -> AudioSegment:
            with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
                core,
                "_synth_segment",
                side_effect=fake_synth_segment,
            ):
                return await core._synth_item(
                    "W: final sentence",
                    rate=1,
                    volume=1,
                    pitch=1,
                    pause=0,
                    proxy="",
                    tmp_dir=tmp_dir,
                )

        result = asyncio.run(synth())

        self.assertAlmostEqual(
            len(result),
            500 + core._FINAL_POST_ROLL_MS,
            delta=5,
        )
        self.assertGreaterEqual(
            core._edge_silence_ms(
                result,
                leading=False,
                max_scan_ms=core._FINAL_POST_ROLL_MS,
            ),
            core._FINAL_POST_ROLL_MS,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            for fmt, quality in (
                ("mp3", "128 kbps（标准）"),
                ("ogg", "128 kbps（标准）"),
                ("aac", "128 kbps（标准）"),
                ("opus", "128 kbps（标准）"),
                ("wav", "无损（仅 wav 生效）"),
            ):
                with self.subTest(fmt=fmt):
                    output_path = Path(tmp_dir, f"post-roll.{fmt}")
                    core.export_audio(result, fmt, quality, str(output_path))
                    with output_path.open("rb") as audio_source:
                        decoded = AudioSegment.from_file(audio_source)
                    self.assertAlmostEqual(
                        len(decoded),
                        len(result),
                        delta=30,
                    )
                    if fmt == "opus":
                        # Opus 编码会在数字静音中留下极低电平的编码噪声。
                        self.assertLess(decoded[-50:].dBFS, -55)
                    else:
                        self.assertGreaterEqual(
                            core._edge_silence_ms(
                                decoded,
                                leading=False,
                                max_scan_ms=core._FINAL_POST_ROLL_MS,
                            ),
                            core._FINAL_POST_ROLL_MS - 15,
                        )

    def test_ttsmaker_male_disables_engine_pause_and_is_not_trimmed(self):
        raw = (
            self._tone()
            + Sine(440).to_audio_segment(duration=120).apply_gain(-45)
            + self._silence(80)
        )
        synth_male = mock.AsyncMock(return_value=raw)
        fake_ttsmaker = SimpleNamespace(synth_male_ttsmaker=synth_male)

        with mock.patch.object(core, "_TTSMaker_AVAILABLE", True), mock.patch.object(
            core,
            "_ttsmaker",
            fake_ttsmaker,
        ):
            result = asyncio.run(
                core._synth_segment(
                    "hello",
                    core.MALE_VOICE,
                    rate=1,
                    volume=1,
                    pitch=1,
                    proxy="",
                    tmp_dir="/unused",
                    pause=500,
                )
            )

        self.assertEqual(len(result), len(raw))
        self.assertEqual(result.raw_data, raw.raw_data)
        self.assertEqual(synth_male.await_args.kwargs["pause"], -1)


if __name__ == "__main__":
    unittest.main()
