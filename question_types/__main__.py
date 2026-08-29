"""命令行批量解析入口：``python -m question_types``。

遍历 examples/documents 下的 Word/Excel 文档，解析全部可识别题型并
把汇总 JSON 保存到 examples/parsed/parsed_results.json。这是原
word_parser 模块的示例用法，随题型注册表一起保留。
"""

import json
import os
import sys

QUESTION_TYPES_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(QUESTION_TYPES_DIR)
WORD_DIR = os.path.join(PROJECT_ROOT, "examples", "documents")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "examples", "parsed")

from question_types import PARSER_MAP, detect_doc_type


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
