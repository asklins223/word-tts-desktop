from __future__ import annotations

import asyncio
import gc
import importlib
import io
import json
import shutil
import tempfile
import threading
import unittest
import warnings
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import edge_tts
import gradio as gr
from pydub import AudioSegment
from pydub.generators import Sine

# 确保能找到 edge_tts/ 目录下的模块
import sys as _sys
ROOT = Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(ROOT / "edge_tts"))

from voice_match_788 import (
    VoiceMatchError,
    build_filter_graph,
    load_profile,
    pcm_to_wav_bytes,
    process_audio_segment,
    stream_edge_tts_788_pcm,
)


CALIBRATION_SOURCE = ROOT / "experiments" / "_tmp_1to1" / "tts_raw.mp3"


class VoiceMatchProfileTests(unittest.TestCase):
    def test_profile_is_valid_and_targets_remy(self):
        profile = load_profile()
        self.assertEqual(profile.source_voice, "fr-FR-RemyMultilingualNeural")
        self.assertEqual(profile.sample_rate_hz, 16000)
        self.assertEqual(len(profile.frequencies_hz), len(profile.gains_db))
        self.assertGreater(len(profile.frequencies_hz), 32)

    def test_profile_rejects_non_object_and_unsafe_sample_rate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            invalid_root = Path(temp_dir) / "invalid-root.json"
            invalid_root.write_text("[]", encoding="utf-8")
            with self.assertRaises(VoiceMatchError):
                load_profile(invalid_root)

            unsafe_rate = Path(temp_dir) / "unsafe-rate.json"
            unsafe_rate.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "source_voice": "fr-FR-RemyMultilingualNeural",
                        "sample_rate_hz": 1,
                        "post_pitch_ratio": 1,
                        "frequencies_hz": [0, 0.5],
                        "gains_db": [0, 0],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(VoiceMatchError):
                load_profile(unsafe_rate)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_filter_strength_is_bounded(self):
        full = build_filter_graph(100)
        over = build_filter_graph(500)
        off = build_filter_graph(-10)
        self.assertEqual(full, over)
        self.assertIn("firequalizer", full)
        self.assertNotIn("firequalizer", off)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_unity_pitch_profile_does_not_require_rubberband(self):
        unity_pitch = replace(load_profile(), post_pitch_ratio=1.0)
        graph = build_filter_graph(100, unity_pitch)
        self.assertNotIn("rubberband", graph)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_batch_processor_returns_safe_mono_pcm(self):
        source = Sine(220).to_audio_segment(duration=600).apply_gain(-12)
        result = process_audio_segment(source, 100)
        self.assertEqual(result.frame_rate, 16000)
        self.assertEqual(result.channels, 1)
        self.assertEqual(result.sample_width, 2)
        self.assertLess(abs(len(result) - len(source)), 100)
        self.assertLessEqual(result.max_dBFS, 0)


class LiveGeneratorCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        async def fake_list_voices():
            return [
                {
                    "ShortName": "zh-CN-XiaoxiaoNeural",
                    "Locale": "zh-CN",
                    "Gender": "Female",
                },
                {
                    "ShortName": "fr-FR-RemyMultilingualNeural",
                    "Locale": "fr-FR",
                    "Gender": "Male",
                },
            ]

        with warnings.catch_warnings():
            # Gradio creates short-lived policy loops while its component tree
            # is imported; they are unrelated to the generator under test.
            warnings.simplefilter("ignore", ResourceWarning)
            with patch.object(edge_tts, "list_voices", fake_list_voices):
                cls.app_module = importlib.import_module("app")

    def test_outer_generator_close_immediately_closes_pcm_stream(self):
        app_module = self.app_module

        state = {"closed": False}

        async def fake_pcm_stream(*args, **kwargs):
            try:
                yield b"\0" * 6400
                await asyncio.Event().wait()
            finally:
                state["closed"] = True

        async def exercise():
            generator = app_module.generate_788_live(
                "cleanup test", 0, 0, 0, 100, ""
            )
            await generator.__anext__()
            await generator.aclose()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                app_module, "OUTPUT_DIR", temp_dir
            ), patch.object(
                app_module, "stream_edge_tts_788_pcm", fake_pcm_stream
            ):
                asyncio.run(exercise())
                self.assertTrue(state["closed"])
                self.assertEqual(list(Path(temp_dir).iterdir()), [])
            gc.collect()

    def test_export_cancellation_cannot_recreate_output(self):
        app_module = self.app_module
        export_started = threading.Event()
        release_export = threading.Event()

        async def fake_pcm_stream(*args, **kwargs):
            yield b"\0" * 6400

        def slow_export(wav_path, out_path):
            export_started.set()
            if not release_export.wait(timeout=5):
                raise TimeoutError("test exporter was not released")
            Path(out_path).write_bytes(b"late export")

        async def exercise():
            generator = app_module.generate_788_live(
                "export cancellation test", 0, 0, 0, 100, ""
            )
            await generator.__anext__()
            completion = asyncio.create_task(generator.__anext__())
            started = await asyncio.to_thread(export_started.wait, 2)
            self.assertTrue(started)
            completion.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(completion.done())
            release_export.set()
            with self.assertRaises(asyncio.CancelledError):
                await completion
            await generator.aclose()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                app_module, "OUTPUT_DIR", temp_dir
            ), patch.object(
                app_module, "stream_edge_tts_788_pcm", fake_pcm_stream
            ), patch.object(
                app_module, "export_788_live_audio", slow_export
            ):
                asyncio.run(exercise())
                self.assertEqual(list(Path(temp_dir).iterdir()), [])
            gc.collect()


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
@unittest.skipUnless(CALIBRATION_SOURCE.exists(), "Calibration source is required")
class VoiceMatchStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_edge_pipeline_emits_gradio_decodable_wav_chunks(self):
        class FakeCommunicate:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                with CALIBRATION_SOURCE.open("rb") as source:
                    while chunk := source.read(1024):
                        yield {"type": "audio", "data": chunk}

        chunks = []
        with patch.object(edge_tts, "Communicate", FakeCommunicate):
            async for pcm in stream_edge_tts_788_pcm("local streaming test"):
                chunks.append(pcm_to_wav_bytes(pcm))

        self.assertGreater(len(chunks), 1)
        component = gr.Audio(streaming=True, interactive=False, format="wav")
        streamed_duration = 0.0
        for index, chunk in enumerate(chunks):
            # Gradio 6 decodes every yielded bytes object independently before
            # converting it to an AAC browser chunk.
            decoded_chunk = AudioSegment.from_file(io.BytesIO(chunk), format="wav")
            self.assertGreater(len(decoded_chunk), 0)
            media, _ = await component.stream_output(
                chunk,
                output_id="voice-match-test",
                first_chunk=index == 0,
            )
            self.assertIsNotNone(media)
            streamed_duration += media["duration"]

        self.assertGreater(sum(map(len, chunks)), 10000)
        self.assertGreater(streamed_duration, 4.0)

    async def test_ffmpeg_failure_cancels_a_stalled_edge_feeder(self):
        payload = CALIBRATION_SOURCE.read_bytes()

        class StallingCommunicate:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                yield {"type": "audio", "data": payload}
                await asyncio.Event().wait()

        stream = stream_edge_tts_788_pcm("early FFmpeg failure test")
        try:
            with patch.object(
                edge_tts, "Communicate", StallingCommunicate
            ), patch(
                "voice_match_788.build_filter_graph",
                return_value="filter_that_does_not_exist",
            ), self.assertRaises(VoiceMatchError) as caught:
                await asyncio.wait_for(stream.__anext__(), timeout=5)
        finally:
            await stream.aclose()

        self.assertIn("filter_that_does_not_exist", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
