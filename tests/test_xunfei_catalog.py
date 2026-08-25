import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import xunfei_voice_catalog as catalog


class XunfeiCatalogTests(unittest.TestCase):
    def test_normalizes_filters_and_replaces_provider_wording(self):
        payload = catalog.normalize_catalog(
            [
                {
                    "speakerNo": "speaker-123",
                    "speakerName": "Demo Voice",
                    "speakerGender": 2,
                    "speakerLanguage": "英语",
                    "tag": "|新闻播报|",
                    "label": "主播",
                }
            ],
            source="test",
        )

        voice = next(item for item in payload["voices"] if item["key"] == "speaker:speaker-123")
        self.assertEqual(voice["gender"], "female")
        self.assertIn("新闻播报", voice["categories"])
        self.assertNotIn("主播", voice["categories"])
        self.assertTrue(any(item["key"] == "tag:新闻播报" for item in payload["filters"]))

    def test_refresh_failure_uses_last_successful_cache(self):
        with tempfile.TemporaryDirectory() as base_dir:
            saved = catalog.normalize_catalog(
                [{"speakerNo": "cached-1", "speakerName": "Cached Voice"}],
                source="test",
            )
            cache_path = catalog.save_catalog(saved, base_dir)
            self.assertEqual(
                cache_path,
                os.path.join(base_dir, "cache", "voices.json"),
            )
            self.assertFalse((Path(base_dir) / "resources").exists())

            with patch.object(catalog, "refresh_catalog", side_effect=RuntimeError("offline")):
                loaded = catalog.load_or_refresh_catalog(base_dir, force_refresh=True)

            keys = {item["key"] for item in loaded["voices"]}
            self.assertIn("speaker:cached-1", keys)
            self.assertEqual(loaded["_meta"]["catalog_source"], "cache")
            self.assertIn("offline", loaded["_meta"]["refresh_error"])


if __name__ == "__main__":
    unittest.main()
