from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from api.workflow_routes import WorkflowRuntime, _artifact_row_is_readable, _release_failed_idempotency, install_workflow_api
from db.migration_runner import MigrationError
from workflow.fake_provider import FakeProvider
from workflow.parser import LegacyWordParser
from workflow.providers import ProviderError
from workflow.repositories import IdempotencyInProgress, RepositoryError
from workflow.security import TicketExpired


class WorkflowApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = FastAPI()
        self.runtime = WorkflowRuntime.from_paths(root / "workflow.db", root / "artifacts", capability="test-capability")
        install_workflow_api(self.app, runtime=self.runtime)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.runtime.database.close()
        self.temp.cleanup()

    def test_generation_dispatch_guard_is_event_loop_aware(self) -> None:
        self.assertIsInstance(self.runtime.generation_dispatch_guard, asyncio.Lock)

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        return {
            "X-Desktop-Capability": "test-capability",
            "X-Idempotency-Key": key,
        }

    def _create_workflow(self) -> dict:
        response = self.client.post(
            "/api/v1/workflows",
            headers=self._headers("create-workflow-key-01"),
            json={"workflow_type": "tts", "configuration": {"mode": "composite_cut"}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["workflow"]

    def _save_generation_configuration(self, workflow_id: str, configuration: dict[str, object]) -> dict:
        workspace_response = self.client.get(
            f"/api/v1/workflows/{workflow_id}/workspace",
            headers={"X-Desktop-Capability": "test-capability"},
        )
        self.assertEqual(workspace_response.status_code, 200, workspace_response.text)
        workspace = workspace_response.json()["workspace"]
        response = self.client.patch(
            f"/api/v1/workflows/{workflow_id}/workspace",
            headers=self._headers(f"save-config-{workflow_id}"),
            json={
                "expected_state_version": workspace["snapshot"]["state_version"],
                "configuration_revision": workspace["configuration"]["configuration_revision"],
                "configuration": configuration,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["workspace"]

    def test_artifact_content_gate_requires_matching_facts_and_mp3_for_tts(self) -> None:
        base = {
            "artifact_format": "mp3",
            "blob_format": "mp3",
            "artifact_sha256": "a" * 64,
            "blob_sha256": "a" * 64,
            "artifact_size_bytes": 10,
            "blob_size_bytes": 10,
            "artifact_type": "tts-segment",
        }
        self.assertTrue(_artifact_row_is_readable(base))
        self.assertFalse(_artifact_row_is_readable({**base, "artifact_format": "wav", "blob_format": "wav"}))
        self.assertFalse(_artifact_row_is_readable({**base, "blob_sha256": "b" * 64}))
        self.assertFalse(_artifact_row_is_readable({**base, "artifact_size_bytes": 0, "blob_size_bytes": 0}))
        self.assertTrue(_artifact_row_is_readable({**base, "artifact_type": "source", "artifact_format": "bin", "blob_format": "bin"}))

    def test_capability_validation_and_idempotency_are_structured(self) -> None:
        unauthorized = self.client.get(
            "/api/v1/workflows/missing",
            headers={"X-Desktop-Capability": "wrong-capability"},
        )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.json()["error_code"], "UNAUTHORIZED")

        invalid = self.client.post(
            "/api/v1/workflows",
            headers=self._headers("invalid-workflow-key"),
            json={"workflow_type": "tts", "configuration": {}, "file_path": "/tmp/secret"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error_code"], "VALIDATION_ERROR")
        self.assertNotIn("file_path", invalid.text)

        missing = self.client.patch(
            "/api/v1/workflows/workflow-does-not-exist",
            headers=self._headers("missing-workflow-key"),
            json={"expected_state_version": 0, "configuration": {}},
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json()["error_code"], "NOT_FOUND")
        self.assertEqual(missing.json()["workflow_id"], "workflow-does-not-exist")

        payload = {"workflow_type": "tts", "configuration": {"language": "en"}}
        first = self.client.post(
            "/api/v1/workflows",
            headers=self._headers("same-create-request-key"),
            json=payload,
        )
        replay = self.client.post(
            "/api/v1/workflows",
            headers=self._headers("same-create-request-key"),
            json=payload,
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), first.json())

    def test_resolve_rejects_step_target_with_structured_error(self) -> None:
        workflow = self._create_workflow()
        workflow_id = workflow["workflow_id"]
        step_id = self.runtime.repository.create_step(
            workflow_id,
            step_key="generate",
            step_type="tts",
        )
        attempt_id = self.runtime.repository.create_attempt(workflow_id, step_id)
        current_workflow = self.runtime.repository.get_workflow(workflow_id)
        current_step = self.runtime.repository.get_step(workflow_id, step_id)

        response = self.client.post(
            f"/api/v1/attempts/{attempt_id}/resolve",
            headers=self._headers("resolve-step-target-key"),
            json={
                "expected_state_version": current_workflow.state_version,
                "expected_target_state_version": current_step["state_version"],
                "target": {"target_type": "STEP", "step_id": step_id},
                "decision": "BLOCKED",
                "evidence": {
                    "source": "test",
                    "evidence_hash": "s" * 32,
                },
            },
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error_code"], "TARGET_REQUIRED")

    def test_recovery_endpoint_and_frozen_reconfigure_are_routable(self) -> None:
        """终止留下的 AMBIGUOUS run 必须有可达的对账入口和可路由的 409。"""
        from workflow.artifact_store import ArtifactStore
        from workflow.engine import WorkflowEngine

        provider = FakeProvider()
        provider.fail_mode = "after"
        self.runtime.providers.register(provider)
        workflow = self._create_workflow()
        workflow_id = workflow["workflow_id"]
        self.runtime.repository.create_item(
            workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="reconcile discovery",
            item_identity_key="sentence:reconcile-discovery",
        )
        engine = WorkflowEngine(
            self.runtime.repository,
            ArtifactStore(Path(self.temp.name) / "recovery-artifacts"),
        )
        result = engine.run_tts(workflow_id, provider)
        self.assertEqual(result.status, "AMBIGUOUS")

        recovery = self.client.get(
            f"/api/v1/workflows/{workflow_id}/recovery",
            headers=self._headers("recovery-read-key"),
        )
        self.assertEqual(recovery.status_code, 200, recovery.text)
        payload = recovery.json()
        self.assertEqual(payload["workflow_id"], workflow_id)
        self.assertEqual(len(payload["interventions"]), 1)
        intervention = payload["interventions"][0]
        self.assertEqual(intervention["attempt_id"], result.attempt_id)
        self.assertTrue(intervention["work_unit_id"])
        self.assertGreaterEqual(intervention["work_unit_state_version"], 0)
        self.assertIn("works_name", intervention)

        snapshot = self.runtime.repository.get_workflow(workflow_id)
        response = self.client.patch(
            f"/api/v1/workflows/{workflow_id}",
            headers=self._headers("recovery-patch-key"),
            json={
                "expected_state_version": snapshot.state_version,
                "configuration": {"mode": "composite_cut", "default_female_voice": "speaker:changed"},
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error_code"], "RECONCILIATION_REQUIRED")

    def test_credential_like_configuration_is_rejected_before_persistence(self) -> None:
        secret = "do-not-write-this-secret"
        response = self.client.post(
            "/api/v1/workflows",
            headers=self._headers("secret-config-request-key"),
            json={
                "workflow_type": "tts",
                "configuration": {
                    "voice": "amanda",
                    "nested": {"api_key": secret},
                },
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")
        self.assertNotIn(secret, response.text)
        with self.runtime.database.read_transaction() as con:
            workflow_count = con.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
            secret_count = con.execute(
                "SELECT COUNT(*) FROM workflows WHERE configuration_snapshot LIKE ?",
                (f"%{secret}%",),
            ).fetchone()[0]
        self.assertEqual(workflow_count, 0)
        self.assertEqual(secret_count, 0)

    def test_versioned_runtime_without_a_supplied_capability_fails_closed(self) -> None:
        self.runtime.capability = None
        install_workflow_api(self.app, runtime=self.runtime)
        self.assertTrue(self.runtime.capability)
        unauthorized = self.client.get("/api/v1/workflows/missing")
        self.assertEqual(unauthorized.status_code, 401, unauthorized.text)
        authorized = self.client.get(
            "/api/v1/workflows/missing",
            headers={"X-Desktop-Capability": self.runtime.capability},
        )
        self.assertEqual(authorized.status_code, 404, authorized.text)

    def test_failed_operation_and_attempt_mutations_release_their_reservation(self) -> None:
        self.runtime.ensure_initialized()
        for index, path_name in enumerate(("operation_id", "attempt_id"), 1):
            key = f"failed-release-key-{index:02d}"
            resource_id = f"resource-{index}"
            self.runtime.repository.begin_idempotency(
                scope=f"test:{resource_id}",
                client_key=key,
                command_name="testMutation",
                method="POST",
                resource_id=resource_id,
                target=None,
                request={"index": index},
            )
            request = Request({
                "type": "http",
                "headers": [(b"x-idempotency-key", key.encode("utf-8"))],
                "path_params": {path_name: resource_id},
                "app": self.app,
            })
            _release_failed_idempotency(request, RepositoryError("expected test failure", code="STATE_CONFLICT"))
            retry_id, cached = self.runtime.repository.begin_idempotency(
                scope=f"test:{resource_id}",
                client_key=key,
                command_name="testMutation",
                method="POST",
                resource_id=resource_id,
                target=None,
                request={"index": index},
            )
            self.assertIsNotNone(retry_id)
            self.assertIsNone(cached)

    def test_failed_idempotency_cleanup_fails_closed_when_scope_is_ambiguous(self) -> None:
        self.runtime.ensure_initialized()
        key = "ambiguous-cleanup-key-123456"
        first, _ = self.runtime.repository.begin_idempotency(
            scope="test:scope-a", client_key=key, command_name="testMutation",
            method="POST", resource_id="same-resource", target=None, request={"scope": "a"},
        )
        second, _ = self.runtime.repository.begin_idempotency(
            scope="test:scope-b", client_key=key, command_name="testMutation",
            method="POST", resource_id="same-resource", target=None, request={"scope": "b"},
        )

        self.assertEqual(
            self.runtime.repository.abandon_idempotency(
                client_key=key,
                resource_id="same-resource",
            ),
            0,
        )
        with self.runtime.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM workflow_idempotency_keys WHERE client_key=? AND response_json IS NULL",
                    (key,),
                ).fetchone()[0],
                2,
            )

        self.runtime.repository.complete_idempotency(first, response_status=409, response={"scope": "a"})
        self.assertEqual(
            self.runtime.repository.abandon_idempotency(
                client_key=key,
                resource_id="same-resource",
            ),
            1,
        )
        with self.runtime.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM workflow_idempotency_keys WHERE idempotency_id=?",
                    (second,),
                ).fetchone()[0],
                0,
            )

    def test_persistence_failure_keeps_idempotency_claim(self) -> None:
        self.runtime.ensure_initialized()
        key = "persistence-unknown-outcome-key"
        self.runtime.repository.begin_idempotency(
            scope="test:persistence-unknown-outcome",
            client_key=key,
            command_name="testMutation",
            method="POST",
            resource_id="workflow-persistence-unknown",
            target=None,
            request={"value": 1},
        )
        request = Request({
            "type": "http",
            "headers": [(b"x-idempotency-key", key.encode("utf-8"))],
            "path_params": {"workflow_id": "workflow-persistence-unknown"},
            "app": self.app,
        })

        _release_failed_idempotency(
            request,
            RepositoryError("database outcome is unknown", code="PERSISTENCE_ERROR"),
        )

        with self.assertRaises(IdempotencyInProgress):
            self.runtime.repository.begin_idempotency(
                scope="test:persistence-unknown-outcome",
                client_key=key,
                command_name="testMutation",
                method="POST",
                resource_id="workflow-persistence-unknown",
                target=None,
                request={"value": 1},
            )

    def test_migration_failure_is_structured_instead_of_opaque_http_500(self) -> None:
        app = FastAPI()
        runtime = WorkflowRuntime.from_paths(
            Path(self.temp.name) / "migration-error.db",
            Path(self.temp.name) / "migration-error-artifacts",
            capability="test-capability",
        )
        install_workflow_api(app, runtime=runtime)
        client = TestClient(app)
        try:
            with patch(
                "workflow.database.load_migrations",
                side_effect=MigrationError("no migration files found"),
            ):
                response = client.get(
                    "/api/v1/workflows?limit=100",
                    headers={"X-Desktop-Capability": "test-capability"},
                )
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["error_code"], "MIGRATION_ERROR")
            self.assertFalse(response.json()["side_effect_occurred"])
        finally:
            client.close()
            runtime.database.close()

    def test_first_request_runs_safe_recovery_scheduler_and_bounded_gc(self) -> None:
        orphan = self.runtime.artifacts.blob_root / "aa" / "orphan.bin"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")

        response = self.client.get(
            "/api/v1/workflows?limit=10",
            headers={"X-Desktop-Capability": "test-capability"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.runtime.initialized)
        self.assertFalse(orphan.exists())
        self.assertTrue(any(item.action == "REMOVED" for item in self.runtime.startup_gc_findings))

    def test_startup_recovery_expires_a_stale_source_generation(self) -> None:
        self.runtime.ensure_initialized()
        workflow = self.runtime.repository.create_workflow("tts", {})
        created = self.runtime.imports.create_import(workflow.workflow_id, metadata={"filename": "stale.docx"})
        grant = self.runtime.imports.acquire_writer(
            created["source_import_id"], 1, expected_state_version=created["state_version"]
        )
        with self.runtime.database.transaction() as con:
            con.execute(
                "UPDATE source_import_generations SET expires_at=? WHERE source_import_id=? AND generation=1",
                ("2000-01-01T00:00:00.000Z", created["source_import_id"]),
            )
        self.runtime.initialized = False
        self.runtime.ensure_initialized()

        current = self.runtime.imports.get_generation(created["source_import_id"], 1)
        self.assertEqual(current["status"], "EXPIRED")
        self.assertTrue(any(item.kind == "source_generation" for item in self.runtime.startup_recovery_findings))
        self.assertIsNotNone(grant)

    def test_second_runtime_cannot_open_the_same_workflow_database(self) -> None:
        self.runtime.ensure_initialized()
        second = WorkflowRuntime.from_paths(
            self.runtime.database.path,
            self.runtime.artifacts.root,
            capability="test-capability",
        )
        app = FastAPI()
        install_workflow_api(app, runtime=second)
        client = TestClient(app)
        try:
            response = client.get(
                "/api/v1/workflows?limit=10",
                headers={"X-Desktop-Capability": "test-capability"},
            )
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["error_code"], "PERSISTENCE_ERROR")
        finally:
            client.close()
            second.database.close()

    def test_expired_content_ticket_uses_gone_status(self) -> None:
        with patch.object(self.runtime.tickets, "consume", side_effect=TicketExpired("ticket has expired")):
            response = self.client.get(
                "/api/v1/artifacts/artifact-1/content",
                headers={
                    "X-Desktop-Capability": "test-capability",
                    "X-Artifact-Ticket": "expired-ticket",
                },
            )
        self.assertEqual(response.status_code, 410, response.text)
        self.assertEqual(response.json()["error_code"], "CURSOR_EXPIRED")

    def test_source_generation_writer_and_artifact_ticket_flow(self) -> None:
        workflow = self._create_workflow()
        workflow_id = workflow["workflow_id"]
        source = self.client.post(
            f"/api/v1/workflows/{workflow_id}/source-imports",
            headers=self._headers("create-source-import-key"),
            json={"metadata": {"filename": "英语听力.docx"}, "content_type": "application/octet-stream"},
        )
        self.assertEqual(source.status_code, 201, source.text)
        source_body = source.json()
        import_id = source_body["source_import_id"]
        generation = source_body["staging_generation"]
        self.assertNotIn("staging_key", source_body)

        current = self.client.get(
            f"/api/v1/source-imports/{import_id}/generations/{generation}",
            headers={"X-Desktop-Capability": "test-capability"},
        )
        self.assertEqual(current.status_code, 200, current.text)
        self.assertNotIn("staging_key", current.json())
        writer = self.client.post(
            f"/api/v1/source-imports/{import_id}/generations/{generation}/writer-tickets",
            headers=self._headers("source-writer-ticket-key"),
            json={"expected_state_version": current.json()["state_version"]},
        )
        self.assertEqual(writer.status_code, 201, writer.text)
        grant = writer.json()["grant"]
        with self.runtime.database.read_transaction() as con:
            cached_writer_response = con.execute(
                "SELECT response_json FROM workflow_idempotency_keys WHERE client_key=?",
                ("source-writer-ticket-key",),
            ).fetchone()[0]
        self.assertNotIn(grant, cached_writer_response)

        replay = self.client.post(
            f"/api/v1/source-imports/{import_id}/generations/{generation}/writer-tickets",
            headers=self._headers("source-writer-ticket-key"),
            json={"expected_state_version": current.json()["state_version"]},
        )
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json()["error_code"], "IDEMPOTENCY_CONFLICT")

        uploaded = self.client.put(
            f"/api/v1/source-imports/{import_id}/content",
            headers={
                **self._headers("source-content-upload-key"),
                "X-Staging-Generation": str(generation),
                "X-Source-Write-Grant": grant,
                "X-Artifact-Format": "bin",
                "Content-Type": "application/octet-stream",
            },
            content=b"hello workflow",
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        ready = uploaded.json()
        self.assertEqual(ready["status"], "READY")
        artifact_id = ready["source_artifact_id"]
        self.assertTrue(artifact_id)

        ticket = self.client.post(
            f"/api/v1/artifacts/{artifact_id}/content-tickets",
            headers={"X-Desktop-Capability": "test-capability"},
        )
        self.assertEqual(ticket.status_code, 201, ticket.text)
        self.assertEqual(ticket.json()["content_type"], "application/octet-stream")
        self.assertEqual(ticket.json()["content_length"], len(b"hello workflow"))
        self.assertEqual(ticket.json()["filename"], "英语听力.docx")
        content = self.client.get(
            f"/api/v1/artifacts/{artifact_id}/content",
            headers={
                "X-Desktop-Capability": "test-capability",
                "X-Artifact-Ticket": ticket.json()["ticket"],
            },
        )
        self.assertEqual(content.status_code, 200, content.text)
        self.assertEqual(content.content, b"hello workflow")
        self.assertEqual(content.headers["x-artifact-filename"], quote("英语听力.docx", safe=""))

        replay = self.client.get(
            f"/api/v1/artifacts/{artifact_id}/content",
            headers={
                "X-Desktop-Capability": "test-capability",
                "X-Artifact-Ticket": ticket.json()["ticket"],
            },
        )
        self.assertEqual(replay.status_code, 401, replay.text)

    def test_real_provider_capability_failure_is_structured_before_acceptance(self) -> None:
        # The formal runtime is real-provider-on by default.  This test keeps
        # the explicit offline capability-gate regression covered.
        self.runtime.providers.get("xunfei", "xunfei-default").allow_real = False
        workflow = self._create_workflow()
        response = self.client.post(
            f"/api/v1/workflows/{workflow['workflow_id']}/generate",
            headers=self._headers("provider-capability-error-key"),
            json={"expected_state_version": workflow["state_version"], "provider": "xunfei"},
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error_code"], "EXTERNAL_CAPABILITY_REQUIRED")
        self.assertFalse(response.json()["side_effect_occurred"])

    def test_generate_uses_submitted_configuration_and_schedules_same_values(self) -> None:
        provider = FakeProvider()
        self.runtime.providers.register(provider)
        workflow = self._create_workflow()
        workflow_id = workflow["workflow_id"]
        self.runtime.repository.create_item(
            workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="configuration override",
            item_identity_key="sentence:configuration-override",
        )
        workspace = self.client.get(
            f"/api/v1/workflows/{workflow_id}/workspace",
            headers={"X-Desktop-Capability": "test-capability"},
        ).json()["workspace"]

        with patch("api.workflow_routes._schedule_generation_task", return_value=None) as schedule:
            response = self.client.post(
                f"/api/v1/workflows/{workflow_id}/generate",
                headers=self._headers("generate-config-override-key"),
                json={
                    "expected_state_version": workspace["snapshot"]["state_version"],
                    "configuration_revision": workspace["configuration"]["configuration_revision"],
                    "generation_mode": "single_segment",
                    "provider": "fake",
                    "account_scope": provider.account_scope,
                },
            )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(schedule.call_args.kwargs["generation_mode"], "single_segment")
        self.assertEqual(schedule.call_args.kwargs["provider"], "fake")
        self.assertEqual(schedule.call_args.kwargs["account_scope"], provider.account_scope)
        saved = self.runtime.repository.get_configuration(workflow_id)
        self.assertEqual(saved["generation_mode"], "single_segment")
        self.assertEqual(saved["provider"], "fake")
        self.assertEqual(saved["account_scope"], provider.account_scope)

    def test_generation_enqueue_failure_does_not_leave_a_running_zombie(self) -> None:
        provider = FakeProvider()
        self.runtime.providers.register(provider)
        workflow = self._create_workflow()
        self.runtime.repository.create_item(
            workflow["workflow_id"],
            item_type="sentence",
            sequence=0,
            normalized_content="enqueue failure",
            item_identity_key="sentence:enqueue-failure",
        )
        saved_workspace = self._save_generation_configuration(
            workflow["workflow_id"],
            {"provider": "fake", "account_scope": provider.account_scope},
        )
        with patch(
            "api.workflow_routes._schedule_generation_task",
            side_effect=RepositoryError(
                "generation queue is full",
                code="RESOURCE_EXHAUSTED",
                details={"queue_depth": 4},
            ),
        ):
            response = self.client.post(
                f"/api/v1/workflows/{workflow['workflow_id']}/generate",
                headers=self._headers("enqueue-failure-key"),
                json={
                    "expected_state_version": saved_workspace["snapshot"]["state_version"],
                    "provider": "fake",
                    "account_scope": provider.account_scope,
                },
            )
        self.assertEqual(response.status_code, 429, response.text)
        current = self.runtime.repository.get_workflow(workflow["workflow_id"])
        self.assertEqual(current.execution_state, "WAITING_RETRY")
        self.assertEqual(current.result_status, "IN_PROGRESS")
        self.assertEqual(current.latest_event["event_type"], "GENERATION_TASK_FAILED")

    def test_workspace_routes_return_snapshot_actions_and_versioned_item_edits(self) -> None:
        workflow = self._create_workflow()
        workflow_id = workflow["workflow_id"]
        item_id = self.runtime.repository.create_item(
            workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="skip from API",
            item_identity_key="sentence:api-0",
        )

        initial = self.client.get(
            f"/api/v1/workflows/{workflow_id}/workspace",
            headers={"X-Desktop-Capability": "test-capability"},
        )
        self.assertEqual(initial.status_code, 200, initial.text)
        initial_workspace = initial.json()["workspace"]
        self.assertEqual(initial_workspace["configuration"]["configuration_revision"], 1)
        self.assertEqual(initial_workspace["progress"]["pending"], 1)
        self.assertIn(initial_workspace["provider"]["status"], {"LOGIN_REQUIRED", "READY", "UNAVAILABLE"})
        self.assertEqual(
            initial_workspace["provider"]["can_start_generation"],
            initial_workspace["provider"]["status"] != "UNAVAILABLE",
        )
        self.assertTrue(any(
            action["kind"] == "SERVICE" and action["type"] == "GENERATE" and action["enabled"]
            for action in initial_workspace["available_actions"]
        ))

        patched = self.client.patch(
            f"/api/v1/workflows/{workflow_id}/workspace",
            headers=self._headers("workspace-patch-key"),
            json={
                "expected_state_version": initial_workspace["snapshot"]["state_version"],
                "configuration_revision": 1,
                "item_overrides": [{
                    "item_id": item_id,
                    "patch": {"status": "SKIPPED", "skip_reason": "API 测试"},
                }],
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        patched_workspace = patched.json()["workspace"]
        self.assertEqual(patched_workspace["configuration"]["configuration_revision"], 2)
        self.assertEqual(patched_workspace["progress"]["skipped"], 1)
        self.assertEqual(patched_workspace["progress"]["pending"], 0)
        self.assertEqual(
            patched_workspace["delivery"]["exclusion_reasons"][item_id],
            "ITEM_SKIPPED",
        )

        active = self.client.get(
            "/api/v1/workflows/active?limit=10",
            headers={"X-Desktop-Capability": "test-capability"},
        )
        self.assertEqual(active.status_code, 200, active.text)
        self.assertTrue(any(
            candidate["workflow"]["workflow_id"] == workflow_id
            for candidate in active.json()["workflows"]
        ))

    def test_all_skipped_generate_finishes_without_provider_capability(self) -> None:
        workflow = self._create_workflow()
        workflow_id = workflow["workflow_id"]
        item_id = self.runtime.repository.create_item(
            workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="already skipped",
            item_identity_key="sentence:skip-0",
        )
        patched = self.client.patch(
            f"/api/v1/workflows/{workflow_id}/workspace",
            headers=self._headers("all-skipped-patch-key"),
            json={
                "expected_state_version": workflow["state_version"],
                "configuration_revision": 1,
                "item_overrides": [{"item_id": item_id, "patch": {"status": "SKIPPED"}}],
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)

        generated = self.client.post(
            f"/api/v1/workflows/{workflow_id}/generate",
            headers=self._headers("all-skipped-generate-key"),
            json={
                "expected_state_version": patched.json()["workspace"]["snapshot"]["state_version"],
                "configuration_revision": 2,
            },
        )
        self.assertEqual(generated.status_code, 202, generated.text)
        current = self.client.get(
            f"/api/v1/workflows/{workflow_id}",
            headers={"X-Desktop-Capability": "test-capability"},
        )
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["workflow"]["result_status"], "SUCCEEDED")

    def test_large_item_content_is_read_in_bounded_utf8_chunks(self) -> None:
        workflow = self._create_workflow()
        workflow_id = workflow["workflow_id"]
        content = "词汇 example\n" * 12000
        item_id = self.runtime.repository.create_item(
            workflow_id,
            item_type="vocabulary",
            sequence=0,
            normalized_content=content,
            item_identity_key="vocabulary:large-content",
            metadata={"sheet_name": "Unit 6", "row": 12},
        )
        workspace_response = self.client.get(
            f"/api/v1/workflows/{workflow_id}/workspace",
            headers={"X-Desktop-Capability": "test-capability"},
        )
        self.assertEqual(workspace_response.status_code, 200, workspace_response.text)
        item = workspace_response.json()["workspace"]["items"][0]
        self.assertIsNone(item["normalized_content"])
        content_ref = item["content_ref"]
        self.assertGreater(content_ref["size_bytes"], content_ref["max_response_bytes"])

        pieces = []
        offset = 0
        for _ in range(64):
            response = self.client.get(
                f"/api/v1/workflows/{workflow_id}/items/{item_id}/content/{content_ref['content_id']}",
                params={
                    "expected_state_version": workspace_response.json()["workspace"]["snapshot"]["state_version"],
                    "offset_bytes": offset,
                    "max_response_bytes": 8192,
                },
                headers={"X-Desktop-Capability": "test-capability"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            pieces.append(payload["content"])
            if not payload["truncated"]:
                break
            next_offset = payload["next_offset_bytes"]
            self.assertGreater(next_offset, offset)
            offset = next_offset
        else:
            self.fail("large content did not finish within the bounded chunk count")
        self.assertEqual("".join(pieces), content)

    def test_cancel_signal_releases_generation_slot_after_cooperative_browser_stop(self) -> None:
        class CooperativeProvider:
            provider = "xunfei"
            account_scope = "test-cancel-account"

            def __init__(self) -> None:
                self.started = threading.Event()

            def submit(self, submission_key, payload):
                self.started.set()
                cancel_check = payload.get("_cancel_check")
                while not callable(cancel_check) or not cancel_check():
                    time.sleep(0.01)
                raise ProviderError(
                    "讯飞浏览器任务已取消；为避免重复扣费，提交结果需要核验",
                    code="SUBMISSION_AMBIGUOUS",
                    ambiguous=True,
                )

            def query(self, submission_key):
                return None

            def download(self, receipt):
                return b""

        provider = CooperativeProvider()
        self.runtime.providers.register(provider)
        workflow = self._create_workflow()
        self.runtime.repository.create_item(
            workflow["workflow_id"],
            item_type="sentence",
            sequence=0,
            normalized_content="hello",
            item_identity_key="sentence:0",
        )
        saved_workspace = self._save_generation_configuration(
            workflow["workflow_id"],
            {"provider": "xunfei", "account_scope": provider.account_scope},
        )

        # Keep one ASGI portal alive across requests.  A plain TestClient
        # request closes its portal immediately and correctly waits for
        # background tasks, which would make this deliberately blocking test
        # deadlock before the cancel request can be sent.
        with TestClient(self.app) as client:
            started = client.post(
                f"/api/v1/workflows/{workflow['workflow_id']}/generate",
                headers=self._headers("cooperative-generate-key"),
                json={
                    "expected_state_version": saved_workspace["snapshot"]["state_version"],
                    "provider": "xunfei",
                    "account_scope": provider.account_scope,
                },
            )
            self.assertEqual(started.status_code, 202, started.text)
            self.assertTrue(provider.started.wait(2.0))

            # The worker advances the workflow version while it creates the
            # durable TTS plan and marks the provider boundary.  Cancel must
            # use that authoritative version, not the pre-worker acceptance
            # response returned by /generate.
            current = client.get(
                f"/api/v1/workflows/{workflow['workflow_id']}",
                headers={"X-Desktop-Capability": "test-capability"},
            ).json()["workflow"]
            cancelled = client.post(
                f"/api/v1/workflows/{workflow['workflow_id']}/cancel",
                headers=self._headers("cooperative-cancel-key"),
                json={"expected_state_version": current["state_version"], "reason": "test-cancel"},
            )
            self.assertEqual(cancelled.status_code, 202, cancelled.text)

            deadline = time.monotonic() + 2.0
            while self.runtime.generation_tasks and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(self.runtime.generation_tasks)
            snapshot = client.get(
                f"/api/v1/workflows/{workflow['workflow_id']}",
                headers={"X-Desktop-Capability": "test-capability"},
            ).json()["workflow"]
        self.assertEqual(snapshot["cleanup_state"], "SUCCEEDED")
        self.assertEqual(snapshot["control_state"], "TERMINATING")
        self.assertEqual(snapshot["execution_state"], "WAITING_USER")

    def test_external_runtime_requires_full_profile_and_route_lifecycle_is_fenced(self) -> None:
        workflow = self._create_workflow()
        blocked = self.client.post(
            f"/api/v1/workflows/{workflow['workflow_id']}/external-records",
            headers=self._headers("external-2a-blocked-key"),
            json={
                "external_system": "fake-external",
                "account_scope": "scope-a",
                "business_record_key": "record-2a",
                "mapping_version": "v1",
            },
        )
        self.assertEqual(blocked.status_code, 503, blocked.text)
        self.assertEqual(blocked.json()["error_code"], "MIGRATION_REQUIRED")

        full_root = Path(self.temp.name) / "full"
        full_app = FastAPI()
        full_runtime = WorkflowRuntime.from_paths(
            full_root / "workflow.db", full_root / "artifacts", capability="test-capability", profile="full"
        )
        install_workflow_api(full_app, runtime=full_runtime)
        full_client = TestClient(full_app)
        try:
            full_runtime.ensure_initialized()
            full_workflow = full_runtime.repository.create_workflow("external-sync", {"mapping_version": "v1"})
            item_id = full_runtime.repository.create_item(
                full_workflow.workflow_id,
                item_type="record",
                sequence=0,
                normalized_content="payload",
                item_identity_key="record:0",
            )
            headers = self._headers("external-full-record-key")
            record = full_client.post(
                f"/api/v1/workflows/{full_workflow.workflow_id}/external-records",
                headers=headers,
                json={
                    "external_system": "fake-external",
                    "account_scope": "scope-a",
                    "business_record_key": "record-full",
                    "mapping_version": "v1",
                    "item_id": item_id,
                },
            )
            self.assertEqual(record.status_code, 201, record.text)
            mapping_id = record.json()["external_record_mapping_id"]
            lease = full_client.post(
                f"/api/v1/external-records/{mapping_id}/leases",
                headers=self._headers("external-full-lease-key"),
                json={"owner_id": "api-test-owner"},
            )
            self.assertEqual(lease.status_code, 201, lease.text)
            lease_body = lease.json()
            operation = full_client.post(
                f"/api/v1/workflows/{full_workflow.workflow_id}/external-operations",
                headers=self._headers("external-full-operation-key"),
                json={
                    "mapping_id": mapping_id,
                    "operation_key": "sync:record-full",
                    "payload": {"business_record_key": "record-full", "value": "payload"},
                    "mapping_version": "v1",
                    "item_id": item_id,
                },
            )
            self.assertEqual(operation.status_code, 201, operation.text)
            operation_body = operation.json()
            lease_reference = {
                "lease_id": lease_body["lease_id"],
                "mapping_id": mapping_id,
                "owner_id": lease_body["owner_id"],
                "fencing_token": lease_body["fencing_token"],
            }
            begun = full_client.post(
                f"/api/v1/external-operations/{operation_body['external_operation_id']}/begin",
                headers=self._headers("external-full-begin-key"),
                json=lease_reference,
            )
            self.assertEqual(begun.status_code, 202, begun.text)
            observed = full_client.post(
                f"/api/v1/external-operations/{operation_body['external_operation_id']}/submissions",
                headers=self._headers("external-full-submit-key"),
                json={
                    "lease": lease_reference,
                    "external_record_id": "external-1",
                    "canonical_key": "record-full",
                    "summary": {"authorization": "must-not-be-persisted"},
                },
            )
            self.assertEqual(observed.status_code, 202, observed.text)
            self.assertEqual(observed.json()["receipt"]["summary"]["authorization"], "[REDACTED]")
            resolved = full_client.post(
                f"/api/v1/external-operations/{operation_body['external_operation_id']}/resolve",
                headers=self._headers("external-full-resolve-key"),
                json={
                    "decision": "CONFIRMED",
                    "resolved_by": "api-test",
                    "evidence": {"source": "test", "evidence_hash": "e" * 32},
                },
            )
            self.assertEqual(resolved.status_code, 202, resolved.text)
            self.assertEqual(resolved.json()["side_effect_state"], "CONFIRMED")
        finally:
            full_client.close()
            full_runtime.database.close()


if __name__ == "__main__":
    unittest.main()
