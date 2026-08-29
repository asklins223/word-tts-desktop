"""Stable parser port and normalized document model.

The legacy parser remains the implementation of the supported Word/Excel
formats for now.  This module gives the workflow layer one deterministic
intermediate representation and keeps parser-specific fields out of the
database identity and cache rules.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TextIO

from .domain import DomainError, canonical_json, content_hash


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
    """Normalize one legacy parser item with deterministic identity."""

    text = str(raw.get("text") or raw.get("normalized_content") or "").strip()
    if not text:
        raise ParserError("VALIDATION_ERROR", f"parsed item {sequence} has empty content")
    category = str(raw.get("category") or raw.get("item_type") or document_type).strip()[:128]
    explicit_id = str(raw.get("identity_key") or raw.get("item_id") or "").strip()
    filename_stem = str(raw.get("filename_stem") or "").strip()
    number = str(raw.get("number") or raw.get("seq") or sequence)
    basis = explicit_id or filename_stem or f"{category}:{number}:{content_hash(text)[:16]}"
    identity = f"{source_basis}:{document_type}:{basis}"
    locator = str(raw.get("source_locator") or f"{document_type}/{category}/{number}")
    metadata = _safe_mapping({
        "doc_type": document_type,
        "category": category,
        "section": raw.get("section"),
        "number": raw.get("number"),
        "filename_stem": filename_stem or None,
        "conversation_number": raw.get("conversation_number"),
    })
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


class LegacyWordParser(ParserPort):
    """Adapter around the existing ``word_parser.py`` implementation."""

    def __init__(
        self,
        *,
        parse_callable: Callable[[str], tuple[list[Mapping[str, Any]], str]] | None = None,
        parser_version: str = "14",
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
        parse_callable = self.parse_callable or _load_legacy_parse_callable()
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
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    continue
                items.append(normalize_item(raw, sequence=len(items), source_basis=source_basis, document_type=document_type))
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


def _load_legacy_parse_callable() -> Callable[[str], tuple[list[Mapping[str, Any]], str]]:
    module_path = Path(__file__).resolve().parents[1] / "word_parser" / "word_parser.py"
    if not module_path.is_file():
        raise ParserError("PARSER_ERROR", "legacy parser module is unavailable")
    spec = importlib.util.spec_from_file_location("wordtts_legacy_word_parser", module_path)
    if spec is None or spec.loader is None:
        raise ParserError("PARSER_ERROR", "legacy parser module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = getattr(module, "parse_document_auto", None)
    if not callable(parser):
        raise ParserError("PARSER_ERROR", "legacy parser entry point is unavailable")
    return parser


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
    "LegacyWordParser", "ParsedDocument", "ParsedItem", "ParserError",
    "ParserPort", "PARSER_MODEL_VERSION", "PARSER_NORMALIZATION_VERSION",
    "document_hash", "iter_json_items", "normalize_item",
]
