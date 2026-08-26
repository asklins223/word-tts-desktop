from __future__ import annotations

import unittest
from unittest import mock

from pydub import AudioSegment
from pydub.generators import Sine

import word_tts_app as core


class AudioAssemblyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _raw_segment() -> AudioSegment:
        """带首尾静音的模拟讯飞音频，用于确认不会被裁切。"""
        return (
            AudioSegment.silent(duration=20, frame_rate=44100)
            + Sine(440).to_audio_segment(duration=100)
            + AudioSegment.silent(duration=30, frame_rate=44100)
        )

    async def test_xunfei_segments_are_concatenated_without_trim_or_pause(self):
        calls = []

        async def fake_synth_segment(text, voice, rate, volume, pitch):
            calls.append((text, voice, rate, volume, pitch))
            return self._raw_segment()

        with mock.patch.object(core, "_synth_segment", side_effect=fake_synth_segment):
            result = await core._synth_item(
                "W: first\nsecond line\nM: final",
                rate=50,
                volume=50,
                pitch=50,
            )

        self.assertEqual(
            calls,
            [
                ("first\nsecond line", core.FEMALE_VOICE, 50, 50, 50),
                ("final", core.MALE_VOICE, 50, 50, 50),
            ],
        )
        self.assertEqual(len(result), 2 * len(self._raw_segment()))

    async def test_named_roles_use_the_selected_voice_parameters(self):
        calls = []

        async def fake_synth_segment(text, voice, rate, volume, pitch):
            calls.append((text, voice, rate, volume, pitch))
            return self._raw_segment()

        with mock.patch.object(core, "_synth_segment", side_effect=fake_synth_segment):
            await core._synth_item(
                "Reporter: opening\nMr Yan: answer",
                rate=50,
                volume=50,
                pitch=50,
                female_voice="speaker:reporter",
                male_voice="speaker:yan",
                role_voices={"Reporter": "speaker:reporter", "Mr Yan": "speaker:yan"},
                voice_configs={
                    "speaker:reporter": {"rate": 12, "volume": 13, "pitch": 14},
                    "speaker:yan": {"rate": 82, "volume": 83, "pitch": 84},
                },
            )

        self.assertEqual(
            calls,
            [
                ("opening", "speaker:reporter", 12, 13, 14),
                ("answer", "speaker:yan", 82, 83, 84),
            ],
        )

    async def test_same_voice_uses_separate_default_and_role_parameters(self):
        calls = []

        async def fake_synth_segment(text, voice, rate, volume, pitch):
            calls.append((text, voice, rate, volume, pitch))
            return self._raw_segment()

        with mock.patch.object(core, "_synth_segment", side_effect=fake_synth_segment):
            await core._synth_item(
                "M: default male\nReporter: role text",
                rate=50,
                volume=50,
                pitch=50,
                female_voice="speaker:shared",
                male_voice="speaker:shared",
                role_voices={"Reporter": "speaker:shared"},
                role_configs={
                    core.DEFAULT_FEMALE_ROLE_KEY: {"rate": 10, "volume": 11, "pitch": 12},
                    core.DEFAULT_MALE_ROLE_KEY: {"rate": 20, "volume": 21, "pitch": 22},
                    "role:reporter": {"rate": 80, "volume": 81, "pitch": 82},
                },
                default_role=core.DEFAULT_MALE_ROLE_KEY,
            )

        self.assertEqual(
            calls,
            [
                ("default male", "speaker:shared", 20, 21, 22),
                ("role text", "speaker:shared", 80, 81, 82),
            ],
        )

    async def test_batch_download_progress_is_forwarded_per_completed_item(self):
        segment = self._raw_segment()
        events = []

        async def fake_batch(jobs, progress_callback=None):
            for job in jobs:
                progress_callback({
                    "job_id": job["job_id"],
                    "downloaded": True,
                    "stage": "downloaded",
                })
                progress_callback({
                    "job_id": job["job_id"],
                    "downloaded": True,
                    "stage": "saved",
                })
            return {
                job["job_id"]: {"segment": segment, "error": None}
                for job in jobs
            }

        item_specs = [
            {
                "item_id": "q1",
                "text": "W: first\nM: second",
                "rate": 50,
                "volume": 50,
                "pitch": 50,
                "default_voice": core.FEMALE_VOICE,
            },
            {
                "item_id": "q2",
                "text": "second",
                "rate": 50,
                "volume": 50,
                "pitch": 50,
                "default_voice": core.FEMALE_VOICE,
            },
        ]

        with mock.patch.object(core._xunfei, "synth_xunfei_batch", new=fake_batch):
            result = await core._synth_items_batch(
                item_specs,
                progress_callback=events.append,
            )

        self.assertEqual(
            [(event["item_id"], event["status"]) for event in events],
            [("q1", "downloaded"), ("q1", "ready"), ("q2", "downloaded"), ("q2", "ready")],
        )
        self.assertEqual(
            (events[0]["completed_segments"], events[0]["total_segments"]),
            (2, 2),
        )
        self.assertEqual(set(result), {"q1", "q2"})
        self.assertTrue(all(result[item_id]["audio"] is not None for item_id in result))


if __name__ == "__main__":
    unittest.main()
