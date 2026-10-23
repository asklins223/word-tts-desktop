from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import workflow.audio_asr as audio_asr
from workflow.artifact_store import ArtifactStore
from workflow.audio_asr import AudioImportCoordinator, _ImportBatch
from workflow.repositories import ConflictError


class _TranscriptExtension:
    def __init__(self, transcripts: list[str]) -> None:
        self.transcripts = transcripts

    def create_recognizer(self):
        return object()

    def recognize_spans(self, _path, spans, *, recognizer=None):
        assert len(spans) == len(self.transcripts)
        return [{"text": text, "scope": "segment", "error": ""} for text in self.transcripts]


class AudioImportContinuousTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.coordinator = AudioImportCoordinator(object(), self.artifacts)
        self.cut_spans: list[tuple[tuple[float, float], ...]] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _analyze(self, items: list[dict], spans: list[tuple[float, float]], transcripts: list[str]):
        batch_id = "continuous-batch"
        workflow_id = "workflow"
        source = self.artifacts.stage_stream(io.BytesIO(b"source audio"))
        batch = _ImportBatch(
            batch_id,
            workflow_id,
            [{"file_name": "recording.mp3", "extension": ".mp3", "size_bytes": source.size_bytes}],
            items,
            mode="continuous",
        )
        batch.staged[0] = source
        self.coordinator._batches[batch_id] = batch

        def stage_segment(_path, selected_spans, artifacts):
            self.cut_spans.append(tuple(selected_spans))
            return artifacts.stage_stream(io.BytesIO(f"clip-{len(self.cut_spans)}".encode()))

        with (
            patch.object(audio_asr, "_audio_duration_and_speech_spans", return_value=(max(end for _start, end in spans) + 1, spans)),
            patch.object(audio_asr, "_stage_audio_segments", side_effect=stage_segment),
        ):
            result = self.coordinator.analyze(
                workflow_id,
                batch_id,
                _TranscriptExtension(transcripts),
            )
        return result, batch

    @staticmethod
    def _items() -> list[dict]:
        return [
            {
                "item_id": "q1",
                "sequence": 0,
                "normalized_content": "There are four seasons in a year, and each season has its special features.",
            },
            {"item_id": "q2", "sequence": 1, "normalized_content": "Life begins again in spring."},
            {
                "item_id": "q3",
                "sequence": 2,
                "normalized_content": "Plants start to grow, and birds come back from the south.",
            },
        ]

    def test_continuous_audio_matches_questions_merges_repeats_and_allows_complete_binding(self) -> None:
        items = self._items()
        spans = [(0.0, 1.0), (2.0, 4.0), (6.0, 8.0), (11.0, 13.0), (15.0, 17.0)]
        transcripts = [
            "请听题目，开始。",
            "There are four seasons in a year, and each season has its special features",
            "Life begins again in spring!",
            "Plants start to grow, and birds come back from the south.",
            "Life begins again in spring.",
        ]

        result, _batch = self._analyze(items, spans, transcripts)

        self.assertEqual(result["status"], "analyzed")
        self.assertTrue(result["can_commit"])
        self.assertEqual(result["missing_item_ids"], [])
        self.assertEqual([row["suggested_item_id"] for row in result["files"]], ["q1", "q2", "q3"])
        self.assertEqual(next(row["segment_count"] for row in result["files"] if row["suggested_item_id"] == "q2"), 2)
        self.assertEqual(len(self.cut_spans), 3)
        self.assertEqual(len(self.cut_spans[1]), 2)
        self.assertEqual(result["files"][1]["source_spans"], [
            {"start_seconds": 6.0, "end_seconds": 8.0},
            {"start_seconds": 15.0, "end_seconds": 17.0},
        ])

        bindings = [
            {"file_index": row["file_index"], "item_id": row["suggested_item_id"]}
            for row in result["files"]
        ]
        _batch, normalized = self.coordinator.validate_bindings("workflow", "continuous-batch", bindings)
        self.assertEqual(len(normalized), 3)

    def test_continuous_audio_missing_question_cannot_be_committed(self) -> None:
        items = self._items()[:2]
        result, _batch = self._analyze(
            items,
            [(2.0, 4.0)],
            ["There are four seasons in a year, and each season has its special features."],
        )

        self.assertFalse(result["can_commit"])
        self.assertEqual(result["missing_item_ids"], ["q2"])
        with self.assertRaises(ConflictError) as raised:
            self.coordinator.validate_bindings(
                "workflow",
                "continuous-batch",
                [{"file_index": 0, "item_id": "q1"}],
            )
        self.assertEqual(raised.exception.code, "AUDIO_IMPORT_FILES_MISSING")

    def test_updated_foreign_exam_stems_align_and_repeated_question_is_kept_once_per_clip(self) -> None:
        items = [
            {
                "item_id": "q1",
                "sequence": 0,
                "normalized_content": (
                    "What activity does the man enjoy doing? "
                    "(Hiking / Fishing / Cooking.)"
                ),
            },
            {
                "item_id": "q2",
                "sequence": 1,
                "normalized_content": "How often does the man do the activity?",
            },
        ]
        spans = [
            (0.0, 1.5), (2.0, 3.5),
            (6.0, 7.5), (8.0, 9.5),
            (12.0, 13.5),
        ]
        recognized = [
            {"text": "What activity does the man enjoy doing?"},
            {"text": "Hiking, fishing, cooking."},
            {"text": "What activity does the man enjoy doing?"},
            {"text": "Hiking, fishing, cooking."},
            {"text": "How often does the man do the activity"},
        ]

        occurrences = audio_asr._continuous_audio_matches(recognized, spans, items)

        q1_occurrences = [row for row in occurrences if row["top_item_id"] == "q1"]
        q2_occurrences = [row for row in occurrences if row["top_item_id"] == "q2"]
        self.assertEqual(len(q1_occurrences), 2)
        self.assertEqual(len(q2_occurrences), 1)

    def test_unspoken_choices_do_not_pull_the_next_question_into_the_clip(self) -> None:
        items = [
            {"item_id": "q1", "sequence": 0, "normalized_content": "Who is Mr White? (The woman's boss. / The woman's teacher.)"},
            {"item_id": "q2", "sequence": 1, "normalized_content": "Where is Mr White from? (Britain. / Canada. / Australia.)"},
        ]
        result, _batch = self._analyze(items, [(1, 3), (5, 7)], ["Who is Mr White?", "Where is Mr White from?"])

        self.assertTrue(result["can_commit"])
        self.assertEqual(self.cut_spans, [((1.0, 3.0),), ((5.0, 7.0),)])

    def test_dialogue_repeats_end_before_the_next_reading_starts(self) -> None:
        item = {"item_id": "dialogue", "sequence": 0, "normalized_content": "W: What is your hobby?\nM: I like hiking.\nW: How often do you go?\nM: Three times a month."}
        phrases = ["What is your hobby?", "I like hiking.", "How often do you go?", "Three times a month."]
        spans = [(1, 2), (3, 4), (5, 6), (7, 8), (10, 11), (12, 13), (14, 15), (16, 17)]
        result, _batch = self._analyze([item], spans, phrases + phrases)

        self.assertTrue(result["can_commit"])
        self.assertEqual(self.cut_spans, [((1.0, 8.0), (10.0, 17.0))])

    def test_question_cannot_claim_a_similar_sentence_inside_a_passage(self) -> None:
        items = [
            {"item_id": "passage", "sequence": 0, "normalized_content": "One night, I had a dream. A robot cooks breakfast for me. I go to work by car. The car drives by itself."},
            {"item_id": "question", "sequence": 1, "normalized_content": "Can the car drive by itself?"},
        ]
        spans = [(1, 3), (4, 6), (7, 9), (10, 12), (15, 17)]
        phrases = ["One night, I had a dream.", "A robot cooks breakfast for me.", "I go to work by car.", "The car drives by itself.", "Can the car drive by itself?"]
        result, _batch = self._analyze(items, spans, phrases)

        self.assertTrue(result["can_commit"])
        self.assertEqual(self.cut_spans, [((1.0, 12.0),), ((15.0, 17.0),)])

    def test_long_matching_prefix_without_the_ending_is_not_a_complete_clip(self) -> None:
        prefix = "I visited a beautiful village last August. There were many exciting activities to choose from. First, I took a boat down the river. Next, I rode a bike in the countryside."
        items = [{"item_id": "passage", "sequence": 0, "normalized_content": prefix + " What an unforgettable trip!"}]
        matches = audio_asr._continuous_audio_matches([{"text": prefix}], [(1, 35)], items)

        self.assertEqual(matches, [])

    def test_announcements_interrupt_a_passage_even_with_short_pauses(self) -> None:
        items = [{"item_id": "q1", "sequence": 0, "normalized_content": "The weather is sunny. We will go to the park."}]
        matches = audio_asr._continuous_audio_matches(
            [{"text": "The weather is sunny."}, {"text": "现在请听下一道题目。"}, {"text": "We will go to the park."}],
            [(1, 2), (2.5, 3), (3.5, 5)], items,
        )

        self.assertEqual(matches, [])

    def test_short_opening_word_keeps_its_audio_range(self) -> None:
        items = [{"item_id": "q1", "sequence": 0, "normalized_content": "I visited a beautiful village last August."}]
        result, _batch = self._analyze(items, [(1, 1.4), (2, 5)], ["I", "visited a beautiful village last August."])

        self.assertTrue(result["can_commit"])
        self.assertEqual(self.cut_spans, [((1.0, 5.0),)])

    def test_required_chinese_guidance_is_matched_alongside_english_content(self) -> None:
        items = [
            {"item_id": "body", "sequence": 0, "normalized_content": "I visited a beautiful village last August."},
            {"item_id": "guidance", "sequence": 1, "normalized_content": "你将听到一篇介绍旅行的短文，录音播放两遍。请根据所听内容复述短文。"},
            {"item_id": "starter", "sequence": 2, "normalized_content": "下面你有60秒钟的时间准备，你的复述可以这样开始：I visited a village."},
        ]
        spans = [(1, 4), (5, 8), (10, 15), (17, 20)]
        transcripts = [
            "你将听到一篇介绍旅行的短文，录音播放两遍。", "请根据所听内容复述短文。",
            "I visited a beautiful village last August.",
            "下面你有60秒钟的时间准备，你的复述可以这样开始：I visited a village.",
        ]
        result, _batch = self._analyze(items, spans, transcripts)

        self.assertTrue(result["can_commit"])
        self.assertEqual(result["matched_item_count"], 3)
        self.assertEqual(self.cut_spans, [((10.0, 15.0),), ((1.0, 8.0),), ((17.0, 20.0),)])

    def test_different_preparation_instruction_is_reported_as_missing(self) -> None:
        items = [
            {"item_id": "body", "sequence": 0, "normalized_content": "I visited a beautiful village last August."},
            {"item_id": "starter", "sequence": 1, "normalized_content": "下面你有60秒钟的时间准备，你的复述可以这样开始：I visited a village."},
        ]
        result, _batch = self._analyze(items, [(1, 5), (7, 11)], [
            "I visited a beautiful village last August.",
            "现在你有15秒钟的时间阅读思维导图，你的复述可以这样开始：I visited a village.",
        ])

        self.assertFalse(result["can_commit"])
        self.assertEqual(result["missing_item_ids"], ["starter"])

    def test_romanized_name_and_small_spelling_errors_keep_the_complete_audio(self) -> None:
        items = [
            {"item_id": "q1", "sequence": 0, "normalized_content": "Where is Mr White from?"},
            {"item_id": "q2", "sequence": 1, "normalized_content": "Peter visited Yangshuo last August."},
        ]
        result, _batch = self._analyze(items, [(1, 3), (5, 8)], [
            "Where is mister Wite from?", "Peter visited 杨ang朔 last August.",
        ])
        self.assertTrue(result["can_commit"])
        self.assertEqual(self.cut_spans, [((1.0, 3.0),), ((5.0, 8.0),)])
        self.assertEqual(result["files"][1]["transcript"], "Peter visited 杨ang朔 last August.")

    def test_chinese_name_in_a_separate_span_stays_in_an_english_clip(self) -> None:
        items = [{"item_id": "q1", "sequence": 0, "normalized_content": "Peter visited Yangshuo last August."}]
        result, _batch = self._analyze(items, [(1, 2), (2.4, 3), (3.5, 5)], [
            "Peter visited", "阳朔", "last August.",
        ])
        self.assertTrue(result["can_commit"])
        self.assertEqual(self.cut_spans, [((1.0, 5.0),)])

    def test_fuzzy_names_that_fit_two_questions_remain_for_manual_review(self) -> None:
        items = [
            {"item_id": "q1", "sequence": 0, "normalized_content": "Peter visited Yangshuo last August."},
            {"item_id": "q2", "sequence": 1, "normalized_content": "Peter visited Yangzhuo last August."},
        ]
        result, _batch = self._analyze(items, [(1, 3)], ["Peter visited 阳朔 last August."])
        self.assertFalse(result["can_commit"])
        self.assertEqual(result["files"][0]["match_status"], "needs_review")
        self.assertIsNone(result["files"][0]["suggested_item_id"])

    def test_nearly_identical_instruction_with_a_changed_number_is_still_missing(self) -> None:
        items = [{
            "item_id": "guidance", "sequence": 0,
            "normalized_content": "你将听到一篇介绍旅行的短文，录音播放两遍。请根据所听到的内容，依据思维导图的提示，在60秒内复述短文。",
        }]
        result, _batch = self._analyze(items, [(1, 15)], [
            "你将听到一篇介绍旅行的短文，录音播放两遍。请根据所听到的内容，依据思维导图的提示，在十五秒内复述短文。",
        ])
        self.assertFalse(result["can_commit"])
        self.assertEqual(result["missing_item_ids"], ["guidance"])

    def test_real_exam_transcripts_cover_all_items_without_overlap_or_spilling_repeats(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "audio_asr_exam.json").read_text(encoding="utf-8"))
        spans = [(entry["start"], entry["end"]) for entry in fixture["speech"]]
        transcripts = [entry["text"] for entry in fixture["speech"]]
        result, _batch = self._analyze(fixture["items"], spans, transcripts)

        self.assertTrue(result["can_commit"])
        self.assertEqual(result["matched_item_count"], 16)
        self.assertEqual(sum(row["segment_count"] for row in result["files"]), 21)
        for actual, expected in zip(self.cut_spans, fixture["expected_spans"]):
            self.assertEqual(len(actual), len(expected))
            for (start, end), (expected_start, expected_end) in zip(actual, expected):
                self.assertAlmostEqual(start, expected_start, delta=0.02)
                self.assertAlmostEqual(end, expected_end, delta=0.02)
        chronological = sorted(span for clip in self.cut_spans for span in clip)
        self.assertTrue(all(left[1] <= right[0] for left, right in zip(chronological, chronological[1:])))

    def test_three_guides_recover_the_reading_clip_without_substituting_for_missing_preparation(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "audio_asr_exam.json").read_text(encoding="utf-8"))
        items = fixture["items"] + [
            {"item_id": "intro", "sequence": 16, "normalized_content": "你将听到一篇 Peter 讲述去阳朔旅行的短文，录音播放两遍。请根据所听到的内容，依据思维导图的提示，在60秒内复述短文。"},
            {"item_id": "reading", "sequence": 17, "normalized_content": "现在，你有15秒钟的时间阅读思维导图。"},
            {"item_id": "preparation", "sequence": 18, "normalized_content": "下面你有60秒钟的时间准备，你的复述可以这样开始：Peter visited Yangshuo ..."},
        ]
        result, _batch = self._analyze(
            items,
            [(entry["start"], entry["end"]) for entry in fixture["speech"]],
            [entry["text"] for entry in fixture["speech"]],
        )

        self.assertEqual(result["required_item_count"], 19)
        self.assertEqual(result["matched_item_count"], 18)
        self.assertEqual(result["missing_item_ids"], ["preparation"])
        self.assertFalse(result["can_commit"])
        reading = next(row for row in result["files"] if row["suggested_item_id"] == "reading")
        self.assertEqual(reading["source_spans"], [{"start_seconds": 416.873, "end_seconds": 421.609}])

    def test_corrected_three_guide_document_matches_all_nineteen_items(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "audio_asr_exam.json").read_text(encoding="utf-8"))
        items = fixture["items"] + [
            {"item_id": "intro", "sequence": 16, "normalized_content": "你将听到一篇 Peter 讲述去阳朔旅行的短文，录音播放两遍。请根据所听到的内容，依据思维导图的提示，在60秒内复述短文。"},
            {"item_id": "reading", "sequence": 17, "normalized_content": "现在，你有15秒钟的时间阅读思维导图。"},
            {"item_id": "starter", "sequence": 18, "normalized_content": "你的复述可以这样开始：Peter visited Yangshuo ..."},
        ]
        result, _batch = self._analyze(
            items,
            [(entry["start"], entry["end"]) for entry in fixture["speech"]],
            [entry["text"] for entry in fixture["speech"]],
        )

        self.assertTrue(result["can_commit"])
        self.assertEqual(result["matched_item_count"], 19)
        self.assertEqual(result["missing_item_ids"], [])
        self.assertEqual(result["files"][-1]["source_spans"], [
            {"start_seconds": 421.784, "end_seconds": 426.726},
        ])
        chronological = sorted(span for clip in self.cut_spans for span in clip)
        self.assertTrue(all(left[1] <= right[0] for left, right in zip(chronological, chronological[1:])))

    def test_short_question_ends_at_the_announcement_even_when_asr_mishears_the_name(self) -> None:
        items = [{"item_id": "q10", "sequence": 0, "normalized_content": "What do people think of Amy?"}]
        result, _batch = self._analyze(items, [(1, 3), (4, 5)], ["what do people think of f", "开始录音。"])

        self.assertTrue(result["can_commit"])
        self.assertEqual(self.cut_spans, [((1.0, 3.0),)])

    def test_short_question_without_a_closing_announcement_keeps_the_strict_ending(self) -> None:
        items = [{"item_id": "q10", "sequence": 0, "normalized_content": "What do people think of Amy?"}]
        occurrences = audio_asr._continuous_audio_matches(
            [{"text": "what do people think of f"}], [(1, 3)], items,
        )

        self.assertEqual(occurrences, [])

    def test_long_passage_missing_its_ending_is_not_completed_by_an_announcement(self) -> None:
        body = (
            "One night, I had a dream. A robot cooks breakfast for me. I go to work by car. "
            "The car drives by itself. What an unforgettable trip!"
        )
        spoken = body.rsplit(" What", 1)[0]
        items = [{"item_id": "passage", "sequence": 0, "normalized_content": body}]
        occurrences = audio_asr._continuous_audio_matches(
            [{"text": spoken}, {"text": "开始录音。"}], [(1, 30), (31, 32)], items,
        )

        self.assertEqual(occurrences, [])

    def test_real_shenzhen_2024_set_two_recovers_the_question_and_retelling_clips(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "audio_asr_exam_shenzhen_2024_set2.json").read_text(encoding="utf-8")
        )
        result, _batch = self._analyze(
            fixture["items"],
            [(entry["start"], entry["end"]) for entry in fixture["speech"]],
            [entry["text"] for entry in fixture["speech"]],
        )

        self.assertTrue(result["can_commit"])
        self.assertEqual(result["required_item_count"], 19)
        self.assertEqual(result["missing_item_ids"], [])
        spans_by_item = {
            row["suggested_item_id"]: [
                (span["start_seconds"], span["end_seconds"]) for span in row["source_spans"]
            ]
            for row in result["files"]
        }
        self.assertEqual(spans_by_item["item-13"], [(662.93, 665.265)])
        self.assertEqual(spans_by_item["item-15"], [(740.631, 781.156), (782.878, 823.404)])


class AudioRecognitionContextTests(unittest.TestCase):
    def test_a_short_file_is_full_recognition_not_a_partial_opening(self) -> None:
        recognizer = SimpleNamespace(
            create_stream=lambda: SimpleNamespace(accept_waveform=lambda *_: None),
            decode_stream=lambda stream: setattr(stream, "result", {"text": "The car cannot drive by itself."}),
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(audio_asr, "_ffmpeg_executable", return_value="ffmpeg"),
            patch.object(audio_asr, "_decode_audio_window", return_value=np.zeros(3 * 16000, dtype=np.float32)),
        ):
            rows = audio_asr.AudioAsrExtension(temporary).recognize(["short.mp3"], recognizer=recognizer)
        self.assertEqual(rows[0]["scope"], "full")

    def _recognize(self, spans, results):
        class Recognizer:
            calls = 0

            def create_stream(self):
                return SimpleNamespace(accept_waveform=lambda *_: None)

            def decode_stream(self, stream):
                stream.result = results[self.calls]
                self.calls += 1

        recognizer = Recognizer()
        progress = []
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(audio_asr, "_ffmpeg_executable", return_value="ffmpeg"),
            patch.object(audio_asr, "_decode_audio_window", return_value=np.zeros(16000, dtype=np.float32)),
        ):
            rows = audio_asr.AudioAsrExtension(temporary).recognize_spans(
                "source.mp3", spans, recognizer=recognizer, progress_callback=progress.append,
            )
        return rows, recognizer.calls, progress

    def test_short_speech_uses_context_and_token_times_restore_each_source_span(self) -> None:
        rows, calls, progress = self._recognize(
            [(10, 11), (11.7, 13), (14, 16)],
            [{"text": "One night, I had a dream. Next, I went out.",
              "tokens": ["One", " night", ",", " I", " had", " a", " dream", ".", " Next", ",", " I", " went", " out", "."],
              "timestamps": [0.1, 0.4, 0.8, 1.8, 2.0, 2.1, 2.5, 2.9, 4.1, 4.5, 4.7, 5.0, 5.4, 5.8]}],
        )

        self.assertEqual(calls, 1)
        self.assertEqual([row["text"] for row in rows], ["One night,", "I had a dream.", "Next, I went out."])
        self.assertEqual(progress[-1]["completed"], 3)
        self.assertTrue(all(not row["error"] for row in rows))

    def test_missing_timestamps_falls_back_to_separate_decodes(self) -> None:
        rows, calls, _progress = self._recognize(
            [(1, 2), (3, 4)],
            [{"text": "Hello world."}, {"text": "Hello"}, {"text": "world."}],
        )

        self.assertEqual(calls, 3)
        self.assertEqual([row["text"] for row in rows], ["Hello", "world."])

    def test_long_pauses_and_context_limit_start_new_decodes(self) -> None:
        rows, calls, _progress = self._recognize(
            [(0, 10), (11, 20), (24, 25)],
            [{"text": "First."}, {"text": "Second."}, {"text": "Third."}],
        )

        self.assertEqual(calls, 3)
        self.assertEqual([row["text"] for row in rows], ["First.", "Second.", "Third."])

    def test_pause_boundaries_retain_the_whole_speech_including_soft_edges(self) -> None:
        probe = SimpleNamespace(stderr=b"Duration: 00:00:06.00")
        silence = SimpleNamespace(returncode=0, stderr=(
            b"silence_start: 0\nsilence_end: 1\n"
            b"silence_start: 2\nsilence_end: 2.5\n"
            b"silence_start: 4\nsilence_end: 6\n"
        ))
        with (
            patch.object(audio_asr, "_ffmpeg_executable", return_value="ffmpeg"),
            patch.object(audio_asr.subprocess, "run", side_effect=[probe, silence]),
        ):
            duration, spans = audio_asr._audio_duration_and_speech_spans("source.mp3")

        self.assertEqual(duration, 6)
        self.assertEqual(len(spans), 2)
        for (start, end), (speech_start, speech_end) in zip(spans, [(1, 2), (2.5, 4)]):
            self.assertLess(start, speech_start)
            self.assertGreater(end, speech_end)
        self.assertLess(spans[0][1], spans[1][0])


if __name__ == "__main__":
    unittest.main()
