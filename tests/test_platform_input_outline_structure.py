"""Structure-first layout detection from the rendered second-step outline."""

import pytest

from platform_entry.adapter.outline import analyze_outline_rows


def _rows(cards):
    """Build ordered outline rows from ``{part: (title, [(sub, cards)])}``."""

    rows = []
    for part, (title, sections) in enumerate(cards, start=1):
        rows.append({"kind": "part", "text": f"{'一二三四五六七八九十'[part - 1]}、{title}"})
        for index, (subsection, entries) in enumerate(sections):
            if subsection:
                rows.append({"kind": "subsection", "text": f"第{'一二三四五六'[index]}节 {subsection}"})
            for number, card_kind in entries:
                rows.append(
                    {
                        "kind": "card",
                        "number": number,
                        "card": card_kind,
                        "text": f"第{number}题({card_kind})",
                    }
                )
    return rows


def _foreign_legacy_rows():
    return _rows([
        ("模仿朗读", [("", [(1, "录音题")])]),
        ("信息获取", [
            ("听选信息", [(number, "录音题") for number in range(2, 8)]),
            ("回答问题", [(number, "录音题") for number in range(8, 12)]),
        ]),
        ("信息转述及询问", [
            ("信息转述", [(12, "录音题")]),
            ("询问信息", [(13, "录音题"), (14, "录音题")]),
        ]),
    ])


def test_global_outline_exposes_counts_and_subsections():
    structure = analyze_outline_rows(_foreign_legacy_rows())

    assert structure is not None
    assert structure.counts() == {"选择题": 0, "填空题": 0, "录音题": 14}
    assert structure.group_target("信息获取") == ("第2题(录音题)", 0)
    assert structure.subsection_target("信息获取", "回答问题") == ("第8题(录音题)", 0)
    assert structure.subsection_target("信息转述及询问", "询问信息") == ("第13题(录音题)", 0)


def test_numbering_restarts_are_read_as_section_local():
    rows = _rows([
        ("模仿朗读", [("", [(1, "录音题")])]),
        ("信息获取", [("", [(1, "录音题"), (2, "录音题")])]),
    ])

    structure = analyze_outline_rows(rows)

    assert structure is not None
    # Section starts come from the page, not from an assumed global counter.
    assert structure.group_target("模仿朗读") == ("第1题(录音题)", 0)
    assert structure.group_target("信息获取") == ("第1题(录音题)", 0)


def test_unreadable_outline_returns_none_so_callers_keep_registered_rules():
    assert analyze_outline_rows([]) is None
    assert analyze_outline_rows([{"kind": "part", "text": "一、模仿朗读"}]) is None


@pytest.mark.parametrize("kind", ["选择题", "填空题", "录音题"])
def test_counts_track_each_card_kind(kind):
    rows = _rows([("任意部分", [("", [(1, kind), (2, kind)])])])

    structure = analyze_outline_rows(rows)

    assert structure is not None
    assert structure.counts()[kind] == 2
    assert sum(value for key, value in structure.counts().items() if key != kind) == 0
