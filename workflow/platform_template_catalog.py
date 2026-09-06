"""Durable catalogue for platform question and paper templates.

The external platform remains authoritative.  This module keeps only the
display-safe identifiers and scope needed to build the local cascading
selectors; it never stores browser credentials or an Authorization token.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app_paths import ensure_data_dir
from workflow.repositories import RepositoryError


PLATFORM_TEMPLATE_CATALOG_SCHEMA_VERSION = 1
PLATFORM_TEMPLATE_CATALOG_FILENAME = "platform-template-catalog.json"
PLATFORM_TEMPLATE_CATALOG_MAX_RECORDS = 20_000
PLATFORM_TEMPLATE_KINDS = frozenset({"question", "paper"})


class PlatformTemplateCatalogError(RepositoryError):
    """A safe error that can be shown by the renderer."""


def _text(value: Any, limit: int = 256) -> str:
    return str(value or "").strip()[:limit]


def _normal_key(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _first(row: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    for key in aliases:
        if row.get(key) not in (None, ""):
            return row.get(key)
    normalized = {_normal_key(key): value for key, value in row.items()}
    for key in aliases:
        value = normalized.get(_normal_key(key))
        if value not in (None, ""):
            return value
    return None


def _mapping_name(value: Mapping[str, Any]) -> str:
    return _text(_first(value, ("name", "label", "text", "title", "dictLabel", "value")))


def _mapping_id(value: Mapping[str, Any]) -> str:
    return _text(_first(value, ("id", "value", "key", "code")), 128)


def _choice(row: Mapping[str, Any], name_aliases: Sequence[str], id_aliases: Sequence[str]) -> dict[str, str] | None:
    raw = _first(row, name_aliases)
    identifier = _text(_first(row, id_aliases), 128)
    if isinstance(raw, Mapping):
        name = _mapping_name(raw)
        identifier = identifier or _mapping_id(raw)
    else:
        name = _text(raw)
    if not name or name.isdigit():
        return None
    return {"id": identifier or name, "name": name}


_SCOPE_ALIASES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "province": (
        ("provinceName", "province_name", "province", "provinceInfo", "province_info"),
        ("provinceId", "province_id", "provinceCode", "province_code"),
    ),
    "city": (
        ("cityName", "city_name", "city", "cityInfo", "city_info"),
        ("cityId", "city_id", "cityCode", "city_code"),
    ),
    "stage": (
        ("stageName", "stage_name", "stage", "schoolStage", "school_stage", "periodName"),
        ("stageId", "stage_id", "schoolStageId", "school_stage_id", "periodId"),
    ),
    "grade": (
        ("gradeName", "grade_name", "grade", "schoolGrade", "school_grade"),
        ("gradeId", "grade_id", "schoolGradeId", "school_grade_id"),
    ),
}


def _template_name(row: Mapping[str, Any]) -> str:
    raw = _first(
        row,
        (
            "templateName", "template_name", "paperTemplateName", "paper_template_name",
            "questionTypeTemplateName", "question_type_template_name", "name", "title",
        ),
    )
    return _mapping_name(raw) if isinstance(raw, Mapping) else _text(raw)


def _template_id(row: Mapping[str, Any]) -> str:
    return _text(
        _first(
            row,
            (
                "platform_template_id", "platformTemplateId",
                "templateId", "template_id", "paperTemplateId", "paper_template_id",
                "questionTypeTemplateId", "question_type_template_id", "id",
            ),
        ),
        128,
    )


def _enabled(row: Mapping[str, Any]) -> bool:
    raw = _first(row, ("enabled", "isEnabled", "is_enabled", "status", "state", "enableStatus"))
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # The current platform list uses 1=启用 and 0=停用.
        return raw != 0
    normalized = _normal_key(raw)
    return normalized not in {"false", "disabled", "disable", "inactive", "off", "停用", "禁用", "已停用"}


def _question_types(row: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = _first(
        row,
        (
            "questionTypes", "question_types", "questionTypeList", "question_type_list",
            "questionTypeVoList", "question_type_vo_list", "questionTypeNameList",
            "includeQuestionTypes", "include_question_types", "containedQuestionTypes",
            "questionTypeNames", "question_type_names",
        ),
    )
    if isinstance(raw, str):
        values: Sequence[Any] = [part.strip() for part in raw.replace("，", ",").split(",")]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = raw
    else:
        values = ()
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            name = _mapping_name(value) or _text(_first(value, ("questionTypeName", "typeName")))
            identifier = _mapping_id(value) or _text(_first(value, ("questionTypeId", "typeId")), 128)
        else:
            name = _text(value)
            identifier = ""
        key = _normal_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({"id": identifier or name, "name": name})
    return result


def _normalize_record(row: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    name = _template_name(row)
    if not name:
        return None
    identifier = _template_id(row)
    existing_key = _text(_first(row, ("platform_template_key", "platformTemplateKey")), 300)
    record: dict[str, Any] = {
        "platform_template_key": existing_key or f"{kind}:{identifier or _normal_key(name)}",
        "template_kind": kind,
        "platform_template_id": identifier,
        "name": name,
        "enabled": _enabled(row),
    }
    version = _text(_first(row, ("version", "templateVersion", "template_version", "updatedAt", "updateTime")), 128)
    if version:
        record["platform_template_version"] = version
    for field, (names, ids) in _SCOPE_ALIASES.items():
        value = _choice(row, names, ids)
        if value:
            record[field] = value
    if kind == "paper":
        question_types = _question_types(row)
        if question_types:
            record["question_types"] = question_types
    return record


def normalize_platform_template_catalog(
    question_rows: Sequence[Mapping[str, Any]],
    paper_rows: Sequence[Mapping[str, Any]],
    *,
    question_total: int | None = None,
    paper_total: int | None = None,
    page_count: int | None = None,
    synced_at: str | None = None,
) -> dict[str, Any]:
    """Normalize both platform lists into one bounded scoped catalogue."""

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, rows in (("question", question_rows), ("paper", paper_rows)):
        for raw in rows:
            if len(records) >= PLATFORM_TEMPLATE_CATALOG_MAX_RECORDS:
                break
            if not isinstance(raw, Mapping):
                continue
            record = _normalize_record(raw, kind)
            if not record:
                continue
            fingerprint = str(record["platform_template_key"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            records.append(record)
    question_count = sum(record["template_kind"] == "question" for record in records)
    paper_count = sum(record["template_kind"] == "paper" for record in records)
    return {
        "schema_version": PLATFORM_TEMPLATE_CATALOG_SCHEMA_VERSION,
        "synced_at": synced_at or _now_iso(),
        "record_count": len(records),
        "question_template_count": question_count,
        "paper_template_count": paper_count,
        "source_question_total": max(question_count, _safe_int(question_total) or 0),
        "source_paper_total": max(paper_count, _safe_int(paper_total) or 0),
        "page_count": _safe_int(page_count) or 0,
        "records": records,
    }


def empty_platform_template_catalog() -> dict[str, Any]:
    return normalize_platform_template_catalog([], [], question_total=0, paper_total=0, page_count=0)


class PlatformTemplateCatalogStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else Path(ensure_data_dir()) / PLATFORM_TEMPLATE_CATALOG_FILENAME
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                return empty_platform_template_catalog()
        if not isinstance(payload, Mapping):
            return empty_platform_template_catalog()
        rows = payload.get("records")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return empty_platform_template_catalog()
        question_rows = [row for row in rows if isinstance(row, Mapping) and row.get("template_kind") == "question"]
        paper_rows = [row for row in rows if isinstance(row, Mapping) and row.get("template_kind") == "paper"]
        return normalize_platform_template_catalog(
            question_rows,
            paper_rows,
            question_total=_safe_int(payload.get("source_question_total")),
            paper_total=_safe_int(payload.get("source_paper_total")),
            page_count=_safe_int(payload.get("page_count")),
            synced_at=_text(payload.get("synced_at"), 64) or None,
        )

    def save(self, catalog: Mapping[str, Any]) -> dict[str, Any]:
        rows = catalog.get("records") if isinstance(catalog, Mapping) else []
        safe_rows = [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else []
        safe = normalize_platform_template_catalog(
            [row for row in safe_rows if row.get("template_kind") == "question"],
            [row for row in safe_rows if row.get("template_kind") == "paper"],
            question_total=_safe_int(catalog.get("source_question_total")) if isinstance(catalog, Mapping) else None,
            paper_total=_safe_int(catalog.get("source_paper_total")) if isinstance(catalog, Mapping) else None,
            page_count=_safe_int(catalog.get("page_count")) if isinstance(catalog, Mapping) else None,
            synced_at=_text(catalog.get("synced_at"), 64) if isinstance(catalog, Mapping) else None,
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temp.open("w", encoding="utf-8") as handle:
                    json.dump(safe, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
        return safe


class PlatformTemplateCatalogService:
    def __init__(self, store: PlatformTemplateCatalogStore | None = None) -> None:
        self.store = store or PlatformTemplateCatalogStore()
        self._lock = threading.RLock()
        self._status: dict[str, Any] | None = None

    def get_catalog(self) -> dict[str, Any]:
        return self.store.load()

    def get_sync_status(self) -> dict[str, Any]:
        with self._lock:
            if self._status is not None:
                return dict(self._status)
        catalog = self.get_catalog()
        count = int(catalog.get("record_count") or 0)
        return {
            "sync_id": None,
            "status": "SUCCEEDED" if catalog.get("synced_at") and count else "IDLE",
            "message": "已加载本机平台模板目录" if count else "尚未同步平台模板目录",
            "synced_at": catalog.get("synced_at"),
            "record_count": count,
            "question_template_count": int(catalog.get("question_template_count") or 0),
            "paper_template_count": int(catalog.get("paper_template_count") or 0),
        }

    def start_sync(self) -> dict[str, Any]:
        with self._lock:
            if self._status and self._status.get("status") == "RUNNING":
                return dict(self._status)
            sync_id = f"platform-template-catalog-{uuid.uuid4().hex[:16]}"
            self._status = {
                "sync_id": sync_id,
                "status": "RUNNING",
                "message": "正在打开平台模板管理；如果需要登录，请在 Chrome 窗口完成登录。",
                "started_at": _now_iso(),
                "record_count": 0,
            }
            threading.Thread(target=self._run_sync, args=(sync_id,), daemon=True).start()
            return dict(self._status)

    def _update(self, sync_id: str, **values: Any) -> None:
        with self._lock:
            if self._status and self._status.get("sync_id") == sync_id:
                self._status.update(values)

    def _run_sync(self, sync_id: str) -> None:
        try:
            from platform_entry.adapter.runtime import _default_profile_dir
            from platform_entry.adapter.template_page import sync_platform_template_catalog_live

            self._update(sync_id, message="正在读取专项模板和试卷模板（会自动处理分页）……")
            result = sync_platform_template_catalog_live(
                profile_dir=_default_profile_dir(),
                login_timeout_seconds=300,
            )
            catalog = normalize_platform_template_catalog(
                result.get("question_rows") or [],
                result.get("paper_rows") or [],
                question_total=_safe_int(result.get("question_total")),
                paper_total=_safe_int(result.get("paper_total")),
                page_count=_safe_int(result.get("page_count")),
            )
            if not catalog.get("record_count"):
                raise PlatformTemplateCatalogError(
                    "模板管理页面没有返回可用模板，请确认登录账号有模板查看权限",
                    code="PLATFORM_TEMPLATE_CATALOG_EMPTY",
                )
            saved = self.store.save(catalog)
            question_count = int(saved.get("question_template_count") or 0)
            paper_count = int(saved.get("paper_template_count") or 0)
            self._update(
                sync_id,
                status="SUCCEEDED",
                message=f"平台模板同步完成：专项 {question_count} 个，试卷 {paper_count} 个",
                finished_at=_now_iso(),
                synced_at=saved.get("synced_at"),
                record_count=int(saved.get("record_count") or 0),
                question_template_count=question_count,
                paper_template_count=paper_count,
            )
        except Exception as exc:  # noqa: BLE001 - rendered as a bounded safe status
            message = _text(str(exc), 500) or "平台模板同步失败"
            code = getattr(exc, "code", None) or "PLATFORM_TEMPLATE_CATALOG_SYNC_FAILED"
            if isinstance(exc, TimeoutError) or "登录" in message:
                code = "PLATFORM_TEMPLATE_CATALOG_LOGIN_REQUIRED"
                message = "平台登录状态已失效，请在打开的 Chrome 窗口登录后重试同步。"
            self._update(sync_id, status="FAILED", code=code, message=message, finished_at=_now_iso())


__all__ = [
    "PlatformTemplateCatalogError",
    "PlatformTemplateCatalogService",
    "PlatformTemplateCatalogStore",
    "empty_platform_template_catalog",
    "normalize_platform_template_catalog",
]
