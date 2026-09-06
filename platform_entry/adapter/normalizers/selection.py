"""Page-semantic normalizer for listening-selection groups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import PlatformInputError
from ..normalization import (
    _first_present,
    _normalise_common_numbers,
    _normalise_question,
    _require_nonempty_text,
    _resolve_audio_path,
)


def normalise_selection_group(
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

    raw_materials = _first_present(value, ("materials", "recordings", "passages"))
    if raw_materials is None:
        raw_questions = _first_present(value, ("questions", "items"))
        if raw_questions is None:
            raise PlatformInputError(
                f"{group_name}（听后选择）必须提供 materials；每个材料下面再列 questions"
            )
        # One-material shorthand still keeps a materials layer so a shared
        # recording cannot be mistaken for one audio file per sub-question.
        raw_materials = [
            {
                "audio_path": group_audio,
                "listening_text": group_text,
                "questions": raw_questions,
                "times": value.get("times", value.get("play_count")),
                "gap": value.get("gap", value.get("interval")),
            }
        ]

    if isinstance(raw_materials, (str, bytes)) or not isinstance(raw_materials, Sequence):
        raise PlatformInputError(f"{group_name}.materials 必须是数组")
    if not raw_materials:
        raise PlatformInputError(f"{group_name}.materials 不能为空")

    materials: list[dict[str, Any]] = []
    for material_index, raw_material in enumerate(raw_materials):
        material_name = f"{group_name}.materials[{material_index}]"
        if not isinstance(raw_material, Mapping):
            raise PlatformInputError(f"{material_name} 必须是对象")
        audio = _first_present(
            raw_material,
            ("audio_path", "listening_audio_path", "original_audio_path", "audio"),
        )
        if audio is None:
            audio = group_audio
        if isinstance(audio, Mapping):
            audio = _first_present(audio, ("path", "local_path"))
        if audio is None:
            raise PlatformInputError(f"{material_name}.audio_path 不能为空")
        listening_text = _first_present(
            raw_material,
            ("listening_text", "hearing_text", "original_text", "source_text"),
        )
        if listening_text is None:
            listening_text = group_text
        listening_text = _require_nonempty_text(
            listening_text,
            f"{material_name}.listening_text",
        )
        raw_questions = _first_present(raw_material, ("questions", "items"))
        if isinstance(raw_questions, (str, bytes)) or not isinstance(raw_questions, Sequence):
            raise PlatformInputError(f"{material_name}.questions 必须是数组")
        if not raw_questions:
            raise PlatformInputError(f"{material_name}.questions 不能为空")
        questions = tuple(
            _normalise_question(
                question,
                f"{material_name}.questions[{question_index}]",
                group_type="听后选择",
                base_dir=base_dir,
            )
            for question_index, question in enumerate(raw_questions)
        )
        material: dict[str, Any] = {
            "audio_path": _resolve_audio_path(
                audio,
                f"{material_name}.audio_path",
                base_dir,
            ),
            "listening_text": listening_text,
            "questions": questions,
        }
        material.update(
            _normalise_common_numbers(
                raw_material,
                material_name,
                keys={"times": ("times", "play_count"), "gap": ("gap", "interval")},
            )
        )
        materials.append(material)
    return {"type": "听后选择", "materials": tuple(materials)}


__all__ = ["normalise_selection_group"]
