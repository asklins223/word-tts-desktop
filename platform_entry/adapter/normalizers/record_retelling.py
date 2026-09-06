"""Page-semantic normalizer for listening-record and retelling groups."""

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
    _resolve_image_path,
)


def normalise_record_retelling_group(
    value: Mapping[str, Any],
    index: int,
    *,
    base_dir: Path | None,
) -> dict[str, Any]:
    group_name = f"groups[{index}]"
    recording_source = _first_present(
        value,
        ("recording", "recording_section", "listening_record"),
    )
    if recording_source is None:
        recording_source = value
    if not isinstance(recording_source, Mapping):
        raise PlatformInputError(f"{group_name}.recording 必须是对象")

    audio = _first_present(
        recording_source,
        ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
    )
    if audio is None:
        audio = _first_present(
            value,
            ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
        )
    if isinstance(audio, Mapping):
        audio = _first_present(audio, ("path", "local_path"))
    if audio is None:
        raise PlatformInputError(f"{group_name}.recording.audio_path 不能为空")
    listening_text = _first_present(
        recording_source,
        ("listening_text", "hearing_text", "original_text", "source_text", "text"),
    )
    if listening_text is None:
        raise PlatformInputError(f"{group_name}.recording.listening_text 不能为空")

    raw_blank_questions = _first_present(
        recording_source,
        ("questions", "items", "blank_questions"),
    )
    if raw_blank_questions is None:
        raw_blank_questions = _first_present(value, ("recording_questions",))
    if isinstance(raw_blank_questions, (str, bytes)) or not isinstance(raw_blank_questions, Sequence):
        raise PlatformInputError(f"{group_name}.recording.questions 必须是数组")
    if not raw_blank_questions:
        raise PlatformInputError(f"{group_name}.recording.questions 不能为空")

    blank_questions: list[dict[str, Any]] = []
    for question_index, raw_question in enumerate(raw_blank_questions):
        question_name = f"{group_name}.recording.questions[{question_index}]"
        if not isinstance(raw_question, Mapping):
            raise PlatformInputError(f"{question_name} 必须是对象")
        score = raw_question.get("score")
        if score is None:
            raise PlatformInputError(f"{question_name}.score 不能为空")
        answers = _first_present(
            raw_question,
            ("answers", "reference_answers", "answer", "correct_answer"),
        )
        blank_questions.append(
            {
                "score": _normalise_number(
                    score,
                    f"{question_name}.score",
                    minimum=0,
                ),
                "answers": tuple(
                    _normalise_string_list(answers, f"{question_name}.answers")
                ),
            }
        )

    image = _first_present(recording_source, ("image_path", "table_image_path", "image"))
    image_path = (
        _resolve_image_path(
            image,
            f"{group_name}.recording.image_path",
            base_dir,
        )
        if image is not None
        else None
    )
    instruction = _first_present(
        recording_source,
        ("instruction_text", "recording_instruction"),
    )
    recording: dict[str, Any] = {
        "audio_path": _resolve_audio_path(
            audio,
            f"{group_name}.recording.audio_path",
            base_dir,
        ),
        "listening_text": _require_nonempty_text(
            listening_text,
            f"{group_name}.recording.listening_text",
        ),
        "questions": tuple(blank_questions),
    }
    if image_path is not None:
        recording["image_path"] = image_path
    if instruction is not None:
        recording["instruction_text"] = _require_nonempty_text(
            instruction,
            f"{group_name}.recording.instruction_text",
        )
        occurrence = recording_source.get(
            "instruction_occurrence",
            recording_source.get("instruction_editor_index"),
        )
        if occurrence is not None:
            if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0:
                raise PlatformInputError(
                    f"{group_name}.recording.instruction_occurrence 必须是非负整数"
                )
            recording["instruction_occurrence"] = occurrence

    retelling_source = _first_present(value, ("retelling", "retelling_question"))
    if retelling_source is None:
        retelling_source = {}
    if not isinstance(retelling_source, Mapping):
        raise PlatformInputError(f"{group_name}.retelling 必须是对象")
    prompt = _first_present(retelling_source, ("prompt", "stem", "question", "text"))
    if prompt is None:
        raise PlatformInputError(f"{group_name}.retelling.prompt 不能为空")
    score = retelling_source.get("score")
    if score is None:
        raise PlatformInputError(f"{group_name}.retelling.score 不能为空")
    reference_answers = _first_present(
        retelling_source,
        ("reference_answers", "answers"),
    )
    retelling: dict[str, Any] = {
        "prompt": _require_nonempty_text(prompt, f"{group_name}.retelling.prompt"),
        "score": _normalise_number(
            score,
            f"{group_name}.retelling.score",
            minimum=0,
        ),
        "reference_answers": tuple(
            _normalise_string_list(
                reference_answers,
                f"{group_name}.retelling.reference_answers",
            )
        ),
    }
    answer_time = _first_present(
        retelling_source,
        ("answer_time", "reply", "answerDuration"),
    )
    if answer_time is not None:
        retelling["answer_time"] = _normalise_number(
            answer_time,
            f"{group_name}.retelling.answer_time",
            minimum=0,
        )
    return {
        "type": "听后记录并转述信息",
        "recording": recording,
        "retelling": retelling,
    }


__all__ = ["normalise_record_retelling_group"]
