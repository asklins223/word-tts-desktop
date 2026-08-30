"""v0007 外部目标关联表测试（D-EXT-001：多态目标 trigger 校验与回填列）。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from workflow.database import WorkflowDatabase
from workflow.external import ExternalRecordService
from workflow.fake_external import FakeExternalAdapter
from workflow.repositories import WorkflowRepository

from question_model import sync_sub_type_registry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExternalTargetLinksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wordtts-ext-targets-")
        root = Path(self.temp.name)
        self.database = WorkflowDatabase(root / "workflow.db", profile="full")
        self.database.initialize()
        self.addCleanup(self.database.close)
        self.addCleanup(self.temp.cleanup)
        self.con = self.database.connect(write=True)
        sync_sub_type_registry(self.con)
        self.con.commit()
        self._seed()
        self.repository = WorkflowRepository(self.database)
        self.service = ExternalRecordService(
            self.database, intent_log=self.repository.intent_log
        )
        self.adapter = FakeExternalAdapter()
        self.workflow = self.repository.create_workflow(
            "external-sync", {"mapping_version": "v1"}
        )

    def _seed(self):
        now = _now()
        self.con.execute(
            """INSERT INTO source_documents VALUES
               ('sd1', 'doc1', 'local', 'other', NULL, ?)""",
            (now,),
        )
        self.con.execute(
            """INSERT INTO document_revisions VALUES
               ('dr1', 'sd1', NULL, 'h1', 1, 1, ?)""",
            (now,),
        )
        self.con.execute(
            """INSERT INTO question_items VALUES
               ('question:doc1:q1', 'sd1', 'info_acquisition',
                'listening_info', NULL, ?)""",
            (now,),
        )
        self.con.execute(
            """INSERT INTO stimuli VALUES
               ('stimulus:doc1:s1', 'sd1', 'listening_info',
                'listening_script', NULL, NULL, ?)""",
            (now,),
        )
        self.con.commit()

    def _ensure_mapping(self) -> str:
        mapping = self.service.ensure_record(
            self.workflow.workflow_id,
            external_system=self.adapter.system,
            account_scope=self.adapter.account_scope,
            business_record_key="business-1",
            mapping_version="v1",
        )
        return mapping["external_record_mapping_id"]

    def _insert_record_target(self, mapping_id: str, kind: str, target_id: str):
        self.con.execute(
            """INSERT INTO external_record_targets VALUES
               (?, ?, ?, ?, NULL, 0, 'PRIMARY', NULL, ?)""",
            (f"rt-{kind}-{target_id}", mapping_id, kind, target_id, _now()),
        )

    def test_valid_question_target_accepted(self) -> None:
        mapping_id = self._ensure_mapping()
        self._insert_record_target(mapping_id, "QUESTION", "question:doc1:q1")
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM external_record_targets"
            ).fetchone()[0],
            1,
        )

    def test_unknown_question_target_rejected(self) -> None:
        mapping_id = self._ensure_mapping()
        with self.assertRaises(Exception):
            self._insert_record_target(mapping_id, "QUESTION", "question:doc1:missing")
        self.con.rollback()

    def test_valid_stimulus_target_accepted(self) -> None:
        mapping_id = self._ensure_mapping()
        self._insert_record_target(mapping_id, "STIMULUS", "stimulus:doc1:s1")

    def test_unknown_stimulus_target_rejected(self) -> None:
        mapping_id = self._ensure_mapping()
        with self.assertRaises(Exception):
            self._insert_record_target(mapping_id, "STIMULUS", "stimulus:doc1:missing")
        self.con.rollback()

    def test_invalid_kind_enum_rejected(self) -> None:
        mapping_id = self._ensure_mapping()
        with self.assertRaises(Exception):
            self._insert_record_target(mapping_id, "CUSTOM", "anything")
        self.con.rollback()

    def test_duplicate_target_link_rejected(self) -> None:
        mapping_id = self._ensure_mapping()
        self._insert_record_target(mapping_id, "QUESTION", "question:doc1:q1")
        with self.assertRaises(Exception):
            self._insert_record_target(mapping_id, "QUESTION", "question:doc1:q1")
        self.con.rollback()

    def test_operation_target_snapshot_unique(self) -> None:
        mapping_id = self._ensure_mapping()
        lease = self.service.acquire_record_lease(mapping_id, "test-owner")
        operation = self.service.prepare_operation(
            self.workflow.workflow_id,
            mapping_id=mapping_id,
            operation_key="sync:business-1",
            payload={"business_record_key": "business-1"},
            mapping_version="v1",
        )
        operation_id = operation["external_operation_id"]
        self.con.execute(
            """INSERT INTO external_operation_targets VALUES
               ('ot1', ?, 'QUESTION', 'question:doc1:q1', NULL, 0,
                'PENDING', NULL, ?)""",
            (operation_id, _now()),
        )
        self.con.commit()
        with self.assertRaises(Exception):
            self.con.execute(
                """INSERT INTO external_operation_targets VALUES
                   ('ot2', ?, 'QUESTION', 'question:doc1:q1', NULL, 0,
                    'PENDING', NULL, ?)""",
                (operation_id, _now()),
            )
        self.con.rollback()

    def test_workflow_step_and_attempt_backfill_columns(self) -> None:
        """v0007 为 external_operations 增加可回填列；历史行允许为空。"""
        mapping_id = self._ensure_mapping()
        operation = self.service.prepare_operation(
            self.workflow.workflow_id,
            mapping_id=mapping_id,
            operation_key="sync:business-1",
            payload={"business_record_key": "business-1"},
            mapping_version="v1",
        )
        operation_id = operation["external_operation_id"]
        row = self.con.execute(
            """SELECT workflow_step_id, attempt_id FROM external_operations
               WHERE external_operation_id = ?""",
            (operation_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (None, None))
        self.con.execute(
            """UPDATE external_operations
               SET workflow_step_id = 'step-1', attempt_id = 'attempt-1'
               WHERE external_operation_id = ?""",
            (operation_id,),
        )
        self.con.commit()
        row = self.con.execute(
            """SELECT workflow_step_id, attempt_id FROM external_operations
               WHERE external_operation_id = ?""",
            (operation_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("step-1", "attempt-1"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
