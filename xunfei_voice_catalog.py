"""讯飞配音音色目录：启动刷新、规范化和本地缓存。

这个模块只负责读取讯飞公开音色接口和维护 JSON 缓存，不下载头像文件。
头像/试听地址保留在目录中，由 Electron 端按需加载；网络不可用时直接使用
上一次成功缓存的目录，避免音色选择器因为接口暂时不可达而变空。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Iterable


SPEAKER_FLAT_LIST_URL = (
    "https://peiyin.xunfei.cn/video-api/proxy-zhizuo/api/asset/speaker/flat/list"
)
SPEAKER_COMMON_LIST_URL = (
    "https://peiyin.xunfei.cn/video-api/proxy-zhizuo/api/asset/speaker/common/list"
)
# 旧名称保留给外部调用方；实现已经切换到网页多人配音实际使用的
# speaker/common/list 接口，不再调用历史的 qry_common_speakers 接口。
COMMON_SPEAKERS_URL = SPEAKER_COMMON_LIST_URL

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://peiyin.xunfei.cn/make",
    "Origin": "https://peiyin.xunfei.cn",
}

DEFAULT_FEMALE_KEY = "amanda"
DEFAULT_MALE_KEY = "george"


def _provider_success_code(value: Any) -> bool:
    """兼容目录接口返回数字或字符串形式的成功码。"""
    return value is not None and str(value).strip() in {"0", "000000", "200"}


def _request_json(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 10,
    retries: int = 2,
) -> dict[str, Any] | None:
    """请求 JSON，短重试后返回 None；不把网络异常传播到启动流程。"""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    payload = None
    headers = dict(HEADERS)
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    for attempt in range(max(1, retries)):
        try:
            request = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method=method,
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                decoded = response.read().decode("utf-8")
            parsed = json.loads(decoded)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            if attempt + 1 < max(1, retries):
                time.sleep(0.35 * (attempt + 1))
    return None


def fetch_flat_list_speakers(timeout: float = 10) -> list[dict[str, Any]]:
    """按 fetch_xunfei_voices.py 的 flat/list 接口抓取完整音色列表。"""
    records: list[dict[str, Any]] = []
    page = 1
    size = 100
    max_pages = 50

    while page <= max_pages:
        data = _request_json(
            SPEAKER_FLAT_LIST_URL,
            params={"current": page, "size": size, "scope": "common"},
            timeout=timeout,
        )
        if not data or not _provider_success_code(data.get("code")):
            break
        payload = data.get("data") or {}
        page_records = payload.get("records") or []
        if not isinstance(page_records, list) or not page_records:
            break
        records.extend(item for item in page_records if isinstance(item, dict))

        total = int(payload.get("total") or 0)
        pages = int(payload.get("pages") or 0)
        if (pages and page >= pages) or len(page_records) < size:
            break
        if total and len(records) >= total:
            break
        page += 1

    return records


def fetch_common_list_speakers(timeout: float = 10) -> list[dict[str, Any]]:
    """抓取多人配音弹窗使用的基础音色列表。

    讯飞网页的普通音色选择器使用 ``speaker/flat/list`` 返回具体变体，
    但“多人配音”弹窗实际请求的是 ``speaker/common/list``，返回的是
    ``commonId + speakerName`` 基础音色。两者不能混作同一套展示名称，
    否则配置页选择的 ``欣畅-Pro+`` 无法在多人配音弹窗中搜索到。
    """
    records: list[dict[str, Any]] = []
    page = 1
    size = 20  # 与讯飞多人配音弹窗当前请求保持一致。
    max_pages = 50

    while page <= max_pages:
        data = _request_json(
            SPEAKER_COMMON_LIST_URL,
            params={"current": page, "size": size},
            timeout=timeout,
        )
        if not data or not _provider_success_code(data.get("code")):
            break
        payload = data.get("data") or {}
        page_records = payload.get("records") or []
        if not isinstance(page_records, list) or not page_records:
            break
        records.extend(item for item in page_records if isinstance(item, dict))

        total = int(payload.get("total") or 0)
        pages = int(payload.get("pages") or 0)
        if (pages and page >= pages) or len(page_records) < size:
            break
        if total and len(records) >= total:
            break
        page += 1

    return records


def fetch_common_speakers(timeout: float = 10) -> list[dict[str, Any]]:
    """兼容旧调用名，返回多人配音弹窗的基础音色列表。"""
    return fetch_common_list_speakers(timeout=timeout)


def merge_speakers(
    flat_speakers: Iterable[dict[str, Any]],
    common_speakers: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并两个接口结果，优先保留 flat/list 的完整字段。"""
    merged: dict[str, dict[str, Any]] = {}
    for speaker in flat_speakers:
        speaker_no = speaker.get("speakerNo")
        if speaker_no is None or speaker_no == "":
            continue
        key = str(speaker_no)
        merged[key] = {**speaker, "_source": "flat_list"}

    for speaker in common_speakers:
        speaker_no = speaker.get("speakerNo")
        common_id = speaker.get("commonId")
        key = str(speaker_no) if speaker_no not in (None, "") else f"common_{common_id}"
        existing = merged.get(key)
        if existing is None:
            merged[key] = {**speaker, "_source": "common_speakers"}
            continue
        for field, value in speaker.items():
            if not existing.get(field) and value:
                existing[field] = value

    return list(merged.values())


def _raw_speaker_no(raw: dict[str, Any]) -> Any:
    return raw.get("speakerNo") or raw.get("speaker_no")


def _raw_common_speaker(raw: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("commonSpeaker")
    return value if isinstance(value, dict) else {}


def _raw_common_id(raw: dict[str, Any]) -> Any:
    common_speaker = _raw_common_speaker(raw)
    return (
        raw.get("commonId")
        or raw.get("common_id")
        or common_speaker.get("commonId")
        or common_speaker.get("common_id")
    )


def _raw_common_name(raw: dict[str, Any]) -> str:
    common_speaker = _raw_common_speaker(raw)
    return str(
        raw.get("composite_name")
        or raw.get("common_name")
        or raw.get("commonName")
        or common_speaker.get("speakerName")
        or common_speaker.get("speaker_name")
        or raw.get("speakerName")
        or raw.get("speaker_name")
        or raw.get("name")
        or ""
    ).strip()


def _common_group_key(raw: dict[str, Any]) -> str:
    common_id = _raw_common_id(raw)
    if common_id not in (None, ""):
        return f"id:{common_id}"
    name = _raw_common_name(raw).casefold()
    return f"name:{name}" if name else ""


def _variant_name(raw: dict[str, Any]) -> str:
    return str(
        raw.get("variant_name")
        or raw.get("variantName")
        or raw.get("speakerName")
        or raw.get("speaker_name")
        or raw.get("name")
        or ""
    ).strip()


def _variant_label(raw: dict[str, Any]) -> str:
    """返回多人配音详情面板展示的具体变体标签。"""
    return str(
        raw.get("variant_label")
        or raw.get("variantLabel")
        or raw.get("emotDesc")
        or raw.get("emot_desc")
        or ""
    ).strip()


def build_composite_speakers(
    common_speakers: Iterable[dict[str, Any]],
    flat_speakers: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 common/list 基础音色和 flat/list 具体音色拼成可提交目录。

    common/list 是多人配音页面真正使用的展示来源，但它只返回
    ``commonId``，不返回提交多人作品必需的 ``speakerNo``。因此每个基础
    音色从 flat/list 中取网页默认的第一个具体变体作为提交标识，同时把
    基础名称保存在 ``speakerName``/``composite_name`` 中。这样前端显示、
    多人配音搜索和最终提交分别使用同一组正确的字段。

    common/list 偶尔会包含已经没有 flat 变体的历史记录；这类记录不放入
    可生成目录，避免用户选择后才在提交阶段收到缺少 speakerNo 的错误。
    """
    flat_records = [item for item in flat_speakers if isinstance(item, dict)]
    flat_by_group: dict[str, list[dict[str, Any]]] = {}
    for item in flat_records:
        group_key = _common_group_key(item)
        if group_key:
            flat_by_group.setdefault(group_key, []).append(item)

    common_records = [item for item in common_speakers if isinstance(item, dict)]
    result: list[dict[str, Any]] = []
    seen_groups: set[str] = set()

    def make_group(common: dict[str, Any], variants: list[dict[str, Any]], source: str):
        variants = [
            item for item in variants
            if _raw_speaker_no(item) not in (None, "")
        ]
        if not variants:
            return None
        primary = variants[0]
        speaker_no = _raw_speaker_no(primary)
        common_name = _raw_common_name(common)
        if not common_name:
            common_name = _raw_common_name(primary)
        if not common_name:
            return None
        variant_names = [_variant_name(item) for item in variants]
        variant_names = [item for item in variant_names if item]
        variant_labels = [_variant_label(item) for item in variants]
        variant_keys = [
            f"speaker:{_raw_speaker_no(item)}"
            for item in variants
            if _raw_speaker_no(item) not in (None, "")
        ]
        merged = dict(primary)
        # 基础音色的展示字段来自 common/list；试听地址等变体字段仍来自
        # flat/list 的主变体，前端切换到普通模式时仍可继续试听。
        merged["speakerName"] = common_name
        merged["commonId"] = _raw_common_id(common) or _raw_common_id(primary)
        merged["commonSpeaker"] = dict(common)
        for field in (
            "speakerGender", "speakerLanguage", "speakerStyle", "speakerDesc",
            "vipType", "tag", "label", "isTrain", "speakerSpecialty", "imgUrl",
        ):
            if common.get(field) not in (None, ""):
                merged[field] = common[field]
        merged["_source"] = source
        merged["_composite_name"] = common_name
        merged["_composite_variant_names"] = variant_names
        merged["_composite_variant_labels"] = variant_labels
        merged["_composite_variant_keys"] = variant_keys
        merged["_composite_primary_variant_name"] = _variant_name(primary)
        # 保持现有 Amanda/George 配置 key 不变，避免升级后默认配置产生重复
        # 角色；普通变体的 key 仍然是 speaker:<speakerNo>。
        primary_name = _variant_name(primary).casefold()
        if primary_name == "amanda":
            merged["key"] = DEFAULT_FEMALE_KEY
        elif primary_name == "george":
            merged["key"] = DEFAULT_MALE_KEY
        return merged

    if common_records:
        for common in common_records:
            group_key = _common_group_key(common)
            variants = flat_by_group.get(group_key, []) if group_key else []
            if not variants:
                # 少数旧数据 commonId 类型可能不一致；名称匹配只作为
                # 兼容兜底，不能跨不同基础音色合并同名变体。
                common_name = _raw_common_name(common).casefold()
                variants = [
                    item for item in flat_records
                    if _raw_common_name(item).casefold() == common_name
                ]
            merged = make_group(common, variants, "common_list")
            if merged is None:
                continue
            result.append(merged)
            if group_key:
                seen_groups.add(group_key)
        return result

    # common/list 暂时不可达时仍从 flat/list 的 commonSpeaker 字段构造一份
    # 降级目录。它不是首选来源，但可以让应用在接口短暂失败时继续工作。
    grouped: dict[str, list[dict[str, Any]]] = {}
    group_first: dict[str, dict[str, Any]] = {}
    for item in flat_records:
        group_key = _common_group_key(item)
        if not group_key or group_key in seen_groups:
            continue
        grouped.setdefault(group_key, []).append(item)
        group_first.setdefault(group_key, item)
    for group_key, variants in grouped.items():
        common = _raw_common_speaker(group_first[group_key])
        if not common:
            common = {
                "commonId": _raw_common_id(group_first[group_key]),
                "speakerName": _raw_common_name(group_first[group_key]),
            }
        merged = make_group(common, variants, "flat_list_fallback")
        if merged is not None:
            result.append(merged)
    return result


def _text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_text_values(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_text_values(item))
        return result
    text = str(value).strip()
    return [text] if text else []


def _tag_values(value: Any) -> list[str]:
    """拆分讯飞的 |标签|、JSON label 和普通数组字段。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                return _tag_values(json.loads(text))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return [part for part in re.split(r"[|、,，/]+", text) if part.strip()]
    if isinstance(value, dict):
        if "text" in value:
            return _tag_values(value.get("text"))
        result: list[str] = []
        for item in value.values():
            result.extend(_tag_values(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_tag_values(item))
        return result
    return _text_values(value)


def _unique_text(values: Iterable[str], limit: int = 12) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw).strip()).replace("主播", "音色")
        if not value or value.isdigit() or value in seen or len(value) > 32:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _gender_value(raw: Any) -> str:
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        return {1: "male", 2: "female"}.get(int(raw), "")
    text = str(raw or "").strip().lower()
    if text in {"1", "male", "man", "男", "男声"}:
        return "male"
    if text in {"2", "female", "woman", "女", "女声"}:
        return "female"
    return ""


def _stable_key(raw: dict[str, Any], name: str) -> str:
    if name.casefold() == "amanda":
        return DEFAULT_FEMALE_KEY
    if name.casefold() == "george":
        return DEFAULT_MALE_KEY
    explicit_key = str(raw.get("key") or "").strip()
    if explicit_key:
        return explicit_key[:160]
    speaker_no = raw.get("speakerNo") or raw.get("speaker_no")
    if speaker_no not in (None, ""):
        return f"speaker:{speaker_no}"
    common_id = raw.get("commonId") or raw.get("common_id")
    if common_id not in (None, ""):
        return f"common:{common_id}"
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", name.casefold()).strip("-")
    return f"name:{slug or 'voice'}"


def normalize_voice(raw: dict[str, Any]) -> dict[str, Any] | None:
    """把 API/缓存字段收敛成 Electron 直接消费的稳定结构。"""
    name = str(
        raw.get("speakerName") or raw.get("speaker_name") or raw.get("name") or ""
    ).strip()
    if not name:
        return None

    gender = _gender_value(raw.get("speakerGender") or raw.get("gender"))
    common_speaker = raw.get("commonSpeaker")
    if not isinstance(common_speaker, dict):
        common_speaker = {}
    language_values = _tag_values(
        raw.get("speakerLanguage")
        or raw.get("languageName")
        or raw.get("language")
        or common_speaker.get("speakerLanguage")
    )
    tag_values: list[str] = []
    for field in (
        "tags", "tag",
        "labels", "label",
        "speakerTags",
        "speakerLabels",
        "speakerStyle",
        "speakerSpecialty",
        "style",
        "speakerType",
        "category",
        "categories",
    ):
        tag_values.extend(_tag_values(raw.get(field)))
    for field in ("tag", "label", "speakerStyle", "speakerSpecialty", "type"):
        tag_values.extend(_tag_values(common_speaker.get(field)))
    tags = _unique_text(tag_values)
    language = _unique_text(language_values, limit=3)
    categories = _unique_text([*tags, *language], limit=16)

    gender_label = {"female": "女声", "male": "男声"}.get(gender)
    if gender_label and gender_label not in categories:
        categories.insert(0, gender_label)

    key = _stable_key(raw, name)
    common_id = _raw_common_id(raw)
    composite_name = _raw_common_name(raw) or name
    variant_name = str(
        raw.get("variant_name")
        or raw.get("variantName")
        or raw.get("_composite_primary_variant_name")
        or name
    ).strip()
    variant_names = _unique_text(
        raw.get("variant_names")
        or raw.get("_composite_variant_names")
        or ([variant_name] if variant_name else [])
    )
    variant_labels = [
        str(value).strip()
        for value in (
            raw.get("variant_labels")
            or raw.get("_composite_variant_labels")
            or []
        )
    ]
    variant_keys = [
        str(value).strip()
        for value in (
            raw.get("variant_keys")
            or raw.get("_composite_variant_keys")
            or []
        )
        if str(value).strip()
    ]
    speaker_no = _raw_speaker_no(raw)
    speaker_language = (
        raw.get("speakerLanguage")
        or raw.get("languageName")
        or raw.get("language")
        or common_speaker.get("speakerLanguage")
        or ""
    )
    return {
        "key": key,
        "speaker_no": speaker_no,
        "common_id": common_id,
        "name": name,
        # common/list 的基础名称是多人配音弹窗的搜索键；flat/list 的
        # name 仍保留具体变体，供普通模式和兼容旧配置使用。
        "composite_name": composite_name,
        "variant_name": variant_name,
        "variant_names": variant_names,
        "variant_labels": variant_labels,
        "variant_keys": variant_keys,
        "composite_key": str(raw.get("composite_key") or key).strip(),
        "emot_desc": str(raw.get("emotDesc") or raw.get("emot_desc") or "").strip(),
        "gender": gender or "unknown",
        "gender_label": gender_label or "音色",
        "language": language,
        "speaker_language": speaker_language,
        "vcn_type": raw.get("vcnType") or raw.get("vcn_type") or common_speaker.get("vcnType") or 1,
        "is_vip": raw.get("isVip") if "isVip" in raw else raw.get("is_vip"),
        "tags": tags,
        "categories": categories,
        "img_url": str(raw.get("imgUrl") or raw.get("img_url") or "").strip(),
        "audio_url": str(raw.get("audioUrl") or raw.get("audio_url") or "").strip(),
        "icon_file": str(raw.get("iconFile") or raw.get("icon_file") or "").strip(),
        "source": str(raw.get("_source") or raw.get("source") or "").strip(),
    }


def _fallback_voice(key: str, name: str, gender: str) -> dict[str, Any]:
    # 这两个默认音色还要用于多人配音 payload；即使在线目录与本地缓存都
    # 暂时不可用，也不能因为 speakerNo 缺失而让默认模式无法提交。
    fallback_speaker_no = {
        DEFAULT_FEMALE_KEY: 544508087,
        DEFAULT_MALE_KEY: 593031758,
    }.get(key)
    return {
        "key": key,
        "speaker_no": fallback_speaker_no,
        "common_id": None,
        "name": name,
        "composite_name": name,
        "variant_name": name,
        "variant_names": [name],
        "variant_labels": [],
        "variant_keys": [key],
        "composite_key": key,
        "emot_desc": "",
        "gender": gender,
        "gender_label": "女声" if gender == "female" else "男声",
        "language": ["英语"],
        "speaker_language": "英语",
        "vcn_type": 1,
        "is_vip": False,
        "tags": ["英语"],
        "categories": ["女声" if gender == "female" else "男声", "英语"],
        "img_url": "",
        "audio_url": "",
        "icon_file": "",
        "source": "builtin",
    }


def _normalize_voice_entries(
    raw_voices: Iterable[dict[str, Any]],
    *,
    source: str,
    sort: bool = True,
) -> list[dict[str, Any]]:
    voices_by_key: dict[str, dict[str, Any]] = {}
    for raw in raw_voices:
        if not isinstance(raw, dict):
            continue
        voice = normalize_voice(raw)
        if voice is None:
            continue
        voice["source"] = voice["source"] or source
        voices_by_key.setdefault(voice["key"], voice)

    # 即使服务端本次返回不完整，默认角色也始终可以配置；实际生成时仍按
    # Amanda/George 的可见名称向讯飞页面选择音色。
    voices_by_key.setdefault(
        DEFAULT_FEMALE_KEY,
        _fallback_voice(DEFAULT_FEMALE_KEY, "Amanda", "female"),
    )
    voices_by_key.setdefault(
        DEFAULT_MALE_KEY,
        _fallback_voice(DEFAULT_MALE_KEY, "George", "male"),
    )

    voices = list(voices_by_key.values())
    if sort:
        voices.sort(
            key=lambda item: (
                item["key"] not in {"amanda", "george"},
                item["name"].casefold(),
            )
        )
    return voices


def _build_voice_filters(voices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    voices = list(voices)
    count_by_filter: dict[str, int] = {"all": len(voices)}
    for voice in voices:
        if voice["gender"] in {"female", "male"}:
            count_by_filter[voice["gender"]] = count_by_filter.get(voice["gender"], 0) + 1
        for category in voice.get("categories") or []:
            if category and category not in {"女声", "男声"}:
                count_by_filter[category] = count_by_filter.get(category, 0) + 1

    filters = [{"key": "all", "label": "全部", "count": len(voices)}]
    for key, label in (("female", "女声"), ("male", "男声")):
        filters.append({"key": key, "label": label, "count": count_by_filter.get(key, 0)})
    categories = sorted(
        (
            {"key": f"tag:{label}", "label": label, "count": count}
            for label, count in count_by_filter.items()
            if label not in {"all", "female", "male"} and count > 0
        ),
        key=lambda item: (-item["count"], item["label"]),
    )
    preferred_labels = (
        "最热", "最新", "新闻播报", "解说", "商业广告", "情感陪伴",
        "教育课件", "角色演绎", "客服对话", "短视频", "地道方言",
        "多语种", "超拟人", "童声",
    )
    preferred = [item for item in categories if item["label"] in preferred_labels]
    preferred_keys = {item["key"] for item in preferred}
    filters.extend(preferred)
    filters.extend(item for item in categories if item["key"] not in preferred_keys)
    return filters[:20]


def normalize_catalog(
    raw_voices: Iterable[dict[str, Any]],
    *,
    composite_raw_voices: Iterable[dict[str, Any]] | None = None,
    fetched_at: str | None = None,
    source: str = "cache",
) -> dict[str, Any]:
    """规范化普通音色目录和多人配音基础音色目录。

    ``voices`` 保留 flat/list 的具体变体，兼容单条生成；显式传入
    ``composite_raw_voices`` 时，``composite_voices`` 使用 common/list 的
    基础音色。两套目录共享具体变体 key，因此切换生成方式不会丢失用户
    已选配置。
    """
    raw_voice_list = list(raw_voices)
    voices = _normalize_voice_entries(raw_voice_list, source=source, sort=True)
    composite_inputs = raw_voice_list if composite_raw_voices is None else composite_raw_voices
    composite_voices = _normalize_voice_entries(
        composite_inputs,
        source=source,
        # common/list 返回顺序就是讯飞多人配音弹窗的展示顺序。
        sort=composite_raw_voices is None,
    )

    # 将每个 flat 变体关联到 common/list 中对应的基础目录项。旧版本缓存
    # 没有这些字段时会自然保留原值，在线刷新成功后即可补齐。
    composite_by_common_id = {
        str(voice.get("common_id")): voice
        for voice in composite_voices
        if voice.get("common_id") not in (None, "")
    }
    for voice in voices:
        common_id = voice.get("common_id")
        group = composite_by_common_id.get(str(common_id)) if common_id not in (None, "") else None
        if group is None:
            continue
        voice["composite_key"] = group["key"]
        voice["composite_name"] = group["name"]
        voice["variant_names"] = list(group.get("variant_names") or voice.get("variant_names") or [])
        voice["variant_keys"] = list(group.get("variant_keys") or voice.get("variant_keys") or [])

    filters = _build_voice_filters(voices)
    composite_filters = _build_voice_filters(composite_voices)

    return {
        "_meta": {
            "description": "讯飞配音音色列表",
            "source": "https://peiyin.xunfei.cn/make",
            "fetched_at": fetched_at or datetime.now().isoformat(),
            "total_count": len(voices),
            "composite_count": len(composite_voices),
            "catalog_source": source,
            "flat_list_endpoint": SPEAKER_FLAT_LIST_URL,
            "composite_list_endpoint": SPEAKER_COMMON_LIST_URL,
        },
        "voices": voices,
        "filters": filters,
        "composite_voices": composite_voices,
        "composite_filters": composite_filters,
    }


def _cache_candidates(base_dir: str, resource_dir: str | None) -> list[str]:
    # cache/voices.json 是新的可写缓存；resources/voices.json 是只读种子资源。
    # 旧版 base_dir/resources 和 xunfei_voices 路径继续兼容读取，避免升级后
    # 丢失已经下载的音色目录。
    candidates = [
        os.path.join(base_dir, "cache", "voices.json"),
        os.path.join(base_dir, "resources", "voices.json"),
        os.path.join(base_dir, "xunfei_voices", "voices.json"),
    ]
    if resource_dir:
        for folder in ("resources", "xunfei_voices"):
            resource_path = os.path.join(resource_dir, folder, "voices.json")
            if resource_path not in candidates:
                candidates.append(resource_path)
    return candidates


def load_cached_catalog(base_dir: str, resource_dir: str | None = None) -> dict[str, Any] | None:
    for path in _cache_candidates(base_dir, resource_dir):
        try:
            with open(path, "r", encoding="utf-8") as source:
                payload = json.load(source)
            if not isinstance(payload, dict) or not isinstance(payload.get("voices"), list):
                continue
            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            cached_composite = payload.get("composite_voices")
            if not isinstance(cached_composite, list):
                cached_composite = None
            return normalize_catalog(
                payload["voices"],
                composite_raw_voices=cached_composite,
                fetched_at=str(meta.get("fetched_at") or "") or None,
                source="cache",
            )
        except (OSError, ValueError, TypeError):
            continue
    return None


def save_catalog(catalog: dict[str, Any], base_dir: str) -> str:
    output_dir = os.path.join(base_dir, "cache")
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "voices.json")
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as target:
        json.dump(catalog, target, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)
    return path


def refresh_catalog(
    base_dir: str,
    resource_dir: str | None = None,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    """刷新远端目录并写入可写数据目录；失败时回退缓存。"""
    flat = fetch_flat_list_speakers(timeout=timeout)
    common = fetch_common_list_speakers(timeout=timeout)
    composite = build_composite_speakers(common, flat)
    if not flat and not composite:
        raise RuntimeError("讯飞音色接口未返回有效目录")
    catalog = normalize_catalog(
        flat,
        composite_raw_voices=composite,
        fetched_at=datetime.now().isoformat(),
        source="live",
    )
    save_catalog(catalog, base_dir)
    return catalog


def load_or_refresh_catalog(
    base_dir: str,
    resource_dir: str | None = None,
    *,
    force_refresh: bool = True,
) -> dict[str, Any]:
    """应用启动时优先刷新；接口失败则返回最近一次缓存并带 error。"""
    error = ""
    if force_refresh:
        try:
            return refresh_catalog(base_dir, resource_dir)
        except Exception as exc:
            error = str(exc)

    cached = load_cached_catalog(base_dir, resource_dir)
    if cached is not None:
        cached["_meta"]["catalog_source"] = "cache"
        if error:
            cached["_meta"]["refresh_error"] = error[:240]
        return cached

    fallback = normalize_catalog([], source="builtin")
    fallback["_meta"]["refresh_error"] = error or "未找到本地音色缓存"
    return fallback
