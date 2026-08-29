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

FAMILY_DISPLAY_NAMES_BY_CODE = {code: key for key, code in QUESTION_TYPE_CODES.items()}


@dataclass(frozen=True)
class QuestionSubType:
    """小题型注册表条目：业务能力/音色/命名/题量校验的最小挂载粒度。

    - ``family`` 是解析器粒度的大题型（QUESTION_TYPE_CODES 的 code）；
    - 模仿朗读、词汇等本身就是最小题型，家族下只有一个同名小题型；
    - ``status='reserved'`` 表示已注册但解析器尚未接入（如询问信息）；
    - 展示名（display_name）只用于界面，不参与身份与业务键。
    """

    code: str
    family: str
    display_name: str
    item_role: str                 # question / stimulus / content
    has_options: bool = False
    answer_kind: str | None = None  # single_choice / spoken_response / None
    audio_granularity: str = "per_item"  # per_item / script_whole / passage
    voice_policy: str = "speaker"       # speaker / forced_female / default
    naming_prefix: str = "录音稿"
    status: str = "active"              # active / reserved


SUB_TYPE_REGISTRY: dict[str, QuestionSubType] = {
    st.code: st
    for st in (
        # 信息获取：听选信息（选择题）与回答问题（口头作答）
        QuestionSubType("listening_info", "info_acquisition", "听选信息",
                        "question", has_options=True,
                        answer_kind="single_choice"),
        QuestionSubType("answer_question", "info_acquisition", "回答问题",
                        "question", answer_kind="spoken_response"),
        # 听后选择：叶子题型
        QuestionSubType("listening_choice", "listening_choice", "听后选择",
                        "question", has_options=True,
                        answer_kind="single_choice"),
        # 听后应答：叶子题型
        QuestionSubType("listening_response", "listening_response", "听后应答",
                        "question", answer_kind="spoken_response"),
        # 信息转述及询问：询问信息已注册但解析器未接入
        QuestionSubType("info_retelling", "info_retelling", "信息转述",
                        "stimulus", audio_granularity="script_whole"),
        QuestionSubType("asking_info", "info_retelling", "询问信息",
                        "question", status="reserved"),
        # 模仿朗读：本身就是最小题型；外网/教材是来源属性不是小题型
        QuestionSubType("imitation_reading", "imitation_reading", "模仿朗读",
                        "stimulus", audio_granularity="passage"),
        # 课文跟读：句子/段落/语篇三种跟读小题型
        QuestionSubType("text_reading_sentence", "text_reading", "句子跟读",
                        "content"),
        QuestionSubType("text_reading_paragraph", "text_reading", "段落跟读",
                        "content"),
        QuestionSubType("text_reading_discourse", "text_reading", "语篇跟读",
                        "content"),
        # 词汇：本身就是最小题型；单词/例句是词条属性不是小题型
        QuestionSubType("vocabulary", "vocabulary", "词汇",
                        "content", voice_policy="forced_female",
                        naming_prefix="词条"),
    )
}

# 大题型 → 小题型 codes；family 本身必须是 QUESTION_TYPE_CODES 的值。
FAMILY_SUB_TYPES: dict[str, tuple[str, ...]] = {}
for _st in SUB_TYPE_REGISTRY.values():
    FAMILY_SUB_TYPES.setdefault(_st.family, []).append(_st.code)  # type: ignore[union-attr]


def _validate_sub_type(code: str, *, allowed_roles: tuple[str, ...] | None = None) -> None:
    sub_type = SUB_TYPE_REGISTRY.get(code)
    if sub_type is None:
        raise ValueError(f"未注册的小题型代码: {code}")
    if sub_type.status == "reserved":
        raise ValueError(f"小题型 {code} 已注册但解析器尚未接入，不得产出实体")
    if allowed_roles and sub_type.item_role not in allowed_roles:
        raise ValueError(
            f"小题型 {code} 的角色 {sub_type.item_role} 不允许用于此实体"
        )


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
    sub_type_code: str            # SUB_TYPE_REGISTRY 的稳定 code
    stimulus_type: str            # listening_script / reading_passage / ...
    text: str
    source_locator: str
    section: str | None = None
    material_source: str | None = None   # 来源属性（如 外网/教材），不是小题型
    resolution_state: ResolutionState = ResolutionState.DRAFT
    content_hash: str = field(default="", compare=False)

    def __post_init__(self):
        _validate_sub_type(self.sub_type_code, allowed_roles=None)
        if not self.content_hash:
            object.__setattr__(self, "content_hash", content_hash({
                "stimulus_id": self.stimulus_id,
                "sub_type_code": self.sub_type_code,
                "stimulus_type": self.stimulus_type,
                "text": self.text,
                "material_source": self.material_source,
            }))

    def to_dict(self) -> dict:
        return {
            "stimulus_id": self.stimulus_id,
            "sub_type_code": self.sub_type_code,
            "stimulus_type": self.stimulus_type,
            "text": self.text,
            "source_locator": self.source_locator,
            "section": self.section,
            "material_source": self.material_source,
            "resolution_state": self.resolution_state.value,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class QuestionItem:
    """最小业务小题。阶段 1 只有题干，选项/答案缺失时不得进入外部链路。

    ``question_type`` 是小题型粒度的稳定 code（SUB_TYPE_REGISTRY），
    不使用中文 category，也不是解析器粒度的大题型。
    """

    question_id: str
    question_type: str            # SUB_TYPE_REGISTRY 的稳定 code
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
        _validate_sub_type(self.question_type, allowed_roles=("question",))
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
    def type_family(self) -> str:
        return SUB_TYPE_REGISTRY[self.question_type].family

    @property
    def major_type(self) -> str:
        """大题型中文名（展示/回溯用，不参与身份）。"""
        return FAMILY_DISPLAY_NAMES_BY_CODE[self.type_family]

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

    ``content_kind`` 是小题型粒度的稳定 code（如 text_reading_sentence、
    vocabulary）；词汇的单词/例句是词条属性 ``entry_kind``，不是小题型。
    """

    content_unit_id: str
    content_kind: str            # SUB_TYPE_REGISTRY 的稳定 code
    text: str
    source_locator: str
    section: str | None = None
    entry_kind: str | None = None     # 词汇词条属性：word / example_sentence
    discourse_number: int | None = None
    sentence_number: int | None = None
    entry_number: int | None = None   # 词条、朗读任务等顺序号
    unit_kind: str = "LEARNING_CONTENT"
    resolution_state: ResolutionState = ResolutionState.DRAFT
    content_hash: str = field(default="", compare=False)

    def __post_init__(self):
        _validate_sub_type(self.content_kind, allowed_roles=("content",))
        if not self.content_hash:
            object.__setattr__(self, "content_hash", content_hash({
                "content_unit_id": self.content_unit_id,
                "content_kind": self.content_kind,
                "text": self.text,
                "entry_kind": self.entry_kind,
                "unit_kind": self.unit_kind,
            }))

    def to_dict(self) -> dict:
        return {
            "content_unit_id": self.content_unit_id,
            "content_kind": self.content_kind,
            "text": self.text,
            "source_locator": self.source_locator,
            "section": self.section,
            "entry_kind": self.entry_kind,
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
    type_code: str                 # 大题型（解析器粒度）稳定 code
    type_version: str = "1"
    claimed_blocks: tuple[str, ...] = ()   # 候选声明的原文范围（source_locator）
    entities: tuple[Stimulus | QuestionItem | ContentUnit, ...] = ()
    confidence: float = 0.95
    diagnostics: tuple[str, ...] = ()
    capabilities: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self):
        if self.type_code not in QUESTION_TYPE_CODES.values():
            raise ValueError(f"未注册的大题型代码: {self.type_code}")

    @property
    def sub_type_codes(self) -> tuple[str, ...]:
        """候选实体覆盖到的小题型（去重保序）。"""
        seen: list[str] = []
        for entity in self.entities:
            code = getattr(entity, "question_type", None) or \
                getattr(entity, "sub_type_code", None) or \
                getattr(entity, "content_kind", None)
            if code and code not in seen:
                seen.append(code)
        return tuple(seen)

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
