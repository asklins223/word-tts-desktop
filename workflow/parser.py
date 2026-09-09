"""Stable document parser port and normalized document model.

This module gives the workflow layer one deterministic intermediate
representation and keeps parser-specific fields out of database identity and
cache rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TextIO

from document_profiles import is_imitation_item

from .domain import DomainError, canonical_json, content_hash
from .system_input_content import (
    PageInputFactsError,
    build_page_input_facts,
    sanitize_page_input,
)


PARSER_MODEL_VERSION = "1"
PARSER_NORMALIZATION_VERSION = "1"
SUPPORTED_SUFFIXES = {".docx", ".xlsx"}
_SECRET_MARKERS = ("token", "secret", "password", "cookie", "authorization", "credential", "access_key", "refresh")


class ParserError(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class ParsedItem:
    identity_key: str
    item_type: str
    sequence: int
    normalized_content: str
    role: str | None = None
    voice_key: str | None = None
    source_locator: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParsedDocument:
    schema_version: str
    parser_version: str
    normalization_version: str
    source_filename: str
    source_sha256: str
    source_size_bytes: int
    items: tuple[ParsedItem, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def item_count(self) -> int:
        return len(self.items)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["items"] = [item.as_dict() for item in self.items]
        return value


class ParserPort:
    def parse(self, source: str | os.PathLike[str], **kwargs: Any) -> ParsedDocument:
        raise NotImplementedError


def document_hash(source: str | os.PathLike[str] | bytes | bytearray | memoryview) -> tuple[str, int]:
    """Hash a document in bounded chunks without loading it into memory."""

    digest = hashlib.sha256()
    size = 0
    if isinstance(source, (bytes, bytearray, memoryview)):
        value = bytes(source)
        digest.update(value)
        return digest.hexdigest(), len(value)
    original = Path(source).expanduser()
    if original.is_symlink():
        raise ParserError("VALIDATION_ERROR", "parser source may not be a symbolic link")
    path = original.resolve()
    if not path.is_file():
        raise ParserError("NOT_FOUND", f"parser source is not a regular file: {path.name}")
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise ParserError("PERSISTENCE_ERROR", "parser source cannot be read") from exc
    return digest.hexdigest(), size


def iter_json_items(source: str | os.PathLike[str] | TextIO) -> Iterable[Mapping[str, Any]]:
    """Yield JSON item objects while preserving a narrow, safe input contract.

    Legacy ``parsed.json`` is a small array in current releases.  The
    iterator also accepts the normalized ``{"items": [...]}`` envelope, so
    importers can process one item at a time without exposing arbitrary JSON
    objects to the repository.
    """

    close_after = False
    handle: TextIO
    if hasattr(source, "read"):
        handle = source  # type: ignore[assignment]
    else:
        original = Path(source).expanduser()
        if original.is_symlink():
            raise ParserError("VALIDATION_ERROR", "parsed JSON source may not be a symbolic link")
        path = original.resolve()
        if not path.is_file():
            raise ParserError("NOT_FOUND", "parsed JSON file does not exist")
        handle = path.open("r", encoding="utf-8")
        close_after = True
    try:
        try:
            value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ParserError("VALIDATION_ERROR", "parsed JSON is invalid") from exc
        candidates: list[Any]
        if isinstance(value, list):
            candidates = value
        elif isinstance(value, Mapping) and isinstance(value.get("items"), list):
            candidates = list(value["items"])
        else:
            raise ParserError("VALIDATION_ERROR", "parsed JSON must contain an item array")
        for item in candidates:
            if isinstance(item, Mapping):
                yield item
    finally:
        if close_after:
            handle.close()


def normalize_item(
    raw: Mapping[str, Any],
    *,
    sequence: int,
    source_basis: str,
    document_type: str = "document",
) -> ParsedItem:
    """Normalize one parser item with deterministic identity."""

    text = str(raw.get("text") or raw.get("normalized_content") or "").strip()
    if not text:
        raise ParserError("VALIDATION_ERROR", f"parsed item {sequence} has empty content")
    category = str(raw.get("category") or raw.get("item_type") or document_type).strip()[:128]
    explicit_id = str(raw.get("identity_key") or raw.get("item_id") or "").strip()
    filename_stem = str(raw.get("filename_stem") or "").strip()
    audio_filename_stem = str(raw.get("audio_filename_stem") or "").strip()
    number = str(raw.get("number") or raw.get("seq") or sequence)
    basis = explicit_id or filename_stem or f"{category}:{number}:{content_hash(text)[:16]}"
    identity = f"{source_basis}:{document_type}:{basis}"
    locator = str(raw.get("source_locator") or f"{document_type}/{category}/{number}")
    page_input = None
    if raw.get("page_input") is not None or raw.get("input_payload") is not None:
        try:
            page_input = sanitize_page_input(
                raw.get("page_input") or raw.get("input_payload")
            )
        except PageInputFactsError as exc:
            raise ParserError(
                "PARSER_ERROR",
                f"parsed item {sequence} has invalid page-input facts",
            ) from exc
    metadata = _safe_mapping({
        "doc_type": document_type,
        "category": category,
        "section": raw.get("section"),
        "source": raw.get("source"),
        "number": raw.get("number"),
        "filename_stem": filename_stem or None,
        "audio_filename_stem": audio_filename_stem or None,
        "audio_only_auxiliary": raw.get("audio_only_auxiliary") is True,
        "question_numbers": raw.get("question_numbers"),
        "conversation_number": raw.get("conversation_number"),
        # Preserve parser-owned display facts for the review workbench.  The
        # legacy parsers emit the intended default gender as ``voice``; it
        # must survive normalization instead of being silently discarded.
        "voice": raw.get("voice"),
        "voice_gender": raw.get("voice_gender"),
        # Keep the generic gender field too: third-party/future parsers may
        # emit it instead of the legacy ``voice`` name, and the workflow
        # engine already treats both forms as equivalent facts.
        "gender": raw.get("gender"),
        "question_type": raw.get("question_type"),
        "sub_type_code": raw.get("sub_type_code"),
        "type_path": raw.get("type_path") or raw.get("type_hierarchy"),
        # These fields are the source-side facts used by the independent
        # system-input projection.  They are deliberately metadata only:
        # ``normalized_content`` remains the TTS input and no external write
        # is implied by retaining them here.
        "unit": raw.get("unit"),
        "unit_id": raw.get("unit_id"),
        "unit_label": raw.get("unit_label"),
        "set_number": raw.get("set_number"),
        "paper_set": raw.get("paper_set"),
        "set": raw.get("set"),
        "material_source": raw.get("material_source"),
        "raw_text": raw.get("raw_text") or raw.get("listening_text"),
        "score": raw.get("score"),
        "answer": raw.get("answer"),
        "reference_answer": raw.get("reference_answer"),
        "paper_category": raw.get("paper_category"),
        "paper_category_status": raw.get("paper_category_status"),
        "paper_category_evidence": raw.get("paper_category_evidence"),
        "exam_form": raw.get("exam_form"),
        "parse_coverage_status": raw.get("parse_coverage_status"),
        "confidence": raw.get("confidence"),
        "major_section_profile": raw.get("major_section_profile"),
        "entry_profile": raw.get("entry_profile"),
        "capabilities": raw.get("capabilities"),
        # This is a bounded, paper-only semantic side channel consumed by the
        # trusted visible-page adapter.  It is intentionally kept out of the
        # TTS text and out of the renderer's configuration projection.
        "page_input": page_input,
    })
    # ``_safe_mapping`` intentionally caps generic parser metadata at 32
    # entries per collection.  Page-input facts have their own sanitizer and
    # a larger, explicit 256-item contract; running them through the generic
    # cap would silently drop later questions/reference answers in a complete
    # paper.  Reattach the already-sanitized fact unchanged.
    if page_input is not None:
        metadata["page_input"] = page_input
    # ``_safe_mapping`` deliberately caps the generic metadata envelope at
    # 32 keys.  These three parser-owned facts are part of the document-entry
    # contract, though, and currently sit after that cap in the envelope.
    # Reattach them through the same sanitizer so the cap cannot silently
    # turn an otherwise supported full paper or imitation-reading special
    # into an unsupported document.
    metadata.update(_safe_mapping({
        "major_section_profile": raw.get("major_section_profile"),
        "entry_profile": raw.get("entry_profile"),
        "capabilities": raw.get("capabilities"),
        # 课文页面的必填“译文”显示事实；与页面事实一样绕过通用 32 键
        # 上限，避免整包被截断时静默丢掉译文。
        "translation": raw.get("translation"),
        # 角色是独立列，但课文“角色扮演/同步课文”建议依赖 metadata 里
        # 的角色事实；一并透传，保持单一事实来源。
        "role": raw.get("role"),
        # 课文录入的结构事实：段落边界与段落小标题不能从已经切成句子
        # 的正文里反推，否则同一篇文章会被错误合并成一条平台记录。
        "entry_form": raw.get("entry_form"),
        "paragraph_id": raw.get("paragraph_id"),
        "paragraph_scope": raw.get("paragraph_scope"),
        "paragraph_title": raw.get("paragraph_title"),
        # 课文文章切分的结构事实：文章标题/主题驱动录入单元切分与
        # 课文记录命名。
        "article_title": raw.get("article_title"),
        "article_theme": raw.get("article_theme"),
    }))
    return ParsedItem(
        identity_key=identity,
        item_type=category,
        sequence=int(sequence),
        normalized_content=text,
        role=_text_or_none(raw.get("role")),
        voice_key=_text_or_none(raw.get("voice_key")),
        source_locator=locator[:512],
        metadata=metadata,
    )


class DocumentParser(ParserPort):
    """Adapter around the question_types registry implementation."""

    def __init__(
        self,
        *,
        parse_callable: Callable[[str], tuple[list[Mapping[str, Any]], str]] | None = None,
        parser_version: str = "19",
    ) -> None:
        self.parse_callable = parse_callable
        self.parser_version = str(parser_version)

    def parse(self, source: str | os.PathLike[str], **kwargs: Any) -> ParsedDocument:
        original = Path(source).expanduser()
        if original.is_symlink():
            raise ParserError("VALIDATION_ERROR", "parser source may not be a symbolic link")
        path = original.resolve()
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ParserError("UNSUPPORTED_MEDIA_TYPE", "only .docx and .xlsx sources are supported")
        source_sha256, size = document_hash(path)
        source_basis = str(kwargs.get("source_basis") or source_sha256[:32])
        parse_callable = self.parse_callable or _load_parse_callable()
        try:
            raw_results, summary = parse_callable(str(path))
        except ParserError:
            raise
        except Exception as exc:
            raise ParserError("PARSER_ERROR", "legacy document parser failed") from exc
        if not isinstance(raw_results, list):
            raise ParserError("PARSER_ERROR", "legacy parser returned an invalid result")
        items: list[ParsedItem] = []
        for result_index, result in enumerate(raw_results):
            if not isinstance(result, Mapping):
                continue
            document_type = str(result.get("doc_type") or result.get("document_type") or f"document-{result_index}")
            raw_items = result.get("items")
            if not isinstance(raw_items, list):
                continue
            parse_auxiliary_audio = kwargs.get("include_auxiliary_audio") is True
            selected_items = list(raw_items)
            if parse_auxiliary_audio:
                auxiliary_items = result.get("audio_items")
                if isinstance(auxiliary_items, list):
                    selected_items.extend(auxiliary_items)
            for raw_index, raw in enumerate(selected_items):
                if not isinstance(raw, Mapping):
                    continue
                raw_for_item = dict(raw)
                try:
                    page_input = build_page_input_facts(
                        document_type,
                        result,
                        raw_for_item,
                        raw_index,
                    )
                except PageInputFactsError as exc:
                    raise ParserError(
                        "PARSER_ERROR",
                        f"{document_type} page-input facts are invalid",
                    ) from exc
                if page_input is None and is_imitation_item(
                    document_type,
                    raw_for_item,
                    context=result,
                ):
                    # An unsupported imitation-reading profile must not keep
                    # an explicit legacy page_input/input_payload supplied by
                    # an older parser projection.  Audio content remains in
                    # the normalized item; only the executable entry fact is
                    # removed.
                    raw_for_item.pop("page_input", None)
                    raw_for_item.pop("input_payload", None)
                if page_input is not None:
                    raw_for_item["page_input"] = page_input
                items.append(normalize_item(raw_for_item, sequence=len(items), source_basis=source_basis, document_type=document_type))
        if not items:
            raise ParserError("DEPENDENCY_NOT_READY", str(summary or "no supported items were parsed"))
        requested_filename = str(kwargs.get("source_filename") or "").strip()
        if requested_filename:
            requested_filename = re.split(r"[\\/]", requested_filename)[-1]
        source_filename = requested_filename if (
            requested_filename
            and Path(requested_filename).suffix.lower() in SUPPORTED_SUFFIXES
        ) else path.name
        return ParsedDocument(
            schema_version=PARSER_MODEL_VERSION,
            parser_version=self.parser_version,
            normalization_version=PARSER_NORMALIZATION_VERSION,
            source_filename=source_filename,
            source_sha256=source_sha256,
            source_size_bytes=size,
            items=tuple(items),
            metadata={"summary": str(summary or "")[:500]},
        )

def _load_parse_callable() -> Callable[[str], tuple[list[Mapping[str, Any]], str]]:
    """返回题型注册表的文档解析入口。

    统一结构读取（``parse_document_once``，文档只加载一次）。
    """
    try:
        from question_types.segmenter import parse_document_once
    except ImportError as exc:
        raise ParserError("PARSER_ERROR", "question type registry is unavailable") from exc
    return parse_document_once


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:256] if text else None


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    def clean(item: Any, key: str = "") -> Any:
        if any(marker in key.lower() for marker in _SECRET_MARKERS):
            return "[REDACTED]"
        if isinstance(item, Mapping):
            return {str(k)[:128]: clean(v, str(k)) for k, v in list(item.items())[:32]}
        if isinstance(item, (list, tuple)):
            return [clean(v, key) for v in list(item)[:32]]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item if not isinstance(item, str) else re.sub(r"(?:^|[/\\])(?:Users|home|tmp)[/\\].*", "[REDACTED_PATH]", item)[:512]
        return str(item)[:512]

    result = clean(dict(value))
    return result if isinstance(result, dict) else {}


__all__ = [
    "DocumentParser", "ParsedDocument", "ParsedItem", "ParserError",
    "ParserPort", "PARSER_MODEL_VERSION", "PARSER_NORMALIZATION_VERSION",
    "document_hash", "iter_json_items", "normalize_item",
]
