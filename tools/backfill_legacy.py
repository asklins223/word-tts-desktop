"""存量数据回填：旧 work_items / progress.json / worksId → legacy_aliases。

方案 7.2：旧数据迁移可重复执行，无法匹配的记录保留为 legacy 记录
进入人工确认，不阻塞新文档解析；重复执行不产生重复行。

用法::

    .venv/bin/python tools/backfill_legacy.py [session_dir ...]
      # 不带参数时扫描 .runtime 下全部会话 progress.json
"""

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

DEFAULT_RUNTIME = os.path.join(PROJECT_ROOT, ".runtime")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def alias_id(kind, value, target_kind, target_id):
    key = f"{kind}|{value}|{target_kind}|{target_id}"
    return f"alias:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"


def backfill_progress_file(conn, progress_path):
    """把一个旧 progress.json 的条目回填为 legacy 别名（幂等）。"""
    with open(progress_path, encoding="utf-8") as fh:
        progress = json.load(fh)
    inserted = 0
    for item in progress.get("items", []):
        raw = item.get("raw_item") or {}
        category = item.get("category") or raw.get("category") or ""
        number = raw.get("number", item.get("seq"))
        # 旧条目只携带 category+序号：身份按 locator 规则重建；
        # 无法确定业务实体时记 FILENAME 别名进入人工确认，不强行匹配
        locator = f"{category}/题目{number}" if number is not None else \
            f"{category}/录音稿{raw.get('index', item.get('seq'))}"
        rows = conn.execute(
            """SELECT 'QUESTION', question_id FROM question_items
               WHERE question_id = ? UNION ALL
               SELECT 'STIMULUS', stimulus_id FROM stimuli
               WHERE stimulus_id = ?""",
            (f"question:{os.path.basename(progress_path)}:{locator}",
             f"stimulus:{os.path.basename(progress_path)}:{locator}"),
        ).fetchall()
        if rows:
            target_kind, target_id = rows[0]
        else:
            target_kind, target_id = "WORK_ITEM", item.get("id", locator)
        conn.execute(
            """INSERT OR IGNORE INTO legacy_aliases
               (alias_id, alias_kind, alias_value, target_kind, target_id,
                target_revision_id, created_at)
               VALUES (?, 'PROGRESS_ITEM', ?, ?, ?, NULL, ?)""",
            (alias_id("PROGRESS_ITEM", progress_path, target_kind, target_id),
             progress_path, target_kind, target_id, _now()),
        )
        inserted += 1
    for works_id in (progress.get("xunfei_works_ids") or {}).values() \
            if isinstance(progress.get("xunfei_works_ids"), dict) else []:
        conn.execute(
            """INSERT OR IGNORE INTO legacy_aliases
               (alias_id, alias_kind, alias_value, target_kind, target_id,
                target_revision_id, created_at)
               VALUES (?, 'WORKS_ID', ?, 'WORK_ITEM', ?, NULL, ?)""",
            (alias_id("WORKS_ID", works_id, "WORK_ITEM", works_id),
             works_id, works_id, _now()),
        )
    return inserted


def main():
    from app_paths import ensure_data_dir

    db_path = os.path.join(str(ensure_data_dir()), "workflow.db")
    if not os.path.exists(db_path):
        print(f"[跳过] workflow 数据库不存在: {db_path}")
        return 1
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    from question_model import sync_sub_type_registry

    sync_sub_type_registry(conn)
    conn.commit()

    paths = sys.argv[1:] or [
        os.path.join(dirpath, "progress.json")
        for dirpath, _, files in os.walk(DEFAULT_RUNTIME)
        if "progress.json" in files
    ]
    total = 0
    for path in paths:
        try:
            total += backfill_progress_file(conn, path)
            print(f"[回填] {path}")
        except Exception as exc:
            print(f"[失败] {path}: {exc}")
    conn.commit()
    conn.close()
    print(f"完成：处理 {len(paths)} 个会话，{total} 条旧条目参与回填"
          "（重复执行幂等）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
