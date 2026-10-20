from __future__ import annotations

import pytest
from types import SimpleNamespace

from workflow.artifact_store import ArtifactStore
from workflow.audio_asr import AudioImportCoordinator, _ImportBatch, _item_candidates
from workflow.audio_text_matching import compare_transcript, text_profile


@pytest.mark.parametrize("source,observed", [
    ("Where is Mr White from?", "Where is mister Wite from?"),
    ("I don't think he's ready.", "I do not think he is ready."),
    ("At 7:00 a.m. a robot cooks breakfast.", "At seven AM a robot cooks breakfast."),
    ("She left at 7:30.", "She left at seven thirty."),
    ("你有六十秒钟的时间准备。", "你有60秒钟的时间准备。"),
    ("I visited the countryside last August.", "I visited the country side last Augest."),
    ("Peter visited Yangshuo last August.", "Peter visited 杨朔 last August."),
    ("Peter visited Beijing last August.", "Peter visited 北京 last August."),
    ("你的复述可以这样开始：Peter visited Yangshuo ...", "你的复述可以这样开始。Peter visited 杨ang朔。"),
    ("你将听到一篇短文，录音播放两遍。", "你将听到一篇短文，路音播放两遍。"),
])
def test_small_asr_errors_can_match_without_changing_the_transcript(source, observed):
    match = compare_transcript(text_profile(source), text_profile(observed))
    assert match.compatible
    assert match.complete
    assert match.score >= 0.82


@pytest.mark.parametrize("ellipsis", ["...", "……", "…", "", " . . . "])
def test_retelling_ellipsis_does_not_require_any_spoken_or_recognized_punctuation(ellipsis):
    content = "下面你有60秒钟的时间准备，你的复述可以这样开始：Peter visited Yangshuo"
    match = compare_transcript(text_profile(content + ellipsis), text_profile(content))
    assert match.score == 1.0
    assert match.complete


@pytest.mark.parametrize("source,observed", [
    ("下面你有60秒钟的时间准备。", "下面你有十五秒钟的时间准备。"),
    ("Please wait fifteen seconds before answering.", "Please wait fifty seconds before answering."),
    ("The price is 1.5 dollars.", "The price is 15 dollars."),
    ("The car can drive by itself.", "The car cannot drive by itself."),
    ("How does the man feel?", "Why does the man feel?"),
    ("你有60秒钟的时间准备。", "你没有60秒钟的时间准备。"),
])
def test_critical_content_changes_are_not_automatically_accepted(source, observed):
    match = compare_transcript(text_profile(source), text_profile(observed))
    assert not match.compatible
    assert not match.complete


def test_missing_ending_is_not_a_typo_even_after_a_long_matching_prefix():
    body = "Peter visited Yangshuo last August. He took a boat down the river and rode a bike in the countryside."
    match = compare_transcript(text_profile(body + " What an unforgettable trip!"), text_profile(body))
    assert not match.complete


def test_identical_spelling_outranks_a_pronunciation_only_candidate():
    candidates = _item_candidates("Peter visited Yangshuo last August.", [
        {"item_id": "chinese", "sequence": 0, "normalized_content": "Peter visited 阳朔 last August."},
        {"item_id": "english", "sequence": 1, "normalized_content": "Peter visited Yangshuo last August."},
    ], full=True)
    assert candidates[0]["item_id"] == "english"
    assert candidates[0]["score"] > candidates[1]["score"]


def test_a_full_recording_cannot_use_a_prefix_match_for_missing_content():
    candidates = _item_candidates("Peter visited Yangshuo.", [
        {"item_id": "passage", "sequence": 0, "normalized_content": "Peter visited Yangshuo. He took a boat down the river and rode a bike."},
    ], full=True)
    assert not candidates[0]["complete"]


def test_opening_recognition_allows_unheard_later_numbers():
    candidates = _item_candidates("One night I had a dream.", [
        {"item_id": "passage", "sequence": 0, "normalized_content": "One night I had a dream. At 7 AM a robot cooked breakfast."},
    ])
    assert candidates[0]["compatible"]
    assert candidates[0]["score"] == 1.0


@pytest.mark.parametrize("transcript", ["", "...", "<|en|><|NEUTRAL|>"])
def test_empty_recognition_is_never_an_exact_empty_prefix(transcript):
    candidates = _item_candidates(transcript, [
        {"item_id": "passage", "sequence": 0, "normalized_content": "Peter visited Yangshuo."},
    ])
    assert candidates[0]["score"] == 0.0


def _analyze_files(tmp_path, contents, transcripts):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    coordinator = AudioImportCoordinator(object(), artifacts)
    items = [
        {"item_id": str(index), "sequence": index, "normalized_content": content}
        for index, content in enumerate(contents)
    ]
    batch = _ImportBatch("batch", "workflow", [
        {"file_name": f"{index}.mp3", "extension": ".mp3", "size_bytes": 10}
        for index in range(len(items))
    ], items)
    batch.staged = {index: SimpleNamespace(path=f"{index}.mp3") for index in range(len(items))}
    coordinator._batches["batch"] = batch
    extension = SimpleNamespace(
        create_recognizer=lambda: object(),
        recognize=lambda paths, **_kwargs: [
            {"text": transcripts[int(str(path).split(".")[0])], "scope": "full", "error": ""}
            for path in paths
        ],
    )
    return coordinator.analyze("workflow", "batch", extension)


def test_file_import_uses_the_same_tolerance_and_number_checks(tmp_path):
    result = _analyze_files(tmp_path, [
        "Where is Mr White from?", "你有15秒钟的时间准备。",
    ], [
        "Where is mister Wite from?", "你有十五秒钟的时间准备。",
    ])
    assert result["can_commit"]
    assert [row["suggested_item_id"] for row in result["files"]] == ["0", "1"]


def test_file_import_with_a_changed_number_requires_review(tmp_path):
    result = _analyze_files(tmp_path, ["你有60秒钟的时间准备。"], ["你有十五秒钟的时间准备。"])
    assert not result["can_commit"]
    assert result["files"][0]["match_status"] == "needs_review"
    assert result["files"][0]["suggested_item_id"] is None


def test_complete_file_recognition_cannot_commit_a_truncated_passage(tmp_path):
    result = _analyze_files(tmp_path, [
        "Peter visited Yangshuo. He took a boat down the river and rode a bike.",
    ], ["Peter visited Yangshuo."])
    assert not result["can_commit"]
    assert result["files"][0]["suggested_item_id"] is None
