"""Bounded audio verification, promotion and segment-boundary helpers."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Iterator

from .artifact_store import ArtifactStore, BlobInfo, StagedFile
from .domain import DomainError


AUDIO_ALGORITHM_VERSION = "1"


class AudioError(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class AudioMetadata:
    sha256: str
    size_bytes: int
    format: str
    duration_ms: int | None = None
    algorithm_version: str = AUDIO_ALGORITHM_VERSION


@dataclass(frozen=True)
class SegmentBoundary:
    segment_index: int
    start_ms: int
    end_ms: int
    evidence: str = "verified"


class AudioVerifier:
    """Streaming hash/size validation before a Blob becomes READY."""

    def __init__(self, *, minimum_bytes: int = 1, max_bytes: int | None = None) -> None:
        self.minimum_bytes = max(1, int(minimum_bytes))
        self.max_bytes = None if max_bytes is None else max(1, int(max_bytes))

    def fingerprint(
        self,
        source: BinaryIO | Iterable[bytes],
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> AudioMetadata:
        digest = hashlib.sha256()
        size = 0
        for chunk in _iter_chunks(source, chunk_size):
            size += len(chunk)
            if self.max_bytes is not None and size > self.max_bytes:
                raise AudioError("RESOURCE_EXHAUSTED", "audio exceeds the configured size budget")
            digest.update(chunk)
        actual = digest.hexdigest()
        if size < self.minimum_bytes:
            raise AudioError("ARTIFACT_INVALID", "audio artifact is empty")
        if expected_size is not None and size != int(expected_size):
            raise AudioError("ARTIFACT_INVALID", "audio size does not match the declared size")
        if expected_sha256 and actual != str(expected_sha256).lower():
            raise AudioError("ARTIFACT_INVALID", "audio hash does not match the declared digest")
        return AudioMetadata(actual, size, "bin")

    @staticmethod
    def verify_bytes(data: bytes, *, format: str = "bin", expected_sha256: str | None = None) -> AudioMetadata:
        verifier = AudioVerifier()
        result = verifier.fingerprint(io.BytesIO(data), expected_sha256=expected_sha256)
        return AudioMetadata(result.sha256, result.size_bytes, str(format).lower().lstrip("."))


class AudioProcessor:
    """Stage, verify and promote audio through the shared ArtifactStore."""

    def __init__(self, store: ArtifactStore, *, verifier: AudioVerifier | None = None) -> None:
        self.store = store
        self.verifier = verifier or AudioVerifier(max_bytes=store.max_bytes)

    def process(
        self,
        source: BinaryIO | Iterable[bytes],
        *,
        format: str = "bin",
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> tuple[BlobInfo, AudioMetadata]:
        staged = self.store.stage_stream(
            source,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        try:
            metadata = AudioMetadata(staged.sha256, staged.size_bytes, str(format).lower().lstrip("."))
            if metadata.size_bytes < self.verifier.minimum_bytes:
                raise AudioError("ARTIFACT_INVALID", "audio artifact is empty")
            if self.verifier.max_bytes is not None and metadata.size_bytes > self.verifier.max_bytes:
                raise AudioError("RESOURCE_EXHAUSTED", "audio exceeds the configured size budget")
            blob = self.store.promote(staged, format=metadata.format)
            return blob, metadata
        except Exception:
            self.store.delete_staging(staged.key)
            raise


def validate_segment_boundaries(
    boundaries: Iterable[SegmentBoundary],
    *,
    duration_ms: int,
    require_contiguous: bool = False,
) -> tuple[SegmentBoundary, ...]:
    ordered = tuple(boundaries)
    if duration_ms < 0:
        raise AudioError("VALIDATION_ERROR", "duration_ms must not be negative")
    previous_end = 0
    for expected_index, boundary in enumerate(ordered):
        if boundary.segment_index != expected_index:
            raise AudioError("ARTIFACT_INVALID", "audio segment indexes must be contiguous")
        if boundary.start_ms < 0 or boundary.end_ms <= boundary.start_ms or boundary.end_ms > duration_ms:
            raise AudioError("ARTIFACT_INVALID", "audio segment boundary is outside the source duration")
        if boundary.start_ms < previous_end:
            raise AudioError("ARTIFACT_INVALID", "audio segment boundaries overlap or are out of order")
        if require_contiguous and boundary.start_ms != previous_end:
            raise AudioError("ARTIFACT_INVALID", "audio segment boundaries contain an unaccounted gap")
        previous_end = boundary.end_ms
    return ordered


def looks_like_mp3_bytes(data: bytes | bytearray | memoryview, *, scan_bytes: int = 4096) -> bool:
    """Perform a bounded MP3 container/frame-header check before publication."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        return False
    window = bytes(data)[:max(4, int(scan_bytes))]
    # An ``ID3`` prefix alone is only a metadata tag, not proof that the
    # provider returned audio.  In particular, short test doubles such as
    # ``b"ID3\\x04\\x00\\x00..."`` must not bypass the frame check.
    for index in range(max(0, len(window) - 3)):
        if window[index] != 0xFF or (window[index + 1] & 0xE0) != 0xE0:
            continue
        second = window[index + 1]
        third = window[index + 2]
        version = (second >> 3) & 0x03
        layer = (second >> 1) & 0x03
        bitrate_index = (third >> 4) & 0x0F
        sample_rate_index = (third >> 2) & 0x03
        # Version bits ``01`` are reserved, as are layer 0, bitrate indexes
        # 0/15, and sample-rate index 3.  Checking the complete four-byte
        # header prevents arbitrary ``ff e0`` data from passing the MP3 gate.
        if version != 0x01 and layer != 0 and bitrate_index not in (0, 0x0F) and sample_rate_index != 0x03:
            return True
    return False


def _iter_chunks(source: BinaryIO | Iterable[bytes], chunk_size: int) -> Iterator[bytes]:
    if hasattr(source, "read"):
        while True:
            chunk = source.read(chunk_size)  # type: ignore[union-attr]
            if not chunk:
                return
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise AudioError("ARTIFACT_INVALID", "audio stream yielded a non-byte chunk")
            yield bytes(chunk)
        return
    for chunk in source:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise AudioError("ARTIFACT_INVALID", "audio stream yielded a non-byte chunk")
        if chunk:
            yield bytes(chunk)


__all__ = [
    "AUDIO_ALGORITHM_VERSION", "AudioError", "AudioMetadata", "AudioProcessor",
    "AudioVerifier", "SegmentBoundary", "looks_like_mp3_bytes", "validate_segment_boundaries",
]
