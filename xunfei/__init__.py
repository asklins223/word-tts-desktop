"""Xunfei browser provider package.

This package is the canonical public API.  Provider responsibilities are
split into focused modules while callers use this package-level surface.
"""

from .config import (
    API_SIGN_URL,
    API_WORKS_LIST_URL,
    BASE_DIR,
    DOWNLOAD_PAGE_URL,
    HOME_URL,
    IS_MAC,
    MAX_TRACKED_SUBMISSION_REQUESTS,
    MUTE_AUDIO_SCRIPT,
    OUTPUT_DIR,
    PARAM_DEFAULT,
    PARAM_MAX,
    PARAM_MIN,
    PROFILE_DIR,
    STEALTH_SCRIPT,
    WORKS_ID_FINAL_GRACE_SECONDS,
    clamp_param,
)
from .errors import (
    XunfeiBrowserLaunchError,
    XunfeiCancelled,
    XunfeiError,
    XunfeiLoginRequired,
    XunfeiQuotaExceeded,
    XunfeiRateLimited,
    XunfeiSubmissionAmbiguous,
)
from .page_scripts import AI_FLAG_KEYWORD_VARIANTS, JS
from .runtime import (
    close_session,
    ensure_session,
    is_available,
    synth_xunfei,
    synth_xunfei_batch,
    synth_xunfei_composite,
)
from .session import XunFeiSession
from .signing import _build_api_sign, _canonical_sign_value
from .voice_catalog import (
    DEFAULT_FEMALE,
    DEFAULT_MALE,
    VOICES,
    get_voice_info,
    register_voice_aliases,
    register_voice_catalog,
)

__all__ = [
    "API_SIGN_URL",
    "API_WORKS_LIST_URL",
    "AI_FLAG_KEYWORD_VARIANTS",
    "BASE_DIR",
    "DEFAULT_FEMALE",
    "DEFAULT_MALE",
    "DOWNLOAD_PAGE_URL",
    "HOME_URL",
    "IS_MAC",
    "JS",
    "MAX_TRACKED_SUBMISSION_REQUESTS",
    "MUTE_AUDIO_SCRIPT",
    "OUTPUT_DIR",
    "PARAM_DEFAULT",
    "PARAM_MAX",
    "PARAM_MIN",
    "PROFILE_DIR",
    "STEALTH_SCRIPT",
    "VOICES",
    "XunfeiCancelled",
    "XunfeiBrowserLaunchError",
    "XunfeiError",
    "XunfeiLoginRequired",
    "XunfeiQuotaExceeded",
    "XunfeiRateLimited",
    "XunfeiSubmissionAmbiguous",
    "XunFeiSession",
    "clamp_param",
    "close_session",
    "ensure_session",
    "get_voice_info",
    "is_available",
    "register_voice_aliases",
    "register_voice_catalog",
    "synth_xunfei",
    "synth_xunfei_batch",
    "synth_xunfei_composite",
    "WORKS_ID_FINAL_GRACE_SECONDS",
]
