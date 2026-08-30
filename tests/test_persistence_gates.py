from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.verify_backup import verify
from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.domain import content_hash
from workflow.engine import WorkflowEngine
from workflow.fake_provider import FakeProvider
from workflow.repositories import WorkflowRepository
from workflow.side_effect_log import SideEffectIntentLog, SideEffectLogError


class PersistenceGatesTests(unittest.TestCase):
    def _create_workflow(self, root: Path, *, profile: str = "2a") -> tuple[WorkflowDatabase, WorkflowRepository, str]:
        database = WorkflowDatabase(root / "workflow.db", profile=profile)
        database.initialize()
        repository = WorkflowRepository(database)
        workflow = repository.create_workflow("tts", {"mode": "composite_cut"})
        repository.create_item(
            workflow.workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="hello",
            item_identity_key="lesson:0",
            role="default",
            voice_key="fake",
        )
        return database, repository, workflow.workflow_id

    def test_side_effect_journal_failure_blocks_tts_before_sqlite_or_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-persistence-gate-") as tmp:
            root = Path(tmp)
            database, repository, workflow_id = self._create_workflow(root)
            item = repository.list_items(workflow_id)[0]
            plan = [{
                "ordinal": 0,
                "item_id": item["item_id"],
                "identity_key": item["item_identity_key"],
                "content": item["normalized_content"],
                "content_hash": item["content_hash"],
                "role": item["role"],
                "voice_key": item["voice_key"],
            }]
            intent_log = repository.intent_log
            with patch.object(intent_log, "_append", side_effect=SideEffectLogError("simulated fsync failure")):
                with self.assertRaises(SideEffectLogError):
                    repository.prepare_tts_plan(
                        workflow_id,
                        provider="fake",
                        provider_account_scope="fake-account",
                        unit_type="composite",
                        tts_submission_key="fsync-failure-key",
                        ordered_plan=plan,
                        input_hash=content_hash(plan),
                        submission_profile_hash=content_hash({"profile": "test"}),
                    )
            with database.read_transaction() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_submissions").fetchone()[0], 0)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM side_effect_intents").fetchone()[0], 0)
            self.assertEqual(intent_log.read_entries(), [])
            database.close()

    def test_side_effect_journal_repairs_only_a_torn_final_line(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-journal-tail-") as tmp:
            path = Path(tmp) / "side_effect_intents.jsonl"
            journal = SideEffectIntentLog(path)
            journal.record(
                operation_namespace="tts",
                operation_key="complete-line",
                payload={"value": "safe"},
                intent_id="intent-complete",
            )
            with path.open("ab") as target:
                target.write(b'{"entry_type":"state","intent_id":"torn')

            entries = journal.read_entries()
            self.assertEqual([entry["intent_id"] for entry in entries], ["intent-complete"])
            self.assertTrue(path.read_bytes().endswith(b"\n"))

            # A malformed line with a durable newline is not a crash-tail and
            # must still fail closed rather than being silently discarded.
            with path.open("ab") as target:
                target.write(b"not-json\n")
            with self.assertRaises(SideEffectLogError):
                journal.read_entries()

    def test_side_effect_journal_preserves_a_complete_legacy_line_without_newline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-journal-legacy-line-") as tmp:
            path = Path(tmp) / "side_effect_intents.jsonl"
            journal = SideEffectIntentLog(path)
            journal.record(
                operation_namespace="tts",
                operation_key="legacy-line",
                payload={"value": "safe"},
                intent_id="intent-legacy",
            )
            path.write_bytes(path.read_bytes().rstrip(b"\n"))

            entries = journal.read_entries()
            self.assertEqual([entry["intent_id"] for entry in entries], ["intent-legacy"])
            self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_side_effect_journal_drops_a_valid_json_but_invalid_schema_tail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-journal-invalid-tail-") as tmp:
            path = Path(tmp) / "side_effect_intents.jsonl"
            journal = SideEffectIntentLog(path)
            journal.record(
                operation_namespace="tts",
                operation_key="complete-line",
                payload={"value": "safe"},
                intent_id="intent-complete",
            )
            with path.open("ab") as target:
                target.write(b'{"a":1}')

            entries = journal.read_entries()
            self.assertEqual([entry["intent_id"] for entry in entries], ["intent-complete"])
            self.assertEqual(path.read_text(encoding="utf-8").count("{\"a\":1}"), 0)

    def test_side_effect_journal_uses_the_windows_lock_backend_when_fcntl_is_unavailable(self) -> None:
        class FakeMsvcrt:
            LK_LOCK = 1
            LK_RLCK = 2
            LK_UNLCK = 3

            def __init__(self) -> None:
                self.calls = []

            def locking(self, descriptor, mode, length) -> None:
                self.calls.append((descriptor, mode, length))

        with tempfile.TemporaryDirectory(prefix="wordtts-journal-lock-") as tmp:
            path = Path(tmp) / "side_effect_intents.jsonl"
            path.touch()
            fake_msvcrt = FakeMsvcrt()
            with path.open("r+b") as target, patch.dict(
                sys.modules, {"fcntl": None, "msvcrt": fake_msvcrt}
            ):
                SideEffectIntentLog._lock_descriptor(target, exclusive=True)
                SideEffectIntentLog._unlock_descriptor(target)

            self.assertEqual([call[1:] for call in fake_msvcrt.calls], [(1, 1), (3, 1)])

    def test_rejected_tts_plan_marks_pretransaction_journal_as_aborted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-journal-abort-") as tmp:
            root = Path(tmp)
            database = WorkflowDatabase(root / "workflow.db", profile="2a")
            database.initialize()
            repository = WorkflowRepository(database)

            with self.assertRaises(Exception):
                repository.prepare_tts_plan(
                    "missing-workflow",
                    provider="fake",
                    provider_account_scope="fake-account",
                    unit_type="composite",
                    tts_submission_key="rejected-plan-key",
                    ordered_plan=[{"item_id": "missing-item"}],
                    input_hash="a" * 64,
                    submission_profile_hash="b" * 64,
                )

            entries = repository.intent_log.read_entries()
            self.assertEqual([entry["state"] for entry in entries], ["RECORDED", "ABORTED"])
            with database.read_transaction() as con:
                self.assertEqual(
                    repository.intent_log.verify_against_rows(
                        con.execute("SELECT * FROM side_effect_intents").fetchall()
                    ),
                    [],
                )
            database.close()

    def test_journal_is_redacted_and_backup_verifier_compares_db_and_file_facts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-backup-gate-") as tmp:
            root = Path(tmp)
            database, repository, workflow_id = self._create_workflow(root)
            result = WorkflowEngine(repository, ArtifactStore(root / "artifacts")).run_tts(workflow_id, FakeProvider())
            self.assertEqual(result.status, "SUCCEEDED")
            database.close()

            journal_path = root / "side_effect_intents.jsonl"
            raw_journal = journal_path.read_text(encoding="utf-8")
            self.assertNotIn("fake-audio", raw_journal)
            self.assertNotIn("password", raw_journal.lower())
            self.assertNotIn("hello", raw_journal)

            backup_root = root / "backup"
            backup_root.mkdir()
            backup_db = backup_root / "workflow.db"
            backup_journal = backup_root / "side_effect_intents.jsonl"
            shutil.copy2(root / "workflow.db", backup_db)
            shutil.copy2(journal_path, backup_journal)
            checked = verify(
                root / "workflow.db",
                backup=backup_db,
                intent_log=journal_path,
                backup_intent_log=backup_journal,
                profile="2a",
            )
            self.assertTrue(checked["ok"], checked["errors"])
            self.assertEqual(checked["source"]["schema_version"], 4)

            # A file-side state transition without the matching SQLite state
            # must stop restore promotion instead of being silently accepted.
            journal = SideEffectIntentLog(journal_path)
            journal.mark(
                operation_namespace="tts",
                operation_key=next(
                    entry["operation_key"] for entry in journal.read_entries()
                    if entry["entry_type"] == "intent"
                ),
                state="NEEDS_RECONCILE",
            )
            inconsistent = verify(
                root / "workflow.db",
                intent_log=journal_path,
                profile="2a",
            )
            self.assertFalse(inconsistent["ok"])
            self.assertTrue(any("journal state mismatch" in error for error in inconsistent["errors"]))

    def test_rejected_tts_failure_keeps_sqlite_and_journal_states_aligned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-rejected-backup-gate-") as tmp:
            root = Path(tmp)
            database, repository, workflow_id = self._create_workflow(root)
            provider = FakeProvider()
            provider.fail_mode = "before"
            result = WorkflowEngine(
                repository,
                ArtifactStore(root / "artifacts"),
            ).run_tts(workflow_id, provider)
            self.assertEqual(result.status, "WAITING_RETRY")
            with database.read_transaction() as con:
                self.assertEqual(
                    con.execute("SELECT state FROM side_effect_intents").fetchone()[0],
                    "ARCHIVED",
                )
            database.close()

            checked = verify(
                root / "workflow.db",
                intent_log=root / "side_effect_intents.jsonl",
                profile="2a",
            )
            self.assertTrue(checked["ok"], checked["errors"])


if __name__ == "__main__":
    unittest.main()
