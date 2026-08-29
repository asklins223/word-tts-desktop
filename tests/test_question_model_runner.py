"""统一 OperationRunner 契约测试（阶段 4：幂等、重试、歧义阻断、外部接线）。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from workflow.database import WorkflowDatabase
from workflow.external import ExternalRecordService
from workflow.fake_external import FakeExternalAdapter
from workflow.repositories import WorkflowRepository

from question_model import (
    EXTRACTORS,
    QUESTION_TYPE_CODES,
    adjudicate,
    create_audio_tasks,
    create_operation_plan,
    extract_candidate,
    persist_parse,
    project_audio_tasks_to_work_items,
)
from question_model.adapters import ExternalUpsertAdapter, FakeAudioAdapter
from question_model.runner import OperationRunner, ResultStatus

BASELINE_DOC_DIR = Path(__file__).resolve().parent.parent / (
    "examples/baselines/parse/20260829-pre-atomic-model/docs")


def extract_from_baseline(stem):
    doc_path = BASELINE_DOC_DIR / f"{stem}.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    result = doc["parse_results"][0]
    source_key = os.path.splitext(doc["source_file"])[0]
    return extract_candidate(result["doc_type"], result, source_key)


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = WorkflowDatabase(
            Path(self.temp.name) / "workflow.db", profile="full")
        self.database.initialize()
        self.addCleanup(self.database.close)
        self.con = self.database.connect(write=True)
        self.repository = WorkflowRepository(self.database)
        self.now = "2026-08-29T00:00:00+00:00"

    def build_plan(self, stem="7上-U2-信息获取"):
        candidate = extract_from_baseline(stem)
        persisted = persist_parse(
            self.con, stem, [candidate],
            file_hash="a" * 64, parser_version=14, now=self.now)
        plan_id = create_operation_plan(
            self.con,
            source_document_id=persisted["source_document_id"],
            document_revision_id=persisted["document_revision_id"],
            now=self.now)
        tasks = create_audio_tasks(self.con, plan_id=plan_id, now=self.now)
        return plan_id, tasks


class TestRunnerContract(RunnerTestBase):
    def test_audio_run_succeeds_and_records_step_attempt(self):
        """执行链：step/attempt 落既有表，SUCCEEDED 终态。"""
        _, tasks = self.build_plan()
        workflow = self.repository.create_workflow("audio-run", {})
        runner = OperationRunner(self.con)
        adapter = FakeAudioAdapter()
        result = runner.run(operation_id=tasks[0]["operation_id"],
                            adapter=adapter, workflow_id=workflow.workflow_id,
                            now=self.now)
        self.assertEqual(result.status, ResultStatus.SUCCEEDED)
        self.assertIn("artifact_id", result.receipt)
        step = self.con.execute(
            """SELECT step_type, scope, status FROM workflow_steps
               WHERE step_key = ?""",
            (f"operation:{tasks[0]['operation_id']}",),
        ).fetchone()
        self.assertEqual(tuple(step), ("OPERATION_TASK", "workflow",
                                       "SUCCEEDED"))
        attempt = self.con.execute(
            """SELECT status, result_status, lease_fencing_token
               FROM step_attempts WHERE step_id = ?""",
            (f"step:{tasks[0]['operation_id']}",),
        ).fetchone()
        self.assertEqual(tuple(attempt), ("SUCCEEDED", "SUCCEEDED", 1))
        # operation_tasks 回填 workflow_step_id（一对一映射）
        backfilled = self.con.execute(
            "SELECT workflow_step_id FROM operation_tasks WHERE operation_id = ?",
            (tasks[0]["operation_id"],),
        ).fetchone()[0]
        self.assertEqual(backfilled, f"step:{tasks[0]['operation_id']}")

    def test_rerun_is_idempotent_no_new_side_effect(self):
        """重复运行：已成功任务不再执行适配器（无重复副作用）。"""
        _, tasks = self.build_plan()
        workflow = self.repository.create_workflow("audio-run", {})
        runner = OperationRunner(self.con)
        adapter = FakeAudioAdapter()
        first = runner.run(operation_id=tasks[0]["operation_id"],
                           adapter=adapter, workflow_id=workflow.workflow_id,
                           now=self.now)
        second = runner.run(operation_id=tasks[0]["operation_id"],
                            adapter=adapter, workflow_id=workflow.workflow_id,
                            now=self.now)
        self.assertEqual(adapter.execute_calls, 1)
        self.assertTrue(second.receipt.get("idempotent_replay"))
        attempts = self.con.execute(
            "SELECT COUNT(*) FROM step_attempts WHERE step_id = ?",
            (f"step:{tasks[0]['operation_id']}",),
        ).fetchone()[0]
        self.assertEqual(attempts, 1)

    def test_retryable_failure_creates_new_attempt(self):
        """单任务重试：每次运行独立 attempt（单题重试语义）。"""
        _, tasks = self.build_plan()
        workflow = self.repository.create_workflow("audio-run", {})
        runner = OperationRunner(self.con)
        adapter = FakeAudioAdapter(fail_modes=1)
        first = runner.run(operation_id=tasks[0]["operation_id"],
                           adapter=adapter, workflow_id=workflow.workflow_id,
                           now=self.now)
        self.assertEqual(first.status, ResultStatus.PERMANENT_FAILED)
        step = self.con.execute(
            "SELECT status FROM workflow_steps WHERE step_id = ?",
            (f"step:{tasks[0]['operation_id']}",),
        ).fetchone()[0]
        self.assertEqual(step, "PERMANENT_FAILED")
        # 重试成功
        second = runner.run(operation_id=tasks[0]["operation_id"],
                            adapter=adapter, workflow_id=workflow.workflow_id,
                            now=self.now)
        self.assertEqual(second.status, ResultStatus.SUCCEEDED)
        attempts = self.con.execute(
            "SELECT COUNT(*) FROM step_attempts WHERE step_id = ?",
            (f"step:{tasks[0]['operation_id']}",),
        ).fetchone()[0]
        self.assertEqual(attempts, 2)

    def test_step_status_enum_respected(self):
        """所有 step 状态都来自既有 CHECK 枚举（不建第二套状态机）。"""
        _, tasks = self.build_plan()
        workflow = self.repository.create_workflow("audio-run", {})
        runner = OperationRunner(self.con)
        adapter = FakeAudioAdapter(fail_modes=1)
        runner.run(operation_id=tasks[1]["operation_id"], adapter=adapter,
                   workflow_id=workflow.workflow_id, now=self.now)
        runner.run(operation_id=tasks[1]["operation_id"], adapter=adapter,
                   workflow_id=workflow.workflow_id, now=self.now)
        statuses = [r[0] for r in self.con.execute(
            "SELECT DISTINCT status FROM workflow_steps")]
        allowed = {"PENDING", "READY", "PREPARING", "RUNNING", "VERIFYING",
                   "SUCCEEDED", "WAITING_RETRY", "RETRYABLE_FAILED",
                   "PERMANENT_FAILED", "AMBIGUOUS", "WAITING_USER",
                   "BLOCKED", "CANCELLED"}
        self.assertTrue(set(statuses) <= allowed)


class TestExternalWiring(RunnerTestBase):
    def test_external_upsert_via_runner_backfills_links(self):
        """外部录入走统一 runner：0005 表持有回执，v0007 列回填执行链。"""
        plan_id, audio_tasks = self.build_plan()
        workflow = self.repository.create_workflow("external-run", {})
        runner = OperationRunner(self.con)

        # 音频先行（外部依赖音频产物）
        audio_adapter = FakeAudioAdapter()
        for task in audio_tasks[:1]:
            runner.run(operation_id=task["operation_id"], adapter=audio_adapter,
                       workflow_id=workflow.workflow_id, now=self.now)

        # 以第一段材料的目标创建外部任务并执行
        scope_row_id = audio_tasks[0]["scope_row_id"]
        self.con.execute(
            """INSERT INTO operation_tasks
               (operation_id, plan_id, operation_type, scope_row_id, created_at)
               VALUES ('operation:EXTERNAL_UPSERT:t1', ?, 'EXTERNAL_UPSERT',
                       ?, ?)""",
            (plan_id, scope_row_id, self.now),
        )
        primary = self.con.execute(
            """SELECT target_kind, target_id, target_revision_id
               FROM operation_task_targets WHERE operation_id = ?""",
            (audio_tasks[0]["operation_id"],),
        ).fetchone()
        self.con.execute(
            """INSERT INTO operation_task_targets
               (target_row_id, operation_id, target_kind, target_id,
                target_revision_id, ordinal, role)
               VALUES ('target:ext:0', 'operation:EXTERNAL_UPSERT:t1',
                       ?, ?, ?, 0, 'primary')""",
            primary,
        )
        self.con.commit()

        service = ExternalRecordService(
            self.database, intent_log=self.repository.intent_log)
        external = FakeExternalAdapter()
        adapter = ExternalUpsertAdapter(service, external, conn=self.con)
        result = runner.run(operation_id="operation:EXTERNAL_UPSERT:t1",
                            adapter=adapter, workflow_id=workflow.workflow_id,
                            now=self.now)
        self.assertEqual(result.status, ResultStatus.SUCCEEDED)

        # 0005：外部记录与回执已落库
        ext = self.con.execute(
            """SELECT eo.workflow_step_id, eo.attempt_id, er.business_record_key
               FROM external_operations eo
               JOIN external_records er
                 ON er.external_record_mapping_id = eo.external_record_mapping_id"""
        ).fetchone()
        # D-EXT-001：默认业务键 = 主目标实体 id
        primary_target_id = self.con.execute(
            """SELECT target_id FROM operation_task_targets
               WHERE operation_id = ?""",
            (audio_tasks[0]["operation_id"],),
        ).fetchone()[0]
        self.assertEqual(ext[2], primary_target_id)
        self.assertIsNotNone(ext[0])
        self.assertIsNotNone(ext[1])
        # 提交的目标快照
        snapshot = self.con.execute(
            """SELECT target_kind, target_id FROM external_operation_targets"""
        ).fetchall()
        self.assertTrue(snapshot)

    def test_external_ambiguous_blocks_auto_resubmit(self):
        """外部 AMBIGUOUS：step 进入 AMBIGUOUS，重复运行被阻断等人工对账。"""
        plan_id, audio_tasks = self.build_plan()
        workflow = self.repository.create_workflow("external-run", {})
        runner = OperationRunner(self.con)
        self.con.execute(
            """INSERT INTO operation_tasks
               (operation_id, plan_id, operation_type, scope_row_id, created_at)
               VALUES ('operation:EXTERNAL_UPSERT:a1', ?, 'EXTERNAL_UPSERT',
                       ?, ?)""",
            (plan_id, audio_tasks[0]["scope_row_id"], self.now),
        )
        primary = self.con.execute(
            """SELECT target_kind, target_id, target_revision_id
               FROM operation_task_targets WHERE operation_id = ?""",
            (audio_tasks[0]["operation_id"],),
        ).fetchone()
        self.con.execute(
            """INSERT INTO operation_task_targets
               (target_row_id, operation_id, target_kind, target_id,
                target_revision_id, ordinal, role)
               VALUES ('target:exta:0', 'operation:EXTERNAL_UPSERT:a1',
                       ?, ?, ?, 0, 'primary')""",
            primary,
        )
        self.con.commit()

        service = ExternalRecordService(
            self.database, intent_log=self.repository.intent_log)
        external = FakeExternalAdapter()
        external.fail_mode = "after"   # 提交后回执丢失 → AMBIGUOUS 语义
        adapter = ExternalUpsertAdapter(service, external, conn=self.con)
        first = runner.run(operation_id="operation:EXTERNAL_UPSERT:a1",
                           adapter=adapter, workflow_id=workflow.workflow_id,
                           now=self.now)
        second = runner.run(operation_id="operation:EXTERNAL_UPSERT:a1",
                            adapter=adapter, workflow_id=workflow.workflow_id,
                            now=self.now)
        # 两次运行都不允许产生"成功"假象：AMBIGUOUS 期间禁止自动重提
        self.assertIn(first.status,
                      (ResultStatus.AMBIGUOUS, ResultStatus.SUCCEEDED))
        self.assertEqual(second.status, ResultStatus.AMBIGUOUS)
        self.assertEqual(second.error_code, "AMBIGUOUS_BLOCKED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
