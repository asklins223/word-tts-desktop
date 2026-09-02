"""Configuration and platform primitives for the Xunfei provider.

This module deliberately contains no Playwright objects.  Keeping paths,
provider URLs and scalar normalization here lets the browser/session code be
split without changing the legacy public module's values.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from app_paths import ensure_data_dir, resource_dir


BASE_DIR = ensure_data_dir()

# 合成临时文件必须和后端其它输出共用可写数据目录，不能写入
# PyInstaller 的 _MEIPASS，也不能在 Electron 开发时散落到代码目录。
OUTPUT_DIR = os.path.join(BASE_DIR, "xunfei_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

HOME_URL = "https://peiyin.xunfei.cn/make"
# 讯飞当前“我的作品/下载”页实际路由是 /user；旧版 /myworks 已经返回 404。
DOWNLOAD_PAGE_URL = "https://peiyin.xunfei.cn/user"
API_WORKS_LIST_URL = "https://peiyin.xunfei.cn/video-api/synth/qry_works_synth_list"
API_SIGN_URL = "https://peiyin.xunfei.cn/video-api/synth/get_work_sign_url"

# 持久化浏览器配置目录（保存 cookies / 登录状态）。
# 首次升级时优先复用旧目录，避免用户被迫重新扫码登录；新安装统一放在
# WordTTS 数据目录内，和音频、音色缓存保持同一数据边界。不能只用
# ``os.path.exists`` 判断新目录：版本升级或一次启动可能先创建了空的
# 新目录，从而把仍然有登录状态的旧目录遮住。
_legacy_profile_dir = os.path.join(
    os.path.expanduser("~"), ".xunfei_chrome_profile"
)


def _has_persistent_browser_state(profile_dir):
    """判断 Profile 是否已初始化，只检查 Chrome 状态文件是否存在。

    不读取 Cookies 内容，避免在配置解析阶段接触任何登录凭据；这些文件
    名称也兼容 Chromium 在不同版本中的 Cookies 存放位置。
    """

    profile_root = os.path.abspath(os.path.expanduser(str(profile_dir)))
    return any(
        os.path.isfile(os.path.join(profile_root, relative_path))
        for relative_path in (
            "Default/Cookies",
            "Default/Network/Cookies",
            "Default/Preferences",
            "Local State",
        )
    )


def _resolve_profile_dir(base_dir, legacy_profile_dir):
    """Resolve one stable Profile path across app updates.

    The legacy location was authoritative before the data-directory refactor.
    If it still contains an initialized browser Profile, keep using it even
    when an update has already created a second canonical directory. This
    avoids silently switching the account's encrypted browser storage.
    """

    canonical_profile_dir = os.path.join(base_dir, "xunfei_chrome_profile")
    if _has_persistent_browser_state(legacy_profile_dir):
        return os.path.abspath(os.path.expanduser(str(legacy_profile_dir)))
    return canonical_profile_dir


PROFILE_DIR = _resolve_profile_dir(BASE_DIR, _legacy_profile_dir)

# Chrome 可执行文件路径候选。Windows 不应该依赖 PATH：普通安装通常把
# chrome.exe 放在 Program Files 或用户的 LocalAppData 中，而桌面应用启动
# 时拿到的 PATH 可能被 Electron/安装器裁剪过。
_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Volumes/asklins/app/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
]

# 讯飞平台的三个可调参数取值范围（50 = 默认）
PARAM_MIN = 0
PARAM_MAX = 100
PARAM_DEFAULT = 50
# makeMultipleSpeakerWork 的临时 ID 可能先于 order_gen 的正式 ID 到达；
# 给同一轮 Playwright 网络回调一个明确的缓冲窗口，避免网络稍慢时过早
# 消费临时值，随后又因下一条任务的 request fence 丢掉正式 worksId。
WORKS_ID_FINAL_GRACE_SECONDS = 3.0
# 只保留最近的提交请求引用，用于把 response 绑定回真正发起它的本次提交。
# Playwright 的 request 事件一定先于同一请求的 response 事件，因此即使旧
# response 延迟到下一条任务之后到达，也不会被新的截止线误认成当前任务。
MAX_TRACKED_SUBMISSION_REQUESTS = 512

IS_MAC = sys.platform == "darwin"
_SELECT_ALL = "Meta+A" if IS_MAC else "Control+A"
_MULTI_SELECT_MODIFIER = "Meta" if IS_MAC else "Control"

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en-US', 'en']
});
if (!window.chrome) {
    Object.defineProperty(window, 'chrome', {
        value: {runtime: {}}
    });
}
"""

# 禁止音频自动播放
MUTE_AUDIO_SCRIPT = """
if (window.Audio) {
    Audio.prototype.play = function() { return Promise.resolve(); };
}
if (window.HTMLMediaElement) {
    HTMLMediaElement.prototype.play = function() { return Promise.resolve(); };
}
"""


def _add_unique_path(paths, value):
    """Append a non-empty path once, preserving the preferred order."""

    text = str(value or "").strip()
    if text and text not in paths:
        paths.append(text)


def _chrome_candidates():
    """Return platform-aware Chrome executable candidates."""

    candidates = list(_CHROME_CANDIDATES)
    if sys.platform == "win32":
        # Chrome can be installed machine-wide or per user.  Do not use a
        # single hard-coded drive letter: packaged apps are often installed
        # outside C:\\ and the per-user installer uses LOCALAPPDATA.
        windows_roots = (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
            os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local"),
        )
        for root in windows_roots:
            if not root:
                continue
            _add_unique_path(
                candidates,
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
            )
    return candidates


def _playwright_browser_roots():
    """Return browser roots that are safe to inspect for this process."""

    roots = []
    configured = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")).strip()
    if configured and configured != "0":
        _add_unique_path(roots, os.path.abspath(os.path.expanduser(configured)))

    # The release builder stages Chromium beside the frozen backend.  This is
    # deliberately checked even when an inherited environment variable points
    # somewhere else; a stale developer/CI variable must not break the
    # installed app.
    if getattr(sys, "frozen", False):
        _add_unique_path(
            roots,
            os.path.join(resource_dir(), "playwright_browsers"),
        )

    # PLAYWRIGHT_BROWSERS_PATH=0 is a valid source-install setting.  The
    # browser then lives under Playwright's driver package rather than the
    # normal per-user cache, so include that narrow location when available.
    if configured == "0":
        try:
            import inspect
            import playwright

            _add_unique_path(
                roots,
                str(Path(inspect.getfile(playwright)).parent / "driver" / "package" / ".local-browsers"),
            )
        except Exception:
            pass
    return roots


def _has_chromium_revision(root):
    """Return whether a browser root contains a numbered Chromium revision."""

    try:
        return any(
            path.is_dir()
            and path.name[len("chromium-"):].isdigit()
            for path in Path(root).glob("chromium-*")
        )
    except (OSError, ValueError):
        return False


def _find_bundled_chromium():
    """Find the staged full Chromium executable, if this package has one."""

    if sys.platform == "win32":
        relative_executables = (
            ("chrome-win", "chrome.exe"),
            ("chrome-win32", "chrome.exe"),
            ("chrome.exe",),
        )
    elif sys.platform == "darwin":
        relative_executables = (
            ("chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
            ("chrome-mac", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"),
        )
    else:
        relative_executables = (
            ("chrome-linux", "chrome"),
            ("chrome-linux64", "chrome"),
        )

    for root in _playwright_browser_roots():
        browser_root = Path(root)
        try:
            revisions = sorted(
                (
                    path for path in browser_root.glob("chromium-*")
                    if path.is_dir() and path.name[len("chromium-"):].isdigit()
                ),
                key=lambda path: int(path.name[len("chromium-"):]),
                reverse=True,
            )
        except (OSError, ValueError):
            continue
        for revision in revisions:
            for relative in relative_executables:
                executable = revision.joinpath(*relative)
                if executable.is_file():
                    return str(executable)
    return None


def _platform_user_agent():
    """Return a normal headed-Chromium UA for the local platform."""

    if sys.platform == "win32":
        platform = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        platform = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )


def _electron_executable_for_backend():
    """Resolve the sibling Electron executable used as Playwright's Node."""

    if not getattr(sys, "frozen", False):
        return None
    backend_executable = os.path.realpath(sys.executable)
    if sys.platform == "darwin":
        contents_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(backend_executable))
        )
        candidate = os.path.join(contents_dir, "MacOS", "小猪wordTTS")
    elif sys.platform == "win32":
        app_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(backend_executable))
        )
        candidate = os.path.join(app_dir, "小猪wordTTS.exe")
    else:
        return None
    return candidate if os.path.isfile(candidate) else None


def _default_playwright_driver_node():
    """Return Playwright's bundled Node path when the build kept it."""

    try:
        import inspect
        import playwright

        driver_dir = Path(inspect.getfile(playwright)).parent / "driver"
        return str(driver_dir / ("node.exe" if sys.platform == "win32" else "node"))
    except Exception:
        return None


def configure_playwright_runtime():
    """Make the packaged Playwright driver and browser paths deterministic.

    The release build omits Playwright's duplicate Node binary and reuses the
    Electron executable instead.  This function validates an inherited path,
    repairs stale values, and provides the same fallback when the backend is
    launched directly from the final application directory.
    """

    if getattr(sys, "frozen", False):
        bundled_root = os.path.join(resource_dir(), "playwright_browsers")
        if os.path.isdir(bundled_root) and _has_chromium_revision(bundled_root):
            # Override an inherited PLAYWRIGHT_BROWSERS_PATH.  A stale value
            # such as a developer's cache or "0" is not part of the release.
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled_root

    configured_node = str(os.environ.get("PLAYWRIGHT_NODEJS_PATH", "")).strip()
    if configured_node and os.path.isfile(configured_node):
        return

    if configured_node:
        os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)

    default_node = _default_playwright_driver_node()
    if default_node and os.path.isfile(default_node):
        return

    electron_node = _electron_executable_for_backend()
    if electron_node:
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = electron_node
        # An inherited ELECTRON_RUN_AS_NODE=0 would otherwise make the
        # Electron binary start the desktop app instead of acting as Node.
        os.environ["ELECTRON_RUN_AS_NODE"] = "1"


def playwright_runtime_diagnostics():
    """Return non-secret startup facts for a user-facing launch failure."""

    configured_node = str(os.environ.get("PLAYWRIGHT_NODEJS_PATH", "")).strip()
    default_node = _default_playwright_driver_node()
    bundled_chromium = _find_bundled_chromium()
    return {
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
        "browser_root_configured": bool(
            str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")).strip()
        ),
        "bundled_chromium_found": bool(bundled_chromium),
        "driver_node_configured": bool(configured_node),
        "driver_node_exists": bool(configured_node and os.path.isfile(configured_node)),
        "driver_node_fallback_exists": bool(default_node and os.path.isfile(default_node)),
        "electron_node_fallback_exists": bool(_electron_executable_for_backend()),
        "system_chrome_found": bool(_find_chrome()),
    }


def clamp_param(value, default=PARAM_DEFAULT):
    """把任意输入收敛为合法的 0-100 整数参数。"""
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        v = default
    return max(PARAM_MIN, min(PARAM_MAX, v))


def _provider_success_code(value):
    """讯飞不同接口可能返回数字 0、字符串 0 或字符串 000000。"""
    if value is None:
        return False
    return str(value).strip() in {"000000", "0", "200"}


def _provider_bool(value, default=False):
    """把目录接口中的 bool/0-1/中英文字符串稳定转换为 bool。"""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().casefold()
    if text in {"", "0", "false", "no", "n", "否", "不是", "非"}:
        return False
    if text in {"1", "true", "yes", "y", "是", "vip"}:
        return True
    return bool(default)


def _find_chrome():
    """查找系统 Chrome 可执行文件路径。"""
    for path in _chrome_candidates():
        if os.path.isfile(path):
            return path
    if IS_MAC:
        try:
            result = subprocess.run(
                ["mdfind", "kMDItemCFBundleIdentifier == 'com.google.Chrome'"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    chrome_path = os.path.join(
                        line.strip(), "Contents", "MacOS", "Google Chrome"
                    )
                    if os.path.isfile(chrome_path):
                        return chrome_path
        except Exception:
            pass
    for name in ("google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None
