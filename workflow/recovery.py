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
                findings.append(RecoveryFinding("provider_submission", str(row["provider_submission_id"]), "RECONCILE", str(row["side_effect_state"])))
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
        tts_in_flight: list[tuple[str, str, str, str]] = []
        external_in_flight: list[tuple[str, str, str]] = []
        with self.database.read_transaction() as con:
            tts_in_flight = [
                (
                    str(row["provider_submission_id"]),
                    str(row["workflow_id"]),
                    str(row["work_unit_id"]),
                    str(row["operation_key"]),
                )
                for row in con.execute(
                    """SELECT DISTINCT p.provider_submission_id, u.workflow_id,
                              u.work_unit_id, i.operation_key
                       FROM provider_submissions p
                       JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                       JOIN side_effect_intents i
                         ON i.workflow_id=u.workflow_id AND i.work_unit_id=u.work_unit_id
                        AND i.operation_namespace='tts'
                       WHERE p.side_effect_state='IN_FLIGHT'"""
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
        for _submission_id, _workflow_id, _work_unit_id, operation_key in tts_in_flight:
            journal_keys.add(("tts", operation_key))
        for operation_id, mapping_id, operation_key in external_in_flight:
            journal_keys.add(("external", f"{mapping_id}:{operation_key}"))
        for namespace, operation_key in sorted(journal_keys):
            self.intent_log.mark(
                operation_namespace=namespace,
                operation_key=operation_key,
                state="NEEDS_RECONCILE",
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
            # A process can die after the local IN_FLIGHT commit and before a
            # provider response.  Marking the submission AMBIGUOUS is the
            # only safe local action; no recovery path submits again.
            rows = con.execute(
                """SELECT p.provider_submission_id, p.workflow_group_id
                   FROM provider_submissions p WHERE p.side_effect_state='IN_FLIGHT'""",
            ).fetchall()
            for row in rows:
                submission_id = str(row["provider_submission_id"])
                con.execute(
                    """UPDATE provider_submissions SET side_effect_state='AMBIGUOUS', state_version=state_version+1
                       WHERE provider_submission_id=? AND side_effect_state='IN_FLIGHT'""",
                    (submission_id,),
                )
                units = con.execute(
                    """SELECT u.workflow_id, u.work_unit_id, u.step_id,
                              u.created_by_attempt_id
                       FROM work_units u WHERE u.provider_submission_id=?""",
                    (submission_id,),
                ).fetchall()
                for unit in units:
                    con.execute(
                        """UPDATE work_units SET side_effect_state='AMBIGUOUS', status='AMBIGUOUS',
                           state_version=state_version+1 WHERE work_unit_id=? AND side_effect_state='IN_FLIGHT'""",
                        (unit["work_unit_id"],),
                    )
                    con.execute(
                        """UPDATE step_attempts SET status='AMBIGUOUS', result_status='MIXED',
                           error_code='SUBMISSION_AMBIGUOUS', state_version=state_version+1
                           WHERE workflow_id=? AND attempt_id=? AND status IN ('RUNNING','PREPARING','VERIFYING')""",
                        (unit["workflow_id"], unit["created_by_attempt_id"]),
                    )
                    con.execute(
                        """UPDATE work_unit_attempts SET status='AMBIGUOUS', side_effect_state='AMBIGUOUS',
                           state_version=state_version+1
                           WHERE workflow_id=? AND work_unit_id=? AND status IN ('RUNNING','PREPARING','VERIFYING')""",
                        (unit["workflow_id"], unit["work_unit_id"]),
                    )
                    con.execute(
                        """UPDATE side_effect_intents SET state='NEEDS_RECONCILE', updated_at=?
                           WHERE workflow_id=? AND work_unit_id=? AND operation_namespace='tts'""",
                        (now, unit["workflow_id"], unit["work_unit_id"]),
                    )
                    con.execute(
                        """UPDATE workflow_steps SET status='AMBIGUOUS', error_code='SUBMISSION_AMBIGUOUS',
                           state_version=state_version+1 WHERE workflow_id=? AND step_id=?
                           AND status IN ('PREPARING','RUNNING','VERIFYING')""",
                        (unit["workflow_id"], unit["step_id"]),
                    )
                    con.execute(
                        """UPDATE workflows SET execution_state='WAITING_USER', last_error_code='SUBMISSION_AMBIGUOUS',
                           last_error_message='provider submission requires reconciliation',
                           state_version=state_version+1, updated_at=?
                           WHERE workflow_id=? AND execution_state IN ('PREPARING','RUNNING','RECOVERING')""",
                        (now, unit["workflow_id"]),
                    )
                    if con.execute(
                        """SELECT 1 FROM user_interventions
                           WHERE workflow_id=? AND work_unit_id=? AND state IN ('OPEN','CLAIMED') LIMIT 1""",
                        (unit["workflow_id"], unit["work_unit_id"]),
                    ).fetchone() is None:
                        con.execute(
                            """INSERT INTO user_interventions(
                                intervention_id, workflow_id, step_id, attempt_id, work_unit_id,
                                intervention_type, reason, owner_id, state, evidence_json,
                                expires_at, resolved_by, resolved_at, state_version, created_at, updated_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (f"intervention_{submission_id}_{unit['work_unit_id']}", unit["workflow_id"], unit["step_id"],
                             unit["created_by_attempt_id"], unit["work_unit_id"], "RECONCILE_PROVIDER",
                             "provider response was not observed after IN_FLIGHT commit", None, "OPEN", "{}",
                             None, None, None, 0, now, now),
                        )
                    self._append_recovery_event(con, str(unit["workflow_id"]), submission_id, str(unit["work_unit_id"]))
                    findings.append(RecoveryFinding("provider_submission", submission_id, "RECONCILE", "AMBIGUOUS"))
            # Older desktop builds could persist the provider/unit/attempt
            # projections as AMBIGUOUS and then exit during cleanup before the
            # parent workflow was moved out of RUNNING.  That combination is
            # particularly harmful: the renderer presents a live task while a
            # later retry opens a browser only to perform read-only works-name
            # reconciliation.  Repair the local projection on startup; this is
            # fail-closed and never submits to the provider.
            self._repair_ambiguous_workflow_projections(con, now, findings)

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

    def _repair_ambiguous_workflow_projections(self, con, now, findings) -> None:
        """Repair legacy ``RUNNING + AMBIGUOUS`` workflow projections.

        A provider side effect that is already AMBIGUOUS is not safe to retry.
        The only valid local action is to surface it as WAITING_USER and make
        every child projection agree.  The transition is limited to non-final
        rows so a verified artifact is never demoted.
        """

        rows = con.execute(
            """SELECT DISTINCT w.workflow_id, u.workflow_group_id,
                              u.work_unit_id, u.step_id, u.created_by_attempt_id,
                              u.provider_submission_id,
                              u.side_effect_state AS unit_side_effect_state,
                              p.side_effect_state AS submission_side_effect_state
                       FROM workflows w
                       JOIN work_units u ON u.workflow_id=w.workflow_id
                       LEFT JOIN provider_submissions p
                         ON p.provider_submission_id=u.provider_submission_id
                        AND p.workflow_group_id=u.workflow_group_id
                       WHERE w.execution_state IN ('PREPARING','RUNNING','RECOVERING')
                         AND u.status <> 'SUCCEEDED'
                         AND (u.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                              OR p.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS'))""",
        ).fetchall()

        for row in rows:
            workflow_id = str(row["workflow_id"])
            work_unit_id = str(row["work_unit_id"])
            step_id = str(row["step_id"])
            attempt_id = str(row["created_by_attempt_id"] or "") or None
            submission_id = str(row["provider_submission_id"] or "") or None
            changed = False

            if submission_id and row["submission_side_effect_state"] in {
                "IN_FLIGHT", "SUBMITTED", "CONFIRMED"
            }:
                changed = bool(con.execute(
                    """UPDATE provider_submissions
                          SET side_effect_state='AMBIGUOUS', state_version=state_version+1
                        WHERE provider_submission_id=? AND workflow_group_id=?
                          AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED')""",
                    (submission_id, row["workflow_group_id"]),
                ).rowcount) or changed

            changed = bool(con.execute(
                """UPDATE work_units
                      SET side_effect_state='AMBIGUOUS', status='AMBIGUOUS',
                          state_version=state_version+1
                    WHERE workflow_id=? AND work_unit_id=?
                      AND status <> 'SUCCEEDED'
                      AND side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED')""",
                (workflow_id, work_unit_id),
            ).rowcount) or changed

            changed = bool(con.execute(
                """UPDATE work_unit_items
                      SET result_status='AMBIGUOUS', state_version=state_version+1
                    WHERE workflow_id=? AND work_unit_id=?
                      AND result_status NOT IN ('SUCCEEDED','SKIPPED','AMBIGUOUS')""",
                (workflow_id, work_unit_id),
            ).rowcount) or changed
            changed = bool(con.execute(
                """UPDATE work_unit_segments
                      SET result_status='AMBIGUOUS'
                    WHERE work_unit_id=? AND result_status NOT IN ('SUCCEEDED','SKIPPED','AMBIGUOUS')""",
                (work_unit_id,),
            ).rowcount) or changed
            changed = bool(con.execute(
                """UPDATE work_items
                      SET status='AMBIGUOUS', state_version=state_version+1, updated_at=?
                    WHERE workflow_id=? AND item_id IN (
                        SELECT item_id FROM work_unit_items
                         WHERE workflow_id=? AND work_unit_id=?
                    ) AND status NOT IN ('SUCCEEDED','SKIPPED','AMBIGUOUS')""",
                (now, workflow_id, workflow_id, work_unit_id),
            ).rowcount) or changed

            if attempt_id:
                changed = bool(con.execute(
                    """UPDATE step_attempts
                          SET status='AMBIGUOUS', result_status='MIXED',
                              error_code='SUBMISSION_AMBIGUOUS',
                              state_version=state_version+1
                        WHERE workflow_id=? AND attempt_id=?
                          AND status IN ('CREATED','PREPARING','RUNNING','VERIFYING',
                                         'WAITING_RETRY','WAITING_USER','RECOVERING')""",
                    (workflow_id, attempt_id),
                ).rowcount) or changed
            changed = bool(con.execute(
                """UPDATE work_unit_attempts
                      SET status='AMBIGUOUS', side_effect_state='AMBIGUOUS',
                          state_version=state_version+1
                    WHERE workflow_id=? AND work_unit_id=?
                      AND status IN ('CREATED','PREPARING','RUNNING','VERIFYING',
                                     'WAITING_RETRY','WAITING_USER','RECOVERING')""",
                (workflow_id, work_unit_id),
            ).rowcount) or changed
            changed = bool(con.execute(
                """UPDATE workflow_steps
                      SET status='AMBIGUOUS', error_code='SUBMISSION_AMBIGUOUS',
                          retry_after=NULL, state_version=state_version+1
                    WHERE workflow_id=? AND step_id=?
                      AND status IN ('PENDING','READY','PREPARING','RUNNING','VERIFYING',
                                     'WAITING_RETRY','RETRYABLE_FAILED','WAITING_USER','BLOCKED')""",
                (workflow_id, step_id),
            ).rowcount) or changed

            intent = con.execute(
                """SELECT state FROM side_effect_intents
                    WHERE workflow_id=? AND work_unit_id=?
                      AND operation_namespace='tts' LIMIT 1""",
                (workflow_id, work_unit_id),
            ).fetchone()
            if intent is not None and str(intent["state"]) != "NEEDS_RECONCILE":
                con.execute(
                    """UPDATE side_effect_intents
                          SET state='NEEDS_RECONCILE', updated_at=?
                        WHERE workflow_id=? AND work_unit_id=?
                          AND operation_namespace='tts'""",
                    (now, workflow_id, work_unit_id),
                )
                changed = True

            updated_workflow = con.execute(
                """UPDATE workflows
                      SET execution_state='WAITING_USER',
                          last_error_code='SUBMISSION_AMBIGUOUS',
                          last_error_message='provider submission requires reconciliation',
                          state_version=state_version+1, updated_at=?
                    WHERE workflow_id=?
                      AND execution_state IN ('PREPARING','RUNNING','RECOVERING')""",
                (now, workflow_id),
            )
            changed = bool(updated_workflow.rowcount) or changed

            if not changed:
                continue

            open_intervention = con.execute(
                """SELECT 1 FROM user_interventions
                    WHERE workflow_id=? AND work_unit_id=?
                      AND state IN ('OPEN','CLAIMED') LIMIT 1""",
                (workflow_id, work_unit_id),
            ).fetchone()
            if open_intervention is None:
                intervention_id = f"intervention_repair_{work_unit_id}"
                existing = con.execute(
                    "SELECT 1 FROM user_interventions WHERE intervention_id=?",
                    (intervention_id,),
                ).fetchone()
                if existing is None:
                    con.execute(
                        """INSERT INTO user_interventions(
                            intervention_id, workflow_id, step_id, attempt_id, work_unit_id,
                            intervention_type, reason, owner_id, state, evidence_json,
                            expires_at, resolved_by, resolved_at, state_version, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (intervention_id, workflow_id, step_id, attempt_id, work_unit_id,
                         "RECONCILE_PROVIDER",
                         "legacy workflow projection repaired after ambiguous provider submission",
                         None, "OPEN", "{}", None, None, None, 0, now, now),
                    )

            self._append_projection_repaired_event(
                con, workflow_id, submission_id, work_unit_id,
            )
            findings.append(
                RecoveryFinding("workflow_projection", workflow_id, "REPAIR", "AMBIGUOUS")
            )

    def _append_recovery_event(self, con, workflow_id: str, submission_id: str, work_unit_id: str) -> None:
        from .event_store import EventStore
        from .repositories import _snapshot_from_connection

        events = EventStore(self.database)
        events.append_in_transaction(
            con, workflow_id, "RECOVERY_REQUIRES_RECONCILE",
            {"submission_id": submission_id, "work_unit_id": work_unit_id},
            actor_type="RECOVERY", actor_id="recovery-service",
        )
        snapshot = _snapshot_from_connection(con, workflow_id)
        events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())

    def _append_projection_repaired_event(
        self, con, workflow_id: str, submission_id: str | None, work_unit_id: str
    ) -> None:
        from .event_store import EventStore
        from .repositories import _snapshot_from_connection

        events = EventStore(self.database)
        events.append_in_transaction(
            con,
            workflow_id,
            "RECOVERY_PROJECTION_REPAIRED",
            {
                "submission_id": submission_id,
                "work_unit_id": work_unit_id,
                "reason": "legacy ambiguous provider state was made visible as WAITING_USER",
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
