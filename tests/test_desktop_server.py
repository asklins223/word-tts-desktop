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
        self.assertEqual(
            response.json()["backend_contract_version"],
            server.core.BACKEND_CONTRACT_VERSION,
        )
        self.assertNotIn("test-token", response.text)

    def test_query_token_supports_eventsource_and_media_urls(self):
        response = self.client.get("/api/health?token=test-token")
        self.assertEqual(response.status_code, 200)

    def test_config_reads_local_catalog_before_online_refresh(self):
        calls = []
        local_catalog = {
            "_meta": {"catalog_source": "live"},
            "voices": [],
            "filters": [],
        }

        def load_catalog(force_refresh):
            calls.append(force_refresh)
            return local_catalog

        with mock.patch.object(server, "_load_voice_catalog_sync", side_effect=load_catalog):
            result = asyncio.run(server.get_config())

        self.assertEqual(calls, [False])
        self.assertEqual(result["voice_catalog_meta"]["catalog_source"], "live")


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

    def test_malformed_persisted_output_path_is_rejected_safely(self):
        fingerprint = {"sha256": "same", "size": 1}
        progress = {
            "source_fingerprint": fingerprint,
            "config": {
                "generation_mode": server.core.GENERATION_MODE_SINGLE,
                "audio_algorithm_version": server.core.AUDIO_ALGORITHM_VERSION,
                "parser_version": server.core.PARSER_VERSION,
            },
            "items": [{
                "status": "done",
                "raw_item": {},
                "output_path": {"unexpected": "object"},
            }],
        }

        self.assertFalse(server.progress_is_reusable(progress, fingerprint))

    def test_malformed_composite_progress_is_rejected_safely(self):
        fingerprint = {"sha256": "same", "size": 1}
        progress = {
            "source_fingerprint": fingerprint,
            "config": {
                "generation_mode": server.core.GENERATION_MODE_COMPOSITE,
                "audio_algorithm_version": server.core.AUDIO_ALGORITHM_VERSION,
                "parser_version": server.core.PARSER_VERSION,
            },
            "items": [{"id": "q1", "status": "pending", "raw_item": {}}],
            "composite_work_plan": [{
                "work_id": "composite:q1",
                "item_ids": ["q1"],
                "item_count": 1,
            }],
            "composite_works": [],
        }

        self.assertFalse(server.progress_is_reusable(progress, fingerprint))

    def test_composite_progress_rejects_work_over_item_safety_limit(self):
        fingerprint = {"sha256": "same", "size": 1}
        item_count = server.core.COMPOSITE_MAX_ITEMS_PER_WORK + 1
        item_ids = [f"q{index}" for index in range(item_count)]
        progress = {
            "source_fingerprint": fingerprint,
            "config": {
                "generation_mode": server.core.GENERATION_MODE_COMPOSITE,
                "audio_algorithm_version": server.core.AUDIO_ALGORITHM_VERSION,
                "parser_version": server.core.PARSER_VERSION,
            },
            "items": [
                {"id": item_id, "status": "pending", "raw_item": {}}
                for item_id in item_ids
            ],
            "composite_work_plan": [{
                "work_id": "composite:over-limit",
                "item_ids": item_ids,
                "item_count": item_count,
            }],
            "composite_works": {
                "composite:over-limit": {
                    "status": "pending",
                    "item_ids": item_ids,
                    "item_count": item_count,
                },
            },
        }

        self.assertFalse(server.progress_is_reusable(progress, fingerprint))

    def test_legacy_single_progress_without_generation_mode_matches_new_config(self):
        normalized = server.core.normalize_tts_config({
            "generation_mode": server.core.GENERATION_MODE_SINGLE,
        })
        legacy_config = dict(normalized)
        legacy_config.pop("generation_mode", None)

        self.assertTrue(server._configs_match(legacy_config, normalized))
        self.assertTrue(
            server._configs_match(
                {**legacy_config, "generation_mode": server.core.GENERATION_MODE_SINGLE},
                normalized,
            )
        )

    def test_progress_count_is_always_an_integer(self):
        self.assertEqual(server._integer_progress_count(3.6, 37), 4)
        self.assertEqual(server._integer_progress_count(33.4, 37), 33)
        self.assertEqual(server._integer_progress_count(999.9, 37), 37)
        self.assertEqual(server._integer_progress_count(float('nan'), 37), 0)

    def test_parse_cache_rejects_previous_cache_version(self):
        with tempfile.TemporaryDirectory() as session_dir:
            old_fingerprint = {
                "cache_version": server.PARSE_CACHE_VERSION - 1,
                "sha256": "same",
                "size": 1,
            }
            Path(session_dir, server.SOURCE_META_FILENAME).write_text(
                json.dumps(old_fingerprint), encoding="utf-8"
            )
            Path(session_dir, "parsed.json").write_text(
                json.dumps([{"items": [{"filename_stem": "旧命名"}]}]),
                encoding="utf-8",
            )

            current_fingerprint = {
                **old_fingerprint,
                "cache_version": server.PARSE_CACHE_VERSION,
            }
            self.assertIsNone(server.load_parse_cache(session_dir, current_fingerprint))

    def test_parse_cache_rejects_previous_parser_version(self):
        with tempfile.TemporaryDirectory() as session_dir:
            old_fingerprint = {
                "cache_version": server.PARSE_CACHE_VERSION,
                "parser_version": server.core.PARSER_VERSION - 1,
                "sha256": "same",
                "size": 1,
            }
            Path(session_dir, server.SOURCE_META_FILENAME).write_text(
                json.dumps(old_fingerprint), encoding="utf-8"
            )
            Path(session_dir, "parsed.json").write_text(
                json.dumps([{"items": [{"filename_stem": "旧命名"}]}]),
                encoding="utf-8",
            )

            current_fingerprint = {
                **old_fingerprint,
                "parser_version": server.core.PARSER_VERSION,
            }
            self.assertIsNone(server.load_parse_cache(session_dir, current_fingerprint))

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
            # 这些时间线测试覆盖原有单段流程；产品默认由前端/API
            # 选择 composite_cut，旧流程必须显式声明以免测试掩盖回退。
            "generation_mode": server.core.GENERATION_MODE_SINGLE,
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

    def test_missing_generation_mode_uses_composite_default(self):
        session = server.SessionState("missing-generation-mode")
        captured = {}

        async def fake_generate(_session, _source_filename, _filepath, config):
            captured.update(config)

        async def exercise():
            with mock.patch.object(server, "_generate_audio_stream", new=fake_generate):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    {
                        "format": "mp3",
                        "quality": "128 kbps（标准）",
                    },
                )

        asyncio.run(exercise())
        self.assertEqual(
            captured["generation_mode"],
            server.core.GENERATION_MODE_COMPOSITE,
        )

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

    def test_batch_download_failure_persists_works_id_for_retry(self):
        """普通批量下载失败后，下一轮只下载原作品而不再次提交计费。"""
        session = server.SessionState("batch-works-resume")
        session.parse_results = self._parse_results("需要重试下载的内容")
        first_specs = []
        second_specs = []

        async def first_batch(item_specs, progress_callback=None, **_kwargs):
            first_specs.extend(dict(spec) for spec in item_specs)
            spec = item_specs[0]
            item_id = str(spec["item_id"])
            segment_id = f"{item_id}::segment:0"
            payload = {
                "item_id": item_id,
                "status": "submitted",
                "completed_segments": 1,
                "total_segments": 1,
                "segment_id": segment_id,
                "works_ids": {segment_id: "works-paid-once"},
                "ambiguous_works_ids": [],
            }
            await progress_callback(payload)
            await progress_callback({
                **payload,
                "status": "error",
                "completed_segments": 0,
                "error": "下载页暂时不可用",
            })
            return {
                item_id: {
                    "audio": None,
                    "error": "讯飞批量统一下载异常：下载页暂时不可用",
                },
            }

        async def second_batch(item_specs, progress_callback=None, **_kwargs):
            second_specs.extend(dict(spec) for spec in item_specs)
            spec = item_specs[0]
            item_id = str(spec["item_id"])
            segment_id = f"{item_id}::segment:0"
            self.assertEqual(
                spec["xunfei_works_ids"],
                {segment_id: "works-paid-once"},
            )
            await progress_callback({
                "item_id": item_id,
                "status": "submitted",
                "completed_segments": 1,
                "total_segments": 1,
                "segment_id": segment_id,
                "works_ids": {segment_id: "works-paid-once"},
                "ambiguous_works_ids": [],
            })
            return {item_id: {"audio": object(), "error": None}}

        def fake_export(_audio, _fmt, _quality, output_path):
            Path(output_path).write_bytes(b"retried-audio")

        async def exercise(batch):
            with mock.patch.object(
                server,
                "source_fingerprint",
                return_value={"size": 1},
            ), mock.patch.object(
                server.core,
                "_synth_items_batch",
                new=batch,
            ), mock.patch.object(
                server.core,
                "export_audio",
                side_effect=fake_export,
            ), mock.patch.object(
                server.core._xunfei,
                "close_session",
                new=mock.AsyncMock(),
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    self._config(),
                )

        asyncio.run(exercise(first_batch))
        persisted = server.core.load_progress(session.session_dir)
        self.assertEqual(
            persisted["items"][0]["xunfei_works_ids"],
            {
                f"{persisted['items'][0]['id']}::segment:0": "works-paid-once",
            },
        )
        self.assertEqual(len(first_specs), 1)

        asyncio.run(exercise(second_batch))
        self.assertEqual(len(second_specs), 1)
        self.assertTrue(session.final_done["zip_available"])
        self.assertEqual(
            (session.final_done["completed"], session.final_done["failed"]),
            (1, 0),
        )

    def test_batch_invalid_works_id_is_removed_before_retry(self):
        """讯飞确认作品不存在后，下一轮必须重新提交而不是复用坏 ID。"""
        session = server.SessionState("batch-invalid-works-resume")
        session.parse_results = self._parse_results("需要重新合成的内容")
        second_specs = []

        async def first_batch(item_specs, progress_callback=None, **_kwargs):
            spec = item_specs[0]
            item_id = str(spec["item_id"])
            segment_id = f"{item_id}::segment:0"
            await progress_callback({
                "item_id": item_id,
                "status": "submitted",
                "completed_segments": 1,
                "total_segments": 1,
                "segment_id": segment_id,
                "works_ids": {segment_id: "works-no-longer-exists"},
                "ambiguous_works_ids": [],
                "invalid_works_ids": [],
            })
            await progress_callback({
                "item_id": item_id,
                "status": "error",
                "completed_segments": 0,
                "total_segments": 1,
                "segment_id": segment_id,
                "works_ids": {},
                "ambiguous_works_ids": [],
                "invalid_works_ids": [segment_id],
                "error": "讯飞作品列表中未找到该 worksId，已标记为失效",
            })
            return {
                item_id: {
                    "audio": None,
                    "error": "讯飞作品列表中未找到该 worksId，已标记为失效",
                },
            }

        async def second_batch(item_specs, **_kwargs):
            second_specs.extend(dict(spec) for spec in item_specs)
            self.assertEqual(item_specs[0].get("xunfei_works_ids"), {})
            return {
                str(spec["item_id"]): {"audio": object(), "error": None}
                for spec in item_specs
            }

        def fake_export(_audio, _fmt, _quality, output_path):
            Path(output_path).write_bytes(b"fresh-audio")

        async def exercise(batch):
            with mock.patch.object(
                server,
                "source_fingerprint",
                return_value={"size": 1},
            ), mock.patch.object(
                server.core,
                "_synth_items_batch",
                new=batch,
            ), mock.patch.object(
                server.core,
                "export_audio",
                side_effect=fake_export,
            ), mock.patch.object(
                server.core._xunfei,
                "close_session",
                new=mock.AsyncMock(),
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    self._config(),
                )

        asyncio.run(exercise(first_batch))
        persisted = server.core.load_progress(session.session_dir)
        self.assertEqual(persisted["items"][0]["xunfei_works_ids"], {})

        asyncio.run(exercise(second_batch))
        self.assertEqual(len(second_specs), 1)
        self.assertTrue(session.final_done["zip_available"])
        self.assertEqual(
            (session.final_done["completed"], session.final_done["failed"]),
            (1, 0),
        )

    def test_batch_ambiguous_works_name_is_persisted_and_used_for_reconciliation(self):
        """确认提交但漏捕获 ID 时，断点只携带作品名对账，不重复提交。"""
        session = server.SessionState("batch-ambiguous-works-resume")
        session.parse_results = self._parse_results("需要对账的内容")
        second_specs = []

        async def first_batch(item_specs, progress_callback=None, **_kwargs):
            spec = item_specs[0]
            item_id = str(spec["item_id"])
            segment_id = f"{item_id}::segment:0"
            await progress_callback({
                "item_id": item_id,
                "status": "error",
                "completed_segments": 0,
                "total_segments": 1,
                "segment_id": segment_id,
                "works_ids": {},
                "ambiguous_works_ids": [segment_id],
                "ambiguous_works_names": {segment_id: "wordtts_paid_once"},
                "invalid_works_ids": [],
                "error": "已确认提交但未捕获 worksId",
            })
            return {
                item_id: {
                    "audio": None,
                    "error": "已确认提交但未捕获 worksId",
                },
            }

        async def second_batch(item_specs, progress_callback=None, **_kwargs):
            second_specs.extend(dict(spec) for spec in item_specs)
            spec = item_specs[0]
            item_id = str(spec["item_id"])
            segment_id = f"{item_id}::segment:0"
            self.assertEqual(
                spec["xunfei_ambiguous_works"],
                {segment_id: "wordtts_paid_once"},
            )
            self.assertEqual(spec["xunfei_works_ids"], {})
            await progress_callback({
                "item_id": item_id,
                "status": "submitted",
                "completed_segments": 1,
                "total_segments": 1,
                "segment_id": segment_id,
                "works_ids": {segment_id: "works-reconciled"},
                "ambiguous_works_ids": [],
                "ambiguous_works_names": {},
                "invalid_works_ids": [],
            })
            return {item_id: {"audio": object(), "error": None}}

        def fake_export(_audio, _fmt, _quality, output_path):
            Path(output_path).write_bytes(b"reconciled-audio")

        async def exercise(batch):
            with mock.patch.object(
                server,
                "source_fingerprint",
                return_value={"size": 1},
            ), mock.patch.object(
                server.core,
                "_synth_items_batch",
                new=batch,
            ), mock.patch.object(
                server.core,
                "export_audio",
                side_effect=fake_export,
            ), mock.patch.object(
                server.core._xunfei,
                "close_session",
                new=mock.AsyncMock(),
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    self._config(),
                )

        asyncio.run(exercise(first_batch))
        persisted = server.core.load_progress(session.session_dir)
        self.assertEqual(
            persisted["items"][0]["xunfei_ambiguous_works"],
            {
                f"{persisted['items'][0]['id']}::segment:0": "wordtts_paid_once",
            },
        )

        asyncio.run(exercise(second_batch))
        self.assertEqual(len(second_specs), 1)
        self.assertTrue(session.final_done["zip_available"])
        self.assertEqual(
            (session.final_done["completed"], session.final_done["failed"]),
            (1, 0),
        )

    def test_composite_task_emits_work_phases_and_mode_in_done(self):
        session = server.SessionState("composite-timeline")
        session.parse_results = self._parse_results("需要合并生成的内容")
        config = {
            **self._config(),
            "generation_mode": server.core.GENERATION_MODE_COMPOSITE,
        }

        async def fake_composite(item_specs, progress_callback=None, **kwargs):
            work_plan = kwargs["work_plan"]
            self.assertEqual(len(work_plan), 1)
            work_id = work_plan[0]["work_id"]
            for status, extra in (
                ("submitted", {"works_id": "works-test"}),
                ("downloaded", {"works_id": "works-test"}),
                (
                    "cut",
                    {
                        "works_id": "works-test",
                        "cut_item_count": 1,
                        "cut_diagnostics": {
                            "item_count": 1,
                            "strategy": "outer_edge_trim",
                            "selected_count": 0,
                        },
                    },
                ),
            ):
                await progress_callback({
                    "work_id": work_id,
                    "status": status,
                    **extra,
                })
            return {
                str(spec["item_id"]): {"audio": object(), "error": None}
                for spec in item_specs
            }

        def fake_export(_audio, _fmt, _quality, output_path):
            Path(output_path).write_bytes(b"composite-audio")

        async def exercise():
            with mock.patch.object(server, "source_fingerprint", return_value={"size": 1}), mock.patch.object(
                server.core,
                "_synth_items_batch_composite",
                new=fake_composite,
            ), mock.patch.object(
                server.core,
                "export_audio",
                side_effect=fake_export,
            ), mock.patch.object(
                server.core._xunfei,
                "close_session",
                new=mock.AsyncMock(),
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    config,
                )

        asyncio.run(exercise())
        work_logs = [
            event["entry"]
            for event in session.event_journal
            if event["type"] == "log" and event["entry"].get("kind") == "work"
        ]
        self.assertEqual(
            [entry["work"]["status"] for entry in work_logs],
            ["submitted", "downloaded", "cut"],
        )
        self.assertEqual(
            work_logs[-1]["work"]["cut_diagnostics"]["strategy"],
            "outer_edge_trim",
        )
        self.assertIn("切割诊断", work_logs[-1]["detail"])
        phases = [
            event.get("phase")
            for event in session.event_journal
            if event["type"] == "stats"
        ]
        self.assertTrue(
            {
                "composite-plan",
                "composite-submit",
                "composite-download",
                "composite-cut",
                "composite-export",
                "package",
                "archive",
            }.issubset(phases)
        )
        self.assertLess(phases.index("package"), phases.index("archive"))
        stats_indices = [
            index
            for index, event in enumerate(session.event_journal)
            if event["type"] == "stats"
        ]
        done_index = next(
            index
            for index, event in enumerate(session.event_journal)
            if event["type"] == "done"
        )
        self.assertLess(max(stats_indices), done_index)
        self.assertEqual(
            [
                event["entry"]
                for event in session.event_journal
                if event["type"] == "log"
                and event["entry"].get("kind") == "item"
                and event["entry"].get("status") == "success"
            ],
            [],
        )
        done = session.final_done
        self.assertIsNotNone(done)
        self.assertEqual(done["generation_mode"], server.core.GENERATION_MODE_COMPOSITE)
        self.assertEqual(
            done["composite_works"],
            {
                "kind": "composite_batch",
                "completed": 1,
                "total": 1,
                "submitted": 1,
                "downloaded": 1,
                "failed": 0,
                "sliced": 1,
                "exported": 1,
            },
        )
        self.assertEqual((done["completed"], done["failed"], done["total"]), (1, 0, 1))
        self.assertTrue(done["zip_available"])

        # 已完成任务重新打开/重放时不再有待处理 item_specs，但作品汇总
        # 仍应来自落盘进度，而不是被重置为 0 个作品。
        asyncio.run(exercise())
        replay_done = session.final_done
        self.assertEqual(replay_done["composite_works"], done["composite_works"])

    def test_retry_does_not_emit_previous_failures_in_initial_stats(self):
        """重试任务的首个统计不能把上一轮失败数带进本轮。"""
        session = server.SessionState("composite-retry-initial-stats")
        session.parse_results = self._parse_results("需要重试的内容")
        config = {
            **self._config(),
            "generation_mode": server.core.GENERATION_MODE_COMPOSITE,
        }
        fingerprint = {"size": 1}
        persisted = server.core.build_progress(
            "lesson.docx",
            "/missing/lesson.docx",
            session.parse_results,
            config,
        )
        persisted["source_fingerprint"] = fingerprint
        persisted["items"][0]["status"] = "error"
        persisted["items"][0]["error"] = "上一次标注失败"
        persisted["failed"] = 1

        async def fake_composite(item_specs, **_kwargs):
            return {
                str(spec["item_id"]): {"audio": object(), "error": None}
                for spec in item_specs
            }

        def fake_export(_audio, _fmt, _quality, output_path):
            Path(output_path).write_bytes(b"retry-audio")

        async def exercise():
            with mock.patch.object(
                server,
                "source_fingerprint",
                return_value=fingerprint,
            ), mock.patch.object(
                server.core,
                "load_progress",
                return_value=persisted,
            ), mock.patch.object(
                server.core,
                "_synth_items_batch_composite",
                new=fake_composite,
            ), mock.patch.object(
                server.core,
                "export_audio",
                side_effect=fake_export,
            ), mock.patch.object(
                server.core._xunfei,
                "close_session",
                new=mock.AsyncMock(),
            ):
                await server.generate_audio_stream(
                    session,
                    "lesson.docx",
                    "/missing/lesson.docx",
                    config,
                )

        asyncio.run(exercise())
        stats = [
            event
            for event in session.event_journal
            if event["type"] == "stats"
        ]
        self.assertTrue(stats)
        self.assertEqual(stats[0]["failed"], 0)
        self.assertEqual(stats[0]["processed"], 0)
        self.assertEqual(stats[0]["failed_items"], [])
        self.assertEqual(
            (session.final_done["completed"], session.final_done["failed"]),
            (1, 0),
        )

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
            "config": {
                "format": "mp3",
                "preview": False,
                "generation_mode": server.core.GENERATION_MODE_COMPOSITE,
            },
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
        self.assertEqual(record["generation_mode"], server.core.GENERATION_MODE_COMPOSITE)
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

    def test_legacy_history_manifest_defaults_to_single_generation_mode(self):
        archived = self._archive("legacy-history-mode")
        manifest_path = Path(archived["session"].session_dir, server.HISTORY_MANIFEST_FILENAME)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("generation_mode", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        record = server.get_history_record(archived["record"]["id"])

        self.assertEqual(record["generation_mode"], server.core.GENERATION_MODE_SINGLE)

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
