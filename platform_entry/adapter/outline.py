"""Structure-first reading of the second-step question outline.

The platform lets editors create paper templates freely, so the template list
is dynamic data.  Layout facts (which card kinds exist, how many of each, and
whether outline numbers restart inside a section) live on the rendered page,
not in the template's display name.  This module turns the visible outline
into that structure so callers can choose a layout by what the page shows
instead of by a hard-coded name table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

CARD_KINDS = ("选择题", "填空题", "录音题")

_CARD_TEXT = re.compile(r"^第\s*(\d+)\s*题?\s*[（(]\s*(选择题|填空题|录音题)\s*[）)]$")
_PART_TEXT = re.compile(r"^[一二三四五六七八九十]+\s*[、.]\s*(.*)$")

# Group families whose outline entry is a part heading.  The section order is
# authoritative on the page, so a keyword is only used to find the heading;
# no number is registered here.
_GROUP_PART_KEYWORDS: dict[str, tuple[str, ...]] = {
    "听后选择": ("听后选择",),
    "听后应答": ("听后应答",),
    "模仿朗读": ("模仿朗读",),
    "听后记录并转述信息": ("听后记录",),
    "信息获取": ("信息获取",),
    "信息转述及询问": ("信息转述",),
}

_RETELLING_SUBSECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "信息转述": ("信息转述",),
    "询问信息": ("询问信息", "提问"),
}


@dataclass(frozen=True)
class OutlineCard:
    number: int
    kind: str

    @property
    def text(self) -> str:
        return f"第{self.number}题({self.kind})"


@dataclass(frozen=True)
class OutlineSection:
    """One part heading with its optional lazy-loaded subsections."""

    title: str
    first_number: int
    cards: tuple[OutlineCard, ...]
    subsections: tuple[tuple[str, int], ...] = ()

    def subsection_target(self, keywords: Sequence[str]) -> tuple[int, str] | None:
        for title, number in self.subsections:
            if any(keyword in title for keyword in keywords):
                return number, self.kind_at(number)
        return None

    def kind_at(self, number: int) -> str:
        for card in self.cards:
            if card.number == number:
                return card.kind
        return self.cards[0].kind if self.cards else "录音题"


@dataclass(frozen=True)
class OutlineStructure:
    """Visible outline facts for one opened second-step page."""

    cards: tuple[OutlineCard, ...]
    sections: tuple[OutlineSection, ...]

    def counts(self) -> dict[str, int]:
        counts = {kind: 0 for kind in CARD_KINDS}
        for card in self.cards:
            counts[card.kind] = counts.get(card.kind, 0) + 1
        return counts

    def section_for(self, group_type: str) -> OutlineSection | None:
        keywords = _GROUP_PART_KEYWORDS.get(group_type, (group_type,))
        for section in self.sections:
            if any(keyword in section.title for keyword in keywords):
                return section
        return None

    def group_target(self, group_type: str) -> tuple[str, int] | None:
        section = self.section_for(group_type)
        if section is None:
            return None
        return OutlineCard(section.first_number, section.kind_at(section.first_number)).text, 0

    def subsection_target(self, group_type: str, subsection: str) -> tuple[str, int] | None:
        section = self.section_for(group_type)
        if section is None:
            return None
        keywords = _RETELLING_SUBSECTION_KEYWORDS.get(subsection, (subsection,))
        resolved = section.subsection_target(keywords)
        if resolved is None:
            return None
        number, kind = resolved
        return OutlineCard(number, kind).text, 0


def analyze_outline_rows(rows: Sequence[Any]) -> OutlineStructure | None:
    """Build a structure from the ordered visible outline rows.

    ``rows`` is the list produced by the page probe: ``{"kind": "part" |
    "subsection" | "card", ...}`` in document order.  Returns ``None`` when
    the probe did not observe a usable card outline, so callers can keep the
    registered fallback instead of treating a blank page as a valid layout.
    """

    cards: list[OutlineCard] = []
    sections: list[OutlineSection] = []
    current_title: str | None = None
    current_cards: list[OutlineCard] = []
    current_subsections: list[tuple[str, int]] = []
    pending_subsection: str | None = None

    def flush() -> None:
        nonlocal current_title, current_cards, current_subsections, pending_subsection
        if current_title is not None and current_cards:
            sections.append(
                OutlineSection(
                    title=current_title,
                    first_number=current_cards[0].number,
                    cards=tuple(current_cards),
                    subsections=tuple(current_subsections),
                )
            )
        current_title = None
        current_cards = []
        current_subsections = []
        pending_subsection = None

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or "")
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        if kind == "card":
            number = row.get("number")
            card_kind = str(row.get("card") or "")
            if isinstance(number, bool) or not isinstance(number, int):
                match = _CARD_TEXT.match(text)
                if match is None:
                    continue
                number = int(match.group(1))
                card_kind = match.group(2)
            if card_kind not in CARD_KINDS:
                continue
            card = OutlineCard(number, card_kind)
            cards.append(card)
            current_cards.append(card)
            if pending_subsection is not None:
                current_subsections.append((pending_subsection, number))
                pending_subsection = None
            continue
        if kind == "part":
            flush()
            match = _PART_TEXT.match(text)
            current_title = match.group(1).strip() if match else text
            continue
        if kind == "subsection" and current_title is not None:
            pending_subsection = text

    flush()
    if not cards:
        return None
    return OutlineStructure(cards=tuple(cards), sections=tuple(sections))


__all__ = [
    "CARD_KINDS",
    "OutlineCard",
    "OutlineSection",
    "OutlineStructure",
    "analyze_outline_rows",
]
