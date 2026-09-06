"""Normalizers for the reviewed 外研旧版套卷 layout."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from typing import Any

from ..errors import PlatformInputError
from ..normalization import (
    _first_present,
    _normalise_number,
    _normalise_options,
    _normalise_string_list,
    _require_nonempty_text,
    _resolve_audio_path,
    _resolve_image_path,
)


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise PlatformInputError(f"{name} 必须是数组")
    if not value:
        raise PlatformInputError(f"{name} 不能为空")
    return value


def _question_prompt(raw: Mapping[str, Any], name: str) -> tuple[str, tuple[str, ...]]:
    stem = _require_nonempty_text(
        _first_present(raw, ("prompt", "stem", "question", "text")),
        f"{name}.prompt",
    )
    raw_options = _first_present(raw, ("options", "choices"))
    if raw_options is None:
        return stem, ()
    options = _normalise_options(raw_options, f"{name}.options")
    option_texts = tuple(
        re.sub(r"\s+", " ", str(option.get("text") or "")).strip()
        for option in options
    )
    # The legacy editor stores the question and its choices in one rich-text
    # stem.  Keep the choices in their source order and use the same compact
    # slash-separated form as the worksheet: ``Question? (Four. / Five. / Six.)``.
    suffix = f"({' / '.join(option_texts)})" if option_texts else ""
    prompt = stem if suffix and stem.endswith(suffix) else f"{stem} {suffix}".strip()
    return prompt, option_texts


def _reference_answers(
    raw: Mapping[str, Any],
    name: str,
    *,
    option_texts: Sequence[str] = (),
) -> tuple[str, ...]:
    references = _first_present(raw, ("reference_answers", "answers"))
    if references is not None:
        return tuple(_normalise_string_list(references, f"{name}.reference_answers"))
    answer = _first_present(raw, ("answer", "correct_answer", "correct"))
    if answer is not None and option_texts:
        answer_text = str(answer).strip()
        if answer_text.isdigit():
            index = int(answer_text) - 1
            if 0 <= index < len(option_texts):
                return (option_texts[index],)
        for index, option in enumerate(option_texts):
            if answer_text.casefold() in {
                chr(ord("A") + index).casefold(),
                str(index + 1),
                option.casefold(),
            }:
                return (option,)
    raise PlatformInputError(f"{name}.reference_answers 不能为空")


def _info_acquisition_section(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    section = _require_nonempty_text(value, name)
    compact = re.sub(r"\s+", "", section)
    if "听选信息" in compact:
        return "听选信息"
    if "回答问题" in compact:
        return "回答问题"
    return section


def normalise_info_acquisition_group(
    value: Mapping[str, Any],
    index: int,
    *,
    base_dir: Path | None,
) -> dict[str, Any]:
    group_name = f"groups[{index}]"
    materials = _sequence(
        _first_present(value, ("materials", "recordings", "passages")),
        f"{group_name}.materials",
    )
    normalized = []
    for material_index, raw_material in enumerate(materials):
        name = f"{group_name}.materials[{material_index}]"
        if not isinstance(raw_material, Mapping):
            raise PlatformInputError(f"{name} 必须是对象")
        audio = _first_present(
            raw_material,
            ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
        )
        if isinstance(audio, Mapping):
            audio = _first_present(audio, ("path", "local_path"))
        if audio is None:
            raise PlatformInputError(f"{name}.audio_path 不能为空")
        listening_text = _require_nonempty_text(
            _first_present(raw_material, ("listening_text", "hearing_text", "text")),
            f"{name}.listening_text",
        )
        questions = _sequence(
            _first_present(raw_material, ("questions", "items")),
            f"{name}.questions",
        )
        normalized_questions = []
        for question_index, raw_question in enumerate(questions):
            question_name = f"{name}.questions[{question_index}]"
            if not isinstance(raw_question, Mapping):
                raise PlatformInputError(f"{question_name} 必须是对象")
            prompt, option_texts = _question_prompt(raw_question, question_name)
            score = raw_question.get("score")
            if score is None:
                raise PlatformInputError(f"{question_name}.score 不能为空")
            normalized_question = {
                "prompt": prompt,
                "text": prompt,
                "score": _normalise_number(score, f"{question_name}.score", minimum=0),
                "reference_answers": _reference_answers(
                    raw_question,
                    question_name,
                    option_texts=option_texts,
                ),
            }
            prompt_audio = raw_question.get("prompt_audio_path")
            if prompt_audio is not None:
                normalized_question["prompt_audio_path"] = _resolve_audio_path(
                    prompt_audio,
                    f"{question_name}.prompt_audio_path",
                    base_dir,
                )
            normalized_questions.append(normalized_question)
        normalized_material = {
            "audio_path": _resolve_audio_path(audio, f"{name}.audio_path", base_dir),
            "listening_text": listening_text,
            "questions": tuple(normalized_questions),
        }
        section = _info_acquisition_section(
            _first_present(raw_material, ("section", "subsection")),
            f"{name}.section",
        )
        if section:
            normalized_material["section"] = section
        normalized.append(normalized_material)
    return {"type": "信息获取", "materials": tuple(normalized)}


def _normalise_spoken_question(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    prompt = _require_nonempty_text(
        _first_present(raw, ("prompt", "stem", "question", "text")),
        f"{name}.prompt",
    )
    score = raw.get("score")
    if score is None:
        raise PlatformInputError(f"{name}.score 不能为空")
    references = _first_present(raw, ("reference_answers", "answers", "answer"))
    return {
        "prompt": prompt,
        "text": prompt,
        "score": _normalise_number(score, f"{name}.score", minimum=0),
        "reference_answers": tuple(
            _normalise_string_list(references, f"{name}.reference_answers")
        ),
    }


def normalise_info_retelling_group(
    value: Mapping[str, Any],
    index: int,
    *,
    base_dir: Path | None,
) -> dict[str, Any]:
    group_name = f"groups[{index}]"
    recording = value.get("recording")
    if not isinstance(recording, Mapping):
        raise PlatformInputError(f"{group_name}.recording 必须是对象")
    audio = _first_present(
        recording,
        ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
    )
    listening_text = _require_nonempty_text(
        _first_present(recording, ("listening_text", "hearing_text", "text")),
        f"{group_name}.recording.listening_text",
    )
    normalized_recording: dict[str, Any] = {
        "audio_path": _resolve_audio_path(audio, f"{group_name}.recording.audio_path", base_dir),
        "listening_text": listening_text,
    }
    image = _first_present(recording, ("image_path", "table_image_path", "image"))
    if image is not None:
        normalized_recording["image_path"] = _resolve_image_path(
            image,
            f"{group_name}.recording.image_path",
            base_dir,
        )
    instruction = recording.get("instruction_text")
    if instruction is not None:
        normalized_recording["instruction_text"] = _require_nonempty_text(
            instruction,
            f"{group_name}.recording.instruction_text",
        )
        occurrence = recording.get("instruction_occurrence")
        if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0:
            raise PlatformInputError(
                f"{group_name}.recording.instruction_occurrence 必须是非负整数"
            )
        normalized_recording["instruction_occurrence"] = occurrence
    instruction_audio = recording.get("instruction_audio_path")
    if instruction_audio is not None:
        normalized_recording["instruction_audio_path"] = _resolve_audio_path(
            instruction_audio,
            f"{group_name}.recording.instruction_audio_path",
            base_dir,
        )
    asking_instruction = recording.get("asking_instruction_text")
    if asking_instruction is not None:
        normalized_recording["asking_instruction_text"] = _require_nonempty_text(
            asking_instruction,
            f"{group_name}.recording.asking_instruction_text",
        )
        occurrence = recording.get("asking_instruction_occurrence")
        if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0:
            raise PlatformInputError(
                f"{group_name}.recording.asking_instruction_occurrence 必须是非负整数"
            )
        normalized_recording["asking_instruction_occurrence"] = occurrence
    asking_instruction_audio = recording.get("asking_instruction_audio_path")
    if asking_instruction_audio is not None:
        normalized_recording["asking_instruction_audio_path"] = _resolve_audio_path(
            asking_instruction_audio,
            f"{group_name}.recording.asking_instruction_audio_path",
            base_dir,
        )

    retelling = value.get("retelling")
    if not isinstance(retelling, Mapping):
        raise PlatformInputError(f"{group_name}.retelling 必须是对象")
    prompt = _require_nonempty_text(
        _first_present(retelling, ("prompt", "stem", "question", "text")),
        f"{group_name}.retelling.prompt",
    )
    score = retelling.get("score")
    if score is None:
        raise PlatformInputError(f"{group_name}.retelling.score 不能为空")
    normalized_retelling: dict[str, Any] = {
        "prompt": prompt,
        "score": _normalise_number(score, f"{group_name}.retelling.score", minimum=0),
        "reference_answers": tuple(
            _normalise_string_list(
                _first_present(retelling, ("reference_answers", "answers")),
                f"{group_name}.retelling.reference_answers",
            )
        ),
    }
    prompt_audio = retelling.get("prompt_audio_path")
    if prompt_audio is not None:
        normalized_retelling["prompt_audio_path"] = _resolve_audio_path(
            prompt_audio,
            f"{group_name}.retelling.prompt_audio_path",
            base_dir,
        )

    raw_asking = _sequence(value.get("asking"), f"{group_name}.asking")
    asking = tuple(
        _normalise_spoken_question(item, f"{group_name}.asking[{question_index}]")
        for question_index, item in enumerate(raw_asking)
        if isinstance(item, Mapping)
    )
    if len(asking) != len(raw_asking):
        raise PlatformInputError(f"{group_name}.asking 中存在无效小题")
    return {
        "type": "信息转述及询问",
        "recording": normalized_recording,
        "retelling": normalized_retelling,
        "asking": asking,
    }


__all__ = ["normalise_info_acquisition_group", "normalise_info_retelling_group"]
