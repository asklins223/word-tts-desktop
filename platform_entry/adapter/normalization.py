"""Validate page-semantic JSON before any browser is opened.

This module owns shared field validation plus the reviewed family normalizers.
Their page actions and card counters are registered through ``handlers.py``;
the runner and browser lifecycle do not need to know each family field layout.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import normalise_text
from .constants import (
    _DIRECT_QUESTION_TEXT_PLACEHOLDERS,
    _QUESTION_TYPE_ALIASES,
    _UI_PAPER_FIELDS,
    _UI_ROOT_FIELDS,
)
from .errors import PlatformInputError
from .group_counts import (
    count_info_acquisition_group,
    count_info_retelling_group,
    count_imitation_group,
    count_record_retelling_group,
    count_response_group,
    count_selection_group,
)
from .handlers import (
    QuestionTypeHandler,
    get_question_type_handler,
    register_aliases_from_mapping,
    register_question_type,
)
from .models import PlatformInputSpec
from .rules import resolve_paper_bundle_rule

_normalise_text = normalise_text

def _require_nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformInputError(f"{name} 必须是非空字符串")
    return value.strip()

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def _normalise_number(
    value: Any,
    name: str,
    *,
    minimum: float = 0,
    integer: bool = False,
) -> int | float:
    if not _is_number(value):
        raise PlatformInputError(f"{name} 必须是数字")
    if isinstance(value, float) and not math.isfinite(value):
        raise PlatformInputError(f"{name} 必须是有限数字")
    if value < minimum:
        raise PlatformInputError(f"{name} 必须大于等于 {minimum}")
    if integer and not isinstance(value, int):
        raise PlatformInputError(f"{name} 必须是整数")
    return value

def _normalise_choice(value: Any, name: str) -> dict[str, Any]:
    """把 name/label 形式的页面选项规范化，不把选择转换成 API 写载荷。"""

    identifier: Any | None = None
    if isinstance(value, Mapping):
        identifier = value.get("id", value.get("value"))
        label = value.get("name", value.get("label", value.get("text")))
        if label is None and identifier is not None:
            raise PlatformInputError(
                f"{name} 需要提供 name 或 label；页面选择不能只依赖 API ID"
            )
    else:
        label = value

    label = _require_nonempty_text(label, f"{name}.name")
    result = {"name": label}
    if identifier is not None:
        result["id"] = identifier
    return result

def _normalise_choice_list(
    value: Any,
    name: str,
    *,
    required: bool = True,
) -> list[dict[str, Any]]:
    if value in (None, "") and not required:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PlatformInputError(f"{name} 必须是页面选项数组")
    if not value and required:
        raise PlatformInputError(f"{name} 不能为空；页面表单要求选择至少一个区/县")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        choice = _normalise_choice(item, f"{name}[{index}]")
        key = _normalise_text(choice["name"]).casefold()
        if key in seen:
            raise PlatformInputError(
                f"{name} 存在重复页面选项：{choice['name']}"
            )
        seen.add(key)
        result.append(choice)
    return result

def _resolve_category(raw: Mapping[str, Any], paper: Mapping[str, Any]) -> str:
    candidates = (
        raw.get("paper_category"),
        raw.get("paperCategory"),
        raw.get("category"),
        paper.get("paper_category"),
        paper.get("paperCategory"),
        paper.get("category"),
    )
    category = next((candidate for candidate in candidates if candidate is not None), None)
    if category is None:
        category = "题型专项"
    category = _require_nonempty_text(category, "paper_category")
    if category not in {"题型专项", "听说考试"}:
        raise PlatformInputError("paper_category 目前只支持“题型专项”或“听说考试”")
    return category

def _resolve_audio_path(value: Any, name: str, base_dir: Path | None) -> str:
    path = _resolve_local_asset_path(
        value,
        name,
        base_dir,
        description="本地音频文件路径",
    )
    if Path(path).suffix.lower() not in {".mp3", ".wav"}:
        raise PlatformInputError(
            f"{name} 必须是 MP3/WAV 音频文件: {path}"
        )
    return path

def _resolve_local_asset_path(
    value: Any,
    name: str,
    base_dir: Path | None,
    *,
    description: str,
) -> str:
    """解析页面上传控件使用的本地文件，不接受远程 URL。"""

    path_text = _require_nonempty_text(value, name)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path_text):
        raise PlatformInputError(
            f"{name} 必须是{description}；脚本不会把远程 URL 直接写入页面"
        )
    path = Path(os.path.expanduser(path_text))
    if not path.is_absolute():
        path = (base_dir or Path.cwd()) / path
    if path.is_symlink():
        raise PlatformInputError(
            f"{name} 不能是符号链接；请提供受控的本地文件路径"
        )
    path = path.resolve()
    if not path.is_file():
        raise PlatformInputError(f"{name} 文件不存在: {path}")
    return str(path)

def _resolve_image_path(value: Any, name: str, base_dir: Path | None) -> str:
    path = _resolve_local_asset_path(
        value,
        name,
        base_dir,
        description="本地 JPG/PNG 图片文件路径",
    )
    if Path(path).suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise PlatformInputError(
            f"{name} 必须是 JPG/PNG 图片文件: {path}"
        )
    return path

def _first_present(source: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None

def _legacy_payload_paths(value: Any, path: str = "root"):
    """Yield locations of the old API-ready page fields, wherever nested."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text in {"pages", "jsonContent", "json_content"}:
                yield child_path
            yield from _legacy_payload_paths(child, child_path)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            yield from _legacy_payload_paths(child, f"{path}[{index}]")

def _normalise_question_type(value: Any, name: str) -> str:
    text = _require_nonempty_text(value, name)
    handler = get_question_type_handler(text)
    if handler is not None:
        return handler.canonical_type
    raise PlatformInputError(
        f"{name} 不支持：{text!r}；可选题型为“听后选择”“听后应答”“模仿朗读”"
        "、“信息获取”“信息转述及询问”或“听后记录并转述信息”"
    )

def _normalise_string_list(
    value: Any,
    name: str,
    *,
    required: bool = True,
) -> list[str]:
    if value in (None, ""):
        if required:
            raise PlatformInputError(f"{name} 不能为空")
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = list(value)
    else:
        raise PlatformInputError(f"{name} 必须是字符串或字符串数组")
    result = []
    for index, item in enumerate(values):
        result.append(_require_nonempty_text(item, f"{name}[{index}]"))
    if required and not result:
        raise PlatformInputError(f"{name} 不能为空")
    return result

def _normalise_options(value: Any, name: str) -> list[dict[str, str]]:
    """规范化页面选项，保留稳定的 A/B/C... option_id。"""

    if isinstance(value, Mapping):
        raw_options = [
            {"option_id": option_id, "text": option_text}
            for option_id, option_text in value.items()
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw_options = list(value)
    else:
        raise PlatformInputError(f"{name} 必须是选项数组或 option_id 到文本的对象")

    if len(raw_options) < 2:
        raise PlatformInputError(f"{name} 至少需要两个选项，不能只录入一个选项")

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_option in enumerate(raw_options):
        if isinstance(raw_option, Mapping):
            option_id = _first_present(
                raw_option,
                ("option_id", "id", "key", "label", "letter"),
            )
            text = _first_present(
                raw_option,
                ("text", "content", "value", "name"),
            )
        else:
            option_id = None
            text = raw_option
        if option_id is None:
            option_id = chr(ord("A") + index)
        option_id = _require_nonempty_text(option_id, f"{name}[{index}].option_id")
        text = _require_nonempty_text(text, f"{name}[{index}].text")
        option_key = _normalise_text(option_id).casefold()
        if option_key in seen:
            raise PlatformInputError(f"{name} 存在重复 option_id：{option_id}")
        seen.add(option_key)
        options.append({"option_id": option_id, "text": text})
    return options

def _normalise_answer(value: Any, options: Sequence[Mapping[str, Any]], name: str) -> str:
    if isinstance(value, Mapping):
        value = _first_present(value, ("option_id", "id", "key", "label", "value"))
    answer = _require_nonempty_text(value, name)
    for option in options:
        option_id = str(option.get("option_id", ""))
        option_text = str(option.get("text", ""))
        if answer == option_id or answer.casefold() == option_id.casefold():
            return option_id
        if answer == option_text:
            return option_id
    raise PlatformInputError(
        f"{name}={answer!r} 不在选项 option_id 中："
        + ", ".join(str(option["option_id"]) for option in options)
    )

def _normalise_common_numbers(
    source: Mapping[str, Any],
    name: str,
    *,
    keys: Mapping[str, Sequence[str]],
) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for canonical, aliases in keys.items():
        candidate = _first_present(source, aliases)
        if candidate is None:
            continue
        result[canonical] = _normalise_number(
            candidate,
            f"{name}.{canonical}",
            minimum=0,
        )
    return result

def _normalise_question(
    value: Any,
    name: str,
    *,
    group_type: str,
    base_dir: Path | None,
    fallback_prompt: Any = None,
    fallback_audio: Any = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlatformInputError(f"{name} 必须是对象")

    prompt_keys = ("prompt", "stem", "question", "text")
    if group_type == "听后应答":
        # The response page can use its listening sentence as the visible
        # question prompt when the compact input has no separate prompt. A
        # selection/reading page must not silently turn the listening script
        # into a question stem.
        prompt_keys += (
            "listening_text",
            "hearing_text",
            "original_text",
        )
    prompt = _first_present(value, prompt_keys)
    if prompt is None:
        prompt = fallback_prompt
    prompt = _require_nonempty_text(prompt, f"{name}.prompt")

    result: dict[str, Any] = {
        "prompt": prompt,
        "text": prompt,
    }
    listening_text = _first_present(
        value,
        ("listening_text", "hearing_text", "original_text", "source_text"),
    )
    if listening_text is not None:
        result["listening_text"] = _require_nonempty_text(
            listening_text,
            f"{name}.listening_text",
        )

    audio = _first_present(
        value,
        ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
    )
    if audio is None:
        audio = fallback_audio
    if audio is not None:
        if isinstance(audio, Mapping):
            audio = _first_present(audio, ("path", "local_path"))
        result["audio_path"] = _resolve_audio_path(audio, f"{name}.audio_path", base_dir)

    result.update(
        _normalise_common_numbers(
            value,
            name,
            keys={
                "score": ("score",),
                "answer_time": ("answer_time", "reply", "answerDuration"),
                "times": ("times", "play_count"),
                "gap": ("gap", "interval"),
            },
        )
    )

    raw_options = _first_present(value, ("options", "choices"))
    if group_type in {"听后选择", "听后应答"}:
        if raw_options is None:
            raise PlatformInputError(f"{name}.options 不能为空；页面小题必须完整录入选项")
        options = _normalise_options(raw_options, f"{name}.options")
        result["options"] = options
        raw_answer = _first_present(
            value,
            ("answer", "correct_answer", "correct", "right_answer"),
        )
        result["answer"] = _normalise_answer(
            raw_answer,
            options,
            f"{name}.answer",
        )
        if result.get("score") is None:
            raise PlatformInputError(f"{name}.score 不能为空；页面小题分数是必填项")

    answer_prompt = _first_present(value, ("answer_prompt", "response_prompt"))
    if answer_prompt is not None:
        result["answer_prompt"] = _require_nonempty_text(
            answer_prompt,
            f"{name}.answer_prompt",
        )

    return result

from .normalizers.imitation import normalise_imitation_group as _normalise_imitation_group
from .normalizers.legacy_exam import (
    normalise_info_acquisition_group as _normalise_info_acquisition_group,
    normalise_info_retelling_group as _normalise_info_retelling_group,
)
from .normalizers.record_retelling import (
    normalise_record_retelling_group as _normalise_record_group,
)
from .normalizers.response import normalise_response_group as _normalise_response_group
from .normalizers.selection import normalise_selection_group as _normalise_selection_group


def _register_builtin_question_types() -> None:
    """Register the reviewed families once when the normalization module loads."""

    builtins = (
        QuestionTypeHandler(
            canonical_type="听后选择",
            aliases=(),
            card_kind="选择题",
            normalizer=_normalise_selection_group,
            page_method="_fill_selection_groups",
            card_counter=count_selection_group,
        ),
        QuestionTypeHandler(
            canonical_type="听后应答",
            aliases=(),
            card_kind="选择题",
            normalizer=_normalise_response_group,
            page_method="_fill_response_groups",
            card_counter=count_response_group,
        ),
        QuestionTypeHandler(
            canonical_type="模仿朗读",
            aliases=(),
            card_kind="录音题",
            normalizer=_normalise_imitation_group,
            page_method="_fill_imitation_groups",
            card_counter=count_imitation_group,
        ),
        QuestionTypeHandler(
            canonical_type="信息获取",
            aliases=(),
            card_kind="录音题",
            normalizer=_normalise_info_acquisition_group,
            page_method="_fill_info_acquisition_groups",
            card_counter=count_info_acquisition_group,
        ),
        QuestionTypeHandler(
            canonical_type="信息转述及询问",
            aliases=(),
            card_kind="录音题",
            normalizer=_normalise_info_retelling_group,
            page_method="_fill_info_retelling_groups",
            card_counter=count_info_retelling_group,
        ),
        QuestionTypeHandler(
            canonical_type="听后记录并转述信息",
            aliases=(),
            card_kind="填空题",
            normalizer=_normalise_record_group,
            page_method="_fill_record_retelling_groups",
            card_counter=count_record_retelling_group,
        ),
    )
    for handler in builtins:
        if get_question_type_handler(handler.canonical_type) is None:
            register_question_type(handler)
    register_aliases_from_mapping(_QUESTION_TYPE_ALIASES)


_register_builtin_question_types()

def _normalise_group(
    value: Any,
    index: int,
    *,
    base_dir: Path | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlatformInputError(f"groups[{index}] 必须是对象")
    group_type = _normalise_question_type(
        _first_present(value, ("type", "question_type", "questionType", "category")),
        f"groups[{index}].type",
    )
    handler = get_question_type_handler(group_type)
    if handler is None:
        # This is an internal consistency failure, not user input.  Keeping it
        # explicit prevents an unregistered family from silently falling into
        # the last handler and receiving the wrong field semantics.
        raise PlatformInputError(f"题型处理器未注册：{group_type}")
    return handler.normalizer(value, index, base_dir=base_dir)

def _normalise_item(
    value: Any,
    index: int,
    *,
    base_dir: Path | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlatformInputError(f"items[{index}] 必须是对象")

    listening_text = next(
        (
            value.get(key)
            for key in ("listening_text", "hearing_text", "original_text")
            if key in value
        ),
        None,
    )
    if listening_text is None:
        # 保留旧输入文件的兼容别名；模仿朗读页面会把它写入“听力原文”，
        # 不会再误写“题干”。新配置请使用 listening_text，语义更明确。
        listening_text = value.get(
            "text",
            value.get("tts_text", value.get("content")),
        )
    listening_text = _require_nonempty_text(
        listening_text,
        f"items[{index}].listening_text",
    )

    # text/audio_path 保留为内部兼容别名；模仿朗读专用流程不会使用它们
    # 去填写题干或题干音频。
    item: dict[str, Any] = {
        "listening_text": listening_text,
        "text": listening_text,
    }
    audio = next(
        (
            value.get(key)
            for key in (
                "original_audio_path",
                "listening_audio_path",
                "audio_path",
                "audioFile",
                "audio",
            )
            if key in value
        ),
        None,
    )
    if audio is not None:
        if isinstance(audio, Mapping):
            audio = audio.get("path", audio.get("local_path"))
        audio_path = _resolve_audio_path(
            audio,
            f"items[{index}].original_audio_path",
            base_dir,
        )
        item["original_audio_path"] = audio_path
        item["audio_path"] = audio_path

    for key, aliases in {
        "times": ("times", "play_count"),
        "gap": ("gap", "interval"),
        "reply": ("reply", "answer_time", "answerDuration"),
        "score": ("score",),
    }.items():
        candidate = next((value.get(alias) for alias in aliases if alias in value), None)
        if candidate is not None:
            item[key] = _normalise_number(
                candidate,
                f"items[{index}].{key}",
                minimum=0,
            )

    reference_answers = _first_present(value, ("reference_answers", "answers"))
    if reference_answers is not None:
        item["reference_answers"] = tuple(
            _normalise_string_list(
                reference_answers,
                f"items[{index}].reference_answers",
            )
        )

    editor_index = value.get("editor_index")
    if editor_index is not None:
        if isinstance(editor_index, bool) or not isinstance(editor_index, int):
            raise PlatformInputError(f"items[{index}].editor_index 必须是整数")
        if editor_index < 0:
            raise PlatformInputError(f"items[{index}].editor_index 不能小于 0")
        item["editor_index"] = editor_index
    return item

def normalize_spec(
    raw: Mapping[str, Any],
    *,
    base_dir: Path | None = None,
    rules: Sequence[Any] | None = None,
) -> PlatformInputSpec:
    """校验页面语义输入，拒绝旧版 API-ready 载荷。"""

    if not isinstance(raw, Mapping):
        raise PlatformInputError("输入根对象必须是 JSON 对象")
    unknown_root_fields = sorted(set(raw) - _UI_ROOT_FIELDS)
    if unknown_root_fields:
        raise PlatformInputError(
            "输入包含非页面字段或旧版 API 字段: "
            + ", ".join(str(field) for field in unknown_root_fields)
        )
    legacy_payload_locations = list(_legacy_payload_paths(raw))
    if legacy_payload_locations:
        raise PlatformInputError(
            "页面驱动模式不接受 pages/jsonContent；内容必须通过第二步页面录入"
        )
    raw_paper = raw.get("paper")
    if not isinstance(raw_paper, Mapping):
        raise PlatformInputError("输入必须包含对象字段 paper")

    paper_source = dict(raw_paper)
    unknown_fields = sorted(set(paper_source) - _UI_PAPER_FIELDS)
    if unknown_fields:
        raise PlatformInputError(
            "paper 包含非页面字段或旧版 API 字段: " + ", ".join(unknown_fields)
        )
    content = raw.get("content")
    content_mapping = content if isinstance(content, Mapping) else {}

    category = _resolve_category(raw, paper_source)
    title = _require_nonempty_text(paper_source.get("title"), "paper.title")
    province = _normalise_choice(paper_source.get("province"), "paper.province")
    city = _normalise_choice(paper_source.get("city"), "paper.city")
    districts = _normalise_choice_list(
        paper_source.get("districts"),
        "paper.districts",
        required=False,
    )
    stage = _normalise_choice(paper_source.get("stage"), "paper.stage")
    grade = _normalise_choice(paper_source.get("grade"), "paper.grade")

    year = paper_source.get("year")
    if year is not None:
        year = _normalise_number(year, "paper.year", minimum=2000, integer=True)
        if year > 2100:
            raise PlatformInputError("paper.year 必须不大于 2100")
    duration = paper_source.get("duration")
    if duration is not None:
        duration = _normalise_number(
            duration,
            "paper.duration",
            minimum=1,
            integer=True,
        )
        if duration > 60:
            raise PlatformInputError("paper.duration 必须不大于 60 分钟")

    paper_type_source = paper_source.get("paper_type", paper_source.get("paperType"))
    paper_type = None
    if category == "听说考试":
        if paper_type_source is None:
            raise PlatformInputError(
                "paper_category=听说考试 时必须提供 paper.paper_type，脚本会通过页面下拉框选择"
            )
        paper_type = _normalise_choice(paper_type_source, "paper.paper_type")

    template_source = (
        raw.get("template_name")
        or raw.get("templateName")
        or paper_source.get("template_name")
        or paper_source.get("templateName")
    )
    template_name = _require_nonempty_text(template_source, "template_name")
    template_id_source = _first_present(
        raw,
        ("template_id", "platform_template_id", "platformTemplateId"),
    )
    template_id = (
        _require_nonempty_text(str(template_id_source), "template_id")
        if template_id_source is not None
        else None
    )
    template_version_source = _first_present(
        raw,
        ("template_version", "platform_template_version", "platformTemplateVersion"),
    )
    template_version = (
        _require_nonempty_text(str(template_version_source), "template_version")
        if template_version_source is not None
        else None
    )
    bundle_rule = resolve_paper_bundle_rule(
        template_name,
        paper_name=title,
        template_id=template_id,
        explicit_key=_first_present(raw, ("bundle_rule", "paper_bundle_rule")),
        rules=rules,
    )

    raw_groups = _first_present(
        raw,
        ("question_groups", "groups"),
    )
    if raw_groups is None:
        raw_groups = _first_present(
            content_mapping,
            ("question_groups", "groups"),
        )

    groups: tuple[dict[str, Any], ...] = ()
    if raw_groups is not None:
        if isinstance(raw_groups, (str, bytes)) or not isinstance(raw_groups, Sequence):
            raise PlatformInputError("question_groups/groups 必须是数组")
        if not raw_groups:
            raise PlatformInputError("question_groups/groups 不能为空")
        groups = tuple(
            _normalise_group(group, index, base_dir=base_dir)
            for index, group in enumerate(raw_groups)
        )
        items: tuple[dict[str, Any], ...] = ()
    else:
        raw_items = raw.get("items")
        if raw_items is None:
            raw_items = content_mapping.get("items")
        if raw_items is None:
            raw_items = raw.get("questions")
        if isinstance(raw_items, (str, bytes)) or not isinstance(raw_items, Sequence):
            raise PlatformInputError(
                "输入必须包含 items 数组或 question_groups 数组；脚本不会通过接口拼造平台 pages"
            )
        if not raw_items:
            raise PlatformInputError("items 不能为空")

        # 也接受 items 中直接放题型组的写法，但一旦检测到题型组，就仍
        # 走完整的组/材料/小题校验，而不是退回旧的一个条目一个编辑器。
        if all(
            isinstance(item, Mapping)
            and _first_present(item, ("type", "question_type", "questionType"))
            is not None
            for item in raw_items
        ):
            groups = tuple(
                _normalise_group(item, index, base_dir=base_dir)
                for index, item in enumerate(raw_items)
            )
            items = ()
        else:
            items = tuple(
                _normalise_item(item, index, base_dir=base_dir)
                for index, item in enumerate(raw_items)
            )

    if bundle_rule is not None and not groups:
        raise PlatformInputError(
            "已注册套卷模板必须使用 question_groups/groups，不能用旧版扁平 items 录入"
        )

    paper: dict[str, Any] = {
        "title": title,
        "province": province,
        "city": city,
        "districts": districts,
        "stage": stage,
        "grade": grade,
        "year": year,
        "duration": duration,
        "paper_type": paper_type,
    }
    return PlatformInputSpec(
        paper=paper,
        items=items,
        paper_category=category,
        template_name=template_name,
        groups=groups,
        template_id=template_id,
        template_version=template_version,
        bundle_rule=bundle_rule.key if bundle_rule is not None else None,
    )
