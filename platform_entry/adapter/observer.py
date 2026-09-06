"""Read-only feedback observer for the visible 外部平台 page."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .common import normalise_text
from .constants import (
    API_BASE_URL,
    PAPER_CONTENT_GET_PATH,
    PAPER_PAGE_PATH,
    READ_ONLY_FEEDBACK_PATHS,
)

_normalise_text = normalise_text

def _candidate_identifier(value: Any) -> Any | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return value
    return None

def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _walk_mappings(child)

def _mapping_identifier(value: Mapping[str, Any]) -> Any | None:
    for key in ("paperId", "paper_id", "paperID", "id"):
        candidate = _candidate_identifier(value.get(key))
        if candidate is not None:
            return candidate
    return None

def _mapping_status(value: Mapping[str, Any]) -> Any | None:
    for key in (
        "statusName",
        "statusText",
        "stateName",
        "stateText",
        "status",
        "state",
    ):
        if value.get(key) not in (None, ""):
            return value[key]
    return None

class ReadOnlyFeedbackObserver:
    """监听页面响应，只解析白名单 GET，不主动发起任何请求。"""

    def __init__(
        self,
        page: Any,
        *,
        api_base: str = API_BASE_URL,
        admin_url: str = "",
        paper_title: str = "",
    ) -> None:
        self.page = page
        self.api_base = api_base.rstrip("/")
        self.admin_url = str(admin_url or "").rstrip("/")
        self.paper_title = paper_title
        self.read_only_requests: list[dict[str, Any]] = []
        self.write_responses_observed: list[dict[str, Any]] = []
        self.paper_id: Any | None = None
        self.status: Any | None = None
        self.content_readback_seen = False
        # Read-only verification needs every same-title row in the list
        # response, not just the first one: a duplicate title must fail the
        # verification instead of silently binding an arbitrary record.
        self.paper_matches: list[dict[str, Any]] = []
        self.list_response_count = 0
        self.content_dump_path = os.environ.get("PLATFORM_INPUT_CONTENT_DUMP", "").strip()
        if hasattr(page, "on"):
            page.on("response", self._on_response)

    def _is_platform_response(self, url: str) -> bool:
        # An origin prefix check would also accept a look-alike host such as
        # ``https://api.example.test.attacker.test``. The observer is allowed
        # to read feedback only from configured platform origins.
        try:
            expected = urlparse(self.api_base)
            actual = urlparse(str(url))
        except Exception:
            return False
        allowed_origins = {(expected.scheme.casefold(), expected.netloc.casefold())}
        # The admin SPA may send some read-only `/admin-api` requests to its
        # own configured origin. Accept that explicit companion origin without
        # broadening the observer to arbitrary hosts or reading write bodies.
        try:
            companion = urlparse(self.admin_url)
        except Exception:
            companion = None
        if (
            companion is not None
            and companion.scheme
            and companion.netloc
            and companion.scheme.casefold() == expected.scheme.casefold()
        ):
            allowed_origins.add((companion.scheme.casefold(), companion.netloc.casefold()))
        return bool(
            expected.scheme
            and expected.netloc
            and (actual.scheme.casefold(), actual.netloc.casefold()) in allowed_origins
        )

    @staticmethod
    def _response_method(response: Any) -> str:
        try:
            return str(response.request.method).upper()
        except Exception:
            return ""

    @staticmethod
    def _response_path(response: Any) -> str:
        try:
            return urlparse(str(response.url)).path
        except Exception:
            return ""

    @staticmethod
    def _response_status(response: Any) -> int | None:
        try:
            value = response.status
            return int(value) if value is not None else None
        except (TypeError, ValueError, AttributeError):
            return None

    def _on_response(self, response: Any) -> None:
        try:
            url = str(response.url)
            if not self._is_platform_response(url):
                return
            method = self._response_method(response)
            path = self._response_path(response)
            status = self._response_status(response)
            resource_type = getattr(response.request, "resource_type", None)

            if method in {"GET", "HEAD"} and path in READ_ONLY_FEEDBACK_PATHS:
                event = {"method": method, "path": path, "status": status}
                self.read_only_requests.append(event)
                if path in {PAPER_PAGE_PATH, PAPER_CONTENT_GET_PATH}:
                    body = self._read_json_body(response)
                    self._consume_read_only_body(path, url, body)
                return

            # 仅记录页面自己触发的 XHR/fetch 写响应，不读取其正文，也不
            # 使用响应内容来执行下一步。这样可以审计“页面做了什么”，
            # 同时保持脚本本身没有写接口客户端。
            if (
                method not in {"GET", "HEAD", "OPTIONS"}
                and resource_type in {None, "xhr", "fetch"}
            ):
                self.write_responses_observed.append(
                    {"method": method, "path": path, "status": status}
                )
        except Exception:
            # 监听器不能因单个响应对象异常影响页面操作。
            return

    @staticmethod
    def _read_json_body(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            try:
                text = response.text()
                return json.loads(text) if text else None
            except Exception:
                return None

    def _consume_read_only_body(self, path: str, url: str, body: Any) -> None:
        if path == PAPER_CONTENT_GET_PATH:
            self.content_readback_seen = True
            if self.content_dump_path:
                try:
                    Path(self.content_dump_path).expanduser().resolve().write_text(
                        json.dumps(body, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            query = parse_qs(urlparse(url).query)
            values = query.get("paperId") or query.get("paper_id")
            if values:
                candidate = _candidate_identifier(values[0])
                if candidate is not None:
                    self.paper_id = candidate
            for mapping in _walk_mappings(body):
                candidate = _mapping_identifier(mapping)
                if candidate is not None and "paperId" in mapping:
                    self.paper_id = candidate
                    break
            return

        if path != PAPER_PAGE_PATH:
            return

        self.list_response_count += 1
        wanted = _normalise_text(self.paper_title)
        if not wanted:
            return
        for mapping in _walk_mappings(body):
            title = mapping.get("title", mapping.get("paperName"))
            if _normalise_text(title) != wanted:
                continue
            candidate = _mapping_identifier(mapping)
            status = _mapping_status(mapping)
            row = {
                "paperId": candidate if candidate is not None else "",
                "status": status if status is not None else "",
            }
            if not any(
                existing.get("paperId") == row["paperId"] and existing.get("status") == row["status"]
                for existing in self.paper_matches
            ):
                self.paper_matches.append(row)
            if candidate is not None and self.paper_id is None:
                self.paper_id = candidate
            if status is not None and self.status is None:
                self.status = status

    def begin_saved_feedback_capture(self) -> None:
        """开始读取保存后的列表结果，但保留第二步 GET 得到的 paperId。"""

        self.status = None

    def close(self) -> None:
        """Detach the response listener before a shared page is reused."""

        if hasattr(self.page, "remove_listener"):
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        feedback: dict[str, Any] = {
            "paperId": self.paper_id,
            "status": self.status,
            "contentReadback": self.content_readback_seen,
            "readOnlyRequests": list(self.read_only_requests),
            "writeResponsesObserved": list(self.write_responses_observed),
            "paperMatches": list(self.paper_matches),
        }
        return feedback

__all__ = [
    "ReadOnlyFeedbackObserver",
    "_candidate_identifier",
    "_mapping_identifier",
    "_mapping_status",
    "_walk_mappings",
]
