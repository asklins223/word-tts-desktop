from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.external import ExternalRecordService, ExternalServiceError, ExternalVerifyMismatch
from workflow.fake_external import FakeExternalAdapter
from workflow.fake_provider import FakeProvider
from workflow.engine import WorkflowEngine
from workflow.recovery import RecoveryService
from workflow.repositories import ConflictError


class ExternalRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wordtts-external-test-")
        root = Path(self.temp.name)
        self.database = WorkflowDatabase(root / "workflow.db", profile="full")
        self.database.initialize()
        from workflow.repositories import WorkflowRepository

        self.repository = WorkflowRepository(self.database)
        self.service = ExternalRecordService(self.database, intent_log=self.repository.intent_log)
        self.adapter = FakeExternalAdapter()
        self.workflow = self.repository.create_workflow("external-sync", {"mapping_version": "v1"})
        self.item_id = self.repository.create_item(
            self.workflow.workflow_id,
            item_type="record",
            sequence=0,
            normalized_content="hello",
            item_identity_key="record:0",
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _operation(self, *, payload: dict | None = None):
        mapping = self.service.ensure_record(
            self.workflow.workflow_id,
            external_system=self.adapter.system,
            account_scope=self.adapter.account_scope,
            business_record_key="business-1",
            mapping_version="v1",
            item_id=self.item_id,
        )
        lease = self.service.acquire_record_lease(mapping["external_record_mapping_id"], "test-owner")
        operation = self.service.prepare_operation(
            self.workflow.workflow_id,
            mapping_id=mapping["external_record_mapping_id"],
            operation_key="sync:business-1",
            payload=payload or {"business_record_key": "business-1", "value": "hello"},
            mapping_version="v1",
            item_id=self.item_id,
        )
        return mapping, lease, operation

    def test_submit_verify_is_idempotent_and_persists_bindings(self) -> None:
        mapping, lease, operation = self._operation()
        started = self.service.begin_operation(operation["external_operation_id"], lease)
        self.assertEqual(started["side_effect_state"], "IN_FLIGHT")
        payload = {"business_record_key": "business-1", "value": "hello"}
        observed = self.service.submit_operation(operation["external_operation_id"], lease, self.adapter, payload)
        repeated_observed = self.service.record_submission(
            operation["external_operation_id"],
            lease,
            {
                "external_record_id": observed["receipt"]["external_record_id"],
                "canonical_key": observed["receipt"]["canonical_key"],
                "summary": {"attempt": "same-receipt"},
            },
        )
        self.assertEqual(repeated_observed["receipt"], observed["receipt"])
        with self.assertRaises(ConflictError) as conflict:
            self.service.record_submission(
                operation["external_operation_id"],
                lease,
                {
                    "external_record_id": "different-external-id",
                    "canonical_key": observed["receipt"]["canonical_key"],
                },
            )
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        journal_entry = self.repository.intent_log.latest_by_key()[
            ("external", f"{mapping['external_record_mapping_id']}:{operation['external_operation_key']}")
        ]
        self.assertEqual(journal_entry["state"], "COMMITTED")
        verified = self.service.verify_operation(operation["external_operation_id"], lease, self.adapter, payload)

        self.assertEqual(verified["side_effect_state"], "CONFIRMED")
        self.assertEqual(verified["receipt"]["external_record_id"], observed["receipt"]["external_record_id"])
        self.assertEqual(self.adapter.submit_calls, 1)
        repeated = self.service.prepare_operation(
            self.workflow.workflow_id,
            mapping_id=mapping["external_record_mapping_id"],
            operation_key="sync:business-1",
            payload=payload,
            mapping_version="v1",
            item_id=self.item_id,
        )
        self.assertEqual(repeated["external_operation_id"], operation["external_operation_id"])
        with self.database.read_transaction() as con:
            states = con.execute(
                "SELECT side_effect_state FROM external_operations WHERE external_operation_id=?",
                (operation["external_operation_id"],),
            ).fetchone()[0]
            bindings = con.execute(
                "SELECT relation_type FROM external_record_bindings WHERE external_operation_id=? ORDER BY relation_type",
                (operation["external_operation_id"],),
            ).fetchall()
            intent = con.execute(
                "SELECT state FROM side_effect_intents WHERE operation_namespace='external'"
            ).fetchone()[0]
        self.assertEqual(states, "CONFIRMED")
        self.assertEqual([row[0] for row in bindings], ["CREATED", "TOUCHED", "VERIFIED"])
        self.assertEqual(intent, "ARCHIVED")
        journal = self.repository.intent_log.path.read_text(encoding="utf-8")
        self.assertNotIn("hello", journal)

    def test_active_record_rejects_a_different_operation_key(self) -> None:
        mapping, _lease, operation = self._operation()
        with self.assertRaises(ConflictError) as context:
            self.service.prepare_operation(
                self.workflow.workflow_id,
                mapping_id=mapping["external_record_mapping_id"],
                operation_key="sync:business-1:parallel",
                payload={"business_record_key": "business-1", "value": "different"},
                mapping_version="v1",
                item_id=self.item_id,
            )
        self.assertEqual(context.exception.code, "EXTERNAL_OPERATION_ACTIVE")
        self.assertEqual(
            context.exception.details["external_operation_id"],
            operation["external_operation_id"],
        )
        self.assertEqual(
            {(entry["operation_namespace"], entry["operation_key"]) for entry in self.repository.intent_log.read_entries()},
            {("external", f"{mapping['external_record_mapping_id']}:{operation['external_operation_key']}")},
        )

    def test_expired_record_lease_is_rejected_before_external_query(self) -> None:
        _mapping, lease, operation = self._operation()
        self.service.begin_operation(operation["external_operation_id"], lease)
        self.service.release_record_lease(lease)
        with self.assertRaises(ConflictError):
            self.service.reconcile(operation["external_operation_id"], lease, self.adapter)
        self.assertEqual(self.adapter.query_calls, 0)

    def test_ambiguous_reconcile_queries_without_resubmitting(self) -> None:
        _mapping, lease, operation = self._operation()
        self.service.begin_operation(operation["external_operation_id"], lease)
        self.adapter.fail_mode = "after"
        payload = {"business_record_key": "business-1", "value": "hello"}
        with self.assertRaises(RuntimeError):
            self.adapter.submit(operation["external_operation_key"], payload)
        self.service.mark_ambiguous(operation["external_operation_id"], lease, error_code="SUBMISSION_AMBIGUOUS")
        result = self.service.reconcile(operation["external_operation_id"], lease, self.adapter)
        self.assertEqual(result["side_effect_state"], "CONFIRMED")
        self.assertEqual(self.adapter.submit_calls, 1)
        self.assertGreaterEqual(self.adapter.query_calls, 1)

    def test_external_adapter_calls_renew_the_record_lease_while_blocking(self) -> None:
        _mapping, lease, operation = self._operation()
        self.service.begin_operation(operation["external_operation_id"], lease)

        class SlowAdapter(FakeExternalAdapter):
            def query(self, operation_key, external_record_id=None):
                time.sleep(0.08)
                return super().query(operation_key, external_record_id)

        adapter = SlowAdapter()
        adapter.submit(
            operation["external_operation_key"],
            {"business_record_key": "business-1", "value": "hello"},
        )
        with patch("workflow.external.EXTERNAL_LEASE_HEARTBEAT_INTERVAL_SECONDS", 0.01), patch.object(
            self.service,
            "renew_record_lease",
            wraps=self.service.renew_record_lease,
        ) as renew:
            result = self.service.reconcile(operation["external_operation_id"], lease, adapter)

        self.assertEqual(result["side_effect_state"], "CONFIRMED")
        self.assertGreaterEqual(renew.call_count, 3)

    def test_mismatch_creates_manual_intervention_and_never_confirms(self) -> None:
        _mapping, lease, operation = self._operation()
        payload = {"business_record_key": "business-1", "value": "hello"}
        self.service.begin_operation(operation["external_operation_id"], lease)
        self.service.record_submission(
            operation["external_operation_id"], lease, self.adapter.submit(operation["external_operation_key"], payload)
        )
        with self.assertRaises(ExternalVerifyMismatch):
            self.service.verify_operation(
                operation["external_operation_id"], lease, self.adapter,
                {"business_record_key": "business-1", "value": "tampered"},
            )
        operation_after = self.service.get_operation(operation["external_operation_id"])
        self.assertEqual(operation_after["side_effect_state"], "AMBIGUOUS")
        with self.database.read_transaction() as con:
            intervention = con.execute(
                "SELECT state, reason FROM user_interventions WHERE intervention_id=?",
                (f"intervention_external_{operation['external_operation_id']}",),
            ).fetchone()
            mapping = con.execute(
                "SELECT external_status FROM external_records WHERE external_record_mapping_id=?",
                (operation["external_record_mapping_id"],),
            ).fetchone()
        self.assertEqual(intervention["state"], "OPEN")
        self.assertEqual(intervention["reason"], "EXTERNAL_VERIFY_MISMATCH")
        self.assertEqual(mapping["external_status"], "AMBIGUOUS")

    def test_record_lease_is_fenced(self) -> None:
        mapping = self.service.ensure_record(
            self.workflow.workflow_id,
            external_system=self.adapter.system,
            account_scope=self.adapter.account_scope,
            business_record_key="business-2",
            mapping_version="v1",
        )
        first = self.service.acquire_record_lease(mapping["external_record_mapping_id"], "owner-a")
        with self.assertRaises(ConflictError):
            self.service.acquire_record_lease(mapping["external_record_mapping_id"], "owner-b")
        self.service.release_record_lease(first)
        second = self.service.acquire_record_lease(mapping["external_record_mapping_id"], "owner-b")
        self.assertGreater(second.fencing_token, first.fencing_token)
        with self.assertRaises(ConflictError):
            self.service.renew_record_lease(first)

    def test_same_business_key_rebinds_to_rerun_while_preserving_binding_history(self) -> None:
        mapping, _lease, _operation = self._operation()
        self.service.resolve_operation(
            _operation["external_operation_id"],
            decision="NOT_SUBMITTED",
            evidence_source="operator",
            evidence_hash="r" * 32,
            evidence={"reason": "prepare-only fixture"},
        )
        artifacts = ArtifactStore(Path(self.temp.name) / "rerun-artifacts")
        terminal = WorkflowEngine(self.repository, artifacts).run_tts(self.workflow.workflow_id, FakeProvider())
        source = self.repository.get_workflow(self.workflow.workflow_id)
        rerun = self.repository.create_rerun(
            self.workflow.workflow_id,
            expected_group_state_version=source.group_state_version,
            reason="external rerun binding",
        )
        rerun_item_id = self.repository.list_items(rerun.workflow_id)[0]["item_id"]

        rebound = self.service.ensure_record(
            rerun.workflow_id,
            external_system=self.adapter.system,
            account_scope=self.adapter.account_scope,
            business_record_key="business-1",
            mapping_version="v1",
            item_id=rerun_item_id,
        )
        next_operation = self.service.prepare_operation(
            rerun.workflow_id,
            mapping_id=rebound["external_record_mapping_id"],
            operation_key="sync:business-1:v2",
            payload={"business_record_key": "business-1", "value": "rerun"},
            mapping_version="v1",
            item_id=rerun_item_id,
        )

        self.assertEqual(terminal.status, "SUCCEEDED")
        self.assertEqual(rebound["external_record_mapping_id"], mapping["external_record_mapping_id"])
        self.assertEqual(rebound["local_workflow_id"], rerun.workflow_id)
        self.assertEqual(rebound["local_item_id"], rerun_item_id)
        with self.database.read_transaction() as con:
            touched_runs = con.execute(
                "SELECT DISTINCT workflow_id FROM external_record_bindings WHERE external_record_mapping_id=? ORDER BY workflow_id",
                (mapping["external_record_mapping_id"],),
            ).fetchall()
            operation_run = con.execute(
                "SELECT workflow_id, item_id FROM external_operations WHERE external_operation_id=?",
                (next_operation["external_operation_id"],),
            ).fetchone()
        self.assertEqual({row[0] for row in touched_runs}, {self.workflow.workflow_id, rerun.workflow_id})
        self.assertEqual((operation_run[0], operation_run[1]), (rerun.workflow_id, rerun_item_id))

    def test_rejected_external_operation_can_retry_without_reusing_a_confirmed_receipt(self) -> None:
        _mapping, _lease, operation = self._operation()
        resolved = self.service.resolve_operation(
            operation["external_operation_id"],
            decision="NOT_SUBMITTED",
            evidence_source="operator",
            evidence_hash="r" * 32,
            evidence={"reason": "provider rejected before submission"},
        )
        self.assertEqual(resolved["side_effect_state"], "REJECTED")
        before = self.repository.get_workflow(self.workflow.workflow_id)
        self.repository.targeted_command(
            self.workflow.workflow_id,
            "retry",
            {"target_type": "EXTERNAL_OPERATION", "external_operation_id": operation["external_operation_id"]},
            expected_state_version=before.state_version,
            expected_target_state_version=int(resolved["state_version"]),
            reason="safe external retry",
        )
        retried = self.service.get_operation(operation["external_operation_id"])
        self.assertEqual(retried["side_effect_state"], "INTENT_RECORDED")
        self.assertEqual(retried["receipt"], {})
        with self.database.read_transaction() as con:
            intent_state = con.execute(
                "SELECT state FROM side_effect_intents WHERE operation_namespace='external'"
            ).fetchone()[0]
            record_state = con.execute(
                "SELECT external_status, last_error FROM external_records WHERE external_record_mapping_id=?",
                (operation["external_record_mapping_id"],),
            ).fetchone()
        self.assertEqual(intent_state, "RECORDED")
        self.assertEqual((record_state[0], record_state[1]), ("PENDING", None))

    def test_manual_external_resolution_cannot_cross_receipt_boundary_backwards(self) -> None:
        _mapping, lease, operation = self._operation()
        with self.assertRaises(ExternalServiceError) as context:
            self.service.resolve_operation(
                operation["external_operation_id"],
                decision="CONFIRMED",
                evidence_source="operator",
                evidence_hash="c" * 32,
                evidence={"reason": "no observed receipt"},
            )
        self.assertEqual(context.exception.code, "EVIDENCE_REQUIRED")

        self.service.begin_operation(operation["external_operation_id"], lease)
        self.service.record_submission(
            operation["external_operation_id"],
            lease,
            self.adapter.submit(operation["external_operation_key"], {"business_record_key": "business-1", "value": "hello"}),
        )
        with self.assertRaises(ConflictError) as context:
            self.service.resolve_operation(
                operation["external_operation_id"],
                decision="NOT_SUBMITTED",
                evidence_source="operator",
                evidence_hash="n" * 32,
                evidence={"reason": "stale operator decision"},
            )
        self.assertEqual(context.exception.code, "EXTERNAL_STATE_CONFLICT")

        confirmed = self.service.resolve_operation(
            operation["external_operation_id"],
            decision="CONFIRMED",
            evidence_source="operator",
            evidence_hash="v" * 32,
            evidence={"reason": "receipt observed"},
        )
        self.assertEqual(confirmed["side_effect_state"], "CONFIRMED")
        with self.assertRaises(ConflictError) as context:
            self.service.resolve_operation(
                operation["external_operation_id"],
                decision="BLOCKED",
                evidence_source="operator",
                evidence_hash="b" * 32,
                evidence={"reason": "stale block"},
            )
        self.assertEqual(context.exception.code, "EXTERNAL_STATE_CONFLICT")

    def test_generic_external_target_resolution_cannot_downgrade_a_submission(self) -> None:
        _mapping, lease, operation = self._operation()
        self.service.begin_operation(operation["external_operation_id"], lease)
        observed = self.service.record_submission(
            operation["external_operation_id"],
            lease,
            self.adapter.submit(operation["external_operation_key"], {"business_record_key": "business-1", "value": "hello"}),
        )
        snapshot = self.repository.get_workflow(self.workflow.workflow_id)
        with self.database.read_transaction() as con:
            target_version = con.execute(
                "SELECT state_version FROM external_operations WHERE external_operation_id=?",
                (operation["external_operation_id"],),
            ).fetchone()[0]

        with self.assertRaises(ConflictError) as context:
            self.repository.targeted_command(
                self.workflow.workflow_id,
                "resolve",
                {"target_type": "EXTERNAL_OPERATION", "external_operation_id": operation["external_operation_id"]},
                expected_state_version=snapshot.state_version,
                expected_target_state_version=int(target_version),
                decision="NOT_SUBMITTED",
                evidence={"source": "stale-operator", "evidence_hash": "n" * 32},
            )
        self.assertEqual(context.exception.code, "STATE_CONFLICT")
        current = self.service.get_operation(operation["external_operation_id"])
        self.assertEqual(current["side_effect_state"], "SUBMITTED")
        self.assertEqual(current["receipt"], observed["receipt"])

    def test_restart_recovery_marks_external_in_flight_ambiguous_without_submission(self) -> None:
        mapping, lease, operation = self._operation()
        self.service.begin_operation(operation["external_operation_id"], lease)
        findings = RecoveryService(self.database).apply_safe_recovery()
        self.assertTrue(
            any(item.resource_id == operation["external_operation_id"] and item.action == "RECONCILE" for item in findings)
        )
        recovered = self.service.get_operation(operation["external_operation_id"])
        self.assertEqual(recovered["side_effect_state"], "AMBIGUOUS")
        self.assertEqual(self.adapter.submit_calls, 0)
        with self.database.read_transaction() as con:
            intervention = con.execute(
                "SELECT state FROM user_interventions WHERE intervention_id=?",
                (f"intervention_external_{operation['external_operation_id']}",),
            ).fetchone()[0]
            record = con.execute(
                "SELECT external_status FROM external_records WHERE external_record_mapping_id=?",
                (mapping["external_record_mapping_id"],),
            ).fetchone()[0]
        self.assertEqual(intervention, "OPEN")
        self.assertEqual(record, "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
