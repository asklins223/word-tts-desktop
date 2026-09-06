"""Small dependency-free helpers shared by the split input modules."""

from __future__ import annotations

import re
from typing import Any


def normalise_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def text_without_html(value: Any) -> str:
    return re.sub(r"<[^>]*>", "", str(value or ""))


def is_imitation_template(name: Any) -> bool:
    return normalise_text(name) == "模仿朗读"


# Compatibility aliases used by the former single-file entry point and its
# tests.  The implementation remains in this focused helper module.
_normalise_text = normalise_text
_text_without_html = text_without_html
_is_imitation_template = is_imitation_template

