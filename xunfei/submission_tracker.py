"""Works ID capture and submission-boundary correlation for the legacy page.

The browser session still owns the mutable state for now.  This mixin only
owns the request/response correlation behavior, so it can later be replaced by
the durable Provider submission tracker without changing page actions.
"""

from __future__ import annotations

import time

from .config import MAX_TRACKED_SUBMISSION_REQUESTS, _provider_success_code
from .errors import _check_cancel_requested, _log


class SubmissionTrackerMixin:
    """Capture temporary/formal works IDs without crossing task boundaries."""

    def _remember_api_request(self, request):
        """记录网页真实请求中的认证信息，供列表/签名接口复用。"""
        try:
            payload = request.post_data_json
        except Exception:
            payload = None
        if isinstance(payload, dict):
            base = payload.get("base")
            if isinstance(base, dict):
                with self._works_lock:
                    for key in ("appid", "channelId", "userId", "osid"):
                        if key in base and base[key] not in (None, ""):
                            self._api_base[key] = base[key]

        try:
            headers = request.all_headers()
        except Exception:
            headers = {}
        authorization = headers.get("authorization") or headers.get("Authorization")
        if authorization:
            with self._works_lock:
                self._api_authorization = authorization

    def _on_request(self, request):
        """记录提交请求序号，供 response 事件做跨任务隔离。"""
        url = str(getattr(request, "url", "") or "")
        if "makeMultipleSpeakerWork" not in url and "order_gen" not in url:
            return
        # Playwright 不同版本可能为 response.request 返回新的 Python
        # 包装对象；优先比较稳定的底层实现对象，测试桩则退回自身。
        request_token = getattr(request, "_impl_obj", None)
        if request_token is None:
            request_token = request
        with self._works_lock:
            self._submission_request_sequence += 1
            sequence = self._submission_request_sequence
            self._submission_requests.append((request_token, sequence))
            if len(self._submission_requests) > MAX_TRACKED_SUBMISSION_REQUESTS:
                del self._submission_requests[:-MAX_TRACKED_SUBMISSION_REQUESTS]

    def _submission_sequence_for_request(self, request):
        """返回 response 对应的提交 request 序号；测试桩/旧页面可返回 None。"""
        if request is None:
            return None
        request_token = getattr(request, "_impl_obj", None)
        if request_token is None:
            request_token = request
        with self._works_lock:
            for tracked_request, sequence in reversed(self._submission_requests):
                if tracked_request is request_token:
                    return sequence
        return None

    def _on_response(self, response):
        url = response.url
        try:
            request = getattr(response, "request", None)
            is_submission_response = (
                "makeMultipleSpeakerWork" in url or "order_gen" in url
            )
            request_sequence = self._submission_sequence_for_request(request)
            if is_submission_response and request_sequence is not None:
                with self._works_lock:
                    if request_sequence <= self._submission_request_cutoff:
                        _log(
                            f"[xunfei]   忽略跨任务延迟 worksId response: "
                            f"sequence={request_sequence}, "
                            f"cutoff={self._submission_request_cutoff}"
                        )
                        return
            self._remember_api_request(response.request)
            wid = None
            is_final_work = False
            is_temporary_work = False
            if "makeMultipleSpeakerWork" in url:
                data = response.json()
                response_code = data.get("retCode")
                if response_code is None:
                    response_code = data.get("code")
                if _provider_success_code(response_code):
                    temporary_id = data.get("tempWorksId")
                    formal_id = data.get("worksId")
                    # 某些版本只返回 tempWorksId，另一些版本直接返回
                    # worksId；只有明确标为 temp 的值才进入临时 ID 保护。
                    wid = formal_id or temporary_id
                    is_final_work = bool(formal_id)
                    is_temporary_work = bool(temporary_id and not formal_id)
            elif "order_gen" in url:
                data = response.json()
                response_code = data.get("code")
                if response_code is None:
                    response_code = data.get("retCode")
                if _provider_success_code(response_code):
                    wid = (data.get("data") or {}).get("payOrder", {}).get("worksId")
                    is_final_work = True
            elif "get_work_sign_url" in url:
                data = response.json()
                response_code = data.get("code")
                if response_code is None:
                    response_code = data.get("retCode")
                if _provider_success_code(response_code) and (data.get("data") or {}).get("url"):
                    sign_works_id = None
                    try:
                        request_payload = response.request.post_data_json
                        sign_works_id = (request_payload.get("param", {}) or {}).get("worksId")
                    except Exception:
                        pass
                    if sign_works_id:
                        with self._works_lock:
                            self._sign_urls.append(
                                (str(sign_works_id), data["data"]["url"], time.time())
                            )
            if wid:
                with self._works_lock:
                    entry = (wid, time.time())
                    self._works_entries.append(entry)
                    if is_final_work:
                        self._final_works_entries.append(entry)
                    if is_temporary_work:
                        self._temporary_works_entries.append(entry)
                _log(f"[xunfei]   📝 捕获 worksId: {wid}")
        except Exception:
            pass

    def _mark_works_cutoff(self):
        """每条任务开始前调用：只接受本次任务发起的提交响应。"""
        with self._works_lock:
            self._works_entries.clear()
            self._final_works_entries.clear()
            self._temporary_works_entries.clear()
            self._sign_urls.clear()
            self._works_cutoff = time.time()
            self._submission_request_cutoff = self._submission_request_sequence

    @staticmethod
    def _duplicate_pending_work_ids(pending_items):
        """返回本批次重复的 worksId，禁止后续按字典键静默覆盖。"""
        counts = {}
        for item in pending_items or []:
            works_id = str(item.get("works_id") or "").strip()
            if works_id:
                counts[works_id] = counts.get(works_id, 0) + 1
        return {works_id for works_id, count in counts.items() if count > 1}

    def _consume_works_id(self, timeout=12, exclude_ids=None, cancel_check=None):
        excluded = {
            str(value)
            for value in (exclude_ids or [])
            if value not in (None, "")
        }
        deadline = time.time() + timeout
        while time.time() < deadline:
            _check_cancel_requested(cancel_check)
            with self._works_lock:
                fresh_final = [
                    e for e in self._final_works_entries
                    if e[1] >= self._works_cutoff - 0.5
                    and str(e[0]) not in excluded
                ]
                fresh = [
                    e for e in self._works_entries
                    if e[1] >= self._works_cutoff - 0.5
                    and str(e[0]) not in excluded
                ]
                # 同一次可见页面提交可能先捕获 tempWorksId，随后捕获
                # order_gen 的正式 worksId。正式 ID 才能稳定出现在作品页，
                # 必须优先于“最新到达”的临时 ID。
                candidate = fresh_final[-1] if fresh_final else (fresh[-1] if fresh else None)
                temporary_ids = {
                    str(e[0]) for e in self._temporary_works_entries
                    if e[1] >= self._works_cutoff - 0.5
                    and str(e[0]) not in excluded
                }
                temporary_only = (
                    candidate is not None
                    and not fresh_final
                    and str(candidate[0]) in temporary_ids
                )
                if candidate and not temporary_only:
                    wid = candidate[0]
                    self._works_entries.clear()
                    self._final_works_entries.clear()
                    self._temporary_works_entries.clear()
                    return wid
            # 作品 worksId 是 Playwright response 监听器异步写入的。同步
            # API 线程如果用 time.sleep，会阻塞事件分发，导致已经成功的
            # 提交直到超时后才被回调，随后上层误触发整条作品重试。让页面
            # 自己等待 100ms 可同时泵动真实浏览器事件；无页面时再退回
            # 普通 sleep（便于单元测试和关闭阶段调用）。
            if self._page is not None:
                try:
                    self._page.wait_for_timeout(100)
                    continue
                except Exception:
                    pass
            _check_cancel_requested(cancel_check)
            time.sleep(0.1)
        return None

