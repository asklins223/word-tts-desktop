from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "word_parser"))

from word_parser import InfoAcquisitionParser  # noqa: E402
import word_tts_app as core  # noqa: E402


class InfoAcquisitionQuestionRulesTests(unittest.TestCase):
    @staticmethod
    def _make_document(path: Path) -> None:
        document = Document()
        paragraphs = [
            "第一节 听选信息",
            "听第一段对话，回答第1—2两个问题。",
            "1. What is Amy doing?",
            "（Reading. / Singing. / Running.）",
            "2．Where is Tom?",
            "（At home. / At school. / In a park.）",
            "录音稿：",
            "W: Amy is reading.",
            "M: Tom is at school.",
            "第二节 回答问题",
            "3、Who helps Amy?",
            "4) Why is Tom happy?",
            "录音稿：",
            "(M) Their teacher helps them.",
            "参考答案：",
            "3. Their teacher.",
            "4. Because he finished his work.",
        ]
        for text in paragraphs:
            document.add_paragraph(text)
        document.save(path)

    def test_questions_continue_across_sections_and_alternate_voices(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "信息获取.docx")
            self._make_document(path)
            result = InfoAcquisitionParser(path).parse()

        questions = [
            item for item in result["items"] if item["category"].endswith("题目")
        ]
        self.assertEqual([item["number"] for item in questions], [1, 2, 3, 4])
        self.assertEqual(
            [item["category"] for item in questions],
            ["听选信息题目", "听选信息题目", "回答问题题目", "回答问题题目"],
        )
        self.assertEqual(
            [item["voice"] for item in questions],
            ["male", "female", "male", "female"],
        )
        self.assertEqual(
            [item["text"] for item in questions],
            [
                "M: What is Amy doing?",
                "W: Where is Tom?",
                "M: Who helps Amy?",
                "W: Why is Tom happy?",
            ],
        )
        self.assertEqual(
            [item["filename_stem"] for item in questions],
            ["问题1", "问题2", "问题3", "问题4"],
        )

        synthesized_segments = [core.parse_speakers(item["text"])[0] for item in questions]
        self.assertEqual(
            [voice for voice, _ in synthesized_segments],
            [
                core.MALE_VOICE,
                core.FEMALE_VOICE,
                core.MALE_VOICE,
                core.FEMALE_VOICE,
            ],
        )
        self.assertEqual(
            [text for _, text in synthesized_segments],
            [
                "What is Amy doing?",
                "Where is Tom?",
                "Who helps Amy?",
                "Why is Tom happy?",
            ],
        )

        scripts = [
            item for item in result["items"] if item["category"].endswith("录音稿")
        ]
        self.assertEqual(len(scripts), 2)

    def test_question_audio_files_use_question_numbers(self):
        parse_results = [{
            "doc_type": "信息获取",
            "items": [
                {
                    "category": "听选信息题目",
                    "number": 1,
                    "filename_stem": "问题1",
                    "text": "M: First question?",
                },
                {
                    "category": "回答问题题目",
                    "number": 7,
                    "filename_stem": "问题7",
                    "text": "M: Seventh question?",
                },
                {
                    "category": "回答问题录音稿",
                    "index": 1,
                    "text": "(W) Script text.",
                },
            ],
        }]

        progress = core.build_progress(
            "信息获取.docx",
            "/tmp/信息获取.docx",
            parse_results,
            {"format": "mp3"},
        )

        self.assertEqual(
            [item["filename"] for item in progress["items"]],
            ["问题1.mp3", "问题7.mp3", "回答问题-录音稿1.mp3"],
        )


if __name__ == "__main__":
    unittest.main()
