from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.legacy_import import LegacyImporter
from workflow.repositories import WorkflowRepository
from workflow.security import OneTimeTicketManager
from workflow.source_imports import SourceImportService


class LegacyImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wordtts-legacy-test-")
        root = Path(self.temp.name)
        self.legacy_root = root / "legacy"
        self.source_root = root / "sources"
        self.session = self.legacy_root / "session-one"
        (self.session / "audio").mkdir(parents=True)
        self.source_root.mkdir()
        source = self.source_root / "lesson.docx"
        source.write_bytes(b"legacy-source")
        (self.session / "audio" / "one.mp3").write_bytes(b"audio-one")
        (self.session / "audio" / "two.mp3").write_bytes(b"audio-two")
        fingerprint = {"cache_version": 10, "parser_version": 14, "sha256": hashlib.sha256(b"legacy-source").hexdigest(), "size": 13}
        parsed = [{"doc_type": "lesson", "items": [
            {"id": "old-one", "category": "朗读", "text": "hello"},
            {"id": "old-two", "category": "朗读", "text": "world"},
        ]}]
        progress = {
            "source_file": "lesson.docx",
            "source_path": str(source),
            "status": "completed",
            "config": {"tts_config_version": 5, "voice": "Amanda", "access_token": "must-not-import"},
            "items": [
                {"id": "old-one", "doc_type": "lesson", "category": "朗读", "filename": "one.mp3", "status": "done", "raw_item": parsed[0]["items"][0]},
                {"id": "old-two", "doc_type": "lesson", "category": "朗读", "filename": "two.mp3", "status": "done", "raw_item": parsed[0]["items"][1]},
            ],
            "total_items": 2,
            "completed": 2,
            "failed": 0,
        }
        (self.session / "source_fingerprint.json").write_text(json.dumps(fingerprint), encoding="utf-8")
        (self.session / "parsed.json").write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
        (self.session / "progress.json").write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_dry_run_is_read_only_and_reports_hashes_and_versions(self) -> None:
        root = Path(self.temp.name)
        before = {path: path.stat().st_mtime_ns for path in self.session.rglob("*") if path.is_file()}
        importer = LegacyImporter(self.legacy_root, source_root=self.source_root)
        reports = importer.run(apply=False)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertTrue(report.dry_run)
        self.assertFalse(report.applied)
        self.assertEqual(report.item_count, 2)
        self.assertEqual(report.succeeded_count, 2)
        self.assertEqual(report.config_version, "5")
        self.assertEqual(report.source_sha256, hashlib.sha256(b"legacy-source").hexdigest())
        self.assertFalse((root / "workflow.db").exists())
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in self.session.rglob("*") if path.is_file()})

    def test_apply_is_idempotent_and_does_not_store_credentials_or_legacy_paths(self) -> None:
        root = Path(self.temp.name)
        database = WorkflowDatabase(root / "workflow.db", profile="full")
        database.initialize()
        repository = WorkflowRepository(database)
        artifacts = ArtifactStore(root / "artifacts")
        imports = SourceImportService(database, artifacts, ticket_manager=OneTimeTicketManager(max_ttl_seconds=3600))
        importer = LegacyImporter(
            self.legacy_root,
            source_root=self.source_root,
            repository=repository,
            artifact_store=artifacts,
            source_imports=imports,
        )
        first = importer.run(apply=True)[0]
        self.assertTrue(first.applied)
        self.assertIsNotNone(first.workflow_id)
        snapshot = repository.get_workflow(first.workflow_id)
        self.assertEqual(snapshot.result_status, "SUCCEEDED")
        with database.read_transaction() as con:
            item_count = con.execute("SELECT COUNT(*) FROM work_items WHERE workflow_id=?", (first.workflow_id,)).fetchone()[0]
            artifact_count = con.execute("SELECT COUNT(*) FROM artifacts WHERE workflow_id=?", (first.workflow_id,)).fetchone()[0]
            config = con.execute("SELECT configuration_snapshot FROM workflows WHERE workflow_id=?", (first.workflow_id,)).fetchone()[0]
        self.assertEqual(item_count, 2)
        self.assertEqual(artifact_count, 4)  # source + parsed + two imported audio Blobs
        self.assertNotIn("must-not-import", config)
        self.assertNotIn(str(self.source_root), config)

        second = importer.run(apply=True)[0]
        self.assertTrue(second.applied)
        with database.read_transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM workflows").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 4)
        database.close()


if __name__ == "__main__":
    unittest.main()
