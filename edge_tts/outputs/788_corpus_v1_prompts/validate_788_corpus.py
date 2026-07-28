#!/usr/bin/env python3
"""Validate native 788 corpus deliveries before normalization or training.

Audio is decoded only in memory for measurement.  Original files are never
modified.  The validator accepts one file per prompt ID and produces a JSON
report containing format, duration, level, clipping, silence, and checksum
information.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from array import array
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_PROMPTS = Path("datasets/788/prompts/788_corpus.tsv")
DEFAULT_META = Path("datasets/788/prompts/788_corpus.meta.json")
DEFAULT_AUDIO_DIR = Path("datasets/788/inbox")
DEFAULT_REPORT = Path("datasets/788/reports/quality_report.json")
DEFAULT_RIGHTS = Path("datasets/788/SOURCE_AND_RIGHTS.json")
DEFAULT_FROZEN_RUN = Path("datasets/788/runs/frozen_run.json")
LOCKED_CORPUS_VERSION = "1.0"
LOCKED_CORPUS_SHA256 = "52639e76f3447afe7b060f5561527110b24c4718c8f21f0a4f98a2f8cef0c9be"
LOCKED_SPLIT_COUNTS = {"train": 400, "validation": 40, "test": 40}
SUPPORTED_EXTENSIONS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}
DECODE_SAMPLE_RATE = 16000
MAX_ARCHIVED_AUDIO_BYTES = 50 * 1024 * 1024
MAX_TEST_PACKAGE_AUDIO_BYTES = 2 * 1024 * 1024 * 1024
FROZEN_ARTIFACT_FIELDS = {
    "training_manifest": (
        "training_manifest_path",
        "training_manifest_sha256",
    ),
    "model": ("model_path", "model_sha256"),
    "index": ("index_path", "index_sha256"),
    "config": ("config_path", "config_sha256"),
    "thresholds": ("thresholds_path", "thresholds_sha256"),
    "evaluator_bundle": (
        "evaluator_bundle_path",
        "evaluator_bundle_sha256",
    ),
}


class CorpusValidationError(RuntimeError):
    """Raised when the corpus or local audio tools cannot be inspected."""


@dataclass(frozen=True)
class PromptRow:
    prompt_id: str
    split: str
    category: str
    text: str


@dataclass(frozen=True)
class AudioMetrics:
    duration_seconds: float
    probe_duration_seconds: float
    active_seconds: float
    sample_rate_hz: int
    channels: int
    codec: str
    bitrate_bps: int | None
    peak_dbfs: float
    rms_dbfs: float
    clipping_ratio: float
    dc_offset_ratio: float
    leading_silence_seconds: float
    trailing_silence_seconds: float
    active_frame_ratio: float
    sha256: str
    pcm_sha256: str


def _dbfs(value: float) -> float:
    if value <= 0:
        return -120.0
    return 20.0 * math.log10(value)


def _int_or_none(value: object) -> int | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise CorpusValidationError(f"无法读取文件并计算 SHA-256：{path}") from exc
    return digest.hexdigest()


def load_prompts(path: Path) -> list[PromptRow]:
    try:
        with path.open("r", encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source, delimiter="\t"))
    except OSError as exc:
        raise CorpusValidationError(f"无法读取语料表：{path}") from exc
    required = {"id", "split", "category", "text"}
    if not rows:
        raise CorpusValidationError(f"语料表为空：{path}")
    if not required.issubset(rows[0]):
        raise CorpusValidationError(
            f"语料表缺少列：{', '.join(sorted(required - set(rows[0])))}"
        )

    prompts: list[PromptRow] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        prompt_id = str(row.get("id", "")).strip()
        split = str(row.get("split", "")).strip()
        category = str(row.get("category", "")).strip()
        text = str(row.get("text", "")).strip()
        if not prompt_id or not split or not category or not text:
            raise CorpusValidationError(f"语料表第 {line_number} 行含空字段")
        if prompt_id in seen_ids:
            raise CorpusValidationError(f"重复语料 ID：{prompt_id}")
        folded_text = text.casefold()
        if folded_text in seen_texts:
            raise CorpusValidationError(f"重复语料文本：{text}")
        if split not in {"train", "validation", "test"}:
            raise CorpusValidationError(
                f"语料 {prompt_id} 的 split 无效：{split}"
            )
        seen_ids.add(prompt_id)
        seen_texts.add(folded_text)
        prompts.append(PromptRow(prompt_id, split, category, text))
    return prompts


def validate_prompt_lock(
    prompts_path: Path,
    meta_path: Path,
    prompts: list[PromptRow],
    *,
    allow_unlocked: bool = False,
) -> dict:
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CorpusValidationError(f"无法读取语料锁定元数据：{meta_path}") from exc
    except json.JSONDecodeError as exc:
        raise CorpusValidationError(f"语料锁定元数据不是有效 JSON：{meta_path}") from exc
    if not isinstance(metadata, dict):
        raise CorpusValidationError("语料锁定元数据根节点必须是对象")

    actual_sha256 = sha256_file(prompts_path)
    actual_splits = Counter(prompt.split for prompt in prompts)
    raw_splits = metadata.get("split_counts")
    if not isinstance(raw_splits, dict):
        raise CorpusValidationError("语料锁定元数据缺少 split_counts 对象")
    try:
        metadata_splits = {
            str(key): int(value) for key, value in raw_splits.items()
        }
        metadata_count = int(metadata.get("prompt_count", -1))
    except (TypeError, ValueError) as exc:
        raise CorpusValidationError("语料锁定元数据中的数量无效") from exc
    errors: list[str] = []
    if str(metadata.get("version")) != LOCKED_CORPUS_VERSION:
        errors.append(
            f"语料版本应为 {LOCKED_CORPUS_VERSION}，"
            f"当前为 {metadata.get('version')}"
        )
    if metadata.get("sha256") != actual_sha256:
        errors.append("语料 TSV 与 meta.json 中的 SHA-256 不一致")
    if metadata_count != len(prompts):
        errors.append("语料 TSV 与 meta.json 中的句数不一致")
    if metadata_splits != dict(actual_splits):
        errors.append("语料 TSV 与 meta.json 中的 split 数量不一致")
    if not allow_unlocked:
        if actual_sha256 != LOCKED_CORPUS_SHA256:
            errors.append(
                "语料 TSV 不是已锁定的 788 v1 版本；"
                "若明确使用自定义清单，请加 --allow-unlocked-prompts"
            )
        if dict(actual_splits) != LOCKED_SPLIT_COUNTS:
            errors.append("锁定语料必须保持 train/validation/test = 400/40/40")
    if errors:
        raise CorpusValidationError("；".join(errors))
    return {
        "version": str(metadata.get("version")),
        "sha256": actual_sha256,
        "prompt_count": len(prompts),
        "split_counts": dict(actual_splits),
        "locked": not allow_unlocked,
    }


def inspect_rights_record(path: Path) -> dict:
    if not path.is_file():
        return {
            "path": str(path),
            "present": False,
            "complete": False,
            "sha256": None,
            "issues": [],
        }
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusValidationError(f"无法读取来源与授权记录：{path}") from exc
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CorpusValidationError(f"来源与授权记录不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise CorpusValidationError("来源与授权记录根节点必须是对象")

    issues: list[str] = []
    required_strings = [
        "provided_by",
        "provided_at",
        "source_provider",
        "voice_name",
        "voice_id",
        "authorization_basis",
        "signer",
        "signed_at",
    ]
    for field in required_strings:
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            issues.append(f"{field} 未填写")
    if payload.get("voice_id") != "788":
        issues.append("voice_id 必须为 788")
    if payload.get("training_authorized") is not True:
        issues.append("training_authorized 必须明确为 true")
    if payload.get("postprocessed") is not False:
        issues.append("postprocessed 必须明确为 false")
    native_audio = payload.get("native_audio")
    if not isinstance(native_audio, dict):
        issues.append("native_audio 必须是对象")
    else:
        if not str(native_audio.get("format") or "").strip():
            issues.append("native_audio.format 未填写")
        sample_rate = _int_or_none(native_audio.get("sample_rate_hz"))
        if sample_rate is None or sample_rate < 16000:
            issues.append("native_audio.sample_rate_hz 必须至少为 16000")
    settings = payload.get("synthesis_settings")
    if not isinstance(settings, dict):
        issues.append("synthesis_settings 必须是对象")
    else:
        for field in ("rate", "pitch", "volume", "style"):
            if not str(settings.get(field) or "").strip():
                issues.append(f"synthesis_settings.{field} 未填写")
    for field in ("provided_at", "signed_at"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            try:
                datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                issues.append(f"{field} 必须是 ISO-8601 日期或时间")
    return {
        "path": str(path),
        "present": True,
        "complete": not issues,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "issues": issues,
    }


def discover_audio(audio_dir: Path) -> tuple[dict[str, Path], list[str]]:
    if not audio_dir.exists():
        return {}, [f"音频目录不存在：{audio_dir}"]
    candidates: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(audio_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            candidates[path.stem].append(path)
    audio: dict[str, Path] = {}
    errors: list[str] = []
    for stem, paths in candidates.items():
        if len(paths) > 1:
            errors.append(
                f"ID {stem} 有多个音频文件："
                + ", ".join(str(path) for path in paths)
            )
        else:
            audio[stem] = paths[0]
    return audio, errors


def _tool(name: str, environment_name: str) -> str:
    configured = os.environ.get(environment_name)
    binary = configured or shutil.which(name)
    if not binary:
        raise CorpusValidationError(
            f"未找到 {name}；请先安装 FFmpeg，或设置 {environment_name}"
        )
    return str(binary)


def probe_audio(path: Path) -> dict:
    command = [
        _tool("ffprobe", "FFPROBE_BINARY"),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,sample_rate,channels,bit_rate:"
        "format=duration,bit_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CorpusValidationError(f"ffprobe 无法检查 {path.name}：{exc}") from exc
    if result.returncode != 0:
        error = result.stderr.strip() or "未知错误"
        raise CorpusValidationError(f"{path.name} 无法解码：{error}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CorpusValidationError(f"ffprobe 返回无效 JSON：{path.name}") from exc
    streams = [
        stream
        for stream in payload.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    if not streams:
        raise CorpusValidationError(f"{path.name} 不包含音频流")
    stream = streams[0]
    format_data = payload.get("format", {})
    duration = _float_or_none(format_data.get("duration"))
    if duration is None or duration <= 0:
        raise CorpusValidationError(f"{path.name} 的音频时长无效")
    return {
        "duration_seconds": duration,
        "sample_rate_hz": _int_or_none(stream.get("sample_rate")) or 0,
        "channels": _int_or_none(stream.get("channels")) or 0,
        "codec": str(stream.get("codec_name") or "unknown"),
        "bitrate_bps": (
            _int_or_none(stream.get("bit_rate"))
            or _int_or_none(format_data.get("bit_rate"))
        ),
    }


def decode_pcm(path: Path, duration_seconds: float) -> bytes:
    command = [
        _tool("ffmpeg", "FFMPEG_BINARY"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(DECODE_SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=max(30.0, duration_seconds * 4.0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CorpusValidationError(f"FFmpeg 无法解码 {path.name}：{exc}") from exc
    if result.returncode != 0 or not result.stdout:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise CorpusValidationError(
            f"{path.name} PCM 解码失败：{error or '无音频输出'}"
        )
    if len(result.stdout) % 2:
        raise CorpusValidationError(f"{path.name} 解码后 PCM 未按采样帧对齐")
    return result.stdout


def analyze_pcm(pcm: bytes, sample_rate_hz: int = DECODE_SAMPLE_RATE) -> dict:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise CorpusValidationError("PCM 不包含采样")

    scale = 32768.0
    count = len(samples)
    peak = max(abs(value) for value in samples) / scale
    square_sum = sum(float(value) * float(value) for value in samples)
    rms = math.sqrt(square_sum / count) / scale
    mean = sum(samples) / count / scale
    clipping_ratio = sum(abs(value) >= 32760 for value in samples) / count

    frame_size = max(1, round(sample_rate_hz * 0.02))
    frame_levels: list[float] = []
    for start in range(0, count, frame_size):
        frame = samples[start : start + frame_size]
        if not frame:
            continue
        frame_square_sum = sum(float(value) * float(value) for value in frame)
        frame_levels.append(math.sqrt(frame_square_sum / len(frame)) / scale)
    active_threshold = 10 ** (-45.0 / 20.0)
    active_indices = [
        index for index, level in enumerate(frame_levels) if level >= active_threshold
    ]
    if active_indices:
        first_active = active_indices[0]
        last_active = active_indices[-1]
        active_frame_count = len(active_indices)
        leading = first_active * frame_size / sample_rate_hz
        trailing = (
            len(frame_levels) - last_active - 1
        ) * frame_size / sample_rate_hz
        active_seconds = active_frame_count * frame_size / sample_rate_hz
    else:
        leading = count / sample_rate_hz
        trailing = count / sample_rate_hz
        active_seconds = 0.0
        active_frame_count = 0

    return {
        "peak_dbfs": _dbfs(peak),
        "rms_dbfs": _dbfs(rms),
        "clipping_ratio": clipping_ratio,
        "dc_offset_ratio": mean,
        "leading_silence_seconds": leading,
        "trailing_silence_seconds": trailing,
        "active_seconds": min(active_seconds, count / sample_rate_hz),
        "active_frame_ratio": active_frame_count / max(1, len(frame_levels)),
    }


def inspect_audio(path: Path) -> AudioMetrics:
    probe = probe_audio(path)
    pcm = decode_pcm(path, probe["duration_seconds"])
    analysis = analyze_pcm(pcm)
    decoded_duration = len(pcm) / (2 * DECODE_SAMPLE_RATE)
    duration_tolerance = max(0.05, decoded_duration * 0.02)
    if abs(probe["duration_seconds"] - decoded_duration) > duration_tolerance:
        raise CorpusValidationError(
            f"{path.name} 的容器时长与实际解码时长相差过大："
            f"{probe['duration_seconds']:.3f}s vs {decoded_duration:.3f}s"
        )
    return AudioMetrics(
        duration_seconds=round(decoded_duration, 4),
        probe_duration_seconds=round(probe["duration_seconds"], 4),
        active_seconds=round(analysis["active_seconds"], 4),
        sample_rate_hz=probe["sample_rate_hz"],
        channels=probe["channels"],
        codec=probe["codec"],
        bitrate_bps=probe["bitrate_bps"],
        peak_dbfs=round(analysis["peak_dbfs"], 4),
        rms_dbfs=round(analysis["rms_dbfs"], 4),
        clipping_ratio=round(analysis["clipping_ratio"], 8),
        dc_offset_ratio=round(analysis["dc_offset_ratio"], 8),
        leading_silence_seconds=round(analysis["leading_silence_seconds"], 4),
        trailing_silence_seconds=round(analysis["trailing_silence_seconds"], 4),
        active_frame_ratio=round(analysis["active_frame_ratio"], 6),
        sha256=sha256_file(path),
        pcm_sha256=hashlib.sha256(pcm).hexdigest(),
    )


def quality_messages(
    metrics: AudioMetrics,
    *,
    category: str,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if metrics.duration_seconds < 0.8:
        errors.append("时长小于 0.8 秒")
    elif metrics.duration_seconds < 1.5:
        message = "时长小于 1.5 秒"
        if category == "short_status":
            warnings.append(message + "，仅作为受控短句保留")
        else:
            errors.append(message)
    if metrics.duration_seconds > 12:
        errors.append("时长超过 12 秒")
    elif metrics.duration_seconds > 10:
        warnings.append("时长超过推荐的 10 秒")
    if metrics.sample_rate_hz < 16000:
        errors.append("原生采样率低于 16 kHz")
    if metrics.channels != 1:
        errors.append("源文件必须是单声道")
    if metrics.peak_dbfs > -1:
        warnings.append("采样峰值高于 -1 dBFS")
    if metrics.peak_dbfs < -15:
        warnings.append("整体峰值偏低")
    if metrics.rms_dbfs < -36:
        errors.append("平均电平过低或接近静音")
    elif metrics.rms_dbfs < -30:
        warnings.append("平均电平偏低")
    if metrics.rms_dbfs > -8:
        warnings.append("平均电平过高，可能存在强压缩")
    if metrics.clipping_ratio > 0.001:
        errors.append("削波采样超过 0.1%")
    elif metrics.clipping_ratio > 0.0001:
        warnings.append("检测到少量削波采样")
    if abs(metrics.dc_offset_ratio) > 0.02:
        warnings.append("DC 偏置超过 2%")
    if metrics.leading_silence_seconds > 0.8:
        errors.append("句首静音超过 0.8 秒")
    elif metrics.leading_silence_seconds > 0.3:
        warnings.append("句首静音超过 0.3 秒")
    if metrics.trailing_silence_seconds > 0.8:
        errors.append("句尾静音超过 0.8 秒")
    elif metrics.trailing_silence_seconds > 0.3:
        warnings.append("句尾静音超过 0.3 秒")
    if metrics.active_frame_ratio < 0.55:
        errors.append("有效语音帧不足 55%")
    elif metrics.active_frame_ratio < 0.70:
        warnings.append("有效语音帧比例低于 70%")
    elif metrics.active_frame_ratio > 0.985:
        warnings.append("有效语音帧超过 98.5%，请检查首尾是否被截断")
    if (
        metrics.codec in {"mp3", "aac"}
        and metrics.bitrate_bps is not None
        and metrics.bitrate_bps < 32000
    ):
        warnings.append("有损音频码率低于 32 kbps；请确认这是引擎最高原生质量")
    return errors, warnings


def verify_frozen_artifacts(
    frozen_run_path: Path,
    payload: dict,
) -> tuple[dict[str, dict], list[str]]:
    records: dict[str, dict] = {}
    errors: list[str] = []
    for label, (path_field, hash_field) in FROZEN_ARTIFACT_FIELDS.items():
        raw_path = payload.get(path_field)
        expected_sha = payload.get(hash_field)
        record = {
            "path_field": path_field,
            "hash_field": hash_field,
            "path": raw_path,
            "expected_sha256": expected_sha,
            "actual_sha256": None,
            "verified": False,
        }
        records[label] = record
        if not isinstance(raw_path, str) or not raw_path.strip():
            errors.append(f"冻结 run manifest 的 {path_field} 必须是非空路径")
            continue
        if not isinstance(expected_sha, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha
        ):
            errors.append(
                f"冻结 run manifest 的 {hash_field} 必须是 64 位小写 SHA-256"
            )
            continue
        artifact_path = Path(raw_path)
        if not artifact_path.is_absolute():
            artifact_path = frozen_run_path.parent / artifact_path
        record["path"] = str(artifact_path)
        if not artifact_path.is_file():
            errors.append(f"冻结产物不存在或不是文件：{artifact_path}")
            continue
        try:
            actual_sha = sha256_file(artifact_path)
        except CorpusValidationError as exc:
            errors.append(str(exc))
            continue
        record["actual_sha256"] = actual_sha
        if actual_sha != expected_sha:
            errors.append(
                f"冻结产物哈希不一致：{label}（{artifact_path}）"
            )
            continue
        record["verified"] = True
    return records, errors


def discover_sealed_test_members(
    package_path: Path,
    expected_test_ids: set[str],
) -> tuple[dict[str, str], list[str]]:
    members: dict[str, str] = {}
    errors: list[str] = []
    total_audio_bytes = 0
    try:
        with zipfile.ZipFile(package_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                member_path = Path(info.filename)
                if (
                    info.filename.startswith("/")
                    or ".." in member_path.parts
                ):
                    errors.append(f"封存 test 包含不安全路径：{info.filename}")
                    continue
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type == 0o120000:
                    errors.append(f"封存 test 包不得包含符号链接：{info.filename}")
                    continue
                suffix = member_path.suffix.casefold()
                if suffix not in SUPPORTED_EXTENSIONS:
                    errors.append(
                        f"封存 test 包含非音频文件：{info.filename}"
                    )
                    continue
                prompt_id = member_path.stem
                if prompt_id not in expected_test_ids:
                    errors.append(
                        f"封存 test 包含未知或非 test 音频 ID：{prompt_id}"
                    )
                    continue
                if prompt_id in members:
                    errors.append(f"封存 test 包内音频 ID 重复：{prompt_id}")
                    continue
                if info.flag_bits & 0x1:
                    errors.append(f"封存 test 音频不得加密：{info.filename}")
                    continue
                if info.file_size <= 0:
                    errors.append(f"封存 test 音频为空：{info.filename}")
                    continue
                if info.file_size > MAX_ARCHIVED_AUDIO_BYTES:
                    errors.append(
                        f"封存 test 单个音频超过 50 MiB：{info.filename}"
                    )
                    continue
                total_audio_bytes += info.file_size
                members[prompt_id] = info.filename
            missing = sorted(expected_test_ids - set(members))
            if missing:
                errors.append(
                    f"封存 test 包缺少 {len(missing)} 条锁定音频"
                )
            if total_audio_bytes > MAX_TEST_PACKAGE_AUDIO_BYTES:
                errors.append("封存 test 音频解压后总大小超过 2 GiB")
            if not errors:
                bad_member = archive.testzip()
                if bad_member is not None:
                    errors.append(
                        f"封存 test 包 CRC 校验失败：{bad_member}"
                    )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        errors.append(f"无法安全读取封存 test 包：{package_path}（{exc}）")
    return members, errors


def inspect_archived_audio(package_path: Path, member_name: str) -> AudioMetrics:
    suffix = Path(member_name).suffix.casefold()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="788-sealed-test-",
            suffix=suffix,
            delete=False,
        ) as target:
            temp_path = Path(target.name)
            with zipfile.ZipFile(package_path) as archive:
                member = archive.getinfo(member_name)
                if (
                    member.file_size <= 0
                    or member.file_size > MAX_ARCHIVED_AUDIO_BYTES
                ):
                    raise CorpusValidationError(
                        f"封存 test 音频大小无效：{member_name}"
                    )
                with archive.open(member) as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        return inspect_audio(temp_path)
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CorpusValidationError(
            f"无法读取封存 test 音频：{member_name}"
        ) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _requirement_messages(
    level: str,
    expected: Counter,
    delivered: Counter,
    active_seconds: Counter,
    missing_by_split: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if level == "partial":
        if not sum(delivered.values()):
            warnings.append("尚未发现音频文件")
        return errors, warnings

    if level in {"minimum", "recommended", "complete"}:
        if delivered["validation"] < expected["validation"]:
            errors.append(
                f"{level} 不得低于 minimum：必须提供全部 40 条 validation 音频"
            )
        if delivered["train"] < 220:
            errors.append(
                f"{level} 不得低于 minimum：至少需要 220 条 train 音频"
            )
        if active_seconds["train"] + active_seconds["validation"] < 30 * 60:
            errors.append(
                f"{level} 不得低于 minimum："
                "train+validation 有效语音至少 30 分钟"
            )
        if active_seconds["train"] < 27 * 60:
            errors.append(
                f"{level} 不得低于 minimum："
                "train 有效语音时长至少 27 分钟"
            )
        if active_seconds["validation"] < 3 * 60:
            errors.append(
                f"{level} 不得低于 minimum："
                "validation 有效语音时长至少 3 分钟"
            )

    if level == "minimum":
        if delivered["test"]:
            errors.append("minimum 阶段 test 目标音频必须继续封存，不能进入训练目录")
        return errors, warnings

    if level == "recommended":
        for split in ("train", "validation"):
            count = expected[split]
            if delivered[split] != count:
                errors.append(
                    f"recommended 要求 {split} {count} 条，"
                    f"当前为 {delivered[split]} 条"
                )
        if delivered["test"]:
            errors.append("recommended 阶段 test 目标音频必须继续封存")
        train_validation_seconds = active_seconds["train"] + active_seconds[
            "validation"
        ]
        if train_validation_seconds < 40 * 60:
            warnings.append(
                "train+validation 有效语音不足 40 分钟；"
                "请确认实际语速是否异常偏快"
            )
        return errors, warnings

    if level == "complete":
        for split, count in expected.items():
            if delivered[split] != count:
                errors.append(
                    f"complete 要求 {split} {count} 条，"
                    f"当前为 {delivered[split]} 条"
                )
        train_validation_seconds = (
            active_seconds["train"] + active_seconds["validation"]
        )
        total_seconds = sum(active_seconds.values())
        if total_seconds < 35 * 60:
            errors.append("complete 要求全部 split 有效语音合计至少 35 分钟")
        elif total_seconds < 40 * 60:
            warnings.append("总有效语音不足 40 分钟；请确认实际语速是否异常偏快")
        for split, missing in missing_by_split.items():
            if missing:
                errors.append(f"{split} 缺少 {len(missing)} 条锁定语料")
        return errors, warnings

    raise CorpusValidationError(f"未知验收级别：{level}")


def validate_corpus(
    prompts_path: Path,
    audio_dir: Path,
    *,
    meta_path: Path = DEFAULT_META,
    rights_path: Path = DEFAULT_RIGHTS,
    frozen_run_path: Path = DEFAULT_FROZEN_RUN,
    sealed_test_package_path: Path | None = None,
    reveal_test: bool = False,
    level: str = "partial",
    allow_unlocked_prompts: bool = False,
) -> dict:
    prompts = load_prompts(prompts_path)
    corpus_lock = validate_prompt_lock(
        prompts_path,
        meta_path,
        prompts,
        allow_unlocked=allow_unlocked_prompts,
    )
    prompt_by_id = {prompt.prompt_id: prompt for prompt in prompts}
    audio_by_id, discovery_errors = discover_audio(audio_dir)

    global_errors = list(discovery_errors)
    global_warnings: list[str] = []
    rights_record = inspect_rights_record(rights_path)
    if not rights_record["present"]:
        message = (
            f"尚未提供来源与授权记录：{rights_path}；"
            "请复制 SOURCE_AND_RIGHTS_TEMPLATE.json 后填写"
        )
        if level == "partial":
            global_warnings.append(message)
        else:
            global_errors.append(message)
    elif not rights_record["complete"]:
        message = (
            "来源与授权记录尚未填写完整："
            + ", ".join(rights_record["issues"])
        )
        if level == "partial":
            global_warnings.append(message)
        else:
            global_errors.append(message)
    global_warnings.append(
        "当前只完成容器与信号预检；进入训练前仍须执行文本/ASR 对齐、"
        "788 身份一致性、噪声/混响和人工抽听 QC"
    )
    expected_test_ids = {
        prompt.prompt_id for prompt in prompts if prompt.split == "test"
    }
    inbox_test_ids = sorted(expected_test_ids & set(audio_by_id))
    if inbox_test_ids:
        message = (
            f"训练目录中发现 {len(inbox_test_ids)} 条 test 音频；"
            "test 只能从冻结哈希对应的封存 ZIP 揭封，且不会扫描这些 inbox 文件"
        )
        if level == "partial":
            global_warnings.append(message)
        else:
            global_errors.append(message)

    frozen_run_present = frozen_run_path.is_file()
    frozen_run_record = {
        "path": str(frozen_run_path),
        "present": frozen_run_present,
        "sha256": None,
        "artifacts": {},
        "validated": False,
    }
    if frozen_run_present:
        try:
            frozen_run_record["sha256"] = sha256_file(frozen_run_path)
        except CorpusValidationError as exc:
            global_errors.append(str(exc))

    sealed_test_record = {
        "path": (
            str(sealed_test_package_path)
            if sealed_test_package_path is not None
            else None
        ),
        "reveal_requested": reveal_test,
        "expected_sha256": None,
        "actual_sha256": None,
        "member_count": 0,
        "scan_authorized": False,
        "source_policy": "sealed_zip_only",
    }
    test_package_members: dict[str, str] = {}
    frozen_payload: dict | None = None
    if level == "complete":
        test_gate_errors: list[str] = []
        if not reveal_test:
            test_gate_errors.append(
                "complete 阶段必须显式使用 --reveal-test；"
                "本次不会读取或解码任何 test 音频"
            )
        if not rights_record["complete"]:
            test_gate_errors.append(
                "来源与授权记录未通过，禁止揭封 test"
            )
        if inbox_test_ids:
            test_gate_errors.append(
                "请先移除训练目录中的 te_*；complete 只接受封存 ZIP 作为 test 来源"
            )
        manifest_error_start = len(test_gate_errors)
        if not frozen_run_present:
            test_gate_errors.append(
                "test 揭封前必须提供冻结的 run manifest："
                f"{frozen_run_path}"
            )
        else:
            try:
                loaded_payload = json.loads(
                    frozen_run_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                test_gate_errors.append(
                    f"无法读取冻结 run manifest：{frozen_run_path}（{exc}）"
                )
            else:
                if not isinstance(loaded_payload, dict):
                    test_gate_errors.append(
                        "冻结 run manifest 根节点必须是对象"
                    )
                else:
                    frozen_payload = loaded_payload

        if frozen_payload is not None:
            if (
                frozen_payload.get("corpus_sha256")
                != corpus_lock["sha256"]
            ):
                test_gate_errors.append(
                    "冻结 run manifest 的 corpus_sha256 与锁定语料不一致"
                )
            frozen_at = frozen_payload.get("frozen_at")
            if not isinstance(frozen_at, str) or not frozen_at.strip():
                test_gate_errors.append(
                    "冻结 run manifest 的 frozen_at 必须是 ISO-8601 时间"
                )
            else:
                try:
                    datetime.fromisoformat(
                        frozen_at.strip().replace("Z", "+00:00")
                    )
                except ValueError:
                    test_gate_errors.append(
                        "冻结 run manifest 的 frozen_at 必须是 ISO-8601 时间"
                    )
            expected_package_sha = frozen_payload.get(
                "sealed_test_package_sha256"
            )
            sealed_test_record["expected_sha256"] = expected_package_sha
            if not isinstance(expected_package_sha, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_package_sha
            ):
                test_gate_errors.append(
                    "冻结 run manifest 的 sealed_test_package_sha256 "
                    "必须是 64 位小写 SHA-256"
                )
            artifact_records, artifact_errors = verify_frozen_artifacts(
                frozen_run_path,
                frozen_payload,
            )
            frozen_run_record["artifacts"] = artifact_records
            test_gate_errors.extend(artifact_errors)
        frozen_run_record["validated"] = (
            frozen_payload is not None
            and len(test_gate_errors) == manifest_error_start
        )

        if sealed_test_package_path is None:
            test_gate_errors.append(
                "complete 阶段必须用 --sealed-test-package 提供揭封的原始 test 包"
            )
        elif not sealed_test_package_path.is_file():
            test_gate_errors.append(
                f"封存 test 包不存在：{sealed_test_package_path}"
            )
        elif reveal_test:
            try:
                actual_package_sha = sha256_file(sealed_test_package_path)
            except CorpusValidationError as exc:
                test_gate_errors.append(str(exc))
            else:
                sealed_test_record["actual_sha256"] = actual_package_sha
                if (
                    frozen_payload is None
                    or frozen_payload.get("sealed_test_package_sha256")
                    != actual_package_sha
                ):
                    test_gate_errors.append(
                        "揭封 test 包的 SHA-256 与冻结记录不一致"
                    )

        if reveal_test and not test_gate_errors:
            test_package_members, package_errors = (
                discover_sealed_test_members(
                    sealed_test_package_path,
                    expected_test_ids,
                )
            )
            test_gate_errors.extend(package_errors)
            sealed_test_record["member_count"] = len(test_package_members)
        sealed_test_record["scan_authorized"] = (
            reveal_test and not test_gate_errors
        )
        global_errors.extend(test_gate_errors)
    unknown_ids = sorted(set(audio_by_id) - set(prompt_by_id))
    if unknown_ids:
        message = f"发现 {len(unknown_ids)} 个不在锁定语料表中的音频 ID"
        if level == "partial":
            global_warnings.append(message)
        else:
            global_errors.append(message)

    entries: list[dict] = []
    delivered = Counter()
    active_seconds: Counter = Counter()
    hashes: dict[str, list[str]] = defaultdict(list)
    pcm_hashes: dict[str, list[str]] = defaultdict(list)
    for prompt in prompts:
        archived_member: str | None = None
        path: Path | None = None
        if prompt.split == "test":
            if not sealed_test_record["scan_authorized"]:
                continue
            archived_member = test_package_members.get(prompt.prompt_id)
            if archived_member is None:
                continue
            audio_path_label = (
                f"{sealed_test_package_path}!/{archived_member}"
            )
        else:
            path = audio_by_id.get(prompt.prompt_id)
            if path is None:
                continue
            audio_path_label = str(path)
        entry = {
            "id": prompt.prompt_id,
            "split": prompt.split,
            "category": prompt.category,
            "text": prompt.text,
            "audio_path": audio_path_label,
            "errors": [],
            "warnings": [],
        }
        try:
            if archived_member is not None:
                metrics = inspect_archived_audio(
                    sealed_test_package_path,
                    archived_member,
                )
            else:
                metrics = inspect_audio(path)
            errors, warnings = quality_messages(
                metrics,
                category=prompt.category,
            )
            entry["metrics"] = asdict(metrics)
            entry["errors"].extend(errors)
            entry["warnings"].extend(warnings)
            delivered[prompt.split] += 1
            active_seconds[prompt.split] += metrics.active_seconds
            hashes[metrics.sha256].append(prompt.prompt_id)
            pcm_hashes[metrics.pcm_sha256].append(prompt.prompt_id)
        except CorpusValidationError as exc:
            entry["errors"].append(str(exc))
        entries.append(entry)

    if sealed_test_record["scan_authorized"]:
        try:
            package_sha_after_scan = sha256_file(sealed_test_package_path)
        except CorpusValidationError as exc:
            global_errors.append(str(exc))
        else:
            if package_sha_after_scan != sealed_test_record["actual_sha256"]:
                global_errors.append(
                    "封存 test 包在扫描期间发生变化，结果已作废"
                )

    for digest, prompt_ids in hashes.items():
        if len(prompt_ids) > 1:
            global_errors.append(
                "多个 ID 使用完全相同的音频："
                + ", ".join(prompt_ids)
                + f"（SHA-256 {digest[:12]}…）"
            )
    for digest, prompt_ids in pcm_hashes.items():
        if len(prompt_ids) > 1:
            global_errors.append(
                "多个 ID 解码后得到完全相同的 PCM："
                + ", ".join(prompt_ids)
                + f"（PCM SHA-256 {digest[:12]}…）"
            )

    expected = Counter(corpus_lock["split_counts"])
    available_ids = {
        prompt_id
        for prompt_id in audio_by_id
        if prompt_id not in expected_test_ids
    }
    if sealed_test_record["scan_authorized"]:
        available_ids.update(test_package_members)
    missing_by_split = {
        split: [
            prompt.prompt_id
            for prompt in prompts
            if prompt.split == split and prompt.prompt_id not in available_ids
        ]
        for split in ("train", "validation", "test")
    }
    requirement_errors, requirement_warnings = _requirement_messages(
        level,
        expected,
        delivered,
        active_seconds,
        missing_by_split,
    )
    global_errors.extend(requirement_errors)
    global_warnings.extend(requirement_warnings)

    entry_error_count = sum(bool(entry["errors"]) for entry in entries)
    entry_warning_count = sum(bool(entry["warnings"]) for entry in entries)
    report = {
        "schema_version": 1,
        "scope": "signal_preflight_only",
        "level": level,
        "prompts_path": str(prompts_path),
        "meta_path": str(meta_path),
        "audio_dir": str(audio_dir),
        "corpus_lock": corpus_lock,
        "rights_record": rights_record,
        "frozen_run_record": frozen_run_record,
        "sealed_test_package": sealed_test_record,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "summary": {
            "expected_count": dict(expected),
            "delivered_count": {
                split: delivered[split]
                for split in ("train", "validation", "test")
            },
            "missing_count": {
                split: len(missing_by_split[split])
                for split in ("train", "validation", "test")
            },
            "active_minutes": {
                split: round(active_seconds[split] / 60.0, 3)
                for split in ("train", "validation", "test")
            },
            "unknown_audio_ids": unknown_ids,
            "files_with_errors": entry_error_count,
            "files_with_warnings": entry_warning_count,
            "global_error_count": len(global_errors),
            "global_warning_count": len(global_warnings),
        },
        "global_errors": global_errors,
        "global_warnings": global_warnings,
        "missing_ids": missing_by_split,
        "entries": entries,
    }
    report["signal_preflight_passed"] = (
        not global_errors
        and not entry_error_count
        and sum(delivered.values()) > 0
    )
    report["training_admission"] = {
        "passed": False,
        "pending_checks": [
            "transcript_or_forced_alignment",
            "target_788_speaker_consistency",
            "snr_reverb_background_audio",
            "human_listening_review",
        ],
        "note": (
            "Signal preflight passing does not by itself admit audio to training."
        ),
    }
    # `passed` is intentionally reserved for full training admission.  This
    # prevents automation from treating a correctly named sine wave as 788.
    report["passed"] = report["training_admission"]["passed"]
    return report


def _print_messages(label: str, messages: Iterable[str], limit: int = 12) -> None:
    messages = list(messages)
    for message in messages[:limit]:
        print(f"  {label} {message}")
    if len(messages) > limit:
        print(f"  {label} …另有 {len(messages) - limit} 项，详见 JSON 报告")


def print_summary(report: dict) -> None:
    summary = report["summary"]
    print("788 语料质检")
    print(f"  验收级别：{report['level']}")
    print(
        "  已交付："
        + ", ".join(
            f"{split} {summary['delivered_count'][split]}/"
            f"{summary['expected_count'][split]}"
            for split in ("train", "validation", "test")
        )
    )
    print(
        "  有效时长："
        + ", ".join(
            f"{split} {summary['active_minutes'][split]:.2f} 分钟"
            for split in ("train", "validation", "test")
        )
    )
    print(
        f"  文件问题：错误 {summary['files_with_errors']}，"
        f"警告 {summary['files_with_warnings']}"
    )
    _print_messages("[错误]", report["global_errors"])
    _print_messages("[警告]", report["global_warnings"])
    print(
        "  信号预检："
        f"{'通过' if report['signal_preflight_passed'] else '未通过'}"
    )
    print("  训练准入：待文本、身份、噪声/混响与人工复听 QC")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 788 训练语料完整性与音频质量")
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--rights", type=Path, default=DEFAULT_RIGHTS)
    parser.add_argument(
        "--frozen-run",
        type=Path,
        default=DEFAULT_FROZEN_RUN,
        help="complete 阶段用于证明 test 揭封前参数已冻结的 run manifest",
    )
    parser.add_argument(
        "--sealed-test-package",
        type=Path,
        help="complete 阶段揭封的原始 test ZIP；SHA 必须匹配冻结记录",
    )
    parser.add_argument(
        "--reveal-test",
        action="store_true",
        help="模型、阈值和评估器全部冻结后，显式允许扫描 test",
    )
    parser.add_argument(
        "--allow-unlocked-prompts",
        action="store_true",
        help="明确允许自定义语料表；默认只接受锁定的 788 v1 SHA-256",
    )
    parser.add_argument(
        "--level",
        choices=["partial", "minimum", "recommended", "complete"],
        default="partial",
        help=(
            "partial=随到随检；minimum=30 分钟首版；"
            "recommended=完整训练包；complete=盲测揭封后的全部语料"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        report = validate_corpus(
            args.prompts,
            args.audio_dir,
            meta_path=args.meta,
            rights_path=args.rights,
            frozen_run_path=args.frozen_run,
            sealed_test_package_path=args.sealed_test_package,
            reveal_test=args.reveal_test,
            level=args.level,
            allow_unlocked_prompts=args.allow_unlocked_prompts,
        )
    except CorpusValidationError as exc:
        raise SystemExit(f"质检失败：{exc}") from exc
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary_report = args.report.with_name(
            f".{args.report.name}.{os.getpid()}.tmp"
        )
        temporary_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_report, args.report)
    except OSError as exc:
        raise SystemExit(f"无法原子写入质检报告：{args.report}：{exc}") from exc
    finally:
        if "temporary_report" in locals() and temporary_report.exists():
            try:
                temporary_report.unlink()
            except OSError:
                pass
    print_summary(report)
    print(f"  报告：{args.report}")
    if report["training_admission"]["passed"]:
        raise SystemExit(0)
    if report["signal_preflight_passed"]:
        # Distinct non-zero status: signal is healthy, but identity/text and
        # human QC have not admitted the files to training.
        raise SystemExit(2)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
