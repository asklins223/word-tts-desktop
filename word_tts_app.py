#!/usr/bin/env python3
"""
Word 文档解析 + 讯飞配音音频生成 — 一体化应用
================================================
1. 上传 Word 文档 → 自动识别题型并解析为 JSON
2. 解析成功后自动开始生成音频（支持 w/m 说话人标识自动选音色）
3. 全程进度记录，支持断点续传
4. 生成完成后可下载 ZIP 包或选择单个文件下载
5. 文件命名规则：信息获取题目使用问题x；其他题型使用题型-录音稿x

引擎与音色规则（统一使用讯飞配音 peiyin.xunfei.cn）：
  - w/W 标识 → 女声 Amanda
  - m/M 标识 → 男声 George
  - 无标识   → 默认女声 Amanda
  - 词汇题型（单词/例句）统一使用默认女声 Amanda（无单独音色）
  - 生成音频时自动去除 w/m 标识
  - 可调参数为讯飞平台三参数：语速 / 语调 / 音量（0-100，50=默认）

用法:
    python word_tts_app.py
"""

import os
import sys

# Windows 打包后 stdout/stderr 默认使用 cp1252 编码，无法输出中文。
# 在任何 print 之前重配置为 UTF-8，防止 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

import re
import json
import uuid
import shutil
import asyncio
import zipfile
import html as _html
from datetime import datetime

# ============================================================================
# 路径与模块导入
# ============================================================================
# ---- PyInstaller 兼容：将打包资源路径加入 sys.path ----
_configured_data_dir = os.environ.get("WORDTTS_DATA_DIR", "").strip()
if _configured_data_dir:
    BASE_DIR = os.path.abspath(os.path.expanduser(_configured_data_dir))
    os.makedirs(BASE_DIR, exist_ok=True)
    _RESOURCE_DIR = (
        getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        if getattr(sys, 'frozen', False)
        else os.path.dirname(os.path.abspath(__file__))
    )
elif getattr(sys, 'frozen', False):
    # 打包模式：BASE_DIR 指向用户数据目录（可写、持久化），
    # 避免写入 .app 包内部（代码签名后只读，App Translocation 后只读）。
    if sys.platform == 'darwin':
        BASE_DIR = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'WordTTS')
    elif sys.platform == 'win32':
        BASE_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'WordTTS')
    else:
        BASE_DIR = os.path.join(os.path.expanduser('~'), '.wordtts')
    os.makedirs(BASE_DIR, exist_ok=True)
    _RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    if _RESOURCE_DIR not in sys.path:
        sys.path.insert(0, _RESOURCE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _RESOURCE_DIR = BASE_DIR

WORD_PARSER_DIR = os.path.join(_RESOURCE_DIR, "word_parser")

if WORD_PARSER_DIR not in sys.path:
    sys.path.insert(0, WORD_PARSER_DIR)

try:
    import gradio as gr
except (ImportError, ModuleNotFoundError):
    # 打包模式（PyInstaller 排除了 gradio）下使用 stub，
    # 让模块级 UI 代码安全执行但不产生实际效果。
    # server.py 只使用核心 TTS 函数，不会访问任何 UI 对象。
    class _GrStub:
        def __getattr__(self, _name):
            return self
        def __call__(self, *args, **kwargs):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    gr = _GrStub()

from pydub import AudioSegment

# ---- 配置 pydub 使用 imageio-ffmpeg 自带的静态 ffmpeg ----
def _find_ffmpeg():
    """查找 ffmpeg 可执行文件路径，兼容 PyInstaller 打包环境。"""
    # 方式 1：imageio_ffmpeg.get_ffmpeg_exe()
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception as e:
        print(f"[ffmpeg] imageio_ffmpeg.get_ffmpeg_exe() 失败: {e}", file=sys.stdout)

    # 方式 2：在 PyInstaller 的 _MEIPASS 中手动搜索 binaries 目录
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        binaries_dir = os.path.join(meipass, 'imageio_ffmpeg', 'binaries')
        if os.path.isdir(binaries_dir):
            for name in os.listdir(binaries_dir):
                if name.lower().startswith('ffmpeg') and (
                    name.lower().endswith('.exe') or
                    not name.lower().endswith(('.md', '.txt', '.py'))
                ):
                    candidate = os.path.join(binaries_dir, name)
                    if os.path.isfile(candidate):
                        return candidate

    # 方式 3：系统 PATH 中的 ffmpeg
    import shutil
    system_ff = shutil.which('ffmpeg')
    if system_ff:
        return system_ff

    return None

_ffmpeg_path = _find_ffmpeg()
if _ffmpeg_path:
    AudioSegment.converter = _ffmpeg_path
    os.environ["FFMPEG_BINARY"] = _ffmpeg_path
    # 将 ffmpeg 所在目录加入 PATH，供其他模块（如 ffmpy）使用
    ff_dir = os.path.dirname(_ffmpeg_path)
    if ff_dir not in os.environ.get('PATH', ''):
        os.environ['PATH'] = ff_dir + os.pathsep + os.environ.get('PATH', '')
    print(f"[ffmpeg] 使用: {_ffmpeg_path}", file=sys.stdout)
    # 验证 ffmpeg 可执行
    try:
        import subprocess as _sp
        _r = _sp.run([_ffmpeg_path, '-version'], capture_output=True, timeout=10)
        if _r.returncode == 0:
            _ver_line = _r.stdout.decode('utf-8', errors='replace').split('\n')[0]
            print(f"[ffmpeg] 验证通过: {_ver_line}", file=sys.stdout)
        else:
            print(f"[ffmpeg] 验证失败: returncode={_r.returncode}", file=sys.stdout)
    except Exception as _e:
        print(f"[ffmpeg] 验证异常: {_e}", file=sys.stdout)
else:
    print("[ffmpeg] 警告: 未找到 ffmpeg，音频处理将失败", file=sys.stdout)

# ---- pydub ffprobe 兼容 ----
# pydub 的 mediainfo_json() 会调用 ffprobe（独立可执行文件），
# 但 imageio_ffmpeg 只提供 ffmpeg，不包含 ffprobe。
# 在打包环境中 ffprobe 不存在会导致 WinError 2。
# 解决：monkey-patch mediainfo_json，当 ffprobe 不可用时返回 None，
# 让 pydub 走纯 ffmpeg 路径。
import pydub.utils as _pydub_utils
_orig_mediainfo_json = _pydub_utils.mediainfo_json

def _safe_mediainfo_json(filepath, read_ahead_limit=-1):
    """如果 ffprobe 不可用，返回 None 而不是抛出 FileNotFoundError。"""
    try:
        return _orig_mediainfo_json(filepath, read_ahead_limit)
    except (FileNotFoundError, OSError):
        print("[pydub] ffprobe 不可用，跳过 mediainfo", file=sys.stdout)
        return None

_pydub_utils.mediainfo_json = _safe_mediainfo_json

from word_parser import parse_document_auto, PARSER_MAP

# ---- 讯飞配音客户端（统一 TTS 引擎，女声/男声均使用）----
try:
    import xunfei_peiyin as _xunfei
    _XUNFEI_AVAILABLE = _xunfei.is_available()
except Exception:
    _XUNFEI_AVAILABLE = False
    _xunfei = None


# ============================================================================
# 常量配置
# ============================================================================

OUTPUT_BASE = os.path.join(BASE_DIR, "tts_output")
os.makedirs(OUTPUT_BASE, exist_ok=True)

# 音色配置 — 讯飞配音发音人 key
# 女声 → Amanda (英语女声)；词汇题型同样使用该女声（无单独音色）
FEMALE_VOICE = "amanda"
# 男声 → George (英语男声)
MALE_VOICE = "george"

# 词汇题型不再使用单独音色，统一走默认女声。
WORD_CATEGORIES = frozenset({"单词", "例句"})

# 每条解析结果（每道题）独立生成一个音频文件，不做跨题合并

# 导出格式
FORMAT_MAP = {
    "mp3": ("mp3", ".mp3"),
    "ogg": ("ogg", ".ogg"),
    "aac": ("adts", ".aac"),
    "opus": ("opus", ".opus"),
    "wav": ("wav", ".wav"),
}

QUALITY_BITRATE = {
    "48 kbps（低）": "48k",
    "128 kbps（标准）": "128k",
    "192 kbps（高）": "192k",
    "320 kbps（极高）": "320k",
    "无损（仅 wav 生效）": None,
}

# 音频生成算法版本。改变讯飞音频拼接策略时递增，避免复用旧算法产物。
AUDIO_ALGORITHM_VERSION = 3

# 解析器版本。解析逻辑变更（如音色分配、文件命名规则等）时递增，
# 避免断点续传复用旧解析结果（旧结果可能缺少 voice/filename_stem 等字段）。
PARSER_VERSION = 8

# 讯飞平台三项声音参数：均为整数 0-100，50 为平台默认值。
TTS_PARAM_MIN = 0
TTS_PARAM_MAX = 100
TTS_PARAM_DEFAULT = 50
TTS_CONFIG_VERSION = 4


def clamp_tts_param(value, default=TTS_PARAM_DEFAULT):
    """将任意输入收敛为讯飞平台接受的 0-100 整数。"""
    try:
        normalized = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        normalized = default
    return max(TTS_PARAM_MIN, min(TTS_PARAM_MAX, normalized))


def normalize_role_key(value):
    """返回前后端统一使用的角色 key。"""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text.casefold()[:80]


def _normalize_voice_key(value, fallback):
    key = str(value or "").strip()
    return key[:160] if key else fallback


def _normalize_voice_params(raw, fallback):
    values = raw if isinstance(raw, dict) else {}
    return {
        "rate": clamp_tts_param(values.get("rate", fallback["rate"])),
        "volume": clamp_tts_param(values.get("volume", fallback["volume"])),
        "pitch": clamp_tts_param(values.get("pitch", fallback["pitch"])),
    }


def normalize_tts_config(config=None):
    """返回只包含当前产品支持项的规范化声音/输出配置。

    前端校验用于即时反馈，服务端和核心流程也必须独立收敛输入，避免旧版
    预设或手工请求把倍率、代理、旧音色字段带回讯飞调用链。
    """
    raw = config if isinstance(config, dict) else {}
    fmt = raw.get("format", "mp3")
    if fmt not in FORMAT_MAP:
        fmt = "mp3"
    quality = raw.get("quality", "128 kbps（标准）")
    if quality not in QUALITY_BITRATE:
        quality = "128 kbps（标准）"

    base_params = {
        "rate": clamp_tts_param(raw.get("rate", TTS_PARAM_DEFAULT)),
        "volume": clamp_tts_param(raw.get("volume", TTS_PARAM_DEFAULT)),
        "pitch": clamp_tts_param(raw.get("pitch", TTS_PARAM_DEFAULT)),
    }
    default_female_voice = _normalize_voice_key(
        raw.get("default_female_voice"), FEMALE_VOICE
    )
    default_male_voice = _normalize_voice_key(
        raw.get("default_male_voice"), MALE_VOICE
    )

    voice_configs = {}
    raw_voice_configs = raw.get("voice_configs")
    if isinstance(raw_voice_configs, dict):
        for key, value in raw_voice_configs.items():
            normalized_key = _normalize_voice_key(key, "")
            if normalized_key:
                voice_configs[normalized_key] = _normalize_voice_params(value, base_params)
    voice_configs.setdefault(
        default_female_voice,
        _normalize_voice_params(None, base_params),
    )
    voice_configs.setdefault(
        default_male_voice,
        _normalize_voice_params(None, base_params),
    )

    role_voices = {}
    raw_role_voices = raw.get("role_voices")
    if isinstance(raw_role_voices, dict):
        for role, voice in raw_role_voices.items():
            role_key = normalize_role_key(role)
            voice_key = _normalize_voice_key(voice, "")
            if role_key and voice_key:
                role_voices[role_key] = voice_key

    # 兼容旧前端的全局三参数，同时保留每个音色/角色的独立配置。
    return {
        "config_version": TTS_CONFIG_VERSION,
        "rate": base_params["rate"],
        "volume": base_params["volume"],
        "pitch": base_params["pitch"],
        "format": fmt,
        "quality": quality,
        "preview": bool(raw.get("preview", False)),
        "default_female_voice": default_female_voice,
        "default_male_voice": default_male_voice,
        "voice_configs": voice_configs,
        "role_voices": role_voices,
    }

TYPE_COLORS = {
    "信息获取": "#0e7490",
    "课文跟读": "#15803d",
    "信息转述及询问": "#b45309",
    "模仿朗读": "#9f1239",
    "词汇": "#1e40af",
}


# ============================================================================
# 工具函数
# ============================================================================

def fmt_pct(v):
    return ("+" if v >= 0 else "") + str(int(v)) + "%"


def fmt_hz(v):
    return ("+" if v >= 0 else "") + str(int(v)) + "Hz"


def export_audio(seg, fmt, quality, out_path):
    """按指定格式与码率导出音频。"""
    if len(seg) < 50:
        raise RuntimeError(f"音频时长过短 ({len(seg)}ms)，无法导出")
    fmt_id, _ext = FORMAT_MAP[fmt]
    kwargs = {"format": fmt_id}
    br = QUALITY_BITRATE.get(quality)
    if br and fmt in ("mp3", "aac", "opus"):
        kwargs["bitrate"] = br
    print(f"[export] 导出: {out_path} fmt={fmt_id} dur={len(seg)}ms bitrate={br}", file=sys.stdout)
    export_result = seg.export(out_path, **kwargs)
    if hasattr(export_result, "close"):
        export_result.close()
    out_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    print(f"[export] 完成: {out_path} ({out_size} bytes)", file=sys.stdout)
    if out_size == 0:
        raise RuntimeError(f"导出文件为空: {out_path}")

    # 回读验证：确认导出的文件可以被 pydub 重新加载
    # 注意：Windows 上路径含中文字符时，pydub 直接将路径传给 ffmpeg 子进程可能失败，
    # 因此先复制到 ASCII 临时文件名再验证。
    import tempfile as _verify_tmp
    _verify_fd, _verify_tmp_path = _verify_tmp.mkstemp(suffix=f".{_ext.lstrip('.')}")
    try:
        import shutil as _verify_shutil
        _verify_shutil.copy2(out_path, _verify_tmp_path)
        try:
            with open(_verify_tmp_path, "rb") as verify_source:
                # 让 FFmpeg 从文件内容自动识别输入容器。输出侧的 adts/opus
                # 名称不是所有 FFmpeg 构建都接受的输入 demuxer 名称。
                verify_seg = AudioSegment.from_file(verify_source)
            if len(verify_seg) < 10:
                raise RuntimeError(f"回读验证失败: 时长 {len(verify_seg)}ms 过短")
            print(f"[export] 回读验证通过: dur={len(verify_seg)}ms size={out_size}B", file=sys.stdout)
        except Exception as ve:
            # 回读失败可能是 ffmpeg 子进程问题，不一定是文件本身问题
            # 文件大小已确认非零，降级为警告而非错误
            print(f"[export] 警告: 回读验证失败 (非致命): {ve} (文件大小: {out_size}B)", file=sys.stdout)
    finally:
        try:
            os.close(_verify_fd)
        except OSError:
            pass
        try:
            os.remove(_verify_tmp_path)
        except OSError:
            pass
    return out_path


def _get_filepath(file_obj):
    """兼容不同 Gradio 版本下 gr.File 返回值的类型。"""
    if file_obj is None:
        return None
    if isinstance(file_obj, list):
        file_obj = file_obj[0] if file_obj else None
    if file_obj is None:
        return None
    if isinstance(file_obj, str):
        return file_obj
    for attr in ("name", "path"):
        v = getattr(file_obj, attr, None)
        if isinstance(v, str) and v:
            return v
    if isinstance(file_obj, dict):
        return file_obj.get("name") or file_obj.get("path")
    return None


def sanitize_dirname(name):
    """将文件名转换为安全的目录名。"""
    name = os.path.splitext(name)[0]
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = re.sub(r'\s+', '_', name)
    return name[:80]


def now_str():
    return datetime.now().strftime("%H:%M:%S")


# ============================================================================
# 说话人解析：从文本中提取 w/m 标识，分配音色，并清除标识
# ============================================================================

# 匹配行首的 W: / M: / W：/ M：标记
RE_LINE_SPEAKER = re.compile(r'^([WwMm])\s*[:：]\s*(.*)')
# 匹配行首的 (W) / (M) 标记
RE_PAREN_SPEAKER = re.compile(r'^\(([WwMm])\)\s*(.*)')
# 匹配题型录音稿中的通用角色标记，例如 Reporter: / Mr Yan: / Ms Wu:
RE_ROLE_LINE = re.compile(r'^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$')


def _looks_like_role_label(label):
    """避免把 URL、时间或带句末标点的普通句子误当成角色名。"""
    value = str(label or "").strip()
    if not value or len(value) > 48:
        return False
    if len(re.split(r"\s+", value)) > 4:
        return False
    if value[0].isdigit() or "://" in value or "\\" in value or "/" in value:
        return False
    if re.search(r"[.!?。！？；;，,]", value):
        return False
    return True


def _infer_role_voice(label, female_voice, male_voice):
    """没有手动分配时，按 Mr/Ms 等常见称谓给角色一个可改的初始音色。"""
    value = str(label or "").strip().casefold()
    if re.match(r"^(mr|mr\.|sir|男|先生)\b", value):
        return male_voice
    if re.match(r"^(ms|ms\.|mrs|mrs\.|miss|女|女士)\b", value):
        return female_voice
    return female_voice


def parse_speakers_with_roles(
    text,
    default_voice=None,
    female_voice=None,
    male_voice=None,
    role_voices=None,
):
    """解析 W/M 和通用角色标记，返回 ``(role, voice, clean_text)``。

    ``role`` 是用户在界面中看到的原始角色名；没有角色名的普通段落为 None。
    角色映射只决定音色，参数由 ``_synth_item`` 按最终 voice key 查找，因而
    同一个音色在多个角色中仍然共享同一套独立配置。
    """
    if default_voice is None:
        default_voice = FEMALE_VOICE
    fv = female_voice if female_voice else FEMALE_VOICE
    mv = male_voice if male_voice else MALE_VOICE
    role_map = {}
    if isinstance(role_voices, dict):
        role_map = {
            normalize_role_key(role): str(voice).strip()
            for role, voice in role_voices.items()
            if normalize_role_key(role) and str(voice or "").strip()
        }

    segments = []
    lines = str(text or "").strip().split('\n')
    current_voice = default_voice
    current_role = None
    current_lines = []

    # 先看完整录音稿里是否至少有两个不同的角色标签。这样即使调用方没有
    # 传入 role_voices，也能处理真正的多角色对话；单独一行的普通冒号文本
    # 则不会被误拆。若前端已经明确传入某个角色映射，则允许该角色单独出现。
    candidate_role_keys = set()
    for line in lines:
        candidate = RE_ROLE_LINE.match(line.strip())
        if candidate and _looks_like_role_label(candidate.group(1)):
            candidate_role_keys.add(normalize_role_key(candidate.group(1)))
    allow_inferred_roles = len(candidate_role_keys) >= 2

    def flush():
        nonlocal current_lines
        if current_lines:
            clean = '\n'.join(current_lines).strip()
            if clean:
                segments.append((current_role, current_voice, clean))
            current_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        marker = RE_LINE_SPEAKER.match(stripped)
        if marker:
            flush()
            gender = marker.group(1).upper()
            current_voice = fv if gender == 'W' else mv
            current_role = None
            content = marker.group(2).strip()
            if content:
                current_lines.append(content)
            continue

        paren_marker = RE_PAREN_SPEAKER.match(stripped)
        if paren_marker:
            flush()
            gender = paren_marker.group(1).upper()
            current_voice = fv if gender == 'W' else mv
            current_role = None
            content = paren_marker.group(2).strip()
            if content:
                current_lines.append(content)
            continue

        role_marker = RE_ROLE_LINE.match(stripped)
        if role_marker and _looks_like_role_label(role_marker.group(1)):
            candidate_role = role_marker.group(1).strip()
            role_key = normalize_role_key(candidate_role)
            # 通用角色名由前端根据完整解析结果识别并写入 role_voices。
            # 没有当前文档角色映射时，保留普通的「说明: 内容」原文，
            # 避免无角色题被误拆成角色段落。
            if role_key not in role_map and not allow_inferred_roles:
                current_lines.append(stripped)
                continue
            flush()
            current_role = candidate_role
            current_voice = role_map.get(role_key) or _infer_role_voice(current_role, fv, mv)
            content = role_marker.group(2).strip()
            if content:
                current_lines.append(content)
            continue

        current_lines.append(stripped)

    flush()
    if not segments:
        clean = str(text or "").strip()
        if clean:
            segments.append((None, default_voice, clean))
    return segments


def parse_speakers(text, default_voice=None, female_voice=None, male_voice=None):
    """
    解析文本中的 w/m 说话人标识，返回 [(voice, clean_text), ...] 列表。

    处理规则:
      - "W: text" 或 "w: text" → 女声，去除 "W:" 前缀
      - "M: text" 或 "m: text" → 男声，去除 "M:" 前缀
      - "(W) text" 或 "(w) text" → 女声，去除 "(W)" 前缀
      - "(M) text" 或 "(m) text" → 男声，去除 "(M)" 前缀
      - 无标识的行 → 使用 default_voice（默认女声）
      - 连续相同说话人的行合并为一段

    default_voice: 无说话人标识时的默认音色，用于课文跟读等需要
                   按规则指定男声/女声但文本中没有 w/m 前缀的场景。
    female_voice: W/w 标识映射到的女声 ShortName，None 时用 FEMALE_VOICE。
    male_voice:   M/m 标识映射到的男声 ShortName，None 时用 MALE_VOICE。
                  传入后，男声标识将使用该音色，而非模块级常量。
    """
    return [
        (voice, clean_text)
        for _role, voice, clean_text in parse_speakers_with_roles(
            text,
            default_voice=default_voice,
            female_voice=female_voice,
            male_voice=male_voice,
        )
    ]


def default_voice_for_item(raw_item, female_voice=None, male_voice=None):
    """按统一的 Amanda/George 规则决定条目的无标识默认音色。

    单词和例句没有独立音色：即使历史解析结果带有旧的 per-item 音色字段，
    也始终回到默认女声。其他题型才使用解析器给出的男女声分配。
    """
    item = raw_item if isinstance(raw_item, dict) else {}
    fv = female_voice or FEMALE_VOICE
    mv = male_voice or MALE_VOICE
    if item.get("category") in WORD_CATEGORIES:
        return fv
    return mv if item.get("voice") == "male" else fv


# ============================================================================
# 音频生成核心
# ============================================================================

async def _synth_segment(text, voice, speed, volume, pitch):
    """合成单段文本的音频，返回 AudioSegment。

    统一使用讯飞配音 (peiyin.xunfei.cn) 生成音频，女声和男声均走同一引擎。
    voice 参数为讯飞配音发音人 key（"amanda"/"george"）。

    speed/volume/pitch: 讯飞平台三参数（0-100，50=默认）。
    """
    print(f"[tts] 使用讯飞配音生成: voice={voice} text={text[:50]}...", file=sys.stdout)
    seg = await _xunfei.synth_xunfei(
        text, voice_key=voice,
        speed=speed, volume=volume, pitch=pitch,
    )
    dur_ms = len(seg)
    print(f"[tts] 讯飞配音解码完成: duration={dur_ms}ms channels={seg.channels} sample_rate={seg.frame_rate}", file=sys.stdout)
    if dur_ms < 50:
        raise RuntimeError(f"解码后音频时长过短 ({dur_ms}ms)")
    if seg.channels == 0 or seg.frame_rate == 0:
        raise RuntimeError(f"解码后音频参数异常 (channels={seg.channels}, frame_rate={seg.frame_rate})")
    return seg


async def _synth_item(text, rate, volume, pitch, default_voice=None,
                      female_voice=None, male_voice=None, voice_configs=None,
                      role_voices=None):
    """
    为一条解析结果生成完整音频。
    自动处理 W/M 与通用角色切换，并按讯飞原始音频顺序直接拼接。

    rate/volume/pitch: 讯飞平台三参数（0-100，50=默认）
    default_voice: 无 w/m 标识时的默认音色，None 表示女声
    female_voice: W/w 标识使用的女声发音人 key，None 时用 FEMALE_VOICE。
    male_voice:   M/m 标识使用的男声发音人 key，None 时用 MALE_VOICE。
    voice_configs: 每个音色独立的 rate/volume/pitch 配置。
    role_voices: 角色名到音色 key 的映射。
    """
    segments = parse_speakers_with_roles(
        text,
        default_voice=default_voice,
        female_voice=female_voice,
        male_voice=male_voice,
        role_voices=role_voices,
    )
    if not segments:
        raise ValueError("文本为空")

    base_params = {
        "rate": clamp_tts_param(rate),
        "volume": clamp_tts_param(volume),
        "pitch": clamp_tts_param(pitch),
    }
    configs = voice_configs if isinstance(voice_configs, dict) else {}

    audio_parts = []
    for _role, voice, seg_text in segments:
        params = _normalize_voice_params(configs.get(voice), base_params)
        # 不切割首尾，也不额外插入或归一化段落停顿；保留讯飞返回的音频内容。
        part = await _synth_segment(
            seg_text,
            voice,
            params["rate"],
            params["volume"],
            params["pitch"],
        )
        audio_parts.append(part)

    if not audio_parts:
        raise RuntimeError("合成失败，未生成任何音频")

    full = audio_parts[0]
    for seg in audio_parts[1:]:
        full = full + seg
    return full


def generate_item_audio(text, rate, volume, pitch, default_voice=None,
                        female_voice=None, male_voice=None):
    """同步包装：为一条文本生成音频。"""
    return asyncio.run(_synth_item(text, rate, volume, pitch,
                                   default_voice=default_voice,
                                   female_voice=female_voice, male_voice=male_voice))


# ============================================================================
# 进度记录与断点续传
# ============================================================================

def get_session_dir(source_filename):
    """根据源文件名获取会话目录路径。"""
    dirname = sanitize_dirname(source_filename)
    return os.path.join(OUTPUT_BASE, dirname)


def load_progress(session_dir):
    """加载进度文件，返回进度字典或 None。"""
    progress_path = os.path.join(session_dir, "progress.json")
    if os.path.exists(progress_path):
        try:
            with open(progress_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 校验必需的字段是否存在
            required = ("items", "completed", "total_items", "config", "status")
            if all(k in data for k in required):
                return data
            return None
        except Exception:
            return None
    return None


def save_progress(session_dir, progress):
    """保存进度文件（原子写入：先写临时文件再 rename，防止中断时损坏）。"""
    progress_path = os.path.join(session_dir, "progress.json")
    tmp_path = os.path.join(session_dir, "progress.json.tmp")
    progress["updated_at"] = datetime.now().isoformat()
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, progress_path)
    except (OSError, ValueError, TypeError):
        # 磁盘满、权限错误或序列化失败时，清理临时文件但不中断处理
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _category_to_prefix(category):
    """将 category 转为文件名前缀，去除"录音稿"后缀。"""
    if not category:
        return "audio"
    # 去掉"录音稿"后缀
    prefix = category.replace("录音稿", "").strip()
    # 处理"模仿朗读-外网" → "模仿朗读_外网"
    prefix = prefix.replace("-", "_")
    # 清理不安全字符
    prefix = re.sub(r'[\\/:*?"<>|]', '_', prefix)
    return prefix if prefix else "audio"


def _sanitize_filename_stem(value):
    """清理解析器指定的文件名主体；空值表示仍使用默认命名规则。"""
    stem = str(value or "").strip()
    stem = re.sub(r'[\\/:*?"<>|]', '_', stem).strip(' .')
    return stem[:120]


def _unique_filename_stem(stem, used_stems):
    """以大小写不敏感方式避免同一批任务中的文件名冲突。"""
    candidate = stem
    suffix = 2
    while candidate.casefold() in used_stems:
        candidate = f"{stem}_{suffix}"
        suffix += 1
    used_stems.add(candidate.casefold())
    return candidate


def build_progress(source_filename, source_path, parse_results, config):
    """
    构建初始进度数据结构。
    每条解析结果（每道题）独立生成一个音频文件。

    文件命名规则：
      - 信息获取题目（听选信息题目/回答问题题目）：问题x.mp3（x 为题号）
      - 其他题型：题型-录音稿x.mp3（x 为同题型内的顺序号）
    """
    config = {
        **config,
        "audio_algorithm_version": AUDIO_ALGORITHM_VERSION,
        "parser_version": PARSER_VERSION,
    }
    ext = FORMAT_MAP[config['format']][1].lstrip('.')
    items = []
    # 每个子题型独立编号
    seq_by_cat = {}
    used_filename_stems = set()

    for result in parse_results:
        doc_type = result["doc_type"]
        raw_items = result["items"]

        for raw_item in raw_items:
            cat = raw_item.get("category", "")
            prefix = _category_to_prefix(cat)
            seq_by_cat[prefix] = seq_by_cat.get(prefix, 0) + 1
            default_seq = seq_by_cat[prefix]
            requested_stem = _sanitize_filename_stem(raw_item.get("filename_stem"))
            if requested_stem:
                filename_stem = _unique_filename_stem(requested_stem, used_filename_stems)
                try:
                    seq = int(raw_item.get("number"))
                except (TypeError, ValueError):
                    seq = default_seq
                item_id = f"{prefix}_{filename_stem}"
            else:
                # 其他题型：题型-录音稿x
                seq = default_seq
                filename_stem = _unique_filename_stem(
                    f"{prefix}-录音稿{seq}", used_filename_stems
                )
                item_id = filename_stem
            text_preview = raw_item.get("text", "")[:80].replace('\n', ' ')
            # 解析器只负责题型音色与文件命名；三项声音参数统一由当前配置提供。
            voice_override = raw_item.get("voice") or None      # "male" / "female" / None
            items.append({
                "id": item_id,
                "doc_type": doc_type,
                "category": cat,
                "seq": seq,
                "filename": f"{filename_stem}.{ext}",
                "status": "pending",
                "output_path": None,
                "error": None,
                "text_preview": text_preview,
                "merged": False,
                "merged_count": 1,
                "raw_item": raw_item,
                "voice_override": voice_override,
            })

    return {
        "source_file": source_filename,
        "source_path": source_path,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "status": "parsing",
        "config": config,
        "parse_results": parse_results,
        "total_items": len(items),
        "completed": 0,
        "failed": 0,
        "items": items,
    }


def get_completed_file_list(progress):
    """从进度数据中获取已完成的文件列表。"""
    files = []
    for item in progress.get("items", []):
        if item["status"] == "done" and item["output_path"]:
            raw_item = item.get("raw_item", {})
            files.append({
                "id": item["id"],
                "filename": item["filename"],
                "path": item["output_path"],
                "doc_type": item["doc_type"],
                "category": item["category"],
                "text": raw_item.get("text", ""),
                "text_preview": item.get("text_preview", raw_item.get("text", "")[:80]),
            })
    return files


# ============================================================================
# ZIP 打包
# ============================================================================

def create_zip(session_dir, progress):
    """创建包含所有音频和 JSON 的 ZIP 包。"""
    zip_path = os.path.join(session_dir, "output.zip")

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 添加所有已完成的音频文件
        for item in progress["items"]:
            if item["status"] == "done" and item["output_path"] and os.path.exists(item["output_path"]):
                arcname = "audio/" + item["filename"]
                zf.write(item["output_path"], arcname)

        # 添加解析结果 JSON
        parsed_path = os.path.join(session_dir, "parsed.json")
        if os.path.exists(parsed_path):
            zf.write(parsed_path, "parsed.json")

        # 添加进度/清单 JSON
        manifest = {
            "source_file": progress["source_file"],
            "created_at": progress["created_at"],
            "completed": progress["completed"],
            "failed": progress["failed"],
            "total_items": progress["total_items"],
            "config": progress["config"],
            "files": [
                {"filename": item["filename"], "doc_type": item["doc_type"],
                 "category": item["category"], "status": item["status"],
                 "merged": item.get("merged", False),
                 "merged_count": item.get("merged_count", 1)}
                for item in progress["items"]
            ],
        }
        manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
        zf.writestr("manifest.json", manifest_json)

    return zip_path


# ============================================================================
# UI 构建：进度日志 HTML
# ============================================================================

def build_progress_log_html(log_entries):
    """将日志条目列表构建为 HTML。"""
    if not log_entries:
        return (
            '<div style="padding:48px 20px; text-align:center; '
            'color:var(--c-text-muted); font-size:13px; '
            'line-height:1.6;">'
            '<div style="font-size:28px; margin-bottom:12px; opacity:0.4;">"♫"</div>'
            '上传 Word 文档并点击「开始处理」'
            '</div>'
        )

    parts = ['<div style="font-family:var(--font-mono); '
             'font-size:12px; line-height:1.85; padding:4px 0;">']

    for entry in log_entries:
        time = entry.get("time", "")
        level = entry.get("level", "info")
        msg = entry.get("msg", "")

        if level == "success":
            color = "var(--c-success)"
            icon = "✓"
        elif level == "error":
            color = "var(--c-error)"
            icon = "✗"
        elif level == "warn":
            color = "var(--c-warn)"
            icon = "⚠"
        elif level == "progress":
            color = "var(--c-accent)"
            icon = "⟳"
        else:
            color = "var(--c-text-sub)"
            icon = "•"

        safe_msg = _html.escape(msg)
        parts.append(
            f'<div style="padding:2px 0;">'
            f'<span style="color:var(--c-text-muted);">{time}</span> '
            f'<span style="color:{color}; font-weight:600;">{icon}</span> '
            f'<span style="color:var(--c-text);">{safe_msg}</span>'
            f'</div>'
        )

    parts.append('</div>')
    return ''.join(parts)


def _dl_button(path, filename, label="下载"):
    """
    生成下载按钮 HTML（纯 inline onclick，不依赖外部 <script>）。
    路径通过 json.dumps 转为 JS 安全字符串，再对 HTML 属性做引号转义。
    pywebview 模式下调用原生保存对话框；浏览器模式下使用 <a download>。
    """
    js_path = json.dumps(path)
    js_name = json.dumps(filename)
    js_code = (
        f"try{{window.pywebview.api.save_file({js_path},{js_name})}}"
        f"catch(e){{var a=document.createElement('a');"
        f"a.href='/file='+encodeURIComponent({js_path});"
        f"a.download={js_name};"
        f"document.body.appendChild(a);a.click();a.remove()}}"
    )
    # 单引号 HTML 属性中，需要把 JS 代码里的 ' 转义为 &#39;
    js_escaped = js_code.replace("'", "&#39;")
    return f'<button class="dl-btn" onclick=\'{js_escaped}\'>{_html.escape(label)}</button>'


def build_download_html(progress, file_list, zip_path=None):
    """构建下载区域 HTML。"""
    if not progress or progress["completed"] == 0:
        return (
            '<div style="padding:48px 20px; text-align:center; '
            'color:var(--c-text-muted); font-size:13px; '
            'line-height:1.6;">'
            '<div style="font-size:28px; margin-bottom:12px; opacity:0.4;">↓</div>'
            '处理完成后可在此下载'
            '</div>'
        )

    parts = ['<div style="padding:16px 0;">']

    # 统计信息
    total = progress["total_items"]
    done = progress["completed"]
    failed = progress["failed"]
    parts.append(
        f'<div style="margin-bottom:16px; padding:12px 16px; '
        f'background:var(--c-accent-bg); border-radius:var(--r-md); '
        f'font-size:13px; color:var(--c-text);">'
        f'<strong>完成进度：{done}/{total}</strong>'
        + (f'  |  失败：{failed}' if failed > 0 else '')
        + '</div>'
    )

    # ZIP 下载按钮
    if zip_path and os.path.exists(zip_path):
        zip_name = os.path.basename(zip_path)
        parts.append(
            f'<div style="margin-bottom:16px;">'
            f'{_dl_button(zip_path, zip_name, "下载全部（ZIP）")}'
            f'</div>'
        )

    # 文件列表
    parts.append('<div style="margin-bottom:16px;">')
    parts.append('<div style="font-size:10.5px; font-weight:600; '
                 'color:var(--c-text-muted); text-transform:uppercase; '
                 'letter-spacing:0.08em; margin-bottom:8px;">'
                 '文件列表</div>')

    for f in file_list:
        color = TYPE_COLORS.get(f["doc_type"], "var(--c-text-muted)")
        parts.append(
            f'<div style="display:flex; align-items:center; gap:8px; '
            f'padding:6px 0; border-bottom:1px solid var(--c-border-light);">'
            f'<span style="display:inline-block; width:6px; height:6px; '
            f'border-radius:50%; background:{color}; flex-shrink:0;"></span>'
            f'<span style="font-size:12px; color:var(--c-text); '
            f'flex:1; font-family:var(--font-mono);">{_html.escape(f["filename"])}</span>'
            f'<span style="font-size:11px; color:var(--c-text-muted); margin-right:4px;">'
            f'{_html.escape(f["category"])}</span>'
            f'{_dl_button(f["path"], f["filename"], "下载")}'
            f'</div>'
        )

    parts.append('</div>')
    parts.append('</div>')
    return ''.join(parts)


def build_stats_bar(progress):
    """构建状态栏统计。"""
    if not progress:
        return '<div class="stats-wrap"><span class="stat-pill"><span>等待处理</span></span></div>'

    total = progress["total_items"]
    done = progress["completed"]
    failed = progress["failed"]

    parts = ['<div class="stats-wrap">']

    # 按题型统计
    type_counts = {}
    for item in progress["items"]:
        dt = item["doc_type"]
        if dt not in type_counts:
            type_counts[dt] = {"done": 0, "total": 0}
        type_counts[dt]["total"] += 1
        if item["status"] == "done":
            type_counts[dt]["done"] += 1

    for dt, counts in type_counts.items():
        color = TYPE_COLORS.get(dt, "var(--c-text-muted)")
        parts.append(
            f'<span class="stat-pill">'
            f'<span class="stat-dot" style="background:{color}"></span>'
            f'<span>{_html.escape(dt)} <span class="stat-count">{counts["done"]}/{counts["total"]}</span></span>'
            f'</span>'
        )

    parts.append(
        f'<span class="stat-pill">'
        f'<span>共 <span class="stat-count">{done}/{total}</span></span>'
        f'</span>'
    )
    if failed > 0:
        parts.append(
            f'<span class="stat-pill" style="border-color:var(--c-error);">'
            f'<span style="color:var(--c-error);">失败 <span class="stat-count" style="color:var(--c-error);">{failed}</span></span>'
            f'</span>'
        )
    parts.append('</div>')
    return ''.join(parts)


def get_supported_types_html():
    parts = ['<div class="types-note">']
    items = []
    for doc_type in PARSER_MAP:
        items.append(f'<span class="type-tag">{doc_type}</span>')
    parts.append('、'.join(items))
    parts.append('</div>')
    return ''.join(parts)


# ============================================================================
# 核心处理流程（生成器，支持流式进度更新）
# ============================================================================

def process_document(file_obj, rate, volume, pitch, fmt, quality, preview):
    """
    主处理函数（生成器）：
    1. 解析 Word 文档
    2. 逐条生成音频
    3. 打包 ZIP
    4. 流式输出进度

    Yields: (log_html, download_html, stats_html, status_text, zip_file, file_dropdown, single_file, current_file_path)

    音色规则（讯飞配音）：
      - 全部题型统一：w/W → 女声 Amanda，m/M → 男声 George，无标识默认女声
      - 词汇题型同样使用默认女声（无单独单词音色）
    """
    filepath = _get_filepath(file_obj)
    if not filepath:
        raise gr.Error("请先上传文档（.docx 或 .xlsx）")
    if not (filepath.lower().endswith('.docx') or filepath.lower().endswith('.xlsx')):
        raise gr.Error("仅支持 .docx 或 .xlsx 格式")
    if not os.path.exists(filepath):
        raise gr.Error(f"文件不存在: {filepath}")

    source_filename = os.path.basename(filepath)
    session_dir = get_session_dir(source_filename)
    audio_dir = os.path.join(session_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    # 音色由题型自动决定，不再从前端读取；三项平台参数统一收敛为 0-100。
    fv = FEMALE_VOICE
    mv = MALE_VOICE
    config = normalize_tts_config({
        "rate": rate,
        "volume": volume,
        "pitch": pitch,
        "format": fmt,
        "quality": quality,
        "preview": preview,
    })
    rate = config["rate"]
    volume = config["volume"]
    pitch = config["pitch"]
    fmt = config["format"]
    quality = config["quality"]
    preview = config["preview"]
    config.update({
        "audio_algorithm_version": AUDIO_ALGORITHM_VERSION,
        "parser_version": PARSER_VERSION,
    })

    log_entries = []

    # ---- 检查是否有已有进度（断点续传）----
    existing = load_progress(session_dir)
    if existing and existing.get("items"):
        # 检查进度文件版本（旧版没有 raw_item 字段，需要重新解析）
        has_raw_item = any("raw_item" in i for i in existing.get("items", []))
        # 检查配置是否一致
        old_config = existing.get("config", {})
        config_changed = (
            old_config.get("config_version") != TTS_CONFIG_VERSION
            or old_config.get("rate") != rate
            or old_config.get("volume") != volume
            or old_config.get("pitch") != pitch
            or old_config.get("format") != fmt
            or old_config.get("quality") != quality
            or old_config.get("preview") != preview
            or old_config.get("audio_algorithm_version") != AUDIO_ALGORITHM_VERSION
            or old_config.get("parser_version") != PARSER_VERSION
        )

        if config_changed or not has_raw_item:
            reason = "配置已变更" if config_changed else "进度文件版本过旧"
            log_entries.append({
                "time": now_str(), "level": "warn",
                "msg": f"检测到已有进度但{reason}，重新开始处理"
            })
            progress = None
        else:
            progress = existing
            done = progress["completed"]
            total = progress["total_items"]
            log_entries.append({
                "time": now_str(), "level": "info",
                "msg": f"检测到已有进度（{done}/{total} 已完成），继续处理..."
            })
            yield (
                build_progress_log_html(log_entries),
                build_download_html(progress, get_completed_file_list(progress),
                                    zip_path=os.path.join(session_dir, "output.zip")
                                    if progress["status"] == "done" else None),
                build_stats_bar(progress),
                f"断点续传中 — {done}/{total} 已完成",
                gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
            )
    else:
        progress = None

    # ---- 如果没有进度或配置变更，重新解析 ----
    if progress is None:
        log_entries.append({
            "time": now_str(), "level": "info",
            "msg": f"开始解析文档: {source_filename}"
        })
        yield (
            build_progress_log_html(log_entries),
            build_download_html(None, []),
            build_stats_bar(None),
            "正在解析文档...",
            gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
        )

        try:
            parse_results, summary = parse_document_auto(filepath)
        except Exception as e:
            log_entries.append({
                "time": now_str(), "level": "error",
                "msg": f"解析失败: {e}"
            })
            yield (
                build_progress_log_html(log_entries),
                build_download_html(None, []),
                build_stats_bar(None),
                f"解析失败: {e}",
                gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
            )
            return

        if not parse_results:
            log_entries.append({
                "time": now_str(), "level": "error",
                "msg": f"未识别到任何题型内容: {summary}"
            })
            yield (
                build_progress_log_html(log_entries),
                build_download_html(None, []),
                build_stats_bar(None),
                summary or "未识别到任何题型内容",
                gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
            )
            return

        # 保存解析结果 JSON
        parsed_path = os.path.join(session_dir, "parsed.json")
        with open(parsed_path, 'w', encoding='utf-8') as f:
            json.dump(parse_results, f, ensure_ascii=False, indent=2)

        type_names = "、".join(r["doc_type"] for r in parse_results)

        # 构建进度数据（每条解析结果独立生成一个音频）
        progress = build_progress(source_filename, filepath, parse_results, config)

        # 试听模式：只处理前 3 条
        if preview and progress["total_items"] > 3:
            original_total = progress["total_items"]
            progress["items"] = progress["items"][:3]
            progress["total_items"] = 3
            log_entries.append({
                "time": now_str(), "level": "warn",
                "msg": f"试听模式：仅生成前 3 条（共 {original_total} 条）"
            })

        save_progress(session_dir, progress)

        audio_total = progress["total_items"]
        count_info = f"共 {audio_total} 个音频"
        log_entries.append({
            "time": now_str(), "level": "success",
            "msg": f"解析完成 — {summary} | 题型：{type_names} | {count_info}"
        })

    # ---- 开始生成音频 ----
    progress["status"] = "generating"
    save_progress(session_dir, progress)

    total = progress["total_items"]
    log_entries.append({
        "time": now_str(), "level": "info",
        "msg": f"开始生成音频（{progress['completed']}/{total}）..."
    })
    # ---- 讯飞配音会话登录（所有音频都通过讯飞配音生成）----
    if _XUNFEI_AVAILABLE:
        try:
            asyncio.run(_xunfei.ensure_session(voice_key=FEMALE_VOICE))
            log_entries.append({
                "time": now_str(), "level": "success",
                "msg": "讯飞配音登录成功，开始生成音频"
            })
        except Exception as login_err:
            log_entries.append({
                "time": now_str(), "level": "error",
                "msg": f"讯飞配音登录失败: {login_err}"
            })
            raise RuntimeError(f"讯飞配音登录失败: {login_err}")
        yield (
            build_progress_log_html(log_entries),
            build_download_html(progress, get_completed_file_list(progress)),
            build_stats_bar(progress),
            f"生成中 — {progress['completed']}/{total}",
            gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
        )
    else:
        raise RuntimeError("讯飞配音引擎不可用（缺少 playwright），无法生成音频")

    try:
        # ---- 逐条生成音频 ----
        for item in progress["items"]:
            if item["status"] == "done":
                continue

            item_id = item["id"]
            raw_item = item["raw_item"]
            text = raw_item.get("text", "")
            if not text.strip():
                item["status"] = "error"
                item["error"] = "文本为空"
                progress["failed"] += 1
                log_entries.append({
                    "time": now_str(), "level": "warn",
                    "msg": f"{item_id} — 文本为空，跳过"
                })
                save_progress(session_dir, progress)
                yield (
                    build_progress_log_html(log_entries),
                    build_download_html(progress, get_completed_file_list(progress)),
                    build_stats_bar(progress),
                    f"生成中 — {progress['completed']}/{total}",
                    gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
                )
                continue

            # 词汇题型没有独立音色；其他题型按解析器的男女声规则分配。
            item_default_voice = default_voice_for_item(
                raw_item,
                female_voice=fv,
                male_voice=mv,
            )
            speakers = parse_speakers(text, default_voice=item_default_voice,
                                      female_voice=fv, male_voice=mv)
            speaker_info = ""
            if len(speakers) > 1 or speakers[0][0] != fv:
                voices_used = set(v for v, _ in speakers)
                if voices_used == {fv, mv}:
                    speaker_info = " [混合音色]"
                elif mv in voices_used:
                    speaker_info = " [男声]"
                else:
                    speaker_info = " [女声]"
            log_entries.append({
                "time": now_str(), "level": "progress",
                "msg": f"正在生成: {item_id}{speaker_info}..."
            })
            yield (
                build_progress_log_html(log_entries),
                build_download_html(progress, get_completed_file_list(progress)),
                build_stats_bar(progress),
                f"生成中 — {progress['completed']}/{total} — {item_id}",
                gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
            )

            try:
                audio_seg = generate_item_audio(
                    text, rate, volume, pitch,
                    default_voice=item_default_voice,
                    female_voice=fv, male_voice=mv,
                )
                out_path = os.path.join(audio_dir, item["filename"])
                export_audio(audio_seg, fmt, quality, out_path)

                item["status"] = "done"
                item["output_path"] = out_path
                item["error"] = None
                progress["completed"] += 1

                log_entries.append({
                    "time": now_str(), "level": "success",
                    "msg": f"{item_id} 完成 ({progress['completed']}/{total})"
                })
            except Exception as e:
                item["status"] = "error"
                item["error"] = str(e)
                progress["failed"] += 1
                log_entries.append({
                    "time": now_str(), "level": "error",
                    "msg": f"{item_id} 失败: {e}"
                })

            save_progress(session_dir, progress)

            yield (
                build_progress_log_html(log_entries),
                build_download_html(progress, get_completed_file_list(progress)),
                build_stats_bar(progress),
                f"生成中 — {progress['completed']}/{total}",
                gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
            )

        # ---- 打包 ZIP ----
        progress["status"] = "packaging"
        save_progress(session_dir, progress)

        log_entries.append({
            "time": now_str(), "level": "info",
            "msg": "正在打包 ZIP..."
        })
        yield (
            build_progress_log_html(log_entries),
            build_download_html(progress, get_completed_file_list(progress)),
            build_stats_bar(progress),
            "正在打包...",
            gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
        )

        zip_path = create_zip(session_dir, progress)

        progress["status"] = "done"
        save_progress(session_dir, progress)

        done = progress["completed"]
        failed = progress["failed"]
        log_entries.append({
            "time": now_str(), "level": "success",
            "msg": f"全部完成！成功 {done}/{total}" + (f"，失败 {failed}" if failed > 0 else "")
        })

        # 构建文件下拉列表
        file_list = get_completed_file_list(progress)
        dropdown_choices = [(f["filename"], f["path"]) for f in file_list]
        dropdown_value = file_list[0]["path"] if file_list else None

        status_text = f"完成 — 成功 {done}/{total}"
        if failed > 0:
            status_text += f"，失败 {failed}"

        has_files = bool(file_list)
        yield (
            build_progress_log_html(log_entries),
            build_download_html(progress, file_list, zip_path=zip_path if has_files else None),
            build_stats_bar(progress),
            status_text,
            gr.update(value=zip_path if has_files else None, visible=has_files),
            gr.update(choices=dropdown_choices, value=dropdown_value, visible=has_files),
            gr.update(value=dropdown_value),
            filepath,
        )
    finally:
        # 无论成功/失败/取消，都关闭讯飞配音浏览器会话
        if _XUNFEI_AVAILABLE:
            try:
                asyncio.run(_xunfei.close_session())
                log_entries.append({
                    "time": now_str(), "level": "info",
                    "msg": "讯飞配音浏览器已关闭"
                })
            except Exception as close_err:
                log_entries.append({
                    "time": now_str(), "level": "warn",
                    "msg": f"关闭讯飞配音浏览器异常: {close_err}"
                })


# ============================================================================
# 清除函数
# ============================================================================

def clear_all():
    return (
        None,  # file_input
        build_progress_log_html([]),
        build_download_html(None, []),
        build_stats_bar(None),
        "就绪",
        gr.update(value=None, visible=False),  # zip_file
        gr.update(choices=[], value=None, visible=False),
        gr.update(value=None),
        None,
        # 高级选项重置
        gr.update(value=False),  # preview
    )


# ============================================================================
# 主题与样式
# ============================================================================

CUSTOM_THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.stone,
    secondary_hue=gr.themes.colors.teal,
    neutral_hue=gr.themes.colors.stone,
    font=["-apple-system", "BlinkMacSystemFont", "Segoe UI", "system-ui", "sans-serif"],
    font_mono=["SF Mono", "JetBrains Mono", "Menlo", "Consolas", "monospace"],
    radius_size=gr.themes.sizes.radius_md,
    spacing_size=gr.themes.sizes.spacing_md,
).set(
    background_fill_primary="#faf9f7",
    background_fill_primary_dark="#0c0a09",
    background_fill_secondary="#ffffff",
    background_fill_secondary_dark="#1c1917",
    block_background_fill="transparent",
    block_background_fill_dark="transparent",
    block_border_color="transparent",
    block_border_color_dark="transparent",
    block_border_width="0px",
    block_border_width_dark="0px",
    block_radius="0px",
    body_text_color="#1c1917",
    body_text_color_dark="#f5f5f4",
    body_text_color_subdued="#57534e",
    body_text_color_subdued_dark="#a8a29e",
    block_label_text_color="#78716c",
    block_label_text_color_dark="#a8a29e",
    block_title_text_color="#1c1917",
    block_title_text_color_dark="#f5f5f4",
    input_background_fill="#ffffff",
    input_background_fill_dark="#1c1917",
    input_border_color="#e7e5e4",
    input_border_color_dark="#292524",
    input_border_width="1px",
    input_border_color_focus="#0d6560",
    input_border_color_focus_dark="#2dd4bf",
    input_radius="6px",
    input_placeholder_color="#a8a29e",
    input_placeholder_color_dark="#78716c",
    button_primary_background_fill="#1c1917",
    button_primary_background_fill_hover="#292524",
    button_primary_background_fill_dark="#f5f5f4",
    button_primary_background_fill_hover_dark="#e7e5e4",
    button_primary_text_color="#ffffff",
    button_primary_text_color_dark="#0c0a09",
    button_primary_border_color="transparent",
    button_secondary_background_fill="#ffffff",
    button_secondary_background_fill_dark="#1c1917",
    button_secondary_background_fill_hover="#f5f4f2",
    button_secondary_background_fill_hover_dark="#292524",
    button_secondary_border_color="#e7e5e4",
    button_secondary_border_color_dark="#292524",
    button_secondary_text_color="#57534e",
    button_secondary_text_color_dark="#a8a29e",
    container_radius="0px",
    shadow_drop="none",
    shadow_drop_lg="none",
    shadow_inset="none",
    shadow_spread="0px",
    shadow_spread_dark="0px",
    checkbox_label_text_color="#1c1917",
    checkbox_label_text_color_dark="#f5f5f4",
)

CUSTOM_CSS = """
/* ===== Design Tokens ===== */
:root {
    --c-bg: #faf9f7;
    --c-panel: #ffffff;
    --c-sidebar: #f5f4f2;
    --c-toolbar: var(--c-bg);
    --c-text: #1c1917;
    --c-text-sub: #57534e;
    --c-text-muted: #a8a29e;
    --c-border: #e7e5e4;
    --c-border-light: #f0eeec;
    --c-accent: #0d6560;
    --c-accent-hover: #0a4f4b;
    --c-accent-bg: #f0fdfa;
    --c-hover: #f5f4f2;
    --c-active: #ebe9e6;
    --c-scrollbar: #d6d3d1;
    --c-code-bg: #f5f4f2;
    --c-code-text: #1c1917;
    --c-success: #15803d;
    --c-error: #b91c1c;
    --c-warn: #b45309;
    --c-info: #0369a1;
    --c-btn-primary: #1c1917;
    --c-btn-primary-hover: #292524;
    --r-sm: 4px;
    --r-md: 6px;
    --r-lg: 8px;
    --t-fast: 0.1s ease;
    --t-normal: 0.15s ease;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    --font-mono: "SF Mono", "JetBrains Mono", "Menlo", "Consolas", monospace;
}
body.dark {
    --c-bg: #0c0a09;
    --c-panel: #1c1917;
    --c-sidebar: #131110;
    --c-toolbar: var(--c-bg);
    --c-text: #f5f5f4;
    --c-text-sub: #a8a29e;
    --c-text-muted: #78716c;
    --c-border: #292524;
    --c-border-light: #1c1917;
    --c-accent: #2dd4bf;
    --c-accent-hover: #5eead4;
    --c-accent-bg: rgba(45,212,191,0.08);
    --c-hover: #1c1917;
    --c-active: #292524;
    --c-scrollbar: #292524;
    --c-code-bg: #1c1917;
    --c-code-text: #f5f5f4;
    --c-success: #4ade80;
    --c-error: #f87171;
    --c-warn: #fbbf24;
    --c-info: #38bdf8;
    --c-btn-primary: #f5f5f4;
    --c-btn-primary-hover: #e7e5e4;
}

* { box-sizing: border-box !important; }

html, body {
    margin: 0 !important;
    padding: 0 !important;
    height: 100% !important;
    overflow: hidden !important;
    background: var(--c-bg) !important;
    font-family: var(--font-sans) !important;
    -webkit-font-smoothing: antialiased !important;
    -moz-osx-font-smoothing: grayscale !important;
}

.gradio-container {
    max-width: 100% !important;
    width: 100% !important;
    height: 100vh !important;
    margin: 0 !important;
    padding: 0 !important;
    background: var(--c-bg) !important;
    color: var(--c-text) !important;
    overflow: hidden !important;
    font-family: var(--font-sans) !important;
}

footer.svelte-zxu34v,
.show-api, .built-with, .settings,
button.show-api, a.built-with, button.settings,
.record, .show-api-divider {
    display: none !important;
}

main.fillable {
    width: 100% !important;
    max-width: 100% !important;
    height: 100vh !important;
    padding: 0 !important;
    background: var(--c-bg) !important;
    gap: 0 !important;
    overflow: hidden !important;
}

.wrap, .contain {
    flex-direction: column !important;
    width: 100% !important;
    height: 100% !important;
    gap: 0 !important;
    overflow: hidden !important;
}
.contain > .column {
    flex-grow: 1 !important;
    width: 100% !important;
    height: 100% !important;
    gap: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
}

.gr-block, .gr-form, .gr-group, .gr-panel,
.gradio-container .form, .gradio-container .block, .gradio-container .wrap {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
}
.column, .gradio-column {
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
    gap: 0 !important;
}

/* ===== Toolbar ===== */
#toolbar {
    flex-direction: row !important;
    align-items: center !important;
    gap: 0 !important;
    height: 48px !important;
    min-height: 48px !important;
    flex-shrink: 0 !important;
    background: transparent !important;
    border-bottom: 1px solid var(--c-border-light) !important;
    padding: 0 16px !important;
    box-sizing: border-box !important;
}
#toolbar > .form { flex: 0 0 auto !important; }
#toolbar > .form:first-child { flex: 1 1 auto !important; }

/* macOS traffic light buttons */
.window-controls {
    display: inline-flex !important;
    flex-direction: row !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 8px;
    flex-shrink: 0;
    flex-wrap: nowrap !important;
    height: 14px;
    line-height: 0;
    font-size: 0;
    -webkit-app-region: no-drag;
    margin-right: 12px;
    vertical-align: middle;
}
.win-btn {
    display: inline-block !important;
    width: 12px !important;
    height: 12px !important;
    min-width: 12px !important;
    max-width: 12px !important;
    min-height: 12px !important;
    max-height: 12px !important;
    border-radius: 50% !important;
    border: none !important;
    padding: 0 !important;
    margin: 0 !important;
    cursor: pointer;
    position: relative;
    box-sizing: border-box !important;
    vertical-align: top !important;
    line-height: 0 !important;
    font-size: 0 !important;
    overflow: hidden;
    -webkit-app-region: no-drag;
    transition: filter 0.15s;
}
.win-btn:hover { filter: brightness(0.88); }
.win-btn:active { filter: brightness(0.75); }
.win-btn .win-icon {
    opacity: 0;
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    font-size: 8px;
    line-height: 1;
    color: rgba(0,0,0,0.5);
    font-weight: 700;
    pointer-events: none;
}
.window-controls:hover .win-icon { opacity: 1; }
.win-close { background: #ff5f57; }
.win-min { background: #febc2e; }
.win-max { background: #28c840; }

.toolbar-wrap {
    display: flex;
    align-items: center;
    height: 28px;
    gap: 10px;
    padding: 0;
    flex: 1;
    flex-wrap: nowrap !important;
}

.toolbar-brand {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
    font-weight: 500;
    color: var(--c-text-sub);
    white-space: nowrap;
    user-select: none;
    letter-spacing: -0.01em;
}
.toolbar-brand .brand-mark {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: var(--r-sm);
    background: var(--c-btn-primary);
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    flex-shrink: 0;
    letter-spacing: 0;
}
body.dark .toolbar-brand .brand-mark {
    color: #0c0a09;
}
.toolbar-spacer { flex: 1; }

/* ===== Buttons ===== */
.dl-btn {
    display: inline-block;
    padding: 4px 12px;
    font-size: 11px;
    font-weight: 500;
    color: #fff;
    background: var(--c-accent);
    border: none;
    border-radius: var(--r-sm);
    cursor: pointer;
    flex-shrink: 0;
    transition: background var(--t-fast);
    font-family: var(--font-sans);
    line-height: 1.4;
}
.dl-btn:hover { background: var(--c-accent-hover); }
.dl-btn:active { background: var(--c-accent-hover); }

#start-btn {
    background: var(--c-btn-primary) !important;
    border: 1px solid var(--c-btn-primary) !important;
    color: #fff !important;
    font-size: 12px !important;
    font-weight: 500 !important;
    padding: 0 18px !important;
    border-radius: var(--r-md) !important;
    height: 30px !important;
    min-height: 30px !important;
    line-height: 1 !important;
    box-shadow: none !important;
    transition: background var(--t-fast) !important;
    flex: 0 0 auto !important;
    width: auto !important;
    letter-spacing: 0.01em;
}
body.dark #start-btn { color: #0c0a09 !important; }
#start-btn:hover {
    background: var(--c-btn-primary-hover) !important;
    border-color: var(--c-btn-primary-hover) !important;
}
#clear-btn {
    background: transparent !important;
    border: 1px solid var(--c-border) !important;
    color: var(--c-text-sub) !important;
    font-size: 12px !important;
    font-weight: 400 !important;
    padding: 0 16px !important;
    border-radius: var(--r-md) !important;
    height: 30px !important;
    min-height: 30px !important;
    line-height: 1 !important;
    box-shadow: none !important;
    transition: all var(--t-fast) !important;
    flex: 0 0 auto !important;
    width: auto !important;
}
#clear-btn:hover {
    border-color: var(--c-text-muted) !important;
    background: var(--c-hover) !important;
    color: var(--c-text) !important;
}

/* ===== Body Layout ===== */
#body {
    flex-direction: row !important;
    flex: 1 !important;
    min-height: 0 !important;
    overflow: hidden !important;
    gap: 0 !important;
}

/* ===== Sidebar ===== */
#sidebar {
    width: 300px !important;
    min-width: 300px !important;
    max-width: 300px !important;
    background: var(--c-sidebar) !important;
    border-right: 1px solid var(--c-border) !important;
    padding: 0 !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 0 !important;
    height: 100% !important;
    flex-shrink: 0 !important;
}
#sidebar::-webkit-scrollbar { width: 6px; }
#sidebar::-webkit-scrollbar-track { background: transparent; }
#sidebar::-webkit-scrollbar-thumb {
    background: var(--c-scrollbar);
    border-radius: 3px;
}
#sidebar::-webkit-scrollbar-thumb:hover {
    background: var(--c-text-muted);
}

#sidebar .gr-group, #sidebar .styler {
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
}
#sidebar-upload, #sidebar-config, #sidebar-advanced {
    border-bottom: 1px solid var(--c-border-light) !important;
}

.sidebar-section {
    padding: 14px 16px 8px;
}
.sidebar-section-title {
    font-size: 10.5px;
    font-weight: 600;
    color: var(--c-text-muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 4px;
}

/* Upload area */
#file-upload {
    min-height: 56px !important;
    max-height: 100px !important;
    padding: 0 16px 12px !important;
    margin: 0 !important;
    flex-shrink: 0 !important;
}
#file-upload .wrap, #file-upload .svelte-1uj8rng {
    min-height: 56px !important;
    max-height: 100px !important;
    padding: 0 !important;
}
#file-upload .svelte-8prmba,
#file-upload button {
    border: 1px dashed var(--c-border) !important;
    border-radius: var(--r-md) !important;
    background: var(--c-panel) !important;
    min-height: 56px !important;
    max-height: 100px !important;
    font-size: 12px !important;
    color: var(--c-text-muted) !important;
    transition: all var(--t-fast) !important;
    width: 100% !important;
    padding: 10px 12px !important;
}
#file-upload .svelte-8prmba:hover,
#file-upload button:hover {
    border-color: var(--c-accent) !important;
    background: var(--c-accent-bg) !important;
    color: var(--c-accent) !important;
}
#file-upload .or { display: none !important; }
#file-upload label { display: none !important; }

/* Config controls */
#sidebar-config {
    flex: 0 0 auto !important;
}
/* 讯飞风格参数滑块（语速/语调/音量）：滑轨 + 数字输入框 */
.config-slider-param {
    padding: 6px 16px 2px !important;
}
.config-slider-param label {
    font-size: 11.5px !important;
    color: var(--c-text-sub) !important;
    font-weight: 400 !important;
}
.config-slider-param input[type="range"] {
    height: 4px !important;
    border-radius: 2px !important;
}
.config-slider-param input[type="range"]::-webkit-slider-thumb {
    width: 14px !important;
    height: 14px !important;
    border-radius: 50% !important;
    background: var(--c-accent) !important;
    border: 2px solid var(--c-panel) !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12) !important;
    cursor: pointer;
}
.config-slider-param input[type="number"] {
    font-size: 12px !important;
    color: var(--c-text) !important;
}
.config-dropdown {
    padding: 0 16px !important;
}
.config-dropdown .svelte-1pkg2y {
    font-size: 11.5px !important;
    color: var(--c-text-sub) !important;
    font-weight: 400 !important;
}
.config-textbox {
    padding: 0 16px !important;
}
.config-textbox .svelte-1pkg2y {
    font-size: 11.5px !important;
    color: var(--c-text-sub) !important;
    font-weight: 400 !important;
}
.config-checkbox {
    padding: 4px 16px !important;
}
.config-checkbox label {
    font-size: 12px !important;
    color: var(--c-text) !important;
}
.config-checkbox input[type="checkbox"] {
    accent-color: var(--c-accent);
}
#sidebar-advanced {
    flex: 0 0 auto !important;
}

/* Types note */
#sidebar-types {
    flex: 1 !important;
}
.types-note {
    font-size: 11.5px;
    color: var(--c-text-muted);
    line-height: 1.7;
}
.types-note .type-tag {
    color: var(--c-text-sub);
    font-weight: 500;
}

/* Voice info */
.voice-info {
    font-size: 11px;
    color: var(--c-text-sub);
    line-height: 1.7;
    padding: 10px 12px;
    background: var(--c-accent-bg);
    border-radius: var(--r-md);
    margin: 8px 16px;
    border: 1px solid transparent;
}
.voice-info strong {
    color: var(--c-text);
    font-weight: 600;
    font-size: 11px;
}
.voice-info .voice-female { color: #be185d; font-weight: 600; }
.voice-info .voice-male { color: #0d6560; font-weight: 600; }

/* ===== Main Panel ===== */
#main-panel {
    flex: 1 !important;
    background: var(--c-panel) !important;
    display: flex !important;
    flex-direction: column !important;
    min-width: 0 !important;
    overflow: hidden !important;
    height: 100% !important;
    padding: 0 !important;
    min-height: 0 !important;
}
#main-panel .gr-group, #main-panel .styler {
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
}

/* Tabs */
#main-panel .tabs {
    height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
}
#main-panel .tab-wrapper {
    flex-shrink: 0 !important;
    border-bottom: 1px solid var(--c-border) !important;
    background: var(--c-panel) !important;
}
#main-panel .tab-container {
    display: flex !important;
    gap: 0 !important;
    padding: 0 20px !important;
    height: 38px !important;
}
#main-panel .tab-container button {
    font-size: 12.5px !important;
    font-weight: 400 !important;
    color: var(--c-text-muted) !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    border-radius: 0 !important;
    padding: 0 14px !important;
    height: 38px !important;
    background: transparent !important;
    transition: all var(--t-fast) !important;
    letter-spacing: 0.01em;
}
#main-panel .tab-container button:hover {
    color: var(--c-text-sub) !important;
}
#main-panel .tab-container button.selected {
    color: var(--c-accent) !important;
    border-bottom-color: var(--c-accent) !important;
    font-weight: 500 !important;
    background: transparent !important;
}
#main-panel .tabitem {
    flex: 1 !important;
    overflow: hidden !important;
    padding: 0 !important;
}
#main-panel .tabitem > .column {
    height: 100% !important;
}

/* Progress area */
#progress-area {
    height: 100% !important;
    overflow-y: auto !important;
    padding: 16px 20px !important;
    color: var(--c-text) !important;
    max-height: none !important;
    flex: 1 !important;
}
#progress-area::-webkit-scrollbar { width: 6px; }
#progress-area::-webkit-scrollbar-track { background: transparent; }
#progress-area::-webkit-scrollbar-thumb {
    background: var(--c-scrollbar);
    border-radius: 3px;
}

/* Download area */
#download-area {
    height: 100% !important;
    overflow-y: auto !important;
    padding: 20px 24px !important;
    max-height: none !important;
    flex: 1 !important;
}
#download-area::-webkit-scrollbar { width: 6px; }
#download-area::-webkit-scrollbar-track { background: transparent; }
#download-area::-webkit-scrollbar-thumb {
    background: var(--c-scrollbar);
    border-radius: 3px;
}

/* JSON preview */
#json-preview, #json-preview textarea {
    background: var(--c-panel) !important;
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    height: 100% !important;
}
#json-preview textarea {
    font-family: var(--font-mono) !important;
    font-size: 12px !important;
    line-height: 1.65 !important;
    color: var(--c-text) !important;
    min-height: 300px !important;
    border-radius: 0 !important;
    padding: 20px 24px !important;
}

/* ===== Status Bar ===== */
#statusbar {
    flex-direction: row !important;
    align-items: center !important;
    gap: 0 !important;
    height: 28px !important;
    min-height: 28px !important;
    flex-shrink: 0 !important;
    background: transparent !important;
    border-top: 1px solid var(--c-border-light) !important;
    padding: 0 16px !important;
}
#statusbar > .form { flex: 1 1 auto !important; }
#statusbar > .form:last-child { flex: 0 0 auto !important; }
#statusbar .block { padding: 0 !important; margin: 0 !important; }

#status-text, #status-text textarea, #status-text label {
    background: transparent !important;
    border: none !important;
    font-size: 11px !important;
    color: var(--c-text-muted) !important;
    padding: 0 !important;
    height: 20px !important;
    line-height: 20px !important;
    font-family: var(--font-sans) !important;
}
#status-text .input-container, #status-text .svelte-1hguek3 {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
}

.stats-wrap {
    display: flex;
    align-items: center;
    gap: 6px;
    height: 20px;
}
.stat-pill {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 8px;
    background: transparent;
    border: 1px solid var(--c-border);
    border-radius: 10px;
    font-size: 10.5px;
    color: var(--c-text-sub);
    font-weight: 400;
    line-height: 1;
}
.stat-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
}
.stat-count {
    color: var(--c-accent);
    font-weight: 600;
}

/* Single download row */
#single-download-row {
    padding: 0 0 8px 0 !important;
}
"""


# ============================================================================
# 界面
# ============================================================================

with gr.Blocks(title="Word → TTS 一体化工具") as app:

    # ===== 顶部工具栏 =====
    with gr.Row(elem_id="toolbar"):
        gr.HTML(
            '<div class="toolbar-wrap">'
            '<div class="window-controls">'
            '<button class="win-btn win-close" onclick="try{window.pywebview.api.close_app()}catch(e){window.close()}"><span class="win-icon">x</span></button>'
            '<button class="win-btn win-min" onclick="try{window.pywebview.api.minimize_window()}catch(e){}"><span class="win-icon">–</span></button>'
            '<button class="win-btn win-max" onclick="try{window.pywebview.api.toggle_maximize()}catch(e){}"><span class="win-icon">+</span></button>'
            '</div>'
            '<div class="toolbar-brand">'
            '<span class="brand-mark">W</span>'
            '<span>Word → TTS 一体化工具</span>'
            '</div>'
            '<div class="toolbar-spacer"></div>'
            '</div>'
        )
        start_btn = gr.Button("开始处理", variant="primary", elem_id="start-btn")
        clear_btn = gr.Button("清除", elem_id="clear-btn")

    # ===== 主体 =====
    with gr.Row(elem_id="body", equal_height=False):

        # ---------- 侧边栏 ----------
        with gr.Column(scale=0, min_width=300, elem_id="sidebar"):

            # 文档上传
            with gr.Group(elem_id="sidebar-upload"):
                gr.HTML('<div class="sidebar-section"><div class="sidebar-section-title">文档上传</div></div>')
                file_input = gr.File(
                    label="",
                    file_types=[".docx", ".xlsx"],
                    file_count="single",
                    type="filepath",
                    elem_id="file-upload",
                )

            # 音频配置
            with gr.Group(elem_id="sidebar-config"):
                gr.HTML('<div class="sidebar-section"><div class="sidebar-section-title">音频配置</div></div>')

                gr.HTML(
                    '<div class="voice-info-box">'
                    '<div class="voice-info-row"><span class="voice-badge voice-female">女声</span>'
                    '<span>全部题型：Amanda（讯飞英语女声）</span></div>'
                    '<div class="voice-info-row"><span class="voice-badge voice-male">男声</span>'
                    '<span>全部题型：George（讯飞英语男声）</span></div>'
                    '</div>'
                )
                with gr.Group(elem_classes="config-slider-param"):
                    rate = gr.Slider(
                        minimum=0, maximum=100, value=50, step=1,
                        label="语速",
                        info="0-100，50 为正常语速",
                    )
                with gr.Group(elem_classes="config-slider-param"):
                    pitch = gr.Slider(
                        minimum=0, maximum=100, value=50, step=1,
                        label="语调",
                        info="0-100，50 为默认音调",
                    )
                with gr.Group(elem_classes="config-slider-param"):
                    volume = gr.Slider(
                        minimum=0, maximum=100, value=50, step=1,
                        label="音量",
                        info="0-100，50 为标准音量",
                    )
                with gr.Group(elem_classes="config-dropdown"):
                    fmt = gr.Dropdown(
                        choices=["mp3", "ogg", "aac", "opus", "wav"],
                        value="mp3",
                        label="音频格式",
                        info="输出音频的容器格式",
                    )
                with gr.Group(elem_classes="config-dropdown"):
                    quality = gr.Dropdown(
                        choices=list(QUALITY_BITRATE.keys()),
                        value="128 kbps（标准）",
                        label="音频质量",
                        info="音频码率（MP3/AAC/OPUS；OGG 用默认质量、WAV 无损）",
                    )

            # 高级选项
            with gr.Group(elem_id="sidebar-advanced"):
                gr.HTML('<div class="sidebar-section"><div class="sidebar-section-title">高级选项</div></div>')

                with gr.Group(elem_classes="config-checkbox"):
                    preview = gr.Checkbox(
                        label="试听模式",
                        value=False,
                        info="仅生成前 3 条用于快速预览效果",
                    )

            # 音色说明
            gr.HTML(
                '<div class="voice-info">'
                '<strong>音色分配规则（讯飞配音）：</strong><br>'
                '<span class="voice-female">● 女声</span>：Amanda（英语女声）<br>'
                '　└ w/W 标识 → 女声<br>'
                '　└ 无标识 → 默认女声<br>'
                '　└ 词汇题型（单词/例句）→ 默认女声<br>'
                '<span class="voice-male">● 男声</span>：George（英语男声）<br>'
                '　└ m/M 标识 → 男声<br>'
                '<em style="font-size:10px; color:var(--c-text-muted);">'
                '通过讯飞配音生成，首次使用需在弹出的浏览器中扫码登录 · 生成音频时自动去除 w/m 标识</em>'
                '</div>'
            )

            # 题型说明
            with gr.Group(elem_id="sidebar-types"):
                gr.HTML(
                    '<div class="sidebar-section">'
                    '<div class="sidebar-section-title">支持题型</div>'
                    + get_supported_types_html() +
                    '</div>'
                )

        # ---------- 主面板 ----------
        with gr.Column(scale=1, min_width=400, elem_id="main-panel"):

            with gr.Tabs():
                with gr.Tab("处理进度"):
                    progress_output = gr.HTML(
                        value=build_progress_log_html([]),
                        elem_id="progress-area",
                    )

                with gr.Tab("下载"):
                    with gr.Row(elem_id="download-area"):
                        with gr.Column():
                            download_html = gr.HTML(
                                value=build_download_html(None, []),
                            )
                            # 隐藏的组件（仅用于状态管理，下载通过 HTML 按钮处理）
                            zip_file = gr.File(
                                label="下载全部（ZIP）",
                                interactive=False,
                                visible=False,
                            )
                            file_dropdown = gr.Dropdown(
                                label="选择文件下载",
                                choices=[],
                                value=None,
                                visible=False,
                                interactive=True,
                            )
                            single_file = gr.File(
                                label="",
                                interactive=False,
                                visible=False,
                            )

                with gr.Tab("JSON 数据"):
                    json_output = gr.Textbox(
                        label="",
                        lines=20,
                        max_lines=50,
                        elem_id="json-preview",
                        interactive=False,
                        show_label=False,
                    )

    # ===== 底部状态栏 =====
    with gr.Row(elem_id="statusbar"):
        status_box = gr.Textbox(
            label="",
            interactive=False,
            value="就绪",
            elem_id="status-text",
            container=False,
            show_label=False,
        )
        stats_output = gr.HTML(
            value=build_stats_bar(None),
        )

    # 隐藏状态
    current_file_path = gr.State(value=None)

    # ---------- 事件绑定 ----------

    # 文件上传时更新 JSON 预览
    def on_file_change(file_obj):
        filepath = _get_filepath(file_obj)
        if not filepath:
            return "", None
        # 检查是否有已有解析结果
        source_filename = os.path.basename(filepath)
        session_dir = get_session_dir(source_filename)
        parsed_path = os.path.join(session_dir, "parsed.json")
        if os.path.exists(parsed_path):
            try:
                with open(parsed_path, 'r', encoding='utf-8') as f:
                    return f.read(), filepath
            except Exception:
                pass
        return "", filepath

    file_input.change(
        fn=on_file_change,
        inputs=[file_input],
        outputs=[json_output, current_file_path],
    )

    # 下载通过 HTML 按钮处理，无需 file_dropdown 事件

    # 开始处理
    start_btn.click(
        fn=process_document,
        inputs=[file_input, rate, volume, pitch, fmt, quality, preview],
        outputs=[
            progress_output,    # log_html
            download_html,      # download_html
            stats_output,       # stats_html
            status_box,         # status_text
            zip_file,           # zip_file
            file_dropdown,      # file_dropdown (choices + visibility)
            single_file,        # single_file (clear)
            current_file_path,  # current_file_path
        ],
        concurrency_limit=1,
    )

    # 清除
    clear_btn.click(
        fn=clear_all,
        outputs=[
            file_input,
            progress_output,
            download_html,
            stats_output,
            status_box,
            zip_file,
            file_dropdown,
            single_file,
            current_file_path,
            preview,
        ],
    )


# ============================================================================
# 启动
# ============================================================================

if __name__ == "__main__":
    import threading
    import time

    PORT = 7862
    URL = f"http://127.0.0.1:{PORT}"

    def _run_server():
        app.launch(
            inbrowser=False,
            server_name="127.0.0.1",
            server_port=PORT,
            show_error=True,
            prevent_thread_lock=False,
            theme=CUSTOM_THEME,
            css=CUSTOM_CSS,
        )

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    # 等待服务器就绪
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(URL, timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    if getattr(sys, 'frozen', False):
        # 打包模式：使用 pywebview 创建原生窗口
        import webview

        class WindowApi:
            def __init__(self):
                self._window = None
                self._maximized = False

            def set_window(self, w):
                self._window = w

            def minimize_window(self):
                if self._window:
                    self._window.minimize()

            def toggle_maximize(self):
                if not self._window:
                    return
                try:
                    if self._maximized:
                        self._window.restore()
                        self._maximized = False
                    else:
                        try:
                            js = self._window.evaluate_js(
                                'JSON.stringify([screen.width, screen.height])'
                            )
                            if js:
                                dims = json.loads(js)
                                w, h = int(dims[0]), int(dims[1])
                                self._window.resize(w, h)
                            else:
                                self._window.resize(1920, 1080)
                        except Exception:
                            self._window.resize(1920, 1080)
                        self._maximized = True
                except Exception:
                    pass

            def close_app(self):
                if self._window:
                    self._window.destroy()
                os._exit(0)

            def save_file(self, source_path, suggested_name):
                """通过原生保存对话框下载文件。"""
                import shutil
                try:
                    result = self._window.create_file_dialog(
                        webview.SAVE_DIALOG,
                        save_filename=suggested_name,
                    )
                    if result:
                        dest = result if isinstance(result, str) else result[0]
                        shutil.copy2(source_path, dest)
                        return True
                except Exception:
                    pass
                return False

        api = WindowApi()
        window = webview.create_window(
            title="",
            url=URL,
            width=1280,
            height=860,
            min_size=(900, 600),
            frameless=True,
            easy_drag=True,
            js_api=api,
        )
        api.set_window(window)
        webview.start()
        os._exit(0)
    else:
        # 开发模式：打开浏览器
        import webbrowser
        webbrowser.open(URL)
        server_thread.join()
