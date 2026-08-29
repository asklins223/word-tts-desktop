"""Durable, redacted file-side journal for external side-effect intents.

SQLite is the source of truth for workflow state, but a small append-only
journal closes the failure window between deciding to perform a billable
operation and committing the corresponding database row.  The journal never
stores request payloads, credentials, cookies, or provider responses: only
content hashes and stable identifiers are written.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from .domain import canonical_json, content_hash, new_id, utc_now


class SideEffectLogError(RuntimeError):
    code = "PERSISTENCE_ERROR"


class SideEffectIntentLog:
    """An fsync'd JSONL journal with process and thread safe appends."""

    VERSION = 1
    _SAFE_STATES = {
        "RECORDED", "COMMITTED", "IN_FLIGHT", "SUBMITTED", "CONFIRMED",
        "ARCHIVED", "NEEDS_RECONCILE", "REJECTED", "AMBIGUOUS", "ABORTED",
    }

    def __init__(self, path: str | os.PathLike[str], *, create_parent: bool = True) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        if create_parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def record(
        self,
        *,
        operation_namespace: str,
        operation_key: str,
        payload: Mapping[str, Any] | None = None,
        payload_hash: str | None = None,
        workflow_id: str | None = None,
        step_id: str | None = None,
        attempt_id: str | None = None,
        work_unit_id: str | None = None,
        provider_account_scope: str | None = None,
        intent_id: str | None = None,
    ) -> str:
        """Record an intent before its SQLite transaction begins.

        Repeating the same namespace/key/hash is idempotent and does not add
        noisy duplicate intent records.  A different hash is fail-closed.
        """

        namespace = self._required_text(operation_namespace, "operation_namespace")
        key = self._required_text(operation_key, "operation_key")
        digest = str(payload_hash or (content_hash(payload or {})))
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise SideEffectLogError("payload_hash must be a SHA-256 digest")
        with self._lock:
            entries = self.read_entries()
            latest = None
            for entry in reversed(entries):
                if entry.get("operation_namespace") != namespace or entry.get("operation_key") != key:
                    continue
                latest = entry
                break
            # An ABORTED entry means the previous caller durably recorded the
            # recovery fact, but its SQLite transaction was rejected before a
            # side-effect row could be committed.  It must not be reused as a
            # live intent on the next attempt.
            if latest is not None and latest.get("state") == "ABORTED":
                latest = None
            if latest is not None:
                entry = latest
                if entry.get("payload_hash") != digest:
                    raise SideEffectLogError("side-effect key is bound to another payload")
                return str(entry["intent_id"])
            entry_id = intent_id or new_id("intent")
            self._append({
                "entry_type": "intent",
                "journal_version": self.VERSION,
                "intent_id": entry_id,
                "operation_namespace": namespace,
                "operation_key": key,
                "payload_hash": digest,
                "state": "RECORDED",
                "workflow_id": workflow_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "work_unit_id": work_unit_id,
                "provider_account_scope": provider_account_scope,
                "created_at": utc_now(),
            })
            return entry_id

    def abort(
        self,
        *,
        operation_namespace: str,
        operation_key: str,
        payload_hash: str | None = None,
        intent_id: str | None = None,
    ) -> str:
        """Record that a pre-side-effect intent was rejected before SQLite commit.

        The marker keeps the append-only journal auditable while allowing a
        later retry to allocate a fresh intent id.  Callers must use this only
        when they know their SQLite transaction was rolled back; an uncertain
        commit must remain fail-closed as ``RECORDED``.
        """

        return self.mark(
            operation_namespace=operation_namespace,
            operation_key=operation_key,
            state="ABORTED",
            payload_hash=payload_hash,
            intent_id=intent_id,
        )

    def mark(
        self,
        *,
        operation_namespace: str,
        operation_key: str,
        state: str,
        payload_hash: str | None = None,
        intent_id: str | None = None,
    ) -> str:
        """Append a redacted state transition after a DB state transition."""

        if state not in self._SAFE_STATES:
            raise SideEffectLogError(f"unsupported side-effect journal state: {state}")
        namespace = self._required_text(operation_namespace, "operation_namespace")
        key = self._required_text(operation_key, "operation_key")
        with self._lock:
            entries = self.read_entries()
            current = next(
                (entry for entry in reversed(entries)
                 if entry.get("operation_namespace") == namespace and entry.get("operation_key") == key),
                None,
            )
            if current is None:
                raise SideEffectLogError("cannot mark a side-effect that has no journal intent")
            if intent_id and current.get("intent_id") != intent_id:
                raise SideEffectLogError("side-effect journal intent id does not match")
            digest = str(payload_hash or current.get("payload_hash") or "")
            if digest != current.get("payload_hash"):
                raise SideEffectLogError("side-effect journal payload hash does not match")
            self._append({
                "entry_type": "state",
                "journal_version": self.VERSION,
                "intent_id": str(current["intent_id"]),
                "operation_namespace": namespace,
                "operation_key": key,
                "payload_hash": digest,
                "state": state,
                "created_at": utc_now(),
            })
            return str(current["intent_id"])

    def read_entries(self) -> list[dict[str, Any]]:
        """Read and validate every journal line; malformed data is fatal."""

        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as source:
                for line_number, raw in enumerate(source, 1):
                    if not raw.strip():
                        continue
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise SideEffectLogError(f"invalid side-effect journal line {line_number}") from exc
                    if not isinstance(value, dict):
                        raise SideEffectLogError(f"side-effect journal line {line_number} is not an object")
                    self._validate_entry(value, line_number)
                    entries.append(value)
        except OSError as exc:
            raise SideEffectLogError(f"cannot read side-effect journal: {self.path}") from exc
        return entries

    def latest_by_key(self) -> dict[tuple[str, str], dict[str, Any]]:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self.read_entries():
            latest[(str(entry["operation_namespace"]), str(entry["operation_key"]))] = entry
        return latest

    def verify_against_rows(self, rows: Iterable[Mapping[str, Any]]) -> list[str]:
        """Return consistency errors for DB intent rows without mutating either side."""

        latest = self.latest_by_key()
        row_map: dict[tuple[str, str], Mapping[str, Any]] = {}
        errors: list[str] = []
        for row in rows:
            key = (str(row["operation_namespace"]), str(row["operation_key"]))
            row_map[key] = row
            entry = latest.get(key)
            if entry is None:
                errors.append(f"missing journal intent: {key[0]}:{key[1]}")
                continue
            if entry.get("state") == "ABORTED":
                errors.append(f"aborted journal intent has SQLite row: {key[0]}:{key[1]}")
                continue
            if str(entry.get("payload_hash")) != str(row["payload_hash"]):
                errors.append(f"journal hash mismatch: {key[0]}:{key[1]}")
            if str(entry.get("intent_id")) != str(row["intent_id"]):
                errors.append(f"journal intent id mismatch: {key[0]}:{key[1]}")
            if str(entry.get("state")) != str(row.get("state")):
                errors.append(f"journal state mismatch: {key[0]}:{key[1]}")
        for key in sorted(
            key for key in set(latest) - set(row_map)
            if latest[key].get("state") != "ABORTED"
        ):
            errors.append(f"journal intent has no SQLite row: {key[0]}:{key[1]}")
        return errors

    def _append(self, entry: Mapping[str, Any]) -> None:
        serialized = (canonical_json(dict(entry)) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("ab", buffering=0) as target:
                try:
                    import fcntl

                    fcntl.flock(target.fileno(), fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
                try:
                    target.write(serialized)
                    target.flush()
                    os.fsync(target.fileno())
                finally:
                    try:
                        import fcntl

                        fcntl.flock(target.fileno(), fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        pass
            try:
                os.chmod(self.path, 0o600)
            except OSError as exc:
                raise SideEffectLogError(f"cannot protect side-effect journal: {self.path}") from exc
            self._fsync_directory(self.path.parent)
        except OSError as exc:
            raise SideEffectLogError(f"cannot fsync side-effect journal: {self.path}") from exc

    @classmethod
    def _validate_entry(cls, entry: Mapping[str, Any], line_number: int) -> None:
        required = {"entry_type", "journal_version", "intent_id", "operation_namespace", "operation_key", "payload_hash", "state", "created_at"}
        missing = required - set(entry)
        if missing:
            raise SideEffectLogError(f"side-effect journal line {line_number} misses {sorted(missing)}")
        if entry.get("entry_type") not in {"intent", "state"} or entry.get("journal_version") != cls.VERSION:
            raise SideEffectLogError(f"unsupported side-effect journal line {line_number}")
        if not all(isinstance(entry.get(name), str) and entry.get(name) for name in ("intent_id", "operation_namespace", "operation_key", "created_at")):
            raise SideEffectLogError(f"invalid side-effect journal identifiers on line {line_number}")
        digest = entry.get("payload_hash")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise SideEffectLogError(f"invalid side-effect journal hash on line {line_number}")
        if entry.get("state") not in cls._SAFE_STATES:
            raise SideEffectLogError(f"invalid side-effect journal state on line {line_number}")

    @staticmethod
    def _required_text(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SideEffectLogError(f"{name} is required")
        return value

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(str(path), os.O_RDONLY)
        except OSError:
            # The supported desktop target is POSIX.  If the directory cannot
            # be opened, the durability contract is unknown and the caller
            # must fail closed before invoking a billable side effect.
            raise
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
