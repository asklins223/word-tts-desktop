"""讯飞 video-api 请求签名。"""

from __future__ import annotations

import hashlib


def _canonical_sign_value(value):
    """按讯飞网页 Axios 拦截器的规则生成签名原文。"""
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            child = _canonical_sign_value(value[key])
            # 网页端会忽略空字符串/空值，但保留数组字段。
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


def _build_api_sign(param, base):
    """生成讯飞 video-api 请求需要的 sign 请求头。"""
    base_digest = hashlib.md5(
        _canonical_sign_value(base).encode("utf-8")
    ).hexdigest()
    payload = {"param": param, "base": base}
    return hashlib.md5(
        (_canonical_sign_value(payload) + base_digest).encode("utf-8")
    ).hexdigest()

