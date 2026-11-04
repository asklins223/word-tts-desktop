from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from platform_entry.adapter.catalog_pagination import collect_catalog_pages, validate_catalog_rows
from platform_entry.adapter.constants import (
    PAPER_TEMPLATE_PAGE_PATH,
    QUESTION_TYPE_TEMPLATE_PAGE_PATH,
    TEXTBOOK_PAGE_PATH,
)
from platform_entry.adapter.template_page import PlatformTemplateCatalogResponseObserver
from platform_entry.adapter.textbook_page import TextbookCatalogResponseObserver
from workflow.platform_template_catalog import (
    PlatformTemplateCatalogService,
    PlatformTemplateCatalogStore,
    normalize_platform_template_catalog,
)
from workflow.textbook_catalog import TextbookCatalogService, TextbookCatalogStore, normalize_textbook_catalog


class Response:
    request = type("Request", (), {"method": "GET"})()
    status = 200

    def __init__(self, path: str, rows: list[dict], total: int, *, page_no: int | None = None) -> None:
        query = f"?pageNo={page_no}&pageSize=2" if page_no is not None else ""
        self.url = f"https://api.example.test{path}{query}"
        self.rows = rows
        self.total = total

    def json(self) -> dict:
        return {"code": 0, "data": {"list": self.rows, "total": self.total, "pageSize": 2}}


class FakeArrow:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def is_visible(self) -> bool:
        return True

    def locator(self, selector: str) -> "FakeArrow":
        assert selector == ".."
        return self

    def is_disabled(self) -> bool:
        return False

    def get_attribute(self, name: str) -> str | None:
        return "no-change" if name == "class" and self.page.disabled else None

    def click(self) -> None:
        self.page.clicks += 1
        target = self.page.clicks + 1
        # A delayed refresh of the previous page must not satisfy the wait
        # for the new page, even if that new page was seen earlier.
        self.page.pending.extend((
            lambda: self.page.emit(target - 1),
            lambda: self.page.emit(target),
        ))


class FakeLocator:
    def __init__(self, arrow: FakeArrow | None) -> None:
        self.arrow = arrow

    def count(self) -> int:
        return int(self.arrow is not None)

    def nth(self, _index: int) -> FakeArrow:
        assert self.arrow is not None
        return self.arrow


class FakePage:
    def __init__(self, emit, *, has_arrow: bool = True, disabled: bool = False) -> None:
        self.emit = emit
        self.has_arrow = has_arrow
        self.disabled = disabled
        self.clicks = 0
        self.waits = 0
        self.pending: list = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(FakeArrow(self) if self.has_arrow and selector == ".arrow-right:visible" else None)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        self.waits += 1
        if self.pending:
            self.pending.pop(0)()


class CatalogPaginationTests(unittest.TestCase):
    def test_ignores_template_auxiliary_lists(self) -> None:
        observer = PlatformTemplateCatalogResponseObserver(None, api_base="https://api.example.test")
        for path in (
            "/admin-api/system/question-type-template/status-options",
            "/admin-api/system/question-type-template/existing-regions",
        ):
            observer._on_response(Response(path, [{"id": "auxiliary"}], 1))
        observer._on_response(Response(QUESTION_TYPE_TEMPLATE_PAGE_PATH, [{"id": "real"}], 1, page_no=1))

        self.assertEqual(observer.pages["question"], {1: [{"id": "real"}]})
        self.assertEqual(observer.response_counts["question"], 1)

    def test_both_catalogs_collect_fresh_responses_across_three_pages(self) -> None:
        rows_by_page = {
            1: [{"id": "1"}, {"id": "2"}],
            2: [{"id": "3"}, {"id": "4"}],
            3: [{"id": "5"}],
        }
        for kind, path in (
            ("textbook", TEXTBOOK_PAGE_PATH),
            ("question", QUESTION_TYPE_TEMPLATE_PAGE_PATH),
            ("paper", PAPER_TEMPLATE_PAGE_PATH),
        ):
            with self.subTest(kind=kind):
                if kind == "textbook":
                    observer = TextbookCatalogResponseObserver(None, api_base="https://api.example.test")
                    pages, versions = observer.pages, observer.page_versions
                    total = lambda: observer.total
                    set_expected = lambda number: setattr(observer, "expected_page", number)
                else:
                    observer = PlatformTemplateCatalogResponseObserver(None, api_base="https://api.example.test")
                    observer.set_active_kind(kind)
                    pages, versions = observer.pages[kind], observer.page_versions[kind]
                    total = lambda: observer.totals.get(kind)
                    set_expected = lambda number: observer.expected_pages.__setitem__(kind, number)

                def emit(number: int) -> None:
                    observer._on_response(Response(path, rows_by_page[number], 5, page_no=number))

                emit(1)
                observer._on_response(Response(path, [{"id": "stale"}], 5, page_no=2))
                page = FakePage(emit)
                visited = collect_catalog_pages(
                    page, pages, versions, total=total, set_expected_page=set_expected,
                    auth_error=lambda: observer.auth_error, label="测试列表", timeout=1,
                )

                self.assertEqual(visited, {1, 2, 3})
                self.assertEqual(page.clicks, 2)
                self.assertEqual(page.waits, 4)
                self.assertEqual([row["id"] for number in sorted(visited) for row in pages[number]],
                                 ["1", "2", "3", "4", "5"])

    def test_missing_or_disabled_pagination_rejects_partial_results(self) -> None:
        for has_arrow, disabled in ((False, False), (True, True)):
            with self.subTest(has_arrow=has_arrow, disabled=disabled):
                page = FakePage(lambda _number: None, has_arrow=has_arrow, disabled=disabled)
                with self.assertRaisesRegex(RuntimeError, "只读取到 1 / 3 条"):
                    collect_catalog_pages(
                        page, {1: [{"id": 1}]}, {1: 1}, total=lambda: 3,
                        set_expected_page=lambda _number: None, auth_error=lambda: False,
                        label="教材列表", timeout=1, control_timeout=0,
                    )

    def test_waits_for_pagination_control_to_render(self) -> None:
        pages = {1: [{"id": "1"}]}
        versions = {1: 1}

        def emit(number: int) -> None:
            if number == 2:
                pages[2] = [{"id": "2"}]
                versions[2] = versions.get(2, 0) + 1

        page = FakePage(emit, has_arrow=False)
        original_wait = page.wait_for_timeout

        def render_then_wait(milliseconds: int) -> None:
            page.has_arrow = True
            original_wait(milliseconds)

        page.wait_for_timeout = render_then_wait
        visited = collect_catalog_pages(
            page, pages, versions, total=lambda: 2, set_expected_page=lambda _number: None,
            auth_error=lambda: False, label="教材列表", timeout=1, control_timeout=1,
        )
        self.assertEqual(visited, {1, 2})
        self.assertEqual(page.clicks, 1)

    def test_rejects_unverifiable_or_repeated_data(self) -> None:
        page = FakePage(lambda _number: None, has_arrow=False)
        with self.assertRaisesRegex(RuntimeError, "无法确认数据是否完整"):
            collect_catalog_pages(
                page, {1: [{"id": 1}]}, {1: 1}, total=lambda: None,
                set_expected_page=lambda _number: None, auth_error=lambda: False,
                label="教材列表", control_timeout=0,
            )
        with self.assertRaisesRegex(RuntimeError, "去重后只有 1 / 2 条"):
            validate_catalog_rows([{"id": 1}], 2, "教材列表")

    def test_response_without_page_number_uses_requested_page(self) -> None:
        for kind, path in (("textbook", TEXTBOOK_PAGE_PATH), ("question", QUESTION_TYPE_TEMPLATE_PAGE_PATH)):
            with self.subTest(kind=kind):
                if kind == "textbook":
                    observer = TextbookCatalogResponseObserver(None, api_base="https://api.example.test")
                    pages = observer.pages
                    set_expected = lambda number: setattr(observer, "expected_page", number)
                else:
                    observer = PlatformTemplateCatalogResponseObserver(None, api_base="https://api.example.test")
                    pages = observer.pages[kind]
                    set_expected = lambda number: observer.expected_pages.__setitem__(kind, number)
                observer._on_response(Response(path, [{"id": "first"}], 2))
                observer._on_response(Response(path, [{"id": "first"}], 2))
                set_expected(2)
                observer._on_response(Response(path, [{"id": "second"}], 2))
                self.assertEqual(pages, {1: [{"id": "first"}], 2: [{"id": "second"}]})

    def test_partial_sync_keeps_previous_cache_for_both_catalogs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            textbook_store = TextbookCatalogStore(Path(directory) / "textbooks.json")
            textbook_store.save(normalize_textbook_catalog([{"id": "old", "versionName": "旧教材"}]))
            template_store = PlatformTemplateCatalogStore(Path(directory) / "templates.json")
            template_store.save(normalize_platform_template_catalog([{"id": "old", "name": "旧模板"}], []))
            services = (
                (TextbookCatalogService(textbook_store), "platform_entry.adapter.textbook_page.sync_textbook_catalog_live"),
                (PlatformTemplateCatalogService(template_store), "platform_entry.adapter.template_page.sync_platform_template_catalog_live"),
            )
            for service, target in services:
                with self.subTest(target=target):
                    original = service.store.path.read_bytes()
                    service._status = {"sync_id": "test", "status": "RUNNING"}
                    with patch(target, side_effect=RuntimeError("列表只读取到 1 / 3 条，无法继续翻页")):
                        service._run_sync("test")
                    self.assertEqual(service.get_sync_status()["status"], "FAILED")
                    self.assertIn("无法继续翻页", service.get_sync_status()["message"])
                    self.assertEqual(service.store.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
