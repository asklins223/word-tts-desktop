"""Download, works-list reconciliation, and page recovery actions.

These methods remain a mixin because they share the live page/session state
with the legacy session.  The boundary is intentionally about responsibility:
works-list/API/download behavior is isolated from editor and confirmation UI.
"""

from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request
import uuid

from .config import (
    API_SIGN_URL,
    API_WORKS_LIST_URL,
    DOWNLOAD_PAGE_URL,
    HOME_URL,
    _provider_success_code,
)
from .errors import (
    XunfeiCancelled,
    XunfeiError,
    _check_cancel_requested,
    _log,
    _wait_with_cancel,
)
from .helpers import (
    looks_like_mp3 as _looks_like_mp3,
    normalize_download_label as _normalize_download_label,
    notify_batch_progress as _notify_batch_progress,
    poll as _poll,
    safe_eval as _safe_eval,
)
from .page_scripts import JS


class DownloadMixin:

    def _signed_api_post(self, page, url, param):
        """按讯飞网页真实 Axios 规则调用 video-api。"""
        credentials = _safe_eval(page, JS.GET_API_CREDENTIALS) or {}
        with self._works_lock:
            stable_base = dict(self._api_base)
            fallback_authorization = self._api_authorization

        user_id = credentials.get("userId") or stable_base.get("userId")
        authorization = credentials.get("sessid") or fallback_authorization
        if not user_id or not authorization:
            _log("[xunfei]   video-api 认证信息未就绪，无法请求作品数据")
            return None

        # 网页端 uuid(32, 50) 每个请求生成一个新的 sid；不能复用之前
        # response 里捕获的 sid，否则会被讯飞接口判为要素认证失败。
        base = {
            "appid": stable_base.get("appid") or "xfpy",
            "sid": uuid.uuid4().hex,
            "channelId": stable_base.get("channelId") or "40000001",
            "userId": str(user_id),
            "osid": stable_base.get("osid", 0),
        }
        headers = {
            "X-Channel-No": str(base["channelId"]),
            "authorization": authorization,
            "sign": _build_api_sign(param, base),
            "x-accept-language": "zh_CN",
        }
        result = _safe_eval(page, JS.POST_API_JSON, [url, param, base, headers])
        if not isinstance(result, dict):
            _log(f"[xunfei]   video-api 请求无响应: {url.rsplit('/', 1)[-1]}")
            return None
        data = result.get("data")
        if not isinstance(data, dict):
            _log(
                f"[xunfei]   video-api 返回异常: "
                f"{url.rsplit('/', 1)[-1]} HTTP {result.get('httpStatus')}"
            )
            return None
        response_code = data.get("code")
        if response_code is None:
            response_code = data.get("retCode")
        if not _provider_success_code(response_code):
            _log(
                f"[xunfei]   video-api 失败: "
                f"{url.rsplit('/', 1)[-1]} code={response_code} "
                f"desc={data.get('desc') or data.get('message') or '未知错误'}"
            )
            return None
        return data

    @staticmethod
    def _works_list_page_size(needed_count):
        """按讯飞接口约束计算固定页大小，分页期间不能随剩余量变化。"""
        try:
            needed = max(1, int(needed_count or 1))
        except (TypeError, ValueError, OverflowError):
            needed = 1
        return max(50, min(200, needed + 20))

    @staticmethod
    def _works_list_max_pages(needed_count):
        """为批量列表扫描设置有界页数，避免接口异常时无限轮询。"""
        try:
            needed = max(1, int(needed_count or 1))
        except (TypeError, ValueError, OverflowError):
            needed = 1
        # 按实际请求页大小估算所需页数，再额外预留 4 页覆盖历史作品
        # 插入；同时保留至少 5 页给单条断点任务寻找较早作品。
        page_size = DownloadMixin._works_list_page_size(needed)
        return min(100, max(5, (needed + page_size - 1) // page_size + 4))

    def _fetch_works_list_in_page(
        self,
        page,
        needed_count=1,
        page_index=1,
        works_name=None,
    ):
        """获取指定页的已完成作品列表，返回讯飞原始作品对象。"""
        # 作品列表按最新创建时间返回；批量提交可能超过接口单页上限，
        # 调用方通过 page_index 扫描后续页，不能只依赖第一页的 200 条。
        needed = max(1, int(needed_count or 1))
        page_size = self._works_list_page_size(needed)
        page_index = max(1, int(page_index or 1))
        param = {
            "needCount": 1,
            "pageIndex": page_index,
            "pageSize": page_size,
            "worksName": str(works_name or "").strip(),
        }
        data = self._signed_api_post(page, API_WORKS_LIST_URL, param)
        # _signed_api_post 对成功响应返回 dict，对认证/网络/API 错误返回
        # None。记录这个区别，断点恢复时不能把一次列表接口故障误判成
        # worksId 已失效，否则下一轮会重复提交并可能重复计费。
        self._last_works_list_fetch_ok = isinstance(data, dict)
        if not data:
            return []
        items = (data.get("data") or {}).get("userWorksList") or []
        return items if isinstance(items, list) else []

    def _fetch_works_list_pages(
        self,
        page,
        needed_count=1,
        expected_ids=None,
        works_name=None,
        cancel_check=None,
    ):
        """有界分页读取作品列表，直到找到目标 ID 或扫描完安全页数。"""
        try:
            target_count = max(1, int(needed_count or 1))
        except (TypeError, ValueError, OverflowError):
            target_count = 1
        expected = {
            str(value).strip()
            for value in (expected_ids or [])
            if str(value or "").strip()
        }
        records = []
        seen_record_ids = set()
        scan_complete = True
        self._last_works_list_scan_complete = None
        page_limit = self._works_list_max_pages(target_count)
        for page_index in range(1, page_limit + 1):
            _check_cancel_requested(cancel_check)
            self._last_works_list_fetch_ok = None
            fetch_kwargs = {
                "needed_count": target_count,
                "page_index": page_index,
            }
            # 只有对账时才带作品名过滤参数，保持旧版测试替身和普通列表
            # 请求的调用形态不变。
            if works_name:
                fetch_kwargs["works_name"] = str(works_name).strip()
            current = self._fetch_works_list_in_page(page, **fetch_kwargs)
            fetch_ok = getattr(self, "_last_works_list_fetch_ok", None)
            if fetch_ok is False:
                scan_complete = False
                break
            if not current:
                break
            for record in current:
                if not isinstance(record, dict):
                    continue
                record_id = record.get("id") or record.get("worksId")
                if record_id is None:
                    records.append(record)
                    continue
                normalized_id = str(record_id)
                if normalized_id in seen_record_ids:
                    continue
                seen_record_ids.add(normalized_id)
                records.append(record)
            if expected and expected.issubset(seen_record_ids):
                break
        else:
            # 到达安全页数上限但仍未找到全部目标，不能据此断言作品已删除。
            scan_complete = False
        self._last_works_list_scan_complete = scan_complete
        return records

    def _recover_works_id_by_name(
        self,
        page,
        works_name,
        timeout=60,
        cancel_check=None,
    ):
        """提交已确认但漏捕获 ID 时，只按唯一作品名做安全对账。

        作品名是提交前写入讯飞作品设置弹窗的短唯一值。对账必须同时满足
        “名称完全一致”和“只找到一个 ID”；否则保持不确定状态，绝不拿最新
        作品或临时 ID 猜测归属。
        """
        target_name = self._normalize_works_name(works_name)
        target_label = _normalize_download_label(target_name)
        if not target_label:
            return None
        deadline = time.time() + max(0, float(timeout))
        logged_wait = False
        while time.time() < deadline:
            _check_cancel_requested(cancel_check)
            records = self._fetch_works_list_pages(
                page,
                needed_count=1,
                works_name=target_name,
                cancel_check=cancel_check,
            )
            candidates = {}
            for record in records:
                if not isinstance(record, dict):
                    continue
                record_id = record.get("id") or record.get("worksId")
                record_name = (
                    record.get("worksName")
                    or record.get("works_name")
                    or record.get("name")
                    or record.get("title")
                )
                if record_id is None or not record_name:
                    continue
                if _normalize_download_label(record_name) != target_label:
                    continue
                candidates[str(record_id)] = record
            if len(candidates) == 1:
                works_id = next(iter(candidates))
                _log(
                    f"[xunfei] ✅ 通过唯一作品名找回已提交 worksId: "
                    f"{works_id} ({target_name})"
                )
                return works_id
            if len(candidates) > 1:
                _log(
                    f"[xunfei] ⚠️ 作品名对账发现多个 worksId，保持不确定状态: "
                    f"{target_name}"
                )
            elif not logged_wait:
                _log(f"[xunfei] ⏳ 等待作品列表对账: {target_name}")
                logged_wait = True
            if time.time() >= deadline:
                break
            try:
                page.wait_for_timeout(1000)
            except Exception:
                time.sleep(1)
        return None

    def _wait_for_works_entry(self, page, works_id, timeout=120):
        """等待同一个 worksId 出现在作品列表中，严禁按名称或最新记录替代。"""
        expected = str(works_id)
        deadline = time.time() + timeout
        logged_wait = False
        while time.time() < deadline:
            for item in self._fetch_works_list_in_page(page, needed_count=1):
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id") or item.get("worksId")
                if item_id is not None and str(item_id) == expected:
                    _log(f"[xunfei]   ✅ 作品列表已匹配 worksId: {expected}")
                    return item
            if not logged_wait:
                _log(f"[xunfei]   ⏳ 等待作品列表匹配 worksId: {expected}")
                logged_wait = True
            page.wait_for_timeout(2000)
        _log(f"[xunfei]   ⚠️ 作品列表未匹配到 worksId: {expected}")
        return None

    def _wait_for_works_ready(self, page, works_id, timeout=180):
        """等待精确 worksId 对应的音频文件真正可下载。"""
        expected = str(works_id)
        deadline = time.time() + timeout
        matched_logged = False
        waiting_logged = False
        while time.time() < deadline:
            items = self._fetch_works_list_in_page(page, needed_count=1)
            exact = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id") or item.get("worksId")
                if item_id is not None and str(item_id) == expected:
                    exact = item
                    break

            if exact:
                if not matched_logged:
                    _log(f"[xunfei]   ✅ 作品列表已匹配 worksId: {expected}")
                    matched_logged = True

                # 作品列表有时先返回记录，再异步补齐音频地址；优先使用
                # 该精确记录自身的地址，绝不使用其它作品的最新地址。
                audio_url = exact.get("audioUrl")
                if audio_url:
                    exact["_download_url"] = audio_url
                    _log(f"[xunfei]   ✅ 匹配作品音频已就绪 worksId: {expected}")
                    return exact

                # audioUrl 尚未补齐时，继续用同一个 worksId 请求签名 URL。
                # 接口可能先返回 code=0/url 为空，不能把这种状态当成功。
                sign_url = self._fetch_sign_url_in_page(
                    page, expected, log_result=False
                )
                if sign_url:
                    exact["_download_url"] = sign_url
                    _log(f"[xunfei]   ✅ 匹配作品签名 URL 已就绪 worksId: {expected}")
                    return exact

                if not waiting_logged:
                    _log(f"[xunfei]   ⏳ worksId 已匹配，等待音频文件就绪: {expected}")
                    waiting_logged = True

            elif not matched_logged and not waiting_logged:
                _log(f"[xunfei]   ⏳ 等待作品列表匹配 worksId: {expected}")
                waiting_logged = True

            page.wait_for_timeout(2000)

        _log(f"[xunfei]   ⚠️ 匹配作品在限定时间内仍不可下载 worksId: {expected}")
        return None

    def _fetch_sign_url_in_page(self, page, works_id, log_result=True):
        """按精确 worksId 请求对应签名 URL。"""
        param = {"worksId": str(works_id), "worksType": 1}
        data = self._signed_api_post(page, API_SIGN_URL, param)
        if not data:
            if log_result:
                _log(f"[xunfei]   签名接口未返回数据 worksId: {works_id}")
            return None
        url = (data.get("data") or {}).get("url")
        if log_result:
            _log(
                f"[xunfei]   签名接口结果 worksId={works_id}: "
                f"{'有 URL' if url else '无 URL'}"
            )
        return url

    def _cleanup_after_item(self, page):
        """单条提交后关闭残留弹窗并清空编辑器，不刷新页面。"""
        _safe_eval(page, JS.CLOSE_ALL_MODALS, [])
        # 讯飞页面的音色和三项参数状态要跨条复用；这里只清空输入内容，
        # 不能用 goto/reload，否则同一音色分组会被迫重复选择和设置参数。
        self._clear_editor(page)
        # 不再固定等待 1~2 秒。弹窗关闭动画和编辑器清空完成后立即继续，
        # 如果页面较慢则最多等待 2 秒，避免下一条输入撞上旧弹窗。
        ready = _poll(
            lambda: (
                not (_safe_eval(page, JS.GET_EDITOR_TEXT) or "").strip()
                and bool(_safe_eval(page, JS.CHECK_NO_VISIBLE_MODAL))
            ),
            timeout=2,
            interval=0.1,
            page=page,
        )
        if not ready:
            self._pause(page, 0.25, 0.08)

    def _recover_and_retry(self, page, cancel_check=None):
        """合成失败后恢复页面状态（重新加载编辑页，重置音色/参数记忆）。"""
        try:
            _check_cancel_requested(cancel_check)
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            editor_ready = _poll(
                lambda: bool(page.locator(".ssml-editor:visible").count()),
                timeout=30,
                interval=0.25,
                max_interval=1.0,
                page=page,
                cancel_check=cancel_check,
            )
            if not editor_ready:
                _log("[xunfei]   页面恢复后编辑器未就绪")
                return False
            if not self._dismiss_local_draft_prompt(page, cancel_check=cancel_check):
                _log("[xunfei]   页面仍被本地缓存恢复弹窗遮挡")
                return False
            self._current_voice_key = None
            self._current_voice_name = None
            self._applied_params = None
            return True
        except XunfeiCancelled:
            raise
        except Exception as e:
            _log(f"[xunfei]   页面恢复失败: {e}")
            return False

    def _recover_for_retry(self, page, cancel_check=None):
        """兼容旧调用桩，同时把真实取消探针传入页面恢复。"""
        if cancel_check is None:
            return self._recover_and_retry(page)
        return self._recover_and_retry(page, cancel_check=cancel_check)

    def _dismiss_local_draft_prompt(self, page, timeout=8, cancel_check=None):
        """清除讯飞持久 profile 遗留的本地编辑缓存提示。"""
        # 该层由前端异步挂载。首次 evaluate 发生在编辑器已经出现、但
        # 缓存提示尚未完成渲染的窗口内时，立即返回 ``not_found`` 会让
        # 后续输入动作撞上刚刚出现的遮罩层。只在本地页面上短暂等待它
        # 自己出现；没有提示时最多增加约 1.5 秒，不把正常启动变成长轮询。
        _check_cancel_requested(cancel_check)
        state = _safe_eval(page, JS.DISMISS_LOCAL_DRAFT_PROMPT)
        detect_deadline = time.monotonic() + min(1.5, max(0.0, float(timeout)))
        while state == "not_found" and time.monotonic() < detect_deadline:
            _check_cancel_requested(cancel_check)
            try:
                page.wait_for_timeout(100)
            except Exception:
                break
            state = _safe_eval(page, JS.DISMISS_LOCAL_DRAFT_PROMPT)
        # 旧版测试桩/兼容页面没有实现这个新探针，会返回 None 或 Mock；
        # 不应因此阻断原有的生成流程。真实页面的探针始终返回字符串。
        if not isinstance(state, str):
            return True
        if state == "not_found":
            return True
        if state != "clicked":
            _log(
                "[xunfei]   检测到讯飞本地缓存恢复弹窗，但未找到“空白开始”按钮"
            )
            return False

        cleared = _poll(
            lambda: (
                True
                if _safe_eval(page, JS.DISMISS_LOCAL_DRAFT_PROMPT) == "not_found"
                else None
            ),
            timeout=timeout,
            interval=0.15,
            max_interval=0.5,
            page=page,
            cancel_check=cancel_check,
        )
        if cleared:
            _log("[xunfei]   已清除讯飞上次中断留下的本地编辑缓存")
        else:
            _log("[xunfei]   讯飞本地缓存恢复弹窗关闭超时")
        return bool(cleared)

    @staticmethod
    def _click_visible_exact_button(page, label, scope=None):
        """点击可见且文字完全匹配的按钮，返回是否成功。"""
        root = scope or page
        try:
            buttons = root.locator('button:visible')
            for index in range(min(buttons.count(), 100)):
                button = buttons.nth(index)
                try:
                    if re.sub(r"\\s+", "", button.inner_text(timeout=500)).strip() != label:
                        continue
                    if button.is_disabled():
                        continue
                    button.click(force=True, timeout=5000)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _select_download_rows(self, page, targets):
        """在讯飞作品页按 worksId 对应的 orderNo 精确勾选作品行。"""
        selected = {}
        missing = list(targets)
        for attempt in range(8):
            state = _safe_eval(page, JS.SELECT_DOWNLOAD_ROWS, missing or targets) or {}
            for item in state.get("selected") or []:
                selected[str(item.get("works_id") or "")] = item
            missing_ids = {
                str(item.get("works_id") or "")
                for item in state.get("missing") or []
            }
            missing = [
                item for item in targets
                if str(item.get("works_id") or "") not in selected
                and str(item.get("works_id") or "") in missing_ids
            ]
            if not missing:
                break
            if attempt >= 7:
                break
            _safe_eval(page, JS.SCROLL_DOWNLOAD_LIST)
            page.wait_for_timeout(500)

        if selected:
            _log(
                f"[xunfei]   下载页已勾选 {len(selected)}/{len(targets)} 条作品"
            )
        if missing:
            _log(
                "[xunfei]   ⚠️ 下载页未找到 worksId 对应作品: "
                + ", ".join(str(item.get("works_id") or "") for item in missing)
            )
        return selected, missing

    def _download_selected_rows(
        self,
        page,
        selected_targets,
        progress_callback=None,
        cancel_check=None,
    ):
        """点击下载页“下载”，处理确认弹窗并收集所有浏览器下载事件。

        下载事件逐个到达时立即通知上层，不能等全部下载事件收集完成后
        才汇报，否则统一下载期间前端进度条会长时间停在 0%。

        这里只收集事件，不按事件到达顺序绑定作品。浏览器可能并发返回
        下载文件，真正的作品归属由调用方按唯一 worksName/worksId 匹配。
        """
        downloads = []

        def on_download(download):
            downloads.append(download)
            _log(f"[xunfei]   📥 下载页浏览器下载事件: {download.suggested_filename}")

        page.on("download", on_download)
        try:
            _check_cancel_requested(cancel_check)
            if not self._click_visible_exact_button(page, "下载"):
                _log("[xunfei]   ❌ 下载页未找到可用的“下载”按钮")
                return []

            # 当前页面通常直接触发多个 MP3 下载；部分账号会先弹出
            # Ant Design 下载确认框，再点击确认按钮。
            page.wait_for_timeout(500)
            _check_cancel_requested(cancel_check)
            dialog = self._find_visible_dialog(page, "下载")
            if dialog is not None:
                if not self._click_visible_exact_button(page, "下载", scope=dialog):
                    _log("[xunfei]   ❌ 未能点击下载确认弹窗中的“下载”")
                    return downloads
                _log("[xunfei]   已确认下载弹窗")

            expected = len(selected_targets)
            deadline = time.time() + 120
            while len(downloads) < expected and time.time() < deadline:
                _check_cancel_requested(cancel_check)
                page.wait_for_timeout(500)
            _log(
                f"[xunfei]   下载页事件完成: {len(downloads)}/{expected} 条"
            )
            return downloads
        finally:
            try:
                page.remove_listener("download", on_download)
            except Exception:
                pass

    @staticmethod
    def _match_download_index(downloads, target):
        """按唯一作品名/worksId匹配浏览器下载事件，避免乱序错配。"""
        target_values = [
            target.get("works_name"),
            target.get("works_id"),
            (target.get("item") or {}).get("works_name"),
        ]
        normalized_targets = [
            _normalize_download_label(value)
            for value in target_values
            if _normalize_download_label(value)
        ]
        if not normalized_targets:
            return None
        for index, download in enumerate(downloads):
            try:
                filename = download.suggested_filename
            except Exception:
                filename = ""
            normalized_filename = _normalize_download_label(filename)
            if not normalized_filename:
                continue
            if any(
                normalized_filename == value or value in normalized_filename
                for value in normalized_targets
            ):
                return index
        return None

    @staticmethod
    def _download_signed_url(download_url, output_path):
        """通过精确 worksId 对应的签名地址下载 MP3。"""
        if (
            not output_path
            or not str(download_url or "").startswith(("http://", "https://"))
        ):
            return False
        temporary_path = f"{output_path}.part"
        try:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            request = urllib.request.Request(
                str(download_url),
                headers={
                    "User-Agent": "Mozilla/5.0 WordTTS/1.0",
                    "Referer": HOME_URL,
                },
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                with open(temporary_path, "wb") as target:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        target.write(chunk)
            if not _looks_like_mp3(temporary_path):
                raise XunfeiError("签名地址返回的文件不是有效 MP3")
            os.replace(temporary_path, output_path)
            return True
        except (OSError, ValueError, urllib.error.URLError, XunfeiError) as error:
            _log(f"[xunfei]   worksId 签名下载失败: {error}")
            return False
        finally:
            try:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
            except OSError:
                pass

    def _download_pending_batch(
        self,
        pending_items,
        progress_callback=None,
        cancel_check=None,
    ):
        """进入讯飞作品下载页，按 worksId 勾选本批次作品后统一下载。"""
        _check_cancel_requested(cancel_check)
        page = self._page
        if not pending_items:
            return {}

        duplicate_ids = self._duplicate_pending_work_ids(pending_items)
        if duplicate_ids:
            raise XunfeiError(
                "批量任务包含重复 worksId，拒绝按字典键下载以免音频错配："
                + ", ".join(sorted(duplicate_ids))
            )

        _log(
            f"[xunfei] 进入讯飞作品下载页，准备勾选本批次 {len(pending_items)} 条音频"
        )
        _check_cancel_requested(cancel_check)
        try:
            page.goto(
                DOWNLOAD_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as error:
            raise XunfeiError(f"无法打开讯飞作品下载页: {error}")

        if not _poll(
            lambda: bool(_safe_eval(page, JS.CHECK_DOWNLOAD_PAGE)),
            timeout=30,
            interval=0.5,
            page=page,
            cancel_check=cancel_check,
        ):
            raise XunfeiError("讯飞作品下载页未加载完成")

        _log(f"[xunfei] 下载页已打开: {page.url}")
        ready = self._wait_for_pending_ready(
            page,
            pending_items,
            timeout=180,
            cancel_check=cancel_check,
        )
        _check_cancel_requested(cancel_check)
        records_kwargs = {
            "needed_count": max(len(pending_items), 1),
            "expected_ids": {
                str(item.get("works_id") or "")
                for item in pending_items
            },
        }
        if cancel_check is not None:
            records_kwargs["cancel_check"] = cancel_check
        records = self._fetch_works_list_pages(page, **records_kwargs)
        list_scan_complete = getattr(self, "_last_works_list_scan_complete", None)
        record_indexes = {}
        record_by_id = {}
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            record_id = record.get("id") or record.get("worksId")
            if record_id is None:
                continue
            record_id = str(record_id)
            record_indexes[record_id] = index
            record_by_id[record_id] = record

        results = {}
        targets = []
        for item in pending_items:
            _check_cancel_requested(cancel_check)
            works_id = str(item.get("works_id") or "")
            ready_item = ready.get(works_id) or {}
            ready_record = ready_item.get("record")
            record_found = isinstance(ready_record, dict) or works_id in record_by_id
            record = ready_record if isinstance(ready_record, dict) else (
                record_by_id.get(works_id) or {}
            )
            if works_id not in ready and not record_found:
                # 分页扫描可能早于列表刷新，或目标作品虽已存在但列表接口
                # 没有返回它。先用同一个 worksId 直接请求签名地址；只有
                # 列表扫描完整且签名接口也确认没有地址时，才把 ID 标成
                # 失效，避免临时接口故障触发重复合成。
                direct_url = self._fetch_sign_url_in_page(
                    page, works_id, log_result=False
                )
                if direct_url:
                    ready[works_id] = {
                        **item,
                        "record": dict(record),
                        "download_url": direct_url,
                    }
                    ready_item = ready[works_id]
            target = {
                "works_id": works_id,
                "order_no": str(record.get("orderNo") or ""),
                "works_name": str(record.get("worksName") or item.get("works_name") or ""),
                "row_index": record_indexes.get(works_id),
                "item": item,
            }
            if works_id not in ready:
                works_id_invalid = (
                    not record_found and list_scan_complete is True
                )
                result = {
                    **item,
                    "downloaded": False,
                    "error": (
                        "讯飞作品列表中未找到该 worksId，已标记为失效"
                        if works_id_invalid
                        else "作品未在下载页按 worksId 就绪"
                    ),
                }
                if works_id_invalid:
                    result["works_id_invalid"] = True
                results[works_id] = result
                _notify_batch_progress(progress_callback, {
                    "job_id": str(item.get("job_id") or ""),
                    "works_id": works_id,
                    "works_id_invalid": works_id_invalid,
                    "downloaded": False,
                    "stage": "saved",
                    "error": result["error"],
                })
                continue
            targets.append(target)

        # 先用精确 worksId 对应的签名地址下载。作品列表和签名接口已经按
        # worksId 逐条校验过，这条路径不会受浏览器下载事件乱序影响。
        browser_targets = []
        for target in targets:
            _check_cancel_requested(cancel_check)
            works_id = str(target.get("works_id") or "")
            item = target.get("item") or {}
            ready_item = ready.get(works_id) or {}
            if self._download_signed_url(
                ready_item.get("download_url"),
                item.get("output_path"),
            ):
                output_path = item.get("output_path")
                size = os.path.getsize(output_path)
                result = {
                    **item,
                    "downloaded": True,
                    "size": size,
                }
                results[works_id] = result
                _log(
                    f"[xunfei] ✅ worksId 签名下载完成 worksId={works_id} "
                    f"({size:,} bytes)"
                )
                _notify_batch_progress(progress_callback, {
                    "job_id": str(item.get("job_id") or ""),
                    "works_id": works_id,
                    "downloaded": True,
                    "stage": "downloaded",
                })
                _notify_batch_progress(progress_callback, {
                    "job_id": str(item.get("job_id") or ""),
                    "works_id": works_id,
                    "downloaded": True,
                    "stage": "saved",
                })
            else:
                browser_targets.append(target)

        # 浏览器兜底时按 API 返回的行顺序勾选，后续下载事件仍必须按唯一
        # worksName/worksId 匹配，不能把到达顺序当成作品顺序。
        browser_targets.sort(key=lambda target: (
            target.get("row_index") is None,
            target.get("row_index") if target.get("row_index") is not None else 10**9,
        ))
        if not browser_targets:
            return results

        selected, missing = self._select_download_rows(page, browser_targets)
        _check_cancel_requested(cancel_check)
        selected_targets = [
            target for target in browser_targets
            if str(target.get("works_id") or "") in selected
        ]
        for target in missing:
            works_id = str(target.get("works_id") or "")
            item = target.get("item") or {}
            result = {
                **target.get("item", {}),
                "downloaded": False,
                "error": "下载页未找到对应作品复选框",
            }
            results[works_id] = result
            _notify_batch_progress(progress_callback, {
                "job_id": str(item.get("job_id") or ""),
                "works_id": works_id,
                "downloaded": False,
                "stage": "saved",
                "error": result["error"],
            })

        if selected_targets:
            _check_cancel_requested(cancel_check)
            download_kwargs = {
                "progress_callback": progress_callback,
            }
            if cancel_check is not None:
                download_kwargs["cancel_check"] = cancel_check
            downloads = self._download_selected_rows(
                page,
                selected_targets,
                **download_kwargs,
            )
            remaining_downloads = list(downloads)
            for target in selected_targets:
                _check_cancel_requested(cancel_check)
                item = target["item"]
                works_id = str(target.get("works_id") or "")
                download_index = self._match_download_index(
                    remaining_downloads,
                    target,
                )
                # 只有本次确实只选中一条目标、且只收到一条下载时才可
                # 无歧义兜底；多条目标如果文件名不能证明归属，宁可失败
                # 也不把音频写错题目。
                if (
                    download_index is None
                    and len(selected_targets) == 1
                    and len(remaining_downloads) == 1
                ):
                    download_index = 0
                download = (
                    remaining_downloads.pop(download_index)
                    if download_index is not None
                    else None
                )
                output_path = item.get("output_path")
                downloaded = False
                if download and output_path:
                    try:
                        download.save_as(output_path)
                        downloaded = os.path.exists(output_path) and _looks_like_mp3(output_path)
                    except Exception as error:
                        _log(f"[xunfei]   保存下载文件失败 worksId={works_id}: {error}")
                if not download and remaining_downloads:
                    _log(
                        f"[xunfei]   ❌ 下载事件无法按 worksName/worksId 匹配 "
                        f"worksId={works_id}，拒绝按顺序写入"
                    )
                if downloaded:
                    size = os.path.getsize(output_path)
                    result = {
                        **item,
                        "downloaded": True,
                        "size": size,
                    }
                    results[works_id] = result
                    _log(
                        f"[xunfei] ✅ 下载页统一下载完成 worksId={works_id} "
                        f"({size:,} bytes)"
                    )
                else:
                    result = {
                        **item,
                        "downloaded": False,
                        "error": "下载页未收到本条浏览器下载文件",
                    }
                    results[works_id] = result
                    _log(f"[xunfei] ❌ 下载页统一下载失败 worksId={works_id}")
                _notify_batch_progress(progress_callback, {
                    "job_id": str(item.get("job_id") or ""),
                    "works_id": works_id,
                    "downloaded": bool(result.get("downloaded")),
                    "stage": "downloaded" if result.get("downloaded") else "saved",
                    "error": result.get("error"),
                })
                if result.get("downloaded"):
                    _notify_batch_progress(progress_callback, {
                        "job_id": str(item.get("job_id") or ""),
                        "works_id": works_id,
                        "downloaded": True,
                        "stage": "saved",
                    })

        return results
