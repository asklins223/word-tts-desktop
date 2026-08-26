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
            [
                ("q1", "downloaded"),
                ("q1", "downloaded"),
                ("q1", "ready"),
                ("q2", "downloaded"),
                ("q2", "ready"),
            ],
        )
        self.assertEqual(
            (events[1]["completed_segments"], events[1]["total_segments"]),
            (2, 2),
        )
        self.assertEqual(set(result), {"q1", "q2"})
        self.assertTrue(all(result[item_id]["audio"] is not None for item_id in result))

    async def test_composite_batch_downloads_once_then_cuts_each_item(self):
        events = []
        tone = Sine(440).to_audio_segment(duration=700).apply_gain(-3)

        async def fake_composite(works, progress_callback=None, resume=None):
            self.assertEqual(len(works), 1)
            work = works[0]
            works_id = "works-composite-test"
            if progress_callback:
                progress_callback({
                    "work_id": work["work_id"],
                    "stage": "submitted",
                    "works_id": works_id,
                })
                progress_callback({
                    "work_id": work["work_id"],
                    "stage": "downloaded",
                    "works_id": works_id,
                })
            full_audio = tone
            for _ in work["items"][1:]:
                full_audio += AudioSegment.silent(duration=800) + tone
            return {
                work["work_id"]: {
                    "audio": full_audio,
                    "works_id": works_id,
                    "error": None,
                }
            }

        item_specs = [
            {
                "item_id": "q1",
                "text": "first",
                "default_voice": core.FEMALE_VOICE,
            },
            {
                "item_id": "q2",
                "text": "second",
                "default_voice": core.FEMALE_VOICE,
            },
        ]

        with mock.patch.object(
            core,
            "_XUNFEI_AVAILABLE",
            True,
        ), mock.patch.object(
            core._xunfei,
            "synth_xunfei_composite",
            new=fake_composite,
        ):
            result = await core._synth_items_batch_composite(
                item_specs,
                progress_callback=events.append,
            )

        self.assertEqual(
            [event["status"] for event in events],
            ["submitted", "downloaded", "cut"],
        )
        self.assertTrue(events[0]["work_id"].startswith("composite:"))
        self.assertEqual(
            {event["work_id"] for event in events},
            {events[0]["work_id"]},
        )
        self.assertEqual(set(result), {"q1", "q2"})
        self.assertTrue(all(result[item_id]["audio"] is not None for item_id in result))
        self.assertTrue(all(len(result[item_id]["audio"]) >= 600 for item_id in result))

    def test_composite_cut_uses_internal_pauses_and_keeps_short_edge_protection(self):
        tone = Sine(440).to_audio_segment(duration=900).apply_gain(-3)
        audio = (
            AudioSegment.silent(duration=1200)
            + tone
            + AudioSegment.silent(duration=2000)
            + tone
            + AudioSegment.silent(duration=2000)
            + tone
            + AudioSegment.silent(duration=1200)
        )

        pieces = core.cut_composite_audio(audio, 3, item_lengths=[1, 1, 1])

        self.assertEqual(len(pieces), 3)
        self.assertTrue(all(950 <= len(piece) <= 1150 for piece in pieces))
        self.assertTrue(all(core._edge_silence_length(piece, leading=True) <= 130 for piece in pieces))
        self.assertTrue(all(core._edge_silence_length(piece, leading=False) <= 130 for piece in pieces))

    def test_composite_cut_keeps_short_outer_quiet_edges_for_weak_speech(self):
        speech = Sine(440).to_audio_segment(duration=800).apply_gain(-3)
        audio = (
            AudioSegment.silent(duration=100)
            + speech
            + AudioSegment.silent(duration=2000)
            + speech.fade_out(300)
            + AudioSegment.silent(duration=250)
        )

        pieces = core.cut_composite_audio(audio, 2)

        # 100/250ms 是作品最外层的自然空档，不应按内部人工 break 的规则
        # 激进裁切；尤其要保留最后一段的弱尾音。
        self.assertGreaterEqual(core._edge_silence_length(pieces[0], leading=True), 80)
        self.assertGreaterEqual(core._edge_silence_length(pieces[1], leading=False), 200)

    def test_single_item_composite_trims_only_long_outer_quiet_edges(self):
        speech = Sine(440).to_audio_segment(duration=800).apply_gain(-3)
        audio = AudioSegment.silent(duration=1000) + speech + AudioSegment.silent(duration=1000)

        pieces = core.cut_composite_audio(audio, 1)

        self.assertEqual(len(pieces), 1)
        self.assertTrue(100 <= core._edge_silence_length(pieces[0], leading=True) <= 140)
        self.assertTrue(100 <= core._edge_silence_length(pieces[0], leading=False) <= 140)

    def test_composite_cut_rejects_short_pause_instead_of_guessing(self):
        tone = Sine(440).to_audio_segment(duration=900).apply_gain(-3)
        audio = tone + AudioSegment.silent(duration=120) + tone

        with self.assertRaises(core.CompositeCutError):
            core.cut_composite_audio(audio, 2)

    def test_composite_cut_does_not_consume_a_late_pause_needed_by_next_boundary(self):
        audio = AudioSegment.silent(duration=10000)
        runs = [
            {"start": 2500, "end": 3500, "center": 3000, "length": 1000},
            {"start": 7000, "end": 9500, "center": 8250, "length": 2500},
        ]

        selected = core._select_composite_silence_runs(
            audio,
            runs,
            boundary_count=2,
            item_lengths=[1, 1, 1],
        )

        self.assertEqual([run["center"] for run in selected], [3000, 8250])

    def test_composite_plan_keeps_items_whole_when_work_limit_is_reached(self):
        specs = [
            {"item_id": "q1", "text": "first", "default_voice": core.FEMALE_VOICE},
            {"item_id": "q2", "text": "second", "default_voice": core.FEMALE_VOICE},
        ]

        works = core.build_composite_work_plan(specs, max_chars=5)

        self.assertEqual([work["item_ids"] for work in works], [["q1"], ["q2"]])
        self.assertEqual(sum(work["item_count"] for work in works), 2)

    def test_composite_plan_rejects_duplicate_item_ids(self):
        specs = [
            {"item_id": "same", "text": "first", "default_voice": core.FEMALE_VOICE},
            {"item_id": "same", "text": "second", "default_voice": core.FEMALE_VOICE},
        ]

        with self.assertRaises(core.CompositePlanError):
            core.build_composite_work_plan(specs)

    def test_composite_plan_rejects_malformed_items_instead_of_silently_dropping_them(self):
        with self.assertRaises(core.CompositePlanError):
            core.build_composite_work_plan([
                {"item_id": "q1", "text": "first", "default_voice": core.FEMALE_VOICE},
                None,
            ])
        with self.assertRaises(core.CompositePlanError):
            core.build_composite_work_plan(
                [{"item_id": "q1", "text": "first", "default_voice": core.FEMALE_VOICE}],
                existing_plan=[{"work_id": "broken", "item_ids": []}],
            )

    def test_composite_plan_rejects_missing_or_duplicate_stored_work_ids(self):
        specs = [{"item_id": "q1", "text": "first", "default_voice": core.FEMALE_VOICE}]

        with self.assertRaises(core.CompositePlanError):
            core.build_composite_work_plan(
                specs,
                existing_plan=[{"item_ids": ["q1"]}],
            )
        with self.assertRaises(core.CompositePlanError):
            core.build_composite_work_plan(
                specs,
                existing_plan=[
                    {"work_id": "same", "item_ids": ["q1"]},
                    {"work_id": "same", "item_ids": ["q2"]},
                ],
            )


if __name__ == "__main__":
    unittest.main()
