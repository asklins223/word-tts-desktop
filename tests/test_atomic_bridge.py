"""旧 UI 命令桥接测试：workflow 主链路解析 → 原子模型同步落库。"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from workflow.database import WorkflowDatabase

from application.atomic_bridge import _explicit_type_code, bridge_parse_to_atomic_model

DOC = Path(__file__).resolve().parent.parent / (
    "examples/documents/7上-U2-信息获取.docx")


class AtomicBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = WorkflowDatabase(
            Path(self.temp.name) / "workflow.db", profile="full")
        self.database.initialize()
        self.addCleanup(self.database.close)

    def _bridge(self):
        return bridge_parse_to_atomic_model(
            self.database,
            source_path=str(DOC),
            filename=DOC.name,
            source_sha256="a" * 64,
            workflow_id="wf-bridge-1",
            now="2026-08-30T00:00:00+00:00",
        )

    def test_bridge_persists_atomic_model_and_session(self):
        result = self._bridge()
        self.assertTrue(result["bridged"])
        self.assertEqual(result["question_count"], 10)
        self.assertEqual(result["stimulus_count"], 4)
        self.assertEqual(result["audio_task_count"], 4)
        self.assertTrue(result["plan_id"].startswith("plan:document-revision:"))
        con = self.database.connect(write=True)
        try:
            sessions = con.execute(
                """SELECT source_classification, import_state
                   FROM legacy_execution_sessions"""
            ).fetchall()
            self.assertEqual([tuple(r) for r in sessions],
                             [("LEGACY_BRIDGED", "IMPORTED")])
        finally:
            con.close()

    def test_bridge_is_idempotent(self):
        first = self._bridge()
        second = self._bridge()
        self.assertEqual(first["document_revision_id"],
                         second["document_revision_id"])
        self.assertEqual(first["plan_id"], second["plan_id"])
        con = self.database.connect(write=True)
        try:
            counts = {
                table: con.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("question_items", "operation_tasks",
                              "legacy_execution_sessions")
            }
        finally:
            con.close()
        self.assertEqual(counts, {"question_items": 10,
                                  "operation_tasks": 4,
                                  "legacy_execution_sessions": 1})

    def test_bridge_failure_does_not_raise(self):
        result = bridge_parse_to_atomic_model(
            self.database,
            source_path="/nonexistent/doc.docx",
            filename="doc.docx",
            source_sha256="a" * 64,
            workflow_id="wf-bridge-2",
        )
        self.assertFalse(result["bridged"])
        self.assertIn("error", result)

    def test_bridge_is_explicitly_disabled_before_v0006_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.db", profile="2a")
            database.initialize()
            try:
                result = bridge_parse_to_atomic_model(
                    database,
                    source_path="/nonexistent/doc.docx",
                    filename="doc.docx",
                    source_sha256="a" * 64,
                    workflow_id="wf-2a-bridge",
                )
            finally:
                database.close()
        self.assertFalse(result["bridged"])
        self.assertEqual(result["reason"], "atomic model schema is not installed")
        self.assertIn("v0006", result["error"])

    def test_mixed_document_does_not_use_first_candidate_as_explicit_type(self):
        results = [
            {"doc_type": "课文跟读"},
            {"doc_type": "词汇"},
        ]
        self.assertIsNone(_explicit_type_code(results, "混合内容.docx"))
        self.assertEqual(
            _explicit_type_code(results, "课文跟读.docx"),
            "text_reading",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
