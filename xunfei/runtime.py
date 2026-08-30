"""Playwright runtime and async orchestration for the Xunfei provider.

This module owns the dedicated Sync API worker, session state, and async
session/audio orchestration for the package-level provider API.
"""

from __future__ import annotations

import os
import atexit
import queue
import threading
import uuid
from concurrent.futures import Future
from functools import partial

from .config import PARAM_DEFAULT, OUTPUT_DIR, clamp_param
from .errors import XunfeiError, _log
from .session import XunFeiSession
from .voice_catalog import DEFAULT_FEMALE

_session = None
_session_lock = threading.Lock()
_playwright_executor = None
_playwright_executor_lock = threading.Lock()


class _DaemonSingleThreadExecutor:
    """Run Playwright Sync API calls on one daemon thread without hanging exit.

    ``asyncio`` only requires an executor-like object with ``submit`` for
    ``run_in_executor``.  ``ThreadPoolExecutor`` deliberately uses non-daemon
    workers, which kept a finished test process alive after the last assertion
    whenever a Playwright call had been made.  The browser session must still
    be serialized, but an abandoned worker must not prevent the application or
    test runner from exiting.
    """

    def __init__(self, *, thread_name: str):
        self._tasks = queue.Queue()
        self._lock = threading.Lock()
        self._shutdown = False
        self._thread = threading.Thread(
            target=self._worker,
            name=thread_name,
            daemon=True,
        )
        self._thread.start()

    def submit(self, function, *args, **kwargs):
        future = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._tasks.put((future, function, args, kwargs))
        return future

    def _worker(self):
        while True:
            task = self._tasks.get()
            if task is None:
                return
            future, function, args, kwargs = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                if cancel_futures:
                    while True:
                        try:
                            task = self._tasks.get_nowait()
                        except queue.Empty:
                            break
                        if task is not None:
                            task[0].cancel()
                self._tasks.put(None)
            thread = self._thread
        if wait and threading.current_thread() is not thread:
            thread.join()


def _get_playwright_executor():
    """返回专供 Playwright Sync API 使用的单线程执行器。"""
    global _playwright_executor
    with _playwright_executor_lock:
        if _playwright_executor is None:
            _playwright_executor = _DaemonSingleThreadExecutor(
                thread_name="xunfei-playwright",
            )
        return _playwright_executor


def _shutdown_playwright_executor(wait=True):
    """Release the dedicated worker after a browser session is closed."""
    global _playwright_executor
    with _playwright_executor_lock:
        executor = _playwright_executor
        _playwright_executor = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


atexit.register(_shutdown_playwright_executor, False)


def _notify_batch_progress(callback, payload):
    """通知批量下载进度；回调异常不能中断讯飞生成主流程。"""
    if not callable(callback):
        return
    try:
        callback(dict(payload or {}))
    except Exception as error:
        _log(f"[xunfei] 批量进度回调异常（已忽略）: {error}")


async def _run_playwright_sync(function, *args, **kwargs):
    """把同一讯飞会话的所有 Sync API 调用固定到同一个线程。"""
    import asyncio

    loop = asyncio.get_running_loop()
    call = partial(function, *args, **kwargs)
    return await loop.run_in_executor(_get_playwright_executor(), call)


def is_available():
    """检查讯飞配音模块是否可用。"""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def session_status_snapshot():
    """Return session health flags without exposing the session object.

    Capability projections run on the application thread while Playwright
    callbacks run on its dedicated executor.  Keep the global lookup and the
    per-session flags synchronized so a projection cannot combine values from
    two different lifecycle transitions.
    """
    with _session_lock:
        session = _session
    if session is None:
        return None
    getter = getattr(session, "runtime_status_snapshot", None)
    if not callable(getter):
        # Do not fall back to reading private flags without their owning lock;
        # an unknown legacy session is safer to project as unavailable than as
        # spuriously READY.
        return None
    return getter()


def _session_is_healthy(session):
    """轻量级健康检查。"""
    if session is None:
        return False
    if not getattr(session, "_logged_in", False):
        return False
    if getattr(session, "_browser_disconnected", False):
        return False
    page = getattr(session, "_page", None)
    ctx = getattr(session, "_ctx", None)
    if page is None or ctx is None:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        return False
    return True


def _discard_session_unsafe():
    global _session
    _session = None


async def ensure_session(voice_key="amanda", cancel_check=None, progress_callback=None):
    """
    确保讯飞配音浏览器会话已登录。
    如果会话不存在或已损坏，则创建并打开浏览器等待用户登录。
    """
    global _session

    if not is_available():
        raise XunfeiError("讯飞配音模块不可用，请安装 playwright")

    import asyncio

    def _locked_create():
        global _session
        with _session_lock:
            if _session is not None:
                if _session_is_healthy(_session):
                    return _session
                # A user closing the Playwright window leaves the Python
                # session object and its profile lock alive.  Drop the global
                # reference and close that stale object on this same
                # Playwright executor thread before creating a replacement.
                stale = _session
                _discard_session_unsafe()
                try:
                    stale.close()
                except Exception as error:
                    _log(f"[xunfei] 清理失效浏览器会话异常（继续重建）: {error}")
            session = XunFeiSession(voice_key=voice_key)
            # Publish the candidate while login is still in progress.  If the
            # user closes the newly opened browser before login/editor
            # readiness, the adapter can inspect the same session and mark it
            # as a safe pre-confirm handoff instead of losing the disconnect
            # signal because the normal post-login assignment was never hit.
            _session = session
            try:
                login_kwargs = {"login_timeout": 300}
                if cancel_check is not None:
                    login_kwargs["cancel_check"] = cancel_check
                if progress_callback is not None:
                    login_kwargs["progress_callback"] = progress_callback
                # Keep compatibility with injected test/legacy session
                # implementations that predate one or both observability
                # hooks. Only remove a keyword when Python explicitly reports
                # that keyword as unsupported.
                while True:
                    try:
                        session.login(**login_kwargs)
                        break
                    except TypeError as error:
                        message = str(error)
                        unsupported = next(
                            (key for key in ("progress_callback", "cancel_check")
                             if key in login_kwargs and key in message),
                            None,
                        )
                        if unsupported is None:
                            raise
                        login_kwargs.pop(unsupported, None)
            except Exception:
                try:
                    session.close()
                except Exception:
                    pass
                raise
            # 必须在线程内确认全局会话。否则两个 asyncio 任务同时首次
            # 调用 ensure_session 时，第二个任务可能在主事件循环中看见
            # 仍为空，再创建第二个 Playwright Sync 会话；这里通常已经
            # 在 login 前发布过同一个对象，赋值仍保留作显式不变量。
            _session = session
            return session

    # Playwright Sync API 的所有 page/context 操作（包括健康检查）都必须
    # 留在同一个专用线程，不能在 FastAPI/asyncio 事件循环线程或其它
    # 默认线程池线程调用，否则会触发 greenlet 跨线程异常。
    return await _run_playwright_sync(_locked_create)


async def synth_xunfei(
    text, voice_key="amanda",
    speed=PARAM_DEFAULT, pitch=PARAM_DEFAULT, volume=PARAM_DEFAULT,
):
    """
    用讯飞配音生成一条音频，返回 pydub.AudioSegment。

    Args:
        text: 要合成的文本
        voice_key: 发音人 key（"amanda"/"george"）
        speed/pitch/volume: 讯飞平台三参数（0-100，50=默认）

    Returns:
        pydub.AudioSegment 音频段

    Raises:
        XunfeiQuotaExceeded / XunfeiRateLimited / XunfeiLoginRequired / XunfeiError
    """
    import asyncio

    if not is_available():
        raise XunfeiError("讯飞配音模块不可用，请安装 playwright")

    session = await ensure_session(voice_key=voice_key)

    import uuid
    output_name = f".xunfei_{uuid.uuid4().hex[:8]}.mp3"

    result_path = await _run_playwright_sync(
        session.synth_one,
        text,
        output_name,
        4,          # max_retries
        voice_key,
        clamp_param(speed),
        clamp_param(pitch),
        clamp_param(volume),
    )

    if not result_path or not os.path.exists(result_path):
        raise XunfeiError(f"讯飞配音未生成音频文件: {result_path}")

    fsize = os.path.getsize(result_path)
    _log(f"[xunfei] 生成完成: {result_path} ({fsize} bytes)")
    if fsize < 100:
        raise XunfeiError(f"讯飞配音返回的音频过小 ({fsize} bytes)")

    from pydub import AudioSegment
    seg = AudioSegment.from_file(result_path, format="mp3", codec="mp3")
    dur_ms = len(seg)
    _log(f"[xunfei] pydub 解码完成: duration={dur_ms}ms channels={seg.channels} sample_rate={seg.frame_rate}")
    if dur_ms < 50:
        raise XunfeiError(f"解码后音频时长过短 ({dur_ms}ms)")
    if seg.channels == 0 or seg.frame_rate == 0:
        raise XunfeiError(f"解码后音频参数异常 (channels={seg.channels}, frame_rate={seg.frame_rate})")

    # 清理临时文件
    try:
        os.remove(result_path)
    except OSError:
        pass
    return seg


async def synth_xunfei_batch(jobs, progress_callback=None, cancel_check=None):
    """批量讯飞合成：按音色/参数分组提交，最后统一下载并解码。

    ``jobs`` 中每项至少包含 ``job_id``、``text``、``voice_key``、``speed``、
    ``pitch``、``volume``。返回 ``job_id -> {segment, error}``，其中 segment
    是已解码的 pydub.AudioSegment。Playwright 的 Sync API 仍全部固定在同一
    专用线程内，避免 asyncio loop 与 greenlet 跨线程冲突。下载回调只
    汇报 job 状态，不得在回调中直接操作 Playwright。
    """
    if not jobs:
        return {}
    if not is_available():
        raise XunfeiError("讯飞配音模块不可用，请安装 playwright")

    import asyncio
    first_voice = str((jobs[0] or {}).get("voice_key") or DEFAULT_FEMALE)
    session = await ensure_session(
        voice_key=first_voice,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    normalized_jobs = []
    for index, job in enumerate(jobs):
        item = dict(job)
        item.setdefault("job_id", f"batch-{index}")
        item.setdefault("output_name", f".xunfei_{uuid.uuid4().hex}.mp3")
        # 讯飞下载页文件名不带 worksId；为本批次每段设置短且唯一的作品名，
        # 既便于页面核对，也避免同名作品的浏览器下载事件无法区分。
        item.setdefault("works_name", f"wordtts_{index + 1:04d}_{uuid.uuid4().hex[:8]}")
        normalized_jobs.append(item)

    batch_kwargs = {"progress_callback": progress_callback}
    if cancel_check is not None:
        batch_kwargs["cancel_check"] = cancel_check
    raw_results = await _run_playwright_sync(
        session.synth_batch,
        normalized_jobs,
        4,
        **batch_kwargs,
    )
    decoded = {}
    from pydub import AudioSegment

    for job in normalized_jobs:
        job_id = str(job["job_id"])
        result = raw_results.get(job_id) if isinstance(raw_results, dict) else None
        if not isinstance(result, dict) or not result.get("downloaded"):
            decoded[job_id] = {
                "segment": None,
                "works_id": (result or {}).get("works_id") if isinstance(result, dict) else None,
                "works_id_invalid": (
                    bool(result.get("works_id_invalid"))
                    if isinstance(result, dict)
                    else False
                ),
                "ambiguous_works_id": (
                    bool(result.get("ambiguous_works_id"))
                    if isinstance(result, dict)
                    else False
                ),
                "works_name": (
                    result.get("works_name")
                    if isinstance(result, dict)
                    else None
                ),
                "error": (result or {}).get("error", "讯飞批量下载失败")
                if isinstance(result, dict) else "讯飞批量生成无结果",
            }
            continue

        path = result.get("output_path")
        try:
            if not path or not os.path.exists(path):
                raise XunfeiError(f"讯飞批量音频文件不存在: {path}")
            size = os.path.getsize(path)
            if size < 100:
                raise XunfeiError(f"讯飞批量音频文件过小: {size} bytes")

            def decode_file(source_path=path):
                return AudioSegment.from_file(source_path, format="mp3", codec="mp3")

            seg = await asyncio.to_thread(decode_file)
            if len(seg) < 50:
                raise XunfeiError(f"解码后音频时长过短 ({len(seg)}ms)")
            if seg.channels == 0 or seg.frame_rate == 0:
                raise XunfeiError(
                    f"解码后音频参数异常 (channels={seg.channels}, frame_rate={seg.frame_rate})"
                )
            _log(
                f"[xunfei] 批量音频解码完成 job={job_id}: "
                f"duration={len(seg)}ms size={size:,} bytes"
            )
            decoded[job_id] = {"segment": seg, "error": None}
        except Exception as error:
            decoded[job_id] = {"segment": None, "error": str(error)}
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return decoded


async def synth_xunfei_composite(
    works,
    progress_callback=None,
    resume=None,
    cancel_check=None,
):
    """用讯飞多人配音接口生成合并作品并解码完整 MP3。"""
    if not works:
        return {}
    if not is_available():
        raise XunfeiError("讯飞配音模块不可用，请安装 playwright")

    import asyncio
    first_voice = DEFAULT_FEMALE
    try:
        first_voice = str(
            (works[0].get("items") or [])[0].get("segments", [])[0].get("voice_key")
            or DEFAULT_FEMALE
        )
    except (AttributeError, IndexError, TypeError):
        pass
    session = await ensure_session(
        voice_key=first_voice,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    normalized_works = []
    for index, work in enumerate(works, start=1):
        item = dict(work)
        item.setdefault("work_id", f"composite:{index}")
        item.setdefault("job_id", item["work_id"])
        item.setdefault("output_name", f".xunfei_composite_{uuid.uuid4().hex}.mp3")
        item.setdefault(
            "works_name",
            f"wordtts_composite_{index:04d}_{uuid.uuid4().hex[:8]}",
        )
        normalized_works.append(item)

    composite_kwargs = {
        "progress_callback": progress_callback,
        "resume": resume,
    }
    if cancel_check is not None:
        composite_kwargs["cancel_check"] = cancel_check
    raw_results = await _run_playwright_sync(
        session.synth_composite,
        normalized_works,
        4,
        **composite_kwargs,
    )
    decoded = {}
    from pydub import AudioSegment

    for work in normalized_works:
        work_id = str(work["work_id"])
        result = raw_results.get(work_id) if isinstance(raw_results, dict) else None
        if not isinstance(result, dict) or not result.get("downloaded"):
            decoded[work_id] = {
                "audio": None,
                "works_id": (result or {}).get("works_id") if isinstance(result, dict) else None,
                "ambiguous_works_id": (
                    bool(result.get("ambiguous_works_id"))
                    if isinstance(result, dict)
                    else False
                ),
                "works_name": (
                    result.get("works_name")
                    if isinstance(result, dict)
                    else None
                ),
                "works_id_invalid": (
                    bool(result.get("works_id_invalid"))
                    if isinstance(result, dict)
                    else False
                ),
                "error": (result or {}).get("error", "讯飞多人配音下载失败")
                if isinstance(result, dict) else "讯飞多人配音生成无结果",
            }
            continue

        path = result.get("output_path")
        try:
            if not path or not os.path.exists(path):
                raise XunfeiError(f"讯飞多人配音音频文件不存在: {path}")
            size = os.path.getsize(path)
            if size < 100:
                raise XunfeiError(f"讯飞多人配音音频文件过小: {size} bytes")

            def decode_file(source_path=path):
                return AudioSegment.from_file(source_path, format="mp3", codec="mp3")

            audio = await asyncio.to_thread(decode_file)
            if len(audio) < 50:
                raise XunfeiError(f"多人配音解码后音频过短 ({len(audio)}ms)")
            if audio.channels == 0 or audio.frame_rate == 0:
                raise XunfeiError(
                    f"多人配音解码后音频参数异常 (channels={audio.channels}, frame_rate={audio.frame_rate})"
                )
            decoded[work_id] = {
                "audio": audio,
                "works_id": result.get("works_id"),
                "error": None,
            }
            _log(
                f"[xunfei] 多人配音完整作品解码完成 work={work_id}: "
                f"duration={len(audio)}ms size={size:,} bytes"
            )
        except Exception as error:
            decoded[work_id] = {
                "audio": None,
                "works_id": result.get("works_id"),
                "works_id_invalid": bool(result.get("works_id_invalid")),
                "ambiguous_works_id": bool(result.get("ambiguous_works_id")),
                "works_name": result.get("works_name"),
                "error": str(error),
            }
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return decoded


async def close_session():
    """关闭讯飞配音浏览器会话。"""
    global _session

    with _session_lock:
        old = _session
        _session = None
    try:
        if old is not None:
            await _run_playwright_sync(old.close)
            _log("[xunfei] 浏览器会话已关闭")
    except Exception as e:
        _log(f"[xunfei] 关闭浏览器会话异常: {e}")
    finally:
        _shutdown_playwright_executor()
