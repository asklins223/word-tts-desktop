"""统一 OperationRunner / OperationAdapter 契约（阶段 4，方案 3.0.1）。

固定执行链：

    validate（纯）→ prepare（纯，不产生外部副作用）
    → record_intent（写 workflow_step + step_attempt 意图与 fencing token）
    → execute（事务外调用适配器，携带 fencing token）
    → observe/verify（回执保存）→ finalize（runner 统一推进状态）

关键约束：

* 状态只写现有 ``workflow_steps``/``step_attempts``，不建第二套状态机；
* 适配器结果分类 SUCCEEDED / RETRYABLE_FAILED / PERMANENT_FAILED /
  AMBIGUOUS / WAITING_USER，由 runner 映射到既有枚举；
* ``AMBIGUOUS`` 的任务不能自动重新执行（方案 5.3），重试必须走人工确认；
* 一个 OperationTask 对应一个 ``scope='workflow'`` 的 workflow_step
  （方案 3.0 冻结映射），step_id 由 operation_id 确定性派生。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from .persistence import _now

STEP_DEFINITION_VERSION = "1"
STEP_TYPE_OPERATION_TASK = "OPERATION_TASK"


class ResultStatus:
    """适配器结果分类（方案 3.0.1）；由 runner 映射到既有 workflow 枚举。"""

    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    PERMANENT_FAILED = "PERMANENT_FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    WAITING_USER = "WAITING_USER"


# 适配器结果 → workflow_steps.status（既有 CHECK 枚举）
STEP_STATUS_BY_RESULT = {
    ResultStatus.SUCCEEDED: "SUCCEEDED",
    ResultStatus.RETRYABLE_FAILED: "WAITING_RETRY",
    ResultStatus.PERMANENT_FAILED: "PERMANENT_FAILED",
    ResultStatus.AMBIGUOUS: "AMBIGUOUS",
    ResultStatus.WAITING_USER: "WAITING_USER",
}

# 适配器结果 → step_attempts.status（attempt 层枚举用 FAILED，
# 无 PERMANENT_FAILED 值——两者语义差异保留在 step 层）
ATTEMPT_STATUS_BY_RESULT = {
    ResultStatus.SUCCEEDED: "SUCCEEDED",
    ResultStatus.RETRYABLE_FAILED: "WAITING_RETRY",
    ResultStatus.PERMANENT_FAILED: "FAILED",
    ResultStatus.AMBIGUOUS: "AMBIGUOUS",
    ResultStatus.WAITING_USER: "WAITING_USER",
}

# 适配器结果 → step_attempts.result_status
ATTEMPT_RESULT_BY_RESULT = {
    ResultStatus.SUCCEEDED: "SUCCEEDED",
    ResultStatus.RETRYABLE_FAILED: "FAILED",
    ResultStatus.PERMANENT_FAILED: "FAILED",
    ResultStatus.AMBIGUOUS: "MIXED",
    ResultStatus.WAITING_USER: "IN_PROGRESS",
}


@dataclass(frozen=True)
class PreparedOperation:
    """prepare 的产物；只含 payload 与交付单元描述，无任何外部副作用。"""

    payload: dict[str, Any]
    delivery_units: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TargetSnapshot:
    """任务目标的冻结快照（来自 operation_scope_members + 版本表）。"""

    operation_id: str
    operation_type: str
    scope_row_id: str
    scope_kind: str
    members: tuple[dict[str, Any], ...] = ()
    primary: dict[str, Any] | None = None


@dataclass(frozen=True)
class OperationResult:
    status: str
    receipt: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class OperationAdapter(Protocol):
    """适配器只负责目标转换与外部能力；不得直接推进 workflow 状态。"""

    operation_type: str

    def capabilities(self) -> dict[str, Any]: ...
    def validate(self, snapshot: TargetSnapshot, config: dict) -> None: ...
    def prepare(self, snapshot: TargetSnapshot,
                config: dict) -> PreparedOperation: ...
    def execute(self, prepared: PreparedOperation,
                fencing_token: int) -> dict[str, Any]: ...
    def verify(self, receipt: dict[str, Any]) -> OperationResult: ...


class OperationRunner:
    """统一执行入口：状态、attempt、fencing、幂等由 runner 负责。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- workflow_step 映射（方案 3.0：一对一，scope=workflow） ----

    def ensure_step(self, operation_id: str, workflow_id: str,
                    *, now: str | None = None) -> str:
        step_id = f"step:{operation_id}"
        created = now or _now()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO workflow_steps
                (step_id, workflow_id, scope, item_id, step_key, step_type,
                 step_definition_version, dependency_keys_json, status,
                 state_version)
            VALUES (?, ?, 'workflow', NULL, ?, ?, ?, '[]', 'PENDING', 0)
            """,
            (step_id, workflow_id, f"operation:{operation_id}",
             STEP_TYPE_OPERATION_TASK, STEP_DEFINITION_VERSION),
        )
        self.conn.execute(
            """
            UPDATE operation_tasks SET workflow_step_id = ?
            WHERE operation_id = ?
            """,
            (step_id, operation_id),
        )
        return step_id

    # ---- 执行 ----

    def run(
        self,
        *,
        operation_id: str,
        adapter: OperationAdapter,
        workflow_id: str,
        config: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> OperationResult:
        created = now or _now()
        task = self.conn.execute(
            """SELECT operation_type, scope_row_id FROM operation_tasks
               WHERE operation_id = ?""",
            (operation_id,),
        ).fetchone()
        if task is None:
            raise ValueError(f"操作任务不存在: {operation_id}")
        operation_type, scope_row_id = task[0], task[1]
        if operation_type != adapter.operation_type:
            raise ValueError(
                f"适配器类型不匹配: 任务 {operation_type} vs 适配器 "
                f"{adapter.operation_type}"
            )

        step_id = self.ensure_step(operation_id, workflow_id, now=created)
        step_status = self.conn.execute(
            "SELECT status FROM workflow_steps WHERE step_id = ?",
            (step_id,),
        ).fetchone()[0]
        if step_status == "SUCCEEDED":
            # 幂等重放：已成功的任务不再产生副作用
            return OperationResult(status=ResultStatus.SUCCEEDED,
                                   receipt={"idempotent_replay": True})
        if step_status == "AMBIGUOUS":
            # 外部副作用歧义：必须人工对账，不允许自动重提
            return OperationResult(
                status=ResultStatus.AMBIGUOUS,
                error_code="AMBIGUOUS_BLOCKED",
                details={"step_id": step_id},
            )

        snapshot = self._load_snapshot(operation_id, operation_type,
                                       scope_row_id)
        config = config or {}
        adapter.capabilities()
        adapter.validate(snapshot, config)
        prepared = adapter.prepare(snapshot, config)

        attempt_id, fencing_token = self._open_attempt(
            operation_id, step_id, workflow_id, created)
        self._set_step_status(step_id, "RUNNING")

        bind = getattr(adapter, "bind", None)
        if callable(bind):
            bind(workflow_id=workflow_id, attempt_id=attempt_id)

        try:
            receipt = adapter.execute(prepared, fencing_token)
        except Exception as exc:
            result = OperationResult(
                status=ResultStatus.PERMANENT_FAILED,
                error_code=type(exc).__name__,
                details={"message": str(exc)},
            )
            self._finalize(step_id, attempt_id, result, created)
            return result

        result = adapter.verify(receipt)
        self._record_receipt(operation_id, attempt_id, receipt, result)
        self._link_external_operation(receipt, step_id, attempt_id)
        self._finalize(step_id, attempt_id, result, created)
        return result

    # ---- 内部 ----

    def _load_snapshot(self, operation_id: str, operation_type: str,
                       scope_row_id: str) -> TargetSnapshot:
        scope = self.conn.execute(
            """SELECT scope_kind FROM operation_scopes
               WHERE scope_row_id = ?""",
            (scope_row_id,),
        ).fetchone()
        members = self.conn.execute(
            """SELECT target_kind, target_id, target_revision_id, ordinal
               FROM operation_scope_members WHERE scope_row_id = ?
               ORDER BY ordinal""",
            (scope_row_id,),
        ).fetchall()
        member_dicts = [
            {"target_kind": m[0], "target_id": m[1],
             "target_revision_id": m[2], "ordinal": m[3]}
            for m in members
        ]
        primary_row = self.conn.execute(
            """SELECT target_kind, target_id, target_revision_id
               FROM operation_task_targets WHERE operation_id = ?
               AND role = 'primary'""",
            (operation_id,),
        ).fetchone()
        primary = None
        if primary_row:
            primary = {"target_kind": primary_row[0],
                       "target_id": primary_row[1],
                       "target_revision_id": primary_row[2]}
        return TargetSnapshot(
            operation_id=operation_id,
            operation_type=operation_type,
            scope_row_id=scope_row_id,
            scope_kind=scope[0] if scope else "DOCUMENT",
            members=tuple(member_dicts),
            primary=primary,
        )

    def _open_attempt(self, operation_id: str, step_id: str,
                      workflow_id: str, created: str) -> tuple[str, int]:
        attempt_seq = self.conn.execute(
            """SELECT COALESCE(MAX(attempt_seq), 0) + 1 FROM step_attempts
               WHERE workflow_id = ? AND step_id = ?""",
            (workflow_id, step_id),
        ).fetchone()[0]
        attempt_id = f"attempt:{operation_id}:{attempt_seq}"
        self.conn.execute(
            """
            INSERT INTO step_attempts
                (attempt_id, workflow_id, step_id, attempt_kind, attempt_seq,
                 execute_attempt_no, status, result_status,
                 lease_fencing_token, state_version, started_at)
            VALUES (?, ?, ?, 'EXECUTE', ?, ?, 'RUNNING', 'IN_PROGRESS',
                    ?, 0, ?)
            """,
            (attempt_id, workflow_id, step_id, attempt_seq, attempt_seq,
             attempt_seq, created),
        )
        self.conn.execute(
            "UPDATE workflow_steps SET current_attempt_id = ?, "
            "attempt_count = attempt_count + 1, "
            "state_version = state_version + 1, "
            "started_at = COALESCE(started_at, ?) WHERE step_id = ?",
            (attempt_id, created, step_id),
        )
        return attempt_id, attempt_seq

    def _set_step_status(self, step_id: str, status: str,
                         now: str | None = None,
                         *, finished: bool = False) -> None:
        timestamp = now or _now()
        if finished:
            self.conn.execute(
                "UPDATE workflow_steps SET status = ?, "
                "state_version = state_version + 1, finished_at = ? "
                "WHERE step_id = ?",
                (status, timestamp, step_id),
            )
        else:
            self.conn.execute(
                "UPDATE workflow_steps SET status = ?, "
                "state_version = state_version + 1, "
                "started_at = COALESCE(started_at, ?) WHERE step_id = ?",
                (status, timestamp, step_id),
            )

    def _link_external_operation(self, receipt: dict, step_id: str,
                                 attempt_id: str) -> None:
        """D-EXT-001：外部操作回填统一执行链关联（历史行允许为空）。"""
        external_operation_id = receipt.get("external_operation_id") \
            if isinstance(receipt, dict) else None
        if not external_operation_id:
            return
        self.conn.execute(
            """UPDATE external_operations
               SET workflow_step_id = ?, attempt_id = ?
               WHERE external_operation_id = ?""",
            (step_id, attempt_id, external_operation_id),
        )

    def _record_receipt(self, operation_id: str, attempt_id: str,
                        receipt: dict, result: OperationResult) -> None:
        """回执与结果摘要保存到 attempt（外部回执仍在各自事实表）。"""
        details = {
            "receipt": receipt,
            "result": result.status,
            "error_code": result.error_code,
        }
        self.conn.execute(
            """UPDATE step_attempts SET error_details_json = ?
               WHERE attempt_id = ?""",
            (json.dumps(details, ensure_ascii=False), attempt_id),
        )

    def _finalize(self, step_id: str, attempt_id: str,
                  result: OperationResult, created: str) -> None:
        step_status = STEP_STATUS_BY_RESULT[result.status]
        attempt_status = ATTEMPT_STATUS_BY_RESULT[result.status]
        attempt_result = ATTEMPT_RESULT_BY_RESULT[result.status]
        finished = created
        self.conn.execute(
            """UPDATE step_attempts SET status = ?, result_status = ?,
               error_code = ?, finished_at = ?, state_version = state_version + 1
               WHERE attempt_id = ?""",
            (attempt_status, attempt_result, result.error_code, finished,
             attempt_id),
        )
        self._set_step_status(step_id, step_status, created, finished=True)
