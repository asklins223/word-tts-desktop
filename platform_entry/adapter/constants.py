"""Stable platform facts and page-input vocabulary.

This module contains only reviewed constants.  It is deliberately separate
from the question handlers so a platform selector change does not require
editing the runner or the content normalizer.
"""

from __future__ import annotations

import base64 as _base64


def restore_url(value: str) -> str:
    """Restore a deployment URL only in the running process.

    The repository only stores a reversible text encoding.  This is not a
    security boundary; it merely keeps the deployment address out of source
    listings and prevents callers from having to provide it manually.
    """

    return _base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")



# Deployment-specific origins are kept in a reversible text encoding so the
# packaged app can use them without asking the user for website settings.
# Only the origin is encoded; page routes are appended at import time.
_PLATFORM_ORIGIN = restore_url("aHR0cHM6Ly9hZG1pbi5sZXh1ZWp1bi5jbg==")
_API_ORIGIN = restore_url("aHR0cHM6Ly9hcGkubGV4dWVqdW4uY24=")

ADMIN_URL = f"{_PLATFORM_ORIGIN}/#/resource/exam"
API_BASE_URL = _API_ORIGIN
DEFAULT_PROFILE_DIR = "platform_input_chrome_profile"

# The textbook catalogue is exposed as the top-level textbook-management
# list, not the resource/text page used by the text-entry workflow.
TEXTBOOK_MANAGEMENT_URL = f"{_PLATFORM_ORIGIN}/#/textbook"

# 模板管理同时承载“题型模板”和“试卷模板”两个列表。同步器只监听页面
# 发出的只读列表响应，不会直接调用下面的接口。
TEMPLATE_MANAGEMENT_URL = f"{_PLATFORM_ORIGIN}/#/resource/template"

# 课文管理 page route, used by the standalone text-entry workflow.
RESOURCE_TEXT_ROUTE = "#/resource/text"
RESOURCE_TEXT_URL = f"{_PLATFORM_ORIGIN}/{RESOURCE_TEXT_ROUTE}"

# These paths are only observed after the visible page has made the request.
# The input workflow never calls them directly.
PAPER_PAGE_PATH = "/admin-api/system/paper/page"
PAPER_CONTENT_GET_PATH = "/admin-api/system/paper/content/get"
PAPER_FILTER_OPTIONS_PATH = "/admin-api/system/paper/filter-options"
REGION_TREE_PATH = "/admin-api/system/region/tree"
STAGE_LIST_PATH = "/admin-api/system/stage/list"
GRADE_PAGE_PATH = "/admin-api/system/grade/page"
GRADE_BY_STAGE_PATH = "/admin-api/system/grade/list-by-stage"
PAPER_TEMPLATE_PAGE_PATH = "/admin-api/system/paper-template/page"
QUESTION_TYPE_TEMPLATE_PAGE_PATH = "/admin-api/system/question-type-template/page"
QUESTION_TEMPLATE_PAGE_PATH = (
    "/admin-api/system/paper-template/question-type-library/page"
)
TEXTBOOK_PAGE_PATH = "/admin-api/system/textbook/page"

PAPER_CREATE_PATH = "/admin-api/system/paper/create"
PAPER_UPDATE_PATH = "/admin-api/system/paper/update"
CONTENT_CREATE_PATH = "/admin-api/system/paper/content/create"
CONTENT_UPDATE_PATH = "/admin-api/system/paper/content/update"
UPDATE_STATS_PATH = "/admin-api/system/paper/update-stats"

READ_ONLY_FEEDBACK_PATHS = frozenset(
    {
        PAPER_PAGE_PATH,
        PAPER_CONTENT_GET_PATH,
        PAPER_FILTER_OPTIONS_PATH,
        REGION_TREE_PATH,
        STAGE_LIST_PATH,
        GRADE_PAGE_PATH,
        GRADE_BY_STAGE_PATH,
        PAPER_TEMPLATE_PAGE_PATH,
        QUESTION_TYPE_TEMPLATE_PAGE_PATH,
        QUESTION_TEMPLATE_PAGE_PATH,
        TEXTBOOK_PAGE_PATH,
    }
)

UI_PAPER_FIELDS = frozenset(
    {
        "title",
        "province",
        "city",
        "districts",
        "stage",
        "grade",
        "year",
        "duration",
        "paper_type",
        "paperType",
        "template_name",
        "templateName",
        "category",
        "paper_category",
        "paperCategory",
    }
)

UI_ROOT_FIELDS = frozenset(
    {
        "paper",
        "content",
        "items",
        "questions",
        "groups",
        "question_groups",
        "paper_category",
        "paperCategory",
        "category",
        "template_name",
        "templateName",
        "template_id",
        "platform_template_id",
        "platformTemplateId",
        "template_version",
        "platform_template_version",
        "platformTemplateVersion",
        "bundle_rule",
        "paper_bundle_rule",
    }
)

QUESTION_TEXT_PLACEHOLDERS = (
    "题干",
    "朗读",
    "小题",
)

PRIMARY_QUESTION_TEXT_PLACEHOLDERS = (
    "输入需要朗读的文章内容",
    "请输入题干",
)

DIRECT_QUESTION_TEXT_PLACEHOLDERS = (
    "请输入题干",
)

SECTION_TEXT_PLACEHOLDERS = (
    "章节名称",
    "节标题",
    "部分名称",
)

AUDIO_LABELS = (
    "题目指导文字音频",
    "小节指导文字音频",
    "原文音频",
    "题干音频",
    "音频",
)

ITEM_NUMBER_LABELS = {
    "times": ("原文播放次数(次)", "音频播放次数", "播放次数"),
    "gap": ("原文播放间隔(秒)", "题间间隔时间", "播放间隔"),
    "reply": ("答题时长(秒)", "学生作答时间", "作答时间"),
    "score": ("分数",),
}

QUESTION_TYPE_ALIASES = {
    "听后选择": "听后选择",
    "listening_choice": "听后选择",
    "listening-selection": "听后选择",
    "choice": "听后选择",
    "听后应答": "听后应答",
    "listening_response": "听后应答",
    "listening-response": "听后应答",
    "response": "听后应答",
    "模仿朗读": "模仿朗读",
    "imitation_reading": "模仿朗读",
    "imitation-reading": "模仿朗读",
    "听后记录并转述信息": "听后记录并转述信息",
    "听后记录": "听后记录并转述信息",
    "listening_record_retelling": "听后记录并转述信息",
    "listening-record-retelling": "听后记录并转述信息",
    "record_retelling": "听后记录并转述信息",
    "信息获取": "信息获取",
    "info_acquisition": "信息获取",
    "信息转述及询问": "信息转述及询问",
    "info_retelling": "信息转述及询问",
}

GROUP_FILL_ORDER = (
    "听后选择",
    "听后应答",
    "模仿朗读",
    "信息获取",
    "信息转述及询问",
    "听后记录并转述信息",
)

DEFAULT_OUTLINE_TARGETS = {
    "听后选择": ("第1题(选择题)", 0),
    "听后应答": ("第1题(录音题)", 0),
    "模仿朗读": ("第1题(录音题)", 0),
    "听后记录并转述信息": ("第1题(填空题)", 0),
    "信息获取": ("第1题(录音题)", 0),
    "信息转述及询问": ("第1题(录音题)", 0),
}

# Private aliases preserve the old module-level names for compatibility with
# existing integrations while new code can use the public names above.
_UI_PAPER_FIELDS = UI_PAPER_FIELDS
_UI_ROOT_FIELDS = UI_ROOT_FIELDS
_QUESTION_TEXT_PLACEHOLDERS = QUESTION_TEXT_PLACEHOLDERS
_PRIMARY_QUESTION_TEXT_PLACEHOLDERS = PRIMARY_QUESTION_TEXT_PLACEHOLDERS
_DIRECT_QUESTION_TEXT_PLACEHOLDERS = DIRECT_QUESTION_TEXT_PLACEHOLDERS
_SECTION_TEXT_PLACEHOLDERS = SECTION_TEXT_PLACEHOLDERS
_AUDIO_LABELS = AUDIO_LABELS
_ITEM_NUMBER_LABELS = ITEM_NUMBER_LABELS
_QUESTION_TYPE_ALIASES = QUESTION_TYPE_ALIASES
_GROUP_FILL_ORDER = GROUP_FILL_ORDER
_DEFAULT_OUTLINE_TARGETS = DEFAULT_OUTLINE_TARGETS
