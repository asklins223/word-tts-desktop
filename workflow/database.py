"""SQLite connection and transaction boundary for the workflow store."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

from db.migration_runner import (
    Migration,
    MigrationError,
    apply_migrations,
    backup_database,
    load_migrations,
    prepare_migration_backup,
    repair_data_consistency,
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
        self._lock_file: IO[bytes] | None = None
        self._lock_backend: str | None = None
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
            repair_backup_created = backup_path is not None

            def repair_callback(
                con: sqlite3.Connection,
                migration: Migration | None,
                statement: str | None,
                error: Exception,
            ) -> bool:
                nonlocal repair_backup_created
                if not repair_backup_created:
                    repair_backup = backup_database(self.path)
                    if repair_backup is None:
                        raise MigrationError(
                            "cannot create backup before repairing data consistency"
                        )
                    self.last_migration_backup = str(repair_backup)
                    repair_backup_created = True
                return repair_data_consistency(con, migration, statement, error)

            con = self.connect(write=True)
            try:
                result = apply_migrations(
                    con,
                    target=target,
                    migrations=migrations,
                    repair_data_consistency=repair_callback,
                )
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
            lock_file = lock_path.open("a+b", buffering=0)
            lock_file.seek(0)
            if lock_file.read(1) == b"":
                lock_file.seek(0)
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                backend = "fcntl"
            except ImportError:  # pragma: no cover - exercised on Windows.
                try:
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    backend = "msvcrt"
                except (ImportError, OSError) as exc:
                    lock_file.close()
                    raise RuntimeError("workflow data directory cannot be locked on this platform") from exc
            except (BlockingIOError, OSError) as exc:
                lock_file.close()
                raise RuntimeError("workflow data directory is already in use") from exc
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            self._lock_file = lock_file
            self._lock_backend = backend
            return True

    def close(self) -> None:
        with self._lock_guard:
            if self._lock_file is None:
                return
            try:
                if self._lock_backend == "msvcrt":  # pragma: no cover - exercised on Windows.
                    import msvcrt

                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self._lock_file.close()
            self._lock_file = None
            self._lock_backend = None

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
            # Keep every SELECT in one consistent SQLite snapshot. Without an
            # explicit read transaction, state_version and joined item rows
            # could come from different commits during a workspace refresh.
            con.execute("BEGIN")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
