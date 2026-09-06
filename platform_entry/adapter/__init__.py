"""Reusable components for the 外部平台 visible-page input workflow.

The executable compatibility entry point remains
``platform_entry/paper_input.py``.  New paper types should add a handler/rule in
this package instead of growing that entry point.
"""

from .automation import PlatformInputPageAutomation
from .constants import (
    CONTENT_CREATE_PATH,
    CONTENT_UPDATE_PATH,
    PAPER_CONTENT_GET_PATH,
    PAPER_CREATE_PATH,
    PAPER_PAGE_PATH,
    PAPER_UPDATE_PATH,
)
from .errors import (
    PlatformInputExistingPaperIncompatibleError,
    PlatformInputError,
    PlatformInputLoginError,
    PlatformInputUiError,
)
from .handlers import (
    QuestionTypeHandler,
    get_question_type_handler,
    register_question_type,
    registered_question_types,
)
from .models import PlatformInputSpec, PaperBundleRule, UiChoice
from .observer import ReadOnlyFeedbackObserver
from .normalization import normalize_spec
from .rules import PAPER_BUNDLE_RULES, resolve_paper_bundle_rule
from .flow import build_dry_run_result, run_page_input

__all__ = [
    "PlatformInputError",
    "PlatformInputLoginError",
    "PlatformInputExistingPaperIncompatibleError",
    "PlatformInputPageAutomation",
    "CONTENT_CREATE_PATH",
    "CONTENT_UPDATE_PATH",
    "PAPER_CONTENT_GET_PATH",
    "PAPER_CREATE_PATH",
    "PAPER_PAGE_PATH",
    "PAPER_UPDATE_PATH",
    "PlatformInputSpec",
    "PlatformInputUiError",
    "PAPER_BUNDLE_RULES",
    "PaperBundleRule",
    "QuestionTypeHandler",
    "ReadOnlyFeedbackObserver",
    "UiChoice",
    "get_question_type_handler",
    "normalize_spec",
    "build_dry_run_result",
    "run_page_input",
    "register_question_type",
    "registered_question_types",
    "resolve_paper_bundle_rule",
]
