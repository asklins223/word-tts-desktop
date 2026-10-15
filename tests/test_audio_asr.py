from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            patch.object(audio_asr, "_audio_duration_and_speech_spans", return_value=(60.0, spans)),
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


if __name__ == "__main__":
    unittest.main()
