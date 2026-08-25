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
COMMON_SPEAKERS_URL = (
    "https://peiyin.xunfei.cn/video-api/asset/qry_common_speakers"
)

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
        if not data or data.get("code") != 0:
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
    """抓取推荐音色；推荐接口失败不影响 flat/list 目录使用。"""
    body = {
        "param": {"pageSize": 999, "needCount": 1},
        "base": {
            "appid": "xfpy",
            "sid": "",
            "channelId": "40000001",
            "userId": "",
            "osid": 0,
        },
    }
    data = _request_json(
        COMMON_SPEAKERS_URL,
        method="POST",
        body=body,
        timeout=timeout,
    )
    if not data or data.get("code") != 0:
        return []
    records = (data.get("data") or {}).get("commonSpeakers") or []
    return [item for item in records if isinstance(item, dict)]


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
    return {
        "key": key,
        "speaker_no": raw.get("speakerNo") or raw.get("speaker_no"),
        "common_id": raw.get("commonId") or raw.get("common_id"),
        "name": name,
        "gender": gender or "unknown",
        "gender_label": gender_label or "音色",
        "language": language,
        "tags": tags,
        "categories": categories,
        "img_url": str(raw.get("imgUrl") or raw.get("img_url") or "").strip(),
        "audio_url": str(raw.get("audioUrl") or raw.get("audio_url") or "").strip(),
        "icon_file": str(raw.get("iconFile") or raw.get("icon_file") or "").strip(),
        "source": str(raw.get("_source") or raw.get("source") or "").strip(),
    }


def _fallback_voice(key: str, name: str, gender: str) -> dict[str, Any]:
    return {
        "key": key,
        "speaker_no": None,
        "common_id": None,
        "name": name,
        "gender": gender,
        "gender_label": "女声" if gender == "female" else "男声",
        "language": ["英语"],
        "tags": ["英语"],
        "categories": ["女声" if gender == "female" else "男声", "英语"],
        "img_url": "",
        "audio_url": "",
        "icon_file": "",
        "source": "builtin",
    }


def normalize_catalog(
    raw_voices: Iterable[dict[str, Any]],
    *,
    fetched_at: str | None = None,
    source: str = "cache",
) -> dict[str, Any]:
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
    voices.sort(key=lambda item: (item["key"] not in {"amanda", "george"}, item["name"].casefold()))

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
    filters = filters[:20]

    return {
        "_meta": {
            "description": "讯飞配音音色列表",
            "source": "https://peiyin.xunfei.cn/make",
            "fetched_at": fetched_at or datetime.now().isoformat(),
            "total_count": len(voices),
            "catalog_source": source,
        },
        "voices": voices,
        "filters": filters,
    }


def _cache_candidates(base_dir: str, resource_dir: str | None) -> list[str]:
    candidates = [os.path.join(base_dir, "xunfei_voices", "voices.json")]
    if resource_dir:
        resource_path = os.path.join(resource_dir, "xunfei_voices", "voices.json")
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
            return normalize_catalog(
                payload["voices"],
                fetched_at=str(meta.get("fetched_at") or "") or None,
                source="cache",
            )
        except (OSError, ValueError, TypeError):
            continue
    return None


def save_catalog(catalog: dict[str, Any], base_dir: str) -> str:
    output_dir = os.path.join(base_dir, "xunfei_voices")
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
    common = fetch_common_speakers(timeout=timeout)
    merged = merge_speakers(flat, common)
    if not merged:
        raise RuntimeError("讯飞音色接口未返回有效目录")
    catalog = normalize_catalog(
        merged,
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
