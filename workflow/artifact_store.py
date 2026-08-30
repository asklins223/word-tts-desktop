"""Bounded staging and immutable content-addressed Blob storage."""

from __future__ import annotations

import hashlib
import errno
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import BinaryIO, Iterable


class ArtifactStoreError(RuntimeError):
    code = "ARTIFACT_INVALID"


class ArtifactTooLarge(ArtifactStoreError):
    code = "RESOURCE_EXHAUSTED"


class ArtifactIntegrityError(ArtifactStoreError):
    code = "ARTIFACT_INVALID"


@dataclass(frozen=True)
class StagedFile:
    key: str
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BlobInfo:
    sha256: str
    size_bytes: int
    format: str
    storage_key: str


def _format_key(value: str) -> str:
    value = str(value or "bin").lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9][a-z0-9+_-]{0,15}", value):
        raise ArtifactStoreError("invalid artifact format")
    return value


class ArtifactStore:
    def __init__(self, root: str | os.PathLike[str], *, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_bytes = max(1, int(max_bytes))
        self.staging_root = self.root / "staging"
        self.blob_root = self.root / "blobs"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        for directory in (self.root, self.staging_root, self.blob_root):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    def _new_staging_path(self) -> tuple[str, Path]:
        key = f"staging/{secrets.token_hex(16)}.part"
        return key, self.root / key

    def stage_stream(
        self,
        source: BinaryIO | Iterable[bytes],
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> StagedFile:
        if expected_size is not None and (expected_size < 0 or expected_size > self.max_bytes):
            raise ArtifactTooLarge("expected artifact size exceeds the local storage budget")
        key, path = self._new_staging_path()
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("xb") as target:
                if hasattr(source, "read"):
                    while True:
                        chunk = source.read(chunk_size)  # type: ignore[union-attr]
                        if not chunk:
                            break
                        size = self._write_chunk(target, digest, chunk, size)
                else:
                    for chunk in source:
                        if not chunk:
                            continue
                        size = self._write_chunk(target, digest, chunk, size)
                target.flush()
                os.fsync(target.fileno())
            if expected_size is not None and size != expected_size:
                raise ArtifactIntegrityError("artifact size does not match the declared size")
            actual_sha256 = digest.hexdigest()
            if expected_sha256 and actual_sha256 != expected_sha256.lower():
                raise ArtifactIntegrityError("artifact SHA-256 does not match the declared digest")
            return StagedFile(key, path, size, actual_sha256)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise

    def _write_chunk(self, target: BinaryIO, digest: "hashlib._Hash", chunk: bytes, size: int) -> int:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ArtifactIntegrityError("artifact stream yielded a non-byte chunk")
        chunk = bytes(chunk)
        next_size = size + len(chunk)
        if next_size > self.max_bytes:
            raise ArtifactTooLarge("artifact exceeds the local storage budget")
        target.write(chunk)
        digest.update(chunk)
        return next_size

    def promote(self, staged: StagedFile, *, format: str) -> BlobInfo:
        fmt = _format_key(format)
        try:
            staging_root = self.staging_root.resolve()
            staged_path = Path(staged.path).expanduser().resolve(strict=True)
            relative_staged_path = staged_path.relative_to(staging_root)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ArtifactIntegrityError("staging file is outside the managed staging directory") from exc
        expected_key = f"staging/{relative_staged_path.as_posix()}"
        if staged.key != expected_key or not staged_path.is_file():
            raise ArtifactIntegrityError("staging file is missing")
        # Re-open through the confined descriptor path immediately before the
        # fingerprint check.  The resolved Path above is only a lexical
        # boundary check; it is not a safe-open primitive by itself.
        actual = self._storage_fingerprint(staged.key)
        if actual != (staged.size_bytes, staged.sha256):
            raise ArtifactIntegrityError("staging file changed after it was verified")
        storage_key = f"blobs/{staged.sha256[:2]}/{staged.sha256}.{fmt}"
        destination = self.root / storage_key
        destination_parts = self._storage_parts(storage_key)
        self._ensure_confined_directory(destination_parts[:-1])
        try:
            destination_stat = os.lstat(destination)
        except FileNotFoundError:
            destination_stat = None
        except OSError as exc:
            raise ArtifactIntegrityError("cannot inspect the content-addressed destination") from exc
        if destination_stat is not None:
            if stat.S_ISLNK(destination_stat.st_mode) or not stat.S_ISREG(destination_stat.st_mode):
                raise ArtifactIntegrityError("content-addressed destination is not a regular file")
            if self._storage_fingerprint(storage_key) != (staged.size_bytes, staged.sha256):
                raise ArtifactIntegrityError("content-addressed destination has different content")
            self._unlink_confined(staged.key)
        else:
            # Rename by directory handle where supported, so a path swap after
            # validation cannot redirect the move through a symlinked parent.
            self._replace_confined(staged.key, storage_key)
            self._fsync_confined_directory(destination_parts[:-1])
        return BlobInfo(staged.sha256, staged.size_bytes, fmt, storage_key)

    def read(self, storage_key: str) -> BinaryIO:
        parts = self._storage_parts(storage_key)
        try:
            return self._open_confined_file(parts)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArtifactIntegrityError("artifact path contains a symlink or is not a regular file") from exc
            raise

    def delete_staging(self, key: str) -> bool:
        if not key.startswith("staging/") or key.startswith("staging/../"):
            return False
        try:
            self._unlink_confined(key)
            return True
        except FileNotFoundError:
            return False
        except (ArtifactStoreError, OSError):
            return False

    def delete_blob(self, key: str) -> bool:
        """Remove one unreferenced Blob entry without following path links."""

        if not key.startswith("blobs/"):
            return False
        try:
            self._unlink_confined(key)
            return True
        except FileNotFoundError:
            return False
        except (ArtifactStoreError, OSError):
            return False

    def has_regular_file(self, key: str) -> bool:
        """Check a managed entry through the same confined open as reads."""

        try:
            with self.read(key):
                return True
        except (FileNotFoundError, ArtifactStoreError, OSError):
            return False

    def scan_orphans(self, referenced_storage_keys: set[str]) -> list[str]:
        """Return unreferenced relative keys without deleting anything."""
        found: list[str] = []
        if not self.blob_root.exists():
            return found
        for path in self.blob_root.rglob("*"):
            if path.is_file():
                relative = path.relative_to(self.root).as_posix()
                if relative not in referenced_storage_keys:
                    found.append(relative)
        return sorted(found)

    def _safe_storage_path(self, storage_key: str) -> Path:
        parts = self._storage_parts(storage_key)
        return self.root.joinpath(*parts).resolve()

    def _storage_parts(self, storage_key: str) -> tuple[str, ...]:
        """Validate a relative storage key and return canonical POSIX parts.

        ``resolve()`` remains a useful early escape check, but callers that
        open, unlink or rename use the returned lexical parts with directory
        handles so a symlink swap cannot invalidate this check between steps.
        """

        if not isinstance(storage_key, str) or not storage_key or storage_key.startswith(("/", "\\")):
            raise ArtifactStoreError("invalid internal storage key")
        if "\\" in storage_key or "\x00" in storage_key:
            raise ArtifactStoreError("invalid internal storage key")
        parts = tuple(PurePosixPath(storage_key).parts)
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ArtifactStoreError("invalid internal storage key")
        candidate = self.root.joinpath(*parts)
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ArtifactStoreError("invalid internal storage key") from exc
        if resolved != self.root and self.root not in resolved.parents:
            raise ArtifactStoreError("storage key escapes the artifact root")
        return parts

    @staticmethod
    def _secure_dirfd_available() -> bool:
        return bool(getattr(os, "O_NOFOLLOW", 0)) and os.open in getattr(os, "supports_dir_fd", set())

    def _open_confined_directory(self, parts: tuple[str, ...]) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(self.root), flags | directory_flag | nofollow_flag)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ArtifactIntegrityError("artifact root is not a directory")
            for part in parts:
                child = os.open(part, flags | directory_flag | nofollow_flag, dir_fd=descriptor)
                try:
                    if not stat.S_ISDIR(os.fstat(child).st_mode):
                        raise ArtifactIntegrityError("artifact path contains a non-directory component")
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _ensure_confined_directory(self, parts: tuple[str, ...]) -> None:
        if self._secure_dirfd_available():
            if not parts:
                return
            parent = self._open_confined_directory(parts[:-1])
            try:
                try:
                    os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            finally:
                os.close(parent)
            directory = self._open_confined_directory(parts)
            try:
                try:
                    os.fchmod(directory, 0o700)
                except OSError:
                    pass
            finally:
                os.close(directory)
            return

        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    def _open_confined_file(self, parts: tuple[str, ...]) -> BinaryIO:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        if self._secure_dirfd_available():
            directory = self._open_confined_directory(parts[:-1])
            file_descriptor = -1
            try:
                file_descriptor = os.open(parts[-1], flags | nofollow_flag, dir_fd=directory)
                if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                    raise ArtifactIntegrityError("artifact content is not a regular file")
                handle = os.fdopen(file_descriptor, "rb")
                file_descriptor = -1
                return handle
            finally:
                os.close(directory)
                if file_descriptor >= 0:
                    os.close(file_descriptor)

        # Windows and other platforms without dir_fd/O_NOFOLLOW still get a
        # final symlink check and a regular-file check.  The supported desktop
        # target uses the stronger descriptor path above; this fallback is
        # deliberately isolated so its platform durability boundary is clear.
        path = self.root.joinpath(*parts)
        if path.is_symlink():
            raise ArtifactIntegrityError("artifact path may not be a symbolic link")
        file_descriptor = os.open(str(path), flags | nofollow_flag)
        try:
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise ArtifactIntegrityError("artifact content is not a regular file")
            handle = os.fdopen(file_descriptor, "rb")
            file_descriptor = -1
            return handle
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    def _storage_fingerprint(self, storage_key: str) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with self.read(storage_key) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    def _fsync_confined_directory(self, parts: tuple[str, ...]) -> None:
        if self._secure_dirfd_available():
            directory = self._open_confined_directory(parts)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return
        self._fsync_directory(self.root.joinpath(*parts))

    def _unlink_confined(self, storage_key: str) -> None:
        parts = self._storage_parts(storage_key)
        if self._secure_dirfd_available() and os.unlink in getattr(os, "supports_dir_fd", set()):
            directory = self._open_confined_directory(parts[:-1])
            try:
                os.unlink(parts[-1], dir_fd=directory)
            finally:
                os.close(directory)
            return
        self.root.joinpath(*parts).unlink()

    def _replace_confined(self, source_key: str, destination_key: str) -> None:
        source_parts = self._storage_parts(source_key)
        destination_parts = self._storage_parts(destination_key)
        supports_dirfd = self._secure_dirfd_available() and os.replace in getattr(os, "supports_dir_fd", set())
        if supports_dirfd:
            source_directory = self._open_confined_directory(source_parts[:-1])
            destination_directory = -1
            try:
                destination_directory = self._open_confined_directory(destination_parts[:-1])
                os.replace(
                    source_parts[-1], destination_parts[-1],
                    src_dir_fd=source_directory, dst_dir_fd=destination_directory,
                )
            finally:
                os.close(source_directory)
                if destination_directory >= 0:
                    os.close(destination_directory)
            return
        self.root.joinpath(*destination_parts).parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.root.joinpath(*source_parts), self.root.joinpath(*destination_parts))

    @staticmethod
    def _file_fingerprint(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
