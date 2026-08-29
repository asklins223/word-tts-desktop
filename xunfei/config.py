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

from app_paths import ensure_data_dir


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
# WordTTS 数据目录内，和音频、音色缓存保持同一数据边界。
_legacy_profile_dir = os.path.join(
    os.path.expanduser("~"), ".xunfei_chrome_profile"
)
PROFILE_DIR = os.path.join(BASE_DIR, "xunfei_chrome_profile")
if not os.path.exists(PROFILE_DIR) and os.path.isdir(_legacy_profile_dir):
    PROFILE_DIR = _legacy_profile_dir

# Chrome 可执行文件路径候选（macOS 自定义安装位置 + Linux 常见位置）
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
    for path in _CHROME_CANDIDATES:
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

