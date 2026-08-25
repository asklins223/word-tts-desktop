from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import app_paths


class AppPathTests(unittest.TestCase):
    def test_configured_data_dir_is_shared_and_created(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = os.path.join(temp_dir, "WordTTS-data")
            with patch.dict(os.environ, {"WORDTTS_DATA_DIR": configured}):
                self.assertEqual(app_paths.data_dir(), os.path.abspath(configured))
                self.assertFalse(os.path.exists(configured))
                self.assertEqual(app_paths.ensure_data_dir(), os.path.abspath(configured))
                self.assertTrue(os.path.isdir(configured))

    def test_source_resource_dir_is_project_root(self):
        self.assertEqual(app_paths.resource_dir(), app_paths.PROJECT_ROOT)

    def test_source_data_dir_is_separate_from_resources(self):
        with patch.dict(os.environ, {"WORDTTS_DATA_DIR": ""}):
            self.assertEqual(
                app_paths.data_dir(),
                os.path.join(app_paths.PROJECT_ROOT, ".runtime"),
            )


if __name__ == "__main__":
    unittest.main()
