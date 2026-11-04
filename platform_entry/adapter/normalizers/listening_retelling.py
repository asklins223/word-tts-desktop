"""Validate the standalone listening-retelling page fields."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import PlatformInputError
from ..normalization import (
    _first_present, _normalise_number, _normalise_string_list,
    _require_nonempty_text, _resolve_audio_path, _resolve_image_path,
)


def normalise_listening_retelling_group(
    value: Mapping[str, Any], index: int, *, base_dir: Path | None,
) -> dict[str, Any]:
    name = f"groups[{index}]"
    source = value.get("recording")
    question = value.get("retelling")
    if not isinstance(source, Mapping) or not isinstance(question, Mapping):
        raise PlatformInputError(f"{name} 必须提供原文和转述题")
    audio = _first_present(source, ("audio_path", "original_audio_path"))
    image = _first_present(source, ("image_path", "table_image_path"))
    if image is None:
        # The reviewed template ships with an unrelated example diagram.
        # Refuse to leave it in the submitted paper.
        raise PlatformInputError(f"{name}.recording.image_path 不能为空")
    recording = {
        "listening_text": _require_nonempty_text(source.get("listening_text"), f"{name}.recording.listening_text"),
        "audio_path": _resolve_audio_path(audio, f"{name}.recording.audio_path", base_dir),
        "image_path": _resolve_image_path(image, f"{name}.recording.image_path", base_dir),
    }
    retelling = {
        "prompt": _require_nonempty_text(question.get("prompt"), f"{name}.retelling.prompt"),
        "score": _normalise_number(question.get("score"), f"{name}.retelling.score", minimum=0),
        "reference_answers": tuple(_normalise_string_list(question.get("reference_answers"), f"{name}.retelling.reference_answers")),
    }
    if question.get("answer_time") is not None:
        retelling["answer_time"] = _normalise_number(question["answer_time"], f"{name}.retelling.answer_time", minimum=0)
    return {"type": "听后转述", "recording": recording, "retelling": retelling}


__all__ = ["normalise_listening_retelling_group"]
