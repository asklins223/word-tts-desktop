"""讯飞配音音色目录：启动刷新、规范化和本地缓存。

这个模块只负责读取讯飞公开音色接口和维护 JSON 缓存，不下载头像文件。
头像/试听地址保留在目录中，由 Electron 端按需加载；网络不可用时直接使用
上一次成功缓存的目录，避免音色选择器因为接口暂时不可达而变空。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Iterable


SPEAKER_FLAT_LIST_URL = (
    "https://peiyin.xunfei.cn/video-api/proxy-zhizuo/api/asset/speaker/flat/list"
)
SPEAKER_COMMON_LIST_URL = (
    "https://peiyin.xunfei.cn/video-api/proxy-zhizuo/api/asset/speaker/common/list"
)
# 多人配音页面的标签筛选接口。该接口需要网页登录态；没有登录态时，
# common/list 每条记录自带的 tag/label 会作为本地筛选兜底。
SPEAKER_TAGS_URL = "https://peiyin.xunfei.cn/video-api/asset/qry_tags"
# 旧名称保留给外部调用方。
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
DEFAULT_FEMALE_NAME = "英语-Amanda"
DEFAULT_MALE_NAME = "英语-George"

# 音色卡片和分类按钮共用这组业务优先级；同一语言分组内继续保留接口
# 返回顺序，两个内置默认音色会额外置于英语分组最前。
VOICE_LANGUAGE_PRIORITY = (
    ("英语", ("英语", "english")),
    ("多语种", ("多语种", "multilingual")),
)

# qry_tags 的结果是相对稳定的筛选字典。抓取过一次后固定快照，避免每次
# 启动都依赖登录态请求；每次 common/list 刷新仍会按当前音色数量计算标签
# count，因此新音色仍能正确显示在对应分类中。这里保留接口返回的父子层级、
# id、higherId、tagValue 和 useCount，避免只保存展示名称而丢掉标签元数据。
FIXED_TAG_CATEGORIES = (
    {
        "id": "1011000",
        "tagName": "最热",
        "rank": 1,
        "tagValue": None,
        "tagList": [{
            "id": "1011000", "tagType": 1, "tagIntro": "最热", "iconUrl": "",
            "useCount": 25, "rank": 1, "higherId": "", "tagName": "最热",
        }],
    },
    {
        "id": "1050001",
        "tagName": "最新",
        "rank": 1,
        "tagValue": None,
        "tagList": [{
            "id": "1050001", "tagType": 1, "tagIntro": "最新", "iconUrl": "",
            "useCount": 173, "rank": 1, "higherId": "1050000", "tagName": "最新",
        }],
    },
    {
        "id": "1010031",
        "tagName": "超拟人",
        "rank": 1,
        "tagValue": None,
        "tagList": [{
            "id": "1010031", "tagType": 1, "tagIntro": "超拟人", "iconUrl": "",
            "useCount": 0, "rank": 1, "higherId": "1010000", "tagName": "超拟人",
        }],
    },
    {
        "id": "1010010",
        "tagName": "解说",
        "rank": 1,
        "tagValue": 0,
        "tagList": [
            {"id": "1011010", "tagType": 1, "tagIntro": "教育培训", "iconUrl": "", "useCount": 8, "rank": 2, "higherId": "1010010", "tagName": "教育培训"},
            {"id": "1011011", "tagType": 1, "tagIntro": "有声阅读", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1010010", "tagName": "有声阅读"},
            {"id": "1011013", "tagType": 1, "tagIntro": "体育解说", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1010010", "tagName": "体育解说"},
            {"id": "1011014", "tagType": 1, "tagIntro": "游戏解说", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1010010", "tagName": "游戏解说"},
            {"id": "1011016", "tagType": 1, "tagIntro": "纪录片", "iconUrl": "", "useCount": 19, "rank": 2, "higherId": "1010010", "tagName": "纪录片"},
            {"id": "1011017", "tagType": 1, "tagIntro": "情感", "iconUrl": "", "useCount": 14, "rank": 2, "higherId": "1010010", "tagName": "情感"},
            {"id": "1011019", "tagType": 1, "tagIntro": "短视频", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1010010", "tagName": "短视频"},
        ],
    },
    {
        "id": "1011001",
        "tagName": "新闻主持",
        "rank": 1,
        "tagValue": None,
        "tagList": [
            {"id": "1012001", "tagType": 1, "tagIntro": "大会主持", "iconUrl": "", "useCount": 4, "rank": 2, "higherId": "1011001", "tagName": "大会主持"},
            {"id": "1012002", "tagType": 1, "tagIntro": "新闻", "iconUrl": "", "useCount": 38, "rank": 2, "higherId": "1011001", "tagName": "新闻"},
            {"id": "1012003", "tagType": 1, "tagIntro": "资讯", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011001", "tagName": "资讯"},
        ],
    },
    {
        "id": "1011002",
        "tagName": "广告营销",
        "rank": 1,
        "tagValue": None,
        "tagList": [
            {"id": "1013002", "tagType": 1, "tagIntro": "直播", "iconUrl": "", "useCount": 4, "rank": 2, "higherId": "1011002", "tagName": "直播"},
            {"id": "1013003", "tagType": 1, "tagIntro": "广告", "iconUrl": "", "useCount": 21, "rank": 2, "higherId": "1011002", "tagName": "广告"},
        ],
    },
    {
        "id": "1010024",
        "tagName": "娱乐",
        "rank": 1,
        "tagValue": None,
        "tagList": [
            {"id": "1013027", "tagType": 1, "tagIntro": "自创特色", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1010024", "tagName": "自创特色"},
            {"id": "1013028", "tagType": 1, "tagIntro": "影视动漫", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1010024", "tagName": "影视动漫"},
        ],
    },
    {
        "id": "1011003",
        "tagName": "语音助手",
        "rank": 1,
        "tagValue": None,
        "tagList": [{
            "id": "1011003", "tagType": 1, "tagIntro": "语音助手", "iconUrl": "",
            "useCount": 7, "rank": 1, "higherId": "", "tagName": "语音助手",
        }],
    },
    {
        "id": "1020002",
        "tagName": "方言",
        "rank": 1,
        "tagValue": 0,
        "tagList": [{
            "id": "1020002", "tagType": 1, "tagIntro": "方言", "iconUrl": "",
            "useCount": 20, "rank": 1, "higherId": "1020000", "tagName": "方言",
            "tagValue": 0,
        }],
    },
    {
        "id": "1011004",
        "tagName": "多语种",
        "rank": 1,
        "tagValue": None,
        "tagList": [
            {"id": "1013005", "tagType": 1, "tagIntro": "英语", "iconUrl": "", "useCount": 18, "rank": 2, "higherId": "1011004", "tagName": "英语"},
            {"id": "1013006", "tagType": 1, "tagIntro": "俄语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "俄语"},
            {"id": "1013009", "tagType": 1, "tagIntro": "法语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "法语"},
            {"id": "1013010", "tagType": 1, "tagIntro": "西班牙语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "西班牙语"},
            {"id": "1013011", "tagType": 1, "tagIntro": "日语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "日语"},
            {"id": "1013012", "tagType": 1, "tagIntro": "韩语", "iconUrl": "", "useCount": 4, "rank": 2, "higherId": "1011004", "tagName": "韩语"},
            {"id": "1013013", "tagType": 1, "tagIntro": "德语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "德语"},
            {"id": "1013014", "tagType": 1, "tagIntro": "阿拉伯语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "阿拉伯语"},
            {"id": "1013015", "tagType": 1, "tagIntro": "泰语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "泰语"},
            {"id": "1013016", "tagType": 1, "tagIntro": "马来语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "马来语"},
            {"id": "1013017", "tagType": 1, "tagIntro": "印尼语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "印尼语"},
            {"id": "1013018", "tagType": 1, "tagIntro": "意大利语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "意大利语"},
            {"id": "1013019", "tagType": 1, "tagIntro": "菲律宾语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "菲律宾语"},
            {"id": "1013020", "tagType": 1, "tagIntro": "葡萄牙语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "葡萄牙语"},
            {"id": "1013021", "tagType": 1, "tagIntro": "越南语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "越南语"},
            {"id": "1013022", "tagType": 1, "tagIntro": "波兰语", "iconUrl": "", "useCount": 0, "rank": 2, "higherId": "1011004", "tagName": "波兰语"},
        ],
    },
    {
        "id": "1020005",
        "tagName": "童声",
        "rank": 1,
        "tagValue": None,
        "tagList": [{
            "id": "1020005", "tagType": 1, "tagIntro": "童声", "iconUrl": "",
            "useCount": 0, "rank": 1, "higherId": "1020000", "tagName": "童声",
        }],
    },
    {
        "id": "1030004",
        "tagName": "老年",
        "rank": 1,
        "tagValue": None,
        "tagList": [{
            "id": "1030004", "tagType": 1, "tagIntro": "老年", "iconUrl": "",
            "useCount": 0, "rank": 1, "higherId": "", "tagName": "老年",
        }],
    },
    {
        "id": "1030002",
        "tagName": "女声",
        "rank": 1,
        "tagValue": 2,
        "tagList": [{
            "id": "1030002", "tagType": 1, "tagIntro": "女声", "iconUrl": "",
            "useCount": 0, "rank": 1, "higherId": "1030000", "tagName": "女声",
            "tagValue": 2,
        }],
    },
    {
        "id": "1030001",
        "tagName": "男声",
        "rank": 1,
        "tagValue": 1,
        "tagList": [{
            "id": "1030001", "tagType": 1, "tagIntro": "男声", "iconUrl": "",
            "useCount": 0, "rank": 1, "higherId": "1030000", "tagName": "男声",
            "tagValue": 1,
        }],
    },
)


def _provider_success_code(value: Any) -> bool:
    """兼容目录接口返回数字或字符串形式的成功码。"""
    return value is not None and str(value).strip() in {"0", "000000", "200"}


def _safe_int(value: Any) -> int:
    """读取分页元数据；服务端偶尔会返回空值或非数字字符串。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _request_json(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
    retries: int = 2,
) -> dict[str, Any] | None:
    """请求 JSON，短重试后返回 None；不把网络异常传播到启动流程。"""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    payload = None
    request_headers = dict(HEADERS)
    if headers:
        request_headers.update(
            {
                str(key): str(value)
                for key, value in headers.items()
                if value not in (None, "")
            }
        )
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    for attempt in range(max(1, retries)):
        try:
            request = urllib.request.Request(
                url,
                data=payload,
                headers=request_headers,
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


def _canonical_sign_value(value: Any) -> str:
    """按讯飞网页 video-api 的规则生成签名原文。"""
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            child = _canonical_sign_value(value[key])
            if child or isinstance(value[key], list):
                parts.append(f"{key}={child}")
        return "&".join(parts)
    if isinstance(value, list):
        return ",".join(_canonical_sign_value(item) for item in value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_api_sign(param: dict[str, Any], base: dict[str, Any]) -> str:
    """生成 qry_tags 所需的 sign；不记录或持久化认证信息。"""
    base_digest = hashlib.md5(
        _canonical_sign_value(base).encode("utf-8")
    ).hexdigest()
    payload = {"param": param, "base": base}
    return hashlib.md5(
        (_canonical_sign_value(payload) + base_digest).encode("utf-8")
    ).hexdigest()


def fetch_flat_list_speakers(timeout: float = 10) -> list[dict[str, Any]]:
    """按 fetch_xunfei_voices.py 的 flat/list 接口抓取完整音色列表。"""
    records: list[dict[str, Any]] = []
    page = 1
    size = 40
    max_pages = 50
    completed = False

    while page <= max_pages:
        data = _request_json(
            SPEAKER_FLAT_LIST_URL,
            params={"current": page, "size": size, "scope": "common"},
            timeout=timeout,
        )
        if not data or not _provider_success_code(data.get("code")):
            break
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            break
        page_records = payload.get("records") or []
        if not isinstance(page_records, list):
            break
        if not page_records:
            completed = True
            break
        records.extend(item for item in page_records if isinstance(item, dict))

        total = _safe_int(payload.get("total"))
        pages = _safe_int(payload.get("pages"))
        if (pages and page < pages) or (total and len(records) < total):
            page += 1
            continue
        if page_records and len(page_records) >= size and not pages and not total:
            page += 1
            continue
        completed = True
        break

    # 中途请求失败时不返回半截目录，交给上层回退到最近一次成功缓存。
    return records if completed else []


def _raw_audio_url(raw: dict[str, Any]) -> str:
    """读取 flat/list 记录中的示例音频地址。"""
    for field in ("audioUrl", "audio_url", "previewAudioUrl", "preview_audio_url"):
        value = str(raw.get(field) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def merge_preview_audio(
    common_speakers: Iterable[dict[str, Any]],
    flat_speakers: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 flat/list 的示例音频合并回 common/list 基础音色卡片。

    ``common/list`` 是当前应用的展示与选择来源，记录只有基础音色的
    ``commonId``；``flat/list`` 的具体变体则通过 ``commonSpeaker.commonId``
    关联，并提供可直接播放的 ``audioUrl``。同一基础音色可能有多个变体，
    按 flat/list 返回顺序使用第一个可用示例，保持与网页默认变体一致。
    """
    by_common_id: dict[str, dict[str, Any]] = {}
    by_common_name: dict[str, dict[str, Any]] = {}

    for raw in flat_speakers:
        if not isinstance(raw, dict):
            continue
        audio_url = _raw_audio_url(raw)
        if not audio_url:
            continue
        common_id = _raw_common_id(raw)
        candidate = {
            "audioUrl": audio_url,
            "commonId": common_id,
        }
        if common_id not in (None, ""):
            by_common_id.setdefault(str(common_id), candidate)
        common_name = _raw_common_name(raw).casefold()
        if common_name:
            by_common_name.setdefault(common_name, candidate)

    merged: list[dict[str, Any]] = []
    for raw in common_speakers:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        common_id = _raw_common_id(item)
        candidate = (
            by_common_id.get(str(common_id))
            if common_id not in (None, "")
            else None
        )
        if candidate is None:
            common_name = _raw_common_name(item).casefold()
            name_candidate = by_common_name.get(common_name) if common_name else None
            candidate_common_id = name_candidate.get("commonId") if name_candidate else None
            # 名称只做兼容兜底；如果两边都提供了不同的 commonId，
            # 拒绝跨基础音色误配示例音频。
            if name_candidate and (
                common_id in (None, "")
                or candidate_common_id in (None, "")
                or str(candidate_common_id) == str(common_id)
            ):
                candidate = name_candidate
        if candidate and not _raw_audio_url(item):
            item["audioUrl"] = candidate["audioUrl"]
        merged.append(item)
    return merged


def fetch_common_list_speakers(timeout: float = 10) -> list[dict[str, Any]]:
    """抓取讯飞“多人配音”弹窗使用的基础音色卡片列表。

    这里的 ``speakerName`` 是用户在多人配音弹窗中实际搜索的名称（例如
    ``欣畅``），不是右侧普通音色栏里的具体变体名称（例如
    ``欣畅-Pro+``）。
    """
    records: list[dict[str, Any]] = []
    page = 1
    size = 20  # 与讯飞多人配音弹窗当前请求保持一致。
    max_pages = 50
    completed = False

    while page <= max_pages:
        data = _request_json(
            SPEAKER_COMMON_LIST_URL,
            params={"current": page, "size": size},
            timeout=timeout,
        )
        if not data or not _provider_success_code(data.get("code")):
            break
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            break
        page_records = payload.get("records") or []
        if not isinstance(page_records, list):
            break
        if not page_records:
            completed = True
            break
        records.extend(item for item in page_records if isinstance(item, dict))

        total = _safe_int(payload.get("total"))
        pages = _safe_int(payload.get("pages"))
        if (pages and page < pages) or (total and len(records) < total):
            page += 1
            continue
        if page_records and len(page_records) >= size and not pages and not total:
            page += 1
            continue
        completed = True
        break

    # 中途请求失败时不返回半截目录，交给上层回退到最近一次成功缓存。
    return records if completed else []


def fetch_common_speakers(timeout: float = 10) -> list[dict[str, Any]]:
    """兼容旧调用名，返回多人配音弹窗的基础音色列表。"""
    return fetch_common_list_speakers(timeout=timeout)


def _tag_credentials_from_environment() -> dict[str, Any]:
    """读取可选的讯飞标签接口认证，不把用户凭证写入代码或缓存。"""
    authorization = str(
        os.environ.get("XUNFEI_TAG_AUTHORIZATION")
        or os.environ.get("XUNFEI_AUTHORIZATION")
        or ""
    ).strip()
    user_id = str(
        os.environ.get("XUNFEI_TAG_USER_ID")
        or os.environ.get("XUNFEI_USER_ID")
        or ""
    ).strip()
    if not authorization or not user_id:
        return {}
    credentials = {
        "authorization": authorization,
        "user_id": user_id,
        "appid": os.environ.get("XUNFEI_TAG_APPID") or "xfpy",
        "channel_id": os.environ.get("XUNFEI_TAG_CHANNEL") or "40000001",
        "osid": 0,
    }
    cookie = str(
        os.environ.get("XUNFEI_TAG_COOKIE")
        or os.environ.get("XUNFEI_COOKIE")
        or ""
    ).strip()
    if cookie:
        credentials["cookie"] = cookie
    return credentials


def fetch_tag_categories(
    timeout: float = 10,
    *,
    credentials: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """获取多人配音页面的标签层级。

    ``qry_tags`` 与 ``common/list`` 不同，需要网页登录态和动态 sign。调用方
    可以传入当前页面捕获的 ``authorization``/``user_id``；桌面应用启动时
    通常尚未打开讯飞登录页，因此没有认证时直接返回空列表，由 common/list
    记录中的 ``tag`` 字段生成筛选项。
    """
    auth = dict(credentials or _tag_credentials_from_environment())
    authorization = str(auth.get("authorization") or "").strip()
    user_id = str(auth.get("user_id") or auth.get("userId") or "").strip()
    if not authorization or not user_id:
        return []

    param = {"tagType": 1}
    base = {
        "appid": str(auth.get("appid") or "xfpy"),
        "sid": str(auth.get("sid") or uuid.uuid4().hex),
        "channelId": str(auth.get("channel_id") or auth.get("channelId") or "40000001"),
        "userId": user_id,
        "osid": auth.get("osid", 0),
    }
    cookie = str(auth.get("cookie") or auth.get("cookies") or "").strip()
    data = _request_json(
        SPEAKER_TAGS_URL,
        method="POST",
        body={"param": param, "base": base},
        headers={
            "Authorization": authorization,
            "X-Channel-No": base["channelId"],
            "sign": _build_api_sign(param, base),
            "x-accept-language": "zh_CN",
            "Cookie": cookie,
        },
        timeout=timeout,
    )
    if not data or not _provider_success_code(data.get("code")):
        return []
    payload = data.get("data")
    if not isinstance(payload, dict):
        return []
    categories = payload.get("tagCategories") or []
    return [item for item in categories if isinstance(item, dict)]


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


def _catalog_entry_identity(raw: dict[str, Any]) -> str:
    """返回目录条目的稳定身份，用于识别旧版基础音色缓存。"""
    speaker_no = _raw_speaker_no(raw)
    if speaker_no not in (None, ""):
        identity = f"speaker:{speaker_no}"
    else:
        common_id = _raw_common_id(raw)
        if common_id not in (None, ""):
            identity = f"common:{common_id}"
        else:
            identity = ""
    # 旧版 build_composite_speakers 可能保留相同 speakerNo，却把展示名改成
    # 基础名称；只比较 ID 会把这种旧结构误当成当前 flat 变体列表。
    display_name = str(
        raw.get("speakerName")
        or raw.get("speaker_name")
        or raw.get("name")
        or ""
    ).strip().casefold()
    if identity and display_name:
        return f"{identity}|name:{display_name}"
    if identity:
        return identity
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
        if primary_name in {"amanda", "英语-amanda"}:
            merged["key"] = DEFAULT_FEMALE_KEY
        elif primary_name in {"george", "英语-george"}:
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


def _values_contain_label(values: Iterable[Any], labels: Iterable[str]) -> bool:
    """在一组接口字段中按不区分大小写的包含关系匹配标签。"""
    normalized_labels = [
        str(label).strip().casefold()
        for label in labels
        if str(label).strip()
    ]
    if not normalized_labels:
        return False
    for value in values:
        for text in _tag_values(value):
            normalized_text = str(text).strip().casefold()
            if normalized_text and any(label in normalized_text for label in normalized_labels):
                return True
    return False


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
    normalized_name = name.casefold()
    if normalized_name in {"amanda", "英语-amanda"}:
        return DEFAULT_FEMALE_KEY
    if normalized_name in {"george", "英语-george"}:
        return DEFAULT_MALE_KEY
    explicit_key = str(raw.get("key") or "").strip()
    # common/list 基础卡片以及旧 flat 记录里的 commonSpeaker 都以
    # commonId 作为跨变体稳定身份；speakerNo 只用于生成/试听兜底，不能
    # 再把同一个基础音色拆成多个 App 选项。
    common_id = raw.get("commonId") or raw.get("common_id")
    # 旧缓存的规范化变体带有 key=speaker:<speakerNo>；遇到 commonId 时
    # 必须丢弃这个变体 key，才能迁移到新的基础音色身份。其它显式 key
    # 仍保留，兼容内置或外部注册的自定义目录项。
    if explicit_key and not (
        common_id not in (None, "") and explicit_key.startswith("speaker:")
    ):
        return explicit_key[:160]
    if common_id not in (None, ""):
        return f"common:{common_id}"
    speaker_no = raw.get("speakerNo") or raw.get("speaker_no")
    if speaker_no not in (None, ""):
        return f"speaker:{speaker_no}"
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
    priority_categories = [
        label
        for label, aliases in VOICE_LANGUAGE_PRIORITY
        if _values_contain_label([name, *language_values, *tag_values], aliases)
    ]
    # 将排序所依据的语言分类写回规范化数据，保证分类计数和前端筛选
    # 与卡片排序使用同一套识别规则，即使接口只在名称中携带语言信息。
    categories = _unique_text([*priority_categories, *tags, *language], limit=16)

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
        # common/list 的基础名称就是多人配音弹窗的搜索键；旧 flat/list
        # 缓存经过 _legacy_common_records 后也会落到同一个字段。
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


def _voice_has_language_label(voice: dict[str, Any], labels: Iterable[str]) -> bool:
    """在名称、语言和标签中识别英语/多语种相关音色。"""
    return _values_contain_label(
        (voice.get(field) for field in ("name", "speaker_language", "language", "tags", "categories")),
        labels,
    )


def _voice_sort_key(voice: dict[str, Any]) -> tuple[int, int]:
    """英语优先、多语种其次；同组内保持 common/list 的原始顺序。"""
    if voice.get("key") in {DEFAULT_FEMALE_KEY, DEFAULT_MALE_KEY}:
        # 这两个内置 key 的业务含义固定为英语，即使上游偶发省略语言字段，
        # 也不能让默认音色掉到普通音色之后。
        language_priority = 0
    else:
        language_priority = len(VOICE_LANGUAGE_PRIORITY)
        for priority, (_label, aliases) in enumerate(VOICE_LANGUAGE_PRIORITY):
            if _voice_has_language_label(voice, aliases):
                language_priority = priority
                break
    default_priority = {
        DEFAULT_FEMALE_KEY: 0,
        DEFAULT_MALE_KEY: 1,
    }.get(voice.get("key"), 2)
    return language_priority, default_priority


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
        _fallback_voice(DEFAULT_FEMALE_KEY, DEFAULT_FEMALE_NAME, "female"),
    )
    voices_by_key.setdefault(
        DEFAULT_MALE_KEY,
        _fallback_voice(DEFAULT_MALE_KEY, DEFAULT_MALE_NAME, "male"),
    )

    voices = list(voices_by_key.values())
    if sort:
        voices.sort(key=_voice_sort_key)
    return voices


def _raw_voice_key(raw: dict[str, Any]) -> str:
    """读取原始/旧缓存条目的 key，用于生成不可见的兼容别名。"""
    explicit = str(raw.get("key") or raw.get("voice_key") or "").strip()
    if explicit:
        return explicit[:160]
    speaker_no = _raw_speaker_no(raw)
    if speaker_no not in (None, ""):
        return f"speaker:{speaker_no}"
    common_id = _raw_common_id(raw)
    if common_id not in (None, ""):
        return f"common:{common_id}"
    return ""


def _build_voice_aliases(
    voices: Iterable[dict[str, Any]],
    *legacy_sources: Iterable[dict[str, Any]] | None,
) -> dict[str, str]:
    """把旧 flat/list key 映射到当前 common/list key，但不暴露为列表项。"""
    by_common_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for voice in voices:
        if not isinstance(voice, dict):
            continue
        key = str(voice.get("key") or "").strip()
        if not key:
            continue
        common_id = voice.get("common_id") or voice.get("commonId")
        if common_id not in (None, ""):
            by_common_id.setdefault(str(common_id), key)
        for name in (
            voice.get("name"),
            voice.get("composite_name"),
            voice.get("compositeName"),
        ):
            normalized = str(name or "").strip().casefold()
            if normalized:
                by_name.setdefault(normalized, key)

    aliases: dict[str, str] = {}
    for source in legacy_sources:
        if source is None:
            continue
        for raw in source:
            if not isinstance(raw, dict):
                continue
            alias = _raw_voice_key(raw)
            if not alias:
                continue
            common_id = _raw_common_id(raw)
            target = (
                by_common_id.get(str(common_id))
                if common_id not in (None, "")
                else None
            )
            if not target:
                name = _raw_common_name(raw).casefold()
                target = by_name.get(name) if name else None
            if target and alias != target and alias not in aliases:
                aliases[alias] = target
            if len(aliases) >= 4096:
                return aliases
    return aliases


def _looks_like_common_list(raw_voices: Iterable[dict[str, Any]]) -> bool:
    """判断一组记录是否已经是 common/list 的基础音色卡片。"""
    records = [item for item in raw_voices if isinstance(item, dict)]
    if not records:
        return False
    has_common_card = any(
        _raw_common_id(item) not in (None, "")
        and _raw_speaker_no(item) in (None, "")
        for item in records
    )
    has_variant = any(_raw_speaker_no(item) not in (None, "") for item in records)
    return has_common_card and not has_variant


def _legacy_common_records(
    raw_voices: Iterable[dict[str, Any]],
    preferred_voices: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把旧版 flat/list 或已规范化变体缓存降级为基础音色卡片。

    旧缓存里同时存在两种结构：原始 flat 记录带 ``commonSpeaker``，规范化
    记录则把基础名称保存为 ``composite_name``。这里只用于离线回退，不会
    影响在线 common/list 的首选数据源。
    """
    records: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}

    def append_record(raw: dict[str, Any], source_label: str):
        common_speaker = _raw_common_speaker(raw)
        name = _raw_common_name(raw)
        if not name:
            return
        common_id = _raw_common_id(raw)
        group_key = (
            f"id:{common_id}"
            if common_id not in (None, "")
            else f"name:{name.casefold()}"
        )
        target = seen.get(group_key)
        if target is None:
            target = dict(common_speaker) if common_speaker else {}
            # 先保留 commonSpeaker 的字段，再用旧变体字段补齐头像/试听等
            # 兼容信息；最终展示名始终强制使用基础音色名。
            for key, value in raw.items():
                if key.startswith("_") or key in target:
                    continue
                if value not in (None, ""):
                    target[key] = value
            target["speakerName"] = name
            if common_id not in (None, ""):
                target["commonId"] = common_id
                # 旧规范化缓存的 key 可能仍是 speaker:<speakerNo>；让
                # normalize_voice 根据 commonId 重新生成基础音色 key。
                target.pop("key", None)
            target["_source"] = source_label
            seen[group_key] = target
            records.append(target)
            return

        # 同一基础音色的第一个变体决定提交/试听兜底标识；只补空字段，
        # 不覆盖 common/list 或旧缓存已有的展示信息。
        for key, value in raw.items():
            if key.startswith("_") or target.get(key) not in (None, ""):
                continue
            if value not in (None, ""):
                target[key] = value

    preferred = [item for item in (preferred_voices or []) if isinstance(item, dict)]
    for item in preferred:
        append_record(item, "legacy_common_cache")
    for item in raw_voices:
        if isinstance(item, dict):
            append_record(item, "legacy_flat_cache")
    return records


def _tag_category_labels(tag_categories: Iterable[dict[str, Any]] | None) -> list[str]:
    """按 qry_tags 返回顺序提取父/子标签名称。"""
    labels: list[str] = []
    seen: set[str] = set()
    for category in tag_categories or []:
        if not isinstance(category, dict):
            continue
        values = [category.get("tagName"), category.get("tag_name")]
        values.extend(
            item.get("tagName")
            for item in (category.get("tagList") or [])
            if isinstance(item, dict)
        )
        for value in values:
            label = str(value or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            labels.append(label)
    return labels


def _copy_tag_categories(
    tag_categories: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """复制标签快照，避免调用方修改模块内固定数据。"""
    copied: list[dict[str, Any]] = []
    for category in tag_categories or []:
        if not isinstance(category, dict):
            continue
        item = dict(category)
        tag_list = category.get("tagList")
        if isinstance(tag_list, list):
            item["tagList"] = [dict(tag) for tag in tag_list if isinstance(tag, dict)]
        copied.append(item)
    return copied


def _build_voice_filters(
    voices: Iterable[dict[str, Any]],
    tag_categories: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    voices = list(voices)
    count_by_filter: dict[str, int] = {"all": len(voices)}
    for voice in voices:
        if voice["gender"] in {"female", "male"}:
            count_by_filter[voice["gender"]] = count_by_filter.get(voice["gender"], 0) + 1
        for category in voice.get("categories") or []:
            if category and category not in {"女声", "男声"}:
                count_by_filter[category] = count_by_filter.get(category, 0) + 1
    # qry_tags 返回的标签是固定快照；即使当前 common/list 中暂时没有命中，
    # 也要保留该标签，不能因为 useCount=0 或本次目录数量变化而从 UI 消失。
    for label in _tag_category_labels(tag_categories):
        if label not in {"女声", "男声"}:
            count_by_filter.setdefault(label, 0)

    filters = [{"key": "all", "label": "全部", "count": len(voices)}]
    categories_by_label = {
        label: {"key": f"tag:{label}", "label": label, "count": count}
        for label, count in count_by_filter.items()
        if label not in {"all", "female", "male"}
    }
    language_priority_labels = tuple(label for label, _aliases in VOICE_LANGUAGE_PRIORITY)
    language_priority = [
        categories_by_label[label]
        for label in language_priority_labels
        if label in categories_by_label
    ]
    filters.extend(language_priority)
    for key, label in (("female", "女声"), ("male", "男声")):
        filters.append({"key": key, "label": label, "count": count_by_filter.get(key, 0)})
    provider_order = _tag_category_labels(tag_categories)
    categories = sorted(
        (
            categories_by_label[label]
            for label in categories_by_label
        ),
        key=lambda item: (-item["count"], item["label"]),
    )
    preferred_labels = (
        "最热", "最新", "新闻播报", "解说", "商业广告", "情感陪伴",
        "教育课件", "角色演绎", "客服对话", "短视频", "地道方言",
        "超拟人", "童声",
    )
    preferred = [
        categories_by_label[label]
        for label in provider_order
        if label in categories_by_label and label not in language_priority_labels
    ]
    preferred_labels_set = {item["label"] for item in preferred}
    preferred.extend(
        item
        for item in categories
        if (
            item["label"] in preferred_labels
            and item["label"] not in language_priority_labels
            and item["label"] not in preferred_labels_set
        )
    )
    preferred_keys = {item["key"] for item in preferred}
    priority_keys = {item["key"] for item in language_priority}
    filters.extend(preferred)
    filters.extend(
        item
        for item in categories
        if item["key"] not in preferred_keys | priority_keys
    )
    # 标签栏本身支持横向滚动；不要截断 qry_tags 快照，否则新闻主持、
    # 广告营销和多语种下的子标签会在目录刷新后再次丢失。
    return filters


def normalize_catalog(
    raw_voices: Iterable[dict[str, Any]],
    *,
    composite_raw_voices: Iterable[dict[str, Any]] | None = None,
    tag_categories: Iterable[dict[str, Any]] | None = None,
    fetched_at: str | None = None,
    source: str = "cache",
) -> dict[str, Any]:
    """规范化讯飞多人配音目录。

    在线首选输入是 ``speaker/common/list``，因此 ``name`` 必须保持基础
    音色名（如 ``欣畅``），这样用户在 App 里选中的名称才能被多人配音
    弹窗原样搜索到。传入旧版 flat/list 数据时，仅在离线兼容路径将其按
    ``commonId`` 聚合为基础名，绝不把具体变体重新暴露成主列表。
    """
    raw_voice_list = list(raw_voices)
    provided_tag_categories = list(tag_categories or [])
    effective_tag_categories = _copy_tag_categories(
        provided_tag_categories or FIXED_TAG_CATEGORIES
    )
    if _looks_like_common_list(raw_voice_list):
        common_inputs = raw_voice_list
    else:
        # 优先使用旧缓存里单独保存的 composite 视图；若它也是变体，
        # _legacy_common_records 会从 composite_name/commonSpeaker 恢复基础名。
        preferred = list(composite_raw_voices or [])
        common_inputs = _legacy_common_records(raw_voice_list, preferred)
        if not common_inputs and preferred:
            common_inputs = _legacy_common_records(preferred)
        if not common_inputs:
            common_inputs = raw_voice_list

    # 先按英语、多语种、其他音色分组，再保留每组的 common/list 原始顺序，
    # 这样英语和多语种优先，同时不会破坏服务端“最热/最新”的相对顺序。
    # 默认英语-Amanda/英语-George 仍由 _normalize_voice_entries 追加为离线
    # 可用的内置角色，避免已有文档配置失效。
    voices = _normalize_voice_entries(common_inputs, source=source, sort=True)
    composite_voices = list(voices)
    voice_aliases = _build_voice_aliases(
        voices,
        raw_voice_list,
        composite_raw_voices,
    )

    filters = _build_voice_filters(voices, effective_tag_categories)
    composite_filters = list(filters)

    return {
        "_meta": {
            "description": "讯飞配音音色列表",
            "source": "https://peiyin.xunfei.cn/make",
            "fetched_at": fetched_at or datetime.now().isoformat(),
            "total_count": len(voices),
            "provider_count": len(
                [item for item in common_inputs if isinstance(item, dict)]
            ),
            "composite_count": len(composite_voices),
            "catalog_source": source,
            "tags_source": "frozen_snapshot",
            "speaker_list_endpoint": SPEAKER_COMMON_LIST_URL,
            "composite_list_endpoint": SPEAKER_COMMON_LIST_URL,
            "tags_endpoint": SPEAKER_TAGS_URL,
            # 保留给诊断/旧缓存识别；它不是当前 App 的列表来源。
            "legacy_flat_list_endpoint": SPEAKER_FLAT_LIST_URL,
        },
        "voices": voices,
        "filters": filters,
        "composite_voices": composite_voices,
        "composite_filters": composite_filters,
        "voice_aliases": voice_aliases,
        "tag_categories": effective_tag_categories,
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


def _load_legacy_catalog_payload(
    base_dir: str,
    resource_dir: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取一份旧 flat 缓存，供在线 common 刷新时迁移旧选择。"""
    for path in _cache_candidates(base_dir, resource_dir):
        try:
            with open(path, "r", encoding="utf-8") as source:
                payload = json.load(source)
            if not isinstance(payload, dict) or not isinstance(payload.get("voices"), list):
                continue
            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            if _catalog_uses_common_list(meta):
                continue
            composite = payload.get("composite_voices")
            return (
                [item for item in payload["voices"] if isinstance(item, dict)],
                [item for item in composite if isinstance(item, dict)]
                if isinstance(composite, list)
                else [],
            )
        except (OSError, ValueError, TypeError):
            continue
    return [], []


def _load_cached_preview_records(
    base_dir: str,
    resource_dir: str | None,
) -> list[dict[str, Any]]:
    """读取各级目录缓存中的试听地址，供在线刷新补齐短暂缺失项。"""
    records: list[dict[str, Any]] = []
    for path in _cache_candidates(base_dir, resource_dir):
        try:
            with open(path, "r", encoding="utf-8") as source:
                payload = json.load(source)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        for field in ("voices", "composite_voices"):
            values = payload.get(field)
            if not isinstance(values, list):
                continue
            records.extend(
                item for item in values
                if isinstance(item, dict) and _raw_audio_url(item)
            )
    return records


def _catalog_uses_common_list(meta: dict[str, Any]) -> bool:
    """判断缓存元数据是否明确来自当前 common/list 主目录。"""
    speaker_endpoint = str(meta.get("speaker_list_endpoint") or "").strip()
    if speaker_endpoint:
        return speaker_endpoint == SPEAKER_COMMON_LIST_URL
    # 旧版本可能同时写过 flat_list_endpoint 和 common composite endpoint；
    # 只要明确存在 flat 主目录，就仍按旧变体缓存迁移。
    flat_endpoint = str(meta.get("flat_list_endpoint") or "").strip()
    if flat_endpoint == SPEAKER_FLAT_LIST_URL:
        return False
    return str(meta.get("composite_list_endpoint") or "").strip() == SPEAKER_COMMON_LIST_URL


def load_cached_catalog(base_dir: str, resource_dir: str | None = None) -> dict[str, Any] | None:
    for path in _cache_candidates(base_dir, resource_dir):
        try:
            with open(path, "r", encoding="utf-8") as source:
                payload = json.load(source)
            if not isinstance(payload, dict) or not isinstance(payload.get("voices"), list):
                continue
            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            cached_voices = payload["voices"]
            legacy_composite = (
                payload.get("composite_voices")
                if isinstance(payload.get("composite_voices"), list)
                else None
            )
            if not _catalog_uses_common_list(meta):
                # resources/voices.json 和旧的 cache 都是 flat/list 变体；
                # normalize_catalog 会按 commonId/基础名聚合，并同时生成
                # speaker:<旧变体 ID> -> common:<基础音色 ID> 的兼容别名。
                if not _legacy_common_records(cached_voices, legacy_composite):
                    continue
            catalog = normalize_catalog(
                cached_voices,
                composite_raw_voices=legacy_composite,
                tag_categories=payload.get("tag_categories")
                if isinstance(payload.get("tag_categories"), list)
                else None,
                fetched_at=str(meta.get("fetched_at") or "") or None,
                source="cache",
            )
            stored_aliases = payload.get("voice_aliases")
            if isinstance(stored_aliases, dict):
                catalog["voice_aliases"] = {
                    **catalog.get("voice_aliases", {}),
                    **{
                        str(alias): str(target)
                        for alias, target in stored_aliases.items()
                        if str(alias).strip() and str(target).strip()
                    },
                }
            return catalog
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
    """刷新多人配音目录并写入可写数据目录；失败时回退缓存。

    ``common/list`` 是页面卡片的唯一主来源；标签使用已抓取的固定快照，
    不在每次刷新时重复请求需要登录态的 ``qry_tags``。
    """
    # common/list 决定当前 App 的基础音色顺序；flat/list 只补充同一基础
    # 音色的试听地址。两个请求并行执行，避免增加配置页首屏等待时间。
    with ThreadPoolExecutor(max_workers=2) as executor:
        common_future = executor.submit(fetch_common_list_speakers, timeout)
        flat_future = executor.submit(fetch_flat_list_speakers, timeout)
        common = common_future.result()
        try:
            flat = flat_future.result()
        except Exception:
            # 示例音频是增强能力；flat/list 失败时仍应保留可配置的基础目录。
            flat = []
    if not common:
        raise RuntimeError("讯飞多人配音音色接口未返回有效目录")
    tag_categories = _copy_tag_categories(FIXED_TAG_CATEGORIES)
    legacy_voices, legacy_composite = _load_legacy_catalog_payload(
        base_dir,
        resource_dir,
    )
    # 当前 flat/list 优先；缓存只用于补齐接口短暂失败或分页不完整时
    # 暂时缺少的音频地址，避免一次刷新把已有试听能力全部清空。
    cached_preview_records = _load_cached_preview_records(base_dir, resource_dir)
    catalog = normalize_catalog(
        merge_preview_audio(common, [*flat, *cached_preview_records]),
        tag_categories=tag_categories,
        fetched_at=datetime.now().isoformat(),
        source="live",
    )
    catalog["_meta"]["preview_audio_endpoint"] = SPEAKER_FLAT_LIST_URL
    catalog["_meta"]["preview_audio_count"] = sum(
        bool(item.get("audio_url")) for item in catalog.get("voices") or []
    )
    catalog["voice_aliases"] = _build_voice_aliases(
        catalog.get("voices") or [],
        legacy_voices,
        legacy_composite,
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
