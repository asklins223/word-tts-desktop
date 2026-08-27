#!/usr/bin/env python3
"""
FastAPI 后端服务器 — 小猪wordTTS 一体化应用
============================================
复用 word_tts_app.py 中的核心 TTS 逻辑，
通过 REST API + SSE 提供服务，供 Electron 前端调用。
"""

import os
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
import shutil
import asyncio
import hashlib
import math
import argparse
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime

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

WORD_PARSER_DIR = os.path.join(RESOURCE_DIR, "word_parser")
if WORD_PARSER_DIR not in sys.path:
    sys.path.insert(0, WORD_PARSER_DIR)

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

# ============================================================================
# 导入核心模块（复用 word_tts_app 的全部逻辑）
# ============================================================================
# word_tts_app 在 import 时会执行模块级代码（路径设置、ffmpeg 配置等），
# 这里只加载解析、音频和讯飞配音核心函数。
import word_tts_app as core
import xunfei_voice_catalog as _voice_catalog

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import (
    StreamingResponse,
    FileResponse,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import copy
import uvicorn
from uvicorn.config import LOGGING_CONFIG as _UVICORN_DEFAULT_LOG_CONFIG

# ============================================================================
# 会话与进度管理
# ============================================================================

MAX_LOG_ENTRIES = 500  # 重连时保留最近的结构化日志
MAX_EVENT_JOURNAL_ENTRIES = 1200  # 每个 SSE 连接按游标独立读取的有界事件日志


def _integer_progress_count(value, total=None):
    """将内部批量阶段进度转换为用户可见的整数计数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number):
        number = 0.0

    count = max(0, math.floor(number + 0.5))
    if total is not None:
        try:
            upper_bound = max(0, int(total))
        except (TypeError, ValueError):
            upper_bound = 0
        count = min(count, upper_bound)
    return count

class SessionState:
    """每个处理会话的状态。"""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_dir = session_output_dir(session_id)
        self.event_seq: int = 0
        self.event_journal = deque(maxlen=MAX_EVENT_JOURNAL_ENTRIES)
        # Python 3.9 会在 asyncio.Event() 构造时绑定当前事件循环；会话
        # 可能先由同步上传/测试代码创建，再由 FastAPI 的事件循环消费，
        # 因此不能在这里提前绑定。第一次进入异步上下文时再懒创建。
        self.event_signal: Optional[asyncio.Event] = None
        self._event_signal_loop = None
        self._event_signal_lock = threading.Lock()
        self.log_entries: list = []
        self.log_seq: int = 0
        self.progress: Optional[dict] = None
        self.parse_results: Optional[list] = None
        self.source_fingerprint: Optional[dict] = None
        self.status: str = "idle"
        self.done: bool = False
        self.ended: bool = False  # 任务流已发送 end；供多 SSE 连接可靠收尾
        self.cancelled: bool = False  # 用户取消标记
        self.cleaning_up: bool = False  # cleanup 期间拒绝新的生成请求
        self.task: Optional[asyncio.Task] = None  # 当前生成任务引用
        self.final_download: Optional[dict] = None  # 最终 download 事件（供重连时重放）
        self.final_zip_path: Optional[str] = None  # 最终 zip 路径（供重连时重放）
        self.final_done: Optional[dict] = None  # 完整 done 事件（含历史记录 ID，供重连时重放）
        self.final_error: Optional[dict] = None  # 终止错误（供并存/重连 SSE 重放）
        self.final_cancelled: Optional[dict] = None  # 取消终态（供断线重连 SSE 重放）
        self.last_stats: Optional[dict] = None  # 最新 stats 事件（供重连时重放）
        self.lifecycle_version: int = 0  # cleanup 时递增，使并发中的旧启动请求失效

    def ensure_event_signal(self) -> asyncio.Event:
        """返回绑定到当前运行循环的 SSE 广播事件。"""
        loop = asyncio.get_running_loop()
        with self._event_signal_lock:
            if self.event_signal is None or self._event_signal_loop is not loop:
                self.event_signal = asyncio.Event()
                self._event_signal_loop = loop
            return self.event_signal

# 全局会话注册表
_sessions: dict[str, SessionState] = {}
MAX_SESSIONS = 20  # 最大并发会话数，防止内存泄漏
MAX_HISTORY_RECORDS = 20
PROGRESS_SAVE_ITEM_INTERVAL = 5
PROGRESS_SAVE_INTERVAL_SECONDS = 1.0
STATS_EMIT_INTERVAL_SECONDS = 0.12
PARSE_CACHE_VERSION = 10
SOURCE_META_FILENAME = "source_fingerprint.json"
SESSION_DIR_PREFIX = "session_"
HISTORY_MANIFEST_FILENAME = "history.json"
HISTORY_SCHEMA_VERSION = 1
_history_lock = threading.RLock()

# 音色目录先从本地种子/缓存快速加载，避免网络波动阻塞应用启动；在线目录
# 会在配置接口返回后后台刷新。远端接口失败时仍由 xunfei_voice_catalog
# 回退到本地 JSON 缓存。
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

# 讯飞客户端复用一个有头浏览器页面；页面内的选音色、调参数、输入文本和
# 下载响应都不是可并发操作的。不同会话仍可并行解析/排队，但实际生成必须
# 串行，否则一个任务可能改写另一个任务刚设置好的音色或参数。锁延迟到
# 当前运行中的事件循环创建，避免模块导入时绑定到已关闭的临时事件循环。
_xunfei_generation_lock: Optional[asyncio.Lock] = None
_xunfei_generation_lock_loop = None
_xunfei_generation_lock_guard = threading.Lock()


def _get_xunfei_generation_lock() -> asyncio.Lock:
    global _xunfei_generation_lock, _xunfei_generation_lock_loop
    loop = asyncio.get_running_loop()
    with _xunfei_generation_lock_guard:
        if (
            _xunfei_generation_lock is None
            or _xunfei_generation_lock_loop is not loop
        ):
            _xunfei_generation_lock = asyncio.Lock()
            _xunfei_generation_lock_loop = loop
        return _xunfei_generation_lock


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


def session_output_dir(session_id: str) -> str:
    """为每个 API 会话分配独立目录，避免同名文档互删或覆盖。"""
    label = core.sanitize_dirname(session_id)[:48] or "task"
    unique_suffix = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return os.path.join(core.OUTPUT_BASE, f"{SESSION_DIR_PREFIX}{label}_{unique_suffix}")


def confined_file_path(root_dir: str, filename: str) -> str:
    """解析目录内文件，并拒绝通过符号链接逃逸到目录之外。"""
    real_root = os.path.realpath(root_dir)
    real_path = os.path.realpath(os.path.join(real_root, filename))
    try:
        common = os.path.commonpath([real_root, real_path])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法文件路径") from exc
    if os.path.normcase(common) != os.path.normcase(real_root):
        raise HTTPException(status_code=400, detail="非法文件路径")
    return real_path


def source_fingerprint(filepath: str) -> dict:
    """计算源文档内容指纹，避免同名文件误用旧解析或旧音频。"""
    digest = hashlib.sha256()
    size = 0
    with open(filepath, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {
        "cache_version": PARSE_CACHE_VERSION,
        "parser_version": core.PARSER_VERSION,
        "sha256": digest.hexdigest(),
        "size": size,
    }


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


def history_id_for_session(session_id: str) -> str:
    """生成不暴露原始会话名、且可幂等复用的历史记录 ID。"""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def _history_manifest_path(session_dir: str) -> str:
    return os.path.join(session_dir, HISTORY_MANIFEST_FILENAME)


def _is_confined_history_dir(session_dir: str) -> bool:
    try:
        root = os.path.realpath(core.OUTPUT_BASE)
        candidate = os.path.realpath(session_dir)
        return (
            os.path.commonpath([root, candidate]) == root
            and candidate != root
            and os.path.basename(candidate).startswith(SESSION_DIR_PREFIX)
        )
    except (OSError, ValueError):
        return False


def _clean_history_file(item: dict, session_dir: str) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    filename = str(item.get("filename") or "")
    if not filename or filename != os.path.basename(filename):
        return None
    try:
        file_path = confined_file_path(os.path.join(session_dir, "audio"), filename)
    except HTTPException:
        return None
    return {
        "id": str(item.get("id") or "")[:160],
        "filename": filename,
        "doc_type": str(item.get("doc_type") or "")[:160],
        "category": str(item.get("category") or "")[:160],
        "text": str(item.get("text") or "")[:20000],
        "text_preview": str(item.get("text_preview") or "")[:500],
        "voice_keys": _clean_voice_keys(item.get("voice_keys")),
        "available": os.path.isfile(file_path),
    }


def _read_history_manifest(session_dir: str) -> Optional[dict]:
    """读取并清洗历史清单；返回值不会包含任意文件路径或生成凭据。"""
    if not _is_confined_history_dir(session_dir):
        return None
    try:
        with open(_history_manifest_path(session_dir), "r", encoding="utf-8") as source:
            raw = json.load(source)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != HISTORY_SCHEMA_VERSION:
        return None

    session_id = str(raw.get("session_id") or "")
    history_id = str(raw.get("history_id") or "")
    if not session_id or history_id != history_id_for_session(session_id):
        return None

    files = []
    for item in raw.get("files") or []:
        cleaned = _clean_history_file(item, session_dir)
        if cleaned:
            files.append(cleaned)

    completed = max(0, int(raw.get("completed") or len(files)))
    failed = max(0, int(raw.get("failed") or 0))
    total = max(completed + failed, int(raw.get("total") or 0))
    completed_at = str(raw.get("completed_at") or raw.get("created_at") or "")
    zip_path = os.path.join(session_dir, "output.zip")
    failed_items = []
    for item in (raw.get("failed_items") or [])[:20]:
        if not isinstance(item, dict):
            continue
        failed_items.append({
            "id": str(item.get("id") or "")[:160],
            "doc_type": str(item.get("doc_type") or "")[:160],
            "error": str(item.get("error") or "")[:240],
        })

    return {
        "id": history_id,
        "session_id": session_id,
        "source_filename": os.path.basename(str(raw.get("source_filename") or "未命名文档.docx")),
        "created_at": str(raw.get("created_at") or completed_at),
        "completed_at": completed_at,
        "completed": completed,
        "failed": failed,
        "total": total,
        "format": str(raw.get("format") or "mp3")[:16].lower(),
        "preview": bool(raw.get("preview")),
        "generation_mode": (
            raw.get("generation_mode")
            if raw.get("generation_mode") in core.GENERATION_MODES
            else core.GENERATION_MODE_SINGLE
        ),
        "zip_available": os.path.isfile(zip_path),
        "available_files": sum(1 for item in files if item["available"]),
        "files": files,
        "failed_items": failed_items,
        "_session_dir": session_dir,
    }


def _history_records() -> list[dict]:
    records = []
    try:
        entries = [
            entry for entry in os.scandir(core.OUTPUT_BASE)
            if entry.name.startswith(SESSION_DIR_PREFIX) and entry.is_dir(follow_symlinks=False)
        ]
    except OSError:
        return records
    for entry in entries:
        record = _read_history_manifest(entry.path)
        # 即使成品被用户或外部工具移走，也保留清单记录供界面展示缺失状态，
        # 并让用户仍能显式删除该历史目录。
        if record:
            records.append(record)
    records.sort(key=lambda item: item.get("completed_at") or "", reverse=True)
    return records


def _public_history_record(record: dict, include_files: bool) -> dict:
    public = {key: value for key, value in record.items() if not key.startswith("_") and key != "session_id"}
    if not include_files:
        public.pop("files", None)
        public.pop("failed_items", None)
    return public


def list_history_records() -> list[dict]:
    with _history_lock:
        # 启动后首次读取也执行一次收敛，覆盖上次进程在写清单与裁剪之间异常退出的情况。
        _trim_history_records()
        return [_public_history_record(record, False) for record in _history_records()[:MAX_HISTORY_RECORDS]]


def get_history_record(history_id: str) -> Optional[dict]:
    if len(history_id) != 24 or any(ch not in "0123456789abcdef" for ch in history_id):
        return None
    with _history_lock:
        for record in _history_records():
            if record["id"] == history_id:
                return _public_history_record(record, True)
    return None


def _find_history_record_internal(history_id: str) -> Optional[dict]:
    if len(history_id) != 24 or any(ch not in "0123456789abcdef" for ch in history_id):
        return None
    for record in _history_records():
        if record["id"] == history_id:
            return record
    return None


def _trim_history_records() -> None:
    records = _history_records()
    for record in records[MAX_HISTORY_RECORDS:]:
        session = _sessions.get(record["session_id"])
        if session and session.task and not session.task.done():
            continue
        session_dir = record["_session_dir"]
        if not _is_confined_history_dir(session_dir):
            continue
        shutil.rmtree(session_dir, ignore_errors=True)
        _sessions.pop(record["session_id"], None)


def archive_history_record(
    session: SessionState,
    progress: dict,
    file_list: list[dict],
    zip_path: Optional[str],
) -> Optional[dict]:
    """把已完成会话登记为历史记录，并物理淘汰第 21 条及更早记录。"""
    if not file_list or not _is_confined_history_dir(session.session_dir):
        return None
    failed_items = [
        {
            "id": item.get("id", ""),
            "doc_type": item.get("doc_type", ""),
            "error": str(item.get("error") or "")[:240],
        }
        for item in progress.get("items", [])
        if item.get("status") == "error"
    ][:20]
    now = datetime.now().isoformat()
    progress_config = progress.get("config") if isinstance(progress.get("config"), dict) else {}
    generation_mode = progress_config.get(
        "generation_mode",
        core.GENERATION_MODE_SINGLE,
    )
    if generation_mode not in core.GENERATION_MODES:
        generation_mode = core.GENERATION_MODE_SINGLE
    manifest = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "history_id": history_id_for_session(session.session_id),
        "session_id": session.session_id,
        "source_filename": os.path.basename(str(progress.get("source_file") or "未命名文档.docx")),
        "created_at": str(progress.get("created_at") or now),
        "completed_at": now,
        "total": max(0, int(progress.get("total_items") or 0)),
        "completed": max(0, int(progress.get("completed") or len(file_list))),
        "failed": max(0, int(progress.get("failed") or 0)),
        "format": str(progress_config.get("format") or "mp3")[:16].lower(),
        "preview": bool(progress_config.get("preview")),
        "generation_mode": generation_mode,
        "zip_available": bool(zip_path and os.path.isfile(zip_path)),
        "failed_items": failed_items,
        "files": [
            {
                "id": str(item.get("id") or "")[:160],
                "filename": os.path.basename(str(item.get("filename") or "")),
                "doc_type": str(item.get("doc_type") or "")[:160],
                "category": str(item.get("category") or "")[:160],
                "text": str(item.get("text") or "")[:20000],
                "text_preview": str(item.get("text_preview") or "")[:500],
                "voice_keys": _clean_voice_keys(item.get("voice_keys")),
            }
            for item in file_list
            if item.get("filename") and os.path.basename(str(item.get("filename"))) == str(item.get("filename"))
        ],
    }
    if not manifest["files"]:
        return None
    with _history_lock:
        _atomic_write_json(_history_manifest_path(session.session_dir), manifest)
        _trim_history_records()
        record = _read_history_manifest(session.session_dir)
        return _public_history_record(record, True) if record else None


def delete_history_record(history_id: str) -> bool:
    with _history_lock:
        record = _find_history_record_internal(history_id)
        if not record:
            return False
        session = _sessions.get(record["session_id"])
        if session and session.task and not session.task.done():
            raise HTTPException(status_code=409, detail="任务仍在生成，暂时不能删除")
        session_dir = record["_session_dir"]
        if not _is_confined_history_dir(session_dir):
            raise HTTPException(status_code=400, detail="非法历史记录路径")
        shutil.rmtree(session_dir)
        _sessions.pop(record["session_id"], None)
        return True


def resolve_history_asset(history_id: str, filename: str) -> tuple[str, dict]:
    with _history_lock:
        record = _find_history_record_internal(history_id)
        if not record:
            raise HTTPException(status_code=404, detail="历史记录不存在")
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name != filename:
            raise HTTPException(status_code=400, detail="非法文件名")
        if safe_name == "output.zip":
            file_path = confined_file_path(record["_session_dir"], safe_name)
        else:
            allowed_names = {item["filename"] for item in record["files"]}
            if safe_name not in allowed_names:
                raise HTTPException(status_code=404, detail="文件不在历史记录中")
            file_path = confined_file_path(os.path.join(record["_session_dir"], "audio"), safe_name)
        if not os.path.isfile(file_path):
            raise HTTPException(status_code=404, detail="历史文件不存在")
        return file_path, record


def load_parse_cache(session_dir: str, fingerprint: dict) -> Optional[list]:
    parsed_path = os.path.join(session_dir, "parsed.json")
    meta_path = os.path.join(session_dir, SOURCE_META_FILENAME)
    try:
        with open(meta_path, "r", encoding="utf-8") as source:
            cached_fingerprint = json.load(source)
        if cached_fingerprint != fingerprint:
            return None
        with open(parsed_path, "r", encoding="utf-8") as source:
            parse_results = json.load(source)
        if not isinstance(parse_results, list) or not parse_results:
            return None
        return parse_results
    except (OSError, ValueError, TypeError):
        return None


def clear_generated_outputs(session_dir: str) -> None:
    """仅清除可再生成的产物，保留目录本身。"""
    for dirname in ("audio", ".tmp", "composite"):
        shutil.rmtree(os.path.join(session_dir, dirname), ignore_errors=True)
    for filename in (
        "progress.json",
        "progress.json.tmp",
        "output.zip",
        HISTORY_MANIFEST_FILENAME,
        f"{HISTORY_MANIFEST_FILENAME}.tmp",
    ):
        try:
            os.remove(os.path.join(session_dir, filename))
        except FileNotFoundError:
            pass


def save_parse_cache(session_dir: str, parse_results: list, fingerprint: dict) -> None:
    os.makedirs(session_dir, exist_ok=True)
    meta_path = os.path.join(session_dir, SOURCE_META_FILENAME)
    # 先让旧元数据失效，避免 parsed.json 已更新但 meta 写入失败时形成错配命中。
    try:
        os.remove(meta_path)
    except FileNotFoundError:
        pass
    _atomic_write_json(os.path.join(session_dir, "parsed.json"), parse_results)
    # 元数据最后落盘；中途失败时旧/缺失指纹会让下次安全地重新解析。
    _atomic_write_json(meta_path, fingerprint)


def _composite_progress_is_valid(progress: dict, items: list) -> bool:
    """校验断点中的合并作品结构，坏数据只能触发安全重建。"""
    plan = progress.get("composite_work_plan")
    states = progress.get("composite_works")
    if plan is None and states is None:
        return True
    if not isinstance(plan, list) or not isinstance(states, dict):
        return False

    item_ids = {
        str(item.get("id") or "").strip()
        for item in items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    plan_ids = set()
    plan_item_ids = set()
    for work in plan:
        if not isinstance(work, dict):
            return False
        raw_work_id = work.get("work_id")
        if not isinstance(raw_work_id, (str, int)) or isinstance(raw_work_id, bool):
            return False
        work_id = str(raw_work_id).strip()
        if not work_id or work_id in plan_ids:
            return False
        raw_item_ids = work.get("item_ids")
        if not isinstance(raw_item_ids, (list, tuple)) or not raw_item_ids:
            return False
        normalized_item_ids = []
        for raw_item_id in raw_item_ids:
            if not isinstance(raw_item_id, (str, int)) or isinstance(raw_item_id, bool):
                return False
            item_id = str(raw_item_id).strip()
            if not item_id or item_id in plan_item_ids:
                return False
            normalized_item_ids.append(item_id)
            plan_item_ids.add(item_id)
        if any(item_id not in item_ids for item_id in normalized_item_ids):
            return False
        try:
            item_count = int(work.get("item_count") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            item_count != len(normalized_item_ids)
            or item_count <= 0
            or item_count > core.COMPOSITE_MAX_ITEMS_PER_WORK
        ):
            return False
        plan_ids.add(work_id)

    if any(str(key) not in plan_ids for key in states):
        return False
    allowed_statuses = {"pending", "submitted", "downloaded", "cut", "done", "complete", "error"}
    for work_id, state in states.items():
        if str(work_id) not in plan_ids or not isinstance(state, dict):
            return False
        plan_work = next(work for work in plan if str(work.get("work_id")) == str(work_id))
        state_item_ids = state.get("item_ids")
        if state_item_ids is not None:
            if not isinstance(state_item_ids, (list, tuple)):
                return False
            if list(state_item_ids) != list(plan_work["item_ids"]):
                return False
        cut_diagnostics = state.get("cut_diagnostics")
        if cut_diagnostics is not None and not isinstance(cut_diagnostics, dict):
            return False
        status = str(state.get("status") or "pending")
        if status not in allowed_statuses:
            return False
        try:
            cut_count = int(state.get("cut_item_count") or 0)
            item_count = int(plan_work.get("item_count") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if cut_count < 0 or cut_count > item_count:
            return False
    return True


def progress_is_reusable(progress: Optional[dict], fingerprint: dict) -> bool:
    if not isinstance(progress, dict):
        return False
    items = progress.get("items")
    if not isinstance(items, list) or not items:
        return False
    if progress.get("source_fingerprint") != fingerprint:
        return False
    cfg = progress.get("config")
    if not isinstance(cfg, dict):
        return False
    generation_mode = cfg.get(
        "generation_mode",
        core.GENERATION_MODE_SINGLE,
    )
    if generation_mode not in core.GENERATION_MODES:
        return False
    expected_algorithms = {core.AUDIO_ALGORITHM_VERSION}
    if generation_mode == core.GENERATION_MODE_SINGLE:
        expected_algorithms.add(core.LEGACY_AUDIO_ALGORITHM_VERSION)
    if cfg.get("audio_algorithm_version") not in expected_algorithms:
        return False
    if cfg.get("parser_version") != core.PARSER_VERSION:
        return False
    if not all(isinstance(item, dict) and "raw_item" in item for item in items):
        return False
    if generation_mode == core.GENERATION_MODE_COMPOSITE and not _composite_progress_is_valid(
        progress,
        items,
    ):
        return False
    for item in items:
        if item.get("status") != "done":
            continue
        output_path = item.get("output_path")
        if not isinstance(output_path, str) or not output_path:
            return False
        if not os.path.exists(output_path):
            return False
    return True


def _persisted_session_dirs(excluded_dirs: set[str]) -> list[str]:
    try:
        entries = [
            entry for entry in os.scandir(core.OUTPUT_BASE)
            if entry.name.startswith(SESSION_DIR_PREFIX)
            and entry.is_dir(follow_symlinks=False)
            and entry.path not in excluded_dirs
        ]
    except OSError:
        return []
    def modified_at(entry) -> int:
        try:
            return entry.stat().st_mtime_ns
        except OSError:
            return 0
    entries.sort(key=modified_at, reverse=True)
    return [entry.path for entry in entries]


def find_persisted_parse_cache(
    fingerprint: dict,
    excluded_dirs: set[str],
) -> Optional[list]:
    """从已退出任务的私有目录中只读复用解析结果。"""
    for candidate_dir in _persisted_session_dirs(excluded_dirs):
        parse_results = load_parse_cache(candidate_dir, fingerprint)
        if parse_results:
            return parse_results
    return None


def _configs_match(previous: dict, requested: dict) -> bool:
    previous = previous if isinstance(previous, dict) else {}
    requested = requested if isinstance(requested, dict) else {}
    for key, value in requested.items():
        previous_value = previous.get(key)
        # 旧任务没有保存 generation_mode，但它们实际走的是原有逐段流程。
        if key == "generation_mode" and previous_value is None:
            previous_value = core.GENERATION_MODE_SINGLE
        if previous_value != value:
            return False
    return True


def restore_persisted_progress(
    fingerprint: dict,
    config: dict,
    target_dir: str,
    source_filename: str,
    source_path: str,
    excluded_dirs: set[str],
) -> Optional[dict]:
    """复制历史任务的可用进度到当前私有目录，不共享可写产物。"""
    for candidate_dir in _persisted_session_dirs(excluded_dirs | {target_dir}):
        progress = core.load_progress(candidate_dir)
        if not progress_is_reusable(progress, fingerprint):
            continue
        if not _configs_match(progress.get("config", {}), config):
            continue

        restored = copy.deepcopy(progress)
        source_audio_dir = os.path.realpath(os.path.join(candidate_dir, "audio"))
        target_audio_dir = os.path.join(target_dir, "audio")
        clear_generated_outputs(target_dir)
        os.makedirs(target_audio_dir, exist_ok=True)

        valid = True
        for item in restored.get("items", []):
            if item.get("status") != "done":
                item["output_path"] = None
                continue
            filename = os.path.basename(item.get("filename", ""))
            if not filename or filename != item.get("filename"):
                valid = False
                break
            source_audio = os.path.realpath(os.path.join(source_audio_dir, filename))
            if os.path.dirname(source_audio) != source_audio_dir or not os.path.isfile(source_audio):
                valid = False
                break
            target_audio = os.path.join(target_audio_dir, filename)
            try:
                shutil.copy2(source_audio, target_audio)
            except OSError:
                valid = False
                break
            item["output_path"] = target_audio

        if not valid:
            clear_generated_outputs(target_dir)
            continue

        restored["source_file"] = source_filename
        restored["source_path"] = source_path
        restored["source_fingerprint"] = fingerprint
        return restored
    return None


def get_or_create_session(session_id: str) -> SessionState:
    if session_id not in _sessions:
        # 超过上限时清理最旧的已完成会话
        if len(_sessions) >= MAX_SESSIONS:
            # 优先清理已完成的会话，其次清理最早的
            done_sessions = [sid for sid, s in _sessions.items() if s.done]
            if done_sessions:
                del _sessions[done_sessions[0]]
            else:
                # 不驱逐仍在运行的任务，避免其目录脱离会话表后被恢复扫描读取。
                raise HTTPException(status_code=503, detail="当前任务过多，请稍后重试")
        _sessions[session_id] = SessionState(session_id)
    return _sessions[session_id]


def push_event(session: SessionState, event: dict):
    """向会话队列推送事件，同时更新本地状态。"""
    session.event_seq += 1
    event = {**event, "event_seq": session.event_seq}
    if event["type"] == "log":
        session.log_entries.append(event["entry"])
        # 超出上限时丢弃最早的日志（保留最近 MAX_LOG_ENTRIES 条）
        if len(session.log_entries) > MAX_LOG_ENTRIES:
            session.log_entries = session.log_entries[-MAX_LOG_ENTRIES:]
    elif event["type"] == "status":
        session.status = event["text"]
    elif event["type"] == "stats":
        session.last_stats = event
    elif event["type"] == "done":
        session.final_done = dict(event)
        session.done = True
    elif event["type"] == "error":
        session.final_error = event
    elif event["type"] == "cancelled":
        session.final_cancelled = dict(event)
    elif event["type"] == "end":
        session.ended = True
    session.event_journal.append(event)
    # asyncio.Event 是广播唤醒：所有 SSE 连接都会按各自游标读取同一事件，
    # 不会像单消费者 Queue 那样在重连窗口互相“偷走”进度。
    # 生成事件通常在 FastAPI 事件循环内推送；同步测试/初始化阶段则没有
    # 可绑定的 loop，此时只记录 journal，SSE 建立时会从 journal 补发。
    try:
        session.ensure_event_signal().set()
    except RuntimeError:
        pass


# ============================================================================
# 核心生成流程（适配 SSE 流式输出）
# ============================================================================

async def generate_audio_stream(
    session: SessionState,
    source_filename: str,
    filepath: str,
    config: dict,
):
    """串行化讯飞浏览器任务，避免共享页面发生跨会话竞态。"""
    # /api/generate 会先显式规范化新配置，默认是 composite_cut。对旧前端
    # 或直接调用本函数但没有传 generation_mode 的请求，也必须遵循产品默认，
    # 否则打包客户端一旦前后端版本不同就会悄悄退回逐条生成并显著变慢。
    raw_config = dict(config or {})
    if "generation_mode" not in raw_config:
        raw_config["generation_mode"] = core.GENERATION_MODE_COMPOSITE
    async with _get_xunfei_generation_lock():
        await _generate_audio_stream(session, source_filename, filepath, raw_config)


async def _generate_audio_stream(
    session: SessionState,
    source_filename: str,
    filepath: str,
    config: dict,
):
    """
    异步生成音频，通过会话事件日志向所有 SSE 连接广播进度。
    复用 word_tts_app.py 中的所有核心函数。
    """
    config = core.normalize_tts_config(config)
    # 正常流程会先请求 /api/config；这里仍用缓存兜底注册任意音色，避免
    # 直接调用生成接口时 catalog 尚未进入讯飞客户端注册表。
    await asyncio.to_thread(_load_voice_catalog_sync, False)
    config.update({
        "audio_algorithm_version": core.AUDIO_ALGORITHM_VERSION,
        "parser_version": core.PARSER_VERSION,
    })
    task_started_at = time.perf_counter()
    eta_started_at: Optional[float] = None
    eta_baseline_processed = 0
    session_dir = session.session_dir
    last_progress_save_processed = -1
    last_progress_save_at = 0.0
    last_stats_emit_at = 0.0
    last_generation_status_at = 0.0
    stats_cache_progress_id = None
    stats_cache_item_count = -1
    stats_by_type = {}
    stats_item_states = {}
    stats_failed_items = {}

    async def persist_progress(progress, *, force=False):
        """节流并异步保存进度，避免每条音频阻塞 FastAPI 事件循环。"""
        nonlocal last_progress_save_processed, last_progress_save_at
        completed = max(0, int(progress.get("completed") or 0))
        failed = max(0, int(progress.get("failed") or 0))
        processed = completed + failed
        now = time.monotonic()
        if not force and last_progress_save_processed >= 0:
            if (
                processed - last_progress_save_processed < PROGRESS_SAVE_ITEM_INTERVAL
                and now - last_progress_save_at < PROGRESS_SAVE_INTERVAL_SECONDS
            ):
                return False
        await asyncio.to_thread(core.save_progress, session_dir, progress)
        last_progress_save_processed = processed
        last_progress_save_at = time.monotonic()
        return True

    def stats_item_key(item):
        return str(item.get("id") or f"object:{id(item)}")

    def failed_item_snapshot(item):
        return {
            "id": item.get("id", ""),
            "doc_type": item.get("doc_type", ""),
            "error": str(item.get("error") or "")[:240],
        }

    def rebuild_stats_cache(progress):
        nonlocal stats_cache_progress_id, stats_cache_item_count
        stats_by_type.clear()
        stats_item_states.clear()
        stats_failed_items.clear()
        items = progress.get("items", [])
        for item in items:
            key = stats_item_key(item)
            doc_type = item.get("doc_type", "")
            status = item.get("status")
            stats_item_states[key] = (doc_type, status)
            bucket = stats_by_type.setdefault(doc_type, {"done": 0, "failed": 0, "total": 0})
            bucket["total"] += 1
            if status == "done":
                bucket["done"] += 1
            elif status == "error":
                bucket["failed"] += 1
                stats_failed_items[key] = failed_item_snapshot(item)
        stats_cache_progress_id = id(progress)
        stats_cache_item_count = len(items)

    def ensure_stats_cache(progress):
        if (
            stats_cache_progress_id != id(progress)
            or stats_cache_item_count != len(progress.get("items", []))
        ):
            rebuild_stats_cache(progress)

    def update_stats_cache(progress, item):
        """只更新状态发生变化的条目，避免每条完成都扫描整个文档。"""
        ensure_stats_cache(progress)
        key = stats_item_key(item)
        current = (item.get("doc_type", ""), item.get("status"))
        previous = stats_item_states.get(key)
        if previous == current:
            if current[1] == "error":
                stats_failed_items[key] = failed_item_snapshot(item)
            return
        if previous is not None:
            old_type, old_status = previous
            old_bucket = stats_by_type.get(old_type)
            if old_bucket:
                old_bucket["total"] = max(0, old_bucket["total"] - 1)
                if old_status == "done":
                    old_bucket["done"] = max(0, old_bucket["done"] - 1)
                elif old_status == "error":
                    old_bucket["failed"] = max(0, old_bucket["failed"] - 1)
                if old_bucket["total"] == 0:
                    stats_by_type.pop(old_type, None)
            stats_failed_items.pop(key, None)
        new_type, new_status = current
        new_bucket = stats_by_type.setdefault(new_type, {"done": 0, "failed": 0, "total": 0})
        new_bucket["total"] += 1
        if new_status == "done":
            new_bucket["done"] += 1
        elif new_status == "error":
            new_bucket["failed"] += 1
            stats_failed_items[key] = failed_item_snapshot(item)
        stats_item_states[key] = current

    def log(
        level: str,
        msg: str,
        *,
        stage: str = "system",
        kind: str = "notice",
        status: Optional[str] = None,
        key: Optional[str] = None,
        title: Optional[str] = None,
        detail: Optional[str] = None,
        item: Optional[dict] = None,
        progress_snapshot: Optional[dict] = None,
        duration_ms: Optional[int] = None,
        work: Optional[dict] = None,
        segments: Optional[dict] = None,
    ):
        session.log_seq += 1
        entry = {
            "v": 1,
            "time": core.now_str(),
            "ts": datetime.now().astimezone().isoformat(),
            "level": level,
            "msg": msg,
            "stage": stage,
            "kind": kind,
            "status": status or {
                "progress": "running",
                "success": "success",
                "warn": "warning",
                "error": "error",
            }.get(level, "info"),
            "title": str(title or msg)[:240],
            "seq": session.log_seq,
            "generation_mode": config.get(
                "generation_mode",
                core.GENERATION_MODE_SINGLE,
            ),
        }
        if key:
            entry["key"] = str(key)[:160]
        if detail:
            entry["detail"] = str(detail)[:1200]
        if duration_ms is not None:
            entry["duration_ms"] = max(0, int(duration_ms))
        if work:
            entry["work"] = dict(work)
        if segments:
            entry["segments"] = dict(segments)
        if item:
            entry["item"] = {
                "id": str(item.get("id") or "")[:160],
                "filename": os.path.basename(str(item.get("filename") or "")),
                "doc_type": str(item.get("doc_type") or "")[:160],
                "category": str(item.get("category") or "")[:160],
                "voice": str(item.get("voice") or "")[:80],
                "text_preview": str(item.get("text_preview") or "")[:240],
            }
        if progress_snapshot:
            completed = max(0, int(progress_snapshot.get("completed") or 0))
            failed = max(0, int(progress_snapshot.get("failed") or 0))
            total = max(completed + failed, int(progress_snapshot.get("total_items") or 0))
            entry["progress"] = {
                "completed": completed,
                "failed": failed,
                "total": total,
            }
        push_event(session, {
            "type": "log",
            "entry": entry,
        })

    def emit_stats(
        progress,
        *,
        force=False,
        completed_override=None,
        failed_override=None,
        processed_override=None,
        phase=None,
        work=None,
        segments=None,
    ):
        nonlocal last_stats_emit_at
        now = time.monotonic()
        if not force and now - last_stats_emit_at < STATS_EMIT_INTERVAL_SECONDS:
            return
        ensure_stats_cache(progress)
        type_counts = {
            doc_type: dict(counts)
            for doc_type, counts in stats_by_type.items()
        }
        failed_items = list(stats_failed_items.values())[:20]
        completed_value = (
            progress.get("completed")
            if completed_override is None
            else completed_override
        )
        failed_value = (
            progress.get("failed")
            if failed_override is None
            else failed_override
        )
        completed = max(0, int(completed_value or 0))
        failed = max(0, int(failed_value or 0))
        total = max(completed + failed, int(progress.get("total_items") or 0))
        processed_float = (
            completed + failed
            if processed_override is None
            else max(0.0, float(processed_override))
        )
        processed_float = min(processed_float, float(total))
        processed_value = _integer_progress_count(processed_float, total)
        pending_value = max(0, total - processed_value)
        elapsed_ms = max(0, round((time.perf_counter() - task_started_at) * 1000))
        eta_ms = None
        run_processed = max(0, processed_float - eta_baseline_processed)
        if eta_started_at is not None and run_processed > 0 and total > processed_float:
            run_elapsed_ms = max(0, round((time.perf_counter() - eta_started_at) * 1000))
            eta_ms = round((run_elapsed_ms / run_processed) * (total - processed_float))
        event = {
            "type": "stats",
            "completed": completed,
            "total": total,
            "failed": failed,
            "processed": processed_value,
            "pending": pending_value,
            "elapsed_ms": elapsed_ms,
            "eta_ms": eta_ms,
            "failed_items": failed_items,
            "by_type": type_counts,
            "generation_mode": config.get(
                "generation_mode",
                core.GENERATION_MODE_SINGLE,
            ),
        }
        if phase:
            event["phase"] = str(phase)
        if work:
            event["work"] = dict(work)
        if segments:
            event["segments"] = dict(segments)
        push_event(session, event)
        last_stats_emit_at = time.monotonic()

    def emit_status(text: str):
        push_event(session, {"type": "status", "text": text})

    def emit_generation_status(
        progress,
        current_item: Optional[str] = None,
        *,
        force=False,
        completed_override=None,
        failed_override=None,
        processed_override=None,
    ):
        nonlocal last_generation_status_at
        completed_value = (
            progress.get("completed")
            if completed_override is None
            else completed_override
        )
        failed_value = (
            progress.get("failed")
            if failed_override is None
            else failed_override
        )
        completed = max(0, int(completed_value or 0))
        failed = max(0, int(failed_value or 0))
        total = max(completed + failed, int(progress.get("total_items") or 0))
        processed_float = (
            completed + failed
            if processed_override is None
            else max(0.0, float(processed_override))
        )
        processed_float = min(processed_float, float(total))
        processed_value = str(_integer_progress_count(processed_float, total))
        text = f"生成中 — 已处理 {processed_value}/{total} · 成功 {completed}"
        if failed:
            text += f" · 失败 {failed}"
        if current_item:
            text += f" · {current_item}"
        now = time.monotonic()
        if not force and now - last_generation_status_at < STATS_EMIT_INTERVAL_SECONDS:
            return
        emit_status(text)
        last_generation_status_at = time.monotonic()

    def emit_download(progress, file_list, zip_path=None):
        event = {
            "type": "download",
            "completed": progress["completed"],
            "total": progress["total_items"],
            "failed": progress["failed"],
            "file_list": [
                {
                    "id": f["id"],
                    "filename": f["filename"],
                    "doc_type": f["doc_type"],
                    "category": f["category"],
                    "text": f.get("text", ""),
                    "text_preview": f.get("text_preview", ""),
                    "voice_keys": _clean_voice_keys(f.get("voice_keys")),
                }
                for f in file_list
            ],
            "zip_available": bool(zip_path and os.path.exists(zip_path)),
        }
        # 如果带有 zip_path，说明是最终 download 事件，保存以供 SSE 重连重放
        if zip_path is not None:
            session.final_download = event
        push_event(session, event)

    def emit_error_terminal(
        title: str,
        detail: str,
        *,
        status_text: Optional[str] = None,
        progress_snapshot: Optional[dict] = None,
        duration_ms: Optional[int] = None,
    ):
        if session.final_error:
            return
        elapsed_ms = (
            max(0, int(duration_ms))
            if duration_ms is not None
            else round((time.perf_counter() - task_started_at) * 1000)
        )
        emit_status(status_text or title)
        log(
            "error",
            detail,
            stage="complete",
            kind="summary",
            key="task:summary",
            title=title,
            detail=detail,
            progress_snapshot=progress_snapshot,
            duration_ms=elapsed_ms,
        )
        push_event(session, {
            "type": "error",
            "msg": detail,
            "duration_ms": elapsed_ms,
        })

    def emit_cancelled_terminal(reason: str):
        if session.final_cancelled:
            return
        active_progress = session.progress or {}
        completed = max(0, int(active_progress.get("completed") or 0))
        failed = max(0, int(active_progress.get("failed") or 0))
        total = max(
            completed + failed,
            int(active_progress.get("total_items") or 0),
        )
        elapsed_ms = round((time.perf_counter() - task_started_at) * 1000)
        log(
            "warn",
            reason,
            stage="complete",
            kind="summary",
            key="task:summary",
            title="任务已取消",
            detail=(
                f"已完成 {completed} 条，失败 {failed} 条，"
                f"未处理 {max(total - completed - failed, 0)} 条 · {reason}"
            ),
            progress_snapshot=active_progress or None,
            duration_ms=elapsed_ms,
        )
        emit_status("已取消")
        push_event(session, {
            "type": "cancelled",
            "completed": completed,
            "failed": failed,
            "total": total,
            "duration_ms": elapsed_ms,
        })

    try:
        audio_dir = os.path.join(session_dir, "audio")
        os.makedirs(audio_dir, exist_ok=True)

        prepare_started_at = time.perf_counter()
        scope_label = "试听前 3 条" if config.get("preview") else "完整文档"
        log(
            "progress",
            "正在校验文档与输出设置",
            stage="prepare",
            kind="stage",
            key="stage:prepare",
            title="准备生成任务",
            detail=(
                f"{source_filename} · {scope_label} · "
                f"{str(config.get('format') or 'mp3').upper()} · "
                f"{config.get('quality') or '标准质量'}"
            ),
        )
        current_fingerprint = await asyncio.to_thread(source_fingerprint, filepath)
        if session.source_fingerprint and current_fingerprint != session.source_fingerprint:
            raise RuntimeError("源文档在导入后发生变化，请重新导入文档")
        session.source_fingerprint = current_fingerprint
        log(
            "success",
            "文档与输出设置校验完成",
            stage="prepare",
            kind="stage",
            key="stage:prepare",
            title="任务准备完成",
            detail="源文档可读取，输出目录与生成参数已确认",
            duration_ms=round((time.perf_counter() - prepare_started_at) * 1000),
        )

        # ---- 检查断点续传 ----
        existing = core.load_progress(session_dir)
        if not progress_is_reusable(existing, current_fingerprint):
            blocked_dirs = {
                state.session_dir
                for state in _sessions.values()
                if state is not session
            }
            restored = await asyncio.to_thread(
                restore_persisted_progress,
                current_fingerprint,
                config,
                session_dir,
                source_filename,
                filepath,
                blocked_dirs,
            )
            if restored:
                existing = restored
                await persist_progress(existing, force=True)
                log(
                    "info",
                    f"已恢复历史任务进度（{existing['completed']}/{existing['total_items']}）",
                    stage="prepare",
                    kind="recovery",
                    key="task:recovery",
                    title="已找到可继续的任务进度",
                    detail=f"将从第 {existing['completed'] + 1} 条继续，已完成 {existing['completed']} / {existing['total_items']}",
                    progress_snapshot=existing,
                )
        if progress_is_reusable(existing, current_fingerprint):
            old_config = existing.get("config", {})
            config_changed = not _configs_match(old_config, config)

            if config_changed:
                reason = "配置已变更"
                log(
                    "warn",
                    f"检测到已有进度但{reason}，重新开始处理",
                    stage="prepare",
                    kind="recovery",
                    key="task:recovery",
                    title="已有进度不能继续使用",
                    detail="声音或输出设置发生变化，将重新生成以保证结果一致",
                )
                progress = None
            else:
                progress = existing
                session.progress = progress  # 保存到 session 供下载端点使用
                done = progress["completed"]
                total = progress["total_items"]
                retry_count = sum(
                    1
                    for item in progress.get("items", [])
                    if item.get("status") == "error"
                )
                if retry_count:
                    # 旧失败项属于上一轮尝试，不能在本轮刚开始时继续计入
                    # failed；否则客户端刚连上 SSE 就会先显示“失败 N 条”
                    # 或 99% 的假进度，随后才回到真实重试进度。保留合并
                    # 作品的 error 状态供下面的断点逻辑决定重做边界，题目
                    # 状态和统计先恢复为本轮待处理。
                    for item in progress.get("items", []):
                        if item.get("status") == "error":
                            item["status"] = "pending"
                            item["error"] = None
                    progress["completed"] = sum(
                        1
                        for item in progress.get("items", [])
                        if item.get("status") == "done"
                    )
                    progress["failed"] = 0
                    rebuild_stats_cache(progress)
                    log(
                        "info",
                        f"检测到已有进度，本轮准备重试 {retry_count} 条失败内容",
                        stage="prepare",
                        kind="recovery",
                        key="task:recovery",
                        title="准备重试上次失败内容",
                        detail=(
                            f"已完成 {progress['completed']} 条，"
                            f"本轮待重试 {retry_count} 条"
                        ),
                        progress_snapshot=progress,
                    )
                else:
                    log(
                        "info",
                        f"检测到已有进度（{done}/{total} 已完成），继续处理",
                        stage="prepare",
                        kind="recovery",
                        key="task:recovery",
                        title="继续上次未完成的任务",
                        detail=f"已完成 {done} 条，剩余 {max(total - done, 0)} 条",
                        progress_snapshot=progress,
                    )
                emit_stats(progress)
                emit_status(
                    f"准备重试 {retry_count} 条失败内容"
                    if retry_count
                    else f"断点续传中 — {done}/{total} 已完成"
                )
        else:
            if existing:
                log(
                    "warn",
                    "已有进度与当前源文档不匹配或产物缺失，将重新开始处理",
                    stage="prepare",
                    kind="recovery",
                    key="task:recovery",
                    title="旧进度已失效",
                    detail="源文档内容或已生成文件发生变化，本次将重新处理",
                )
            progress = None

        # ---- 解析文档 ----
        if progress is None:
            parse_started_at = time.perf_counter()
            log(
                "progress",
                f"开始解析文档: {source_filename}",
                stage="parse",
                kind="stage",
                key="stage:parse",
                title="识别文档内容",
                detail="正在读取题型、分段与需要生成的音频条目",
            )
            emit_status("正在解析文档...")

            parse_results = session.parse_results
            if parse_results:
                summary = f"已识别 {sum(len(result.get('items', [])) for result in parse_results)} 条内容"
                log("info", "使用导入阶段已验证的文档解析结果")
            else:
                try:
                    parse_results, summary = await asyncio.to_thread(
                        core.parse_document_auto, filepath
                    )
                except Exception as e:
                    parse_duration_ms = round((time.perf_counter() - parse_started_at) * 1000)
                    log(
                        "error",
                        f"解析失败: {e}",
                        stage="parse",
                        kind="stage",
                        key="stage:parse",
                        title="文档内容识别失败",
                        detail=str(e),
                        duration_ms=parse_duration_ms,
                    )
                    emit_error_terminal(
                        "生成任务未能继续",
                        f"文档解析失败：{e}",
                        status_text=f"解析失败: {e}",
                        duration_ms=parse_duration_ms,
                    )
                    return

            if not parse_results:
                parse_duration_ms = round((time.perf_counter() - parse_started_at) * 1000)
                log(
                    "error",
                    f"未识别到任何题型内容: {summary}",
                    stage="parse",
                    kind="stage",
                    key="stage:parse",
                    title="没有找到可生成的内容",
                    detail=summary or "请检查 Word 文档的题型与段落结构",
                    duration_ms=parse_duration_ms,
                )
                empty_detail = summary or "未识别到任何题型内容"
                emit_error_terminal(
                    "生成任务未能继续",
                    empty_detail,
                    status_text=empty_detail,
                    duration_ms=parse_duration_ms,
                )
                return

            # 全量重做时清理旧产物，并保存与源文件指纹绑定的解析结果。
            await asyncio.to_thread(clear_generated_outputs, session_dir)
            await asyncio.to_thread(
                save_parse_cache, session_dir, parse_results, current_fingerprint
            )
            os.makedirs(audio_dir, exist_ok=True)

            type_names = "、".join(r["doc_type"] for r in parse_results)
            progress = core.build_progress(source_filename, filepath, parse_results, config)
            progress["source_fingerprint"] = current_fingerprint

            # 试听模式
            if config.get("preview") and progress["total_items"] > 3:
                original_total = progress["total_items"]
                progress["items"] = progress["items"][:3]
                progress["total_items"] = 3
                log(
                    "info",
                    f"试听模式：仅生成前 3 条（共 {original_total} 条）",
                    stage="parse",
                    kind="notice",
                    key="scope:preview",
                    title="已应用试听范围",
                    detail=f"文档共有 {original_total} 条内容，本次先生成前 3 条用于试听",
                )

            await persist_progress(progress, force=True)
            session.progress = progress  # 保存到 session 供下载端点使用

            count_info = f"共 {progress['total_items']} 个音频"
            log(
                "success",
                f"解析完成 — {summary} | 题型：{type_names} | {count_info}",
                stage="parse",
                kind="stage",
                key="stage:parse",
                title="文档内容识别完成",
                detail=f"{summary} · {count_info} · 题型：{type_names}",
                progress_snapshot=progress,
                duration_ms=round((time.perf_counter() - parse_started_at) * 1000),
            )
        else:
            cached_types = "、".join(
                sorted({str(item.get("doc_type") or "") for item in progress.get("items", []) if item.get("doc_type")})
            )
            log(
                "success",
                "已加载验证过的文档结构",
                stage="parse",
                kind="stage",
                key="stage:parse",
                title="文档内容已就绪",
                detail=f"共 {progress['total_items']} 个音频" + (f" · 题型：{cached_types}" if cached_types else ""),
                progress_snapshot=progress,
            )

        # ---- 为断点续传重置失败项 ----
        # 失败项会在本轮重新尝试，因此必须先从统计中移除；否则每次重试都会
        # 累加 failed，最终出现 completed + failed > total 的错误结果。
        retry_items = [item for item in progress["items"] if item.get("status") == "error"]
        for item in retry_items:
            item["status"] = "pending"
            item["error"] = None
        progress["completed"] = sum(1 for item in progress["items"] if item.get("status") == "done")
        progress["failed"] = 0
        rebuild_stats_cache(progress)
        if retry_items:
            log(
                "info",
                f"将重新尝试 {len(retry_items)} 个失败项",
                stage="synthesize",
                kind="recovery",
                key="task:retry",
                title="重新处理上次失败内容",
                detail=f"共 {len(retry_items)} 条，将沿用当前声音与输出设置",
                progress_snapshot=progress,
            )

        # ---- 开始生成 ----
        progress["status"] = "generating"
        await persist_progress(progress, force=True)

        total = progress["total_items"]
        rate = config.get("rate", 50)
        volume = config.get("volume", 50)
        pitch = config.get("pitch", 50)
        # 输出格式不接受前端或旧任务的回退值，生成阶段始终落地为 MP3。
        fmt = "mp3"
        quality = config.get("quality", "128 kbps（标准）")
        # 音色与参数均来自前端的独立配置；词汇/例句仍强制使用默认女声。
        fv = config.get("default_female_voice") or core.FEMALE_VOICE
        mv = config.get("default_male_voice") or core.MALE_VOICE
        voice_configs = config.get("voice_configs") or {}
        role_configs = config.get("role_configs") or {}
        role_voices = config.get("role_voices") or {}
        generation_mode = config.get(
            "generation_mode",
            core.GENERATION_MODE_SINGLE,
        )
        composite_mode = generation_mode == core.GENERATION_MODE_COMPOSITE
        generation_mode_label = "全部生成后切割" if composite_mode else "单条单条生成"

        log(
            "info",
            f"生成计划已创建（{progress['completed']}/{total}）",
            stage="prepare",
            kind="notice",
            key="task:plan",
            title="生成计划已创建",
            detail=(
                f"待处理 {max(total - progress['completed'], 0)} 条 · "
                f"{str(fmt).upper()} · {quality} · 方式：{generation_mode_label}"
            ),
            progress_snapshot=progress,
        )
        log(
            "info",
            f"全部音频使用讯飞配音生成（{generation_mode_label}，音色与参数按角色独立配置）",
            stage="prepare",
            kind="notice",
            key="engine:plan",
            title="音色引擎已就绪",
            detail=(
                "统一使用讯飞配音引擎；W/M 或文本角色名分别使用所选音色，"
                f"无标识默认女声；生成方式：{generation_mode_label}"
            ),
        )

        emit_stats(progress)
        emit_generation_status(progress)
        if session.cancelled:
            emit_cancelled_terminal("生成计划准备完成后已停止任务")
            return

        # ---- 讯飞配音会话登录（所有音频都需要浏览器会话）----
        engine_started_at = time.perf_counter()
        if core._XUNFEI_AVAILABLE:
            log(
                "progress",
                "正在连接讯飞配音（首次需扫码登录，后续自动复用登录状态）",
                stage="prepare",
                kind="stage",
                key="engine:xunfei",
                title="连接讯飞配音服务",
                detail="将在浏览器中打开讯飞配音，完成后自动继续生成",
            )
            try:
                await core._xunfei.ensure_session(voice_key=fv)
                log(
                    "success",
                    "讯飞配音登录成功，开始生成音频",
                    stage="prepare",
                    kind="stage",
                    key="engine:xunfei",
                    title="讯飞配音服务已连接",
                    detail="默认女声与男声，以及文本中配置的角色音色已就绪",
                    duration_ms=round((time.perf_counter() - engine_started_at) * 1000),
                )
            except Exception as login_err:
                log(
                    "error",
                    f"讯飞配音登录失败: {login_err}",
                    stage="prepare",
                    kind="notice",
                    key="engine:xunfei",
                    title="讯飞配音服务连接失败",
                    detail=f"请重新开始任务并完成扫码登录。原因：{login_err}",
                    duration_ms=round((time.perf_counter() - engine_started_at) * 1000),
                )
                raise RuntimeError(f"讯飞配音登录失败: {login_err}")
        else:
            raise RuntimeError("讯飞配音引擎不可用（缺少 playwright），无法生成音频")

        synthesis_started_at = time.perf_counter()
        eta_started_at = synthesis_started_at
        eta_baseline_processed = progress["completed"] + progress["failed"]
        log(
            "progress",
            f"开始生成音频（{progress['completed']}/{total}）",
            stage="synthesize",
            kind="stage",
            key="stage:synthesize",
            title="合并生成后切割" if composite_mode else "逐条生成音频",
            detail=(
                f"已完成 {progress['completed']} 条，剩余 {max(total - progress['completed'], 0)} 条；"
                f"生成方式：{generation_mode_label}"
            ),
            progress_snapshot=progress,
        )

        # ---- 构造题目级任务 ----
        # single_segment 延续下面原有的逻辑片段批量流程；composite_cut
        # 会把这些逻辑片段重新装配为讯飞多人配音作品。
        item_specs = []
        item_contexts = {}
        item_started_at = {}
        empty_items = []
        composite_resume_item_ids = set()
        if composite_mode:
            for state in (progress.get("composite_works") or {}).values():
                if not isinstance(state, dict):
                    continue
                if str(state.get("status") or "") in {"done", "complete"}:
                    continue
                composite_resume_item_ids.update(
                    str(item_id)
                    for item_id in (state.get("item_ids") or [])
                    if str(item_id or "").strip()
                )

        for item in progress["items"]:
            if session.cancelled:
                emit_cancelled_terminal("已收到取消请求，未开始的内容不会继续处理")
                return
            if item["status"] == "done" and item["id"] not in composite_resume_item_ids:
                continue

            item_id = item["id"]
            raw_item = item["raw_item"]
            text = raw_item.get("text", "")
            if not text.strip():
                item["status"] = "error"
                item["error"] = "文本为空"
                progress["failed"] += 1
                empty_items.append(item)
                continue

            is_word_item = raw_item.get("category") in core.WORD_CATEGORIES
            item_default_voice = core.default_voice_for_item(
                raw_item, female_voice=fv, male_voice=mv
            )
            item_female_voice = fv
            item_male_voice = fv if is_word_item else mv
            item_role_voices = {} if is_word_item else role_voices
            item_default_role = (
                core.DEFAULT_FEMALE_ROLE_KEY
                if is_word_item or raw_item.get("voice") != "male"
                else core.DEFAULT_MALE_ROLE_KEY
            )
            speakers = core.parse_speakers_with_roles(
                text,
                default_voice=fv if is_word_item else item_default_voice,
                female_voice=item_female_voice,
                male_voice=item_male_voice,
                role_voices=item_role_voices,
                default_role=item_default_role,
                preserve_default_roles=True,
            )
            voice_label = "女声"
            if len(speakers) > 1 or speakers[0][1] != fv:
                voices_used = {voice for _role, voice, _text in speakers}
                voice_label = "、".join(sorted(voices_used))[:120] or "女声"
            voice_keys = []
            for _role, voice, _text in speakers:
                normalized_voice = str(voice or "").strip()
                if normalized_voice and normalized_voice not in voice_keys:
                    voice_keys.append(normalized_voice)
            item["voice_keys"] = voice_keys
            item_context = {
                "id": item_id,
                "filename": item.get("filename"),
                "doc_type": item.get("doc_type"),
                "category": item.get("category"),
                "voice": voice_label,
                "voice_keys": voice_keys,
                "text_preview": item.get("text_preview"),
            }
            item_contexts[item_id] = item_context
            item_started_at[item_id] = time.perf_counter()
            item_specs.append({
                "item_id": item_id,
                "text": text,
                "rate": rate,
                "volume": volume,
                "pitch": pitch,
                "default_voice": fv if is_word_item else item_default_voice,
                "female_voice": item_female_voice,
                "male_voice": item_male_voice,
                "voice_configs": voice_configs,
                "role_voices": item_role_voices,
                "role_configs": role_configs,
                "default_role": item_default_role,
                # 普通批量模式也保留已提交作品的 ID；下载/导出失败时重试
                # 只重新下载这些作品，避免再次提交讯飞合成。
                "xunfei_works_ids": (
                    dict(item.get("xunfei_works_ids"))
                    if isinstance(item.get("xunfei_works_ids"), dict)
                    else {}
                ),
                # “已确认提交但 worksId 不确定”不能按普通错误重试；保留
                # 作品名，下一轮只做列表对账，找回后再下载。
                "xunfei_ambiguous_works": (
                    dict(item.get("xunfei_ambiguous_works"))
                    if isinstance(item.get("xunfei_ambiguous_works"), dict)
                    else {}
                ),
            })
            # 合并生成已经有作品级阶段日志；如果再为每一题写一条“已排队”，
            # 22 行对话会把日志时间线刷满，而且这些条目随后也不会单独提交。
            # 失败项仍在下面保留逐项记录，便于定位问题。
            if not composite_mode:
                log(
                    "progress",
                    f"已加入批量生成: {item_id}",
                    stage="synthesize",
                    kind="item",
                    key=f"item:{item_id}",
                    title=f"已排队 {item_id}",
                    detail=" · ".join(filter(None, [item.get("doc_type"), item.get("category"), voice_label])),
                    item=item_context,
                    progress_snapshot=progress,
                )

        for item in empty_items:
            log(
                "warn",
                f"{item['id']} — 文本为空，跳过",
                stage="synthesize",
                kind="item",
                status="error",
                key=f"item:{item['id']}",
                title=f"{item['id']} 未生成",
                detail="对应内容为空，已跳过这一条",
                item={
                    "id": item["id"],
                    "filename": item.get("filename"),
                    "doc_type": item.get("doc_type"),
                    "category": item.get("category"),
                    "voice_keys": _clean_voice_keys(item.get("voice_keys")),
                    "text_preview": item.get("text_preview"),
                },
                progress_snapshot=progress,
            )
        if empty_items:
            rebuild_stats_cache(progress)
            await persist_progress(progress, force=True)
            emit_stats(progress, force=True)
            emit_generation_status(progress)

        batch_results = {}
        batch_error = None
        batch_display_completed = progress["completed"]
        batch_display_failed = progress["failed"]
        batch_display_states = {}
        batch_display_completed_items = set()
        # 统一批量阶段还没有把 AudioSegment 导出到最终文件，不能直接修改
        # progress.completed；用一个单调的显示进度把“提交/下载/整理”阶段
        # 连起来，避免长时间停在 0% 或从较高进度倒退。
        batch_processed_floor = float(progress["completed"] + progress["failed"])
        batch_item_progress = {}
        progress_item_by_id = {
            str(item.get("id") or ""): item
            for item in progress.get("items", [])
        }
        composite_plan = []
        composite_work_states = {}
        composite_display_completed = progress["completed"]
        composite_display_failed = progress["failed"]
        composite_processed_floor = batch_processed_floor

        def composite_work_snapshot():
            states = list(composite_work_states.values())
            item_by_id = {
                str(item.get("id") or ""): item
                for item in progress.get("items", [])
            }
            total_segments = sum(
                max(0, int(state.get("item_count") or 0))
                for state in states
            )
            sliced_segments = sum(
                min(
                    max(0, int(state.get("item_count") or 0)),
                    max(0, int(state.get("cut_item_count") or 0)),
                )
                for state in states
            )
            exported_segments = sum(
                1
                for state in states
                for item_id in (state.get("item_ids") or [])
                if (item_by_id.get(str(item_id)) or {}).get("status") == "done"
            )
            return {
                "kind": "composite_batch",
                "completed": sum(
                    1 for state in states if state.get("status") == "done"
                ),
                "total": len(composite_plan),
                "submitted": sum(
                    1
                    for state in states
                    if state.get("status")
                    in {"submitted", "downloaded", "cut", "done"}
                ),
                "downloaded": sum(
                    1
                    for state in states
                    if state.get("status") in {"downloaded", "cut", "done"}
                ),
                "failed": sum(
                    1 for state in states if state.get("status") == "error"
                ),
                "sliced": min(sliced_segments, total_segments),
                "exported": min(exported_segments, total_segments),
            }

        def composite_segments_snapshot():
            states = list(composite_work_states.values())
            total = sum(max(0, int(state.get("item_count") or 0)) for state in states)
            completed = sum(
                min(
                    max(0, int(state.get("item_count") or 0)),
                    max(0, int(state.get("cut_item_count") or 0)),
                )
                for state in states
            )
            item_by_id = {
                str(item.get("id") or ""): item
                for item in progress.get("items", [])
            }
            exported = sum(
                1
                for state in states
                for item_id in (state.get("item_ids") or [])
                if (item_by_id.get(str(item_id)) or {}).get("status") == "done"
            )
            return {
                "completed": min(completed, total),
                "sliced": min(completed, total),
                "exported": min(exported, total),
                "total": total,
            }

        def composite_work_progress_floor():
            """把作品阶段映射为单题进度，且不重复计算已导出的题目。"""
            item_by_id = {
                str(item.get("id") or ""): item for item in progress.get("items", [])
            }
            stage_ratio = {
                "pending": 0.0,
                "submitted": 0.16,
                "downloaded": 0.56,
                "cut": 0.90,
                "done": 1.0,
                "error": 0.96,
            }
            value = float(progress["completed"] + progress["failed"])
            for state in composite_work_states.values():
                pending_count = sum(
                    1
                    for item_id in (state.get("item_ids") or [])
                    if (item_by_id.get(str(item_id)) or {}).get("status")
                    not in {"done", "error"}
                )
                value += pending_count * stage_ratio.get(
                    str(state.get("status") or "pending"),
                    0.0,
                )
            return value

        async def on_batch_item_progress(event):
            """把批量提交、下载和保存结果即时反映到 SSE 进度。"""
            nonlocal batch_display_completed, batch_display_failed, batch_processed_floor
            item_id = str(event.get("item_id") or "")
            status = str(event.get("status") or "")
            if not item_id or status not in {"submitted", "downloaded", "ready", "error"}:
                return

            # worksId 在普通批量模式中必须先于最终导出持久化。只保留一对一
            # 映射；重复 ID 属于跨任务歧义，直接丢弃对应的复用资格，
            # 不能让下一轮把同一个作品写入多个题目。
            progress_item = progress_item_by_id.get(item_id)
            if progress_item is not None and (
                isinstance(event.get("works_ids"), dict)
                or event.get("ambiguous_works_ids")
                or isinstance(event.get("ambiguous_works_names"), dict)
                or event.get("invalid_works_ids")
            ):
                raw_works = event.get("works_ids") or {}
                ambiguous_segments = {
                    str(value).strip()
                    for value in (event.get("ambiguous_works_ids") or [])
                    if str(value or "").strip()
                }
                invalid_segments = {
                    str(value).strip()
                    for value in (event.get("invalid_works_ids") or [])
                    if str(value or "").strip()
                }
                ambiguous_names = {
                    str(segment_id or "").strip(): str(works_name or "").strip()
                    for segment_id, works_name in (event.get("ambiguous_works_names") or {}).items()
                    if str(segment_id or "").strip() and str(works_name or "").strip()
                }
                excluded_segments = ambiguous_segments | invalid_segments
                normalized_works = {}
                for raw_segment_id, raw_works_id in raw_works.items():
                    segment_id = str(raw_segment_id or "").strip()
                    works_id = str(raw_works_id or "").strip()
                    if segment_id and works_id and segment_id not in excluded_segments:
                        normalized_works[segment_id] = works_id
                works_counts = {}
                for works_id in normalized_works.values():
                    works_counts[works_id] = works_counts.get(works_id, 0) + 1
                safe_works = {
                    segment_id: works_id
                    for segment_id, works_id in normalized_works.items()
                    if works_counts[works_id] == 1
                }
                previous_works = progress_item.get("xunfei_works_ids")
                previous_works = (
                    dict(previous_works)
                    if isinstance(previous_works, dict)
                    else {}
                )
                persisted_works = {
                    segment_id: works_id
                    for segment_id, works_id in previous_works.items()
                    if segment_id not in excluded_segments
                }
                persisted_works.update(safe_works)
                previous_ambiguous = progress_item.get("xunfei_ambiguous_works")
                previous_ambiguous = (
                    dict(previous_ambiguous)
                    if isinstance(previous_ambiguous, dict)
                    else {}
                )
                persisted_ambiguous = {
                    segment_id: works_name
                    for segment_id, works_name in previous_ambiguous.items()
                    if segment_id not in excluded_segments
                    and segment_id not in safe_works
                }
                for segment_id in ambiguous_segments:
                    if ambiguous_names.get(segment_id):
                        persisted_ambiguous[segment_id] = ambiguous_names[segment_id]
                if (
                    previous_works != persisted_works
                    or previous_ambiguous != persisted_ambiguous
                ):
                    progress_item["xunfei_works_ids"] = persisted_works
                    progress_item["xunfei_ambiguous_works"] = persisted_ambiguous
                    await persist_progress(progress, force=True)

            previous = batch_display_states.get(item_id, "pending")
            total_segments = max(1, int(event.get("total_segments") or 1))
            completed_segments = min(
                total_segments,
                max(0, int(event.get("completed_segments") or 0)),
            )
            segment_ratio = completed_segments / total_segments

            # 每道题在批量阶段占用一个“进度单位”：提交到 45%，下载到
            # 85%，保存完成到 95%，最后的导出再由真实 completed 推到 100%。
            if status == "submitted":
                item_progress = 0.05 + segment_ratio * 0.40
            elif status == "downloaded":
                item_progress = 0.50 + segment_ratio * 0.35
            elif status == "ready":
                item_progress = 0.95
            else:
                item_progress = 1.0
            batch_item_progress[item_id] = max(
                float(batch_item_progress.get(item_id, 0.0)),
                item_progress,
            )
            batch_processed_floor = max(
                batch_processed_floor,
                float(progress["completed"] + progress["failed"])
                + sum(batch_item_progress.values()),
            )

            if status == "downloaded":
                # 下载事件先于最终 save_as 结果到达，先让用户看到真实的
                # 下载进度；如果保存失败，error 状态会把失败项计入已处理。
                if (
                    completed_segments >= total_segments
                    and item_id not in batch_display_completed_items
                ):
                    batch_display_completed_items.add(item_id)
                    batch_display_completed += 1
                batch_display_states[item_id] = "downloaded"
            elif status == "submitted":
                batch_display_states[item_id] = "submitted"
            elif status == "ready":
                if item_id not in batch_display_completed_items:
                    batch_display_completed_items.add(item_id)
                    batch_display_completed += 1
                batch_display_states[item_id] = "ready"
            elif status == "error":
                if item_id in batch_display_completed_items:
                    batch_display_completed_items.discard(item_id)
                    batch_display_completed = max(0, batch_display_completed - 1)
                if previous != "error":
                    batch_display_failed += 1
                batch_display_states[item_id] = "error"

            phase = "batch-submit" if status == "submitted" else "batch-download"
            emit_stats(
                progress,
                force=True,
                completed_override=batch_display_completed,
                failed_override=batch_display_failed,
                processed_override=batch_processed_floor,
                phase=phase,
            )
            item_label = f"{item_id} 已提交，等待下载"
            if status == "submitted":
                item_label = f"{item_id} 已提交音频段 {completed_segments}/{total_segments}"
            if status == "ready":
                item_label = f"{item_id} 下载完成，等待整理"
            elif status == "error":
                item_label = f"{item_id} 下载失败"
            elif status == "downloaded":
                item_label = f"{item_id} 已下载音频段 {completed_segments}/{total_segments}"
            emit_generation_status(
                progress,
                current_item=item_label,
                force=True,
                completed_override=batch_display_completed,
                failed_override=batch_display_failed,
                processed_override=batch_processed_floor,
            )

        async def on_composite_progress(event):
            """把合并作品的提交、下载和安全切割进度写入 SSE。"""
            nonlocal composite_processed_floor
            if not isinstance(event, dict):
                return
            work_id = str(event.get("work_id") or event.get("job_id") or "").strip()
            if not work_id:
                return
            state = composite_work_states.get(work_id)
            if not isinstance(state, dict):
                return

            incoming = str(event.get("status") or "").strip()
            if incoming not in {"submitted", "downloaded", "cut", "error"}:
                return
            previous = str(state.get("status") or "pending")
            order = {
                "pending": 0,
                "submitted": 1,
                "downloaded": 2,
                "cut": 3,
                "done": 4,
                "error": 5,
            }
            if (
                incoming != "error"
                and previous != "error"
                and order.get(incoming, 0) <= order.get(previous, 0)
            ):
                return

            state["status"] = incoming
            if event.get("works_name"):
                state["works_name"] = str(event["works_name"])[:25]
            if event.get("ambiguous_works_id") or event.get("works_id_invalid"):
                # 该 worksId 无法唯一归属或已被讯飞确认失效，不能在断点
                # 中继续复用；否则下一轮会继续错配或永远下载同一个坏 ID。
                state["works_id"] = None
                state["ambiguous_submission"] = bool(
                    event.get("ambiguous_works_id")
                )
            elif event.get("works_id"):
                state["works_id"] = str(event["works_id"])
                state["ambiguous_submission"] = False
            if isinstance(event.get("cut_diagnostics"), dict):
                state["cut_diagnostics"] = dict(event["cut_diagnostics"])
            if incoming == "cut":
                state["cut_item_count"] = min(
                    max(0, int(state.get("item_count") or 0)),
                    max(0, int(event.get("cut_item_count") or 0)),
                )
                state["error"] = None
            elif incoming == "error":
                state["error"] = str(event.get("error") or "合并作品处理失败")[:1200]
            state["updated_at"] = core.now_str()

            work_snapshot = composite_work_snapshot()
            segment_snapshot = composite_segments_snapshot()
            composite_processed_floor = max(
                composite_processed_floor,
                composite_work_progress_floor(),
            )
            phase_by_status = {
                "submitted": "composite-submit",
                "downloaded": "composite-download",
                "cut": "composite-cut",
                "error": "composite-error",
            }
            phase = phase_by_status[incoming]
            work_index = int(state.get("work_index") or 0)
            total_works = len(composite_plan)
            item_count = int(state.get("item_count") or 0)
            works_id = str(state.get("works_id") or "")
            work_detail = {
                "id": work_id,
                "index": work_index,
                "total": total_works,
                "status": incoming,
                "item_count": item_count,
                "item_ids": list(state.get("item_ids") or []),
                "works_id": works_id,
                "works_name": str(state.get("works_name") or ""),
            }
            if state.get("ambiguous_submission"):
                work_detail["ambiguous_submission"] = True
            if isinstance(state.get("cut_diagnostics"), dict):
                work_detail["cut_diagnostics"] = dict(state["cut_diagnostics"])
            if state.get("error"):
                work_detail["error"] = state["error"]
            emit_stats(
                progress,
                force=True,
                completed_override=composite_display_completed,
                failed_override=composite_display_failed,
                processed_override=composite_processed_floor,
                phase=phase,
                work=work_snapshot,
                segments=segment_snapshot,
            )
            if incoming == "submitted":
                current_item = (
                    f"合并作品 {work_index}/{total_works} 已提交 · "
                    f"包含 {item_count} 条"
                )
                level = "progress"
                title = "多人配音合并作品已提交"
                detail = (
                    f"作品 {work_index}/{total_works} · {item_count} 条题目"
                    + (f" · worksId {works_id}" if works_id else "")
                )
            elif incoming == "downloaded":
                current_item = (
                    f"合并作品 {work_index}/{total_works} 已下载 · "
                    f"等待安全切割"
                )
                level = "progress"
                title = "合并音频已下载"
                detail = (
                    f"作品 {work_index}/{total_works} · 已取得完整合并音频，"
                    "不会按时长比例猜测题目边界"
                )
            elif incoming == "cut":
                cut_count = int(state.get("cut_item_count") or 0)
                current_item = (
                    f"合并作品 {work_index}/{total_works} 已安全切割 · "
                    f"{cut_count}/{item_count} 条"
                )
                level = "success"
                title = "合并音频已安全切割"
                detail = (
                    f"作品 {work_index}/{total_works} · 已从人工停顿恢复 "
                    f"{cut_count} 条独立音频，保留首尾保护间隔"
                )
                diagnostic_text = core.format_composite_cut_diagnostics(
                    state.get("cut_diagnostics")
                )
                if diagnostic_text:
                    detail = f"{detail} · {diagnostic_text}"
            else:
                current_item = f"合并作品 {work_index}/{total_works} 处理失败"
                level = "error"
                title = "多人配音合并作品失败"
                detail = str(state.get("error") or "合并作品处理失败")
                diagnostic_text = core.format_composite_cut_diagnostics(
                    state.get("cut_diagnostics")
                )
                if diagnostic_text and diagnostic_text not in detail:
                    detail = f"{detail} · {diagnostic_text}"
            log(
                level,
                detail,
                stage="synthesize",
                kind="work",
                key=f"work:{work_id}:{incoming}",
                title=title,
                detail=detail,
                work=work_detail,
                segments=segment_snapshot,
                progress_snapshot=progress,
            )
            emit_generation_status(
                progress,
                current_item=current_item,
                force=True,
                completed_override=composite_display_completed,
                failed_override=composite_display_failed,
                processed_override=composite_processed_floor,
            )
            await persist_progress(progress, force=True)

        if item_specs:
            if composite_mode:
                try:
                    composite_plan = core.build_composite_work_plan(
                        item_specs,
                        existing_plan=progress.get("composite_work_plan") or None,
                    )
                    previous_states = progress.get("composite_works") or {}
                    if not isinstance(previous_states, dict):
                        previous_states = {}
                    composite_work_states = {}
                    for work_index, work in enumerate(composite_plan, start=1):
                        work_id = str(work["work_id"])
                        previous = previous_states.get(work_id)
                        previous = previous if isinstance(previous, dict) else {}
                        previous_status = str(previous.get("status") or "pending")
                        if previous_status == "complete":
                            previous_status = "done"
                        composite_work_states[work_id] = {
                            "work_id": work_id,
                            "work_index": work_index,
                            "item_ids": list(work.get("item_ids") or []),
                            "item_count": int(work.get("item_count") or 0),
                            "char_count": int(work.get("char_count") or 0),
                            "works_name": str(
                                previous.get("works_name")
                                or work.get("works_name")
                                or ""
                            ),
                            "status": previous_status,
                            "works_id": str(previous.get("works_id") or "") or None,
                            "ambiguous_submission": bool(
                                previous.get("ambiguous_submission")
                                or previous.get("ambiguous_works_id")
                            ),
                            "cut_item_count": int(previous.get("cut_item_count") or 0),
                            "cut_diagnostics": (
                                dict(previous["cut_diagnostics"])
                                if isinstance(previous.get("cut_diagnostics"), dict)
                                else None
                            ),
                            "error": previous.get("error"),
                        }
                    progress["composite_work_plan"] = composite_plan
                    progress["composite_works"] = composite_work_states
                    await persist_progress(progress, force=True)
                    log(
                        "progress",
                        f"构造 {len(composite_plan)} 个多人配音合并作品",
                        stage="synthesize",
                        kind="stage",
                        key="stage:synthesize:composite",
                        title="构造多人配音合并作品",
                        detail=(
                            f"{len(item_specs)} 条题目将一次性提交；"
                            f"单个作品最多 {core.COMPOSITE_MAX_ITEMS_PER_WORK} 条；"
                            "仅在讯飞字数、条目安全上限或断点计划要求时拆分作品"
                        ),
                        progress_snapshot=progress,
                    )
                    emit_status(
                        f"全部文本合并生成中 — {len(composite_plan)} 个作品，"
                        f"待处理 {len(item_specs)} 道题"
                    )
                    composite_processed_floor = max(
                        composite_processed_floor,
                        composite_work_progress_floor(),
                    )
                    emit_stats(
                        progress,
                        force=True,
                        processed_override=composite_processed_floor,
                        phase="composite-plan",
                        work=composite_work_snapshot(),
                        segments=composite_segments_snapshot(),
                    )
                    batch_results = await core._synth_items_batch_composite(
                        item_specs,
                        progress_callback=on_composite_progress,
                        work_plan=progress.get("composite_work_plan"),
                        resume=progress.get("composite_works"),
                        debug_dir=os.path.join(session_dir, "composite"),
                        cancel_check=lambda: session.cancelled,
                    )
                except Exception as error:
                    batch_error = str(error)
            else:
                log(
                    "progress",
                    f"按音色与参数分组提交 {len(item_specs)} 道题",
                    stage="synthesize",
                    kind="stage",
                    key="stage:synthesize:batch",
                    title="分组生成音频",
                    detail="相同音色、语速、语调、音量只切换一次；全部提交后统一按 worksId 下载",
                    progress_snapshot=progress,
                )
                emit_status(f"按音色分组生成中 — 待处理 {len(item_specs)} 道题")
                try:
                    batch_results = await core._synth_items_batch(
                        item_specs,
                        progress_callback=on_batch_item_progress,
                        cancel_check=lambda: session.cancelled,
                    )
                except Exception as error:
                    batch_error = str(error)

        # 已完成任务再次打开/重放时不会重新构造 item_specs，但最终 done
        # 事件仍应保留上次的合并作品汇总，不能因为本轮没有待处理题目而
        # 把作品数量显示成 0。
        if composite_mode and not item_specs and not composite_work_states:
            stored_plan = progress.get("composite_work_plan")
            stored_states = progress.get("composite_works")
            if isinstance(stored_plan, list):
                composite_plan = [
                    dict(work)
                    for work in stored_plan
                    if isinstance(work, dict) and work.get("work_id")
                ]
            if isinstance(stored_states, dict):
                for index, work in enumerate(composite_plan, start=1):
                    work_id = str(work["work_id"])
                    state = stored_states.get(work_id)
                    state = dict(state) if isinstance(state, dict) else {}
                    if state.get("status") == "complete":
                        state["status"] = "done"
                    state.setdefault("work_id", work_id)
                    state.setdefault("work_index", index)
                    state.setdefault("item_ids", list(work.get("item_ids") or []))
                    state.setdefault("item_count", int(work.get("item_count") or 0))
                    state.setdefault("char_count", int(work.get("char_count") or 0))
                    composite_work_states[work_id] = state

        def update_composite_work_after_item(item):
            if not composite_mode:
                return None
            item_id = str(item.get("id") or "")
            matched = None
            item_by_id = {
                str(candidate.get("id") or ""): candidate
                for candidate in progress.get("items", [])
            }
            for state in composite_work_states.values():
                if item_id not in {str(value) for value in (state.get("item_ids") or [])}:
                    continue
                matched = state
                members = [item_by_id.get(str(value)) for value in state.get("item_ids") or []]
                members = [member for member in members if member is not None]
                if members and any(member.get("status") == "error" for member in members):
                    state["status"] = "error"
                    state["error"] = next(
                        (
                            str(member.get("error") or "")[:1200]
                            for member in members
                            if member.get("status") == "error" and member.get("error")
                        ),
                        state.get("error") or "合并作品中有题目生成失败",
                    )
                elif members and all(member.get("status") == "done" for member in members):
                    state["status"] = "done"
                    state["cut_item_count"] = int(state.get("item_count") or len(members))
                    state["error"] = None
                state["updated_at"] = core.now_str()
                break
            return matched

        for item in progress["items"]:
            item_id = item["id"]
            if item["status"] == "done" or item_id not in item_contexts:
                continue
            item_context = item_contexts[item_id]
            item_started = item_started_at.get(item_id, time.perf_counter())
            result = batch_results.get(item_id) if isinstance(batch_results, dict) else None
            audio_seg = result.get("audio") if isinstance(result, dict) else None
            error = batch_error or (
                result.get("error") if isinstance(result, dict) else None
            )
            try:
                if error or audio_seg is None:
                    raise RuntimeError(error or "讯飞批量生成未返回音频")
                out_path = os.path.join(audio_dir, item["filename"])
                await asyncio.to_thread(core.export_audio, audio_seg, fmt, quality, out_path)
                item["status"] = "done"
                item["output_path"] = out_path
                item["error"] = None
                progress["completed"] += 1
                if not composite_mode:
                    log(
                        "success",
                        f"{item_id} 完成 ({progress['completed']}/{total})",
                        stage="synthesize",
                        kind="item",
                        key=f"item:{item_id}",
                        title=f"{item_id} 已生成",
                        detail=f"{item.get('filename')} · {item_context['voice']} · 第 {progress['completed']} / {total} 条",
                        item=item_context,
                        progress_snapshot=progress,
                        duration_ms=round((time.perf_counter() - item_started) * 1000),
                    )
            except Exception as error:
                item["status"] = "error"
                item["error"] = str(error)
                progress["failed"] += 1
                log(
                    "error",
                    f"{item_id} 失败: {error}",
                    stage="synthesize",
                    kind="item",
                    key=f"item:{item_id}",
                    title=f"{item_id} 生成失败",
                    detail=str(error),
                    item=item_context,
                    progress_snapshot=progress,
                    duration_ms=round((time.perf_counter() - item_started) * 1000),
                )
            update_composite_work_after_item(item)
            update_stats_cache(progress, item)
            await persist_progress(progress)
            if composite_mode:
                composite_display_completed = progress["completed"]
                composite_display_failed = progress["failed"]
                composite_processed_floor = max(
                    composite_processed_floor,
                    composite_work_progress_floor(),
                )
                display_processed = composite_processed_floor
                display_phase = "composite-export"
                display_work = composite_work_snapshot()
                display_segments = composite_segments_snapshot()
            else:
                display_processed = batch_processed_floor
                display_phase = "batch-export"
                display_work = None
                display_segments = None
            if display_processed > progress["completed"] + progress["failed"]:
                emit_stats(
                    progress,
                    processed_override=display_processed,
                    phase=display_phase,
                    work=display_work,
                    segments=display_segments,
                )
                emit_generation_status(
                    progress,
                    current_item=f"{item_id} 正在整理音频",
                    force=True,
                    processed_override=display_processed,
                )
            else:
                emit_stats(
                    progress,
                    work=display_work,
                    segments=display_segments,
                    phase=display_phase if composite_mode else None,
                )
                emit_generation_status(progress)

        if composite_mode and composite_work_states:
            for completed_item in progress.get("items", []):
                update_composite_work_after_item(completed_item)
            composite_display_completed = progress["completed"]
            composite_display_failed = progress["failed"]
            composite_processed_floor = max(
                composite_processed_floor,
                composite_work_progress_floor(),
            )
            await persist_progress(progress, force=True)
            emit_stats(
                progress,
                force=True,
                processed_override=composite_processed_floor,
                phase="composite-export",
                work=composite_work_snapshot(),
                segments=composite_segments_snapshot(),
            )

        if session.cancelled:
            await persist_progress(progress, force=True)
            emit_cancelled_terminal(
                f"{generation_mode_label}阶段结束后已停止后续任务"
            )
            return

        if composite_mode and composite_work_states:
            emit_stats(
                progress,
                force=True,
                processed_override=composite_processed_floor,
                phase="composite-export",
                work=composite_work_snapshot(),
                segments=composite_segments_snapshot(),
            )
        else:
            emit_stats(progress, force=True, phase="batch-export")
        # 即使题目已经全部导出，ZIP 打包和历史归档仍未完成；明确保留
        # “生成阶段结束”的阶段标识，前端不会把这一条中间状态当作终态。
        emit_status("生成阶段完成，正在整理交付文件...")

        synthesis_duration_ms = round((time.perf_counter() - synthesis_started_at) * 1000)
        synthesis_all_failed = progress["completed"] == 0 and progress["failed"] > 0
        log(
            "error" if synthesis_all_failed else ("success" if progress["failed"] == 0 else "warn"),
            f"音频生成阶段结束：成功 {progress['completed']}，失败 {progress['failed']}",
            stage="synthesize",
            kind="stage",
            key="stage:synthesize",
            title=(
                "音频生成失败"
                if synthesis_all_failed
                else ("音频生成完成" if progress["failed"] == 0 else "音频已部分生成")
            ),
            detail=f"成功 {progress['completed']} / {total}" + (f" · 失败 {progress['failed']}" if progress["failed"] else ""),
            progress_snapshot=progress,
            work=composite_work_snapshot() if composite_mode else None,
            segments=composite_segments_snapshot() if composite_mode else None,
            duration_ms=synthesis_duration_ms,
        )

        # ---- 清理 + 打包 ----
        if session.cancelled:
            emit_cancelled_terminal("音频生成阶段结束后已停止交付整理")
            return
        progress["status"] = "packaging"
        await persist_progress(progress, force=True)
        package_started_at = time.perf_counter()
        emit_stats(
            progress,
            force=True,
            phase="package",
            work=composite_work_snapshot() if composite_mode else None,
            segments=composite_segments_snapshot() if composite_mode else None,
        )
        emit_status("正在打包交付文件...")
        if progress["completed"] > 0:
            log(
                "progress",
                "正在打包 ZIP",
                stage="package",
                kind="stage",
                key="stage:package",
                title="整理交付文件",
                detail=f"正在将 {progress['completed']} 个音频打包为 ZIP",
                progress_snapshot=progress,
            )
            emit_status("正在打包...")
            zip_path = await asyncio.to_thread(core.create_zip, session_dir, progress)
            log(
                "success",
                "ZIP 打包完成",
                stage="package",
                kind="stage",
                key="stage:package",
                title="交付文件已整理完成",
                detail=f"ZIP 中包含 {progress['completed']} 个已生成音频",
                progress_snapshot=progress,
                duration_ms=round((time.perf_counter() - package_started_at) * 1000),
            )
        else:
            zip_path = ""
            log(
                "warn",
                "没有成功生成的音频，已跳过 ZIP 打包",
                stage="package",
                kind="stage",
                key="stage:package",
                title="没有可整理的交付文件",
                detail=f"本次 {progress['failed']} 条内容均未生成成功，请查看失败记录后重试",
                progress_snapshot=progress,
                duration_ms=round((time.perf_counter() - package_started_at) * 1000),
            )
        progress["status"] = "done"
        await persist_progress(progress, force=True)

        if session.cancelled:
            emit_cancelled_terminal("交付文件整理结束后已停止任务")
            return

        done = progress["completed"]
        failed = progress["failed"]

        file_list = core.get_completed_file_list(progress)
        if done == 0 and failed > 0:
            status_text = f"任务结束 — {failed} 条均未生成成功"
        else:
            status_text = f"完成 — 成功 {done}/{total}"
        if failed > 0 and done > 0:
            status_text += f"，失败 {failed}"
        emit_download(progress, file_list, zip_path=zip_path)
        # 保存最终 zip_path 供 SSE 重连重放
        session.final_zip_path = zip_path if file_list else None
        history_record = None
        if session.cancelled:
            emit_cancelled_terminal("任务在保存历史记录前已停止")
            return
        if file_list:
            archive_started_at = time.perf_counter()
            emit_stats(
                progress,
                force=True,
                phase="archive",
                work=composite_work_snapshot() if composite_mode else None,
                segments=composite_segments_snapshot() if composite_mode else None,
            )
            emit_status("交付文件已整理，正在保存历史记录...")
            log(
                "progress",
                "正在保存历史记录",
                stage="archive",
                kind="stage",
                key="stage:archive",
                title="保存到历史记录",
                detail="完成后可从历史记录重新试听和下载",
                progress_snapshot=progress,
            )
            try:
                history_record = await asyncio.to_thread(
                    archive_history_record,
                    session,
                    progress,
                    file_list,
                    session.final_zip_path,
                )
                log(
                    "success",
                    "历史记录保存完成",
                    stage="archive",
                    kind="stage",
                    key="stage:archive",
                    title="已保存到历史记录",
                    detail="结果已安全保存在当前电脑，历史记录最多保留 20 条",
                    progress_snapshot=progress,
                    duration_ms=round((time.perf_counter() - archive_started_at) * 1000),
                )
            except Exception as history_error:
                log(
                    "warn",
                    f"历史记录保存失败：{history_error}",
                    stage="archive",
                    kind="stage",
                    key="stage:archive",
                    title="历史记录保存失败",
                    detail=f"请先下载本次结果后再新建任务。原因：{history_error}",
                    progress_snapshot=progress,
                    duration_ms=round((time.perf_counter() - archive_started_at) * 1000),
                )
        else:
            emit_stats(
                progress,
                force=True,
                phase="archive",
                work=composite_work_snapshot() if composite_mode else None,
                segments=composite_segments_snapshot() if composite_mode else None,
            )
            emit_status("没有可保存的音频结果，正在结束任务...")
            log(
                "warn",
                "没有可保存的音频结果",
                stage="archive",
                kind="stage",
                key="stage:archive",
                title="本次未写入历史记录",
                detail="没有成功生成的音频文件，可返回配置后重试",
                progress_snapshot=progress,
            )
        if session.cancelled:
            emit_cancelled_terminal("历史记录处理结束后已停止任务")
            return
        emit_status(status_text)
        task_duration_ms = round((time.perf_counter() - task_started_at) * 1000)
        all_failed = done == 0 and failed > 0
        log(
            "error" if all_failed else ("success" if failed == 0 else "warn"),
            f"任务完成：成功 {done}/{total}" + (f"，失败 {failed}" if failed > 0 else ""),
            stage="complete",
            kind="summary",
            key="task:summary",
            title=(
                "本次未生成可交付音频"
                if all_failed
                else ("全部处理完成" if failed == 0 else "任务已完成，部分内容需要关注")
            ),
            detail=(
                f"成功 {done} 条 · 失败 {failed} 条 · {str(fmt).upper()} · "
                f"共用时 {task_duration_ms / 1000:.1f} 秒"
            ),
            progress_snapshot=progress,
            duration_ms=task_duration_ms,
        )
        done_event = {
            "type": "done",
            "zip_path": session.final_zip_path,
            "zip_available": bool(session.final_zip_path),
            "history_id": history_record.get("id") if history_record else None,
            "completed": done,
            "failed": failed,
            "total": total,
            "file_count": len(file_list),
            "duration_ms": task_duration_ms,
            "generation_mode": generation_mode,
        }
        if composite_mode:
            done_event["composite_works"] = composite_work_snapshot()
        push_event(session, done_event)

    except asyncio.CancelledError:
        emit_cancelled_terminal("生成进程已安全停止")
    except Exception as e:
        emit_error_terminal(
            "生成任务未能完成",
            str(e),
            status_text="生成任务未能完成",
        )
    finally:
        # 无论成功/失败/取消，都关闭讯飞配音浏览器会话，防止进程泄漏
        if core._XUNFEI_AVAILABLE:
            try:
                await core._xunfei.close_session()
            except asyncio.CancelledError:
                pass
            except Exception as close_err:
                print(f"[wordtts] 关闭讯飞配音浏览器异常: {close_err}", file=sys.stderr)
        push_event(session, {"type": "end"})


# ============================================================================
# API 请求/响应模型
# ============================================================================

class GenerateRequest(BaseModel):
    session_id: str
    source_filename: str
    file_path: str
    config: dict


class ParseRequest(BaseModel):
    file_path: str


# ============================================================================
# FastAPI 应用
# ============================================================================

app = FastAPI(title="小猪wordTTS API")
_API_TOKEN = os.environ.get("WORDTTS_API_TOKEN", "")
APP_VERSION = os.environ.get("WORDTTS_VERSION", "2.0.0")


@app.middleware("http")
async def authenticate_local_api(request, call_next):
    """Electron 启动的后端只接受本次进程生成的随机令牌。"""
    if _API_TOKEN and request.url.path.startswith("/api/"):
        supplied = request.headers.get("X-WordTTS-Token") or request.query_params.get("token")
        if not supplied or not secrets.compare_digest(supplied, _API_TOKEN):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "未授权的本地 API 请求"})
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    # 仅允许本地来源，防止恶意网页通过浏览器访问本地 API
    allow_origins=[
        "null",  # Electron file:// 协议的 origin 为 "null"
    ],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# 静态文件（如果存在 renderer 目录）
_renderer_dir = os.path.join(RESOURCE_DIR, "electron", "renderer")
if os.path.isdir(_renderer_dir):
    app.mount("/static", StaticFiles(directory=_renderer_dir), name="static")


@app.get("/api/health")
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


@app.get("/api/config")
async def get_config():
    """返回前端所需的配置选项，并在响应后异步刷新音色目录。"""
    # 配置接口是 Electron 首屏就绪的关键路径，只读取本地种子/缓存；如果
    # 这里等待讯飞在线目录超时，渲染器会一直显示“正在连接服务”，导致桌面
    # 端到端冒烟测试及离线启动失败。
    catalog = await asyncio.to_thread(_load_voice_catalog_sync, False)
    if (catalog.get("_meta") or {}).get("catalog_source") != "live":
        _schedule_voice_catalog_refresh()
    default_female = core.FEMALE_VOICE
    default_male = core.MALE_VOICE
    return {
        "formats": ["mp3"],
        "qualities": list(core.QUALITY_BITRATE.keys()),
        "supported_types": list(core.PARSER_MAP.keys()),
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
        "female_voice": "Amanda",
        "male_voice": "George",
        "voices": catalog.get("voices") or [],
        "voice_filters": catalog.get("filters") or [],
        "voice_catalog_meta": catalog.get("_meta") or {},
    }


@app.post("/api/voice-assets/cache")
async def cache_voice_assets(payload: dict):
    """后台缓存用户刚选中的音色头像和示例音频。"""
    raw_keys = payload.get("voice_keys") if isinstance(payload, dict) else []
    voice_keys = _clean_voice_keys(raw_keys)[:64]
    if not voice_keys:
        return {"status": "ok", "cached": {}}
    await asyncio.to_thread(_load_voice_catalog_sync, False)
    cached = await asyncio.to_thread(_cache_voice_assets_sync, voice_keys)
    return {"status": "ok", "cached": cached}


@app.get("/api/voice-assets/{voice_key}/{kind}")
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


@app.get("/api/diagnose")
async def diagnose():
    """返回诊断信息（用于调试）。"""
    import shutil
    import subprocess
    import platform
    
    diagnose_info = {
        "platform": platform.system(),
        "platform_version": platform.version(),
        "python_version": platform.python_version(),
        "is_frozen": getattr(sys, "frozen", False),
        "resource_dir": RESOURCE_DIR,
        "base_dir": BASE_DIR,
        "output_base": core.OUTPUT_BASE,
        "ffmpeg": {},
        "ffprobe": {},
    }
    
    # 检查 ffmpeg
    ffmpeg_path = getattr(core, "_ffmpeg_path", None)
    diagnose_info["ffmpeg"]["path"] = ffmpeg_path
    
    if ffmpeg_path and os.path.isfile(ffmpeg_path):
        try:
            result = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                version_line = result.stderr.split("\n")[0] if result.stderr else result.stdout.split("\n")[0]
                diagnose_info["ffmpeg"]["version"] = version_line.strip()
                diagnose_info["ffmpeg"]["status"] = "ok"
            else:
                diagnose_info["ffmpeg"]["status"] = f"failed (exit code {result.returncode})"
                diagnose_info["ffmpeg"]["error"] = result.stderr[:200] if result.stderr else result.stdout[:200]
        except Exception as e:
            diagnose_info["ffmpeg"]["status"] = "error"
            diagnose_info["ffmpeg"]["error"] = str(e)
    else:
        diagnose_info["ffmpeg"]["status"] = "not_found"
    
    # 检查 ffprobe
    ffprobe_path = shutil.which("ffprobe")
    diagnose_info["ffprobe"]["path"] = ffprobe_path
    if ffprobe_path:
        try:
            result = subprocess.run(
                [ffprobe_path, "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            diagnose_info["ffprobe"]["status"] = "ok" if result.returncode == 0 else "failed"
        except Exception as e:
            diagnose_info["ffprobe"]["status"] = "error"
            diagnose_info["ffprobe"]["error"] = str(e)
    else:
        diagnose_info["ffprobe"]["status"] = "not_found"
    
    # 检查 imageio_ffmpeg
    try:
        import imageio_ffmpeg
        try:
            ie_path = imageio_ffmpeg.get_ffmpeg_exe()
            diagnose_info["imageio_ffmpeg"] = {
                "version": imageio_ffmpeg.__version__,
                "get_ffmpeg_exe": ie_path,
            }
        except Exception as e:
            diagnose_info["imageio_ffmpeg"] = {
                "version": imageio_ffmpeg.__version__,
                "error": str(e),
            }
    except ImportError:
        diagnose_info["imageio_ffmpeg"] = {"status": "not_installed"}
    
    # 检查 pydub
    diagnose_info["pydub"] = {"converter": getattr(core.AudioSegment, "converter", "not_set")}
    
    return diagnose_info


@app.post("/api/parse")
async def parse_document_endpoint(req: ParseRequest):
    """解析 Word 文档，返回解析结果和会话信息。"""
    filepath = req.file_path
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=400, detail="文件不存在")
    if not filepath.lower().endswith(('.docx', '.xlsx')):
        raise HTTPException(status_code=400, detail="仅支持 .docx 或 .xlsx 格式")
    # 防止路径穿越：确保解析后的路径是真实文件（非符号链接等）
    real_path = os.path.realpath(filepath)
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=400, detail="路径无效或不是文件")

    source_filename = os.path.basename(filepath)
    session_id = core.sanitize_dirname(source_filename) + "_" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    session = get_or_create_session(session_id)
    session.progress = None
    session_dir = session.session_dir
    try:
        fingerprint = await asyncio.to_thread(source_fingerprint, real_path)
    except Exception as e:
        _sessions.pop(session_id, None)
        raise HTTPException(status_code=422, detail=f"无法读取文档: {e}")

    parse_results = await asyncio.to_thread(load_parse_cache, session_dir, fingerprint)
    if parse_results is None:
        blocked_dirs = {
            state.session_dir
            for state in _sessions.values()
            if state is not session
        }
        parse_results = await asyncio.to_thread(
            find_persisted_parse_cache, fingerprint, blocked_dirs
        )
    cache_reused = parse_results is not None
    if parse_results is None:
        try:
            parse_results, summary = await asyncio.to_thread(
                core.parse_document_auto, real_path
            )
        except Exception as e:
            _sessions.pop(session_id, None)
            raise HTTPException(status_code=500, detail=f"解析失败: {e}")
        if not parse_results:
            _sessions.pop(session_id, None)
            raise HTTPException(status_code=422, detail=summary or "未识别到支持的题型内容")
        await asyncio.to_thread(clear_generated_outputs, session_dir)
        await asyncio.to_thread(save_parse_cache, session_dir, parse_results, fingerprint)
    elif not os.path.exists(os.path.join(session_dir, "parsed.json")):
        await asyncio.to_thread(save_parse_cache, session_dir, parse_results, fingerprint)

    existing_progress = None
    if cache_reused:
        existing = core.load_progress(session_dir)
        if progress_is_reusable(existing, fingerprint):
            existing_progress = {
                "completed": existing["completed"],
                "total": existing["total_items"],
                "failed": existing["failed"],
                "status": existing["status"],
            }
        elif existing:
            await asyncio.to_thread(clear_generated_outputs, session_dir)

    session.parse_results = parse_results
    session.source_fingerprint = fingerprint

    return {
        "session_id": session_id,
        "source_filename": source_filename,
        "file_path": real_path,
        "parse_results": parse_results,
        "existing_progress": existing_progress,
    }


@app.post("/api/parse/upload")
async def parse_document_upload(file: UploadFile = File(...)):
    """上传 Word 文档并解析（浏览器模式）。"""
    if not file.filename or not file.filename.lower().endswith(('.docx', '.xlsx')):
        raise HTTPException(status_code=400, detail="仅支持 .docx 或 .xlsx 格式")

    # 保存到临时文件（防止路径穿越：只取文件名部分）
    safe_filename = os.path.basename(file.filename)
    if not safe_filename or safe_filename != file.filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    upload_dir = os.path.join(BASE_DIR, "tts_output", "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    # 添加时间戳后缀防止同名文件覆盖
    name_stem, name_ext = os.path.splitext(safe_filename)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    unique_filename = f"{name_stem}_{timestamp}{name_ext}"
    filepath = os.path.join(upload_dir, unique_filename)

    # 限制上传文件大小（最大 50MB）
    MAX_UPLOAD_SIZE = 50 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"文件过大，最大支持 {MAX_UPLOAD_SIZE // (1024*1024)}MB")
    with open(filepath, 'wb') as f:
        f.write(content)

    # 复用 parse 逻辑
    source_filename = file.filename
    session_id = core.sanitize_dirname(source_filename) + "_" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    session = get_or_create_session(session_id)
    session.progress = None

    session_dir = session.session_dir
    try:
        fingerprint = await asyncio.to_thread(source_fingerprint, filepath)
        blocked_dirs = {
            state.session_dir
            for state in _sessions.values()
            if state is not session
        }
        parse_results = await asyncio.to_thread(
            find_persisted_parse_cache, fingerprint, blocked_dirs
        )
        if parse_results is None:
            parse_results, summary = await asyncio.to_thread(
                core.parse_document_auto, filepath
            )
    except Exception as e:
        _sessions.pop(session_id, None)
        raise HTTPException(status_code=500, detail=f"解析失败: {e}")
    if not parse_results:
        _sessions.pop(session_id, None)
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise HTTPException(status_code=422, detail=summary or "未识别到支持的题型内容")

    await asyncio.to_thread(clear_generated_outputs, session_dir)
    await asyncio.to_thread(save_parse_cache, session_dir, parse_results, fingerprint)
    session.parse_results = parse_results
    session.source_fingerprint = fingerprint

    return {
        "session_id": session_id,
        "source_filename": source_filename,
        "file_path": filepath,
        "parse_results": parse_results,
        "existing_progress": None,
    }


@app.post("/api/generate")
async def generate_endpoint(req: GenerateRequest):
    """启动音频生成（异步），通过 SSE 推送进度。"""
    # 生成只能使用解析阶段已经注册的会话，不能在 cleanup 后由旧请求重新创建。
    session = _sessions.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在或已被清理")
    if session.cleaning_up:
        raise HTTPException(status_code=409, detail="会话正在清理，请重新导入文档")
    lifecycle_version = session.lifecycle_version

    # 防止并发生成：如果已有任务在运行，先取消旧任务并等待其退出
    if session.task and not session.task.done():
        session.cancelled = True
        try:
            await asyncio.wait_for(session.task, timeout=5.0)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            session.task.cancel()
            try:
                await asyncio.wait_for(session.task, timeout=2.0)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        except Exception:
            pass

    if (
        _sessions.get(req.session_id) is not session
        or session.lifecycle_version != lifecycle_version
        or session.cleaning_up
    ):
        raise HTTPException(status_code=409, detail="会话已结束，请重新导入文档")

    session.done = False
    session.ended = False
    session.event_seq = 0
    session.log_entries = []
    session.log_seq = 0
    session.cancelled = False
    session.status = "starting"
    session.final_download = None
    session.final_zip_path = None
    session.final_done = None
    session.final_error = None
    session.final_cancelled = None
    session.last_stats = None

    # 清空上一次生成的事件日志，避免旧终态污染新流。
    session.event_journal.clear()
    session.ensure_event_signal().clear()

    # 只接受当前讯飞配置；旧版 proxy、旧音色和倍率字段在这里被丢弃。
    normalized_config = core.normalize_tts_config(req.config)

    # 在后台启动生成任务（保存引用以防止并发和 orphaned task）
    session.task = asyncio.create_task(
        generate_audio_stream(session, req.source_filename, req.file_path, normalized_config)
    )

    return {"session_id": req.session_id, "status": "started"}


@app.get("/api/progress/{session_id}")
async def progress_sse(session_id: str):
    """SSE 端点：流式推送进度更新。"""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    async def event_stream():
        event_signal = session.ensure_event_signal()
        # 建立连接时一次性冻结快照及水位线。随后队列里早于该水位线的
        # status/stats/log 已被快照覆盖，必须跳过，避免重连后进度倒退。
        snapshot_event_seq = session.event_seq
        snapshot_logs = list(session.log_entries)
        snapshot_status = session.status
        snapshot_stats = copy.deepcopy(session.last_stats)
        snapshot_download = copy.deepcopy(session.final_download)
        snapshot_done_event = copy.deepcopy(session.final_done)
        snapshot_error = copy.deepcopy(session.final_error)
        snapshot_cancelled = copy.deepcopy(session.final_cancelled)
        snapshot_done = session.done
        snapshot_ended = session.ended
        snapshot_task_done = bool(session.task and session.task.done())

        # 如果任务已完成（正常结束），重放最终状态后关闭
        if snapshot_done:
            if snapshot_logs:
                yield f"data: {json.dumps({'type': 'log_init', 'entries': snapshot_logs}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'status', 'text': snapshot_status}, ensure_ascii=False)}\n\n"
            # 重放最终 download 事件（含文件列表）
            if snapshot_download:
                yield f"data: {json.dumps(snapshot_download, ensure_ascii=False)}\n\n"
            # 重放最后 stats 事件（进度条）
            if snapshot_stats:
                yield f"data: {json.dumps(snapshot_stats, ensure_ascii=False)}\n\n"
            # 重放完整 done 事件（包含 zip_path 与 history_id）
            done_event = snapshot_done_event or {"type": "done", "zip_path": session.final_zip_path}
            yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return

        # 如果任务已终止（错误或取消）但不是正常完成，直接发送 end
        # 防止 SSE 重连后队列已空导致永久挂在心跳循环
        if (
            snapshot_ended
            or snapshot_task_done
            or snapshot_error
            or snapshot_cancelled
        ) and not snapshot_done:
            # 先发送已有日志
            if snapshot_logs:
                yield f"data: {json.dumps({'type': 'log_init', 'entries': snapshot_logs}, ensure_ascii=False)}\n\n"
            # 发送当前状态和最后 stats
            yield f"data: {json.dumps({'type': 'status', 'text': snapshot_status}, ensure_ascii=False)}\n\n"
            if snapshot_stats:
                yield f"data: {json.dumps(snapshot_stats, ensure_ascii=False)}\n\n"
            if snapshot_download:
                yield f"data: {json.dumps(snapshot_download, ensure_ascii=False)}\n\n"
            if snapshot_cancelled:
                yield f"data: {json.dumps(snapshot_cancelled, ensure_ascii=False)}\n\n"
            if snapshot_error:
                yield f"data: {json.dumps(snapshot_error, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return

        # 先发送已有日志
        if snapshot_logs:
            yield f"data: {json.dumps({'type': 'log_init', 'entries': snapshot_logs}, ensure_ascii=False)}\n\n"

        # 发送当前状态和最后 stats
        yield f"data: {json.dumps({'type': 'status', 'text': snapshot_status}, ensure_ascii=False)}\n\n"
        if snapshot_stats:
            yield f"data: {json.dumps(snapshot_stats, ensure_ascii=False)}\n\n"

        # 流式发送新事件。每个连接维护自己的游标，从同一个有界日志读取，
        # 因此并行连接/重连不会分食事件。
        cursor_event_seq = snapshot_event_seq
        while True:
            if session.done:
                # done 可能紧跟在最后一条日志之后写入；此时不能依赖游标
                # 已逐条消费完，重放快照可保证汇总日志一定先于终态到达。
                if session.log_entries:
                    yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'status', 'text': session.status}, ensure_ascii=False)}\n\n"
                if session.final_download:
                    yield f"data: {json.dumps(session.final_download, ensure_ascii=False)}\n\n"
                if session.last_stats:
                    yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"
                done_event = session.final_done or {"type": "done", "zip_path": session.final_zip_path}
                yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'end'})}\n\n"
                return
            if (
                session.final_cancelled
                or session.final_error
                or session.ended
                or (session.task and session.task.done())
            ):
                if session.log_entries:
                    yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'status', 'text': session.status}, ensure_ascii=False)}\n\n"
                if session.last_stats:
                    yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"
                if session.final_download:
                    yield f"data: {json.dumps(session.final_download, ensure_ascii=False)}\n\n"
                if session.final_cancelled:
                    yield f"data: {json.dumps(session.final_cancelled, ensure_ascii=False)}\n\n"
                if session.final_error:
                    yield f"data: {json.dumps(session.final_error, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'end'})}\n\n"
                return

            journal_snapshot = list(session.event_journal)
            if journal_snapshot:
                oldest_seq = int(journal_snapshot[0].get("event_seq") or 0)
                if cursor_event_seq < oldest_seq - 1:
                    # 极慢连接落后到有界日志之外时，用最新快照重新同步。
                    cursor_event_seq = session.event_seq
                    if session.log_entries:
                        yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'status', 'text': session.status}, ensure_ascii=False)}\n\n"
                    if session.last_stats:
                        yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"
                    continue

            pending_events = [
                event for event in journal_snapshot
                if int(event.get("event_seq") or 0) > cursor_event_seq
            ]
            if pending_events:
                for event in pending_events:
                    cursor_event_seq = max(
                        cursor_event_seq,
                        int(event.get("event_seq") or 0),
                    )
                    event_type = event.get("type")
                    # 任一终止事件都以 session 快照为准重放完整结果。
                    if event_type in {"done", "end"} and session.done:
                        if session.log_entries:
                            yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"
                        if session.final_download:
                            yield f"data: {json.dumps(session.final_download, ensure_ascii=False)}\n\n"
                        if session.last_stats:
                            yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"
                        done_event = session.final_done or {"type": "done", "zip_path": session.final_zip_path}
                        yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps({'type': 'end'})}\n\n"
                        return
                    if event_type == "end" and session.final_cancelled:
                        if session.log_entries:
                            yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps(session.final_cancelled, ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps({'type': 'end'})}\n\n"
                        return
                    if event_type == "end" and session.final_error:
                        if session.log_entries:
                            yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps(session.final_error, ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps({'type': 'end'})}\n\n"
                        return
                    if event_type in {"error", "cancelled"} and session.log_entries:
                        yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event_type in {"done", "error", "cancelled", "end"}:
                        if event_type in {"done", "error", "cancelled"}:
                            yield f"data: {json.dumps({'type': 'end'})}\n\n"
                        return
                continue

            # 清除后再复查，避免事件恰好落在“检查为空”和 wait 之间而丢唤醒。
            event_signal.clear()
            if any(
                int(event.get("event_seq") or 0) > cursor_event_seq
                for event in session.event_journal
            ):
                continue
            try:
                await asyncio.wait_for(event_signal.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                if not session.done and not session.ended:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/session/{session_id}")
async def get_session_info(session_id: str):
    """获取会话当前状态。"""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "session_id": session_id,
        "status": session.status,
        "done": session.done,
        "log_count": len(session.log_entries),
    }


@app.get("/api/download/zip/{session_id}")
async def download_zip(session_id: str):
    """下载 ZIP 包。"""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if session.progress:
        zip_path = confined_file_path(session.session_dir, "output.zip")
        if os.path.exists(zip_path):
            # 使用源文件名作为 ZIP 下载文件名
            source_name = os.path.splitext(session.progress["source_file"])[0]
            download_name = f"{source_name}_tts.zip"
            return FileResponse(zip_path, filename=download_name, media_type="application/zip")

    raise HTTPException(status_code=404, detail="ZIP 文件不存在")


@app.get("/api/download/file/{session_id}/{filename}")
async def download_file(session_id: str, filename: str):
    """下载单个音频文件。"""
    # 防止路径穿越：只取文件名部分
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(status_code=400, detail="非法文件名")

    session = _sessions.get(session_id)
    if not session or not session.progress:
        raise HTTPException(status_code=404, detail="会话不存在")

    file_path = confined_file_path(
        os.path.join(session.session_dir, "audio"),
        safe_name,
    )

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(file_path, filename=safe_name)


@app.get("/api/file-path")
async def resolve_file_path(session_id: str, filename: str):
    """返回文件在磁盘上的绝对路径（供 Electron 原生下载使用）。"""
    # 防止路径穿越：只取文件名部分
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(status_code=400, detail="非法文件名")

    session = _sessions.get(session_id)
    if not session or not session.progress:
        raise HTTPException(status_code=404, detail="会话不存在")

    file_path = confined_file_path(
        os.path.join(session.session_dir, "audio"),
        safe_name,
    )

    # 先检查音频文件
    if os.path.exists(file_path):
        return {"path": file_path}

    # 再检查 ZIP（当请求的是 output.zip 时）
    if safe_name == "output.zip":
        zip_path = confined_file_path(session.session_dir, "output.zip")
        if os.path.exists(zip_path):
            return {"path": zip_path}

    raise HTTPException(status_code=404, detail="文件不存在")


@app.get("/api/history")
async def history_list():
    """返回最近完成的本机任务，最多 20 条。"""
    records = await asyncio.to_thread(list_history_records)
    return {"records": records, "limit": MAX_HISTORY_RECORDS}


@app.get("/api/history/{history_id}")
async def history_detail(history_id: str):
    record = await asyncio.to_thread(get_history_record, history_id)
    if not record:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    return record


@app.get("/api/history/{history_id}/zip")
async def history_download_zip(history_id: str):
    file_path, record = await asyncio.to_thread(resolve_history_asset, history_id, "output.zip")
    source_name = os.path.splitext(record["source_filename"])[0]
    return FileResponse(file_path, filename=f"{source_name}_tts.zip", media_type="application/zip")


@app.get("/api/history/{history_id}/file/{filename}")
async def history_download_file(history_id: str, filename: str):
    file_path, _record = await asyncio.to_thread(resolve_history_asset, history_id, filename)
    return FileResponse(file_path, filename=filename)


@app.get("/api/history/{history_id}/file-path")
async def history_file_path(history_id: str, filename: str):
    file_path, _record = await asyncio.to_thread(resolve_history_asset, history_id, filename)
    return {"path": file_path}


@app.delete("/api/history/{history_id}")
async def history_delete(history_id: str):
    deleted = await asyncio.to_thread(delete_history_record, history_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    return {"status": "ok", "deleted": deleted}


@app.post("/api/cleanup/{session_id}")
async def cleanup_session(session_id: str):
    """清理会话数据（删除生成的音频和临时文件）。"""
    session = _sessions.get(session_id)
    if not session:
        return {"status": "ok", "message": "会话不存在，无需清理"}

    # 先关闭会话入口，再等待旧任务；避免等待期间有新 generate 穿透。
    session.cleaning_up = True
    session.lifecycle_version += 1
    session.cancelled = True

    # 如果有正在运行的任务，等待其退出
    if session.task and not session.task.done():
        try:
            await asyncio.wait_for(session.task, timeout=5.0)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            if session.task and not session.task.done():
                session.task.cancel()
                try:
                    await asyncio.wait_for(session.task, timeout=2.0)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        except Exception:
            if session.task and not session.task.done():
                session.task.cancel()

    # 已归档的完成任务只释放内存；历史删除接口才负责物理删除成品。
    archived_record = await asyncio.to_thread(_read_history_manifest, session.session_dir)
    if archived_record and session.done:
        if _sessions.get(session_id) is session:
            del _sessions[session_id]
        return {"status": "ok", "message": "会话已释放，历史结果已保留", "archived": True}

    # 未完成会话的目录独占，可以安全清理。
    if os.path.exists(session.session_dir):
        shutil.rmtree(session.session_dir, ignore_errors=True)

    # 从全局会话表中移除
    if _sessions.get(session_id) is session:
        del _sessions[session_id]

    return {"status": "ok", "message": "已清理"}


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
    print(f"[server] 启动 FastAPI 服务器: http://127.0.0.1:{args.port}")
    # 将 uvicorn 默认日志从 stderr 改为 stdout，
    # 避免 Electron 控制台中所有日志都显示为 [python:err]
    _log_config = copy.deepcopy(_UVICORN_DEFAULT_LOG_CONFIG)
    _log_config["handlers"]["default"]["stream"] = "ext://sys.stdout"
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", log_config=_log_config)
