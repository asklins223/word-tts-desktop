"""操作层测试：OperationPlan / 不可变 Scope / AUDIO 任务与能力矩阵。"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from db.migration_runner import (
    apply_migrations,
    load_migrations,
    resolve_target,
)

from question_model import (
    create_audio_tasks,
    create_operation_plan,
    create_scope,
    extract_candidate,
    persist_parse,
    validate_scope_kind,
)

BASELINE_DOC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "baselines", "parse", "20260829-pre-atomic-model", "docs",
)


def load_baseline_doc(stem):
    with open(os.path.join(BASELINE_DOC_DIR, f"{stem}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def extract_from_baseline(stem):
    doc = load_baseline_doc(stem)
    result = doc["parse_results"][0]
    source_key = os.path.splitext(doc["source_file"])[0]
    return extract_candidate(result["doc_type"], result, source_key)


class OperationsTestBase(unittest.TestCase):
    def setUp(self):
        from workflow.database import WorkflowDatabase

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = WorkflowDatabase(Path(tmp.name) / "workflow.db",
                                         profile="full")
        self.database.initialize()
        self.addCleanup(self.database.close)
        self.con = self.database.connect(write=True)
        self.now = "2026-08-29T00:00:00+00:00"

    def persist_and_plan(self, stem):
        candidate = extract_from_baseline(stem)
        persisted = persist_parse(
            self.con, os.path.splitext(candidate.candidate_id.split(":")[-1])[0]
            if False else stem, [candidate],
            file_hash="a" * 64, parser_version=14, now=self.now)
        plan_id = create_operation_plan(
            self.con,
            source_document_id=persisted["source_document_id"],
            document_revision_id=persisted["document_revision_id"],
            now=self.now,
        )
        return plan_id


class TestAudioTasks(OperationsTestBase):
    def test_one_task_per_stimulus_with_question_members(self):
        """方案 9.5：STIMULUS scope 携带材料 + 关联小题成员快照。"""
        plan_id = self.persist_and_plan("7上-U2-信息获取")
        tasks = create_audio_tasks(self.con, plan_id=plan_id, now=self.now)
        self.assertEqual(len(tasks), 4)   # 3 段对话 + 1 段独白
        member_counts = sorted(t["member_count"] for t in tasks)
        self.assertEqual(member_counts, [3, 3, 3, 5])   # 材料+2题 / 材料+4题
        for task in tasks:
            self.assertEqual(task["operation_type"], "AUDIO_GENERATE")
            self.assertTrue(task["operation_id"].startswith("operation:AUDIO_GENERATE:"))

    def test_audio_tasks_idempotent(self):
        plan_id = self.persist_and_plan("7上-U2-信息获取")
        first = create_audio_tasks(self.con, plan_id=plan_id, now=self.now)
        second = create_audio_tasks(self.con, plan_id=plan_id, now=self.now)
        self.assertEqual(
            [t["operation_id"] for t in first],
            [t["operation_id"] for t in second],
        )
        self.assertTrue(all(t["reused_scope"] for t in second))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM operation_tasks").fetchone()[0],
            4,
        )

    def test_content_units_get_per_unit_tasks(self):
        plan_id = self.persist_and_plan("U6单词导入模板")
        tasks = create_audio_tasks(self.con, plan_id=plan_id, now=self.now)
        self.assertEqual(len(tasks), 80)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM operation_scopes").fetchone()[0],
            80,
        )

    def test_task_target_snapshot(self):
        plan_id = self.persist_and_plan("7上-U2-信息获取")
        create_audio_tasks(self.con, plan_id=plan_id, now=self.now)
        roles = dict(self.con.execute(
            "SELECT role, COUNT(*) FROM operation_task_targets GROUP BY role"
        ).fetchall())
        self.assertEqual(roles, {"primary": 4})


class TestScopeImmutability(OperationsTestBase):
    def test_member_change_creates_new_revision(self):
        """范围成员变化 → 新 scope_revision；旧 revision 不可变。"""
        plan_id = self.persist_and_plan("7上-U2-信息获取")
        create_audio_tasks(self.con, plan_id=plan_id, now=self.now)
        row = self.con.execute(
            """SELECT scope_id, scope_row_id, member_hash FROM operation_scopes
               ORDER BY scope_row_id LIMIT 1"""
        ).fetchone()
        scope_id, old_row_id, old_hash = row
        # 同 scope_id 但成员变化：加入一个额外 QUESTION 目标版本
        revision_id = self.con.execute(
            "SELECT entity_revision_id FROM document_revision_members "
            "WHERE entity_kind='STIMULUS' LIMIT 1"
        ).fetchone()[0]
        question_rev = self.con.execute(
            "SELECT entity_revision_id FROM document_revision_members "
            "WHERE entity_kind='QUESTION' LIMIT 1"
        ).fetchone()[0]
        members = [
            {"target_kind": "STIMULUS",
             "target_id": "stimulus:x",
             "target_revision_id": revision_id},
            {"target_kind": "QUESTION",
             "target_id": "question:x",
             "target_revision_id": question_rev},
        ]
        result = create_scope(
            self.con, plan_id=plan_id, scope_kind="STIMULUS",
            members=members, scope_id=scope_id, now=self.now)
        self.assertFalse(result["reused"])
        self.assertEqual(result["scope_revision"], 2)
        rows = self.con.execute(
            "SELECT scope_row_id, member_hash FROM operation_scopes "
            "WHERE scope_id = ? ORDER BY scope_revision",
            (scope_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], old_hash)   # 旧 revision 不变

    def test_unknown_member_revision_rejected(self):
        plan_id = self.persist_and_plan("7上-U2-信息获取")
        with self.assertRaises(ValueError):
            create_scope(
                self.con, plan_id=plan_id, scope_kind="STIMULUS",
                members=[{"target_kind": "STIMULUS",
                          "target_id": "stimulus:x",
                          "target_revision_id": "stimulus-revision:missing"}],
                now=self.now)

    def test_empty_scope_rejected(self):
        plan_id = self.persist_and_plan("7上-U2-信息获取")
        with self.assertRaises(ValueError):
            create_scope(self.con, plan_id=plan_id, scope_kind="STIMULUS",
                         members=[], now=self.now)


class TestCapabilityMatrix(OperationsTestBase):
    def test_scope_kind_allowed_by_role(self):
        """题目型小题型支持 QUESTION..DOCUMENT；内容型支持 CONTENT_UNIT。"""
        validate_scope_kind("listening_info", "QUESTION")
        validate_scope_kind("listening_info", "DOCUMENT")
        validate_scope_kind("vocabulary", "CONTENT_UNIT")
        validate_scope_kind("text_reading_sentence", "DOCUMENT")

    def test_scope_kind_violation_blocked(self):
        """内容型小题型不允许 QUESTION/STIMULUS 范围（提交前阻断）。"""
        with self.assertRaises(ValueError):
            validate_scope_kind("vocabulary", "STIMULUS")
        with self.assertRaises(ValueError):
            validate_scope_kind("text_reading_discourse", "QUESTION")

    def test_unregistered_sub_type_rejected(self):
        with self.assertRaises(ValueError):
            validate_scope_kind("不存在的题型", "QUESTION")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestWorkItemsProjection(OperationsTestBase):
    """方案 7.2：音频任务派生 work_items 兼容投影（单向、幂等、可追溯）。"""

    def _project(self, stem="7上-U2-信息获取"):
        from workflow.repositories import WorkflowRepository
        from question_model import project_audio_tasks_to_work_items

        plan_id = self.persist_and_plan(stem)
        create_audio_tasks(self.con, plan_id=plan_id, now=self.now)
        # work_items.workflow_id 外键到 workflows：经现有 repository 建运行
        repository = WorkflowRepository(self.database)
        workflow = repository.create_workflow("audio-projection", {})
        projected = project_audio_tasks_to_work_items(
            self.con, plan_id=plan_id,
            workflow_id=workflow.workflow_id, now=self.now)
        return plan_id, projected

    def test_projection_creates_work_items_and_aliases(self):
        plan_id, projected = self._project()
        self.assertEqual(len(projected), 4)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
            4,
        )
        # 每个投影行可追溯到 operation → scope → 成员
        row = self.con.execute(
            """SELECT item_id, metadata_json, voice_key FROM work_items LIMIT 1"""
        ).fetchone()
        metadata = json.loads(row[1])
        self.assertIn("operation_id", metadata)
        self.assertIn("members", metadata)
        self.assertTrue(metadata["members"])
        # legacy 别名：WORK_ITEM → 主目标实体版本
        aliases = self.con.execute(
            """SELECT alias_kind, target_kind FROM legacy_aliases"""
        ).fetchall()
        self.assertEqual(len(aliases), 4)
        self.assertTrue(all(a[0] == "WORK_ITEM" for a in aliases))

    def test_projection_is_idempotent(self):
        _, first = self._project()
        _, second = self._project()
        self.assertEqual([p["item_id"] for p in first],
                         [p["item_id"] for p in second])
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
            4,
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM legacy_aliases").fetchone()[0],
            4,
        )
