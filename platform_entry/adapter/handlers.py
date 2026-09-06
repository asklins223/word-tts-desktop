"""Question-family registry used by the parser and page workflow.

The registry is intentionally small and code-owned.  Input JSON can select a
known family by its human-readable name or alias, but it cannot register
functions, selectors, or request payloads at runtime.  A new family therefore
has one explicit registration point and remains subject to the same local
validation and visible-page safeguards as the existing families.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


GroupNormalizer = Callable[..., dict[str, Any]]
GroupCardCounter = Callable[[Mapping[str, Any]], Mapping[str, int]]


@dataclass(frozen=True)
class QuestionTypeHandler:
    """Code-owned behavior contract for one normalized question family."""

    canonical_type: str
    aliases: tuple[str, ...]
    card_kind: str
    normalizer: GroupNormalizer
    page_method: str
    card_counter: GroupCardCounter


_HANDLERS: dict[str, QuestionTypeHandler] = {}
_ALIASES: dict[str, str] = {}


def _key(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "-")


def register_question_type(
    handler: QuestionTypeHandler,
    *,
    replace: bool = False,
) -> QuestionTypeHandler:
    """Register one page-supported question family.

    Registration is explicit and rejects collisions by default.  ``replace``
    is reserved for application-owned startup customization; external JSON
    never reaches this function.
    """

    canonical = str(handler.canonical_type or "").strip()
    if not canonical:
        raise ValueError("question type handler needs a canonical_type")
    if not callable(handler.normalizer):
        raise TypeError(f"question type handler {canonical!r} needs a normalizer")
    if not str(handler.card_kind or "").strip():
        raise ValueError(f"question type handler {canonical!r} needs a card_kind")
    if not str(handler.page_method or "").strip():
        raise ValueError(f"question type handler {canonical!r} needs a page_method")
    if not callable(handler.card_counter):
        raise TypeError(f"question type handler {canonical!r} needs a card_counter")

    canonical_key = _key(canonical)
    if not replace and canonical_key in _HANDLERS:
        raise ValueError(f"question type already registered: {canonical}")
    occupied = {canonical_key, *(_key(alias) for alias in handler.aliases)}
    if not replace:
        collisions = sorted(alias for alias in occupied if alias in _ALIASES)
        if collisions:
            raise ValueError(
                "question type alias already registered: " + ", ".join(collisions)
            )

    _HANDLERS[canonical_key] = handler
    for alias in occupied:
        _ALIASES[alias] = canonical
    return handler


def get_question_type_handler(value: Any) -> QuestionTypeHandler | None:
    """Return the registered handler for a canonical name or alias."""

    canonical = _ALIASES.get(_key(value))
    return _HANDLERS.get(_key(canonical)) if canonical else None


def registered_question_types() -> tuple[QuestionTypeHandler, ...]:
    """Return handlers in registration order for diagnostics and tooling."""

    return tuple(_HANDLERS.values())


def register_aliases_from_mapping(mapping: dict[str, str]) -> None:
    """Add legacy aliases after the core handler has been registered.

    Keeping this helper separate lets constants retain their backwards-
    compatible public mapping while the behavior registry owns dispatch.
    """

    for alias, canonical in mapping.items():
        handler = _HANDLERS.get(_key(canonical))
        if handler is None:
            continue
        alias_key = _key(alias)
        existing = _ALIASES.get(alias_key)
        if existing is not None and existing != canonical:
            raise ValueError(f"question type alias collision: {alias}")
        _ALIASES[alias_key] = handler.canonical_type


__all__ = [
    "GroupNormalizer",
    "GroupCardCounter",
    "QuestionTypeHandler",
    "get_question_type_handler",
    "register_aliases_from_mapping",
    "register_question_type",
    "registered_question_types",
]
