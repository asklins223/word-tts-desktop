"""原子小题抽取器测试：直接以解析基线快照为 fixture。

基线目录 examples/baselines/parse/20260829-pre-atomic-model 是改造前
当前解析规则的存档（tools/parse_baseline.py 生成），在这里同时充当
抽取规则的对照数据源。
"""

import json
import os

import pytest

from question_model import (
    EXTRACTORS,
    FAMILY_SUB_TYPES,
    QUESTION_TYPE_CODES,
    SUB_TYPE_REGISTRY,
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

    def test_sub_type_split(self, candidate):
        """信息获取按小题型产出：听选信息 6 题 + 回答问题 4 题。"""
        assert candidate.type_code == "info_acquisition"
        questions = [q for q in candidate.entities if hasattr(q, "question_type")]
        listening = [q for q in questions if q.question_type == "listening_info"]
        answering = [q for q in questions if q.question_type == "answer_question"]
        assert len(listening) == 6
        assert len(answering) == 4
        assert all(q.major_type == "信息获取" for q in questions)
        assert all(q.type_family == "info_acquisition" for q in questions)

    def test_stimulus_sub_types(self, candidate):
        stimuli = [e for e in candidate.entities if hasattr(e, "stimulus_type")]
        assert {s.sub_type_code for s in stimuli} == {
            "listening_info", "answer_question"}


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
        assert all(e.sub_type_code == "listening_choice"
                   for e in candidate.entities)

    def test_audio_only_with_diagnostics(self, candidate):
        """听后选择当前只解析录音稿：必须带诊断，不得伪造成完整小题。"""
        assert candidate.audio_only
        assert "listening_choice_stem_options_answer_not_extracted" in candidate.diagnostics
        assert candidate.confidence < 0.95


class TestListeningResponse:
    @pytest.fixture()
    def candidate(self):
        candidate, _ = extract_from_baseline("S1-听后应答")
        return candidate

    def test_one_question_item_per_prompt_sentence(self, candidate):
        """方案 4 节映射表：每个待应答句一条 QuestionItem。"""
        assert candidate.type_code == "listening_response"
        assert len(candidate.entities) == 7
        assert all(q.question_type == "listening_response"
                   for q in candidate.entities)
        numbers = sorted(q.question_number for q in candidate.entities)
        assert numbers == list(range(1, 8))

    def test_audio_only_draft(self, candidate):
        assert candidate.audio_only
        assert "listening_response_expected_answer_not_extracted" in candidate.diagnostics
        assert all(e.resolution_state is ResolutionState.DRAFT
                   for e in candidate.entities)


class TestInfoRetelling:
    def test_stimulus_only_with_diagnostics(self):
        """当前只解析出整段录音稿：只出 Stimulus，任务未拆分必须写诊断。"""
        candidate, _ = extract_from_baseline("信息转述及询问信息 7上- U1")
        assert candidate.type_code == "info_retelling"
        assert len(candidate.entities) == 1
        assert all(e.sub_type_code == "info_retelling"
                   for e in candidate.entities)
        assert candidate.audio_only
        assert "info_retelling_task_split_not_extracted" in candidate.diagnostics


class TestImitationReading:
    @pytest.fixture()
    def candidate(self):
        candidate, _ = extract_from_baseline("模仿朗读-7上-U5-U6")
        return candidate

    def test_one_passage_stimulus_per_item(self, candidate):
        assert candidate.type_code == "imitation_reading"
        assert len(candidate.entities) == 6
        assert all(e.sub_type_code == "imitation_reading"
                   for e in candidate.entities)
        assert all(e.stimulus_type == "reading_passage"
                   for e in candidate.entities)

    def test_material_source_is_attribute_not_sub_type(self, candidate):
        """外网/教材是来源属性：模仿朗读本身就是最小题型。"""
        sources = {e.material_source for e in candidate.entities}
        assert sources == {"外网", "教材"}

    def test_locator_carries_source_and_unit(self, candidate):
        """外网/教材来源与单元号进入定位，4 外网 + 2 教材。"""
        locators = [e.source_locator for e in candidate.entities]
        assert sum("模仿朗读-外网" in l for l in locators) == 4
        assert sum("模仿朗读-教材" in l for l in locators) == 2
        assert any("朗读1" in l for l in locators)
        assert len(set(e.stimulus_id for e in candidate.entities)) == 6


class TestTextReading:
    @pytest.fixture()
    def candidate(self):
        candidate, _ = extract_from_baseline("课文跟读-G7-1")
        return candidate

    def test_content_units_not_questions(self, candidate):
        """课文跟读是学习内容：必须映射为 ContentUnit，不伪装成考试小题。"""
        assert candidate.type_code == "text_reading"
        assert len(candidate.entities) == 82
        kinds = [e.content_kind for e in candidate.entities]
        assert kinds.count("text_reading_sentence") == 29
        assert kinds.count("text_reading_paragraph") == 17
        assert kinds.count("text_reading_discourse") == 36
        assert all(e.unit_kind == "LEARNING_CONTENT" for e in candidate.entities)

    def test_structural_numbers_preserved(self, candidate):
        discourses = [e for e in candidate.entities
                      if e.content_kind == "discourse_reading"]
        assert all(e.discourse_number is not None for e in discourses)
        assert all(e.sentence_number is not None for e in discourses)

    def test_unique_ids_and_determinism(self, candidate):
        assert len({e.content_unit_id for e in candidate.entities}) == 82
        again, _ = extract_from_baseline("课文跟读-G7-1")
        assert again.to_dict() == candidate.to_dict()

    def test_missing_sentence_number_uses_ordinal(self):
        """7上-Starter 的段落条目 sentence_number=None：按条目顺序定位且 id 唯一。"""
        candidate, _ = extract_from_baseline("课文跟读-7上-Starter Unit 1 Hello")
        assert len(candidate.entities) == 37
        assert len({e.content_unit_id for e in candidate.entities}) == 37
        assert all("条目" in e.source_locator
                   for e in candidate.entities
                   if e.content_kind == "paragraph_reading")


class TestVocabulary:
    @pytest.fixture()
    def candidate(self):
        candidate, _ = extract_from_baseline("U6单词导入模板")
        return candidate

    def test_words_and_example_sentences(self, candidate):
        assert candidate.type_code == "vocabulary"
        assert len(candidate.entities) == 80
        assert all(e.content_kind == "vocabulary" for e in candidate.entities)
        entry_kinds = [e.entry_kind for e in candidate.entities]
        assert entry_kinds.count("word") == 40
        assert entry_kinds.count("example_sentence") == 40
        assert all(e.unit_kind == "LEARNING_CONTENT" for e in candidate.entities)

    def test_entry_numbers_preserved(self, candidate):
        words = [e for e in candidate.entities if e.entry_kind == "word"]
        assert sorted(e.entry_number for e in words) == list(range(1, 41))
        assert len({e.content_unit_id for e in candidate.entities}) == 80


class TestRegistryAlignment:
    def test_extractors_cover_all_registered_types(self):
        """全部注册大题型必须接入原子小题抽取。"""
        from question_types import QUESTION_TYPES
        registered = {qt.key for qt in QUESTION_TYPES}
        assert registered == set(QUESTION_TYPE_CODES)
        assert registered == set(EXTRACTORS.keys())

    def test_family_sub_types_cover_all_families(self):
        """每个大题型至少有一个小题型；family 代码必须有效。"""
        assert set(FAMILY_SUB_TYPES) == set(QUESTION_TYPE_CODES.values())
        for family, sub_codes in FAMILY_SUB_TYPES.items():
            assert len(sub_codes) >= 1
            for code in sub_codes:
                assert SUB_TYPE_REGISTRY[code].family == family

    def test_leaf_families_have_single_self_sub_type(self):
        """模仿朗读/词汇/听后选择/听后应答本身就是最小题型。"""
        for family in ("imitation_reading", "vocabulary",
                       "listening_choice", "listening_response"):
            assert tuple(FAMILY_SUB_TYPES[family]) == (family,)

    def test_info_retelling_asking_info_reserved(self):
        """询问信息已注册但未接入：预留状态，不得产出实体。"""
        assert SUB_TYPE_REGISTRY["asking_info"].status == "reserved"
        import pytest
        from question_model import QuestionItem
        with pytest.raises(ValueError):
            QuestionItem(
                question_id="question:x:asking-1",
                question_type="asking_info",
                stem="Where is the library?",
                source_locator="询问信息/问题1",
            )

    def test_capability_matrix_on_registry(self):
        """能力声明挂在小题型：听选信息有选项，回答问题口头作答。"""
        assert SUB_TYPE_REGISTRY["listening_info"].has_options is True
        assert SUB_TYPE_REGISTRY["listening_info"].answer_kind == "single_choice"
        assert SUB_TYPE_REGISTRY["answer_question"].has_options is False
        assert SUB_TYPE_REGISTRY["answer_question"].answer_kind == "spoken_response"
        assert SUB_TYPE_REGISTRY["vocabulary"].voice_policy == "forced_female"


class TestGuardRails:
    def test_unregistered_type_rejected(self):
        with pytest.raises(KeyError):
            extract_candidate("不存在的题型", {"items": []}, "doc")

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
