from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from platform_entry.adapter import textbook_page
from platform_entry.adapter.constants import restore_url
from platform_entry.adapter.textbook_page import TextbookCatalogResponseObserver
from workflow.textbook_catalog import TextbookCatalogService, TextbookCatalogStore, normalize_textbook_catalog


class TextbookCatalogTests(unittest.TestCase):
    def test_catalog_page_opener_uses_the_textbook_management_list(self) -> None:
        class Locator:
            def __init__(self, visible: bool) -> None:
                self.visible = visible

            def count(self) -> int:
                return 1 if self.visible else 0

            def nth(self, _index: int) -> "Locator":
                return self

            def is_visible(self) -> bool:
                return self.visible

        class Page:
            url = textbook_page.TEXTBOOK_MANAGEMENT_URL

            def __init__(self) -> None:
                self.goto_calls: list[tuple[str, dict[str, object]]] = []

            def goto(self, url: str, **kwargs: object) -> None:
                self.goto_calls.append((url, kwargs))

            def get_by_text(self, text: str, *, exact: bool) -> Locator:
                self.assert_exact = exact
                return Locator(text == "教材列表")

        page = Page()
        textbook_page._open_catalog_list(page, 7)

        self.assertEqual(len(page.goto_calls), 1)
        self.assertEqual(page.goto_calls[0][0], textbook_page.TEXTBOOK_MANAGEMENT_URL)
        self.assertEqual(page.goto_calls[0][1]["wait_until"], "domcontentloaded")

    def test_catalog_page_opener_waits_through_route_settling(self) -> None:
        class Locator:
            def __init__(self, visible: bool = False, body: str = "") -> None:
                self.visible = visible
                self.body = body

            def count(self) -> int:
                return 1 if self.visible else 0

            def nth(self, _index: int) -> "Locator":
                return self

            def is_visible(self) -> bool:
                return self.visible

            def inner_text(self, **_kwargs: object) -> str:
                return self.body

        class Page:
            # 登录中转页；域名与生产一致，来源与 constants.py 相同的混淆常量。
            url = restore_url("aHR0cHM6Ly9hZG1pbi5sZXh1ZWp1bi5jbg==") + "/#/login"

            def __init__(self) -> None:
                self.goto_calls: list[tuple[str, dict[str, object]]] = []
                self.ready = False

            def goto(self, url: str, **kwargs: object) -> None:
                self.goto_calls.append((url, kwargs))
                self.url = textbook_page.TEXTBOOK_MANAGEMENT_URL

            def get_by_text(self, text: str, *, exact: bool) -> Locator:
                return Locator(self.ready and text == "教材列表")

            def locator(self, selector: str) -> Locator:
                if selector == "body":
                    self.ready = True
                return Locator(body="")

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = Page()
        textbook_page._open_catalog_list(page, 7)

        self.assertEqual(len(page.goto_calls), 1)
        self.assertEqual(page.url, textbook_page.TEXTBOOK_MANAGEMENT_URL)

    def test_catalog_page_opener_does_not_accept_list_shell_after_auth_error(self) -> None:
        class Locator:
            def count(self) -> int:
                return 1

            def nth(self, _index: int) -> "Locator":
                return self

            def is_visible(self) -> bool:
                return True

            def inner_text(self, **_kwargs: object) -> str:
                return "教材列表"

        class Page:
            url = textbook_page.TEXTBOOK_MANAGEMENT_URL

            def __init__(self, observer: object) -> None:
                self.observer = observer
                self.wait_count = 0

            def goto(self, _url: str, **_kwargs: object) -> None:
                return None

            def get_by_text(self, _text: str, *, exact: bool) -> Locator:
                return Locator()

            def locator(self, _selector: str) -> Locator:
                return Locator()

            def wait_for_timeout(self, _milliseconds: int) -> None:
                self.wait_count += 1
                self.observer.auth_error = False

        observer = type("Observer", (), {"auth_error": True})()
        page = Page(observer)
        textbook_page._open_catalog_list(page, 7, observer=observer)

        self.assertEqual(page.wait_count, 1)

    def test_response_observer_captures_only_authenticated_textbook_pages(self) -> None:
        class Request:
            method = "GET"

        class Response:
            request = Request()
            url = "https://api.example.test/admin-api/system/textbook/page?pageNo=2&pageSize=10"
            status = 200

            @staticmethod
            def json() -> dict:
                return {
                    "code": 0,
                    "data": {
                        "list": [{"id": 2, "versionName": "人教版"}],
                        "total": 11,
                        "pageSize": 10,
                    },
                }

        observer = TextbookCatalogResponseObserver(page=None, api_base="https://api.example.test")
        observer._on_response(Response())
        self.assertEqual(observer.response_count, 1)
        self.assertEqual(observer.total, 11)
        self.assertEqual(observer.page_size, 10)
        self.assertEqual(observer.pages[2][0]["id"], 2)

        class AuthResponse(Response):
            status = 401

            @staticmethod
            def json() -> dict:
                return {"code": 401, "message": "账号未登录"}

        observer._on_response(AuthResponse())
        self.assertTrue(observer.auth_error)

    def test_initial_catalog_wait_recovers_when_first_response_reports_expired_login(self) -> None:
        page = object()
        observer = type("Observer", (), {"auth_error": True})()
        with (
            patch.object(
                textbook_page,
                "_wait_for_catalog_page",
                side_effect=[TimeoutError("教材平台登录状态已失效"), None],
            ) as wait_for_page,
            patch.object(textbook_page, "_recover_catalog_auth") as recover_auth,
        ):
            textbook_page._wait_for_authenticated_catalog_page(page, observer, 1, 17)

        self.assertEqual(wait_for_page.call_count, 2)
        recover_auth.assert_called_once_with(page, observer, 17)

    def test_normalize_platform_rows_into_cascading_choices(self) -> None:
        rows = [
            {
                "id": 101,
                "versionId": 11,
                "versionName": "外研版",
                "stageId": 2,
                "stageName": "初中",
                "gradeId": 7,
                "gradeName": "七年级",
                "volumeId": 1,
                "volumeName": "上册",
                "unitId": 1,
                "unitName": "Unit 1",
                "lessonId": 1,
                "lessonName": "Section A",
            },
            {
                "id": 102,
                "version": {"id": 11, "name": "外研版"},
                "stage": {"id": 2, "name": "初中"},
                "grade": {"id": 7, "name": "七年级"},
                "volume": {"id": 1, "name": "上册"},
                "unit": {"id": 2, "name": "Unit 2"},
                "lesson": {"id": 2, "name": "Section A"},
            },
        ]

        catalog = normalize_textbook_catalog(rows, total=2, page_count=1, synced_at="2026-09-05T00:00:00+00:00")

        self.assertEqual(catalog["record_count"], 2)
        self.assertEqual(catalog["source_total"], 2)
        self.assertEqual(catalog["options"]["version"], [{"id": "11", "name": "外研版"}])
        self.assertEqual(catalog["options"]["lesson"], [{"id": "1", "name": "Section A"}])
        self.assertEqual(catalog["records"][0]["grade"], {"id": "7", "name": "七年级"})

    def test_normalize_live_textbook_page_rows_keeps_version_and_nested_lessons(self) -> None:
        catalog = normalize_textbook_catalog(
            [
                {
                    "id": "book-1",
                    "textBookNameId": "version-1",
                    "name": "人教版",
                    "stageId": 2,
                    "stageName": "初中",
                    "gradeId": 7,
                    "gradeName": "七年级",
                    "volumeId": 1,
                    "volumeName": "上册",
                    "units": [
                        {
                            "id": "unit-1",
                            "unit": "Unit 1",
                            "classHours": [{"id": "lesson-1", "name": "Section A"}],
                        }
                    ],
                }
            ],
            total=1,
            page_count=1,
        )

        self.assertEqual(catalog["record_count"], 1)
        self.assertEqual(catalog["source_total"], 1)
        self.assertEqual(catalog["source_total_scope"], "source_books")
        self.assertEqual(catalog["records"][0]["version"], {"id": "version-1", "name": "人教版"})
        self.assertEqual(catalog["records"][0]["unit"], {"id": "unit-1", "name": "Unit 1"})
        self.assertEqual(catalog["records"][0]["lesson"], {"id": "lesson-1", "name": "Section A"})

    def test_normalize_never_reports_source_total_below_retained_records(self) -> None:
        catalog = normalize_textbook_catalog(
            [
                {"id": "book-1", "stageName": "初中"},
                {"id": "book-2", "stageName": "高中"},
            ],
            total=1,
            page_count=-1,
        )

        self.assertEqual(catalog["record_count"], 2)
        self.assertEqual(catalog["source_total"], 2)
        self.assertEqual(catalog["page_count"], 0)

    def test_nested_catalog_keeps_platform_book_total_when_store_round_trips_paths(self) -> None:
        raw_rows = [
            {
                "id": "book-1",
                "textBookNameId": "version-1",
                "name": "人教版",
                "stageName": "初中",
                "gradeName": "七年级",
                "volumeName": "上册",
                "units": [
                    {
                        "id": "unit-1",
                        "unit": "Unit 1",
                        "classHours": [
                            {"id": "lesson-a", "name": "Section A"},
                            {"id": "lesson-b", "name": "Section B"},
                        ],
                    }
                ],
            },
            {
                "id": "book-2",
                "textBookNameId": "version-2",
                "name": "外研版",
                "stageName": "初中",
                "gradeName": "七年级",
                "volumeName": "上册",
                "units": [{"id": "unit-1", "unit": "Unit 1", "classHours": [{"id": "lesson-a", "name": "Section A"}]}],
            },
        ]
        catalog = normalize_textbook_catalog(raw_rows, total=2, page_count=1)

        self.assertEqual(catalog["record_count"], 3)
        self.assertEqual(catalog["source_total"], 2)
        self.assertEqual(catalog["source_total_scope"], "source_books")

        with tempfile.TemporaryDirectory(prefix="wordtts-textbook-catalog-") as temp:
            store = TextbookCatalogStore(Path(temp) / "textbook-catalog.json")
            saved = store.save(catalog)
            loaded = store.load()
            self.assertEqual(saved["source_total"], 2)
            self.assertEqual(saved["source_total_scope"], "source_books")
            self.assertEqual(loaded["record_count"], 3)
            self.assertEqual(loaded["source_total"], 2)
            self.assertEqual(loaded["source_total_scope"], "source_books")

    def test_normalize_prefers_version_label_when_version_alias_is_numeric(self) -> None:
        catalog = normalize_textbook_catalog([{
            "version": 11,
            "versionId": 11,
            "name": "人教版",
        }])

        self.assertEqual(catalog["records"], [{
            "id": "",
            "version": {"id": "11", "name": "人教版"},
        }])

    def test_normalize_keeps_same_labels_when_platform_ids_differ(self) -> None:
        rows = [
            {
                "id": "book-1",
                "textBookNameId": "version-1",
                "name": "人教版",
                "stageId": 2,
                "stageName": "初中",
                "gradeId": 7,
                "gradeName": "七年级",
                "volumeId": 1,
                "volumeName": "上册",
                "units": [{"id": "unit-1", "unit": "Unit 1", "classHours": [{"id": "lesson-1", "name": "Section A"}]}],
            },
            {
                "id": "book-2",
                "textBookNameId": "version_1",
                "name": "人教版",
                "stageId": 2,
                "stageName": "初中",
                "gradeId": 7,
                "gradeName": "七年级",
                "volumeId": 1,
                "volumeName": "上册",
                "units": [{"id": "unit_1", "unit": "Unit 1", "classHours": [{"id": "lesson_1", "name": "Section A"}]}],
            },
        ]

        catalog = normalize_textbook_catalog(rows, total=2, page_count=1)

        self.assertEqual(catalog["record_count"], 2)
        self.assertEqual(
            {record["version"]["id"] for record in catalog["records"]},
            {"version-1", "version_1"},
        )

    def test_store_round_trips_atomically_and_service_reports_idle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-textbook-catalog-") as temp:
            path = Path(temp) / "nested" / "textbook-catalog.json"
            store = TextbookCatalogStore(path)
            saved = store.save(normalize_textbook_catalog([
                {"id": "text-1", "versionName": "人教版", "stageName": "初中"},
            ], total=1, page_count=1))

            loaded = store.load()
            self.assertEqual(loaded["record_count"], 1)
            self.assertEqual(loaded["records"][0]["version"]["name"], "人教版")
            self.assertEqual(path.exists(), True)

            service = TextbookCatalogService(store)
            self.assertEqual(service.get_sync_status()["status"], "SUCCEEDED")
            self.assertEqual(service.get_catalog()["record_count"], 1)

    def test_malformed_store_fails_closed_to_empty_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wordtts-textbook-catalog-") as temp:
            path = Path(temp) / "textbook-catalog.json"
            path.write_text("not json", encoding="utf-8")
            catalog = TextbookCatalogStore(path).load()
            self.assertEqual(catalog["record_count"], 0)
            self.assertEqual(catalog["records"], [])


if __name__ == "__main__":
    unittest.main()
