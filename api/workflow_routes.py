"""Thin `/api/v1` routes over the durable workflow services.

The legacy `/api/*` surface is intentionally not imported here.  This module
is safe to mount alongside it while the Electron renderer is migrated, but all
new clients use only the versioned routes below.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app_paths import ensure_data_dir
from audio_naming import audio_filename_from_stem
from application.workflow_service import WorkflowApplicationService
from db.migration_runner import MigrationError
from workflow.artifact_store import ArtifactStore, ArtifactStoreError, ArtifactTooLarge
from workflow.data_safety import redact_public_json
from workflow.database import WorkflowDatabase
from workflow.domain import DomainError, content_hash, new_id
from workflow.event_store import CursorExpired, EventStoreError, InvalidCursor
from workflow.external import ExternalRecordService, ExternalSubmission, ExternalLease
from workflow.garbage_collector import ArtifactGarbageCollector
from workflow.parser import LegacyWordParser
from workflow.providers import ProviderError, ProviderRegistry, XunfeiTTSAdapter
from workflow.recovery import RecoveryService
from workflow.repositories import (
    ConflictError,
    IdempotencyConflict,
    IdempotencyInProgress,
    NotFoundError,
    RepositoryError,
    WorkflowRepository,
    _snapshot_from_connection,
)
from workflow.scheduler import PersistentScheduler
from workflow.security import OneTimeTicketManager, TicketError, TicketExpired, verify_capability
from workflow.source_imports import SourceImportService
from workflow.workspace import (
    DELIVERABLE_AUDIO_FORMAT,
    WORKSPACE_CONTENT_DETAIL_LIMIT,
    artifact_blob_facts_match,
    item_content_id,
)


logger = logging.getLogger(__name__)


class WorkflowCreateBody(BaseModel):
    workflow_type: str
    business_key: str | None = None
    configuration: dict[str, Any]

    class Config:
        extra = "forbid"


class WorkflowPatchBody(BaseModel):
    expected_state_version: int
    configuration_revision: int | None = Field(default=None, ge=1)
    configuration: dict[str, Any] | None = None
    item_overrides: list[dict[str, Any]] | None = None

    class Config:
        extra = "forbid"


class WorkflowCommandBody(BaseModel):
    expected_state_version: int
    reason: str | None = None

    class Config:
        extra = "forbid"


class ParseBody(BaseModel):
    expected_state_version: int
    source_artifact_id: str | None = None

    class Config:
        extra = "forbid"


class GenerateBody(BaseModel):
    expected_state_version: int
    configuration_revision: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=1000)
    generation_mode: Literal["composite_cut", "single_segment"] | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    account_scope: str | None = Field(default=None, min_length=1, max_length=256)
    item_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
    )

    @field_validator("item_ids")
    @classmethod
    def item_ids_must_be_unique(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("item_ids must contain unique item ids")
        return value

    class Config:
        extra = "forbid"


class ExportZipBody(BaseModel):
    expected_state_version: int
    include_item_ids: list[str] | None = Field(default=None, min_length=1, max_length=2000)

    class Config:
        extra = "forbid"


class ArchiveBody(BaseModel):
    expected_state_version: int
    reason: str | None = None

    class Config:
        extra = "forbid"


class TargetedCommandBody(BaseModel):
    expected_state_version: int
    expected_target_state_version: int
    target: dict[str, Any]
    expected_attempt_id: str | None = None
    reason: str | None = None

    class Config:
        extra = "forbid"


class SourceImportBody(BaseModel):
    metadata: dict[str, Any]
    expected_size_bytes: int | None = None
    expected_sha256: str | None = None
    content_type: str | None = None

    class Config:
        extra = "forbid"


class GenerationBody(BaseModel):
    expected_state_version: int

    class Config:
        extra = "forbid"


class SourceImportCommandBody(BaseModel):
    expected_state_version: int
    reason: str | None = None

    class Config:
        extra = "forbid"


class RerunBody(BaseModel):
    expected_group_state_version: int
    source_workflow_id: str | None = None
    reason: str | None = None

    class Config:
        extra = "forbid"


class ResolveEvidenceBody(BaseModel):
    source: str = Field(min_length=1, max_length=256)
    evidence_hash: str = Field(min_length=16, max_length=128)
    reference: str | None = Field(default=None, max_length=512)
    summary: str | None = Field(default=None, max_length=2000)

    class Config:
        extra = "forbid"


class ResolveBody(BaseModel):
    expected_state_version: int
    expected_target_state_version: int
    target: dict[str, Any]
    decision: str
    evidence: ResolveEvidenceBody

    class Config:
        extra = "forbid"


class EventTicketBody(BaseModel):
    last_event_id: str | None

    class Config:
        extra = "forbid"


class ExternalRecordBody(BaseModel):
    external_system: str = Field(min_length=1, max_length=128)
    account_scope: str = Field(min_length=1, max_length=256)
    business_record_key: str = Field(min_length=1, max_length=512)
    mapping_version: str = Field(min_length=1, max_length=128)
    item_id: str | None = None

    class Config:
        extra = "forbid"


class ExternalLeaseBody(BaseModel):
    owner_id: str = Field(min_length=1, max_length=256)
    ttl_seconds: int = Field(default=60, ge=1, le=300)

    class Config:
        extra = "forbid"


class ExternalOperationBody(BaseModel):
    mapping_id: str = Field(min_length=1, max_length=128)
    operation_key: str = Field(min_length=1, max_length=512)
    payload: dict[str, Any]
    mapping_version: str = Field(min_length=1, max_length=128)
    item_id: str | None = None

    class Config:
        extra = "forbid"


class ExternalLeaseReferenceBody(BaseModel):
    lease_id: str = Field(min_length=1, max_length=128)
    mapping_id: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=256)
    fencing_token: int = Field(ge=1)

    class Config:
        extra = "forbid"


class ExternalSubmitBody(BaseModel):
    lease: ExternalLeaseReferenceBody
    external_record_id: str = Field(min_length=1, max_length=512)
    canonical_key: str = Field(min_length=1, max_length=512)
    summary: dict[str, Any] = Field(default_factory=dict)

    class Config:
        extra = "forbid"


class ExternalResolveBody(BaseModel):
    decision: str
    evidence: ResolveEvidenceBody
    resolved_by: str = Field(default="desktop", min_length=1, max_length=256)

    class Config:
        extra = "forbid"


@dataclass
class WorkflowRuntime:
    database: WorkflowDatabase
    repository: WorkflowRepository
    artifacts: ArtifactStore
    imports: SourceImportService
    external: ExternalRecordService
    application: WorkflowApplicationService
    providers: ProviderRegistry
    tickets: OneTimeTicketManager
    capability: str | None = None
    initialized: bool = False
    generation_tasks: set[asyncio.Task] = field(default_factory=set)
    generation_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    generation_dispatch_guard: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation_tasks_by_workflow: dict[str, asyncio.Task] = field(default_factory=dict)
    generation_slot_owners: dict[str, asyncio.Task] = field(default_factory=dict)
    generation_cancel_events: dict[str, threading.Event] = field(default_factory=dict)
    recovery: RecoveryService | None = None
    scheduler: PersistentScheduler | None = None
    garbage_collector: ArtifactGarbageCollector | None = None
    startup_recovery_findings: tuple[Any, ...] = field(default_factory=tuple)
    startup_recovery_error: str | None = None
    startup_scheduler_claims: tuple[Any, ...] = field(default_factory=tuple)
    startup_gc_findings: tuple[Any, ...] = field(default_factory=tuple)
    initialization_lock: threading.Lock = field(default_factory=threading.Lock)
    auto_retry_enabled: bool = True
    scheduler_task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        # A directly constructed runtime must be protected just like the
        # desktop startup path.  Leaving capability unset would turn the
        # versioned router into an unauthenticated loopback service.
        if not self.capability:
            self.capability = secrets.token_urlsafe(32)

    @classmethod
    def from_paths(
        cls,
        database_path: str | os.PathLike[str],
        artifact_root: str | os.PathLike[str],
        *,
        capability: str | None = None,
        profile: str = "2a",
        allow_real: bool = True,
        auto_retry_enabled: bool = True,
    ) -> "WorkflowRuntime":
        database = WorkflowDatabase(database_path, profile=profile)
        artifacts = ArtifactStore(artifact_root)
        tickets = OneTimeTicketManager(max_ttl_seconds=300)
        repository = WorkflowRepository(database)
        providers = ProviderRegistry()
        providers.register(XunfeiTTSAdapter(
            account_scope=os.environ.get("WORDTTS_XUNFEI_ACCOUNT_SCOPE", "xunfei-default"),
            # Formal runtimes use the real Provider by default.  Logical
            # smoke/tests pass allow_real=False explicitly and never touch a
            # browser or third-party page.
            allow_real=allow_real,
        ))
        application = WorkflowApplicationService(
            repository,
            SourceImportService(database, artifacts, ticket_manager=tickets, event_store=repository.events),
            artifacts,
            parser=LegacyWordParser(),
            providers=providers,
        )
        return cls(
            database=database,
            repository=repository,
            artifacts=artifacts,
            imports=application.source_imports,
            external=ExternalRecordService(database, intent_log=repository.intent_log),
            application=application,
            providers=providers,
            tickets=tickets,
            capability=capability or secrets.token_urlsafe(32),
            recovery=RecoveryService(database),
            scheduler=PersistentScheduler(database, event_store=repository.events),
            garbage_collector=ArtifactGarbageCollector(database, artifacts),
            auto_retry_enabled=auto_retry_enabled,
        )

    def ensure_initialized(self) -> None:
        if self.initialized:
            self._start_scheduler_if_possible()
            return
        with self.initialization_lock:
            if self.initialized:
                self._start_scheduler_if_possible()
                return
            try:
                self.repository.initialize()
            except MigrationError as exc:
                # A broken frozen bundle must not turn the first workflow
                # request into an opaque ASGI HTTP 500.  Keep the underlying
                # exception for controlled server diagnostics while exposing
                # the stable contract error used by the recovery UI.
                raise RepositoryError(
                    "workflow schema initialization failed",
                    code="MIGRATION_ERROR",
                ) from exc
            except RuntimeError as exc:
                # A second backend instance or an unavailable data directory
                # is an expected fail-closed condition, not an opaque 500.
                raise RepositoryError(
                    "workflow data directory is unavailable",
                    code="PERSISTENCE_ERROR",
                ) from exc
            recovery = self.recovery or RecoveryService(self.database)
            scheduler = self.scheduler or PersistentScheduler(self.database, event_store=self.repository.events)
            garbage_collector = self.garbage_collector or ArtifactGarbageCollector(self.database, self.artifacts)
            self.recovery = recovery
            self.scheduler = scheduler
            self.garbage_collector = garbage_collector

            # Startup maintenance is deliberately best-effort.  A crashed
            # worker may leave one malformed legacy row behind, but that row
            # must not make the whole application unable to import a new
            # document.  Each maintenance task is isolated and can be retried
            # on the next process start; schema initialization above remains
            # fail-closed because the API cannot operate without a database.
            self.startup_recovery_error = None
            try:
                # Recovery is safe-only: it expires local leases, converts TTS
                # uncertainty into a local retryable state, and keeps only
                # external work on the AMBIGUOUS/reconciliation path.
                self.startup_recovery_findings = tuple(recovery.apply_safe_recovery())
            except Exception:
                self.startup_recovery_findings = tuple()
                self.startup_recovery_error = "workflow startup recovery failed"
                logger.exception("workflow startup recovery failed; continuing with the local API")

            try:
                self.startup_scheduler_claims = tuple(scheduler.expire_interventions())
            except Exception:
                self.startup_scheduler_claims = tuple()
                logger.exception("startup intervention expiry failed; continuing with the local API")

            try:
                self.startup_gc_findings = tuple(garbage_collector.collect(limit=32))
            except Exception:
                self.startup_gc_findings = tuple()
                logger.exception("startup artifact cleanup failed; continuing with the local API")
            self.initialized = True
        self._start_scheduler_if_possible()

    def _start_scheduler_if_possible(self) -> None:
        """Attach one event-loop scheduler when the API is serving requests."""

        if not self.auto_retry_enabled or self.scheduler_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous migration/unit-test callers still get the durable
            # scheduler object and can call claim_due_retries explicitly.
            return
        self.scheduler_task = loop.create_task(
            _automatic_retry_loop(self),
            name="wordtts-provider-aware-retry-scheduler",
        )


def _request_id() -> str:
    return new_id("request")


def _expires_at_iso(ttl_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error_payload(
    exc: Exception,
    *,
    request_id: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    code = getattr(exc, "code", "INTERNAL_ERROR")
    safe_details = redact_public_json(getattr(exc, "details", {}) or {})
    details = dict(safe_details) if isinstance(safe_details, Mapping) else {}
    path_params = getattr(request, "path_params", {}) if request is not None else {}

    def context_id(name: str) -> str | None:
        value = getattr(exc, name, None) or details.get(name) or path_params.get(name)
        if value is None:
            return None
        return str(value)[:256]

    safe_message = redact_public_json(str(exc)[:2000])
    return {
        "request_id": request_id or _request_id(),
        "error_code": code,
        "message": safe_message if isinstance(safe_message, str) else "request failed",
        "retryable": code in {
            "TRANSIENT_PROVIDER_ERROR", "PROVIDER_RATE_LIMITED", "PROVIDER_LOGIN_REQUIRED", "PERSISTENCE_ERROR",
            "PERSISTENCE_AMBIGUOUS", "RESOURCE_EXHAUSTED",
        },
        "side_effect_occurred": code in {
            "SUBMISSION_AMBIGUOUS", "PERSISTENCE_AMBIGUOUS", "STALE_ATTEMPT",
            "EXTERNAL_VERIFY_MISMATCH", "EXTERNAL_RECONCILIATION_REQUIRED",
        },
        "workflow_id": context_id("workflow_id"),
        "step_id": context_id("step_id"),
        "attempt_id": context_id("attempt_id"),
        "details": details,
    }


def _status_for_error(exc: Exception) -> int:
    code = getattr(exc, "code", "INTERNAL_ERROR")
    if code in {"UNAUTHORIZED", "CURSOR_INVALID"}:
        return 401 if code == "UNAUTHORIZED" else 409
    if code == "NOT_FOUND":
        return 404
    if code in {"CURSOR_EXPIRED"}:
        return 410
    if isinstance(exc, ArtifactTooLarge):
        return 413
    if code in {"RESOURCE_EXHAUSTED", "PROVIDER_RATE_LIMITED"}:
        return 429
    if code == "ITEM_CONTENT_TOO_LARGE":
        return 413
    if code == "INSUFFICIENT_STORAGE":
        return 507
    if code in {"EXTERNAL_CAPABILITY_REQUIRED", "PROVIDER_UNAVAILABLE"}:
        return 503
    if code in {"PERSISTENCE_ERROR", "MIGRATION_ERROR", "MIGRATION_REQUIRED"}:
        return 503
    if code == "VALIDATION_ERROR":
        return 400
    return 409


def _response_headers(status_code: int) -> dict[str, str]:
    return {"Retry-After": "1"} if status_code == 429 else {}


def _release_failed_idempotency(request: Request, exc: Exception) -> None:
    """Make a failed mutation retryable without releasing active claims."""

    # Keep the claim when the local outcome is unknown or the operation has
    # crossed a side-effect/reconciliation boundary.  Deleting it in these
    # cases would let a retry submit the same external operation again (or use
    # the same client key for a different request) while the first result is
    # still unresolved.  A normal validation/state conflict remains safe to
    # release and retry.
    if getattr(exc, "code", None) in {
        "IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_IN_PROGRESS",
        "PERSISTENCE_ERROR", "PERSISTENCE_AMBIGUOUS", "MIGRATION_ERROR", "MIGRATION_REQUIRED",
        "SUBMISSION_AMBIGUOUS", "TTS_SUBMISSION_AMBIGUOUS", "STALE_ATTEMPT",
        "EXTERNAL_VERIFY_MISMATCH", "EXTERNAL_RECONCILIATION_REQUIRED", "EXTERNAL_SUBMIT_UNKNOWN",
    }:
        return
    key = request.headers.get("X-Idempotency-Key")
    if not key or not (16 <= len(key) <= 256):
        return
    runtime = getattr(request.app.state, "workflow_runtime", None)
    if runtime is None or not getattr(runtime, "initialized", False):
        return
    path_params = getattr(request, "path_params", {}) or {}
    resource_id = next(
        (str(path_params[name]) for name in (
            "workflow_id", "import_id", "artifact_id", "external_record_id", "mapping_id",
            "attempt_id", "operation_id",
        ) if path_params.get(name)),
        None,
    )
    try:
        runtime.repository.abandon_idempotency(client_key=key, resource_id=resource_id)
    except Exception:
        # The original route error is more useful than cleanup diagnostics.
        pass


def _workflow_envelope(snapshot, request_id: str | None = None) -> dict[str, Any]:
    return {"request_id": request_id or _request_id(), "workflow": snapshot.as_dict()}


def _command_response(snapshot, action: str, request_id: str | None = None, **extra: Any) -> dict[str, Any]:
    latest_event = snapshot.latest_event or {}
    latest_payload = latest_event.get("payload") if isinstance(latest_event, Mapping) else None
    target_attempt_id = (
        latest_payload.get("reconcile_attempt_id")
        if isinstance(latest_payload, Mapping)
        else None
    )
    response = {
        "request_id": request_id or _request_id(),
        "workflow_id": snapshot.workflow_id,
        "accepted_action": action,
        "result_status": snapshot.result_status,
        "execution_state": snapshot.execution_state,
        "control_state": snapshot.control_state,
        "cleanup_state": snapshot.cleanup_state,
        "state_version": snapshot.state_version,
        "current_snapshot": snapshot.as_dict(),
        "target_attempt_id": target_attempt_id,
    }
    response.update(extra)
    return response


def _event_idempotency_recovery(
    runtime: WorkflowRuntime,
    event_types: set[str],
    builder,
):
    """Build a recovery callback from an event atomically written by a route.

    The idempotency row and the domain event use the same request id.  After a
    process restart, the event proves that the mutation committed even if the
    HTTP response was lost; without that proof the repository may reclaim the
    orphaned reservation and retry the local mutation.
    """

    expected = {str(event_type) for event_type in event_types}

    def recover(row: Mapping[str, Any]):
        event = runtime.repository.get_event_by_request_id(str(row["idempotency_id"]))
        if event is None:
            return None
        event_type = str(event.get("event_type") or "")
        if event_type not in expected:
            raise IdempotencyInProgress(
                "the idempotency request has a conflicting durable event",
                details={
                    "idempotency_id": str(row["idempotency_id"]),
                    "event_type": event_type,
                },
            )
        stored_workflow_id = row.get("workflow_id")
        if stored_workflow_id and str(event.get("workflow_id")) != str(stored_workflow_id):
            raise IdempotencyInProgress(
                "the idempotency event belongs to another workflow",
                details={"idempotency_id": str(row["idempotency_id"])},
            )
        try:
            return builder(row, event)
        except IdempotencyInProgress:
            raise
        except Exception as exc:
            raise IdempotencyInProgress(
                "the committed idempotency response cannot be reconstructed safely",
                details={"idempotency_id": str(row["idempotency_id"])},
            ) from exc

    return recover


def _durable_idempotency_recovery(builder):
    """Adapt a natural-key lookup into an idempotency recovery callback."""

    def recover(row: Mapping[str, Any]):
        try:
            return builder(row)
        except IdempotencyInProgress:
            raise
        except Exception as exc:
            raise IdempotencyInProgress(
                "the committed idempotency response cannot be reconstructed safely",
                details={"idempotency_id": str(row["idempotency_id"])},
            ) from exc

    return recover


def _is_tts_workflow(runtime: WorkflowRuntime, workflow_id: str) -> bool:
    """Read the workflow kind without involving a provider or scheduler."""

    with runtime.database.read_transaction() as con:
        row = con.execute(
            "SELECT workflow_type FROM workflows WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
    return bool(row and str(row["workflow_type"] or "").lower() == "tts")


def _tts_generation_accepted(runtime: WorkflowRuntime, workflow_id: str) -> bool:
    """Return whether this TTS workflow has crossed the local generate fence."""

    with runtime.database.read_transaction() as con:
        row = con.execute(
            """SELECT 1 FROM workflows w
               WHERE w.workflow_id=?
                 AND (
                     EXISTS (
                         SELECT 1 FROM workflow_events e
                         WHERE e.workflow_id=w.workflow_id
                           AND e.event_type='WORKFLOW_GENERATE'
                     )
                     OR EXISTS (
                         SELECT 1 FROM workflow_steps s
                         WHERE s.workflow_id=w.workflow_id
                           AND s.step_type='TTS'
                     )
                 )
               LIMIT 1""",
            (workflow_id,),
        ).fetchone()
    return row is not None


def _force_local_cancel(
    runtime: WorkflowRuntime,
    workflow_id: str,
    *,
    reason: str,
    request_id: str | None = None,
    expected_state_version: int | None = None,
):
    """Make cancellation monotonic and terminal without contacting a provider.

    Cancellation is a local fence.  A renderer may submit an old version, or
    replay a response written by an older build that stopped at TERMINATING;
    neither case should leave the user blocked behind a stale optimistic-lock
    value.  The command is retried against the latest local snapshot, then the
    repository's publication fence closes the workflow immediately.
    """

    cancel_event = runtime.generation_cancel_events.get(workflow_id)
    if cancel_event is not None:
        cancel_event.set()
    _release_generation_slot_for_cancel(runtime, workflow_id)

    supplied_version = expected_state_version
    for attempt in range(3):
        snapshot = runtime.repository.get_workflow(workflow_id)
        if snapshot.execution_state == "TERMINAL":
            return snapshot
        if snapshot.control_state in {"TERMINATING", "TERMINATED"}:
            break
        command_version = supplied_version if attempt == 0 and supplied_version is not None else snapshot.state_version
        try:
            runtime.repository.command(
                workflow_id,
                "cancel",
                command_version,
                request_id=request_id,
                reason=reason,
            )
            break
        except ConflictError as exc:
            if getattr(exc, "code", "") != "STATE_CONFLICT" or attempt >= 2:
                raise

    return runtime.repository.finalize_generation_cleanup(
        workflow_id,
        reason=reason,
        force_cancel=True,
    )


def _recover_workflow_command(runtime: WorkflowRuntime, row: Mapping[str, Any], event: Mapping[str, Any], action: str):
    workflow_id = str(event["workflow_id"])
    if action == "cancel":
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        reason = str(payload.get("reason") or "用户取消任务")
        snapshot = _force_local_cancel(
            runtime,
            workflow_id,
            reason=reason,
            request_id=None,
        )
    else:
        snapshot = runtime.repository.get_workflow(workflow_id)
    return 202, _command_response(snapshot, action, str(row["idempotency_id"]))


def _recover_workflow_envelope(runtime: WorkflowRuntime, row: Mapping[str, Any], event: Mapping[str, Any]):
    snapshot = runtime.repository.get_workflow(str(event["workflow_id"]))
    return 200, _workflow_envelope(snapshot, str(row["idempotency_id"]))


def _recover_parse_results(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        metadata: dict[str, Any] = {}
        raw_metadata = item.get("metadata_json")
        if isinstance(raw_metadata, str):
            try:
                parsed_metadata = json.loads(raw_metadata)
            except (TypeError, json.JSONDecodeError):
                parsed_metadata = {}
            if isinstance(parsed_metadata, Mapping):
                metadata = dict(parsed_metadata)
        doc_type = str(metadata.get("doc_type") or item.get("item_type") or "document")
        groups.setdefault(doc_type, []).append({
            "id": item.get("item_identity_key"),
            "category": item.get("item_type"),
            "text": item.get("normalized_content"),
            "role": item.get("role"),
            "voice_key": item.get("voice_key"),
            "source_locator": item.get("source_locator"),
            "metadata": metadata,
        })
    return [{"doc_type": key, "items": value} for key, value in groups.items()]


def _recover_parse(runtime: WorkflowRuntime, row: Mapping[str, Any], event: Mapping[str, Any]):
    workflow_id = str(event["workflow_id"])
    snapshot = runtime.repository.get_workflow(workflow_id)
    items = runtime.repository.list_items(workflow_id)
    if not items:
        raise IdempotencyInProgress("the committed parser result is incomplete")
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    source_artifact_id = str(payload.get("source_artifact_id") or snapshot.source_artifact_id or "")
    if not source_artifact_id:
        raise IdempotencyInProgress("the committed parser result has no source artifact")
    parsed_artifact_ids = payload.get("parsed_artifact_ids")
    parsed_artifact_id = (
        str(parsed_artifact_ids[0])
        if isinstance(parsed_artifact_ids, list) and parsed_artifact_ids
        else None
    )
    return 202, _command_response(
        snapshot,
        "parse",
        str(row["idempotency_id"]),
        parse_results=_recover_parse_results(items),
        source_filename=runtime.application._source_filename(workflow_id, source_artifact_id),
        source_artifact_id=source_artifact_id,
        parsed_artifact_id=parsed_artifact_id,
    )


def _recover_export(runtime: WorkflowRuntime, row: Mapping[str, Any], event: Mapping[str, Any]):
    workflow_id = str(event["workflow_id"])
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    requested_item_ids = payload.get("requested_item_ids")
    if requested_item_ids is not None and not isinstance(requested_item_ids, list):
        raise IdempotencyInProgress("the committed export request has an invalid item selection")

    stored_response = payload.get("response")
    if not isinstance(stored_response, Mapping):
        # Older export events did not persist the response scope. Rebuilding
        # from the current segments could silently return a different ZIP, so
        # fail closed instead of violating idempotency.
        raise IdempotencyInProgress(
            "the committed export response cannot be reconstructed safely; retry with a new idempotency key"
        )
    stored_artifact = stored_response.get("artifact")
    if not isinstance(stored_artifact, Mapping):
        raise IdempotencyInProgress("the committed export response has no artifact metadata")
    artifact_id = str(stored_artifact.get("artifact_id") or payload.get("artifact_id") or "")
    if not artifact_id or (
        payload.get("artifact_id") is not None
        and str(payload.get("artifact_id")) != artifact_id
    ):
        raise IdempotencyInProgress("the committed export response has conflicting artifact metadata")
    if str(stored_artifact.get("artifact_type") or "") != "export-zip":
        raise IdempotencyInProgress("the committed export response points to a non-export artifact")
    try:
        storage = runtime.repository.get_artifact_storage(artifact_id, workflow_id=workflow_id)
    except Exception as exc:
        raise IdempotencyInProgress("the committed export artifact is no longer available") from exc
    if (
        storage.get("artifact_type") != "export-zip"
        or str(storage.get("format") or "").lower().lstrip(".") != "zip"
        or str(stored_artifact.get("format") or "").lower().lstrip(".") != "zip"
        or str(storage.get("sha256") or "") != str(stored_artifact.get("sha256") or "")
        or int(storage.get("size_bytes") or -1) != int(stored_artifact.get("size_bytes") or -1)
    ):
        raise IdempotencyInProgress("the committed export artifact failed integrity validation")
    try:
        state_version = int(stored_response["state_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IdempotencyInProgress("the committed export response has an invalid state version") from exc
    return 201, {
        "request_id": str(row["idempotency_id"]),
        "workflow_id": workflow_id,
        "state_version": state_version,
        "artifact": dict(stored_artifact),
    }


def _recover_source_write(
    runtime: WorkflowRuntime,
    import_id: str,
    expected_size: int,
    expected_sha256: str,
):
    result = runtime.imports.get_import(import_id)
    if (
        str(result.get("status")) == "READY"
        and int(result.get("actual_size_bytes") or -1) == int(expected_size)
        and str(result.get("actual_sha256") or "") == str(expected_sha256)
    ):
        return 201, result
    return None


def _recover_source_generation(
    runtime: WorkflowRuntime,
    row: Mapping[str, Any],
    event: Mapping[str, Any],
    source_import_id: str,
):
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    if str(payload.get("source_import_id") or "") != source_import_id:
        raise IdempotencyInProgress("the source generation event belongs to another import")
    try:
        generation = int(payload["generation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IdempotencyInProgress("the committed source generation has invalid durable facts") from exc
    stored_response = payload.get("response")
    if isinstance(stored_response, Mapping):
        if (
            str(stored_response.get("source_import_id") or "") != source_import_id
            or int(stored_response.get("staging_generation") or -1) != generation
        ):
            raise IdempotencyInProgress("the committed source generation response has conflicting durable facts")
        # Verify the historical row still exists, but do not project the
        # current parent generation over the original response.
        runtime.imports.get_generation(source_import_id, generation)
        return 201, dict(stored_response)
    result = runtime.imports.get_import(source_import_id)
    if int(result.get("staging_generation") or -1) != generation:
        raise IdempotencyInProgress("the committed source generation is no longer current")
    return 201, result


def _recover_external_record(runtime: WorkflowRuntime, workflow_id: str, body: ExternalRecordBody):
    record = runtime.external.find_record(
        external_system=body.external_system,
        account_scope=body.account_scope,
        business_record_key=body.business_record_key,
    )
    if record is None:
        return None
    if str(record.get("local_workflow_id") or "") != workflow_id:
        raise IdempotencyInProgress("the external mapping is owned by another workflow")
    return 201, record


def _recover_external_lease(runtime: WorkflowRuntime, mapping_id: str, owner_id: str):
    lease = runtime.external.find_record_lease(mapping_id, owner_id)
    if lease is None:
        return None
    return 201, {
        "lease_id": lease.lease_id,
        "mapping_id": lease.mapping_id,
        "owner_id": lease.owner_id,
        "fencing_token": lease.fencing_token,
        "lease_until": lease.lease_until,
    }


def _recover_external_operation_prepare(
    runtime: WorkflowRuntime,
    workflow_id: str,
    body: ExternalOperationBody,
):
    operation = runtime.external.find_operation(body.mapping_id, body.operation_key)
    if operation is None:
        return None
    if (
        str(operation.get("workflow_id")) != workflow_id
        or str(operation.get("target_payload_hash")) != content_hash(body.payload)
        or str(operation.get("mapping_version")) != body.mapping_version
    ):
        raise IdempotencyInProgress("the external operation is bound to different durable facts")
    return 201, operation


def _recover_external_operation_state(
    runtime: WorkflowRuntime,
    operation_id: str,
    allowed_states: set[str],
):
    operation = runtime.external.get_operation(operation_id)
    if str(operation.get("side_effect_state") or "") not in allowed_states:
        return None
    return 202, operation


def _generation_target_state_version(runtime: WorkflowRuntime, workflow_id: str, work_unit_id: str | None) -> int | None:
    if not work_unit_id:
        return None
    with runtime.database.read_transaction() as con:
        row = con.execute(
            "SELECT state_version FROM work_units WHERE workflow_id=? AND work_unit_id=?",
            (workflow_id, work_unit_id),
        ).fetchone()
    return int(row["state_version"]) if row is not None else None


def _publish_generation_result_event(runtime: WorkflowRuntime, workflow_id: str, result: Any) -> None:
    """Publish a durable event when a worker produced no local receipt."""

    status = str(getattr(result, "status", ""))
    if status not in {"AMBIGUOUS", "WAITING_RETRY"}:
        return
    event_type = "TTS_SUBMISSION_AMBIGUOUS" if status == "AMBIGUOUS" else "TTS_SUBMISSION_REJECTED"
    snapshot = runtime.repository.get_workflow(workflow_id)
    latest = snapshot.latest_event or {}
    if latest.get("event_type") == event_type and latest.get("attempt_id") == getattr(result, "attempt_id", None):
        return
    work_unit_id = str(getattr(result, "work_unit_id", "") or "") or None
    details = dict(getattr(result, "error_details", {}) or {})
    message = " ".join(str(getattr(result, "error_message", None) or getattr(result, "error_code", None) or "生成任务未能完成").split())[:2000]
    runtime.repository.events.append(
        workflow_id,
        event_type,
        {
            "submission_id": getattr(result, "submission_id", None),
            "work_unit_id": work_unit_id,
            "attempt_id": getattr(result, "attempt_id", None),
            "error_code": getattr(result, "error_code", None),
            "message": message,
            "details": details,
            "target": {"target_type": "WORK_UNIT", "work_unit_id": work_unit_id} if work_unit_id else None,
            "target_state_version": _generation_target_state_version(runtime, workflow_id, work_unit_id),
            "workflow_state_version": snapshot.state_version,
        },
        actor_type="WORKER",
        actor_id="generation-task",
        step_id=getattr(result, "step_id", None),
        attempt_id=getattr(result, "attempt_id", None),
    )


def _publish_generation_runtime_event(
    runtime: WorkflowRuntime,
    workflow_id: str,
    *,
    event_type: str,
    status: str,
    message: str,
    elapsed_seconds: float | None = None,
    progress: dict[str, Any] | None = None,
) -> None:
    """Expose bounded provider progress while the worker is still running.

    Browser startup/login and the provider's download page can legitimately
    take longer than one HTTP request.  Persisting a small, redacted status
    event keeps the renderer from looking frozen without putting credentials,
    URLs, or the process-local callback into durable submission facts.
    """

    payload: dict[str, Any] = {
        "phase": "provider",
        "status": str(status)[:64],
        "message": " ".join(str(message).split())[:500],
    }
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = round(max(0.0, float(elapsed_seconds)), 1)
    if isinstance(progress, dict):
        for key in ("item_id", "segment_id", "stage", "completed_segments", "total_segments", "downloaded", "error"):
            if key not in progress:
                continue
            value = progress[key]
            if key == "error":
                payload[key] = " ".join(str(value or "").split())[:500]
            elif key in {"completed_segments", "total_segments"}:
                try:
                    payload[key] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
            elif key == "downloaded":
                payload[key] = bool(value)
            else:
                payload[key] = str(value)[:256]
    try:
        runtime.repository.events.append(
            workflow_id,
            event_type,
            payload,
            actor_type="WORKER",
            actor_id="generation-task",
        )
    except Exception:
        # Observability must never turn a valid provider result into a failed
        # generation, especially while the database is under a short lock.
        pass


def _active_generation_task(runtime: WorkflowRuntime, workflow_id: str) -> asyncio.Task | None:
    task = runtime.generation_tasks_by_workflow.get(workflow_id)
    return task if task is not None and not task.done() else None


def _release_generation_slot_for_cancel(runtime: WorkflowRuntime, workflow_id: str) -> bool:
    """Detach a canceled local worker that is stuck in an uninterruptible call.

    The provider thread is intentionally left alone; Playwright may still be
    unwinding on its own thread.  The durable workflow is already terminal at
    this point, so releasing only this worker's lease lets a fresh workflow
    start.  ``_GenerationSlotLease`` sees the removed owner and will not
    release the semaphore a second time when the old task eventually exits.
    """

    active_task = _active_generation_task(runtime, workflow_id)
    owner = runtime.generation_slot_owners.get(workflow_id)
    if active_task is None or owner is not active_task:
        return False
    runtime.generation_slot_owners.pop(workflow_id, None)
    runtime.generation_slots.release()
    return True


class _GenerationSlotLease:
    """Semaphore lease that can be revoked by the local cancel fence."""

    def __init__(self, runtime: WorkflowRuntime, workflow_id: str):
        self.runtime = runtime
        self.workflow_id = workflow_id
        self.owner: asyncio.Task | None = None

    async def __aenter__(self):
        await self.runtime.generation_slots.acquire()
        self.owner = asyncio.current_task()
        self.runtime.generation_slot_owners[self.workflow_id] = self.owner
        return self

    async def __aexit__(self, _exc_type, _exc_value, _traceback):
        if self.runtime.generation_slot_owners.get(self.workflow_id) is self.owner:
            self.runtime.generation_slot_owners.pop(self.workflow_id, None)
            self.runtime.generation_slots.release()
        return False


def _ensure_generation_dispatch_capacity(runtime: WorkflowRuntime, workflow_id: str) -> None:
    """Fail before changing durable state when no local worker can be queued."""

    if _active_generation_task(runtime, workflow_id) is not None:
        raise RepositoryError("generation is already running for this workflow", code="GENERATION_ALREADY_RUNNING")
    max_queue = 4
    if len(runtime.generation_tasks) >= 1 + max_queue:
        raise RepositoryError(
            "generation queue is full",
            code="RESOURCE_EXHAUSTED",
            details={"queue_depth": max(0, len(runtime.generation_tasks) - 1), "max_active": 1, "max_queue": max_queue},
        )


async def _wait_for_retryable_generation_to_finish(
    runtime: WorkflowRuntime,
    workflow_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Let a just-failed local worker release its dispatch slot before retry.

    A TTS worker records ``WAITING_RETRY`` at the failure boundary and only
    then returns through its provider thread and cleanup callback.  A user can
    click retry during that short interval.  Waiting for the same local task
    here is safe because the durable state is already retryable; a genuinely
    running task remains protected by the normal capacity check below.
    """

    active = _active_generation_task(runtime, workflow_id)
    if active is None:
        return
    try:
        snapshot = runtime.repository.get_workflow(workflow_id)
    except Exception:
        return
    if snapshot.execution_state not in {"WAITING_RETRY", "WAITING_USER"}:
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(active),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        # The worker may still be doing provider/browser cleanup. Let the
        # caller receive the usual GENERATION_ALREADY_RUNNING response rather
        # than starting a second browser session.
        return


def _pause_probe(runtime: WorkflowRuntime, workflow_id: str) -> bool:
    """Turn a requested pause into PAUSED only at a worker safe point."""

    snapshot = runtime.repository.get_workflow(workflow_id)
    if snapshot.control_state == "PAUSE_REQUESTED":
        snapshot = runtime.repository.acknowledge_pause(workflow_id)
    return snapshot.control_state == "PAUSED"


def _workspace_capabilities(runtime: WorkflowRuntime, workflow_id: str) -> dict[str, Any]:
    """Expose only capabilities confirmed by the local runtime state."""

    snapshot = runtime.repository.get_workflow(workflow_id)
    active = _active_generation_task(runtime, workflow_id) is not None
    is_tts_workflow = _is_tts_workflow(runtime, workflow_id)
    tts_generation_accepted = is_tts_workflow and _tts_generation_accepted(runtime, workflow_id)
    local_tts_pause_available = (
        tts_generation_accepted
        and snapshot.control_state == "RUNNING"
        and snapshot.execution_state in {"PREPARING", "RUNNING", "RECOVERING"}
    )
    local_tts_resume_available = (
        tts_generation_accepted
        and snapshot.control_state in {"PAUSED", "PAUSE_REQUESTED"}
    )
    capabilities: dict[str, Any] = {
        # TTS can settle a pause locally when a browser worker disappeared
        # after acceptance. Other workflow kinds still require their live
        # worker to acknowledge a cooperative pause.
        "supports_pause": (
            snapshot.control_state == "RUNNING"
            and (active or local_tts_pause_available)
        ),
        "supports_resume": active or local_tts_resume_available,
        "supports_takeover": False,
    }
    configuration = runtime.application._configuration(workflow_id)
    provider_name = str(configuration.get("provider") or "xunfei")[:128]
    provider_projection: dict[str, Any] = {
        "provider": provider_name or "UNKNOWN",
        "status": "UNKNOWN",
        "ready": False,
        "can_generate": False,
        "can_start_generation": False,
        "reason": "Provider 状态尚未确认",
    }
    try:
        provider = runtime.providers.get(
            provider_name or "xunfei",
            str(configuration.get("account_scope") or "xunfei-default"),
        )
        capability_snapshot = getattr(provider, "capability_snapshot", None)
        if callable(capability_snapshot):
            capability_snapshot = capability_snapshot()
        has_backend = getattr(provider, "backend", None) is not None
        if isinstance(capability_snapshot, Mapping):
            # Once an adapter supplies a snapshot, do not merge implicit
            # backend defaults into omitted fields.  A partial/unknown
            # snapshot must remain fail-closed until it explicitly confirms
            # each capability.
            provider_projection.update({
                "status": "UNKNOWN",
                "ready": False,
                "can_generate": False,
                "can_start_generation": False,
            })
            provider_projection["provider"] = str(
                capability_snapshot.get("provider") or provider_projection["provider"]
            )[:128]
            snapshot_status = str(capability_snapshot.get("status") or "").upper()
            if snapshot_status in {"UNKNOWN", "READY", "LOGIN_REQUIRED", "EXPIRED", "UNAVAILABLE", "DISABLED"}:
                provider_projection["status"] = snapshot_status
            if "ready" in capability_snapshot:
                provider_projection["ready"] = bool(capability_snapshot["ready"])
            if "can_generate" in capability_snapshot:
                provider_projection["can_generate"] = bool(capability_snapshot["can_generate"])
            if "can_start_generation" in capability_snapshot:
                provider_projection["can_start_generation"] = bool(capability_snapshot["can_start_generation"])
            if capability_snapshot.get("reason"):
                provider_projection["reason"] = str(capability_snapshot["reason"])[:500]
        else:
            provider_projection["ready"] = bool(has_backend)
            provider_projection["can_generate"] = provider_projection["ready"]
            provider_projection["can_start_generation"] = bool(has_backend)
            provider_projection["status"] = "READY" if provider_projection["ready"] else "UNKNOWN"
            provider_projection["reason"] = (
                "本地 Provider 已注册，可提交生成"
                if provider_projection["ready"]
                else "Provider 已注册，但尚未确认可用能力"
            )
        # A capability snapshot is advisory input, but blocked statuses must
        # never be allowed to leak a stale ready/can_generate bit into the
        # command gate.  READY also requires both explicit readiness and an
        # explicit generation capability.
        if provider_projection["status"] != "READY":
            provider_projection["ready"] = False
            provider_projection["can_generate"] = False
            # A foreground generation may be the provider's login/reconnect
            # entry point, but only an explicit adapter capability may open
            # that path.  Unknown providers stay fail-closed.
            provider_projection["can_start_generation"] = bool(
                provider_projection["can_start_generation"]
            )
        else:
            provider_projection["ready"] = bool(provider_projection["ready"])
            provider_projection["can_generate"] = bool(
                provider_projection["can_generate"] and provider_projection["ready"]
            )
            provider_projection["can_start_generation"] = bool(
                provider_projection["can_start_generation"] and provider_projection["ready"]
            )
    except Exception:
        provider_projection["provider"] = provider_name or "UNKNOWN"
        provider_projection["status"] = "UNAVAILABLE"
        provider_projection["can_generate"] = False
        provider_projection["can_start_generation"] = False
        provider_projection["reason"] = "当前运行时未注册该 Provider"
    capabilities["provider"] = provider_projection
    if not active and snapshot.control_state in {"PAUSED", "PAUSE_REQUESTED"}:
        candidate = next(
            (
                value for value in runtime.repository.list_active_workflows(limit=200)
                if value.get("workflow", {}).get("workflow_id") == workflow_id
            ),
            None,
        )
        capabilities["supports_resume"] = bool(candidate and candidate.get("can_resume"))
    if not active and snapshot.control_state == "RUNNING":
        candidate = next(
            (
                value for value in runtime.repository.list_active_workflows(limit=200)
                if value.get("workflow", {}).get("workflow_id") == workflow_id
            ),
            None,
        )
        capabilities["supports_takeover"] = bool(candidate and candidate.get("can_takeover"))
    return capabilities


def _safe_content_filename(value: Any, fallback: str) -> str:
    raw = str(value or "").replace("\\", "/")
    name = Path(raw).name
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127).strip()
    if len(name) <= 256:
        return name or fallback
    suffix = Path(name).suffix.lower()
    if suffix in {".docx", ".xlsx"}:
        max_stem_length = max(1, 256 - len(suffix))
        return f"{Path(name).stem[:max_stem_length]}{suffix}"
    return name[:256] or fallback


def _artifact_content_metadata(runtime: WorkflowRuntime, row: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve response metadata from durable facts without exposing storage keys."""

    artifact_id = str(row["artifact_id"])
    fmt = str(row["blob_format"] or row["artifact_format"] or "bin").lower().lstrip(".") or "bin"
    artifact_type = str(row["artifact_type"] or "artifact")
    source_mime = None
    source_format = None
    if artifact_type == "source":
        # Source blobs are intentionally stored as ``bin`` so the upload
        # protocol does not trust a renderer-controlled format header.  The
        # validated content type captured at import time is still safe to use
        # for the download response, but only for the two supported source
        # formats.
        try:
            details = json.loads(str(row["source_details_json"] or "{}"))
            metadata = details.get("metadata") if isinstance(details, Mapping) else None
            content_type = str((metadata or {}).get("content_type") or "").split(";", 1)[0].strip().lower()
            source_metadata_formats = {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            }
            source_format = source_metadata_formats.get(content_type)
            source_mime = {
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }.get(source_format)
        except (TypeError, ValueError, json.JSONDecodeError):
            source_mime = None
            source_format = None
    if source_format:
        # The stored blob remains ``bin`` by design, but the validated source
        # metadata is authoritative for the user-facing artifact contract.
        fmt = source_format
    filename = None
    try:
        workspace = runtime.repository.get_workspace(str(row["workflow_id"]))
        filename = next(
            (
                artifact.get("filename")
                for artifact in workspace.get("artifacts", [])
                if artifact.get("artifact_id") == artifact_id and artifact.get("filename")
            ),
            None,
        )
    except Exception:
        # A malformed optional projection must not make an otherwise verified
        # artifact unreadable; the fallback below still uses only safe facts.
        filename = None
    if not filename:
        source_name = "wordtts"
        try:
            details = json.loads(str(row["source_details_json"] or "{}"))
            metadata = details.get("metadata") if isinstance(details, Mapping) else None
            if isinstance(metadata, Mapping):
                source_name = _safe_content_filename(metadata.get("filename"), source_name)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        stem = Path(source_name).stem or "wordtts"
        if artifact_type == "tts-segment" and row["item_sequence"] is not None:
            custom_filename = None
            try:
                item_id = row["item_id"]
                if item_id:
                    with runtime.database.read_transaction() as con:
                        item_row = con.execute(
                            """SELECT metadata_json FROM work_items
                               WHERE workflow_id=? AND item_id=?""",
                            (str(row["workflow_id"]), str(item_id)),
                        ).fetchone()
                    if item_row is not None:
                        item_metadata = json.loads(str(item_row["metadata_json"] or "{}"))
                        if isinstance(item_metadata, Mapping):
                            custom_filename = audio_filename_from_stem(
                                item_metadata.get("audio_filename_stem"), fmt
                            )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                custom_filename = None
            filename = custom_filename or f"{int(row['item_sequence']) + 1:03d}.{fmt}"
        elif artifact_type == "export-zip":
            filename = f"{_safe_content_filename(stem, 'wordtts')}_tts.zip"
        elif artifact_type == "parse-output":
            filename = f"{_safe_content_filename(stem, 'source')}.parsed.json"
        elif artifact_type == "source":
            filename = source_name
        else:
            filename = f"artifact.{fmt}"
    mime = source_mime or {
        "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
        "aac": "audio/aac", "ogg": "audio/ogg", "opus": "audio/ogg",
        "flac": "audio/flac", "json": "application/json", "zip": "application/zip",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(fmt, "application/octet-stream")
    size = row["blob_size_bytes"] if row["blob_size_bytes"] is not None else row["artifact_size_bytes"]
    sha256 = row["blob_sha256"] or row["artifact_sha256"]
    return {
        "format": fmt,
        "filename": _safe_content_filename(filename, f"artifact.{fmt}"),
        "mime_type": mime,
        "size_bytes": int(size) if size is not None else None,
        "sha256": str(sha256) if sha256 else None,
    }


def _artifact_row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _artifact_row_is_readable(row: Mapping[str, Any]) -> bool:
    """Keep content tickets behind one consistent Artifact/Blob integrity fence."""

    if not artifact_blob_facts_match(
        artifact_format=_artifact_row_value(row, "artifact_format"),
        blob_format=_artifact_row_value(row, "blob_format"),
        artifact_sha256=_artifact_row_value(row, "artifact_sha256"),
        blob_sha256=_artifact_row_value(row, "blob_sha256"),
        artifact_size_bytes=_artifact_row_value(row, "artifact_size_bytes"),
        blob_size_bytes=_artifact_row_value(row, "blob_size_bytes"),
    ):
        return False
    artifact_type = str(_artifact_row_value(row, "artifact_type") or "")
    artifact_format = str(_artifact_row_value(row, "artifact_format") or "").strip().lower().lstrip(".")
    blob_format = str(_artifact_row_value(row, "blob_format") or "").strip().lower().lstrip(".")
    if artifact_type == "tts-segment":
        return artifact_format == DELIVERABLE_AUDIO_FORMAT and blob_format == DELIVERABLE_AUDIO_FORMAT
    if artifact_type == "export-zip":
        return artifact_format == "zip" and blob_format == "zip"
    return True


def _schedule_generation_task(
    runtime: WorkflowRuntime,
    workflow_id: str,
    *,
    generation_mode: str | None = None,
    provider: str | None = None,
    account_scope: str | None = None,
    allow_interactive_provider: bool = True,
    item_ids: list[str] | None = None,
    cancel_event: threading.Event | None = None,
) -> asyncio.Task:
    """Run one accepted workflow in the bounded desktop worker pool."""

    active_task = runtime.generation_tasks_by_workflow.get(workflow_id)
    if active_task is not None and not active_task.done():
        raise RepositoryError("generation is already running for this workflow", code="GENERATION_ALREADY_RUNNING")
    queue_depth = max(0, len(runtime.generation_tasks) - 1)
    max_queue = 4
    if len(runtime.generation_tasks) >= 1 + max_queue:
        raise RepositoryError(
            "generation queue is full",
            code="RESOURCE_EXHAUSTED",
            details={"queue_depth": queue_depth, "max_active": 1, "max_queue": max_queue},
        )

    cancel_event = cancel_event or threading.Event()
    runtime.generation_cancel_events[workflow_id] = cancel_event

    async def run_task() -> None:
        try:
            async with _GenerationSlotLease(runtime, workflow_id):
                if cancel_event.is_set():
                    return
                # A queued task can receive PAUSE_REQUESTED before it owns a
                # generation slot.  It has no provider safe point to call
                # back from, so acknowledge that durable request here and
                # leave the task parked without opening the browser.  Without
                # this gate the queued worker started after the pause click,
                # which made the UI look as if pause had no effect.
                if await asyncio.to_thread(_pause_probe, runtime, workflow_id):
                    return
                # Cancellation can arrive while the queued worker is doing
                # the durable pause probe. Do not start a provider call after
                # its slot lease has already been revoked by the cancel path.
                if cancel_event.is_set():
                    return
                started_at = time.monotonic()
                _publish_generation_runtime_event(
                    runtime,
                    workflow_id,
                    event_type="TTS_RUNTIME_STATUS",
                    status="starting",
                    message="正在启动讯飞浏览器会话",
                )

                def provider_progress(value: Mapping[str, Any]) -> None:
                    progress = dict(value or {})
                    stage = str(progress.get("stage") or progress.get("status") or "处理中")
                    item_id = str(progress.get("item_id") or "")
                    _publish_generation_runtime_event(
                        runtime,
                        workflow_id,
                        event_type="TTS_RUNTIME_PROGRESS",
                        status=str(progress.get("status") or stage),
                        message=(f"正在处理条目 {item_id}" if item_id else f"讯飞浏览器：{stage}"),
                        elapsed_seconds=time.monotonic() - started_at,
                        progress=progress,
                    )

                provider_task = asyncio.create_task(asyncio.to_thread(
                    runtime.application.run_generation,
                    workflow_id,
                    generation_mode=generation_mode,
                    provider=provider,
                    account_scope=account_scope,
                    allow_interactive_provider=allow_interactive_provider,
                    cancel_check=cancel_event.is_set,
                    pause_check=lambda: _pause_probe(runtime, workflow_id),
                    item_ids=item_ids,
                    progress_callback=provider_progress,
                ))
                while not provider_task.done():
                    await asyncio.wait({provider_task}, timeout=2.0)
                    if not provider_task.done():
                        # The engine persists a retryable submission failure
                        # before the provider thread returns.  Do not append a
                        # fresh "still processing" status in that small
                        # handoff window: it can become the latest event and
                        # make the renderer hide the actionable failure.
                        try:
                            live_snapshot = runtime.repository.get_workflow(workflow_id)
                        except Exception:
                            live_snapshot = None
                        if live_snapshot is not None and live_snapshot.execution_state in {
                            "WAITING_RETRY", "WAITING_USER", "TERMINAL",
                        }:
                            continue
                        _publish_generation_runtime_event(
                            runtime,
                            workflow_id,
                            event_type="TTS_RUNTIME_STATUS",
                            status="waiting",
                            message="讯飞浏览器正在处理，任务仍在运行",
                            elapsed_seconds=time.monotonic() - started_at,
                        )
                result = await provider_task
                _publish_generation_result_event(runtime, workflow_id, result)
        except Exception as exc:
            # The engine persists typed provider failures. Unexpected failures
            # still need a durable state transition; otherwise a process
            # restart leaves an accepted workflow RUNNING forever.
            try:
                snapshot = runtime.repository.get_workflow(workflow_id)
                latest = snapshot.latest_event or {}
                if not cancel_event.is_set() and latest.get("event_type") not in {
                    "TTS_SUBMISSION_AMBIGUOUS", "TTS_SUBMISSION_REJECTED", "TTS_OUTPUT_VERIFIED",
                }:
                    message = " ".join(str(exc or "生成任务未能完成").split())[:2000]
                    runtime.repository.record_generation_task_failure(
                        workflow_id,
                        error_code=getattr(exc, "code", "INTERNAL_ERROR"),
                        error_message=message,
                        error_details=getattr(exc, "details", None),
                    )
            except Exception:
                pass
        finally:
            try:
                runtime.repository.finalize_generation_cleanup(
                    workflow_id,
                    reason="用户取消任务" if cancel_event.is_set() else "生成任务已退出",
                )
            except Exception:
                # Cleanup diagnostics must not turn a completed worker into an
                # unhandled asyncio task exception. The durable workflow is
                # still available to the recovery scanner.
                pass
            if runtime.generation_cancel_events.get(workflow_id) is cancel_event:
                runtime.generation_cancel_events.pop(workflow_id, None)

    task = asyncio.create_task(run_task(), name=f"wordtts-generation:{workflow_id}")
    runtime.generation_tasks.add(task)
    runtime.generation_tasks_by_workflow[workflow_id] = task

    def forget_task(done_task: asyncio.Task) -> None:
        runtime.generation_tasks.discard(done_task)
        if runtime.generation_tasks_by_workflow.get(workflow_id) is done_task:
            runtime.generation_tasks_by_workflow.pop(workflow_id, None)

    task.add_done_callback(forget_task)
    return task




async def _dispatch_due_retries_once(runtime: WorkflowRuntime) -> int:
    """Claim and execute only safe, provider-ready retry candidates."""

    scheduler = runtime.scheduler
    if scheduler is None:
        return 0
    # Do this before claiming new work so a transient failure cannot create an
    # unbounded browser-reopen loop after the automatic retry budget is used.
    await asyncio.to_thread(scheduler.hold_exhausted_retries, limit=8)
    claims = await asyncio.to_thread(scheduler.claim_due_retries, safe_only=True, limit=8)
    dispatched = 0
    for claim in claims:
        try:
            configuration = runtime.application._configuration(claim.workflow_id)
            provider_name = str(configuration.get("provider") or "xunfei")
            account_scope = str(configuration.get("account_scope") or "xunfei-default")
            items = runtime.repository.list_items(claim.workflow_id)
            all_skipped = bool(items) and all(
                str(item.get("status") or "") == "SKIPPED" for item in items
            )
            async with runtime.generation_dispatch_guard:
                # The scheduler claim is made before the process-level
                # dispatch lock. A user can therefore hold/cancel the run in
                # between. Re-read the local state while holding the same
                # fence used by those commands; never start a worker for a
                # task that is now waiting for the user or already terminal.
                current = await asyncio.to_thread(runtime.repository.get_workflow, claim.workflow_id)
                if (
                    current.execution_state != "WAITING_RETRY"
                    or current.control_state != "RUNNING"
                ):
                    continue
                if _active_generation_task(runtime, claim.workflow_id) is None:
                    # The capability check is local-only, but it still does
                    # not belong before the cancellation/state fence. A claim
                    # that was cancelled while waiting for this lock must be
                    # returned without touching provider setup.
                    if not all_skipped:
                        adapter = runtime.application.provider(provider_name, account_scope)
                        runtime.application._ensure_provider_ready(
                            adapter,
                            allow_interactive=False,
                        )
                    _schedule_generation_task(
                        runtime,
                        claim.workflow_id,
                        generation_mode=str(configuration.get("generation_mode") or "composite_cut"),
                        provider=provider_name,
                        account_scope=account_scope,
                        allow_interactive_provider=False,
                    )
                    dispatched += 1
        except RepositoryError as exc:
            delay = 1 if getattr(exc, "code", "") == "RESOURCE_EXHAUSTED" else 30
            await asyncio.to_thread(
                scheduler.defer_retry_claim,
                claim,
                retry_after=_expires_at_iso(delay),
                reason=str(exc),
            )
        except Exception as exc:
            # A provider can be disabled or missing while the durable retry is
            # still valid. Return it to WAITING_RETRY instead of consuming the
            # claim or manufacturing a failed external submission.
            await asyncio.to_thread(
                scheduler.defer_retry_claim,
                claim,
                retry_after=_expires_at_iso(30),
                reason=str(exc),
            )
    return dispatched


async def _dispatch_recoverable_once(runtime: WorkflowRuntime) -> int:
    """Take over safe in-progress runs after a backend restart.

    The repository performs the conditional side-effect scan and takeover
    fence in one database transaction.  This dispatcher only schedules a
    worker after that fence succeeds; it never re-submits an unresolved
    provider operation and never auto-resumes a user-paused workflow.
    """

    dispatched = 0
    for candidate in await asyncio.to_thread(
        runtime.repository.list_active_workflows,
        limit=32,
        recoverable_only=True,
    ):
        workflow = candidate.get("workflow") if isinstance(candidate, Mapping) else None
        if not isinstance(workflow, Mapping) or not candidate.get("can_takeover"):
            continue
        workflow_id = str(workflow.get("workflow_id") or "")
        if not workflow_id or _active_generation_task(runtime, workflow_id) is not None:
            continue
        try:
            configuration = runtime.application._configuration(workflow_id)
            provider_name = str(configuration.get("provider") or "xunfei")
            account_scope = str(configuration.get("account_scope") or "xunfei-default")
            items = runtime.repository.list_items(workflow_id)
            all_skipped = bool(items) and all(
                str(item.get("status") or "") == "SKIPPED" for item in items
            )
            # The durable takeover transition and local task registration must
            # share one process-level fence with pause/cancel/resume.  If
            # ``mark_takeover`` runs before this lock, a user command can land
            # between the state transition and task registration and observe
            # a RECOVERING run that is not yet represented by a local worker.
            # That window is especially harmful for pause: the next recovery
            # pass can then race the user's control request.
            async with runtime.generation_dispatch_guard:
                # Re-read before checking provider capability. A recovery
                # candidate can be cancelled or paused after the index scan;
                # neither state should cause a new provider setup attempt.
                current = await asyncio.to_thread(runtime.repository.get_workflow, workflow_id)
                if (
                    current.control_state != "RUNNING"
                    or current.execution_state not in {"PREPARING", "RUNNING", "RECOVERING"}
                ):
                    continue
                if not all_skipped:
                    adapter = runtime.application.provider(provider_name, account_scope)
                    runtime.application._ensure_provider_ready(
                        adapter,
                        allow_interactive=False,
                    )
                taken_over = await asyncio.to_thread(runtime.repository.mark_takeover, workflow_id)
                if taken_over is None:
                    continue
                current = await asyncio.to_thread(runtime.repository.get_workflow, workflow_id)
                if current.control_state != "RUNNING" or current.execution_state != "RECOVERING":
                    # Keep the final state check even though the transition is
                    # fenced: repository-side recovery may have returned an
                    # already-RECOVERING snapshot, or another process may have
                    # changed the row between database transactions.
                    continue
                if _active_generation_task(runtime, workflow_id) is not None:
                    continue
                _schedule_generation_task(
                    runtime,
                    workflow_id,
                    generation_mode=str(configuration.get("generation_mode") or "composite_cut"),
                    provider=provider_name,
                    account_scope=account_scope,
                    allow_interactive_provider=False,
                )
            dispatched += 1
        except RepositoryError:
            # A full local queue or a concurrent command leaves the durable
            # RECOVERING/candidate state for the next bounded tick.
            continue
        except Exception:
            # Provider availability is checked before the takeover fence.  If
            # it is unavailable, leave the durable run untouched for a later
            # retry instead of manufacturing a failure.
            continue
    return dispatched


async def _automatic_retry_loop(runtime: WorkflowRuntime) -> None:
    """Keep retry dispatch alive across API requests and backend restarts."""

    while True:
        await asyncio.sleep(1.0)
        try:
            await _dispatch_recoverable_once(runtime)
            await _dispatch_due_retries_once(runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The next tick retries the scheduler itself; durable workflow
            # state remains the source of truth and no provider call is made
            # from this error branch.
            continue


def _idempotency_key(value: str | None) -> str:
    if not value or not (16 <= len(value) <= 256):
        raise DomainError("VALIDATION_ERROR", "X-Idempotency-Key must be 16–256 characters")
    return value


def install_workflow_api(
    app,
    *,
    runtime: WorkflowRuntime | None = None,
    database_path: str | os.PathLike[str] | None = None,
    artifact_root: str | os.PathLike[str] | None = None,
    capability: str | None = None,
) -> WorkflowRuntime:
    """Mount the new API and return its runtime for tests/health checks."""

    if runtime is None:
        data_dir = Path(ensure_data_dir())
        runtime = WorkflowRuntime.from_paths(
            database_path or os.environ.get("WORDTTS_WORKFLOW_DB_PATH", str(data_dir / "workflow.db")),
            artifact_root or os.environ.get("WORDTTS_ARTIFACT_ROOT", str(data_dir / "artifacts")),
            capability=capability if capability is not None else os.environ.get("WORDTTS_API_TOKEN"),
        )
    elif not runtime.capability:
        # Keep callers that assemble a runtime manually fail-closed as well.
        runtime.capability = secrets.token_urlsafe(32)
    app.state.workflow_runtime = runtime

    async def capability_dependency(request: Request) -> None:
        supplied = request.headers.get("X-Desktop-Capability")
        if runtime.capability and not verify_capability(supplied, runtime.capability):
            raise TicketError("missing or invalid desktop capability")

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(capability_dependency)])

    @router.post("/workflows", status_code=201)
    async def create_workflow(body: WorkflowCreateBody, request: Request, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        request_id = _request_id()
        response, replayed = runtime.repository.create_workflow_idempotent(
            body.workflow_type,
            body.configuration,
            business_key=body.business_key,
            client_key=key,
            request=body.model_dump(),
            request_id=request_id,
        )
        return JSONResponse(response, status_code=200 if replayed else 201) if replayed else response

    @router.get("/workflows/active")
    async def list_active_workflows(limit: int = 100):
        """Return bounded restart candidates with server-owned continuation facts."""

        runtime.ensure_initialized()
        return runtime.repository.list_active_workflows(
            limit=limit,
            _include_page=True,
        )

    @router.get("/workflows/{workflow_id}/workspace")
    async def get_workflow_workspace(workflow_id: str):
        runtime.ensure_initialized()
        workspace = runtime.repository.get_workspace(
            workflow_id,
            capabilities=_workspace_capabilities(runtime, workflow_id),
        )
        return {"request_id": _request_id(), "workspace": workspace}

    @router.get("/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str):
        runtime.ensure_initialized()
        return _workflow_envelope(runtime.repository.get_workflow(workflow_id))

    @router.delete("/workflows/{workflow_id}")
    async def delete_workflow(
        workflow_id: str,
        body: WorkflowCommandBody,
        idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    ):
        """Physically delete an unfinished workflow and its local data."""

        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        # The workflow row is deliberately not attached to this reservation:
        # the mutation removes that row.  The repository completes this key
        # inside the same transaction as the delete, making replay safe after
        # the target no longer exists.
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}",
            client_key=key,
            command_name="deleteWorkflow",
            method="DELETE",
            resource_id=workflow_id,
            target=None,
            request=body.model_dump(),
            workflow_id=None,
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)

        request_id = str(idem_id)
        response = {
            "request_id": request_id,
            "workflow_id": workflow_id,
            "accepted_action": "delete",
            "deleted": True,
        }
        # A history deletion is an explicit user-confirmed local purge.  Ask a
        # cooperative local worker to stop first, then take the same dispatch
        # fence before deleting its durable rows.  We never delete underneath
        # an in-flight provider call because its thread could still publish a
        # result after the workflow row disappeared.
        active_task = None
        async with runtime.generation_dispatch_guard:
            active_task = _active_generation_task(runtime, workflow_id)
            if active_task is not None:
                cancel_event = runtime.generation_cancel_events.get(workflow_id)
                if cancel_event is not None:
                    cancel_event.set()
        if active_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(active_task), timeout=15.0)
            except asyncio.TimeoutError as exc:
                raise RepositoryError(
                    "generation is still running; wait for the current provider call to stop",
                    code="GENERATION_ALREADY_RUNNING",
                ) from exc
            except Exception:
                # The task wrapper persists its own typed failure.  A task
                # exception must not prevent the explicit local purge once it
                # has released the worker slot and lease.
                pass

        async with runtime.generation_dispatch_guard:
            if _active_generation_task(runtime, workflow_id) is not None:
                raise RepositoryError(
                    "generation is still running; retry deletion shortly",
                    code="GENERATION_ALREADY_RUNNING",
                )
            runtime.application.delete_workflow(
                workflow_id,
                expected_state_version=body.expected_state_version,
                request_id=request_id,
                response=response,
                allow_unresolved=True,
            )

        # Staging refs are workflow-owned and can be removed immediately.  A
        # fresh GC scan handles content-addressed Blobs only when no remaining
        # Artifact references them, including the case where a rerun shared a
        # Blob with another workflow.
        if runtime.garbage_collector is not None:
            try:
                runtime.garbage_collector.collect(limit=256)
            except Exception:
                # The database delete has already committed; cleanup is
                # recoverable on the next startup and must not turn success
                # into an ambiguous HTTP response.
                pass
        return JSONResponse(response, status_code=200)

    @router.get("/workflows/{workflow_id}/recovery")
    async def get_workflow_recovery(workflow_id: str):
        """Expose external-operation handoffs after a restart.

        This compatibility endpoint is for the separate external-system
        workflow profile. TTS workspaces return an empty list and use local
        retry/cancel state only; the renderer does not call this endpoint.
        """

        runtime.ensure_initialized()
        snapshot = runtime.repository.get_workflow(workflow_id)
        interventions = (
            []
            if _is_tts_workflow(runtime, workflow_id)
            else runtime.repository.list_open_reconciliations(workflow_id)
        )
        return {
            "request_id": _request_id(),
            "workflow_id": workflow_id,
            "workflow_state_version": snapshot.state_version,
            "execution_state": snapshot.execution_state,
            "control_state": snapshot.control_state,
            "interventions": interventions,
        }

    @router.get("/workflows")
    async def list_workflows(limit: int = 100):
        runtime.ensure_initialized()
        return {"workflows": runtime.application.history(limit=limit)}

    @router.get("/workflows/{workflow_id}/items")
    async def list_workflow_items(workflow_id: str):
        runtime.ensure_initialized()
        return {"items": runtime.repository.list_items(workflow_id)}

    @router.get("/workflows/{workflow_id}/items/{item_id}/content/{content_id}")
    async def get_workflow_item_content(
        workflow_id: str,
        item_id: str,
        content_id: str,
        expected_state_version: int | None = None,
        offset_bytes: int = 0,
        max_response_bytes: int = WORKSPACE_CONTENT_DETAIL_LIMIT,
    ):
        """Read one bounded item body after the list projection returned a ref.

        Large text is deliberately not copied into every workspace refresh.
        The opaque id is bound to the workflow, item and persisted content
        hash, while the optional state version prevents an editor from
        silently applying text loaded from an older workspace.
        """

        runtime.ensure_initialized()
        if offset_bytes < 0:
            raise RepositoryError("content offset must not be negative", code="VALIDATION_ERROR")
        if max_response_bytes < 1024 or max_response_bytes > WORKSPACE_CONTENT_DETAIL_LIMIT:
            raise RepositoryError(
                "content response size exceeds the bounded detail limit",
                code="VALIDATION_ERROR",
                details={"max_response_bytes": WORKSPACE_CONTENT_DETAIL_LIMIT},
            )
        with runtime.repository.database.read_transaction() as con:
            snapshot = _snapshot_from_connection(con, workflow_id)
            if expected_state_version is not None and snapshot.state_version != int(expected_state_version):
                raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
            row = con.execute(
                """SELECT item_id, state_version, normalized_content, content_hash
                   FROM work_items WHERE workflow_id=? AND item_id=?""",
                (workflow_id, item_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("item does not exist")
            stored_hash = str(row["content_hash"] or "")
            if item_content_id(workflow_id, item_id, stored_hash) != str(content_id):
                raise NotFoundError("item content does not exist")
            content = str(row["normalized_content"] or "")
            raw_content = content.encode("utf-8")
            size_bytes = len(raw_content)
            if offset_bytes > size_bytes:
                raise RepositoryError(
                    "content offset is past the end of the item",
                    code="VALIDATION_ERROR",
                    details={"size_bytes": size_bytes, "offset_bytes": offset_bytes},
                )
            # Align a byte offset to a UTF-8 boundary so callers can request
            # arbitrary chunks without receiving replacement characters.
            aligned_offset = len(raw_content[:offset_bytes].decode("utf-8", errors="ignore").encode("utf-8"))
            chunk = raw_content[aligned_offset:aligned_offset + max_response_bytes]
            chunk_text = chunk.decode("utf-8", errors="ignore")
            consumed_bytes = len(chunk_text.encode("utf-8"))
            next_offset = aligned_offset + consumed_bytes
            return {
                "workflow_id": workflow_id,
                "item_id": item_id,
                "content_id": content_id,
                "state_version": int(snapshot.state_version),
                "item_state_version": int(row["state_version"] or 0),
                "content_hash": stored_hash,
                "size_bytes": size_bytes,
                "offset_bytes": aligned_offset,
                "next_offset_bytes": next_offset,
                "truncated": next_offset < size_bytes,
                "content": chunk_text,
            }

    @router.get("/workflows/{workflow_id}/artifacts")
    async def list_workflow_artifacts(workflow_id: str, limit: int = 500):
        runtime.ensure_initialized()
        return {"artifacts": runtime.application.artifacts(workflow_id, limit=limit)}

    @router.post("/workflows/{workflow_id}/archive", status_code=202)
    async def archive_workflow(workflow_id: str, body: ArchiveBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="archiveWorkflow",
            method="POST", resource_id=workflow_id, target=None, request=body.model_dump(),
            workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {"WORKFLOW_ARCHIVED"},
                lambda row, event: _recover_workflow_command(runtime, row, event, "archive"),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        snapshot = runtime.application.archive(
            workflow_id,
            expected_state_version=body.expected_state_version,
            reason=body.reason,
            request_id=request_id,
        )
        response = _command_response(snapshot, "archive", request_id)
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=response, workflow_id=workflow_id)
        return JSONResponse(response, status_code=202)

    @router.patch("/workflows/{workflow_id}")
    async def patch_workflow(workflow_id: str, body: WorkflowPatchBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="patchDraftWorkflow",
            method="PATCH", resource_id=workflow_id, target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {"WORKFLOW_PATCHED"},
                lambda row, event: _recover_workflow_envelope(runtime, row, event),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        snapshot = runtime.repository.patch_draft(
            workflow_id, body.expected_state_version,
            expected_configuration_revision=body.configuration_revision,
            configuration=body.configuration,
            item_overrides=body.item_overrides or [], request_id=request_id,
        )
        response = _workflow_envelope(snapshot, request_id)
        runtime.repository.complete_idempotency(idem_id, response_status=200, response=response, workflow_id=workflow_id)
        return response

    @router.patch("/workflows/{workflow_id}/workspace")
    async def patch_workflow_workspace(workflow_id: str, body: WorkflowPatchBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        """Apply a versioned configuration/item revision and return the full projection."""

        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="patchWorkflowWorkspace",
            method="PATCH", resource_id=workflow_id, target=None, request=body.model_dump(),
            workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {"WORKFLOW_PATCHED"},
                lambda row, event: (
                    200,
                    {
                        "request_id": str(row["idempotency_id"]),
                        "workspace": runtime.repository.get_workspace(
                            str(event["workflow_id"]),
                            capabilities=_workspace_capabilities(runtime, str(event["workflow_id"])),
                        ),
                    },
                ),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        runtime.repository.patch_draft(
            workflow_id,
            body.expected_state_version,
            expected_configuration_revision=body.configuration_revision,
            configuration=body.configuration,
            item_overrides=body.item_overrides or [],
            request_id=request_id,
        )
        workspace = runtime.repository.get_workspace(
            workflow_id,
            capabilities=_workspace_capabilities(runtime, workflow_id),
        )
        response = {"request_id": request_id, "workspace": workspace}
        runtime.repository.complete_idempotency(idem_id, response_status=200, response=response, workflow_id=workflow_id)
        return response

    @router.post("/workflows/{workflow_id}/retry-hold", status_code=202)
    async def hold_workflow_retry(workflow_id: str, body: WorkflowCommandBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        """Keep an automatic retry from racing the configuration editor."""

        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="holdRetry",
            method="POST", resource_id=workflow_id, target=None, request=body.model_dump(),
            workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {"RETRY_HELD"},
                lambda row, event: _recover_workflow_command(runtime, row, event, "retry_hold"),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        # Serialize the durable hold with local dispatch. Without this fence
        # a scheduler can claim the retry between the state transition and
        # the cancel-event check, reopening the browser while the user is
        # returning to configuration.
        async with runtime.generation_dispatch_guard:
            snapshot = runtime.repository.hold_automatic_retry(
                workflow_id,
                body.expected_state_version,
                request_id=request_id,
                reason=body.reason or "desktop-return-to-configuration",
            )
            # A scheduler claim may have started the local worker before the
            # lock was acquired. Ask it to stop. TTS has no reconciliation
            # handoff: the local retry/stop decision remains authoritative
            # even if the provider boundary was crossed.
            cancel_event = runtime.generation_cancel_events.get(workflow_id)
            active_task = runtime.generation_tasks_by_workflow.get(workflow_id)
            if (
                snapshot.execution_state == "WAITING_USER"
                and cancel_event is not None
                and active_task is not None
                and not active_task.done()
            ):
                cancel_event.set()
        response = _command_response(snapshot, "retry_hold", request_id)
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=response, workflow_id=workflow_id)
        return JSONResponse(response, status_code=202)

    async def workflow_command(workflow_id: str, action: str, body: WorkflowCommandBody, idempotency_key: str):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name=action,
            method="POST", resource_id=workflow_id, target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {f"WORKFLOW_{action.upper()}"},
                lambda row, event: _recover_workflow_command(runtime, row, event, action),
            ),
        )
        if cached is not None:
            if action == "cancel":
                async with runtime.generation_dispatch_guard:
                    snapshot = _force_local_cancel(
                        runtime,
                        workflow_id,
                        reason=body.reason or "用户取消任务",
                        request_id=str(idem_id),
                    )
                response = _command_response(snapshot, action, str(idem_id))
                # Older builds cached the intermediate TERMINATING snapshot.
                # Replace that response so every subsequent replay observes
                # the same terminal local outcome.
                runtime.repository.complete_idempotency(
                    idem_id,
                    response_status=202,
                    response=response,
                    workflow_id=workflow_id,
                )
                return JSONResponse(response, status_code=200)
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        if action == "pause":
            # The local pause decision and worker lookup must be one fenced
            # operation. Otherwise recovery dispatch can register a worker
            # after the no-worker check but before PAUSE is persisted.
            async with runtime.generation_dispatch_guard:
                before = runtime.repository.get_workflow(workflow_id)
                active_task = _active_generation_task(runtime, workflow_id)
                local_tts_pause = (
                    _is_tts_workflow(runtime, workflow_id)
                    and _tts_generation_accepted(runtime, workflow_id)
                    and before.execution_state in {"PREPARING", "RUNNING", "RECOVERING"}
                    and before.control_state == "RUNNING"
                )
                if active_task is None and not local_tts_pause:
                    raise RepositoryError(
                        "no local generation worker is available to receive the pause request",
                        code="GENERATION_NOT_RUNNING",
                    )
                snapshot = runtime.repository.command(
                    workflow_id, action, body.expected_state_version,
                    request_id=request_id, reason=body.reason,
                )
                if local_tts_pause:
                    # TTS control is local. Persist PAUSED immediately even
                    # when a browser worker is active; its private control
                    # probe parks before the next page action. This makes the
                    # durable state and UI respond without waiting for an
                    # arbitrary Playwright timeout.
                    snapshot = runtime.repository.acknowledge_pause(workflow_id)
        elif action == "cancel":
            async with runtime.generation_dispatch_guard:
                snapshot = _force_local_cancel(
                    runtime,
                    workflow_id,
                    reason=body.reason or "用户取消任务",
                    request_id=request_id,
                    expected_state_version=body.expected_state_version,
                )
        else:
            before = runtime.repository.get_workflow(workflow_id)
            if action == "resume" and _active_generation_task(runtime, workflow_id) is None:
                if before.control_state in {"PAUSED", "PAUSE_REQUESTED"}:
                    candidate = next(
                        (
                            value for value in runtime.repository.list_active_workflows(limit=200)
                            if value.get("workflow", {}).get("workflow_id") == workflow_id
                        ),
                        None,
                    )
                    if not candidate or not candidate.get("can_resume"):
                        raise ConflictError(
                            "workflow cannot resume until its external side effect is reconciled",
                            code="RECONCILIATION_REQUIRED",
                        )
                    configuration = runtime.application._configuration(workflow_id)
                    # A workflow whose every item is already SKIPPED can be
                    # closed by the local repository path and must not require
                    # a provider login merely to resume that local transition.
                    items = runtime.repository.list_items(workflow_id)
                    all_skipped = bool(items) and all(
                        str(item.get("status") or "") == "SKIPPED" for item in items
                    )
                    if not all_skipped:
                        adapter = runtime.application.provider(
                            str(configuration.get("provider") or "xunfei"),
                            str(configuration.get("account_scope") or "xunfei-default"),
                        )
                        runtime.application._ensure_provider_ready(adapter)
            snapshot = runtime.repository.command(
                workflow_id, action, body.expected_state_version,
                request_id=request_id, reason=body.reason,
            )
        if action == "resume":
            try:
                async with runtime.generation_dispatch_guard:
                    # A recovery tick may win the lock after the resume
                    # command changes PAUSED -> RUNNING. Re-check the worker
                    # registry inside the fence so that winner is reused
                    # instead of being treated as a scheduling failure.
                    if _active_generation_task(runtime, workflow_id) is None:
                        _ensure_generation_dispatch_capacity(runtime, workflow_id)
                        configuration = runtime.application._configuration(workflow_id)
                        _schedule_generation_task(
                            runtime,
                            workflow_id,
                            generation_mode=str(configuration.get("generation_mode") or "composite_cut"),
                            provider=str(configuration.get("provider") or "xunfei"),
                            account_scope=str(configuration.get("account_scope") or "xunfei-default"),
                        )
            except Exception:
                # Keep a restarted workflow paused if scheduling fails after
                # the resume command was fenced.  A later explicit resume can
                # retry without pretending that a worker is already running.
                try:
                    runtime.repository.command(
                        workflow_id, "pause", snapshot.state_version,
                        reason="resume scheduling failed",
                    )
                    snapshot = runtime.repository.acknowledge_pause(workflow_id)
                except Exception:
                    pass
                raise
        response = _command_response(snapshot, action, request_id)
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=response, workflow_id=workflow_id)
        return JSONResponse(response, status_code=202)

    def make_workflow_command_route(action: str):
        async def route(workflow_id: str, body: WorkflowCommandBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
            return await workflow_command(workflow_id, action, body, idempotency_key)
        route.__name__ = f"{action}_workflow_route"
        return route

    async def parse_workflow(workflow_id: str, body: ParseBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="parseWorkflow",
            method="POST", resource_id=workflow_id, target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {"WORKFLOW_PARSED"},
                lambda row, event: _recover_parse(runtime, row, event),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        result = await asyncio.to_thread(
            runtime.application.parse,
            workflow_id,
            expected_state_version=body.expected_state_version,
            source_artifact_id=body.source_artifact_id,
            request_id=request_id,
        )
        snapshot = result["workflow"]
        response = _command_response(
            snapshot,
            "parse",
            request_id,
            parse_results=result["parse_results"],
            source_filename=result["source_filename"],
            source_artifact_id=result["source_artifact_id"],
            parsed_artifact_id=result["parsed_artifact_id"],
        )
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=response, workflow_id=workflow_id)
        return JSONResponse(response, status_code=202)

    async def generate_workflow(workflow_id: str, body: GenerateBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="generateWorkflow",
            method="POST", resource_id=workflow_id, target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {"WORKFLOW_GENERATE"},
                lambda row, event: _recover_workflow_command(runtime, row, event, "generate"),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        if body.configuration_revision is not None:
            current_configuration_revision = runtime.repository.get_configuration_revision(workflow_id)
            if int(body.configuration_revision) != current_configuration_revision:
                raise ConflictError(
                    "workflow configuration revision is stale",
                    code="CONFIGURATION_CONFLICT",
                    details={
                        "expected_configuration_revision": int(body.configuration_revision),
                        "current_configuration_revision": current_configuration_revision,
                    },
                )
        # The capacity check, durable acceptance and local task registration
        # must be one process-level critical section.  Otherwise two concurrent
        # requests can both observe a free slot, accept two RUNNING snapshots,
        # and only then discover that one enqueue lost the race.
        async with runtime.generation_dispatch_guard:
            await _wait_for_retryable_generation_to_finish(runtime, workflow_id)
            _ensure_generation_dispatch_capacity(runtime, workflow_id)
            snapshot = runtime.application.accept_generation(
                workflow_id,
                expected_state_version=body.expected_state_version,
                generation_mode=body.generation_mode,
                provider=body.provider,
                account_scope=body.account_scope,
                expected_configuration_revision=body.configuration_revision,
                request_id=request_id,
            )
            response = _command_response(snapshot, "generate", request_id)
            effective_configuration = runtime.application._configuration(workflow_id)
            # There is no provider work when every parsed item was explicitly
            # skipped.  Close this local, billable-free path before returning
            # instead of enqueueing a task whose only job is to discover that
            # it has nothing to submit.  Besides avoiding a needless browser
            # startup, this makes the POST response and the immediately-read
            # workflow snapshot agree on the terminal result.
            items = runtime.repository.list_items(workflow_id)
            all_items_skipped = bool(items) and body.item_ids is None and all(
                str(item.get("status") or "") == "SKIPPED" for item in items
            )
            if all_items_skipped:
                snapshot = runtime.repository.complete_skipped_workflow(
                    workflow_id,
                    request_id=request_id,
                )
                response = _command_response(snapshot, "generate", request_id)
            else:
                try:
                    _schedule_generation_task(
                        runtime,
                        workflow_id,
                        generation_mode=str(effective_configuration.get("generation_mode") or "composite_cut"),
                        provider=str(effective_configuration.get("provider") or "xunfei"),
                        account_scope=str(effective_configuration.get("account_scope") or "xunfei-default"),
                        item_ids=body.item_ids,
                    )
                except RepositoryError as exc:
                    # Capacity is checked before acceptance, but the task registry
                    # can still change inside the helper.  Do not leave a durable
                    # RUNNING workflow behind when the enqueue fails.
                    if exc.code != "GENERATION_ALREADY_RUNNING":
                        try:
                            runtime.repository.record_generation_task_failure(
                                workflow_id,
                                error_code=exc.code,
                                error_message=str(exc),
                                error_details=getattr(exc, "details", None),
                            )
                        except Exception:
                            pass
                    raise
                except Exception as exc:
                    try:
                        runtime.repository.record_generation_task_failure(
                            workflow_id,
                            error_code=getattr(exc, "code", "INTERNAL_ERROR"),
                            error_message=str(exc),
                            error_details=getattr(exc, "details", None),
                        )
                    except Exception:
                        pass
                    raise
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=response, workflow_id=workflow_id)
        return JSONResponse(response, status_code=202)

    @router.post("/workflows/{workflow_id}/export-zip", status_code=201)
    async def export_workflow_zip(workflow_id: str, body: ExportZipBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="exportWorkflowZip",
            method="POST", resource_id=workflow_id, target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {"WORKFLOW_EXPORTED"},
                lambda row, event: _recover_export(runtime, row, event),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        snapshot = runtime.repository.get_workflow(workflow_id)
        if snapshot.state_version != body.expected_state_version:
            raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
        artifact = await asyncio.to_thread(
            runtime.application.create_export_zip,
            workflow_id,
            include_item_ids=body.include_item_ids,
            request_id=request_id,
        )
        response = {
            "request_id": request_id,
            "workflow_id": workflow_id,
            "state_version": snapshot.state_version,
            "artifact": artifact,
        }
        runtime.repository.complete_idempotency(idem_id, response_status=201, response=response, workflow_id=workflow_id)
        return JSONResponse(response, status_code=201)

    router.add_api_route("/workflows/{workflow_id}/parse", parse_workflow, methods=["POST"], status_code=202, response_model=None, name="parse_workflow")
    router.add_api_route("/workflows/{workflow_id}/generate", generate_workflow, methods=["POST"], status_code=202, response_model=None, name="generate_workflow")

    for action in ("pause", "resume", "cancel"):
        router.add_api_route(
            f"/workflows/{{workflow_id}}/{action}",
            make_workflow_command_route(action),
            methods=["POST"], status_code=202, response_model=None,
            name=f"{action}_workflow",
        )

    async def targeted(workflow_id: str, action: str, body: TargetedCommandBody, idempotency_key: str):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name=action,
            method="POST", resource_id=workflow_id, target=body.target, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {f"WORKFLOW_{action.upper()}_TARGETED"},
                lambda row, event: _recover_workflow_command(runtime, row, event, action),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        snapshot = runtime.repository.targeted_command(
            workflow_id, action, body.target,
            expected_state_version=body.expected_state_version,
            expected_target_state_version=body.expected_target_state_version,
            request_id=request_id, reason=body.reason,
            expected_attempt_id=body.expected_attempt_id,
        )
        response = _command_response(snapshot, action, request_id)
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=response, workflow_id=workflow_id)
        return JSONResponse(response, status_code=202)

    def make_targeted_route(action: str):
        async def route(workflow_id: str, body: TargetedCommandBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
            return await targeted(workflow_id, action, body, idempotency_key)
        route.__name__ = f"{action}_workflow_target_route"
        return route

    for action in ("retry", "reconcile"):
        router.add_api_route(
            f"/workflows/{{workflow_id}}/{action}",
            make_targeted_route(action),
            methods=["POST"], status_code=202, response_model=None,
            name=f"{action}_workflow_target",
        )

    @router.post("/workflows/{workflow_id}/reruns", status_code=201)
    async def create_workflow_rerun(workflow_id: str, body: RerunBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        if body.source_workflow_id is not None and body.source_workflow_id != workflow_id:
            raise DomainError("VALIDATION_ERROR", "source_workflow_id must match the workflow path")
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="createWorkflowRerun",
            method="POST", resource_id=workflow_id, target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_event_idempotency_recovery(
                runtime,
                {"WORKFLOW_RERUN_CREATED"},
                lambda row, event: _recover_workflow_envelope(runtime, row, event),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        snapshot = runtime.repository.create_rerun(
            workflow_id,
            expected_group_state_version=body.expected_group_state_version,
            request_id=request_id,
            reason=body.reason,
        )
        response = _workflow_envelope(snapshot, request_id)
        runtime.repository.complete_idempotency(idem_id, response_status=201, response=response, workflow_id=snapshot.workflow_id)
        return response

    @router.post("/workflows/{workflow_id}/external-records", status_code=201)
    async def ensure_external_record(workflow_id: str, body: ExternalRecordBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}:external-record", client_key=key,
            command_name="ensureExternalRecord", method="POST", resource_id=workflow_id,
            target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_durable_idempotency_recovery(
                lambda row: _recover_external_record(runtime, workflow_id, body),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        result = runtime.external.ensure_record(
            workflow_id,
            external_system=body.external_system,
            account_scope=body.account_scope,
            business_record_key=body.business_record_key,
            mapping_version=body.mapping_version,
            item_id=body.item_id,
        )
        runtime.repository.complete_idempotency(idem_id, response_status=201, response=result, workflow_id=workflow_id)
        return result

    @router.get("/external-records/{mapping_id}")
    async def get_external_record(mapping_id: str):
        runtime.ensure_initialized()
        return runtime.external.get_record(mapping_id)

    @router.post("/external-records/{mapping_id}/leases", status_code=201)
    async def acquire_external_record_lease(mapping_id: str, body: ExternalLeaseBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        mapping = runtime.external.get_record(mapping_id)
        workflow_id = str(mapping.get("local_workflow_id") or "")
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"external-record:{mapping_id}:lease", client_key=key,
            command_name="acquireExternalRecordLease", method="POST", resource_id=mapping_id,
            target=None, request=body.model_dump(), workflow_id=workflow_id or None,
            recovery=_durable_idempotency_recovery(
                lambda row: _recover_external_lease(runtime, mapping_id, body.owner_id),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        lease = runtime.external.acquire_record_lease(mapping_id, body.owner_id, ttl_seconds=body.ttl_seconds)
        result = {
            "lease_id": lease.lease_id,
            "mapping_id": lease.mapping_id,
            "owner_id": lease.owner_id,
            "fencing_token": lease.fencing_token,
            "lease_until": lease.lease_until,
        }
        runtime.repository.complete_idempotency(idem_id, response_status=201, response=result, workflow_id=workflow_id or None)
        return result

    @router.post("/workflows/{workflow_id}/external-operations", status_code=201)
    async def prepare_external_operation(workflow_id: str, body: ExternalOperationBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}:external-operation", client_key=key,
            command_name="prepareExternalOperation", method="POST", resource_id=workflow_id,
            target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_durable_idempotency_recovery(
                lambda row: _recover_external_operation_prepare(runtime, workflow_id, body),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        result = runtime.external.prepare_operation(
            workflow_id,
            mapping_id=body.mapping_id,
            operation_key=body.operation_key,
            payload=body.payload,
            mapping_version=body.mapping_version,
            item_id=body.item_id,
        )
        runtime.repository.complete_idempotency(idem_id, response_status=201, response=result, workflow_id=workflow_id)
        return result

    @router.get("/external-operations/{operation_id}")
    async def get_external_operation(operation_id: str):
        runtime.ensure_initialized()
        return runtime.external.get_operation(operation_id)

    @router.post("/external-operations/{operation_id}/begin", status_code=202)
    async def begin_external_operation(operation_id: str, body: ExternalLeaseReferenceBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        operation = runtime.external.get_operation(operation_id)
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{operation['workflow_id']}:external-operation:{operation_id}", client_key=key,
            command_name="beginExternalOperation", method="POST", resource_id=operation_id,
            target={"external_operation_id": operation_id}, request=body.model_dump(), workflow_id=str(operation["workflow_id"]),
            recovery=_durable_idempotency_recovery(
                lambda row: _recover_external_operation_state(
                    runtime,
                    operation_id,
                    {"IN_FLIGHT", "SUBMITTED", "CONFIRMED", "REJECTED", "AMBIGUOUS"},
                ),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        lease = ExternalLease(body.lease_id, body.mapping_id, body.owner_id, body.fencing_token, "")
        result = runtime.external.begin_operation(operation_id, lease)
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=result, workflow_id=str(operation["workflow_id"]))
        return result

    @router.post("/external-operations/{operation_id}/submissions", status_code=202)
    async def observe_external_submission(operation_id: str, body: ExternalSubmitBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        operation = runtime.external.get_operation(operation_id)
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{operation['workflow_id']}:external-operation:{operation_id}", client_key=key,
            command_name="observeExternalSubmission", method="POST", resource_id=operation_id,
            target={"external_operation_id": operation_id}, request=body.model_dump(), workflow_id=str(operation["workflow_id"]),
            recovery=_durable_idempotency_recovery(
                lambda row: _recover_external_operation_state(
                    runtime,
                    operation_id,
                    {"SUBMITTED", "CONFIRMED", "REJECTED"},
                ),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        lease = ExternalLease(body.lease.lease_id, body.lease.mapping_id, body.lease.owner_id, body.lease.fencing_token, "")
        submission = ExternalSubmission(body.external_record_id, body.canonical_key, body.summary)
        result = runtime.external.record_submission(operation_id, lease, submission)
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=result, workflow_id=str(operation["workflow_id"]))
        return result

    @router.post("/external-operations/{operation_id}/resolve", status_code=202)
    async def resolve_external_operation(operation_id: str, body: ExternalResolveBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        operation = runtime.external.get_operation(operation_id)
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{operation['workflow_id']}:external-operation:{operation_id}", client_key=key,
            command_name="resolveExternalOperation", method="POST", resource_id=operation_id,
            target={"external_operation_id": operation_id}, request=body.model_dump(), workflow_id=str(operation["workflow_id"]),
            recovery=_durable_idempotency_recovery(
                lambda row: _recover_external_operation_state(
                    runtime,
                    operation_id,
                    {
                        "CONFIRMED" if body.decision == "CONFIRMED" else
                        "REJECTED" if body.decision == "NOT_SUBMITTED" else "AMBIGUOUS",
                    },
                ),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        result = runtime.external.resolve_operation(
            operation_id,
            decision=body.decision,
            evidence_source=body.evidence.source,
            evidence_hash=body.evidence.evidence_hash,
            evidence=body.evidence.model_dump(),
            resolved_by=body.resolved_by,
        )
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=result, workflow_id=str(operation["workflow_id"]))
        return result

    @router.post("/attempts/{attempt_id}/resolve", status_code=202)
    async def resolve_attempt(attempt_id: str, body: ResolveBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        target = dict(body.target)
        target.setdefault("work_unit_attempt_id", attempt_id) if target.get("target_type") == "WORK_UNIT_ATTEMPT" else None
        # Resolve has no workflow path parameter; resolve the target once to
        # discover its workflow, then reuse the same conditional operation.
        parsed = runtime.repository.get_workflow_for_target(target)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{parsed}", client_key=key, command_name="resolve",
            method="POST", resource_id=attempt_id, target=target, request=body.model_dump(),
            workflow_id=parsed,
            recovery=_event_idempotency_recovery(
                runtime,
                {"WORKFLOW_RESOLVE_TARGETED"},
                lambda row, event: _recover_workflow_command(runtime, row, event, "resolve"),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        snapshot = runtime.repository.targeted_command(
            parsed, "resolve", target,
            expected_state_version=body.expected_state_version,
            expected_target_state_version=body.expected_target_state_version,
            request_id=request_id, decision=body.decision,
            evidence=body.evidence.model_dump(),
            expected_attempt_id=attempt_id,
            source_attempt_id=attempt_id if target.get("target_type") != "WORK_UNIT_ATTEMPT" else None,
        )
        response = _command_response(snapshot, "resolve", request_id)
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=response, workflow_id=parsed)
        return response

    @router.post("/workflows/{workflow_id}/source-imports", status_code=201)
    async def create_source_import(workflow_id: str, body: SourceImportBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"workflow:{workflow_id}", client_key=key, command_name="createSourceImport",
            method="POST", resource_id=workflow_id, target=None, request=body.model_dump(), workflow_id=workflow_id,
            recovery=_durable_idempotency_recovery(
                lambda row: (
                    (201, existing)
                    if (existing := runtime.imports.get_import_by_request_key(workflow_id, key)) is not None
                    else None
                ),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        result = runtime.imports.create_import(workflow_id, metadata=body.metadata, expected_size_bytes=body.expected_size_bytes, expected_sha256=body.expected_sha256, content_type=body.content_type, request_key=key)
        runtime.repository.complete_idempotency(idem_id, response_status=201, response=result, workflow_id=workflow_id)
        return result

    @router.get("/source-imports/{import_id}")
    async def get_source_import(import_id: str):
        runtime.ensure_initialized()
        return runtime.imports.get_import(import_id)

    @router.post("/source-imports/{import_id}/generations", status_code=201)
    async def create_generation(import_id: str, body: GenerationBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        source_import = runtime.imports.get_import(import_id)
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"source-import:{import_id}", client_key=key, command_name="createSourceImportGeneration",
            method="POST", resource_id=import_id, target=None, request=body.model_dump(), workflow_id=source_import["workflow_id"],
            recovery=_event_idempotency_recovery(
                runtime,
                {"SOURCE_GENERATION_CREATED"},
                lambda row, event: _recover_source_generation(runtime, row, event, import_id),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        request_id = str(idem_id)
        runtime.imports.create_generation(
            import_id,
            expected_state_version=body.expected_state_version,
            request_id=request_id,
        )
        result = runtime.imports.get_import(import_id)
        runtime.repository.complete_idempotency(idem_id, response_status=201, response=result, workflow_id=source_import["workflow_id"])
        return result

    @router.get("/source-imports/{import_id}/generations/{generation}")
    async def get_generation(import_id: str, generation: int):
        runtime.ensure_initialized()
        return runtime.imports.get_generation(import_id, generation)

    @router.post("/source-imports/{import_id}/generations/{generation}/writer-tickets", status_code=201)
    async def create_writer_ticket(import_id: str, generation: int, body: GenerationBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        source_generation = runtime.imports.get_generation(import_id, generation)
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"source-generation:{import_id}:{generation}", client_key=key,
            command_name="createSourceWriterTicket", method="POST",
            resource_id=import_id, target={"generation": generation}, request=body.model_dump(), workflow_id=source_generation["workflow_id"],
        )
        if cached is not None:
            # Writer grants are one-time capabilities, not replayable API
            # results. Never return a cached grant (including one persisted
            # by an older build); use a fresh idempotency key after a lost
            # response so the credential cannot be reused.
            raise ConflictError(
                "one-time source writer grant cannot be replayed; retry with a new idempotency key",
                code="IDEMPOTENCY_CONFLICT",
            )
        grant = runtime.imports.acquire_writer(
            import_id, generation, expected_state_version=body.expected_state_version,
        )
        result = {
            "grant": grant.token,
            "source_import_id": import_id,
            "generation": generation,
            "expires_at": grant.expires_at,
        }
        runtime.repository.complete_idempotency(
            idem_id,
            response_status=201,
            response={
                "source_import_id": import_id,
                "generation": generation,
                "expires_at": grant.expires_at,
                "one_time": True,
            },
            workflow_id=source_generation["workflow_id"],
        )
        return result

    @router.put("/source-imports/{import_id}/content", status_code=201)
    async def write_source_content(import_id: str, request: Request, idempotency_key: str = Header(..., alias="X-Idempotency-Key"), staging_generation: int = Header(..., alias="X-Staging-Generation"), source_write_grant: str = Header(..., alias="X-Source-Write-Grant")):
        runtime.ensure_initialized()
        source_generation = runtime.imports.get_generation(import_id, staging_generation)
        # Reject malformed idempotency keys before consuming an untrusted request body.
        key = _idempotency_key(idempotency_key)
        max_bytes = runtime.artifacts.max_bytes
        with tempfile.TemporaryFile() as body:
            size = 0
            digest = hashlib.sha256()
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise ArtifactTooLarge("request body exceeds the local storage budget")
                digest.update(chunk)
                body.write(chunk)
            content_hash_value = digest.hexdigest()
            idem_id, cached = runtime.repository.begin_idempotency(
                scope=f"source-import:{import_id}", client_key=key, command_name="writeSourceImportContent",
                method="PUT", resource_id=import_id, target={"generation": staging_generation},
                request={"generation": staging_generation, "size_bytes": size, "sha256": content_hash_value},
                workflow_id=source_generation["workflow_id"],
                recovery=_durable_idempotency_recovery(
                    lambda row: _recover_source_write(runtime, import_id, size, content_hash_value),
                ),
            )
            if cached is not None:
                return JSONResponse(cached, status_code=200)
            body.seek(0)
            result = await asyncio.to_thread(runtime.imports.write_generation, import_id, staging_generation, body, grant=source_write_grant, format=(request.headers.get("X-Artifact-Format") or "bin"))
        result = runtime.imports.get_import(import_id)
        runtime.repository.complete_idempotency(idem_id, response_status=201, response=result)
        return result

    @router.post("/source-imports/{import_id}/abort", status_code=202)
    async def abort_source_import(import_id: str, body: SourceImportCommandBody, idempotency_key: str = Header(..., alias="X-Idempotency-Key")):
        runtime.ensure_initialized()
        source_import = runtime.imports.get_import(import_id)
        key = _idempotency_key(idempotency_key)
        idem_id, cached = runtime.repository.begin_idempotency(
            scope=f"source-import:{import_id}", client_key=key, command_name="abortSourceImport",
            method="POST", resource_id=import_id, target=None, request=body.model_dump(),
            workflow_id=source_import["workflow_id"],
            recovery=_durable_idempotency_recovery(
                lambda row: (
                    (202, current)
                    if str((current := runtime.imports.get_import(import_id)).get("status")) == "ABORTED"
                    else None
                ),
            ),
        )
        if cached is not None:
            return JSONResponse(cached, status_code=200)
        runtime.imports.abort(import_id, expected_state_version=body.expected_state_version)
        result = runtime.imports.get_import(import_id)
        runtime.repository.complete_idempotency(idem_id, response_status=202, response=result)
        return result

    @router.post("/workflows/{workflow_id}/event-tickets", status_code=201)
    async def create_event_ticket(workflow_id: str, body: EventTicketBody):
        runtime.ensure_initialized()
        runtime.repository.get_workflow(workflow_id)
        token, _expires = runtime.tickets.issue(action="workflow-events", resource_id=workflow_id, audience="renderer", ttl_seconds=60)
        return {"ticket": token, "workflow_id": workflow_id, "expires_at": _expires_at_iso(60)}

    @router.get("/workflows/{workflow_id}/events")
    async def stream_events(workflow_id: str, request: Request, last_event_id: str | None = Header(None, alias="Last-Event-ID"), sse_ticket: str = Header(..., alias="X-SSE-Ticket")):
        runtime.ensure_initialized()
        runtime.tickets.consume(sse_ticket, action="workflow-events", resource_id=workflow_id, audience="renderer")
        snapshot = runtime.repository.get_workflow(workflow_id)
        if last_event_id:
            # Validate the cursor before opening the response body so a stale
            # cursor is a normal 410 response, not an opaque stream failure.
            events = runtime.repository.events.read_after(workflow_id, last_event_id=last_event_id)
            cursor = events[-1].seq if events else snapshot.latest_seq
            initial = []
        else:
            events = []
            cursor = snapshot.latest_seq
            initial = [runtime.repository.events.snapshot_frame(snapshot.as_dict(), workflow_id=workflow_id, seq=snapshot.latest_seq, event_id=snapshot.latest_event_id)]

        async def event_stream():
            nonlocal cursor
            for frame in initial:
                yield frame
            for frame in runtime.repository.events.sse(events):
                yield frame
            idle = 0.0
            while idle < 30.0:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.25)
                try:
                    fresh = runtime.repository.events.read_after(workflow_id, after_seq=cursor)
                except CursorExpired:
                    # Compaction can happen after the initial cursor
                    # validation but before this live poll. Do not let the
                    # streaming generator die with a transport error: publish
                    # the current authoritative snapshot and re-anchor the
                    # cursor so the next poll continues from a known point.
                    recovered = runtime.repository.get_workflow(workflow_id)
                    cursor = recovered.latest_seq
                    yield runtime.repository.events.snapshot_frame(
                        recovered.as_dict(),
                        workflow_id=workflow_id,
                        seq=recovered.latest_seq,
                        event_id=recovered.latest_event_id,
                    )
                    idle = 0.0
                    continue
                if fresh:
                    for frame in runtime.repository.events.sse(fresh):
                        yield frame
                    idle = 0.0
                    cursor = fresh[-1].seq
                else:
                    idle += 0.25
                    if int(idle * 4) % 20 == 0:
                        yield ": heartbeat\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    @router.post("/artifacts/{artifact_id}/content-tickets", status_code=201)
    async def create_artifact_ticket(artifact_id: str):
        runtime.ensure_initialized()
        with runtime.database.read_transaction() as con:
            row = con.execute(
                """SELECT a.artifact_id, a.workflow_id, a.item_id, a.artifact_type,
                          a.format AS artifact_format, a.sha256 AS artifact_sha256,
                          a.size_bytes AS artifact_size_bytes, a.lifecycle_state, a.verified,
                          b.storage_key, b.format AS blob_format,
                          b.size_bytes AS blob_size_bytes, b.sha256 AS blob_sha256,
                          b.lifecycle_state AS blob_lifecycle_state,
                          wi.sequence AS item_sequence,
                          si.error_details_json AS source_details_json
                   FROM artifacts a
                   LEFT JOIN artifact_blobs b ON b.blob_id=a.blob_id
                   LEFT JOIN work_items wi ON wi.workflow_id=a.workflow_id AND wi.item_id=a.item_id
                   LEFT JOIN workflows w ON w.workflow_id=a.workflow_id
                   LEFT JOIN artifacts source_a ON source_a.artifact_id=w.source_artifact_id
                   LEFT JOIN source_imports si ON si.current_artifact_id=source_a.artifact_id
                   WHERE a.artifact_id=?""",
                (artifact_id,),
            ).fetchone()
        if (
            row is None
            or row["lifecycle_state"] != "READY"
            or int(row["verified"] or 0) != 1
            or row["blob_lifecycle_state"] != "READY"
            or not _artifact_row_is_readable(row)
        ):
            raise NotFoundError("artifact does not exist")
        metadata = _artifact_content_metadata(runtime, row)
        token, _expires = runtime.tickets.issue(action="artifact-content", resource_id=artifact_id, audience="renderer", ttl_seconds=60)
        return {
            "ticket": token,
            "artifact_id": artifact_id,
            "expires_at": _expires_at_iso(60),
            "content_type": metadata["mime_type"],
            "content_length": metadata["size_bytes"],
            "sha256": metadata["sha256"],
            "filename": metadata["filename"],
        }

    @router.get("/artifacts/{artifact_id}/content")
    async def read_artifact(artifact_id: str, artifact_ticket: str = Header(..., alias="X-Artifact-Ticket")):
        runtime.ensure_initialized()
        runtime.tickets.consume(artifact_ticket, action="artifact-content", resource_id=artifact_id, audience="renderer")
        with runtime.database.read_transaction() as con:
            row = con.execute(
                """SELECT a.artifact_id, a.workflow_id, a.item_id, a.artifact_type,
                          a.format AS artifact_format, a.sha256 AS artifact_sha256,
                          a.size_bytes AS artifact_size_bytes, a.lifecycle_state,
                          b.storage_key, b.format AS blob_format,
                          b.size_bytes AS blob_size_bytes, b.sha256 AS blob_sha256,
                          b.lifecycle_state AS blob_lifecycle_state,
                          wi.sequence AS item_sequence,
                          si.error_details_json AS source_details_json
                   FROM artifacts a
                   JOIN artifact_blobs b ON b.blob_id=a.blob_id
                   LEFT JOIN work_items wi ON wi.workflow_id=a.workflow_id AND wi.item_id=a.item_id
                   LEFT JOIN workflows w ON w.workflow_id=a.workflow_id
                   LEFT JOIN artifacts source_a ON source_a.artifact_id=w.source_artifact_id
                   LEFT JOIN source_imports si ON si.current_artifact_id=source_a.artifact_id
                   WHERE a.artifact_id=? AND a.lifecycle_state='READY' AND a.verified=1
                     AND b.lifecycle_state='READY'""",
                (artifact_id,),
            ).fetchone()
        if row is None or not _artifact_row_is_readable(row):
            raise NotFoundError("artifact does not exist")
        file_obj = runtime.artifacts.read(str(row["storage_key"]))

        def chunks():
            try:
                while True:
                    chunk = file_obj.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                file_obj.close()

        metadata = _artifact_content_metadata(runtime, row)
        headers = {
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(metadata['filename'], safe='')}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "none",
        }
        if metadata["size_bytes"] is not None:
            headers["Content-Length"] = str(metadata["size_bytes"])
        if metadata["sha256"]:
            headers["ETag"] = f'"{metadata["sha256"]}"'
            headers["X-Artifact-SHA256"] = metadata["sha256"]
        # HTTP field values are latin-1 encoded by Starlette.  Keep the
        # browser-readable RFC 5987 filename in Content-Disposition above and
        # expose an ASCII percent-encoded copy for Electron clients that read
        # response headers directly.
        headers["X-Artifact-Filename"] = quote(metadata["filename"], safe="")
        return StreamingResponse(chunks(), media_type=metadata["mime_type"], headers=headers)

    app.include_router(router)

    async def repository_error_handler(request: Request, exc: RepositoryError):
        _release_failed_idempotency(request, exc)
        status_code = _status_for_error(exc)
        return JSONResponse(_error_payload(exc, request=request), status_code=status_code, headers=_response_headers(status_code))

    async def domain_error_handler(request: Request, exc: DomainError):
        _release_failed_idempotency(request, exc)
        status_code = _status_for_error(exc)
        return JSONResponse(_error_payload(exc, request=request), status_code=status_code, headers=_response_headers(status_code))

    async def ticket_error_handler(request: Request, exc: TicketError):
        status_code = 410 if isinstance(exc, TicketExpired) else 401
        return JSONResponse(_error_payload(exc, request=request), status_code=status_code, headers=_response_headers(status_code))

    async def event_error_handler(request: Request, exc: EventStoreError):
        _release_failed_idempotency(request, exc)
        status_code = _status_for_error(exc)
        return JSONResponse(_error_payload(exc, request=request), status_code=status_code, headers=_response_headers(status_code))

    async def provider_error_handler(request: Request, exc: ProviderError):
        _release_failed_idempotency(request, exc)
        status_code = _status_for_error(exc)
        return JSONResponse(_error_payload(exc, request=request), status_code=status_code, headers=_response_headers(status_code))

    async def request_validation_error_handler(request: Request, exc: RequestValidationError):
        # Do not echo submitted values (which may contain document metadata or
        # credentials) into the public error body.  The detailed validation
        # tree remains available to the server's controlled diagnostics.
        error = DomainError("VALIDATION_ERROR", "request body or headers failed validation")
        _release_failed_idempotency(request, error)
        return JSONResponse(_error_payload(error, request=request), status_code=400)

    app.add_exception_handler(RepositoryError, repository_error_handler)
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(TicketError, ticket_error_handler)
    app.add_exception_handler(EventStoreError, event_error_handler)
    app.add_exception_handler(ProviderError, provider_error_handler)
    app.add_exception_handler(ArtifactStoreError, repository_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    return runtime
