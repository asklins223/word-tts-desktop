#!/usr/bin/env python3
"""
讯飞配音自动化客户端（wordtts 统一 TTS 引擎）
====================
通过 Playwright 有头 Chrome 自动化操作讯飞配音网站 (peiyin.xunfei.cn/make)，
实现文本到语音的合成与下载。

设计:
  - 使用持久化浏览器配置目录，首次需手动登录，后续自动复用登录状态
  - 单条合成：输入文本 → 选发音人 → 设置语速/语调/音量 → 生成音频 → 确认合成 → 拦截 worksId → 签名 URL 下载
  - 页面复用：下载通过页面内 API 完成，不离开编辑页，发音人与参数跨条目保持
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
import threading
import urllib.request

from playwright.sync_api import sync_playwright


def _log(*args, **kwargs):
    """所有日志输出到 stdout/stderr，确保 Electron / Gradio 能捕获。"""
    kwargs.setdefault('file', sys.stdout)
    print(*args, **kwargs)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PyInstaller 打包环境下 _MEIPASS 是只读的，输出目录需要用可写位置
if getattr(sys, 'frozen', False):
    _OUTPUT_BASE = os.path.join(
        os.path.expanduser("~"), ".wordtts", "xunfei_output"
    )
else:
    _OUTPUT_BASE = os.path.join(BASE_DIR, "xunfei_output")
OUTPUT_DIR = _OUTPUT_BASE
os.makedirs(OUTPUT_DIR, exist_ok=True)

HOME_URL = "https://peiyin.xunfei.cn/make"
API_SIGN_URL = "https://peiyin.xunfei.cn/video-api/synth/get_work_sign_url"

# 持久化浏览器配置目录（保存 cookies / 登录状态）
PROFILE_DIR = os.path.join(
    os.path.expanduser("~"), ".xunfei_chrome_profile"
)

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
    },
    # 男声
    "george": {
        "name": "George",
        "display": "George (英语男声)",
        "gender": "male",
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
        gender = str(voice.get("gender") or "unknown").strip().lower()
        gender_label = "女声" if gender == "female" else ("男声" if gender == "male" else "音色")
        VOICES[key] = {
            "name": name,
            "display": f"{name} ({gender_label})",
            "gender": gender,
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
        const modals = document.querySelectorAll('.ant-modal');
        for (const modal of modals) {
            const style = window.getComputedStyle(modal);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (modal.getBoundingClientRect().width === 0) continue;
            const text = modal.textContent || '';
            if (keywords.every(kw => text.includes(kw))) return true;
        }
        return false;
    }
    """

    CLICK_BTN_IN_MODAL = """
    (buttonText) => {
        const modals = document.querySelectorAll('.ant-modal');
        for (const modal of modals) {
            const style = window.getComputedStyle(modal);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (modal.getBoundingClientRect().width === 0) continue;
            const btns = modal.querySelectorAll('button');
            for (const b of btns) {
                if (b.textContent?.trim() === buttonText && b.offsetParent !== null) {
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

    GET_EDITOR_TEXT = """
    () => {
        const editor = document.querySelector('.ssml-editor');
        return editor?.textContent?.trim() || '';
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
        const btns = document.querySelectorAll('button');
        for (const b of btns) {
            if (b.className?.includes('active') || b.className?.includes('selected')) {
                const t = b.textContent?.trim() || '';
                if (t.includes(name)) return t;
            }
        }
        return null;
    }
    """

    SEARCH_AND_CLICK_VOICE = """
    (name) => {
        const btns = document.querySelectorAll('button');
        for (const b of btns) {
            const t = b.textContent?.trim() || '';
            if (t.includes(name) && t.length < 100 && b.offsetParent !== null) {
                b.click(); return true;
            }
        }
        return false;
    }
    """

    CHECK_SEARCH_RESULT = """
    (name) => {
        const btns = document.querySelectorAll('button');
        for (const b of btns) {
            const t = b.textContent?.trim() || '';
            if (t.includes(name) && t.length < 100 && b.offsetParent !== null) return true;
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
        const text = body.textContent || '';
        return text.includes('余额不足') || text.includes('次数不足') || text.includes('额度不足');
    }
    """

    CHECK_RATE_LIMITED = """
    () => {
        const body = document.body;
        if (!body) return false;
        const text = body.textContent || '';
        return text.includes('操作频繁') || text.includes('稍后再试') || text.includes('请求过于频繁');
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
        const modals = document.querySelectorAll('.ant-modal');
        for (const modal of modals) {
            const style = window.getComputedStyle(modal);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (modal.getBoundingClientRect().width === 0) continue;
            const text = modal.textContent || '';
            if (!text.includes('不再提示')) continue;
            const wrappers = modal.querySelectorAll('.ant-checkbox-wrapper');
            for (const w of wrappers) {
                const input = w.querySelector('.ant-checkbox-input');
                if (input && !input.checked) { w.click(); return 'clicked'; }
                if (input && input.checked) return 'already';
            }
        }
        return 'not_found';
    }
    """

    CLICK_AI_SWITCH = """
    () => {
        const modals = document.querySelectorAll('.ant-modal');
        for (const modal of modals) {
            const style = window.getComputedStyle(modal);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (modal.getBoundingClientRect().width === 0) continue;
            const text = modal.textContent || '';
            if (!text.includes('作品设置') && !text.includes('确认合成')) continue;
            if (text.includes('不再提示')) continue;
            const handle = modal.querySelector('.ant-switch-handle');
            if (handle) { handle.click(); return 'handle'; }
            const sw = modal.querySelector('button.ant-switch, .ant-switch');
            if (sw) { sw.click(); return 'switch'; }
        }
        return 'not_found';
    }
    """

    CLICK_AI_CONFIRM = """
    () => {
        const modals = document.querySelectorAll('.ant-modal');
        for (const modal of modals) {
            const style = window.getComputedStyle(modal);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (modal.getBoundingClientRect().width === 0) continue;
            const text = modal.textContent || '';
            if (!text.includes('不再提示')) continue;
            const btns = modal.querySelectorAll('button');
            for (const b of btns) {
                if (b.textContent?.trim() === '确认') { b.click(); return true; }
            }
        }
        return false;
    }
    """

    FETCH_SIGN_URL = """
    (worksId) => {
        return fetch('%s', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                param: {worksId: worksId, worksType: 1},
                base: {appid: "xfpy", sid: "", channelId: "40000001", osid: 0}
            })
        }).then(r => r.json()).then(d => {
            if (d.code === 0 && d.data && d.data.url) return d.data.url;
            return null;
        }).catch(() => null);
    }
    """ % API_SIGN_URL


# AI 标识弹窗关键词变体（文案可能变化，逐个尝试）
AI_FLAG_KEYWORD_VARIANTS = [
    ["AI", "标识", "不再提示"],
    ["AI", "标识", "说明"],
    ["标识", "不再提示"],
    ["AI", "不再提示"],
    ["AI", "标识"],
]


def _poll(check_fn, timeout, interval=0.5, page=None):
    """轮询等待 check_fn 返回 truthy；间隔带 ±25% 抖动。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = check_fn()
            if result:
                return result
        except Exception:
            pass
        sleep_s = min(interval * (0.75 + (time.time() % 0.5)), max(0.05, deadline - time.time()))
        if page is not None:
            page.wait_for_timeout(int(sleep_s * 1000))
        else:
            time.sleep(sleep_s)
    return None


def _safe_eval(page, script, arg=None):
    try:
        if arg is not None:
            return page.evaluate(script, arg)
        return page.evaluate(script)
    except Exception:
        return None


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
        # 页面状态跟踪（页面复用的关键）
        self._current_voice_name = None
        self._applied_params = None  # dict(speed=, pitch=, volume=) 或 None
        # worksId 捕获（时间戳截止线防跨条错配）
        self._works_lock = threading.Lock()
        self._works_entries = []     # [(works_id, ts)]
        self._works_cutoff = 0.0
        # sign_url 兜底通道捕获
        self._sign_urls = []
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
        在已聚焦的编辑器中拟人输入：
        短文本全量逐字符击键（间隔抖动、标点停顿）；
        长文本开头手打一小段、其余 insertText（浏览器受信输入事件）。
        """
        if len(text) > 80:
            head_len = 4 + int((time.time() * 13) % 8)
            head, tail = text[:head_len], text[head_len:]
        else:
            head, tail = text, ""

        for ch in head:
            page.keyboard.type(ch)
            delay = 0.03 + ((time.time() * 11) % 1) * 0.06
            if ch in ",.!?;:\n。，！？；：、":
                delay += 0.08 + ((time.time() * 17) % 1) * 0.2
            page.wait_for_timeout(int(delay * 1000))

        if tail:
            try:
                page.keyboard.insertText(tail)
                page.wait_for_timeout(300)
            except Exception:
                for ch in tail:
                    page.keyboard.type(ch)
                    page.wait_for_timeout(60)

    # ------------------------------------------------------------------
    # worksId 捕获
    # ------------------------------------------------------------------

    def _on_response(self, response):
        url = response.url
        try:
            wid = None
            if "makeMultipleSpeakerWork" in url:
                data = response.json()
                if data.get("retCode") == "000000" and data.get("tempWorksId"):
                    wid = data["tempWorksId"]
            elif "order_gen" in url:
                data = response.json()
                if data.get("code") == 0:
                    wid = data.get("data", {}).get("payOrder", {}).get("worksId")
            elif "get_work_sign_url" in url:
                data = response.json()
                if data.get("code") == 0 and data.get("data", {}).get("url"):
                    self._sign_urls.append(data["data"]["url"])
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

    def _consume_works_id(self, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._works_lock:
                fresh = [e for e in self._works_entries if e[1] >= self._works_cutoff - 0.5]
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
        page.wait_for_timeout(350)

        for attempt in range(2):
            actual = _safe_eval(page, JS.GET_EDITOR_TEXT) or ""
            if len(actual) >= len(text) * 0.85:
                return True
            _log(f"[xunfei]   输入验证失败 (attempt {attempt + 1})，重试...")
            self._clear_editor(page)
            page.locator(".ssml-editor").first.click(timeout=5000)
            self._type_text(page, text)
            page.wait_for_timeout(350)
        return False

    def _select_voice(self, page, voice_name):
        """在发音人列表中搜索并选择指定发音人，选中后回读验证。"""
        if self._current_voice_name == voice_name:
            return True

        def mark_selected():
            # 讯飞页面切换音色后会把三项调节恢复为页面默认值；即使新旧
            # 音色的目标数值恰好相同，也必须让 _apply_params() 重新下发。
            voice_changed = self._current_voice_name != voice_name
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

    # ------------------------------------------------------------------
    # 确认合成弹窗流程
    # ------------------------------------------------------------------

    def _observe_after_first_confirm(self, page):
        """第一次点击确认合成后的状态探测。"""

        def probe():
            if _safe_eval(page, JS.CHECK_INSUFFICIENT):
                return "insufficient"
            if _safe_eval(page, JS.CHECK_RATE_LIMITED):
                return "rate_limited"
            if _safe_eval(page, JS.CHECK_GO_DOWNLOAD) or _safe_eval(page, JS.CHECK_FREE_MODAL):
                return "order"
            for kws in AI_FLAG_KEYWORD_VARIANTS:
                if _safe_eval(page, JS.CHECK_MODAL_HAS_TEXT, kws):
                    return "ai_modal"
            return None

        return _poll(probe, timeout=7, interval=0.4, page=page) or "none"

    def _handle_ai_flag_dialog(self, page):
        _safe_eval(page, JS.CHECK_NO_REMIND)
        self._pause(page, 0.35, 0.15)
        _safe_eval(page, JS.CLICK_AI_SWITCH)
        self._pause(page, 0.35, 0.15)
        _safe_eval(page, JS.CLICK_AI_CONFIRM)
        self._pause(page, 0.5, 0.2)

    def _wait_order_or_error(self, page, timeout):
        def probe():
            if _safe_eval(page, JS.CHECK_INSUFFICIENT):
                return "insufficient"
            if _safe_eval(page, JS.CHECK_RATE_LIMITED):
                return "rate_limited"
            if _safe_eval(page, JS.CHECK_LOGIN_MODAL):
                return "login"
            if _safe_eval(page, JS.CHECK_GO_DOWNLOAD) or _safe_eval(page, JS.CHECK_FREE_MODAL):
                return "ok"
            return None

        return _poll(probe, timeout=timeout, interval=0.8, page=page)

    def _confirm_synth(self, page):
        """
        处理确认合成弹窗完整流程。

        返回: 'ok' | 'insufficient' | 'rate_limited' | 'login' | 'failed'
        """
        appeared = _poll(
            lambda: _safe_eval(page, JS.CHECK_MODAL_HAS_TEXT, ["确认合成"]),
            timeout=10, interval=0.6, page=page,
        )
        if not appeared:
            # 无弹窗也可能直接开始合成；若出现订单/错误则按其处理
            return self._wait_order_or_error(page, 4) or "failed"

        self._pause(page, 0.6, 0.3)

        # 第一次点击"确认合成"
        clicked = bool(_safe_eval(page, JS.CLICK_BTN_IN_MODAL, "确认合成"))
        if not clicked:
            try:
                page.locator('button:text-is("确认合成")').first.click(timeout=3000)
                clicked = True
            except Exception:
                pass
        _log(f"[xunfei]   第一次确认合成: {'✓' if clicked else '✗'}")
        if not clicked:
            return "failed"

        outcome = self._observe_after_first_confirm(page)
        if outcome == "ai_modal":
            _log("[xunfei]   检测到 AI 标识说明弹窗")
            self._handle_ai_flag_dialog(page)
        elif outcome in ("order", "insufficient", "rate_limited"):
            return "ok" if outcome == "order" else outcome

        # 弹窗仍在 → 需要第二次确认
        still_there = _safe_eval(page, JS.CHECK_MODAL_HAS_TEXT, ["确认合成"])
        if not still_there:
            return self._wait_order_or_error(page, 8) or "ok"

        clicked2 = bool(_poll(
            lambda: _safe_eval(page, JS.CLICK_BTN_IN_MODAL, "确认合成"),
            timeout=6, interval=0.4, page=page,
        ))
        _log(f"[xunfei]   第二次确认合成: {'✓' if clicked2 else '✗'}")
        return self._wait_order_or_error(page, 90) or ("ok" if clicked2 else "failed")

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def _download_file(self, url, output_path, attempts=2):
        """使用 urllib 下载文件，带 MP3 魔数校验。"""
        ua = self._real_ua or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": ua})
                with urllib.request.urlopen(req, timeout=60) as response:
                    with open(output_path, 'wb') as f:
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                size = os.path.getsize(output_path)
                if size == 0:
                    _log("[xunfei]   下载失败: 文件为空")
                elif not _looks_like_mp3(output_path):
                    _log("[xunfei]   下载内容不是有效 MP3，丢弃重试")
                    try:
                        os.remove(output_path)
                    except OSError:
                        pass
                else:
                    return True
            except Exception as e:
                _log(f"[xunfei]   下载异常 (attempt {attempt}): {e}")
            page = self._page
            if page:
                page.wait_for_timeout(1500)
        return False

    def _fetch_sign_url_in_page(self, page, works_id):
        """直接在页面上下文里调用签名接口（不离开编辑页）。"""
        return _safe_eval(page, JS.FETCH_SIGN_URL, works_id)

    def _download_via_intercept(self, page, output_path):
        """兜底：点击'去下载'跳转下载页，拦截 get_work_sign_url 响应获取音频 URL。"""
        if not _safe_eval(page, JS.CLICK_GO_DOWNLOAD):
            return False
        deadline = time.time() + 20
        sign_url = None
        while time.time() < deadline:
            if self._sign_urls:
                sign_url = self._sign_urls[-1]
                break
            page.wait_for_timeout(500)
        if not sign_url:
            return False
        if not self._download_file(sign_url, output_path):
            return False
        # 该路径离开了编辑页，重置页面状态
        self._recover_and_retry(page)
        return True

    def _cleanup_after_item(self, page):
        """单条完成后关闭残留弹窗，回到可继续输入的状态。"""
        _safe_eval(page, JS.CLOSE_ALL_MODALS, [])
        self._pause(page, 0.3, 0.15)

    def _recover_and_retry(self, page):
        """合成失败后恢复页面状态（重新加载编辑页，重置音色/参数记忆）。"""
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(".ssml-editor", timeout=30000)
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
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1440,1000",
            "--lang=zh-CN",
            "--mute-audio",
        ]

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

        # 等待页面 JS 执行完毕
        for _ in range(30):
            self._page.wait_for_timeout(1000)
            try:
                if self._page.evaluate("() => document.readyState") == "complete":
                    break
            except Exception:
                continue

        # 确认编辑器存在
        try:
            self._page.wait_for_selector(".ssml-editor", timeout=30000)
        except Exception:
            _log("[xunfei] 页面编辑器未找到，重试加载...")
            self._page.wait_for_timeout(3000)
            try:
                self._page.goto(HOME_URL, wait_until="load", timeout=60000)
            except Exception:
                pass
            self._page.wait_for_timeout(5000)
            try:
                self._page.wait_for_selector(".ssml-editor", timeout=30000)
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
    # 单条合成
    # ------------------------------------------------------------------

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

        page = self._page
        last_error = None

        for attempt in range(1, max_retries + 1):
            _log(f"[xunfei]   第 {attempt}/{max_retries} 次尝试...")

            try:
                # 确保编辑页就绪（上一条可能停留在未知状态）
                if page.locator(".ssml-editor").count() == 0:
                    if not self._recover_and_retry(page):
                        raise XunfeiError("页面恢复失败")

                # 选发音人 + 应用参数
                self._select_voice(page, voice_name)
                self._apply_params(page, speed, pitch, volume)

                # 输入文本
                if not self._input_text(page, text):
                    raise XunfeiError("文本输入失败")

                # 开始计时捕获（防止上一条的迟到响应错配到本条）
                self._mark_works_cutoff()

                # 点击生成 + 处理确认弹窗
                self._click_generate(page)
                status = self._confirm_synth(page)

                if status == "insufficient":
                    raise XunfeiQuotaExceeded("讯飞配音额度不足")
                if status == "login":
                    raise XunfeiLoginRequired("合成过程中弹出登录框，请重新登录")
                if status == "rate_limited":
                    raise XunfeiRateLimited("触发讯飞频控")
                if status != "ok":
                    raise XunfeiError("确认合成弹窗流程未完成")

                # 拿到 worksId 后直接在页面内请求签名 URL 下载（不离开编辑页）
                works_id = self._consume_works_id(timeout=12)
                downloaded = False
                if works_id:
                    sign_url = self._fetch_sign_url_in_page(page, works_id)
                    if sign_url:
                        downloaded = self._download_file(sign_url, output_path)
                if not downloaded:
                    _log("[xunfei]   页面内下载未成功，走'去下载'拦截兜底...")
                    downloaded = self._download_via_intercept(page, output_path)

                if downloaded and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    self._cleanup_after_item(page)
                    self._pause(page, 1.2, 0.6)  # 条目间随机间隙
                    size = os.path.getsize(output_path)
                    _log(f"[xunfei] ✅ 生成成功 ({size:,} bytes)")
                    return output_path

                raise XunfeiError("合成已完成但未能下载音频")

            except (XunfeiQuotaExceeded, XunfeiLoginRequired):
                # 额度/登录问题向上传播，由调用方决定整体策略
                raise
            except XunfeiRateLimited as e:
                last_error = e
                cooldown = 18 + (time.time() % 10) * 2
                _log(f"[xunfei]   ⏳ 频控冷却 {cooldown:.0f}s 后重试")
                page.wait_for_timeout(int(cooldown * 1000))
                self._recover_and_retry(page)
            except Exception as attempt_err:
                last_error = attempt_err
                _log(f"[xunfei]   第 {attempt} 次异常: {attempt_err}")
                if not self._recover_and_retry(page):
                    break

        _log("[xunfei] ❌ 生成失败")
        raise XunfeiError(f"讯飞配音生成失败：{last_error or '已重试仍未成功'}")

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
            self._current_voice_name = None
            self._applied_params = None
            self._response_handler = None
            with self._works_lock:
                self._works_entries = []
                self._sign_urls = []
            _log("[xunfei] 浏览器已关闭（登录状态已保留）")


# ============================================================================
# 模块级异步接口（供 word_tts_app.py 调用）
# ============================================================================

_session = None
_session_lock = threading.Lock()


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

    if _session is not None:
        if _session_is_healthy(_session):
            return _session
        _log("[xunfei] 检测到已有会话已失效，将丢弃并重新创建")
        _discard_session_unsafe()

    import asyncio

    def _locked_create():
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
            return session

    _session = await asyncio.to_thread(_locked_create)
    return _session


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

    result_path = await asyncio.to_thread(
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


async def close_session():
    """关闭讯飞配音浏览器会话。"""
    global _session
    import asyncio

    old = _session
    _session = None
    if old is not None:
        try:
            await asyncio.to_thread(old.close)
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
