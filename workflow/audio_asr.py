"""On-demand local SenseVoice installation and external-audio matching."""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from app_paths import ensure_data_dir
from .artifact_store import ArtifactStore, StagedFile
from .audio_text_matching import compare_transcript, text_profile
from .domain import DomainError, new_id
from .repositories import ConflictError, NotFoundError, RepositoryError, WorkflowRepository


# Manifest schema version. The model artifact itself is pinned separately by
# revision and SHA-256 so compatible weights can be updated without changing
# this on-disk schema.
ASR_EXTENSION_VERSION = "1"
ASR_OPENING_SECONDS = 12
ASR_CONTEXT_SECONDS = 18
ASR_CONTEXT_GAP_SECONDS = 1.6
ASR_MAX_FILES = 2000
ASR_MAX_FILE_BYTES = 512 * 1024 * 1024
ASR_MAX_BATCH_BYTES = 4 * 1024 * 1024 * 1024
ASR_MAX_CONTINUOUS_DURATION_SECONDS = 60 * 60
ASR_MODEL_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
ASR_TOKENS_MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024
ASR_SUPPORTED_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"})
ASR_MODEL_REVISION = "73eca47697f980daa3d16112404174b6b950b514"
ASR_MODEL_URLS = {
    "model.int8.onnx": f"https://modelscope.cn/models/pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue/resolve/{ASR_MODEL_REVISION}/model.int8.onnx",
    "tokens.txt": f"https://modelscope.cn/models/pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue/resolve/{ASR_MODEL_REVISION}/tokens.txt",
}
ASR_MODEL_SHA256 = {
    "model.int8.onnx": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    "tokens.txt": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
}


def _sha256_file(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


class AudioImportError(RepositoryError):
    code = "AUDIO_IMPORT_INVALID"


def normalize_recognition_text(value: Any) -> str:
    """Normalize ASR and source text without depending on punctuation output."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"<\|[^|]{1,64}\|>", "", text)
    return "".join(
        char for char in text
        if not char.isspace() and unicodedata.category(char)[0] not in {"P", "S", "Z", "C"}
    )


def _duplicate_opening(left: str, right: str) -> bool:
    left = left[:32]
    right = right[:32]
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    prefix = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        prefix += 1
    if prefix / max(1, min(len(left), len(right))) >= 0.913:
        return True
    # Similarity cannot reach the full-recognition threshold when the opening
    # lengths differ too much, so avoid the more expensive character matcher.
    if len(shorter) / len(longer) < 0.72:
        return False
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.84


def _item_candidates(text: str, items: Sequence[Mapping[str, Any]], *, full: bool = False) -> list[dict[str, Any]]:
    observed = text_profile(text)
    ranked: list[dict[str, Any]] = []
    for item in items:
        variants = _spoken_text_variants(item.get("normalized_content"))
        if not variants:
            continue
        match = max(
            (compare_transcript(text_profile(content), observed, opening=not full) for content in variants),
            key=lambda result: (result.compatible, result.score),
        )
        ranked.append({
            "item_id": str(item.get("item_id") or ""),
            "sequence": int(item.get("sequence") or 0),
            "score": round(match.score, 4),
            "compatible": match.compatible,
            "complete": match.complete,
            "match_method": match.method,
            "text": str(item.get("normalized_content") or "")[:180],
        })
    ranked.sort(key=lambda row: (not row["compatible"], -row["score"], row["sequence"], row["item_id"]))
    return ranked[:5]


def _spoken_text_variants(value: Any) -> list[str]:
    """Match what is spoken, including questions whose choices stay on paper."""
    text = re.sub(
        r"(?im)^\s*(?:[WMF]|man|woman|boy|girl|male|female|speaker\s*\d+)\s*[:：]\s*",
        "", str(value or ""),
    ).strip()
    variants = [text]
    choices = re.search(r"\s*[（(]([^()（）]+)[)）]\s*$", text)
    if choices and "/" in choices.group(1) and text[:choices.start()].rstrip().endswith(("?", "？")):
        variants.append(text[:choices.start()])
    return list(dict.fromkeys(variant for variant in variants if variant))


def _spoken_content_variants(value: Any) -> list[str]:
    return [normalize_recognition_text(text) for text in _spoken_text_variants(value)]


class AudioAsrExtension:
    """Versioned model files in user data; application updates never own them."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root or Path(ensure_data_dir()) / "extensions" / "audio-asr").expanduser().resolve()
        self.models_root = self.root / "models"
        self._lock = threading.RLock()
        self._state = "not_installed"
        self._progress = 0
        self._message = "模型尚未安装"
        self._error: str | None = None
        self._worker: threading.Thread | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        self.models_root.mkdir(parents=True, exist_ok=True)

    def _manifest(self) -> dict[str, Any] | None:
        path = self.root / "manifest.json"
        try:
            if path.is_symlink() or self.models_root.is_symlink() or not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("extension_id") != "audio-asr-sensevoice-small-int8"
                or payload.get("version") != ASR_EXTENSION_VERSION
            ):
                return None
            model_dir = (self.root / str(payload.get("model_dir") or "")).resolve(strict=True)
            if self.models_root.resolve() not in model_dir.parents:
                return None
            model_path = model_dir / "model.int8.onnx"
            tokens_path = model_dir / "tokens.txt"
            if (
                model_path.is_symlink() or tokens_path.is_symlink()
                or not model_path.is_file() or not tokens_path.is_file()
                or model_path.stat().st_size < 100 * 1024 * 1024
                or tokens_path.stat().st_size < 1024
                or int(payload.get("model_bytes") or 0) != model_path.stat().st_size
                or int(payload.get("tokens_bytes") or 0) != tokens_path.stat().st_size
                or payload.get("model_sha256") != ASR_MODEL_SHA256["model.int8.onnx"]
                or payload.get("tokens_sha256") != ASR_MODEL_SHA256["tokens.txt"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("model_sha256") or ""))
                or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("tokens_sha256") or ""))
            ):
                return None
            payload["_resolved_model_dir"] = str(model_dir)
            return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _legacy_model_installed(self) -> bool:
        """Detect the earlier FunASR-format download and ask for a compatible model."""

        try:
            payload = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
            return (
                payload.get("extension_id") == "audio-asr-sensevoice-small-int8"
                and (
                    payload.get("version") != ASR_EXTENSION_VERSION
                    or payload.get("model_sha256") != ASR_MODEL_SHA256["model.int8.onnx"]
                    or payload.get("tokens_sha256") != ASR_MODEL_SHA256["tokens.txt"]
                )
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def status(self) -> dict[str, Any]:
        manifest = self._manifest()
        with self._lock:
            if manifest:
                installed = True
                state = "installed" if self._state != "downloading" else self._state
                message = "本地语音识别已安装"
            else:
                installed = False
                state = self._state if self._state in {"downloading", "failed"} else "not_installed"
                if state != "not_installed":
                    message = self._message
                elif self._legacy_model_installed():
                    message = "已下载的模型格式与识别组件不兼容，请重新下载兼容模型"
                else:
                    message = "模型尚未安装"
            return {
                "extension_id": "audio-asr-sensevoice-small-int8",
                "version": ASR_EXTENSION_VERSION,
                "installed": installed,
                "state": state,
                "progress": int(self._progress),
                "message": message,
                "error": self._error,
                "model_bytes": int(manifest.get("model_bytes") or 0) if manifest else None,
                "storage": "用户数据目录 / extensions / audio-asr",
                "source": "ModelScope",
            }

    def model_paths(self) -> tuple[str, str]:
        manifest = self._manifest()
        if manifest is None:
            raise RepositoryError("本地语音识别扩展尚未安装", code="DEPENDENCY_NOT_READY")
        model_dir = str(manifest["_resolved_model_dir"])
        model_path = Path(model_dir) / "model.int8.onnx"
        tokens_path = Path(model_dir) / "tokens.txt"
        if (
            _sha256_file(model_path) != manifest.get("model_sha256")
            or _sha256_file(tokens_path) != manifest.get("tokens_sha256")
        ):
            raise RepositoryError("本地语音识别模型文件校验失败，请在设置中心的扩展板块重新安装", code="DEPENDENCY_NOT_READY")
        return str(model_path), str(tokens_path)

    def start_install(self) -> dict[str, Any]:
        with self._lock:
            if self._manifest() is not None:
                self._state = "installed"
                self._progress = 100
                self._message = "本地语音识别已安装"
                return self.status()
            if self._worker is not None and self._worker.is_alive():
                return self.status()
            self._state = "downloading"
            self._progress = 0
            self._message = "正在从 ModelScope 下载语音识别模型"
            self._error = None
            self._worker = threading.Thread(target=self._install, name="audio-asr-install", daemon=True)
            self._worker.start()
        return self.status()

    def _install(self) -> None:
        stage = self.root / f".install-{secrets.token_hex(8)}"
        try:
            stage.mkdir(mode=0o700)
            sizes: dict[str, int] = {}
            hashes: dict[str, str] = {}
            for name, url in ASR_MODEL_URLS.items():
                request = urllib.request.Request(url, headers={"User-Agent": "WordTTS-AudioExtension/1"})
                destination = stage / name
                digest = hashlib.sha256()
                size = 0
                maximum = ASR_MODEL_MAX_DOWNLOAD_BYTES if name.endswith(".onnx") else ASR_TOKENS_MAX_DOWNLOAD_BYTES
                with urllib.request.urlopen(request, timeout=45) as response, destination.open("xb") as target:
                    expected = int(response.headers.get("Content-Length") or 0)
                    if expected > maximum:
                        raise RuntimeError(f"下载的 {name} 超过安全大小限制")
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > maximum:
                            raise RuntimeError(f"下载的 {name} 超过安全大小限制")
                        digest.update(chunk)
                        target.write(chunk)
                        with self._lock:
                            total = size + sum(sizes.values())
                            denominator = expected + sum(sizes.values())
                            self._progress = min(94, int(total * 94 / denominator)) if denominator else min(94, self._progress + 1)
                            self._message = f"正在下载 {name}"
                    target.flush()
                    os.fsync(target.fileno())
                minimum = 100 * 1024 * 1024 if name.endswith(".onnx") else 1024
                if size < minimum:
                    raise RuntimeError(f"下载的 {name} 文件不完整")
                if expected and size != expected:
                    raise RuntimeError(f"下载的 {name} 文件长度不完整")
                sizes[name] = size
                hashes[name] = digest.hexdigest()
                expected_sha = ASR_MODEL_SHA256.get(name)
                if expected_sha and hashes[name] != expected_sha:
                    raise RuntimeError(f"下载的 {name} 与已验证模型版本不匹配")

            model_sha = hashes["model.int8.onnx"]
            tokens_sha = hashes["tokens.txt"]
            version_dir_name = f"sensevoice-small-int8-{model_sha[:16]}-{tokens_sha[:8]}"
            version_dir = self.models_root / version_dir_name
            existing_matches = False
            if version_dir.exists() and not version_dir.is_symlink():
                existing_matches = (
                    _sha256_file(version_dir / "model.int8.onnx") == model_sha
                    and _sha256_file(version_dir / "tokens.txt") == tokens_sha
                )
            if version_dir.exists() and not existing_matches:
                # Keep an unexpected/corrupt directory intact for diagnosis;
                # never let an install manifest point at files we didn't verify.
                preserved_dir = self.models_root / f".preserved-{version_dir_name}-{secrets.token_hex(4)}"
                os.replace(version_dir, preserved_dir)
            if not existing_matches:
                os.replace(stage, version_dir)
            else:
                shutil.rmtree(stage, ignore_errors=True)
            manifest = {
                "extension_id": "audio-asr-sensevoice-small-int8",
                "version": ASR_EXTENSION_VERSION,
                "model_dir": f"models/{version_dir_name}",
                "model_bytes": sizes["model.int8.onnx"],
                "model_sha256": model_sha,
                "tokens_bytes": sizes["tokens.txt"],
                "tokens_sha256": tokens_sha,
                "source": ASR_MODEL_URLS,
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            temp_manifest = self.root / f".manifest-{secrets.token_hex(8)}.tmp"
            with temp_manifest.open("x", encoding="utf-8") as target:
                json.dump(manifest, target, ensure_ascii=False, indent=2)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_manifest, self.root / "manifest.json")
            with self._lock:
                self._state = "installed"
                self._progress = 100
                self._message = "本地语音识别已安装"
                self._error = None
        except Exception as exc:
            with self._lock:
                self._state = "failed"
                self._message = "模型安装失败，可重试下载"
                self._error = str(exc)[:500]
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def create_recognizer(self):
        model_path, tokens_path = self.model_paths()
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RepositoryError("语音识别运行组件未随应用安装", code="DEPENDENCY_NOT_READY") from exc
        try:
            recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model_path,
                tokens=tokens_path,
                num_threads=max(1, min(4, os.cpu_count() or 1)),
                use_itn=True,
                debug=False,
                provider="cpu",
            )
        except Exception as exc:
            raise RepositoryError("无法加载本地语音识别模型", code="DEPENDENCY_NOT_READY") from exc
        return recognizer

    def recognize(
        self,
        paths: Sequence[str | os.PathLike[str]],
        *,
        full_indices: set[int] | None = None,
        recognizer: Any = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
        progress_label: str = "正在识别音频",
        progress_stage: str = "recognition",
    ) -> list[dict[str, str]]:
        recognizer = recognizer or self.create_recognizer()
        ffmpeg = _ffmpeg_executable()
        expanded = full_indices or set()
        output: list[dict[str, str]] = []
        for index, path in enumerate(paths):
            total = len(paths)
            if callable(progress_callback):
                progress_callback({
                    "stage": progress_stage,
                    "label": f"{progress_label} {index + 1} / {total}",
                    "completed": index,
                    "total": total,
                })
            scope = "full" if index in expanded else "opening"
            try:
                stream = recognizer.create_stream()
                if index in expanded:
                    # Decode in short windows so full fallback does not load a
                    # long recording into memory at once.
                    offset = 0
                    accepted_audio = False
                    while True:
                        waveform = _decode_audio_window(ffmpeg, path, offset, 30)
                        if waveform.size == 0:
                            break
                        stream.accept_waveform(16000, waveform)
                        accepted_audio = True
                        sample_count = int(waveform.size)
                        offset += 30
                        if sample_count < 30 * 16000:
                            break
                    if not accepted_audio:
                        raise ValueError("音频没有可识别的语音内容")
                else:
                    waveform = _decode_audio_window(ffmpeg, path, 0, ASR_OPENING_SECONDS)
                    if waveform.size == 0:
                        raise ValueError("音频没有可识别的语音内容")
                    if waveform.size < ASR_OPENING_SECONDS * 16000:
                        # A short question has already been read in full.
                        # It must not bypass missing-number/negation checks
                        # under the more permissive prefix-only policy.
                        scope = "full"
                    stream.accept_waveform(16000, waveform)
                recognizer.decode_stream(stream)
                result = stream.result
                text = getattr(result, "text", "")
                if not text and isinstance(result, Mapping):
                    text = result.get("text", "")
                output.append({"text": str(text or ""), "scope": scope, "error": ""})
            except Exception as exc:
                output.append({"text": "", "scope": scope, "error": str(exc)[:200]})
            if callable(progress_callback):
                progress_callback({
                    "stage": progress_stage,
                    "label": progress_label,
                    "completed": index + 1,
                    "total": total,
                })
        return output

    def recognize_spans(
        self,
        path: str | os.PathLike[str],
        spans: Sequence[tuple[float, float]],
        *,
        recognizer: Any = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
        progress_label: str = "正在识别中英文片段",
        progress_stage: str = "continuous_recognition",
    ) -> list[dict[str, str]]:
        """Decode with sentence context, then map timed tokens back to pauses."""
        recognizer = recognizer or self.create_recognizer()
        ffmpeg = _ffmpeg_executable()
        output = [{"text": "", "scope": "segment", "error": ""} for _ in spans]
        total = len(spans)
        groups: list[list[int]] = []
        for index, (start, end) in enumerate(spans):
            if (
                not groups
                or start - spans[groups[-1][-1]][1] > ASR_CONTEXT_GAP_SECONDS
                or end - spans[groups[-1][0]][0] > ASR_CONTEXT_SECONDS
            ):
                groups.append([index])
            else:
                groups[-1].append(index)
        for indices in groups:
            start, end = spans[indices[0]][0], spans[indices[-1]][1]
            if callable(progress_callback):
                progress_callback({
                    "stage": progress_stage,
                    "label": f"{progress_label} {indices[0] + 1} / {total}",
                    "completed": indices[0],
                    "total": total,
                })
            try:
                waveform = _decode_audio_window(ffmpeg, path, start, end - start)
                if waveform.size < 2400:
                    raise ValueError("音频片段太短，没有可识别的语音内容")
                stream = recognizer.create_stream()
                stream.accept_waveform(16000, waveform)
                recognizer.decode_stream(stream)
                result = stream.result

                def result_value(name: str, default: Any) -> Any:
                    return result.get(name, default) if isinstance(result, Mapping) else getattr(result, name, default)

                tokens = result_value("tokens", [])
                timestamps = result_value("timestamps", [])
                if len(indices) == 1:
                    output[indices[0]]["text"] = str(result_value("text", "") or "")
                elif tokens and len(tokens) == len(timestamps):
                    target = 0
                    for token, timestamp in zip(tokens, timestamps):
                        absolute = start + float(timestamp)
                        while target < len(indices) - 1 and absolute > (
                            spans[indices[target]][1] + spans[indices[target + 1]][0]
                        ) / 2:
                            target += 1
                        output[indices[target]]["text"] += str(token)
                    for index in indices:
                        output[index]["text"] = output[index]["text"].strip()
                else:
                    # Older runtimes may omit token times. Decode each span
                    # separately instead of inventing boundaries from text.
                    for index in indices:
                        span_start, span_end = spans[index]
                        single = recognizer.create_stream()
                        single.accept_waveform(
                            16000, _decode_audio_window(ffmpeg, path, span_start, span_end - span_start),
                        )
                        recognizer.decode_stream(single)
                        single_result = single.result
                        output[index]["text"] = str(
                            single_result.get("text", "") if isinstance(single_result, Mapping)
                            else getattr(single_result, "text", "")
                        )
            except Exception as exc:
                for index in indices:
                    output[index] = {"text": "", "scope": "segment", "error": str(exc)[:200]}
            if callable(progress_callback):
                progress_callback({
                    "stage": progress_stage,
                    "label": progress_label,
                    "completed": indices[-1] + 1,
                    "total": total,
                })
        return output


def _ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RepositoryError("音频转换组件不可用", code="DEPENDENCY_NOT_READY") from exc
    if not ffmpeg or not Path(ffmpeg).is_file():
        raise RepositoryError("未找到音频转换组件", code="DEPENDENCY_NOT_READY")
    return str(ffmpeg)


def _decode_audio_window(
    ffmpeg: str,
    path: str | os.PathLike[str],
    start_seconds: float,
    duration_seconds: float,
) -> np.ndarray:
    completed = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start_seconds:.3f}",
         "-i", str(path), "-t", f"{duration_seconds:.3f}", "-vn", "-ac", "1", "-ar", "16000",
         "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5 * 60,
    )
    if completed.returncode != 0:
        details = completed.stderr.decode("utf-8", errors="replace")[-200:]
        raise ValueError("音频解码失败" + (f"：{details}" if details else ""))
    samples = np.frombuffer(completed.stdout, dtype="<i2")
    return samples.astype(np.float32) / 32768.0


def _audio_duration_and_speech_spans(path: str | os.PathLike[str]) -> tuple[float, list[tuple[float, float]]]:
    """Find speech ranges using FFmpeg silence detection, retaining short pauses."""
    ffmpeg = _ffmpeg_executable()
    probe = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-i", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    probe_text = probe.stderr.decode("utf-8", errors="replace")
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe_text)
    if not duration_match:
        raise DomainError("AUDIO_IMPORT_INVALID", "无法读取整段音频的时长")
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0 or duration > ASR_MAX_CONTINUOUS_DURATION_SECONDS:
        raise DomainError("RESOURCE_EXHAUSTED", "整段音频时长必须在 1 小时以内")

    detected = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path),
        "-vn", "-af", "silencedetect=noise=-38dB:d=0.32", "-f", "null", os.devnull],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60 * 60,
    )
    if detected.returncode != 0:
        details = detected.stderr.decode("utf-8", errors="replace")[-200:]
        raise DomainError("AUDIO_IMPORT_INVALID", "无法分析整段音频" + (f"：{details}" if details else ""))
    log = detected.stderr.decode("utf-8", errors="replace")
    events = []
    for match in re.finditer(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)", log):
        events.append((float(match.group(2)), match.group(1)))
    silences: list[tuple[float, float]] = []
    silence_start: float | None = None
    for timestamp, kind in events:
        if kind == "start":
            silence_start = timestamp
        elif silence_start is not None:
            if timestamp - silence_start >= 0.30:
                silences.append((max(0.0, silence_start), min(duration, timestamp)))
            silence_start = None
    if silence_start is not None and duration - silence_start >= 0.30:
        silences.append((max(0.0, silence_start), duration))

    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for silence_start, silence_end in silences:
        if silence_start - cursor >= 0.32:
            spans.append((max(0.0, cursor - 0.10), min(duration, silence_start + 0.12)))
        cursor = max(cursor, silence_end)
    if duration - cursor >= 0.32:
        spans.append((max(0.0, cursor - 0.10), duration))
    if not spans:
        raise DomainError("AUDIO_IMPORT_INVALID", "整段音频中没有检测到可识别的语音片段")
    if len(spans) > 4000:
        raise DomainError("RESOURCE_EXHAUSTED", "整段音频停顿过多，无法安全切分")
    return duration, spans


def _is_english_transcript(value: str) -> bool:
    compact = normalize_recognition_text(value)
    latin = sum(char.isascii() and char.isalpha() for char in compact)
    letters = sum(char.isalpha() for char in compact)
    # Context decoding can assign a short word such as "I" or "One" its
    # own pause-delimited range. Keep it so the exported clip starts there.
    return latin >= 1 and latin / max(1, letters) >= 0.60


def _continuous_audio_matches(
    recognized: Sequence[Mapping[str, Any]],
    spans: Sequence[tuple[float, float]],
    items: Sequence[Mapping[str, Any]],
    *,
    progress_callback: Callable[[int, int], Any] | None = None,
) -> list[dict[str, Any]]:
    """Find complete spoken items and assign each speech range only once."""
    includes_chinese = any(
        re.search(r"[\u4e00-\u9fff]", str(item.get("normalized_content") or ""))
        for item in items
    )
    runs: list[list[dict[str, Any]]] = []
    interrupted = True
    for row_index, (row, span) in enumerate(zip(recognized, spans)):
        text = str(row.get("text") or "").strip()
        normalized = normalize_recognition_text(text)
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))
        short_mixed_name = has_chinese and len(re.findall(r"[\u4e00-\u9fff]", text)) <= 3 and any(
            0 <= neighbour < len(recognized)
            and _is_english_transcript(str(recognized[neighbour].get("text") or ""))
            and max(0, span[0] - spans[neighbour][1], spans[neighbour][0] - span[1]) <= ASR_CONTEXT_GAP_SECONDS
            for neighbour in (row_index - 1, row_index + 1)
        )
        if row.get("error") or not normalized or not (
            _is_english_transcript(text) or (includes_chinese and has_chinese) or short_mixed_name
        ):
            # Announcements divide sections, even when the pauses around
            # them are short. A spoken question number does not divide a run.
            if sum("\u4e00" <= char <= "\u9fff" for char in text) >= 4 or row.get("error"):
                interrupted = True
            continue
        segment = {"start": span[0], "end": span[1], "text": text, "normalized": normalized}
        if interrupted or not runs or span[0] - runs[-1][-1]["end"] > 3.2:
            runs.append([segment])
        else:
            runs[-1].append(segment)
        interrupted = False

    targets = [
        (item, text_profile(variant))
        for item in items
        for variant in _spoken_text_variants(item.get("normalized_content"))
    ]
    max_length = max((max(len(target.text), len(target.phonetic)) for _item, target in targets), default=0)

    occurrences: list[dict[str, Any]] = []
    total_windows = sum(len(run) for run in runs)
    completed_windows = 0
    for run in runs:
        for start_index in range(len(run)):
            text_parts: list[str] = []
            transcript_parts: list[str] = []
            for end_index in range(start_index, min(len(run), start_index + 120)):
                segment = run[end_index]
                if segment["end"] - run[start_index]["start"] > 180:
                    break
                text_parts.append(segment["normalized"])
                transcript_parts.append(segment["text"])
                normalized = "".join(text_parts)
                if len(normalized) > max_length * 1.35:
                    break
                observed = text_profile(" ".join(transcript_parts))
                if max(len(observed.text), len(observed.phonetic)) < 6:
                    continue
                ranked: dict[str, dict[str, Any]] = {}
                for item, target in targets:
                    match = compare_transcript(target, observed)
                    score = match.score
                    if not score or not match.compatible:
                        continue
                    item_id = str(item.get("item_id") or "")
                    previous = ranked.get(item_id)
                    if previous is None or score > previous["score"]:
                        ranked[item_id] = {
                            "item_id": item_id,
                            "sequence": int(item.get("sequence") or 0),
                            "score": round(score, 4),
                            "text": str(item.get("normalized_content") or "")[:180],
                            "coverage": match.coverage,
                            "complete": match.complete,
                            "match_method": match.method,
                            "target_length": len(target.text),
                        }
                candidates = sorted(ranked.values(), key=lambda entry: (-entry["score"], entry["sequence"]))
                if not candidates or not candidates[0]["complete"]:
                    continue
                top = candidates[0]
                threshold = 0.76 if top["target_length"] >= 36 else 0.80
                if top["score"] < threshold:
                    continue
                second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
                margin = top["score"] - second_score
                occurrences.append({
                    "start": run[start_index]["start"],
                    "end": segment["end"],
                    "transcript": " ".join(transcript_parts),
                    "candidates": [
                        {key: candidate[key] for key in ("item_id", "sequence", "score", "text")}
                        for candidate in candidates[:5]
                    ],
                    "top_item_id": top["item_id"],
                    "score": top["score"],
                    "adjusted_score": top["score"] * top["target_length"] ** 0.5,
                    "margin": margin,
                    "coverage": top["coverage"],
                    "match_method": top["match_method"],
                })
            completed_windows += 1
            if callable(progress_callback):
                progress_callback(completed_windows, total_windows)

    # Longer complete material owns its speech before a short question can
    # claim a similar sentence inside it. Whole-content scores distinguish a
    # complete reading from a window that spills into the next repetition.
    occurrences.sort(key=lambda row: (-row["adjusted_score"], -row["score"], -row["coverage"], row["end"] - row["start"], row["start"]))
    selected: list[dict[str, Any]] = []
    for occurrence in occurrences:
        duplicate = False
        for current in selected:
            overlap = max(0.0, min(occurrence["end"], current["end"]) - max(occurrence["start"], current["start"]))
            if overlap > 0.01:
                duplicate = True
                break
        if not duplicate:
            selected.append(occurrence)
    selected.sort(key=lambda row: (row["start"], row["end"]))
    return selected


def _stage_audio_segments(
    source_path: str | os.PathLike[str],
    spans: Sequence[tuple[float, float]],
    artifacts: ArtifactStore,
) -> StagedFile:
    """Cut one or more English spans and stage a single MP3 clip."""
    if not spans:
        raise ValueError("音频片段不能为空")
    ffmpeg = _ffmpeg_executable()
    with tempfile.TemporaryDirectory(prefix="wordtts-audio-segment-") as temporary:
        directory = Path(temporary)
        wave_paths: list[Path] = []
        for index, (start, end) in enumerate(spans):
            if end <= start:
                continue
            wave_path = directory / f"part-{index:04d}.wav"
            try:
                completed = subprocess.run(
                    [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
                     "-i", str(source_path), "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", "44100",
                     "-c:a", "pcm_s16le", str(wave_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=15 * 60,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DomainError("AUDIO_IMPORT_INVALID", "切割音频片段失败，请检查源文件") from exc
            if completed.returncode != 0 or not wave_path.is_file() or wave_path.stat().st_size < 256:
                details = completed.stderr.decode("utf-8", errors="replace")[-200:]
                raise DomainError("AUDIO_IMPORT_INVALID", "切割音频片段失败" + (f"：{details}" if details else ""))
            wave_paths.append(wave_path)
        if not wave_paths:
            raise DomainError("AUDIO_IMPORT_INVALID", "没有可保存的语音片段")
        list_path = directory / "segments.txt"
        list_path.write_text("".join(f"file '{path.as_posix()}'\n" for path in wave_paths), encoding="utf-8")
        destination = directory / "segment.mp3"
        try:
            completed = subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(list_path), "-vn", "-ac", "1", "-ar", "44100", "-b:a", "128k", "-f", "mp3",
                 "-fs", str(artifacts.max_bytes), str(destination)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30 * 60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError("AUDIO_IMPORT_INVALID", "合并音频片段失败") from exc
        output_size = destination.stat().st_size if destination.is_file() else 0
        if completed.returncode != 0 or output_size < 128 or output_size >= artifacts.max_bytes:
            raise DomainError("RESOURCE_EXHAUSTED" if output_size >= artifacts.max_bytes else "AUDIO_IMPORT_INVALID",
                              "切割后的音频超出本地存储限制" if output_size >= artifacts.max_bytes else "无法生成切割后的 MP3")
        with destination.open("rb") as source:
            return artifacts.stage_stream(source, expected_size=output_size)


def transcode_audio_to_mp3(path: str | os.PathLike[str], artifacts: ArtifactStore) -> Any:
    """Use the app's bundled FFmpeg and publish one MP3 Blob."""
    ffmpeg = _ffmpeg_executable()
    with tempfile.TemporaryDirectory(prefix="wordtts-audio-import-") as temporary:
        destination = Path(temporary) / "imported.mp3"
        try:
            completed = subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                 "-vn", "-ac", "1", "-ar", "44100", "-b:a", "128k", "-f", "mp3",
                 "-fs", str(artifacts.max_bytes), str(destination)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60 * 60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepositoryError("音频转换失败，请检查文件是否完整", code="AUDIO_IMPORT_INVALID") from exc
        output_size = destination.stat().st_size if destination.is_file() else 0
        if completed.returncode != 0 or output_size < 128 or output_size >= artifacts.max_bytes:
            details = completed.stderr.decode("utf-8", errors="replace")[-400:]
            output_too_large = output_size >= artifacts.max_bytes
            raise RepositoryError(
                ("转换后的音频超过本地存储限制" if output_too_large else "音频无法转换为 MP3")
                + (f"：{details}" if details else ""),
                code="RESOURCE_EXHAUSTED" if output_too_large else "AUDIO_IMPORT_INVALID",
            )
        with destination.open("rb") as source:
            staged = artifacts.stage_stream(source, expected_size=destination.stat().st_size)
        try:
            return artifacts.promote(staged, format="mp3")
        except Exception:
            artifacts.delete_staging(staged.key)
            raise


def _invoke_with_optional_progress(
    method: Callable[..., Any],
    *args: Any,
    progress_callback: Callable[[Mapping[str, Any]], Any],
    progress_label: str,
    progress_stage: str,
    **kwargs: Any,
) -> Any:
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_extra = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    for name, value in (
        ("progress_callback", progress_callback),
        ("progress_label", progress_label),
        ("progress_stage", progress_stage),
    ):
        if name in parameters or accepts_extra:
            kwargs[name] = value
    return method(*args, **kwargs)


@dataclass
class _ImportBatch:
    batch_id: str
    workflow_id: str
    files: list[dict[str, Any]]
    items: list[dict[str, Any]]
    mode: str = "files"
    created_at: float = field(default_factory=time.time)
    staged: dict[int, StagedFile] = field(default_factory=dict)
    analysis: dict[str, Any] | None = None
    progress: dict[str, Any] = field(default_factory=lambda: {
        "active": False,
        "stage": "ready",
        "label": "等待识别",
        "completed": 0,
        "total": 0,
    })
    analyzing: bool = False
    committing: bool = False
    committed: dict[str, Any] | None = None
    archive_expected_size: int | None = None


class AudioImportCoordinator:
    def __init__(self, repository: WorkflowRepository, artifacts: ArtifactStore) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self._lock = threading.RLock()
        self._batches: dict[str, _ImportBatch] = {}

    @staticmethod
    def _clean_filename(value: Any) -> str:
        raw = str(value or "").replace("\\", "/")
        name = PurePath(raw).name.strip()
        if len(name) > 256:
            name = name[:256]
        return name

    def _batch(self, workflow_id: str, batch_id: str) -> _ImportBatch:
        batch = self._batches.get(str(batch_id))
        if batch is None or batch.workflow_id != str(workflow_id):
            raise NotFoundError("音频导入批次不存在或已过期")
        if (time.time() - batch.created_at > 12 * 60 * 60
                and not batch.analyzing and not batch.committing):
            self._remove_batch(batch.batch_id)
            raise NotFoundError("音频导入批次已过期，请重新选择文件")
        return batch

    def analysis_progress(self, workflow_id: str, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._batch(workflow_id, batch_id)
            return {"batch_id": batch.batch_id, **batch.progress}

    def _required_items(self, workflow_id: str) -> list[dict[str, Any]]:
        snapshot = self.repository.get_workflow(workflow_id)
        if snapshot.status != "ACTIVE" or snapshot.execution_state != "PREPARING" or snapshot.result_status != "IN_PROGRESS":
            raise ConflictError("当前任务不能导入外部音频", code="STATE_CONFLICT")
        items = self.repository.list_items(workflow_id)
        if not items:
            raise ConflictError("请先完成文档解析，再导入音频", code="WORKFLOW_NOT_PARSED")
        required = [item for item in items if str(item.get("status") or "") != "SKIPPED"]
        if not required:
            raise ConflictError("当前没有需要音频的题目", code="AUDIO_IMPORT_INVALID")
        if any(str(item.get("status") or "") != "PENDING" for item in required):
            raise ConflictError("当前任务已有生成或导入结果，不能开始音频导入", code="STATE_CONFLICT")
        return required

    @staticmethod
    def _batch_items_payload(required: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"item_id": str(item["item_id"]), "sequence": int(item["sequence"]),
             "item_type": str(item.get("item_type") or ""),
             "text": str(item.get("normalized_content") or "")[:360]}
            for item in required
        ]

    def create_batch(
        self,
        workflow_id: str,
        file_specs: Sequence[Mapping[str, Any]],
        *,
        mode: str = "files",
    ) -> dict[str, Any]:
        required = self._required_items(workflow_id)
        if mode not in {"files", "continuous"}:
            raise DomainError("VALIDATION_ERROR", "不支持的音频导入方式")
        if not file_specs or len(file_specs) > ASR_MAX_FILES:
            raise DomainError("VALIDATION_ERROR", "音频文件数量不符合要求")
        if mode == "continuous" and len(file_specs) != 1:
            raise DomainError("VALIDATION_ERROR", "整段音频模式只能选择一个音频文件")
        clean_specs: list[dict[str, Any]] = []
        for spec in file_specs:
            filename = self._clean_filename(spec.get("file_name"))
            extension = Path(filename).suffix.lower()
            size_bytes = int(spec.get("size_bytes") or 0)
            if not filename or extension not in ASR_SUPPORTED_EXTENSIONS:
                raise DomainError("VALIDATION_ERROR", f"不支持的音频文件：{filename or '未命名'}")
            if size_bytes <= 0 or size_bytes > ASR_MAX_FILE_BYTES:
                raise DomainError("RESOURCE_EXHAUSTED", f"音频文件大小超出限制：{filename}")
            clean_specs.append({"file_name": filename, "extension": extension, "size_bytes": size_bytes})
        if sum(int(spec["size_bytes"]) for spec in clean_specs) > ASR_MAX_BATCH_BYTES:
            raise DomainError("RESOURCE_EXHAUSTED", "本次音频总大小不能超过 4 GB")
        batch = _ImportBatch(new_id("audioimport"), workflow_id, clean_specs, required, mode=mode)
        with self._lock:
            self._expire_old_batches()
            self._batches[batch.batch_id] = batch
        return {
            "batch_id": batch.batch_id,
            "workflow_id": workflow_id,
            "mode": mode,
            "required_item_count": len(required),
            "selected_file_count": len(clean_specs),
            "file_count_matches": len(required) == len(clean_specs),
            "files": [dict(index=index, **entry) for index, entry in enumerate(clean_specs)],
            "items": self._batch_items_payload(required),
        }

    def create_archive_batch(self, workflow_id: str, archive_name: Any, size_bytes: Any) -> dict[str, Any]:
        required = self._required_items(workflow_id)
        filename = self._clean_filename(archive_name)
        size = int(size_bytes or 0)
        if Path(filename).suffix.lower() != ".zip":
            raise DomainError("VALIDATION_ERROR", "只支持 ZIP 压缩包")
        if size <= 0 or size > ASR_MAX_FILE_BYTES:
            raise DomainError("RESOURCE_EXHAUSTED", "ZIP 文件为空或超过 512 MB")
        batch = _ImportBatch(
            new_id("audioimport"), workflow_id, [], required, archive_expected_size=size,
        )
        with self._lock:
            self._expire_old_batches()
            self._batches[batch.batch_id] = batch
        return {
            "batch_id": batch.batch_id,
            "workflow_id": workflow_id,
            "mode": "files",
            "required_item_count": len(required),
            "selected_file_count": None,
            "file_count_matches": None,
            "archive_name": filename,
            "items": self._batch_items_payload(required),
        }

    async def upload(self, workflow_id: str, batch_id: str, index: int, source: Any) -> dict[str, Any]:
        with self._lock:
            batch = self._batch(workflow_id, batch_id)
            if batch.committing or batch.analyzing:
                raise ConflictError("音频正在识别或提交，暂时不能替换文件", code="IDEMPOTENCY_IN_PROGRESS")
            if index < 0 or index >= len(batch.files):
                raise DomainError("VALIDATION_ERROR", "音频文件序号无效")
            expected = int(batch.files[index]["size_bytes"])
        staged = await self.artifacts.stage_async_stream(source, expected_size=expected)
        try:
            with self._lock:
                batch = self._batch(workflow_id, batch_id)
                if batch.committing or batch.analyzing:
                    raise ConflictError("音频正在识别或提交，暂时不能替换文件", code="IDEMPOTENCY_IN_PROGRESS")
                previous = batch.staged.get(index)
                batch.staged[index] = staged
                batch.analysis = None
        except Exception:
            self.artifacts.delete_staging(staged.key)
            raise
        if previous:
            self.artifacts.delete_staging(previous.key)
        return {"batch_id": batch_id, "file_index": index, "size_bytes": staged.size_bytes, "sha256": staged.sha256}

    def open_preview(self, workflow_id: str, batch_id: str, index: int):
        """Open one uploaded batch file for a bounded, authenticated preview stream."""
        with self._lock:
            batch = self._batch(workflow_id, batch_id)
            if batch.committing or batch.committed:
                raise ConflictError("音频正在导入，暂时不能试听", code="IDEMPOTENCY_IN_PROGRESS")
            if index < 0 or index >= len(batch.files):
                raise DomainError("VALIDATION_ERROR", "音频文件序号无效")
            staged = batch.staged.get(index)
            if staged is None:
                raise ConflictError("音频尚未上传完成，暂时不能试听", code="AUDIO_IMPORT_FILES_MISSING")
            try:
                source = staged.path.open("rb")
            except OSError as exc:
                raise NotFoundError("音频预览文件不存在或已过期") from exc
            return source, str(batch.files[index]["file_name"]), int(staged.size_bytes)

    async def upload_archive(self, workflow_id: str, batch_id: str, source: Any) -> dict[str, Any]:
        with self._lock:
            batch = self._batch(workflow_id, batch_id)
            if batch.archive_expected_size is None:
                raise ConflictError("当前导入批次不接收 ZIP 文件", code="AUDIO_IMPORT_INVALID")
            if batch.committing or batch.analyzing:
                raise ConflictError("音频正在识别或提交，暂时不能替换文件", code="IDEMPOTENCY_IN_PROGRESS")
            expected_size = batch.archive_expected_size
        archive_staged = await self.artifacts.stage_async_stream(source, expected_size=expected_size)
        staged_audio: dict[int, StagedFile] = {}
        file_specs: list[dict[str, Any]] = []
        actual_uncompressed_total = 0
        try:
            with zipfile.ZipFile(archive_staged.path, "r") as archive:
                audio_entries = []
                total_uncompressed = 0
                entries = archive.infolist()
                if len(entries) > 10000:
                    raise DomainError("RESOURCE_EXHAUSTED", "ZIP 中的目录和文件条目不能超过 10000 个")
                for entry in entries:
                    if entry.is_dir():
                        continue
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise DomainError("AUDIO_IMPORT_INVALID", "ZIP 中不能包含符号链接")
                    filename = self._clean_filename(entry.filename)
                    extension = Path(filename).suffix.lower()
                    if extension not in ASR_SUPPORTED_EXTENSIONS:
                        continue
                    if entry.flag_bits & 0x1:
                        raise DomainError("AUDIO_IMPORT_INVALID", f"ZIP 中的音频已加密：{filename}")
                    size = int(entry.file_size)
                    if size <= 0 or size > ASR_MAX_FILE_BYTES:
                        raise DomainError("RESOURCE_EXHAUSTED", f"ZIP 中的音频大小超出限制：{filename}")
                    total_uncompressed += size
                    if total_uncompressed > ASR_MAX_BATCH_BYTES:
                        raise DomainError("RESOURCE_EXHAUSTED", "ZIP 解压后的音频总大小不能超过 4 GB")
                    audio_entries.append((entry, filename, extension, size))
                    if len(audio_entries) > ASR_MAX_FILES:
                        raise DomainError("RESOURCE_EXHAUSTED", "ZIP 中的音频文件不能超过 2000 个")
                if not audio_entries:
                    raise DomainError("AUDIO_IMPORT_INVALID", "ZIP 中没有支持的音频文件")
                for index, (entry, filename, extension, size) in enumerate(audio_entries):
                    with archive.open(entry, "r") as source_file:
                        def bounded_chunks():
                            nonlocal actual_uncompressed_total
                            actual_file_size = 0
                            while chunk := source_file.read(1024 * 1024):
                                actual_file_size += len(chunk)
                                actual_uncompressed_total += len(chunk)
                                if actual_file_size > size or actual_file_size > ASR_MAX_FILE_BYTES:
                                    raise DomainError("AUDIO_IMPORT_INVALID", f"ZIP 中的音频长度与目录不一致：{filename}")
                                if actual_uncompressed_total > ASR_MAX_BATCH_BYTES:
                                    raise DomainError("RESOURCE_EXHAUSTED", "ZIP 解压后的音频总大小不能超过 4 GB")
                                yield chunk

                        staged_audio[index] = self.artifacts.stage_stream(bounded_chunks(), expected_size=size)
                    file_specs.append({
                        "file_name": filename,
                        "extension": extension,
                        "size_bytes": size,
                    })
        except DomainError:
            for staged in staged_audio.values():
                self.artifacts.delete_staging(staged.key)
            raise
        except Exception as exc:
            for staged in staged_audio.values():
                self.artifacts.delete_staging(staged.key)
            if isinstance(exc, (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError)):
                raise DomainError("AUDIO_IMPORT_INVALID", "ZIP 文件损坏、加密或无法读取") from exc
            raise
        finally:
            self.artifacts.delete_staging(archive_staged.key)

        try:
            with self._lock:
                batch = self._batch(workflow_id, batch_id)
                if batch.committing or batch.analyzing:
                    raise ConflictError("音频正在识别或提交，暂时不能替换文件", code="IDEMPOTENCY_IN_PROGRESS")
                previous_files = list(batch.staged.values())
                batch.files = file_specs
                batch.staged = staged_audio
                batch.analysis = None
                staged_audio = {}
        except Exception:
            for staged in staged_audio.values():
                self.artifacts.delete_staging(staged.key)
            raise
        for staged in previous_files:
            self.artifacts.delete_staging(staged.key)
        return {
            "batch_id": batch_id,
            "required_item_count": len(batch.items),
            "selected_file_count": len(file_specs),
            "file_count_matches": len(batch.items) == len(file_specs),
            "files": [dict(index=index, **entry) for index, entry in enumerate(file_specs)],
        }

    def analyze(self, workflow_id: str, batch_id: str, extension: AudioAsrExtension) -> dict[str, Any]:
        with self._lock:
            batch = self._batch(workflow_id, batch_id)
            if batch.analysis and batch.analysis.get("status") == "analyzed":
                return batch.analysis
            if batch.committing or batch.analyzing:
                raise ConflictError("音频正在识别或提交", code="IDEMPOTENCY_IN_PROGRESS")
            if batch.mode == "files" and len(batch.files) != len(batch.items):
                result = {
                    "batch_id": batch_id,
                    "status": "file_count_mismatch",
                    "required_item_count": len(batch.items),
                    "selected_file_count": len(batch.files),
                    "can_commit": False,
                    "files": [],
                }
                batch.analysis = result
                return result
            missing = [index for index in range(len(batch.files)) if index not in batch.staged]
            if missing:
                raise ConflictError("仍有音频文件没有上传完成", code="AUDIO_IMPORT_FILES_MISSING",
                                    details={"missing_file_indexes": missing})
            batch.analyzing = True
            batch.analysis = None
            batch.progress = {
                "active": True,
                "stage": "loading_model",
                "label": "正在准备本地语音识别模型",
                "completed": 0,
                "total": 0,
            }
            paths = [str(batch.staged[index].path) for index in range(len(batch.files))]
            items = list(batch.items)

        def report_progress(stage: str, label: str, completed: int = 0, total: int = 0) -> None:
            normalized_total = max(0, int(total or 0))
            normalized_completed = max(0, min(normalized_total, int(completed or 0)))
            update: dict[str, Any] = {
                "active": True,
                "stage": str(stage or "processing"),
                "label": str(label or "正在处理音频"),
                "completed": normalized_completed,
                "total": normalized_total,
            }
            if normalized_total:
                update["percent"] = round(normalized_completed / normalized_total * 100)
            with self._lock:
                if self._batches.get(str(batch_id)) is batch:
                    batch.progress = update

        def report_extension_progress(update: Mapping[str, Any]) -> None:
            report_progress(
                str(update.get("stage") or "recognition"),
                str(update.get("label") or "正在识别音频"),
                int(update.get("completed") or 0),
                int(update.get("total") or 0),
            )

        try:
            if batch.mode == "continuous":
                result, replacement_files, replacement_staged = self._analyze_continuous(
                    batch, paths[0], extension, items, report_progress=report_progress,
                )
                previous_staged: list[StagedFile] = []
                with self._lock:
                    current = self._batches.get(str(batch_id))
                    if current is not batch:
                        for staged in replacement_staged.values():
                            self.artifacts.delete_staging(staged.key)
                        raise NotFoundError("音频导入批次不存在或已过期")
                    if result["status"] == "analyzed":
                        previous_staged = list(batch.staged.values())
                        batch.files = replacement_files
                        batch.staged = replacement_staged
                    batch.analysis = result
                for staged in previous_staged:
                    self.artifacts.delete_staging(staged.key)
                return result
            report_progress("loading_model", "正在加载本地语音识别模型")
            recognizer = extension.create_recognizer()
            report_progress("opening_recognition", "正在识别音频开头", 0, len(paths))
            opening = _invoke_with_optional_progress(
                extension.recognize,
                paths,
                recognizer=recognizer,
                progress_callback=report_extension_progress,
                progress_label="正在识别音频开头",
                progress_stage="opening_recognition",
            )
            full_indices: set[int] = set()
            normalized = [normalize_recognition_text(row.get("text")) for row in opening]
            for left in range(len(normalized)):
                for right in range(left + 1, len(normalized)):
                    if _duplicate_opening(normalized[left], normalized[right]):
                        full_indices.update((left, right))
            recognized = list(opening)
            if full_indices:
                expanded_indices = sorted(full_indices)
                report_progress("full_review", "正在复核重复开头的完整音频", 0, len(expanded_indices))
                expanded_rows = _invoke_with_optional_progress(
                    extension.recognize,
                    [paths[index] for index in expanded_indices],
                    full_indices=set(range(len(expanded_indices))),
                    recognizer=recognizer,
                    progress_callback=report_extension_progress,
                    progress_label="正在复核长音频",
                    progress_stage="full_review",
                )
                for index, row in zip(expanded_indices, expanded_rows):
                    recognized[index] = row
            report_progress("matching", "正在将识别内容与题目匹配")
            results: list[dict[str, Any]] = []
            candidate_item_claims: dict[str, list[int]] = {}
            report_progress("matching", "正在把识别内容与题目匹配", 0, len(recognized))
            for index, row in enumerate(recognized):
                full_recognition = row.get("scope") == "full"
                candidates = _item_candidates(row.get("text", ""), items, full=full_recognition)
                top = candidates[0] if candidates else None
                alternatives = [candidate for candidate in candidates[1:] if candidate["compatible"]]
                margin = (top["score"] - alternatives[0]["score"]) if top and alternatives else (top["score"] if top else 0)
                matched = bool(
                    top and top["compatible"] and (not full_recognition or top["complete"])
                    and not row.get("error") and top["score"] >= 0.62 and margin >= 0.075
                )
                if matched:
                    candidate_item_claims.setdefault(str(top["item_id"]), []).append(index)
                results.append({
                    "file_index": index,
                    "file_name": batch.files[index]["file_name"],
                    "transcript": str(row.get("text") or "")[:600],
                    "recognition_scope": row.get("scope") or "opening",
                    "recognition_error": str(row.get("error") or "")[:200],
                    "suggested_item_id": str(top["item_id"]) if matched and top else None,
                    "match_status": "matched" if matched else ("recognition_failed" if row.get("error") else "needs_review"),
                    "candidates": candidates,
                })
                report_progress("matching", "正在把识别内容与题目匹配", index + 1, len(recognized))
            for item_id, indices in candidate_item_claims.items():
                if len(indices) > 1:
                    for index in indices:
                        results[index]["suggested_item_id"] = None
                        results[index]["match_status"] = "duplicate_candidate"
            suggestions = [row["suggested_item_id"] for row in results if row["suggested_item_id"]]
            result = {
                "batch_id": batch_id,
                "status": "analyzed",
                "required_item_count": len(items),
                "selected_file_count": len(batch.files),
                "can_commit": len(suggestions) == len(items) and len(set(suggestions)) == len(items),
                "files": results,
            }
            with self._lock:
                current = self._batches.get(str(batch_id))
                if current is not batch:
                    raise NotFoundError("音频导入批次不存在或已过期")
                batch.analysis = result
            return result
        finally:
            with self._lock:
                if self._batches.get(str(batch_id)) is batch:
                    batch.analyzing = False
                    if batch.analysis is not None:
                        batch.progress = {
                            "active": False,
                            "stage": "complete",
                            "label": "识别完成",
                            "completed": 1,
                            "total": 1,
                            "percent": 100,
                        }
                    else:
                        batch.progress = {
                            **batch.progress,
                            "active": False,
                            "stage": "failed",
                            "label": "识别失败",
                        }

    def _analyze_continuous(
        self,
        batch: _ImportBatch,
        path: str,
        extension: AudioAsrExtension,
        items: Sequence[Mapping[str, Any]],
        *,
        report_progress: Callable[[str, str, int, int], Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, StagedFile]]:
        if len(batch.files) != 1 or 0 not in batch.staged:
            raise ConflictError("整段音频尚未上传完成", code="AUDIO_IMPORT_FILES_MISSING")
        if callable(report_progress):
            report_progress("detecting_pauses", "正在分析整段录音并检测停顿", 0, 0)
        _, detected_spans = _audio_duration_and_speech_spans(path)
        spans: list[tuple[float, float]] = []
        for start, end in detected_spans:
            cursor = start
            while end - cursor > 22:
                split_end = cursor + 20
                spans.append((cursor, split_end))
                cursor = split_end - 0.15
            spans.append((cursor, end))
        if len(spans) > 4000:
            raise DomainError("RESOURCE_EXHAUSTED", "整段音频片段过多，无法安全识别")
        if callable(report_progress):
            report_progress("loading_model", "已切出候选片段，正在加载本地识别模型", 0, 0)
        recognizer = extension.create_recognizer()
        if callable(report_progress):
            report_progress("continuous_recognition", f"识别到 {len(spans)} 个候选片段", 0, len(spans))
        recognized = _invoke_with_optional_progress(
            extension.recognize_spans,
            path,
            spans,
            recognizer=recognizer,
            progress_callback=lambda update: report_progress(
                str(update.get("stage") or "continuous_recognition"),
                str(update.get("label") or "正在识别中英文片段"),
                int(update.get("completed") or 0),
                int(update.get("total") or 0),
            ) if callable(report_progress) else None,
            progress_label="正在识别中英文片段",
            progress_stage="continuous_recognition",
        )
        if callable(report_progress):
            report_progress("matching", "正在把录音内容与题目匹配", 0, 0)
        occurrences = _continuous_audio_matches(
            recognized,
            spans,
            items,
            progress_callback=(
                lambda completed, total: report_progress(
                    "matching",
                    "正在把录音内容与题目匹配",
                    completed,
                    total,
                )
            ) if callable(report_progress) else None,
        )

        grouped: dict[str, list[dict[str, Any]]] = {}
        for occurrence in occurrences:
            grouped.setdefault(str(occurrence["top_item_id"]), []).append(occurrence)
        if not grouped:
            return ({
                "batch_id": batch.batch_id,
                "mode": "continuous",
                "status": "no_matches",
                "required_item_count": len(items),
                "selected_file_count": 0,
                "matched_item_count": 0,
                "missing_item_ids": [str(item["item_id"]) for item in items],
                "can_commit": False,
                "files": [{
                    "file_index": 0,
                    "file_name": batch.files[0]["file_name"],
                    "size_bytes": int(batch.files[0]["size_bytes"]),
                    "transcript": "",
                    "recognition_scope": "source",
                    "recognition_error": "未找到可与题目对应的语音片段",
                    "suggested_item_id": None,
                    "match_status": "needs_review",
                    "candidates": [],
                }],
            }, [], {})

        source_name = Path(batch.files[0]["file_name"]).stem or "audio"
        item_by_id = {str(item["item_id"]): item for item in items}
        matched_files: list[dict[str, Any]] = []
        staged_files: dict[int, StagedFile] = {}
        ordered_groups = sorted(
            grouped.items(),
            key=lambda entry: int(item_by_id.get(entry[0], {}).get("sequence") or 0),
        )
        if callable(report_progress):
            report_progress("cutting_audio", "正在生成匹配的题目音频", 0, len(ordered_groups))
        try:
            results: list[dict[str, Any]] = []
            for item_id, group in ordered_groups:
                item = item_by_id[item_id]
                sequence = int(item.get("sequence") or 0)
                file_index = len(matched_files)
                suffix = f"-{sequence + 1:03d}.mp3"
                filename = f"{source_name[:256 - len(suffix)]}{suffix}"
                clip_spans = [(float(row["start"]), float(row["end"])) for row in group]
                staged = _stage_audio_segments(path, clip_spans, self.artifacts)
                staged_files[file_index] = staged
                spec = {
                    "file_name": filename,
                    "extension": ".mp3",
                    "size_bytes": int(staged.size_bytes),
                }
                matched_files.append(spec)
                if callable(report_progress):
                    report_progress("cutting_audio", "正在生成匹配的题目音频", file_index + 1, len(ordered_groups))
                candidate_map: dict[str, dict[str, Any]] = {}
                for occurrence in group:
                    for candidate in occurrence["candidates"]:
                        previous = candidate_map.get(str(candidate["item_id"]))
                        if previous is None or candidate["score"] > previous["score"]:
                            candidate_map[str(candidate["item_id"])] = dict(candidate)
                candidates = sorted(candidate_map.values(), key=lambda row: (-row["score"], row["sequence"]))[:5]
                confident = all(row["score"] >= 0.82 and row["margin"] >= 0.075 for row in group)
                results.append({
                    "file_index": file_index,
                    "file_name": filename,
                    "size_bytes": spec["size_bytes"],
                    "transcript": " … ".join(str(row["transcript"]) for row in group)[:600],
                    "recognition_scope": "segment",
                    "recognition_error": "",
                    "suggested_item_id": item_id if confident else None,
                    "match_status": "matched" if confident else "needs_review",
                    "candidates": candidates,
                    "segment_count": len(group),
                    "source_spans": [
                        {"start_seconds": round(start, 3), "end_seconds": round(end, 3)}
                        for start, end in clip_spans
                    ],
                })
            missing_ids = sorted(
                {str(item["item_id"]) for item in items} - {item_id for item_id, _group in ordered_groups},
                key=lambda item_id: int(item_by_id[item_id].get("sequence") or 0),
            )
            suggested = [row["suggested_item_id"] for row in results if row["suggested_item_id"]]
            can_commit = (
                not missing_ids
                and len(matched_files) == len(items)
                and len(suggested) == len(items)
                and len(set(suggested)) == len(items)
            )
            result = {
                "batch_id": batch.batch_id,
                "mode": "continuous",
                "status": "analyzed",
                "required_item_count": len(items),
                "selected_file_count": len(matched_files),
                "matched_item_count": len(grouped),
                "missing_item_ids": missing_ids,
                "can_commit": can_commit,
                "files": results,
            }
            return result, matched_files, staged_files
        except Exception:
            for staged in staged_files.values():
                self.artifacts.delete_staging(staged.key)
            raise

    def validate_bindings(self, workflow_id: str, batch_id: str, bindings: Sequence[Mapping[str, Any]]) -> tuple[_ImportBatch, list[dict[str, Any]]]:
        with self._lock:
            batch = self._batch(workflow_id, batch_id)
            if batch.committed:
                return batch, []
            if batch.committing or batch.analyzing:
                raise ConflictError("音频正在识别或提交", code="IDEMPOTENCY_IN_PROGRESS")
            if not batch.analysis or batch.analysis.get("status") != "analyzed":
                raise ConflictError("请先完成本机识别和题目匹配", code="AUDIO_IMPORT_NOT_ANALYZED")
            if len(batch.files) != len(batch.items):
                raise ConflictError("音频数量与待处理题目数量不一致，不能导入", code="AUDIO_IMPORT_FILES_MISSING",
                                    details={"required_item_count": len(batch.items), "selected_file_count": len(batch.files)})
            if set(batch.staged) != set(range(len(batch.files))):
                raise ConflictError("存在未上传的音频文件，不能导入", code="AUDIO_IMPORT_FILES_MISSING")
            if len(bindings) != len(batch.files):
                raise DomainError("VALIDATION_ERROR", "每个音频必须绑定一道题目")
            normalized: list[dict[str, Any]] = []
            seen_files: set[int] = set()
            seen_items: set[str] = set()
            required_ids = {str(item["item_id"]) for item in batch.items}
            for binding in bindings:
                file_index = int(binding.get("file_index", -1))
                item_id = str(binding.get("item_id") or "")
                if file_index < 0 or file_index >= len(batch.files) or file_index in seen_files:
                    raise DomainError("VALIDATION_ERROR", "音频绑定包含无效或重复文件")
                if item_id not in required_ids or item_id in seen_items:
                    raise DomainError("VALIDATION_ERROR", "音频绑定包含无效或重复题目")
                seen_files.add(file_index)
                seen_items.add(item_id)
                normalized.append({"file_index": file_index, "item_id": item_id})
            if seen_items != required_ids or seen_files != set(range(len(batch.files))):
                raise ConflictError("存在未绑定题目的音频，不能导入", code="AUDIO_IMPORT_FILES_MISSING")
            batch.committing = True
            return batch, normalized

    def finish_commit(self, batch: _ImportBatch, response: dict[str, Any] | None) -> None:
        with self._lock:
            batch.committing = False
            batch.committed = response
            if response is not None:
                for staged in batch.staged.values():
                    self.artifacts.delete_staging(staged.key)
                self._batches.pop(batch.batch_id, None)

    def abort(self, workflow_id: str, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._batch(workflow_id, batch_id)
            if batch.committing or batch.analyzing:
                raise ConflictError("音频正在识别或提交，暂时不能取消", code="IDEMPOTENCY_IN_PROGRESS")
            self._remove_batch(batch.batch_id)
        return {"batch_id": batch_id, "aborted": True}

    def _remove_batch(self, batch_id: str) -> None:
        batch = self._batches.pop(batch_id, None)
        if batch:
            for staged in batch.staged.values():
                self.artifacts.delete_staging(staged.key)

    def _expire_old_batches(self) -> None:
        expired = [key for key, value in self._batches.items()
                   if time.time() - value.created_at > 12 * 60 * 60 and not value.analyzing and not value.committing]
        for key in expired:
            self._remove_batch(key)
