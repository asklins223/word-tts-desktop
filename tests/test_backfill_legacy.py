"""存量回填工具测试：别名幂等 + 未桥接会话登记 LEGACY_OUT_OF_BAND。"""
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
from question_model import sync_sub_type_registry

import sys
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from backfill_legacy import backfill_progress_file  # noqa: E402


class BackfillLegacyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.session_dir = os.path.join(tmp.name, "session-a")
        os.makedirs(self.session_dir)
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
        sync_sub_type_registry(self.con)
        self.progress_path = os.path.join(self.session_dir, "progress.json")
        with open(self.progress_path, "w", encoding="utf-8") as fh:
            json.dump({
                "source_file": "样例.docx",
                "items": [{
                    "id": "问题1", "doc_type": "信息获取",
                    "category": "听选信息题目", "seq": 1,
                    "raw_item": {"category": "听选信息题目", "number": 1},
                }],
                "xunfei_works_ids": {"问题1": "works-123"},
            }, fh, ensure_ascii=False)

    def test_backfill_registers_out_of_band_session_and_aliases(self):
        inserted = backfill_progress_file(self.con, self.progress_path)
        self.assertEqual(inserted, 1)
        session = self.con.execute(
            """SELECT source_classification, import_state
               FROM legacy_execution_sessions"""
        ).fetchone()
        self.assertEqual(tuple(session), ("LEGACY_OUT_OF_BAND", "PENDING"))
        kinds = dict(self.con.execute(
            "SELECT alias_kind, COUNT(*) FROM legacy_aliases GROUP BY alias_kind"
        ).fetchall())
        self.assertEqual(kinds, {"PROGRESS_ITEM": 1, "WORKS_ID": 1})

    def test_backfill_is_idempotent(self):
        backfill_progress_file(self.con, self.progress_path)
        backfill_progress_file(self.con, self.progress_path)
        count = self.con.execute(
            "SELECT COUNT(*) FROM legacy_aliases").fetchone()[0]
        self.assertEqual(count, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
