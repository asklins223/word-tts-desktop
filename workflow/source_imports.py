"""Durable source-import sessions and single-writer generations."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterable, Mapping

from .artifact_store import ArtifactStore, ArtifactStoreError, BlobInfo, StagedFile
from .data_safety import redact_public_json
from .domain import canonical_json, content_hash, new_id, utc_now
from .repositories import ConflictError, NotFoundError, RepositoryError
from .security import OneTimeTicketManager, TicketError


class SourceImportError(RepositoryError):
    code = "SOURCE_NOT_AVAILABLE"


def _is_expired(value: Any) -> bool:
    try:
        expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _validate_sha256(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value.lower())
    ):
        raise SourceImportError("expected_sha256 must be a SHA-256 digest", code="VALIDATION_ERROR")


@dataclass(frozen=True)
class WriterGrant:
    token: str
    source_import_id: str
    generation: int
    lease_id: str
    fencing_token: int
    state_version: int
    expires_at: str


def _public_generation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source_import_id": str(row["source_import_id"]),
        "workflow_id": str(row["workflow_id"]),
        "generation": int(row["generation"]),
        "status": str(row["status"]),
        "state_version": int(row["state_version"]),
        "received_size_bytes": int(row["received_size_bytes"]),
        "actual_size_bytes": row["actual_size_bytes"],
        "actual_sha256": row["actual_sha256"],
        "source_artifact_id": row["source_artifact_id"],
        "error_code": row["error_code"],
        "expires_at": row["expires_at"],
        "updated_at": row["updated_at"],
    }


class SourceImportService:
    def __init__(
        self,
        database,
        artifact_store: ArtifactStore,
        *,
        ticket_manager: OneTimeTicketManager | None = None,
        max_ttl_seconds: int = 3600,
    ) -> None:
        self.database = database
        self.artifact_store = artifact_store
        self.tickets = ticket_manager or OneTimeTicketManager(max_ttl_seconds=max_ttl_seconds)
        self.max_ttl_seconds = max(1, int(max_ttl_seconds))
        self._grants: dict[str, WriterGrant] = {}
        self._grant_lock = threading.Lock()

    def create_import(
        self,
        workflow_id: str,
        *,
        metadata: Mapping[str, Any],
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
        content_type: str | None = None,
        request_key: str | None = None,
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        if expected_size_bytes is not None and expected_size_bytes < 0:
            raise SourceImportError("expected_size_bytes must be non-negative", code="VALIDATION_ERROR")
        _validate_sha256(expected_sha256)
        request_key = request_key or new_id("source-request")
        source_import_id = new_id("import")
        generation_id = new_id("generation")
        now = utc_now()
        expires_at = self._expiry(ttl_seconds)
        safe_metadata = redact_public_json(metadata)
        metadata_value = dict(safe_metadata) if isinstance(safe_metadata, Mapping) else {}
        if content_type:
            metadata_value.setdefault("content_type", content_type)
        metadata_hash = content_hash(metadata_value)
        staging_key = f"staging/{source_import_id}/1.part"
        with self.database.transaction() as con:
            if con.execute("SELECT 1 FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone() is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            try:
                con.execute(
                    """INSERT INTO source_imports(
                        source_import_id, workflow_id, request_key, metadata_hash,
                        current_generation, current_status, current_artifact_id,
                        expires_at, error_code, error_details_json, created_at,
                        updated_at, completed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_import_id, workflow_id, request_key, metadata_hash, 1,
                     "CREATED", None, expires_at, None,
                     canonical_json({"metadata": metadata_value}), now, now, None),
                )
                con.execute(
                    """INSERT INTO source_import_generations(
                        source_import_generation_id, source_import_id, workflow_id,
                        generation, staging_key, expected_size_bytes, expected_sha256,
                        received_size_bytes, actual_size_bytes, actual_sha256,
                        writer_lease_id, writer_fencing_token, state_version,
                        source_artifact_id, status, expires_at, error_code,
                        error_details_json, created_at, updated_at, completed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (generation_id, source_import_id, workflow_id, 1, staging_key,
                     expected_size_bytes, expected_sha256.lower() if expected_sha256 else None,
                     0, None, None, None, None, 0, None, "CREATED", expires_at,
                     None, None, now, now, None),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"source import cannot be created: {exc}") from exc
        return self.get_import(source_import_id)

    def create_generation(
        self,
        source_import_id: str,
        *,
        expected_state_version: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        generation_id = new_id("generation")
        now = utc_now()
        expires_at = self._expiry(ttl_seconds)
        with self.database.transaction() as con:
            parent = con.execute(
                "SELECT * FROM source_imports WHERE source_import_id=?",
                (source_import_id,),
            ).fetchone()
            if parent is None:
                raise NotFoundError(f"source import does not exist: {source_import_id}")
            if parent["current_status"] not in {"FAILED", "ABORTED", "EXPIRED"}:
                raise ConflictError(
                    f"source import is {str(parent['current_status']).lower()}; a new generation is only allowed after a failed, aborted or expired generation",
                    code="STATE_CONFLICT",
                )
            if int(parent["current_generation"]) < 1:
                raise SourceImportError("source import has no current generation")
            current_generation = con.execute(
                "SELECT state_version FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (source_import_id, parent["current_generation"]),
            ).fetchone()
            if current_generation is None or int(current_generation["state_version"]) != expected_state_version:
                raise ConflictError("source import generation changed while creating a new generation")
            # A ready generation remains immutable; a retry gets a fresh child.
            generation = int(con.execute(
                "SELECT COALESCE(MAX(generation),0)+1 FROM source_import_generations WHERE source_import_id=?",
                (source_import_id,),
            ).fetchone()[0])
            if expected_size_bytes is not None and expected_size_bytes < 0:
                raise SourceImportError("expected_size_bytes must be non-negative", code="VALIDATION_ERROR")
            _validate_sha256(expected_sha256)
            generation_row = (
                generation_id, source_import_id, parent["workflow_id"], generation,
                f"staging/{source_import_id}/{generation}.part", expected_size_bytes,
                expected_sha256.lower() if expected_sha256 else None, 0, None, None,
                None, None, 0, None, "CREATED", expires_at, None, None, now, now, None,
            )
            try:
                con.execute(
                    """INSERT INTO source_import_generations(
                        source_import_generation_id, source_import_id, workflow_id,
                        generation, staging_key, expected_size_bytes, expected_sha256,
                        received_size_bytes, actual_size_bytes, actual_sha256,
                        writer_lease_id, writer_fencing_token, state_version,
                        source_artifact_id, status, expires_at, error_code,
                        error_details_json, created_at, updated_at, completed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    generation_row,
                )
                updated = con.execute(
                    """UPDATE source_imports SET current_generation=?, current_status='CREATED',
                        current_artifact_id=NULL, expires_at=?, error_code=NULL,
                        updated_at=?, completed_at=NULL
                        WHERE source_import_id=? AND current_generation=?""",
                    (generation, expires_at, now, source_import_id, int(parent["current_generation"])),
                )
                if updated.rowcount != 1:
                    raise ConflictError("source import current generation changed")
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"source generation cannot be created: {exc}") from exc
            result = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_generation_id=?",
                (generation_id,),
            ).fetchone()
            return _public_generation(result)

    def acquire_writer(
        self,
        source_import_id: str,
        generation: int,
        *,
        expected_state_version: int,
        ttl_seconds: int = 300,
    ) -> WriterGrant:
        now = utc_now()
        ticket_ttl = min(max(1, int(ttl_seconds)), self.max_ttl_seconds)
        expires_at = self._expiry(ticket_ttl)
        lease_id = new_id("source-writer")
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (source_import_id, generation),
            ).fetchone()
            if row is None:
                raise NotFoundError("source generation does not exist")
            if int(row["state_version"]) != expected_state_version:
                raise ConflictError("source generation state_version is stale")
            if row["status"] in {"READY", "ABORTED", "EXPIRED"}:
                raise ConflictError(f"source generation is {row['status'].lower()}")
            if row["writer_lease_id"] and str(row["expires_at"]) > now:
                raise ConflictError("source generation already has a writer")
            old_token = int(row["writer_fencing_token"] or 0)
            fencing_token = old_token + 1
            updated = con.execute(
                """UPDATE source_import_generations SET status='RECEIVING',
                    writer_lease_id=?, writer_fencing_token=?, state_version=state_version+1,
                    expires_at=?, updated_at=?
                    WHERE source_import_id=? AND generation=? AND state_version=?""",
                (lease_id, fencing_token, expires_at, now, source_import_id, generation, expected_state_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("source generation changed while acquiring writer")
            new_version = expected_state_version + 1
        token, _ticket_expiry = self.tickets.issue(
            action="source-write",
            resource_id=f"{source_import_id}:{generation}",
            audience="internal-source-writer",
            ttl_seconds=ticket_ttl,
        )
        grant = WriterGrant(token, source_import_id, generation, lease_id, fencing_token, new_version, expires_at)
        with self._grant_lock:
            self._grants[token] = grant
        return grant

    def write_generation(
        self,
        source_import_id: str,
        generation: int,
        content: BinaryIO | Iterable[bytes],
        *,
        grant: str,
        format: str = "bin",
    ) -> dict[str, Any]:
        with self._grant_lock:
            writer = self._grants.get(grant)
        if writer is None or writer.source_import_id != source_import_id or writer.generation != generation:
            raise SourceImportError("source write grant is invalid", code="UNAUTHORIZED")
        try:
            self.tickets.consume(
                grant,
                action="source-write",
                resource_id=f"{source_import_id}:{generation}",
                audience="internal-source-writer",
            )
        except TicketError as exc:
            raise SourceImportError(str(exc), code="UNAUTHORIZED") from exc
        with self._grant_lock:
            self._grants.pop(grant, None)

        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (source_import_id, generation),
            ).fetchone()
        if row is None:
            raise NotFoundError("source generation does not exist")
        if row["writer_lease_id"] != writer.lease_id or int(row["writer_fencing_token"] or 0) != writer.fencing_token:
            raise SourceImportError("source writer lease is stale", code="STALE_ATTEMPT")
        if row["status"] != "RECEIVING":
            raise ConflictError(f"source generation is {row['status'].lower()}")
        # The database lease is authoritative in addition to the in-memory
        # one-time ticket.  This closes the small clock-domain gap where a
        # monotonic ticket could still be valid after the persisted wall-clock
        # generation lease has expired.
        if _is_expired(row["expires_at"]):
            raise SourceImportError("source writer lease has expired", code="STALE_ATTEMPT")
        try:
            staged = self.artifact_store.stage_stream(
                content,
                expected_size=row["expected_size_bytes"],
                expected_sha256=row["expected_sha256"],
            )
            blob = self.artifact_store.promote(staged, format=format)
            return self._commit_ready(row, writer, blob)
        except SourceImportError:
            # A lease can expire or be fenced while a large stream is being
            # staged. Do not rewrite that race as a generic persistence
            # ambiguity; recovery owns the stale generation and the promoted
            # blob remains eligible for orphan GC.
            raise
        except ArtifactStoreError as exc:
            self._mark_failed(row, writer, exc.code, str(exc))
            raise SourceImportError(str(exc), code=exc.code) from exc
        except Exception as exc:
            self._mark_failed(row, writer, "PERSISTENCE_AMBIGUOUS", str(exc))
            raise

    def _commit_ready(self, row: sqlite3.Row, writer: WriterGrant, blob: BlobInfo) -> dict[str, Any]:
        now = utc_now()
        artifact_id = new_id("artifact")
        blob_id = new_id("blob")
        with self.database.transaction() as con:
            current = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (row["source_import_id"], row["generation"]),
            ).fetchone()
            if current is None:
                raise SourceImportError("source generation no longer exists", code="STALE_ATTEMPT")
            if (
                current["status"] != "RECEIVING"
                or int(current["state_version"]) != writer.state_version
                or current["writer_lease_id"] != writer.lease_id
                or int(current["writer_fencing_token"] or 0) != writer.fencing_token
                or _is_expired(current["expires_at"])
            ):
                raise SourceImportError("source writer lease became stale or expired", code="STALE_ATTEMPT")
            # The caller's pre-staging row can be arbitrarily old for a large
            # upload; commit against the authoritative second read.
            row = current
            existing_blob = con.execute(
                "SELECT * FROM artifact_blobs WHERE sha256=?",
                (blob.sha256,),
            ).fetchone()
            if existing_blob is None:
                con.execute(
                    """INSERT INTO artifact_blobs(
                        blob_id, sha256, size_bytes, format, storage_key,
                        lifecycle_state, verified_at, created_at, deleted_at
                    ) VALUES (?,?,?,?,?,?,?,?,NULL)""",
                    (blob_id, blob.sha256, blob.size_bytes, blob.format, blob.storage_key,
                     "READY", now, now),
                )
                blob_id = str(blob_id)
            else:
                if int(existing_blob["size_bytes"]) != blob.size_bytes or existing_blob["storage_key"] != blob.storage_key:
                    raise SourceImportError("existing Blob fingerprint conflicts", code="ARTIFACT_INVALID")
                blob_id = str(existing_blob["blob_id"])
            con.execute(
                """INSERT INTO artifacts(
                    artifact_id, workflow_id, item_id, step_id, attempt_id,
                    work_unit_id, work_unit_segment_id, source_import_id,
                    source_import_generation, source_import_generation_id, blob_id,
                    staging_ref, artifact_type, sha256, size_bytes, format,
                    producer, producer_version, verified, verified_at,
                    lifecycle_state, schema_version, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (artifact_id, row["workflow_id"], None, None, None, None, None,
                 row["source_import_id"], row["generation"], row["source_import_generation_id"],
                 blob_id, None, "source", blob.sha256, blob.size_bytes, blob.format,
                 "source_import", "1", 1, now, "READY", "1", now, now),
            )
            updated = con.execute(
                """UPDATE source_import_generations SET status='READY',
                    received_size_bytes=?, actual_size_bytes=?, actual_sha256=?,
                    source_artifact_id=?, writer_lease_id=NULL, state_version=state_version+1,
                    completed_at=?, updated_at=?
                    WHERE source_import_id=? AND generation=? AND state_version=?
                      AND writer_lease_id=? AND writer_fencing_token=? AND expires_at>?""",
                (blob.size_bytes, blob.size_bytes, blob.sha256, artifact_id, now, now,
                 row["source_import_id"], row["generation"], writer.state_version,
                 writer.lease_id, writer.fencing_token, utc_now()),
            )
            if updated.rowcount != 1:
                raise SourceImportError("source writer lease became stale or expired", code="STALE_ATTEMPT")
            parent_updated = con.execute(
                """UPDATE source_imports SET current_status='READY', current_artifact_id=?,
                    updated_at=?, completed_at=? WHERE source_import_id=? AND current_generation=?""",
                (artifact_id, now, now, row["source_import_id"], row["generation"]),
            )
            if parent_updated.rowcount != 1:
                raise SourceImportError("source import current projection changed", code="PERSISTENCE_ERROR")
            workflow_updated = con.execute(
                """UPDATE workflows SET source_artifact_id=?, state_version=state_version+1,
                    updated_at=? WHERE workflow_id=? AND (source_artifact_id IS NULL OR source_artifact_id=?)""",
                (artifact_id, now, row["workflow_id"], artifact_id),
            )
            if workflow_updated.rowcount != 1:
                raise SourceImportError("workflow source binding changed", code="CONTENT_CONFLICT")
            result = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (row["source_import_id"], row["generation"]),
            ).fetchone()
            return _public_generation(result)

    def _mark_failed(self, row: sqlite3.Row, writer: WriterGrant, code: str, message: str) -> None:
        now = utc_now()
        try:
            with self.database.transaction() as con:
                parent = con.execute(
                    "SELECT error_details_json FROM source_imports WHERE source_import_id=?",
                    (row["source_import_id"],),
                ).fetchone()
                details: dict[str, Any] = {}
                if parent is not None:
                    try:
                        existing = json.loads(str(parent["error_details_json"] or "{}"))
                        if isinstance(existing, Mapping):
                            details = dict(existing)
                    except (TypeError, json.JSONDecodeError):
                        details = {}
                safe_message = redact_public_json(message)
                details["message"] = (safe_message if isinstance(safe_message, str) else "source import failed")[:1000]
                details["error_code"] = code
                details_json = canonical_json(redact_public_json(details))
                updated = con.execute(
                    """UPDATE source_import_generations SET status='FAILED', error_code=?,
                        error_details_json=?, writer_lease_id=NULL, state_version=state_version+1,
                        completed_at=?, updated_at=?
                        WHERE source_import_id=? AND generation=? AND state_version=?
                          AND writer_lease_id=? AND writer_fencing_token=?""",
                    (code, details_json, now, now,
                     row["source_import_id"], row["generation"], writer.state_version,
                     writer.lease_id, writer.fencing_token),
                )
                if updated.rowcount:
                    con.execute(
                        """UPDATE source_imports SET current_status='FAILED', error_code=?,
                            error_details_json=?, updated_at=?, completed_at=?
                            WHERE source_import_id=? AND current_generation=?""",
                        (code, details_json, now, now,
                         row["source_import_id"], row["generation"]),
                    )
        except Exception:
            # The original exception remains the useful signal.  Recovery will
            # find the stale RECEIVING generation by expiry and fencing token.
            return

    def abort(self, source_import_id: str, *, expected_state_version: int) -> dict[str, Any]:
        with self.database.transaction() as con:
            parent = con.execute(
                "SELECT * FROM source_imports WHERE source_import_id=?",
                (source_import_id,),
            ).fetchone()
            if parent is None:
                raise NotFoundError("source import does not exist")
            row = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (source_import_id, parent["current_generation"]),
            ).fetchone()
            if row is None:
                raise SourceImportError("current source generation is missing")
            if int(row["state_version"]) != expected_state_version:
                raise ConflictError("source generation state_version is stale")
            if row["status"] == "READY":
                raise ConflictError("READY source generation is immutable")
            now = utc_now()
            con.execute(
                "UPDATE source_import_generations SET status='ABORTED', writer_lease_id=NULL, state_version=state_version+1, completed_at=?, updated_at=? WHERE source_import_id=? AND generation=? AND state_version=?",
                (now, now, source_import_id, row["generation"], expected_state_version),
            )
            con.execute(
                "UPDATE source_imports SET current_status='ABORTED', error_code='USER_CANCELLED', updated_at=?, completed_at=? WHERE source_import_id=? AND current_generation=?",
                (now, now, source_import_id, row["generation"]),
            )
            result = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (source_import_id, row["generation"]),
            ).fetchone()
            return _public_generation(result)

    def get_import(self, source_import_id: str) -> dict[str, Any]:
        with self.database.read_transaction() as con:
            parent = con.execute("SELECT * FROM source_imports WHERE source_import_id=?", (source_import_id,)).fetchone()
            if parent is None:
                raise NotFoundError("source import does not exist")
            generation = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (source_import_id, parent["current_generation"]),
            ).fetchone()
            if generation is None:
                raise SourceImportError("current source generation is missing")
            return {
                "source_import_id": source_import_id,
                "workflow_id": parent["workflow_id"],
                "staging_generation": int(parent["current_generation"]),
                "status": parent["current_status"],
                "state_version": int(generation["state_version"]),
                "received_size_bytes": int(generation["received_size_bytes"]),
                "actual_size_bytes": generation["actual_size_bytes"],
                "actual_sha256": generation["actual_sha256"],
                "source_artifact_id": generation["source_artifact_id"],
                "error_code": parent["error_code"],
                "expires_at": parent["expires_at"],
                "updated_at": parent["updated_at"],
            }

    def get_generation(self, source_import_id: str, generation: int) -> dict[str, Any]:
        with self.database.read_transaction() as con:
            row = con.execute(
                "SELECT * FROM source_import_generations WHERE source_import_id=? AND generation=?",
                (source_import_id, generation),
            ).fetchone()
            if row is None:
                raise NotFoundError("source generation does not exist")
            return _public_generation(row)

    def _expiry(self, seconds: int) -> str:
        from datetime import datetime, timedelta, timezone

        ttl = min(max(1, int(seconds)), self.max_ttl_seconds)
        return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
