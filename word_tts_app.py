#!/usr/bin/env python3
"""
Word 文档解析 + Edge TTS 音频生成 — 一体化应用
================================================
1. 上传 Word 文档 → 自动识别题型并解析为 JSON
2. 解析成功后自动开始生成音频（支持 w/m 说话人标识自动选音色）
3. 全程进度记录，支持断点续传
4. 生成完成后可下载 ZIP 包或选择单个文件下载
5. 文件命名规则：题型_序号

音色规则：
  - w/W 标识 → 女声 en-US-JennyNeural
  - m/M 标识 → 男声 fr-FR-RemyMultilingualNeural
  - 无标识   → 默认女声 en-US-JennyNeural
  - 生成音频时自动去除 w/m 标识

用法:
    python word_tts_app.py
"""

import os
import sys
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
if getattr(sys, 'frozen', False):
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

import edge_tts
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

# ---- TTSMaker 客户端（可选模块，用于男声 788/Alfie 生成）----
try:
    import ttsmaker_client as _ttsmaker
    _TTSMaker_AVAILABLE = _ttsmaker.is_available()
except Exception:
    _TTSMaker_AVAILABLE = False
    _ttsmaker = None


# ============================================================================
# 常量配置
# ============================================================================

OUTPUT_BASE = os.path.join(BASE_DIR, "tts_output")
os.makedirs(OUTPUT_BASE, exist_ok=True)

# 音色配置
FEMALE_VOICE = "en-US-JennyNeural"
MALE_VOICE = "fr-FR-RemyMultilingualNeural"

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


def ttsmaker_to_edge_pct(multiplier):
    """将 TTSMaker 倍率 (1.5) 转换为 edge-tts 百分比 (+50)。"""
    return int(round((float(multiplier) - 1.0) * 100))


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
    seg.export(out_path, **kwargs)
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
            verify_seg = AudioSegment.from_file(_verify_tmp_path, format=fmt_id)
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


def parse_speakers(text):
    """
    解析文本中的 w/m 说话人标识，返回 [(voice, clean_text), ...] 列表。

    处理规则:
      - "W: text" 或 "w: text" → 女声，去除 "W:" 前缀
      - "M: text" 或 "m: text" → 男声，去除 "M:" 前缀
      - "(W) text" 或 "(w) text" → 女声，去除 "(W)" 前缀
      - "(M) text" 或 "(m) text" → 男声，去除 "(M)" 前缀
      - 无标识的行 → 默认女声
      - 连续相同说话人的行合并为一段
    """
    segments = []
    lines = text.strip().split('\n')

    current_voice = FEMALE_VOICE
    current_lines = []

    def flush():
        nonlocal current_lines
        if current_lines:
            clean = '\n'.join(current_lines).strip()
            if clean:
                segments.append((current_voice, clean))
            current_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 检查 W: / M: 前缀
        m = RE_LINE_SPEAKER.match(stripped)
        if m:
            flush()
            gender = m.group(1).upper()
            current_voice = FEMALE_VOICE if gender == 'W' else MALE_VOICE
            content = m.group(2).strip()
            if content:
                current_lines.append(content)
            continue

        # 检查 (W) / (M) 前缀
        m2 = RE_PAREN_SPEAKER.match(stripped)
        if m2:
            flush()
            gender = m2.group(1).upper()
            current_voice = FEMALE_VOICE if gender == 'W' else MALE_VOICE
            content = m2.group(2).strip()
            if content:
                current_lines.append(content)
            continue

        # 普通行，加入当前段
        current_lines.append(stripped)

    flush()

    # 如果没有检测到任何说话人标识，整段用默认女声
    if not segments:
        clean = text.strip()
        if clean:
            segments.append((FEMALE_VOICE, clean))

    return segments


# ============================================================================
# 音频生成核心
# ============================================================================

async def _synth_segment(text, voice, rate, volume, pitch, proxy, tmp_dir):
    """合成单段文本的音频，返回 AudioSegment。

    男声 (MALE_VOICE) 优先使用 TTSMaker 788 (Alfie) 生成；
    女声 (FEMALE_VOICE) 使用 edge-tts 生成。
    TTSMaker 不可用或失败时回退到 edge-tts。

    段落间的停顿由 _synth_item 统一用 AudioSegment.silent 插入，
    本函数只负责生成单段干净音频（不含尾部静音）。

    rate/volume/pitch: TTSMaker 格式 (float 倍率, 如 1.5)
    """
    # ---- 男声：优先使用 TTSMaker ----
    if voice == MALE_VOICE and _TTSMaker_AVAILABLE:
        try:
            print(f"[tts] 使用 TTSMaker 788 (Alfie) 生成男声: {text[:50]}...", file=sys.stdout)
            # pause=-1：让 TTSMaker 不在音频内部加段落停顿，
            # 段落间静音统一由 _synth_item 用 AudioSegment.silent 插入，
            # 避免 TTSMaker 内部停顿 + _synth_item 显式静音 = 双重停顿
            seg = await _ttsmaker.synth_male_ttsmaker(
                text, tmp_dir, voice_key="alfie",
                rate=rate, volume=volume, pitch=pitch, pause=-1,
            )
            return seg
        except Exception as e:
            print(f"[tts] TTSMaker 生成失败，回退到 edge-tts: {e}", file=sys.stdout)
            # 继续执行 edge-tts 回退逻辑

    # ---- 女声 / 回退：使用 edge-tts ----
    # 将 TTSMaker 倍率格式转换为 edge-tts 百分比格式
    edge_rate = fmt_pct(ttsmaker_to_edge_pct(rate))
    edge_volume = fmt_pct(ttsmaker_to_edge_pct(volume))
    edge_pitch = fmt_hz(ttsmaker_to_edge_pct(pitch))

    uid = uuid.uuid4().hex[:8]
    tmp_path = os.path.join(tmp_dir, f".seg_{uid}.mp3")
    try:
        communicate = edge_tts.Communicate(
            text, voice,
            rate=edge_rate,
            volume=edge_volume,
            pitch=edge_pitch,
            proxy=proxy or None,
        )
        await communicate.save(tmp_path)
        fsize = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
        print(f"[tts] edge_tts 保存完成: {tmp_path} ({fsize} bytes)", file=sys.stdout)
        if fsize < 100:
            # 小于 100 字节几乎不可能是有效音频
            raise RuntimeError(f"edge_tts 返回的音频过小 ({fsize} bytes)，可能网络异常")
        seg = AudioSegment.from_file(tmp_path, format="mp3", codec="mp3")
        dur_ms = len(seg)
        print(f"[tts] pydub 解码完成: duration={dur_ms}ms channels={seg.channels} sample_rate={seg.frame_rate}", file=sys.stdout)
        if dur_ms < 50:
            raise RuntimeError(f"解码后音频时长过短 ({dur_ms}ms)，可能 edge_tts 返回了空音频")
        if seg.channels == 0 or seg.frame_rate == 0:
            raise RuntimeError(f"解码后音频参数异常 (channels={seg.channels}, frame_rate={seg.frame_rate})")
        return seg
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def _synth_item(text, rate, volume, pitch, pause, proxy, tmp_dir):
    """
    为一条解析结果生成完整音频。
    自动处理 w/m 说话人切换，段落间插入停顿。

    rate/volume/pitch: TTSMaker 格式 (float 倍率)
    pause: TTSMaker 格式 (int ms, -1=不停顿, 0=默认300ms, N=N ms)
    """
    segments = parse_speakers(text)
    if not segments:
        raise ValueError("文本为空")

    # 将 TTSMaker pause 值转换为实际毫秒数用于插入静音
    pause_val = int(float(pause))
    if pause_val == -1:
        pause_ms = 0
    elif pause_val == 0:
        pause_ms = 300
    else:
        pause_ms = pause_val

    audio_parts = []
    for voice, seg_text in segments:
        # 段内按换行分段落
        paragraphs = [p.strip() for p in seg_text.splitlines() if p.strip()]
        for para in paragraphs:
            part = await _synth_segment(para, voice, rate, volume, pitch, proxy, tmp_dir)
            audio_parts.append(part)

    if not audio_parts:
        raise RuntimeError("合成失败，未生成任何音频")

    silence = AudioSegment.silent(duration=max(0, pause_ms))
    full = audio_parts[0]
    for seg in audio_parts[1:]:
        full = full + silence + seg
    return full


def generate_item_audio(text, rate, volume, pitch, pause, proxy, tmp_dir):
    """同步包装：为一条文本生成音频。"""
    return asyncio.run(_synth_item(text, rate, volume, pitch, pause, proxy, tmp_dir))


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


def build_progress(source_filename, source_path, parse_results, config):
    """
    构建初始进度数据结构。
    每条解析结果（每道题）独立生成一个音频文件。
    文件名使用具体子题型（如"听选信息"、"回答问题"）而非大题型。
    同一子题型内按序号编号。
    """
    ext = FORMAT_MAP[config['format']][1].lstrip('.')
    items = []
    # 每个子题型独立编号
    seq_by_cat = {}

    for result in parse_results:
        doc_type = result["doc_type"]
        raw_items = result["items"]

        for raw_item in raw_items:
            cat = raw_item.get("category", "")
            prefix = _category_to_prefix(cat)
            seq_by_cat[prefix] = seq_by_cat.get(prefix, 0) + 1
            seq = seq_by_cat[prefix]
            item_id = f"{prefix}_{seq:03d}"
            text_preview = raw_item.get("text", "")[:80].replace('\n', ' ')
            items.append({
                "id": item_id,
                "doc_type": doc_type,
                "category": cat,
                "seq": seq,
                "filename": f"{item_id}.{ext}",
                "status": "pending",
                "output_path": None,
                "error": None,
                "text_preview": text_preview,
                "merged": False,
                "merged_count": 1,
                "raw_item": raw_item,
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

def process_document(file_obj, rate, volume, pitch, pause, fmt, quality, proxy,
                     preview):
    """
    主处理函数（生成器）：
    1. 解析 Word 文档
    2. 逐条生成音频
    3. 打包 ZIP
    4. 流式输出进度

    Yields: (log_html, download_html, stats_html, status_text, zip_file, file_dropdown, single_file, current_file_path)
    """
    filepath = _get_filepath(file_obj)
    if not filepath:
        raise gr.Error("请先上传 Word 文档（.docx）")
    if not filepath.lower().endswith('.docx'):
        raise gr.Error("仅支持 .docx 格式的 Word 文档")
    if not os.path.exists(filepath):
        raise gr.Error(f"文件不存在: {filepath}")

    source_filename = os.path.basename(filepath)
    session_dir = get_session_dir(source_filename)
    audio_dir = os.path.join(session_dir, "audio")
    tmp_dir = os.path.join(session_dir, ".tmp")
    # 清理上次中断可能残留的临时目录
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(audio_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    config = {
        "rate": rate,
        "volume": volume,
        "pitch": pitch,
        "pause": pause,
        "format": fmt,
        "quality": quality,
        "proxy": proxy or "",
        "preview": preview,
    }

    log_entries = []

    # ---- 检查是否有已有进度（断点续传）----
    existing = load_progress(session_dir)
    if existing and existing.get("items"):
        # 检查进度文件版本（旧版没有 raw_item 字段，需要重新解析）
        has_raw_item = any("raw_item" in i for i in existing.get("items", []))
        # 检查配置是否一致
        old_config = existing.get("config", {})
        config_changed = (
            old_config.get("rate") != rate
            or old_config.get("volume") != volume
            or old_config.get("pitch") != pitch
            or old_config.get("pause") != pause
            or old_config.get("format") != fmt
            or old_config.get("quality") != quality
            or old_config.get("preview") != preview
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
    if _TTSMaker_AVAILABLE:
        log_entries.append({
            "time": now_str(), "level": "info",
            "msg": "男声使用 TTSMaker 788 (Alfie) 生成，女声使用 edge-tts"
        })
        log_entries.append({
            "time": now_str(), "level": "warn",
            "msg": "正在启动 TTSMaker 浏览器（首次需扫码登录，后续自动复用登录状态）"
        })
    else:
        log_entries.append({
            "time": now_str(), "level": "warn",
            "msg": "TTSMaker 不可用，男声将使用 edge-tts (Remy) 生成"
        })
    yield (
        build_progress_log_html(log_entries),
        build_download_html(progress, get_completed_file_list(progress)),
        build_stats_bar(progress),
        f"生成中 — {progress['completed']}/{total}",
        gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
    )

    # ---- 检查是否有男声数据，决定是否需要启动 TTSMaker ----
    has_male_voice = False
    if _TTSMaker_AVAILABLE:
        for item in progress["items"]:
            if item["status"] == "done":
                continue
            raw_item = item.get("raw_item", {})
            text = raw_item.get("text", "")
            if text.strip():
                speakers = parse_speakers(text)
                if any(v == MALE_VOICE for v, _ in speakers):
                    has_male_voice = True
                    break

    # ---- TTSMaker 登录（仅有男声数据时才唤起浏览器）----
    if _TTSMaker_AVAILABLE and has_male_voice:
        try:
            asyncio.run(_ttsmaker.ensure_session(voice_key="alfie"))
            log_entries.append({
                "time": now_str(), "level": "success",
                "msg": "TTSMaker 登录成功，开始生成音频"
            })
        except Exception as login_err:
            log_entries.append({
                "time": now_str(), "level": "error",
                "msg": f"TTSMaker 登录失败: {login_err}，男声将回退到 edge-tts"
            })
        yield (
            build_progress_log_html(log_entries),
            build_download_html(progress, get_completed_file_list(progress)),
            build_stats_bar(progress),
            f"生成中 — {progress['completed']}/{total}",
            gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
        )
    elif _TTSMaker_AVAILABLE and not has_male_voice:
        log_entries.append({
            "time": now_str(), "level": "info",
            "msg": "未检测到男声数据，跳过 TTSMaker 浏览器启动"
        })
        yield (
            build_progress_log_html(log_entries),
            build_download_html(progress, get_completed_file_list(progress)),
            build_stats_bar(progress),
            f"生成中 — {progress['completed']}/{total}",
            gr.update(visible=False), gr.update(), gr.update(value=None), filepath,
        )

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

            speakers = parse_speakers(text)
            speaker_info = ""
            if len(speakers) > 1 or speakers[0][0] != FEMALE_VOICE:
                voices_used = set(v for v, _ in speakers)
                if voices_used == {FEMALE_VOICE, MALE_VOICE}:
                    speaker_info = " [混合音色]"
                elif MALE_VOICE in voices_used:
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
                    text, rate, volume, pitch, pause, proxy, tmp_dir
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

        # ---- 清理临时目录 ----
        shutil.rmtree(tmp_dir, ignore_errors=True)

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
        # 无论成功/失败/取消，都关闭 TTSMaker 浏览器会话
        if _TTSMaker_AVAILABLE and has_male_voice:
            try:
                asyncio.run(_ttsmaker.close_session())
                log_entries.append({
                    "time": now_str(), "level": "info",
                    "msg": "TTSMaker 浏览器已关闭"
                })
            except Exception as close_err:
                log_entries.append({
                    "time": now_str(), "level": "warn",
                    "msg": f"关闭 TTSMaker 浏览器异常: {close_err}"
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
.config-slider {
    padding: 0 16px !important;
}
.config-slider .svelte-1pkg2y {
    font-size: 11.5px !important;
    color: var(--c-text-sub) !important;
    font-weight: 400 !important;
}
.config-slider input[type="range"] {
    height: 4px !important;
    border-radius: 2px !important;
}
.config-slider input[type="range"]::-webkit-slider-thumb {
    width: 14px !important;
    height: 14px !important;
    border-radius: 50% !important;
    background: var(--c-accent) !important;
    border: 2px solid var(--c-panel) !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12) !important;
    cursor: pointer;
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
                    file_types=[".docx"],
                    file_count="single",
                    type="filepath",
                    elem_id="file-upload",
                )

            # 音频配置
            with gr.Group(elem_id="sidebar-config"):
                gr.HTML('<div class="sidebar-section"><div class="sidebar-section-title">音频配置</div></div>')

                with gr.Group(elem_classes="config-dropdown"):
                    rate = gr.Dropdown(
                        choices=[
                            ("0.5x 降速", "0.5"), ("0.6x 降速", "0.6"),
                            ("0.7x 降速", "0.7"), ("0.8x 降速", "0.8"),
                            ("0.85x 降速", "0.85"), ("0.9x 降速", "0.9"),
                            ("0.95x 降速", "0.95"), ("1.0x (默认语速)", "1.0"),
                            ("1.05x 加速", "1.05"), ("1.1x 加速", "1.1"),
                            ("1.15x 加速", "1.15"), ("1.2x 加速", "1.2"),
                            ("1.3x 加速", "1.3"), ("1.4x 加速", "1.4"),
                            ("1.5x 加速", "1.5"), ("2.0x 加速", "2.0"),
                        ],
                        value="1.0",
                        label="语速",
                        info="调节语音播放速度",
                    )
                with gr.Group(elem_classes="config-dropdown"):
                    volume = gr.Dropdown(
                        choices=[
                            ("10% 降低音量", "0.1"), ("20% 降低音量", "0.2"),
                            ("50% 降低音量", "0.5"), ("80% 降低音量", "0.8"),
                            ("100% (默认音量)", "1"),
                            ("120% 提升音量", "1.2"), ("150% 提升音量", "1.5"),
                            ("180% 提升音量", "1.8"), ("200% 提升音量 (可能破音)", "2.0"),
                        ],
                        value="1",
                        label="音量",
                        info="调节语音音量大小",
                    )
                with gr.Group(elem_classes="config-dropdown"):
                    pitch = gr.Dropdown(
                        choices=[
                            ("0.5x 重度降低 (-50%)", "0.5"),
                            ("0.75x 中度降低 (-25%)", "0.75"),
                            ("0.9x 轻微降低 (-10%)", "0.9"),
                            ("0.95x 微微降低 (-5%)", "0.95"),
                            ("1.0x (默认)", "1"),
                            ("1.05x 微微升高 (+5%)", "1.05"),
                            ("1.1x 轻度升高 (+10%)", "1.1"),
                            ("1.25x 中度升高 (+25%)", "1.25"),
                            ("1.5x 重度升高 (+50%)", "1.5"),
                            ("2.0x 超级升高 (+100%)", "2.0"),
                        ],
                        value="1",
                        label="音调",
                        info="调节语音音调高低",
                    )
                with gr.Group(elem_classes="config-dropdown"):
                    pause = gr.Dropdown(
                        choices=[
                            ("0s (消除停顿，段落连读不停顿)", "-1"),
                            ("50ms 紧凑", "50"), ("100ms 紧凑", "100"),
                            ("200ms 紧凑", "200"),
                            ("300ms (默认，听感最自然)", "0"),
                            ("500ms", "500"), ("600ms", "600"),
                            ("800ms", "800"), ("1000ms (1s)", "1000"),
                            ("1200ms", "1200"), ("1500ms", "1500"),
                            ("1800ms", "1800"), ("2000ms (2s)", "2000"),
                            ("2500ms", "2500"), ("3000ms (3s)", "3000"),
                            ("4000ms", "4000"), ("5000ms (5s)", "5000"),
                            ("10000ms (10s)", "10000"),
                        ],
                        value="0",
                        label="段落停顿时间",
                        info="调节每一个段落（换行）的停顿时间",
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
                with gr.Group(elem_classes="config-textbox"):
                    proxy = gr.Textbox(
                        label="代理地址（可选）",
                        placeholder="如 http://127.0.0.1:7890",
                        info="遇到 403 / 网络问题时可填入代理",
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
                '<strong>音色自动分配规则：</strong><br>'
                '<span class="voice-female">● 女声</span>：en-US-JennyNeural (edge-tts)<br>'
                '　└ w/W 标识 → 女声<br>'
                '　└ 无标识 → 默认女声<br>'
                '<span class="voice-male">● 男声</span>：'
                + ('TTSMaker 788 Alfie' if _TTSMaker_AVAILABLE else 'fr-FR-RemyMultilingualNeural (edge-tts)')
                + '<br>'
                '　└ m/M 标识 → 男声<br>'
                '<em style="font-size:10px; color:var(--c-text-muted);">'
                + ('男声通过 TTSMaker 网站生成，需要 Playwright + Chrome' if _TTSMaker_AVAILABLE else 'TTSMaker 不可用，男声回退到 edge-tts')
                + ' · 生成音频时自动去除 w/m 标识</em>'
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
        inputs=[file_input, rate, volume, pitch, pause, fmt, quality, proxy,
                preview],
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
