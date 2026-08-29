from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workflow.artifact_store import ArtifactStore
from workflow.database import WorkflowDatabase
from workflow.engine import WorkflowEngine
from workflow.providers import (
    ArtifactDownloader,
    BrowserRuntime,
    ProviderError,
    ProviderRegistry,
    SubmissionTracker,
    XunfeiTTSAdapter,
    _audio_to_mp3_bytes,
    _normalize_legacy_error,
)
from workflow.repositories import WorkflowRepository


class _Backend:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, key, payload):
        self.calls += 1
        return {
            "provider": "xunfei",
            "account_scope": "test-account",
            "provider_job_id": "formal-1",
            "canonical_key": "canonical-1",
            "output": b"provider-audio",
            "temporary_works_id": "temp-1",
            "formal_works_id": "formal-1",
            "summary": {"authorization": "should-not-be-used-by-repo"},
        }

    def query(self, key):
        return self.submit(key, {})


class _ExportableAudio:
    def __init__(self) -> None:
        self.export_kwargs = None

    def export(self, output, **kwargs):
        self.export_kwargs = kwargs
        output.write(b"encoded-audio")


class ProviderTests(unittest.TestCase):
    def test_real_provider_is_enabled_by_default(self) -> None:
        provider = XunfeiTTSAdapter(account_scope="test-account")
        self.assertTrue(provider.allow_real)

    def test_legacy_provider_errors_are_stable_and_safe(self) -> None:
        quota = _normalize_legacy_error(
            type("XunfeiQuotaExceeded", (RuntimeError,), {})("额度不足 token=secret-value"),
        )
        self.assertEqual(quota.code, "PROVIDER_QUOTA_EXCEEDED")
        self.assertFalse(quota.ambiguous)
        self.assertEqual(str(quota), "讯飞配音额度不足，请检查账号额度后重试")

        ambiguous = _normalize_legacy_error(
            type("XunfeiSubmissionAmbiguous", (RuntimeError,), {"works_name": "wordtts-demo"})("提交后无法定位作品"),
        )
        self.assertEqual(ambiguous.code, "SUBMISSION_AMBIGUOUS")
        self.assertTrue(ambiguous.ambiguous)
        self.assertEqual(ambiguous.details["works_name"], "wordtts-demo")

        # A plain XunfeiError is raised while preparing the editor, before
        # "确认合成".  It is safe to retry and must not become an ambiguous
        # external submission merely because the durable intent was recorded.
        pre_boundary = _normalize_legacy_error(
            type("XunfeiError", (RuntimeError,), {})("浏览器页面准备失败"),
        )
        self.assertEqual(pre_boundary.code, "TRANSIENT_PROVIDER_ERROR")
        self.assertFalse(pre_boundary.ambiguous)

        error = ProviderError(
            "provider failed authorization=secret-value",
            details={"cookie": "session-secret", "works_name": "safe-name"},
        )
        self.assertNotIn("secret-value", str(error))
        self.assertEqual(error.details["cookie"], "[REDACTED]")

    def test_legacy_work_name_is_deterministic_for_reconciliation(self) -> None:
        provider = XunfeiTTSAdapter(account_scope="test-account", allow_real=True)
        payload = {"plan": [{"content": "hello", "voice_key": "amanda"}]}
        first = provider._legacy_works("submission-key", payload)
        second = provider._legacy_works("submission-key", payload)
        self.assertEqual(first[0]["works_name"], second[0]["works_name"])
        self.assertEqual(first[0]["work_id"], "submission-key")

    def test_legacy_submission_preserves_materialized_voice_parameters(self) -> None:
        provider = XunfeiTTSAdapter(account_scope="test-account", allow_real=True)
        works = provider._legacy_works(
            "submission-key-with-params",
            {
                "plan": [{
                    "item_id": "sentence:0",
                    "content": "hello",
                    "voice_key": "speaker:linda",
                    "speed": 62,
                    "pitch": 48,
                    "volume": 55,
                }],
            },
        )
        segment = works[0]["items"][0]["segments"][0]
        self.assertEqual(
            (segment["voice_key"], segment["speed"], segment["pitch"], segment["volume"]),
            ("speaker:linda", 62, 48, 55),
        )

    def test_closed_page_transport_error_is_safe_before_confirmation(self) -> None:
        import xunfei_peiyin as legacy

        class ClosedPage:
            def is_closed(self):
                return True

        class ClosedSession:
            _page = ClosedPage()
            _browser_disconnected = False
            _confirm_click_succeeded = False
            _submission_state_uncertain = False

        provider = XunfeiTTSAdapter(account_scope="test-account", allow_real=True)
        payload = {"plan": [{"item_id": "item-1", "content": "hello", "voice_key": "amanda"}]}
        with mock.patch.object(legacy, "_session", ClosedSession()), \
                mock.patch("workflow.providers._run_sync", side_effect=RuntimeError("Target page, context or browser has been closed")):
            with self.assertRaises(ProviderError) as context:
                provider._submit_legacy_xunfei("submission-key", payload)
        self.assertEqual(context.exception.code, "TRANSIENT_PROVIDER_ERROR")
        self.assertFalse(context.exception.ambiguous)
        self.assertTrue(context.exception.details["browser_disconnected"])
        self.assertTrue(context.exception.details["cancelled_before_confirmation"])

    def test_legacy_audio_export_honors_persisted_quality(self) -> None:
        audio = _ExportableAudio()
        output = _audio_to_mp3_bytes(audio, quality="320 kbps（极高）")
        self.assertEqual(output, b"encoded-audio")
        self.assertEqual(audio.export_kwargs, {"format": "mp3", "bitrate": "320k"})

    def test_tracker_merges_temporary_and_formal_ids(self) -> None:
        tracker = SubmissionTracker()
        self.assertEqual(tracker.observe("submission", temporary_works_id="temp"), "temp")
        self.assertEqual(tracker.observe("submission", formal_works_id="formal", canonical_key="temp"), "temp")
        self.assertEqual(tracker.get("submission"), {
            "canonical_key": "temp", "temporary_works_id": "temp", "formal_works_id": "formal",
        })

    def test_browser_runtime_and_artifact_downloader_are_injectable(self) -> None:
        closed = []
        runtime = BrowserRuntime(lambda voice: {"voice": voice}, lambda: closed.append(True))
        self.assertEqual(runtime.ensure_session("amanda"), {"voice": "amanda"})
        self.assertIs(runtime.ensure_session("george"), runtime.ensure_session("amanda"))
        runtime.close()
        self.assertEqual(closed, [True])
        downloader = ArtifactDownloader(lambda receipt: b"bytes")
        self.assertEqual(downloader.download(object()), b"bytes")
        self.assertEqual(len(ArtifactDownloader.verify(b"bytes")), 64)

    def test_adapter_backend_can_run_engine_without_engine_specific_provider_code(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="wordtts-provider-test-")
        try:
            root = Path(temp.name)
            database = WorkflowDatabase(root / "workflow.db")
            database.initialize()
            repository = WorkflowRepository(database)
            workflow = repository.create_workflow("tts", {"mode": "composite_cut"})
            repository.create_item(
                workflow.workflow_id, item_type="sentence", sequence=0,
                normalized_content="hello", item_identity_key="sentence:0",
            )
            backend = _Backend()
            provider = XunfeiTTSAdapter(account_scope="test-account", backend=backend)
            registry = ProviderRegistry()
            registry.register(provider)
            self.assertIs(registry.get("xunfei", "test-account"), provider)
            result = WorkflowEngine(repository, ArtifactStore(root / "artifacts")).run_tts(workflow.workflow_id, provider)
            self.assertEqual(result.status, "SUCCEEDED")
            self.assertEqual(backend.calls, 1)
            self.assertEqual(provider.capability_snapshot()["capability_version"], "xunfei-adapter-1")
            database.close()
        finally:
            temp.cleanup()

    def test_backend_receipt_defaults_to_registered_account_scope(self) -> None:
        class MinimalBackend:
            def submit(self, _key, _payload):
                return {"provider_job_id": "job-1", "output": b"provider-audio"}

        provider = XunfeiTTSAdapter(account_scope="non-default-account", backend=MinimalBackend())
        receipt = provider.submit("submission-key", {"plan": []})
        self.assertEqual(receipt.provider, "xunfei")
        self.assertEqual(receipt.account_scope, "non-default-account")


if __name__ == "__main__":
    unittest.main()
