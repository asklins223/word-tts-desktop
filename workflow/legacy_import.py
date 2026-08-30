"""Offline, read-only discovery and one-time import of legacy JSON sessions."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .artifact_store import ArtifactStore
from .domain import canonical_json, content_hash
from .repositories import ConflictError, WorkflowRepository
from .source_imports import SourceImportService


LEGACY_IMPORT_VERSION = "1"
SECRET_MARKERS = ("token", "secret", "password", "cookie", "authorization", "credential", "access_key", "refresh")


@dataclass(frozen=True)
class LegacyItem:
    old_id: str
    doc_type: str
    category: str
    text: str
    filename: str | None
    status: str
    error: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class LegacySession:
    directory: Path
    session_key: str
    source_filename: str
    source_path: Path | None
    source_sha256: str | None
    source_size_bytes: int | None
    config: Mapping[str, Any]
    config_version: str
    items: tuple[LegacyItem, ...]
    parsed_json_path: Path | None
    progress_status: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class LegacyImportReport:
    directory: str
    session_key: str
    dry_run: bool
    applied: bool
    workflow_id: str | None = None
    workflow_group_id: str | None = None
    source_sha256: str | None = None
    source_size_bytes: int | None = None
    config_version: str = "unknown"
    item_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    unresolved_count: int = 0
    artifact_count: int = 0
    artifact_sha256: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LegacyImporter:
    """Discover legacy sessions without writing until ``apply=True``."""

    def __init__(
        self,
        legacy_root: str | os.PathLike[str],
        *,
        source_root: str | os.PathLike[str] | None = None,
        repository: WorkflowRepository | None = None,
        artifact_store: ArtifactStore | None = None,
        source_imports: SourceImportService | None = None,
    ) -> None:
        self.legacy_root = Path(legacy_root).expanduser().resolve()
        self.source_root = Path(source_root).expanduser().resolve() if source_root else self.legacy_root
        self.repository = repository
        self.artifact_store = artifact_store
        self.source_imports = source_imports

    def discover(self) -> list[LegacySession]:
        if not self.legacy_root.is_dir():
            raise FileNotFoundError(f"legacy root does not exist: {self.legacy_root}")
        sessions: list[LegacySession] = []
        for candidate in sorted(self.legacy_root.iterdir(), key=lambda path: path.name):
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            session = self._read_session(candidate)
            if session is not None:
                sessions.append(session)
        return sessions

    def run(self, *, apply: bool = False) -> list[LegacyImportReport]:
        sessions = self.discover()
        reports: list[LegacyImportReport] = []
        for session in sessions:
            report = self._report_for(session, dry_run=not apply)
            if apply and report.error is None:
                try:
                    self._apply_session(session, report)
                except Exception as exc:  # preserve per-session evidence and continue safely
                    report.error = str(exc)[:1000]
                    report.warnings.append("import transaction did not complete; original legacy files were not modified")
            reports.append(report)
        return reports

    def _report_for(self, session: LegacySession, *, dry_run: bool) -> LegacyImportReport:
        succeeded = sum(item.status == "SUCCEEDED" for item in session.items)
        failed = sum(item.status == "FAILED" for item in session.items)
        unresolved = sum(item.status in {"PENDING", "RUNNING", "UNRESOLVED"} for item in session.items)
        warnings = list(session.warnings)
        for item in session.items:
            if item.status == "SUCCEEDED":
                audio = self._audio_path(session.directory, item.filename)
                if audio is None:
                    unresolved += 1
                    warnings.append(f"missing or unsafe audio for legacy item {item.old_id}")
                elif dry_run:
                    try:
                        self._fingerprint_file(audio)
                    except OSError as exc:
                        warnings.append(f"cannot read legacy audio {item.old_id}: {exc}")
        return LegacyImportReport(
            directory=str(session.directory),
            session_key=session.session_key,
            dry_run=dry_run,
            applied=False,
            source_sha256=session.source_sha256,
            source_size_bytes=session.source_size_bytes,
            config_version=session.config_version,
            item_count=len(session.items),
            succeeded_count=succeeded,
            failed_count=failed,
            unresolved_count=unresolved,
            warnings=warnings[:100],
        )

    def _apply_session(self, session: LegacySession, report: LegacyImportReport) -> None:
        if self.repository is None or self.artifact_store is None or self.source_imports is None:
            raise RuntimeError("LegacyImporter apply requires a WorkflowRepository, ArtifactStore and SourceImportService")
        token = session.session_key
        workflow_id = f"legacy-workflow-{token}"
        group_id = f"legacy-group-{token}"
        report.workflow_id = workflow_id
        report.workflow_group_id = group_id
        try:
            existing = self.repository.get_workflow(workflow_id)
        except Exception:
            existing = None
        if existing is not None:
            report.applied = True
            report.dry_run = False
            report.warnings.append("deterministic workflow already exists; no legacy row or Artifact was duplicated")
            report.item_count = existing.item_count
            report.artifact_count = existing.artifact_count
            return

        configuration = {
            "legacy_import_version": LEGACY_IMPORT_VERSION,
            # The session key is a deterministic local identity, not a
            # credential. Use an explicit reference name so the repository's
            # credential guard does not mistake it for bearer material.
            "legacy_session_ref": token,
            "source_filename": session.source_filename,
            "source_sha256": session.source_sha256,
            "source_size_bytes": session.source_size_bytes,
            "config_version": session.config_version,
            "legacy_config": _safe_value(session.config),
        }
        definition = {
            "workflow_type": "legacy-tts",
            "steps": [{"key": "legacy-import", "type": "LEGACY_IMPORT", "version": LEGACY_IMPORT_VERSION}],
        }
        snapshot = self.repository.create_workflow(
            "legacy-tts",
            configuration,
            business_key=f"legacy:{token}",
            definition_family="legacy-import",
            definition_version=LEGACY_IMPORT_VERSION,
            definition_snapshot=definition,
            workflow_id=workflow_id,
            workflow_group_id=group_id,
            request_id=f"legacy-request-{token}",
        )
        step_id = self.repository.create_step(
            workflow_id,
            step_key="legacy-import",
            step_type="LEGACY_IMPORT",
        )
        item_statuses: dict[str, str] = {}
        item_ids: dict[str, str] = {}
        plan_hash = content_hash({"session_key": token, "items": [item.old_id for item in session.items]})
        for sequence, item in enumerate(session.items):
            item_key = self._item_identity(item, sequence)
            status = item.status
            audio_missing = status == "SUCCEEDED" and self._audio_path(session.directory, item.filename) is None
            if audio_missing:
                status = "UNRESOLVED"
                report.unresolved_count += 1
            item_key_digest = hashlib.sha256(item_key.encode("utf-8")).hexdigest()[:24]
            item_id = f"legacy-item-{token}-{item_key_digest}"
            self.repository.create_item(
                workflow_id,
                item_type=item.category or "legacy-item",
                sequence=sequence,
                normalized_content=item.text,
                item_identity_key=item_key,
                role=None,
                voice_key=None,
                item_id=item_id,
                metadata={
                    "legacy_id": item.old_id,
                    "doc_type": item.doc_type,
                    "category": item.category,
                    "filename": item.filename,
                    "error": item.error,
                    "legacy_status": item.status,
                },
                status=status,
            )
            self.repository.create_assignment(
                workflow_id,
                step_id=step_id,
                item_id=item_id,
                delivery_unit_key=f"legacy:{token}",
                plan_hash=plan_hash,
            )
            item_ids[item.old_id or str(sequence)] = item_id
            item_statuses[item_id] = status

            if status == "SUCCEEDED":
                audio = self._audio_path(session.directory, item.filename)
                if audio is not None:
                    fmt = audio.suffix.lstrip(".").lower() or "bin"
                    with audio.open("rb") as source_audio:
                        staged = self.artifact_store.stage_stream(source_audio)
                    try:
                        blob = self.artifact_store.promote(staged, format=fmt)
                    finally:
                        # promote removes the part; this close is still needed
                        # on the original source handle.
                        pass
                    artifact_digest = hashlib.sha256((item_key + blob.sha256).encode("utf-8")).hexdigest()[:24]
                    artifact_id = f"legacy-artifact-{token}-{artifact_digest}"
                    self.repository.attach_imported_artifact(
                        workflow_id,
                        artifact_id=artifact_id,
                        blob=blob,
                        artifact_type="legacy-audio",
                        producer="legacy-import",
                        producer_version=LEGACY_IMPORT_VERSION,
                        item_id=item_id,
                        step_id=step_id,
                    )
                    report.artifact_count += 1
                    report.artifact_sha256.append(blob.sha256)

        if session.parsed_json_path is not None and session.parsed_json_path.is_file():
            with session.parsed_json_path.open("rb") as parsed_source:
                staged = self.artifact_store.stage_stream(parsed_source)
            blob = self.artifact_store.promote(staged, format="json")
            artifact_id = f"legacy-parsed-{token}"
            self.repository.attach_imported_artifact(
                workflow_id,
                artifact_id=artifact_id,
                blob=blob,
                artifact_type="legacy-parsed",
                producer="legacy-import",
                producer_version=LEGACY_IMPORT_VERSION,
                step_id=step_id,
            )
            report.artifact_count += 1
            report.artifact_sha256.append(blob.sha256)

        source_warning = self._import_source(session, workflow_id, report)
        if source_warning:
            report.warnings.append(source_warning)
        blocked = bool(report.unresolved_count or report.warnings)
        self.repository.finalize_legacy_import(
            workflow_id,
            step_id=step_id,
            item_statuses=item_statuses,
            blocked=blocked,
            warnings=report.warnings,
        )
        report.applied = True
        report.dry_run = False
        report.artifact_sha256 = sorted(set(report.artifact_sha256))

    def _import_source(self, session: LegacySession, workflow_id: str, report: LegacyImportReport) -> str | None:
        source = session.source_path
        if source is None:
            return "legacy source file was not found under the explicitly allowed source root; run remains blocked"
        try:
            size, digest = self._fingerprint_file(source)
            metadata = {
                "filename": session.source_filename,
                "legacy_session_ref": session.session_key,
                "sha256": digest,
                "size_bytes": size,
                "import_version": LEGACY_IMPORT_VERSION,
            }
            created = self.source_imports.create_import(
                workflow_id,
                metadata=metadata,
                expected_size_bytes=size,
                expected_sha256=digest,
                content_type="application/octet-stream",
                request_key=f"legacy-source:{session.session_key}",
            )
            if created["source_artifact_id"]:
                return None
            grant = self.source_imports.acquire_writer(
                created["source_import_id"],
                created["staging_generation"],
                expected_state_version=created["state_version"],
            )
            with source.open("rb") as content:
                ready = self.source_imports.write_generation(
                    created["source_import_id"],
                    created["staging_generation"],
                    content,
                    grant=grant.token,
                    format=source.suffix.lstrip(".").lower() or "bin",
                )
            if not ready.get("source_artifact_id"):
                return "source import did not produce a READY Artifact"
            return None
        except Exception as exc:
            return f"source import unavailable: {str(exc)[:500]}"

    def _read_session(self, directory: Path) -> LegacySession | None:
        warnings: list[str] = []
        progress = self._read_json(directory / "progress.json")
        parsed = self._read_json(directory / "parsed.json")
        fingerprint = self._read_json(directory / "source_fingerprint.json")
        history = self._read_json(directory / "history.json")
        if progress is None and parsed is None and history is None:
            return None
        session_key_raw = str((history or {}).get("session_id") or directory.name)
        session_key = hashlib.sha256(session_key_raw.encode("utf-8")).hexdigest()[:32]
        source_filename = os.path.basename(str((progress or history or {}).get("source_file") or (progress or history or {}).get("source_filename") or "legacy-source.bin"))
        config = (progress or {}).get("config") if isinstance((progress or {}).get("config"), Mapping) else {}
        config_version = str(config.get("tts_config_version") or config.get("config_version") or config.get("version") or "legacy-unknown")
        source_path = self._resolve_source(directory, progress, source_filename, warnings)
        source_sha256 = str((fingerprint or {}).get("sha256") or "") or None
        source_size = self._as_nonnegative_int((fingerprint or {}).get("size"))
        if source_path is not None:
            try:
                actual_size, actual_sha = self._fingerprint_file(source_path)
                source_size = actual_size
                source_sha256 = actual_sha
            except OSError as exc:
                warnings.append(f"source file cannot be read: {exc}")

        items = self._extract_items(progress, parsed, history, warnings)
        status = str((progress or history or {}).get("status") or "unknown")
        parsed_path = directory / "parsed.json" if parsed is not None and (directory / "parsed.json").is_file() else None
        return LegacySession(
            directory=directory,
            session_key=session_key,
            source_filename=source_filename,
            source_path=source_path,
            source_sha256=source_sha256,
            source_size_bytes=source_size,
            config=_safe_value(config),
            config_version=config_version,
            items=tuple(items),
            parsed_json_path=parsed_path,
            progress_status=status,
            warnings=tuple(warnings),
        )

    def _extract_items(
        self,
        progress: Mapping[str, Any] | None,
        parsed: Any,
        history: Mapping[str, Any] | None,
        warnings: list[str],
    ) -> list[LegacyItem]:
        progress_items = progress.get("items") if isinstance(progress, Mapping) else []
        by_id = {str(item.get("id")): item for item in progress_items or [] if isinstance(item, Mapping) and item.get("id")}
        parsed_items: list[tuple[str, Mapping[str, Any]]] = []
        if isinstance(parsed, list):
            for group in parsed:
                if not isinstance(group, Mapping):
                    continue
                doc_type = str(group.get("doc_type") or group.get("type") or "legacy")
                for raw in group.get("items") or []:
                    if isinstance(raw, Mapping):
                        parsed_items.append((doc_type, raw))
        if not parsed_items:
            for item in progress_items or []:
                if not isinstance(item, Mapping):
                    continue
                raw = item.get("raw_item") if isinstance(item.get("raw_item"), Mapping) else item
                parsed_items.append((str(item.get("doc_type") or "legacy"), raw))
        if not parsed_items and isinstance(history, Mapping):
            for item in history.get("files") or []:
                if isinstance(item, Mapping):
                    parsed_items.append((str(item.get("doc_type") or "legacy"), item))
        result: list[LegacyItem] = []
        for index, (doc_type, raw) in enumerate(parsed_items):
            old_id = str(raw.get("id") or "") or str((progress_items[index] if index < len(progress_items) and isinstance(progress_items[index], Mapping) else {}).get("id") or f"legacy-{index}")
            progress_item = by_id.get(old_id, progress_items[index] if index < len(progress_items) and isinstance(progress_items[index], Mapping) else {})
            text = str(raw.get("text") or raw.get("content") or progress_item.get("text") or "").strip()
            if not text:
                warnings.append(f"legacy item {old_id} has no text and was imported as unresolved")
            legacy_status = str(progress_item.get("status") or raw.get("status") or "pending").lower()
            status = "SUCCEEDED" if legacy_status in {"done", "completed", "success", "succeeded"} else "FAILED" if legacy_status in {"error", "failed", "failure"} else "UNRESOLVED" if legacy_status in {"ambiguous", "unknown"} else "PENDING"
            if not text:
                status = "UNRESOLVED"
            result.append(LegacyItem(
                old_id=old_id,
                doc_type=doc_type,
                category=str(raw.get("category") or progress_item.get("category") or "legacy"),
                text=text or "[legacy item has no text]",
                filename=os.path.basename(str(progress_item.get("filename") or raw.get("filename") or "")) or None,
                status=status,
                error=str(progress_item.get("error") or raw.get("error") or "")[:1000] or None,
                raw=_safe_value(raw),
            ))
        return result

    def _resolve_source(self, directory: Path, progress: Mapping[str, Any] | None, source_filename: str, warnings: list[str]) -> Path | None:
        raw = str((progress or {}).get("source_path") or "")
        candidates = []
        if raw:
            candidates.append(Path(raw).expanduser())
        candidates.extend((self.source_root / source_filename, directory / source_filename, directory / "source" / source_filename))
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not resolved.is_file() or resolved.is_symlink():
                continue
            if not self._within(resolved, self.source_root) and not self._within(resolved, directory):
                warnings.append("legacy source_path was outside the explicitly allowed source root and was ignored")
                continue
            return resolved
        return None

    @staticmethod
    def _audio_path(directory: Path, filename: str | None) -> Path | None:
        if not filename or os.path.basename(filename) != filename:
            return None
        audio = directory / "audio" / filename
        try:
            resolved = audio.resolve()
        except OSError:
            return None
        if not resolved.is_file() or resolved.is_symlink() or not LegacyImporter._within(resolved, directory / "audio"):
            return None
        return resolved

    @staticmethod
    def _item_identity(item: LegacyItem, sequence: int) -> str:
        if item.old_id.strip():
            return f"legacy:{item.old_id}"
        return f"legacy:content:{hashlib.sha256(f'{item.doc_type}|{item.category}|{sequence}|{item.text}'.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.is_file() or path.is_symlink():
            return None
        try:
            with path.open("r", encoding="utf-8") as source:
                return json.load(source)
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _fingerprint_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _as_nonnegative_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            return os.path.commonpath([str(path.resolve()), str(root.resolve())]) == str(root.resolve())
        except (OSError, ValueError):
            return False


def _safe_value(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:64]:
            member = str(raw_key)
            if any(marker in member.lower() for marker in SECRET_MARKERS):
                # Keeping a credential-like member name would still be
                # rejected by the repository boundary, even if the value had
                # already been replaced.
                continue
            safe[member] = _safe_value(raw_value, member)
        return safe
    if any(marker in key.lower() for marker in SECRET_MARKERS):
        return "[REDACTED]"
    if isinstance(value, (list, tuple)):
        return [_safe_value(v, key) for v in list(value)[:64]]
    if isinstance(value, str):
        return value[:20000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:20000]
