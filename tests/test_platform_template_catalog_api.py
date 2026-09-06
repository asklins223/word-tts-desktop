from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.workflow_routes import WorkflowRuntime, install_workflow_api
from workflow.platform_template_catalog import PlatformTemplateCatalogService, PlatformTemplateCatalogStore


class PlatformTemplateCatalogApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wordtts-platform-template-catalog-api-")
        root = Path(self.temp.name)
        self.runtime = WorkflowRuntime.from_paths(root / "workflow.db", root / "artifacts", capability="test-capability")
        self.runtime.platform_template_catalog = PlatformTemplateCatalogService(
            PlatformTemplateCatalogStore(root / "platform-template-catalog.json")
        )
        self.app = FastAPI()
        install_workflow_api(self.app, runtime=self.runtime)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.runtime.database.close()
        self.temp.cleanup()

    def test_catalog_endpoint_and_manual_sync_start(self) -> None:
        headers = {"X-Desktop-Capability": "test-capability"}
        response = self.client.get("/api/v1/system-input/platform-template-catalog", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["catalog"]["record_count"], 0)
        self.assertEqual(response.json()["sync"]["status"], "IDLE")

        with patch.object(self.runtime.platform_template_catalog, "start_sync", return_value={
            "sync_id": "platform-template-catalog-test",
            "status": "RUNNING",
        }) as start_sync:
            response = self.client.post(
                "/api/v1/system-input/platform-template-catalog/sync",
                headers={**headers, "X-Idempotency-Key": "platform-template-sync-test"},
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["sync"]["sync_id"], "platform-template-catalog-test")
        start_sync.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
