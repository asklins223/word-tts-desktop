"""Restart/recovery scanner for stale leases, imports and side effects."""

from __future__ import annotations

from dataclasses import dataclass

from .domain import utc_now
from .side_effect_log import SideEffectIntentLog


@dataclass(frozen=True)
class RecoveryFinding:
    kind: str
    resource_id: str
    action: str
    side_effect_state: str | None = None


class RecoveryService:
    def __init__(self, database) -> None:
        self.database = database
        self.intent_log = SideEffectIntentLog(database.path.parent / "side_effect_intents.jsonl")

    def scan(self) -> list[RecoveryFinding]:
        now = utc_now()
        findings: list[RecoveryFinding] = []
        with self.database.read_transaction() as con:
            for row in con.execute(
                "SELECT lease_id, resource_id FROM workflow_leases WHERE state='ACTIVE' AND lease_until<=?",
                (now,),
            ):
                findings.append(RecoveryFinding("lease", str(row["lease_id"]), "EXPIRE"))
            for row in con.execute(
                """SELECT source_import_generation_id FROM source_import_generations
                    WHERE status='RECEIVING' AND expires_at<=?""",
                (now,),
            ):
                findings.append(RecoveryFinding("source_generation", str(row["source_import_generation_id"]), "EXPIRE"))
            for row in con.execute(
                """SELECT provider_submission_id, side_effect_state FROM provider_submissions
                    WHERE side_effect_state IN ('IN_FLIGHT','AMBIGUOUS')""",
            ):
                findings.append(RecoveryFinding("provider_submission", str(row["provider_submission_id"]), "RETRY", str(row["side_effect_state"])))
            for row in con.execute(
                """SELECT attempt_id FROM step_attempts
                    WHERE status IN ('RUNNING','PREPARING','VERIFYING')""",
            ):
                findings.append(RecoveryFinding("attempt", str(row["attempt_id"]), "RECOVER"))
            for row in con.execute(
                """SELECT intervention_id FROM user_interventions
                   WHERE state='OPEN' AND expires_at IS NOT NULL AND expires_at<=?""",
                (now,),
            ):
                findings.append(RecoveryFinding("intervention", str(row["intervention_id"]), "EXPIRE"))
            if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_operations'"
            ).fetchone() is not None:
                for row in con.execute(
                    """SELECT external_operation_id, side_effect_state
                       FROM external_operations
                       WHERE side_effect_state IN ('IN_FLIGHT','SUBMITTED','AMBIGUOUS')""",
                ):
                    findings.append(
                        RecoveryFinding(
                            "external_operation",
                            str(row["external_operation_id"]),
                            "RECONCILE",
                            str(row["side_effect_state"]),
                        )
                    )
        return findings

    def apply_safe_recovery(self) -> list[RecoveryFinding]:
        """Apply only local, unambiguous cleanup; never re-submit a Provider."""
        now = utc_now()
        findings: list[RecoveryFinding] = []
        tts_unresolved: list[tuple[str, str, str, str | None]] = []
        external_in_flight: list[tuple[str, str, str]] = []
        with self.database.read_transaction() as con:
            tts_unresolved = [
                (
                    str(row["provider_submission_id"]),
                    str(row["workflow_id"]),
                    str(row["work_unit_id"]),
                    str(row["operation_key"]) if row["operation_key"] is not None else None,
                )
                for row in con.execute(
                    """SELECT DISTINCT p.provider_submission_id, u.workflow_id,
                              u.work_unit_id, i.operation_key
                       FROM provider_submissions p
                       JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                       LEFT JOIN side_effect_intents i
                         ON i.workflow_id=u.workflow_id AND i.work_unit_id=u.work_unit_id
                        AND i.operation_namespace='tts'
                       WHERE p.side_effect_state IN ('IN_FLIGHT','AMBIGUOUS')
                         AND u.status <> 'SUCCEEDED'"""
                ).fetchall()
            ]
            if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_operations'"
            ).fetchone() is not None:
                external_in_flight = [
                    (
                        str(row["external_operation_id"]),
                        str(row["external_record_mapping_id"]),
                        str(row["external_operation_key"]),
                    )
                    for row in con.execute(
                        """SELECT external_operation_id, external_record_mapping_id,
                                  external_operation_key
                           FROM external_operations WHERE side_effect_state='IN_FLIGHT'"""
                    ).fetchall()
                ]
        # The file-side transition is written first.  If it cannot be fsync'd,
        # no SQLite state is changed; if SQLite then fails, the journal itself
        # exposes the mismatch and subsequent recovery remains fail-closed.
        journal_keys: set[tuple[str, str]] = set()
        for _submission_id, _workflow_id, _work_unit_id, operation_key in tts_unresolved:
            if operation_key:
                journal_keys.add(("tts", operation_key))
        for operation_id, mapping_id, operation_key in external_in_flight:
            journal_keys.add(("external", f"{mapping_id}:{operation_key}"))
        for namespace, operation_key in sorted(journal_keys):
            self.intent_log.mark(
                operation_namespace=namespace,
                operation_key=operation_key,
                state="ARCHIVED" if namespace == "tts" else "NEEDS_RECONCILE",
            )
        with self.database.transaction() as con:
            for row in con.execute(
                "SELECT lease_id, resource_id FROM workflow_leases WHERE state='ACTIVE' AND lease_until<=?",
                (now,),
            ).fetchall():
                con.execute(
                    "UPDATE workflow_leases SET state='EXPIRED', heartbeat_at=? WHERE lease_id=? AND state='ACTIVE'",
                    (now, row["lease_id"]),
                )
                findings.append(RecoveryFinding("lease", str(row["lease_id"]), "EXPIRE"))
            rows = con.execute(
                """SELECT * FROM source_import_generations
                    WHERE status='RECEIVING' AND expires_at<=?""",
                (now,),
            ).fetchall()
            for row in rows:
                con.execute(
                    """UPDATE source_import_generations SET status='EXPIRED', writer_lease_id=NULL,
                        error_code='SOURCE_NOT_AVAILABLE', completed_at=?, updated_at=?, state_version=state_version+1
                        WHERE source_import_generation_id=? AND status='RECEIVING'""",
                    (now, now, row["source_import_generation_id"]),
                )
                con.execute(
                    """UPDATE source_imports SET current_status='EXPIRED', error_code='SOURCE_NOT_AVAILABLE',
                        completed_at=?, updated_at=? WHERE source_import_id=? AND current_generation=?""",
                    (now, now, row["source_import_id"], row["generation"]),
                )
                findings.append(RecoveryFinding("source_generation", str(row["source_import_generation_id"]), "EXPIRE"))
            # A process can die after the local submission boundary and before
            # the result is usable by the renderer.  TTS has no provider-side
            # reconciliation path: the durable local attempt is rejected and
            # the next explicit generation creates a fresh execute attempt.
            rows = con.execute(
                """SELECT DISTINCT p.provider_submission_id, u.workflow_id,
                                  u.work_unit_id, u.step_id, u.created_by_attempt_id,
                                  u.created_at
                   FROM provider_submissions p
                   JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                   WHERE p.side_effect_state IN ('IN_FLIGHT','AMBIGUOUS')
                     AND u.status <> 'SUCCEEDED'
                   ORDER BY u.workflow_id, u.step_id, u.created_at DESC, u.work_unit_id DESC""",
            ).fetchall()
            self._normalize_tts_step_attempts_for_retry(con, rows, now)
            for row in rows:
                submission_id = str(row["provider_submission_id"])
                changed = False
                changed = bool(con.execute(
                    """UPDATE provider_submissions SET side_effect_state='REJECTED', state_version=state_version+1
                       WHERE provider_submission_id=? AND side_effect_state IN ('IN_FLIGHT','AMBIGUOUS')""",
                    (submission_id,),
                ).rowcount) or changed
                workflow_id = str(row["workflow_id"])
                work_unit_id = str(row["work_unit_id"])
                step_id = str(row["step_id"])
                changed = bool(con.execute(
                    """UPDATE work_units SET side_effect_state='REJECTED', status='WAITING_RETRY',
                              finished_at=NULL, state_version=state_version+1
                           WHERE workflow_id=? AND work_unit_id=? AND status <> 'SUCCEEDED'
                             AND side_effect_state IN ('IN_FLIGHT','AMBIGUOUS')""",
                    (workflow_id, work_unit_id),
                ).rowcount) or changed
                changed = bool(con.execute(
                    """UPDATE work_unit_items SET result_status='FAILED', state_version=state_version+1
                           WHERE workflow_id=? AND work_unit_id=?
                             AND result_status NOT IN ('SUCCEEDED','SKIPPED')""",
                    (workflow_id, work_unit_id),
                ).rowcount) or changed
                changed = bool(con.execute(
                    """UPDATE work_unit_segments SET result_status='FAILED'
                           WHERE work_unit_id=? AND result_status NOT IN ('SUCCEEDED','SKIPPED')""",
                    (work_unit_id,),
                ).rowcount) or changed
                changed = bool(con.execute(
                    """UPDATE work_items SET status='FAILED', state_version=state_version+1, updated_at=?
                           WHERE workflow_id=? AND item_id IN (
                               SELECT item_id FROM work_unit_items
                                WHERE workflow_id=? AND work_unit_id=?
                           ) AND status NOT IN ('SUCCEEDED','SKIPPED')""",
                    (now, workflow_id, workflow_id, work_unit_id),
                ).rowcount) or changed
                changed = bool(con.execute(
                    """UPDATE work_unit_attempts SET status='WAITING_RETRY', side_effect_state='REJECTED',
                              finished_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND work_unit_id=?
                             AND status IN ('CREATED','PREPARING','RUNNING','VERIFYING',
                                            'WAITING_USER','BLOCKED','RECOVERING','AMBIGUOUS')""",
                    (now, workflow_id, work_unit_id),
                ).rowcount) or changed
                changed = bool(con.execute(
                    """UPDATE side_effect_intents SET state='ARCHIVED', updated_at=?
                           WHERE workflow_id=? AND work_unit_id=? AND operation_namespace='tts'
                             AND state <> 'ARCHIVED'""",
                    (now, workflow_id, work_unit_id),
                ).rowcount) or changed
                changed = bool(con.execute(
                    """UPDATE workflow_steps SET status='WAITING_RETRY',
                              error_code='LOCAL_SUBMISSION_NOT_CONFIRMED', retry_after=NULL,
                              state_version=state_version+1
                           WHERE workflow_id=? AND step_id=?
                             AND status NOT IN ('SUCCEEDED','PERMANENT_FAILED','CANCELLED')""",
                    (workflow_id, step_id),
                ).rowcount) or changed
                changed = bool(con.execute(
                    """UPDATE workflows SET execution_state='WAITING_RETRY', result_status='IN_PROGRESS',
                              last_error_code='LOCAL_SUBMISSION_NOT_CONFIRMED',
                              last_error_message='本地没有可下载结果，请重新生成',
                              state_version=state_version+1, updated_at=?
                           WHERE workflow_id=? AND control_state='RUNNING'
                             AND execution_state <> 'TERMINAL'""",
                    (now, workflow_id),
                ).rowcount) or changed
                changed = bool(con.execute(
                    """UPDATE user_interventions SET state='RESOLVED', resolved_by='recovery-service',
                              resolved_at=?, updated_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND work_unit_id=?
                             AND intervention_type='RECONCILE_PROVIDER'
                             AND state IN ('OPEN','CLAIMED')""",
                    (now, now, workflow_id, work_unit_id),
                ).rowcount) or changed
                if changed:
                    self._append_local_retry_event(con, workflow_id, submission_id, work_unit_id)
                    findings.append(RecoveryFinding("provider_submission", submission_id, "RETRY", "REJECTED"))

            for operation_id, mapping_id, operation_key in external_in_flight:
                operation = con.execute(
                    """SELECT * FROM external_operations
                       WHERE external_operation_id=? AND side_effect_state='IN_FLIGHT'""",
                    (operation_id,),
                ).fetchone()
                if operation is None:
                    continue
                updated = con.execute(
                    """UPDATE external_operations
                       SET side_effect_state='AMBIGUOUS', state_version=state_version+1
                       WHERE external_operation_id=? AND side_effect_state='IN_FLIGHT'""",
                    (operation_id,),
                )
                if updated.rowcount != 1:
                    continue
                con.execute(
                    """UPDATE external_records
                       SET external_status='AMBIGUOUS', last_error='EXTERNAL_SUBMISSION_AMBIGUOUS', updated_at=?
                       WHERE external_record_mapping_id=?""",
                    (now, mapping_id),
                )
                con.execute(
                    """UPDATE side_effect_intents SET state='NEEDS_RECONCILE', updated_at=?
                       WHERE workflow_id=? AND operation_namespace='external' AND operation_key=?""",
                    (now, operation["workflow_id"], f"{mapping_id}:{operation_key}"),
                )
                intervention_id = f"intervention_external_{operation_id}"
                if con.execute(
                    "SELECT 1 FROM user_interventions WHERE intervention_id=?",
                    (intervention_id,),
                ).fetchone() is None:
                    con.execute(
                        """INSERT INTO user_interventions(
                           intervention_id, workflow_id, step_id, attempt_id, work_unit_id,
                           intervention_type, reason, owner_id, state, evidence_json,
                           expires_at, resolved_by, resolved_at, state_version, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (intervention_id, operation["workflow_id"], None, None, None,
                         "RECONCILE_EXTERNAL", "external response was not observed after IN_FLIGHT commit",
                         None, "OPEN", "{}", None, None, None, 0, now, now),
                    )
                self._append_external_recovery_event(con, str(operation["workflow_id"]), operation_id)
                findings.append(RecoveryFinding("external_operation", operation_id, "RECONCILE", "AMBIGUOUS"))
            for row in con.execute(
                """SELECT intervention_id, workflow_id, state_version FROM user_interventions
                   WHERE state IN ('OPEN','CLAIMED') AND expires_at IS NOT NULL AND expires_at<=?""",
                (now,),
            ).fetchall():
                updated = con.execute(
                    """UPDATE user_interventions SET state='EXPIRED', state_version=state_version+1,
                       updated_at=? WHERE intervention_id=? AND state_version=?""",
                    (now, row["intervention_id"], row["state_version"]),
                )
                if updated.rowcount:
                    self._append_intervention_expired(con, str(row["workflow_id"]), str(row["intervention_id"]))
                    findings.append(RecoveryFinding("intervention", str(row["intervention_id"]), "EXPIRE"))
        return findings

    def _normalize_tts_step_attempts_for_retry(self, con, rows, now: str) -> None:
        """Leave one retryable step attempt when legacy rows disagree.

        ``ux_active_step_attempt`` intentionally permits only one active attempt
        for a workflow step.  Older recovery passes could leave an old
        ``WAITING_USER`` attempt beside an ``AMBIGUOUS`` provider attempt; the
        latter then failed while being promoted to ``WAITING_RETRY``.  Resolve
        that projection conflict locally before applying the provider cleanup.
        The newest unresolved unit remains the canonical retry target, while
        other active attempts are retained as terminal audit records.
        """

        candidate_attempts: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            step_key = (str(row["workflow_id"]), str(row["step_id"]))
            candidate_attempts.setdefault(step_key, [])
            attempt_id = str(row["created_by_attempt_id"] or "") or None
            if attempt_id and attempt_id not in candidate_attempts[step_key]:
                candidate_attempts[step_key].append(attempt_id)

        active_statuses = (
            "'PREPARING', 'RUNNING', 'WAITING_RETRY', 'WAITING_USER',"
            " 'RECOVERING', 'BLOCKED'"
        )
        retryable_statuses = (
            "'CREATED', 'PREPARING', 'RUNNING', 'VERIFYING',"
            " 'WAITING_RETRY', 'WAITING_USER', 'RECOVERING', 'BLOCKED', 'AMBIGUOUS'"
        )
        terminal_statuses = {"SUCCEEDED", "FAILED", "CANCELLED"}

        for (workflow_id, step_id), attempt_ids in candidate_attempts.items():
            canonical_attempt_id = None
            for attempt_id in attempt_ids:
                attempt = con.execute(
                    """SELECT status FROM step_attempts
                       WHERE workflow_id=? AND step_id=? AND attempt_id=?""",
                    (workflow_id, step_id, attempt_id),
                ).fetchone()
                if attempt is not None and str(attempt["status"]) not in terminal_statuses:
                    canonical_attempt_id = attempt_id
                    break

            if canonical_attempt_id:
                con.execute(
                    f"""UPDATE step_attempts SET status='FAILED', result_status='FAILED',
                              error_code='RECOVERY_SUPERSEDED',
                              error_details_json='{{\"error_code\":\"RECOVERY_SUPERSEDED\"}}',
                              finished_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND step_id=? AND attempt_id<>?
                             AND status IN ({active_statuses})""",
                    (now, workflow_id, step_id, canonical_attempt_id),
                )
                con.execute(
                    f"""UPDATE step_attempts SET status='WAITING_RETRY', result_status='FAILED',
                              error_code='LOCAL_SUBMISSION_NOT_CONFIRMED',
                              error_details_json='{{\"error_code\":\"LOCAL_SUBMISSION_NOT_CONFIRMED\"}}',
                              finished_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND step_id=? AND attempt_id=?
                             AND status IN ({retryable_statuses})""",
                    (now, workflow_id, step_id, canonical_attempt_id),
                )
            else:
                con.execute(
                    f"""UPDATE step_attempts SET status='FAILED', result_status='FAILED',
                              error_code='RECOVERY_SUPERSEDED',
                              error_details_json='{{\"error_code\":\"RECOVERY_SUPERSEDED\"}}',
                              finished_at=?, state_version=state_version+1
                           WHERE workflow_id=? AND step_id=?
                             AND status IN ({active_statuses})""",
                    (now, workflow_id, step_id),
                )

            con.execute(
                """UPDATE workflow_steps SET current_attempt_id=?, state_version=state_version+1
                   WHERE workflow_id=? AND step_id=? AND current_attempt_id IS NOT ?""",
                (canonical_attempt_id, workflow_id, step_id, canonical_attempt_id),
            )

    def _append_local_retry_event(
        self, con, workflow_id: str, submission_id: str, work_unit_id: str
    ) -> None:
        from .event_store import EventStore
        from .repositories import _snapshot_from_connection

        events = EventStore(self.database)
        events.append_in_transaction(
            con,
            workflow_id,
            "RECOVERY_LOCAL_RETRYABLE",
            {
                "submission_id": submission_id,
                "work_unit_id": work_unit_id,
                "reason": "本地没有可下载结果，下一次显式生成将创建新的执行尝试",
            },
            actor_type="RECOVERY",
            actor_id="recovery-service",
        )
        snapshot = _snapshot_from_connection(con, workflow_id)
        events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())

    def _append_intervention_expired(self, con, workflow_id: str, intervention_id: str) -> None:
        from .event_store import EventStore
        from .repositories import _snapshot_from_connection

        events = EventStore(self.database)
        events.append_in_transaction(
            con, workflow_id, "INTERVENTION_EXPIRED", {"intervention_id": intervention_id},
            actor_type="RECOVERY", actor_id="recovery-service",
        )
        snapshot = _snapshot_from_connection(con, workflow_id)
        events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())

    def _append_external_recovery_event(self, con, workflow_id: str, operation_id: str) -> None:
        from .event_store import EventStore
        from .repositories import _snapshot_from_connection

        events = EventStore(self.database)
        events.append_in_transaction(
            con,
            workflow_id,
            "RECOVERY_REQUIRES_EXTERNAL_RECONCILE",
            {"external_operation_id": operation_id},
            actor_type="RECOVERY",
            actor_id="recovery-service",
        )
        snapshot = _snapshot_from_connection(con, workflow_id)
        events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
