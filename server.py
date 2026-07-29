#!/usr/bin/env python3
"""
FastAPI 后端服务器 — Word → TTS 一体化应用
============================================
复用 word_tts_app.py 中的核心 TTS 逻辑，
通过 REST API + SSE 提供服务，供 Electron 前端调用。
"""

import os
import sys
import json
import shutil
import asyncio
import hashlib
import argparse
import secrets
from datetime import datetime

# ============================================================================
# 路径设置（与 word_tts_app.py 一致）
# ============================================================================
# PyInstaller 兼容：打包后 __file__ 指向临时解压目录，
# BASE_DIR 需要指向可执行文件所在目录（用于写入输出文件），
# RESOURCE_DIR 指向 _MEIPASS（用于读取打包的只读资源）。
if getattr(sys, 'frozen', False):
    # 打包模式：BASE_DIR 指向用户数据目录（可写、持久化），
    # 避免写入 .app 包内部（代码签名后不可写，更新时会被擦除）。
    if sys.platform == 'darwin':
        BASE_DIR = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'WordTTS')
    elif sys.platform == 'win32':
        BASE_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'WordTTS')
    else:
        BASE_DIR = os.path.join(os.path.expanduser('~'), '.wordtts')
    os.makedirs(BASE_DIR, exist_ok=True)
    RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

if RESOURCE_DIR not in sys.path:
    sys.path.insert(0, RESOURCE_DIR)

WORD_PARSER_DIR = os.path.join(RESOURCE_DIR, "word_parser")
if WORD_PARSER_DIR not in sys.path:
    sys.path.insert(0, WORD_PARSER_DIR)

EDGE_TTS_DIR = os.path.join(RESOURCE_DIR, "edge_tts")
if EDGE_TTS_DIR not in sys.path:
    sys.path.insert(0, EDGE_TTS_DIR)

# ============================================================================
# 导入核心模块（复用 word_tts_app 的全部逻辑）
# ============================================================================
# word_tts_app 在 import 时会执行模块级代码（路径设置、ffmpeg 配置等），
# 但不会启动 Gradio（有 __name__ == "__main__" 守卫）。
import word_tts_app as core

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

class SessionState:
    """每个处理会话的状态。"""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_dir = session_output_dir(session_id)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.log_entries: list = []
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
        self.final_error: Optional[dict] = None  # 终止错误（供并存/重连 SSE 重放）
        self.last_stats: Optional[dict] = None  # 最新 stats 事件（供重连时重放）
        self.lifecycle_version: int = 0  # cleanup 时递增，使并发中的旧启动请求失效

# 全局会话注册表
_sessions: dict[str, SessionState] = {}
MAX_SESSIONS = 20  # 最大并发会话数，防止内存泄漏
PARSE_CACHE_VERSION = 1
SOURCE_META_FILENAME = "source_fingerprint.json"
SESSION_DIR_PREFIX = "session_"


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
    for dirname in ("audio", ".tmp"):
        shutil.rmtree(os.path.join(session_dir, dirname), ignore_errors=True)
    for filename in ("progress.json", "progress.json.tmp", "output.zip"):
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


def progress_is_reusable(progress: Optional[dict], fingerprint: dict) -> bool:
    if not progress or not progress.get("items"):
        return False
    if progress.get("source_fingerprint") != fingerprint:
        return False
    if not all("raw_item" in item for item in progress.get("items", [])):
        return False
    return all(
        item.get("status") != "done"
        or (item.get("output_path") and os.path.exists(item["output_path"]))
        for item in progress.get("items", [])
    )


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
    return all(previous.get(key) == value for key, value in requested.items())


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
        os.makedirs(os.path.join(target_dir, ".tmp"), exist_ok=True)

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


MAX_LOG_ENTRIES = 500  # 日志条目上限，防止长任务内存膨胀


def push_event(session: SessionState, event: dict):
    """向会话队列推送事件，同时更新本地状态。"""
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
        session.done = True
    elif event["type"] == "error":
        session.final_error = event
    elif event["type"] == "end":
        session.ended = True
    session.queue.put_nowait(event)


# ============================================================================
# 核心生成流程（适配 SSE 流式输出）
# ============================================================================

async def generate_audio_stream(
    session: SessionState,
    source_filename: str,
    filepath: str,
    config: dict,
):
    """
    异步生成音频，通过 session.queue 推送进度事件。
    复用 word_tts_app.py 中的所有核心函数。
    """
    # finally 中需要读取该变量；必须在任何可能抛错的 I/O 之前初始化。
    has_male_voice = False

    def log(level: str, msg: str):
        push_event(session, {
            "type": "log",
            "entry": {"time": core.now_str(), "level": level, "msg": msg},
        })

    def emit_stats(progress):
        type_counts = {}
        for item in progress.get("items", []):
            dt = item["doc_type"]
            if dt not in type_counts:
                type_counts[dt] = {"done": 0, "total": 0}
            type_counts[dt]["total"] += 1
            if item["status"] == "done":
                type_counts[dt]["done"] += 1
        failed_items = [
            {
                "id": item.get("id", ""),
                "doc_type": item.get("doc_type", ""),
                "error": str(item.get("error") or "")[:240],
            }
            for item in progress.get("items", [])
            if item.get("status") == "error"
        ][:20]
        push_event(session, {
            "type": "stats",
            "completed": progress["completed"],
            "total": progress["total_items"],
            "failed": progress["failed"],
            "failed_items": failed_items,
            "by_type": type_counts,
        })

    def emit_status(text: str):
        push_event(session, {"type": "status", "text": text})

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
                }
                for f in file_list
            ],
            "zip_available": bool(zip_path and os.path.exists(zip_path)),
        }
        # 如果带有 zip_path，说明是最终 download 事件，保存以供 SSE 重连重放
        if zip_path is not None:
            session.final_download = event
        push_event(session, event)

    try:
        session_dir = session.session_dir
        audio_dir = os.path.join(session_dir, "audio")
        tmp_dir = os.path.join(session_dir, ".tmp")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(audio_dir, exist_ok=True)
        os.makedirs(tmp_dir, exist_ok=True)

        current_fingerprint = await asyncio.to_thread(source_fingerprint, filepath)
        if session.source_fingerprint and current_fingerprint != session.source_fingerprint:
            raise RuntimeError("源文档在导入后发生变化，请重新导入文档")
        session.source_fingerprint = current_fingerprint

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
                core.save_progress(session_dir, existing)
                log("info", f"已恢复历史任务进度（{existing['completed']}/{existing['total_items']}）")
        if progress_is_reusable(existing, current_fingerprint):
            old_config = existing.get("config", {})
            config_changed = any(
                old_config.get(k) != v for k, v in config.items()
                if k != "proxy"
            ) or old_config.get("proxy", "") != config.get("proxy", "")

            if config_changed:
                reason = "配置已变更"
                log("warn", f"检测到已有进度但{reason}，重新开始处理")
                progress = None
            else:
                progress = existing
                session.progress = progress  # 保存到 session 供下载端点使用
                done = progress["completed"]
                total = progress["total_items"]
                log("info", f"检测到已有进度（{done}/{total} 已完成），继续处理...")
                emit_stats(progress)
                emit_status(f"断点续传中 — {done}/{total} 已完成")
                emit_download(progress, core.get_completed_file_list(progress))
        else:
            if existing:
                log("warn", "已有进度与当前源文档不匹配或产物缺失，将重新开始处理")
            progress = None

        # ---- 解析文档 ----
        if progress is None:
            log("info", f"开始解析文档: {source_filename}")
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
                    log("error", f"解析失败: {e}")
                    emit_status(f"解析失败: {e}")
                    push_event(session, {"type": "error", "msg": str(e)})
                    return

            if not parse_results:
                log("error", f"未识别到任何题型内容: {summary}")
                emit_status(summary or "未识别到任何题型内容")
                push_event(session, {"type": "error", "msg": summary or "未识别到任何题型内容"})
                return

            # 全量重做时清理旧产物，并保存与源文件指纹绑定的解析结果。
            await asyncio.to_thread(clear_generated_outputs, session_dir)
            await asyncio.to_thread(
                save_parse_cache, session_dir, parse_results, current_fingerprint
            )
            os.makedirs(audio_dir, exist_ok=True)
            os.makedirs(tmp_dir, exist_ok=True)

            type_names = "、".join(r["doc_type"] for r in parse_results)
            progress = core.build_progress(source_filename, filepath, parse_results, config)
            progress["source_fingerprint"] = current_fingerprint

            # 试听模式
            if config.get("preview") and progress["total_items"] > 3:
                original_total = progress["total_items"]
                progress["items"] = progress["items"][:3]
                progress["total_items"] = 3
                log("warn", f"试听模式：仅生成前 3 条（共 {original_total} 条）")

            core.save_progress(session_dir, progress)
            session.progress = progress  # 保存到 session 供下载端点使用

            count_info = f"共 {progress['total_items']} 个音频"
            log("success", f"解析完成 — {summary} | 题型：{type_names} | {count_info}")

        # ---- 为断点续传重置失败项 ----
        # 失败项会在本轮重新尝试，因此必须先从统计中移除；否则每次重试都会
        # 累加 failed，最终出现 completed + failed > total 的错误结果。
        retry_items = [item for item in progress["items"] if item.get("status") == "error"]
        for item in retry_items:
            item["status"] = "pending"
            item["error"] = None
        progress["completed"] = sum(1 for item in progress["items"] if item.get("status") == "done")
        progress["failed"] = 0
        if retry_items:
            log("info", f"将重新尝试 {len(retry_items)} 个失败项")

        # ---- 开始生成 ----
        progress["status"] = "generating"
        core.save_progress(session_dir, progress)

        total = progress["total_items"]
        rate = config.get("rate", 1.0)
        volume = config.get("volume", 1)
        pitch = config.get("pitch", 1)
        pause = config.get("pause", 0)
        proxy = config.get("proxy", "")
        fmt = config.get("format", "mp3")
        quality = config.get("quality", "128 kbps（标准）")

        log("info", f"开始生成音频（{progress['completed']}/{total}）...")
        if core._TTSMaker_AVAILABLE:
            log("info", "男声使用 TTSMaker 788 (Alfie) 生成，女声使用 edge-tts")
        else:
            log("warn", "TTSMaker 不可用，男声将使用 edge-tts (Remy) 生成")

        emit_stats(progress)
        emit_status(f"生成中 — {progress['completed']}/{total}")
        emit_download(progress, core.get_completed_file_list(progress))

        # ---- 检查是否有男声数据，决定是否需要启动 TTSMaker ----
        if core._TTSMaker_AVAILABLE:
            for item in progress["items"]:
                if item["status"] == "done":
                    continue
                raw_item = item.get("raw_item", {})
                text = raw_item.get("text", "")
                if text.strip():
                    speakers = core.parse_speakers(text)
                    if any(v == core.MALE_VOICE for v, _ in speakers):
                        has_male_voice = True
                        break

        # ---- TTSMaker 登录（仅有男声数据时才唤起浏览器）----
        if core._TTSMaker_AVAILABLE and has_male_voice:
            log("warn", "检测到男声数据，正在启动 TTSMaker 浏览器（首次需扫码登录，后续自动复用登录状态）")
            try:
                await core._ttsmaker.ensure_session(voice_key="alfie")
                log("success", "TTSMaker 登录成功，开始生成音频")
            except Exception as login_err:
                log("error", f"TTSMaker 登录失败: {login_err}，男声将回退到 edge-tts")
        elif core._TTSMaker_AVAILABLE and not has_male_voice:
            log("info", "未检测到男声数据，跳过 TTSMaker 浏览器启动")

        # ---- 逐条生成 ----
        for item in progress["items"]:
            # 检查是否被用户取消
            if session.cancelled:
                log("warn", "用户已取消生成")
                emit_status("已取消")
                return

            if item["status"] == "done":
                continue

            item_id = item["id"]
            raw_item = item["raw_item"]
            text = raw_item.get("text", "")
            if not text.strip():
                item["status"] = "error"
                item["error"] = "文本为空"
                progress["failed"] += 1
                log("warn", f"{item_id} — 文本为空，跳过")
                core.save_progress(session_dir, progress)
                emit_stats(progress)
                emit_status(f"生成中 — {progress['completed']}/{total}")
                continue

            speakers = core.parse_speakers(text)
            speaker_info = ""
            if len(speakers) > 1 or speakers[0][0] != core.FEMALE_VOICE:
                voices_used = set(v for v, _ in speakers)
                if voices_used == {core.FEMALE_VOICE, core.MALE_VOICE}:
                    speaker_info = " [混合音色]"
                elif core.MALE_VOICE in voices_used:
                    speaker_info = " [男声]"
                else:
                    speaker_info = " [女声]"

            log("progress", f"正在生成: {item_id}{speaker_info}...")
            emit_status(f"生成中 — {progress['completed']}/{total} — {item_id}")

            try:
                audio_seg = await core._synth_item(
                    text, rate, volume, pitch, pause, proxy, tmp_dir
                )
                out_path = os.path.join(audio_dir, item["filename"])
                await asyncio.to_thread(core.export_audio, audio_seg, fmt, quality, out_path)

                item["status"] = "done"
                item["output_path"] = out_path
                item["error"] = None
                progress["completed"] += 1
                log("success", f"{item_id} 完成 ({progress['completed']}/{total})")
            except Exception as e:
                item["status"] = "error"
                item["error"] = str(e)
                progress["failed"] += 1
                log("error", f"{item_id} 失败: {e}")

            core.save_progress(session_dir, progress)
            emit_stats(progress)
            emit_status(f"生成中 — {progress['completed']}/{total}")
            emit_download(progress, core.get_completed_file_list(progress))

        # ---- 清理 + 打包 ----
        shutil.rmtree(tmp_dir, ignore_errors=True)
        progress["status"] = "packaging"
        core.save_progress(session_dir, progress)
        log("info", "正在打包 ZIP...")
        emit_status("正在打包...")

        zip_path = await asyncio.to_thread(core.create_zip, session_dir, progress)
        progress["status"] = "done"
        core.save_progress(session_dir, progress)

        done = progress["completed"]
        failed = progress["failed"]
        log("success", f"全部完成！成功 {done}/{total}" + (f"，失败 {failed}" if failed > 0 else ""))

        file_list = core.get_completed_file_list(progress)
        status_text = f"完成 — 成功 {done}/{total}"
        if failed > 0:
            status_text += f"，失败 {failed}"
        emit_status(status_text)
        emit_download(progress, file_list, zip_path=zip_path)
        # 保存最终 zip_path 供 SSE 重连重放
        session.final_zip_path = zip_path if file_list else None
        push_event(session, {"type": "done", "zip_path": session.final_zip_path})

    except Exception as e:
        log("error", f"处理异常: {e}")
        push_event(session, {"type": "error", "msg": str(e)})
    finally:
        # 无论成功/失败/取消，都关闭 TTSMaker 浏览器会话，防止进程泄漏
        if core._TTSMaker_AVAILABLE and has_male_voice:
            try:
                await core._ttsmaker.close_session()
                log("info", "TTSMaker 浏览器已关闭")
            except Exception as close_err:
                log("warn", f"关闭 TTSMaker 浏览器异常: {close_err}")
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

app = FastAPI(title="Word → TTS API")
_API_TOKEN = os.environ.get("WORDTTS_API_TOKEN", "")
APP_VERSION = os.environ.get("WORDTTS_VERSION", "1.2.1")


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
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# 静态文件（如果存在 renderer 目录）
_renderer_dir = os.path.join(RESOURCE_DIR, "electron", "renderer")
if os.path.isdir(_renderer_dir):
    app.mount("/static", StaticFiles(directory=_renderer_dir), name="static")


@app.get("/api/health")
async def health():
    instance = hashlib.sha256(_API_TOKEN.encode("utf-8")).hexdigest()[:16] if _API_TOKEN else "development"
    return {"status": "ok", "app": "wordtts", "version": APP_VERSION, "instance": instance}


@app.get("/api/config")
async def get_config():
    """返回前端所需的配置选项。"""
    return {
        "formats": list(core.FORMAT_MAP.keys()),
        "qualities": list(core.QUALITY_BITRATE.keys()),
        "supported_types": list(core.PARSER_MAP.keys()),
        "type_colors": core.TYPE_COLORS,
        "ttsmaker_available": core._TTSMaker_AVAILABLE,
        "female_voice": core.FEMALE_VOICE,
        "male_voice": core.MALE_VOICE,
    }


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
    if not filepath.lower().endswith('.docx'):
        raise HTTPException(status_code=400, detail="仅支持 .docx 格式")
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
    if not file.filename or not file.filename.lower().endswith('.docx'):
        raise HTTPException(status_code=400, detail="仅支持 .docx 格式")

    # 保存到临时文件（防止路径穿越：只取文件名部分）
    safe_filename = os.path.basename(file.filename)
    if not safe_filename or safe_filename != file.filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    upload_dir = os.path.join(BASE_DIR, "edge_tts", "uploads")
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
    session.log_entries = []
    session.cancelled = False
    session.status = "starting"
    session.final_download = None
    session.final_zip_path = None
    session.final_error = None
    session.last_stats = None

    # 清空队列中残留的旧事件，防止上次生成的 end/done 事件污染新流
    while not session.queue.empty():
        try:
            session.queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    # 在后台启动生成任务（保存引用以防止并发和 orphaned task）
    session.task = asyncio.create_task(
        generate_audio_stream(session, req.source_filename, req.file_path, req.config)
    )

    return {"session_id": req.session_id, "status": "started"}


@app.get("/api/progress/{session_id}")
async def progress_sse(session_id: str):
    """SSE 端点：流式推送进度更新。"""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    async def event_stream():
        # 如果任务已完成（正常结束），重放最终状态后关闭
        if session.done:
            # 重放最终 download 事件（含文件列表）
            if session.final_download:
                yield f"data: {json.dumps(session.final_download, ensure_ascii=False)}\n\n"
            # 重放最后 stats 事件（进度条）
            if session.last_stats:
                yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"
            # 重放 done 事件（含 zip_path）
            yield f"data: {json.dumps({'type': 'done', 'zip_path': session.final_zip_path}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return

        # 如果任务已终止（错误或取消）但不是正常完成，直接发送 end
        # 防止 SSE 重连后队列已空导致永久挂在心跳循环
        if session.task and session.task.done() and not session.done:
            # 先发送已有日志
            if session.log_entries:
                yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"
            # 发送当前状态和最后 stats
            yield f"data: {json.dumps({'type': 'status', 'text': session.status}, ensure_ascii=False)}\n\n"
            if session.last_stats:
                yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"
            if session.final_download:
                yield f"data: {json.dumps(session.final_download, ensure_ascii=False)}\n\n"
            if session.final_error:
                yield f"data: {json.dumps(session.final_error, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return

        # 先发送已有日志
        if session.log_entries:
            yield f"data: {json.dumps({'type': 'log_init', 'entries': session.log_entries}, ensure_ascii=False)}\n\n"

        # 发送当前状态和最后 stats
        yield f"data: {json.dumps({'type': 'status', 'text': session.status}, ensure_ascii=False)}\n\n"
        if session.last_stats:
            yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"

        # 流式发送新事件
        while True:
            # queue 是单消费者；断线重连时旧连接可能短暂存活并取走 done/end。
            # 终态同时保存在 session 上，因此每个连接都能独立完成收尾。
            if session.done:
                if session.final_download:
                    yield f"data: {json.dumps(session.final_download, ensure_ascii=False)}\n\n"
                if session.last_stats:
                    yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'zip_path': session.final_zip_path}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'end'})}\n\n"
                return
            if session.ended or (session.task and session.task.done()):
                if session.final_error:
                    yield f"data: {json.dumps(session.final_error, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'end'})}\n\n"
                return
            try:
                event = await asyncio.wait_for(session.queue.get(), timeout=5.0)
                event_type = event.get("type")
                # 两个连接可能分别取到 done / end；任一终止事件都以 session
                # 快照为准重放完整结果，不能只把自己取到的那一半发出去。
                if event_type in {"done", "end"} and session.done:
                    if session.final_download:
                        yield f"data: {json.dumps(session.final_download, ensure_ascii=False)}\n\n"
                    if session.last_stats:
                        yield f"data: {json.dumps(session.last_stats, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'zip_path': session.final_zip_path}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'end'})}\n\n"
                    return
                if event_type == "end" and session.final_error:
                    yield f"data: {json.dumps(session.final_error, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'end'})}\n\n"
                    return
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event_type in {"done", "error", "end"}:
                    if event_type in {"done", "error"}:
                        yield f"data: {json.dumps({'type': 'end'})}\n\n"
                    return
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

    # 会话目录独占，解析后尚未生成的任务也可以安全清理。
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WordTTS local API server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORDTTS_PORT", DEFAULT_PORT)))
    parser.add_argument("--token", default=os.environ.get("WORDTTS_API_TOKEN", ""))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    _API_TOKEN = args.token
    print(f"[server] 启动 FastAPI 服务器: http://127.0.0.1:{args.port}")
    # 将 uvicorn 默认日志从 stderr 改为 stdout，
    # 避免 Electron 控制台中所有日志都显示为 [python:err]
    _log_config = copy.deepcopy(_UVICORN_DEFAULT_LOG_CONFIG)
    _log_config["handlers"]["default"]["stream"] = "ext://sys.stdout"
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", log_config=_log_config)
