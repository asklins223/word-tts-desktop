"""Regression tests for the workflow schema gate."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from db.migration_runner import (
    Migration,
    apply_migrations,
    backup_database,
    prepare_migration_backup,
    check_database_file,
    iter_sql_statements,
    load_migrations,
    resolve_target,
)
from workflow.database import WorkflowDatabase


class MigrationRunnerTests(unittest.TestCase):
    def test_profiles_have_explicit_boundaries(self) -> None:
        migrations = load_migrations()
        self.assertEqual(resolve_target(migrations, profile="2a"), 4)
        self.assertEqual(resolve_target(migrations, profile="full"), 7)
        self.assertEqual(resolve_target(migrations, up_to=4), 4)

    def test_clean_2a_database_does_not_apply_external_schema(self) -> None:
        migrations = load_migrations()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "workflow.db"
            con = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                self.assertEqual(
                    apply_migrations(
                        con,
                        target=resolve_target(migrations, profile="2a"),
                        migrations=migrations,
                    ),
                    0,
                )
            finally:
                con.close()

            ro = sqlite3.connect(str(db_path))
            try:
                tables = {
                    row[0]
                    for row in ro.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                ro.close()
            self.assertNotIn("external_records", tables)
            self.assertIn("source_import_generations", tables)

    def test_statement_failure_rolls_back_every_statement_in_migration(self) -> None:
        sql = """
        CREATE TABLE partial_side_effect (id INTEGER PRIMARY KEY);
        INSERT INTO partial_side_effect(id) VALUES (1);
        THIS IS INTENTIONALLY INVALID;
        """
        migration = Migration(
            version=1,
            name="0001_failure.sql",
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "failure.db"
            con = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                self.assertEqual(apply_migrations(con, target=1, migrations=[migration]), 3)
                self.assertIsNone(
                    con.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='partial_side_effect'"
                    ).fetchone()
                )
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                    0,
                )
            finally:
                con.close()

    def test_upgrade_backup_preserves_pre_upgrade_schema_and_side_effect_journal(self) -> None:
        migrations = load_migrations()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "workflow.db"
            con = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                self.assertEqual(apply_migrations(con, target=4, migrations=migrations), 0)
            finally:
                con.close()
            journal = root / "side_effect_intents.jsonl"
            journal.write_text('{"operation_namespace":"tts","state":"RECORDED"}\n', encoding="utf-8")

            backup = prepare_migration_backup(db_path, target=5, migrations=migrations)
            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertTrue(backup.is_file())
            self.assertTrue(Path(f"{backup}.side_effect_intents.jsonl").is_file())

            backup_con = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
            try:
                self.assertEqual(
                    backup_con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                    4,
                )
            finally:
                backup_con.close()

    def test_default_upgrade_backups_never_overwrite_an_earlier_point(self) -> None:
        migrations = load_migrations()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "workflow.db"
            con = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                self.assertEqual(apply_migrations(con, target=4, migrations=migrations), 0)
            finally:
                con.close()

            first = backup_database(db_path)
            second = backup_database(db_path)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_check_database_file_is_read_only(self) -> None:
        migrations = load_migrations()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "workflow.db"
            con = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                self.assertEqual(
                    apply_migrations(con, target=4, migrations=migrations),
                    0,
                )
            finally:
                con.close()
            before = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
            self.assertEqual(
                check_database_file(db_path, target=4, migrations=migrations),
                0,
            )
            after = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
            self.assertEqual(before, after)

    def test_sql_parser_keeps_trigger_bodies_atomic(self) -> None:
        migration = next(
            item for item in load_migrations() if item.version == 4
        )
        statements = list(iter_sql_statements(migration.sql))
        self.assertGreater(len(statements), 10)
        self.assertTrue(any("CREATE TRIGGER" in statement for statement in statements))
        self.assertTrue(all(statement.rstrip().endswith(";") for statement in statements))

    def test_long_lived_database_owner_blocks_second_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "workflow.db"
            first = WorkflowDatabase(db_path)
            first.initialize()
            second = WorkflowDatabase(db_path)
            try:
                with self.assertRaises(RuntimeError):
                    second.initialize()
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
