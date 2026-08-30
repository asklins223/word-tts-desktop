"""文档 revision 间的小题身份匹配（方案 6.2.1 冻结算法 revmatch-v1）。

两阶段：

1. **确定性匹配**：``question_id`` 由 (来源键, 结构定位) 派生、跨版本稳定，
   同 id 比对内容哈希 → ``MATCHED``（同内容）/ ``CHANGED``（正文变化，
   保留逻辑身份产生新 revision）；只在旧版 → ``REMOVED``；只在新版 → ``NEW``。
2. **候选匹配**：对 ``NEW``/``REMOVED`` 按 (小题型, 内容哈希) 指纹配对；
   仅当一组内恰好一对一（全局唯一）才自动判定 ``MATCHED`` 并在
   ``candidates_json`` 记录旧新 id 映射；多个候选一律 ``AMBIGUOUS``，
   等待人工裁决，绝不按题号或文本相似强行配对。

所有决策写入 ``revision_match_decisions``：同一输入 + 算法版本幂等重放，
不会产生不同结果。决策只记录事实，不自动改写 ``question_items`` 身份。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

ALGORITHM_VERSION = "revmatch-v1"

DECISION_MATCHED = "MATCHED"
DECISION_NEW = "NEW"
DECISION_REMOVED = "REMOVED"
DECISION_CHANGED = "CHANGED"
DECISION_AMBIGUOUS = "AMBIGUOUS"




@dataclass
class MatchReport:
    source_document_id: str
    from_document_revision_id: str | None
    to_document_revision_id: str
    algorithm_version: str
    decisions: list[dict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.decisions:
            counts[d["decision"]] = counts.get(d["decision"], 0) + 1
        return counts

    def ambiguous_question_ids(self) -> list[str]:
        return [d["question_id"] for d in self.decisions
                if d["decision"] == DECISION_AMBIGUOUS]


def _load_revisions(conn, document_revision_id):
    """文档版本的小题集合：经由成员关系表（revision 行内容寻址、可复用）。"""
    rows = conn.execute(
        """
        SELECT qr.question_revision_id, qr.question_id, qr.sub_type_code,
               qr.content_hash
        FROM document_revision_members m
        JOIN question_revisions qr
          ON qr.question_revision_id = m.entity_revision_id
        WHERE m.document_revision_id = ? AND m.entity_kind = 'QUESTION'
        ORDER BY m.ordinal
        """,
        (document_revision_id,),
    ).fetchall()
    return {
        row[1]: {"question_revision_id": row[0], "sub_type_code": row[2],
                 "content_hash": row[3]}
        for row in rows
    }


def _record_decision(conn, report: MatchReport, *, question_id: str,
                     decision: str, from_revision_id: str | None,
                     to_revision_id: str | None, candidates: list[dict],
                     now: str) -> None:
    decision_id = (
        f"revmatch:{report.from_document_revision_id or 'origin'}:"
        f"{report.to_document_revision_id}:{question_id}:{report.algorithm_version}"
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO revision_match_decisions
            (decision_id, source_document_id, from_document_revision_id,
             to_document_revision_id, question_id, decision, algorithm_version,
             candidates_json, resolved_by, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?)
        """,
        (decision_id, report.source_document_id,
         report.from_document_revision_id, report.to_document_revision_id,
         question_id, decision, report.algorithm_version,
         json.dumps(candidates, ensure_ascii=False), now),
    )
    report.decisions.append({
        "question_id": question_id,
        "decision": decision,
        "from_question_revision_id": from_revision_id,
        "to_question_revision_id": to_revision_id,
        "candidates": candidates,
    })


def match_document_revisions(
    conn: sqlite3.Connection,
    *,
    source_document_id: str,
    to_document_revision_id: str,
    from_document_revision_id: str | None = None,
    algorithm_version: str = ALGORITHM_VERSION,
    now: str | None = None,
) -> MatchReport:
    """执行两阶段匹配并把决策落库（幂等）。"""
    from .persistence import _now

    report = MatchReport(
        source_document_id=source_document_id,
        from_document_revision_id=from_document_revision_id,
        to_document_revision_id=to_document_revision_id,
        algorithm_version=algorithm_version,
    )
    to_set = _load_revisions(conn, to_document_revision_id)
    from_set = _load_revisions(conn, from_document_revision_id) \
        if from_document_revision_id else {}
    timestamp = now or _now()

    new_items: list[tuple[str, dict]] = []
    removed_items: list[tuple[str, dict]] = []

    # ---- 阶段 1：确定性匹配 ----
    for question_id, to_item in to_set.items():
        from_item = from_set.get(question_id)
        if from_item is None:
            new_items.append((question_id, to_item))
            continue
        if from_item["content_hash"] == to_item["content_hash"]:
            _record_decision(conn, report, question_id=question_id,
                             decision=DECISION_MATCHED,
                             from_revision_id=from_item["question_revision_id"],
                             to_revision_id=to_item["question_revision_id"],
                             candidates=[], now=timestamp)
        else:
            _record_decision(conn, report, question_id=question_id,
                             decision=DECISION_CHANGED,
                             from_revision_id=from_item["question_revision_id"],
                             to_revision_id=to_item["question_revision_id"],
                             candidates=[], now=timestamp)
    for question_id, from_item in from_set.items():
        if question_id not in to_set:
            removed_items.append((question_id, from_item))

    # ---- 阶段 2：候选匹配（NEW × REMOVED 按 小题型+内容指纹） ----
    fingerprint_groups: dict[tuple, dict[str, list]] = defaultdict(
        lambda: {"new": [], "removed": []})
    for question_id, item in new_items:
        key = (item["sub_type_code"], item["content_hash"])
        fingerprint_groups[key]["new"].append((question_id, item))
    for question_id, item in removed_items:
        key = (item["sub_type_code"], item["content_hash"])
        fingerprint_groups[key]["removed"].append((question_id, item))

    for (sub_type_code, content_hash), group in fingerprint_groups.items():
        if len(group["new"]) == 1 and len(group["removed"]) == 1:
            # 全局唯一一对一：自动保留原逻辑身份（记录映射，不改写行）
            (new_id, new_item), = group["new"]
            (old_id, old_item), = group["removed"]
            candidate = {
                "sub_type_code": sub_type_code,
                "content_hash": content_hash,
                "match_basis": "sub_type_and_content_fingerprint",
                "from_question_id": old_id,
                "to_question_id": new_id,
            }
            _record_decision(conn, report, question_id=new_id,
                             decision=DECISION_MATCHED,
                             from_revision_id=old_item["question_revision_id"],
                             to_revision_id=new_item["question_revision_id"],
                             candidates=[candidate], now=timestamp)
            _record_decision(conn, report, question_id=old_id,
                             decision=DECISION_MATCHED,
                             from_revision_id=old_item["question_revision_id"],
                             to_revision_id=new_item["question_revision_id"],
                             candidates=[candidate], now=timestamp)
        else:
            # 多候选或无候选：有对立侧的落 AMBIGUOUS 等人工裁决，
            # 无对立侧的落 NEW / REMOVED
            for question_id, item in group["new"]:
                decision = DECISION_AMBIGUOUS if group["removed"] else DECISION_NEW
                candidates = []
                if group["removed"]:
                    candidates = [{
                        "sub_type_code": sub_type_code,
                        "content_hash": content_hash,
                        "match_basis": "sub_type_and_content_fingerprint",
                        "conflict_with": [rid for rid, _ in group["removed"]],
                    }]
                _record_decision(conn, report, question_id=question_id,
                                 decision=decision,
                                 from_revision_id=None,
                                 to_revision_id=item["question_revision_id"],
                                 candidates=candidates, now=timestamp)
            for question_id, item in group["removed"]:
                decision = DECISION_AMBIGUOUS if group["new"] else DECISION_REMOVED
                _record_decision(conn, report, question_id=question_id,
                                 decision=decision,
                                 from_revision_id=item["question_revision_id"],
                                 to_revision_id=None,
                                 candidates=[], now=timestamp)
    return report
