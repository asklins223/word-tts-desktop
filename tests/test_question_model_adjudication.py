"""候选裁决层测试（方案 5.2.1 固定规则）。"""

import pytest

from question_model import (
    ParseCandidate,
    adjudicate,
    extract_candidate,
)


def make_info_candidate(source_key="docA"):
    result = {
        "doc_type": "信息获取",
        "items": [
            {"category": "听选信息题目", "number": 1, "filename_stem": "问题1",
             "voice": "male", "text": "M: q1"},
            {"category": "听选信息录音稿", "index": 1, "text": "M: script one"},
            {"category": "听选信息题目", "number": 2, "filename_stem": "问题2",
             "voice": "female", "text": "W: q2"},
            {"category": "听选信息录音稿", "index": 2, "text": "W: script two"},
        ],
    }
    return extract_candidate("信息获取", result, source_key)


def competing_candidate(blocks, entities, type_code="listening_choice",
                        source_key="docA"):
    """模拟另一个题型规则对相同结构块的声明。"""
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(blocks),
        entities=tuple(entities),
        confidence=0.95,
        diagnostics=(),
        capabilities={"question_fields_complete": False, "audio_only": True},
    )


class TestNoConflict:
    def test_disjoint_candidates_both_own(self):
        c1 = make_info_candidate("docA")
        c2 = competing_candidate(("模仿朗读/朗读1",), (), type_code="imitation_reading",
                                 source_key="docA")
        result = adjudicate([c1, c2], explicit_type_code="info_acquisition")
        assert not result.ambiguous
        assert result.conflicts == ()
        assert len(result.entities) == len(c1.entities)

    def test_duplicate_candidate_id_deduped(self):
        c1 = make_info_candidate("docA")
        result = adjudicate([c1, c1])
        assert len(result.candidates) == 1
        assert len(result.entities) == len(c1.entities)


class TestExplicitMarkerPriority:
    def test_explicit_type_wins_overlapping_blocks(self):
        """显式题型标记优先：冲突块归显式题型，其他候选整体出局。"""
        c1 = make_info_candidate("docA")
        c2 = competing_candidate(c1.claimed_blocks, c1.entities)
        result = adjudicate([c1, c2], explicit_type_code="info_acquisition")
        assert not result.ambiguous
        assert len(result.conflicts) == len(c1.claimed_blocks)
        assert all(conf.winner_type_code == "info_acquisition"
                   for conf in result.conflicts)
        assert c2.candidate_id in result.conflicts[0].loser_candidate_ids
        assert len(result.entities) == len(c1.entities)
        assert any(d.startswith("superseded_by_explicit_type:")
                   for d in result.diagnostics)


class TestAmbiguousBlocks:
    def test_unresolved_conflict_publishes_nothing_for_block(self):
        """无法唯一裁决：候选保留、冲突块实体不发布、整体 AMBIGUOUS。"""
        c1 = make_info_candidate("docA")
        stim = next(e for e in c1.entities if hasattr(e, "stimulus_type"))
        c2 = competing_candidate((stim.source_locator,), (stim,))
        result = adjudicate([c1, c2])
        assert result.ambiguous
        published_locators = {e.source_locator for e in result.entities}
        assert stim.source_locator not in published_locators
        # 无冲突的题目实体仍然发布
        assert len(result.entities) == len(c1.entities) - 1
        assert any(conf.winner_type_code is None for conf in result.conflicts)
        assert any(d.startswith("block_owner_conflict:")
                   for d in result.diagnostics)

    def test_same_entity_id_across_candidates_flagged(self):
        """去重不靠文本：不同候选产出同一实体 id 必须写诊断且只发布一份。"""
        c1 = make_info_candidate("docA")
        c2 = competing_candidate(("模仿朗读/朗读1",), c1.entities,
                                 type_code="imitation_reading")
        result = adjudicate([c1, c2])
        assert not result.ambiguous   # 结构块不重叠
        assert len(result.entities) == len(c1.entities)
        assert any(d.startswith("duplicate_entity_id:") for d in result.diagnostics)


class TestEndToEnd:
    def test_real_extractor_candidates_adjudicate(self):
        """真实抽取器产出 + 裁决：结构块不重叠的多题型文档各自发布。"""
        c_info = make_info_candidate()
        c_selection = extract_candidate(
            "听后选择",
            {"items": [{"category": "听后选择录音稿", "index": 1,
                        "question_index": 1, "text": "W: hello"}]},
            "docA",
        )
        result = adjudicate([c_info, c_selection],
                            explicit_type_code="info_acquisition")
        assert not result.ambiguous
        assert result.conflicts == ()
        locators = {e.source_locator for e in result.entities}
        assert any(l.startswith("听选信息") for l in locators)
        assert any(l.startswith("听后选择") for l in locators)

    def test_real_extractor_overlap_resolved_by_explicit_marker(self):
        """真实候选声明同一结构块时，显式题型标记决定 owner。"""
        c_info = make_info_candidate()
        c_fake = competing_candidate(c_info.claimed_blocks, c_info.entities,
                                     type_code="listening_choice")
        result = adjudicate([c_info, c_fake],
                            explicit_type_code="info_acquisition")
        assert not result.ambiguous
        assert len(result.entities) == len(c_info.entities)
        assert any(conf.winner_type_code == "info_acquisition"
                   for conf in result.conflicts)
