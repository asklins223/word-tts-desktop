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

from .model import (
    SUB_TYPE_REGISTRY,
    ContentUnit,
    ParseCandidate,
    QuestionItem,
    Stimulus,
)

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


def sync_sub_type_registry(conn: sqlite3.Connection) -> int:
    """把代码权威的小题型注册表幂等同步到 question_sub_types 表。"""
    import json as _json

    rows = []
    for st in SUB_TYPE_REGISTRY.values():
        capabilities = {
            "has_options": st.has_options,
            "answer_kind": st.answer_kind,
            "audio_granularity": st.audio_granularity,
            "voice_policy": st.voice_policy,
            "naming_prefix": st.naming_prefix,
        }
        rows.append((
            st.code, st.family, st.display_name, st.item_role,
            1 if st.has_options else 0, st.answer_kind,
            st.audio_granularity, st.voice_policy, st.naming_prefix,
            st.status, _json.dumps(capabilities, ensure_ascii=False),
        ))
    before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO question_sub_types
            (sub_type_code, type_family, display_name, item_role, has_options,
             answer_kind, audio_granularity, voice_policy, naming_prefix,
             status, capabilities_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sub_type_code) DO UPDATE SET
            type_family = excluded.type_family,
            display_name = excluded.display_name,
            item_role = excluded.item_role,
            has_options = excluded.has_options,
            answer_kind = excluded.answer_kind,
            audio_granularity = excluded.audio_granularity,
            voice_policy = excluded.voice_policy,
            naming_prefix = excluded.naming_prefix,
            status = excluded.status,
            capabilities_json = excluded.capabilities_json
        """,
        rows,
    )
    return conn.total_changes - before


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
        "SELECT sub_type_code, stimulus_type FROM stimuli WHERE stimulus_id = ?",
        (entity.stimulus_id,),
    ).fetchone()
    if existing_type and (
        existing_type[0] != entity.sub_type_code
        or existing_type[1] != entity.stimulus_type
    ):
        raise ValueError(
            f"材料身份漂移: {entity.stimulus_id} "
            f"{tuple(existing_type)} != "
            f"('{entity.sub_type_code}', '{entity.stimulus_type}')"
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO stimuli
            (stimulus_id, source_document_id, sub_type_code, stimulus_type,
             material_source, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entity.stimulus_id, source_document_id, entity.sub_type_code,
         entity.stimulus_type, entity.material_source, created),
    )
    revision_id = entity.stimulus_revision_id
    conn.execute(
        """
        INSERT OR IGNORE INTO stimulus_revisions
            (stimulus_revision_id, stimulus_id, document_revision_id,
             sub_type_code, text, section, material_source, source_locator,
             content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, entity.stimulus_id, document_revision_id,
         entity.sub_type_code, entity.text, entity.section,
         entity.material_source, entity.source_locator, entity.content_hash,
         created),
    )
    conn.execute(
        "UPDATE stimuli SET current_revision_id = ? WHERE stimulus_id = ?",
        (revision_id, entity.stimulus_id),
    )
    return revision_id


def _persist_question(conn, entity: QuestionItem, source_document_id: str,
                      document_revision_id: str, created: str) -> str:
    family_code = SUB_TYPE_REGISTRY[entity.question_type].family
    existing_type = conn.execute(
        "SELECT type_code, sub_type_code FROM question_items WHERE question_id = ?",
        (entity.question_id,),
    ).fetchone()
    if existing_type and (
        existing_type[0] != family_code
        or existing_type[1] != entity.question_type
    ):
        raise ValueError(
            f"小题身份漂移: {entity.question_id} "
            f"{tuple(existing_type)} != "
            f"('{family_code}', '{entity.question_type}')"
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO question_items
            (question_id, source_document_id, type_code, sub_type_code,
             created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entity.question_id, source_document_id, family_code,
         entity.question_type, created),
    )
    revision_id = entity.question_revision_id
    conn.execute(
        """
        INSERT OR IGNORE INTO question_revisions
            (question_revision_id, question_id, document_revision_id,
             sub_type_code, stem, options_json, answer_json, question_number,
             number_inferred, section, source_locator, resolution_state,
             content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, entity.question_id, document_revision_id,
         entity.question_type, entity.stem,
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
        "SELECT sub_type_code FROM content_units WHERE content_unit_id = ?",
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
            (content_unit_id, source_document_id, sub_type_code, unit_kind,
             created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entity.content_unit_id, source_document_id, entity.content_kind,
         entity.unit_kind, created),
    )
    revision_id = entity.content_unit_revision_id
    conn.execute(
        """
        INSERT OR IGNORE INTO content_unit_revisions
            (content_unit_revision_id, content_unit_id, document_revision_id,
             sub_type_code, entry_kind, text, section, discourse_number,
             sentence_number, entry_number, source_locator, content_hash,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, entity.content_unit_id, document_revision_id,
         entity.content_kind, entity.entry_kind, entity.text,
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

    def _record_membership(entity_kind: str, entity_revision_id: str, ordinal: int):
        conn.execute(
            """
            INSERT OR IGNORE INTO document_revision_members
                (document_revision_id, entity_kind, entity_revision_id, ordinal)
            VALUES (?, ?, ?, ?)
            """,
            (document_revision_id, entity_kind, entity_revision_id, ordinal),
        )

    for entity in candidate.entities:
        if isinstance(entity, Stimulus):
            revision_id = _persist_stimulus(
                conn, entity, source_document_id, document_revision_id, created)
            _record_membership("STIMULUS", revision_id, counts["stimuli"])
            stimulus_revision_by_id[entity.stimulus_id] = revision_id
            counts["stimuli"] += 1
        elif isinstance(entity, QuestionItem):
            revision_id = _persist_question(
                conn, entity, source_document_id, document_revision_id, created)
            _record_membership("QUESTION", revision_id, counts["questions"])
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
            revision_id = _persist_content_unit(
                conn, entity, source_document_id, document_revision_id, created)
            _record_membership("CONTENT_UNIT", revision_id, counts["content_units"])
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
        sync_sub_type_registry(conn)
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
