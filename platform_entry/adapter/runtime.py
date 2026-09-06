"""Browser runtime lifecycle for the visible-page workflow."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .automation import PlatformInputPageAutomation
from .constants import ADMIN_URL, API_BASE_URL, DEFAULT_PROFILE_DIR
from .errors import PlatformInputError
from .flow import run_page_input, run_page_verify
from .models import PlatformInputSpec
from .observer import ReadOnlyFeedbackObserver


def _profile_lock_lifecycle() -> Any:
    """Return the already-tested Chrome profile-lock implementation.

    The visible-page adapter and the Xunfei provider use the same dedicated
    Chromium profile.  Keep the lock rules in one place instead of allowing
    the two launch paths to drift apart again.  The import stays lazy so
    importing the page adapter still works in environments without Playwright.
    """

    from xunfei.session import SessionLifecycleMixin

    return SessionLifecycleMixin


def _default_profile_dir() -> Path:
    from app_paths import ensure_data_dir

    # The desktop workflow and the standalone adapter share one persistent
    # browser profile.  Prefer the desktop-specific name used by the Electron
    # executor, while keeping the original CLI variable backwards compatible.
    configured = (
        os.environ.get("WORDTTS_PLATFORM_INPUT_PROFILE_DIR", "").strip()
        or os.environ.get("PLATFORM_INPUT_PROFILE_DIR", "").strip()
    )
    if configured:
        return Path(os.path.abspath(os.path.expanduser(configured)))
    return Path(ensure_data_dir()) / DEFAULT_PROFILE_DIR

def _launch_browser(playwright: Any, profile_dir: Path) -> Any:
    """启动可见独立 Chrome 配置，不复用用户正在运行的默认配置。"""

    from xunfei.config import (
        _find_bundled_chromium,
        _find_chrome,
        configure_playwright_runtime,
    )

    configure_playwright_runtime()
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_lifecycle = _profile_lock_lifecycle()
    # macOS may leave SingletonLock/Cookie/Socket behind after the last
    # visible window closes.  Only ownerless locks are removed; a live Chrome
    # process is never touched before the first launch attempt.
    lock_lifecycle._clear_stale_profile_lock(profile_dir)
    executable = _find_chrome() or _find_bundled_chromium()
    options: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "viewport": None,
        "locale": "zh-CN",
    }
    if executable:
        options["executable_path"] = executable

    # A browser can disappear between the stale-lock check and Playwright's
    # profile acquisition.  Reclaim only a Chrome process proven to own this
    # profile, clear its now-stale singleton files, then make one clean retry.
    for launch_attempt in range(2):
        try:
            return playwright.chromium.launch_persistent_context(**options)
        except Exception:
            if launch_attempt == 0:
                lock_lifecycle._terminate_profile_owner(profile_dir)
                lock_lifecycle._clear_stale_profile_lock(profile_dir)
                continue
            raise


class PersistentBrowserSession:
    """Own one Playwright driver/context for a multi-unit input run.

    The session is deliberately small: the page adapters still own visible
    page actions, while this object owns only the driver and persistent
    browser lifetime.  Keeping the context open preserves the authenticated
    profile and avoids a second launch between preflight and execution.
    """

    def __init__(self, profile_dir: Path, *, launch_browser: Any | None = None) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self._launch_browser = launch_browser or _launch_browser
        self._manager: Any | None = None
        self._manager_started = False
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._owner_pid: int | None = None
        # The system-input preflight intentionally stops before the external
        # write boundary.  Keep the visible page phase so the real execution
        # can continue from that exact page instead of repeating "新增试卷"
        # and leaving the user on the first step.
        self._preflight_phase: str | None = None

    @property
    def context(self) -> Any:
        return self.open()

    def open(self) -> Any:
        if self._context is not None:
            try:
                if not self._context.is_closed():
                    return self._context
            except Exception:
                # Older Playwright shims may not expose is_closed().  The
                # context object is still the best source of truth there.
                return self._context
            self.close()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PlatformInputError(
                "执行模式需要 Playwright；请先安装 requirements_electron.txt"
            ) from exc

        manager = sync_playwright()
        manager_started = False
        try:
            if callable(getattr(manager, "start", None)):
                playwright = manager.start()
                manager_started = True
            else:
                # Small test/integration shims sometimes expose only the
                # context-manager surface of sync_playwright().
                playwright = manager.__enter__()
            context = self._launch_browser(playwright, self.profile_dir)
        except Exception:
            try:
                if manager_started and callable(getattr(manager, "stop", None)):
                    manager.stop()
                elif callable(getattr(manager, "__exit__", None)):
                    manager.__exit__(None, None, None)
            except Exception:
                pass
            raise
        self._manager = manager
        self._manager_started = manager_started
        self._playwright = playwright
        self._context = context
        self._owner_pid = _profile_lock_lifecycle()._profile_lock_owner_pid(self.profile_dir)
        return context

    def page(self) -> Any:
        context = self.open()
        try:
            pages = list(context.pages)
        except Exception:
            pages = []
        for page in pages:
            try:
                if not page.is_closed():
                    return page
            except Exception:
                return page
        return context.new_page()

    def mark_preflight_phase(self, phase: str) -> None:
        self._preflight_phase = str(phase or "").strip() or None

    def consume_preflight_phase(self) -> str | None:
        phase = self._preflight_phase
        self._preflight_phase = None
        return phase

    def close(self) -> None:
        context = self._context
        manager = self._manager
        manager_started = self._manager_started
        owner_pid = self._owner_pid
        self._context = None
        self._manager = None
        self._manager_started = False
        self._playwright = None
        self._owner_pid = None
        self._preflight_phase = None
        try:
            if context is not None:
                context.close()
        finally:
            try:
                if owner_pid is not None:
                    _profile_lock_lifecycle()._terminate_profile_owner(
                        self.profile_dir,
                        expected_pid=owner_pid,
                    )
            finally:
                if manager is not None:
                    try:
                        if manager_started and callable(getattr(manager, "stop", None)):
                            manager.stop()
                        elif callable(getattr(manager, "__exit__", None)):
                            manager.__exit__(None, None, None)
                    except Exception:
                        pass

    def __enter__(self) -> "PersistentBrowserSession":
        self.open()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

def execute_live(
    spec: PlatformInputSpec,
    *,
    profile_dir: Path,
    admin_url: str,
    api_base: str,
    login_timeout: float,
    return_to_list: bool = True,
    edit_existing: bool = False,
    existing_paper_id: str | None = None,
    keep_browser_open: bool = False,
    browser_session: PersistentBrowserSession | None = None,
    control_check: Callable[[], None] | None = None,
    resume_phase: str | None = None,
) -> dict[str, Any]:
    """打开浏览器并执行页面流程；api_base 只用于响应监听过滤。"""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlatformInputError(
            "执行模式需要 Playwright；请先安装 requirements_electron.txt"
        ) from exc

    def run_in_context(context: Any) -> dict[str, Any]:
        page = browser_session.page() if browser_session is not None else (
            context.pages[0] if context.pages else context.new_page()
        )
        observer = ReadOnlyFeedbackObserver(
            page,
            api_base=api_base,
            admin_url=admin_url,
            paper_title=spec.paper["title"],
        )
        try:
            automation = PlatformInputPageAutomation(
                page,
                spec,
                observer,
                existing_paper_id=existing_paper_id,
            )
            result = run_page_input(
                automation,
                admin_url=admin_url,
                login_timeout=login_timeout,
                return_to_list=return_to_list,
                edit_existing=edit_existing,
                existing_paper_id=existing_paper_id,
                control_check=control_check,
                resume_phase=resume_phase,
            )
            if keep_browser_open:
                print(
                    "页面录入已完成，Chrome 保持打开；按 Ctrl+C 结束脚本。",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
            return result
        finally:
            if callable(getattr(observer, "close", None)):
                observer.close()

    if browser_session is not None:
        return run_in_context(browser_session.open())

    with sync_playwright() as playwright:
        context = _launch_browser(playwright, profile_dir)
        try:
            return run_in_context(context)
        finally:
            context.close()


def _verify_spec(paper_title: str) -> PlatformInputSpec:
    """Build the minimal spec the read-only verification flow needs.

    Verification never opens a form, so only the title used for the visible
    list search is required. Template and content fields stay empty on
    purpose; filling them would imply write capabilities this flow must not
    have.
    """

    return PlatformInputSpec(
        paper={"title": paper_title},
        items=(),
        paper_category="",
        template_name="",
        groups=(),
    )


def verify_live(
    *,
    paper_title: str,
    profile_dir: Path,
    admin_url: str,
    api_base: str,
    login_timeout: float,
) -> dict[str, Any]:
    """Run a read-only paper lookup through the visible platform page."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlatformInputError(
            "执行模式需要 Playwright；请先安装 requirements_electron.txt"
        ) from exc

    with sync_playwright() as playwright:
        context = _launch_browser(playwright, profile_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            observer = ReadOnlyFeedbackObserver(
                page,
                api_base=api_base,
                admin_url=admin_url,
                paper_title=paper_title,
            )
            automation = PlatformInputPageAutomation(
                page,
                _verify_spec(paper_title),
                observer,
            )
            return run_page_verify(
                automation,
                admin_url=admin_url,
                login_timeout=login_timeout,
            )
        finally:
            context.close()
