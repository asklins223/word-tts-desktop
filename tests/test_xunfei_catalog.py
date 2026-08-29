import json
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
                "records": [{"speakerNo": index, "speakerName": f"Voice {index}"} for index in range(40)],
                "total": 80,
                "pages": 2,
            },
        }

        with patch.object(catalog, "_request_json", side_effect=[first_page, None]) as request:
            records = catalog.fetch_flat_list_speakers(timeout=1)

        self.assertEqual(records, [])
        self.assertEqual(request.call_args_list[0].args[0], catalog.SPEAKER_FLAT_LIST_URL)
        self.assertEqual(
            request.call_args_list[0].kwargs["params"],
            {"current": 1, "size": 40, "scope": "common"},
        )

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

    def test_qry_tags_uses_signed_authenticated_request_when_credentials_are_supplied(self):
        response = {
            "code": 0,
            "data": {
                "tagCategories": [
                    {"tagName": "解说", "tagList": [{"tagName": "纪录片"}]}
                ]
            },
        }
        credentials = {
            "authorization": "session-for-test",
            "user_id": "user-for-test",
            "sid": "sid-for-test",
        }
        with patch.object(catalog, "_request_json", return_value=response) as request:
            categories = catalog.fetch_tag_categories(
                timeout=1,
                credentials=credentials,
            )

        self.assertEqual(categories[0]["tagName"], "解说")
        self.assertEqual(request.call_args.args[0], catalog.SPEAKER_TAGS_URL)
        self.assertEqual(request.call_args.kwargs["method"], "POST")
        body = request.call_args.kwargs["body"]
        self.assertEqual(body["param"], {"tagType": 1})
        self.assertEqual(body["base"]["userId"], "user-for-test")
        self.assertEqual(
            request.call_args.kwargs["headers"]["sign"],
            catalog._build_api_sign(body["param"], body["base"]),
        )

    def test_refresh_uses_common_list_as_primary_and_merges_flat_preview_audio(self):
        common = [
            {
                "commonId": 100,
                "speakerName": "多人基础音色",
                "speakerGender": 2,
                "speakerLanguage": "普通话",
                "tag": "最新|广告",
            }
        ]
        flat = [
            {
                "speakerNo": 123,
                "speakerName": "多人基础音色-Pro+",
                "audioUrl": "https://example.test/voice.wav",
                "commonSpeaker": {"commonId": 100, "speakerName": "多人基础音色"},
            }
        ]
        with patch.object(catalog, "fetch_common_list_speakers", return_value=common), \
                patch.object(catalog, "fetch_tag_categories", return_value=[]) as fetch_tags, \
                patch.object(catalog, "fetch_flat_list_speakers", return_value=flat) as fetch_flat:
            with tempfile.TemporaryDirectory() as base_dir:
                result = catalog.refresh_catalog(base_dir, timeout=1)

        self.assertEqual(result["_meta"]["speaker_list_endpoint"], catalog.SPEAKER_COMMON_LIST_URL)
        self.assertEqual(result["_meta"]["tags_source"], "frozen_snapshot")
        fetch_tags.assert_not_called()
        fetch_flat.assert_called_once_with(1)
        self.assertEqual(result["_meta"]["provider_count"], 1)
        names = {item["name"] for item in result["voices"]}
        self.assertIn("多人基础音色", names)
        self.assertNotIn("多人基础音色-Pro", names)
        voice = next(item for item in result["voices"] if item["name"] == "多人基础音色")
        self.assertEqual(voice["audio_url"], "https://example.test/voice.wav")
        self.assertEqual(result["_meta"]["preview_audio_endpoint"], catalog.SPEAKER_FLAT_LIST_URL)
        self.assertEqual(result["_meta"]["preview_audio_count"], 1)

    def test_refresh_keeps_cached_preview_when_flat_list_is_temporarily_empty(self):
        common = [{
            "commonId": 100,
            "speakerName": "已有缓存音色",
            "speakerGender": 2,
            "speakerLanguage": "普通话",
        }]
        with tempfile.TemporaryDirectory() as base_dir:
            cache_dir = Path(base_dir) / "cache"
            cache_dir.mkdir()
            (cache_dir / "voices.json").write_text(
                json.dumps({
                    "_meta": {"speaker_list_endpoint": catalog.SPEAKER_COMMON_LIST_URL},
                    "voices": [{
                        "common_id": 100,
                        "name": "已有缓存音色",
                        "audio_url": "https://example.test/cached.wav",
                    }],
                }),
                encoding="utf-8",
            )
            with patch.object(catalog, "fetch_common_list_speakers", return_value=common), \
                    patch.object(catalog, "fetch_tag_categories", return_value=[]), \
                    patch.object(catalog, "fetch_flat_list_speakers", return_value=[]):
                result = catalog.refresh_catalog(base_dir, timeout=1)

        voice = next(item for item in result["voices"] if item["name"] == "已有缓存音色")
        self.assertEqual(voice["audio_url"], "https://example.test/cached.wav")
        self.assertEqual(result["_meta"]["preview_audio_count"], 1)

    def test_merge_preview_audio_uses_common_id_then_name_fallback(self):
        common = [
            {"commonId": 100, "speakerName": "按 ID 匹配"},
            {"commonId": 200, "speakerName": "按名称匹配"},
            {"commonId": 300, "speakerName": "没有示例"},
        ]
        flat = [
            {
                "speakerNo": 1,
                "speakerName": "按 ID 匹配-Pro+",
                "audioUrl": "https://example.test/id.wav",
                "commonSpeaker": {"commonId": 100, "speakerName": "按 ID 匹配"},
            },
            {
                "speakerNo": 2,
                "speakerName": "按名称匹配-Pro+",
                "audioUrl": "https://example.test/name.wav",
                "commonSpeaker": {"speakerName": "按名称匹配"},
            },
            {
                "speakerNo": 3,
                "speakerName": "没有示例-Pro+",
                "commonSpeaker": {"commonId": 300, "speakerName": "没有示例"},
            },
        ]

        merged = catalog.merge_preview_audio(common, flat)

        self.assertEqual(merged[0]["audioUrl"], "https://example.test/id.wav")
        self.assertEqual(merged[1]["audioUrl"], "https://example.test/name.wav")
        self.assertNotIn("audioUrl", merged[2])

    def test_fixed_tag_snapshot_matches_complete_qry_tags_hierarchy(self):
        expected_labels = [
            "最热", "最新", "超拟人", "解说", "教育培训", "有声阅读",
            "体育解说", "游戏解说", "纪录片", "情感", "短视频", "新闻主持",
            "大会主持", "新闻", "资讯", "广告营销", "直播", "广告", "娱乐",
            "自创特色", "影视动漫", "语音助手", "方言", "多语种", "英语", "俄语",
            "法语", "西班牙语", "日语", "韩语", "德语", "阿拉伯语", "泰语",
            "马来语", "印尼语", "意大利语", "菲律宾语", "葡萄牙语", "越南语",
            "波兰语", "童声", "老年", "女声", "男声",
        ]

        self.assertEqual(
            catalog._tag_category_labels(catalog.FIXED_TAG_CATEGORIES),
            expected_labels,
        )
        self.assertEqual(len(catalog.FIXED_TAG_CATEGORIES), 14)
        self.assertEqual(catalog.FIXED_TAG_CATEGORIES[3]["id"], "1010010")
        self.assertEqual(
            catalog.FIXED_TAG_CATEGORIES[9]["tagList"][-1]["tagName"],
            "波兰语",
        )

    def test_tag_filters_do_not_truncate_the_fixed_snapshot(self):
        labels = catalog._tag_category_labels(catalog.FIXED_TAG_CATEGORIES)
        payload = catalog.normalize_catalog([], source="test")
        filter_labels = {item["label"] for item in payload["filters"]}
        self.assertTrue(set(labels).issubset(filter_labels))

    def test_common_list_uses_english_amanda_and_george_as_default_names(self):
        payload = catalog.normalize_catalog([
            {
                "commonId": 10001009,
                "speakerName": "英语-George",
                "speakerGender": 1,
                "speakerLanguage": "英语",
            },
            {
                "commonId": 10001089,
                "speakerName": "英语-Amanda",
                "speakerGender": 2,
                "speakerLanguage": "英语",
            },
        ], source="live")

        by_key = {item["key"]: item for item in payload["voices"]}
        self.assertEqual(by_key["amanda"]["name"], "英语-Amanda")
        self.assertEqual(by_key["george"]["name"], "英语-George")
        self.assertEqual(
            [item["name"] for item in payload["voices"][:2]],
            ["英语-Amanda", "英语-George"],
        )
        self.assertNotIn("Amanda", {item["name"] for item in payload["voices"]})
        self.assertNotIn("George", {item["name"] for item in payload["voices"]})

    def test_catalog_prioritizes_english_then_multilingual_and_filters(self):
        payload = catalog.normalize_catalog([
            {
                "commonId": 1,
                "speakerName": "普通话音色",
                "speakerGender": 2,
                "speakerLanguage": "普通话",
                "tag": "最热",
            },
            {
                "commonId": 2,
                "speakerName": "多语种音色",
                "speakerGender": 1,
                "speakerLanguage": "韩语",
                "tag": "多语种",
            },
            {
                "commonId": 3,
                "speakerName": "英语音色",
                "speakerGender": 2,
                "speakerLanguage": "英语",
            },
        ], source="live")

        self.assertEqual(
            [item["name"] for item in payload["voices"]],
            ["英语-Amanda", "英语-George", "英语音色", "多语种音色", "普通话音色"],
        )
        self.assertEqual(
            [item["label"] for item in payload["filters"][:5]],
            ["全部", "英语", "多语种", "女声", "男声"],
        )
        filter_labels = [item["label"] for item in payload["filters"]]
        self.assertEqual(len(filter_labels), len(set(filter_labels)))

    def test_language_sort_and_filter_share_name_and_alias_detection(self):
        payload = catalog.normalize_catalog([
            {"commonId": 4, "speakerName": "English Voice"},
            {"commonId": 5, "speakerName": "Multilingual Voice"},
            {"commonId": 6, "speakerName": "普通话音色"},
        ], source="live")

        self.assertEqual(
            [item["name"] for item in payload["voices"]],
            ["英语-Amanda", "英语-George", "English Voice", "Multilingual Voice", "普通话音色"],
        )
        english = next(item for item in payload["voices"] if item["name"] == "English Voice")
        multilingual = next(item for item in payload["voices"] if item["name"] == "Multilingual Voice")
        self.assertIn("英语", english["categories"])
        self.assertIn("多语种", multilingual["categories"])
        self.assertEqual(
            next(item["count"] for item in payload["filters"] if item["label"] == "英语"),
            3,
        )
        self.assertEqual(
            next(item["count"] for item in payload["filters"] if item["label"] == "多语种"),
            1,
        )

    def test_builtin_default_keys_stay_in_english_group_without_language_fields(self):
        payload = catalog.normalize_catalog([
            {"commonId": 7, "speakerName": "普通话音色"},
            {"commonId": 8, "speakerName": "Amanda"},
        ], source="live")

        self.assertEqual(
            [item["name"] for item in payload["voices"][:2]],
            ["Amanda", "英语-George"],
        )

    def test_common_list_catalog_uses_base_name_and_keeps_common_id(self):
        # App 必须显示 common/list 的基础名称（欣畅），而不是右侧 flat
        # 列表里的欣畅-Pro+/欣畅-Pro 变体；多人配音弹窗按基础名称搜索。
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
        names = {item["name"] for item in payload["voices"]}
        self.assertIn("欣畅", names)
        self.assertNotIn("欣畅-Pro+", names)
        self.assertNotIn("欣畅-Pro", names)
        base = next(item for item in payload["voices"] if item["name"] == "欣畅")
        self.assertEqual(base["common_id"], 10001135)
        self.assertEqual(base["speaker_no"], 591199169)
        self.assertEqual(
            payload["voice_aliases"]["speaker:591199169"],
            "common:10001135",
        )

    def test_composite_catalog_keeps_provider_variant_labels(self):
        # common/list 展示基础名；旧缓存转化时仍可以保留第一个变体的
        # emot_desc 作为兼容信息，但不能把变体名暴露为主列表。
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
        # 旧变体缓存会降级为一个 common/list 风格的基础音色卡片。
        payload = catalog.normalize_catalog(
            flat,
            composite_raw_voices=flat,
            source="test",
        )
        self.assertEqual(
            [item["name"] for item in payload["composite_voices"] if item["key"].startswith("common:")],
            ["欣畅"],
        )
        self.assertEqual(
            next(item for item in payload["voices"] if item["name"] == "欣畅")["emot_desc"],
            "Pro+",
        )

    def test_old_normalized_cache_is_migrated_to_common_key_with_alias(self):
        old_variant = {
            "key": "speaker:591199169",
            "speaker_no": 591199169,
            "common_id": 10001135,
            "name": "欣畅-Pro+",
            "composite_name": "欣畅",
            "gender": "female",
        }
        payload = catalog.normalize_catalog(
            [old_variant],
            composite_raw_voices=[old_variant],
            source="cache",
        )

        self.assertEqual(
            [voice["key"] for voice in payload["voices"] if voice["name"] == "欣畅"],
            ["common:10001135"],
        )
        self.assertEqual(
            payload["voice_aliases"]["speaker:591199169"],
            "common:10001135",
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
                if item["key"].startswith("common:")
            ],
            ["Voice A", "Voice B"],
        )

    def test_empty_composite_list_falls_back_to_flat_variants_without_common_metadata(self):
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
            ["英语-Amanda", "英语-George", "Voice A"],
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
