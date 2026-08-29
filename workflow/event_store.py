"""Durable workflow event log, snapshots, cursors and SSE serialization."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from .database import WorkflowDatabase
from .data_safety import redact_public_json
from .domain import WorkflowEvent, canonical_json, new_id, utc_now


class EventStoreError(RuntimeError):
    pass


class CursorExpired(EventStoreError):
    code = "CURSOR_EXPIRED"


class InvalidCursor(EventStoreError):
    code = "CURSOR_INVALID"


def _event_from_row(row: sqlite3.Row) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=str(row["event_id"]),
        seq=int(row["seq"]),
        workflow_id=str(row["workflow_id"]),
        mutation_id=str(row["mutation_id"]),
        schema_version=str(row["schema_version"]),
        step_id=row["step_id"],
        item_id=row["item_id"],
        attempt_id=row["attempt_id"],
        correlation_id=str(row["correlation_id"] or ""),
        causation_id=row["causation_id"],
        actor_type=str(row["actor_type"]),
        actor_id=row["actor_id"],
        event_type=str(row["event_type"]),
        phase=row["phase"],
        payload=json.loads(str(row["payload_json"])),
        created_at=str(row["created_at"]),
    )


class EventStore:
    def __init__(self, database: WorkflowDatabase, *, schema_version: str = "1") -> None:
        self.database = database
        self.schema_version = schema_version

    def append(
        self,
        workflow_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        mutation_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        actor_type: str = "SYSTEM",
        actor_id: str | None = None,
        step_id: str | None = None,
        item_id: str | None = None,
        attempt_id: str | None = None,
        phase: str | None = None,
    ) -> WorkflowEvent:
        with self.database.transaction() as con:
            return self.append_in_transaction(
                con,
                workflow_id,
                event_type,
                payload,
                mutation_id=mutation_id,
                request_id=request_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                actor_type=actor_type,
                actor_id=actor_id,
                step_id=step_id,
                item_id=item_id,
                attempt_id=attempt_id,
                phase=phase,
            )

    def append_in_transaction(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        mutation_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        actor_type: str = "SYSTEM",
        actor_id: str | None = None,
        step_id: str | None = None,
        item_id: str | None = None,
        attempt_id: str | None = None,
        phase: str | None = None,
    ) -> WorkflowEvent:
        mutation_id = mutation_id or new_id("mutation")
        safe_payload = redact_public_json(payload)
        if not isinstance(safe_payload, Mapping):
            safe_payload = {}
        existing = con.execute(
            "SELECT * FROM workflow_events WHERE mutation_id=?",
            (mutation_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["workflow_id"]) != workflow_id:
                raise EventStoreError("mutation_id is already owned by another workflow")
            expected_payload = canonical_json(dict(safe_payload))
            if (
                str(existing["event_type"]) != event_type
                or str(existing["payload_json"]) != expected_payload
                or existing["step_id"] != step_id
                or existing["item_id"] != item_id
                or existing["attempt_id"] != attempt_id
                or existing["phase"] != phase
            ):
                raise EventStoreError("mutation_id is already used for a different event")
            return _event_from_row(existing)

        con.execute(
            "INSERT OR IGNORE INTO workflow_event_streams(workflow_id, updated_at) VALUES (?, ?)",
            (workflow_id, utc_now()),
        )
        stream = con.execute(
            "SELECT latest_seq FROM workflow_event_streams WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        if stream is None:
            raise EventStoreError(f"workflow does not exist: {workflow_id}")
        con.execute(
            "UPDATE workflow_event_streams SET latest_seq=latest_seq+1, updated_at=? WHERE workflow_id=?",
            (utc_now(), workflow_id),
        )
        seq = int(
            con.execute(
                "SELECT latest_seq FROM workflow_event_streams WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()[0]
        )
        created_at = utc_now()
        event_id = new_id("event")
        con.execute(
            """INSERT INTO workflow_events (
                event_id, workflow_id, seq, mutation_id, schema_version,
                step_id, item_id, attempt_id, request_id, correlation_id,
                causation_id, actor_type, actor_id, event_type, phase,
                payload_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                workflow_id,
                seq,
                mutation_id,
                self.schema_version,
                step_id,
                item_id,
                attempt_id,
                request_id,
                correlation_id or str(uuid.uuid4()),
                causation_id,
                actor_type,
                actor_id,
                event_type,
                phase,
                canonical_json(dict(safe_payload)),
                created_at,
            ),
        )
        return _event_from_row(
            con.execute("SELECT * FROM workflow_events WHERE event_id=?", (event_id,)).fetchone()
        )

    def write_snapshot(
        self,
        workflow_id: str,
        state: Mapping[str, Any],
        *,
        snapshot_seq: int | None = None,
        snapshot_id: str | None = None,
    ) -> str:
        with self.database.transaction() as con:
            return self.write_snapshot_in_transaction(
                con,
                workflow_id,
                state,
                snapshot_seq=snapshot_seq,
                snapshot_id=snapshot_id,
            )

    def write_snapshot_in_transaction(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        state: Mapping[str, Any],
        *,
        snapshot_seq: int | None = None,
        snapshot_id: str | None = None,
    ) -> str:
        stream = con.execute(
            "SELECT latest_seq FROM workflow_event_streams WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        if stream is None:
            raise EventStoreError(f"workflow does not exist: {workflow_id}")
        seq = int(stream["latest_seq"] if snapshot_seq is None else snapshot_seq)
        safe_state = redact_public_json(state)
        if not isinstance(safe_state, Mapping):
            safe_state = {}
        state_json = canonical_json(dict(safe_state))
        snapshot_id = snapshot_id or new_id("snapshot")
        event_row = con.execute(
            "SELECT event_id FROM workflow_events WHERE workflow_id=? AND seq=?",
            (workflow_id, seq),
        ).fetchone()
        event_id = event_row[0] if event_row else None
        con.execute(
            """INSERT OR IGNORE INTO workflow_snapshots(
                snapshot_id, workflow_id, snapshot_seq, snapshot_event_id,
                schema_version, state_json, size_bytes, created_at
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                workflow_id,
                seq,
                event_id,
                self.schema_version,
                state_json,
                len(state_json.encode("utf-8")),
                utc_now(),
            ),
        )
        actual_snapshot_id = con.execute(
            "SELECT snapshot_id FROM workflow_snapshots WHERE workflow_id=? AND snapshot_seq=?",
            (workflow_id, seq),
        ).fetchone()[0]
        if event_id and seq >= 1:
            con.execute(
                """INSERT OR IGNORE INTO snapshot_anchors(
                    workflow_id, snapshot_event_id, snapshot_seq, snapshot_id, retained_until
                ) VALUES (?,?,?,?,NULL)""",
                (workflow_id, event_id, seq, actual_snapshot_id),
            )
        con.execute(
            """UPDATE workflow_event_streams
               SET latest_snapshot_seq=CASE
                       WHEN latest_snapshot_seq IS NULL OR latest_snapshot_seq < ? THEN ?
                       ELSE latest_snapshot_seq
                   END,
                   updated_at=?
               WHERE workflow_id=?""",
            (seq, seq, utc_now(), workflow_id),
        )
        return str(actual_snapshot_id)

    def _cursor_seq(self, con: sqlite3.Connection, workflow_id: str, last_event_id: str | None) -> int:
        if not last_event_id:
            return 0
        row = con.execute(
            "SELECT workflow_id, seq FROM workflow_events WHERE event_id=?",
            (last_event_id,),
        ).fetchone()
        if row is None:
            # Compaction removes the event row but retains its snapshot anchor
            # so a reconnect can still be classified as an expired cursor.
            row = con.execute(
                "SELECT workflow_id, snapshot_seq AS seq FROM snapshot_anchors WHERE snapshot_event_id=?",
                (last_event_id,),
            ).fetchone()
        if row is None:
            # A valid cursor can be deleted by compaction without being the
            # snapshot anchor itself.  Once a stream has a retained-window
            # boundary, an unknown cursor for that workflow is therefore
            # treated as an old cursor rather than an invalid cross-workflow
            # identifier.  The workflow-specific boundary prevents this from
            # weakening validation for an un-compacted stream.
            stream = con.execute(
                "SELECT min_available_seq FROM workflow_event_streams WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if stream is not None and int(stream["min_available_seq"]) > 1:
                raise CursorExpired("event cursor is older than the retained event window")
        if row is None or str(row["workflow_id"]) != workflow_id:
            raise InvalidCursor("Last-Event-ID does not belong to this workflow")
        return int(row["seq"])

    def read_after(
        self,
        workflow_id: str,
        *,
        last_event_id: str | None = None,
        after_seq: int | None = None,
    ) -> list[WorkflowEvent]:
        with self.database.read_transaction() as con:
            stream = con.execute(
                "SELECT min_available_seq, latest_seq FROM workflow_event_streams WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if stream is None:
                raise EventStoreError(f"workflow does not exist: {workflow_id}")
            cursor = self._cursor_seq(con, workflow_id, last_event_id) if after_seq is None else int(after_seq)
            if cursor < int(stream["min_available_seq"]) - 1:
                raise CursorExpired("event cursor is older than the retained event window")
            rows = con.execute(
                "SELECT * FROM workflow_events WHERE workflow_id=? AND seq>? ORDER BY seq",
                (workflow_id, cursor),
            ).fetchall()
            return [_event_from_row(row) for row in rows]

    def compact(self, workflow_id: str, *, before_seq: int) -> int:
        """Delete old events only after a snapshot anchor exists."""

        with self.database.transaction() as con:
            stream = con.execute(
                "SELECT min_available_seq, latest_seq FROM workflow_event_streams WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if stream is None:
                raise EventStoreError(f"workflow does not exist: {workflow_id}")
            before_seq = int(before_seq)
            latest_seq = int(stream["latest_seq"])
            min_available_seq = int(stream["min_available_seq"])
            if before_seq < 1 or before_seq > latest_seq + 1:
                raise EventStoreError("compaction boundary is outside the event stream")
            if before_seq <= min_available_seq:
                return 0
            anchor = con.execute(
                "SELECT 1 FROM snapshot_anchors WHERE workflow_id=? AND snapshot_seq>=? LIMIT 1",
                (workflow_id, before_seq - 1),
            ).fetchone()
            if anchor is None:
                raise EventStoreError("compaction requires a retained snapshot anchor")
            result = con.execute(
                "DELETE FROM workflow_events WHERE workflow_id=? AND seq<?",
                (workflow_id, before_seq),
            )
            con.execute(
                "UPDATE workflow_event_streams SET min_available_seq=?, updated_at=? WHERE workflow_id=?",
                (before_seq, utc_now(), workflow_id),
            )
            return int(result.rowcount)

    def sse(self, events: Iterable[WorkflowEvent]) -> Iterator[str]:
        for event in events:
            yield f"id: {event.event_id}\n"
            yield "event: workflow_event\n"
            yield f"data: {json.dumps(event.as_dict(), ensure_ascii=False, separators=(',', ':'))}\n\n"

    def snapshot_frame(self, state: Mapping[str, Any], *, workflow_id: str, seq: int, event_id: str | None) -> str:
        payload = {
            "kind": "snapshot",
            "workflow_id": workflow_id,
            "snapshot_seq": seq,
            "snapshot_event_id": event_id,
            "state": dict(state),
        }
        event_id_line = f"id: {event_id}\n" if event_id else ""
        return event_id_line + "event: snapshot\ndata: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"
