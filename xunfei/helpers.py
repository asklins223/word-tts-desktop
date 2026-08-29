"""Small, side-effect-free helpers shared by Xunfei page components."""

from __future__ import annotations

import os
import re
import time

from .errors import _check_cancel_requested, _check_page_open, _log


def poll(
    check_fn,
    timeout,
    interval=0.5,
    page=None,
    max_interval=None,
    cancel_check=None,
):
    """轮询等待 check_fn 返回 truthy；自适应退避但保留延迟页面兜底。"""
    deadline = time.monotonic() + max(0, float(timeout))
    current_interval = max(0.05, float(interval))
    upper_interval = max(current_interval, float(max_interval or current_interval * 2.5))
    while True:
        _check_cancel_requested(cancel_check)
        _check_page_open(page)
        try:
            result = check_fn()
            if result:
                return result
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # 轻微抖动避免固定节奏，同时将高频 DOM 探测逐步降到低频。
        sleep_s = min(
            current_interval * (0.9 + (time.monotonic() % 0.2)),
            remaining,
        )
        if page is not None:
            try:
                page.wait_for_timeout(int(sleep_s * 1000))
            except Exception:
                _check_page_open(page)
                raise
        else:
            time.sleep(sleep_s)
        current_interval = min(upper_interval, current_interval * 1.35)
    return None


def safe_eval(page, script, arg=None):
    """Evaluate a page script while treating a closed page as no result."""
    try:
        if arg is not None:
            return page.evaluate(script, arg)
        return page.evaluate(script)
    except Exception:
        return None


def looks_like_mp3(path):
    """通过文件头做轻量 MP3 格式检查。"""
    try:
        with open(path, "rb") as stream:
            head = stream.read(4)
    except OSError:
        return False
    if len(head) < 2:
        return False
    if head[:3] == b"ID3":
        return True
    return head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def normalize_download_label(value):
    """规范化作品名/浏览器文件名，供下载兜底匹配使用。"""
    name = os.path.basename(str(value or "")).strip()
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", name).casefold()


def notify_batch_progress(callback, payload):
    """通知批量下载进度；回调异常不能中断讯飞生成主流程。"""
    if not callable(callback):
        return
    try:
        callback(dict(payload or {}))
    except Exception as error:
        _log(f"[xunfei] 批量进度回调异常（已忽略）: {error}")
