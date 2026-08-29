#!/usr/bin/env python3
"""Read-only verification for a workflow database and an optional backup.

The command never calls the migration runner in write mode.  It validates
SQLite integrity, migration checksums, side-effect journal coverage, and (when
a backup is supplied) compares schema and content digests for every user
table.  It is intended to be a release/restore gate, not a repair command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from db.migration_runner import MigrationError, load_migrations, resolve_target, verify_recorded_checksums
from workflow.side_effect_log import SideEffectIntentLog, SideEffectLogError


class BackupVerificationError(RuntimeError):
    pass


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupVerificationError(f"database does not exist: {path}")
    try:
        con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA foreign_keys=ON")
        return con
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"cannot open database read-only: {path}") from exc


def _user_tables(con: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _schema_signature(con: sqlite3.Connection, table: str) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in con.execute(f'PRAGMA table_info("{table}")').fetchall())


def _table_digest(con: sqlite3.Connection, table: str) -> tuple[int, str]:
    columns = [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if not columns:
        raise BackupVerificationError(f"table has no readable columns: {table}")
    digest = hashlib.sha256()
    count = 0
    query = f'SELECT * FROM "{table}"'
    for row in con.execute(query):
        encoded = json.dumps([row[column] for column in columns], ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def _migration_rows(con: sqlite3.Connection) -> list[tuple[int, str, str]]:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if exists is None:
        raise BackupVerificationError("schema_migrations table is missing")
    return [tuple(row) for row in con.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()]


def _verify_one(path: Path, *, profile: str) -> dict[str, Any]:
    migrations = load_migrations()
    con = _read_only(path)
    try:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [tuple(row) for row in con.execute("PRAGMA foreign_key_check").fetchall()]
        if integrity != "ok":
            raise BackupVerificationError(f"integrity_check failed for {path}: {integrity}")
        if foreign_keys:
            raise BackupVerificationError(f"foreign_key_check failed for {path}: {foreign_keys[:3]}")
        target = resolve_target(migrations, profile="2a" if profile == "2a" else "full") if profile != "auto" else migrations[-1].version
        current = verify_recorded_checksums(con, migrations, target=target)
        rows = _migration_rows(con)
        if not rows:
            raise BackupVerificationError(f"database has no applied migrations: {path}")
        if profile == "2a" and current != 4:
            raise BackupVerificationError(f"database is not a 2A schema: v{current}")
        if profile == "full" and current != migrations[-1].version:
            raise BackupVerificationError(f"database is not a full schema: v{current}")
        tables = _user_tables(con)
        table_stats = {}
        table_schemas = {}
        for table in tables:
            count, digest = _table_digest(con, table)
            table_stats[table] = {"rows": count, "sha256": digest}
            table_schemas[table] = _schema_signature(con, table)
        intent_rows = [dict(row) for row in con.execute(
            "SELECT intent_id, operation_namespace, operation_key, payload_hash, state FROM side_effect_intents"
        ).fetchall()] if "side_effect_intents" in tables else []
        return {
            "path": str(path),
            "schema_version": current,
            "migrations": rows,
            "tables": table_stats,
            "table_schemas": table_schemas,
            "side_effect_intents": intent_rows,
        }
    finally:
        con.close()


def _compare_databases(source: dict[str, Any], backup: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if source["schema_version"] != backup["schema_version"]:
        errors.append("source and backup schema versions differ")
    if source["migrations"] != backup["migrations"]:
        errors.append("source and backup migration records differ")
    source_tables = source["tables"]
    backup_tables = backup["tables"]
    if set(source_tables) != set(backup_tables):
        errors.append("source and backup user tables differ")
    for table in sorted(set(source_tables) & set(backup_tables)):
        if source["table_schemas"].get(table) != backup["table_schemas"].get(table):
            errors.append(f"source and backup schema differs: {table}")
        if source_tables[table] != backup_tables[table]:
            errors.append(f"source and backup content differs: {table}")
    return errors


def verify(
    database: str | Path,
    *,
    backup: str | Path | None = None,
    intent_log: str | Path | None = None,
    backup_intent_log: str | Path | None = None,
    profile: str = "auto",
) -> dict[str, Any]:
    source = _verify_one(Path(database).expanduser().resolve(), profile=profile)
    errors: list[str] = []
    journal_path = Path(intent_log).expanduser().resolve() if intent_log else Path(database).expanduser().resolve().parent / "side_effect_intents.jsonl"
    try:
        journal = SideEffectIntentLog(journal_path, create_parent=False)
        errors.extend(journal.verify_against_rows(source["side_effect_intents"]))
        source["journal_entries"] = len(journal.read_entries())
        source["journal_path"] = str(journal_path)
    except SideEffectLogError as exc:
        errors.append(str(exc))

    result: dict[str, Any] = {"ok": not errors, "errors": errors, "source": source}
    if backup is not None:
        backup_data = _verify_one(Path(backup).expanduser().resolve(), profile=profile)
        result["backup"] = backup_data
        errors.extend(_compare_databases(source, backup_data))
        backup_journal_path = (
            Path(backup_intent_log).expanduser().resolve()
            if backup_intent_log
            else (Path(backup).expanduser().resolve().parent / "side_effect_intents.jsonl")
        )
        if backup_journal_path.exists() or source["side_effect_intents"]:
            try:
                backup_journal = SideEffectIntentLog(backup_journal_path, create_parent=False)
                errors.extend(backup_journal.verify_against_rows(backup_data["side_effect_intents"]))
                result["backup"]["journal_entries"] = len(backup_journal.read_entries())
                if backup_journal_path.exists():
                    source_entries = SideEffectIntentLog(journal_path, create_parent=False).read_entries()
                    backup_entries = backup_journal.read_entries()
                    if source_entries != backup_entries:
                        errors.append("source and backup side-effect journals differ")
            except SideEffectLogError as exc:
                errors.append(str(exc))
        result["ok"] = not errors
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="workflow SQLite database")
    parser.add_argument("--backup", help="optional backup SQLite database")
    parser.add_argument("--intent-log", help="side-effect journal for the source database")
    parser.add_argument("--backup-intent-log", help="side-effect journal for the backup")
    parser.add_argument("--profile", choices=("auto", "2a", "full"), default="auto")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        result = verify(
            args.database,
            backup=args.backup,
            intent_log=args.intent_log,
            backup_intent_log=args.backup_intent_log,
            profile=args.profile,
        )
    except (BackupVerificationError, MigrationError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "errors": [str(exc)]}
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print("backup verification: " + ("PASS" if result.get("ok") else "FAIL"))
        for error in result.get("errors", []):
            print(f"- {error}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
