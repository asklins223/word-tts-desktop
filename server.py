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
        self.queue: asyncio.Queue = asyncio.Queue()
        self.log_entries: list = []
        self.progress: Optional[dict] = None
        self.status: str = "idle"
        self.done: bool = False
        self.cancelled: bool = False  # 用户取消标记
        self.task: Optional[asyncio.Task] = None  # 当前生成任务引用
        self.final_download: Optional[dict] = None  # 最终 download 事件（供重连时重放）
        self.final_zip_path: Optional[str] = None  # 最终 zip 路径（供重连时重放）
        self.last_stats: Optional[dict] = None  # 最新 stats 事件（供重连时重放）

# 全局会话注册表
_sessions: dict[str, SessionState] = {}
MAX_SESSIONS = 20  # 最大并发会话数，防止内存泄漏


def get_or_create_session(session_id: str) -> SessionState:
    if session_id not in _sessions:
        # 超过上限时清理最旧的已完成会话
        if len(_sessions) >= MAX_SESSIONS:
            # 优先清理已完成的会话，其次清理最早的
            done_sessions = [sid for sid, s in _sessions.items() if s.done]
            if done_sessions:
                del _sessions[done_sessions[0]]
            else:
                # 全部在运行中，取消并删除最早插入的
                oldest = next(iter(_sessions))
                old_session = _sessions[oldest]
                old_session.cancelled = True
                # 如果有正在运行的任务，取消它
                if old_session.task and not old_session.task.done():
                    old_session.task.cancel()
                del _sessions[oldest]
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
        push_event(session, {
            "type": "stats",
            "completed": progress["completed"],
            "total": progress["total_items"],
            "failed": progress["failed"],
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
        session_dir = core.get_session_dir(source_filename)
        audio_dir = os.path.join(session_dir, "audio")
        tmp_dir = os.path.join(session_dir, ".tmp")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(audio_dir, exist_ok=True)
        os.makedirs(tmp_dir, exist_ok=True)

        # ---- 检查断点续传 ----
        existing = core.load_progress(session_dir)
        if existing and existing.get("items"):
            has_raw_item = any("raw_item" in i for i in existing.get("items", []))
            old_config = existing.get("config", {})
            config_changed = any(
                old_config.get(k) != v for k, v in config.items()
                if k != "proxy"
            ) or old_config.get("proxy", "") != config.get("proxy", "")

            if config_changed or not has_raw_item:
                reason = "配置已变更" if config_changed else "进度文件版本过旧"
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
            progress = None

        # ---- 解析文档 ----
        if progress is None:
            log("info", f"开始解析文档: {source_filename}")
            emit_status("正在解析文档...")

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

            # 保存解析结果
            parsed_path = os.path.join(session_dir, "parsed.json")
            with open(parsed_path, 'w', encoding='utf-8') as f:
                json.dump(parse_results, f, ensure_ascii=False, indent=2)

            type_names = "、".join(r["doc_type"] for r in parse_results)
            progress = core.build_progress(source_filename, filepath, parse_results, config)

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
        has_male_voice = False
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

app.add_middleware(
    CORSMiddleware,
    # 仅允许本地来源，防止恶意网页通过浏览器访问本地 API
    allow_origins=[
        "http://127.0.0.1:7863",
        "http://localhost:7863",
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
    return {"status": "ok", "version": "1.0.0"}


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
    session_id = core.sanitize_dirname(source_filename) + "_" + datetime.now().strftime("%Y%m%d%H%M%S")
    session = get_or_create_session(session_id)
    session.progress = None

    # 检查是否有已有进度
    session_dir = core.get_session_dir(source_filename)
    existing = core.load_progress(session_dir)
    existing_progress = None
    if existing and existing.get("items"):
        has_raw_item = any("raw_item" in i for i in existing.get("items", []))
        if has_raw_item:
            existing_progress = {
                "completed": existing["completed"],
                "total": existing["total_items"],
                "failed": existing["failed"],
                "status": existing["status"],
            }

    # 尝试读取已有解析结果
    parsed_path = os.path.join(session_dir, "parsed.json")
    parse_results = None
    if os.path.exists(parsed_path):
        try:
            with open(parsed_path, 'r', encoding='utf-8') as f:
                parse_results = json.load(f)
        except Exception:
            pass

    # 如果没有已有解析结果，立即解析
    if parse_results is None:
        try:
            parse_results, summary = await asyncio.to_thread(
                core.parse_document_auto, real_path
            )
            os.makedirs(session_dir, exist_ok=True)
            with open(parsed_path, 'w', encoding='utf-8') as f:
                json.dump(parse_results, f, ensure_ascii=False, indent=2)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析失败: {e}")

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
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
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
    session_id = core.sanitize_dirname(source_filename) + "_" + datetime.now().strftime("%Y%m%d%H%M%S")
    session = get_or_create_session(session_id)
    session.progress = None

    session_dir = core.get_session_dir(source_filename)
    try:
        parse_results, summary = await asyncio.to_thread(
            core.parse_document_auto, filepath
        )
        os.makedirs(session_dir, exist_ok=True)
        parsed_path = os.path.join(session_dir, "parsed.json")
        with open(parsed_path, 'w', encoding='utf-8') as f:
            json.dump(parse_results, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析失败: {e}")

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
    session = get_or_create_session(req.session_id)

    # 防止并发生成：如果已有任务在运行，先取消旧任务并等待其退出
    if session.task and not session.task.done():
        session.cancelled = True
        try:
            await asyncio.wait_for(session.task, timeout=5.0)
        except asyncio.TimeoutError:
            session.task.cancel()
            try:
                await asyncio.wait_for(session.task, timeout=2.0)
            except Exception:
                pass
        except Exception:
            pass

    session.done = False
    session.log_entries = []
    session.cancelled = False
    session.status = "starting"
    session.final_download = None
    session.final_zip_path = None
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
            try:
                event = await asyncio.wait_for(session.queue.get(), timeout=30.0)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") == "end":
                    break
            except asyncio.TimeoutError:
                # 发送心跳
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

    # 从进度中获取 session_dir
    # session_id 格式: dirname_timestamp，实际 session_dir 用 source_filename
    # 我们需要从最近的 done 事件中获取 zip_path
    # 更可靠的方式：搜索所有 session 中的 zip_path
    # 但最简单的：根据 session 的 progress 重建
    if session.progress:
        session_dir = core.get_session_dir(session.progress["source_file"])
        zip_path = os.path.join(session_dir, "output.zip")
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

    session_dir = core.get_session_dir(session.progress["source_file"])
    file_path = os.path.join(session_dir, "audio", safe_name)

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

    session_dir = core.get_session_dir(session.progress["source_file"])
    file_path = os.path.join(session_dir, "audio", safe_name)

    # 先检查音频文件
    if os.path.exists(file_path):
        return {"path": file_path}

    # 再检查 ZIP（当请求的是 output.zip 时）
    if safe_name == "output.zip":
        zip_path = os.path.join(session_dir, "output.zip")
        if os.path.exists(zip_path):
            return {"path": zip_path}

    raise HTTPException(status_code=404, detail="文件不存在")


@app.post("/api/cleanup/{session_id}")
async def cleanup_session(session_id: str):
    """清理会话数据（删除生成的音频和临时文件）。"""
    session = _sessions.get(session_id)
    if not session:
        return {"status": "ok", "message": "会话不存在，无需清理"}

    # 标记为已取消，让正在运行的生成任务在下一轮迭代时停止
    session.cancelled = True

    # 如果有正在运行的任务，等待其退出
    if session.task and not session.task.done():
        try:
            await asyncio.wait_for(session.task, timeout=5.0)
        except Exception:
            # 超时或异常，强制取消
            if session.task and not session.task.done():
                session.task.cancel()

    # 获取会话目录
    if session.progress:
        session_dir = core.get_session_dir(session.progress["source_file"])
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)

    # 从全局会话表中移除
    del _sessions[session_id]

    return {"status": "ok", "message": "已清理"}


# ============================================================================
# 启动
# ============================================================================

PORT = 7863

if __name__ == "__main__":
    print(f"[server] 启动 FastAPI 服务器: http://127.0.0.1:{PORT}")
    # 将 uvicorn 默认日志从 stderr 改为 stdout，
    # 避免 Electron 控制台中所有日志都显示为 [python:err]
    _log_config = copy.deepcopy(_UVICORN_DEFAULT_LOG_CONFIG)
    _log_config["handlers"]["default"]["stream"] = "ext://sys.stdout"
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", log_config=_log_config)
