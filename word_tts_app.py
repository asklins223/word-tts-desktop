#!/usr/bin/env python3
"""
Word 文档解析 + 讯飞配音音频生成 — 一体化应用
================================================
1. 上传 Word 文档 → 自动识别题型并解析为 JSON
2. 解析成功后自动开始生成音频（支持 w/m 说话人标识自动选音色）
3. 全程进度记录，支持断点续传
4. 生成完成后可下载 ZIP 包或选择单个文件下载
5. 文件命名规则：信息获取题目使用问题x；听后选择使用听后选择-录音稿x；
   听后应答使用 7上-应答-x / 9上-应答-x；
   含 Conversation x 的段落/语篇跟读使用 SA-段-Cx-y 或 SA-语-Cx-y；
   其他题型使用题型-录音稿x

引擎与音色规则（统一使用讯飞配音 peiyin.xunfei.cn）：
  - w/W 标识 → 女声 英语-Amanda
  - m/M 标识 → 男声 英语-George
  - 无标识   → 默认女声 英语-Amanda
  - 词汇题型（单词/例句）统一使用默认女声 英语-Amanda（无单独音色）
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
import inspect
import threading
import zipfile
import hashlib
import math
import uuid
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

# ---- 查找并配置 imageio-ffmpeg 自带的静态 ffmpeg ----
#
# pydub 在导入 AudioSegment 时就会扫描 PATH，并在找不到 ffmpeg 时发出
# RuntimeWarning。打包应用的 ffmpeg 位于 PyInstaller 的 _internal 目录，
# 因而必须先找到它并加入 PATH，再导入 pydub；否则即使后面已经设置了
# AudioSegment.converter，启动日志仍会出现“ffmpeg 不存在”的误导性警告。
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
    os.environ["FFMPEG_BINARY"] = _ffmpeg_path
    # 将 ffmpeg 所在目录加入 PATH，供其他模块（如 ffmpy）使用
    ff_dir = os.path.dirname(_ffmpeg_path)
    if ff_dir not in os.environ.get('PATH', ''):
        os.environ['PATH'] = ff_dir + os.pathsep + os.environ.get('PATH', '')

from pydub import AudioSegment

if _ffmpeg_path:
    AudioSegment.converter = _ffmpeg_path
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
# 女声 → 英语-Amanda；词汇题型同样使用该女声（无单独音色）
FEMALE_VOICE = "amanda"
# 男声 → 英语-George
MALE_VOICE = "george"

# 词汇题型不再使用单独音色，统一走默认女声。
WORD_CATEGORIES = frozenset({"单词", "例句"})

# 每条解析结果（每道题）最终独立导出一个音频文件；合并模式只在讯飞端
# 临时合并作品，下载后仍会按安全停顿恢复为题目级文件。

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

# 音频生成算法版本。改变讯飞音频拼接、合并切割策略或参数寻址方式时递增，
# 避免复用旧算法产物。4 是原有“单段合成后拼接”算法，保留为历史兼容版本。
LEGACY_AUDIO_ALGORITHM_VERSION = 4
AUDIO_ALGORITHM_VERSION = 8

# Electron 主进程用它确认内置后端和渲染器来自同一次构建。旧版客户端如果
# 把旧后端混进新前端，不能继续以“看似启动成功”的方式走逐条生成流程。
BACKEND_CONTRACT_VERSION = 5

# 解析器版本。解析逻辑变更（如音色分配、文件命名规则、音频边界等）时递增，
# 避免断点续传复用旧解析结果（旧结果可能缺少 voice/filename_stem 等字段）。
PARSER_VERSION = 14

# 讯飞平台三项声音参数：均为整数 0-100，50 为平台默认值。
# 女声 Amanda 默认 50/50/50，男声 George 默认 35/50/50（语速 35）。
TTS_PARAM_MIN = 0
TTS_PARAM_MAX = 100
TTS_PARAM_DEFAULT = 50
TTS_FEMALE_RATE_DEFAULT = 50
TTS_MALE_RATE_DEFAULT = 35
TTS_CONFIG_VERSION = 5
DEFAULT_FEMALE_ROLE_KEY = "__default_female__"
DEFAULT_MALE_ROLE_KEY = "__default_male__"
ROLE_CONFIG_PREFIX = "role:"

# 生成方式。composite_cut 使用讯飞多人配音作品一次提交，再按人工停顿
# 安全切割；single_segment 保留原有逐逻辑片段生成流程。
GENERATION_MODE_COMPOSITE = "composite_cut"
GENERATION_MODE_SINGLE = "single_segment"
GENERATION_MODES = (
    GENERATION_MODE_COMPOSITE,
    GENERATION_MODE_SINGLE,
)
DEFAULT_GENERATION_MODE = GENERATION_MODE_COMPOSITE

# 讯飞编辑器当前显示单次最多约 10000 字。为多人配音标记、编辑器 JSON
# 以及接口字段预留空间，不把上限顶满；拆分只发生在完整题目之间。
COMPOSITE_MAX_TEXT_LENGTH = 9000
# 合并作品同时受讯飞编辑器的行数、页面选区稳定性和切割候选数量影响。
# 超过这个条目数时拆成多个作品；只有超长任务才会因此增加提交次数，
# 普通任务仍然保持“全部文本一次生成后切割”。
COMPOSITE_MAX_ITEMS_PER_WORK = 120
COMPOSITE_BOUNDARY_MS = 2000
COMPOSITE_SILENCE_FRAME_MS = 20
COMPOSITE_SILENCE_CORE_DBFS = -50.0
COMPOSITE_SILENCE_EDGE_DBFS = -36.0
COMPOSITE_MIN_CORE_SILENCE_MS = 300
COMPOSITE_MIN_SAFE_SILENCE_MS = 450
# 讯飞页面插入的 2 秒停顿是切割定位标记；普通语句间隙通常明显短于
# 这个值。候选足够时优先使用长标记，避免第三个边界被自然停顿抢走。
COMPOSITE_MARKER_MIN_CORE_MS = 900
# 强标记应接近页面插入的 2 秒停顿。强标记数量必须与边界数一致；数量不足
# 时，只允许在候选数恰好等于边界数的情况下使用较宽松的长停顿集合；数量
# 多于边界或候选仍有歧义时宁可失败，不把自然停顿静默当成题目边界。
COMPOSITE_MARKER_STRONG_MIN_CORE_MS = 1400
COMPOSITE_MARKER_TARGET_TOLERANCE_MS = 650
COMPOSITE_EDGE_KEEP_MS = 90
COMPOSITE_EDGE_TRIM_MIN_MS = 180
# 合并作品最外层的静音不属于题目之间的人工边界。只在它确实很长时
# 才整理，避免把弱首辅音或自然尾音当成可删除的静音；内部边界仍使用
# 更短的保护间隔，以免每段音频残留约 1 秒的合并停顿。
COMPOSITE_OUTER_EDGE_KEEP_MS = 120
COMPOSITE_OUTER_EDGE_TRIM_MIN_MS = 600
COMPOSITE_MIN_OUTPUT_MS = 40


class CompositePlanError(RuntimeError):
    """多人配音合并批次无法安全构造。"""


class CompositeCutError(RuntimeError):
    """合并音频缺少可验证的安全切割边界。"""


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
    generation_mode = str(
        raw.get("generation_mode", DEFAULT_GENERATION_MODE) or ""
    ).strip()
    if generation_mode not in GENERATION_MODES:
        generation_mode = DEFAULT_GENERATION_MODE
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
    # 女声/男声的独立默认值：Amanda 50/50/50，George 35/50/50
    female_base_params = {
        "rate": TTS_FEMALE_RATE_DEFAULT,
        "volume": TTS_PARAM_DEFAULT,
        "pitch": TTS_PARAM_DEFAULT,
    }
    male_base_params = {
        "rate": TTS_MALE_RATE_DEFAULT,
        "volume": TTS_PARAM_DEFAULT,
        "pitch": TTS_PARAM_DEFAULT,
    }
    default_female_voice = _normalize_voice_key(
        raw.get("default_female_voice"), FEMALE_VOICE
    )
    default_male_voice = _normalize_voice_key(
        raw.get("default_male_voice"), MALE_VOICE
    )

    voice_configs = {}
    legacy_voice_configs = {}
    raw_voice_configs = raw.get("voice_configs")
    if isinstance(raw_voice_configs, dict):
        for key, value in raw_voice_configs.items():
            normalized_key = _normalize_voice_key(key, "")
            if normalized_key:
                legacy_voice_configs[normalized_key] = value
                # 兼容旧版按音色保存的配置：若该音色为默认男声且未显式指定 rate，则按男声默认值 35 回退
                fallback = male_base_params if normalized_key == default_male_voice else female_base_params if normalized_key == default_female_voice else base_params
                voice_configs[normalized_key] = _normalize_voice_params(value, fallback)
    voice_configs.setdefault(
        default_female_voice,
        _normalize_voice_params(None, female_base_params),
    )
    voice_configs.setdefault(
        default_male_voice,
        _normalize_voice_params(None, male_base_params),
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
                # 按角色区分默认语速：男声默认 35，女声及其他角色默认 50
                fallback = male_base_params if normalized_key == DEFAULT_MALE_ROLE_KEY else female_base_params if normalized_key == DEFAULT_FEMALE_ROLE_KEY else base_params
                role_configs[normalized_key] = _normalize_voice_params(value, fallback)
    def legacy_params_for_role(voice_key, fallback):
        # 男女默认槽位允许选择同一个音色。此时不能从已经规范化的共享
        # voice_configs 读取，否则先建立的女声 50 会覆盖男声 35；只从
        # 原始按音色配置迁移，并让各槽位使用自己的默认值。
        source = (
            legacy_voice_configs.get(voice_key)
            if default_female_voice == default_male_voice
            else voice_configs.get(voice_key)
        )
        return _normalize_voice_params(source, fallback)

    role_configs.setdefault(
        DEFAULT_FEMALE_ROLE_KEY,
        legacy_params_for_role(default_female_voice, female_base_params),
    )
    role_configs.setdefault(
        DEFAULT_MALE_ROLE_KEY,
        legacy_params_for_role(default_male_voice, male_base_params),
    )

    # 兼容旧前端的全局三参数，同时保留每个音色/角色的独立配置。
    return {
        "config_version": TTS_CONFIG_VERSION,
        "generation_mode": generation_mode,
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
    "听后选择": "#2563eb",
    "听后应答": "#7c3aed",
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


def _audio_dbfs(audio):
    """返回可比较的 dBFS；完全静音和空音频统一视为很低能量。"""
    try:
        value = float(audio.dBFS)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return -100.0
    return value if math.isfinite(value) else -100.0


def _find_composite_silence_runs(
    audio,
    *,
    frame_ms=COMPOSITE_SILENCE_FRAME_MS,
    core_dbfs=COMPOSITE_SILENCE_CORE_DBFS,
    edge_dbfs=COMPOSITE_SILENCE_EDGE_DBFS,
):
    """找出合并音频中的低能量候选区。

    先用更低的 core 阈值确认“确实静音”，再用较宽的 edge 阈值扩展候选区。
    这样切点不会落在静音刚开始或刚结束的边缘，能给首音、尾音留保护空间。
    """
    duration = len(audio)
    if duration <= 0:
        return []
    frame_ms = max(10, int(frame_ms))
    frames = []
    for start in range(0, duration, frame_ms):
        end = min(duration, start + frame_ms)
        dbfs = _audio_dbfs(audio[start:end])
        frames.append({
            "start": start,
            "end": end,
            "dbfs": dbfs,
            "core": dbfs <= core_dbfs,
            "edge": dbfs <= edge_dbfs,
        })

    runs = []
    index = 0
    while index < len(frames):
        if not frames[index]["core"]:
            index += 1
            continue
        core_start = index
        while index + 1 < len(frames) and frames[index + 1]["core"]:
            index += 1
        core_end = index

        left = core_start
        while left > 0 and frames[left - 1]["edge"]:
            left -= 1
        right = core_end
        while right + 1 < len(frames) and frames[right + 1]["edge"]:
            right += 1

        core_start_ms = frames[core_start]["start"]
        core_end_ms = frames[core_end]["end"]
        start_ms = frames[left]["start"]
        end_ms = frames[right]["end"]
        core_length = max(0, core_end_ms - core_start_ms)
        safe_length = max(0, end_ms - start_ms)
        if (
            core_length >= COMPOSITE_MIN_CORE_SILENCE_MS
            and safe_length >= COMPOSITE_MIN_SAFE_SILENCE_MS
        ):
            runs.append({
                "start": start_ms,
                "end": end_ms,
                # 扩展后的 safe 区可能因为首尾弱音而左右不对称；真正的
                # 切点固定落在 core 静音中心，避免把尾音/首音带进切点。
                "center": (core_start_ms + core_end_ms) // 2,
                "cut_position": (core_start_ms + core_end_ms) // 2,
                "core_start": core_start_ms,
                "core_end": core_end_ms,
                "core_length": core_length,
                "length": safe_length,
            })
        index += 1
    return runs


def _select_composite_silence_runs(
    audio,
    runs,
    boundary_count,
    item_lengths=None,
    diagnostics=None,
):
    """全局选择每个条目之间的安全停顿，不按比例猜测切点。

    旧实现按边界逐个贪心选择：前面某个自然停顿一旦被选中，后面的
    expected position 就会整体错位，表现为前两段正常、第三段开始音频
    对不上。现在使用全局动态规划同时选择整条有序候选链，并在候选足够
    时优先保留页面插入的长停顿标记。
    """
    if isinstance(diagnostics, dict):
        context = {
            key: diagnostics[key]
            for key in ("item_count", "detected_run_count")
            if key in diagnostics
        }
        diagnostics.clear()
        diagnostics.update(context)
        diagnostics.update({
            "boundary_count": max(0, int(boundary_count or 0)),
            "total_duration_ms": max(0, len(audio) if audio is not None else 0),
        })

    if boundary_count <= 0:
        return []
    if len(runs) < boundary_count:
        if isinstance(diagnostics, dict):
            diagnostics.update({
                "candidate_count": len(runs),
                "strategy": "insufficient_candidates",
            })
        raise CompositeCutError(
            f"合并音频只找到 {len(runs)} 个安全停顿，需要 {boundary_count} 个"
        )

    total_duration = max(1, len(audio))
    expected_positions = []
    lengths = [max(1, int(value or 0)) for value in (item_lengths or [])]
    if len(lengths) == boundary_count + 1 and sum(lengths) > 0:
        accumulated = 0
        total_length = sum(lengths)
        for value in lengths[:-1]:
            accumulated += value
            expected_positions.append(round(total_duration * accumulated / total_length))
    else:
        expected_positions = [
            round(total_duration * index / (boundary_count + 1))
            for index in range(1, boundary_count + 1)
        ]

    ordered_runs = sorted(
        (
            run for run in runs
            if run["start"] > 0 and run["end"] < total_duration
        ),
        key=lambda run: run["center"],
    )
    if len(ordered_runs) < boundary_count:
        if isinstance(diagnostics, dict):
            diagnostics.update({
                "candidate_count": len(ordered_runs),
                "strategy": "insufficient_candidates",
            })
        raise CompositeCutError(
            f"合并音频只找到 {len(ordered_runs)} 个内部安全停顿，需要 {boundary_count} 个"
        )

    long_marker_runs = [
        run for run in ordered_runs
        if float(run.get("core_length", run.get("length", 0)) or 0)
        >= COMPOSITE_MARKER_MIN_CORE_MS
    ]
    strong_marker_runs = [
        run for run in long_marker_runs
        if (
            float(run.get("core_length", run.get("length", 0)) or 0)
            >= COMPOSITE_MARKER_STRONG_MIN_CORE_MS
            and abs(
                float(run.get("core_length", run.get("length", 0)) or 0)
                - COMPOSITE_BOUNDARY_MS
            )
            <= COMPOSITE_MARKER_TARGET_TOLERANCE_MS
        )
    ]
    if isinstance(diagnostics, dict):
        diagnostics.update({
            "candidate_count": len(ordered_runs),
            "long_marker_count": len(long_marker_runs),
            "strong_marker_count": len(strong_marker_runs),
        })

    if len(strong_marker_runs) == boundary_count:
        ordered_runs = strong_marker_runs
        selection_strategy = "strong_markers"
    elif len(strong_marker_runs) > boundary_count:
        if isinstance(diagnostics, dict):
            diagnostics["strategy"] = "ambiguous_or_extra_markers"
        raise CompositeCutError(
            "人工停顿标记数量存在歧义："
            f"需要 {boundary_count} 个，检测到 {len(strong_marker_runs)} 个"
            "接近 2 秒的强标记；拒绝猜测边界"
        )
    elif len(long_marker_runs) == boundary_count:
        # 兼容音频编码后 2 秒停顿的 core 被压短的情况；候选数必须刚好
        # 等于边界数，避免把额外的自然长停顿当成定位标记。
        ordered_runs = long_marker_runs
        selection_strategy = "exact_long_markers"
    else:
        if isinstance(diagnostics, dict):
            diagnostics["strategy"] = "ambiguous_or_missing_markers"
        raise CompositeCutError(
            "人工停顿标记不足或存在歧义："
            f"需要 {boundary_count} 个，"
            f"强标记 {len(strong_marker_runs)} 个，"
            f"长停顿候选 {len(long_marker_runs)} 个，"
            f"全部安全候选 {len(ordered_runs)} 个；拒绝按自然停顿猜测"
        )

    def score(run, expected):
        core_length = float(run.get("core_length", run.get("length", 0)) or 0)
        safe_length = float(run.get("length", core_length) or core_length)
        # 页面标记不只要“长”，还应接近实际插入的 2 秒；这样正文中
        # 偶然出现的 1.5 秒长停顿不会轻易压过真正的定位标记。
        length_score = min(core_length, COMPOSITE_BOUNDARY_MS * 1.5) * 2.0
        edge_score = min(safe_length, COMPOSITE_BOUNDARY_MS * 1.5) * 0.5
        target_penalty = min(
            abs(core_length - COMPOSITE_BOUNDARY_MS),
            COMPOSITE_BOUNDARY_MS * 2,
        ) * 1.5
        distance_penalty = abs(run["center"] - expected) / total_duration * 300.0
        return length_score + edge_score - target_penalty - distance_penalty

    # states[run_index] = (累计分数, 已选择的候选索引路径)，表示当前边界
    # 选择该候选时的最优前缀。候选数量通常很小，完整保留状态能避免贪心
    # 选早了一个自然停顿后把后续边界全部推偏。
    states = []
    for boundary_index, expected in enumerate(expected_positions):
        next_states = [None] * len(ordered_runs)
        for run_index, run in enumerate(ordered_runs):
            best = None
            current_score = score(run, expected)
            if boundary_index == 0:
                best = (current_score, [run_index])
            else:
                for previous_index, previous_state in enumerate(states):
                    if previous_state is None or previous_index >= run_index:
                        continue
                    previous_run = ordered_runs[previous_index]
                    if run["center"] - previous_run["center"] < COMPOSITE_MIN_OUTPUT_MS:
                        continue
                    candidate = (
                        previous_state[0] + current_score,
                        [*previous_state[1], run_index],
                    )
                    if best is None or candidate[0] > best[0]:
                        best = candidate
            next_states[run_index] = best
        states = next_states

    candidates = [state for state in states if state is not None]
    if not candidates:
        raise CompositeCutError("安全停顿顺序不连续，拒绝按比例强行切割")
    selected_path = max(candidates, key=lambda state: state[0])[1]
    selected = [ordered_runs[index] for index in selected_path]

    if isinstance(diagnostics, dict):
        diagnostics.update({
            "strategy": selection_strategy,
            "selected_count": len(selected),
            "selected_centers": [int(run["center"]) for run in selected],
            "selected_core_lengths": [
                int(run.get("core_length", run.get("length", 0)) or 0)
                for run in selected
            ],
        })

    if any(
        right["center"] - left["center"] < COMPOSITE_MIN_OUTPUT_MS
        for left, right in zip(selected, selected[1:])
    ):
        raise CompositeCutError("候选停顿之间的音频过短，无法安全恢复题目")
    return selected


def _edge_silence_length(
    audio,
    *,
    leading,
    dbfs_threshold=COMPOSITE_SILENCE_CORE_DBFS,
):
    """返回音频首部或尾部连续的低能量长度。

    首尾整理使用更严格的 core 阈值，不把低音量尾音或弱首辅音误判成
    可删除的保护空档。
    """
    duration = len(audio)
    if duration <= 0:
        return 0
    frame_ms = max(10, COMPOSITE_SILENCE_FRAME_MS)
    starts = range(0, duration, frame_ms)
    frames = [
        _audio_dbfs(audio[start:min(duration, start + frame_ms)])
        <= dbfs_threshold
        for start in starts
    ]
    if leading:
        count = 0
        for is_silent in frames:
            if not is_silent:
                break
            count += 1
        return min(duration, count * frame_ms)
    count = 0
    for is_silent in reversed(frames):
        if not is_silent:
            break
        count += 1
    return min(duration, count * frame_ms)


def _trim_composite_edge_silence(
    audio,
    *,
    trim_leading=True,
    trim_trailing=True,
    leading_is_outer=False,
    trailing_is_outer=False,
):
    """去掉边界人工停顿残留，同时保护真实首音和尾音。

    内部切点两侧已知来自人工 break，因此可以在较短阈值下整理；合并
    作品最外层没有这个确定性，只处理明显过长的静音，并保留更长保护。
    """
    leading = _edge_silence_length(audio, leading=True)
    trailing = _edge_silence_length(audio, leading=False)
    start = 0
    end = len(audio)
    leading_min = (
        COMPOSITE_OUTER_EDGE_TRIM_MIN_MS
        if leading_is_outer
        else COMPOSITE_EDGE_TRIM_MIN_MS
    )
    leading_keep = (
        COMPOSITE_OUTER_EDGE_KEEP_MS
        if leading_is_outer
        else COMPOSITE_EDGE_KEEP_MS
    )
    trailing_min = (
        COMPOSITE_OUTER_EDGE_TRIM_MIN_MS
        if trailing_is_outer
        else COMPOSITE_EDGE_TRIM_MIN_MS
    )
    trailing_keep = (
        COMPOSITE_OUTER_EDGE_KEEP_MS
        if trailing_is_outer
        else COMPOSITE_EDGE_KEEP_MS
    )
    if trim_leading and leading >= leading_min:
        start = min(max(0, leading - leading_keep), end)
    if trim_trailing and trailing >= trailing_min:
        end = max(start, end - max(0, trailing - trailing_keep))
    trimmed = audio[start:end]
    if len(trimmed) < COMPOSITE_MIN_OUTPUT_MS:
        raise CompositeCutError("切割后得到的音频段过短，可能存在首尾边界异常")
    return trimmed


def cut_composite_audio(audio, item_count, item_lengths=None, diagnostics=None):
    """按多人配音作品中的人工停顿恢复每道题的音频。

    切点只允许落在通过双阈值静音检测的停顿中；找不到足够安全的停顿时
    抛出 CompositeCutError，由上层保留合并音频并提示用户，不按时长比例
    猜测边界。
    """
    count = int(item_count or 0)
    if audio is None or count <= 0:
        raise CompositeCutError("没有可切割的合并音频或题目数量")
    if isinstance(diagnostics, dict):
        diagnostics.clear()
        diagnostics.update({
            "item_count": count,
            "total_duration_ms": len(audio),
        })
    if count == 1:
        # 单题作品没有内部 break 可供定位，但讯飞仍可能在作品最外层
        # 留下较长首尾空档；沿用外层保护规则，避免默认合并模式下单题
        # 音频与单条生成相比出现明显的首尾停顿。
        pieces = [_trim_composite_edge_silence(
            audio,
            leading_is_outer=True,
            trailing_is_outer=True,
        )]
        if isinstance(diagnostics, dict):
            diagnostics.update({
                "strategy": "outer_edge_trim",
                "selected_count": 0,
                "piece_lengths": [len(pieces[0])],
            })
        return pieces

    runs = _find_composite_silence_runs(audio)
    if isinstance(diagnostics, dict):
        diagnostics["detected_run_count"] = len(runs)
    selected = _select_composite_silence_runs(
        audio,
        runs,
        count - 1,
        item_lengths=item_lengths,
        diagnostics=diagnostics,
    )
    cut_positions = [
        int(run.get("cut_position", run["center"]))
        for run in selected
    ]
    pieces = []
    start = 0
    for piece_index, cut_position in enumerate([*cut_positions, len(audio)]):
        piece = audio[start:cut_position]
        pieces.append(_trim_composite_edge_silence(
            piece,
            # 切点内部的这一侧是人工 break 的残留；真正作品首尾使用
            # 更高阈值，避免误伤弱首音或自然尾音。
            trim_leading=True,
            trim_trailing=True,
            leading_is_outer=piece_index == 0,
            trailing_is_outer=piece_index == count - 1,
        ))
        start = cut_position
    if len(pieces) != count:
        raise CompositeCutError(
            f"安全切割段数异常：期望 {count}，实际 {len(pieces)}"
        )
    if isinstance(diagnostics, dict):
        diagnostics["piece_lengths"] = [len(piece) for piece in pieces]
    return pieces


def format_composite_cut_diagnostics(diagnostics):
    """把合并切割诊断压缩成可读日志，不改变结构化诊断原数据。"""
    if (
        not isinstance(diagnostics, dict)
        or not diagnostics
        or not any(key in diagnostics for key in ("item_count", "strategy"))
    ):
        return ""

    def as_int(key):
        try:
            return int(diagnostics.get(key) or 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    strategy = str(diagnostics.get("strategy") or "unknown")
    boundary_count = as_int("boundary_count")
    candidate_count = as_int("candidate_count")
    long_count = as_int("long_marker_count")
    strong_count = as_int("strong_marker_count")
    selected_count = as_int("selected_count")
    centers = diagnostics.get("selected_centers")
    if isinstance(centers, (list, tuple)):
        preview = [str(value) for value in centers[:4]]
        if len(centers) > 8:
            preview.append("…")
            preview.extend(str(value) for value in centers[-4:])
        elif len(centers) > 4:
            preview.extend(str(value) for value in centers[4:])
        center_text = ",".join(preview) or "无"
    else:
        center_text = "无"
    return (
        f"切割诊断：策略={strategy}，需要边界={boundary_count}，"
        f"候选={candidate_count}，长停顿={long_count}，强标记={strong_count}，"
        f"已选={selected_count}，中心位置(ms)=[{center_text}]"
    )


def _composite_item_from_spec(spec):
    """把一道题展开为可写入进度文件的多人配音单元。"""
    raw_item_id = spec.get("item_id")
    if not isinstance(raw_item_id, (str, int)) or isinstance(raw_item_id, bool):
        raise CompositePlanError("多人配音批次的 item_id 格式异常")
    item_id = str(raw_item_id).strip()
    if not item_id:
        raise CompositePlanError("多人配音批次缺少 item_id")
    segments = build_synthesis_segments(
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
    char_count = sum(len(str(segment.get("text") or "")) for segment in segments)
    if char_count <= 0:
        raise CompositePlanError(f"{item_id} 文本为空，无法加入多人配音作品")
    if char_count > COMPOSITE_MAX_TEXT_LENGTH:
        raise CompositePlanError(
            f"{item_id} 单题文本超过讯飞多人配音安全上限 "
            f"{COMPOSITE_MAX_TEXT_LENGTH} 字，不能从题目内部强行拆分"
        )
    return {
        "item_id": item_id,
        "segments": [dict(segment) for segment in segments],
        "char_count": char_count,
    }


def _stable_composite_work_id(item_ids):
    raw = "|".join(str(item_id) for item_id in item_ids)
    return f"composite:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def build_composite_work_plan(
    item_specs,
    max_chars=COMPOSITE_MAX_TEXT_LENGTH,
    existing_plan=None,
    *,
    max_items=COMPOSITE_MAX_ITEMS_PER_WORK,
):
    """按完整题目构造多人配音作品批次。

    ``existing_plan`` 用于断点续传：已有合并作品的题目组合必须保持不变，
    避免程序中断后重新分批导致 worksId 无法复用。
    """
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_chars = COMPOSITE_MAX_TEXT_LENGTH
    try:
        max_items = max(1, int(max_items))
    except (TypeError, ValueError):
        max_items = COMPOSITE_MAX_ITEMS_PER_WORK
    raw_specs = list(item_specs or [])
    if any(not isinstance(spec, dict) for spec in raw_specs):
        raise CompositePlanError("多人配音批次包含格式异常的题目")
    specs = raw_specs
    units = [_composite_item_from_spec(spec) for spec in specs]
    unit_ids = [unit["item_id"] for unit in units]
    if len(set(unit_ids)) != len(unit_ids):
        raise CompositePlanError("多人配音批次存在重复 item_id，拒绝覆盖切割结果")
    by_id = {unit["item_id"]: unit for unit in units}
    works = []
    used_ids = set()
    stored_work_ids = set()

    def append_work(item_units, work_id=None, works_name=None):
        if not item_units:
            return
        if len(item_units) > max_items:
            raise CompositePlanError(
                f"多人配音作品条目数超过安全上限 {max_items}"
            )
        item_ids = [unit["item_id"] for unit in item_units]
        resolved_work_id = str(work_id or _stable_composite_work_id(item_ids))
        if any(work["work_id"] == resolved_work_id for work in works):
            raise CompositePlanError("多人配音断点中存在重复作品 ID")
        # 作品名在提交前写入讯飞页面，并作为漏捕获 worksId 时的对账键。
        # 从稳定 work_id 派生，进程重启/断点续传时仍保持不变。
        resolved_works_name = str(
            works_name
            or f"wordtts_{hashlib.sha1(resolved_work_id.encode('utf-8')).hexdigest()[:16]}"
        )[:25]
        works.append({
            "work_id": resolved_work_id,
            "works_name": resolved_works_name,
            "item_ids": item_ids,
            "items": item_units,
            "item_count": len(item_units),
            "char_count": sum(unit["char_count"] for unit in item_units),
            "boundary_ms": COMPOSITE_BOUNDARY_MS,
        })

    for stored_work in existing_plan or []:
        if not isinstance(stored_work, dict):
            raise CompositePlanError("断点中的多人配音作品格式异常")
        stored_work_id = str(stored_work.get("work_id") or "").strip()
        if not stored_work_id:
            raise CompositePlanError("断点中的多人配音作品缺少作品 ID")
        if stored_work_id in stored_work_ids:
            raise CompositePlanError("多人配音断点中存在重复作品 ID")
        stored_work_ids.add(stored_work_id)
        raw_stored_ids = stored_work.get("item_ids") or []
        if not isinstance(raw_stored_ids, (list, tuple)):
            raise CompositePlanError("断点中的多人配音作品 item_ids 格式异常")
        stored_ids = []
        for raw_item_id in raw_stored_ids:
            if not isinstance(raw_item_id, (str, int)) or isinstance(raw_item_id, bool):
                raise CompositePlanError("断点中的多人配音作品包含异常题目 ID")
            item_id = str(raw_item_id).strip()
            if not item_id:
                raise CompositePlanError("断点中的多人配音作品包含空题目 ID")
            stored_ids.append(item_id)
        if not stored_ids:
            raise CompositePlanError("断点中的多人配音作品缺少题目")
        if len(set(stored_ids)) != len(stored_ids):
            raise CompositePlanError("断点中的多人配音作品存在重复题目")
        present_ids = [item_id for item_id in stored_ids if item_id in by_id]
        if not present_ids:
            continue
        if len(present_ids) != len(stored_ids):
            raise CompositePlanError(
                "断点中的多人配音作品缺少原始题目，拒绝改变作品边界"
            )
        if any(item_id in used_ids for item_id in present_ids):
            raise CompositePlanError("断点中的多人配音作品存在重复题目")
        item_units = [by_id[item_id] for item_id in present_ids]
        if len(item_units) > max_items:
            raise CompositePlanError(
                f"断点中的多人配音作品超过安全条目上限 {max_items}"
            )
        if sum(unit["char_count"] for unit in item_units) > max_chars:
            raise CompositePlanError(
                "断点中的多人配音作品超过当前字数上限，拒绝改变作品边界"
            )
        append_work(item_units, stored_work_id, stored_work.get("works_name"))
        used_ids.update(present_ids)

    current = []
    current_chars = 0
    for unit in units:
        if unit["item_id"] in used_ids:
            continue
        unit_chars = unit["char_count"]
        if current and (
            current_chars + unit_chars > max_chars
            or len(current) >= max_items
        ):
            append_work(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += unit_chars
    append_work(current)

    return works


async def _synth_items_batch(
    item_specs,
    progress_callback=None,
    cancel_check=None,
):
    """批量生成多道题，讯飞端先按音色/参数分组后统一下载。

    返回 ``item_id -> {audio, error}``。每道题的多角色段落仍按照原文顺序
    拼接；单个题目失败不会丢弃同一批里已经成功生成的其它题目。
    """
    if not item_specs:
        return {}
    if not _XUNFEI_AVAILABLE or _xunfei is None:
        raise RuntimeError("讯飞配音引擎不可用（缺少 playwright）")
    if callable(cancel_check) and cancel_check():
        raise asyncio.CancelledError

    jobs = []
    item_job_ids = {}
    job_item_ids = {}
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
        resume_works_ids = (
            spec.get("xunfei_works_ids")
            if isinstance(spec.get("xunfei_works_ids"), dict)
            else {}
        )
        resume_ambiguous_names = (
            spec.get("xunfei_ambiguous_works")
            if isinstance(spec.get("xunfei_ambiguous_works"), dict)
            else {}
        )
        for segment in segment_specs:
            job_id = f"{item_id}::segment:{segment['segment_index']}"
            ids.append(job_id)
            job_item_ids[job_id] = item_id
            jobs.append({
                "job_id": job_id,
                "item_id": item_id,
                "segment_index": segment["segment_index"],
                "text": segment["text"],
                "voice_key": segment["voice_key"],
                "speed": segment["speed"],
                "pitch": segment["pitch"],
                "volume": segment["volume"],
                "works_name": (
                    f"wordtts_{hashlib.sha1(job_id.encode('utf-8')).hexdigest()[:16]}"
                ),
            })
            resume_works_id = str(resume_works_ids.get(job_id) or "").strip()
            if resume_works_id:
                jobs[-1]["resume_works_id"] = resume_works_id
            ambiguous_name = str(resume_ambiguous_names.get(job_id) or "").strip()
            if ambiguous_name and not resume_works_id:
                jobs[-1]["ambiguous_works_name"] = ambiguous_name
        item_job_ids[item_id] = ids

    batch_results = {}
    progress_consumer = None
    progress_queue = None
    progress_futures = []
    progress_futures_lock = threading.Lock()

    if callable(progress_callback):
        loop = asyncio.get_running_loop()
        progress_queue = asyncio.Queue()
        submitted_jobs = {item_id: set() for item_id in item_job_ids}
        downloaded_jobs = {item_id: set() for item_id in item_job_ids}
        saved_jobs = {item_id: {} for item_id in item_job_ids}
        submitted_works = {item_id: {} for item_id in item_job_ids}
        ambiguous_works = {item_id: set() for item_id in item_job_ids}
        ambiguous_work_names = {item_id: {} for item_id in item_job_ids}
        invalid_works = {item_id: set() for item_id in item_job_ids}
        terminal_alert_sent = {item_id: set() for item_id in item_job_ids}
        final_progress_sent = set()

        def track_work_event(item_id, job_id, event):
            works_id = str(event.get("works_id") or "").strip()
            if event.get("works_id_invalid"):
                invalid_works[item_id].add(job_id)
                submitted_works[item_id].pop(job_id, None)
                ambiguous_works[item_id].discard(job_id)
                ambiguous_work_names[item_id].pop(job_id, None)
            elif event.get("ambiguous_works_id"):
                ambiguous_works[item_id].add(job_id)
                submitted_works[item_id].pop(job_id, None)
                works_name = str(event.get("works_name") or "").strip()
                if works_name:
                    ambiguous_work_names[item_id][job_id] = works_name
            elif works_id and job_id not in ambiguous_works[item_id]:
                ambiguous_works[item_id].discard(job_id)
                ambiguous_work_names[item_id].pop(job_id, None)
                submitted_works[item_id][job_id] = works_id

        def work_progress_snapshot(item_id):
            return {
                "works_ids": dict(submitted_works[item_id]),
                "ambiguous_works_ids": sorted(ambiguous_works[item_id]),
                "ambiguous_works_names": dict(ambiguous_work_names[item_id]),
                "invalid_works_ids": sorted(invalid_works[item_id]),
            }

        async def consume_batch_progress():
            while True:
                event = await progress_queue.get()
                if event is None:
                    return
                job_id = str(event.get("job_id") or "")
                item_id = job_item_ids.get(job_id)
                if not item_id:
                    continue
                stage = str(event.get("stage") or "saved")
                track_work_event(item_id, job_id, event)
                if stage == "submitted":
                    if job_id not in submitted_jobs[item_id]:
                        submitted_jobs[item_id].add(job_id)
                        try:
                            callback_result = progress_callback({
                                "item_id": item_id,
                                "status": "submitted",
                                "completed_segments": len(submitted_jobs[item_id]),
                                "total_segments": len(item_job_ids[item_id]),
                                "segment_id": job_id,
                                **work_progress_snapshot(item_id),
                            })
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception as error:
                            _log(f"[xunfei] 题目提交进度回调异常（已忽略）: {error}")
                elif stage == "downloaded":
                    if job_id not in downloaded_jobs[item_id]:
                        downloaded_jobs[item_id].add(job_id)
                        try:
                            callback_result = progress_callback({
                                "item_id": item_id,
                                "status": "downloaded",
                                "completed_segments": len(downloaded_jobs[item_id]),
                                "total_segments": len(item_job_ids[item_id]),
                                "segment_id": job_id,
                                **work_progress_snapshot(item_id),
                            })
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception as error:
                            _log(f"[xunfei] 题目下载进度回调异常（已忽略）: {error}")
                else:
                    saved_jobs[item_id][job_id] = bool(event.get("downloaded"))
                    if (
                        (event.get("ambiguous_works_id") or event.get("works_id_invalid"))
                        and job_id not in terminal_alert_sent[item_id]
                    ):
                        terminal_alert_sent[item_id].add(job_id)
                        try:
                            callback_result = progress_callback({
                                "item_id": item_id,
                                "status": "error",
                                "completed_segments": sum(
                                    1 for downloaded in saved_jobs[item_id].values()
                                    if downloaded
                                ),
                                "total_segments": len(item_job_ids[item_id]),
                                "segment_id": job_id,
                                "error": event.get("error") or (
                                    "讯飞已确认提交但作品 ID 不确定"
                                    if event.get("ambiguous_works_id")
                                    else "讯飞作品 ID 已失效"
                                ),
                                **work_progress_snapshot(item_id),
                            })
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception as error:
                            _log(f"[tts] 题目不确定提交状态回调异常（已忽略）: {error}")
                    if (
                        len(saved_jobs[item_id]) == len(item_job_ids[item_id])
                        and item_id not in final_progress_sent
                    ):
                        final_progress_sent.add(item_id)
                        failures = [
                            job_key for job_key, downloaded in saved_jobs[item_id].items()
                            if not downloaded
                        ]
                        try:
                            callback_result = progress_callback({
                                "item_id": item_id,
                                "status": "ready" if not failures else "error",
                                "completed_segments": len(item_job_ids[item_id]) - len(failures),
                                "total_segments": len(item_job_ids[item_id]),
                                "error": event.get("error") if failures else None,
                                **work_progress_snapshot(item_id),
                            })
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception as error:
                            _log(f"[xunfei] 题目保存进度回调异常（已忽略）: {error}")

        progress_consumer = asyncio.create_task(consume_batch_progress())

        def queue_batch_progress(event):
            if not isinstance(event, dict):
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    progress_queue.put(dict(event)),
                    loop,
                )
            except RuntimeError:
                return
            with progress_futures_lock:
                progress_futures.append(future)

        try:
            batch_kwargs = {"progress_callback": queue_batch_progress}
            if cancel_check is not None:
                batch_kwargs["cancel_check"] = cancel_check
            batch_results = await _xunfei.synth_xunfei_batch(
                jobs,
                **batch_kwargs,
            )
        except Exception as error:
            cancelled_type = getattr(_xunfei, "XunfeiCancelled", None)
            if (
                cancelled_type is not None
                and isinstance(error, cancelled_type)
                and callable(cancel_check)
                and cancel_check()
            ):
                raise asyncio.CancelledError from error
            raise
        finally:
            # Sync Playwright 在专用线程中调用完所有回调后才会返回；等待
            # 已排队的跨线程 put 完成，再发送结束标记，防止最后几条进度丢失。
            with progress_futures_lock:
                queued_futures = list(progress_futures)
            if queued_futures:
                await asyncio.gather(
                    *(asyncio.wrap_future(future) for future in queued_futures),
                    return_exceptions=True,
                )
            progress_queue.put_nowait(None)
            await progress_consumer
    else:
        batch_kwargs = {}
        if cancel_check is not None:
            batch_kwargs["cancel_check"] = cancel_check
        try:
            batch_results = await _xunfei.synth_xunfei_batch(jobs, **batch_kwargs)
        except Exception as error:
            cancelled_type = getattr(_xunfei, "XunfeiCancelled", None)
            if (
                cancelled_type is not None
                and isinstance(error, cancelled_type)
                and callable(cancel_check)
                and cancel_check()
            ):
                raise asyncio.CancelledError from error
            raise

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


async def _synth_items_batch_composite(
    item_specs,
    progress_callback=None,
    *,
    work_plan=None,
    resume=None,
    debug_dir=None,
    cancel_check=None,
):
    """一次提交多人配音作品，再按安全停顿恢复为题目音频。

    这里的“批量”单位是合并作品，而不是音色组。一个作品可包含多个音色
    和各自参数；只有网页字数上限或断点计划要求时才会有多个作品。
    """
    if not item_specs:
        return {}
    if not _XUNFEI_AVAILABLE or _xunfei is None:
        raise RuntimeError("讯飞配音引擎不可用（缺少 playwright）")
    if callable(cancel_check) and cancel_check():
        raise asyncio.CancelledError

    works = build_composite_work_plan(
        item_specs,
        existing_plan=work_plan,
    )
    if not works:
        return {}
    work_by_id = {str(work["work_id"]): work for work in works}
    resume_map = resume if isinstance(resume, dict) else {}
    request_works = []
    for index, work in enumerate(works, start=1):
        request = dict(work)
        request["job_id"] = str(work["work_id"])
        request["work_index"] = index
        request["work_total"] = len(works)
        request.setdefault(
            "works_name",
            f"wordtts_composite_{index:04d}_{uuid.uuid4().hex[:8]}",
        )
        request_works.append(request)

    submitted_work_ids = set()
    downloaded_work_ids = set()
    progress_consumer = None
    progress_queue = None
    progress_futures = []
    progress_futures_lock = threading.Lock()

    async def forward_progress(event):
        if not callable(progress_callback):
            return
        payload = dict(event or {})
        work_id = str(payload.get("work_id") or payload.get("job_id") or "")
        work = work_by_id.get(work_id) or {}
        stage = str(payload.get("stage") or "saved")
        if stage == "submitted":
            submitted_work_ids.add(work_id)
            status = "submitted"
        elif stage == "downloaded":
            downloaded_work_ids.add(work_id)
            status = "downloaded"
        elif stage == "cut":
            status = "cut"
        elif stage in {"cut_error", "error", "saved"} and payload.get("error"):
            status = "error"
        else:
            status = "downloaded" if work_id in downloaded_work_ids else "submitted"
        callback_payload = {
            "work_id": work_id,
            "job_id": work_id,
            "status": status,
            "stage": stage,
            "works_id": payload.get("works_id"),
            "works_name": payload.get("works_name") or work.get("works_name"),
            "item_count": int(work.get("item_count") or 0),
            "item_ids": list(work.get("item_ids") or []),
            "total_works": len(works),
            "submitted_works": len(submitted_work_ids),
            "downloaded_works": len(downloaded_work_ids),
            "error": payload.get("error"),
        }
        if payload.get("ambiguous_works_id"):
            callback_payload["ambiguous_works_id"] = True
        if payload.get("works_id_invalid"):
            callback_payload["works_id_invalid"] = True
        if isinstance(payload.get("cut_diagnostics"), dict):
            callback_payload["cut_diagnostics"] = dict(payload["cut_diagnostics"])
        callback_result = progress_callback(callback_payload)
        if inspect.isawaitable(callback_result):
            await callback_result

    if callable(progress_callback):
        loop = asyncio.get_running_loop()
        progress_queue = asyncio.Queue()

        async def consume_work_progress():
            while True:
                event = await progress_queue.get()
                if event is None:
                    return
                try:
                    await forward_progress(event)
                except Exception as error:
                    print(
                        f"[tts] 多人配音进度回调异常（已忽略）: {error}",
                        file=sys.stdout,
                    )

        progress_consumer = asyncio.create_task(consume_work_progress())

        def queue_work_progress(event):
            if not isinstance(event, dict):
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    progress_queue.put(dict(event)),
                    loop,
                )
            except RuntimeError:
                return
            with progress_futures_lock:
                progress_futures.append(future)
    else:
        queue_work_progress = None

    try:
        composite_kwargs = {
            "progress_callback": queue_work_progress,
            "resume": resume_map,
        }
        if cancel_check is not None:
            composite_kwargs["cancel_check"] = cancel_check
        raw_results = await _xunfei.synth_xunfei_composite(
            request_works,
            **composite_kwargs,
        )
    except Exception as error:
        cancelled_type = getattr(_xunfei, "XunfeiCancelled", None)
        if (
            cancelled_type is not None
            and isinstance(error, cancelled_type)
            and callable(cancel_check)
            and cancel_check()
        ):
            raise asyncio.CancelledError from error
        raise
    finally:
        if progress_consumer is not None:
            with progress_futures_lock:
                queued_futures = list(progress_futures)
            if queued_futures:
                await asyncio.gather(
                    *(asyncio.wrap_future(future) for future in queued_futures),
                    return_exceptions=True,
                )
            progress_queue.put_nowait(None)
            await progress_consumer

    item_results = {}
    from pydub import AudioSegment

    for work in works:
        work_id = str(work["work_id"])
        raw = raw_results.get(work_id) if isinstance(raw_results, dict) else None
        audio = raw.get("audio") if isinstance(raw, dict) else None
        error = raw.get("error") if isinstance(raw, dict) else None
        if audio is None:
            message = str(error or "讯飞多人配音作品未返回音频")
            for item_id in work["item_ids"]:
                item_results[str(item_id)] = {"audio": None, "error": message}
            await forward_progress({
                "work_id": work_id,
                "stage": "error",
                "works_id": raw.get("works_id") if isinstance(raw, dict) else None,
                "ambiguous_works_id": (
                    bool(raw.get("ambiguous_works_id"))
                    if isinstance(raw, dict)
                    else False
                ),
                "works_id_invalid": (
                    bool(raw.get("works_id_invalid"))
                    if isinstance(raw, dict)
                    else False
                ),
                "works_name": (
                    raw.get("works_name") or work.get("works_name")
                    if isinstance(raw, dict)
                    else work.get("works_name")
                ),
                "error": message,
            })
            continue

        cut_diagnostics = {}
        try:
            pieces = cut_composite_audio(
                audio,
                work["item_count"],
                item_lengths=[
                    unit.get("char_count") for unit in work.get("items") or []
                ],
                diagnostics=cut_diagnostics,
            )
            if len(pieces) != len(work["item_ids"]):
                raise CompositeCutError("多人配音安全切割数量与题目数量不一致")
            for item_id, piece in zip(work["item_ids"], pieces):
                if not isinstance(piece, AudioSegment) or len(piece) < COMPOSITE_MIN_OUTPUT_MS:
                    raise CompositeCutError(f"{item_id} 切割后的音频过短")
                item_results[str(item_id)] = {"audio": piece, "error": None}
            diagnostic_text = format_composite_cut_diagnostics(cut_diagnostics)
            if diagnostic_text:
                print(
                    f"[tts] 合并作品 {work_id} {diagnostic_text}",
                    file=sys.stdout,
                )
            await forward_progress({
                "work_id": work_id,
                "stage": "cut",
                "works_id": raw.get("works_id") if isinstance(raw, dict) else None,
                "cut_item_count": len(pieces),
                "cut_diagnostics": cut_diagnostics,
            })
        except Exception as cut_error:
            message = str(cut_error)
            diagnostic_text = format_composite_cut_diagnostics(cut_diagnostics)
            if diagnostic_text:
                print(
                    f"[tts] 合并作品 {work_id} {diagnostic_text}",
                    file=sys.stdout,
                )
                message = f"{message}；{diagnostic_text}"
            if debug_dir:
                try:
                    os.makedirs(debug_dir, exist_ok=True)
                    debug_path = os.path.join(
                        debug_dir,
                        f"{re.sub(r'[^0-9A-Za-z_.-]+', '_', work_id)}.mp3",
                    )
                    await asyncio.to_thread(audio.export, debug_path, format="mp3")
                    message = f"{message}；合并音频已保留：{debug_path}"
                except Exception as save_error:
                    message = f"{message}；保留合并音频失败：{save_error}"
            for item_id in work["item_ids"]:
                item_results[str(item_id)] = {"audio": None, "error": message}
            await forward_progress({
                "work_id": work_id,
                "stage": "cut_error",
                "works_id": raw.get("works_id") if isinstance(raw, dict) else None,
                "ambiguous_works_id": (
                    bool(raw.get("ambiguous_works_id"))
                    if isinstance(raw, dict)
                    else False
                ),
                "works_id_invalid": (
                    bool(raw.get("works_id_invalid"))
                    if isinstance(raw, dict)
                    else False
                ),
                "error": message,
                "cut_diagnostics": cut_diagnostics,
            })
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
    每条解析结果（每个音频条目）独立生成一个音频文件。

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
                # single_segment 批量提交后即使下载/导出失败，也保留每个
                # 逻辑片段对应的 worksId，下一轮可以只重试下载而不重复计费。
                "xunfei_works_ids": {},
                # 页面已确认提交但 worksId 漏捕获时保存的作品名对账键。
                "xunfei_ambiguous_works": {},
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
                "voice_keys": list(item.get("voice_keys") or []),
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
