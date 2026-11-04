"""Page contracts for the Wuhan two-section and retelling templates."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from platform_entry.adapter.content_record import PlatformInputRecordContentMixin
from platform_entry.adapter.content_response import PlatformInputResponseContentMixin
from platform_entry.adapter.errors import PlatformInputError, PlatformInputUiError
from platform_entry.adapter.normalizers.listening_retelling import normalise_listening_retelling_group
from platform_entry.adapter.normalizers.response import normalise_response_group
from platform_entry.adapter.outline import analyze_outline_rows
from platform_entry.adapter.page_content import PlatformInputContentMixin
from workflow.platform_template_catalog import normalize_platform_template_catalog
from workflow.system_input_content import page_input_completeness
from workflow.system_input_executor import PlatformInputWorkflowPageExecutor


def _assets(tmp_path):
    audio = tmp_path / "passage.mp3"
    image = tmp_path / "picture.png"
    audio.write_bytes(b"audio")
    image.write_bytes(b"image")
    return str(audio), str(image)


def _response_facts(audio, image):
    return [
        {"section": "第一节", "prompt": "What is it?", "listening_text": "What is it?",
         "audio_path": audio, "score": 1, "answer_time": 5,
         "options": [{"option_id": "A", "text": "★ History."},
                     {"option_id": "B", "text": "★ Biology."},
                     {"option_id": "C", "text": "★ Art."}], "answer": "B",
         **({"image_path": image, "block_image_required": True} if index == 4 else {})}
        for index in range(5)
    ] + [
        {"section": "第二节", "prompt": "Which subject?", "listening_text": "W: Which one? M: IT.",
         "audio_path": audio, "score": 1, "answer_time": 5,
         "reference_answers": ["IT.", "He likes IT."]}
        for _ in range(5)
    ]


def test_response_sections_have_distinct_stems_answers_and_image(tmp_path):
    audio, image = _assets(tmp_path)
    result = normalise_response_group({"questions": _response_facts(audio, image)}, 0, base_dir=tmp_path)
    first, pictured, second = result["questions"][0], result["questions"][4], result["questions"][5]
    assert first["prompt"] == "★ History. ★ Biology. ★ Art."
    assert first["reference_answers"] == ("Biology.",)
    assert pictured["image_path"] == image
    assert second["prompt"] == "Which subject?"
    assert second["listening_text"] == "W: Which one? M: IT."
    assert second["reference_answers"] == ("IT.", "He likes IT.")

    missing_prompt = _response_facts(audio, image)
    del missing_prompt[5]["prompt"]
    with pytest.raises(PlatformInputError, match="prompt"):
        normalise_response_group({"questions": missing_prompt}, 0, base_dir=tmp_path)
    missing_image = _response_facts(audio, image)
    del missing_image[4]["image_path"]
    with pytest.raises(PlatformInputError, match="配图"):
        normalise_response_group({"questions": missing_image}, 0, base_dir=tmp_path)


def test_retelling_requires_own_image_and_keeps_each_answer(tmp_path):
    audio, image = _assets(tmp_path)
    fact = {"type": "听后转述", "recording": {"listening_text": "John likes IT.",
            "image_artifact_id": "guide"}, "retelling": {"prompt": "John says:", "score": 5,
            "answer_time": 90, "reference_answers": ["He likes IT.", "IT is his favourite."]}}
    assert page_input_completeness(fact)["status"] == "complete"
    without_image = {**fact, "recording": {"listening_text": "John likes IT."}}
    assert page_input_completeness(without_image)["status"] == "incomplete"
    normalized = normalise_listening_retelling_group({
        "recording": {"listening_text": "John likes IT.", "audio_path": audio, "image_path": image},
        "retelling": fact["retelling"],
    }, 0, base_dir=tmp_path)
    assert normalized["retelling"]["reference_answers"] == ("He likes IT.", "IT is his favourite.")
    assert normalized["recording"]["image_path"] == image
    with pytest.raises(PlatformInputError, match="image_path"):
        normalise_listening_retelling_group({"recording": {"audio_path": audio,
            "listening_text": "John likes IT."}, "retelling": fact["retelling"]}, 0, base_dir=tmp_path)


def test_question_and_retelling_images_materialize_to_separate_files(tmp_path):
    executor = object.__new__(PlatformInputWorkflowPageExecutor)
    def materialize(artifact, workflow, destination):
        assert workflow == "w1"
        destination.write_text(artifact)
        return "png"
    executor._materialize_page_image = materialize
    question = executor._materialize_page_assets({"type": "听后应答", "questions": [
        {"image_artifact_id": "question-image", "block_image_required": True}]}, "w1", tmp_path, 0)
    retelling = executor._materialize_page_assets({"type": "听后转述", "recording": {
        "image_artifact_id": "guide-image"}}, "w1", tmp_path, 1)
    question_path = Path(question["questions"][0]["image_path"])
    guide_path = Path(retelling["recording"]["image_path"])
    assert question_path != guide_path
    assert question_path.read_text() == "question-image"
    assert guide_path.read_text() == "guide-image"
    assert "image_artifact_id" not in question["questions"][0]
    assert "image_artifact_id" not in retelling["recording"]


def _response_owner(structure, cards_by_target):
    owner = PlatformInputResponseContentMixin()
    owner._read_outline_structure = Mock(return_value=structure)
    owner._activate_group_section = Mock(
        side_effect=lambda _group, _kind, _count, _editors, outline_target: (cards_by_target[outline_target], [])
    )
    owner._question_page_scope = Mock(side_effect=lambda card, _: f"scope-{card}")
    owner._labeled_text_editor_in_scope = Mock(side_effect=lambda scope, label: f"{scope}-{label}")
    owner._audio_region_in_scope = Mock(return_value=object())
    owner._saved_image = Mock(return_value=None)
    owner._image_region = Mock(side_effect=lambda _index, scope: object() if scope.endswith("-4") else None)
    owner._response_screen_question_scope = Mock(side_effect=lambda scope, _: f"guidance-{scope}")
    owner._replace_rich_text = Mock()
    owner._upload_audio_in_scope = Mock()
    owner._upload_image = Mock()
    owner._clear_audio_in_scope = Mock()
    owner._fill_recording_card = Mock()
    owner._fill_card_number = Mock()
    return owner


def test_response_dispatches_two_panels_and_uploads_image_in_its_own_scope(tmp_path):
    audio, image = _assets(tmp_path)
    questions = normalise_response_group({"questions": _response_facts(audio, image)}, 0, base_dir=tmp_path)["questions"]
    cards = [[f"first-{i}" for i in range(5)], [f"second-{i}" for i in range(5)]]
    rows = [{"kind": "part", "text": "二、听后应答"}, {"kind": "subsection", "text": "第一节"}]
    rows += [{"kind": "card", "number": n, "card": "录音题", "text": f"第{n}题(录音题)"} for n in range(11, 16)]
    rows += [{"kind": "subsection", "text": "第二节"}]
    rows += [{"kind": "card", "number": n, "card": "录音题", "text": f"第{n}题(录音题)"} for n in range(16, 21)]
    structure = analyze_outline_rows(rows)
    targets = [structure.subsection_target("听后应答", name) for name in ("第一节", "第二节")]
    owner = _response_owner(structure, dict(zip(targets, cards)))
    assert owner._fill_split_response_questions(questions) == {"录音题": 10}
    owner._upload_image.assert_called_once_with(image, 0, scope="scope-first-4")
    assert owner._fill_recording_card.call_count == 10
    assert owner._fill_recording_card.call_args_list[0].kwargs["prompt"] == "★ History. ★ Biology. ★ Art."
    assert owner._fill_recording_card.call_args_list[5].kwargs["prompt"] is None
    owner._replace_rich_text.assert_any_call(
        "guidance-scope-second-0-题目指导文字", "Q: Which subject?", field="听后应答第16题屏幕问题"
    )
    owner._clear_audio_in_scope.assert_any_call(
        "guidance-scope-second-0", "题目指导文字音频", "听后应答第16题"
    )
    owner._read_outline_structure.return_value = None
    with pytest.raises(PlatformInputUiError, match="分节导航"):
        owner._fill_split_response_questions(questions)


def test_response_dispatches_one_panel_when_paper_outline_has_ten_ordered_cards(tmp_path):
    audio, image = _assets(tmp_path)
    questions = normalise_response_group({"questions": _response_facts(audio, image)}, 0, base_dir=tmp_path)["questions"]
    rows = [{"kind": "part", "text": "二、听后应答"}]
    rows.extend({"kind": "card", "number": n, "card": "录音题", "text": f"第{n}题(录音题)"}
                for n in range(11, 21))
    structure = analyze_outline_rows(rows)
    cards = [f"card-{i}" for i in range(10)]
    owner = _response_owner(structure, {None: cards})
    assert owner._fill_split_response_questions(questions) == {"录音题": 10}
    assert owner._activate_group_section.call_count == 2
    owner._activate_group_section.assert_called_with("听后应答", "录音题", 10, 0, outline_target=None)
    owner._upload_image.assert_called_once_with(image, 0, scope="scope-card-4")
    assert owner._upload_audio_in_scope.call_count == 10
    assert all(call.kwargs["label"] == "原文音频" for call in owner._upload_audio_in_scope.call_args_list)
    assert all("prompt_audio_path" not in call.kwargs for call in owner._fill_recording_card.call_args_list)
    assert owner._fill_recording_card.call_args_list[4].kwargs["prompt"] == "★ History. ★ Biology. ★ Art."
    assert owner._fill_recording_card.call_args_list[5].kwargs["prompt"] is None

    rows[-1]["number"] = 21
    rows[-1]["text"] = "第21题(录音题)"
    owner._read_outline_structure.return_value = analyze_outline_rows(rows)
    with pytest.raises(PlatformInputUiError, match="题卡顺序"):
        owner._fill_split_response_questions(questions)


def test_picture_only_response_keeps_choices_without_source_text_or_audio(tmp_path):
    audio, image = _assets(tmp_path)
    questions = normalise_response_group({"questions": _response_facts(audio, image)}, 0, base_dir=tmp_path)["questions"]
    rows = [{"kind": "part", "text": "二、听后应答"}]
    rows += [{"kind": "card", "number": n, "card": "录音题", "text": f"第{n}题(录音题)"} for n in range(11, 21)]
    cards = [f"card-{i}" for i in range(10)]
    owner = _response_owner(analyze_outline_rows(rows), {None: cards})
    owner._labeled_text_editor_in_scope.side_effect = lambda scope, label: (
        None if (scope, label) == ("scope-card-4", "听力原文") else f"{scope}-{label}"
    )
    owner._audio_region_in_scope.side_effect = lambda scope, label: (
        None if (scope, label) == ("scope-card-4", "原文音频") else object()
    )

    assert owner._fill_split_response_questions(questions) == {"录音题": 10}
    assert owner._upload_audio_in_scope.call_count == 9
    assert all(call.args[0] != "scope-card-4" for call in owner._upload_audio_in_scope.call_args_list)
    assert owner._fill_recording_card.call_args_list[4].kwargs["prompt"] == "★ History. ★ Biology. ★ Art."
    assert "prompt_audio_path" not in owner._fill_recording_card.call_args_list[4].kwargs
    owner._upload_image.assert_called_once_with(image, 0, scope="scope-card-4")


def test_response_without_picture_cannot_skip_both_source_fields(tmp_path):
    audio, image = _assets(tmp_path)
    questions = normalise_response_group({"questions": _response_facts(audio, image)}, 0, base_dir=tmp_path)["questions"]
    rows = [{"kind": "part", "text": "二、听后应答"}]
    rows += [{"kind": "card", "number": n, "card": "录音题", "text": f"第{n}题(录音题)"} for n in range(11, 21)]
    owner = _response_owner(analyze_outline_rows(rows), {None: [f"card-{i}" for i in range(10)]})
    owner._labeled_text_editor_in_scope.side_effect = lambda scope, label: (
        None if (scope, label) == ("scope-card-3", "听力原文") else f"{scope}-{label}"
    )
    owner._audio_region_in_scope.side_effect = lambda scope, label: (
        None if (scope, label) == ("scope-card-3", "原文音频") else object()
    )

    with pytest.raises(PlatformInputUiError, match="第14题缺少“听力原文”"):
        owner._fill_split_response_questions(questions)
    owner._fill_recording_card.assert_not_called()
    owner._upload_audio_in_scope.assert_not_called()


@pytest.mark.parametrize("missing_label", ["听力原文", "原文音频"])
def test_response_requires_original_fields_even_when_stem_fields_exist(tmp_path, missing_label):
    audio, image = _assets(tmp_path)
    questions = normalise_response_group({"questions": _response_facts(audio, image)}, 0, base_dir=tmp_path)["questions"]
    rows = [{"kind": "part", "text": "二、听后应答"}]
    rows += [{"kind": "card", "number": n, "card": "录音题", "text": f"第{n}题(录音题)"} for n in range(11, 21)]
    cards = [f"card-{i}" for i in range(10)]
    owner = _response_owner(analyze_outline_rows(rows), {None: cards})
    owner._labeled_text_editor_in_scope.side_effect = lambda scope, label: (
        None if (scope, label) == ("scope-card-4", missing_label) else f"{scope}-{label}"
    )
    owner._audio_region_in_scope.side_effect = lambda scope, label: (
        None if (scope, label) == ("scope-card-4", missing_label) else object()
    )
    with pytest.raises(PlatformInputUiError, match=f"第15题缺少“{missing_label}”"):
        owner._fill_split_response_questions(questions)
    owner._replace_rich_text.assert_not_called()
    owner._upload_audio_in_scope.assert_not_called()
    owner._fill_recording_card.assert_not_called()


def test_response_screen_question_guidance_must_be_unique():
    owner = PlatformInputResponseContentMixin()
    first, second, third = Mock(), Mock(), Mock()
    editor_1, editor_2, editor_3 = Mock(), Mock(), Mock()
    editor_1.inner_text.return_value = "请根据屏幕上的问题作答。"
    editor_2.inner_text.return_value = "Q: Where are the toy cars produced?"
    editor_3.inner_text.return_value = "其他指导文字"
    candidates = Mock()
    candidates.count.return_value = 3
    candidates.nth.side_effect = [first, second, third]
    scope = Mock()
    scope.locator.return_value = candidates
    owner._labeled_text_editor_in_scope = Mock(side_effect=[editor_1, editor_2, editor_3])
    assert owner._response_screen_question_scope(scope, "第16题") is second
    editor_3.inner_text.return_value = "Q：Another question?"
    candidates.nth.side_effect = [first, second, third]
    owner._labeled_text_editor_in_scope.side_effect = [editor_1, editor_2, editor_3]
    with pytest.raises(PlatformInputUiError, match="无法唯一定位"):
        owner._response_screen_question_scope(scope, "第16题")


def test_bundle_preflights_response_before_editing_earlier_question_types():
    groups = ({"type": "听后选择", "materials": ()}, {"type": "听后应答", "questions": (
        {"section": "第一节"}, {"section": "第二节"},
    )})
    owner = SimpleNamespace(
        spec=SimpleNamespace(groups=groups),
        _group_requested_card_counts=Mock(return_value={"选择题": 1, "录音题": 2}),
        _read_outline_structure=Mock(return_value=SimpleNamespace(counts=lambda: {"选择题": 1, "录音题": 2})),
        _preflight_split_response_questions=Mock(side_effect=PlatformInputUiError("图片位置不明确")),
        _fill_selection_groups=Mock(),
    )
    with pytest.raises(PlatformInputUiError, match="图片位置不明确"):
        PlatformInputContentMixin._fill_grouped_content(owner)
    owner._fill_selection_groups.assert_not_called()


def test_retelling_fills_source_diagram_and_recording_card(tmp_path):
    audio, image = _assets(tmp_path)
    group = normalise_listening_retelling_group({"recording": {"listening_text": "IT.",
        "audio_path": audio, "image_path": image}, "retelling": {"prompt": "John says:",
        "score": 5, "answer_time": 90, "reference_answers": ["He likes IT.", "IT is his favourite."]}}, 0, base_dir=tmp_path)
    owner = SimpleNamespace(_activate_group_section=Mock(return_value=(["card"], ["editor"])),
        _question_page_scope=Mock(return_value="own-page"), _next_listening_editor=lambda editors, *_: editors[0],
        _replace_rich_text=Mock(), _upload_original_audio=Mock(), _upload_image=Mock(), _fill_recording_card=Mock())
    assert PlatformInputRecordContentMixin._fill_listening_retelling_groups(owner, {"听后转述": [group]}) == {"录音题": 1}
    owner._upload_image.assert_called_once_with(image, 0, scope="own-page")
    assert owner._fill_recording_card.call_args.args[1]["reference_answers"] == ("He likes IT.", "IT is his favourite.")


def test_local_numbered_response_subsections_select_correct_occurrence():
    rows = [{"kind": "part", "text": "一、听后应答"},
            {"kind": "subsection", "text": "第一节"}]
    rows += [{"kind": "card", "number": n, "card": "录音题", "text": f"第{n}题(录音题)"} for n in range(1, 6)]
    rows += [{"kind": "subsection", "text": "第二节"}]
    rows += [{"kind": "card", "number": n, "card": "录音题", "text": f"第{n}题(录音题)"} for n in range(1, 6)]
    structure = analyze_outline_rows(rows)
    assert structure.subsection_target("听后应答", "第一节") == ("第1题(录音题)", 0)
    assert structure.subsection_target("听后应答", "第二节") == ("第1题(录音题)", 1)


def test_catalog_treats_string_zero_as_disabled():
    result = normalize_platform_template_catalog([
        {"id": "1", "name": "听后应答-新题型", "status": "0"},
        {"id": "2", "name": "听后选择-新题型", "status": "1"},
    ], [])
    assert [item["enabled"] for item in result["records"]] == [False, True]
