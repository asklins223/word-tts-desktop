"""AUDIO_GENERATE 任务 → 现有 work_items 的兼容投影（方案 7.2 双模型单向派生）。

- ``work_items`` 仍是音频 delivery projection：一个 AUDIO 任务投影为一个
  work_item，item_id 确定性派生（幂等重投影）；
- 投影是单向的：规范化模型 → work_items，绝不反向；
- work_item 与业务实体的追溯关系写进 metadata_json（operation_id、
  scope、成员小题），并同步写 ``legacy_aliases``（WORK_ITEM → 实体版本）。
"""

from __future__ import annotations

import json
import sqlite3

from .persistence import _now


def project_audio_tasks_to_work_items(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    workflow_id: str,
    now: str | None = None,
) -> list[dict]:
    """把一个 plan 的 AUDIO_GENERATE 任务投影为 work_items（幂等）。

    ``workflow_id`` 是现有 workflow 引擎创建的运行；执行状态、attempt、
    provider 事实仍归 workflow 表所有，本函数只写投影行与别名。
    """
    created = now or _now()
    tasks = conn.execute(
        """
        SELECT t.operation_id, t.scope_row_id,
               tt.target_kind, tt.target_id, tt.target_revision_id
        FROM operation_tasks t
        JOIN operation_task_targets tt ON tt.operation_id = t.operation_id
        WHERE t.plan_id = ? AND t.operation_type = 'AUDIO_GENERATE'
          AND tt.role = 'primary'
        ORDER BY t.operation_id
        """,
        (plan_id,),
    ).fetchall()

    projected = []
    for sequence, (operation_id, scope_row_id, target_kind, target_id,
                   target_revision_id) in enumerate(tasks):
        content, voice_policy = _load_target_content(conn, target_revision_id)
        members = conn.execute(
            """
            SELECT m.target_kind, m.target_id, m.target_revision_id
            FROM operation_scope_members m
            WHERE m.scope_row_id = ? ORDER BY m.ordinal
            """,
            (scope_row_id,),
        ).fetchall()
        metadata = {
            "operation_id": operation_id,
            "scope_row_id": scope_row_id,
            "members": [list(m) for m in members],
            "projection": "atomic-question-model/v1",
        }
        item_id = f"audio:{operation_id}"
        conn.execute(
            """
            INSERT OR IGNORE INTO work_items
                (item_id, workflow_id, item_identity_key, item_type, sequence,
                 identity_version, source_locator, normalized_content,
                 content_hash, role, voice_key, metadata_json, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, 'audio', ?, '1', NULL, ?, ?, ?, ?, ?,
                    'PENDING', ?, ?)
            """,
            (item_id, workflow_id, operation_id, sequence, content,
             _hash_of(content), _sub_type_of(conn, target_revision_id,
                                             target_kind),
             voice_policy, json.dumps(metadata, ensure_ascii=False),
             created, created),
        )
        # 主目标的 legacy 别名（work_item → 业务实体版本）
        conn.execute(
            """
            INSERT OR IGNORE INTO legacy_aliases
                (alias_id, alias_kind, alias_value, target_kind, target_id,
                 target_revision_id, created_at)
            VALUES (?, 'WORK_ITEM', ?, ?, ?, ?, ?)
            """,
            (f"alias:{item_id}", item_id, target_kind, target_id,
             target_revision_id, created),
        )
        projected.append({
            "item_id": item_id,
            "operation_id": operation_id,
            "target_kind": target_kind,
            "target_id": target_id,
        })
    return projected


def _load_target_content(conn: sqlite3.Connection, revision_id: str):
    for table, pk, text_col in (
        ("stimulus_revisions", "stimulus_revision_id", "text"),
        ("content_unit_revisions", "content_unit_revision_id", "text"),
        ("question_revisions", "question_revision_id", "stem"),
    ):
        row = conn.execute(
            f"SELECT {text_col} FROM {table} WHERE {pk} = ?",
            (revision_id,),
        ).fetchone()
        if row:
            return row[0], None
    raise ValueError(f"目标版本不存在: {revision_id}")


def _sub_type_of(conn: sqlite3.Connection, revision_id: str, kind: str):
    table, pk = {
        "STIMULUS": ("stimulus_revisions", "stimulus_revision_id"),
        "CONTENT_UNIT": ("content_unit_revisions", "content_unit_revision_id"),
        "QUESTION": ("question_revisions", "question_revision_id"),
    }[kind]
    row = conn.execute(
        f"SELECT sub_type_code FROM {table} WHERE {pk} = ?",
        (revision_id,),
    ).fetchone()
    return row[0] if row else None


def _hash_of(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()
