"""原子小题模型落库仓储（阶段 2，v0006 schema）。

约定：

* 所有函数接收 sqlite3 连接，事务边界由调用方管理
  （``workflow.database.WorkflowDatabase.transaction``）；
* revision 主键按内容寻址（``question-revision:<content_hash>``），
  正文变化产生新 revision 行，旧行不可覆盖；
* 重放幂等：同一输入重复持久化不产生新行；
* 身份漂移（同一 question_id 出现不同 type_code 等）直接报错，
  不静默合并（方案 5.3 / 7.1 约束）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .model import ContentUnit, ParseCandidate, QuestionItem, Stimulus

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_hash(*parts) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def ensure_source_document(
    conn: sqlite3.Connection,
    logical_key: str,
    *,
    business_scope: str = "local",
    source_type: str = "other",
    display_name: str | None = None,
    now: str | None = None,
) -> str:
    """按 (业务范围, logical_key) 幂等创建文档逻辑身份。"""
    source_document_id = f"source-document:{business_scope}:{logical_key}"
    created = now or _now()
    conn.execute(
        """
        INSERT OR IGNORE INTO source_documents
            (source_document_id, logical_key, business_scope, source_type,
             display_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_document_id, logical_key, business_scope, source_type,
         display_name, created),
    )
    return source_document_id


def create_document_revision(
    conn: sqlite3.Connection,
    source_document_id: str,
    *,
    file_hash: str,
    parser_version: int,
    schema_version: int = SCHEMA_VERSION,
    source_artifact_id: str | None = None,
    now: str | None = None,
) -> str:
    """幂等创建文档内容版本；同一文件 + 解析版本重放返回同一 id。"""
    document_revision_id = (
        f"document-revision:{source_document_id}:{_short_hash(file_hash, str(parser_version), str(schema_version))}"
    )
    created = now or _now()
    conn.execute(
        """
        INSERT OR IGNORE INTO document_revisions
            (document_revision_id, source_document_id, source_artifact_id,
             file_hash, parser_version, schema_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (document_revision_id, source_document_id, source_artifact_id,
         file_hash, parser_version, schema_version, created),
    )
    return document_revision_id


def _question_revision_id(content_hash: str) -> str:
    return f"question-revision:{content_hash}"


def _stimulus_revision_id(content_hash: str) -> str:
    return f"stimulus-revision:{content_hash}"


def _content_unit_revision_id(content_hash: str) -> str:
    return f"content-unit-revision:{content_hash}"


def _options_json(options) -> str:
    return json.dumps(
        [{"option_id": o.option_id, "text": o.text} for o in options],
        ensure_ascii=False,
    )


def _answer_json(answer) -> str | None:
    if answer is None:
        return None
    return json.dumps({"kind": answer.kind, "value": answer.value},
                      ensure_ascii=False)


def _persist_stimulus(conn, entity: Stimulus, source_document_id: str,
                      document_revision_id: str, created: str) -> str:
    existing_type = conn.execute(
        "SELECT stimulus_type FROM stimuli WHERE stimulus_id = ?",
        (entity.stimulus_id,),
    ).fetchone()
    if existing_type and existing_type[0] != entity.stimulus_type:
        raise ValueError(
            f"材料身份漂移: {entity.stimulus_id} "
            f"{existing_type[0]} != {entity.stimulus_type}"
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO stimuli
            (stimulus_id, source_document_id, stimulus_type, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (entity.stimulus_id, source_document_id, entity.stimulus_type, created),
    )
    revision_id = _stimulus_revision_id(entity.content_hash)
    conn.execute(
        """
        INSERT OR IGNORE INTO stimulus_revisions
            (stimulus_revision_id, stimulus_id, document_revision_id, text,
             section, source_locator, content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, entity.stimulus_id, document_revision_id, entity.text,
         entity.section, entity.source_locator, entity.content_hash, created),
    )
    conn.execute(
        "UPDATE stimuli SET current_revision_id = ? WHERE stimulus_id = ?",
        (revision_id, entity.stimulus_id),
    )
    return revision_id


def _persist_question(conn, entity: QuestionItem, source_document_id: str,
                      document_revision_id: str, created: str) -> str:
    existing_type = conn.execute(
        "SELECT type_code FROM question_items WHERE question_id = ?",
        (entity.question_id,),
    ).fetchone()
    if existing_type and existing_type[0] != entity.question_type:
        raise ValueError(
            f"小题身份漂移: {entity.question_id} "
            f"{existing_type[0]} != {entity.question_type}"
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO question_items
            (question_id, source_document_id, type_code, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (entity.question_id, source_document_id, entity.question_type, created),
    )
    revision_id = _question_revision_id(entity.content_hash)
    conn.execute(
        """
        INSERT OR IGNORE INTO question_revisions
            (question_revision_id, question_id, document_revision_id, stem,
             options_json, answer_json, question_number, number_inferred,
             section, source_locator, resolution_state, content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, entity.question_id, document_revision_id, entity.stem,
         _options_json(entity.options), _answer_json(entity.answer),
         entity.question_number, 1 if entity.number_inferred else 0,
         entity.section, entity.source_locator, entity.resolution_state.value,
         entity.content_hash, created),
    )
    conn.execute(
        "UPDATE question_items SET current_revision_id = ? WHERE question_id = ?",
        (revision_id, entity.question_id),
    )
    return revision_id


def _persist_content_unit(conn, entity: ContentUnit, source_document_id: str,
                          document_revision_id: str, created: str) -> str:
    existing_kind = conn.execute(
        "SELECT content_kind FROM content_units WHERE content_unit_id = ?",
        (entity.content_unit_id,),
    ).fetchone()
    if existing_kind and existing_kind[0] != entity.content_kind:
        raise ValueError(
            f"内容单元身份漂移: {entity.content_unit_id} "
            f"{existing_kind[0]} != {entity.content_kind}"
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO content_units
            (content_unit_id, source_document_id, content_kind, unit_kind,
             created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entity.content_unit_id, source_document_id, entity.content_kind,
         entity.unit_kind, created),
    )
    revision_id = _content_unit_revision_id(entity.content_hash)
    conn.execute(
        """
        INSERT OR IGNORE INTO content_unit_revisions
            (content_unit_revision_id, content_unit_id, document_revision_id,
             text, section, discourse_number, sentence_number, entry_number,
             source_locator, content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, entity.content_unit_id, document_revision_id, entity.text,
         entity.section, entity.discourse_number, entity.sentence_number,
         entity.entry_number, entity.source_locator, entity.content_hash,
         created),
    )
    conn.execute(
        "UPDATE content_units SET current_revision_id = ? WHERE content_unit_id = ?",
        (revision_id, entity.content_unit_id),
    )
    return revision_id


def persist_candidate(
    conn: sqlite3.Connection,
    candidate: ParseCandidate,
    *,
    source_document_id: str,
    document_revision_id: str,
    now: str | None = None,
) -> dict:
    """把一个 ParseCandidate 的实体写入 v0006 表（幂等）。

    返回各实体类别的处理数量；同一输入重复调用行数不变。
    小题与材料的关联按 relation_type='references' 写 question_stimuli。
    """
    created = now or _now()
    counts = {
        "stimuli": 0,
        "questions": 0,
        "question_stimuli": 0,
        "content_units": 0,
    }
    stimulus_revision_by_id: dict[str, str] = {}

    for entity in candidate.entities:
        if isinstance(entity, Stimulus):
            revision_id = _persist_stimulus(
                conn, entity, source_document_id, document_revision_id, created)
            stimulus_revision_by_id[entity.stimulus_id] = revision_id
            counts["stimuli"] += 1
        elif isinstance(entity, QuestionItem):
            revision_id = _persist_question(
                conn, entity, source_document_id, document_revision_id, created)
            counts["questions"] += 1
            if entity.stimulus_id:
                stimulus_revision_id = stimulus_revision_by_id.get(entity.stimulus_id)
                if stimulus_revision_id is None:
                    raise ValueError(
                        f"小题 {entity.question_id} 引用的材料 "
                        f"{entity.stimulus_id} 不在同一候选中；"
                        "跨候选关联必须先持久化材料候选"
                    )
                link_id = f"link:{revision_id}:{stimulus_revision_id}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO question_stimuli
                        (link_id, question_revision_id, stimulus_revision_id,
                         relation_type, ordinal)
                    VALUES (?, ?, ?, 'references', ?)
                    """,
                    (link_id, revision_id, stimulus_revision_id,
                     counts["question_stimuli"]),
                )
                counts["question_stimuli"] += 1
        elif isinstance(entity, ContentUnit):
            _persist_content_unit(
                conn, entity, source_document_id, document_revision_id, created)
            counts["content_units"] += 1
        else:
            raise TypeError(f"未知的实体类型: {type(entity).__name__}")
    return counts


def persist_parse(
    conn: sqlite3.Connection,
    logical_key: str,
    candidates,
    *,
    file_hash: str,
    parser_version: int,
    source_type: str = "other",
    source_artifact_id: str | None = None,
    now: str | None = None,
) -> dict:
    """端到端入口：文档身份 + revision + 候选集合。

    自管事务（未处于事务中时开启 BEGIN IMMEDIATE）；调用方连接需
    开启 ``PRAGMA foreign_keys = ON``。
    """
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        source_document_id = ensure_source_document(
            conn, logical_key, source_type=source_type, now=now)
        document_revision_id = create_document_revision(
            conn, source_document_id, file_hash=file_hash,
            parser_version=parser_version, schema_version=SCHEMA_VERSION,
            source_artifact_id=source_artifact_id, now=now)
        totals = {
            "stimuli": 0, "questions": 0,
            "question_stimuli": 0, "content_units": 0,
        }
        for candidate in candidates:
            counts = persist_candidate(
                conn, candidate,
                source_document_id=source_document_id,
                document_revision_id=document_revision_id, now=now)
            for key in totals:
                totals[key] += counts[key]
    except Exception:
        if own_transaction:
            conn.execute("ROLLBACK")
        raise
    if own_transaction:
        conn.execute("COMMIT")
    return {
        "source_document_id": source_document_id,
        "document_revision_id": document_revision_id,
        **totals,
    }
