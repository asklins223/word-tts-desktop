from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import server


class DesktopServerSecurityTests(unittest.TestCase):
    """活跃 API 面（/api/v1 + 中间件）的安全与契约测试。

    旧会话/生成引擎与旧 /api/* 路由已按方案 13.1 物理删除；这里保留的
    都是仍然在线上运行的代码：/api/v1 健康探针、能力校验、本地来源限制、
    配置目录刷新与音色资产缓存，以及旧路径的 410 收口行为。
    """

    def setUp(self):
        self.original_token = server._API_TOKEN
        server._API_TOKEN = "test-token"
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        server._API_TOKEN = self.original_token

    def test_versioned_health_uses_desktop_capability_header(self):
        response = self.client.get(
            "/api/v1/health",
            headers={"X-Desktop-Capability": "test-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["app"], "wordtts")
        self.assertEqual(
            response.json()["backend_contract_version"],
            server.core.BACKEND_CONTRACT_VERSION,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/health",
                headers={"X-WordTTS-Token": "test-token"},
            ).status_code,
            401,
        )

    def test_runtime_version_prefers_bundled_canonical_version(self):
        with mock.patch.object(server, "_package_version", return_value="3.0.1"), mock.patch.dict(
            os.environ,
            {"WORDTTS_VERSION": "99.0.0"},
            clear=False,
        ):
            self.assertEqual(server._runtime_version(), "3.0.1")

        with mock.patch.object(server, "_package_version", return_value=None), mock.patch.dict(
            os.environ,
            {"WORDTTS_VERSION": "V4.5.6"},
            clear=False,
        ):
            self.assertEqual(server._runtime_version(), "4.5.6")

        with tempfile.TemporaryDirectory() as directory:
            version_path = Path(directory) / "version.json"
            version_path.write_text('{"version": "V5.6.7"}', encoding="utf-8")
            with mock.patch.object(server, "RESOURCE_DIR", directory):
                self.assertEqual(server._package_version(), "5.6.7")

    def test_retired_legacy_routes_return_410_without_opt_in(self):
        """方案 13.1：旧路径物理删除后必须由中间件统一返回 410。"""
        previous = os.environ.pop("WORDTTS_LEGACY_API", None)
        try:
            for path in (
                "/api/health",
                "/api/config",
                "/api/generate",
                "/api/parse",
                "/api/history",
                "/api/diagnose",
            ):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 410, path)
                self.assertEqual(response.json()["error_code"], "API_VERSION_RETIRED")
                self.assertEqual(response.headers.get("X-API-Version"), "v1")
        finally:
            if previous is not None:
                os.environ["WORDTTS_LEGACY_API"] = previous

    def test_host_and_origin_are_restricted_to_local_desktop_requests(self):
        capability = {"X-Desktop-Capability": "test-token"}
        wrong_host = self.client.get(
            "/api/v1/health",
            headers={"Host": "attacker.example", **capability},
        )
        self.assertEqual(wrong_host.status_code, 403)
        self.assertEqual(wrong_host.json()["error_code"], "ORIGIN_NOT_ALLOWED")

        wrong_origin = self.client.get(
            "/api/v1/health",
            headers={"Origin": "https://attacker.example", **capability},
        )
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(wrong_origin.json()["error_code"], "ORIGIN_NOT_ALLOWED")

        electron_origin = self.client.get(
            "/api/v1/health",
            headers={"Origin": "null", **capability},
        )
        self.assertEqual(electron_origin.status_code, 200)

    def test_versioned_api_fails_closed_when_no_launch_token_exists(self):
        original_token = server._API_TOKEN
        original_capability = server._workflow_runtime.capability
        server._API_TOKEN = ""
        server._workflow_runtime.capability = server._DEVELOPMENT_WORKFLOW_CAPABILITY
        try:
            missing = self.client.get("/api/v1/not-a-route")
            self.assertEqual(missing.status_code, 401)
            self.assertEqual(missing.json()["error_code"], "UNAUTHORIZED")

            valid = self.client.get(
                "/api/v1/not-a-route",
                headers={"X-Desktop-Capability": server._DEVELOPMENT_WORKFLOW_CAPABILITY},
            )
            self.assertEqual(valid.status_code, 404)
        finally:
            server._API_TOKEN = original_token
            server._workflow_runtime.capability = original_capability

    def test_config_refreshes_multi_speaker_catalog_before_returning(self):
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

        self.assertEqual(calls, [True])
        self.assertEqual(result["voice_catalog_meta"]["catalog_source"], "live")

    def test_offline_desktop_start_uses_cached_catalog_without_network_refresh(self):
        calls = []
        local_catalog = {
            "_meta": {"catalog_source": "cache"},
            "voices": [],
            "filters": [],
        }

        def load_catalog(force_refresh):
            calls.append(force_refresh)
            return local_catalog

        with mock.patch.dict(os.environ, {"WORDTTS_ENABLE_REAL_PROVIDER": "0"}, clear=False), \
                mock.patch.object(server, "_load_voice_catalog_sync", side_effect=load_catalog):
            result = asyncio.run(server.get_config())

        self.assertEqual(calls, [False])
        self.assertEqual(result["voice_catalog_meta"]["catalog_source"], "cache")


class DesktopVoiceAssetCacheTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
