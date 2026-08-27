#!/usr/bin/env python3
"""
讯飞配音自动化客户端（wordtts 统一 TTS 引擎）
====================
通过 Playwright 有头 Chrome 自动化操作讯飞配音网站 (peiyin.xunfei.cn/make)，
实现文本到语音的合成与下载。

设计:
  - 使用持久化浏览器配置目录，首次需手动登录，后续自动复用登录状态
  - 单条合成：输入文本 → 选发音人 → 设置语速/语调/音量 → 生成音频 → 确认合成 → 拦截 worksId → 签名 URL 下载
  - 多人合成：在可见编辑器中输入所有行，按音色/参数把不连续段落加入讯飞网页的多段选择队列，一次性标注每个配置组，插入短停顿后点击页面生成，再按 worksId 下载
  - 页面复用：生成阶段保持编辑页，提交完成后进入作品下载页；按 worksId
    获取精确签名地址下载，浏览器下载仅作为按作品名匹配的兜底通道
  - 反批量检测采用行为拟真：击键抖动、随机间隙、真实鼠标事件；发布版默认使用
    隔离的 Playwright Chromium，源码调试时可显式切换系统 Chrome

发音人（默认）:
  - 女声 Amanda（英语女声）
  - 男声 George（英语男声）

依赖:
  pip install playwright && playwright install chromium
"""
import os
import re
import sys
import time
import json
import hashlib
import threading
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from playwright.sync_api import sync_playwright
from app_paths import ensure_data_dir
from browser_window_controller import BrowserWindowController

try:
    import psutil
except ImportError:  # 源码精简环境仍可运行，发布构建会把 psutil 一并收集。
    psutil = None


def _log(*args, **kwargs):
    """所有日志输出到 stdout/stderr，确保 Electron 后端能捕获。"""
    kwargs.setdefault('file', sys.stdout)
    kwargs.setdefault('flush', True)
    print(*args, **kwargs)


BASE_DIR = ensure_data_dir()

# 合成临时文件必须和后端其它输出共用可写数据目录，不能写入
# PyInstaller 的 _MEIPASS，也不能在 Electron 开发时散落到代码目录。
OUTPUT_DIR = os.path.join(BASE_DIR, "xunfei_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _confined_temp_output_path(output_name):
    """把合成临时文件名解析到专用输出目录内。"""
    try:
        name = os.fspath(output_name)
    except TypeError:
        name = str(output_name or "")
    if isinstance(name, bytes):
        name = os.fsdecode(name)
    name = str(name).strip()
    # output_name 是文件名而不是路径。额外拒绝 Windows 分隔符和盘符，
    # 这样同一份输入在 macOS/Linux 与 Windows 上都不会逃逸到别处。
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
        or ":" in name
    ):
        raise XunfeiError(f"非法讯飞临时文件名: {name!r}")
    root = os.path.realpath(OUTPUT_DIR)
    candidate = os.path.realpath(os.path.join(root, name))
    try:
        common = os.path.commonpath([root, candidate])
    except ValueError as error:
        raise XunfeiError(f"非法讯飞临时文件名: {name!r}") from error
    if (
        os.path.normcase(common) != os.path.normcase(root)
        or os.path.normcase(candidate) == os.path.normcase(root)
    ):
        raise XunfeiError(f"讯飞临时文件路径越界: {name!r}")
    return candidate


def _is_confined_temp_output_path(output_path):
    """判断临时输出路径是否仍位于讯飞专用目录。"""
    try:
        candidate = os.path.realpath(os.fspath(output_path))
        root = os.path.realpath(OUTPUT_DIR)
        common = os.path.commonpath([root, candidate])
    except (OSError, TypeError, ValueError):
        return False
    return (
        os.path.normcase(common) == os.path.normcase(root)
        and os.path.normcase(candidate) != os.path.normcase(root)
    )


def _remove_confined_temp_output(output_path):
    """只删除讯飞专用目录内的临时 MP3 及其下载分片。"""
    if not _is_confined_temp_output_path(output_path):
        return
    for candidate in (os.fspath(output_path), f"{output_path}.part"):
        try:
            os.remove(candidate)
        except OSError:
            pass

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

AUTOMATION_BROWSER_ENV = "WORDTTS_AUTOMATION_BROWSER"
CONTROL_POLL_MAX_INTERVAL_SECONDS = 0.8
SIGNED_DOWNLOAD_TIMEOUT_SECONDS = 30
CONTROLLED_NAVIGATION_TIMEOUT_MS = 15000


def _find_bundled_chromium(playwright):
    """返回 Playwright 当前解析出的 Chromium 可执行文件。"""
    try:
        executable_path = str(playwright.chromium.executable_path or "").strip()
    except Exception:
        return None
    return executable_path if executable_path and os.path.isfile(executable_path) else None


def _select_browser_executable(playwright, allow_system_chrome=False):
    """选择自动化浏览器并返回 ``(path, mode)``。

    所有运行环境默认只使用 Playwright 解析出的 Chromium，避免系统 Chrome
    的应用级窗口和 Dock 图标无法与用户会话隔离。系统 Chrome 只作为明确
    的调试/兼容性降级路径，并必须通过环境变量显式切换。
    """
    requested = os.environ.get(AUTOMATION_BROWSER_ENV, "").strip().casefold()
    if requested not in {"", "bundled", "chromium", "system", "chrome"}:
        raise XunfeiError(
            f"{AUTOMATION_BROWSER_ENV} 只支持 bundled 或 system，实际为 {requested!r}"
        )

    # 环境变量是明确的开发/兼容性覆盖；桌面设置只有在用户明确允许时
    # 才能把系统 Chrome 作为无环境变量时的降级路径打开。
    use_system = requested in {"system", "chrome"} or (
        not requested and bool(allow_system_chrome)
    )

    if use_system:
        system_path = _find_chrome()
        if system_path:
            return system_path, "system"
        if requested in {"system", "chrome"}:
            raise XunfeiError("已指定使用系统 Chrome，但未找到可执行文件")

    bundled_path = _find_bundled_chromium(playwright)
    if bundled_path:
        return bundled_path, "bundled"

    raise XunfeiError(
        "未找到随应用打包的 Playwright Chromium；请先安装 Chromium，"
        f"或在开发环境设置 {AUTOMATION_BROWSER_ENV}=system"
    )

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


def _canonical_sign_value(value):
    """按讯飞网页 Axios 拦截器的规则生成签名原文。"""
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            child = _canonical_sign_value(value[key])
            # 网页端会忽略空字符串/空值，但保留数组字段。
            if child or isinstance(value[key], list):
                parts.append(f"{key}={child}")
        return "&".join(parts)
    if isinstance(value, list):
        return ",".join(_canonical_sign_value(item) for item in value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_api_sign(param, base):
    """生成讯飞 video-api 请求需要的 sign 请求头。"""
    base_digest = hashlib.md5(
        _canonical_sign_value(base).encode("utf-8")
    ).hexdigest()
    payload = {"param": param, "base": base}
    return hashlib.md5(
        (_canonical_sign_value(payload) + base_digest).encode("utf-8")
    ).hexdigest()


def _find_chrome():
    """查找系统 Chrome 可执行文件路径。"""
    for p in _CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    if IS_MAC:
        import subprocess
        try:
            result = subprocess.run(
                ["mdfind", "kMDItemCFBundleIdentifier == 'com.google.Chrome'"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    chrome_path = os.path.join(line.strip(), "Contents", "MacOS", "Google Chrome")
                    if os.path.isfile(chrome_path):
                        return chrome_path
        except Exception:
            pass
    import shutil
    for name in ("google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


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


# ============================================================================
# 异常体系：驱动上层（word_tts_app / server.py）的控制流
# ============================================================================

class XunfeiError(RuntimeError):
    """讯飞配音引擎错误基类。"""


class XunfeiQuotaExceeded(XunfeiError):
    """额度不足：上层应立即停止整批任务。"""


class XunfeiRateLimited(XunfeiError):
    """触发频控：上层应拉长节奏后重试。"""


class XunfeiLoginRequired(XunfeiError):
    """会话失效：需要人工重新扫码登录。"""


class XunfeiSubmissionAmbiguous(XunfeiError):
    """页面已确认提交，但本轮无法安全定位唯一 worksId。

    这是一个不可自动重试的状态：再次点击生成可能会创建第二个作品并重复
    扣费。上层应持久化 works_name，下一轮只做作品列表对账，不重新提交。
    """

    def __init__(self, message, works_name=None):
        super().__init__(message)
        self.works_name = str(works_name or "").strip() or None
        self.submission_confirmed = True


class XunfeiCancelled(XunfeiError):
    """批量任务被上层取消，停止尚未提交的后续工作。"""


class XunfeiControlError(XunfeiCancelled):
    """上层控制探针异常；必须停止自动化，不能按“未取消”继续。"""


class XunfeiSessionBusy(XunfeiError):
    """当前专用浏览器已归属于另一个后端任务。"""


def _check_cancel_requested(cancel_check):
    """执行可选取消探针；探针异常必须停止，避免控制失败时继续执行。"""
    if not callable(cancel_check):
        return
    try:
        cancelled = bool(cancel_check())
    except XunfeiControlError:
        raise
    except Exception as error:
        raise XunfeiControlError(
            f"任务控制探针异常，已停止讯飞自动化：{error}"
        ) from error
    if cancelled:
        raise XunfeiCancelled("讯飞批量任务已取消，已停止后续提交")


def _controlled_wait(page, seconds, cancel_check=None, slice_seconds=0.5):
    """将长等待切成可取消/可暂停的短检查片段。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _check_cancel_requested(cancel_check)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        wait_seconds = min(remaining, max(0.05, float(slice_seconds)))
        if page is not None:
            page.wait_for_timeout(int(wait_seconds * 1000))
        else:
            time.sleep(wait_seconds)


# ============================================================================
# 发音人配置
# ============================================================================

VOICES = {
    # 女声（默认；词汇题型也使用该女声）
    "amanda": {
        "name": "Amanda",
        "display": "Amanda (英语女声)",
        "gender": "female",
        # 目录刷新失败时仍要能构造多人配音 payload。这个值与仓库内置
        # 音色种子一致；在线目录返回更完整信息后会覆盖它。
        "speaker_no": 544508087,
        "vcn_type": 1,
        "language": "英语",
    },
    # 男声
    "george": {
        "name": "George",
        "display": "George (英语男声)",
        "gender": "male",
        "speaker_no": 593031758,
        "vcn_type": 1,
        "language": "英语",
    },
}

DEFAULT_FEMALE = "amanda"
DEFAULT_MALE = "george"


def register_voice_catalog(voices):
    """注册目录中的稳定 key 与讯飞网页可见名称。"""
    if not isinstance(voices, (list, tuple)):
        return
    for voice in voices:
        if not isinstance(voice, dict):
            continue
        key = str(voice.get("key") or "").strip()
        name = str(voice.get("name") or voice.get("speakerName") or "").strip()
        if not key or not name:
            continue
        previous = VOICES.get(key) if isinstance(VOICES.get(key), dict) else {}
        gender = str(voice.get("gender") or "unknown").strip().lower()
        gender_label = "女声" if gender == "female" else ("男声" if gender == "male" else "音色")

        speaker_no = voice.get("speaker_no")
        if speaker_no in (None, ""):
            speaker_no = voice.get("speakerNo")
        if speaker_no in (None, ""):
            speaker_no = previous.get("speaker_no") or previous.get("speakerNo")
        common_id = voice.get("common_id")
        if common_id in (None, ""):
            common_id = voice.get("commonId")
        if common_id in (None, ""):
            common_id = previous.get("common_id") or previous.get("commonId")
        language = voice.get("language")
        if language in (None, ""):
            language = voice.get("speakerLanguage")
        if language in (None, ""):
            language = previous.get("language") or previous.get("speaker_language") or ""
        vcn_type = voice.get("vcn_type")
        if vcn_type in (None, ""):
            vcn_type = voice.get("vcnType")
        if vcn_type in (None, ""):
            vcn_type = previous.get("vcn_type") or previous.get("vcnType") or 1
        speaker_language = voice.get("speaker_language")
        if speaker_language in (None, ""):
            speaker_language = voice.get("speakerLanguage")
        if speaker_language in (None, ""):
            speaker_language = previous.get("speaker_language") or language or ""
        is_vip = voice.get("is_vip") if "is_vip" in voice else voice.get("isVip")
        if is_vip is None:
            is_vip = previous.get("is_vip") if "is_vip" in previous else previous.get("isVip")
        composite_name = voice.get("composite_name") or voice.get("common_name")
        if composite_name in (None, ""):
            composite_name = previous.get("composite_name") or previous.get("common_name") or name
        variant_names = voice.get("variant_names")
        if not isinstance(variant_names, (list, tuple)):
            variant_names = previous.get("variant_names") or []
        variant_keys = voice.get("variant_keys")
        if not isinstance(variant_keys, (list, tuple)):
            variant_keys = previous.get("variant_keys") or []
        composite_key = voice.get("composite_key")
        if composite_key in (None, ""):
            composite_key = previous.get("composite_key") or key
        emot_desc = voice.get("emot_desc") if "emot_desc" in voice else voice.get("emotDesc")
        if emot_desc in (None, ""):
            emot_desc = previous.get("emot_desc") or previous.get("emotDesc") or ""
        VOICES[key] = {
            "name": name,
            "display": f"{name} ({gender_label})",
            "gender": gender,
            "speaker_no": speaker_no,
            "common_id": common_id,
            "composite_name": str(composite_name).strip() or name,
            "composite_key": str(composite_key).strip() or key,
            "variant_names": [str(item).strip() for item in variant_names if str(item).strip()],
            "variant_keys": [str(item).strip() for item in variant_keys if str(item).strip()],
            "img_url": voice.get("img_url") or voice.get("imgUrl") or "",
            "language": language,
            "vcn_type": vcn_type,
            "speaker_language": speaker_language,
            "is_vip": is_vip,
            "emot_desc": str(emot_desc).strip(),
            "emot_type": voice.get("emot_type") or voice.get("emotType"),
            "emot_val": voice.get("emot_val") or voice.get("emotVal"),
        }


def get_voice_info(voice_key):
    """返回已注册音色信息，避免用未知 key 发起错误合成。"""
    key = str(voice_key or "").strip()
    if key not in VOICES:
        raise ValueError(f"未知音色 {key!r}，请刷新讯飞音色目录后重试")
    return VOICES[key]


# ============================================================================
# 页面注入 JS（集中管理）
# ============================================================================

class JS:
    CHECK_MODAL_HAS_TEXT = """
    (keywords) => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const expected = (keywords || []).map(normalize);
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (expected.every(kw => kw && text.includes(kw))) return true;
        }
        return false;
    }
    """

    CLICK_BTN_IN_MODAL = """
    (buttonText) => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const target = String(buttonText || '').replace(/\\s+/g, '');
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const btns = modal.querySelectorAll('button, [role="button"], .ant-btn');
            for (const b of btns) {
                if (!visible(b)) continue;
                const label = String(b.textContent || '').replace(/\\s+/g, '').trim();
                if (label === target) {
                    b.click();
                    return true;
                }
            }
        }
        return false;
    }
    """

    CLOSE_ALL_MODALS = """
    (excludeKeywords) => {
        const modals = document.querySelectorAll('.ant-modal');
        let closed = 0;
        for (const modal of modals) {
            const style = window.getComputedStyle(modal);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (modal.getBoundingClientRect().width === 0) continue;
            const text = modal.textContent || '';
            if (excludeKeywords.some(kw => text.includes(kw))) continue;
            const closeBtn = modal.querySelector('button.ant-modal-close, .ant-modal-close-x');
            if (closeBtn && closeBtn.offsetParent !== null) {
                closeBtn.click();
                closed++;
            }
        }
        return closed;
    }
    """

    CHECK_NO_VISIBLE_MODAL = """
    () => {
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        return !Array.from(document.querySelectorAll(
            '.ant-modal, [role="dialog"], .el-dialog, .el-message-box'
        )).some(visible);
    }
    """

    GET_EDITOR_TEXT = """
    () => {
        const editor = document.querySelector('.ssml-editor');
        return editor?.textContent?.trim() || '';
    }
    """

    GET_SELECTION_TEXT = """
    () => {
        const selection = window.getSelection?.();
        if (!selection || selection.rangeCount === 0) return '';
        const range = selection.getRangeAt(0);
        // 讯飞把 speaker 标签和正文放在同一个标注节点里。浏览器的
        // Selection.toString() 会把不可编辑的 “Amanda-教育” 标签也读出来，
        // 导致已经标注过的区间在下一次修正时被误判为选区漂移。只从选区
        // 克隆片段中移除标签元节点，保留真正的 speaker-content 正文。
        const fragment = range.cloneContents();
        fragment.querySelectorAll(
            '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
        ).forEach((node) => node.remove());
        return fragment.textContent || '';
    }
    """

    SELECT_EDITOR_RANGE = """
    ([firstIndex, lastIndex]) => {
        const paragraphs = Array.from(
            document.querySelectorAll('.ssml-editor p')
        );
        const first = paragraphs[Number(firstIndex)];
        const last = paragraphs[Number(lastIndex)];
        if (!first || !last) return null;

        const isEditorMetadataText = (node) => {
            const parent = node?.parentElement;
            return Boolean(parent?.closest(
                '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
            ));
        };
        const firstTextNode = (root) => {
            const walker = document.createTreeWalker(
                root,
                NodeFilter.SHOW_TEXT,
            );
            let current = walker.nextNode();
            while (current) {
                if (!isEditorMetadataText(current) && current.textContent?.trim()) return current;
                current = walker.nextNode();
            }
            return null;
        };
        const lastTextNode = (root) => {
            const walker = document.createTreeWalker(
                root,
                NodeFilter.SHOW_TEXT,
            );
            let current = null;
            let next = walker.nextNode();
            while (next) {
                if (!isEditorMetadataText(next) && next.textContent?.trim()) current = next;
                next = walker.nextNode();
            }
            return current;
        };

        const startNode = firstTextNode(first);
        const endNode = lastTextNode(last);
        if (!startNode || !endNode) return null;

        const range = document.createRange();
        range.setStart(startNode, 0);
        range.setEnd(endNode, endNode.textContent?.length || 0);
        const selection = window.getSelection();
        if (!selection) return null;
        selection.removeAllRanges();
        selection.addRange(range);
        const fragment = range.cloneContents();
        fragment.querySelectorAll(
            '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
        ).forEach((node) => node.remove());
        return fragment.textContent || '';
    }
    """

    SELECT_EDITOR_ROW = """
    (rowIndex) => {
        const paragraph = document.querySelectorAll('.ssml-editor p')[Number(rowIndex)];
        if (!paragraph) return null;
        paragraph.scrollIntoView({
            block: 'center',
            inline: 'nearest',
            behavior: 'instant',
        });
        const isMetadata = (node) => Boolean(
            node?.parentElement?.closest(
                '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
            )
        );
        const textNodes = [];
        const walker = document.createTreeWalker(paragraph, NodeFilter.SHOW_TEXT);
        let node = walker.nextNode();
        while (node) {
            if (!isMetadata(node) && node.textContent?.length) textNodes.push(node);
            node = walker.nextNode();
        }
        const first = textNodes[0];
        const last = textNodes[textNodes.length - 1];
        if (!first || !last) return null;
        const range = document.createRange();
        range.setStart(first, 0);
        range.setEnd(last, last.textContent?.length || 0);
        const selection = window.getSelection();
        if (!selection) return null;
        selection.removeAllRanges();
        selection.addRange(range);
        const rect = paragraph.getBoundingClientRect();
        return {
            text: range.cloneContents().textContent || '',
            box: {
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
            },
        };
    }
    """

    CLEAR_EDITOR = """
    () => {
        const editor = document.querySelector('.ssml-editor');
        if (editor) {
            editor.focus();
            editor.innerHTML = '<p><br></p>';
            editor.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        }
        return false;
    }
    """

    SET_PARAM_INPUT = """
    ([index, value]) => {
        const inputs = document.querySelectorAll('input.w-12');
        if (inputs.length <= index) return false;
        const inp = inputs[index];
        inp.focus();
        inp.value = String(value);
        inp.dispatchEvent(new Event('input', {bubbles: true}));
        inp.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    }
    """

    READ_PARAM_INPUTS = """
    () => {
        return Array.from(document.querySelectorAll('input.w-12')).slice(0, 3).map(i => i.value);
    }
    """

    CHECK_VOICE_SELECTED = """
    (name) => {
        const normalize = (value) => String(value || '').replace(/\s+/g, '').trim();
        const expected = normalize(name);
        const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const selected = (button) => {
            const ariaSelected = button.getAttribute('aria-selected');
            const style = window.getComputedStyle(button);
            const inlineStyle = String(button.getAttribute('style') || '').replace(/\s+/g, '').toLowerCase();
            const hasSelectedBorder = style.borderColor === 'rgb(26, 145, 255)'
                || inlineStyle.includes('border:1pxsolidrgb(26,145,255)')
                || inlineStyle.includes('border:1pxsolid#1a91ff');
            return ariaSelected === 'true'
                || button.classList.contains('active')
                || button.classList.contains('selected')
                || button.classList.contains('is-selected')
                || hasSelectedBorder;
        };
        const voiceLabel = (button) => {
            const label = button.querySelector('p, strong, [class*="name"], [class*="title"]');
            return normalize(label?.textContent || button.textContent);
        };
        for (const b of document.querySelectorAll('button')) {
            if (!visible(b) || !selected(b)) continue;
            const label = voiceLabel(b);
            // 先按音色卡片的主名称精确匹配，避免“Linda-品质”被另一个
            // 同名/相似名称卡片或隐藏 DOM 误判为已选中。
            if (label === expected || label.includes(expected)) return b.textContent?.trim() || label;
        }
        return null;
    }
    """

    SEARCH_AND_CLICK_VOICE = """
    (name) => {
        const normalize = (value) => String(value || '').replace(/\s+/g, '').trim();
        const expected = normalize(name);
        const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const labelOf = (button) => normalize(
            button.querySelector('p, strong, [class*="name"], [class*="title"]')?.textContent
                || button.textContent
        );
        const buttons = Array.from(document.querySelectorAll('button'))
            .filter((button) => visible(button) && labelOf(button).length < 100);
        // 搜索结果的卡片主名称优先精确匹配；只有页面没有提供独立名称节点
        // 时才退回到整张卡片包含匹配。
        const exact = buttons.find((button) => {
            const label = normalize(button.querySelector('p, strong, [class*="name"], [class*="title"]')?.textContent);
            return label === expected;
        });
        const target = exact || buttons.find((button) => labelOf(button).includes(expected));
        if (target) {
            target.click();
            return true;
        }
        return false;
    }
    """

    CHECK_SEARCH_RESULT = """
    (name) => {
        const normalize = (value) => String(value || '').replace(/\s+/g, '').trim();
        const expected = normalize(name);
        const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        for (const b of document.querySelectorAll('button')) {
            if (!visible(b)) continue;
            const label = normalize(
                b.querySelector('p, strong, [class*="name"], [class*="title"]')?.textContent
                    || b.textContent
            );
            if (label === expected || (label.includes(expected) && label.length < 100)) return true;
        }
        return false;
    }
    """

    CHECK_GO_DOWNLOAD = """
    () => {
        const els = document.querySelectorAll('a, button, span, div');
        for (const el of els) {
            if (el.children.length === 0 && el.textContent?.trim() === '去下载' && el.offsetParent !== null) {
                return true;
            }
        }
        return false;
    }
    """

    CLICK_GO_DOWNLOAD = """
    () => {
        const els = document.querySelectorAll('a, button, span, div');
        for (const el of els) {
            if (el.children.length === 0 && el.textContent?.trim() === '去下载' && el.offsetParent !== null) {
                el.click();
                return true;
            }
        }
        return false;
    }
    """

    CHECK_DOWNLOAD_PAGE = """
    () => {
        const text = String(document.body?.innerText || '').replace(/\\s+/g, '');
        const checkboxes = document.querySelectorAll('input.ant-checkbox-input, input[type="checkbox"]');
        return text.includes('作品名称') && text.includes('审核通过') && checkboxes.length > 0;
    }
    """

    GET_DOWNLOAD_ROWS = """
    () => {
        const rowFromInput = (input) => {
            let parent = input;
            for (let level = 0; parent && level < 9; level += 1) {
                const classes = Array.from(parent.classList || []);
                if (classes.some((name) => name.endsWith('__item'))) return parent;
                parent = parent.parentElement;
            }
            const button = input.closest('[class*="__botton"]');
            return button ? button.parentElement : null;
        };
        const rows = [];
        const seen = new Set();
        for (const input of document.querySelectorAll('input.ant-checkbox-input, input[type="checkbox"]')) {
            const row = rowFromInput(input);
            if (!row || seen.has(row)) continue;
            const name = row.querySelector('[class*="__name"]');
            seen.add(row);
            rows.push({
                index: rows.length,
                text: String(row.innerText || '').replace(/\\s+/g, ' ').trim(),
                works_name: String(name?.innerText || '').replace(/\\s+/g, ' ').trim(),
            });
        }
        return rows;
    }
    """

    SELECT_DOWNLOAD_ROWS = """
    (targets) => {
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const rowFromInput = (input) => {
            let parent = input;
            for (let level = 0; parent && level < 9; level += 1) {
                const classes = Array.from(parent.classList || []);
                if (classes.some((name) => name.endsWith('__item'))) return parent;
                parent = parent.parentElement;
            }
            const button = input.closest('[class*="__botton"]');
            return button ? button.parentElement : null;
        };
        const rows = [];
        const seen = new Set();
        for (const input of document.querySelectorAll('input.ant-checkbox-input, input[type="checkbox"]')) {
            const row = rowFromInput(input);
            if (!row || seen.has(row)) continue;
            seen.add(row);
            rows.push({
                row,
                input: row.querySelector('input.ant-checkbox-input, input[type="checkbox"]'),
                text: normalize(row.innerText || ''),
                name: normalize(row.querySelector('[class*="__name"]')?.innerText || ''),
            });
        }

        const used = new Set();
        const selected = [];
        const missing = [];
        for (const target of Array.isArray(targets) ? targets : []) {
            const orderNo = normalize(target?.order_no);
            const worksName = normalize(target?.works_name);
            let found = -1;
            if (orderNo) {
                found = rows.findIndex((row, index) => (
                    !used.has(index) && row.text.includes(orderNo)
                ));
            }
            if (found < 0 && Number.isInteger(target?.row_index)) {
                const index = target.row_index;
                if (index >= 0 && index < rows.length && !used.has(index)) found = index;
            }
            if (found < 0 && worksName) {
                found = rows.findIndex((row, index) => (
                    !used.has(index) && row.name === worksName
                ));
            }
            if (found < 0) {
                missing.push({
                    works_id: String(target?.works_id || ''),
                    order_no: String(target?.order_no || ''),
                    works_name: String(target?.works_name || ''),
                });
                continue;
            }

            const checkbox = rows[found].input;
            if (!checkbox) {
                missing.push({
                    works_id: String(target?.works_id || ''),
                    order_no: String(target?.order_no || ''),
                    works_name: String(target?.works_name || ''),
                });
                continue;
            }
            if (!checkbox.checked) checkbox.click();
            if (!checkbox.checked) {
                missing.push({
                    works_id: String(target?.works_id || ''),
                    order_no: String(target?.order_no || ''),
                    works_name: String(target?.works_name || ''),
                });
                continue;
            }
            used.add(found);
            selected.push({
                works_id: String(target?.works_id || ''),
                order_no: String(target?.order_no || ''),
                works_name: String(target?.works_name || ''),
                row_index: found,
            });
        }
        return {selected, missing, row_count: rows.length};
    }
    """

    SCROLL_DOWNLOAD_LIST = """
    () => {
        let moved = false;
        const containers = document.querySelectorAll(
            '[class*="__scrolledList"], [class*="scrolledList"], [class*="scroll"]'
        );
        for (const container of containers) {
            if (container.scrollHeight > container.clientHeight) {
                container.scrollTop = container.scrollHeight;
                moved = true;
            }
        }
        window.scrollTo(0, document.body.scrollHeight);
        return moved;
    }
    """

    CLICK_DOWNLOAD_PAGE_BUTTON = """
    () => {
        for (const button of document.querySelectorAll('button')) {
            const style = window.getComputedStyle(button);
            const rect = button.getBoundingClientRect();
            const label = String(button.textContent || '').replace(/\\s+/g, '').trim();
            if (label !== '下载' || style.display === 'none' || style.visibility === 'hidden'
                || rect.width === 0 || rect.height === 0 || button.disabled) continue;
            button.click();
            return true;
        }
        return false;
    }
    """

    CHECK_FREE_MODAL = """
    () => {
        const modals = document.querySelectorAll('.ant-modal');
        for (const m of modals) {
            const style = window.getComputedStyle(m);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const text = m.textContent || '';
            if (text.includes('本单免费') || text.includes('免费')) return true;
        }
        return false;
    }
    """

    CHECK_INSUFFICIENT = """
    () => {
        const body = document.body;
        if (!body) return false;
        // textContent 会包含 display:none 的模板、历史提示和隐藏弹窗，
        // 不能据此判断本次合成是否真的出现了额度错误。
        const text = body.innerText || '';
        return text.includes('余额不足') || text.includes('次数不足') || text.includes('额度不足');
    }
    """

    CHECK_RATE_LIMITED = """
    () => {
        const body = document.body;
        if (!body) return false;
        // 只读取当前页面的可见文本，避免把隐藏 DOM 中的旧提示误判为
        // 本次生成的频控错误。
        const text = body.innerText || '';
        return text.includes('操作频繁') || text.includes('稍后再试') || text.includes('请求过于频繁');
    }
    """

    PROBE_SYNTH_STATE = """
    (aiKeywordVariants) => {
        // 一轮只做一次页面扫描，供确认合成、AI 弹窗和订单等待共同使用。
        // React/Ant Design 页面可能延迟挂载，因此这里只负责“当前状态快照”，
        // Python 侧仍会持续轮询，不能把一次未命中当成页面没有弹窗。
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const modalSelector =
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box';
        const modals = Array.from(document.querySelectorAll(modalSelector))
            .filter(visible);
        const variants = Array.isArray(aiKeywordVariants) ? aiKeywordVariants : [];
        const bodyText = normalize(document.body?.innerText || '');
        let aiModal = false;
        let order = bodyText.includes('去下载');
        let free = false;
        let login = false;
        let confirm = false;
        let aiSwitch = 'not_found';
        const switchSelector = '[role="switch"], .ant-switch, button[aria-pressed]';
        const findAiSwitch = (modal) => {
            const switches = Array.from(modal.querySelectorAll(switchSelector));
            const labels = modal.querySelectorAll('span, div, label');
            const aiLabel = Array.from(labels).find((el) => (
                el.children.length === 0 && normalize(el.textContent) === 'AI标识'
            ));
            let parent = aiLabel;
            for (let level = 0; parent && level < 6; level += 1) {
                const rowSwitch = parent.querySelector(switchSelector);
                if (rowSwitch) return rowSwitch;
                parent = parent.parentElement;
            }
            return switches[0] || null;
        };

        for (const modal of modals) {
            const text = normalize(modal.innerText || modal.textContent || '');
            const isAi = variants.some(group => (
                Array.isArray(group)
                && group.length > 0
                && group.every(keyword => text.includes(normalize(keyword)))
            ));
            if (isAi) aiModal = true;
            if (text.includes('本单免费') || text.includes('免费')) free = true;
            if (
                text.includes('登录')
                && (text.includes('扫码') || text.includes('手机号') || text.includes('验证码'))
            ) login = true;

            if (text.includes('确认合成')) confirm = true;
            if (!confirm) {
                const buttons = modal.querySelectorAll('button, [role="button"]');
                for (const button of buttons) {
                    if (!visible(button)) continue;
                    if (normalize(button.innerText || button.textContent) === '确认合成') {
                        confirm = true;
                        break;
                    }
                }
            }

            // 优先按“AI 标识”所在行寻找开关；AI 说明弹窗没有 switch，
            // 且“不再提示”优先判定为说明弹窗。
            if (aiSwitch === 'not_found' && !text.includes('不再提示')) {
                const sw = findAiSwitch(modal);
                if (sw) {
                    const ariaChecked = sw.getAttribute('aria-checked');
                    const ariaPressed = sw.getAttribute('aria-pressed');
                    const isOn = ariaChecked === 'true'
                        || ariaPressed === 'true'
                        || sw.classList.contains('ant-switch-checked');
                    aiSwitch = isOn ? 'on' : 'off';
                }
            }
        }

        let state = null;
        if (aiModal) state = 'ai_modal';
        else if (bodyText.includes('余额不足') || bodyText.includes('次数不足') || bodyText.includes('额度不足')) {
            state = 'insufficient';
        } else if (bodyText.includes('操作频繁') || bodyText.includes('稍后再试') || bodyText.includes('请求过于频繁')) {
            state = 'rate_limited';
        } else if (login) {
            state = 'login';
        } else if (order || free) {
            state = 'order';
        } else if (confirm) {
            state = 'confirm';
        }

        return {
            state,
            ai_modal: aiModal,
            ai_switch: aiSwitch,
            order,
            free,
            login,
            confirm,
        };
    }
    """

    CHECK_LOGIN_MODAL = """
    () => {
        const modals = document.querySelectorAll('.ant-modal');
        for (const m of modals) {
            const style = window.getComputedStyle(m);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (m.getBoundingClientRect().width === 0) continue;
            const text = m.textContent || '';
            if ((text.includes('扫码') || text.includes('手机号') || text.includes('验证码')) && text.includes('登录')) return true;
        }
        return false;
    }
    """

    CHECK_NO_REMIND = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        for (const modal of modals) {
            if (!visible(modal)) continue;
            if (!normalize(modal.textContent || '').includes('不再提示')) continue;

            // 优先点真实 checkbox input。Ant Design 的 input 可能是透明的，
            // 不能依赖 offsetParent/可见尺寸判断它是否可点击。
            const inputs = modal.querySelectorAll('input[type="checkbox"], .ant-checkbox-input');
            for (const input of inputs) {
                if (!input.checked) {
                    input.click();
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return 'clicked_input';
                }
            }
            for (const input of inputs) {
                if (input.checked) return 'already';
            }

            // 兼容没有 input 的自定义 checkbox：按“ 不再提示 ”文字找到
            // 最近的 label / role=checkbox 容器并点击。
            const controls = modal.querySelectorAll(
                '.ant-checkbox-wrapper, label, [role="checkbox"], button'
            );
            for (const control of controls) {
                if (!normalize(control.textContent || '').includes('不再提示')) continue;
                const ariaChecked = control.getAttribute('aria-checked');
                if (ariaChecked === 'true' || control.classList.contains('ant-checkbox-checked')) {
                    return 'already';
                }
                control.click();
                return 'clicked_label';
            }
        }
        return 'not_found';
    }
    """

    CLICK_AI_SWITCH = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const switchSelector = '[role="switch"], .ant-switch, button[aria-pressed]';
        const findSwitch = (modal) => {
            const switches = Array.from(modal.querySelectorAll(switchSelector));
            // 讯飞当前 DOM 的开关和“AI 标识”文字在同一行；先按这行找，
            // 避免弹窗里存在其它开关时误点到别的设置。
            const aiLabel = Array.from(modal.querySelectorAll('*')).find((el) => {
                return el.children.length === 0 && normalize(el.textContent) === 'AI标识';
            });
            let parent = aiLabel;
            for (let level = 0; parent && level < 6; level += 1) {
                const rowSwitch = parent.querySelector(switchSelector);
                if (rowSwitch) return rowSwitch;
                parent = parent.parentElement;
            }
            return switches[0] || null;
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (!text.includes('作品设置') && !text.includes('确认合成') && !text.includes('作品名称')) continue;
            if (text.includes('不再提示')) continue;
            const sw = findSwitch(modal);
            if (!sw) continue;
            if (!visible(sw)) continue;
            const ariaChecked = sw.getAttribute('aria-checked');
            const ariaPressed = sw.getAttribute('aria-pressed');
            const isOn = ariaChecked === 'true'
                || ariaPressed === 'true'
                || sw.classList.contains('ant-switch-checked');
            if (!isOn) {
                return 'already_off';
            }
            // 直接调用真实 button 的 click，确保 React/Ant Design 的事件
            // 处理器收到的是 button[role=switch] 的点击，而不是只点内部装饰节点。
            sw.click();
            return 'clicked';
        }
        return 'not_found';
    }
    """

    SET_MP3_FORMAT = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const labelText = (input) => {
            const label = input.closest('label');
            if (label) return normalize(label.textContent || '').toLowerCase();
            return normalize(input.parentElement?.textContent || '').toLowerCase();
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            const radios = Array.from(
                modal.querySelectorAll('input[type="radio"][name="exportFormat"]')
            );
            // AI 说明弹窗的正文也会提到“作品设置”，必须同时要求真实
            // exportFormat 单选项，避免把说明弹窗误当成作品设置弹窗。
            if (!text.includes('作品设置') || radios.length === 0) continue;

            const mp3 = radios.find((input) => {
                const value = normalize(input.value).toLowerCase();
                const label = labelText(input);
                return value === 'mp3'
                    || label === 'mp3'
                    || label.startsWith('mp3');
            });
            if (!mp3) {
                return {
                    status: 'mp3_not_found',
                    checked: false,
                    radio_count: radios.length,
                };
            }
            if (mp3.disabled) {
                return {
                    status: 'mp3_disabled',
                    checked: Boolean(mp3.checked),
                    radio_count: radios.length,
                };
            }
            if (mp3.checked) {
                return {
                    status: 'already_mp3',
                    checked: true,
                    radio_count: radios.length,
                };
            }

            // 必须点击真实 radio input/label，让 React/Ant Design 的受控
            // 状态更新；不能只给 checked 属性赋值。
            mp3.click();
            if (!mp3.checked) {
                const label = mp3.closest('label');
                if (label) label.click();
            }
            return {
                status: 'clicked_mp3',
                checked: Boolean(mp3.checked),
                radio_count: radios.length,
            };
        }
        return {status: 'not_found', checked: false, radio_count: 0};
    }
    """

    GET_MP3_FORMAT = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const labelText = (input) => {
            const label = input.closest('label');
            if (label) return normalize(label.textContent || '').toLowerCase();
            return normalize(input.parentElement?.textContent || '').toLowerCase();
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            const radios = Array.from(
                modal.querySelectorAll('input[type="radio"][name="exportFormat"]')
            );
            if (!text.includes('作品设置') || radios.length === 0) continue;
            const mp3 = radios.find((input) => {
                const value = normalize(input.value).toLowerCase();
                const label = labelText(input);
                return value === 'mp3'
                    || label === 'mp3'
                    || label.startsWith('mp3');
            });
            if (!mp3) {
                return {
                    status: 'mp3_not_found',
                    checked: false,
                    radio_count: radios.length,
                };
            }
            return {
                status: mp3.checked ? 'mp3' : 'other',
                checked: Boolean(mp3.checked),
                radio_count: radios.length,
            };
        }
        return {status: 'not_found', checked: false, radio_count: 0};
    }
    """

    CHECK_AI_SWITCH_OFF = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const switchSelector = '[role="switch"], .ant-switch, button[aria-pressed]';
        const findSwitch = (modal) => {
            const switches = Array.from(modal.querySelectorAll(switchSelector));
            const aiLabel = Array.from(modal.querySelectorAll('*')).find((el) => {
                return el.children.length === 0 && normalize(el.textContent) === 'AI标识';
            });
            let parent = aiLabel;
            for (let level = 0; parent && level < 6; level += 1) {
                const rowSwitch = parent.querySelector(switchSelector);
                if (rowSwitch) return rowSwitch;
                parent = parent.parentElement;
            }
            return switches[0] || null;
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (!text.includes('作品设置') && !text.includes('确认合成') && !text.includes('作品名称')) continue;
            if (text.includes('不再提示')) continue;
            const sw = findSwitch(modal);
            if (!sw || !visible(sw)) continue;
            const ariaChecked = sw.getAttribute('aria-checked');
            const ariaPressed = sw.getAttribute('aria-pressed');
            const isOn = ariaChecked === 'true'
                || ariaPressed === 'true'
                || sw.classList.contains('ant-switch-checked');
            return !isOn;
        }
        return false;
    }
    """

    GET_AI_SWITCH_STATE = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const switchSelector = '[role="switch"], .ant-switch, button[aria-pressed]';
        const findSwitch = (modal) => {
            const switches = Array.from(modal.querySelectorAll(switchSelector));
            const aiLabel = Array.from(modal.querySelectorAll('*')).find((el) => {
                return el.children.length === 0 && normalize(el.textContent) === 'AI标识';
            });
            let parent = aiLabel;
            for (let level = 0; parent && level < 6; level += 1) {
                const rowSwitch = parent.querySelector(switchSelector);
                if (rowSwitch) return rowSwitch;
                parent = parent.parentElement;
            }
            return switches[0] || null;
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (!text.includes('作品设置') && !text.includes('确认合成') && !text.includes('作品名称')) continue;
            if (text.includes('不再提示')) continue;
            const sw = findSwitch(modal);
            if (!sw || !visible(sw)) continue;
            const ariaChecked = sw.getAttribute('aria-checked');
            const ariaPressed = sw.getAttribute('aria-pressed');
            const isOn = ariaChecked === 'true'
                || ariaPressed === 'true'
                || sw.classList.contains('ant-switch-checked');
            return isOn ? 'on' : 'off';
        }
        return 'not_found';
    }
    """

    CLICK_AI_CONFIRM = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const confirmLabels = new Set(['确认', '确定', '知道了', '我知道了', '继续']);
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (!text.includes('不再提示')) continue;
            const btns = modal.querySelectorAll('button, [role="button"], .ant-btn');
            for (const b of btns) {
                if (!visible(b)) continue;
                const label = normalize(b.textContent || '');
                if (confirmLabels.has(label)) { b.click(); return true; }
            }
        }
        return false;
    }
    """

    SNAPSHOT_DIALOGS = """
    () => {
        const roots = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        return Array.from(roots)
            .filter(visible)
            .map((root) => ({
                className: String(root.className || '').slice(0, 160),
                text: String(root.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 500),
                buttons: Array.from(root.querySelectorAll('button, [role="button"]'))
                    .filter(visible)
                    .map((button) => String(button.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean)
                    .slice(0, 12),
                checkboxes: Array.from(root.querySelectorAll('input[type="checkbox"], [role="checkbox"]'))
                    .map((input) => ({
                        checked: Boolean(input.checked) || input.getAttribute('aria-checked') === 'true',
                        className: String(input.className || '').slice(0, 100),
                    }))
                    .slice(0, 8),
            }));
    }
    """

    GET_API_CREDENTIALS = """
    () => {
        const readCookie = (name) => {
            try {
                if (typeof window.getCookie === 'function') {
                    return window.getCookie(name) || '';
                }
            } catch (_) {}
            const prefix = `${name}=`;
            const item = document.cookie.split('; ').find(v => v.startsWith(prefix));
            return item ? decodeURIComponent(item.slice(prefix.length)) : '';
        };
        let sessid = '';
        try {
            if (typeof window.getSessid === 'function') {
                sessid = window.getSessid() || '';
            }
        } catch (_) {}
        const fromSpread = readCookie('XF_FTYPE')
            || readCookie('fromSpread')
            || String(window._fromSpread || '');
        return {userId: readCookie('uid'), sessid, fromSpread};
    }
    """

    POST_API_JSON = """
    async ([url, param, base, headers]) => {
        try {
            const response = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: Object.assign({'Content-Type': 'application/json'}, headers || {}),
                body: JSON.stringify({param, base})
            });
            let data = null;
            try { data = await response.json(); } catch (_) {}
            return {httpStatus: response.status, data};
        } catch (error) {
            return {httpStatus: 0, error: String(error)};
        }
    }
    """

# AI 标识弹窗关键词变体（文案可能变化，逐个尝试）
AI_FLAG_KEYWORD_VARIANTS = [
    ["AI", "标识", "不再提示"],
    ["AI", "标识", "说明"],
    ["人工智能", "不再提示"],
    ["AI生成", "不再提示"],
    ["标识", "不再提示"],
    ["AI", "不再提示"],
    ["不再提示"],
]


def _poll(
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
    if callable(cancel_check):
        upper_interval = max(
            current_interval,
            min(upper_interval, CONTROL_POLL_MAX_INTERVAL_SECONDS),
        )
    while True:
        _check_cancel_requested(cancel_check)
        try:
            result = check_fn()
            if result:
                return result
        except (XunfeiCancelled, XunfeiControlError):
            raise
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
        if callable(cancel_check):
            sleep_s = min(sleep_s, CONTROL_POLL_MAX_INTERVAL_SECONDS)
        if page is not None:
            page.wait_for_timeout(int(sleep_s * 1000))
        else:
            time.sleep(sleep_s)
        current_interval = min(upper_interval, current_interval * 1.35)
    return None


def _safe_eval(page, script, arg=None):
    try:
        if arg is not None:
            return page.evaluate(script, arg)
        return page.evaluate(script)
    except Exception:
        return None


def _probe_synth_state(page):
    """一次读取讯飞页面状态，避免同一轮重复执行多次 DOM 全量扫描。"""
    result = _safe_eval(page, JS.PROBE_SYNTH_STATE, AI_FLAG_KEYWORD_VARIANTS)
    return result if isinstance(result, dict) else None


def _looks_like_mp3(path):
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    if len(head) < 2:
        return False
    if head[:3] == b"ID3":
        return True
    return head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def _normalize_download_label(value):
    """规范化作品名/浏览器文件名，供下载兜底匹配使用。"""
    name = os.path.basename(str(value or "")).strip()
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", name).casefold()


class XunFeiSession:
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

    def __init__(self, voice_key="amanda"):
        voice_info = get_voice_info(voice_key)
        self.voice_key = str(voice_key)
        self.voice_name = voice_info["name"]
        self.voice_display = voice_info["display"]
        self._playwright = None
        self._ctx = None
        self._page = None
        self._logged_in = False
        self._browser_executable_path = None
        self._browser_mode = None
        self._browser_controller = None
        self._browser_started_at = None
        self._browser_identity_lock = threading.RLock()
        self._browser_pid = None
        self._browser_process_ids = []
        self._browser_process_ids_before = set()
        self._browser_window_handles = []
        self._browser_page_count = 0
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

    # ------------------------------------------------------------------
    # 拟人行为辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _pause(page, base, spread=0.4, cancel_check=None):
        """拟人等待 base±spread 秒，并在任务控制等待时保持可中断。"""
        seconds = max(0.05, base + ((time.time() * 7) % 1) * 2 * spread - spread)
        _controlled_wait(page, seconds, cancel_check=cancel_check)

    @staticmethod
    def _type_text(page, text):
        """
        在已聚焦的编辑器中一次性插入文本，等价于用户粘贴。

        讯飞编辑器对长文本逐字符击键非常慢，也容易让页面在输入期间
        进入半更新状态；这里不再按字符调用 keyboard.type。若编辑器不
        接受键盘插入，再用 contenteditable 的 fill 做一次性兜底。
        """
        value = str(text or "")
        try:
            page.keyboard.insert_text(value)
            return True
        except Exception as exc:
            _log(f"[xunfei]   一次性插入文本失败，尝试编辑器填充: {exc}")
        try:
            page.locator(".ssml-editor").first.fill(value, timeout=5000)
            return True
        except Exception as exc:
            _log(f"[xunfei]   编辑器一次性填充失败: {exc}")
            return False

    # ------------------------------------------------------------------
    # worksId 捕获
    # ------------------------------------------------------------------

    def _remember_api_request(self, request):
        """记录网页真实请求中的认证信息，供列表/签名接口复用。"""
        try:
            payload = request.post_data_json
        except Exception:
            payload = None
        if isinstance(payload, dict):
            base = payload.get("base")
            if isinstance(base, dict):
                with self._works_lock:
                    for key in ("appid", "channelId", "userId", "osid"):
                        if key in base and base[key] not in (None, ""):
                            self._api_base[key] = base[key]

        try:
            headers = request.all_headers()
        except Exception:
            headers = {}
        authorization = headers.get("authorization") or headers.get("Authorization")
        if authorization:
            with self._works_lock:
                self._api_authorization = authorization

    def _on_request(self, request):
        """记录提交请求序号，供 response 事件做跨任务隔离。"""
        url = str(getattr(request, "url", "") or "")
        if "makeMultipleSpeakerWork" not in url and "order_gen" not in url:
            return
        # Playwright 不同版本可能为 response.request 返回新的 Python
        # 包装对象；优先比较稳定的底层实现对象，测试桩则退回自身。
        request_token = getattr(request, "_impl_obj", None)
        if request_token is None:
            request_token = request
        with self._works_lock:
            self._submission_request_sequence += 1
            sequence = self._submission_request_sequence
            self._submission_requests.append((request_token, sequence))
            if len(self._submission_requests) > MAX_TRACKED_SUBMISSION_REQUESTS:
                del self._submission_requests[:-MAX_TRACKED_SUBMISSION_REQUESTS]

    def _submission_sequence_for_request(self, request):
        """返回 response 对应的提交 request 序号；测试桩/旧页面可返回 None。"""
        if request is None:
            return None
        request_token = getattr(request, "_impl_obj", None)
        if request_token is None:
            request_token = request
        with self._works_lock:
            for tracked_request, sequence in reversed(self._submission_requests):
                if tracked_request is request_token:
                    return sequence
        return None

    def _on_response(self, response):
        url = response.url
        try:
            request = getattr(response, "request", None)
            is_submission_response = (
                "makeMultipleSpeakerWork" in url or "order_gen" in url
            )
            request_sequence = self._submission_sequence_for_request(request)
            if is_submission_response and request_sequence is not None:
                with self._works_lock:
                    if request_sequence <= self._submission_request_cutoff:
                        _log(
                            f"[xunfei]   忽略跨任务延迟 worksId response: "
                            f"sequence={request_sequence}, "
                            f"cutoff={self._submission_request_cutoff}"
                        )
                        return
            self._remember_api_request(response.request)
            wid = None
            is_final_work = False
            is_temporary_work = False
            if "makeMultipleSpeakerWork" in url:
                data = response.json()
                response_code = data.get("retCode")
                if response_code is None:
                    response_code = data.get("code")
                if _provider_success_code(response_code):
                    temporary_id = data.get("tempWorksId")
                    formal_id = data.get("worksId")
                    # 某些版本只返回 tempWorksId，另一些版本直接返回
                    # worksId；只有明确标为 temp 的值才进入临时 ID 保护。
                    wid = formal_id or temporary_id
                    is_final_work = bool(formal_id)
                    is_temporary_work = bool(temporary_id and not formal_id)
            elif "order_gen" in url:
                data = response.json()
                response_code = data.get("code")
                if response_code is None:
                    response_code = data.get("retCode")
                if _provider_success_code(response_code):
                    wid = (data.get("data") or {}).get("payOrder", {}).get("worksId")
                    is_final_work = True
            elif "get_work_sign_url" in url:
                data = response.json()
                response_code = data.get("code")
                if response_code is None:
                    response_code = data.get("retCode")
                if _provider_success_code(response_code) and (data.get("data") or {}).get("url"):
                    sign_works_id = None
                    try:
                        request_payload = response.request.post_data_json
                        sign_works_id = (request_payload.get("param", {}) or {}).get("worksId")
                    except Exception:
                        pass
                    if sign_works_id:
                        with self._works_lock:
                            self._sign_urls.append(
                                (str(sign_works_id), data["data"]["url"], time.time())
                            )
            if wid:
                with self._works_lock:
                    entry = (wid, time.time())
                    self._works_entries.append(entry)
                    if is_final_work:
                        self._final_works_entries.append(entry)
                    if is_temporary_work:
                        self._temporary_works_entries.append(entry)
                _log(f"[xunfei]   📝 捕获 worksId: {wid}")
        except Exception:
            pass

    def _mark_works_cutoff(self):
        """每条任务开始前调用：只接受本次任务发起的提交响应。"""
        with self._works_lock:
            self._works_entries.clear()
            self._final_works_entries.clear()
            self._temporary_works_entries.clear()
            self._sign_urls.clear()
            self._works_cutoff = time.time()
            self._submission_request_cutoff = self._submission_request_sequence

    @staticmethod
    def _duplicate_pending_work_ids(pending_items):
        """返回本批次重复的 worksId，禁止后续按字典键静默覆盖。"""
        counts = {}
        for item in pending_items or []:
            works_id = str(item.get("works_id") or "").strip()
            if works_id:
                counts[works_id] = counts.get(works_id, 0) + 1
        return {works_id for works_id, count in counts.items() if count > 1}

    def _consume_works_id(self, timeout=12, exclude_ids=None, cancel_check=None):
        excluded = {
            str(value)
            for value in (exclude_ids or [])
            if value not in (None, "")
        }
        deadline = time.time() + timeout
        while time.time() < deadline:
            _check_cancel_requested(cancel_check)
            with self._works_lock:
                fresh_final = [
                    e for e in self._final_works_entries
                    if e[1] >= self._works_cutoff - 0.5
                    and str(e[0]) not in excluded
                ]
                fresh = [
                    e for e in self._works_entries
                    if e[1] >= self._works_cutoff - 0.5
                    and str(e[0]) not in excluded
                ]
                # 同一次可见页面提交可能先捕获 tempWorksId，随后捕获
                # order_gen 的正式 worksId。正式 ID 才能稳定出现在作品页，
                # 必须优先于“最新到达”的临时 ID。
                candidate = fresh_final[-1] if fresh_final else (fresh[-1] if fresh else None)
                temporary_ids = {
                    str(e[0]) for e in self._temporary_works_entries
                    if e[1] >= self._works_cutoff - 0.5
                    and str(e[0]) not in excluded
                }
                temporary_only = (
                    candidate is not None
                    and not fresh_final
                    and str(candidate[0]) in temporary_ids
                )
                if candidate and not temporary_only:
                    wid = candidate[0]
                    self._works_entries.clear()
                    self._final_works_entries.clear()
                    self._temporary_works_entries.clear()
                    return wid
            # 作品 worksId 是 Playwright response 监听器异步写入的。同步
            # API 线程如果用 time.sleep，会阻塞事件分发，导致已经成功的
            # 提交直到超时后才被回调，随后上层误触发整条作品重试。让页面
            # 自己等待 100ms 可同时泵动真实浏览器事件；无页面时再退回
            # 普通 sleep（便于单元测试和关闭阶段调用）。
            if self._page is not None:
                try:
                    self._page.wait_for_timeout(100)
                    continue
                except Exception:
                    pass
            _check_cancel_requested(cancel_check)
            time.sleep(0.1)
        return None

    # ------------------------------------------------------------------
    # 页面基础操作
    # ------------------------------------------------------------------

    def _is_logged_in(self, page):
        """检测是否已登录：登录按钮消失 = 已登录"""
        try:
            btns = page.locator("button")
            for i in range(min(btns.count(), 30)):
                try:
                    txt = btns.nth(i).inner_text().strip()
                    if txt == "登录":
                        return False
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def _clear_editor(self, page, cancel_check=None):
        """清空文本编辑器内容。"""
        _check_cancel_requested(cancel_check)
        _safe_eval(page, JS.CLEAR_EDITOR)
        _controlled_wait(page, 0.2, cancel_check=cancel_check)
        actual = _safe_eval(page, JS.GET_EDITOR_TEXT)
        if actual:
            try:
                _check_cancel_requested(cancel_check)
                page.locator(".ssml-editor").first.click(timeout=3000)
                page.keyboard.press(_SELECT_ALL)
                page.keyboard.press("Backspace")
                _controlled_wait(page, 0.2, cancel_check=cancel_check)
            except XunfeiCancelled:
                raise
            except Exception:
                pass

    def _input_text(self, page, text, cancel_check=None):
        """在编辑器中拟人输入文本并验证。"""
        _check_cancel_requested(cancel_check)
        self._clear_editor(page, cancel_check=cancel_check)
        page.locator(".ssml-editor").first.click(timeout=5000)
        self._pause(page, 0.15, 0.08, cancel_check=cancel_check)
        page.keyboard.press(_SELECT_ALL)
        page.keyboard.press("Backspace")
        self._pause(page, 0.1, 0.05, cancel_check=cancel_check)
        self._type_text(page, text)
        _check_cancel_requested(cancel_check)
        _controlled_wait(page, 0.15, cancel_check=cancel_check)

        for attempt in range(2):
            _check_cancel_requested(cancel_check)
            actual = _safe_eval(page, JS.GET_EDITOR_TEXT) or ""
            if len(actual) >= len(text) * 0.85:
                return True
            _log(f"[xunfei]   输入验证失败 (attempt {attempt + 1})，重试...")
            self._clear_editor(page, cancel_check=cancel_check)
            page.locator(".ssml-editor").first.click(timeout=5000)
            self._type_text(page, text)
            _controlled_wait(page, 0.15, cancel_check=cancel_check)
        return False

    @staticmethod
    def _clear_editor_with_keyboard(page, cancel_check=None):
        """只用真实键盘操作清空编辑器，供多人配音 UI 流程使用。"""
        _check_cancel_requested(cancel_check)
        # 讯飞失败重试时可能还留着 ssml-float-bar；先用键盘收起它，
        # 避免真实 editor.click 被浮动条遮挡。
        page.keyboard.press("Escape")
        _controlled_wait(page, 0.03, cancel_check=cancel_check, slice_seconds=0.03)
        editor = page.locator(".ssml-editor").first
        editor.click(timeout=5000)
        page.keyboard.press(_SELECT_ALL)
        page.keyboard.press("Backspace")
        _controlled_wait(page, 0.2, cancel_check=cancel_check)
        _check_cancel_requested(cancel_check)
        paragraphs = page.locator(".ssml-editor p")
        remaining = []
        for index in range(paragraphs.count()):
            paragraph = paragraphs.nth(index)
            text = paragraph.inner_text(timeout=1000)
            # ProseMirror 空编辑器会显示 contenteditable=false 的占位符，
            # 它属于 UI 提示而不是用户文本，不能把它误判成清空失败。
            placeholders = paragraph.locator(".ssml-editor-placeholder")
            for placeholder_index in range(placeholders.count()):
                placeholder_text = placeholders.nth(placeholder_index).inner_text(
                    timeout=500
                )
                text = text.replace(placeholder_text, "")
            text = text.strip()
            if text:
                remaining.append(text)
        if remaining:
            raise XunfeiError(
                "讯飞编辑器未能通过键盘清空，停止多人配音 UI 操作"
            )

    @classmethod
    def _read_editor_paragraphs(cls, page):
        """读取编辑器的可见段落文本，不修改页面。"""
        paragraphs = page.locator(".ssml-editor p")
        values = []
        for index in range(paragraphs.count()):
            paragraph = paragraphs.nth(index)
            text = paragraph.inner_text(timeout=1000)
            placeholders = paragraph.locator(".ssml-editor-placeholder")
            for placeholder_index in range(placeholders.count()):
                placeholder_text = placeholders.nth(placeholder_index).inner_text(
                    timeout=500
                )
                text = text.replace(placeholder_text, "")
            values.append(text)
        return values

    @classmethod
    def _input_composite_text(cls, page, rows, cancel_check=None):
        """把多人配音的逻辑行按真实编辑器段落输入并回读。"""
        _check_cancel_requested(cancel_check)
        values = [str(row.get("text") or "") for row in rows]
        if not values or any(not value.strip() for value in values):
            raise XunfeiError("多人配音 UI 文本包含空行，无法安全定位选区")
        cls._clear_editor_with_keyboard(page, cancel_check=cancel_check)
        editor = page.locator(".ssml-editor").first
        editor.click(timeout=5000)
        cls._type_text(page, "\n".join(values))
        _check_cancel_requested(cancel_check)
        _controlled_wait(page, 0.25, cancel_check=cancel_check)
        actual = cls._read_editor_paragraphs(page)
        if len(actual) != len(values):
            raise XunfeiError(
                "多人配音 UI 文本段落数量校验失败："
                f"期望 {len(values)}，实际 {len(actual)}"
            )
        for index, (expected, received) in enumerate(zip(values, actual)):
            if received.strip() != expected.strip():
                raise XunfeiError(
                    f"多人配音 UI 文本第 {index + 1} 行校验失败："
                    f"期望 {expected!r}，实际 {received!r}"
                )
        return True

    @staticmethod
    def _normalize_selection_text(value):
        return re.sub(r"\s+", "", str(value or ""))

    @classmethod
    def _verify_editor_selection(cls, page, expected_values):
        """校验当前浏览器选区恰好覆盖目标行，禁止误选全文。"""
        selected = _safe_eval(page, JS.GET_SELECTION_TEXT) or ""
        expected = "".join(str(value or "") for value in expected_values)
        if cls._normalize_selection_text(selected) != cls._normalize_selection_text(expected):
            raise XunfeiError(
                "多人配音 UI 选区校验失败："
                f"期望 {expected!r}，实际 {selected!r}；已停止以免误套用音色"
            )
        return selected

    @classmethod
    def _select_editor_rows(
        cls, page, rows, first_index, last_index, cancel_check=None
    ):
        """通过真实页面选区选中一行或一段连续逻辑行。

        讯飞编辑器通常会把多行文本放进可滚动的 contenteditable 中。
        仅靠一次从首行拖到末行的鼠标动作，在长文档或打包客户端的小窗口
        中很容易因为滚动导致首尾不同时可见，进而误选或漏选。这里按真实
        浏览器交互的可靠性依次尝试 Shift-click、鼠标拖选，最后才用页面
        Range 重新建立同一个浏览器选区；三种方式都必须通过精确文本回读。
        任何方式都失败时直接停止，不能把一个本应批量设置的组拆成逐行操作。
        """
        _check_cancel_requested(cancel_check)
        if first_index < 0 or last_index < first_index or last_index >= len(rows):
            raise XunfeiError("多人配音 UI 选区索引越界")
        paragraphs = page.locator(".ssml-editor p")
        if paragraphs.count() != len(rows):
            raise XunfeiError(
                "多人配音 UI 选区前段落数量已变化，拒绝继续操作"
            )

        first = paragraphs.nth(first_index)
        last = paragraphs.nth(last_index)
        expected_values = [row["text"] for row in rows[first_index:last_index + 1]]
        if first_index == last_index:
            # Playwright 的 select_text 只选当前段落，绝不退化为编辑器全选。
            _check_cancel_requested(cancel_check)
            first.select_text(timeout=5000)
            page.wait_for_timeout(80)
            return cls._verify_editor_selection(page, expected_values)

        errors = []

        def paragraph_text_target(paragraph):
            # 讯飞完成一次音色标记后，段落开头会多出一个不可编辑的
            # speaker 标签。直接对整个 <p> 执行 select_text() 会把这个
            # 标签当成选区起点，页面有时会因此把后续音色套用到错误范围。
            # 优先只选真正可编辑的正文 span；未标注段落仍使用 <p> 本身。
            content = paragraph.locator(
                'span.range-annotation-content.speaker-content'
                ':not(.ssml-tag):not([data-type="range_anchor"]):visible'
            )
            try:
                if content.count() == 1:
                    return content.first
            except Exception:
                pass
            return paragraph

        # 方式一：先真实选中首行，再滚动到末行并 Shift-click。这个动作
        # 不要求首尾同时出现在视口中，最适合打包客户端的窄窗口和长文档。
        try:
            _check_cancel_requested(cancel_check)
            first_target = paragraph_text_target(first)
            last_target = paragraph_text_target(last)
            first_target.scroll_into_view_if_needed(timeout=5000)
            first_target.select_text(timeout=5000)
            last_target.scroll_into_view_if_needed(timeout=5000)
            last_box = last_target.bounding_box()
            if not last_box:
                raise XunfeiError("末行不可见，无法执行 Shift-click")
            last_target.click(
                position={
                    "x": max(2, last_box["width"] - 2),
                    "y": max(2, last_box["height"] - 2),
                },
                modifiers=["Shift"],
                timeout=5000,
            )
            page.wait_for_timeout(120)
            selected = cls._verify_editor_selection(page, expected_values)
            _log(
                f"[xunfei]   多人配音批量选区行 {first_index + 1}-"
                f"{last_index + 1}（Shift-click）"
            )
            return selected
        except XunfeiCancelled:
            raise
        except Exception as error:
            errors.append(f"Shift-click: {error}")

        # 方式二：短范围仍优先使用真实鼠标拖选，兼容讯飞页面没有稳定
        # 锚点行为的版本。只有首尾都在当前视口时才执行，避免跨滚动拖选。
        try:
            _check_cancel_requested(cancel_check)
            first_target = paragraph_text_target(first)
            last_target = paragraph_text_target(last)
            first_target.scroll_into_view_if_needed(timeout=5000)
            last_target.scroll_into_view_if_needed(timeout=5000)
            first_box = first_target.bounding_box()
            last_box = last_target.bounding_box()
            if not first_box or not last_box:
                raise XunfeiError("首尾行不可同时看见，无法执行鼠标拖选")
            start = {
                "x": first_box["x"] + 2,
                # 从首段第一行附近开始，长句换行时不能从段落中间起拖。
                "y": first_box["y"] + 2,
            }
            end = {
                "x": max(last_box["x"] + 2, last_box["x"] + last_box["width"] - 2),
                # 到末段最后一行附近结束，避免漏选长句的尾音文本。
                "y": max(last_box["y"] + 2, last_box["y"] + last_box["height"] - 2),
            }
            page.mouse.move(start["x"], start["y"])
            page.mouse.down()
            page.mouse.move(end["x"], end["y"], steps=8)
            page.mouse.up()
            page.wait_for_timeout(120)
            selected = cls._verify_editor_selection(page, expected_values)
            _log(
                f"[xunfei]   多人配音批量选区行 {first_index + 1}-"
                f"{last_index + 1}（鼠标拖选）"
            )
            return selected
        except XunfeiCancelled:
            raise
        except Exception as error:
            errors.append(f"鼠标拖选: {error}")

        # 方式三：仍然只改变浏览器当前 Selection，不调用讯飞接口，也不
        # 修改编辑器内容。它是跨滚动场景的页面交互兜底，后续“使用”按钮
        # 仍由页面 UI 读取这个选区并产生 speaker 标记。
        try:
            _check_cancel_requested(cancel_check)
            selected = _safe_eval(
                page,
                JS.SELECT_EDITOR_RANGE,
                [first_index, last_index],
            )
            page.wait_for_timeout(120)
            verified = cls._verify_editor_selection(page, expected_values)
            _log(
                f"[xunfei]   多人配音批量选区行 {first_index + 1}-"
                f"{last_index + 1}（页面选区兜底）"
            )
            return verified
        except XunfeiCancelled:
            raise
        except Exception as error:
            errors.append(f"页面选区兜底: {error}")

        detail = "；".join(str(error) for error in errors[-3:])
        raise XunfeiError(
            f"多人配音 UI 批量选区失败：行 {first_index + 1}-{last_index + 1}；{detail}"
        )

    @classmethod
    def _read_composite_queue_count(cls, page):
        """读取讯飞页面多段选择队列数量。

        页面在编辑器滚动后可能暂时不渲染浮动的 ``已选 N 段`` 徽标，
        但仍会保留每个选区对应的 ``.msq-pending-range`` 装饰节点。
        徽标和装饰节点都属于网页 UI 状态，后者作为同一页面交互的回读
        兜底，避免长文档被误判为空队列。
        """
        try:
            # 浮动工具条在滚动期间可能短暂隐藏，但队列状态仍然保留；
            # 读取隐藏条的文本比把短暂不可见误判成队列已清空更安全。
            pending = page.locator(".msq-pending-range")
            pending_count = pending.count()
            if pending_count > 0:
                # 连续范围的一次拖选在徽标中计为 1 个队列区间，但页面
                # 装饰节点会按实际段落各保留一个；这里校验段落覆盖数，
                # 才能确认没有漏掉连续范围中的第二行。
                return pending_count

            badge = page.locator(".msq-queue-badge")
            for index in range(badge.count()):
                text = badge.nth(index).inner_text(timeout=1000)
                match = re.search(r"已选\s*(\d+)\s*段", text or "")
                if match:
                    return int(match.group(1))
            return 0
        except Exception:
            return 0

    @classmethod
    def _clear_composite_queue(cls, page, cancel_check=None):
        """清空讯飞网页的多段选区队列，不改动编辑器文本。"""
        try:
            _check_cancel_requested(cancel_check)
            # 选区浮动条本身会拦截 editor.click。先用真实键盘 Escape
            # 收起工具条并清掉队列，只有页面仍保留待处理段落时才需要
            # 再点击编辑器确认焦点。
            page.keyboard.press("Escape")
            if cls._read_composite_queue_count(page) == 0:
                return True
            _controlled_wait(page, 0.02, cancel_check=cancel_check, slice_seconds=0.02)
            if cls._read_composite_queue_count(page) == 0:
                return True
            _check_cancel_requested(cancel_check)
            editor = page.locator(".ssml-editor").first
            editor.click(timeout=3000)
            page.keyboard.press("Escape")
        except XunfeiCancelled:
            raise
        except Exception:
            return False
        return bool(_poll(
            lambda: cls._read_composite_queue_count(page) == 0,
            timeout=3,
            interval=0.1,
            page=page,
            cancel_check=cancel_check,
        ))

    @classmethod
    def _select_composite_queue_rows(
        cls, page, rows, ranges, *, native=False, cancel_check=None
    ):
        """用讯飞网页真实的 Command/Ctrl 多选队列加入多个不连续区间。

        讯飞的多段队列只在真实 pointerup 带有 Command/Ctrl 修饰键时生效，
        不能用一次全选替代。因此正常路径先在当前真实页面中用 Range 精确
        建立一行正文选区，再用带修饰键的真实鼠标 pointerup 加入队列；这
        比 Playwright 对每行执行 select_text 少一次编辑器节点往返。最终
        “使用”动作仍只执行一次。Range 路径只负责建立浏览器当前选区，若
        页面版本没有正确接受它，调用方会清空队列并切回原生 select_text。
        """
        _check_cancel_requested(cancel_check)
        normalized_ranges = [
            (int(first), int(last))
            for first, last in ranges
            if int(first) <= int(last)
        ]
        if not normalized_ranges:
            raise XunfeiError("多人配音多段选区没有可加入的目标区间")
        if cls._read_composite_queue_count(page) != 0:
            raise XunfeiError("多人配音多段选区开始前仍有上一组待处理选区")
        if any(
            first < 0 or last >= len(rows)
            for first, last in normalized_ranges
        ):
            raise XunfeiError("多人配音多段选区索引越界")

        paragraphs = page.locator(".ssml-editor p")
        if paragraphs.count() != len(rows):
            raise XunfeiError(
                "多人配音多段选区前段落数量已变化，拒绝继续操作"
            )

        def paragraph_text_target(paragraph):
            # 这里每次都会先由 _input_composite_text 清空编辑器，待标注
            # 的目标段落不含 speaker 标签；直接操作 <p> 可省掉每行一次
            # 子节点计数往返。最终套用音色后仍用整组 DOM 回读校验正文。
            return paragraph

        def select_exact_text(row_index):
            """用页面 Range 或浏览器原生方式选中一整行正文。"""
            _check_cancel_requested(cancel_check)
            target = paragraph_text_target(paragraphs.nth(row_index))
            if not native:
                selected = _safe_eval(
                    page,
                    JS.SELECT_EDITOR_ROW,
                    row_index,
                )
                if not isinstance(selected, dict):
                    raise XunfeiError(
                        f"多人配音快速选区失败：第 {row_index + 1} 行不可见"
                    )
                expected_text = rows[row_index].get("text") or ""
                if cls._normalize_selection_text(selected.get("text")) != cls._normalize_selection_text(expected_text):
                    raise XunfeiError(
                        f"多人配音快速选区回读失败：第 {row_index + 1} 行正文不一致"
                    )
                box = selected.get("box")
                if not isinstance(box, dict):
                    raise XunfeiError(
                        f"多人配音快速选区失败：第 {row_index + 1} 行坐标不可用"
                    )
                page.wait_for_timeout(20)
                return None, box

            # 保留一次轻量居中滚动：讯飞的待处理选区装饰只在目标行进入
            # 当前编辑器视口后才稳定回读。等待从旧实现的 120ms 降到 30ms，
            # 仍避免长文档滚动尚未完成就发送 pointerup。
            try:
                box = target.evaluate(
                    """el => {
                        el.scrollIntoView({
                        block: 'center',
                        inline: 'nearest',
                        behavior: 'instant',
                        });
                        const rect = el.getBoundingClientRect();
                        return {
                            x: rect.x,
                            y: rect.y,
                            width: rect.width,
                            height: rect.height,
                        };
                    }"""
                )
            except Exception:
                target.scroll_into_view_if_needed(timeout=5000)
                box = target.bounding_box()
            page.wait_for_timeout(20)
            target.select_text(timeout=5000)
            page.wait_for_timeout(20)
            return target, box

        def enqueue_current_selection(target, box):
            """用真实 Command/Ctrl pointerup 把当前 Selection 加入队列。

            讯飞队列监听的是 pointerup，而不是某个内部接口。先由浏览器
            原生 select_text/Shift-click 完整选区，再只发送一次带修饰键的
            真实鼠标 pointerup，避免长句换行时依赖鼠标拖动终点。
            """
            _check_cancel_requested(cancel_check)
            if not box or box["width"] < 4 or box["height"] < 4:
                raise XunfeiError("多人配音多段选区目标行不可见")
            def send_pointerup():
                page.keyboard.down(_MULTI_SELECT_MODIFIER)
                try:
                    # 长段落可能占两三行。讯飞的 pointerup 监听在已有队列
                    # 遮罩出现后，对段落中间/末行的坐标并不总会触发；首行
                    # 的正文区域在滚动和浮动工具条出现后仍稳定可用。
                    y = box["y"] + min(6, max(3, box["height"] * 0.12))
                    page.mouse.move(
                        box["x"] + box["width"] / 2,
                        y,
                    )
                    page.mouse.up()
                finally:
                    page.keyboard.up(_MULTI_SELECT_MODIFIER)

            send_pointerup()
            # 不逐行轮询装饰节点：讯飞会把选区装饰异步批量渲染，逐行等
            # 反而会在打包客户端里累积数百毫秒。固定给事件 35ms 落地，
            # 最终统一用 expected_count 回读；总数不符时由上层清空队列
            # 后重试整组，避免以速度换取漏段。
            page.wait_for_timeout(35)

        # 每行加入同一队列；连续配置仍由上层合并为一个配置组，后续只
        # 点击一次“使用”，不会退化成逐段打开音色面板。
        for first, last in normalized_ranges:
            for row_index in range(first, last + 1):
                _check_cancel_requested(cancel_check)
                target, box = select_exact_text(row_index)
                enqueue_current_selection(target, box)

        # 队列装饰按实际段落保留一个节点，徽标则可能按连续区间计数；
        # 这里校验段落覆盖总数，避免漏掉任一目标行。
        expected_count = sum(
            last - first + 1 for first, last in normalized_ranges
        )
        def expected_queue_count():
            current = cls._read_composite_queue_count(page)
            return current if current == expected_count else None

        actual_count = _poll(
            expected_queue_count,
            timeout=5,
            interval=0.15,
            max_interval=0.6,
            page=page,
            cancel_check=cancel_check,
        )
        if actual_count != expected_count:
            cls._clear_composite_queue(page, cancel_check=cancel_check)
            raise XunfeiError(
                "多人配音多段选区数量校验失败："
                f"期望 {expected_count} 个待选段，实际 {actual_count} 个"
            )
        _log(
            f"[xunfei]   多人配音已加入多段选区："
            f"{len(normalized_ranges)} 个配置区间、{expected_count} 行"
        )
        return actual_count

    def _select_voice(self, page, voice_name, voice_key=None, cancel_check=None):
        """搜索并选择指定发音人，并以页面实际选中态校验缓存。"""
        _check_cancel_requested(cancel_check)
        target_key = str(voice_key or "").strip() or None

        # 提交作品后讯飞页面可能把发音人恢复为平台默认值。不能只相信
        # 本地缓存，否则下一条同音色任务会跳过搜索，最终悄悄使用默认音色。
        # 同时按 key 追踪，避免音色目录出现同名发音人时错误复用。
        cache_matches = (
            self._current_voice_key == target_key
            if target_key is not None
            else self._current_voice_name == voice_name
        )
        if cache_matches:
            _check_cancel_requested(cancel_check)
            selected = _safe_eval(page, JS.CHECK_VOICE_SELECTED, voice_name)
            if selected:
                return True
            _log(
                f"[xunfei]   页面当前音色不是缓存的 '{voice_name}'，"
                "强制重新搜索"
            )
            self._current_voice_key = None
            self._current_voice_name = None

        _log(
            f"[xunfei]   搜索并选择发音人: {voice_name}"
            + (f" (key={target_key})" if target_key else "")
        )

        def mark_selected():
            # 讯飞页面切换音色后会把三项调节恢复为页面默认值；即使新旧
            # 音色的目标数值恰好相同，也必须让 _apply_params() 重新下发。
            voice_changed = (
                self._current_voice_key != target_key
                if target_key is not None
                else self._current_voice_name != voice_name
            )
            self._current_voice_key = target_key
            self._current_voice_name = voice_name
            if voice_changed:
                self._applied_params = None
            return True

        for round_idx in range(2):
            _check_cancel_requested(cancel_check)
            selected = _safe_eval(page, JS.CHECK_VOICE_SELECTED, voice_name)
            if selected:
                return mark_selected()

            search_input = page.locator(
                "input.h-full.w-full, input[placeholder*='搜索'], input[placeholder*='音色'], input[placeholder*='主播']"
            )
            if search_input.count() > 0:
                search_input.first.click(timeout=3000)
                search_input.first.fill("")
                self._pause(page, 0.15, 0.06, cancel_check=cancel_check)
                _check_cancel_requested(cancel_check)
                search_input.first.fill(voice_name)
                _poll(
                    lambda: _safe_eval(page, JS.CHECK_SEARCH_RESULT, voice_name),
                    timeout=5,
                    interval=0.6,
                    page=page,
                    cancel_check=cancel_check,
                )

            _check_cancel_requested(cancel_check)
            clicked = _safe_eval(page, JS.SEARCH_AND_CLICK_VOICE, voice_name)
            if clicked:
                self._pause(page, 0.6, 0.25, cancel_check=cancel_check)
                _check_cancel_requested(cancel_check)
                selected = _safe_eval(page, JS.CHECK_VOICE_SELECTED, voice_name)
                if selected:
                    return mark_selected()
                _log(f"[xunfei]   发音人 '{voice_name}' 点击后未见选中态，重试...")

        raise XunfeiError(f"未找到或无法选中发音人: {voice_name}")

    def _apply_params(self, page, speed, pitch, volume, cancel_check=None):
        """
        设置语速/语调/音量三项并回读验证。
        与已应用参数一致时跳过；切换发音人后必须重新应用（站点会重置参数）。
        """
        targets = {"speed": clamp_param(speed), "pitch": clamp_param(pitch),
                   "volume": clamp_param(volume)}
        _check_cancel_requested(cancel_check)
        if self._applied_params == targets:
            return True

        labels = ("语速", "语调", "音量")
        values = (targets["speed"], targets["pitch"], targets["volume"])
        failed_labels = []
        for idx, (label, value) in enumerate(zip(labels, values)):
            _check_cancel_requested(cancel_check)
            ok = False
            # 方式一：真实键盘输入（点击 → 全选 → 输入 → Tab 失焦）
            try:
                loc = page.locator("input.w-12").nth(idx)
                if loc.count() > 0:
                    loc.click(timeout=3000)
                    page.keyboard.press(_SELECT_ALL)
                    page.keyboard.type(str(value))
                    page.keyboard.press("Tab")
                    self._pause(page, 0.25, 0.1, cancel_check=cancel_check)
                    _check_cancel_requested(cancel_check)
                    readback = _safe_eval(page, JS.READ_PARAM_INPUTS) or []
                    ok = idx < len(readback) and readback[idx].strip() == str(value)
            except Exception:
                ok = False
            # 方式二：JS 注入兜底 + 回读验证
            if not ok:
                _check_cancel_requested(cancel_check)
                _safe_eval(page, JS.SET_PARAM_INPUT, [idx, value])
                self._pause(page, 0.2, 0.08, cancel_check=cancel_check)
                _check_cancel_requested(cancel_check)
                readback = _safe_eval(page, JS.READ_PARAM_INPUTS) or []
                ok = idx < len(readback) and readback[idx].strip() == str(value)
            if not ok:
                _log(f"[xunfei]   ⚠️ 参数[{label}] 设置为 {value} 后回读不一致")
                failed_labels.append(label)

        if failed_labels:
            # 不能把未验证成功的参数写入缓存，否则后续合成会跳过设置，
            # 最终生成的音频可能悄悄使用了网页上的旧参数。
            self._applied_params = None
            failed = ", ".join(failed_labels)
            raise XunfeiError(f"讯飞参数设置失败，回读不一致: {failed}")

        self._applied_params = dict(targets)
        applied_log = ", ".join(f"{l}={v}" for l, v in zip(labels, values))
        _log(f"[xunfei]   参数已应用: {applied_log}")
        return True

    def _click_generate(self, page, cancel_check=None):
        """点击'生成音频'按钮。"""
        _check_cancel_requested(cancel_check)
        btn = page.locator("button", has_text="生成音频")
        if btn.count() == 0:
            btn = page.locator("button.bg-blue-600")
        if btn.count() == 0:
            raise XunfeiError("未找到'生成音频'按钮")
        btn.first.click(timeout=5000)
        _log("[xunfei]   已点击生成音频")
        # 生成按钮一旦点击，讯飞可能已经开始计费/创建作品。这里的短暂
        # 页面稳定等待不能再读取取消探针；否则“点击成功 -> 取消 -> 未
        # 捕获 worksId”会把已扣费任务丢出断点记录，下一轮只能冒险重复
        # 提交。调用方会在这个边界之后用不可打断的确认/worksId 对账事务
        # 收尾，完成后再在安全检查点响应暂停或终止。
        self._pause(page, 0.6, 0.3)

    @staticmethod
    def _normalize_works_name(value):
        """收敛讯飞作品名称，避免下载页名称被截断或包含非法字符。"""
        text = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", str(value or "")).strip()
        return text[:25] or f"wordtts_{uuid.uuid4().hex[:10]}"

    def _set_works_name(self, page, works_name, cancel_check=None):
        """在作品设置弹窗中写入唯一名称，便于下载页人工核对。"""
        normalized = self._normalize_works_name(works_name)
        try:
            _check_cancel_requested(cancel_check)
            field = page.locator('input[placeholder*="作品名称"]:visible').first
            if field.count() == 0:
                return False
            field.click(timeout=3000)
            page.keyboard.press(_SELECT_ALL)
            page.keyboard.insert_text(normalized)
            page.keyboard.press("Tab")
            self._pause(page, 0.2, 0.08, cancel_check=cancel_check)
            _check_cancel_requested(cancel_check)
            actual = field.input_value(timeout=1000)
            if actual == normalized:
                _log(f"[xunfei]   作品名称已设置: {normalized}")
                return True
        except XunfeiCancelled:
            raise
        except Exception as error:
            _log(f"[xunfei]   作品名称设置失败（继续使用默认名称）: {error}")
        return False

    @classmethod
    def _set_mp3_format_with_locator(cls, page):
        """用 Playwright locator 兜底选择作品设置中的 MP3 单选项。

        讯飞的实际 DOM 没有稳定的 MP3 value，选项文本在 ``label`` 内；
        因此这里同时读取 input value 和 label 文本，但永远不会按“第一个
        单选项”点击，避免 MP3 缺失时误选 WAV。
        """
        try:
            dialogs = page.locator(
                '.ant-modal:visible, .ant-modal-content:visible, [role="dialog"]:visible, '
                '.el-dialog:visible, .el-message-box:visible'
            )
            for index in range(min(dialogs.count(), 20)):
                dialog = dialogs.nth(index)
                text = re.sub(r"\s+", "", dialog.inner_text(timeout=500))
                radios = dialog.locator(
                    'input[type="radio"][name="exportFormat"]'
                )
                if "作品设置" not in text or radios.count() == 0:
                    continue

                mp3 = None
                mp3_label = None
                for radio_index in range(radios.count()):
                    radio = radios.nth(radio_index)
                    value = (radio.get_attribute("value") or "").strip().lower()
                    label = radio.locator("xpath=ancestor::label[1]")
                    try:
                        label_text = re.sub(r"\s+", "", label.inner_text(timeout=500)).lower()
                    except Exception:
                        try:
                            label_text = re.sub(
                                r"\s+", "", radio.evaluate(
                                    "element => element.parentElement?.textContent || ''"
                                )
                            ).lower()
                        except Exception:
                            label_text = ""
                    if (
                        value == "mp3"
                        or label_text == "mp3"
                        or label_text.startswith("mp3")
                    ):
                        mp3 = radio
                        mp3_label = label
                        break

                if mp3 is None:
                    return "mp3_not_found"
                if mp3.is_checked():
                    return "already_locator"
                if mp3.is_disabled():
                    return "mp3_disabled"

                mp3.click(force=True, timeout=2000)
                if mp3.is_checked():
                    return "clicked_locator"
                if mp3_label is not None and mp3_label.count() > 0:
                    mp3_label.click(force=True, timeout=2000)
                return "clicked_locator"
        except Exception as error:
            _log(f"[xunfei]   locator 选择 MP3 失败: {error}")
        return None

    @classmethod
    def _read_mp3_format_with_locator(cls, page):
        """读取 locator 看到的作品设置格式，仅返回 MP3 的真实勾选状态。"""
        try:
            dialogs = page.locator(
                '.ant-modal:visible, .ant-modal-content:visible, [role="dialog"]:visible, '
                '.el-dialog:visible, .el-message-box:visible'
            )
            for index in range(min(dialogs.count(), 20)):
                dialog = dialogs.nth(index)
                text = re.sub(r"\s+", "", dialog.inner_text(timeout=500))
                radios = dialog.locator(
                    'input[type="radio"][name="exportFormat"]'
                )
                if "作品设置" not in text or radios.count() == 0:
                    continue
                for radio_index in range(radios.count()):
                    radio = radios.nth(radio_index)
                    value = (radio.get_attribute("value") or "").strip().lower()
                    label = radio.locator("xpath=ancestor::label[1]")
                    try:
                        label_text = re.sub(r"\s+", "", label.inner_text(timeout=500)).lower()
                    except Exception:
                        label_text = ""
                    if (
                        value == "mp3"
                        or label_text == "mp3"
                        or label_text.startswith("mp3")
                    ):
                        return "mp3" if radio.is_checked() else "other"
                return "mp3_not_found"
        except Exception:
            pass
        return None

    def _ensure_mp3_format(self, page, timeout=10, cancel_check=None):
        """在最终确认合成前强制确认讯飞作品格式为 MP3。

        这里不接受“默认应该是 MP3”作为成功条件：必须找到真实的
        ``exportFormat`` MP3 radio，并在点击后回读 checked 状态；否则不
        点击“确认合成”，防止在 Windows/不同账号默认值为 WAV 时生成失败。
        """
        def set_probe():
            result = _safe_eval(page, JS.SET_MP3_FORMAT)
            if isinstance(result, dict) and result.get("status") != "not_found":
                return result
            return None

        result = _poll(
            set_probe,
            timeout=timeout,
            interval=0.35,
            page=page,
            cancel_check=cancel_check,
        )
        status = result.get("status") if isinstance(result, dict) else None
        if status not in {"already_mp3", "clicked_mp3"}:
            # JS 选择器失败时只按同一套精确规则兜底，绝不退化为 first radio。
            fallback = self._set_mp3_format_with_locator(page)
            if fallback in {"already_locator", "clicked_locator"}:
                status = fallback
            elif fallback in {"mp3_not_found", "mp3_disabled"}:
                status = fallback

        if status in {"mp3_not_found", "mp3_disabled"}:
            _log(
                "[xunfei]   作品设置中没有可用的 MP3 选项，"
                f"停止提交 (status={status})"
            )
            return False

        def read_probe():
            state = _safe_eval(page, JS.GET_MP3_FORMAT)
            if isinstance(state, dict) and state.get("status") != "not_found":
                return state
            return None

        state = _poll(
            read_probe,
            timeout=4,
            interval=0.25,
            page=page,
            cancel_check=cancel_check,
        )
        if not isinstance(state, dict) or not state.get("checked"):
            # React 受控单选项偶尔会让 JS click 后的 DOM 更新稍慢；只有在
            # 回读仍未确认时才使用 locator，再次点击同一个 MP3 选项。
            fallback = self._set_mp3_format_with_locator(page)
            if fallback in {"already_locator", "clicked_locator"}:
                state = _poll(
                    read_probe,
                    timeout=3,
                    interval=0.25,
                    page=page,
                    cancel_check=cancel_check,
                )

        if isinstance(state, dict) and state.get("checked"):
            _log(
                "[xunfei]   作品设置格式已确认为 MP3 "
                f"(status={status or state.get('status')})"
            )
            return True

        # 最后再读取一次 locator 状态，日志里明确区分“没弹窗”和“MP3
        # 不存在/未勾选”，便于定位 Windows 端页面结构差异。
        locator_state = self._read_mp3_format_with_locator(page)
        if locator_state == "mp3":
            _log("[xunfei]   作品设置格式已确认为 MP3 (locator)")
            return True
        snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
        if snapshot:
            _log(
                "[xunfei]   作品设置 MP3 格式确认失败，当前弹窗: "
                + json.dumps(snapshot, ensure_ascii=False)[:1800]
            )
        else:
            _log(
                "[xunfei]   作品设置 MP3 格式确认失败: "
                f"status={status or 'not_found'}, locator={locator_state or 'not_found'}"
            )
        return False

    @staticmethod
    def _visible_confirm_synth_buttons(page):
        """返回当前页面可见的“确认合成”按钮，兼容讯飞弹窗 DOM 变化。"""
        buttons = []
        try:
            candidates = page.locator('button:visible')
            for index in range(min(candidates.count(), 200)):
                button = candidates.nth(index)
                try:
                    label = re.sub(r"\s+", "", button.inner_text(timeout=500)).strip()
                except Exception:
                    continue
                if label != "确认合成":
                    continue
                try:
                    disabled = button.is_disabled()
                except Exception:
                    disabled = None
                buttons.append((button, disabled))
        except Exception:
            pass
        return buttons

    @classmethod
    def _click_confirm_synth_button(cls, page):
        """用可见按钮 locator 点击“确认合成”，并记录现场状态。"""
        buttons = cls._visible_confirm_synth_buttons(page)
        if buttons:
            _log(
                "[xunfei]   确认合成按钮现场: "
                + ", ".join(f"visible disabled={disabled}" for _, disabled in buttons)
            )
        for button, disabled in buttons:
            if disabled is True:
                continue
            try:
                button.click(force=True, timeout=3000)
                return True
            except Exception as error:
                _log(f"[xunfei]   locator 点击确认合成失败: {error}")
        return False

    # ------------------------------------------------------------------
    # 确认合成弹窗流程
    # ------------------------------------------------------------------

    def _observe_after_first_confirm(self, page, cancel_check=None):
        """第一次点击确认合成后的状态探测。"""

        def probe():
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            # 第一次确认后，确认按钮本身可能还没卸载；这里只接受真正的
            # AI/错误/订单状态，避免把旧的确认弹窗当成已完成。
            return state if state in {
                "ai_modal", "insufficient", "rate_limited", "login", "order",
            } else None

        result = _poll(
            probe,
            # 讯飞页面的 React 弹层可能在点击后数秒才挂载；保留较长
            # 的等待窗口，但每轮只做一次合并状态快照，避免拖慢浏览器。
            timeout=15,
            interval=0.4,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        )
        if not result:
            _check_cancel_requested(cancel_check)
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            result = state if state in {
                "ai_modal", "insufficient", "rate_limited", "login", "order",
            } else None
        result = result or "none"
        if result == "none":
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   第一次确认后未检测到 AI 标识弹窗，当前可见弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            else:
                _log("[xunfei]   第一次确认后未检测到 AI 标识弹窗（可能已选择‘不再提示’，或讯飞本次未展示）")
        return result

    @staticmethod
    def _find_visible_dialog(page, text_fragment):
        """按文案找到可见弹窗，兼容 Ant Design 和新版通用 dialog。"""
        try:
            dialogs = page.locator(
                '.ant-modal:visible, .ant-modal-content:visible, [role="dialog"]:visible, '
                '.el-dialog:visible, .el-message-box:visible'
            )
            for index in range(min(dialogs.count(), 20)):
                dialog = dialogs.nth(index)
                try:
                    text = re.sub(r"\s+", "", dialog.inner_text(timeout=500))
                except Exception:
                    continue
                if text_fragment in text:
                    return dialog
        except Exception:
            pass
        return None

    @classmethod
    def _click_no_remind_with_locator(cls, page):
        """JS 找不到复选框时，用 Playwright 强制点击真实控件兜底。"""
        dialog = cls._find_visible_dialog(page, "不再提示")
        if dialog is None:
            return None
        try:
            inputs = dialog.locator('input[type="checkbox"], .ant-checkbox-input')
            unchecked = []
            for index in range(inputs.count()):
                checkbox = inputs.nth(index)
                try:
                    if checkbox.is_checked():
                        continue
                except Exception:
                    continue
                unchecked.append(checkbox)
            if unchecked:
                unchecked[0].click(force=True, timeout=2000)
                return "clicked_locator_input"
            if inputs.count() > 0:
                return "already_locator"

            labels = dialog.locator('.ant-checkbox-wrapper, label, [role="checkbox"], button')
            for index in range(labels.count()):
                label = labels.nth(index)
                label_text = re.sub(r"\s+", "", label.inner_text(timeout=500))
                if "不再提示" not in label_text:
                    continue
                label.click(force=True, timeout=2000)
                return "clicked_locator_label"
        except Exception:
            pass
        return None

    @classmethod
    def _click_ai_switch_with_locator(cls, page):
        """用 locator 兜底点击确认合成弹窗中的 AI 标识开关。"""
        try:
            dialogs = page.locator(
                '.ant-modal:visible, .ant-modal-content:visible, [role="dialog"]:visible, '
                '.el-dialog:visible, .el-message-box:visible'
            )
            for index in range(min(dialogs.count(), 20)):
                dialog = dialogs.nth(index)
                text = re.sub(r"\s+", "", dialog.inner_text(timeout=500))
                if "不再提示" in text:
                    continue
                if not any(marker in text for marker in ("作品设置", "确认合成", "作品名称")):
                    continue
                switches = dialog.locator(
                    'button[role="switch"], [role="switch"], .ant-switch, button[aria-pressed]'
                )
                for switch_index in range(switches.count()):
                    switch = switches.nth(switch_index)
                    aria_checked = switch.get_attribute("aria-checked")
                    aria_pressed = switch.get_attribute("aria-pressed")
                    class_name = switch.get_attribute("class") or ""
                    is_on = (
                        aria_checked == "true"
                        or aria_pressed == "true"
                        or "ant-switch-checked" in class_name
                    )
                    if not is_on:
                        return "already_locator"
                    # 优先点击真实 button[role=switch]，让 Ant Design/React
                    # 收到完整的开关事件；不要只点击内部装饰 handle。
                    switch.click(force=True, timeout=2000)
                    return "clicked_locator"
        except Exception:
            pass
        return None

    def _ensure_ai_switch_off(self, page, timeout=12, cancel_check=None):
        """确保作品设置中的 AI 标识开关为关闭状态。

        讯飞有时跳过“AI 标识说明”弹窗，直接展示“作品设置”；因此这个
        检查必须独立于说明弹窗流程，并且必须回读 aria-checked/class 状态。
        返回 ``off``、``on`` 或 ``not_found``。
        """
        last_state = "not_found"
        js_click_attempted = False
        last_locator_attempt = 0.0

        def probe():
            nonlocal last_state, js_click_attempted, last_locator_attempt
            info = _probe_synth_state(page)
            if info and info.get("ai_modal"):
                # 说明弹窗可以延迟挂载；处理成功后从头回读作品设置，
                # 不把“当前还没看到 switch”误判为关闭成功。
                if self._handle_ai_flag_dialog(
                    page,
                    ensure_switch=False,
                    cancel_check=cancel_check,
                ):
                    js_click_attempted = False
                    last_locator_attempt = 0.0
                return None

            state = str((info or {}).get("ai_switch") or "not_found")
            last_state = state
            if state == "off":
                return "off"
            if state == "on":
                if not js_click_attempted:
                    clicked = _safe_eval(page, JS.CLICK_AI_SWITCH)
                    if clicked == "already_off":
                        return None
                    if clicked == "clicked":
                        js_click_attempted = True
                        self._pause(page, 0.18, 0.05, cancel_check=cancel_check)
                        return None
                # JS click 没有让 React 受控状态变化时，降低频率再用
                # locator 点击真实 button[role=switch]，避免连续点同一开关。
                now = time.monotonic()
                if now - last_locator_attempt >= 0.65:
                    last_locator_attempt = now
                    if self._click_ai_switch_with_locator(page):
                        self._pause(page, 0.25, 0.08, cancel_check=cancel_check)
                return None

            # switch 尚未挂载时也给 locator 一次机会；页面继续异步渲染时，
            # 自适应轮询会再次回到这里，不会漏掉延迟出现的开关。
            now = time.monotonic()
            if now - last_locator_attempt >= 0.65:
                last_locator_attempt = now
                if self._click_ai_switch_with_locator(page):
                    self._pause(page, 0.25, 0.08, cancel_check=cancel_check)
            return None

        result = _poll(
            probe,
            timeout=timeout,
            interval=0.2,
            max_interval=0.85,
            page=page,
            cancel_check=cancel_check,
        )
        if result == "off":
            return "off"
        return last_state

    @classmethod
    def _click_ai_confirm_with_locator(cls, page):
        """用 locator 兜底点击 AI 标识弹窗的确认按钮。"""
        dialog = cls._find_visible_dialog(page, "不再提示")
        if dialog is None:
            return False
        try:
            buttons = dialog.locator('button, [role="button"], .ant-btn')
            labels = {"确认", "确定", "知道了", "我知道了", "继续"}
            for index in range(buttons.count()):
                button = buttons.nth(index)
                label = re.sub(r"\s+", "", button.inner_text(timeout=500)).strip()
                if label not in labels:
                    continue
                button.click(force=True, timeout=2000)
                return True
        except Exception:
            pass
        return False

    def _handle_ai_flag_dialog(self, page, ensure_switch=True, cancel_check=None):
        def check_no_remind():
            result = _safe_eval(page, JS.CHECK_NO_REMIND)
            return result if result in {"clicked", "clicked_input", "clicked_label", "already"} else None

        checked = _poll(
            check_no_remind,
            timeout=10,
            interval=0.25,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        )
        if not checked:
            checked = self._click_no_remind_with_locator(page)
        _log(f"[xunfei]   AI 标识弹窗‘不再提示’: {'✓' if checked else '✗'}{f' ({checked})' if checked else ''}")
        if not checked:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   AI 弹窗未勾选‘不再提示’，当前弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            return False
        self._pause(page, 0.35, 0.15, cancel_check=cancel_check)

        if ensure_switch:
            switch_state = self._ensure_ai_switch_off(
                page,
                timeout=12,
                cancel_check=cancel_check,
            )
            _log(
                f"[xunfei]   AI 标识开关关闭: "
                f"{'✓' if switch_state == 'off' else '未出现' if switch_state == 'not_found' else '✗'}"
                f" ({switch_state})"
            )
            if switch_state == "on":
                snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
                if snapshot:
                    _log(f"[xunfei]   AI 标识开关未确认关闭，当前弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
                return False
            self._pause(page, 0.35, 0.15, cancel_check=cancel_check)

        confirmed = bool(_poll(
            lambda: _safe_eval(page, JS.CLICK_AI_CONFIRM),
            timeout=12,
            interval=0.35,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        ))
        if not confirmed:
            confirmed = self._click_ai_confirm_with_locator(page)
        if not confirmed:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   AI 弹窗仍未关闭，当前弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
        _log(f"[xunfei]   AI 标识弹窗确认: {'✓' if confirmed else '✗'}")
        if not confirmed:
            return False

        def ai_modal_closed():
            info = _probe_synth_state(page)
            return bool(info and info.get("ai_modal") is False)

        closed = _poll(
            ai_modal_closed,
            timeout=8,
            interval=0.25,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        )
        if not closed:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   AI 标识确认后弹窗仍存在: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            return False
        self._pause(page, 0.5, 0.2, cancel_check=cancel_check)
        return True

    def _wait_order_or_error(self, page, timeout, cancel_check=None):
        def probe():
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            if state == "order":
                return "ok"
            return state if state in {"insufficient", "rate_limited", "login"} else None

        result = _poll(
            probe,
            timeout=timeout,
            interval=0.8,
            max_interval=1.5,
            page=page,
            cancel_check=cancel_check,
        )
        if result:
            return result
        # 超时边界再做一次同步快照，覆盖最后一刻才挂载的错误/订单提示。
        info = _probe_synth_state(page)
        state = (info or {}).get("state")
        if state == "order":
            return "ok"
        return state if state in {"insufficient", "rate_limited", "login"} else None

    def _confirm_synth(self, page, works_name=None, cancel_check=None):
        """
        处理确认合成弹窗完整流程。

        返回: 'ok' | 'insufficient' | 'rate_limited' | 'login' | 'failed'
        """
        initial_ai_state = None
        self._confirm_click_succeeded = False
        self._submission_state_uncertain = False
        confirm_clicked = False

        def uncertain_after_confirm(reason):
            """确认按钮已点击后无法判定结果时，禁止回到通用重试。"""
            if confirm_clicked:
                raise XunfeiSubmissionAmbiguous(reason, works_name=works_name)
            return "failed"

        def active_cancel_check():
            # 点击“确认合成”可能已经触发计费。此后的页面观察、AI 弹窗
            # 收尾和订单状态等待不能再被暂停/终止探针打断，否则上层拿
            # 不到 worksId 或作品名，下一轮只能冒险重复提交。提交前仍
            # 完整响应原有控制探针。
            return cancel_check if not confirm_clicked else None

        def ensure_ai_off(timeout=12):
            kwargs = {"timeout": timeout}
            current_cancel_check = active_cancel_check()
            if current_cancel_check is not None:
                kwargs["cancel_check"] = current_cancel_check
            return self._ensure_ai_switch_off(page, **kwargs)

        def ensure_mp3():
            current_cancel_check = active_cancel_check()
            if current_cancel_check is None:
                return self._ensure_mp3_format(page)
            return self._ensure_mp3_format(page, cancel_check=current_cancel_check)

        def observe_after_first_confirm():
            current_cancel_check = active_cancel_check()
            if current_cancel_check is None:
                return self._observe_after_first_confirm(page)
            return self._observe_after_first_confirm(
                page,
                cancel_check=current_cancel_check,
            )

        def handle_ai_flag(ensure_switch=False):
            kwargs = {"ensure_switch": ensure_switch}
            current_cancel_check = active_cancel_check()
            if current_cancel_check is not None:
                kwargs["cancel_check"] = current_cancel_check
            return self._handle_ai_flag_dialog(page, **kwargs)

        def wait_order(timeout):
            current_cancel_check = active_cancel_check()
            if current_cancel_check is None:
                return self._wait_order_or_error(page, timeout)
            return self._wait_order_or_error(
                page,
                timeout,
                cancel_check=current_cancel_check,
            )

        def ensure_ai_setting(allow_missing=False):
            # “订单支付”/“去下载”弹窗已经说明作品提交完成；此时原来的
            # 作品设置弹窗已经被卸载，不可能再读到 AI switch。第一次提交
            # 前已确认过关闭状态，不能在这里再次轮询 8 秒等待不存在的开关。
            if allow_missing and initial_ai_state == "off":
                _log("[xunfei]   作品设置弹窗已关闭，沿用第一次确认前已验证的 AI 标识关闭状态")
                return True
            state = ensure_ai_off()
            _log(f"[xunfei]   合成前 AI 标识开关状态: {state}")
            return state == "off"

        def confirm_state():
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            return state if state in {"confirm", "ai_modal", "order", "insufficient", "rate_limited", "login"} else None

        appeared = _poll(
            confirm_state,
            # 不假设“作品设置”会同步出现；讯飞客户端可能延迟挂载
            # 5–10 秒，继续轮询但每轮只读取一次状态快照。
            timeout=15,
            interval=0.6,
            max_interval=1.25,
            page=page,
            cancel_check=active_cancel_check(),
        )
        if not appeared and self._visible_confirm_synth_buttons(page):
            appeared = "confirm"
        if not appeared:
            # 无弹窗也可能直接开始合成；若出现订单/错误则按其处理
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   未找到确认合成按钮，当前可见弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            else:
                _log("[xunfei]   未找到确认合成按钮，当前没有可识别的可见弹窗")
            settled = wait_order(4) or "failed"
            if settled == "ok":
                # 某些版本没有弹出“确认合成”按钮，而是直接出现订单
                # 状态；订单本身已经证明提交发生，后续回读失败也不能重试。
                confirm_clicked = True
                self._confirm_click_succeeded = True
                if not ensure_ai_setting():
                    return uncertain_after_confirm(
                        "已出现订单但作品设置回读失败，提交结果不确定"
                    )
            elif settled == "failed":
                # 生成按钮已经点击，但页面没有给出可判定的确认/订单
                # 状态；不能把这个未知结果当成“未提交”再次点击。
                self._submission_state_uncertain = True
            return settled

        self._pause(page, 0.6, 0.3, cancel_check=cancel_check)

        # 讯飞“作品设置”弹窗中的格式是独立的 WAV/MP3 单选项。不能依赖
        # 默认勾选，也不能取第一个 option；提交前必须回读并确认 MP3。
        if not ensure_mp3():
            _log("[xunfei]   未能确认作品格式为 MP3，停止提交，避免误生成 WAV")
            return "failed"

        if works_name:
            works_name_kwargs = {}
            if cancel_check is not None:
                works_name_kwargs["cancel_check"] = cancel_check
            self._set_works_name(page, works_name, **works_name_kwargs)

        # “作品设置”就是这次提交使用的最终设置，真实 DOM 中开关位于这里：
        # role="switch"、aria-checked="true"。必须在第一次确认合成前关闭，
        # 不能等弹窗切换或订单完成后再处理，否则水印配置已经被提交。
        initial_ai_state = ensure_ai_off()
        _log(f"[xunfei]   第一次确认前 AI 标识开关状态: {initial_ai_state}")
        if initial_ai_state != "off":
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(
                    "[xunfei]   AI 标识开关未找到可关闭的作品设置弹窗: "
                    + json.dumps(snapshot, ensure_ascii=False)[:1800]
                )
            _log("[xunfei]   AI 标识开关无法确认关闭，停止提交，避免生成带水印音频")
            return "failed"

        # 第一次点击"确认合成"
        clicked = self._click_confirm_synth_button(page)
        if not clicked:
            clicked = bool(_safe_eval(page, JS.CLICK_BTN_IN_MODAL, "确认合成"))
        _log(f"[xunfei]   第一次确认合成: {'✓' if clicked else '✗'}")
        if not clicked:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   第一次确认合成点击失败，当前可见弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            return "failed"
        confirm_clicked = True
        self._confirm_click_succeeded = True

        outcome = observe_after_first_confirm()
        _log(f"[xunfei]   第一次确认后的页面状态: {outcome}")
        ai_modal_seen = outcome == "ai_modal"
        if outcome == "ai_modal":
            _log("[xunfei]   检测到 AI 标识说明弹窗")
            if not handle_ai_flag(False):
                _log("[xunfei]   AI 标识弹窗未完成确认，停止本次合成")
                return uncertain_after_confirm("确认合成后 AI 标识弹窗未完成，提交结果不确定")
        elif outcome in ("order", "insufficient", "rate_limited"):
            if outcome == "order" and not ensure_ai_setting(allow_missing=True):
                return uncertain_after_confirm(
                    "确认合成后已出现订单，但作品设置回读失败"
                )
            return "ok" if outcome == "order" else outcome

        # AI 弹窗关闭、页面切换和确认合成按钮重新出现之间存在异步延迟。
        # 这里必须继续轮询状态，不能用一次立即查询把任务误判为已完成。
        def probe_followup():
            # 与第一次确认后的探测保持相同优先级；不要在一轮中重复执行
            # 多个 page.evaluate，延迟挂载时仍由外层轮询继续等待。
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            return state if state in {
                "ai_modal", "insufficient", "rate_limited", "login", "order", "confirm",
            } else None

        followup = _poll(
            probe_followup,
                timeout=15,
                interval=0.35,
                max_interval=1.0,
                page=page,
                cancel_check=active_cancel_check(),
        )
        if followup == "ai_modal":
            ai_modal_seen = True
            # 少数页面会在第一次 AI 弹窗确认后重新挂载一次弹窗，允许再处理一轮。
            _log("[xunfei]   AI 标识弹窗仍在，重新处理")
            if not handle_ai_flag(False):
                _log("[xunfei]   AI 标识弹窗二次处理失败，停止本次合成")
                return uncertain_after_confirm(
                    "确认合成后的 AI 标识弹窗未完成，提交结果不确定"
                )
            followup = _poll(
                probe_followup,
                timeout=12,
                interval=0.35,
                max_interval=1.0,
                page=page,
                cancel_check=active_cancel_check(),
            )
        _log(f"[xunfei]   二次确认前页面状态: {followup or '未发现明确状态'}")
        if followup in ("order", "insufficient", "rate_limited"):
            if followup == "order" and not ensure_ai_setting(allow_missing=True):
                return uncertain_after_confirm("确认合成后已出现订单，但作品设置回读失败")
            return "ok" if followup == "order" else followup

        # 真实“作品设置”弹窗的结构是 role="switch" + aria-checked，
        # 它可能不会触发 AI 说明弹窗；二次确认前再次强制回读并关闭。
        if not ensure_ai_setting(allow_missing=True):
            return uncertain_after_confirm("确认合成后作品设置回读失败，提交结果不确定")
        clicked2 = bool(_poll(
            lambda: self._click_confirm_synth_button(page)
            or _safe_eval(page, JS.CLICK_BTN_IN_MODAL, "确认合成"),
            timeout=12,
            interval=0.35,
            max_interval=1.0,
            page=page,
            cancel_check=active_cancel_check(),
        ))
        _log(f"[xunfei]   第二次确认合成: {'✓' if clicked2 else '✗'}")
        if clicked2:
            confirm_clicked = True
            self._confirm_click_succeeded = True
            settled = wait_order(90) or "ok"
            if settled == "ok" and not ensure_ai_setting(allow_missing=True):
                return uncertain_after_confirm("二次确认后作品设置回读失败，提交结果不确定")
            return settled

        # 讯飞部分账号/版本在没有 AI 说明弹窗时，第一次“确认合成”就
        # 已经提交任务，不会再显示第二个确认按钮。等待一小段时间确认
        # 没有额度、登录或频控错误后，按已提交处理，避免误重试造成频控。
        settled = wait_order(12)
        if settled:
            if settled == "ok" and not ensure_ai_setting(allow_missing=True):
                return uncertain_after_confirm("确认合成后作品设置回读失败，提交结果不确定")
            return settled
        if ai_modal_seen:
            info = _probe_synth_state(page)
            if info and info.get("ai_modal"):
                return uncertain_after_confirm("确认合成后的 AI 标识弹窗仍未关闭，提交结果不确定")
        if not ensure_ai_setting(allow_missing=True):
            return uncertain_after_confirm("确认合成后无法确认作品设置，提交结果不确定")
        return "ok"

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

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
        page_size = XunFeiSession._works_list_page_size(needed)
        return min(100, max(5, (needed + page_size - 1) // page_size + 4))

    def _fetch_works_list_in_page(
        self,
        page,
        needed_count=1,
        page_index=1,
        works_name=None,
        cancel_check=None,
    ):
        """获取指定页的已完成作品列表，返回讯飞原始作品对象。"""
        _check_cancel_requested(cancel_check)
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
        _check_cancel_requested(cancel_check)
        # _signed_api_post 对成功响应返回 dict，对认证/网络/API 错误返回
        # None。记录这个区别，断点恢复时不能把一次列表接口故障误判成
        # worksId 已失效，否则下一轮会重复提交并可能重复计费。
        if not data:
            self._last_works_list_fetch_ok = False
            return []
        payload = data.get("data")
        if not isinstance(payload, dict):
            self._last_works_list_fetch_ok = False
            return []
        items = payload.get("userWorksList")
        if items is None:
            # 讯飞在“没有作品”时有版本会返回 null，视为一次成功的空扫描；
            # 缺少 data 或返回非列表则仍视为协议异常，不能据此认定 worksId 失效。
            self._last_works_list_fetch_ok = True
            return []
        if not isinstance(items, list):
            self._last_works_list_fetch_ok = False
            return []
        self._last_works_list_fetch_ok = True
        return items

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
            if cancel_check is not None:
                fetch_kwargs["cancel_check"] = cancel_check
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
            _controlled_wait(page, 1.0, cancel_check=cancel_check)
        return None

    def _wait_for_works_entry(self, page, works_id, timeout=120, cancel_check=None):
        """等待同一个 worksId 出现在作品列表中，严禁按名称或最新记录替代。"""
        expected = str(works_id)
        deadline = time.time() + timeout
        logged_wait = False
        while time.time() < deadline:
            _check_cancel_requested(cancel_check)
            fetch_kwargs = {"needed_count": 1}
            if cancel_check is not None:
                fetch_kwargs["cancel_check"] = cancel_check
            for item in self._fetch_works_list_in_page(page, **fetch_kwargs):
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id") or item.get("worksId")
                if item_id is not None and str(item_id) == expected:
                    _log(f"[xunfei]   ✅ 作品列表已匹配 worksId: {expected}")
                    return item
            if not logged_wait:
                _log(f"[xunfei]   ⏳ 等待作品列表匹配 worksId: {expected}")
                logged_wait = True
            _controlled_wait(page, 2.0, cancel_check=cancel_check)
        _log(f"[xunfei]   ⚠️ 作品列表未匹配到 worksId: {expected}")
        return None

    def _wait_for_works_ready(self, page, works_id, timeout=180, cancel_check=None):
        """等待精确 worksId 对应的音频文件真正可下载。"""
        expected = str(works_id)
        deadline = time.time() + timeout
        matched_logged = False
        waiting_logged = False
        while time.time() < deadline:
            _check_cancel_requested(cancel_check)
            fetch_kwargs = {"needed_count": 1}
            if cancel_check is not None:
                fetch_kwargs["cancel_check"] = cancel_check
            items = self._fetch_works_list_in_page(page, **fetch_kwargs)
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
                    page,
                    expected,
                    log_result=False,
                    cancel_check=cancel_check,
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

            _controlled_wait(page, 2.0, cancel_check=cancel_check)

        _log(f"[xunfei]   ⚠️ 匹配作品在限定时间内仍不可下载 worksId: {expected}")
        return None

    def _fetch_sign_url_in_page(
        self,
        page,
        works_id,
        log_result=True,
        cancel_check=None,
    ):
        """按精确 worksId 请求对应签名 URL。"""
        _check_cancel_requested(cancel_check)
        param = {"worksId": str(works_id), "worksType": 1}
        data = self._signed_api_post(page, API_SIGN_URL, param)
        _check_cancel_requested(cancel_check)
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

    def _cleanup_after_item(self, page, cancel_check=None):
        """单条提交后关闭残留弹窗并清空编辑器，不刷新页面。"""
        _check_cancel_requested(cancel_check)
        _safe_eval(page, JS.CLOSE_ALL_MODALS, [])
        # 讯飞页面的音色和三项参数状态要跨条复用；这里只清空输入内容，
        # 不能用 goto/reload，否则同一音色分组会被迫重复选择和设置参数。
        self._clear_editor(page, cancel_check=cancel_check)
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
            cancel_check=cancel_check,
        )
        if not ready:
            self._pause(page, 0.25, 0.08, cancel_check=cancel_check)

    def _recover_and_retry(self, page, cancel_check=None):
        """合成失败后恢复页面状态（重新加载编辑页，重置音色/参数记忆）。"""
        try:
            _check_cancel_requested(cancel_check)
            page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=CONTROLLED_NAVIGATION_TIMEOUT_MS,
            )
            _check_cancel_requested(cancel_check)
            page.wait_for_selector(".ssml-editor", timeout=CONTROLLED_NAVIGATION_TIMEOUT_MS)
            _check_cancel_requested(cancel_check)
            self._current_voice_key = None
            self._current_voice_name = None
            self._applied_params = None
            return True
        except XunfeiCancelled:
            raise
        except Exception as e:
            _log(f"[xunfei]   页面恢复失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 登录与会话
    # ------------------------------------------------------------------

    @staticmethod
    def _process_snapshot():
        """读取当前进程树中可见的子进程信息；失败时返回空字典。"""
        if psutil is None:
            return {}
        try:
            parent = psutil.Process(os.getpid())
            processes = parent.children(recursive=True)
        except Exception:
            return {}
        snapshot = {}
        for process in processes:
            try:
                snapshot[process.pid] = {
                    "process": process,
                    "exe": str(process.exe() or ""),
                    "cmdline": [str(value) for value in (process.cmdline() or [])],
                    "create_time": float(process.create_time()),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        return snapshot

    def _capture_browser_process_identity(self):
        """把 Playwright 启动出的 Chromium 主进程绑定到控制器。"""
        if psutil is None:
            return
        executable = os.path.realpath(str(self._browser_executable_path or ""))
        profile = os.path.realpath(str(PROFILE_DIR or ""))
        deadline = time.monotonic() + 2.0
        candidate_snapshot = {}
        while time.monotonic() < deadline:
            snapshot = self._process_snapshot()
            candidates = {}
            for pid, info in snapshot.items():
                if pid in self._browser_process_ids_before:
                    continue
                exe = os.path.realpath(info.get("exe") or "")
                cmdline = info.get("cmdline") or []
                cmdline_text = " ".join(cmdline)
                exact_executable = bool(executable and exe == executable)
                profile_match = bool(profile and profile in cmdline_text)
                if exact_executable or profile_match:
                    candidates[pid] = info
            if candidates:
                candidate_snapshot = candidates
                break
            time.sleep(0.05)

        if not candidate_snapshot:
            return
        # 主 Chromium 进程没有 --type=renderer/utility 等子进程标记；若
        # 平台命令行差异较大，退回创建时间最早的匹配进程。
        main_candidates = [
            (pid, info) for pid, info in candidate_snapshot.items()
            if not any(str(arg).startswith("--type=") for arg in info.get("cmdline") or [])
        ]
        pool = main_candidates or list(candidate_snapshot.items())
        main_pid = min(pool, key=lambda item: item[1].get("create_time", float("inf")))[0]
        process_ids = sorted(candidate_snapshot)
        with self._browser_identity_lock:
            self._browser_pid = int(main_pid)
            self._browser_process_ids = [int(pid) for pid in process_ids]
            controller = self._browser_controller
            if controller:
                controller.attach_processes(self._browser_pid, self._browser_process_ids)

    def browser_snapshot(self):
        """返回浏览器身份、页面上下文和平台窗口控制状态。"""
        with self._browser_identity_lock:
            controller = self._browser_controller
            if controller is None:
                return {
                    "visibility": "unavailable",
                    "platform": sys.platform,
                    "permission_required": False,
                    "last_error": "自动化浏览器尚未启动",
                    "pid": None,
                    "process_ids": [],
                    "executable_path": self._browser_executable_path,
                    "profile_dir": PROFILE_DIR,
                    "started_at": self._browser_started_at,
                    "window_handles": [],
                    "context_id": id(self._ctx) if self._ctx else None,
                    "page_id": id(self._page) if self._page else None,
                    "page_count": self._browser_page_count,
                    "logged_in": bool(self._logged_in),
                    "browser_mode": self._browser_mode,
                }
            snapshot = controller.snapshot()
            snapshot.update({
                "context_id": id(self._ctx) if self._ctx else None,
                "page_id": id(self._page) if self._page else None,
                "page_count": self._browser_page_count,
                "logged_in": bool(self._logged_in),
                "browser_mode": self._browser_mode,
            })
            return snapshot

    def set_browser_visibility(self, visible: bool, *, minimize=False):
        """对已绑定身份的专用浏览器执行显示/隐藏。"""
        with self._browser_identity_lock:
            controller = self._browser_controller
        if controller is None:
            return self.browser_snapshot()
        return controller.set_visibility(bool(visible), minimize=minimize)

    def login(self, login_timeout=300, cancel_check=None, allow_system_chrome=False):
        """
        打开可见的 Chrome 浏览器，导航到讯飞配音。
        首次需要手动登录（手机号+验证码），后续自动复用已保存的登录状态。
        """
        _check_cancel_requested(cancel_check)
        self._playwright = sync_playwright().start()

        browser_path, browser_mode = _select_browser_executable(
            self._playwright,
            allow_system_chrome=allow_system_chrome,
        )
        self._browser_executable_path = browser_path
        self._browser_mode = browser_mode
        self._browser_started_at = time.time()
        self._browser_process_ids_before = set(self._process_snapshot())
        self._browser_controller = BrowserWindowController(
            executable_path=browser_path,
            profile_dir=PROFILE_DIR,
            started_at=self._browser_started_at,
        )
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

        # 无论开发还是发布，都显式传入已解析的 executable path，避免
        # PLAYWRIGHT_BROWSERS_PATH 只定位资源目录而实际启动了另一份浏览器。
        _check_cancel_requested(cancel_check)
        launch_kwargs["executable_path"] = browser_path
        _log(f"[xunfei] 使用自动化浏览器 ({browser_mode}): {browser_path}")

        self._ctx = self._playwright.chromium.launch_persistent_context(
            **launch_kwargs
        )
        self._capture_browser_process_identity()

        self._ctx.add_init_script(STEALTH_SCRIPT)
        self._ctx.add_init_script(MUTE_AUDIO_SCRIPT)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._browser_page_count = len(self._ctx.pages)

        # 注册网络响应监听（整个会话期间持续捕获 worksId / sign_url）
        self._response_handler = self._on_response
        self._request_handler = self._on_request
        self._page.on("request", self._request_handler)
        self._page.on("response", self._response_handler)

        try:
            self._real_ua = self._page.evaluate("navigator.userAgent")
        except Exception:
            pass

        _log("[xunfei] 正在打开讯飞配音...")

        try:
            _check_cancel_requested(cancel_check)
            self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            _check_cancel_requested(cancel_check)
        except XunfeiCancelled:
            raise
        except Exception as goto_error:
            _log(f"[xunfei] 首次加载提示: {goto_error}")

        # 不再按秒轮询 document.readyState。讯飞页面可能持续有网络请求，
        # readyState=complete 并不等于编辑器可用；直接等待真正需要的编辑器
        # 节点，页面一旦就绪就立即继续，避免启动阶段白占 CPU 和最多 30 秒。
        try:
            self._page.wait_for_selector(
                ".ssml-editor", state="attached", timeout=30000
            )
            _check_cancel_requested(cancel_check)
        except XunfeiCancelled:
            raise
        except Exception:
            _log("[xunfei] 页面编辑器未找到，重试加载...")
            try:
                _check_cancel_requested(cancel_check)
                self._page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=CONTROLLED_NAVIGATION_TIMEOUT_MS,
                )
                _check_cancel_requested(cancel_check)
            except XunfeiCancelled:
                raise
            except Exception:
                pass
            try:
                self._page.wait_for_selector(
                    ".ssml-editor", state="attached", timeout=30000
                )
                _check_cancel_requested(cancel_check)
            except XunfeiCancelled:
                raise
            except Exception:
                raise XunfeiError("无法加载讯飞配音编辑器")

        # 检测登录状态
        _check_cancel_requested(cancel_check)
        if self._is_logged_in(self._page):
            _log("[xunfei] 检测到已保存的登录状态，无需重新登录")
        else:
            _log("[xunfei] 登录状态无效，请在浏览器中手动登录...")
            _log(f"[xunfei] 等待用户登录（超时 {login_timeout} 秒）...")
            deadline = time.time() + login_timeout
            logged = False
            while time.time() < deadline:
                _controlled_wait(self._page, 2.0, cancel_check=cancel_check)
                if self._is_logged_in(self._page):
                    if self._page.locator(".ssml-editor").count() > 0:
                        logged = True
                        break
            if not logged:
                raise XunfeiLoginRequired(
                    f"等待登录超时（{login_timeout}秒），讯飞配音登录未完成"
                )
            _log("[xunfei] 登录成功！")

        self._logged_in = True

    # ------------------------------------------------------------------
    # 合成页面操作
    # ------------------------------------------------------------------

    @staticmethod
    def _speaker_number(voice_key, info):
        value = info.get("speaker_no") or info.get("speakerNo")
        if value in (None, ""):
            match = re.match(r"^speaker:(\d+)$", str(voice_key or "").strip())
            value = match.group(1) if match else None
        try:
            number = int(float(value))
        except (TypeError, ValueError, OverflowError):
            number = 0
        if number <= 0:
            raise XunfeiError(
                f"音色 {voice_key!r} 缺少讯飞 speakerNo，无法提交多人配音作品；"
                "请刷新音色目录后重试"
            )
        return number

    @staticmethod
    def _composite_voice_search_name(info):
        """返回多人配音 common/list 使用的基础音色名称。"""
        return str(
            info.get("composite_name")
            or info.get("common_name")
            or info.get("name")
            or ""
        ).strip()

    @staticmethod
    def _composite_variant_label(voice_name, composite_name, explicit_label=None):
        """返回多人配音详情面板中的具体变体短标签。"""
        explicit = str(explicit_label or "").strip()
        if explicit:
            return explicit
        name = str(voice_name or "").strip()
        base = str(composite_name or "").strip()
        if not name or not base:
            return ""
        if name.casefold() == base.casefold():
            return ""
        if name.casefold().startswith(base.casefold()):
            suffix = name[len(base):].strip(" -－_")
            return suffix
        return ""

    @classmethod
    def _select_composite_variant(
        cls, page, voice_name, composite_name, variant_label=None, cancel_check=None
    ):
        """基础卡片打开详情后，按具体变体名称选择对应短标签。

        common/list 卡片只展示“欣畅”，详情面板才展示 “Pro+ / Pro”。
        优先使用 flat/list 提供的 ``emotDesc`` 精确标签；没有该字段时，
        再从变体名称中提取后缀，避免把 flat/list 的完整名称再次拿去搜索
        基础卡片。
        """
        label = cls._composite_variant_label(voice_name, composite_name, variant_label)
        if not label:
            return True
        scope = cls._composite_ui_scope(page)
        controls = scope.locator("div.cursor-pointer:visible")
        try:
            metadata = controls.evaluate_all(
                """els => els.map((el, index) => ({
                    index,
                    text: (el.innerText || '').trim(),
                }))"""
            )
        except Exception:
            return False
        candidates = [
            item for item in metadata[:200]
            if cls._normalize_composite_ui_text(item.get("text"))
            == cls._normalize_composite_ui_text(label)
        ]
        if len(candidates) != 1:
            return False
        _check_cancel_requested(cancel_check)
        controls.nth(int(candidates[0]["index"])).click(timeout=5000)
        # 变体点击由 React 异步更新边框；等到目标短标签所在卡片出现
        # 讯飞当前选中蓝色边框后再继续点击“使用”。
        def selected():
            try:
                current_controls = scope.locator("div.cursor-pointer:visible")
                current_metadata = current_controls.evaluate_all(
                    """els => els.map(el => {
                            const button = el.querySelector('button');
                            const nodes = button ? [el, button] : [el];
                            const styles = nodes.map(node => window.getComputedStyle(node));
                            const inline = nodes.map(node => String(
                                node.getAttribute('style') || ''
                            )).join(' ').replace(/\\s+/g, '').toLowerCase();
                            const className = nodes.map(node => String(node.className || ''))
                                .join(' ').toLowerCase();
                            const selected = styles.some(style => (
                                style.borderColor === 'rgb(26, 145, 255)'
                                || style.backgroundColor === 'rgba(26, 145, 255, 0.04)'
                            ))
                                || inline.includes('#1a91ff')
                                || inline.includes('rgba(26,145,255,0.04)')
                                || className.includes('border-[#1a91ff');
                            return {
                                text: (el.innerText || '').trim(),
                                selected,
                            };
                        })"""
                )
                return any(
                    cls._normalize_composite_ui_text(item.get("text"))
                    == cls._normalize_composite_ui_text(label)
                    and item.get("selected")
                    for item in current_metadata[:200]
                )
            except Exception:
                return False

        return bool(
            _poll(
                selected,
                timeout=3,
                interval=0.08,
                max_interval=0.3,
                page=page,
                cancel_check=cancel_check,
            )
        )

    @staticmethod
    def _normalize_composite_ui_text(value):
        return re.sub(r"\s+", "", str(value or "")).strip().casefold()

    @classmethod
    def _composite_ui_text_matches(cls, actual, expected):
        actual_text = cls._normalize_composite_ui_text(actual)
        expected_text = cls._normalize_composite_ui_text(expected)
        if not actual_text or not expected_text:
            return False
        if expected_text in actual_text:
            return True
        # 音色卡片有的版本把名称中的短横线渲染成空格，匹配时兼容
        # 这种展示差异，但仍要求完整音色名称出现在卡片文字中。
        compact_actual = actual_text.replace("-", "").replace("－", "")
        compact_expected = expected_text.replace("-", "").replace("－", "")
        return compact_expected in compact_actual

    @classmethod
    def _composite_ui_scope(cls, page):
        """返回当前多人配音弹层，避免点击被背景遮罩或旧卡片拦截。"""
        search_selector = (
            'input[placeholder*="输入主播名称进行搜索"]:visible, '
            'input[placeholder*="输入主播名称"]:visible'
        )
        # 大多数调用发生在编辑器工具栏（尤其是批量停顿）上，此时
        # 页面没有弹层。先做一次直接查询，避免每次都遍历 fixed 根节点。
        if page.locator(search_selector).count() == 0:
            return page
        roots = page.locator(
            'div.fixed:visible, [role="dialog"]:visible, .ant-modal:visible'
        )
        try:
            for index in range(min(roots.count(), 20)):
                root = roots.nth(index)
                if root.locator(
                    'input[placeholder*="输入主播名称进行搜索"]:visible, '
                    'input[placeholder*="输入主播名称"]:visible'
                ).count() > 0:
                    return root
        except Exception:
            pass
        return page

    @classmethod
    def _click_composite_ui_control(cls, page, label):
        """点击可见的多人配音工具按钮，使用真实 Playwright click。"""
        expected = cls._normalize_composite_ui_text(label)
        # 停顿按钮会被连续点击很多次，但每次的可访问名称都稳定为
        # ``2s``/``1s`` 等。优先直接定位这个可见 UI 按钮，避免每处停顿
        # 都重新扫描整页几十个控件；点击仍是 Playwright 的真实 click，
        # 找不到时再走下面的严格元数据扫描兜底。
        if re.fullmatch(r"\d+(?:\.\d+)?s", expected):
            try:
                direct = page.get_by_role("button", name=label, exact=True).last
                direct.click(timeout=500)
                return True
            except Exception:
                pass
        scope = cls._composite_ui_scope(page)
        controls = scope.locator(
            'button:visible, [role="button"]:visible, [data-speaker-id]:visible, '
            '.cursor-pointer:visible'
        )
        try:
            # 逐个 inner_text/is_disabled 会产生大量 Playwright ↔ 浏览器
            # 往返，打包客户端里尤其明显。这里只把当前可见控件的必要
            # 元数据一次性读回，最终 click 仍然使用真实页面控件。
            metadata = controls.evaluate_all(
                """els => els.map((el, index) => ({
                    index,
                    text: (el.innerText || '').trim(),
                    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                }))"""
            )
            for item in metadata[:200]:
                if cls._normalize_composite_ui_text(item.get("text")) != expected:
                    continue
                if item.get("disabled"):
                    continue
                controls.nth(int(item["index"])).click(timeout=5000)
                return True
        except Exception:
            pass
        return False

    @classmethod
    def _find_composite_voice_card(cls, page, voice_name):
        """寻找当前搜索结果中唯一的目标音色卡片。

        多人配音弹层同时包含搜索结果、最近使用音色和参数快捷卡片。
        以前只按整张控件的包含文本匹配，搜索 ``Amanda`` 时可能先命中
        最近使用卡片，或者在同名前缀音色中选择到错误项。这里只接受带
        音色头像的候选，并优先选择搜索结果的非 button 卡片；候选仍然
        不唯一时直接报错，交给上层重试，绝不盲点第一项。
        """
        scope = cls._composite_ui_scope(page)
        controls = scope.locator(
            'button:visible, [role="button"]:visible, [data-speaker-id]:visible, '
            '.cursor-pointer:visible'
        )
        candidates = []
        try:
            metadata = controls.evaluate_all(
                """els => els.map((el, index) => ({
                    index,
                    text: (el.innerText || '').trim(),
                    tagName: el.tagName || '',
                    className: typeof el.className === 'string' ? el.className : '',
                    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                    alt: el.querySelector('img[alt]')?.getAttribute('alt') || '',
                }))"""
            )
        except Exception:
            return None
        for item in metadata[:300]:
            index = int(item["index"])
            text = str(item.get("text") or "")
            if not cls._composite_ui_text_matches(text, voice_name):
                continue
            normalized = cls._normalize_composite_ui_text(text)
            if normalized in {"多人配音", "使用"} or "使用" in normalized:
                continue
            if item.get("disabled"):
                continue
            alt = str(item.get("alt") or "")
            if not cls._composite_ui_text_matches(f"{alt} {text}", voice_name):
                continue
            candidates.append(
                (
                    str(item.get("tagName") or "").upper(),
                    str(item.get("className") or ""),
                    controls.nth(index),
                )
            )

        # 讯飞当前页面的搜索结果是 div.w-full 卡片，最近使用列表是
        # button。保留同名情况下的搜索结果优先级，同时兼容未来把结果
        # 渲染成 button 的版本。
        preferred = [
            item for item in candidates
            if item[0] != "BUTTON" or "w-full" in item[1]
        ]
        selected = preferred or candidates
        if len(selected) == 1:
            return selected[0][2]
        if len(selected) > 1:
            raise XunfeiError(
                f"多人配音音色候选不唯一: {voice_name}（{len(selected)} 项）"
            )
        return None

    @classmethod
    def _open_composite_voice_panel(cls, page, cancel_check=None):
        """打开“多人配音”面板，并返回其搜索框。"""
        _check_cancel_requested(cancel_check)
        search_selector = (
            'input[placeholder*="输入主播名称进行搜索"]:visible, '
            'input[placeholder*="输入主播名称"]:visible'
        )
        search = page.locator(search_selector)
        if search.count() == 0:
            # 队列刚完成时工具栏按钮可能有几十到几百毫秒的 disabled
            # 状态。立即判失败会触发整组重试，客户端看起来就会慢很多；
            # 这里只等待按钮真正可用，正常路径第一次轮询即完成。
            clicked = _poll(
                lambda: (
                    True
                    if cls._click_composite_ui_control(page, "多人配音")
                    else None
                ),
                timeout=4,
                interval=0.08,
                max_interval=0.3,
                page=page,
                cancel_check=cancel_check,
            )
            if not clicked:
                raise XunfeiError("未找到可用的“多人配音”按钮")
            search = _poll(
                lambda: (
                    page.locator(search_selector)
                    if page.locator(search_selector).count() > 0
                    else None
                ),
                timeout=8,
                interval=0.08,
                max_interval=0.4,
                page=page,
                cancel_check=cancel_check,
            )
        if not search or search.count() == 0:
            raise XunfeiError("“多人配音”面板未加载音色搜索框")
        return search.first

    @classmethod
    def _close_composite_voice_panel(cls, page, cancel_check=None):
        """关闭多人配音音色面板，避免失败重试时遮挡编辑器。

        音色卡片搜索失败时，讯飞页面仍会保留一个 fixed 遮罩层。这个
        遮罩层会拦截编辑器的真实 click，导致后续重新输入看起来像是
        编辑器坏了。优先使用页面支持的 Escape，再在仍可见时点击面板
        内的明确关闭/取消控件；整个过程只使用浏览器可见 UI 操作。
        """
        search_selector = (
            'input[placeholder*="输入主播名称进行搜索"]:visible, '
            'input[placeholder*="输入主播名称"]:visible'
        )

        def panel_closed():
            return page.locator(search_selector).count() == 0

        _check_cancel_requested(cancel_check)
        if panel_closed():
            return True
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        if _poll(
            panel_closed,
            timeout=1.5,
            interval=0.1,
            max_interval=0.4,
            page=page,
            cancel_check=cancel_check,
        ):
            return True

        # 某些页面版本不响应 Escape，但面板会渲染“关闭/取消”按钮。
        # 只在包含音色搜索框的 fixed 弹层内匹配，避免误点编辑器其它按钮。
        try:
            search = page.locator(search_selector).first
            scope = search.locator(
                'xpath=ancestor::*[contains(@class, "fixed")][1]'
            )
            controls = scope.locator(
                'button:visible, [role="button"]:visible'
            )
            close_labels = {"关闭", "取消", "×", "✕", "close", "cancel"}
            for index in range(min(controls.count(), 100)):
                control = controls.nth(index)
                label = ""
                try:
                    label = (control.inner_text(timeout=500) or "").strip()
                except Exception:
                    pass
                aria = (control.get_attribute("aria-label") or "").strip()
                title = (control.get_attribute("title") or "").strip()
                if not any(
                    value.casefold() in close_labels
                    for value in (label, aria, title)
                    if value
                ):
                    continue
                control.click(timeout=3000)
                if _poll(
                    panel_closed,
                    timeout=1.5,
                    interval=0.1,
                    max_interval=0.4,
                    page=page,
                ):
                    return True
        except XunfeiCancelled:
            raise
        except Exception:
            pass
        return panel_closed()

    @classmethod
    def _apply_composite_ui_params(cls, page, speed, pitch, volume, cancel_check=None):
        """在多人配音面板中用键盘设置三项参数并逐项回读。"""
        _check_cancel_requested(cancel_check)
        targets = (
            clamp_param(speed),
            clamp_param(pitch),
            clamp_param(volume),
        )
        labels = ("语速", "语调", "音量")
        scope = cls._composite_ui_scope(page)

        def find_inputs():
            inputs = scope.locator('input.w-12:visible')
            if inputs.count() >= 3:
                return inputs
            inputs = scope.locator('input[placeholder="数值"]:visible')
            return inputs if inputs.count() >= 3 else None

        inputs = _poll(
            find_inputs,
            timeout=8,
            interval=0.25,
            max_interval=0.8,
            page=page,
            cancel_check=cancel_check,
        )
        if inputs is None or inputs.count() < 3:
            raise XunfeiError("“多人配音”面板的语速、语调、音量输入框未完整加载")

        for index, (label, value) in enumerate(zip(labels, targets)):
            _check_cancel_requested(cancel_check)
            field = inputs.nth(index)

            def read_expected_value():
                try:
                    actual_value = field.input_value(timeout=1000).strip()
                except Exception:
                    return None
                return actual_value if actual_value == str(value) else None

            try:
                field.click(timeout=3000)
                page.keyboard.press(_SELECT_ALL)
                page.keyboard.type(str(value))
                page.keyboard.press("Tab")
                # 输入框的 DOM value 会先于讯飞 React 表单状态更新；
                # 不能只看到 input_value 正确就立即点击“使用”，否则
                # 会把上一组音色的旧参数带入标记。80ms 足够让 blur/input
                # 状态落地，仍比原先每项固定 180ms 更快。
                _controlled_wait(page, 0.08, cancel_check=cancel_check, slice_seconds=0.08)
                actual = _poll(
                    read_expected_value,
                    timeout=1.2,
                    interval=0.025,
                    max_interval=0.12,
                    page=page,
                    cancel_check=cancel_check,
                )
            except XunfeiCancelled:
                raise
            except Exception as error:
                raise XunfeiError(
                    f"多人配音 UI 参数[{label}]设置失败: {error}"
                ) from error
            if actual != str(value):
                raise XunfeiError(
                    f"多人配音 UI 参数[{label}]回读不一致："
                    f"期望 {value}，实际 {actual!r}"
                )

    @classmethod
    def _composite_row_signature(cls, row):
        return (
            str(row.get("voice_key") or DEFAULT_FEMALE),
            clamp_param(row.get("speed", PARAM_DEFAULT)),
            clamp_param(row.get("pitch", PARAM_DEFAULT)),
            clamp_param(row.get("volume", PARAM_DEFAULT)),
        )

    @classmethod
    def _composite_row_groups(cls, rows):
        """把相邻且配置完全相同的编辑器行合并为一次选区操作。"""
        if not rows:
            return []
        groups = []
        start = 0
        previous = cls._composite_row_signature(rows[0])
        for index in range(1, len(rows)):
            current = cls._composite_row_signature(rows[index])
            if current != previous:
                groups.append((start, index - 1))
                start = index
                previous = current
        groups.append((start, len(rows) - 1))
        return groups

    @classmethod
    def _composite_signature_ranges(cls, rows):
        """按最终配置收集不连续的连续区间，供讯飞多段选择队列使用。

        讯飞编辑器支持按住 Command/Ctrl 依次加入多个不连续选区，随后
        对队列统一套用音色和三项参数。这里保留连续区间边界用于规划、
        校验和日志统计；实际长文档按行用浏览器原生精确选区加入队列，
        避免跨滚动或换行造成误选，同时不再用全文覆盖去修正例外。
        """
        groups = {}
        order = []
        for first_index, last_index in cls._composite_row_groups(rows):
            signature = cls._composite_row_signature(rows[first_index])
            if signature not in groups:
                groups[signature] = []
                order.append(signature)
            groups[signature].append((first_index, last_index))
        return [
            {
                "signature": signature,
                "ranges": groups[signature],
            }
            for signature in order
        ]

    @classmethod
    def _composite_marking_plan(cls, rows):
        """生成多人配音的低交互次数标注计划。

        Chrome 的原生 Selection 只有一个连续 Range，不能安全地把交错的
        多个非连续段落同时交给讯飞页面。因此这里采用“基准覆盖 + 例外
        修正”：先把全文一次性设置为出现次数最多的完整配置，再按连续
        区间修正其它配置。这样既保留真实页面选区，也避免 W/M 交替时为
        每一行重复打开音色面板。
        """
        if not rows:
            return {
                "base_index": None,
                "base_signature": None,
                "correction_groups": [],
                "contiguous_group_count": 0,
            }

        counts = {}
        first_indices = {}
        for index, row in enumerate(rows):
            signature = cls._composite_row_signature(row)
            counts[signature] = counts.get(signature, 0) + 1
            first_indices.setdefault(signature, index)
        base_signature = max(
            counts,
            key=lambda signature: (
                counts[signature],
                -first_indices[signature],
            ),
        )
        base_index = first_indices[base_signature]

        correction_groups = []
        start = None
        for index, row in enumerate(rows):
            if cls._composite_row_signature(row) == base_signature:
                if start is not None:
                    correction_groups.append((start, index - 1))
                    start = None
            elif start is None:
                start = index
        if start is not None:
            correction_groups.append((start, len(rows) - 1))

        return {
            "base_index": base_index,
            "base_signature": base_signature,
            "correction_groups": correction_groups,
            "contiguous_group_count": len(cls._composite_row_groups(rows)),
        }

    @classmethod
    def _composite_ui_rows(cls, work):
        """展开作品为编辑器行，并记录每道题之后需要插入的停顿位置。"""
        rows = []
        item_last_indices = []
        items = list(work.get("items") or [])
        if not items:
            raise XunfeiError("多人配音作品没有可合成的题目")

        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                raise XunfeiError(f"多人配音第 {item_index + 1} 道题数据异常")
            segments = item.get("segments") or []
            if not segments and item.get("text"):
                segments = [{"text": item.get("text")}]
            before = len(rows)
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                text = str(segment.get("text") or "")
                if not text.strip():
                    continue
                voice_key = str(segment.get("voice_key") or DEFAULT_FEMALE).strip()
                # 目录校验提前进行，避免已经输入文本后才发现音色 key 无效。
                get_voice_info(voice_key)
                lines = text.splitlines() or [text]
                for line in lines:
                    clean = line.strip()
                    if not clean:
                        continue
                    rows.append({
                        "item_index": item_index,
                        "text": clean,
                        "voice_key": voice_key,
                        "speed": clamp_param(segment.get("speed", PARAM_DEFAULT)),
                        "pitch": clamp_param(segment.get("pitch", PARAM_DEFAULT)),
                        "volume": clamp_param(segment.get("volume", PARAM_DEFAULT)),
                    })
            if len(rows) == before:
                raise XunfeiError(
                    f"多人配音第 {item_index + 1} 道题没有可合成的文本"
                )
            item_last_indices.append(len(rows) - 1)

        boundary_ms = int(work.get("boundary_ms") or 2000)
        boundaries = [
            (last_index, boundary_ms)
            for last_index in item_last_indices[:-1]
        ]
        return rows, boundaries

    @classmethod
    def _verify_composite_voice_marks(
        cls, page, rows, first_index, last_index, voice_name, speaker_number,
        config_row=None,
    ):
        """确认目标行只有一个完整、正确的音色标记。"""
        return cls._verify_composite_voice_marks_ranges(
            page,
            rows,
            [(first_index, last_index)],
            voice_name,
            speaker_number,
            config_row,
        )

    @classmethod
    def _verify_composite_voice_marks_ranges(
        cls, page, rows, ranges, voice_name, speaker_number, config_row=None
    ):
        """确认多个不连续选区中的每一行都只保留目标音色标记。"""
        del voice_name  # 仅用于保留原调用签名，页面回读以 speakerNo 为准。
        expected_indices = [
            index
            for first_index, last_index in ranges
            for index in range(first_index, last_index + 1)
        ]
        if not expected_indices:
            return False
        try:
            snapshot = page.evaluate(
                """indices => indices.map(index => {
                    const paragraph = document.querySelectorAll('.ssml-editor p')[index];
                    if (!paragraph) return {index, paragraph: false};
                    const marks = Array.from(
                        paragraph.querySelectorAll('.ssml-text-mark-speaker')
                    );
                    return {
                        index,
                        paragraph: true,
                        markCount: marks.length,
                        marks: marks.map(mark => {
                            const content = Array.from(
                                mark.querySelectorAll(
                                    'span.range-annotation-content.speaker-content'
                                )
                            ).filter(el => (
                                !el.classList.contains('ssml-tag')
                                && el.getAttribute('data-type') !== 'range_anchor'
                            ));
                            return {
                                speakerId: mark.getAttribute('data-speaker-id') || '',
                                rate: mark.getAttribute('data-rate') || '',
                                pitch: mark.getAttribute('data-pitch') || '',
                                volume: mark.getAttribute('data-volume') || '',
                                contentCount: content.length,
                                contentText: content.length === 1
                                    ? (content[0].textContent || '')
                                    : '',
                            };
                        }),
                    };
                })""",
                expected_indices,
            )
        except Exception:
            return False
        if not isinstance(snapshot, list) or len(snapshot) != len(expected_indices):
            return False

        expected_id = str(speaker_number)
        expected_params = None
        if config_row is not None:
            expected_params = {
                "rate": str(clamp_param(config_row.get("speed", PARAM_DEFAULT))),
                "pitch": str(clamp_param(config_row.get("pitch", PARAM_DEFAULT))),
                "volume": str(clamp_param(config_row.get("volume", PARAM_DEFAULT))),
            }
        for item, index in zip(snapshot, expected_indices):
            # 一个逻辑行只能有一个完整 speaker mark。只要保留旧标记、
            # 产生混合标记或标记被截成两段，都必须失败，不能“有一个对的
            # 标记就算通过”，否则最终作品会出现错音色片段。
            if not item.get("paragraph") or item.get("markCount") != 1:
                return False
            mark = item.get("marks", [None])[0]
            if not isinstance(mark, dict) or mark.get("speakerId") != expected_id:
                return False
            if expected_params is not None:
                for attribute, expected_value in expected_params.items():
                    if mark.get(attribute) != expected_value:
                        return False
            if mark.get("contentCount") != 1:
                return False
            expected_text = str(rows[index].get("text") or "")
            if cls._normalize_selection_text(mark.get("contentText")) != cls._normalize_selection_text(expected_text):
                return False
        return True

    @classmethod
    def _apply_composite_voice_to_selection(
        cls, page, rows, first_index, last_index, *, config_row=None,
        verify_ranges=None, cancel_check=None,
    ):
        """给当前精确选区设置音色、参数，并回读页面的 speaker 标记。"""
        _check_cancel_requested(cancel_check)
        first_row = config_row or rows[first_index]
        voice_key = str(first_row.get("voice_key") or DEFAULT_FEMALE)
        info = dict(get_voice_info(voice_key))
        voice_name = str(info.get("name") or voice_key)
        composite_name = cls._composite_voice_search_name(info) or voice_name
        speaker_number = cls._speaker_number(voice_key, info)
        if config_row is None:
            for index in range(first_index, last_index + 1):
                if cls._composite_row_signature(rows[index]) != cls._composite_row_signature(first_row):
                    raise XunfeiError("多人配音批量选区包含不同音色或参数，拒绝套用")

        phase_started_at = time.perf_counter()
        search = cls._open_composite_voice_panel(page, cancel_check=cancel_check)
        panel_open_ms = round((time.perf_counter() - phase_started_at) * 1000)
        card = None
        for search_attempt in range(2):
            _check_cancel_requested(cancel_check)
            search.click(timeout=3000)
            page.keyboard.press(_SELECT_ALL)
            # 多人配音弹窗的 common/list 只搜索基础名称，例如“欣畅”；
            # flat/list 目录里的“欣畅-Pro+”是具体变体，直接搜索会得到
            # 暂无匹配。详情面板再按具体变体短标签完成二次选择。
            page.keyboard.type(composite_name)
            card = _poll(
                lambda: cls._find_composite_voice_card(page, composite_name),
                timeout=5,
                interval=0.08,
                max_interval=0.35,
                page=page,
                cancel_check=cancel_check,
            )
            if card is not None:
                break
            if search_attempt == 0:
                # 搜索结果偶尔会因弹层刚打开而没有挂载。重新打开同一
                # 个网页面板即可恢复，不改变编辑器选区，也不盲点其它
                # 音色卡片。
                cls._close_composite_voice_panel(page, cancel_check=cancel_check)
                search = cls._open_composite_voice_panel(page, cancel_check=cancel_check)
        if card is None:
            raise XunfeiError(
                f"多人配音面板未找到音色卡片: {voice_name}（搜索基础名称 {composite_name}）"
            )
        _check_cancel_requested(cancel_check)
        card.click(timeout=5000)
        if not cls._select_composite_variant(
            page,
            voice_name,
            composite_name,
            variant_label=info.get("emot_desc"),
            cancel_check=cancel_check,
        ):
            raise XunfeiError(
                f"多人配音面板未找到具体变体: {voice_name}（基础音色 {composite_name}）"
            )
        # 选中卡片后面板会重新挂载三项参数输入框；输入框数量出现
        # 之前，旧的输入节点也可能短暂可见。给 React 一次短落地时间，
        # 避免把参数发给上一张卡片的旧表单。
        _controlled_wait(page, 0.18, cancel_check=cancel_check)
        card_ms = round((time.perf_counter() - phase_started_at) * 1000)
        params_started_at = time.perf_counter()
        cls._apply_composite_ui_params(
            page,
            first_row.get("speed", PARAM_DEFAULT),
            first_row.get("pitch", PARAM_DEFAULT),
            first_row.get("volume", PARAM_DEFAULT),
            cancel_check=cancel_check,
        )
        params_ms = round((time.perf_counter() - params_started_at) * 1000)
        apply_started_at = time.perf_counter()
        if not cls._click_composite_ui_control(page, "使用"):
            raise XunfeiError(f"多人配音面板未找到可用的“使用”按钮: {voice_name}")
        ranges_to_verify = verify_ranges or [(first_index, last_index)]
        verified = _poll(
            lambda: cls._verify_composite_voice_marks_ranges(
                page,
                rows,
                ranges_to_verify,
                voice_name,
                speaker_number,
                first_row,
            ),
            timeout=8,
            interval=0.2,
            max_interval=0.8,
            page=page,
            cancel_check=cancel_check,
        )
        if not verified:
            raise XunfeiError(
                f"多人配音音色标记回读失败：行 {first_index + 1}-{last_index + 1} "
                f"未确认使用 {voice_name}"
            )
        apply_ms = round((time.perf_counter() - apply_started_at) * 1000)
        # 讯飞页面对包含连续范围的队列有时会保留 pending-range 装饰，
        # 虽然音色已经成功套用。显式用 Escape 清理网页队列，确保下一
        # 个音色配置组不会把上一组的待处理段落一起带入。
        if verify_ranges and not cls._clear_composite_queue(page, cancel_check=cancel_check):
            raise XunfeiError("多人配音上一组多段选区未能清理")
        _log(
            f"[xunfei]   多人配音配置细分 {voice_name}："
            f"面板 {panel_open_ms}ms，音色卡片 {card_ms - panel_open_ms}ms，"
            f"参数 {params_ms}ms，应用回读 {apply_ms}ms"
        )
        if not verify_ranges:
            _log(
                f"[xunfei]   多人配音已设置行 {first_index + 1}-{last_index + 1}: "
                f"{voice_name}, speed={clamp_param(first_row.get('speed'))}, "
                f"pitch={clamp_param(first_row.get('pitch'))}, "
                f"volume={clamp_param(first_row.get('volume'))}"
            )

    @classmethod
    def _apply_composite_voice_to_queue(cls, page, rows, ranges, cancel_check=None):
        """对讯飞网页多段选择队列一次性设置音色和三项参数。"""
        _check_cancel_requested(cancel_check)
        if not ranges:
            raise XunfeiError("多人配音多段选区没有可套用的音色配置")
        first_index = ranges[0][0]
        first_row = rows[first_index]
        expected_signature = cls._composite_row_signature(first_row)
        for first, last in ranges:
            for index in range(first, last + 1):
                _check_cancel_requested(cancel_check)
                if cls._composite_row_signature(rows[index]) != expected_signature:
                    raise XunfeiError("多人配音多段选区包含不同音色或参数，拒绝套用")

        cls._apply_composite_voice_to_selection(
            page,
            rows,
            first_index,
            ranges[0][1],
            config_row=first_row,
            verify_ranges=ranges,
            cancel_check=cancel_check,
        )
        _log(
            f"[xunfei]   多人配音已统一设置 {len(ranges)} 个区间、"
            f"{sum(last - first + 1 for first, last in ranges)} 行: "
            f"{get_voice_info(first_row.get('voice_key') or DEFAULT_FEMALE)['name']}"
        )

    @classmethod
    def _read_composite_pause_issues(cls, page, boundaries):
        """一次回读所有停顿标记，避免每个段落都单独查询 DOM。"""
        expected = [
            {"row": int(row_index), "value": str(int(boundary_ms))}
            for row_index, boundary_ms in boundaries
        ]
        try:
            result = page.evaluate(
                """expected => expected.map(({row, value}) => {
                    const paragraph = document.querySelectorAll('.ssml-editor p')[row];
                    const count = paragraph
                        ? Array.from(paragraph.querySelectorAll('[data-type="break"]'))
                            .filter(el => el.getAttribute('data-value') === value).length
                        : 0;
                    return {row, value, count};
                }).filter(item => item.count !== 1)""",
                expected,
            )
        except Exception:
            return expected
        return result if isinstance(result, list) else expected

    @classmethod
    def _insert_composite_pause(
        cls,
        page,
        row_index,
        boundary_ms,
        *,
        emit_log=True,
        verify=True,
        cancel_check=None,
    ):
        """在指定题目末行末尾通过页面停顿按钮插入内部定位标记。"""
        _check_cancel_requested(cancel_check)
        paragraphs = page.locator(".ssml-editor p")
        paragraph = paragraphs.nth(row_index)
        # 先选中整行，再用方向键折叠到文本末尾，避免点击段落中部把
        # 停顿插到句中或被浏览器保留为跨段选区。正常路径直接在当前页面
        # 建立正文 Range，避免每处停顿额外等待一次 Playwright select_text；
        # 页面版本不接受该选区时再回退到原生方式。
        fast_selection = _safe_eval(page, JS.SELECT_EDITOR_ROW, row_index)
        fast_box = fast_selection.get("box") if isinstance(fast_selection, dict) else None
        if not isinstance(fast_box, dict) or not fast_selection.get("text"):
            if row_index < 0 or row_index >= paragraphs.count():
                raise XunfeiError("多人配音停顿位置超出编辑器段落范围")
            paragraph.select_text(timeout=5000)
        else:
            page.wait_for_timeout(10)
        page.keyboard.press("ArrowRight")
        # select_text + ArrowRight 都是同步的浏览器输入动作；只给页面
        # 一个很短的事件循环机会，实际插入结果由下面的回读轮询确认。
        page.wait_for_timeout(10)
        label = f"{int(boundary_ms) / 1000:g}s"
        clicked = _poll(
            lambda: (
                True if cls._click_composite_ui_control(page, label) else None
            ),
            timeout=1.5,
            interval=0.04,
            max_interval=0.2,
            page=page,
            cancel_check=cancel_check,
        )
        if not clicked:
            raise XunfeiError(f"未找到讯飞停顿按钮: {label}")
        if verify:
            selector = (
                f'[data-type="break"][data-value="{int(boundary_ms)}"]'
            )
            inserted = _poll(
                lambda: paragraphs.nth(row_index).locator(selector).count() > 0,
                timeout=3,
                interval=0.04,
                max_interval=0.2,
                page=page,
                cancel_check=cancel_check,
            )
            if not inserted:
                raise XunfeiError(
                    f"讯飞停顿插入校验失败：第 {row_index + 1} 行未找到 {boundary_ms}ms 标记"
                )
        if emit_log:
            _log(f"[xunfei]   已在第 {row_index + 1} 行后插入 {label} 停顿")

    @classmethod
    def _prepare_composite_editor(cls, page, work, cancel_check=None):
        """用讯飞页面 UI 构造多人作品，返回行和停顿边界。"""
        _check_cancel_requested(cancel_check)
        started_at = time.perf_counter()
        rows, boundaries = cls._composite_ui_rows(work)
        cls._input_composite_text(page, rows, cancel_check=cancel_check)
        groups = cls._composite_row_groups(rows)
        queue_plan = cls._composite_signature_ranges(rows)
        _log(
            f"[xunfei]   多人配音 UI 已输入 {len(rows)} 行，"
            f"原连续配置 {len(groups)} 组；多段队列将按 "
            f"{len(queue_plan)} 个音色/参数组统一标注"
        )

        # 讯飞新版编辑器提供真实的 Command/Ctrl 多段选择队列：同一配置的
        # 不连续行先全部加入队列，再一次点击“使用”统一设置音色和参数。
        # 若页面版本没有该能力或队列回读失败，重新输入文本后退回旧的
        # 连续区间方案，保证正确性优先。
        queue_error = None
        # 正常路径使用页面 Range 建立选区，遇到页面版本不接受 Range
        # 时，后续整批都切换为原生 select_text，避免在同一批任务中反复
        # 试探两种选区机制。
        native_selection = False
        for queue_attempt in range(2):
            _check_cancel_requested(cancel_check)
            if queue_attempt:
                native_selection = True
                _log(
                    "[xunfei]   多人配音多段队列应用回读失败，"
                    "重新输入全部文本后再试一次"
                )
                cls._close_composite_voice_panel(page, cancel_check=cancel_check)
                cls._clear_composite_queue(page, cancel_check=cancel_check)
                cls._input_composite_text(page, rows, cancel_check=cancel_check)
            try:
                for entry_index, entry in enumerate(queue_plan, start=1):
                    group_started_at = time.perf_counter()
                    ranges = entry["ranges"]
                    selection_error = None
                    for selection_attempt in range(2):
                        try:
                            _check_cancel_requested(cancel_check)
                            cls._select_composite_queue_rows(
                                page,
                                rows,
                                ranges,
                                native=(native_selection or selection_attempt > 0),
                                cancel_check=cancel_check,
                            )
                            selection_error = None
                            break
                        except XunfeiCancelled:
                            raise
                        except XunfeiError as error:
                            selection_error = error
                            retryable = (
                                "多人配音 UI 选区校验失败" in str(error)
                                or "多人配音多段选区数量校验失败" in str(error)
                                or "多人配音快速选区" in str(error)
                            )
                            if selection_attempt == 0 and retryable:
                                native_selection = True
                                _log(
                                    "[xunfei]   多人配音多段选区回读不一致，"
                                    "清空当前队列后重试一次"
                                )
                                if not cls._clear_composite_queue(page, cancel_check=cancel_check):
                                    break
                                continue
                            break
                    if selection_error:
                        raise selection_error
                    cls._apply_composite_voice_to_queue(
                        page,
                        rows,
                        ranges,
                        cancel_check=cancel_check,
                    )
                    voice_name = get_voice_info(
                        rows[ranges[0][0]].get("voice_key") or DEFAULT_FEMALE
                    )["name"]
                    group_duration_ms = round(
                        (time.perf_counter() - group_started_at) * 1000
                    )
                    _log(
                        f"[xunfei]   多人配音配置组 {entry_index}/{len(queue_plan)} "
                        f"已完成：{voice_name}，{sum(last - first + 1 for first, last in ranges)} 行，"
                        f"耗时 {group_duration_ms}ms"
                    )
                queue_error = None
                break
            except XunfeiCancelled:
                raise
            except XunfeiError as error:
                queue_error = error
                cls._close_composite_voice_panel(page, cancel_check=cancel_check)
                cls._clear_composite_queue(page, cancel_check=cancel_check)

        try:
            if queue_error:
                raise queue_error
        except XunfeiCancelled:
            raise
        except XunfeiError as error:
            _log(
                f"[xunfei]   多人配音多段队列不可用，重新输入后按连续区间处理: {error}"
            )
            cls._close_composite_voice_panel(page, cancel_check=cancel_check)
            cls._clear_composite_queue(page, cancel_check=cancel_check)
            cls._input_composite_text(page, rows, cancel_check=cancel_check)
            marking_plan = cls._composite_marking_plan(rows)
            base_index = marking_plan["base_index"]
            correction_groups = marking_plan["correction_groups"]
            try:
                cls._select_editor_rows(
                    page,
                    rows,
                    0,
                    len(rows) - 1,
                    cancel_check=cancel_check,
                )
                cls._apply_composite_voice_to_selection(
                    page,
                    rows,
                    0,
                    len(rows) - 1,
                    config_row=rows[base_index],
                    cancel_check=cancel_check,
                )
            except XunfeiCancelled:
                raise
            except XunfeiError as fallback_error:
                _log(
                    f"[xunfei]   多人配音全文基准标注失败，按连续区间处理: {fallback_error}"
                )
                cls._close_composite_voice_panel(page, cancel_check=cancel_check)
                cls._input_composite_text(page, rows, cancel_check=cancel_check)
                for first_index, last_index in groups:
                    _check_cancel_requested(cancel_check)
                    cls._select_editor_rows(
                        page,
                        rows,
                        first_index,
                        last_index,
                        cancel_check=cancel_check,
                    )
                    cls._apply_composite_voice_to_selection(
                        page,
                        rows,
                        first_index,
                        last_index,
                        cancel_check=cancel_check,
                    )
            else:
                for first_index, last_index in correction_groups:
                    _check_cancel_requested(cancel_check)
                    cls._select_editor_rows(
                        page,
                        rows,
                        first_index,
                        last_index,
                        cancel_check=cancel_check,
                    )
                    cls._apply_composite_voice_to_selection(
                        page,
                        rows,
                        first_index,
                        last_index,
                        cancel_check=cancel_check,
                    )
            marking_mode = "连续区间回退"
            marking_group_count = len(correction_groups) + 1
        else:
            marking_mode = "多段队列"
            marking_group_count = len(queue_plan)

        marking_duration_ms = round((time.perf_counter() - started_at) * 1000)
        _log(
            f"[xunfei]   多人配音音色标注完成：模式={marking_mode}，"
            f"统一配置组 {marking_group_count} 组，耗时 {marking_duration_ms}ms"
        )

        pause_started_at = time.perf_counter()
        for row_index, boundary_ms in boundaries:
            _check_cancel_requested(cancel_check)
            cls._insert_composite_pause(
                page,
                row_index,
                boundary_ms,
                emit_log=False,
                verify=False,
                cancel_check=cancel_check,
            )
        if boundaries:
            all_pauses_inserted = _poll(
                lambda: (
                    True
                    if not cls._read_composite_pause_issues(page, boundaries)
                    else None
                ),
                timeout=3,
                interval=0.04,
                max_interval=0.2,
                page=page,
                cancel_check=cancel_check,
            )
            if not all_pauses_inserted:
                issues = cls._read_composite_pause_issues(page, boundaries)
                duplicate_rows = [item for item in issues if item.get("count", 0) > 1]
                if duplicate_rows:
                    raise XunfeiError(
                        "讯飞停顿插入校验失败：检测到重复停顿标记，"
                        f"行 {[item['row'] + 1 for item in duplicate_rows]}"
                    )
                for item in issues:
                    _check_cancel_requested(cancel_check)
                    cls._insert_composite_pause(
                        page,
                        item["row"],
                        item["value"],
                        emit_log=False,
                        verify=True,
                        cancel_check=cancel_check,
                    )
                if cls._read_composite_pause_issues(page, boundaries):
                    raise XunfeiError("讯飞停顿批量插入后回读仍不完整")
            _log(
                f"[xunfei]   多人配音停顿已批量完成：{len(boundaries)} 处，"
                f"耗时 {round((time.perf_counter() - pause_started_at) * 1000)}ms，"
                "每处均按段落末尾 UI 定位并回读校验"
            )
        return rows, boundaries

    def _generate_pending_composite(
        self,
        work,
        *,
        output_name=None,
        max_retries=4,
        cancel_check=None,
    ):
        """通过讯飞多人配音页面提交作品，返回待下载作品信息。

        多人配音的编辑内容、音色标记、参数和停顿全部由可见页面操作完成；
        这里不调用 makeMultipleSpeakerWork 或 order_gen 提交接口。生成按钮
        点击后的 worksId 仍由已有响应监听器捕获，下载阶段继续复用精确
        worksId/签名 URL 流程。
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")
        if not output_name:
            output_name = f".xunfei_composite_{uuid.uuid4().hex}.mp3"
        output_path = _confined_temp_output_path(output_name)
        page = self._page
        recovery_kwargs = (
            {"cancel_check": cancel_check} if cancel_check is not None else {}
        )
        works_name = self._normalize_works_name(
            work.get("works_name")
            or f"wordtts_composite_{uuid.uuid4().hex[:8]}"
        )
        last_error = None
        for attempt in range(1, max_retries + 1):
            _check_cancel_requested(cancel_check)
            submission_confirmed = False
            _log(
                f"[xunfei]   多人配音作品提交 {attempt}/{max_retries}: "
                f"{works_name}（{len(work.get('item_ids') or [])} 道题）"
            )
            try:
                if page.locator(".ssml-editor").count() == 0:
                    if not self._recover_and_retry(page, **recovery_kwargs):
                        raise XunfeiError("页面恢复失败")

                # 文本、连续同配置批量选区、音色/参数和内部停顿均通过
                # 可见讯飞页面完成，并且每次套用前都有选区/标记回读校验。
                prepare_kwargs = {}
                if cancel_check is not None:
                    prepare_kwargs["cancel_check"] = cancel_check
                self._prepare_composite_editor(page, work, **prepare_kwargs)
                _check_cancel_requested(cancel_check)
                self._mark_works_cutoff()
                click_kwargs = {}
                if cancel_check is not None:
                    click_kwargs["cancel_check"] = cancel_check
                self._click_generate(page, **click_kwargs)
                try:
                    confirm_kwargs = {"works_name": works_name}
                    # _click_generate 已经跨过提交边界。确认弹窗、订单状态
                    # 和作品 ID 对账必须完整执行，不能让取消请求造成已扣费
                    # 作品无法恢复的中间态。
                    status = self._confirm_synth(page, **confirm_kwargs)
                except XunfeiSubmissionAmbiguous:
                    raise
                except XunfeiCancelled:
                    raise
                except Exception as confirm_error:
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击，但后续页面状态异常；"
                            "已暂停自动重试，请稍后按作品名对账",
                            works_name=works_name,
                        ) from confirm_error
                    raise
                if status == "insufficient":
                    raise XunfeiQuotaExceeded("讯飞配音额度不足")
                if status == "login":
                    raise XunfeiLoginRequired("合成过程中弹出登录框，请重新登录")
                if status == "rate_limited":
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击后触发频控，提交结果不确定；"
                            "已暂停自动重试，请稍后按作品名对账",
                            works_name=works_name,
                        )
                    raise XunfeiRateLimited("触发讯飞频控")
                if status != "ok":
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击，但确认流程未完成；"
                            "已暂停自动重试，请稍后按作品名对账",
                            works_name=works_name,
                        )
                    raise XunfeiError("确认合成弹窗流程未完成")
                submission_confirmed = True

                try:
                    # 确认合成已经成功后进入不可打断的对账事务：即使用户
                    # 此时请求暂停/终止，也必须先把 worksId 或唯一作品名
                    # 保存下来，否则下一轮无法判断是否已扣费。
                    works_id = self._consume_works_id(
                        timeout=30,
                    )
                    if not works_id:
                        works_id = self._recover_works_id_by_name(
                            page,
                            works_name,
                            timeout=60,
                        )
                except (XunfeiCancelled, XunfeiSubmissionAmbiguous):
                    raise
                except Exception as tracking_error:
                    raise XunfeiSubmissionAmbiguous(
                        "合成已确认提交，但追踪 worksId 时发生异常；"
                        "已暂停自动重试，请稍后按作品名对账",
                        works_name=works_name,
                    ) from tracking_error
                if not works_id:
                    raise XunfeiSubmissionAmbiguous(
                        "多人配音已确认提交，但无法安全定位唯一 worksId；"
                        "已暂停自动重试，请稍后按作品名对账",
                        works_name=works_name,
                    )
                pending = {
                    "works_id": str(works_id),
                    "output_path": output_path,
                    "works_name": works_name,
                    "work_id": str(work.get("work_id") or work.get("job_id") or ""),
                    "item_count": int(work.get("item_count") or 0),
                }
                _log(
                    f"[xunfei] ✅ 多人配音作品已提交 worksId={pending['works_id']} "
                    f"work={pending['work_id']}"
                )
                # 此处作品已经提交并拿到 worksId。清理页面属于提交后的
                # best-effort 操作，异常不能回到外层“提交失败”重试，否则
                # 同一作品可能再次扣费；收尾也不能再被取消探针打断。
                try:
                    self._cleanup_after_item(page)
                except Exception as cleanup_error:
                    _log(f"[xunfei]   提交后页面清理异常（不重复提交）: {cleanup_error}")
                return pending
            except (
                XunfeiQuotaExceeded,
                XunfeiLoginRequired,
                XunfeiSubmissionAmbiguous,
                XunfeiCancelled,
            ):
                raise
            except XunfeiRateLimited as error:
                last_error = error
                cooldown = 18 + (time.time() % 10) * 2
                _log(f"[xunfei]   多人配音频控冷却 {cooldown:.0f}s 后重试提交")
                _controlled_wait(page, cooldown, cancel_check=cancel_check)
                if (
                    attempt < max_retries
                    and not self._recover_and_retry(page, **recovery_kwargs)
                ):
                    break
            except Exception as error:
                if submission_confirmed:
                    raise XunfeiSubmissionAmbiguous(
                        "多人配音已确认提交，但提交结果整理时发生异常；"
                        "已暂停自动重试，请稍后按作品名对账",
                        works_name=works_name,
                    ) from error
                last_error = error
                _log(f"[xunfei]   多人配音提交异常: {error}")
                if (
                    attempt < max_retries
                    and not self._recover_and_retry(page, **recovery_kwargs)
                ):
                    break
        raise XunfeiError(f"讯飞多人配音生成失败：{last_error or '已重试仍未成功'}")

    def _generate_pending_one(
        self,
        text,
        output_name=None,
        works_name=None,
        max_retries=4,
        voice_key=None,
        speed=PARAM_DEFAULT,
        pitch=PARAM_DEFAULT,
        volume=PARAM_DEFAULT,
        cancel_check=None,
    ):
        """只提交一条合成并返回 worksId，不在本处下载。

        这是批量流程的第一阶段：页面始终停留在编辑页，成功后只关闭弹窗、
        清空编辑器，保留当前音色和参数缓存给同组下一条任务复用。
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")

        vk = voice_key if voice_key else self.voice_key
        voice_name = get_voice_info(vk)["name"]
        if not output_name:
            output_name = f".xunfei_{uuid.uuid4().hex}.mp3"
        output_path = _confined_temp_output_path(output_name)
        # 作品名必须在点击生成前确定并写入页面。它是 worksId 监听漏捕获
        # 时唯一可用于安全对账的幂等键，不能留空后再随机生成。
        works_name = self._normalize_works_name(
            works_name or f"wordtts_{uuid.uuid4().hex[:16]}"
        )

        page = self._page
        recovery_kwargs = (
            {"cancel_check": cancel_check} if cancel_check is not None else {}
        )
        last_error = None
        for attempt in range(1, max_retries + 1):
            _check_cancel_requested(cancel_check)
            submission_confirmed = False
            _log(f"[xunfei]   第 {attempt}/{max_retries} 次尝试提交...")
            try:
                if page.locator(".ssml-editor").count() == 0:
                    if not self._recover_and_retry(page, **recovery_kwargs):
                        raise XunfeiError("页面恢复失败")

                # 同组任务命中这两个缓存时，不会重复切换音色或设置参数。
                voice_kwargs = {"voice_key": vk}
                params_kwargs = {}
                if cancel_check is not None:
                    voice_kwargs["cancel_check"] = cancel_check
                    params_kwargs["cancel_check"] = cancel_check
                self._select_voice(page, voice_name, **voice_kwargs)
                self._apply_params(page, speed, pitch, volume, **params_kwargs)

                input_kwargs = {}
                if cancel_check is not None:
                    input_kwargs["cancel_check"] = cancel_check
                if not self._input_text(page, text, **input_kwargs):
                    raise XunfeiError("文本输入失败")

                _check_cancel_requested(cancel_check)
                self._mark_works_cutoff()
                click_kwargs = {}
                if cancel_check is not None:
                    click_kwargs["cancel_check"] = cancel_check
                self._click_generate(page, **click_kwargs)
                try:
                    confirm_kwargs = {"works_name": works_name}
                    # _click_generate 已经跨过提交边界。确认弹窗、订单状态
                    # 和作品 ID 对账必须完整执行，不能让取消请求造成已扣费
                    # 作品无法恢复的中间态。
                    status = self._confirm_synth(page, **confirm_kwargs)
                except XunfeiSubmissionAmbiguous:
                    raise
                except XunfeiCancelled:
                    raise
                except Exception as confirm_error:
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击，但后续页面状态异常；"
                            "已暂停自动重试，请稍后按作品名对账",
                            works_name=works_name,
                        ) from confirm_error
                    raise
                if status == "insufficient":
                    raise XunfeiQuotaExceeded("讯飞配音额度不足")
                if status == "login":
                    raise XunfeiLoginRequired("合成过程中弹出登录框，请重新登录")
                if status == "rate_limited":
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击后触发频控，提交结果不确定；"
                            "已暂停自动重试，请稍后按作品名对账",
                            works_name=works_name,
                        )
                    raise XunfeiRateLimited("触发讯飞频控")
                if status != "ok":
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击，但确认流程未完成；"
                            "已暂停自动重试，请稍后按作品名对账",
                            works_name=works_name,
                        )
                    raise XunfeiError("确认合成弹窗流程未完成")
                submission_confirmed = True

                try:
                    # 确认合成已经成功后进入不可打断的对账事务：即使用户
                    # 此时请求暂停/终止，也必须先把 worksId 或唯一作品名
                    # 保存下来，否则下一轮无法判断是否已扣费。
                    works_id = self._consume_works_id(
                        timeout=30,
                    )
                    if not works_id:
                        works_id = self._recover_works_id_by_name(
                            page,
                            works_name,
                            timeout=60,
                        )
                except (XunfeiCancelled, XunfeiSubmissionAmbiguous):
                    raise
                except Exception as tracking_error:
                    raise XunfeiSubmissionAmbiguous(
                        "合成已确认提交，但追踪 worksId 时发生异常；"
                        "已暂停自动重试，请稍后按作品名对账",
                        works_name=works_name,
                    ) from tracking_error
                if not works_id:
                    raise XunfeiSubmissionAmbiguous(
                        "合成已确认提交，但无法安全定位唯一 worksId；"
                        "已暂停自动重试，请稍后按作品名对账",
                        works_name=works_name,
                    )

                pending = {
                    "works_id": str(works_id),
                    "output_path": output_path,
                    "voice_key": vk,
                    "voice_name": voice_name,
                    "works_name": works_name,
                    "speed": clamp_param(speed),
                    "pitch": clamp_param(pitch),
                    "volume": clamp_param(volume),
                }
                _log(
                    f"[xunfei] ✅ 已提交待下载任务 worksId={pending['works_id']} "
                    f"voice={voice_name}"
                )
                # worksId 已经确认，清理失败不能被当作提交失败处理；否则
                # 外层恢复逻辑会重新点击合成并可能产生重复计费；收尾不再
                # 读取取消探针，确保本次已扣费提交先完整落入待下载队列。
                try:
                    self._cleanup_after_item(page)
                except Exception as cleanup_error:
                    _log(f"[xunfei]   提交后页面清理异常（不重复提交）: {cleanup_error}")
                return pending

            except (
                XunfeiQuotaExceeded,
                XunfeiLoginRequired,
                XunfeiSubmissionAmbiguous,
                XunfeiCancelled,
            ):
                raise
            except XunfeiRateLimited as error:
                last_error = error
                cooldown = 18 + (time.time() % 10) * 2
                _log(f"[xunfei]   频控冷却 {cooldown:.0f}s 后重试提交")
                _controlled_wait(page, cooldown, cancel_check=cancel_check)
                self._recover_and_retry(page, **recovery_kwargs)
            except Exception as attempt_error:
                if submission_confirmed:
                    raise XunfeiSubmissionAmbiguous(
                        "合成已确认提交，但提交结果整理时发生异常；"
                        "已暂停自动重试，请稍后按作品名对账",
                        works_name=works_name,
                    ) from attempt_error
                last_error = attempt_error
                _log(f"[xunfei]   第 {attempt} 次提交异常: {attempt_error}")
                if not self._recover_and_retry(page, **recovery_kwargs):
                    break

        raise XunfeiError(f"讯飞配音生成失败：{last_error or '已重试仍未成功'}")

    def _wait_for_pending_ready(
        self,
        page,
        pending_items,
        timeout=180,
        cancel_check=None,
    ):
        """批量等待精确 worksId 对应的音频地址就绪。"""
        _check_cancel_requested(cancel_check)
        duplicate_ids = self._duplicate_pending_work_ids(pending_items)
        if duplicate_ids:
            raise XunfeiError(
                "批量任务捕获到重复 worksId，拒绝继续下载以免音频错配："
                + ", ".join(sorted(duplicate_ids))
            )
        remaining = {
            str(item["works_id"]): item
            for item in pending_items
            if item.get("works_id")
        }
        ready = {}
        if not remaining:
            return ready

        deadline = time.time() + timeout
        matched = set()
        target_count = max(len(remaining), 1)
        while remaining and time.time() < deadline:
            _check_cancel_requested(cancel_check)
            fetch_kwargs = {
                "needed_count": target_count,
                "expected_ids": set(remaining),
            }
            if cancel_check is not None:
                fetch_kwargs["cancel_check"] = cancel_check
            records = self._fetch_works_list_pages(page, **fetch_kwargs)
            for record in records:
                if not isinstance(record, dict):
                    continue
                record_id = record.get("id") or record.get("worksId")
                expected = str(record_id) if record_id is not None else ""
                item = remaining.get(expected)
                if item is None:
                    continue
                if expected not in matched:
                    _log(f"[xunfei]   ✅ 作品列表已匹配 worksId: {expected}")
                    matched.add(expected)

                audio_url = record.get("audioUrl")
                if not audio_url:
                    # 列表记录可能先出现、音频地址后补齐；只对同一个
                    # worksId 请求签名 URL，绝不复用其它记录的最新地址。
                    audio_url = self._fetch_sign_url_in_page(
                        page,
                        expected,
                        log_result=False,
                        cancel_check=cancel_check,
                    )
                if audio_url:
                    ready[expected] = {
                        **item,
                        "record": dict(record),
                        "download_url": audio_url,
                    }
                    remaining.pop(expected, None)
                    _log(f"[xunfei]   ✅ 匹配作品音频已就绪 worksId: {expected}")

            if remaining:
                if not matched:
                    _log(
                        f"[xunfei]   ⏳ 等待 {len(remaining)} 条作品匹配 worksId"
                    )
                else:
                    _log(
                        f"[xunfei]   ⏳ 仍有 {len(remaining)} 条作品等待音频就绪"
                    )
                _check_cancel_requested(cancel_check)
                _controlled_wait(page, 2.0, cancel_check=cancel_check)

        if remaining:
            _log(
                "[xunfei]   ⚠️ 批量等待音频超时，未就绪 worksId: "
                + ", ".join(sorted(remaining))
            )
        return ready

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

    def _select_download_rows(self, page, targets, cancel_check=None):
        """在讯飞作品页按 worksId 对应的 orderNo 精确勾选作品行。"""
        selected = {}
        missing = list(targets)
        for attempt in range(8):
            _check_cancel_requested(cancel_check)
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
            _check_cancel_requested(cancel_check)
            _safe_eval(page, JS.SCROLL_DOWNLOAD_LIST)
            _controlled_wait(page, 0.5, cancel_check=cancel_check)

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
    def _download_signed_url(download_url, output_path, cancel_check=None):
        """通过精确 worksId 对应的签名地址下载 MP3。"""
        _check_cancel_requested(cancel_check)
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
            with urllib.request.urlopen(request, timeout=SIGNED_DOWNLOAD_TIMEOUT_SECONDS) as response:
                with open(temporary_path, "wb") as target:
                    while True:
                        _check_cancel_requested(cancel_check)
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        target.write(chunk)
            if not _looks_like_mp3(temporary_path):
                raise XunfeiError("签名地址返回的文件不是有效 MP3")
            os.replace(temporary_path, output_path)
            return True
        except XunfeiCancelled:
            raise
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
                timeout=CONTROLLED_NAVIGATION_TIMEOUT_MS,
            )
            _check_cancel_requested(cancel_check)
        except XunfeiCancelled:
            raise
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
                # 没有返回它。先用同一个 worksId 直接请求签名地址作为补偿
                # 路径；即使仍未拿到地址，也保留提交记录，避免临时接口故障
                # 触发重复合成。
                direct_url = self._fetch_sign_url_in_page(
                    page,
                    works_id,
                    log_result=False,
                    cancel_check=cancel_check,
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
                # “列表完整但没看到 ID”仍不能证明作品失效：列表刷新、账号
                # 权限和异步入库都可能让已提交作品暂时不可见。保留 worksId
                # 进入下一轮断点对账，绝不因为一次缺行自动重提交并重复计费。
                works_id_invalid = False
                result = {
                    **item,
                    "downloaded": False,
                    "works_id_invalid": works_id_invalid,
                    "error": (
                        "作品未在下载页按 worksId 就绪；已保留提交记录，稍后可继续对账"
                    ),
                }
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
                cancel_check=cancel_check,
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

        select_kwargs = {}
        if cancel_check is not None:
            select_kwargs["cancel_check"] = cancel_check
        selected, missing = self._select_download_rows(
            page,
            browser_targets,
            **select_kwargs,
        )
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

    @staticmethod
    def _group_batch_jobs(jobs):
        """按音色 + 三项参数分组，保留每组首次出现的顺序。"""
        groups = {}
        for job in jobs:
            voice_key = str(job.get("voice_key") or DEFAULT_FEMALE)
            key = (
                voice_key,
                clamp_param(job.get("speed")),
                clamp_param(job.get("pitch")),
                clamp_param(job.get("volume")),
            )
            groups.setdefault(key, []).append(job)
        return list(groups.values())

    def synth_batch(
        self,
        jobs,
        max_retries=4,
        progress_callback=None,
        cancel_check=None,
    ):
        """按音色/参数分组，先全部提交合成，最后统一下载。

        返回 ``job_id -> result``。单条失败会记录在对应结果中，已成功提交
        的其它任务仍会进入统一下载阶段。``progress_callback`` 会在线程内
        收到每条任务的下载事件和最终保存结果；调用方不得在回调中操作
        Playwright 页面。
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")
        normalized_jobs = [dict(job) for job in jobs if isinstance(job, dict)]
        grouped_jobs = self._group_batch_jobs(normalized_jobs)
        pending = []
        results = {}
        reported_progress = set()

        def report_progress(payload):
            if not callable(progress_callback):
                return
            item = dict(payload or {})
            job_id = str(item.get("job_id") or "")
            stage = str(item.get("stage") or "saved")
            report_key = (job_id, stage)
            if job_id and report_key in reported_progress:
                return
            if job_id:
                reported_progress.add(report_key)
            _notify_batch_progress(progress_callback, item)

        for group_index, group in enumerate(grouped_jobs, start=1):
            _check_cancel_requested(cancel_check)
            if not group:
                continue
            sample = group[0]
            _log(
                f"[xunfei] 批量生成分组 {group_index}/{len(grouped_jobs)}: "
                f"{get_voice_info(sample.get('voice_key') or DEFAULT_FEMALE)['name']} "
                f"speed={clamp_param(sample.get('speed'))}, "
                f"pitch={clamp_param(sample.get('pitch'))}, "
                f"volume={clamp_param(sample.get('volume'))}，共 {len(group)} 条"
            )
            for job in group:
                _check_cancel_requested(cancel_check)
                job_id = str(job.get("job_id") or uuid.uuid4().hex)
                try:
                    resume_works_id = str(job.get("resume_works_id") or "").strip()
                    ambiguous_works_name = str(
                        job.get("ambiguous_works_name") or ""
                    ).strip()
                    if resume_works_id:
                        output_name = job.get("output_name") or (
                            f".xunfei_{uuid.uuid4().hex}.mp3"
                        )
                        pending_item = {
                            "works_id": resume_works_id,
                            "output_path": _confined_temp_output_path(output_name),
                            "voice_key": str(job.get("voice_key") or DEFAULT_FEMALE),
                            "voice_name": str(job.get("voice_name") or ""),
                            "works_name": self._normalize_works_name(
                                job.get("works_name")
                            ),
                            "speed": clamp_param(job.get("speed")),
                            "pitch": clamp_param(job.get("pitch")),
                            "volume": clamp_param(job.get("volume")),
                        }
                        resumed = True
                        _log(
                            f"[xunfei] ♻️ 复用已提交任务 worksId={resume_works_id} "
                            f"job={job_id}"
                        )
                    elif ambiguous_works_name:
                        output_name = job.get("output_name") or (
                            f".xunfei_{uuid.uuid4().hex}.mp3"
                        )
                        works_name = self._normalize_works_name(
                            ambiguous_works_name or job.get("works_name")
                        )
                        recovered_works_id = self._recover_works_id_by_name(
                            self._page,
                            works_name,
                            timeout=60,
                            cancel_check=cancel_check,
                        )
                        if not recovered_works_id:
                            raise XunfeiSubmissionAmbiguous(
                                "上次已确认提交，但仍无法按作品名定位唯一 worksId；"
                                "不会重新提交",
                                works_name=works_name,
                            )
                        pending_item = {
                            "works_id": str(recovered_works_id),
                            "output_path": _confined_temp_output_path(output_name),
                            "voice_key": str(job.get("voice_key") or DEFAULT_FEMALE),
                            "voice_name": str(job.get("voice_name") or ""),
                            "works_name": works_name,
                            "speed": clamp_param(job.get("speed")),
                            "pitch": clamp_param(job.get("pitch")),
                            "volume": clamp_param(job.get("volume")),
                        }
                        resumed = True
                        _log(
                            f"[xunfei] ♻️ 通过作品名找回已提交任务 "
                            f"worksId={recovered_works_id} job={job_id}"
                        )
                    else:
                        generate_kwargs = {
                            "output_name": job.get("output_name"),
                            "works_name": job.get("works_name"),
                            "max_retries": max_retries,
                            "voice_key": job.get("voice_key"),
                            "speed": job.get("speed", PARAM_DEFAULT),
                            "pitch": job.get("pitch", PARAM_DEFAULT),
                            "volume": job.get("volume", PARAM_DEFAULT),
                        }
                        if cancel_check is not None:
                            generate_kwargs["cancel_check"] = cancel_check
                        pending_item = self._generate_pending_one(
                            job.get("text", ""),
                            **generate_kwargs,
                        )
                        resumed = False
                    pending_item["job_id"] = job_id
                    pending.append(pending_item)
                    # 统一下载模式下，提交每个音频段也是可见进度的一部分。
                    # 如果只等到下载页全部返回，长文档在生成阶段会一直显示 0%。
                    report_progress({
                        "job_id": job_id,
                        "works_id": pending_item.get("works_id"),
                        "resumed": resumed,
                        "downloaded": False,
                        "stage": "submitted",
                    })
                except (
                    XunfeiQuotaExceeded,
                    XunfeiLoginRequired,
                    XunfeiCancelled,
                ):
                    raise
                except XunfeiSubmissionAmbiguous as error:
                    result = {
                        "job_id": job_id,
                        "downloaded": False,
                        "ambiguous_works_id": True,
                        "works_name": error.works_name or job.get("works_name"),
                        "error": str(error),
                    }
                    results[job_id] = result
                    report_progress({
                        "job_id": job_id,
                        "downloaded": False,
                        "ambiguous_works_id": True,
                        "works_name": result["works_name"],
                        "stage": "saved",
                        "error": result["error"],
                    })
                except Exception as error:
                    result = {
                        "job_id": job_id,
                        "downloaded": False,
                        "error": str(error),
                    }
                    results[job_id] = result
                    report_progress({
                        "job_id": job_id,
                        "downloaded": False,
                        "stage": "saved",
                        "error": result["error"],
                    })

        duplicate_ids = self._duplicate_pending_work_ids(pending)
        pending_for_download = []
        for item in pending:
            _check_cancel_requested(cancel_check)
            works_id = str(item.get("works_id") or "")
            if works_id not in duplicate_ids:
                pending_for_download.append(item)
                continue
            job_id = str(item["job_id"])
            error = f"本批次 worksId 重复，无法安全归属音频：{works_id}"
            result = {
                **item,
                "job_id": job_id,
                "downloaded": False,
                "ambiguous_works_id": True,
                "error": error,
            }
            results[job_id] = result
            report_progress({
                "job_id": job_id,
                "works_id": works_id,
                "ambiguous_works_id": True,
                "downloaded": False,
                "stage": "saved",
                "error": error,
            })

        download_error = None
        try:
            _check_cancel_requested(cancel_check)
            download_kwargs = {}
            if callable(progress_callback):
                download_kwargs["progress_callback"] = report_progress
            if cancel_check is not None:
                download_kwargs["cancel_check"] = cancel_check
            downloaded = self._download_pending_batch(
                pending_for_download,
                **download_kwargs,
            )
        except XunfeiCancelled:
            raise
        except Exception as error:
            downloaded = {}
            download_error = f"讯飞批量统一下载异常：{error}"
            _log(f"[xunfei] ❌ {download_error}")

        for item in pending_for_download:
            job_id = str(item["job_id"])
            works_id = str(item["works_id"])
            result = downloaded.get(works_id)
            if result:
                results[job_id] = {**result, "job_id": job_id}
            else:
                result = {
                    **item,
                    "job_id": job_id,
                    "downloaded": False,
                    "error": download_error or "合成已提交但统一下载失败",
                }
                results[job_id] = result
            if callable(progress_callback) and (
                job_id,
                "saved",
            ) not in reported_progress:
                report_progress({
                    "job_id": job_id,
                    "works_id": str(result.get("works_id") or works_id),
                    "works_id_invalid": bool(result.get("works_id_invalid")),
                    "downloaded": bool(result.get("downloaded")),
                    "stage": "saved",
                    "error": result.get("error"),
                })
        return results

    def synth_composite(
        self,
        works,
        max_retries=4,
        progress_callback=None,
        resume=None,
        cancel_check=None,
    ):
        """提交多人配音作品并统一下载，返回 work_id -> 文件结果。

        ``resume`` 只复用已经落盘的 worksId，不会因为切割失败而重新计费；
        下载失败时由上层决定是否重试或切换到原有单段模式。
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")
        normalized_works = [dict(work) for work in works if isinstance(work, dict)]
        if not normalized_works:
            return {}
        resume_map = resume if isinstance(resume, dict) else {}
        pending = []
        results = {}
        reported_progress = set()

        def report_progress(payload):
            if not callable(progress_callback):
                return
            item = dict(payload or {})
            work_id = str(item.get("work_id") or item.get("job_id") or "")
            stage = str(item.get("stage") or "saved")
            key = (work_id, stage)
            if work_id and key in reported_progress:
                return
            if work_id:
                reported_progress.add(key)
            _notify_batch_progress(progress_callback, item)

        for work in normalized_works:
            _check_cancel_requested(cancel_check)
            work_id = str(work.get("work_id") or work.get("job_id") or uuid.uuid4().hex)
            work["work_id"] = work_id
            work["job_id"] = work_id
            work.setdefault(
                "output_name",
                f".xunfei_composite_{uuid.uuid4().hex}.mp3",
            )
            work.setdefault(
                "works_name",
                f"wordtts_composite_{int(work.get('work_index') or 1):04d}_{uuid.uuid4().hex[:8]}",
            )
            previous = resume_map.get(work_id)
            previous_id = previous.get("works_id") if isinstance(previous, dict) else None
            previous_ambiguous = bool(
                previous.get("ambiguous_submission")
                or previous.get("ambiguous_works_id")
            ) if isinstance(previous, dict) else False
            try:
                if previous_id:
                    pending_item = {
                        "works_id": str(previous_id),
                        "output_path": _confined_temp_output_path(work["output_name"]),
                        "works_name": str(previous.get("works_name") or work["works_name"]),
                        "work_id": work_id,
                        "job_id": work_id,
                        "item_count": int(work.get("item_count") or 0),
                    }
                    _log(
                        f"[xunfei] ♻️ 复用多人配音作品 worksId={pending_item['works_id']} "
                        f"work={work_id}"
                    )
                elif previous_ambiguous:
                    works_name = self._normalize_works_name(
                        (previous or {}).get("works_name") or work["works_name"]
                    )
                    recovered_works_id = self._recover_works_id_by_name(
                        self._page,
                        works_name,
                        timeout=60,
                        cancel_check=cancel_check,
                    )
                    if not recovered_works_id:
                        raise XunfeiSubmissionAmbiguous(
                            "上次已确认多人配音提交，但仍无法按作品名定位唯一 worksId；"
                            "不会重新提交",
                            works_name=works_name,
                        )
                    pending_item = {
                        "works_id": str(recovered_works_id),
                        "output_path": _confined_temp_output_path(work["output_name"]),
                        "works_name": works_name,
                        "work_id": work_id,
                        "job_id": work_id,
                        "item_count": int(work.get("item_count") or 0),
                    }
                    _log(
                        f"[xunfei] ♻️ 通过作品名找回多人配音作品 "
                        f"worksId={recovered_works_id} work={work_id}"
                    )
                else:
                    generate_kwargs = {
                        "output_name": work.get("output_name"),
                        "max_retries": max_retries,
                    }
                    if cancel_check is not None:
                        generate_kwargs["cancel_check"] = cancel_check
                    pending_item = self._generate_pending_composite(
                        work,
                        **generate_kwargs,
                    )
                    pending_item["job_id"] = work_id
                    pending_item["work_id"] = work_id
                pending.append(pending_item)
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "works_id": pending_item.get("works_id"),
                    "works_name": pending_item.get("works_name") or work.get("works_name"),
                    "stage": "submitted",
                    "downloaded": False,
                })
            except (
                XunfeiQuotaExceeded,
                XunfeiLoginRequired,
                XunfeiCancelled,
            ):
                raise
            except XunfeiSubmissionAmbiguous as error:
                result = {
                    "work_id": work_id,
                    "downloaded": False,
                    "audio": None,
                    "ambiguous_works_id": True,
                    "works_name": error.works_name or work.get("works_name"),
                    "error": str(error),
                }
                results[work_id] = result
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "ambiguous_works_id": True,
                    "works_name": result["works_name"],
                    "stage": "saved",
                    "downloaded": False,
                    "error": result["error"],
                })
            except Exception as error:
                results[work_id] = {
                    "work_id": work_id,
                    "downloaded": False,
                    "audio": None,
                    "error": str(error),
                }
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "stage": "saved",
                    "downloaded": False,
                    "error": str(error),
                })

        duplicate_ids = self._duplicate_pending_work_ids(pending)
        pending_for_download = []
        for pending_item in pending:
            _check_cancel_requested(cancel_check)
            works_id = str(pending_item.get("works_id") or "")
            if works_id not in duplicate_ids:
                pending_for_download.append(pending_item)
                continue
            work_id = str(
                pending_item.get("work_id")
                or pending_item.get("job_id")
                or ""
            )
            error = f"本批次 worksId 重复，无法安全归属音频：{works_id}"
            results[work_id] = {
                **pending_item,
                "work_id": work_id,
                "job_id": work_id,
                "downloaded": False,
                "audio": None,
                "ambiguous_works_id": True,
                "error": error,
            }
            report_progress({
                "work_id": work_id,
                "job_id": work_id,
                "works_id": works_id,
                "ambiguous_works_id": True,
                "stage": "saved",
                "downloaded": False,
                "error": error,
            })

        download_error = None
        try:
            _check_cancel_requested(cancel_check)
            download_kwargs = {
                "progress_callback": report_progress if callable(progress_callback) else None,
            }
            if cancel_check is not None:
                download_kwargs["cancel_check"] = cancel_check
            downloaded = self._download_pending_batch(
                pending_for_download,
                **download_kwargs,
            )
        except XunfeiCancelled:
            raise
        except Exception as error:
            downloaded = {}
            download_error = f"讯飞多人配音统一下载异常：{error}"
            _log(f"[xunfei] ❌ {download_error}")

        for pending_item in pending_for_download:
            _check_cancel_requested(cancel_check)
            work_id = str(pending_item.get("work_id") or pending_item.get("job_id") or "")
            works_id = str(pending_item.get("works_id") or "")
            result = downloaded.get(works_id)
            if result:
                results[work_id] = {**result, "work_id": work_id, "job_id": work_id}
            else:
                results[work_id] = {
                    **pending_item,
                    "work_id": work_id,
                    "job_id": work_id,
                    "downloaded": False,
                    "error": download_error or "多人配音作品已提交但统一下载失败",
                }
            if callable(progress_callback) and (work_id, "saved") not in reported_progress:
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "works_id": works_id,
                    "works_id_invalid": bool(results[work_id].get("works_id_invalid")),
                    "stage": "saved",
                    "downloaded": bool(results[work_id].get("downloaded")),
                    "error": results[work_id].get("error"),
                })
        return results

    def synth_one(
        self,
        text,
        output_name=None,
        max_retries=4,
        voice_key=None,
        speed=PARAM_DEFAULT,
        pitch=PARAM_DEFAULT,
        volume=PARAM_DEFAULT,
    ):
        """
        在已登录的浏览器会话中生成一条音频。
        生成完成后浏览器与页面状态保持，等待下一条。

        Args:
            text: 要合成的文本
            output_name: 输出文件名（不含路径），None 时自动生成
            max_retries: 最大重试次数
            voice_key: 发音人 key（覆盖默认），如 "amanda"/"george"
            speed/pitch/volume: 讯飞平台三参数（0-100，50=默认）

        Returns: 生成的音频文件路径
        Raises:
            XunfeiQuotaExceeded: 额度不足（应停止整批任务）
            XunfeiRateLimited: 触发频控
            XunfeiLoginRequired: 会话失效
            XunfeiError: 其他生成失败
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")

        vk = voice_key if voice_key else self.voice_key
        voice_name = get_voice_info(vk)["name"]

        if not output_name:
            output_name = f"xunfei_{vk}_{int(time.time())}.mp3"
        output_path = _confined_temp_output_path(output_name)

        pending = self._generate_pending_one(
            text,
            output_name=output_name,
            max_retries=max_retries,
            voice_key=voice_key,
            speed=speed,
            pitch=pitch,
            volume=volume,
        )
        output_path = pending["output_path"]
        try:
            result = self._download_pending_batch([pending]).get(str(pending["works_id"]))
            if result and result.get("downloaded") and os.path.exists(output_path):
                _log(f"[xunfei] ✅ 生成成功 ({os.path.getsize(output_path):,} bytes)")
                return output_path
            raise XunfeiError("合成已完成但未能下载音频")
        except Exception:
            _remove_confined_temp_output(output_path)
            raise

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
            self._browser_executable_path = None
            self._browser_mode = None
            with self._browser_identity_lock:
                self._browser_controller = None
                self._browser_started_at = None
                self._browser_pid = None
                self._browser_process_ids = []
                self._browser_process_ids_before = set()
                self._browser_window_handles = []
                self._browser_page_count = 0
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


# ============================================================================
# 模块级异步接口（供 word_tts_app.py 调用）
# ============================================================================

_session = None
_session_lock = threading.Lock()
_browser_owner_session_id = None
_playwright_executor = None
_playwright_executor_lock = threading.Lock()
_playwright_activity_condition = threading.Condition()
_playwright_pending_calls = 0


def _get_playwright_executor():
    """返回专供 Playwright Sync API 使用的单线程执行器。"""
    global _playwright_executor
    with _playwright_executor_lock:
        if _playwright_executor is None:
            _playwright_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="xunfei-playwright",
            )
        return _playwright_executor


def _mark_playwright_call_done(_future):
    global _playwright_pending_calls
    with _playwright_activity_condition:
        _playwright_pending_calls = max(0, _playwright_pending_calls - 1)
        _playwright_activity_condition.notify_all()


def _submit_playwright_call(call):
    """提交同步调用并跟踪到真正完成（包括 asyncio 方被取消的情况）。"""
    global _playwright_pending_calls
    with _playwright_activity_condition:
        _playwright_pending_calls += 1
    try:
        future = _get_playwright_executor().submit(call)
    except Exception:
        _mark_playwright_call_done(None)
        raise
    future.add_done_callback(_mark_playwright_call_done)
    return future


def _wait_for_playwright_idle_sync(timeout=None):
    deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
    with _playwright_activity_condition:
        while _playwright_pending_calls:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            _playwright_activity_condition.wait(remaining)
        return True


async def wait_for_playwright_idle(timeout=None):
    """等待已提交的同步 Playwright 调用真正退出。

    asyncio 取消只能取消包装 Future，不能中断已经运行的 Sync API。清理会话
    目录或启动下一轮之前必须调用此屏障，避免旧线程继续写入新一轮产物。
    """
    import asyncio

    return await asyncio.to_thread(_wait_for_playwright_idle_sync, timeout)


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
    future = _submit_playwright_call(call)
    return await asyncio.wrap_future(future, loop=loop)


async def _run_playwright_sync_until_done(function, *args, **kwargs):
    """外层任务取消时，仍等待计费相关的同步调用完成。"""
    import asyncio

    operation = asyncio.create_task(_run_playwright_sync(function, *args, **kwargs))
    cancellation_requested = False
    while True:
        try:
            result = await asyncio.shield(operation)
            break
        except asyncio.CancelledError:
            # shield 防止包装 Future 被取消；继续等待专用线程退出，避免
            # 调用方 finally 提前删除仍在写入的临时 MP3。完成后再把取消
            # 交还给上层状态机。
            cancellation_requested = True
            if operation.done():
                try:
                    result = operation.result()
                except asyncio.CancelledError:
                    result = None
                break
            continue
    if cancellation_requested:
        raise asyncio.CancelledError
    return result


def is_available():
    """检查讯飞配音模块是否可用。"""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _session_is_healthy(session):
    """轻量级健康检查。"""
    if session is None:
        return False
    if not getattr(session, "_logged_in", False):
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


def _normalize_owner_session_id(value):
    text = str(value or "").strip()
    return text or None


def _owner_mismatch_state(owner_session_id, current_owner):
    expected = _normalize_owner_session_id(owner_session_id)
    if not expected or not current_owner or expected == current_owner:
        return None
    return _unavailable_browser_snapshot(
        "专用自动化浏览器当前归属于另一个任务",
        owner_session_id=current_owner,
        owner_mismatch=True,
    )


async def ensure_session(
    voice_key="amanda",
    cancel_check=None,
    owner_session_id=None,
    allow_system_chrome=False,
):
    """
    确保讯飞配音浏览器会话已登录。
    如果会话不存在或已损坏，则创建并打开浏览器等待用户登录。
    """
    global _session, _browser_owner_session_id

    if not is_available():
        raise XunfeiError("讯飞配音模块不可用，请安装 playwright")

    import asyncio

    def _locked_create():
        global _session, _browser_owner_session_id
        requested_owner = _normalize_owner_session_id(owner_session_id)
        old_session = None

        # 只在极短的“检查/摘除全局引用”临界区持有锁。健康检查本身会
        # 读取 Sync Playwright 对象，旧会话 close 和新会话 login 还可能等待
        # 用户操作；若把它们放在锁内，浏览器状态 API 会把 FastAPI 事件循环
        # 阻塞数分钟，恢复快捷键和 SSE 也会一起失去响应。
        with _session_lock:
            if _session is not None:
                mismatch = _owner_mismatch_state(
                    requested_owner,
                    _browser_owner_session_id,
                )
                if mismatch is not None:
                    raise XunfeiSessionBusy(mismatch["last_error"])
                if _session_is_healthy(_session):
                    if requested_owner and not _browser_owner_session_id:
                        _browser_owner_session_id = requested_owner
                    return _session
                old_session = _session
                _session = None
                _browser_owner_session_id = None

        if old_session is not None:
            try:
                old_session.close()
            except Exception:
                pass

        session = XunFeiSession(voice_key=voice_key)
        try:
            login_kwargs = {"login_timeout": 300}
            if cancel_check is not None:
                login_kwargs["cancel_check"] = cancel_check
            if allow_system_chrome:
                login_kwargs["allow_system_chrome"] = True
            session.login(**login_kwargs)
        except Exception:
            try:
                session.close()
            except Exception:
                pass
            raise
        # 必须在线程内写入全局会话。否则两个 asyncio 任务同时首次
        # 调用 ensure_session 时，第二个任务可能在主事件循环中看见
        # 仍为空，再创建第二个 Playwright Sync 会话。所有 ensure/close
        # 调用又都排在同一个单线程 executor 中，所以这里不会和下一轮
        # 会话创建交叉覆盖。
        with _session_lock:
            _session = session
            _browser_owner_session_id = requested_owner
        return session

    # Playwright Sync API 的所有 page/context 操作（包括健康检查）都必须
    # 留在同一个专用线程，不能在 FastAPI/asyncio 事件循环线程或其它
    # 默认线程池线程调用，否则会触发 greenlet 跨线程异常。
    return await _run_playwright_sync(_locked_create)


async def synth_xunfei(
    text, voice_key="amanda",
    speed=PARAM_DEFAULT, pitch=PARAM_DEFAULT, volume=PARAM_DEFAULT,
    owner_session_id=None,
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

    session = await ensure_session(
        voice_key=voice_key,
        owner_session_id=owner_session_id,
    )

    import uuid
    output_name = f".xunfei_{uuid.uuid4().hex[:8]}.mp3"

    result_path = None
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
        return seg
    finally:
        # 无论下载失败、解码失败还是调用方取消，都不能把临时 MP3 留在
        # xunfei_output；仅允许删除本模块自己目录内的路径。
        _remove_confined_temp_output(result_path)


async def synth_xunfei_batch(
    jobs,
    progress_callback=None,
    cancel_check=None,
    max_retries=4,
    owner_session_id=None,
):
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
        owner_session_id=owner_session_id,
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

    expected_temp_paths = [
        _confined_temp_output_path(item["output_name"])
        for item in normalized_jobs
    ]

    batch_kwargs = {"progress_callback": progress_callback}
    if cancel_check is not None:
        batch_kwargs["cancel_check"] = cancel_check
    raw_results = None
    try:
        raw_results = await _run_playwright_sync_until_done(
            session.synth_batch,
            normalized_jobs,
            max(1, int(max_retries)),
            **batch_kwargs,
        )
        decoded = {}
        from pydub import AudioSegment

        for job in normalized_jobs:
            _check_cancel_requested(cancel_check)
            job_id = str(job["job_id"])
            result = raw_results.get(job_id) if isinstance(raw_results, dict) else None
            path = result.get("output_path") if isinstance(result, dict) else None
            try:
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
                _remove_confined_temp_output(path)
        return decoded
    finally:
        # 失败结果、未返回结果和解码中途取消的任务也必须清理；此处只会
        # 处理已通过文件名校验的当前批次临时路径，不会碰用户其它文件。
        for path in expected_temp_paths:
            _remove_confined_temp_output(path)
        if isinstance(raw_results, dict):
            for result in raw_results.values():
                if isinstance(result, dict):
                    _remove_confined_temp_output(result.get("output_path"))


async def synth_xunfei_composite(
    works,
    progress_callback=None,
    resume=None,
    cancel_check=None,
    max_retries=4,
    owner_session_id=None,
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
        owner_session_id=owner_session_id,
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

    expected_temp_paths = [
        _confined_temp_output_path(work["output_name"])
        for work in normalized_works
    ]

    composite_kwargs = {
        "progress_callback": progress_callback,
        "resume": resume,
    }
    if cancel_check is not None:
        composite_kwargs["cancel_check"] = cancel_check
    raw_results = None
    try:
        raw_results = await _run_playwright_sync_until_done(
            session.synth_composite,
            normalized_works,
            max(1, int(max_retries)),
            **composite_kwargs,
        )
        decoded = {}
        from pydub import AudioSegment

        for work in normalized_works:
            _check_cancel_requested(cancel_check)
            work_id = str(work["work_id"])
            result = raw_results.get(work_id) if isinstance(raw_results, dict) else None
            path = result.get("output_path") if isinstance(result, dict) else None
            try:
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
                    "works_id": result.get("works_id") if isinstance(result, dict) else None,
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
                    "error": str(error),
                }
            finally:
                _remove_confined_temp_output(path)
        return decoded
    finally:
        for path in expected_temp_paths:
            _remove_confined_temp_output(path)
        if isinstance(raw_results, dict):
            for result in raw_results.values():
                if isinstance(result, dict):
                    _remove_confined_temp_output(result.get("output_path"))


async def close_session(owner_session_id=None):
    """关闭讯飞配音浏览器会话。"""
    global _session, _browser_owner_session_id

    def _close_owned_session():
        global _session, _browser_owner_session_id
        # 整个“校验归属 -> 摘除全局引用 -> close”作为一个专用线程调用
        # 执行。这样 ensure_session 若同时排队，只会在旧 close 完成后运行，
        # 不会出现新会话已经创建、旧会话随后又被误关的窗口。
        with _session_lock:
            mismatch = _owner_mismatch_state(
                owner_session_id,
                _browser_owner_session_id,
            )
            if mismatch is not None:
                raise XunfeiSessionBusy(mismatch["last_error"])
            old = _session
            _session = None
            _browser_owner_session_id = None
        if old is None:
            return False
        try:
            old.close()
            _log("[xunfei] 浏览器会话已关闭")
            return True
        except Exception as error:
            _log(f"[xunfei] 关闭浏览器会话异常: {error}")
            return False

    await _run_playwright_sync(_close_owned_session)


def _unavailable_browser_snapshot(
    reason="自动化浏览器尚未启动",
    *,
    owner_session_id=None,
    owner_mismatch=False,
):
    return {
        "visibility": "unavailable",
        "platform": sys.platform,
        "permission_required": False,
        "last_error": reason,
        "pid": None,
        "process_ids": [],
        "executable_path": None,
        "profile_dir": PROFILE_DIR,
        "started_at": None,
        "window_handles": [],
        "context_id": None,
        "page_id": None,
        "page_count": 0,
        "logged_in": False,
        "browser_mode": None,
        "owner_session_id": owner_session_id,
        "owner_mismatch": bool(owner_mismatch),
    }


async def browser_snapshot(owner_session_id=None):
    """返回当前专用浏览器的身份和原生窗口状态。"""
    global _browser_owner_session_id
    import asyncio
    with _session_lock:
        session = _session
        current_owner = _browser_owner_session_id
    mismatch = _owner_mismatch_state(owner_session_id, current_owner)
    if mismatch is not None:
        return mismatch
    if session is None:
        return _unavailable_browser_snapshot()
    snapshot = await asyncio.to_thread(session.browser_snapshot)
    snapshot["owner_session_id"] = current_owner
    snapshot["owner_mismatch"] = False
    return snapshot


async def set_browser_visibility(
    visible: bool,
    *,
    minimize=False,
    owner_session_id=None,
):
    """在不触碰 Playwright Sync API 的情况下控制专用浏览器窗口。"""
    import asyncio
    with _session_lock:
        session = _session
        current_owner = _browser_owner_session_id
    mismatch = _owner_mismatch_state(owner_session_id, current_owner)
    if mismatch is not None:
        return mismatch
    if session is None:
        return _unavailable_browser_snapshot()
    snapshot = await asyncio.to_thread(
        session.set_browser_visibility,
        visible,
        minimize=minimize,
    )
    snapshot["owner_session_id"] = current_owner
    snapshot["owner_mismatch"] = False
    return snapshot


# ============================================================================
# 命令行直接调用
# ============================================================================
if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello, this is a test."
    voice = sys.argv[2] if len(sys.argv) > 2 else "amanda"
    sp = int(sys.argv[3]) if len(sys.argv) > 3 else PARAM_DEFAULT
    pi = int(sys.argv[4]) if len(sys.argv) > 4 else PARAM_DEFAULT
    vo = int(sys.argv[5]) if len(sys.argv) > 5 else PARAM_DEFAULT
    r = XunFeiSession(voice_key=voice)
    r.login()
    path = r.synth_one(text, speed=sp, pitch=pi, volume=vo)
    if path:
        _log(f"\n✅ 文件: {path}")
    r.close()
