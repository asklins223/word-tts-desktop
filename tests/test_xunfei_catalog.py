import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import xunfei_voice_catalog as catalog


class XunfeiCatalogTests(unittest.TestCase):
    def test_flat_list_discards_partial_response_when_a_later_page_fails(self):
        first_page = {
            "code": 0,
            "data": {
                "records": [{"speakerNo": index, "speakerName": f"Voice {index}"} for index in range(100)],
                "total": 200,
                "pages": 2,
            },
        }

        with patch.object(catalog, "_request_json", side_effect=[first_page, None]):
            records = catalog.fetch_flat_list_speakers(timeout=1)

        self.assertEqual(records, [])

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
        # 新版多人配音面板与主面板均使用 flat 变体（欣畅-Pro+），不再
        # 通过 common/list 的基础名称（欣畅）去重。旧的 base 聚合逻辑
        # 在 normalize 时会被识别为旧缓存并回退到变体列表，确保配置页
        # 可选项与面板内卡片一一对应。
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
        # 旧 base 列表会被识别并回退为变体列表
        composite_names = {item["name"] for item in payload["composite_voices"]}
        self.assertIn("欣畅-Pro+", composite_names)
        self.assertIn("欣畅-Pro", composite_names)
        variant = next(item for item in payload["voices"] if item["name"] == "欣畅-Pro+")
        self.assertEqual(variant["common_id"], 10001135)
        # 新逻辑的 composite 直接是变体，不再有独立的 base composite_key
        self.assertEqual(variant["name"], "欣畅-Pro+")

    def test_composite_catalog_keeps_provider_variant_labels(self):
        # 旧的 base 聚合（欣畅 -> Pro+/Pro）在新逻辑中不再作为 composite
        # 的展示名称，但 flat 变体的 emot_desc 仍需保留供 UI 展示。
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
        # 新逻辑的 composite 直接是变体列表，旧 base 列表会被回退
        payload = catalog.normalize_catalog(
            flat,
            composite_raw_voices=flat,
            source="test",
        )
        # 变体列表应保留 emot_desc
        self.assertEqual(
            next(item for item in payload["composite_voices"] if item["name"] == "欣畅-Pro")["emot_desc"],
            "Pro",
        )
        self.assertEqual(
            next(item for item in payload["voices"] if item["name"] == "欣畅-Pro")["emot_desc"],
            "Pro",
        )

    def test_composite_falls_back_when_old_base_list_has_same_length(self):
        common = [
            {"commonId": 1, "speakerName": "Voice A"},
            {"commonId": 2, "speakerName": "Voice B"},
        ]
        flat = [
            {
                "speakerNo": 101,
                "speakerName": "Voice A-Pro",
                "commonSpeaker": {"commonId": 1, "speakerName": "Voice A"},
            },
            {
                "speakerNo": 102,
                "speakerName": "Voice B-Pro",
                "commonSpeaker": {"commonId": 2, "speakerName": "Voice B"},
            },
        ]

        payload = catalog.normalize_catalog(
            flat,
            composite_raw_voices=common,
            source="test",
        )

        self.assertEqual(
            [
                item["name"]
                for item in payload["composite_voices"]
                if item["key"].startswith("speaker:")
            ],
            ["Voice A-Pro", "Voice B-Pro"],
        )

    def test_empty_composite_list_falls_back_to_flat_variants(self):
        flat = [{"speakerNo": 101, "speakerName": "Voice A-Pro"}]

        payload = catalog.normalize_catalog(
            flat,
            composite_raw_voices=[],
            source="test",
        )

        names = {item["name"] for item in payload["composite_voices"]}
        self.assertIn("Voice A-Pro", names)

    def test_composite_falls_back_when_legacy_group_keeps_same_single_speaker_id(self):
        common = [{"commonId": 1, "speakerName": "Voice A"}]
        flat = [{
            "speakerNo": 101,
            "speakerName": "Voice A-Pro",
            "commonSpeaker": {"commonId": 1, "speakerName": "Voice A"},
        }]

        payload = catalog.normalize_catalog(
            flat,
            composite_raw_voices=catalog.build_composite_speakers(common, flat),
            source="test",
        )

        self.assertEqual(
            [item["name"] for item in payload["composite_voices"]],
            ["Voice A-Pro", "Amanda", "George"],
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
