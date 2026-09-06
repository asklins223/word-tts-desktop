"""Read the two template-management lists through the visible platform UI."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlparse

from .constants import (
    API_BASE_URL,
    PAPER_TEMPLATE_PAGE_PATH,
    QUESTION_TEMPLATE_PAGE_PATH,
    QUESTION_TYPE_TEMPLATE_PAGE_PATH,
    TEMPLATE_MANAGEMENT_URL,
)


def _text(value: Any, limit: int = 256) -> str:
    return str(value or "").strip()[:limit]


def _safe_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _page_rows(body: Any) -> tuple[list[Mapping[str, Any]], int | None, int | None, bool]:
    data = body.get("data") if isinstance(body, Mapping) else None
    source = data if isinstance(data, Mapping) else body
    if isinstance(source, Mapping):
        total = _safe_int(source.get("total", source.get("totalCount", source.get("count"))))
        page_size = _safe_int(source.get("pageSize", source.get("page_size", source.get("size"))))
        for key in ("list", "records", "rows", "items", "data"):
            value = source.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return [row for row in value if isinstance(row, Mapping)], total, page_size, True
        return [], total, page_size, total is not None
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        return [row for row in source if isinstance(row, Mapping)], None, None, True
    return [], None, None, False


class PlatformTemplateCatalogResponseObserver:
    """Capture only read-only template page responses from the platform host."""

    def __init__(self, page: Any, *, api_base: str = API_BASE_URL) -> None:
        self.api_base = str(api_base or "").rstrip("/")
        self.active_kind = "question"
        self.pages: dict[str, dict[int, list[Mapping[str, Any]]]] = {"question": {}, "paper": {}}
        self.totals: dict[str, int] = {}
        self.page_sizes: dict[str, int] = {}
        self.response_counts: dict[str, int] = {"question": 0, "paper": 0}
        self.auth_error = False
        self.last_error = ""
        if hasattr(page, "on"):
            page.on("response", self._on_response)

    def set_active_kind(self, kind: str) -> None:
        if kind not in self.pages:
            raise ValueError(f"unsupported template kind: {kind}")
        self.active_kind = kind

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            if str(request.method or "").upper() not in {"GET", "HEAD"}:
                return
            parsed = urlparse(str(response.url))
            expected = urlparse(self.api_base)
            if (
                not expected.netloc
                or parsed.scheme.casefold() != expected.scheme.casefold()
                or parsed.netloc.casefold() != expected.netloc.casefold()
            ):
                return
            path_key = parsed.path.casefold()
            if parsed.path not in {
                PAPER_TEMPLATE_PAGE_PATH,
                QUESTION_TEMPLATE_PAGE_PATH,
                QUESTION_TYPE_TEMPLATE_PAGE_PATH,
            } and not (
                parsed.path.startswith("/admin-api/system/") and "template" in path_key
            ):
                return
            body = self._read_json(response)
            status = int(getattr(response, "status", 0) or 0)
            code = body.get("code") if isinstance(body, Mapping) else None
            message = _text(
                (body.get("msg") or body.get("message") or body.get("error"))
                if isinstance(body, Mapping) else ""
            )
            if status in {401, 403} or str(code) in {"401", "403"} or "登录" in message or "未登录" in message:
                self.auth_error = True
                self.last_error = message or "账号未登录"
                return
            rows, total, page_size, is_page = _page_rows(body)
            if not is_page:
                return
            kind = self.active_kind
            # Known endpoint names beat tab timing when the response clearly
            # identifies a question-type library.
            if parsed.path in {QUESTION_TEMPLATE_PAGE_PATH, QUESTION_TYPE_TEMPLATE_PAGE_PATH}:
                kind = "question"
            elif parsed.path == PAPER_TEMPLATE_PAGE_PATH:
                kind = "paper"
            query = parse_qs(parsed.query)
            page_no = _safe_int((query.get("pageNo") or query.get("page") or [None])[0])
            if page_no is None and isinstance(body, Mapping) and isinstance(body.get("data"), Mapping):
                page_no = _safe_int(body["data"].get("pageNo", body["data"].get("page")))
            if page_no is None:
                page_no = len(self.pages[kind]) + 1
            self.auth_error = False
            self.last_error = ""
            self.pages[kind][page_no] = rows
            self.response_counts[kind] += 1
            if total is not None:
                self.totals[kind] = total
            query_size = _safe_int((query.get("pageSize") or query.get("size") or [None])[0])
            if query_size is not None or page_size is not None:
                self.page_sizes[kind] = query_size or page_size or 10
        except Exception:
            return

    @staticmethod
    def _read_json(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            try:
                text = response.text()
                return json.loads(text) if text else None
            except Exception:
                return None


def _visible_exact(page: Any, text: str) -> Any | None:
    candidates = page.get_by_text(text, exact=True)
    try:
        count = candidates.count()
    except Exception:
        count = 0
    for index in range(count - 1, -1, -1):
        candidate = candidates.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def _body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=2_000) or "")
    except Exception:
        return ""


def _open_template_list(
    page: Any,
    observer: PlatformTemplateCatalogResponseObserver,
    login_timeout_seconds: int,
) -> None:
    observer.set_active_kind("question")
    page.goto(TEMPLATE_MANAGEMENT_URL, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + max(1, int(login_timeout_seconds))
    warned = False
    refreshed_ready_page = False
    while time.monotonic() < deadline:
        current_url = str(getattr(page, "url", ""))
        ready = _visible_exact(page, "题型模板管理") is not None and _visible_exact(page, "试卷模板管理") is not None
        if "#/resource/template" in current_url and ready and not observer.auth_error:
            # A persistent profile may restore this exact hash route. In that
            # case ``goto`` can become a hash-only navigation and the active
            # tab does not issue its list request again. Reload once so the
            # observer always sees a fresh first-page response.
            if not refreshed_ready_page and not observer.pages["question"]:
                refreshed_ready_page = True
                page.reload(wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(250)
                continue
            return
        body = _body_text(page)
        if "#/login" in current_url or any(token in body for token in ("欢迎登录", "账号登录", "密码登录", "登录状态已失效")):
            if not warned:
                print("[browser] 请在打开的 Chrome 窗口完成登录；模板同步会继续等待。", flush=True)
                warned = True
        elif "#/resource/template" not in current_url:
            try:
                page.goto(TEMPLATE_MANAGEMENT_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
        page.wait_for_timeout(1_000)
    raise RuntimeError("等待登录/模板管理页面超时；未读取平台模板目录。")


def _wait_for_kind_page(
    page: Any,
    observer: PlatformTemplateCatalogResponseObserver,
    kind: str,
    page_no: int = 1,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if observer.auth_error:
            raise TimeoutError("平台登录状态已失效")
        if page_no in observer.pages[kind]:
            return
        page.wait_for_timeout(250)
    raise TimeoutError("等待模板列表接口响应超时")


def _next_button(page: Any) -> Any | None:
    for selector in (
        "button.btn-next:visible",
        ".el-pagination button[aria-label*='下一页']:visible",
        ".el-pagination button[title*='下一页']:visible",
    ):
        try:
            node = page.locator(selector).first
            if node.count() and node.is_visible():
                return node
        except Exception:
            continue
    return None


def _disabled(button: Any) -> bool:
    try:
        return bool(button.is_disabled()) or "is-disabled" in str(button.get_attribute("class") or "")
    except Exception:
        return False


def _collect_visible_pages(page: Any, observer: PlatformTemplateCatalogResponseObserver, kind: str) -> None:
    _wait_for_kind_page(page, observer, kind)
    visited = {1}
    current = 1
    while len(visited) < 1000:
        total = observer.totals.get(kind)
        page_size = observer.page_sizes.get(kind, 10)
        if total is not None and (total == 0 or len(visited) >= (total + page_size - 1) // page_size):
            break
        button = _next_button(page)
        if button is None or _disabled(button):
            break
        before = observer.response_counts[kind]
        button.click()
        target = current + 1
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if observer.auth_error:
                raise TimeoutError("平台登录状态已失效")
            if target in observer.pages[kind] or observer.response_counts[kind] > before:
                break
            page.wait_for_timeout(250)
        if observer.response_counts[kind] <= before:
            raise TimeoutError("等待模板列表下一页响应超时")
        if target not in observer.pages[kind]:
            target = max(observer.pages[kind] or {current})
        if target in visited:
            break
        visited.add(target)
        current = target


def _dedupe_rows(pages: Mapping[int, Sequence[Mapping[str, Any]]]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for page_no in sorted(pages):
        for row in pages[page_no]:
            fingerprint = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            rows.append(row)
    return rows


def sync_platform_template_catalog_live(
    *,
    profile_dir: Any,
    api_base: str = API_BASE_URL,
    login_timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Observe and paginate both authenticated template-management tabs."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("平台模板同步需要 Playwright") from exc

    from .runtime import _launch_browser

    profile_dir = getattr(profile_dir, "expanduser", lambda: profile_dir)()
    profile_dir = getattr(profile_dir, "resolve", lambda: profile_dir)()
    with sync_playwright() as playwright:
        context = _launch_browser(playwright, profile_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            observer = PlatformTemplateCatalogResponseObserver(page, api_base=api_base)
            _open_template_list(page, observer, max(1, int(login_timeout_seconds)))

            question_tab = _visible_exact(page, "题型模板管理")
            if question_tab is None:
                raise RuntimeError("模板管理页面缺少题型模板列表")
            observer.set_active_kind("question")
            question_tab.click()
            _collect_visible_pages(page, observer, "question")

            paper_tab = _visible_exact(page, "试卷模板管理")
            if paper_tab is None:
                raise RuntimeError("模板管理页面缺少试卷模板列表")
            observer.set_active_kind("paper")
            paper_tab.click()
            _collect_visible_pages(page, observer, "paper")

            question_rows = _dedupe_rows(observer.pages["question"])
            paper_rows = _dedupe_rows(observer.pages["paper"])
            return {
                "status": "SUCCEEDED",
                "question_rows": question_rows,
                "paper_rows": paper_rows,
                "question_total": observer.totals.get("question", len(question_rows)),
                "paper_total": observer.totals.get("paper", len(paper_rows)),
                "page_count": len(observer.pages["question"]) + len(observer.pages["paper"]),
            }
        finally:
            context.close()


__all__ = ["PlatformTemplateCatalogResponseObserver", "sync_platform_template_catalog_live"]
