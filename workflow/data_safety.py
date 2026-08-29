"""Small helpers for keeping user-controlled JSON safe to persist or expose.

The workflow database is not a credential vault.  Configuration accepts a
JSON object for forward compatibility, so the repository boundary must reject
credential-like input instead of relying on individual providers or UI code to
remember which fields are safe.  Read-only projections and diagnostic
payloads use the same walker in redaction mode so older rows cannot be echoed
back verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class DataSafetyError(ValueError):
    """Raised when a value cannot be accepted as public JSON."""


_SENSITIVE_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "cookie",
    "authorization",
    "auth_header",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "private_key",
    "signing_key",
    "encryption_key",
    "bearer",
    "session_key",
    "client_secret",
    "refresh_token",
    "id_token",
)

_SAFE_INTERNAL_KEYS = {
    "fencing_token",
    "lease_fencing_token",
    "writer_fencing_token",
    "state_version",
    "token_version",
}

_SENSITIVE_TEXT = re.compile(
    r"(?ix)"
    r"(?:\b(?:bearer|basic)\s+\S+|"
    r"\b(?:token|secret|password|passwd|pwd|cookie|authorization|"
    r"api[-_ ]?key|access[-_ ]?key|client[-_ ]?secret)\s*[:=]\s*\S+)"
)


def _normalized_key(key: Any) -> str:
    text = str(key).casefold()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def is_sensitive_key(key: Any) -> bool:
    """Return whether a JSON member name normally carries bearer material."""

    normalized = _normalized_key(key)
    if not normalized or normalized in _SAFE_INTERNAL_KEYS:
        return False
    # Explicit references/IDs are allowed: the plan permits a credential
    # reference, while the secret itself must stay outside ordinary JSON.
    if normalized.endswith(("_ref", "_reference", "_id")):
        return False
    return any(
        normalized == marker
        or normalized.startswith(f"{marker}_")
        or normalized.endswith(f"_{marker}")
        or f"_{marker}_" in normalized
        for marker in _SENSITIVE_KEY_MARKERS
    )


def _clean(value: Any, *, reject_sensitive: bool, key: str = "") -> Any:
    if key and is_sensitive_key(key):
        if reject_sensitive:
            raise DataSafetyError("credential-like JSON member is not allowed")
        return "[REDACTED]"

    if isinstance(value, Mapping):
        return {
            str(member): _clean(item, reject_sensitive=reject_sensitive, key=str(member))
            for member, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item, reject_sensitive=reject_sensitive) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _SENSITIVE_TEXT.search(value):
            if reject_sensitive:
                raise DataSafetyError("credential-like text is not allowed")
            return "[REDACTED]"
        return value
    if reject_sensitive:
        raise DataSafetyError("value is not JSON serializable")
    return "[UNSERIALIZABLE]"


def validate_public_object(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and copy a user-supplied JSON object without secret fields."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DataSafetyError("expected a JSON object")
    cleaned = _clean(value, reject_sensitive=True)
    if not isinstance(cleaned, dict):  # pragma: no cover - guarded above
        raise DataSafetyError("expected a JSON object")
    return cleaned


def redact_public_json(value: Any) -> Any:
    """Copy JSON-like data while replacing sensitive or unsupported values."""

    return _clean(value, reject_sensitive=False)

