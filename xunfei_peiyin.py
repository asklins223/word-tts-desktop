#!/usr/bin/env python3
"""
讯飞配音自动化客户端（wordtts 统一 TTS 引擎）
====================
通过 Playwright 有头 Chrome 自动化操作讯飞配音网站 (peiyin.xunfei.cn/make)，
实现文本到语音的合成与下载。

设计:
  - 使用持久化浏览器配置目录，首次需手动登录，后续自动复用登录状态
  - 单条合成：输入文本 → 选发音人 → 设置语速/语调/音量 → 生成音频 → 确认合成 → 拦截 worksId → 签名 URL 下载
  - 多人合成：在可见编辑器中输入所有行，先用出现频率最高的配置覆盖全文，再只修正例外区间，插入短停顿后点击页面生成，再按 worksId 下载
  - 页面复用：生成阶段保持编辑页，提交完成后进入作品下载页；按 worksId
    获取精确签名地址下载，浏览器下载仅作为按作品名匹配的兜底通道
  - 反批量检测采用行为拟真：击键抖动、随机间隙、真实鼠标事件、系统 Chrome 真实指纹

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

IS_MAC = sys.platform == "darwin"
_SELECT_ALL = "Meta+A" if IS_MAC else "Control+A"


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
        VOICES[key] = {
            "name": name,
            "display": f"{name} ({gender_label})",
            "gender": gender,
            "speaker_no": speaker_no,
            "common_id": common_id,
            "img_url": voice.get("img_url") or voice.get("imgUrl") or "",
            "language": language,
            "vcn_type": vcn_type,
            "speaker_language": speaker_language,
            "is_vip": is_vip,
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
    () => window.getSelection?.().toString() || ''
    """

    SELECT_EDITOR_RANGE = """
    ([firstIndex, lastIndex]) => {
        const paragraphs = Array.from(
            document.querySelectorAll('.ssml-editor p')
        );
        const first = paragraphs[Number(firstIndex)];
        const last = paragraphs[Number(lastIndex)];
        if (!first || !last) return null;

        const firstTextNode = (root) => {
            const walker = document.createTreeWalker(
                root,
                NodeFilter.SHOW_TEXT,
            );
            return walker.nextNode();
        };
        const lastTextNode = (root) => {
            const walker = document.createTreeWalker(
                root,
                NodeFilter.SHOW_TEXT,
            );
            let current = null;
            let next = walker.nextNode();
            while (next) {
                current = next;
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
        return selection.toString();
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


def _poll(check_fn, timeout, interval=0.5, page=None, max_interval=None):
    """轮询等待 check_fn 返回 truthy；自适应退避但保留延迟页面兜底。"""
    deadline = time.monotonic() + max(0, float(timeout))
    current_interval = max(0.05, float(interval))
    upper_interval = max(current_interval, float(max_interval or current_interval * 2.5))
    while True:
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
        self._real_ua = None
        # 页面状态跟踪（页面复用的关键）。音色 key 和页面显示名称都保留：
        # key 防止同名音色串用，页面回读防止讯飞提交后把音色恢复为默认值。
        self._current_voice_key = None
        self._current_voice_name = None
        self._applied_params = None  # dict(speed=, pitch=, volume=) 或 None
        # worksId 捕获（时间戳截止线防跨条错配）
        self._works_lock = threading.Lock()
        self._works_entries = []     # [(works_id, ts)]
        self._works_cutoff = 0.0
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

    # ------------------------------------------------------------------
    # 拟人行为辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _pause(page, base, spread=0.4):
        """拟人等待 base±spread 秒。"""
        seconds = max(0.05, base + ((time.time() * 7) % 1) * 2 * spread - spread)
        page.wait_for_timeout(int(seconds * 1000))

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

    def _on_response(self, response):
        url = response.url
        try:
            self._remember_api_request(response.request)
            wid = None
            if "makeMultipleSpeakerWork" in url:
                data = response.json()
                response_code = data.get("retCode")
                if response_code is None:
                    response_code = data.get("code")
                if _provider_success_code(response_code):
                    wid = data.get("tempWorksId") or data.get("worksId")
            elif "order_gen" in url:
                data = response.json()
                response_code = data.get("code")
                if response_code is None:
                    response_code = data.get("retCode")
                if _provider_success_code(response_code):
                    wid = (data.get("data") or {}).get("payOrder", {}).get("worksId")
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
                    self._works_entries.append((wid, time.time()))
                _log(f"[xunfei]   📝 捕获 worksId: {wid}")
        except Exception:
            pass

    def _mark_works_cutoff(self):
        """每条任务开始前调用：之后的捕获才属于本条任务。"""
        with self._works_lock:
            self._works_entries.clear()
            self._sign_urls.clear()
            self._works_cutoff = time.time()

    def _consume_works_id(self, timeout=12, exclude_ids=None):
        excluded = {
            str(value)
            for value in (exclude_ids or [])
            if value not in (None, "")
        }
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._works_lock:
                fresh = [
                    e for e in self._works_entries
                    if e[1] >= self._works_cutoff - 0.5
                    and str(e[0]) not in excluded
                ]
                if fresh:
                    wid = fresh[-1][0]
                    self._works_entries.clear()
                    return wid
            time.sleep(0.25)
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

    def _clear_editor(self, page):
        """清空文本编辑器内容。"""
        _safe_eval(page, JS.CLEAR_EDITOR)
        page.wait_for_timeout(200)
        actual = _safe_eval(page, JS.GET_EDITOR_TEXT)
        if actual:
            try:
                page.locator(".ssml-editor").first.click(timeout=3000)
                page.keyboard.press(_SELECT_ALL)
                page.keyboard.press("Backspace")
                page.wait_for_timeout(200)
            except Exception:
                pass

    def _input_text(self, page, text):
        """在编辑器中拟人输入文本并验证。"""
        self._clear_editor(page)
        page.locator(".ssml-editor").first.click(timeout=5000)
        self._pause(page, 0.15, 0.08)
        page.keyboard.press(_SELECT_ALL)
        page.keyboard.press("Backspace")
        self._pause(page, 0.1, 0.05)
        self._type_text(page, text)
        page.wait_for_timeout(150)

        for attempt in range(2):
            actual = _safe_eval(page, JS.GET_EDITOR_TEXT) or ""
            if len(actual) >= len(text) * 0.85:
                return True
            _log(f"[xunfei]   输入验证失败 (attempt {attempt + 1})，重试...")
            self._clear_editor(page)
            page.locator(".ssml-editor").first.click(timeout=5000)
            self._type_text(page, text)
            page.wait_for_timeout(150)
        return False

    @staticmethod
    def _clear_editor_with_keyboard(page):
        """只用真实键盘操作清空编辑器，供多人配音 UI 流程使用。"""
        editor = page.locator(".ssml-editor").first
        editor.click(timeout=5000)
        page.keyboard.press(_SELECT_ALL)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)
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
    def _input_composite_text(cls, page, rows):
        """把多人配音的逻辑行按真实编辑器段落输入并回读。"""
        values = [str(row.get("text") or "") for row in rows]
        if not values or any(not value.strip() for value in values):
            raise XunfeiError("多人配音 UI 文本包含空行，无法安全定位选区")
        cls._clear_editor_with_keyboard(page)
        editor = page.locator(".ssml-editor").first
        editor.click(timeout=5000)
        cls._type_text(page, "\n".join(values))
        page.wait_for_timeout(250)
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
    def _select_editor_rows(cls, page, rows, first_index, last_index):
        """通过真实页面选区选中一行或一段连续逻辑行。

        讯飞编辑器通常会把多行文本放进可滚动的 contenteditable 中。
        仅靠一次从首行拖到末行的鼠标动作，在长文档或打包客户端的小窗口
        中很容易因为滚动导致首尾不同时可见，进而误选或漏选。这里按真实
        浏览器交互的可靠性依次尝试 Shift-click、鼠标拖选，最后才用页面
        Range 重新建立同一个浏览器选区；三种方式都必须通过精确文本回读。
        任何方式都失败时直接停止，不能把一个本应批量设置的组拆成逐行操作。
        """
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
            first.select_text(timeout=5000)
            page.wait_for_timeout(80)
            return cls._verify_editor_selection(page, expected_values)

        errors = []

        # 方式一：先真实选中首行，再滚动到末行并 Shift-click。这个动作
        # 不要求首尾同时出现在视口中，最适合打包客户端的窄窗口和长文档。
        try:
            first.scroll_into_view_if_needed(timeout=5000)
            first.select_text(timeout=5000)
            last.scroll_into_view_if_needed(timeout=5000)
            last_box = last.bounding_box()
            if not last_box:
                raise XunfeiError("末行不可见，无法执行 Shift-click")
            last.click(
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
        except Exception as error:
            errors.append(f"Shift-click: {error}")

        # 方式二：短范围仍优先使用真实鼠标拖选，兼容讯飞页面没有稳定
        # 锚点行为的版本。只有首尾都在当前视口时才执行，避免跨滚动拖选。
        try:
            first.scroll_into_view_if_needed(timeout=5000)
            last.scroll_into_view_if_needed(timeout=5000)
            first_box = first.bounding_box()
            last_box = last.bounding_box()
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
        except Exception as error:
            errors.append(f"鼠标拖选: {error}")

        # 方式三：仍然只改变浏览器当前 Selection，不调用讯飞接口，也不
        # 修改编辑器内容。它是跨滚动场景的页面交互兜底，后续“使用”按钮
        # 仍由页面 UI 读取这个选区并产生 speaker 标记。
        try:
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
        except Exception as error:
            errors.append(f"页面选区兜底: {error}")

        detail = "；".join(str(error) for error in errors[-3:])
        raise XunfeiError(
            f"多人配音 UI 批量选区失败：行 {first_index + 1}-{last_index + 1}；{detail}"
        )

    def _select_voice(self, page, voice_name, voice_key=None):
        """搜索并选择指定发音人，并以页面实际选中态校验缓存。"""
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
            selected = _safe_eval(page, JS.CHECK_VOICE_SELECTED, voice_name)
            if selected:
                return mark_selected()

            search_input = page.locator(
                "input.h-full.w-full, input[placeholder*='搜索'], input[placeholder*='音色'], input[placeholder*='主播']"
            )
            if search_input.count() > 0:
                search_input.first.click(timeout=3000)
                search_input.first.fill("")
                self._pause(page, 0.15, 0.06)
                search_input.first.fill(voice_name)
                _poll(
                    lambda: _safe_eval(page, JS.CHECK_SEARCH_RESULT, voice_name),
                    timeout=5, interval=0.6, page=page,
                )

            clicked = _safe_eval(page, JS.SEARCH_AND_CLICK_VOICE, voice_name)
            if clicked:
                self._pause(page, 0.6, 0.25)
                selected = _safe_eval(page, JS.CHECK_VOICE_SELECTED, voice_name)
                if selected:
                    return mark_selected()
                _log(f"[xunfei]   发音人 '{voice_name}' 点击后未见选中态，重试...")

        raise XunfeiError(f"未找到或无法选中发音人: {voice_name}")

    def _apply_params(self, page, speed, pitch, volume):
        """
        设置语速/语调/音量三项并回读验证。
        与已应用参数一致时跳过；切换发音人后必须重新应用（站点会重置参数）。
        """
        targets = {"speed": clamp_param(speed), "pitch": clamp_param(pitch),
                   "volume": clamp_param(volume)}
        if self._applied_params == targets:
            return True

        labels = ("语速", "语调", "音量")
        values = (targets["speed"], targets["pitch"], targets["volume"])
        failed_labels = []
        for idx, (label, value) in enumerate(zip(labels, values)):
            ok = False
            # 方式一：真实键盘输入（点击 → 全选 → 输入 → Tab 失焦）
            try:
                loc = page.locator("input.w-12").nth(idx)
                if loc.count() > 0:
                    loc.click(timeout=3000)
                    page.keyboard.press(_SELECT_ALL)
                    page.keyboard.type(str(value))
                    page.keyboard.press("Tab")
                    self._pause(page, 0.25, 0.1)
                    readback = _safe_eval(page, JS.READ_PARAM_INPUTS) or []
                    ok = idx < len(readback) and readback[idx].strip() == str(value)
            except Exception:
                ok = False
            # 方式二：JS 注入兜底 + 回读验证
            if not ok:
                _safe_eval(page, JS.SET_PARAM_INPUT, [idx, value])
                self._pause(page, 0.2, 0.08)
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

    def _click_generate(self, page):
        """点击'生成音频'按钮。"""
        btn = page.locator("button", has_text="生成音频")
        if btn.count() == 0:
            btn = page.locator("button.bg-blue-600")
        if btn.count() == 0:
            raise XunfeiError("未找到'生成音频'按钮")
        btn.first.click(timeout=5000)
        _log("[xunfei]   已点击生成音频")
        self._pause(page, 0.6, 0.3)

    @staticmethod
    def _normalize_works_name(value):
        """收敛讯飞作品名称，避免下载页名称被截断或包含非法字符。"""
        text = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", str(value or "")).strip()
        return text[:25] or f"wordtts_{uuid.uuid4().hex[:10]}"

    def _set_works_name(self, page, works_name):
        """在作品设置弹窗中写入唯一名称，便于下载页人工核对。"""
        normalized = self._normalize_works_name(works_name)
        try:
            field = page.locator('input[placeholder*="作品名称"]:visible').first
            if field.count() == 0:
                return False
            field.click(timeout=3000)
            page.keyboard.press(_SELECT_ALL)
            page.keyboard.insert_text(normalized)
            page.keyboard.press("Tab")
            self._pause(page, 0.2, 0.08)
            actual = field.input_value(timeout=1000)
            if actual == normalized:
                _log(f"[xunfei]   作品名称已设置: {normalized}")
                return True
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

    def _ensure_mp3_format(self, page, timeout=10):
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

        result = _poll(set_probe, timeout=timeout, interval=0.35, page=page)
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

        state = _poll(read_probe, timeout=4, interval=0.25, page=page)
        if not isinstance(state, dict) or not state.get("checked"):
            # React 受控单选项偶尔会让 JS click 后的 DOM 更新稍慢；只有在
            # 回读仍未确认时才使用 locator，再次点击同一个 MP3 选项。
            fallback = self._set_mp3_format_with_locator(page)
            if fallback in {"already_locator", "clicked_locator"}:
                state = _poll(
                    read_probe, timeout=3, interval=0.25, page=page
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

    def _observe_after_first_confirm(self, page):
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
        )
        if not result:
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

    def _ensure_ai_switch_off(self, page, timeout=12):
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
                if self._handle_ai_flag_dialog(page, ensure_switch=False):
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
                        self._pause(page, 0.18, 0.05)
                        return None
                # JS click 没有让 React 受控状态变化时，降低频率再用
                # locator 点击真实 button[role=switch]，避免连续点同一开关。
                now = time.monotonic()
                if now - last_locator_attempt >= 0.65:
                    last_locator_attempt = now
                    if self._click_ai_switch_with_locator(page):
                        self._pause(page, 0.25, 0.08)
                return None

            # switch 尚未挂载时也给 locator 一次机会；页面继续异步渲染时，
            # 自适应轮询会再次回到这里，不会漏掉延迟出现的开关。
            now = time.monotonic()
            if now - last_locator_attempt >= 0.65:
                last_locator_attempt = now
                if self._click_ai_switch_with_locator(page):
                    self._pause(page, 0.25, 0.08)
            return None

        result = _poll(
            probe,
            timeout=timeout,
            interval=0.2,
            max_interval=0.85,
            page=page,
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

    def _handle_ai_flag_dialog(self, page, ensure_switch=True):
        def check_no_remind():
            result = _safe_eval(page, JS.CHECK_NO_REMIND)
            return result if result in {"clicked", "clicked_input", "clicked_label", "already"} else None

        checked = _poll(
            check_no_remind,
            timeout=10,
            interval=0.25,
            max_interval=1.0,
            page=page,
        )
        if not checked:
            checked = self._click_no_remind_with_locator(page)
        _log(f"[xunfei]   AI 标识弹窗‘不再提示’: {'✓' if checked else '✗'}{f' ({checked})' if checked else ''}")
        if not checked:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   AI 弹窗未勾选‘不再提示’，当前弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            return False
        self._pause(page, 0.35, 0.15)

        if ensure_switch:
            switch_state = self._ensure_ai_switch_off(page, timeout=12)
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
            self._pause(page, 0.35, 0.15)

        confirmed = bool(_poll(
            lambda: _safe_eval(page, JS.CLICK_AI_CONFIRM),
            timeout=12,
            interval=0.35,
            max_interval=1.0,
            page=page,
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
        )
        if not closed:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   AI 标识确认后弹窗仍存在: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            return False
        self._pause(page, 0.5, 0.2)
        return True

    def _wait_order_or_error(self, page, timeout):
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
        )
        if result:
            return result
        # 超时边界再做一次同步快照，覆盖最后一刻才挂载的错误/订单提示。
        info = _probe_synth_state(page)
        state = (info or {}).get("state")
        if state == "order":
            return "ok"
        return state if state in {"insufficient", "rate_limited", "login"} else None

    def _confirm_synth(self, page, works_name=None):
        """
        处理确认合成弹窗完整流程。

        返回: 'ok' | 'insufficient' | 'rate_limited' | 'login' | 'failed'
        """
        initial_ai_state = None

        def ensure_ai_setting(allow_missing=False):
            # “订单支付”/“去下载”弹窗已经说明作品提交完成；此时原来的
            # 作品设置弹窗已经被卸载，不可能再读到 AI switch。第一次提交
            # 前已确认过关闭状态，不能在这里再次轮询 8 秒等待不存在的开关。
            if allow_missing and initial_ai_state == "off":
                _log("[xunfei]   作品设置弹窗已关闭，沿用第一次确认前已验证的 AI 标识关闭状态")
                return True
            state = self._ensure_ai_switch_off(page, timeout=12)
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
            settled = self._wait_order_or_error(page, 4) or "failed"
            if settled == "ok" and not ensure_ai_setting():
                return "failed"
            return settled

        self._pause(page, 0.6, 0.3)

        # 讯飞“作品设置”弹窗中的格式是独立的 WAV/MP3 单选项。不能依赖
        # 默认勾选，也不能取第一个 option；提交前必须回读并确认 MP3。
        if not self._ensure_mp3_format(page):
            _log("[xunfei]   未能确认作品格式为 MP3，停止提交，避免误生成 WAV")
            return "failed"

        if works_name:
            self._set_works_name(page, works_name)

        # “作品设置”就是这次提交使用的最终设置，真实 DOM 中开关位于这里：
        # role="switch"、aria-checked="true"。必须在第一次确认合成前关闭，
        # 不能等弹窗切换或订单完成后再处理，否则水印配置已经被提交。
        initial_ai_state = self._ensure_ai_switch_off(page, timeout=12)
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

        outcome = self._observe_after_first_confirm(page)
        _log(f"[xunfei]   第一次确认后的页面状态: {outcome}")
        ai_modal_seen = outcome == "ai_modal"
        if outcome == "ai_modal":
            _log("[xunfei]   检测到 AI 标识说明弹窗")
            if not self._handle_ai_flag_dialog(page, ensure_switch=False):
                _log("[xunfei]   AI 标识弹窗未完成确认，停止本次合成")
                return "failed"
        elif outcome in ("order", "insufficient", "rate_limited"):
            if outcome == "order" and not ensure_ai_setting(allow_missing=True):
                return "failed"
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
        )
        if followup == "ai_modal":
            ai_modal_seen = True
            # 少数页面会在第一次 AI 弹窗确认后重新挂载一次弹窗，允许再处理一轮。
            _log("[xunfei]   AI 标识弹窗仍在，重新处理")
            if not self._handle_ai_flag_dialog(page, ensure_switch=False):
                _log("[xunfei]   AI 标识弹窗二次处理失败，停止本次合成")
                return "failed"
            followup = _poll(
                probe_followup,
                timeout=12,
                interval=0.35,
                max_interval=1.0,
                page=page,
            )
        _log(f"[xunfei]   二次确认前页面状态: {followup or '未发现明确状态'}")
        if followup in ("order", "insufficient", "rate_limited"):
            if followup == "order" and not ensure_ai_setting(allow_missing=True):
                return "failed"
            return "ok" if followup == "order" else followup

        # 真实“作品设置”弹窗的结构是 role="switch" + aria-checked，
        # 它可能不会触发 AI 说明弹窗；二次确认前再次强制回读并关闭。
        if not ensure_ai_setting(allow_missing=True):
            return "failed"
        clicked2 = bool(_poll(
            lambda: self._click_confirm_synth_button(page)
            or _safe_eval(page, JS.CLICK_BTN_IN_MODAL, "确认合成"),
            timeout=12,
            interval=0.35,
            max_interval=1.0,
            page=page,
        ))
        _log(f"[xunfei]   第二次确认合成: {'✓' if clicked2 else '✗'}")
        if clicked2:
            settled = self._wait_order_or_error(page, 90) or "ok"
            if settled == "ok" and not ensure_ai_setting(allow_missing=True):
                return "failed"
            return settled

        # 讯飞部分账号/版本在没有 AI 说明弹窗时，第一次“确认合成”就
        # 已经提交任务，不会再显示第二个确认按钮。等待一小段时间确认
        # 没有额度、登录或频控错误后，按已提交处理，避免误重试造成频控。
        settled = self._wait_order_or_error(page, 12)
        if settled:
            if settled == "ok" and not ensure_ai_setting(allow_missing=True):
                return "failed"
            return settled
        if ai_modal_seen:
            info = _probe_synth_state(page)
            if info and info.get("ai_modal"):
                return "failed"
        if not ensure_ai_setting(allow_missing=True):
            return "failed"
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

    def _fetch_works_list_in_page(self, page, needed_count=1):
        """获取已完成作品列表，返回讯飞原始作品对象。"""
        # 作品页按最新创建时间返回第 1 页；等待 1~3 个新 worksId 时没必要
        # 每轮都让页面解析 200 条历史记录。保留足够余量并限制上限，既能
        # 覆盖批量提交，也能降低 Chrome 在轮询期间的 JSON/DOM 开销。
        needed = max(1, int(needed_count or 1))
        page_size = max(50, min(200, needed + 20))
        param = {
            "needCount": 1,
            "pageIndex": 1,
            "pageSize": page_size,
            "worksName": "",
        }
        data = self._signed_api_post(page, API_WORKS_LIST_URL, param)
        if not data:
            return []
        items = (data.get("data") or {}).get("userWorksList") or []
        return items if isinstance(items, list) else []

    def _wait_for_works_entry(self, page, works_id, timeout=120):
        """等待同一个 worksId 出现在作品列表中，严禁按名称或最新记录替代。"""
        expected = str(works_id)
        deadline = time.time() + timeout
        logged_wait = False
        while time.time() < deadline:
            for item in self._fetch_works_list_in_page(page, needed_count=1):
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id") or item.get("worksId")
                if item_id is not None and str(item_id) == expected:
                    _log(f"[xunfei]   ✅ 作品列表已匹配 worksId: {expected}")
                    return item
            if not logged_wait:
                _log(f"[xunfei]   ⏳ 等待作品列表匹配 worksId: {expected}")
                logged_wait = True
            page.wait_for_timeout(2000)
        _log(f"[xunfei]   ⚠️ 作品列表未匹配到 worksId: {expected}")
        return None

    def _wait_for_works_ready(self, page, works_id, timeout=180):
        """等待精确 worksId 对应的音频文件真正可下载。"""
        expected = str(works_id)
        deadline = time.time() + timeout
        matched_logged = False
        waiting_logged = False
        while time.time() < deadline:
            items = self._fetch_works_list_in_page(page, needed_count=1)
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
                    page, expected, log_result=False
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

            page.wait_for_timeout(2000)

        _log(f"[xunfei]   ⚠️ 匹配作品在限定时间内仍不可下载 worksId: {expected}")
        return None

    def _fetch_sign_url_in_page(self, page, works_id, log_result=True):
        """在确认作品列表包含该 worksId 后请求对应签名 URL。"""
        param = {"worksId": str(works_id), "worksType": 1}
        data = self._signed_api_post(page, API_SIGN_URL, param)
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

    def _cleanup_after_item(self, page):
        """单条提交后关闭残留弹窗并清空编辑器，不刷新页面。"""
        _safe_eval(page, JS.CLOSE_ALL_MODALS, [])
        # 讯飞页面的音色和三项参数状态要跨条复用；这里只清空输入内容，
        # 不能用 goto/reload，否则同一音色分组会被迫重复选择和设置参数。
        self._clear_editor(page)
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
        )
        if not ready:
            self._pause(page, 0.25, 0.08)

    def _recover_and_retry(self, page):
        """合成失败后恢复页面状态（重新加载编辑页，重置音色/参数记忆）。"""
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(".ssml-editor", timeout=30000)
            self._current_voice_key = None
            self._current_voice_name = None
            self._applied_params = None
            return True
        except Exception as e:
            _log(f"[xunfei]   页面恢复失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 登录与会话
    # ------------------------------------------------------------------

    def login(self, login_timeout=300):
        """
        打开可见的 Chrome 浏览器，导航到讯飞配音。
        首次需要手动登录（手机号+验证码），后续自动复用已保存的登录状态。
        """
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

        # 注册网络响应监听（整个会话期间持续捕获 worksId / sign_url）
        self._response_handler = self._on_response
        self._page.on("response", self._response_handler)

        try:
            self._real_ua = self._page.evaluate("navigator.userAgent")
        except Exception:
            pass

        _log("[xunfei] 正在打开讯飞配音...")

        try:
            self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as goto_error:
            _log(f"[xunfei] 首次加载提示: {goto_error}")

        # 不再按秒轮询 document.readyState。讯飞页面可能持续有网络请求，
        # readyState=complete 并不等于编辑器可用；直接等待真正需要的编辑器
        # 节点，页面一旦就绪就立即继续，避免启动阶段白占 CPU 和最多 30 秒。
        try:
            self._page.wait_for_selector(
                ".ssml-editor", state="attached", timeout=30000
            )
        except Exception:
            _log("[xunfei] 页面编辑器未找到，重试加载...")
            try:
                self._page.goto(
                    HOME_URL, wait_until="domcontentloaded", timeout=60000
                )
            except Exception:
                pass
            try:
                self._page.wait_for_selector(
                    ".ssml-editor", state="attached", timeout=30000
                )
            except Exception:
                raise XunfeiError("无法加载讯飞配音编辑器")

        # 检测登录状态
        if self._is_logged_in(self._page):
            _log("[xunfei] 检测到已保存的登录状态，无需重新登录")
        else:
            _log("[xunfei] 登录状态无效，请在浏览器中手动登录...")
            _log(f"[xunfei] 等待用户登录（超时 {login_timeout} 秒）...")
            deadline = time.time() + login_timeout
            logged = False
            while time.time() < deadline:
                self._page.wait_for_timeout(2000)
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
        scope = cls._composite_ui_scope(page)
        controls = scope.locator(
            'button:visible, [role="button"]:visible, [data-speaker-id]:visible, '
            '.cursor-pointer:visible'
        )
        try:
            for index in range(min(controls.count(), 200)):
                control = controls.nth(index)
                try:
                    text = control.inner_text(timeout=500)
                except Exception:
                    continue
                if cls._normalize_composite_ui_text(text) != expected:
                    continue
                try:
                    if control.is_disabled():
                        continue
                except Exception:
                    pass
                control.click(timeout=5000)
                return True
        except Exception:
            pass
        return False

    @classmethod
    def _find_composite_voice_card(cls, page, voice_name):
        """寻找当前搜索结果中可见的目标音色卡片，不点击隐藏结果。"""
        scope = cls._composite_ui_scope(page)
        controls = scope.locator(
            'button:visible, [role="button"]:visible, [data-speaker-id]:visible, '
            '.cursor-pointer:visible'
        )
        for index in range(min(controls.count(), 300)):
            control = controls.nth(index)
            try:
                text = control.inner_text(timeout=500)
            except Exception:
                continue
            if not cls._composite_ui_text_matches(text, voice_name):
                continue
            normalized = cls._normalize_composite_ui_text(text)
            if normalized in {"多人配音", "使用"} or "使用" in normalized:
                continue
            try:
                if control.is_disabled():
                    continue
            except Exception:
                pass
            return control
        return None

    @classmethod
    def _open_composite_voice_panel(cls, page):
        """打开“多人配音”面板，并返回其搜索框。"""
        search = page.locator(
            'input[placeholder*="输入主播名称进行搜索"]:visible, '
            'input[placeholder*="输入主播名称"]:visible'
        )
        if search.count() == 0:
            if not cls._click_composite_ui_control(page, "多人配音"):
                raise XunfeiError("未找到可用的“多人配音”按钮")
            search = _poll(
                lambda: (
                    page.locator(
                        'input[placeholder*="输入主播名称进行搜索"]:visible, '
                        'input[placeholder*="输入主播名称"]:visible'
                    )
                    if page.locator(
                        'input[placeholder*="输入主播名称进行搜索"]:visible, '
                        'input[placeholder*="输入主播名称"]:visible'
                    ).count() > 0
                    else None
                ),
                timeout=8,
                interval=0.25,
                max_interval=0.8,
                page=page,
            )
        if not search or search.count() == 0:
            raise XunfeiError("“多人配音”面板未加载音色搜索框")
        return search.first

    @classmethod
    def _apply_composite_ui_params(cls, page, speed, pitch, volume):
        """在多人配音面板中用键盘设置三项参数并逐项回读。"""
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
        )
        if inputs is None or inputs.count() < 3:
            raise XunfeiError("“多人配音”面板的语速、语调、音量输入框未完整加载")

        for index, (label, value) in enumerate(zip(labels, targets)):
            field = inputs.nth(index)
            try:
                field.click(timeout=3000)
                page.keyboard.press(_SELECT_ALL)
                page.keyboard.type(str(value))
                page.keyboard.press("Tab")
                page.wait_for_timeout(180)
                actual = field.input_value(timeout=1000).strip()
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
        cls, page, rows, first_index, last_index, voice_name, speaker_number
    ):
        """确认目标行都被页面实际标记为目标音色。"""
        paragraphs = page.locator(".ssml-editor p")
        expected_id = str(speaker_number)
        for index in range(first_index, last_index + 1):
            marks = paragraphs.nth(index).locator(
                ".ssml-text-mark-speaker"
            )
            matched = False
            for mark_index in range(marks.count()):
                mark = marks.nth(mark_index)
                mark_id = (mark.get_attribute("data-speaker-id") or "").strip()
                mark_label = mark.get_attribute("data-label") or ""
                if (
                    (mark_id and mark_id == expected_id)
                    or (
                        not mark_id
                        and cls._composite_ui_text_matches(mark_label, voice_name)
                    )
                ):
                    matched = True
                    break
            if not matched:
                return False
        return True

    @classmethod
    def _apply_composite_voice_to_selection(
        cls, page, rows, first_index, last_index, *, config_row=None
    ):
        """给当前精确选区设置音色、参数，并回读页面的 speaker 标记。"""
        first_row = config_row or rows[first_index]
        voice_key = str(first_row.get("voice_key") or DEFAULT_FEMALE)
        info = dict(get_voice_info(voice_key))
        voice_name = str(info.get("name") or voice_key)
        speaker_number = cls._speaker_number(voice_key, info)
        if config_row is None:
            for index in range(first_index, last_index + 1):
                if cls._composite_row_signature(rows[index]) != cls._composite_row_signature(first_row):
                    raise XunfeiError("多人配音批量选区包含不同音色或参数，拒绝套用")

        search = cls._open_composite_voice_panel(page)
        search.click(timeout=3000)
        page.keyboard.press(_SELECT_ALL)
        page.keyboard.type(voice_name)
        card = _poll(
            lambda: cls._find_composite_voice_card(page, voice_name),
            timeout=8,
            interval=0.25,
            max_interval=0.8,
            page=page,
        )
        if card is None:
            raise XunfeiError(f"多人配音面板未找到音色卡片: {voice_name}")
        card.click(timeout=5000)
        page.wait_for_timeout(250)
        cls._apply_composite_ui_params(
            page,
            first_row.get("speed", PARAM_DEFAULT),
            first_row.get("pitch", PARAM_DEFAULT),
            first_row.get("volume", PARAM_DEFAULT),
        )
        if not cls._click_composite_ui_control(page, "使用"):
            raise XunfeiError(f"多人配音面板未找到可用的“使用”按钮: {voice_name}")
        verified = _poll(
            lambda: cls._verify_composite_voice_marks(
                page,
                rows,
                first_index,
                last_index,
                voice_name,
                speaker_number,
            ),
            timeout=8,
            interval=0.2,
            max_interval=0.8,
            page=page,
        )
        if not verified:
            raise XunfeiError(
                f"多人配音音色标记回读失败：行 {first_index + 1}-{last_index + 1} "
                f"未确认使用 {voice_name}"
            )
        _log(
            f"[xunfei]   多人配音已设置行 {first_index + 1}-{last_index + 1}: "
            f"{voice_name}, speed={clamp_param(first_row.get('speed'))}, "
            f"pitch={clamp_param(first_row.get('pitch'))}, "
            f"volume={clamp_param(first_row.get('volume'))}"
        )

    @classmethod
    def _insert_composite_pause(cls, page, row_index, boundary_ms):
        """在指定题目末行末尾通过页面停顿按钮插入内部定位标记。"""
        paragraphs = page.locator(".ssml-editor p")
        if row_index < 0 or row_index >= paragraphs.count():
            raise XunfeiError("多人配音停顿位置超出编辑器段落范围")
        paragraph = paragraphs.nth(row_index)
        # 先选中整行，再用方向键折叠到文本末尾，避免点击段落中部把
        # 停顿插到句中或被浏览器保留为跨段选区。
        paragraph.select_text(timeout=5000)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(100)
        label = f"{int(boundary_ms) / 1000:g}s"
        if not cls._click_composite_ui_control(page, label):
            raise XunfeiError(f"未找到讯飞停顿按钮: {label}")
        selector = (
            f'[data-type="break"][data-value="{int(boundary_ms)}"]'
        )
        inserted = _poll(
            lambda: paragraphs.nth(row_index).locator(selector).count() > 0,
            timeout=5,
            interval=0.2,
            max_interval=0.8,
            page=page,
        )
        if not inserted:
            raise XunfeiError(
                f"讯飞停顿插入校验失败：第 {row_index + 1} 行未找到 {boundary_ms}ms 标记"
            )
        _log(f"[xunfei]   已在第 {row_index + 1} 行后插入 {label} 停顿")

    @classmethod
    def _prepare_composite_editor(cls, page, work):
        """用讯飞页面 UI 构造多人作品，返回行和停顿边界。"""
        started_at = time.perf_counter()
        rows, boundaries = cls._composite_ui_rows(work)
        cls._input_composite_text(page, rows)
        groups = cls._composite_row_groups(rows)
        marking_plan = cls._composite_marking_plan(rows)
        base_index = marking_plan["base_index"]
        correction_groups = marking_plan["correction_groups"]
        _log(
            f"[xunfei]   多人配音 UI 已输入 {len(rows)} 行，"
            f"原连续配置 {len(groups)} 组；采用基准覆盖后需修正 "
            f"{len(correction_groups)} 个区间"
        )

        # 交错角色不能用 Chrome 原生 Selection 安全地组成多个非连续
        # Range。先覆盖全文，再修正例外区间，最终页面上的每一行仍会经过
        # speaker 标记回读校验。若“全文选区”在某个讯飞页面版本失败，
        # 重新输入文本后退回连续区间方案，保证正确性优先。
        try:
            cls._select_editor_rows(page, rows, 0, len(rows) - 1)
            cls._apply_composite_voice_to_selection(
                page,
                rows,
                0,
                len(rows) - 1,
                config_row=rows[base_index],
            )
        except XunfeiError as error:
            _log(
                f"[xunfei]   多人配音全文基准标注失败，重新输入后按连续区间处理: {error}"
            )
            cls._input_composite_text(page, rows)
            for first_index, last_index in groups:
                cls._select_editor_rows(page, rows, first_index, last_index)
                cls._apply_composite_voice_to_selection(
                    page, rows, first_index, last_index
                )
        else:
            for first_index, last_index in correction_groups:
                cls._select_editor_rows(page, rows, first_index, last_index)
                cls._apply_composite_voice_to_selection(
                    page, rows, first_index, last_index
                )

        marking_duration_ms = round((time.perf_counter() - started_at) * 1000)
        _log(
            f"[xunfei]   多人配音音色标注完成：全文基准 1 次，"
            f"例外修正 {len(correction_groups)} 组，耗时 {marking_duration_ms}ms"
        )

        for row_index, boundary_ms in boundaries:
            cls._insert_composite_pause(page, row_index, boundary_ms)
        return rows, boundaries

    def _generate_pending_composite(
        self,
        work,
        *,
        output_name=None,
        max_retries=4,
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
        output_path = os.path.join(OUTPUT_DIR, output_name)
        page = self._page
        works_name = self._normalize_works_name(
            work.get("works_name")
            or f"wordtts_composite_{uuid.uuid4().hex[:8]}"
        )
        last_error = None
        for attempt in range(1, max_retries + 1):
            _log(
                f"[xunfei]   多人配音作品提交 {attempt}/{max_retries}: "
                f"{works_name}（{len(work.get('item_ids') or [])} 道题）"
            )
            try:
                if page.locator(".ssml-editor").count() == 0:
                    if not self._recover_and_retry(page):
                        raise XunfeiError("页面恢复失败")

                # 文本、连续同配置批量选区、音色/参数和内部停顿均通过
                # 可见讯飞页面完成，并且每次套用前都有选区/标记回读校验。
                self._prepare_composite_editor(page, work)
                self._mark_works_cutoff()
                self._click_generate(page)
                status = self._confirm_synth(page, works_name=works_name)
                if status == "insufficient":
                    raise XunfeiQuotaExceeded("讯飞配音额度不足")
                if status == "login":
                    raise XunfeiLoginRequired("合成过程中弹出登录框，请重新登录")
                if status == "rate_limited":
                    raise XunfeiRateLimited("触发讯飞频控")
                if status != "ok":
                    raise XunfeiError("确认合成弹窗流程未完成")

                works_id = self._consume_works_id(timeout=30)
                if not works_id:
                    raise XunfeiError("多人配音已提交但未捕获 worksId")
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
                self._cleanup_after_item(page)
                return pending
            except (XunfeiQuotaExceeded, XunfeiLoginRequired):
                raise
            except XunfeiRateLimited as error:
                last_error = error
                cooldown = 18 + (time.time() % 10) * 2
                _log(f"[xunfei]   多人配音频控冷却 {cooldown:.0f}s 后重试提交")
                page.wait_for_timeout(int(cooldown * 1000))
            except Exception as error:
                last_error = error
                _log(f"[xunfei]   多人配音提交异常: {error}")
                if attempt < max_retries and not self._recover_and_retry(page):
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
        output_path = os.path.join(OUTPUT_DIR, output_name)

        page = self._page
        last_error = None
        for attempt in range(1, max_retries + 1):
            _log(f"[xunfei]   第 {attempt}/{max_retries} 次尝试提交...")
            try:
                if page.locator(".ssml-editor").count() == 0:
                    if not self._recover_and_retry(page):
                        raise XunfeiError("页面恢复失败")

                # 同组任务命中这两个缓存时，不会重复切换音色或设置参数。
                self._select_voice(page, voice_name, voice_key=vk)
                self._apply_params(page, speed, pitch, volume)

                if not self._input_text(page, text):
                    raise XunfeiError("文本输入失败")

                self._mark_works_cutoff()
                self._click_generate(page)
                status = self._confirm_synth(page, works_name=works_name)
                if status == "insufficient":
                    raise XunfeiQuotaExceeded("讯飞配音额度不足")
                if status == "login":
                    raise XunfeiLoginRequired("合成过程中弹出登录框，请重新登录")
                if status == "rate_limited":
                    raise XunfeiRateLimited("触发讯飞频控")
                if status != "ok":
                    raise XunfeiError("确认合成弹窗流程未完成")

                works_id = self._consume_works_id(timeout=30)
                if not works_id:
                    raise XunfeiError("合成已提交但未捕获本条 worksId")

                pending = {
                    "works_id": str(works_id),
                    "output_path": output_path,
                    "voice_key": vk,
                    "voice_name": voice_name,
                    "works_name": self._normalize_works_name(works_name) if works_name else "",
                    "speed": clamp_param(speed),
                    "pitch": clamp_param(pitch),
                    "volume": clamp_param(volume),
                }
                _log(
                    f"[xunfei] ✅ 已提交待下载任务 worksId={pending['works_id']} "
                    f"voice={voice_name}"
                )
                self._cleanup_after_item(page)
                return pending

            except (XunfeiQuotaExceeded, XunfeiLoginRequired):
                raise
            except XunfeiRateLimited as error:
                last_error = error
                cooldown = 18 + (time.time() % 10) * 2
                _log(f"[xunfei]   频控冷却 {cooldown:.0f}s 后重试提交")
                page.wait_for_timeout(int(cooldown * 1000))
                self._recover_and_retry(page)
            except Exception as attempt_error:
                last_error = attempt_error
                _log(f"[xunfei]   第 {attempt} 次提交异常: {attempt_error}")
                if not self._recover_and_retry(page):
                    break

        raise XunfeiError(f"讯飞配音生成失败：{last_error or '已重试仍未成功'}")

    def _wait_for_pending_ready(self, page, pending_items, timeout=180):
        """批量等待精确 worksId 对应的音频地址就绪。"""
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
        while remaining and time.time() < deadline:
            records = self._fetch_works_list_in_page(
                page, needed_count=max(len(remaining), 1)
            )
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
                        page, expected, log_result=False
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
                page.wait_for_timeout(2000)

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

    def _select_download_rows(self, page, targets):
        """在讯飞作品页按 worksId 对应的 orderNo 精确勾选作品行。"""
        selected = {}
        missing = list(targets)
        for attempt in range(8):
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
            _safe_eval(page, JS.SCROLL_DOWNLOAD_LIST)
            page.wait_for_timeout(500)

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

    def _download_selected_rows(self, page, selected_targets, progress_callback=None):
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
            if not self._click_visible_exact_button(page, "下载"):
                _log("[xunfei]   ❌ 下载页未找到可用的“下载”按钮")
                return []

            # 当前页面通常直接触发多个 MP3 下载；部分账号会先弹出
            # Ant Design 下载确认框，再点击确认按钮。
            page.wait_for_timeout(500)
            dialog = self._find_visible_dialog(page, "下载")
            if dialog is not None:
                if not self._click_visible_exact_button(page, "下载", scope=dialog):
                    _log("[xunfei]   ❌ 未能点击下载确认弹窗中的“下载”")
                    return downloads
                _log("[xunfei]   已确认下载弹窗")

            expected = len(selected_targets)
            deadline = time.time() + 120
            while len(downloads) < expected and time.time() < deadline:
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
    def _download_signed_url(download_url, output_path):
        """通过精确 worksId 对应的签名地址下载 MP3。"""
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
            with urllib.request.urlopen(request, timeout=60) as response:
                with open(temporary_path, "wb") as target:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        target.write(chunk)
            if not _looks_like_mp3(temporary_path):
                raise XunfeiError("签名地址返回的文件不是有效 MP3")
            os.replace(temporary_path, output_path)
            return True
        except (OSError, ValueError, urllib.error.URLError, XunfeiError) as error:
            _log(f"[xunfei]   worksId 签名下载失败: {error}")
            return False
        finally:
            try:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
            except OSError:
                pass

    def _download_pending_batch(self, pending_items, progress_callback=None):
        """进入讯飞作品下载页，按 worksId 勾选本批次作品后统一下载。"""
        page = self._page
        if not pending_items:
            return {}

        _log(
            f"[xunfei] 进入讯飞作品下载页，准备勾选本批次 {len(pending_items)} 条音频"
        )
        try:
            page.goto(
                DOWNLOAD_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as error:
            raise XunfeiError(f"无法打开讯飞作品下载页: {error}")

        if not _poll(
            lambda: bool(_safe_eval(page, JS.CHECK_DOWNLOAD_PAGE)),
            timeout=30,
            interval=0.5,
            page=page,
        ):
            raise XunfeiError("讯飞作品下载页未加载完成")

        _log(f"[xunfei] 下载页已打开: {page.url}")
        ready = self._wait_for_pending_ready(page, pending_items, timeout=180)
        records = self._fetch_works_list_in_page(
            page, needed_count=max(len(pending_items), 1)
        )
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
            works_id = str(item.get("works_id") or "")
            ready_item = ready.get(works_id) or {}
            record = ready_item.get("record") or record_by_id.get(works_id) or {}
            target = {
                "works_id": works_id,
                "order_no": str(record.get("orderNo") or ""),
                "works_name": str(record.get("worksName") or item.get("works_name") or ""),
                "row_index": record_indexes.get(works_id),
                "item": item,
            }
            if works_id not in ready:
                result = {
                    **item,
                    "downloaded": False,
                    "error": "作品未在下载页按 worksId 就绪",
                }
                results[works_id] = result
                _notify_batch_progress(progress_callback, {
                    "job_id": str(item.get("job_id") or ""),
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
            works_id = str(target.get("works_id") or "")
            item = target.get("item") or {}
            ready_item = ready.get(works_id) or {}
            if self._download_signed_url(
                ready_item.get("download_url"),
                item.get("output_path"),
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
                    "downloaded": True,
                    "stage": "downloaded",
                })
                _notify_batch_progress(progress_callback, {
                    "job_id": str(item.get("job_id") or ""),
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

        selected, missing = self._select_download_rows(page, browser_targets)
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
                "downloaded": False,
                "stage": "saved",
                "error": result["error"],
            })

        if selected_targets:
            downloads = self._download_selected_rows(
                page,
                selected_targets,
                progress_callback=progress_callback,
            )
            remaining_downloads = list(downloads)
            for target in selected_targets:
                item = target["item"]
                works_id = str(target.get("works_id") or "")
                download_index = self._match_download_index(
                    remaining_downloads,
                    target,
                )
                # 只有单条兜底下载时没有歧义可以直接使用；多条下载如果
                # 文件名不能证明归属，宁可失败也不把音频写错题目。
                if download_index is None and len(remaining_downloads) == 1:
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
                    "downloaded": bool(result.get("downloaded")),
                    "stage": "downloaded" if result.get("downloaded") else "saved",
                    "error": result.get("error"),
                })
                if result.get("downloaded"):
                    _notify_batch_progress(progress_callback, {
                        "job_id": str(item.get("job_id") or ""),
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

    def synth_batch(self, jobs, max_retries=4, progress_callback=None):
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
                job_id = str(job.get("job_id") or uuid.uuid4().hex)
                try:
                    pending_item = self._generate_pending_one(
                        job.get("text", ""),
                        output_name=job.get("output_name"),
                        works_name=job.get("works_name"),
                        max_retries=max_retries,
                        voice_key=job.get("voice_key"),
                        speed=job.get("speed", PARAM_DEFAULT),
                        pitch=job.get("pitch", PARAM_DEFAULT),
                        volume=job.get("volume", PARAM_DEFAULT),
                    )
                    pending_item["job_id"] = job_id
                    pending.append(pending_item)
                    # 统一下载模式下，提交每个音频段也是可见进度的一部分。
                    # 如果只等到下载页全部返回，长文档在生成阶段会一直显示 0%。
                    report_progress({
                        "job_id": job_id,
                        "downloaded": False,
                        "stage": "submitted",
                    })
                except (XunfeiQuotaExceeded, XunfeiLoginRequired):
                    raise
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

        if callable(progress_callback):
            downloaded = self._download_pending_batch(
                pending,
                progress_callback=report_progress,
            )
        else:
            downloaded = self._download_pending_batch(pending)
        for item in pending:
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
                    "error": "合成已提交但统一下载失败",
                }
                results[job_id] = result
            if callable(progress_callback) and (
                job_id,
                "saved",
            ) not in reported_progress:
                report_progress({
                    "job_id": job_id,
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
            try:
                if previous_id:
                    pending_item = {
                        "works_id": str(previous_id),
                        "output_path": os.path.join(OUTPUT_DIR, work["output_name"]),
                        "works_name": str(previous.get("works_name") or work["works_name"]),
                        "work_id": work_id,
                        "job_id": work_id,
                        "item_count": int(work.get("item_count") or 0),
                    }
                    _log(
                        f"[xunfei] ♻️ 复用多人配音作品 worksId={pending_item['works_id']} "
                        f"work={work_id}"
                    )
                else:
                    pending_item = self._generate_pending_composite(
                        work,
                        output_name=work.get("output_name"),
                        max_retries=max_retries,
                    )
                    pending_item["job_id"] = work_id
                    pending_item["work_id"] = work_id
                pending.append(pending_item)
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "works_id": pending_item.get("works_id"),
                    "stage": "submitted",
                    "downloaded": False,
                })
            except (XunfeiQuotaExceeded, XunfeiLoginRequired):
                raise
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

        downloaded = self._download_pending_batch(
            pending,
            progress_callback=report_progress if callable(progress_callback) else None,
        )
        for pending_item in pending:
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
                    "error": "多人配音作品已提交但统一下载失败",
                }
            if callable(progress_callback) and (work_id, "saved") not in reported_progress:
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "works_id": works_id,
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
        output_path = os.path.join(OUTPUT_DIR, output_name)

        pending = self._generate_pending_one(
            text,
            output_name=output_name,
            max_retries=max_retries,
            voice_key=voice_key,
            speed=speed,
            pitch=pitch,
            volume=volume,
        )
        result = self._download_pending_batch([pending]).get(str(pending["works_id"]))
        output_path = pending["output_path"]
        if result and result.get("downloaded") and os.path.exists(output_path):
            _log(f"[xunfei] ✅ 生成成功 ({os.path.getsize(output_path):,} bytes)")
            return output_path
        raise XunfeiError("合成已完成但未能下载音频")

    def close(self):
        """关闭浏览器，保留登录状态（持久化目录不被删除）。"""
        _log("[xunfei] 正在关闭浏览器...")
        try:
            if self._page and self._response_handler:
                try:
                    self._page.remove_listener("response", self._response_handler)
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
            self._current_voice_key = None
            self._current_voice_name = None
            self._applied_params = None
            self._response_handler = None
            with self._works_lock:
                self._works_entries = []
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
_playwright_executor = None
_playwright_executor_lock = threading.Lock()


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


async def ensure_session(voice_key="amanda"):
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
                _discard_session_unsafe()
            session = XunFeiSession(voice_key=voice_key)
            try:
                session.login(login_timeout=300)
            except Exception:
                try:
                    session.close()
                except Exception:
                    pass
                raise
            # 必须在线程内写入全局会话。否则两个 asyncio 任务同时首次
            # 调用 ensure_session 时，第二个任务可能在主事件循环中看见
            # 仍为空，再创建第二个 Playwright Sync 会话。
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


async def synth_xunfei_batch(jobs, progress_callback=None):
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
    session = await ensure_session(voice_key=first_voice)
    normalized_jobs = []
    for index, job in enumerate(jobs):
        item = dict(job)
        item.setdefault("job_id", f"batch-{index}")
        item.setdefault("output_name", f".xunfei_{uuid.uuid4().hex}.mp3")
        # 讯飞下载页文件名不带 worksId；为本批次每段设置短且唯一的作品名，
        # 既便于页面核对，也避免同名作品的浏览器下载事件无法区分。
        item.setdefault("works_name", f"wordtts_{index + 1:04d}_{uuid.uuid4().hex[:8]}")
        normalized_jobs.append(item)

    raw_results = await _run_playwright_sync(
        session.synth_batch,
        normalized_jobs,
        4,
        progress_callback=progress_callback,
    )
    decoded = {}
    from pydub import AudioSegment

    for job in normalized_jobs:
        job_id = str(job["job_id"])
        result = raw_results.get(job_id) if isinstance(raw_results, dict) else None
        if not isinstance(result, dict) or not result.get("downloaded"):
            decoded[job_id] = {
                "segment": None,
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


async def synth_xunfei_composite(works, progress_callback=None, resume=None):
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
    session = await ensure_session(voice_key=first_voice)
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

    raw_results = await _run_playwright_sync(
        session.synth_composite,
        normalized_works,
        4,
        progress_callback,
        resume,
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
    import asyncio

    with _session_lock:
        old = _session
        _session = None
    if old is not None:
        try:
            await _run_playwright_sync(old.close)
            _log("[xunfei] 浏览器会话已关闭")
        except Exception as e:
            _log(f"[xunfei] 关闭浏览器会话异常: {e}")


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
