#!/usr/bin/env python3
"""
TTSMaker 客户端 — 男声 (788/Alfie) 音频生成
=============================================
通过 Playwright 自动化操作 TTSMaker 网站，生成 788 (Alfie) 男声音频。

设计:
  - 男声 (MALE_VOICE) → TTSMaker 788 (Alfie)
  - 女声 (FEMALE_VOICE) → edge-tts (不变)
  - TTSMaker 不可用时（打包模式/缺依赖）→ 回退到 edge-tts

依赖（仅开发模式需要）:
  pip install playwright ddddocr
  playwright install chromium
"""

import os
import sys
import uuid
import asyncio

# ============================================================================
# 路径设置
# ============================================================================
if getattr(sys, 'frozen', False):
    _RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
else:
    _RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# 尝试将 ttsmaker/ 目录加入 sys.path
_TTSMAKER_DIR = os.path.join(_RESOURCE_DIR, "ttsmaker")
if _TTSMAKER_DIR not in sys.path:
    sys.path.insert(0, _TTSMAKER_DIR)

# ============================================================================
# 可选导入 TTSMaker 核心模块
# ============================================================================
_TTSMAKER_AVAILABLE = False
_ttsmaker_generate = None

try:
    from ttsmaker.ttsmaker import generate as _ttsmaker_generate_sync
    _TTSMAKER_AVAILABLE = True
    print("[ttsmaker] TTSMaker 模块加载成功，男声将使用 TTSMaker 788 (Alfie)", file=sys.stderr)
except ImportError as e:
    print(f"[ttsmaker] TTSMaker 模块不可用: {e}，男声将回退到 edge-tts", file=sys.stderr)
except Exception as e:
    print(f"[ttsmaker] TTSMaker 模块加载异常: {e}，男声将回退到 edge-tts", file=sys.stderr)


def is_available():
    """检查 TTSMaker 是否可用。"""
    return _TTSMAKER_AVAILABLE


def _generate_sync(text, output_path, voice_key="alfie", max_retries=8, show_browser=False):
    """同步调用 TTSMaker 生成音频。"""
    if not _TTSMAKER_AVAILABLE:
        raise RuntimeError("TTSMaker 模块不可用")
    return _ttsmaker_generate_sync(
        text,
        voice_key=voice_key,
        output_name=os.path.basename(output_path),
        max_retries=max_retries,
        show_browser=show_browser,
    )


async def synth_male_ttsmaker(text, tmp_dir, voice_key="alfie"):
    """
    用 TTSMaker 生成男声音频，返回 pydub.AudioSegment。

    Args:
        text: 要合成的文本
        tmp_dir: 临时目录路径
        voice_key: TTSMaker 音色 key（默认 "alfie" = 788 男声）

    Returns:
        pydub.AudioSegment 音频段

    Raises:
        RuntimeError: TTSMaker 不可用或生成失败
    """
    if not _TTSMAKER_AVAILABLE:
        raise RuntimeError("TTSMaker 模块不可用，请安装 playwright 和 ddddocr")

    # 生成临时输出路径
    uid = uuid.uuid4().hex[:8]
    output_filename = f".ttsmaker_{uid}.mp3"
    output_path = os.path.join(tmp_dir, output_filename)

    # 检查环境变量是否要求显示浏览器
    show_browser = os.environ.get("TTSMAKER_SHOW_BROWSER", "").lower() in {"1", "true", "yes"}

    try:
        # 在线程中运行同步的 Playwright 代码
        result_path = await asyncio.to_thread(
            _generate_sync,
            text,
            output_path,
            voice_key,
            8,  # max_retries
            show_browser,
        )

        if not result_path or not os.path.exists(result_path):
            raise RuntimeError(f"TTSMaker 未生成音频文件: {result_path}")

        # 检查文件大小
        fsize = os.path.getsize(result_path)
        print(f"[ttsmaker] 生成完成: {result_path} ({fsize} bytes)", file=sys.stderr)
        if fsize < 100:
            raise RuntimeError(f"TTSMaker 返回的音频过小 ({fsize} bytes)，可能生成失败")

        # 用 pydub 加载音频
        from pydub import AudioSegment
        seg = AudioSegment.from_file(result_path, format="mp3", codec="mp3")
        dur_ms = len(seg)
        print(f"[ttsmaker] pydub 解码完成: duration={dur_ms}ms channels={seg.channels} sample_rate={seg.frame_rate}", file=sys.stderr)
        if dur_ms < 50:
            raise RuntimeError(f"解码后音频时长过短 ({dur_ms}ms)，可能 TTSMaker 返回了空音频")
        if seg.channels == 0 or seg.frame_rate == 0:
            raise RuntimeError(f"解码后音频参数异常 (channels={seg.channels}, frame_rate={seg.frame_rate})")
        return seg

    finally:
        # 清理临时文件
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
