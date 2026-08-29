# Word 解析原子小题模型改造方案

状态：方案草案（复审修订版）

## 1. 背景与结论

当前 Word 解析采用“大题/题型 Parser + Parser 内部固定拆分”的方式：

- `question_types` 中注册的是“信息获取、听后选择、听后应答、课文跟读、信息转述及询问、模仿朗读”等大类。
- Parser 输出的 `items` 粒度不一致：有的小题、有的整段录音稿、有的文章/教材块。
- `work_items` 同时承担解析结果、TTS 生成目标、进度统计和失败重试。
- `db/migrations/0005_external_records.sql` 已经建立 `external_records`、`external_operations`、`external_record_bindings` 和租约表；`workflow/external.py` 已实现 payload hash、回执、对账、人工确认、租约和幂等协调，并已有测试覆盖。
- 但 0005 外部记录运行时还没有接入讯飞生成链路。当前讯飞实际按“作品”运行：composite 模式把最多 120 个 `work_items` 打包成一个 `worksId`，旧链路的关联保存在 `progress.json` 的 `xunfei_works_ids` 中。
- 当前同时存在两条执行路径：workflow 引擎使用数据库中的 `work_items`/工作流状态；旧 UI 使用 `wordtts/progress.py` 把 `parse_results` 直接转换成 `progress.json`。两条路径不能继续各自维护解析和状态事实。
- 现有 workflow 数据库已经有 `workflow_steps`、`step_attempts`、`work_units`、`work_unit_items`、`provider_submissions`、`artifacts` 和事件表。它们已经承担执行状态、批次、尝试、Provider 回执和产物关系，不能在其旁边再造一套平行的操作状态机。

如果外部系统以“小题”为基础数据单元，继续沿用当前模型会导致一个本地 item 可能对应多个外部小题，无法可靠地完成单题编辑、同步、重试、去重和状态对账。

本方案的核心结论是：

> 所有能力共用一套标准流程和一份规范化数据。最小业务小题与共享材料组成统一事实模型；音频生成、外部录入只是同一工作流下的不同操作类型，使用同一身份、版本、范围、状态和审计机制。现有 `work_items` 和 `progress.json` 先作为兼容投影保留，但不再作为新的业务事实源。

本次复审进一步冻结以下实现方向，后续章节中的“建议”均以此为准：

- 一个 `OperationPlan` 默认对应一个 `workflow_group` 和一个本次执行的 `workflow`，使用统一的 `operation-plan` workflow definition；音频、外部录入和校验作为同一运行中的不同 `OperationTask`，不拆成互不感知的两条运行链。
- 一个规范化 `OperationTask` 统一映射一个 `workflow_step`。由于现有 `workflow_steps.scope` 只有 `workflow`/`item`，首期规范化任务统一使用 `scope=workflow`，实际小题、材料和版本通过不可变的 target snapshot 表表达；现有 `scope=item` 只保留给旧 TTS 投影兼容。
- `OperationScope` 的 `scope_members` 是任务目标的唯一事实源；`OperationTask` 中的 `question_ids`、`target_ids` 只能是查询投影，不能被前端单独修改。
- 内容模型首期采用专用实体表：`question_items`、`question_revisions`、`question_parts`、`stimuli`、`stimulus_revisions`、`question_stimuli`、`major_sections`、`question_groups` 和用于非考试内容的 `content_units`。不以无约束的多态 `metadata_json` 替代这些关系。
- 外部映射采用新增目标关联表的方案：保留 0005 现有表及旧 `item_id` 兼容列，新任务使用 `external_record_targets` 和 `external_operation_targets` 保存小题/范围目标及提交快照。
- `LEGACY_OUT_OF_BAND` 是迁移分类，不是新的 workflow 状态；旧路径是否已桥接通过 legacy session/来源元数据表达。

## 2. 目标与非目标

### 2.1 目标

1. 每个需要录入外部系统的小题拥有独立、可定位、可版本化的 `QuestionItem`。
2. 一份听力稿或阅读文章可以被多个小题引用，不重复复制材料正文。
3. 音频生成和外部录入共用同一套解析、核对、计划、执行、校验和交付流程。
4. TTS 可以继续按录音稿、句子或合并批次生成；外部系统也可以按小题、题组或大题操作，但两者不再使用两套基础数据。
5. 新增小题型只需增加抽取规则和类型定义，不需要改动所有下游流程。
6. 旧文档、旧任务和已有音频可以通过兼容投影继续使用。
7. 所有新操作都进入同一个 workflow 执行边界；兼容文件只能是投影或受控导入，不得再直接创建新的业务状态。

### 2.2 非目标

- 本方案不规定具体外部系统的浏览器/API 业务实现；阶段 4 只实现统一 runner、能力校验、0005 接线和 fake adapter 契约，真实外部系统适配器另按该契约接入。
- 不立即删除或重写当前 TTS `work_items` 流程。
- 不立即删除 `category`；本轮先把它降级为兼容/展示属性，并通过显式命名和音色策略字段逐步替代其业务含义。
- 不新增一套与现有 `workflow_steps`/`step_attempts`/`work_units` 并行的操作状态机；`OperationPlan`、`OperationTask`、`OperationScope` 首先作为领域概念和持久化目标关系，执行状态仍由现有 workflow 表负责。
- 不把所有内容强行拆成句子；“最小”指外部业务单元，不必等于最短文本或最短音频。
- 不根据前端展示需要反推解析结构；结构事实必须由后端解析结果提供。

## 3. 目标领域模型

```text
SourceDocument
  └─ DocumentRevision
       ├─ Assessment / ContentSet
       │    └─ MajorSection             大题/章节
       │         ├─ Stimulus             共享材料：听力稿、文章、对话
       │         │    └─ QuestionItem    最小业务小题，可有多个
       │         └─ QuestionItem         不依赖共享材料的小题
       ├─ ContentUnit                    非考试学习内容
       └─ OperationPlan                  同一规范数据上的操作计划
            ├─ OperationTask(AUDIO)      音频生成/校验/交付
            └─ OperationTask(EXTERNAL)   外部录入/校验/交付
```

### 3.0 领域模型与现有 workflow 表的关系

`OperationPlan`、`OperationTask`、`OperationScope` 是业务规划层的概念，不等于必须新建三套状态表。首期实现采用“一个事实源、一个执行状态机”的映射：

| 领域概念 | 首期落地方式 | 权威状态来源 |
| --- | --- | --- |
| `SourceDocument` / `DocumentRevision` | 新增文档身份与版本实体，关联现有 source import/artifact | 规范化内容模型 |
| `QuestionItem` / `Stimulus` / `ContentUnit` | 新增规范化业务实体和版本关系 | 规范化内容模型 |
| `OperationPlan` | 一次操作意图；映射到一个 `workflow_group` 和一个本次执行的 `workflow`，使用统一 `operation-plan` workflow definition | workflow group/run |
| `OperationTask` | 逻辑操作身份；一对一映射到一个 `workflow_step`，操作类型保存在任务实体，step 使用 `scope=workflow` | `workflow_steps.status` |
| `OperationAttempt` | 执行尝试 | `step_attempts` / `work_unit_attempts` |
| `OperationScope` | 冻结的目标集合和版本；音频投影可继续使用 assignment/work-unit 成员关系 | scope 成员快照 + workflow 关系 |
| `DeliveryUnit` | Provider 实际提交批次，例如单段或 composite 作品 | `work_units` / `provider_submissions` |
| `AudioArtifact` | 音频及切割结果 | `artifacts` / Provider receipt |
| `ExternalOperation` | 外部副作用事实 | `external_records` / `external_operations` |

首期新增物理 `operation_tasks` 表，但不得让 `operation_tasks.status` 和 `workflow_steps.status` 同时可写。该表只保存逻辑身份、操作类型、scope revision、输入/配置 hash 和到 `workflow_step` 的一对一关联；状态、Attempt、租约、事件和终态仍由现有 workflow 表维护。`workflow_step.step_type=OPERATION_TASK` 只表示执行节点，具体操作类型以 `operation_tasks.operation_type` 为准。阶段 0 必须冻结这个映射，否则“统一流程”会变成两套状态同步。

首期的具体映射固定如下：

```text
OperationPlan
  └─ workflow_group(workflow_type=operation-plan)
       └─ workflow(一次接受/执行的不可变运行)
            ├─ workflow_step(scope=workflow, step_type=OPERATION_TASK)
            │    └─ operation_task(operation_type=AUDIO_GENERATE|EXTERNAL_UPSERT)
            │         └─ operation_scope_revision + operation_task_targets
            └─ workflow_step_dependencies
```

现有 schema 的 `workflow_type` 只有“非空”约束，因此可以新增并发布 `operation-plan` definition；不能直接复用只支持 TTS 的旧 definition。混合音频/外部计划必须在同一个 `operation-plan` workflow 中表达，只有重新规划或需要独立生命周期时才创建新的 plan/run。现有 `workflow_steps.scope=item` 仅用于旧 TTS 投影；规范化任务即使目标只有一个小题，也通过 workflow-scoped step 加 target snapshot 表达。这样既不修改现有 step 状态机，又能支持任意已建模目标粒度。

### 3.0.1 统一执行器契约

统一流程必须落到一个 runner，而不是只统一数据名。`OperationRunner` 负责状态、租约、Attempt、预算和审计；`OperationAdapter` 只负责目标转换和外部能力。适配器不得直接写 `workflow_steps.status`。

```text
plan/validate（纯函数）
  → prepare（生成 payload/DeliveryUnit，不产生外部副作用）
  → record_intent（事务内写 step_attempt、目标快照和 side-effect intent）
  → execute（事务外调用 Provider/外部系统，携带 fencing token）
  → observe/verify（幂等保存回执或 Artifact）
  → reconcile（不确定时查询/人工确认）
  → finalize（由 runner 统一推进 step/attempt 状态）
```

最小接口约定：

```python
class OperationAdapter(Protocol):
    operation_type: str

    def capabilities(self) -> CapabilityMatrix: ...
    def validate(self, target_snapshot, config) -> ValidationResult: ...
    def prepare(self, target_snapshot, config) -> PreparedOperation: ...
    def execute(self, prepared, fencing_token) -> SubmissionReceipt: ...
    def verify(self, submission) -> VerificationResult: ...
    def reconcile(self, submission) -> ReconcileResult: ...
```

数据库事务只覆盖意图、Attempt、目标快照和本地状态转换；Provider/外部调用必须在事务外执行，并通过 `attempt_id`、`operation_key`、`fencing_token` 和幂等键回写。适配器返回 `SUCCEEDED`、`RETRYABLE_FAILED`、`PERMANENT_FAILED`、`AMBIGUOUS` 或 `WAITING_USER` 等结果，由 runner 映射到已有 `workflow_steps`/`step_attempts` 状态；外部 `side_effect_state` 和 Provider receipt 仍保留在各自事实表。

现有 `WorkflowEngine.run_tts` 在首期作为 `AUDIO_GENERATE` 适配器的兼容实现：统一 runner 先创建规范化任务和目标快照，再将音频目标投影为 `work_items`/assignments，最后调用现有 TTS engine。`ExternalRecordService` 也必须被包装为 `EXTERNAL_UPSERT` 适配器，不能由 UI 或独立服务绕过 runner 直接提交。

### 3.1 QuestionItem：外部系统的基础单元

建议字段：

```json
{
  "question_id": "question:<stable-source>:<locator>",
  "question_revision_id": "question-revision:<id>",
  "document_revision_id": "document-revision:<id>",
  "identity_version": "1",
  "unit_kind": "QUESTION",
  "question_type": "listening_choice",
  "question_type_version": "1",
  "major_type": "信息获取",
  "section": "听选信息",
  "question_number": 1,
  "sub_number": null,
  "stem": "What is Amy doing?",
  "options": [
    {"option_id": "A", "text": "Reading."},
    {"option_id": "B", "text": "Singing."},
    {"option_id": "C", "text": "Running."}
  ],
  "answer": {"kind": "single_choice", "value": "A"},
  "stimulus_id": "stimulus:<stable-source>:dialogue-1",
  "source_locator": "第一节听选信息/对话1/题目1",
  "resolution_state": "CONFIRMED",
  "content_hash": "...",
  "metadata": {}
}
```

字段原则：

- `question_type` 表示可扩展的业务小题型，不使用展示用的中文 `category` 代替。
- `major_type`、`section` 保留原文层级，方便筛选和回溯，但不作为小题唯一身份。
- `question_number` 是文档中的题号；没有题号时由解析器生成结构定位，并标记 `number_inferred=true`。
- `question_id` 表示跨版本的逻辑小题，`question_revision_id` 表示本次解析得到的不可变内容版本；内容变化不能直接覆盖旧版本。
- `stem`、`options`、`answer` 与材料正文分离，不能把整段 Word 文本塞进一个 `text` 字段后再由下游猜测。选项必须有稳定的 `option_id`，答案必须有明确的答案类型，不能只保存一个无法解释的字符串。
- `source_locator` 必须能定位到原文段落、表格、章节或结构块。
- 正文变化只改变 `content_hash`/修订，不应静默覆盖另一个已有小题。
- 复合题、多空题和子问不能只依赖 `sub_number`；需要 `question_parts` 或父子 `QuestionItem` 关系，并为每个部分保存题干、选项、答案和顺序。

`resolution_state` 只描述内容事实是否已经确认，不承担音频或外部副作用状态。建议至少包括 `DRAFT`、`CANDIDATE`、`AMBIGUOUS`、`UNRESOLVED`、`CONFIRMED`、`REJECTED`。

### 3.2 Stimulus：共享材料单元

用于表达一个或多个小题共同引用的听力稿、对话、阅读文章、图片说明或其他背景材料。

建议字段：

```json
{
  "stimulus_id": "stimulus:<stable-source>:dialogue-1",
  "stimulus_revision_id": "stimulus-revision:<id>",
  "document_revision_id": "document-revision:<id>",
  "stimulus_type": "listening_script",
  "text": "W: ...\\nM: ...",
  "section": "听选信息",
  "source_locator": "第一节听选信息/对话1/录音稿",
  "media_artifact_ids": [],
  "content_hash": "..."
}
```

一个 `Stimulus` 可以被多个 `QuestionItem` 引用。这样“一个对话回答两道题”不会被错误地当作一道题，也不会复制两份录音稿。`question_stimuli` 关系需要保存 `relation_type`、`ordinal` 和可选的材料片段定位，不能只有一对多的裸外键。

`ContentUnit` 首期只承载非考试学习内容；考试题目和共享材料分别使用 `QuestionItem`、`Stimulus` 专用实体，不通过一个无约束的多态父表表达。课文跟读等内容统一标记为 `unit_kind=LEARNING_CONTENT`，仍可作为 `OperationScope` 的目标进入相同操作流程，而不必伪装成考试题目。

### 3.3 OperationTask：统一下游操作单元

音频生成和外部录入不再各自建立一套基础数据，而是在同一份规范化小题/材料图上创建不同的操作任务。两类操作共用：

- `scope_id + scope_revision`：操作的不可变目标范围；`scope_kind`、`target_ids` 和 `question_ids` 仅为从 scope members 展开的只读投影；
- `identity_version`、`content_hash` 和 `configuration_hash`：输入与配置版本；
- 统一的执行状态引用、Attempt、retry、receipt 和审计事件；具体状态由现有 workflow step/attempt 维护，外部 side-effect 状态仍由对应的 Provider/External 事实维护；
- `artifact` 和 `source_locator`：操作产物及其原文来源。

只有执行适配器和产物类型不同：

- 音频操作可以把共享听力稿作为一个目标、把题干按题目生成，或按句子/段落生成；
- 外部操作把同一批规范化小题转换成外部系统需要的 payload；
- 课文跟读等非考试内容仍使用相同流程，但操作类型和目标类型标记为内容单元。

建议字段：

```json
{
  "operation_id": "operation:<type>:<stable-source>:dialogue-1",
  "operation_plan_id": "plan:<stable-source>:<revision>",
  "operation_type": "AUDIO_GENERATE",
  "scope_id": "scope:<operation>:dialogue-1",
  "scope_revision": 1,
  "scope_kind": "STIMULUS",
  "target_ids": ["stimulus:...:dialogue-1"],
  "question_ids": ["question:...:1", "question:...:2"],
  "target_revision_ids": ["stimulus-revision:..."],
  "input_hash": "...",
  "configuration_hash": "...",
  "adapter_version": "1",
  "depends_on_operation_ids": [],
  "output_artifact_ids": []
}
```

这里 `scope_id + scope_revision` 是唯一目标引用；`scope_kind`、`target_ids`、`question_ids` 和 `target_revision_ids` 是从 `operation_scope_members` 展开的只读投影。创建或更新任务时只能提交 scope revision，不能同时提交几组可能互相矛盾的 ID 数组。若任务只针对一个小题，也必须先生成 `QUESTION` scope；不能把某个 `work_item.item_id` 直接当作规范化目标。

`OperationTask` 的 `operation_type` 至少包括 `AUDIO_GENERATE` 和 `EXTERNAL_UPSERT`。`work_items` 可以继续作为音频操作的兼容落库/执行投影；外部录入和音频生成都应从统一规范化模型创建操作任务，不直接把 `category` 当作业务事实。

`OperationTask` 的身份、执行尝试和 Provider 批次必须分开：同一个逻辑任务可以有多个 Attempt，同一个 Attempt 可以因为 Provider 限制产生一个或多个 `DeliveryUnit`。任务的成员集合和输入版本一旦进入执行就冻结；成员或版本变化必须生成新的任务，不得复用旧的 side-effect key。

统一执行关系固定为：

```text
OperationTask
  → workflow_step（唯一可写的任务状态）
      → step_attempt
          → DeliveryUnit / work_unit
              → ProviderSubmission 或 ExternalOperation
                  → Artifact / Receipt / ReconcileEvidence
```

如果外部系统需要音频地址或音频作品 ID，`EXTERNAL_UPSERT` 必须显式依赖相应的音频任务或 Artifact；如果外部系统不需要音频，则由适配器能力声明允许并行执行。依赖、失败传播和部分成功规则必须在计划中落库，不能由前端临时决定。

### 3.4 层级关系与可配置操作粒度

原子小题是事实源，但不是唯一的操作粒度。需要额外保留可选的题组关系，并在统一工作流中生成不同粒度的 `OperationScope`（操作范围）。音频生成和外部录入都使用同一个范围模型。

```text
SourceDocument
  └─ MajorSection
       └─ QuestionGroup（可选：一段材料/一组题）
            ├─ Stimulus（可选共享材料）
            ├─ QuestionItem 1
            └─ QuestionItem 2

OperationScope（不可变范围快照，不复制业务事实）
  ├─ QUESTION       → 单个 QuestionItem
  ├─ GROUP          → 一个 QuestionGroup 内的小题集合
  ├─ STIMULUS       → 材料及其关联小题
  ├─ MAJOR_SECTION  → 一个大题/章节下的小题集合
  ├─ DOCUMENT       → 整份文档的小题集合
  └─ CONTENT_UNIT   → 非考试学习内容集合
```

`OperationScope` 建议至少包含：

```json
{
  "scope_id": "scope:<integration>:<scope_kind>:<stable-key>",
  "scope_kind": "QUESTION",
  "scope_revision": 1,
  "question_ids": ["question:...:1"],
  "stimulus_ids": ["stimulus:...:dialogue-1"],
  "major_section_id": "section:...:listening-choice",
  "member_hash": "...",
  "payload_hash": "..."
}
```

生产实现不能只依赖 JSON 数组。应增加 `operation_scopes` 和 `operation_scope_members`，或使用等价的不可变范围快照，至少保存目标类型、目标 ID、目标版本、顺序和成员关系。`OperationTask` 必须引用具体的 scope revision；范围成员发生变化时创建新 revision，不能原地修改正在执行的范围。

粒度规则：

- `QuestionItem`、`Stimulus`、`MajorSection`、`ContentUnit` 是解析事实；`OperationScope` 是面向不同操作适配器的不可变目标范围快照。
- 同一批小题可以按小题逐条操作，也可以按题组、材料、大题或文档聚合操作，不复制或改变小题事实。
- 默认外部录入粒度为 `QUESTION`；音频可以根据边界策略选择 `QUESTION`/`STIMULUS`/`GROUP`；只有目标系统明确支持更大对象时，才启用 `MAJOR_SECTION`/`DOCUMENT`。
- 每一种 `scope_kind` 都要有独立的稳定业务键、payload hash、状态、幂等和回执关联，不能用某一个小题的 `question_id` 冒充整组提交的身份。
- 一个材料关联多个小题时，`STIMULUS` 范围可以携带共享材料和题目列表；如果外部系统只接受单题，则适配器把同一 `stimulus_id` 关联到多个 `QUESTION` 范围。
- “任意颗粒度”应理解为在已建模层级和外部系统能力内可配置的粒度；对于解析器没有识别出的更细结构，必须先补充解析规则，不能由聚合层猜测。

- 适配器必须暴露能力矩阵：支持的 `scope_kind`、是否允许一对多成员、是否支持部分成功、是否需要音频 Artifact。计划阶段发现范围不支持时直接阻断，不得进入有副作用的提交阶段。

### 3.5 内容、操作和副作用状态分层

三类状态不能共用一个字段：

- `resolution_state`：解析出的内容事实是否已经确认，由 `QuestionItem`/`Stimulus` 持有；
- `operation_status`：统一任务处于待执行、执行中、重试、等待人工、成功或失败，由现有 `workflow_steps`/`step_attempts` 持有；
- `side_effect_state`：讯飞或其他外部系统的提交副作用是否已确认，由现有 `work_units`/Provider receipt 或 `external_operations` 持有。

内容为 `AMBIGUOUS`/`UNRESOLVED` 时不能创建可提交的操作任务；外部提交为 `AMBIGUOUS` 时不能倒写成内容歧义，也不能自动重新提交。`UNRESOLVED` 不是现有外部操作表的状态值，不能把它直接写入 `external_operations`。

## 4. 当前题型到目标模型的映射

当前题型 Parser 的输出是“音频抽取结果”，不等于完整的外部业务小题。阶段 1 只能把已有事实安全地带入规范化模型，不能凭 `category` 或录音稿文本反推题干、选项、答案和材料归属。只有完成题型级业务抽取并达到 `resolution_state=CONFIRMED`，才允许创建 `EXTERNAL_UPSERT`。

| 当前题型 | 目标 QuestionItem | 目标 Stimulus | 统一流程中的音频操作 |
| --- | --- | --- | --- |
| 信息获取 / 听选信息 | 每个编号小题一条 | 每段录音稿一条 | 录音稿可整段；题干可逐题 |
| 信息获取 / 回答问题 | 每个问题一条 | 对应独白/录音稿 | 材料与问题分别配置 |
| 听后选择 | 每个编号选择题一条，保存选项 | 每块录音原文一条 | 一块对话可保持一个音频 |
| 听后应答 | 每个待应答句一条 | 通常只需保存提示上下文 | 每句一个音频 |
| 信息转述及询问 | 按实际转述/询问任务拆分 | 录音稿或图片等材料 | 可按任务或材料生成 |
| 模仿朗读 | 按产品定义的朗读任务拆分 | 外网/教材文章或段落 | 可整篇、分段或逐句 |
| 课文跟读 | 作为学习内容单元，不强行标成考试小题 | 文章/语篇可独立建模 | 句子/段落/语篇按现有规则 |

其中“题干、选项、材料、音频”是四种不同事实，不能继续只靠 `category + text` 隐含表达。

## 5. 解析流程改造

### 5.1 统一标准流程

```text
Word 文档
  → 原文结构读取（段落、表格、样式、编号、定位）
  → 结构分段（大题/章节/材料块）
  → 原子小题抽取
  → 材料与小题关联
  → 规范化与校验
  → QuestionItem / Stimulus 持久化
  → 创建 OperationPlan / OperationScope
  → 创建 OperationTask（AUDIO 或 EXTERNAL）
  → 映射到现有 workflow_step / attempt / delivery unit
  → 执行适配器
  → 校验结果、写入 Artifact、更新状态和审计
  → 交付音频或外部录入结果
```

音频生成和外部录入只在“操作类型、适配器、产物格式”上不同，不能从 Word 文档开始就分成两套解析和业务流程。两者都必须经过同一份规范化数据、同一套范围选择、同一套执行状态、重试、幂等和审计逻辑；执行状态最终只写现有 workflow 状态表，不在 `progress.json` 或另一套 `operation_tasks` 状态表中复制一份。

### 5.2 Parser 职责调整

当前每个 Parser 直接返回最终 TTS `items`。改造后建议分为结构解析、业务规范化和统一操作三个阶段：

1. **结构解析层**：一次读取文档，识别章节、材料块、题号、题干、选项、答案、原文定位和格式结构。结构读取器必须支持段落、表格/单元格和样式提示；当前只读取普通段落的 loader 不能直接作为最终实现。
2. **题型规则层**：将结构块转换为标准 `QuestionItem`、`Stimulus`、`ContentUnit` 和关联关系。只有输出了完整业务字段的题型才允许进入外部录入；仅有录音稿的题型只能生成音频操作或进入待补全状态。
3. **操作计划层**：根据同一份规范化数据创建 `AUDIO`、`EXTERNAL` 或其他操作任务，并映射到现有 workflow step/attempt/work-unit 执行模型。

每个题型模块仍然可以独立维护，但它只负责输出统一领域模型，不直接决定 TTS 或外部系统的字段。音频和外部录入的差异由操作适配器处理。阶段 1 的结构读取器只建立统一的原文事实和定位能力；在阶段 3 的共享分段器完成前，旧题型 Parser 仍可能独立扫描全文。

### 5.2.1 统一解析产物与题型裁决

结构层和题型层之间必须有明确的中间协议，不能继续直接把任意 `ParsedItem` 当作 `QuestionItem`。每个题型规则至少返回以下信息：

```json
{
  "candidate_id": "candidate:<id>",
  "type_code": "listening_choice",
  "type_version": "1",
  "claimed_block_ids": ["block:<id>"],
  "entities": ["QuestionItem|Stimulus|ContentUnit"],
  "confidence": 0.98,
  "diagnostics": [],
  "capabilities": {"question_fields_complete": true, "audio_only": false}
}
```

候选裁决规则固定为：显式题型标记优先于自动检测；同一结构块只能有一个题型规则成为 owner；多个规则同时命中且无法按优先级唯一裁决时，保留多个候选并写入 `AMBIGUOUS`，不能把两份结果都发布为小题；没有完整题干/选项/答案的候选只能标记 `audio_only` 或待补全。去重不能依赖最终文本相同，因为同文不同题可能文本相同，也不能依赖 `category + sequence`。

阶段 1 为兼容旧 Parser 可以暂时重复读取全文，但必须把每次扫描的原文范围、候选 owner、去重键和裁决结果写入诊断；阶段 3 的共享分段器完成后，结构读取和范围分发改为一次读取、一次 owner 裁决。新增题型的验收必须包含“与旧大题 Parser 同时启用时只产生一份业务结果”的负向测试。

### 5.3 解析不确定性

以下情况不能静默合并或猜测：

- 题号缺失且无法从上下文确定；
- 一个录音稿可能对应多个不同题组；
- 题干、选项和答案无法明确归属；
- 解析结果的题目数量与材料中的题量明显不一致。

应将不确定性落为内容模型的 `resolution_state=AMBIGUOUS` 或 `UNRESOLVED`，同时保存诊断信息和原文定位；用户确认后才生成可执行的 `OperationPlan`。如果只是外部副作用不确定，则使用外部操作已有的 `AMBIGUOUS`/人工对账流程，不能倒写内容状态。用户确认后，由操作类型决定后续是生成音频、录入外部系统，还是两者都执行。

## 6. 身份、版本与关联规则

### 6.1 身份分离

必须分开保存：

- `source_document_id`：业务文档或教材的逻辑身份，不随上传文件 hash 改变；
- `document_revision_id`：一次文档内容版本，关联 source artifact、解析器和 schema 版本；
- `question_id`：跨文档版本的业务小题逻辑身份；
- `question_revision_id`：该小题在某次文档版本中的不可变内容版本；
- `stimulus_id` / `stimulus_revision_id`：共享材料的逻辑身份和内容版本；
- `operation_id`：统一工作流中的逻辑操作身份；Attempt 和 Provider submission 另有自己的身份；
- `content_hash`：当前内容版本；
- `parser_version` / `schema_version`：解析算法和模型版本。

必须使用 `legacy_aliases` 保存旧 `work_items.item_id`、`progress.json.items[].id`、旧文件名和历史 `worksId` 对应的新身份。别名只能用于迁移和查询，不能重新成为业务主键。

不要继续使用“类别 + 顺序 + 文本 hash”同时承担业务身份、音频身份和缓存身份。

### 6.2 建议的身份定位

优先使用稳定来源绑定和结构定位，例如：

```text
<source_document_id>/<major_section>/<material_locator>/<question_local_key>
```

`source_document_id` 不能只使用本次上传文件的 SHA-256；如果用户修改了文档但仍然是同一份业务试卷，必须能通过来源绑定、稳定业务编号或用户确认维持关联。题号只能作为展示和初始匹配线索，不能单独决定逻辑身份。

文档发生插题、删除、重排、重编号或文本修改时，先执行“旧 revision → 新 revision”的匹配并产生差异报告：确认是同一小题则保留 `question_id`、创建新的 `question_revision_id`；无法证明是同一小题则创建新的逻辑身份并阻断外部复用。外部记录是更新原记录还是创建新记录，必须由集成策略显式决定，不能由 hash 变化自动推断。

### 6.2.1 Revision 匹配算法

身份匹配必须是可解释、可重放的两阶段算法，不能只写“做 diff”：

1. **确定性匹配**：优先匹配同一 `source_document_id` 下的显式业务编号/题目 ID；其次匹配上一版本保存的 `question_local_key`、`legacy_alias` 和稳定材料/题组定位。匹配条件必须包含 `type_code` 和所属结构范围，避免跨题型误合并。
2. **候选匹配**：没有确定键时，使用“题型 + 结构范围 + 规范化题干/选项指纹 + 材料关系 + 相邻结构锚点”生成候选。候选必须是一对一且超过固定阈值才可自动保留原 `question_id`；多个候选、低于阈值或跨范围匹配一律进入 `AMBIGUOUS`，等待人工确认。

匹配输出至少包括 `MATCHED`、`NEW`、`REMOVED`、`CHANGED`、`AMBIGUOUS` 五类差异，并持久化旧 revision、新 revision、算法版本、候选分数、裁决人和裁决时间。必须写入 `revision_match_decisions`，保证同一输入和算法版本重复运行得到相同结果。没有稳定来源键且没有人工确认时，不得因为题号相同或文本相似就复用 `question_id`。

`source_documents.logical_key` 必须在明确的业务范围/租户内唯一；上传文件 hash 只用于去重和回放，不承担来源所有权。`question_local_key` 是文档内稳定键：有显式题目 ID 时直接使用，没有时由解析器基于结构块和题型生成并在首个确认版本中固化。外部 upsert 只有在身份匹配已确认、目标 revision 已冻结且集成策略允许更新时才能复用原外部记录；身份歧义、题型变化或范围变化都必须先阻断并生成新操作。

### 6.3 现有 external_records 的演进决策

这一部分不是从零设计。`0005_external_records.sql` 和 `workflow/external.py` 已经提供外部记录、操作、绑定、租约、payload hash、回执、人工确认、幂等和对账能力，并已有测试覆盖；目前缺的是把它接到新的原子小题和实际执行链路上。

当前 0005 的本地关联仍指向 TTS 工作项：

- `external_records.local_item_id` 外键指向 `work_items.item_id`；
- `external_operations.item_id` 和 `external_record_bindings.item_id` 也指向 `work_items`；
- 外部运行时已经具备记录级串行、side-effect 状态和回执约束，但尚未具备 `QuestionItem`/`OperationScope` 目标类型。

同时要把外部业务记录与讯飞作品明确区分：讯飞 composite 产生的 `worksId` 是 Provider 作品/音频产物事实，旧链路保存在 `progress.json.xunfei_works_ids`；它不是 `external_records` 中的业务记录 ID，不能用作品 ID 代替小题外部记录映射。

#### D-EXT-001：0005 的小题目标映射方式（已冻结）

本方案冻结采用“新增目标关联表”，不再在阶段 0 重新二选一。原因是同一套标准流程必须同时支持单题、材料、题组和聚合提交，而 0005 的单个 `local_item_id` 只能表达旧 TTS 工作项，不能表达一对多目标和提交时成员快照。`external_records` 仍是外部业务记录主映射，不另建第二套外部事实源。

迁移策略是：保留 0005 的 `local_workflow_id`、`local_item_id`、`external_operations.item_id` 和 `external_record_bindings.item_id` 作为历史兼容列；新任务不再把这些列当作规范化目标来源，改由目标关联和 operation snapshot 表表达。旧列只读兼容，不能把 `work_item_id` 改名为 `question_id`，也不能破坏旧回执、租约和人工确认。

外部映射的目标语义应统一为：

```text
external_system + account_scope + business_record_key
    ↔ local_target_id + target_kind
```

其中 `target_kind` 至少区分 `QUESTION`、`STIMULUS`、`GROUP`、`MAJOR_SECTION` 和 `SCOPE`。payload hash、状态、回执、人工确认和重试预算继续复用现有 0005/`workflow/external.py` 实现；新增的是目标关联、原子小题 payload 构造和统一执行链路接线。

D-EXT-001 不能只停留在表名选择，还必须冻结以下约束：

- 一个外部业务记录允许关联一个还是多个本地目标；一个 `QuestionItem` 可以在不同外部系统或账号范围下有不同映射，但同一集成范围内只能有一个 active mapping；
- 目标关联同时保存逻辑身份、具体 target revision、ordinal 和 relation type，聚合 payload 的成员集合和顺序由 operation snapshot 冻结；
- `external_records` 的 `current_*` 字段只是旧兼容投影，历史 workflow/operation 仍通过绑定、目标快照和操作表追溯，不能因为换 revision 覆盖历史事实；
- `external_operations` 和 `external_record_bindings` 不再单靠 `item_id` 表达新聚合目标；新 operation 必须关联 `workflow_step_id`、`attempt_id` 和 operation-target snapshot，旧 `item_id` 只供历史读取；
- `ExternalRecordService.prepare_operation()` 需要从 `item_id` 扩展为显式 target/scope 输入，并把 scope revision、目标成员 hash、workflow step/attempt 关联到幂等键和回执；
- 目标表使用 `target_kind + target_id` 时，SQLite 不能用一个多态外键自动约束所有实体，必须由 repository 校验并配套按类型 trigger/完整性测试；不得用不存在的通用 FK 假装已保证引用完整性。
- payload 原文是否持久化、保存在哪里、谁可读取必须单独定义；回执和日志继续脱敏，不能把账号凭据或不必要的敏感内容写入 JSON、事件或 progress。

建议的目标关联最小结构为：

```text
external_record_targets(
  external_record_mapping_id, target_kind, target_id,
  target_revision_id, ordinal, relation_type, target_hash
)

external_operation_targets(
  external_operation_id, target_kind, target_id,
  target_revision_id, ordinal, result_status,
  payload_fragment_hash
)
```

并以 `(external_record_mapping_id, target_kind, target_id, target_revision_id, relation_type)` 或等价约束防止重复关联。对一条聚合外部记录，必须保存提交时的成员快照；单个成员发生变化时，先生成新的 payload/operation，再按集成策略更新整条记录或要求人工确认。新迁移还要为 `external_operations` 增加可回填的 `workflow_step_id`、`attempt_id`（历史行允许为空，新写入行必须有值），使外部副作用能沿统一执行链追溯到 Attempt。

`ExternalRecordService` 的新入口至少为 `prepare_operation(task_ref, target_snapshot, payload, mapping_version)`；旧的 `item_id` 参数只保留在兼容 wrapper 中。新入口必须先校验目标 revision、scope capability、幂等键和 active lease，再在本地事务中记录 intent，事务外提交，最后由 runner 通过 receipt/对账完成状态收敛。`business_record_key` 的生成由集成适配器声明：默认小题使用 `question_id`，聚合记录使用 `scope_id + scope_revision`，不能把 `worksId` 作为业务键。

### 6.4 两条运行时路径与权威边界

当前路径必须显式分层，不能把它们都当作并列事实源：

```text
规范化解析模型（唯一内容事实源）
        ├─ WorkflowRuntime → workflow step/attempt/work-unit/Artifact
        └─ Legacy UI       → audio OperationScope → progress.json（兼容缓存）
```

权威性约定：

- 新任务的内容、题目身份、材料关联和修订以规范化解析模型为准；
- 新任务的执行状态、Attempt、Artifact、外部回执和对账以 workflow 数据库为准；
- `progress.json` 只作为旧 UI/旧讯飞链路的音频兼容投影，不得创建新的题目身份、外部记录或独立状态事实；
- `progress.json.xunfei_works_ids` 只表示讯飞作品关联。它可以通过受控导入为 Provider 证据，但不能直接写入 `external_records`；
- 在旧 UI 收口前，它必须消费由规范化模型派生的音频操作投影，而不是继续直接把原始 `parse_results` 作为第二套解析入口；如果旧执行器暂时不能调用 workflow API，就必须作为受控 legacy adapter，在 Provider 调用前后写入同一套 workflow operation/attempt 事实；
- 外部录入只允许从 workflow 的统一 `OperationTask(EXTERNAL)` 发起，不从旧 `progress.json` 路径发起。

因此迁移期间不能同时存在“旧 UI 直接写进度并执行 Provider”和“workflow 数据库是唯一执行状态源”两个未桥接的事实源。推荐方案是旧 UI 只发起 workflow 命令并读取投影；在桥接完成前，旧会话必须标记为 `LEGACY_OUT_OF_BAND`，不得混入新 workflow 的状态统计或外部录入。`LEGACY_OUT_OF_BAND` 不是 `workflow_steps`、`step_attempts` 或 `work_items` 的状态值，而是 `legacy_execution_sessions`/迁移元数据中的来源分类，避免污染现有状态机。阶段 5 再宣布 `progress.json` 只读冻结或完成下线。

`progress.json` 投影必须带 `schema_version`、`projection_generation`、来源 `workflow_id`/`operation_plan_id` 和生成时间。数据库状态变化后由单向 projector 原子重建文件；文件缺失或 generation 过期时只能重新生成，不能把旧文件内容直接当作新状态。旧 UI 的人工修改只能进入受控 legacy import，生成差异和待确认事件，不能直接覆盖 workflow 或 external 事实。若 legacy adapter 仍负责 Provider 调用，必须先创建对应的 workflow step/attempt、写入 intent，调用完成后再回写 submission/receipt；否则会话保持 out-of-band，不纳入新链路成功率统计。

## 7. 数据库与兼容策略

### 7.1 推荐新增实体

首期冻结采用专用实体表，不再在实现阶段二选一：

- `source_documents` / `document_revisions`：业务文档逻辑身份、版本、来源 artifact 和解析版本；现有 `source_imports` 是上传过程，不替代业务文档身份。
- `major_sections`：大题/章节边界、顺序和 source locator。
- `question_groups`：题组/材料边界和顺序。
- `question_items`：原子业务小题逻辑身份及题型。
- `question_revisions`：题目内容版本、解析来源、差异和 `resolution_state`。
- `question_parts`：复合题、多空题、子问及其顺序。
- `stimuli` / `stimulus_revisions`：共享材料逻辑身份和内容版本。
- `question_stimuli`：小题与材料的多对多关联，保存角色、顺序和片段定位。
- `content_units` / `content_unit_revisions`：非考试学习内容，不把它伪装成 `QuestionItem`。
- `operation_plans`：一次统一操作意图及其 workflow group/run 关联。
- `operation_scopes` / `operation_scope_members`：不可变目标范围及成员快照。
- `operation_tasks`：逻辑操作身份、类型、scope 和 workflow step 一对一关联，不复制可写状态。
- `operation_task_dependencies`：音频、外部录入和校验之间的依赖关系。
- `operation_task_targets` / `delivery_unit_targets`：操作目标与音频 delivery unit 的明确关联，支持“一个材料一份音频、多个小题引用”。
- `revision_match_decisions`：文档 revision 间身份匹配、差异和人工裁决记录。
- `legacy_aliases` / `legacy_execution_sessions`：旧 `work_item`、progress item、文件名、worksId 和旧执行来源到新身份的迁移关系。

这些是首期规范化事实表，不再以“等价投影”替代。首期 schema 至少要明确以下字段和约束，而不是只停留在 JSON 示例：

| 实体 | 必要字段/关系 | 必要约束 |
| --- | --- | --- |
| `source_documents` | `source_document_id`、`logical_key`、所属业务范围、来源类型 | 同一业务范围内 `logical_key` 唯一 |
| `document_revisions` | `document_revision_id`、文档 ID、source artifact、文件 hash、parser/schema 版本 | revision 不可变；同一 artifact+版本可幂等重放 |
| `major_sections` / `question_groups` | 所属文档 revision、稳定 local key、ordinal、source locator | 同一 revision 内 local key 和 ordinal 受约束，范围不能交叉 |
| `question_items` | `question_id`、稳定 `type_code`、所属 document/source 范围、当前 revision 引用 | 逻辑身份与内容 revision 分离；同一来源范围内 identity 唯一 |
| `question_revisions` | `question_revision_id`、题目字段、content hash、`resolution_state`、source locator | 同一题目的 revision 不可覆盖；内容 hash 冲突必须报错 |
| `stimuli` / `stimulus_revisions` / `question_stimuli` | 材料版本、关系角色、ordinal、片段定位 | 支持一材料多题和一题多材料；材料 revision 不可覆盖 |
| `question_parts` | 父题、part key、ordinal、题干/选项/答案 | 复合题部件顺序稳定，不能依赖展示文本 |
| `content_units` / revisions | 内容类型、内容版本、source locator、content hash | 仅承载非考试内容，版本不可变 |
| `operation_plans` | plan identity、source/document revision、workflow group/run、configuration snapshot | 一个 plan 只能绑定一个 accepted run；重新规划产生新 plan |
| `operation_scopes` / members | scope revision、目标版本、成员顺序、member hash | 执行后成员快照不可变 |
| `operation_tasks` | 逻辑 operation ID、`operation_type`、scope、`workflow_step_id`、输入/配置 hash | 与 workflow step 一对一；不保存第二份可写状态 |
| `operation_task_targets` / delivery targets | 任务目标、目标 revision、目标角色、delivery unit | 一个 delivery unit 可服务多个题目，但关系可追溯 |
| `operation_task_dependencies` | 上游/下游 task、依赖类型、失败传播规则 | 防止外部任务在所需 Artifact 未就绪时提交 |
| `revision_match_decisions` | 旧/新 revision、候选、算法版本、裁决结果、操作者 | 同一输入和算法版本幂等；歧义不可自动通过 |
| `legacy_execution_sessions` | 来源分类、旧文件/会话、bridge version、导入状态 | `LEGACY_OUT_OF_BAND` 仅为来源分类，不进入 workflow 状态枚举 |

`metadata_json` 只能保存未结构化的扩展属性，不能承载题干、选项、答案、材料关系、目标成员或状态。题目和材料的 revision 必须通过显式表关联到对应的 `document_revision_id`。

不新增一套平行的外部记录事实源。已有 `external_records`、`external_operations`、`external_record_bindings` 和相关租约表按第 6.3 节已冻结的 D-EXT-001 演进：新增目标关联/操作快照表，保留旧 `item_id` 兼容列，并由 v0007 扩展 workflow step/attempt 关联。

`category` 不作为上述规范化模型的业务键。它只在兼容投影中保留；音色、文件名和展示分别使用显式的 `voice_policy`、`naming_policy` 和 `display_category`。题型注册表必须提供稳定的 `type_code`、版本、能力声明和默认策略，不能继续让 `category` 同时承担这些含义。

策略优先级必须固定并写入操作配置快照：小题级覆盖 > 文档/题型策略 > 系统默认；旧 `category` 只能作为兼容输入，不能反向覆盖已经冻结的操作配置。

题型注册表的具体落点是扩展现有 `QuestionType` 定义，至少增加 `type_code`、`type_version`、`capabilities`、`content_schema_version`、`default_voice_policy` 和 `default_naming_policy`。现有 `force_female_categories` 迁移为注册表/小题级的 `voice_policy.gender=FEMALE` 默认策略，并保留 category alias 读取兼容；未注册的旧 category 只能生成兼容音频结果，不能静默创建完整外部小题。迁移完成后，题型能力、音色、文件名和展示字段都必须有独立回归测试。

但长期仍推荐独立实体，避免把多个概念继续塞进 `work_items`。现有 `work_items` 仍是音频 delivery projection，不是 `QuestionItem` 的同义词。

### 7.1.1 迁移版本与发布顺序

现有 migration runner 要求版本连续、已应用 migration checksum 不可改变，并且 `2a` profile 目前停在 v4。因此新增迁移不能插入或修改 0001～0005，发布顺序固定为：

1. **v0006 `atomic_question_model`**：创建文档/章节/题组、题目及 revision、材料及 revision、题目部件和关系、学习内容、operation plan/scope/task 及目标关系、revision match 和 legacy session 表；发布 `operation-plan` workflow definition，但不切换旧任务。
2. **v0007 `external_target_links`**：在已有 0005 基础上增加 `external_record_targets`、`external_operation_targets`，为新写入的 `external_operations` 增加可回填的 `workflow_step_id`/`attempt_id`，扩展 scope guard 和索引；旧 `item_id` 列继续保留。
3. **回填与双写桥接**：先回填规范化身份、目标快照、Provider 作品证据，再启用新任务写入；回填脚本必须使用 checkpoint、幂等键和差异报告。
4. **切换与收缩**：灰度验证通过后，旧列和 `progress.json` 只保留读取/投影兼容；任何删除旧列或旧入口的动作另起迁移，且不得与 v0006/v0007 同批执行。

`2a` profile 仍表示“只安装到 v4 的旧基础 workflow schema”，不能声称已包含原子模型；从 v4 升级到 full 必须依次执行 v0005、v0006、v0007，并通过旧数据兼容检查。新 migration 的 `schema_migrations` 记录、checksum 校验、半失败回滚和重复执行都必须纳入测试。数据库迁移只改变本地事实，不自动重放已经发生的 Provider/外部副作用。

### 7.2 兼容旧任务

采用“双模型、单向派生”：

```text
新解析结果
  → 原子模型（事实源）
  → OperationPlan / OperationScope
       ├─ OperationTask(AUDIO) → legacy work_items（兼容投影）
       └─ OperationTask(EXTERNAL) → external mapping
```

旧版本解析结果只读兼容，不反向推导完整的题干、选项和材料关系。旧任务没有足够信息时，外部录入必须显示“需要重新解析/人工确认”。

旧 UI 的 `wordtts/progress.py` 兼容路径也必须纳入这条单向投影：它可以把 `OperationTask(AUDIO)` 转成 `progress.json`，并继续记录讯飞作品级的 `xunfei_works_ids`；但 `progress.json` 不得反向成为解析、外部记录或工作流状态的事实源。新 workflow 不能读取旧进度文件来判断外部录入是否成功。

旧数据迁移必须有显式回填步骤：读取旧 `work_items`、`progress.json`、音频文件 hash、文件名和 `xunfei_works_ids`，写入 `legacy_aliases` 及 Provider 作品/批次关联；无法确定小题身份时保留为 legacy 记录并进入人工确认，不能按顺序强行匹配。

讯飞 composite 的 `worksId` 应落到现有 `provider_submissions`/`provider_receipts` 体系：在 `provider_receipt_identifiers` 中以 `identifier_type=WORKS_ID` 保存，在 `provider_receipt_bindings` 中关联对应 `work_unit`/attempt，并通过 `work_unit_items`、`work_unit_segments` 保存作品内目标顺序和切割边界。一个 worksId 对多个音频单元是正常情况，不能用单个 `question_id` 或 `external_record_id` 代替。`progress.json.xunfei_works_ids` 导入时只生成 Provider 作品证据和 `legacy_alias`，不直接改变外部记录状态。

解析产物和数据库迁移均使用版本号；禁止在已有任务上静默改变 item 数量和身份。旧任务的回填必须可重复、可校验，并且不能因为数据库回滚而重复触发已发生的外部副作用。

## 8. 分阶段实施

### 阶段 0：冻结契约和样例

- 冻结领域概念到现有 workflow 表的映射：一个 `OperationPlan` 对应一个 `workflow_group`/accepted `workflow`，每个 `OperationTask` 一对一对应一个 `scope=workflow` 的 `workflow_step`；Attempt、租约、事件和终态只由现有 workflow 表维护，禁止出现两套可写状态。
- 冻结统一 `OperationRunner`/`OperationAdapter` 契约、事务边界、fencing token、错误分类、依赖失败传播和 `workflow_step` 状态转换；现有 TTS engine 与 `ExternalRecordService` 都必须挂到该契约下。
- 冻结 `source_document_id`、document revision、`question_id`、question revision、`question_local_key` 和 legacy alias 的身份与版本规则，明确插题、删题、重排、重编号、改文时的匹配算法、阈值、人工裁决和外部复用策略。
- 确认外部系统真正的最小单元：题干、选项、答案、音频是否都按小题保存。
- 确认外部题型枚举和必填字段。
- 按已冻结的 D-EXT-001 采用新增 `external_record_targets`、`external_operation_targets`；同时处理 `external_operations.item_id`、`external_record_bindings.item_id` 的历史兼容语义，以及 `workflow_step_id`/`attempt_id` 的新关联。
- 明确 0005 已有的外部记录、回执、对账、人工确认和 `retry_budgets.budget_kind=external` 不重复设计；阶段 4 只补目标关联、payload 构造、执行接线和引擎使用。
- 明确 workflow 数据库是新任务的执行状态事实源；`progress.json` 只保留为旧 UI/讯飞音频链路的兼容投影。
- 明确旧 UI 的执行桥接：推荐旧 UI 只调用 workflow 命令；若暂时保留旧执行器，必须通过 legacy adapter 写入同一套 step/attempt/provider 事实，桥接完成前的会话不得混入新 workflow 统计。
- 固定专用实体表 schema、`ParseCandidate` 中间协议、`QuestionItem`、`Stimulus`、`OperationTask`、`OperationScope` JSON schema；明确 `scope_id + scope_revision` 是目标唯一来源，其他 ID 数组只能是投影。
- 固定 scope 能力矩阵、状态分层、音频到外部录入的依赖规则和聚合提交的部分成功语义。
- 固定统一命令契约：创建/预览计划、确认解析、执行、暂停、恢复、重试、人工确认、对账和导出；每个命令都要带 request/idempotency key、期望版本和目标范围。
- 固定 v0006/v0007 migration 顺序、回填 checkpoint、失败恢复和 full/2a profile 行为；不得修改已发布 0001～0005 checksum。
- 建立端到端 fixture：一材料两小题、无材料小题、混合题型、题号缺失/歧义、复合题/多空题、插题/重排/改文、0005 历史数据、旧 progress + composite worksId，并覆盖音频任务、外部任务、依赖、重试和对账。

交付物：schema、字段说明、样例 JSON、题型映射表、身份规则。

### 阶段 1：增加规范化领域模型

- 增加结构读取器、原子模型和校验器，保留当前 legacy parser 入口；结构读取器至少要能提供段落、表格/单元格、样式和稳定定位。
- 增加 `ParseCandidate` 中间协议，保存 parser/type version、claimed block、候选实体、confidence、diagnostics 和 `audio_only` 能力；建立显式题型标记、owner、优先级和重复候选裁决规则。
- 对信息获取、听后选择、听后应答补齐真正的题干、选项、答案、材料关联和题型能力，不把仅含录音稿的旧结果伪装成完整 `QuestionItem`。
- 生成解析诊断和 `source_locator`；内容歧义写入 `resolution_state=AMBIGUOUS/UNRESOLVED`，不新增 `needs_review` 状态。
- 对 `audio_only` 结果只允许创建音频任务或待补全记录；未达到 `CONFIRMED` 的内容不得创建外部录入任务。
- 实现旧 revision 到新 revision 的确定性匹配、候选阈值和 `revision_match_decisions` 记录；插题、删题、重排、重编号、改文都必须产生可解释差异。
- 明确本阶段只是适配现有各 Parser 的结果，文档仍会被多个 Parser 独立重扫；统一结构分段器推迟到阶段 3，本阶段不宣称已消除解析重叠风险。

交付物：`ParseCandidate`/规范化模型契约、新旧结果对照测试、候选 owner 与去重负向测试、解析结果版本升级，不能重复或漏题。

### 阶段 2：持久化原子模型

- 增加数据库迁移和 repository API。
- 按 v0006 创建并持久化文档身份、章节/题组、题目及 revision、题目部件、材料及 revision、材料关系、学习内容、revision match 和 legacy session；`question_items`/`stimuli` 成功写入后，创建统一 `OperationPlan` 和不可变 `OperationScope`。
- 音频操作再派生现有 `work_items`，避免第一阶段影响 TTS；通过 `operation_task_targets`/delivery target 关系记录一个 `work_item` 对应哪个 Question/Stimulus/ContentUnit、用途和版本。外部操作使用同一计划和状态机制。
- 现有 TTS engine 的 `item_ids` 入口保留为兼容接口；统一入口先把 `OperationScope` 解析成稳定的音频 delivery targets，再调用 engine，禁止由前端直接拼接一批无业务身份的 item ID。
- 将每个 `OperationTask` 映射为统一 `operation-plan` workflow 中一个 `scope=workflow` 的 `workflow_step`；规范化小题通过 task target snapshot 进入 `work_items`/assignments 和 work-unit。`OperationRunner` 以现有 workflow 状态、Artifact、Attempt 和 Provider 事实为权威，不另建可写的任务状态表。
- 先完成旧 UI 的统一执行桥接：旧 UI 通过同一规范化模型派生音频投影，再由 `wordtts/progress.py` 生成 `progress.json`；旧执行器的 Provider 调用必须能关联到 workflow attempt。
- 为 progress projection 增加 schema/generation/source 元数据；数据库是唯一写入源，旧文件过期时重建，旧 UI 人工修改只能进入受控 legacy import。
- 明确旧链路的 `xunfei_works_ids` 只作为讯飞 Provider 作品证据；不把它当成 `external_records` 业务记录或小题映射。
- 为历史 `work_items`、progress item、音频 Artifact 和 worksId 执行可重复回填，无法匹配的记录进入人工确认，不阻塞新文档解析。

交付物：原子小题查询、材料关联查询、统一 runner 最小实现、重复解析幂等、revision 匹配差异、版本冲突保护和 v0006 migration。

### 阶段 2A：category 解耦与双路径收口

- 盘点并迁移 `category` 的全库使用：题型展示、`xunfei_voice_catalog.py` 音色映射、`wordtts/progress.py` 文件名前缀、进度 manifest 和前端筛选/展示。
- 在规范化模型和 `OperationTask` 中使用显式字段：`question_type`/`content_kind`、`display_category`、`voice_policy`、`naming_policy`；由稳定题型注册表提供能力和默认策略，`category` 只作为旧 `parse_results`/兼容投影字段。
- 固定策略优先级为“小题覆盖 > 文档/题型策略 > 系统默认”，并把最终策略写入操作配置快照。
- 让 workflow 路径和旧 UI 路径都从同一份规范化结果派生；旧路径只通过统一执行桥接执行音频，不得创建独立解析、外部记录或工作流状态。
- 为同一文档双路径输出一致的小题身份、顺序、正文和材料关系编写对照测试。

交付物：category 兼容适配器、音色/命名/manifest/前端回归测试、双路径事实源说明。

### 阶段 3：接入剩余题型

- 先实现统一结构分段器：文档只读取一次，支持段落、表格/单元格和样式定位，生成大题/章节/材料范围，再把范围交给题型规则，停止各 Parser 对全文独立重扫。
- 在分段器基础上迁移信息转述、模仿朗读、课文跟读。
- 对非考试内容明确使用 `ContentUnit`/`content_unit` 或 `generation_type`，并纳入 `OperationScope` 的 `CONTENT_UNIT` 目标类型，不要全部伪装成 `QuestionItem`。
- 补齐复合题、多空题、子问和多材料关联的结构规则。
- 为每种题型定义“业务原子边界”和“音频边界”两个独立规则。

交付物：统一分段器、全题型 fixture、解析差异报告和重复提取负向测试。

### 阶段 4：统一操作执行与外部适配

- 音频生成和外部录入统一通过 `OperationRunner`，由各自 `OperationAdapter` 映射到现有 workflow step/attempt/work-unit 执行，不能分别维护两套任务状态；适配器不得直接推进 workflow 状态。
- 按 v0007 和已冻结的 D-EXT-001 演进已有 0005 表，不另起一套外部记录事实源；外部记录默认按 `question_id` 创建或更新，聚合记录按 `scope_id + scope_revision` 创建或更新。
- 按 D-EXT-001 修改 `ExternalRecordService` 的 target/scope 输入，新增 record-target 和 operation-target 快照以及 workflow step/attempt 关联；保留旧 `item_id` 的历史读取兼容，并扩展现有 scope guard/唯一约束。
- 共享材料按 `stimulus_id` 去重，或按外部系统要求映射到多个小题。
- 单题失败只重试该题；聚合提交按成员快照记录部分成功，材料上传失败可按材料关系影响关联题目并明确展示。
- 音频 Artifact、外部 payload、外部回执和状态全部保存到统一操作任务及其目标关系。
- 外部系统不支持的 scope、部分成功或音频依赖在 `validate/prepare` 阶段阻断；依赖满足后才记录 intent 和提交，不能由前端绕过能力矩阵。
- 复用现有 `retry_budgets.budget_kind=external`，补齐引擎对外部单题/聚合操作的 reserve、use、释放和 `AMBIGUOUS` 保留规则；不新建第二套预算表。

交付物：v0007 迁移、统一 runner/adapter、单题幂等、单题重试、部分成功、依赖阻断、重复运行和回执对账测试，以及讯飞作品级 `worksId` 与外部业务记录的隔离测试。

### 阶段 5：收口旧模型

- 新任务默认只使用原子模型。
- 旧 `parse_results` 和 `progress.json` 只作为兼容读取/展示格式；不再允许旧 UI 直接产生新的解析事实、Provider 副作用或外部录入操作。
- 完成历史数据回填、差异核对和未匹配记录处置后，将 `progress.json` 改为只读投影；所有暂停、重试、确认和恢复命令都走 workflow API。
- 当历史任务和旧接口使用量稳定后，再评估废弃直接以 `work_items` 作为外部业务单元的路径。

### 阶段 5A：灰度、回滚与运维

- 使用 expand/contract 方式执行数据库迁移：先加新表和兼容字段，再回填和校验，最后切换读取/写入；保留 0005 及历史数据备份。
- 通过 feature flag 按文档、题型或用户灰度启用原子模型；新模型解析失败时只能回退到兼容展示，不能重新触发已发生的外部副作用。
- 明确回滚边界：代码和读取路径可以回滚，已经提交到讯飞或其他外部系统的副作用不能通过数据库回滚撤销，只能进入对账/人工处理。
- 增加 `AMBIGUOUS`、`UNRESOLVED`、对账积压、旧路径调用、scope 不支持、迁移未匹配和重复幂等冲突的指标及告警。

## 9. 验收标准

### 9.1 解析验收

- 一段材料对应两道小题时，得到 2 个 `QuestionItem`、1 个 `Stimulus`，两道题都引用同一材料。
- 同一解析结果可以生成 `QUESTION`、`GROUP`、`STIMULUS`、`MAJOR_SECTION` 和 `DOCUMENT` 等受支持的提交范围，且每种范围的成员小题集合可追溯。
- 聚合操作的范围变化只改变 `OperationScope` 和 payload 版本，不改变成员 `QuestionItem` 的稳定身份。
- 题干、选项、答案不会混入材料正文。
- 同一文档重新解析不会产生重复小题。
- 文档插题、删除、重排、重编号和改文时，匹配结果能区分“保留逻辑身份的新 revision”和“新建逻辑小题”，并生成差异报告。
- 新增独立小题型不会与旧大题 Parser 重复提取。
- `ParseCandidate` 的 owner、范围、题型版本、置信度和诊断可回放；同一结构块多规则命中时不会产生两份业务小题。
- 题号缺失或归属不确定时进入内容 `resolution_state=AMBIGUOUS/UNRESOLVED`，不静默合并，也不会生成外部提交任务。
- 段落、表格/单元格和样式来源都能被 `source_locator` 精确回溯。
- 题目 revision 匹配能按确定性键、候选阈值和人工裁决稳定重放；相同输入、parser/schema 版本和算法版本不会产生不同身份。
- `OperationTask` 的逻辑状态和 `workflow_step` 的执行状态只有一个可写来源；内容 `resolution_state`、任务状态和 side-effect 状态互不倒写。

### 9.2 TTS 验收

- 业务小题拆分后，现有录音稿整段生成规则仍可保留。
- 共享材料可以只生成一份音频，并能被多个小题引用。
- 句子、段落、语篇等非考试内容保持原有生成粒度。
- 旧任务的音频、进度和重试不因新模型上线而失效。
- workflow 路径和旧 UI 路径对同一文档得到一致的规范化小题；旧路径只生成兼容的 `progress.json`，不会写入新的业务状态。
- `category` 变化不会改变规范化小题身份；音色、文件名和展示分别由显式策略/展示字段决定。
- 旧 UI 的音频执行与 workflow 数据库中的 Attempt、Provider submission、Artifact 和状态一致；未桥接的 legacy 会话不会混入新 workflow 统计。
- 音频 scope 解析出的 delivery target、work item、work unit、作品内切割片段和最终 Artifact 能逐级追溯到 Question/Stimulus/ContentUnit revision。
- 音频和外部任务在同一个 `operation-plan` workflow 中可以按依赖顺序或允许的并行关系运行，失败传播、暂停、恢复和重试都由同一个 runner 执行。

### 9.3 外部录入验收

- 每个小题有独立外部业务键和本地身份。
- 外部系统按题组或大题提交时，仍能展开到具体成员小题，并分别显示成员状态或明确记录整体状态。
- 单题修改不会覆盖同材料下的其他小题。
- 单题失败可以独立重试；材料级失败的影响范围可追踪。
- 重复运行不会重复创建外部记录。
- 外部回执可以准确回写到具体小题，而不是只回写到整段录音稿。
- 不支持的 `scope_kind`、多目标关系、部分成功能力或音频依赖会在提交前被拒绝并进入明确状态。
- 聚合记录保存提交时的目标成员快照；单个成员改动不会静默覆盖原聚合操作，更新范围和影响成员可追踪。
- 0005 既有记录、操作、绑定、租约和人工确认能力继续有效；演进后旧 `local_item_id`/`item_id` 历史数据仍可读取。
- 外部单题/聚合操作复用现有 `retry_budgets.budget_kind=external`，`AMBIGUOUS` 期间不会错误释放预算。
- 旧 progress、音频文件 hash 和 composite worksId 能回填为 Provider 作品/批次证据；无法匹配的记录不会被强行绑定到错误小题。
- 新外部操作的目标必须能通过 `external_record_targets`、`external_operation_targets`、`workflow_step_id` 和 `attempt_id` 追溯；旧 `item_id` 只承担历史兼容，不影响新目标判断。

### 9.4 迁移与运行验收

- 从已有 0005 数据库升级时，旧记录、操作、绑定、租约、回执和人工确认数据保持可读，迁移失败不会留下半套新表或半套关联。
- 旧 `work_items`、`progress.json`、音频文件和 worksId 回填可以重复执行，重复执行不会创建重复小题、重复 Provider submission 或重复外部操作。
- 新旧路径在灰度期间有明确 feature flag、统计口径和切换时间点；未桥接的 legacy 会话不会被伪装成新 workflow 成功。
- 发生代码或数据库回滚时，已发生的讯飞/外部副作用仍能通过 operation key、receipt 和对账记录恢复，不会因回滚而再次提交。
- `AMBIGUOUS`、`UNRESOLVED`、对账积压、迁移未匹配和幂等冲突均能在 UI 和日志中定位到具体文档 revision、scope、task 和目标成员。

### 9.5 最小端到端场景

必须通过以下同一 fixture 才能进入外部适配器灰度：

```text
一份文档 revision
  ├─ 一个 Stimulus（听力材料）
  ├─ QuestionItem-1
  └─ QuestionItem-2
```

执行一个 `OperationPlan`：音频任务以 `STIMULUS` scope 生成一个 composite `worksId`，作品内边界可追溯到两个小题；外部任务以两个 `QUESTION` scope 分别 upsert 两条业务记录，并保存相同材料引用。随后只修改 QuestionItem-2：必须生成新的 question revision、只重算受影响的音频/外部任务，QuestionItem-1 的身份、Artifact 和外部记录不能被覆盖。再分别模拟 Provider 成功、外部 `AMBIGUOUS`、单题重试、聚合部分成功、旧 progress/worksId 回填和重复执行，验证不会产生重复作品或外部记录，且所有状态都能沿统一 runner 链路追溯。

## 10. 主要风险与决策点

1. **操作映射落地不一致**：若 `OperationTask` 与 `workflow_steps` 都可写状态，或混合计划继续复用只支持 TTS 的 definition，会形成第二套状态机或两条运行链；必须使用统一 `operation-plan` workflow 和 runner。
2. **0005 兼容演进风险**：目标关联方案已冻结为新增表，但旧 `local_item_id`、scope guard、唯一索引和历史回执必须在 v0007 中并行兼容，不能只新增两张表就切换。
3. **来源身份问题**：仅用文件 hash 无法支持用户修改文档后继续关联原外部记录；即使有 revision diff，也必须严格执行确定性匹配、候选阈值和人工裁决。
4. **旧 UI 权威冲突**：旧 UI 若继续直接执行 Provider 并写 progress，就不可能同时让 workflow 数据库成为唯一执行状态源；必须先完成命令/事件桥接，或明确标记为 out-of-band。
5. **Parser 事实不足**：旧 Parser 主要输出音频片段，不能自动补出题干、选项、答案和材料关联；未达到完整事实和 `CONFIRMED` 的小题不能进入外部链路。
6. **历史数据拆分**：旧 item 可能无法还原题干与材料关系，progress 和 worksId 也可能是一个作品对应多个音频单元，不能按顺序强行迁移。
7. **身份与修订漂移**：插题、重排、重编号和改文可能改变 locator；缺少 alias 和 revision 匹配会导致错误复用外部记录。
8. **状态语义混淆**：内容歧义、任务失败和外部副作用歧义必须分层，否则人工确认会错误改变任务或外部记录状态。
9. **范围与依赖不完整**：scope 成员变动、聚合部分成功、音频 Artifact 依赖和不支持的外部粒度必须在提交前确定。
10. **音频边界变化**：业务小题拆分不应强迫改变现有音频边界，二者必须有独立配置和测试。
11. **category 兼容面广**：音色、命名、manifest 和前端展示迁移不完整时，新增题型可能出现声音或文件名回退。
12. **结构读取能力不足**：当前 loader 主要读取普通段落，表格/单元格和样式定位若未纳入结构层，会导致 source locator 和题目事实不完整。
13. **解析重叠**：当前每个检测到的题型都会重新加载全文并独立扫描，阶段 1 适配旧 Parser 时该风险原样继承；统一结构分段器推迟到阶段 3，阶段 1/2 不宣称已消除该风险。
14. **外部副作用不可回滚**：数据库或代码可以回滚，已提交的外部记录不能靠回滚撤销，必须保留对账和人工恢复路径。
15. **payload 数据暴露**：题干、答案和外部回执可能包含业务敏感信息；必须明确原文存储、脱敏日志、访问控制和保留期限，不能把兼容 JSON 当作安全存储。
16. **适配器绕过 runner**：如果 `ExternalRecordService` 或旧 TTS engine 继续直接推进状态/提交 Provider，统一模型只会停留在数据层；必须由 runner 统一负责 intent、Attempt、租约、预算和终态。
17. **多态目标引用完整性**：`target_kind + target_id` 无法由 SQLite 一个普通外键完整约束，v0006/v0007 必须有按类型校验、trigger 或目标注册表，并覆盖删除、改版和跨范围引用。
18. **迁移 profile 漂移**：migration runner 要求版本连续且 2a 停在 v4；v0006/v0007 的顺序、checksum、从 v4 升级和半失败续跑必须单独验证。

## 11. 推荐优先级

在真正开发外部录入之前，优先完成：

1. 实现并验证统一 `OperationRunner`/`OperationAdapter` 与 `operation-plan` workflow 映射，不新增第二套可写状态；
2. 实现文档、小题、revision、alias 的身份表、确定性匹配和人工裁决记录；
3. 落地 v0006/v0007 schema、0005 目标关联和旧列/触发器兼容；
4. 固定内容状态、操作状态、side-effect 状态和 scope 能力矩阵；
5. 完成旧 UI 的 workflow 命令/事件桥接或明确 out-of-band 边界；
6. 实现专用 `QuestionItem`/`Stimulus`/`ContentUnit`/`OperationScope` schema，并补齐复合题和多空题；
7. “一段材料 + 两道小题”、历史 0005、旧 progress + composite worksId、插题重排改文等端到端 fixture；
8. 信息获取、听后选择两个题型的完整原子化解析与 Parser owner 裁决；
9. category 解耦和新原子模型到旧 TTS `work_items`/`progress.json` 的兼容投影。

在这九项完成前，不建议直接开发外部录入适配器，也不建议继续向当前 `category` 字段堆叠更多小题型含义。

## 12. 实施状态（2026-08-29 更新）

基线：`tools/parse_baseline.py` + `examples/baselines/parse/20260829-pre-atomic-model/`
（13 份示例文档 465 条，两解析路径逐条一致；每次提交前指纹比对零回归）。

已完成：

| 切片 | 内容 | 提交 |
| --- | --- | --- |
| 基线 | 解析基线快照工具 + 方案冻结 | 62cb17c |
| 阶段1 | question_model 规范化模型 + ParseCandidate + 信息获取/听后选择抽取器 | c4ba382 |
| 阶段1 | 全部 7 大题型接入（11 个小题型含预留） | e2b9ee6 |
| 阶段1 | ParseCandidate 裁决层（显式标记优先/唯一 owner/AMBIGUOUS） | a308919 |
| 阶段2 | v0006 全部表 + 幂等落库仓储 | b022a36 |
| 重构 | 两级题型注册表（family + sub_type，能力/音色/命名挂小题型） | 3bc0e51 |
| 阶段4 | v0007 external_record/operation_targets + 多态目标 trigger | 772a74f |
| 阶段1 | revmatch-v1 revision 匹配 + document_revision_members | df75d01 |
| 阶段2 | OperationPlan/不可变 Scope/AUDIO 任务 + work_items 投影 + legacy_aliases | 1fcc1f9 |
| 阶段4 | 统一 OperationRunner/OperationAdapter + ExternalUpsertAdapter 接线 | f1dafdb |

关键设计修正（实施中确认）：

- 信息获取文档题目在录音稿**之前**，小题关联到后续第一段录音稿；
- content_hash 为纯内容指纹（不含身份），revision id = 身份+内容派生；
- 文档版本成员关系由 `document_revision_members` 表达（revision 行内容寻址可复用）；
- 模仿朗读/词汇为叶子题型（业务确认），外网/教材与单词/例句是属性不是小题型。

待完成：

1. 真实 TTS 引擎接入 OperationAdapter（现用 FakeAudioAdapter 干跑）；
2. 阶段3 统一结构分段器（各 Parser 仍独立重扫全文）与听后选择/信息转述业务字段抽取；
3. 旧 UI 命令桥接与 progress.json 单向投影元数据（schema_version/generation）；
4. 历史数据回填脚本（legacy_aliases 的存量回填部分）；
5. 阶段5A 灰度/feature flag/监控指标。
