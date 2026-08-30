from __future__ import annotations

import sqlite3
import io
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from workflow.artifact_store import ArtifactIntegrityError, ArtifactStore, ArtifactStoreError, StagedFile
from workflow.database import WorkflowDatabase
from workflow.domain import content_hash
from workflow.event_store import CursorExpired, EventStore, EventStoreError
from workflow.engine import WorkflowEngine
from workflow.fake_provider import AmbiguousProviderError, FakeProvider
from workflow.providers import ProviderError, ProviderReceipt
from workflow.garbage_collector import ArtifactGarbageCollector, GarbageFinding
from workflow.recovery import RecoveryService
from workflow.repositories import (
    BudgetExhausted,
    ConflictError,
    IdempotencyConflict,
    IdempotencyInProgress,
    LeaseConflict,
    WorkflowRepository,
)
from workflow.scheduler import PersistentScheduler
from workflow.security import OneTimeTicketManager, TicketExpired, TicketError
from workflow.source_imports import SourceImportError, SourceImportService


class WorkflowRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wordtts-workflow-test-")
        root = Path(self.temp.name)
        self.database = WorkflowDatabase(root / "workflow.db")
        self.database.initialize()
        self.repository = WorkflowRepository(self.database)
        self.events = EventStore(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_workflow_commands_use_optimistic_state_version(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"voice": "a"})
        updated = self.repository.patch_draft(snapshot.workflow_id, 0, configuration={"voice": "b"})
        self.assertEqual(updated.state_version, 1)
        with self.assertRaises(ConflictError):
            self.repository.patch_draft(snapshot.workflow_id, 0, configuration={"voice": "c"})

    def test_parsed_workflow_can_save_config_before_first_attempt_but_freezes_afterward(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"generation_mode": "composite_cut"})
        parsed = self.repository.command(snapshot.workflow_id, "parse", snapshot.state_version)
        configured = self.repository.patch_draft(
            snapshot.workflow_id,
            parsed.state_version,
            configuration={
                "generation_mode": "composite_cut",
                "default_female_voice": "speaker:linda",
                "default_male_voice": "speaker:steve",
            },
        )
        self.assertEqual(configured.state_version, parsed.state_version + 1)
        self.assertEqual(
            self.repository.get_configuration(snapshot.workflow_id)["default_female_voice"],
            "speaker:linda",
        )

        self.repository.create_item(
            snapshot.workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="hello",
            item_identity_key="sentence:0",
        )
        finished = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "config-freeze-artifacts"),
        ).run_tts(snapshot.workflow_id, FakeProvider())
        self.assertEqual(finished.status, "SUCCEEDED")
        current = self.repository.get_workflow(snapshot.workflow_id)
        with self.assertRaises(ConflictError) as context:
            self.repository.patch_draft(
                snapshot.workflow_id,
                current.state_version,
                configuration={
                    "generation_mode": "composite_cut",
                    "default_female_voice": "speaker:changed",
                    "default_male_voice": "speaker:steve",
                },
            )
        self.assertEqual(context.exception.code, "CONFIG_FROZEN")

    def test_workspace_projection_versions_item_skip_and_configuration_revision(self) -> None:
        snapshot = self.repository.create_workflow(
            "tts",
            {"generation_mode": "composite_cut", "preview": False},
        )
        first_item = self.repository.create_item(
            snapshot.workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="keep this line",
            item_identity_key="sentence:0",
            role="narrator",
        )
        second_item = self.repository.create_item(
            snapshot.workflow_id,
            item_type="sentence",
            sequence=1,
            normalized_content="skip this line",
            item_identity_key="sentence:1",
        )

        initial = self.repository.get_workspace(
            snapshot.workflow_id,
            capabilities={"supports_pause": False, "supports_resume": False},
        )
        self.assertEqual(initial["configuration"]["configuration_revision"], 1)
        self.assertEqual(initial["progress"], {
            "total": 2,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "skipped": 0,
            "pending": 2,
            "deliverable": 0,
            "percent": 0,
            "deliverable_percent": 0,
        })
        self.assertFalse(next(
            action for action in initial["available_actions"]
            if action["kind"] == "SERVICE" and action["type"] == "PAUSE"
        )["enabled"])

        patched = self.repository.patch_draft(
            snapshot.workflow_id,
            snapshot.state_version,
            expected_configuration_revision=1,
            configuration={"generation_mode": "single_segment", "preview": False},
            item_overrides=[{
                "item_id": second_item,
                "patch": {"status": "SKIPPED", "skip_reason": "重复内容"},
            }],
        )
        self.assertEqual(self.repository.get_configuration_revision(snapshot.workflow_id), 2)
        self.assertEqual(self.repository.list_items(snapshot.workflow_id)[1]["status"], "SKIPPED")
        self.assertGreater(patched.state_version, snapshot.state_version)

        workspace = self.repository.get_workspace(snapshot.workflow_id)
        self.assertEqual(workspace["configuration"]["configuration_revision"], 2)
        self.assertEqual(workspace["configuration"]["effective"]["generation_mode"], "single_segment")
        self.assertEqual(workspace["progress"]["skipped"], 1)
        self.assertEqual(workspace["progress"]["pending"], 1)
        skipped = next(item for item in workspace["items"] if item["item_id"] == second_item)
        self.assertEqual(skipped["status"], "SKIPPED")
        self.assertEqual(workspace["delivery"]["exclusion_reasons"][second_item], "ITEM_SKIPPED")
        self.assertIn(first_item, {item["item_id"] for item in workspace["items"]})

        with self.assertRaises(ConflictError) as stale:
            self.repository.patch_draft(
                snapshot.workflow_id,
                patched.state_version,
                expected_configuration_revision=1,
                item_overrides=[{"item_id": first_item, "patch": {"status": "SKIPPED"}}],
            )
        self.assertEqual(stale.exception.code, "CONFIGURATION_CONFLICT")

    def test_workspace_blocks_generation_on_unverified_artifact_projection(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"generation_mode": "single_segment"})
        item_id = self.repository.create_item(
            snapshot.workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="success without a verified artifact",
            item_identity_key="sentence:0",
            status="SUCCEEDED",
        )

        workspace = self.repository.get_workspace(
            snapshot.workflow_id,
            capabilities={
                "provider": {
                    "provider": "fake",
                    "status": "READY",
                    "ready": True,
                    "can_generate": True,
                },
            },
        )

        self.assertIn(item_id, workspace["delivery"]["excluded_item_ids"])
        self.assertIn(
            "ARTIFACT_MISSING_OR_UNVERIFIED",
            {blocker["code"] for blocker in workspace["blockers"]},
        )
        generate = next(
            action for action in workspace["available_actions"]
            if action["type"] == "GENERATE"
        )
        self.assertFalse(generate["enabled"])
        self.assertEqual(generate["reason"], "存在尚未核验或状态冲突的条目")
        blocker = next(
            blocker for blocker in workspace["blockers"]
            if blocker["code"] == "ARTIFACT_MISSING_OR_UNVERIFIED"
        )
        self.assertEqual(blocker["recovery_action"]["type"], "RECONCILE")
        self.assertFalse(blocker["recovery_action"]["enabled"])
        reconcile = next(
            action for action in workspace["available_actions"]
            if action["kind"] == "SERVICE" and action["type"] == "RECONCILE"
        )
        self.assertFalse(reconcile["enabled"])

    def test_workspace_normalizes_blocked_provider_capability_snapshot(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"generation_mode": "single_segment"})
        self.repository.create_item(
            snapshot.workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="provider gate",
            item_identity_key="sentence:0",
        )

        workspace = self.repository.get_workspace(
            snapshot.workflow_id,
            capabilities={
                "provider": {
                    "provider": "fake",
                    "status": "EXPIRED",
                    "ready": True,
                    "can_generate": True,
                },
            },
        )

        self.assertEqual(workspace["provider"]["status"], "EXPIRED")
        self.assertFalse(workspace["provider"]["ready"])
        self.assertFalse(workspace["provider"]["can_generate"])
        generate = next(
            action for action in workspace["available_actions"]
            if action["type"] == "GENERATE"
        )
        self.assertFalse(generate["enabled"])

    def test_workspace_keeps_login_entry_enabled_without_advertising_provider_ready(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"generation_mode": "single_segment"})
        self.repository.create_item(
            snapshot.workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="provider login entry",
            item_identity_key="sentence:provider-login-entry",
        )

        workspace = self.repository.get_workspace(
            snapshot.workflow_id,
            capabilities={
                "provider": {
                    "provider": "xunfei",
                    "status": "LOGIN_REQUIRED",
                    "ready": False,
                    "can_generate": False,
                    "can_start_generation": True,
                    "reason": "首次生成时将打开讯飞浏览器，请完成登录",
                },
            },
        )

        self.assertFalse(workspace["provider"]["ready"])
        self.assertFalse(workspace["provider"]["can_generate"])
        self.assertTrue(workspace["provider"]["can_start_generation"])
        generate = next(
            action for action in workspace["available_actions"]
            if action["type"] == "GENERATE"
        )
        self.assertTrue(generate["enabled"])

    def test_workspace_flags_oversized_item_metadata_instead_of_silently_truncating(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"generation_mode": "single_segment"})
        metadata = {
            key: "source-fact-" + ("x" * 500)
            for key in (
                "category", "doc_type", "question_type", "section", "sub_type_code",
                "material_source", "language", "sheet_name", "sheet_index", "row",
                "column", "entry_number", "sentence_number", "number", "tags",
            )
        }
        item_id = self.repository.create_item(
            snapshot.workflow_id,
            item_type="vocabulary",
            sequence=0,
            normalized_content="oversized metadata",
            item_identity_key="vocabulary:0",
            metadata=metadata,
        )

        workspace = self.repository.get_workspace(snapshot.workflow_id)
        item = next(item for item in workspace["items"] if item["item_id"] == item_id)
        self.assertLessEqual(
            len(json.dumps(item["metadata"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            4096,
        )
        self.assertIn(
            "ITEM_METADATA_TOO_LARGE",
            {blocker["code"] for blocker in workspace["blockers"]},
        )

    def test_pause_resume_and_restart_takeover_are_fenced_by_server_state(self) -> None:
        workflow_id = self._workflow_with_items()
        accepted = self.repository.command(workflow_id, "generate", 0)
        paused_request = self.repository.command(workflow_id, "pause", accepted.state_version)
        self.assertEqual(paused_request.control_state, "PAUSE_REQUESTED")

        paused = self.repository.acknowledge_pause(workflow_id)
        self.assertEqual(paused.control_state, "PAUSED")
        candidate = next(
            item for item in self.repository.list_active_workflows()
            if item["workflow"]["workflow_id"] == workflow_id
        )
        self.assertTrue(candidate["can_resume"])
        self.assertFalse(candidate["can_takeover"])

        resumed = self.repository.command(workflow_id, "resume", paused.state_version)
        self.assertEqual(resumed.control_state, "RUNNING")
        candidate = next(
            item for item in self.repository.list_active_workflows()
            if item["workflow"]["workflow_id"] == workflow_id
        )
        self.assertTrue(candidate["can_takeover"])

        recovered = self.repository.mark_takeover(workflow_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.execution_state, "RECOVERING")
        self.assertEqual(self.repository.mark_takeover(workflow_id).execution_state, "RECOVERING")

    def test_parsed_workflow_is_not_recovered_as_an_implicit_generation(self) -> None:
        """解析页的 PREPARING 状态不能被后台调度器当成已确认生成。"""
        snapshot = self.repository.create_workflow("tts", {})
        parsed = self.repository.command(snapshot.workflow_id, "parse", snapshot.state_version)

        self.assertEqual(parsed.execution_state, "PREPARING")
        visible = self.repository.list_active_workflows()
        visible_candidate = next(item for item in visible if item["workflow"]["workflow_id"] == parsed.workflow_id)
        self.assertFalse(visible_candidate["generation_accepted"])
        self.assertFalse(visible_candidate["can_takeover"])
        self.assertEqual(self.repository.list_active_workflows(recoverable_only=True), [])

        accepted = self.repository.command(parsed.workflow_id, "generate", parsed.state_version)
        candidates = self.repository.list_active_workflows()
        candidate = next(
            item for item in candidates
            if item["workflow"]["workflow_id"] == accepted.workflow_id
        )
        self.assertTrue(candidate["can_takeover"])

    def test_all_skipped_generation_closes_locally_without_provider_submission(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"generation_mode": "composite_cut"})
        item_ids = [
            self.repository.create_item(
                snapshot.workflow_id,
                item_type="sentence",
                sequence=sequence,
                normalized_content=f"line {sequence}",
                item_identity_key=f"sentence:{sequence}",
            )
            for sequence in range(2)
        ]
        patched = self.repository.patch_draft(
            snapshot.workflow_id,
            snapshot.state_version,
            expected_configuration_revision=1,
            item_overrides=[
                {"item_id": item_id, "patch": {"status": "SKIPPED"}}
                for item_id in item_ids
            ],
        )

        from application.workflow_service import WorkflowApplicationService
        from workflow.providers import ProviderRegistry

        artifacts = ArtifactStore(Path(self.temp.name) / "all-skipped-artifacts")
        service = WorkflowApplicationService(
            self.repository,
            SourceImportService(self.database, artifacts),
            artifacts,
            providers=ProviderRegistry(),
        )
        accepted = service.accept_generation(
            snapshot.workflow_id,
            expected_state_version=patched.state_version,
        )
        result = service.run_generation(snapshot.workflow_id)

        self.assertEqual(accepted.execution_state, "RUNNING")
        self.assertEqual(result.status, "SUCCEEDED")
        terminal = self.repository.get_workflow(snapshot.workflow_id)
        self.assertEqual(
            (terminal.result_status, terminal.execution_state, terminal.control_state),
            ("SUCCEEDED", "TERMINAL", "TERMINATED"),
        )
        workspace = self.repository.get_workspace(snapshot.workflow_id)
        self.assertEqual(workspace["progress"]["skipped"], 2)
        self.assertEqual(workspace["progress"]["pending"], 0)
        self.assertEqual(workspace["delivery"]["included_item_ids"], [])
        self.assertEqual(set(workspace["delivery"]["excluded_item_ids"]), set(item_ids))
        with self.database.read_transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_submissions").fetchone()[0], 0)

    def test_all_skipped_generation_can_close_during_pause_request(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"generation_mode": "composite_cut"})
        item_id = self.repository.create_item(
            snapshot.workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="already skipped",
            item_identity_key="sentence:already-skipped",
        )
        patched = self.repository.patch_draft(
            snapshot.workflow_id,
            snapshot.state_version,
            expected_configuration_revision=1,
            item_overrides=[{"item_id": item_id, "patch": {"status": "SKIPPED"}}],
        )
        accepted = self.repository.command(
            snapshot.workflow_id,
            "generate",
            patched.state_version,
        )
        pause_requested = self.repository.command(
            snapshot.workflow_id,
            "pause",
            accepted.state_version,
        )

        completed = self.repository.complete_skipped_workflow(snapshot.workflow_id)
        self.assertEqual(pause_requested.control_state, "PAUSE_REQUESTED")
        self.assertEqual(
            (completed.result_status, completed.execution_state, completed.control_state),
            ("SUCCEEDED", "TERMINAL", "TERMINATED"),
        )

    def test_tts_plan_materializes_selected_default_and_role_voices(self) -> None:
        snapshot = self.repository.create_workflow(
            "tts",
            {
                "generation_mode": "composite_cut",
                "default_female_voice": "speaker:linda",
                "default_male_voice": "speaker:steve",
                "role_voices": {"teacher": "speaker:teacher"},
                "role_configs": {
                    "__default_female__": {"rate": 62, "pitch": 48, "volume": 55},
                    "__default_male__": {"rate": 31, "pitch": 52, "volume": 49},
                    "role:teacher": {"rate": 71, "pitch": 44, "volume": 58},
                },
            },
        )
        for sequence, role in enumerate((None, "Mr Fox", "Teacher")):
            self.repository.create_item(
                snapshot.workflow_id,
                item_type="sentence",
                sequence=sequence,
                normalized_content=f"line {sequence}",
                item_identity_key=f"sentence:{sequence}",
                role=role,
            )

        result = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "config-plan-artifacts"),
        ).run_tts(snapshot.workflow_id, FakeProvider())
        self.assertEqual(result.status, "SUCCEEDED")
        with self.database.read_transaction() as con:
            row = con.execute("SELECT ordered_plan_json FROM provider_submissions").fetchone()
        plan = json.loads(row["ordered_plan_json"])
        self.assertEqual(
            [(item["voice_key"], item["speed"], item["pitch"], item["volume"]) for item in plan],
            [
                ("speaker:linda", 62, 48, 55),
                ("speaker:steve", 31, 52, 49),
                ("speaker:teacher", 71, 44, 58),
            ],
        )

    def test_preview_plan_is_bounded_and_terminalizes_as_partial_success(self) -> None:
        snapshot = self.repository.create_workflow(
            "tts",
            {"generation_mode": "composite_cut", "preview": True},
        )
        for sequence in range(4):
            self.repository.create_item(
                snapshot.workflow_id,
                item_type="sentence",
                sequence=sequence,
                normalized_content=f"preview line {sequence}",
                item_identity_key=f"sentence:{sequence}",
            )

        provider = FakeProvider()
        result = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "preview-artifacts"),
        ).run_tts(snapshot.workflow_id, provider)

        self.assertEqual(result.status, "SUCCEEDED")
        terminal = self.repository.get_workflow(snapshot.workflow_id)
        self.assertEqual(terminal.result_status, "PARTIAL_SUCCESS")
        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT ordered_plan_json FROM provider_submissions WHERE workflow_group_id=?",
                (terminal.workflow_group_id,),
            ).fetchone()
        self.assertEqual(len(json.loads(row["ordered_plan_json"])), 3)
        self.assertEqual(len(result.artifact_ids), 4)  # one composite + three segments

        rerun = self.repository.create_rerun(
            snapshot.workflow_id,
            expected_group_state_version=terminal.group_state_version,
            reason="preview-to-full",
        )
        configured = self.repository.patch_draft(
            rerun.workflow_id,
            rerun.state_version,
            configuration={"generation_mode": "composite_cut", "preview": False},
        )
        full = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "preview-full-artifacts"),
        ).run_tts(rerun.workflow_id, provider)
        self.assertEqual(full.status, "SUCCEEDED")
        self.assertEqual(self.repository.get_workflow(rerun.workflow_id).result_status, "SUCCEEDED")
        self.assertGreater(configured.state_version, rerun.state_version)
        with self.database.read_transaction() as con:
            plans = con.execute(
                "SELECT ordered_plan_json FROM provider_submissions WHERE workflow_group_id=? ORDER BY created_at",
                (terminal.workflow_group_id,),
            ).fetchall()
        self.assertEqual([len(json.loads(row["ordered_plan_json"])) for row in plans], [3, 4])

    def test_pre_submission_failure_allows_voice_change_before_safe_retry(self) -> None:
        snapshot = self.repository.create_workflow(
            "tts",
            {
                "generation_mode": "composite_cut",
                "default_female_voice": "speaker:linda",
            },
        )
        self.repository.create_item(
            snapshot.workflow_id,
            item_type="sentence",
            sequence=0,
            normalized_content="hello",
            item_identity_key="sentence:0",
        )
        provider = FakeProvider()
        provider.fail_mode = "before"
        engine = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "pre-submission-retry-artifacts"),
        )

        first = engine.run_tts(snapshot.workflow_id, provider)
        self.assertEqual(first.status, "WAITING_RETRY")
        current = self.repository.get_workflow(snapshot.workflow_id)
        changed = self.repository.patch_draft(
            snapshot.workflow_id,
            current.state_version,
            configuration={
                "generation_mode": "composite_cut",
                "default_female_voice": "speaker:changed",
            },
        )
        self.assertEqual(
            self.repository.get_configuration(snapshot.workflow_id)["default_female_voice"],
            "speaker:changed",
        )
        self.assertGreater(changed.state_version, current.state_version)

        provider.fail_mode = None
        second = engine.run_tts(snapshot.workflow_id, provider)
        self.assertEqual(second.status, "SUCCEEDED")
        with self.database.read_transaction() as con:
            plans = con.execute(
                "SELECT ordered_plan_json FROM provider_submissions WHERE workflow_group_id=? ORDER BY created_at",
                (changed.workflow_group_id,),
            ).fetchall()
        self.assertEqual(len(plans), 2)
        self.assertEqual(json.loads(plans[-1]["ordered_plan_json"])[0]["voice_key"], "speaker:changed")

    def test_cancel_cleanup_terminalizes_work_without_a_provider_submission(self) -> None:
        workflow_id = self._workflow_with_items()
        snapshot = self.repository.get_workflow(workflow_id)
        terminating = self.repository.command(
            workflow_id,
            "cancel",
            snapshot.state_version,
            reason="test-cancel",
        )
        self.assertEqual((terminating.execution_state, terminating.control_state), ("BLOCKED", "TERMINATING"))

        finalized = self.repository.finalize_generation_cleanup(workflow_id, reason="test-cancel")

        self.assertEqual(
            (finalized.result_status, finalized.execution_state, finalized.control_state, finalized.cleanup_state),
            ("CANCELLED", "TERMINAL", "TERMINATED", "SUCCEEDED"),
        )
        with self.database.read_transaction() as con:
            item_statuses = [row[0] for row in con.execute(
                "SELECT status FROM work_items WHERE workflow_id=? ORDER BY sequence",
                (workflow_id,),
            ).fetchall()]
            event_type = con.execute(
                "SELECT event_type FROM workflow_events WHERE workflow_id=? ORDER BY seq DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()[0]
        self.assertEqual(item_statuses, ["CANCELLED", "CANCELLED"])
        self.assertEqual(event_type, "WORKFLOW_CANCELLED")

    def test_cancel_cleanup_releases_local_resources_but_keeps_ambiguous_side_effect_blocked(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "after"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "cancel-artifacts"))
        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "AMBIGUOUS")

        snapshot = self.repository.get_workflow(workflow_id)
        self.repository.command(workflow_id, "cancel", snapshot.state_version, reason="test-cancel-ambiguous")
        finalized = self.repository.finalize_generation_cleanup(workflow_id, reason="test-cancel-ambiguous")

        self.assertEqual(finalized.cleanup_state, "SUCCEEDED")
        self.assertEqual((finalized.result_status, finalized.control_state), ("IN_PROGRESS", "TERMINATING"))
        # The unresolved provider side effect keeps the workflow blocked even
        # though the local generation resource has already been cleaned up.
        self.assertEqual(finalized.execution_state, "BLOCKED")
        with self.database.read_transaction() as con:
            submission_state = con.execute(
                "SELECT side_effect_state FROM provider_submissions WHERE workflow_group_id=?",
                (finalized.workflow_group_id,),
            ).fetchone()[0]
        self.assertEqual(submission_state, "AMBIGUOUS")

    def test_multiple_runs_reuse_the_same_immutable_definition_snapshot(self) -> None:
        first = self.repository.create_workflow("tts", {"voice": "a"})
        second = self.repository.create_workflow("tts", {"voice": "b"})
        with self.database.read_transaction() as con:
            definitions = con.execute("SELECT workflow_definition_id FROM workflow_definitions").fetchall()
            workflow_definitions = con.execute(
                "SELECT workflow_definition_id FROM workflows ORDER BY workflow_id"
            ).fetchall()
        self.assertEqual(len(definitions), 1)
        self.assertNotEqual(first.workflow_group_id, second.workflow_group_id)
        self.assertEqual({row[0] for row in workflow_definitions}, {definitions[0][0]})

        with self.assertRaises(ConflictError):
            self.repository.create_workflow(
                "tts",
                {},
                definition_snapshot={"workflow_type": "tts", "steps": [{"key": "different"}]},
            )

    def test_event_seq_is_idempotent_and_old_cursor_is_expired_after_compaction(self) -> None:
        snapshot = self.repository.create_workflow("tts", {})
        event = self.events.append(
            snapshot.workflow_id,
            "TEST_EVENT",
            {"value": 1},
            mutation_id="mutation-test-1",
            actor_type="SYSTEM",
        )
        duplicate = self.events.append(
            snapshot.workflow_id,
            "TEST_EVENT",
            {"value": 1},
            mutation_id="mutation-test-1",
            actor_type="SYSTEM",
        )
        self.assertEqual(event.event_id, duplicate.event_id)
        self.assertEqual(event.seq, 2)
        with self.assertRaises(EventStoreError):
            self.events.append(
                snapshot.workflow_id,
                "TEST_EVENT",
                {"value": 999},
                mutation_id="mutation-test-1",
                actor_type="SYSTEM",
            )
        second = self.events.append(snapshot.workflow_id, "TEST_EVENT_2", {}, actor_type="SYSTEM")
        self.events.write_snapshot(snapshot.workflow_id, {"state": "latest"}, snapshot_seq=second.seq)
        self.assertEqual(self.events.compact(snapshot.workflow_id, before_seq=event.seq), 1)
        with self.assertRaises(CursorExpired):
            self.events.read_after(snapshot.workflow_id, after_seq=0)
        self.assertEqual(
            [item.seq for item in self.events.read_after(snapshot.workflow_id, last_event_id=event.event_id)],
            [second.seq],
        )

    def test_event_sequence_is_restored_when_a_duplicate_insert_error_is_caught(self) -> None:
        first = self.repository.create_workflow("tts", {})
        second = self.repository.create_workflow("tts", {})
        events = self.events

        def append_after_caught_error(
            target_workflow_id: str,
            competing_workflow_id: str,
            mutation_id: str,
            competing_payload: dict[str, object],
            attempted_payload: dict[str, object],
        ) -> int:
            before = self.repository.get_workflow(target_workflow_id).latest_seq
            original_transaction = self.database.transaction

            class EmptyResult:
                @staticmethod
                def fetchone():
                    return None

            class RacingConnection:
                def __init__(self, connection):
                    self.connection = connection
                    self.injected = False

                def execute(self, sql, params=()):
                    if (
                        not self.injected
                        and "SELECT * FROM workflow_events WHERE mutation_id=?" in sql
                    ):
                        self.injected = True
                        events.append_in_transaction(
                            self.connection,
                            competing_workflow_id,
                            "RACE_EVENT",
                            competing_payload,
                            mutation_id=mutation_id,
                            actor_type="SYSTEM",
                        )
                        return EmptyResult()
                    return self.connection.execute(sql, params)

            @contextmanager
            def racing_transaction():
                with original_transaction() as connection:
                    yield RacingConnection(connection)

            with patch.object(self.database, "transaction", racing_transaction):
                with self.database.transaction() as connection:
                    with self.assertRaises(EventStoreError):
                        events.append_in_transaction(
                            connection,
                            target_workflow_id,
                            "RACE_EVENT",
                            attempted_payload,
                            mutation_id=mutation_id,
                            actor_type="SYSTEM",
                        )
                    following = events.append_in_transaction(
                        connection,
                        target_workflow_id,
                        "AFTER_RACE",
                        {},
                        actor_type="SYSTEM",
                    )
            expected_next = before + (2 if target_workflow_id == competing_workflow_id else 1)
            self.assertEqual(following.seq, expected_next)
            return following.seq

        cross_stream_seq = append_after_caught_error(
            second.workflow_id,
            first.workflow_id,
            "mutation-cross-stream-race",
            {"value": "winner"},
            {"value": "loser"},
        )
        same_stream_seq = append_after_caught_error(
            second.workflow_id,
            second.workflow_id,
            "mutation-payload-race",
            {"value": "winner"},
            {"value": "different"},
        )
        self.assertEqual(same_stream_seq, cross_stream_seq + 2)

    def test_deleted_non_anchor_cursor_is_expired_after_compaction(self) -> None:
        snapshot = self.repository.create_workflow("tts", {})
        first = self.events.append(snapshot.workflow_id, "FIRST", {}, actor_type="SYSTEM")
        second = self.events.append(snapshot.workflow_id, "SECOND", {}, actor_type="SYSTEM")
        third = self.events.append(snapshot.workflow_id, "THIRD", {}, actor_type="SYSTEM")
        self.events.write_snapshot(snapshot.workflow_id, {"state": "latest"}, snapshot_seq=third.seq)
        self.assertEqual(self.events.compact(snapshot.workflow_id, before_seq=third.seq), 3)

        with self.assertRaises(CursorExpired):
            self.events.read_after(snapshot.workflow_id, last_event_id=first.event_id)
        with self.assertRaises(CursorExpired):
            self.events.read_after(snapshot.workflow_id, last_event_id=second.event_id)

    def test_snapshot_sse_frame_exposes_event_id_for_reconnect_cursor(self) -> None:
        frame = self.events.snapshot_frame(
            {"status": "PREPARING"},
            workflow_id="workflow-1",
            seq=3,
            event_id="event-3",
        )

        self.assertTrue(frame.startswith("id: event-3\nevent: snapshot\n"))
        self.assertIn('"snapshot_event_id":"event-3"', frame)
        self.assertIn('"workflow_id":"workflow-1"', frame)

    def test_source_generation_promotes_verified_blob_without_leaking_internal_keys(self) -> None:
        snapshot = self.repository.create_workflow("tts", {})
        artifacts = ArtifactStore(Path(self.temp.name) / "artifacts")
        service = SourceImportService(self.database, artifacts)
        created = service.create_import(
            snapshot.workflow_id,
            metadata={"filename": "lesson.docx"},
            expected_size_bytes=5,
        )
        grant = service.acquire_writer(
            created["source_import_id"],
            1,
            expected_state_version=created["state_version"],
        )
        ready = service.write_generation(
            created["source_import_id"],
            1,
            io.BytesIO(b"hello"),
            grant=grant.token,
            format="bin",
        )
        self.assertEqual(ready["status"], "READY")
        self.assertNotIn("staging_key", ready)
        public = service.get_import(created["source_import_id"])
        self.assertEqual(public["source_artifact_id"], ready["source_artifact_id"])
        self.assertEqual(self.repository.get_workflow(snapshot.workflow_id).state_version, snapshot.state_version + 1)
        workspace = self.repository.get_workspace(snapshot.workflow_id)
        source_projection = next(
            artifact for artifact in workspace["artifacts"]
            if artifact["artifact_id"] == ready["source_artifact_id"]
        )
        self.assertEqual(source_projection["format"], "docx")
        self.assertEqual(source_projection["extension"], ".docx")
        self.assertEqual(
            source_projection["mime_type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT b.storage_key FROM artifacts a JOIN artifact_blobs b ON b.blob_id=a.blob_id WHERE a.artifact_id=?",
                (ready["source_artifact_id"],),
            ).fetchone()
        with artifacts.read(row["storage_key"]) as handle:
            self.assertEqual(handle.read(), b"hello")

    def test_source_writer_rechecks_persisted_lease_expiry_before_staging(self) -> None:
        snapshot = self.repository.create_workflow("tts", {})
        artifacts = ArtifactStore(Path(self.temp.name) / "expired-writer-artifacts")
        service = SourceImportService(self.database, artifacts)
        created = service.create_import(snapshot.workflow_id, metadata={"filename": "expired.docx"})
        grant = service.acquire_writer(
            created["source_import_id"], 1, expected_state_version=created["state_version"]
        )
        with self.database.transaction() as con:
            con.execute(
                "UPDATE source_import_generations SET expires_at=? WHERE source_import_id=? AND generation=1",
                ("2000-01-01T00:00:00.000Z", created["source_import_id"]),
            )

        with self.assertRaises(SourceImportError) as caught:
            service.write_generation(
                created["source_import_id"], 1, io.BytesIO(b"late"), grant=grant.token
            )
        self.assertEqual(caught.exception.code, "STALE_ATTEMPT")
        self.assertEqual(list(artifacts.staging_root.glob("*.part")), [])

    def test_workspace_source_projection_preserves_xlsx_for_long_filename(self) -> None:
        snapshot = self.repository.create_workflow("tts", {})
        artifacts = ArtifactStore(Path(self.temp.name) / "long-xlsx-artifacts")
        service = SourceImportService(self.database, artifacts)
        filename = f"{'v' * 300}.xlsx"
        created = service.create_import(
            snapshot.workflow_id,
            metadata={"filename": filename},
            expected_size_bytes=5,
        )
        grant = service.acquire_writer(
            created["source_import_id"], 1, expected_state_version=created["state_version"]
        )
        ready = service.write_generation(
            created["source_import_id"], 1, io.BytesIO(b"hello"), grant=grant.token, format="bin"
        )

        workspace = self.repository.get_workspace(snapshot.workflow_id)
        source_projection = next(
            artifact for artifact in workspace["artifacts"]
            if artifact["artifact_id"] == ready["source_artifact_id"]
        )
        self.assertEqual(len(source_projection["filename"]), 256)
        self.assertTrue(source_projection["filename"].endswith(".xlsx"))
        self.assertEqual(source_projection["format"], "xlsx")
        self.assertEqual(source_projection["extension"], ".xlsx")
        self.assertEqual(
            source_projection["mime_type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        history = self.repository.list_history_records(limit=20)
        history_record = next(record for record in history if record["workflow_id"] == snapshot.workflow_id)
        self.assertEqual(len(history_record["source_filename"]), 256)
        self.assertTrue(history_record["source_filename"].endswith(".xlsx"))

    def test_promote_rejects_a_staged_file_outside_managed_staging_root(self) -> None:
        artifacts = ArtifactStore(Path(self.temp.name) / "confined-artifacts")
        outside = Path(self.temp.name) / "outside.part"
        outside.write_bytes(b"outside")
        digest = content_hash(b"outside")
        staged = StagedFile("staging/outside.part", outside, len(b"outside"), digest)

        with self.assertRaises(ArtifactIntegrityError):
            artifacts.promote(staged, format="bin")
        self.assertTrue(outside.exists())

    def test_read_rejects_a_symbolic_link_even_when_target_stays_inside_root(self) -> None:
        artifacts = ArtifactStore(Path(self.temp.name) / "symlink-artifacts")
        blob_dir = artifacts.blob_root / "aa"
        blob_dir.mkdir(parents=True)
        (blob_dir / "actual.bin").write_bytes(b"private")
        (blob_dir / "link.bin").symlink_to("actual.bin")

        with self.assertRaises(ArtifactIntegrityError):
            artifacts.read("blobs/aa/link.bin")

    def test_ticket_is_bound_and_one_time(self) -> None:
        clock_value = [100.0]
        manager = OneTimeTicketManager(clock=lambda: clock_value[0])
        token, _ = manager.issue(action="events", resource_id="workflow-1", audience="renderer", ttl_seconds=10)
        manager.consume(token, action="events", resource_id="workflow-1", audience="renderer")
        with self.assertRaises(TicketError):
            manager.consume(token, action="events", resource_id="workflow-1", audience="renderer")
        token, _ = manager.issue(action="events", resource_id="workflow-1", audience="renderer", ttl_seconds=10)
        clock_value[0] = 111.0
        with self.assertRaises(TicketExpired):
            manager.consume(token, action="events", resource_id="workflow-1", audience="renderer")

    def test_lease_fencing_and_persistent_budget(self) -> None:
        snapshot = self.repository.create_workflow("tts", {})
        lease_id, token, _ = self.repository.acquire_lease(snapshot.workflow_id, "provider", "fake", "owner-a")
        with self.assertRaises(ConflictError):
            self.repository.acquire_lease(snapshot.workflow_id, "provider", "fake", "owner-b")
        self.repository.release_lease(lease_id, "owner-a", token)
        _, newer_token, _ = self.repository.acquire_lease(snapshot.workflow_id, "provider", "fake", "owner-b")
        self.assertGreater(newer_token, token)
        with self.assertRaises(ConflictError):
            self.repository.renew_lease(lease_id, "owner-a", token)

        budget_id = self.repository.reserve_budget(snapshot.workflow_group_id, "tts-intent", budget_kind="tts", max_attempts=1)
        self.repository.commit_budget_use(budget_id)
        with self.assertRaises(BudgetExhausted):
            self.repository.reserve_budget(snapshot.workflow_group_id, "tts-intent", budget_kind="tts", max_attempts=1)
        with self.assertRaises(BudgetExhausted):
            self.repository.reserve_budget(
                snapshot.workflow_group_id,
                "elapsed-budget",
                budget_kind="tts",
                max_elapsed_ms=0,
            )

    def test_idempotency_rejects_same_key_with_different_body(self) -> None:
        first, cached = self.repository.begin_idempotency(
            scope="workflow:create", client_key="client-key-123456", command_name="create",
            method="POST", resource_id=None, target=None, request={"a": 1},
        )
        self.assertIsNone(cached)
        self.repository.complete_idempotency(first, response_status=201, response={"ok": True})
        _, cached = self.repository.begin_idempotency(
            scope="workflow:create", client_key="client-key-123456", command_name="create",
            method="POST", resource_id=None, target=None, request={"a": 1},
        )
        self.assertEqual(cached, {"ok": True})
        with self.assertRaises(ConflictError):
            self.repository.begin_idempotency(
                scope="workflow:create", client_key="client-key-123456", command_name="create",
                method="POST", resource_id=None, target=None, request={"a": 2},
            )

    def test_idempotency_binds_command_resource_and_target(self) -> None:
        target = {"target_type": "ITEM", "item_id": "item-a"}
        first, cached = self.repository.begin_idempotency(
            scope="workflow:shared", client_key="metadata-key-123456", command_name="retry",
            method="POST", resource_id="workflow-1", target=target,
            request={"expected_state_version": 3},
        )
        self.assertIsNone(cached)
        self.repository.complete_idempotency(first, response_status=202, response={"accepted": True})

        with self.assertRaises(IdempotencyConflict):
            self.repository.begin_idempotency(
                scope="workflow:shared", client_key="metadata-key-123456", command_name="retry",
                method="POST", resource_id="workflow-1",
                target={"target_type": "ITEM", "item_id": "item-b"},
                request={"expected_state_version": 3},
            )
        with self.assertRaises(IdempotencyConflict):
            self.repository.begin_idempotency(
                scope="workflow:shared", client_key="metadata-key-123456", command_name="cancel",
                method="POST", resource_id="workflow-1", target=target,
                request={"expected_state_version": 3},
            )
        with self.assertRaises(IdempotencyConflict):
            self.repository.begin_idempotency(
                scope="workflow:shared", client_key="metadata-key-123456", command_name="retry",
                method="PATCH", resource_id="workflow-1", target=target,
                request={"expected_state_version": 3},
            )

        _, cached = self.repository.begin_idempotency(
            scope="workflow:shared", client_key="metadata-key-123456", command_name="retry",
            method="POST", resource_id="workflow-1", target=target,
            request={"expected_state_version": 3},
        )
        self.assertEqual(cached, {"accepted": True})

    def test_idempotency_competing_insert_rechecks_and_replays(self) -> None:
        """A UNIQUE loser must replay the reservation created by the winner."""

        original_transaction = self.database.transaction

        class RacingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.injected = False

            def execute(self, sql, params=()):
                if (
                    not self.injected
                    and "INSERT INTO workflow_idempotency_keys" in sql
                ):
                    self.injected = True
                    # Insert the competing winner through the real connection,
                    # then make this caller observe the UNIQUE loser.  The
                    # outer transaction is still alive, so the subsequent
                    # re-query sees the winner and can replay it.
                    result = self.connection.execute(sql, params)
                    self.connection.execute(
                        "UPDATE workflow_idempotency_keys "
                        "SET response_status=?, response_json=? WHERE idempotency_id=?",
                        (202, json.dumps({"accepted": True}), params[0]),
                    )
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint failed: workflow_idempotency_keys.scope_hash, "
                        "workflow_idempotency_keys.client_key"
                    )
                return self.connection.execute(sql, params)

        @contextmanager
        def racing_transaction():
            with original_transaction() as connection:
                yield RacingConnection(connection)

        with patch.object(self.database, "transaction", racing_transaction):
            reservation_id, cached = self.repository.begin_idempotency(
                scope="workflow:race",
                client_key="race-key-123456",
                command_name="create",
                method="POST",
                resource_id=None,
                target=None,
                request={"value": 1},
            )

        self.assertTrue(reservation_id)
        self.assertEqual(cached, {"accepted": True})

        # A second attempt now observes the durable winner normally; the
        # defensive race path did not create a duplicate row.
        with self.assertRaises(ConflictError):
            self.repository.begin_idempotency(
                scope="workflow:race",
                client_key="race-key-123456",
                command_name="create",
                method="POST",
                resource_id=None,
                target=None,
                request={"value": 2},
            )

    def test_idempotency_second_competing_insert_is_in_progress(self) -> None:
        original_transaction = self.database.transaction

        class RacingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.insert_attempts = 0

            def execute(self, sql, params=()):
                if "INSERT INTO workflow_idempotency_keys" in sql:
                    self.insert_attempts += 1
                    if self.insert_attempts == 1:
                        self.connection.execute(sql, params)
                        self.connection.execute(
                            "UPDATE workflow_idempotency_keys SET expires_at=? WHERE idempotency_id=?",
                            ("2000-01-01T00:00:00+00:00", params[0]),
                        )
                        raise sqlite3.IntegrityError("UNIQUE constraint failed: competing reservation")
                    if self.insert_attempts == 2:
                        self.connection.execute(sql, params)
                        raise sqlite3.IntegrityError("UNIQUE constraint failed: competing reservation")
                return self.connection.execute(sql, params)

        @contextmanager
        def racing_transaction():
            with original_transaction() as connection:
                yield RacingConnection(connection)

        with patch.object(self.database, "transaction", racing_transaction):
            with self.assertRaises(IdempotencyInProgress):
                self.repository.begin_idempotency(
                    scope="workflow:expired-race",
                    client_key="expired-race-key-123456",
                    command_name="create",
                    method="POST",
                    resource_id=None,
                    target=None,
                    request={"value": 1},
                )

    def _workflow_with_items(self) -> str:
        snapshot = self.repository.create_workflow("tts", {"mode": "composite_cut"})
        for sequence, content in enumerate(("hello", "world")):
            self.repository.create_item(
                snapshot.workflow_id,
                item_type="sentence",
                sequence=sequence,
                normalized_content=content,
                item_identity_key=f"lesson:{sequence}",
                role="default",
                voice_key="fake",
            )
        return snapshot.workflow_id

    def test_workspace_keeps_non_mp3_tts_artifacts_out_of_delivery(self) -> None:
        workflow_id = self._workflow_with_items()
        result = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "non-mp3-artifacts"),
        ).run_tts(workflow_id, FakeProvider(output_format="bin"))

        self.assertEqual(result.status, "SUCCEEDED")
        workspace = self.repository.get_workspace(workflow_id)
        self.assertEqual(workspace["progress"]["completed"], 0)
        self.assertEqual(workspace["progress"]["deliverable"], 0)
        self.assertEqual(workspace["progress"]["pending"], 2)
        self.assertEqual(workspace["delivery"]["included_item_ids"], [])
        self.assertIn(
            "ARTIFACT_FORMAT_UNSUPPORTED",
            {blocker["code"] for blocker in workspace["blockers"]},
        )
        self.assertEqual(self.repository.list_verified_tts_segments(workflow_id), [])

    def test_workspace_blocks_artifact_blob_metadata_conflict(self) -> None:
        workflow_id = self._workflow_with_items()
        result = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "metadata-conflict-artifacts"),
        ).run_tts(workflow_id, FakeProvider(output_format="mp3"))
        self.assertEqual(result.status, "SUCCEEDED")
        with self.database.transaction() as con:
            # Healthy databases reject this mutation at the trigger boundary.
            # Drop only the guard in this isolated corruption fixture so the
            # workspace projection can prove it fails closed for legacy or
            # externally-corrupted rows that predate the guard.
            con.execute("DROP TRIGGER artifacts_ready_guard_update")
            con.execute(
                "UPDATE artifacts SET format='wav' WHERE artifact_id=?",
                (result.artifact_ids[-1],),
            )

        workspace = self.repository.get_workspace(workflow_id)
        self.assertEqual(workspace["progress"]["completed"], 1)
        self.assertEqual(workspace["progress"]["deliverable"], 1)
        self.assertEqual(workspace["progress"]["pending"], 1)
        self.assertIn(
            "ARTIFACT_METADATA_CONFLICT",
            {blocker["code"] for blocker in workspace["blockers"]},
        )
        self.assertEqual(len(workspace["delivery"]["included_item_ids"]), 1)

    def test_latest_invalid_tts_artifact_does_not_revive_an_older_mp3(self) -> None:
        workflow_id = self._workflow_with_items()
        artifact_root = Path(self.temp.name) / "latest-artifact-fence"
        store = ArtifactStore(artifact_root)
        result = WorkflowEngine(self.repository, store).run_tts(workflow_id, FakeProvider())
        self.assertEqual(result.status, "SUCCEEDED")

        first_item_id = self.repository.list_items(workflow_id)[0]["item_id"]
        staged = store.stage_stream(io.BytesIO(b"not an audio file"))
        invalid_blob = store.promote(staged, format="wav")
        self.repository.attach_imported_artifact(
            workflow_id,
            artifact_id="artifact-latest-invalid",
            blob=invalid_blob,
            artifact_type="tts-segment",
            producer="corruption-fixture",
            producer_version="1",
            item_id=first_item_id,
        )

        # The second item remains deliverable, but the first item must not
        # fall back to its older MP3 now that a newer invalid TTS artifact is
        # the authoritative result for that item.
        segments = self.repository.list_verified_tts_segments(workflow_id)
        self.assertEqual({str(row["item_id"]) for row in segments}, {
            str(self.repository.list_items(workflow_id)[1]["item_id"]),
        })
        history = self.repository.list_history_records(limit=20)
        self.assertEqual(history[0]["available_files"], 1)
        self.assertEqual(history[0]["completed"], 1)
        self.assertEqual(history[0]["failed"], 1)

        workspace = self.repository.get_workspace(workflow_id)
        self.assertEqual(workspace["progress"]["deliverable"], 1)
        self.assertIn("ARTIFACT_FORMAT_UNSUPPORTED", {
            blocker["code"] for blocker in workspace["blockers"]
        })

    def test_fake_provider_vertical_chain_persists_receipt_segments_and_artifacts(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "engine-artifacts"))

        result = engine.run_tts(workflow_id, provider)

        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(len(result.artifact_ids), 3)  # composite + one segment per item
        self.assertEqual(provider.submit_calls, 1)
        with self.database.read_transaction() as con:
            submission = con.execute("SELECT * FROM provider_submissions").fetchone()
            receipt = con.execute("SELECT * FROM provider_receipts").fetchone()
            binding = con.execute("SELECT * FROM provider_receipt_bindings").fetchone()
            attempts = con.execute(
                "SELECT attempt_kind, execute_attempt_no, status FROM step_attempts ORDER BY attempt_seq"
            ).fetchall()
            step_projection = con.execute(
                "SELECT current_attempt_id, attempt_count FROM workflow_steps WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        self.assertEqual(submission["side_effect_state"], "CONFIRMED")
        self.assertEqual(receipt["query_status"], "FOUND")
        self.assertEqual(binding["relation_type"], "SUBMITTED")
        self.assertEqual([(row["attempt_kind"], row["execute_attempt_no"], row["status"]) for row in attempts], [("EXECUTE", 1, "SUCCEEDED")])
        self.assertEqual(step_projection["attempt_count"], 1)
        self.assertEqual(step_projection["current_attempt_id"], binding["observed_by_attempt_id"])

        repeated = engine.run_tts(workflow_id, provider)
        self.assertEqual(repeated.status, "SUCCEEDED")
        self.assertEqual(provider.submit_calls, 1)

    def test_tts_derivations_use_the_semantic_primary_artifact_not_list_position(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "reordered-artifacts"))
        original_stage_output = engine._stage_output

        def reorder_specs(plan, output, ordered_plan, *, receipt, generation_mode):
            specs = original_stage_output(
                plan,
                output,
                ordered_plan,
                receipt=receipt,
                generation_mode=generation_mode,
            )
            # Deliberately put one segment before the composite and omit the
            # optional parent index so the repository must identify the
            # primary by its semantic fields.
            first_segment = dict(specs[1])
            first_segment.pop("parent_index", None)
            second_segment = dict(specs[2])
            second_segment.pop("parent_index", None)
            return [first_segment, dict(specs[0]), second_segment]

        with patch.object(engine, "_stage_output", side_effect=reorder_specs):
            result = engine.run_tts(workflow_id, provider)

        self.assertEqual(result.status, "SUCCEEDED")
        with self.database.read_transaction() as con:
            derivations = con.execute(
                """SELECT parent.artifact_type AS parent_type, child.artifact_type AS child_type
                   FROM artifact_derivations d
                   JOIN artifacts parent ON parent.artifact_id=d.parent_artifact_id
                   JOIN artifacts child ON child.artifact_id=d.child_artifact_id
                   WHERE d.relation_type='CUT_SEGMENT' AND child.workflow_id=?""",
                (workflow_id,),
            ).fetchall()
        self.assertEqual(len(derivations), 2)
        self.assertEqual({row["parent_type"] for row in derivations}, {"tts-composite"})
        self.assertEqual({row["child_type"] for row in derivations}, {"tts-segment"})

    def test_tts_receipt_summary_is_redacted_at_the_repository_boundary(self) -> None:
        workflow_id = self._workflow_with_items()

        class ProviderWithSensitiveSummary(FakeProvider):
            def __init__(self):
                super().__init__(output_format="mp3")

            def submit(self, submission_key, payload):
                raw = super().submit(submission_key, payload)
                return ProviderReceipt(
                    provider=raw.provider,
                    account_scope=raw.account_scope,
                    submission_key=raw.submission_key,
                    provider_job_id=raw.provider_job_id,
                    canonical_key=raw.provider_job_id,
                    output=raw.output,
                    segments=raw.segments,
                    output_format="mp3",
                    summary={
                        "authorization": "Bearer must-not-persist",
                        "nested": {"cookie": "session-secret"},
                        "works_name": "safe-work-name",
                    },
                )

        result = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "receipt-redaction-artifacts"),
        ).run_tts(workflow_id, ProviderWithSensitiveSummary())
        self.assertEqual(result.status, "SUCCEEDED")
        with self.database.read_transaction() as con:
            row = con.execute("SELECT receipt_summary_json FROM provider_receipts").fetchone()
        summary = json.loads(row["receipt_summary_json"])
        self.assertEqual(summary["authorization"], "[REDACTED]")
        self.assertEqual(summary["nested"]["cookie"], "[REDACTED]")
        self.assertEqual(summary["works_name"], "safe-work-name")

    def test_source_import_metadata_is_redacted_before_persistence(self) -> None:
        workflow = self.repository.create_workflow("tts", {"mode": "composite_cut"})
        imports = SourceImportService(
            self.database,
            ArtifactStore(Path(self.temp.name) / "metadata-redaction-artifacts"),
        )
        created = imports.create_import(
            workflow.workflow_id,
            metadata={
                "filename": "lesson.docx",
                "nested": {"password": "must-not-persist"},
            },
        )
        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT error_details_json FROM source_imports WHERE source_import_id=?",
                (created["source_import_id"],),
            ).fetchone()
        details = json.loads(row["error_details_json"])
        self.assertEqual(details["metadata"]["nested"]["password"], "[REDACTED]")
        self.assertEqual(details["metadata"]["filename"], "lesson.docx")

    def test_observed_receipt_retries_output_without_resubmitting(self) -> None:
        workflow_id = self._workflow_with_items()

        class DownloadFailsOnce(FakeProvider):
            def __init__(self):
                super().__init__()
                self.download_calls = 0

            def download(self, receipt):
                self.download_calls += 1
                if self.download_calls == 1:
                    raise ArtifactStoreError("simulated output download failure")
                return super().download(receipt)

        provider = DownloadFailsOnce()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "output-retry-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "WAITING_RETRY")
        self.assertEqual(first.error_code, "ARTIFACT_INVALID")
        self.assertEqual(provider.submit_calls, 1)
        with self.database.read_transaction() as con:
            submission = con.execute(
                "SELECT side_effect_state FROM provider_submissions WHERE provider_submission_id=?",
                (first.submission_id,),
            ).fetchone()
            unit = con.execute(
                "SELECT status, side_effect_state FROM work_units WHERE work_unit_id=?",
                (first.work_unit_id,),
            ).fetchone()
            intent = con.execute(
                "SELECT state FROM side_effect_intents WHERE work_unit_id=?",
                (first.work_unit_id,),
            ).fetchone()
        self.assertEqual(submission["side_effect_state"], "SUBMITTED")
        self.assertEqual((unit["status"], unit["side_effect_state"]), ("WAITING_RETRY", "SUBMITTED"))
        self.assertEqual(intent["state"], "COMMITTED")

        second = engine.run_tts(workflow_id, provider)
        self.assertEqual(second.status, "SUCCEEDED")
        self.assertEqual(provider.submit_calls, 1)
        self.assertEqual(provider.download_calls, 2)

    def test_generic_resolution_cannot_downgrade_a_work_unit_with_a_receipt(self) -> None:
        workflow_id = self._workflow_with_items()

        class DownloadFailsOnce(FakeProvider):
            def __init__(self):
                super().__init__()
                self.download_calls = 0

            def download(self, receipt):
                self.download_calls += 1
                if self.download_calls == 1:
                    raise ArtifactStoreError("simulated output download failure")
                return super().download(receipt)

        provider = DownloadFailsOnce()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "resolve-boundary-artifacts"))
        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "WAITING_RETRY")

        snapshot = self.repository.get_workflow(workflow_id)
        with self.database.read_transaction() as con:
            unit = con.execute(
                "SELECT work_unit_id, state_version, side_effect_state FROM work_units WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            receipt = con.execute(
                "SELECT receipt_id, state_version, query_status FROM provider_receipts WHERE provider_submission_id=?",
                (first.submission_id,),
            ).fetchone()
        self.assertEqual(unit["side_effect_state"], "SUBMITTED")
        self.assertEqual(receipt["query_status"], "FOUND")

        with self.assertRaises(ConflictError) as conflict:
            self.repository.targeted_command(
                workflow_id,
                "resolve",
                {"target_type": "WORK_UNIT", "work_unit_id": unit["work_unit_id"]},
                expected_state_version=snapshot.state_version,
                expected_target_state_version=int(unit["state_version"]),
                decision="NOT_SUBMITTED",
                evidence={"source": "stale-operator", "evidence_hash": "n" * 32},
            )
        self.assertEqual(conflict.exception.code, "STATE_CONFLICT")
        with self.database.read_transaction() as con:
            states = con.execute(
                """SELECT p.side_effect_state AS submission_state, u.side_effect_state AS unit_state,
                          r.query_status
                   FROM provider_submissions p
                   JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                   JOIN provider_receipts r ON r.provider_submission_id=p.provider_submission_id
                   WHERE p.provider_submission_id=?""",
                (first.submission_id,),
            ).fetchone()
        self.assertEqual(
            (states["submission_state"], states["unit_state"], states["query_status"]),
            ("SUBMITTED", "SUBMITTED", "FOUND"),
        )

    def test_provider_receipt_resolution_updates_the_parent_projection_without_not_submitted(self) -> None:
        workflow_id = self._workflow_with_items()

        class DownloadFails(FakeProvider):
            def download(self, receipt):
                raise ArtifactStoreError("keep output verification pending")

        provider = DownloadFails()
        first = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "receipt-target-artifacts"),
        ).run_tts(workflow_id, provider)
        self.assertEqual(first.status, "WAITING_RETRY")
        snapshot = self.repository.get_workflow(workflow_id)
        with self.database.read_transaction() as con:
            receipt = con.execute(
                "SELECT receipt_id, state_version FROM provider_receipts WHERE provider_submission_id=?",
                (first.submission_id,),
            ).fetchone()
            unit = con.execute(
                "SELECT work_unit_id, state_version FROM work_units WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()

        with self.assertRaises(ConflictError) as conflict:
            self.repository.targeted_command(
                workflow_id,
                "resolve",
                {"target_type": "PROVIDER_RECEIPT", "provider_receipt_id": receipt["receipt_id"]},
                expected_state_version=snapshot.state_version,
                expected_target_state_version=int(receipt["state_version"]),
                decision="NOT_SUBMITTED",
                evidence={"source": "stale-operator", "evidence_hash": "r" * 32},
            )
        self.assertEqual(conflict.exception.code, "STATE_CONFLICT")

        confirmed = self.repository.targeted_command(
            workflow_id,
            "resolve",
            {"target_type": "PROVIDER_RECEIPT", "provider_receipt_id": receipt["receipt_id"]},
            expected_state_version=snapshot.state_version,
            expected_target_state_version=int(receipt["state_version"]),
            decision="CONFIRMED",
            evidence={"source": "receipt-observed", "evidence_hash": "c" * 32},
        )
        self.assertEqual(confirmed.latest_event["event_type"], "WORKFLOW_RESOLVE_TARGETED")
        with self.database.read_transaction() as con:
            states = con.execute(
                """SELECT p.side_effect_state AS submission_state, u.side_effect_state AS unit_state,
                          u.status AS unit_status, r.query_status
                   FROM provider_submissions p
                   JOIN work_units u ON u.provider_submission_id=p.provider_submission_id
                   JOIN provider_receipts r ON r.provider_submission_id=p.provider_submission_id
                   WHERE p.provider_submission_id=?""",
                (first.submission_id,),
            ).fetchone()
        self.assertEqual(
            (states["submission_state"], states["unit_state"], states["unit_status"], states["query_status"]),
            ("CONFIRMED", "CONFIRMED", "VERIFYING", "FOUND"),
        )

    def test_provider_lookup_failure_persists_ambiguous_instead_of_retrying_output(self) -> None:
        workflow_id = self._workflow_with_items()

        class DownloadThenQueryFails(FakeProvider):
            def __init__(self):
                super().__init__()
                self.download_calls = 0

            def download(self, receipt):
                self.download_calls += 1
                if self.download_calls == 1:
                    raise ArtifactStoreError("simulated output failure")
                return super().download(receipt)

            def query(self, submission_key):
                raise ProviderError(
                    "provider lookup is unavailable",
                    code="TRANSIENT_PROVIDER_ERROR",
                    ambiguous=True,
                )

        provider = DownloadThenQueryFails()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "query-failure-artifacts"))
        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "WAITING_RETRY")
        second = engine.run_tts(workflow_id, provider)
        self.assertEqual(second.status, "AMBIGUOUS")
        with self.database.read_transaction() as con:
            submission = con.execute("SELECT side_effect_state FROM provider_submissions").fetchone()
            workflow = con.execute("SELECT execution_state FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
        self.assertEqual(submission["side_effect_state"], "AMBIGUOUS")
        self.assertEqual(workflow["execution_state"], "WAITING_USER")

    def test_fake_provider_ambiguous_submission_is_reconciled_without_resubmit(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "after"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "engine-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "AMBIGUOUS")
        self.assertEqual(first.error_code, "SUBMISSION_AMBIGUOUS")
        self.assertEqual(provider.submit_calls, 1)

        resumed = engine.run_tts(workflow_id, provider)
        self.assertEqual(resumed.status, "SUCCEEDED")
        self.assertEqual(provider.submit_calls, 1)
        with self.database.read_transaction() as con:
            budget = con.execute("SELECT used_attempts, reserved_attempts FROM retry_budgets").fetchone()
            states = con.execute("SELECT side_effect_state, status FROM work_units").fetchone()
        self.assertEqual((budget["used_attempts"], budget["reserved_attempts"]), (1, 0))
        self.assertEqual((states["side_effect_state"], states["status"]), ("CONFIRMED", "SUCCEEDED"))

    def test_ambiguous_run_routes_reconfigure_to_reconciliation_not_dead_end(self) -> None:
        """终止/断网留下的 AMBIGUOUS run：patch 必须返回可路由的对账错误。

        用户在配置页点击生成时不能再拿到笼统的 CONFIG_FROZEN 死胡同；
        未决副作用必须以 RECONCILIATION_REQUIRED 暴露，且恢复入口
        （attempt/work_unit/目标版本）可以通过 list_open_reconciliations
        重建——应用重启后依然可达。
        """

        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "after"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "reconcile-route-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "AMBIGUOUS")
        snapshot = self.repository.get_workflow(workflow_id)
        self.assertEqual(snapshot.execution_state, "WAITING_USER")

        interventions = self.repository.list_open_reconciliations(workflow_id)
        self.assertEqual(len(interventions), 1)
        intervention = interventions[0]
        self.assertEqual(intervention["attempt_id"], first.attempt_id)
        self.assertTrue(intervention["work_unit_id"])
        self.assertGreaterEqual(intervention["work_unit_state_version"], 0)

        with self.assertRaises(ConflictError) as raised:
            self.repository.patch_draft(
                workflow_id,
                snapshot.state_version,
                configuration={"mode": "composite_cut", "default_female_voice": "speaker:changed"},
            )
        self.assertEqual(raised.exception.code, "RECONCILIATION_REQUIRED")

        # 对账（确认未提交）之后配置恢复可编辑，生成可以继续——文档不会
        # 因为一次网络中断而永久卡死。
        resolved = self.repository.targeted_command(
            workflow_id,
            "resolve",
            {
                "target_type": "WORK_UNIT",
                "work_unit_id": intervention["work_unit_id"],
            },
            expected_state_version=self.repository.get_workflow(workflow_id).state_version,
            expected_target_state_version=intervention["work_unit_state_version"],
            request_id="test-reconcile-not-submitted",
            decision="NOT_SUBMITTED",
            evidence={
                "source": "desktop-user-confirmed-not-submitted",
                "evidence_hash": "test-evidence-hash",
                "summary": "用户确认讯飞作品列表中没有本次作品",
            },
        )
        self.assertEqual(resolved.execution_state, "WAITING_USER")
        with self.database.read_transaction() as con:
            intervention_state = con.execute(
                "SELECT state FROM user_interventions WHERE workflow_id=? AND work_unit_id=?",
                (workflow_id, intervention["work_unit_id"]),
            ).fetchone()[0]
        self.assertEqual(intervention_state, "RESOLVED")
        provider.fail_mode = None
        second = engine.run_tts(workflow_id, provider)
        self.assertEqual(second.status, "SUCCEEDED")

    def test_fake_provider_ambiguous_submission_is_reconciled_without_resubmit(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "after"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "engine-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "AMBIGUOUS")
        self.assertEqual(first.error_code, "SUBMISSION_AMBIGUOUS")
        self.assertEqual(provider.submit_calls, 1)

        resumed = engine.run_tts(workflow_id, provider)
        self.assertEqual(resumed.status, "SUCCEEDED")
        self.assertEqual(provider.submit_calls, 1)
        with self.database.read_transaction() as con:
            budget = con.execute("SELECT used_attempts, reserved_attempts FROM retry_budgets").fetchone()
            states = con.execute("SELECT side_effect_state, status FROM work_units").fetchone()
        self.assertEqual((budget["used_attempts"], budget["reserved_attempts"]), (1, 0))
        self.assertEqual((states["side_effect_state"], states["status"]), ("CONFIRMED", "SUCCEEDED"))

    def test_expired_provider_lease_is_fenced_before_provider_submit(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "lease-fence-artifacts"))

        with patch.object(
            self.repository,
            "renew_lease",
            side_effect=LeaseConflict("provider lease expired"),
        ):
            result = engine.run_tts(workflow_id, provider)

        self.assertEqual(result.status, "AMBIGUOUS")
        self.assertEqual(result.error_code, "STALE_ATTEMPT")
        self.assertEqual(provider.submit_calls, 0)
        with self.database.read_transaction() as con:
            state = con.execute(
                "SELECT side_effect_state, status FROM work_units WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            workflow = con.execute(
                "SELECT execution_state FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            intervention = con.execute(
                "SELECT state FROM user_interventions WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            intent = con.execute(
                "SELECT state FROM side_effect_intents WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        self.assertEqual((state["side_effect_state"], state["status"]), ("AMBIGUOUS", "AMBIGUOUS"))
        self.assertEqual(workflow["execution_state"], "WAITING_USER")
        self.assertEqual(intervention["state"], "OPEN")
        self.assertEqual(intent["state"], "NEEDS_RECONCILE")

    def test_provider_lease_heartbeat_uses_the_acquisition_ttl(self) -> None:
        workflow_id = self._workflow_with_items()

        class SlowProvider(FakeProvider):
            def submit(self, submission_key, payload):
                time.sleep(0.08)
                return super().submit(submission_key, payload)

        provider = SlowProvider()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "heartbeat-artifacts"))
        with patch("workflow.engine.PROVIDER_LEASE_HEARTBEAT_INTERVAL_SECONDS", 0.01), patch.object(
            self.repository,
            "renew_lease",
            wraps=self.repository.renew_lease,
        ) as renew:
            result = engine.run_tts(workflow_id, provider)

        self.assertEqual(result.status, "SUCCEEDED")
        self.assertGreaterEqual(renew.call_count, 4)
        self.assertTrue(all(call.kwargs.get("ttl_seconds") == 300 for call in renew.call_args_list))

    def test_succeeded_work_unit_cannot_be_reset_by_targeted_retry(self) -> None:
        workflow_id = self._workflow_with_items()
        result = WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "succeeded-retry-artifacts"),
        ).run_tts(workflow_id, FakeProvider())
        snapshot = self.repository.get_workflow(workflow_id)
        with self.database.read_transaction() as con:
            unit = con.execute(
                "SELECT work_unit_id, state_version, status, side_effect_state FROM work_units WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()

        with self.assertRaises(ConflictError) as conflict:
            self.repository.targeted_command(
                workflow_id,
                "retry",
                {"target_type": "WORK_UNIT", "work_unit_id": unit["work_unit_id"]},
                expected_state_version=snapshot.state_version,
                expected_target_state_version=int(unit["state_version"]),
            )
        self.assertEqual(conflict.exception.code, "ITEM_ALREADY_DELIVERED")
        with self.database.read_transaction() as con:
            unchanged = con.execute(
                "SELECT status, side_effect_state, state_version FROM work_units WHERE work_unit_id=?",
                (result.work_unit_id,),
            ).fetchone()
        self.assertEqual(
            (unchanged["status"], unchanged["side_effect_state"], unchanged["state_version"]),
            ("SUCCEEDED", "CONFIRMED", int(unit["state_version"])),
        )

    def test_resolve_rejects_step_and_item_targets_with_a_structured_target_error(self) -> None:
        workflow_id = self._workflow_with_items()
        WorkflowEngine(
            self.repository,
            ArtifactStore(Path(self.temp.name) / "resolve-target-artifacts"),
        ).run_tts(workflow_id, FakeProvider())
        snapshot = self.repository.get_workflow(workflow_id)
        with self.database.read_transaction() as con:
            step = con.execute(
                "SELECT step_id, state_version FROM workflow_steps WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            attempt = con.execute(
                "SELECT attempt_id FROM step_attempts WHERE workflow_id=? ORDER BY attempt_seq LIMIT 1",
                (workflow_id,),
            ).fetchone()

        with self.assertRaises(ConflictError) as conflict:
            self.repository.targeted_command(
                workflow_id,
                "resolve",
                {"target_type": "STEP", "step_id": step["step_id"]},
                expected_state_version=snapshot.state_version,
                expected_target_state_version=int(step["state_version"]),
                expected_attempt_id=attempt["attempt_id"],
                decision="BLOCKED",
                evidence={"source": "test", "evidence_hash": "s" * 32},
            )
        self.assertEqual(conflict.exception.code, "TARGET_REQUIRED")

    def test_targeted_generation_keeps_unselected_items_retryable(self) -> None:
        workflow_id = self._workflow_with_items()
        item_ids = [str(item["item_id"]) for item in self.repository.list_items(workflow_id)]
        provider = FakeProvider()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "targeted-progress-artifacts"))

        first = engine.run_tts(workflow_id, provider, item_ids=[item_ids[0]])
        self.assertEqual(first.status, "SUCCEEDED")
        partial = self.repository.get_workflow(workflow_id)
        self.assertEqual((partial.result_status, partial.execution_state, partial.control_state),
                         ("IN_PROGRESS", "WAITING_RETRY", "RUNNING"))
        self.assertEqual(self.repository.list_items(workflow_id)[0]["status"], "SUCCEEDED")
        self.assertEqual(self.repository.list_items(workflow_id)[1]["status"], "PENDING")

        second = engine.run_tts(workflow_id, provider, item_ids=[item_ids[1]])
        self.assertEqual(second.status, "SUCCEEDED")
        terminal = self.repository.get_workflow(workflow_id)
        self.assertEqual((terminal.result_status, terminal.execution_state, terminal.control_state),
                         ("SUCCEEDED", "TERMINAL", "TERMINATED"))
        self.assertEqual(provider.submit_calls, 2)

    def test_reconcile_creates_a_read_only_attempt_for_the_exact_work_unit(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "after"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "reconcile-attempt-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "AMBIGUOUS")
        snapshot = self.repository.get_workflow(workflow_id)
        with self.database.read_transaction() as con:
            work_unit = con.execute(
                "SELECT work_unit_id, state_version FROM work_units WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()

        reconciled = self.repository.targeted_command(
            workflow_id,
            "reconcile",
            {"target_type": "WORK_UNIT", "work_unit_id": work_unit["work_unit_id"]},
            expected_state_version=snapshot.state_version,
            expected_target_state_version=int(work_unit["state_version"]),
            reason="operator requested provider lookup",
        )
        self.assertEqual(reconciled.latest_event["event_type"], "WORKFLOW_RECONCILE_TARGETED")
        reconcile_attempt_id = reconciled.latest_event["payload"]["reconcile_attempt_id"]
        self.assertEqual(reconciled.latest_event["attempt_id"], reconcile_attempt_id)
        with self.database.read_transaction() as con:
            attempt = con.execute(
                "SELECT attempt_kind, execute_attempt_no, status FROM step_attempts WHERE attempt_id=?",
                (reconcile_attempt_id,),
            ).fetchone()
            target = con.execute(
                "SELECT target_type, target_id, expected_state_version, source_attempt_id FROM reconcile_targets WHERE reconcile_attempt_id=?",
                (reconcile_attempt_id,),
            ).fetchone()
        self.assertEqual((attempt["attempt_kind"], attempt["execute_attempt_no"], attempt["status"]), ("RECONCILE", None, "CREATED"))
        self.assertEqual(
            (target["target_type"], target["target_id"], target["expected_state_version"]),
            ("WORK_UNIT", work_unit["work_unit_id"], int(work_unit["state_version"])),
        )
        self.assertEqual(target["source_attempt_id"], None)

    def test_targeted_command_rejects_an_attempt_from_another_target(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        other_step_id = self.repository.create_step(workflow_id, step_key="other", step_type="TTS")
        other_attempt_id = self.repository.create_attempt(workflow_id, other_step_id, attempt_kind="RECONCILE")
        WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "target-fence-artifacts")).run_tts(
            workflow_id, provider,
        )
        with self.database.read_transaction() as con:
            work_unit = con.execute(
                "SELECT work_unit_id, state_version FROM work_units WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        snapshot = self.repository.get_workflow(workflow_id)
        with self.assertRaises(ConflictError) as context:
            self.repository.targeted_command(
                workflow_id,
                "reconcile",
                {"target_type": "WORK_UNIT", "work_unit_id": work_unit["work_unit_id"]},
                expected_state_version=snapshot.state_version,
                expected_target_state_version=int(work_unit["state_version"]),
                expected_attempt_id=other_attempt_id,
            )
        self.assertEqual(context.exception.code, "TARGET_REQUIRED")

    def test_manual_not_submitted_resolution_creates_a_fresh_execute_attempt(self) -> None:
        workflow_id = self._workflow_with_items()

        class AmbiguousWithoutReceiptProvider(FakeProvider):
            def submit(self, submission_key, payload):
                if self.submit_calls == 0:
                    self.submit_calls += 1
                    raise AmbiguousProviderError("simulated browser disconnect before a provider receipt existed")
                return super().submit(submission_key, payload)

        provider = AmbiguousWithoutReceiptProvider()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "engine-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "AMBIGUOUS")
        self.assertEqual(provider.submit_calls, 1)

        snapshot = self.repository.get_workflow(workflow_id)
        with self.database.read_transaction() as con:
            unit = con.execute(
                "SELECT work_unit_id, state_version, status, side_effect_state FROM work_units WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        resolved = self.repository.targeted_command(
            workflow_id,
            "resolve",
            {"target_type": "WORK_UNIT", "work_unit_id": unit["work_unit_id"]},
            expected_state_version=snapshot.state_version,
            expected_target_state_version=int(unit["state_version"]),
            decision="NOT_SUBMITTED",
            evidence={"source": "test-user-confirmed", "evidence_hash": "r" * 32},
        )
        self.assertEqual(resolved.control_state, "RUNNING")

        resumed = engine.run_tts(workflow_id, provider)
        self.assertEqual(resumed.status, "SUCCEEDED")
        self.assertEqual(provider.submit_calls, 2)
        with self.database.read_transaction() as con:
            attempts = con.execute(
                "SELECT attempt_kind, execute_attempt_no, status FROM step_attempts WHERE workflow_id=? ORDER BY attempt_seq",
                (workflow_id,),
            ).fetchall()
        self.assertEqual(
            [(row["attempt_kind"], row["execute_attempt_no"], row["status"]) for row in attempts],
            [("EXECUTE", 1, "AMBIGUOUS"), ("EXECUTE", 2, "SUCCEEDED")],
        )

    def test_fake_provider_failure_before_submission_creates_a_new_execute_attempt_on_retry(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "before"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "engine-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "WAITING_RETRY")
        provider.fail_mode = None
        second = engine.run_tts(workflow_id, provider)
        self.assertEqual(second.status, "SUCCEEDED")
        with self.database.read_transaction() as con:
            rows = con.execute(
                "SELECT attempt_kind, execute_attempt_no, status FROM step_attempts ORDER BY attempt_seq"
            ).fetchall()
        self.assertEqual(
            [(row["attempt_kind"], row["execute_attempt_no"], row["status"]) for row in rows],
            [("EXECUTE", 1, "FAILED"), ("EXECUTE", 2, "SUCCEEDED")],
        )

    def test_browser_disconnect_before_confirmation_waits_for_user_instead_of_auto_retry(self) -> None:
        class BrowserClosedProvider:
            provider = "xunfei"
            account_scope = "test-account"

            def submit(self, submission_key, payload):
                raise ProviderError(
                    "浏览器在提交确认前已关闭",
                    code="TRANSIENT_PROVIDER_ERROR",
                    details={
                        "browser_disconnected": True,
                        "cancelled_before_confirmation": True,
                    },
                    ambiguous=False,
                )

            def query(self, submission_key):
                return None

            def download(self, receipt):
                return b""

        workflow_id = self._workflow_with_items()
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "browser-handoff-artifacts"))

        first = engine.run_tts(workflow_id, BrowserClosedProvider())
        self.assertEqual(first.status, "WAITING_RETRY")
        held = self.repository.get_workflow(workflow_id)
        self.assertEqual(held.execution_state, "WAITING_USER")
        self.assertEqual(
            PersistentScheduler(self.database).claim_due_retries(now="9999-12-31T00:00:00.000Z"),
            [],
        )
        with self.database.read_transaction() as con:
            step = con.execute(
                "SELECT status, retry_after FROM workflow_steps WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        self.assertEqual((step["status"], step["retry_after"]), ("WAITING_USER", None))

        changed = self.repository.patch_draft(
            workflow_id,
            held.state_version,
            configuration={"mode": "composite_cut", "default_female_voice": "speaker:changed"},
        )
        self.assertGreater(changed.state_version, held.state_version)
        second = engine.run_tts(workflow_id, FakeProvider())
        self.assertEqual(second.status, "SUCCEEDED")

    def test_safe_waiting_retry_allows_config_change_and_creates_a_new_plan(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "before"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "config-retry-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "WAITING_RETRY")
        before_patch = self.repository.get_workflow(workflow_id)
        patched = self.repository.patch_draft(
            workflow_id,
            before_patch.state_version,
            configuration={"mode": "composite_cut", "default_female_voice": "speaker:changed"},
        )
        self.assertEqual(patched.state_version, before_patch.state_version + 1)

        provider.fail_mode = None
        second = engine.run_tts(workflow_id, provider)
        self.assertEqual(second.status, "SUCCEEDED")
        self.assertNotEqual(first.submission_id, second.submission_id)
        with self.database.read_transaction() as con:
            self.assertEqual(
                con.execute(
                    """SELECT COUNT(*) FROM provider_submissions
                       WHERE workflow_group_id=(SELECT workflow_group_id FROM workflows WHERE workflow_id=?)""",
                    (workflow_id,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM retry_budgets WHERE workflow_group_id=(SELECT workflow_group_id FROM workflows WHERE workflow_id=?)",
                    (workflow_id,),
                ).fetchone()[0],
                1,
            )

    def test_user_hold_blocks_background_retry_and_item_retry_clears_error_projection(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "before"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "hold-retry-artifacts"))

        first = engine.run_tts(workflow_id, provider)
        self.assertEqual(first.status, "WAITING_RETRY")
        before_hold = self.repository.get_workflow(workflow_id)
        held = self.repository.hold_automatic_retry(
            workflow_id,
            before_hold.state_version,
            reason="desktop-return-to-configuration",
        )
        self.assertEqual(held.execution_state, "WAITING_USER")

        # A restarted/scheduled dispatcher must not reclaim a retry after the
        # user has taken ownership of the configuration page.
        self.assertEqual(
            PersistentScheduler(self.database).claim_due_retries(safe_only=True, now="9999-12-31T00:00:00.000Z"),
            [],
        )

        item = self.repository.list_items(workflow_id)[0]
        retried = self.repository.targeted_command(
            workflow_id,
            "retry",
            {
                "target_type": "ITEM",
                "step_id": first.step_id,
                "item_id": item["item_id"],
            },
            expected_state_version=held.state_version,
            expected_target_state_version=int(item["state_version"]),
            reason="desktop-retry-failed-items",
        )
        self.assertEqual(retried.execution_state, "WAITING_USER")
        self.assertEqual(self.repository.list_items(workflow_id)[0]["status"], "PENDING")

        provider.fail_mode = None
        second = engine.run_tts(workflow_id, provider, item_ids=[item["item_id"]])
        self.assertEqual(second.status, "SUCCEEDED")

    def test_unattended_retry_requires_an_accepted_generation_command(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "before"
        engine = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "consent-retry-artifacts"))

        # A takeover/recovery artifact that failed before the user ever sent a
        # Generate command must stay silent: the unattended dispatcher must not
        # re-open the browser for work nobody asked for.
        orphan = engine.run_tts(workflow_id, provider)
        self.assertEqual(orphan.status, "WAITING_RETRY")
        self.assertEqual(
            PersistentScheduler(self.database).claim_due_retries(safe_only=True, now="9999-12-31T00:00:00.000Z"),
            [],
        )

        # The user-accepted path keeps its durable consent marker and stays
        # claimable by the same unattended dispatcher.
        accepted = self.repository.command(
            workflow_id,
            "generate",
            self.repository.get_workflow(workflow_id).state_version,
            reason="test-consent",
        )
        self.assertEqual(accepted.execution_state, "RUNNING")
        provider.fail_mode = "before"
        rejected = engine.run_tts(workflow_id, provider)
        self.assertEqual(rejected.status, "WAITING_RETRY")
        claims = PersistentScheduler(self.database).claim_due_retries(safe_only=True, now="9999-12-31T00:00:00.000Z")
        self.assertEqual([claim.resource_id for claim in claims], [rejected.step_id])

        provider.fail_mode = None
        self.assertEqual(engine.run_tts(workflow_id, provider).status, "SUCCEEDED")

    def test_provider_error_message_and_code_are_persisted_without_becoming_ambiguous(self) -> None:
        class QuotaProvider:
            provider = "xunfei"
            account_scope = "test-account"

            def submit(self, submission_key, payload):
                raise ProviderError(
                    "讯飞配音额度不足，请检查账号额度后重试",
                    code="PROVIDER_QUOTA_EXCEEDED",
                    ambiguous=False,
                )

            def query(self, submission_key):
                return None

            def download(self, receipt):
                return b""

        workflow_id = self._workflow_with_items()
        result = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "engine-artifacts")).run_tts(
            workflow_id,
            QuotaProvider(),
        )
        self.assertEqual(result.status, "WAITING_RETRY")
        self.assertEqual(result.error_code, "PROVIDER_QUOTA_EXCEEDED")
        self.assertEqual(result.error_message, "讯飞配音额度不足，请检查账号额度后重试")
        with self.database.read_transaction() as con:
            workflow = con.execute("SELECT execution_state, last_error_code, last_error_message, state_version FROM workflows").fetchone()
            event = con.execute(
                "SELECT event_type, payload_json FROM workflow_events WHERE event_type='TTS_SUBMISSION_REJECTED' ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(
            (workflow["execution_state"], workflow["last_error_code"], workflow["last_error_message"]),
            ("WAITING_RETRY", "PROVIDER_QUOTA_EXCEEDED", "讯飞配音额度不足，请检查账号额度后重试"),
        )
        self.assertEqual(workflow["state_version"], 3)
        self.assertIn("额度不足", event["payload_json"])

    def test_scheduler_claims_persisted_retry_without_an_in_memory_timer(self) -> None:
        workflow_id = self._workflow_with_items()
        provider = FakeProvider()
        provider.fail_mode = "before"
        result = WorkflowEngine(self.repository, ArtifactStore(Path(self.temp.name) / "scheduler-artifacts")).run_tts(workflow_id, provider)
        self.assertEqual(result.status, "WAITING_RETRY")

        claims = PersistentScheduler(self.database).claim_due_retries(now="9999-12-31T00:00:00.000Z")

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].resource_id, result.step_id)
        with self.database.read_transaction() as con:
            status = con.execute(
                "SELECT status FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                (workflow_id, result.step_id),
            ).fetchone()[0]
        self.assertEqual(status, "READY")

    def test_recovery_marks_in_flight_submission_ambiguous_without_resubmitting(self) -> None:
        workflow_id = self._workflow_with_items()
        items = self.repository.list_items(workflow_id)
        plan_items = [{
            "ordinal": 0,
            "item_id": str(items[0]["item_id"]),
            "identity_key": str(items[0]["item_identity_key"]),
            "content": str(items[0]["normalized_content"]),
            "content_hash": str(items[0]["content_hash"]),
            "role": items[0]["role"],
            "voice_key": items[0]["voice_key"],
        }]
        input_hash = content_hash({"mode": "composite_cut", "items": plan_items})
        profile_hash = content_hash({"generation_mode": "composite_cut", "format": "bin", "provider": "fake"})
        _lease_id, fencing_token, _ = self.repository.acquire_lease(
            workflow_id, "provider", "fake:fake-account", "recovery-test-owner"
        )
        plan = self.repository.prepare_tts_plan(
            workflow_id,
            provider="fake",
            provider_account_scope="fake-account",
            unit_type="composite",
            tts_submission_key="recovery-test-submission",
            ordered_plan=plan_items,
            input_hash=input_hash,
            submission_profile_hash=profile_hash,
            capability_snapshot={},
            lease_fencing_token=fencing_token,
        )
        self.repository.begin_tts_submission(plan)
        before_recovery = self.repository.get_workflow(workflow_id)

        findings = RecoveryService(self.database).apply_safe_recovery()

        self.assertTrue(any(item.resource_id == plan["submission_id"] for item in findings))
        after_recovery = self.repository.get_workflow(workflow_id)
        self.assertEqual(after_recovery.state_version, before_recovery.state_version + 1)
        with self.database.read_transaction() as con:
            submission = con.execute(
                "SELECT side_effect_state FROM provider_submissions WHERE provider_submission_id=?",
                (plan["submission_id"],),
            ).fetchone()[0]
            unit = con.execute(
                "SELECT side_effect_state, status FROM work_units WHERE work_unit_id=?",
                (plan["work_unit_id"],),
            ).fetchone()
        self.assertEqual(submission, "AMBIGUOUS")
        self.assertEqual((unit["side_effect_state"], unit["status"]), ("AMBIGUOUS", "AMBIGUOUS"))

    def test_recovery_repairs_legacy_running_projection_without_resubmitting(self) -> None:
        workflow_id = self._workflow_with_items()
        item = self.repository.list_items(workflow_id)[0]
        plan_item = {
            "ordinal": 0,
            "item_id": str(item["item_id"]),
            "identity_key": str(item["item_identity_key"]),
            "content": str(item["normalized_content"]),
            "content_hash": str(item["content_hash"]),
            "role": item["role"],
            "voice_key": item["voice_key"],
        }
        _lease_id, fencing_token, _ = self.repository.acquire_lease(
            workflow_id, "provider", "fake:fake-account", "legacy-repair-owner"
        )
        plan = self.repository.prepare_tts_plan(
            workflow_id,
            provider="fake",
            provider_account_scope="fake-account",
            unit_type="composite",
            tts_submission_key="legacy-running-ambiguous",
            ordered_plan=[plan_item],
            input_hash=content_hash({"mode": "composite_cut", "items": [plan_item]}),
            submission_profile_hash=content_hash({"generation_mode": "composite_cut"}),
            capability_snapshot={},
            lease_fencing_token=fencing_token,
        )
        self.repository.begin_tts_submission(plan)

        # Reproduce the old desktop projection: the provider result is already
        # uncertain, but cleanup left the parent workflow looking RUNNING.
        with self.database.transaction() as con:
            con.execute(
                "UPDATE provider_submissions SET side_effect_state='AMBIGUOUS' WHERE provider_submission_id=?",
                (plan["submission_id"],),
            )
            con.execute(
                "UPDATE work_units SET side_effect_state='AMBIGUOUS', status='AMBIGUOUS' WHERE work_unit_id=?",
                (plan["work_unit_id"],),
            )
            con.execute(
                "UPDATE step_attempts SET status='AMBIGUOUS', result_status='MIXED', error_code='SUBMISSION_AMBIGUOUS' WHERE attempt_id=?",
                (plan["attempt_id"],),
            )
            con.execute(
                "UPDATE work_unit_attempts SET status='AMBIGUOUS', side_effect_state='AMBIGUOUS' WHERE attempt_id=?",
                (plan["attempt_id"],),
            )
            con.execute(
                "UPDATE workflow_steps SET status='AMBIGUOUS', error_code='SUBMISSION_AMBIGUOUS' WHERE step_id=?",
                (plan["step_id"],),
            )
            con.execute(
                """UPDATE workflows SET execution_state='RUNNING', cleanup_state='SUCCEEDED',
                   result_status='IN_PROGRESS', last_error_code='SUBMISSION_AMBIGUOUS' WHERE workflow_id=?""",
                (workflow_id,),
            )

        findings = RecoveryService(self.database).apply_safe_recovery()
        self.assertTrue(any(
            finding.kind == "workflow_projection" and finding.resource_id == workflow_id
            for finding in findings
        ))
        repaired = self.repository.get_workflow(workflow_id)
        self.assertEqual(repaired.execution_state, "WAITING_USER")
        with self.database.read_transaction() as con:
            item_status = con.execute(
                "SELECT status FROM work_items WHERE item_id=?", (item["item_id"],)
            ).fetchone()[0]
            event_count = con.execute(
                """SELECT COUNT(*) FROM workflow_events
                   WHERE workflow_id=? AND event_type='RECOVERY_PROJECTION_REPAIRED'""",
                (workflow_id,),
            ).fetchone()[0]
        self.assertEqual(item_status, "AMBIGUOUS")
        self.assertEqual(event_count, 1)
        # The repair is idempotent after the workflow becomes WAITING_USER.
        self.assertFalse(RecoveryService(self.database).apply_safe_recovery())

    def test_garbage_collector_only_removes_unreferenced_blob_paths(self) -> None:
        store = ArtifactStore(Path(self.temp.name) / "gc-artifacts")
        orphan = store.blob_root / "aa" / "orphan.bin"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"orphan")

        findings = ArtifactGarbageCollector(self.database, store).scan()
        removed = ArtifactGarbageCollector(self.database, store).collect()

        self.assertEqual([(item.kind, item.key) for item in findings], [("blob", "blobs/aa/orphan.bin")])
        self.assertEqual([(item.kind, item.action) for item in removed], [("blob", "REMOVED")])
        self.assertFalse(orphan.exists())

    def test_garbage_collector_reports_missing_ready_blobs_without_deleting_candidates(self) -> None:
        missing_key = "blobs/bb/missing.bin"
        with self.database.transaction() as con:
            con.execute(
                """INSERT INTO artifact_blobs(
                    blob_id, sha256, size_bytes, format, storage_key,
                    lifecycle_state, verified_at, created_at, deleted_at
                ) VALUES (?,?,?,?,?,?,?,?,NULL)""",
                ("blob-missing", "b" * 64, 3, "bin", missing_key, "READY", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
            )
        store = ArtifactStore(Path(self.temp.name) / "missing-blob-artifacts")
        collector = ArtifactGarbageCollector(self.database, store)

        findings = collector.scan()
        self.assertIn(("blob-missing", missing_key, "MISSING"), [(item.kind, item.key, item.action) for item in findings])
        self.assertEqual(collector.collect(), [])

    def test_garbage_collector_does_not_follow_blob_symlink_or_stale_orphan_finding(self) -> None:
        store = ArtifactStore(Path(self.temp.name) / "gc-symlink-artifacts")
        outside = Path(self.temp.name) / "outside.bin"
        outside.write_bytes(b"must survive")
        blob_key = "blobs/cc/outside.bin"
        blob_path = store.root / blob_key
        blob_path.parent.mkdir(parents=True)
        blob_path.symlink_to(outside)
        collector = ArtifactGarbageCollector(self.database, store)

        with patch.object(collector, "scan", return_value=[GarbageFinding("blob", blob_key, "ORPHAN")]):
            self.assertEqual(collector.collect(), [])
        self.assertTrue(outside.exists())
        self.assertTrue(blob_path.is_symlink())

    def test_garbage_collector_rechecks_references_before_removing_a_stale_finding(self) -> None:
        store = ArtifactStore(Path(self.temp.name) / "gc-race-artifacts")
        blob_key = "blobs/dd/race.bin"
        blob_path = store.root / blob_key
        blob_path.parent.mkdir(parents=True)
        blob_path.write_bytes(b"referenced after scan")
        collector = ArtifactGarbageCollector(self.database, store)

        with self.database.transaction() as con:
            con.execute(
                """INSERT INTO artifact_blobs(
                    blob_id, sha256, size_bytes, format, storage_key,
                    lifecycle_state, verified_at, created_at, deleted_at
                ) VALUES (?,?,?,?,?,?,?,?,NULL)""",
                ("blob-race", "d" * 64, len(b"referenced after scan"), "bin", blob_key, "READY", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
            )
        with patch.object(collector, "scan", return_value=[GarbageFinding("blob", blob_key, "ORPHAN")]):
            self.assertEqual(collector.collect(), [])
        self.assertTrue(blob_path.exists())

    def test_terminal_rerun_creates_a_new_run_and_reuses_source_blob_locally(self) -> None:
        snapshot = self.repository.create_workflow("tts", {"mode": "composite_cut"})
        artifacts = ArtifactStore(Path(self.temp.name) / "rerun-artifacts")
        imports = SourceImportService(self.database, artifacts)
        created = imports.create_import(snapshot.workflow_id, metadata={"filename": "lesson.docx"}, expected_size_bytes=5)
        grant = imports.acquire_writer(created["source_import_id"], 1, expected_state_version=created["state_version"])
        imports.write_generation(created["source_import_id"], 1, io.BytesIO(b"hello"), grant=grant.token)
        self.repository.create_item(
            snapshot.workflow_id, item_type="sentence", sequence=0, normalized_content="hello",
            item_identity_key="lesson:0", role="default", voice_key="fake",
        )
        terminal = WorkflowEngine(self.repository, artifacts).run_tts(snapshot.workflow_id, FakeProvider())
        source = self.repository.get_workflow(snapshot.workflow_id)
        rerun = self.repository.create_rerun(
            snapshot.workflow_id,
            expected_group_state_version=source.group_state_version,
            reason="manual verification",
        )

        self.assertEqual(terminal.status, "SUCCEEDED")
        self.assertNotEqual(rerun.workflow_id, source.workflow_id)
        self.assertEqual(rerun.parent_workflow_id, source.workflow_id)
        self.assertEqual(rerun.workflow_group_id, source.workflow_group_id)
        self.assertEqual(rerun.status, "DRAFT")
        self.assertEqual(rerun.item_count, 1)
        self.assertIsNotNone(rerun.source_artifact_id)
        self.assertNotEqual(rerun.source_artifact_id, source.source_artifact_id)
        with self.database.read_transaction() as con:
            source_blob = con.execute(
                "SELECT blob_id FROM artifacts WHERE artifact_id=?", (source.source_artifact_id,)
            ).fetchone()[0]
            rerun_blob = con.execute(
                "SELECT blob_id FROM artifacts WHERE artifact_id=?", (rerun.source_artifact_id,)
            ).fetchone()[0]
        self.assertEqual(rerun_blob, source_blob)


if __name__ == "__main__":
    unittest.main()
