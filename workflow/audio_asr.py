"""On-demand local SenseVoice installation and external-audio matching."""

from __future__ import annotations

import difflib
import hashlib
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
from typing import Any, Mapping, Sequence

import numpy as np

from app_paths import ensure_data_dir
from .artifact_store import ArtifactStore, StagedFile
from .domain import DomainError, new_id
from .repositories import ConflictError, NotFoundError, RepositoryError, WorkflowRepository


# Manifest schema version. The model artifact itself is pinned separately by
# revision and SHA-256 so compatible weights can be updated without changing
# this on-disk schema.
ASR_EXTENSION_VERSION = "1"
ASR_OPENING_SECONDS = 12
ASR_MAX_FILES = 2000
ASR_MAX_FILE_BYTES = 512 * 1024 * 1024
ASR_MAX_BATCH_BYTES = 4 * 1024 * 1024 * 1024
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


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    best = 0.0
    # ASR commonly returns only part of the first sentence. Compare the result
    # with the beginning of the question at nearby lengths, not its full body.
    for length in {
        max(1, len(shorter) - 8), max(1, len(shorter) - 3), len(shorter),
        min(len(longer), len(shorter) + 4), min(len(longer), len(shorter) + 12),
    }:
        candidate = longer[:length]
        ratio = difflib.SequenceMatcher(None, shorter, candidate, autojunk=False).ratio()
        best = max(best, ratio)
    prefix = 0
    for a, b in zip(left, right):
        if a != b:
            break
        prefix += 1
    prefix_score = prefix / max(1, min(len(left), len(right)))
    return max(best, prefix_score * 0.92)


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


def _item_candidates(text: str, items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_recognition_text(text)
    ranked: list[dict[str, Any]] = []
    for item in items:
        content = normalize_recognition_text(item.get("normalized_content"))
        if not content:
            continue
        score = _similarity(normalized, content)
        ranked.append({
            "item_id": str(item.get("item_id") or ""),
            "sequence": int(item.get("sequence") or 0),
            "score": round(score, 4),
            "text": str(item.get("normalized_content") or "")[:180],
        })
    ranked.sort(key=lambda row: (-row["score"], row["sequence"], row["item_id"]))
    return ranked[:5]


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
    ) -> list[dict[str, str]]:
        recognizer = recognizer or self.create_recognizer()
        ffmpeg = _ffmpeg_executable()
        expanded = full_indices or set()
        output: list[dict[str, str]] = []
        for index, path in enumerate(paths):
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
                    stream.accept_waveform(16000, waveform)
                recognizer.decode_stream(stream)
                result = stream.result
                text = getattr(result, "text", "")
                if not text and isinstance(result, Mapping):
                    text = result.get("text", "")
                output.append({"text": str(text or ""), "scope": scope, "error": ""})
            except Exception as exc:
                output.append({"text": "", "scope": scope, "error": str(exc)[:200]})
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
    start_seconds: int,
    duration_seconds: int,
) -> np.ndarray:
    completed = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(start_seconds),
         "-i", str(path), "-t", str(duration_seconds), "-vn", "-ac", "1", "-ar", "16000",
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


@dataclass
class _ImportBatch:
    batch_id: str
    workflow_id: str
    files: list[dict[str, Any]]
    items: list[dict[str, Any]]
    created_at: float = field(default_factory=time.time)
    staged: dict[int, StagedFile] = field(default_factory=dict)
    analysis: dict[str, Any] | None = None
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

    def create_batch(self, workflow_id: str, file_specs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        required = self._required_items(workflow_id)
        if not file_specs or len(file_specs) > ASR_MAX_FILES:
            raise DomainError("VALIDATION_ERROR", "音频文件数量不符合要求")
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
        batch = _ImportBatch(new_id("audioimport"), workflow_id, clean_specs, required)
        with self._lock:
            self._expire_old_batches()
            self._batches[batch.batch_id] = batch
        return {
            "batch_id": batch.batch_id,
            "workflow_id": workflow_id,
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
            if len(batch.files) != len(batch.items):
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
            paths = [str(batch.staged[index].path) for index in range(len(batch.files))]
            items = list(batch.items)
        try:
            recognizer = extension.create_recognizer()
            opening = extension.recognize(paths, recognizer=recognizer)
            full_indices: set[int] = set()
            normalized = [normalize_recognition_text(row.get("text")) for row in opening]
            for left in range(len(normalized)):
                for right in range(left + 1, len(normalized)):
                    if _duplicate_opening(normalized[left], normalized[right]):
                        full_indices.update((left, right))
            recognized = list(opening)
            if full_indices:
                expanded_indices = sorted(full_indices)
                expanded_rows = extension.recognize(
                    [paths[index] for index in expanded_indices],
                    full_indices=set(range(len(expanded_indices))),
                    recognizer=recognizer,
                )
                for index, row in zip(expanded_indices, expanded_rows):
                    recognized[index] = row
            results: list[dict[str, Any]] = []
            candidate_item_claims: dict[str, list[int]] = {}
            for index, row in enumerate(recognized):
                candidates = _item_candidates(row.get("text", ""), items)
                top = candidates[0] if candidates else None
                margin = (top["score"] - candidates[1]["score"]) if top and len(candidates) > 1 else (top["score"] if top else 0)
                matched = bool(top and not row.get("error") and top["score"] >= 0.62 and margin >= 0.075)
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
