"""题型注册表测试：各派生视图与唯一权威注册表保持一致。

新增题型的正确姿势（方案 2A 单一权威注册表）：
1. question_model.model 注册 QuestionFamily + QuestionSubType；
2. 新建解析器切片（BaseParser 子类）；
3. question_types.__init__ 的 PARSERS_BY_FAMILY 绑定一行。
本测试保证派生的解析映射、内容识别、展示颜色、文件名识别与
音色兼容视图完整无缺；切片不再各自声明 QUESTION_TYPE。
"""
from __future__ import annotations

import unittest

import wordtts.config
from question_types import (
    CONTENT_MARKERS,
    PARSER_MAP,
    QUESTION_TYPES,
    TYPE_COLORS,
    BaseParser,
    detect_doc_type,
    detect_types_in_content,
)


# 解析与文件名识别依赖这个固定顺序，不可随意调整。
EXPECTED_ORDER = [
    "信息获取",
    "听后选择",
    "听后应答",
    "课文跟读",
    "信息转述及询问",
    "听后记录并转述信息",
    "模仿朗读",
    "词汇",
]


class QuestionTypeRegistryTests(unittest.TestCase):
    def test_registry_order_is_stable(self):
        self.assertEqual(list(PARSER_MAP.keys()), EXPECTED_ORDER)
        self.assertEqual([qt.key for qt in QUESTION_TYPES], EXPECTED_ORDER)

    def test_every_slice_declares_complete_metadata(self):
        for question_type in QUESTION_TYPES:
            self.assertTrue(
                issubclass(question_type.parser, BaseParser),
                f"{question_type.key} 的 parser 必须是 BaseParser 子类",
            )
            self.assertTrue(
                question_type.color.startswith("#"),
                f"{question_type.key} 缺少展示颜色",
            )
            self.assertIn(question_type.key, TYPE_COLORS)
            self.assertIn(question_type.key, CONTENT_MARKERS)

    def test_type_colors_are_derived_from_family_registry(self):
        self.assertEqual(TYPE_COLORS["信息获取"], "#0e7490")
        self.assertEqual(TYPE_COLORS["词汇"], "#1e40af")
        self.assertEqual(len(TYPE_COLORS), len(QUESTION_TYPES))

    def test_detect_doc_type_by_keyword_and_extension(self):
        self.assertEqual(detect_doc_type("7上-信息获取.docx"), "信息获取")
        self.assertEqual(detect_doc_type("听后应答-x.docx"), "听后应答")
        self.assertEqual(detect_doc_type("信息转述练习.docx"), "信息转述及询问")
        self.assertEqual(
            detect_doc_type("七上Starter Unit 1 Hello听后记录并转述信息专项.docx"),
            "听后记录并转述信息",
        )
        # Excel 文件统一归为词汇类型，优先于文件名关键词。
        self.assertEqual(detect_doc_type("单词表.xlsx"), "词汇")
        self.assertIsNone(detect_doc_type("随便命名的文档.docx"))

    def test_content_markers_detect_types_in_fixed_order(self):
        paras = [
            (0, "第一节 听选信息", "Normal"),
            (1, "一、模仿朗读", "Normal"),
        ]
        self.assertEqual(detect_types_in_content(paras), ["信息获取", "模仿朗读"])
        # 词汇只支持 Excel 模板，没有内容识别标记。
        self.assertEqual(CONTENT_MARKERS.get("词汇", ()), ())

    def test_force_female_categories_are_derived_from_family_registry(self):
        # 词汇 family 声明「单词/例句」强制默认女声；其余题型不强制。
        # 源头：FAMILY_REGISTRY["vocabulary"].female_categories →
        # QUESTION_TYPES.force_female_categories → wordtts WORD_CATEGORIES。
        self.assertEqual(sorted(wordtts.config.WORD_CATEGORIES), ["例句", "单词"])


if __name__ == "__main__":
    unittest.main()
