"""旧 Parser 结果 → 原子小题候选的抽取器（阶段 1）。

本模块是只读消费者：输入 ``parse_document_auto`` 的单个题型结果，
输出 ``ParseCandidate``，不改变旧解析结果本身（基线快照必须保持不变）。

方案 4 节的映射约束在这里落地：

- 题干/选项/答案未由旧 Parser 抽取的题型，候选只能 ``audio_only``，
  实体保持 ``resolution_state=DRAFT``，不得伪造业务字段；
- 信息获取文档的排版是“题目在前、录音稿在后”，小题关联到**后续**
  第一段录音稿；没有关联到录音稿的小题保留实体并写诊断，
  不允许静默丢弃或猜测归属；
- 题号断档/重复写诊断但不合并，归属歧义最终由 ``AMBIGUOUS`` 状态表达。

阶段 1 先覆盖信息获取、听后选择两个优先题型（方案 11 节第 8 项），
其余题型在阶段 3 统一分段器落地后接入。
"""

from __future__ import annotations

from .model import (
    QUESTION_TYPE_CODES,
    Answer,
    Option,
    ParseCandidate,
    QuestionItem,
    ResolutionState,
    Stimulus,
    build_identity,
)

BASE_CONFIDENCE = 0.95
DIAGNOSTIC_CONFIDENCE_PENALTY = 0.15

CAPABILITIES_AUDIO_ONLY = {
    "question_fields_complete": False,
    "audio_only": True,
}


def _confidence(diagnostics) -> float:
    return max(0.5, BASE_CONFIDENCE - DIAGNOSTIC_CONFIDENCE_PENALTY * len(diagnostics))


def _section_of(category: str) -> str:
    """听选信息录音稿/听选信息题目 → 听选信息。"""
    for suffix in ("录音稿", "题目"):
        if category.endswith(suffix):
            return category[: -len(suffix)]
    return category


def _extract_info_acquisition(result: dict, source_key: str) -> ParseCandidate:
    type_code = QUESTION_TYPE_CODES["信息获取"]
    stimuli: list[Stimulus] = []
    questions: list[QuestionItem] = []
    diagnostics: list[str] = []
    pending: list[dict] = []          # 等待关联到下一段录音稿的题目

    def flush_pending(stimulus_id: str | None):
        for raw in pending:
            number = raw.get("number")
            locator = f"{raw['category']}/题目{number if number is not None else raw.get('filename_stem', '?')}"
            questions.append(QuestionItem(
                question_id=build_identity("question", source_key, locator),
                question_type=type_code,
                stem=raw["text"],
                source_locator=locator,
                question_number=number,
                section=_section_of(raw["category"]),
                stimulus_id=stimulus_id,
                resolution_state=ResolutionState.DRAFT,
            ))
        pending.clear()

    for raw in result["items"]:
        category = raw.get("category", "")
        if category.endswith("录音稿"):
            locator = f"{category}/录音稿{raw.get('index')}"
            stimulus = Stimulus(
                stimulus_id=build_identity("stimulus", source_key, locator),
                stimulus_type="listening_script",
                text=raw["text"],
                source_locator=locator,
                section=_section_of(category),
                resolution_state=ResolutionState.DRAFT,
            )
            flush_pending(stimulus.stimulus_id)
            stimuli.append(stimulus)
        elif category.endswith("题目"):
            pending.append(raw)
        else:
            diagnostics.append(f"unknown_category:{category}")

    if pending:
        flush_pending(None)
        diagnostics.append("question_without_stimulus")

    numbers = sorted(q.question_number for q in questions if q.question_number is not None)
    if numbers:
        if len(set(numbers)) != len(numbers):
            diagnostics.append("duplicate_question_number")
        if numbers != list(range(numbers[0], numbers[-1] + 1)):
            diagnostics.append("question_number_gap")
    for stimulus in stimuli:
        if not any(q.stimulus_id == stimulus.stimulus_id for q in questions):
            diagnostics.append(f"stimulus_without_questions:{stimulus.source_locator}")

    entities: tuple = tuple(stimuli) + tuple(questions)
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(e.source_locator for e in entities),
        entities=entities,
        confidence=_confidence(diagnostics),
        diagnostics=tuple(diagnostics),
        capabilities=dict(CAPABILITIES_AUDIO_ONLY),
    )


def _extract_listening_selection(result: dict, source_key: str) -> ParseCandidate:
    type_code = QUESTION_TYPE_CODES["听后选择"]
    stimuli: list[Stimulus] = []
    for raw in result["items"]:
        category = raw.get("category", "")
        locator = f"{category}/录音稿{raw.get('index')}"
        stimuli.append(Stimulus(
            stimulus_id=build_identity("stimulus", source_key, locator),
            stimulus_type="listening_script",
            text=raw["text"],
            source_locator=locator,
            section=_section_of(category),
            resolution_state=ResolutionState.DRAFT,
        ))

    diagnostics = [
        "listening_choice_stem_options_answer_not_extracted",
        "audio_only_candidate",
    ]
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(s.source_locator for s in stimuli),
        entities=tuple(stimuli),
        confidence=_confidence(diagnostics),
        diagnostics=tuple(diagnostics),
        capabilities=dict(CAPABILITIES_AUDIO_ONLY),
    )


# key 与 question_types 注册表的题型名（doc_type）一致；
# 未列出的题型在阶段 3 接入前不做候选映射。
EXTRACTORS = {
    "信息获取": _extract_info_acquisition,
    "听后选择": _extract_listening_selection,
}


def extract_candidate(doc_type: str, result: dict, source_key: str) -> ParseCandidate:
    """把单个题型解析结果映射为原子小题候选。

    ``source_key`` 是文档的稳定业务键；阶段 1 暂用文件名，
    ``source_documents`` 表（v0006）落地后由文档身份提供。
    """
    extractor = EXTRACTORS.get(doc_type)
    if extractor is None:
        raise KeyError(f"题型 {doc_type} 尚未接入原子小题抽取")
    return extractor(result, source_key)
