"""Typed data contracts for page-semantic input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import DEFAULT_OUTLINE_TARGETS


@dataclass(frozen=True)
class UiChoice:
    """页面下拉框要点击的显示值；id 仅作为输入记录。"""

    name: str
    identifier: Any | None = None


@dataclass(frozen=True)
class PaperBundleRule:
    """Code-owned rules for one reviewed paper-template layout."""

    key: str
    template_name_tokens: tuple[str, ...] = ()
    paper_name_tokens: tuple[str, ...] = ()
    template_ids: tuple[str, ...] = ()
    expected_card_counts: tuple[tuple[str, int], ...] = ()
    outline_targets: tuple[tuple[str, str, int], ...] = ()
    # Most listening-test templates number the outline globally.  A few
    # bundled templates restart numbering inside each section, so their
    # registered target text/occurrence must be used as-is.
    outline_numbering: str = "global"
    # Some legacy bundles expose one normalized group as multiple lazy-loaded
    # subsections. Keep those targets code-owned alongside the main group
    # target so handlers do not have to infer page positions from card counts.
    outline_subsection_targets: tuple[tuple[str, str, str, int], ...] = ()

    def card_counts(self) -> dict[str, int]:
        return {str(kind): int(count) for kind, count in self.expected_card_counts}

    def outline_target(self, group_type: str) -> tuple[str, int] | None:
        for kind, target_text, occurrence in self.outline_targets:
            if kind == group_type:
                return target_text, occurrence
        return DEFAULT_OUTLINE_TARGETS.get(group_type)

    def outline_subsection_target(
        self,
        group_type: str,
        subsection: str,
    ) -> tuple[str, int] | None:
        for kind, name, target_text, occurrence in self.outline_subsection_targets:
            if kind == group_type and name == subsection:
                return target_text, occurrence
        return None


@dataclass(frozen=True)
class PlatformInputSpec:
    """已经通过本地校验、可用于页面录入的表单数据。"""

    paper: dict[str, Any]
    items: tuple[dict[str, Any], ...]
    paper_category: str
    template_name: str
    groups: tuple[dict[str, Any], ...] = ()
    template_id: str | None = None
    template_version: str | None = None
    bundle_rule: str | None = None

    @property
    def question_count(self) -> int:
        if not self.groups:
            return len(self.items)

        total = 0
        for group in self.groups:
            materials = group.get("materials") or []
            if materials:
                total += sum(
                    len(material.get("questions") or [])
                    for material in materials
                )
            total += len(group.get("questions") or [])
            recording = group.get("recording") or {}
            total += len(recording.get("questions") or [])
            if group.get("retelling"):
                total += 1
            total += len(group.get("asking") or [])
        return total

    @property
    def question_groups(self) -> tuple[dict[str, Any], ...]:
        return self.groups
