"""Durable system-input projection and the audio acceptance boundary.

The existing workflow engine owns document parsing and TTS generation.  This
module owns the adjacent, deliberately separate system-input graph: stable
record units, content-to-audio mappings, configuration entries, and the
user-confirmed audio gate.  It never writes to a third-party system directly.
An optional page executor can be attached by the application boundary; that
executor is expected to use visible page controls and return only observed
read-only feedback.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from document_profiles import (
    IMITATION_UNIT_SOURCE_SPECIAL_PROFILE,
    is_imitation_document_type,
)

from .data_safety import redact_public_json
from .domain import canonical_json, content_hash, new_id, utc_now
from .external import ExternalSubmission, _safe_summary
from .repositories import (
    ConflictError,
    LeaseConflict,
    NotFoundError,
    RepositoryError,
    _configuration_revision,
    repair_system_input_artifacts_in_transaction,
    _snapshot_from_connection,
)
from .system_input_content import (
    PageInputFactsError,
    document_entry_support,
    page_input_collection_status,
    sanitize_page_input,
)
from .textbook_suggestions import suggest_textbook_configuration


INPUT_TYPES = frozenset({"paper", "textbook", "vocabulary"})
DELIVERY_MODES = frozenset({"audio_only", "audio_and_input"})
PAPER_CATEGORIES = frozenset({"题型专项", "听说考试"})

# Keep the input-type capability matrix in one place.  The durable graph is
# deliberately broader than the currently implemented browser adapters: a
# textbook or vocabulary task can be parsed, reviewed, audio-generated and
# configured now, while its external page write remains disabled until the
# corresponding adapter is registered.  New platform/type combinations should
# add a row here and an adapter, rather than growing scattered ``if paper``
# branches throughout the service and renderer.
SYSTEM_INPUT_TYPE_CAPABILITIES: dict[str, dict[str, Any]] = {
    "paper": {
        "input_type": "paper",
        "label": "试卷",
        "unit_label": "套",
        "platform": "platform_input",
        "adapter_key": "platform_input.paper",
        "config_schema_version": "paper-v1",
        "external_supported": True,
        "status": "supported",
        "reason": None,
    },
    "textbook": {
        "input_type": "textbook",
        "label": "课文",
        "unit_label": "单元",
        "platform": "platform_input",
        "adapter_key": "platform_input.textbook",
        "config_schema_version": "textbook-v1",
        "external_supported": True,
        "status": "supported",
        "reason": None,
    },
    "vocabulary": {
        "input_type": "vocabulary",
        "label": "词汇",
        "unit_label": "单元",
        "platform": "platform_input",
        "adapter_key": "platform_input.vocabulary",
        "config_schema_version": "vocabulary-v1",
        "external_supported": False,
        "status": "reserved",
        "reason": "词汇页面录入适配器尚未接入",
    },
}


def system_input_capability(input_type: Any) -> dict[str, Any] | None:
    """Return a copy of the registered capability for an input type."""

    normalized = str(input_type or "").strip().casefold()
    capability = SYSTEM_INPUT_TYPE_CAPABILITIES.get(normalized)
    return dict(capability) if capability is not None else None


def system_input_capabilities() -> list[dict[str, Any]]:
    """Return the safe capability matrix exposed to API/UI projections."""

    return [dict(capability) for capability in SYSTEM_INPUT_TYPE_CAPABILITIES.values()]


# Backwards-compatible import for callers that only need the executable set.
SUPPORTED_EXTERNAL_INPUT_TYPES = frozenset(
    input_type
    for input_type, capability in SYSTEM_INPUT_TYPE_CAPABILITIES.items()
    if capability["external_supported"]
)
CONFIGURATION_KEYS = frozenset({
    "delivery_mode",
    "input_type",
    "app_template_id",
    "unit_count_override",
    "paper_category",
    "paperCategory",
    "units",
    # Safe page-form facts. They may be defaults or be repeated per unit;
    # they never carry document content, answers, audio, or playback rules.
    "paperName",
    "paperCategory",
    "paperType",
    "provinceId",
    "cityId",
    "districtIds",
    "stageId",
    "gradeId",
    "year",
    "answerTimeMinutes",
    "platformTemplateId",
    "platformTemplateVersion",
    "platformTemplateName",
    # 课文（textbook）页面字段。与试卷字段一样只保存页面显示事实，
    # 不保存课文正文、音频或播放规则。平台的新增课文表单按显示文本
    # 逐项选择，因此这里保存的是页面可见的文本值而不是平台 ID。
    "textbookNameZh",
    "textbookNameEn",
    "textbookForm",
    "textbookVersion",
    "textbookStage",
    "textbookGrade",
    "textbookVolume",
    "textbookUnit",
    "textbookLesson",
})

_SYSTEM_FORM_KEYS = frozenset({
    "paperName", "paperCategory", "paperType", "provinceId", "cityId",
    "districtIds", "stageId", "gradeId", "year", "answerTimeMinutes",
    "platformTemplateId", "platformTemplateVersion", "platformTemplateName",
})

# 课文页面表单的九个必填字段，对应平台“新增课文”第一步的分类信息。
_TEXTBOOK_FORM_KEYS = frozenset({
    "textbookNameZh",
    "textbookNameEn",
    "textbookForm",
    "textbookVersion",
    "textbookStage",
    "textbookGrade",
    "textbookVolume",
    "textbookUnit",
    "textbookLesson",
})

_UNIT_CONFIGURATION_KEYS = frozenset(_SYSTEM_FORM_KEYS | _TEXTBOOK_FORM_KEYS | {
    "unit_id",
    "input_type",
    "delivery_mode",
    "app_template_id",
    "unit_count_override",
    "paper_category",
})

_FORBIDDEN_CONFIG_KEYS = frozenset({
    "score",
    "answer",
    "referenceanswer",
    "audio",
    "audioartifactid",
    "artifactid",
    "artifactids",
    "segments",
    "content",
    "binding",
    "bindings",
    "playcount",
    "preparetime",
    "recordtime",
    "difficulty",
    "questiontext",
    "questionstem",
    "stem",
    "ttscontent",
    "rawtext",
    "normalizedcontent",
    "ttstext",
    "sourcelocator",
    "metadata",
    "text",
    "question",
    "itemid",
    "contentitemid",
    "workitemid",
    "source",
})

_UNIT_FIELD_ALIASES = {
    "paper_name": "paperName",
    "paper_category": "paperCategory",
    "paper_type": "paperType",
    "province_id": "provinceId",
    "city_id": "cityId",
    "district_ids": "districtIds",
    "district_id": "districtId",
    "stage_id": "stageId",
    "grade_id": "gradeId",
    "answer_time_minutes": "answerTimeMinutes",
    "platform_template_id": "platformTemplateId",
    "platform_template_version": "platformTemplateVersion",
    "platform_template_name": "platformTemplateName",
    "textbook_name_zh": "textbookNameZh",
    "textbook_name_en": "textbookNameEn",
    "textbook_form": "textbookForm",
    "textbook_version": "textbookVersion",
    "textbook_stage": "textbookStage",
    "textbook_grade": "textbookGrade",
    "textbook_volume": "textbookVolume",
    "textbook_unit": "textbookUnit",
    "textbook_lesson": "textbookLesson",
}

_PLATFORM_TEMPLATE_UPDATE_KEYS = frozenset({
    "input_type",
    "name",
    "platform_template_id",
    "platform_template_version",
})


class SystemInputError(RepositoryError):
    """A typed, safe error raised by the system-input boundary."""


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(decoded) if isinstance(decoded, list) else []


def _json_decoded(value: Any) -> Any:
    if isinstance(value, (Mapping, list, tuple)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _json_value(value: Any, *, fallback: Any = None) -> Any:
    safe = redact_public_json(value)
    if safe is None and value is not None:
        return fallback
    return safe


def _safe_exception_message(exc: BaseException, *, limit: int = 2000) -> str:
    safe = redact_public_json(str(exc)[:limit])
    return _text(safe, limit=limit)


def _exception_evidence(exc: BaseException) -> dict[str, Any]:
    """Persist safe structured diagnostics for retryable executor failures."""

    evidence: dict[str, Any] = {
        "executor_error": _safe_exception_message(exc, limit=1000),
        "error_type": type(exc).__name__,
    }
    details = getattr(exc, "details", None)
    if isinstance(details, Mapping) and details:
        safe_details = redact_public_json(details)
        if isinstance(safe_details, Mapping):
            evidence["details"] = dict(safe_details)
    return evidence


_BROWSER_CLOSED_TOKENS = (
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
    "browser closed",
    "page closed",
    "target crashed",
    "browser disconnected",
)


def _is_browser_closed_error(exc: BaseException) -> bool:
    """Whether a playwright failure means the user closed the browser.

    Playwright surfaces a closed window as a plain Error whose text varies
    by version and by what was open (page/context/browser), so the check is
    a token match on the redacted message instead of an exception type. The
    page executors also wrap the original error in ``SystemInputError`` and
    put the useful message in ``details``; inspect that wrapper and its cause
    chain as well, otherwise the scheduler can mistake a manually closed
    browser for a retryable page failure and open a new window.
    """

    messages: list[str] = []
    pending: list[tuple[Any, int]] = [(exc, 0)]
    seen: set[int] = set()

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 3 or len(messages) >= 32:
            return
        if isinstance(value, str):
            safe = _safe_exception_message(ValueError(value), limit=1000).lower()
            if safe:
                messages.append(safe)
                # Some adapters expose a machine-readable marker instead of
                # the Playwright sentence. Normalize separators so values such
                # as ``INPUT_BROWSER_CLOSED`` still match the human-readable
                # browser-close tokens below.
                normalized = safe.replace("_", " ").replace("-", " ")
                if normalized != safe:
                    messages.append(normalized)
            return
        if isinstance(value, Mapping):
            for key, item in list(value.items())[:32]:
                normalized_key = str(key).strip().casefold()
                if normalized_key in {
                    "browser_closed",
                    "browser_disconnected",
                    "page_closed",
                    "target_closed",
                } and item is True:
                    messages.append(normalized_key)
                collect(item, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value[:32]:
                collect(item, depth + 1)

    while pending and len(messages) < 32:
        current, depth = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        message = _safe_exception_message(current, limit=1000).lower()
        if message:
            messages.append(message)
            normalized = message.replace("_", " ").replace("-", " ")
            if normalized != message:
                messages.append(normalized)
        collect(getattr(current, "details", None))
        pending.extend(
            (linked, depth + 1)
            for linked in (getattr(current, "__cause__", None), getattr(current, "__context__", None))
            if isinstance(linked, BaseException) and depth < 4
        )

    return any(
        token in message
        or message in {"browser_closed", "browser_disconnected", "page_closed", "target_closed"}
        for message in messages
        for token in _BROWSER_CLOSED_TOKENS
    )


def _text(value: Any, *, limit: int = 512) -> str:
    return str(value or "").strip()[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _page_content_status(
    page_inputs: Sequence[Any],
    *,
    required: bool,
    auxiliary: Sequence[bool] | None = None,
) -> dict[str, Any]:
    """Return the shared page-content readiness result for one target."""

    return page_input_collection_status(
        page_inputs,
        required=required,
        auxiliary=auxiliary,
    )


def _int(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    result = int(number)
    return result if 0 <= result <= 2_147_483_647 else None


def _page_choice_has_label(value: Any) -> bool:
    """Return whether a saved choice can be replayed through page controls.

    The browser adapter needs a visible label to search/click. Treating a
    numeric ID as a complete choice would let an invalid configuration pass
    the durable start gate and fail only after an external operation had
    already been marked in-flight.
    """

    if isinstance(value, Mapping):
        label = _text(value.get("name") or value.get("label") or value.get("text"), limit=256)
        return bool(label) and not label.isdigit()
    if isinstance(value, str):
        value = value.strip()
        return bool(value) and not value.isdigit()
    return False


def _page_choice_has_id_and_label(value: Any) -> bool:
    """Return whether a saved choice has both page text and its stable ID."""

    if not isinstance(value, Mapping):
        return False
    identifier = value.get("id", value.get("value"))
    if identifier is None or isinstance(identifier, bool) or not _text(identifier, limit=256):
        return False
    return _page_choice_has_label(value)


def _normal_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").casefold())


def _assert_no_forbidden(value: Any, path: str = "configuration") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normal_key(key)
            if normalized in _FORBIDDEN_CONFIG_KEYS:
                raise SystemInputError(
                    f"系统录入配置不能包含内容/音频字段：{key}",
                    code="SYSTEM_INPUT_CONFIG_FORBIDDEN_FIELD",
                    details={"field": f"{path}.{key}"},
                )
            _assert_no_forbidden(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value[:256]):
            _assert_no_forbidden(child, f"{path}[{index}]")


def _extract_system_configuration(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(raw or {}) if isinstance(raw, Mapping) else {}
    nested = source.get("system_input")
    if isinstance(nested, Mapping):
        selected = dict(nested)
        # Top-level fields are the canonical representation used by the
        # desktop form.  Let them override a stale nested copy when both exist.
        for key in CONFIGURATION_KEYS:
            if key in source:
                selected[key] = source[key]
    else:
        selected = {key: source[key] for key in CONFIGURATION_KEYS if key in source}
    if "delivery_mode" not in selected:
        selected["delivery_mode"] = "audio_only"
    return selected


def _canonical_unit(value: Mapping[str, Any], *, index: int, input_type: str, category: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:128]:
        key = _UNIT_FIELD_ALIASES.get(str(raw_key), str(raw_key))
        if key in {"entry_id", "external_record_id", "external_record_mapping_id"}:
            continue
        if key not in _UNIT_CONFIGURATION_KEYS:
            continue
        if isinstance(raw_value, (Mapping, list, tuple)):
            safe = _json_value(raw_value)
            if safe is not None:
                result[key] = safe
        elif raw_value is not None:
            result[key] = str(raw_value)[:1024] if isinstance(raw_value, str) else raw_value
    unit_id = _text(result.get("unit_id"), limit=256) or f"unit-position-{index + 1}"
    result["unit_id"] = unit_id
    if input_type == "paper":
        resolved_category = _text(result.get("paperCategory") or category, limit=64)
        if resolved_category in PAPER_CATEGORIES:
            result["paperCategory"] = resolved_category
        if resolved_category != "听说考试":
            # The page form must omit paperType for 题型专项, rather than
            # sending an empty value that the platform may interpret as a
            # different category.
            result.pop("paperType", None)
        for key in _TEXTBOOK_FORM_KEYS:
            # 课文页面字段是类型专属 schema，不允许跨类型残留。
            result.pop(key, None)
    elif input_type == "textbook":
        # Paper fields are intentionally not a generic fallback for future
        # content types.  Keeping them out of the persisted unit projection
        # prevents a textbook/vocabulary editor from accidentally inheriting a
        # stale paper configuration when its dedicated adapter is added later.
        for key in _SYSTEM_FORM_KEYS:
            result.pop(key, None)
        result.pop("paper_category", None)
        # 课文形式等字段是课文页面专属值；空值不落库，避免覆盖建议值。
        for key in _TEXTBOOK_FORM_KEYS:
            if not _text(result.get(key), limit=256):
                result.pop(key, None)
    else:
        # Paper fields are intentionally not a generic fallback for future
        # content types.  Keeping them out of the persisted unit projection
        # prevents a textbook/vocabulary editor from accidentally inheriting a
        # stale paper configuration when its dedicated adapter is added later.
        for key in _SYSTEM_FORM_KEYS:
            result.pop(key, None)
        result.pop("paper_category", None)
        for key in _TEXTBOOK_FORM_KEYS:
            result.pop(key, None)
    return result


def validate_system_input_configuration(
    raw: Mapping[str, Any] | None,
    *,
    unit_ids: Sequence[str] | None = None,
    allow_partial: bool = True,
) -> dict[str, Any]:
    """Normalize the safe part of a workflow's system-input configuration.

    This function accepts a complete workflow configuration as well as the
    nested ``system_input`` object.  TTS fields are ignored; content, score,
    answer, audio, and platform playback fields are rejected wherever they
    appear in the system-input object.
    """

    selected = _extract_system_configuration(raw)
    _assert_no_forbidden(selected)
    mode = _text(selected.get("delivery_mode"), limit=64) or "audio_only"
    if mode not in DELIVERY_MODES:
        raise SystemInputError("delivery_mode 只能是 audio_only 或 audio_and_input", code="VALIDATION_ERROR")
    input_type = _text(selected.get("input_type"), limit=32) or None
    if input_type is not None and input_type not in INPUT_TYPES:
        raise SystemInputError("input_type 只能是 paper、textbook 或 vocabulary", code="VALIDATION_ERROR")

    category = _text(selected.get("paper_category") or selected.get("paperCategory"), limit=64) or None
    if category is not None and category not in PAPER_CATEGORIES:
        raise SystemInputError("paper_category 只能是题型专项或听说考试", code="VALIDATION_ERROR")
    if input_type != "paper":
        category = None

    unit_ids_set = {str(value) for value in (unit_ids or []) if str(value).strip()}
    raw_units = selected.get("units", [])
    if isinstance(raw_units, Mapping):
        candidates = []
        for key, value in list(raw_units.items())[:256]:
            if not isinstance(value, Mapping):
                raise SystemInputError("units 的每一项必须是对象", code="VALIDATION_ERROR")
            item = dict(value)
            item.setdefault("unit_id", key)
            candidates.append(item)
    elif isinstance(raw_units, Sequence) and not isinstance(raw_units, (str, bytes, bytearray)):
        candidates = [dict(item) for item in raw_units[:256] if isinstance(item, Mapping)]
        if len(candidates) != len(raw_units[:256]):
            raise SystemInputError("units 的每一项必须是对象", code="VALIDATION_ERROR")
    elif raw_units in (None, ""):
        candidates = []
    else:
        raise SystemInputError("units 必须是对象数组", code="VALIDATION_ERROR")

    units = [
        _canonical_unit(item, index=index, input_type=input_type or "paper", category=category)
        for index, item in enumerate(candidates)
    ]
    seen: set[str] = set()
    for unit in units:
        unit_id = str(unit["unit_id"])
        if unit_id in seen:
            raise SystemInputError("units 中 unit_id 不能重复", code="VALIDATION_ERROR")
        seen.add(unit_id)
        if unit_ids_set and unit_id not in unit_ids_set:
            raise SystemInputError("配置中的 unit_id 不属于当前解析结果", code="VALIDATION_ERROR")

    override = _text(selected.get("unit_count_override"), limit=32) or None
    if override not in {None, "single", "multiple"}:
        raise SystemInputError("unit_count_override 只能是 single 或 multiple", code="VALIDATION_ERROR")
    template_id = _text(selected.get("app_template_id"), limit=256) or None
    if not allow_partial and mode == "audio_and_input" and not input_type:
        raise SystemInputError("开启系统录入前必须确认录入类型", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")

    canonical: dict[str, Any] = {
        "delivery_mode": mode,
        "input_type": input_type,
        "app_template_id": template_id,
        "unit_count_override": override,
        "units": units,
    }
    for key in _SYSTEM_FORM_KEYS:
        value = selected.get(key)
        if value is None:
            continue
        safe = _json_value(value)
        if safe is not None:
            canonical[key] = safe
    for key in _TEXTBOOK_FORM_KEYS:
        value = selected.get(key)
        if value is None:
            continue
        safe = _json_value(value)
        if safe is not None:
            canonical[key] = safe
    if input_type != "paper":
        # The paper form fields are a type-specific schema, not shared
        # configuration.  Retain only the common template reference fields so
        # a future textbook/vocabulary adapter can opt into its own schema
        # without reusing paper semantics.
        for key in _SYSTEM_FORM_KEYS - {
            "platformTemplateId", "platformTemplateVersion", "platformTemplateName",
        }:
            canonical.pop(key, None)
        canonical.pop("paper_category", None)
    if input_type != "textbook":
        # 课文页面字段同样是类型专属 schema；试卷/词汇配置不得携带。
        for key in _TEXTBOOK_FORM_KEYS:
            canonical.pop(key, None)
    else:
        for key in _TEXTBOOK_FORM_KEYS:
            if not _text(canonical.get(key), limit=256):
                canonical.pop(key, None)
    if category != "听说考试":
        canonical.pop("paperType", None)
    if category is not None:
        canonical["paper_category"] = category
    if len(units) > 1:
        # A paper name belongs to one input unit.  A legacy workflow may still
        # carry one top-level name, but retaining it beside multiple units lets
        # partial saves reintroduce that value during a later projection.
        canonical.pop("paperName", None)
    # Null values are intentionally omitted from the persisted payload to make
    # hashes stable and to keep 题型专项 free of paperType-like placeholders.
    return {
        key: value for key, value in canonical.items()
        if value is not None and value != [] or key == "units"
    }


def project_system_input_configuration(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a small renderer-safe system configuration projection."""

    value = validate_system_input_configuration(raw, allow_partial=True)
    result = {
        "delivery_mode": value.get("delivery_mode", "audio_only"),
        "input_type": value.get("input_type"),
        "app_template_id": value.get("app_template_id"),
        "unit_count_override": value.get("unit_count_override"),
        "paper_category": value.get("paper_category"),
        "units": [],
    }
    # A unit projection is passed here as well as the workflow-level config.
    # Preserve the safe page-form fields at the top level so the renderer and
    # page executor can reconstruct each unit without exposing source content.
    for key in _SYSTEM_FORM_KEYS:
        if key in value:
            safe = _json_value(value[key])
            if safe is not None:
                result[key] = safe
    for key in _TEXTBOOK_FORM_KEYS:
        if key in value:
            safe = _json_value(value[key])
            if safe is not None:
                result[key] = safe
    for unit in value.get("units", [])[:256]:
        result["units"].append({
            key: _json_value(item)
            for key, item in unit.items()
            if key not in {"score", "answer", "referenceAnswer", "audio", "segments"}
        })
    return result


def _merge_saved_unit_configurations(
    current: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep previously saved units that a partial editor payload omitted.

    The parser owns the set of input units and the drawer edits one unit at a
    time.  A stale renderer, an interrupted mode switch, or a future partial
    editor request can therefore send only the currently visible unit.  The
    configuration endpoint must not interpret that as a request to erase the
    other saved targets.  Matching units are replaced wholesale so explicit
    field clearing still works; only absent unit IDs are retained.
    """

    current_units = [dict(unit) for unit in current if isinstance(unit, Mapping)]
    incoming_units = [dict(unit) for unit in incoming if isinstance(unit, Mapping)]
    if not current_units:
        return incoming_units
    if not incoming_units:
        return current_units

    by_id = {str(unit.get("unit_id")): unit for unit in incoming_units}
    merged: list[dict[str, Any]] = []
    for unit in current_units:
        unit_id = str(unit.get("unit_id"))
        merged.append(by_id.pop(unit_id, unit))
    # New IDs are parser-owned additions; preserve their incoming order after
    # the existing units rather than silently dropping them.
    merged.extend(by_id.values())
    return merged


def validate_template_configuration(input_type: str, configuration: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate an app template without allowing document/task facts."""

    normalized_type = _text(input_type, limit=32)
    if normalized_type not in INPUT_TYPES:
        raise SystemInputError("模板 input_type 不受支持", code="VALIDATION_ERROR")
    candidate = dict(configuration or {})
    _assert_no_forbidden(candidate, "template.configuration")
    if "units" in candidate or "unit_id" in candidate or "entry_id" in candidate:
        raise SystemInputError("应用配置模板不能保存当前任务的录入单元或录入 ID", code="TEMPLATE_FORBIDDEN_FIELD")
    return dict(redact_public_json(candidate) or {})


def _source_filename(config: Mapping[str, Any], supplied: str | None) -> str:
    value = _text(supplied or config.get("source_filename"), limit=256)
    return value or "未命名文档.docx"


def _entry_document_name(
    input_type: Any,
    configuration: Mapping[str, Any] | None,
    fallback: Any = None,
) -> str:
    """Return the name users can search for on the input platform.

    ``document_name`` used to be populated with the uploaded source filename.
    The platform does not expose a stable review URL, so the durable entry must
    instead retain the title submitted in its visible form: a paper's
    ``paperName`` or a textbook's ``textbookNameZh``.  Keep the old persisted
    value as a compatibility fallback for entries created before this rule.
    """

    selected = configuration if isinstance(configuration, Mapping) else {}
    normalized_type = _text(input_type, limit=32).casefold()
    title_keys = (
        ("textbookNameZh", "textbook_name_zh")
        if normalized_type == "textbook"
        else ("paperName", "paper_name")
    )
    for key in title_keys:
        title = _text(selected.get(key), limit=256)
        if title:
            return title
    return _text(fallback, limit=256) or "未命名文档.docx"


def _projection_source_filename(con: sqlite3.Connection, workflow_id: str, config: Mapping[str, Any]) -> str:
    """Best-effort source filename for renderer suggestions."""

    configured = _text(config.get("source_filename"), limit=256)
    if configured:
        return configured
    try:
        row = con.execute(
            """SELECT error_details_json FROM source_imports
               WHERE workflow_id=? ORDER BY updated_at DESC LIMIT 1""",
            (workflow_id,),
        ).fetchone()
    except Exception:
        return ""
    metadata = _json_object(row["error_details_json"] if row is not None else None).get("metadata")
    if isinstance(metadata, Mapping):
        return _text(metadata.get("filename"), limit=256)
    return ""


def _entry_document_type(metadata: Mapping[str, Any]) -> str:
    """Choose the strongest persisted type label for entry preflight.

    Older projections can put a generic ``doc_type`` beside a more specific
    imitation-reading category.  Prefer the recognized family label so a
    legacy item cannot hide behind the generic value and retain executable
    page facts.
    """

    values = [
        _text(metadata.get("doc_type"), limit=128),
        _text(metadata.get("document_type"), limit=128),
        _text(metadata.get("category"), limit=128),
    ]
    for value in values:
        if value and is_imitation_document_type(value):
            return value
    return next((value for value in values if value), "")


def _document_entry_support_for_metadata(
    metadata_rows: Sequence[Mapping[str, Any]],
    *,
    input_type: Any,
) -> dict[str, Any]:
    """Run the document-entry preflight over parser-owned item metadata.

    Projection, start and recovery must use the same aligned facts. Keeping
    the assembly here prevents one path from silently dropping a category,
    profile or capability field that another path uses as a safety gate.
    """

    rows = [row if isinstance(row, Mapping) else {} for row in metadata_rows]
    return document_entry_support(
        [row.get("page_input") for row in rows],
        input_type=_text(input_type, limit=32),
        document_types=[_entry_document_type(row) for row in rows],
        major_section_profiles=[row.get("major_section_profile") for row in rows],
        entry_profiles=[row.get("entry_profile") for row in rows],
        capabilities=[row.get("capabilities") for row in rows],
        auxiliary=[row.get("audio_only_auxiliary") is True for row in rows],
    )


_PAPER_DOCUMENT_TYPES = frozenset({
    "信息获取",
    "听后选择",
    "听后应答",
    "信息转述及询问",
    "听后记录并转述信息",
    "模仿朗读",
})
_TEXTBOOK_DOCUMENT_TYPES = frozenset({"课文跟读"})
_VOCABULARY_DOCUMENT_TYPES = frozenset({"词汇"})


def _classification_tokens(item: Mapping[str, Any]) -> tuple[str, ...]:
    """Collect parser-owned type facts without consulting the source filename.

    ``work_items`` contains the normalized parser metadata as JSON.  The
    source filename is intentionally absent here: it is display metadata, not
    evidence of what the document contains.  Both the family name and the
    family code are accepted because old and new parser projections may not
    have the same metadata shape.
    """

    metadata = _json_object(item.get("metadata_json"))
    values: list[str] = []
    for value in (
        metadata.get("doc_type"),
        metadata.get("document_type"),
        metadata.get("question_type"),
        metadata.get("sub_type_code"),
        metadata.get("category"),
        item.get("item_type"),
    ):
        text = _text(value, limit=128)
        if text:
            values.append(text)
    page_input = metadata.get("page_input")
    if isinstance(page_input, Mapping):
        page_type = _text(page_input.get("type") or page_input.get("question_type"), limit=128)
        if page_type:
            values.append(page_type)
    raw_path = metadata.get("type_path") or metadata.get("type_hierarchy")
    if isinstance(raw_path, str):
        values.extend(
            _text(value, limit=128)
            for value in re.split(r"[>/|,，]+", raw_path)
            if _text(value, limit=128)
        )
    elif isinstance(raw_path, Sequence) and not isinstance(raw_path, (bytes, bytearray, str)):
        values.extend(
            _text(value, limit=128)
            for value in raw_path[:16]
            if _text(value, limit=128)
        )
    return tuple(dict.fromkeys(values))


def _classify_input_type(items: Sequence[Mapping[str, Any]], filename: str, config: Mapping[str, Any]) -> tuple[str, str]:
    """Classify the system-input family from parser facts, not file naming.

    A single derived family remains ``suggested`` until the user confirms it.
    If no parser fact exists, or facts from different content families are
    mixed, return a safe paper display fallback with an ``unknown``/``conflict``
    status; the start gate requires explicit confirmation and therefore cannot
    submit a misclassified document.  ``filename`` is retained in the
    signature for callers that still pass it, but is deliberately unused.
    """

    del filename
    selected = _extract_system_configuration(config)
    explicit = _text(selected.get("input_type"), limit=32)
    if explicit in INPUT_TYPES:
        return explicit, "user_override"

    families: set[str] = set()
    unresolved_items = 0
    for item in items:
        if not isinstance(item, Mapping):
            unresolved_items += 1
            continue
        tokens = _classification_tokens(item)
        item_families: set[str] = set()
        for token in tokens:
            if token in _VOCABULARY_DOCUMENT_TYPES or token in {"单词", "例句"} or token.casefold() in {"vocabulary", "word", "words"}:
                item_families.add("vocabulary")
            elif token in _TEXTBOOK_DOCUMENT_TYPES or token.casefold() in {"textbook", "text-reading", "text_reading"}:
                item_families.add("textbook")
            elif token in _PAPER_DOCUMENT_TYPES or any(
                marker in token for marker in ("听后选择", "听后应答", "信息获取", "信息转述", "模仿朗读")
            ) or token.casefold() in {"paper", "exam", "listening_choice", "listening_response", "imitation_reading", "info_acquisition", "info_retelling", "listening_record_retelling"}:
                item_families.add("paper")
        if len(item_families) == 1:
            families.update(item_families)
        elif len(item_families) > 1:
            families.update(item_families)
        else:
            unresolved_items += 1

    if len(families) > 1:
        return "paper", "conflict"
    if len(families) == 1 and unresolved_items == 0:
        return next(iter(families)), "suggested"
    if len(families) == 1:
        # A recognized family mixed with an unclassified parser item is not
        # safe to auto-confirm.  Keep the family visible, but require review.
        return next(iter(families)), "conflict"
    return "paper", "unknown"


def _classify_paper_category(
    items: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Project parser structure into the current专项/套卷 form labels.

    The parser emits ``exam_form`` and ``paper_category`` when it can inspect
    the source document.  The metadata-only fallback also recognizes a
    complete paper when multiple independent paper families are present, so
    older parsed workspaces do not silently fall back to专项.  Explicit saved
    configuration always wins and is recorded as a user override.
    """

    selected = _extract_system_configuration(config)
    configured = _text(
        selected.get("paper_category") or selected.get("paperCategory"),
        limit=64,
    )
    if configured in PAPER_CATEGORIES:
        return configured, "user_override", {
            "strategy": "configuration",
            "detected_types": [],
            "metadata_categories": [configured],
            "exam_forms": [],
            "signals": ["使用用户已保存的试卷分类"],
        }

    categories: set[str] = set()
    exam_forms: set[str] = set()
    category_statuses: set[str] = set()
    paper_types: list[str] = []
    invalid_category_values: list[str] = []
    paper_type_codes = {
        "info_acquisition": "信息获取",
        "listening_choice": "听后选择",
        "listening_response": "听后应答",
        "info_retelling": "信息转述及询问",
        "listening_record_retelling": "听后记录并转述信息",
        "imitation_reading": "模仿朗读",
    }
    paper_type_names = tuple(sorted(_PAPER_DOCUMENT_TYPES, key=len, reverse=True))
    for item in items:
        if not isinstance(item, Mapping):
            continue
        metadata = _json_object(item.get("metadata_json"))
        category = _text(metadata.get("paper_category"), limit=64)
        if category in PAPER_CATEGORIES:
            categories.add(category)
        elif category:
            invalid_category_values.append(category)
        form = _text(metadata.get("exam_form"), limit=32).casefold()
        if form in {"special", "paper", "unknown"}:
            exam_forms.add(form)
        status = _text(metadata.get("paper_category_status"), limit=32).casefold()
        if status in {"suggested", "confirmed", "conflict", "user_override"}:
            category_statuses.add(status)
        for token in _classification_tokens(item):
            token_key = token.casefold()
            paper_type = paper_type_codes.get(token_key)
            if paper_type is None:
                paper_type = next(
                    (name for name in paper_type_names if name in token),
                    None,
                )
            if paper_type is not None and paper_type not in paper_types:
                paper_types.append(paper_type)

    form_categories = {
        "paper": "听说考试",
        "special": "题型专项",
    }
    for form in exam_forms:
        if form in form_categories:
            categories.add(form_categories[form])

    conflict_reasons: list[str] = []
    if len(categories) > 1:
        conflict_reasons.append("解析条目的试卷分类不一致")
    if "unknown" in exam_forms:
        conflict_reasons.append("解析结果无法确认试卷组织形态")
    if "conflict" in category_statuses:
        conflict_reasons.append("解析结果标记了分类冲突")
    if invalid_category_values:
        conflict_reasons.append("解析结果包含不受支持的试卷分类")

    if conflict_reasons and not categories:
        # ``unknown`` has no faithful UI label. Keep the visible choice at the
        # safe专项 value while preserving the conflict status and evidence;
        # the start gate still requires an explicit user confirmation.
        category = "题型专项"
        strategy = "conflicting_metadata"
    elif len(categories) > 1:
        category = "题型专项"
        strategy = "conflicting_metadata"
    elif categories:
        category = next(iter(categories))
        strategy = "parser_category"
    elif len(paper_types) >= 2:
        # Multiple independent major paper families are the compatibility
        # signal for a full listening paper in older parser projections.
        category = "听说考试"
        strategy = "multiple_paper_types"
    elif paper_types:
        category = "题型专项"
        strategy = "single_paper_type"
    else:
        # Keep the legacy UI usable while marking the evidence as weak.  The
        # saved form still requires the user to confirm before external input.
        category = "题型专项"
        strategy = "legacy_safe_default"

    status = "conflict" if conflict_reasons else "suggested"
    signals = [
        f"解析到 {len(paper_types)} 个独立试卷题型",
        f"分类候选: {category}",
    ]
    signals.extend(conflict_reasons)
    return category, status, {
        "strategy": strategy,
        "detected_types": paper_types[:32],
        "metadata_categories": sorted(categories),
        "exam_forms": sorted(exam_forms),
        "category_statuses": sorted(category_statuses),
        "signals": signals,
    }


def _textbook_group_parts(metadata: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Derive (section, category, subgroup, title_source) for one item."""

    section = _text(metadata.get("section"), limit=128)
    category = _text(metadata.get("category") or metadata.get("doc_type"), limit=64)
    subgroup = ""
    if category == "段落跟读":
        conversation = _text(metadata.get("conversation_number"), limit=64)
        subgroup = f"Conversation {conversation}" if conversation else ""
    elif category == "语篇跟读":
        subgroup = _text(metadata.get("article_title"), limit=256)
    return section, category, subgroup, subgroup


def _resolve_textbook_groups(
    items: Sequence[Mapping[str, Any]],
) -> tuple["dict[str, list[Mapping[str, Any]]]", str, dict[str, Any], "dict[str, dict[str, str]]"]:
    """Split parsed textbook items into one entry unit per platform record.

    平台侧一条课文记录对应“章节 × 内容类型 × 文章/对话”一组内容；
    切分完全由解析器输出的结构事实（section/category/conversation/
    article_title）驱动，同一文档出现多组即视为多个录入单元。
    """

    groups: "dict[str, list[Mapping[str, Any]]]" = {}
    meta: "dict[str, dict[str, str]]" = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_metadata = _json_object(item.get("metadata_json"))
        section, category, subgroup, _title = _textbook_group_parts(item_metadata)
        key = "|".join((section, category, subgroup))
        groups.setdefault(key, []).append(item)
        meta.setdefault(key, {
            "section": section,
            "category": category,
            "subgroup": subgroup,
            "theme": _text(item_metadata.get("article_theme"), limit=256),
        })
    count_status = "multiple_confirmed" if len(groups) > 1 else "single_default"
    evidence = {
        "strategy": "textbook_structure",
        "group_keys": list(groups.keys())[:32],
        "item_count": sum(len(group) for group in groups.values()),
    }
    return groups, count_status, evidence, meta


def _textbook_unit_label(meta: Mapping[str, Any]) -> str:
    section = _text(meta.get("section"), limit=128)
    category = _text(meta.get("category"), limit=64)
    subgroup = _text(meta.get("subgroup"), limit=256)
    if subgroup:
        return f"{section} · {subgroup}" if section else subgroup
    if section and category:
        return f"{section} · {category}"
    return _text(section or category, limit=256) or "课文单元"


def _textbook_unit_configuration_defaults(
    meta: Mapping[str, Any],
    source_filename: str,
) -> dict[str, str]:
    """Derive the nine platform form fields for one textbook entry unit."""

    section = _text(meta.get("section"), limit=128)
    category = _text(meta.get("category"), limit=64)
    subgroup = _text(meta.get("subgroup"), limit=256)
    theme = _text(meta.get("theme"), limit=256)

    document_defaults = suggest_textbook_configuration(source_filename, [])
    document_value = lambda field: _text(  # noqa: E731
        (document_defaults.get(field) or {}).get("value"),
        limit=256,
    )

    name_zh = theme or section
    if not name_zh and subgroup:
        name_zh = subgroup
    if category == "段落跟读" and subgroup:
        name_zh = f"{section} {subgroup}".strip() if section else subgroup
    name_en = subgroup if category == "语篇跟读" else document_value("textbookNameEn")
    return {
        "textbookNameZh": _text(name_zh, limit=256),
        "textbookNameEn": _text(name_en, limit=256),
        "textbookForm": "同步课文",
        "textbookVersion": document_value("textbookVersion"),
        "textbookStage": document_value("textbookStage"),
        "textbookGrade": document_value("textbookGrade"),
        "textbookVolume": document_value("textbookVolume"),
        "textbookUnit": document_value("textbookUnit"),
        "textbookLesson": section,
    }


def _unit_key(metadata: Mapping[str, Any], item: Mapping[str, Any], index: int) -> tuple[str, bool]:
    for key in ("unit_id", "unit", "unit_label", "set_number", "paper_set", "set", "套数"):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            value = value.get("id") or value.get("name") or value.get("label")
        text = _text(value, limit=256)
        if text:
            return f"{key}:{text}", True
    for value in (metadata.get("section"), metadata.get("category"), item.get("source_locator")):
        match = re.search(r"(?:第\s*)?([0-9]{1,3})\s*(?:套|份)", _text(value, limit=512))
        if match:
            return f"set:{int(match.group(1))}", True
    return "default", False


def _structure_signature(metadata: Mapping[str, Any], item: Mapping[str, Any]) -> tuple[tuple[str, ...], bool]:
    """Return a bounded structural signature for one parsed content item.

    Unit labels are intentionally excluded from this signature.  A repeated
    label is only a candidate boundary; the candidate must also repeat the
    content/type shape of a complete block before it can become an automatic
    multi-unit split.
    """

    item_type = _text(item.get("item_type"), limit=128)
    category = _text(metadata.get("category"), limit=128) or item_type
    raw_path = metadata.get("type_path") or metadata.get("type_hierarchy")
    if isinstance(raw_path, str):
        explicit_path = tuple(
            _text(part, limit=128)
            for part in re.split(r"[>/|,，]+", raw_path)
            if _text(part, limit=128)
        )
    elif isinstance(raw_path, Sequence) and not isinstance(raw_path, (bytes, bytearray, str)):
        explicit_path = tuple(
            _text(part, limit=128)
            for part in raw_path
            if _text(part, limit=128)
        )
    else:
        explicit_path = ()
    question_type = _text(metadata.get("question_type"), limit=128)
    subtype = _text(metadata.get("sub_type_code"), limit=128)
    material = _text(metadata.get("material_source"), limit=128)
    if not material:
        source = _text(metadata.get("source"), limit=128)
        # The new U/source imitation rule stores 外网/教材 as an item-level
        # fact while its explicit type path describes the repeated paper
        # shape.  Do not make the material label look like a second unit
        # boundary; the older layouts keep the historical source signal.
        if (
            metadata.get("major_section_profile") != IMITATION_UNIT_SOURCE_SPECIAL_PROFILE
            and source.casefold() in {"外网", "教材", "课本", "网络", "web", "textbook"}
        ):
            material = source

    # ``item_type`` is present on every work item and is not, by itself,
    # evidence of a separate complete structure.  A path, material category,
    # or explicit subtype is the additional signal required for a candidate
    # block to participate in automatic confirmation.
    has_independent_signal = bool(
        explicit_path
        or material
        or question_type
        or subtype
        or (category and category != item_type)
    )
    return (
        tuple(value for value in (category, *explicit_path, question_type, subtype, material) if value),
        has_independent_signal,
    )


def _candidate_boundaries(groups: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Build evidence for candidate ranges without storing document text."""

    boundaries: list[dict[str, Any]] = []
    for key, grouped_items in groups.items():
        if not grouped_items:
            continue
        first = grouped_items[0]
        last = grouped_items[-1]
        signatures = []
        for item in grouped_items:
            metadata = _json_object(item.get("metadata_json"))
            signature, _ = _structure_signature(metadata, item)
            signatures.append(list(signature))
        boundaries.append({
            "group_key": str(key),
            "first_sequence": _int(first.get("sequence")),
            "last_sequence": _int(last.get("sequence")),
            "first_locator": _text(first.get("source_locator"), limit=512),
            "last_locator": _text(last.get("source_locator"), limit=512),
            "item_count": len(grouped_items),
            "item_ids": [_text(item.get("item_id"), limit=256) for item in grouped_items],
            "structure_signature": signatures,
        })
    return boundaries


def _repeated_structure_groups(groups: Mapping[str, Sequence[Mapping[str, Any]]]) -> bool:
    """Whether every candidate range repeats an independently evidenced shape."""

    if len(groups) < 2:
        return False
    profiles: list[tuple[tuple[str, ...], ...]] = []
    for grouped_items in groups.values():
        if not grouped_items:
            return False
        # A one-item block with only a unit label and a generic item category
        # is still marker-only evidence.  It needs an explicit structural path
        # (or another independent field) before it can be auto-confirmed.
        if len(grouped_items) < 2:
            first_metadata = _json_object(grouped_items[0].get("metadata_json"))
            _first_signature, first_has_signal = _structure_signature(first_metadata, grouped_items[0])
            if not first_has_signal or not (
                first_metadata.get("type_path")
                or first_metadata.get("type_hierarchy")
                or first_metadata.get("material_source")
                or first_metadata.get("question_type")
                or first_metadata.get("sub_type_code")
            ):
                return False
        profile: list[tuple[str, ...]] = []
        independent_signal = False
        for item in grouped_items:
            metadata = _json_object(item.get("metadata_json"))
            signature, has_signal = _structure_signature(metadata, item)
            if not signature:
                return False
            profile.append(signature)
            independent_signal = independent_signal or has_signal
        if not independent_signal:
            return False
        profiles.append(tuple(profile))
    # Exact ordered repetition is deliberately conservative.  A future parser
    # can emit richer optional-node metadata without making labels themselves a
    # high-confidence split rule.
    return len(set(profiles)) == 1


def _resolve_unit_groups(
    items: Sequence[Mapping[str, Any]],
    override: str | None,
) -> tuple[dict[str, list[dict[str, Any]]], str, dict[str, Any]]:
    """Resolve parser candidates into safe unit ranges and audit evidence."""

    candidates: dict[str, list[dict[str, Any]]] = {}
    explicit_count = 0
    for index, item in enumerate(items):
        metadata = _json_object(item.get("metadata_json"))
        key, explicit = _unit_key(metadata, item, index)
        explicit_count += int(explicit)
        candidates.setdefault(key, []).append(dict(item))
    if not candidates:
        candidates = {"default": []}

    boundaries = _candidate_boundaries(candidates)
    repeated = _repeated_structure_groups(candidates)
    if override == "single":
        groups = {"default": [dict(item) for item in items]}
        strategy = "user_override_single"
        count_status = "single_default"
    elif repeated:
        groups = {key: list(grouped) for key, grouped in candidates.items()}
        strategy = "repeated_structure"
        count_status = "multiple_confirmed"
    elif len(candidates) > 1:
        # Do not expose weak marker-only ranges as independent external
        # records.  Keep the ranges in evidence so a review UI can confirm,
        # split, or merge them later.
        groups = {"default": [dict(item) for item in items]}
        strategy = "candidate_requires_confirmation"
        count_status = "multiple_candidate"
    elif override == "multiple":
        groups = {key: list(grouped) for key, grouped in candidates.items()}
        strategy = "user_override_multiple_requires_confirmation"
        count_status = "multiple_candidate"
    else:
        groups = {key: list(grouped) for key, grouped in candidates.items()}
        strategy = "single_default"
        count_status = "single_default"

    return groups, count_status, {
        "strategy": strategy,
        "explicit_group_markers": explicit_count,
        "candidate_boundaries": boundaries,
        "structure_repeated": repeated,
        "confidence_band": "HIGH" if count_status == "multiple_confirmed" else "MEDIUM" if count_status == "multiple_candidate" else "LOW",
    }


def _boundary_decision_from_connection(
    con: sqlite3.Connection,
    workflow_id: str,
) -> dict[str, Any] | None:
    if not _table_exists(con, "input_unit_boundary_decisions"):
        return None
    row = con.execute(
        "SELECT * FROM input_unit_boundary_decisions WHERE workflow_id=?",
        (workflow_id,),
    ).fetchone()
    if row is None:
        return None
    payload = _json_decoded(row["boundaries_json"])
    if isinstance(payload, Mapping):
        boundaries = payload.get("boundaries", [])
    else:
        boundaries = payload
    return {
        "decision_id": str(row["decision_id"]),
        "workflow_id": str(row["workflow_id"]),
        "mode": str(row["decision_type"]),
        "boundaries": list(boundaries) if isinstance(boundaries, Sequence) and not isinstance(boundaries, (str, bytes, bytearray)) else [],
        "automatic_status": str(row["automatic_status"]),
        "automatic_evidence": _json_object(row["automatic_evidence_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _user_boundary_groups(
    items: Sequence[Mapping[str, Any]],
    decision: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]] | None:
    """Apply a durable user decision only when it still covers current items."""

    ordered = [dict(item) for item in items]
    item_positions = {str(item.get("item_id")): index for index, item in enumerate(ordered)}
    if len(item_positions) != len(ordered):
        return None
    mode = _text(decision.get("mode"), limit=32)
    if mode == "single":
        return ({"user:single": ordered}, {"user:single": ""})
    if mode != "multiple" or len(ordered) < 2:
        return None

    raw_boundaries = decision.get("boundaries")
    if isinstance(raw_boundaries, (str, bytes, bytearray)) or not isinstance(raw_boundaries, Sequence):
        return None
    groups: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    seen: set[str] = set()
    for boundary in raw_boundaries[:256]:
        if not isinstance(boundary, Mapping):
            return None
        raw_item_ids = boundary.get("item_ids")
        if isinstance(raw_item_ids, (str, bytes, bytearray)) or not isinstance(raw_item_ids, Sequence):
            return None
        item_ids = [_text(item_id, limit=256) for item_id in raw_item_ids]
        if not item_ids or any(not item_id for item_id in item_ids):
            return None
        if len(set(item_ids)) != len(item_ids) or seen.intersection(item_ids):
            return None
        positions = [item_positions.get(item_id) for item_id in item_ids]
        if any(position is None for position in positions):
            return None
        first_position = positions[0]
        if positions != list(range(first_position, first_position + len(item_ids))):
            return None
        key = f"user:multiple:{item_ids[0]}:{item_ids[-1]}"
        groups[key] = [ordered[position] for position in positions]
        labels[key] = _text(boundary.get("label"), limit=128)
        seen.update(item_ids)

    if len(groups) < 2 or seen != set(item_positions):
        return None
    return groups, labels


def _unit_label(key: str, group_index: int, input_type: str, metadata: Mapping[str, Any]) -> str:
    for field in ("unit_label", "unit", "paper_set", "set_number"):
        value = metadata.get(field)
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("label") or value.get("id")
        if _text(value, limit=128):
            return _text(value, limit=128)
    if input_type == "paper":
        return f"第{group_index + 1}套"
    return f"第{group_index + 1}个录入单元"


def _path_labels(metadata: Mapping[str, Any]) -> list[str]:
    raw = metadata.get("type_path") or metadata.get("type_hierarchy")
    if isinstance(raw, str):
        values = [part.strip() for part in re.split(r"[>/|,，]+", raw) if part.strip()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray, str)):
        values = [_text(part, limit=128) for part in raw if _text(part, limit=128)]
    else:
        values = []
    for key in ("section", "category", "sub_type_code"):
        value = _text(metadata.get(key), limit=128)
        if value and value not in values:
            values.append(value)
    return values[:16]


def _unit_id(workflow_id: str, key: str) -> str:
    digest = content_hash({"workflow_id": workflow_id, "unit_key": key})
    return f"unit-{digest[:32]}"


def _node_id(workflow_id: str, unit_id: str, parent: str | None, kind: str, label: str) -> str:
    digest = content_hash({
        "workflow_id": workflow_id,
        "unit_id": unit_id,
        "parent": parent,
        "kind": kind,
        "label": label,
    })
    return f"node-{digest[:32]}"


def _segment_id(workflow_id: str, item_id: str) -> str:
    digest = content_hash({"workflow_id": workflow_id, "item_id": item_id})
    return f"segment-{digest[:32]}"


def _paper_name_for_unit(
    base_name: Any,
    unit_label: Any,
    index: int,
    total: int,
    unit_labels: Sequence[str] | None = None,
) -> str:
    """Return only the explicit paper name; never derive one from a unit label.

    ``unit_label``/index arguments remain in the signature for callers that
    still pass the old normalization context. A parsed unit label describes a
    content boundary and must not overwrite the paper name entered by the
    user, especially when a workflow contains multiple units.
    """

    return _text(base_name, limit=256)


def _unit_configuration(
    config: Mapping[str, Any],
    unit_id: str,
    *,
    unit_label: Any = None,
    unit_index: int = 0,
    unit_total: int | None = None,
    unit_labels: Sequence[str] | None = None,
    input_type: str | None = None,
) -> dict[str, Any]:
    selected = validate_system_input_configuration(config, allow_partial=True)
    base = {key: value for key, value in selected.items() if key != "units"}
    base["unit_id"] = unit_id
    prior_units = [
        unit for unit in selected.get("units", [])
        if isinstance(unit, Mapping)
    ]
    requested_total = _int(unit_total)
    multi_unit = len(prior_units) > 1 or (
        requested_total is not None and requested_total > 1
    )
    if multi_unit:
        # A legacy top-level paperName is a single-target value. Do not spread
        # it across multiple parsed units; only an explicit name nested in the
        # matched unit may survive this projection.
        base.pop("paperName", None)
        base.pop("paper_name", None)
    matched = False
    for unit in prior_units:
        if str(unit.get("unit_id") or "") == unit_id:
            base.update(unit)
            matched = True
            break
    if not matched:
        # A user-confirmed merge changes the derived unit ID. Preserve fields
        # shared by the previous unit drafts so a boundary decision does not
        # silently erase the platform/template configuration. Conflicting
        # fields remain unset and still require explicit user review.
        if len(prior_units) == 1:
            base.update({
                key: value
                for key, value in prior_units[0].items()
                if key != "unit_id" and (not multi_unit or key not in {"paperName", "paper_name"})
            })
        elif (
            requested_total is not None
            and requested_total == len(prior_units)
            and 0 <= unit_index < len(prior_units)
        ):
            # A rerun creates fresh workflow-local unit IDs while preserving
            # the same ordered parser groups. When cardinality is unchanged,
            # carry each unit's own target fields by ordinal; otherwise a
            # seven-unit task would fall back to only the fields common to all
            # units and silently lose names/templates. Boundary changes with a
            # different cardinality still use the conservative common-field
            # merge below.
            base.update({
                key: value
                for key, value in prior_units[unit_index].items()
                if key != "unit_id"
            })
        elif prior_units:
            common_keys = set(prior_units[0]) - {"unit_id"}
            if multi_unit:
                common_keys.difference_update({"paperName", "paper_name"})
            for prior in prior_units[1:]:
                common_keys.intersection_update(prior)
            for key in common_keys:
                values = [prior.get(key) for prior in prior_units]
                if all(value == values[0] for value in values[1:]):
                    base[key] = values[0]

    resolved_input_type = _text(input_type or base.get("input_type") or selected.get("input_type"), limit=32)
    if resolved_input_type in {"", "paper"}:
        total = max(1, requested_total if requested_total is not None else len(prior_units) or 1)
        paper_name = _paper_name_for_unit(
            base.get("paperName") or base.get("paper_name"),
            unit_label,
            unit_index,
            total,
            unit_labels,
        )
        if paper_name:
            base["paperName"] = paper_name
        else:
            base.pop("paperName", None)
            base.pop("paper_name", None)
        base.pop("paper_name", None)
    return base


def _configuration_complete(input_type: str, configuration: Mapping[str, Any]) -> bool:
    capability = system_input_capability(input_type)
    if capability is None or not capability.get("external_supported"):
        return False
    if input_type == "textbook":
        # 课文页面的九个分类信息字段全部是平台必填项；缺任何一项都不能
        # 安全打开新增课文表单。这些字段是页面显示文本，不需要平台 ID。
        for key in _TEXTBOOK_FORM_KEYS:
            value = configuration.get(key)
            if value is None or not _text(value, limit=256):
                return False
        return True
    if input_type != "paper":
        return False
    category = _text(configuration.get("paperCategory") or configuration.get("paper_category"), limit=64)
    # 区/县在平台页面上是可选的；省、市、学段、年级等基础字段才是
    # 第一版开始页面录入前必须确认的字段。
    required = (
        "paperName", "provinceId", "cityId", "stageId", "gradeId",
        "year", "answerTimeMinutes",
    )
    if not category or category not in PAPER_CATEGORIES:
        return False
    if category == "听说考试" and not configuration.get("paperType"):
        return False
    for key in required:
        value = configuration.get(key)
        if value is None or not _text(value, limit=256):
            return False
    for key in ("provinceId", "cityId", "stageId", "gradeId"):
        if not _page_choice_has_id_and_label(configuration.get(key)):
            return False
    if category == "听说考试" and not _page_choice_has_id_and_label(configuration.get("paperType")):
        return False
    try:
        if isinstance(configuration.get("year"), bool) or isinstance(configuration.get("answerTimeMinutes"), bool):
            return False
        year = _int(configuration.get("year"))
        duration = _int(configuration.get("answerTimeMinutes"))
    except (TypeError, ValueError, OverflowError):
        return False
    if year is None or duration is None:
        return False
    if year < 2000 or year > 2100 or duration < 1 or duration > 60:
        return False
    if not platform_template_reference_complete(configuration):
        return False
    if platform_template_reference_matches_catalog(configuration) is False:
        return False
    return True


def platform_template_reference_complete(configuration: Mapping[str, Any] | None) -> bool:
    """Whether a unit has a replayable platform-template reference.

    The visible page and the page automation both search the platform catalog
    by the template name. Older configurations may still carry platform-owned
    ID/version snapshots, but those fields are optional compatibility metadata
    rather than user-facing requirements.
    """

    if not isinstance(configuration, Mapping):
        return False
    template_name = configuration.get("platformTemplateName") or configuration.get("platform_template_name")
    return _page_choice_has_label(template_name)


def _catalog_choice_parts(value: Any) -> tuple[str, str]:
    if isinstance(value, Mapping):
        identifier = _text(value.get("id") or value.get("value"), limit=256)
        label = _text(value.get("name") or value.get("label") or value.get("text"), limit=256)
        return identifier, label
    return "", _text(value, limit=256)


def _catalog_choice_matches(left: Any, right: Any) -> bool:
    left_id, left_label = _catalog_choice_parts(left)
    right_id, right_label = _catalog_choice_parts(right)
    if left_id and right_id:
        return left_id == right_id
    return bool(left_label and right_label) and re.sub(r"\s+", "", left_label).casefold() == re.sub(r"\s+", "", right_label).casefold()


def platform_template_reference_matches_catalog(configuration: Mapping[str, Any] | None) -> bool | None:
    """Validate a paper template against the last successful scoped sync.

    ``None`` means that no catalogue is available yet, preserving legacy
    configurations until the user performs the explicit manual sync.
    """

    if not isinstance(configuration, Mapping):
        return False
    try:
        from workflow.platform_template_catalog import PlatformTemplateCatalogStore

        records = PlatformTemplateCatalogStore().load().get("records") or []
    except Exception:
        return None
    if not records:
        return None
    category = _text(configuration.get("paperCategory") or configuration.get("paper_category"), limit=64)
    kind = "paper" if category == "听说考试" else "question"
    required_scope = ("province", "city", "stage", "grade") if kind == "paper" else ("province", "city")
    scope_keys = {
        "province": ("provinceId", "province_id"),
        "city": ("cityId", "city_id"),
        "stage": ("stageId", "stage_id"),
        "grade": ("gradeId", "grade_id"),
    }
    template_reference = {
        "platform_template_id": configuration.get("platformTemplateId") or configuration.get("platform_template_id"),
        "name": configuration.get("platformTemplateName") or configuration.get("platform_template_name"),
        "platform_template_version": configuration.get("platformTemplateVersion") or configuration.get("platform_template_version"),
    }
    for record in records:
        if not isinstance(record, Mapping) or record.get("enabled") is False or record.get("template_kind") != kind:
            continue
        record_id = _text(record.get("platform_template_id"), limit=256)
        reference_id = _text(template_reference.get("platform_template_id"), limit=256)
        same_template = record_id == reference_id if record_id and reference_id else _catalog_choice_matches(
            template_reference.get("name"), record.get("name")
        )
        if not same_template:
            continue
        if all(_catalog_choice_matches(
            configuration.get(scope_keys[field][0]) or configuration.get(scope_keys[field][1]),
            record.get(field),
        ) for field in required_scope):
            return True
    return False


def _external_runtime_configuration() -> tuple[str, str, str]:
    """Return non-user-controlled external mapping facts.

    The platform account is process configuration, not part of a reusable
    page form template.  Keeping it outside the saved configuration prevents
    an app template from silently moving a task to another account or mapping
    implementation.
    """

    external_system = _text(os.environ.get("WORDTTS_SYSTEM_INPUT_EXTERNAL_SYSTEM") or "platform_input", limit=128)
    account_scope = _text(os.environ.get("WORDTTS_PLATFORM_INPUT_ACCOUNT_SCOPE") or "default", limit=256)
    mapping_version = _text(os.environ.get("WORDTTS_SYSTEM_INPUT_MAPPING_VERSION") or "platform_input-paper-v1", limit=128)
    return external_system or "platform_input", account_scope or "default", mapping_version or "platform_input-paper-v1"


def _external_operation_payload(
    input_run_id: str,
    entry_id: str,
    unit: Mapping[str, Any],
    target: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the durable operation hash without local paths or answers."""

    segments: list[dict[str, Any]] = []
    raw_segments = unit.get("segments")
    if isinstance(raw_segments, Sequence) and not isinstance(raw_segments, (str, bytes, bytearray)):
        for segment in raw_segments[:2000]:
            if not isinstance(segment, Mapping):
                continue
            segments.append({
                "segment_id": _text(segment.get("segment_id"), limit=256),
                "item_id": _text(segment.get("item_id"), limit=256),
                "ordinal": _int(segment.get("ordinal")) or 0,
                "raw_text": _text(segment.get("raw_text"), limit=1_000_000),
                "tts_text": _text(segment.get("tts_text"), limit=1_000_000),
                "score": _number(segment.get("score")),
                "audio_artifact_id": _text(segment.get("audio_artifact_id"), limit=256),
                "audio_filename_stem": _text(segment.get("audio_filename_stem"), limit=256),
                "audio_only_auxiliary": segment.get("audio_only_auxiliary") is True,
                # Hash the explicit semantic page facts into the durable
                # operation identity without duplicating answer text in the
                # external-operation row.
                "page_input_hash": content_hash(segment.get("page_input"))
                if isinstance(segment.get("page_input"), Mapping)
                else None,
            })
    return {
        "input_run_id": input_run_id,
        "entry_id": entry_id,
        "workflow_id": _text(target.get("workflow_id"), limit=256),
        "unit_id": _text(unit.get("unit_id"), limit=256),
        "unit_label": _text(unit.get("unit_label"), limit=256),
        "input_type": _text(unit.get("input_type"), limit=32),
        "configuration": project_system_input_configuration(_json_object(unit.get("configuration"))),
        "structure_revision": _int(unit.get("structure_revision")) or 1,
        "audio_revision": _int(target.get("audio_revision")) or 0,
        "artifact_manifest_hash": _text(target.get("artifact_manifest_hash"), limit=128),
        "segments": segments,
    }


def _page_target_snapshot(target: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the fields a page executor needs to render the form.

    The durable target keeps source facts for local audit/recovery.  The page
    adapter receives only the explicitly versioned semantic page-input facts
    plus the minimal render fields; arbitrary metadata, paths and future-only
    fields are never forwarded by accident.  Keeping this projection next to
    the execution boundary makes the page-only policy explicit for injected
    executors as well as the real PlatformInput adapter.
    """

    page_target: dict[str, Any] = {
        "workflow_id": _text(target.get("workflow_id"), limit=256),
        "input_type": _text(target.get("input_type"), limit=32),
        "audio_revision": _int(target.get("audio_revision")) or 0,
        "artifact_manifest_hash": _text(target.get("artifact_manifest_hash"), limit=128),
        "units": [],
    }
    capability = system_input_capability(page_target["input_type"])
    if capability is not None:
        page_target["adapter_key"] = _text(capability.get("adapter_key"), limit=128)
        page_target["schema_version"] = _text(capability.get("config_schema_version"), limit=64)
    units = target.get("units")
    if not isinstance(units, Sequence) or isinstance(units, (str, bytes, bytearray)):
        return page_target
    for unit in units[:256]:
        if not isinstance(unit, Mapping):
            continue
        page_unit = {
            "unit_id": _text(unit.get("unit_id"), limit=256),
            "unit_label": _text(unit.get("unit_label"), limit=256),
            "input_type": _text(unit.get("input_type"), limit=32),
            "configuration": project_system_input_configuration(_json_object(unit.get("configuration"))),
            "structure_revision": _int(unit.get("structure_revision")) or 1,
            "segments": [],
        }
        segments = unit.get("segments")
        if isinstance(segments, Sequence) and not isinstance(segments, (str, bytes, bytearray)):
            for segment in segments[:2000]:
                if not isinstance(segment, Mapping):
                    continue
                page_unit["segments"].append({
                    "segment_id": _text(segment.get("segment_id"), limit=256),
                    "item_id": _text(segment.get("item_id"), limit=256),
                    "content_item_id": _text(segment.get("content_item_id"), limit=256),
                    "ordinal": _int(segment.get("ordinal")) or 0,
                    "raw_text": _text(segment.get("raw_text"), limit=1_000_000),
                    "tts_text": _text(segment.get("tts_text"), limit=1_000_000),
                    "score": _number(segment.get("score")),
                    "audio_artifact_id": _text(segment.get("audio_artifact_id"), limit=256),
                    "category": _text(segment.get("category") or segment.get("item_type"), limit=128),
                    "filename_stem": _text(segment.get("filename_stem"), limit=256),
                    "audio_filename_stem": _text(segment.get("audio_filename_stem"), limit=256),
                    "audio_only_auxiliary": segment.get("audio_only_auxiliary") is True,
                    "source_locator": _text(segment.get("source_locator"), limit=512),
                    "translation": _text(segment.get("translation"), limit=1_000_000),
                })
                raw_page_input = segment.get("page_input")
                if raw_page_input is not None:
                    try:
                        page_input = sanitize_page_input(raw_page_input)
                    except PageInputFactsError as exc:
                        raise SystemInputError(
                            "系统录入页面内容事实无效",
                            code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                            details={"segment_id": page_unit["segments"][-1]["segment_id"]},
                        ) from exc
                    if page_input is not None:
                        page_unit["segments"][-1]["page_input"] = page_input
        page_target["units"].append(page_unit)
    return page_target


class SystemInputService:
    """Own system-input state without owning third-party page side effects."""

    def __init__(self, database: Any, repository: Any, *, external: Any = None, executor: Any = None) -> None:
        self.database = database
        self.repository = repository
        self.external = external
        self.executor = executor
        # Serialize the durable control request with the final page-write
        # boundary for the same run.  The database transaction alone cannot
        # close the gap between the last flag read and begin_operation().
        self._input_boundary_locks: dict[str, threading.RLock] = {}
        self._input_boundary_locks_guard = threading.Lock()

    @property
    def available(self) -> bool:
        try:
            with self.database.read_transaction() as con:
                return _table_exists(con, "input_units")
        except Exception:
            return False

    def _require_available(self, con: sqlite3.Connection) -> None:
        if not _table_exists(con, "input_units"):
            raise SystemInputError("system-input requires the full workflow schema", code="MIGRATION_REQUIRED")

    def sync_projection(self, workflow_id: str, *, source_filename: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as con:
            self._require_available(con)
            row = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            try:
                config = json.loads(str(row["configuration_snapshot"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                config = {}
            config = dict(config) if isinstance(config, Mapping) else {}
            items = [dict(item) for item in con.execute(
                "SELECT * FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id", (workflow_id,)
            ).fetchall()]
            filename = _source_filename(config, source_filename)
            input_type, input_status = _classify_input_type(items, filename, config)
            selected = _extract_system_configuration(config)
            override = _text(selected.get("unit_count_override"), limit=32) or None
            textbook_group_meta: dict[str, dict[str, str]] = {}
            if input_type == "textbook":
                # 课文按“章节 × 内容类型 × 文章/对话”结构切分：一个录入
                # 单元对应平台一条课文记录，切分由解析结构事实驱动。
                groups, count_status, grouping_evidence, textbook_group_meta = (
                    _resolve_textbook_groups(items)
                )
            else:
                groups, count_status, grouping_evidence = _resolve_unit_groups(items, override)
            decision = _boundary_decision_from_connection(con, workflow_id)
            effective_override = override
            user_labels: dict[str, str] = {}
            if decision is not None:
                effective_override = _text(decision.get("mode"), limit=32) or override
                resolved = _user_boundary_groups(items, decision)
                grouping_evidence = {
                    **grouping_evidence,
                    "decision_id": decision.get("decision_id"),
                    "automatic_status": decision.get("automatic_status"),
                    "automatic_evidence": decision.get("automatic_evidence", {}),
                    "user_override": effective_override,
                }
                if resolved is not None:
                    groups, user_labels = resolved
                    count_status = "multiple_confirmed" if effective_override == "multiple" else "single_default"
                    grouping_evidence["strategy"] = "user_override"
                else:
                    # The parser no longer contains the exact item IDs covered
                    # by the decision. Keep the current automatic projection
                    # visible as evidence, but never present it as a confirmed
                    # external target until the user confirms the new ranges.
                    count_status = "multiple_candidate"
                    grouping_evidence["strategy"] = "user_override_stale"
            category = None
            category_status = "not_applicable"
            category_evidence: dict[str, Any] = {}
            if input_type == "paper":
                category, category_status, category_evidence = _classify_paper_category(items, selected)
            coverage = "complete"
            if any(_text(_json_object(item.get("metadata_json")).get("parse_coverage_status"), limit=32) in {"partial", "unclassified"} for item in items):
                coverage = "partial"

            old_runs = con.execute("SELECT 1 FROM input_runs WHERE workflow_id=? LIMIT 1", (workflow_id,)).fetchone()
            # Once an input run exists its target snapshot is immutable. A
            # later parse request must not create, delete, or relabel units
            # underneath an already prepared external operation.
            if old_runs is not None:
                return self._projection_from_connection(con, workflow_id)

            now = utc_now()
            batch_id = f"audio-batch-{workflow_id}"
            con.execute(
                """INSERT INTO audio_batches(audio_batch_id, workflow_id, source_artifact_id,
                   audio_revision, manifest_hash, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(workflow_id) DO UPDATE SET source_artifact_id=excluded.source_artifact_id,
                   updated_at=excluded.updated_at""",
                (batch_id, workflow_id, row["source_artifact_id"], 0, None, "DRAFT", now, now),
            )

            # Before an input run starts the parser projection may be rebuilt.
            # Remove units that disappeared from the parser result as well as
            # their cascaded entries/nodes/segments; otherwise a re-parse with
            # fewer groups leaves phantom external targets in the workspace.
            unit_ids = [_unit_id(workflow_id, key) for key in groups]
            if unit_ids:
                placeholders = ", ".join("?" for _ in unit_ids)
                con.execute(
                    f"DELETE FROM input_units WHERE workflow_id=? AND unit_id NOT IN ({placeholders})",
                    (workflow_id, *unit_ids),
                )
            else:
                con.execute("DELETE FROM input_units WHERE workflow_id=?", (workflow_id,))
            con.execute("DELETE FROM content_segments WHERE workflow_id=?", (workflow_id,))
            con.execute("DELETE FROM structure_nodes WHERE workflow_id=?", (workflow_id,))

            unit_labels: list[str] = []
            for ordinal, (key, grouped_items) in enumerate(groups.items()):
                first_metadata = _json_object(grouped_items[0].get("metadata_json")) if grouped_items else {}
                label = user_labels.get(key) or (
                    _textbook_unit_label(textbook_group_meta.get(key) or {})
                    if input_type == "textbook"
                    else _unit_label(key, ordinal, input_type, first_metadata)
                )
                if grouping_evidence["strategy"] in {"candidate_requires_confirmation", "user_override_stale"} and len(groups) == 1:
                    label = f"第{ordinal + 1}{'套' if input_type == 'paper' else '个录入单元'}"
                elif (
                    grouping_evidence["strategy"] == "user_override_single"
                    or (grouping_evidence["strategy"] == "user_override" and effective_override == "single")
                ) and len(groups) == 1:
                    label = f"第{ordinal + 1}{'套' if input_type == 'paper' else '个录入单元'}"
                unit_labels.append(label)

            for ordinal, (key, grouped_items) in enumerate(groups.items()):
                unit_id = unit_ids[ordinal]
                label = unit_labels[ordinal]
                start = grouped_items[0] if grouped_items else None
                end = grouped_items[-1] if grouped_items else None
                source_range = {
                    "first_sequence": int(start["sequence"]) if start is not None else None,
                    "last_sequence": int(end["sequence"]) if end is not None else None,
                    "first_locator": _text(start.get("source_locator"), limit=512) if start else None,
                    "last_locator": _text(end.get("source_locator"), limit=512) if end else None,
                }
                evidence = {
                    "group_key": key,
                    "item_count": len(grouped_items),
                    "parser_projection": "work_items.metadata_json",
                    "unit_grouping": grouping_evidence,
                }
                if input_type == "paper":
                    evidence["paper_category"] = category_evidence
                unit_config = _unit_configuration(
                    config,
                    unit_id,
                    unit_label=label,
                    unit_index=ordinal,
                    unit_total=len(groups),
                    unit_labels=unit_labels,
                    input_type=input_type,
                )
                existing = con.execute("SELECT configuration_json, structure_revision FROM input_units WHERE unit_id=?", (unit_id,)).fetchone()
                stored_config = _json_object(existing["configuration_json"]) if existing is not None else {}
                if stored_config:
                    stored_config = _unit_configuration(
                        {"input_type": input_type, "units": [stored_config]},
                        unit_id,
                        unit_label=label,
                        unit_index=ordinal,
                        unit_total=len(groups),
                        unit_labels=unit_labels,
                        input_type=input_type,
                    )
                else:
                    stored_config = unit_config
                if input_type == "textbook":
                    # 结构推导的课文分类信息：仅在用户尚未填写该字段时
                    # 注入，已保存/已修改的值永远优先。同时固化单元类型，
                    # 否则投影读回时课文键会被当成未知类型剥掉。
                    stored_config.setdefault("input_type", input_type)
                    unit_defaults = _textbook_unit_configuration_defaults(
                        textbook_group_meta.get(key) or {},
                        filename,
                    )
                    for config_field, default_value in unit_defaults.items():
                        if not _text(stored_config.get(config_field), limit=256) and _text(default_value, limit=256):
                            stored_config[config_field] = default_value
                structure_revision = max(1, int(existing["structure_revision"] or 1)) if existing else 1
                if existing is None:
                    con.execute(
                        """INSERT INTO input_units(
                           unit_id, workflow_id, audio_batch_id, ordinal, label, input_type,
                           unit_count_status, unit_count_override, input_type_status,
                           paper_category, paper_category_status, parse_coverage_status,
                           source_range_json, evidence_json, configuration_json,
                           structure_revision, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (unit_id, workflow_id, batch_id, ordinal, label, input_type,
                         count_status, effective_override, input_status, category, category_status,
                         coverage, canonical_json(source_range), canonical_json(evidence),
                         canonical_json(stored_config), structure_revision, now, now),
                    )
                else:
                    con.execute(
                        """UPDATE input_units SET audio_batch_id=?, ordinal=?, label=?, input_type=?,
                           unit_count_status=?, unit_count_override=COALESCE(?, unit_count_override),
                           input_type_status=?, paper_category=?, paper_category_status=?,
                           parse_coverage_status=?, source_range_json=?, evidence_json=?, updated_at=?
                           WHERE unit_id=? AND workflow_id=?""",
                        (batch_id, ordinal, label, input_type, count_status, effective_override,
                         input_status, category, category_status, coverage,
                         canonical_json(source_range), canonical_json(evidence), now, unit_id, workflow_id),
                    )

                root_id = _node_id(workflow_id, unit_id, None, "unit", label)
                self._upsert_node(con, root_id, workflow_id, unit_id, None, 0, "unit", label, [label], source_range.get("first_locator"), 1.0, evidence, now)
                for item_ordinal, item in enumerate(grouped_items):
                    metadata = _json_object(item.get("metadata_json"))
                    parent_id = root_id
                    path = [label]
                    for path_index, path_label in enumerate(_path_labels(metadata), start=1):
                        path.append(path_label)
                        node_kind = "type" if path_index == 1 else "context"
                        node = _node_id(workflow_id, unit_id, parent_id, node_kind, path_label)
                        self._upsert_node(con, node, workflow_id, unit_id, parent_id, path_index, node_kind, path_label, path, _text(item.get("source_locator"), limit=512), _number(metadata.get("confidence")), {"source": "metadata", "path_index": path_index}, now)
                        parent_id = node
                    item_id = str(item["item_id"])
                    raw_text = _text(metadata.get("raw_text"), limit=1_000_000) or _text(item.get("normalized_content"), limit=1_000_000)
                    tts_text = _text(item.get("normalized_content"), limit=1_000_000)
                    score = _number(metadata.get("score"))
                    answer = metadata.get("answer", metadata.get("reference_answer"))
                    answer_json = canonical_json(_json_value(answer)) if answer is not None else None
                    segment_id = _segment_id(workflow_id, item_id)
                    con.execute(
                        """INSERT INTO content_segments(
                           segment_id, workflow_id, unit_id, node_id, item_id, content_item_id,
                           ordinal, raw_text, tts_text, source_locator, score, answer_json,
                           audio_artifact_id, audio_revision, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(segment_id) DO UPDATE SET node_id=excluded.node_id,
                           ordinal=excluded.ordinal, raw_text=excluded.raw_text, tts_text=excluded.tts_text,
                           source_locator=excluded.source_locator, score=excluded.score,
                           answer_json=excluded.answer_json, updated_at=excluded.updated_at""",
                        (segment_id, workflow_id, unit_id, parent_id, item_id, item_id,
                         item_ordinal, raw_text, tts_text, _text(item.get("source_locator"), limit=512),
                         score, answer_json, None, 0, now, now),
                    )

            # The projection is refreshed only for parser-owned fields.  A
            # saved entry or input run remains tied to its original unit ID.
            return self._projection_from_connection(con, workflow_id)

    def confirm_unit_boundaries(
        self,
        workflow_id: str,
        expected_state_version: int,
        *,
        mode: str,
        boundaries: Sequence[Mapping[str, Any]] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist the user's final unit-boundary decision.

        Decisions reference stable ``work_items.item_id`` values rather than
        parser-derived unit IDs. This lets a later parse refresh the local
        projection without silently changing a confirmed single/multiple
        choice. A decision that no longer covers the current items is kept for
        audit but is deliberately shown as a candidate until reconfirmed.
        """

        normalized_mode = _text(mode, limit=32)
        if normalized_mode not in {"single", "multiple"}:
            raise SystemInputError("录入单元决策只能是 single 或 multiple", code="VALIDATION_ERROR")

        with self.database.transaction() as con:
            self._require_available(con)
            if not _table_exists(con, "input_unit_boundary_decisions"):
                raise SystemInputError(
                    "录入单元边界确认需要最新数据库迁移",
                    code="MIGRATION_REQUIRED",
                )
            row = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            if int(row["state_version"]) != int(expected_state_version):
                raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
            if con.execute("SELECT 1 FROM input_runs WHERE workflow_id=? LIMIT 1", (workflow_id,)).fetchone() is not None:
                raise ConflictError(
                    "系统录入运行已经创建，不能改变录入单元边界",
                    code="BOUNDARY_FROZEN",
                )

            items = [dict(item) for item in con.execute(
                "SELECT item_id, sequence, source_locator, metadata_json, normalized_content FROM work_items "
                "WHERE workflow_id=? ORDER BY sequence, item_id",
                (workflow_id,),
            ).fetchall()]
            item_ids = [_text(item.get("item_id"), limit=256) for item in items]
            if not items or any(not item_id for item_id in item_ids) or len(set(item_ids)) != len(item_ids):
                raise SystemInputError(
                    "当前解析结果没有可确认的稳定录入单元边界",
                    code="SYSTEM_INPUT_BOUNDARIES_INVALID",
                )

            raw_config = _json_object(row["configuration_snapshot"])
            selected = _extract_system_configuration(raw_config)
            automatic_override = _text(selected.get("unit_count_override"), limit=32) or None
            _, automatic_status, automatic_evidence = _resolve_unit_groups(items, automatic_override)

            normalized_boundaries: list[dict[str, Any]]
            if normalized_mode == "single":
                normalized_boundaries = [{"item_ids": item_ids}]
            else:
                if (
                    isinstance(boundaries, (str, bytes, bytearray))
                    or not isinstance(boundaries, Sequence)
                    or len(boundaries) > 256
                ):
                    raise SystemInputError(
                        "多单元决策必须提供不超过 256 个连续边界",
                        code="SYSTEM_INPUT_BOUNDARIES_INVALID",
                    )
                normalized_boundaries = []
                for boundary in boundaries:
                    if not isinstance(boundary, Mapping):
                        raise SystemInputError(
                            "每个录入单元边界必须是对象",
                            code="SYSTEM_INPUT_BOUNDARIES_INVALID",
                        )
                    raw_ids = boundary.get("item_ids")
                    if (
                        isinstance(raw_ids, (str, bytes, bytearray))
                        or not isinstance(raw_ids, Sequence)
                        or not raw_ids
                        or len(raw_ids) > 2000
                    ):
                        raise SystemInputError(
                            "每个录入单元必须包含连续的 item_ids",
                            code="SYSTEM_INPUT_BOUNDARIES_INVALID",
                        )
                    boundary_ids = [_text(item_id, limit=256) for item_id in raw_ids]
                    if any(not item_id for item_id in boundary_ids):
                        raise SystemInputError(
                            "录入单元边界不能包含空 item_id",
                            code="SYSTEM_INPUT_BOUNDARIES_INVALID",
                        )
                    normalized_boundary: dict[str, Any] = {"item_ids": boundary_ids}
                    label = _text(boundary.get("label"), limit=128)
                    if label:
                        normalized_boundary["label"] = label
                    normalized_boundaries.append(normalized_boundary)
                if _user_boundary_groups(
                    items,
                    {"mode": "multiple", "boundaries": normalized_boundaries},
                ) is None:
                    raise SystemInputError(
                        "录入单元边界必须按文档顺序连续覆盖全部内容，且至少包含两个单元",
                        code="SYSTEM_INPUT_BOUNDARIES_INVALID",
                    )

            decision_id = new_id("unit-boundary-decision")
            now = utc_now()
            con.execute(
                """INSERT INTO input_unit_boundary_decisions(
                   decision_id, workflow_id, decision_type, boundaries_json,
                   automatic_status, automatic_evidence_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(workflow_id) DO UPDATE SET decision_id=excluded.decision_id,
                   decision_type=excluded.decision_type, boundaries_json=excluded.boundaries_json,
                   automatic_status=excluded.automatic_status,
                   automatic_evidence_json=excluded.automatic_evidence_json,
                   updated_at=excluded.updated_at""",
                (
                    decision_id,
                    workflow_id,
                    normalized_mode,
                    canonical_json({"boundaries": normalized_boundaries}),
                    automatic_status,
                    canonical_json(automatic_evidence),
                    now,
                    now,
                ),
            )
            updated = con.execute(
                "UPDATE workflows SET state_version=state_version+1, updated_at=? "
                "WHERE workflow_id=? AND state_version=?",
                (now, workflow_id, expected_state_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
            self.repository.events.append_in_transaction(
                con,
                workflow_id,
                "SYSTEM_INPUT_BOUNDARIES_CONFIRMED",
                {
                    "decision_id": decision_id,
                    "decision_type": normalized_mode,
                    "boundary_count": len(normalized_boundaries),
                    "automatic_status": automatic_status,
                },
                request_id=request_id,
                actor_type="USER",
                actor_id="desktop",
            )
            snapshot = _snapshot_from_connection(con, workflow_id)
            self.repository.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())

        projection = self.sync_projection(workflow_id)
        return {
            "snapshot": snapshot.as_dict(),
            "projection": projection,
            "decision": projection.get("unit_boundary_decision"),
        }

    @staticmethod
    def _upsert_node(
        con: sqlite3.Connection,
        node_id: str,
        workflow_id: str,
        unit_id: str,
        parent_id: str | None,
        ordinal: int,
        kind: str,
        label: str,
        path: Sequence[str],
        source_locator: str | None,
        confidence: float | None,
        evidence: Mapping[str, Any],
        now: str,
    ) -> None:
        con.execute(
            """INSERT INTO structure_nodes(
               node_id, workflow_id, unit_id, parent_node_id, ordinal, node_kind,
               label, path_json, source_locator, confidence, evidence_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(node_id) DO UPDATE SET parent_node_id=excluded.parent_node_id,
               ordinal=excluded.ordinal, node_kind=excluded.node_kind, label=excluded.label,
               path_json=excluded.path_json, source_locator=excluded.source_locator,
               confidence=excluded.confidence, evidence_json=excluded.evidence_json,
               updated_at=excluded.updated_at""",
            (node_id, workflow_id, unit_id, parent_id, ordinal, kind, label,
             canonical_json(list(path)), source_locator, confidence,
             canonical_json(dict(evidence)), now, now),
        )

    def _audio_gate_from_connection(self, con: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
        items = [dict(row) for row in con.execute(
            "SELECT item_id, sequence, status FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id", (workflow_id,)
        ).fetchall()]
        artifact_rows = [dict(row) for row in con.execute(
            """SELECT a.artifact_id, a.item_id, a.sha256 AS artifact_sha256, a.size_bytes AS artifact_size,
                      a.format AS artifact_format, a.lifecycle_state AS artifact_state, a.verified,
                      b.sha256 AS blob_sha256, b.size_bytes AS blob_size, b.format AS blob_format,
                      b.lifecycle_state AS blob_state, a.created_at
               FROM artifacts a LEFT JOIN artifact_blobs b ON b.blob_id=a.blob_id
               WHERE a.workflow_id=? AND a.artifact_type='tts-segment'
               ORDER BY a.item_id, a.created_at DESC, a.artifact_id DESC""", (workflow_id,)
        ).fetchall()]
        latest: dict[str, dict[str, Any]] = {}
        for artifact in artifact_rows:
            item_id = str(artifact.get("item_id") or "")
            if item_id and item_id not in latest:
                latest[item_id] = artifact
        required: list[dict[str, Any]] = []
        missing: list[str] = []
        conflicts: list[str] = []
        reasons: dict[str, str] = {}
        for item in items:
            item_id = str(item["item_id"])
            if str(item["status"]) == "SKIPPED":
                continue
            artifact = latest.get(item_id)
            if artifact is None:
                missing.append(item_id)
                reasons[item_id] = "ARTIFACT_MISSING"
                continue
            normalized_formats = {
                _text(artifact.get("artifact_format"), limit=16).lower().lstrip("."),
                _text(artifact.get("blob_format"), limit=16).lower().lstrip("."),
            }
            valid = (
                str(item["status"]) == "SUCCEEDED"
                and str(artifact.get("artifact_state") or "") == "READY"
                and bool(artifact.get("verified"))
                and str(artifact.get("blob_state") or "") == "READY"
                and normalized_formats == {"mp3"}
                and str(artifact.get("artifact_sha256") or "") == str(artifact.get("blob_sha256") or "")
                and int(artifact.get("artifact_size") or -1) == int(artifact.get("blob_size") or -2)
            )
            if not valid:
                conflicts.append(item_id)
                reasons[item_id] = "ARTIFACT_METADATA_CONFLICT" if artifact else "ITEM_NOT_SUCCEEDED"
                continue
            required.append({
                "item_id": item_id,
                "sequence": int(item["sequence"]),
                "artifact_id": str(artifact["artifact_id"]),
                "sha256": str(artifact["blob_sha256"]),
                "size_bytes": int(artifact["blob_size"]),
                "format": "mp3",
            })
        required.sort(key=lambda value: (value["sequence"], value["item_id"]))
        manifest_hash = content_hash({"workflow_id": workflow_id, "artifacts": required}) if required else ""
        technical_status = "passed" if not missing and not conflicts and bool(required) else "failed"
        return {
            "technical_status": technical_status,
            "audio_revision": int(manifest_hash[:12], 16) if manifest_hash else 0,
            "manifest_hash": manifest_hash,
            "required_artifact_ids": [value["artifact_id"] for value in required],
            "required_artifacts": required,
            "missing_item_ids": missing,
            "conflict_item_ids": conflicts,
            "reasons": reasons,
        }

    def _configuration_editable_from_connection(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
    ) -> bool:
        """Whether changing the next input target is still side-effect safe.

        An input run is an immutable record once it crosses the external write
        fence. A failed preflight can, however, leave a run with only
        ``REJECTED`` operations (or no operation at all). Keeping that case
        editable is necessary for correcting a bad platform template/session
        without modifying the old run's target snapshot.
        """

        if not _table_exists(con, "input_runs"):
            return True
        runs = con.execute(
            "SELECT input_run_id, status FROM input_runs WHERE workflow_id=?",
            (workflow_id,),
        ).fetchall()
        if not runs:
            return True
        if any(str(row["status"]) not in {"FAILED", "PARTIAL_SUCCESS"} for row in runs):
            return False

        # A partially successful run has at least one external record that
        # must remain tied to its original configuration.
        if _table_exists(con, "input_entries"):
            protected = con.execute(
                """SELECT 1 FROM input_entries
                   WHERE workflow_id=? AND input_status IN ('succeeded', 'running', 'needs_reconcile')
                   LIMIT 1""",
                (workflow_id,),
            ).fetchone()
            if protected is not None:
                return False

        # An operation that is not explicitly rejected remains the recovery
        # authority, even if the aggregate run status was recorded as failed.
        if not _table_exists(con, "external_operations") or not _table_exists(con, "input_attempts"):
            return False
        operations = con.execute(
            """SELECT eo.side_effect_state, ia.status AS attempt_status,
                      ia.error_code AS attempt_error_code,
                      ie.input_status, ie.external_record_id
               FROM external_operations eo
               JOIN input_attempts ia ON ia.external_operation_id=eo.external_operation_id
               JOIN input_runs ir ON ir.input_run_id=ia.input_run_id
               JOIN input_entries ie ON ie.entry_id=ia.entry_id
               WHERE ir.workflow_id=?""",
            (workflow_id,),
        ).fetchall()
        for row in operations:
            if str(row["side_effect_state"]) == "REJECTED":
                continue
            # A page write may be observed after the worker loses its response.
            # Once the external record has been manually confirmed, the local
            # run is retryable against that same record.  This is the only
            # confirmed-operation state that remains editable: the next run
            # must use the existing-record path and can never create a second
            # paper as a configuration correction.
            if (
                str(row["side_effect_state"]) == "CONFIRMED"
                and str(row["attempt_status"]) == "FAILED"
                and str(row["attempt_error_code"]) == "INPUT_PARTIAL_EXTERNAL_RECORD"
                and str(row["input_status"]) == "failed_retryable"
                and (
                    _text(row["external_record_id"], limit=512)
                    # A confirmed partial paper may be deliberately detached
                    # for replacement. Its immutable confirmed operation is
                    # still the audit proof after the entry stops pointing at
                    # the obsolete external ID.
                    or str(row["attempt_error_code"]) == "INPUT_PARTIAL_EXTERNAL_RECORD"
                )
            ):
                continue
            return False
        return True

    def _projection_from_connection(self, con: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
        self._require_available(con)
        workflow = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
        if workflow is None:
            raise NotFoundError(f"workflow does not exist: {workflow_id}")
        config = _json_object(workflow["configuration_snapshot"])
        system_config = validate_system_input_configuration(config, allow_partial=True)
        boundary_decision = _boundary_decision_from_connection(con, workflow_id)
        batch = con.execute("SELECT * FROM audio_batches WHERE workflow_id=?", (workflow_id,)).fetchone()
        units = [dict(row) for row in con.execute(
            "SELECT * FROM input_units WHERE workflow_id=? ORDER BY ordinal, unit_id", (workflow_id,)
        ).fetchall()]
        nodes = [dict(row) for row in con.execute(
            "SELECT * FROM structure_nodes WHERE workflow_id=? ORDER BY unit_id, ordinal, node_id", (workflow_id,)
        ).fetchall()]
        segments = [dict(row) for row in con.execute(
            "SELECT * FROM content_segments WHERE workflow_id=? ORDER BY unit_id, ordinal, segment_id", (workflow_id,)
        ).fetchall()]
        item_metadata = {
            str(row["item_id"]): _json_object(row["metadata_json"])
            for row in con.execute(
                "SELECT item_id, metadata_json FROM work_items WHERE workflow_id=?",
                (workflow_id,),
            ).fetchall()
        }
        gate = self._audio_gate_from_connection(con, workflow_id)
        latest_artifact_by_item = {
            str(row["item_id"]): str(row["artifact_id"])
            for row in con.execute(
                """SELECT a.item_id, a.artifact_id FROM artifacts a
                   WHERE a.workflow_id=? AND a.artifact_type='tts-segment'
                   AND NOT EXISTS (SELECT 1 FROM artifacts newer
                       WHERE newer.workflow_id=a.workflow_id AND newer.item_id=a.item_id
                         AND newer.artifact_type='tts-segment'
                         AND (newer.created_at>a.created_at OR (newer.created_at=a.created_at AND newer.artifact_id>a.artifact_id)))""",
                (workflow_id,),
            ).fetchall()
            if row["item_id"] is not None
        }
        unit_labels = [str(unit["label"]) for unit in units]
        has_saved_target = bool(system_config.get("units")) or any(
            key in system_config for key in _SYSTEM_FORM_KEYS
        )
        for index, row in enumerate(units):
            row["source_range"] = _json_object(row.pop("source_range_json", "{}"))
            row["evidence"] = _json_object(row.pop("evidence_json", "{}"))
            stored_configuration = _json_object(row.pop("configuration_json", "{}"))
            if has_saved_target:
                # A previous version could save the workflow snapshot while
                # leaving the per-unit projection stale when delivery_mode was
                # audio_only. The snapshot is the authoritative saved target;
                # overlay it so reopening that task does not lose the form.
                snapshot_configuration = _unit_configuration(
                    system_config,
                    str(row["unit_id"]),
                    unit_label=row["label"],
                    unit_index=index,
                    unit_total=len(units),
                    unit_labels=unit_labels,
                    input_type=str(row["input_type"]),
                )
                stored_configuration.update(snapshot_configuration)
            row["configuration"] = project_system_input_configuration(stored_configuration)
        for row in nodes:
            row["path"] = _json_value(_json_array(row.pop("path_json", "[]")), fallback=[])
            row["evidence"] = _json_object(row.pop("evidence_json", "{}"))
        for row in segments:
            segment_item_metadata = item_metadata.get(str(row["item_id"]), {})
            answer_raw = row.pop("answer_json", None)
            row["answer"] = _json_value(_json_decoded(answer_raw), fallback=None) if answer_raw else None
            row["audio_artifact_id"] = latest_artifact_by_item.get(str(row["item_id"]))
            row["translation"] = _text(segment_item_metadata.get("translation"), limit=1_000_000) or None
            row["category"] = _text(segment_item_metadata.get("category"), limit=128) or None
            row["filename_stem"] = _text(segment_item_metadata.get("filename_stem"), limit=256) or None
            row["audio_filename_stem"] = _text(segment_item_metadata.get("audio_filename_stem"), limit=256) or None
            row["audio_only_auxiliary"] = segment_item_metadata.get("audio_only_auxiliary") is True
            raw_page_input = segment_item_metadata.get("page_input")
            if raw_page_input is not None:
                try:
                    page_input = sanitize_page_input(raw_page_input)
                except PageInputFactsError:
                    # Keep the review projection readable even when a parser
                    # or an older workspace contains malformed facts. The
                    # start gate still rejects the same facts before any
                    # external side effect.
                    row["page_input_status"] = "invalid"
                else:
                    if page_input is not None:
                        row["page_input"] = page_input
        entries = []
        for row in con.execute(
            """SELECT ie.* FROM input_entries ie
               JOIN input_units iu ON iu.workflow_id=ie.workflow_id AND iu.unit_id=ie.unit_id
               WHERE ie.workflow_id=? ORDER BY iu.ordinal, ie.entry_id""",
            (workflow_id,),
        ).fetchall():
            value = dict(row)
            entry_configuration = _json_object(value.pop("configuration_json", "{}"))
            value["configuration"] = project_system_input_configuration(entry_configuration)
            value["document_name"] = _entry_document_name(
                value.get("input_type"),
                entry_configuration,
                value.get("document_name"),
            )
            # The current platform has no stable review URL.  Hide legacy
            # values from the renderer as well as from new writes, so an old
            # entry cannot make the UI claim that a link is available.
            if _text(value.get("input_type"), limit=32).casefold() in {"paper", "textbook"}:
                value["review_url"] = None
                value["run_review_url"] = None
                if str(value.get("review_url_status") or "").casefold() == "available":
                    value["review_url_status"] = "unavailable"
                    value["review_url_source"] = "platform_not_supported"
            value["requires_reconcile"] = bool(value.get("requires_reconcile"))
            entries.append(value)
        acceptance_rows = [dict(row) for row in con.execute(
            """SELECT * FROM audio_acceptances WHERE workflow_id=?
               ORDER BY updated_at DESC, acceptance_id DESC LIMIT 20""", (workflow_id,)
        ).fetchall()]
        matching_acceptance = next((row for row in acceptance_rows if str(row["artifact_manifest_hash"]) == gate["manifest_hash"]), None)
        prior_accepted = any(str(row["user_status"]) == "accepted" for row in acceptance_rows)
        acceptance = {
            "status": "accepted" if matching_acceptance and matching_acceptance["user_status"] == "accepted" else ("invalidated" if prior_accepted and gate["manifest_hash"] else "pending"),
            "acceptance_id": str(matching_acceptance["acceptance_id"]) if matching_acceptance else None,
            "audio_revision": gate["audio_revision"],
            "manifest_hash": gate["manifest_hash"],
            "accepted_at": matching_acceptance["accepted_at"] if matching_acceptance else None,
        }
        run = con.execute(
            "SELECT * FROM input_runs WHERE workflow_id=? ORDER BY created_at DESC, input_run_id DESC LIMIT 1", (workflow_id,)
        ).fetchone()
        input_run = dict(run) if run is not None else None
        if input_run is not None:
            input_run["target_snapshot"] = _json_object(input_run.pop("target_snapshot_json", "{}"))
            input_run["control"] = self._input_run_control_row(con, str(input_run["input_run_id"]))
            attempt_rows = con.execute(
                """SELECT ia.attempt_id, ia.entry_id, ia.external_operation_id, ia.status,
                          ia.side_effect_state, ia.evidence_json, ia.error_code, ia.error_message,
                          ia.started_at, ia.finished_at, ia.created_at, ia.updated_at
                   FROM input_attempts ia
                   JOIN input_entries ie ON ie.entry_id=ia.entry_id
                   JOIN input_units iu ON iu.unit_id=ie.unit_id
                   WHERE ia.input_run_id=?
                   ORDER BY iu.ordinal, ia.created_at, ia.rowid""",
                (input_run["input_run_id"],),
            ).fetchall()
            input_run["attempts"] = []
            for attempt_row in attempt_rows:
                attempt = {
                    field: attempt_row[field]
                    for field in (
                        "attempt_id", "entry_id", "external_operation_id", "status",
                        "side_effect_state", "error_code", "error_message",
                        "started_at", "finished_at", "created_at", "updated_at",
                    )
                }
                attempt["evidence"] = _json_value(
                    _json_object(attempt_row["evidence_json"]),
                    fallback={},
                ) or {}
                safe_attempt = redact_public_json(attempt)
                if isinstance(safe_attempt, Mapping):
                    input_run["attempts"].append(dict(safe_attempt))
        input_status = "not_enabled"
        if system_config.get("delivery_mode") == "audio_and_input":
            statuses = {str(row["input_status"]) for row in entries}
            if input_run is not None and str(input_run.get("status")) == "RUNNING":
                input_status = "running"
            elif input_run is not None and str(input_run.get("status")) == "SUCCEEDED":
                input_status = "succeeded"
            elif input_run is not None and str(input_run.get("status")) == "AMBIGUOUS":
                input_status = "needs_reconcile"
            elif "pending_config" in statuses or not entries:
                input_status = "pending_config"
            elif "running" in statuses:
                input_status = "running"
            elif statuses and statuses.issubset({"succeeded"}):
                input_status = "succeeded"
            elif "needs_reconcile" in statuses:
                input_status = "needs_reconcile"
            elif "failed_retryable" in statuses or "failed" in statuses:
                input_status = "failed_retryable"
            else:
                input_status = "pending_execute"
        unit_count_status = (
            "multiple_candidate"
            if any(str(unit.get("unit_count_status") or "") == "multiple_candidate" for unit in units)
            else "multiple_confirmed"
            if len(units) > 1
            else units[0]["unit_count_status"]
            if units
            else "single_default"
        )
        projected_input_type = str(
            system_config.get("input_type") or (units[0]["input_type"] if units else "")
        )
        projected_category = _text(
            system_config.get("paper_category")
            or system_config.get("paperCategory")
            or (units[0].get("paper_category") if units else ""),
            limit=64,
        )
        document_entry_metadata = [
            item_metadata.get(str(segment["item_id"]), {})
            for segment in segments
        ]
        page_inputs = [row.get("page_input") for row in document_entry_metadata]
        page_input_auxiliary = [row.get("audio_only_auxiliary") is True for row in document_entry_metadata]
        document_entry = _document_entry_support_for_metadata(
            document_entry_metadata,
            input_type=projected_input_type or "paper",
        )
        suggested_configuration: dict[str, Any] = {
            "input_type": projected_input_type or None,
            "input_type_status": units[0]["input_type_status"] if units else "suggested",
            "paper_category": projected_category or None,
            "paper_category_status": units[0]["paper_category_status"] if units else "not_applicable",
        }
        if projected_input_type == "textbook":
            # 课文表单的字段级建议：文件名 + 解析条目结构。识别结果由
            # 渲染层呈现；用户保存的配置永远优先于这里的建议。
            suggested_configuration["textbook"] = suggest_textbook_configuration(
                _projection_source_filename(con, workflow_id, config),
                list(item_metadata.values()),
            )
        # Once a document has matched one of the supported page-entry shapes,
        # its page facts are required regardless of whether the user later
        # labels the paper as a full listening exam or a special-topic paper.
        # Otherwise a partial page payload could appear runnable under
        # ``题型专项`` and fail only after a run is created in the
        # visible-page executor.
        page_content_status = _page_content_status(
            page_inputs,
            required=projected_input_type == "paper"
            and document_entry.get("supported") is True,
            auxiliary=page_input_auxiliary,
        )
        return {
            "available": True,
            "delivery_mode": system_config.get("delivery_mode", "audio_only"),
            "input_type": system_config.get("input_type") or (units[0]["input_type"] if units else None),
            "input_type_status": units[0]["input_type_status"] if units else "suggested",
            "paper_category": system_config.get("paper_category") or (units[0]["paper_category"] if units else None),
            "paper_category_status": units[0]["paper_category_status"] if units else "not_applicable",
            "parse_coverage_status": "partial" if any(str(unit["parse_coverage_status"]) != "complete" for unit in units) else "complete",
            "unit_count_status": unit_count_status,
            "unit_count_override": (
                boundary_decision.get("mode")
                if boundary_decision is not None
                else system_config.get("unit_count_override")
            ),
            "unit_boundary_decision": boundary_decision,
            "units": units,
            "structure_nodes": nodes,
            "content_segments": segments,
            "page_content_status": page_content_status,
            "document_entry_support": document_entry,
            "suggested_configuration": suggested_configuration,
            "audio_batch": {
                "audio_batch_id": str(batch["audio_batch_id"]) if batch else f"audio-batch-{workflow_id}",
                "audio_revision": gate["audio_revision"],
                "manifest_hash": gate["manifest_hash"],
                "status": "ACCEPTED" if acceptance["status"] == "accepted" else ("READY" if gate["technical_status"] == "passed" else "DRAFT"),
            },
            "audio_gate": gate,
            "audio_acceptance": acceptance,
            "entries": entries,
            "input_run": input_run,
            "input_status": input_status,
            "configuration_editable": self._configuration_editable_from_connection(con, workflow_id),
            "executor_available": self.executor is not None,
            "supported_external_input": bool(
                (system_input_capability(
                    str(system_config.get("input_type") or (units[0]["input_type"] if units else ""))
                ) or {}).get("external_supported")
            ),
            "input_capability": system_input_capability(
                str(system_config.get("input_type") or (units[0]["input_type"] if units else ""))
            ),
            "input_type_capabilities": system_input_capabilities(),
        }

    def get_projection(self, workflow_id: str, *, con: sqlite3.Connection | None = None) -> dict[str, Any]:
        if con is not None:
            return self._projection_from_connection(con, workflow_id)
        with self.database.read_transaction() as read_con:
            if not _table_exists(read_con, "input_units"):
                return {
                    "available": False,
                    "delivery_mode": "audio_only",
                    "input_type": None,
                    "input_status": "not_enabled",
                    "configuration_editable": False,
                    "executor_available": self.executor is not None,
                    "supported_external_input": False,
                    "input_capability": None,
                    "input_type_capabilities": system_input_capabilities(),
                    "units": [], "structure_nodes": [], "content_segments": [], "entries": [],
                    "audio_gate": {"technical_status": "failed", "reasons": {"schema": "MIGRATION_REQUIRED"}},
                    "audio_acceptance": {"status": "pending"},
                    "document_entry_support": document_entry_support([], input_type=""),
                }
            return self._projection_from_connection(read_con, workflow_id)

    def list_recoverable_run_ids(self, *, limit: int = 16) -> list[str]:
        """List user-started runs that still have safe local work pending.

        ``RUNNING`` is included because the desktop backend may restart after
        returning the 202 response or while a page worker is open.  The
        per-operation state machine below remains the authority: unresolved
        external operations are never retried as fresh page submissions.
        A run with a durable stop request is never recovered: the user closed
        the browser or pressed stop, and reopening the platform window against
        that decision is exactly what must not happen.
        """

        bounded_limit = max(1, min(64, int(limit)))
        try:
            with self.database.read_transaction() as con:
                if not _table_exists(con, "input_runs"):
                    return []
                control_join = ""
                stop_filter = (
                    "AND COALESCE(r.error_code, '') NOT IN "
                    "('INPUT_RUN_STOPPED', 'INPUT_BROWSER_CLOSED')"
                )
                if _table_exists(con, "input_run_controls"):
                    control_join = (
                        "LEFT JOIN input_run_controls c ON c.input_run_id=r.input_run_id"
                    )
                    # A pause is a durable user decision too.  Do not create a
                    # worker on backend restart (or on the next dispatcher
                    # tick after the original worker has exited) until the
                    # user explicitly presses resume.  The worker itself also
                    # checkpoints this flag, but excluding it here prevents
                    # a paused run from being re-armed at all.
                    stop_filter += (
                        " AND NOT (COALESCE(c.pause_requested, 0) = 1)"
                        " AND NOT (COALESCE(c.stop_requested, 0) = 1)"
                    )
                rows = con.execute(
                    f"""SELECT r.input_run_id AS input_run_id FROM input_runs r
                       {control_join}
                       WHERE r.status IN ('PENDING', 'RUNNING')
                         {stop_filter}
                       ORDER BY r.created_at, r.input_run_id LIMIT ?""",
                    (bounded_limit,),
                ).fetchall()
                return [str(row["input_run_id"]) for row in rows]
        except (TypeError, ValueError):
            return []

    def list_browser_closed_run_ids(self, *, limit: int = 16) -> list[str]:
        """Return close/stop-marked runs awaiting terminal cleanup.

        The page worker normally finalizes these rows immediately after
        publishing ``INPUT_BROWSER_CLOSED`` or recording a durable stop
        request. Keeping a small read-only scan lets the restart dispatcher
        finish either handoff without ever treating the marker as permission
        to launch a new browser.
        """

        bounded_limit = max(1, min(64, int(limit)))
        try:
            with self.database.read_transaction() as con:
                if not _table_exists(con, "input_runs"):
                    return []
                control_join = ""
                stop_condition = (
                    "(r.status IN ('PENDING', 'RUNNING') AND "
                    "COALESCE(r.error_code, '') IN "
                    "('INPUT_BROWSER_CLOSED', 'INPUT_RUN_STOPPED'))"
                    " OR (r.status <> 'SUCCEEDED' AND "
                    "COALESCE(r.error_code, '') = 'INPUT_BROWSER_CLOSED')"
                )
                if _table_exists(con, "input_run_controls"):
                    control_join = (
                        "LEFT JOIN input_run_controls c ON c.input_run_id=r.input_run_id"
                    )
                    stop_condition += (
                        " OR COALESCE(c.stop_requested, 0) = 1"
                    )
                rows = con.execute(
                    f"""SELECT r.input_run_id FROM input_runs r
                       {control_join}
                       WHERE ({stop_condition})
                       ORDER BY r.updated_at, r.input_run_id LIMIT ?""",
                    (bounded_limit,),
                ).fetchall()
                return [str(row["input_run_id"]) for row in rows]
        except (TypeError, ValueError):
            return []

    def _ensure_entries_in_transaction(self, con: sqlite3.Connection, workflow_id: str, config: Mapping[str, Any]) -> list[dict[str, Any]]:
        canonical = validate_system_input_configuration(config, allow_partial=True)
        units = con.execute("SELECT * FROM input_units WHERE workflow_id=? ORDER BY ordinal, unit_id", (workflow_id,)).fetchall()
        revision = _configuration_revision(config, draft_revision=0)
        now = utc_now()
        unit_labels = [str(unit["label"]) for unit in units]
        if canonical.get("delivery_mode") != "audio_and_input":
            for index, unit in enumerate(units):
                unit_config = _unit_configuration(
                    config,
                    str(unit["unit_id"]),
                    unit_label=unit["label"],
                    unit_index=index,
                    unit_total=len(units),
                    unit_labels=unit_labels,
                    input_type=str(unit["input_type"]),
                )
                con.execute(
                    "UPDATE input_units SET configuration_json=?, updated_at=? WHERE unit_id=? AND workflow_id=?",
                    (canonical_json(unit_config), now, str(unit["unit_id"]), workflow_id),
                )
            con.execute("UPDATE input_entries SET input_status='not_enabled', updated_at=? WHERE workflow_id=? AND input_status NOT IN ('succeeded', 'running')", (now, workflow_id))
            return []
        for index, unit in enumerate(units):
            unit_id = str(unit["unit_id"])
            unit_config = _unit_configuration(
                config,
                unit_id,
                unit_label=unit["label"],
                unit_index=index,
                unit_total=len(units),
                unit_labels=unit_labels,
                input_type=str(unit["input_type"]),
            )
            configured_input_type = _text(canonical.get("input_type"), limit=32)
            input_type_status = (
                "user_override"
                if configured_input_type
                else str(unit["input_type_status"])
            )
            configured_category = _text(
                unit_config.get("paperCategory") or canonical.get("paper_category"),
                limit=64,
            )
            if str(unit["input_type"]) == "paper":
                paper_category = configured_category or unit["paper_category"]
                paper_category_status = (
                    "user_override"
                    if configured_category
                    else str(unit["paper_category_status"])
                )
            else:
                paper_category = None
                paper_category_status = "not_applicable"
            con.execute(
                """UPDATE input_units SET input_type_status=?, paper_category=?,
                   paper_category_status=?, updated_at=?
                   WHERE unit_id=? AND workflow_id=?""",
                (input_type_status, paper_category, paper_category_status, now, unit_id, workflow_id),
            )
            existing = con.execute(
                "SELECT entry_id, input_status FROM input_entries WHERE workflow_id=? AND unit_id=?",
                (workflow_id, unit_id),
            ).fetchone()
            entry_digest = content_hash({"workflow_id": workflow_id, "unit_id": unit_id})
            entry_id = str(existing["entry_id"]) if existing else f"IN-{entry_digest[:12].upper()}"
            complete = _configuration_complete(str(unit["input_type"]), unit_config)
            document_name = _entry_document_name(
                unit["input_type"],
                unit_config,
                config.get("source_filename"),
            )
            existing_status = str(existing["input_status"]) if existing else ""
            status = (
                existing_status
                if existing_status in {"succeeded", "running", "needs_reconcile"}
                else ("pending_execute" if complete else "pending_config")
            )
            if existing:
                con.execute(
                    """UPDATE input_entries SET audio_batch_id=?, input_type=?, document_name=?, unit_label=?,
                       configuration_json=?, configuration_revision=?, structure_revision=?, input_status=?, updated_at=?
                       WHERE entry_id=?""",
                    (str(unit["audio_batch_id"]), str(unit["input_type"]), document_name,
                     str(unit["label"]), canonical_json(unit_config), revision, int(unit["structure_revision"]), status, now, entry_id),
                )
            else:
                con.execute(
                    """INSERT INTO input_entries(
                       entry_id, workflow_id, unit_id, audio_batch_id, input_type, document_name,
                       unit_label, configuration_json, configuration_revision, structure_revision,
                       external_record_mapping_id, external_record_id, input_status, external_status,
                       requires_reconcile, review_url, review_url_status, review_url_source,
                       run_review_url, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (entry_id, workflow_id, unit_id, str(unit["audio_batch_id"]), str(unit["input_type"]),
                     document_name, str(unit["label"]), canonical_json(unit_config),
                     revision, int(unit["structure_revision"]), None, None, status, "UNKNOWN", 0, None,
                     "unknown", None, None, now, now),
                )
            con.execute("UPDATE input_units SET configuration_json=?, updated_at=? WHERE unit_id=? AND workflow_id=?", (canonical_json(unit_config), now, unit_id, workflow_id))
        return [dict(row) for row in con.execute(
            """SELECT ie.* FROM input_entries ie
               JOIN input_units iu ON iu.workflow_id=ie.workflow_id AND iu.unit_id=ie.unit_id
               WHERE ie.workflow_id=? ORDER BY iu.ordinal, ie.entry_id""",
            (workflow_id,),
        ).fetchall()]

    def ensure_entries(self, workflow_id: str, configuration: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self.database.transaction() as con:
            self._require_available(con)
            row = con.execute("SELECT configuration_snapshot FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            config = _json_object(row["configuration_snapshot"])
            if configuration is not None:
                config.update(dict(configuration))
            self._ensure_entries_in_transaction(con, workflow_id, config)
            return self._projection_from_connection(con, workflow_id)

    def save_configuration(
        self,
        workflow_id: str,
        expected_state_version: int,
        configuration: Mapping[str, Any],
        *,
        expected_configuration_revision: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Save only system-input fields, including after TTS is terminal."""

        canonical = validate_system_input_configuration(configuration, allow_partial=True)
        incoming_source = dict(configuration or {}) if isinstance(configuration, Mapping) else {}
        incoming_nested = incoming_source.get("system_input")
        incoming_keys = set(incoming_nested) if isinstance(incoming_nested, Mapping) else set(incoming_source)
        incoming_keys.update(key for key in CONFIGURATION_KEYS if key in incoming_source)
        with self.database.transaction() as con:
            self._require_available(con)
            row = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            if int(row["state_version"]) != int(expected_state_version):
                raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
            raw_current = _json_object(row["configuration_snapshot"])
            current_revision = _configuration_revision(raw_current, draft_revision=int(row["draft_revision"] or 0))
            if expected_configuration_revision is not None and int(expected_configuration_revision) != current_revision:
                raise ConflictError("workflow configuration revision is stale", code="CONFIGURATION_CONFLICT")
            if not self._configuration_editable_from_connection(con, workflow_id):
                raise ConflictError("system-input configuration is frozen after input starts", code="CONFIG_FROZEN")
            if row["execution_state"] not in {"CREATED", "PREPARING", "WAITING_RETRY", "WAITING_USER", "TERMINAL"}:
                raise ConflictError("system-input configuration is frozen while generation is running", code="CONFIG_FROZEN")
            current_canonical = validate_system_input_configuration(raw_current, allow_partial=True)
            # The delivery-mode controls submit a deliberately small patch.
            # Preserve omitted system-input fields so switching between
            # audio-only and audio-plus-input cannot silently discard the
            # already saved unit configuration.
            for key, value in current_canonical.items():
                if key in CONFIGURATION_KEYS and key not in incoming_keys:
                    canonical[key] = value
            if "units" in incoming_keys:
                canonical["units"] = _merge_saved_unit_configurations(
                    current_canonical.get("units", []),
                    canonical.get("units", []),
                )
            if len(canonical.get("units", [])) > 1:
                # The preservation loop above may have copied a legacy
                # single-target name from a snapshot that predates the units
                # array. Once the merged payload is multi-unit, that fallback
                # is no longer a valid configuration field.
                canonical.pop("paperName", None)
                canonical.pop("paper_name", None)
            merged = dict(raw_current)
            for key in CONFIGURATION_KEYS:
                merged.pop(key, None)
            merged.pop("system_input", None)
            merged.update(canonical)
            next_revision = current_revision + 1
            stored = dict(merged)
            stored["_workflow_configuration_revision"] = next_revision
            now = utc_now()
            con.execute(
                """UPDATE workflows SET configuration_snapshot=?, configuration_hash=?,
                   configuration_version=?, draft_revision=draft_revision+1, state_version=state_version+1,
                   updated_at=? WHERE workflow_id=? AND state_version=?""",
                (canonical_json(stored), content_hash(merged), str(row["configuration_version"]), now, workflow_id, expected_state_version),
            )
            self._ensure_entries_in_transaction(con, workflow_id, merged)
            self.repository.events.append_in_transaction(
                con, workflow_id, "SYSTEM_INPUT_CONFIGURATION_SAVED",
                {"configuration_revision": next_revision, "delivery_mode": canonical.get("delivery_mode"), "input_type": canonical.get("input_type")},
                request_id=request_id, actor_type="USER", actor_id="desktop",
            )
            snapshot = _snapshot_from_connection(con, workflow_id)
            self.repository.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
            return {"snapshot": snapshot.as_dict(), "configuration_revision": next_revision, "projection": self._projection_from_connection(con, workflow_id)}

    def reconcile_observed_external_record(
        self,
        workflow_id: str,
        input_run_id: str,
        attempt_id: str,
        external_record_id: str,
        *,
        evidence: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        resolved_by: str = "desktop",
    ) -> dict[str, Any]:
        """Close a confirmed page write as a retryable local partial record.

        The page executor can lose its response after the platform has already
        created a paper.  External reconciliation confirms that immutable
        fact, while this method repairs the local system-input projection
        without pretending that the content-entry run succeeded.  A later
        retry therefore edits the observed paper instead of creating a
        duplicate.
        """

        external_id = _text(external_record_id, limit=512)
        if not external_id:
            raise SystemInputError("核对到的外部录入 ID 不能为空", code="VALIDATION_ERROR")
        safe_evidence = _safe_summary(evidence if isinstance(evidence, Mapping) else {})
        actor = _text(resolved_by, limit=256) or "desktop"
        fixed_message = "已核对到平台上的未完成试卷，下一次重试将打开既有试卷继续录入"
        with self.database.transaction() as con:
            self._require_available(con)
            run = con.execute(
                "SELECT * FROM input_runs WHERE input_run_id=? AND workflow_id=?",
                (input_run_id, workflow_id),
            ).fetchone()
            if run is None:
                raise NotFoundError(f"input run does not exist: {input_run_id}")
            attempt = con.execute(
                """SELECT ia.*, ie.input_status, ie.external_record_mapping_id,
                          ie.external_record_id, ie.external_status
                   FROM input_attempts ia
                   JOIN input_entries ie ON ie.entry_id=ia.entry_id
                   WHERE ia.input_run_id=? AND ia.attempt_id=? AND ie.workflow_id=?""",
                (input_run_id, attempt_id, workflow_id),
            ).fetchone()
            if attempt is None:
                raise NotFoundError(f"input attempt does not exist: {attempt_id}")
            operation_id = _text(attempt["external_operation_id"], limit=256)
            if not operation_id:
                raise SystemInputError(
                    "该录入尝试没有可核对的外部操作",
                    code="INPUT_RECONCILIATION_INVALID",
                )
            operation = con.execute(
                """SELECT external_operation_id, external_record_mapping_id,
                          side_effect_state, receipt_json
                   FROM external_operations WHERE external_operation_id=?""",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise NotFoundError(f"external operation does not exist: {operation_id}")
            if str(operation["side_effect_state"]) != "CONFIRMED":
                raise ConflictError(
                    "请先确认外部操作，再修复本地录入状态",
                    code="INPUT_RECONCILIATION_REQUIRES_CONFIRMED_OPERATION",
                )
            mapping_id = str(operation["external_record_mapping_id"])
            mapping = con.execute(
                "SELECT external_record_id FROM external_records WHERE external_record_mapping_id=?",
                (mapping_id,),
            ).fetchone()
            if mapping is None or str(mapping["external_record_id"] or "") != external_id:
                raise ConflictError(
                    "核对的外部录入 ID 与已确认回执不一致",
                    code="INPUT_RECONCILIATION_ID_CONFLICT",
                )
            try:
                receipt = json.loads(str(operation["receipt_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                receipt = {}
            if not isinstance(receipt, Mapping) or str(receipt.get("external_record_id") or "") != external_id:
                raise ConflictError(
                    "已确认外部操作缺少匹配的录入回执",
                    code="INPUT_RECONCILIATION_ID_CONFLICT",
                )
            if attempt["external_record_mapping_id"] not in (None, mapping_id):
                raise ConflictError(
                    "录入尝试已绑定到其他外部记录",
                    code="INPUT_RECONCILIATION_MAPPING_CONFLICT",
                )
            if attempt["external_record_id"] not in (None, external_id):
                raise ConflictError(
                    "录入尝试已绑定到其他外部录入 ID",
                    code="INPUT_RECONCILIATION_ID_CONFLICT",
                )

            already_reconciled = (
                str(attempt["status"]) == "FAILED"
                and str(attempt["side_effect_state"]) == "CONFIRMED"
                and str(attempt["error_code"]) == "INPUT_PARTIAL_EXTERNAL_RECORD"
                and str(attempt["input_status"]) == "failed_retryable"
                and str(attempt["external_record_id"] or "") == external_id
            )
            if already_reconciled:
                return self._projection_from_connection(con, workflow_id)
            if str(attempt["status"]) not in {"AMBIGUOUS", "NEEDS_RECONCILE"}:
                raise ConflictError(
                    "当前录入尝试不处于待核对状态",
                    code="INPUT_RECONCILIATION_STATE_CONFLICT",
                )

            try:
                prior_evidence = json.loads(str(attempt["evidence_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                prior_evidence = {}
            merged_evidence: dict[str, Any] = dict(prior_evidence) if isinstance(prior_evidence, Mapping) else {}
            merged_evidence["external_record_reconciliation"] = {
                "external_record_id": external_id,
                "resolved_by": actor,
                "evidence": safe_evidence,
            }
            now = utc_now()
            con.execute(
                """UPDATE input_attempts SET status='FAILED', side_effect_state='CONFIRMED',
                   error_code=?, error_message=?, evidence_json=?, finished_at=?, updated_at=?
                   WHERE input_run_id=? AND attempt_id=?""",
                (
                    "INPUT_PARTIAL_EXTERNAL_RECORD",
                    fixed_message,
                    canonical_json(merged_evidence),
                    now,
                    now,
                    input_run_id,
                    attempt_id,
                ),
            )
            con.execute(
                """UPDATE input_entries SET input_status='failed_retryable',
                   external_record_mapping_id=?, external_record_id=?, external_status='SUCCEEDED',
                   requires_reconcile=0, review_url_status='unknown', updated_at=?
                   WHERE entry_id=? AND workflow_id=?""",
                (mapping_id, external_id, now, str(attempt["entry_id"]), workflow_id),
            )

            latest = con.execute(
                """SELECT current.status FROM input_attempts current
                   WHERE current.input_run_id=?
                     AND NOT EXISTS (
                         SELECT 1 FROM input_attempts newer
                         WHERE newer.input_run_id=current.input_run_id
                           AND newer.entry_id=current.entry_id
                           AND (newer.created_at>current.created_at
                                OR (newer.created_at=current.created_at
                                    AND newer.rowid>current.rowid))
                     )""",
                (input_run_id,),
            ).fetchall()
            statuses = {str(row["status"]) for row in latest}
            terminal_statuses = {"SUCCEEDED", "FAILED", "AMBIGUOUS", "NEEDS_RECONCILE"}
            all_terminal = bool(statuses) and statuses.issubset(terminal_statuses)
            if all_terminal and statuses.issubset({"SUCCEEDED"}):
                final_status = "SUCCEEDED"
            elif all_terminal and ("AMBIGUOUS" in statuses or "NEEDS_RECONCILE" in statuses):
                final_status = "AMBIGUOUS"
            elif all_terminal and statuses.issubset({"FAILED"}):
                final_status = "FAILED"
            elif all_terminal and "FAILED" in statuses:
                final_status = "PARTIAL_SUCCESS"
            else:
                final_status = "RUNNING"
            final_error_code = None if final_status == "SUCCEEDED" else "INPUT_PARTIAL_EXTERNAL_RECORD"
            final_error_message = None if final_status == "SUCCEEDED" else fixed_message
            con.execute(
                """UPDATE input_runs SET status=?, error_code=?, error_message=?,
                   finished_at=?, updated_at=? WHERE input_run_id=?""",
                (
                    final_status,
                    final_error_code,
                    final_error_message,
                    now if final_status in {"SUCCEEDED", "FAILED", "PARTIAL_SUCCESS", "AMBIGUOUS"} else None,
                    now,
                    input_run_id,
                ),
            )
            con.execute(
                "UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                (now, workflow_id),
            )
            self.repository.events.append_in_transaction(
                con,
                workflow_id,
                "SYSTEM_INPUT_EXTERNAL_RECORD_RECONCILED",
                {
                    "input_run_id": input_run_id,
                    "attempt_id": attempt_id,
                    "external_operation_id": operation_id,
                    "external_record_id": external_id,
                    "result": "retryable_partial_record",
                    "resolved_by": actor,
                },
                request_id=request_id,
                actor_type="USER",
                actor_id=actor,
            )
            snapshot = _snapshot_from_connection(con, workflow_id)
            self.repository.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
            return self._projection_from_connection(con, workflow_id)

    def resolve_ambiguous_external_operation(
        self,
        workflow_id: str,
        input_run_id: str,
        attempt_id: str,
        decision: str,
        external_record_id: str | None = None,
        *,
        evidence: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        resolved_by: str = "desktop",
    ) -> dict[str, Any]:
        """Resolve a page-write attempt without ever submitting it again.

        A page executor can fail after the external side-effect boundary has
        been crossed.  The old renderer exposed this state as a disabled
        ``START_INPUT`` action, but offered no way to close the recovery loop.
        This command is the single safe entry point for that loop: the user
        either confirms that no external record exists (making the attempt
        retryable) or supplies the observed record id (which is then bound to
        the existing operation before the local projection becomes retryable).
        """

        workflow_key = _text(workflow_id, limit=256)
        run_key = _text(input_run_id, limit=128)
        attempt_key = _text(attempt_id, limit=128)
        resolution = _text(decision, limit=32).upper()
        external_id = _text(external_record_id, limit=512)
        if not workflow_key or not run_key or not attempt_key:
            raise SystemInputError("录入结果核验参数不完整", code="VALIDATION_ERROR")
        if resolution not in {"CONFIRMED", "NOT_SUBMITTED"}:
            raise SystemInputError("不支持的录入结果核验决定", code="VALIDATION_ERROR")
        if resolution == "CONFIRMED" and not external_id:
            raise SystemInputError("已生成的外部录入 ID 不能为空", code="VALIDATION_ERROR")
        if self.external is None:
            raise SystemInputError("外部录入运行时未初始化", code="INPUT_EXTERNAL_RUNTIME_UNAVAILABLE")

        safe_evidence = _safe_summary(evidence if isinstance(evidence, Mapping) else {})
        actor = _text(resolved_by, limit=256) or "desktop"
        with self.database.read_transaction() as con:
            self._require_available(con)
            run = con.execute(
                "SELECT * FROM input_runs WHERE input_run_id=? AND workflow_id=?",
                (run_key, workflow_key),
            ).fetchone()
            if run is None:
                raise NotFoundError(f"input run does not exist: {run_key}")
            attempt = con.execute(
                """SELECT ia.*, ie.input_status, ie.external_record_mapping_id,
                          ie.external_record_id, ie.external_status
                   FROM input_attempts ia
                   JOIN input_entries ie ON ie.entry_id=ia.entry_id
                   WHERE ia.input_run_id=? AND ia.attempt_id=? AND ie.workflow_id=?""",
                (run_key, attempt_key, workflow_key),
            ).fetchone()
            if attempt is None:
                raise NotFoundError(f"input attempt does not exist: {attempt_key}")
            operation_id = _text(attempt["external_operation_id"], limit=256)
            if not operation_id:
                raise SystemInputError("该录入尝试没有可核验的外部操作", code="INPUT_RECONCILIATION_INVALID")
            operation = con.execute(
                """SELECT external_operation_id, external_record_mapping_id,
                          side_effect_state, receipt_json
                   FROM external_operations WHERE external_operation_id=?""",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise NotFoundError(f"external operation does not exist: {operation_id}")
            attempt_status = str(attempt["status"] or "")
            local_status = str(attempt["input_status"] or "")
            operation_state = str(operation["side_effect_state"] or "")
            already_not_submitted = (
                resolution == "NOT_SUBMITTED"
                and operation_state == "REJECTED"
                and attempt_status == "FAILED"
                and str(attempt["side_effect_state"] or "") == "REJECTED"
                and local_status == "failed_retryable"
            )
            if already_not_submitted:
                return self._projection_from_connection(con, workflow_key)
            if attempt_status not in {"AMBIGUOUS", "NEEDS_RECONCILE"}:
                raise ConflictError(
                    "当前录入尝试不处于待核验状态",
                    code="INPUT_RECONCILIATION_STATE_CONFLICT",
                )
            if resolution == "NOT_SUBMITTED" and operation_state != "AMBIGUOUS":
                # A local ambiguous attempt can survive a worker crash even
                # when the external operation itself is still in flight. In
                # that case rejecting it would erase the only durable proof
                # that a page write may still complete; require the external
                # runtime to reach its explicit AMBIGUOUS state first.
                raise ConflictError(
                    "外部录入操作尚未进入可安全判定未提交的状态",
                    code="INPUT_RECONCILIATION_OPERATION_STATE_CONFLICT",
                )
            mapping_id = _text(operation["external_record_mapping_id"], limit=128)
            if not mapping_id:
                raise SystemInputError("外部操作缺少记录映射", code="INPUT_RECONCILIATION_INVALID")

        evidence_payload = {
            "input_run_id": run_key,
            "attempt_id": attempt_key,
            "decision": resolution,
            "external_record_id": external_id or None,
            "evidence": safe_evidence,
        }
        evidence_hash = content_hash(evidence_payload)
        if resolution == "CONFIRMED":
            lease = self.external.acquire_record_lease(
                mapping_id,
                f"reconcile:{run_key}:{attempt_key}",
                ttl_seconds=300,
            )
            try:
                self.external.confirm_operation(
                    operation_id,
                    lease,
                    external_record_id=external_id,
                    evidence_source="desktop-system-input",
                    evidence_hash=evidence_hash,
                    evidence={"source": "desktop-system-input", **safe_evidence},
                )
            finally:
                try:
                    self.external.release_record_lease(lease)
                except Exception:
                    pass
            return self.reconcile_observed_external_record(
                workflow_key,
                run_key,
                attempt_key,
                external_id,
                evidence={"source": "desktop-system-input", **safe_evidence},
                request_id=request_id,
                resolved_by=actor,
            )

        self.external.resolve_operation(
            operation_id,
            decision="NOT_SUBMITTED",
            evidence_source="desktop-system-input",
            evidence_hash=evidence_hash,
            evidence={"source": "desktop-system-input", **safe_evidence},
            resolved_by=actor,
        )
        self._set_run_state(
            run_key,
            run_status="FAILED",
            attempt_id=attempt_key,
            attempt_status="FAILED",
            side_effect_state="REJECTED",
            external_operation_id=operation_id,
            error_code="INPUT_EXTERNAL_NOT_SUBMITTED",
            error_message="已确认平台未生成记录，可以安全重试",
            evidence={"external_resolution": evidence_payload},
            entry_updates={
                "input_status": "failed_retryable",
                "external_record_mapping_id": mapping_id,
                "external_record_id": None,
                # input_entries intentionally uses the narrower execution
                # status vocabulary (UNKNOWN/PENDING/SUCCEEDED/FAILED/
                # AMBIGUOUS). The external-record mapping keeps the richer
                # NOT_FOUND state; the local entry is a retryable execution
                # failure after the user confirmed no paper was created.
                "external_status": "FAILED",
                "requires_reconcile": 0,
                "review_url_status": "unknown",
            },
        )
        with self.database.transaction() as con:
            now = utc_now()
            con.execute(
                "UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                (now, workflow_key),
            )
            self.repository.events.append_in_transaction(
                con,
                workflow_key,
                "SYSTEM_INPUT_EXTERNAL_OPERATION_RESOLVED",
                {
                    "input_run_id": run_key,
                    "attempt_id": attempt_key,
                    "external_operation_id": operation_id,
                    "decision": resolution,
                    "resolved_by": actor,
                },
                request_id=request_id,
                actor_type="USER",
                actor_id=actor,
            )
            snapshot = _snapshot_from_connection(con, workflow_key)
            self.repository.events.write_snapshot_in_transaction(con, workflow_key, snapshot.as_dict())
            return self._projection_from_connection(con, workflow_key)

    def _verification_outcome_target(self, con: sqlite3.Connection, workflow_key: str, run_key: str, attempt_key: str) -> tuple[Any, Any, Any, str, str]:
        """Load the ambiguous run/attempt/entry and its paper title."""

        run = con.execute(
            "SELECT * FROM input_runs WHERE input_run_id=? AND workflow_id=?",
            (run_key, workflow_key),
        ).fetchone()
        if run is None:
            raise NotFoundError(f"input run does not exist: {run_key}")
        attempt = con.execute(
            """SELECT ia.*, ie.input_status, ie.unit_id, ie.configuration_json,
                      ie.external_record_mapping_id, ie.external_record_id, ie.external_status
               FROM input_attempts ia
               JOIN input_entries ie ON ie.entry_id=ia.entry_id
               WHERE ia.input_run_id=? AND ia.attempt_id=? AND ie.workflow_id=?""",
            (run_key, attempt_key, workflow_key),
        ).fetchone()
        if attempt is None:
            raise NotFoundError(f"input attempt does not exist: {attempt_key}")
        if str(attempt["status"]) not in {"AMBIGUOUS", "NEEDS_RECONCILE"}:
            raise ConflictError(
                "当前录入尝试不处于待核验状态",
                code="INPUT_VERIFICATION_STATE_CONFLICT",
            )
        entry_configuration = _json_object(attempt["configuration_json"])
        unit_row = con.execute(
            "SELECT configuration_json, input_type FROM input_units WHERE unit_id=?",
            (attempt["unit_id"],),
        ).fetchone()
        unit_configuration = _json_object(unit_row["configuration_json"]) if unit_row is not None else {}
        target_snapshot = _json_object(run["target_snapshot_json"])
        target_units = target_snapshot.get("units") if isinstance(target_snapshot.get("units"), list) else []
        paper_title = ""
        for source in (
            entry_configuration,
            unit_configuration,
            *(unit.get("configuration") for unit in target_units if isinstance(unit, Mapping)),
        ):
            if not isinstance(source, Mapping):
                continue
            candidate = _text(source.get("paperName") or source.get("paper_name"), limit=256)
            if not candidate:
                # 课文录入单元的核验标题是课文名称（中文）。
                candidate = _text(source.get("textbookNameZh"), limit=256)
            if candidate:
                paper_title = candidate
                break
        if not paper_title:
            raise SystemInputError(
                "当前录入单元缺少试卷/课文名称，无法执行只读核验",
                code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
            )
        input_type = str(unit_row["input_type"] if unit_row is not None else "") or str(
            target_snapshot.get("input_type") or ""
        )
        return run, attempt, unit_row, paper_title, input_type

    def _record_verification_outcome(
        self,
        workflow_key: str,
        run_key: str,
        attempt_key: str,
        outcome: Mapping[str, Any],
        *,
        request_id: str | None,
        resolved_by: str,
    ) -> dict[str, Any]:
        """Persist a terminal verification outcome on the attempt evidence.

        The verification route answers 202 and works in the background, so the
        durable attempt evidence — not the HTTP response — is what the
        renderer polls for. Writing the outcome here keeps every terminal
        path (bound / not submitted / needs manual resolution / failed)
        observable after a restart.
        """

        with self.database.transaction() as con:
            self._require_available(con)
            attempt = con.execute(
                "SELECT evidence_json FROM input_attempts WHERE input_run_id=? AND attempt_id=?",
                (run_key, attempt_key),
            ).fetchone()
            if attempt is None:
                raise NotFoundError(f"input attempt does not exist: {attempt_key}")
            try:
                prior = json.loads(str(attempt["evidence_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                prior = {}
            evidence = dict(prior) if isinstance(prior, Mapping) else {}
            evidence["external_record_verification"] = dict(outcome)
            now = utc_now()
            con.execute(
                """UPDATE input_attempts SET evidence_json=?, updated_at=?
                   WHERE input_run_id=? AND attempt_id=?""",
                (canonical_json(evidence), now, run_key, attempt_key),
            )
            con.execute(
                "UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                (now, workflow_key),
            )
            self.repository.events.append_in_transaction(
                con,
                workflow_key,
                "SYSTEM_INPUT_EXTERNAL_RECORD_VERIFIED",
                {
                    "input_run_id": run_key,
                    "attempt_id": attempt_key,
                    "outcome": _safe_summary(outcome),
                    "resolved_by": resolved_by,
                },
                request_id=request_id,
                actor_type="USER",
                actor_id=resolved_by,
            )
            snapshot = _snapshot_from_connection(con, workflow_key)
            self.repository.events.write_snapshot_in_transaction(con, workflow_key, snapshot.as_dict())
            return self._projection_from_connection(con, workflow_key)

    def verify_ambiguous_external_record(
        self,
        workflow_id: str,
        input_run_id: str,
        attempt_id: str,
        *,
        request_id: str | None = None,
        resolved_by: str = "desktop",
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Resolve an ambiguous page write through a read-only check.

        The user is never asked to find or type the platform record ID. The
        page executor searches the visible paper list for the configured
        title and observes only read-only list responses:

        - exactly one match: bind it and make the attempt retryable against
          that record (the next safe retry edits the existing paper);
        - zero matches: archive the operation as not submitted and open the
          safe retry without any manual editing;
        - several matches or an executor failure: fail closed, record the
          concrete problem on the attempt, and let the user fix that single
          problem instead of re-editing the whole configuration.
        """

        workflow_key = _text(workflow_id, limit=256)
        run_key = _text(input_run_id, limit=128)
        attempt_key = _text(attempt_id, limit=128)
        if not workflow_key or not run_key or not attempt_key:
            raise SystemInputError("只读核验参数不完整", code="VALIDATION_ERROR")
        if self.executor is None:
            raise SystemInputError("页面录入执行器尚未连接", code="INPUT_EXECUTOR_UNAVAILABLE")
        actor = _text(resolved_by, limit=256) or "desktop"
        try:
            if automatic and self._input_run_stop_requested_now(run_key):
                raise SystemInputError(
                    "系统录入已停止，不再自动核验",
                    code="INPUT_RUN_STOPPED",
                )
            # Title resolution runs inside the guarded block as well: a unit
            # whose configuration carries no usable name (for example an old
            # textbook workspace) must produce a durable failed outcome for
            # the renderer instead of a silent six-minute poll timeout.
            with self.database.read_transaction() as con:
                self._require_available(con)
                _, _, _, paper_title, input_type = self._verification_outcome_target(
                    con, workflow_key, run_key, attempt_key,
                )
            if automatic and self._input_run_stop_requested_now(run_key):
                raise SystemInputError(
                    "系统录入已停止，不再自动核验",
                    code="INPUT_RUN_STOPPED",
                )
            verification_payload: dict[str, Any] = {
                "workflow_id": workflow_key,
                "paper_title": paper_title,
                "unit": {"input_type": input_type},
                "target": {"input_type": input_type},
            }
            if automatic:
                # Built-in page adapters invoke this after acquiring their
                # profile lock, closing the stop/launch race. Custom adapters
                # still receive the pre-call durable check above.
                verification_payload["_abort_check"] = lambda: self._input_run_stop_requested_now(run_key)
            try:
                verification = self.executor.verify(verification_payload)
            except SystemInputError:
                raise
            except Exception as exc:
                # Built-in adapters already normalize page failures, but keep
                # the service boundary fail-closed for custom/legacy
                # adapters too. In particular, preserve the original cause
                # so a manually closed browser is still recognized as a stop.
                raise SystemInputError(
                    "只读核验执行失败",
                    code="INPUT_VERIFY_FAILED",
                    details={
                        "error_type": type(exc).__name__,
                        "error_message": _safe_exception_message(exc),
                    },
                ) from exc
            if not isinstance(verification, Mapping):
                raise SystemInputError(
                    "只读核验没有返回可读结果",
                    code="INPUT_EXECUTOR_INVALID_RESULT",
                )
            raw_matches = verification.get("matches")
            if not isinstance(raw_matches, list) or any(
                not isinstance(match, Mapping) for match in raw_matches
            ):
                raise SystemInputError(
                    "只读核验返回的 matches 结构无效",
                    code="INPUT_EXECUTOR_INVALID_RESULT",
                )
            if automatic and self._input_run_stop_requested_now(run_key):
                raise SystemInputError(
                    "系统录入已停止，不再处理自动核验结果",
                    code="INPUT_RUN_STOPPED",
                )
        except SystemInputError as exc:
            error_code = str(getattr(exc, "code", "") or "INPUT_VERIFY_FAILED").strip().upper()
            browser_closed = error_code == "INPUT_BROWSER_CLOSED" or _is_browser_closed_error(exc)
            stopped = error_code == "INPUT_RUN_STOPPED" or (
                automatic and self._input_run_stop_requested_now(run_key)
            )
            if browser_closed or stopped:
                # Closing the visible browser or an explicit stop is terminal
                # even when it happened during a read-only reconciliation.
                if browser_closed:
                    # Persist the close fence before the finalizer so a
                    # process exit between these two transactions cannot
                    # re-arm automatic verification after restart.
                    self._mark_input_run_browser_closed(
                        run_key,
                        reason="平台浏览器窗口被关闭，已停止本次系统录入",
                        request_id=request_id,
                        requested_by=actor,
                    )
                self.finalize_stopped_input_run(
                    run_key,
                    reason=(
                        "平台浏览器窗口被关闭，已停止本次系统录入"
                        if browser_closed
                        else "用户停止了系统录入"
                    ),
                    request_id=request_id,
                    requested_by=actor,
                )
            elif not stopped:
                self._record_verification_outcome(
                    workflow_key,
                    run_key,
                    attempt_key,
                    {
                        "status": "failed",
                        "error_code": error_code,
                        "message": _safe_exception_message(exc),
                    },
                    request_id=request_id,
                    resolved_by=actor,
                )
            raise
        matches = list(verification["matches"])
        match_count = len(matches)
        verification_summary = {
            "source": "page-readonly-verification",
            "paper_title": paper_title,
            "verify_status": _text(verification.get("status"), limit=64),
            "matches_count": match_count,
        }
        if match_count == 1:
            bound_id = _text(matches[0].get("external_record_id"), limit=512)
            if bound_id:
                projection = self.resolve_ambiguous_external_operation(
                    workflow_key,
                    run_key,
                    attempt_key,
                    "CONFIRMED",
                    bound_id,
                    evidence={
                        **verification_summary,
                        "summary": "只读核验发现同名试卷，已自动绑定该记录",
                    },
                    request_id=request_id,
                    resolved_by=actor,
                )
                return projection
        if match_count == 0:
            return self.resolve_ambiguous_external_operation(
                workflow_key,
                run_key,
                attempt_key,
                "NOT_SUBMITTED",
                None,
                evidence={
                    **verification_summary,
                    "summary": "只读核验未发现同名试卷，已自动解除保护并开放安全重试",
                },
                request_id=request_id,
                resolved_by=actor,
            )
        # Several same-title records must not be guessed away, and a found
        # record without an observed id (the textbook list view reports
        # existence only) cannot be bound automatically either. Record the
        # concrete problem on the attempt and surface exactly that to the
        # user instead of pretending the run is safe to retry.
        if match_count == 1:
            verification_message = (
                "平台上找到了同名记录，但本次只读核验没有读到记录 ID；"
                "请打开平台核对后重新核验，或使用下方手动处理"
            )
        else:
            verification_message = (
                f"平台上存在 {match_count} 条同名试卷记录，无法自动判断，请清理多余记录后重新核验"
            )
        outcome = {
            "status": "needs_manual_resolution",
            "message": verification_message,
            "paper_title": paper_title,
            "matches": _safe_summary({"matches": matches}).get("matches") or [],
        }
        self._record_verification_outcome(
            workflow_key,
            run_key,
            attempt_key,
            outcome,
            request_id=request_id,
            resolved_by=actor,
        )
        return self.get_projection(workflow_key)

    def detach_confirmed_external_record_for_replacement(
        self,
        workflow_id: str,
        entry_id: str,
        external_record_id: str,
        expected_state_version: int,
        *,
        evidence: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        resolved_by: str = "desktop",
    ) -> dict[str, Any]:
        """Detach an incompatible confirmed paper while retaining its audit trail.

        A platform paper whose immutable category is ``题型专项`` cannot be
        converted into the configured ``听说考试`` paper. This command does
        not delete or rewrite that external record. It only clears the
        current entry pointer after validating the confirmed partial-write
        evidence, so the next run can create a replacement mapping and paper.
        """

        workflow_key = _text(workflow_id, limit=256)
        entry_key = _text(entry_id, limit=256)
        external_id = _text(external_record_id, limit=512)
        if not workflow_key or not entry_key or not external_id:
            raise SystemInputError("替代试卷核对参数不完整", code="VALIDATION_ERROR")
        safe_evidence = _safe_summary(evidence if isinstance(evidence, Mapping) else {})
        actor = _text(resolved_by, limit=256) or "desktop"
        with self.database.transaction() as con:
            self._require_available(con)
            workflow = con.execute(
                "SELECT * FROM workflows WHERE workflow_id=?",
                (workflow_key,),
            ).fetchone()
            if workflow is None:
                raise NotFoundError(f"workflow does not exist: {workflow_key}")
            if int(workflow["state_version"]) != int(expected_state_version):
                raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
            entry = con.execute(
                "SELECT * FROM input_entries WHERE workflow_id=? AND entry_id=?",
                (workflow_key, entry_key),
            ).fetchone()
            if entry is None:
                raise NotFoundError(f"input entry does not exist: {entry_key}")
            if str(entry["external_record_id"] or "") != external_id:
                raise ConflictError(
                    "当前录入单元没有绑定所提供的外部试卷 ID",
                    code="INPUT_REPLACEMENT_ID_CONFLICT",
                )
            active = con.execute(
                """SELECT input_run_id, status FROM input_runs
                   WHERE workflow_id=? AND status IN ('PENDING', 'RUNNING', 'AMBIGUOUS')
                   LIMIT 1""",
                (workflow_key,),
            ).fetchone()
            if active is not None:
                raise ConflictError(
                    "当前仍有系统录入运行，不能切换替代试卷",
                    code="SYSTEM_INPUT_ALREADY_RUNNING",
                )
            confirmed = con.execute(
                """SELECT ia.attempt_id, ia.external_operation_id,
                          eo.external_record_mapping_id, eo.receipt_json,
                          eo.side_effect_state
                   FROM input_attempts ia
                   JOIN input_runs ir ON ir.input_run_id=ia.input_run_id
                   JOIN external_operations eo ON eo.external_operation_id=ia.external_operation_id
                   WHERE ir.workflow_id=? AND ia.entry_id=?
                     AND ia.status='FAILED'
                     AND ia.error_code='INPUT_PARTIAL_EXTERNAL_RECORD'
                     AND eo.side_effect_state='CONFIRMED'
                   ORDER BY ia.finished_at DESC, ia.attempt_id DESC
                   LIMIT 1""",
                (workflow_key, entry_key),
            ).fetchone()
            if confirmed is None:
                raise ConflictError(
                    "没有找到已确认的部分外部试卷，不能切换替代记录",
                    code="INPUT_REPLACEMENT_REQUIRES_CONFIRMED_PARTIAL",
                )
            mapping_id = str(confirmed["external_record_mapping_id"] or "")
            mapping = con.execute(
                "SELECT external_record_id FROM external_records WHERE external_record_mapping_id=?",
                (mapping_id,),
            ).fetchone()
            if mapping is None or str(mapping["external_record_id"] or "") != external_id:
                raise ConflictError(
                    "外部试卷 ID 与已确认的历史映射不一致",
                    code="INPUT_REPLACEMENT_ID_CONFLICT",
                )
            try:
                receipt = json.loads(str(confirmed["receipt_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                receipt = {}
            if not isinstance(receipt, Mapping) or str(receipt.get("external_record_id") or "") != external_id:
                raise ConflictError(
                    "已确认历史操作缺少匹配的外部试卷回执",
                    code="INPUT_REPLACEMENT_ID_CONFLICT",
                )
            now = utc_now()
            con.execute(
                """UPDATE input_entries SET external_record_mapping_id=NULL,
                   external_record_id=NULL, input_status='failed_retryable',
                   external_status='UNKNOWN', requires_reconcile=0,
                   review_url=NULL, review_url_status='unknown',
                   review_url_source=NULL, run_review_url=NULL, updated_at=?
                   WHERE workflow_id=? AND entry_id=?""",
                (now, workflow_key, entry_key),
            )
            self.repository.events.append_in_transaction(
                con,
                workflow_key,
                "SYSTEM_INPUT_EXTERNAL_RECORD_DETACHED",
                {
                    "entry_id": entry_key,
                    "external_record_mapping_id": mapping_id,
                    "external_record_id": external_id,
                    "confirmed_attempt_id": str(confirmed["attempt_id"]),
                    "replacement_policy": "new_business_mapping",
                    "resolved_by": actor,
                    "evidence": safe_evidence,
                },
                request_id=request_id,
                actor_type="USER",
                actor_id=actor,
            )
            con.execute(
                "UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                (now, workflow_key),
            )
            snapshot = _snapshot_from_connection(con, workflow_key)
            self.repository.events.write_snapshot_in_transaction(con, workflow_key, snapshot.as_dict())
            return self._projection_from_connection(con, workflow_key)

    def accept_audio(self, workflow_id: str, expected_state_version: int, *, request_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as con:
            self._require_available(con)
            row = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            if int(row["state_version"]) != int(expected_state_version):
                raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
            if row["execution_state"] != "TERMINAL" or row["result_status"] not in {"SUCCEEDED", "PARTIAL_SUCCESS"}:
                raise SystemInputError("音频生成尚未完成，不能整批验收", code="AUDIO_NOT_TERMINAL")
            if con.execute("SELECT 1 FROM input_runs WHERE workflow_id=? LIMIT 1", (workflow_id,)).fetchone() is not None:
                raise ConflictError(
                    "系统录入运行已经创建，不能改变其音频批次",
                    code="AUDIO_BATCH_FROZEN",
                )
            gate = self._audio_gate_from_connection(con, workflow_id)
            if gate["technical_status"] != "passed":
                raise SystemInputError("音频技术产物闸门未通过", code="AUDIO_GATE_FAILED", details=gate)
            batch = con.execute("SELECT * FROM audio_batches WHERE workflow_id=?", (workflow_id,)).fetchone()
            if batch is None:
                raise SystemInputError("音频批次投影不存在", code="PERSISTENCE_ERROR")
            existing = con.execute(
                "SELECT * FROM audio_acceptances WHERE workflow_id=? AND audio_revision=? AND artifact_manifest_hash=?",
                (workflow_id, gate["audio_revision"], gate["manifest_hash"]),
            ).fetchone()
            now = utc_now()
            if existing is None:
                acceptance_id = new_id("audio-acceptance")
                con.execute(
                    """INSERT INTO audio_acceptances(
                       acceptance_id, workflow_id, audio_batch_id, audio_revision, artifact_manifest_hash,
                       required_artifact_ids_json, technical_status, user_status, accepted_at,
                       invalidated_at, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (acceptance_id, workflow_id, str(batch["audio_batch_id"]), gate["audio_revision"], gate["manifest_hash"],
                     canonical_json(gate["required_artifact_ids"]), "passed", "accepted", now, None, now, now),
                )
            else:
                acceptance_id = str(existing["acceptance_id"])
                con.execute("UPDATE audio_acceptances SET technical_status='passed', user_status='accepted', accepted_at=COALESCE(accepted_at, ?), updated_at=? WHERE acceptance_id=?", (now, now, acceptance_id))
            con.execute(
                """UPDATE content_segments SET audio_artifact_id=NULL, audio_revision=0
                   WHERE workflow_id=?""",
                (workflow_id,),
            )
            for artifact in gate["required_artifacts"]:
                con.execute(
                    """UPDATE content_segments SET audio_artifact_id=?, audio_revision=?
                       WHERE workflow_id=? AND item_id=?""",
                    (artifact["artifact_id"], gate["audio_revision"], workflow_id, artifact["item_id"]),
                )
            con.execute("UPDATE audio_batches SET audio_revision=?, manifest_hash=?, status='ACCEPTED', updated_at=? WHERE workflow_id=?", (gate["audio_revision"], gate["manifest_hash"], now, workflow_id))
            con.execute("UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=? AND state_version=?", (now, workflow_id, expected_state_version))
            self.repository.events.append_in_transaction(
                con, workflow_id, "AUDIO_BATCH_ACCEPTED",
                {"acceptance_id": acceptance_id, "audio_revision": gate["audio_revision"], "artifact_manifest_hash": gate["manifest_hash"]},
                request_id=request_id, actor_type="USER", actor_id="desktop",
            )
            snapshot = _snapshot_from_connection(con, workflow_id)
            self.repository.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
            return {"snapshot": snapshot.as_dict(), "projection": self._projection_from_connection(con, workflow_id)}

    def _input_target_snapshot(self, con: sqlite3.Connection, workflow_id: str, config: Mapping[str, Any], gate: Mapping[str, Any]) -> dict[str, Any]:
        units = [dict(row) for row in con.execute("SELECT * FROM input_units WHERE workflow_id=? ORDER BY ordinal, unit_id", (workflow_id,)).fetchall()]
        segments = [dict(row) for row in con.execute("SELECT * FROM content_segments WHERE workflow_id=? ORDER BY unit_id, ordinal", (workflow_id,)).fetchall()]
        item_metadata = {
            str(row["item_id"]): _json_object(row["metadata_json"])
            for row in con.execute(
                "SELECT item_id, metadata_json FROM work_items WHERE workflow_id=?",
                (workflow_id,),
            ).fetchall()
        }
        by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
        artifact_by_item = {str(item["item_id"]): str(item["artifact_id"]) for item in gate.get("required_artifacts", [])}
        for segment in segments:
            segment_metadata = item_metadata.get(str(segment["item_id"]), {})
            target_segment = {
                "segment_id": str(segment["segment_id"]),
                "item_id": str(segment["item_id"]),
                "content_item_id": str(segment["content_item_id"]),
                "ordinal": int(segment["ordinal"]),
                "raw_text": str(segment["raw_text"] or ""),
                "tts_text": str(segment["tts_text"] or ""),
                "score": segment["score"],
                "answer": _json_value(_json_decoded(segment["answer_json"])) if segment["answer_json"] else None,
                "audio_artifact_id": artifact_by_item.get(str(segment["item_id"])),
                "category": _text(segment_metadata.get("category"), limit=128),
                "filename_stem": _text(segment_metadata.get("filename_stem"), limit=256),
                "audio_filename_stem": _text(segment_metadata.get("audio_filename_stem"), limit=256),
                "audio_only_auxiliary": segment_metadata.get("audio_only_auxiliary") is True,
                "source_locator": _text(segment["source_locator"], limit=512),
                # 课文页面每条句子内容都带必填的中文译文；它由解析器从
                # 原文下方的“中文：”行提取，属于页面显示事实而不是审计内容。
                "translation": _text(segment_metadata.get("translation"), limit=1_000_000),
            }
            raw_page_input = segment_metadata.get("page_input")
            if raw_page_input is not None:
                try:
                    page_input = sanitize_page_input(raw_page_input)
                except PageInputFactsError as exc:
                    raise SystemInputError(
                        "系统录入页面内容事实无效",
                        code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                        details={"item_id": str(segment["item_id"])},
                    ) from exc
                if page_input is not None:
                    target_segment["page_input"] = page_input
            by_unit[str(segment["unit_id"])].append(target_segment)
        target_units = []
        unit_labels = [str(unit["label"]) for unit in units]
        for index, unit in enumerate(units):
            unit_config = _unit_configuration(
                config,
                str(unit["unit_id"]),
                unit_label=unit["label"],
                unit_index=index,
                unit_total=len(units),
                unit_labels=unit_labels,
                input_type=str(unit["input_type"]),
            )
            target_units.append({
                "unit_id": str(unit["unit_id"]),
                "unit_label": str(unit["label"]),
                "input_type": str(unit["input_type"]),
                "configuration": project_system_input_configuration(unit_config),
                "structure_revision": int(unit["structure_revision"]),
                "segments": by_unit.get(str(unit["unit_id"]), []),
            })
        return {
            "workflow_id": workflow_id,
            "input_type": _text(config.get("input_type"), limit=32),
            "audio_revision": gate["audio_revision"],
            "artifact_manifest_hash": gate["manifest_hash"],
            "units": target_units,
        }

    def start_input(
        self,
        workflow_id: str,
        expected_state_version: int,
        *,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        key = _text(idempotency_key, limit=256)
        if len(key) < 16:
            raise SystemInputError("idempotency_key 至少需要 16 个字符", code="VALIDATION_ERROR")
        with self.database.transaction() as con:
            self._require_available(con)
            existing_run = con.execute("SELECT * FROM input_runs WHERE idempotency_key=?", (key,)).fetchone()
            if existing_run is None and _table_exists(con, "input_run_idempotency_keys"):
                existing_run = con.execute(
                    """SELECT r.* FROM input_runs r
                       JOIN input_run_idempotency_keys k ON k.input_run_id=r.input_run_id
                       WHERE k.idempotency_key=?""",
                    (key,),
                ).fetchone()
            if existing_run is not None:
                if str(existing_run["workflow_id"]) != str(workflow_id):
                    raise ConflictError(
                        "idempotency key is bound to another workflow",
                        code="IDEMPOTENCY_CONFLICT",
                        details={"workflow_id": str(existing_run["workflow_id"])},
                    )
                return {"input_run_id": str(existing_run["input_run_id"]), "projection": self._projection_from_connection(con, workflow_id), "replayed": True}
            row = con.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"workflow does not exist: {workflow_id}")
            if int(row["state_version"]) != int(expected_state_version):
                raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
            if row["execution_state"] != "TERMINAL" or row["result_status"] not in {"SUCCEEDED", "PARTIAL_SUCCESS"}:
                raise SystemInputError("音频生成尚未完成，不能开始系统录入", code="AUDIO_NOT_TERMINAL")
            # Repair workspaces created by the old rerun path before building
            # the immutable input target.  This is deliberately limited to
            # trusted page-image artifact references and keeps the new run
            # local even when the source metadata still points at an ancestor.
            repair_system_input_artifacts_in_transaction(
                con,
                workflow_id,
                now=utc_now(),
            )
            config = _json_object(row["configuration_snapshot"])
            system_config = validate_system_input_configuration(config, allow_partial=False)
            if system_config.get("delivery_mode") != "audio_and_input":
                raise SystemInputError("当前任务未开启生成音频并录入系统", code="SYSTEM_INPUT_NOT_ENABLED")
            input_type = str(system_config.get("input_type") or "")
            capability = system_input_capability(input_type)
            if capability is None or not capability.get("external_supported"):
                raise SystemInputError(
                    (capability or {}).get("reason") or "当前录入类型暂不支持外部录入",
                    code="SYSTEM_INPUT_TYPE_UNSUPPORTED",
                    details={"input_type": input_type},
                )
            entry_metadata_rows = con.execute(
                "SELECT metadata_json FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id",
                (workflow_id,),
            ).fetchall()
            entry_support = _document_entry_support_for_metadata(
                [_json_object(entry["metadata_json"]) for entry in entry_metadata_rows],
                input_type=input_type,
            )
            # Only an explicit supported=true preflight may cross the
            # external-write boundary. Missing or stale status values are
            # normalized to the user-facing “暂未支持” outcome.
            if entry_support.get("supported") is not True:
                raise SystemInputError(
                    entry_support["reason"] or "当前文档不支持系统录入",
                    code="SYSTEM_INPUT_DOCUMENT_UNSUPPORTED",
                    details=entry_support,
                )
            supports_input_type = getattr(self.executor, "supports_input_type", None)
            if callable(supports_input_type) and not supports_input_type(input_type):
                raise SystemInputError(
                    "当前录入类型的页面适配器未连接",
                    code="SYSTEM_INPUT_TYPE_UNSUPPORTED",
                    details={"input_type": input_type},
                )
            units = con.execute("SELECT * FROM input_units WHERE workflow_id=? ORDER BY ordinal, unit_id", (workflow_id,)).fetchall()
            if not units:
                raise SystemInputError("当前文档没有可录入单元", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
            if any(str(unit["unit_count_status"]) == "multiple_candidate" for unit in units):
                raise SystemInputError("录入单元数量仍待确认", code="SYSTEM_INPUT_UNITS_UNCONFIRMED")
            if any(str(unit["input_type_status"]) == "conflict" or str(unit["paper_category_status"]) == "conflict" for unit in units):
                raise SystemInputError("录入类型或试卷分类存在冲突", code="SYSTEM_INPUT_CONFIG_CONFLICT")
            if any(str(unit["input_type_status"]) not in {"confirmed", "user_override"} for unit in units):
                raise SystemInputError(
                    "录入类型还没有完成确认",
                    code="SYSTEM_INPUT_CLASSIFICATION_UNCONFIRMED",
                )
            if input_type == "paper" and any(
                str(unit["paper_category_status"]) not in {"confirmed", "user_override"}
                for unit in units
            ):
                raise SystemInputError(
                    "试卷分类还没有完成确认",
                    code="SYSTEM_INPUT_CLASSIFICATION_UNCONFIRMED",
                )
            if any(str(unit["parse_coverage_status"]) != "complete" for unit in units):
                raise SystemInputError(
                    "解析结果仍有未归类或未完成映射的内容",
                    code="SYSTEM_INPUT_PARSE_INCOMPLETE",
                )
            incomplete_items = con.execute(
                """SELECT item_id, status FROM work_items
                   WHERE workflow_id=? AND status<>? ORDER BY sequence, item_id""",
                (workflow_id, "SUCCEEDED"),
            ).fetchall()
            if incomplete_items:
                raise SystemInputError(
                    "仍有音频条目未成功生成，不能开始系统录入",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                    details={"item_ids": [str(item["item_id"]) for item in incomplete_items[:256]]},
                )
            self._ensure_entries_in_transaction(con, workflow_id, config)
            entries = con.execute(
                """SELECT ie.* FROM input_entries ie
                   JOIN input_units iu ON iu.workflow_id=ie.workflow_id AND iu.unit_id=ie.unit_id
                   WHERE ie.workflow_id=? ORDER BY iu.ordinal, ie.entry_id""",
                (workflow_id,),
            ).fetchall()
            if input_type == "paper" and any(
                not platform_template_reference_complete(_json_object(entry["configuration_json"]))
                for entry in entries
            ):
                # 课文录入没有平台题型模板概念；模板引用只约束试卷。
                raise SystemInputError(
                    "当前平台录入模板已失效或无权访问",
                    code="SYSTEM_INPUT_PLATFORM_TEMPLATE_UNAVAILABLE",
                )
            retryable_entries = {"pending_execute", "failed_retryable", "failed"}
            if not entries or any(
                str(entry["input_status"]) not in retryable_entries | {"succeeded"}
                for entry in entries
            ):
                raise SystemInputError("仍有录入单元配置未完成", code="SYSTEM_INPUT_CONFIG_INCOMPLETE")
            entries_to_run = [
                entry for entry in entries
                if str(entry["input_status"]) in retryable_entries
            ]
            if not entries_to_run:
                raise SystemInputError(
                    "所有录入单元已经完成",
                    code="SYSTEM_INPUT_ALREADY_COMPLETE",
                )
            gate = self._audio_gate_from_connection(con, workflow_id)
            if gate["technical_status"] != "passed":
                raise SystemInputError("音频技术产物闸门未通过", code="AUDIO_GATE_FAILED", details=gate)
            accepted = con.execute(
                "SELECT 1 FROM audio_acceptances WHERE workflow_id=? AND audio_revision=? AND artifact_manifest_hash=? AND user_status='accepted'",
                (workflow_id, gate["audio_revision"], gate["manifest_hash"]),
            ).fetchone()
            if accepted is None:
                raise SystemInputError("请先完成整批音频验收", code="AUDIO_ACCEPTANCE_REQUIRED")
            if con.execute("SELECT 1 FROM input_runs WHERE workflow_id=? AND status IN ('PENDING', 'RUNNING', 'AMBIGUOUS') LIMIT 1", (workflow_id,)).fetchone() is not None:
                raise ConflictError("当前已有系统录入运行", code="SYSTEM_INPUT_ALREADY_RUNNING")
            target = self._input_target_snapshot(con, workflow_id, config, gate)
            # Freeze the same parser-owned document gate that authorized run
            # creation. Recovery must validate both this immutable fact and
            # the current workspace before an external side effect.
            target["document_entry_support"] = dict(entry_support)
            page_content_status = _page_content_status(
                [
                    segment.get("page_input")
                    for unit in target["units"]
                    for segment in unit.get("segments", [])
                ],
                # A recognized supported document shape always needs its
                # page facts before the external write, even for a
                # ``题型专项`` paper.  The document preflight above is the
                # single source for this requirement.
                required=input_type == "paper" and entry_support.get("supported") is True,
                auxiliary=[
                    segment.get("audio_only_auxiliary") is True
                    for unit in target["units"]
                    for segment in unit.get("segments", [])
                ],
            )
            if page_content_status["status"] not in {"complete", "not_required"}:
                raise SystemInputError(
                    page_content_status["reason"] or "页面内容事实不完整，不能开始录入",
                    code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                    details=page_content_status,
                )
            for target_unit in target["units"]:
                target_segments = target_unit.get("segments")
                if not isinstance(target_segments, list) or not target_segments:
                    raise SystemInputError(
                        "录入单元没有完整的内容片段",
                        code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                        details={"unit_id": target_unit.get("unit_id")},
                    )
                for target_segment in target_segments:
                    if not _text(target_segment.get("raw_text") or target_segment.get("tts_text"), limit=1_000_000):
                        raise SystemInputError(
                            "内容片段缺少听力原文",
                            code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                            details={"item_id": target_segment.get("item_id")},
                        )
                    if (
                        input_type == "paper"
                        and target_segment.get("audio_only_auxiliary") is not True
                        and target_segment.get("score") is None
                    ):
                        # 平台试卷页面要求逐题分值；课文页面没有分值概念。
                        raise SystemInputError(
                            "内容片段缺少分数",
                            code="SYSTEM_INPUT_CONTENT_INCOMPLETE",
                            details={"item_id": target_segment.get("item_id")},
                        )
                    if not _text(target_segment.get("audio_artifact_id"), limit=256):
                        raise SystemInputError(
                            "内容片段缺少原文音频产物",
                            code="AUDIO_GATE_FAILED",
                            details={"item_id": target_segment.get("item_id")},
                        )
            payload_hash = content_hash(target)
            now = utc_now()
            config_revision = _configuration_revision(config, draft_revision=int(row["draft_revision"] or 0))
            retry_run = con.execute(
                """SELECT * FROM input_runs
                   WHERE workflow_id=? AND status IN ('FAILED', 'PARTIAL_SUCCESS')
                   ORDER BY created_at DESC, input_run_id DESC LIMIT 1""",
                (workflow_id,),
            ).fetchone()
            reuse_run = bool(
                retry_run is not None
                and int(retry_run["audio_revision"]) == int(gate["audio_revision"])
                and str(retry_run["artifact_manifest_hash"]) == str(gate["manifest_hash"])
                and int(retry_run["configuration_revision"]) == int(config_revision)
                and str(retry_run["payload_hash"]) == payload_hash
            )
            run_id = str(retry_run["input_run_id"]) if reuse_run else new_id("input-run")
            if reuse_run:
                # A new explicit retry is a fresh control session.  Normally
                # terminal runs already have their control row deleted, but a
                # process can stop between the browser-close marker and its
                # finalizer; do not let that stale stop/pause flag immediately
                # re-stop the user's deliberate retry.
                if _table_exists(con, "input_run_controls"):
                    con.execute(
                        "DELETE FROM input_run_controls WHERE input_run_id=?",
                        (run_id,),
                    )
                con.execute(
                    """UPDATE input_runs SET status='PENDING', error_code=NULL, error_message=NULL,
                       finished_at=NULL, updated_at=? WHERE input_run_id=?""",
                    (now, run_id),
                )
                event_name = "SYSTEM_INPUT_RUN_RETRY_CREATED"
            else:
                con.execute(
                    """INSERT INTO input_runs(input_run_id, workflow_id, input_type, status, audio_revision,
                       artifact_manifest_hash, configuration_revision, target_snapshot_json, payload_hash,
                       idempotency_key, error_code, error_message, started_at, finished_at, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, workflow_id, input_type, "PENDING", gate["audio_revision"], gate["manifest_hash"],
                     config_revision, canonical_json(target), payload_hash, key, None, None, None, None, now, now),
                )
                event_name = "SYSTEM_INPUT_RUN_CREATED"
            if _table_exists(con, "input_run_idempotency_keys"):
                con.execute(
                    "INSERT INTO input_run_idempotency_keys(idempotency_key, input_run_id, created_at) VALUES (?,?,?)",
                    (key, run_id, now),
                )
            for entry in entries_to_run:
                attempt_id = new_id("input-attempt")
                con.execute(
                    """INSERT INTO input_attempts(
                       attempt_id, input_run_id, entry_id, external_operation_id, status,
                       side_effect_state, idempotency_key, evidence_json, error_code, error_message,
                       started_at, finished_at, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (attempt_id, run_id, str(entry["entry_id"]), None, "PENDING", "NOT_STARTED",
                     f"{key}:{entry['entry_id']}:{attempt_id}", "{}", None, None, None, None, now, now),
                )
            con.execute("UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=? AND state_version=?", (now, workflow_id, expected_state_version))
            self.repository.events.append_in_transaction(
                con, workflow_id, event_name,
                {"input_run_id": run_id, "input_type": input_type, "unit_count": len(entries_to_run), "artifact_manifest_hash": gate["manifest_hash"], "retry": reuse_run},
                request_id=request_id, actor_type="USER", actor_id="desktop",
            )
            snapshot = _snapshot_from_connection(con, workflow_id)
            self.repository.events.write_snapshot_in_transaction(con, workflow_id, snapshot.as_dict())
            return {"input_run_id": run_id, "snapshot": snapshot.as_dict(), "projection": self._projection_from_connection(con, workflow_id), "replayed": False, "retry": reuse_run}

    def _set_run_state(
        self,
        input_run_id: str,
        *,
        run_status: str,
        attempt_id: str,
        attempt_status: str,
        side_effect_state: str,
        external_operation_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        entry_updates: Mapping[str, Any] | None = None,
        force_run_status: bool = False,
    ) -> None:
        now = utc_now()
        with self.database.transaction() as con:
            attempt = con.execute("SELECT entry_id FROM input_attempts WHERE input_run_id=? AND attempt_id=?", (input_run_id, attempt_id)).fetchone()
            if attempt is None:
                raise NotFoundError(f"input attempt does not exist: {attempt_id}")
            if external_operation_id is None:
                con.execute(
                    """UPDATE input_attempts SET status=?, side_effect_state=?, error_code=?, error_message=?,
                       evidence_json=?, started_at=COALESCE(started_at, ?), finished_at=?, updated_at=?
                       WHERE input_run_id=? AND attempt_id=?""",
                    (attempt_status, side_effect_state, error_code, error_message, canonical_json(dict(evidence or {})), now,
                     now if attempt_status in {"SUCCEEDED", "FAILED", "AMBIGUOUS", "NEEDS_RECONCILE"} else None, now, input_run_id, attempt_id),
                )
            else:
                con.execute(
                    """UPDATE input_attempts SET external_operation_id=?, status=?, side_effect_state=?,
                       error_code=?, error_message=?, evidence_json=?, started_at=COALESCE(started_at, ?),
                       finished_at=?, updated_at=? WHERE input_run_id=? AND attempt_id=?""",
                    (external_operation_id, attempt_status, side_effect_state, error_code, error_message,
                     canonical_json(dict(evidence or {})), now,
                     now if attempt_status in {"SUCCEEDED", "FAILED", "AMBIGUOUS", "NEEDS_RECONCILE"} else None,
                     now, input_run_id, attempt_id),
                )
            update = dict(entry_updates or {})
            if update:
                assignments = []
                params: list[Any] = []
                for field in ("input_status", "external_record_mapping_id", "external_record_id", "external_status", "requires_reconcile", "review_url", "review_url_status", "review_url_source", "run_review_url"):
                    if field in update:
                        assignments.append(f"{field}=?")
                        params.append(update[field])
                if assignments:
                    assignments.append("updated_at=?")
                    params.extend([now, str(attempt["entry_id"])])
                    con.execute(f"UPDATE input_entries SET {', '.join(assignments)} WHERE entry_id=?", params)
            # A safe retry appends a new attempt for the same entry.  Roll up
            # only the latest attempt per entry; historical FAILED attempts
            # must not keep a successfully retried run in PARTIAL_SUCCESS.
            remaining = con.execute(
                """SELECT current.status FROM input_attempts current
                   WHERE current.input_run_id=?
                     AND NOT EXISTS (
                         SELECT 1 FROM input_attempts newer
                         WHERE newer.input_run_id=current.input_run_id
                           AND newer.entry_id=current.entry_id
                           AND (newer.created_at>current.created_at
                                OR (newer.created_at=current.created_at
                                    AND newer.rowid>current.rowid))
                     )""",
                (input_run_id,),
            ).fetchall()
            statuses = {str(row["status"]) for row in remaining}
            terminal_statuses = {"SUCCEEDED", "FAILED", "AMBIGUOUS", "NEEDS_RECONCILE"}
            all_terminal = bool(statuses) and statuses.issubset(terminal_statuses)
            if force_run_status:
                final_run_status = run_status
            elif all_terminal and statuses.issubset({"SUCCEEDED"}):
                final_run_status = "SUCCEEDED"
            elif all_terminal and ("AMBIGUOUS" in statuses or "NEEDS_RECONCILE" in statuses):
                final_run_status = "AMBIGUOUS"
            elif all_terminal and statuses.issubset({"FAILED"}):
                final_run_status = "FAILED"
            elif all_terminal and "FAILED" in statuses:
                final_run_status = "PARTIAL_SUCCESS"
            else:
                final_run_status = run_status
            con.execute("UPDATE input_runs SET status=?, error_code=?, error_message=?, finished_at=?, updated_at=? WHERE input_run_id=?", (final_run_status, error_code, error_message, now if final_run_status in {"SUCCEEDED", "FAILED", "PARTIAL_SUCCESS", "AMBIGUOUS"} else None, now, input_run_id))
            run_terminal_statuses = terminal_statuses | {"PARTIAL_SUCCESS"}
            if final_run_status in run_terminal_statuses and _table_exists(con, "input_run_controls"):
                # Pause is a property of the active worker, not of a future
                # retry.  Keep an explicit stop marker for the follow-up
                # finalizer, but do not leave an ordinary terminal failure
                # permanently paused on the next explicit retry.  A stop
                # request that arrived while the final page was still in
                # flight must survive this roll-up; otherwise the successful
                # page would erase the user's stop before the worker reaches
                # its post-page checkpoint.
                control = self._input_run_control_row(con, input_run_id)
                if not control.get("stop_requested"):
                    con.execute(
                        "DELETE FROM input_run_controls WHERE input_run_id=?",
                        (input_run_id,),
                    )

    _INPUT_RUN_CONTROL_ACTIONS = ("pause", "resume", "stop")

    def _input_run_boundary_lock(self, input_run_id: str) -> threading.RLock:
        run_key = _text(input_run_id, limit=128) or "__invalid__"
        with self._input_boundary_locks_guard:
            lock = self._input_boundary_locks.get(run_key)
            if lock is None:
                lock = threading.RLock()
                self._input_boundary_locks[run_key] = lock
            return lock

    def _input_run_control_row(self, con: sqlite3.Connection, input_run_id: str) -> dict[str, bool]:
        if not _table_exists(con, "input_run_controls"):
            return {"pause_requested": False, "stop_requested": False}
        row = con.execute(
            "SELECT pause_requested, stop_requested FROM input_run_controls WHERE input_run_id=?",
            (input_run_id,),
        ).fetchone()
        if row is None:
            return {"pause_requested": False, "stop_requested": False}
        return {
            "pause_requested": bool(row["pause_requested"]),
            "stop_requested": bool(row["stop_requested"]),
        }

    def _input_run_control_now(self, input_run_id: str) -> dict[str, bool]:
        with self.database.read_transaction() as con:
            return self._input_run_control_row(con, input_run_id)

    def _clear_input_run_control(self, input_run_id: str) -> None:
        """Clear a late control request after the run has already completed."""

        with self.database.transaction() as con:
            if _table_exists(con, "input_run_controls"):
                con.execute(
                    "DELETE FROM input_run_controls WHERE input_run_id=?",
                    (input_run_id,),
                )

    def _input_run_stop_requested_now(self, input_run_id: str) -> bool:
        """Return whether a run has already been explicitly stopped.

        Automatic read-only reconciliation runs in its own worker and can be
        queued behind the shared browser-profile lock. Checking the durable
        run marker both before submitting that worker and again after the
        lock is acquired prevents a stop/close decision from being followed
        by a fresh browser launch.
        """

        run_key = _text(input_run_id, limit=128)
        if not run_key:
            return False
        with self.database.read_transaction() as con:
            if not _table_exists(con, "input_runs"):
                return False
            run = con.execute(
                "SELECT status, error_code FROM input_runs WHERE input_run_id=?",
                (run_key,),
            ).fetchone()
            if run is None:
                return False
            control = self._input_run_control_row(con, run_key)
            error_code = str(run["error_code"] or "").strip().upper()
            return bool(control.get("stop_requested")) or error_code in {
                "INPUT_RUN_STOPPED",
                "INPUT_BROWSER_CLOSED",
            }

    def _mark_input_run_browser_closed(
        self,
        input_run_id: str,
        *,
        reason: str | None = None,
        request_id: str | None = None,
        requested_by: str = "system-input",
    ) -> None:
        """Persist a browser-close fence before attempting terminal cleanup.

        Read-only reconciliation runs in a separate worker from the input
        writer.  If the browser is closed and the process exits while the
        finalizer is between transactions, the close decision still has to
        survive a restart; otherwise the automatic verifier could open a new
        browser against the user's explicit stop.
        """

        run_key = _text(input_run_id, limit=128)
        if not run_key:
            return
        note = _text(reason, limit=512) or "平台浏览器窗口被关闭，已停止本次系统录入"
        actor = _text(requested_by, limit=256) or "system-input"
        with self.database.transaction() as con:
            self._require_available(con)
            run = con.execute(
                "SELECT workflow_id, status, error_code FROM input_runs WHERE input_run_id=?",
                (run_key,),
            ).fetchone()
            if run is None:
                raise NotFoundError(f"input run does not exist: {run_key}")
            if str(run["status"] or "") == "SUCCEEDED":
                return
            current_error = str(run["error_code"] or "").strip().upper()
            if current_error in {"INPUT_BROWSER_CLOSED", "INPUT_RUN_STOPPED"}:
                return
            now = utc_now()
            con.execute(
                """UPDATE input_runs SET error_code='INPUT_BROWSER_CLOSED',
                   error_message=?, updated_at=? WHERE input_run_id=?""",
                (note, now, run_key),
            )
            workflow_key = str(run["workflow_id"])
            con.execute(
                "UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                (now, workflow_key),
            )
            self.repository.events.append_in_transaction(
                con,
                workflow_key,
                "SYSTEM_INPUT_BROWSER_CLOSED",
                {
                    "input_run_id": run_key,
                    "error_code": "INPUT_BROWSER_CLOSED",
                    "message": note,
                    "resolved_by": actor,
                },
                request_id=request_id,
                actor_type="SYSTEM",
                actor_id=actor,
            )
            snapshot = _snapshot_from_connection(con, workflow_key)
            self.repository.events.write_snapshot_in_transaction(con, workflow_key, snapshot.as_dict())

    def request_input_run_control(
        self,
        workflow_id: str,
        input_run_id: str,
        *,
        action: str,
        reason: str | None = None,
        request_id: str | None = None,
        requested_by: str = "user",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        """Set a durable pause/stop/resume flag on an active input run.

        A page write cannot be interrupted halfway, so the adapter consumes
        both commands at safe page checkpoints: pause parks the worker after
        the current browser action (and between textbook sentences), while
        stop lets that atomic action finish and then finalizes the run.
        Entries that were never attempted keep their retryable state, so a
        later ``start_input`` continues exactly where the run left off.
        """

        workflow_key = _text(workflow_id, limit=256)
        run_key = _text(input_run_id, limit=128)
        control_action = _text(action, limit=16).lower()
        if not workflow_key or not run_key:
            raise SystemInputError("录入运行控制参数不完整", code="VALIDATION_ERROR")
        if control_action not in self._INPUT_RUN_CONTROL_ACTIONS:
            raise SystemInputError("不支持的录入运行控制动作", code="VALIDATION_ERROR")
        actor = _text(requested_by, limit=256) or "user"
        note = _text(reason, limit=512) or None
        # Enter the same per-run boundary lock used immediately around
        # begin_operation().  A request that wins first is visible before a
        # new page operation starts; a request that arrives second is applied
        # after that in-flight operation and is consumed at the next unit.
        with self._input_run_boundary_lock(run_key), self.database.transaction() as con:
            self._require_available(con)
            workflow = con.execute(
                "SELECT state_version FROM workflows WHERE workflow_id=?",
                (workflow_key,),
            ).fetchone()
            if workflow is None:
                raise NotFoundError(f"workflow does not exist: {workflow_key}")
            if (
                expected_state_version is not None
                and int(workflow["state_version"]) != int(expected_state_version)
            ):
                raise ConflictError("workflow state_version is stale", code="STATE_CONFLICT")
            run = con.execute(
                "SELECT status FROM input_runs WHERE input_run_id=? AND workflow_id=?",
                (run_key, workflow_key),
            ).fetchone()
            if run is None:
                raise NotFoundError(f"input run does not exist: {run_key}")
            if str(run["status"]) not in {"PENDING", "RUNNING"}:
                raise ConflictError(
                    "当前录入运行已结束，不能暂停或停止",
                    code="INPUT_RUN_CONTROL_STATE_CONFLICT",
                )
            now = utc_now()
            control = self._input_run_control_row(con, run_key)
            pause_requested = control["pause_requested"]
            stop_requested = control["stop_requested"]
            if stop_requested and control_action != "stop":
                raise ConflictError(
                    "当前录入运行已请求停止，不能暂停或恢复",
                    code="INPUT_RUN_CONTROL_STATE_CONFLICT",
                )
            if control_action == "pause":
                pause_requested = True
            elif control_action == "resume":
                pause_requested = False
            else:
                stop_requested = True
            con.execute(
                """INSERT INTO input_run_controls(
                       input_run_id, pause_requested, stop_requested, reason,
                       requested_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(input_run_id) DO UPDATE SET
                       pause_requested=excluded.pause_requested,
                       stop_requested=excluded.stop_requested,
                       reason=excluded.reason,
                       requested_by=excluded.requested_by,
                       updated_at=excluded.updated_at""",
                (run_key, int(pause_requested), int(stop_requested), note, actor, now, now),
            )
            con.execute(
                "UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                (now, workflow_key),
            )
            self.repository.events.append_in_transaction(
                con,
                workflow_key,
                "SYSTEM_INPUT_RUN_CONTROLLED",
                {
                    "input_run_id": run_key,
                    "action": control_action,
                    "pause_requested": int(pause_requested),
                    "stop_requested": int(stop_requested),
                    "reason": note,
                    "resolved_by": actor,
                },
                request_id=request_id,
                actor_type="USER",
                actor_id=actor,
            )
            snapshot = _snapshot_from_connection(con, workflow_key)
            self.repository.events.write_snapshot_in_transaction(con, workflow_key, snapshot.as_dict())
            return {
                "action": control_action,
                "control": {"pause_requested": pause_requested, "stop_requested": stop_requested},
                "projection": self._projection_from_connection(con, workflow_key),
            }

    def finalize_stopped_input_run(
        self,
        input_run_id: str,
        *,
        reason: str | None = None,
        request_id: str | None = None,
        requested_by: str = "user",
    ) -> dict[str, Any]:
        """End an active run as user-stopped without touching pending units.

        A genuine successful run and an already-finalized user stop are
        returned untouched. Other terminal-looking states can still be the
        result of the browser-close exception being rolled up just before
        this method, so they are converted to an explicit stop. Entries the
        run never attempted keep ``pending_execute``/``failed_retryable``, so
        a later explicit retry can continue with the remaining units. The run
        carries ``INPUT_RUN_STOPPED`` so the renderer can distinguish an
        intentional stop (browser closed, stop button) from an ordinary
        failure and must not fire automatic read-only verification against
        the user's wishes.
        """

        run_key = _text(input_run_id, limit=128)
        actor = _text(requested_by, limit=256) or "user"
        note = _text(reason, limit=512) or "用户停止了系统录入"
        with self.database.transaction() as con:
            self._require_available(con)
            run = con.execute("SELECT * FROM input_runs WHERE input_run_id=?", (run_key,)).fetchone()
            if run is None:
                raise NotFoundError(f"input run does not exist: {run_key}")
            workflow_key = str(run["workflow_id"])
            control = self._input_run_control_row(con, run_key)
            stop_requested = bool(control.get("stop_requested"))
            # A browser-close path can first roll a single-unit/preflight
            # failure up to FAILED (or a mixed run up to PARTIAL_SUCCESS)
            # before this finalizer is reached. Those are still intentional
            # stops. Preserve only a genuine successful run with no pending
            # stop request and an already finalized user stop as immutable
            # terminal states.
            preserve_terminal = (
                str(run["status"]) == "SUCCEEDED" and not stop_requested
                or (
                    str(run["status"]) == "FAILED"
                    and str(run["error_code"] or "") == "INPUT_RUN_STOPPED"
                )
            )
            if preserve_terminal:
                if _table_exists(con, "input_run_controls"):
                    con.execute("DELETE FROM input_run_controls WHERE input_run_id=?", (run_key,))
                return self._projection_from_connection(con, workflow_key)
            now = utc_now()
            con.execute(
                """UPDATE input_runs SET status='FAILED', error_code='INPUT_RUN_STOPPED',
                   error_message=?, finished_at=?, updated_at=? WHERE input_run_id=?""",
                (note, now, now, run_key),
            )
            if _table_exists(con, "input_run_controls"):
                con.execute("DELETE FROM input_run_controls WHERE input_run_id=?", (run_key,))
            con.execute(
                "UPDATE workflows SET state_version=state_version+1, updated_at=? WHERE workflow_id=?",
                (now, workflow_key),
            )
            self.repository.events.append_in_transaction(
                con,
                workflow_key,
                "SYSTEM_INPUT_RUN_STOPPED",
                {
                    "input_run_id": run_key,
                    "reason": note,
                    "resolved_by": actor,
                },
                request_id=request_id,
                actor_type="USER",
                actor_id=actor,
            )
            snapshot = _snapshot_from_connection(con, workflow_key)
            self.repository.events.write_snapshot_in_transaction(con, workflow_key, snapshot.as_dict())
            return self._projection_from_connection(con, workflow_key)

    def _park_for_input_run_control(self, input_run_id: str, *, lease: Any = None) -> tuple[str, Any]:
        """Park until resumed or stopped, renewing an active page lease.

        Returns ``("stop", current_lease)`` when the park ended because of a
        stop request and ``("resume", current_lease)`` when the pause flag was
        cleared. A page-level pause can happen while the adapter has already
        created a platform-side draft, so keep its external lease alive while
        waiting and return any renewed lease to the caller. The stop/pause
        poll interval is deliberately short enough for the UI to feel
        responsive.
        """

        current_lease = lease
        last_lease_refresh = time.monotonic()
        while True:
            time.sleep(1.0)
            if (
                current_lease is not None
                and self.external is not None
                and time.monotonic() - last_lease_refresh >= 20.0
            ):
                renew = getattr(self.external, "renew_record_lease", None)
                if callable(renew):
                    try:
                        current_lease = renew(current_lease, ttl_seconds=300)
                    except Exception:
                        # The final repository fence remains authoritative. A
                        # failed refresh must not claim that a page write was
                        # safely undone.
                        pass
                last_lease_refresh = time.monotonic()
            control = self._input_run_control_now(input_run_id)
            if control.get("stop_requested"):
                return "stop", current_lease
            if not control.get("pause_requested"):
                return "resume", current_lease

    def execute_input_run(self, input_run_id: str) -> dict[str, Any]:
        """Run an injected page executor with durable per-unit fencing.

        Active runs are intentionally unusable without an executor. A stopped
        or already-completed run may still be replayed for a read-only
        projection/terminal cleanup, but it must never be mistaken for a new
        external write.
        """

        entry_gate_error: tuple[str, str, dict[str, Any]] | None = None
        with self.database.read_transaction() as con:
            run = con.execute("SELECT * FROM input_runs WHERE input_run_id=?", (input_run_id,)).fetchone()
            if run is None:
                raise NotFoundError(f"input run does not exist: {input_run_id}")
            workflow_id = str(run["workflow_id"])
            run_status = str(run["status"] or "")
            run_error_code = str(run["error_code"] or "").strip().upper()
            stop_requested = self._input_run_control_row(con, input_run_id).get("stop_requested", False)
            if (run_status == "SUCCEEDED" and not stop_requested) or (
                run_status == "AMBIGUOUS"
                and run_error_code not in {"INPUT_RUN_STOPPED", "INPUT_BROWSER_CLOSED"}
            ):
                # A replay of the original start command is allowed to read
                # the stopped projection, but it must never re-enter the
                # worker.  This matters when the HTTP response is retried
                # after the browser-close finalizer has already persisted its
                # terminal marker.
                return self.get_projection(workflow_id)
            if run_error_code in {"INPUT_RUN_STOPPED", "INPUT_BROWSER_CLOSED"} or stop_requested:
                # The marker is the durable stop fence.  It can be written
                # before the finalizer while the row still says PENDING or
                # RUNNING; a restart/replay must drain it directly and never
                # re-enter the page executor or open another browser.
                return self.finalize_stopped_input_run(
                    input_run_id,
                    reason=(
                        "平台浏览器窗口被关闭，已停止本次系统录入"
                        if run_error_code == "INPUT_BROWSER_CLOSED"
                        else "用户停止了系统录入"
                    ),
                    requested_by="system-input-worker",
                )
            target = _json_object(run["target_snapshot_json"])
            # A retry appends a fresh attempt for each retryable entry to the
            # same durable run.  Do not replay an older PENDING attempt from
            # the previous run: it may have been left pending by a stop or a
            # lost worker while its replacement is already the current
            # attempt.  The roll-up code below uses the same ordering rule;
            # keeping it here is what prevents a stale page operation from
            # being opened a second time after a retry.
            attempts = [dict(row) for row in con.execute(
                """SELECT ia.* FROM input_attempts ia
                   JOIN input_entries ie ON ie.entry_id=ia.entry_id
                   JOIN input_units iu ON iu.unit_id=ie.unit_id
                   WHERE ia.input_run_id=?
                     AND NOT EXISTS (
                         SELECT 1 FROM input_attempts newer
                         WHERE newer.input_run_id=ia.input_run_id
                           AND newer.entry_id=ia.entry_id
                           AND (newer.created_at>ia.created_at
                                OR (newer.created_at=ia.created_at
                                    AND newer.rowid>ia.rowid))
                     )
                   ORDER BY iu.ordinal, ia.created_at, ia.rowid""",
                (input_run_id,),
            ).fetchall()]
            target_entry_support = target.get("document_entry_support")
            current_entry_support = _document_entry_support_for_metadata(
                [
                    _json_object(row["metadata_json"])
                    for row in con.execute(
                        "SELECT metadata_json FROM work_items WHERE workflow_id=? ORDER BY sequence, item_id",
                        (workflow_id,),
                    ).fetchall()
                ],
                input_type=str(run["input_type"] or target.get("input_type") or ""),
            )
            if current_entry_support.get("supported") is not True:
                entry_gate_error = (
                    "SYSTEM_INPUT_DOCUMENT_UNSUPPORTED",
                    current_entry_support.get("reason") or "当前文档不支持系统录入，已停止外部录入。",
                    {
                        "target": dict(target_entry_support) if isinstance(target_entry_support, Mapping) else None,
                        "current": current_entry_support,
                    },
                )
            elif not isinstance(target_entry_support, Mapping) or target_entry_support.get("supported") is not True:
                entry_gate_error = (
                    "SYSTEM_INPUT_DOCUMENT_UNSUPPORTED",
                    "录入运行快照缺少当前文档结构门禁，已停止外部录入，请重新开始录入。",
                    {
                        "target": dict(target_entry_support) if isinstance(target_entry_support, Mapping) else None,
                        "current": current_entry_support,
                    },
                )
            elif dict(target_entry_support) != current_entry_support:
                entry_gate_error = (
                    "SYSTEM_INPUT_DOCUMENT_UNSUPPORTED",
                    "文档录入结构判断与运行快照不一致，已停止外部录入，请重新开始录入。",
                    {
                        "target": dict(target_entry_support),
                        "current": current_entry_support,
                    },
                )

        if run_error_code in {"INPUT_RUN_STOPPED", "INPUT_BROWSER_CLOSED"}:
            # A restart dispatcher normally drains this marker first. Keep a
            # direct/replayed worker invocation safe as well, and finish an
            # active two-phase marker instead of leaving the run projected as
            # RUNNING forever when the scheduler is disabled.
            if run_status in {"PENDING", "RUNNING"}:
                return self.finalize_stopped_input_run(
                    input_run_id,
                    reason=(
                        "平台浏览器窗口被关闭，已停止本次系统录入"
                        if run_error_code == "INPUT_BROWSER_CLOSED"
                        else "用户停止了系统录入"
                    ),
                    requested_by="system-input-worker",
                )
            return self.get_projection(workflow_id)

        if self.executor is None:
            raise SystemInputError("页面录入执行器未连接", code="INPUT_EXECUTOR_UNAVAILABLE")

        if entry_gate_error is not None:
            error_code, error_message, evidence = entry_gate_error
            pending_attempts = [
                attempt
                for attempt in attempts
                if str(attempt.get("status")) not in {"SUCCEEDED", "FAILED", "AMBIGUOUS", "NEEDS_RECONCILE"}
            ]
            for attempt in pending_attempts:
                operation_id = _text(attempt.get("external_operation_id"), limit=256) or None
                operation_state = "NOT_STARTED"
                if operation_id:
                    operation_state = "UNKNOWN"
                    if self.external is not None:
                        try:
                            operation_state = _text(
                                self.external.get_operation(operation_id).get("side_effect_state"),
                                limit=64,
                            ) or "UNKNOWN"
                        except Exception:
                            # If the operation cannot be read, do not guess
                            # that its page side effect did not happen.
                            operation_state = "UNKNOWN"

                if operation_state in {"INTENT_RECORDED", "NOT_STARTED", "REJECTED"} and operation_id:
                    if operation_state in {"INTENT_RECORDED", "NOT_STARTED"} and self.external is not None:
                        try:
                            self.external.resolve_operation(
                                operation_id,
                                decision="NOT_SUBMITTED",
                                evidence_source="document-entry-gate",
                                evidence_hash=content_hash({
                                    "operation_id": operation_id,
                                    "document_entry_gate": evidence,
                                }),
                                evidence={"document_entry_gate": evidence},
                                resolved_by="system-input-worker",
                            )
                            operation_state = "REJECTED"
                        except Exception:
                            operation_state = "UNKNOWN"

                if operation_state not in {"NOT_STARTED", "REJECTED"}:
                    # An operation that was already in flight, submitted,
                    # confirmed or could not be read is a recovery concern.
                    # Stop without attempting a new page write and preserve
                    # the conservative ambiguous state for manual review.
                    self._set_run_state(
                        input_run_id,
                        run_status="RUNNING",
                        attempt_id=str(attempt["attempt_id"]),
                        attempt_status="NEEDS_RECONCILE",
                        side_effect_state="AMBIGUOUS",
                        external_operation_id=operation_id,
                        error_code=error_code,
                        error_message=error_message,
                        evidence={"document_entry_gate": evidence},
                        entry_updates={
                            "input_status": "needs_reconcile",
                            "external_status": "AMBIGUOUS",
                            "requires_reconcile": 1,
                            "review_url_status": "unknown",
                        },
                    )
                    continue
                self._set_run_state(
                    input_run_id,
                    run_status="RUNNING",
                    attempt_id=str(attempt["attempt_id"]),
                    attempt_status="FAILED",
                    side_effect_state=operation_state,
                    external_operation_id=operation_id,
                    error_code=error_code,
                    error_message=error_message,
                    evidence={"document_entry_gate": evidence},
                    entry_updates={
                        "input_status": "failed_retryable",
                        "external_status": "UNKNOWN",
                        "requires_reconcile": 0,
                        "review_url_status": "unknown",
                    },
                )
            return self.get_projection(workflow_id)
        with self.database.transaction() as con:
            con.execute(
                "UPDATE input_runs SET status='RUNNING', started_at=COALESCE(started_at, ?), updated_at=? WHERE input_run_id=?",
                (utc_now(), utc_now(), input_run_id),
            )

        executor_run_started = False
        executor_begin_run = getattr(self.executor, "begin_run", None)
        executor_end_run = getattr(self.executor, "end_run", None)
        if callable(executor_begin_run):
            executor_begin_run(
                input_run_id,
                _text(run["input_type"] or target.get("input_type"), limit=32),
            )
            executor_run_started = callable(executor_end_run)

        def close_executor_run() -> None:
            nonlocal executor_run_started
            if not executor_run_started:
                return
            executor_run_started = False
            try:
                executor_end_run(
                    input_run_id,
                    _text(run["input_type"] or target.get("input_type"), limit=32),
                )
            except Exception:
                # Browser cleanup is best effort.  The durable input state is
                # already authoritative, and a cleanup failure must not mask
                # the run result or trigger a second page write.
                pass

        external_system, account_scope, mapping_version = _external_runtime_configuration()
        for attempt_index, attempt in enumerate(attempts):
            attempt_id = str(attempt["attempt_id"])
            if str(attempt.get("status")) in {"SUCCEEDED", "FAILED", "AMBIGUOUS", "NEEDS_RECONCILE"}:
                continue
            # Entry-boundary control checkpoint. A page write itself is never
            # interrupted; pause parks here and stop finalizes the run with
            # every un-attempted unit kept retryable.
            control = self._input_run_control_now(input_run_id)
            if control.get("stop_requested"):
                close_executor_run()
                return self.finalize_stopped_input_run(
                    input_run_id,
                    reason="用户停止了系统录入",
                    requested_by="system-input-worker",
                )
            if control.get("pause_requested"):
                control_signal, _ = self._park_for_input_run_control(input_run_id)
                if control_signal == "stop":
                    close_executor_run()
                    return self.finalize_stopped_input_run(
                        input_run_id,
                        reason="用户停止了系统录入",
                        requested_by="system-input-worker",
                    )
            entry_id = str(attempt["entry_id"])
            lease = None
            operation_id: str | None = None
            mapping_id: str | None = None
            side_effect_crossed = False
            browser_closed = False
            operation_state = "NOT_STARTED"
            preserved_external_id = ""
            try:
                if self.external is None:
                    raise SystemInputError(
                        "外部录入运行时未初始化",
                        code="INPUT_EXTERNAL_RUNTIME_UNAVAILABLE",
                    )
                with self.database.read_transaction() as con:
                    entry_row = con.execute("SELECT * FROM input_entries WHERE entry_id=?", (entry_id,)).fetchone()
                    if entry_row is None:
                        raise NotFoundError(f"input entry does not exist: {entry_id}")
                    entry = dict(entry_row)
                    entry["configuration"] = _json_object(entry.pop("configuration_json", "{}"))
                    preserved_external_id = _text(entry.get("external_record_id"), limit=512)
                    unit_id = str(entry_row["unit_id"])
                    unit = next(
                        (item for item in target.get("units", []) if str(item.get("unit_id")) == unit_id),
                        {},
                    )
                if not isinstance(unit, Mapping) or not unit:
                    raise SystemInputError(
                        "系统录入目标单元不存在",
                        code="SYSTEM_INPUT_CONFIG_INCOMPLETE",
                    )

                # If a confirmed partial record was explicitly detached for a
                # replacement, keep the original business mapping immutable
                # and allocate a deterministic replacement mapping for this
                # configuration revision. Retries of the same replacement
                # continue to reuse that mapping.
                business_record_key = entry_id
                if not _text(entry.get("external_record_id"), limit=512):
                    prior_mapping = self.external.find_record(
                        external_system=external_system,
                        account_scope=account_scope,
                        business_record_key=entry_id,
                    )
                    if prior_mapping and _text(prior_mapping.get("external_record_id"), limit=512):
                        business_record_key = (
                            f"{entry_id}:replacement:{int(run['configuration_revision'])}"
                        )
                mapping = self.external.ensure_record(
                    workflow_id,
                    external_system=external_system,
                    account_scope=account_scope,
                    business_record_key=business_record_key,
                    mapping_version=mapping_version,
                    item_id=None,
                )
                mapping_id = str(mapping["external_record_mapping_id"])
                lease = self.external.acquire_record_lease(
                    mapping_id,
                    f"input:{input_run_id}:{attempt_id}",
                    ttl_seconds=300,
                )
                operation_payload = _external_operation_payload(input_run_id, entry_id, unit, target)
                operation = self.external.prepare_operation(
                    workflow_id,
                    mapping_id=mapping_id,
                    operation_key=f"input-run:{input_run_id}:{entry_id}",
                    payload=operation_payload,
                    mapping_version=mapping_version,
                    item_id=None,
                )
                operation_id = str(operation["external_operation_id"])
                operation_state = str(operation.get("side_effect_state") or "NOT_STARTED")

                # A preflight failure is safely resolved as NOT_SUBMITTED,
                # which leaves the legacy operation key in REJECTED. A later
                # safe retry must get a fresh durable operation; reusing the
                # rejected key would return the old row and begin_operation()
                # would reject every retry forever. Keep the legacy lookup
                # for recovery compatibility, then namespace only the new
                # attempt when that lookup finds a rejected operation.
                if operation_state == "REJECTED":
                    operation = self.external.prepare_operation(
                        workflow_id,
                        mapping_id=mapping_id,
                        operation_key=f"input-run:{input_run_id}:{entry_id}:attempt:{attempt_id}",
                        payload=operation_payload,
                        mapping_version=mapping_version,
                        item_id=None,
                    )
                    operation_id = str(operation["external_operation_id"])
                    operation_state = str(operation.get("side_effect_state") or "NOT_STARTED")

                # A lost worker response can leave the local attempt pending
                # even though the durable external operation was confirmed.
                # Replay its receipt instead of opening another page.
                if operation_state == "CONFIRMED":
                    receipt = operation.get("receipt") if isinstance(operation.get("receipt"), Mapping) else {}
                    external_id = _text(receipt.get("external_record_id"), limit=512)
                    if not external_id:
                        raise SystemInputError("已确认操作缺少录入 ID", code="INPUT_RESULT_UNCONFIRMED")
                    self._set_run_state(
                        input_run_id,
                        run_status="RUNNING",
                        attempt_id=attempt_id,
                        attempt_status="SUCCEEDED",
                        side_effect_state="CONFIRMED",
                        external_operation_id=operation_id,
                        evidence={"replayed_external_receipt": True},
                        entry_updates={
                            "input_status": "succeeded",
                            "external_record_mapping_id": mapping_id,
                            "external_record_id": external_id,
                            "external_status": "SUCCEEDED",
                            "requires_reconcile": 0,
                            "review_url": None,
                            "review_url_status": "unavailable",
                            "review_url_source": "platform_not_supported",
                            "run_review_url": None,
                        },
                    )
                    continue
                if operation_state in {"IN_FLIGHT", "SUBMITTED", "AMBIGUOUS"}:
                    side_effect_crossed = True
                    raise SystemInputError(
                        "已有未完成的外部录入操作，需要先核验",
                        code="INPUT_RESULT_UNCONFIRMED",
                    )

                self._set_run_state(
                    input_run_id,
                    run_status="RUNNING",
                    attempt_id=attempt_id,
                    attempt_status="RUNNING",
                    side_effect_state="INTENT_RECORDED",
                    external_operation_id=operation_id,
                    entry_updates={
                        "input_status": "running",
                        "external_record_mapping_id": mapping_id,
                        "external_status": "PENDING",
                        "requires_reconcile": 0,
                    },
                )
                # ``prepare_operation`` currently returns INTENT_RECORDED,
                # but keep the local state aligned with the durable attempt
                # transition. This makes a preflight failure resolve the
                # operation as NOT_SUBMITTED even if a future repository
                # implementation returns NOT_STARTED for a newly prepared
                # operation.
                operation_state = "INTENT_RECORDED"

                payload = {
                    "input_run_id": input_run_id,
                    "workflow_id": workflow_id,
                    "entry_id": entry_id,
                    "entry": entry,
                    # The executor receives a page-safe projection. Answers
                    # and other local audit-only facts remain in the durable
                    # target snapshot, but are not part of the page adapter
                    # contract.
                    "unit": _page_target_snapshot({"units": [unit]})["units"][0],
                    "target": _page_target_snapshot(target),
                    "external_operation_id": operation_id,
                    "external_record_mapping_id": mapping_id,
                }

                # Install the control callback before preflight as well as
                # before the final page-write boundary.  Template loading and
                # other read-only form waits can otherwise hide a stop request
                # for their entire timeout window.
                if getattr(self.executor, "accepts_input_run_control", False):
                    def page_control_check() -> None:
                        nonlocal lease
                        control = self._input_run_control_now(input_run_id)
                        if control.get("stop_requested"):
                            raise SystemInputError(
                                "系统录入已停止，不再继续当前页面操作",
                                code="INPUT_RUN_STOPPED",
                            )
                        if control.get("pause_requested"):
                            control_signal, lease = self._park_for_input_run_control(
                                input_run_id,
                                lease=lease,
                            )
                            if control_signal == "stop":
                                raise SystemInputError(
                                    "系统录入已停止，不再继续当前页面操作",
                                    code="INPUT_RUN_STOPPED",
                                )

                    payload["_control_check"] = page_control_check
                preflight = getattr(self.executor, "preflight", None)
                if callable(preflight):
                    preflight(payload)

                # Preflight may open a visible browser and wait for the user
                # to finish login.  A stop request received during that wait
                # must be consumed before the side-effect boundary is opened;
                # otherwise the worker would continue into begin_operation()
                # and launch the write page after the user already stopped it.
                control = self._input_run_control_now(input_run_id)
                if control.get("stop_requested"):
                    raise SystemInputError(
                        "系统录入已停止，不再打开新的录入页面",
                        code="INPUT_RUN_STOPPED",
                    )
                if control.get("pause_requested"):
                    # Do not hold a five-minute external lease while a user
                    # pauses during preflight.  The durable intent remains
                    # INTENT_RECORDED and is reacquired after resume.
                    if self.external is not None and lease is not None:
                        try:
                            self.external.release_record_lease(lease)
                        except (ConflictError, LeaseConflict, RepositoryError):
                            pass
                        lease = None
                    control_signal, _ = self._park_for_input_run_control(input_run_id)
                    if control_signal == "stop":
                        raise SystemInputError(
                            "系统录入已停止，不再打开新的录入页面",
                            code="INPUT_RUN_STOPPED",
                        )
                    lease = self.external.acquire_record_lease(
                        mapping_id,
                        f"input:{input_run_id}:{attempt_id}",
                        ttl_seconds=300,
                    )

                # Serialize the final control check and the durable page-write
                # boundary.  Without this lock, a stop could commit after the
                # check but before begin_operation(), causing a fresh page to
                # open after the user had already stopped the run.
                while True:
                    pause_before_boundary = False
                    with self._input_run_boundary_lock(input_run_id):
                        control = self._input_run_control_now(input_run_id)
                        if control.get("stop_requested"):
                            raise SystemInputError(
                                "系统录入已停止，不再打开新的录入页面",
                                code="INPUT_RUN_STOPPED",
                            )
                        pause_before_boundary = bool(control.get("pause_requested"))
                        if not pause_before_boundary:
                            # Treat entry into begin_operation() as the
                            # side-effect boundary even if its bookkeeping
                            # raises after the durable operation transition.
                            # Failing closed avoids retrying a possibly-opened
                            # external write.
                            side_effect_crossed = True
                            self.external.begin_operation(operation_id, lease)
                            break

                    # A pause may have arrived while the lease was being
                    # reacquired. Release it outside the lock so resume/stop
                    # requests can update the durable control row.
                    if self.external is not None and lease is not None:
                        try:
                            self.external.release_record_lease(lease)
                        except (ConflictError, LeaseConflict, RepositoryError):
                            pass
                        lease = None
                    control_signal, _ = self._park_for_input_run_control(input_run_id)
                    if control_signal == "stop":
                        raise SystemInputError(
                            "系统录入已停止，不再打开新的录入页面",
                            code="INPUT_RUN_STOPPED",
                        )
                    lease = self.external.acquire_record_lease(
                        mapping_id,
                        f"input:{input_run_id}:{attempt_id}",
                        ttl_seconds=300,
                    )
                self._set_run_state(
                    input_run_id,
                    run_status="RUNNING",
                    attempt_id=attempt_id,
                    attempt_status="RUNNING",
                    side_effect_state="IN_FLIGHT",
                    external_operation_id=operation_id,
                    entry_updates={
                        "input_status": "running",
                        "external_record_mapping_id": mapping_id,
                        "external_status": "PENDING",
                    },
                )

                result = self.executor(payload) if callable(self.executor) else self.executor.execute(payload)
                if not isinstance(result, Mapping):
                    raise SystemInputError("页面执行器没有返回可核验反馈", code="INPUT_EXECUTOR_INVALID_RESULT")
                external_id = _text(result.get("external_record_id") or result.get("paperId"), limit=512)
                if not external_id:
                    raise SystemInputError("页面保存后没有观察到录入 ID", code="INPUT_RESULT_UNCONFIRMED")
                feedback = redact_public_json(result.get("feedback") if isinstance(result.get("feedback"), Mapping) else {})
                feedback = dict(feedback) if isinstance(feedback, Mapping) else {}
                steps = redact_public_json(result.get("steps") or [])
                steps = steps if isinstance(steps, list) else []
                self.external.record_submission(
                    operation_id,
                    lease,
                    ExternalSubmission(external_id, external_id, feedback),
                )
                self.external.confirm_operation(
                    operation_id,
                    lease,
                    external_record_id=external_id,
                    evidence_source="page-readback",
                    evidence_hash=content_hash({"operation_id": operation_id, "feedback": feedback, "steps": steps}),
                    evidence={"feedback": feedback, "steps": steps},
                )
                self._set_run_state(
                    input_run_id,
                    run_status="RUNNING",
                    attempt_id=attempt_id,
                    attempt_status="SUCCEEDED",
                    side_effect_state="CONFIRMED",
                    external_operation_id=operation_id,
                    evidence={"feedback": feedback, "steps": steps},
                    entry_updates={
                        "input_status": "succeeded",
                        "external_record_mapping_id": mapping_id,
                        "external_record_id": external_id,
                        "external_status": "SUCCEEDED",
                        "requires_reconcile": 0,
                        # The current platform confirms the external record
                        # ID but does not expose a stable review URL.  Keep
                        # this an explicit non-error state; users search the
                        # submitted document name instead.
                        "review_url": None,
                        "review_url_status": "unavailable",
                        "review_url_source": "platform_not_supported",
                        "run_review_url": None,
                    },
                )
            except Exception as exc:
                error_code = getattr(exc, "code", "INPUT_RESULT_UNKNOWN")
                if not isinstance(error_code, str) or not error_code:
                    error_code = "INPUT_RESULT_UNKNOWN"
                error_message = _safe_exception_message(exc)
                evidence = _exception_evidence(exc)
                browser_closed = str(error_code) == "INPUT_BROWSER_CLOSED" or _is_browser_closed_error(exc)
                # Publish the close marker before the explicit-stop finalizer
                # runs. The renderer may poll between those transactions and
                # must not start a read-only browser check in that interval.
                run_error_code = "INPUT_BROWSER_CLOSED" if browser_closed else error_code
                if side_effect_crossed:
                    if self.external is not None and operation_id and lease is not None:
                        try:
                            self.external.mark_ambiguous(
                                operation_id,
                                lease,
                                error_code=error_code,
                            )
                        except (ConflictError, LeaseConflict, RepositoryError):
                            # The durable operation/lease state is already the
                            # recovery authority if the lease expired.
                            pass
                    self._set_run_state(
                        input_run_id,
                        # A browser close is an explicit user stop. Preserve
                        # the attempted unit's conservative AMBIGUOUS state
                        # for reconciliation, but let finalize_stopped... set
                        # the run itself to FAILED/INPUT_RUN_STOPPED instead
                        # of allowing _set_run_state's roll-up to win.
                        run_status="RUNNING" if browser_closed else "AMBIGUOUS",
                        attempt_id=attempt_id,
                        attempt_status="AMBIGUOUS",
                        side_effect_state="AMBIGUOUS",
                        external_operation_id=operation_id,
                        error_code=run_error_code,
                        error_message=error_message,
                        evidence=evidence,
                        force_run_status=browser_closed,
                        entry_updates={
                            "input_status": "needs_reconcile",
                            "external_record_mapping_id": mapping_id,
                            "external_status": "AMBIGUOUS",
                            "requires_reconcile": 1,
                            "review_url_status": "unknown",
                        },
                    )
                else:
                    if self.external is not None and operation_id and operation_state == "INTENT_RECORDED":
                        try:
                            self.external.resolve_operation(
                                operation_id,
                                decision="NOT_SUBMITTED",
                                evidence_source="page-not-started",
                                evidence_hash=content_hash({"operation_id": operation_id, "error": error_message}),
                                evidence=evidence,
                                resolved_by="system-input-worker",
                            )
                        except (ConflictError, LeaseConflict, RepositoryError):
                            pass
                    # A later preflight failure must not downgrade a known
                    # platform record to FAILED. The page was never started
                    # for this attempt, while the earlier confirmed partial
                    # record remains a real external side effect.
                    self._set_run_state(
                        input_run_id,
                        run_status="RUNNING",
                        attempt_id=attempt_id,
                        attempt_status="FAILED",
                        side_effect_state=operation_state,
                        external_operation_id=operation_id,
                        error_code=run_error_code,
                        error_message=error_message,
                        evidence=evidence,
                        entry_updates={
                            "input_status": "failed_retryable",
                            "external_record_mapping_id": mapping_id,
                            "external_status": (
                                "SUCCEEDED"
                                if preserved_external_id
                                else ("FAILED" if mapping_id else "UNKNOWN")
                            ),
                            "requires_reconcile": 0,
                            "review_url_status": "unknown",
                        },
                    )
            finally:
                if self.external is not None and lease is not None:
                    try:
                        self.external.release_record_lease(lease)
                    except (ConflictError, LeaseConflict, RepositoryError):
                        pass
            # A browser close or explicit stop is never a reason to launch the
            # next unit in a fresh browser. Finalize the run; the attempted
            # unit keeps its own ambiguous/retryable state and the rest stay
            # untouched. This also handles the final-page race: once the
            # control marker exists, a successful page response must not erase
            # the user's stop request before the finalizer consumes it.
            stop_requested = self._input_run_stop_requested_now(input_run_id)
            if browser_closed or stop_requested:
                close_executor_run()
                return self.finalize_stopped_input_run(
                    input_run_id,
                    reason=(
                        "平台浏览器窗口被关闭，已停止本次系统录入"
                        if browser_closed
                        else "用户停止了系统录入"
                    ),
                    requested_by="system-input-worker",
                )
        close_executor_run()
        return self.get_projection(workflow_id)

    def create_template(
        self,
        input_type: str,
        name: str,
        configuration: Mapping[str, Any],
        *,
        platform_template_id: str | None = None,
        platform_template_name: str | None = None,
        platform_template_version: str | None = None,
    ) -> dict[str, Any]:
        canonical = validate_template_configuration(input_type, configuration)
        template_id = new_id("app-template")
        now = utc_now()
        with self.database.transaction() as con:
            self._require_available(con)
            con.execute(
                """INSERT INTO input_config_templates(
                   app_template_id, input_type, name, schema_version, configuration_json,
                   platform_template_id, platform_template_name, platform_template_version,
                   created_at, updated_at, archived_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (template_id, _text(input_type, limit=32), _text(name, limit=256), "1", canonical_json(canonical),
                 _text(platform_template_id, limit=256) or None, _text(platform_template_name, limit=256) or None,
                 _text(platform_template_version, limit=128) or None, now, now, None),
            )
            return self._template_row(con, template_id)

    @staticmethod
    def _template_row(con: sqlite3.Connection, template_id: str) -> dict[str, Any]:
        row = con.execute("SELECT * FROM input_config_templates WHERE app_template_id=?", (template_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"input config template does not exist: {template_id}")
        value = dict(row)
        value["configuration"] = _json_object(value.pop("configuration_json", "{}"))
        return value

    def list_templates(self, input_type: str | None = None) -> list[dict[str, Any]]:
        with self.database.read_transaction() as con:
            if not _table_exists(con, "input_config_templates"):
                return []
            if input_type:
                rows = con.execute("SELECT app_template_id FROM input_config_templates WHERE input_type=? AND archived_at IS NULL ORDER BY updated_at DESC, app_template_id", (_text(input_type, limit=32),)).fetchall()
            else:
                rows = con.execute("SELECT app_template_id FROM input_config_templates WHERE archived_at IS NULL ORDER BY updated_at DESC, app_template_id").fetchall()
            return [self._template_row(con, str(row["app_template_id"])) for row in rows[:256]]

    @staticmethod
    def _platform_template_row(con: sqlite3.Connection, template_key: str) -> dict[str, Any]:
        row = con.execute(
            "SELECT * FROM platform_input_templates WHERE platform_template_key=?",
            (template_key,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"platform input template does not exist: {template_key}")
        return dict(row)

    def _require_platform_template_available(self, con: sqlite3.Connection) -> None:
        self._require_available(con)
        if not _table_exists(con, "platform_input_templates"):
            raise SystemInputError(
                "platform input template catalog requires the latest workflow schema",
                code="MIGRATION_REQUIRED",
            )

    @staticmethod
    def _platform_template_input_type(value: Any) -> str:
        normalized = _text(value, limit=32)
        if normalized not in INPUT_TYPES:
            raise SystemInputError(
                "平台模板 input_type 不受支持",
                code="VALIDATION_ERROR",
            )
        return normalized

    @staticmethod
    def _platform_template_name(value: Any) -> str:
        normalized = _text(value, limit=256)
        if not normalized:
            raise SystemInputError(
                "平台模板名称不能为空",
                code="VALIDATION_ERROR",
            )
        if normalized.isdigit():
            raise SystemInputError(
                "平台模板名称不能只有数字",
                code="VALIDATION_ERROR",
            )
        return normalized

    def create_platform_template(
        self,
        input_type: str,
        name: str,
        *,
        platform_template_id: str | None = None,
        platform_template_version: str | None = None,
    ) -> dict[str, Any]:
        normalized_type = self._platform_template_input_type(input_type)
        normalized_name = self._platform_template_name(name)
        template_key = new_id("platform-template")
        now = utc_now()
        with self.database.transaction() as con:
            self._require_platform_template_available(con)
            try:
                con.execute(
                    """INSERT INTO platform_input_templates(
                       platform_template_key, input_type, name,
                       platform_template_id, platform_template_version,
                       created_at, updated_at, archived_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        template_key,
                        normalized_type,
                        normalized_name,
                        _text(platform_template_id, limit=256) or None,
                        _text(platform_template_version, limit=128) or None,
                        now,
                        now,
                        None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "同一录入类型下的平台模板名称已存在",
                    code="PLATFORM_TEMPLATE_CONFLICT",
                ) from exc
            return self._platform_template_row(con, template_key)

    def list_platform_templates(self, input_type: str | None = None) -> list[dict[str, Any]]:
        with self.database.read_transaction() as con:
            if not _table_exists(con, "platform_input_templates"):
                return []
            if input_type:
                normalized_type = self._platform_template_input_type(input_type)
                rows = con.execute(
                    """SELECT * FROM platform_input_templates
                       WHERE input_type=? AND archived_at IS NULL
                       ORDER BY updated_at DESC, platform_template_key
                       LIMIT 256""",
                    (normalized_type,),
                ).fetchall()
            else:
                rows = con.execute(
                    """SELECT * FROM platform_input_templates
                       WHERE archived_at IS NULL
                       ORDER BY updated_at DESC, platform_template_key
                       LIMIT 256""",
                ).fetchall()
            return [dict(row) for row in rows]

    def get_platform_template(self, template_key: str) -> dict[str, Any]:
        normalized_key = _text(template_key, limit=256)
        if not normalized_key:
            raise NotFoundError("platform input template key is required")
        with self.database.read_transaction() as con:
            if not _table_exists(con, "platform_input_templates"):
                raise SystemInputError(
                    "platform input template catalog requires the latest workflow schema",
                    code="MIGRATION_REQUIRED",
                )
            return self._platform_template_row(con, normalized_key)

    def update_platform_template(
        self,
        template_key: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized_key = _text(template_key, limit=256)
        if not normalized_key:
            raise NotFoundError("platform input template key is required")
        candidate = dict(changes or {})
        unknown = set(candidate) - _PLATFORM_TEMPLATE_UPDATE_KEYS
        if unknown:
            raise SystemInputError(
                f"平台模板包含不支持的字段: {sorted(unknown)}",
                code="VALIDATION_ERROR",
            )
        with self.database.transaction() as con:
            self._require_platform_template_available(con)
            current = self._platform_template_row(con, normalized_key)
            if current.get("archived_at"):
                raise ConflictError(
                    "平台模板已删除，不能继续编辑",
                    code="PLATFORM_TEMPLATE_ARCHIVED",
                )
            if not candidate:
                return current

            values: dict[str, Any] = {}
            if "input_type" in candidate:
                values["input_type"] = self._platform_template_input_type(candidate["input_type"])
            if "name" in candidate:
                values["name"] = self._platform_template_name(candidate["name"])
            if "platform_template_id" in candidate:
                values["platform_template_id"] = _text(candidate["platform_template_id"], limit=256) or None
            if "platform_template_version" in candidate:
                values["platform_template_version"] = _text(candidate["platform_template_version"], limit=128) or None

            assignments = [f"{key}=?" for key in values]
            now = utc_now()
            assignments.append("updated_at=?")
            try:
                updated = con.execute(
                    f"UPDATE platform_input_templates SET {', '.join(assignments)} "
                    "WHERE platform_template_key=? AND archived_at IS NULL",
                    (*values.values(), now, normalized_key),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "同一录入类型下的平台模板名称已存在",
                    code="PLATFORM_TEMPLATE_CONFLICT",
                ) from exc
            if updated.rowcount != 1:
                raise ConflictError("平台模板在编辑过程中发生变化", code="PLATFORM_TEMPLATE_CONFLICT")
            return self._platform_template_row(con, normalized_key)

    def delete_platform_template(self, template_key: str) -> dict[str, Any]:
        normalized_key = _text(template_key, limit=256)
        if not normalized_key:
            raise NotFoundError("platform input template key is required")
        with self.database.transaction() as con:
            self._require_platform_template_available(con)
            current = self._platform_template_row(con, normalized_key)
            if current.get("archived_at"):
                return current
            now = utc_now()
            updated = con.execute(
                """UPDATE platform_input_templates
                   SET archived_at=?, updated_at=?
                   WHERE platform_template_key=? AND archived_at IS NULL""",
                (now, now, normalized_key),
            )
            if updated.rowcount != 1:
                raise ConflictError("平台模板在删除过程中发生变化", code="PLATFORM_TEMPLATE_CONFLICT")
            return self._platform_template_row(con, normalized_key)


__all__ = [
    "SYSTEM_INPUT_TYPE_CAPABILITIES",
    "DELIVERY_MODES",
    "INPUT_TYPES",
    "PAPER_CATEGORIES",
    "SUPPORTED_EXTERNAL_INPUT_TYPES",
    "system_input_capability",
    "system_input_capabilities",
    "SystemInputError",
    "SystemInputService",
    "platform_template_reference_complete",
    "project_system_input_configuration",
    "validate_system_input_configuration",
    "validate_template_configuration",
]
