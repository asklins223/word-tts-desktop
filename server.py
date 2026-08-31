#!/usr/bin/env python3
"""
FastAPI 后端服务器 — 小猪wordTTS 本地 API 宿主
================================================
本文件只承担后端进程入口职责：/api/v1 工作流 API 的挂载、
进程能力校验中间件、音色目录与音色资产缓存，以及打包启动逻辑。
旧的会话/生成引擎与旧 /api/* 路由已按方案 13.1 物理删除；
未升级路径统一由中间件返回 410 API_VERSION_RETIRED。
生成编排见 application/workflow_service.py 与 workflow/engine.py。
"""

import os
import re
import sys

# Windows 打包后 stdout/stderr 默认使用 cp1252 编码，无法输出中文。
# 在任何 print 之前重配置为 UTF-8，防止 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

import json
import asyncio
import hashlib
import argparse
import secrets
import threading
import time
import urllib.error
import urllib.request

# ============================================================================
# 路径设置
# ============================================================================
# 资源根目录和可写数据目录由同一个模块解析，避免 server、核心 TTS 和
# 讯飞客户端在开发/打包模式下各自计算出不同的位置。
from app_paths import ensure_data_dir, resource_dir

BASE_DIR = ensure_data_dir()
RESOURCE_DIR = resource_dir()

if RESOURCE_DIR not in sys.path:
    sys.path.insert(0, RESOURCE_DIR)

# Playwright 的 PyInstaller 运行时会在 frozen 进程中默认把浏览器目录
# 解析到 playwright/driver/package/.local-browsers。Electron 打包流程为了
# 保留 Chromium 的完整目录结构，会把浏览器复制到 _MEIPASS/playwright_browsers；
# 在导入讯飞客户端之前显式指向这个可读资源目录，避免 Windows 或没有系统
# Chrome 的环境启动讯飞浏览器时找不到 Chromium。
if getattr(sys, "frozen", False):
    _bundled_playwright_browsers = os.path.join(
        RESOURCE_DIR, "playwright_browsers"
    )
    if os.path.isdir(_bundled_playwright_browsers):
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH", _bundled_playwright_browsers
        )

    # The packaged backend intentionally does not carry a second ~106MB Node
    # binary.  Electron normally supplies its own executable through the
    # environment when it launches this process.  Keep the backend's direct
    # packaging smoke (and manually launched paired backend) equally
    # deterministic by deriving that sibling executable when the environment
    # was not inherited from Electron.
    if not os.environ.get("PLAYWRIGHT_NODEJS_PATH"):
        _backend_executable = os.path.realpath(sys.executable)
        _electron_executable = None
        if sys.platform == "darwin":
            _contents_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(_backend_executable))
            )
            _electron_executable = os.path.join(
                _contents_dir, "MacOS", "小猪wordTTS"
            )
        elif sys.platform == "win32":
            _app_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(_backend_executable))
            )
            _electron_executable = os.path.join(_app_dir, "小猪wordTTS.exe")
        if _electron_executable and os.path.isfile(_electron_executable):
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = _electron_executable
            os.environ.setdefault("ELECTRON_RUN_AS_NODE", "1")

# ============================================================================
# 导入核心模块
# ============================================================================
# wordtts 在 import 时会执行模块级引导（编码设置、ffmpeg 配置等）；
# 这里只加载解析、音频和讯飞配音核心函数。题型注册表（解析器、
# supported_types）位于 question_types 包。
import wordtts as core
import question_types
import xunfei_voice_catalog as _voice_catalog

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import copy
import uvicorn
from uvicorn.config import LOGGING_CONFIG as _UVICORN_DEFAULT_LOG_CONFIG
from api.workflow_routes import install_workflow_api
# 音色目录首屏直接刷新讯飞多人配音 common/list，保证配置页拿到的名称
# 可以原样被多人配音弹窗搜索。网络失败时由 xunfei_voice_catalog 回退到
# 已缓存的 common 目录（旧 flat 缓存也会在读取时聚合成基础名称）。
_voice_catalog_lock = threading.RLock()
_voice_catalog_loaded = False
_voice_catalog_live = False
_voice_catalog_data: dict = {}
_voice_catalog_refresh_state_lock = threading.Lock()
_voice_catalog_refresh_in_progress = False
_voice_asset_cache_lock = threading.RLock()

VOICE_ASSET_CACHE_DIR = os.path.join(BASE_DIR, "cache", "voice-assets")
VOICE_ASSET_MAX_BYTES = {
    "avatar": 8 * 1024 * 1024,
    "sample": 24 * 1024 * 1024,
}
VOICE_ASSET_FIELDS = {
    "avatar": "img_url",
    "sample": "audio_url",
}
VOICE_ASSET_FALLBACK_MIME = {
    "avatar": "image/jpeg",
    "sample": "audio/mpeg",
}
def _load_voice_catalog_sync(force_refresh: bool = True) -> dict:
    """加载并注册音色目录；调用方可在 asyncio.to_thread 中运行。"""
    global _voice_catalog_loaded, _voice_catalog_live, _voice_catalog_data
    # 成功拿到在线目录后，本次进程不再重复请求；如果首次请求失败而
    # 回退到缓存/内置目录，则允许后续的 /api/config 重试在线刷新。
    if _voice_catalog_loaded and (_voice_catalog_live or not force_refresh):
        return _voice_catalog_data
    with _voice_catalog_lock:
        if _voice_catalog_loaded and (_voice_catalog_live or not force_refresh):
            return _voice_catalog_data
        if not _voice_catalog_loaded or force_refresh:
            _voice_catalog_data = _voice_catalog.load_or_refresh_catalog(
                BASE_DIR,
                RESOURCE_DIR,
                force_refresh=force_refresh,
            )
            _voice_catalog_loaded = True
            _voice_catalog_live = (
                (_voice_catalog_data.get("_meta") or {}).get("catalog_source")
                == "live"
            )
            if core._xunfei is not None:
                core._xunfei.register_voice_catalog(
                    _voice_catalog_data.get("voices") or []
                )
                register_aliases = getattr(core._xunfei, "register_voice_aliases", None)
                if callable(register_aliases):
                    register_aliases(_voice_catalog_data.get("voice_aliases") or {})
    return _voice_catalog_data


async def _refresh_voice_catalog_in_background() -> None:
    """在线刷新音色目录，不阻塞首次配置响应。"""
    global _voice_catalog_refresh_in_progress
    try:
        await asyncio.to_thread(_load_voice_catalog_sync, True)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # 刷新失败不影响已返回的本地目录；下次配置请求仍可再次尝试。
        print(f"[wordtts] 后台刷新讯飞音色目录失败: {error}", file=sys.stderr)
    finally:
        with _voice_catalog_refresh_state_lock:
            _voice_catalog_refresh_in_progress = False


def _schedule_voice_catalog_refresh() -> None:
    """只启动一个后台刷新任务，避免多个配置请求重复访问远端接口。"""
    global _voice_catalog_refresh_in_progress
    with _voice_catalog_refresh_state_lock:
        if _voice_catalog_refresh_in_progress:
            return
        _voice_catalog_refresh_in_progress = True
    try:
        asyncio.create_task(_refresh_voice_catalog_in_background())
    except RuntimeError:
        # 仅在没有运行中的事件循环时发生（例如同步测试直接调用辅助函数）。
        with _voice_catalog_refresh_state_lock:
            _voice_catalog_refresh_in_progress = False


def _voice_catalog_refresh_enabled() -> bool:
    """判断当前进程是否允许首屏触发联网音色目录刷新。"""
    # Electron 的打包冒烟测试显式关闭真实 Provider；它必须完全离线，
    # 否则 /api/v1/config 会在联网刷新期间阻塞渲染器就绪探测。
    return os.environ.get("WORDTTS_ENABLE_REAL_PROVIDER", "1") != "0"


def _atomic_write_json(path: str, data) -> None:
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as target:
            json.dump(data, target, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _clean_voice_keys(value) -> list[str]:
    """规范化结果中携带的音色 key，避免历史清单膨胀或写入异常数据。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result = []
    for raw_key in value:
        key = str(raw_key or "").strip()
        if not key or len(key) > 200 or key in result:
            continue
        result.append(key)
        if len(result) >= 32:
            break
    return result


def _voice_asset_paths(voice_key: str, kind: str) -> tuple[str, str, str]:
    """返回某音色某类资产的目录、数据文件和元数据文件。"""
    key_hash = hashlib.sha256(str(voice_key).encode("utf-8")).hexdigest()
    directory = os.path.join(VOICE_ASSET_CACHE_DIR, key_hash)
    data_path = os.path.join(directory, f"{kind}.bin")
    meta_path = os.path.join(directory, "meta.json")
    return directory, data_path, meta_path


def _catalog_voice_by_key(voice_key: str) -> Optional[dict]:
    normalized = str(voice_key or "").strip()
    if not normalized:
        return None
    for voice in (_voice_catalog_data.get("voices") or []):
        if isinstance(voice, dict) and str(voice.get("key") or "").strip() == normalized:
            return voice
    return None


def _download_voice_asset(url: str, target_path: str, kind: str) -> Optional[str]:
    """下载一个音色资产到本机缓存；失败时不影响主生成流程。"""
    if not str(url or "").startswith(("http://", "https://")):
        return None
    request = urllib.request.Request(
        str(url),
        headers={"User-Agent": "Mozilla/5.0 WordTTS/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            content_type = response.headers.get_content_type()
            max_bytes = VOICE_ASSET_MAX_BYTES[kind]
            payload = response.read(max_bytes + 1)
            if not payload or len(payload) > max_bytes:
                return None
    except (OSError, ValueError, urllib.error.URLError):
        return None

    media_type = content_type if "/" in str(content_type or "") else VOICE_ASSET_FALLBACK_MIME[kind]
    temporary_path = f"{target_path}.tmp"
    try:
        with open(temporary_path, "wb") as target:
            target.write(payload)
        os.replace(temporary_path, target_path)
    except OSError:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        return None
    return media_type


def _cache_voice_assets_sync(voice_keys) -> dict:
    """按音色 key 缓存头像和示例音频，同一个 key 只创建一个缓存目录。"""
    keys = _clean_voice_keys(voice_keys)
    if not keys:
        return {}
    os.makedirs(VOICE_ASSET_CACHE_DIR, exist_ok=True)
    cached = {}
    with _voice_asset_cache_lock:
        for voice_key in keys:
            voice = _catalog_voice_by_key(voice_key)
            if not voice:
                continue
            directory, _unused_data_path, meta_path = _voice_asset_paths(voice_key, "avatar")
            os.makedirs(directory, exist_ok=True)
            try:
                with open(meta_path, "r", encoding="utf-8") as source:
                    meta = json.load(source)
            except (OSError, ValueError, TypeError):
                meta = {}
            asset_meta = meta if isinstance(meta, dict) else {}
            asset_meta.setdefault("voice_key", voice_key)
            if not isinstance(asset_meta.get("assets"), dict):
                asset_meta["assets"] = {}

            for kind, field in VOICE_ASSET_FIELDS.items():
                _directory, data_path, _meta_path = _voice_asset_paths(voice_key, kind)
                try:
                    has_cached_asset = os.path.isfile(data_path) and os.path.getsize(data_path) > 0
                except OSError:
                    has_cached_asset = False
                if has_cached_asset:
                    cached.setdefault(voice_key, {})[kind] = True
                    continue
                url = str(voice.get(field) or "").strip()
                if not url:
                    continue
                try:
                    media_type = _download_voice_asset(url, data_path, kind)
                except Exception:
                    # 头像/试听是结果页增强项，远端响应异常不能让缓存接口
                    # 影响文档解析或音频生成主流程。
                    media_type = None
                if not media_type:
                    continue
                asset_meta["assets"][kind] = {
                    "media_type": media_type,
                    "source_url": url[:2000],
                }
                cached.setdefault(voice_key, {})[kind] = True

            try:
                _atomic_write_json(meta_path, asset_meta)
            except (OSError, ValueError, TypeError):
                # 资产本身已经可用，元数据写入失败不阻塞生成或试听。
                pass
    return cached


def _cached_voice_asset(voice_key: str, kind: str) -> tuple[Optional[str], str]:
    if kind not in VOICE_ASSET_FIELDS or not str(voice_key or "").strip():
        return None, ""
    _directory, data_path, meta_path = _voice_asset_paths(voice_key, kind)
    try:
        has_cached_asset = os.path.isfile(data_path) and os.path.getsize(data_path) > 0
    except OSError:
        has_cached_asset = False
    if not has_cached_asset:
        return None, ""
    media_type = VOICE_ASSET_FALLBACK_MIME[kind]
    try:
        with open(meta_path, "r", encoding="utf-8") as source:
            meta = json.load(source)
        media_type = str(
            ((meta.get("assets") or {}).get(kind) or {}).get("media_type")
            or media_type
        )
    except (OSError, ValueError, TypeError):
        pass
    return data_path, media_type



app = FastAPI(title="小猪wordTTS API")
_API_TOKEN = os.environ.get("WORDTTS_API_TOKEN", "")
_DEVELOPMENT_WORKFLOW_CAPABILITY = secrets.token_urlsafe(32)


def _versioned_api_capability():
    """Return the process-local capability required by /api/v1 routes."""
    return _API_TOKEN or _DEVELOPMENT_WORKFLOW_CAPABILITY


_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*)|(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))"
    r"(?:\.(?:(?:0|[1-9]\d*)|(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _package_version() -> str | None:
    try:
        with open(os.path.join(RESOURCE_DIR, "version.json"), encoding="utf-8") as version_file:
            payload = json.load(version_file)
            value = payload.get("version") if isinstance(payload, dict) else payload
            version = re.sub(r"^[vV]", "", str(value or "").strip())
            return version if _VERSION_PATTERN.fullmatch(version) else None
    except (OSError, TypeError, ValueError, AttributeError):
        return None


def _runtime_version() -> str:
    """Resolve the backend version without letting an environment variable drift it.

    ``WORDTTS_VERSION`` is retained as a compatibility fallback for legacy
    bundles that predate the bundled ``version.json``. Once the canonical file
    is present, it is authoritative for both the Electron shell and backend.
    """
    packaged_version = _package_version()
    if packaged_version:
        return packaged_version
    environment_version = re.sub(
        r"^[vV]", "", str(os.environ.get("WORDTTS_VERSION") or "").strip()
    )
    if _VERSION_PATTERN.fullmatch(environment_version):
        return environment_version
    return "0.0.0-dev"


APP_VERSION = _runtime_version()


def _legacy_api_enabled() -> bool:
    # The legacy surface is never enabled implicitly.  The supported desktop
    # entry point and imported ASGI app both default to the versioned API;
    # compatibility tests/tools must opt in explicitly.
    return os.environ.get("WORDTTS_LEGACY_API", "0") == "1"


def _local_request_allowed(request) -> bool:
    """Reject DNS-rebinding/browser-origin requests before route dispatch."""

    host = str(request.headers.get("host") or "").lower().strip()
    origin = request.headers.get("origin")
    # Starlette's TestClient uses ``testserver``; keeping this narrow test-only
    # exception allows unit probes without weakening a launched server.
    if __name__ != "__main__" and host.split(":", 1)[0] in {"testserver", "localhost"}:
        host_allowed = True
    else:
        host_name, separator, host_port = host.rpartition(":")
        if host.startswith("[") and "]" in host:
            host_name = host[1:host.index("]")]
            host_port = host.split("]", 1)[1].lstrip(":")
        elif not separator:
            host_name, host_port = host, ""
        host_allowed = host_name in {"127.0.0.1", "::1"}
        expected_port = str(os.environ.get("WORDTTS_PORT") or "").strip()
        if host_allowed and expected_port and host_port != expected_port:
            host_allowed = False
    if not host_allowed:
        return False
    return origin is None or origin == "null"


@app.middleware("http")
async def authenticate_local_api(request, call_next):
    """Electron 启动的后端只接受本次进程生成的随机令牌。"""
    path = request.url.path
    if path.startswith("/api/") and not _local_request_allowed(request):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=403,
            content={
                "request_id": f"request-{secrets.token_hex(8)}",
                "error_code": "ORIGIN_NOT_ALLOWED",
                "message": "local API request origin or host is not allowed",
                "retryable": False,
                "side_effect_occurred": False,
            },
        )
    if path.startswith("/api/") and not path.startswith("/api/v1/") and not _legacy_api_enabled():
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=410,
            headers={"X-API-Version": "v1"},
            content={
                "request_id": f"request-{secrets.token_hex(8)}",
                "error_code": "API_VERSION_RETIRED",
                "message": "legacy API has been retired; use /api/v1",
                "retryable": False,
                "side_effect_occurred": False,
            },
        )
    if path.startswith("/api/v1/"):
        # Versioned workflow routes are fail-closed even when server.py is
        # imported without WORDTTS_API_TOKEN (for example by a bare ASGI
        # runner).  The random process-local capability is never exposed to a
        # browser, while formal Electron startup replaces it with its token.
        supplied = request.headers.get("X-Desktop-Capability")
        expected = _versioned_api_capability()
        if not supplied or not secrets.compare_digest(supplied, expected):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={
                    "request_id": f"request-{secrets.token_hex(8)}",
                    "error_code": "UNAUTHORIZED",
                    "message": "missing or invalid desktop capability",
                    "retryable": False,
                    "side_effect_occurred": False,
                },
            )
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    # 仅允许本地来源，防止恶意网页通过浏览器访问本地 API
    allow_origins=[
        "null",  # Electron file:// 协议的 origin 为 "null"
    ],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=[
        "Content-Type",
        "Last-Event-ID",
        "X-Artifact-Format",
        "X-Artifact-Ticket",
        "X-Desktop-Capability",
        "X-Idempotency-Key",
        "X-Source-Write-Grant",
        "X-SSE-Ticket",
        "X-Staging-Generation",
    ],
)

# 静态文件（如果存在 renderer 目录）
_renderer_dir = os.path.join(RESOURCE_DIR, "electron", "renderer")
if os.path.isdir(_renderer_dir):
    app.mount("/static", StaticFiles(directory=_renderer_dir), name="static")

# The versioned workflow API is mounted independently from the legacy UI API.
# Its database is initialized lazily on the first request so importing server.py
# for the existing unit tests cannot mutate a user's runtime database.
_workflow_runtime = install_workflow_api(app, capability=_versioned_api_capability())
@app.get("/api/v1/health")
async def health():
    instance = hashlib.sha256(_API_TOKEN.encode("utf-8")).hexdigest()[:16] if _API_TOKEN else "development"
    return {
        "status": "ok",
        "app": "wordtts",
        "version": APP_VERSION,
        "instance": instance,
        "backend_contract_version": core.BACKEND_CONTRACT_VERSION,
        "audio_algorithm_version": core.AUDIO_ALGORITHM_VERSION,
        "parser_version": core.PARSER_VERSION,
        "default_generation_mode": core.DEFAULT_GENERATION_MODE,
    }
@app.get("/api/v1/config")
async def get_config():
    """返回前端所需配置，并确保音色来自讯飞多人配音 common/list。"""
    # 不能先把 resources/voices.json 的 flat/list 变体返回给渲染器再在
    # 后台刷新：首屏一旦展示了“欣畅-Pro+”，用户选择后多人配音面板只能
    # 搜索“欣畅”，这正是旧实现的错配。common/list 是公开分页接口，首屏
    # 等待一次有界刷新；失败时函数内部仍会立即回退到本地 common 缓存。
    catalog = await asyncio.to_thread(
        _load_voice_catalog_sync,
        _voice_catalog_refresh_enabled(),
    )
    default_female = core.FEMALE_VOICE
    default_male = core.MALE_VOICE
    return {
        "formats": ["mp3"],
        "qualities": list(core.QUALITY_BITRATE.keys()),
        "supported_types": list(question_types.PARSER_MAP.keys()),
        "type_colors": core.TYPE_COLORS,
        "tts_engine": "xunfei",
        "tts_parameters": ["rate", "pitch", "volume"],
        "tts_param_min": core.TTS_PARAM_MIN,
        "tts_param_max": core.TTS_PARAM_MAX,
        "tts_param_default": core.TTS_PARAM_DEFAULT,
        "xunfei_available": core._XUNFEI_AVAILABLE,
        "backend_contract_version": core.BACKEND_CONTRACT_VERSION,
        "audio_algorithm_version": core.AUDIO_ALGORITHM_VERSION,
        "parser_version": core.PARSER_VERSION,
        "generation_modes": [
            {
                "value": core.GENERATION_MODE_COMPOSITE,
                "label": "全部生成后切割",
                "default": True,
            },
            {
                "value": core.GENERATION_MODE_SINGLE,
                "label": "单条单条生成",
                "default": False,
            },
        ],
        "default_generation_mode": core.DEFAULT_GENERATION_MODE,
        "default_female_voice": default_female,
        "default_male_voice": default_male,
        "female_voice": "英语-Amanda",
        "male_voice": "英语-George",
        "voices": catalog.get("voices") or [],
        "voice_filters": catalog.get("filters") or [],
        "voice_aliases": catalog.get("voice_aliases") or {},
        "voice_catalog_meta": catalog.get("_meta") or {},
    }
@app.post("/api/v1/voice-assets/cache")
async def cache_voice_assets(payload: dict):
    """后台缓存用户刚选中的音色头像和示例音频。"""
    raw_keys = payload.get("voice_keys") if isinstance(payload, dict) else []
    voice_keys = _clean_voice_keys(raw_keys)[:64]
    if not voice_keys:
        return {"status": "ok", "cached": {}}
    await asyncio.to_thread(_load_voice_catalog_sync, False)
    cached = await asyncio.to_thread(_cache_voice_assets_sync, voice_keys)
    return {"status": "ok", "cached": cached}
@app.get("/api/v1/voice-assets/{voice_key}/{kind}")
async def get_voice_asset(voice_key: str, kind: str):
    """提供已经缓存到本机的音色资产，避免结果页重复请求讯飞资源。"""
    file_path, media_type = await asyncio.to_thread(
        _cached_voice_asset,
        voice_key,
        kind,
    )
    if not file_path:
        raise HTTPException(status_code=404, detail="音色示例尚未缓存")
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
# ============================================================================
# 启动
# ============================================================================

DEFAULT_PORT = 7863


def run_playwright_packaging_smoke_test():
    """验证打包内的 Playwright driver、Node 复用和 Chromium 可实际启动。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        # Playwright 1.56 默认 headless=True 会寻找单独的
        # chromium_headless_shell；打包只保留完整 Chromium，显式使用新版
        # Chromium headless 模式，避免重复内置第二套浏览器。
        browser = playwright.chromium.launch(channel='chromium', headless=True)
        try:
            page = browser.new_page()
            page.set_content('<title>WordTTS packaging smoke</title><p id="ok">ok</p>')
            if page.title() != 'WordTTS packaging smoke':
                raise RuntimeError('Chromium 页面执行结果不正确')
        finally:
            browser.close()
    print('[smoke] Playwright driver 与内置 Chromium 启动通过')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="小猪wordTTS local API server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORDTTS_PORT", DEFAULT_PORT)))
    parser.add_argument("--token", default=os.environ.get("WORDTTS_API_TOKEN", ""))
    parser.add_argument(
        "--enable-real-provider",
        action="store_true",
        help="兼容参数：显式启用真实 Provider；正式运行默认已启用",
    )
    parser.add_argument(
        "--disable-real-provider",
        action="store_true",
        help="仅在离线诊断/逻辑测试时显式关闭真实 Provider",
    )
    parser.add_argument(
        "--smoke-playwright",
        action="store_true",
        help="启动后立即验证 Playwright driver 与 Chromium，然后退出",
    )
    args = parser.parse_args()
    if args.smoke_playwright:
        run_playwright_packaging_smoke_test()
        raise SystemExit(0)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    _API_TOKEN = args.token
    if args.enable_real_provider and args.disable_real_provider:
        parser.error("--enable-real-provider 与 --disable-real-provider 不能同时使用")
    # Formal backend starts are real-provider-on by default.  Keep an explicit
    # off switch for logical diagnostics and retain WORDTTS_ENABLE_REAL_PROVIDER
    # as a backwards-compatible environment override for controlled launches.
    if args.disable_real_provider:
        real_provider_enabled = False
    elif args.enable_real_provider:
        real_provider_enabled = True
    else:
        real_provider_enabled = os.environ.get("WORDTTS_ENABLE_REAL_PROVIDER", "1") != "0"
    try:
        provider = _workflow_runtime.providers.get(
            "xunfei",
            os.environ.get("WORDTTS_XUNFEI_ACCOUNT_SCOPE", "xunfei-default"),
        )
        if hasattr(provider, "allow_real"):
            provider.allow_real = real_provider_enabled
    except Exception:
        # Provider construction is validated on the first workflow request;
        # do not make startup less diagnosable than the normal capability gate.
        pass
    # The formal desktop/server process owns the provider-aware retry loop.
    # Explicit offline launches disable it together with the real Provider.
    _workflow_runtime.auto_retry_enabled = real_provider_enabled
    # Keep the capability only in the backend process.  The versioned API
    # checks X-Desktop-Capability; query-string tokens are not accepted there.
    _workflow_runtime.capability = _versioned_api_capability()
    print(f"[server] 启动 FastAPI 服务器: http://127.0.0.1:{args.port}")
    # 将 uvicorn 默认日志从 stderr 改为 stdout，
    # 避免 Electron 控制台中所有日志都显示为 [python:err]
    _log_config = copy.deepcopy(_UVICORN_DEFAULT_LOG_CONFIG)
    _log_config["handlers"]["default"]["stream"] = "ext://sys.stdout"
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", log_config=_log_config)
