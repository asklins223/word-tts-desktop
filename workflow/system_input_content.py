"""Page-input content facts shared by parsers and trusted page adapters.

The workflow stores the source document and the generated audio separately
from the page form configuration.  A paper adapter still needs the semantic
facts that belong in the visible form (question stems, options, answers and
reference answers), so this module defines the deliberately narrow bridge
between those two layers.

This is not an API payload model.  It intentionally drops paths, IDs owned by
the external platform, HTML, arbitrary keys and executable values.  A future
content type should register its own builder/schema instead of widening this
paper contract.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePath
from typing import Any

from document_profiles import (
    IMITATION_READING_ENTRY_PROFILE,
    RECORD_RETELLING_ENTRY_PROFILE,
    RECORD_RETELLING_TABLE_SPECIAL_PROFILE,
    RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE,
    RESPONSE_ENTRY_PROFILE,
    SUPPORTED_IMITATION_SECTION_PROFILES,
    is_imitation_document_type,
    is_imitation_item,
)


PAGE_INPUT_SCHEMA_VERSION = "paper-page-input-v1"
PAGE_INPUT_TYPES = frozenset({
    "听后选择",
    "听后应答",
    "模仿朗读",
    "听后记录并转述信息",
    "信息获取",
    "信息转述及询问",
})
PAGE_INPUT_MAX_BYTES = 2 * 1024 * 1024
PAGE_INPUT_MAX_TEXT = 1_000_000
PAGE_INPUT_MAX_LIST = 256
# The first version of document entry deliberately recognizes whole document
# shapes instead of treating every individual page-input fact as an entry
# point.  This keeps the review screen and the external adapter aligned: a
# full listening paper is one flow, while imitation-reading, listening-response
# and listening-record/retelling 专项 documents are smaller, separate flows. New
# document families should be added here with a parser-owned profile before
# they become visible in the UI.
DOCUMENT_ENTRY_PROFILES = (
    {
        "format": "listening_paper",
        "label": "听说测试题",
        "types": (
            "听后选择",
            "听后应答",
            "模仿朗读",
            "听后记录并转述信息",
        ),
    },
    {
        "format": "legacy_listening_paper",
        "label": "听说测试题（旧版套卷）",
        "types": ("模仿朗读", "信息获取", "信息转述及询问"),
    },
    {
        "format": "imitation_reading",
        "label": "模仿朗读",
        "types": ("模仿朗读",),
    },
    {
        "format": "listening_response",
        "label": "听后应答",
        "types": ("听后应答",),
    },
    {
        "format": "listening_record_retelling",
        "label": "听后记录并转述信息",
        "types": ("听后记录并转述信息",),
    },
    {
        "format": "legacy_info_retelling",
        "label": "信息转述及询问",
        "types": ("信息转述及询问",),
    },
)
# 课文跟读没有逐题页面事实（page_input）；它的结构证据是解析器标记的
# 文档类型本身。这个档案只描述课文文档形状，不参与按 page_input 匹配。
TEXTBOOK_DOCUMENT_ENTRY_PROFILE = {
    "format": "text_reading",
    "label": "课文跟读",
    "types": (),
}
DOCUMENT_ENTRY_SCHEMA_VERSION = "document-entry-preflight-v1"


class PageInputFactsError(ValueError):
    """A parser supplied page fact that is outside the safe content schema."""


def _text(value: Any, *, limit: int = 1024) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _safe_stem(value: Any, *, limit: int = 240) -> str:
    """Keep an audio stem as a basename, never as a path."""

    raw = str(value or "").replace("\\", "/")
    stem = PurePath(raw).name
    stem = re.sub(r"[\x00-\x1f\x7f]", "", stem).strip(" .")
    return stem[:limit]


_PAGE_PAREN_SPEAKER_MARKER_RE = re.compile(
    r"(?im)^[ \t]*\([WwMm]\)(?:[ \t]*[:：])?[ \t]*"
)


def _page_listening_text(value: Any) -> str:
    """Remove only parenthesized speaker labels from system-input text.

    TTS keeps the original parser item text, while page facts use this
    display-only form so the external editor and document view do not show
    ``(W)/(M)``.  ``W:``/``M:`` remain part of the visible text submitted to
    the system; the audio pipeline removes both forms independently.
    """

    return _PAGE_PAREN_SPEAKER_MARKER_RE.sub(
        "", _text(value, limit=PAGE_INPUT_MAX_TEXT)
    ).strip()


def _number(value: Any) -> int | float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _string_list(value: Any, *, limit: int = PAGE_INPUT_MAX_LIST) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        values = list(value)[:limit]
    else:
        return []
    return [_text(item, limit=PAGE_INPUT_MAX_TEXT) for item in values if _text(item, limit=PAGE_INPUT_MAX_TEXT)]


def _slash_split_string_list(value: Any) -> list[str]:
    """Expand slash-separated alternatives at the asking-answer boundary."""

    result: list[str] = []
    for item in _string_list(value):
        result.extend(
            part.strip()
            for part in re.split(r"\s*/\s*", item)
            if part.strip()
        )
        if len(result) >= PAGE_INPUT_MAX_LIST:
            break
    return result[:PAGE_INPUT_MAX_LIST]


def _option(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    option_id = _text(
        value.get("option_id") or value.get("id") or value.get("key") or value.get("label"),
        limit=32,
    )
    text = _text(value.get("text") or value.get("content") or value.get("value"), limit=PAGE_INPUT_MAX_TEXT)
    if not option_id or not text:
        return None
    return {"option_id": option_id, "text": text}


def _options(value: Any) -> list[dict[str, str]]:
    if isinstance(value, Mapping):
        values = [
            {"option_id": key, "text": item}
            for key, item in list(value.items())[:PAGE_INPUT_MAX_LIST]
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        values = list(value)[:PAGE_INPUT_MAX_LIST]
    else:
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in values:
        option = _option(item)
        if option is None:
            continue
        option_key = option["option_id"].casefold()
        if option_key in seen:
            # Duplicate ids make the correct answer ambiguous.  Do not
            # silently drop one option and let the page adapter write a
            # different-looking question.
            raise PageInputFactsError("页面选项存在重复 option_id")
        seen.add(option_key)
        result.append(option)
    return result


def _question(
    value: Any,
    *,
    listening_text_as_prompt: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    number = value.get("number") or value.get("question_number") or value.get("index")
    if number not in (None, "") and not isinstance(number, bool):
        result["number"] = number if isinstance(number, (int, float)) else _text(number, limit=32)
    prompt_value = (
        value.get("prompt")
        or value.get("stem")
        or value.get("question")
        or value.get("text")
    )
    if not prompt_value and listening_text_as_prompt:
        # The response page uses the listening script as its question prompt.
        # Some compact parser outputs only have ``listening_text``; retaining
        # it as a prompt keeps the durable fact complete without changing the
        # semantics of selection or imitation pages.
        prompt_value = (
            value.get("listening_text")
            or value.get("hearing_text")
            or value.get("original_text")
            or value.get("source_text")
        )
    prompt = _page_listening_text(_text(prompt_value, limit=PAGE_INPUT_MAX_TEXT))
    if prompt:
        result["prompt"] = prompt
    listening_text = _page_listening_text(
        _text(
            value.get("listening_text")
            or value.get("hearing_text")
            or value.get("original_text")
            or value.get("source_text"),
            limit=PAGE_INPUT_MAX_TEXT,
        )
    )
    if listening_text:
        result["listening_text"] = listening_text
    options = _options(value.get("options") or value.get("choices"))
    if options:
        result["options"] = options
    for key in ("score", "answer_time", "times", "gap"):
        number_value = _number(value.get(key))
        if number_value is not None:
            result[key] = number_value
    answer = value.get("answer", value.get("correct_answer", value.get("correct")))
    if isinstance(answer, Mapping):
        answer = answer.get("option_id") or answer.get("id") or answer.get("key") or answer.get("value")
    if answer not in (None, ""):
        result["answer"] = _text(answer, limit=128)
    reference_answers = _string_list(value.get("reference_answers"), limit=PAGE_INPUT_MAX_LIST)
    if reference_answers:
        result["reference_answers"] = reference_answers
    reference_answers_source = _text(value.get("reference_answers_source"), limit=32)
    if reference_answers_source in {"document", "red_option"}:
        result["reference_answers_source"] = reference_answers_source
    answers = _string_list(value.get("answers"), limit=PAGE_INPUT_MAX_LIST)
    if answers:
        result["answers"] = answers
    prompt_audio_stem = _safe_stem(value.get("prompt_audio_filename_stem"))
    if prompt_audio_stem:
        result["prompt_audio_filename_stem"] = prompt_audio_stem
    return result


PageInputSanitizer = Callable[[Mapping[str, Any]], dict[str, Any] | None]
_PAGE_INPUT_SANITIZERS: dict[str, PageInputSanitizer] = {}


def register_page_input_sanitizer(
    input_type: str,
    sanitizer: PageInputSanitizer,
    *,
    replace: bool = False,
) -> None:
    """Register a code-owned sanitizer for one future page-input type.

    The durable workflow may carry facts for content types that do not have an
    external adapter yet.  Keeping the sanitizer registry separate from the
    paper schema lets a future textbook/vocabulary adapter add its own bounded
    contract without weakening this module's allowlist or accepting an import
    path supplied by a user configuration.
    """

    key = _text(input_type, limit=32).casefold()
    if not key or not callable(sanitizer):
        raise ValueError("input_type and callable sanitizer are required")
    if key in _PAGE_INPUT_SANITIZERS and not replace:
        raise ValueError(f"page-input sanitizer already registered: {key}")
    _PAGE_INPUT_SANITIZERS[key] = sanitizer


def _sanitize_paper_page_input(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a bounded paper page-input fact or ``None``.

    Partial facts are allowed to survive parsing so the review layer can show
    what is missing.  The page adapter performs the stricter completeness
    check immediately before a write.
    """

    if value in (None, ""):
        return None
    if not isinstance(value, Mapping):
        raise PageInputFactsError("page_input 必须是对象")
    schema_version = _text(value.get("schema_version") or PAGE_INPUT_SCHEMA_VERSION, limit=64)
    if schema_version != PAGE_INPUT_SCHEMA_VERSION:
        raise PageInputFactsError("page_input schema_version 不受支持")
    input_type = _text(value.get("input_type") or "paper", limit=32).casefold()
    if input_type != "paper":
        raise PageInputFactsError("当前页面内容契约只接受 paper")
    group_type = _text(value.get("type") or value.get("question_type") or value.get("category"), limit=64)
    if group_type not in PAGE_INPUT_TYPES:
        raise PageInputFactsError(f"page_input type 不受支持: {group_type}")

    result: dict[str, Any] = {
        "schema_version": PAGE_INPUT_SCHEMA_VERSION,
        "input_type": "paper",
        "type": group_type,
    }
    if group_type == "听后选择":
        raw_materials = value.get("materials")
        materials = raw_materials if isinstance(raw_materials, Sequence) and not isinstance(raw_materials, (bytes, bytearray, str)) else []
        normalized_materials: list[dict[str, Any]] = []
        for raw_material in list(materials)[:PAGE_INPUT_MAX_LIST]:
            if not isinstance(raw_material, Mapping):
                continue
            material: dict[str, Any] = {}
            listening_text = _page_listening_text(
                _text(
                    raw_material.get("listening_text") or raw_material.get("text"),
                    limit=PAGE_INPUT_MAX_TEXT,
                )
            )
            if listening_text:
                material["listening_text"] = listening_text
            for key in ("times", "gap"):
                number_value = _number(raw_material.get(key))
                if number_value is not None:
                    material[key] = number_value
            raw_questions = raw_material.get("questions")
            questions = raw_questions if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (bytes, bytearray, str)) else []
            normalized_questions: list[dict[str, Any]] = []
            for item in list(questions)[:PAGE_INPUT_MAX_LIST]:
                question = _question(item)
                if question:
                    normalized_questions.append(question)
            material["questions"] = normalized_questions
            normalized_materials.append(material)
        result["materials"] = normalized_materials
    elif group_type == "信息获取":
        raw_materials = value.get("materials")
        materials = (
            raw_materials
            if isinstance(raw_materials, Sequence)
            and not isinstance(raw_materials, (bytes, bytearray, str))
            else []
        )
        normalized_materials: list[dict[str, Any]] = []
        for raw_material in list(materials)[:PAGE_INPUT_MAX_LIST]:
            if not isinstance(raw_material, Mapping):
                continue
            material: dict[str, Any] = {}
            listening_text = _page_listening_text(
                _text(
                    raw_material.get("listening_text") or raw_material.get("text"),
                    limit=PAGE_INPUT_MAX_TEXT,
                )
            )
            if listening_text:
                material["listening_text"] = listening_text
            section = _text(raw_material.get("section"), limit=128)
            if section:
                material["section"] = section
            stem = _safe_stem(
                raw_material.get("audio_filename_stem")
                or raw_material.get("filename_stem")
            )
            if stem:
                material["audio_filename_stem"] = stem
            raw_questions = raw_material.get("questions")
            questions = (
                raw_questions
                if isinstance(raw_questions, Sequence)
                and not isinstance(raw_questions, (bytes, bytearray, str))
                else []
            )
            material["questions"] = [
                question
                for item in list(questions)[:PAGE_INPUT_MAX_LIST]
                for question in [_question(item)]
                if question
            ]
            normalized_materials.append(material)
        result["materials"] = normalized_materials
    elif group_type in {"听后应答", "模仿朗读"}:
        raw_questions = value.get("questions")
        questions = raw_questions if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (bytes, bytearray, str)) else []
        normalized_questions: list[dict[str, Any]] = []
        for item in list(questions)[:PAGE_INPUT_MAX_LIST]:
            question = _question(
                item,
                listening_text_as_prompt=group_type == "听后应答",
            )
            if question:
                normalized_questions.append(question)
        result["questions"] = normalized_questions
    elif group_type == "信息转述及询问":
        raw_recording = value.get("recording")
        recording_source = raw_recording if isinstance(raw_recording, Mapping) else {}
        recording: dict[str, Any] = {}
        listening_text = _page_listening_text(
            _text(
                recording_source.get("listening_text") or recording_source.get("text"),
                limit=PAGE_INPUT_MAX_TEXT,
            )
        )
        if listening_text:
            recording["listening_text"] = listening_text
        stem = _safe_stem(
            recording_source.get("audio_filename_stem")
            or recording_source.get("filename_stem")
        )
        if stem:
            recording["audio_filename_stem"] = stem
        for key in (
            "table_image_required",
            "block_image_required",
        ):
            if recording_source.get(key) is True:
                recording[key] = True
        block_kind = _text(recording_source.get("block_kind"), limit=32)
        if block_kind in {"table", "drawing_group"}:
            recording["block_kind"] = block_kind
        block_index = recording_source.get("block_index")
        if isinstance(block_index, int) and not isinstance(block_index, bool) and block_index >= 0:
            recording["block_index"] = block_index
        table_index = recording_source.get("table_index")
        if isinstance(table_index, int) and not isinstance(table_index, bool) and table_index >= 0:
            recording["table_index"] = table_index
        image_artifact_id = _text(
            recording_source.get("image_artifact_id")
            or recording_source.get("table_image_artifact_id"),
            limit=256,
        )
        if image_artifact_id:
            recording["image_artifact_id"] = image_artifact_id
        instruction_text = _text(
            recording_source.get("instruction_text"),
            limit=PAGE_INPUT_MAX_TEXT,
        )
        if instruction_text:
            recording["instruction_text"] = instruction_text
        occurrence = recording_source.get("instruction_occurrence")
        if isinstance(occurrence, int) and not isinstance(occurrence, bool) and occurrence >= 0:
            recording["instruction_occurrence"] = occurrence
        instruction_stem = _safe_stem(recording_source.get("instruction_audio_filename_stem"))
        if instruction_stem:
            recording["instruction_audio_filename_stem"] = instruction_stem
        asking_instruction_text = _text(
            recording_source.get("asking_instruction_text"),
            limit=PAGE_INPUT_MAX_TEXT,
        )
        if asking_instruction_text:
            recording["asking_instruction_text"] = asking_instruction_text
        asking_instruction_occurrence = recording_source.get("asking_instruction_occurrence")
        if (
            isinstance(asking_instruction_occurrence, int)
            and not isinstance(asking_instruction_occurrence, bool)
            and asking_instruction_occurrence >= 0
        ):
            recording["asking_instruction_occurrence"] = asking_instruction_occurrence
        asking_instruction_stem = _safe_stem(
            recording_source.get("asking_instruction_audio_filename_stem")
        )
        if asking_instruction_stem:
            recording["asking_instruction_audio_filename_stem"] = asking_instruction_stem
        recording["questions"] = []
        result["recording"] = recording

        raw_retelling = value.get("retelling")
        if isinstance(raw_retelling, Mapping):
            retelling: dict[str, Any] = {}
            prompt = _text(
                raw_retelling.get("prompt")
                or raw_retelling.get("stem")
                or raw_retelling.get("text"),
                limit=PAGE_INPUT_MAX_TEXT,
            )
            if prompt:
                retelling["prompt"] = prompt
            for key in ("score",):
                number_value = _number(raw_retelling.get(key))
                if number_value is not None:
                    retelling[key] = number_value
            references = _string_list(
                raw_retelling.get("reference_answers"),
                limit=PAGE_INPUT_MAX_LIST,
            )
            if references:
                retelling["reference_answers"] = references
            prompt_stem = _safe_stem(raw_retelling.get("prompt_audio_filename_stem"))
            if prompt_stem:
                retelling["prompt_audio_filename_stem"] = prompt_stem
            result["retelling"] = retelling

        raw_asking = value.get("asking")
        asking = (
            raw_asking
            if isinstance(raw_asking, Sequence)
            and not isinstance(raw_asking, (bytes, bytearray, str))
            else []
        )
        normalized_asking = []
        for item in list(asking)[:PAGE_INPUT_MAX_LIST]:
            question = _question(item)
            if question:
                normalized_asking.append(question)
        result["asking"] = normalized_asking
    else:
        raw_recording = value.get("recording")
        recording_source = raw_recording if isinstance(raw_recording, Mapping) else {}
        recording: dict[str, Any] = {}
        listening_text = _page_listening_text(
            _text(
                recording_source.get("listening_text") or recording_source.get("text"),
                limit=PAGE_INPUT_MAX_TEXT,
            )
        )
        if listening_text:
            recording["listening_text"] = listening_text
        image_artifact_id = _text(
            recording_source.get("image_artifact_id")
            or recording_source.get("table_image_artifact_id"),
            limit=256,
        )
        if image_artifact_id:
            # Only an internal artifact reference crosses the durable
            # boundary.  Local paths and URLs are intentionally not accepted;
            # the trusted page adapter resolves this ID through ArtifactStore
            # immediately before the visible upload control is used.
            recording["image_artifact_id"] = image_artifact_id
        if recording_source.get("table_image_required") is True:
            recording["table_image_required"] = True
        if recording_source.get("block_image_required") is True:
            recording["block_image_required"] = True
        block_kind = _text(recording_source.get("block_kind"), limit=32)
        if block_kind in {"table", "drawing_group"}:
            recording["block_kind"] = block_kind
        block_index = recording_source.get("block_index")
        if isinstance(block_index, int) and not isinstance(block_index, bool) and block_index >= 0:
            recording["block_index"] = block_index
        table_index = recording_source.get("table_index")
        if isinstance(table_index, int) and not isinstance(table_index, bool) and table_index >= 0:
            recording["table_index"] = table_index
        raw_questions = recording_source.get("questions")
        questions = raw_questions if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (bytes, bytearray, str)) else []
        normalized_questions: list[dict[str, Any]] = []
        for item in list(questions)[:PAGE_INPUT_MAX_LIST]:
            question = _question(item)
            if question:
                normalized_questions.append(question)
        recording["questions"] = normalized_questions
        result["recording"] = recording
        raw_retelling = value.get("retelling")
        if isinstance(raw_retelling, Mapping):
            retelling: dict[str, Any] = {}
            prompt = _text(raw_retelling.get("prompt") or raw_retelling.get("stem") or raw_retelling.get("text"), limit=PAGE_INPUT_MAX_TEXT)
            if prompt:
                retelling["prompt"] = prompt
            for key in ("score", "answer_time"):
                number_value = _number(raw_retelling.get(key))
                if number_value is not None:
                    retelling[key] = number_value
            references = _string_list(raw_retelling.get("reference_answers"), limit=PAGE_INPUT_MAX_LIST)
            if references:
                retelling["reference_answers"] = references
            result["retelling"] = retelling

    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > PAGE_INPUT_MAX_BYTES:
        raise PageInputFactsError("page_input 内容超过安全大小上限")
    return result


def sanitize_page_input(
    value: Any,
    *,
    input_type: str | None = None,
) -> dict[str, Any] | None:
    """Dispatch page facts through the registered, type-specific sanitizer."""

    if value in (None, ""):
        return None
    if not isinstance(value, Mapping):
        raise PageInputFactsError("page_input 必须是对象")
    key = _text(input_type or value.get("input_type") or "paper", limit=32).casefold()
    sanitizer = _PAGE_INPUT_SANITIZERS.get(key)
    if sanitizer is None:
        raise PageInputFactsError(f"当前录入类型没有注册页面内容契约: {key}")
    return sanitizer(value)


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_non_negative_number(value: Any) -> bool:
    number = _number(value)
    return number is not None and number >= 0


def _choice_question_complete(value: Any) -> bool:
    if not isinstance(value, Mapping) or not _has_text(value.get("prompt")):
        return False
    options = value.get("options")
    if not isinstance(options, list) or len(options) < 2:
        return False
    answer = _text(value.get("answer"), limit=128)
    if not answer:
        return False
    answer_folded = answer.casefold()
    if not any(
        answer_folded == _text(option.get("option_id"), limit=32).casefold()
        or answer == _text(option.get("text"), limit=PAGE_INPUT_MAX_TEXT)
        for option in options
        if isinstance(option, Mapping)
    ):
        return False
    return _has_non_negative_number(value.get("score"))


def page_input_completeness(value: Any) -> dict[str, Any]:
    """Classify whether one page fact can pass the page adapter.

    Parsing intentionally keeps partial facts visible for review. This
    separate check is shared by the durable start gate and the executor so a
    missing answer or score cannot turn into a late page-run failure.
    """

    if value in (None, ""):
        return {"status": "missing", "reason": "缺少页面内容事实"}
    try:
        facts = sanitize_page_input(value)
    except PageInputFactsError:
        return {"status": "invalid", "reason": "页面内容事实格式无效"}
    if facts is None:
        return {"status": "missing", "reason": "缺少页面内容事实"}

    group_type = facts["type"]
    if group_type == "听后选择":
        materials = facts.get("materials")
        if not isinstance(materials, list) or not materials:
            return {"status": "incomplete", "reason": "听后选择缺少录音材料"}
        complete = all(
            isinstance(material, Mapping)
            and _has_text(material.get("listening_text"))
            and isinstance(material.get("questions"), list)
            and bool(material["questions"])
            and all(_choice_question_complete(question) for question in material["questions"])
            for material in materials
        )
        reason = "听后选择页面内容不完整（请补齐题干、选项、答案和分值）"
    elif group_type == "信息获取":
        materials = facts.get("materials")
        complete = (
            isinstance(materials, list)
            and bool(materials)
            and all(
                isinstance(material, Mapping)
                and _has_text(material.get("listening_text"))
                and _has_text(material.get("audio_filename_stem"))
                and isinstance(material.get("questions"), list)
                and bool(material["questions"])
                and all(
                    isinstance(question, Mapping)
                    and _has_text(question.get("prompt"))
                    and _has_non_negative_number(question.get("score"))
                    and bool(_string_list(question.get("reference_answers")))
                    for question in material["questions"]
                )
                for material in materials
            )
        )
        reason = "信息获取页面内容不完整（请补齐录音、题干、分值和参考答案）"
    elif group_type == "听后应答":
        questions = facts.get("questions")
        complete = (
            isinstance(questions, list)
            and bool(questions)
            and all(_choice_question_complete(question) for question in questions)
        )
        reason = "听后应答页面内容不完整（请补齐选项、答案和分值）"
    elif group_type == "模仿朗读":
        questions = facts.get("questions")
        complete = (
            isinstance(questions, list)
            and bool(questions)
            and all(
                isinstance(question, Mapping)
                and _has_text(question.get("listening_text"))
                and _has_non_negative_number(question.get("score"))
                for question in questions
            )
        )
        reason = "模仿朗读页面内容不完整（请补齐原文和分值）"
    elif group_type == "信息转述及询问":
        recording = facts.get("recording")
        retelling = facts.get("retelling")
        asking = facts.get("asking")
        image_required = (
            isinstance(recording, Mapping)
            and (
                recording.get("table_image_required") is True
                or recording.get("block_image_required") is True
            )
        )
        if image_required and not _has_text(recording.get("image_artifact_id")):
            return {
                "status": "incomplete",
                "reason": "信息转述缺少思维导图图片产物，不能开始录入",
            }
        recording_complete = (
            isinstance(recording, Mapping)
            and _has_text(recording.get("listening_text"))
            and _has_text(recording.get("audio_filename_stem"))
            and _has_text(recording.get("instruction_text"))
            and _has_text(recording.get("instruction_audio_filename_stem"))
            and _has_text(recording.get("asking_instruction_text"))
            and _has_text(recording.get("asking_instruction_audio_filename_stem"))
        )
        retelling_complete = (
            isinstance(retelling, Mapping)
            and _has_text(retelling.get("prompt"))
            and _has_non_negative_number(retelling.get("score"))
            and _has_text(retelling.get("prompt_audio_filename_stem"))
            and bool(_string_list(retelling.get("reference_answers")))
        )
        asking_complete = (
            isinstance(asking, list)
            and bool(asking)
            and all(
                isinstance(question, Mapping)
                and _has_text(question.get("prompt"))
                and _has_non_negative_number(question.get("score"))
                and bool(_string_list(question.get("reference_answers")))
                for question in asking
            )
        )
        complete = recording_complete and retelling_complete and asking_complete
        reason = "信息转述及询问页面内容不完整（请补齐思维导图、题干音频、分值和参考答案）"
    else:
        recording = facts.get("recording")
        retelling = facts.get("retelling")
        recording_questions = recording.get("questions") if isinstance(recording, Mapping) else None
        recording_complete = (
            isinstance(recording, Mapping)
            and _has_text(recording.get("listening_text"))
            and isinstance(recording_questions, list)
            and bool(recording_questions)
            and all(
                isinstance(question, Mapping)
                and _has_non_negative_number(question.get("score"))
                and bool(
                    _string_list(question.get("answers"))
                    or _string_list(question.get("reference_answers"))
                    or _text(question.get("answer"), limit=PAGE_INPUT_MAX_TEXT)
                )
                for question in recording_questions
            )
        )
        image_required = (
            isinstance(recording, Mapping)
            and (
                recording.get("table_image_required") is True
                or recording.get("block_image_required") is True
            )
        )
        if image_required and not _has_text(recording.get("image_artifact_id")):
            return {
                "status": "incomplete",
                "reason": "听后记录缺少文档块图片产物，不能开始录入",
            }
        retelling_complete = (
            isinstance(retelling, Mapping)
            and _has_text(retelling.get("prompt"))
            and _has_non_negative_number(retelling.get("score"))
            and isinstance(retelling.get("reference_answers"), list)
            and bool(retelling["reference_answers"])
        )
        complete = recording_complete and retelling_complete
        reason = "听后记录并转述信息页面内容不完整（请补齐填空答案、转述题干、分值和参考答案）"

    return {"status": "complete" if complete else "incomplete", "reason": None if complete else reason}


def page_input_collection_status(
    values: Sequence[Any],
    *,
    required: bool,
    auxiliary: Sequence[bool] | None = None,
) -> dict[str, Any]:
    """Classify page facts for all content segments in one input unit."""

    raw_page_inputs = list(values)
    auxiliary_flags = list(auxiliary or [])
    page_inputs = [
        value
        for index, value in enumerate(raw_page_inputs)
        if not (
            index < len(auxiliary_flags)
            and auxiliary_flags[index] is True
        )
    ]
    structured = [value for value in page_inputs if value not in (None, "")]
    summary = {
        "structured_count": len(structured),
        "expected_count": len(page_inputs),
        "auxiliary_count": len(raw_page_inputs) - len(page_inputs),
    }
    if not structured and not required:
        return {"status": "not_required", "reason": None, **summary}
    if not structured:
        return {
            "status": "incomplete",
            "reason": "听说考试缺少结构化页面内容事实，不能开始录入",
            **summary,
        }
    if len(structured) != len(page_inputs):
        return {
            "status": "incomplete",
            "reason": "录入单元的页面内容事实不完整，不能开始录入",
            **summary,
        }
    for value in structured:
        result = page_input_completeness(value)
        if result["status"] != "complete":
            return {
                "status": "incomplete",
                "reason": result["reason"] or "页面内容事实不完整，不能开始录入",
                **summary,
            }
    return {"status": "complete", "reason": None, **summary}


def document_entry_support(
    page_inputs: Sequence[Any],
    *,
    input_type: str | None = None,
    document_types: Sequence[Any] | None = None,
    major_section_profiles: Sequence[Any] | None = None,
    entry_profiles: Sequence[Any] | None = None,
    capabilities: Sequence[Any] | None = None,
    auxiliary: Sequence[bool] | None = None,
) -> dict[str, Any]:
    """Classify whether one parsed document has a supported entry shape.

    The parser can preserve partial page facts for review, so this check is
    intentionally separate from :func:`page_input_collection_status`.  A
    recognized shape is allowed to continue to the review screen even when a
    required fact is incomplete; the later start gate still blocks the
    external write.  Rows without a reliable preflight result are also
    treated as unsupported: the user-facing contract intentionally has only
    two outcomes, and missing facts cannot open an external input flow safely.
    In particular, an imitation-reading row must carry one of the registered
    source-layout profiles, the registered entry profile and explicitly
    advertise ``capabilities.external_input``; the presence of a page-input
    object alone is never sufficient evidence.
    """

    raw_values = list(page_inputs or [])
    raw_document_types = list(document_types or [])
    raw_major_section_profiles = list(major_section_profiles or [])
    raw_entry_profiles = list(entry_profiles or [])
    raw_capabilities = list(capabilities or [])
    auxiliary_flags = list(auxiliary or [])
    if auxiliary_flags:
        active_indexes = [
            index
            for index in range(len(raw_values))
            if not (index < len(auxiliary_flags) and auxiliary_flags[index] is True)
        ]
        raw_values = [raw_values[index] for index in active_indexes]
        raw_document_types = [
            raw_document_types[index]
            for index in active_indexes
            if index < len(raw_document_types)
        ]
        raw_major_section_profiles = [
            raw_major_section_profiles[index]
            for index in active_indexes
            if index < len(raw_major_section_profiles)
        ]
        raw_entry_profiles = [
            raw_entry_profiles[index]
            for index in active_indexes
            if index < len(raw_entry_profiles)
        ]
        raw_capabilities = [
            raw_capabilities[index]
            for index in active_indexes
            if index < len(raw_capabilities)
        ]
    detected: list[str] = []
    page_types_by_index: list[str] = []
    invalid_count = 0
    structured_count = 0
    for raw in raw_values:
        page_type = ""
        if raw in (None, ""):
            page_types_by_index.append(page_type)
            continue
        structured_count += 1
        try:
            facts = sanitize_page_input(raw)
        except PageInputFactsError:
            invalid_count += 1
            page_types_by_index.append(page_type)
            continue
        if facts is None:
            page_types_by_index.append(page_type)
            continue
        page_type = _text(facts.get("type"), limit=64)
        page_types_by_index.append(page_type)
        if page_type and page_type not in detected:
            detected.append(page_type)

    known_document_types: list[str] = []
    for raw in raw_document_types:
        value = _text(raw, limit=128)
        if value and value not in known_document_types:
            known_document_types.append(value)

    major_section_profile_invalid_count = 0
    entry_profile_invalid_count = 0
    entry_capability_invalid_count = 0
    scope_count = max(
        len(raw_values),
        len(raw_document_types),
        len(raw_major_section_profiles),
        len(raw_entry_profiles),
        len(raw_capabilities),
    )
    for index in range(scope_count):
        document_type = _text(
            raw_document_types[index] if index < len(raw_document_types) else "",
            limit=128,
        )
        page_type = page_types_by_index[index] if index < len(page_types_by_index) else ""
        if is_imitation_document_type(document_type) or is_imitation_document_type(page_type):
            major_section_profile = _text(
                raw_major_section_profiles[index]
                if index < len(raw_major_section_profiles)
                else "",
                limit=128,
            )
            if major_section_profile not in SUPPORTED_IMITATION_SECTION_PROFILES:
                major_section_profile_invalid_count += 1
            entry_profile = _text(
                raw_entry_profiles[index] if index < len(raw_entry_profiles) else "",
                limit=128,
            )
            if entry_profile != IMITATION_READING_ENTRY_PROFILE:
                entry_profile_invalid_count += 1
            raw_capability = (
                raw_capabilities[index]
                if index < len(raw_capabilities)
                else {}
            )
            if not isinstance(raw_capability, Mapping) or raw_capability.get("external_input") is not True:
                entry_capability_invalid_count += 1

    normalized_input_type = _text(input_type, limit=32).casefold()
    detected_set = set(detected)
    profile = next(
        (
            candidate
            for candidate in DOCUMENT_ENTRY_PROFILES
            if detected_set == set(candidate["types"])
        ),
        None,
    )
    record_retelling_profile = (
        profile is not None
        and profile["format"] == "listening_record_retelling"
    )
    response_profile = (
        profile is not None
        and profile["format"] == "listening_response"
    )
    matched_profile = profile
    if response_profile:
        for index, page_type in enumerate(page_types_by_index):
            if page_type != "听后应答":
                continue
            major_section_profile = _text(
                raw_major_section_profiles[index]
                if index < len(raw_major_section_profiles)
                else "",
                limit=128,
            )
            if major_section_profile != RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE:
                major_section_profile_invalid_count += 1
            entry_profile = _text(
                raw_entry_profiles[index] if index < len(raw_entry_profiles) else "",
                limit=128,
            )
            if entry_profile != RESPONSE_ENTRY_PROFILE:
                entry_profile_invalid_count += 1
            raw_capability = (
                raw_capabilities[index]
                if index < len(raw_capabilities)
                else {}
            )
            if not isinstance(raw_capability, Mapping) or raw_capability.get("external_input") is not True:
                entry_capability_invalid_count += 1
    if record_retelling_profile:
        for index, page_type in enumerate(page_types_by_index):
            if page_type != "听后记录并转述信息":
                continue
            major_section_profile = _text(
                raw_major_section_profiles[index]
                if index < len(raw_major_section_profiles)
                else "",
                limit=128,
            )
            if major_section_profile != RECORD_RETELLING_TABLE_SPECIAL_PROFILE:
                major_section_profile_invalid_count += 1
            entry_profile = _text(
                raw_entry_profiles[index] if index < len(raw_entry_profiles) else "",
                limit=128,
            )
            if entry_profile != RECORD_RETELLING_ENTRY_PROFILE:
                entry_profile_invalid_count += 1
            raw_capability = (
                raw_capabilities[index]
                if index < len(raw_capabilities)
                else {}
            )
            if not isinstance(raw_capability, Mapping) or raw_capability.get("external_input") is not True:
                entry_capability_invalid_count += 1
    # A single malformed page fact means the parser did not provide a
    # reliable document-level preflight.  Keep the review view closed rather
    # than treating the other recognized types as sufficient evidence.
    if (
        invalid_count
        or major_section_profile_invalid_count
        or entry_profile_invalid_count
        or entry_capability_invalid_count
    ):
        profile = None
    if normalized_input_type == "textbook":
        # 课文跟读没有逐题 page_input；它的结构证据是解析器给出的文档
        # 类型。只有当全部已知文档类型都是“课文跟读”时才认为可录入；
        # 混入其他题型或完全没有类型事实时保持 fail-closed。
        if known_document_types and all(
            value == "课文跟读" for value in known_document_types
        ):
            profile = dict(TEXTBOOK_DOCUMENT_ENTRY_PROFILE)
            status = "supported"
            reason = None
        else:
            profile = None
            status = "unsupported"
            reason = "当前文档没有可靠的课文跟读结构判断，暂不能按课文录入。"
    elif normalized_input_type not in {"", "paper"}:
        profile = None
        status = "unsupported"
        reason = "当前录入类型还没有接入可用的文档录入脚本。"
    elif major_section_profile_invalid_count:
        status = "unsupported"
        profile_label = (
            "听后记录并转述信息" if record_retelling_profile
            else "听后应答" if response_profile
            else "模仿朗读"
        )
        reason = f"存在未开放或未确认的{profile_label}版式画像，当前暂不支持文稿录入。"
    elif entry_profile_invalid_count:
        status = "unsupported"
        profile_label = (
            "听后记录并转述信息" if record_retelling_profile
            else "听后应答" if response_profile
            else "模仿朗读"
        )
        reason = f"存在未开放或未确认的{profile_label}录入画像，当前暂不支持文稿录入。"
    elif entry_capability_invalid_count:
        status = "unsupported"
        profile_label = (
            "听后记录并转述信息" if record_retelling_profile
            else "听后应答" if response_profile
            else "模仿朗读"
        )
        reason = f"{profile_label}的录入字段尚未确认完整，当前暂不支持文稿录入。"
    elif invalid_count:
        status = "unsupported"
        reason = "当前文档的页面内容事实格式无效，暂不支持文稿录入。"
    elif profile is not None:
        status = "supported"
        reason = None
    elif detected or invalid_count or known_document_types:
        status = "unsupported"
        reason = (
            "当前录入脚本只支持“听说测试题”“模仿朗读”“听后应答”和"
            "“听后记录并转述信息”四种文档结构；"
            "这份文档尚未形成可录入的完整结构。"
        )
    else:
        # Some old workspaces predate page_input facts.  Missing preflight
        # evidence is fail-closed and belongs to the same user-facing
        # “暂不支持” outcome as a positively unsupported parser shape.
        status = "unsupported"
        reason = "尚未获得文档录入结构判断，当前暂不支持文稿录入。"

    expected_profile = profile
    if expected_profile is None and normalized_input_type in {"", "paper"}:
        # Keep diagnostics tied to the structurally matched document shape even
        # when its entry profile/capability facts are stale or incomplete.
        expected_profile = matched_profile
    expected_types = list(
        expected_profile["types"]
        if expected_profile
        else DOCUMENT_ENTRY_PROFILES[0]["types"]
    )
    return {
        "schema_version": DOCUMENT_ENTRY_SCHEMA_VERSION,
        "supported": status == "supported",
        "status": status,
        "format": profile["format"] if profile else None,
        "label": profile["label"] if profile else "暂不支持",
        "reason": reason,
        "detected_types": detected,
        "document_types": known_document_types[:16],
        "expected_types": expected_types,
        "structured_count": structured_count,
        "invalid_count": invalid_count,
        "major_section_profile_invalid_count": major_section_profile_invalid_count,
        "entry_profile_invalid_count": entry_profile_invalid_count,
        "entry_capability_invalid_count": entry_capability_invalid_count,
    }


PageInputBuilder = Callable[[str, Mapping[str, Any], Mapping[str, Any], int], Mapping[str, Any] | None]
_PAGE_INPUT_BUILDERS: dict[str, PageInputBuilder] = {}
_DOCUMENT_TYPE_ALIASES = {
    "信息转述及询问": "信息转述及询问",
    "信息转述及询问题": "信息转述及询问",
}


def register_page_input_builder(
    document_type: str,
    builder: PageInputBuilder,
    *,
    replace: bool = False,
) -> None:
    """Register a code-owned parser-to-page-facts builder."""

    key = _text(document_type, limit=128)
    if not key or not callable(builder):
        raise ValueError("document_type and callable builder are required")
    if key in _PAGE_INPUT_BUILDERS and not replace:
        raise ValueError(f"page-input builder already registered: {key}")
    _PAGE_INPUT_BUILDERS[key] = builder


def _item_text(raw_item: Mapping[str, Any]) -> str:
    return _text(
        raw_item.get("listening_text") or raw_item.get("text") or raw_item.get("normalized_content"),
        limit=PAGE_INPUT_MAX_TEXT,
    )


def _item_ordinal(raw_item: Mapping[str, Any], item_index: int) -> int:
    # ``index`` is the position inside a document section, while ``number``
    # may be the global exam question number (for example 9–15 in a full
    # listening paper).  Prefer the semantic question number so the response
    # builder does not silently fall back to audio-only facts.
    value = (
        raw_item.get("question_number")
        or raw_item.get("number")
        or raw_item.get("index")
        or raw_item.get("question_index")
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = item_index + 1
    return parsed if parsed > 0 else item_index + 1


def _selection_builder(
    _document_type: str,
    result: Mapping[str, Any],
    raw_item: Mapping[str, Any],
    item_index: int,
) -> Mapping[str, Any]:
    ordinal = _item_ordinal(raw_item, item_index)
    questions = []
    raw_questions = result.get("questions")
    if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (bytes, bytearray, str)):
        questions = [
            dict(question)
            for question in raw_questions
            if isinstance(question, Mapping)
            and (
                question.get("script_ordinal") in {ordinal, str(ordinal)}
                or question.get("script_index") in {ordinal, str(ordinal)}
            )
        ]
    return {
        "schema_version": PAGE_INPUT_SCHEMA_VERSION,
        "input_type": "paper",
        "type": "听后选择",
        "materials": [{
            "listening_text": _page_listening_text(_item_text(raw_item)),
            "questions": questions,
        }],
    }


def _response_builder(
    _document_type: str,
    result: Mapping[str, Any],
    raw_item: Mapping[str, Any],
    item_index: int,
) -> Mapping[str, Any]:
    ordinal = _item_ordinal(raw_item, item_index)
    questions = []
    raw_questions = result.get("questions")
    if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (bytes, bytearray, str)):
        for question in raw_questions:
            if not isinstance(question, Mapping):
                continue
            number = question.get("number") or question.get("question_number") or question.get("index")
            if number in {ordinal, str(ordinal), None}:
                questions.append(dict(question))
                if number not in (None, ""):
                    break
    if not questions:
        questions = [{"listening_text": _item_text(raw_item)}]
    return {
        "schema_version": PAGE_INPUT_SCHEMA_VERSION,
        "input_type": "paper",
        "type": "听后应答",
        "questions": questions,
    }


def _imitation_builder(
    _document_type: str,
    result: Mapping[str, Any],
    raw_item: Mapping[str, Any],
    _item_index: int,
) -> Mapping[str, Any]:
    text = _item_text(raw_item)
    references = raw_item.get("reference_answers")
    exam_form = _text(
        raw_item.get("exam_form") or result.get("exam_form"),
        limit=32,
    ).casefold()
    # The parser profile describes the source layout, not whether the source
    # is a complete exam.  In particular, ``imitation_numbered_exam_special``
    # is one of the three special-paper layouts.  Only the document-level
    # paper decision may enable the reference-answer row.
    is_exam_paper = exam_form == "paper"
    # Some parser projections keep a shared per-question score on the result
    # envelope instead of repeating it on every source item.  The page-input
    # contract is item-shaped, so materialize that authoritative fallback here
    # before sanitization would otherwise drop the score as ``None``.
    score = raw_item.get("score")
    if score is None:
        score = result.get("score_per_item")
    question: dict[str, Any] = {
        "listening_text": text,
        "score": score,
    }
    # Only a complete listening exam has a reference-answer row for imitation
    # reading. A topic-specific special paper must leave that field absent;
    # otherwise the page adapter and document view mistake the source passage
    # for an answer that should be entered.
    if is_exam_paper:
        if references:
            question["reference_answers"] = references
        elif text:
            question["reference_answers"] = [text]
        question["reference_answers_source"] = "document"
    return {
        "schema_version": PAGE_INPUT_SCHEMA_VERSION,
        "input_type": "paper",
        "type": "模仿朗读",
        "questions": [question],
    }


def _info_acquisition_builder(
    _document_type: str,
    result: Mapping[str, Any],
    raw_item: Mapping[str, Any],
    item_index: int,
) -> Mapping[str, Any] | None:
    if not _text(raw_item.get("category"), limit=128).endswith("录音稿"):
        return None
    ordinal = _item_ordinal(raw_item, item_index)
    raw_questions = result.get("questions")
    questions = []
    if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (bytes, bytearray, str)):
        questions = [
            dict(question)
            for question in raw_questions
            if isinstance(question, Mapping)
            and (
                question.get("script_ordinal") in {ordinal, str(ordinal)}
                or question.get("script_index") in {ordinal, str(ordinal)}
            )
        ]
    section = _text(raw_item.get("category"), limit=128).removesuffix("录音稿")
    listening_text = _page_listening_text(_item_text(raw_item))
    material: dict[str, Any] = {
        "section": section,
        "listening_text": listening_text,
        "questions": questions,
    }
    stem = _safe_stem(raw_item.get("audio_filename_stem") or raw_item.get("filename_stem"))
    if stem:
        material["audio_filename_stem"] = stem
    return {
        "schema_version": PAGE_INPUT_SCHEMA_VERSION,
        "input_type": "paper",
        "type": "信息获取",
        "materials": [material],
    }


def _info_retelling_builder(
    _document_type: str,
    result: Mapping[str, Any],
    raw_item: Mapping[str, Any],
    _item_index: int,
) -> Mapping[str, Any] | None:
    if _text(raw_item.get("category"), limit=128) != "信息转述录音稿":
        return None
    source = result.get("recording") if isinstance(result.get("recording"), Mapping) else {}
    retelling = result.get("retelling") if isinstance(result.get("retelling"), Mapping) else {}
    recording: dict[str, Any] = {
            "listening_text": _page_listening_text(_item_text(raw_item))
        or _page_listening_text(source.get("listening_text")),
        "questions": [],
    }
    stem = _safe_stem(raw_item.get("audio_filename_stem") or raw_item.get("filename_stem"))
    if stem:
        recording["audio_filename_stem"] = stem
    for key in (
        "table_image_required",
        "block_image_required",
        "block_kind",
        "block_index",
        "table_index",
        "image_artifact_id",
        "instruction_text",
        "instruction_occurrence",
        "instruction_audio_filename_stem",
        "asking_instruction_text",
        "asking_instruction_occurrence",
        "asking_instruction_audio_filename_stem",
    ):
        if key in raw_item:
            recording[key] = raw_item[key]
        elif key in source:
            recording[key] = source[key]
    asking = result.get("asking")
    if not isinstance(asking, Sequence) or isinstance(asking, (bytes, bytearray, str)):
        asking = source.get("asking") if isinstance(source.get("asking"), Sequence) else []
    normalized_asking = []
    for item in asking:
        if not isinstance(item, Mapping):
            continue
        value = dict(item)
        references = _slash_split_string_list(value.get("reference_answers"))
        if not references and value.get("reference_answer"):
            references = _slash_split_string_list(value.get("reference_answer"))
        if references:
            value["reference_answers"] = references
        normalized_asking.append(value)
    return {
        "schema_version": PAGE_INPUT_SCHEMA_VERSION,
        "input_type": "paper",
        "type": "信息转述及询问",
        "recording": recording,
        "retelling": dict(retelling),
        "asking": normalized_asking,
    }


def _record_builder(
    _document_type: str,
    result: Mapping[str, Any],
    raw_item: Mapping[str, Any],
    _item_index: int,
) -> Mapping[str, Any]:
    recording = result.get("recording") if isinstance(result.get("recording"), Mapping) else {}
    retelling = result.get("retelling") if isinstance(result.get("retelling"), Mapping) else None
    recording_questions = result.get("recording_questions")
    if not isinstance(recording_questions, Sequence) or isinstance(recording_questions, (bytes, bytearray, str)):
        recording_questions = recording.get("questions") if isinstance(recording, Mapping) else []
    if not isinstance(recording_questions, Sequence) or isinstance(recording_questions, (bytes, bytearray, str)):
        recording_questions = []
    value: dict[str, Any] = {
        "schema_version": PAGE_INPUT_SCHEMA_VERSION,
        "input_type": "paper",
        "type": "听后记录并转述信息",
        "recording": {
            "listening_text": _text(recording.get("listening_text") if isinstance(recording, Mapping) else "", limit=PAGE_INPUT_MAX_TEXT) or _item_text(raw_item),
            "questions": list(recording_questions)[:PAGE_INPUT_MAX_LIST],
        },
    }
    # A document may contain multiple retelling sets.  The parser can attach
    # the drawing selector to each source item; prefer that item-local value
    # over the legacy result-level recording selector so each set receives
    # its own diagram image.
    item_has_block_selector = any(
        key in raw_item
        for key in ("block_image_required", "block_kind", "block_index")
    )
    selector = raw_item if item_has_block_selector else recording
    table_image_required = (
        selector.get("table_image_required") is True
        if isinstance(selector, Mapping)
        else False
    )
    if table_image_required:
        value["recording"]["table_image_required"] = True
    block_image_required = (
        selector.get("block_image_required") is True
        if isinstance(selector, Mapping)
        else False
    )
    if block_image_required:
        value["recording"]["block_image_required"] = True
    block_kind = _text(
        selector.get("block_kind") if isinstance(selector, Mapping) else "",
        limit=32,
    )
    if block_kind in {"table", "drawing_group"}:
        value["recording"]["block_kind"] = block_kind
    block_index = selector.get("block_index") if isinstance(selector, Mapping) else None
    if isinstance(block_index, int) and not isinstance(block_index, bool) and block_index >= 0:
        value["recording"]["block_index"] = block_index
    table_index = selector.get("table_index") if isinstance(selector, Mapping) else None
    if isinstance(table_index, int) and not isinstance(table_index, bool) and table_index >= 0:
        value["recording"]["table_index"] = table_index
    image_artifact_id = _text(
        recording.get("image_artifact_id")
        or recording.get("table_image_artifact_id")
        or raw_item.get("image_artifact_id"),
        limit=256,
    )
    if image_artifact_id:
        value["recording"]["image_artifact_id"] = image_artifact_id
    if retelling is not None:
        value["retelling"] = dict(retelling)
    return value


def _explicit_builder(
    _document_type: str,
    _result: Mapping[str, Any],
    raw_item: Mapping[str, Any],
    _item_index: int,
) -> Mapping[str, Any] | None:
    value = raw_item.get("page_input") or raw_item.get("input_payload")
    return value if isinstance(value, Mapping) else None


register_page_input_sanitizer("paper", _sanitize_paper_page_input)
register_page_input_builder("听后选择", _selection_builder)
register_page_input_builder("听后应答", _response_builder)
register_page_input_builder("模仿朗读", _imitation_builder)
register_page_input_builder("信息获取", _info_acquisition_builder)
register_page_input_builder("听后记录并转述信息", _record_builder)
register_page_input_builder("信息转述及询问", _info_retelling_builder)


def build_page_input_facts(
    document_type: str,
    result: Mapping[str, Any],
    raw_item: Mapping[str, Any],
    item_index: int,
) -> dict[str, Any] | None:
    """Build and sanitize the page facts for one parser work item."""

    if (
        is_imitation_item(document_type, raw_item, context=result)
        and _text(raw_item.get("major_section_profile"), limit=128)
        not in SUPPORTED_IMITATION_SECTION_PROFILES
    ):
        # A registered entry profile without a supported source layout is not
        # enough to make an item executable.  This keeps an old U/source item
        # closed even if stale data later gains the newer entry fields.
        return None
    if (
        is_imitation_item(document_type, raw_item, context=result)
        and _text(raw_item.get("entry_profile"), limit=128) != IMITATION_READING_ENTRY_PROFILE
    ):
        # A legacy imitation-reading item is still valid audio content, but it
        # has no registered external-page contract.  Do not let an explicit
        # input_payload or the generic type name bypass this profile gate.
        return None
    if (
        is_imitation_item(document_type, raw_item, context=result)
        and not (
            isinstance(raw_item.get("capabilities"), Mapping)
            and raw_item["capabilities"].get("external_input") is True
        )
    ):
        # A recognized layout may still be audio-only while required
        # platform fields are incomplete.  Keep the audio item but do not
        # construct an executable page-input projection.
        return None

    explicit = _explicit_builder(document_type, result, raw_item, item_index)
    builder = _PAGE_INPUT_BUILDERS.get(_text(document_type, limit=128))
    if builder is None:
        builder = _PAGE_INPUT_BUILDERS.get(_DOCUMENT_TYPE_ALIASES.get(_text(document_type, limit=128), ""))
    value = explicit if explicit is not None else builder(document_type, result, raw_item, item_index) if builder else None
    return sanitize_page_input(value)


__all__ = [
    "DOCUMENT_ENTRY_PROFILES",
    "DOCUMENT_ENTRY_SCHEMA_VERSION",
    "PAGE_INPUT_SCHEMA_VERSION",
    "PAGE_INPUT_TYPES",
    "PageInputFactsError",
    "build_page_input_facts",
    "document_entry_support",
    "page_input_collection_status",
    "page_input_completeness",
    "register_page_input_builder",
    "register_page_input_sanitizer",
    "sanitize_page_input",
]
