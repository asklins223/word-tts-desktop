#!/usr/bin/env python3
"""
TTSMaker.cn 批量 TTS 生成脚本

功能：
  - 用 Playwright 操控真实 Chrome 浏览器访问 https://ttsmaker.cn
  - 等待用户微信扫码登录
  - 批量读取 788_corpus.tsv，逐条生成 TTS 音频
  - 选择 Alfie (ID 788) 音色，WAV 格式
  - 文件命名为 <id>.wav，存放到 datasets/788/inbox/
  - 支持断点续跑、验证码自动识别、频率限制处理

用法：
  python3 ttsmaker_batch_788.py                    # 从头跑全部 tr_+va_
  python3 ttsmaker_batch_788.py --resume            # 跳过已完成的条目
  python3 ttsmaker_batch_788.py --start tr_0100      # 从指定 ID 开始
  python3 ttsmaker_batch_788.py --only te_          # 只跑指定前缀
  python3 ttsmaker_batch_788.py --max-retries 5     # 每条最多重试次数
  python3 ttsmaker_batch_788.py --delay 3            # 每条之间等待秒数
"""

import os
import sys
import csv
import json
import time
import base64
import io
import re
import shutil
import hashlib
import argparse
import subprocess
from pathlib import Path

# ─── 路径配置 ───────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TSV_PATH     = PROJECT_ROOT / "voice_training" / "datasets" / "788" / "prompts" / "788_corpus.tsv"
INBOX_DIR    = PROJECT_ROOT / "voice_training" / "datasets" / "788" / "inbox"
PROGRESS_FILE= PROJECT_ROOT / "voice_training" / "datasets" / "788" / "runs" / "ttsmaker_progress.json"
LOG_FILE     = PROJECT_ROOT / "voice_training" / "datasets" / "788" / "runs" / "ttsmaker_batch.log"

INBOX_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ─── 常量 ──────────────────────────────────────────────────
TTSMAKER_URL   = "https://ttsmaker.cn/"
VOICE_ID       = "788"          # Alfie
VOICE_RADIO_ID = f"#RadioUserSelectAnnouncerID{VOICE_ID}"
WAV_RADIO_ID   = "#RadioUserSelectAudioFormatWAV"
AUDIO_FORMAT   = "wav"          # README 要求优先 WAV

# 全局已下载文件 SHA-256 集合，防止不同 ID 拿到相同音频
_DOWNLOADED_HASHES: dict[str, str] = {}

# ─── 反检测脚本 ─────────────────────────────────────────────
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
if (!window.chrome) { Object.defineProperty(window, 'chrome', { value: { runtime: {} } }); }
"""

# ═══════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════

def log(msg, level="INFO"):
    """同时输出到终端和日志文件"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_tsv():
    """读取 TSV，返回 [(id, split, category, text), ...]"""
    rows = []
    with open(TSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for r in reader:
            rows.append((
                r["id"].strip(),
                r["split"].strip(),
                r["category"].strip(),
                r["text"].strip(),
            ))
    return rows


def filter_rows(rows, prefixes=None, start_id=None):
    """按前缀和起始 ID 过滤"""
    result = []
    for rid, split, cat, text in rows:
        if prefixes and not any(rid.startswith(p) for p in prefixes):
            continue
        if start_id and rid < start_id:
            continue
        result.append((rid, split, cat, text))
    return result


def load_progress():
    """加载进度文件"""
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"completed": {}, "failed": {}, "skipped": {}}


def save_progress(progress):
    """保存进度文件"""
    PROGRESS_FILE.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_completed(progress, rid):
    """检查某条是否已完成"""
    return rid in progress["completed"]


def mark_completed(progress, rid, filepath):
    """标记完成"""
    progress["completed"][rid] = {
        "file": str(filepath),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_progress(progress)


def mark_failed(progress, rid, reason):
    """标记失败"""
    progress["failed"][rid] = {
        "reason": reason,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_progress(progress)


def solve_captcha(img_bytes):
    """用 ddddocr / pytesseract 识别 4 位数字验证码"""
    # 方案 1: ddddocr
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        text = ocr.classification(img_bytes)
        if text:
            d = re.sub(r"[^0-9]", "", text)
            if len(d) == 4:
                return d
            if len(d) > 4:
                return d[:4]
            if len(d) > 0:
                # 补零或重试
                pass
    except Exception:
        pass

    # 方案 2: pytesseract
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(io.BytesIO(img_bytes)).convert("L")
        w, h = img.size
        img = img.resize((w * 4, h * 4)).point(lambda x: 0 if x < 140 else 255)
        for cfg in [
            "--psm 7 -c tessedit_char_whitelist=0123456789",
            "--psm 8 -c tessedit_char_whitelist=0123456789",
            "--psm 10 -c tessedit_char_whitelist=0123456789",
        ]:
            t = pytesseract.image_to_string(img, config=cfg).strip()
            d = re.sub(r"[^0-9]", "", t)
            if len(d) == 4:
                return d
    except Exception:
        pass

    return None


# ═══════════════════════════════════════════════════════════
#  浏览器自动化
# ═══════════════════════════════════════════════════════════

def create_browser(playwright, headless=False):
    """启动可见 Chrome 浏览器"""
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,1000",
        "--lang=zh-CN",
        "--mute-audio",            # 静音所有标签页
        "--disable-features=MediaRouter",  # 禁用媒体路由
    ]
    try:
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=headless,
            args=launch_args,
        )
        log("使用系统 Google Chrome 启动")
    except Exception as e:
        log(f"系统 Chrome 启动失败 ({e})，改用 Playwright Chromium")
        browser = playwright.chromium.launch(
            headless=headless,
            args=launch_args,
        )

    chrome_ver = browser.version
    ua = (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_ver.split('.')[0]}.0.0.0 Safari/537.36"
    )
    context = browser.new_context(
        locale="zh-CN",
        viewport={"width": 1440, "height": 1000},
        user_agent=ua,
        accept_downloads=True,
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    context.add_init_script(STEALTH_SCRIPT)
    # 在页面级别拦截音频自动播放
    context.add_init_script("""
        // 禁止音频自动播放
        if (window.Audio) {
            const _play = Audio.prototype.play;
            Audio.prototype.play = function() { return Promise.resolve(); };
        }
        // 拦截 HTMLMediaElement.play
        if (window.HTMLMediaElement) {
            HTMLMediaElement.prototype.play = function() { return Promise.resolve(); };
        }
    """)
    return browser, context


def safe_page_title(page):
    """安全获取页面标题，重试 3 次"""
    for _ in range(3):
        try:
            return page.title()
        except Exception:
            page.wait_for_timeout(1000)
    return "(unknown)"


def safe_page_url(page):
    """安全获取当前 URL，重试 3 次"""
    for _ in range(3):
        try:
            return page.url
        except Exception:
            page.wait_for_timeout(1000)
    return "(unknown)"


def wait_for_page_stable(page, max_wait=30):
    """等待页面停止导航，稳定可操作"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            # 尝试执行简单 JS，如果上下文还活着说明页面已稳定
            page.evaluate("() => document.readyState")
            return True
        except Exception:
            page.wait_for_timeout(1000)
    log("页面在导航中，继续尝试...", "WARN")
    return False


def is_logged_in(page):
    """检测是否已登录"""
    try:
        # ttsmaker.cn 登录后页面不会出现 '登录|注册' 按钮
        login_btn = page.locator("[data-bs-target='#wx_login_modal_staticBackdrop']")
        if login_btn.count() == 0:
            return True  # 登录按钮消失 = 已登录

        # 检查页面上是否有 '请先登录' 提示
        try:
            body_text = page.inner_text("body")
            if "请先登录" in body_text:
                return False
        except Exception:
            pass

        # 登录按钮存在但可能不可见
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


def wait_for_login(page, timeout=600):
    """
    等待用户微信扫码登录。
    ttsmaker.cn 的页面初始就有 TTS 表单，但需要登录才能转换。
    检测策略：登录按钮消失 或 出现 '转换成功' 相关元素。
    timeout: 最长等待秒数（默认 10 分钟）
    """
    log("=" * 60)
    log("请在浏览器中完成微信扫码登录")
    log(f"脚本将等待最多 {timeout} 秒，登录成功后自动继续")
    log("=" * 60)

    deadline = time.time() + timeout
    last_hint = 0

    while time.time() < deadline:
        try:
            # 先等页面稳定
            wait_for_page_stable(page, max_wait=5)

            # 检查是否已登录
            if is_logged_in(page):
                log("✅ 检测到已登录！")
                # 确保表单可用
                textarea = page.locator("#UserInputTextarea")
                if textarea.count() > 0:
                    log("✅ TTS 表单已就绪")
                    return True
                # 如果表单不在，可能需要刷新
                page.wait_for_timeout(2000)
                if page.locator("#UserInputTextarea").count() > 0:
                    log("✅ TTS 表单已就绪（刷新后）")
                    return True

        except Exception:
            pass

        # 每 30 秒提示一次
        elapsed = int(time.time() - (deadline - timeout))
        if elapsed - last_hint >= 30:
            remaining = timeout - elapsed
            log(f"⏳ 等待扫码登录中... 剩余 {remaining} 秒")
            last_hint = elapsed

        page.wait_for_timeout(1000)

    log("❌ 等待登录超时！", "ERROR")
    return False


def select_english_language(page):
    """选择英语语言，触发 updateAnnouncersList 动态生成 voice radio"""
    try:
        # 检查当前语言是否已是 en-us
        current_lang = page.evaluate(
            "() => document.querySelector('#userSelectLanguageID')?.value"
        )
        if current_lang == "en-us":
            return True

        # 用 jQuery 改 select 值并触发 change
        # bootstrap-select 用 selectpicker，需要同时更新 UI
        page.evaluate(
            """() => {
                const sel = document.querySelector('#userSelectLanguageID');
                sel.value = 'en-us';
                // 触发原生 change 事件
                const event = new Event('change', {bubbles: true});
                sel.dispatchEvent(event);
                // 也触发 jQuery change
                if (typeof $ !== 'undefined') {
                    $('#userSelectLanguageID').trigger('change');
                }
            }"""
        )
        page.wait_for_timeout(1500)  # 等动态渲染
        log("已选择语言: English (en-us)")
        return True
    except Exception as e:
        log(f"选择语言失败: {e}", "ERROR")
        return False


def select_voice(page):
    """选择 Alfie (788) 音色 — 需先选英语语言"""
    try:
        # 先确保选了英语语言
        select_english_language(page)

        # 等待 radio button 动态出现（最多 15 秒）
        radio = page.locator(VOICE_RADIO_ID)
        for wait in range(15):
            if radio.count() > 0:
                break
            page.wait_for_timeout(1000)
            radio = page.locator(VOICE_RADIO_ID)

        if radio.count() == 0:
            # 再试一次触发语言切换
            log("voice radio 未出现，重新触发语言切换...", "WARN")
            select_english_language(page)
            page.wait_for_timeout(2000)
            radio = page.locator(VOICE_RADIO_ID)
            for wait in range(10):
                if radio.count() > 0:
                    break
                page.wait_for_timeout(1000)
                radio = page.locator(VOICE_RADIO_ID)

        if radio.count() == 0:
            log(f"未找到音色选择器 {VOICE_RADIO_ID}", "ERROR")
            return False

        # 滚动到可见并点击
        page.evaluate(
            f"""() => {{
                const el = document.querySelector('{VOICE_RADIO_ID}');
                if (el) {{
                    el.scrollIntoView({{block: 'center'}});
                    el.click();
                }}
            }}"""
        )
        page.wait_for_timeout(500)
        log(f"已选择音色 Alfie (ID: {VOICE_ID})")
        return True
    except Exception as e:
        log(f"选择音色失败: {e}", "ERROR")
        return False


def input_text(page, text):
    """输入文本"""
    try:
        textarea = page.locator("#UserInputTextarea")
        if textarea.count() == 0:
            textarea = page.locator("textarea").first
        textarea.fill("")
        page.wait_for_timeout(200)
        textarea.fill(text)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        log(f"输入文本失败: {e}", "ERROR")
        return False


def select_audio_format(page):
    """选择 WAV 格式 — ttsmaker.cn 用 radio button"""
    try:
        # ttsmaker.cn 页面上有 #RadioUserSelectAudioFormatWAV
        wav_radio = page.locator(WAV_RADIO_ID)
        if wav_radio.count() > 0:
            page.evaluate(
                f"""() => {{
                    const el = document.querySelector('{WAV_RADIO_ID}');
                    if (el) el.click();
                }}"""
            )
            page.wait_for_timeout(300)
            log("已选择音频格式: WAV")
            return True

        # 备用: 通过 label 点击
        wav_label = page.locator("label:has-text('WAV'), label:has-text('wav')")
        if wav_label.count() > 0:
            wav_label.first.click()
            page.wait_for_timeout(300)
            log("已选择音频格式: WAV (label)")
            return True

        # 备用: 设置 JS 变量
        page.evaluate(
            """() => {
                try {
                    if (typeof user_select_tts_setting_audio_format !== 'undefined') {
                        user_select_tts_setting_audio_format = 'wav';
                    }
                } catch(e) {}
            }"""
        )
        log("未找到 WAV radio，尝试设置 JS 变量", "WARN")
        return True
    except Exception as e:
        log(f"选择音频格式失败: {e}", "WARN")
        return False


def get_captcha_info(page):
    """获取验证码图片的 uuid、key 和 src"""
    try:
        cap = page.evaluate(
            """() => {
                const img = document.querySelector('#VerifyCaptchaIMG');
                const input = document.querySelector('#UserInputCaptcha');
                if (!img) return null;
                return {
                    uuid: img.getAttribute('uuid') || '',
                    src: img.src || '',
                    key:
                        input?.getAttribute('data-captcha-key')
                        || img.getAttribute('data-captcha-key')
                        || img.getAttribute('data-key')
                        || ''
                };
            }"""
        )
        return cap
    except Exception:
        return None


def fetch_captcha_image(page, src):
    """在浏览器内 fetch 验证码图片，返回 bytes"""
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
            src,
        )
        return base64.b64decode(b64.split(",", 1)[1])
    except Exception as e:
        log(f"下载验证码图片失败: {e}", "ERROR")
        return None


def refresh_captcha(page):
    """刷新验证码"""
    try:
        page.evaluate('document.querySelector("#reVerify")?.click()')
        page.wait_for_timeout(2000)
    except Exception:
        pass


def fill_captcha(page, captcha_text):
    """填入验证码"""
    try:
        page.locator("#UserInputCaptcha").fill(captcha_text)
        page.wait_for_timeout(200)
        return True
    except Exception as e:
        log(f"填入验证码失败: {e}", "ERROR")
        return False


def clear_previous_audio(page):
    """清除上一次 TTS 生成的音频结果，防止 wait_and_download 拿到旧 URL。

    ttsmaker.cn 在转换完成后会把音频 URL 写入 #tts_mp3_download_player_source
    和 #tts_mp3_download_player_audio 的 src 属性。如果不清除，下一次调用
    wait_and_download 会立即发现旧 URL 并下载旧音频，导致不同文本拿到
    相同音频。
    """
    try:
        page.evaluate(
            """() => {
                // 清除 audio source
                const source = document.querySelector('#tts_mp3_download_player_source');
                if (source) source.removeAttribute('src');
                const audio = document.querySelector('#tts_mp3_download_player_audio');
                if (audio) {
                    audio.removeAttribute('src');
                    audio.pause();
                    audio.load();
                }
                // 隐藏下载区域
                const dlDiv = document.querySelector('#tts_mp3_download_player_div');
                if (dlDiv) dlDiv.style.display = 'none';
                // 清除可能的 result 文本
                const resultDiv = document.querySelector('#tts_result_div');
                if (resultDiv) resultDiv.innerHTML = '';
            }"""
        )
        page.wait_for_timeout(200)
        log("已清除上一次音频结果")
    except Exception as e:
        log(f"清除旧音频结果失败 (非致命): {e}", "WARN")


def file_sha256(path: Path) -> str:
    """计算文件 SHA-256"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def click_convert_button(page):
    """点击转换按钮 — ttsmaker.cn 用 #tts_order_submit"""
    try:
        # ttsmaker.cn 的提交按钮 ID 是 tts_order_submit
        btn = page.locator("#tts_order_submit")
        if btn.count() > 0:
            txt = btn.first.inner_text().strip()
            log(f"点击转换按钮: {txt}")
            btn.first.click()
            return True

        # 备用: 找包含 “开始转换” 的按钮
        buttons = page.locator("button, a.btn, input[type='submit']")
        for i in range(min(buttons.count(), 30)):
            txt = buttons.nth(i).inner_text().strip()
            if any(kw in txt for kw in ["开始转换", "转换", "submit", "convert"]):
                log(f"点击按钮: {txt}")
                buttons.nth(i).click()
                return True

        log("未找到转换按钮", "ERROR")
        return False
    except Exception as e:
        log(f"点击转换按钮失败: {e}", "ERROR")
        return False


def wait_and_download(page, context, output_path, timeout=120):
    """
    等待音频生成完成并下载。
    ttsmaker.cn 的流程：
    1. 点击“开始转换”后，页面显示进度条
    2. 转换完成后出现 #tts_mp3_download_player_div
    3. #tts_mp3_download_player_source 的 src 属性含音频 URL
    4. 点击 #tts_mp3_download_btn 下载
    """
    audio_url = None
    download_event = {"received": None}

    def on_download(download):
        download_event["received"] = download
        log(f"收到下载事件: {download.suggested_filename}")

    page.on("download", on_download)

    # 监听网络响应中的音频文件
    def on_response(response):
        nonlocal audio_url
        url = response.url
        if any(ext in url.lower() for ext in [".wav", ".mp3", ".aac"]):
            if "ttsmaker" in url or "tts-file" in url:
                if not audio_url:
                    audio_url = url
                    log(f"[网络] 发现音频: {url}")

    page.on("response", on_response)

    deadline = time.time() + timeout
    found = False

    while time.time() < deadline:
        page.wait_for_timeout(1000)

        # 检查 1: 下载按钮出现 + audio source 有 URL
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
                log(f"[页面] 发现音频 URL: {src}")
                break
        except Exception:
            pass

        # 检查 2: 下载事件
        if download_event["received"]:
            try:
                download = download_event["received"]
                download.save_as(str(output_path))
                size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                if size > 0:
                    log(f"下载完成 (事件): {output_path} ({size:,} bytes)")
                    found = True
                    break
            except Exception as e:
                log(f"保存下载文件失败: {e}", "WARN")

        # 检查 3: 页面文本中的音频 URL
        try:
            page_text = page.inner_text("body")
            urls = re.findall(
                r'https?://[^\s"\'<>]+\.(?:wav|mp3|aac|opus)', page_text, re.I
            )
            if urls:
                audio_url = urls[0]
                break
        except Exception:
            pass

    # 如果找到了音频 URL，下载它
    if audio_url and not found:
        # 先尝试点击下载按钮
        try:
            dl_btn = page.locator("#tts_mp3_download_btn")
            if dl_btn.count() > 0:
                log("点击下载按钮...")
                with page.expect_download(timeout=15000) as dl_info:
                    dl_btn.first.click()
                download = dl_info.value
                download.save_as(str(output_path))
                size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                if size > 0:
                    log(f"下载完成 (按钮): {output_path} ({size:,} bytes)")
                    found = True
        except Exception as e:
            log(f"下载按钮点击失败: {e}", "WARN")

        if not found:
            # 用 curl 下载
            try:
                result = subprocess.run(
                    ["curl", "-fL", "-sS", "--max-time", "60",
                     "-o", str(output_path), audio_url],
                    timeout=65, check=False,
                )
                if result.returncode == 0 and os.path.exists(output_path):
                    size = os.path.getsize(output_path)
                    if size > 0:
                        log(f"下载完成 (curl): {output_path} ({size:,} bytes)")
                        found = True
            except Exception as e:
                log(f"curl 下载失败: {e}", "WARN")

        if not found:
            # 用 urllib 下载
            try:
                import urllib.request
                req = urllib.request.Request(
                    audio_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    with open(output_path, "wb") as f:
                        f.write(resp.read())
                size = os.path.getsize(output_path)
                if size > 0:
                    log(f"下载完成 (urllib): {output_path} ({size:,} bytes)")
                    found = True
            except Exception as e:
                log(f"urllib 下载失败: {e}", "WARN")

    # 清理事件监听
    try:
        page.remove_listener("download", on_download)
        page.remove_listener("response", on_response)
    except Exception:
        pass

    return found


def check_rate_limit(page):
    """检查是否遇到频率限制"""
    try:
        page_text = page.inner_text("body")
        rate_keywords = ["429", "rate limit", "频率", "限制", "稍后", "too many",
                         "请稍", "频繁", "wait", "queue", "排队"]
        for kw in rate_keywords:
            if kw in page_text.lower():
                return True
    except Exception:
        pass
    return False


def reload_page(page):
    """刷新页面并等待表单加载，重新选英语语言"""
    try:
        page.reload(wait_until="load", timeout=60000)
        page.wait_for_timeout(3000)
        wait_for_page_stable(page, max_wait=15)
        page.wait_for_selector("#UserInputTextarea", timeout=60000)
        # 重新选英语语言（刷新后语言会重置）
        select_english_language(page)
        return True
    except Exception as e:
        log(f"页面刷新失败: {e}", "ERROR")
        # 尝试重新导航
        try:
            page.goto(TTSMAKER_URL, wait_until="load", timeout=60000)
            page.wait_for_timeout(5000)
            wait_for_page_stable(page, max_wait=15)
            page.wait_for_selector("#UserInputTextarea", timeout=60000)
            select_english_language(page)
            return True
        except Exception as e2:
            log(f"重新导航也失败: {e2}", "ERROR")
            return False


# ═══════════════════════════════════════════════════════════
#  单条生成
# ═══════════════════════════════════════════════════════════

def generate_one(page, context, rid, text, output_path, max_retries=8):
    """生成单条 TTS 音频"""

    # 如果文件已存在，跳过
    if output_path.exists() and output_path.stat().st_size > 0:
        size = output_path.stat().st_size
        log(f"  [{rid}] 文件已存在 ({size:,} bytes)，跳过")
        return True

    log(f"\n{'─'*50}")
    log(f"  [{rid}] 开始生成")
    log(f"  文本: {text[:60]}{'...' if len(text) > 60 else ''}")
    log(f"  输出: {output_path}")
    log(f"{'─'*50}")

    for attempt in range(1, max_retries + 1):
        log(f"  [{rid}] 第 {attempt}/{max_retries} 次尝试...")

        # 选音色
        if not select_voice(page):
            log(f"  [{rid}] 选音色失败，刷新页面重试", "WARN")
            if not reload_page(page):
                break
            continue

        # 输入文本
        if not input_text(page, text):
            log(f"  [{rid}] 输入文本失败，刷新页面重试", "WARN")
            if not reload_page(page):
                break
            continue

        # 选音频格式
        select_audio_format(page)

        # 清除上一次的音频结果（防止下载到旧音频）
        clear_previous_audio(page)

        # 验证码处理
        # ttsmaker.cn 登录后 JS 会把 user_input_captcha_text 设为 8888
        # 所以登录后可能不需要手动输入验证码
        cap = get_captcha_info(page)
        if cap and cap.get("uuid") and cap.get("src"):
            # 有验证码 — 下载并 OCR
            img_bytes = fetch_captcha_image(page, cap["src"])
            if img_bytes:
                captcha_text = solve_captcha(img_bytes)
                if captcha_text:
                    log(f"  [{rid}] OCR: '{captcha_text}'")
                    fill_captcha(page, captcha_text)
                else:
                    log(f"  [{rid}] OCR 失败，尝试提交", "WARN")
            else:
                log(f"  [{rid}] 验证码图片下载失败，尝试提交", "WARN")
        else:
            # 登录后可能无验证码或已自动填入 8888
            log(f"  [{rid}] 无验证码或已自动填入")

        # 点击转换
        if not click_convert_button(page):
            log(f"  [{rid}] 找不到转换按钮", "ERROR")
            if not reload_page(page):
                break
            continue

        # 等待并下载
        found = wait_and_download(page, context, output_path, timeout=120)
        if found and output_path.exists() and output_path.stat().st_size > 0:
            size = output_path.stat().st_size
            # 校验唯一性：计算 SHA-256 并与已下载文件比对
            sha = file_sha256(output_path)
            if sha in _DOWNLOADED_HASHES:
                prev_id = _DOWNLOADED_HASHES[sha]
                log(f"  [{rid}] ⚠️ 下载的音频与 {prev_id} 完全相同 (SHA {sha[:12]}), 删除并重试", "WARN")
                output_path.unlink(missing_ok=True)
                if not reload_page(page):
                    break
                refresh_captcha(page)
                continue
            _DOWNLOADED_HASHES[sha] = rid
            log(f"  [{rid}] ✅ 成功! ({size:,} bytes, SHA {sha[:12]})")
            return True

        # 检查频率限制
        if check_rate_limit(page):
            log(f"  [{rid}] 遇到频率限制，等待 65 秒...", "WARN")
            time.sleep(65)
            if not reload_page(page):
                break
            continue

        # 检查验证码错误
        try:
            page_text = page.inner_text("body").lower()
            if any(kw in page_text for kw in ["captcha", "验证", "verification"]):
                log(f"  [{rid}] 验证码错误，重试", "WARN")
                refresh_captcha(page)
                continue
        except Exception:
            pass

        # 其他情况：刷新页面重试
        log(f"  [{rid}] 未获取到音频，刷新重试", "WARN")
        if not reload_page(page):
            break
        refresh_captcha(page)

    log(f"  [{rid}] ❌ 失败（超过最大重试次数 {max_retries}）", "ERROR")
    return False


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="TTSMaker.cn 批量 TTS 生成")
    parser.add_argument("--resume", action="store_true",
                        help="跳过已完成的条目（断点续跑）")
    parser.add_argument("--start", default=None,
                        help="从指定 ID 开始（如 tr_0100）")
    parser.add_argument("--only", default=None,
                        help="只跑指定前缀（如 tr_ 或 va_ 或 te_）")
    parser.add_argument("--max-retries", type=int, default=8,
                        help="每条最多重试次数（默认 8）")
    parser.add_argument("--delay", type=float, default=2,
                        help="每条之间等待秒数（默认 2）")
    parser.add_argument("--login-timeout", type=int, default=600,
                        help="登录等待超时秒数（默认 600 = 10 分钟）")
    parser.add_argument("--all", action="store_true",
                        help="生成全部 480 条（包括 test）")
    args = parser.parse_args()

    # 加载 TSV
    rows = load_tsv()
    log(f"TSV 共 {len(rows)} 条")

    # 过滤
    if args.only:
        prefixes = [args.only]
    elif args.all:
        prefixes = ["tr_", "va_", "te_"]
    else:
        # 默认只跑 tr_ 和 va_（README 要求）
        prefixes = ["tr_", "va_"]

    rows = filter_rows(rows, prefixes=prefixes, start_id=args.start)
    log(f"过滤后需生成 {len(rows)} 条（前缀: {prefixes}）")

    if not rows:
        log("没有需要生成的条目", "ERROR")
        sys.exit(1)

    # 加载进度
    progress = load_progress()
    if args.resume:
        done_count = len(progress["completed"])
        log(f"断点续跑模式：已完成 {done_count} 条")

    # 统计
    total = len(rows)
    already_done = 0
    to_run = []

    for rid, split, cat, text in rows:
        output_path = INBOX_DIR / f"{rid}.wav"
        if args.resume and is_completed(progress, rid):
            already_done += 1
            continue
        if output_path.exists() and output_path.stat().st_size > 0:
            already_done += 1
            mark_completed(progress, rid, output_path)
            continue
        to_run.append((rid, split, cat, text))

    log(f"已完成: {already_done}, 待生成: {len(to_run)}")

    if not to_run:
        log("全部已完成，无需生成")
        # 打印统计
        print_summary(progress, total)
        return

    # 启动浏览器
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, context = create_browser(p, headless=False)
        page = context.new_page()

        try:
            # 1. 打开 TTSMaker.cn
            #    注意：ttsmaker.cn 返回 HTTP 500 但 HTML 内容正常，
            #    所以不能依赖状态码判断，用 load 等待 + 超时容错
            log("\n1. 打开 TTSMaker.cn...")
            try:
                page.goto(TTSMAKER_URL, wait_until="load", timeout=60000)
            except Exception as e:
                log(f"首次加载提示（500 状态码正常）: {e}")

            # 等待页面 JS 执行完毕
            log("等待页面加载...")
            for i in range(30):  # 最多等 30 秒
                page.wait_for_timeout(1000)
                try:
                    ready = page.evaluate("() => document.readyState")
                    if ready == "complete":
                        break
                except Exception:
                    continue

            # 确认页面已加载
            wait_for_page_stable(page, max_wait=15)
            log(f"当前 URL: {safe_page_url(page)}")
            log(f"页面标题: {safe_page_title(page)}")

            # 检查表单是否存在
            textarea = page.locator("#UserInputTextarea")
            if textarea.count() == 0:
                log("页面表单未找到，重试加载...", "WARN")
                page.wait_for_timeout(3000)
                try:
                    page.goto(TTSMAKER_URL, wait_until="load", timeout=60000)
                except Exception:
                    pass
                page.wait_for_timeout(5000)
                wait_for_page_stable(page, max_wait=15)
                if page.locator("#UserInputTextarea").count() == 0:
                    log("仍无法加载 TTS 表单", "ERROR")
                    browser.close()
                    sys.exit(1)

            # 2. 等待微信登录
            if not wait_for_login(page, timeout=args.login_timeout):
                log("登录超时，退出", "ERROR")
                browser.close()
                sys.exit(1)

            # 3. 确保页面处于 TTS 状态
            try:
                page.wait_for_selector("#UserInputTextarea", timeout=30000)
            except Exception:
                log("重新加载页面...")
                try:
                    page.goto(TTSMAKER_URL, wait_until="load", timeout=60000)
                except Exception as e:
                    log(f"重新加载提示: {e}")
                page.wait_for_timeout(5000)
                wait_for_page_stable(page, max_wait=15)
                page.wait_for_selector("#UserInputTextarea", timeout=30000)

            log("页面就绪，开始批量生成")

            # 预先选择英语语言
            select_english_language(page)

            # 预加载已有文件的 SHA-256，确保断点续跑也能检测重复
            existing_wavs = sorted(INBOX_DIR.glob("*.wav"))
            for wav in existing_wavs:
                try:
                    sha = file_sha256(wav)
                    _DOWNLOADED_HASHES[sha] = wav.stem
                except Exception:
                    pass
            if _DOWNLOADED_HASHES:
                log(f"已加载 {len(_DOWNLOADED_HASHES)} 个已有文件哈希，用于重复检测")

            # 4. 批量生成
            success_count = 0
            fail_count = 0

            for idx, (rid, split, cat, text) in enumerate(to_run):
                progress_idx = already_done + idx + 1
                log(f"\n{'='*60}")
                log(f"  进度: {progress_idx}/{already_done + len(to_run)} "
                    f"({progress_idx * 100 // (already_done + len(to_run))}%)")
                log(f"  ID: {rid} | Split: {split} | Category: {cat}")
                log(f"{'='*60}")

                output_path = INBOX_DIR / f"{rid}.wav"

                try:
                    ok = generate_one(
                        page, context, rid, text, output_path,
                        max_retries=args.max_retries,
                    )
                    if ok:
                        success_count += 1
                        mark_completed(progress, rid, output_path)
                    else:
                        fail_count += 1
                        mark_failed(progress, rid, "超过最大重试次数")
                except Exception as e:
                    log(f"  [{rid}] 异常: {e}", "ERROR")
                    fail_count += 1
                    mark_failed(progress, rid, str(e))
                    # 刷新页面
                    reload_page(page)

                # 条间延迟
                if idx < len(to_run) - 1:
                    log(f"  等待 {args.delay} 秒...")
                    time.sleep(args.delay)

            # 5. 总结
            log(f"\n{'='*60}")
            log(f"  批量生成完成！")
            log(f"  成功: {success_count}")
            log(f"  失败: {fail_count}")
            log(f"  之前已完成: {already_done}")
            log(f"  总计: {already_done + len(to_run)}")
            log(f"  输出目录: {INBOX_DIR}")
            log(f"{'='*60}")

        except KeyboardInterrupt:
            log("\n用户中断，保存进度后退出")
        except Exception as e:
            log(f"严重错误: {e}", "ERROR")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    # 打印最终统计
    print_summary(progress, total)


def print_summary(progress, total):
    """打印进度统计"""
    log(f"\n{'='*60}")
    log(f"  进度统计")
    log(f"{'='*60}")
    log(f"  总条目: {total}")
    log(f"  已完成: {len(progress['completed'])}")
    log(f"  已失败: {len(progress['failed'])}")
    if progress["failed"]:
        log(f"\n  失败列表:")
        for rid, info in progress["failed"].items():
            log(f"    {rid}: {info['reason']}")
    log(f"\n  进度文件: {PROGRESS_FILE}")
    log(f"  音频目录: {INBOX_DIR}")
    log(f"  日志文件: {LOG_FILE}")


if __name__ == "__main__":
    main()
