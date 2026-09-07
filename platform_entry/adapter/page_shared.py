"""Shared namespace for the split page-action mixins.

The mixins intentionally contain only browser actions.  This small context
module keeps their imports explicit in one place while the public entry point
and workflow orchestration stay independent from individual page controls.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .common import (
    _is_imitation_template,
    _normalise_text,
    _text_without_html,
)
from .constants import (
    ADMIN_URL,
    API_BASE_URL,
    AUDIO_LABELS,
    CONTENT_CREATE_PATH,
    CONTENT_UPDATE_PATH,
    DEFAULT_OUTLINE_TARGETS,
    DIRECT_QUESTION_TEXT_PLACEHOLDERS,
    GROUP_FILL_ORDER,
    ITEM_NUMBER_LABELS,
    PRIMARY_QUESTION_TEXT_PLACEHOLDERS,
    PAPER_CONTENT_GET_PATH,
    PAPER_PAGE_PATH,
    QUESTION_TEXT_PLACEHOLDERS,
    READ_ONLY_FEEDBACK_PATHS,
    SECTION_TEXT_PLACEHOLDERS,
    _AUDIO_LABELS,
    _DEFAULT_OUTLINE_TARGETS,
    _DIRECT_QUESTION_TEXT_PLACEHOLDERS,
    _GROUP_FILL_ORDER,
    _ITEM_NUMBER_LABELS,
    _PRIMARY_QUESTION_TEXT_PLACEHOLDERS,
    _QUESTION_TEXT_PLACEHOLDERS,
    _SECTION_TEXT_PLACEHOLDERS,
)
from .errors import PlatformInputError, PlatformInputLoginError, PlatformInputUiError
from .models import PlatformInputSpec, PaperBundleRule, UiChoice
from .normalization import (
    _first_present,
    _normalise_answer,
    _normalise_choice,
    _normalise_choice_list,
    _normalise_common_numbers,
    _normalise_group,
    _normalise_item,
    _normalise_number,
    _normalise_options,
    _normalise_question,
    _normalise_question_type,
    _normalise_record_group,
    _normalise_response_group,
    _normalise_selection_group,
    _normalise_string_list,
    _require_nonempty_text,
    _resolve_audio_path,
    _resolve_category,
    _resolve_image_path,
    _resolve_local_asset_path,
)
from .observer import ReadOnlyFeedbackObserver
from .rules import PAPER_BUNDLE_RULES


def _dom_debug_enabled() -> bool:
    """Return whether the opt-in visible-page DOM trace is enabled."""

    return os.environ.get("PLATFORM_INPUT_DOM_DEBUG", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _debug_dom_snapshot(owner: Any, label: str) -> None:
    """Print a compact, real DOM snapshot for a visible-page investigation.

    The normal runner remains quiet.  When ``PLATFORM_INPUT_DOM_DEBUG`` is
    enabled, this records the actual navigation nodes and mounted card field
    shapes at section boundaries, so a selector change can be based on the
    rendered page rather than on the template description alone.
    """

    if not _dom_debug_enabled():
        return
    page = getattr(owner, "page", None)
    if page is None:
        return

    def _safe_text(locator: Any, limit: int = 320) -> str:
        try:
            return re.sub(r"\s+", " ", str(locator.inner_text() or "")).strip()[:limit]
        except Exception:
            return ""

    def _safe_html(locator: Any, limit: int = 1400) -> str:
        try:
            return str(locator.evaluate("(el) => el.outerHTML") or "")[:limit]
        except Exception:
            return ""

    def _visible_nodes(selector: str, limit: int = 30) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            nodes = page.locator(selector)
            count = nodes.count()
        except Exception:
            return result
        for index in range(min(count, limit)):
            try:
                node = nodes.nth(index)
                if not node.is_visible():
                    continue
                result.append(
                    {
                        "index": index,
                        "text": _safe_text(node),
                        "class": str(node.get_attribute("class") or ""),
                        "html": _safe_html(node),
                    }
                )
            except Exception:
                continue
        return result

    cards: list[dict[str, Any]] = []
    try:
        card_nodes = page.locator(".optionBack:visible")
        card_count = card_nodes.count()
    except Exception:
        card_nodes = None
        card_count = 0
    if card_nodes is not None:
        for index in range(min(card_count, 20)):
            try:
                card = card_nodes.nth(index)
                editors = card.locator(
                    '.rich-text-editor .editor-content[contenteditable="true"]:visible'
                )
                editor_placeholders = [
                    str(editors.nth(editor_index).get_attribute("data-placeholder") or "")
                    for editor_index in range(min(editors.count(), 12))
                ]
                inputs = card.locator('input:visible:not([type="file"])')
                input_descriptors = []
                for input_index in range(min(inputs.count(), 12)):
                    input_node = inputs.nth(input_index)
                    input_descriptors.append(
                        {
                            "placeholder": str(input_node.get_attribute("placeholder") or ""),
                            "id": str(input_node.get_attribute("id") or ""),
                        }
                    )
                cards.append(
                    {
                        "index": index,
                        "text": _safe_text(card, 520),
                        "class": str(card.get_attribute("class") or ""),
                        "editors": editor_placeholders,
                        "inputs": input_descriptors,
                        "html": _safe_html(card),
                    }
                )
            except Exception:
                continue

    payload = {
        "label": label,
        "url": str(getattr(page, "url", "") or ""),
        "visible_qbox": _visible_nodes(".qBox:visible"),
        "visible_cards": cards,
        "visible_editors": _visible_nodes(
            '.rich-text-editor .editor-content[contenteditable="true"]:visible',
            limit=40,
        ),
    }
    print(
        "[platform-input-dom] "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )

# Ancestor-climb probe: the per-level Python loop pays one round-trip per
# ancestor (locator("xpath=..") + count), which multiplies badly on Windows.
# This resolves the whole climb inside the browser and returns the 1-based
# ancestor level holding exactly ``required`` visible matches, mirroring the
# loop's "climb one level, then test" order.  The Python caller re-checks the
# returned level through the real Playwright locator so a probe/engine
# disagreement can only cost a fallback, never a wrong element.
_ANCESTOR_LEVEL_JS_TEMPLATE = """
(node) => {
    let parent = node;
    for (let level = 0; level < %(max_level)d; level += 1) {
        parent = parent.parentElement;
        if (!parent) {
            return -1;
        }
        const matches = Array.from(parent.querySelectorAll(%(css)s));
        const visible = matches.filter((el) => {
            if (!el || typeof el.getBoundingClientRect !== 'function') {
                return false;
            }
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) {
                return false;
            }
            return window.getComputedStyle(el).visibility !== 'hidden';
        });
        if (visible.length === %(required)d) {
            return level + 1;
        }
    }
    return -1;
}
"""


def _closest_visible_level(
    node: Any,
    css: str,
    *,
    max_level: int,
    required: int = 1,
) -> int:
    """Return the nearest 1-based ancestor level with the expected matches.

    ``css`` may contain Playwright's ``:visible`` pseudo-class; it is stripped
    before running in the browser and visibility is re-checked manually with
    the same bounding-box + visibility rule used elsewhere.  Returns -1 when
    nothing qualifies or the node does not support ``evaluate`` (test shims).
    """

    evaluate = getattr(node, "evaluate", None)
    if not callable(evaluate):
        return -1
    script = _ANCESTOR_LEVEL_JS_TEMPLATE % {
        "max_level": int(max_level),
        "css": json.dumps(css.replace(":visible", "")),
        "required": int(required),
    }
    try:
        return int(evaluate(script))
    except Exception:
        return -1


def _ancestor_at_level(node: Any, level: int) -> Any:
    """Climb ``level`` ``xpath=..`` hops; building locators costs no IPC."""

    parent = node
    for _ in range(max(0, int(level))):
        parent = parent.locator("xpath=..")
    return parent


# The page mixins use this as their intentionally shared action context.  Keep
# private compatibility helpers available as well as public types/constants;
# the mixins are implementation modules, not a user-facing wildcard API.
__all__ = tuple(
    name
    for name in globals()
    if name not in {"__builtins__", "__annotations__"}
    and not name.startswith("__")
)
