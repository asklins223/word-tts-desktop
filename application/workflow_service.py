"""Use-case orchestration for the desktop workflow API.

The service owns the vertical boundaries between source import, parsing and
generation.  FastAPI handlers and Electron transport code do not call legacy
parser/provider functions directly, and no user-controlled path crosses this
module's public API.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Sequence

from workflow.artifact_store import ArtifactStore, ArtifactStoreError
from workflow.domain import DomainError, canonical_json, content_hash
from workflow.engine import TTSRunResult, WorkflowEngine
from workflow.parser import ParsedDocument, ParserPort
from workflow.providers import ProviderCapabilityError, ProviderRegistry, TTSProviderPort
from workflow.repositories import NotFoundError, RepositoryError, WorkflowRepository
from workflow.source_imports import SourceImportService


class WorkflowApplicationError(DomainError):
    """A stable application-layer error that is safe to expose to the API."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(code, message)
        self.details = dict(details or {})


class WorkflowApplicationService:
    """Coordinate one complete workflow use case through durable services."""

    def __init__(
        self,
        repository: WorkflowRepository,
        source_imports: SourceImportService,
        artifact_store: ArtifactStore,
        *,
        parser: ParserPort | None = None,
        engine: WorkflowEngine | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.source_imports = source_imports
        self.artifact_store = artifact_store
        self.parser = parser
        self.engine = engine or WorkflowEngine(repository, artifact_store)
        self.providers = providers or ProviderRegistry()

    def create_draft(
        self,
        workflow_type: str,
        configuration: Mapping[str, Any],
        *,
        business_key: str | None = None,
        request_id: str | None = None,
    ):
        return self.repository.create_workflow(
            workflow_type,
            configuration,
            business_key=business_key,
            request_id=request_id,
        )

    def import_source(
        self,
        workflow_id: str,
        content: BinaryIO | Iterable[bytes] | bytes | bytearray | memoryview,
        *,
        filename: str,
        expected_sha256: str | None = None,
        request_key: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Import bytes through the single-writer source-generation protocol."""

        source = self._as_stream(content)
        expected_size = self._content_length(content)
        created = self.source_imports.create_import(
            workflow_id,
            metadata={"filename": self._safe_filename(filename)},
            expected_size_bytes=expected_size,
            expected_sha256=expected_sha256,
            content_type=content_type,
            request_key=request_key,
        )
        current = self.source_imports.get_generation(
            created["source_import_id"], created["staging_generation"],
        )
        grant = self.source_imports.acquire_writer(
            created["source_import_id"],
            created["staging_generation"],
            expected_state_version=current["state_version"],
        )
        self.source_imports.write_generation(
            created["source_import_id"],
            created["staging_generation"],
            source,
            grant=grant.token,
            format="bin",
        )
        return self.source_imports.get_import(created["source_import_id"])

    def parse(
        self,
        workflow_id: str,
        *,
        expected_state_version: int,
        source_artifact_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Parse the managed source Blob and atomically publish normalized items."""

        if self.parser is None:
            raise WorkflowApplicationError("DEPENDENCY_NOT_READY", "document parser is not configured")
        snapshot = self.repository.get_workflow(workflow_id)
        source_id = str(source_artifact_id or snapshot.source_artifact_id or "")
        if not source_id:
            raise WorkflowApplicationError("SOURCE_NOT_AVAILABLE", "workflow has no ready source Artifact")
        source = self.repository.get_artifact_storage(source_id, workflow_id=workflow_id)
        filename = self._source_filename(workflow_id, source_id)

        parsed: ParsedDocument
        with tempfile.TemporaryDirectory(prefix="wordtts-parse-") as temp_dir:
            suffix = Path(filename).suffix.lower()
            if suffix not in {".docx", ".xlsx"}:
                suffix = ".docx"
            temp_path = Path(temp_dir) / f"source{suffix}"
            try:
                with self.artifact_store.read(str(source["storage_key"])) as source_handle, temp_path.open("xb") as target:
                    while True:
                        chunk = source_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                parsed = self.parser.parse(
                    temp_path,
                    source_basis=source["sha256"][:32],
                    source_filename=filename,
                )
            except ArtifactStoreError:
                raise
            except OSError as exc:
                raise WorkflowApplicationError("PERSISTENCE_ERROR", "managed source could not be prepared for parsing") from exc

        serialized = canonical_json(parsed.as_dict()).encode("utf-8")
        try:
            staged = self.artifact_store.stage_stream(io.BytesIO(serialized), expected_size=len(serialized))
            parsed_blob = self.artifact_store.promote(staged, format="json")
        except ArtifactStoreError:
            raise

        updated = self.repository.persist_parsed_document(
            workflow_id,
            parsed,
            source_artifact_id=source_id,
            expected_state_version=expected_state_version,
            parsed_blob=parsed_blob,
            parsed_artifact_id=f"artifact-parse-{content_hash(f'{workflow_id}:{parsed.source_sha256}')[:32]}",
            request_id=request_id,
        )
        return {
            "workflow": updated,
            "parse_results": self._legacy_parse_results(parsed),
            "source_filename": parsed.source_filename,
            "source_artifact_id": source_id,
            "parsed_artifact_id": f"artifact-parse-{content_hash(f'{workflow_id}:{parsed.source_sha256}')[:32]}",
        }

    def start_generation(
        self,
        workflow_id: str,
        *,
        expected_state_version: int,
        generation_mode: str | None = None,
        provider: str | None = None,
        account_scope: str | None = None,
        expected_configuration_revision: int | None = None,
        request_id: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], bool] | None = None,
        item_ids: Sequence[str] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> tuple[Any, TTSRunResult]:
        """Accept a generation command and run it through the provider port."""

        snapshot = self.accept_generation(
            workflow_id,
            expected_state_version=expected_state_version,
            generation_mode=generation_mode,
            provider=provider,
            account_scope=account_scope,
            expected_configuration_revision=expected_configuration_revision,
            request_id=request_id,
        )
        config = self._configuration(workflow_id)
        mode = str(generation_mode or config.get("generation_mode") or "composite_cut")
        provider_name = str(provider or config.get("provider") or "xunfei")
        scope = str(account_scope or config.get("account_scope") or "xunfei-default")
        if item_ids is None and self._all_items_skipped(workflow_id):
            return snapshot, self._complete_skipped_result(workflow_id)
        adapter = self.provider(provider_name, scope)
        result = self.engine.run_tts(
            workflow_id,
            adapter,
            generation_mode=mode,
            cancel_check=cancel_check,
            pause_check=pause_check,
            item_ids=item_ids,
            progress_callback=progress_callback,
        )
        return snapshot, result

    def accept_generation(
        self,
        workflow_id: str,
        *,
        expected_state_version: int,
        generation_mode: str | None = None,
        provider: str | None = None,
        account_scope: str | None = None,
        expected_configuration_revision: int | None = None,
        request_id: str | None = None,
    ):
        """Validate provider capability and durably accept a generate command."""

        config = self._configuration(workflow_id)
        requested_configuration = dict(config)
        for key, value in (
            ("generation_mode", generation_mode),
            ("provider", provider),
            ("account_scope", account_scope),
        ):
            if value is not None:
                requested_configuration[key] = value

        # The request may arrive directly from a non-renderer client.  Keep
        # the values used for this acceptance in the durable workflow
        # configuration instead of passing them only through the in-memory
        # worker task.  Otherwise a restart would silently run with the old
        # provider/mode/account while the accepted response described the
        # new request.
        configuration_changed = canonical_json(requested_configuration) != canonical_json(config)
        if expected_configuration_revision is not None and not configuration_changed:
            current_revision = self.repository.get_configuration_revision(workflow_id)
            if int(expected_configuration_revision) != current_revision:
                raise RepositoryError(
                    "workflow configuration revision is stale",
                    code="CONFIGURATION_CONFLICT",
                    details={
                        "expected_configuration_revision": int(expected_configuration_revision),
                        "current_configuration_revision": current_revision,
                    },
                )

        provider_name = str(requested_configuration.get("provider") or "xunfei")
        scope = str(requested_configuration.get("account_scope") or "xunfei-default")
        # An all-skipped workflow is a local, billable-free completion.  Do
        # not make it depend on an external provider being configured or
        # logged in merely to close the durable workflow state.
        items = self.repository.list_items(workflow_id)
        all_skipped = bool(items) and all(
            str(item.get("status") or "") == "SKIPPED" for item in items
        )
        if not all_skipped:
            adapter = self.provider(provider_name, scope)
            self._ensure_provider_ready(adapter)
        snapshot = self.repository.get_workflow(workflow_id)
        if configuration_changed:
            snapshot = self.repository.patch_draft(
                workflow_id,
                expected_state_version,
                expected_configuration_revision=expected_configuration_revision,
                configuration=requested_configuration,
                request_id=request_id,
            )
            expected_state_version = snapshot.state_version
        if snapshot.status == "DRAFT" or snapshot.execution_state != "RUNNING":
            return self.repository.command(
                workflow_id,
                "generate",
                expected_state_version,
                request_id=request_id,
                reason=f"generation_mode={requested_configuration.get('generation_mode') or 'composite_cut'}",
            )
        if snapshot.state_version != expected_state_version:
            raise RepositoryError("workflow state_version is stale", code="STATE_CONFLICT")
        return snapshot

    def run_generation(
        self,
        workflow_id: str,
        *,
        generation_mode: str | None = None,
        provider: str | None = None,
        account_scope: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], bool] | None = None,
        item_ids: Sequence[str] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> TTSRunResult:
        """Run an already-accepted workflow without issuing another command."""

        config = self._configuration(workflow_id)
        mode = str(generation_mode or config.get("generation_mode") or "composite_cut")
        provider_name = str(provider or config.get("provider") or "xunfei")
        scope = str(account_scope or config.get("account_scope") or "xunfei-default")
        items = self.repository.list_items(workflow_id)
        all_skipped = item_ids is None and bool(items) and all(
            str(item.get("status") or "") == "SKIPPED" for item in items
        )
        if all_skipped:
            return self._complete_skipped_result(workflow_id)
        adapter = self.provider(provider_name, scope)
        self._ensure_provider_ready(adapter)
        return self.engine.run_tts(
            workflow_id,
            adapter,
            generation_mode=mode,
            cancel_check=cancel_check,
            pause_check=pause_check,
            item_ids=item_ids,
            progress_callback=progress_callback,
        )

    def _all_items_skipped(self, workflow_id: str) -> bool:
        items = self.repository.list_items(workflow_id)
        return bool(items) and all(str(item.get("status") or "") == "SKIPPED" for item in items)

    def _complete_skipped_result(self, workflow_id: str) -> TTSRunResult:
        snapshot = self.repository.complete_skipped_workflow(workflow_id)
        return TTSRunResult(
            workflow_id,
            "",
            "",
            "",
            "",
            None,
            tuple(),
            "SUCCEEDED" if snapshot.result_status == "SUCCEEDED" else snapshot.result_status,
            None,
            False,
        )

    def provider(self, provider: str, account_scope: str) -> TTSProviderPort:
        return self.providers.get(provider, account_scope)

    def command(self, workflow_id: str, action: str, *, expected_state_version: int, reason: str | None = None, request_id: str | None = None):
        return self.repository.command(
            workflow_id,
            action,
            expected_state_version,
            reason=reason,
            request_id=request_id,
        )

    def artifacts(self, workflow_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        return self.repository.list_artifacts(workflow_id, limit=limit)

    def create_export_zip(
        self,
        workflow_id: str,
        *,
        include_item_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Create a deterministic ZIP and return its complete delivery scope."""

        snapshot = self.repository.get_workflow(workflow_id)
        if snapshot.execution_state != "TERMINAL":
            raise WorkflowApplicationError(
                "DEPENDENCY_NOT_READY",
                "工作流尚未完成，暂时不能整理 ZIP 交付文件",
            )
        if snapshot.result_status not in {"SUCCEEDED", "PARTIAL_SUCCESS"}:
            raise WorkflowApplicationError(
                "DEPENDENCY_NOT_READY",
                "当前工作流没有可整理的已验证音频",
            )

        all_segments = self.repository.list_verified_tts_segments(workflow_id)
        item_rows = self.repository.list_items(workflow_id)
        item_by_id = {str(item["item_id"]): item for item in item_rows}
        requested_ids: list[str] | None = None
        requested_set: set[str] | None = None
        if include_item_ids is not None:
            requested_ids = [str(item_id).strip() for item_id in include_item_ids if str(item_id).strip()]
            if not requested_ids or len(set(requested_ids)) != len(requested_ids):
                raise WorkflowApplicationError("VALIDATION_ERROR", "include_item_ids must contain unique item ids")
            missing_ids = [item_id for item_id in requested_ids if item_id not in item_by_id]
            if missing_ids:
                raise WorkflowApplicationError(
                    "NOT_FOUND",
                    "delivery item does not exist",
                    details={"item_ids": missing_ids},
                )
            requested_set = set(requested_ids)
            segments = [segment for segment in all_segments if str(segment["item_id"]) in requested_set]
        else:
            segments = list(all_segments)

        ready_item_ids = {str(segment["item_id"]) for segment in all_segments}
        included_item_ids = [str(segment["item_id"]) for segment in segments]
        included_set = set(included_item_ids)
        excluded_item_ids: list[str] = []
        exclusion_reasons: dict[str, str] = {}
        for item in item_rows:
            item_id = str(item["item_id"])
            if item_id in included_set:
                continue
            status = str(item.get("status") or "")
            if requested_set is not None and item_id not in requested_set and item_id in ready_item_ids:
                reason = "NOT_SELECTED"
            elif status == "SKIPPED":
                reason = "ITEM_SKIPPED"
            elif status in {"AMBIGUOUS", "UNRESOLVED"}:
                reason = "REQUIRES_RECONCILE"
            elif status == "FAILED":
                reason = "ITEM_FAILED"
            elif status == "CANCELLED":
                reason = "ITEM_CANCELLED"
            elif status == "SUCCEEDED":
                reason = "ARTIFACT_MISSING_OR_UNVERIFIED"
            else:
                reason = "NOT_GENERATED"
            excluded_item_ids.append(item_id)
            exclusion_reasons[item_id] = reason

        if not segments:
            raise WorkflowApplicationError(
                "DEPENDENCY_NOT_READY",
                "当前工作流没有可整理的已验证音频",
                details={
                    "included_item_ids": [],
                    "excluded_item_ids": excluded_item_ids,
                    "exclusion_reasons": exclusion_reasons,
                },
            )

        export_basis = [
            {
                "item_id": str(segment["item_id"]),
                "sequence": int(segment["sequence"]),
                "artifact_id": str(segment["artifact_id"]),
                "sha256": str(segment["sha256"]),
            }
            for segment in segments
        ]
        export_hash = content_hash({
            "workflow_id": workflow_id,
            "segments": export_basis,
            "requested_item_ids": sorted(requested_ids) if requested_ids is not None else None,
        })
        artifact_id = f"artifact-export-{export_hash[:32]}"
        existing = next(
            (
                artifact for artifact in self.repository.list_artifacts(workflow_id, limit=2000)
                if artifact.get("artifact_id") == artifact_id
                and artifact.get("artifact_type") == "export-zip"
                and artifact.get("lifecycle_state") == "READY"
                and artifact.get("verified") is True
            ),
            None,
        )
        if existing is not None:
            return {
                **existing,
                "included_count": len(segments),
                "included_item_ids": included_item_ids,
                "excluded_item_ids": excluded_item_ids,
                "exclusion_reasons": exclusion_reasons,
                "filename": self._export_filename(workflow_id),
                "extension": ".zip",
                "mime_type": "application/zip",
                "duration_ms": None,
            }

        width = max(3, len(str(max(1, len(segments)))))
        temporary_path: Path | None = None
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=f"{artifact_id}-",
                suffix=".zip.part",
                dir=str(self.artifact_store.staging_root),
            )
            os.close(fd)
            temporary_path = Path(raw_path)
            with zipfile.ZipFile(
                temporary_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                for segment in segments:
                    fmt = str(segment.get("format") or "mp3").lower().lstrip(".") or "bin"
                    entry_name = f"{int(segment['sequence']) + 1:0{width}d}.{fmt}"
                    info = zipfile.ZipInfo(entry_name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (0o644 & 0xFFFF) << 16
                    with archive.open(info, mode="w", force_zip64=True) as target:
                        with self.artifact_store.read(str(segment["storage_key"])) as source:
                            shutil.copyfileobj(source, target, length=1024 * 1024)

            with temporary_path.open("rb") as source:
                staged = self.artifact_store.stage_stream(source)
            blob = self.artifact_store.promote(staged, format="zip")
            self.repository.attach_export_artifact(
                workflow_id,
                artifact_id=artifact_id,
                blob=blob,
                parent_artifact_ids=[str(segment["artifact_id"]) for segment in segments],
            )
            return {
                "artifact_id": artifact_id,
                "workflow_id": workflow_id,
                "artifact_type": "export-zip",
                "lifecycle_state": "READY",
                "sha256": blob.sha256,
                "size_bytes": blob.size_bytes,
                "format": blob.format,
                "producer": "workflow-export",
                "producer_version": "1",
                "verified": True,
                "included_count": len(segments),
                "included_item_ids": included_item_ids,
                "excluded_item_ids": excluded_item_ids,
                "exclusion_reasons": exclusion_reasons,
                "filename": self._export_filename(workflow_id),
                "extension": ".zip",
                "mime_type": "application/zip",
                "duration_ms": None,
            }
        except (ArtifactStoreError, OSError, zipfile.BadZipFile) as exc:
            if isinstance(exc, ArtifactStoreError):
                raise
            raise WorkflowApplicationError(
                "DOWNLOAD_ERROR",
                "整理 ZIP 交付文件失败",
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _export_filename(self, workflow_id: str) -> str:
        """Create a safe stable filename without exposing source paths."""

        try:
            snapshot = self.repository.get_workflow(workflow_id)
            if snapshot.source_artifact_id:
                source_name = self._source_filename(workflow_id, snapshot.source_artifact_id)
                stem = Path(source_name).stem or "wordtts"
            else:
                stem = "wordtts"
        except (NotFoundError, RepositoryError, OSError):
            stem = "wordtts"
        return f"{Path(stem).name[:200]}_tts.zip"

    def workflows(self, *, limit: int = 100):
        return self.repository.list_workflows(limit=limit)

    def history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_history_records(limit=limit)

    def archive(
        self,
        workflow_id: str,
        *,
        expected_state_version: int,
        reason: str | None = None,
        request_id: str | None = None,
    ):
        return self.repository.archive_workflow(
            workflow_id,
            expected_state_version=expected_state_version,
            reason=reason,
            request_id=request_id,
        )

    def _configuration(self, workflow_id: str) -> dict[str, Any]:
        # Keep the server-owned configuration revision marker out of every
        # provider-facing configuration read.  The repository still exposes
        # the revision separately for conditional command validation.
        return self.repository.get_configuration(workflow_id)

    def _source_filename(self, workflow_id: str, source_artifact_id: str) -> str:
        # Source-import metadata is deliberately not public, but the parser
        # needs a safe extension.  Resolve it inside the repository boundary.
        with self.repository.database.read_transaction() as con:
            row = con.execute(
                """SELECT s.error_details_json, a.format
                   FROM artifacts a LEFT JOIN source_imports s
                     ON s.current_artifact_id=a.artifact_id
                   WHERE a.workflow_id=? AND a.artifact_id=?""",
                (workflow_id, source_artifact_id),
            ).fetchone()
        if row is not None:
            try:
                details = json.loads(str(row["error_details_json"] or "{}"))
                if isinstance(details, Mapping):
                    metadata = details.get("metadata")
                    if isinstance(metadata, Mapping):
                        filename = self._safe_filename(str(metadata.get("filename") or ""))
                        if filename:
                            return filename
            except (ValueError, TypeError, WorkflowApplicationError):
                pass
        configured_name = str(self._configuration(workflow_id).get("source_filename") or "")
        if configured_name:
            try:
                return self._safe_filename(configured_name)
            except WorkflowApplicationError:
                pass
        format_key = str(row["format"] if row else "docx").lower()
        return f"source.{format_key if format_key in {'docx', 'xlsx'} else 'docx'}"

    @staticmethod
    def _legacy_parse_results(parsed: ParsedDocument) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in parsed.items:
            metadata = dict(item.metadata)
            doc_type = str(metadata.get("doc_type") or item.item_type or "document")
            groups.setdefault(doc_type, []).append({
                "id": item.identity_key,
                "category": item.item_type,
                "text": item.normalized_content,
                "role": item.role,
                "voice_key": item.voice_key,
                "source_locator": item.source_locator,
                "metadata": metadata,
            })
        return [{"doc_type": key, "items": items} for key, items in groups.items()]

    @staticmethod
    def _safe_filename(value: str) -> str:
        name = Path(str(value or "source.docx")).name
        if Path(name).suffix.lower() not in {".docx", ".xlsx"}:
            raise WorkflowApplicationError("UNSUPPORTED_MEDIA_TYPE", "only .docx and .xlsx sources are supported")
        return name[:256]

    @staticmethod
    def _content_length(content: Any) -> int | None:
        if isinstance(content, (bytes, bytearray, memoryview)):
            return len(content)
        return None

    @staticmethod
    def _as_stream(content: BinaryIO | Iterable[bytes] | bytes | bytearray | memoryview) -> BinaryIO | Iterable[bytes]:
        if isinstance(content, (bytes, bytearray, memoryview)):
            return io.BytesIO(bytes(content))
        return content

    @staticmethod
    def _ensure_provider_ready(provider: TTSProviderPort) -> None:
        if getattr(provider, "backend", object()) is None and not bool(getattr(provider, "allow_real", True)):
            raise ProviderCapabilityError("provider is not enabled for real external calls")


__all__ = ["WorkflowApplicationError", "WorkflowApplicationService"]
