"""单题合成核心：角色段落展开与讯飞音频拼接。"""


import sys

from wordtts.speakers import parse_speakers_with_roles
from wordtts.tts_config import (
    _normalize_voice_params,
    clamp_tts_param,
    role_config_key,
)
from wordtts.xunfei_bridge import _xunfei


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
