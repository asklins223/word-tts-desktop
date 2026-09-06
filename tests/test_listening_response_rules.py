from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from question_types import (  # noqa: E402
    ListeningResponseParser,
)
from question_types.segmenter import parse_document_once
import wordtts as core  # noqa: E402


class ListeningResponseRuleTests(unittest.TestCase):
    @staticmethod
    def _make_document(path: Path) -> None:
        document = Document()
        paragraphs = [
            "七上 Starter Unit 1 Hello! 听后应答专项",
            "二、听后应答（共7小题）",
            "（计算机语音和屏幕文字提示）听句子，朗读正确应答语。每小题在5秒钟内完成。",
            "（计算机语音提示）听下面1个句子。",
            "Good morning, class.",
            "（计算机语音提示）请朗读应答语。",
            "（计算机屏幕显示5秒倒计时进度条）",
            "★ Good morning, Peter.\t\t★ Good morning, Ms Gao.",
            "（计算机语音提示）听下面2个句子。",
            "First sentence.",
            "Second sentence.",
            "（计算机语音提示）请朗读应答语。",
            "（计算机语音提示）听下面1个句子。",
            "Inline one.\nInline two.",
            "（计算机语音提示）请朗读应答语。",
        ]
        for text in paragraphs:
            document.add_paragraph(text)
        document.save(path)

    def test_prompt_blocks_extract_each_content_line_and_ignore_answers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "S1-听后应答.docx")
            self._make_document(path)
            result = ListeningResponseParser(path).parse()

        self.assertEqual(result["doc_type"], "听后应答")
        self.assertEqual(result["item_count"], 5)
        self.assertEqual(
            [item["filename_stem"] for item in result["items"]],
            [
                "7上-应答-1",
                "7上-应答-2",
                "7上-应答-3",
                "7上-应答-4",
                "7上-应答-5",
            ],
        )
        self.assertEqual(
            [item["text"] for item in result["items"]],
            [
                "Good morning, class.",
                "First sentence.",
                "Second sentence.",
                "Inline one.",
                "Inline two.",
            ],
        )
        self.assertTrue(all(item["voice"] == "female" for item in result["items"]))
        self.assertNotIn("Good morning, Peter", " ".join(item["text"] for item in result["items"]))
        self.assertEqual(
            [question["answer_time"] for question in result["questions"]],
            [5] * 5,
        )

    def test_auto_detection_and_progress_keep_response_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "S1-听后应答.docx")
            self._make_document(path)
            results, summary = parse_document_once(path)

        self.assertIn("检测到 1 种题型", summary)
        self.assertEqual([result["doc_type"] for result in results], ["听后应答"])
        progress = core.build_progress(
            path.name,
            str(path),
            results,
            {},
        )
        self.assertEqual(
            [item["filename"] for item in progress["items"]],
            [
                "7上-应答-1.mp3",
                "7上-应答-2.mp3",
                "7上-应答-3.mp3",
                "7上-应答-4.mp3",
                "7上-应答-5.mp3",
            ],
        )
        self.assertTrue(all(item["voice_override"] == "female" for item in progress["items"]))

    def test_multiple_word_paragraphs_are_not_truncated_by_prompt_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "九上-听后应答.docx")
            document = Document()
            for text in [
                "九上 听后应答",
                "（计算机语音提示）听下面1个句子。",
                "First response line.",
                "Second response line.",
                "（计算机语音提示）请朗读应答语。",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = ListeningResponseParser(path).parse()

        self.assertEqual(
            [item["filename_stem"] for item in result["items"]],
            ["9上-应答-1", "9上-应答-2"],
        )
        self.assertEqual(
            [item["text"] for item in result["items"]],
            ["First response line.", "Second response line."],
        )

    def test_response_keeps_star_prefix_and_uses_red_option_as_answer(self):
        option_row = "★ In the box.   ★ Purple."
        paras = [
            (0, "二、听后应答（每小题1分，满分1分）", "Normal"),
            (1, "（计算机语音提示）听下面1个句子。", "Normal"),
            (2, "Where is it?", "Normal"),
            (3, "（计算机语音提示）请朗读应答语。", "Normal"),
            (4, option_row, "Normal"),
        ]
        metadata = [{}, {}, {}, {}, {
            "colored_runs": [{
                "start": option_row.index("Purple"),
                "end": len(option_row),
                "text": "Purple.",
                "rgb": "FF0000",
            }],
        }]

        result = ListeningResponseParser(
            "red-response.docx",
            preloaded_paras=(paras, metadata),
        ).parse()

        question = result["questions"][0]
        self.assertEqual(question["answer"], "B")
        self.assertEqual(
            [option["text"] for option in question["options"]],
            ["★ In the box.", "★ Purple."],
        )
        self.assertEqual(result["score_per_item"], 1)
        self.assertEqual(result["section_score"], 1)
        self.assertEqual(
            result["items"][0]["major_section_profile"],
            "response_unknown",
        )
        self.assertIsNone(result["items"][0]["entry_profile"])
        self.assertFalse(result["items"][0]["capabilities"]["external_input"])

    def test_confirmed_response_special_keeps_source_numbers_and_opens_entry(self):
        paras = [
            (0, "二、听后应答（共7小题，每小题1分，满分7分）", "Normal"),
            (1, "（计算机语音和屏幕文字提示）听句子，朗读正确应答语。每小题在5秒钟内完成。", "Normal"),
        ]
        metadata = [{}, {}]
        expected_answers = []
        for index in range(7):
            question_number = 9 + index
            prompt = f"Prompt {index + 1}?"
            first = f"First response {index + 1}."
            second = f"Second response {index + 1}."
            answer_index = index % 2
            option_row = f"{question_number}.★ {first}   ★ {second}"
            red_text = first if answer_index == 0 else second
            red_start = option_row.index(red_text)
            paras.extend([
                (len(paras), "（计算机语音提示）听下面1个句子。", "Normal"),
                (len(paras) + 1, prompt, "Normal"),
                (len(paras) + 2, "（计算机语音提示）请朗读应答语。", "Normal"),
                (len(paras) + 3, option_row, "Normal"),
            ])
            metadata.extend([
                {},
                {},
                {},
                {
                    "colored_runs": [{
                        "start": red_start,
                        "end": red_start + len(red_text),
                        "text": red_text,
                        "rgb": "FF0000",
                    }],
                },
            ])
            expected_answers.append("A" if answer_index == 0 else "B")

        result = ListeningResponseParser(
            "S2-听后应答.docx",
            preloaded_paras=(paras, metadata),
        ).parse()

        self.assertEqual([item["number"] for item in result["items"]], list(range(9, 16)))
        self.assertEqual([question["number"] for question in result["questions"]], list(range(9, 16)))
        self.assertEqual([question["answer"] for question in result["questions"]], expected_answers)
        self.assertTrue(all(
            item["major_section_profile"] == "response_colored_options_special"
            and item["entry_profile"] == "listening_response_v1"
            and item["capabilities"]["external_input"] is True
            for item in result["items"]
        ))


if __name__ == "__main__":
    unittest.main()
