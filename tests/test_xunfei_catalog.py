import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import xunfei_voice_catalog as catalog


class XunfeiCatalogTests(unittest.TestCase):
    def test_common_list_fetches_all_pages_with_the_multi_speaker_endpoint(self):
        first_page = {
            "code": 0,
            "data": {
                "records": [{"commonId": index, "speakerName": f"Voice {index}"} for index in range(20)],
                "total": 21,
                "current": 1,
                "pages": 2,
            },
        }
        second_page = {
            "code": 0,
            "data": {
                "records": [{"commonId": 20, "speakerName": "Voice 20"}],
                "total": 21,
                "current": 2,
                "pages": 2,
            },
        }

        with patch.object(catalog, "_request_json", side_effect=[first_page, second_page]) as request:
            records = catalog.fetch_common_list_speakers(timeout=1)

        self.assertEqual(len(records), 21)
        self.assertEqual(request.call_args_list[0].args[0], catalog.SPEAKER_COMMON_LIST_URL)
        self.assertEqual(request.call_args_list[0].kwargs["params"], {"current": 1, "size": 20})
        self.assertEqual(request.call_args_list[1].kwargs["params"], {"current": 2, "size": 20})

    def test_common_list_catalog_uses_base_name_and_keeps_default_variant_identifiers(self):
        common = [{
            "commonId": 10001135,
            "speakerName": "欣畅",
            "speakerGender": 2,
            "speakerLanguage": "普通话",
            "speakerStyle": "激情力度",
            "tag": "直播|广告",
        }]
        flat = [
            {
                "speakerNo": 591199169,
                "speakerName": "欣畅-Pro+",
                "speakerGender": 2,
                "speakerLanguage": "普通话",
                "commonSpeaker": {"commonId": 10001135, "speakerName": "欣畅"},
                "audioUrl": "https://example.test/pro-plus.wav",
            },
            {
                "speakerNo": 548016606,
                "speakerName": "欣畅-Pro",
                "speakerGender": 2,
                "speakerLanguage": "普通话",
                "commonSpeaker": {"commonId": 10001135, "speakerName": "欣畅"},
            },
        ]

        payload = catalog.normalize_catalog(
            flat,
            composite_raw_voices=catalog.build_composite_speakers(common, flat),
            source="test",
        )
        composite = next(item for item in payload["composite_voices"] if item["name"] == "欣畅")
        variant = next(item for item in payload["voices"] if item["name"] == "欣畅-Pro+")

        self.assertEqual(composite["speaker_no"], 591199169)
        self.assertEqual(composite["common_id"], 10001135)
        self.assertEqual(composite["variant_names"], ["欣畅-Pro+", "欣畅-Pro"])
        self.assertEqual(composite["variant_keys"], ["speaker:591199169", "speaker:548016606"])
        self.assertEqual(variant["common_id"], 10001135)
        self.assertEqual(variant["composite_name"], "欣畅")
        self.assertEqual(variant["composite_key"], composite["key"])

    def test_composite_catalog_keeps_provider_variant_labels(self):
        common = [{"commonId": 10001135, "speakerName": "欣畅"}]
        flat = [
            {
                "speakerNo": 591199169,
                "speakerName": "欣畅-Pro+",
                "emotDesc": "Pro+",
                "commonSpeaker": {"commonId": 10001135, "speakerName": "欣畅"},
            },
            {
                "speakerNo": 548016606,
                "speakerName": "欣畅-Pro",
                "emotDesc": "Pro",
                "commonSpeaker": {"commonId": 10001135, "speakerName": "欣畅"},
            },
        ]

        composite = catalog.build_composite_speakers(common, flat)
        self.assertEqual(composite[0]["_composite_variant_labels"], ["Pro+", "Pro"])
        payload = catalog.normalize_catalog(
            flat,
            composite_raw_voices=composite,
            source="test",
        )
        group = next(item for item in payload["composite_voices"] if item["name"] == "欣畅")
        self.assertEqual(group["variant_labels"], ["Pro+", "Pro"])
        self.assertEqual(
            next(item for item in payload["voices"] if item["name"] == "欣畅-Pro")["emot_desc"],
            "Pro",
        )

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

    def test_builtin_fallback_has_identifiers_for_default_composite_voices(self):
        payload = catalog.normalize_catalog([], source="builtin")

        by_key = {voice["key"]: voice for voice in payload["voices"]}
        self.assertEqual(by_key["amanda"]["speaker_no"], 544508087)
        self.assertEqual(by_key["george"]["speaker_no"], 593031758)


if __name__ == "__main__":
    unittest.main()
