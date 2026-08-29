"""原子小题规范化模型（阶段 1，内存态，v0006 落库前的事实载体）。

对应 docs/atomic-question-model-plan.md 的冻结契约：

- ``QuestionItem`` 是外部系统的最小业务单元，``Stimulus`` 是共享材料单元，
  二者是解析事实；音频/外部录入等操作模型在阶段 2+ 再引入。
- ``resolution_state`` 只描述内容事实是否确认，与任务状态、外部副作用状态分层。
- 身份采用 ``question:<source_key>:<locator>`` / ``stimulus:<source_key>:<locator>``；
  阶段 1 的 ``source_key`` 暂用文档名，``source_documents`` 表落地后切换为
  稳定业务键，届时 id 生成规则只改 ``build_identity`` 一处。
- 阶段 1 的实体由旧 Parser 结果映射而来，题目只有题干、没有选项/答案，
  因此一律 ``resolution_state=DRAFT`` 且 ``audio_only=True``，
  不得伪装成可外部录入的完整小题。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ResolutionState(str, Enum):
    """内容事实确认状态；与操作状态、外部副作用状态严格分层。"""

    DRAFT = "DRAFT"
    CANDIDATE = "CANDIDATE"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


# 稳定题型代码：question_type 字段只允许用这里的 code，不使用中文 category。
# key 与 question_types 注册表的题型名一一对应。
QUESTION_TYPE_CODES = {
    "信息获取": "info_acquisition",
    "听后选择": "listening_choice",
    "听后应答": "listening_response",
    "信息转述及询问": "info_retelling",
    "模仿朗读": "imitation_reading",
    "课文跟读": "text_reading",
    "词汇": "vocabulary",
}

IDENTITY_VERSION = "1"


def canonical_payload(obj: Any) -> str:
    """键排序的紧凑 JSON，用于内容哈希，保证同内容同哈希。"""
    import json

    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def slugify(locator: str) -> str:
    """把结构定位转成可读且稳定的 id 片段。"""
    return re.sub(r"[\s/\\]+", "-", locator.strip())


def build_identity(kind: str, source_key: str, locator: str) -> str:
    """``question:<source>:<locator>`` / ``stimulus:<source>:<locator>``。"""
    return f"{kind}:{source_key}:{slugify(locator)}"


@dataclass(frozen=True)
class Option:
    option_id: str
    text: str


@dataclass(frozen=True)
class Answer:
    kind: str   # single_choice / multi_choice / short_answer / ...
    value: str


@dataclass(frozen=True)
class Stimulus:
    """共享材料：一段录音稿/文章可被多个小题引用。"""

    stimulus_id: str
    stimulus_type: str          # listening_script / reading_passage / ...
    text: str
    source_locator: str
    section: str | None = None
    resolution_state: ResolutionState = ResolutionState.DRAFT
    content_hash: str = field(default="", compare=False)

    def __post_init__(self):
        if not self.content_hash:
            object.__setattr__(self, "content_hash", content_hash({
                "stimulus_id": self.stimulus_id,
                "stimulus_type": self.stimulus_type,
                "text": self.text,
            }))

    def to_dict(self) -> dict:
        return {
            "stimulus_id": self.stimulus_id,
            "stimulus_type": self.stimulus_type,
            "text": self.text,
            "source_locator": self.source_locator,
            "section": self.section,
            "resolution_state": self.resolution_state.value,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class QuestionItem:
    """最小业务小题。阶段 1 只有题干，选项/答案缺失时不得进入外部链路。"""

    question_id: str
    question_type: str          # QUESTION_TYPE_CODES 的稳定 code
    stem: str
    source_locator: str
    question_number: int | None = None
    number_inferred: bool = False
    options: tuple[Option, ...] = ()
    answer: Answer | None = None
    section: str | None = None
    stimulus_id: str | None = None
    resolution_state: ResolutionState = ResolutionState.DRAFT
    content_hash: str = field(default="", compare=False)

    def __post_init__(self):
        if self.question_type not in QUESTION_TYPE_CODES.values():
            raise ValueError(f"未注册的题型代码: {self.question_type}")
        if not self.content_hash:
            object.__setattr__(self, "content_hash", content_hash({
                "question_id": self.question_id,
                "question_type": self.question_type,
                "stem": self.stem,
                "options": [(o.option_id, o.text) for o in self.options],
                "answer": (self.answer.kind, self.answer.value) if self.answer else None,
                "stimulus_id": self.stimulus_id,
            }))

    @property
    def question_revision_id(self) -> str:
        """内容寻址的版本号；v0006 落库后改为持久化 revision 表。"""
        return f"question-revision:{self.content_hash[:16]}"

    @property
    def question_fields_complete(self) -> bool:
        return bool(self.stem) and bool(self.options) and self.answer is not None

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "question_revision_id": self.question_revision_id,
            "identity_version": IDENTITY_VERSION,
            "question_type": self.question_type,
            "question_number": self.question_number,
            "number_inferred": self.number_inferred,
            "stem": self.stem,
            "options": [{"option_id": o.option_id, "text": o.text} for o in self.options],
            "answer": {"kind": self.answer.kind, "value": self.answer.value} if self.answer else None,
            "section": self.section,
            "stimulus_id": self.stimulus_id,
            "source_locator": self.source_locator,
            "resolution_state": self.resolution_state.value,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ContentUnit:
    """非考试学习内容单元（课文跟读、词汇等）。

    方案 3.2：不伪装成 QuestionItem；unit_kind 恒为 LEARNING_CONTENT，
    仍可作为 OperationScope 的目标进入统一操作流程。
    """

    content_unit_id: str
    content_kind: str            # discourse_reading / paragraph_reading / sentence_reading / word / example_sentence
    text: str
    source_locator: str
    section: str | None = None
    discourse_number: int | None = None
    sentence_number: int | None = None
    entry_number: int | None = None   # 词汇词条、朗读任务等顺序号
    unit_kind: str = "LEARNING_CONTENT"
    resolution_state: ResolutionState = ResolutionState.DRAFT
    content_hash: str = field(default="", compare=False)

    def __post_init__(self):
        if not self.content_hash:
            object.__setattr__(self, "content_hash", content_hash({
                "content_unit_id": self.content_unit_id,
                "content_kind": self.content_kind,
                "text": self.text,
                "unit_kind": self.unit_kind,
            }))

    def to_dict(self) -> dict:
        return {
            "content_unit_id": self.content_unit_id,
            "content_kind": self.content_kind,
            "text": self.text,
            "source_locator": self.source_locator,
            "section": self.section,
            "discourse_number": self.discourse_number,
            "sentence_number": self.sentence_number,
            "entry_number": self.entry_number,
            "unit_kind": self.unit_kind,
            "resolution_state": self.resolution_state.value,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ParseCandidate:
    """结构层与题型层之间的中间协议（方案 5.2.1）。

    一个候选是一次题型规则对一块原文范围的完整抽取结果；裁决层据此
    决定 owner，多规则命中同一范围且无法唯一裁决时保留多个候选并标 AMBIGUOUS。
    """

    candidate_id: str
    type_code: str
    type_version: str = "1"
    claimed_blocks: tuple[str, ...] = ()   # 候选声明的原文范围（source_locator）
    entities: tuple[Stimulus | QuestionItem | ContentUnit, ...] = ()
    confidence: float = 0.95
    diagnostics: tuple[str, ...] = ()
    capabilities: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self):
        if self.type_code not in QUESTION_TYPE_CODES.values():
            raise ValueError(f"未注册的题型代码: {self.type_code}")

    @property
    def audio_only(self) -> bool:
        return bool(self.capabilities.get("audio_only"))

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "type_code": self.type_code,
            "type_version": self.type_version,
            "claimed_blocks": list(self.claimed_blocks),
            "entities": [e.to_dict() for e in self.entities],
            "confidence": self.confidence,
            "diagnostics": list(self.diagnostics),
            "capabilities": dict(self.capabilities),
        }
