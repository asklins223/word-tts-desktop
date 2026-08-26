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

本模块由 Electron 的 FastAPI 后端导入，不再提供独立 UI 入口。
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
import asyncio
import zipfile
from datetime import datetime

# ============================================================================
# 路径与模块导入
# ============================================================================
# 统一区分只读资源目录和可写应用数据目录。
from app_paths import ensure_data_dir, resource_dir

BASE_DIR = ensure_data_dir()
_RESOURCE_DIR = resource_dir()

if _RESOURCE_DIR not in sys.path:
    sys.path.insert(0, _RESOURCE_DIR)

WORD_PARSER_DIR = os.path.join(_RESOURCE_DIR, "word_parser")

if WORD_PARSER_DIR not in sys.path:
    sys.path.insert(0, WORD_PARSER_DIR)

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

# 导出格式：讯飞音频统一落地为 MP3，保留单一格式避免不同平台产生差异。
FORMAT_MAP = {
    "mp3": ("mp3", ".mp3"),
}

QUALITY_BITRATE = {
    "48 kbps（低）": "48k",
    "128 kbps（标准）": "128k",
    "192 kbps（高）": "192k",
    "320 kbps（极高）": "320k",
}

# 音频生成算法版本。改变讯飞音频拼接策略或参数寻址方式时递增，避免
# 复用旧算法产物。
AUDIO_ALGORITHM_VERSION = 4

# 解析器版本。解析逻辑变更（如音色分配、文件命名规则等）时递增，
# 避免断点续传复用旧解析结果（旧结果可能缺少 voice/filename_stem 等字段）。
PARSER_VERSION = 8

# 讯飞平台三项声音参数：均为整数 0-100，50 为平台默认值。
TTS_PARAM_MIN = 0
TTS_PARAM_MAX = 100
TTS_PARAM_DEFAULT = 50
TTS_CONFIG_VERSION = 5
DEFAULT_FEMALE_ROLE_KEY = "__default_female__"
DEFAULT_MALE_ROLE_KEY = "__default_male__"
ROLE_CONFIG_PREFIX = "role:"


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


def normalize_role_config_key(value):
    """规范化默认角色和文档角色的参数配置 key。"""
    raw = str(value or "").strip()
    if raw in {DEFAULT_FEMALE_ROLE_KEY, DEFAULT_MALE_ROLE_KEY}:
        return raw
    if raw.startswith(ROLE_CONFIG_PREFIX):
        raw = raw[len(ROLE_CONFIG_PREFIX):]
    role_key = normalize_role_key(raw)
    return f"{ROLE_CONFIG_PREFIX}{role_key}" if role_key else ""


def role_config_key(role):
    """把解析出来的角色名映射到独立的参数配置槽位。"""
    return normalize_role_config_key(role)


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
    # 不再根据平台、旧配置或质量选项切换输出格式；历史配置中的 WAV 等
    # 值在进入当前流程时统一收敛为 MP3。
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

    # 新配置按“角色/默认槽位”保存参数，而不是按最终音色 key 保存。
    # 这样同一个音色同时被默认男声、默认女声或多个角色使用时，各自仍
    # 能保留独立的语速、语调、音量。旧版 voice_configs 仅作为迁移兜底。
    role_configs = {}
    raw_role_configs = raw.get("role_configs")
    if isinstance(raw_role_configs, dict):
        for key, value in raw_role_configs.items():
            normalized_key = normalize_role_config_key(key)
            if normalized_key:
                role_configs[normalized_key] = _normalize_voice_params(value, base_params)
    role_configs.setdefault(
        DEFAULT_FEMALE_ROLE_KEY,
        _normalize_voice_params(voice_configs.get(default_female_voice), base_params),
    )
    role_configs.setdefault(
        DEFAULT_MALE_ROLE_KEY,
        _normalize_voice_params(voice_configs.get(default_male_voice), base_params),
    )

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
        "role_configs": role_configs,
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


def export_audio(seg, fmt, quality, out_path):
    """按指定格式与码率导出音频。"""
    if len(seg) < 50:
        raise RuntimeError(f"音频时长过短 ({len(seg)}ms)，无法导出")
    fmt_id, _ext = FORMAT_MAP["mp3"]
    kwargs = {"format": fmt_id}
    br = QUALITY_BITRATE.get(quality, QUALITY_BITRATE["128 kbps（标准）"])
    kwargs["bitrate"] = br
    print(f"[export] 导出: {out_path} fmt={fmt_id} dur={len(seg)}ms bitrate={br}", file=sys.stdout)
    export_result = seg.export(out_path, **kwargs)
    if hasattr(export_result, "close"):
        export_result.close()
    out_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    print(f"[export] 完成: {out_path} ({out_size} bytes)", file=sys.stdout)
    if out_size == 0:
        raise RuntimeError(f"导出文件为空: {out_path}")

    # 讯飞批量下载阶段已经用 FFmpeg 解码过原始 MP3。这里不再复制文件并
    # 启动第二次 FFmpeg 回读，只做轻量容器头检查，避免每条音频额外产生
    # 一次磁盘 I/O 和解码进程；真正的完整回读验证保留给显式调试模式。
    if fmt_id == "mp3" and not _looks_like_mp3_file(out_path):
        raise RuntimeError(f"导出的文件不是有效 MP3: {out_path}")
    return out_path


def _looks_like_mp3_file(path):
    """用 MP3 帧头/ID3 头做快速校验，不启动 ffmpeg。"""
    try:
        with open(path, "rb") as source:
            data = source.read(4096)
    except OSError:
        return False
    if data.startswith(b"ID3"):
        return True
    # 无 ID3 标签的 MP3 可能从任意偏移直接开始 MPEG 帧。
    return any(
        data[index] == 0xFF and (data[index + 1] & 0xE0) == 0xE0
        for index in range(max(0, len(data) - 1))
    )


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
    default_role=None,
    preserve_default_roles=False,
):
    """解析 W/M 和通用角色标记，返回 ``(role, voice, clean_text)``。

    ``role`` 是用户在界面中看到的原始角色名；没有角色名的普通段落为 None。
    角色映射只决定音色；参数槽位由角色名决定，因此同一个音色被多个角色
    使用时仍能分别配置。未启用 ``preserve_default_roles`` 时保持旧的 None
    返回行为，供只需要音色解析的调用方兼容使用。
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
    current_role = default_role if preserve_default_roles else None
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
            current_role = (
                DEFAULT_FEMALE_ROLE_KEY if gender == 'W' else DEFAULT_MALE_ROLE_KEY
            ) if preserve_default_roles else None
            content = marker.group(2).strip()
            if content:
                current_lines.append(content)
            continue

        paren_marker = RE_PAREN_SPEAKER.match(stripped)
        if paren_marker:
            flush()
            gender = paren_marker.group(1).upper()
            current_voice = fv if gender == 'W' else mv
            current_role = (
                DEFAULT_FEMALE_ROLE_KEY if gender == 'W' else DEFAULT_MALE_ROLE_KEY
            ) if preserve_default_roles else None
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
            segments.append((
                current_role if preserve_default_roles else None,
                default_voice,
                clean,
            ))
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


def build_synthesis_segments(text, rate, volume, pitch, default_voice=None,
                             female_voice=None, male_voice=None,
                             voice_configs=None, role_voices=None,
                             role_configs=None, default_role=None):
    """把一道题展开为可独立分组的讯飞合成段。

    多角色题目可能同时包含多个音色；这里先保留题内顺序，再由批量引擎
    按 ``音色 + 语速 + 语调 + 音量`` 重新分组提交。调用方最后按
    ``segment_index`` 拼回原题音频，因此分组不会改变成品顺序。
    """
    segments = parse_speakers_with_roles(
        text,
        default_voice=default_voice,
        female_voice=female_voice,
        male_voice=male_voice,
        role_voices=role_voices,
        default_role=default_role,
        preserve_default_roles=isinstance(role_configs, dict) or default_role is not None,
    )
    if not segments:
        raise ValueError("文本为空")

    base_params = {
        "rate": clamp_tts_param(rate),
        "volume": clamp_tts_param(volume),
        "pitch": clamp_tts_param(pitch),
    }
    configs = voice_configs if isinstance(voice_configs, dict) else {}
    role_param_configs = role_configs if isinstance(role_configs, dict) else {}
    result = []
    for segment_index, (_role, voice, seg_text) in enumerate(segments):
        role_params = role_param_configs.get(role_config_key(_role)) if _role else None
        params = _normalize_voice_params(
            role_params if role_params is not None else configs.get(voice),
            base_params,
        )
        result.append({
            "segment_index": segment_index,
            "role": _role,
            "text": seg_text,
            "voice_key": voice,
            "speed": params["rate"],
            "pitch": params["pitch"],
            "volume": params["volume"],
        })
    return result


async def _synth_item(text, rate, volume, pitch, default_voice=None,
                      female_voice=None, male_voice=None, voice_configs=None,
                      role_voices=None, role_configs=None, default_role=None):
    """
    为一条解析结果生成完整音频。
    自动处理 W/M 与通用角色切换，并按讯飞原始音频顺序直接拼接。

    rate/volume/pitch: 讯飞平台三参数（0-100，50=默认）
    default_voice: 无 w/m 标识时的默认音色，None 表示女声
    female_voice: W/w 标识使用的女声发音人 key，None 时用 FEMALE_VOICE。
    male_voice:   M/m 标识使用的男声发音人 key，None 时用 MALE_VOICE。
    voice_configs: 旧版按音色保存的 rate/volume/pitch 配置，作为兼容兜底。
    role_configs: 按默认角色或文档角色保存的独立参数配置。
    role_voices: 角色名到音色 key 的映射。
    default_role: 没有说话人标识时使用的默认角色槽位。
    """
    segment_specs = build_synthesis_segments(
        text, rate, volume, pitch,
        default_voice=default_voice,
        female_voice=female_voice,
        male_voice=male_voice,
        voice_configs=voice_configs,
        role_voices=role_voices,
        role_configs=role_configs,
        default_role=default_role,
    )
    audio_parts = []
    for segment in segment_specs:
        # 不切割首尾，也不额外插入或归一化段落停顿；保留讯飞返回的音频内容。
        part = await _synth_segment(
            segment["text"],
            segment["voice_key"],
            segment["speed"],
            segment["volume"],
            segment["pitch"],
        )
        audio_parts.append(part)

    if not audio_parts:
        raise RuntimeError("合成失败，未生成任何音频")
    return _concat_audio_segments(audio_parts)


def _concat_audio_segments(audio_parts):
    """按原顺序拼接音频；同参数段落直接合并 raw bytes，避免反复复制。"""
    if not audio_parts:
        raise ValueError("没有可拼接的音频段")
    if len(audio_parts) == 1:
        return audio_parts[0]

    first = audio_parts[0]
    compatible = all(
        part.sample_width == first.sample_width
        and part.frame_rate == first.frame_rate
        and part.channels == first.channels
        for part in audio_parts[1:]
    )
    if compatible:
        raw_data = b"".join(part.raw_data for part in audio_parts)
        return first._spawn(raw_data)

    # 理论上不同编码参数才会走这里；保留 pydub 原生拼接作为安全兜底。
    full = first
    for part in audio_parts[1:]:
        full = full + part
    return full


async def _synth_items_batch(item_specs):
    """批量生成多道题，讯飞端先按音色/参数分组后统一下载。

    返回 ``item_id -> {audio, error}``。每道题的多角色段落仍按照原文顺序
    拼接；单个题目失败不会丢弃同一批里已经成功生成的其它题目。
    """
    if not item_specs:
        return {}
    if not _XUNFEI_AVAILABLE or _xunfei is None:
        raise RuntimeError("讯飞配音引擎不可用（缺少 playwright）")

    jobs = []
    item_job_ids = {}
    for item_index, spec in enumerate(item_specs):
        item_id = str(spec["item_id"])
        segment_specs = build_synthesis_segments(
            spec.get("text", ""),
            spec.get("rate", TTS_PARAM_DEFAULT),
            spec.get("volume", TTS_PARAM_DEFAULT),
            spec.get("pitch", TTS_PARAM_DEFAULT),
            default_voice=spec.get("default_voice"),
            female_voice=spec.get("female_voice"),
            male_voice=spec.get("male_voice"),
            voice_configs=spec.get("voice_configs"),
            role_voices=spec.get("role_voices"),
            role_configs=spec.get("role_configs"),
            default_role=spec.get("default_role"),
        )
        ids = []
        for segment in segment_specs:
            job_id = f"{item_id}::segment:{segment['segment_index']}"
            ids.append(job_id)
            jobs.append({
                "job_id": job_id,
                "item_id": item_id,
                "segment_index": segment["segment_index"],
                "text": segment["text"],
                "voice_key": segment["voice_key"],
                "speed": segment["speed"],
                "pitch": segment["pitch"],
                "volume": segment["volume"],
            })
        item_job_ids[item_id] = ids

    batch_results = await _xunfei.synth_xunfei_batch(jobs)
    item_results = {}
    for spec in item_specs:
        item_id = str(spec["item_id"])
        parts = []
        error = None
        for job_id in item_job_ids.get(item_id, []):
            result = batch_results.get(job_id) if isinstance(batch_results, dict) else None
            segment = result.get("segment") if isinstance(result, dict) else None
            if segment is None:
                error = (result or {}).get("error") if isinstance(result, dict) else None
                error = error or "讯飞批量下载未返回音频"
                break
            parts.append(segment)

        if error:
            item_results[item_id] = {"audio": None, "error": str(error)}
            continue
        if not parts:
            item_results[item_id] = {"audio": None, "error": "未生成任何音频段"}
            continue
        item_results[item_id] = {"audio": _concat_audio_segments(parts), "error": None}
    return item_results


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
            json.dump(progress, f, ensure_ascii=False, separators=(",", ":"))
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
        **normalize_tts_config(config),
        "audio_algorithm_version": AUDIO_ALGORITHM_VERSION,
        "parser_version": PARSER_VERSION,
    }
    ext = FORMAT_MAP["mp3"][1].lstrip('.')
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
