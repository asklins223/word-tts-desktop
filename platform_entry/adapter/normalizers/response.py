"""Page-semantic normalizer for listening-response groups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from typing import Any

from ..errors import PlatformInputError
from ..normalization import (
    _first_present,
    _normalise_question,
    _require_nonempty_text,
)


def normalise_response_group(
    value: Mapping[str, Any],
    index: int,
    *,
    base_dir: Path | None,
) -> dict[str, Any]:
    group_name = f"groups[{index}]"
    group_audio = _first_present(
        value,
        ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
    )
    if isinstance(group_audio, Mapping):
        group_audio = _first_present(group_audio, ("path", "local_path"))
    group_text = _first_present(
        value,
        ("listening_text", "hearing_text", "original_text", "source_text"),
    )
    raw_questions = _first_present(value, ("questions", "items"))
    if raw_questions is None:
        if group_text is None:
            raise PlatformInputError(
                f"{group_name}（听后应答）必须提供 questions；不能只填写一项"
            )
        raw_questions = [value]
    if isinstance(raw_questions, (str, bytes)) or not isinstance(raw_questions, Sequence):
        raise PlatformInputError(f"{group_name}.questions 必须是数组")
    if not raw_questions:
        raise PlatformInputError(f"{group_name}.questions 不能为空")

    questions: list[dict[str, Any]] = []
    for question_index, raw_question in enumerate(raw_questions):
        question_name = f"{group_name}.questions[{question_index}]"
        if not isinstance(raw_question, Mapping):
            raise PlatformInputError(f"{question_name} 必须是对象")
        question = _normalise_question(
            raw_question,
            question_name,
            group_type="听后应答",
            base_dir=base_dir,
            fallback_prompt=group_text,
            fallback_audio=group_audio,
        )
        listening_fallback = question["prompt"]
        # The platform's listening-response section is rendered as a spoken
        # card.  Its two Word choices are not editable option rows there: the
        # choices themselves become the card stem, separated by exactly one
        # space, while the selected choice becomes the reference-answer row.
        answer_key = str(question.get("answer") or "").casefold()
        option_texts = [
            re.sub(r"\s+", " ", str(option.get("text") or "")).strip()
            for option in question.get("options", ())
        ]
        selected_texts = [
            text
            for option, text in zip(question.get("options", ()), option_texts)
            if str(option.get("option_id") or "").casefold() == answer_key
        ]
        if len(selected_texts) != 1:
            raise PlatformInputError(
                f"{question_name}.answer 无法唯一转换为听后应答参考答案"
            )
        response_prompt = " ".join(option_texts).strip()
        if not response_prompt:
            raise PlatformInputError(f"{question_name}.options 不能转换为空题干")
        question["prompt"] = response_prompt
        question["text"] = response_prompt
        # Word marks the correct response with ★.  The platform answer row
        # stores only the response text, without that marker.
        question["reference_answers"] = (
            re.sub(r"^\s*[★☆*]\s*", "", selected_texts[0]).strip(),
        )
        if not question["reference_answers"][0]:
            raise PlatformInputError(
                f"{question_name}.answer 对应的参考答案不能为空"
            )
        # Prompt and listening script are independent fields when both are
        # supplied; compact input can still use the same text for both.
        if "listening_text" not in question:
            question["listening_text"] = _require_nonempty_text(
                group_text if group_text is not None else listening_fallback,
                f"{question_name}.listening_text",
            )
        questions.append(question)
    return {"type": "听后应答", "questions": tuple(questions)}


__all__ = ["normalise_response_group"]
