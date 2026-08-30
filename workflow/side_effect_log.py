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
    # Windows does not expose a portable directory-fsync operation.  On
    # Windows runners, opening or flushing a directory handle commonly
    # returns ACCESS_DENIED even though the journal file itself was flushed
    # successfully.  Treat those documented unsupported-handle outcomes as
    # the expected fallback; unexpected errors must still fail closed.
    _UNSUPPORTED_WINDOWS_DIRECTORY_FLUSH_ERRORS = frozenset({
        1,    # ERROR_INVALID_FUNCTION
        5,    # ERROR_ACCESS_DENIED
        6,    # ERROR_INVALID_HANDLE
        50,   # ERROR_NOT_SUPPORTED
        87,   # ERROR_INVALID_PARAMETER
        120,  # ERROR_CALL_NOT_IMPLEMENTED
    })
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
        """Read and validate every journal line.

        The append protocol always writes a trailing newline.  If a process
        dies after writing only part of the final line, discard that torn tail
        while preserving every preceding complete record.  A malformed line
        that *does* end in a newline remains fatal: it is a data-integrity
        error, not a crash-tail repair case.
        """

        raw_bytes = self._read_and_repair_torn_tail()
        if not raw_bytes:
            return []
        entries: list[dict[str, Any]] = []
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SideEffectLogError("side-effect journal is not valid UTF-8") from exc
        for line_number, raw in enumerate(text.splitlines(), 1):
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
        return entries

    def _read_and_repair_torn_tail(self) -> bytes:
        if not self.path.exists():
            return b""
        try:
            with self._lock:
                with self.path.open("r+b", buffering=0) as source:
                    self._lock_descriptor(source, exclusive=True)
                    try:
                        raw = source.read()
                        if raw and not raw.endswith(b"\n"):
                            last_newline = raw.rfind(b"\n")
                            tail = raw[last_newline + 1:]
                            complete_legacy_line = False
                            try:
                                # Older versions wrote a single JSON record
                                # without a final newline.  Preserve it when
                                # it is complete; read_entries still performs
                                # the authoritative journal-schema checks.
                                candidate = json.loads(tail.decode("utf-8"))
                                if isinstance(candidate, dict):
                                    # Parsing as an object is not enough: a
                                    # torn write can end at a valid JSON object
                                    # prefix (for example {"a":1}).  Only a
                                    # complete, current-schema journal entry
                                    # may be repaired by appending the missing
                                    # newline.  Invalid objects are discarded
                                    # as torn tails and will not be persisted
                                    # as a durable journal line.
                                    self._validate_entry(
                                        candidate,
                                        raw[:last_newline + 1].count(b"\n") + 1,
                                    )
                                    complete_legacy_line = True
                            except (UnicodeDecodeError, json.JSONDecodeError, SideEffectLogError):
                                pass
                            if complete_legacy_line:
                                source.seek(0, os.SEEK_END)
                                source.write(b"\n")
                                source.flush()
                                os.fsync(source.fileno())
                                self._fsync_directory(self.path.parent)
                                raw += b"\n"
                            else:
                                repaired = raw[: last_newline + 1] if last_newline >= 0 else b""
                                source.seek(len(repaired))
                                source.truncate()
                                source.flush()
                                os.fsync(source.fileno())
                                self._fsync_directory(self.path.parent)
                                raw = repaired
                        return raw
                    finally:
                        self._unlock_descriptor(source)
        except OSError as exc:
            raise SideEffectLogError(f"cannot read side-effect journal: {self.path}") from exc

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
            # A physically deleted unfinished workflow may intentionally leave
            # a terminal, non-billable journal fact behind.  The append-only
            # journal is the audit trail for that decision; only live or
            # uncertain states are orphan errors that require recovery.
            if latest[key].get("state") not in {"ABORTED", "ARCHIVED", "REJECTED"}
        ):
            errors.append(f"journal intent has no SQLite row: {key[0]}:{key[1]}")
        return errors

    def _append(self, entry: Mapping[str, Any]) -> None:
        serialized = (canonical_json(dict(entry)) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("ab", buffering=0) as target:
                self._lock_descriptor(target, exclusive=True)
                try:
                    target.write(serialized)
                    target.flush()
                    os.fsync(target.fileno())
                finally:
                    self._unlock_descriptor(target)
            try:
                os.chmod(self.path, 0o600)
            except OSError as exc:
                raise SideEffectLogError(f"cannot protect side-effect journal: {self.path}") from exc
            self._fsync_directory(self.path.parent)
        except OSError as exc:
            raise SideEffectLogError(f"cannot fsync side-effect journal: {self.path}") from exc

    @staticmethod
    def _lock_descriptor(target: Any, *, exclusive: bool) -> None:
        """Coordinate append/repair across processes on supported desktops."""

        try:
            import fcntl
        except ImportError:  # pragma: no cover - exercised on Windows CI.
            try:
                import msvcrt
            except ImportError as exc:  # pragma: no cover - unsupported host.
                raise OSError("no supported side-effect journal lock backend") from exc
            # ``msvcrt.locking`` locks the region beginning at the current
            # file position.  Always use the first byte so append and repair
            # operations contend on the same region, regardless of where the
            # file was opened.
            target.seek(0)
            mode = getattr(msvcrt, "LK_LOCK" if exclusive else "LK_RLCK", msvcrt.LK_LOCK)
            msvcrt.locking(target.fileno(), mode, 1)
            target.seek(0)
            return
        fcntl.flock(target.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    @staticmethod
    def _unlock_descriptor(target: Any) -> None:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - exercised on Windows CI.
            try:
                import msvcrt
            except ImportError:
                return
            try:
                target.seek(0)
                msvcrt.locking(target.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return
        try:
            fcntl.flock(target.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

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
        if os.name == "nt":
            SideEffectIntentLog._flush_windows_directory(path)
            return
        try:
            descriptor = os.open(str(path), os.O_RDONLY)
        except OSError:
            # If the directory cannot be opened, the durability contract is
            # unknown and the caller must fail closed before invoking a
            # billable side effect.
            raise
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _flush_windows_directory(path: Path) -> None:
        """Flush a directory handle on Windows when the filesystem supports it.

        ``os.open(directory)`` is a POSIX idiom and raises on common Windows
        filesystems.  The file itself has already been flushed by ``_append``;
        Windows' native directory handle is used here for the remaining
        metadata flush.  Some filesystems do not expose flushable directory
        handles, in which case the durable file flush is the strongest
        operation available and the journal remains usable.
        """

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = [wintypes.HANDLE]
        flush_file_buffers.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002 | 0x00000004,  # FILE_SHARE_{READ,WRITE,DELETE}
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS, required for directories
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            if error in SideEffectIntentLog._UNSUPPORTED_WINDOWS_DIRECTORY_FLUSH_ERRORS:
                return
            raise OSError(error, f"cannot open side-effect journal directory: {path}")
        try:
            if not flush_file_buffers(handle):
                error = ctypes.get_last_error()
                if error in SideEffectIntentLog._UNSUPPORTED_WINDOWS_DIRECTORY_FLUSH_ERRORS:
                    return
                raise OSError(error, f"cannot flush side-effect journal directory: {path}")
        finally:
            close_handle(handle)
