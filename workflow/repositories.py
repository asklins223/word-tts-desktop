"""Transactional repositories for workflow state and side-effect facts."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from collections.abc import Callable
from typing import Any, Mapping

from audio_naming import ARCHIVE_LAYOUT_VERSION
from .database import WorkflowDatabase
from .data_safety import DataSafetyError, redact_public_json, validate_public_object
from .domain import CommandTarget, WorkflowSnapshot, canonical_json, content_hash, new_id, utc_now
from .event_store import EventStore
from .retry_policy import RetryPolicy
from .side_effect_log import SideEffectIntentLog
from .state_machine import (
    InvalidTransition,
    command_transition,
    require_expected as require_state_version,
    transition_step,
    validate_target,
)


class RepositoryError(RuntimeError):
    code = "PERSISTENCE_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        safe_details = redact_public_json(details or {})
        self.details = dict(safe_details) if isinstance(safe_details, Mapping) else {}


class NotFoundError(RepositoryError):
    code = "NOT_FOUND"


class ConflictError(RepositoryError):
    code = "STATE_CONFLICT"


class IdempotencyConflict(ConflictError):
    code = "IDEMPOTENCY_CONFLICT"


class IdempotencyInProgress(ConflictError):
    code = "IDEMPOTENCY_IN_PROGRESS"


class LeaseConflict(ConflictError):
    code = "STALE_ATTEMPT"


class BudgetExhausted(ConflictError):
    code = "RESOURCE_EXHAUSTED"


IdempotencyRecovery = Callable[
    [Mapping[str, Any]],
    tuple[int, Mapping[str, Any]] | None,
]


def _safe_source_filename(value: Any, fallback: str = "未命名文档.docx") -> str:
    raw = str(value or "").replace("\\", "/")
    name = PurePath(raw).name
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127).strip()
    if len(name) <= 256:
        return name or fallback
    suffix = PurePath(name).suffix.lower()
    if suffix in {".docx", ".xlsx"}:
        max_stem_length = max(1, 256 - len(suffix))
        return f"{PurePath(name).stem[:max_stem_length]}{suffix}"
    return name[:256] or fallback


_CONFIGURATION_REVISION_KEY = "_workflow_configuration_revision"
_SKIP_REASON_KEY = "_workflow_skip_reason"


def _configuration_public(
    value: Mapping[str, Any] | None,
    *,
    reject_sensitive: bool = False,
) -> dict[str, Any]:
    """Return a safe configuration without the server-owned revision marker.

    New user input is validated in reject mode.  Existing rows are read in
    redaction mode so a database created by an older build cannot leak a
    credential through a workspace or rerun response.
    """

    try:
        result = (
            validate_public_object(value)
            if reject_sensitive
            else redact_public_json(value)
        )
    except DataSafetyError as exc:
        raise RepositoryError(
            "workflow configuration contains unsupported or credential-like data",
            code="VALIDATION_ERROR",
        ) from exc
    if not isinstance(result, dict):
        result = {}
    result.pop(_CONFIGURATION_REVISION_KEY, None)
    return result


def _configuration_revision(value: Mapping[str, Any] | None, *, draft_revision: int = 0) -> int:
    """Read the durable saved-configuration revision.

    The 2A schema is intentionally shared with the frozen migration profile,
    so the revision is carried as a reserved server-owned JSON member rather
    than a new migration column.  It is never returned by get_configuration
    or any public workspace projection.  The fallback keeps databases created
    by older builds deterministic while remaining distinct from draft_revision.
    """

    raw = (value or {}).get(_CONFIGURATION_REVISION_KEY)
    try:
        revision = int(raw)
    except (TypeError, ValueError):
        revision = 0
    return revision if revision >= 1 else max(1, int(draft_revision or 0) + 1)


def _configuration_stored(value: Mapping[str, Any] | None, *, revision: int) -> dict[str, Any]:
    result = _configuration_public(value)
    result[_CONFIGURATION_REVISION_KEY] = max(1, int(revision))
    return result


def require_expected(actual: int, expected: int) -> None:
    """Expose optimistic-lock conflicts as repository conflicts."""

    try:
        require_state_version(actual, expected)
    except InvalidTransition as exc:
        raise ConflictError(str(exc)) from exc


def _expiry(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_expired(value: Any) -> bool:
    try:
        expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _table_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(con, table_name):
        return set()
    return {str(row["name"]) for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _idempotency_request_fingerprint(
    *,
    command_name: str,
    method: str,
    resource_id: str | None,
    target_json: str,
    request: Mapping[str, Any],
) -> str:
    """Hash every request dimension that can change the side effect.

    Older databases stored only the request body hash.  New reservations
    include the command metadata and typed target as well; replay validation
    below still accepts the legacy hash when all of those persisted metadata
    fields match, so upgrading does not invalidate safe retries.
    """

    return content_hash(
        {
            "command_name": command_name,
            "method": method,
            "resource_id": resource_id,
            "target_json": target_json,
            "request": request,
        }
    )


def _snapshot_from_connection(con: sqlite3.Connection, workflow_id: str) -> WorkflowSnapshot:
    row = con.execute(
        "SELECT * FROM workflows WHERE workflow_id=?",
        (workflow_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"workflow does not exist: {workflow_id}")
    counts = con.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM work_items WHERE workflow_id=?) AS item_count, "
        "(SELECT COUNT(*) FROM artifacts WHERE workflow_id=? AND lifecycle_state <> 'DELETED') AS artifact_count",
        (workflow_id, workflow_id),
    ).fetchone()
    group = con.execute(
        "SELECT state_version FROM workflow_groups WHERE workflow_group_id=?",
        (row["workflow_group_id"],),
    ).fetchone()
    if group is None:
        raise RepositoryError("workflow group does not exist", code="PERSISTENCE_ERROR")
    stream_row = con.execute(
        "SELECT latest_seq FROM workflow_event_streams WHERE workflow_id=?",
        (workflow_id,),
    ).fetchone()
    latest_seq = int(stream_row["latest_seq"]) if stream_row is not None else 0
    event_row = con.execute(
        "SELECT * FROM workflow_events WHERE workflow_id=? ORDER BY seq DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    latest_event = None
    if event_row is not None:
        from .event_store import _event_from_row

        latest_event = _event_from_row(event_row).as_dict()
    latest_event_id = event_row["event_id"] if event_row is not None else None
    if latest_event_id is None and latest_seq > 0:
        # When the latest event row has been compacted, the stream sequence is
        # still authoritative and the snapshot anchor is the only safe cursor
        # that can be handed to an SSE reconnect.
        snapshot_row = con.execute(
            "SELECT snapshot_event_id FROM workflow_snapshots "
            "WHERE workflow_id=? AND snapshot_seq=?",
            (workflow_id, latest_seq),
        ).fetchone()
        latest_event_id = snapshot_row["snapshot_event_id"] if snapshot_row else None
    return WorkflowSnapshot(
        workflow_id=str(row["workflow_id"]),
        workflow_group_id=str(row["workflow_group_id"]),
        group_state_version=int(group["state_version"]),
        parent_workflow_id=row["parent_workflow_id"],
        result_status=str(row["result_status"]),
        execution_state=str(row["execution_state"]),
        control_state=str(row["control_state"]),
        cleanup_state=str(row["cleanup_state"]),
        status=str(row["status"]),
        state_version=int(row["state_version"]),
        draft_revision=int(row["draft_revision"]),
        current_step_id=row["current_step_id"],
        source_artifact_id=row["source_artifact_id"],
        item_count=int(counts["item_count"]),
        artifact_count=int(counts["artifact_count"]),
        latest_event_id=latest_event_id,
        latest_seq=latest_seq,
        last_error_code=(str(row["last_error_code"])[:128] if row["last_error_code"] is not None else None),
        last_error_message=(str(row["last_error_message"])[:2000] if row["last_error_message"] is not None else None),
        updated_at=str(row["updated_at"]),
        latest_event=latest_event,
    )


class WorkflowRepository:
    def __init__(
        self,
        database: WorkflowDatabase,
        *,
        event_store: EventStore | None = None,
        intent_log: SideEffectIntentLog | None = None,
    ) -> None:
        self.database = database
        self.events = event_store or EventStore(database)
        self.intent_log = intent_log or SideEffectIntentLog(database.path.parent / "side_effect_intents.jsonl")
        # A pending reservation is still live while this repository instance
        # is executing its route.  A different process cannot observe this
        # in-memory set, so an unexpired pending row must remain protected by
        # its durable TTL until a committed outcome can be recovered.
        self._idempotency_activity_lock = threading.RLock()
        self._active_idempotency_ids: set[str] = set()

    def initialize(self) -> None:
        self.database.initialize()

    def get_workflow(self, workflow_id: str) -> WorkflowSnapshot:
        with self.database.read_transaction() as con:
            return _snapshot_from_connection(con, workflow_id)

    def get_workflow_type(self, workflow_id: str) -> str:
        """Return the durable workflow kind for projections outside a snapshot."""

        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT workflow_type FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            return str(row["workflow_type"] or "")

    def get_event_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        """Return the latest durable workflow event for one API request."""

        from .event_store import _event_from_row

        with self.database.read_transaction() as con:
            row = con.execute(
                """SELECT * FROM workflow_events
                   WHERE request_id=? ORDER BY seq DESC LIMIT 1""",
                (request_id,),
            ).fetchone()
            return _event_from_row(row).as_dict() if row is not None else None

    def get_workspace(
        self,
        workflow_id: str,
        *,
        capabilities: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the server-owned workspace projection from one DB snapshot."""

        from .workspace import build_workflow_workspace

        return build_workflow_workspace(self, workflow_id, capabilities=capabilities)

    def list_active_workflows(
        self,
        *,
        limit: int = 100,
        recoverable_only: bool = False,
        _include_page: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return active workflow facts, optionally limited to recovery candidates.

        This endpoint is intentionally a read-only index.  It never claims a
        lease or calls a provider; a caller must still use ``mark_takeover``
        inside the same database boundary before scheduling a worker.  A
        parsed document also has ``PREPARING`` as its execution state, so the
        recovery scheduler must opt into ``recoverable_only``; otherwise a
        configuration-page draft could be started before the user clicks
        Generate.
        """

        limit = min(max(1, int(limit)), 200)
        candidates: list[dict[str, Any]] = []
        with self.database.read_transaction() as con:
            generation_evidence = """(
                EXISTS (
                    SELECT 1 FROM workflow_events e
                    WHERE e.workflow_id=w.workflow_id
                      AND e.event_type='WORKFLOW_GENERATE'
                )
                OR EXISTS (
                    SELECT 1 FROM workflow_steps generation_step
                    WHERE generation_step.workflow_id=w.workflow_id
                      AND generation_step.step_type='TTS'
                )
            )"""
            recovery_filter = f"AND {generation_evidence}" if recoverable_only else ""
            rows = con.execute(
                f"""SELECT w.workflow_id, {generation_evidence} AS generation_accepted
                    FROM workflows w
                    WHERE w.status <> 'CLOSED' AND w.result_status='IN_PROGRESS'
                      AND w.execution_state <> 'TERMINAL'
                      {recovery_filter}
                    ORDER BY w.updated_at DESC, w.workflow_id DESC LIMIT ?""",
                (limit + 1,),
            ).fetchall()
            for row in rows:
                workflow_id = str(row["workflow_id"])
                snapshot = _snapshot_from_connection(con, workflow_id)
                generation_accepted = bool(row["generation_accepted"])
                is_tts_workflow = str(con.execute(
                    "SELECT workflow_type FROM workflows WHERE workflow_id=?", (workflow_id,)
                ).fetchone()[0] or "").lower() == "tts"
                unresolved = False if is_tts_workflow else con.execute(
                    """SELECT 1 FROM work_units
                       WHERE workflow_id=?
                         AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                         AND status <> 'SUCCEEDED'
                       UNION ALL
                       SELECT 1 FROM provider_submissions p
                       JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                       WHERE u.workflow_id=?
                         AND p.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                         AND u.status <> 'SUCCEEDED'
                       LIMIT 1""",
                    (workflow_id, workflow_id),
                ).fetchone() is not None
                takeover_allowed = (
                    generation_accepted
                    and snapshot.control_state == "RUNNING"
                    and snapshot.execution_state in {"PREPARING", "RUNNING", "RECOVERING"}
                    and not unresolved
                )
                if snapshot.control_state in {"PAUSED", "PAUSE_REQUESTED"}:
                    resume_reason = "任务已暂停，可恢复执行"
                elif not generation_accepted:
                    resume_reason = "等待用户在配置页确认生成"
                elif takeover_allowed:
                    resume_reason = "发现未完成且无未决外部副作用的运行，可安全接管"
                elif unresolved:
                    resume_reason = "存在未决外部操作，需要人工处理"
                else:
                    resume_reason = "当前状态不允许自动接管，请查看工作区动作"
                candidates.append({
                    "workflow": snapshot.as_dict(),
                    "can_resume": generation_accepted and snapshot.control_state in {"PAUSED", "PAUSE_REQUESTED"} and not unresolved,
                    "can_takeover": takeover_allowed,
                    "generation_accepted": generation_accepted,
                    "resume_reason": resume_reason,
                    "requires_reconcile": unresolved,
                })
        truncated = len(candidates) > limit
        visible = candidates[:limit]
        if _include_page:
            return {"workflows": visible, "limit": limit, "truncated": truncated}
        return visible

    def mark_takeover(self, workflow_id: str) -> WorkflowSnapshot | None:
        """Fence a safe restart takeover before a new worker is scheduled."""

        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            if snapshot.execution_state == "TERMINAL" or snapshot.control_state != "RUNNING":
                return None
            if snapshot.execution_state not in {"PREPARING", "RUNNING", "RECOVERING"}:
                return None
            is_tts_workflow = str(con.execute(
                "SELECT workflow_type FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()[0] or "").lower() == "tts"
            unresolved = None if is_tts_workflow else con.execute(
                """SELECT 1 FROM work_units
                   WHERE workflow_id=?
                     AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                     AND status <> 'SUCCEEDED'
                   UNION ALL
                   SELECT 1 FROM provider_submissions p
                   JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                   WHERE u.workflow_id=?
                     AND p.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                     AND u.status <> 'SUCCEEDED'
                   LIMIT 1""",
                (workflow_id, workflow_id),
            ).fetchone()
            if unresolved is not None:
                return None
            if snapshot.execution_state == "RECOVERING":
                return snapshot
            now = utc_now()
            updated = con.execute(
                """UPDATE workflows SET execution_state='RECOVERING',
                       state_version=state_version+1, updated_at=?
                   WHERE workflow_id=? AND state_version=? AND control_state='RUNNING'
                     AND execution_state IN ('PREPARING','RUNNING')""",
                (now, workflow_id, snapshot.state_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("workflow changed while taking over")
            self.events.append_in_transaction(
                con,
                workflow_id,
                "WORKFLOW_TAKEOVER_SCHEDULED",
                {"previous_execution_state": snapshot.execution_state},
                actor_type="RECOVERY",
                actor_id="workflow-startup",
            )
            taken_over = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, taken_over.as_dict())
            return taken_over

    def acknowledge_pause(self, workflow_id: str) -> WorkflowSnapshot:
        """Ack a cooperative pause only after the worker reaches a safe point."""

        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            if snapshot.control_state != "PAUSE_REQUESTED":
                return snapshot
            now = utc_now()
            updated = con.execute(
                """UPDATE workflows SET control_state='PAUSED', state_version=state_version+1,
                       updated_at=? WHERE workflow_id=? AND state_version=?
                     AND control_state='PAUSE_REQUESTED'""",
                (now, workflow_id, snapshot.state_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("workflow changed while acknowledging pause")
            self.events.append_in_transaction(
                con,
                workflow_id,
                "WORKFLOW_PAUSED",
                {"execution_state": snapshot.execution_state},
                actor_type="WORKER",
                actor_id="generation-task",
            )
            paused = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, paused.as_dict())
            return paused

    def complete_skipped_workflow(
        self,
        workflow_id: str,
        *,
        request_id: str | None = None,
    ) -> WorkflowSnapshot:
        """Close a generation whose complete input set was explicitly skipped.

        Skipping every parsed item is a valid, billable-free completion. It
        must not leave the workflow in ``RUNNING`` merely because there was no
        provider submission to call; otherwise it can neither be archived nor
        represented as a settled delivery decision after a restart.
        """

        now = utc_now()
        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            if snapshot.execution_state == "TERMINAL":
                return snapshot
            if snapshot.control_state not in {"RUNNING", "PAUSE_REQUESTED"}:
                # A cancel/paused control fence must never be silently
                # replaced by a successful all-skipped completion.  A
                # PAUSE_REQUESTED fence is different: no provider work is
                # pending on this local path, so closing the already-settled
                # workflow is safe and avoids a spurious 409 race.
                raise ConflictError(
                    f"workflow control state is {snapshot.control_state}",
                    code="CONTROL_STATE_CONFLICT",
                )
            rows = con.execute(
                "SELECT item_id, status FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id",
                (workflow_id,),
            ).fetchall()
            if not rows:
                raise RepositoryError("cannot complete a workflow without work items", code="DEPENDENCY_NOT_READY")
            if any(str(row["status"]) != "SKIPPED" for row in rows):
                raise ConflictError("workflow still has eligible work items", code="STATE_CONFLICT")
            unresolved = con.execute(
                """SELECT 1 FROM work_units
                   WHERE workflow_id=?
                     AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                     AND status <> 'SUCCEEDED'
                   UNION ALL
                   SELECT 1 FROM provider_submissions p
                   JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                   WHERE u.workflow_id=?
                     AND p.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                     AND u.status <> 'SUCCEEDED'
                   LIMIT 1""",
                (workflow_id, workflow_id),
            ).fetchone()
            if unresolved is not None:
                raise ConflictError(
                    "workflow has an unresolved provider side effect",
                    code="RECONCILIATION_REQUIRED",
                )

            # A TTS step is only created when a provider plan is prepared. If
            # an all-skipped run never needed one, leave the successful parse
            # step untouched; if a legacy path did create it, settle it
            # explicitly so the step projection agrees with the workflow.
            con.execute(
                """UPDATE workflow_steps
                   SET status='SUCCEEDED', output_reference_json=?, finished_at=?,
                       error_code=NULL, error_details_json=NULL, state_version=state_version+1
                   WHERE workflow_id=? AND step_key='tts'
                     AND status NOT IN ('SUCCEEDED','PERMANENT_FAILED','CANCELLED')""",
                (canonical_json({"skipped_item_ids": [str(row["item_id"]) for row in rows]}), now, workflow_id),
            )
            con.execute(
                """UPDATE workflow_groups SET lifecycle_state='ACTIVE',
                       accepted_at=COALESCE(accepted_at, ?), state_version=state_version+1,
                       updated_at=?
                   WHERE workflow_group_id=? AND lifecycle_state='DRAFT'""",
                (now, now, snapshot.workflow_group_id),
            )
            updated = con.execute(
                """UPDATE workflows SET status=CASE WHEN status='DRAFT' THEN 'ACTIVE' ELSE status END,
                       result_status='SUCCEEDED', execution_state='TERMINAL', control_state='TERMINATED',
                       cleanup_state='SUCCEEDED', last_error_code=NULL, last_error_message=NULL,
                       finished_at=COALESCE(finished_at, ?), state_version=state_version+1, updated_at=?
                   WHERE workflow_id=? AND execution_state <> 'TERMINAL'""",
                (now, now, workflow_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("workflow changed while completing skipped items")
            self.events.append_in_transaction(
                con,
                workflow_id,
                "TTS_ALL_ITEMS_SKIPPED",
                {"skipped_item_ids": [str(row["item_id"]) for row in rows]},
                request_id=request_id,
                actor_type="WORKER",
                actor_id="workflow-engine",
            )
            final = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, final.as_dict())
            return final

    def create_workflow(
        self,
        workflow_type: str,
        configuration: Mapping[str, Any],
        *,
        business_key: str | None = None,
        definition_family: str = "default",
        definition_version: str = "1",
        definition_snapshot: Mapping[str, Any] | None = None,
        workflow_id: str | None = None,
        workflow_group_id: str | None = None,
        request_id: str | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> WorkflowSnapshot:
        if not workflow_type.strip():
            raise RepositoryError("workflow_type is required", code="VALIDATION_ERROR")
        workflow_id = workflow_id or new_id("workflow")
        workflow_group_id = workflow_group_id or new_id("group")
        definition_id = new_id("definition")
        now = utc_now()
        definition = dict(definition_snapshot or {"workflow_type": workflow_type, "steps": []})
        definition_json = canonical_json(definition)
        public_configuration = _configuration_public(configuration, reject_sensitive=True)
        configuration_json = canonical_json(_configuration_stored(public_configuration, revision=1))
        definition_hash = content_hash(definition)
        configuration_hash = content_hash(public_configuration)
        step_graph_hash = content_hash(definition.get("steps", []))
        try:
            transaction = self.database.transaction() if _connection is None else nullcontext(_connection)
            with transaction as con:
                existing_definition = con.execute(
                    """SELECT * FROM workflow_definitions
                       WHERE workflow_type=? AND definition_family=? AND version=?""",
                    (workflow_type, definition_family, definition_version),
                ).fetchone()
                if existing_definition is not None:
                    if (
                        str(existing_definition["definition_hash"]) != definition_hash
                        or str(existing_definition["definition_json"]) != definition_json
                    ):
                        raise ConflictError("definition version is already bound to another snapshot")
                    definition_id = str(existing_definition["workflow_definition_id"])
                else:
                    con.execute(
                        """INSERT INTO workflow_definitions(
                            workflow_definition_id, workflow_type, definition_family,
                            version, definition_hash, definition_json, published_at, created_at
                        ) VALUES (?,?,?,?,?,?,?,?)""",
                        (definition_id, workflow_type, definition_family, definition_version,
                         definition_hash, definition_json, now, now),
                    )
                con.execute(
                    """INSERT INTO workflow_groups(
                        workflow_group_id, workflow_type, definition_family,
                        workflow_definition_id, business_key, lifecycle_state,
                        root_workflow_id, state_version, policy_version,
                        retention_policy_version, accepted_at, created_at, updated_at,
                        abandoned_at, closed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (workflow_group_id, workflow_type, definition_family, definition_id,
                     business_key, "DRAFT", None, 0, "1", "1", None, now, now, None, None),
                )
                con.execute(
                    """INSERT INTO workflows(
                        workflow_id, workflow_group_id, parent_workflow_id,
                        workflow_type, workflow_definition_id, schema_version,
                        workflow_definition_version, step_graph_hash,
                        workflow_business_key, source_id, source_fingerprint,
                        source_artifact_id, configuration_version, configuration_hash,
                        configuration_snapshot, result_status, execution_state,
                        control_state, cleanup_state, status, current_step_id,
                        state_version, draft_revision, draft_expires_at,
                        last_error_code, last_error_message, created_at, updated_at,
                        accepted_at, finished_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (workflow_id, workflow_group_id, None, workflow_type, definition_id,
                     "1", definition_version, step_graph_hash, business_key, None, None,
                     None, definition_version, configuration_hash, configuration_json,
                     "IN_PROGRESS", "CREATED", "RUNNING", "NONE", "DRAFT", None,
                     0, 0, None, None, None, now, now, None, None),
                )
                con.execute(
                    "UPDATE workflow_groups SET root_workflow_id=? WHERE workflow_group_id=?",
                    (workflow_id, workflow_group_id),
                )
                con.execute(
                    "INSERT INTO workflow_event_streams(workflow_id, updated_at) VALUES (?,?)",
                    (workflow_id, now),
                )
                self.events.append_in_transaction(
                    con,
                    workflow_id,
                    "WORKFLOW_CREATED",
                    {"workflow_group_id": workflow_group_id, "workflow_type": workflow_type},
                    request_id=request_id,
                    actor_type="USER",
                    actor_id="desktop",
                )
                snapshot = _snapshot_from_connection(con, workflow_id)
                self.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
                return snapshot
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"workflow cannot be created: {exc}") from exc

    def create_workflow_idempotent(
        self,
        workflow_type: str,
        configuration: Mapping[str, Any],
        *,
        business_key: str | None = None,
        client_key: str,
        request: Mapping[str, Any],
        request_id: str,
        ttl_seconds: int = 86400,
    ) -> tuple[dict[str, Any], bool]:
        """Create and complete the public workflow mutation atomically.

        The ordinary route helpers reserve a key and complete it in separate
        transactions because most commands operate on an existing workflow.
        Creation has no pre-existing resource to bind, so keeping the
        reservation, workflow rows, event, and replay response in one write
        transaction is the only safe way to close the crash window.

        The boolean is ``True`` for a replayed response and ``False`` for a
        newly-created workflow.
        """

        scope = "workflow:create"
        scope_hash = content_hash(scope)
        target_json = "{}"
        request_hash = _idempotency_request_fingerprint(
            command_name="createWorkflow",
            method="POST",
            resource_id=None,
            target_json=target_json,
            request=request,
        )
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT * FROM workflow_idempotency_keys WHERE scope_hash=? AND client_key=?",
                (scope_hash, client_key),
            ).fetchone()
            if row is not None and _is_expired(row["expires_at"]):
                con.execute(
                    "DELETE FROM workflow_idempotency_keys WHERE idempotency_id=?",
                    (row["idempotency_id"],),
                )
                row = None
            if row is not None:
                metadata_matches = (
                    str(row["command_name"]) == "createWorkflow"
                    and str(row["method"]) == "POST"
                    and row["resource_id"] is None
                    and str(row["target_json"] or "{}") == target_json
                )
                hash_matches = row["request_hash"] in {request_hash, content_hash(request)}
                if not metadata_matches or not hash_matches:
                    raise IdempotencyConflict("same idempotency key was used for a different request")
                if row["response_json"]:
                    return json.loads(row["response_json"]), True
                # A reservation made by this atomic path always has a bound
                # workflow.  Recovering it also makes old partially-completed
                # rows safe to replay when a previous build had already
                # written the binding before losing its response transaction.
                if row["workflow_id"]:
                    snapshot = _snapshot_from_connection(con, str(row["workflow_id"]))
                    event_request_id = (
                        snapshot.latest_event.get("request_id")
                        if isinstance(snapshot.latest_event, Mapping)
                        else None
                    )
                    response = {
                        "request_id": str(event_request_id or request_id),
                        "workflow": snapshot.as_dict(),
                    }
                    con.execute(
                        """UPDATE workflow_idempotency_keys
                           SET response_status=201, response_json=?
                           WHERE idempotency_id=? AND response_json IS NULL""",
                        (canonical_json(response), row["idempotency_id"]),
                    )
                    return response, True
                raise IdempotencyInProgress(
                    "the same idempotency request is still in progress; retry after it completes"
                )

            snapshot = self.create_workflow(
                workflow_type,
                configuration,
                business_key=business_key,
                request_id=request_id,
                _connection=con,
            )
            response = {"request_id": request_id, "workflow": snapshot.as_dict()}
            idem_id = new_id("idempotency")
            con.execute(
                """INSERT INTO workflow_idempotency_keys(
                    idempotency_id, scope_hash, client_key, command_name, method,
                    resource_id, target_json, request_hash, workflow_id,
                    response_status, response_json, expires_at, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    idem_id,
                    scope_hash,
                    client_key,
                    "createWorkflow",
                    "POST",
                    None,
                    target_json,
                    request_hash,
                    snapshot.workflow_id,
                    201,
                    canonical_json(response),
                    _expiry(ttl_seconds),
                    utc_now(),
                ),
            )
            return response, False

    def create_rerun(
        self,
        source_workflow_id: str,
        *,
        expected_group_state_version: int,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> WorkflowSnapshot:
        """Create a fresh run in the same active group.

        A rerun never resets the source run's id, sequence or terminal facts.
        It clones only the immutable input graph and references the same
        content-addressed source Blob through a new run-local Artifact row.
        Attempts, WorkUnits, receipts and generated artifacts remain owned by
        the source run and are intentionally not copied.
        """

        new_workflow_id = new_id("workflow")
        now = utc_now()
        with self.database.transaction() as con:
            source = con.execute(
                "SELECT * FROM workflows WHERE workflow_id=?",
                (source_workflow_id,),
            ).fetchone()
            if source is None:
                raise NotFoundError(f"workflow does not exist: {source_workflow_id}")
            group = con.execute(
                "SELECT * FROM workflow_groups WHERE workflow_group_id=?",
                (source["workflow_group_id"],),
            ).fetchone()
            if group is None:
                raise RepositoryError("workflow group is missing", code="PERSISTENCE_ERROR")
            require_expected(int(group["state_version"]), expected_group_state_version)
            if group["lifecycle_state"] != "ACTIVE":
                raise ConflictError(f"workflow group is {group['lifecycle_state'].lower()}")
            if source["execution_state"] != "TERMINAL" or source["result_status"] not in {
                "SUCCEEDED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED",
            }:
                raise ConflictError("only a terminal workflow can be rerun")

            source_artifact = None
            if source["source_artifact_id"] is not None:
                source_artifact = con.execute(
                    """SELECT * FROM artifacts
                       WHERE artifact_id=? AND workflow_id=? AND lifecycle_state='READY'""",
                    (source["source_artifact_id"], source_workflow_id),
                ).fetchone()
                if source_artifact is None:
                    raise RepositoryError("source Artifact is missing from the terminal run", code="PERSISTENCE_ERROR")
                if source_artifact["blob_id"] is None:
                    raise RepositoryError("source Artifact has no immutable Blob", code="PERSISTENCE_ERROR")

            updated_group = con.execute(
                """UPDATE workflow_groups SET state_version=state_version+1, updated_at=?
                   WHERE workflow_group_id=? AND lifecycle_state='ACTIVE' AND state_version=?""",
                (now, source["workflow_group_id"], expected_group_state_version),
            )
            if updated_group.rowcount != 1:
                raise ConflictError("workflow group changed while creating rerun")

            con.execute(
                """INSERT INTO workflows(
                    workflow_id, workflow_group_id, parent_workflow_id,
                    workflow_type, workflow_definition_id, schema_version,
                    workflow_definition_version, step_graph_hash,
                    workflow_business_key, source_id, source_fingerprint,
                    source_artifact_id, configuration_version, configuration_hash,
                    configuration_snapshot, result_status, execution_state,
                    control_state, cleanup_state, status, current_step_id,
                    state_version, draft_revision, draft_expires_at,
                    last_error_code, last_error_message, created_at, updated_at,
                    accepted_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_workflow_id, source["workflow_group_id"], source_workflow_id,
                 source["workflow_type"], source["workflow_definition_id"], source["schema_version"],
                 source["workflow_definition_version"], source["step_graph_hash"],
                 source["workflow_business_key"], None, source["source_fingerprint"], None,
                 source["configuration_version"], source["configuration_hash"],
                 canonical_json(_configuration_stored(
                     _configuration_public(json.loads(str(source["configuration_snapshot"] or "{}"))),
                     revision=1,
                 )),
                 "IN_PROGRESS", "CREATED", "RUNNING", "NONE", "DRAFT", None,
                 0, 0, None, None, None, now, now, None, None),
            )
            con.execute(
                "INSERT INTO workflow_event_streams(workflow_id, updated_at) VALUES (?,?)",
                (new_workflow_id, now),
            )

            item_map: dict[str, str] = {}
            for item in con.execute(
                "SELECT * FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id",
                (source_workflow_id,),
            ).fetchall():
                new_item_id = new_id("item")
                item_map[str(item["item_id"])] = new_item_id
                try:
                    copied_metadata = json.loads(str(item["metadata_json"] or "{}"))
                except (TypeError, json.JSONDecodeError):
                    copied_metadata = {}
                copied_metadata = redact_public_json(copied_metadata)
                if not isinstance(copied_metadata, Mapping):
                    copied_metadata = {}
                con.execute(
                    """INSERT INTO work_items(
                        item_id, workflow_id, item_identity_key, item_type, sequence,
                        identity_version, source_locator, normalized_content,
                        content_hash, role, voice_key, metadata_json, status,
                        state_version, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_item_id, new_workflow_id, item["item_identity_key"], item["item_type"], item["sequence"],
                     item["identity_version"], None, item["normalized_content"], item["content_hash"],
                     item["role"], item["voice_key"], canonical_json(copied_metadata), "PENDING", 0, now, now),
                )

            step_map: dict[str, str] = {}
            for step in con.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY step_id",
                (source_workflow_id,),
            ).fetchall():
                new_step_id = new_id("step")
                step_map[str(step["step_id"])] = new_step_id
                mapped_item_id = item_map.get(str(step["item_id"])) if step["item_id"] is not None else None
                con.execute(
                    """INSERT INTO workflow_steps(
                        step_id, workflow_id, scope, item_id, step_key, step_type,
                        step_definition_version, dependency_keys_json, status,
                        current_attempt_id, attempt_count, state_version,
                        aggregate_operation_key, operation_key_type, input_hash,
                        output_reference_json, retry_after, error_code,
                        error_details_json, started_at, finished_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_step_id, new_workflow_id, step["scope"], mapped_item_id, step["step_key"], step["step_type"],
                     step["step_definition_version"], step["dependency_keys_json"], "PENDING", None, 0, 0,
                     None, step["operation_key_type"], step["input_hash"], None, None, None, None, None, None),
                )

            for dependency in con.execute(
                "SELECT * FROM workflow_step_dependencies WHERE workflow_id=?",
                (source_workflow_id,),
            ).fetchall():
                con.execute(
                    """INSERT INTO workflow_step_dependencies(
                        dependency_id, workflow_id, step_id, depends_on_step_id,
                        binding_rule, definition_version
                    ) VALUES (?,?,?,?,?,?)""",
                    (new_id("dependency"), new_workflow_id, step_map[str(dependency["step_id"])],
                     step_map[str(dependency["depends_on_step_id"])], dependency["binding_rule"],
                     dependency["definition_version"]),
                )

            for assignment in con.execute(
                "SELECT * FROM work_item_assignments WHERE workflow_id=? AND state='ACTIVE'",
                (source_workflow_id,),
            ).fetchall():
                con.execute(
                    """INSERT INTO work_item_assignments(
                        assignment_id, workflow_id, step_id, item_id, delivery_unit_key,
                        assignment_revision, state, supersedes_assignment_id, plan_hash,
                        state_version, created_at, superseded_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("assignment"), new_workflow_id, step_map[str(assignment["step_id"])],
                     item_map[str(assignment["item_id"])], assignment["delivery_unit_key"], 0,
                     "ACTIVE", None, assignment["plan_hash"], 0, now, None),
                )

            if source_artifact is not None:
                new_artifact_id = new_id("artifact")
                con.execute(
                    """INSERT INTO artifacts(
                        artifact_id, workflow_id, item_id, step_id, attempt_id,
                        work_unit_id, work_unit_segment_id, source_import_id,
                        source_import_generation, source_import_generation_id, blob_id,
                        staging_ref, artifact_type, sha256, size_bytes, format,
                        producer, producer_version, verified, verified_at,
                        lifecycle_state, schema_version, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_artifact_id, new_workflow_id, None, None, None, None, None,
                     None, None, None, source_artifact["blob_id"], None, "source-reuse",
                     source_artifact["sha256"], source_artifact["size_bytes"], source_artifact["format"],
                     "workflow-rerun", "1", 1, now, "READY", source_artifact["schema_version"], now, now),
                )
                con.execute(
                    """INSERT INTO artifact_derivations(
                        derivation_id, parent_artifact_id, child_artifact_id,
                        relation_type, derivation_version, derivation_context_hash, created_at
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (new_id("derivation"), source_artifact["artifact_id"], new_artifact_id,
                     "CACHE_REUSE", "1", content_hash({"source_workflow_id": source_workflow_id, "rerun": new_workflow_id}), now),
                )
                con.execute(
                    "UPDATE workflows SET source_artifact_id=?, state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                    (new_artifact_id, now, new_workflow_id),
                )

            self.events.append_in_transaction(
                con, new_workflow_id, "WORKFLOW_RERUN_CREATED",
                {"parent_workflow_id": source_workflow_id, "reason": reason},
                request_id=request_id, actor_type="USER", actor_id="desktop",
            )
            snapshot = _snapshot_from_connection(con, new_workflow_id)
            self.events.write_snapshot_in_transaction(con, new_workflow_id, snapshot.as_dict())
            return snapshot

    def patch_draft(
        self,
        workflow_id: str,
        expected_state_version: int,
        *,
        expected_configuration_revision: int | None = None,
        configuration: Mapping[str, Any] | None = None,
        item_overrides: list[Mapping[str, Any]] | None = None,
        request_id: str | None = None,
    ) -> WorkflowSnapshot:
        with self.database.transaction() as con:
            row = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            require_expected(int(row["state_version"]), expected_state_version)
            overrides = list(item_overrides or [])
            has_attempt = con.execute(
                "SELECT 1 FROM step_attempts WHERE workflow_id=? AND attempt_kind='EXECUTE' LIMIT 1", (workflow_id,)
            ).fetchone() is not None

            try:
                raw_current_configuration = json.loads(str(row["configuration_snapshot"] or "{}"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RepositoryError("workflow configuration is invalid", code="PERSISTENCE_ERROR") from exc
            current_configuration = _configuration_public(raw_current_configuration)
            current_configuration_revision = _configuration_revision(
                raw_current_configuration,
                draft_revision=int(row["draft_revision"] or 0),
            )
            if (
                expected_configuration_revision is not None
                and int(expected_configuration_revision) != current_configuration_revision
            ):
                raise ConflictError(
                    "workflow configuration revision is stale",
                    code="CONFIGURATION_CONFLICT",
                    details={
                        "expected_configuration_revision": int(expected_configuration_revision),
                        "current_configuration_revision": current_configuration_revision,
                    },
                )
            requested_configuration = (
                current_configuration
                if configuration is None
                else _configuration_public(configuration, reject_sensitive=True)
            )
            configuration_changed = canonical_json(requested_configuration) != canonical_json(current_configuration)

            allowed_override_fields = {
                "role", "voice_key", "normalized_content", "metadata", "status", "skip_reason",
            }
            override_rows: list[tuple[str, sqlite3.Row, dict[str, Any]]] = []
            seen_item_ids: set[str] = set()
            for override in overrides:
                if not isinstance(override, Mapping):
                    raise RepositoryError("invalid item override", code="VALIDATION_ERROR")
                item_id = str(override.get("item_id") or "").strip()
                raw_patch = override.get("patch")
                if not item_id or not isinstance(raw_patch, Mapping):
                    raise RepositoryError("invalid item override", code="VALIDATION_ERROR")
                patch = dict(raw_patch)
                if set(patch) - allowed_override_fields:
                    raise RepositoryError("invalid item override", code="VALIDATION_ERROR")
                if item_id in seen_item_ids:
                    raise RepositoryError("an item may only be patched once per revision", code="VALIDATION_ERROR")
                seen_item_ids.add(item_id)
                item = con.execute(
                    "SELECT * FROM work_items WHERE workflow_id=? AND item_id=?",
                    (workflow_id, item_id),
                ).fetchone()
                if item is None:
                    raise NotFoundError(f"item does not exist: {item_id}")
                requested_status = patch.get("status")
                if requested_status is not None and str(requested_status).upper() not in {"PENDING", "SKIPPED"}:
                    raise RepositoryError(
                        "item status can only be PENDING or SKIPPED while editing",
                        code="VALIDATION_ERROR",
                    )
                if "metadata" in patch and not isinstance(patch["metadata"], Mapping):
                    raise RepositoryError("item metadata must be an object", code="VALIDATION_ERROR")
                if "skip_reason" in patch and patch["skip_reason"] is not None:
                    if not isinstance(patch["skip_reason"], str) or len(patch["skip_reason"]) > 500:
                        raise RepositoryError("skip_reason must be at most 500 characters", code="VALIDATION_ERROR")
                for field in ("role", "voice_key"):
                    if field in patch and patch[field] is not None:
                        if not isinstance(patch[field], str) or len(patch[field]) > 256:
                            raise RepositoryError(
                                f"{field} must be a string of at most 256 characters",
                                code="VALIDATION_ERROR",
                            )
                if "normalized_content" in patch:
                    content = patch["normalized_content"]
                    if not isinstance(content, str) or not content.strip():
                        raise RepositoryError(
                            "normalized_content must be a non-empty string",
                            code="VALIDATION_ERROR",
                        )
                    if len(content) > 1_000_000:
                        raise RepositoryError(
                            "normalized_content is too large",
                            code="VALIDATION_ERROR",
                        )
                override_rows.append((item_id, item, patch))

            changes_requested = configuration_changed or bool(overrides)
            if not changes_requested:
                return _snapshot_from_connection(con, workflow_id)

            # Parsing is an accepted workflow step, so the run is already
            # ACTIVE even though no provider/side-effect attempt has started.
            # The UI still needs to be able to save the voice selection made on
            # the configuration page during this small pre-execution window.
            # Once an attempt exists, the input graph and configuration are
            # immutable: silently changing them would make reconciliation and
            # billing keys describe a different external submission.
            if has_attempt:
                # An attempt can exist without crossing the provider boundary:
                # for example, the user may close the browser while the editor
                # is still being prepared.  That path is durably marked
                # REJECTED/WAITING_RETRY and is safe to reconfigure; the next
                # run gets a new submission key because its plan hash changes.
                is_tts_workflow = str(row["workflow_type"] or "").lower() == "tts"
                unresolved = con.execute(
                    """SELECT 1 FROM work_units
                       WHERE workflow_id=?
                         AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                         AND status <> 'SUCCEEDED'
                       UNION ALL
                       SELECT 1 FROM provider_submissions p
                       JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                       WHERE u.workflow_id=?
                         AND p.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                         AND u.status <> 'SUCCEEDED'
                       LIMIT 1""",
                    (workflow_id, workflow_id),
                ).fetchone() is not None
                safe_reconfigure = (
                    (not unresolved or is_tts_workflow)
                    and row["status"] == "ACTIVE"
                    and row["execution_state"] in {"WAITING_RETRY", "WAITING_USER"}
                    and row["control_state"] == "RUNNING"
                )
                if not safe_reconfigure:
                    if unresolved and not is_tts_workflow:
                        # External operations retain their evidence-driven
                        # handoff. TTS failures are locally retryable and were
                        # deliberately allowed above.
                        raise ConflictError(
                            "workflow has unresolved provider side effects; complete reconciliation before reconfiguring",
                            code="RECONCILIATION_REQUIRED",
                            details={"workflow_id": workflow_id},
                        )
                    raise ConflictError(
                        "workflow configuration is frozen after an execution attempt has started",
                        code="CONFIG_FROZEN",
                    )
                # The partial index permits only one active attempt per step.
                # Retire the safe, pre-boundary attempt before a changed plan
                # can create its replacement; keep the row as FAILED history
                # instead of losing the interruption audit trail.
                if overrides or requested_configuration != current_configuration:
                    now = utc_now()
                    con.execute(
                        """UPDATE step_attempts
                           SET status='FAILED', result_status='FAILED', finished_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND status IN ('WAITING_RETRY','WAITING_USER')""",
                        (now, workflow_id),
                    )
                    # A changed configuration is an explicit user decision to
                    # take ownership of the next run.  Do not leave the old
                    # step eligible for the background retry dispatcher while
                    # the renderer is still on the configuration page.
                    con.execute(
                        """UPDATE workflow_steps
                           SET status='WAITING_USER', retry_after=NULL,
                               state_version=state_version+1
                           WHERE workflow_id=? AND status IN ('WAITING_RETRY','READY')""",
                        (workflow_id,),
                    )
                    con.execute(
                        """UPDATE work_unit_attempts
                           SET status='FAILED', finished_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND status IN ('WAITING_RETRY','WAITING_USER')
                             AND side_effect_state NOT IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')""",
                        (now, workflow_id),
                    )
            if row["status"] not in {"DRAFT", "ACTIVE"}:
                raise ConflictError("only a pre-execution workflow can be patched")
            if row["status"] == "ACTIVE" and (
                row["execution_state"] not in {"CREATED", "PREPARING", "WAITING_RETRY", "WAITING_USER"}
                or row["control_state"] != "RUNNING"
            ):
                raise ConflictError("workflow configuration is frozen after generation is accepted", code="CONFIG_FROZEN")
            for item_id, item, patch in override_rows:
                current_status = str(item["status"])
                requested_status = str(patch.get("status") or current_status).upper()
                has_ready_artifact = con.execute(
                    """SELECT 1 FROM artifacts
                       WHERE workflow_id=? AND item_id=? AND artifact_type='tts-segment'
                         AND lifecycle_state='READY' AND verified=1 LIMIT 1""",
                    (workflow_id, item_id),
                ).fetchone() is not None
                unresolved_item = con.execute(
                    """SELECT 1 FROM work_unit_items wui
                       JOIN work_units wu ON wu.work_unit_id=wui.work_unit_id
                       WHERE wui.workflow_id=? AND wui.item_id=?
                         AND wu.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                         AND wu.status <> 'SUCCEEDED' LIMIT 1""",
                    (workflow_id, item_id),
                ).fetchone() is not None
                if has_ready_artifact or current_status == "SUCCEEDED":
                    raise ConflictError(
                        "a delivered item cannot be edited or skipped",
                        code="ITEM_ALREADY_DELIVERED",
                        details={"item_id": item_id},
                    )
                if unresolved_item and str(row["workflow_type"] or "").lower() != "tts":
                    raise ConflictError(
                        "an item with an unresolved provider side effect cannot be edited",
                        code="RECONCILIATION_REQUIRED",
                        details={"item_id": item_id},
                    )
                try:
                    current_metadata = json.loads(str(item["metadata_json"] or "{}"))
                except (TypeError, json.JSONDecodeError):
                    current_metadata = {}
                if not isinstance(current_metadata, Mapping):
                    current_metadata = {}
                raw_metadata = patch.get("metadata", current_metadata)
                safe_metadata = redact_public_json(raw_metadata)
                metadata = dict(safe_metadata) if isinstance(safe_metadata, Mapping) else {}
                if requested_status == "SKIPPED":
                    skip_reason = patch.get("skip_reason")
                    if skip_reason is not None:
                        metadata[_SKIP_REASON_KEY] = str(skip_reason).strip()[:500]
                    elif not str(metadata.get(_SKIP_REASON_KEY) or "").strip():
                        metadata[_SKIP_REASON_KEY] = "用户跳过"
                else:
                    metadata.pop(_SKIP_REASON_KEY, None)
                values = {
                    "role": patch.get("role", item["role"]),
                    "voice_key": patch.get("voice_key", item["voice_key"]),
                    "normalized_content": patch.get("normalized_content", item["normalized_content"]),
                    "metadata_json": canonical_json(metadata),
                }
                con.execute(
                    """UPDATE work_items SET role=?, voice_key=?, normalized_content=?,
                        content_hash=?, metadata_json=?, status=?, state_version=state_version+1,
                        updated_at=? WHERE workflow_id=? AND item_id=?""",
                    (values["role"], values["voice_key"], values["normalized_content"],
                     content_hash(values["normalized_content"]), values["metadata_json"], requested_status,
                     utc_now(), workflow_id, item_id),
                )
            current_revision = current_configuration_revision
            next_revision = current_revision + 1
            config_json = canonical_json(_configuration_stored(requested_configuration, revision=next_revision))
            config_hash = content_hash(requested_configuration)
            now = utc_now()
            updated = con.execute(
                """UPDATE workflows SET configuration_snapshot=?, configuration_hash=?,
                    draft_revision=draft_revision+1, state_version=state_version+1,
                    updated_at=? WHERE workflow_id=? AND state_version=?
                    AND (status='DRAFT' OR (status='ACTIVE' AND execution_state IN ('CREATED','PREPARING','WAITING_RETRY','WAITING_USER')
                         AND control_state='RUNNING'))""",
                (config_json, config_hash, now, workflow_id, expected_state_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("workflow changed while patching")
            self.events.append_in_transaction(
                con, workflow_id, "WORKFLOW_PATCHED",
                {
                    "draft_revision": int(row["draft_revision"]) + 1,
                    "configuration_revision": next_revision,
                    "configuration_hash": config_hash,
                    "item_count": len(overrides),
                    "item_ids": sorted(seen_item_ids),
                    "changed_fields": sorted({field for _item_id, _item, patch in override_rows for field in patch}),
                },
                request_id=request_id, actor_type="USER", actor_id="desktop",
            )
            snapshot = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
            return snapshot

    def hold_automatic_retry(
        self,
        workflow_id: str,
        expected_state_version: int,
        *,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> WorkflowSnapshot:
        """Pause safe automatic retry while the user edits configuration.

        A failed pre-submit attempt remains retryable, but it must not race a
        renderer that has returned to the configuration page.  Moving the
        eligible step to ``WAITING_USER`` is durable and survives a backend
        restart; the next explicit ``generate`` command moves the workflow
        back to RUNNING and the engine creates the correct retry attempt.
        """

        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            require_expected(snapshot.state_version, expected_state_version)
            if snapshot.execution_state not in {"WAITING_RETRY", "WAITING_USER"}:
                return snapshot

            rows = con.execute(
                """SELECT step_id, status FROM workflow_steps
                   WHERE workflow_id=? AND status IN ('WAITING_RETRY','READY')""",
                (workflow_id,),
            ).fetchall()
            if not rows:
                return snapshot

            for row in rows:
                con.execute(
                    """UPDATE workflow_steps
                       SET status='WAITING_USER', retry_after=NULL,
                           state_version=state_version+1
                       WHERE workflow_id=? AND step_id=? AND status IN ('WAITING_RETRY','READY')""",
                    (workflow_id, row["step_id"]),
                )
            now = utc_now()
            updated = con.execute(
                """UPDATE workflows
                   SET execution_state='WAITING_USER', state_version=state_version+1,
                       updated_at=?
                   WHERE workflow_id=? AND state_version=?
                     AND execution_state IN ('WAITING_RETRY','WAITING_USER')""",
                (now, workflow_id, expected_state_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("workflow changed while holding automatic retry")
            for row in rows:
                self.events.append_in_transaction(
                    con,
                    workflow_id,
                    "RETRY_HELD",
                    {
                        "step_id": str(row["step_id"]),
                        "previous_status": str(row["status"]),
                        "reason": reason,
                    },
                    request_id=request_id,
                    actor_type="USER",
                    actor_id="desktop",
                    step_id=str(row["step_id"]),
                )
            updated_snapshot = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, updated_snapshot.as_dict())
            return updated_snapshot

    def get_configuration(self, workflow_id: str) -> dict[str, Any]:
        """Return the persisted, non-secret configuration for a workflow.

        The engine uses this only after the workflow has been accepted.  It is
        deliberately read through the repository so provider payloads cannot
        accidentally depend on renderer memory or a stale UI form.
        """

        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT configuration_snapshot FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
        try:
            value = json.loads(str(row["configuration_snapshot"] or "{}"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RepositoryError("workflow configuration is invalid", code="PERSISTENCE_ERROR") from exc
        return _configuration_public(value)

    def get_configuration_revision(self, workflow_id: str) -> int:
        """Return the server-owned saved configuration revision.

        This is intentionally separate from ``draft_revision``.  Callers use
        it as a conditional-generation fence so a request cannot claim one
        revision while the worker reads another persisted configuration.
        """

        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT configuration_snapshot, draft_revision FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            try:
                value = json.loads(str(row["configuration_snapshot"] or "{}"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RepositoryError("workflow configuration is invalid", code="PERSISTENCE_ERROR") from exc
            return _configuration_revision(
                value if isinstance(value, Mapping) else {},
                draft_revision=int(row["draft_revision"] or 0),
            )

    def command(
        self,
        workflow_id: str,
        action: str,
        expected_state_version: int,
        *,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> WorkflowSnapshot:
        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            require_expected(snapshot.state_version, expected_state_version)
            changes = command_transition(snapshot.as_dict(), action)
            if not changes:
                return snapshot
            assignments: list[str] = []
            values: list[Any] = []
            for key, value in changes.items():
                assignments.append(f"{key}=?")
                values.append(value)
            assignments.extend(["state_version=state_version+1", "updated_at=?"])
            values.extend([utc_now(), workflow_id, expected_state_version])
            result = con.execute(
                f"UPDATE workflows SET {', '.join(assignments)} WHERE workflow_id=? AND state_version=?",
                values,
            )
            if result.rowcount != 1:
                raise ConflictError("workflow changed while applying command")
            if action in {"parse", "generate"} and snapshot.status == "DRAFT":
                con.execute(
                    """UPDATE workflow_groups SET lifecycle_state='ACTIVE',
                        accepted_at=COALESCE(accepted_at, ?), state_version=state_version+1,
                        updated_at=? WHERE workflow_group_id=? AND lifecycle_state='DRAFT'""",
                    (utc_now(), utc_now(), snapshot.workflow_group_id),
                )
            self.events.append_in_transaction(
                con, workflow_id, f"WORKFLOW_{action.upper()}",
                {"reason": reason} if reason else {},
                request_id=request_id, actor_type="USER", actor_id="desktop",
            )
            updated = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, updated.as_dict())
            return updated

    def finalize_generation_cleanup(
        self,
        workflow_id: str,
        *,
        reason: str | None = None,
        force_cancel: bool = False,
    ) -> WorkflowSnapshot:
        """Close local generation state and optionally finish cancellation.

        Worker cleanup keeps the conservative default: an unresolved provider
        boundary is recorded locally without guessing its remote outcome.
        The user-facing cancel route passes ``force_cancel=True`` so the local
        workflow becomes terminal immediately; late worker callbacks are
        fenced by the publication guards below.
        """

        now = utc_now()
        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            if snapshot.execution_state == "TERMINAL":
                return snapshot
            workflow_row = con.execute(
                "SELECT workflow_type FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            is_tts_workflow = bool(
                workflow_row
                and str(workflow_row["workflow_type"] or "").lower() == "tts"
            )
            # Only local TTS side effects can be retired by the user-facing
            # hard-stop path.  An external operation may already have reached
            # the remote system, so a generic force flag must never turn that
            # workflow terminal without its own recovery/confirmation fence.
            force_cancel = bool(force_cancel and is_tts_workflow)
            if snapshot.cleanup_state == "SUCCEEDED" and not force_cancel:
                return snapshot

            unresolved = con.execute(
                """SELECT 1 FROM work_units
                   WHERE workflow_id=?
                     AND side_effect_state IN ('IN_FLIGHT', 'SUBMITTED', 'CONFIRMED', 'AMBIGUOUS')
                     AND status <> 'SUCCEEDED'
                   UNION ALL
                   SELECT 1 FROM provider_submissions p
                   JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                   WHERE u.workflow_id=?
                     AND p.side_effect_state IN ('IN_FLIGHT', 'SUBMITTED', 'CONFIRMED', 'AMBIGUOUS')
                     AND u.status <> 'SUCCEEDED'
                   LIMIT 1""",
                (workflow_id, workflow_id),
            ).fetchone() is not None
            if not is_tts_workflow and _table_exists(con, "external_operations"):
                unresolved = unresolved or con.execute(
                    """SELECT 1 FROM external_operations
                       WHERE workflow_id=?
                         AND side_effect_state IN (
                             'INTENT_RECORDED', 'IN_FLIGHT', 'SUBMITTED',
                             'CONFIRMED', 'AMBIGUOUS'
                         )
                       LIMIT 1""",
                    (workflow_id,),
                ).fetchone() is not None
            cancel_requested = (
                snapshot.control_state in {"TERMINATING", "TERMINATED"}
                and snapshot.result_status == "IN_PROGRESS"
                and (force_cancel or not unresolved)
            )

            if cancel_requested:
                # Preserve any already verified item Artifact while marking
                # only the remaining local work as cancelled.
                if force_cancel:
                    # The cancellation command is the local decision point.
                    # Retire every TTS side-effect projection here so restart
                    # recovery cannot reopen a reconciliation handoff for a
                    # task the user has already stopped.
                    con.execute(
                        """UPDATE provider_submissions
                           SET side_effect_state='REJECTED', state_version=state_version+1
                           WHERE provider_submission_id IN (
                               SELECT provider_submission_id FROM work_units
                               WHERE workflow_id=? AND provider_submission_id IS NOT NULL
                           ) AND side_effect_state IN ('IN_FLIGHT', 'SUBMITTED', 'CONFIRMED', 'AMBIGUOUS')""",
                        (workflow_id,),
                    )
                    con.execute(
                        """UPDATE side_effect_intents
                           SET state='ARCHIVED', updated_at=?
                           WHERE workflow_id=? AND operation_namespace='tts'
                             AND state <> 'ARCHIVED'""",
                        (now, workflow_id),
                    )
                con.execute(
                    """UPDATE work_unit_items
                       SET result_status='CANCELLED', state_version=state_version+1
                       WHERE workflow_id=? AND result_status NOT IN ('SUCCEEDED', 'CANCELLED', 'SKIPPED')""",
                    (workflow_id,),
                )
                con.execute(
                    """UPDATE work_unit_segments
                       SET result_status='CANCELLED'
                       WHERE work_unit_id IN (
                           SELECT work_unit_id FROM work_units WHERE workflow_id=?
                       ) AND result_status NOT IN ('SUCCEEDED', 'CANCELLED', 'SKIPPED')""",
                    (workflow_id,),
                )
                con.execute(
                    """UPDATE work_units
                       SET status='CANCELLED', side_effect_state=CASE
                               WHEN ? THEN 'REJECTED' ELSE side_effect_state END,
                           finished_at=?, state_version=state_version+1
                       WHERE workflow_id=?
                         AND status NOT IN ('SUCCEEDED', 'CANCELLED')
                         AND (? OR side_effect_state IN ('NOT_STARTED', 'INTENT_RECORDED', 'REJECTED'))""",
                    (int(force_cancel), now, workflow_id, int(force_cancel)),
                )
                con.execute(
                    """UPDATE work_items
                       SET status='CANCELLED', updated_at=?, state_version=state_version+1
                       WHERE workflow_id=?
                         AND status NOT IN ('SUCCEEDED', 'CANCELLED', 'SKIPPED')
                         AND NOT EXISTS (
                             SELECT 1 FROM artifacts a
                             WHERE a.workflow_id=work_items.workflow_id
                              AND a.item_id=work_items.item_id
                              AND a.artifact_type='tts-segment'
                              AND a.lifecycle_state='READY' AND a.verified=1
                         )""",
                    (now, workflow_id),
                )
                con.execute(
                    """UPDATE work_unit_attempts
                       SET status='CANCELLED', side_effect_state=CASE
                               WHEN ? THEN 'REJECTED' ELSE side_effect_state END,
                           finished_at=?, state_version=state_version+1
                       WHERE workflow_id=?
                         AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
                         AND (? OR status <> 'AMBIGUOUS')""",
                    (int(force_cancel), now, workflow_id, int(force_cancel)),
                )
                con.execute(
                    """UPDATE step_attempts
                       SET status='CANCELLED', result_status='CANCELLED', finished_at=?, state_version=state_version+1
                       WHERE workflow_id=?
                         AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
                         AND (? OR status <> 'AMBIGUOUS')""",
                    (now, workflow_id, int(force_cancel)),
                )
                con.execute(
                    """UPDATE workflow_steps
                       SET status='CANCELLED', finished_at=?, state_version=state_version+1
                       WHERE workflow_id=?
                         AND status NOT IN ('SUCCEEDED', 'PERMANENT_FAILED', 'CANCELLED')""",
                    (now, workflow_id),
                )
                successful_items = int(con.execute(
                    """SELECT COUNT(DISTINCT a.item_id) FROM artifacts a
                       JOIN artifact_blobs b ON b.blob_id=a.blob_id
                       JOIN work_items i ON i.workflow_id=a.workflow_id AND i.item_id=a.item_id
                       WHERE a.workflow_id=? AND a.artifact_type='tts-segment'
                         AND a.lifecycle_state='READY' AND a.verified=1 AND a.item_id IS NOT NULL
                         AND b.lifecycle_state='READY' AND i.status='SUCCEEDED'""",
                    (workflow_id,),
                ).fetchone()[0])
                result_status = "PARTIAL_SUCCESS" if successful_items else "CANCELLED"
                con.execute(
                    """UPDATE workflows
                       SET result_status=?, execution_state='TERMINAL', control_state='TERMINATED',
                           cleanup_state='SUCCEEDED', last_error_code='WORKFLOW_CANCELLED',
                           last_error_message=?, finished_at=COALESCE(finished_at, ?),
                           state_version=state_version+1, updated_at=?
                       WHERE workflow_id=? AND result_status='IN_PROGRESS'
                         AND control_state IN ('TERMINATING', 'TERMINATED')""",
                    (result_status, reason or "任务已由用户取消", now, now, workflow_id),
                )
            else:
                # Cleanup is independent from business outcome.  An
                # ambiguous provider side effect therefore becomes locally
                # cleaned up while the workflow remains TERMINATING/WAITING_USER.
                con.execute(
                    """UPDATE workflows SET cleanup_state='SUCCEEDED', state_version=state_version+1,
                       updated_at=? WHERE workflow_id=? AND cleanup_state <> 'SUCCEEDED'""",
                    (now, workflow_id),
                )

            updated = _snapshot_from_connection(con, workflow_id)
            if updated.latest_event_id == snapshot.latest_event_id and updated.state_version == snapshot.state_version:
                return updated
            self.events.append_in_transaction(
                con,
                workflow_id,
                "WORKFLOW_CANCELLED" if cancel_requested else "WORKFLOW_CLEANUP_COMPLETED",
                {
                    "reason": reason,
                    "business_terminal": updated.execution_state == "TERMINAL",
                    "result_status": updated.result_status,
                    "unresolved_side_effect": unresolved,
                },
                actor_type="WORKER",
                actor_id="generation-task",
            )
            final_snapshot = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, final_snapshot.as_dict())
            return final_snapshot

    def persist_parsed_document(
        self,
        workflow_id: str,
        parsed: Any,
        *,
        source_artifact_id: str,
        expected_state_version: int,
        parsed_blob: Any | None = None,
        parsed_artifact_id: str | None = None,
        request_id: str | None = None,
    ) -> WorkflowSnapshot:
        """Atomically publish one parser result and its run-local items.

        Parsing happens outside SQLite because it is CPU-/dependency-heavy.  A
        successful parser result enters the workflow only through this method:
        the source binding, parse step, normalized items, optional JSON Blob,
        workflow transition, and event are committed together.  A repeated
        call for an already-published parse is a read-only idempotent replay.
        """

        parsed_value = parsed.as_dict() if hasattr(parsed, "as_dict") else dict(parsed or {})
        raw_items = parsed_value.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise RepositoryError("parsed document must contain at least one item", code="VALIDATION_ERROR")
        source_artifact_id = str(source_artifact_id or "")
        if not source_artifact_id:
            raise RepositoryError("source_artifact_id is required", code="VALIDATION_ERROR")
        source_sha256 = str(parsed_value.get("source_sha256") or "")
        parse_input_hash = content_hash({
            "source_artifact_id": source_artifact_id,
            "source_sha256": source_sha256,
            "parser_version": str(parsed_value.get("parser_version") or ""),
            "normalization_version": str(parsed_value.get("normalization_version") or ""),
        })
        now = utc_now()
        parse_step_id = f"step-parse-{content_hash(f'{workflow_id}:parse')[:32]}"
        parsed_artifact_id = parsed_artifact_id or f"artifact-parse-{content_hash(f'{workflow_id}:{parse_input_hash}')[:32]}"

        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            require_expected(snapshot.state_version, expected_state_version)
            source = con.execute(
                """SELECT a.artifact_id, a.sha256, a.size_bytes, a.format,
                          a.lifecycle_state, a.blob_id
                   FROM artifacts a
                   WHERE a.workflow_id=? AND a.artifact_id=?""",
                (workflow_id, source_artifact_id),
            ).fetchone()
            if source is None or source["lifecycle_state"] != "READY" or source["blob_id"] is None:
                raise RepositoryError("source Artifact is not ready for parsing", code="SOURCE_NOT_AVAILABLE")

            existing_step = con.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND step_key='parse' ORDER BY step_id LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if existing_step is not None:
                if existing_step["status"] == "SUCCEEDED" and snapshot.source_artifact_id == source_artifact_id:
                    return snapshot
                raise ConflictError("workflow already has a different parse projection")
            if snapshot.status != "DRAFT":
                raise ConflictError("parse can only publish a draft workflow")

            changes = command_transition(snapshot.as_dict(), "parse")
            con.execute(
                """UPDATE workflow_groups SET lifecycle_state='ACTIVE',
                    accepted_at=COALESCE(accepted_at, ?), state_version=state_version+1,
                    updated_at=? WHERE workflow_group_id=? AND lifecycle_state='DRAFT'""",
                (now, now, snapshot.workflow_group_id),
            )
            con.execute(
                """INSERT INTO workflow_steps(
                    step_id, workflow_id, scope, item_id, step_key, step_type,
                    step_definition_version, dependency_keys_json, status,
                    current_attempt_id, attempt_count, state_version,
                    aggregate_operation_key, operation_key_type, input_hash,
                    output_reference_json, retry_after, error_code,
                    error_details_json, started_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (parse_step_id, workflow_id, "workflow", None, "parse", "PARSER", "1", "[]",
                 "SUCCEEDED", None, 0, 1, None, None, parse_input_hash, None, None, None, None, now, now),
            )

            published_artifact_ids: list[str] = []
            if parsed_blob is not None:
                blob_sha256 = str(parsed_blob.sha256)
                blob_size = int(parsed_blob.size_bytes)
                blob_format = str(parsed_blob.format)
                storage_key = str(parsed_blob.storage_key)
                blob_row = con.execute("SELECT * FROM artifact_blobs WHERE sha256=?", (blob_sha256,)).fetchone()
                if blob_row is None:
                    blob_id = new_id("blob")
                    con.execute(
                        """INSERT INTO artifact_blobs(
                            blob_id, sha256, size_bytes, format, storage_key,
                            lifecycle_state, verified_at, created_at, deleted_at
                        ) VALUES (?,?,?,?,?,?,?,?,NULL)""",
                        (blob_id, blob_sha256, blob_size, blob_format, storage_key, "READY", now, now),
                    )
                else:
                    if (int(blob_row["size_bytes"]) != blob_size
                            or str(blob_row["storage_key"]) != storage_key
                            or blob_row["lifecycle_state"] != "READY"):
                        raise RepositoryError("parsed Blob fingerprint conflicts", code="ARTIFACT_INVALID")
                    blob_id = str(blob_row["blob_id"])
                existing_artifact = con.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=? AND workflow_id=?",
                    (parsed_artifact_id, workflow_id),
                ).fetchone()
                if existing_artifact is None:
                    con.execute(
                        """INSERT INTO artifacts(
                            artifact_id, workflow_id, item_id, step_id, attempt_id,
                            work_unit_id, work_unit_segment_id, source_import_id,
                            source_import_generation, source_import_generation_id, blob_id,
                            staging_ref, artifact_type, sha256, size_bytes, format,
                            producer, producer_version, verified, verified_at,
                            lifecycle_state, schema_version, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (parsed_artifact_id, workflow_id, None, parse_step_id, None, None, None,
                         None, None, None, blob_id, None, "parse-output", blob_sha256, blob_size,
                         blob_format, "parser", str(parsed_value.get("parser_version") or "1"),
                         1, now, "READY", "1", now, now),
                    )
                elif (str(existing_artifact["sha256"]) != blob_sha256
                      or int(existing_artifact["size_bytes"]) != blob_size
                      or existing_artifact["lifecycle_state"] != "READY"):
                    raise RepositoryError("parsed artifact id points to a different Blob", code="ARTIFACT_INVALID")
                published_artifact_ids.append(parsed_artifact_id)
                con.execute(
                    """INSERT OR IGNORE INTO artifact_derivations(
                        derivation_id, parent_artifact_id, child_artifact_id,
                        relation_type, derivation_version, derivation_context_hash, created_at
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (new_id("derivation"), source_artifact_id, parsed_artifact_id,
                     "PARSE_OUTPUT", "1", parse_input_hash, now),
                )

            item_ids: list[str] = []
            for sequence, raw_item in enumerate(raw_items):
                if not isinstance(raw_item, Mapping):
                    raise RepositoryError("parsed item is not an object", code="VALIDATION_ERROR")
                identity_key = str(raw_item.get("identity_key") or "").strip()
                content = str(raw_item.get("normalized_content") or raw_item.get("text") or "").strip()
                if not identity_key or not content:
                    raise RepositoryError("parsed item identity and content are required", code="VALIDATION_ERROR")
                item_id = f"item-parse-{content_hash(f'{workflow_id}:{identity_key}')[:32]}"
                metadata = raw_item.get("metadata")
                if not isinstance(metadata, Mapping):
                    metadata = {}
                existing_item = con.execute(
                    "SELECT * FROM work_items WHERE workflow_id=? AND item_identity_key=?",
                    (workflow_id, identity_key),
                ).fetchone()
                if existing_item is None:
                    con.execute(
                        """INSERT INTO work_items(
                            item_id, workflow_id, item_identity_key, item_type, sequence,
                            identity_version, source_locator, normalized_content,
                            content_hash, role, voice_key, metadata_json, status,
                            state_version, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (item_id, workflow_id, identity_key, str(raw_item.get("item_type") or "document"),
                         sequence, "1", str(raw_item.get("source_locator") or "")[:512], content,
                         content_hash(content), str(raw_item.get("role") or "")[:256] or None,
                         str(raw_item.get("voice_key") or "")[:256] or None,
                         canonical_json(redact_public_json(metadata)), "PENDING", 0, now, now),
                    )
                else:
                    if str(existing_item["item_id"]) != item_id or str(existing_item["content_hash"]) != content_hash(content):
                        raise ConflictError("parsed item identity is already bound to different content")
                item_ids.append(item_id)

            output_reference = {
                "source_artifact_id": source_artifact_id,
                "parsed_artifact_ids": published_artifact_ids,
                "item_ids": item_ids,
                "source_sha256": source_sha256,
                "parser_version": str(parsed_value.get("parser_version") or ""),
                "normalization_version": str(parsed_value.get("normalization_version") or ""),
            }
            con.execute(
                """UPDATE workflow_steps SET output_reference_json=?
                   WHERE workflow_id=? AND step_id=?""",
                (canonical_json(output_reference), workflow_id, parse_step_id),
            )
            assignments = [f"{key}=?" for key in changes]
            values: list[Any] = [changes[key] for key in changes]
            assignments.extend([
                "source_artifact_id=?", "current_step_id=?", "state_version=state_version+1", "updated_at=?",
            ])
            values.extend([source_artifact_id, parse_step_id, now, workflow_id, expected_state_version])
            updated = con.execute(
                f"UPDATE workflows SET {', '.join(assignments)} WHERE workflow_id=? AND state_version=?",
                values,
            )
            if updated.rowcount != 1:
                raise ConflictError("workflow changed while publishing parser output")
            self.events.append_in_transaction(
                con,
                workflow_id,
                "WORKFLOW_PARSED",
                {"source_artifact_id": source_artifact_id, "item_count": len(item_ids), "parsed_artifact_ids": published_artifact_ids},
                request_id=request_id,
                actor_type="WORKER",
                actor_id="parser",
                step_id=parse_step_id,
            )
            updated_snapshot = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, updated_snapshot.as_dict())
            return updated_snapshot

    def targeted_command(
        self,
        workflow_id: str,
        action: str,
        target: CommandTarget | Mapping[str, object],
        *,
        expected_state_version: int,
        expected_target_state_version: int,
        request_id: str | None = None,
        reason: str | None = None,
        decision: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        source_attempt_id: str | None = None,
        expected_attempt_id: str | None = None,
    ) -> WorkflowSnapshot:
        """Apply a retry/reconcile/resolve operation to one typed target.

        The workflow and target predicates are checked inside the same
        ``BEGIN IMMEDIATE`` transaction.  A caller cannot read one target,
        race another worker, and then accidentally mutate a different child.
        """

        parsed = validate_target(target)
        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            workflow_row = con.execute(
                "SELECT workflow_type FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if (
                str(workflow_row["workflow_type"] or "").lower() == "tts"
                and action.lower() in {"reconcile", "resolve"}
            ):
                raise ConflictError(
                    "TTS workflows use local task state; generate again instead of reconciling",
                    code="RECONCILIATION_DISABLED",
                )
            # Legacy renderers may still retry an obsolete confirmation click
            # with an old workflow version.  Reject that removed operation on
            # its semantic boundary first; it must never surface as a version
            # conflict or mutate a TTS target.
            require_expected(snapshot.state_version, expected_state_version)
            target_row, table, key_column = self._target_row(con, workflow_id, parsed)
            target_version = int(target_row["state_version"])
            require_expected(target_version, expected_target_state_version)
            if expected_attempt_id is not None and source_attempt_id is not None and str(expected_attempt_id) != str(source_attempt_id):
                raise ConflictError(
                    "expected attempt does not match the resolve source attempt",
                    code="TARGET_REQUIRED",
                )
            attempt_fence = str(expected_attempt_id or source_attempt_id or "") or None
            self._validate_target_attempt(con, workflow_id, parsed, target_row, attempt_fence)
            changed = False
            reconcile_attempt_id: str | None = None
            if action == "retry":
                changed = self._retry_target(con, workflow_id, parsed, target_row, table, key_column)
            elif action == "reconcile":
                # Reconciliation is deliberately an auditable command.  The
                # command creates a read-only RECONCILE attempt and target
                # fact, but it does not assert a provider result merely
                # because a request was accepted.  A later worker/evidence
                # path must still perform the actual query and resolution.
                reconcile_attempt_id = self._create_reconcile_attempt(
                    con,
                    workflow_id,
                    parsed,
                    target_row,
                    key_column,
                    expected_state_version=target_version,
                    source_attempt_id=attempt_fence,
                    reason=reason,
                )
                changed = True
            elif action == "resolve":
                if parsed.target_type in {"STEP", "ITEM"}:
                    raise ConflictError(
                        "resolve requires a work-unit, work-unit-attempt, provider-receipt, or external-operation target",
                        code="TARGET_REQUIRED",
                    )
                if decision not in {"CONFIRMED", "NOT_SUBMITTED", "BLOCKED"}:
                    raise RepositoryError("resolve decision is required", code="VALIDATION_ERROR")
                changed = self._resolve_target(con, workflow_id, parsed, target_row, table, key_column, decision)
                if evidence is None:
                    raise RepositoryError("resolve evidence is required", code="VALIDATION_ERROR")
                evidence_source = str(evidence.get("source") or "")
                evidence_hash = str(evidence.get("evidence_hash") or "")
                if not evidence_source or not evidence_hash:
                    raise RepositoryError("resolve evidence source and evidence_hash are required", code="VALIDATION_ERROR")
                # A WorkUnitAttempt target carries its owning StepAttempt.  A
                # shorthand caller may use the WorkUnitAttempt id in the
                # path, so retain the canonical StepAttempt as the evidence
                # source after the target relationship has been checked.
                evidence_attempt_id = source_attempt_id
                if evidence_attempt_id is None and parsed.target_type == "WORK_UNIT_ATTEMPT":
                    evidence_attempt_id = str(target_row["attempt_id"])
                con.execute(
                    """INSERT INTO reconcile_evidence(
                        evidence_id, workflow_id, source_attempt_id, target_type,
                        target_id, evidence_source, evidence_hash, evidence_json, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (new_id("evidence"), workflow_id, evidence_attempt_id, parsed.target_type,
                     str(target_row[key_column]), evidence_source, evidence_hash,
                     canonical_json(redact_public_json(evidence)), utc_now()),
                )
            else:
                raise RepositoryError(f"unsupported targeted action: {action}", code="VALIDATION_ERROR")
            self.events.append_in_transaction(
                con,
                workflow_id,
                f"WORKFLOW_{action.upper()}_TARGETED",
                {
                    "target": parsed.as_dict(),
                    "reason": reason,
                    "decision": decision,
                    "changed": changed,
                    "reconcile_attempt_id": reconcile_attempt_id,
                },
                request_id=request_id,
                actor_type="USER",
                actor_id="desktop",
                attempt_id=reconcile_attempt_id,
            )
            updated = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, updated.as_dict())
            return updated

    def _target_row(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        target: CommandTarget,
    ) -> tuple[sqlite3.Row, str, str]:
        if target.target_type == "STEP":
            row = con.execute("SELECT * FROM workflow_steps WHERE workflow_id=? AND step_id=?", (workflow_id, target.step_id)).fetchone()
            return self._require_target(row, "workflow_steps", "step_id", target.step_id)
        if target.target_type == "ITEM":
            row = con.execute("SELECT * FROM work_items WHERE workflow_id=? AND item_id=?", (workflow_id, target.item_id)).fetchone()
            return self._require_target(row, "work_items", "item_id", target.item_id)
        if target.target_type == "WORK_UNIT":
            row = con.execute("SELECT * FROM work_units WHERE workflow_id=? AND work_unit_id=?", (workflow_id, target.work_unit_id)).fetchone()
            return self._require_target(row, "work_units", "work_unit_id", target.work_unit_id)
        if target.target_type == "WORK_UNIT_ATTEMPT":
            row = con.execute("SELECT * FROM work_unit_attempts WHERE workflow_id=? AND work_unit_attempt_id=?", (workflow_id, target.work_unit_attempt_id)).fetchone()
            return self._require_target(row, "work_unit_attempts", "work_unit_attempt_id", target.work_unit_attempt_id)
        if target.target_type == "PROVIDER_RECEIPT":
            row = con.execute(
                """SELECT r.* FROM provider_receipts r
                    JOIN provider_receipt_bindings b
                      ON b.receipt_id=r.receipt_id AND b.workflow_id=?
                    WHERE r.receipt_id=?
                    LIMIT 1""",
                (workflow_id, target.provider_receipt_id),
            ).fetchone()
            return self._require_target(row, "provider_receipts", "receipt_id", target.provider_receipt_id)
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_operations'"
        ).fetchone() is None:
            raise RepositoryError(
                "external operation targets require the Full workflow profile",
                code="EXTERNAL_CAPABILITY_REQUIRED",
            )
        row = con.execute(
            """SELECT o.* FROM external_operations o WHERE o.workflow_id=? AND o.external_operation_id=?""",
            (workflow_id, target.external_operation_id),
        ).fetchone()
        return self._require_target(row, "external_operations", "external_operation_id", target.external_operation_id)

    @staticmethod
    def _require_target(row: sqlite3.Row | None, table: str, key_column: str, key: str | None) -> tuple[sqlite3.Row, str, str]:
        if row is None:
            raise NotFoundError(f"target does not exist: {key}")
        return row, table, key_column

    def _validate_target_attempt(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        target: CommandTarget,
        row: sqlite3.Row,
        attempt_id: str | None,
    ) -> None:
        """Fence a command to the attempt that actually owns its target."""

        if not attempt_id:
            return
        attempt = con.execute(
            "SELECT step_id FROM step_attempts WHERE workflow_id=? AND attempt_id=?",
            (workflow_id, attempt_id),
        ).fetchone()
        # Backwards-compatible shorthand for a WorkUnitAttempt path.  The
        # canonical source used for evidence is filled from the row later.
        if attempt is None:
            if target.target_type == "WORK_UNIT_ATTEMPT" and target.work_unit_attempt_id == attempt_id:
                return
            raise NotFoundError("target attempt does not exist")

        matches = False
        if target.target_type == "STEP":
            matches = str(attempt["step_id"]) == str(target.step_id)
        elif target.target_type == "ITEM":
            matches = (
                str(attempt["step_id"]) == str(target.step_id)
                and con.execute(
                    """SELECT 1 FROM work_unit_items wui
                       JOIN work_unit_attempts wua
                         ON wua.workflow_id=wui.workflow_id
                        AND wua.work_unit_id=wui.work_unit_id
                        AND wua.attempt_id=?
                       WHERE wui.workflow_id=? AND wui.item_id=? LIMIT 1""",
                    (attempt_id, workflow_id, target.item_id),
                ).fetchone() is not None
            )
        elif target.target_type == "WORK_UNIT":
            matches = con.execute(
                """SELECT 1 FROM work_unit_attempts
                   WHERE workflow_id=? AND work_unit_id=? AND attempt_id=? LIMIT 1""",
                (workflow_id, target.work_unit_id, attempt_id),
            ).fetchone() is not None
        elif target.target_type == "WORK_UNIT_ATTEMPT":
            matches = str(row["attempt_id"]) == attempt_id
        elif target.target_type == "PROVIDER_RECEIPT":
            matches = con.execute(
                """SELECT 1 FROM provider_receipt_bindings
                   WHERE workflow_id=? AND receipt_id=?
                     AND (observed_by_attempt_id=? OR work_unit_attempt_id=?)
                   LIMIT 1""",
                (workflow_id, target.provider_receipt_id, attempt_id, attempt_id),
            ).fetchone() is not None
        else:
            # External operations can be created before a workflow step has a
            # delivery attempt.  The workflow fence is still enforced above;
            # there is no stronger local relationship to require here.
            matches = True
        if not matches:
            raise ConflictError(
                "expected attempt is not associated with the target",
                code="TARGET_REQUIRED",
                details={"expected_attempt_id": attempt_id, "target": target.as_dict()},
            )

    def _create_reconcile_attempt(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        target: CommandTarget,
        row: sqlite3.Row,
        key_column: str,
        *,
        expected_state_version: int,
        source_attempt_id: str | None,
        reason: str | None,
    ) -> str:
        """Persist one read-only reconciliation attempt and its target fact."""

        if target.target_type not in {
            "WORK_UNIT", "WORK_UNIT_ATTEMPT", "PROVIDER_RECEIPT", "EXTERNAL_OPERATION",
        }:
            raise ConflictError(
                "reconcile requires a work unit, provider receipt or external operation target",
                code="TARGET_REQUIRED",
            )

        step_id: str | None = None
        inferred_source_attempt_id: str | None = source_attempt_id
        if target.target_type in {"WORK_UNIT", "WORK_UNIT_ATTEMPT"}:
            step_id = str(row["step_id"])
            if inferred_source_attempt_id is None and target.target_type == "WORK_UNIT_ATTEMPT":
                inferred_source_attempt_id = str(row["attempt_id"])
        elif target.target_type == "PROVIDER_RECEIPT":
            binding = con.execute(
                """SELECT u.step_id, u.work_unit_id, b.observed_by_attempt_id
                   FROM provider_receipt_bindings b
                   JOIN work_units u
                     ON u.workflow_id=b.workflow_id AND u.work_unit_id=b.work_unit_id
                   WHERE b.workflow_id=? AND b.receipt_id=?
                   ORDER BY b.last_observed_at DESC, b.binding_id DESC LIMIT 1""",
                (workflow_id, target.provider_receipt_id),
            ).fetchone()
            if binding is None:
                raise NotFoundError("provider receipt is not bound to this workflow")
            step_id = str(binding["step_id"])
            if inferred_source_attempt_id is None and binding["observed_by_attempt_id"]:
                inferred_source_attempt_id = str(binding["observed_by_attempt_id"])
        else:
            operation_item = row["item_id"]
            if operation_item:
                step = con.execute(
                    """SELECT step_id FROM workflow_steps
                       WHERE workflow_id=? AND scope='item' AND item_id=?
                       ORDER BY step_id LIMIT 1""",
                    (workflow_id, operation_item),
                ).fetchone()
            else:
                step = None
            if step is None:
                step = con.execute(
                    "SELECT current_step_id AS step_id FROM workflows WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()
            step_id = str(step["step_id"] or "") if step is not None else ""
            if not step_id:
                raise ConflictError("reconcile target has no local step", code="TARGET_REQUIRED")

        if not step_id:
            raise ConflictError("reconcile target has no local step", code="TARGET_REQUIRED")
        if inferred_source_attempt_id is not None and con.execute(
            "SELECT 1 FROM step_attempts WHERE workflow_id=? AND attempt_id=?",
            (workflow_id, inferred_source_attempt_id),
        ).fetchone() is None:
            inferred_source_attempt_id = None

        now = utc_now()
        reconcile_attempt_id = new_id("attempt")
        attempt_seq = int(con.execute(
            "SELECT COALESCE(MAX(attempt_seq),0)+1 FROM step_attempts WHERE workflow_id=? AND step_id=?",
            (workflow_id, step_id),
        ).fetchone()[0])
        con.execute(
            """INSERT INTO step_attempts(
                attempt_id, workflow_id, step_id, attempt_kind, attempt_seq,
                execute_attempt_no, status, result_status, error_code,
                error_details_json, lease_fencing_token, state_version,
                started_at, heartbeat_at, finished_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (reconcile_attempt_id, workflow_id, step_id, "RECONCILE", attempt_seq,
             None, "CREATED", "IN_PROGRESS", None,
             canonical_json({"reason": reason}) if reason else None, None, 0,
             now, None, None),
        )
        con.execute(
            """UPDATE workflow_steps SET current_attempt_id=?,
                attempt_count=attempt_count+1, state_version=state_version+1
                WHERE workflow_id=? AND step_id=?""",
            (reconcile_attempt_id, workflow_id, step_id),
        )
        try:
            con.execute(
                """INSERT INTO reconcile_targets(
                    reconcile_target_id, workflow_id, reconcile_attempt_id,
                    target_type, target_id, source_attempt_id,
                    expected_state_version, created_at
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (new_id("reconcile-target"), workflow_id, reconcile_attempt_id,
                 target.target_type, str(row[key_column]), inferred_source_attempt_id,
                 expected_state_version, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"reconcile target cannot be recorded: {exc}") from exc
        return reconcile_attempt_id


    def _retry_target(self, con: sqlite3.Connection, workflow_id: str, target: CommandTarget, row: sqlite3.Row, table: str, key_column: str) -> bool:
        if target.target_type == "STEP":
            if row["status"] not in {"WAITING_RETRY", "RETRYABLE_FAILED", "AMBIGUOUS", "WAITING_USER"}:
                raise ConflictError(f"step status {row['status']} is not retryable")
            transition_step(str(row["status"]), "READY")
            result = con.execute(
                "UPDATE workflow_steps SET status='READY', error_code=NULL, retry_after=NULL, state_version=state_version+1 WHERE workflow_id=? AND step_id=? AND state_version=?",
                (workflow_id, row["step_id"], row["state_version"]),
            )
        elif target.target_type == "ITEM":
            # TTS legacy projections may still contain AMBIGUOUS/UNRESOLVED
            # item rows before startup recovery has normalized them. They are
            # local retry candidates; no provider lookup is needed.
            workflow_type = con.execute(
                "SELECT workflow_type FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            is_tts_workflow = str(workflow_type["workflow_type"] or "").lower() == "tts" if workflow_type else False
            retryable_item_statuses = {"FAILED"}
            if is_tts_workflow:
                retryable_item_statuses.update({"SUCCEEDED", "AMBIGUOUS", "UNRESOLVED"})
            if row["status"] not in retryable_item_statuses:
                raise ConflictError(f"item status {row['status']} is not retryable")
            # Match the workspace projection's latest-artifact fence. A
            # corrupted/legacy artifact may still carry READY+verified flags,
            # but if its Artifact and Blob facts disagree it is precisely a
            # local retry target and must not be mistaken for delivered audio.
            latest_artifact = con.execute(
                """SELECT a.format AS artifact_format, a.sha256 AS artifact_sha256,
                          a.size_bytes AS artifact_size_bytes,
                          b.format AS blob_format, b.sha256 AS blob_sha256,
                          b.size_bytes AS blob_size_bytes
                   FROM artifacts a
                   JOIN artifact_blobs b ON b.blob_id=a.blob_id
                   WHERE a.workflow_id=? AND a.item_id=? AND a.artifact_type='tts-segment'
                     AND a.lifecycle_state='READY' AND a.verified=1 AND b.lifecycle_state='READY'
                   ORDER BY a.created_at DESC, a.artifact_id DESC
                   LIMIT 1""",
                (workflow_id, target.item_id),
            ).fetchone()
            if latest_artifact is not None:
                from .workspace import artifact_blob_facts_match

                if artifact_blob_facts_match(
                    artifact_format=latest_artifact["artifact_format"],
                    blob_format=latest_artifact["blob_format"],
                    artifact_sha256=latest_artifact["artifact_sha256"],
                    blob_sha256=latest_artifact["blob_sha256"],
                    artifact_size_bytes=latest_artifact["artifact_size_bytes"],
                    blob_size_bytes=latest_artifact["blob_size_bytes"],
                ):
                    raise ConflictError(
                        "an item with a verified artifact cannot be retried in place",
                        code="ITEM_ALREADY_DELIVERED",
                        details={"item_id": target.item_id},
                    )
            step = con.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                (workflow_id, target.step_id),
            ).fetchone()
            if step is None:
                raise NotFoundError(f"step does not exist: {target.step_id}")
            # The failed item can be retried with a narrower plan. Retire the
            # old local attempt first so the active-attempt partial index does
            # not reject the new work unit. Provider state is never queried.
            if step["status"] in {"WAITING_RETRY", "WAITING_USER", "READY"}:
                now = utc_now()
                active_attempts = con.execute(
                    """SELECT attempt_id FROM step_attempts
                       WHERE workflow_id=? AND step_id=?
                         AND status IN ('WAITING_RETRY','WAITING_USER')""",
                    (workflow_id, target.step_id),
                ).fetchall()
                for attempt in active_attempts:
                    con.execute(
                        """UPDATE step_attempts
                           SET status='FAILED', result_status='FAILED',
                               finished_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND attempt_id=?""",
                        (now, workflow_id, attempt["attempt_id"]),
                    )
                    con.execute(
                        """UPDATE work_unit_attempts
                           SET status='FAILED', finished_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND attempt_id=?
                             AND side_effect_state NOT IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')""",
                        (now, workflow_id, attempt["attempt_id"]),
                    )
            result = con.execute(
                "UPDATE work_items SET status='PENDING', state_version=state_version+1, updated_at=? WHERE workflow_id=? AND item_id=? AND state_version=?",
                (utc_now(), workflow_id, row["item_id"], row["state_version"]),
            )
        elif target.target_type == "WORK_UNIT":
            if row["status"] == "SUCCEEDED":
                raise ConflictError(
                    "a succeeded work unit cannot be retried in place; create a rerun",
                    code="ITEM_ALREADY_DELIVERED",
                )
            if row["side_effect_state"] in {"IN_FLIGHT", "SUBMITTED", "AMBIGUOUS"}:
                raise ConflictError("work unit has an unresolved side effect")
            result = con.execute(
                "UPDATE work_units SET status='READY', state_version=state_version+1 WHERE workflow_id=? AND work_unit_id=? AND state_version=?",
                (workflow_id, row["work_unit_id"], row["state_version"]),
            )
        elif target.target_type == "EXTERNAL_OPERATION":
            if row["side_effect_state"] != "REJECTED":
                raise ConflictError("external operation is retryable only after a confirmed non-submission")
            now = utc_now()
            result = con.execute(
                """UPDATE external_operations
                   SET side_effect_state='INTENT_RECORDED', receipt_json='{}',
                       confirmed_at=NULL, state_version=state_version+1
                   WHERE workflow_id=? AND external_operation_id=? AND state_version=?""",
                (workflow_id, row["external_operation_id"], row["state_version"]),
            )
            if result.rowcount == 1:
                con.execute(
                    """UPDATE external_records
                       SET external_status='PENDING', last_error=NULL, updated_at=?
                       WHERE external_record_mapping_id=?""",
                    (now, row["external_record_mapping_id"]),
                )
                con.execute(
                    """UPDATE side_effect_intents
                       SET state='RECORDED', updated_at=?
                       WHERE workflow_id=? AND operation_namespace='external'
                         AND operation_key=?""",
                    (now, workflow_id, f"{row['external_record_mapping_id']}:{row['external_operation_key']}"),
                )
        else:
            raise ConflictError("retry target must be a step, item, work unit or external operation")
        if result.rowcount != 1:
            raise ConflictError("target changed while retrying")
        return True

    @staticmethod
    def _tts_receipt_for_work_unit(
        con: sqlite3.Connection,
        workflow_id: str,
        work_unit_id: str,
        submission_id: str | None,
    ) -> sqlite3.Row | None:
        """Return the run-local receipt that proves a TTS boundary was crossed."""

        if not submission_id:
            return None
        return con.execute(
            """SELECT r.*, b.work_unit_attempt_id AS binding_work_unit_attempt_id,
                      b.observed_by_attempt_id AS binding_observed_by_attempt_id
               FROM provider_receipts r
               JOIN provider_receipt_bindings b
                 ON b.receipt_id=r.receipt_id AND b.workflow_id=? AND b.work_unit_id=?
               WHERE r.provider_submission_id=?
               ORDER BY b.last_observed_at DESC, b.binding_id DESC
               LIMIT 1""",
            (workflow_id, work_unit_id, submission_id),
        ).fetchone()

    @staticmethod
    def _resolution_state_conflict(message: str, *, evidence_required: bool = False) -> None:
        raise ConflictError(message, code="EVIDENCE_REQUIRED" if evidence_required else "STATE_CONFLICT")

    def list_open_reconciliations(self, workflow_id: str) -> list[dict[str, Any]]:
        """Return external-operation handoffs for one workflow.

        TTS workflows deliberately return no reconciliation projection:
        their durable local state is the only source of truth and a later
        explicit generation creates a fresh attempt.
        """

        with self.database.read_transaction() as con:
            rows = con.execute(
                """SELECT i.intervention_id, i.attempt_id, i.work_unit_id, i.reason,
                          i.created_at,
                          u.state_version AS work_unit_state_version,
                          u.status AS work_unit_status,
                          u.side_effect_state,
                          (
                              SELECT json_extract(e.payload_json, '$.details.works_name')
                              FROM workflow_events e
                              WHERE e.workflow_id = i.workflow_id
                                AND e.event_type = 'TTS_SUBMISSION_AMBIGUOUS'
                                AND json_extract(e.payload_json, '$.work_unit_id') = i.work_unit_id
                              ORDER BY e.seq DESC LIMIT 1
                          ) AS works_name
                   FROM user_interventions i
                   JOIN workflows w ON w.workflow_id = i.workflow_id
                   LEFT JOIN work_units u ON u.work_unit_id = i.work_unit_id
                   WHERE i.workflow_id = ?
                     AND LOWER(COALESCE(w.workflow_type, '')) <> 'tts'
                     AND i.state = 'OPEN'
                     AND i.intervention_type = 'RECONCILE_PROVIDER'
                   ORDER BY i.created_at, i.intervention_id""",
                (workflow_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                work_unit_state_version = row["work_unit_state_version"]
                result.append({
                    "intervention_id": str(row["intervention_id"]),
                    "attempt_id": str(row["attempt_id"]) if row["attempt_id"] else None,
                    "work_unit_id": str(row["work_unit_id"]) if row["work_unit_id"] else None,
                    "work_unit_state_version": int(work_unit_state_version) if work_unit_state_version is not None else None,
                    "work_unit_status": str(row["work_unit_status"] or "") or None,
                    "side_effect_state": str(row["side_effect_state"] or "") or None,
                    "works_name": str(row["works_name"]) if row["works_name"] else None,
                    "reason": str(row["reason"] or "")[:500],
                    "created_at": str(row["created_at"] or "") or None,
                })
            return result

    def _resolve_target(self, con: sqlite3.Connection, workflow_id: str, target: CommandTarget, row: sqlite3.Row, table: str, key_column: str, decision: str) -> bool:
        submission_id: str | None = None
        work_unit_id: str | None = None
        submission_state_override: str | None = None
        intent_state_override: str | None = None
        unit_side_effect_override: str | None = None
        unit_status_override: str | None = None
        skip_unit_projection = False
        now = utc_now()
        if target.target_type == "WORK_UNIT":
            work_unit_id = str(row["work_unit_id"])
            submission_id = str(row["provider_submission_id"] or "") or None
            current_side_effect = str(row["side_effect_state"])
            current_status = str(row["status"])
            submission = (
                con.execute(
                    "SELECT side_effect_state FROM provider_submissions WHERE provider_submission_id=?",
                    (submission_id,),
                ).fetchone()
                if submission_id
                else None
            )
            receipt = self._tts_receipt_for_work_unit(con, workflow_id, work_unit_id, submission_id)
            desired_side_effect = {"CONFIRMED": "CONFIRMED", "NOT_SUBMITTED": "REJECTED", "BLOCKED": "AMBIGUOUS"}[decision]
            if decision == "NOT_SUBMITTED" and (
                current_side_effect in {"SUBMITTED", "CONFIRMED"} or receipt is not None
            ):
                self._resolution_state_conflict("an observed TTS submission cannot be resolved as not submitted")
            if current_side_effect in {"CONFIRMED", "REJECTED"} and current_side_effect != desired_side_effect:
                self._resolution_state_conflict("a resolved TTS target cannot be downgraded")
            if decision == "CONFIRMED" and receipt is None:
                self._resolution_state_conflict(
                    "confirmed TTS target requires an observed provider receipt",
                    evidence_required=True,
                )
            if decision == "CONFIRMED" and submission is not None and str(submission["side_effect_state"]) == "REJECTED":
                self._resolution_state_conflict("a rejected TTS submission cannot be confirmed")
            side_effect = current_side_effect if decision == "BLOCKED" and current_side_effect in {"SUBMITTED", "CONFIRMED"} else desired_side_effect
            status = (
                current_status if decision == "CONFIRMED" and current_status == "SUCCEEDED"
                else {"CONFIRMED": "VERIFYING", "NOT_SUBMITTED": "READY", "BLOCKED": "WAITING_USER"}[decision]
            )
            submission_state_override = (
                str(submission["side_effect_state"])
                if decision == "BLOCKED" and submission is not None and str(submission["side_effect_state"]) in {"SUBMITTED", "CONFIRMED"}
                else side_effect
            )
            intent_state_override = (
                "ARCHIVED" if decision == "NOT_SUBMITTED"
                else "COMMITTED" if submission_state_override in {"SUBMITTED", "CONFIRMED"}
                else "NEEDS_RECONCILE"
            )
            unit_side_effect_override = side_effect
            unit_status_override = status
            result = con.execute(
                "UPDATE work_units SET side_effect_state=?, status=?, state_version=state_version+1 WHERE workflow_id=? AND work_unit_id=? AND state_version=?",
                (side_effect, status, workflow_id, row["work_unit_id"], row["state_version"]),
            )
            skip_unit_projection = True
        elif target.target_type == "WORK_UNIT_ATTEMPT":
            work_unit_id = str(row["work_unit_id"])
            unit = con.execute(
                "SELECT * FROM work_units WHERE workflow_id=? AND work_unit_id=?",
                (workflow_id, work_unit_id),
            ).fetchone()
            if unit is None:
                raise RepositoryError("TTS work unit is missing for the attempt", code="PERSISTENCE_ERROR")
            submission_id = str(unit["provider_submission_id"] or "") or None
            current_side_effect = str(row["side_effect_state"])
            submission = (
                con.execute(
                    "SELECT side_effect_state FROM provider_submissions WHERE provider_submission_id=?",
                    (submission_id,),
                ).fetchone()
                if submission_id
                else None
            )
            receipt = self._tts_receipt_for_work_unit(con, workflow_id, work_unit_id, submission_id)
            desired_side_effect = {"CONFIRMED": "CONFIRMED", "NOT_SUBMITTED": "REJECTED", "BLOCKED": "AMBIGUOUS"}[decision]
            if decision == "NOT_SUBMITTED" and (
                current_side_effect in {"SUBMITTED", "CONFIRMED"}
                or str(unit["side_effect_state"]) in {"SUBMITTED", "CONFIRMED"}
                or receipt is not None
            ):
                self._resolution_state_conflict("an observed TTS submission cannot be resolved as not submitted")
            if current_side_effect in {"CONFIRMED", "REJECTED"} and current_side_effect != desired_side_effect:
                self._resolution_state_conflict("a resolved TTS target cannot be downgraded")
            if decision == "CONFIRMED" and receipt is None:
                self._resolution_state_conflict(
                    "confirmed TTS target requires an observed provider receipt",
                    evidence_required=True,
                )
            if decision == "CONFIRMED" and submission is not None and str(submission["side_effect_state"]) == "REJECTED":
                self._resolution_state_conflict("a rejected TTS submission cannot be confirmed")
            side_effect = current_side_effect if decision == "BLOCKED" and current_side_effect in {"SUBMITTED", "CONFIRMED"} else desired_side_effect
            status = (
                str(row["status"])
                if decision == "CONFIRMED" and str(row["status"]) == "SUCCEEDED"
                else {"CONFIRMED": "VERIFYING", "NOT_SUBMITTED": "CREATED", "BLOCKED": "WAITING_USER"}[decision]
            )
            submission_state_override = (
                str(submission["side_effect_state"])
                if decision == "BLOCKED" and submission is not None and str(submission["side_effect_state"]) in {"SUBMITTED", "CONFIRMED"}
                else side_effect
            )
            intent_state_override = (
                "ARCHIVED" if decision == "NOT_SUBMITTED"
                else "COMMITTED" if submission_state_override in {"SUBMITTED", "CONFIRMED"}
                else "NEEDS_RECONCILE"
            )
            unit_side_effect_override = (
                str(unit["side_effect_state"])
                if decision == "BLOCKED" and str(unit["side_effect_state"]) in {"SUBMITTED", "CONFIRMED"}
                else desired_side_effect
            )
            unit_status_override = (
                str(unit["status"])
                if decision == "CONFIRMED" and str(unit["status"]) == "SUCCEEDED" else status
            )
            result = con.execute(
                "UPDATE work_unit_attempts SET side_effect_state=?, status=?, state_version=state_version+1 WHERE workflow_id=? AND work_unit_attempt_id=? AND state_version=?",
                (side_effect, status, workflow_id, row["work_unit_attempt_id"], row["state_version"]),
            )
        elif target.target_type == "PROVIDER_RECEIPT":
            submission_id = str(row["provider_submission_id"])
            binding = con.execute(
                """SELECT b.work_unit_id, b.work_unit_attempt_id, b.observed_by_attempt_id,
                          wu.side_effect_state AS unit_side_effect_state, wu.status AS unit_status,
                          p.side_effect_state AS submission_side_effect_state
                   FROM provider_receipt_bindings b
                   JOIN work_units wu
                     ON wu.workflow_id=b.workflow_id AND wu.work_unit_id=b.work_unit_id
                   JOIN provider_submissions p ON p.provider_submission_id=?
                   WHERE b.workflow_id=? AND b.receipt_id=?
                   ORDER BY b.last_observed_at DESC, b.binding_id DESC
                   LIMIT 1""",
                (submission_id, workflow_id, row["receipt_id"]),
            ).fetchone()
            if binding is None:
                raise RepositoryError("provider receipt has no run-local work-unit binding", code="PERSISTENCE_ERROR")
            work_unit_id = str(binding["work_unit_id"])
            current_side_effect = str(binding["unit_side_effect_state"])
            submission_state = str(binding["submission_side_effect_state"])
            if decision == "NOT_SUBMITTED":
                self._resolution_state_conflict("a durable provider receipt cannot be resolved as not submitted")
            if decision == "CONFIRMED" and submission_state == "REJECTED":
                self._resolution_state_conflict("a rejected TTS submission cannot be confirmed")
            if decision == "BLOCKED" and current_side_effect == "CONFIRMED":
                self._resolution_state_conflict("a confirmed TTS target cannot be blocked")
            query_status = {"CONFIRMED": "FOUND", "BLOCKED": "CONFLICT"}[decision]
            result = con.execute(
                "UPDATE provider_receipts SET query_status=?, confirmed_at=?, state_version=state_version+1 "
                "WHERE receipt_id=? AND state_version=?",
                (query_status, now if decision == "CONFIRMED" else row["confirmed_at"], row["receipt_id"], row["state_version"]),
            )
            if decision == "CONFIRMED":
                unit_side_effect_override = "CONFIRMED"
                unit_status_override = "SUCCEEDED" if str(binding["unit_status"]) == "SUCCEEDED" else "VERIFYING"
                submission_state_override = "CONFIRMED"
                intent_state_override = "COMMITTED"
            else:
                unit_side_effect_override = current_side_effect if current_side_effect in {
                    "IN_FLIGHT", "SUBMITTED", "CONFIRMED", "AMBIGUOUS"
                } else "AMBIGUOUS"
                unit_status_override = "WAITING_USER"
                submission_state_override = submission_state if submission_state in {"SUBMITTED", "CONFIRMED"} else "AMBIGUOUS"
                intent_state_override = "COMMITTED" if submission_state_override in {"SUBMITTED", "CONFIRMED"} else "NEEDS_RECONCILE"
        else:
            # External operations have their own dedicated service endpoint,
            # but they are also valid typed targets for the generic resolve
            # command.  Keep this path receipt/evidence driven: a manual
            # CONFIRMED decision is only allowed when the operation already
            # carries an observed external id, so a UI cannot manufacture a
            # successful side effect from a bare click.
            operation_state = str(row["side_effect_state"])
            desired_state = {"CONFIRMED": "CONFIRMED", "NOT_SUBMITTED": "REJECTED", "BLOCKED": "AMBIGUOUS"}[decision]
            if operation_state in {"CONFIRMED", "REJECTED"} and operation_state != desired_state:
                raise ConflictError("a resolved external operation cannot be downgraded")
            receipt: dict[str, Any] = {}
            try:
                parsed_receipt = json.loads(str(row["receipt_json"] or "{}"))
                if isinstance(parsed_receipt, dict):
                    receipt = parsed_receipt
            except (TypeError, json.JSONDecodeError):
                receipt = {}
            external_record_id = str(receipt.get("external_record_id") or "").strip()
            if decision == "NOT_SUBMITTED" and (operation_state == "SUBMITTED" or external_record_id):
                raise ConflictError(
                    "an observed external submission cannot be resolved as not submitted",
                    code="STATE_CONFLICT",
                )
            if decision == "CONFIRMED" and not external_record_id:
                raise ConflictError(
                    "confirmed external operation requires an observed external_record_id",
                    code="EVIDENCE_REQUIRED",
                )
            if decision == "CONFIRMED" and operation_state not in {"IN_FLIGHT", "SUBMITTED", "AMBIGUOUS", "CONFIRMED"}:
                raise ConflictError("external operation is not ready to confirm", code="STATE_CONFLICT")
            result = con.execute(
                "UPDATE external_operations SET side_effect_state=?, confirmed_at=?, state_version=state_version+1 "
                "WHERE workflow_id=? AND external_operation_id=? AND state_version=?",
                (desired_state, now if decision == "CONFIRMED" else None, workflow_id,
                 row["external_operation_id"], row["state_version"]),
            )
            mapping_id = str(row["external_record_mapping_id"])
            record_state = {"CONFIRMED": "VERIFIED", "NOT_SUBMITTED": "NOT_FOUND", "BLOCKED": "BLOCKED"}[decision]
            con.execute(
                """UPDATE external_records SET external_record_id=COALESCE(NULLIF(?, ''), external_record_id),
                   external_status=?, last_verified_at=?, last_error=?, updated_at=?
                   WHERE external_record_mapping_id=?""",
                (external_record_id, record_state, now if decision == "CONFIRMED" else None,
                 None if decision == "CONFIRMED" else ("MANUAL_NOT_SUBMITTED" if decision == "NOT_SUBMITTED" else "MANUAL_BLOCK"),
                 now, mapping_id),
            )
            if decision == "CONFIRMED":
                binding_key = f"external:{mapping_id}:{row['external_operation_id']}:verified"
                con.execute(
                    """INSERT OR IGNORE INTO external_record_bindings(
                       binding_id, binding_key, external_record_mapping_id, workflow_id, item_id,
                       external_operation_id, relation_type, first_touched_at, last_touched_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (new_id("external-binding"), binding_key, mapping_id, workflow_id, row["item_id"],
                     row["external_operation_id"], "VERIFIED", now, now),
                )
            con.execute(
                """UPDATE side_effect_intents SET state=?, updated_at=?
                   WHERE workflow_id=? AND operation_namespace='external' AND operation_key=?""",
                ("COMMITTED" if decision == "CONFIRMED" else "ARCHIVED" if decision == "NOT_SUBMITTED" else "NEEDS_RECONCILE",
                 now, workflow_id, f"{mapping_id}:{row['external_operation_key']}"),
            )
            con.execute(
                """UPDATE user_interventions SET state='RESOLVED', resolved_by='desktop',
                   resolved_at=?, state_version=state_version+1, updated_at=?
                   WHERE intervention_id=? AND state IN ('OPEN','CLAIMED')""",
                (now, now, f"intervention_external_{row['external_operation_id']}"),
            )
        if result.rowcount != 1:
            raise ConflictError("target changed while resolving")
        if work_unit_id is not None and not skip_unit_projection:
            unit_side_effect = unit_side_effect_override or {
                "CONFIRMED": "CONFIRMED", "NOT_SUBMITTED": "REJECTED", "BLOCKED": "AMBIGUOUS"
            }[decision]
            unit_status = unit_status_override or {
                "CONFIRMED": "VERIFYING", "NOT_SUBMITTED": "READY", "BLOCKED": "WAITING_USER"
            }[decision]
            con.execute(
                """UPDATE work_units SET side_effect_state=?, status=?, state_version=state_version+1
                   WHERE workflow_id=? AND work_unit_id=?""",
                (unit_side_effect, unit_status, workflow_id, work_unit_id),
            )
        if submission_id is not None:
            submission_state = submission_state_override or {
                "CONFIRMED": "CONFIRMED", "NOT_SUBMITTED": "REJECTED", "BLOCKED": "AMBIGUOUS"
            }[decision]
            intent_state = intent_state_override or {
                "CONFIRMED": "COMMITTED", "NOT_SUBMITTED": "ARCHIVED", "BLOCKED": "NEEDS_RECONCILE"
            }[decision]
            con.execute(
                """UPDATE provider_submissions SET side_effect_state=?,
                   confirmed_at=CASE WHEN ?='CONFIRMED' THEN COALESCE(confirmed_at, ?) ELSE confirmed_at END,
                   state_version=state_version+1
                   WHERE provider_submission_id=?""",
                (submission_state, submission_state, now, submission_id),
            )
            if work_unit_id is not None:
                con.execute(
                    """UPDATE side_effect_intents SET state=?, updated_at=?
                       WHERE workflow_id=? AND work_unit_id=? AND operation_namespace='tts'""",
                    (intent_state, now, workflow_id, work_unit_id),
                )
        if work_unit_id is not None and decision in {"CONFIRMED", "NOT_SUBMITTED"}:
            # TTS reconciliation is represented by a durable intervention. A
            # successful or explicitly non-submitted decision closes that
            # handoff; BLOCKED deliberately remains open for further evidence.
            con.execute(
                """UPDATE user_interventions SET state='RESOLVED', resolved_by='desktop',
                   resolved_at=?, state_version=state_version+1, updated_at=?
                   WHERE workflow_id=? AND work_unit_id=?
                     AND intervention_type='RECONCILE_PROVIDER'
                     AND state IN ('OPEN','CLAIMED')""",
                (now, now, workflow_id, work_unit_id),
            )
        return True

    def create_item(
        self,
        workflow_id: str,
        *,
        item_type: str,
        sequence: int,
        normalized_content: str,
        item_identity_key: str,
        role: str | None = None,
        voice_key: str | None = None,
        item_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        status: str = "PENDING",
        source_locator: str | None = None,
    ) -> str:
        item_id = item_id or new_id("item")
        now = utc_now()
        if status not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "AMBIGUOUS", "CANCELLED", "SKIPPED", "UNRESOLVED"}:
            raise RepositoryError(f"unsupported item status: {status}", code="VALIDATION_ERROR")
        with self.database.transaction() as con:
            if con.execute("SELECT 1 FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone() is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            try:
                con.execute(
                    """INSERT INTO work_items(
                        item_id, workflow_id, item_identity_key, item_type, sequence,
                        identity_version, source_locator, normalized_content,
                        content_hash, role, voice_key, metadata_json, status,
                        state_version, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, workflow_id, item_identity_key, item_type, sequence, "1",
                     source_locator, normalized_content, content_hash(normalized_content), role,
                     voice_key, canonical_json(redact_public_json(metadata or {})), status, 0, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"item cannot be created: {exc}") from exc
        return item_id

    def attach_imported_artifact(
        self,
        workflow_id: str,
        *,
        artifact_id: str,
        blob: Any,
        artifact_type: str,
        producer: str,
        producer_version: str,
        item_id: str | None = None,
        step_id: str | None = None,
    ) -> str:
        """Bind an already fsync'd Blob to a run-local legacy artifact."""

        now = utc_now()
        with self.database.transaction() as con:
            if con.execute("SELECT 1 FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone() is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            existing_artifact = con.execute(
                "SELECT * FROM artifacts WHERE workflow_id=? AND artifact_id=?",
                (workflow_id, artifact_id),
            ).fetchone()
            if existing_artifact is not None:
                if str(existing_artifact["sha256"]) != str(blob.sha256) or int(existing_artifact["size_bytes"]) != int(blob.size_bytes):
                    raise ConflictError("imported artifact id points to a different Blob", code="ARTIFACT_INVALID")
                return artifact_id
            blob_row = con.execute("SELECT * FROM artifact_blobs WHERE sha256=?", (blob.sha256,)).fetchone()
            if blob_row is None:
                blob_id = new_id("blob")
                con.execute(
                    """INSERT INTO artifact_blobs(
                       blob_id, sha256, size_bytes, format, storage_key, lifecycle_state,
                       verified_at, created_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,NULL)""",
                    (blob_id, blob.sha256, blob.size_bytes, blob.format, blob.storage_key, "READY", now, now),
                )
            else:
                if str(blob_row["storage_key"]) != str(blob.storage_key) or int(blob_row["size_bytes"]) != int(blob.size_bytes) or blob_row["lifecycle_state"] != "READY":
                    raise RepositoryError("content-addressed Blob fingerprint conflicts", code="ARTIFACT_INVALID")
                blob_id = str(blob_row["blob_id"])
            try:
                con.execute(
                    """INSERT INTO artifacts(
                       artifact_id, workflow_id, item_id, step_id, attempt_id, work_unit_id,
                       work_unit_segment_id, source_import_id, source_import_generation,
                       source_import_generation_id, blob_id, staging_ref, artifact_type,
                       sha256, size_bytes, format, producer, producer_version, verified,
                       verified_at, lifecycle_state, schema_version, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (artifact_id, workflow_id, item_id, step_id, None, None, None, None, None, None,
                     blob_id, None, artifact_type, blob.sha256, blob.size_bytes, blob.format,
                     producer, producer_version, 1, now, "READY", "1", now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"imported artifact cannot be bound: {exc}", code="ARTIFACT_INVALID") from exc
        return artifact_id

    def attach_export_artifact(
        self,
        workflow_id: str,
        *,
        artifact_id: str,
        blob: Any,
        artifact_type: str = "export-zip",
        producer: str = "workflow-export",
        producer_version: str = "1",
        parent_artifact_ids: list[str] | None = None,
        request_id: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> str:
        """Bind a generated delivery package and record its source artifacts."""

        now = utc_now()
        parents = [str(parent) for parent in (parent_artifact_ids or []) if str(parent)]
        with self.database.transaction() as con:
            if con.execute("SELECT 1 FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone() is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            existing = con.execute(
                "SELECT * FROM artifacts WHERE workflow_id=? AND artifact_id=?",
                (workflow_id, artifact_id),
            ).fetchone()
            if existing is not None:
                if str(existing["sha256"]) != str(blob.sha256) or int(existing["size_bytes"]) != int(blob.size_bytes):
                    raise ConflictError("export artifact id points to a different Blob", code="ARTIFACT_INVALID")
                if request_id:
                    self._append_export_event_in_transaction(
                        con,
                        workflow_id,
                        artifact_id,
                        request_id,
                        event_payload,
                    )
                return artifact_id

            blob_row = con.execute("SELECT * FROM artifact_blobs WHERE sha256=?", (blob.sha256,)).fetchone()
            if blob_row is None:
                blob_id = new_id("blob")
                con.execute(
                    """INSERT INTO artifact_blobs(
                       blob_id, sha256, size_bytes, format, storage_key, lifecycle_state,
                       verified_at, created_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,NULL)""",
                    (blob_id, blob.sha256, blob.size_bytes, blob.format, blob.storage_key, "READY", now, now),
                )
            else:
                if (
                    str(blob_row["storage_key"]) != str(blob.storage_key)
                    or int(blob_row["size_bytes"]) != int(blob.size_bytes)
                    or blob_row["lifecycle_state"] != "READY"
                ):
                    raise RepositoryError("content-addressed Blob fingerprint conflicts", code="ARTIFACT_INVALID")
                blob_id = str(blob_row["blob_id"])

            try:
                con.execute(
                    """INSERT INTO artifacts(
                       artifact_id, workflow_id, item_id, step_id, attempt_id, work_unit_id,
                       work_unit_segment_id, source_import_id, source_import_generation,
                       source_import_generation_id, blob_id, staging_ref, artifact_type,
                       sha256, size_bytes, format, producer, producer_version, verified,
                       verified_at, lifecycle_state, schema_version, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (artifact_id, workflow_id, None, None, None, None, None, None, None, None,
                     blob_id, None, artifact_type, blob.sha256, blob.size_bytes, blob.format,
                     producer, producer_version, 1, now, "READY", "1", now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"export artifact cannot be bound: {exc}", code="ARTIFACT_INVALID") from exc

            for parent_id in parents:
                parent = con.execute(
                    """SELECT artifact_id FROM artifacts
                       WHERE workflow_id=? AND artifact_id=?
                         AND lifecycle_state='READY' AND verified=1""",
                    (workflow_id, parent_id),
                ).fetchone()
                if parent is None:
                    raise RepositoryError("export parent artifact is not ready", code="ARTIFACT_INVALID")
                con.execute(
                    """INSERT OR IGNORE INTO artifact_derivations(
                       derivation_id, parent_artifact_id, child_artifact_id,
                       relation_type, derivation_version, derivation_context_hash, created_at
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (new_id("derivation"), parent_id, artifact_id, "EXPORT", "1",
                     content_hash({"workflow_id": workflow_id, "parent": parent_id, "child": artifact_id}), now),
                )
            if request_id:
                self._append_export_event_in_transaction(
                    con,
                    workflow_id,
                    artifact_id,
                    request_id,
                    event_payload,
                )
        return artifact_id

    def _append_export_event_in_transaction(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        artifact_id: str,
        request_id: str,
        event_payload: Mapping[str, Any] | None,
    ) -> None:
        payload = dict(event_payload or {})
        payload["artifact_id"] = artifact_id
        self.events.append_in_transaction(
            con,
            workflow_id,
            "WORKFLOW_EXPORTED",
            payload,
            request_id=request_id,
            actor_type="USER",
            actor_id="desktop",
        )
        snapshot = _snapshot_from_connection(con, workflow_id)
        self.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())

    def record_export_event(
        self,
        workflow_id: str,
        *,
        artifact_id: str,
        request_id: str,
        event_payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Record an export request that reused an existing immutable ZIP."""

        with self.database.transaction() as con:
            row = con.execute(
                """SELECT a.artifact_id, a.artifact_type, a.format,
                          a.lifecycle_state, a.verified,
                          b.format AS blob_format, b.lifecycle_state AS blob_lifecycle_state
                   FROM artifacts a
                   JOIN artifact_blobs b ON b.blob_id=a.blob_id
                   WHERE a.workflow_id=? AND a.artifact_id=?""",
                (workflow_id, artifact_id),
            ).fetchone()
            if (
                row is None
                or row["artifact_type"] != "export-zip"
                or row["lifecycle_state"] != "READY"
                or int(row["verified"] or 0) != 1
                or row["blob_lifecycle_state"] != "READY"
                or str(row["format"] or "").lower().lstrip(".") != "zip"
                or str(row["blob_format"] or "").lower().lstrip(".") != "zip"
            ):
                raise RepositoryError("export artifact is not ready", code="ARTIFACT_INVALID")
            self._append_export_event_in_transaction(
                con,
                workflow_id,
                artifact_id,
                request_id,
                event_payload,
            )

    def finalize_legacy_import(
        self,
        workflow_id: str,
        *,
        step_id: str,
        item_statuses: Mapping[str, str],
        blocked: bool,
        warnings: list[str] | None = None,
    ) -> WorkflowSnapshot:
        """Publish one import projection without taking over unfinished work."""

        now = utc_now()
        warnings = list(warnings or [])[:20]
        with self.database.transaction() as con:
            workflow = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            for item_id, status in item_statuses.items():
                if status not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "AMBIGUOUS", "CANCELLED", "SKIPPED", "UNRESOLVED"}:
                    raise RepositoryError(f"unsupported imported item status: {status}", code="VALIDATION_ERROR")
                con.execute(
                    "UPDATE work_items SET status=?, state_version=state_version+1, updated_at=? WHERE workflow_id=? AND item_id=?",
                    (status, now, workflow_id, item_id),
                )
            has_failed = any(status in {"FAILED", "AMBIGUOUS", "UNRESOLVED"} for status in item_statuses.values())
            has_pending = any(status in {"PENDING", "RUNNING"} for status in item_statuses.values())
            if blocked or has_pending:
                result_status, execution_state, control_state, cleanup_state, step_status = (
                    "IN_PROGRESS", "BLOCKED", "PAUSED", "NONE", "WAITING_USER"
                )
                error_code = "LEGACY_IMPORT_REQUIRES_REVIEW"
            else:
                result_status = "PARTIAL_SUCCESS" if has_failed and any(status == "SUCCEEDED" for status in item_statuses.values()) else "FAILED" if has_failed else "SUCCEEDED"
                execution_state, control_state, cleanup_state = "TERMINAL", "TERMINATED", "SUCCEEDED"
                step_status = "PERMANENT_FAILED" if has_failed else "SUCCEEDED"
                error_code = "LEGACY_IMPORT_HAS_ERRORS" if has_failed else None
            con.execute(
                """UPDATE workflow_groups SET lifecycle_state='ACTIVE', accepted_at=COALESCE(accepted_at, ?),
                   state_version=state_version+1, updated_at=? WHERE workflow_group_id=?""",
                (now, now, workflow["workflow_group_id"]),
            )
            con.execute(
                """UPDATE workflow_steps SET status=?, error_code=?, error_details_json=?, finished_at=?,
                   state_version=state_version+1 WHERE workflow_id=? AND step_id=?""",
                (step_status, error_code, canonical_json({"warnings": warnings}), None if blocked else now, workflow_id, step_id),
            )
            con.execute(
                """UPDATE workflows SET result_status=?, execution_state=?, control_state=?, cleanup_state=?,
                   status='ACTIVE', current_step_id=?, last_error_code=?, last_error_message=?,
                   accepted_at=COALESCE(accepted_at, ?), finished_at=?, state_version=state_version+1, updated_at=?
                   WHERE workflow_id=?""",
                (result_status, execution_state, control_state, cleanup_state, step_id, error_code,
                 "; ".join(warnings)[:2000] if warnings else None, now, None if blocked else now, now, workflow_id),
            )
            self.events.append_in_transaction(
                con, workflow_id, "LEGACY_IMPORT_PROJECTED",
                {"item_count": len(item_statuses), "blocked": blocked, "warnings": warnings},
                actor_type="SYSTEM", actor_id="legacy-importer", step_id=step_id,
            )
            snapshot = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
            return snapshot

    def create_step(
        self,
        workflow_id: str,
        *,
        step_key: str,
        step_type: str,
        scope: str = "workflow",
        item_id: str | None = None,
        step_id: str | None = None,
    ) -> str:
        step_id = step_id or new_id("step")
        now = utc_now()
        with self.database.transaction() as con:
            try:
                con.execute(
                    """INSERT INTO workflow_steps(
                        step_id, workflow_id, scope, item_id, step_key, step_type,
                        step_definition_version, dependency_keys_json, status,
                        current_attempt_id, attempt_count, state_version,
                        aggregate_operation_key, operation_key_type, input_hash,
                        output_reference_json, retry_after, error_code,
                        error_details_json, started_at, finished_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (step_id, workflow_id, scope, item_id, step_key, step_type, "1", "[]",
                     "PENDING", None, 0, 0, None, None, None, None, None, None, None, None, None),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"step cannot be created: {exc}") from exc
        return step_id

    def create_assignment(
        self,
        workflow_id: str,
        *,
        step_id: str,
        item_id: str,
        delivery_unit_key: str,
        assignment_id: str | None = None,
        plan_hash: str = "plan",
    ) -> str:
        assignment_id = assignment_id or new_id("assignment")
        with self.database.transaction() as con:
            try:
                con.execute(
                    """INSERT INTO work_item_assignments(
                        assignment_id, workflow_id, step_id, item_id, delivery_unit_key,
                        assignment_revision, state, supersedes_assignment_id, plan_hash,
                        state_version, created_at, superseded_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (assignment_id, workflow_id, step_id, item_id, delivery_unit_key, 0,
                     "ACTIVE", None, plan_hash, 0, utc_now(), None),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"assignment cannot be created: {exc}") from exc
        return assignment_id

    def create_attempt(
        self,
        workflow_id: str,
        step_id: str,
        *,
        attempt_kind: str = "EXECUTE",
        expected_step_state_version: int | None = None,
        attempt_id: str | None = None,
    ) -> str:
        attempt_id = attempt_id or new_id("attempt")
        now = utc_now()
        with self.database.transaction() as con:
            step = con.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                (workflow_id, step_id),
            ).fetchone()
            if step is None:
                raise NotFoundError(f"step does not exist: {step_id}")
            if expected_step_state_version is not None:
                require_expected(int(step["state_version"]), expected_step_state_version)
            attempt_seq = int(con.execute(
                "SELECT COALESCE(MAX(attempt_seq),0)+1 FROM step_attempts WHERE workflow_id=? AND step_id=?",
                (workflow_id, step_id),
            ).fetchone()[0])
            execute_no = None
            if attempt_kind == "EXECUTE":
                execute_no = int(con.execute(
                    "SELECT COALESCE(MAX(execute_attempt_no),0)+1 FROM step_attempts WHERE workflow_id=? AND step_id=? AND attempt_kind='EXECUTE'",
                    (workflow_id, step_id),
                ).fetchone()[0])
            try:
                con.execute(
                    """INSERT INTO step_attempts(
                        attempt_id, workflow_id, step_id, attempt_kind, attempt_seq,
                        execute_attempt_no, status, result_status, error_code,
                        error_details_json, lease_fencing_token, state_version,
                        started_at, heartbeat_at, finished_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (attempt_id, workflow_id, step_id, attempt_kind, attempt_seq, execute_no,
                     "CREATED", "IN_PROGRESS", None, None, None, 0, now, None, None),
                )
                updated = con.execute(
                    """UPDATE workflow_steps SET current_attempt_id=?,
                        attempt_count=attempt_count+1, state_version=state_version+1
                        WHERE workflow_id=? AND step_id=?
                          AND (? IS NULL OR state_version=?)""",
                    (attempt_id, workflow_id, step_id,
                     expected_step_state_version, expected_step_state_version),
                )
                if updated.rowcount != 1:
                    raise ConflictError("step changed while creating attempt")
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"attempt cannot be created: {exc}") from exc
        return attempt_id

    def get_step(self, workflow_id: str, step_id: str) -> dict[str, Any]:
        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                (workflow_id, step_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"step does not exist: {step_id}")
            return dict(row)

    def list_items(self, workflow_id: str) -> list[dict[str, Any]]:
        with self.database.read_transaction() as con:
            if con.execute("SELECT 1 FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone() is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            return [dict(row) for row in con.execute(
                "SELECT * FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id",
                (workflow_id,),
            ).fetchall()]

    def list_workflows(self, *, limit: int = 100) -> list[WorkflowSnapshot]:
        """Return bounded, persisted workflow snapshots for the local history UI."""

        limit = min(max(1, int(limit)), 500)
        with self.database.read_transaction() as con:
            rows = con.execute(
                "SELECT workflow_id FROM workflows ORDER BY updated_at DESC, workflow_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_snapshot_from_connection(con, str(row["workflow_id"])) for row in rows]

    def _workflow_delete_block_reason(
        self,
        con: sqlite3.Connection,
        snapshot: WorkflowSnapshot,
        *,
        allow_unresolved: bool = False,
    ) -> str | None:
        """Return why a workflow cannot be physically removed, if any.

        Deletion is intentionally narrower than hiding a terminal workflow.
        The database keeps provider/external facts as the recovery authority,
        so an unfinished run is removable only when no durable fact still
        says that an external operation may have crossed its side-effect
        boundary.
        """

        if snapshot.status == "CLOSED" or snapshot.execution_state == "TERMINAL":
            return "终态任务请使用归档"
        terminating = snapshot.control_state == "TERMINATING"
        if con.execute(
            "SELECT 1 FROM workflows WHERE parent_workflow_id=? LIMIT 1",
            (snapshot.workflow_id,),
        ).fetchone() is not None:
            return "任务存在派生运行，暂不能删除"

        # A user-confirmed history deletion is an explicit local purge.  It
        # may discard unresolved provider/external projections, while the
        # deletion path still preserves detached external mapping facts and
        # archives the append-only intent journal after the DB commit.
        if allow_unresolved:
            return None

        for row in con.execute(
            "SELECT lease_until FROM workflow_leases "
            "WHERE workflow_id=? AND state='ACTIVE'",
            (snapshot.workflow_id,),
        ).fetchall():
            if not _is_expired(row["lease_until"]):
                return "任务正在结束，请稍后再试" if terminating else "任务仍被执行器占用，请稍后再试"

        if con.execute(
            "SELECT 1 FROM user_interventions "
            "WHERE workflow_id=? AND state IN ('OPEN','CLAIMED') LIMIT 1",
            (snapshot.workflow_id,),
        ).fetchone() is not None:
            return "任务存在待处理核验事项，必须先完成核验"

        if con.execute(
            "SELECT 1 FROM side_effect_intents "
            "WHERE workflow_id=? AND state NOT IN ('ARCHIVED','REJECTED','ABORTED') LIMIT 1",
            (snapshot.workflow_id,),
        ).fetchone() is not None:
            return "任务存在未决外部副作用，必须先完成核验"

        if con.execute(
            """SELECT 1
               FROM work_units u
               LEFT JOIN provider_submissions p
                 ON p.workflow_group_id=u.workflow_group_id
                AND p.provider_submission_id=u.provider_submission_id
               WHERE u.workflow_id=?
                 AND (
                     u.side_effect_state NOT IN ('NOT_STARTED','REJECTED')
                     OR (p.provider_submission_id IS NOT NULL
                         AND p.side_effect_state NOT IN ('NOT_STARTED','REJECTED'))
                 )
               LIMIT 1""",
            (snapshot.workflow_id,),
        ).fetchone() is not None:
            return "任务存在未决 Provider 提交，必须先完成核验"

        # The Full profile may have a second external-record graph.  Do not
        # guess whether its rows are safe to discard: any row tied directly to
        # this run remains an explicit deletion guard.
        if _table_exists(con, "external_operations") and con.execute(
            "SELECT 1 FROM external_operations WHERE workflow_id=? LIMIT 1",
            (snapshot.workflow_id,),
        ).fetchone() is not None:
            return "任务存在外部系统操作事实，必须先完成核验"
        if _table_exists(con, "external_records") and con.execute(
            """SELECT 1 FROM external_records
               WHERE local_workflow_id=?
                  OR local_item_id IN (
                      SELECT item_id FROM work_items WHERE workflow_id=?
                  )
               LIMIT 1""",
            (snapshot.workflow_id, snapshot.workflow_id),
        ).fetchone() is not None:
            return "任务存在外部记录映射，必须先完成核验"
        if _table_exists(con, "external_record_bindings") and con.execute(
            "SELECT 1 FROM external_record_bindings WHERE workflow_id=? LIMIT 1",
            (snapshot.workflow_id,),
        ).fetchone() is not None:
            return "任务存在外部记录绑定，必须先完成核验"
        return None

    def list_history_records(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return a safe, bounded history projection for the desktop UI.

        The projection is derived from SQLite workflow/item/Artifact facts.  It
        deliberately omits storage keys, staging paths and raw provider
        receipts; the UI must use an Artifact ticket for bytes.
        """

        limit = min(max(1, int(limit)), 500)
        records: list[dict[str, Any]] = []
        with self.database.read_transaction() as con:
            workflow_rows = con.execute(
                """SELECT * FROM workflows
                   WHERE status <> 'CLOSED'
                   ORDER BY updated_at DESC, workflow_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            for row in workflow_rows:
                workflow_id = str(row["workflow_id"])
                snapshot = _snapshot_from_connection(con, workflow_id)
                try:
                    configuration = json.loads(str(row["configuration_snapshot"] or "{}"))
                except (TypeError, json.JSONDecodeError):
                    configuration = {}
                if not isinstance(configuration, Mapping):
                    configuration = {}

                source_filename = str(configuration.get("source_filename") or "").strip()
                if not source_filename:
                    source_row = con.execute(
                        """SELECT error_details_json FROM source_imports
                           WHERE workflow_id=? ORDER BY updated_at DESC LIMIT 1""",
                        (workflow_id,),
                    ).fetchone()
                    if source_row is not None:
                        try:
                            details = json.loads(str(source_row["error_details_json"] or "{}"))
                            metadata = details.get("metadata") if isinstance(details, Mapping) else {}
                            source_filename = str((metadata or {}).get("filename") or "").strip()
                        except (TypeError, json.JSONDecodeError):
                            source_filename = ""
                source_filename = _safe_source_filename(source_filename)

                artifact_rows = con.execute(
                    """SELECT artifact_id, item_id, artifact_type, format, size_bytes,
                              sequence, sha256
                       FROM (
                           SELECT a.artifact_id, a.item_id, a.artifact_type, a.format,
                                  a.size_bytes, a.sha256, wi.sequence,
                                  a.lifecycle_state AS artifact_lifecycle_state,
                                  a.verified AS artifact_verified, b.format AS blob_format,
                                  b.sha256 AS blob_sha256, b.size_bytes AS blob_size_bytes,
                                  b.lifecycle_state AS blob_lifecycle_state, wi.status AS item_status,
                                  a.created_at,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY a.item_id
                                      ORDER BY a.created_at DESC, a.artifact_id DESC
                                  ) AS row_number
                           FROM artifacts a
                           JOIN artifact_blobs b ON b.blob_id=a.blob_id
                           JOIN work_items wi ON wi.workflow_id=a.workflow_id AND wi.item_id=a.item_id
                           WHERE a.workflow_id=? AND a.artifact_type='tts-segment'
                       )
                       WHERE row_number=1
                         AND artifact_lifecycle_state='READY' AND artifact_verified=1
                         AND blob_lifecycle_state='READY' AND item_status='SUCCEEDED'
                         AND LOWER(TRIM(format))='mp3'
                         AND LOWER(TRIM(blob_format))='mp3'
                         AND sha256=blob_sha256
                         AND size_bytes=blob_size_bytes
                       ORDER BY sequence, item_id""",
                    (workflow_id,),
                ).fetchall()
                item_rows = con.execute(
                    """SELECT item_id, item_identity_key, item_type, status, normalized_content
                       FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id""",
                    (workflow_id,),
                ).fetchall()
                # History may be rendered without hydrating a workspace, so
                # it must apply the same current-scope check as the workspace
                # projection.  A latest valid ZIP from an earlier partial run
                # is not a current delivery once a newer segment is present.
                expected_full_zip_id = None
                if artifact_rows:
                    export_basis = [
                        {
                            "item_id": str(artifact["item_id"]),
                            "sequence": int(artifact["sequence"]),
                            "artifact_id": str(artifact["artifact_id"]),
                            "sha256": str(artifact["sha256"]),
                        }
                        for artifact in artifact_rows
                        if artifact["item_id"] is not None
                    ]
                    if export_basis:
                        export_hash = content_hash({
                            "workflow_id": workflow_id,
                            "segments": export_basis,
                            "requested_item_ids": None,
                            "archive_layout": ARCHIVE_LAYOUT_VERSION,
                        })
                        expected_full_zip_id = f"artifact-export-{export_hash[:32]}"
                zip_row = None
                if expected_full_zip_id:
                    zip_row = con.execute(
                        """SELECT a.artifact_id FROM artifacts a
                           JOIN artifact_blobs b ON b.blob_id=a.blob_id
                           WHERE a.workflow_id=? AND a.artifact_id=?
                             AND a.lifecycle_state='READY' AND a.verified=1
                             AND a.artifact_type='export-zip'
                             AND b.lifecycle_state='READY'
                             AND LOWER(TRIM(a.format))='zip'
                             AND LOWER(TRIM(b.format))='zip'
                             AND a.sha256=b.sha256
                             AND a.size_bytes=b.size_bytes""",
                        (workflow_id, expected_full_zip_id),
                    ).fetchone()
                # The history action is an explicit, user-confirmed local
                # purge, so unresolved reconciliation facts must not leave a
                # stale disabled button.  Terminal runs still use archive and
                # derived runs remain protected by the repository guard.
                delete_block_reason = self._workflow_delete_block_reason(
                    con,
                    snapshot,
                    allow_unresolved=True,
                )
                terminal = snapshot.execution_state == "TERMINAL"
                available_item_ids = {str(item["item_id"]) for item in artifact_rows if item["item_id"]}
                failed_items = []
                failed_count = 0
                cancelled_count = 0
                skipped_count = 0
                for item in item_rows:
                    item_id = str(item["item_id"])
                    status = str(item["status"])
                    if status == "CANCELLED":
                        cancelled_count += 1
                        continue
                    if status == "SKIPPED":
                        skipped_count += 1
                        continue
                    if status in {"FAILED", "AMBIGUOUS", "UNRESOLVED"} or (
                        snapshot.execution_state == "TERMINAL" and item_id not in available_item_ids
                    ):
                        failed_count += 1
                        failed_items.append({
                            "item_id": item_id,
                            "id": str(item["item_identity_key"]),
                            "doc_type": str(item["item_type"]),
                            "status": status,
                            "text_preview": str(item["normalized_content"] or "")[:160],
                        })
                first_format = str(artifact_rows[0]["format"] or configuration.get("format") or "mp3") if artifact_rows else str(configuration.get("format") or "mp3")
                records.append({
                    "id": workflow_id,
                    "workflow_id": workflow_id,
                    "source_filename": source_filename,
                    "available_files": len(artifact_rows),
                    "completed": len(available_item_ids),
                    "failed": failed_count,
                    "cancelled": cancelled_count,
                    "skipped": skipped_count,
                    "total": len(item_rows),
                    "pending": max(0, len(item_rows) - len(available_item_ids) - failed_count - cancelled_count - skipped_count),
                    "format": first_format,
                    "generation_mode": str(configuration.get("generation_mode") or "composite_cut"),
                    "preview": bool(configuration.get("preview", False)),
                    "zip_available": zip_row is not None,
                    "zip_artifact_id": str(zip_row["artifact_id"]) if zip_row is not None else None,
                    "failed_items": failed_items[:500],
                    "status": snapshot.status,
                    "result_status": snapshot.result_status,
                    "execution_state": snapshot.execution_state,
                    "control_state": snapshot.control_state,
                    "state_version": snapshot.state_version,
                    "can_delete": not terminal and delete_block_reason is None,
                    "delete_reason": (
                        delete_block_reason
                        or ("终态任务请使用归档" if terminal else None)
                    ),
                    "created_at": str(row["created_at"]),
                    "completed_at": row["finished_at"] or row["updated_at"],
                    "updated_at": snapshot.updated_at,
                })
        return records

    def archive_workflow(
        self,
        workflow_id: str,
        *,
        expected_state_version: int,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> WorkflowSnapshot:
        """Hide a terminal run from the history projection without deleting facts."""

        now = utc_now()
        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            require_expected(snapshot.state_version, expected_state_version)
            if snapshot.status == "CLOSED":
                return snapshot
            if snapshot.execution_state != "TERMINAL" or snapshot.result_status not in {
                "SUCCEEDED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED",
            }:
                raise ConflictError("only a terminal workflow can be archived")
            if snapshot.control_state != "TERMINATED":
                raise ConflictError("workflow control state is not terminal")
            updated = con.execute(
                """UPDATE workflows SET status='CLOSED', state_version=state_version+1,
                   updated_at=? WHERE workflow_id=? AND state_version=? AND status <> 'CLOSED'""",
                (now, workflow_id, expected_state_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("workflow changed while archiving")
            self.events.append_in_transaction(
                con,
                workflow_id,
                "WORKFLOW_ARCHIVED",
                {"reason": reason} if reason else {},
                request_id=request_id,
                actor_type="USER",
                actor_id="desktop",
            )
            archived = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, archived.as_dict())
            return archived

    def delete_workflow(
        self,
        workflow_id: str,
        *,
        expected_state_version: int,
        request_id: str | None = None,
        response: Mapping[str, Any] | None = None,
        allow_unresolved: bool = False,
    ) -> dict[str, Any]:
        """Physically remove an unfinished workflow and its local facts.

        This is deliberately an atomic database operation.  The idempotency
        response is written in the same transaction as the delete and is not
        linked back to the workflow, so a lost HTTP response can be replayed
        after the workflow row has disappeared.  Files are returned as a
        post-commit cleanup plan; the garbage collector performs the final
        Blob unlink against a fresh reference scan.  ``allow_unresolved`` is
        reserved for the explicit history-delete command: it removes local
        reconciliation/provider projections and leaves any external mapping
        detached rather than pretending that a remote submission was undone.
        """

        if request_id is not None and response is None:
            raise RepositoryError(
                "delete idempotency completion requires a response",
                code="PERSISTENCE_ERROR",
            )

        staging_keys: set[str] = set()
        candidate_blobs: dict[str, str] = {}
        deleted_blob_storage_keys: set[str] = set()
        deleted_intents: list[dict[str, str]] = []
        external_operation_ids: set[str] = set()
        external_mapping_ids: set[str] = set()
        now = utc_now()
        try:
            with self.database.transaction() as con:
                snapshot = _snapshot_from_connection(con, workflow_id)
                require_expected(snapshot.state_version, expected_state_version)
                block_reason = self._workflow_delete_block_reason(
                    con,
                    snapshot,
                    allow_unresolved=allow_unresolved,
                )
                if block_reason:
                    raise ConflictError(
                        block_reason,
                        code=(
                            "ARCHIVE_REQUIRED"
                            if snapshot.execution_state == "TERMINAL"
                            else "DELETE_BLOCKED"
                        ),
                        details={"workflow_id": workflow_id},
                    )

                workflow_row = con.execute(
                    "SELECT workflow_group_id, parent_workflow_id FROM workflows WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()
                if workflow_row is None:
                    raise NotFoundError(f"workflow does not exist: {workflow_id}")
                group_id = str(workflow_row["workflow_group_id"])
                has_other_workflows = con.execute(
                    "SELECT 1 FROM workflows WHERE workflow_group_id=? AND workflow_id<>? LIMIT 1",
                    (group_id, workflow_id),
                ).fetchone() is not None

                if allow_unresolved:
                    deleted_intents = [
                        {
                            "operation_namespace": str(row["operation_namespace"]),
                            "operation_key": str(row["operation_key"]),
                            "payload_hash": str(row["payload_hash"]),
                            "intent_id": str(row["intent_id"]),
                        }
                        for row in con.execute(
                            """SELECT intent_id, operation_namespace, operation_key, payload_hash
                               FROM side_effect_intents WHERE workflow_id=?""",
                            (workflow_id,),
                        ).fetchall()
                    ]

                    if _table_exists(con, "external_operations"):
                        for row in con.execute(
                            """SELECT external_operation_id, external_record_mapping_id
                               FROM external_operations WHERE workflow_id=?""",
                            (workflow_id,),
                        ).fetchall():
                            external_operation_ids.add(str(row["external_operation_id"]))
                            external_mapping_ids.add(str(row["external_record_mapping_id"]))
                    if _table_exists(con, "external_record_bindings"):
                        for row in con.execute(
                            """SELECT external_operation_id, external_record_mapping_id
                               FROM external_record_bindings WHERE workflow_id=?""",
                            (workflow_id,),
                        ).fetchall():
                            if row["external_operation_id"]:
                                external_operation_ids.add(str(row["external_operation_id"]))
                            external_mapping_ids.add(str(row["external_record_mapping_id"]))
                    if _table_exists(con, "external_records"):
                        for row in con.execute(
                            """SELECT external_record_mapping_id
                               FROM external_records
                               WHERE local_workflow_id=?
                                  OR local_item_id IN (
                                      SELECT item_id FROM work_items WHERE workflow_id=?
                                  )""",
                            (workflow_id, workflow_id),
                        ).fetchall():
                            external_mapping_ids.add(str(row["external_record_mapping_id"]))

                # In explicit history-delete mode, mappings owned by this
                # workflow will be detached below.  They must not keep the
                # workflow group alive by themselves; mappings belonging to
                # another run still do.
                if _table_exists(con, "external_records"):
                    if allow_unresolved and external_mapping_ids:
                        mapping_placeholders = ",".join("?" for _ in external_mapping_ids)
                        has_external_group_reference = con.execute(
                            f"""SELECT 1 FROM external_records
                                WHERE current_workflow_group_id=?
                                  AND external_record_mapping_id NOT IN ({mapping_placeholders})
                                LIMIT 1""",
                            (group_id, *sorted(external_mapping_ids)),
                        ).fetchone() is not None
                    else:
                        has_external_group_reference = con.execute(
                            "SELECT 1 FROM external_records WHERE current_workflow_group_id=? LIMIT 1",
                            (group_id,),
                        ).fetchone() is not None
                else:
                    has_external_group_reference = False
                delete_group_facts = not has_other_workflows and not has_external_group_reference

                if request_id is not None:
                    idem_row = con.execute(
                        "SELECT workflow_id, response_json FROM workflow_idempotency_keys WHERE idempotency_id=?",
                        (request_id,),
                    ).fetchone()
                    if idem_row is None:
                        raise NotFoundError(f"idempotency key does not exist: {request_id}")
                    if idem_row["response_json"] is not None:
                        raise ConflictError(
                            "delete idempotency request has already completed",
                            code="IDEMPOTENCY_IN_PROGRESS",
                        )
                    if idem_row["workflow_id"] is not None:
                        raise ConflictError(
                            "delete idempotency reservation is bound to the workflow",
                            code="IDEMPOTENCY_CONFLICT",
                        )

                artifact_rows = con.execute(
                    """SELECT DISTINCT a.blob_id, b.storage_key, a.staging_ref
                       FROM artifacts a
                       LEFT JOIN artifact_blobs b ON b.blob_id=a.blob_id
                       WHERE a.workflow_id=?""",
                    (workflow_id,),
                ).fetchall()
                for artifact in artifact_rows:
                    staging_ref = str(artifact["staging_ref"] or "").strip()
                    if staging_ref:
                        staging_keys.add(staging_ref)
                    blob_id = str(artifact["blob_id"] or "").strip()
                    storage_key = str(artifact["storage_key"] or "").strip()
                    if blob_id and storage_key:
                        candidate_blobs[blob_id] = storage_key

                source_import_columns = _table_columns(con, "source_imports")
                if "staging_key" in source_import_columns:
                    staging_keys.update(
                        str(row["staging_key"])
                        for row in con.execute(
                            "SELECT staging_key FROM source_imports WHERE workflow_id=? AND staging_key IS NOT NULL",
                            (workflow_id,),
                        ).fetchall()
                        if str(row["staging_key"] or "").strip()
                    )
                generation_columns = _table_columns(con, "source_import_generations")
                if "staging_key" in generation_columns:
                    staging_keys.update(
                        str(row["staging_key"])
                        for row in con.execute(
                            "SELECT staging_key FROM source_import_generations WHERE workflow_id=?",
                            (workflow_id,),
                        ).fetchall()
                        if str(row["staging_key"] or "").strip()
                    )

                # Break the source-import/artifact cycle before deleting either
                # side.  Both current (generation projection) and older local
                # databases are supported here because users may upgrade in
                # place without recreating their data directory.
                con.execute(
                    "UPDATE workflows SET source_artifact_id=NULL WHERE workflow_id=?",
                    (workflow_id,),
                )
                if "current_artifact_id" in source_import_columns:
                    con.execute(
                        "UPDATE source_imports SET current_artifact_id=NULL WHERE workflow_id=?",
                        (workflow_id,),
                    )
                elif "source_artifact_id" in source_import_columns:
                    con.execute(
                        "UPDATE source_imports SET source_artifact_id=NULL WHERE workflow_id=?",
                        (workflow_id,),
                    )
                if "source_artifact_id" in generation_columns:
                    con.execute(
                        "UPDATE source_import_generations SET source_artifact_id=NULL WHERE workflow_id=?",
                        (workflow_id,),
                    )

                # Explicit history deletion is allowed to remove unresolved
                # local provider/external projections.  External records are
                # deliberately retained as detached business facts so a
                # later run can still discover an already-created remote
                # record; operation/binding rows that point at this workflow
                # must be removed before their local workflow FK targets.
                if allow_unresolved and _table_exists(con, "external_operations"):
                    if external_operation_ids and _table_exists(con, "external_operation_targets"):
                        operation_placeholders = ",".join("?" for _ in external_operation_ids)
                        con.execute(
                            f"DELETE FROM external_operation_targets WHERE external_operation_id IN ({operation_placeholders})",
                            tuple(sorted(external_operation_ids)),
                        )
                    if _table_exists(con, "external_record_bindings"):
                        if external_operation_ids:
                            operation_placeholders = ",".join("?" for _ in external_operation_ids)
                            con.execute(
                                f"""DELETE FROM external_record_bindings
                                    WHERE workflow_id=? OR external_operation_id IN ({operation_placeholders})""",
                                (workflow_id, *sorted(external_operation_ids)),
                            )
                        else:
                            con.execute(
                                "DELETE FROM external_record_bindings WHERE workflow_id=?",
                                (workflow_id,),
                            )
                    con.execute(
                        "DELETE FROM external_operations WHERE workflow_id=?",
                        (workflow_id,),
                    )
                    if external_mapping_ids and _table_exists(con, "external_records"):
                        mapping_placeholders = ",".join("?" for _ in external_mapping_ids)
                        mapping_params = tuple(sorted(external_mapping_ids))
                        con.execute(
                            f"""UPDATE external_records
                                SET local_workflow_id=CASE WHEN local_workflow_id=? THEN NULL ELSE local_workflow_id END,
                                    local_item_id=CASE WHEN local_item_id IN (
                                        SELECT item_id FROM work_items WHERE workflow_id=?
                                    ) THEN NULL ELSE local_item_id END,
                                    current_workflow_group_id=CASE
                                        WHEN ? AND current_workflow_group_id=? THEN NULL
                                        ELSE current_workflow_group_id END,
                                    current_operation_key=CASE
                                        WHEN current_operation_key IS NOT NULL AND NOT EXISTS (
                                            SELECT 1 FROM external_operations remaining
                                            WHERE remaining.external_record_mapping_id=external_records.external_record_mapping_id
                                              AND remaining.external_operation_key=external_records.current_operation_key
                                        ) THEN NULL
                                        ELSE current_operation_key END,
                                    updated_at=?
                                WHERE external_record_mapping_id IN ({mapping_placeholders})""",
                            (
                                workflow_id,
                                workflow_id,
                                1 if delete_group_facts else 0,
                                group_id,
                                now,
                                *mapping_params,
                            ),
                        )
                        if _table_exists(con, "external_record_leases"):
                            con.execute(
                                f"""UPDATE external_record_leases
                                    SET state='RELEASED', heartbeat_at=?
                                    WHERE external_record_mapping_id IN ({mapping_placeholders})
                                      AND NOT EXISTS (
                                          SELECT 1 FROM external_operations remaining
                                          WHERE remaining.external_record_mapping_id=external_record_leases.external_record_mapping_id
                                      )
                                      AND NOT EXISTS (
                                          SELECT 1 FROM external_record_bindings remaining
                                          WHERE remaining.external_record_mapping_id=external_record_leases.external_record_mapping_id
                                      )""",
                                (now, *mapping_params),
                            )

                # Events/snapshots and recovery facts must go before the
                # workflow-owned execution rows because their foreign keys are
                # intentionally RESTRICT rather than cascading.
                con.execute("DELETE FROM snapshot_anchors WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM workflow_snapshots WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM workflow_events WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM workflow_event_streams WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM reconcile_targets WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM reconcile_evidence WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM user_interventions WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM side_effect_intents WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM provider_receipt_bindings WHERE workflow_id=?", (workflow_id,))

                # Derivations may point across workflows (for example a rerun
                # reusing a source Blob), so remove only edges touching the
                # target before removing its Artifact rows.
                con.execute(
                    """DELETE FROM artifact_derivations
                       WHERE parent_artifact_id IN (SELECT artifact_id FROM artifacts WHERE workflow_id=?)
                          OR child_artifact_id IN (SELECT artifact_id FROM artifacts WHERE workflow_id=?)""",
                    (workflow_id, workflow_id),
                )
                con.execute("DELETE FROM artifacts WHERE workflow_id=?", (workflow_id,))
                if generation_columns:
                    con.execute("DELETE FROM source_import_generations WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM source_imports WHERE workflow_id=?", (workflow_id,))

                # Work-unit children have to be removed in dependency order.
                con.execute(
                    "DELETE FROM work_unit_segments WHERE work_unit_id IN (SELECT work_unit_id FROM work_units WHERE workflow_id=?)",
                    (workflow_id,),
                )
                con.execute("DELETE FROM work_unit_items WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM work_unit_attempts WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM workflow_step_dependencies WHERE workflow_id=?", (workflow_id,))
                con.execute(
                    "UPDATE work_item_assignments SET supersedes_assignment_id=NULL WHERE workflow_id=?",
                    (workflow_id,),
                )
                con.execute("DELETE FROM work_item_assignments WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM work_units WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM step_attempts WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM workflow_steps WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM work_items WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM workflow_idempotency_keys WHERE workflow_id=?", (workflow_id,))
                con.execute("DELETE FROM workflow_leases WHERE workflow_id=?", (workflow_id,))

                # A group root is a deferred composite foreign key.  Clear it
                # before deleting the root workflow even when the group itself
                # is retained by an external mapping.
                con.execute(
                    "UPDATE workflow_groups SET root_workflow_id=NULL WHERE root_workflow_id=?",
                    (workflow_id,),
                )

                if request_id is not None:
                    updated = con.execute(
                        """UPDATE workflow_idempotency_keys
                           SET response_status=?, response_json=?, workflow_id=NULL
                           WHERE idempotency_id=? AND response_json IS NULL""",
                        (200, canonical_json(dict(response or {})), request_id),
                    )
                    if updated.rowcount != 1:
                        raise ConflictError(
                            "delete idempotency reservation changed while deleting",
                            code="IDEMPOTENCY_IN_PROGRESS",
                        )

                deleted = con.execute(
                    "DELETE FROM workflows WHERE workflow_id=? AND state_version=?",
                    (workflow_id, expected_state_version),
                )
                if deleted.rowcount != 1:
                    raise ConflictError("workflow changed while deleting")

                # Group-level provider facts are shared by reruns.  Remove
                # them only when this was the final local run and no external
                # record still points at the group.
                if delete_group_facts:
                    con.execute(
                        """DELETE FROM provider_receipt_bindings
                           WHERE receipt_id IN (
                               SELECT receipt_id FROM provider_receipts WHERE workflow_group_id=?
                           )""",
                        (group_id,),
                    )
                    con.execute(
                        """DELETE FROM provider_receipt_identifiers
                           WHERE receipt_id IN (
                               SELECT receipt_id FROM provider_receipts WHERE workflow_group_id=?
                           )""",
                        (group_id,),
                    )
                    con.execute("DELETE FROM provider_receipts WHERE workflow_group_id=?", (group_id,))
                    con.execute("DELETE FROM provider_submissions WHERE workflow_group_id=?", (group_id,))
                    con.execute("DELETE FROM provider_sessions WHERE workflow_group_id=?", (group_id,))
                    con.execute("DELETE FROM retry_budgets WHERE workflow_group_id=?", (group_id,))
                    con.execute("DELETE FROM workflow_groups WHERE workflow_group_id=?", (group_id,))

                for blob_id, storage_key in candidate_blobs.items():
                    if con.execute(
                        "SELECT 1 FROM artifacts WHERE blob_id=? LIMIT 1",
                        (blob_id,),
                    ).fetchone() is not None:
                        continue
                    deleted_blob = con.execute(
                        "DELETE FROM artifact_blobs WHERE blob_id=?",
                        (blob_id,),
                    )
                    if deleted_blob.rowcount:
                        deleted_blob_storage_keys.add(storage_key)
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(
                "workflow data could not be deleted safely",
                code="PERSISTENCE_ERROR",
            ) from exc

        if allow_unresolved:
            # The database delete has committed.  Mark the append-only journal
            # entries as intentionally archived after the commit so a normal
            # recovery scan does not resurrect a task the user explicitly
            # removed.  A journal write failure leaves the live entry for the
            # recovery verifier to surface rather than claiming it is safe.
            for intent in deleted_intents:
                try:
                    self.intent_log.mark(
                        operation_namespace=intent["operation_namespace"],
                        operation_key=intent["operation_key"],
                        payload_hash=intent["payload_hash"],
                        intent_id=intent["intent_id"],
                        state="ARCHIVED",
                    )
                except Exception:
                    pass

        if request_id is not None:
            self._idempotency_forget(request_id)
        return {
            "workflow_id": workflow_id,
            "staging_keys": sorted(staging_keys),
            "blob_storage_keys": sorted(deleted_blob_storage_keys),
            "deleted_at": now,
        }

    def list_artifacts(self, workflow_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return safe artifact metadata; storage keys remain an internal fact."""

        limit = min(max(1, int(limit)), 2000)
        with self.database.read_transaction() as con:
            if con.execute("SELECT 1 FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone() is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            rows = con.execute(
                """SELECT artifact_id, workflow_id, item_id, step_id, work_unit_id,
                          artifact_type, sha256, size_bytes, format, producer,
                          producer_version, verified, lifecycle_state, created_at, updated_at
                   FROM artifacts
                   WHERE workflow_id=?
                   ORDER BY created_at, artifact_id
                   LIMIT ?""",
                (workflow_id, limit),
            ).fetchall()
            return [
                {
                    **{key: row[key] for key in (
                        "artifact_id", "workflow_id", "item_id", "step_id", "work_unit_id",
                        "artifact_type", "sha256", "size_bytes", "format", "producer",
                        "producer_version", "lifecycle_state", "created_at", "updated_at",
                    )},
                    "verified": bool(row["verified"]),
                }
                for row in rows
            ]

    def list_verified_tts_segments(self, workflow_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        """Return the ordered, latest verified segment artifacts for export."""

        limit = min(max(1, int(limit)), 2000)
        with self.database.read_transaction() as con:
            if con.execute("SELECT 1 FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone() is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            rows = con.execute(
                """SELECT item_id, sequence, item_identity_key, artifact_id, sha256,
                          size_bytes, format, storage_key
                   FROM (
                       SELECT a.item_id, i.sequence, i.item_identity_key, i.status AS item_status, a.artifact_id,
                              a.sha256, a.size_bytes, a.format, b.format AS blob_format,
                              b.sha256 AS blob_sha256, b.size_bytes AS blob_size_bytes,
                              b.storage_key, a.lifecycle_state AS artifact_lifecycle_state,
                              a.verified AS artifact_verified,
                              b.lifecycle_state AS blob_lifecycle_state,
                              ROW_NUMBER() OVER (
                                  PARTITION BY a.item_id
                                  ORDER BY a.created_at DESC, a.artifact_id DESC
                              ) AS row_number
                       FROM artifacts a
                       JOIN artifact_blobs b ON b.blob_id=a.blob_id
                       JOIN work_items i ON i.workflow_id=a.workflow_id AND i.item_id=a.item_id
                       WHERE a.workflow_id=? AND a.artifact_type='tts-segment'
                   )
                   WHERE row_number=1
                     AND artifact_lifecycle_state='READY' AND artifact_verified=1
                     AND blob_lifecycle_state='READY' AND item_status='SUCCEEDED'
                     AND LOWER(TRIM(format))='mp3'
                     AND LOWER(TRIM(blob_format))='mp3'
                     AND sha256=blob_sha256
                     AND size_bytes=blob_size_bytes
                   ORDER BY sequence, item_id
                   LIMIT ?""",
                (workflow_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_artifact_storage(self, artifact_id: str, *, workflow_id: str | None = None) -> dict[str, Any]:
        """Resolve an artifact to an internal Blob storage key for services only."""

        with self.database.read_transaction() as con:
            row = con.execute(
                """SELECT a.*, b.storage_key, b.format AS blob_format,
                          b.sha256 AS blob_sha256, b.size_bytes AS blob_size_bytes,
                          b.lifecycle_state AS blob_lifecycle_state
                   FROM artifacts a JOIN artifact_blobs b ON b.blob_id=a.blob_id
                   WHERE a.artifact_id=? AND a.verified=1""",
                (artifact_id,),
            ).fetchone()
            if row is None or (workflow_id is not None and str(row["workflow_id"]) != workflow_id):
                raise NotFoundError(f"artifact does not exist: {artifact_id}")
            if row["lifecycle_state"] != "READY" or row["blob_lifecycle_state"] != "READY":
                raise RepositoryError("artifact is not ready", code="ARTIFACT_INVALID")
            # A READY flag alone is not enough to authorize a read.  The
            # Artifact row and its content-addressed Blob must still describe
            # the same byte object; otherwise a historical/corrupt row could
            # make a service read under the wrong format or fingerprint.
            from .workspace import artifact_blob_facts_match

            if not artifact_blob_facts_match(
                artifact_format=row["format"],
                blob_format=row["blob_format"],
                artifact_sha256=row["sha256"],
                blob_sha256=row["blob_sha256"],
                artifact_size_bytes=row["size_bytes"],
                blob_size_bytes=row["blob_size_bytes"],
            ):
                raise RepositoryError(
                    "artifact and Blob metadata conflict",
                    code="ARTIFACT_INVALID",
                )
            return {
                "artifact_id": str(row["artifact_id"]),
                "workflow_id": str(row["workflow_id"]),
                "artifact_type": str(row["artifact_type"]),
                "storage_key": str(row["storage_key"]),
                "format": str(row["blob_format"]),
                "sha256": str(row["blob_sha256"]),
                "size_bytes": int(row["blob_size_bytes"]),
            }

    def list_work_unit_items(self, work_unit_id: str) -> list[dict[str, Any]]:
        with self.database.read_transaction() as con:
            rows = con.execute(
                """SELECT wui.*, wus.work_unit_segment_id
                   FROM work_unit_items wui
                   LEFT JOIN work_unit_segments wus
                     ON wus.work_unit_id=wui.work_unit_id AND wus.item_id=wui.item_id
                   WHERE wui.work_unit_id=? ORDER BY wui.ordinal, wui.item_id""",
                (work_unit_id,),
            ).fetchall()
            if not rows:
                if con.execute("SELECT 1 FROM work_units WHERE work_unit_id=?", (work_unit_id,)).fetchone() is None:
                    raise NotFoundError(f"work unit does not exist: {work_unit_id}")
            return [dict(row) for row in rows]

    def get_budget(self, workflow_group_id: str, budget_key: str) -> dict[str, Any] | None:
        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT * FROM retry_budgets WHERE workflow_group_id=? AND budget_key=?",
                (workflow_group_id, budget_key),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_work_unit_artifacts(self, work_unit_id: str) -> list[str]:
        with self.database.read_transaction() as con:
            return [str(row["artifact_id"]) for row in con.execute(
                """SELECT artifact_id FROM artifacts
                   WHERE work_unit_id=? AND lifecycle_state='READY' AND verified=1 ORDER BY artifact_id""",
                (work_unit_id,),
            ).fetchall()]

    def get_tts_plan(self, workflow_id: str, tts_submission_key: str) -> dict[str, Any] | None:
        with self.database.read_transaction() as con:
            row = con.execute(
                """SELECT u.*, p.provider_submission_id, p.provider,
                          p.provider_account_scope, p.tts_submission_key,
                          p.side_effect_state AS submission_state
                   FROM work_units u JOIN provider_submissions p
                     ON p.provider_submission_id=u.provider_submission_id
                   WHERE u.workflow_id=? AND u.tts_submission_key=?""",
                (workflow_id, tts_submission_key),
            ).fetchone()
            if row is None:
                return None
            attempt = con.execute(
                """SELECT attempt_id FROM work_unit_attempts
                   WHERE workflow_id=? AND work_unit_id=? ORDER BY started_at DESC LIMIT 1""",
                (workflow_id, row["work_unit_id"]),
            ).fetchone()
            return {
                "workflow_id": workflow_id,
                "workflow_group_id": str(row["workflow_group_id"]),
                "step_id": str(row["step_id"]),
                "submission_id": str(row["provider_submission_id"]),
                "work_unit_id": str(row["work_unit_id"]),
                "attempt_id": str(attempt["attempt_id"] if attempt else row["created_by_attempt_id"]),
                "side_effect_state": str(row["side_effect_state"]),
                "submission_state": str(row["submission_state"]),
                "status": str(row["status"]),
                "state_version": int(row["state_version"]),
                "operation_key": f"{row['provider']}:{row['provider_account_scope']}:{row['tts_submission_key']}",
                "intent_id": self._intent_id_for_plan(con, workflow_id, str(row["work_unit_id"])),
                "reused": True,
            }

    def get_latest_successful_tts_plan(self, workflow_id: str) -> dict[str, Any] | None:
        """Return a completed TTS plan for recovery when the config hash changes."""

        with self.database.read_transaction() as con:
            row = con.execute(
                """SELECT u.*, p.provider_submission_id, p.provider,
                          p.provider_account_scope, p.tts_submission_key,
                          p.side_effect_state AS submission_state
                   FROM work_units u JOIN provider_submissions p
                     ON p.provider_submission_id=u.provider_submission_id
                   WHERE u.workflow_id=? AND u.status='SUCCEEDED'
                     AND u.side_effect_state='CONFIRMED'
                     AND p.side_effect_state='CONFIRMED'
                   ORDER BY u.finished_at DESC, u.work_unit_id DESC LIMIT 1""",
                (workflow_id,),
            ).fetchone()
            if row is None:
                return None
            attempt = con.execute(
                """SELECT attempt_id FROM work_unit_attempts
                   WHERE workflow_id=? AND work_unit_id=?
                   ORDER BY started_at DESC LIMIT 1""",
                (workflow_id, row["work_unit_id"]),
            ).fetchone()
            return {
                "workflow_id": workflow_id,
                "step_id": str(row["step_id"]),
                "work_unit_id": str(row["work_unit_id"]),
                "attempt_id": str(attempt["attempt_id"] if attempt is not None else row["created_by_attempt_id"]),
                "submission_id": str(row["provider_submission_id"]),
                "submission_state": str(row["submission_state"]),
                "status": str(row["status"]),
                "reused": True,
            }

    @staticmethod
    def _intent_id_for_plan(con: sqlite3.Connection, workflow_id: str, work_unit_id: str) -> str | None:
        row = con.execute(
            """SELECT intent_id FROM side_effect_intents
               WHERE workflow_id=? AND work_unit_id=? AND operation_namespace='tts'
               ORDER BY created_at DESC LIMIT 1""",
            (workflow_id, work_unit_id),
        ).fetchone()
        return str(row["intent_id"]) if row is not None else None

    @staticmethod
    def _assert_plan_lease(con: sqlite3.Connection, plan: Mapping[str, Any]) -> None:
        """Reject local writes from a worker whose provider lease is stale.

        Older callers may construct repository plans without lease metadata;
        those calls remain compatible. Engine-created plans carry all three
        fields, so side-effect projections enforce the same fencing predicate
        as the external-call boundary.
        """

        lease_id = str(plan.get("lease_id") or "")
        owner_id = str(plan.get("lease_owner_id") or "")
        token = plan.get("lease_fencing_token")
        if not lease_id or not owner_id or token is None:
            return
        try:
            fencing_token = int(token)
        except (TypeError, ValueError) as exc:
            raise LeaseConflict("TTS plan has an invalid lease fencing token") from exc
        row = con.execute(
            """SELECT 1 FROM workflow_leases
               WHERE lease_id=? AND owner_id=? AND fencing_token=?
                 AND state='ACTIVE' AND lease_until>?""",
            (lease_id, owner_id, fencing_token, utc_now()),
        ).fetchone()
        if row is None:
            raise LeaseConflict("TTS plan lease is stale or expired")

    @contextmanager
    def _transaction_after_intent(
        self,
        *,
        operation_namespace: str,
        operation_key: str,
        payload_hash: str,
        intent_id: str,
    ):
        """Keep a rejected pre-transaction journal record auditable.

        The file journal is intentionally written before SQLite so a crash
        cannot lose the minimum recovery fact.  If the SQLite transaction is
        then rejected by a known repository/domain error, append an ABORTED
        marker.  Unknown database/commit failures deliberately remain
        RECORDED so recovery can fail closed instead of guessing rollback.
        """

        try:
            with self.database.transaction() as con:
                yield con
        except RepositoryError:
            # A conflict can be raised after discovering an already-existing
            # SQLite intent.  Do not append ABORTED in that case: the journal
            # and row still describe a real durable operation.  If the
            # verification read itself fails, retain RECORDED and fail closed.
            try:
                with self.database.read_transaction() as con:
                    existing = con.execute(
                        "SELECT 1 FROM side_effect_intents "
                        "WHERE operation_namespace=? AND operation_key=? LIMIT 1",
                        (operation_namespace, operation_key),
                    ).fetchone()
            except Exception:
                raise
            if existing is not None:
                raise
            try:
                self.intent_log.abort(
                    operation_namespace=operation_namespace,
                    operation_key=operation_key,
                    payload_hash=payload_hash,
                    intent_id=intent_id,
                )
            except Exception:
                # Preserve the original domain error.  A failed abort marker
                # leaves RECORDED in place, which is the safe fail-closed
                # outcome for the next recovery pass.
                pass
            raise

    def prepare_tts_plan(
        self,
        workflow_id: str,
        *,
        provider: str,
        provider_account_scope: str,
        unit_type: str,
        tts_submission_key: str,
        ordered_plan: list[Mapping[str, Any]],
        input_hash: str,
        submission_profile_hash: str,
        capability_snapshot: Mapping[str, Any] | None = None,
        submission_contract_version: str = "1",
        step_key: str = "tts",
        step_type: str = "TTS",
        lease_fencing_token: int | None = None,
        lease_id: str | None = None,
        lease_owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one durable TTS plan and all run-local ownership rows.

        This is the T7 hand-off between planning and a provider adapter.  It
        is intentionally idempotent by the provider/account/submission key:
        a retry can recover the same work unit without creating a second
        billable submission intent.
        """

        if unit_type not in {"single", "composite", "upload"}:
            raise RepositoryError("unsupported TTS unit type", code="VALIDATION_ERROR")
        if not provider.strip() or not provider_account_scope.strip() or not tts_submission_key.strip():
            raise RepositoryError("provider, account scope and submission key are required", code="VALIDATION_ERROR")
        if not ordered_plan:
            raise RepositoryError("TTS plan must contain at least one item", code="VALIDATION_ERROR")
        capability_snapshot = dict(capability_snapshot or {})
        plan_hash = content_hash(ordered_plan)
        capability_hash = content_hash(capability_snapshot)
        operation_key = f"{provider}:{provider_account_scope}:{tts_submission_key}"
        intent_payload = {"plan": ordered_plan, "input_hash": input_hash}
        journal_intent_id = self.intent_log.record(
            operation_namespace="tts",
            operation_key=operation_key,
            payload_hash=content_hash(intent_payload),
            workflow_id=workflow_id,
            provider_account_scope=provider_account_scope,
        )
        now = utc_now()
        with self._transaction_after_intent(
            operation_namespace="tts",
            operation_key=operation_key,
            payload_hash=content_hash(intent_payload),
            intent_id=journal_intent_id,
        ) as con:
            self._assert_plan_lease(
                con,
                {
                    "lease_id": lease_id,
                    "lease_owner_id": lease_owner_id,
                    "lease_fencing_token": lease_fencing_token,
                },
            )
            workflow = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            if workflow["result_status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise ConflictError("terminal workflow cannot accept a new TTS plan")
            if workflow["control_state"] != "RUNNING":
                raise ConflictError(f"workflow control state is {workflow['control_state']}")

            group_id = str(workflow["workflow_group_id"])
            group = con.execute(
                "SELECT lifecycle_state FROM workflow_groups WHERE workflow_group_id=?",
                (group_id,),
            ).fetchone()
            if group is None:
                raise RepositoryError("workflow group is missing", code="PERSISTENCE_ERROR")
            if group["lifecycle_state"] == "DRAFT":
                con.execute(
                    """UPDATE workflow_groups SET lifecycle_state='ACTIVE', accepted_at=COALESCE(accepted_at, ?),
                        state_version=state_version+1, updated_at=? WHERE workflow_group_id=? AND lifecycle_state='DRAFT'""",
                    (now, now, group_id),
                )
            elif group["lifecycle_state"] in {"ABANDONED", "CLOSED"}:
                raise ConflictError(f"workflow group is {group['lifecycle_state'].lower()}")

            step = con.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND step_key=? ORDER BY step_id LIMIT 1",
                (workflow_id, step_key),
            ).fetchone()
            if step is None:
                step_id = new_id("step")
                con.execute(
                    """INSERT INTO workflow_steps(
                        step_id, workflow_id, scope, item_id, step_key, step_type,
                        step_definition_version, dependency_keys_json, status,
                        current_attempt_id, attempt_count, state_version,
                        aggregate_operation_key, operation_key_type, input_hash,
                        output_reference_json, retry_after, error_code,
                        error_details_json, started_at, finished_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (step_id, workflow_id, "workflow", None, step_key, step_type, "1", "[]",
                     "PENDING", None, 0, 0, None, None, input_hash, None, None, None, None, None, None),
                )
                step = con.execute("SELECT * FROM workflow_steps WHERE step_id=?", (step_id,)).fetchone()
            else:
                step_id = str(step["step_id"])
                if step["status"] in {"SUCCEEDED", "PERMANENT_FAILED", "CANCELLED"}:
                    raise ConflictError(f"TTS step is already {step['status'].lower()}")

            submission = con.execute(
                """SELECT * FROM provider_submissions
                   WHERE provider=? AND provider_account_scope=? AND tts_submission_key=?""",
                (provider, provider_account_scope, tts_submission_key),
            ).fetchone()
            if submission is not None:
                if (
                    str(submission["workflow_group_id"]) != group_id
                    or str(submission["unit_type"]) != unit_type
                    or str(submission["plan_hash"]) != plan_hash
                    or str(submission["input_hash"]) != input_hash
                    or str(submission["submission_profile_hash"]) != submission_profile_hash
                ):
                    raise IdempotencyConflict("TTS submission key is bound to a different plan")
                submission_id = str(submission["provider_submission_id"])
            else:
                submission_id = new_id("submission")
                try:
                    con.execute(
                        """INSERT INTO provider_submissions(
                            provider_submission_id, workflow_group_id, provider,
                            provider_account_scope, unit_type, tts_submission_key,
                            ordered_plan_json, plan_hash, input_hash, submission_profile_hash,
                            submission_contract_version, capability_snapshot_hash,
                            capability_snapshot_json, side_effect_state, state_version,
                            created_at, submitted_at, confirmed_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (submission_id, group_id, provider, provider_account_scope, unit_type,
                         tts_submission_key, canonical_json(ordered_plan), plan_hash, input_hash,
                         submission_profile_hash, submission_contract_version, capability_hash,
                         canonical_json(capability_snapshot), "NOT_STARTED", 0, now, None, None),
                    )
                except sqlite3.IntegrityError as exc:
                    raise IdempotencyConflict(f"TTS submission key cannot be claimed: {exc}") from exc

            existing_unit = con.execute(
                "SELECT * FROM work_units WHERE workflow_id=? AND provider_submission_id=?",
                (workflow_id, submission_id),
            ).fetchone()
            if existing_unit is not None:
                # A normal provider rejection leaves the unit in WAITING_RETRY;
                # an ambiguous submission that the user explicitly resolved as
                # NOT_SUBMITTED leaves it READY.  Both states represent a new
                # execute attempt over the same durable submission intent.  Do
                # not reuse the original ambiguous attempt, otherwise the
                # second real submission would be recorded against a finished
                # attempt and the retry audit would be incomplete.
                if existing_unit["status"] in {"WAITING_RETRY", "READY"} and submission["side_effect_state"] == "REJECTED":
                    previous_attempt = con.execute(
                        """SELECT attempt_id FROM work_unit_attempts
                           WHERE workflow_id=? AND work_unit_id=? ORDER BY started_at DESC LIMIT 1""",
                        (workflow_id, str(existing_unit["work_unit_id"])),
                    ).fetchone()
                    if previous_attempt is not None:
                        con.execute(
                            """UPDATE step_attempts SET status='FAILED', result_status='FAILED'
                               WHERE workflow_id=? AND attempt_id=? AND status='WAITING_RETRY'""",
                            (workflow_id, previous_attempt["attempt_id"]),
                        )
                        con.execute(
                            """UPDATE work_unit_attempts SET status='FAILED', finished_at=?
                               WHERE workflow_id=? AND attempt_id=? AND status='WAITING_RETRY'""",
                            (now, workflow_id, previous_attempt["attempt_id"]),
                        )
                    retry_attempt_id = new_id("attempt")
                    retry_attempt_seq = int(con.execute(
                        "SELECT COALESCE(MAX(attempt_seq),0)+1 FROM step_attempts WHERE workflow_id=? AND step_id=?",
                        (workflow_id, str(existing_unit["step_id"])),
                    ).fetchone()[0])
                    retry_execute_no = int(con.execute(
                        """SELECT COALESCE(MAX(execute_attempt_no),0)+1 FROM step_attempts
                           WHERE workflow_id=? AND step_id=? AND attempt_kind='EXECUTE'""",
                        (workflow_id, str(existing_unit["step_id"])),
                    ).fetchone()[0])
                    con.execute(
                        """INSERT INTO step_attempts(
                            attempt_id, workflow_id, step_id, attempt_kind, attempt_seq,
                            execute_attempt_no, status, result_status, error_code,
                            error_details_json, lease_fencing_token, state_version,
                            started_at, heartbeat_at, finished_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (retry_attempt_id, workflow_id, str(existing_unit["step_id"]), "EXECUTE",
                         retry_attempt_seq, retry_execute_no, "CREATED", "IN_PROGRESS", None, None,
                         lease_fencing_token, 0, now, None, None),
                    )
                    con.execute(
                        """INSERT INTO work_unit_attempts(
                            work_unit_attempt_id, workflow_id, step_id, work_unit_id, attempt_id,
                            attempt_kind, status, side_effect_state, fencing_token, state_version,
                            started_at, heartbeat_at, finished_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (new_id("unit-attempt"), workflow_id, str(existing_unit["step_id"]),
                         str(existing_unit["work_unit_id"]), retry_attempt_id, "EXECUTE",
                         "CREATED", "NOT_STARTED", lease_fencing_token, 0, now, None, None),
                    )
                    con.execute(
                        """UPDATE work_units SET status='READY', side_effect_state='REJECTED', state_version=state_version+1
                           WHERE workflow_id=? AND work_unit_id=? AND state_version=?""",
                        (workflow_id, str(existing_unit["work_unit_id"]), int(existing_unit["state_version"])),
                    )
                    con.execute(
                        """UPDATE workflow_steps SET status='READY', error_code=NULL, state_version=state_version+1
                           WHERE workflow_id=? AND step_id=?""",
                        (workflow_id, str(existing_unit["step_id"])),
                    )
                    con.execute(
                        """UPDATE workflow_steps SET current_attempt_id=?,
                               attempt_count=attempt_count+1, state_version=state_version+1
                           WHERE workflow_id=? AND step_id=?""",
                        (retry_attempt_id, workflow_id, str(existing_unit["step_id"])),
                    )
                    self.events.append_in_transaction(
                        con, workflow_id, "TTS_RETRY_ATTEMPT_CREATED",
                        {"submission_id": submission_id, "work_unit_id": str(existing_unit["work_unit_id"])},
                        actor_type="WORKER", actor_id="workflow-engine", step_id=str(existing_unit["step_id"]), attempt_id=retry_attempt_id,
                    )
                    snapshot = _snapshot_from_connection(con, workflow_id)
                    self.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
                    return {
                        "workflow_id": workflow_id,
                        "workflow_group_id": group_id,
                        "step_id": str(existing_unit["step_id"]),
                        "submission_id": submission_id,
                        "work_unit_id": str(existing_unit["work_unit_id"]),
                        "attempt_id": retry_attempt_id,
                        "side_effect_state": "REJECTED",
                        "submission_state": str(submission["side_effect_state"]),
                        "status": "READY",
                        "state_version": int(existing_unit["state_version"]) + 1,
                        "operation_key": operation_key,
                        "intent_id": self._intent_id_for_plan(con, workflow_id, str(existing_unit["work_unit_id"])),
                        "reused": True,
                        "lease_id": lease_id,
                        "lease_owner_id": lease_owner_id,
                        "lease_fencing_token": lease_fencing_token,
                    }
                return {
                    "workflow_id": workflow_id,
                    "workflow_group_id": group_id,
                    "step_id": str(existing_unit["step_id"]),
                    "submission_id": submission_id,
                    "work_unit_id": str(existing_unit["work_unit_id"]),
                    "attempt_id": str(existing_unit["created_by_attempt_id"]),
                    "side_effect_state": str(existing_unit["side_effect_state"]),
                    "status": str(existing_unit["status"]),
                    "submission_state": str(submission["side_effect_state"]),
                    "state_version": int(existing_unit["state_version"]),
                    "operation_key": operation_key,
                    "intent_id": self._intent_id_for_plan(con, workflow_id, str(existing_unit["work_unit_id"])),
                    "reused": True,
                    "lease_id": lease_id,
                    "lease_owner_id": lease_owner_id,
                    "lease_fencing_token": lease_fencing_token,
                }

            attempt_id = new_id("attempt")
            attempt_seq = int(con.execute(
                "SELECT COALESCE(MAX(attempt_seq),0)+1 FROM step_attempts WHERE workflow_id=? AND step_id=?",
                (workflow_id, step_id),
            ).fetchone()[0])
            execute_no = int(con.execute(
                """SELECT COALESCE(MAX(execute_attempt_no),0)+1 FROM step_attempts
                   WHERE workflow_id=? AND step_id=? AND attempt_kind='EXECUTE'""",
                (workflow_id, step_id),
            ).fetchone()[0])
            con.execute(
                """INSERT INTO step_attempts(
                    attempt_id, workflow_id, step_id, attempt_kind, attempt_seq,
                    execute_attempt_no, status, result_status, error_code,
                    error_details_json, lease_fencing_token, state_version,
                    started_at, heartbeat_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id, workflow_id, step_id, "EXECUTE", attempt_seq, execute_no,
                     "CREATED", "IN_PROGRESS", None, None, lease_fencing_token, 0, now, None, None),
            )
            unit_id = new_id("unit")
            con.execute(
                """INSERT INTO work_units(
                    work_unit_id, workflow_id, workflow_group_id, step_id,
                    provider_submission_id, created_by_attempt_id, unit_type,
                    tts_submission_key, input_hash, provider_receipt_ref,
                    side_effect_state, status, state_version, created_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (unit_id, workflow_id, group_id, step_id, submission_id, attempt_id, unit_type,
                 tts_submission_key, input_hash, None, "NOT_STARTED", "READY", 0, now, None),
            )
            for ordinal, item_spec in enumerate(ordered_plan):
                item_id = str(item_spec.get("item_id") or "")
                if not item_id:
                    raise RepositoryError("each TTS plan item needs item_id", code="VALIDATION_ERROR")
                item = con.execute(
                    "SELECT * FROM work_items WHERE workflow_id=? AND item_id=?",
                    (workflow_id, item_id),
                ).fetchone()
                if item is None:
                    raise NotFoundError(f"item does not exist: {item_id}")
                assignment = con.execute(
                    """SELECT * FROM work_item_assignments
                       WHERE workflow_id=? AND step_id=? AND item_id=? AND state='ACTIVE'
                       ORDER BY assignment_revision DESC LIMIT 1""",
                    (workflow_id, step_id, item_id),
                ).fetchone()
                assignment_id = str(assignment["assignment_id"]) if assignment else new_id("assignment")
                if assignment is None:
                    con.execute(
                        """INSERT INTO work_item_assignments(
                            assignment_id, workflow_id, step_id, item_id, delivery_unit_key,
                            assignment_revision, state, supersedes_assignment_id, plan_hash,
                            state_version, created_at, superseded_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (assignment_id, workflow_id, step_id, item_id, tts_submission_key, 0,
                         "ACTIVE", None, plan_hash, 0, now, None),
                    )
                unit_item_id = new_id("unit-item")
                con.execute(
                    """INSERT INTO work_unit_items(
                        work_unit_item_id, workflow_id, work_unit_id, assignment_id, item_id,
                        ordinal, result_status, result_metadata_json, state_version
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (unit_item_id, workflow_id, unit_id, assignment_id, item_id, ordinal,
                     "PENDING", "{}", 0),
                )
                segment_id = new_id("segment")
                con.execute(
                    """INSERT INTO work_unit_segments(
                        work_unit_segment_id, work_unit_id, item_id, segment_index,
                        segment_key, ordered_position, input_hash, result_status
                    ) VALUES (?,?,?,?,?,?,?,?)""",
                    (segment_id, unit_id, item_id, 0, f"{tts_submission_key}:{ordinal}", ordinal,
                     str(item["content_hash"]), "PENDING"),
                )
            work_unit_attempt_id = new_id("unit-attempt")
            con.execute(
                """INSERT INTO work_unit_attempts(
                    work_unit_attempt_id, workflow_id, step_id, work_unit_id, attempt_id,
                    attempt_kind, status, side_effect_state, fencing_token, state_version,
                    started_at, heartbeat_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (work_unit_attempt_id, workflow_id, step_id, unit_id, attempt_id, "EXECUTE",
                 "CREATED", "NOT_STARTED", lease_fencing_token, 0, now, None, None),
            )
            intent_id = journal_intent_id
            try:
                con.execute(
                    """INSERT INTO side_effect_intents(
                        intent_id, workflow_id, step_id, attempt_id, work_unit_id,
                        work_unit_attempt_id, operation_namespace, operation_key,
                        payload_hash, provider_account_scope, state, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (intent_id, workflow_id, step_id, attempt_id, unit_id, None, "tts",
                     operation_key, content_hash(intent_payload),
                     provider_account_scope, "RECORDED", now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict(f"side-effect intent key is already bound: {exc}") from exc
            con.execute(
                """UPDATE workflows SET current_step_id=?, execution_state='PREPARING',
                    status=CASE WHEN status='DRAFT' THEN 'ACTIVE' ELSE status END,
                    accepted_at=COALESCE(accepted_at, ?), state_version=state_version+1,
                    updated_at=? WHERE workflow_id=?""",
                (step_id, now, now, workflow_id),
            )
            con.execute(
                "UPDATE workflow_steps SET status='READY', input_hash=? WHERE workflow_id=? AND step_id=?",
                (input_hash, workflow_id, step_id),
            )
            con.execute(
                """UPDATE workflow_steps SET current_attempt_id=?,
                       attempt_count=attempt_count+1, state_version=state_version+1
                   WHERE workflow_id=? AND step_id=?""",
                (attempt_id, workflow_id, step_id),
            )
            self.events.append_in_transaction(
                con, workflow_id, "TTS_PLAN_PREPARED",
                {"submission_id": submission_id, "work_unit_id": unit_id, "item_count": len(ordered_plan)},
                actor_type="WORKER", actor_id="workflow-engine", step_id=step_id, attempt_id=attempt_id,
            )
            snapshot = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
            return {
                "workflow_id": workflow_id,
                "workflow_group_id": group_id,
                "step_id": step_id,
                "submission_id": submission_id,
                "work_unit_id": unit_id,
                "attempt_id": attempt_id,
                "side_effect_state": "NOT_STARTED",
                "submission_state": str(submission["side_effect_state"]) if submission is not None else "NOT_STARTED",
                "status": "READY",
                "state_version": 0,
                "operation_key": operation_key,
                "intent_id": intent_id,
                "reused": False,
                "lease_id": lease_id,
                "lease_owner_id": lease_owner_id,
                "lease_fencing_token": lease_fencing_token,
            }

    def begin_tts_submission(self, plan: Mapping[str, Any]) -> None:
        operation_key = str(plan.get("operation_key") or "")
        if not operation_key:
            raise RepositoryError("TTS side-effect operation key is missing", code="PERSISTENCE_ERROR")
        with self.database.transaction() as con:
            self._assert_plan_lease(con, plan)
            submission = con.execute(
                "SELECT * FROM provider_submissions WHERE provider_submission_id=?",
                (plan["submission_id"],),
            ).fetchone()
            unit = con.execute(
                "SELECT * FROM work_units WHERE work_unit_id=? AND workflow_id=?",
                (plan["work_unit_id"], plan["workflow_id"]),
            ).fetchone()
            attempt = con.execute(
                "SELECT * FROM step_attempts WHERE attempt_id=? AND workflow_id=?",
                (plan["attempt_id"], plan["workflow_id"]),
            ).fetchone()
            if submission is None or unit is None or attempt is None:
                raise NotFoundError("TTS plan is incomplete")
            workflow = con.execute(
                "SELECT control_state, execution_state FROM workflows WHERE workflow_id=?",
                (plan["workflow_id"],),
            ).fetchone()
            if workflow is None:
                raise NotFoundError("workflow does not exist")
            if str(workflow["control_state"]) != "RUNNING":
                code = "WORKFLOW_CANCELLED" if str(workflow["control_state"]) in {"TERMINATING", "TERMINATED"} else "CONTROL_STATE_CONFLICT"
                raise ConflictError(
                    "workflow control state does not permit provider submission",
                    code=code,
                    details={
                        "control_state": str(workflow["control_state"]),
                        "execution_state": str(workflow["execution_state"]),
                    },
                )
            if submission["side_effect_state"] in {"AMBIGUOUS", "SUBMITTED", "CONFIRMED"}:
                raise ConflictError("TTS submission already crossed the side-effect boundary")
            if unit["state_version"] != plan["state_version"]:
                raise ConflictError("TTS work unit changed while starting")
            now = utc_now()
            intent = con.execute(
                """UPDATE side_effect_intents SET state='COMMITTED', updated_at=?
                   WHERE workflow_id=? AND work_unit_id=? AND operation_namespace='tts'
                     AND state IN ('RECORDED','COMMITTED','ARCHIVED')""",
                (now, plan["workflow_id"], plan["work_unit_id"]),
            )
            if intent.rowcount != 1:
                raise RepositoryError("TTS side-effect intent is missing", code="PERSISTENCE_ERROR")
            if con.execute(
                """UPDATE provider_submissions SET side_effect_state='IN_FLIGHT', state_version=state_version+1
                   WHERE provider_submission_id=? AND state_version=?""",
                (plan["submission_id"], submission["state_version"]),
            ).rowcount != 1:
                raise ConflictError("TTS submission changed while starting")
            if con.execute(
                """UPDATE work_units SET side_effect_state='IN_FLIGHT', status='RUNNING', state_version=state_version+1
                   WHERE workflow_id=? AND work_unit_id=? AND state_version=?""",
                (plan["workflow_id"], plan["work_unit_id"], plan["state_version"]),
            ).rowcount != 1:
                raise ConflictError("TTS work unit changed while starting")
            con.execute(
                """UPDATE work_items SET status='RUNNING', state_version=state_version+1, updated_at=?
                   WHERE workflow_id=? AND item_id IN (
                       SELECT item_id FROM work_unit_items WHERE workflow_id=? AND work_unit_id=?
                   ) AND status <> 'SKIPPED'""",
                (now, plan["workflow_id"], plan["workflow_id"], plan["work_unit_id"]),
            )
            con.execute(
                """UPDATE step_attempts SET status='RUNNING', heartbeat_at=?, state_version=state_version+1
                   WHERE workflow_id=? AND attempt_id=? AND status='CREATED'""",
                (now, plan["workflow_id"], plan["attempt_id"]),
            )
            con.execute(
                """UPDATE workflow_steps SET status='RUNNING', state_version=state_version+1
                   WHERE workflow_id=? AND step_id=? AND status IN ('READY','PREPARING')""",
                (plan["workflow_id"], plan["step_id"]),
            )
            con.execute(
                "UPDATE workflows SET execution_state='RUNNING', state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                (now, plan["workflow_id"]),
            )
            self.events.append_in_transaction(
                con, plan["workflow_id"], "TTS_SUBMISSION_IN_FLIGHT",
                {"submission_id": plan["submission_id"], "work_unit_id": plan["work_unit_id"]},
                actor_type="WORKER", actor_id="workflow-engine", step_id=plan["step_id"], attempt_id=plan["attempt_id"],
            )
            snapshot = _snapshot_from_connection(con, plan["workflow_id"])
            self.events.write_snapshot_in_transaction(con, plan["workflow_id"], snapshot.as_dict())
        self.intent_log.mark(
            operation_namespace="tts",
            operation_key=operation_key,
            state="COMMITTED",
            intent_id=str(plan.get("intent_id") or "") or None,
        )

    def mark_tts_failure(
        self,
        plan: Mapping[str, Any],
        *,
        error_code: str,
        error_message: str | None = None,
        error_details: Mapping[str, Any] | None = None,
        ambiguous: bool = False,
        preserve_submission: bool = False,
        require_lease: bool = True,
    ) -> None:
        """Record a TTS failure without ever guessing a provider outcome.

        ``preserve_submission`` is used only after a provider receipt has
        already been observed.  At that point a download, staging, or local
        publication failure must leave the billable submission in its known
        submitted state for the current local attempt; a later explicit
        generation still creates a fresh attempt and does not query the
        provider.
        """

        now = utc_now()
        safe_message = redact_public_json(" ".join(str(error_message or error_code).split()))
        message = (safe_message if isinstance(safe_message, str) else str(error_code))[:2000]
        safe_details = redact_public_json(error_details or {})
        details = dict(safe_details) if isinstance(safe_details, Mapping) else {}
        retry_after = None
        # TTS has no provider reconciliation branch. Keep the old argument for
        # callers compiled against the previous repository API, but normalize
        # every no-receipt failure to a local retryable state.
        if error_code in {"SUBMISSION_AMBIGUOUS", "TTS_SUBMISSION_AMBIGUOUS"}:
            error_code = "LOCAL_SUBMISSION_NOT_CONFIRMED"
        ambiguous = False
        details_payload = {"error_code": error_code, "message": message, "details": details}
        suppress_automatic_retry = bool(
            details.get("browser_disconnected")
            or details.get("cancelled_before_confirmation")
        )
        if preserve_submission:
            # The caller can request this mode only after record_tts_receipt;
            # verify the durable fact before mutating any projections.  This
            # also prevents a stale/misrouted plan from silently preserving a
            # different submission's state.
            with self.database.read_transaction() as con:
                submission = con.execute(
                    "SELECT side_effect_state FROM provider_submissions WHERE provider_submission_id=?",
                    (plan["submission_id"],),
                ).fetchone()
                receipt = con.execute(
                    """SELECT 1 FROM provider_receipts r
                       JOIN provider_receipt_bindings b
                         ON b.receipt_id=r.receipt_id
                        AND b.workflow_id=?
                        AND b.work_unit_id=?
                       WHERE r.provider_submission_id=?
                       LIMIT 1""",
                    (plan["workflow_id"], plan["work_unit_id"], plan["submission_id"]),
                ).fetchone()
            if submission is None or str(submission["side_effect_state"]) not in {"SUBMITTED", "CONFIRMED"} or receipt is None:
                raise RepositoryError(
                    "cannot preserve TTS submission before a durable provider receipt",
                    code="PERSISTENCE_ERROR",
                )
            submission_state = str(submission["side_effect_state"])
            unit_state = submission_state
            unit_status = "WAITING_RETRY"
            attempt_status = unit_status
            step_status = unit_status
            result_status = "FAILED"
        else:
            submission_state, unit_state, unit_status, attempt_status, step_status, result_status = (
                "REJECTED", "REJECTED", "WAITING_RETRY", "WAITING_RETRY", "WAITING_RETRY", "FAILED"
            )
        intent_state = "COMMITTED" if preserve_submission else "ARCHIVED"
        with self.database.transaction() as con:
            if require_lease:
                self._assert_plan_lease(con, plan)
            current_workflow = con.execute(
                "SELECT execution_state FROM workflows WHERE workflow_id=?",
                (plan["workflow_id"],),
            ).fetchone()
            current_unit = con.execute(
                "SELECT status, created_by_attempt_id FROM work_units WHERE workflow_id=? AND work_unit_id=?",
                (plan["workflow_id"], plan["work_unit_id"]),
            ).fetchone()
            # A stale worker must never demote a later successful publication
            # or a terminal workflow that superseded it.
            if current_workflow is None or current_workflow["execution_state"] == "TERMINAL":
                return
            if current_unit is None:
                raise RepositoryError("TTS work unit does not exist", code="PERSISTENCE_ERROR")
            if current_unit["status"] == "SUCCEEDED":
                return
            if not require_lease and str(current_unit["created_by_attempt_id"] or "") != str(plan.get("attempt_id") or ""):
                return
            if not ambiguous and not suppress_automatic_retry and error_code in RetryPolicy.RETRYABLE:
                # Keep the first automatic retry out of the same event-loop
                # turn as the failure.  The attempt number is read in this
                # transaction and the delay is capped; this is intentionally
                # deterministic so a restart cannot create a retry storm.
                # Rate limiting keeps its shorter provider-facing delay.
                retry_after = _expiry(5) if error_code == "PROVIDER_RATE_LIMITED" else _expiry(15)
            if not preserve_submission:
                con.execute(
                    "UPDATE provider_submissions SET side_effect_state=?, state_version=state_version+1 WHERE provider_submission_id=?",
                    (submission_state, plan["submission_id"]),
                )
            con.execute(
                """UPDATE work_units SET side_effect_state=?, status=?, state_version=state_version+1
                   WHERE workflow_id=? AND work_unit_id=?""",
                (unit_state, unit_status, plan["workflow_id"], plan["work_unit_id"]),
            )
            # Keep WorkItem status as a queryable projection of the current
            # delivery outcome.  The original implementation only updated
            # the WorkUnit/attempt rows, so the item-target retry command saw
            # PENDING even after an entire external submission had failed.
            con.execute(
                """UPDATE work_items SET status=?, state_version=state_version+1, updated_at=?
                   WHERE workflow_id=? AND item_id IN (
                       SELECT item_id FROM work_unit_items WHERE workflow_id=? AND work_unit_id=?
                   ) AND status <> 'SKIPPED'""",
                ("AMBIGUOUS" if ambiguous else "FAILED", now,
                 plan["workflow_id"], plan["workflow_id"], plan["work_unit_id"]),
            )
            con.execute(
                """UPDATE step_attempts SET status=?, result_status=?, error_code=?, error_details_json=?,
                   finished_at=?, state_version=state_version+1 WHERE workflow_id=? AND attempt_id=?""",
                (attempt_status, result_status, error_code, canonical_json(details_payload), now,
                 plan["workflow_id"], plan["attempt_id"]),
            )
            con.execute(
                """UPDATE work_unit_attempts SET status=?, side_effect_state=?, finished_at=?, state_version=state_version+1
                   WHERE workflow_id=? AND attempt_id=?""",
                (attempt_status, unit_state, now, plan["workflow_id"], plan["attempt_id"]),
            )
            con.execute(
                """UPDATE side_effect_intents SET state=?, updated_at=?
                   WHERE workflow_id=? AND work_unit_id=? AND operation_namespace='tts'""",
                (intent_state, now, plan["workflow_id"], plan["work_unit_id"]),
            )
            con.execute(
                """UPDATE workflow_steps SET status=?, error_code=?, retry_after=?,
                       state_version=state_version+1
                   WHERE workflow_id=? AND step_id=?""",
                (step_status, error_code, retry_after, plan["workflow_id"], plan["step_id"]),
            )
            con.execute(
                """UPDATE workflows SET execution_state=?, last_error_code=?, last_error_message=?,
                       state_version=state_version+1, updated_at=?
                   WHERE workflow_id=?""",
                ("WAITING_RETRY", error_code, message, now, plan["workflow_id"]),
            )
            unit_after = con.execute(
                "SELECT state_version FROM work_units WHERE workflow_id=? AND work_unit_id=?",
                (plan["workflow_id"], plan["work_unit_id"]),
            ).fetchone()
            workflow_after = con.execute(
                "SELECT state_version FROM workflows WHERE workflow_id=?",
                (plan["workflow_id"],),
            ).fetchone()
            self.events.append_in_transaction(
                con,
                plan["workflow_id"],
                "TTS_OUTPUT_RETRYABLE_FAILURE" if preserve_submission else "TTS_SUBMISSION_REJECTED",
                {
                    "submission_id": plan["submission_id"],
                    "work_unit_id": plan["work_unit_id"],
                    "attempt_id": plan["attempt_id"],
                    "error_code": error_code,
                    "message": message,
                    "details": details,
                    "submission_preserved": preserve_submission,
                    "target": {"target_type": "WORK_UNIT", "work_unit_id": plan["work_unit_id"]},
                    "target_state_version": int(unit_after["state_version"]) if unit_after is not None else None,
                    "workflow_state_version": int(workflow_after["state_version"]) if workflow_after is not None else None,
                },
                actor_type="WORKER", actor_id="workflow-engine", step_id=plan["step_id"], attempt_id=plan["attempt_id"],
            )
            snapshot = _snapshot_from_connection(con, plan["workflow_id"])
            self.events.write_snapshot_in_transaction(con, plan["workflow_id"], snapshot.as_dict())
        self.intent_log.mark(
            operation_namespace="tts",
            operation_key=str(plan.get("operation_key") or ""),
            state=intent_state,
            intent_id=str(plan.get("intent_id") or "") or None,
        )

    def record_generation_task_failure(
        self,
        workflow_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
        error_details: Mapping[str, Any] | None = None,
    ) -> WorkflowSnapshot:
        """Persist an unexpected worker failure before the task disappears.

        The in-memory task registry is only a scheduling aid. If an exception
        occurs before the engine can create a typed TTS failure, leaving the
        workflow in RUNNING/IN_PROGRESS makes the run look alive forever after
        restart. TTS boundaries are normalized to local rejection here, so the
        next explicit generation is a fresh attempt and never a provider
        reconciliation request.
        """

        now = utc_now()
        code = str(error_code or "INTERNAL_ERROR")[:128]
        safe_message = redact_public_json(" ".join(str(error_message or "生成任务未能完成").split()))
        message = (safe_message if isinstance(safe_message, str) else "生成任务未能完成")[:2000]
        safe_details = redact_public_json(error_details or {})
        details = dict(safe_details) if isinstance(safe_details, Mapping) else {}
        with self.database.transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            if snapshot.execution_state == "TERMINAL" or snapshot.control_state == "TERMINATING":
                return snapshot
            workflow_row = con.execute(
                "SELECT workflow_type FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            is_tts_workflow = str(workflow_row["workflow_type"] or "").lower() == "tts" if workflow_row else False
            unresolved = con.execute(
                """SELECT 1 FROM work_units
                   WHERE workflow_id=?
                     AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                     AND status <> 'SUCCEEDED'
                   LIMIT 1""",
                (workflow_id,),
            ).fetchone() is not None
            if is_tts_workflow:
                # An unexpected worker exception can happen after the local
                # IN_FLIGHT commit. Retire those projections together before
                # publishing the retryable workflow state; no remote fact is
                # inferred and no lookup is attempted.
                con.execute(
                    """UPDATE provider_submissions
                       SET side_effect_state='REJECTED', state_version=state_version+1
                       WHERE provider_submission_id IN (
                           SELECT provider_submission_id FROM work_units
                           WHERE workflow_id=? AND provider_submission_id IS NOT NULL
                       ) AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')""",
                    (workflow_id,),
                )
                con.execute(
                    """UPDATE work_units
                       SET side_effect_state='REJECTED', status='WAITING_RETRY',
                           state_version=state_version+1
                       WHERE workflow_id=? AND status NOT IN ('SUCCEEDED','CANCELLED')
                         AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')""",
                    (workflow_id,),
                )
                con.execute(
                    """UPDATE work_unit_attempts
                       SET side_effect_state='REJECTED', status='WAITING_RETRY',
                           finished_at=?, state_version=state_version+1
                       WHERE workflow_id=?
                         AND status IN ('CREATED','PREPARING','RUNNING','VERIFYING',
                                        'WAITING_USER','BLOCKED','RECOVERING','AMBIGUOUS')
                         AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')""",
                    (now, workflow_id),
                )
                con.execute(
                    """UPDATE work_unit_items SET result_status='FAILED', state_version=state_version+1
                       WHERE workflow_id=? AND result_status NOT IN ('SUCCEEDED','SKIPPED')""",
                    (workflow_id,),
                )
                con.execute(
                    """UPDATE work_unit_segments SET result_status='FAILED'
                       WHERE work_unit_id IN (
                           SELECT work_unit_id FROM work_units WHERE workflow_id=?
                       ) AND result_status NOT IN ('SUCCEEDED','SKIPPED')""",
                    (workflow_id,),
                )
                con.execute(
                    """UPDATE work_items SET status='FAILED', state_version=state_version+1, updated_at=?
                       WHERE workflow_id=? AND status NOT IN ('SUCCEEDED','SKIPPED','CANCELLED')""",
                    (now, workflow_id),
                )
                con.execute(
                    """UPDATE side_effect_intents SET state='ARCHIVED', updated_at=?
                       WHERE workflow_id=? AND operation_namespace='tts' AND state <> 'ARCHIVED'""",
                    (now, workflow_id),
                )
                con.execute(
                    """UPDATE user_interventions SET state='RESOLVED', resolved_by='worker-failure',
                              resolved_at=?, updated_at=?, state_version=state_version+1
                       WHERE workflow_id=? AND intervention_type='RECONCILE_PROVIDER'
                         AND state IN ('OPEN','CLAIMED')""",
                    (now, now, workflow_id),
                )
            next_execution = "WAITING_RETRY" if is_tts_workflow else ("WAITING_USER" if unresolved else "WAITING_RETRY")
            next_status = next_execution
            con.execute(
                """UPDATE step_attempts
                   SET status=?, result_status='FAILED', error_code=?, error_details_json=?,
                       finished_at=?, state_version=state_version+1
                   WHERE workflow_id=? AND status IN ('CREATED','PREPARING','RUNNING','VERIFYING')""",
                (next_status, code,
                 canonical_json({"error_code": code, "message": message, "details": details}),
                 now, workflow_id),
            )
            con.execute(
                """UPDATE work_unit_attempts
                   SET status=?, finished_at=?, state_version=state_version+1
                   WHERE workflow_id=? AND status IN ('CREATED','PREPARING','RUNNING','VERIFYING')
                     AND side_effect_state NOT IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')""",
                (next_status, now, workflow_id),
            )
            con.execute(
                """UPDATE work_units SET status=?, state_version=state_version+1
                   WHERE workflow_id=? AND status IN ('PENDING','READY','RUNNING','VERIFYING')
                     AND side_effect_state NOT IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')""",
                (next_status, workflow_id),
            )
            con.execute(
                """UPDATE workflow_steps SET status=?, error_code=?, error_details_json=?,
                       state_version=state_version+1
                   WHERE workflow_id=? AND status IN ('PENDING','READY','PREPARING','RUNNING','VERIFYING')""",
                (next_status, code,
                 canonical_json({"error_code": code, "message": message, "details": details}), workflow_id),
            )
            con.execute(
                """UPDATE workflows SET execution_state=?, result_status='IN_PROGRESS',
                       last_error_code=?, last_error_message=?, updated_at=?, state_version=state_version+1
                   WHERE workflow_id=? AND execution_state <> 'TERMINAL'""",
                (next_execution, code, message, now, workflow_id),
            )
            after = _snapshot_from_connection(con, workflow_id)
            self.events.append_in_transaction(
                con,
                workflow_id,
                "GENERATION_TASK_FAILED",
                {
                    "error_code": code,
                    "message": message,
                    "details": details,
                    "workflow_state_version": after.state_version,
                    "execution_state": after.execution_state,
                },
                actor_type="WORKER",
                actor_id="generation-task",
            )
            final = _snapshot_from_connection(con, workflow_id)
            self.events.write_snapshot_in_transaction(con, workflow_id, final.as_dict())
            return final

    def complete_tts(
        self,
        plan: Mapping[str, Any],
        *,
        receipt_id: str,
        artifacts: list[Mapping[str, Any]],
        keep_workflow_open: bool = False,
    ) -> list[str]:
        """Bind verified Blob(s), close the attempt, and publish one snapshot."""

        if not artifacts:
            raise RepositoryError("verified TTS output is required", code="ARTIFACT_INVALID")
        now = utc_now()
        artifact_ids: list[str] = []
        with self.database.transaction() as con:
            self._assert_plan_lease(con, plan)
            unit = con.execute(
                "SELECT * FROM work_units WHERE workflow_id=? AND work_unit_id=?",
                (plan["workflow_id"], plan["work_unit_id"]),
            ).fetchone()
            if unit is None:
                raise NotFoundError("TTS work unit does not exist")
            if unit["status"] == "SUCCEEDED":
                return [str(row["artifact_id"]) for row in con.execute(
                    "SELECT artifact_id FROM artifacts WHERE workflow_id=? AND work_unit_id=? AND lifecycle_state='READY' AND verified=1 ORDER BY artifact_id",
                    (plan["workflow_id"], plan["work_unit_id"]),
                ).fetchall()]
            if unit["side_effect_state"] not in {"SUBMITTED", "CONFIRMED"}:
                raise ConflictError("TTS output cannot be verified before a provider receipt is observed")
            receipt = con.execute(
                """SELECT r.* FROM provider_receipts r
                   JOIN provider_receipt_bindings b
                     ON b.receipt_id=r.receipt_id
                    AND b.workflow_id=?
                    AND b.work_unit_id=?
                   WHERE r.receipt_id=?
                     AND r.workflow_group_id=?
                     AND r.provider_submission_id=?
                   LIMIT 1""",
                (
                    plan["workflow_id"], plan["work_unit_id"], receipt_id,
                    plan["workflow_group_id"], plan["submission_id"],
                ),
            ).fetchone()
            if receipt is None:
                raise ConflictError(
                    "provider receipt is not bound to this TTS plan",
                    code="PROVIDER_RECEIPT_SCOPE",
                )

            expected_segments = {
                str(row["item_id"]): str(row["work_unit_segment_id"])
                for row in con.execute(
                    "SELECT item_id, work_unit_segment_id FROM work_unit_segments WHERE work_unit_id=?",
                    (plan["work_unit_id"],),
                ).fetchall()
            }
            if not expected_segments:
                raise RepositoryError("TTS work unit has no item segment mapping", code="PERSISTENCE_ERROR")
            total_item_count = int(con.execute(
                "SELECT COUNT(*) FROM work_items WHERE workflow_id=?",
                (plan["workflow_id"],),
            ).fetchone()[0])
            seen_segments: set[str] = set()
            primary_count = 0
            for spec in artifacts:
                item_id = spec.get("item_id")
                segment_id = spec.get("work_unit_segment_id")
                if item_id is None and segment_id is None:
                    primary_count += 1
                    if primary_count > 1 or str(spec.get("artifact_type") or "") == "tts-segment":
                        raise RepositoryError("TTS artifact list has an invalid primary artifact", code="ARTIFACT_INVALID")
                    continue
                item_key = str(item_id or "")
                segment_key = str(segment_id or "")
                if item_key not in expected_segments or expected_segments[item_key] != segment_key:
                    raise RepositoryError("TTS artifact is not bound to its planned item segment", code="ARTIFACT_INVALID")
                if item_key in seen_segments:
                    raise RepositoryError("TTS item has more than one result artifact", code="ARTIFACT_INVALID")
                if str(spec.get("artifact_type") or "") != "tts-segment":
                    raise RepositoryError("TTS item artifact has an invalid artifact type", code="ARTIFACT_INVALID")
                seen_segments.add(item_key)
            if primary_count != 1 or seen_segments != set(expected_segments):
                raise RepositoryError("TTS artifact list does not cover the complete planned item set", code="ARTIFACT_INVALID")

            # The provider call and local staging can outlive a user command.
            # Acquire the final control-plane fence before publishing any
            # READY artifact. A cancel that wins this race keeps the local
            # workflow terminal and rejects the late publication.
            workflow = con.execute(
                "SELECT control_state, execution_state FROM workflows WHERE workflow_id=?",
                (plan["workflow_id"],),
            ).fetchone()
            if workflow is None:
                raise NotFoundError("workflow does not exist")
            if str(workflow["control_state"]) != "RUNNING":
                code = "WORKFLOW_CANCELLED" if str(workflow["control_state"]) in {"TERMINATING", "TERMINATED"} else "CONTROL_STATE_CONFLICT"
                raise ConflictError(
                    "workflow control state does not permit artifact publication",
                    code=code,
                    details={"control_state": str(workflow["control_state"]), "execution_state": str(workflow["execution_state"])},
                )

            primary_artifact_id: str | None = None
            persisted_artifacts: list[tuple[Mapping[str, Any], str]] = []
            for spec in artifacts:
                blob = spec.get("blob")
                if blob is None:
                    raise RepositoryError("artifact spec has no BlobInfo", code="ARTIFACT_INVALID")
                sha256 = str(blob.sha256)
                size_bytes = int(blob.size_bytes)
                fmt = str(blob.format)
                storage_key = str(blob.storage_key)
                blob_row = con.execute("SELECT * FROM artifact_blobs WHERE sha256=?", (sha256,)).fetchone()
                if blob_row is None:
                    blob_id = new_id("blob")
                    con.execute(
                        """INSERT INTO artifact_blobs(
                            blob_id, sha256, size_bytes, format, storage_key,
                            lifecycle_state, verified_at, created_at, deleted_at
                        ) VALUES (?,?,?,?,?,?,?,?,NULL)""",
                        (blob_id, sha256, size_bytes, fmt, storage_key, "READY", now, now),
                    )
                else:
                    if int(blob_row["size_bytes"]) != size_bytes or str(blob_row["storage_key"]) != storage_key or blob_row["lifecycle_state"] != "READY":
                        raise RepositoryError("content-addressed Blob fingerprint conflicts", code="ARTIFACT_INVALID")
                    blob_id = str(blob_row["blob_id"])
                artifact_id = str(spec.get("artifact_id") or new_id("artifact"))
                item_id = spec.get("item_id")
                segment_id = spec.get("work_unit_segment_id")
                con.execute(
                    """INSERT INTO artifacts(
                        artifact_id, workflow_id, item_id, step_id, attempt_id,
                        work_unit_id, work_unit_segment_id, source_import_id,
                        source_import_generation, source_import_generation_id, blob_id,
                        staging_ref, artifact_type, sha256, size_bytes, format,
                        producer, producer_version, verified, verified_at,
                        lifecycle_state, schema_version, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (artifact_id, plan["workflow_id"], item_id, plan["step_id"], plan["attempt_id"],
                     plan["work_unit_id"], segment_id, None, None, None, blob_id, None,
                     str(spec.get("artifact_type") or "tts-output"), sha256, size_bytes, fmt,
                     str(spec.get("producer") or "fake-provider"), str(spec.get("producer_version") or "1"),
                     1, now, "READY", "1", now, now),
                )
                artifact_ids.append(artifact_id)
                persisted_artifacts.append((spec, artifact_id))
                # The primary artifact is identified by its semantic shape,
                # not by list position.  Callers may persist segment rows in
                # any order, while parent_index (when supplied) still refers
                # to the actual persisted list order.
                if item_id is None and segment_id is None:
                    primary_artifact_id = artifact_id
                if segment_id:
                    con.execute(
                        """UPDATE work_unit_segments SET result_status='SUCCEEDED'
                           WHERE work_unit_id=? AND work_unit_segment_id=?""",
                        (plan["work_unit_id"], segment_id),
                    )
                if item_id:
                    con.execute(
                        """UPDATE work_unit_items SET result_status='SUCCEEDED', result_metadata_json=?, state_version=state_version+1
                           WHERE workflow_id=? AND work_unit_id=? AND item_id=?""",
                        (canonical_json({"artifact_id": artifact_id}), plan["workflow_id"], plan["work_unit_id"], item_id),
                    )
                    con.execute(
                        """UPDATE work_items SET status='SUCCEEDED',
                                  state_version=state_version+1, updated_at=?
                           WHERE workflow_id=? AND item_id=? AND status <> 'SKIPPED'""",
                        (now, plan["workflow_id"], item_id),
                    )
            if primary_artifact_id is None:
                raise RepositoryError("TTS artifact list has no primary artifact", code="ARTIFACT_INVALID")
            for spec, child_id in persisted_artifacts:
                if child_id == primary_artifact_id:
                    continue
                parent_index = spec.get("parent_index")
                if parent_index is None:
                    parent_id = primary_artifact_id
                else:
                    try:
                        parent_id = artifact_ids[int(parent_index)]
                    except (IndexError, TypeError, ValueError) as exc:
                        raise RepositoryError("artifact derivation parent is invalid", code="ARTIFACT_INVALID") from exc
                if parent_id and parent_id != child_id:
                    con.execute(
                        """INSERT OR IGNORE INTO artifact_derivations(
                            derivation_id, parent_artifact_id, child_artifact_id,
                            relation_type, derivation_version, derivation_context_hash, created_at
                        ) VALUES (?,?,?,?,?,?,?)""",
                        (new_id("derivation"), parent_id, child_id, str(spec.get("relation_type") or "CUT_SEGMENT"),
                         "1", content_hash({"work_unit_id": plan["work_unit_id"], "parent": parent_id}), now),
                    )
            # A targeted generation may intentionally contain only a subset
            # of the WorkItems. The workflow result is therefore based on all
            # verified item artifacts already owned by this run, not just the
            # current WorkUnit's plan. Preview runs retain their historical
            # terminal PARTIAL_SUCCESS result; targeted partial progress keeps
            # the run retryable until every WorkItem has a READY+verified
            # segment.
            verified_item_count = int(con.execute(
                """SELECT COUNT(DISTINCT a.item_id) FROM artifacts a
                   JOIN artifact_blobs b ON b.blob_id=a.blob_id
                   JOIN work_items i ON i.workflow_id=a.workflow_id AND i.item_id=a.item_id
                   WHERE a.workflow_id=? AND a.artifact_type='tts-segment'
                     AND a.item_id IS NOT NULL AND a.lifecycle_state='READY' AND a.verified=1
                     AND b.lifecycle_state='READY' AND i.status='SUCCEEDED'""",
                (plan["workflow_id"],),
            ).fetchone()[0])
            skipped_item_count = int(con.execute(
                "SELECT COUNT(*) FROM work_items WHERE workflow_id=? AND status='SKIPPED'",
                (plan["workflow_id"],),
            ).fetchone()[0])
            result_status = "SUCCEEDED" if verified_item_count + skipped_item_count >= total_item_count else "PARTIAL_SUCCESS"
            keep_open = bool(keep_workflow_open and result_status != "SUCCEEDED")
            con.execute(
                """UPDATE provider_receipts SET query_status='FOUND', confirmed_at=?, state_version=state_version+1
                   WHERE receipt_id=? AND workflow_group_id=?""",
                (now, receipt_id, plan["workflow_group_id"]),
            )
            con.execute(
                """UPDATE provider_submissions SET side_effect_state='CONFIRMED', confirmed_at=COALESCE(confirmed_at, ?),
                   state_version=state_version+1 WHERE provider_submission_id=?""",
                (now, plan["submission_id"]),
            )
            con.execute(
                """UPDATE work_units SET side_effect_state='CONFIRMED', status='SUCCEEDED', provider_receipt_ref=?,
                   state_version=state_version+1, finished_at=? WHERE workflow_id=? AND work_unit_id=?""",
                (receipt_id, now, plan["workflow_id"], plan["work_unit_id"]),
            )
            con.execute(
                """UPDATE work_unit_attempts SET side_effect_state='CONFIRMED', status='SUCCEEDED',
                   finished_at=?, state_version=state_version+1 WHERE workflow_id=? AND attempt_id=?""",
                (now, plan["workflow_id"], plan["attempt_id"]),
            )
            con.execute(
                """UPDATE step_attempts SET status='SUCCEEDED', result_status='SUCCEEDED', error_code=NULL,
                   error_details_json=NULL, finished_at=?, state_version=state_version+1
                   WHERE workflow_id=? AND attempt_id=?""",
                (now, plan["workflow_id"], plan["attempt_id"]),
            )
            output_reference = canonical_json({
                "artifact_ids": artifact_ids,
                "receipt_id": receipt_id,
                "result_status": result_status,
                "partial": keep_open,
            })
            if keep_open:
                con.execute(
                    """UPDATE workflow_steps SET status='WAITING_RETRY', output_reference_json=?,
                           error_code=NULL, retry_after=NULL, finished_at=NULL,
                           state_version=state_version+1
                       WHERE workflow_id=? AND step_id=?""",
                    (output_reference, plan["workflow_id"], plan["step_id"]),
                )
                con.execute(
                    """UPDATE workflows SET result_status='IN_PROGRESS', execution_state='WAITING_RETRY',
                           control_state='RUNNING', cleanup_state='SUCCEEDED',
                           last_error_code=NULL, last_error_message=NULL, finished_at=NULL,
                           updated_at=?, state_version=state_version+1
                       WHERE workflow_id=?""",
                    (now, plan["workflow_id"]),
                )
            else:
                con.execute(
                    """UPDATE workflow_steps SET status='SUCCEEDED', output_reference_json=?, finished_at=?,
                           state_version=state_version+1 WHERE workflow_id=? AND step_id=?""",
                    (output_reference, now, plan["workflow_id"], plan["step_id"]),
                )
                con.execute(
                    """UPDATE workflows SET result_status=?, execution_state='TERMINAL', control_state='TERMINATED', cleanup_state='SUCCEEDED',
                           last_error_code=NULL, last_error_message=NULL, finished_at=?, updated_at=?, state_version=state_version+1
                       WHERE workflow_id=?""",
                    (result_status, now, now, plan["workflow_id"]),
                )
            con.execute(
                "UPDATE side_effect_intents SET state='ARCHIVED', updated_at=? WHERE workflow_id=? AND work_unit_id=?",
                (now, plan["workflow_id"], plan["work_unit_id"]),
            )
            self.events.append_in_transaction(
                con, plan["workflow_id"], "TTS_OUTPUT_VERIFIED",
                {
                    "submission_id": plan["submission_id"],
                    "receipt_id": receipt_id,
                    "artifact_ids": artifact_ids,
                    "workflow_result_status": "IN_PROGRESS" if keep_open else result_status,
                    "partial": keep_open,
                },
                actor_type="WORKER", actor_id="workflow-engine", step_id=plan["step_id"], attempt_id=plan["attempt_id"],
            )
            snapshot = _snapshot_from_connection(con, plan["workflow_id"])
            self.events.write_snapshot_in_transaction(con, plan["workflow_id"], snapshot.as_dict())
        self.intent_log.mark(
            operation_namespace="tts",
            operation_key=str(plan.get("operation_key") or ""),
            state="ARCHIVED",
            intent_id=str(plan.get("intent_id") or "") or None,
        )
        return artifact_ids

    def record_tts_receipt(self, plan: Mapping[str, Any], receipt: Mapping[str, Any]) -> str:
        """Persist a provider receipt and move the local unit to VERIFYING."""

        receipt_id = str(receipt.get("receipt_id") or new_id("receipt"))
        provider = str(receipt["provider"])
        account_scope = str(receipt["account_scope"])
        canonical_key = str(receipt.get("canonical_key") or receipt.get("provider_job_id") or "")
        if not canonical_key:
            raise RepositoryError("provider receipt has no canonical key", code="PERSISTENCE_ERROR")
        now = utc_now()
        safe_summary = redact_public_json(receipt.get("summary") or {})
        summary = dict(safe_summary) if isinstance(safe_summary, Mapping) else {}
        with self.database.transaction() as con:
            self._assert_plan_lease(con, plan)
            workflow = con.execute(
                "SELECT control_state, execution_state FROM workflows WHERE workflow_id=?",
                (plan["workflow_id"],),
            ).fetchone()
            if workflow is None:
                raise NotFoundError("workflow does not exist")
            if str(workflow["control_state"]) != "RUNNING":
                code = "WORKFLOW_CANCELLED" if str(workflow["control_state"]) in {"TERMINATING", "TERMINATED"} else "CONTROL_STATE_CONFLICT"
                raise ConflictError(
                    "workflow control state does not permit provider receipt publication",
                    code=code,
                    details={
                        "control_state": str(workflow["control_state"]),
                        "execution_state": str(workflow["execution_state"]),
                    },
                )
            submission = con.execute(
                "SELECT * FROM provider_submissions WHERE provider_submission_id=?",
                (plan["submission_id"],),
            ).fetchone()
            if submission is None:
                raise NotFoundError("provider submission does not exist")
            if str(submission["provider"]) != provider or str(submission["provider_account_scope"]) != account_scope:
                raise ConflictError("provider receipt belongs to another account scope")
            existing = con.execute(
                "SELECT * FROM provider_receipts WHERE provider_submission_id=?",
                (plan["submission_id"],),
            ).fetchone()
            relation_type = "SUBMITTED"
            if existing is not None:
                if existing["canonical_key"] != canonical_key or existing["provider"] != provider or existing["provider_account_scope"] != account_scope:
                    raise ConflictError("provider submission has a conflicting receipt")
                receipt_id = str(existing["receipt_id"])
                relation_type = "REUSED"
            else:
                try:
                    con.execute(
                        """INSERT INTO provider_receipts(
                            receipt_id, workflow_group_id, provider_submission_id, provider,
                            provider_account_scope, canonical_key, query_status,
                            receipt_summary_json, state_version, created_at, confirmed_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (receipt_id, plan["workflow_group_id"], plan["submission_id"], provider,
                         account_scope, canonical_key, "FOUND", canonical_json(summary), 0, now, None),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError(f"provider receipt cannot be recorded: {exc}") from exc
                identifier = str(receipt.get("provider_job_id") or canonical_key)
                con.execute(
                    """INSERT OR IGNORE INTO provider_receipt_identifiers(
                        identifier_id, receipt_id, provider, provider_account_scope,
                        identifier_type, identifier_value, created_at
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (new_id("receipt-id"), receipt_id, provider, account_scope, "provider_job_id", identifier, now),
                )
            work_unit_attempt = con.execute(
                """SELECT work_unit_attempt_id FROM work_unit_attempts
                   WHERE workflow_id=? AND work_unit_id=? AND attempt_id=?
                   ORDER BY started_at DESC LIMIT 1""",
                (plan["workflow_id"], plan["work_unit_id"], plan["attempt_id"]),
            ).fetchone()
            work_unit_attempt_id = (
                str(work_unit_attempt["work_unit_attempt_id"])
                if work_unit_attempt is not None else None
            )
            binding_key = f"{plan['workflow_id']}:{plan['work_unit_id']}:{receipt_id}"
            existing_binding = con.execute(
                "SELECT * FROM provider_receipt_bindings WHERE binding_key=?",
                (binding_key,),
            ).fetchone()
            if existing_binding is not None:
                immutable_binding_fields = {
                    "receipt_id": str(receipt_id),
                    "workflow_id": str(plan["workflow_id"]),
                    "work_unit_id": str(plan["work_unit_id"]),
                }
                for field, expected in immutable_binding_fields.items():
                    if str(existing_binding[field]) != expected:
                        raise ConflictError(
                            "provider receipt binding conflicts with the existing run-local fact",
                            code="PROVIDER_RECEIPT_SCOPE",
                            details={"binding_key": binding_key, "field": field},
                        )
                if (
                    relation_type != "REUSED"
                    and str(existing_binding["observed_by_attempt_id"] or "") != str(plan["attempt_id"])
                ):
                    raise ConflictError(
                        "provider receipt binding conflicts with the existing run-local fact",
                        code="PROVIDER_RECEIPT_SCOPE",
                        details={"binding_key": binding_key, "field": "observed_by_attempt_id"},
                    )
                if (
                    str(existing_binding["relation_type"]) != relation_type
                    and {str(existing_binding["relation_type"]), relation_type} != {"SUBMITTED", "REUSED"}
                ):
                    raise ConflictError(
                        "provider receipt binding has a conflicting relation type",
                        code="PROVIDER_RECEIPT_SCOPE",
                        details={"binding_key": binding_key},
                    )
                existing_unit_attempt_id = existing_binding["work_unit_attempt_id"]
                if (
                    relation_type != "REUSED"
                    and existing_unit_attempt_id
                    and str(existing_unit_attempt_id) != str(work_unit_attempt_id)
                ):
                    raise ConflictError(
                        "provider receipt binding has a conflicting work-unit attempt",
                        code="PROVIDER_RECEIPT_SCOPE",
                        details={"binding_key": binding_key},
                    )
                con.execute(
                    """UPDATE provider_receipt_bindings
                       SET work_unit_attempt_id=COALESCE(work_unit_attempt_id, ?),
                           last_observed_at=?
                       WHERE binding_key=?""",
                    (work_unit_attempt_id, now, binding_key),
                )
            else:
                try:
                    con.execute(
                        """INSERT INTO provider_receipt_bindings(
                            binding_id, binding_key, receipt_id, workflow_id, work_unit_id,
                            work_unit_attempt_id, observed_by_attempt_id, relation_type,
                            first_observed_at, last_observed_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (new_id("receipt-binding"), binding_key, receipt_id,
                         plan["workflow_id"], plan["work_unit_id"], work_unit_attempt_id,
                         plan["attempt_id"], relation_type, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError(
                        "provider receipt binding cannot be recorded",
                        code="PROVIDER_RECEIPT_SCOPE",
                    ) from exc
            con.execute(
                """UPDATE provider_submissions SET side_effect_state='SUBMITTED', submitted_at=COALESCE(submitted_at, ?),
                   state_version=state_version+1 WHERE provider_submission_id=?""",
                (now, plan["submission_id"]),
            )
            con.execute(
                """UPDATE work_units SET provider_receipt_ref=?, side_effect_state='SUBMITTED', status='VERIFYING',
                   state_version=state_version+1 WHERE workflow_id=? AND work_unit_id=?""",
                (receipt_id, plan["workflow_id"], plan["work_unit_id"]),
            )
            con.execute(
                """UPDATE step_attempts SET status='VERIFYING', heartbeat_at=?, state_version=state_version+1
                   WHERE workflow_id=? AND attempt_id=?""",
                (now, plan["workflow_id"], plan["attempt_id"]),
            )
            con.execute(
                """UPDATE work_unit_attempts SET side_effect_state='SUBMITTED', status='VERIFYING', state_version=state_version+1
                   WHERE workflow_id=? AND attempt_id=?""",
                (plan["workflow_id"], plan["attempt_id"]),
            )
            self.events.append_in_transaction(
                con, plan["workflow_id"], "PROVIDER_RECEIPT_OBSERVED",
                {"submission_id": plan["submission_id"], "receipt_id": receipt_id},
                actor_type="WORKER", actor_id="workflow-engine", step_id=plan["step_id"], attempt_id=plan["attempt_id"],
            )
            snapshot = _snapshot_from_connection(con, plan["workflow_id"])
            self.events.write_snapshot_in_transaction(con, plan["workflow_id"], snapshot.as_dict())
        return receipt_id

    def transition_step(
        self,
        workflow_id: str,
        step_id: str,
        *,
        expected_state_version: int,
        target_status: str,
        error_code: str | None = None,
    ) -> None:
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT status, state_version FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                (workflow_id, step_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"step does not exist: {step_id}")
            require_expected(int(row["state_version"]), expected_state_version)
            transition_step(str(row["status"]), target_status)
            updated = con.execute(
                """UPDATE workflow_steps SET status=?, error_code=?, state_version=state_version+1,
                    started_at=CASE WHEN ? IN ('PREPARING','RUNNING') AND started_at IS NULL THEN ? ELSE started_at END,
                    finished_at=CASE WHEN ? IN ('SUCCEEDED','PERMANENT_FAILED','CANCELLED') THEN ? ELSE finished_at END
                    WHERE workflow_id=? AND step_id=? AND state_version=?""",
                (target_status, error_code, target_status, utc_now(), target_status, utc_now(),
                 workflow_id, step_id, expected_state_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("step changed while transitioning")

    def acquire_lease(
        self,
        workflow_id: str,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        *,
        ttl_seconds: int = 30,
        lease_id: str | None = None,
    ) -> tuple[str, int, str]:
        lease_id = lease_id or new_id("lease")
        now = utc_now()
        until = _expiry(ttl_seconds)
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT * FROM workflow_leases WHERE resource_type=? AND resource_id=?",
                (resource_type, resource_id),
            ).fetchone()
            if row is not None and row["state"] == "ACTIVE" and str(row["lease_until"]) > now and row["owner_id"] != owner_id:
                raise LeaseConflict("resource lease is held by another owner")
            token = int(row["fencing_token"]) + 1 if row is not None else 1
            if row is None:
                con.execute(
                    """INSERT INTO workflow_leases(
                        lease_id, workflow_id, resource_type, resource_id, owner_id,
                        fencing_token, lease_until, heartbeat_at, state
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (lease_id, workflow_id, resource_type, resource_id, owner_id, token, until, now, "ACTIVE"),
                )
            else:
                con.execute(
                    """UPDATE workflow_leases SET lease_id=?, workflow_id=?, owner_id=?,
                        fencing_token=?, lease_until=?, heartbeat_at=?, state='ACTIVE'
                        WHERE resource_type=? AND resource_id=?""",
                    (lease_id, workflow_id, owner_id, token, until, now, resource_type, resource_id),
                )
            return lease_id, token, until

    def renew_lease(
        self,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: int = 30,
    ) -> str:
        now = utc_now()
        until = _expiry(ttl_seconds)
        with self.database.transaction() as con:
            result = con.execute(
                """UPDATE workflow_leases SET lease_until=?, heartbeat_at=?
                    WHERE lease_id=? AND owner_id=? AND fencing_token=?
                      AND state='ACTIVE' AND lease_until>?""",
                (until, now, lease_id, owner_id, fencing_token, now),
            )
            if result.rowcount != 1:
                raise LeaseConflict("lease is stale or expired")
            return until

    def release_lease(self, lease_id: str, owner_id: str, fencing_token: int) -> None:
        with self.database.transaction() as con:
            result = con.execute(
                "UPDATE workflow_leases SET state='RELEASED', heartbeat_at=? WHERE lease_id=? AND owner_id=? AND fencing_token=? AND state='ACTIVE'",
                (utc_now(), lease_id, owner_id, fencing_token),
            )
            if result.rowcount != 1:
                raise LeaseConflict("lease is stale or already released")

    def reserve_budget(
        self,
        workflow_group_id: str,
        budget_key: str,
        *,
        budget_kind: str = "pure",
        max_attempts: int | None = None,
        max_elapsed_ms: int | None = None,
        deadline_at: str | None = None,
        policy_version: str = "1",
    ) -> str:
        if max_elapsed_ms is not None and int(max_elapsed_ms) < 0:
            raise RepositoryError("max_elapsed_ms must be non-negative", code="VALIDATION_ERROR")
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT * FROM retry_budgets WHERE workflow_group_id=? AND budget_key=?",
                (workflow_group_id, budget_key),
            ).fetchone()
            budget_id = str(row["retry_budget_id"]) if row else new_id("budget")
            if row is None:
                effective_deadline = deadline_at
                if max_elapsed_ms is not None:
                    elapsed_deadline = (
                        datetime.now(timezone.utc) + timedelta(milliseconds=int(max_elapsed_ms))
                    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    if effective_deadline is None or elapsed_deadline < effective_deadline:
                        effective_deadline = elapsed_deadline
                con.execute(
                    """INSERT INTO retry_budgets(
                        retry_budget_id, workflow_group_id, budget_kind, budget_key,
                        policy_version, max_attempts, max_elapsed_ms, deadline_at,
                        used_attempts, reserved_attempts, next_action_at,
                        last_decision, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (budget_id, workflow_group_id, budget_kind, budget_key, policy_version,
                     max_attempts, max_elapsed_ms, effective_deadline, 0, 0, None, None, utc_now()),
                )
            effective_deadline = row["deadline_at"] if row is not None else effective_deadline
            if effective_deadline and _is_expired(effective_deadline):
                raise BudgetExhausted("retry budget deadline has passed")
            updated = con.execute(
                """UPDATE retry_budgets SET reserved_attempts=reserved_attempts+1,
                    last_decision='RESERVED', updated_at=?
                    WHERE retry_budget_id=? AND
                    (max_attempts IS NULL OR used_attempts+reserved_attempts < max_attempts)""",
                (utc_now(), budget_id),
            )
            if updated.rowcount != 1:
                raise BudgetExhausted("retry budget is exhausted")
            return budget_id

    def commit_budget_use(self, budget_id: str) -> None:
        with self.database.transaction() as con:
            updated = con.execute(
                """UPDATE retry_budgets SET reserved_attempts=reserved_attempts-1,
                    used_attempts=used_attempts+1, last_decision='USED', updated_at=?
                    WHERE retry_budget_id=? AND reserved_attempts>0""",
                (utc_now(), budget_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("retry budget reservation is missing")

    def release_budget(self, budget_id: str) -> None:
        with self.database.transaction() as con:
            updated = con.execute(
                """UPDATE retry_budgets SET reserved_attempts=reserved_attempts-1,
                    last_decision='RELEASED', updated_at=?
                    WHERE retry_budget_id=? AND reserved_attempts>0""",
                (utc_now(), budget_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("retry budget reservation is missing")

    def begin_idempotency(
        self,
        *,
        scope: str,
        client_key: str,
        command_name: str,
        method: str,
        resource_id: str | None,
        target: Mapping[str, Any] | None,
        request: Mapping[str, Any],
        workflow_id: str | None = None,
        ttl_seconds: int = 86400,
        recovery: IdempotencyRecovery | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        scope_hash = content_hash(scope)
        target_json = canonical_json(dict(target or {}))
        request_hash = _idempotency_request_fingerprint(
            command_name=command_name,
            method=method,
            resource_id=resource_id,
            target_json=target_json,
            request=request,
        )
        orphan_row: dict[str, Any] | None = None
        created_id: str | None = None

        def replay_existing(row: sqlite3.Row | None, *, purge_expired: bool) -> tuple[str, dict[str, Any] | None] | None:
            nonlocal orphan_row
            if row is None:
                return None
            if purge_expired and _is_expired(row["expires_at"]):
                # A completed response is only a cache entry and can be
                # discarded after its TTL.  An expired pending row is also a
                # durable claim: keep it long enough for the recovery path to
                # distinguish an unknown outcome from a safe, expired claim.
                if row["response_json"] is not None:
                    con.execute(
                        "DELETE FROM workflow_idempotency_keys WHERE idempotency_id=?",
                        (row["idempotency_id"],),
                    )
                    return None
            # The metadata columns are part of the idempotency identity,
            # not merely audit fields.  Without these checks a client key
            # reused for two commands in the same scope could replay the
            # first command's response for the second one.
            metadata_matches = (
                str(row["command_name"]) == str(command_name)
                and str(row["method"]) == str(method)
                and row["resource_id"] == resource_id
                and str(row["target_json"] or "{}") == target_json
            )
            # Databases created before the composite fingerprint was
            # introduced contain a body-only hash. Preserve those safe
            # replays, but only after the durable metadata comparison.
            legacy_hash = content_hash(request)
            hash_matches = row["request_hash"] in {request_hash, legacy_hash}
            if not metadata_matches or not hash_matches:
                raise IdempotencyConflict("same idempotency key was used for a different request")
            if row["response_json"] is not None:
                return str(row["idempotency_id"]), json.loads(row["response_json"])
            idempotency_id = str(row["idempotency_id"])
            if self._idempotency_is_active(idempotency_id):
                raise IdempotencyInProgress(
                    "the same idempotency request is still in progress; retry after it completes"
                )
            # A pending row without a live owner belongs to a request that
            # may have exited after its domain transaction committed but
            # before response_json was saved.  Defer the decision until the
            # write transaction has closed so the recovery callback can read
            # the durable domain facts without nesting transactions.
            orphan_row = dict(row)
            return None

        try:
            with self.database.transaction() as con:
                row = con.execute(
                    "SELECT * FROM workflow_idempotency_keys WHERE scope_hash=? AND client_key=?",
                    (scope_hash, client_key),
                ).fetchone()
                existing = replay_existing(row, purge_expired=True)
                if existing is not None:
                    return existing
                if orphan_row is None:
                    idem_id = new_id("idempotency")
                    # Mark before INSERT so another request in this process
                    # cannot mistake the tiny commit window for an orphan.
                    self._idempotency_mark_active(idem_id)
                    created_id = idem_id
                    # A route may reserve an idempotency key before its domain
                    # lookup returns NOT_FOUND. Keep the association when the
                    # workflow exists, but do not violate the optional FK
                    # while preserving the route's intended 404 response.
                    stored_workflow_id = workflow_id
                    if stored_workflow_id is not None:
                        exists = con.execute(
                            "SELECT 1 FROM workflows WHERE workflow_id=?",
                            (stored_workflow_id,),
                        ).fetchone()
                        if exists is None:
                            stored_workflow_id = None
                    values = (
                        idem_id, scope_hash, client_key, command_name, method, resource_id,
                        target_json, request_hash, stored_workflow_id, None, None, _expiry(ttl_seconds), utc_now(),
                    )
                if orphan_row is None:
                    try:
                        con.execute(
                            """INSERT INTO workflow_idempotency_keys(
                                idempotency_id, scope_hash, client_key, command_name, method,
                                resource_id, target_json, request_hash, workflow_id,
                                response_status, response_json, expires_at, created_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            values,
                        )
                    except sqlite3.IntegrityError:
                        # BEGIN IMMEDIATE normally serializes this path, but a
                        # legacy caller or another process can still race the
                        # SELECT/INSERT window. Turn the UNIQUE loser into the
                        # same safe replay or in-progress result instead of
                        # leaking a 500.
                        if created_id is not None:
                            self._idempotency_forget(created_id)
                            created_id = None
                        competing = con.execute(
                            "SELECT * FROM workflow_idempotency_keys WHERE scope_hash=? AND client_key=?",
                            (scope_hash, client_key),
                        ).fetchone()
                        existing = replay_existing(competing, purge_expired=True)
                        if existing is not None:
                            return existing
                        if orphan_row is not None:
                            raise IdempotencyInProgress(
                                "the same idempotency request is still in progress; retry after it completes"
                            )
                        # The only reason the row can disappear here is an
                        # expired reservation observed during the defensive
                        # recheck. Retry the insert once; if another
                        # reservation wins that race, it is an in-progress
                        # request rather than a persistence fault.
                        idem_id = new_id("idempotency")
                        self._idempotency_mark_active(idem_id)
                        created_id = idem_id
                        stored_workflow_id = workflow_id
                        if stored_workflow_id is not None:
                            exists = con.execute(
                                "SELECT 1 FROM workflows WHERE workflow_id=?",
                                (stored_workflow_id,),
                            ).fetchone()
                            if exists is None:
                                stored_workflow_id = None
                        values = (
                            idem_id, scope_hash, client_key, command_name, method, resource_id,
                            target_json, request_hash, stored_workflow_id, None, None, _expiry(ttl_seconds), utc_now(),
                        )
                        try:
                            con.execute(
                                """INSERT INTO workflow_idempotency_keys(
                                    idempotency_id, scope_hash, client_key, command_name, method,
                                    resource_id, target_json, request_hash, workflow_id,
                                    response_status, response_json, expires_at, created_at
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                values,
                            )
                        except sqlite3.IntegrityError as retry_exc:
                            self._idempotency_forget(created_id)
                            created_id = None
                            raise IdempotencyInProgress(
                                "the same idempotency request is still in progress; retry after it completes"
                            ) from retry_exc
        except Exception:
            if created_id is not None:
                self._idempotency_forget(created_id)
            raise

        if created_id is not None:
            return created_id, None
        if orphan_row is None:
            raise RepositoryError("idempotency reservation could not be established")
        if recovery is None:
            raise IdempotencyInProgress(
                "the same idempotency request is still in progress; retry after it completes"
            )
        return self._recover_or_reclaim_idempotency(
            orphan_row,
            recovery=recovery,
            scope_hash=scope_hash,
            client_key=client_key,
            command_name=command_name,
            method=method,
            resource_id=resource_id,
            target_json=target_json,
            request_hash=request_hash,
            workflow_id=workflow_id,
            ttl_seconds=ttl_seconds,
        )

    def _recover_or_reclaim_idempotency(
        self,
        orphan_row: Mapping[str, Any],
        *,
        recovery: IdempotencyRecovery,
        scope_hash: str,
        client_key: str,
        command_name: str,
        method: str,
        resource_id: str | None,
        target_json: str,
        request_hash: str,
        workflow_id: str | None,
        ttl_seconds: int,
    ) -> tuple[str, dict[str, Any] | None]:
        recovered = recovery(orphan_row)
        if recovered is not None:
            response_status, response = recovered
            if not isinstance(response_status, int) or not isinstance(response, Mapping):
                raise IdempotencyInProgress(
                    "the committed idempotency response cannot be reconstructed safely"
                )
            response_value = dict(response)
            with self.database.transaction() as con:
                current = con.execute(
                    "SELECT * FROM workflow_idempotency_keys WHERE idempotency_id=?",
                    (orphan_row["idempotency_id"],),
                ).fetchone()
                if current is None:
                    raise IdempotencyInProgress(
                        "the idempotency reservation changed while recovering"
                    )
                if current["response_json"] is not None:
                    return str(current["idempotency_id"]), json.loads(current["response_json"])
                updated = con.execute(
                    """UPDATE workflow_idempotency_keys
                       SET response_status=?, response_json=?,
                           workflow_id=COALESCE(?, workflow_id)
                       WHERE idempotency_id=? AND response_json IS NULL""",
                    (
                        response_status,
                        canonical_json(response_value),
                        workflow_id,
                        orphan_row["idempotency_id"],
                    ),
                )
                if updated.rowcount != 1:
                    raise IdempotencyInProgress(
                        "the idempotency reservation changed while recovering"
                    )
            self._idempotency_forget(str(orphan_row["idempotency_id"]))
            return str(orphan_row["idempotency_id"]), response_value

        # No durable outcome was found.  An unexpired pending row may still be
        # owned by a live request in another process; reclaiming it here could
        # execute the same mutation twice.  Only the durable claim expiry
        # permits a fresh reservation.
        if not _is_expired(orphan_row.get("expires_at")):
            raise IdempotencyInProgress(
                "the same idempotency request is still in progress; retry after it completes"
            )

        # Replace only this exact expired pending row with a fresh reservation.
        # The second transaction rechecks the row so a concurrent completion
        # is replayed instead of being deleted.
        fresh_id = new_id("idempotency")
        self._idempotency_mark_active(fresh_id)
        try:
            with self.database.transaction() as con:
                current = con.execute(
                    "SELECT * FROM workflow_idempotency_keys WHERE scope_hash=? AND client_key=?",
                    (scope_hash, client_key),
                ).fetchone()
                if current is None:
                    raise IdempotencyInProgress("the idempotency reservation changed while recovering")
                if current["response_json"] is not None:
                    self._idempotency_forget(fresh_id)
                    return str(current["idempotency_id"]), json.loads(current["response_json"])
                if str(current["idempotency_id"]) != str(orphan_row["idempotency_id"]):
                    raise IdempotencyInProgress("the same idempotency request is still in progress; retry after it completes")
                con.execute(
                    "DELETE FROM workflow_idempotency_keys WHERE idempotency_id=? AND response_json IS NULL",
                    (orphan_row["idempotency_id"],),
                )
                stored_workflow_id = workflow_id or current["workflow_id"]
                if stored_workflow_id is not None and con.execute(
                    "SELECT 1 FROM workflows WHERE workflow_id=?", (stored_workflow_id,)
                ).fetchone() is None:
                    stored_workflow_id = None
                try:
                    con.execute(
                        """INSERT INTO workflow_idempotency_keys(
                            idempotency_id, scope_hash, client_key, command_name, method,
                            resource_id, target_json, request_hash, workflow_id,
                            response_status, response_json, expires_at, created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            fresh_id, scope_hash, client_key, command_name, method, resource_id,
                            target_json, request_hash, stored_workflow_id, None, None,
                            _expiry(ttl_seconds), utc_now(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise IdempotencyInProgress(
                        "the same idempotency request is still in progress; retry after it completes"
                    ) from exc
        except Exception:
            self._idempotency_forget(fresh_id)
            raise
        return fresh_id, None

    def _idempotency_is_active(self, idempotency_id: str) -> bool:
        with self._idempotency_activity_lock:
            return idempotency_id in self._active_idempotency_ids

    def _idempotency_mark_active(self, idempotency_id: str) -> None:
        with self._idempotency_activity_lock:
            self._active_idempotency_ids.add(str(idempotency_id))

    def _idempotency_forget(self, idempotency_id: str) -> None:
        with self._idempotency_activity_lock:
            self._active_idempotency_ids.discard(str(idempotency_id))

    def abandon_idempotency(self, *, client_key: str, resource_id: str | None = None) -> int:
        """Release a reservation whose route failed before a response was saved.

        A completed key is never touched.  Route exception handlers pass the
        path resource when available; create-workflow reservations use the
        NULL resource branch.  This makes a failed request retryable without
        allowing a second concurrent request to reuse an active reservation.
        """

        with self.database.transaction() as con:
            if resource_id is None:
                rows = con.execute(
                    "SELECT idempotency_id FROM workflow_idempotency_keys "
                    "WHERE client_key=? AND response_json IS NULL AND resource_id IS NULL "
                    "LIMIT 2",
                    (client_key,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT idempotency_id FROM workflow_idempotency_keys "
                    "WHERE client_key=? AND resource_id=? AND response_json IS NULL "
                    "LIMIT 2",
                    (client_key, resource_id),
                ).fetchall()
            # The exception handler does not know the idempotency scope.  If
            # the same client key/resource has reservations in multiple
            # scopes, deleting one arbitrarily could release a live request.
            # Fail closed and let the durable claims expire/reconcile instead.
            if len(rows) != 1:
                return 0
            result = con.execute(
                "DELETE FROM workflow_idempotency_keys "
                "WHERE idempotency_id=? AND response_json IS NULL",
                (rows[0]["idempotency_id"],),
            )
            deleted = int(result.rowcount)
            if deleted:
                self._idempotency_forget(str(rows[0]["idempotency_id"]))
            return deleted

    def complete_idempotency(
        self,
        idempotency_id: str,
        *,
        response_status: int,
        response: Mapping[str, Any],
        workflow_id: str | None = None,
    ) -> None:
        with self.database.transaction() as con:
            updated = con.execute(
                """UPDATE workflow_idempotency_keys SET response_status=?, response_json=?,
                    workflow_id=COALESCE(?, workflow_id) WHERE idempotency_id=?""",
                (response_status, canonical_json(dict(response)), workflow_id, idempotency_id),
            )
            if updated.rowcount != 1:
                raise NotFoundError(f"idempotency key does not exist: {idempotency_id}")
        self._idempotency_forget(idempotency_id)

    def record_side_effect_intent(
        self,
        workflow_id: str,
        *,
        operation_namespace: str,
        operation_key: str,
        payload: Mapping[str, Any],
        step_id: str | None = None,
        attempt_id: str | None = None,
        work_unit_id: str | None = None,
        work_unit_attempt_id: str | None = None,
        provider_account_scope: str | None = None,
    ) -> str:
        payload_hash = content_hash(payload)
        intent_id = self.intent_log.record(
            operation_namespace=operation_namespace,
            operation_key=operation_key,
            payload_hash=payload_hash,
            workflow_id=workflow_id,
            step_id=step_id,
            attempt_id=attempt_id,
            work_unit_id=work_unit_id,
            provider_account_scope=provider_account_scope,
        )
        with self._transaction_after_intent(
            operation_namespace=operation_namespace,
            operation_key=operation_key,
            payload_hash=payload_hash,
            intent_id=intent_id,
        ) as con:
            row = con.execute(
                "SELECT * FROM side_effect_intents WHERE operation_namespace=? AND operation_key=?",
                (operation_namespace, operation_key),
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    raise IdempotencyConflict("side-effect key is bound to another payload")
                return str(row["intent_id"])
            try:
                con.execute(
                    """INSERT INTO side_effect_intents(
                        intent_id, workflow_id, step_id, attempt_id, work_unit_id,
                        work_unit_attempt_id, operation_namespace, operation_key,
                        payload_hash, provider_account_scope, state, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (intent_id, workflow_id, step_id, attempt_id, work_unit_id,
                     work_unit_attempt_id, operation_namespace, operation_key,
                     payload_hash, provider_account_scope, "RECORDED", utc_now(), utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"side-effect intent cannot be recorded: {exc}") from exc
            return intent_id

    def bind_provider_receipt(
        self,
        *,
        binding_key: str,
        receipt_id: str,
        workflow_id: str,
        work_unit_id: str,
        relation_type: str = "OBSERVED",
        work_unit_attempt_id: str | None = None,
        observed_by_attempt_id: str | None = None,
    ) -> str:
        binding_id = new_id("receipt-binding")
        now = utc_now()
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT * FROM provider_receipt_bindings WHERE binding_key=?",
                (binding_key,),
            ).fetchone()
            if row is not None:
                same = all(row[key] == value for key, value in {
                    "receipt_id": receipt_id,
                    "workflow_id": workflow_id,
                    "work_unit_id": work_unit_id,
                    "work_unit_attempt_id": work_unit_attempt_id,
                    "observed_by_attempt_id": observed_by_attempt_id,
                    "relation_type": relation_type,
                }.items())
                if not same:
                    raise ConflictError("receipt binding key points to another relation")
                return str(row["binding_id"])
            try:
                con.execute(
                    """INSERT INTO provider_receipt_bindings(
                        binding_id, binding_key, receipt_id, workflow_id, work_unit_id,
                        work_unit_attempt_id, observed_by_attempt_id, relation_type,
                        first_observed_at, last_observed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (binding_id, binding_key, receipt_id, workflow_id, work_unit_id,
                     work_unit_attempt_id, observed_by_attempt_id, relation_type, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"receipt binding cannot be created: {exc}") from exc
            return binding_id

    def get_workflow_for_target(self, target: CommandTarget | Mapping[str, object]) -> str:
        """Resolve a target's run, rejecting group-wide receipt ambiguity."""
        parsed = validate_target(target)
        with self.database.read_transaction() as con:
            if parsed.target_type == "STEP":
                query, params = "SELECT workflow_id FROM workflow_steps WHERE step_id=?", (parsed.step_id,)
            elif parsed.target_type == "ITEM":
                query, params = "SELECT workflow_id FROM work_items WHERE item_id=?", (parsed.item_id,)
            elif parsed.target_type == "WORK_UNIT":
                query, params = "SELECT workflow_id FROM work_units WHERE work_unit_id=?", (parsed.work_unit_id,)
            elif parsed.target_type == "WORK_UNIT_ATTEMPT":
                query, params = "SELECT workflow_id FROM work_unit_attempts WHERE work_unit_attempt_id=?", (parsed.work_unit_attempt_id,)
            elif parsed.target_type == "EXTERNAL_OPERATION":
                if con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_operations'"
                ).fetchone() is None:
                    raise RepositoryError(
                        "external operation targets require the Full workflow profile",
                        code="EXTERNAL_CAPABILITY_REQUIRED",
                    )
                query, params = "SELECT workflow_id FROM external_operations WHERE external_operation_id=?", (parsed.external_operation_id,)
            else:
                query, params = (
                    """SELECT DISTINCT b.workflow_id FROM provider_receipt_bindings b
                        JOIN provider_receipts r ON r.receipt_id=b.receipt_id
                        WHERE b.receipt_id=?""",
                    (parsed.provider_receipt_id,),
                )
            rows = con.execute(query, params).fetchall()
            workflow_ids = {str(row["workflow_id"]) for row in rows}
            if not workflow_ids:
                raise NotFoundError("target does not exist")
            if len(workflow_ids) > 1:
                raise ConflictError("target is shared by multiple runs; a run-local binding is required", code="TARGET_REQUIRED")
            return next(iter(workflow_ids))

    @staticmethod
    def target_json(target: CommandTarget | Mapping[str, object]) -> str:
        return canonical_json(validate_target(target).as_dict())
