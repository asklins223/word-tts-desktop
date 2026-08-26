from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import server


async def _anext(iterator):
    """兼容 Python 3.9；项目支持的最低版本没有内置 anext。"""
    return await iterator.__anext__()


class DesktopServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.original_token = server._API_TOKEN
        server._API_TOKEN = "test-token"
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        server._API_TOKEN = self.original_token

    def test_health_requires_per_launch_token(self):
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        response = self.client.get(
            "/api/health",
            headers={"X-WordTTS-Token": "test-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["app"], "wordtts")
        self.assertNotIn("test-token", response.text)

    def test_query_token_supports_eventsource_and_media_urls(self):
        response = self.client.get("/api/health?token=test-token")
        self.assertEqual(response.status_code, 200)


class DesktopSessionIsolationTests(unittest.TestCase):
    def test_same_filename_sessions_have_distinct_output_directories(self):
        first = server.session_output_dir("lesson.docx_20260729010101000001")
        second = server.session_output_dir("lesson.docx_20260729010101000002")
        self.assertNotEqual(first, second)

    def test_download_path_cannot_escape_session_directory(self):
        with tempfile.TemporaryDirectory() as session_dir:
            with self.assertRaises(server.HTTPException):
                server.confined_file_path(session_dir, "../outside.mp3")

    def test_progress_from_an_old_audio_algorithm_is_not_reused(self):
        fingerprint = {"sha256": "same", "size": 1}
        progress = {
            "source_fingerprint": fingerprint,
            "config": {},
            "items": [{"status": "pending", "raw_item": {}}],
        }

        self.assertFalse(server.progress_is_reusable(progress, fingerprint))

        progress["config"]["audio_algorithm_version"] = 1
        self.assertFalse(server.progress_is_reusable(progress, fingerprint))

        progress["config"]["audio_algorithm_version"] = (
            server.core.AUDIO_ALGORITHM_VERSION
        )
        progress["config"]["parser_version"] = server.core.PARSER_VERSION
        self.assertTrue(server.progress_is_reusable(progress, fingerprint))

    def test_voice_asset_cache_deduplicates_same_voice_key(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            original_cache_dir = server.VOICE_ASSET_CACHE_DIR
            original_catalog = server._voice_catalog_data
            server.VOICE_ASSET_CACHE_DIR = cache_dir
            server._voice_catalog_data = {
                "voices": [{
                    "key": "speaker:demo",
                    "name": "Demo",
                    "img_url": "https://example.test/demo.jpg",
                    "audio_url": "https://example.test/demo.mp3",
                }],
            }

            def fake_download(_url, target_path, _kind):
                Path(target_path).write_bytes(b"asset")
                return "image/jpeg" if target_path.endswith("avatar.bin") else "audio/mpeg"

            try:
                with mock.patch.object(server, "_download_voice_asset", side_effect=fake_download) as download:
                    first = server._cache_voice_assets_sync(["speaker:demo", "speaker:demo"])
                    second = server._cache_voice_assets_sync(["speaker:demo"])

                self.assertEqual(download.call_count, 2)
                self.assertIn("speaker:demo", first)
                self.assertEqual(first, second)
                voice_dirs = [path for path in Path(cache_dir).iterdir() if path.is_dir()]
                self.assertEqual(len(voice_dirs), 1)
                self.assertTrue((voice_dirs[0] / "avatar.bin").is_file())
                self.assertTrue((voice_dirs[0] / "sample.bin").is_file())
            finally:
                server.VOICE_ASSET_CACHE_DIR = original_cache_dir
                server._voice_catalog_data = original_catalog

    def test_early_generation_error_still_emits_end(self):
        session = server.SessionState("fault-injection")

        async def exercise():
            with mock.patch.object(
                server,
                "source_fingerprint",
                side_effect=OSError("source unavailable"),
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    {},
                )

            events = []
            events.extend(session.event_journal)
            return events

        events = asyncio.run(exercise())
        event_types = [event["type"] for event in events]
        self.assertIn("error", event_types)
        self.assertEqual(event_types[-1], "end")
        summary = next(
            event["entry"]
            for event in events
            if event["type"] == "log" and event["entry"].get("key") == "task:summary"
        )
        self.assertEqual(summary["stage"], "complete")
        self.assertEqual(summary["kind"], "summary")
        self.assertEqual(summary["status"], "error")
        self.assertGreater(summary["seq"], 0)
        self.assertGreaterEqual(summary["duration_ms"], 0)
        self.assertEqual(session.status, "生成任务未能完成")

    def test_log_history_is_bounded_and_keeps_latest_sequence(self):
        session = server.SessionState("bounded-log")
        event_count = max(server.MAX_LOG_ENTRIES, server.MAX_EVENT_JOURNAL_ENTRIES) + 25
        for index in range(event_count):
            server.push_event(session, {
                "type": "log",
                "entry": {
                    "seq": index + 1,
                    "level": "info",
                    "title": f"记录 {index + 1}",
                },
            })

        self.assertEqual(len(session.log_entries), server.MAX_LOG_ENTRIES)
        self.assertEqual(session.log_entries[0]["seq"], event_count - server.MAX_LOG_ENTRIES + 1)
        self.assertEqual(session.log_entries[-1]["seq"], event_count)
        self.assertEqual(len(session.event_journal), server.MAX_EVENT_JOURNAL_ENTRIES)
        self.assertEqual(session.event_journal[0]["event_seq"], event_count - server.MAX_EVENT_JOURNAL_ENTRIES + 1)
        self.assertEqual(session.event_journal[-1]["event_seq"], event_count)

    def test_cancelled_sse_replay_preserves_terminal_state_and_summary(self):
        original_token = server._API_TOKEN
        server._API_TOKEN = "test-token"
        session = server.SessionState("cancelled-log-replay")
        server._sessions[session.session_id] = session
        server.push_event(session, {
            "type": "log",
            "entry": {
                "seq": 1,
                "level": "warn",
                "stage": "complete",
                "kind": "summary",
                "status": "warning",
                "key": "task:summary",
                "title": "任务已取消",
            },
        })
        server.push_event(session, {
            "type": "cancelled",
            "completed": 2,
            "failed": 0,
            "total": 5,
        })
        server.push_event(session, {"type": "end"})

        try:
            with TestClient(server.app) as client:
                response = client.get(
                    f"/api/progress/{session.session_id}",
                    headers={"X-WordTTS-Token": "test-token"},
                )
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            self.assertEqual(events[0]["type"], "log_init")
            cancelled_index = next(
                index for index, event in enumerate(events)
                if event["type"] == "cancelled"
            )
            end_index = next(
                index for index, event in enumerate(events)
                if event["type"] == "end"
            )
            self.assertLess(cancelled_index, end_index)
            self.assertEqual(events[cancelled_index]["completed"], 2)
        finally:
            server._sessions.pop(session.session_id, None)
            server._API_TOKEN = original_token

    def test_sse_replays_error_or_cancelled_before_finally_emits_end(self):
        async def collect(session):
            response = await server.progress_sse(session.session_id)
            events = []
            async for chunk in response.body_iterator:
                events.append(json.loads(chunk.removeprefix("data: ").strip()))
            return events

        terminal_events = [
            {"type": "error", "msg": "engine stopped"},
            {"type": "cancelled", "completed": 1, "failed": 0, "total": 2},
        ]
        for terminal in terminal_events:
            with self.subTest(terminal=terminal["type"]):
                session = server.SessionState(f"terminal-before-end-{terminal['type']}")
                server._sessions[session.session_id] = session
                server.push_event(session, terminal)
                try:
                    events = asyncio.run(asyncio.wait_for(collect(session), timeout=0.5))
                    event_types = [event["type"] for event in events]
                    self.assertIn(terminal["type"], event_types)
                    self.assertEqual(event_types[-1], "end")
                finally:
                    server._sessions.pop(session.session_id, None)

    def test_sse_snapshot_keeps_progress_monotonic_and_replays_final_status(self):
        session = server.SessionState("snapshot-watermark")
        server._sessions[session.session_id] = session
        server.push_event(session, {"type": "status", "text": "生成中 — 1/2"})
        server.push_event(session, {"type": "stats", "completed": 1, "failed": 0, "total": 2})
        server.push_event(session, {"type": "status", "text": "生成中 — 2/2"})
        server.push_event(session, {"type": "stats", "completed": 2, "failed": 0, "total": 2})

        async def exercise():
            response = await server.progress_sse(session.session_id)
            iterator = response.body_iterator
            chunks = [await _anext(iterator)]  # 冻结 2/2 快照后再制造终态竞态
            server.push_event(session, {"type": "status", "text": "完成 — 成功 2/2"})
            server.push_event(session, {"type": "stats", "completed": 2, "failed": 0, "total": 2})
            server.push_event(session, {
                "type": "log",
                "entry": {
                    "seq": 1,
                    "level": "success",
                    "stage": "complete",
                    "kind": "summary",
                    "status": "success",
                    "key": "task:summary",
                    "title": "全部处理完成",
                },
            })
            server.push_event(session, {
                "type": "done",
                "completed": 2,
                "failed": 0,
                "total": 2,
            })
            server.push_event(session, {"type": "end"})
            async for chunk in iterator:
                chunks.append(chunk)
            return [
                json.loads(chunk.removeprefix("data: ").strip())
                for chunk in chunks
            ]

        try:
            events = asyncio.run(exercise())
            self.assertEqual(
                [event["text"] for event in events if event["type"] == "status"],
                ["生成中 — 2/2", "完成 — 成功 2/2"],
            )
            self.assertEqual(
                [event["completed"] for event in events if event["type"] == "stats"],
                [2, 2],
            )
            final_status_index = next(
                index for index, event in enumerate(events)
                if event.get("text") == "完成 — 成功 2/2"
            )
            done_index = next(index for index, event in enumerate(events) if event["type"] == "done")
            self.assertLess(final_status_index, done_index)
        finally:
            server._sessions.pop(session.session_id, None)

    def test_parallel_sse_connections_each_receive_live_events(self):
        session = server.SessionState("broadcast-journal")
        server._sessions[session.session_id] = session

        async def exercise():
            first_response = await server.progress_sse(session.session_id)
            second_response = await server.progress_sse(session.session_id)
            first_iterator = first_response.body_iterator
            second_iterator = second_response.body_iterator
            await _anext(first_iterator)
            await _anext(second_iterator)

            server.push_event(session, {"type": "status", "text": "生成中 — 已处理 1/2"})
            first_status = json.loads((await _anext(first_iterator)).removeprefix("data: ").strip())
            second_status = json.loads((await _anext(second_iterator)).removeprefix("data: ").strip())
            server.push_event(session, {"type": "stats", "completed": 1, "failed": 0, "total": 2})
            first_stats = json.loads((await _anext(first_iterator)).removeprefix("data: ").strip())
            second_stats = json.loads((await _anext(second_iterator)).removeprefix("data: ").strip())
            await first_iterator.aclose()
            await second_iterator.aclose()
            return first_status, second_status, first_stats, second_stats

        try:
            first_status, second_status, first_stats, second_stats = asyncio.run(exercise())
            self.assertEqual(first_status, second_status)
            self.assertEqual(first_stats, second_stats)
            self.assertEqual(first_status["type"], "status")
            self.assertEqual(first_stats["type"], "stats")
        finally:
            server._sessions.pop(session.session_id, None)


class DesktopGenerationTimelineTests(unittest.TestCase):
    def setUp(self):
        self.output_dir = tempfile.TemporaryDirectory()
        self.output_patch = mock.patch.object(server.core, "OUTPUT_BASE", self.output_dir.name)
        self.output_patch.start()
        self.xunfei_patch = None
        if getattr(server.core, "_xunfei", None) is not None:
            async def fake_ensure_session(*_args, **_kwargs):
                return None

            self.xunfei_patch = mock.patch.object(
                server.core._xunfei,
                "ensure_session",
                new=fake_ensure_session,
            )
            self.xunfei_patch.start()

    def tearDown(self):
        if self.xunfei_patch is not None:
            self.xunfei_patch.stop()
        self.output_patch.stop()
        self.output_dir.cleanup()

    @staticmethod
    def _config():
        return {
            "format": "mp3",
            "quality": "128 kbps（标准）",
            "rate": 50,
            "volume": 50,
            "pitch": 50,
            "preview": False,
        }

    @staticmethod
    def _parse_results(text: str):
        return [{
            "doc_type": "朗读",
            "items": [{"category": "测试录音稿", "text": text}],
        }]

    def test_parse_failures_emit_one_structured_terminal_summary(self):
        async def run_case(session_id, *, side_effect=None, return_value=None):
            session = server.SessionState(session_id)
            with mock.patch.object(server, "source_fingerprint", return_value={"size": 1}), mock.patch.object(
                server.core,
                "parse_document_auto",
                side_effect=side_effect,
                return_value=return_value,
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    self._config(),
                )
            return session

        cases = [
            ("parse-raises", OSError("parser unavailable"), None),
            ("parse-empty", None, ([], "未发现题型")),
        ]
        for session_id, side_effect, return_value in cases:
            with self.subTest(session_id=session_id):
                session = asyncio.run(run_case(
                    session_id,
                    side_effect=side_effect,
                    return_value=return_value,
                ))
                summaries = [
                    event["entry"]
                    for event in session.event_journal
                    if event["type"] == "log" and event["entry"].get("key") == "task:summary"
                ]
                self.assertEqual(len(summaries), 1)
                self.assertEqual(summaries[0]["status"], "error")
                self.assertGreaterEqual(summaries[0]["duration_ms"], 0)
                event_types = [event["type"] for event in session.event_journal]
                self.assertLess(event_types.index("error"), event_types.index("end"))

    def test_all_failed_task_has_consistent_delivery_contract(self):
        session = server.SessionState("all-failed-contract")
        session.parse_results = self._parse_results("")

        async def exercise():
            with mock.patch.object(server, "source_fingerprint", return_value={"size": 1}):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    self._config(),
                )

        asyncio.run(exercise())
        download = next(event for event in session.event_journal if event["type"] == "download")
        done = next(event for event in session.event_journal if event["type"] == "done")
        summary = next(
            event["entry"]
            for event in session.event_journal
            if event["type"] == "log" and event["entry"].get("key") == "task:summary"
        )
        self.assertFalse(download["zip_available"])
        self.assertFalse(done["zip_available"])
        self.assertEqual(download["file_list"], [])
        self.assertEqual((done["completed"], done["failed"], done["total"]), (0, 1, 1))
        self.assertEqual(done["file_count"], 0)
        self.assertEqual(summary["status"], "error")
        self.assertFalse(Path(session.session_dir, "output.zip").exists())

    def test_successful_task_emits_detailed_item_update_and_self_contained_done(self):
        session = server.SessionState("successful-timeline")
        session.parse_results = self._parse_results("需要生成的内容")

        async def fake_batch(item_specs, **_kwargs):
            return {
                str(spec["item_id"]): {"audio": object(), "error": None}
                for spec in item_specs
            }

        def fake_export(_audio, _fmt, _quality, output_path):
            Path(output_path).write_bytes(b"audio")

        async def exercise():
            with mock.patch.object(server, "source_fingerprint", return_value={"size": 1}), mock.patch.object(
                server.core, "_synth_items_batch", new=fake_batch
            ), mock.patch.object(
                server.core, "export_audio", side_effect=fake_export
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    self._config(),
                )

        asyncio.run(exercise())
        item_logs = [
            event["entry"]
            for event in session.event_journal
            if event["type"] == "log" and event["entry"].get("kind") == "item"
        ]
        self.assertEqual([entry["status"] for entry in item_logs], ["running", "success"])
        self.assertEqual(item_logs[0]["key"], item_logs[1]["key"])
        self.assertIn("filename", item_logs[-1]["item"])
        self.assertIn("duration_ms", item_logs[-1])
        done = session.final_done
        self.assertIsNotNone(done)
        self.assertTrue(done["zip_available"])
        self.assertEqual((done["completed"], done["failed"], done["total"]), (1, 0, 1))
        self.assertEqual(done["file_count"], 1)
        self.assertIsNotNone(done["history_id"])

    def test_cancelling_during_last_item_does_not_emit_done(self):
        session = server.SessionState("cancel-last-item")
        session.parse_results = self._parse_results("需要生成的内容")

        async def fake_batch(item_specs, **_kwargs):
            session.cancelled = True
            return {
                str(spec["item_id"]): {"audio": object(), "error": None}
                for spec in item_specs
            }

        def fake_export(_audio, _fmt, _quality, output_path):
            Path(output_path).write_bytes(b"audio")

        async def exercise():
            with mock.patch.object(server, "source_fingerprint", return_value={"size": 1}), mock.patch.object(
                server.core, "_synth_items_batch", new=fake_batch
            ), mock.patch.object(
                server.core, "export_audio", side_effect=fake_export
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    self._config(),
                )

        asyncio.run(exercise())
        event_types = [event["type"] for event in session.event_journal]
        self.assertIn("cancelled", event_types)
        self.assertNotIn("done", event_types)
        self.assertEqual(event_types[-1], "end")
        self.assertEqual(session.final_cancelled["completed"], 1)
        self.assertIsNone(session.final_done)

    def test_forced_task_cancel_emits_cancelled_before_end(self):
        session = server.SessionState("forced-cancel")
        session.parse_results = self._parse_results("需要生成的内容")

        async def exercise():
            synth_started = asyncio.Event()

            async def blocked_batch(*_args, **_kwargs):
                synth_started.set()
                await asyncio.Event().wait()

            with mock.patch.object(server, "source_fingerprint", return_value={"size": 1}), mock.patch.object(
                server.core, "_synth_items_batch", new=blocked_batch
            ):
                task = asyncio.create_task(server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    self._config(),
                ))
                await asyncio.wait_for(synth_started.wait(), timeout=1)
                task.cancel()
                await asyncio.wait_for(task, timeout=1)

        asyncio.run(exercise())
        event_types = [event["type"] for event in session.event_journal]
        self.assertIn("cancelled", event_types)
        self.assertNotIn("done", event_types)
        self.assertLess(event_types.index("cancelled"), event_types.index("end"))
        self.assertIsNotNone(session.final_cancelled)


class DesktopHistoryTests(unittest.TestCase):
    def setUp(self):
        self.original_token = server._API_TOKEN
        server._API_TOKEN = "test-token"
        self.auth_headers = {"X-WordTTS-Token": "test-token"}
        self.output_dir = tempfile.TemporaryDirectory()
        self.output_patch = mock.patch.object(
            server.core,
            "OUTPUT_BASE",
            self.output_dir.name,
        )
        self.output_patch.start()
        server._sessions.clear()
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        server._sessions.clear()
        self.output_patch.stop()
        self.output_dir.cleanup()
        server._API_TOKEN = self.original_token

    def _archive(self, session_id: str, *, audio_bytes: bytes | None = None):
        session = server.SessionState(session_id)
        session.done = True
        filename = f"{session_id}.mp3"
        audio_dir = Path(session.session_dir, "audio")
        audio_dir.mkdir(parents=True, exist_ok=True)
        payload = audio_bytes if audio_bytes is not None else f"audio:{session_id}".encode()
        Path(audio_dir, filename).write_bytes(payload)
        zip_payload = f"zip:{session_id}".encode()
        Path(session.session_dir, "output.zip").write_bytes(zip_payload)

        file_list = [{
            "id": session_id,
            "filename": filename,
            "doc_type": "朗读",
            "category": "测试",
            "text": f"text for {session_id}",
            "text_preview": f"preview for {session_id}",
        }]
        progress = {
            "source_file": f"{session_id}.docx",
            "created_at": "2026-07-29T08:00:00",
            "total_items": 1,
            "completed": 1,
            "failed": 0,
            "config": {"format": "mp3", "preview": False},
            "items": [{
                "id": session_id,
                "filename": filename,
                "doc_type": "朗读",
                "status": "done",
            }],
        }
        record = server.archive_history_record(
            session,
            progress,
            file_list,
            str(Path(session.session_dir, "output.zip")),
        )
        self.assertIsNotNone(record)
        return {
            "session": session,
            "progress": progress,
            "file_list": file_list,
            "record": record,
            "audio_bytes": payload,
            "zip_bytes": zip_payload,
        }

    def test_history_limit_physically_evicts_oldest_record(self):
        archived = []
        start = datetime(2026, 7, 29, 9, 0, 0)
        with mock.patch.object(server, "datetime") as clock:
            clock.now.side_effect = [start + timedelta(seconds=index) for index in range(21)]
            for index in range(21):
                archived.append(self._archive(f"task-{index:02d}"))

        records = server.list_history_records()
        self.assertEqual(len(records), server.MAX_HISTORY_RECORDS)
        self.assertFalse(Path(archived[0]["session"].session_dir).exists())
        self.assertTrue(Path(archived[-1]["session"].session_dir).is_dir())
        self.assertNotIn(archived[0]["record"]["id"], {item["id"] for item in records})
        persisted_dirs = [
            path for path in Path(self.output_dir.name).iterdir()
            if path.is_dir() and path.name.startswith(server.SESSION_DIR_PREFIX)
        ]
        self.assertEqual(len(persisted_dirs), server.MAX_HISTORY_RECORDS)

    def test_archiving_same_session_is_idempotent(self):
        archived = self._archive("same-session")
        first_id = archived["record"]["id"]

        second = server.archive_history_record(
            archived["session"],
            archived["progress"],
            archived["file_list"],
            str(Path(archived["session"].session_dir, "output.zip")),
        )

        self.assertIsNotNone(second)
        self.assertEqual(second["id"], first_id)
        records = server.list_history_records()
        self.assertEqual([record["id"] for record in records], [first_id])

    def test_history_survives_restart_and_supports_detail_download_and_delete(self):
        archived = self._archive("restart-safe")
        record_id = archived["record"]["id"]
        filename = archived["file_list"][0]["filename"]
        session_dir = Path(archived["session"].session_dir)

        # 新进程不会保留内存会话；历史 API 必须仅凭磁盘清单恢复。
        server._sessions.clear()
        self.client.close()
        self.client = TestClient(server.app)

        response = self.client.get("/api/history", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["records"]], [record_id])

        response = self.client.get(f"/api/history/{record_id}", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        detail = response.json()
        self.assertEqual(detail["source_filename"], "restart-safe.docx")
        self.assertEqual(detail["files"][0]["filename"], filename)
        self.assertTrue(detail["files"][0]["available"])

        response = self.client.get(
            f"/api/history/{record_id}/file/{filename}",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, archived["audio_bytes"])

        response = self.client.get(
            f"/api/history/{record_id}/zip",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, archived["zip_bytes"])

        response = self.client.delete(
            f"/api/history/{record_id}",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        self.assertFalse(session_dir.exists())
        self.assertEqual(server.list_history_records(), [])

    def test_cleanup_releases_completed_session_but_preserves_history_files(self):
        archived = self._archive("cleanup-preserves-history")
        session = archived["session"]
        session_dir = Path(session.session_dir)
        server._sessions[session.session_id] = session

        response = self.client.post(
            f"/api/cleanup/{session.session_id}",
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["archived"])
        self.assertNotIn(session.session_id, server._sessions)
        self.assertTrue(session_dir.is_dir())
        self.assertEqual(
            [item["id"] for item in server.list_history_records()],
            [archived["record"]["id"]],
        )

    def test_completed_sse_replay_keeps_history_id(self):
        session = server.SessionState("sse-history-contract")
        server._sessions[session.session_id] = session
        history_id = server.history_id_for_session(session.session_id)
        server.push_event(session, {
            "type": "log",
            "entry": {
                "seq": 1,
                "level": "success",
                "stage": "complete",
                "kind": "summary",
                "status": "success",
                "key": "task:summary",
                "title": "全部处理完成",
            },
        })
        server.push_event(session, {
            "type": "done",
            "zip_path": "/internal/output.zip",
            "history_id": history_id,
        })
        server.push_event(session, {"type": "end"})

        response = self.client.get(
            f"/api/progress/{session.session_id}",
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[0]["type"], "log_init")
        self.assertEqual(events[0]["entries"][0]["key"], "task:summary")
        done_event = next(event for event in events if event["type"] == "done")
        self.assertEqual(done_event["history_id"], history_id)
        self.assertLess(
            next(index for index, event in enumerate(events) if event["type"] == "log_init"),
            next(index for index, event in enumerate(events) if event["type"] == "done"),
        )

    def test_history_rejects_illegal_or_unlisted_filenames(self):
        archived = self._archive("safe-path")
        record_id = archived["record"]["id"]

        response = self.client.get(
            f"/api/history/{record_id}/file-path",
            params={"filename": "../outside.mp3"},
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.get(
            f"/api/history/{record_id}/file-path",
            params={"filename": "not-in-manifest.mp3"},
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_history_with_all_assets_missing_remains_listable_and_deletable(self):
        archived = self._archive("missing-assets")
        record_id = archived["record"]["id"]
        filename = archived["file_list"][0]["filename"]
        session_dir = Path(archived["session"].session_dir)
        Path(session_dir, "audio", filename).unlink()
        Path(session_dir, "output.zip").unlink()

        response = self.client.get("/api/history", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["records"]], [record_id])
        self.assertEqual(response.json()["records"][0]["available_files"], 0)
        self.assertFalse(response.json()["records"][0]["zip_available"])

        response = self.client.get(f"/api/history/{record_id}", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["files"][0]["available"])

        response = self.client.delete(
            f"/api/history/{record_id}",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        self.assertFalse(session_dir.exists())

    def test_clearing_old_outputs_invalidates_history_manifest(self):
        archived = self._archive("regenerated")
        session_dir = archived["session"].session_dir
        manifest_path = Path(session_dir, server.HISTORY_MANIFEST_FILENAME)
        self.assertTrue(manifest_path.is_file())

        server.clear_generated_outputs(session_dir)

        self.assertFalse(manifest_path.exists())
        self.assertEqual(server.list_history_records(), [])


if __name__ == "__main__":
    unittest.main()
