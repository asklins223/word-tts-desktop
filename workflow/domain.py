"""Small, dependency-free domain values shared by repositories and routes."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


class DomainError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class CommandTarget:
    target_type: str
    step_id: str | None = None
    item_id: str | None = None
    work_unit_id: str | None = None
    work_unit_attempt_id: str | None = None
    provider_receipt_id: str | None = None
    external_operation_id: str | None = None

    _FIELDS = {
        "STEP": ("step_id",),
        "ITEM": ("step_id", "item_id"),
        "WORK_UNIT": ("work_unit_id",),
        "WORK_UNIT_ATTEMPT": ("work_unit_attempt_id",),
        "PROVIDER_RECEIPT": ("provider_receipt_id",),
        "EXTERNAL_OPERATION": ("external_operation_id",),
    }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CommandTarget":
        target_type = str(value.get("target_type") or "")
        fields = cls._FIELDS.get(target_type)
        if not fields:
            raise DomainError("VALIDATION_ERROR", f"unsupported target_type: {target_type}")
        for field in fields:
            if not str(value.get(field) or ""):
                raise DomainError("VALIDATION_ERROR", f"target.{field} is required")
        allowed = {"target_type", *fields}
        extras = set(value) - allowed
        if extras:
            raise DomainError("VALIDATION_ERROR", f"unexpected target fields: {sorted(extras)}")
        return cls(target_type=target_type, **{field: str(value[field]) for field in fields})

    def as_dict(self) -> dict[str, str]:
        fields = self._FIELDS[self.target_type]
        return {"target_type": self.target_type, **{field: getattr(self, field) for field in fields}}


@dataclass(frozen=True)
class WorkflowSnapshot:
    workflow_id: str
    workflow_group_id: str
    group_state_version: int
    parent_workflow_id: str | None
    result_status: str
    execution_state: str
    control_state: str
    cleanup_state: str
    status: str
    state_version: int
    draft_revision: int
    current_step_id: str | None
    source_artifact_id: str | None
    item_count: int
    artifact_count: int
    latest_event_id: str | None
    latest_seq: int
    updated_at: str
    latest_event: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    seq: int
    workflow_id: str
    mutation_id: str
    schema_version: str
    step_id: str | None
    item_id: str | None
    attempt_id: str | None
    correlation_id: str
    causation_id: str | None
    actor_type: str
    actor_id: str | None
    event_type: str
    phase: str | None
    payload: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
