"""内置 OperationAdapter 实现（阶段 4）。

- ``ExternalUpsertAdapter``：包装既有 ``ExternalRecordService`` + 外部
  适配器，把目标快照转成外部 payload；回执仍由 0005 表持有，
  runner 结束后回填 ``external_operations.workflow_step_id``/``attempt_id``
  （D-EXT-001 的统一执行链关联）。
- ``FakeAudioAdapter``：无副作用的音频任务适配器，用于 runner 契约测试
  与灰度前的干跑；真实 TTS 引擎按同一契约在阶段 4 后续接入。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .runner import (
    OperationResult,
    PreparedOperation,
    ResultStatus,
    TargetSnapshot,
)

_PAYLOAD_VERSION = "v1"


class ExternalUpsertAdapter:
    """EXTERNAL_UPSERT 适配器：通过 ExternalRecordService 提交，不走旁路。"""

    operation_type = "EXTERNAL_UPSERT"

    def __init__(self, service, external_adapter, conn=None, *,
                 account_scope: str | None = None):
        self.service = service
        self.external = external_adapter
        self.conn = conn
        self.account_scope = account_scope or external_adapter.account_scope
        self._workflow_id: str | None = None
        self._attempt_id: str | None = None
        self.last_operation: dict[str, Any] | None = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "scope_kinds": ("QUESTION", "CONTENT_UNIT", "STIMULUS"),
            "partial_success": False,
            "needs_audio_artifact": True,
            "payload_version": _PAYLOAD_VERSION,
        }

    def validate(self, snapshot: TargetSnapshot, config: dict) -> None:
        if snapshot.primary is None:
            raise ValueError("外部录入任务缺少主目标")
        if len(snapshot.members) == 0:
            raise ValueError("外部录入任务目标范围不能为空")

    def prepare(self, snapshot: TargetSnapshot,
                config: dict) -> PreparedOperation:
        primary = snapshot.primary
        payload = {
            "business_record_key": primary["target_id"],
            "target_kind": primary["target_kind"],
            "target_revision_id": primary["target_revision_id"],
            "scope_kind": snapshot.scope_kind,
            "members": [
                {"target_kind": m["target_kind"], "target_id": m["target_id"],
                 "target_revision_id": m["target_revision_id"],
                 "ordinal": m["ordinal"]}
                for m in snapshot.members
            ],
            "payload_version": _PAYLOAD_VERSION,
        }
        delivery_units = ({"unit_id": f"delivery:{snapshot.operation_id}:1",
                           "member_count": len(payload["members"])},)
        return PreparedOperation(payload=payload, delivery_units=delivery_units)

    def execute(self, prepared: PreparedOperation,
                fencing_token: int) -> dict[str, Any]:
        payload = prepared.payload
        workflow_id = self._workflow_id
        if workflow_id is None:
            raise RuntimeError("execute 前必须调用 bind(workflow_id=..., attempt_id=...)")
        mapping = self.service.ensure_record(
            workflow_id,
            external_system=self.external.system,
            account_scope=self.account_scope,
            business_record_key=payload["business_record_key"],
            mapping_version=_PAYLOAD_VERSION,
        )
        mapping_id = mapping["external_record_mapping_id"]
        lease = self.service.acquire_record_lease(
            mapping_id, f"runner-fence-{fencing_token}")
        operation = self.service.prepare_operation(
            workflow_id,
            mapping_id=mapping_id,
            operation_key=f"upsert:{payload['business_record_key']}",
            payload=payload,
            mapping_version=_PAYLOAD_VERSION,
        )
        operation_id = operation["external_operation_id"]
        self.service.begin_operation(operation_id, lease)
        # 提交成员快照先行落库（PENDING），提交后按回执收敛状态
        self._snapshot_targets(operation_id, payload, "PENDING")
        try:
            observed = self.service.submit_operation(
                operation_id, lease, self.external, payload)
            receipt = observed["receipt"]
        except Exception as exc:
            # 提交结果未知（如回执丢失）：不能断言成功或失败
            receipt = {"side_effect_state": "AMBIGUOUS", "error": str(exc)}
            side_effect = "AMBIGUOUS"
        else:
            side_effect = receipt.get("side_effect_state", "CONFIRMED")
        member_status = {
            "CONFIRMED": "SUCCEEDED",
            "AMBIGUOUS": "AMBIGUOUS",
        }.get(side_effect, "PENDING")
        self._snapshot_targets(operation_id, payload, member_status)
        self.last_operation = {
            "external_operation_id": operation_id,
            "attempt_id": self._attempt_id,
        }
        return {
            "external_operation_id": operation_id,
            "external_record_mapping_id": mapping_id,
            "receipt": receipt,
            "workflow_id": workflow_id,
            "fencing_token": fencing_token,
        }

    def _snapshot_targets(self, external_operation_id: str, payload: dict,
                          result_status: str) -> None:
        """把提交成员快照写入 external_operation_targets（v0007）。"""
        if self.conn is None:
            return
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        for ordinal, member in enumerate(payload["members"]):
            fragment = json.dumps(member, ensure_ascii=False,
                                  sort_keys=True, separators=(",", ":"))
            self.conn.execute(
                """
                INSERT OR IGNORE INTO external_operation_targets
                    (operation_target_id, external_operation_id, target_kind,
                     target_id, target_revision_id, ordinal, result_status,
                     payload_fragment_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"ot:{external_operation_id}:{ordinal}",
                 external_operation_id, member["target_kind"],
                 member["target_id"], member["target_revision_id"], ordinal,
                 result_status,
                 hashlib.sha256(fragment.encode("utf-8")).hexdigest(), now),
            )

    def bind(self, *, workflow_id: str, attempt_id: str) -> None:
        """runner 在 execute 前绑定执行上下文。"""
        self._workflow_id = workflow_id
        self._attempt_id = attempt_id

    def verify(self, receipt: dict[str, Any]) -> OperationResult:
        side_effect = receipt.get("receipt", {}).get("side_effect_state")
        if side_effect == "AMBIGUOUS":
            return OperationResult(status=ResultStatus.AMBIGUOUS,
                                   receipt=receipt,
                                   error_code="EXTERNAL_AMBIGUOUS")
        return OperationResult(status=ResultStatus.SUCCEEDED, receipt=receipt)


class FakeAudioAdapter:
    """AUDIO_GENERATE 的无副作用适配器（契约测试与干跑用）。"""

    operation_type = "AUDIO_GENERATE"

    def __init__(self, *, fail_modes: int = 0):
        self.fail_modes = fail_modes
        self.execute_calls = 0

    def capabilities(self) -> dict[str, Any]:
        return {"scope_kinds": ("QUESTION", "STIMULUS", "CONTENT_UNIT",
                                "GROUP", "MAJOR_SECTION", "DOCUMENT"),
                "partial_success": True, "needs_audio_artifact": False}

    def validate(self, snapshot: TargetSnapshot, config: dict) -> None:
        if snapshot.primary is None:
            raise ValueError("音频任务缺少主目标")

    def prepare(self, snapshot: TargetSnapshot,
                config: dict) -> PreparedOperation:
        return PreparedOperation(
            payload={"target": snapshot.primary, "members": len(snapshot.members)},
            delivery_units=({"unit_id": f"delivery:{snapshot.operation_id}:1"},),
        )

    def execute(self, prepared: PreparedOperation,
                fencing_token: int) -> dict[str, Any]:
        self.execute_calls += 1
        if self.execute_calls <= self.fail_modes:
            raise RuntimeError("simulated provider outage")
        return {
            "artifact_id": f"artifact:{fencing_token}",
            "delivery_units": [dict(u) for u in prepared.delivery_units],
        }

    def verify(self, receipt: dict[str, Any]) -> OperationResult:
        return OperationResult(status=ResultStatus.SUCCEEDED, receipt=receipt)
