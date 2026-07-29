from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import server


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

            event_types = []
            while not session.queue.empty():
                event_types.append(session.queue.get_nowait()["type"])
            return event_types

        event_types = asyncio.run(exercise())
        self.assertIn("error", event_types)
        self.assertEqual(event_types[-1], "end")


if __name__ == "__main__":
    unittest.main()
