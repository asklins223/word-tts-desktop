"""SQLite connection and transaction boundary for the workflow store."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

from db.migration_runner import (
    MigrationError,
    apply_migrations,
    load_migrations,
    prepare_migration_backup,
    resolve_target,
)


class WorkflowDatabase:
    """Owns database initialization and short, write-locked transactions.

    A connection is deliberately scoped to one operation.  This keeps a stale
    connection from crossing an Electron/backend restart and makes every state
    mutation visibly carry one SQLite transaction.
    """

    def __init__(self, path: str | os.PathLike[str], *, profile: str = "2a") -> None:
        self.path = Path(path).expanduser().resolve()
        self.profile = profile
        self._lock_guard = threading.Lock()
        self._lock_file: IO[str] | None = None
        self.last_migration_backup: str | None = None

    def _prepare_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

    def connect(self, *, write: bool = False) -> sqlite3.Connection:
        if write:
            self._prepare_parent()
        con = sqlite3.connect(
            str(self.path),
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        if write:
            # These settings are applied before BEGIN IMMEDIATE.  A read-only
            # check never calls this method with write=True.
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = FULL")
        else:
            con.execute("PRAGMA query_only = ON")
        return con

    def initialize(self) -> None:
        self._prepare_parent()
        migrations = load_migrations()
        target = resolve_target(migrations, profile=self.profile)
        acquired_here = self._acquire_database_lock()
        try:
            backup_path = prepare_migration_backup(self.path, target=target, migrations=migrations)
            if backup_path is not None:
                # Keep initialization observable without changing the public
                # API; the backup remains beside the database for
                # restore/verification tooling.
                self.last_migration_backup = str(backup_path)
            con = self.connect(write=True)
            try:
                result = apply_migrations(con, target=target, migrations=migrations)
                if result:
                    raise MigrationError(f"workflow schema initialization failed: {result}")
            finally:
                con.close()
        except Exception:
            if acquired_here:
                self.close()
            raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _acquire_database_lock(self) -> bool:
        with self._lock_guard:
            if self._lock_file is not None:
                return False
            lock_path = self.path.parent / ".workflow.lock"
            try:
                import fcntl
            except ImportError:  # pragma: no cover - the supported MVP is macOS.
                return True
            lock_file = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock_file.close()
                raise RuntimeError("workflow data directory is already in use") from exc
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            self._lock_file = lock_file
            return True

    def close(self) -> None:
        with self._lock_guard:
            if self._lock_file is None:
                return
            try:
                import fcntl

                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self._lock_file.close()
            self._lock_file = None

    def __del__(self) -> None:  # pragma: no cover - exercised by process teardown.
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside one pessimistic write transaction."""

        con = self.connect(write=True)
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        con = self.connect(write=False)
        try:
            yield con
        finally:
            con.close()
