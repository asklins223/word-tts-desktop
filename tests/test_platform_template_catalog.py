from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from platform_entry.adapter.template_page import PlatformTemplateCatalogResponseObserver
from workflow.platform_template_catalog import (
    PlatformTemplateCatalogStore,
    normalize_platform_template_catalog,
)
from workflow.system_input import platform_template_reference_matches_catalog


class PlatformTemplateCatalogTests(unittest.TestCase):
    def test_normalizes_question_and_paper_scopes_separately(self) -> None:
        catalog = normalize_platform_template_catalog(
            [{
                "id": "question-1",
                "templateName": "模仿朗读",
                "provinceId": "42",
                "provinceName": "湖北省",
                "cityId": "4201",
                "cityName": "武汉市",
                "status": "启用",
            }],
            [{
                "paperTemplateId": "paper-1",
                "paperTemplateName": "人教版 听说测试题模板",
                "province": {"id": "42", "name": "湖北省"},
                "city": {"id": "4201", "name": "武汉市"},
                "stageId": "2",
                "stageName": "初中",
                "gradeId": "7",
                "gradeName": "七年级",
                "questionTypes": [{"id": "q1", "name": "听后选择"}, "模仿朗读"],
            }],
            question_total=1,
            paper_total=1,
            page_count=2,
            synced_at="2026-09-06T00:00:00+00:00",
        )

        self.assertEqual(catalog["record_count"], 2)
        self.assertEqual(catalog["question_template_count"], 1)
        self.assertEqual(catalog["paper_template_count"], 1)
        question, paper = catalog["records"]
        self.assertEqual(question["template_kind"], "question")
        self.assertNotIn("stage", question)
        self.assertEqual(question["city"], {"id": "4201", "name": "武汉市"})
        self.assertEqual(paper["template_kind"], "paper")
        self.assertEqual(paper["grade"], {"id": "7", "name": "七年级"})
        self.assertEqual([item["name"] for item in paper["question_types"]], ["听后选择", "模仿朗读"])

    def test_store_round_trip_preserves_scoped_records(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-platform-template-catalog-") as temp:
            store = PlatformTemplateCatalogStore(Path(temp) / "catalog.json")
            saved = store.save(normalize_platform_template_catalog(
                [{"id": "q-1", "name": "信息获取", "provinceName": "广东省", "cityName": "佛山市"}],
                [],
            ))
            loaded = store.load()
        self.assertEqual(loaded["record_count"], 1)
        self.assertEqual(loaded["records"][0]["platform_template_key"], saved["records"][0]["platform_template_key"])
        self.assertEqual(loaded["records"][0]["province"]["name"], "广东省")

    def test_response_observer_separates_active_template_tabs(self) -> None:
        class Request:
            method = "GET"

        class Response:
            request = Request()
            status = 200

            def __init__(self, url: str, row: dict[str, object]) -> None:
                self.url = url
                self.row = row

            def json(self) -> dict[str, object]:
                return {"code": 0, "data": {"list": [self.row], "total": 1, "pageSize": 10}}

        observer = PlatformTemplateCatalogResponseObserver(None, api_base="https://api.example.test")
        observer.set_active_kind("question")
        observer._on_response(Response(
            "https://api.example.test/admin-api/system/question-type-template/page?pageNo=1",
            {
                "id": "q-1", "name": "模仿朗读", "status": 1,
                "provinceId": 420000, "provinceName": "湖北省",
                "cityId": 420100, "cityName": "武汉市",
            },
        ))
        observer.set_active_kind("paper")
        observer._on_response(Response(
            "https://api.example.test/admin-api/system/paper-template/page?pageNo=1",
            {"id": "p-1", "name": "听说测试模板"},
        ))

        self.assertEqual(observer.pages["question"][1][0]["id"], "q-1")
        self.assertEqual(observer.pages["paper"][1][0]["id"], "p-1")

    def test_runtime_gate_uses_category_specific_template_scope(self) -> None:
        catalog = normalize_platform_template_catalog(
            [{
                "id": "q-1", "name": "模仿朗读",
                "provinceId": "42", "provinceName": "湖北省",
                "cityId": "4201", "cityName": "武汉市",
            }],
            [{
                "id": "p-1", "name": "七年级模板",
                "provinceId": "42", "provinceName": "湖北省",
                "cityId": "4201", "cityName": "武汉市",
                "stageId": "2", "stageName": "初中",
                "gradeId": "7", "gradeName": "七年级",
            }],
        )
        common = {
            "provinceId": {"id": "42", "name": "湖北省"},
            "cityId": {"id": "4201", "name": "武汉市"},
            "stageId": {"id": "2", "name": "初中"},
        }
        with patch(
            "workflow.platform_template_catalog.PlatformTemplateCatalogStore.load",
            return_value=catalog,
        ):
            self.assertTrue(platform_template_reference_matches_catalog({
                **common,
                "paperCategory": "题型专项",
                "gradeId": {"id": "99", "name": "其他年级"},
                "platformTemplateId": "q-1",
                "platformTemplateName": "模仿朗读",
            }))
            self.assertFalse(platform_template_reference_matches_catalog({
                **common,
                "paperCategory": "听说考试",
                "gradeId": {"id": "8", "name": "八年级"},
                "platformTemplateId": "p-1",
                "platformTemplateName": "七年级模板",
            }))


if __name__ == "__main__":
    unittest.main()
