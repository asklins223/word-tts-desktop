from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.engine import WorkflowEngine
from workflow.fake_provider import FakeProvider
from workflow.providers import ProviderCapabilityError, XunfeiTTSAdapter
from workflow.repositories import ConflictError, NotFoundError, WorkflowRepository
from workflow.security import OneTimeTicketManager, TicketError, TicketExpired
from workflow.side_effect_log import SideEffectLogError
from workflow.source_imports import SourceImportError, SourceImportService


class RecoveryFaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wordtts-recovery-fault-")
        root = Path(self.temp.name)
        self.database = WorkflowDatabase(root / "workflow.db", profile="2a")
        self.database.initialize()
        self.repository = WorkflowRepository(self.database)
        self.artifacts = ArtifactStore(root / "artifacts")

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _workflow_with_items(self, count: int = 2) -> str:
        workflow = self.repository.create_workflow("tts", {"generation_mode": "composite_cut"})
        for sequence in range(count):
            self.repository.create_item(
                workflow.workflow_id,
                item_type="sentence",
                sequence=sequence,
                normalized_content=f"recovery item {sequence}",
                item_identity_key=f"recovery:{sequence}",
                role="default",
                voice_key="fake",
            )
        return workflow.workflow_id

    def test_success_terminalizes_all_control_planes_and_archive_retains_facts(self) -> None:
        workflow_id = self._workflow_with_items()
        result = WorkflowEngine(self.repository, self.artifacts).run_tts(workflow_id, FakeProvider())
        snapshot = self.repository.get_workflow(workflow_id)

        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(
            (snapshot.result_status, snapshot.execution_state, snapshot.control_state, snapshot.cleanup_state),
            ("SUCCEEDED", "TERMINAL", "TERMINATED", "SUCCEEDED"),
        )
        history = self.repository.list_history_records()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["completed"], 2)
        self.assertEqual(history[0]["failed"], 0)
        artifact_ids = self.repository.list_work_unit_artifacts(result.work_unit_id)
        archived = self.repository.archive_workflow(
            workflow_id,
            expected_state_version=snapshot.state_version,
            reason="fault-gate archive check",
        )
        self.assertEqual(archived.status, "CLOSED")
        self.assertEqual(self.repository.list_history_records(), [])
        self.assertEqual(self.repository.list_work_unit_artifacts(result.work_unit_id), artifact_ids)
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM workflow_events WHERE workflow_id=? AND event_type='WORKFLOW_ARCHIVED'", (workflow_id,)).fetchone()[0],
                1,
            )

    def test_old_source_writer_cannot_write_after_abort_or_generation_rotation(self) -> None:
        workflow = self.repository.create_workflow("tts", {})
        imports = SourceImportService(self.database, self.artifacts, ticket_manager=OneTimeTicketManager(max_ttl_seconds=60))
        created = imports.create_import(workflow.workflow_id, metadata={"filename": "fault.docx"})
        grant = imports.acquire_writer(
            created["source_import_id"], 1, expected_state_version=created["state_version"]
        )
        aborted = imports.abort(created["source_import_id"], expected_state_version=grant.state_version)
        self.assertEqual(aborted["status"], "ABORTED")
        with self.assertRaises(SourceImportError) as caught:
            imports.write_generation(
                created["source_import_id"], 1, io.BytesIO(b"late"), grant=grant.token
            )
        self.assertEqual(caught.exception.code, "STALE_ATTEMPT")

        rotated = imports.create_generation(
            created["source_import_id"], expected_state_version=aborted["state_version"]
        )
        self.assertEqual(rotated["generation"], 2)
        self.assertEqual(imports.get_generation(created["source_import_id"], 1)["status"], "ABORTED")

    def test_side_effect_journal_rejects_payload_reuse_before_sqlite_mutation(self) -> None:
        workflow = self.repository.create_workflow("tts", {})
        self.repository.record_side_effect_intent(
            workflow.workflow_id,
            operation_namespace="test",
            operation_key="one-billable-operation",
            payload={"value": "first"},
        )
        with self.assertRaises(SideEffectLogError):
            self.repository.record_side_effect_intent(
                workflow.workflow_id,
                operation_namespace="test",
                operation_key="one-billable-operation",
                payload={"value": "different"},
            )
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM side_effect_intents WHERE operation_namespace='test' AND operation_key='one-billable-operation'"
                ).fetchone()[0],
                1,
            )

    def test_real_provider_is_fail_closed_without_explicit_capability(self) -> None:
        provider = XunfeiTTSAdapter(account_scope="test-account", allow_real=False)
        with self.assertRaises(ProviderCapabilityError) as caught:
            provider.submit("submission", {"plan": [{"content": "must not call"}]})
        self.assertEqual(caught.exception.code, "EXTERNAL_CAPABILITY_REQUIRED")

    def test_ticket_replay_is_rejected_even_when_action_and_resource_match(self) -> None:
        manager = OneTimeTicketManager(clock=lambda: 100.0)
        token, _ = manager.issue(action="artifact", resource_id="artifact-1", audience="renderer", ttl_seconds=30)
        manager.consume(token, action="artifact", resource_id="artifact-1", audience="renderer")
        with self.assertRaises(TicketError):
            manager.consume(token, action="artifact", resource_id="artifact-1", audience="renderer")

    def test_unclaimed_expired_tickets_do_not_exhaust_capacity_and_keep_expiry_signal(self) -> None:
        clock_value = [100.0]
        manager = OneTimeTicketManager(clock=lambda: clock_value[0], max_ttl_seconds=10, max_tickets=16)
        tokens = [
            manager.issue(action="artifact", resource_id=f"artifact-{index}", audience="renderer", ttl_seconds=1)[0]
            for index in range(16)
        ]
        clock_value[0] = 101.5
        replacement, _ = manager.issue(action="artifact", resource_id="replacement", audience="renderer", ttl_seconds=1)
        self.assertTrue(replacement)
        with self.assertRaises(TicketExpired):
            manager.consume(tokens[0], action="artifact", resource_id="artifact-0", audience="renderer")

    def test_archive_requires_terminal_control_state_and_optimistic_version(self) -> None:
        workflow = self._workflow_with_items(1)
        with self.assertRaises(ConflictError):
            self.repository.archive_workflow(workflow, expected_state_version=0)
        result = WorkflowEngine(self.repository, self.artifacts).run_tts(workflow, FakeProvider())
        snapshot = self.repository.get_workflow(workflow)
        with self.assertRaises(ConflictError):
            self.repository.archive_workflow(workflow, expected_state_version=snapshot.state_version - 1)
        self.assertEqual(result.status, "SUCCEEDED")

    def test_unfinished_submission_is_local_retryable_and_deletable(self) -> None:
        workflow_id = self._workflow_with_items(1)
        provider = FakeProvider()
        provider.fail_mode = "after"
        result = WorkflowEngine(self.repository, self.artifacts).run_tts(workflow_id, provider)
        self.assertEqual(result.status, "WAITING_RETRY")

        history = self.repository.list_history_records()
        self.assertTrue(history[0]["can_delete"])
        self.assertIsNone(history[0]["delete_reason"])
        snapshot = self.repository.get_workflow(workflow_id)
        self.repository.delete_workflow(
            workflow_id,
            expected_state_version=snapshot.state_version,
        )
        with self.assertRaises(NotFoundError):
            self.repository.get_workflow(workflow_id)
        intent_key = next(
            key for key in self.repository.intent_log.latest_by_key()
            if key[0] == "tts"
        )
        self.assertEqual(
            self.repository.intent_log.latest_by_key()[intent_key]["state"],
            "ARCHIVED",
        )

    def test_unfinished_rejected_workflow_can_be_deleted(self) -> None:
        workflow_id = self._workflow_with_items(1)
        provider = FakeProvider()
        provider.fail_mode = "before"
        result = WorkflowEngine(self.repository, self.artifacts).run_tts(workflow_id, provider)
        self.assertEqual(result.status, "WAITING_RETRY")
        snapshot = self.repository.get_workflow(workflow_id)
        history = self.repository.list_history_records()
        self.assertTrue(history[0]["can_delete"])

        self.repository.delete_workflow(
            workflow_id,
            expected_state_version=snapshot.state_version,
        )
        with self.assertRaises(NotFoundError):
            self.repository.get_workflow(workflow_id)

    def test_stuck_terminating_workflow_without_active_lease_can_be_deleted(self) -> None:
        workflow_id = self._workflow_with_items(1)
        snapshot = self.repository.get_workflow(workflow_id)
        terminating = self.repository.command(
            workflow_id,
            "cancel",
            snapshot.state_version,
            reason="stuck-terminating-delete",
        )
        self.assertEqual(terminating.control_state, "TERMINATING")
        self.assertTrue(self.repository.list_history_records()[0]["can_delete"])

        self.repository.delete_workflow(
            workflow_id,
            expected_state_version=terminating.state_version,
        )
        with self.assertRaises(NotFoundError):
            self.repository.get_workflow(workflow_id)


if __name__ == "__main__":
    unittest.main()
