"""声音/输出配置规范化：角色参数槽位与讯飞平台三参数收敛。"""


import re

from wordtts.config import (
    DEFAULT_FEMALE_ROLE_KEY,
    DEFAULT_MALE_ROLE_KEY,
    DEFAULT_GENERATION_MODE,
    FEMALE_VOICE,
    GENERATION_MODES,
    MALE_VOICE,
    QUALITY_BITRATE,
    ROLE_CONFIG_PREFIX,
    TTS_CONFIG_VERSION,
    TTS_FEMALE_RATE_DEFAULT,
    TTS_MALE_RATE_DEFAULT,
    TTS_PARAM_DEFAULT,
    TTS_PARAM_MAX,
    TTS_PARAM_MIN,
)


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
