"""Drive and verify the platform's visible catalogue pagination controls."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


def visible_next_button(page: Any) -> Any | None:
    """Find either an Element pagination button or the platform's arrow control."""

    selectors = (
        "button.btn-next:visible",
        ".el-pagination button[aria-label*='下一页']:visible",
        ".el-pagination button[title*='下一页']:visible",
        ".arrow-right:visible",
    )
    for selector in selectors:
        try:
            nodes = page.locator(selector)
            for index in range(nodes.count()):
                node = nodes.nth(index)
                if node.is_visible():
                    # The custom arrow is a child of the clickable div; its
                    # parent's no-change class also marks the last page.
                    return node.locator("..") if selector == ".arrow-right:visible" else node
        except Exception:
            continue
    return None


def pagination_button_disabled(button: Any) -> bool:
    try:
        if button.is_disabled():
            return True
    except Exception:
        pass
    try:
        classes = set(str(button.get_attribute("class") or "").split())
        return bool(classes & {"is-disabled", "disabled", "no-change"}) or (
            str(button.get_attribute("aria-disabled") or "").casefold() == "true"
        )
    except Exception:
        return False


def validate_catalog_rows(rows: Sequence[Mapping[str, Any]], total: int | None, label: str) -> None:
    """Do not publish a catalogue made incomplete by repeated page data."""

    if total is not None and len(rows) < total:
        raise RuntimeError(f"{label}去重后只有 {len(rows)} / {total} 条，未保存不完整数据。")


def collect_catalog_pages(
    page: Any,
    pages: Mapping[int, Sequence[Mapping[str, Any]]],
    page_versions: Mapping[int, int],
    *,
    total: Callable[[], int | None],
    set_expected_page: Callable[[int], None],
    auth_error: Callable[[], bool],
    label: str,
    recover_auth: Callable[[], None] | None = None,
    timeout: float = 30.0,
    control_timeout: float = 5.0,
    max_pages: int = 1000,
) -> set[int]:
    """Collect each page only after observing its fresh list response."""

    visited = {1}
    current = 1
    while True:
        if auth_error():
            if recover_auth is None:
                raise TimeoutError("平台登录状态已失效")
            recover_auth()
            visited, current = {1}, 1
            continue

        received = sum(len(pages.get(number, ())) for number in visited)
        reported = total()
        if reported is not None and received >= reported:
            return visited

        # A response can arrive before Vue has rendered or re-enabled the
        # pagination control. Give the page time to settle before declaring
        # that a known-incomplete list cannot advance.
        control_deadline = time.monotonic() + max(0.0, control_timeout)
        restarted = False
        while True:
            button = visible_next_button(page)
            if button is not None and not pagination_button_disabled(button):
                break
            if reported is None and button is not None:
                return visited
            if auth_error():
                if recover_auth is None:
                    raise TimeoutError("平台登录状态已失效")
                recover_auth()
                visited, current = {1}, 1
                restarted = True
                break
            if time.monotonic() >= control_deadline:
                if reported is None:
                    raise RuntimeError(f"{label}缺少总数和分页控件，无法确认数据是否完整。")
                raise RuntimeError(
                    f"{label}只读取到 {received} / {reported} 条，无法继续翻页，请重试同步。"
                )
            page.wait_for_timeout(250)
        if restarted:
            continue
        if len(visited) >= max_pages:
            raise RuntimeError(f"{label}超过 {max_pages} 页，未能完整同步。")

        target = current + 1
        before = page_versions.get(target, 0)
        set_expected_page(target)
        button.click()
        deadline = time.monotonic() + max(0.0, timeout)
        restarted = False
        while time.monotonic() < deadline:
            if auth_error():
                if recover_auth is None:
                    raise TimeoutError("平台登录状态已失效")
                recover_auth()
                visited, current = {1}, 1
                restarted = True
                break
            if page_versions.get(target, 0) > before:
                break
            page.wait_for_timeout(250)
        else:
            raise RuntimeError(f"等待{label}第 {target} 页响应超时，未保存不完整数据。")
        if not restarted:
            visited.add(target)
            current = target
