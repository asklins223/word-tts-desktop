"""Low-latency 788/Alfie colour matching for Edge Remy audio.

This module deliberately implements a causal, streamable DSP preset.  It can
make Remy's long-term spectrum and pitch colour closer to the bundled 788
reference, but it does not replace a trained speaker-conversion model.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import os
import shutil
import subprocess
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

from pydub import AudioSegment


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE_PATH = BASE_DIR / "voice_profiles" / "788_remy.json"


class VoiceMatchError(RuntimeError):
    """Raised when the real-time matching pipeline cannot process audio."""


@dataclass(frozen=True)
class VoiceMatchProfile:
    name: str
    source_voice: str
    target_reference: str
    reference_transcript: str
    sample_rate_hz: int
    post_pitch_ratio: float
    frequencies_hz: tuple[float, ...]
    gains_db: tuple[float, ...]


def _bounded_strength(value: float | int) -> float:
    return min(100.0, max(0.0, float(value))) / 100.0


@lru_cache(maxsize=4)
def load_profile(path: str | os.PathLike[str] = DEFAULT_PROFILE_PATH) -> VoiceMatchProfile:
    """Load and validate a calibrated voice-match profile."""
    profile_path = Path(path)
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("profile root must be an object")
        version = int(data.get("version", 0))
        frequencies = tuple(float(value) for value in data.get("frequencies_hz", ()))
        gains = tuple(float(value) for value in data.get("gains_db", ()))
        sample_rate = int(data.get("sample_rate_hz", 0))
        pitch_ratio = float(data.get("post_pitch_ratio", 1.0))
        source_voice = str(data.get("source_voice", "")).strip()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VoiceMatchError(f"无法读取 788 匹配配置：{profile_path}") from exc

    if version != 1:
        raise VoiceMatchError(f"不支持的 788 匹配配置版本：{version}")
    if not source_voice:
        raise VoiceMatchError("788 匹配配置缺少 source_voice")
    if len(frequencies) < 2 or len(frequencies) != len(gains):
        raise VoiceMatchError("788 匹配配置中的频率点与增益数量不一致")
    if not all(math.isfinite(value) for value in frequencies + gains):
        raise VoiceMatchError("788 匹配配置包含非有限数值")
    if frequencies[0] < 0:
        raise VoiceMatchError("788 匹配配置中的频率不能小于 0")
    if any(right <= left for left, right in zip(frequencies, frequencies[1:])):
        raise VoiceMatchError("788 匹配配置中的频率点必须严格递增")
    if not 8000 <= sample_rate <= 192000 or frequencies[-1] > sample_rate / 2:
        raise VoiceMatchError("788 匹配配置中的采样率或最高频率无效")
    if not math.isfinite(pitch_ratio) or not 0.5 <= pitch_ratio <= 2.0:
        raise VoiceMatchError("788 匹配配置中的音高比例无效")
    if any(abs(gain) > 30 for gain in gains):
        raise VoiceMatchError("788 匹配配置中的增益超出安全范围（±30 dB）")

    return VoiceMatchProfile(
        name=str(data.get("name", "788 voice match")),
        source_voice=source_voice,
        target_reference=str(data.get("target_reference", "")),
        reference_transcript=str(data.get("reference_transcript", "")),
        sample_rate_hz=sample_rate,
        post_pitch_ratio=pitch_ratio,
        frequencies_hz=frequencies,
        gains_db=gains,
    )


def ffmpeg_binary() -> str:
    """Return the configured FFmpeg binary or fail with an actionable error."""
    configured = os.environ.get("FFMPEG_BINARY")
    binary = configured or shutil.which("ffmpeg")
    if not binary:
        raise VoiceMatchError(
            "未找到 FFmpeg；请先安装 FFmpeg 后再启用 788 实时匹配"
        )
    return str(binary)


@lru_cache(maxsize=2)
def _available_ffmpeg_filters(binary: str) -> frozenset[str]:
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-filters"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VoiceMatchError(f"无法检查 FFmpeg 音频滤镜：{exc}") from exc

    filters: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0][0:1] in {".", "T", "S", "C", "A", "V", "|"}:
            filters.add(fields[1])
    return frozenset(filters)


def build_filter_graph(
    strength: float | int = 100,
    profile: VoiceMatchProfile | None = None,
    *,
    allow_pitch_shift: bool = True,
) -> str:
    """Build one FFmpeg graph shared by batch and incremental processing."""
    profile = profile or load_profile()
    mix = _bounded_strength(strength)
    binary = ffmpeg_binary()
    available = _available_ffmpeg_filters(binary)
    filters = [f"aresample={profile.sample_rate_hz}"]

    if mix > 0:
        required = {"firequalizer", "alimiter"}
        if allow_pitch_shift and profile.post_pitch_ratio != 1.0:
            required.add("rubberband")
        missing = sorted(required - available)
        if missing:
            raise VoiceMatchError(
                "当前 FFmpeg 缺少 788 完整匹配所需滤镜："
                + ", ".join(missing)
                + "；请安装包含这些滤镜的完整 FFmpeg"
            )

    if (
        allow_pitch_shift
        and mix > 0
        and profile.post_pitch_ratio != 1.0
    ):
        pitch_ratio = 1.0 + (profile.post_pitch_ratio - 1.0) * mix
        filters.append(
            "rubberband="
            f"pitch={pitch_ratio:.6f}:"
            "formant=preserved:pitchq=quality:window=short:transients=crisp"
        )

    if mix > 0:
        gain_entries = ";".join(
            f"entry({frequency:.3f},{gain * mix:.4f})"
            for frequency, gain in zip(profile.frequencies_hz, profile.gains_db)
        )
        filters.append(
            "firequalizer="
            f"gain_entry='{gain_entries}':"
            "delay=0.02:accuracy=1:min_phase=1"
        )

    if "alimiter" in available:
        filters.append("alimiter=limit=0.95")

    return ",".join(filters)


def process_audio_segment(
    audio: AudioSegment,
    strength: float | int = 100,
    profile: VoiceMatchProfile | None = None,
) -> AudioSegment:
    """Apply the calibrated low-latency graph to an in-memory audio segment."""
    if len(audio) == 0:
        return audio
    if _bounded_strength(strength) == 0:
        return audio

    profile = profile or load_profile()
    source = audio.set_channels(1).set_sample_width(2)
    graph = build_filter_graph(strength, profile)
    command = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(source.frame_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-af",
        graph,
        "-f",
        "s16le",
        "-ar",
        str(profile.sample_rate_hz),
        "-ac",
        "1",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            input=source.raw_data,
            check=False,
            capture_output=True,
            timeout=max(30.0, len(source) / 1000.0 * 3.0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VoiceMatchError(f"788 实时匹配处理失败：{exc}") from exc

    if result.returncode != 0 or not result.stdout:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise VoiceMatchError(f"788 实时匹配处理失败：{error or 'FFmpeg 未输出音频'}")

    return AudioSegment(
        data=result.stdout,
        sample_width=2,
        frame_rate=profile.sample_rate_hz,
        channels=1,
    )


def pcm_to_wav_bytes(
    pcm: bytes,
    *,
    sample_rate_hz: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Wrap one PCM block as a standalone WAV for Gradio's stream decoder."""
    if sample_rate_hz <= 0 or channels <= 0 or sample_width <= 0:
        raise VoiceMatchError("实时 PCM 的采样率、声道数或采样宽度无效")
    frame_width = channels * sample_width
    if not pcm or len(pcm) % frame_width:
        raise VoiceMatchError("实时 PCM 块为空或未按采样帧对齐")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(pcm)
    return output.getvalue()


async def stream_edge_tts_788_pcm(
    text: str,
    *,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    proxy: str | None = None,
    strength: float | int = 100,
    profile: VoiceMatchProfile | None = None,
    chunk_duration_ms: int = 200,
) -> AsyncIterator[bytes]:
    """Yield frame-aligned s16le PCM blocks while Edge is synthesizing.

    Edge's MP3 stream is kept compressed until it reaches one persistent
    FFmpeg process. FFmpeg incrementally decodes and filters it, so filter
    state is retained across every network chunk. The caller can wrap each
    block with :func:`pcm_to_wav_bytes` for Gradio and independently accumulate
    the raw PCM into one lossless download stream.
    """
    if not text or not text.strip():
        raise VoiceMatchError("请输入要实时试听的文本")

    import edge_tts

    profile = profile or load_profile()
    try:
        chunk_duration = int(chunk_duration_ms)
    except (TypeError, ValueError) as exc:
        raise VoiceMatchError("实时音频块时长必须是整数毫秒") from exc
    if not 50 <= chunk_duration <= 2000:
        raise VoiceMatchError("实时音频块时长必须在 50–2000 ms 之间")
    bytes_per_chunk = (
        profile.sample_rate_hz * 2 * chunk_duration // 1000
    )
    bytes_per_chunk -= bytes_per_chunk % 2
    if bytes_per_chunk < 2:
        raise VoiceMatchError("实时音频块大小无效")
    graph = build_filter_graph(strength, profile)
    binary = ffmpeg_binary()
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-probesize",
        "32k",
        "-analyzeduration",
        "0",
        "-i",
        "pipe:0",
        "-af",
        graph,
        "-ar",
        str(profile.sample_rate_hz),
        "-ac",
        "1",
        "-f",
        "s16le",
        "pipe:1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise VoiceMatchError(f"无法启动 FFmpeg 实时音频管道：{exc}") from exc
    if process.stdin is None or process.stdout is None or process.stderr is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        await process.wait()
        raise VoiceMatchError("无法创建 788 实时音频管道")

    stderr_reader = asyncio.create_task(process.stderr.read())
    feed_error: Exception | None = None

    async def feed_edge_stream() -> None:
        nonlocal feed_error
        try:
            communicate = edge_tts.Communicate(
                text,
                profile.source_voice,
                rate=rate,
                volume=volume,
                pitch=pitch,
                proxy=proxy or None,
            )
            async for message in communicate.stream():
                if message.get("type") != "audio":
                    continue
                data = message.get("data")
                if data:
                    process.stdin.write(data)
                    await process.stdin.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # propagated after FFmpeg drains
            feed_error = exc
        finally:
            if not process.stdin.is_closing():
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except OSError:
                    pass

    feeder = asyncio.create_task(feed_edge_stream())
    pcm_buffer = bytearray()
    emitted = False
    try:
        while True:
            pcm = await process.stdout.read(8192)
            if not pcm:
                break
            pcm_buffer.extend(pcm)
            while len(pcm_buffer) >= bytes_per_chunk:
                chunk = bytes(pcm_buffer[:bytes_per_chunk])
                del pcm_buffer[:bytes_per_chunk]
                emitted = True
                yield chunk
        if len(pcm_buffer) % 2:
            raise VoiceMatchError("FFmpeg 输出了未按采样帧对齐的 PCM")
        if pcm_buffer:
            emitted = True
            yield bytes(pcm_buffer)

        return_code = await process.wait()
        error_text = (await stderr_reader).decode("utf-8", errors="replace").strip()
        if return_code != 0:
            if not feeder.done():
                feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await feeder
            raise VoiceMatchError(f"788 实时匹配失败：{error_text or 'FFmpeg 异常退出'}")
        await feeder
        if feed_error is not None:
            raise VoiceMatchError(f"Edge 实时合成失败：{feed_error}") from feed_error
        if not emitted:
            raise VoiceMatchError("788 实时匹配未产生音频")
    finally:
        if not feeder.done():
            feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feeder
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        # Process.wait() only waits for the exit code; draining stdout is what
        # lets asyncio close the final pipe transport before the loop exits.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await process.stdout.read()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr_reader
