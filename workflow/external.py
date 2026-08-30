"""Business-key based external-record runtime.

External systems are treated as at-least-once, queryable side effects.  The
service owns local mappings, leases, operation state, verification evidence,
and manual resolution.  It deliberately never retries an ambiguous submit
automatically.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from .domain import canonical_json, content_hash, new_id, utc_now
from .repositories import ConflictError, IdempotencyConflict, LeaseConflict, NotFoundError, RepositoryError
from .side_effect_log import SideEffectIntentLog


class ExternalServiceError(RepositoryError):
    code = "EXTERNAL_RUNTIME_ERROR"


class ExternalVerifyMismatch(ExternalServiceError):
    code = "EXTERNAL_VERIFY_MISMATCH"


class ExternalManualResolutionRequired(ExternalServiceError):
    code = "EXTERNAL_RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class ExternalLookup:
    found: bool
    external_record_id: str | None = None
    business_record_key: str | None = None
    payload_hash: str | None = None
    status: str | None = None
    summary: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExternalSubmission:
    external_record_id: str
    canonical_key: str
    summary: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExternalVerification:
    verified: bool
    external_record_id: str | None = None
    payload_hash: str | None = None
    summary: Mapping[str, Any] | None = None


class ExternalSystemAdapter(Protocol):
    system: str
    account_scope: str

    def lookup(self, business_record_key: str) -> ExternalLookup:
        ...

    def submit(
        self,
        operation_key: str,
        payload: Mapping[str, Any],
        existing: ExternalLookup | None = None,
    ) -> ExternalSubmission:
        ...

    def query(self, operation_key: str, external_record_id: str | None = None) -> ExternalLookup:
        ...

    def verify(self, operation_key: str, payload: Mapping[str, Any], external_record_id: str | None = None) -> ExternalVerification:
        ...


@dataclass(frozen=True)
class ExternalLease:
    lease_id: str
    mapping_id: str
    owner_id: str
    fencing_token: int
    lease_until: str


def _expires(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).isoformat(timespec="milliseconds").replace("+00:00", "Z")


EXTERNAL_LEASE_HEARTBEAT_INTERVAL_SECONDS = 20.0


class _ExternalLeaseHeartbeat:
    """Renew an external-record lease while an adapter call is blocking."""

    def __init__(self, service: "ExternalRecordService", lease: ExternalLease, *, ttl_seconds: int = 60) -> None:
        self._service = service
        self._lease = lease
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._stop = threading.Event()
        self._interval_seconds = max(0.01, min(
            float(EXTERNAL_LEASE_HEARTBEAT_INTERVAL_SECONDS),
            self._ttl_seconds / 2,
        ))
        self._thread = threading.Thread(target=self._run, name="external-lease-heartbeat", daemon=True)

    def __enter__(self) -> "_ExternalLeaseHeartbeat":
        # Extend the lease before entering the adapter so a call that starts
        # close to expiry does not cross the boundary immediately.
        self._lease = self._service.renew_record_lease(self._lease, ttl_seconds=self._ttl_seconds)
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._interval_seconds + 1.0))

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._lease = self._service.renew_record_lease(
                    self._lease,
                    ttl_seconds=self._ttl_seconds,
                )
            except Exception:
                # The post-adapter repository fence remains authoritative.  A
                # failed heartbeat must not be treated as evidence that the
                # external call did not happen.
                continue


def _safe_summary(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep external receipts useful while excluding credential-like data."""

    secret_markers = ("token", "secret", "password", "cookie", "authorization", "credential", "access_key", "refresh")

    def clean(item: Any, key: str = "") -> Any:
        if any(marker in key.lower() for marker in secret_markers):
            return "[REDACTED]"
        if isinstance(item, Mapping):
            return {str(k): clean(v, str(k)) for k, v in list(item.items())[:32]}
        if isinstance(item, (list, tuple)):
            return [clean(v, key) for v in list(item)[:32]]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item if not isinstance(item, str) else item[:512]
        return str(item)[:512]

    result = clean(dict(value or {}))
    return result if isinstance(result, dict) else {}


class ExternalRecordService:
    """Durable external-record and operation coordinator."""

    def __init__(self, database, *, intent_log: SideEffectIntentLog | None = None) -> None:
        self.database = database
        self.intent_log = intent_log or SideEffectIntentLog(database.path.parent / "side_effect_intents.jsonl")

    @contextmanager
    def _transaction_after_intent(
        self,
        *,
        operation_key: str,
        payload_hash: str,
        intent_ref: dict[str, str | None],
    ):
        """Close a known pre-transaction journal record after rollback.

        External operations use the same journal-before-SQLite ordering as
        TTS. A deliberate repository error must not leave a false orphan,
        while an unknown database/commit failure remains ``RECORDED`` and is
        therefore still handled as a fail-closed recovery case.
        """

        try:
            with self.database.transaction() as con:
                yield con
        except RepositoryError:
            intent_id = intent_ref.get("intent_id")
            if intent_id:
                try:
                    with self.database.read_transaction() as con:
                        existing = con.execute(
                            "SELECT 1 FROM side_effect_intents "
                            "WHERE operation_namespace='external' AND operation_key=? LIMIT 1",
                            (operation_key,),
                        ).fetchone()
                except Exception:
                    raise
                if existing is None:
                    try:
                        self.intent_log.abort(
                            operation_namespace="external",
                            operation_key=operation_key,
                            payload_hash=payload_hash,
                            intent_id=intent_id,
                        )
                    except Exception:
                        pass
            raise

    def _require_runtime(self) -> None:
        with self.database.read_transaction() as con:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_records'"
            ).fetchone()
        if exists is None:
            raise ExternalServiceError("ExternalRecord runtime requires the full schema", code="MIGRATION_REQUIRED")

    def ensure_record(
        self,
        workflow_id: str,
        *,
        external_system: str,
        account_scope: str,
        business_record_key: str,
        mapping_version: str,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_runtime()
        self._require_text(external_system, "external_system")
        self._require_text(account_scope, "account_scope")
        self._require_text(business_record_key, "business_record_key")
        self._require_text(mapping_version, "mapping_version")
        now = utc_now()
        with self.database.transaction() as con:
            workflow = con.execute("SELECT workflow_group_id FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            group_id = str(workflow["workflow_group_id"])
            row = con.execute(
                """SELECT * FROM external_records
                   WHERE external_system=? AND external_account_scope=? AND business_record_key=?""",
                (external_system, account_scope, business_record_key),
            ).fetchone()
            if row is None:
                mapping_id = new_id("external-record")
                try:
                    con.execute(
                        """INSERT INTO external_records(
                            external_record_mapping_id, external_system, external_account_scope,
                            business_record_key, external_record_id, current_workflow_group_id,
                            local_workflow_id, local_item_id, current_operation_key, mapping_version,
                            external_status, last_verified_at, last_error, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (mapping_id, external_system, account_scope, business_record_key, None,
                         group_id, workflow_id, item_id, None, mapping_version, "UNKNOWN", None, None, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError(f"external record mapping cannot be created: {exc}") from exc
                row = con.execute("SELECT * FROM external_records WHERE external_record_mapping_id=?", (mapping_id,)).fetchone()
            else:
                if str(row["mapping_version"]) != mapping_version:
                    raise ConflictError("external business key is bound to another mapping version", code="EXTERNAL_MAPPING_VERSION_CONFLICT")
                if row["current_workflow_group_id"] not in (None, group_id):
                    raise ConflictError("external business key is owned by another workflow group", code="EXTERNAL_SCOPE_CONFLICT")
                # A rerun is a new workflow id inside the same immutable
                # workflow group.  Keep the record mapping stable and move
                # only its current local projection; the binding table keeps
                # the complete cross-run history.
                con.execute(
                    """UPDATE external_records SET current_workflow_group_id=COALESCE(current_workflow_group_id, ?),
                       local_workflow_id=?, local_item_id=?, updated_at=?
                       WHERE external_record_mapping_id=?""",
                    (group_id, workflow_id, item_id, now, row["external_record_mapping_id"]),
                )
                row = con.execute("SELECT * FROM external_records WHERE external_record_mapping_id=?", (row["external_record_mapping_id"],)).fetchone()
            return dict(row)

    def get_record(self, mapping_id: str) -> dict[str, Any]:
        self._require_runtime()
        with self.database.read_transaction() as con:
            row = con.execute("SELECT * FROM external_records WHERE external_record_mapping_id=?", (mapping_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"external record mapping does not exist: {mapping_id}")
            return dict(row)

    def find_record(self, *, external_system: str, account_scope: str, business_record_key: str) -> dict[str, Any] | None:
        self._require_runtime()
        with self.database.read_transaction() as con:
            row = con.execute(
                """SELECT * FROM external_records WHERE external_system=? AND external_account_scope=?
                   AND business_record_key=?""",
                (external_system, account_scope, business_record_key),
            ).fetchone()
            return dict(row) if row is not None else None

    def find_record_lease(self, mapping_id: str, owner_id: str) -> ExternalLease | None:
        """Find a still-active lease that can replay a lost lease response."""

        self._require_runtime()
        now = utc_now()
        with self.database.read_transaction() as con:
            row = con.execute(
                """SELECT * FROM external_record_leases
                   WHERE external_record_mapping_id=? AND owner_id=?
                     AND state='ACTIVE' AND lease_until>?""",
                (mapping_id, owner_id, now),
            ).fetchone()
            if row is None:
                return None
            return ExternalLease(
                str(row["lease_id"]),
                str(row["external_record_mapping_id"]),
                str(row["owner_id"]),
                int(row["fencing_token"]),
                str(row["lease_until"]),
            )

    def acquire_record_lease(self, mapping_id: str, owner_id: str, *, ttl_seconds: int = 60) -> ExternalLease:
        self._require_runtime()
        self._require_text(owner_id, "owner_id")
        now = utc_now()
        until = _expires(ttl_seconds)
        lease_id = new_id("external-lease")
        with self.database.transaction() as con:
            if con.execute("SELECT 1 FROM external_records WHERE external_record_mapping_id=?", (mapping_id,)).fetchone() is None:
                raise NotFoundError(f"external record mapping does not exist: {mapping_id}")
            current = con.execute("SELECT * FROM external_record_leases WHERE external_record_mapping_id=?", (mapping_id,)).fetchone()
            if current is not None and current["state"] == "ACTIVE" and str(current["lease_until"]) > now and str(current["owner_id"]) != owner_id:
                raise LeaseConflict("external record lease is held by another owner")
            token = int(current["fencing_token"]) + 1 if current is not None else 1
            if current is None:
                con.execute(
                    """INSERT INTO external_record_leases(
                       lease_id, external_record_mapping_id, owner_id, fencing_token,
                       lease_until, heartbeat_at, state) VALUES (?,?,?,?,?,?,?)""",
                    (lease_id, mapping_id, owner_id, token, until, now, "ACTIVE"),
                )
            else:
                con.execute(
                    """UPDATE external_record_leases SET lease_id=?, owner_id=?, fencing_token=?,
                       lease_until=?, heartbeat_at=?, state='ACTIVE' WHERE external_record_mapping_id=?""",
                    (lease_id, owner_id, token, until, now, mapping_id),
                )
            return ExternalLease(lease_id, mapping_id, owner_id, token, until)

    def renew_record_lease(self, lease: ExternalLease, *, ttl_seconds: int = 60) -> ExternalLease:
        self._require_runtime()
        now = utc_now()
        until = _expires(ttl_seconds)
        with self.database.transaction() as con:
            updated = con.execute(
                """UPDATE external_record_leases SET lease_until=?, heartbeat_at=?
                   WHERE lease_id=? AND external_record_mapping_id=? AND owner_id=?
                     AND fencing_token=? AND state='ACTIVE' AND lease_until>?""",
                (until, now, lease.lease_id, lease.mapping_id, lease.owner_id, lease.fencing_token, now),
            )
            if updated.rowcount != 1:
                raise LeaseConflict("external record lease is stale or expired")
        return ExternalLease(lease.lease_id, lease.mapping_id, lease.owner_id, lease.fencing_token, until)

    def release_record_lease(self, lease: ExternalLease) -> None:
        self._require_runtime()
        with self.database.transaction() as con:
            updated = con.execute(
                """UPDATE external_record_leases SET state='RELEASED', heartbeat_at=?
                   WHERE lease_id=? AND external_record_mapping_id=? AND owner_id=?
                     AND fencing_token=? AND state='ACTIVE'""",
                (utc_now(), lease.lease_id, lease.mapping_id, lease.owner_id, lease.fencing_token),
            )
            if updated.rowcount != 1:
                raise LeaseConflict("external record lease is stale or already released")

    def prepare_operation(
        self,
        workflow_id: str,
        *,
        mapping_id: str,
        operation_key: str,
        payload: Mapping[str, Any],
        mapping_version: str,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_runtime()
        self._require_text(operation_key, "operation_key")
        payload_hash = content_hash(payload)
        journal_key = f"{mapping_id}:{operation_key}"
        now = utc_now()
        intent_ref: dict[str, str | None] = {"intent_id": None}
        with self._transaction_after_intent(
            operation_key=journal_key,
            payload_hash=payload_hash,
            intent_ref=intent_ref,
        ) as con:
            workflow = con.execute("SELECT workflow_group_id FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            mapping = con.execute("SELECT * FROM external_records WHERE external_record_mapping_id=?", (mapping_id,)).fetchone()
            if workflow is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            if mapping is None:
                raise NotFoundError(f"external record mapping does not exist: {mapping_id}")
            if str(mapping["mapping_version"]) != mapping_version:
                raise ConflictError("external operation mapping version is stale", code="EXTERNAL_MAPPING_VERSION_CONFLICT")
            if str(mapping["current_workflow_group_id"] or workflow["workflow_group_id"]) != str(workflow["workflow_group_id"]):
                raise ConflictError("external operation is outside the workflow group", code="EXTERNAL_SCOPE_CONFLICT")
            existing = con.execute(
                """SELECT * FROM external_operations WHERE external_record_mapping_id=? AND external_operation_key=?""",
                (mapping_id, operation_key),
            ).fetchone()
            if existing is not None:
                if str(existing["target_payload_hash"]) != payload_hash or str(existing["workflow_id"]) != workflow_id:
                    raise IdempotencyConflict("external operation key is bound to another payload or workflow")
                journal_id = self.intent_log.record(
                    operation_namespace="external",
                    operation_key=journal_key,
                    payload_hash=payload_hash,
                    workflow_id=workflow_id,
                    provider_account_scope=mapping_id,
                )
                intent_ref["intent_id"] = journal_id
                existing_intent = con.execute(
                    """SELECT intent_id, payload_hash FROM side_effect_intents
                       WHERE workflow_id=? AND operation_namespace='external' AND operation_key=?""",
                    (workflow_id, journal_key),
                ).fetchone()
                if existing_intent is not None and (
                    str(existing_intent["intent_id"]) != journal_id
                    or str(existing_intent["payload_hash"]) != payload_hash
                ):
                    raise IdempotencyConflict("external side-effect intent is bound to another payload or journal entry")
                if existing_intent is None:
                    con.execute(
                        """INSERT INTO side_effect_intents(
                           intent_id, workflow_id, operation_namespace, operation_key, payload_hash,
                           provider_account_scope, state, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (journal_id, workflow_id, "external", journal_key, payload_hash, mapping_id, "RECORDED", now, now),
                    )
                return self._operation_public(existing, journal_id=journal_id)
            active = con.execute(
                """SELECT external_operation_id, external_operation_key, side_effect_state
                   FROM external_operations
                   WHERE external_record_mapping_id=?
                     AND side_effect_state IN ('INTENT_RECORDED', 'IN_FLIGHT', 'SUBMITTED', 'AMBIGUOUS')
                   ORDER BY created_at ASC LIMIT 1""",
                (mapping_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError(
                    "external record already has an active operation",
                    code="EXTERNAL_OPERATION_ACTIVE",
                    details={
                        "external_operation_id": str(active["external_operation_id"]),
                        "external_operation_key": str(active["external_operation_key"]),
                        "side_effect_state": str(active["side_effect_state"]),
                    },
                )
            journal_id = self.intent_log.record(
                operation_namespace="external",
                operation_key=journal_key,
                payload_hash=payload_hash,
                workflow_id=workflow_id,
                provider_account_scope=mapping_id,
            )
            intent_ref["intent_id"] = journal_id
            operation_id = new_id("external-operation")
            try:
                con.execute(
                    """INSERT INTO external_operations(
                       external_operation_id, external_record_mapping_id, workflow_id, item_id,
                       external_operation_key, target_payload_hash, mapping_version, state_version,
                       side_effect_state, receipt_json, created_at, confirmed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (operation_id, mapping_id, workflow_id, item_id, operation_key, payload_hash,
                     mapping_version, 0, "INTENT_RECORDED", "{}", now, None),
                )
                con.execute(
                    """INSERT INTO side_effect_intents(
                       intent_id, workflow_id, operation_namespace, operation_key, payload_hash,
                       provider_account_scope, state, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (journal_id, workflow_id, "external", journal_key, payload_hash, mapping_id, "RECORDED", now, now),
                )
                binding_key = f"external:{mapping_id}:{operation_id}:touched"
                con.execute(
                    """INSERT INTO external_record_bindings(
                       binding_id, binding_key, external_record_mapping_id, workflow_id, item_id,
                       external_operation_id, relation_type, first_touched_at, last_touched_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (new_id("external-binding"), binding_key, mapping_id, workflow_id, item_id,
                     operation_id, "TOUCHED", now, now),
                )
                con.execute(
                    """UPDATE external_records SET local_workflow_id=?,
                       local_item_id=?, current_operation_key=?, external_status='PENDING', updated_at=?
                       WHERE external_record_mapping_id=?""",
                    (workflow_id, item_id, operation_key, now, mapping_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"external operation cannot be created: {exc}") from exc
            row = con.execute("SELECT * FROM external_operations WHERE external_operation_id=?", (operation_id,)).fetchone()
            return self._operation_public(row, journal_id=journal_id)

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        self._require_runtime()
        with self.database.read_transaction() as con:
            row = con.execute(
                """SELECT o.*, r.external_system, r.external_account_scope, r.business_record_key,
                          r.external_record_id, r.external_status
                   FROM external_operations o JOIN external_records r
                     ON r.external_record_mapping_id=o.external_record_mapping_id
                   WHERE o.external_operation_id=?""",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"external operation does not exist: {operation_id}")
            return self._operation_public(row)

    def find_operation(self, mapping_id: str, operation_key: str) -> dict[str, Any] | None:
        """Find a prepared operation by its stable mapping/key pair."""

        self._require_runtime()
        with self.database.read_transaction() as con:
            row = con.execute(
                """SELECT o.*, r.external_system, r.external_account_scope, r.business_record_key,
                          r.external_record_id, r.external_status
                   FROM external_operations o JOIN external_records r
                     ON r.external_record_mapping_id=o.external_record_mapping_id
                   WHERE o.external_record_mapping_id=? AND o.external_operation_key=?""",
                (mapping_id, operation_key),
            ).fetchone()
            return self._operation_public(row) if row is not None else None

    def begin_operation(self, operation_id: str, lease: ExternalLease) -> dict[str, Any]:
        self._require_runtime()
        now = utc_now()
        with self.database.transaction() as con:
            operation = con.execute("SELECT * FROM external_operations WHERE external_operation_id=?", (operation_id,)).fetchone()
            self._require_lease(con, lease, now)
            if operation is None:
                raise NotFoundError(f"external operation does not exist: {operation_id}")
            if str(operation["external_record_mapping_id"]) != lease.mapping_id:
                raise ConflictError("external operation lease does not match mapping", code="STALE_ATTEMPT")
            if operation["side_effect_state"] in {"AMBIGUOUS", "SUBMITTED", "CONFIRMED", "REJECTED"}:
                raise ConflictError("external operation already crossed or resolved its side-effect boundary")
            updated = con.execute(
                """UPDATE external_operations SET side_effect_state='IN_FLIGHT', state_version=state_version+1
                   WHERE external_operation_id=? AND state_version=? AND side_effect_state IN ('NOT_STARTED','INTENT_RECORDED')""",
                (operation_id, operation["state_version"]),
            )
            if updated.rowcount != 1:
                raise ConflictError("external operation changed while starting")
            con.execute(
                """UPDATE external_records SET external_status='PENDING', current_operation_key=?, updated_at=?
                   WHERE external_record_mapping_id=?""",
                (operation["external_operation_key"], now, lease.mapping_id),
            )
            con.execute(
                """UPDATE side_effect_intents SET state='COMMITTED', updated_at=?
                   WHERE workflow_id=? AND operation_namespace='external' AND operation_key=?""",
                (now, operation["workflow_id"], f"{lease.mapping_id}:{operation['external_operation_key']}"),
            )
            row = con.execute("SELECT * FROM external_operations WHERE external_operation_id=?", (operation_id,)).fetchone()
        self.intent_log.mark(
            operation_namespace="external",
            operation_key=f"{lease.mapping_id}:{row['external_operation_key']}",
            state="COMMITTED",
        )
        return self._operation_public(row)

    def record_submission(
        self,
        operation_id: str,
        lease: ExternalLease,
        submission: ExternalSubmission | Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require_runtime()
        external_id = str(submission.external_record_id if isinstance(submission, ExternalSubmission) else submission.get("external_record_id") or "")
        canonical_key_value = str(submission.canonical_key if isinstance(submission, ExternalSubmission) else submission.get("canonical_key") or external_id)
        summary = submission.summary if isinstance(submission, ExternalSubmission) else submission.get("summary")
        if not external_id or not canonical_key_value:
            raise ExternalServiceError("external submission needs an id and canonical key", code="VALIDATION_ERROR")
        now = utc_now()
        with self.database.transaction() as con:
            operation = self._operation_row(con, operation_id)
            self._require_lease(con, lease, now)
            if str(operation["external_record_mapping_id"]) != lease.mapping_id:
                raise ConflictError("external operation lease does not match mapping", code="STALE_ATTEMPT")
            existing_receipt: dict[str, Any] = {}
            try:
                parsed_receipt = json.loads(str(operation["receipt_json"] or "{}"))
                if isinstance(parsed_receipt, dict):
                    existing_receipt = parsed_receipt
            except (TypeError, json.JSONDecodeError):
                existing_receipt = {}
            if operation["side_effect_state"] == "CONFIRMED":
                if existing_receipt and (
                    str(existing_receipt.get("external_record_id") or "") != external_id
                    or str(existing_receipt.get("canonical_key") or "") != canonical_key_value
                ):
                    raise ConflictError(
                        "external operation already has a conflicting receipt",
                        code="IDEMPOTENCY_CONFLICT",
                    )
                return self._operation_public(operation)
            if operation["side_effect_state"] not in {"IN_FLIGHT", "SUBMITTED", "AMBIGUOUS"}:
                raise ConflictError("external operation is not ready to observe a submission")
            # A receipt is an immutable observation of the external side
            # effect. Re-observing the same receipt is idempotent; a different
            # id or canonical key must never overwrite the first observation.
            if existing_receipt:
                if (
                    str(existing_receipt.get("external_record_id") or "") != external_id
                    or str(existing_receipt.get("canonical_key") or "") != canonical_key_value
                ):
                    raise ConflictError(
                        "external operation already has a conflicting receipt",
                        code="IDEMPOTENCY_CONFLICT",
                    )
                return self._operation_public(operation)
            mapping = con.execute("SELECT * FROM external_records WHERE external_record_mapping_id=?", (lease.mapping_id,)).fetchone()
            existed = mapping["external_record_id"] is not None if mapping is not None else False
            receipt = {
                "external_record_id": external_id,
                "canonical_key": canonical_key_value,
                "summary": _safe_summary(summary if isinstance(summary, Mapping) else {}),
            }
            con.execute(
                """UPDATE external_operations SET side_effect_state='SUBMITTED', receipt_json=?, state_version=state_version+1
                   WHERE external_operation_id=?""",
                (canonical_json(receipt), operation_id),
            )
            con.execute(
                """UPDATE external_records SET external_record_id=?, external_status=?, current_operation_key=?,
                   last_error=NULL, updated_at=? WHERE external_record_mapping_id=?""",
                (external_id, "UPDATED" if existed else "CREATED", operation["external_operation_key"], now, lease.mapping_id),
            )
            relation = "UPDATED" if existed else "CREATED"
            self._upsert_binding(con, lease.mapping_id, str(operation["workflow_id"]), operation["item_id"], operation_id, relation, now)
            row = self._operation_row(con, operation_id)
        self.intent_log.mark(
            operation_namespace="external",
            operation_key=f"{lease.mapping_id}:{row['external_operation_key']}",
            # SQLite models the durable external intent as COMMITTED once the
            # provider returned an observed record.  Keep the append-only
            # journal on the same state so strict backup verification compares
            # one canonical state machine instead of the provider's
            # operation-level SUBMITTED label.
            state="COMMITTED",
        )
        return self._operation_public(row)

    def verify_operation(
        self,
        operation_id: str,
        lease: ExternalLease,
        adapter: ExternalSystemAdapter,
        payload: Mapping[str, Any],
        *,
        evidence_source: str = "provider-query",
    ) -> dict[str, Any]:
        self._require_runtime()
        operation = self.assert_operation_lease(operation_id, lease)
        with _ExternalLeaseHeartbeat(self, lease):
            verification = adapter.verify(
                str(operation["external_operation_key"]), payload, operation.get("external_record_id")
            )
        if not verification.verified:
            self.mark_ambiguous(operation_id, lease, error_code="EXTERNAL_VERIFY_MISMATCH")
            raise ExternalVerifyMismatch("external verification does not match the intended payload")
        return self.confirm_operation(
            operation_id,
            lease,
            external_record_id=verification.external_record_id or operation.get("external_record_id"),
            evidence_source=evidence_source,
            evidence_hash=content_hash({"operation_id": operation_id, "payload_hash": content_hash(payload), "summary": _safe_summary(verification.summary)}),
            evidence={"summary": _safe_summary(verification.summary)},
        )

    def reconcile(self, operation_id: str, lease: ExternalLease, adapter: ExternalSystemAdapter) -> dict[str, Any]:
        """Query the external system; never submit from this path."""
        self._require_runtime()
        operation = self.assert_operation_lease(operation_id, lease)
        with _ExternalLeaseHeartbeat(self, lease):
            lookup = adapter.query(str(operation["external_operation_key"]), operation.get("external_record_id"))
        if not lookup.found:
            self.mark_ambiguous(operation_id, lease, error_code="EXTERNAL_NOT_FOUND_REQUIRES_MANUAL")
            return {**self.get_operation(operation_id), "reconciliation": "NOT_FOUND_MANUAL_REQUIRED"}
        observed_hash = lookup.payload_hash
        if observed_hash and observed_hash != operation["target_payload_hash"]:
            self.mark_ambiguous(operation_id, lease, error_code="EXTERNAL_VERIFY_MISMATCH")
            raise ExternalVerifyMismatch("external query found a record with a different payload hash")
        return self.confirm_operation(
            operation_id,
            lease,
            external_record_id=lookup.external_record_id,
            evidence_source="external-query",
            evidence_hash=content_hash({"operation_id": operation_id, "lookup": _safe_summary(lookup.summary)}),
            evidence={"summary": _safe_summary(lookup.summary), "status": lookup.status},
        )

    def assert_operation_lease(self, operation_id: str, lease: ExternalLease) -> dict[str, Any]:
        """Recheck the fencing lease immediately before an adapter call.

        The adapter call itself is outside SQLite, so callers must perform
        this check at the last possible local boundary.  A response received
        after the lease expires is still treated as uncertain by the later
        record/confirm write and is left for reconciliation rather than being
        accepted by a stale worker.
        """

        self._require_runtime()
        with self.database.read_transaction() as con:
            operation = self._operation_row(con, operation_id)
            self._require_lease(con, lease, utc_now())
            if str(operation["external_record_mapping_id"]) != lease.mapping_id:
                raise ConflictError("external operation lease does not match mapping", code="STALE_ATTEMPT")
            if str(operation["side_effect_state"]) not in {"IN_FLIGHT", "SUBMITTED", "AMBIGUOUS"}:
                raise ConflictError("external operation is not ready for an adapter call")
            return self._operation_public(operation)

    def submit_operation(
        self,
        operation_id: str,
        lease: ExternalLease,
        adapter: ExternalSystemAdapter,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Submit once through an adapter after a final lease/hash check.

        An adapter exception is conservatively ambiguous.  If the lease is
        still live, the operation is marked locally; if the lease expired,
        the operation remains in-flight for restart recovery to reconcile.
        This method never retries an adapter call.
        """

        operation = self.assert_operation_lease(operation_id, lease)
        payload_hash = content_hash(payload)
        if payload_hash != str(operation["target_payload_hash"]):
            raise ConflictError("external payload does not match the prepared operation", code="IDEMPOTENCY_CONFLICT")
        try:
            with _ExternalLeaseHeartbeat(self, lease):
                submission = adapter.submit(str(operation["external_operation_key"]), payload)
        except Exception:
            try:
                self.mark_ambiguous(operation_id, lease, error_code="EXTERNAL_SUBMIT_UNKNOWN")
            except (ConflictError, RepositoryError):
                # A stale lease or unavailable local store leaves the durable
                # IN_FLIGHT fact for the recovery scanner; never resubmit.
                pass
            raise
        return self.record_submission(operation_id, lease, submission)

    def mark_ambiguous(self, operation_id: str, lease: ExternalLease, *, error_code: str) -> dict[str, Any]:
        self._require_runtime()
        now = utc_now()
        with self.database.transaction() as con:
            operation = self._operation_row(con, operation_id)
            self._require_lease(con, lease, now)
            if str(operation["external_record_mapping_id"]) != lease.mapping_id:
                raise ConflictError("external operation lease does not match mapping", code="STALE_ATTEMPT")
            con.execute(
                """UPDATE external_operations SET side_effect_state='AMBIGUOUS', state_version=state_version+1
                   WHERE external_operation_id=? AND side_effect_state <> 'CONFIRMED'""",
                (operation_id,),
            )
            con.execute(
                """UPDATE external_records SET external_status='AMBIGUOUS', last_error=?, updated_at=?
                   WHERE external_record_mapping_id=?""",
                (error_code, now, lease.mapping_id),
            )
            intervention_id = f"intervention_external_{operation_id}"
            if con.execute("SELECT 1 FROM user_interventions WHERE intervention_id=?", (intervention_id,)).fetchone() is None:
                con.execute(
                    """INSERT INTO user_interventions(
                       intervention_id, workflow_id, step_id, attempt_id, work_unit_id,
                       intervention_type, reason, owner_id, state, evidence_json,
                       expires_at, resolved_by, resolved_at, state_version, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (intervention_id, operation["workflow_id"], None, None, None,
                     "RECONCILE_EXTERNAL", error_code, None, "OPEN", "{}", None, None, None, 0, now, now),
                )
            con.execute(
                """UPDATE side_effect_intents SET state='NEEDS_RECONCILE', updated_at=?
                   WHERE workflow_id=? AND operation_namespace='external' AND operation_key=?""",
                (now, operation["workflow_id"], f"{lease.mapping_id}:{operation['external_operation_key']}"),
            )
            row = self._operation_row(con, operation_id)
        self.intent_log.mark(
            operation_namespace="external",
            operation_key=f"{lease.mapping_id}:{row['external_operation_key']}",
            state="NEEDS_RECONCILE",
        )
        return self._operation_public(row)

    def confirm_operation(
        self,
        operation_id: str,
        lease: ExternalLease,
        *,
        external_record_id: str | None,
        evidence_source: str,
        evidence_hash: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_runtime()
        if not external_record_id:
            raise ExternalServiceError("confirmed external operation needs an external record id", code="VALIDATION_ERROR")
        if len(str(evidence_hash)) < 16:
            raise ExternalServiceError("evidence_hash is required", code="VALIDATION_ERROR")
        now = utc_now()
        with self.database.transaction() as con:
            operation = self._operation_row(con, operation_id)
            self._require_lease(con, lease, now)
            if str(operation["external_record_mapping_id"]) != lease.mapping_id:
                raise ConflictError("external operation lease does not match mapping", code="STALE_ATTEMPT")
            receipt = {}
            try:
                receipt = json.loads(str(operation["receipt_json"]))
            except (TypeError, json.JSONDecodeError):
                receipt = {}
            if not isinstance(receipt, dict):
                receipt = {}
            operation_state = str(operation["side_effect_state"])
            if operation_state == "CONFIRMED":
                existing_id = str(receipt.get("external_record_id") or "")
                if existing_id == str(external_record_id):
                    return self._operation_public(operation)
                raise ConflictError(
                    "confirmed external operation has a conflicting receipt",
                    code="IDEMPOTENCY_CONFLICT",
                )
            if operation_state == "REJECTED":
                raise ConflictError("rejected external operation cannot be confirmed", code="STATE_CONFLICT")
            if operation_state not in {"IN_FLIGHT", "SUBMITTED", "AMBIGUOUS"}:
                raise ConflictError("external operation is not ready to confirm", code="STATE_CONFLICT")
            existing_id = str(receipt.get("external_record_id") or "")
            if existing_id and existing_id != str(external_record_id):
                raise ConflictError(
                    "external operation has a conflicting receipt",
                    code="IDEMPOTENCY_CONFLICT",
                )
            receipt["external_record_id"] = str(external_record_id)
            con.execute(
                """UPDATE external_operations SET side_effect_state='CONFIRMED',
                   receipt_json=?, confirmed_at=COALESCE(confirmed_at, ?), state_version=state_version+1
                   WHERE external_operation_id=?""",
                (canonical_json(receipt), now, operation_id),
            )
            con.execute(
                """UPDATE external_records SET external_record_id=?, external_status='VERIFIED',
                   last_verified_at=?, last_error=NULL, updated_at=? WHERE external_record_mapping_id=?""",
                (str(external_record_id), now, now, lease.mapping_id),
            )
            self._upsert_binding(con, lease.mapping_id, str(operation["workflow_id"]), operation["item_id"], operation_id, "VERIFIED", now)
            con.execute(
                """INSERT INTO reconcile_evidence(
                   evidence_id, workflow_id, source_attempt_id, target_type, target_id,
                   evidence_source, evidence_hash, evidence_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (new_id("evidence"), operation["workflow_id"], None, "EXTERNAL_OPERATION", operation_id,
                 evidence_source, str(evidence_hash), canonical_json(_safe_summary(evidence)), now),
            )
            con.execute(
                """UPDATE user_interventions SET state='RESOLVED', evidence_json=?, resolved_by='external-runtime',
                   resolved_at=?, state_version=state_version+1, updated_at=?
                   WHERE intervention_id=? AND state IN ('OPEN','CLAIMED')""",
                (canonical_json(_safe_summary(evidence)), now, now, f"intervention_external_{operation_id}"),
            )
            con.execute(
                """UPDATE side_effect_intents SET state='ARCHIVED', updated_at=?
                   WHERE workflow_id=? AND operation_namespace='external' AND operation_key=?""",
                (now, operation["workflow_id"], f"{lease.mapping_id}:{operation['external_operation_key']}"),
            )
            row = self._operation_row(con, operation_id)
        self.intent_log.mark(
            operation_namespace="external",
            operation_key=f"{lease.mapping_id}:{row['external_operation_key']}",
            state="ARCHIVED",
        )
        return self._operation_public(row)

    def resolve_operation(
        self,
        operation_id: str,
        *,
        decision: str,
        evidence_source: str,
        evidence_hash: str,
        evidence: Mapping[str, Any] | None = None,
        resolved_by: str = "desktop",
    ) -> dict[str, Any]:
        self._require_runtime()
        if decision not in {"CONFIRMED", "NOT_SUBMITTED", "BLOCKED"}:
            raise ExternalServiceError("unsupported external resolution decision", code="VALIDATION_ERROR")
        if len(str(evidence_hash)) < 16:
            raise ExternalServiceError("evidence_hash is required", code="VALIDATION_ERROR")
        now = utc_now()
        with self.database.transaction() as con:
            operation = self._operation_row(con, operation_id)
            mapping = con.execute("SELECT * FROM external_records WHERE external_record_mapping_id=?", (operation["external_record_mapping_id"],)).fetchone()
            if mapping is None:
                raise RepositoryError("external record mapping is missing", code="PERSISTENCE_ERROR")
            operation_state = str(operation["side_effect_state"])
            desired_state = {
                "CONFIRMED": "CONFIRMED",
                "NOT_SUBMITTED": "REJECTED",
                "BLOCKED": "AMBIGUOUS",
            }[decision]

            # A manual decision is still a state-machine transition.  In
            # particular, an observed receipt is durable evidence that the
            # side-effect boundary was crossed; it must never be overwritten
            # by a later "not submitted" decision.  Likewise, a resolved
            # operation is historical and cannot be downgraded by a new
            # idempotency key.
            if operation_state in {"CONFIRMED", "REJECTED"}:
                if operation_state != desired_state:
                    raise ConflictError(
                        "a resolved external operation cannot be downgraded",
                        code="EXTERNAL_STATE_CONFLICT",
                    )
                return self._operation_public(operation)
            if operation_state == "SUBMITTED" and decision == "NOT_SUBMITTED":
                raise ConflictError(
                    "an observed external submission cannot be resolved as not submitted",
                    code="EXTERNAL_STATE_CONFLICT",
                )

            receipt: dict[str, Any] = {}
            try:
                parsed_receipt = json.loads(str(operation["receipt_json"] or "{}"))
                if isinstance(parsed_receipt, dict):
                    receipt = parsed_receipt
            except (TypeError, json.JSONDecodeError):
                receipt = {}
            if decision == "CONFIRMED" and not str(receipt.get("external_record_id") or "").strip():
                raise ExternalServiceError(
                    "confirmed external operation requires an observed external_record_id",
                    code="EVIDENCE_REQUIRED",
                )
            op_state = desired_state
            record_state = "VERIFIED" if decision == "CONFIRMED" else "NOT_FOUND" if decision == "NOT_SUBMITTED" else "BLOCKED"
            con.execute(
                """UPDATE external_operations SET side_effect_state=?, confirmed_at=?, state_version=state_version+1
                   WHERE external_operation_id=?""",
                (op_state, now if decision == "CONFIRMED" else None, operation_id),
            )
            con.execute(
                """UPDATE external_records SET external_status=?, last_error=?, last_verified_at=?, updated_at=?
                   WHERE external_record_mapping_id=?""",
                (record_state, None if decision == "CONFIRMED" else ("MANUAL_NOT_SUBMITTED" if decision == "NOT_SUBMITTED" else "MANUAL_BLOCK"), now if decision == "CONFIRMED" else None, now, operation["external_record_mapping_id"]),
            )
            if decision == "CONFIRMED":
                self._upsert_binding(con, str(operation["external_record_mapping_id"]), str(operation["workflow_id"]), operation["item_id"], operation_id, "VERIFIED", now)
            con.execute(
                """INSERT INTO reconcile_evidence(
                   evidence_id, workflow_id, source_attempt_id, target_type, target_id,
                   evidence_source, evidence_hash, evidence_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (new_id("evidence"), operation["workflow_id"], None, "EXTERNAL_OPERATION", operation_id,
                 evidence_source, str(evidence_hash), canonical_json(_safe_summary(evidence)), now),
            )
            con.execute(
                """UPDATE user_interventions SET state='RESOLVED', evidence_json=?, resolved_by=?,
                   resolved_at=?, state_version=state_version+1, updated_at=?
                   WHERE intervention_id=? AND state IN ('OPEN','CLAIMED')""",
                (canonical_json(_safe_summary(evidence)), resolved_by, now, now, f"intervention_external_{operation_id}"),
            )
            con.execute(
                """UPDATE side_effect_intents SET state=?, updated_at=?
                   WHERE workflow_id=? AND operation_namespace='external' AND operation_key=?""",
                ("ARCHIVED" if decision != "BLOCKED" else "NEEDS_RECONCILE", now, operation["workflow_id"], f"{operation['external_record_mapping_id']}:{operation['external_operation_key']}"),
            )
            row = self._operation_row(con, operation_id)
        self.intent_log.mark(
            operation_namespace="external",
            operation_key=f"{row['external_record_mapping_id']}:{row['external_operation_key']}",
            state="ARCHIVED" if decision != "BLOCKED" else "NEEDS_RECONCILE",
        )
        return self._operation_public(row)

    @staticmethod
    def _operation_public(row: sqlite3.Row | Mapping[str, Any], *, journal_id: str | None = None) -> dict[str, Any]:
        raw = dict(row)
        receipt = raw.get("receipt_json")
        try:
            raw["receipt"] = json.loads(receipt) if isinstance(receipt, str) else (receipt or {})
        except json.JSONDecodeError:
            raw["receipt"] = {}
        raw.pop("receipt_json", None)
        if journal_id:
            raw["intent_id"] = journal_id
        return raw

    @staticmethod
    def _operation_row(con: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
        row = con.execute("SELECT * FROM external_operations WHERE external_operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"external operation does not exist: {operation_id}")
        return row

    @staticmethod
    def _require_lease(con: sqlite3.Connection, lease: ExternalLease, now: str) -> None:
        row = con.execute(
            """SELECT 1 FROM external_record_leases
               WHERE lease_id=? AND external_record_mapping_id=? AND owner_id=?
                 AND fencing_token=? AND state='ACTIVE' AND lease_until>?""",
            (lease.lease_id, lease.mapping_id, lease.owner_id, lease.fencing_token, now),
        ).fetchone()
        if row is None:
            raise LeaseConflict("external record lease is stale or expired")

    @staticmethod
    def _upsert_binding(
        con: sqlite3.Connection,
        mapping_id: str,
        workflow_id: str,
        item_id: str | None,
        operation_id: str,
        relation_type: str,
        now: str,
    ) -> None:
        binding_key = f"external:{mapping_id}:{operation_id}:{relation_type.lower()}"
        existing = con.execute("SELECT binding_id FROM external_record_bindings WHERE binding_key=?", (binding_key,)).fetchone()
        if existing is not None:
            con.execute("UPDATE external_record_bindings SET last_touched_at=? WHERE binding_id=?", (now, existing["binding_id"]))
            return
        con.execute(
            """INSERT INTO external_record_bindings(
               binding_id, binding_key, external_record_mapping_id, workflow_id, item_id,
               external_operation_id, relation_type, first_touched_at, last_touched_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (new_id("external-binding"), binding_key, mapping_id, workflow_id, item_id, operation_id, relation_type, now, now),
        )

    @staticmethod
    def _require_text(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ExternalServiceError(f"{name} is required", code="VALIDATION_ERROR")
