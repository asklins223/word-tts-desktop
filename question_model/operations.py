"""OperationPlan / OperationScope / OperationTask 创建（阶段 2）。

方案 3.0/3.3/3.4 的落地面：

- 一个 ``OperationPlan`` 对应一个文档版本（plan_id 含 document revision），
  重新规划 = 新 plan，不在旧 plan 上原地改；
- ``OperationScope`` 不可变：同一 scope_id 成员变化产生新 scope_revision，
  同成员幂等复用既有 revision；成员快照保存在 operation_scope_members；
- ``OperationTask(AUDIO_GENERATE)`` 的逻辑身份 = 任务类型 + scope revision，
  天然幂等；执行状态仍由现有 workflow 表负责（workflow_step_id 先留空，
  阶段 4 统一 runner 接线时回填）；
- ``scope_kind`` 能力矩阵按小题型注册表的 item_role 推导，越界直接拒绝
  （方案 3.4：计划阶段发现范围不支持就阻断，不进入有副作用的提交）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .model import FAMILY_DISPLAY_NAMES_BY_CODE  # noqa: F401  保留展示映射入口
from .persistence import _now

# scope_kind 能力矩阵：按小题型 item_role 推导（方案 3.4 能力矩阵首版）。
SCOPE_KINDS_BY_ROLE = {
    "question": ("QUESTION", "GROUP", "STIMULUS", "MAJOR_SECTION", "DOCUMENT"),
    "stimulus": ("STIMULUS", "GROUP", "MAJOR_SECTION", "DOCUMENT"),
    "content": ("CONTENT_UNIT", "DOCUMENT"),
}

# 实体版本表：target_kind → (成员表, 版本表, 版本主键列)
_ENTITY_TABLES = {
    "QUESTION": ("question_items", "question_revisions", "question_revision_id"),
    "STIMULUS": ("stimuli", "stimulus_revisions", "stimulus_revision_id"),
    "CONTENT_UNIT": ("content_units", "content_unit_revisions",
                     "content_unit_revision_id"),
}


def canonical_member_hash(members: list[dict]) -> str:
    payload = json.dumps(members, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_scope_kind(sub_type_code: str, scope_kind: str) -> None:
    """能力矩阵校验：不支持的范围在计划阶段直接阻断。"""
    from .model import SUB_TYPE_REGISTRY

    sub_type = SUB_TYPE_REGISTRY.get(sub_type_code)
    if sub_type is None:
        raise ValueError(f"未注册的小题型: {sub_type_code}")
    allowed = SCOPE_KINDS_BY_ROLE[sub_type.item_role]
    if scope_kind not in allowed:
        raise ValueError(
            f"小题型 {sub_type_code} 不支持 {scope_kind} 范围"
            f"（允许: {', '.join(allowed)}）"
        )


def create_operation_plan(
    conn: sqlite3.Connection,
    *,
    source_document_id: str,
    document_revision_id: str,
    configuration: dict[str, Any] | None = None,
    workflow_group_id: str | None = None,
    workflow_id: str | None = None,
    now: str | None = None,
) -> str:
    """幂等创建操作计划；一个文档版本一个 plan。"""
    created = now or _now()
    plan_id = f"plan:{document_revision_id}"
    conn.execute(
        """
        INSERT OR IGNORE INTO operation_plans
            (plan_id, source_document_id, document_revision_id,
             workflow_group_id, workflow_id, configuration_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (plan_id, source_document_id, document_revision_id,
         workflow_group_id, workflow_id,
         json.dumps(configuration or {}, ensure_ascii=False), created),
    )
    return plan_id


def create_scope(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    scope_kind: str,
    members: list[dict],
    scope_id: str | None = None,
    now: str | None = None,
) -> dict:
    """创建不可变目标范围；同 scope_id 成员变化 → 新 scope_revision。

    ``members`` 元素: ``{"target_kind", "target_id", "target_revision_id"}``；
    目标版本必须真实存在（外键 + 显式校验）。
    """
    created = now or _now()
    normalized = sorted(
        ({
            "target_kind": m["target_kind"],
            "target_id": m["target_id"],
            "target_revision_id": m.get("target_revision_id"),
        }
         for m in members),
        key=lambda m: (m["target_kind"], m["target_id"],
                       m["target_revision_id"] or ""),
    )
    if not normalized:
        raise ValueError("scope 成员不能为空")
    member_hash = canonical_member_hash(normalized)
    if scope_id is None:
        scope_id = f"scope:auto:{plan_id}:{scope_kind}:{member_hash[:12]}"

    # 同成员幂等复用
    existing = conn.execute(
        """
        SELECT s.scope_row_id, s.scope_revision FROM operation_scopes s
        WHERE s.scope_id = ? AND s.member_hash = ?
        ORDER BY s.scope_revision DESC LIMIT 1
        """,
        (scope_id, member_hash),
    ).fetchone()
    if existing:
        return {"scope_id": scope_id, "scope_row_id": existing[0],
                "scope_revision": existing[1], "reused": True}

    max_revision = conn.execute(
        "SELECT COALESCE(MAX(scope_revision), 0) FROM operation_scopes "
        "WHERE scope_id = ?",
        (scope_id,),
    ).fetchone()[0]
    scope_revision = max_revision + 1
    scope_row_id = f"{scope_id}:v{scope_revision}"
    conn.execute(
        """
        INSERT INTO operation_scopes
            (scope_row_id, scope_id, scope_revision, scope_kind, plan_id,
             member_hash, payload_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (scope_row_id, scope_id, scope_revision, scope_kind, plan_id,
         member_hash, member_hash, created),
    )
    for ordinal, member in enumerate(normalized):
        _validate_member_target(conn, member)
        conn.execute(
            """
            INSERT INTO operation_scope_members
                (member_id, scope_row_id, target_kind, target_id,
                 target_revision_id, ordinal)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (f"member:{scope_row_id}:{ordinal}", scope_row_id,
             member["target_kind"], member["target_id"],
             member["target_revision_id"], ordinal),
        )
    return {"scope_id": scope_id, "scope_row_id": scope_row_id,
            "scope_revision": scope_revision, "reused": False}


def _validate_member_target(conn: sqlite3.Connection, member: dict) -> None:
    kind = member["target_kind"]
    revision_id = member.get("target_revision_id")
    if kind not in _ENTITY_TABLES:
        return  # GROUP / MAJOR_SECTION / SCOPE 等由调用方保证存在
    _, revision_table, revision_pk = _ENTITY_TABLES[kind]
    if revision_id is None:
        raise ValueError(f"{kind} 成员必须携带具体版本 {revision_pk}")
    row = conn.execute(
        f"SELECT 1 FROM {revision_table} WHERE {revision_pk} = ?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"{kind} 目标版本不存在: {revision_id}")


def create_audio_tasks(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    now: str | None = None,
) -> list[dict]:
    """按音频边界策略生成 AUDIO_GENERATE 任务（幂等）。

    首版策略（方案 3.3/4 节映射）：

    - 材料型小题型（listening_info 等）：每段共享材料一个 ``STIMULUS``
      scope，成员 = 材料版本 + 关联小题版本，任务目标 = 材料版本；
    - 学习内容（课文跟读/词汇）：每个内容单元一个 ``CONTENT_UNIT`` scope。
    """
    created = now or _now()
    plan = conn.execute(
        """SELECT source_document_id, document_revision_id FROM operation_plans
           WHERE plan_id = ?""",
        (plan_id,),
    ).fetchone()
    if plan is None:
        raise ValueError(f"操作计划不存在: {plan_id}")
    source_document_id, document_revision_id = plan

    tasks: list[dict] = []
    tasks += _audio_tasks_for_kind(
        conn, plan_id=plan_id, scope_kind="STIMULUS", entity_kind="STIMULUS",
        document_revision_id=document_revision_id, created=created)
    tasks += _audio_tasks_for_kind(
        conn, plan_id=plan_id, scope_kind="CONTENT_UNIT",
        entity_kind="CONTENT_UNIT",
        document_revision_id=document_revision_id, created=created)
    return tasks


def _audio_tasks_for_kind(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    scope_kind: str,
    entity_kind: str,
    document_revision_id: str,
    created: str,
) -> list[dict]:
    entity_table, revision_table, revision_pk = _ENTITY_TABLES[entity_kind]
    entity_pk = {
        "QUESTION": "question_id", "STIMULUS": "stimulus_id",
        "CONTENT_UNIT": "content_unit_id",
    }[entity_kind]
    rows = conn.execute(
        f"""
        SELECT r.{revision_pk} AS revision_id,
               r.{entity_pk} AS entity_id,
               r.sub_type_code
        FROM document_revision_members m
        JOIN {revision_table} r ON r.{revision_pk} = m.entity_revision_id
        WHERE m.document_revision_id = ? AND m.entity_kind = ?
        ORDER BY m.ordinal
        """,
        (document_revision_id, entity_kind),
    ).fetchall()

    tasks: list[dict] = []
    for revision_id, entity_id, sub_type_code in rows:
        validate_scope_kind(sub_type_code, scope_kind)

        members = [{"target_kind": entity_kind, "target_id": entity_id,
                    "target_revision_id": revision_id}]
        if entity_kind == "STIMULUS":
            # 材料关联的小题进入同一范围（成员快照）
            linked = conn.execute(
                """
                SELECT qr.question_id, qr.question_revision_id
                FROM question_stimuli qs
                JOIN question_revisions qr
                  ON qr.question_revision_id = qs.question_revision_id
                WHERE qs.stimulus_revision_id = ? AND qs.relation_type = 'references'
                ORDER BY qs.ordinal
                """,
                (revision_id,),
            ).fetchall()
            members += [
                {"target_kind": "QUESTION", "target_id": qid,
                 "target_revision_id": qrev}
                for qid, qrev in linked
            ]

        scope = create_scope(
            conn, plan_id=plan_id, scope_kind=scope_kind, members=members,
            now=created)
        operation_id = f"operation:AUDIO_GENERATE:{scope['scope_row_id']}"
        conn.execute(
            """
            INSERT OR IGNORE INTO operation_tasks
                (operation_id, plan_id, operation_type, scope_row_id,
                 created_at)
            VALUES (?, ?, 'AUDIO_GENERATE', ?, ?)
            """,
            (operation_id, plan_id, scope["scope_row_id"], created),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO operation_task_targets
                (target_row_id, operation_id, target_kind, target_id,
                 target_revision_id, ordinal, role)
            VALUES (?, ?, ?, ?, ?, 0, 'primary')
            """,
            (f"target:{operation_id}:0", operation_id, entity_kind,
             entity_id, revision_id),
        )
        tasks.append({
            "operation_id": operation_id,
            "operation_type": "AUDIO_GENERATE",
            "scope_id": scope["scope_id"],
            "scope_row_id": scope["scope_row_id"],
            "scope_revision": scope["scope_revision"],
            "reused_scope": scope["reused"],
            "member_count": len(members),
        })
    return tasks


def add_task_dependency(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    depends_on_operation_id: str,
    dependency_type: str = "AUDIO_ARTIFACT",
    failure_policy: str = "BLOCK",
    now: str | None = None,
) -> str:
    """声明任务依赖（如外部录入依赖音频产物）。"""
    _ = now
    dependency_id = f"dep:{operation_id}:{depends_on_operation_id}"
    conn.execute(
        """
        INSERT OR IGNORE INTO operation_task_dependencies
            (dependency_id, operation_id, depends_on_operation_id,
             dependency_type, failure_policy)
        VALUES (?, ?, ?, ?, ?)
        """,
        (dependency_id, operation_id, depends_on_operation_id,
         dependency_type, failure_policy),
    )
    return dependency_id
