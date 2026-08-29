"""Persistent, bounded scheduler claims for retries and interventions.

There is intentionally no in-memory timer here.  A host may poll these
methods after restart; every claim is a short SQLite transaction guarded by
the target's state version.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import utc_now
from .event_store import EventStore
from .retry_policy import RetryPolicy


@dataclass(frozen=True)
class SchedulerClaim:
    kind: str
    workflow_id: str
    resource_id: str
    state_version: int
    error_code: str | None = None


class PersistentScheduler:
    def __init__(
        self,
        database,
        *,
        event_store: EventStore | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.database = database
        self.events = event_store or EventStore(database)
        self.retry_policy = retry_policy or RetryPolicy()

    def hold_exhausted_retries(self, *, limit: int = 32) -> int:
        """Move safe retries over the automatic budget to user intervention.

        Without this terminal guard an immediate transient failure would be
        claimed again on every scheduler tick forever.  The user can still
        inspect the durable attempt history and explicitly retry if desired.
        """

        limit = max(1, min(int(limit), 256))
        retryable = sorted(RetryPolicy.RETRYABLE)
        placeholders = ",".join("?" for _ in retryable)
        with self.database.transaction() as con:
            rows = con.execute(
                f"""SELECT s.workflow_id, s.step_id, s.state_version,
                           COALESCE((SELECT MAX(a.execute_attempt_no)
                                     FROM step_attempts a
                                     WHERE a.workflow_id=s.workflow_id
                                       AND a.step_id=s.step_id
                                       AND a.attempt_kind='EXECUTE'), 0) AS attempts
                    FROM workflow_steps s
                    JOIN workflows w ON w.workflow_id=s.workflow_id
                    WHERE s.status='WAITING_RETRY'
                      AND w.execution_state='WAITING_RETRY'
                      AND w.control_state='RUNNING'
                      AND s.error_code IN ({placeholders})
                      AND COALESCE((SELECT MAX(a.execute_attempt_no)
                                    FROM step_attempts a
                                    WHERE a.workflow_id=s.workflow_id
                                      AND a.step_id=s.step_id
                                      AND a.attempt_kind='EXECUTE'), 0) >= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM work_units u
                          WHERE u.workflow_id=s.workflow_id
                            AND u.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS')
                            AND u.status<>'SUCCEEDED'
                      )
                    ORDER BY s.step_id LIMIT ?""",
                (*retryable, self.retry_policy.max_attempts, limit),
            ).fetchall()
            held: list[tuple[str, str, int]] = []
            for row in rows:
                updated = con.execute(
                    """UPDATE workflow_steps SET status='WAITING_USER', retry_after=NULL,
                              state_version=state_version+1
                       WHERE workflow_id=? AND step_id=? AND state_version=?
                         AND status='WAITING_RETRY'""",
                    (row["workflow_id"], row["step_id"], row["state_version"]),
                )
                if updated.rowcount == 1:
                    held.append((str(row["workflow_id"]), str(row["step_id"]), int(row["attempts"])))
            if not held:
                return 0
            workflow_ids = sorted({workflow_id for workflow_id, _step_id, _attempts in held})
            now = utc_now()
            for workflow_id in workflow_ids:
                con.execute(
                    """UPDATE workflows SET execution_state='WAITING_USER',
                              state_version=state_version+1, updated_at=?
                       WHERE workflow_id=? AND execution_state='WAITING_RETRY'""",
                    (now, workflow_id),
                )
            from .repositories import _snapshot_from_connection

            for workflow_id, step_id, attempts in held:
                self.events.append_in_transaction(
                    con,
                    workflow_id,
                    "RETRY_BUDGET_EXHAUSTED",
                    {"step_id": step_id, "attempts": attempts, "max_attempts": self.retry_policy.max_attempts},
                    actor_type="SCHEDULER",
                    actor_id="provider-aware-scheduler",
                    step_id=step_id,
                )
                snapshot = _snapshot_from_connection(con, workflow_id)
                self.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
            return len(held)

    def claim_due_retries(
        self,
        *,
        now: str | None = None,
        limit: int = 32,
        safe_only: bool = False,
    ) -> list[SchedulerClaim]:
        now = now or utc_now()
        limit = max(1, min(int(limit), 256))
        claims: list[SchedulerClaim] = []
        with self.database.transaction() as con:
            predicates = [
                "s.status='WAITING_RETRY'",
                "(s.retry_after IS NULL OR s.retry_after<=?)",
            ]
            params: list[object] = [now]
            if safe_only:
                # Automatic dispatch is deliberately narrower than the
                # user-facing retry command.  Only a run whose control plane
                # is still RUNNING, whose step carries a policy-approved
                # transient error, and whose provider boundary has been
                # rejected may be claimed.  Ambiguous/in-flight facts never
                # enter this path.
                retryable = sorted(RetryPolicy.RETRYABLE)
                placeholders = ",".join("?" for _ in retryable)
                predicates.extend([
                    "w.execution_state='WAITING_RETRY'",
                    "w.control_state='RUNNING'",
                    f"s.error_code IN ({placeholders})",
                    "COALESCE((SELECT MAX(a.execute_attempt_no) FROM step_attempts a WHERE a.workflow_id=s.workflow_id AND a.step_id=s.step_id AND a.attempt_kind='EXECUTE'), 0) < ?",
                    "NOT EXISTS (SELECT 1 FROM work_units u WHERE u.workflow_id=s.workflow_id AND u.side_effect_state IN ('IN_FLIGHT','SUBMITTED','CONFIRMED','AMBIGUOUS') AND u.status<>'SUCCEEDED')",
                    # Unattended dispatch must never re-open the browser for a
                    # run the user did not accept for generation.  A takeover
                    # that failed before any user Generate command leaves a TTS
                    # step behind, so the consent marker has to be the durable
                    # WORKFLOW_GENERATE event, not step/attempt evidence.
                    """EXISTS (
                        SELECT 1 FROM workflow_events ge
                        WHERE ge.workflow_id=s.workflow_id
                          AND ge.event_type='WORKFLOW_GENERATE'
                    )""",
                ])
                params.extend([*retryable, self.retry_policy.max_attempts])
            params.append(limit)
            rows = con.execute(
                f"""SELECT s.workflow_id, s.step_id, s.state_version, s.error_code
                   FROM workflow_steps s
                   JOIN workflows w ON w.workflow_id=s.workflow_id
                   WHERE {' AND '.join(predicates)}
                   ORDER BY COALESCE(s.retry_after, ''), s.step_id LIMIT ?""",
                tuple(params),
            ).fetchall()
            for row in rows:
                updated = con.execute(
                    """UPDATE workflow_steps SET status='READY', retry_after=NULL, error_code=NULL,
                       state_version=state_version+1
                       WHERE workflow_id=? AND step_id=? AND state_version=? AND status='WAITING_RETRY'""",
                    (row["workflow_id"], row["step_id"], row["state_version"]),
                )
                if updated.rowcount != 1:
                    continue
                self.events.append_in_transaction(
                    con, str(row["workflow_id"]), "RETRY_CLAIMED",
                    {"step_id": str(row["step_id"]), "previous_state_version": int(row["state_version"])},
                    actor_type="SCHEDULER", actor_id="persistent-scheduler", step_id=str(row["step_id"]),
                )
                from .repositories import _snapshot_from_connection

                snapshot = _snapshot_from_connection(con, str(row["workflow_id"]))
                self.events.write_snapshot_in_transaction(con, str(row["workflow_id"]), snapshot.as_dict())
                claims.append(SchedulerClaim(
                    "retry", str(row["workflow_id"]), str(row["step_id"]),
                    int(row["state_version"]) + 1, str(row["error_code"] or "") or None,
                ))
        return claims

    def defer_retry_claim(
        self,
        claim: SchedulerClaim,
        *,
        retry_after: str,
        error_code: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Return a claimed retry to durable WAITING_RETRY state.

        Provider capability can change after a worker claims a row (for
        example, the formal app may be running offline).  Losing the claim
        would strand the workflow in READY, so capability/queue deferrals use
        the same optimistic version fence as the claim itself.
        """

        now = utc_now()
        with self.database.transaction() as con:
            updated = con.execute(
                """UPDATE workflow_steps SET status='WAITING_RETRY', retry_after=?,
                          error_code=COALESCE(?, error_code), state_version=state_version+1
                   WHERE workflow_id=? AND step_id=? AND state_version=? AND status='READY'""",
                (retry_after, error_code, claim.workflow_id, claim.resource_id, claim.state_version),
            )
            if updated.rowcount != 1:
                return False
            self.events.append_in_transaction(
                con,
                claim.workflow_id,
                "RETRY_DEFERRED",
                {
                    "step_id": claim.resource_id,
                    "retry_after": retry_after,
                    "error_code": error_code or claim.error_code,
                    "reason": reason,
                },
                actor_type="SCHEDULER",
                actor_id="provider-aware-scheduler",
                step_id=claim.resource_id,
            )
            from .repositories import _snapshot_from_connection

            snapshot = _snapshot_from_connection(con, claim.workflow_id)
            self.events.write_snapshot_in_transaction(con, claim.workflow_id, snapshot.as_dict())
            return True

    def expire_interventions(self, *, now: str | None = None, limit: int = 32) -> list[SchedulerClaim]:
        now = now or utc_now()
        limit = max(1, min(int(limit), 256))
        claims: list[SchedulerClaim] = []
        with self.database.transaction() as con:
            rows = con.execute(
                """SELECT intervention_id, workflow_id, state_version FROM user_interventions
                   WHERE state IN ('OPEN','CLAIMED') AND expires_at IS NOT NULL AND expires_at<=?
                   ORDER BY expires_at, intervention_id LIMIT ?""",
                (now, limit),
            ).fetchall()
            for row in rows:
                updated = con.execute(
                    """UPDATE user_interventions SET state='EXPIRED', state_version=state_version+1,
                       updated_at=? WHERE intervention_id=? AND state_version=? AND state IN ('OPEN','CLAIMED')""",
                    (now, row["intervention_id"], row["state_version"]),
                )
                if updated.rowcount != 1:
                    continue
                self.events.append_in_transaction(
                    con, str(row["workflow_id"]), "INTERVENTION_EXPIRED",
                    {"intervention_id": str(row["intervention_id"])},
                    actor_type="SCHEDULER", actor_id="persistent-scheduler",
                )
                claims.append(SchedulerClaim("intervention", str(row["workflow_id"]), str(row["intervention_id"]), int(row["state_version"]) + 1))
        return claims
