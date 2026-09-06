"""Pure page-flow orchestration and network-free dry-run planning."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .constants import PAPER_CONTENT_GET_PATH, PAPER_PAGE_PATH
from .errors import PlatformInputError
from .models import PlatformInputSpec

# Read-only verification wait windows. The list query is issued by the page
# itself; the observer needs one fresh PAPER_PAGE_PATH response after 查询
# before the matches list can be trusted.
VERIFY_LIST_RESPONSE_TIMEOUT_SECONDS = 30.0
VERIFY_LIST_SETTLE_MS = 800


def run_page_verify(
    automation: Any,
    *,
    admin_url: str,
    login_timeout: float,
) -> dict[str, Any]:
    """Verify platform paper records by title without any write action.

    The flow only opens the admin list, runs the visible search, and reads
    the read-only list responses the page already received. It never clicks
    新增/编辑/保存, so a verification run cannot create or modify a record.
    """

    steps: list[dict[str, Any]] = []

    def page_step(name: str, action: Any) -> None:
        action()
        steps.append({"name": name, "mode": "PAGE_UI"})

    observer = automation.observer
    page_step(
        "open_and_wait_login",
        lambda: automation.wait_until_ready(admin_url, login_timeout),
    )
    baseline_responses = observer.list_response_count
    page_step("search_paper_list", automation.search_paper_list)
    automation._wait_until(
        lambda: observer.list_response_count > baseline_responses,
        "只读核验没有等到试卷列表查询结果",
        timeout_seconds=VERIFY_LIST_RESPONSE_TIMEOUT_SECONDS,
        interval_ms=200,
    )
    # Give a possible second page (pagination refresh) a short settle window
    # before reading the collected matches.
    automation.page.wait_for_timeout(VERIFY_LIST_SETTLE_MS)
    matches = [dict(row) for row in observer.paper_matches]
    if len(matches) > 1:
        status = "MULTIPLE_MATCHES"
    elif len(matches) == 1:
        status = "FOUND"
    else:
        status = "NOT_FOUND"
    return {
        "status": status,
        "side_effect_policy": "PAGE_UI_READ_ONLY",
        "feedback_policy": "READ_ONLY_RESPONSE_OBSERVATION",
        "paper_title": automation.spec.paper["title"],
        "matches": matches,
        "readOnlyRequests": list(observer.read_only_requests),
        "steps": steps,
    }

def run_page_input(
    automation: PlatformInputPageAutomation,
    *,
    admin_url: str,
    login_timeout: float,
    return_to_list: bool = True,
    edit_existing: bool = False,
    existing_paper_id: str | None = None,
    control_check: Callable[[], None] | None = None,
    resume_phase: str | None = None,
) -> dict[str, Any]:
    """按页面动作顺序执行一次录入；函数本身不接收或构造 API 客户端。

    ``edit_existing`` 用于重试场景：只从列表打开同名的可编辑试卷，找不到
    时直接报错，不回退到新增流程。``control_check`` 在每个页面动作前后
    执行；它可以在动作边界暂停/停止当前运行，但不会强行中断已经提交给
    页面或浏览器的原子动作。
    """

    steps: list[dict[str, Any]] = []

    # The same callback is also consumed by the shared navigation/form wait
    # loops. Page actions remain atomic, but a lazy-loaded template list or
    # editor must not hide a stop request for the full timeout window.
    if control_check is not None:
        automation._control_check = control_check

    def page_step(name: str, action: Any) -> None:
        if control_check is not None:
            control_check()
        action()
        if control_check is not None:
            control_check()
        steps.append({"name": name, "mode": "PAGE_UI"})

    try:
        if resume_phase in {
            "existing_content_ready",
            "new_paper_template_selected",
        }:
            # The read-only preflight intentionally leaves the shared browser
            # on the exact page needed by the real run. Navigating to the list
            # again here would discard that page and make the executor wait
            # forever for list-only controls.
            page_step(
                "reuse_preflight_page",
                lambda: automation.wait_until_preflight_phase(resume_phase),
            )
        else:
            page_step(
                "open_and_wait_login",
                lambda: automation.wait_until_ready(admin_url, login_timeout),
            )
        if resume_phase == "new_paper_template_selected":
            # The read-only preflight already opened the new-paper form,
            # filled its base fields, and selected the template.  The next
            # button is the first action after the durable external-write
            # boundary, so continue from that exact visible page.
            page_step("click_next_to_content", automation.next_to_content)
        elif resume_phase == "existing_content_ready":
            # The preflight already opened the existing editable paper and
            # reached the content step.  Do not reopen it or create a second
            # navigation attempt.
            pass
        elif edit_existing:
            phase: str = "base"

            def open_existing() -> None:
                nonlocal phase
                if existing_paper_id and automation.existing_paper_id != existing_paper_id:
                    automation.existing_paper_id = str(existing_paper_id).strip()
                phase = automation.start_existing_paper()

            page_step("open_existing_paper", open_existing)
            if phase == "base":
                page_step("fill_base_form", automation.fill_base_form)
                # 编辑页会保留原试卷已经绑定的模板，但不一定重新渲染
                # 模板卡片；不要把“编辑既有试卷”误当成重新选模板。
                page_step("click_next_to_content", automation.next_to_content)
        else:
            page_step("click_new_paper", automation.start_new_paper)
            page_step("fill_base_form", automation.fill_base_form)
            page_step("select_template", automation.select_template)
            page_step("click_next_to_content", automation.next_to_content)
        page_step("fill_content", automation.fill_content)
        page_step("click_save_content", automation.save_content)
        automation.observer.begin_saved_feedback_capture()
        if return_to_list:
            page_step("click_close_to_read_status", automation.return_to_list)
        feedback = automation.feedback()
    except PlatformInputError as exc:
        exc.steps = steps
        exc.feedback = automation.feedback()
        raise

    status = "SUCCEEDED" if feedback.get("paperId") is not None else "SAVED_NEEDS_REVIEW"
    return {
        "status": status,
        "side_effect_policy": "PAGE_UI_ONLY",
        "feedback_policy": "READ_ONLY_RESPONSE_OBSERVATION",
        "paper_category": automation.spec.paper_category,
        "paper_title": automation.spec.paper["title"],
        "questionCount": automation.spec.question_count,
        "paperId": feedback.get("paperId"),
        "feedback": feedback,
        "preview": "SKIPPED",
        "steps": steps,
    }

def build_dry_run_result(
    spec: PlatformInputSpec,
    *,
    edit_existing: bool = False,
) -> dict[str, Any]:
    """输出页面动作计划，不打开浏览器、不发任何网络请求。"""

    audio_files: list[str] = []
    image_files: list[str] = []
    if spec.groups:
        for group in spec.groups:
            if group["type"] == "听后选择":
                for material in group["materials"]:
                    audio_files.append(material["audio_path"])
            elif group["type"] in {"听后应答", "模仿朗读"}:
                for question in group["questions"]:
                    audio_files.append(question["audio_path"])
            elif group["type"] == "听后记录并转述信息":
                recording = group["recording"]
                audio_files.append(recording["audio_path"])
                if recording.get("image_path"):
                    image_files.append(recording["image_path"])
    else:
        audio_files = [
            item["audio_path"]
            for item in spec.items
            if item.get("audio_path")
        ]

    steps = [
        {"name": "open_and_wait_login", "mode": "PAGE_UI"},
    ]
    if edit_existing:
        steps.append({"name": "open_existing_paper", "mode": "PAGE_UI"})
        steps.extend(
            [
                {"name": "fill_base_form", "mode": "PAGE_UI"},
                {"name": "click_next_to_content", "mode": "PAGE_UI"},
            ]
        )
    else:
        steps.append({"name": "click_new_paper", "mode": "PAGE_UI"})
        steps.extend(
            [
                {"name": "fill_base_form", "mode": "PAGE_UI"},
                {"name": "select_template", "mode": "PAGE_UI"},
                {"name": "click_next_to_content", "mode": "PAGE_UI"},
            ]
        )
    steps.extend(
        [
            {"name": "fill_content", "mode": "PAGE_UI"},
            {"name": "click_save_content", "mode": "PAGE_UI"},
            {"name": "click_close_to_read_status", "mode": "PAGE_UI"},
        ]
    )

    result = {
        "status": "DRY_RUN",
        "side_effect_policy": "NO_NETWORK",
        "feedback_policy": "READ_ONLY_RESPONSE_OBSERVATION",
        "paper_category": spec.paper_category,
        "paper_title": spec.paper["title"],
        "questionCount": spec.question_count,
        "preview": "SKIPPED",
        "steps": steps,
        "read_only_feedback_paths": [
            PAPER_PAGE_PATH,
            PAPER_CONTENT_GET_PATH,
        ],
        "audio_files": audio_files,
    }
    if spec.groups:
        result["questionGroups"] = [
            {
                "type": group["type"],
                "questionCount": PlatformInputSpec(
                    paper={},
                    items=(),
                    paper_category=spec.paper_category,
                    template_name=spec.template_name,
                    groups=(group,),
                ).question_count,
            }
            for group in spec.groups
        ]
        if image_files:
            result["image_files"] = image_files
    return result
