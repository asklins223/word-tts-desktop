"""Server-owned workspace projections for the desktop workflow UI.

The domain snapshot is intentionally small and stable.  This module joins the
durable snapshot, item, attempt and Artifact facts into the richer projection
used by a workbench.  It is kept on the backend so a renderer cannot infer a
command from a stale status enum or from a guessed file name.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
from collections import defaultdict
from pathlib import PurePath
from typing import Any, Mapping

from .domain import content_hash
from .repositories import NotFoundError, _snapshot_from_connection


RETRY_SCOPES = {"NONE", "WORKFLOW", "ITEMS"}
PROVIDER_STATUSES = {"UNKNOWN", "READY", "LOGIN_REQUIRED", "EXPIRED", "UNAVAILABLE", "DISABLED"}
KNOWN_ITEM_STATUSES = {
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "AMBIGUOUS",
    "CANCELLED", "SKIPPED", "UNRESOLVED",
}

# The workspace is a frequently refreshed UI projection.  Keep small text
# inline for fast rendering, but make large content addressable so a restart
# or a list refresh never duplicates an entire document in the renderer.
WORKSPACE_INLINE_CONTENT_LIMIT = 16 * 1024
WORKSPACE_CONTENT_DETAIL_LIMIT = 64 * 1024

_ITEM_METADATA_ALLOWLIST = {
    "category", "doc_type", "question_type", "section", "sub_type_code",
    "material_source", "language", "sheet_name", "sheet_index", "row",
    "column", "entry_number", "sentence_number", "number", "tags",
    "operation_id", "scope_row_id", "projection", "parser_version",
    "normalization_version", "voice", "voice_gender", "type_path",
    "type_hierarchy",
    "_workflow_skip_reason",
}


def item_content_id(workflow_id: str, item_id: str, stored_content_hash: str) -> str:
    """Return a stable opaque identifier bound to workflow/item/content facts."""

    digest = hashlib.sha256(
        f"{workflow_id}:{item_id}:{stored_content_hash}".encode("utf-8")
    ).hexdigest()
    return f"content-{digest[:32]}"


def _safe_metadata_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _clean_text(value, 512)
    if depth >= 2:
        return None
    if isinstance(value, (list, tuple)):
        return [
            clean
            for item in list(value)[:32]
            if (clean := _safe_metadata_value(item, depth=depth + 1)) is not None
        ]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            clean = _safe_metadata_value(item, depth=depth + 1)
            if clean is not None:
                result[str(key)[:128]] = clean
        return result
    return None


def _safe_item_metadata(raw: Any) -> tuple[dict[str, Any], bool]:
    parsed = _json_object(raw)
    result: dict[str, Any] = {}
    for key, value in sorted(parsed.items(), key=lambda entry: str(entry[0])):
        if str(key) not in _ITEM_METADATA_ALLOWLIST:
            continue
        clean = _safe_metadata_value(value)
        if clean is not None:
            result[str(key)] = clean
    serialized_size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if serialized_size <= 4096:
        return result, False

    # Keep the response bounded, but report the loss explicitly to the
    # workspace builder.  Silently dropping metadata would make the review
    # projection look complete while hiding parser facts from the user.
    bounded: dict[str, Any] = {}
    for key, value in result.items():
        candidate = {**bounded, key: value}
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 4096:
            continue
        bounded = candidate
    return bounded, True


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value)) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _clean_text(value: Any, limit: int = 2000) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] if text else None


def _safe_basename(value: Any, fallback: str) -> str:
    raw = str(value or "").replace("\\", "/")
    name = PurePath(raw).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    if len(name) <= 256:
        return name or fallback
    suffix = PurePath(name).suffix.lower()
    if suffix in {".docx", ".xlsx"}:
        max_stem_length = max(1, 256 - len(suffix))
        return f"{PurePath(name).stem[:max_stem_length]}{suffix}"
    return name[:256] or fallback


def _format(value: Any) -> str:
    return str(value or "").lower().lstrip(".") or "bin"


def _mime_type(fmt: str, artifact_type: str) -> str:
    if artifact_type == "export-zip" or fmt == "zip":
        return "application/zip"
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "flac": "audio/flac",
        "json": "application/json",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(fmt, "application/octet-stream")


DELIVERABLE_AUDIO_FORMAT = "mp3"
DELIVERABLE_AUDIO_MIME = "audio/mpeg"


def _normalized_format(value: Any) -> str | None:
    raw = str(value or "").strip().lower().lstrip(".")
    return raw or None


def _positive_size(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    try:
        size = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return size if size > 0 else None


def artifact_blob_facts_match(
    *,
    artifact_format: Any,
    blob_format: Any,
    artifact_sha256: Any,
    blob_sha256: Any,
    artifact_size_bytes: Any,
    blob_size_bytes: Any,
) -> bool:
    """Require the Artifact row and its Blob row to describe one byte object."""

    artifact_fmt = _normalized_format(artifact_format)
    blob_fmt = _normalized_format(blob_format)
    artifact_sha = str(artifact_sha256 or "").strip().lower()
    blob_sha = str(blob_sha256 or "").strip().lower()
    if (
        not artifact_fmt
        or not blob_fmt
        or artifact_fmt != blob_fmt
        or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha)
        or artifact_sha != blob_sha
    ):
        return False
    artifact_size = _positive_size(artifact_size_bytes)
    blob_size = _positive_size(blob_size_bytes)
    return artifact_size is not None and artifact_size == blob_size


def _number(value: Any, *, integer: bool = True) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(round(number)) if integer else number


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read both sqlite3.Row and ordinary mappings without leaking internals."""

    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _stored_configuration(row: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    config = _json_object(_row_value(row, "configuration_snapshot"))
    revision = _number(config.get("_workflow_configuration_revision"))
    if revision is None or int(revision) < 1:
        # Workflows created before the workspace projection have a durable
        # configuration hash and draft revision but no separate projection
        # marker.  Their first saved configuration is revision 1; later
        # patches are assigned the next saved revision.  This is deliberately
        # not the draft_revision field itself.
        revision = max(1, int(_row_value(row, "draft_revision") or 0) + 1)
    config.pop("_workflow_configuration_revision", None)
    return config, int(revision)


def _source_filename(con: Any, workflow_id: str, config: Mapping[str, Any]) -> str:
    configured = _safe_basename(config.get("source_filename"), "")
    if configured:
        return configured
    row = con.execute(
        """SELECT error_details_json FROM source_imports
           WHERE workflow_id=? ORDER BY updated_at DESC LIMIT 1""",
        (workflow_id,),
    ).fetchone()
    metadata = _json_object(row["error_details_json"] if row is not None else None).get("metadata")
    if isinstance(metadata, Mapping):
        filename = _safe_basename(metadata.get("filename"), "")
        if filename:
            return filename
    return "未命名文档.docx"


def _configuration_projection(
    row: Mapping[str, Any],
    config: Mapping[str, Any],
    revision: int,
    *,
    frozen: bool,
) -> dict[str, Any]:
    """Return only the effective, non-secret generation configuration."""

    def text(name: str) -> str | None:
        value = config.get(name)
        return str(value).strip() if value is not None and str(value).strip() else None

    def numeric(name: str) -> int | None:
        value = _number(config.get(name))
        return int(value) if value is not None else None

    role_voices: dict[str, str | None] = {}
    raw_role_voices = config.get("role_voices")
    if isinstance(raw_role_voices, Mapping):
        for key, value in list(raw_role_voices.items())[:256]:
            role_voices[str(key)[:256]] = str(value).strip() if value is not None and str(value).strip() else None

    role_configs: dict[str, dict[str, int | None]] = {}
    raw_role_configs = config.get("role_configs")
    if isinstance(raw_role_configs, Mapping):
        for key, value in list(raw_role_configs.items())[:256]:
            if not isinstance(value, Mapping):
                continue
            role_configs[str(key)[:256]] = {
                "rate": _number(value.get("rate")),
                "pitch": _number(value.get("pitch")),
                "volume": _number(value.get("volume")),
            }

    generation_mode = text("generation_mode")
    if generation_mode not in {"composite_cut", "single_segment"}:
        generation_mode = "composite_cut"
    effective = {
        "provider": text("provider") or "xunfei",
        "generation_mode": generation_mode,
        "format": text("format") or "mp3",
        "quality": text("quality"),
        "preview": bool(config.get("preview", False)),
        "preview_limit": numeric("preview_limit") if config.get("preview") else None,
        "rate": numeric("rate"),
        "pitch": numeric("pitch"),
        "volume": numeric("volume"),
        "default_female_voice": text("default_female_voice"),
        "default_male_voice": text("default_male_voice"),
        "role_voices": role_voices,
        "role_configs": role_configs,
    }
    source_priority: dict[str, str] = {
        "provider": "GLOBAL",
        "generation_mode": "GLOBAL",
        "format": "GLOBAL",
        "quality": "GLOBAL",
        "preview": "GLOBAL",
        "preview_limit": "GLOBAL",
        "rate": "GLOBAL",
        "pitch": "GLOBAL",
        "volume": "GLOBAL",
        "default_female_voice": "GLOBAL",
        "default_male_voice": "GLOBAL",
        "role_voices": "ROLE",
        "role_configs": "ROLE",
    }
    frozen_fields: list[str] = []
    if frozen:
        frozen_fields = [
            "provider", "generation_mode", "format", "quality", "preview",
            "rate", "pitch", "volume", "default_female_voice",
            "default_male_voice", "role_voices", "role_configs",
            "item_content", "item_role", "item_voice_key", "item_scope",
        ]
    return {
        "configuration_revision": int(revision),
        "configuration_hash": str(_row_value(row, "configuration_hash") or ""),
        "effective": effective,
        "source_priority": source_priority,
        "frozen_fields": frozen_fields,
    }


def _target_for_item(item_id: str, step_id: str | None) -> dict[str, str] | None:
    if not item_id or not step_id:
        return None
    return {"target_type": "ITEM", "step_id": str(step_id), "item_id": str(item_id)}


def _target_for_unit(work_unit_id: str | None) -> dict[str, str] | None:
    if not work_unit_id:
        return None
    return {"target_type": "WORK_UNIT", "work_unit_id": str(work_unit_id)}


def _action(
    kind: str,
    action_type: str,
    *,
    enabled: bool,
    reason: str | None = None,
    target: Mapping[str, Any] | None = None,
    expected_state_version: int | None = None,
    expected_target_state_version: int | None = None,
    expected_group_state_version: int | None = None,
    safe_to_retry: bool = False,
    retry_scope: str = "NONE",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "type": action_type,
        "enabled": bool(enabled),
        "reason": reason if not enabled else None,
        "target": dict(target) if isinstance(target, Mapping) else None,
        "expected_state_version": expected_state_version if kind == "SERVICE" else None,
        "expected_target_state_version": expected_target_state_version if kind == "SERVICE" else None,
        "expected_group_state_version": expected_group_state_version if kind == "SERVICE" else None,
        "safe_to_retry": bool(safe_to_retry),
        "retry_scope": retry_scope if retry_scope in RETRY_SCOPES else "NONE",
    }


def _blocker(
    code: str,
    title: str,
    message: str,
    *,
    severity: str,
    affected_item_ids: list[str] | None = None,
    retryable: bool = False,
    safe_to_retry: bool = False,
    retry_scope: str = "NONE",
    requires_reconcile: bool = False,
    recovery_action: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "message": message,
        "severity": severity,
        "affected_item_ids": sorted({str(value) for value in (affected_item_ids or []) if value}),
        "retryable": bool(retryable),
        "safe_to_retry": bool(safe_to_retry),
        "retry_scope": retry_scope if retry_scope in RETRY_SCOPES else "NONE",
        "requires_reconcile": bool(requires_reconcile),
        "recovery_action": dict(recovery_action) if isinstance(recovery_action, Mapping) else None,
    }


def build_workflow_workspace(
    repository: Any,
    workflow_id: str,
    *,
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a consistent, safe, UI-facing workspace from one DB snapshot."""

    capabilities = dict(capabilities or {})
    with repository.database.read_transaction() as con:
        snapshot = _snapshot_from_connection(con, workflow_id)
        workflow_row = con.execute(
            "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if workflow_row is None:
            raise NotFoundError(f"workflow does not exist: {workflow_id}")
        is_tts_workflow = str(workflow_row["workflow_type"] or "").lower() == "tts"
        config, configuration_revision = _stored_configuration(workflow_row)
        source_filename = _source_filename(con, workflow_id, config)

        item_rows = con.execute(
            "SELECT * FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id",
            (workflow_id,),
        ).fetchall()
        step_rows = con.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY step_id",
            (workflow_id,),
        ).fetchall()
        tts_step = next((row for row in step_rows if row["step_key"] == "tts"), None)
        tts_step_id = str(tts_step["step_id"]) if tts_step is not None else None

        unit_rows = con.execute(
            """SELECT wui.item_id, wui.work_unit_id, wui.result_status AS unit_item_status,
                      wui.state_version AS unit_item_state_version, wu.status AS unit_status,
                      wu.state_version AS unit_state_version, wu.side_effect_state,
                      wu.step_id, wua.work_unit_attempt_id, wua.started_at AS attempt_started_at
               FROM work_unit_items wui
               JOIN work_units wu ON wu.workflow_id=wui.workflow_id AND wu.work_unit_id=wui.work_unit_id
               LEFT JOIN work_unit_attempts wua
                 ON wua.workflow_id=wu.workflow_id AND wua.work_unit_id=wu.work_unit_id
                AND wua.attempt_id=wu.created_by_attempt_id
               WHERE wui.workflow_id=?
               ORDER BY wui.item_id, wua.started_at DESC, wui.ordinal""",
            (workflow_id,),
        ).fetchall()
        item_units: dict[str, dict[str, Any]] = {}
        for row in unit_rows:
            item_units.setdefault(str(row["item_id"]), dict(row))

        attempt_rows = con.execute(
            """SELECT wui.item_id, sa.attempt_id, sa.status, sa.error_code,
                      sa.error_details_json, sa.state_version AS attempt_state_version,
                      sa.started_at, sa.finished_at
               FROM work_unit_items wui
               JOIN work_unit_attempts wua
                 ON wua.workflow_id=wui.workflow_id AND wua.work_unit_id=wui.work_unit_id
               JOIN step_attempts sa
                 ON sa.workflow_id=wua.workflow_id AND sa.attempt_id=wua.attempt_id
               WHERE wui.workflow_id=?
               ORDER BY wui.item_id, sa.attempt_seq DESC, sa.started_at DESC""",
            (workflow_id,),
        ).fetchall()
        attempts_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in attempt_rows:
            attempts_by_item[str(row["item_id"])].append(dict(row))

        artifact_rows = con.execute(
            """SELECT a.artifact_id, a.workflow_id, a.item_id, a.step_id,
                      a.work_unit_id, a.artifact_type, a.lifecycle_state,
                      a.format AS artifact_format, a.sha256 AS artifact_sha256,
                      a.size_bytes AS artifact_size_bytes, a.producer,
                      a.producer_version, a.verified, a.created_at, a.updated_at,
                      b.format AS blob_format, b.size_bytes AS blob_size_bytes,
                      b.sha256 AS blob_sha256, b.lifecycle_state AS blob_lifecycle_state
               FROM artifacts a
               LEFT JOIN artifact_blobs b ON b.blob_id=a.blob_id
               WHERE a.workflow_id=?
               ORDER BY a.created_at, a.artifact_id""",
            (workflow_id,),
        ).fetchall()
        artifacts_by_item: dict[str, list[str]] = defaultdict(list)
        ready_item_artifacts: dict[str, list[str]] = defaultdict(list)
        deliverable_item_artifacts: dict[str, list[str]] = defaultdict(list)
        item_by_id = {str(row["item_id"]): row for row in item_rows}
        workspace_artifacts: list[dict[str, Any]] = []
        ready_artifact_ids: set[str] = set()
        artifact_facts: dict[str, dict[str, Any]] = {}
        for row in artifact_rows:
            artifact_id = str(row["artifact_id"])
            item_id = str(row["item_id"]) if row["item_id"] is not None else None
            artifact_type = str(row["artifact_type"])
            artifact_format = _normalized_format(row["artifact_format"])
            blob_format = _normalized_format(row["blob_format"])
            facts_match = artifact_blob_facts_match(
                artifact_format=artifact_format,
                blob_format=blob_format,
                artifact_sha256=row["artifact_sha256"],
                blob_sha256=row["blob_sha256"],
                artifact_size_bytes=row["artifact_size_bytes"],
                blob_size_bytes=row["blob_size_bytes"],
            )
            fmt = blob_format if facts_match else None
            if artifact_type == "source":
                # Source bytes are intentionally stored as ``bin`` because
                # the upload header is not trusted as an artifact format.
                # The validated source filename is still authoritative for
                # the user-facing workspace/download metadata.
                source_extension = PurePath(source_filename).suffix.lower().lstrip(".")
                if facts_match and source_extension in {"docx", "xlsx"}:
                    fmt = source_extension
            if item_id:
                artifacts_by_item[item_id].append(artifact_id)
            base_ready = (
                str(row["lifecycle_state"]) == "READY"
                and bool(row["verified"])
                and str(row["blob_lifecycle_state"] or "") == "READY"
            )
            ready = base_ready and facts_match
            deliverable = (
                ready
                and artifact_type == "tts-segment"
                and artifact_format == DELIVERABLE_AUDIO_FORMAT
                and blob_format == DELIVERABLE_AUDIO_FORMAT
            )
            artifact_facts[artifact_id] = {
                "base_ready": base_ready,
                "facts_match": facts_match,
                "ready": ready,
                "deliverable": deliverable,
                "artifact_format": artifact_format,
                "blob_format": blob_format,
            }
            if ready:
                ready_artifact_ids.add(artifact_id)
            item_row = item_by_id.get(item_id or "")
            if artifact_type == "tts-segment" and item_row is not None and fmt:
                filename = f"{int(item_row['sequence']) + 1:03d}.{fmt}"
            elif artifact_type == "tts-segment":
                filename = None
            elif artifact_type == "export-zip":
                filename = f"{_safe_basename(source_filename, 'wordtts').rsplit('.', 1)[0]}_tts.zip"
            elif artifact_type == "parse-output":
                filename = f"{_safe_basename(source_filename, 'source').rsplit('.', 1)[0]}.parsed.json"
            elif artifact_type == "source":
                filename = _safe_basename(source_filename, "source")
            else:
                filename = None
            safe_size = None
            safe_sha256 = None
            if facts_match:
                safe_size = int(row["blob_size_bytes"])
                safe_sha256 = str(row["blob_sha256"]).lower()
            workspace_artifacts.append({
                "artifact_id": artifact_id,
                "workflow_id": str(row["workflow_id"]),
                "item_id": item_id,
                "step_id": str(row["step_id"]) if row["step_id"] is not None else None,
                "work_unit_id": str(row["work_unit_id"]) if row["work_unit_id"] is not None else None,
                "artifact_type": artifact_type,
                "lifecycle_state": str(row["lifecycle_state"]),
                "format": fmt,
                "extension": f".{fmt}" if fmt else None,
                "mime_type": _mime_type(fmt, artifact_type) if fmt else None,
                "size_bytes": safe_size,
                "sha256": safe_sha256,
                "verified": bool(row["verified"]),
                "filename": filename,
                "duration_ms": None,
                "producer": str(row["producer"] or ""),
                "producer_version": str(row["producer_version"] or ""),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            })

        latest_tts_by_item: dict[str, dict[str, Any]] = {}
        for artifact in workspace_artifacts:
            item_id = artifact.get("item_id")
            if artifact.get("artifact_type") != "tts-segment" or item_id is None:
                continue
            key = str(item_id)
            current = latest_tts_by_item.get(key)
            candidate_key = (str(artifact.get("created_at") or ""), str(artifact.get("artifact_id") or ""))
            current_key = (str(current.get("created_at") or ""), str(current.get("artifact_id") or "")) if current else None
            if current is None or candidate_key > current_key:
                latest_tts_by_item[key] = artifact
        for item_id, artifact in latest_tts_by_item.items():
            facts = artifact_facts.get(str(artifact["artifact_id"]), {})
            if facts.get("ready"):
                ready_item_artifacts[item_id].append(str(artifact["artifact_id"]))
            if facts.get("deliverable"):
                deliverable_item_artifacts[item_id].append(str(artifact["artifact_id"]))

        has_attempt = con.execute(
            "SELECT 1 FROM step_attempts WHERE workflow_id=? AND attempt_kind='EXECUTE' LIMIT 1", (workflow_id,)
        ).fetchone() is not None
        configuration = _configuration_projection(
            workflow_row, config, configuration_revision, frozen=has_attempt,
        )

        workspace_items: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        completed_ids: set[str] = set()
        failed_ids: set[str] = set()
        cancelled_ids: set[str] = set()
        skipped_ids: set[str] = set()
        pending_ids: set[str] = set()
        unresolved_items: list[str] = []
        projection_blocked_ids: set[str] = set()
        artifact_recovery_units: dict[str, dict[str, Any]] = {}
        failed_item_rows: list[tuple[str, dict[str, Any], str | None]] = []
        for row in item_rows:
            item_id = str(row["item_id"])
            status = str(row["status"])
            item_attempts = attempts_by_item.get(item_id, [])
            latest_attempt = item_attempts[0] if item_attempts else None
            details = _json_object(latest_attempt.get("error_details_json") if latest_attempt else None)
            error_message = _clean_text(details.get("message") or details.get("user_message"))
            error_code = (
                _clean_text(latest_attempt.get("error_code"), 128) if latest_attempt else None
            )
            if error_code is None and status in {"FAILED", "AMBIGUOUS", "UNRESOLVED"}:
                error_code = _clean_text(workflow_row["last_error_code"], 128)
            unit = item_units.get(item_id) or {}
            step_id = str(unit.get("step_id") or tts_step_id or "") or None
            side_effect = str(unit.get("side_effect_state") or "NOT_STARTED")
            # TTS uncertainty is handled from local durable facts and retried
            # explicitly. Only the separate external-operation workflow keeps
            # a provider reconciliation projection.
            requires_reconcile = not is_tts_workflow and (
                status in {"AMBIGUOUS", "UNRESOLVED"}
                or (
                    side_effect in {"IN_FLIGHT", "SUBMITTED", "CONFIRMED", "AMBIGUOUS"}
                    and status != "SUCCEEDED"
                )
            )
            latest_tts = latest_tts_by_item.get(item_id)
            latest_tts_facts = artifact_facts.get(str(latest_tts.get("artifact_id")), {}) if latest_tts else {}
            has_ready_artifact = bool(latest_tts_facts.get("ready"))
            has_deliverable_artifact = bool(latest_tts_facts.get("deliverable"))
            if status not in KNOWN_ITEM_STATUSES:
                pending_ids.add(item_id)
                projection_blocked_ids.add(item_id)
                blockers.append(_blocker(
                    "ITEM_STATUS_UNKNOWN", "条目状态无法确认", "服务端返回了未知条目状态，请重新同步。",
                    severity="BLOCKING", affected_item_ids=[item_id], requires_reconcile=not is_tts_workflow,
                ))
            elif has_deliverable_artifact and status == "SUCCEEDED":
                completed_ids.add(item_id)
            elif has_ready_artifact and status == "SUCCEEDED":
                pending_ids.add(item_id)
                projection_blocked_ids.add(item_id)
                blockers.append(_blocker(
                    "ARTIFACT_FORMAT_UNSUPPORTED", "音频格式未验证",
                    "当前产物不是 MP3 或格式元数据未通过交付校验，暂不开放下载。",
                    severity="ERROR", affected_item_ids=[item_id],
                ))
            elif has_ready_artifact:
                pending_ids.add(item_id)
                projection_blocked_ids.add(item_id)
                blockers.append(_blocker(
                    "ITEM_ARTIFACT_CONFLICT", "条目状态与产物不一致",
                    "已存在已验证音频，但条目状态不是成功；已暂缓计入交付，请重新同步。",
                    severity="BLOCKING", affected_item_ids=[item_id], requires_reconcile=not is_tts_workflow,
                ))
            elif status == "SKIPPED":
                skipped_ids.add(item_id)
            elif status == "FAILED":
                failed_ids.add(item_id)
                failed_item_rows.append((item_id, dict(row), step_id))
            elif is_tts_workflow and status in {"AMBIGUOUS", "UNRESOLVED"}:
                # Startup recovery normally converts these legacy rows to
                # FAILED. Keep old projections retryable during that window.
                failed_ids.add(item_id)
                failed_item_rows.append((item_id, dict(row), step_id))
            elif status == "CANCELLED":
                cancelled_ids.add(item_id)
            elif status == "SUCCEEDED":
                # A durable item status alone is not a deliverable.  Keep the
                # item pending until a READY, verified artifact is visible so
                # the snapshot cannot advertise a false 100% completion. TTS
                # has no provider reconciliation path: expose this local gap
                # as an ordinary retry target.
                pending_ids.add(item_id)
                projection_blocked_ids.add(item_id)
                unit_id = str(unit.get("work_unit_id") or "")
                # Keep one disabled action even when the work-unit row itself
                # is gone.  That projection is still actionable information:
                # the UI can explain why no safe recovery target exists.
                if is_tts_workflow:
                    pending_ids.discard(item_id)
                    failed_ids.add(item_id)
                    failed_item_rows.append((item_id, dict(row), step_id))
                    recovery_action = _action(
                        "SERVICE", "RETRY", enabled=True,
                        reason="本地没有可交付音频，请重新生成",
                        target=_target_for_item(item_id, step_id),
                        expected_state_version=snapshot.state_version,
                        expected_target_state_version=int(row["state_version"]),
                        safe_to_retry=True, retry_scope="ITEMS",
                    )
                else:
                    artifact_recovery_units.setdefault(unit_id, unit)
                    recovery_target = _target_for_unit(unit_id or None)
                    recovery_action = _action(
                        "SERVICE", "RECONCILE", enabled=bool(recovery_target),
                        reason=None if recovery_target else "缺少可对账的工作单元",
                        target=recovery_target,
                        expected_state_version=snapshot.state_version,
                        expected_target_state_version=(
                            int(unit["unit_state_version"])
                            if unit.get("unit_state_version") is not None else None
                        ),
                    )
                if latest_tts and latest_tts_facts.get("base_ready") and not latest_tts_facts.get("facts_match"):
                    blockers.append(_blocker(
                        "ARTIFACT_METADATA_CONFLICT", "产物元数据不一致",
                        "音频产物的 Artifact 与 Blob 元数据不一致，暂不开放交付；请重新生成。",
                        severity="ERROR", affected_item_ids=[item_id],
                    ))
                else:
                    blockers.append(_blocker(
                        "ARTIFACT_MISSING_OR_UNVERIFIED", "成功条目缺少可交付产物",
                        "条目标记为成功，但服务端尚未确认对应产物可读取；请重新运行任务。",
                        severity="ERROR", affected_item_ids=[item_id], requires_reconcile=not is_tts_workflow,
                        recovery_action=recovery_action,
                    ))
            else:
                pending_ids.add(item_id)
            if requires_reconcile:
                unresolved_items.append(item_id)
            retry_scope = "NONE" if requires_reconcile else (
                "ITEMS" if status == "FAILED" or (
                    is_tts_workflow and status in {"AMBIGUOUS", "UNRESOLVED"}
                ) else "NONE"
            )
            normalized_content = str(row["normalized_content"] or "")
            content_size_bytes = len(normalized_content.encode("utf-8"))
            stored_content_hash = str(row["content_hash"])
            content_ref = None
            inline_content = normalized_content
            if content_size_bytes > WORKSPACE_INLINE_CONTENT_LIMIT:
                inline_content = None
                content_ref = {
                    "content_id": item_content_id(workflow_id, item_id, stored_content_hash),
                    "size_bytes": content_size_bytes,
                    "content_hash": stored_content_hash,
                    "max_response_bytes": WORKSPACE_CONTENT_DETAIL_LIMIT,
                }
            metadata, metadata_overflow = _safe_item_metadata(row["metadata_json"])
            skip_reason = _clean_text(metadata.pop("_workflow_skip_reason", None), 500)
            if metadata_overflow:
                blockers.append(_blocker(
                    "ITEM_METADATA_TOO_LARGE", "条目来源信息过大",
                    "条目来源信息超过工作区展示上限，已保留可展示部分；请重新同步或查看原文。",
                    severity="ERROR", affected_item_ids=[item_id],
                ))
            workspace_items.append({
                "item_id": item_id,
                "item_identity_key": str(row["item_identity_key"]),
                "sequence": int(row["sequence"]),
                "item_type": _clean_text(row["item_type"], 128) or "document",
                "normalized_content": inline_content,
                "content_ref": content_ref,
                "source_locator": _clean_text(row["source_locator"], 512),
                "metadata": metadata,
                "skip_reason": skip_reason,
                "content_hash": stored_content_hash,
                "status": status,
                "role": str(row["role"]) if row["role"] is not None else None,
                "voice_key": str(row["voice_key"]) if row["voice_key"] is not None else None,
                "attempt_count": len(item_attempts),
                "error_code": error_code,
                "user_message": error_message,
                "retry_scope": retry_scope,
                "requires_reconcile": requires_reconcile,
                "artifact_ids": artifacts_by_item.get(item_id, []),
                "updated_at": str(row["updated_at"]),
            })

        if unresolved_items:
            for item_id in sorted(set(unresolved_items)):
                unit = item_units.get(item_id) or {}
                unit_id = str(unit.get("work_unit_id") or "") or None
                action = _action(
                    "SERVICE", "RECONCILE", enabled=True,
                    target=_target_for_unit(unit_id),
                    expected_state_version=snapshot.state_version,
                    expected_target_state_version=int(unit.get("unit_state_version")) if unit.get("unit_state_version") is not None else None,
                )
                blockers.append(_blocker(
                    "SUBMISSION_AMBIGUOUS", "生成结果待核验",
                    "外部提交结果未能安全确认，不能盲目重试；请先发起对账并提交证据。",
                    severity="BLOCKING", affected_item_ids=[item_id], requires_reconcile=True,
                    recovery_action=action,
                ))

        # Workflow-level failure is useful even when an item-level attempt has
        # its own error.  Avoid duplicating the same code on every row.
        workflow_error_code = _clean_text(workflow_row["last_error_code"], 128)
        workflow_error_message = _clean_text(workflow_row["last_error_message"])
        if workflow_error_code and not unresolved_items:
            safe = workflow_error_code in {
                "TRANSIENT_PROVIDER_ERROR", "PROVIDER_RATE_LIMITED", "RESOURCE_EXHAUSTED",
            }
            blockers.append(_blocker(
                workflow_error_code, "工作流需要处理", workflow_error_message or "生成任务未完成，请查看恢复动作。",
                severity="ERROR", affected_item_ids=sorted(failed_ids), retryable=safe,
                safe_to_retry=safe, retry_scope="WORKFLOW" if safe else "NONE",
            ))

        total = len(item_rows)
        accounted = len(completed_ids) + len(failed_ids) + len(cancelled_ids) + len(skipped_ids) + len(pending_ids)
        if accounted < total:
            pending_ids.update(item_id for item_id in item_by_id if item_id not in completed_ids | failed_ids | cancelled_ids | skipped_ids)
        # The sets above are disjoint by construction except a defensive
        # unknown-status path; recalculate pending from the authoritative
        # partition to guarantee the progress invariant.
        pending_ids = set(item_by_id) - completed_ids - failed_ids - cancelled_ids - skipped_ids
        completed = len(completed_ids)
        failed = len(failed_ids)
        cancelled = len(cancelled_ids)
        skipped = len(skipped_ids)
        pending = len(pending_ids)
        progress = {
            "total": total,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "skipped": skipped,
            "pending": pending,
            "deliverable": len(deliverable_item_artifacts),
            "percent": int((100 * (completed + failed + cancelled + skipped)) // total) if total else 0,
            "deliverable_percent": int((100 * len(deliverable_item_artifacts)) // total) if total else 0,
        }

        ready_tts_item_ids = {
            item_id for item_id, artifact in latest_tts_by_item.items()
            if artifact_facts.get(str(artifact["artifact_id"]), {}).get("ready")
        }
        deliverable_tts_item_ids = set(deliverable_item_artifacts)
        deliverable_artifact_ids = {
            artifact_id for artifact_ids in deliverable_item_artifacts.values() for artifact_id in artifact_ids
        }
        delivery_segments = [
            item for item in workspace_artifacts
            if item["artifact_type"] == "tts-segment"
            and item["item_id"] is not None
            and item["artifact_id"] in deliverable_artifact_ids
            and _row_value(item_by_id.get(str(item["item_id"])), "status") == "SUCCEEDED"
        ]
        delivery_segments.sort(
            key=lambda item: (
                int(item_by_id[str(item["item_id"])]["sequence"]),
                str(item["item_id"]),
            )
        )

        zip_candidates = [
            artifact for artifact in workspace_artifacts
            if artifact["artifact_type"] == "export-zip"
            and artifact["artifact_id"] in ready_artifact_ids
            and artifact.get("format") == "zip"
            and artifact.get("mime_type") == "application/zip"
        ]
        current_delivery_artifact_ids = {
            str(item["artifact_id"]) for item in delivery_segments
        }
        expected_full_zip_id = None
        if delivery_segments:
            export_basis = [
                {
                    "item_id": str(segment["item_id"]),
                    "sequence": int(item_by_id[str(segment["item_id"])]["sequence"]),
                    "artifact_id": str(segment["artifact_id"]),
                    "sha256": str(segment["sha256"]),
                }
                for segment in delivery_segments
            ]
            export_hash = content_hash({
                "workflow_id": workflow_id,
                "segments": export_basis,
                "requested_item_ids": None,
            })
            expected_full_zip_id = f"artifact-export-{export_hash[:32]}"

        # An export is immutable, so its creation time alone cannot prove that
        # it contains the current TTS artifacts.  Prefer the content-addressed
        # full export ID used by create_export_zip.  A v2 explicitly-selected
        # subset may remain available while newer items are added, but a full
        # export created during an earlier partial run must not be exposed as
        # the current ZIP after a retry publishes a newer segment.
        zip_parent_item_ids: set[str] = set()
        selected_zip = None
        selected_parent_item_ids: set[str] = set()
        ordered_zip_candidates = sorted(
            zip_candidates,
            key=lambda artifact: (str(artifact["created_at"]), str(artifact["artifact_id"])),
            reverse=True,
        )
        for candidate in ordered_zip_candidates:
            parent_rows = con.execute(
                """SELECT DISTINCT parent.item_id, parent.artifact_id
                   FROM artifact_derivations d
                   JOIN artifacts child ON child.artifact_id=d.child_artifact_id
                   JOIN artifacts parent ON parent.artifact_id=d.parent_artifact_id
                   JOIN artifact_blobs parent_blob ON parent_blob.blob_id=parent.blob_id
                   JOIN work_items parent_item
                     ON parent_item.workflow_id=parent.workflow_id AND parent_item.item_id=parent.item_id
                   WHERE child.workflow_id=? AND child.artifact_id=?
                     AND d.relation_type='EXPORT'
                     AND parent.item_id IS NOT NULL
                     AND parent.artifact_type='tts-segment'
                     AND parent.lifecycle_state='READY' AND parent.verified=1
                     AND parent_blob.lifecycle_state='READY'
                     AND LOWER(TRIM(parent.format))='mp3'
                     AND LOWER(TRIM(parent_blob.format))='mp3'
                     AND parent.sha256=parent_blob.sha256
                     AND parent.size_bytes=parent_blob.size_bytes
                     AND parent_item.status='SUCCEEDED'""",
                (workflow_id, candidate["artifact_id"]),
            ).fetchall()
            parent_artifact_ids = {str(row["artifact_id"]) for row in parent_rows}
            parent_item_ids = {str(row["item_id"]) for row in parent_rows}
            if not parent_artifact_ids or not parent_artifact_ids.issubset(current_delivery_artifact_ids):
                continue
            is_current_full = (
                candidate["artifact_id"] == expected_full_zip_id
                and parent_artifact_ids == current_delivery_artifact_ids
            )
            if is_current_full:
                selected_zip = candidate
                selected_parent_item_ids = parent_item_ids
                break
            producer_version = str(candidate.get("producer_version") or "")
            is_explicit_ready_subset = producer_version == "2:subset-ready"
            is_current_partial_subset = (
                producer_version == "2:subset-partial"
                and parent_artifact_ids == current_delivery_artifact_ids
            )
            if selected_zip is None and (is_explicit_ready_subset or is_current_partial_subset):
                selected_zip = candidate
                selected_parent_item_ids = parent_item_ids

        zip_row = selected_zip
        if zip_row is not None:
            zip_parent_item_ids = selected_parent_item_ids
        included_item_ids = sorted(
            zip_parent_item_ids if zip_row is not None else {str(item["item_id"]) for item in delivery_segments},
            key=lambda value: (int(item_by_id[value]["sequence"]), value),
        )
        excluded_item_ids: list[str] = []
        exclusion_reasons: dict[str, str] = {}
        for row in item_rows:
            item_id = str(row["item_id"])
            if item_id in included_item_ids:
                continue
            status = str(row["status"])
            if zip_row is not None and item_id in deliverable_tts_item_ids:
                reason = "NOT_SELECTED"
            elif item_id in deliverable_tts_item_ids:
                reason = "ITEM_ARTIFACT_CONFLICT"
            elif item_id in ready_tts_item_ids:
                latest_facts = artifact_facts.get(str(latest_tts_by_item[item_id]["artifact_id"]), {})
                reason = "ARTIFACT_FORMAT_UNSUPPORTED" if latest_facts.get("facts_match") else "ARTIFACT_METADATA_CONFLICT"
            elif status == "SKIPPED":
                reason = "ITEM_SKIPPED"
            elif status in {"AMBIGUOUS", "UNRESOLVED"} and not is_tts_workflow:
                reason = "REQUIRES_RECONCILE"
            elif status in {"AMBIGUOUS", "UNRESOLVED"}:
                reason = "ITEM_FAILED"
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

        service_actions: list[dict[str, Any]] = []
        workflow_target = {"target_type": "WORKFLOW", "workflow_id": workflow_id}
        terminal = snapshot.execution_state == "TERMINAL"
        editable = (
            not terminal and snapshot.status in {"DRAFT", "ACTIVE"}
            and snapshot.control_state == "RUNNING"
            and snapshot.execution_state in {"CREATED", "PREPARING", "WAITING_RETRY", "WAITING_USER"}
        )
        service_actions.append(_action(
            "SERVICE", "PARSE", enabled=(snapshot.status == "DRAFT" and snapshot.source_artifact_id is not None),
            reason="需要先准备可用源文件" if snapshot.source_artifact_id is None else ("工作流已解析" if snapshot.status != "DRAFT" else None),
            target=workflow_target, expected_state_version=snapshot.state_version,
        ))
        service_actions.append(_action(
            "SERVICE", "SAVE_CONFIGURATION", enabled=editable,
            reason="生成尝试已开始，配置和条目修订已冻结" if not editable else None,
            target=workflow_target, expected_state_version=snapshot.state_version,
        ))
        generation_is_preparing = (
            is_tts_workflow
            and snapshot.execution_state == "PREPARING"
            and tts_step is not None
        )
        can_generate = (
            not terminal and snapshot.control_state == "RUNNING"
            and snapshot.execution_state in {"CREATED", "PREPARING", "WAITING_RETRY", "WAITING_USER"}
            and not generation_is_preparing
            and bool(item_rows) and bool(set(item_by_id) - skipped_ids)
            and not unresolved_items and not projection_blocked_ids
        )
        provider_projection = capabilities.get("provider")
        # Direct repository callers from older integrations do not know about
        # provider capability projection yet.  The versioned HTTP route always
        # supplies it, so only that explicit projection can disable GENERATE.
        provider_can_generate = True
        provider_can_start_generation = False
        if isinstance(provider_projection, Mapping):
            provider_ready = bool(provider_projection.get("ready"))
            provider_status = str(provider_projection.get("status") or "").upper()
            if provider_status not in PROVIDER_STATUSES:
                provider_status = "READY" if provider_ready else "UNKNOWN"
            provider_can_generate = bool(provider_projection.get(
                "can_generate", provider_ready
            ))
            provider_can_generate = provider_can_generate and provider_status == "READY" and provider_ready
            provider_can_start_generation = bool(provider_projection.get("can_start_generation", False))
            provider_can_start_generation = provider_can_start_generation and provider_status not in {"UNAVAILABLE", "DISABLED"}
        all_items_skipped = bool(item_rows) and not bool(set(item_by_id) - skipped_ids)
        can_generate = can_generate and (
            provider_can_generate or provider_can_start_generation or all_items_skipped
        )
        generate_reason = None
        if not can_generate:
            if terminal:
                generate_reason = "工作流已结束"
            elif snapshot.control_state != "RUNNING":
                generate_reason = f"工作流控制态为 {snapshot.control_state}"
            elif snapshot.execution_state not in {"CREATED", "PREPARING", "WAITING_RETRY", "WAITING_USER"}:
                generate_reason = f"工作流执行态为 {snapshot.execution_state}"
            elif not item_rows or not bool(set(item_by_id) - skipped_ids):
                generate_reason = "没有可生成的条目"
            elif generation_is_preparing:
                generate_reason = "生成任务已接受，正在准备执行"
            elif unresolved_items:
                generate_reason = "存在未完成的外部操作"
            elif projection_blocked_ids:
                generate_reason = "存在尚未核验或状态冲突的条目"
            elif not provider_can_generate and not provider_can_start_generation and not all_items_skipped:
                generate_reason = _clean_text(
                    provider_projection.get("reason") if isinstance(provider_projection, Mapping) else None,
                    500,
                ) or "Provider 当前不可用"
            else:
                generate_reason = "当前状态不允许生成"
        service_actions.append(_action(
            "SERVICE", "GENERATE", enabled=can_generate,
            reason=generate_reason,
            target=workflow_target, expected_state_version=snapshot.state_version,
        ))
        pause_enabled = (
            not terminal and snapshot.control_state == "RUNNING"
            and snapshot.execution_state in {"PREPARING", "RUNNING", "RECOVERING"}
            and bool(capabilities.get("supports_pause", False))
        )
        service_actions.append(_action(
            "SERVICE", "PAUSE", enabled=pause_enabled,
            reason="当前运行时不支持协作式暂停" if not bool(capabilities.get("supports_pause", False)) else "当前没有可暂停的运行任务",
            target=workflow_target, expected_state_version=snapshot.state_version,
        ))
        resume_enabled = (
            not terminal and snapshot.control_state in {"PAUSED", "PAUSE_REQUESTED"}
            and bool(capabilities.get("supports_resume", False))
        )
        service_actions.append(_action(
            "SERVICE", "RESUME", enabled=resume_enabled,
            reason="当前运行时不支持恢复" if not bool(capabilities.get("supports_resume", False)) else "工作流未处于暂停状态",
            target=workflow_target, expected_state_version=snapshot.state_version,
        ))
        cancel_enabled = not terminal and snapshot.control_state != "TERMINATED"
        service_actions.append(_action(
            "SERVICE", "CANCEL", enabled=cancel_enabled,
            reason="工作流已进入终态" if not cancel_enabled else None,
            target=workflow_target, expected_state_version=snapshot.state_version,
        ))
        export_enabled = (
            terminal
            and snapshot.result_status in {"SUCCEEDED", "PARTIAL_SUCCESS"}
            and bool(delivery_segments)
            and zip_row is None
        )
        export_reason = None
        if not export_enabled:
            if zip_row is not None:
                export_reason = "已有可下载的 ZIP Artifact"
            elif not terminal:
                export_reason = "工作流尚未结束"
            elif snapshot.result_status not in {"SUCCEEDED", "PARTIAL_SUCCESS"}:
                export_reason = "当前工作流没有可整理的成功结果"
            else:
                export_reason = "没有通过核验的音频可纳入 ZIP"
        service_actions.append(_action(
            "SERVICE", "EXPORT_ZIP", enabled=export_enabled,
            reason=export_reason,
            target=workflow_target, expected_state_version=snapshot.state_version,
        ))
        for item_id, item_row, step_id in failed_item_rows:
            unit = item_units.get(item_id) or {}
            item_requires_reconcile = not is_tts_workflow and str(item_row["status"]) in {"AMBIGUOUS", "UNRESOLVED"}
            service_actions.append(_action(
                "SERVICE", "RETRY", enabled=(
                    (item_row["status"] == "FAILED" or (
                        is_tts_workflow and (
                            item_row["status"] in {"AMBIGUOUS", "UNRESOLVED"}
                            or (item_row["status"] == "SUCCEEDED" and not ready_item_artifacts.get(item_id))
                        )
                    ))
                    and not item_requires_reconcile and not ready_item_artifacts.get(item_id)
                ),
                reason=("该条目存在未决副作用，必须先对账" if item_requires_reconcile
                        else "该条目已有已验证产物，不能覆盖交付事实" if ready_item_artifacts.get(item_id)
                        else None),
                target=_target_for_item(item_id, step_id), expected_state_version=snapshot.state_version,
                expected_target_state_version=int(item_row["state_version"]), safe_to_retry=True,
                retry_scope="ITEMS",
            ))
        # A SUCCEEDED work item without a verified artifact is a corruption or
        # garbage-collection boundary, not an ordinary retryable failure. Give
        # external workflows a read-only reconciliation command when their
        # durable work unit is still available; TTS is handled as a local
        # retryable gap above and never queries or resubmits through recovery.
        for unit in artifact_recovery_units.values():
            unit_id = str(unit.get("work_unit_id") or "") or None
            target = _target_for_unit(unit_id)
            service_actions.append(_action(
                "SERVICE", "RECONCILE", enabled=bool(target),
                reason=None if target else "成功条目缺少可对账的工作单元",
                target=target,
                expected_state_version=snapshot.state_version,
                expected_target_state_version=(
                    int(unit["unit_state_version"])
                    if unit.get("unit_state_version") is not None else None
                ),
            ))
        for item_id in sorted(set(unresolved_items)):
            unit = item_units.get(item_id) or {}
            service_actions.append(_action(
                "SERVICE", "RECONCILE", enabled=True,
                target=_target_for_unit(str(unit.get("work_unit_id") or "") or None),
                expected_state_version=snapshot.state_version,
                expected_target_state_version=int(unit["unit_state_version"]) if unit.get("unit_state_version") is not None else None,
            ))
            service_actions.append(_action(
                "SERVICE", "RESOLVE", enabled=True,
                target=_target_for_unit(str(unit.get("work_unit_id") or "") or None),
                expected_state_version=snapshot.state_version,
                expected_target_state_version=int(unit["unit_state_version"]) if unit.get("unit_state_version") is not None else None,
            ))
        service_actions.append(_action(
            "SERVICE", "ARCHIVE", enabled=(terminal and snapshot.control_state == "TERMINATED" and snapshot.status != "CLOSED"),
            reason="只有已完成且已终止的工作流可以归档" if not (terminal and snapshot.control_state == "TERMINATED" and snapshot.status != "CLOSED") else None,
            target=workflow_target, expected_state_version=snapshot.state_version,
        ))
        service_actions.append(_action(
            "SERVICE", "ABANDON", enabled=False,
            reason="服务端尚未提供可审计的放弃语义", target=workflow_target,
            expected_state_version=snapshot.state_version,
        ))
        service_actions.append(_action(
            "SERVICE", "RERUN", enabled=(terminal and snapshot.group_state_version >= 0),
            reason="仅终态工作流可以创建新运行" if not terminal else None,
            target=workflow_target, expected_state_version=None,
            expected_group_state_version=snapshot.group_state_version,
            safe_to_retry=True, retry_scope="WORKFLOW",
        ))

        ui_actions = [_action("UI", "OPEN_VIEW", enabled=True, target=workflow_target)]
        for artifact in workspace_artifacts:
            if artifact["artifact_id"] in deliverable_artifact_ids or (
                artifact["artifact_type"] == "export-zip"
                and zip_row is not None
                and artifact["artifact_id"] == zip_row["artifact_id"]
            ):
                ui_actions.append(_action(
                    "UI", "DOWNLOAD_ARTIFACT", enabled=True,
                    target={"target_type": "ARTIFACT", "artifact_id": artifact["artifact_id"]},
                ))
        if zip_row is not None:
            ui_actions.append(_action(
                "UI", "DOWNLOAD_ZIP", enabled=True,
                target={"target_type": "ARTIFACT", "artifact_id": zip_row["artifact_id"]},
            ))
        ui_actions.append(_action("UI", "RECONNECT", enabled=True, target=None))

        current_target = None
        active_target_rows = [
            row for row in unit_rows
            if str(row["unit_status"]) in {"RUNNING", "VERIFYING", "RECOVERING"}
            and str(row["unit_item_status"]) not in {"SUCCEEDED", "SKIPPED"}
        ]
        if active_target_rows:
            target_row = active_target_rows[0]
            target_item = item_by_id.get(str(target_row["item_id"]))
            current_target = {
                "item_id": str(target_row["item_id"]),
                "label": _clean_text(target_item["normalized_content"] if target_item is not None else target_row["item_id"], 160) or str(target_row["item_id"]),
                "started_at": str(target_row["attempt_started_at"] or (target_item["updated_at"] if target_item is not None else snapshot.updated_at)),
            }

        delivery = {
            "zip_artifact_id": zip_row["artifact_id"] if zip_row is not None else None,
            "zip_available": zip_row is not None,
            "included_item_ids": included_item_ids,
            "excluded_item_ids": excluded_item_ids,
            "exclusion_reasons": exclusion_reasons,
        }
        if not isinstance(provider_projection, Mapping):
            provider_projection = {
                "provider": "UNKNOWN",
                "status": "UNKNOWN",
                "ready": False,
                "can_generate": False,
                "reason": "Provider 状态尚未由当前运行时确认",
            }
        provider_status = str(provider_projection.get("status") or "").upper()
        if provider_status not in PROVIDER_STATUSES:
            provider_status = "READY" if provider_projection.get("ready") else "UNKNOWN"
        provider_ready = bool(provider_projection.get("ready")) and provider_status == "READY"
        provider_can_generate = bool(provider_projection.get(
            "can_generate", provider_ready
        )) and provider_ready
        provider_can_start_generation = bool(provider_projection.get("can_start_generation", False))
        provider_can_start_generation = provider_can_start_generation and provider_status not in {"UNAVAILABLE", "DISABLED"}
        provider = {
            "provider": _clean_text(provider_projection.get("provider"), 128) or "UNKNOWN",
            "status": provider_status,
            "ready": provider_ready,
            "reason": _clean_text(provider_projection.get("reason"), 500) or "Provider 状态未知",
            "can_generate": provider_can_generate,
            "can_start_generation": provider_can_start_generation,
        }
        return {
            "schema_version": 1,
            "snapshot": snapshot.as_dict(),
            "source_filename": source_filename,
            "progress": progress,
            "blockers": blockers,
            "available_actions": service_actions + ui_actions,
            "current_target": current_target,
            "items": workspace_items,
            "artifacts": workspace_artifacts,
            "configuration": configuration,
            "provider": provider,
            "delivery": delivery,
            "sync": {
                "state_version": snapshot.state_version,
                "last_event_id": snapshot.latest_event_id,
                "requires_resync": False,
            },
        }


__all__ = [
    "DELIVERABLE_AUDIO_FORMAT",
    "DELIVERABLE_AUDIO_MIME",
    "artifact_blob_facts_match",
    "build_workflow_workspace",
]
