"""旧 Parser 结果 → 原子小题候选的抽取器（阶段 1）。

本模块是只读消费者：输入 ``parse_document_auto`` 的单个题型结果，
输出 ``ParseCandidate``，不改变旧解析结果本身（基线快照必须保持不变）。

方案 4 节的映射约束在这里落地：

- 题干/选项/答案未由旧 Parser 抽取的考试题型，候选只能 ``audio_only``，
  实体保持 ``resolution_state=DRAFT``，不得伪造业务字段；
- 信息获取文档的排版是“题目在前、录音稿在后”，小题关联到**后续**
  第一段录音稿；没有关联到录音稿的小题保留实体并写诊断，
  不允许静默丢弃或猜测归属；
- 题号断档/重复写诊断但不合并，归属歧义最终由 ``AMBIGUOUS`` 状态表达；
- 课文跟读、词汇是非考试学习内容，统一映射为 ``ContentUnit``，
  不伪装成考试小题；阶段 1 只使用解析器实际输出的结构字段生成定位，
  定位冲突时追加出现序号并写诊断，不臆测段落/语篇分组。

八个注册题型全部接入；每种题型的“业务原子边界”与“音频边界”在
阶段 1 相同（逐条目），复合结构拆分推迟到阶段 3 统一分段器。
"""

from __future__ import annotations

from collections import Counter

from .model import (
    QUESTION_TYPE_CODES,
    SUB_TYPE_REGISTRY,
    Answer,
    ContentUnit,
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

CAPABILITIES_BUSINESS_COMPLETE = {
    "question_fields_complete": True,
    "audio_only": False,
}


def _confidence(diagnostics) -> float:
    return max(0.5, BASE_CONFIDENCE - DIAGNOSTIC_CONFIDENCE_PENALTY * len(diagnostics))


def _section_of(category: str) -> str:
    """听选信息录音稿/听选信息题目 → 听选信息。"""
    for suffix in ("录音稿", "题目"):
        if category.endswith(suffix):
            return category[: -len(suffix)]
    return category


# category → 小题型；信息获取按听选信息/回答问题两个小题型产出
INFO_ACQUISITION_SUB_TYPES = {
    "听选信息题目": "listening_info",
    "听选信息录音稿": "listening_info",
    "回答问题题目": "answer_question",
    "回答问题录音稿": "answer_question",
}


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
                question_type=INFO_ACQUISITION_SUB_TYPES[raw["category"]],
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
                sub_type_code=INFO_ACQUISITION_SUB_TYPES[category],
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
    """听后选择：录音稿 Stimulus + 题干/选项 QuestionItem（业务字段通道）。

    ``result["questions"]`` 由解析器以独立通道提供（不影响音频 items）；
    文档未携带答案 → ``question_fields_complete`` 仍为 False，
    保持 DRAFT，不得进入外部链路（方案 5.2.2）。
    """
    type_code = QUESTION_TYPE_CODES["听后选择"]
    stimuli: list[Stimulus] = []
    stimulus_by_ordinal: dict[int, str] = {}
    for raw in result["items"]:
        category = raw.get("category", "")
        locator = f"{category}/录音稿{raw.get('index')}"
        stimulus = Stimulus(
            stimulus_id=build_identity("stimulus", source_key, locator),
            sub_type_code="listening_choice",
            stimulus_type="listening_script",
            text=raw["text"],
            source_locator=locator,
            section=_section_of(category),
            resolution_state=ResolutionState.DRAFT,
        )
        stimulus_by_ordinal[raw.get("index")] = stimulus.stimulus_id
        stimuli.append(stimulus)

    questions: list[QuestionItem] = []
    for raw in result.get("questions", []):
        locator = f"听后选择题目/题目{raw['number']}"
        script_ordinal = raw.get("script_ordinal")
        questions.append(QuestionItem(
            question_id=build_identity("question", source_key, locator),
            question_type=type_code,
            stem=raw["stem"],
            source_locator=locator,
            question_number=raw["number"],
            options=tuple(Option(o["option_id"], o["text"])
                          for o in raw.get("options", [])),
            answer=None,
            section=_section_of("听后选择录音稿"),
            stimulus_id=stimulus_by_ordinal.get(script_ordinal),
            resolution_state=ResolutionState.DRAFT,
        ))

    diagnostics = ["audio_only_candidate"]
    if questions:
        diagnostics.append("listening_choice_answer_not_extracted")
        if any(q.stimulus_id is None for q in questions):
            diagnostics.append("listening_choice_question_without_script")
    else:
        diagnostics.append("listening_choice_stem_options_answer_not_extracted")
    entities = tuple(stimuli) + tuple(questions)
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(e.source_locator for e in entities),
        entities=entities,
        confidence=_confidence(diagnostics),
        diagnostics=tuple(diagnostics),
        capabilities=dict(CAPABILITIES_AUDIO_ONLY),
    )


def _dedupe_locators(locators):
    """结构字段不足导致定位重复时追加出现序号；返回 (定位列表, 是否有重复)。"""
    counts = Counter(locators)
    if not any(n > 1 for n in counts.values()):
        return list(locators), False
    seen = Counter()
    deduped = []
    for loc in locators:
        seen[loc] += 1
        deduped.append(f"{loc}#{seen[loc]}" if counts[loc] > 1 else loc)
    return deduped, True


def _extract_listening_response(result: dict, source_key: str) -> ParseCandidate:
    """听后应答：每个待应答句一条 QuestionItem（方案 4 节映射表）。

    提示句即题干；期望应答（答案）未由旧 Parser 抽取，保持 audio_only。
    """
    type_code = QUESTION_TYPE_CODES["听后应答"]
    questions = []
    for raw in result["items"]:
        category = raw.get("category", "听后应答录音稿")
        number = raw.get("number", raw.get("index"))
        locator = f"{category}/应答{number}"
        questions.append(QuestionItem(
            question_id=build_identity("question", source_key, locator),
            question_type=type_code,
            stem=raw["text"],
            source_locator=locator,
            question_number=number,
            section=_section_of(category),
            stimulus_id=None,
            resolution_state=ResolutionState.DRAFT,
        ))

    diagnostics = ["listening_response_expected_answer_not_extracted"]
    numbers = [q.question_number for q in questions if q.question_number is not None]
    if len(set(numbers)) != len(numbers):
        diagnostics.append("duplicate_question_number")
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(q.source_locator for q in questions),
        entities=tuple(questions),
        confidence=_confidence(diagnostics),
        diagnostics=tuple(diagnostics),
        capabilities=dict(CAPABILITIES_AUDIO_ONLY),
    )


def _extract_info_retelling(result: dict, source_key: str) -> ParseCandidate:
    """信息转述及询问：转述材料 + 询问信息小题（业务字段通道）。

    ``result["tasks"]`` 由解析器提供：retelling 任务携带转述参考答案；
    asking 任务（题干+参考应答字段完整）→ ``asking_info`` QuestionItem，
    ``resolution_state=CANDIDATE``（待人工确认），是首个可进外部链路的
    完整小题候选。
    """
    type_code = QUESTION_TYPE_CODES["信息转述及询问"]
    stimuli = []
    for raw in result["items"]:
        category = raw.get("category", "信息转述录音稿")
        locator = f"{category}/录音稿{raw.get('index')}"
        stimuli.append(Stimulus(
            stimulus_id=build_identity("stimulus", source_key, locator),
            sub_type_code=type_code,
            stimulus_type="listening_script",
            text=raw["text"],
            source_locator=locator,
            section=_section_of(category),
            resolution_state=ResolutionState.DRAFT,
        ))

    questions = []
    for raw in result.get("tasks", []):
        if raw.get("task_kind") != "asking":
            continue
        locator = f"询问信息/问题{raw['number']}"
        reference = raw.get("reference_answer")
        questions.append(QuestionItem(
            question_id=build_identity("question", source_key, locator),
            question_type="asking_info",
            stem=raw["prompt"],
            source_locator=locator,
            question_number=raw["number"],
            answer=Answer("spoken_response", reference) if reference else None,
            section="询问信息",
            resolution_state=ResolutionState.CANDIDATE,
        ))

    diagnostics = []
    if not any(t.get("task_kind") == "retelling" for t in result.get("tasks", [])):
        diagnostics.append("info_retelling_reference_not_extracted")
    if not questions:
        diagnostics.append("info_retelling_task_split_not_extracted")
        diagnostics.append("audio_only_candidate")
        capabilities = dict(CAPABILITIES_AUDIO_ONLY)
    else:
        capabilities = dict(CAPABILITIES_BUSINESS_COMPLETE)
    entities = tuple(stimuli) + tuple(questions)
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(e.source_locator for e in entities),
        entities=entities,
        confidence=_confidence(diagnostics),
        diagnostics=tuple(diagnostics),
        capabilities=capabilities,
    )


def _extract_listening_record_retelling(result: dict, source_key: str) -> ParseCandidate:
    """听后记录并转述信息：第一节听力短文映射为一个 Stimulus。

    该题型目前只要求生成听力材料音频；第二节的转述开头与参考答案
    不属于音频正文，因此保持 audio_only，不虚构口语作答题字段。
    """
    type_code = QUESTION_TYPE_CODES["听后记录并转述信息"]
    stimuli = []
    for raw in result["items"]:
        category = raw.get("category", "听后记录并转述信息录音稿")
        locator = f"{category}/录音稿{raw.get('index')}"
        stimuli.append(Stimulus(
            stimulus_id=build_identity("stimulus", source_key, locator),
            sub_type_code=type_code,
            stimulus_type="listening_script",
            text=raw["text"],
            source_locator=locator,
            section=_section_of(category),
            resolution_state=ResolutionState.DRAFT,
        ))

    diagnostics = ("audio_only_candidate",)
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(stimulus.source_locator for stimulus in stimuli),
        entities=tuple(stimuli),
        confidence=_confidence(diagnostics),
        diagnostics=diagnostics,
        capabilities=dict(CAPABILITIES_AUDIO_ONLY),
    )


def _extract_imitation_reading(result: dict, source_key: str) -> ParseCandidate:
    """模仿朗读：每篇文章/段落一条 Stimulus（reading_passage）。

    方案 4 节要求“按产品定义的朗读任务拆分”；一篇文章即一个朗读任务，
    任务级的题干/评分维度未抽取，保持 audio_only。
    """
    type_code = QUESTION_TYPE_CODES["模仿朗读"]
    stimuli = []
    ordinal_by_group = {}
    for raw in result["items"]:
        category = raw.get("category", "模仿朗读")
        unit = raw.get("unit") or "无单元"
        key = (category, unit)
        ordinal_by_group[key] = ordinal_by_group.get(key, 0) + 1
        locator = f"{category}/{unit}/朗读{ordinal_by_group[key]}"
        stimuli.append(Stimulus(
            stimulus_id=build_identity("stimulus", source_key, locator),
            sub_type_code=type_code,
            stimulus_type="reading_passage",
            text=raw["text"],
            source_locator=locator,
            section=category,
            material_source=raw.get("source"),
            resolution_state=ResolutionState.DRAFT,
        ))

    diagnostics = ["imitation_reading_task_details_not_extracted", "audio_only_candidate"]
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(s.source_locator for s in stimuli),
        entities=tuple(stimuli),
        confidence=_confidence(diagnostics),
        diagnostics=tuple(diagnostics),
        capabilities=dict(CAPABILITIES_AUDIO_ONLY),
    )


TEXT_READING_KINDS = {
    "语篇跟读": "text_reading_discourse",
    "段落跟读": "text_reading_paragraph",
    "句子跟读": "text_reading_sentence",
}

VOCABULARY_ENTRY_KINDS = {
    "单词": "word",
    "例句": "example_sentence",
}


def _extract_text_reading(result: dict, source_key: str) -> ParseCandidate:
    """课文跟读：逐条目映射为 ContentUnit（LEARNING_CONTENT）。

    语篇/段落跟读条目是语篇/段落内的句子，阶段 1 不臆测父级分组，
    结构号原样保留；部分文档 sentence_number 缺失时按条目顺序定位。
    """
    type_code = QUESTION_TYPE_CODES["课文跟读"]
    units = []
    locators = []
    ordinal_by_group = {}
    for raw in result["items"]:
        category = raw.get("category", "")
        kind = TEXT_READING_KINDS.get(category)
        if kind is None:
            continue
        section = raw.get("section") or None
        discourse = raw.get("discourse_number")
        sentence = raw.get("sentence_number")
        group = (category, section)
        ordinal_by_group[group] = ordinal_by_group.get(group, 0) + 1
        if discourse is not None and sentence is not None:
            tail = f"语篇{discourse}-句{sentence}"
        elif sentence is not None:
            tail = f"句{sentence}"
        else:
            tail = f"条目{ordinal_by_group[group]}"
        locators.append(f"{category}/{section or '无章节'}/{tail}")

    deduped, duplicated = _dedupe_locators(locators)
    for raw, locator in zip(result["items"], deduped):
        category = raw.get("category", "")
        if category not in TEXT_READING_KINDS:
            continue
        units.append(ContentUnit(
            content_unit_id=build_identity("content", source_key, locator),
            content_kind=TEXT_READING_KINDS[category],
            text=raw["text"],
            source_locator=locator,
            section=raw.get("section") or None,
            discourse_number=raw.get("discourse_number"),
            sentence_number=raw.get("sentence_number"),
            resolution_state=ResolutionState.DRAFT,
        ))

    diagnostics = []
    if duplicated:
        diagnostics.append("duplicate_content_locator")
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(u.source_locator for u in units),
        entities=tuple(units),
        confidence=_confidence(diagnostics),
        diagnostics=tuple(diagnostics),
        capabilities=dict(CAPABILITIES_AUDIO_ONLY),
    )


def _extract_vocabulary(result: dict, source_key: str) -> ParseCandidate:
    """词汇：单词/例句逐条映射为 ContentUnit（LEARNING_CONTENT）。"""
    type_code = QUESTION_TYPE_CODES["词汇"]
    units = []
    locators = []
    for raw in result["items"]:
        category = raw.get("category", "")
        if category not in VOCABULARY_ENTRY_KINDS:
            continue
        locators.append(f"{category}/词条{raw.get('number')}")
    deduped, duplicated = _dedupe_locators(locators)
    for raw, locator in zip(result["items"], deduped):
        category = raw.get("category", "")
        if category not in VOCABULARY_ENTRY_KINDS:
            continue
        # 优先用解析器提供的行级定位（工作表/Sheet/行/N），
        # 旧解析结果无该字段时回退到自造定位
        entity_locator = raw.get("source_locator") or locator
        units.append(ContentUnit(
            content_unit_id=build_identity("content", source_key, entity_locator),
            content_kind="vocabulary",
            text=raw["text"],
            source_locator=entity_locator,
            entry_kind=VOCABULARY_ENTRY_KINDS[category],
            entry_number=raw.get("number"),
            resolution_state=ResolutionState.DRAFT,
        ))

    diagnostics = []
    if duplicated:
        diagnostics.append("duplicate_content_locator")
    return ParseCandidate(
        candidate_id=f"candidate:{type_code}:{source_key}",
        type_code=type_code,
        claimed_blocks=tuple(u.source_locator for u in units),
        entities=tuple(units),
        confidence=_confidence(diagnostics),
        diagnostics=tuple(diagnostics),
        capabilities=dict(CAPABILITIES_AUDIO_ONLY),
    )


# key = 大题型 family code（QUESTION_TYPE_CODES 的值，唯一标识）。
# 注册了 family 但未写抽取器时 extract_candidate 温和降级（诊断占位），
# 不抛错——新增小题型/大题型逐步接入不阻塞录入（方案目标 5）。
EXTRACTORS = {
    "info_acquisition": _extract_info_acquisition,
    "listening_choice": _extract_listening_selection,
    "listening_response": _extract_listening_response,
    "info_retelling": _extract_info_retelling,
    "listening_record_retelling": _extract_listening_record_retelling,
    "imitation_reading": _extract_imitation_reading,
    "text_reading": _extract_text_reading,
    "vocabulary": _extract_vocabulary,
}


def extract_candidate(doc_type: str, result: dict, source_key: str) -> ParseCandidate:
    """把单个题型解析结果映射为原子小题候选。

    ``doc_type`` 接受中文题型名或 family code（经注册表归一）；
    ``source_key`` 是文档的稳定业务键（阶段 1 暂用文件名）。

    注册了 family 但尚未实现抽取器：返回带诊断的空候选（不抛错）；
    完全未注册的题型：抛 KeyError（识别失败应显式暴露）。
    """
    from .model import FAMILY_BY_NAME, FAMILY_REGISTRY

    family_code = doc_type
    if family_code not in FAMILY_REGISTRY:
        family_code = FAMILY_BY_NAME.get(doc_type, doc_type)
    if family_code not in FAMILY_REGISTRY:
        raise KeyError(f"未注册的大题型: {doc_type}")
    extractor = EXTRACTORS.get(family_code)
    if extractor is None:
        return ParseCandidate(
            candidate_id=f"candidate:{family_code}:{source_key}",
            type_code=family_code,
            diagnostics=("family_extractor_not_registered",),
            capabilities=dict(CAPABILITIES_AUDIO_ONLY),
        )
    return extractor(result, source_key)
