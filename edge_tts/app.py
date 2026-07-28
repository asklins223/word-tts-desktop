import os
import math
import shutil
import uuid
import asyncio
import contextlib
import wave

import edge_tts
import gradio as gr
from pydub import AudioSegment

from voice_match_788 import (
    VoiceMatchError,
    load_profile,
    pcm_to_wav_bytes,
    process_audio_segment,
    stream_edge_tts_788_pcm,
)

# ============================================================================
# 路径与基础配置
# ============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_DIR = os.path.join(BASE_DIR, "example")
BGM_DIR = os.path.join(BASE_DIR, "bgm")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
for _d in (BGM_DIR, OUTPUT_DIR):
    os.makedirs(_d, exist_ok=True)

# 读取样式
try:
    with open(os.path.join(BASE_DIR, "style.css"), "r", encoding="utf-8") as _f:
        CSS = _f.read()
except Exception:
    CSS = ""

# ============================================================================
# 音色列表：从 edge-tts 动态获取全部音色，按语言分组，带本地回退
# ============================================================================
LOCALE_NAMES = {
    "zh-CN": "中文（中国大陆）", "zh-CN-liaoning": "中文（辽宁方言）",
    "zh-CN-shaanxi": "中文（陕西方言）", "zh-HK": "中文（香港）", "zh-TW": "中文（台湾）",
    "en-US": "英语（美国）", "en-GB": "英语（英国）", "en-AU": "英语（澳大利亚）",
    "en-CA": "英语（加拿大）", "en-IN": "英语（印度）", "en-IE": "英语（爱尔兰）",
    "en-NZ": "英语（新西兰）", "en-ZA": "英语（南非）", "en-PH": "英语（菲律宾）",
    "en-SG": "英语（新加坡）", "en-HK": "英语（香港）", "en-KE": "英语（肯尼亚）",
    "en-NG": "英语（尼日利亚）", "en-TZ": "英语（坦桑尼亚）", "ja-JP": "日语（日本）", "ko-KR": "韩语（韩国）",
    "fr-FR": "法语（法国）", "fr-CA": "法语（加拿大）", "fr-CH": "法语（瑞士）",
    "fr-BE": "法语（比利时）", "de-DE": "德语（德国）", "de-AT": "德语（奥地利）",
    "de-CH": "德语（瑞士）", "es-ES": "西班牙语（西班牙）", "es-MX": "西班牙语（墨西哥）",
    "es-US": "西班牙语（美国）", "es-AR": "西班牙语（阿根廷）", "es-CO": "西班牙语（哥伦比亚）",
    "it-IT": "意大利语（意大利）", "pt-BR": "葡萄牙语（巴西）", "pt-PT": "葡萄牙语（葡萄牙）",
    "ru-RU": "俄语（俄罗斯）", "ar-EG": "阿拉伯语（埃及）", "ar-SA": "阿拉伯语（沙特）",
    "hi-IN": "印地语（印度）", "vi-VN": "越南语（越南）", "th-TH": "泰语（泰国）",
    "id-ID": "印尼语（印尼）", "ms-MY": "马来语（马来西亚）", "tr-TR": "土耳其语（土耳其）",
    "nl-NL": "荷兰语（荷兰）", "pl-PL": "波兰语（波兰）", "sv-SE": "瑞典语（瑞典）",
    "da-DK": "丹麦语（丹麦）", "fi-FI": "芬兰语（芬兰）", "nb-NO": "挪威语（挪威）",
    "cs-CZ": "捷克语（捷克）", "el-GR": "希腊语（希腊）", "hu-HU": "匈牙利语（匈牙利）",
    "ro-RO": "罗马尼亚语（罗马尼亚）", "uk-UA": "乌克兰语（乌克兰）",
    "he-IL": "希伯来语（以色列）", "af-ZA": "南非荷兰语（南非）",
}

# 网络获取失败时使用的回退音色
FALLBACK_VOICES = [
    {"ShortName": "zh-CN-XiaoxiaoNeural", "Locale": "zh-CN", "Gender": "Female"},
    {"ShortName": "zh-CN-XiaoyiNeural", "Locale": "zh-CN", "Gender": "Female"},
    {"ShortName": "zh-CN-YunjianNeural", "Locale": "zh-CN", "Gender": "Male"},
    {"ShortName": "zh-CN-YunxiNeural", "Locale": "zh-CN", "Gender": "Male"},
    {"ShortName": "zh-CN-YunxiaNeural", "Locale": "zh-CN", "Gender": "Male"},
    {"ShortName": "zh-CN-YunyangNeural", "Locale": "zh-CN", "Gender": "Male"},
    {"ShortName": "zh-CN-liaoning-XiaobeiNeural", "Locale": "zh-CN", "Gender": "Female"},
    {"ShortName": "zh-CN-shaanxi-XiaoniNeural", "Locale": "zh-CN", "Gender": "Female"},
    {"ShortName": "zh-HK-HiuMaanNeural", "Locale": "zh-HK", "Gender": "Female"},
    {"ShortName": "zh-TW-HsiaoChenNeural", "Locale": "zh-TW", "Gender": "Female"},
    {"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female"},
    {"ShortName": "en-US-GuyNeural", "Locale": "en-US", "Gender": "Male"},
    {"ShortName": "en-GB-SoniaNeural", "Locale": "en-GB", "Gender": "Female"},
    {"ShortName": "fr-FR-RemyMultilingualNeural", "Locale": "fr-FR", "Gender": "Male"},
    {"ShortName": "ja-JP-NanamiNeural", "Locale": "ja-JP", "Gender": "Female"},
    {"ShortName": "ko-KR-SunHiNeural", "Locale": "ko-KR", "Gender": "Female"},
]


def _voice_label(v):
    short = v["ShortName"]
    loc = v["Locale"]
    loc_name = LOCALE_NAMES.get(loc, loc)
    personal = short.split("-")[-1].replace("Neural", "")
    gender = "女" if v.get("Gender") == "Female" else "男"
    return f"{loc_name} - {personal}（{gender}）"


# 主界面保留中文、英语，以及 788 匹配预设所需的 Remy。
VOICE_LOCALE_PREFIX = ("zh", "en")
VOICE_ALLOWLIST = {"fr-FR-RemyMultilingualNeural"}
MATCH_788_PROFILE = load_profile()
MATCH_788_VOICE = MATCH_788_PROFILE.source_voice


def load_voices():
    """获取中文+英语音色并按语言分组，返回 [(展示标签, ShortName), ...]。"""
    try:
        voices = asyncio.run(edge_tts.list_voices())
    except Exception as e:  # noqa: BLE001
        print(f"[edge-tts] 获取音色列表失败，使用回退列表：{e}")
        voices = FALLBACK_VOICES
    grouped = {}
    for v in voices:
        loc = v["Locale"]
        if (
            not any(loc.startswith(p) for p in VOICE_LOCALE_PREFIX)
            and v.get("ShortName") not in VOICE_ALLOWLIST
        ):
            continue
        grouped.setdefault(loc, []).append(v)
    choices = []
    for loc in sorted(grouped.keys()):
        for v in sorted(grouped[loc], key=lambda x: x["ShortName"]):
            choices.append((_voice_label(v), v["ShortName"]))
    return choices


VOICE_CHOICES = load_voices()


def example_for_voice(short_name):
    """返回该音色对应的示例音频文件路径（优先 mp3，回退 wav）。"""
    for ext in (".mp3", ".wav"):
        p = os.path.join(EXAMPLE_DIR, short_name + ext)
        if os.path.exists(p):
            return p
    return None


# ============================================================================
# 参数格式化
# ============================================================================
def fmt_pct(v):
    return ("+" if v >= 0 else "") + str(int(v)) + "%"


def fmt_hz(v):
    return ("+" if v >= 0 else "") + str(int(v)) + "Hz"


# ============================================================================
# 导出格式与音频质量
# ============================================================================
# pydub(ffmpeg) 容器格式 -> (format, 扩展名)
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


def export_audio(seg, fmt, quality, out_path):
    """按指定格式与码率导出音频。"""
    fmt_id, _ext = FORMAT_MAP[fmt]
    kwargs = {"format": fmt_id}
    br = QUALITY_BITRATE.get(quality)
    # 仅对支持码率的格式应用：mp3/aac/opus；ogg(vorbis) 用默认质量，wav 无损
    if br and fmt in ("mp3", "aac", "opus"):
        kwargs["bitrate"] = br
    seg.export(out_path, **kwargs)
    return out_path


def export_788_live_audio(wav_path, out_path):
    """Encode the accumulated live PCM without blocking the event loop."""
    AudioSegment.from_wav(wav_path).export(
        out_path,
        format="mp3",
        bitrate="64k",
    )


# ============================================================================
# 核心合成：按段落（换行）合成 + 段落间停顿 + 试听模式
# ============================================================================
def _build_voice_audio(text, voice, rate, volume, pitch, pause_ms, proxy, preview):
    paragraphs = [p.strip() for p in str(text).splitlines() if p.strip()]
    if not paragraphs:
        raise gr.Error("请输入要转换的文本")
    # 试听模式：仅合成第一段开头片段，快速预览
    if preview:
        first = paragraphs[0]
        if len(first) > 40:
            first = first[:40]
        paragraphs = [first]

    async def _synth_all():
        segments = []
        uid = uuid.uuid4().hex[:8]
        for i, para in enumerate(paragraphs):
            tmp = os.path.join(OUTPUT_DIR, f".seg_{uid}_{i}.mp3")
            try:
                communicate = edge_tts.Communicate(
                    para, voice,
                    rate=fmt_pct(rate),
                    volume=fmt_pct(volume),
                    pitch=fmt_hz(pitch),
                    proxy=proxy or None,
                )
                await communicate.save(tmp)
                if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    segments.append(AudioSegment.from_file(tmp))
                else:
                    raise RuntimeError(f"第 {i + 1} 段合成失败：未生成有效音频")
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        return segments

    segments = asyncio.run(_synth_all())
    if not segments:
        raise gr.Error("合成失败，未生成任何音频")
    silence = AudioSegment.silent(duration=max(0, int(pause_ms)))
    full = segments[0]
    for seg in segments[1:]:
        full = full + silence + seg
    return full


# ============================================================================
# 背景音乐：混音
# ============================================================================
def mix_bgm(voice_seg, bgm_choice, bgm_vol):
    if not bgm_choice or bgm_choice == "none":
        return voice_seg
    path = os.path.join(BGM_DIR, bgm_choice)
    if not os.path.exists(path):
        gr.Warning(f"背景音乐文件不存在：{bgm_choice}")
        return voice_seg
    try:
        bgm = AudioSegment.from_file(path)
    except Exception as e:  # noqa: BLE001
        gr.Warning(f"背景音乐读取失败：{e}")
        return voice_seg
    bgm = bgm.set_channels(voice_seg.channels).set_frame_rate(voice_seg.frame_rate)
    vol = int(bgm_vol)
    if vol <= 0:
        return voice_seg
    if vol > 100:
        vol = 100
    # 音量百分比转 dB 增益：100% => 0dB，50% => -6dB，10% => -20dB
    gain_db = 20 * math.log10(vol / 100.0)
    bgm = bgm.apply_gain(gain_db)
    # 循环到至少覆盖语音长度
    if len(bgm) < len(voice_seg):
        loops = len(voice_seg) // len(bgm) + 1
        bgm = bgm * loops
    bgm = bgm[:len(voice_seg)]
    return voice_seg.overlay(bgm)


# ============================================================================
# 背景音乐：文件管理
# ============================================================================
BGM_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".flac", ".wma")


def list_bgm_files():
    if not os.path.isdir(BGM_DIR):
        return []
    return sorted(f for f in os.listdir(BGM_DIR) if f.lower().endswith(BGM_EXTS))


def _bgm_state():
    """返回 (mix_choices, del_choices, del_value, markdown)。"""
    files = list_bgm_files()
    mix = [("无（纯人声）", "none")] + [(f, f) for f in files]
    if files:
        del_choices = [(f, f) for f in files]
        del_val = files[0]
    else:
        del_choices = [("(暂无背景音乐)", "")]
        del_val = ""
    if files:
        md = "### 当前背景音乐\n" + "\n".join(f"- `{f}`" for f in files)
    else:
        md = "### 当前背景音乐\n（暂无，请上传）"
    return mix, del_choices, del_val, md


def _bgm_refresh():
    mix, del_choices, del_val, md = _bgm_state()
    return (
        gr.update(choices=mix, value="none"),
        gr.update(choices=del_choices, value=del_val),
        md,
    )


def _filepath(value):
    """兼容不同 Gradio 版本下 gr.File 返回值的类型。"""
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        return value
    for attr in ("name", "path"):
        v = getattr(value, attr, None)
        if isinstance(v, str) and v:
            return v
    if isinstance(value, dict):
        return value.get("name") or value.get("path")
    return None


def upload_bgm_fn(file_obj):
    src = _filepath(file_obj)
    if not src or not os.path.exists(src):
        gr.Warning("未选择文件或文件不存在")
        return _bgm_refresh() + (gr.update(),)
    # 取原始文件名并去重
    name = os.path.basename(getattr(file_obj, "orig_name", None) or src)
    name = os.path.basename(name)
    dst = os.path.join(BGM_DIR, name)
    if os.path.exists(dst):
        base, ext = os.path.splitext(name)
        dst = os.path.join(BGM_DIR, f"{base}_{uuid.uuid4().hex[:4]}{ext}")
        name = os.path.basename(dst)
    try:
        shutil.copy(src, dst)
    except Exception as e:  # noqa: BLE001
        gr.Warning(f"上传失败：{e}")
        return _bgm_refresh() + (gr.update(),)
    gr.Info(f"已上传背景音乐：{name}")
    return _bgm_refresh() + (gr.update(value=None),)


def delete_bgm_fn(filename):
    if not filename:
        gr.Warning("没有可删除的背景音乐")
        return _bgm_refresh()
    path = os.path.join(BGM_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
            gr.Info(f"已删除：{filename}")
        except Exception as e:  # noqa: BLE001
            gr.Warning(f"删除失败：{e}")
    else:
        gr.Warning("文件不存在")
    return _bgm_refresh()


# ============================================================================
# UI 事件
# ============================================================================
def changeVoice(voice):
    return example_for_voice(voice)


def filter_voices(query):
    """根据搜索词过滤发音人列表（匹配展示标签和 ShortName）。"""
    q = (query or "").strip().lower()
    if not q:
        filtered = VOICE_CHOICES
    else:
        filtered = [(label, val) for label, val in VOICE_CHOICES
                    if q in label.lower() or q in val.lower()]
    return gr.update(choices=filtered)


def contains_cjk(text):
    return any("\u3400" <= char <= "\u9fff" for char in str(text or ""))


def toggle_788_mode(enabled):
    """启用预设时把发音人切到校准所用的 Remy。"""
    if enabled:
        return gr.update(value=MATCH_788_VOICE)
    return gr.update()


def generate(text, voice, rate, volume, pitch, pause, fmt, quality,
             bgm_choice, bgm_vol, preview, proxy, match_788, match_strength):
    if not text or not str(text).strip():
        raise gr.Error("请输入要转换的文本")
    if not voice:
        raise gr.Error("请选择发音人")

    if match_788:
        voice = MATCH_788_VOICE
        if contains_cjk(text):
            gr.Warning("788 预设由英语参考音频校准；中文可处理，但口音和相似度不作保证")

    pause_ms = int(float(pause) * 1000)
    gr.Info("正在合成语音…")
    voice_audio = _build_voice_audio(text, voice, rate, volume, pitch,
                                     pause_ms, proxy, preview)
    if match_788:
        gr.Info("正在应用 788 低延迟匹配…")
        try:
            voice_audio = process_audio_segment(voice_audio, match_strength)
        except VoiceMatchError as exc:
            raise gr.Error(str(exc)) from exc
    gr.Info("正在处理背景音乐与导出…")
    final_audio = mix_bgm(voice_audio, bgm_choice, bgm_vol)
    _fmt_id, ext = FORMAT_MAP[fmt]
    out_path = os.path.join(OUTPUT_DIR, f"output_{uuid.uuid4().hex[:10]}{ext}")
    export_audio(final_audio, fmt, quality, out_path)
    gr.Info("生成完成！")
    return out_path, out_path


async def generate_788_live(text, rate, volume, pitch, match_strength, proxy):
    """边接收 Edge 音频边完成 788 DSP，并把独立 WAV 块推给 Gradio。"""
    paragraphs = [p.strip() for p in str(text or "").splitlines() if p.strip()]
    if not paragraphs:
        raise gr.Error("请输入要实时试听的文本")
    if contains_cjk(text):
        gr.Warning("788 预设由英语参考音频校准；中文可处理，但口音和相似度不作保证")

    # 单个 Edge 流可以保留 MP3 解码器和 DSP 的跨块状态；换行交给引擎
    # 生成自然停顿，精确的自定义段间停顿仍由完整“生成”流程负责。
    stream_text = "\n\n".join(paragraphs)
    uid = uuid.uuid4().hex[:10]
    out_path = os.path.join(OUTPUT_DIR, f"788_live_{uid}.mp3")
    temp_wav_path = os.path.join(OUTPUT_DIR, f".788_live_{uid}.wav")
    emitted = False
    completed = False
    try:
        async with contextlib.aclosing(
            stream_edge_tts_788_pcm(
                stream_text,
                rate=fmt_pct(rate),
                volume=fmt_pct(volume),
                pitch=fmt_hz(pitch),
                proxy=proxy or None,
                strength=match_strength,
            )
        ) as pcm_stream:
            with wave.open(temp_wav_path, "wb") as output_wave:
                output_wave.setnchannels(1)
                output_wave.setsampwidth(2)
                output_wave.setframerate(MATCH_788_PROFILE.sample_rate_hz)
                async for pcm in pcm_stream:
                    output_wave.writeframesraw(pcm)
                    emitted = True
                    yield pcm_to_wav_bytes(
                        pcm,
                        sample_rate_hz=MATCH_788_PROFILE.sample_rate_hz,
                    ), gr.skip()
        if not emitted:
            raise VoiceMatchError("788 实时试听未产生音频")
        export_task = asyncio.create_task(
            asyncio.to_thread(export_788_live_audio, temp_wav_path, out_path)
        )
        try:
            await asyncio.shield(export_task)
        except asyncio.CancelledError as cancelled:
            # Cancelling to_thread only cancels the awaiter, not the encoder
            # thread. Keep shielding until it has stopped so finally cannot
            # delete a path that the worker later recreates.
            while not export_task.done():
                try:
                    await asyncio.shield(export_task)
                except asyncio.CancelledError:
                    continue
            with contextlib.suppress(Exception):
                export_task.result()
            raise cancelled
        completed = True
        yield gr.skip(), out_path
    except Exception as exc:
        raise gr.Error(f"788 实时试听失败：{exc}") from exc
    finally:
        if os.path.exists(temp_wav_path):
            try:
                os.remove(temp_wav_path)
            except OSError:
                pass
        if not completed and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass


def clearSpeech():
    return None, None, None


# ============================================================================
# 界面
# ============================================================================
with gr.Blocks(title="微软 Edge 文本转语音", css=CSS) as demo:
    gr.Markdown(
        "# 微软 Edge 文本转语音\n"
        "调用 edge-tts 进行转换，支持中文/英语音色、多段落停顿、背景音乐，"
        "并提供 Remy → 788 的低延迟匹配预设。"
    )
    with gr.Row():
        # ---------- 左：文本与高级设置 ----------
        with gr.Column():
            text = gr.TextArea(
                label="文本（每一行是一个段落，段落间可设置停顿）",
                placeholder="请输入要转换的文本…",
                elem_classes="text-area",
            )
            with gr.Row():
                btn = gr.Button("生成", elem_id="submit-btn")
                clear = gr.Button("清除", elem_id="clear-btn")
            with gr.Accordion("高级设置", open=False):
                rate = gr.Slider(-100, 100, step=1, value=0,
                                 label="语速增减", info="加快或减慢语速（%）")
                volume = gr.Slider(-100, 100, step=1, value=0,
                                   label="音量增减", info="加大或减小音量（%）")
                pitch = gr.Slider(-50, 50, step=1, value=0,
                                  label="音调增减", info="升高或降低音调（Hz）")
                pause = gr.Slider(0, 5, step=0.1, value=0.5,
                                  label="段落停顿时间",
                                  info="每个换行段落之间的停顿时长（秒）")
                preview = gr.Checkbox(
                    label="试听模式",
                    value=False,
                    info="开启后仅生成第一段开头片段，用于快速预览",
                )
                fmt = gr.Dropdown(
                    choices=["mp3", "ogg", "aac", "opus", "wav"],
                    value="mp3",
                    label="下载文件格式",
                    info="输出音频的容器格式",
                )
                quality = gr.Dropdown(
                    choices=list(QUALITY_BITRATE.keys()),
                    value="128 kbps（标准）",
                    label="MP3 / 音频质量",
                    info="音频码率（影响 MP3/AAC/OPUS；OGG 用默认质量、WAV 无损）",
                )
                proxy = gr.Textbox(
                    label="代理地址（可选）",
                    placeholder="如 http://127.0.0.1:7890，留空则直连",
                    info="遇到 403 / 网络问题时可填入代理",
                )
                with gr.Accordion("788 音色匹配", open=True):
                    match_788 = gr.Checkbox(
                        label="启用 788 极速匹配（自动使用 Remy）",
                        value=False,
                        info="生成时在人声上应用校准后的流式频谱、轻微音色位移和限幅。",
                    )
                    match_strength = gr.Slider(
                        0,
                        100,
                        step=1,
                        value=100,
                        label="788 匹配强度",
                        info="100=完整校准曲线；降低可减少处理痕迹。",
                    )
                    gr.Markdown(
                        "极速匹配是低延迟 DSP，不是声纹克隆；现有单条参考音频"
                        "不足以证明 99% 身份匹配。当前配置按英语校准。"
                    )
                    live_788_btn = gr.Button("边生成边播放 788 试听", variant="primary")
                    live_788_audio = gr.Audio(
                        label="788 实时流",
                        streaming=True,
                        autoplay=True,
                        interactive=False,
                        format="wav",
                    )
                    live_788_download = gr.File(label="实时试听完成后下载")

        # ---------- 右：音色、示例、输出、背景音乐 ----------
        with gr.Column():
            voice_search = gr.Textbox(
                label="搜索发音人",
                placeholder="输入语言或名字，如 中文 / Xiaoxiao / 英语 / Aria",
                interactive=True,
            )
            voices = gr.Dropdown(
                choices=VOICE_CHOICES,
                value="zh-CN-XiaoxiaoNeural",
                label="发音人（音色）",
                info="中文+英语 60+ 音色，并额外提供 Remy；可用搜索框或下拉筛选",
                filterable=True,
                interactive=True,
            )
            example = gr.Audio(
                label="该音色示例",
                value=os.path.join(EXAMPLE_DIR, "zh-CN-XiaoxiaoNeural.wav"),
                interactive=False,
                elem_classes="example",
            )
            bgm_select = gr.Dropdown(
                choices=[("无（纯人声）", "none")],
                value="none",
                label="背景音乐",
                interactive=True,
                info="在下方“背景音乐管理”上传后可在此选择",
            )
            bgm_vol = gr.Slider(
                0, 100, step=1, value=30,
                label="背景音乐音量",
                info="0=静音，100=原始音量",
            )
            audio = gr.Audio(label="输出（试听/下载）", interactive=False, elem_classes="audio")
            download = gr.File(label="下载文件")
            with gr.Accordion("背景音乐管理", open=False):
                bgm_upload = gr.File(
                    label="上传背景音乐",
                    file_count="single",
                    file_types=["audio"],
                )
                with gr.Row():
                    upload_btn = gr.Button("上传", variant="primary")
                    delete_btn = gr.Button("删除选中", variant="stop")
                bgm_delete = gr.Dropdown(
                    choices=[("(暂无背景音乐)", "")],
                    value="",
                    label="选择要删除的文件",
                    interactive=True,
                )
                bgm_list = gr.Markdown("### 当前背景音乐\n（暂无，请上传）")

    # ---------- 事件绑定 ----------
    voice_search.change(fn=filter_voices, inputs=voice_search, outputs=voices)
    voices.change(fn=changeVoice, inputs=voices, outputs=example)
    match_788.change(fn=toggle_788_mode, inputs=match_788, outputs=voices)
    btn.click(
        fn=generate,
        inputs=[text, voices, rate, volume, pitch, pause, fmt, quality,
                bgm_select, bgm_vol, preview, proxy, match_788, match_strength],
        outputs=[audio, download],
    )
    live_788_btn.click(
        fn=generate_788_live,
        inputs=[text, rate, volume, pitch, match_strength, proxy],
        outputs=[live_788_audio, live_788_download],
    )
    clear.click(fn=clearSpeech, outputs=[text, audio, download])
    upload_btn.click(
        fn=upload_bgm_fn,
        inputs=bgm_upload,
        outputs=[bgm_select, bgm_delete, bgm_list, bgm_upload],
    )
    delete_btn.click(
        fn=delete_bgm_fn,
        inputs=bgm_delete,
        outputs=[bgm_select, bgm_delete, bgm_list],
    )
    demo.load(fn=_bgm_refresh, outputs=[bgm_select, bgm_delete, bgm_list])


if __name__ == "__main__":
    demo.launch()
