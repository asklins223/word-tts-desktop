"""ParseCandidate 裁决层（方案 5.2.1）。

固定规则：

1. 显式题型标记（文件名/内容检测得到的 doc_type）优先于自动检测；
2. 同一结构块（claimed_block）只能有一个题型规则成为 owner；
3. 多个规则同时命中且无法按优先级唯一裁决时，保留多个候选并写入
   AMBIGUOUS，冲突块的实体不发布，不能把两份结果都发布为小题；
4. 去重不依赖最终文本、也不依赖 category+sequence：实体身份由
   ``(source_key, 结构定位)`` 决定，跨候选出现同一实体 id 视为冲突。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import ParseCandidate


@dataclass(frozen=True)
class BlockConflict:
    """同一结构块被多个候选声明的冲突记录。"""

    block: str
    winner_type_code: str | None          # 显式标记裁决出的 owner；None 表示未裁决
    loser_candidate_ids: tuple[str, ...]  # 未获得 owner 资格的候选

    def to_dict(self) -> dict:
        return {
            "block": self.block,
            "winner_type_code": self.winner_type_code,
            "loser_candidate_ids": list(self.loser_candidate_ids),
        }


@dataclass(frozen=True)
class AdjudicatedParse:
    """裁决结果：所有候选原样保留，只有无冲突实体进入发布集合。"""

    candidates: tuple[ParseCandidate, ...]
    entities: tuple = ()                  # 已裁决可发布的实体（Stimulus/QuestionItem/ContentUnit）
    conflicts: tuple[BlockConflict, ...] = ()
    ambiguous: bool = False
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "entities": [e.to_dict() for e in self.entities],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "ambiguous": self.ambiguous,
            "diagnostics": list(self.diagnostics),
        }


def _entity_id(entity) -> str:
    if hasattr(entity, "question_id"):
        return entity.question_id
    if hasattr(entity, "stimulus_id"):
        return entity.stimulus_id
    return entity.content_unit_id


def adjudicate(candidates, explicit_type_code: str | None = None) -> AdjudicatedParse:
    """对同一文档的多个题型候选做 owner 裁决。

    ``explicit_type_code`` 是显式题型标记（检测链路的 doc_type）；
    候选顺序保持输入顺序，发布实体按候选顺序稳定排列。
    """
    # 同一候选重复输入先去重（保持首次出现顺序）
    deduped: list[ParseCandidate] = []
    seen_ids = set()
    for candidate in candidates:
        if candidate.candidate_id in seen_ids:
            continue
        seen_ids.add(candidate.candidate_id)
        deduped.append(candidate)
    candidates = deduped

    # 结构块 → 声明它的候选列表
    claims: dict[str, list[ParseCandidate]] = {}
    for candidate in candidates:
        for block in candidate.claimed_blocks:
            claims.setdefault(block, []).append(candidate)

    conflicts: list[BlockConflict] = []
    diagnostics: list[str] = []
    ambiguous = False
    rejected_ids: set[str] = set()

    def contested(block: str) -> bool:
        """该块有多个声明者且显式标记无法唯一裁决。"""
        claimants = claims.get(block, [])
        if len(claimants) <= 1:
            return False
        if explicit_type_code and any(
            c.type_code == explicit_type_code for c in claimants
        ):
            return False
        return True

    for block, claimants in claims.items():
        if len(claimants) == 1:
            continue
        distinct_types = {c.type_code for c in claimants}
        if explicit_type_code and explicit_type_code in distinct_types:
            losers = tuple(
                c.candidate_id for c in claimants if c.type_code != explicit_type_code
            )
            rejected_ids.update(losers)
            conflicts.append(BlockConflict(
                block=block, winner_type_code=explicit_type_code,
                loser_candidate_ids=losers,
            ))
            for c in claimants:
                if c.type_code != explicit_type_code:
                    diagnostics.append(
                        f"superseded_by_explicit_type:{block}:{c.type_code}")
        else:
            # 无法唯一裁决：保留候选，冲突块实体不发布
            ambiguous = True
            conflicts.append(BlockConflict(
                block=block, winner_type_code=None,
                loser_candidate_ids=tuple(c.candidate_id for c in claimants),
            ))
            diagnostics.append(f"block_owner_conflict:{block}")

    # 发布：被显式标记淘汰的候选整体出局；存在未裁决冲突块的候选，
    # 只发布无冲突块上的实体（三类实体都带 source_locator）
    published = []
    seen_entity_ids: dict[str, str] = {}
    for candidate in candidates:
        if candidate.candidate_id in rejected_ids:
            continue
        for entity in candidate.entities:
            if contested(entity.source_locator):
                continue
            _publish(entity, seen_entity_ids, published, diagnostics)

    return AdjudicatedParse(
        candidates=tuple(candidates),
        entities=tuple(published),
        conflicts=tuple(conflicts),
        ambiguous=ambiguous,
        diagnostics=tuple(diagnostics),
    )


def _publish(entity, seen_entity_ids: dict, published: list, diagnostics: list):
    entity_id = _entity_id(entity)
    if entity_id in seen_entity_ids:
        diagnostics.append(f"duplicate_entity_id:{entity_id}")
        return
    seen_entity_ids[entity_id] = entity_id
    published.append(entity)
