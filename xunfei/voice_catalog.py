"""运行时使用的讯飞音色注册表。"""

from __future__ import annotations


VOICES = {
    # 女声（默认；词汇题型也使用该女声）
    "amanda": {
        "name": "英语-Amanda",
        "display": "英语-Amanda (英语女声)",
        "gender": "female",
        # 目录刷新失败时仍要能构造多人配音 payload。在线 common/list
        # 返回该基础音色后会覆盖名称和 commonId。
        "speaker_no": 544508087,
        "vcn_type": 1,
        "language": "英语",
    },
    # 男声
    "george": {
        "name": "英语-George",
        "display": "英语-George (英语男声)",
        "gender": "male",
        "speaker_no": 593031758,
        "vcn_type": 1,
        "language": "英语",
    },
    # 信息转述题干的中文女声。common/list 的稳定 key 在目录刷新后仍保持
    # 不变；speaker_no 仅作为离线目录不可用时的页面选择兜底。
    "common:10000023": {
        "name": "晓燕",
        "display": "晓燕 (普通话女声)",
        "gender": "female",
        "speaker_no": 130165,
        "common_id": 10000023,
        "vcn_type": 1,
        "language": "普通话",
    },
}

DEFAULT_FEMALE = "amanda"
DEFAULT_MALE = "george"


def register_voice_catalog(voices):
    """注册目录中的稳定 key 与讯飞网页可见名称。"""
    if not isinstance(voices, (list, tuple)):
        return
    for voice in voices:
        if not isinstance(voice, dict):
            continue
        key = str(voice.get("key") or "").strip()
        name = str(voice.get("name") or voice.get("speakerName") or "").strip()
        if not key or not name:
            continue
        previous = VOICES.get(key) if isinstance(VOICES.get(key), dict) else {}
        gender = str(voice.get("gender") or "unknown").strip().lower()
        gender_label = "女声" if gender == "female" else ("男声" if gender == "male" else "音色")

        # common/list 的基础卡片只有 commonId，没有具体 speakerNo。先记住
        # 这次输入是否明确带了 common 身份，再决定是否可以沿用内置旧 ID；
        # 否则 live common/list 刷新会把旧的 544508087/593031758 留在内存
        # 里，而页面实际选中的变体已经换成了新的 speakerId，回读校验就会
        # 把一次成功的“使用”误判成失败。
        input_common_id = voice.get("common_id")
        if input_common_id in (None, ""):
            input_common_id = voice.get("commonId")

        speaker_no = voice.get("speaker_no")
        if speaker_no in (None, ""):
            speaker_no = voice.get("speakerNo")
        # 只有没有 common 身份的离线/旧 flat 目录才可以沿用两个内置默认
        # 音色的兜底 ID。明确来自 common/list 时必须清掉旧 ID，让多人配音
        # 页面在点击基础卡片后回读它当次实际生成的 speakerId。
        if (
            speaker_no in (None, "")
            and input_common_id in (None, "")
            and key in {DEFAULT_FEMALE, DEFAULT_MALE}
        ):
            speaker_no = previous.get("speaker_no") or previous.get("speakerNo")
        common_id = voice.get("common_id")
        if common_id in (None, ""):
            common_id = voice.get("commonId")
        if common_id in (None, ""):
            common_id = previous.get("common_id") or previous.get("commonId")
        language = voice.get("language")
        if language in (None, ""):
            language = voice.get("speakerLanguage")
        if language in (None, ""):
            language = previous.get("language") or previous.get("speaker_language") or ""
        vcn_type = voice.get("vcn_type")
        if vcn_type in (None, ""):
            vcn_type = voice.get("vcnType")
        if vcn_type in (None, ""):
            vcn_type = previous.get("vcn_type") or previous.get("vcnType") or 1
        speaker_language = voice.get("speaker_language")
        if speaker_language in (None, ""):
            speaker_language = voice.get("speakerLanguage")
        if speaker_language in (None, ""):
            speaker_language = previous.get("speaker_language") or language or ""
        is_vip = voice.get("is_vip") if "is_vip" in voice else voice.get("isVip")
        if is_vip is None:
            is_vip = previous.get("is_vip") if "is_vip" in previous else previous.get("isVip")
        VOICES[key] = {
            "name": name,
            "display": f"{name} ({gender_label})",
            "gender": gender,
            "speaker_no": speaker_no,
            "common_id": common_id,
            "img_url": voice.get("img_url") or voice.get("imgUrl") or "",
            "language": language,
            "vcn_type": vcn_type,
            "speaker_language": speaker_language,
            "is_vip": is_vip,
            "emot_type": voice.get("emot_type") or voice.get("emotType"),
            "emot_val": voice.get("emot_val") or voice.get("emotVal"),
        }


def register_voice_aliases(aliases):
    """注册旧 flat/list key 到当前 common/list key 的不可见兼容映射。"""
    if not isinstance(aliases, dict):
        return
    for alias, target in list(aliases.items())[:4096]:
        alias_key = str(alias or "").strip()
        target_key = str(target or "").strip()
        if not alias_key or not target_key or alias_key == target_key:
            continue
        target_info = VOICES.get(target_key)
        if not isinstance(target_info, dict):
            continue
        # 别名只用于兼容旧配置，绝不覆盖当前目录中的真实 key。
        if alias_key not in VOICES:
            VOICES[alias_key] = dict(target_info)


def get_voice_info(voice_key):
    """返回已注册音色信息，避免用未知 key 发起错误合成。"""
    key = str(voice_key or "").strip()
    if key not in VOICES:
        raise ValueError(f"未知音色 {key!r}，请刷新讯飞音色目录后重试")
    return VOICES[key]
