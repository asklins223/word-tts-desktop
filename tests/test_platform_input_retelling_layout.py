"""Regression coverage for the Shenzhen variant of the shared exam template."""

from types import SimpleNamespace
from unittest.mock import Mock
from dataclasses import replace

import pytest

from platform_entry.adapter.automation import PlatformInputPageAutomation
from platform_entry.adapter.errors import PlatformInputUiError
from platform_entry.adapter.models import PlatformInputSpec
from platform_entry.adapter.rules import resolve_paper_bundle_rule


def _exam_spec():
    materials = [
        {"section": "听选信息", "questions": [{}] * 6},
        {"section": "回答问题", "questions": [{}] * 4},
    ]
    return PlatformInputSpec(
        paper={"title": "深圳试卷1"},
        items=(),
        paper_category="听说考试",
        template_name="外研版听说测试题模板",
        bundle_rule="platform_input-foreign-legacy-listening-exam-v1",
        groups=(
            {"type": "模仿朗读", "questions": [{}]},
            {"type": "信息获取", "materials": materials},
            {"type": "信息转述及询问", "retelling": {"prompt": "复述"}, "asking": [{}, {}]},
        ),
    )


def _automation_with_outline(numbers, *, full_width=False, hidden_numbers=()):
    owner = object.__new__(PlatformInputPageAutomation)
    owner.spec = _exam_spec()
    nodes = []
    for number in (*numbers, *hidden_numbers):
        brackets = ("（", "）") if full_width else ("(", ")")
        text = f"第{number}题{brackets[0]}录音题{brackets[1]}"
        nodes.append(Mock(
            is_visible=Mock(return_value=len(nodes) < len(numbers)),
            inner_text=Mock(return_value=text),
        ))
    def get_by_text(query, *, exact=False):
        matches = [
            node for node in nodes
            if (
                node.inner_text() == query if exact
                else query.search(node.inner_text()) is not None
            )
        ]
        return Mock(count=Mock(return_value=len(matches)), nth=Mock(side_effect=matches.__getitem__))

    owner.page = Mock(get_by_text=Mock(side_effect=get_by_text))
    return owner


@pytest.mark.parametrize("full_width", [False, True])
def test_shared_bundle_uses_verified_global_subsection_numbers(full_width):
    owner = _automation_with_outline(
        list(range(1, 15)), full_width=full_width, hidden_numbers=[1, 2]
    )
    brackets = ("（", "）") if full_width else ("(", ")")
    for group, section, number in (
        ("信息获取", "听选信息", 2),
        ("信息获取", "回答问题", 8),
        ("信息转述及询问", "信息转述", 12),
        ("信息转述及询问", "询问信息", 13),
    ):
        assert owner._legacy_subsection_target(group, section, ("fallback", 0)) == (
            f"第{number}题{brackets[0]}录音题{brackets[1]}", 0
        )


@pytest.mark.parametrize("numbers", [
    [1, *range(1, 11), 1, 2, 3],  # Original Foshan local numbering.
    list(range(1, 14)),  # Incomplete global outline must not be inferred.
])
def test_local_or_incomplete_outline_keeps_registered_targets(numbers):
    owner = _automation_with_outline(numbers)
    assert owner._legacy_subsection_target("信息获取", "听选信息", ("fallback", 0)) == (
        "第1题(录音题)", 1
    )
    assert owner._legacy_subsection_target("信息转述及询问", "询问信息", ("fallback", 0)) == (
        "第2题(录音题)", 1
    )


@pytest.mark.parametrize("section,number", [("信息转述", 12), ("询问信息", 13)])
def test_global_subsection_target_clicks_the_correct_outline_entry(section, number):
    owner = _automation_with_outline(list(range(1, 15)))
    owner.action_timeout_ms = 100
    owner._wait_until = lambda predicate, *_args, **_kwargs: predicate()
    target = owner._legacy_subsection_target("信息转述及询问", section, ("fallback", 0))
    owner._activate_outline_section("信息转述及询问", outline_target=target)
    selected = owner.page.get_by_text(target[0], exact=True).nth(0)
    selected.click.assert_called_once_with(timeout=100)
    assert selected.inner_text() == f"第{number}题(录音题)"


def test_shenzhen_template_has_its_own_four_subsection_targets():
    rule = resolve_paper_bundle_rule("深圳中考听说真题")
    assert rule is not None
    assert rule.key == "platform_input-shenzhen-listening-exam-v1"
    assert rule.card_counts() == {"录音题": 14}
    assert rule.outline_numbering == "global"
    assert [
        rule.outline_subsection_target(group, section)
        for group, section in (
            ("信息获取", "听选信息"), ("信息获取", "回答问题"),
            ("信息转述及询问", "信息转述"), ("信息转述及询问", "询问信息"),
        )
    ] == [(f"第{number}题(录音题)", 0) for number in (2, 8, 12, 13)]


@pytest.mark.parametrize("bundle_rule", [None, "platform_input-shenzhen-listening-exam-v1"])
@pytest.mark.parametrize("full_width", [False, True])
@pytest.mark.parametrize("group,number", [("信息获取", 8), ("信息转述及询问", 13)])
def test_explicit_subsection_target_is_not_replaced_by_the_group_start(bundle_rule, full_width, group, number):
    owner = _automation_with_outline(list(range(1, 15)), full_width=full_width)
    owner.spec = replace(owner.spec, bundle_rule=bundle_rule, template_name="深圳中考听说真题")
    owner.action_timeout_ms = 100
    owner._wait_until = lambda predicate, *_args, **_kwargs: predicate()

    owner._activate_outline_section(group, outline_target=(f"第{number}题(录音题)", 0))

    left, right = ("（", "）") if full_width else ("(", ")")
    selected = owner.page.get_by_text(f"第{number}题{left}录音题{right}", exact=True).nth(0)
    selected.click.assert_called_once_with(timeout=100)


def test_shenzhen_acquisition_handler_visits_six_card_then_four_card_panels():
    owner = _automation_with_outline(list(range(1, 15)))
    owner.spec = replace(owner.spec, bundle_rule="platform_input-shenzhen-listening-exam-v1")
    groups = [{"materials": [
        {"section": section, "listening_text": section, "audio_path": f"{index}.mp3",
         "questions": [{"prompt": f"Question {number}"} for number in range(count)]}
        for index, (section, count) in enumerate((("听选信息", 6), ("回答问题", 4)))
    ]}]
    owner._activate_group_section = Mock(side_effect=[
        ([Mock() for _ in range(6)], [Mock()]),
        ([Mock() for _ in range(4)], [Mock()]),
    ])
    owner._replace_rich_text = Mock()
    owner._upload_original_audio = Mock()
    owner._fill_recording_card = Mock()

    assert owner._fill_info_acquisition_groups({"信息获取": groups}) == {"录音题": 10}

    calls = owner._activate_group_section.call_args_list
    assert [(call.args[2], call.kwargs["outline_target"]) for call in calls] == [
        (6, ("第2题(录音题)", 0)), (4, ("第8题(录音题)", 0)),
    ]
    assert owner._fill_recording_card.call_count == 10


def test_shenzhen_retelling_uses_the_split_panels_for_the_three_guides():
    owner = _automation_with_outline(list(range(1, 15)))
    owner.spec = replace(owner.spec, bundle_rule="platform_input-shenzhen-listening-exam-v1")
    owner._fill_split_info_retelling_groups = Mock(return_value={"录音题": 3})
    group = _retelling_group()
    group["recording"]["reading_instruction_text"] = "现在，你有15秒钟的时间阅读思维导图。"

    assert owner._fill_info_retelling_groups({"信息转述及询问": [group]}) == {"录音题": 3}

    owner._fill_split_info_retelling_groups.assert_called_once_with(
        [group], retelling_target=("第12题(录音题)", 0), asking_target=("第13题(录音题)", 0),
    )


def _retelling_group():
    return {
        "recording": {
            "listening_text": "I'm Peter. I visited Yangshuo last August.",
            "audio_path": "original.mp3",
            "instruction_text": "你将听到一篇 Peter 讲述去阳朔旅行的短文。",
            "instruction_occurrence": 0,
            "instruction_audio_path": "guide-1.mp3",
            "asking_instruction_text": "你希望了解和短文相关的信息，请根据以下提示提问。",
            "asking_instruction_occurrence": 0,
        },
        "retelling": {
            "prompt": "你的复述可以这样开始：Peter visited Yangshuo ...",
            "prompt_audio_path": "guide-2.mp3",
            "reference_answers": ["Peter visited Yangshuo last August."],
            "score": 8,
        },
        "asking": [
            {"prompt": "你想知道阳朔有机场吗，你问 Peter：", "reference_answers": ["Is there an airport?"]},
            {"prompt": "你想知道 Peter 在阳朔待了多少天，你问 Peter：", "reference_answers": ["How long did you stay?"]},
        ],
    }


def _retelling_automation(scopes):
    owner = object.__new__(PlatformInputPageAutomation)
    owner._activate_group_section = Mock(side_effect=[
        (["retelling-card"], ["listening-editor"]),
        (["asking-card-1", "asking-card-2"], []),
    ])
    owner._retelling_page_instruction_scopes = Mock(return_value=scopes)
    owner._replace_rich_text = Mock()
    owner._upload_original_audio = Mock()
    owner._upload_audio_in_scope = Mock()
    owner._fill_instruction_editor = Mock()
    owner._fill_instruction_audio = Mock()
    owner._fill_recording_card = Mock()
    owner._labeled_text_editor_in_scope = Mock(return_value=Mock())
    owner._audio_region_in_scope = Mock(return_value=Mock())
    return owner


def _fill_split(owner, groups):
    return owner._fill_split_info_retelling_groups(
        groups,
        retelling_target=("第12题(录音题)", 0),
        asking_target=("第13题(录音题)", 0),
    )


def test_two_guide_document_keeps_starter_in_the_recording_card():
    first = Mock(locator=Mock(return_value="page-1-guide-editor"))
    second = Mock(locator=Mock(return_value="page-2-guide-editor"))
    owner = _retelling_automation((first, second))
    group = _retelling_group()

    assert _fill_split(owner, [group]) == {"录音题": 3}

    writes = owner._replace_rich_text.call_args_list
    assert [call.args for call in writes[1:]] == [
        ("page-1-guide-editor", group["recording"]["instruction_text"]),
    ]
    uploads = owner._upload_audio_in_scope.call_args_list
    assert [call.args for call in uploads] == [(first, "guide-1.mp3")]
    assert all(call.kwargs["label"] == "题目指导文字音频" for call in uploads)
    retelling_card = owner._fill_recording_card.call_args_list[0]
    assert retelling_card.args == ("retelling-card", group["retelling"])
    assert retelling_card.kwargs["prompt_audio_path"] == "guide-2.mp3"
    assert retelling_card.kwargs["fill_answer_time"] is False
    assert owner._fill_recording_card.call_count == 3
    # The asking guidance remains text-only; it must not consume either guide audio.
    owner._fill_instruction_editor.assert_called_once_with(
        group["recording"]["asking_instruction_text"], 0, "询问信息题目指导文字"
    )
    owner._fill_instruction_audio.assert_not_called()


def test_previous_layout_keeps_second_guide_in_recording_stem():
    owner = _retelling_automation(None)
    group = _retelling_group()
    assert _fill_split(owner, [group]) == {"录音题": 3}
    owner._upload_audio_in_scope.assert_not_called()
    owner._fill_instruction_audio.assert_called_once_with(
        "guide-1.mp3", 0, "信息转述题目指导文字"
    )
    assert owner._fill_recording_card.call_args_list[0].kwargs["prompt_audio_path"] == "guide-2.mp3"


def test_three_chinese_guides_use_two_page_fields_and_the_recording_stem():
    scopes = tuple(
        Mock(locator=Mock(return_value=editor))
        for editor in ("intro-editor", "reading-editor")
    )
    owner = _retelling_automation(scopes)
    group = _retelling_group()
    group["recording"].update({
        "reading_instruction_text": "现在，你有15秒钟的时间阅读思维导图。",
        "reading_instruction_audio_path": "guide-2.mp3",
    })
    group["retelling"]["prompt_audio_path"] = "guide-3.mp3"

    assert _fill_split(owner, [group]) == {"录音题": 3}

    assert [call.args for call in owner._replace_rich_text.call_args_list[1:]] == [
        ("intro-editor", group["recording"]["instruction_text"]),
        ("reading-editor", group["recording"]["reading_instruction_text"]),
    ]
    assert [call.args for call in owner._upload_audio_in_scope.call_args_list] == [
        (scope, f"guide-{index}.mp3") for index, scope in enumerate(scopes, start=1)
    ]
    retelling_card = owner._fill_recording_card.call_args_list[0]
    assert retelling_card.args == ("retelling-card", group["retelling"])
    assert retelling_card.kwargs["prompt"] == group["retelling"]["prompt"]
    assert retelling_card.kwargs["prompt_audio_path"] == "guide-3.mp3"
    owner._fill_instruction_audio.assert_not_called()


@pytest.mark.parametrize("scopes", [None, (Mock(),)])
def test_three_guides_require_both_page_instruction_fields_before_any_write(scopes):
    owner = _retelling_automation(scopes)
    group = _retelling_group()
    group["recording"]["reading_instruction_text"] = "现在，你有15秒钟的时间阅读思维导图。"

    with pytest.raises(PlatformInputUiError, match="独立阅图指导语"):
        _fill_split(owner, [group])

    owner._replace_rich_text.assert_not_called()
    owner._upload_original_audio.assert_not_called()
    owner._upload_audio_in_scope.assert_not_called()


@pytest.mark.parametrize("missing", ["_labeled_text_editor_in_scope", "_audio_region_in_scope"])
def test_three_guides_require_the_recording_stem_pair_before_any_write(missing):
    owner = _retelling_automation((Mock(), Mock()))
    getattr(owner, missing).return_value = None
    group = _retelling_group()
    group["recording"]["reading_instruction_text"] = "现在，你有15秒钟的时间阅读思维导图。"

    with pytest.raises(PlatformInputUiError, match="题干与题干音频"):
        _fill_split(owner, [group])

    owner._replace_rich_text.assert_not_called()
    owner._upload_original_audio.assert_not_called()


def test_three_guides_do_not_silently_drop_reading_in_an_unconfigured_template():
    owner = _retelling_automation(None)
    owner.spec = SimpleNamespace(bundle_rule=None)
    owner._legacy_subsection_target = Mock(return_value=("unused", 0))
    group = _retelling_group()
    group["recording"]["reading_instruction_text"] = "现在，你有15秒钟的时间阅读思维导图。"

    with pytest.raises(PlatformInputUiError, match="尚未配置三条中文指导语"):
        owner._fill_info_retelling_groups({"信息转述及询问": [group]})

    owner._activate_group_section.assert_not_called()
    owner._replace_rich_text.assert_not_called()


def test_two_guide_layout_rejects_multiple_materials_before_writing():
    owner = _retelling_automation((Mock(), Mock()))
    with pytest.raises(PlatformInputUiError, match="只能录入一组"):
        _fill_split(owner, [_retelling_group(), _retelling_group()])
    owner._replace_rich_text.assert_not_called()
    owner._upload_original_audio.assert_not_called()


@pytest.mark.parametrize("before_count,first_count", [(2, 1), (1, 2)])
def test_ambiguous_page_guides_fail_before_any_write(before_count, first_count):
    owner = object.__new__(PlatformInputPageAutomation)
    first = Mock(count=Mock(return_value=first_count))
    before = Mock(evaluate=Mock(return_value=-1))
    second = Mock(count=Mock(return_value=before_count), nth=Mock(return_value=before))
    page1 = SimpleNamespace(locator=lambda _selector: SimpleNamespace(filter=lambda **_kwargs: first))
    page2 = SimpleNamespace(locator=lambda _selector: SimpleNamespace(filter=lambda **_kwargs: second))
    pages = Mock(filter=Mock(side_effect=[
        SimpleNamespace(count=lambda: 1, first=page1),
        SimpleNamespace(count=lambda: 1, first=page2),
    ]))
    owner.page = Mock(locator=Mock(return_value=pages))
    with pytest.raises(PlatformInputUiError, match="位置不唯一"):
        owner._retelling_page_instruction_scopes()


def test_page_scopes_ignore_the_template_preparation_field_after_the_listening_material():
    owner = object.__new__(PlatformInputPageAutomation)
    first_scope = Mock(locator=Mock(return_value=Mock(count=Mock(return_value=1))))
    before = Mock(evaluate=Mock(return_value=-1), locator=first_scope.locator)
    after = Mock(evaluate=Mock(return_value=1))
    first = Mock(count=Mock(return_value=1), first=first_scope)
    # DOM enumeration order must not affect the semantic ordering.
    second = Mock(count=Mock(return_value=2), nth=Mock(side_effect=[after, before]))
    page1 = SimpleNamespace(locator=lambda _selector: SimpleNamespace(filter=lambda **_kwargs: first))
    page2 = SimpleNamespace(locator=lambda _selector: SimpleNamespace(filter=lambda **_kwargs: second))
    pages = Mock(filter=Mock(side_effect=[
        SimpleNamespace(count=lambda: 1, first=page1),
        SimpleNamespace(count=lambda: 1, first=page2),
    ]))
    owner.page = Mock(locator=Mock(return_value=pages))
    owner._audio_region_in_scope = Mock(return_value=Mock())

    assert owner._retelling_page_instruction_scopes() == (first_scope, before)
    after.locator.assert_not_called()
