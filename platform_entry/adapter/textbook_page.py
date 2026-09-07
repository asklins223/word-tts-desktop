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
    TEXTBOOK_MANAGEMENT_URL,
    TEXTBOOK_PAGE_PATH,
)
from .page_shared import _press_focused_key, _select_all_editable_text


def _text(value: Any, *, limit: int = 1024) -> str:
    return str(value or "").strip()[:limit]


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
        page.wait_for_timeout(1_000)
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
    candidate.click(timeout=timeout)


def _wait_for_cards(page: Any, count: int, timeout: int = 30_000) -> Any:
    deadline = time.monotonic() + timeout / 1000
    cards = page.locator(".expandContent:visible")
    while time.monotonic() < deadline:
        try:
            if cards.count() >= count:
                return cards
        except Exception:
            pass
        page.wait_for_timeout(200)
    raise RuntimeError(f"等待课文句子卡片超时：需要 {count} 个")


def _ensure_card_count(page: Any, count: int) -> Any:
    cards = page.locator(".expandContent:visible")
    current = cards.count()
    if current == 0:
        _click_exact(page, "继续添加句子")
        cards = _wait_for_cards(page, 1)
        current = cards.count()
    while current < count:
        button = _visible_exact(page, "继续添加下一句") or _visible_exact(page, "继续添加句子")
        if button is None:
            raise RuntimeError(f"无法继续添加句子：当前 {current} / {count}")
        button.click()
        cards = _wait_for_cards(page, current + 1)
        current = cards.count()
    return cards


def _replace_editor(page: Any, editor: Any, value: str, field_name: str) -> None:
    expected = re.sub(r"\s+", " ", str(value or "")).strip()

    def readback_matches(actual: str) -> bool:
        return expected in actual if expected else not actual

    try:
        _select_all_editable_text(editor)
        _press_focused_key(page, editor, "Backspace")
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
        _press_focused_key(page, editor, "Tab")
        actual = re.sub(r"\s+", " ", str(editor.inner_text() or "")).strip()
        if not readback_matches(actual):
            # A few historical editor builds only committed text after the
            # full keyboard event sequence.  Keep that slower behavior as a
            # bounded compatibility fallback instead of charging every long
            # paragraph one event per character.
            editor.click()
            editor.press("ControlOrMeta+A")
            _press_focused_key(page, editor, "Backspace")
            if value:
                editor.type(value)
            _press_focused_key(page, editor, "Tab")
            actual = re.sub(
                r"\s+", " ", str(editor.inner_text() or "")
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
    selectors = page.locator(".el-select__wrapper:visible")
    selector = selectors.nth(index)
    try:
        # The wrapper is visible and already resolved.  Dispatching its click
        # avoids a Windows hit-test round trip; a real click remains the
        # fallback for page builds that reject synthetic events.
        dispatch_open = getattr(selector, "dispatch_event", None)
        opened_dispatched = False
        if callable(dispatch_open):
            try:
                dispatch_open("click", timeout=500)
                opened_dispatched = True
            except Exception:
                pass
        if not opened_dispatched:
            selector.click(timeout=15_000)
    except Exception as exc:
        raise RuntimeError(
            f"创建页面下拉框数量不足，无法选择第 {index + 1} 项：{value}"
        ) from exc

    # 弹层渲染有延迟；若一直没有选项，可能是下拉没有展开，重新点击。
    escaped_parts = [re.escape(part) for part in value.split()]
    option_text = re.compile(
        r"^\s*" + r"\s+".join(escaped_parts) + r"\s*$",
        re.IGNORECASE,
    )
    option = page.locator("li:visible, [role='option']:visible").filter(
        has_text=option_text
    ).last
    dispatched = False
    try:
        # The option is already visible and resolved.  A DOM click avoids a
        # second Windows actionability/hit-test round trip; if this page
        # variant ignores synthetic clicks, the normal Playwright click below
        # remains the compatibility path.
        dispatch_event = getattr(option, "dispatch_event", None)
        if callable(dispatch_event):
            try:
                dispatch_event("click", timeout=500)
                dispatched = True
            except Exception:
                pass
        if not dispatched:
            option.click(timeout=2_500)
    except Exception:
        try:
            if not opened_dispatched:
                raise RuntimeError("synthetic open was not used")
            # The synthetic open may have worked even if the option event did
            # not.  Try the real option click before toggling the wrapper.
            if option.count():
                option.click(timeout=2_500)
            else:
                selector.click(timeout=2_500)
                option.click(timeout=9_500)
        except Exception:
            try:
                selector.click(timeout=2_500)
                option.click(timeout=9_500)
            except Exception as exc:
                # Keep the older DOM variants as a compatibility fallback,
                # but do not charge the normal path repeated Python scans.
                option = _find_dropdown_option(page, value)
                if option is None:
                    visible_texts = page.locator(
                        ".el-select-dropdown__item:visible"
                    ).all_inner_texts()
                    raise RuntimeError(
                        f"下拉框中没有找到选项：{value}；当前可见选项={visible_texts[:20]}"
                    ) from exc
                option.click()
    selected_text = re.compile(
        r"\s*".join(escaped_parts),
        re.IGNORECASE,
    )
    expected = re.sub(r"\s+", "", value).casefold()

    # In the normal Element Plus path the wrapper text is updated during the
    # dispatched click itself.  Read it once before asking Playwright to wait
    # for a filtered locator; the latter adds a visible Windows
    # waitForSelector round-trip even when the state is already ready.
    try:
        shown = re.sub(r"\s+", "", str(selector.inner_text() or ""))
    except Exception:
        shown = ""
    if expected and expected in shown.casefold():
        return
    try:
        selector.filter(has_text=selected_text).wait_for(
            state="visible",
            timeout=3_000,
        )
    except Exception as exc:
        try:
            shown = re.sub(r"\s+", "", str(selector.inner_text() or ""))
        except Exception:
            shown = ""
        if expected not in shown.casefold():
            if dispatched:
                # Some older page builds ignore a synthetic click.  Only
                # replay the real click after the fast path failed its
                # readback, so the normal path never pays for two clicks.
                try:
                    option.click(timeout=2_500)
                    selector.filter(has_text=selected_text).wait_for(
                        state="visible",
                        timeout=3_000,
                    )
                    return
                except Exception as fallback_exc:
                    exc = fallback_exc
            raise RuntimeError(
                f"下拉框回读不一致：期望 {value!r}，实际 {shown!r}"
            ) from exc


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


def _fill_content(
    page: Any,
    record: Mapping[str, Any],
    *,
    control_check: Callable[[], None] | None = None,
) -> None:
    items = record["items"]
    cards = _ensure_card_count(page, len(items))
    for index, item in enumerate(items):
        if control_check is not None:
            control_check()
        card = cards.nth(index)
        editors = card.locator(
            '.rich-text-editor .editor-content[contenteditable="true"]'
        )
        if editors.count() < 2:
            raise RuntimeError(f"第 {index + 1} 条没有找到原文/译文编辑器")
        _replace_editor(page, editors.nth(0), str(item["original"]), "原文")
        _replace_editor(
            page,
            editors.nth(1),
            str(item.get("translation") or ""),
            "译文",
        )

        file_inputs = card.locator('input[type="file"]')
        if file_inputs.count() < 1:
            raise RuntimeError(f"第 {index + 1} 条没有找到音频上传控件")
        audio_path = str(item["audio_path"])
        file_inputs.first.set_input_files(audio_path)
        # 平台会把文件名中的非单词字符替换成下划线后再展示。
        stem = Path(audio_path).stem
        normalized_stem = re.sub(r"\W+", "_", stem, flags=re.UNICODE)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                card_text = str(card.inner_text() or "")
                if stem in card_text or normalized_stem in card_text:
                    break
            except Exception:
                pass
            # The filename is rendered asynchronously, but a quarter-second
            # polling interval makes every upload pay a visible Windows tax.
            page.wait_for_timeout(50)
        else:
            raise RuntimeError(f"第 {index + 1} 条音频回读失败：{stem}")
        if control_check is not None:
            control_check()


def _body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=2_000) or "")
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
        page.wait_for_timeout(1_000)
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
            _open_text_list(page, max(1, int(login_timeout)))
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
            search = page.locator("input[placeholder*='课文名称']").first
            search.fill(title)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
            rows = page.locator(".el-table__row:visible")
            matches: list[dict[str, Any]] = []
            count = min(rows.count(), 20)
            for index in range(count):
                row_text = _text(rows.nth(index).inner_text(), limit=2048)
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
