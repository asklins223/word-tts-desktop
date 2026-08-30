"""原子小题规范化模型包。

阶段 1 提供内存态的 QuestionItem/Stimulus 实体、ParseCandidate 中间协议
和旧 Parser 结果的候选抽取器；阶段 2 由 v0006 迁移落库。
契约见 docs/atomic-question-model-plan.md。
"""

from .model import (
    FAMILY_REGISTRY,
    FAMILY_SUB_TYPES,
    QuestionFamily,
    IDENTITY_VERSION,
    QUESTION_TYPE_CODES,
    SUB_TYPE_REGISTRY,
    QuestionSubType,
    Answer,
    ContentUnit,
    Option,
    ParseCandidate,
    QuestionItem,
    ResolutionState,
    Stimulus,
    build_identity,
    content_hash,
)
from .adjudication import AdjudicatedParse, BlockConflict, adjudicate
from .extractors import EXTRACTORS, extract_candidate
from .audio_projection import project_audio_tasks_to_work_items
from .operations import (
    SCOPE_KINDS_BY_ROLE,
    add_task_dependency,
    create_audio_tasks,
    create_operation_plan,
    create_scope,
    validate_scope_kind,
)
from .revision_match import (
    ALGORITHM_VERSION,
    DECISION_AMBIGUOUS,
    DECISION_CHANGED,
    DECISION_MATCHED,
    DECISION_NEW,
    DECISION_REMOVED,
    match_document_revisions,
)
from .persistence import (
    SCHEMA_VERSION,
    create_document_revision,
    ensure_source_document,
    persist_candidate,
    persist_parse,
    sync_sub_type_registry,
)

__all__ = [
    "ALGORITHM_VERSION",
    "SCOPE_KINDS_BY_ROLE",
    "DECISION_AMBIGUOUS",
    "DECISION_CHANGED",
    "DECISION_MATCHED",
    "DECISION_NEW",
    "DECISION_REMOVED",
    "IDENTITY_VERSION",
    "FAMILY_REGISTRY",
    "QUESTION_TYPE_CODES",
    "SUB_TYPE_REGISTRY",
    "FAMILY_SUB_TYPES",
    "QuestionFamily",
    "QuestionSubType",
    "SCHEMA_VERSION",
    "AdjudicatedParse",
    "Answer",
    "BlockConflict",
    "ContentUnit",
    "Option",
    "ParseCandidate",
    "QuestionItem",
    "ResolutionState",
    "Stimulus",
    "adjudicate",
    "build_identity",
    "content_hash",
    "create_document_revision",
    "ensure_source_document",
    "EXTRACTORS",
    "extract_candidate",
    "persist_candidate",
    "persist_parse",
    "match_document_revisions",
    "add_task_dependency",
    "create_audio_tasks",
    "create_operation_plan",
    "create_scope",
    "validate_scope_kind",
    "project_audio_tasks_to_work_items",
]
