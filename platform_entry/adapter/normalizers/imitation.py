"""Page-semantic normalizer for imitation-reading groups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import PlatformInputError
from ..normalization import (
    _first_present,
    _normalise_number,
    _normalise_string_list,
    _require_nonempty_text,
    _resolve_audio_path,
)


def normalise_imitation_group(
    value: Mapping[str, Any],
    index: int,
    *,
    base_dir: Path | None,
) -> dict[str, Any]:
    group_name = f"groups[{index}]"
    raw_questions = _first_present(value, ("questions", "items"))
    if raw_questions is None:
        raw_questions = [value]
    if isinstance(raw_questions, (str, bytes)) or not isinstance(raw_questions, Sequence):
        raise PlatformInputError(f"{group_name}.questions 必须是数组")
    if not raw_questions:
        raise PlatformInputError(f"{group_name}.questions 不能为空")

    group_audio = _first_present(
        value,
        ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
    )
    group_text = _first_present(
        value,
        ("listening_text", "hearing_text", "original_text", "source_text", "text"),
    )
    group_score = value.get("score")
    group_answers = _first_present(value, ("reference_answers", "answers"))
    questions: list[dict[str, Any]] = []
    for question_index, raw_question in enumerate(raw_questions):
        question_name = f"{group_name}.questions[{question_index}]"
        if not isinstance(raw_question, Mapping):
            raise PlatformInputError(f"{question_name} 必须是对象")
        listening_text = _first_present(
            raw_question,
            ("listening_text", "hearing_text", "original_text", "source_text", "text"),
        )
        if listening_text is None:
            listening_text = group_text
        listening_text = _require_nonempty_text(
            listening_text,
            f"{question_name}.listening_text",
        )
        audio = _first_present(
            raw_question,
            ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
        )
        if audio is None:
            audio = group_audio
        if isinstance(audio, Mapping):
            audio = _first_present(audio, ("path", "local_path"))
        if audio is None:
            raise PlatformInputError(f"{question_name}.audio_path 不能为空")
        score = raw_question.get("score", group_score)
        if score is None:
            raise PlatformInputError(f"{question_name}.score 不能为空")
        answers = _first_present(raw_question, ("reference_answers", "answers"))
        if answers is None:
            answers = group_answers
        questions.append(
            {
                "listening_text": listening_text,
                "audio_path": _resolve_audio_path(
                    audio,
                    f"{question_name}.audio_path",
                    base_dir,
                ),
                "score": _normalise_number(
                    score,
                    f"{question_name}.score",
                    minimum=0,
                ),
                "reference_answers": tuple(
                    _normalise_string_list(
                        answers,
                        f"{question_name}.reference_answers",
                        required=False,
                    )
                ),
            }
        )
    return {"type": "模仿朗读", "questions": tuple(questions)}


__all__ = ["normalise_imitation_group"]
