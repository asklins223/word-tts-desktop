"""Reviewed paper-template registry and deterministic rule resolution."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .common import normalise_text
from .constants import DEFAULT_OUTLINE_TARGETS
from .errors import PlatformInputError
from .models import PaperBundleRule


PAPER_BUNDLE_RULES: tuple[PaperBundleRule, ...] = (
    PaperBundleRule(
        key="platform_input-info-retelling-special-v1",
        # The standalone question template is the same major topic as the
        # bundled 外研版 exam, but its two lazy panels contain only this one
        # major topic.  Keep a dedicated rule so the content handler reuses
        # the reviewed retelling/asking flow without treating the page as the
        # 14-card full-paper layout.
        template_name_tokens=(
            "信息转述及询问",
        ),
        template_ids=("2085639857241194496",),
        expected_card_counts=(("录音题", 3),),
        outline_targets=(
            ("信息转述及询问", "第1题(录音题)", 0),
        ),
        outline_numbering="local",
        outline_subsection_targets=(
            ("信息转述及询问", "信息转述", "第1题(录音题)", 0),
            ("信息转述及询问", "询问信息", "第2题(录音题)", 0),
        ),
    ),
    PaperBundleRule(
        key="platform_input-foreign-legacy-listening-exam-v1",
        # This is a template-level layout, not a paper-title-level layout.
        # The imported document title is e.g. “佛山七上Starter 1”, while the
        # platform template identifies the 外研版 14-card bundle.
        template_name_tokens=(
            "外研版 听说测试题模板",
            "外研版听说测试题模板",
            "外研版 听说测试模版",
            "外研版听说测试模版",
        ),
        template_ids=("2094610981895405568",),
        expected_card_counts=(("录音题", 14),),
        outline_targets=(
            ("模仿朗读", "第1题(录音题)", 0),
            # The platform restarts the visible number at each section.  The
            # occurrence disambiguates the three repeated “第1题” entries in
            # the single-page outline: imitation, information acquisition,
            # and retelling/inquiry.
            ("信息获取", "第1题(录音题)", 1),
            ("信息转述及询问", "第1题(录音题)", 2),
        ),
        outline_numbering="local",
        outline_subsection_targets=(
            ("信息获取", "听选信息", "第1题(录音题)", 1),
            ("信息获取", "回答问题", "第7题(录音题)", 0),
            ("信息转述及询问", "信息转述", "第1题(录音题)", 2),
            # The local outline repeats “第2题(录音题)” in 信息获取第一节
            # and 询问信息.  Keep the occurrence explicit; selecting the
            # first match would reopen 信息获取的第二题 and leave asking
            # cards unmounted.
            ("信息转述及询问", "询问信息", "第2题(录音题)", 1),
        ),
    ),
    PaperBundleRule(
        key="platform_input-listening-exam-v1",
        template_name_tokens=("听说测试题模板", "听说测试模版"),
        expected_card_counts=(
            ("选择题", 8),
            ("填空题", 3),
            ("录音题", 9),
        ),
        outline_targets=tuple(
            (group_type, target_text, occurrence)
            for group_type, (target_text, occurrence)
            in DEFAULT_OUTLINE_TARGETS.items()
        ),
    ),
    PaperBundleRule(
        key="platform_input-listening-selection-v1",
        # A standalone 听后选择 document uses the same selection-card
        # semantics as the listening-paper section, but its question count is
        # document-driven rather than fixed to the eight-card full-paper rule.
        template_name_tokens=("听后选择",),
        outline_targets=(
            ("听后选择", "第1题(选择题)", 0),
        ),
        outline_numbering="local",
    ),
)


def resolve_paper_bundle_rule(
    template_name: Any,
    *,
    paper_name: Any = None,
    template_id: Any = None,
    explicit_key: Any = None,
    rules: Sequence[PaperBundleRule] | None = None,
) -> PaperBundleRule | None:
    """Resolve a reviewed bundle rule without trusting input selectors."""

    normalized_name = normalise_text(template_name).casefold()
    normalized_paper_name = normalise_text(paper_name).casefold()
    normalized_id = normalise_text(template_id).casefold()
    requested_key = str(explicit_key or "").strip()
    candidates = list(rules if rules is not None else PAPER_BUNDLE_RULES)
    if requested_key:
        candidates = [rule for rule in candidates if rule.key == requested_key]
        if not candidates:
            raise PlatformInputError(f"bundle_rule 未注册：{requested_key}")
    matched = []
    paper_matched = []
    match_strength: dict[str, int] = {}
    for rule in candidates:
        name_tokens = {
            normalise_text(token).casefold()
            for token in rule.template_name_tokens
            if normalise_text(token)
        }
        template_ids = {
            normalise_text(identifier).casefold()
            for identifier in rule.template_ids
            if normalise_text(identifier)
        }
        name_strengths = [
            len(token) for token in name_tokens if token in normalized_name
        ]
        id_matched = bool(normalized_id) and normalized_id in template_ids
        template_matched = bool(name_strengths) or id_matched
        paper_tokens = {
            normalise_text(token).casefold()
            for token in rule.paper_name_tokens
            if normalise_text(token)
        }
        paper_name_matched = bool(normalized_paper_name) and any(
            token in normalized_paper_name for token in paper_tokens
        )
        if template_matched:
            matched.append(rule)
            # Prefer a template-specific match over a broad compatibility
            # token such as “听说测试题模板”.  An exact registered template
            # id outranks name matching altogether.  Equal-strength matches
            # remain ambiguous and still fail closed below.
            match_strength[rule.key] = max(
                name_strengths + ([10**6] if id_matched else [])
            )
            if paper_name_matched:
                paper_matched.append(rule)

    if len(matched) > 1:
        strongest = max(match_strength.get(rule.key, 0) for rule in matched)
        strongest_matches = [
            rule for rule in matched if match_strength.get(rule.key, 0) == strongest
        ]
        if len(strongest_matches) < len(matched):
            matched = strongest_matches
            paper_matched = [rule for rule in paper_matched if rule in matched]
    # A reviewed paper-specific rule wins over a generic template rule. This
    # is what lets the same visible “听说测试题模板” host both modern and the
    # 外研旧版套卷 without changing the page selectors globally.
    paper_specific = [rule for rule in matched if rule.paper_name_tokens]
    if paper_specific:
        if paper_matched:
            matched = paper_matched
        else:
            # A named non-外研 paper using the same generic template belongs
            # to the original rule; an unqualified direct resolver call keeps
            # the same compatibility behavior.
            matched = [rule for rule in matched if not rule.paper_name_tokens]
    if len(matched) > 1:
        raise PlatformInputError(
            "平台模板同时匹配多个套卷规则："
            + ", ".join(rule.key for rule in matched)
            + "；请通过已注册的 bundle_rule 明确指定"
        )
    if matched:
        return matched[0]
    if requested_key:
        raise PlatformInputError(
            f"bundle_rule={requested_key!r} 与平台模板不匹配；请使用已注册的模板规则"
        )
    return None
