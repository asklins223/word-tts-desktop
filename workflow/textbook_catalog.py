"""Local textbook-directory cache and user-triggered sync jobs.

The external platform owns the directory and its authentication session.  This
module only stores the small, display-safe facts needed by the renderer and
starts a visible, read-only browser sync.  It deliberately never accepts or
persists an Authorization token.
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


TEXTBOOK_CATALOG_SCHEMA_VERSION = 1
TEXTBOOK_CATALOG_FILENAME = "textbook-catalog.json"
TEXTBOOK_CATALOG_MAX_RECORDS = 50_000
TEXTBOOK_CATALOG_FIELDS = ("version", "stage", "grade", "volume", "unit", "lesson")
TEXTBOOK_CATALOG_SOURCE_TOTAL_SCOPES = ("expanded_paths", "source_books")


class TextbookCatalogError(RepositoryError):
    """A safe, renderer-readable textbook catalogue error."""


def _text(value: Any, limit: int = 256) -> str:
    return str(value or "").strip()[:limit]


def _normal_key(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "version": (
        "versionName", "version_name", "editionName", "edition_name", "bookVersion",
        "book_version", "version", "edition", "pressName", "press_name",
        # The textbook-management list currently returns the version label as
        # ``name`` and the version id as ``textBookNameId``.
        "textBookName", "textbookName", "textbook_name", "name",
    ),
    "stage": ("stageName", "stage_name", "schoolStage", "school_stage", "stage"),
    "grade": ("gradeName", "grade_name", "schoolGrade", "school_grade", "grade"),
    "volume": (
        "volumeName", "volume_name", "bookVolume", "book_volume", "volume", "bookName",
        "book_name",
    ),
    "unit": ("unitName", "unit_name", "unit", "chapterName", "chapter_name", "chapter"),
    "lesson": (
        "lessonName", "lesson_name", "lesson", "classHourName", "class_hour_name",
        "classHour", "class_hour", "sectionName", "section_name", "section",
    ),
}

_FIELD_ID_ALIASES: dict[str, tuple[str, ...]] = {
    "version": (
        "versionId", "version_id", "editionId", "edition_id", "pressId", "press_id",
        "textBookNameId", "textbookNameId", "textbook_name_id",
    ),
    "stage": ("stageId", "stage_id", "schoolStageId", "school_stage_id"),
    "grade": ("gradeId", "grade_id", "schoolGradeId", "school_grade_id"),
    "volume": ("volumeId", "volume_id", "bookVolumeId", "book_volume_id", "bookId", "book_id"),
    "unit": ("unitId", "unit_id", "chapterId", "chapter_id"),
    "lesson": ("lessonId", "lesson_id", "classHourId", "class_hour_id", "sectionId", "section_id"),
}


def _mapping_name(value: Mapping[str, Any]) -> str:
    for key in ("name", "label", "text", "title", "value", "dictName"):
        candidate = _text(value.get(key))
        if candidate and not candidate.isdigit():
            return candidate
    return ""


def _mapping_id(value: Mapping[str, Any]) -> str:
    for key in ("id", "value", "key", "code"):
        candidate = _text(value.get(key), 128)
        if candidate:
            return candidate
    return ""


def _choice_from_row(row: Mapping[str, Any], field: str) -> dict[str, str] | None:
    nested = None
    for key in _FIELD_ALIASES[field]:
        if key in row and row.get(key) not in (None, ""):
            nested = row.get(key)
            break
    identifier = ""
    for key in _FIELD_ID_ALIASES[field]:
        if row.get(key) not in (None, ""):
            identifier = _text(row.get(key), 128)
            break
    if isinstance(nested, Mapping):
        name = _mapping_name(nested)
        identifier = identifier or _mapping_id(nested)
    else:
        name = _text(nested)
    if not name or name.isdigit():
        # A few deployments return the id/name pair with a different casing,
        # or expose a numeric field such as ``version`` alongside the human
        # label in ``name``.  Keep looking instead of treating the numeric id
        # as the display value.
        name = ""
        normalized = {_normal_key(key): value for key, value in row.items()}
        for key in _FIELD_ALIASES[field]:
            candidate = normalized.get(_normal_key(key))
            if isinstance(candidate, Mapping):
                candidate_name = _mapping_name(candidate)
                candidate_id = _mapping_id(candidate)
            else:
                candidate_name = _text(candidate)
                candidate_id = ""
            if candidate_name and not candidate_name.isdigit():
                name = candidate_name
                identifier = identifier or candidate_id
                break
    if not name or name.isdigit():
        return None
    return {"id": identifier or name, "name": name}


def _row_id(row: Mapping[str, Any]) -> str:
    for key in ("id", "textbookId", "textbook_id", "textId", "text_id"):
        value = _text(row.get(key), 128)
        if value:
            return value
    return ""


def _rows_from_payload(payload: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, Mapping):
            total = data.get("total", data.get("totalCount", data.get("count")))
            for key in ("list", "records", "rows", "items", "data"):
                candidate = data.get(key)
                if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                    return [row for row in candidate if isinstance(row, Mapping)], _safe_int(total)
            return [], _safe_int(total)
        if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
            return [row for row in data if isinstance(row, Mapping)], None
        for key in ("list", "records", "rows", "items"):
            candidate = payload.get(key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                return [row for row in candidate if isinstance(row, Mapping)], _safe_int(payload.get("total"))
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return [row for row in payload if isinstance(row, Mapping)], None
    return [], None


def _safe_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _dedupe_options(records: Sequence[Mapping[str, Any]], field: str) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        choice = record.get(field)
        if not isinstance(choice, Mapping):
            continue
        name = _text(choice.get("name"))
        if not name:
            continue
        key = _normal_key(name)
        if key in seen:
            continue
        seen.add(key)
        options.append({"id": _text(choice.get("id"), 128) or name, "name": name})
    return options


def _choice_fingerprint(choice: Any) -> tuple[str, str]:
    """Keep platform ids exact while comparing labels case/format-insensitively."""

    if not isinstance(choice, Mapping):
        return "", ""
    return _text(choice.get("id"), 128), _normal_key(choice.get("name"))


def _expand_textbook_directory_row(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Flatten textbook-management rows into version-to-lesson paths."""

    raw_units = next(
        (
            row.get(key)
            for key in ("units", "unitList", "unit_list")
            if isinstance(row.get(key), Sequence)
            and not isinstance(row.get(key), (str, bytes))
        ),
        None,
    )
    if not raw_units:
        return [row]

    expanded: list[Mapping[str, Any]] = []
    for raw_unit in raw_units:
        if not isinstance(raw_unit, Mapping):
            continue
        unit_name = _text(
            raw_unit.get("unit")
            or raw_unit.get("unitName")
            or raw_unit.get("unit_name")
            or raw_unit.get("chapter")
            or raw_unit.get("chapterName")
            or raw_unit.get("name")
            or raw_unit.get("title")
        )
        if not unit_name:
            continue
        unit_choice = {
            "id": _text(raw_unit.get("id"), 128) or unit_name,
            "name": unit_name,
        }
        inherited = dict(row)
        inherited["unit"] = unit_choice

        raw_lessons = next(
            (
                raw_unit.get(key)
                for key in ("classHours", "class_hours", "lessons", "lessonList", "lesson_list")
                if isinstance(raw_unit.get(key), Sequence)
                and not isinstance(raw_unit.get(key), (str, bytes))
            ),
            None,
        )
        if not raw_lessons:
            expanded.append(inherited)
            continue
        for raw_lesson in raw_lessons:
            if not isinstance(raw_lesson, Mapping):
                continue
            lesson_name = _text(
                raw_lesson.get("name")
                or raw_lesson.get("lesson")
                or raw_lesson.get("lessonName")
                or raw_lesson.get("lesson_name")
                or raw_lesson.get("classHour")
                or raw_lesson.get("classHourName")
                or raw_lesson.get("title")
            )
            if not lesson_name:
                continue
            leaf = dict(inherited)
            leaf["lesson"] = {
                "id": _text(raw_lesson.get("id"), 128) or lesson_name,
                "name": lesson_name,
            }
            expanded.append(leaf)

    return expanded or [row]


def normalize_textbook_catalog(
    rows: Sequence[Mapping[str, Any]],
    *,
    total: int | None = None,
    page_count: int | None = None,
    synced_at: str | None = None,
    source_total_scope: str | None = None,
) -> dict[str, Any]:
    """Convert platform page rows into a bounded, display-safe catalogue."""

    records: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    source_rows = [row for row in rows if isinstance(row, Mapping)]
    has_nested_directory = any(
        any(
            isinstance(row.get(key), Sequence)
            and not isinstance(row.get(key), (str, bytes))
            for key in ("units", "unitList", "unit_list")
        )
        for row in source_rows
    )
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        for directory_row in _expand_textbook_directory_row(raw):
            if len(records) >= TEXTBOOK_CATALOG_MAX_RECORDS:
                break
            record: dict[str, Any] = {"id": _row_id(directory_row)}
            for field in TEXTBOOK_CATALOG_FIELDS:
                choice = _choice_from_row(directory_row, field)
                if choice:
                    record[field] = choice
            if not any(field in record for field in TEXTBOOK_CATALOG_FIELDS):
                continue
            fingerprint = tuple(
                _choice_fingerprint(record.get(field))
                for field in TEXTBOOK_CATALOG_FIELDS
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            records.append(record)
        if len(records) >= TEXTBOOK_CATALOG_MAX_RECORDS:
            break

    options = {
        field: _dedupe_options(records, field)
        for field in TEXTBOOK_CATALOG_FIELDS
    }
    reported_total = _safe_int(total) if total is not None else None
    requested_scope = _text(source_total_scope, 32)
    if requested_scope not in TEXTBOOK_CATALOG_SOURCE_TOTAL_SCOPES:
        requested_scope = "source_books" if has_nested_directory else "expanded_paths"
    if requested_scope == "source_books":
        # The textbook-management endpoint returns one row per book and nests
        # units/lessons underneath it.  Keep that platform count separate from
        # the expanded selectable paths so the UI never compares unlike units.
        source_total = reported_total if reported_total is not None else len(source_rows)
    else:
        # A stale/partial platform total must never make the UI claim that
        # fewer records were loaded than we actually retained locally.
        source_total = max(len(records), reported_total if reported_total is not None else 0)
    reported_pages = _safe_int(page_count) if page_count is not None else None
    return {
        "schema_version": TEXTBOOK_CATALOG_SCHEMA_VERSION,
        "synced_at": synced_at or _now_iso(),
        "record_count": len(records),
        "source_total": source_total,
        "source_total_scope": requested_scope,
        "page_count": reported_pages or 0,
        "records": records,
        "options": options,
    }


def empty_textbook_catalog() -> dict[str, Any]:
    return normalize_textbook_catalog([], total=0, page_count=0, synced_at=None)


class TextbookCatalogStore:
    """Small atomic JSON store for the manual sync result."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else Path(ensure_data_dir()) / TEXTBOOK_CATALOG_FILENAME
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                return empty_textbook_catalog()
            if not isinstance(payload, Mapping):
                return empty_textbook_catalog()
            records = payload.get("records")
            if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
                return empty_textbook_catalog()
            safe_rows = [record for record in records if isinstance(record, Mapping)]
            return normalize_textbook_catalog(
                safe_rows,
                total=_safe_int(payload.get("source_total")),
                page_count=_safe_int(payload.get("page_count")),
                synced_at=_text(payload.get("synced_at"), 64) or None,
                source_total_scope=_text(payload.get("source_total_scope"), 32) or None,
            )

    def save(self, catalog: Mapping[str, Any]) -> dict[str, Any]:
        records = catalog.get("records") if isinstance(catalog, Mapping) else []
        safe = normalize_textbook_catalog(
            [record for record in records if isinstance(record, Mapping)]
            if isinstance(records, Sequence) and not isinstance(records, (str, bytes))
            else [],
            total=_safe_int(catalog.get("source_total")) if isinstance(catalog, Mapping) else None,
            page_count=_safe_int(catalog.get("page_count")) if isinstance(catalog, Mapping) else None,
            synced_at=_text(catalog.get("synced_at"), 64) if isinstance(catalog, Mapping) else None,
            source_total_scope=_text(catalog.get("source_total_scope"), 32) if isinstance(catalog, Mapping) else None,
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


class TextbookCatalogService:
    """Own one in-process manual sync job and the durable local cache."""

    def __init__(self, store: TextbookCatalogStore | None = None) -> None:
        self.store = store or TextbookCatalogStore()
        self._lock = threading.RLock()
        self._status: dict[str, Any] | None = None

    def get_catalog(self) -> dict[str, Any]:
        return self.store.load()

    def get_sync_status(self) -> dict[str, Any]:
        with self._lock:
            if self._status is not None:
                return dict(self._status)
        catalog = self.get_catalog()
        return {
            "sync_id": None,
            "status": "SUCCEEDED" if catalog.get("synced_at") and catalog.get("record_count") else "IDLE",
            "message": "已加载本机教材目录" if catalog.get("record_count") else "尚未同步教材目录",
            "synced_at": catalog.get("synced_at"),
            "record_count": catalog.get("record_count", 0),
            "source_total": catalog.get("source_total", catalog.get("record_count", 0)),
            "source_total_scope": catalog.get("source_total_scope", "expanded_paths"),
            "page_count": catalog.get("page_count", 0),
        }

    def start_sync(self) -> dict[str, Any]:
        with self._lock:
            if self._status and self._status.get("status") == "RUNNING":
                return dict(self._status)
            sync_id = f"textbook-catalog-{uuid.uuid4().hex[:16]}"
            self._status = {
                "sync_id": sync_id,
                "status": "RUNNING",
                "message": "正在打开教材管理页面；如果需要登录，请在浏览器窗口完成登录。",
                "started_at": _now_iso(),
                "record_count": 0,
            }
            worker = threading.Thread(target=self._run_sync, args=(sync_id,), daemon=True)
            worker.start()
            return dict(self._status)

    def _update(self, sync_id: str, **values: Any) -> None:
        with self._lock:
            if self._status and self._status.get("sync_id") == sync_id:
                self._status.update(values)

    def _run_sync(self, sync_id: str) -> None:
        try:
            from platform_entry.adapter.runtime import _default_profile_dir
            from platform_entry.adapter.textbook_page import sync_textbook_catalog_live

            self._update(sync_id, message="正在读取教材列表（会自动处理分页）……")
            result = sync_textbook_catalog_live(
                profile_dir=_default_profile_dir(),
                login_timeout_seconds=300,
            )
            catalog = normalize_textbook_catalog(
                result.get("rows") or [],
                total=_safe_int(result.get("total")),
                page_count=_safe_int(result.get("page_count")),
            )
            if not catalog.get("record_count"):
                raise TextbookCatalogError(
                    "教材页面没有返回可用列表数据，请确认登录账号有教材查看权限",
                    code="TEXTBOOK_CATALOG_EMPTY",
                )
            saved = self.store.save(catalog)
            source_total = int(saved.get("source_total") or saved.get("record_count") or 0)
            record_count = int(saved.get("record_count") or 0)
            source_total_scope = saved.get("source_total_scope", "expanded_paths")
            if source_total_scope == "source_books":
                message = f"教材目录同步完成，平台返回 {source_total} 本教材，展开为 {record_count} 条可选路径"
            elif source_total > record_count:
                message = f"教材目录同步完成，可用 {record_count} / 平台返回 {source_total} 条"
            else:
                message = f"教材目录同步完成，共 {record_count} 条"
            self._update(
                sync_id,
                status="SUCCEEDED",
                message=message,
                finished_at=_now_iso(),
                synced_at=saved.get("synced_at"),
                record_count=record_count,
                source_total=source_total,
                source_total_scope=source_total_scope,
                page_count=saved.get("page_count", 0),
            )
        except Exception as exc:  # noqa: BLE001 - the job reports a safe UI result
            message = _text(str(exc), 500) or "教材目录同步失败"
            code = getattr(exc, "code", None) or "TEXTBOOK_CATALOG_SYNC_FAILED"
            if isinstance(exc, TimeoutError) or "登录" in message:
                code = "TEXTBOOK_CATALOG_LOGIN_REQUIRED"
                message = "教材平台登录状态已失效，请在打开的浏览器窗口登录后重试同步。"
            self._update(
                sync_id,
                status="FAILED",
                code=code,
                message=message,
                finished_at=_now_iso(),
            )


__all__ = [
    "TEXTBOOK_CATALOG_FIELDS",
    "TEXTBOOK_CATALOG_SOURCE_TOTAL_SCOPES",
    "TextbookCatalogError",
    "TextbookCatalogService",
    "TextbookCatalogStore",
    "empty_textbook_catalog",
    "normalize_textbook_catalog",
]
