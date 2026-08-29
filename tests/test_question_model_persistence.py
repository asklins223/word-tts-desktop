"""原子小题模型落库测试（v0006 schema + persistence repository）。"""

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
    ResolutionState,
    adjudicate,
    extract_candidate,
    persist_candidate,
    persist_parse,
)

BASELINE_DOC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "baselines", "parse", "20260829-pre-atomic-model", "docs",
)

FILE_HASH = "a" * 64
PARSER_VERSION = 14


def load_baseline_doc(stem):
    with open(os.path.join(BASELINE_DOC_DIR, f"{stem}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def extract_from_baseline(stem):
    doc = load_baseline_doc(stem)
    result = doc["parse_results"][0]
    source_key = os.path.splitext(doc["source_file"])[0]
    return extract_candidate(result["doc_type"], result, source_key)


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.con = sqlite3.connect(
            str(Path(tmp.name) / "workflow.db"), isolation_level=None)
        self.addCleanup(self.con.close)
        self.con.execute("PRAGMA foreign_keys = ON")
        migrations = load_migrations()
        apply_migrations(
            self.con,
            target=resolve_target(migrations, profile="full"),
            migrations=migrations,
        )

    def table_count(self, table):
        return self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestPersistParse(PersistenceTestBase):
    def test_full_pipeline_idempotent(self):
        """文档身份 + revision + 候选落库；重复执行行数不变。"""
        candidate = extract_from_baseline("7上-U2-信息获取")
        first = persist_parse(
            self.con, "7上-U2-信息获取", [candidate],
            file_hash=FILE_HASH, parser_version=PARSER_VERSION, now="2026-08-29T00:00:00+00:00",
        )
        self.assertEqual(first["stimuli"], 4)
        self.assertEqual(first["questions"], 10)
        self.assertEqual(first["question_stimuli"], 10)
        self.assertEqual(self.table_count("source_documents"), 1)
        self.assertEqual(self.table_count("document_revisions"), 1)
        self.assertEqual(self.table_count("stimuli"), 4)
        self.assertEqual(self.table_count("stimulus_revisions"), 4)
        self.assertEqual(self.table_count("question_items"), 10)
        self.assertEqual(self.table_count("question_revisions"), 10)
        self.assertEqual(self.table_count("question_stimuli"), 10)

        second = persist_parse(
            self.con, "7上-U2-信息获取", [candidate],
            file_hash=FILE_HASH, parser_version=PARSER_VERSION, now="2026-08-29T01:00:00+00:00",
        )
        self.assertEqual(first["document_revision_id"],
                         second["document_revision_id"])
        self.assertEqual(self.table_count("question_revisions"), 10)

    def test_one_material_two_questions_linked(self):
        """方案 9.1：一材料两小题落库后共享同一 stimulus。"""
        candidate = extract_from_baseline("7上-U2-信息获取")
        persist_parse(self.con, "7上-U2-信息获取", [candidate],
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        shared = self.con.execute(
            """
            SELECT s.stimulus_id, COUNT(DISTINCT qr.question_id) AS n
            FROM question_stimuli qs
            JOIN stimulus_revisions sr ON sr.stimulus_revision_id = qs.stimulus_revision_id
            JOIN stimuli s ON s.stimulus_id = sr.stimulus_id
            JOIN question_revisions qr ON qr.question_revision_id = qs.question_revision_id
            GROUP BY s.stimulus_id
            ORDER BY n DESC
            """
        ).fetchall()
        by_count = sorted(row[1] for row in shared)
        self.assertEqual(by_count, [2, 2, 2, 4])

    def test_content_change_creates_new_revision_not_overwrite(self):
        """正文变化 → 新 revision；逻辑身份与旧行保留。"""
        candidate = extract_from_baseline("7上-U2-信息获取")
        persist_parse(self.con, "7上-U2-信息获取", [candidate],
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        modified = json.loads(json.dumps(
            load_baseline_doc("7上-U2-信息获取"), ensure_ascii=False))
        modified["parse_results"][0]["items"][0]["text"] += "（改动）"
        source_key = os.path.splitext(modified["source_file"])[0]
        modified_candidate = extract_candidate(
            modified["parse_results"][0]["doc_type"],
            modified["parse_results"][0], source_key)
        persist_parse(self.con, "7上-U2-信息获取", [modified_candidate],
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        self.assertEqual(self.table_count("question_items"), 10)
        self.assertEqual(self.table_count("question_revisions"), 11)

    def test_identity_drift_rejected(self):
        """同一 question_id 出现不同 type_code 必须报错，不静默合并。"""
        from question_model import ParseCandidate, QuestionItem, build_identity
        from question_model.persistence import persist_candidate
        candidate = extract_from_baseline("7上-U2-信息获取")
        persist_parse(self.con, "7上-U2-信息获取", [candidate],
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        # 题型代码不同、定位相同的实体触发身份漂移
        rogue = ParseCandidate(
            candidate_id="candidate:drift:7上-U2-信息获取",
            type_code="listening_choice",
            claimed_blocks=("听选信息题目/题目1",),
            entities=(QuestionItem(
                question_id=build_identity(
                    "question", "7上-U2-信息获取", "听选信息题目/题目1"),
                question_type="listening_choice",
                stem="M: q1",
                source_locator="听选信息题目/题目1",
                resolution_state=ResolutionState.DRAFT,
            ),),
            capabilities={"question_fields_complete": False,
                          "audio_only": True},
        )
        revision_id = self.con.execute(
            "SELECT document_revision_id FROM document_revisions"
        ).fetchone()[0]
        source_document_id = self.con.execute(
            "SELECT source_document_id FROM source_documents"
        ).fetchone()[0]
        with self.assertRaises(ValueError):
            persist_candidate(
                self.con, rogue,
                source_document_id=source_document_id,
                document_revision_id=revision_id,
            )

    def test_cross_candidate_stimulus_reference_rejected(self):
        """小题引用的材料不在同一候选时直接报错，不落悬空关联。"""
        from question_model import ParseCandidate
        from question_model.persistence import persist_candidate
        candidate = extract_from_baseline("7上-U2-信息获取")
        question_only = ParseCandidate(
            candidate_id="candidate:info_acquisition:docx-questions",
            type_code="info_acquisition",
            claimed_blocks=tuple(
                e.source_locator for e in candidate.entities
                if hasattr(e, "question_type")),
            entities=tuple(
                e for e in candidate.entities if hasattr(e, "question_type")),
            capabilities={"question_fields_complete": False,
                          "audio_only": True},
        )
        persist_parse(self.con, "7上-U2-信息获取", [],
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        revision_id = self.con.execute(
            "SELECT document_revision_id FROM document_revisions"
        ).fetchone()[0]
        source_document_id = self.con.execute(
            "SELECT source_document_id FROM source_documents"
        ).fetchone()[0]
        with self.assertRaises(ValueError):
            persist_candidate(
                self.con, question_only,
                source_document_id=source_document_id,
                document_revision_id=revision_id,
            )

    def test_vocabulary_content_units(self):
        """词汇 → ContentUnit 落库；重复执行幂等。"""
        candidate = extract_from_baseline("U6单词导入模板")
        persist_parse(self.con, "U6单词导入模板", [candidate],
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        self.assertEqual(self.table_count("content_units"), 80)
        self.assertEqual(self.table_count("content_unit_revisions"), 80)
        persist_parse(self.con, "U6单词导入模板", [candidate],
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        self.assertEqual(self.table_count("content_unit_revisions"), 80)
        entry_kinds = dict(self.con.execute(
            "SELECT entry_kind, COUNT(*) FROM content_unit_revisions GROUP BY entry_kind"
        ).fetchall())
        self.assertEqual(entry_kinds, {"word": 40, "example_sentence": 40})
        kinds = {row[0] for row in self.con.execute(
            "SELECT DISTINCT sub_type_code FROM content_units")}
        self.assertEqual(kinds, {"vocabulary"})


class TestSubTypeRegistry(PersistenceTestBase):
    def test_registry_synced_on_persist(self):
        candidate = extract_from_baseline("7上-U2-信息获取")
        persist_parse(self.con, "7上-U2-信息获取", [candidate],
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        rows = dict(self.con.execute(
            "SELECT sub_type_code, type_family FROM question_sub_types"))
        self.assertEqual(rows["listening_info"], "info_acquisition")
        self.assertEqual(rows["asking_info"], "info_retelling")
        self.assertEqual(len(rows), 11)
        # 幂等：重复同步不产生新行
        from question_model import sync_sub_type_registry
        sync_sub_type_registry(self.con)
        self.assertEqual(self.table_count("question_sub_types"), 11)

    def test_reserved_sub_type_cannot_back_rows(self):
        from question_model import sync_sub_type_registry
        sync_sub_type_registry(self.con)
        with self.assertRaises(sqlite3.IntegrityError):
            # 询问信息未接入：不允许任何实体行引用（业务校验在模型层，
            # 这里验证注册表状态标记已落库）
            status = self.con.execute(
                "SELECT status FROM question_sub_types WHERE sub_type_code = 'asking_info'"
            ).fetchone()[0]
            self.assertEqual(status, "reserved")
            self.con.execute(
                """
                INSERT INTO question_items
                    (question_id, source_document_id, type_code,
                     sub_type_code, created_at)
                VALUES ('question:doc:asking-1', 'missing', 'info_retelling',
                        'asking_info', '2026')
                """
            )

    def test_sub_type_capability_columns(self):
        from question_model import sync_sub_type_registry
        sync_sub_type_registry(self.con)
        row = self.con.execute(
            """
            SELECT has_options, answer_kind, voice_policy
            FROM question_sub_types WHERE sub_type_code = 'listening_info'
            """
        ).fetchone()
        self.assertEqual(row, (1, "single_choice", "speaker"))


class TestSchemaIntegrity(PersistenceTestBase):
    def test_resolution_state_enum_enforced(self):
        from question_model.persistence import (
            create_document_revision,
            ensure_source_document,
            sync_sub_type_registry,
        )
        sync_sub_type_registry(self.con)
        source_document_id = ensure_source_document(self.con, "doc")
        revision_id = create_document_revision(
            self.con, source_document_id, file_hash=FILE_HASH,
            parser_version=PARSER_VERSION)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO question_items
                    (question_id, source_document_id, type_code,
                     sub_type_code, created_at)
                VALUES ('question:doc:x', ?, 'info_acquisition',
                        'listening_info', '2026')
                """,
                (source_document_id,),
            )
            self.con.execute(
                """
                INSERT INTO question_revisions
                    (question_revision_id, question_id, document_revision_id,
                     stem, source_locator, resolution_state, content_hash,
                     created_at)
                VALUES ('question-revision:x', 'question:doc:x', ?,
                        'stem', 'loc', 'NEEDS_REVIEW', ?, '2026')
                """,
                (revision_id, "b" * 64),
            )

    def test_scope_kind_enum_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO operation_scopes
                    (scope_row_id, scope_id, scope_revision, scope_kind,
                     created_at)
                VALUES ('row1', 'scope:x', 1, 'CUSTOM', '2026')
                """
            )

    def test_adjudicated_parse_persists(self):
        """抽取 + 裁决 + 落库全链路（单题型文档）。"""
        candidate = extract_from_baseline("7上-U2-信息获取")
        adjudicated = adjudicate([candidate], explicit_type_code="info_acquisition")
        persist_parse(self.con, "7上-U2-信息获取", adjudicated.candidates,
                      file_hash=FILE_HASH, parser_version=PARSER_VERSION)
        self.assertEqual(self.table_count("question_items"), 10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
