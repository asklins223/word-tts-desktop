"""revision 匹配算法测试（方案 6.2.1 / 9.1：插题、删题、重编号、改文）。

场景全部基于解析基线：同一份文档的不同"内容版本"先后落库为两个
document_revision，再运行匹配算法验证差异类别与幂等性。
"""

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
    DECISION_MATCHED,
    extract_candidate,
    match_document_revisions,
    persist_parse,
)

BASELINE_DOC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "baselines", "parse", "20260829-pre-atomic-model", "docs",
)
SOURCE_KEY = "7上-U2-信息获取"
PARSER_VERSION = 14
HASH_A = "a" * 64
HASH_B = "b" * 64


def load_baseline_doc():
    with open(os.path.join(BASELINE_DOC_DIR, f"{SOURCE_KEY}.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def candidate_from(doc):
    result = doc["parse_results"][0]
    return extract_candidate(result["doc_type"], result, SOURCE_KEY)


class RevisionMatchTest(unittest.TestCase):
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

    def persist(self, doc, file_hash, now):
        return persist_parse(
            self.con, SOURCE_KEY, [candidate_from(doc)],
            file_hash=file_hash, parser_version=PARSER_VERSION, now=now)

    def match(self, first, second):
        return match_document_revisions(
            self.con,
            source_document_id=first["source_document_id"],
            from_document_revision_id=first["document_revision_id"],
            to_document_revision_id=second["document_revision_id"],
            now="2026-08-29T00:00:00+00:00",
        )

    def test_unchanged_document_all_matched(self):
        first = self.persist(load_baseline_doc(), HASH_A, "2026-08-29T00:00:00+00:00")
        second = self.persist(load_baseline_doc(), HASH_B, "2026-08-29T01:00:00+00:00")
        report = self.match(first, second)
        self.assertEqual(report.counts(), {"MATCHED": 10})

    def test_text_change_keeps_identity_as_changed(self):
        """改文：保留逻辑身份，产生 CHANGED 决策。"""
        doc = load_baseline_doc()
        first = self.persist(doc, HASH_A, "2026-08-29T00:00:00+00:00")
        doc["parse_results"][0]["items"][0]["text"] += "（改动）"
        second = self.persist(doc, HASH_B, "2026-08-29T01:00:00+00:00")
        report = self.match(first, second)
        self.assertEqual(report.counts(), {"MATCHED": 9, "CHANGED": 1})
        changed = [d for d in report.decisions if d["decision"] == "CHANGED"]
        self.assertEqual(len(changed), 1)
        self.assertNotEqual(changed[0]["from_question_revision_id"],
                            changed[0]["to_question_revision_id"])

    def test_removed_question_without_candidate(self):
        """删题：无候选时落 REMOVED。"""
        doc = load_baseline_doc()
        first = self.persist(doc, HASH_A, "2026-08-29T00:00:00+00:00")
        doc["parse_results"][0]["items"] = [
            it for it in doc["parse_results"][0]["items"]
            if not (it.get("category") == "听选信息题目" and it.get("number") == 6)
        ]
        second = self.persist(doc, HASH_B, "2026-08-29T01:00:00+00:00")
        report = self.match(first, second)
        self.assertEqual(report.counts(), {"MATCHED": 9, "REMOVED": 1})

    def test_renumbered_question_paired_by_fingerprint(self):
        """重编号：确定性键失配，但 (小题型, 内容指纹) 唯一一对一 → MATCHED。"""
        doc = load_baseline_doc()
        first = self.persist(doc, HASH_A, "2026-08-29T00:00:00+00:00")
        doc["parse_results"][0]["items"][0]["number"] = 11
        doc["parse_results"][0]["items"][0]["filename_stem"] = "问题11"
        second = self.persist(doc, HASH_B, "2026-08-29T01:00:00+00:00")
        report = self.match(first, second)
        self.assertEqual(report.counts(), {"MATCHED": 11})
        # 新旧 id 都有 MATCHED 决策且记录了映射
        paired = [d for d in report.decisions
                  if d["decision"] == "MATCHED" and d["candidates"]]
        self.assertEqual(len(paired), 2)
        self.assertEqual(paired[0]["candidates"][0]["match_basis"],
                         "sub_type_and_content_fingerprint")

    def test_duplicate_content_renumber_is_ambiguous(self):
        """两道内容相同的小题同时重编号：多候选必须 AMBIGUOUS，不强行配对。"""
        doc = load_baseline_doc()
        # 构造两道内容相同的题目（题目 12/13，原文没有 → 同文本同指纹）
        script_item = {"category": "听选信息录音稿", "index": 4,
                       "text": "M: duplicated script"}
        first_doc = json.loads(json.dumps(doc, ensure_ascii=False))
        first_doc["parse_results"][0]["items"] += [
            script_item,
            {"category": "听选信息题目", "number": 12, "filename_stem": "问题12",
             "voice": "male", "text": "M: duplicated question"},
            {"category": "听选信息题目", "number": 13, "filename_stem": "问题13",
             "voice": "female", "text": "M: duplicated question"},
        ]
        first = self.persist(first_doc, HASH_A, "2026-08-29T00:00:00+00:00")
        second_doc = json.loads(json.dumps(first_doc, ensure_ascii=False))
        second_doc["parse_results"][0]["items"] = [
            it for it in second_doc["parse_results"][0]["items"]
            if not (it.get("category") == "听选信息题目"
                    and it.get("number") in (12, 13))
        ]
        second_doc["parse_results"][0]["items"] += [
            {"category": "听选信息题目", "number": 14, "filename_stem": "问题14",
             "voice": "male", "text": "M: duplicated question"},
            {"category": "听选信息题目", "number": 15, "filename_stem": "问题15",
             "voice": "female", "text": "M: duplicated question"},
        ]
        second = self.persist(second_doc, HASH_B, "2026-08-29T01:00:00+00:00")
        report = self.match(first, second)
        counts = report.counts()
        self.assertEqual(counts.get("AMBIGUOUS"), 4)   # 2 NEW + 2 REMOVED 全部歧义
        self.assertEqual(len(report.ambiguous_question_ids()), 4)

    def test_inserted_question_is_new(self):
        """插题：无任何候选 → NEW。"""
        doc = load_baseline_doc()
        first = self.persist(doc, HASH_A, "2026-08-29T00:00:00+00:00")
        doc["parse_results"][0]["items"].append({
            "category": "听选信息题目", "number": 99, "filename_stem": "问题99",
            "voice": "male", "text": "M: brand new question",
        })
        second = self.persist(doc, HASH_B, "2026-08-29T01:00:00+00:00")
        report = self.match(first, second)
        self.assertEqual(report.counts(), {"MATCHED": 10, "NEW": 1})

    def test_match_is_idempotent(self):
        """同一输入 + 算法版本重复运行：决策行不增、结果一致。"""
        doc = load_baseline_doc()
        first = self.persist(doc, HASH_A, "2026-08-29T00:00:00+00:00")
        doc["parse_results"][0]["items"][0]["text"] += "（改动）"
        second = self.persist(doc, HASH_B, "2026-08-29T01:00:00+00:00")
        report1 = self.match(first, second)
        rows_before = self.con.execute(
            "SELECT COUNT(*) FROM revision_match_decisions").fetchone()[0]
        report2 = self.match(first, second)
        rows_after = self.con.execute(
            "SELECT COUNT(*) FROM revision_match_decisions").fetchone()[0]
        self.assertEqual(rows_before, rows_after)
        self.assertEqual(report1.counts(), report2.counts())

    def test_first_import_marks_all_new(self):
        """首版文档（无 from revision）：全部 NEW。"""
        first = self.persist(load_baseline_doc(), HASH_A, "2026-08-29T00:00:00+00:00")
        report = match_document_revisions(
            self.con,
            source_document_id=first["source_document_id"],
            to_document_revision_id=first["document_revision_id"],
            now="2026-08-29T00:00:00+00:00",
        )
        self.assertEqual(report.counts(), {"NEW": 10})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
