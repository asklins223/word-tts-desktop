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
from contextlib import asynccontextmanager
from concurrent.futures import Future
from functools import partial, wraps

from .config import PARAM_DEFAULT, OUTPUT_DIR, clamp_param
from .errors import (
    XunfeiCancelled,
    XunfeiError,
    XunfeiLoginRequired,
    _check_cancel_requested,
    _log,
)
from .session import XunFeiSession
from .voice_catalog import DEFAULT_FEMALE

_session = None
_session_lock = threading.Lock()
_playwright_executor = None
_playwright_executor_lock = threading.Lock()
_executor_rotated_session = None
_generation_slot_lock = threading.Lock()
_closing_session = None
_orphaned_close_session = None
_orphaned_close_profile_owner_pid = None
# ``Page.is_closed()`` is backed by Playwright's local lifecycle state.  A
# user closing the browser can reach a retry before that callback is pumped,
# so reuse also needs one small, bounded protocol round trip.
_SESSION_TRANSPORT_PROBE_TIMEOUT_MS = 1_500
_close_timer = None
_close_timer_lock = threading.Lock()
_close_timer_generation = 0
_LOGIN_RECOVERY_CLOSE_DELAY_SECONDS = 180.0
_GENERATION_SLOT_POLL_SECONDS = 0.05
# A normal persistent-context close can legitimately spend a few seconds
# releasing Chrome's profile lock.  It must not, however, leave a retry behind
# an indefinitely blocked Sync API worker after the visible browser vanished.
_SESSION_CLOSE_TIMEOUT_SECONDS = 5.0


class _SessionRebuildRequired(RuntimeError):
    """Ask the async caller to rotate the worker before rebuilding a session."""

    def __init__(self, session, *, profile_owner_pid=None):
        super().__init__("the existing Xunfei session is no longer usable")
        self.session = session
        self.profile_owner_pid = profile_owner_pid


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


def _shutdown_playwright_executor(wait=True, *, expected_executor=None):
    """Retire the dedicated worker after disconnect recovery or at exit."""
    global _playwright_executor
    with _playwright_executor_lock:
        executor = _playwright_executor
        if expected_executor is not None and executor is not expected_executor:
            return False
        _playwright_executor = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)
        return True
    return False


def _rotate_playwright_executor(disconnected_session=None, *, expected_executor=None):
    """Detach a stuck browser worker so a replacement session can start.

    Playwright's synchronous transport can occasionally remain blocked after
    the user closes Chrome while a page operation is in flight.  Waiting for
    that daemon thread would leave every later ``ensure_session`` call queued
    behind the dead page, so retire only the executor object and let its
    already-running daemon finish on its own.  The replacement session gets a
    fresh thread and never touches the old Playwright objects.
    """

    global _executor_rotated_session
    if disconnected_session is not None:
        # Several callers can observe the same close callback before the
        # first replacement has finished.  Rotate once for that exact stale
        # session; otherwise concurrent ensure_session calls could retire the
        # fresh executor that the first caller just created.
        with _session_lock:
            if _executor_rotated_session is disconnected_session:
                return False
            _executor_rotated_session = disconnected_session
    return _shutdown_playwright_executor(
        wait=False,
        expected_executor=expected_executor,
    )


def _session_browser_disconnected():
    """Return the lifecycle flag without probing thread-affine Playwright APIs."""

    return _disconnected_session() is not None


def _disconnected_session():
    """Return the current stale session, if its lifecycle callback fired."""

    with _session_lock:
        session = _session
    if session is None:
        return None
    getter = getattr(session, "runtime_status_snapshot", None)
    if not callable(getter):
        return None
    try:
        status = getter()
    except Exception:
        return None
    return session if isinstance(status, dict) and bool(status.get("browser_disconnected")) else None


def _remember_orphaned_close(session, *, profile_owner_pid):
    """Keep profile-recovery facts when a close call cannot finish in time."""

    global _orphaned_close_session, _orphaned_close_profile_owner_pid
    with _session_lock:
        _orphaned_close_session = session
        _orphaned_close_profile_owner_pid = profile_owner_pid


def _release_closing_session(session):
    """Let a retry proceed once this close no longer owns the session fence."""

    global _closing_session
    with _session_lock:
        if _closing_session is session:
            _closing_session = None


def _settle_session_close(session):
    """Clear close bookkeeping after the original worker eventually returns."""

    global _closing_session, _orphaned_close_session, _orphaned_close_profile_owner_pid
    with _session_lock:
        if _closing_session is session:
            _closing_session = None
        if _orphaned_close_session is session:
            _orphaned_close_session = None
            _orphaned_close_profile_owner_pid = None


async def _wait_for_session_close(cancel_check=None):
    """Keep retries out of a worker that is still closing an older session."""

    import asyncio

    while True:
        with _session_lock:
            closing = _closing_session
        if closing is None:
            return
        _check_cancel_requested(cancel_check)
        await asyncio.sleep(_GENERATION_SLOT_POLL_SECONDS)


atexit.register(_shutdown_playwright_executor, False)


def _notify_batch_progress(callback, payload):
    """通知批量下载进度；回调异常不能中断讯飞生成主流程。"""
    if not callable(callback):
        return
    try:
        callback(dict(payload or {}))
    except Exception as error:
        _log(f"[xunfei] 批量进度回调异常（已忽略）: {error}")


def _submit_playwright_sync(function, *args, playwright_executor=None, **kwargs):
    """Submit a thread-affine Sync API call and retain its worker identity."""

    executor = playwright_executor or _get_playwright_executor()
    call = partial(function, *args, **kwargs)
    return executor, executor.submit(call)


async def _run_playwright_sync(function, *args, playwright_executor=None, **kwargs):
    """把同一讯飞会话的所有 Sync API 调用固定到同一个线程。"""
    import asyncio

    loop = asyncio.get_running_loop()
    _executor, future = _submit_playwright_sync(
        function,
        *args,
        playwright_executor=playwright_executor,
        **kwargs,
    )
    return await asyncio.wrap_future(future, loop=loop)


@asynccontextmanager
async def _generation_slot(cancel_check=None):
    """Serialize complete browser generations across event loops and threads.

    The browser session is process-global.  Without this fence, a retry can
    reuse the session while an older task is entering cleanup, then have its
    browser closed by that older task.  Non-blocking polling keeps the fence
    cross-loop without ever blocking an asyncio event loop, and preserves the
    caller's normal cancellation behaviour while waiting.
    """
    import asyncio

    while not _generation_slot_lock.acquire(blocking=False):
        _check_cancel_requested(cancel_check)
        await asyncio.sleep(_GENERATION_SLOT_POLL_SECONDS)
    try:
        _check_cancel_requested(cancel_check)
        yield
    finally:
        _generation_slot_lock.release()


def _serialize_generation(*, cancel_check_position):
    """Apply the process-wide browser-generation fence to a public coroutine."""

    def decorator(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            if "cancel_check" in kwargs:
                cancel_check = kwargs["cancel_check"]
            elif len(args) > cancel_check_position:
                cancel_check = args[cancel_check_position]
            else:
                cancel_check = None
            async with _generation_slot(cancel_check):
                return await function(*args, **kwargs)

        return wrapped

    return decorator


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
    """Return whether a logged-in session can still serve Playwright calls.

    ``page.is_closed()`` alone is not enough here: it can still be ``False``
    for a short interval after the visible browser window has gone away and
    before Playwright delivers its close callback.  A retry in that interval
    previously reused the dead page and never entered the browser-start path.
    The root-locator evaluation is a no-op page operation, but it forces a
    bounded transport round trip on the session's owning Playwright thread.
    """
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
            _mark_session_disconnected_for_recovery(session)
            return False
        # ``Locator.evaluate`` accepts an operation timeout (unlike
        # ``Page.evaluate``) and does not alter the page.  It catches both a
        # delayed browser-close event and a dead Sync API transport.
        connected = page.locator("html").evaluate(
            "node => node.isConnected",
            timeout=_SESSION_TRANSPORT_PROBE_TIMEOUT_MS,
        )
        if not connected:
            _mark_session_disconnected_for_recovery(session)
            return False
    except Exception as error:
        _mark_session_disconnected_for_recovery(session)
        _log(f"[xunfei] 浏览器会话活动探测失败，将重建: {error}")
        return False
    return True


def _mark_session_disconnected_for_recovery(session):
    """Promote a failed transport probe to the session lifecycle state.

    A failed probe means the old Playwright object is unsafe to close through
    the same transport.  The real session exposes a locked lifecycle marker;
    legacy/test doubles without it keep the old best-effort close behavior.
    """

    marker = getattr(session, "_mark_browser_disconnected", None)
    if not callable(marker):
        return
    try:
        marker()
    except Exception:
        pass


def _discard_session_unsafe():
    global _session
    _session = None


async def ensure_session(voice_key="amanda", cancel_check=None, progress_callback=None):
    """
    确保讯飞配音浏览器会话已登录。
    如果会话不存在或已损坏，则创建并打开浏览器等待用户登录。
    """
    global _session, _orphaned_close_session, _orphaned_close_profile_owner_pid
    # A close runs on the same serial Sync API worker.  Do not queue a fresh
    # login behind it: if it reaches the bounded recovery path below, this
    # caller must instead submit to the replacement worker.
    await _wait_for_session_close(cancel_check)
    _cancel_auto_close()

    if not is_available():
        raise XunfeiError("讯飞配音模块不可用，请安装 playwright")

    # If Chrome already reported a disconnect, the previous sync call may be
    # stuck inside Playwright rather than merely waiting in the queue.  A
    # replacement must not be submitted to that same executor or its browser
    # window will never appear.
    disconnected_session = _disconnected_session()
    if disconnected_session is not None:
        _rotate_playwright_executor(disconnected_session)

    def _locked_create():
        global _session, _orphaned_close_session, _orphaned_close_profile_owner_pid
        stale = None
        with _session_lock:
            orphaned_close = _orphaned_close_session
            orphaned_close_pid = _orphaned_close_profile_owner_pid
            reclaim_owner = orphaned_close
            reclaim_owner_pid = orphaned_close_pid
            if _session is not None:
                current_status = None
                status_getter = getattr(_session, "runtime_status_snapshot", None)
                if callable(status_getter):
                    try:
                        current_status = status_getter()
                    except Exception:
                        current_status = None
                current_disconnected = bool(
                    isinstance(current_status, dict)
                    and current_status.get("browser_disconnected")
                )
                # Do not run a thread-affine page health check on a new
                # executor after rotation.  The old session belongs to the
                # retired worker and is intentionally discarded below.
                replace_current = (
                    disconnected_session is not None
                    and _session is disconnected_session
                )
                # A logged-in session with a page/context is expected to be
                # reusable. If its active transport probe fails, calling
                # ``stale.close()`` here can block on the same dead Sync API
                # worker that we need for the retry. Defer that cleanup to the
                # async side, where it can rotate this exact worker first.
                transport_probe_candidate = (
                    bool(getattr(_session, "_logged_in", False))
                    and getattr(_session, "_page", None) is not None
                    and getattr(_session, "_ctx", None) is not None
                )
                healthy = (
                    not current_disconnected
                    and not replace_current
                    and _session_is_healthy(_session)
                )
                current_disconnected = current_disconnected or bool(
                    getattr(_session, "_browser_disconnected", False)
                )
                if healthy and not current_disconnected:
                    # A healthy, already-published replacement makes an older
                    # timed-out close irrelevant; do not carry its profile PID
                    # into a later unrelated launch.
                    if _orphaned_close_session is orphaned_close:
                        _orphaned_close_session = None
                        _orphaned_close_profile_owner_pid = None
                    return _session
                # A user closing the Playwright window leaves the Python
                # session object and its profile lock alive. Drop the global
                # reference before creating a replacement. Transport-stale
                # sessions are handed back to the async side so their worker
                # can be retired without waiting on a potentially blocked
                # ``close()`` call.
                stale = _session
                _discard_session_unsafe()
                stale_profile_owner_pid = getattr(stale, "_profile_owner_pid", None)
                if (
                    current_disconnected
                    or replace_current
                    or transport_probe_candidate
                ):
                    reclaim_owner = stale
                    reclaim_owner_pid = stale_profile_owner_pid
                    raise _SessionRebuildRequired(
                        stale,
                        profile_owner_pid=stale_profile_owner_pid,
                    )
                else:
                    try:
                        stale.close()
                    except Exception as error:
                        _log(f"[xunfei] 清理失效浏览器会话异常（继续重建）: {error}")
            session = XunFeiSession(voice_key=voice_key)
            if reclaim_owner is not None and reclaim_owner_pid is not None:
                # The stale object may still own the profile from a blocked
                # old worker.  Let the new session reclaim only that known
                # recovery path; it must never kill an unrelated browser on a
                # normal first launch.
                try:
                    session._reclaim_profile_owner = True
                    session._reclaim_profile_owner_pid = reclaim_owner_pid
                except Exception:
                    pass
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
            if _orphaned_close_session is orphaned_close:
                _orphaned_close_session = None
                _orphaned_close_profile_owner_pid = None
            return session

    # Playwright Sync API 的所有 page/context 操作（包括健康检查）都必须
    # 留在同一个专用线程，不能在 FastAPI/asyncio 事件循环线程或其它
    # 默认线程池线程调用，否则会触发 greenlet 跨线程异常。
    executor = _get_playwright_executor()
    try:
        return await _run_playwright_sync(
            _locked_create,
            playwright_executor=executor,
        )
    except _SessionRebuildRequired as rebuild:
        # The stale object is intentionally not closed on the old worker: that
        # call is exactly what can hang after a visible browser disconnect.
        # Keep the PID captured before abandoning it, rotate only the worker
        # that ran the failed probe, and let the fresh session reclaim that
        # exact profile owner before launch.
        _remember_orphaned_close(
            rebuild.session,
            profile_owner_pid=rebuild.profile_owner_pid,
        )
        _rotate_playwright_executor(
            rebuild.session,
            expected_executor=executor,
        )
        return await _run_playwright_sync(_locked_create)


@_serialize_generation(cancel_check_position=5)
async def synth_xunfei(
    text, voice_key="amanda",
    speed=PARAM_DEFAULT, pitch=PARAM_DEFAULT, volume=PARAM_DEFAULT,
    cancel_check=None, progress_callback=None,
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

    _check_cancel_requested(cancel_check)
    session = await ensure_session(
        voice_key=voice_key,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )

    import uuid
    output_name = f".xunfei_{uuid.uuid4().hex[:8]}.mp3"
    result_path = None

    synth_kwargs = {}
    if callable(cancel_check):
        synth_kwargs["cancel_check"] = cancel_check
    if callable(progress_callback):
        synth_kwargs["progress_callback"] = progress_callback
    login_recovery = False
    try:
        result_path = await _run_playwright_sync(
            session.synth_one,
            text,
            output_name,
            4,          # max_retries
            voice_key,
            clamp_param(speed),
            clamp_param(pitch),
            clamp_param(volume),
            **synth_kwargs,
        )

        _check_cancel_requested(cancel_check)

        if not result_path or not os.path.exists(result_path):
            raise XunfeiError(f"讯飞配音未生成音频文件: {result_path}")

        fsize = os.path.getsize(result_path)
        _log(f"[xunfei] 生成完成: {result_path} ({fsize} bytes)")
        if fsize < 100:
            raise XunfeiError(f"讯飞配音返回的音频过小 ({fsize} bytes)")

        from pydub import AudioSegment
        seg = AudioSegment.from_file(result_path, format="mp3", codec="mp3")
        _check_cancel_requested(cancel_check)
        dur_ms = len(seg)
        _log(f"[xunfei] pydub 解码完成: duration={dur_ms}ms channels={seg.channels} sample_rate={seg.frame_rate}")
        if dur_ms < 50:
            raise XunfeiError(f"解码后音频时长过短 ({dur_ms}ms)")
        if seg.channels == 0 or seg.frame_rate == 0:
            raise XunfeiError(f"解码后音频参数异常 (channels={seg.channels}, frame_rate={seg.frame_rate})")

        return seg
    except XunfeiLoginRequired:
        login_recovery = True
        raise
    finally:
        # 生成成功、解码失败或用户取消都不能把临时 MP3 留在数据目录。
        if result_path:
            try:
                os.remove(result_path)
            except OSError:
                pass
        await _finish_generation_session(
            session,
            cancel_check,
            login_recovery=login_recovery,
        )


@_serialize_generation(cancel_check_position=2)
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
    login_recovery = False
    try:
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
            _check_cancel_requested(cancel_check)
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
                _check_cancel_requested(cancel_check)
                if not path or not os.path.exists(path):
                    raise XunfeiError(f"讯飞批量音频文件不存在: {path}")
                size = os.path.getsize(path)
                if size < 100:
                    raise XunfeiError(f"讯飞批量音频文件过小: {size} bytes")

                def decode_file(source_path=path):
                    return AudioSegment.from_file(source_path, format="mp3", codec="mp3")

                seg = await asyncio.to_thread(decode_file)
                _check_cancel_requested(cancel_check)
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
            except XunfeiCancelled:
                raise
            except Exception as error:
                decoded[job_id] = {"segment": None, "error": str(error)}
            finally:
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        return decoded
    except XunfeiLoginRequired:
        login_recovery = True
        raise
    finally:
        await _finish_generation_session(
            session,
            cancel_check,
            login_recovery=login_recovery,
        )


@_serialize_generation(cancel_check_position=3)
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
    login_recovery = False
    try:
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
            _check_cancel_requested(cancel_check)
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
                _check_cancel_requested(cancel_check)
                if not path or not os.path.exists(path):
                    raise XunfeiError(f"讯飞多人配音音频文件不存在: {path}")
                size = os.path.getsize(path)
                if size < 100:
                    raise XunfeiError(f"讯飞多人配音音频文件过小: {size} bytes")

                def decode_file(source_path=path):
                    return AudioSegment.from_file(source_path, format="mp3", codec="mp3")

                audio = await asyncio.to_thread(decode_file)
                _check_cancel_requested(cancel_check)
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
            except XunfeiCancelled:
                raise
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
    except XunfeiLoginRequired:
        login_recovery = True
        raise
    finally:
        await _finish_generation_session(
            session,
            cancel_check,
            login_recovery=login_recovery,
        )


def _cancel_auto_close():
    global _close_timer, _close_timer_generation
    with _close_timer_lock:
        _close_timer_generation += 1
        if _close_timer is not None:
            try:
                _close_timer.cancel()
            except Exception:
                pass
            _close_timer = None


def _schedule_auto_close(expected_session=None, *, delay_seconds):
    global _close_timer, _close_timer_generation
    delay = float(delay_seconds)
    with _close_timer_lock:
        # A completion callback from an older session must never replace the
        # close timer belonging to a newer session.
        with _session_lock:
            current_session = _session
        if expected_session is None:
            expected_session = current_session
        elif current_session is not expected_session:
            return
        if _close_timer is not None:
            try:
                _close_timer.cancel()
            except Exception:
                pass
        _close_timer_generation += 1
        timer_generation = _close_timer_generation
        # 捕获调度时的主事件循环，定时器线程通过 call_soon_threadsafe 调度关闭
        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        import threading as _threading
        def _do_close():
            import asyncio as _asyncio
            with _close_timer_lock:
                if timer_generation != _close_timer_generation:
                    return
            # 定时器在独立线程触发，需将协程调度回主循环
            close_kwargs = {
                "expected_session": expected_session,
                "expected_generation": timer_generation,
            }
            if loop is not None and loop.is_running() and not loop.is_closed():
                close_coro = close_session(**close_kwargs)
                try:
                    _asyncio.run_coroutine_threadsafe(close_coro, loop)
                except Exception as e:
                    # If the loop closes in the small window between the
                    # checks and submission, close the unscheduled coroutine
                    # before falling back to a private loop. Otherwise Python
                    # reports an unawaited-coroutine warning during shutdown.
                    close_coro.close()
                    _log(f"[xunfei] 自动关闭调度异常: {e}")
                else:
                    return
            # 无主循环或调度失败时直接新建循环执行
            try:
                _asyncio.run(close_session(**close_kwargs))
            except Exception as e:
                _log(f"[xunfei] 自动关闭浏览器异常: {e}")

        _close_timer = _threading.Timer(delay, _do_close)
        _close_timer.daemon = True
        _close_timer.start()


async def _finish_generation_session(
    session,
    cancel_check=None,
    *,
    login_recovery=False,
):
    """Keep the browser open for recovery only when login actually expired."""

    cancelled = False
    if callable(cancel_check):
        try:
            cancelled = bool(cancel_check())
        except Exception:
            # A diagnostic/control callback must not turn a successful
            # generation into a failed cleanup path.
            cancelled = False
    if cancelled or not login_recovery:
        # Cancellation, successful delivery, and every non-login failure all
        # close the browser immediately. Login recovery is the only exception.
        await close_session(expected_session=session)
    elif login_recovery:
        # A login-expired page is the sole recovery exception: leave its
        # visible window around long enough for the user to sign in, then
        # close it with the same expected-session fence.
        _schedule_auto_close(
            expected_session=session,
            delay_seconds=_LOGIN_RECOVERY_CLOSE_DELAY_SECONDS,
        )


async def close_session(*, expected_session=None, expected_generation=None):
    """关闭讯飞配音浏览器会话。"""
    global _session, _closing_session, _close_timer, _close_timer_generation

    with _close_timer_lock:
        if (
            expected_generation is not None
            and expected_generation != _close_timer_generation
        ):
            return
        with _session_lock:
            if expected_session is not None and _session is not expected_session:
                return
            old = _session
            _session = None
            old_profile_owner_pid = (
                getattr(old, "_profile_owner_pid", None)
                if old is not None
                else None
            )
            if old is not None:
                _closing_session = old
        _close_timer_generation += 1
        timer = _close_timer
        _close_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
    if old is None:
        return

    import asyncio

    executor = None
    try:
        # Capture the exact worker before submission. If a concurrent recovery
        # retires it in the narrow submit window, error handling below must
        # never rotate a freshly-created replacement by mistake.
        executor = _get_playwright_executor()
        _, close_future = _submit_playwright_sync(
            old.close,
            playwright_executor=executor,
        )
        # The daemon worker can finish after this coroutine's event loop has
        # gone away. Clear recovery metadata from the worker callback itself,
        # not from an asyncio task that would be cancelled during loop teardown.
        close_future.add_done_callback(lambda _future: _settle_session_close(old))
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(close_future, loop=loop)),
            timeout=_SESSION_CLOSE_TIMEOUT_SECONDS,
        )
        _log("[xunfei] 浏览器会话已关闭")
    except asyncio.TimeoutError:
        # The browser may already be gone while Playwright's Sync transport is
        # stuck in ``context.close``. Detach just this worker and remember its
        # profile owner so the replacement can safely reclaim that known
        # process instead of waiting behind it forever.
        _remember_orphaned_close(
            old,
            profile_owner_pid=old_profile_owner_pid,
        )
        _release_closing_session(old)
        _rotate_playwright_executor(old, expected_executor=executor)
        _log(
            "[xunfei] 关闭浏览器会话超时，已隔离旧执行线程；"
            "下次任务将重建浏览器"
        )
    except asyncio.CancelledError:
        # Cancellation must not leave every later retry fenced behind the
        # abandoned close task. The Sync call continues only on its detached
        # daemon, while the next session uses a clean worker.
        _remember_orphaned_close(
            old,
            profile_owner_pid=old_profile_owner_pid,
        )
        _release_closing_session(old)
        _rotate_playwright_executor(old, expected_executor=executor)
        raise
    except Exception as error:
        # A failed close is also unsafe to reuse: retain only its narrow
        # profile-reclaim fact, then avoid scheduling later work on that
        # potentially corrupted Sync API worker.
        _release_closing_session(old)
        _remember_orphaned_close(
            old,
            profile_owner_pid=old_profile_owner_pid,
        )
        _rotate_playwright_executor(old, expected_executor=executor)
        _log(f"[xunfei] 关闭浏览器会话异常: {error}")
    # Keep the daemon worker alive after a normal browser close. A retry may
    # already be queued behind ``old.close``; shutting the worker down here
    # cancels that queued login or binds its new session to the wrong thread.
    # Disconnect recovery still rotates a stuck worker, and process exit does
    # the final shutdown.
