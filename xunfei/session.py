"""Browser session lifecycle for the Xunfei provider.

This module owns browser startup, login readiness, and cleanup of session
state; the concrete session combines it with the focused provider mixins.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from .config import (
    HOME_URL,
    MUTE_AUDIO_SCRIPT,
    PROFILE_DIR,
    STEALTH_SCRIPT,
    _find_chrome,
)
from .errors import (
    XunfeiError,
    XunfeiLoginRequired,
    _check_cancel_requested,
    _notify_runtime_progress,
    _log,
    _wait_with_cancel,
)
from .helpers import poll as _poll
from .voice_catalog import get_voice_info
from .submission_tracker import SubmissionTrackerMixin
from .page_actions import PageActionsMixin
from .downloads import DownloadMixin
from .composite_actions import CompositeActionsMixin
from .generation import GenerationMixin


class SessionLifecycleMixin:
    """Initialize, log in, and close a persistent Xunfei browser session."""

    @staticmethod
    def _profile_lock_owner_pid(profile_dir=PROFILE_DIR):
        """Return the PID encoded by Chrome's SingletonLock symlink."""

        lock_path = os.path.join(profile_dir, "SingletonLock")
        try:
            if not os.path.islink(lock_path):
                return None
            target = os.readlink(lock_path)
        except OSError:
            return None
        match = re.search(r"-(\d+)$", str(target))
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _profile_lock_owner_command(pid):
        """Return the owning process command when the platform exposes it."""

        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            else:
                result = subprocess.run(
                    ["ps", "-ww", "-p", str(int(pid)), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            return None
        return str(result.stdout or "").strip()

    @staticmethod
    def _profile_lock_owner_alive(pid):
        """Check a lock owner without treating a stale PID as a live Chrome."""

        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # A live process owned by another user must keep its profile lock.
            return True
        except (TypeError, ValueError, OSError):
            return False

        # If the process probe is unavailable, fail closed and preserve the
        # lock rather than risking another browser using the profile.
        command = SessionLifecycleMixin._profile_lock_owner_command(pid)
        return True if command is None else bool(command)

    @classmethod
    def _terminate_profile_owner(cls, profile_dir=PROFILE_DIR, expected_pid=None):
        """Stop a leftover Chrome process that owns this dedicated profile.

        Closing the last Chrome window on macOS can leave a background Chrome
        process alive.  That process keeps ``SingletonLock`` and makes the
        next Playwright persistent-context launch fail forever.  Only kill a
        process whose current singleton owner matches ``expected_pid`` (or
        whose command explicitly contains this profile) and whose command
        identifies Chrome/Chromium.  This avoids touching a user's unrelated
        browser process.
        """

        owner_pid = cls._profile_lock_owner_pid(profile_dir)
        if owner_pid is None:
            return False
        if expected_pid is not None:
            try:
                if int(expected_pid) != owner_pid:
                    return False
            except (TypeError, ValueError):
                return False
        command = cls._profile_lock_owner_command(owner_pid)
        # A process-list probe failure is intentionally fail-closed.  Without
        # a command line we cannot prove that the PID is our dedicated Chrome.
        if not command:
            return False
        lowered_command = command.casefold()
        profile_marker = os.path.realpath(os.path.abspath(str(profile_dir))).casefold()
        command_profile_marker = str(profile_dir).casefold()
        browser_process = any(
            marker in lowered_command
            for marker in ("chrome", "chromium", "msedge")
        )
        owns_profile = profile_marker in lowered_command or command_profile_marker in lowered_command
        if not browser_process or not owns_profile:
            return False

        _log(f"[xunfei] 正在结束占用讯飞配置目录的残留浏览器进程 pid={owner_pid}")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(owner_pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            else:
                os.kill(owner_pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            return False

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not cls._profile_lock_owner_alive(owner_pid):
                return True
            time.sleep(0.1)
        if os.name != "nt":
            try:
                os.kill(owner_pid, signal.SIGKILL)
            except OSError:
                pass
        return not cls._profile_lock_owner_alive(owner_pid)

    @classmethod
    def _clear_stale_profile_lock(cls, profile_dir=PROFILE_DIR):
        """Remove only Chrome singleton links whose owner process is gone.

        Chrome can leave these links behind after a force-quit or a crashed
        renderer.  Passing that profile to Playwright then fails every retry
        with an "already running" error.  Never delete a lock whose PID is
        still alive, and re-check the symlink before unlinking to avoid racing
        a new Chrome process that has claimed the profile.
        """

        lock_path = os.path.join(profile_dir, "SingletonLock")
        try:
            if not os.path.islink(lock_path):
                return False
            original_target = os.readlink(lock_path)
        except OSError:
            return False
        match = re.search(r"-(\d+)$", str(original_target))
        if not match:
            return False
        try:
            owner_pid = int(match.group(1))
        except (TypeError, ValueError):
            return False
        if cls._profile_lock_owner_alive(owner_pid):
            return False
        try:
            if os.readlink(lock_path) != original_target:
                return False
        except OSError:
            return False

        removed = []
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = os.path.join(profile_dir, name)
            try:
                if os.path.lexists(path):
                    os.unlink(path)
                    removed.append(name)
            except OSError as error:
                _log(f"[xunfei] 清理失效浏览器锁文件失败（{name}）: {error}")
        if removed:
            _log(
                "[xunfei] 已清理失效的讯飞浏览器配置锁，准备重新启动: "
                + ", ".join(removed)
            )
        return bool(removed)

    def __init__(self, voice_key="amanda"):
        voice_info = get_voice_info(voice_key)
        self.voice_key = str(voice_key)
        self.voice_name = voice_info["name"]
        self.voice_display = voice_info["display"]
        self._playwright = None
        self._ctx = None
        self._page = None
        self._status_lock = threading.Lock()
        self._logged_in = False
        self._browser_disconnected = False
        self._profile_owner_pid = None
        self._reclaim_profile_owner = False
        self._reclaim_profile_owner_pid = None
        # ``close()`` is also used to clean up a failed startup.  It must not
        # turn an ordinary startup error (for example, a cache prompt that
        # could not be dismissed) into a false browser-disconnected signal.
        self._close_requested = False
        self._real_ua = None
        # 页面状态跟踪（页面复用的关键）。音色 key 和页面显示名称都保留：
        # key 防止同名音色串用，页面回读防止讯飞提交后把音色恢复为默认值。
        self._current_voice_key = None
        self._current_voice_name = None
        self._applied_params = None  # dict(speed=, pitch=, volume=) 或 None
        # worksId 捕获。时间戳截止线只作为兼容兜底，真实页面优先使用
        # request->response 序号 fence 防止旧请求的延迟 response 跨条串入。
        self._works_lock = threading.Lock()
        self._works_entries = []     # [(works_id, ts)]
        # order_gen 返回的正式 worksId 与多人编辑页先返回的 tempWorksId
        # 可能在同一条任务中同时出现。单独保留正式 ID，消费时优先使用，
        # 防止响应到达顺序把临时 ID 当成最终作品 ID。
        self._final_works_entries = []  # [(works_id, ts)]
        self._temporary_works_entries = []  # [(works_id, ts)]
        self._works_cutoff = 0.0
        self._submission_request_sequence = 0
        self._submission_request_cutoff = 0
        self._submission_requests = []  # [(request_token, sequence)]
        # 作品列表扫描状态：None 表示旧测试桩/兼容实现没有提供状态，False
        # 表示接口失败或扫描被截断，True 表示至少完整得到了一次列表结果。
        self._last_works_list_scan_complete = None
        self._last_works_list_fetch_ok = None
        # sign_url 兜底通道捕获：必须和 worksId 绑定，禁止拿“最新 URL”串条目。
        self._sign_urls = []     # [(works_id, sign_url, ts)]
        # 讯飞网页会为每次 video-api 请求动态生成 sid，并在请求头中补充
        # authorization/sign。这里只保存网页请求暴露的稳定字段，sid 每次
        # 由 _signed_api_post 重新生成，避免复用已消费的请求签名。
        self._api_base = {
            "appid": "xfpy",
            "channelId": "40000001",
            "osid": 0,
        }
        self._api_authorization = None
        self._response_handler = None
        self._request_handler = None
        self._confirm_click_succeeded = False
        self._submission_state_uncertain = False

    def _mark_browser_disconnected(self, *_args):
        """Make a manually closed/crashed Chrome session fail health checks."""
        with self._status_lock:
            # The context/page close events are also emitted by our own
            # cleanup.  Only an unsolicited lifecycle event is evidence that
            # the user/browser disconnected.
            if self._close_requested:
                return
            already_disconnected = self._browser_disconnected
            self._browser_disconnected = True
            self._logged_in = False
        if not already_disconnected:
            _log("[xunfei] 浏览器窗口已关闭或连接断开，将在下次任务中重建会话")

    def runtime_status_snapshot(self):
        """Return a consistent, thread-safe health view for UI projections."""
        with self._status_lock:
            return {
                "logged_in": bool(self._logged_in),
                "browser_disconnected": bool(self._browser_disconnected),
            }

    def login(self, login_timeout=300, cancel_check=None, progress_callback=None):
        """
        打开可见的 Chrome 浏览器，导航到讯飞配音。
        首次需要手动登录（手机号+验证码），后续自动复用已保存的登录状态。
        """
        with self._status_lock:
            self._browser_disconnected = False
            self._close_requested = False
        _check_cancel_requested(cancel_check)
        _notify_runtime_progress(
            progress_callback,
            stage="browser_starting",
            message="正在启动讯飞浏览器会话",
        )
        self._playwright = sync_playwright().start()

        chrome_path = _find_chrome()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1440,1000",
            "--lang=zh-CN",
            "--mute-audio",
            # 自动化只需要当前讯飞页面，不需要 Chrome 的后台同步、组件
            # 更新、扩展和通知。这些服务在低配电脑上会持续占用网络、内存
            # 和后台线程，但不会影响登录、合成或下载。
            "--disable-background-networking",
            "--disable-background-mode",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-notifications",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-pings",
        ]
        # 仅 Linux 容器/沙箱需要这个兼容参数。macOS/Windows 没有 /dev/shm，
        # 强制走磁盘反而可能降低 Chromium 的渲染和页面交互速度。
        if sys.platform.startswith("linux"):
            launch_args.append("--disable-dev-shm-usage")

        launch_kwargs = {
            "user_data_dir": PROFILE_DIR,
            "headless": False,
            "args": launch_args,
            "locale": "zh-CN",
            # viewport=None：跟随真实窗口尺寸，不做分辨率仿真（指纹一致性）
            "viewport": None,
            "extra_http_headers": {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }

        os.makedirs(PROFILE_DIR, exist_ok=True)
        # A force-quit can leave Chrome's singleton symlinks behind even
        # though the owning browser process is gone. Clear that narrow stale
        # lock before every new persistent-context launch so a retry gets a
        # real browser process instead of immediately reusing a dead session.
        self._clear_stale_profile_lock(PROFILE_DIR)
        if self._reclaim_profile_owner:
            self._terminate_profile_owner(
                PROFILE_DIR,
                expected_pid=self._reclaim_profile_owner_pid,
            )
            self._clear_stale_profile_lock(PROFILE_DIR)
        _log(f"[xunfei] 浏览器配置目录: {PROFILE_DIR}")

        # 优先使用系统 Chrome（真实 UA / 真实指纹），仅 Chromium 降级时补 UA
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path
            _log(f"[xunfei] 使用 Chrome: {chrome_path}")
        else:
            launch_kwargs["user_agent"] = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
            _log("[xunfei] 未找到系统 Chrome，使用 Playwright Chromium", )

        try:
            self._ctx = self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )
        except Exception as first_error:
            # The browser can exit between the preflight check and the
            # launch call. Retry the preferred executable once if that race
            # left a now-dead SingletonLock behind.
            lock_recovered = self._clear_stale_profile_lock(PROFILE_DIR)
            if lock_recovered:
                try:
                    self._ctx = self._playwright.chromium.launch_persistent_context(
                        **launch_kwargs
                    )
                except Exception as retry_error:
                    first_error = retry_error

            if self._ctx is None and "executable_path" in launch_kwargs:
                _log(f"[xunfei] Chrome 启动失败，改用 Playwright Chromium: {first_error}")
                launch_kwargs.pop("executable_path", None)
                launch_kwargs.setdefault(
                    "user_agent",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36",
                )
                # A failed Chrome launch may have created its own dead lock;
                # perform the same narrow check before the fallback engine.
                self._clear_stale_profile_lock(PROFILE_DIR)
                self._ctx = self._playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )
            elif self._ctx is None:
                raise first_error

        self._profile_owner_pid = self._profile_lock_owner_pid(PROFILE_DIR)

        self._ctx.add_init_script(STEALTH_SCRIPT)
        self._ctx.add_init_script(MUTE_AUDIO_SCRIPT)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        try:
            self._ctx.on("close", self._mark_browser_disconnected)
        except Exception:
            pass
        for event_name in ("close", "crash"):
            try:
                self._page.on(event_name, self._mark_browser_disconnected)
            except Exception:
                pass

        # 注册网络响应监听（整个会话期间持续捕获 worksId / sign_url）
        self._response_handler = self._on_response
        self._request_handler = self._on_request
        self._page.on("request", self._request_handler)
        self._page.on("response", self._response_handler)

        try:
            self._real_ua = self._page.evaluate("navigator.userAgent")
        except Exception:
            pass

        try:
            self._page.bring_to_front()
        except Exception:
            pass
        _log("[xunfei] 正在打开讯飞配音...")
        _notify_runtime_progress(
            progress_callback,
            stage="page_loading",
            message="正在打开讯飞配音页面",
        )

        try:
            self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as goto_error:
            _log(f"[xunfei] 首次加载提示: {goto_error}")

        def editor_visible():
            try:
                return bool(self._page.locator(".ssml-editor:visible").count())
            except Exception:
                return False

        def page_readiness():
            if self._browser_disconnected:
                return "disconnected"
            # Detect the login surface as soon as it is rendered. Waiting for
            # the editor first made a logged-out/reloaded page look frozen for
            # two consecutive 30-second waits even though the user only needed
            # to complete the visible login step.
            if not self._is_logged_in(self._page):
                return "login"
            return "editor" if editor_visible() else None

        # 不再让一个不可取消的 wait_for_selector 把“返回配置”卡住 30 秒。
        # 按短间隔探测编辑器或登录界面，同时让 cancel_check 能中断登录/重建。
        readiness = _poll(
            page_readiness,
            timeout=30,
            interval=0.25,
            max_interval=1.0,
            page=self._page,
            cancel_check=cancel_check,
        )
        if readiness == "disconnected":
            raise XunfeiError("讯飞浏览器连接已断开")
        if not readiness:
            _log("[xunfei] 页面编辑器未找到，重试加载...")
            _notify_runtime_progress(
                progress_callback,
                stage="page_reloading",
                message="页面加载较慢，正在重新建立讯飞页面",
            )
            _check_cancel_requested(cancel_check)
            try:
                self._page.goto(
                    HOME_URL, wait_until="domcontentloaded", timeout=60000
                )
            except Exception:
                pass
            readiness = _poll(
                page_readiness,
                timeout=30,
                interval=0.25,
                max_interval=1.0,
                page=self._page,
                cancel_check=cancel_check,
            )
            if readiness == "disconnected":
                raise XunfeiError("讯飞浏览器连接已断开")
            if not readiness:
                raise XunfeiError("无法加载讯飞配音编辑器")

        if readiness == "editor":
            _notify_runtime_progress(
                progress_callback,
                stage="editor_ready",
                message="讯飞配音编辑器已就绪",
            )
        if readiness == "editor" and not self._dismiss_local_draft_prompt(self._page, cancel_check=cancel_check):
            raise XunfeiError("讯飞页面被本地缓存恢复弹窗遮挡")

        # 检测登录状态
        if readiness == "editor" and self._is_logged_in(self._page):
            _log("[xunfei] 检测到已保存的登录状态，无需重新登录")
        else:
            _notify_runtime_progress(
                progress_callback,
                stage="waiting_login",
                message="等待你在讯飞浏览器中完成登录",
            )
            _log("[xunfei] 登录状态无效，请在浏览器中手动登录...")
            _log(f"[xunfei] 等待用户登录（超时 {login_timeout} 秒）...")
            deadline = time.time() + login_timeout
            logged = False
            next_status_log = time.monotonic() + 10
            while time.time() < deadline:
                _check_cancel_requested(cancel_check)
                if self._browser_disconnected:
                    raise XunfeiError("讯飞浏览器连接已断开")
                _wait_with_cancel(self._page, 0.5, cancel_check=cancel_check)
                if self._is_logged_in(self._page):
                    if editor_visible():
                        logged = True
                        break
                if time.monotonic() >= next_status_log:
                    _log(f"[xunfei] 仍在等待登录/页面就绪（当前页面: {self._page.url}）")
                    next_status_log = time.monotonic() + 10
            if not logged:
                raise XunfeiLoginRequired(
                    f"等待登录超时（{login_timeout}秒），讯飞配音登录未完成"
                )
            _log("[xunfei] 登录成功！")

        _notify_runtime_progress(
            progress_callback,
            stage="session_ready",
            message="讯飞浏览器会话已就绪，开始提交生成任务",
        )

        with self._status_lock:
            self._logged_in = True

    def close(self):
        """关闭浏览器，保留登录状态（持久化目录不被删除）。"""
        _log("[xunfei] 正在关闭浏览器...")
        with self._status_lock:
            # Preserve a disconnect that was observed before cleanup, but do
            # not create one merely because this method is closing the
            # context after another startup error.
            disconnected_before_close = bool(self._browser_disconnected)
            self._close_requested = True
            expected_profile_owner_pid = self._profile_owner_pid
        # If the user closed the visible window but Chrome stayed alive in
        # background mode, release its profile before asking Playwright to
        # close an already-disconnected context.
        if disconnected_before_close:
            self._terminate_profile_owner(
                PROFILE_DIR,
                expected_pid=expected_profile_owner_pid,
            )
        try:
            # Prevent the intentional context close below from racing with
            # the lifecycle callbacks and overwriting the original error.
            if self._ctx:
                try:
                    self._ctx.remove_listener("close", self._mark_browser_disconnected)
                except Exception:
                    pass
            if self._page:
                for event_name in ("close", "crash"):
                    try:
                        self._page.remove_listener(event_name, self._mark_browser_disconnected)
                    except Exception:
                        pass
            if self._page and self._response_handler:
                try:
                    self._page.remove_listener("response", self._response_handler)
                except Exception:
                    pass
            if self._page and self._request_handler:
                try:
                    self._page.remove_listener("request", self._request_handler)
                except Exception:
                    pass
            if self._ctx:
                self._ctx.close()
        except Exception as e:
            _log(f"[xunfei] 关闭浏览器异常: {e}")
        finally:
            # ``context.close`` normally terminates the owned browser, but a
            # macOS background process can survive the last visible window.
            # Re-check the exact PID after the normal close and release only
            # that process if it still owns the dedicated profile.
            if expected_profile_owner_pid is not None:
                self._terminate_profile_owner(
                    PROFILE_DIR,
                    expected_pid=expected_profile_owner_pid,
                )
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._ctx = None
            self._page = None
            self._playwright = None
            self._profile_owner_pid = None
            with self._status_lock:
                self._logged_in = False
                self._browser_disconnected = disconnected_before_close
            self._current_voice_key = None
            self._current_voice_name = None
            self._applied_params = None
            self._response_handler = None
            self._request_handler = None
            self._confirm_click_succeeded = False
            self._submission_state_uncertain = False
            with self._works_lock:
                self._works_entries = []
                self._final_works_entries = []
                self._temporary_works_entries = []
                self._submission_request_sequence = 0
                self._submission_request_cutoff = 0
                self._submission_requests = []
                self._last_works_list_scan_complete = None
                self._last_works_list_fetch_ok = None
                self._sign_urls = []
                self._api_base = {
                    "appid": "xfpy",
                    "channelId": "40000001",
                    "osid": 0,
                }
                self._api_authorization = None
            _log("[xunfei] 浏览器已关闭（登录状态已保留）")


class XunFeiSession(
    SessionLifecycleMixin,
    SubmissionTrackerMixin,
    PageActionsMixin,
    DownloadMixin,
    CompositeActionsMixin,
    GenerationMixin,
):
    """
    管理讯飞配音的 Chrome 浏览器持久会话：登录 → 连续合成 → 关闭。

    用法:
        session = XunFeiSession()
        session.login()                                     # 打开浏览器，首次需手动登录
        session.synth_one("hello", speed=35, ...)           # 生成第 1 条
        session.synth_one("world")                          # 第 2 条（页面复用，不重选音色）
        session.close()

    会话内记录当前选中的发音人和已应用的语速/语调/音量，
    相同配置的下一条任务直接跳过设置步骤。
    """
