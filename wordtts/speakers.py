"""说话人解析：从文本中提取 w/m 标识与通用角色标记，分配音色并清除标识。"""


import re

from wordtts.config import (
    DEFAULT_FEMALE_ROLE_KEY,
    DEFAULT_MALE_ROLE_KEY,
    FEMALE_VOICE,
    MALE_VOICE,
    WORD_CATEGORIES,
)
from wordtts.tts_config import normalize_role_key


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
