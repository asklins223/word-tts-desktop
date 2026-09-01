"""Stable errors and cancellation helpers for the Xunfei provider."""

from __future__ import annotations

import sys
import time


def _log(*args, **kwargs):
    """所有日志输出到 stdout/stderr，确保 Electron 后端能捕获。"""
    kwargs.setdefault("file", sys.stdout)
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


class XunfeiError(RuntimeError):
    """讯飞配音引擎错误基类。"""


class XunfeiQuotaExceeded(XunfeiError):
    """额度不足：上层应立即停止整批任务。"""


class XunfeiRateLimited(XunfeiError):
    """触发频控：上层应拉长节奏后重试。"""


class XunfeiLoginRequired(XunfeiError):
    """会话失效：需要人工重新扫码登录。"""


class XunfeiBrowserLaunchError(XunfeiError):
    """Playwright driver or the visible Chromium context could not start."""

    def __init__(self, message, *, phase="context_launch", details=None):
        super().__init__(message)
        self.phase = str(phase or "context_launch")[:64]
        self.details = dict(details) if isinstance(details, dict) else {}


class XunfeiSubmissionAmbiguous(XunfeiError):
    """页面提交后未拿到本地 worksId，调用方应重新生成。"""

    def __init__(self, message, works_name=None):
        super().__init__(message)
        self.works_name = str(works_name or "").strip() or None
        self.submission_confirmed = True


class XunfeiCancelled(XunfeiError):
    """批量任务被上层取消，停止后续提交/下载。"""


def _check_cancel_requested(cancel_check):
    """执行可选取消探针；探针自身异常不能误杀正常合成。"""
    if not callable(cancel_check):
        return
    try:
        cancelled = bool(cancel_check())
    except Exception:
        cancelled = False
    if cancelled:
        raise XunfeiCancelled("讯飞批量任务已取消，已停止后续提交")


def _check_page_open(page):
    """Raise a typed cancellation when Playwright reports a closed page."""

    if page is None:
        return
    try:
        is_closed = getattr(page, "is_closed", None)
        # ``MagicMock``/旧测试桩返回的对象不能按 truthiness 判断，否则会
        # 被误认为已关闭；真实 Playwright API 返回精确 bool。
        if callable(is_closed) and is_closed() is True:
            raise XunfeiCancelled("讯飞浏览器页面已关闭，已停止当前操作")
    except XunfeiCancelled:
        raise
    except Exception:
        # 页面状态探针本身失败时，让后续 Playwright 调用给出原始错误；
        # 这里不能把短暂的探针异常误判成用户关闭浏览器。
        return


def _wait_with_cancel(page, seconds, cancel_check=None):
    """Wait in short Playwright slices so close/cancel becomes responsive."""

    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _check_cancel_requested(cancel_check)
        _check_page_open(page)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        delay_ms = max(1, min(250, int(remaining * 1000)))
        try:
            if page is not None:
                page.wait_for_timeout(delay_ms)
            else:
                time.sleep(delay_ms / 1000)
        except Exception:
            _check_page_open(page)
            raise


def _notify_runtime_progress(callback, *, stage, message, **extra):
    """Send redacted browser lifecycle progress without affecting generation."""

    if not callable(callback):
        return
    payload = {
        "stage": str(stage)[:64],
        "status": str(stage)[:64],
        "message": " ".join(str(message or "").split())[:500],
    }
    for key in ("item_id", "work_id", "works_name"):
        if extra.get(key) not in (None, ""):
            payload[key] = str(extra[key])[:256]
    try:
        callback(payload)
    except Exception as error:
        _log(f"[xunfei] 浏览器进度回调异常（已忽略）: {error}")
