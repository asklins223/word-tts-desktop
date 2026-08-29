"""解析基线快照工具。

在原子小题模型改造开始前，把 examples/documents 下所有示例文档按当前
解析规则得到的完整结果单独存档，供后续对照回归：

- 应用路径：``parse_document_auto``（内容识别，支持一份文档多种题型）；
- CLI 路径：``detect_doc_type`` 文件名识别后的单题型解析；
- 旧链路投影：``wordtts.progress.build_progress`` 派生的音频条目
  （id/category/seq/文件名/音色），这是 category→前缀、文件名去重、
  音色策略等数据处理最容易回归的地方。

用法::

    .venv/bin/python tools/parse_baseline.py                  # 快照到默认 label
    .venv/bin/python tools/parse_baseline.py --label 手工命名
    .venv/bin/python tools/parse_baseline.py --compare <目录>  # 与已有基线对照
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

BASELINE_ROOT = os.path.join(PROJECT_ROOT, "examples", "baselines", "parse")
DOC_DIR = os.path.join(PROJECT_ROOT, "examples", "documents")

# progress 投影条目只保留解析派生字段；raw_item/xunfei_works_ids 等运行时
# 字段与 parse_results 重复或恒为空值，不进入快照。
PROGRESS_ITEM_FIELDS = (
    "id",
    "doc_type",
    "category",
    "seq",
    "filename",
    "voice_override",
    "text_preview",
    "merged",
    "merged_count",
)

TS_SENTINEL = "<normalized-timestamp>"


def canonical_sha256(obj):
    """对任意可 JSON 化对象算稳定哈希（键排序、紧凑分隔符）。"""
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def git_state():
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
        capture_output=True, text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
        capture_output=True, text=True,
    ).stdout.splitlines()
    dirty = sorted(line[3:] for line in status if line.strip())
    return commit, dirty


def safe_stem(source_file):
    stem = os.path.splitext(source_file)[0]
    return re.sub(r'[\\/:*?"<>|]', "_", stem)


def list_documents():
    return sorted(
        f for f in os.listdir(DOC_DIR)
        if (f.endswith(".docx") or f.endswith(".xlsx")) and not f.startswith("~$")
    )


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_one_document(filepath):
    """对单个文档跑两条现有解析路径，返回快照片段（纯数据，不含时间戳）。"""
    from question_types import PARSER_MAP, detect_doc_type, parse_document_auto

    filename = os.path.basename(filepath)

    results, summary = parse_document_auto(filepath)

    cli_result = None
    cli_error = None
    doc_type = detect_doc_type(filename)
    if doc_type is not None:
        try:
            cli_result = PARSER_MAP[doc_type](filepath).parse()
        except Exception as exc:  # CLI 路径解析失败也要进快照，方便对照
            cli_error = f"{type(exc).__name__}: {exc}"

    from wordtts.config import AUDIO_ALGORITHM_VERSION, PARSER_VERSION
    from wordtts.progress import build_progress
    from wordtts.tts_config import normalize_tts_config

    progress = build_progress(filename, filepath, results, {})
    progress_items = [
        {key: item.get(key) for key in PROGRESS_ITEM_FIELDS}
        for item in progress["items"]
    ]
    default_config = progress["config"]

    doc_entry = {
        "source_file": filename,
        "sha256": file_sha256(filepath),
        "content_detected_types": [
            r["doc_type"] for r in results
        ],
        "auto_summary": summary,
        "parse_results": results,
        "cli_filename_type": doc_type,
        "cli_result": cli_result,
        "cli_error": cli_error,
        "progress_items": progress_items,
    }
    fingerprints = {
        "parse_results": canonical_sha256(results),
        "cli_result": canonical_sha256(cli_result),
        "progress_items": canonical_sha256(progress_items),
    }
    versions = {
        "parser_version": PARSER_VERSION,
        "audio_algorithm_version": AUDIO_ALGORITHM_VERSION,
        "default_tts_config": default_config,
    }
    return doc_entry, fingerprints, versions, bool(progress_items) or bool(results)


def capture(label):
    from question_types import QUESTION_TYPES

    commit, dirty = git_state()
    documents = []
    category_totals = {}
    for filename in list_documents():
        filepath = os.path.join(DOC_DIR, filename)
        doc_entry, fingerprints, versions, _ = parse_one_document(filepath)
        documents.append({**doc_entry, "fingerprints": fingerprints})

        counts = {}
        for result in doc_entry["parse_results"]:
            for item in result["items"]:
                counts[item.get("category", "")] = counts.get(item.get("category", ""), 0) + 1
        for cat, n in counts.items():
            category_totals[cat] = category_totals.get(cat, 0) + n
        print(f"[快照] {filename}: {sum(counts.values())} 条 "
              f"({', '.join(doc_entry['content_detected_types']) or '无题型'})")

    out_dir = os.path.join(BASELINE_ROOT, label)
    docs_dir = os.path.join(out_dir, "docs")
    os.makedirs(docs_dir, exist_ok=True)

    for entry in documents:
        doc_path = os.path.join(docs_dir, safe_stem(entry["source_file"]) + ".json")
        with open(doc_path, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    parse_all = [
        {"source_file": e["source_file"], "parse_results": e["parse_results"]}
        for e in documents
    ]
    manifest = {
        "label": label,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "git_dirty_files": dirty,
        "data_format_version": open(os.path.join(PROJECT_ROOT, "DATA_FORMAT_VERSION")).read().strip(),
        "parser_version": versions["parser_version"],
        "audio_algorithm_version": versions["audio_algorithm_version"],
        "question_type_registry": [qt.key for qt in QUESTION_TYPES],
        "default_tts_config": versions["default_tts_config"],
        "totals": {
            "documents": len(documents),
            "items_by_category": dict(sorted(category_totals.items())),
            "items_total": sum(category_totals.values()),
        },
        "documents": [
            {
                "source_file": e["source_file"],
                "sha256": e["sha256"],
                "content_detected_types": e["content_detected_types"],
                "cli_filename_type": e["cli_filename_type"],
                "item_count": sum(
                    r["item_count"] for r in e["parse_results"]
                ),
                "fingerprints": e["fingerprints"],
            }
            for e in documents
        ],
        "fingerprints": {
            "parse_results_all": canonical_sha256(parse_all),
            "progress_items_all": canonical_sha256([
                {"source_file": e["source_file"], "progress_items": e["progress_items"]}
                for e in documents
            ]),
        },
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"\n基线已写入: {out_dir}")
    print(f"  文档数: {manifest['totals']['documents']}  "
          f"条目总数: {manifest['totals']['items_total']}")
    print(f"  parse_results 指纹: {manifest['fingerprints']['parse_results_all'][:16]}…")
    print(f"  progress 投影指纹:  {manifest['fingerprints']['progress_items_all'][:16]}…")
    return out_dir


def load_baseline(out_dir):
    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def compare(left_dir, right_dir):
    left = load_baseline(left_dir)
    right = load_baseline(right_dir)
    print(f"旧基线: {left_dir} (commit {left['git_commit'][:8]}, "
          f"parser_version={left['parser_version']})")
    print(f"新基线: {right_dir} (commit {right['git_commit'][:8]}, "
          f"parser_version={right['parser_version']})")
    if left["parser_version"] != right["parser_version"]:
        print("[提示] parser_version 不同，差异可能是规则有意变更")

    old_by_file = {d["source_file"]: d for d in left["documents"]}
    new_by_file = {d["source_file"]: d for d in right["documents"]}
    problems = 0
    for filename in sorted(set(old_by_file) | set(new_by_file)):
        if filename not in new_by_file:
            print(f"[缺失] 新基线缺少文档: {filename}")
            problems += 1
            continue
        if filename not in old_by_file:
            print(f"[新增] 新基线新增文档: {filename}")
            problems += 1
            continue
        old_fp = old_by_file[filename]["fingerprints"]
        new_fp = new_by_file[filename]["fingerprints"]
        for key in ("parse_results", "cli_result", "progress_items"):
            if old_fp[key] != new_fp[key]:
                print(f"[差异] {filename} · {key} 不一致")
                problems += 1
                old_doc = json.load(open(os.path.join(
                    left_dir, "docs", safe_stem(filename) + ".json"), encoding="utf-8"))
                new_doc = json.load(open(os.path.join(
                    right_dir, "docs", safe_stem(filename) + ".json"), encoding="utf-8"))
                field = {
                    "parse_results": "parse_results",
                    "cli_result": "cli_result",
                    "progress_items": "progress_items",
                }[key]
                _print_json_diff(old_doc.get(field), new_doc.get(field), filename, key)
    if problems == 0:
        print("\n结论: 全部文档逐字节一致，解析行为无回归")
    else:
        print(f"\n结论: 共 {problems} 处差异，详见上方")
    return 1 if problems else 0


def _print_json_diff(old, new, filename, key, max_lines=60):
    import difflib

    old_text = json.dumps(old, ensure_ascii=False, indent=2).splitlines()
    new_text = json.dumps(new, ensure_ascii=False, indent=2).splitlines()
    diff = list(difflib.unified_diff(
        old_text, new_text,
        fromfile=f"{filename}:{key}(旧)", tofile=f"{filename}:{key}(新)", lineterm="",
    ))
    for line in diff[:max_lines]:
        print("    " + line)
    if len(diff) > max_lines:
        print(f"    … 差异共 {len(diff)} 行，已截断")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    default_label = datetime.now().strftime("%Y%m%d") + "-baseline"
    p_cap = sub.add_parser("capture", help="生成解析基线快照")
    p_cap.add_argument("--label", default=default_label)

    p_cmp = sub.add_parser("compare", help="对比两份基线")
    p_cmp.add_argument("old_dir")
    p_cmp.add_argument("new_dir")

    args = parser.parse_args()
    if args.command == "compare":
        sys.exit(compare(args.old_dir, args.new_dir))
    else:
        capture(args.label if args.command == "capture" else default_label)


if __name__ == "__main__":
    main()
