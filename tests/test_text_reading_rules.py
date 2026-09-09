from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from question_types import TextReadingParser, detect_document_type, split_sentences  # noqa: E402


class TextReadingRuleTests(unittest.TestCase):
    @staticmethod
    def _save_new_format(
        path: Path,
        title: str = "课文跟读新格式",
        include_conversations: bool = True,
    ) -> None:
        document = Document()
        dialogue = [
            "Conversation 2",
            "Bob: Bob's line.",
            "Alice: Alice's line.",
            "Conversation 1",
            "Teacher: Hello, class.",
            "Class: Hello, teacher.",
        ]
        if not include_conversations:
            dialogue = [
                "Bob: Bob's line.",
                "Alice: Alice's line.",
                "Teacher: Hello, class.",
                "Class: Hello, teacher.",
            ]
        paragraphs = [
            title,
            "Section A",
            "句子跟读",
            "2. Second sentence.",
            "中文：第二句。",
            "1. First sentence.",
            "中文：第一句。",
            "段落跟读",
            *dialogue,
            "Section B",
            "语篇跟读：",
            "// Welcome",
            "A short introduction.",
            "The First Story",
            "The first story text is here.",
            "A second paragraph follows.",
        ]
        for text in paragraphs:
            document.add_paragraph(text)
        document.save(path)

    @staticmethod
    def _save_legacy_format(path: Path) -> None:
        document = Document()
        document.add_heading("Understanding Idea", level=1)
        document.add_heading("句子跟读", level=2)
        document.add_paragraph("1. One sentence.")
        document.add_heading("段落跟读", level=2)
        document.add_paragraph("First sentence. Second sentence.")
        document.add_heading("语篇跟读", level=2)
        document.add_paragraph("语篇1")
        document.add_paragraph("First discourse sentence. Second discourse sentence.")
        document.save(path)

    @staticmethod
    def _save_role_discourse_document(path: Path) -> None:
        document = Document()
        for text in [
            "课程跟读-角色结构样本",
            "Section B",
            "语篇跟读",
            "Conversation 1",
            "Teng Fei: Good morning.",
            "Emma: Good morning, Teng Fei.",
        ]:
            document.add_paragraph(text)
        document.save(path)

    def test_section_format_uses_structure_for_dialogue_mode_and_article_boundaries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读新格式.docx")
            self._save_new_format(path, include_conversations=False)
            parser = TextReadingParser(path)
            result = parser.parse()

        sentences = [item for item in result["items"] if item["category"] == "句子跟读"]
        paragraphs = [item for item in result["items"] if item["category"] == "段落跟读"]
        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]

        self.assertEqual([item["filename_stem"] for item in sentences], ["SA句子1", "SA句子2"])
        self.assertEqual([item["text"] for item in sentences], ["First sentence.", "Second sentence."])
        self.assertTrue(all(item["voice"] == "female" for item in sentences))

        self.assertEqual(
            [item["filename_stem"] for item in paragraphs],
            ["SA段落1", "SA段落2", "SA段落3", "SA段落4"],
        )
        self.assertEqual(parser._detect_section_ab_profile()["role_audio_mode"], "aggregate")
        self.assertEqual(
            [item["role"] for item in paragraphs],
            ["Bob", "Alice", "Teacher", "Class"],
        )
        self.assertEqual(
            [item["text"] for item in paragraphs],
            [
                "Bob's line.",
                "Alice's line.",
                "Hello, class.",
                "Hello, teacher.",
            ],
        )

        # 标题行是文章分组结构：不再生成音频条目，正文挂 article_title。
        self.assertEqual(
            [item["filename_stem"] for item in discourses],
            ["SB语篇1", "SB语篇2", "SB语篇3"],
        )
        self.assertEqual(
            [item["text"] for item in discourses],
            [
                "A short introduction.",
                "The first story text is here.",
                "A second paragraph follows.",
            ],
        )
        self.assertEqual(
            [item["article_title"] for item in discourses],
            ["Welcome", "The First Story", "The First Story"],
        )
        self.assertNotIn("paragraph_title", discourses[0])

    def test_legacy_format_keeps_legacy_sentence_splitting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读旧格式.docx")
            self._save_legacy_format(path)
            result = TextReadingParser(path).parse()

        paragraphs = [item for item in result["items"] if item["category"] == "段落跟读"]
        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]

        self.assertEqual(len(paragraphs), 2)
        self.assertEqual([item["filename_stem"] for item in paragraphs], ["U-段落1", "U-段落2"])
        self.assertEqual(len(discourses), 2)
        self.assertEqual(
            [item["filename_stem"] for item in discourses],
            ["U-语篇1-1", "U-语篇1-2"],
        )

    def test_legacy_role_discourse_exposes_roles_without_crashing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读旧格式角色语篇.docx")
            document = Document()
            document.add_heading("Understanding Idea", level=1)
            document.add_heading("语篇跟读", level=2)
            document.add_paragraph("Reporter: Welcome.")
            document.add_paragraph("Student: Thank you.")
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        self.assertEqual([item["role"] for item in discourses], ["Reporter", "Student"])
        self.assertEqual(
            {item["paragraph_scope"] for item in discourses},
            {"U:语篇跟读:discourse-1"},
        )
        self.assertEqual(
            {item["paragraph_id"] for item in discourses},
            {"U:语篇跟读:discourse-1:dialogue"},
        )

    def test_new_non_dialogue_paragraph_is_entered_sentence_by_sentence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读新版普通段落.docx")
            document = Document()
            for text in [
                "课程跟读-普通段落",
                "Section A",
                "段落跟读",
                "This is one ordinary paragraph. It contains two sentences.",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        paragraphs = [item for item in result["items"] if item["category"] == "段落跟读"]
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(
            [item["text"] for item in paragraphs],
            ["This is one ordinary paragraph.", "It contains two sentences."],
        )
        self.assertTrue(all("role" not in item for item in paragraphs))

    def test_conversation_structure_splits_each_role_into_an_audio_and_exposes_role(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-对话结构样本.docx")
            self._save_new_format(path, title="课程对话结构样本")
            parser = TextReadingParser(path)
            result = parser.parse()

        paragraphs = [item for item in result["items"] if item["category"] == "段落跟读"]
        self.assertEqual(parser._detect_section_ab_profile()["role_audio_mode"], "per_role")
        self.assertEqual(
            [item["filename_stem"] for item in paragraphs],
            [
                "SA-段-C2-1",
                "SA-段-C2-2",
                "SA-段-C1-3",
                "SA-段-C1-4",
            ],
        )
        self.assertEqual(
            [item["role"] for item in paragraphs],
            ["Bob", "Alice", "Teacher", "Class"],
        )
        self.assertEqual(
            [item["text"] for item in paragraphs],
            [
                "Bob's line.",
                "Alice's line.",
                "Hello, class.",
                "Hello, teacher.",
            ],
        )

    def test_sentence_role_prefix_is_removed_and_role_is_preserved(self):
        self.assertEqual(
            TextReadingParser._role_label("Mr. Yan: Hello."),
            "Mr. Yan",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-句子角色样本.docx")
            document = Document()
            for text in [
                "课程跟读-句子角色样本",
                "Section A",
                "句子跟读",
                "1. Pauline: Hello, everyone.",
                "中文：大家好。",
                "2. Peter： Nice to meet you.",
                "中文：很高兴认识你。",
                "3. 小明：Hi.",
                "中文：你好。",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        sentences = [item for item in result["items"] if item["category"] == "句子跟读"]
        self.assertEqual(
            [item["role"] for item in sentences],
            ["Pauline", "Peter", "小明"],
        )
        self.assertEqual(
            [item["text"] for item in sentences],
            ["Hello, everyone.", "Nice to meet you.", "Hi."],
        )
        self.assertEqual(
            [item["translation"] for item in sentences],
            ["大家好。", "很高兴认识你。", "你好。"],
        )
        self.assertTrue(all(item["entry_form"] == "角色扮演" for item in sentences))

    def test_repeated_single_role_lines_without_conversation_are_still_roleplay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-单角色对话样本.docx")
            document = Document()
            for text in [
                "课程跟读-单角色对话样本",
                "Section B",
                "段落跟读",
                "Teacher: First line.",
                "Teacher: Second line.",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        paragraphs = [item for item in result["items"] if item["category"] == "段落跟读"]
        self.assertEqual([item["role"] for item in paragraphs], ["Teacher", "Teacher"])
        self.assertEqual([item["text"] for item in paragraphs], ["First line.", "Second line."])

    def test_conversation_discourse_also_splits_each_role(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-角色对话.docx")
            self._save_role_discourse_document(path)
            parser = TextReadingParser(path)
            result = parser.parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        self.assertEqual(parser._detect_section_ab_profile()["role_audio_mode"], "per_role")
        self.assertEqual(
            [item["filename_stem"] for item in discourses],
            ["SB语篇-C1-1", "SB语篇-C1-2"],
        )
        self.assertEqual([item["role"] for item in discourses], ["Teng Fei", "Emma"])

    def test_conversation_naming_covers_both_reading_modes_and_multiple_blocks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-多Conversation样本.docx")
            document = Document()
            for text in [
                "课程跟读-多Conversation样本",
                "Section A",
                "段落跟读",
                "Conversation 1",
                "Reporter: First report.",
                "Student: First reply.",
                "Conversation 2",
                "Reporter: Second report.",
                "Student: Second reply.",
                "语篇跟读",
                "Conversation 3",
                "Reporter: First passage.",
                "Student: First answer.",
                "Conversation 4",
                "Reporter: Second passage.",
                "Student: Second answer.",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        paragraphs = [item for item in result["items"] if item["category"] == "段落跟读"]
        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        self.assertEqual(
            [item["filename_stem"] for item in paragraphs],
            ["SA-段-C1-1", "SA-段-C1-2", "SA-段-C2-3", "SA-段-C2-4"],
        )
        self.assertEqual(
            [item["filename_stem"] for item in discourses],
            ["SA语篇-C3-1", "SA语篇-C3-2", "SA语篇-C4-1", "SA语篇-C4-2"],
        )

    def test_numbered_discourse_groups_keep_distinct_paragraph_scopes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-多个语篇组.docx")
            document = Document()
            for text in [
                "课程跟读-多个语篇组",
                "Section B",
                "语篇跟读",
                "语篇1",
                "First article",
                "The first article has one sentence.",
                "语篇2",
                "Second article",
                "The second article has one sentence.",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        self.assertEqual(
            {item["paragraph_scope"] for item in discourses},
            {
                "SB:语篇跟读:article-1:discourse-1",
                "SB:语篇跟读:article-1:discourse-2",
            },
        )

    def test_isolated_section_marker_does_not_switch_to_new_parser(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读旧格式含备注.docx")
            document = Document()
            for text in [
                "Section A",
                "Section A is mentioned in the teacher's note.",
                "Understanding Idea",
                "句子跟读",
                "1. Legacy sentence.",
            ]:
                document.add_paragraph(text)
            document.save(path)

            parser = TextReadingParser(path)
            self.assertFalse(parser._is_section_ab_format())
            result = parser.parse()

        self.assertEqual(result["items"][0]["filename_stem"], "U-句子1")

    def test_new_structure_wins_when_old_and_new_sections_are_both_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读混合结构.docx")
            document = Document()
            for text in [
                "Understanding Idea",
                "句子跟读",
                "1. Old sentence.",
                "Section A",
                "句子跟读",
                "1. New sentence.",
            ]:
                document.add_paragraph(text)
            document.save(path)

            parser = TextReadingParser(path)
            self.assertTrue(parser._is_section_ab_format())
            result = parser.parse()

        self.assertEqual(
            [item["filename_stem"] for item in result["items"]],
            ["SA句子1"],
        )
        self.assertEqual(result["items"][0]["text"], "New sentence.")

    def test_reading_plus_uses_rp_prefix_and_inline_slash_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读人教九上U1.docx")
            document = Document()
            for text in [
                "课文跟读人教九上U1",
                "Section B",
                "Reading Plus",
                "语篇跟读",
                "RP title",
                "//Small title\nSmall title paragraph.",
                "Last paragraph.",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        # 标题（RP title / //Small title）是分组结构，不再单独成条。
        self.assertEqual(
            [item["filename_stem"] for item in discourses],
            ["RP语篇1", "RP语篇2"],
        )
        self.assertTrue(all(item["section"] == "Reading Plus" for item in discourses))
        self.assertEqual(
            [item["text"] for item in discourses],
            ["Small title paragraph.", "Last paragraph."],
        )
        self.assertEqual(
            [item["article_title"] for item in discourses],
            ["RP title", "RP title"],
        )
        self.assertEqual(
            [item.get("paragraph_title") for item in discourses],
            ["Small title", None],
        )

    def test_sentence_splitter_keeps_common_english_abbreviations_together(self):
        self.assertEqual(
            split_sentences(
                "Mr. Brown went home. Dr. Smith stayed. "
                "The U.S. President arrived. For example, e.g. this is one sentence."
            ),
            [
                "Mr. Brown went home.",
                "Dr. Smith stayed.",
                "The U.S. President arrived.",
                "For example, e.g. this is one sentence.",
            ],
        )

    def test_vocabulary_type_is_only_detected_for_excel_templates(self):
        root = Path(__file__).resolve().parents[1] / "examples" / "documents"
        self.assertEqual(detect_document_type(root / "U6单词导入模板.xlsx"), "词汇")
        self.assertIsNone(detect_document_type("词汇-G7-u1.docx"))

    def test_bold_title_becomes_article_title_instead_of_audio_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-标题格式提示样本.docx")
            document = Document()
            for text in ["课程跟读-标题格式提示样本", "Section B", "语篇跟读"]:
                document.add_paragraph(text)
            title = document.add_paragraph()
            title_run = title.add_run("Article title")
            title_run.bold = True
            document.add_paragraph("This is a short paragraph. Another clause")
            document.add_paragraph("Final sentence.")
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        # 粗体标题是文章分组结构：正文条目挂 article_title，标题不朗读。
        self.assertEqual(
            [item["text"] for item in discourses],
            [
                "This is a short paragraph.",
                "Another clause",
                "Final sentence.",
            ],
        )
        self.assertEqual(
            [item["article_title"] for item in discourses],
            ["Article title", "Article title", "Article title"],
        )

    def test_content_before_first_conversation_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-Conversation前置正文.docx")
            document = Document()
            for text in [
                "课程跟读-Conversation前置正文",
                "Section B",
                "段落跟读",
                "Introductory paragraph.",
                "Conversation 1",
                "Teacher: Welcome.",
                "Student: Thank you.",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        paragraphs = [item for item in result["items"] if item["category"] == "段落跟读"]
        self.assertEqual(
            [item["text"] for item in paragraphs],
            ["Introductory paragraph.", "Welcome.", "Thank you."],
        )

    def test_discourse_content_before_first_conversation_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-语篇Conversation前置正文.docx")
            document = Document()
            for text in [
                "课程跟读-语篇Conversation前置正文",
                "Section B",
                "语篇跟读",
                "Introductory paragraph.",
                "Conversation 1",
                "Teacher: Welcome.",
                "Student: Thank you.",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        self.assertEqual(
            [item["text"] for item in discourses],
            ["Introductory paragraph.", "Welcome.", "Thank you."],
        )
        self.assertEqual(
            [item["filename_stem"] for item in discourses],
            ["SB语篇1", "SB语篇-C1-1", "SB语篇-C1-2"],
        )

    def test_heading_format_hint_only_applies_to_first_unit_of_a_paragraph(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-同段落标题正文.docx")
            document = Document()
            for text in ["课程跟读-同段落标题正文", "Section B", "语篇跟读"]:
                document.add_paragraph(text)
            paragraph = document.add_paragraph()
            first_run = paragraph.add_run("// Small title")
            first_run.bold = True
            paragraph.add_run("\nThe body remains a sentence.")
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        # 格式提示只作用于段内第一个单元：标题成为文章结构，正文照常。
        self.assertEqual(
            [item["text"] for item in discourses],
            ["The body remains a sentence."],
        )
        self.assertEqual(discourses[0]["article_title"], "Small title")

    def test_reading_plus_big_title_is_not_used_as_paragraph_title(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-ReadingPlus大标题.docx")
            document = Document()
            for text in [
                "课程跟读-ReadingPlus大标题",
                "Section A",
                "句子跟读",
                "1. Warm up.",
                "Section B",
                "语篇跟读",
                "Reading Plus",
                "Making New Friends at School",
                "This is the only paragraph.",
            ]:
                document.add_paragraph(text)
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        self.assertEqual(len(discourses), 1)
        self.assertNotIn("paragraph_title", discourses[0])

    def test_reading_plus_subheadings_stay_in_one_article(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-ReadingPlus小标题.docx")
            document = Document()
            for text in [
                "课程跟读-ReadingPlus小标题",
                "Section B",
                "Reading Plus",
                "语篇跟读",
            ]:
                document.add_paragraph(text)
            big_title = document.add_paragraph()
            big_title.add_run("Love My Hometown, Build My Hometown").bold = True
            document.add_paragraph(
                "The introduction has two sentences. This is still one paragraph."
            )
            for title, body in [
                ("The watermelon farmer", "The first story has two sentences. It stays together."),
                ("The flower live-streamer", "The second story has two sentences. It stays together."),
                ("The village teacher", "The third story has two sentences. It stays together."),
            ]:
                heading = document.add_paragraph()
                heading.add_run(title).bold = True
                document.add_paragraph(body)
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        paragraph_ids = []
        paragraph_titles = []
        for item in discourses:
            if item["paragraph_id"] not in paragraph_ids:
                paragraph_ids.append(item["paragraph_id"])
                paragraph_titles.append(item.get("paragraph_title"))

        self.assertEqual(
            {item["article_title"] for item in discourses},
            {"Love My Hometown, Build My Hometown"},
        )
        self.assertEqual(
            {item["paragraph_scope"] for item in discourses},
            {"RP:语篇跟读:article-1"},
        )
        self.assertEqual(paragraph_titles, [
            None,
            "The watermelon farmer",
            "The flower live-streamer",
            "The village teacher",
        ])
        self.assertEqual(len(paragraph_ids), 4)

    def test_reading_plus_subheading_does_not_leak_to_next_unheaded_paragraph(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-ReadingPlus标题不继承.docx")
            document = Document()
            for text in [
                "课程跟读-ReadingPlus标题不继承",
                "Section B",
                "语篇跟读",
                "Reading Plus",
                "Article title",
            ]:
                document.add_paragraph(text)
            heading = document.add_paragraph()
            heading.add_run("Named paragraph").bold = True
            document.add_paragraph("The named paragraph has one sentence.")
            document.add_paragraph("This paragraph has no small heading.")
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        by_paragraph = {}
        for item in discourses:
            by_paragraph.setdefault(item["paragraph_id"], item.get("paragraph_title"))
        self.assertEqual(
            list(by_paragraph.values()),
            ["Named paragraph", None],
        )

    def test_bold_article_title_and_subheadings_stay_in_one_article(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "课文跟读-文章大标题和人物小标题.docx")
            document = Document()
            for text in ["Unit 1", "Section B", "语篇跟读"]:
                document.add_paragraph(text)
            for title in ["Making new friends", "Pauline Lee"]:
                heading = document.add_paragraph()
                heading.add_run(title).bold = True
            document.add_paragraph("Pauline's paragraph has two sentences. It stays together.")
            peter_heading = document.add_paragraph()
            peter_heading.add_run("Peter Brown").bold = True
            document.add_paragraph("Peter's paragraph has two sentences. It stays together.")
            document.add_paragraph("Reading Plus").runs[0].bold = True
            reading_title = document.add_paragraph()
            reading_title.add_run("Making New Friends at School").bold = True
            document.add_paragraph("The advice has two sentences. It is one paragraph.")
            document.save(path)
            result = TextReadingParser(path).parse()

        discourses = [item for item in result["items"] if item["category"] == "语篇跟读"]
        main_article = [item for item in discourses if item["article_title"] == "Making new friends"]
        main_paragraphs = []
        for item in main_article:
            if item["paragraph_id"] not in {paragraph_id for paragraph_id, _ in main_paragraphs}:
                main_paragraphs.append((item["paragraph_id"], item.get("paragraph_title")))

        self.assertEqual(len(main_article), 4)
        self.assertEqual(
            [title for _, title in main_paragraphs],
            ["Pauline Lee", "Peter Brown"],
        )
        self.assertEqual(
            {item["paragraph_scope"] for item in main_article},
            {"SB:语篇跟读:article-1"},
        )

        reading_article = [
            item for item in discourses
            if item["article_title"] == "Making New Friends at School"
        ]
        self.assertEqual(len(reading_article), 2)
        self.assertEqual(
            {item["paragraph_scope"] for item in reading_article},
            {"RP:语篇跟读:article-1"},
        )


if __name__ == "__main__":
    unittest.main()
