from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from application.workflow_service import WorkflowApplicationError, WorkflowApplicationService
from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.engine import WorkflowEngine
from workflow.fake_provider import FakeProvider
from workflow.parser import LegacyWordParser
from workflow.providers import ProviderCapabilityError, ProviderRegistry
from workflow.repositories import WorkflowRepository
from workflow.source_imports import SourceImportService


class ApplicationServiceTests(unittest.TestCase):
    def test_provider_ready_snapshot_must_confirm_generation_capability(self) -> None:
        class InconsistentProvider:
            backend = object()
            allow_real = True

            @staticmethod
            def capability_snapshot():
                return {
                    "status": "READY",
                    "ready": False,
                    "can_generate": False,
                    "reason": "provider session is not healthy",
                }

        with self.assertRaises(ProviderCapabilityError):
            WorkflowApplicationService._ensure_provider_ready(InconsistentProvider())

    def test_unattended_generation_rejects_interactive_provider_login(self) -> None:
        class LoginRequiredProvider:
            backend = object()
            allow_real = True

            @staticmethod
            def capability_snapshot():
                return {
                    "status": "LOGIN_REQUIRED",
                    "ready": False,
                    "can_generate": False,
                    "can_start_generation": True,
                    "reason": "请先登录",
                }

        provider = LoginRequiredProvider()
        WorkflowApplicationService._ensure_provider_ready(provider)
        with self.assertRaises(ProviderCapabilityError):
            WorkflowApplicationService._ensure_provider_ready(
                provider,
                allow_interactive=False,
            )

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
            bridge_paths = []

            def observe_bridge(_database, *, source_path, **_kwargs):
                bridge_paths.append(Path(source_path))
                self.assertTrue(Path(source_path).is_file())
                return {"bridged": True}

            with patch("application.atomic_bridge.bridge_parse_to_atomic_model", side_effect=observe_bridge):
                parsed = service.parse(
                    draft.workflow_id,
                    expected_state_version=source_snapshot.state_version,
                    source_artifact_id=imported["source_artifact_id"],
                )
            self.assertEqual(parsed["workflow"].item_count, 1)
            self.assertEqual(parsed["source_filename"], "lesson.docx")
            self.assertEqual(len(parsed["parse_results"]), 1)
            self.assertEqual(len(service.artifacts(draft.workflow_id)), 2)
            self.assertEqual(len(bridge_paths), 1)
            self.assertFalse(bridge_paths[0].exists(), "桥接完成后临时文件应被清理")

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
            provider = FakeProvider(output_format="mp3")
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

            full_export = service.create_export_zip(draft.workflow_id)
            workspace = repository.get_workspace(draft.workflow_id)
            self.assertEqual(workspace["delivery"]["zip_artifact_id"], full_export["artifact_id"])
            self.assertEqual(workspace["delivery"]["included_item_ids"], item_ids)
            storage = repository.get_artifact_storage(
                full_export["artifact_id"], workflow_id=draft.workflow_id
            )
            with artifacts.read(storage["storage_key"]) as handle:
                with zipfile.ZipFile(io.BytesIO(handle.read())) as archive:
                    self.assertEqual(
                        archive.namelist(),
                        ["audio/", "audio/001.mp3", "audio/002.mp3"],
                    )
            database.close()

    def test_export_zip_uses_exam_audio_filename_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-exam-export-test-") as tmp:
            root = Path(tmp)
            database = WorkflowDatabase(root / "workflow.db")
            database.initialize()
            artifacts = ArtifactStore(root / "artifacts")
            repository = WorkflowRepository(database)
            imports = SourceImportService(database, artifacts)
            registry = ProviderRegistry()
            registry.register(FakeProvider(output_format="mp3"))
            service = WorkflowApplicationService(
                repository,
                imports,
                artifacts,
                engine=WorkflowEngine(repository, artifacts),
                providers=registry,
            )
            draft = service.create_draft("tts", {"generation_mode": "single_segment"})
            item_id = repository.create_item(
                draft.workflow_id,
                item_type="听后选择录音稿",
                sequence=0,
                normalized_content="W: hello",
                item_identity_key="exam:selection:1",
                metadata={
                    "audio_filename_stem": "听后选择-1",
                    "type_path": ["听后选择"],
                    "question_numbers": [1],
                },
            )

            _accepted, result = service.start_generation(
                draft.workflow_id,
                expected_state_version=draft.state_version,
                provider="fake",
                account_scope="fake-account",
            )
            self.assertEqual(result.status, "SUCCEEDED")
            export = service.create_export_zip(draft.workflow_id)
            storage = repository.get_artifact_storage(
                export["artifact_id"], workflow_id=draft.workflow_id
            )
            with artifacts.read(storage["storage_key"]) as handle:
                with zipfile.ZipFile(io.BytesIO(handle.read())) as archive:
                    self.assertEqual(
                        archive.namelist(),
                        ["audio/", "audio/听后选择-1.mp3"],
                    )
            segment = next(
                artifact for artifact in repository.get_workspace(draft.workflow_id)["artifacts"]
                if artifact["artifact_type"] == "tts-segment"
                and artifact["item_id"] == item_id
            )
            self.assertEqual(segment["filename"], "听后选择-1.mp3")
            database.close()

    def test_export_zip_does_not_reuse_corrupt_historical_zip(self) -> None:
        """A deterministic export id must not turn a bad old row into success."""
        with tempfile.TemporaryDirectory(prefix="wordtts-export-integrity-test-") as tmp:
            root = Path(tmp)
            database = WorkflowDatabase(root / "workflow.db")
            database.initialize()
            artifacts = ArtifactStore(root / "artifacts")
            repository = WorkflowRepository(database)
            imports = SourceImportService(database, artifacts)
            registry = ProviderRegistry()
            registry.register(FakeProvider(output_format="mp3"))
            service = WorkflowApplicationService(
                repository,
                imports,
                artifacts,
                engine=WorkflowEngine(repository, artifacts),
                providers=registry,
            )
            draft = service.create_draft("tts", {"generation_mode": "composite_cut"})
            item_id = repository.create_item(
                draft.workflow_id,
                item_type="sentence",
                sequence=0,
                normalized_content="line 0",
                item_identity_key="sentence:0",
            )
            _accepted, result = service.start_generation(
                draft.workflow_id,
                expected_state_version=draft.state_version,
                provider="fake",
                account_scope="fake-account",
            )
            self.assertEqual(result.status, "SUCCEEDED")
            service.create_export_zip(draft.workflow_id, include_item_ids=[item_id])

            # Healthy schema triggers reject this mutation.  Simulate a
            # legacy/corrupt row outside that guard and verify the service
            # refuses to reuse it rather than returning a false-success ZIP.
            with database.transaction() as con:
                con.execute("DROP TRIGGER artifacts_ready_guard_update")
                con.execute(
                    "UPDATE artifacts SET format='bin' WHERE workflow_id=? AND artifact_type='export-zip'",
                    (draft.workflow_id,),
                )

            with self.assertRaises(WorkflowApplicationError) as context:
                service.create_export_zip(draft.workflow_id, include_item_ids=[item_id])
            self.assertEqual(context.exception.code, "ARTIFACT_INVALID")
            database.close()

    def test_stale_full_zip_is_not_exposed_after_a_new_segment_is_published(self) -> None:
        """A partial-run ZIP must not mask a later retry's current scope."""
        with tempfile.TemporaryDirectory(prefix="wordtts-export-scope-test-") as tmp:
            root = Path(tmp)
            database = WorkflowDatabase(root / "workflow.db")
            database.initialize()
            artifacts = ArtifactStore(root / "artifacts")
            repository = WorkflowRepository(database)
            imports = SourceImportService(database, artifacts)
            registry = ProviderRegistry()
            registry.register(FakeProvider(output_format="mp3"))
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

            second_segment = next(
                segment for segment in repository.list_verified_tts_segments(draft.workflow_id)
                if str(segment["item_id"]) == item_ids[1]
            )
            invalid_staged = artifacts.stage_stream(io.BytesIO(b"not the current audio"))
            invalid_blob = artifacts.promote(invalid_staged, format="wav")
            repository.attach_imported_artifact(
                draft.workflow_id,
                artifact_id="artifact-export-scope-invalid",
                blob=invalid_blob,
                artifact_type="tts-segment",
                producer="scope-test",
                producer_version="1",
                item_id=item_ids[1],
            )

            stale_export = service.create_export_zip(draft.workflow_id)
            self.assertEqual(stale_export["included_item_ids"], [item_ids[0]])
            self.assertEqual(
                repository.get_workspace(draft.workflow_id)["delivery"]["zip_artifact_id"],
                stale_export["artifact_id"],
            )

            with artifacts.read(str(second_segment["storage_key"])) as source:
                restored_staged = artifacts.stage_stream(source)
            restored_blob = artifacts.promote(restored_staged, format="mp3")
            repository.attach_imported_artifact(
                draft.workflow_id,
                artifact_id="artifact-export-scope-restored",
                blob=restored_blob,
                artifact_type="tts-segment",
                producer="scope-test",
                producer_version="1",
                item_id=item_ids[1],
            )

            current_workspace = repository.get_workspace(draft.workflow_id)
            self.assertIsNone(current_workspace["delivery"]["zip_artifact_id"])
            self.assertTrue(next(
                action for action in current_workspace["available_actions"]
                if action["kind"] == "SERVICE" and action["type"] == "EXPORT_ZIP"
            )["enabled"])
            self.assertFalse(repository.list_history_records()[0]["zip_available"])

            current_export = service.create_export_zip(draft.workflow_id)
            self.assertNotEqual(current_export["artifact_id"], stale_export["artifact_id"])
            self.assertEqual(current_export["included_item_ids"], item_ids)
            self.assertEqual(
                repository.get_workspace(draft.workflow_id)["delivery"]["zip_artifact_id"],
                current_export["artifact_id"],
            )
            self.assertTrue(repository.list_history_records()[0]["zip_available"])
            database.close()


if __name__ == "__main__":
    unittest.main()
