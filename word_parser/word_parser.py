#!/usr/bin/env python3
"""
Word 文档解析（兼容门面）
=========================
原单文件实现已按题型拆分到 ``question_types/`` 包：每个题型一个切片
模块，同时持有解析器与该题型的全部静态定义（展示颜色、文件名关键词、
内容识别标记、音色策略）；题型注册表与文档自动识别逻辑位于
``question_types/__init__.py``。

本文件保留原有模块路径与公共 API，供三种既有加载方式继续使用：
  - wordtts.bootstrap 将本目录加入 sys.path 后按顶层模块导入
  - workflow.parser 以 spec_from_file_location 按路径加载
  - pytest 测试将本目录插入 sys.path 后导入
"""

import json
import os
import sys

# 门面可能从任意工作目录被加载；先确保项目根目录在 sys.path 上，
# 才能导入同级的 question_types 包。
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from question_types import (  # noqa: E402,F401
    CONTENT_MARKERS,
    PARSER_MAP,
    QUESTION_TYPES,
    QUESTION_TYPE_MAP,
    TYPE_COLORS,
    BaseParser,
    ExcelVocabularyParser,
    ImitationReadingParser,
    InfoAcquisitionParser,
    InfoRetellingParser,
    ListeningResponseParser,
    ListeningSelectionParser,
    TextReadingParser,
    clean_whitespace,
    detect_doc_type,
    detect_types_in_content,
    is_chinese,
    load_paragraphs,
    parse_document_auto,
    remove_zero_width,
    sanitize,
    split_sentences,
)


# ============================================================================
# 命令行示例入口（路径相对本文件，仅供 python word_parser/word_parser.py 使用）
# ============================================================================
PARSER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PARSER_DIR)
WORD_DIR = os.path.join(PROJECT_ROOT, "examples", "documents")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "examples", "parsed")


def main():
    """主函数：遍历 examples/documents，解析所有 Word/Excel 文档。"""
    print("=" * 70)
    print("文档解析脚本")
    print(f"输入目录: {WORD_DIR}")
    print(f"输出目录: {OUTPUT_DIR}")
    print("=" * 70)

    if not os.path.isdir(WORD_DIR):
        print(f"[警告] 输入目录不存在: {WORD_DIR}")
        return

    word_files = [
        f for f in os.listdir(WORD_DIR)
        if (f.endswith('.docx') or f.endswith('.xlsx')) and not f.startswith('~$')
    ]

    if not word_files:
        print("[警告] 输入目录中没有找到 .docx 或 .xlsx 文件")
        return

    all_results = []

    for fname in sorted(word_files):
        filepath = os.path.join(WORD_DIR, fname)
        doc_type = detect_doc_type(fname)

        if doc_type is None:
            print(f"\n[跳过] 未识别类型的文件: {fname}")
            continue

        parser_cls = PARSER_MAP[doc_type]
        try:
            parser = parser_cls(filepath)
            result = parser.parse()
        except Exception as e:
            print(f"\n[错误] 解析失败: {fname} — {e}")
            continue

        all_results.append(result)

        # 打印摘要
        print(f"\n[{doc_type}] {fname}")
        print(f"  共提取 {result['item_count']} 条内容:")

        for item in result["items"]:
            preview = item["text"][:80].replace('\n', ' ')
            cat = item["category"]

            if "number" in item:
                print(f"  · [{cat}] #{item['number']:>2}  {preview}...")
            elif "sentence_number" in item and "discourse_number" in item:
                print(f"  · [{cat}] 语篇{item['discourse_number']}-{item['sentence_number']}  {preview}...")
            elif "sentence_number" in item:
                print(f"  · [{cat}] 句{item['sentence_number']}  {preview}...")
            elif "index" in item:
                print(f"  · [{cat}] #{item['index']:>2}  {preview}...")
            elif "unit" in item:
                print(f"  · [{cat}] ({item['unit']:<3}) {preview}...")
            elif "discourse_number" in item:
                print(f"  · [{cat}] 语篇{item['discourse_number']}  {preview}...")
            else:
                print(f"  · [{cat}] {preview}...")

    # 保存汇总 JSON
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "parsed_results.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    total_items = sum(r["item_count"] for r in all_results)
    print(f"解析完成！共处理 {len(all_results)} 个文件，提取 {total_items} 条内容")
    print(f"结果已保存到: {output_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
