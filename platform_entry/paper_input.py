#!/usr/bin/env python3
"""Compatibility CLI for the modular 外部平台 visible-page input workflow.

The implementation lives under ``platform_entry.adapter``.  This file intentionally
remains as a stable command/module boundary because the desktop executor,
existing JSON jobs, and documented command line all import it directly.
New question types and paper bundles belong in the package modules, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_entry.adapter.automation import PlatformInputPageAutomation
from platform_entry.adapter.cli import (
    _error_result,
    _load_json,
    build_parser,
    main,
)
from platform_entry.adapter.common import (
    _is_imitation_template,
    _normalise_text,
    _text_without_html,
)
from platform_entry.adapter.constants import *  # noqa: F403,F401
from platform_entry.adapter.errors import (
    PlatformInputExistingPaperIncompatibleError,
    PlatformInputError,
    PlatformInputLoginError,
    PlatformInputUiError,
)
from platform_entry.adapter.flow import build_dry_run_result, run_page_input
from platform_entry.adapter.handlers import (
    QuestionTypeHandler,
    get_question_type_handler,
    register_question_type,
    registered_question_types,
)
from platform_entry.adapter.models import PlatformInputSpec, PaperBundleRule, UiChoice
from platform_entry.adapter.normalization import (
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
    normalize_spec as _normalize_spec,
)
from platform_entry.adapter.observer import ReadOnlyFeedbackObserver
from platform_entry.adapter.rules import (
    PAPER_BUNDLE_RULES as _REGISTERED_PAPER_BUNDLE_RULES,
    resolve_paper_bundle_rule as _resolve_paper_bundle_rule,
)
from platform_entry.adapter.runtime import (
    _default_profile_dir,
    _launch_browser,
    execute_live,
    verify_live,
)


# Keep the old patchable module-level registry.  The wrappers below pass this
# value into the modular implementations so existing tests/integrations that
# replace ``platform_entry.paper_input.PAPER_BUNDLE_RULES`` remain deterministic.
PAPER_BUNDLE_RULES = _REGISTERED_PAPER_BUNDLE_RULES


def resolve_paper_bundle_rule(
    template_name: Any,
    *,
    paper_name: Any = None,
    template_id: Any = None,
    explicit_key: Any = None,
) -> PaperBundleRule | None:
    return _resolve_paper_bundle_rule(
        template_name,
        paper_name=paper_name,
        template_id=template_id,
        explicit_key=explicit_key,
        rules=PAPER_BUNDLE_RULES,
    )


def normalize_spec(raw: dict[str, Any], *, base_dir: Path | None = None) -> PlatformInputSpec:
    return _normalize_spec(raw, base_dir=base_dir, rules=PAPER_BUNDLE_RULES)

if __name__ == "__main__":
    raise SystemExit(main())
