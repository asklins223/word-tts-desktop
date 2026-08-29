"""原子小题规范化模型包。

阶段 1 提供内存态的 QuestionItem/Stimulus 实体、ParseCandidate 中间协议
和旧 Parser 结果的候选抽取器；阶段 2 由 v0006 迁移落库。
契约见 docs/atomic-question-model-plan.md。
"""

from .model import (
    IDENTITY_VERSION,
    QUESTION_TYPE_CODES,
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
from .extractors import EXTRACTORS, extract_candidate

__all__ = [
    "IDENTITY_VERSION",
    "QUESTION_TYPE_CODES",
    "Answer",
    "ContentUnit",
    "Option",
    "ParseCandidate",
    "QuestionItem",
    "ResolutionState",
    "Stimulus",
    "build_identity",
    "content_hash",
    "EXTRACTORS",
    "extract_candidate",
]
