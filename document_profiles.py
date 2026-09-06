"""Shared parser-profile identifiers used by the audio and entry paths.

The identifiers live outside ``question_types`` and ``workflow`` so the
parser can publish structural facts without making the page-input module the
authority for document detection.  A profile describes the source layout;
the entry profile describes a registered external-page contract.
"""

from __future__ import annotations

from collections.abc import Mapping


IMITATION_LEGACY_UNIT_SOURCE_PROFILE = "imitation_legacy_unit_source"
IMITATION_UNIT_SOURCE_SPECIAL_PROFILE = "imitation_unit_source_special"
IMITATION_BOXED_SPECIAL_PROFILE = "imitation_boxed_special"
IMITATION_NUMBERED_EXAM_SPECIAL_PROFILE = "imitation_numbered_exam_special"
IMITATION_UNKNOWN_PROFILE = "imitation_unknown"
IMITATION_READING_ENTRY_PROFILE = "imitation_reading_v1"
RECORD_RETELLING_TABLE_SPECIAL_PROFILE = "record_retelling_table_special"
RECORD_RETELLING_UNKNOWN_PROFILE = "record_retelling_unknown"
RECORD_RETELLING_ENTRY_PROFILE = "listening_record_retelling_v1"
RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE = "response_colored_options_special"
RESPONSE_UNKNOWN_PROFILE = "response_unknown"
RESPONSE_ENTRY_PROFILE = "listening_response_v1"
IMITATION_DOCUMENT_TYPE_NAMES = frozenset({"模仿朗读", "imitation_reading"})
_IMITATION_DOCUMENT_TYPE_NAMES_CASEFOLDED = frozenset(
    name.casefold() for name in IMITATION_DOCUMENT_TYPE_NAMES
)

SUPPORTED_IMITATION_SECTION_PROFILES = frozenset({
    IMITATION_UNIT_SOURCE_SPECIAL_PROFILE,
    IMITATION_BOXED_SPECIAL_PROFILE,
    IMITATION_NUMBERED_EXAM_SPECIAL_PROFILE,
})


def is_imitation_document_type(value: object) -> bool:
    """Return whether a persisted document/category label is imitation reading.

    Older parser projections sometimes used a source suffix such as
    ``模仿朗读-外网`` or ``模仿朗读-教材`` as the document type.  Treat those
    labels as the same family for safety checks, while leaving the suffix
    available as a display/category fact.
    """

    normalized = str(value or "").strip().casefold()
    if normalized in _IMITATION_DOCUMENT_TYPE_NAMES_CASEFOLDED:
        return True
    return (
        normalized.startswith("模仿朗读-")
        or normalized.startswith("模仿朗读/")
        or normalized.startswith("imitation_reading-")
        or normalized.startswith("imitation_reading/")
    )


def is_imitation_item(
    document_type: object,
    raw_item: Mapping[str, object] | None = None,
    *,
    context: Mapping[str, object] | None = None,
) -> bool:
    """Return whether an item carries any known imitation-reading label.

    A few pre-normalized workspaces used a generic top-level ``doc_type`` and
    kept the actual family only in ``category``.  Profile gates must inspect
    both fields; otherwise an old explicit ``page_input`` can survive parser
    normalization merely because the first label is generic.  ``context`` is
    the parser-result envelope for older projections that kept the specific
    category on the result rather than copying it onto every item.
    """

    values = [document_type]
    for candidate in (context, raw_item):
        if not isinstance(candidate, Mapping):
            continue
        values.extend(
            candidate.get(key)
            for key in ("doc_type", "document_type", "category", "item_type")
        )
        # Some legacy projections kept a generic document/category label and
        # exposed the real family only through an already-built page fact.
        # Treat that fact as a safety signal, never as permission: the caller
        # still has to validate the source/entry profiles and capabilities.
        for payload_key in ("page_input", "input_payload"):
            payload = candidate.get(payload_key)
            if isinstance(payload, Mapping):
                values.extend(payload.get(key) for key in ("type", "question_type"))
    return any(is_imitation_document_type(value) for value in values)

__all__ = [
    "IMITATION_BOXED_SPECIAL_PROFILE",
    "IMITATION_LEGACY_UNIT_SOURCE_PROFILE",
    "IMITATION_UNIT_SOURCE_SPECIAL_PROFILE",
    "IMITATION_NUMBERED_EXAM_SPECIAL_PROFILE",
    "IMITATION_READING_ENTRY_PROFILE",
    "IMITATION_DOCUMENT_TYPE_NAMES",
    "IMITATION_UNKNOWN_PROFILE",
    "RECORD_RETELLING_ENTRY_PROFILE",
    "RECORD_RETELLING_TABLE_SPECIAL_PROFILE",
    "RECORD_RETELLING_UNKNOWN_PROFILE",
    "RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE",
    "RESPONSE_ENTRY_PROFILE",
    "RESPONSE_UNKNOWN_PROFILE",
    "SUPPORTED_IMITATION_SECTION_PROFILES",
    "is_imitation_item",
    "is_imitation_document_type",
]
