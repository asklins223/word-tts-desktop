"""原子小题抽取器测试：直接以解析基线快照为 fixture。

基线目录 examples/baselines/parse/20260829-pre-atomic-model 是改造前
当前解析规则的存档（tools/parse_baseline.py 生成），在这里同时充当
抽取规则的对照数据源。
"""

import json
import os

import pytest

from question_model import (
    QUESTION_TYPE_CODES,
    ParseCandidate,
    ResolutionState,
    extract_candidate,
)

BASELINE_DOC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "baselines", "parse", "20260829-pre-atomic-model", "docs",
)


def load_baseline_doc(stem):
    with open(os.path.join(BASELINE_DOC_DIR, f"{stem}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def extract_from_baseline(stem, result_index=0):
    doc = load_baseline_doc(stem)
    result = doc["parse_results"][result_index]
    source_key = os.path.splitext(doc["source_file"])[0]
    return extract_candidate(result["doc_type"], result, source_key), source_key


class TestInfoAcquisition:
    """方案 9.1 验收：一段材料多道小题、引用同一材料、无重复无遗漏。"""

    @pytest.fixture()
    def candidate(self):
        candidate, _ = extract_from_baseline("7上-U2-信息获取")
        return candidate

    def test_stimulus_and_question_counts(self, candidate):
        stimuli = [e for e in candidate.entities if not hasattr(e, "question_type")]
        questions = [e for e in candidate.entities if hasattr(e, "question_type")]
        assert len(stimuli) == 4   # 3 段听选对话 + 1 段回答问题独白
        assert len(questions) == 10

    def test_one_material_two_questions(self, candidate):
        """每段听选对话恰好被 2 道小题引用；独白被 4 道小题引用。"""
        stimuli = {e.stimulus_id: e for e in candidate.entities
                   if not hasattr(e, "question_type")}
        questions = [e for e in candidate.entities if hasattr(e, "question_type")]
        by_stimulus = {}
        for q in questions:
            assert q.stimulus_id in stimuli, f"{q.source_locator} 关联到不存在的材料"
            by_stimulus.setdefault(q.stimulus_id, []).append(q)
        assert sorted(len(v) for v in by_stimulus.values()) == [2, 2, 2, 4]

    def test_question_attaches_to_following_script(self, candidate):
        """文档排版是题目在前、录音稿在后：题目 N 关联到后续第一段录音稿。"""
        questions = {q.question_number: q for q in candidate.entities
                     if hasattr(q, "question_type")}
        assert questions[1].stimulus_id.endswith("听选信息录音稿-录音稿1")
        assert questions[3].stimulus_id.endswith("听选信息录音稿-录音稿2")
        assert questions[7].stimulus_id.endswith("回答问题录音稿-录音稿1")

    def test_question_numbers_continuous(self, candidate):
        numbers = sorted(q.question_number for q in candidate.entities
                         if hasattr(q, "question_type"))
        assert numbers == list(range(1, 11))
        assert candidate.diagnostics == ()   # 干净样例不应有任何诊断

    def test_audio_only_draft(self, candidate):
        """旧结果没有选项/答案：候选只能是 audio_only + DRAFT。"""
        assert candidate.capabilities["audio_only"] is True
        assert candidate.capabilities["question_fields_complete"] is False
        assert candidate.audio_only
        for entity in candidate.entities:
            assert entity.resolution_state is ResolutionState.DRAFT
            assert entity.to_dict()["resolution_state"] == "DRAFT"

    def test_type_code_stable(self, candidate):
        assert candidate.type_code == "info_acquisition"
        assert all(q.question_type == "info_acquisition"
                   for q in candidate.entities if hasattr(q, "question_type"))


class TestDeterminism:
    def test_reparse_produces_identical_candidate(self):
        """同一解析结果重复抽取必须得到逐字节相同的候选（方案 9.1）。"""
        first, _ = extract_from_baseline("7上-U2-信息获取")
        second, _ = extract_from_baseline("7上-U2-信息获取")
        assert first.to_dict() == second.to_dict()

    def test_content_hash_tracks_stem_change(self):
        doc = load_baseline_doc("7上-U2-信息获取")
        result = doc["parse_results"][0]
        source_key = os.path.splitext(doc["source_file"])[0]
        modified = json.loads(json.dumps(result, ensure_ascii=False))
        modified["items"][0]["text"] += "（改动）"
        c1 = extract_candidate(result["doc_type"], result, source_key)
        c2 = extract_candidate(result["doc_type"], modified, source_key)
        q1 = [e for e in c1.entities if hasattr(e, "question_type")][0]
        q2 = [e for e in c2.entities if hasattr(e, "question_type")][0]
        assert q1.content_hash != q2.content_hash
        assert q1.question_revision_id != q2.question_revision_id


class TestListeningSelection:
    @pytest.fixture()
    def candidate(self):
        candidate, _ = extract_from_baseline("听后选择-7上 Starter Unit 1 Hello!")
        return candidate

    def test_stimuli_only(self, candidate):
        assert candidate.type_code == "listening_choice"
        assert len(candidate.entities) == 6
        assert all(hasattr(e, "stimulus_type") for e in candidate.entities)

    def test_audio_only_with_diagnostics(self, candidate):
        """听后选择当前只解析录音稿：必须带诊断，不得伪造成完整小题。"""
        assert candidate.audio_only
        assert "listening_choice_stem_options_answer_not_extracted" in candidate.diagnostics
        assert candidate.confidence < 0.95


class TestGuardRails:
    def test_unregistered_type_rejected(self):
        with pytest.raises(KeyError):
            extract_candidate("课文跟读", {"items": []}, "doc")

    def test_invalid_type_code_rejected(self):
        from question_model import QuestionItem
        with pytest.raises(ValueError):
            QuestionItem(
                question_id="question:x:1",
                question_type="不存在题型",
                stem="s",
                source_locator="l",
            )

    def test_trailing_question_without_stimulus_flagged(self):
        """没有关联到录音稿的小题必须保留并写诊断，不允许静默丢弃。"""
        result = {
            "doc_type": "信息获取",
            "items": [
                {"category": "听选信息题目", "number": 1, "filename_stem": "问题1",
                 "voice": "male", "text": "M: hello?"},
            ],
        }
        candidate = extract_candidate("信息获取", result, "synthetic")
        questions = [e for e in candidate.entities if hasattr(e, "question_type")]
        assert len(questions) == 1
        assert questions[0].stimulus_id is None
        assert "question_without_stimulus" in candidate.diagnostics

    def test_question_number_gap_flagged(self):
        result = {
            "doc_type": "信息获取",
            "items": [
                {"category": "听选信息题目", "number": 1, "filename_stem": "问题1",
                 "voice": "male", "text": "M: a"},
                {"category": "听选信息录音稿", "index": 1, "text": "M: script"},
                {"category": "听选信息题目", "number": 3, "filename_stem": "问题3",
                 "voice": "female", "text": "W: c"},
                {"category": "听选信息录音稿", "index": 2, "text": "W: script2"},
            ],
        }
        candidate = extract_candidate("信息获取", result, "synthetic")
        assert "question_number_gap" in candidate.diagnostics

    def test_type_code_table_covers_registry(self):
        """稳定题型代码必须覆盖 question_types 注册表的全部题型。"""
        from question_types import QUESTION_TYPES
        registered = {qt.key for qt in QUESTION_TYPES}
        assert registered == set(QUESTION_TYPE_CODES)
