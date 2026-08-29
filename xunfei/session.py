"""Browser session lifecycle for the Xunfei provider.

This module owns browser startup, login readiness, and cleanup of session
state; the concrete session combines it with the focused provider mixins.
"""

from __future__ import annotations

import os
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
    def __init__(self, voice_key="amanda"):
        voice_info = get_voice_info(voice_key)
        self.voice_key = str(voice_key)
        self.voice_name = voice_info["name"]
        self.voice_display = voice_info["display"]
        self._playwright = None
        self._ctx = None
        self._page = None
        self._logged_in = False
        self._browser_disconnected = False
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
        if not self._browser_disconnected:
            _log("[xunfei] 浏览器窗口已关闭或连接断开，将在下次任务中重建会话")
        self._browser_disconnected = True
        self._logged_in = False

    def login(self, login_timeout=300, cancel_check=None, progress_callback=None):
        """
        打开可见的 Chrome 浏览器，导航到讯飞配音。
        首次需要手动登录（手机号+验证码），后续自动复用已保存的登录状态。
        """
        self._browser_disconnected = False
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
        except Exception as chrome_error:
            if "executable_path" in launch_kwargs:
                _log(f"[xunfei] Chrome 启动失败，改用 Playwright Chromium: {chrome_error}")
                launch_kwargs.pop("executable_path", None)
                launch_kwargs.setdefault(
                    "user_agent",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36",
                )
                self._ctx = self._playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )
            else:
                raise

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
                self._page.wait_for_timeout(500)
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

        self._logged_in = True

    def close(self):
        """关闭浏览器，保留登录状态（持久化目录不被删除）。"""
        _log("[xunfei] 正在关闭浏览器...")
        try:
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
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._ctx = None
            self._page = None
            self._playwright = None
            self._logged_in = False
            self._browser_disconnected = True
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
