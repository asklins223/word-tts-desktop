"""课文跟读（新增课文）页面流程驱动。

模块职责与 ``platform_entry.paper_input`` 一致：只驱动可见页面控件，
保存响应只做被动记录，不主动发起任何请求。课文表单的页面操作助手
沿用 ``platform_entry.text_input`` 中经过实机验证的实现。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .constants import (
    API_BASE_URL,
    RESOURCE_TEXT_ROUTE,
    RESOURCE_TEXT_URL,
    TEXT_ROLE_ROUTE,
    TEXT_ROLE_URL,
    TEXT_ROLE_PAGE_PATH,
    TEXTBOOK_MANAGEMENT_URL,
    TEXTBOOK_PAGE_PATH,
)
from .page_shared import _fast_inner_text
from .performance import page_perf


def _text(value: Any, *, limit: int = 1024) -> str:
    return str(value or "").strip()[:limit]


_TEXTBOOK_ROLE_PREFIX_RE = re.compile(
    r"^\s*(?P<role>[^:：\n]{1,120})\s*[:：]\s*(?P<text>[\s\S]*)$"
)


def _textbook_page_text(value: Any, role: Any = None) -> str:
    """Return page text without the selected role's label."""

    text = _text(value, limit=1_000_000)
    wanted = _text(role, limit=256)
    if not text or not wanted:
        return text
    match = _TEXTBOOK_ROLE_PREFIX_RE.match(text)
    if match and match.group("role").strip().casefold() == wanted.casefold():
        return match.group("text").strip()
    return text


_PAGE_ACTION_TIMEOUT_MS = 15_000


def _configure_page_timeouts(page: Any) -> None:
    """Bound locator/keyboard waits used by the textbook page helpers."""

    set_default_timeout = getattr(page, "set_default_timeout", None)
    if callable(set_default_timeout):
        try:
            set_default_timeout(_PAGE_ACTION_TIMEOUT_MS)
        except Exception:
            pass
    set_default_navigation_timeout = getattr(
        page,
        "set_default_navigation_timeout",
        None,
    )
    if callable(set_default_navigation_timeout):
        try:
            set_default_navigation_timeout(30_000)
        except Exception:
            pass


def _safe_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _page_rows(body: Any) -> tuple[list[Mapping[str, Any]], int | None, int | None]:
    """Read common Element/Admin page envelopes without calling the API."""

    data = body.get("data") if isinstance(body, Mapping) else None
    source = data if isinstance(data, Mapping) else body
    if isinstance(source, Mapping):
        total = _safe_int(source.get("total", source.get("totalCount", source.get("count"))))
        page_size = _safe_int(source.get("pageSize", source.get("page_size", source.get("size"))))
        for key in ("list", "records", "rows", "items", "data"):
            value = source.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return [row for row in value if isinstance(row, Mapping)], total, page_size
        return [], total, page_size
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        return [row for row in source if isinstance(row, Mapping)], None, None
    return [], None, None


class TextbookCatalogResponseObserver:
    """Capture only the visible textbook list GET responses."""

    def __init__(self, page: Any, *, api_base: str = API_BASE_URL) -> None:
        self.api_base = str(api_base or "").rstrip("/")
        self.pages: dict[int, list[Mapping[str, Any]]] = {}
        self.page_order: list[list[Mapping[str, Any]]] = []
        self.page_totals: dict[int, int] = {}
        self.page_sizes: dict[int, int] = {}
        self.response_count = 0
        self.auth_error = False
        self.last_error = ""
        if hasattr(page, "on"):
            page.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            method = str(request.method or "").upper()
            parsed = urlparse(str(response.url))
            expected = urlparse(self.api_base)
            if method not in {"GET", "HEAD"} or parsed.path != TEXTBOOK_PAGE_PATH:
                return
            if (
                not expected.netloc
                or parsed.scheme.casefold() != expected.scheme.casefold()
                or parsed.netloc.casefold() != expected.netloc.casefold()
            ):
                return
            self.response_count += 1
            status = int(getattr(response, "status", 0) or 0)
            body = self._read_json(response)
            code = body.get("code") if isinstance(body, Mapping) else None
            message_value = ""
            if isinstance(body, Mapping):
                message_value = body.get("msg") or body.get("message") or body.get("error") or ""
            message = _text(message_value, limit=256)
            if status in {401, 403} or str(code) in {"401", "403"} or "登录" in message or "未登录" in message:
                self.auth_error = True
                self.last_error = message or "账号未登录"
                return
            rows, total, page_size = _page_rows(body)
            # A stale first response can be followed by a successful request
            # after the user signs in inside the visible browser.
            self.auth_error = False
            self.last_error = ""
            query = parse_qs(parsed.query)
            page_no = _safe_int((query.get("pageNo") or query.get("page") or [None])[0])
            query_page_size = _safe_int((query.get("pageSize") or query.get("size") or [None])[0])
            if page_no is None and isinstance(body, Mapping):
                data = body.get("data")
                page_no = _safe_int(data.get("pageNo") if isinstance(data, Mapping) else None)
            if page_no is None:
                page_no = len(self.page_order) + 1
            self.pages[page_no] = rows
            self.page_order.append(rows)
            if total is not None:
                self.page_totals[page_no] = total
            if query_page_size is not None:
                self.page_sizes[page_no] = query_page_size
            elif page_size is not None:
                self.page_sizes[page_no] = page_size
        except Exception:
            # A malformed response must never break the visible page flow.
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

    @property
    def total(self) -> int | None:
        return next(iter(self.page_totals.values()), None)

    @property
    def page_size(self) -> int | None:
        return next(iter(self.page_sizes.values()), None)


def _open_catalog_list(
    page: Any,
    login_timeout_seconds: int,
    *,
    observer: TextbookCatalogResponseObserver | None = None,
) -> None:
    """Open the authenticated textbook-management list used by catalog sync."""

    _configure_page_timeouts(page)
    page.goto(TEXTBOOK_MANAGEMENT_URL, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + max(1, int(login_timeout_seconds))
    warned = False
    while time.monotonic() < deadline:
        current_url = str(getattr(page, "url", ""))
        on_textbook_route = "#/textbook" in current_url
        list_ready = _visible_exact(page, "教材列表") is not None or _visible_exact(page, "添加教材") is not None
        if on_textbook_route and list_ready and not (observer and observer.auth_error):
            return
        body = _body_text(page)
        login_page = "#/login" in current_url or any(
            token in body for token in ("登录状态已失效", "欢迎登录", "扫码登录", "账号登录", "密码登录")
        )
        if login_page:
            if not warned:
                print(
                    "[browser] 登录状态已失效，请在打开的 Chrome 窗口完成登录；脚本会继续等待。",
                    flush=True,
                )
                warned = True
        else:
            try:
                if "#/textbook" not in current_url:
                    page.goto(TEXTBOOK_MANAGEMENT_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
        page.wait_for_timeout(200)
    raise RuntimeError("等待登录/教材管理列表页面超时；未读取教材目录。")


def _wait_for_catalog_page(page: Any, observer: TextbookCatalogResponseObserver, page_no: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        if observer.auth_error:
            raise TimeoutError("教材平台登录状态已失效")
        if page_no in observer.pages:
            return
        page.wait_for_timeout(250)
    raise TimeoutError("等待教材列表接口响应超时")


def _recover_catalog_auth(page: Any, observer: TextbookCatalogResponseObserver, login_timeout_seconds: int) -> None:
    """Re-open the visible route so an expired session can be renewed."""

    if not observer.auth_error:
        return
    try:
        _open_catalog_list(page, max(1, int(login_timeout_seconds)), observer=observer)
    except RuntimeError as exc:
        raise TimeoutError(str(exc)) from exc


def _wait_for_authenticated_catalog_page(
    page: Any,
    observer: TextbookCatalogResponseObserver,
    page_no: int,
    login_timeout_seconds: int,
) -> None:
    """Wait for the first page, recovering if its first response discovers 401."""

    try:
        _wait_for_catalog_page(page, observer, page_no)
    except TimeoutError:
        # The page shell may become visible before the first API response is
        # delivered.  Do not turn that normal browser race into a false sync
        # failure when the response is the one that reports an expired login.
        if not observer.auth_error:
            raise
        _recover_catalog_auth(page, observer, login_timeout_seconds)
        _wait_for_catalog_page(page, observer, page_no)


def _visible_next_button(page: Any) -> Any | None:
    selectors = (
        "button.btn-next:visible",
        ".el-pagination button[aria-label*='下一页']:visible",
        ".el-pagination button[title*='下一页']:visible",
    )
    for selector in selectors:
        try:
            node = page.locator(selector).first
            if node.count() and node.is_visible():
                return node
        except Exception:
            continue
    return None


def _pagination_button_disabled(button: Any) -> bool:
    try:
        return bool(button.is_disabled()) or str(button.get_attribute("class") or "").find("is-disabled") >= 0
    except Exception:
        return False


def sync_textbook_catalog_live(
    *,
    profile_dir: Any,
    api_base: str = API_BASE_URL,
    login_timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Sync every textbook list page through the visible platform UI.

    The browser page owns authentication and issues the GET requests.  This
    function only observes those read-only responses and clicks the rendered
    pagination control, so the desktop never needs a platform token.
    """

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("教材目录同步需要 Playwright") from exc

    from .runtime import _launch_browser

    profile_dir = getattr(profile_dir, "expanduser", lambda: profile_dir)()
    profile_dir = getattr(profile_dir, "resolve", lambda: profile_dir)()
    with sync_playwright() as playwright:
        context = _launch_browser(playwright, profile_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            observer = TextbookCatalogResponseObserver(page, api_base=api_base)
            _open_catalog_list(page, max(1, int(login_timeout_seconds)), observer=observer)
            # _open_catalog_list waits for the authenticated textbook list.
            # A pre-login 401 is kept as a fence until a successful page
            # response arrives after the user signs in.
            _wait_for_authenticated_catalog_page(page, observer, 1, login_timeout_seconds)

            current_page = 1
            visited_pages = {1}
            max_pages = 1000
            while len(visited_pages) < max_pages:
                if observer.auth_error:
                    _recover_catalog_auth(page, observer, login_timeout_seconds)
                    current_page = 1
                    visited_pages = {1}
                    _wait_for_catalog_page(page, observer, 1)
                    continue
                total = observer.total
                page_size = observer.page_size or 10
                if total is not None and total > 0 and len(visited_pages) >= (total + page_size - 1) // page_size:
                    break
                next_button = _visible_next_button(page)
                if next_button is None or _pagination_button_disabled(next_button):
                    break
                before_response_count = observer.response_count
                next_button.click()
                target_page = current_page + 1
                deadline = time.monotonic() + 30.0
                reauthenticated = False
                while time.monotonic() < deadline:
                    if observer.auth_error:
                        _recover_catalog_auth(page, observer, login_timeout_seconds)
                        current_page = 1
                        visited_pages = {1}
                        _wait_for_catalog_page(page, observer, 1)
                        reauthenticated = True
                        break
                    if target_page in observer.pages or observer.response_count > before_response_count:
                        break
                    page.wait_for_timeout(250)
                if reauthenticated:
                    continue
                if target_page not in observer.pages:
                    # Some deployment versions omit pageNo from the request;
                    # the response-order fallback still proves progress.
                    if observer.response_count <= before_response_count:
                        raise TimeoutError("等待教材列表下一页响应超时")
                    target_page = max(observer.pages or {current_page})
                if target_page in visited_pages and observer.response_count <= before_response_count:
                    break
                visited_pages.add(target_page)
                current_page = target_page

            rows: list[Mapping[str, Any]] = []
            seen: set[str] = set()
            for page_no in sorted(observer.pages):
                for row in observer.pages[page_no]:
                    fingerprint = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    rows.append(row)
            return {
                "status": "SUCCEEDED",
                "rows": rows,
                "total": observer.total if observer.total is not None else len(rows),
                "page_count": len(observer.pages),
            }
        finally:
            context.close()


# 平台记录 ID 是雪花风格的纯数字串；用于从保存响应中筛出记录 ID，
# 避免把上传响应里的 URL 或其他数字误认成课文 ID。
_RECORD_ID_RE = re.compile(r"^\d{12,32}$")


class TextbookRecordObserver:
    """监听页面响应，捕获“保存课文”之后平台返回的课文记录 ID。

    只读取响应体，不主动发起任何请求；与试卷侧的
    ``ReadOnlyFeedbackObserver`` 一样按平台域名过滤。
    """

    def __init__(self, page: Any, *, api_base: str) -> None:
        self.page = page
        self._api_base = str(api_base or "").rstrip("/")
        self._active = False
        self._captured: list[str] = []
        if hasattr(page, "on"):
            page.on("response", self._on_response)

    def begin(self) -> None:
        self._captured.clear()
        self._active = True

    def end(self) -> None:
        self._active = False

    def close(self) -> None:
        """Detach the response listener before a shared page is reused."""

        self._active = False
        if hasattr(self.page, "remove_listener"):
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass

    def take_record_id(self) -> str:
        return self._captured[0] if self._captured else ""

    def _on_response(self, response: Any) -> None:
        if not self._active:
            return
        try:
            if self._api_base and self._api_base not in str(response.url):
                return
            request = response.request
            if request.method not in {"POST", "PUT"}:
                return
            body = response.json()
        except Exception:
            return
        self._collect_ids(body)

    def _collect_ids(self, body: Any) -> None:
        if not isinstance(body, Mapping):
            return
        if str(body.get("code") or "") not in {"200", 200}:
            return
        data = body.get("data")
        candidates: list[Any] = []
        if isinstance(data, Mapping):
            for key in ("textbookId", "id", "textId"):
                if data.get(key) is not None:
                    candidates.append(data.get(key))
        elif isinstance(data, (str, int)) and not isinstance(data, bool):
            candidates.append(data)
        for candidate in candidates:
            value = _text(candidate, limit=64)
            if _RECORD_ID_RE.fullmatch(value) and value not in self._captured:
                self._captured.append(value)
                return


# ---------------------------------------------------------------------------
# 课文录入页面驱动
#
# 以下助手来自 ``platform_entry/text_input`` 中经过实机验证的实现；
# 独立课文录入 CLI 与系统录入课文适配器共用同一套页面操作。
# ---------------------------------------------------------------------------


_LAST_VISIBLE_INDEX_JS = """
(nodes) => {
    for (let index = nodes.length - 1; index >= 0; index -= 1) {
        const node = nodes[index];
        if (!node || typeof node.getBoundingClientRect !== 'function') continue;
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        if (rect.width > 0 && rect.height > 0) return index;
    }
    return -1;
}
"""

_DROPDOWN_TEXTS_JS = "(nodes) => nodes.map((node) => String(node.innerText || ''))"


def _visible_exact(page: Any, text: str) -> Any | None:
    candidates = page.get_by_text(text, exact=True)
    batch_probe = getattr(candidates, "evaluate_all", None)
    if callable(batch_probe):
        try:
            found = int(batch_probe(_LAST_VISIBLE_INDEX_JS))
        except Exception:
            found = -1
        if found >= 0:
            try:
                candidate = candidates.nth(found)
                if candidate.is_visible():
                    return candidate
            except Exception:
                pass
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


def _click_exact(page: Any, text: str, *, timeout: int = 15_000) -> None:
    candidate = _visible_exact(page, text)
    if candidate is None:
        raise RuntimeError(f"页面上没有找到可点击文本：{text}")
    candidate.scroll_into_view_if_needed(timeout=timeout)
    candidate.click(timeout=timeout)


def _wait_for_cards(
    page: Any,
    count: int,
    *,
    owner: Any | None = None,
    timeout: int = 30_000,
) -> Any:
    scope = owner or page
    cards = scope.locator(".expandContent:visible")
    deadline = time.monotonic() + timeout / 1000
    # Let Playwright wait inside its driver when available. This avoids a
    # Python -> Node -> Chromium round-trip for every 200ms poll on Windows.
    try:
        target = cards.nth(max(0, int(count) - 1))
        waiter = getattr(target, "wait_for", None)
        if callable(waiter):
            waiter(state="visible", timeout=timeout)
            if cards.count() >= count:
                return cards
    except Exception:
        pass

    while time.monotonic() < deadline:
        try:
            if cards.count() >= count:
                return cards
        except Exception:
            pass
        remaining_ms = int(max(1, (deadline - time.monotonic()) * 1000))
        page.wait_for_timeout(min(100, remaining_ms))
    raise RuntimeError(f"等待课文句子卡片超时：需要 {count} 个")


def _ensure_card_count(page: Any, count: int, *, owner: Any | None = None) -> Any:
    scope = owner or page
    cards = scope.locator(".expandContent:visible")
    current = cards.count()
    if current == 0:
        # The second page fetches the initial empty card asynchronously. Give
        # that card a chance to appear before clicking the add control; an
        # early click can otherwise create a duplicate first sentence.
        try:
            cards = _wait_for_cards(page, 1, owner=scope, timeout=5_000)
            current = cards.count()
        except Exception:
            button = _visible_exact(scope, "继续添加下一句") or _visible_exact(scope, "继续添加句子")
            if button is None:
                raise RuntimeError(f"等待课文句子卡片超时：需要 {count} 个")
            button.scroll_into_view_if_needed()
            button.click()
            cards = _wait_for_cards(page, 1, owner=scope)
            current = cards.count()
    while current < count:
        button = _visible_exact(scope, "继续添加下一句") or _visible_exact(scope, "继续添加句子")
        if button is None:
            raise RuntimeError(f"无法继续添加句子：当前 {current} / {count}")
        button.scroll_into_view_if_needed()
        button.click()
        cards = _wait_for_cards(page, current + 1, owner=scope)
        current = cards.count()
    return cards


def _replace_editor(page: Any, editor: Any, value: str, field_name: str) -> None:
    expected = re.sub(r"\s+", " ", str(value or "")).strip()

    def readback_matches(actual: str) -> bool:
        return expected in actual if expected else not actual

    try:
        # click() performs the same actionability scroll and saves one driver
        # round-trip for every long text field.
        editor.click()
        editor.press("ControlOrMeta+A")
        editor.press("Backspace")
        if value:
            keyboard = getattr(page, "keyboard", None)
            insert_text = (
                getattr(keyboard, "insert_text", None)
                if keyboard is not None
                else None
            )
            if callable(insert_text):
                insert_text(str(value))
            else:
                editor.type(value)
        editor.press("Tab")
        actual = re.sub(
            r"\s+",
            " ",
            str(editor.inner_text(timeout=2_000) or ""),
        ).strip()
        if not readback_matches(actual):
            # A few historical editor builds only committed text after the
            # full keyboard event sequence.  Keep that slower behavior as a
            # bounded compatibility fallback instead of charging every long
            # paragraph one event per character.
            editor.click()
            editor.press("ControlOrMeta+A")
            editor.press("Backspace")
            if value:
                editor.type(value)
            editor.press("Tab")
            actual = re.sub(
                r"\s+", " ", str(editor.inner_text(timeout=2_000) or "")
            ).strip()
    except Exception as exc:
        raise RuntimeError(f"填写{field_name}失败：{exc}") from exc
    if expected and not readback_matches(actual):
        raise RuntimeError(f"{field_name}回读不一致：期望 {expected!r}，实际 {actual!r}")
    if not expected and actual:
        raise RuntimeError(f"{field_name}清空后仍有内容：{actual!r}")


def _find_dropdown_option(page: Any, value: str) -> Any | None:
    role_option = page.get_by_role("option", name=value, exact=True)
    batch_probe = getattr(role_option, "evaluate_all", None)
    if callable(batch_probe):
        try:
            found = int(batch_probe(_LAST_VISIBLE_INDEX_JS))
        except Exception:
            found = -1
        if found >= 0:
            try:
                candidate = role_option.nth(found)
                if candidate.is_visible():
                    return candidate
            except Exception:
                pass
    try:
        for option_index in range(role_option.count() - 1, -1, -1):
            candidate = role_option.nth(option_index)
            if candidate.is_visible():
                return candidate
    except Exception:
        pass
    exact_li = page.locator("li:visible").filter(
        has_text=re.compile(rf"^\s*{re.escape(value)}\s*$")
    )
    if exact_li.count():
        return exact_li.last
    # 平台选项大小写可能与文档写法不一致（如文档 “Reading Plus”
    # vs 平台选项 “Reading plus”）；精确匹配失败后按忽略大小写兜底。
    wanted = re.sub(r"\s+", " ", value).strip().casefold()
    if wanted:
        all_li = page.locator("li:visible")
        batch_texts = getattr(all_li, "evaluate_all", None)
        if callable(batch_texts):
            try:
                texts = batch_texts(_DROPDOWN_TEXTS_JS)
            except Exception:
                texts = None
            if isinstance(texts, list):
                for option_index in range(len(texts) - 1, -1, -1):
                    option_text = re.sub(
                        r"\s+", " ", str(texts[option_index] or "")
                    ).strip()
                    if option_text.casefold() != wanted:
                        continue
                    candidate = all_li.nth(option_index)
                    try:
                        confirmed = re.sub(
                            r"\s+",
                            " ",
                            str(candidate.inner_text(timeout=300) or ""),
                        ).strip()
                    except Exception:
                        continue
                    if confirmed.casefold() == wanted:
                        return candidate
        try:
            option_count = all_li.count()
        except Exception:
            option_count = 0
        for option_index in range(option_count - 1, -1, -1):
            candidate = all_li.nth(option_index)
            try:
                option_text = re.sub(
                    r"\s+", " ", str(candidate.inner_text(timeout=300) or "")
                ).strip()
            except Exception:
                continue
            if option_text.casefold() == wanted:
                return candidate
    return _visible_exact(page, value)


def _select_option(page: Any, index: int, value: str) -> None:
    tracer = page_perf(page, operation="textbook-input")
    with tracer.span("select_option", index=int(index)):
        return _select_option_impl(page, index, value)


def _select_option_impl(page: Any, index: int, value: str) -> None:
    selectors = page.locator(".el-select__wrapper:visible")
    selector_count = selectors.count()
    if selector_count <= index:
        selector_deadline = time.monotonic() + 15
        # nth().wait_for() is implemented by Playwright's driver and avoids
        # repeatedly asking the Python process for the same count.
        try:
            target = selectors.nth(index)
            waiter = getattr(target, "wait_for", None)
            if callable(waiter):
                remaining_ms = int(max(1, (selector_deadline - time.monotonic()) * 1000))
                waiter(state="visible", timeout=remaining_ms)
                selector_count = selectors.count()
        except Exception:
            selector_count = selectors.count()
        while time.monotonic() < selector_deadline and selector_count <= index:
            remaining_ms = int(max(1, (selector_deadline - time.monotonic()) * 1000))
            page.wait_for_timeout(min(100, remaining_ms))
            selector_count = selectors.count()
    if selector_count <= index:
        raise RuntimeError(f"创建页面下拉框数量不足，无法选择第 {index + 1} 项：{value}")

    selector = selectors.nth(index)
    # 弹层渲染有延迟；只有确认下拉已经关闭时才重新点击。Element Plus
    # 的 wrapper.click() 是 toggle，过早重试会把已经打开但尚未加载选项的
    # 弹层关闭，且旧实现只再试一次后就会一直等到超时。
    option = None
    open_deadline = time.monotonic() + 12
    next_retry = time.monotonic() + 1.5
    selector.click()

    def dropdown_open() -> bool | None:
        try:
            expanded = selector.get_attribute("aria-expanded")
            if expanded is not None:
                return str(expanded).strip().casefold() == "true"
        except Exception:
            pass
        try:
            return page.locator(".el-select-dropdown:visible").count() > 0
        except Exception:
            return None

    while time.monotonic() < open_deadline:
        option = _find_dropdown_option(page, value)
        if option is not None:
            break
        if time.monotonic() >= next_retry:
            if dropdown_open() is not True:
                selector.click()
            next_retry = time.monotonic() + 1.5
        remaining_ms = int(max(1, (open_deadline - time.monotonic()) * 1000))
        page.wait_for_timeout(min(100, remaining_ms))
    if option is None:
        visible_texts = page.locator(".el-select-dropdown__item:visible").all_inner_texts()
        raise RuntimeError(
            f"下拉框中没有找到选项：{value}；当前可见选项={visible_texts[:20]}"
        )
    option.click()
    expected_normalized = re.sub(r"\s+", "", value).casefold()
    shown = ""
    selected_deadline = time.monotonic() + 3
    while time.monotonic() < selected_deadline:
        try:
            shown = re.sub(
                r"\s+",
                "",
                _fast_inner_text(selector, timeout_ms=300),
            )
        except Exception:
            shown = ""
        if expected_normalized in shown.casefold():
            return
        page.wait_for_timeout(50)
    if expected_normalized not in shown.casefold():
        raise RuntimeError(f"下拉框回读不一致：期望 {value!r}，实际 {shown!r}")


def _visible_role_rows(page: Any) -> Any:
    rows = page.locator(".el-table__body-wrapper .el-table__row:visible")
    if rows.count() == 0:
        rows = page.locator(".el-table__row:visible")
    return rows


def _role_key(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value, limit=256)).strip().casefold()


def _role_volume_matches(row: Any, record: Mapping[str, Any]) -> bool:
    row_text = _role_key(_fast_inner_text(row, timeout_ms=500)).replace(" ", "")
    required = (
        _text(record.get("version"), limit=128),
        _text(record.get("stage"), limit=128),
        _text(record.get("grade"), limit=128),
        _text(record.get("volume"), limit=128),
    )
    return bool(row_text) and all(
        _role_key(value).replace(" ", "") in row_text
        for value in required
        if value
    )


def _find_role_volume_row(page: Any, record: Mapping[str, Any]) -> Any | None:
    """Find a volume row, walking visible role-list pagination if needed."""

    visited: set[str] = set()
    for _ in range(100):
        rows = _visible_role_rows(page)
        for index in range(rows.count()):
            row = rows.nth(index)
            if _role_volume_matches(row, record):
                return row
        signature = "|".join(
            _role_key(_fast_inner_text(rows.nth(index), timeout_ms=300))
            for index in range(rows.count())
        )
        if signature in visited:
            return None
        visited.add(signature)
        next_button = _visible_next_button(page)
        if next_button is None or _pagination_button_disabled(next_button):
            return None
        next_button.click()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current_rows = _visible_role_rows(page)
            current_signature = "|".join(
                _role_key(_fast_inner_text(current_rows.nth(index), timeout_ms=300))
                for index in range(current_rows.count())
            )
            if current_signature != signature:
                break
            page.wait_for_timeout(100)
    return None


def _role_form_inputs(page: Any) -> Any:
    return page.locator("input[placeholder='如 Li Lei']:visible")


def _role_list_row_count(page: Any) -> int:
    try:
        return int(_visible_role_rows(page).count())
    except Exception:
        return 0


def _role_list_rows_ready(page: Any) -> bool:
    return _role_list_row_count(page) > 0


def _role_list_ready(page: Any) -> bool:
    # The same hash route opens either the list or the add/edit form. The
    # list has the filter's 查询 action; the form has the same 添加角色
    # heading but also has the role-name inputs.
    return (
        _visible_exact(page, "添加角色") is not None
        and _visible_exact(page, "查询") is not None
        and _role_form_inputs(page).count() == 0
    )


def _role_list_data_ready(page: Any) -> bool:
    """Return true only after the role table has rendered data or empty state."""

    try:
        if _role_list_rows_ready(page):
            return True
        for selector in (".el-table__empty-text:visible", ".el-table__empty-block:visible"):
            if page.locator(selector).count() > 0:
                return True
        body = _body_text(page)
        return "暂无数据" in body and (
            "共 0 条记录" in body or "显示 0 到 0 条" in body
        )
    except Exception:
        return False


class TextRoleListResponseObserver:
    """等待课文角色列表的真实 GET 响应，再允许做册别匹配。"""

    def __init__(self, page: Any, *, api_base: str = API_BASE_URL) -> None:
        self.page = page
        self.api_base = str(api_base or "").rstrip("/")
        self.response_count = 0
        self.successful_response = False
        self.auth_error = False
        self._listener = self._on_response
        if hasattr(page, "on"):
            page.on("response", self._listener)

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            if str(request.method or "").upper() not in {"GET", "HEAD"}:
                return
            parsed = urlparse(str(response.url))
            expected = urlparse(self.api_base)
            if parsed.path != TEXT_ROLE_PAGE_PATH:
                return
            if (
                not expected.netloc
                or parsed.scheme.casefold() != expected.scheme.casefold()
                or parsed.netloc.casefold() != expected.netloc.casefold()
            ):
                return
            self.response_count += 1
            status = int(getattr(response, "status", 0) or 0)
            self.auth_error = status in {401, 403}
            # 304 is a valid cached list response. The table may already be
            # rendered even when the browser does not expose a fresh 2xx.
            self.successful_response = 200 <= status < 300 or status == 304
        except Exception:
            # A malformed/partial response must not make the script treat the
            # still-loading table as an empty list.
            return

    def close(self) -> None:
        if hasattr(self.page, "remove_listener"):
            try:
                self.page.remove_listener("response", self._listener)
            except Exception:
                pass


def _role_input_value(locator: Any) -> str:
    try:
        value = locator.input_value()
    except Exception:
        try:
            value = locator.get_attribute("value")
        except Exception:
            value = ""
    return _text(value, limit=256)


def _wait_for_role_form(page: Any, timeout: int = 30_000) -> None:
    # 新增页面初始没有角色输入框，只有点击“添加新角色”后才会出现；
    # 用表单自己的保存按钮作为就绪标志，不能把输入框当作必有元素。
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        save = _visible_exact(page, "保存角色")
        if save is not None or _role_form_inputs(page).count():
            return
        page.wait_for_timeout(100)
    raise RuntimeError("等待课文角色编辑表单超时")


def _open_role_list(page: Any, login_timeout_seconds: int) -> None:
    """Open the role-management page and wait for its first list response."""

    _configure_page_timeouts(page)
    observer = TextRoleListResponseObserver(page)
    try:
        page.goto(TEXT_ROLE_URL, wait_until="domcontentloaded", timeout=60_000)
        deadline = time.monotonic() + max(1, int(login_timeout_seconds))
        warned = False
        while time.monotonic() < deadline:
            current_url = str(getattr(page, "url", ""))
            rows_rendered = _role_list_rows_ready(page)
            if (
                TEXT_ROLE_ROUTE in current_url
                and _role_list_ready(page)
                and not observer.auth_error
                and _role_list_data_ready(page)
                # A non-empty rendered table is sufficient when the browser
                # served the list from cache before the response listener was
                # attached. An explicit empty list still requires a captured
                # successful response so a loading shell can never trigger
                # the add branch.
                and (observer.successful_response or (rows_rendered and not observer.response_count))
            ):
                print(
                    f"[browser] 课文角色列表已就绪：{_role_list_row_count(page)} 条册别记录。",
                    flush=True,
                )
                return
            if TEXT_ROLE_ROUTE in current_url and _visible_exact(page, "保存角色") is not None:
                cancel = _visible_exact(page, "取消")
                if cancel is not None:
                    cancel.click()
                    page.wait_for_timeout(100)
                    continue
            body = _body_text(page)
            login_page = "#/login" in current_url or any(
                token in body for token in ("登录状态已失效", "欢迎登录", "扫码登录", "账号登录", "密码登录")
            )
            if login_page:
                if not warned:
                    print("[browser] 登录状态已失效，请在打开的 Chrome 窗口完成登录；脚本会继续等待。", flush=True)
                    warned = True
            else:
                try:
                    if TEXT_ROLE_ROUTE not in current_url:
                        page.goto(TEXT_ROLE_URL, wait_until="domcontentloaded", timeout=60_000)
                except Exception:
                    pass
            page.wait_for_timeout(100)
        if observer.response_count and not observer.successful_response:
            raise RuntimeError("课文角色列表接口返回异常，已停止新增以避免创建重复册别。")
        raise RuntimeError("等待登录/课文角色列表数据超时；未维护任何角色。")
    finally:
        observer.close()


def _wait_for_role_list(
    page: Any,
    timeout: int = 30_000,
    *,
    observer: TextRoleListResponseObserver | None = None,
    after_response_count: int = 0,
) -> None:
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        rows_rendered = _role_list_rows_ready(page)
        if observer is not None:
            if observer.auth_error and observer.response_count > after_response_count:
                raise RuntimeError("保存角色后课文角色列表请求未通过登录校验")
            response_ready = (
                observer.response_count > after_response_count
                and observer.successful_response
            )
            # 保存后有些部署会直接复用已缓存的列表状态，不再发新的
            # GET。只要编辑表单已消失、列表行已经真实渲染，就说明页面
            # 已经回到可匹配状态；空列表仍必须由成功响应确认，避免把
            # 加载中的空壳误判成“册别不存在”。
            cached_rows_ready = rows_rendered and observer.response_count <= after_response_count
        else:
            response_ready = True
            cached_rows_ready = False
        if (
            _role_list_ready(page)
            and _role_list_data_ready(page)
            and (response_ready or cached_rows_ready)
        ):
            return
        page.wait_for_timeout(100)
    raise RuntimeError("保存角色后未返回课文角色列表")


def _modify_role_volume(page: Any, record: Mapping[str, Any], roles: Sequence[str]) -> None:
    target = _find_role_volume_row(page, record)
    volume_label = " / ".join(
        _text(record.get(field), limit=128)
        for field in ("version", "stage", "grade", "volume")
        if _text(record.get(field), limit=128)
    )

    if target is not None:
        print(f"[browser] 已找到角色册别，进入修改：{volume_label}", flush=True)
        modify = target.get_by_text("修改", exact=True)
        if modify.count() == 0:
            modify = target.locator("button").filter(has_text=re.compile(r"^\s*修改\s*$"))
        if modify.count() == 0:
            raise RuntimeError("找到对应册别，但没有找到“修改”按钮")
        modify.last.click()
        _wait_for_role_form(page)
    else:
        print(f"[browser] 未找到角色册别，准备新增：{volume_label}", flush=True)
        _click_exact(page, "添加角色")
        _wait_for_role_form(page)
        for index, field in enumerate(("version", "stage", "grade", "volume")):
            value = _text(record.get(field), limit=128)
            if not value:
                raise RuntimeError(f"新增角色册别缺少分类字段：{field}")
            _select_option(page, index, value)

    existing = {
        _role_key(_role_input_value(_role_form_inputs(page).nth(index)))
        for index in range(_role_form_inputs(page).count())
    }
    added = False
    for role in roles:
        role = _text(role, limit=256)
        key = _role_key(role)
        if not key or key in existing:
            continue
        inputs = _role_form_inputs(page)
        empty_input = None
        for index in range(inputs.count()):
            candidate = inputs.nth(index)
            if not _role_input_value(candidate):
                empty_input = candidate
                break
        if empty_input is None:
            before = inputs.count()
            _click_exact(page, "添加新角色")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _role_form_inputs(page).count() <= before:
                page.wait_for_timeout(100)
            inputs = _role_form_inputs(page)
            if inputs.count() <= before:
                raise RuntimeError("点击“添加新角色”后没有出现新的角色输入框")
            empty_input = inputs.last
        empty_input.fill(role)
        if _role_input_value(empty_input).casefold() != role.casefold():
            raise RuntimeError(f"角色名称回读不一致：期望 {role!r}")
        existing.add(key)
        added = True

    if added:
        observer = TextRoleListResponseObserver(page)
        try:
            baseline_response_count = observer.response_count
            _click_exact(page, "保存角色")
            _wait_for_role_list(
                page,
                observer=observer,
                after_response_count=baseline_response_count,
            )
        finally:
            observer.close()
    else:
        # Existing roles are intentionally untouched; leave the edit page
        # without submitting so the idempotent update has no needless write.
        cancel = _visible_exact(page, "取消")
        if cancel is not None:
            cancel.click()
            _wait_for_role_list(page)


def _ensure_textbook_roles(page: Any, records: Sequence[Mapping[str, Any]], login_timeout_seconds: int) -> None:
    """Create missing volume roles and append only roles absent from a volume."""

    volumes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for record in records:
        roles = list(record.get("roles") or [])
        item_sources: list[Any] = [record.get("items") or []]
        paragraphs = record.get("paragraphs")
        if isinstance(paragraphs, Sequence) and not isinstance(paragraphs, (str, bytes, bytearray)):
            item_sources.extend(
                paragraph.get("items") or []
                for paragraph in paragraphs
                if isinstance(paragraph, Mapping)
            )
        for source in item_sources:
            for item in source:
                role = _text(item.get("role"), limit=256) if isinstance(item, Mapping) else ""
                if role:
                    roles.append(role)
        unique_roles: list[str] = []
        seen: set[str] = set()
        for role in roles:
            key = _role_key(role)
            if key and key not in seen:
                seen.add(key)
                unique_roles.append(_text(role, limit=256))
        if not unique_roles:
            continue
        key = tuple(_text(record.get(field), limit=128) for field in ("version", "stage", "grade", "volume"))
        volume = volumes.setdefault(key, {field: record.get(field) for field in ("version", "stage", "grade", "volume")})
        volume.setdefault("roles", []).extend(unique_roles)
    if not volumes:
        return

    _open_role_list(page, login_timeout_seconds)
    for volume in volumes.values():
        roles: list[str] = []
        seen: set[str] = set()
        for role in volume.get("roles", []):
            key = _role_key(role)
            if key and key not in seen:
                seen.add(key)
                roles.append(role)
        _modify_role_volume(page, volume, roles)


def _fill_classification(page: Any, record: Mapping[str, Any]) -> None:
    chinese_name = page.get_by_placeholder("请输入课文名称（中文）", exact=True)
    english_name = page.get_by_placeholder("请输入课文名称（英文）", exact=True)
    chinese_name.fill(str(record["name_zh"]))
    english_name.fill(str(record["name_en"]))

    values = [
        record["form"],
        record["version"],
        record["stage"],
        record["grade"],
        record["volume"],
        record["unit"],
        record["lesson"],
    ]
    for index, value in enumerate(values):
        _select_option(page, index, str(value))


def _wait_for_card_audio_label(
    card: Any,
    stem: str,
    normalized_stem: str,
    *,
    timeout_ms: int = 20_000,
) -> bool | None:
    """Wait for the upload label in Playwright instead of Python polling.

    ``None`` means that a lightweight page shim has no locator wait API and
    the caller should use its compatibility polling loop. ``False`` means a
    real locator wait timed out or the final readback did not match.
    """

    get_by_text = getattr(card, "get_by_text", None)
    if not callable(get_by_text):
        return None
    try:
        pattern = re.compile(
            rf"(?:{re.escape(stem)}|{re.escape(normalized_stem)})"
        )
        label = get_by_text(pattern)
        waiter = getattr(label, "wait_for", None)
        if not callable(waiter):
            return None
        waiter(state="visible", timeout=timeout_ms)
        card_text = _fast_inner_text(card, timeout_ms=500)
        return stem in card_text or normalized_stem in card_text
    except Exception:
        return False


def _visible_field(owner: Any, selectors: Sequence[str]) -> Any | None:
    for selector in selectors:
        try:
            fields = owner.locator(selector)
            for index in range(fields.count()):
                field = fields.nth(index)
                if field.is_visible():
                    return field
        except Exception:
            continue
    return None


def _fill_paragraph_title(page: Any, paragraph: Any, value: Any, index: int = 0) -> None:
    title = _text(value, limit=256)
    if not title:
        return
    selectors = (
        "input[placeholder='请输入段落标题']:visible",
        "textarea[placeholder='请输入段落标题']:visible",
        ".paragraph-title input:visible",
        ".paragraph-title textarea:visible",
    )
    # On the real 段落 page the title input is a sibling of the sentence
    # cards inside ``.paragraphContent``. It must be scoped to that paragraph;
    # a page-global first-input lookup would put every later title in 段落1.
    field = _visible_field(paragraph, selectors)
    if field is None:
        raise RuntimeError(f"第 {index + 1} 段没有找到段落标题字段：{title}")
    field.fill(title)
    try:
        actual = _text(field.input_value(), limit=256)
    except Exception:
        actual = _text(field.get_attribute("value"), limit=256)
    if actual != title:
        raise RuntimeError(
            f"第 {index + 1} 段标题回读不一致：期望 {title!r}，实际 {actual!r}"
        )


def _fill_card_role(page: Any, card: Any, value: Any, index: int) -> None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        roles = [_text(role, limit=256) for role in value if _text(role, limit=256)]
    else:
        role = _text(value, limit=256)
        roles = [role] if role else []
    if not roles:
        return
    # The live role-play form uses a multi-select button grid rather than an
    # Element Plus combobox. Selected buttons switch from characterNormal to
    # characterSelected; do not click an already-selected role a second time.
    # The card shell renders before the role grid is filled from the textbook
    # role list. Wait for the roles needed by this card, otherwise the first
    # card of a newly opened conversation can be mistaken for a card with no
    # roles at all.
    buttons = card.locator(".characterItem:visible")
    missing = list(roles)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if buttons.count() == 0:
            buttons = card.locator(".characterItem")
        missing = [
            role
            for role in roles
            if buttons.filter(
                has_text=re.compile(rf"^\s*{re.escape(role)}\s*$")
            ).count()
            == 0
        ]
        if not missing:
            break
        remaining_ms = int(max(1, (deadline - time.monotonic()) * 1000))
        page.wait_for_timeout(min(100, remaining_ms))
    if buttons.count() == 0:
        raise RuntimeError(
            f"第 {index + 1} 条角色扮演内容没有找到角色按钮：{', '.join(roles)}"
        )
    if missing:
        raise RuntimeError(f"第 {index + 1} 条没有找到角色：{missing[0]}")
    for role in roles:
        match = buttons.filter(
            has_text=re.compile(rf"^\s*{re.escape(role)}\s*$")
        )
        if match.count() == 0:
            raise RuntimeError(f"第 {index + 1} 条没有找到角色：{role}")
        button = match.last
        class_name = str(button.get_attribute("class") or "")
        if "characterSelected" not in class_name:
            button.click()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            class_name = str(button.get_attribute("class") or "")
            if "characterSelected" in class_name:
                break
            page.wait_for_timeout(50)
        if "characterSelected" not in class_name:
            raise RuntimeError(f"第 {index + 1} 条角色选择回读失败：{role}")


def _wait_for_paragraphs(page: Any, count: int, timeout: int = 30_000) -> Any:
    paragraphs = page.locator(".paragraphContent:visible")
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        try:
            if paragraphs.count() >= count:
                return paragraphs
        except Exception:
            pass
        remaining_ms = int(max(1, (deadline - time.monotonic()) * 1000))
        page.wait_for_timeout(min(100, remaining_ms))
    raise RuntimeError(f"等待课文段落编辑区超时：需要 {count} 个")


def _ensure_paragraph_count(page: Any, count: int) -> Any:
    if count <= 0:
        raise RuntimeError("段落类型课文没有可录入的段落")
    paragraphs = page.locator(".paragraphContent:visible")
    current = paragraphs.count()
    if current == 0:
        # The page first fetches the empty outline asynchronously. Wait for it
        # before using the global add control so a slow response cannot create
        # an extra paragraph.
        try:
            paragraphs = _wait_for_paragraphs(page, 1, timeout=5_000)
            current = paragraphs.count()
        except Exception:
            _click_exact(page, "继续添加段落")
            paragraphs = _wait_for_paragraphs(page, 1)
            current = paragraphs.count()
    while current < count:
        _click_exact(page, "继续添加段落")
        paragraphs = _wait_for_paragraphs(page, current + 1)
        current = paragraphs.count()
    return paragraphs


def _paragraph_specs(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = record.get("paragraphs")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        specs = [paragraph for paragraph in raw if isinstance(paragraph, Mapping)]
        if specs:
            return specs

    # Compatibility fallback for older saved plans: rebuild blocks from the
    # stable paragraph_id carried on each item instead of making one page
    # paragraph per sentence.
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in record.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("paragraph_id"), limit=256) or "paragraph-1"
        if key not in grouped:
            grouped[key] = {
                "title": _text(item.get("paragraph_title"), limit=256),
                "items": [],
            }
            order.append(key)
        grouped[key]["items"].append(item)
    return [grouped[key] for key in order]


def _fill_content_card(
    page: Any,
    card: Any,
    item: Mapping[str, Any],
    index: int,
    *,
    paragraph_index: int | None = None,
) -> None:
    card.scroll_into_view_if_needed()
    _fill_card_role(page, card, item.get("role"), index)
    editors = card.locator(
        '.rich-text-editor .editor-content[contenteditable="true"]'
    )
    if editors.count() < 2:
        location = f"第 {paragraph_index + 1} 段第 {index + 1} 条" if paragraph_index is not None else f"第 {index + 1} 条"
        raise RuntimeError(f"{location}没有找到原文/译文编辑器")
    _replace_editor(
        page,
        editors.nth(0),
        _textbook_page_text(item["original"], item.get("role")),
        "原文",
    )
    _replace_editor(
        page,
        editors.nth(1),
        str(item.get("translation") or ""),
        "译文",
    )

    file_inputs = card.locator('input[type="file"]')
    if file_inputs.count() < 1:
        location = f"第 {paragraph_index + 1} 段第 {index + 1} 条" if paragraph_index is not None else f"第 {index + 1} 条"
        raise RuntimeError(f"{location}没有找到音频上传控件")
    audio_path = str(item["audio_path"])
    file_inputs.first.set_input_files(audio_path)
    # 平台会把文件名中的非单词字符替换成下划线后再展示。
    stem = Path(audio_path).stem
    normalized_stem = re.sub(r"\W+", "_", stem, flags=re.UNICODE)
    audio_label_ready = _wait_for_card_audio_label(
        card,
        stem,
        normalized_stem,
    )
    if audio_label_ready is None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            card_text = _fast_inner_text(card, timeout_ms=300)
            if stem in card_text or normalized_stem in card_text:
                audio_label_ready = True
                break
            remaining_ms = int(max(1, (deadline - time.monotonic()) * 1000))
            page.wait_for_timeout(min(100, remaining_ms))

    if not audio_label_ready:
        location = f"第 {paragraph_index + 1} 段第 {index + 1} 条" if paragraph_index is not None else f"第 {index + 1} 条"
        raise RuntimeError(f"{location}音频回读失败：{stem}")


def _fill_content(
    page: Any,
    record: Mapping[str, Any],
    *,
    control_check: Callable[[], None] | None = None,
) -> None:
    tracer = page_perf(page, operation="textbook-input")
    with tracer.span("fill_content", item_count=len(record.get("items") or ())):
        return _fill_content_impl(page, record, control_check=control_check)


def _fill_content_impl(
    page: Any,
    record: Mapping[str, Any],
    *,
    control_check: Callable[[], None] | None = None,
) -> None:
    items = record["items"]
    if _text(record.get("form"), limit=64) == "段落":
        paragraphs = _paragraph_specs(record)
        page_paragraphs = _ensure_paragraph_count(page, len(paragraphs))
        for paragraph_index, paragraph in enumerate(paragraphs):
            if control_check is not None:
                control_check()
            paragraph_owner = page_paragraphs.nth(paragraph_index)
            _fill_paragraph_title(
                page,
                paragraph_owner,
                paragraph.get("title") or paragraph.get("paragraph_title"),
                paragraph_index,
            )
            paragraph_items = [
                item for item in paragraph.get("items") or []
                if isinstance(item, Mapping)
            ]
            if not paragraph_items:
                raise RuntimeError(f"第 {paragraph_index + 1} 段没有可录入的句子")
            cards = _ensure_card_count(
                page,
                len(paragraph_items),
                owner=paragraph_owner,
            )
            for item_index, item in enumerate(paragraph_items):
                if control_check is not None:
                    control_check()
                _fill_content_card(
                    page,
                    cards.nth(item_index),
                    item,
                    item_index,
                    paragraph_index=paragraph_index,
                )
                if control_check is not None:
                    control_check()
        return

    cards = _ensure_card_count(page, len(items))
    for index, item in enumerate(items):
        if control_check is not None:
            control_check()
        _fill_content_card(page, cards.nth(index), item, index)
        if control_check is not None:
            control_check()


def _body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=500) or "")
    except Exception:
        return ""


def _open_text_list(page: Any, login_timeout_seconds: int) -> None:
    _configure_page_timeouts(page)
    page.goto(RESOURCE_TEXT_URL, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + login_timeout_seconds
    warned = False
    while time.monotonic() < deadline:
        if _visible_exact(page, "新增课文") is not None:
            return
        body = _body_text(page)
        login_page = any(token in body for token in ("登录状态已失效", "欢迎登录", "扫码登录"))
        if login_page:
            if not warned:
                print("[browser] 登录状态已失效，请在打开的 Chrome 窗口完成登录；脚本会继续等待。", flush=True)
                warned = True
        else:
            # 登录后如果停在首页，直接回到已经核实过的课文管理路由。
            try:
                if RESOURCE_TEXT_ROUTE not in str(page.url):
                    page.goto(RESOURCE_TEXT_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
        page.wait_for_timeout(200)
    raise RuntimeError("等待登录/课文管理页面超时；未创建任何课文记录。")


def _create_textbook_record(
    page: Any,
    record: Mapping[str, Any],
    index: int,
    total: int,
    observer: TextbookRecordObserver,
    control_check: Callable[[], None] | None = None,
) -> str:
    print(
        f"[browser] 录入 {index}/{total}: {_text(record.get('name_zh'), limit=128)} / "
        f"{_text(record.get('name_en'), limit=128)}（{len(record.get('items') or [])} 条）",
        flush=True,
    )
    if control_check is not None:
        control_check()
    _click_exact(page, "新增课文")
    page.get_by_placeholder("请输入课文名称（中文）", exact=True).wait_for(state="visible")
    _fill_classification(page, record)
    if control_check is not None:
        control_check()
    # “下一步”会先保存课文大纲并创建平台记录；记录 ID 从这一步的
    # 响应开始捕获。
    observer.begin()
    _click_exact(page, "下一步:录入课文内容")
    page.get_by_text("第二步：录入课文句子内容", exact=True).wait_for(state="visible")
    if control_check is not None:
        control_check()
    _fill_content(page, record, control_check=control_check)
    if control_check is not None:
        control_check()
    _click_exact(page, "保存课文句子")
    try:
        page.get_by_text("保存成功", exact=True).wait_for(state="visible", timeout=20_000)
    except Exception:
        # 部署版本可能只返回列表而不保留 toast；列表按钮同样是成功边界。
        _visible_exact(page, "新增课文")
    record_id = observer.take_record_id()
    observer.end()
    if control_check is not None:
        control_check()
    if _visible_exact(page, "新增课文") is None:
        page.goto(RESOURCE_TEXT_URL, wait_until="domcontentloaded", timeout=60_000)
        page.get_by_text("新增课文", exact=True).wait_for(state="visible", timeout=30_000)
        if control_check is not None:
            control_check()
    return record_id



def execute_records(
    records: Sequence[Mapping[str, Any]],
    *,
    profile_dir: Any,
    api_base: str,
    login_timeout: float,
    keep_browser_open: bool = False,
    browser_session: Any | None = None,
    control_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """通过可见浏览器逐条录入课文记录。"""

    def run_in_context(context: Any) -> dict[str, Any]:
        page = browser_session.page() if browser_session is not None else (
            context.pages[0] if context.pages else context.new_page()
        )
        _configure_page_timeouts(page)
        if control_check is not None:
            control_check()
        try:
            _ensure_textbook_roles(page, records, max(1, int(login_timeout)))
            _open_text_list(page, max(1, int(login_timeout)))
            observer = TextbookCatalogResponseObserver(
                page,
                api_base=API_BASE_URL,
            )
            before_search_responses = observer.response_count
        except RuntimeError as exc:
            # _open_text_list 超时抛 RuntimeError；转成 TimeoutError 供
            # 执行器区分“登录未完成”与一般执行失败。
            raise TimeoutError(str(exc)) from exc
        observer = TextbookRecordObserver(page, api_base=api_base)
        try:
            results: list[dict[str, Any]] = []
            for index, record in enumerate(records, 1):
                record_id = _create_textbook_record(
                    page,
                    record,
                    index,
                    len(records),
                    observer,
                    control_check=control_check,
                )
                results.append({
                    "name_zh": _text(record.get("name_zh"), limit=256),
                    "name_en": _text(record.get("name_en"), limit=256),
                    "external_record_id": record_id or None,
                })
                if control_check is not None:
                    control_check()
            print(
                f"[textbook-page] 完成：{len(results)} 条课文记录。",
                flush=True,
            )
            if keep_browser_open:
                print("[textbook-page] Chrome 保持打开；按 Ctrl+C 结束脚本。", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
            return {"status": "completed", "records": results}
        finally:
            observer.close()

    if browser_session is not None:
        return run_in_context(browser_session.open())

    from playwright.sync_api import sync_playwright
    from .runtime import _launch_browser

    profile_dir = getattr(profile_dir, "expanduser", lambda: profile_dir)()
    profile_dir = getattr(profile_dir, "resolve", lambda: profile_dir)()
    with sync_playwright() as playwright:
        context = _launch_browser(playwright, profile_dir)
        try:
            return run_in_context(context)
        finally:
            context.close()


def verify_live(
    name_zh: str,
    *,
    profile_dir: Any,
    login_timeout: float,
) -> dict[str, Any]:
    """只读核验：在课文管理列表中按课文名称搜索已有记录。

    唯一的页面动作是普通的列表搜索控件；不读取或提交任何写接口。
    """

    from playwright.sync_api import sync_playwright

    from .runtime import _launch_browser

    profile_dir = getattr(profile_dir, "expanduser", lambda: profile_dir)()
    profile_dir = getattr(profile_dir, "resolve", lambda: profile_dir)()
    title = _text(name_zh, limit=256)
    with sync_playwright() as playwright:
        context = _launch_browser(playwright, profile_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _configure_page_timeouts(page)

            _open_text_list(page, max(1, int(login_timeout)))
            observer = TextbookCatalogResponseObserver(
                page,
                api_base=API_BASE_URL,
            )
            before_search_responses = observer.response_count
            search = page.locator("input[placeholder*='课文名称']").first
            search.scroll_into_view_if_needed()
            search.fill(title)
            page.keyboard.press("Enter")
            # Wait for the list request issued by the page. A fixed 2.5s
            # sleep made every read-only verification slow even when the
            # response had already arrived.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if observer.response_count > before_search_responses:
                    break
                remaining_ms = int(max(1, (deadline - time.monotonic()) * 1000))
                page.wait_for_timeout(min(100, remaining_ms))
            # Give Vue one short render tick after the response callback.
            page.wait_for_timeout(100)
            rows = page.locator(".el-table__row:visible")
            matches: list[dict[str, Any]] = []
            count = min(rows.count(), 20)
            for index in range(count):
                row_text = _text(
                    _fast_inner_text(rows.nth(index), timeout_ms=500),
                    limit=2048,
                )
                if title in row_text:
                    matches.append({"external_record_id": None, "status": "FOUND"})
            return {
                "status": "FOUND" if matches else "NOT_FOUND",
                "matches": matches,
            }
        finally:
            context.close()


__all__ = [
    "TextbookCatalogResponseObserver",
    "TextbookRecordObserver",
    "execute_records",
    "sync_textbook_catalog_live",
    "verify_live",
]
