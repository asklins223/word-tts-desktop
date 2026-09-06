"""Pure card-count functions for the reviewed question families."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def count_selection_group(group: Mapping[str, Any]) -> dict[str, int]:
    return {
        "选择题": sum(
            len(material.get("questions", ()))
            for material in group.get("materials", ())
        )
    }


def count_response_group(group: Mapping[str, Any]) -> dict[str, int]:
    # The listening-test template renders response prompts as recording cards
    # (with a spoken reference-answer row), even though the Word source uses
    # two written choices to identify the correct response.
    return {"录音题": len(group.get("questions", ()))}


def count_imitation_group(group: Mapping[str, Any]) -> dict[str, int]:
    return {"录音题": len(group.get("questions", ()))}


def count_record_retelling_group(group: Mapping[str, Any]) -> dict[str, int]:
    recording = group.get("recording") or {}
    return {
        "填空题": len(recording.get("questions", ())),
        "录音题": 1 if group.get("retelling") else 0,
    }


def count_info_acquisition_group(group: Mapping[str, Any]) -> dict[str, int]:
    return {
        "录音题": sum(
            len(material.get("questions", ()))
            for material in group.get("materials", ())
            if isinstance(material, Mapping)
        )
    }


def count_info_retelling_group(group: Mapping[str, Any]) -> dict[str, int]:
    asking = group.get("asking") or ()
    return {"录音题": (1 if group.get("retelling") else 0) + len(asking)}


__all__ = [
    "count_imitation_group",
    "count_record_retelling_group",
    "count_info_acquisition_group",
    "count_info_retelling_group",
    "count_response_group",
    "count_selection_group",
]
