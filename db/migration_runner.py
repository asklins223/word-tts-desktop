#!/usr/bin/env python3
"""Transactional SQLite migration runner for the workflow store.

The runner intentionally does not use :meth:`sqlite3.Connection.executescript`.
Python's sqlite3 wrapper commits an open transaction before ``executescript``;
that would leave half a migration behind after a later statement fails.  SQL
is split with SQLite's own ``complete_statement`` parser and every statement
is executed inside one ``BEGIN IMMEDIATE`` transaction.

``--check`` is non-mutating for an explicitly supplied database: it opens that
file read-only, validates its recorded checksums, and applies the selected
profile only to a temporary database.  A normal run is fail-closed and keeps
the database lock until the migration and integrity checks commit.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DB_PATH_ENV = "WORDTTS_DB_PATH"
DEFAULT_PROFILE = "full"
TWO_A_TARGET = 4


class MigrationError(RuntimeError):
    """A migration cannot safely be applied or verified."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


DataConsistencyRepair = Callable[
    [sqlite3.Connection, Migration | None, str | None, Exception], bool
]

_MAX_DATA_CONSISTENCY_REPAIRS = 8
_SQL_IDENTIFIER = (
    r'(?:[A-Za-z_][A-Za-z0-9_]*|"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[(?:[^\]])+\])'
)
_UNIQUE_INDEX_RE = re.compile(
    rf"\A\s*CREATE\s+UNIQUE\s+INDEX\s+(?P<index>{_SQL_IDENTIFIER})"
    rf"\s+ON\s+(?P<table>{_SQL_IDENTIFIER})\s*"
    rf"\((?P<columns>.*?)\)"
    rf"(?:\s+WHERE\s+(?P<where>.*?))?\s*;?\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


class _DataRepairNotApplicable(Exception):
    """The migration failure is not a safely identifiable data conflict."""


def _db_path() -> Path:
    env = os.getenv(DB_PATH_ENV)
    if env:
        return Path(env)
    try:
        from app_paths import ensure_data_dir

        return Path(ensure_data_dir()) / "workflow.db"
    except Exception:
        return Path(".runtime/workflow.db")


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _migration_version(name: str) -> int:
    match = re.fullmatch(r"(\d+)_([A-Za-z0-9][A-Za-z0-9_.-]*)\.sql", name)
    if not match:
        raise MigrationError(f"invalid migration filename: {name}")
    version = int(match.group(1))
    if version < 1:
        raise MigrationError(f"migration version must be positive: {name}")
    return version


def load_migrations() -> list[Migration]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise MigrationError("no migration files found")

    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for path in files:
        version = _migration_version(path.name)
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version: {version}")
        seen_versions.add(version)
        sql = path.read_text(encoding="utf-8")
        migrations.append(Migration(version, path.name, sql, _checksum(sql)))

    expected = list(range(1, len(migrations) + 1))
    versions = [migration.version for migration in migrations]
    if versions != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 1: found {versions}"
        )
    return migrations


def resolve_target(
    migrations: Sequence[Migration],
    *,
    up_to: int | str | None = None,
    profile: str = DEFAULT_PROFILE,
) -> int:
    if up_to is not None and profile != DEFAULT_PROFILE:
        raise MigrationError("--up-to and --profile cannot be combined")
    if up_to is None:
        target = TWO_A_TARGET if profile == "2a" else migrations[-1].version
    else:
        try:
            target = int(up_to)
        except (TypeError, ValueError) as exc:
            raise MigrationError(f"invalid --up-to version: {up_to}") from exc

    versions = {migration.version for migration in migrations}
    if target not in versions:
        raise MigrationError(
            f"target migration {target} is not available; choose one of {sorted(versions)}"
        )
    return target


def _has_non_schema_objects(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view') "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _ensure_migrations_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            checksum    TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
        """
    )


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _applied_rows(con: sqlite3.Connection) -> list[tuple[int, str, str]]:
    if not _table_exists(con, "schema_migrations"):
        if _has_non_schema_objects(con):
            raise MigrationError(
                "database has application objects but no schema_migrations table"
            )
        return []
    rows = [
        (int(row[0]), str(row[1]), str(row[2]))
        for row in con.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    versions = [row[0] for row in rows]
    if versions != list(range(1, len(rows) + 1)):
        raise MigrationError(f"schema_migrations has non-contiguous versions: {versions}")
    return rows


def verify_recorded_checksums(
    con: sqlite3.Connection,
    migrations: Sequence[Migration],
    target: int,
) -> int:
    rows = _applied_rows(con)
    applied_by_version = {version: (name, checksum) for version, name, checksum in rows}
    if rows and rows[-1][0] > target:
        raise MigrationError(
            f"database is already at version {rows[-1][0]}, target {target} would require downgrade"
        )

    for migration in migrations:
        recorded = applied_by_version.get(migration.version)
        if recorded is None:
            continue
        name, checksum = recorded
        if name != migration.name:
            raise MigrationError(
                f"migration name mismatch for v{migration.version}: "
                f"database has {name}, files have {migration.name}"
            )
        if checksum != migration.checksum:
            raise MigrationError(
                f"checksum mismatch for {migration.name}: "
                f"expected {checksum[:12]} got {migration.checksum[:12]}"
            )
    return rows[-1][0] if rows else 0


def _statement_has_sql(statement: str) -> bool:
    # ``complete_statement`` also returns true for a comment ending in a
    # semicolon.  Ignore comments-only chunks while preserving SQL strings.
    without_comments = re.sub(r"(?m)^\s*--[^\n]*(?:\n|$)", "", statement)
    return bool(without_comments.strip().rstrip("; "))


def iter_sql_statements(sql: str) -> Iterator[str]:
    """Yield complete SQLite statements without committing between them."""

    buffer: list[str] = []
    for line in sql.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            if _statement_has_sql(candidate):
                yield candidate.strip()
            buffer.clear()

    trailing = "".join(buffer).strip()
    if trailing and _statement_has_sql(trailing):
        raise MigrationError("migration contains an incomplete SQL statement")


def _configure_write_connection(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    # journal_mode changes the database header and must never happen from a
    # read-only --check connection.
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = FULL")


def _configure_read_connection(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA query_only = ON")


def _check_integrity(con: sqlite3.Connection) -> None:
    fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise MigrationError(f"foreign_key_check failed: {fk_errors}")
    integrity = con.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise MigrationError(f"integrity_check failed: {integrity}")


def _is_data_consistency_failure(exc: BaseException) -> bool:
    message = str(exc).upper()
    if isinstance(exc, sqlite3.IntegrityError):
        return (
            "UNIQUE CONSTRAINT FAILED" in message
            or "FOREIGN KEY CONSTRAINT FAILED" in message
        )
    return isinstance(exc, MigrationError) and "FOREIGN_KEY_CHECK FAILED" in message


def _quote_identifier(identifier: str) -> str:
    if "\x00" in identifier:
        raise _DataRepairNotApplicable("identifier contains NUL")
    return '"' + identifier.replace('"', '""') + '"'


def _strip_leading_sql_comments(statement: str) -> str:
    remaining = statement.strip()
    while remaining:
        if remaining.startswith("--"):
            newline = remaining.find("\n")
            if newline < 0:
                return ""
            remaining = remaining[newline + 1 :].lstrip()
            continue
        if remaining.startswith("/*"):
            end = remaining.find("*/", 2)
            if end < 0:
                return ""
            remaining = remaining[end + 2 :].lstrip()
            continue
        break
    return remaining


def _unquote_sql_identifier(token: str) -> str | None:
    token = token.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        return token
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        body = token[1:-1]
        if '"' in body.replace('""', ""):
            return None
        return body.replace('""', '"')
    if len(token) >= 2 and token[0] == "`" and token[-1] == "`":
        body = token[1:-1]
        if "`" in body.replace("``", ""):
            return None
        return body.replace("``", "`")
    if len(token) >= 2 and token[0] == "[" and token[-1] == "]":
        body = token[1:-1]
        return body if "]" not in body else None
    return None


def _rowid_table_columns(
    con: sqlite3.Connection,
    table_name: str,
) -> tuple[str, dict[str, tuple[object, ...]]]:
    table = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    if table is None or table[0] is None:
        raise _DataRepairNotApplicable(f"table is unavailable: {table_name}")
    if re.search(r"\bWITHOUT\s+ROWID\b", str(table[0]), re.IGNORECASE):
        raise _DataRepairNotApplicable(f"table has no rowid: {table_name}")
    rows = con.execute(f"PRAGMA table_info({_quote_identifier(table_name)})").fetchall()
    columns = {str(row[1]): tuple(row) for row in rows}
    if not columns:
        raise _DataRepairNotApplicable(f"table has no columns: {table_name}")
    return _quote_identifier(table_name), columns


def _unique_index_conflicts(
    con: sqlite3.Connection,
    statement: str,
) -> tuple[str, list[int]] | None:
    match = _UNIQUE_INDEX_RE.match(_strip_leading_sql_comments(statement))
    if match is None:
        return None

    table_name = _unquote_sql_identifier(match.group("table"))
    columns = [
        _unquote_sql_identifier(column)
        for column in match.group("columns").split(",")
    ]
    if table_name is None or not columns or any(column is None for column in columns):
        return None
    normalized_columns = [column for column in columns if column is not None]
    if len(set(normalized_columns)) != len(normalized_columns):
        return None

    try:
        quoted_table, table_columns = _rowid_table_columns(con, table_name)
    except _DataRepairNotApplicable:
        return None
    if any(column not in table_columns for column in normalized_columns):
        return None

    predicate = match.group("where")
    if predicate is None:
        predicate = "1"
    else:
        predicate = predicate.strip()
        if predicate.endswith(";"):
            predicate = predicate[:-1].rstrip()
        # The predicate comes from a frozen migration file, but rejecting
        # statement separators keeps this repair bounded to one expression.
        if not predicate or ";" in predicate:
            return None

    quoted_columns = [_quote_identifier(column) for column in normalized_columns]
    non_null = " AND ".join(f"{column} IS NOT NULL" for column in quoted_columns)
    group_by = ", ".join(quoted_columns)
    rows = con.execute(
        f"""
        SELECT rowid
        FROM {quoted_table}
        WHERE ({predicate})
          AND ({non_null})
          AND rowid NOT IN (
              SELECT MIN(rowid)
              FROM {quoted_table}
              WHERE ({predicate})
                AND ({non_null})
              GROUP BY {group_by}
          )
        ORDER BY rowid
        """
    ).fetchall()
    return table_name, [int(row[0]) for row in rows]


def _foreign_key_conflicts(con: sqlite3.Connection) -> list[tuple[str, int]] | None:
    rows = con.execute("PRAGMA foreign_key_check").fetchall()
    conflicts: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        table_name = str(row[0]) if row[0] is not None else ""
        if not table_name or row[1] is None:
            return None
        try:
            rowid = int(row[1])
            _rowid_table_columns(con, table_name)
        except (TypeError, ValueError, _DataRepairNotApplicable):
            return None
        conflict = (table_name, rowid)
        if conflict not in seen:
            seen.add(conflict)
            conflicts.append(conflict)
    return conflicts


def _delete_rowids(
    con: sqlite3.Connection,
    rows: Sequence[tuple[str, int]],
) -> int:
    deleted = 0
    for table_name, rowid in rows:
        quoted_table, _ = _rowid_table_columns(con, table_name)
        exists = con.execute(
            f"SELECT 1 FROM {quoted_table} WHERE rowid=?",
            (rowid,),
        ).fetchone()
        if exists is None:
            continue
        con.execute(f"DELETE FROM {quoted_table} WHERE rowid=?", (rowid,))
        deleted += 1
    return deleted


def _rollback_repair_savepoint(con: sqlite3.Connection, name: str) -> None:
    try:
        con.execute(f"ROLLBACK TO {name}")
    except sqlite3.Error:
        pass
    try:
        con.execute(f"RELEASE {name}")
    except sqlite3.Error:
        pass


def repair_data_consistency(
    con: sqlite3.Connection,
    migration: Migration | None,
    statement: str | None,
    error: Exception,
) -> bool:
    """Delete only identifiable conflicting rows so a migration can retry.

    This deliberately handles a narrow set of cases: duplicate rows blocking a
    ``CREATE UNIQUE INDEX`` and already-existing orphan rows reported by
    ``PRAGMA foreign_key_check``.  An INSERT/UPDATE that fails during a
    migration has no unambiguous row to delete, so it remains a hard failure.
    """

    if not _is_data_consistency_failure(error):
        return False

    savepoint = "data_consistency_repair"
    try:
        con.execute(f"SAVEPOINT {savepoint}")
    except sqlite3.Error:
        return False
    try:
        message = str(error).upper()
        if "FOREIGN KEY" in message or "FOREIGN_KEY" in message:
            conflicts = _foreign_key_conflicts(con)
            if not conflicts:
                raise _DataRepairNotApplicable("no existing foreign-key orphan rows")
            deleted = _delete_rowids(con, conflicts)
            description = "foreign-key orphan rows"
        else:
            if migration is None or statement is None:
                raise _DataRepairNotApplicable("unique conflict has no migration statement")
            unique_conflict = _unique_index_conflicts(con, statement)
            if unique_conflict is None:
                raise _DataRepairNotApplicable("unique conflict is not a supported index migration")
            table_name, rowids = unique_conflict
            if not rowids:
                raise _DataRepairNotApplicable("unique index has no identifiable duplicate rows")
            deleted = _delete_rowids(con, [(table_name, rowid) for rowid in rowids])
            description = f"duplicate rows from {table_name}"

        if deleted <= 0:
            raise _DataRepairNotApplicable("no rows were deleted")
        con.execute(f"RELEASE {savepoint}")
        print(f"[migrate] repaired data consistency: deleted {deleted} {description}")
        return True
    except (_DataRepairNotApplicable, sqlite3.Error, ValueError):
        _rollback_repair_savepoint(con, savepoint)
        return False


def apply_migrations(
    con: sqlite3.Connection,
    *,
    target: int,
    migrations: Sequence[Migration] | None = None,
    repair_data_consistency: DataConsistencyRepair | None = None,
    _repair_attempt: int = 0,
) -> int:
    """Apply missing migrations through ``target`` atomically."""

    migrations = list(migrations or load_migrations())
    _configure_write_connection(con)
    _ensure_migrations_table(con)
    current = verify_recorded_checksums(con, migrations, target)

    for migration in migrations:
        if migration.version > target or migration.version <= current:
            continue
        print(f"[migrate] applying {migration.name} ...")
        failing_statement: str | None = None
        try:
            con.execute("BEGIN IMMEDIATE")
            for statement in iter_sql_statements(migration.sql):
                failing_statement = statement
                con.execute(statement)
            con.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            _check_integrity(con)
            con.commit()
            current = migration.version
            print(f"[migrate] applied {migration.name} OK")
        except Exception as exc:
            try:
                con.rollback()
            finally:
                # The migration record and every statement in the failed
                # migration are in the same transaction.  Re-checking here is
                # useful for diagnostics and catches accidental non-transaction
                # statements in future migrations.
                pass
            if (
                repair_data_consistency is not None
                and _repair_attempt < _MAX_DATA_CONSISTENCY_REPAIRS
                and _is_data_consistency_failure(exc)
                and repair_data_consistency(
                    con,
                    migration,
                    failing_statement,
                    exc,
                )
            ):
                print(f"[migrate] retrying {migration.name} after data repair")
                return apply_migrations(
                    con,
                    target=target,
                    migrations=migrations,
                    repair_data_consistency=repair_data_consistency,
                    _repair_attempt=_repair_attempt + 1,
                )
            print(f"[migrate] FAILED {migration.name}: {exc}", file=sys.stderr)
            return 3

    try:
        _check_integrity(con)
    except Exception as exc:
        if (
            repair_data_consistency is not None
            and _repair_attempt < _MAX_DATA_CONSISTENCY_REPAIRS
            and _is_data_consistency_failure(exc)
            and repair_data_consistency(con, None, None, exc)
        ):
            print("[migrate] retrying post-check after data repair")
            return apply_migrations(
                con,
                target=target,
                migrations=migrations,
                repair_data_consistency=repair_data_consistency,
                _repair_attempt=_repair_attempt + 1,
            )
        print(f"[migrate] post-check failed: {exc}", file=sys.stderr)
        return 4
    return 0


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    _configure_read_connection(con)
    return con


def check_database_file(
    path: Path,
    *,
    target: int,
    migrations: Sequence[Migration],
) -> int:
    """Validate an existing DB without creating tables or changing PRAGMAs."""

    if not path.exists():
        return 0
    con = _readonly_connection(path)
    try:
        verify_recorded_checksums(con, migrations, target)
        _check_integrity(con)
        return 0
    finally:
        con.close()


def _temp_check(target: int, migrations: Sequence[Migration]) -> int:
    with tempfile.TemporaryDirectory(prefix="wordtts-migration-check-") as tmp:
        path = Path(tmp) / "check.db"
        con = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        try:
            result = apply_migrations(con, target=target, migrations=migrations)
        finally:
            con.close()
        if result != 0:
            return result
        print(f"[migrate] temp profile through 000{target} OK")
        return 0


@contextmanager
def _migration_lock(db_path: Path) -> Iterable[None]:
    # The lock is shared by the CLI migrator and the long-lived application
    # database owner.  Keeping one lock name prevents a second backend from
    # opening the same workflow data directory between migrations.
    lock_path = db_path.parent / ".workflow.lock"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows fallback is best effort.
        yield
        return

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError("another migration is running (lock held)") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _open_write_database(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=5, isolation_level=None)


def backup_database(path: Path, *, destination: Path | None = None) -> Path | None:
    """Create an atomic SQLite backup without changing the source database."""

    path = Path(path).expanduser().resolve()
    if not path.exists():
        return None
    if destination is None:
        base_target = path.with_name(
            f"{path.name}.pre-migration-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
        )
        target = base_target
        suffix = 1
        # Never replace an earlier backup when two upgrades happen in the
        # same second or a previous process used the same PID.  The backup is
        # the rollback point, so preserving every point-in-time copy matters.
        while target.exists():
            target = Path(f"{base_target}-{suffix}")
            suffix += 1
    else:
        target = Path(destination)
    target = Path(target).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target == path:
        raise MigrationError("migration backup destination must differ from database")
    temporary = Path(tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))[1])
    try:
        source = _readonly_connection(path)
        backup = sqlite3.connect(str(temporary), timeout=5, isolation_level=None)
        try:
            source.backup(backup)
            backup.commit()
        finally:
            backup.close()
            source.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        try:
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is platform-dependent; the database and temp
            # file fsyncs above remain mandatory evidence for the supported
            # local runtime.
            pass
        return target
    except sqlite3.Error as exc:
        raise MigrationError(f"cannot create SQLite backup for {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def prepare_migration_backup(
    path: Path,
    *,
    target: int,
    migrations: Sequence[Migration],
) -> Path | None:
    """Back up an existing database only when an upgrade is actually needed.

    The side-effect journal is copied beside the SQLite backup when present;
    its absence is valid for a newly initialized database.
    """

    path = Path(path).expanduser().resolve()
    if not path.exists():
        return None
    con = _readonly_connection(path)
    try:
        current = verify_recorded_checksums(con, migrations, target)
    finally:
        con.close()
    if current >= target:
        return None
    backup_path = backup_database(path)
    if backup_path is None:
        return None
    journal = path.parent / "side_effect_intents.jsonl"
    if journal.exists():
        journal_backup = Path(f"{backup_path}.side_effect_intents.jsonl")
        temporary = Path(tempfile.mkstemp(prefix=f".{journal_backup.name}.", suffix=".tmp", dir=str(journal_backup.parent))[1])
        try:
            shutil.copyfile(journal, temporary)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, journal_backup)
            try:
                os.chmod(journal_backup, 0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
    return backup_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wordTTS migration runner")
    parser.add_argument("--check", action="store_true", help="read-only check plus a temporary clean migration")
    parser.add_argument("--db", type=str, default=None, help="database path (default WORDTTS_DB_PATH/app data)")
    parser.add_argument("--up-to", type=int, default=None, help="explicit target migration version, e.g. 0004")
    parser.add_argument("--profile", choices=("2a", "full"), default=DEFAULT_PROFILE, help="2a stops at 0004; full applies all migrations")
    args = parser.parse_args(argv)

    try:
        migrations = load_migrations()
        target = resolve_target(migrations, up_to=args.up_to, profile=args.profile)
        db_path = Path(args.db) if args.db else _db_path()

        if args.check:
            # Without an explicit --db, never inspect or mutate the user's
            # runtime DB just because it happens to exist.  An upgrade check
            # is opt-in via --db and remains strictly read-only.
            if args.db:
                result = check_database_file(db_path, target=target, migrations=migrations)
                if result != 0:
                    return result
            return _temp_check(target, migrations)

        with _migration_lock(db_path):
            backup_path = prepare_migration_backup(db_path, target=target, migrations=migrations)
            if backup_path is not None:
                print(f"[migrate] pre-upgrade backup: {backup_path}")
            repair_backup_created = backup_path is not None

            def repair_callback(
                con: sqlite3.Connection,
                migration: Migration | None,
                statement: str | None,
                error: Exception,
            ) -> bool:
                nonlocal repair_backup_created
                if not repair_backup_created:
                    repair_backup = backup_database(db_path)
                    if repair_backup is None:
                        raise MigrationError(
                            "cannot create backup before repairing data consistency"
                        )
                    repair_backup_created = True
                    print(f"[migrate] pre-repair backup: {repair_backup}")
                return repair_data_consistency(con, migration, statement, error)

            con = _open_write_database(db_path)
            try:
                return apply_migrations(
                    con,
                    target=target,
                    migrations=migrations,
                    repair_data_consistency=repair_callback,
                )
            finally:
                con.close()
    except MigrationError as exc:
        print(f"[migrate] MIGRATION_ERROR: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"[migrate] SQLITE_ERROR: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"[migrate] IO_ERROR: {exc}", file=sys.stderr)
        return 7


if __name__ == "__main__":
    sys.exit(main())
