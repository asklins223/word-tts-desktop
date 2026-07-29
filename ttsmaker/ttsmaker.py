#!/usr/bin/env python3
"""
TTSMaker TTS 生成工具 v10
后台系统 Chrome 建立真实浏览器会话，并在同一会话内调用 API。
验证码: #UserInputCaptcha, 4位数字, data-captcha-key

TTSMaker 的网页会把 JSON.stringify(...) 生成的原始 JSON 放进请求体，
但 Content-Type 仍使用 application/x-www-form-urlencoded。这里必须保持
这个看似不匹配的组合，不能把 payload 当作普通表单字段发送。
"""
import os
import sys
import io
import re
import time
import json
import base64
import pathlib
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from PIL import Image
from playwright.sync_api import sync_playwright

# Windows 信号兼容层
_signal = None
if sys.platform == 'win32':
    _signal = None  # Windows 不支持 SIGTERM/SIGKILL
else:
    import signal
    _signal = signal


def _log(*args, **kwargs):
    """所有日志输出到 stderr，确保 Electron 能捕获。"""
    kwargs.setdefault('file', sys.stderr)
    print(*args, **kwargs)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PyInstaller 打包环境下 _MEIPASS 是只读的，输出目录需要用可写位置
if getattr(sys, 'frozen', False):
    _OUTPUT_BASE = os.path.join(
        os.path.expanduser("~"), ".edge_tts_webui", "ttsmaker_output"
    )
else:
    _OUTPUT_BASE = os.path.join(BASE_DIR, "ttsmaker_output")
OUTPUT_DIR = _OUTPUT_BASE
os.makedirs(OUTPUT_DIR, exist_ok=True)

HOME_URL = "https://ttsmaker.cn/"

# 持久化浏览器配置目录（保存 cookies / 登录状态）
PROFILE_DIR = os.path.join(
    os.path.expanduser("~"), ".ttsmaker_chrome_profile"
)

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

VOICES = {
    "alfie":       ("788",  "en-us", "Alfie (US Male)"),
    "alayna":      ("1480", "en-us", "Alayna (US Female v2)"),
    "alayna_hot":  ("148",  "en-us", "Alayna Hot (US Female)"),
    "alayna_fast": ("14801","en-us", "Alayna Fast (US Female)"),
}


def build_order_body(
    uuid, text, lang_id, voice_id, captcha_text, captcha_key,
    speed="1.0", volume="1", pitch="1", pause_time="0",
):
    """按网页 JSON.stringify 的格式构造请求体。

    Args:
        speed: 语速倍率 ("1.0"=正常, "1.5"=快50%, "0.5"=慢50%)
        volume: 音量倍率 ("1"=正常, "1.5"=大50%, "0.5"=小50%)
        pitch: 音调倍率 ("1"=正常, "1.1"=高10%, "0.9"=低10%)
        pause_time: 段落间停顿时间 (秒, 字符串)
    """
    payload = {
        "user_uuid_text": uuid,
        "user_input_text": text,
        "user_select_language_id": lang_id,
        "user_select_announcer_id": voice_id,
        "user_select_tts_setting_audio_format": "mp3",
        "user_select_tts_setting_speed": str(speed),
        "user_select_tts_setting_volume": str(volume),
        "user_select_tts_setting_pitch": str(pitch),
        "user_input_captcha_text": captcha_text,
        "user_input_captcha_key": captcha_key,
        "user_input_paragraph_pause_time": str(pause_time),
        "user_select_tts_voice_high_quality": "0",
        "user_bgm_config": {},
        # 必须是 JSON 布尔值 false，不能是字符串 "false"。
        "accept_queue": False,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def create_isolated_context(browser):
    """为 Playwright 启动的 Chrome 创建临时、非持久化会话。"""
    chrome_major = browser.version.split(".", 1)[0]
    # 跨平台 User-Agent
    desktop_user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_major}.0.0.0 Safari/537.36"
    )
    context = browser.new_context(
        locale="zh-CN",
        viewport={"width": 1440, "height": 1000},
        user_agent=desktop_user_agent,
        extra_http_headers={
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    context.add_init_script(STEALTH_SCRIPT)
    return context


def start_chrome_hide_guard(process_id):
    """
    macOS 专用: 持续隐藏指定 PID 的临时 Chrome。
    Windows/Linux 跳过此功能。
    """
    if sys.platform != 'darwin':
        return None  # 非 macOS 跳过
    try:
        from AppKit import NSRunningApplication
    except ImportError:
        return None

    stop_event = threading.Event()
    started_event = threading.Event()
    state = {"found": False}

    def keep_hidden():
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(
            int(process_id)
        )
        state["found"] = app is not None
        started_event.set()
        if app is None:
            return
        while not stop_event.is_set():
            app.hide()
            stop_event.wait(0.025)

    thread = threading.Thread(
        target=keep_hidden,
        name="ttsmaker-chrome-hide",
        daemon=True,
    )
    thread.start()
    started_event.wait(2)
    if not state["found"]:
        stop_event.set()
        thread.join(timeout=1)
        return None
    return stop_event, thread

def stop_chrome_hide_guard(guard):
    if not guard:
        return
    stop_event, thread = guard
    stop_event.set()
    thread.join(timeout=2)


def chrome_process_is_hidden(process_id):
    if sys.platform != 'darwin':
        return True  # Windows/Linux 假设已隐藏
    try:
        from AppKit import NSRunningApplication
    except ImportError:
        return True
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(
        int(process_id)
    )
    return bool(app and app.isHidden() and not app.isActive())


def close_chrome(browser, background=False):
    """关闭浏览器；CDP 连接的隐藏实例需要显式发送 Browser.close。"""
    if background:
        try:
            session = browser.new_browser_cdp_session()
            try:
                session.send("Browser.close")
            except Exception:
                # Browser.close 会主动断开 CDP，断开异常等同于已关闭。
                pass
            return
        except Exception:
            pass
    try:
        browser.close()
    except Exception:
        pass


def cleanup_background_profile(profile_dir, process_id=None):
    """等待隐藏 Chrome 退出后删除临时 profile。"""
    if not profile_dir:
        return
    for _ in range(50):
        if process_id:
            # 检查进程是否存在
            if _signal is not None:
                try:
                    os.kill(process_id, 0)
                except (ProcessLookupError, PermissionError, OSError):
                    process_id = None
                else:
                    time.sleep(0.1)
                    continue
            else:
                # Windows: 尝试检查进程
                try:
                    import psutil
                    if not psutil.pid_exists(process_id):
                        process_id = None
                    else:
                        time.sleep(0.1)
                        continue
                except ImportError:
                    # Windows 上没有 psutil 时直接删除
                    process_id = None
        try:
            shutil.rmtree(profile_dir)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)
    if process_id and _signal is not None:
        try:
            os.kill(process_id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(0.5)
    shutil.rmtree(profile_dir, ignore_errors=True)


def launch_hidden_system_chrome(playwright, timeout=15):
    """
    macOS 专用: 通过 LaunchServices 在后台隐藏启动独立 Chrome。
    Windows/Linux 使用 Playwright 内置 Chromium，无需此功能。
    """
    if sys.platform != 'darwin':
        raise RuntimeError("launch_hidden_system_chrome 仅支持 macOS")
    profile_dir = tempfile.mkdtemp(prefix="ttsmaker-background-")
    active_port_file = pathlib.Path(profile_dir) / "DevToolsActivePort"
    singleton_lock = pathlib.Path(profile_dir) / "SingletonLock"
    process_id = None
    browser = None
    hide_guard = None
    command = [
        "open",
        "-gj",
        "-na",
        "Google Chrome",
        "--args",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-startup-window",
        "--disable-blink-features=AutomationControlled",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--lang=zh-CN",
        "about:blank",
    ]
    try:
        launched = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if launched.returncode != 0:
            raise RuntimeError(
                launched.stderr.strip()
                or f"open exit={launched.returncode}"
            )

        deadline = time.monotonic() + timeout
        while (
            not active_port_file.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        if not active_port_file.exists():
            raise RuntimeError("等待 Chrome 调试端口超时")

        port = active_port_file.read_text().splitlines()[0].strip()
        if not port.isdigit():
            raise RuntimeError("Chrome 调试端口无效")

        if singleton_lock.is_symlink():
            lock_target = os.readlink(singleton_lock)
            pid_match = re.search(r"-(\d+)$", lock_target)
            if pid_match:
                process_id = int(pid_match.group(1))
        if not process_id:
            raise RuntimeError("无法识别隐藏 Chrome 进程")
        hide_guard = start_chrome_hide_guard(process_id)
        if not hide_guard:
            raise RuntimeError("无法启动 Chrome 隐藏守护")

        browser = playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}"
        )
        if not browser.contexts:
            browser.close()
            raise RuntimeError("隐藏 Chrome 没有可用 context")

        context = browser.contexts[0]
        context.set_extra_http_headers({
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        context.add_init_script(STEALTH_SCRIPT)
        hidden_deadline = time.monotonic() + 2
        while (
            not chrome_process_is_hidden(process_id)
            and time.monotonic() < hidden_deadline
        ):
            time.sleep(0.025)
        if not chrome_process_is_hidden(process_id):
            browser.close()
            raise RuntimeError("连接后无法保持 Chrome 隐藏")
        return browser, context, profile_dir, process_id, hide_guard
    except Exception:
        if browser is not None:
            close_chrome(browser, background=bool(process_id))
        stop_chrome_hide_guard(hide_guard)
        cleanup_background_profile(profile_dir, process_id)
        raise


def post_order_in_browser(page, body, timeout_ms=300000):
    """
    在已通过 Cloudflare 的页面会话内发送原始 JSON。

    Cookie、Origin、Referer、User-Agent、Accept-Encoding 和 Sec-Fetch-*
    由真实 Chrome 自动生成；正文和可设置的 Ajax 请求头与成功 curl 一致。
    """
    result = page.evaluate(
        """async ({body, timeoutMs}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const response = await fetch('/api/create-tts-order', {
                    method: 'POST',
                    headers: {
                        'Content-Type':
                            'application/x-www-form-urlencoded; charset=UTF-8',
                        'Accept':
                            'application/json, text/javascript, */*; q=0.01',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    credentials: 'same-origin',
                    body,
                    signal: controller.signal
                });
                return {
                    http_status: response.status,
                    content_type: response.headers.get('content-type') || '',
                    body: await response.text(),
                    error: ''
                };
            } catch (error) {
                return {
                    http_status: 0,
                    content_type: '',
                    body: '',
                    error: String(error)
                };
            } finally {
                clearTimeout(timer);
            }
        }""",
        {"body": body, "timeoutMs": timeout_ms},
    )
    return result


def download_audio(url, output_path):
    """使用 urllib 下载音频（跨平台，替代 macOS curl 命令）。"""
    try:
        # 使用 urllib.request，无需外部 curl
        _log(f"   下载中: {url[:80]}...")
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=60) as response:
            with open(output_path, 'wb') as f:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
        size = os.path.getsize(output_path)
        if size == 0:
            _log("   下载失败: 文件为空")
            return False
        _log(f"   下载完成: {output_path} ({size:,} bytes)")
        return True
    except Exception as e:
        _log(f"   下载失败: {e}")
        return False


def solve_captcha(img_bytes):
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        text = ocr.classification(img_bytes)
        if text:
            d = re.sub(r'[^0-9]', '', text)
            if len(d) == 4: return d
            if len(d) > 4: return d[:4]
    except: pass
    try:
        import pytesseract
        img = Image.open(io.BytesIO(img_bytes)).convert("L")
        w, h = img.size
        img = img.resize((w*4, h*4)).point(lambda x: 0 if x<140 else 255)
        for cfg in ["--psm 7 -c tessedit_char_whitelist=0123456789",
                     "--psm 8 -c tessedit_char_whitelist=0123456789"]:
            t = pytesseract.image_to_string(img, config=cfg).strip()
            d = re.sub(r'[^0-9]', '', t)
            if len(d) == 4: return d
    except: pass
    return None


def generate(
    text,
    voice_key="alfie",
    output_name=None,
    max_retries=12,
    show_browser=False,
    speed="1.0",
    volume="1",
    pitch="1",
    pause_time="0",
):
    if voice_key not in VOICES:
        raise ValueError(
            f"未知音色 {voice_key!r}，可选: {', '.join(VOICES)}"
        )

    voice_id, lang_id, voice_name = VOICES[voice_key]
    if not output_name:
        output_name = f"ttsmaker_{voice_key}_{int(time.time())}.mp3"
    output_path = os.path.join(OUTPUT_DIR, output_name)

    _log(f"{'='*60}")
    _log(f"  音色: {voice_name} (ID: {voice_id})")
    _log(f"  文本: {text[:60]}...")
    _log(f"{'='*60}")

    # 调试时可临时显示浏览器：
    # TTSMAKER_SHOW_BROWSER=1 python3 ttsmaker.py "text" alayna_hot
    show_browser = show_browser or os.environ.get(
        "TTSMAKER_SHOW_BROWSER", ""
    ).lower() in {"1", "true", "yes"}

    with sync_playwright() as p:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--window-size=1440,1000",
        ]
        background_profile = None
        background_pid = None
        background_guard = None

        if show_browser:
            try:
                browser = p.chromium.launch(
                    channel="chrome",
                    headless=False,
                    args=launch_args,
                )
            except Exception as chrome_error:
                _log(
                    "   系统 Chrome 启动失败，改用 Playwright Chromium: "
                    f"{chrome_error}"
                )
                browser = p.chromium.launch(
                    headless=False,
                    args=launch_args,
                )
            ctx = create_isolated_context(browser)
            browser_mode = "可见调试"
        else:
            try:
                (
                    browser,
                    ctx,
                    background_profile,
                    background_pid,
                    background_guard,
                ) = launch_hidden_system_chrome(p)
                browser_mode = "后台隐藏窗口"
            except Exception as hidden_error:
                _log(
                    "   隐藏 Chrome 启动失败，改用纯无头模式: "
                    f"{hidden_error}"
                )
                # Windows/Linux: 直接使用 Playwright Chromium（非 headless=True 时用内置 Chromium）
                try:
                    browser = p.chromium.launch(
                        headless=True,
                        args=launch_args,
                    )
                except Exception:
                    browser = p.chromium.launch(
                        headless=True,
                        args=launch_args,
                    )
                ctx = create_isolated_context(browser)
                browser_mode = "纯无头模式"

        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            api_requests = []

            def on_request(request):
                if "create-tts-order" in request.url:
                    api_requests.append({
                        "method": request.method,
                        "url": request.url,
                        "body": request.post_data or "",
                        "headers": dict(request.headers),
                    })

            page.on("request", on_request)

            def restore_form():
                selected = page.evaluate(
                    """voiceId => {
                        const radio = document.querySelector(
                            `#RadioUserSelectAnnouncerID${voiceId}`
                        );
                        if (!radio) return false;
                        radio.click();
                        return true;
                    }""",
                    voice_id,
                )
                if not selected:
                    raise RuntimeError(f"页面中找不到音色 ID {voice_id}")
                page.locator("#UserInputTextarea").fill(text)
                page.wait_for_timeout(300)

            def reload_form():
                try:
                    page.reload(
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    page.wait_for_selector(
                        "#UserInputTextarea",
                        timeout=60000,
                    )
                    restore_form()
                    return True
                except Exception as reload_error:
                    _log(f"   页面刷新失败: {reload_error}")
                    return False

            _log(f"\n1. 加载 TTSMaker (系统 Chrome，{browser_mode})...")
            try:
                page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as goto_error:
                _log(f"   首次加载提示: {goto_error}")

            try:
                page.wait_for_selector("#UserInputTextarea", timeout=60000)
            except Exception as e:
                _log(f"   未进入 TTSMaker 表单，当前标题: {page.title()}")
                raise RuntimeError(f"TTSMaker 页面未加载完成（表单未出现）: {e}")

            _log(f"   标题: {page.title()}")
            restore_form()
            _log("   已选音色 + 输入文本")

            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            cookie_uuid = cookies.get("uuid", "")
            _log(
                "   UUID: "
                f"{cookie_uuid[:8] + '...' if cookie_uuid else '无'}"
            )

            for attempt in range(1, max_retries + 1):
                _log(f"\n2. 第 {attempt}/{max_retries} 次...")

                cap = page.evaluate(
                    """() => {
                        const img = document.querySelector(
                            '#VerifyCaptchaIMG'
                        );
                        const input = document.querySelector(
                            '#UserInputCaptcha'
                        );
                        if (!img) return null;
                        return {
                            uuid: img.getAttribute('uuid') || '',
                            src: img.src || '',
                            key:
                                input?.getAttribute('data-captcha-key')
                                || img.getAttribute('data-captcha-key')
                                || ''
                        };
                    }"""
                )
                if (
                    not cap
                    or not cap["uuid"]
                    or not cap["key"]
                    or not cap["src"]
                ):
                    _log("   验证码参数不完整，刷新页面...")
                    if not reload_form():
                        break
                    continue

                try:
                    b64 = page.evaluate(
                        """async url => {
                            const response = await fetch(
                                url,
                                {cache: 'no-store'}
                            );
                            if (!response.ok) {
                                throw new Error(
                                    `captcha HTTP ${response.status}`
                                );
                            }
                            const blob = await response.blob();
                            return await new Promise((resolve, reject) => {
                                const reader = new FileReader();
                                reader.onload = () =>
                                    resolve(reader.result);
                                reader.onerror = reject;
                                reader.readAsDataURL(blob);
                            });
                        }""",
                        cap["src"],
                    )
                    img_bytes = base64.b64decode(b64.split(",", 1)[1])
                except Exception as captcha_error:
                    _log(f"   验证码下载失败: {captcha_error}")
                    page.evaluate(
                        'document.querySelector("#reVerify")?.click()'
                    )
                    page.wait_for_timeout(2000)
                    continue

                captcha_text = solve_captcha(img_bytes)
                if not captcha_text:
                    _log("   OCR 失败，刷新验证码...")
                    page.evaluate(
                        'document.querySelector("#reVerify")?.click()'
                    )
                    page.wait_for_timeout(2000)
                    continue

                _log(
                    f"   OCR: '{captcha_text}', "
                    f"key: '{cap['key']}', "
                    f"uuid: '{cap['uuid'][:8]}...'"
                )
                page.locator("#UserInputCaptcha").fill(captcha_text)

                body = build_order_body(
                    cap["uuid"],
                    text,
                    lang_id,
                    voice_id,
                    captcha_text,
                    cap["key"],
                    speed=speed,
                    volume=volume,
                    pitch=pitch,
                    pause_time=pause_time,
                )
                api_requests.clear()
                _log(f"   请求体: {len(body.encode('utf-8'))} bytes")
                _log("   在系统 Chrome 会话内调用 API...")
                result = post_order_in_browser(page, body)
                http_status = result.get("http_status", 0)
                response_text = result.get("body", "")
                _log(
                    f"   响应: {len(response_text)} bytes, "
                    f"HTTP={http_status}"
                )

                if api_requests:
                    sent_body = api_requests[-1]["body"]
                    if sent_body == body:
                        _log("   已验证: 浏览器实际发送正文与构造正文一致")
                    else:
                        _log("   警告: 浏览器实际发送正文发生变化")

                if result.get("error"):
                    _log(f"   浏览器请求失败: {result['error']}")

                try:
                    data_resp = json.loads(
                        response_text.lstrip("\ufeff \t\r\n")
                    )
                except (json.JSONDecodeError, TypeError):
                    data_resp = None

                if isinstance(data_resp, dict):
                    status = data_resp.get("status", 0)
                    info = str(data_resp.get("info", ""))
                    _log(f"   status={status}, info={info}")

                    url = data_resp.get("auto_stand_url", "")
                    if url:
                        _log(f"\n🎉 生成成功! URL: {url}")
                        if download_audio(url, output_path):
                            return output_path
                        break

                    if str(status) == "429":
                        _log("   频率限制，等 65 秒...")
                        time.sleep(65)
                    elif str(status) == "444":
                        _log(f"   免费高速额度/队列限制: {info}")
                        break
                    elif (
                        "captcha" in info.lower()
                        or "验证" in info
                        or "verification" in info.lower()
                    ):
                        _log("   验证码错误，重试...")
                    else:
                        _log(f"   其他: {info or data_resp}")
                        break
                elif http_status == 403 or "<html" in response_text.lower():
                    _log("   Cloudflare 拦截当前会话，刷新后重试...")
                    if not reload_form():
                        break
                    continue
                else:
                    _log(
                        "   未知响应: "
                        f"{response_text[:200] or result.get('error', '')}"
                    )

                page.evaluate(
                    'document.querySelector("#reVerify")?.click()'
                )
                page.wait_for_timeout(2000)
        finally:
            try:
                close_chrome(
                    browser,
                    background=bool(background_pid),
                )
            finally:
                stop_chrome_hide_guard(background_guard)
                cleanup_background_profile(
                    background_profile,
                    background_pid,
                )

    _log("\n❌ 失败")
    raise RuntimeError(f"TTSMaker 生成失败：已重试 {max_retries} 次仍未成功（可能是频率限制、验证码识别失败或网络问题）")


# ============================================================================
# 持久化会话类 — 浏览器保持开启，连续生成多条音频
# ============================================================================
class TTSMakerSession:
    """
    管理 Chrome 浏览器持久会话：登录 → 连续生成 → 关闭。

    基于 ttsmaker_batch_788.py 参考脚本，使用 UI 交互方式（点击按钮 + 等待下载）
    而非直接调用 API，确保与 ttsmaker.cn 网站兼容。

    用法:
        session = TTSMakerSession()
        session.login()                          # 打开浏览器，首次需扫码，后续自动复用登录
        path = session.synth_one("text 1")       # 生成第 1 条
        path = session.synth_one("text 2")       # 生成第 2 条（浏览器不关闭）
        session.close()                          # 全部完成后关闭（登录状态保留）
    """

    def __init__(self, voice_key="alfie"):
        if voice_key not in VOICES:
            raise ValueError(
                f"未知音色 {voice_key!r}，可选: {', '.join(VOICES)}"
            )
        self.voice_key = voice_key
        self.voice_id, self.lang_id, self.voice_name = VOICES[voice_key]
        self._playwright = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._logged_in = False
        self._is_persistent = False
        # 非持久化模式下的后台 Chrome 资源（login 失败时 close() 需要安全清理）
        self._background_profile = None
        self._background_pid = None
        self._background_guard = None

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _select_language(self, page):
        """选择英语语言，触发 updateAnnouncersList 动态生成 voice radio"""
        try:
            current_lang = page.evaluate(
                "() => document.querySelector('#userSelectLanguageID')?.value"
            )
            if current_lang == self.lang_id:
                return True
            page.evaluate(
                """(langId) => {
                    const sel = document.querySelector('#userSelectLanguageID');
                    if (!sel) return;
                    sel.value = langId;
                    const event = new Event('change', {bubbles: true});
                    sel.dispatchEvent(event);
                    if (typeof $ !== 'undefined') {
                        $('#userSelectLanguageID').trigger('change');
                    }
                }""",
                self.lang_id,
            )
            page.wait_for_timeout(1500)
            _log(f"   已选择语言: {self.lang_id}")
            return True
        except Exception as e:
            _log(f"   选择语言失败: {e}")
            return False

    def _set_tts_settings(self, page, speed, volume, pitch, pause_time):
        """
        在 TTSMaker.cn 页面上设置语速、音量、音调、停顿时间。

        这些设置位于「高级设置」折叠面板中，但即使面板折叠，
        <select> 元素仍在 DOM 中，可以通过 JS 设置值。

        TTSMaker 页面 select 元素 ID:
          - #userSelectTTSSettingSpeed     语速 (值: "0.5"~"1.5")
          - #userSelectTTSSettingVolume    音量 (值: "0.1"~"2.0", 注意 "1" 不是 "1.0")
          - #userSelectTTSSettingPitch     音调 (值: "0.5"~"2.0", 注意 "1" 不是 "1.0")
          - #userSelectTTSParagraphPauseTime  停顿 (值: -1, 0, 50, 100, 200, 500...)
        """
        def set_select_value(select_id, target_value):
            """设置 select 的值，支持精确匹配和数值模糊匹配。"""
            result = page.evaluate(
                """([selectId, targetVal]) => {
                    const sel = document.querySelector('#' + selectId);
                    if (!sel) return { ok: false, reason: 'element not found' };

                    // 1. 精确匹配
                    for (const opt of sel.options) {
                        if (opt.value === targetVal) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            if (typeof $ !== 'undefined') $('#' + selectId).trigger('change');
                            return { ok: true, matched: opt.value };
                        }
                    }

                    // 2. 数值匹配 (处理 "1.0" → "1" 等情况)
                    const targetNum = parseFloat(targetVal);
                    if (!isNaN(targetNum)) {
                        for (const opt of sel.options) {
                            if (parseFloat(opt.value) === targetNum) {
                                sel.value = opt.value;
                                sel.dispatchEvent(new Event('change', {bubbles: true}));
                                if (typeof $ !== 'undefined') $('#' + selectId).trigger('change');
                                return { ok: true, matched: opt.value };
                            }
                        }
                        // 3. 找最接近的选项
                        let bestOpt = null;
                        let bestDiff = Infinity;
                        for (const opt of sel.options) {
                            const optNum = parseFloat(opt.value);
                            if (!isNaN(optNum)) {
                                const diff = Math.abs(optNum - targetNum);
                                if (diff < bestDiff) {
                                    bestDiff = diff;
                                    bestOpt = opt;
                                }
                            }
                        }
                        if (bestOpt) {
                            sel.value = bestOpt.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            if (typeof $ !== 'undefined') $('#' + selectId).trigger('change');
                            return { ok: true, matched: bestOpt.value, approximated: true };
                        }
                    }

                    return { ok: false, reason: 'no matching option for ' + targetVal };
                }""",
                [select_id, str(target_value)],
            )
            if result and result.get("ok"):
                matched = result.get("matched", "?")
                approx = " (近似)" if result.get("approximated") else ""
                _log(f"   设置 {select_id}: {target_value} → {matched}{approx}")
            else:
                _log(f"   ⚠️ 设置 {select_id} 失败: {result}")
            return result.get("ok", False) if result else False

        # 依次设置四个参数
        set_select_value("userSelectTTSSettingSpeed", speed)
        set_select_value("userSelectTTSSettingVolume", volume)
        set_select_value("userSelectTTSSettingPitch", pitch)
        set_select_value("userSelectTTSParagraphPauseTime", pause_time)
        page.wait_for_timeout(300)

    def _select_voice(self, page):
        """选择音色 radio button — 需先选语言"""
        self._select_language(page)
        radio_selector = f"#RadioUserSelectAnnouncerID{self.voice_id}"
        radio = page.locator(radio_selector)
        for _ in range(15):
            if radio.count() > 0:
                break
            page.wait_for_timeout(1000)
            radio = page.locator(radio_selector)
        if radio.count() == 0:
            _log("   voice radio 未出现，重新触发语言切换...")
            self._select_language(page)
            page.wait_for_timeout(2000)
            radio = page.locator(radio_selector)
            for _ in range(10):
                if radio.count() > 0:
                    break
                page.wait_for_timeout(1000)
                radio = page.locator(radio_selector)
        if radio.count() == 0:
            raise RuntimeError(f"未找到音色选择器 {radio_selector}")
        page.evaluate(
            f"""() => {{
                const el = document.querySelector('{radio_selector}');
                if (el) {{ el.scrollIntoView({{block: 'center'}}); el.click(); }}
            }}"""
        )
        page.wait_for_timeout(500)
        _log(f"   已选音色: {self.voice_name} (ID: {self.voice_id})")

    def _clear_previous_audio(self, page):
        """清除上一次 TTS 生成的音频结果，防止下载到旧音频"""
        try:
            page.evaluate(
                """() => {
                    const source = document.querySelector('#tts_mp3_download_player_source');
                    if (source) source.removeAttribute('src');
                    const audio = document.querySelector('#tts_mp3_download_player_audio');
                    if (audio) { audio.removeAttribute('src'); audio.pause(); audio.load(); }
                    const dlDiv = document.querySelector('#tts_mp3_download_player_div');
                    if (dlDiv) dlDiv.style.display = 'none';
                    const resultDiv = document.querySelector('#tts_result_div');
                    if (resultDiv) resultDiv.innerHTML = '';
                }"""
            )
            page.wait_for_timeout(200)
        except Exception as e:
            _log(f"   清除旧音频结果失败 (非致命): {e}")

    def _wait_and_download(self, page, output_path, timeout=120):
        """
        等待音频生成完成并下载。
        ttsmaker.cn 流程: 点击转换 → 等待 #tts_mp3_download_player_source 出现 URL → 下载
        """
        audio_url = None
        deadline = time.time() + timeout

        while time.time() < deadline:
            page.wait_for_timeout(1000)
            try:
                src = page.evaluate(
                    """() => {
                        const source = document.querySelector('#tts_mp3_download_player_source');
                        if (source) return source.getAttribute('src') || '';
                        const audio = document.querySelector('#tts_mp3_download_player_audio');
                        if (audio) return audio.getAttribute('src') || '';
                        return '';
                    }"""
                )
                if src and src != "#!" and any(
                    ext in src.lower() for ext in [".wav", ".mp3", ".aac", ".opus"]
                ):
                    audio_url = src
                    _log(f"   发现音频 URL: {src[:80]}...")
                    break
            except Exception:
                pass

        if not audio_url:
            return False

        # 尝试点击下载按钮
        try:
            dl_btn = page.locator("#tts_mp3_download_btn")
            if dl_btn.count() > 0:
                _log("   点击下载按钮...")
                with page.expect_download(timeout=15000) as dl_info:
                    dl_btn.first.click()
                download = dl_info.value
                download.save_as(output_path)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    _log(f"   下载完成 (按钮): {os.path.getsize(output_path):,} bytes")
                    return True
        except Exception as e:
            _log(f"   下载按钮失败: {e}")

        # 用 curl 下载
        return download_audio(audio_url, output_path)

    def _reload_page(self, page):
        """刷新页面并等待表单加载"""
        try:
            page.reload(wait_until="load", timeout=60000)
            page.wait_for_timeout(3000)
            page.wait_for_selector("#UserInputTextarea", timeout=60000)
            self._select_language(page)
            return True
        except Exception as e:
            _log(f"   页面刷新失败: {e}")
            try:
                page.goto(HOME_URL, wait_until="load", timeout=60000)
                page.wait_for_timeout(5000)
                page.wait_for_selector("#UserInputTextarea", timeout=60000)
                self._select_language(page)
                return True
            except Exception as e2:
                _log(f"   重新导航也失败: {e2}")
                return False

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def login(self, login_timeout=300):
        """
        打开可见的 Chrome 浏览器，导航到 TTSMaker.cn。

        使用持久化用户数据目录（~/.ttsmaker_chrome_profile），
        首次需要扫码登录，后续自动复用已保存的登录状态。

        Args:
            login_timeout: 等待手动登录的超时时间（秒），默认 300 秒。
                           已有有效登录状态时不会等待。
        """
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1440,1000",
            "--lang=zh-CN",
            "--mute-audio",
        ]

        chrome_major = None
        try:
            temp_browser = self._playwright.chromium.launch(
                channel="chrome", headless=True, args=["--no-sandbox"],
            )
            chrome_major = temp_browser.version.split(".", 1)[0]
            temp_browser.close()
        except Exception:
            pass

        # 跨平台 User-Agent: Windows/macOS/Linux 模拟 Chrome
        desktop_user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major or '131'}.0.0.0 Safari/537.36"
        )

        os.makedirs(PROFILE_DIR, exist_ok=True)
        _log(f"[ttsmaker] 浏览器配置目录: {PROFILE_DIR}")

        # 使用持久化上下文 — cookies / localStorage 自动保存
        try:
            self._ctx = self._playwright.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                channel="chrome",
                headless=False,
                args=launch_args,
                locale="zh-CN",
                viewport={"width": 1440, "height": 1000},
                user_agent=desktop_user_agent,
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            self._is_persistent = True
        except Exception as chrome_error:
            _log(
                "   系统 Chrome 启动失败，改用 Playwright Chromium: "
                f"{chrome_error}"
            )
            self._ctx = self._playwright.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=False,
                args=launch_args,
                locale="zh-CN",
                viewport={"width": 1440, "height": 1000},
                user_agent=desktop_user_agent,
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            self._is_persistent = True

        self._ctx.add_init_script(STEALTH_SCRIPT)
        # 禁止音频自动播放
        self._ctx.add_init_script("""
            if (window.Audio) {
                Audio.prototype.play = function() { return Promise.resolve(); };
            }
            if (window.HTMLMediaElement) {
                HTMLMediaElement.prototype.play = function() { return Promise.resolve(); };
            }
        """)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

        _log("[ttsmaker] 正在打开 TTSMaker.cn...")

        try:
            self._page.goto(
                HOME_URL,
                wait_until="load",
                timeout=60000,
            )
        except Exception as goto_error:
            _log(f"   首次加载提示（500 状态码正常）: {goto_error}")

        # 等待页面 JS 执行完毕
        for _ in range(30):
            self._page.wait_for_timeout(1000)
            try:
                if self._page.evaluate("() => document.readyState") == "complete":
                    break
            except Exception:
                continue

        # 确认表单存在
        try:
            self._page.wait_for_selector("#UserInputTextarea", timeout=30000)
        except Exception:
            _log("   页面表单未找到，重试加载...")
            self._page.wait_for_timeout(3000)
            try:
                self._page.goto(HOME_URL, wait_until="load", timeout=60000)
            except Exception:
                pass
            self._page.wait_for_timeout(5000)
            try:
                self._page.wait_for_selector("#UserInputTextarea", timeout=30000)
            except Exception:
                raise RuntimeError("无法加载 TTSMaker 表单")

        # 检测是否已登录（登录按钮消失 = 已登录）
        already_logged_in = self._is_logged_in(self._page)

        if already_logged_in:
            _log("[ttsmaker] 检测到已保存的登录状态，无需重新扫码")
        else:
            _log("[ttsmaker] 登录状态无效，请在浏览器中扫码登录...")
            _log(f"[ttsmaker] 等待用户登录（超时 {login_timeout} 秒）...")
            deadline = time.time() + login_timeout
            while time.time() < deadline:
                self._page.wait_for_timeout(1000)
                if self._is_logged_in(self._page):
                    # 确认表单可用
                    if self._page.locator("#UserInputTextarea").count() > 0:
                        break
            else:
                raise RuntimeError(
                    f"等待登录超时（{login_timeout}秒），TTSMaker 登录未完成"
                )
            _log("[ttsmaker] 登录成功！")

        # 预选英语语言
        self._select_language(self._page)
        self._logged_in = True

    def _is_logged_in(self, page):
        """检测是否已登录：登录按钮消失 = 已登录"""
        try:
            login_btn = page.locator(
                "[data-bs-target='#wx_login_modal_staticBackdrop']"
            )
            if login_btn.count() == 0:
                return True
            # 检查是否有可见的登录按钮
            visible_count = 0
            for i in range(login_btn.count()):
                try:
                    if login_btn.nth(i).is_visible():
                        visible_count += 1
                except Exception:
                    pass
            if visible_count == 0:
                return True
        except Exception:
            pass
        return False

    def synth_one(
        self,
        text,
        output_name=None,
        max_retries=8,
        speed="1.0",
        volume="1",
        pitch="1",
        pause_time="0",
    ):
        """
        在已登录的浏览器会话中生成一条音频（UI 交互方式）。
        生成完成后浏览器保持开启，等待下一条。

        Returns: 生成的音频文件路径
        Raises: RuntimeError 如果生成失败
        """
        if not self._logged_in:
            raise RuntimeError("尚未登录，请先调用 login()")

        if not output_name:
            output_name = f"ttsmaker_{self.voice_key}_{int(time.time())}.mp3"
        output_path = os.path.join(OUTPUT_DIR, output_name)

        _log(f"{'='*60}")
        _log(f"  音色: {self.voice_name} (ID: {self.voice_id})")
        _log(f"  文本: {text[:60]}...")
        _log(f"  参数: speed={speed} volume={volume} pitch={pitch} pause={pause_time}")
        _log(f"{'='*60}")

        page = self._page

        for attempt in range(1, max_retries + 1):
            _log(f"   第 {attempt}/{max_retries} 次尝试...")

            try:
                # 1. 选音色（含选语言）
                self._select_voice(page)

                # 2. 输入文本
                textarea = page.locator("#UserInputTextarea")
                if textarea.count() == 0:
                    textarea = page.locator("textarea").first
                textarea.fill("")
                page.wait_for_timeout(200)
                textarea.fill(text)
                page.wait_for_timeout(300)

                # 3. 设置语速/音量/音调/停顿
                self._set_tts_settings(page, speed, volume, pitch, pause_time)

                # 4. 清除上一次的音频结果
                self._clear_previous_audio(page)

                # 5. 验证码处理
                #    登录后 JS 可能自动填入 8888，所以验证码可能不需要手动处理
                cap = page.evaluate(
                    """() => {
                        const img = document.querySelector('#VerifyCaptchaIMG');
                        const input = document.querySelector('#UserInputCaptcha');
                        if (!img) return null;
                        return {
                            uuid: img.getAttribute('uuid') || '',
                            src: img.src || '',
                            key: input?.getAttribute('data-captcha-key')
                                || img.getAttribute('data-captcha-key')
                                || img.getAttribute('data-key')
                                || ''
                        };
                    }"""
                )
                if cap and cap.get("uuid") and cap.get("src"):
                    # 有验证码 — 下载并 OCR
                    try:
                        b64 = page.evaluate(
                            """async url => {
                                const response = await fetch(url, {cache: 'no-store'});
                                if (!response.ok) throw new Error('captcha HTTP ' + response.status);
                                const blob = await response.blob();
                                return await new Promise((resolve, reject) => {
                                    const reader = new FileReader();
                                    reader.onload = () => resolve(reader.result);
                                    reader.onerror = reject;
                                    reader.readAsDataURL(blob);
                                });
                            }""",
                            cap["src"],
                        )
                        img_bytes = base64.b64decode(b64.split(",", 1)[1])
                        captcha_text = solve_captcha(img_bytes)
                        if captcha_text:
                            _log(f"   OCR: '{captcha_text}'")
                            page.locator("#UserInputCaptcha").fill(captcha_text)
                            page.wait_for_timeout(200)
                        else:
                            _log("   OCR 失败，尝试直接提交")
                    except Exception as cap_err:
                        _log(f"   验证码处理失败: {cap_err}")
                else:
                    _log("   无验证码或已自动填入")

                # 6. 点击转换按钮
                btn = page.locator("#tts_order_submit")
                if btn.count() > 0:
                    btn.first.click()
                    _log("   已点击转换按钮")
                else:
                    # 备用: 找包含"开始转换"的按钮
                    buttons = page.locator("button, a.btn, input[type='submit']")
                    clicked = False
                    for i in range(min(buttons.count(), 30)):
                        try:
                            txt = buttons.nth(i).inner_text().strip()
                            if any(kw in txt for kw in ["开始转换", "转换", "convert"]):
                                buttons.nth(i).click()
                                clicked = True
                                _log(f"   已点击按钮: {txt}")
                                break
                        except Exception:
                            pass
                    if not clicked:
                        _log("   未找到转换按钮")
                        if not self._reload_page(page):
                            break
                        continue

                # 7. 等待并下载
                found = self._wait_and_download(page, output_path, timeout=120)
                if found and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    size = os.path.getsize(output_path)
                    _log(f"\n🎉 生成成功! ({size:,} bytes)")
                    return output_path

                # 8. 检查频率限制
                try:
                    page_text = page.inner_text("body").lower()
                    if any(kw in page_text for kw in ["429", "rate limit", "频率", "限制", "稍后", "too many", "频繁", "排队"]):
                        _log("   遇到频率限制，等 65 秒...")
                        time.sleep(65)
                        if not self._reload_page(page):
                            break
                        continue
                    if any(kw in page_text for kw in ["captcha", "验证", "verification"]):
                        _log("   验证码错误，重试...")
                        page.evaluate('document.querySelector("#reVerify")?.click()')
                        page.wait_for_timeout(2000)
                        continue
                except Exception:
                    pass

                _log("   未获取到音频，刷新重试...")
                if not self._reload_page(page):
                    break
                page.evaluate('document.querySelector("#reVerify")?.click()')
                page.wait_for_timeout(2000)

            except Exception as attempt_err:
                _log(f"   第 {attempt} 次异常: {attempt_err}")
                if not self._reload_page(page):
                    break

        _log("\n❌ 失败")
        raise RuntimeError(
            f"TTSMaker 生成失败：已重试 {max_retries} 次仍未成功"
            f"（可能是频率限制、验证码识别失败或网络问题）"
        )

    def close(self):
        """关闭浏览器，保留登录状态（持久化目录不被删除）。"""
        _log("[ttsmaker] 正在关闭浏览器...")
        try:
            if self._is_persistent and self._ctx:
                self._ctx.close()
            elif self._browser:
                close_chrome(
                    self._browser,
                    background=bool(self._background_pid),
                )
        except Exception as e:
            _log(f"[ttsmaker] 关闭浏览器异常: {e}")
        finally:
            if not self._is_persistent:
                stop_chrome_hide_guard(self._background_guard)
                cleanup_background_profile(
                    self._background_profile,
                    self._background_pid,
                )
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._browser = None
            self._ctx = None
            self._page = None
            self._playwright = None
            self._background_profile = None
            self._background_pid = None
            self._background_guard = None
            self._logged_in = False
            _log("[ttsmaker] 浏览器已关闭（登录状态已保留）")


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "TTS Maker is a text-to-speech tool, a professional AI voice generator dedicated to delivering high-quality voice synthesis services."
    voice = sys.argv[2] if len(sys.argv) > 2 else "alfie"
    r = generate(text, voice)
    if r: _log(f"\n✅ 文件: {r}")
