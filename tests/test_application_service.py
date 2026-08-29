from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from application.workflow_service import WorkflowApplicationService
from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.engine import WorkflowEngine
from workflow.fake_provider import FakeProvider
from workflow.parser import LegacyWordParser
from workflow.providers import ProviderRegistry
from workflow.repositories import WorkflowRepository
from workflow.source_imports import SourceImportService


class ApplicationServiceTests(unittest.TestCase):
    def test_source_parse_and_generation_use_one_application_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-application-test-") as tmp:
            root = Path(tmp)
            database = WorkflowDatabase(root / "workflow.db")
            database.initialize()
            artifacts = ArtifactStore(root / "artifacts")
            repository = WorkflowRepository(database)
            imports = SourceImportService(database, artifacts)
            registry = ProviderRegistry()
            provider = FakeProvider()
            registry.register(provider)

            def parse(source_path: str):
                self.assertEqual(Path(source_path).suffix, ".docx")
                return ([{
                    "doc_type": "text-reading",
                    "items": [{"category": "sentence", "text": "Hello application service."}],
                }], "ok")

            service = WorkflowApplicationService(
                repository,
                imports,
                artifacts,
                parser=LegacyWordParser(parse_callable=parse),
                engine=WorkflowEngine(repository, artifacts),
                providers=registry,
            )
            draft = service.create_draft("tts", {"generation_mode": "composite_cut"})
            imported = service.import_source(
                draft.workflow_id,
                b"managed source",
                filename="lesson.docx",
                request_key="application-source-request",
            )
            source_snapshot = repository.get_workflow(draft.workflow_id)
            parsed = service.parse(
                draft.workflow_id,
                expected_state_version=source_snapshot.state_version,
                source_artifact_id=imported["source_artifact_id"],
            )
            self.assertEqual(parsed["workflow"].item_count, 1)
            self.assertEqual(parsed["source_filename"], "lesson.docx")
            self.assertEqual(len(parsed["parse_results"]), 1)
            self.assertEqual(len(service.artifacts(draft.workflow_id)), 2)

            replay = service.parse(
                draft.workflow_id,
                expected_state_version=parsed["workflow"].state_version,
                source_artifact_id=imported["source_artifact_id"],
            )
            self.assertEqual(replay["workflow"].state_version, parsed["workflow"].state_version)

            _accepted, result = service.start_generation(
                draft.workflow_id,
                expected_state_version=parsed["workflow"].state_version,
                generation_mode="single_segment",
                provider="fake",
                account_scope="fake-account",
            )
            self.assertEqual(result.status, "SUCCEEDED")
            self.assertEqual(provider.submit_calls, 1)
            persisted_config = repository.get_configuration(draft.workflow_id)
            self.assertEqual(persisted_config["generation_mode"], "single_segment")
            self.assertEqual(persisted_config["provider"], "fake")
            self.assertEqual(persisted_config["account_scope"], "fake-account")
            self.assertGreaterEqual(len(service.artifacts(draft.workflow_id)), 4)
            database.close()

    def test_export_zip_returns_authoritative_include_and_exclude_details(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-export-test-") as tmp:
            root = Path(tmp)
            database = WorkflowDatabase(root / "workflow.db")
            database.initialize()
            artifacts = ArtifactStore(root / "artifacts")
            repository = WorkflowRepository(database)
            imports = SourceImportService(database, artifacts)
            registry = ProviderRegistry()
            provider = FakeProvider()
            registry.register(provider)
            service = WorkflowApplicationService(
                repository,
                imports,
                artifacts,
                engine=WorkflowEngine(repository, artifacts),
                providers=registry,
            )
            draft = service.create_draft("tts", {"generation_mode": "composite_cut"})
            item_ids = [
                repository.create_item(
                    draft.workflow_id,
                    item_type="sentence",
                    sequence=sequence,
                    normalized_content=f"line {sequence}",
                    item_identity_key=f"sentence:{sequence}",
                )
                for sequence in range(2)
            ]

            _accepted, result = service.start_generation(
                draft.workflow_id,
                expected_state_version=draft.state_version,
                provider="fake",
                account_scope="fake-account",
            )
            self.assertEqual(result.status, "SUCCEEDED")

            export = service.create_export_zip(
                draft.workflow_id,
                include_item_ids=[item_ids[0]],
            )
            self.assertEqual(export["included_item_ids"], [item_ids[0]])
            self.assertEqual(export["excluded_item_ids"], [item_ids[1]])
            self.assertEqual(export["exclusion_reasons"][item_ids[1]], "NOT_SELECTED")
            self.assertEqual(export["mime_type"], "application/zip")

            workspace = repository.get_workspace(draft.workflow_id)
            self.assertEqual(workspace["delivery"]["zip_artifact_id"], export["artifact_id"])
            self.assertEqual(workspace["delivery"]["included_item_ids"], [item_ids[0]])
            self.assertEqual(workspace["delivery"]["exclusion_reasons"][item_ids[1]], "NOT_SELECTED")
            database.close()


if __name__ == "__main__":
    unittest.main()
