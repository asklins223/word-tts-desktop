"""真实 TTS 引擎适配器测试（引擎函数注入，不依赖讯飞环境）。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from workflow.database import WorkflowDatabase
from workflow.repositories import WorkflowRepository

from question_model import (
    create_audio_tasks,
    create_operation_plan,
    extract_candidate,
    persist_parse,
)
from question_model.runner import OperationRunner, ResultStatus
from question_model.tts_adapter import TtsEngineAudioAdapter, resolve_voice_policy

BASELINE_DOC_DIR = Path(__file__).resolve().parent.parent / (
    "examples/baselines/parse/20260829-pre-atomic-model/docs")


class TtsAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.output_dir = str(root / "tts_output")
        self.database = WorkflowDatabase(root / "workflow.db", profile="full")
        self.database.initialize()
        self.addCleanup(self.database.close)
        self.con = self.database.connect(write=True)
        self.repository = WorkflowRepository(self.database)
        self.workflow = self.repository.create_workflow("tts-run", {})
        self.now = "2026-08-29T00:00:00+00:00"

        candidate = extract_candidate(
            "信息获取",
            json.loads((BASELINE_DOC_DIR / "7上-U2-信息获取.json").read_text(
                encoding="utf-8"))["parse_results"][0],
            "7上-U2-信息获取",
        )
        persisted = persist_parse(
            self.con, "7上-U2-信息获取", [candidate],
            file_hash="a" * 64, parser_version=14, now=self.now)
        plan_id = create_operation_plan(
            self.con,
            source_document_id=persisted["source_document_id"],
            document_revision_id=persisted["document_revision_id"],
            now=self.now)
        self.tasks = create_audio_tasks(self.con, plan_id=plan_id, now=self.now)

    def _run_first(self, engine):
        adapter = TtsEngineAudioAdapter(
            output_dir=self.output_dir, engine=engine)
        return OperationRunner(self.con).run(
            operation_id=self.tasks[0]["operation_id"], adapter=adapter,
            workflow_id=self.workflow.workflow_id,
            config={"text": "M: hello there", "female_voice": "Amanda",
                    "male_voice": "George"},
            now=self.now)

    def test_synth_writes_artifact_and_succeeds(self):
        """引擎产物落盘 → verify SUCCEEDED，回执含哈希。"""
        def fake_engine(text, rate, volume, pitch, **kwargs):
            self.assertEqual(text, "M: hello there")
            return b"ID3fake-audio-bytes"

        result = self._run_first(fake_engine)
        self.assertEqual(result.status, ResultStatus.SUCCEEDED)
        self.assertTrue(os.path.exists(result.receipt["artifact_path"]))
        self.assertEqual(result.receipt["bytes"], len(b"ID3fake-audio-bytes"))
        step = self.con.execute(
            "SELECT status FROM workflow_steps WHERE step_id = ?",
            (f"step:{self.tasks[0]['operation_id']}",),
        ).fetchone()[0]
        self.assertEqual(step, "SUCCEEDED")

    def test_missing_artifact_is_ambiguous_not_success(self):
        """引擎返回但产物校验失败：AMBIGUOUS（结果未知），不伪装成功。"""
        result = self._run_first(lambda *a, **k: b"")
        self.assertEqual(result.status, ResultStatus.AMBIGUOUS)
        self.assertEqual(result.error_code, "ARTIFACT_MISSING")
        step = self.con.execute(
            "SELECT status FROM workflow_steps WHERE step_id = ?",
            (f"step:{self.tasks[0]['operation_id']}",),
        ).fetchone()[0]
        self.assertEqual(step, "AMBIGUOUS")
        # AMBIGUOUS 后自动重跑被 runner 阻断
        blocked = self._run_first(lambda *a, **k: b"x")
        self.assertEqual(blocked.status, ResultStatus.AMBIGUOUS)
        self.assertEqual(blocked.error_code, "AMBIGUOUS_BLOCKED")

    def test_engine_exception_maps_to_failed_step(self):
        def broken_engine(*a, **k):
            raise RuntimeError("playwright 不可用")

        result = self._run_first(broken_engine)
        self.assertEqual(result.status, ResultStatus.PERMANENT_FAILED)
        step = self.con.execute(
            "SELECT status FROM workflow_steps WHERE step_id = ?",
            (f"step:{self.tasks[0]['operation_id']}",),
        ).fetchone()[0]
        self.assertEqual(step, "PERMANENT_FAILED")

    def test_voice_policy_forced_female_for_vocabulary(self):
        """注册表 voice_policy：词汇强制女声，其余走配置。"""
        config = {"female_voice": "Amanda", "male_voice": "George"}
        vocab = resolve_voice_policy("vocabulary", config)
        self.assertEqual(vocab["default_voice"], "Amanda")
        normal = resolve_voice_policy("listening_info", config)
        self.assertIsNone(normal["default_voice"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
