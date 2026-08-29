"""旧链路 → 原子模型桥接（方案 6.4 命令/事件桥接）。

workflow 主链路（application.WorkflowApplicationService.parse）解析
源文档后，由本模块把同一份文档同步落库为原子小题模型：

- ``persist_parse``：文档身份 + revision + 小题/材料/内容单元（幂等）；
- ``create_operation_plan`` + ``create_audio_tasks``：操作计划与音频任务
  （与 work_items 投影同源）；
- ``legacy_execution_sessions``：会话登记为 ``LEGACY_BRIDGED``——
  旧 UI 的执行已经桥接到统一事实源，不再是 out-of-band。

桥接是只追加的旁路写入：失败不阻断主解析流程（旧链路继续可用），
重放由 persist_parse 的幂等性保证。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

BRIDGE_VERSION = "atomic-model/v1"


def bridge_parse_to_atomic_model(
    database,
    *,
    source_path: str | os.PathLike[str],
    filename: str,
    source_sha256: str,
    workflow_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    """把 workflow 主链路的解析结果桥接到原子模型（幂等，失败不抛出）。

    返回桥接摘要（document_revision_id、plan_id、任务数、会话登记）；
    桥接失败时返回 ``{"bridged": False, "error": ...}``，主流程继续。
    """
    from question_model import (
        create_audio_tasks,
        create_operation_plan,
        extract_candidate,
        persist_parse,
        sync_sub_type_registry,
    )
    from question_types import parse_document_auto
    from wordtts.config import PARSER_VERSION

    try:
        results, _ = parse_document_auto(str(source_path))
        if not results:
            # 无可解析内容（含源文件不可读）：不建文档身份，不桥接
            return {"bridged": False, "error": "no parseable items"}
        candidates = [
            extract_candidate(result["doc_type"], result, Path(filename).stem)
            for result in results
        ]
        adjudicated = None
        from question_model import adjudicate

        if candidates:
            adjudicated = adjudicate(
                candidates, explicit_type_code=candidates[0].type_code)

        conn = database.connect(write=True)
        try:
            sync_sub_type_registry(conn)
            persisted = persist_parse(
                conn,
                Path(filename).stem,
                adjudicated.candidates if adjudicated else [],
                file_hash=source_sha256,
                parser_version=PARSER_VERSION,
                now=now,
            )
            plan_id = create_operation_plan(
                conn,
                source_document_id=persisted["source_document_id"],
                document_revision_id=persisted["document_revision_id"],
                now=now,
            )
            tasks = create_audio_tasks(conn, plan_id=plan_id, now=now)
            conn.execute(
                """
                INSERT OR IGNORE INTO legacy_execution_sessions
                    (session_id, source_classification, legacy_source,
                     bridge_version, import_state, recorded_at)
                VALUES (?, 'LEGACY_BRIDGED', ?, ?, 'IMPORTED', ?)
                """,
                (f"legacy:{workflow_id}", f"workflow:{workflow_id}",
                 BRIDGE_VERSION, now),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "bridged": True,
            "document_revision_id": persisted["document_revision_id"],
            "source_document_id": persisted["source_document_id"],
            "plan_id": plan_id,
            "audio_task_count": len(tasks),
            "question_count": persisted["questions"],
            "stimulus_count": persisted["stimuli"],
            "content_unit_count": persisted["content_units"],
        }
    except Exception as exc:  # 桥接失败不阻断主解析流程
        return {"bridged": False, "error": f"{type(exc).__name__}: {exc}"}
