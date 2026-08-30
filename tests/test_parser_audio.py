from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from workflow.artifact_store import ArtifactStore
from workflow.audio import AudioError, AudioProcessor, AudioVerifier, SegmentBoundary, looks_like_mp3_bytes, validate_segment_boundaries
from workflow.parser import LegacyWordParser, ParserError, document_hash, iter_json_items


class ParserAudioTests(unittest.TestCase):
    def test_parser_normalization_is_deterministic_and_redacts_sensitive_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-parser-test-") as tmp:
            source = Path(tmp) / "lesson.docx"
            source.write_bytes(b"test source")
            calls = []

            def parse(_path):
                calls.append(_path)
                return ([{
                    "doc_type": "听后选择",
                    "items": [{
                        "category": "对话录音稿",
                        "filename_stem": "题目1",
                        "text": "Hello world.",
                        "role": "W",
                        "voice": "male",
                        "question_type": "listening_info",
                        "type_path": ["信息获取", "听选信息"],
                        "metadata": {"authorization": "secret"},
                    }],
                }], "ok")

            parser = LegacyWordParser(parse_callable=parse, parser_version="14")
            first = parser.parse(source)
            second = parser.parse(source)
            self.assertEqual(first.as_dict(), second.as_dict())
            self.assertEqual(first.items[0].sequence, 0)
            self.assertTrue(first.items[0].identity_key.startswith(first.source_sha256[:32]))
            self.assertEqual(first.items[0].metadata["category"], "对话录音稿")
            self.assertEqual(first.items[0].metadata["voice"], "male")
            self.assertEqual(first.items[0].metadata["question_type"], "listening_info")
            self.assertEqual(first.items[0].metadata["type_path"], ["信息获取", "听选信息"])
            self.assertNotIn("authorization", first.items[0].metadata)
            self.assertEqual(len(calls), 2)

            with self.assertRaises(ParserError):
                LegacyWordParser(parse_callable=parse).parse(source.with_suffix(".txt"))

    def test_document_hash_and_json_item_iterator_keep_input_facts_stable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-parser-json-") as tmp:
            source = Path(tmp) / "parsed.json"
            source.write_text('{"items":[{"text":"one"},{"text":"two"}]}', encoding="utf-8")
            digest, size = document_hash(source)
            self.assertEqual(size, source.stat().st_size)
            self.assertEqual(len(digest), 64)
            self.assertEqual([item["text"] for item in iter_json_items(source)], ["one", "two"])

    def test_audio_verifier_and_processor_are_streaming_and_content_addressed(self) -> None:
        chunks = (chunk for chunk in (b"a", b"b", b"c"))
        fingerprint = AudioVerifier().fingerprint(chunks)
        self.assertEqual((fingerprint.size_bytes, fingerprint.sha256), (3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"))
        with tempfile.TemporaryDirectory(prefix="wordtts-audio-test-") as tmp:
            processor = AudioProcessor(ArtifactStore(Path(tmp) / "artifacts"))
            blob, metadata = processor.process(io.BytesIO(b"audio"), format="mp3")
            self.assertEqual(blob.sha256, metadata.sha256)
            self.assertEqual(blob.format, "mp3")
            with self.assertRaises(AudioError):
                processor.process(io.BytesIO(b""), format="mp3")

    def test_mp3_publication_check_is_bounded_and_rejects_plain_bytes(self) -> None:
        self.assertFalse(looks_like_mp3_bytes(b"ID3\x04\x00\x00payload"))
        self.assertTrue(looks_like_mp3_bytes(b"\xff\xfb\x90\x64payload"))
        self.assertFalse(looks_like_mp3_bytes(b"\xff\xe0\x90\x64payload"))
        self.assertFalse(looks_like_mp3_bytes(b"plain provider bytes"))

    def test_segment_boundary_validation_rejects_overlap_gap_and_bad_indexes(self) -> None:
        valid = validate_segment_boundaries(
            [SegmentBoundary(0, 0, 100), SegmentBoundary(1, 100, 250)],
            duration_ms=250,
            require_contiguous=True,
        )
        self.assertEqual(valid[-1].end_ms, 250)
        cases = [
            [SegmentBoundary(0, 0, 100), SegmentBoundary(2, 100, 250)],
            [SegmentBoundary(0, 0, 100), SegmentBoundary(1, 90, 250)],
            [SegmentBoundary(0, 10, 100)],
        ]
        for boundaries in cases:
            with self.assertRaises(AudioError):
                validate_segment_boundaries(boundaries, duration_ms=250, require_contiguous=True)


if __name__ == "__main__":
    unittest.main()
