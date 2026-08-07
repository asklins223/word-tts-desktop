#!/usr/bin/env python3
"""
TTSMaker 客户端 — 男声 (788/Alfie) 音频生成
=============================================
通过 Playwright 自动化操作 TTSMaker 网站，生成 788 (Alfie) 男声音频。

设计:
  - 男声 (MALE_VOICE) → TTSMaker 788 (Alfie)
  - 女声 (FEMALE_VOICE) → edge-tts (不变)
  - TTSMaker 不可用时（打包模式/缺依赖）→ 回退到 edge-tts

持久化会话模式:
  - 启动时调用 login()，打开可见浏览器让用户扫码登录
  - 每条男声调用 synth_male_ttsmaker()，浏览器保持开启
  - 全部完成后调用 close_session() 关闭浏览器

依赖:
  开发模式: pip install playwright && playwright install chromium
  打包模式: Playwright + Chromium 已内置打包，无需额外安装
"""

import os
import sys
import uuid
import asyncio
import threading

# ============================================================================
# 路径设置
# ============================================================================
if getattr(sys, 'frozen', False):
    _RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
else:
    _RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# 确保 _RESOURCE_DIR 在 sys.path 中（用于 namespace package 导入）
if _RESOURCE_DIR not in sys.path:
    sys.path.insert(0, _RESOURCE_DIR)

# ============================================================================
# Playwright 浏览器路径设置（PyInstaller 打包环境）
# ============================================================================
# 在打包环境中，Chromium 浏览器二进制被打包到 _MEIPASS/playwright_browsers/
# 需要设置 PLAYWRIGHT_BROWSERS_PATH 环境变量让 Playwright 找到它
if getattr(sys, 'frozen', False):
    _bundled_browsers = os.path.join(_RESOURCE_DIR, 'playwright_browsers')
    if os.path.isdir(_bundled_browsers):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _bundled_browsers
        print(f"[ttsmaker] 使用打包内置的 Playwright 浏览器: {_bundled_browsers}", file=sys.stderr)
    else:
        print("[ttsmaker] 未找到打包的 Playwright 浏览器，将使用系统 Chrome", file=sys.stderr)
else:
    # 开发模式：如果本地安装了 Playwright 浏览器，确保路径正确
    # 不设置环境变量，让 Playwright 使用默认缓存路径
    pass

# ============================================================================
# 可选导入 TTSMaker 核心模块
# ============================================================================
_TTSMAKER_AVAILABLE = False
_TTSMakerSession = None
_ttsmaker_output_dir = None

try:
    from ttsmaker.ttsmaker import TTSMakerSession as _TTSMakerSession, OUTPUT_DIR as _ttsmaker_output_dir
    _TTSMAKER_AVAILABLE = True
    print("[ttsmaker] TTSMaker 模块加载成功，男声将使用 TTSMaker 788 (Alfie)", file=sys.stdout)
except ImportError as e:
    print(f"[ttsmaker] TTSMaker 模块不可用: {e}，男声将回退到 edge-tts", file=sys.stdout)
except Exception as e:
    print(f"[ttsmaker] TTSMaker 模块加载异常: {e}，男声将回退到 edge-tts", file=sys.stdout)


# ============================================================================
# 持久化会话管理
# ============================================================================
# 注意: 不能用 asyncio.Lock()，因为 word_tts_app.py 通过 asyncio.run() 多次调用
# ensure_session/close_session，每次 asyncio.run() 创建新的事件循环，
# 而 asyncio.Lock 会绑定到第一次使用时的循环，第二次调用时抛出
# "bound to a different event loop" 异常。
# threading.Lock 不受事件循环限制，通过 asyncio.to_thread 在线程中获取即可。
_session = None
_session_lock = threading.Lock()


def is_available():
    """检查 TTSMaker 是否可用。"""
    return _TTSMAKER_AVAILABLE


def _session_is_healthy(session):
    """
    轻量级健康检查：验证会话的关键属性仍然有效。

    只检查 Python 层的标志和对象引用，不发起任何 Playwright 网络/DOM
    操作，因此可以在任何线程中安全调用（包括事件循环线程）。
    """
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


async def ensure_session(voice_key="alfie"):
    """
    确保 TTSMaker 浏览器会话已登录。
    如果会话不存在或已损坏，则创建并打开浏览器等待用户登录。
    如果会话已存在且健康，则直接返回。

    Returns: TTSMakerSession 实例
    Raises: RuntimeError 如果 TTSMaker 不可用或登录失败
    """
    global _session

    if not _TTSMAKER_AVAILABLE:
        raise RuntimeError("TTSMaker 模块不可用，请安装 playwright")

    # 快速健康检查：如果已有会话但已损坏（浏览器崩溃/页面关闭等），
    # 先丢弃旧会话再重建，避免后续所有男声生成都卡在坏会话上。
    if _session is not None:
        if _session_is_healthy(_session):
            return _session
        print(
            "[ttsmaker] 检测到已有会话已失效，将丢弃并重新创建",
            file=sys.stderr,
        )
        _discard_session_unsafe()

    # 在线程中获取 threading.Lock（避免阻塞事件循环），
    # 同时支持跨 asyncio.run() 调用（不绑定到特定事件循环）
    def _locked_create():
        with _session_lock:
            # 双重检查
            if _session is not None:
                if _session_is_healthy(_session):
                    return _session
                _discard_session_unsafe()
            return _create_and_login_session(voice_key)

    _session = await asyncio.to_thread(_locked_create)
    return _session


def _create_and_login_session(voice_key="alfie"):
    """同步创建并登录 TTSMaker 会话。"""
    session = _TTSMakerSession(voice_key=voice_key)
    try:
        session.login(login_timeout=300)
    except Exception:
        # 登录失败时关闭浏览器，防止进程泄漏和 profile 目录锁定
        try:
            session.close()
        except Exception:
            pass
        raise
    return session


async def synth_male_ttsmaker(
    text, tmp_dir, voice_key="alfie",
    rate=1.0, volume=1, pitch=1, pause=0,
):
    """
    用 TTSMaker 生成男声音频，返回 pydub.AudioSegment。
    使用持久化会话：首次调用会触发登录，后续调用复用同一浏览器。

    Args:
        text: 要合成的文本
        tmp_dir: 临时目录路径（用于存放中间文件）
        voice_key: TTSMaker 音色 key（默认 "alfie" = 788 男声）
        rate: 语速倍率 (TTSMaker 格式, 1.0=正常, 1.5=加速50%)
        volume: 音量倍率 (TTSMaker 格式, 1=正常, 1.5=增大50%)
        pitch: 音调倍率 (TTSMaker 格式, 1=正常, 1.1=升高10%)
        pause: 段落停顿 (TTSMaker 格式, -1=不停顿, 0=默认300ms, 500=400ms, 其他N=N ms)

    Returns:
        pydub.AudioSegment 音频段

    Raises:
        RuntimeError: TTSMaker 不可用或生成失败
    """
    if not _TTSMAKER_AVAILABLE:
        raise RuntimeError("TTSMaker 模块不可用，请安装 playwright")

    # 参数已经是 TTSMaker 格式，直接转为字符串
    ttsmaker_speed = str(rate)
    ttsmaker_volume = str(volume)
    ttsmaker_pitch = str(pitch)
    ttsmaker_pause = str(int(float(pause)))

    print(
        f"[ttsmaker] 参数: speed={ttsmaker_speed} volume={ttsmaker_volume} "
        f"pitch={ttsmaker_pitch} pause={ttsmaker_pause}",
        file=sys.stdout,
    )

    # 确保会话已登录
    session = await ensure_session(voice_key)

    # 生成唯一的输出文件名
    uid = uuid.uuid4().hex[:8]
    output_name = f".ttsmaker_{uid}.mp3"

    result_path = None
    try:
        # 在线程中运行同步的 Playwright 生成代码
        result_path = await asyncio.to_thread(
            session.synth_one,
            text,
            output_name,
            8,  # max_retries
            ttsmaker_speed,
            ttsmaker_volume,
            ttsmaker_pitch,
            ttsmaker_pause,
        )

        if not result_path or not os.path.exists(result_path):
            raise RuntimeError(f"TTSMaker 未生成音频文件: {result_path}")

        # 检查文件大小
        fsize = os.path.getsize(result_path)
        print(f"[ttsmaker] 生成完成: {result_path} ({fsize} bytes)", file=sys.stdout)
        if fsize < 100:
            raise RuntimeError(f"TTSMaker 返回的音频过小 ({fsize} bytes)，可能生成失败")

        # 用 pydub 加载音频
        from pydub import AudioSegment
        seg = AudioSegment.from_file(result_path, format="mp3", codec="mp3")
        dur_ms = len(seg)
        print(f"[ttsmaker] pydub 解码完成: duration={dur_ms}ms channels={seg.channels} sample_rate={seg.frame_rate}", file=sys.stdout)
        if dur_ms < 50:
            raise RuntimeError(f"解码后音频时长过短 ({dur_ms}ms)，可能 TTSMaker 返回了空音频")
        if seg.channels == 0 or seg.frame_rate == 0:
            raise RuntimeError(f"解码后音频参数异常 (channels={seg.channels}, frame_rate={seg.frame_rate})")
        return seg

    finally:
        # 清理 TTSMaker 生成的临时文件（在 ttsmaker_output/ 目录中）
        # 注意：不关闭浏览器会话，保持待机等待下一条
        if result_path and os.path.exists(result_path):
            try:
                os.remove(result_path)
            except OSError:
                pass


def _discard_session_unsafe():
    """
    清空全局 _session 引用（不加锁）。

    调用方需自行持有 _session_lock 或确保不会并发调用。
    旧会话的 close() 不在这里执行——由 close_session() 负责——
    此函数只确保 _session 不再返回给新的调用方。
    """
    global _session
    _session = None


async def close_session():
    """
    关闭 TTSMaker 浏览器会话。
    应在所有男声生成完成后调用。

    无论 close() 是否抛异常，都会先清空全局 _session，防止坏会话
    被后续任务复用导致长时间卡顿后回退到备用音色。
    """
    global _session
    # 先清空全局引用，再关闭旧会话。
    # 这样即使 close() 抛异常或超时，后续 ensure_session() 也不会
    # 拿到坏会话——最坏情况是重建一个新会话，而不是卡在旧会话上。
    old = _session
    _session = None
    if old is not None:
        try:
            await asyncio.to_thread(old.close)
            print("[ttsmaker] 浏览器会话已关闭", file=sys.stdout)
        except Exception as e:
            print(
                f"[ttsmaker] 关闭浏览器会话异常（已清空引用，不影响后续任务）: {e}",
                file=sys.stderr,
            )
