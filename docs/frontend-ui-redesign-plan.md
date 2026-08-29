# 小猪 Word TTS 前端 UI 重构方案

**文档状态与当前事实基线（2026-08-29 评审合并版）**：方案稿，已完成多轮代码事实复核，整体方向可保留。本节合并并取代此前分散的"评审结论/实施审计更正/前端接线补充结论"多段状态（原文见版本历史）；后续修订直接更新本节与正文对应小节，不再追加新的状态段落，避免多层口径互相覆盖。

当前事实基线（以工作树代码、契约和测试为准）：

- v1 已形成有限闭环：配置持久化与生成前计划固化、启动安全恢复/干预过期/限量 GC、`READY + verified` 交付闸门、安全失败后换音色新计划、SSE 游标清理与事件流 snapshot 重锚定、试听前三条的后端范围限制、终态 rerun、持久化失败条目的目标范围重试、终态确定性 `export-zip`，以及受运行时/真实 Provider 开关控制的安全 retry 派发路径。
- 服务端已提供：活动任务候选（`can_resume`/`can_takeover`/`requires_reconcile`）、`WorkflowWorkspace` 聚合（含 `SERVICE/UI` 动作结构、`configuration_revision`/`configuration_hash`、交付包含/排除明细）、`PATCH workspace` 有限条目覆盖，以及对无未决外部副作用任务的有限安全接管。
- 仍开放的缺口：Renderer 对上述契约的完整接线（workspace hydrate、Snapshot 驱动 UI、配置 revision 贯穿）、编辑/跳过的前端修订闭环、未决外部调用的自动接管/重发、用户暂停任务自动续跑、完整暂停/恢复协作语义、交付范围明细 UI、导入/试听/下载的端到端流式与传输控制、完整工作台 UI，以及真实 Provider/目标设备验收。
- 本次评审（2026-08-29）更正的事实与修订：后端不存在 `WORKFLOW_CANCEL` 事件（详见 §3.3、§9.3.1）；§12.0 已合入第 16 节的实施顺序；§10.1 B 已标注 workspace 字段级已有/待补；§6.1.1/§8.2 已补只读核对的出路规则；§2.3 已固化 `configuration_revision` 初始推导规则。本轮还明确了三处容易误读的边界：当前只有 `generate` 的 `STATE_CONFLICT` 有一次刷新/重试，`patchDraft` 的 `CONFIGURATION_CONFLICT` 尚未自动合并；workspace 的 `RERUN` 当前把 `group_state_version` 填进了 `expected_state_version`，正式契约必须拆出独立字段；Renderer 当前筛选白名单包含 `tts-output`，但条目级交付应以独立的 `tts-segment` 为准。

**目标版本**：前端工作台大版本（建议作为 v3 级别规划）

**编写日期**：2026-08-28
**最近复核**：2026-08-29
**适用范围**：Electron Renderer、前端状态层、工作流相关接口投影；不包含本方案阶段的代码实现

**事实级别**：文中“当前实现”指本仓库当前工作树中的实现；“建议、目标、拟新增”不代表已经存在的接口或能力。

**生效口径**：本文件是“UI 方案 + 实施审计附录”的组合文档。第 2.2 节所说的“不包含代码实现”表示本方案不直接修改 Renderer、Preload、主进程或后端代码；第 16 节只记录当前调用点、缺口和后续实现前置条件。当前实现以代码、契约和测试证据为准；第 15 节未被显式覆盖的产品决策暂按默认路线执行，但在 G0/G1 闸门通过前不视作已冻结。

## 1. 摘要

产品目标和旧版 UI 已覆盖导入 Word 文档、解析内容、配置角色与声音、生成音频、试听和下载；当前工作树已将配置、计划、产物闸门、SSE 游标清理/事件流快照重锚定、终态 rerun、失败条目目标范围重试、确定性 `export-zip` 和受运行时/真实 Provider 开关控制的安全 retry 派发路径接入现有流程，但仍不能把完整工作台承诺写成全部可用。完整 Store/workspace/action 投影、workspace hydrate、编辑/跳过的前端闭环、未决外部调用的自动重发、用户暂停任务自动续跑、交付包含/排除明细、大文件端到端流式和真实外部验收仍是开放项。

本次重构不建议继续在现有“四步向导 + 大量卡片”的结构上局部修补，而是将前端升级为一个**以生成任务为中心的文档配音工作台**：用户始终知道当前任务、当前阶段、下一步动作和异常如何处理；生成过程可观察，并在服务端能力具备时支持暂停、恢复和取消；最终产物可以按条目检查，而不是只能等待整批任务结束。

核心方向可以概括为：

> **从“填写一组设置并等待结果”，升级为“管理一条可恢复的文档配音任务”。**

底层工作流语义不需要被 UI 复刻成复杂的技术面板。前端要做的是把后端的快照和事件转换成稳定、可理解、可操作的任务界面；暂停、恢复和取消只有在服务端能力已确认且语义可验证时才作为可操作动作开放。

## 2. 方案边界

### 2.1 本方案包含

- 当前前端界面、交互、信息架构和视觉语言的审查结论。
- 新版本的产品定位、设计原则、工作台布局和核心页面方案。
- 工作流状态如何映射为用户可理解的界面状态。
- 前端状态层、组件边界和后端接口配合建议。
- 分阶段实施顺序、风险、验收标准和需要确认的决策。

### 2.2 本方案暂不包含

- Renderer、Preload、主进程或后端接口的实际代码修改。
- 语音供应商、文档解析规则或音频生成算法的重设计。
- 用户体系、云端协作、计费等产品范围扩展。
- 在没有确认视觉方向前直接建立新的品牌资产或插画资产。

### 2.3 建议首版范围与默认路线

当前方案同时描述了“可信的最小闭环”和“完整工作台”的能力。为了避免实现团队在契约未冻结前先做出不可用的按钮，首版默认采用 `MVP-A`；未明确列入 `MVP-A` 的能力只能以明确标注“未支持/待契约”的说明或不可操作控件出现在设计稿或 fixture 中，不得让一个普通禁用按钮单独暗示能力已经交付。

| 能力 | `MVP-A` 默认范围 | 后续或条件交付 |
| --- | --- | --- |
| 状态与恢复 | 五维状态派生、Store 作为工作流视图事实源、当前任务启动/切换时的 snapshot + workspace hydrate、SSE 缺口/410 的游标清理与事件流快照重锚定、恢复任务上下文（不等于自动续跑） | 活动任务索引的批量/完整 workspace hydrate、未决外部调用的自动接管/重发、用户暂停任务自动续跑、多任务后台控制 |
| 内容核对 | 只读核对；进入下一步属于 UI 导航，不伪造“已确认”的服务端状态 | 前端编辑、条目级角色/声音覆盖、跳过和持久化确认（后端已有有限 `item_overrides` 原语，但未接入 `MVP-A` UI） |
| 配置与生成 | 当前实现将声音/角色分配、语速/音调/音量、质量、预览范围和生成模式写入服务端配置快照，并由生成计划读取；Renderer 已在生成前读取 workspace 的 `configuration_revision`，并把它带入现有 `patchDraft` 与 `generate` 调用；其中 `generate` 对明确的 `STATE_CONFLICT` 有一次刷新/重试，`patchDraft` 的 `CONFIGURATION_CONFLICT` 目前不会自动合并，且生成请求仍可携带 `generation_mode`、`provider`、`account_scope` 覆盖值；尚未通过 `patchWorkspace`/Store 形成统一配置投影；格式通过端到端验证后才交付单条产物 | 文本编辑/跳过的前端闭环、条目级修订覆盖 UI、统一 workspace/Store 的 revision 接线、生成请求覆盖字段收口、其他格式 |
| 任务控制 | 只展示服务端明确允许且可验证的暂停/恢复/取消动作；安全的失败条目目标范围重试已有后端路径，但 UI 仍需按能力投影收口 | 完整暂停/恢复前端闭环、未决/任意范围重试、自动化对账 |
| 交付 | 通过 Renderer 交付闸门的单条试听/下载、成功/失败/待处理摘要；终态成功或部分成功可按需创建只包含已验证条目的确定性 ZIP；服务端 workspace 已有交付范围字段，但 Renderer 尚未消费 | 交付包含/排除明细、批量交付、大文件流式及传输进度/取消 |

`MVP-A` 不是对产品最终形态的否定，而是进入 P1 页面重构前的契约闸门。产品决策可以在第 15 节覆盖默认路线，但覆盖后必须同步更新本节、接口契约、实施阶段和验收标准。前端 v3 在本文中表示 UI/交互里程碑，不等同于后端 `/api/v1` 的版本号；当前 `app.js` 中仍可见的 v2 注释也不代表本方案已经实现。

本文中的 `configuration_revision` 沿用并需要冻结为 workflow 内的“可生成配置快照版本”，不是当前 `WorkflowSnapshot.draft_revision` 的换名。当前 workspace/后端已经返回并校验该字段；旧 workflow 的已保存配置中没有独立修订标记时，按当前实现推导为 `max(1, draft_revision + 1)`（`workflow/repositories.py` 的 `_configuration_revision`），该推导规则需在 G1 契约中冻结。`draft_revision` 仍表示现有草稿修订事实。在前端 revision 接线和历史 workflow fixture 完成前，不能把 `draft_revision` 直接当成声音参数、生成范围和输出格式均已保存的配置版本。

还要避免与数据库现有字段混淆：当前 `workflows.configuration_version` 写入的是 workflow definition version，`patch_draft` 也不会随配置修改更新它；它不能直接冒充这里的可变 `configuration_revision`。实现时应沿用现有 configuration revision，并在契约中冻结它与 `configuration_snapshot`、`configuration_hash`、`draft_revision` 的关系，不要再新建一个语义重叠的字段。

### 2.4 `MVP-A` 发布判定规则

`MVP-A` 不是“先把页面做出来、再观察接口是否能支撑”的软目标，而是一个有硬门的发布范围：G1/G2/G3 任一未通过时可以做 fixture 驱动的结构验证，但不能发布真实生成按钮或继续生成承诺。

| 分类 | `MVP-A` 口径 |
| --- | --- |
| 必须交付 | 导入与解析、只读内容核对、声音/角色和生成参数的 workflow 持久化与回显、服务端确认的配置生成、五维状态派生、SSE 缺口/410 的游标清理与事件流快照重锚定、历史任务的状态正确展示；单条试听/下载只有在 Renderer 通过 `READY + verified`、格式/MIME 和资源错误恢复验收后才开放。如果 `MVP-A` 开放现有的失败条目重试或终态 ZIP，必须同时交付其安全范围、产物条件和降级说明。 |
| 明确不交付 | 内容编辑/跳过和持久化确认、任意范围或未决条目重试、交付包含/排除明细、大文件端到端流式、未决外部调用的自动接管/重发、用户暂停任务自动续跑。没有对应契约时不得用普通禁用按钮暗示已支持，必须明确标注“未支持/待契约”。 |
| 条件开放 | 暂停、恢复、取消、对账、MP3 交付等动作，只有在 `available_actions`、版本条件、外部调用语义和 fixture/回归测试全部通过后才显示为可操作。 |

若 MP3 的实际字节、Artifact 元数据、文件扩展名或 HTTP MIME 任一项未通过一致性校验，`MVP-A` 应阻止对应交付并显示“格式尚未验证”；不得自动降级为 `.bin` 或其他未知格式来维持“可下载”表象。默认路线的调整必须同时修改本节、§12 和 §13，不能只在产品文案中改口径。

## 3. 产品与代码现状

### 3.1 产品事实

当前产品是一款本地桌面应用，主要服务于将 Word 教材或文档转换成可试听、可交付的音频。README 和旧版 UI 描述的主要能力包括文档解析、男女声角色配置、音频生成、试听、下载、ZIP 交付、历史记录以及生成过程中的恢复、取消和重试；这些内容代表产品意图或旧链路，不能直接当作当前 v1 workflow API 的端到端事实。当前 v1 已有导入源文件、解析、快照/SSE、配置持久化、试听前三条范围限制、条目/产物查询、持久化失败条目的目标范围重试、终态确定性 ZIP 和受运行时开关控制的安全 retry 派发路径；完整 workspace/action 投影、编辑/跳过的前端闭环、未决外部调用的接管/重发、用户暂停任务自动续跑、交付范围元数据/大文件流式和真实外部验收仍需补齐。

输入格式也需要单独冻结：当前 parser 和 Renderer 接受 `.docx`、`.xlsx`，而 README 的产品主张仍以 Word 教材为中心。本文的章节树、原文定位和条目核对模型不能默认覆盖 Excel；若 `MVP-A` 保留 `.xlsx`，必须补充工作表、行列、合并单元格和角色标记的投影及验收，否则应在首版承诺中明确只支持 `.docx`。

输出格式同样存在口径冲突：README 仍列出 MP3、OGG、AAC、OPUS、WAV，当前 Renderer 已固定为 MP3；workspace 的 `configuration.effective.format` 缺失时默认显示 `mp3`，而 Artifact projection 的缺失格式内部兜底为 `bin`，两者都不是实际编码格式的证明。G0 必须明确首版是 MP3-only 还是恢复多格式；在此之前，未知格式应保持“格式未确认/不可交付”，不能把 `.bin` 或默认 MP3 当作真实编码格式，也要同步修正文案和发布说明。

当前前端主要入口和后端调用关系如下：

| 区域 | 当前实现 | 方案判断 |
| --- | --- | --- |
| 应用 Shell | 左侧深色导航 + 顶部服务状态 + 主内容区 | 保留“稳定侧栏 + 专注工作区”的骨架，但重新定义导航语义 |
| 主流程 | 导入、配置、生成、结果四步 | 保留阶段概念，改为“任务工作台”的五个逻辑视图 |
| 状态来源 | `workflow-store.js` 已消费快照、去重事件、检测缺口并持久化 SSE 游标/序号；`app.js` 仍由 `currentStep`、`isGenerating`、`currentSession` 等局部变量驱动主要视图，尚未形成完整 Store 订阅式 workspace 投影 | 需要统一为 Snapshot + workspace 投影驱动，UI 本地状态只保存选择和展开状态 |
| 声音配置 | 角色卡片、语音浏览器、搜索/筛选、参数设置 | 保留功能，减少嵌套卡片和重复滚动，改为“角色列表 + 目录抽屉 + 右侧详情” |
| 生成过程 | 进度、阶段、日志/时间线 | 保留可诊断能力，但把日志降级为按需展开的诊断抽屉 |
| 结果交付 | HTML 中存在条件式 ZIP 卡片和音频列表；当前历史、Artifact ticket 和 Renderer 结果筛选均要求 `READY + verified`；服务端 workspace 已返回交付范围及文件名、MIME、大小和时长字段，但部分文件名/MIME 是 projection 推导值、时长可能为 `null`，Renderer 仍从 `listItems/listArtifacts` 和本地统计拼接结果 | 改为“交付中心”，突出单条试听/下载、交付状态和失败项处理；ZIP 只纳入已验证条目，并继续受契约闸门约束 |
| 历史记录 | `/api/v1/workflows` 返回按更新时间排序且受 `limit` 限制的未 `CLOSED` workflow；服务端另有 `/api/v1/workflows/active` 活动任务候选接口，Renderer 已在刷新历史时附带读取候选，但仍以普通历史列表为主，结果摘要/动作部分按历史投影和可用 Artifact 数量渲染；当前操作文案已改为“归档”，实际调用的也是归档接口；当前“20 条”只是 Renderer 展示截断，不是持久化自动移除策略 | 改成由 workflow 五维状态、workspace 和能力投影统一驱动的任务列表，明确“归档”和“删除”，并把恢复上下文与重新运行区分开 |

### 3.2 当前 UI 的优点

- 入口和主流程一眼可见，初次使用者不容易完全迷路。
- 深色侧栏与浅色工作区形成了清晰的视觉分区。
- 主要动作使用蓝色强调，品牌识别度和可发现性较好。
- 声音目录已经具备搜索、筛选、预览和参数配置等必要能力。
- 生成阶段已经尝试呈现阶段、进度和事件时间线，说明产品已经从“一次性按钮”迈向“可观察任务”。
- Electron + Preload 的安全边界和现有工作流 API 可以作为重构的基础，不需要先做框架迁移。

### 3.3 主要问题与影响

> 本节保留实施前的风险基线，便于追溯当时为什么需要补齐 P0；其中已修复项不能再作为当前状态结论。当前状态以本文件开头的“实施审计更正”、`docs/implementation-plan.md`、代码和回归报告为准。仍开放的主要项是完整 Store/workspace/action 投影、编辑/跳过修订的前端闭环、未决外部调用的接管/重发、用户暂停任务自动续跑、完整暂停/恢复协作语义、交付包含/排除明细、大文件端到端流式及真实外部/设备验收。

#### P0：状态与用户看到的状态不一致

当前界面一部分由 `currentStep`、`isGenerating` 等本地变量驱动，另一部分由 Store 快照和 SSE 事件驱动。这样会导致：

- 页面刷新或应用重启后，活动任务无法自然恢复到正确视图。
- 后端处于 `WAITING_USER`、`BLOCKED`、`WAITING_RETRY` 等状态时，用户看不到明确的处理入口。
- “生成中”只能表达大阶段，无法稳定表达暂停请求、恢复中、清理中、部分成功等细节。
- UI 只能被动等待，无法把后端已有的重试、对账和重跑原语，以及仍需前端接线和验收的暂停/恢复/取消语义，稳定转化为操作。

需要特别区分“已有后端原语”和“前端已接通”：`workflow-store.js` 已具备快照消费、SSE 游标持久化和缺口检测；`app.js` 已调用 `prepare/consume` 做帧校验和游标处理，但目前仍只持久化 Renderer 侧游标与序号，尚未通过 Store 订阅把规范化快照作为完整渲染事实源。当前页面仍主要把快照映射到 `currentSession.state_version`、游标和状态文本，事件处理覆盖的类型有限。因此本方案不能把“Store 已存在”写成“Snapshot 驱动已经完成”；这部分仍是明确的前端收口缺口。

此前发现的断线恢复陷阱已经部分修复：出现序号缺口或服务端返回 `CURSOR_EXPIRED`/HTTP 410 时，Renderer 会清除对应 workflow 的持久化游标和 seq，下一次连接不带旧游标并由服务端 snapshot 重新锚定；但当前恢复仍是清游标后直接重连，尚未完成“先 hydrate snapshot/workspace、再重订阅”的统一顺序。完整 workspace hydrate 仍未接入，因此这项能力只能称为“事件流游标重置/快照重锚定已接通”，不是完整任务工作台恢复。

取消反馈的提前终态问题已部分修复：当前后端没有“取消请求已受理”的专用事件，“正在停止”只能由取消命令响应和 `control_state=TERMINATING` 快照表达；收到 `WORKFLOW_CANCELLED`（清理收敛时发布）后还会读取权威快照，当前分支只校验 `execution_state=TERMINAL`，尚未统一校验 `control_state=TERMINATED`、`result_status` 和 workspace 产物条件。只有这些事实共同收敛后才应进入取消结果视图；后端若收敛为 `PARTIAL_SUCCESS` 也要保留部分成功语义。仍需补全能力投影和更细的取消中 workspace 展示。

这里的事件层次需按代码事实更正：后端只发布 `WORKFLOW_CANCELLED`（worker 清理收敛且用户请求取消时；非取消的清理收敛发布 `WORKFLOW_CLEANUP_COMPLETED`），不存在“取消请求已受理”事件；Renderer 中处理 `WORKFLOW_CANCEL` 的分支（`app.js`）是后端从未触发的死代码，Store 接线时应移除。若已有可保留产物，最终结果还可能是 `PARTIAL_SUCCESS`。当前取消分支还没有把 `control_state`、`result_status` 和条目/Artifact 条件统一交给 reducer，不能把任一事件名或单独的 `execution_state=TERMINAL` 当作“纯取消”终态。若产品需要显式的“取消已受理”通知事件（例如 `WORKFLOW_CANCEL_REQUESTED`），必须在 G1 新增后端契约并补 fixture；契约冻结前 UI 不得依赖该事件。

已接受的生成任务的失败收敛已由运行时任务包装和 Repository 状态更新覆盖：失败事件会伴随可持久化的 `WAITING_RETRY`、`WAITING_USER` 或终态快照；事件仍不能替代快照，Renderer 会在失败事件后重新读取权威状态。当前正式桌面运行时已接入安全 retry 派发，但只针对 provider-aware、无未决外部副作用的可安全任务；不能把所有 `WAITING_RETRY` 或未决提交写成会自动继续。

同类问题此前也出现在 `TTS_OUTPUT_VERIFIED` 和 `GENERATION_TASK_FAILED`：事件到达时不能直接把页面切成完成或失败。当前 Renderer 会在这些事件后读取权威快照，但结果页仍混用本地统计/产物列表，尚未由统一 workspace 投影判断终态和交付条件。因此 P0 仍必须让关键事件触发“状态待同步”，只有快照、条目状态和产物投影收敛后才进入结果或错误终态。

> 当前实现更正：`TTS_OUTPUT_VERIFIED`、`GENERATION_TASK_FAILED` 和取消事件都会触发权威快照读取，但取消终态校验和结果页投影仍不完整；这不等于 Store 已成为完整 UI 单一事实源，仍需完成 workspace 投影迁移。

迁移期间还要保留一个容易误读的兼容字段：当前 `currentSession.session_id` 实际承载的是 `workflow_id`，不是 SourceImport 的逻辑会话 ID。新状态层应使用明确的 `workflow_id`、`source_import_id` 和 `configuration_revision` 字段；在旧代码尚未拆除前，不得把 `session_id` 当作跨接口的统一主键。

当前 Renderer 尚未把暂停、恢复、对账和解决阻塞等命令完整投影到统一页面；失败条目的安全目标范围重试已经接入，但只接受持久化 `FAILED` 且没有已验证产物的条目，未决条目不能批量重试。已有 API 原语不能直接推导出可点击按钮。所有控制动作必须由 `available_actions` 或已冻结的能力清单投影，并在命令 pending、冲突和超时后回到最新快照。

此外，`list_history_records` 的 `available_files`/`completed`、Renderer 的 `resultFilesFromArtifacts` 以及 Artifact ticket 现在统一要求 `lifecycle_state=READY` 且 `verified=true`；服务端发布和下载边界已把“可读”和“可交付”分开。`export-zip` 已有按需创建/复用路径，workspace/export 响应也已有成功、失败、跳过、未决的包含/排除字段；当前阻塞在 Renderer 尚未把这些字段用于交付摘要、明细和按钮条件，不能因此推断完整批量交付已完成。

#### 已修复基线：任务配置持久化闭环（当前转为契约收口）

当前生成入口会在接受任务前把声音、角色覆盖、语速/音调/音量、质量、预览范围和生成模式写回 workflow 配置；Engine 从服务端快照组装计划，试听任务只计划前三条，Provider profile 包含质量并由真实旧版音频导出按码率落地。已开始外部提交的原任务仍冻结配置，安全失败且无未决副作用的 `WAITING_RETRY` 才允许换音色形成新计划。

因此，当前已满足“配置持久化 → 服务端确认 → 生成使用同一份配置”的闭环；历史投影读取 workflow 配置，`localStorage` 仍只保存预设、临时表单恢复和 UI 偏好，不能作为当前任务的事实源。终态修改会创建新的 rerun workflow，不会修改旧 run 的事实。

#### P0：Provider 登录态与可用性未形成显式门槛

README 已明确首次真实生成需要在讯飞配音浏览器窗口完成登录，登录状态保存在本机；当前 Renderer 主要展示“正在连接/启动讯飞浏览器”，后端通过 provider-ready 检查决定是否继续，但没有向用户稳定投影“未登录、检测中、已登录、会话失效、浏览器被关闭、需要重新登录”等状态，也没有独立的登录/检测/重新认证/取消流程。不能把浏览器启动或生成请求发出当作登录成功证据。

首版必须将 Provider 可用性作为生成前门槛：能力或 workspace 投影至少返回脱敏的 `provider_state`、`login_required`、`can_generate` 和用户可见原因；UI 提供“打开登录/重新检测/重新登录”，会话失效或浏览器关闭时回到可解释的等待/重新认证状态，不自动重复提交可能已有副作用的生成。Provider 未就绪 fixture 和真实讯飞登录/会话过期验收通过前，不能把“开始生成”作为无条件主按钮。

#### P0：内容核对投影缺少正文和稳定定位字段

当前 `WorkflowWorkspace.items` 主要返回身份、顺序、hash、状态、角色/声音、尝试和错误信息，不含 `item_type`、`normalized_content`、`source_locator` 或安全的 `metadata.section`；`GET /workflows/{workflow_id}/items` 才能提供部分正文和类型字段。因而第 8.2 节的章节树、原文和定位不能直接由 workspace 驱动。

G1 必须二选一：扩展 workspace 的只读条目投影，或提供有界/分页的条目详情接口，并明确长正文、定位字段和敏感 metadata 的脱敏规则。在这组数据契约落地前，`MVP-A` 只能把页面称为“结构预览/条目列表”，不能暗示用户已经完成了可核对的原文确认；编辑、跳过的前端闭环和持久化确认仍然是更后面的修订能力。

#### P0：内容核对的编辑/跳过能力尚未接入前端

当前后端的 `patchDraft`/`PATCH /api/v1/workflows/{workflow_id}/workspace` 已提供受 workflow `expected_state_version` 和配置 revision 保护的有限条目覆盖：`role`、`voice_key`、`normalized_content`、`metadata`、`status=PENDING/SKIPPED` 以及 `skip_reason`；对已交付或存在未决 Provider 副作用的条目会拒绝修订，生成计划会排除 `SKIPPED`，现有运行时/API 测试也覆盖了配置 revision、跳过和交付排除。因此“编辑/跳过”已经是后端有限能力，但不是现有的端到端 UI 能力：`WorkflowWorkspace.items` 尚未返回正文、定位和可编辑 metadata 等投影字段，Renderer 也没有接入 `patchWorkspace`、条目级冲突处理和修订反馈；`MVP-A` 仍可按默认路线保持只读。

失败条目的有限“局部重试”已经形成端到端路径：Renderer 只收集持久化 `FAILED` 且没有已验证产物的条目，生成请求携带显式 `item_ids`，Engine 按目标范围组装计划并保留其他条目的既有产物。它不覆盖 `AMBIGUOUS`、未决副作用或任意条目范围，也不等于完整的 `retry_scope`/`available_actions` UI 契约；交付中心仍不能把所有失败状态都泛化成安全的“重试此项”。

实现内容核对页前，应先决定：MVP 采用只读核对，还是把现有有限 `item_overrides` 扩展为完整的前端修订/确认闭环；如果还要记录服务端“已确认”，确认本身也必须有明确契约。若开放编辑/跳过 UI，还应补齐条目级版本校验、修订号/计划哈希、明确的跳过操作和首次 TTS attempt 后的冻结规则；改变条目身份的编辑不能静默复用旧音频，必须标记为 `UNRESOLVED` 或要求重新运行。

#### P0：重启恢复的前端闭环与未决任务处理尚未完成

当前 `WorkflowRuntime.ensure_initialized()` 已在服务启动时调用安全恢复、干预过期和限量 GC；正式桌面运行时也已接入 provider-aware 的安全 retry dispatch，并能对 `control_state=RUNNING`、`execution_state` 为 `PREPARING/RUNNING/RECOVERING` 且无未决外部副作用的任务执行有界安全接管。它仍只处理可确认安全的路径，不会接管进行中的外部调用或自动重发不确定提交，也不会自动恢复用户主动暂停的任务。Renderer 仍尚未完成完整活动任务索引和 workspace hydrate。因此可以承诺“恢复事实与上下文”，在运行时启用安全 retry 时可承诺有限的安全任务续接，但不能承诺应用重启后对所有任务自动续跑。`server.py` 中旧 JSON 任务的兼容恢复逻辑也不等于 `/api/v1` workflow 的自动接管。

#### P0：错误反馈不够可用

当前导入/解析失败时可能直接展示类似 `workflow request failed: HTTP 500` 的底层错误。对于用户而言，这不能回答三个关键问题：发生了什么、是否安全重试、下一步应该做什么。

版本化工作流路由的受管异常路径会返回包含 `request_id`、`error_code`、`message`、`retryable`、`side_effect_occurred`、`workflow_id`/`step_id`/`attempt_id` 和 `details` 的结构；工作流/条目相关异常会尽量从请求路径或异常详情补齐可定位的关联 ID。外层鉴权中间件和旧版 `/api` 路由仍可能返回不同形状，不能假设整个 API 面都已经统一。工作流错误信封仍没有本方案后文示例中的完整 `user_message`、`safe_to_retry`、`affected_item_ids`、`recovery_action` 投影；这些字段仍应明确标为 UI 适配层或后端增强的目标，不能把“结构化错误”误写成“完整 UI 问题模型已完成”。

#### 当前实现与可承诺能力边界

| 能力 | 当前 v1 事实 | 首版约束 |
| --- | --- | --- |
| 任务配置/预览 | 生成前完整配置会写入 workflow；Engine 从快照固化声音/参数计划，预览在后端限制为前三条，质量进入 provider profile/真实旧版 MP3 导出 | 仍需补最终产物级质量/格式的更强证据；终态修改走新 rerun，不修改旧 run |
| 条目级重试 | Renderer 和 Engine 已支持针对持久化 `FAILED`、且没有已验证产物的显式 `item_ids` 范围重跑，并保留其他条目的既有产物；`AMBIGUOUS`/未决副作用不进入该路径 | 只有满足上述安全条件才显示“重试失败项”；完整 `retry_scope`、`available_actions` 和未决条目处理仍需统一投影 |
| ZIP 交付 | `POST /api/v1/workflows/{workflow_id}/export-zip` 已按终态成功/部分成功创建或复用确定性 ZIP，仅纳入已验证的 TTS segment，并返回 ZIP Artifact | 继续要求 `READY + verified`；workspace 已提供包含/排除 ID、原因及文件字段，但部分文件名/MIME 是 projection 推导值、时长可能为 `null`，Renderer 尚未消费，大文件流式和传输控制仍待补齐 |
| 音频格式 | Engine 使用 provider receipt 的 `output_format`；legacy Xunfei 路径报告 MP3，测试/模拟 Provider 可能报告 `bin`。真实音频字节的现场可解码性、逐条独立编码和下载链路仍未验收 | 继续保持 `READY + verified` 闸门；真实 Provider/设备证据完成前，不扩大格式支持声明，也不把 `bin` 当作编码格式 |
| 重启/断线恢复 | 启动已接入安全恢复、干预过期和限量 GC；SSE 410/缺口已清除旧游标并由事件流 snapshot 重锚定；正式桌面运行时可对无未决外部副作用的运行执行安全接管，但不会自动重发未决外部调用或恢复用户主动暂停任务，且没有完整 Store workspace hydrate | 只显示安全恢复上下文；只有候选的 `can_takeover`/`can_resume` 证据齐全时，才承诺自动继续 |
| 暂停/取消 | 状态机有控制态；取消是协作式的，路由会设置 `cancel_event`，engine/provider 在若干边界检查并由 cleanup 收敛，但不能保证强制中止进行中的浏览器/网络调用；暂停/恢复已通过 runner 的 `pause_check` 在安全边界协作执行，但重启后不会自动恢复用户主动暂停任务；产物发布前已有最终控制态栅栏，仍需持续覆盖取消竞态；`ProviderCapabilities.supports_resume` 目前只表示可查询/恢复已有供应商作品，不等于 workflow 支持协作式暂停/恢复 | UI 只展示服务端确认的能力；取消收敛前显示处理中或待对账，发布前必须重新校验控制态 |

#### P1：视觉层级过度依赖卡片

现有样式文件规模较大，并存在多轮刷新、重建和桌面可读性补丁。大量白色卡片、圆角、阴影和嵌套面板让内容层级变平，用户需要在多个容器之间寻找真正重要的字段和动作。

#### P1：核心操作的上下文被拆散

- 文档概览、条目数量、角色分配、声音设置和生成入口分布在不同卡片中。
- 生成过程中，状态、进度、异常和日志同时争夺注意力。
- 结果页同时展示整包下载、单条音频、搜索和筛选，但没有明确“交付是否完成”的主线。

#### P1：声音配置的空间效率和选择效率不足

声音浏览器存在多层布局和独立滚动区域。用户需要在角色、声音卡片、搜索筛选和参数面板之间来回移动；当声音数量增加时，比较和回退成本会进一步上升。

#### P0：历史中心的状态投影与恢复动作不安全

历史中心实际上是活动任务的恢复入口。当前实现已经对状态展示做了局部修正，但恢复和交付仍未完全由 workspace/Store 统一驱动：

- 后端普通列表仍返回按更新时间排序且受 `limit` 限制的 `status <> CLOSED` 记录，因此草稿、生成中、阻塞和部分成功任务都会进入同一个列表；服务端已经提供独立的 `/api/v1/workflows/active` 活动任务候选接口，当前 Renderer 已在刷新历史时读取它并在部分恢复提示中使用 `can_resume`、`can_takeover` 和 `requires_reconcile`，但尚未把它接成启动 hydrate、任务选择和 Store 的统一恢复索引，接口本身也没有返回“结果是否被 limit 截断”的标记。
- `historyStatusPresentation` 已使用 `result_status`、`execution_state`、`control_state` 和对账信号派生历史状态，已不再把所有无 Artifact 的活动任务直接当作“文件缺失”；但列表中的可交付数量、待处理数量、按钮禁用条件仍主要来自历史投影的 `available_files`/`completed` 和局部字段，详情页也尚未成为 workspace 驱动的任务视图。
- 当前每条记录提供“查看状态/查看结果”和“归档”动作；归档只允许终态 workflow，不能作为草稿放弃或真正删除。永久删除和草稿放弃仍没有独立后端语义；HTML 当前已明确“列表最多显示最近 20 条，历史事实不会自动删除”，剩余问题是展示上限仍可能让用户误以为列表等于完整活动集。
- 当前 HTML 的侧栏文案已改为“最近生成的音频任务”，但 README 和部分旧产品文案仍以“最近完成”描述历史，需要在发布口径冻结时一并同步。

因此，历史状态正确性虽已有局部修复，完整的恢复入口、workspace/action 统一投影和归档/放弃边界仍属于 P0，而不是视觉阶段的 P1：必须先按五维状态和 `available_actions` 映射状态标题、主动作、禁用原因和视图入口；待这条事实链路完成后，再做列表密度和视觉重构。

#### P2：响应式和可访问性需要纳入设计基线

应用允许较小的窗口尺寸，而当前布局在 900×600 等尺寸下容易出现拥挤、嵌套滚动和操作密度过高的问题。键盘焦点、状态提示、颜色之外的状态表达以及减少动效也应在重构初期定义，而不是最后补救。

### 3.4 术语口径

同一对象在 UI、接口和运行时中使用不同名字会让状态映射和验收失真，后续统一采用以下口径：

| 用户/方案用语 | 领域或代码用语 | 约束 |
| --- | --- | --- |
| 任务 | `workflow` / `workflow_id` | 可持久化、可恢复的用户工作单元；不要用临时 `session` 替代它 |
| 生成运行 | run；诊断层可见 `step` / `attempt` | 一次执行及其尝试；不在普通 UI 中暴露内部 ID |
| 条目 | `work item` / `item_id` | 解析后的单个生成目标，拥有独立状态和版本 |
| 任务生命周期 | `WorkflowStatus`：`DRAFT` / `ACTIVE` / `ABANDONED` / `CLOSED` | 不等同于执行或结果状态 |
| 执行状态 | `ExecutionState` | 表达准备、运行、等待、恢复或终止过程 |
| 控制状态 | `ControlState` | 表达暂停/终止请求及其收敛，不提前承诺结果 |
| 清理状态 | `CleanupState` | 只作为辅助进度，不单独决定页面主标题 |
| 结果状态 | `ResultStatus` | 表达成功、部分成功、失败或取消；只有终态确定后才作为完成文案 |
| 归档 | `archive` / `CLOSED` | 从默认列表隐藏但保留事实；当前没有用户侧取消归档接口，也不等于永久删除 |

“内容核对”“交付中心”是用户可见的工作区名称；`review`、`result page` 只作为代码模块或迁移期间的别名，不能再同时作为产品文案。

## 4. 设计目标与原则

### 4.1 设计目标

1. **任务始终可见**：顶部或侧栏始终显示当前文档、任务状态、阶段和关键计数。
2. **每个工作区只有一个主导的下一步动作**：用户不需要从多个同等重量的按钮中猜下一步；取消、返回、帮助和诊断等控制动作作为次级动作，具体是否可用以服务端能力投影为准。
3. **状态可解释、可恢复**：任何非终态都要说明当前发生了什么、预计下一步是什么、用户是否需要介入。
4. **问题可定位到条目**：批量生成失败时，用户能快速找到失败条目；只有目标范围重试契约成立时才局部重跑，否则明确引导创建新的全量运行。
5. **诊断信息分层**：普通用户先看到结论和动作，日志和事件详情作为按需展开的证据层。
6. **减少视觉噪声**：用排版、分组、留白和分隔线建立层级，减少无意义的容器嵌套。
7. **保留产品亲和力**：小猪形象和轻微的俏皮感可以保留，但不能让核心任务界面变成装饰性首页。

### 4.2 设计原则

- 先表达任务，再表达技术。
- 先给用户结论，再提供日志证据。
- 先解决当前阻塞，再展示完整配置。
- 默认展示常用设置，高级参数按需展开。
- 状态颜色必须配合文字、图标或结构表达，不能只依赖颜色。
- 破坏性或不可逆动作需要明确对象、影响和恢复方式。
- 只在真正有信息价值的时刻使用动效；支持 `prefers-reduced-motion`。

## 5. 设计契约：新版本的统一方向

这是实施前需要团队共同遵守的设计契约。它不是新增的产品文档，不代表已经完成品牌定稿；正式实现前仍需确认。

### THESIS：一句话主张

**这是一个帮助用户把文档可靠地制作成可交付音频的工作台，而不是一个等待结果的表单。**

### WORLD：视觉世界

**教材排版 × 声音信号**。

- 教材排版提供纸张、章节、目录、页边距和编辑秩序感。
- 声音信号提供波形、播放头、录制状态和时间线节奏。
- 两者结合后，界面既有文档工具的可靠性，也有音频工具的即时反馈。

### STORY：用户故事

用户从一份文档开始，沿着“导入 → 核对内容 → 配置声音 → 生成与处理异常 → 检查并交付”的路径前进。每个阶段都能看到上下文、完成度和下一步；即使中途关闭应用，回来后也能恢复同一项任务的上下文，并在服务端确认已接管运行时后继续执行。

### FIRST VIEWPORT：首屏承诺

首屏不追求展示所有功能，而是回答：

- 我现在在哪个任务里？
- 当前任务进行到哪一步？
- 我现在最应该做什么？
- 是否存在需要我处理的问题？

### FORM：界面形态

采用“固定任务轨道 + 单一主工作区 + 按需侧抽屉”的工作台形态。固定轨道提供方向感，主工作区承载当前决策，抽屉承载声音详情和诊断日志，避免把所有内容永久堆在同一屏。

### FINISH：完成标准

用户完成一次任务后，不仅拿到已明确契约的交付包或单条产物，还能清楚知道：总条目数、成功/失败/跳过数量、失败原因、已交付产物以及下次是否可以从当前任务继续。ZIP 只有在后端实际生成并验证 `export-zip` Artifact 后才作为主交付形式。

## 6. 信息架构与应用 Shell

### 6.1 五个逻辑工作区

底层仍然是一条 workflow，但 UI 拆成五个更符合任务心智的逻辑视图：

| 工作区 | 用户问题 | 主动作 | 典型状态 |
| --- | --- | --- | --- |
| 导入 | 我要处理哪份文档？ | 选择或拖入文档 | 空白、接收中、解析中、可继续、失败 |
| 内容核对 | 文档被正确拆成了哪些条目？ | 检查条目；持久化确认、编辑/跳过需先具备相应契约 | 待核对、存在问题、已确认（仅在确认契约存在时） |
| 声音配置 | 谁用什么声音说？ | 分配角色、试听和调整参数 | 未配置、部分配置、已配置 |
| 生成任务 | 任务现在进行到哪里？ | 开始、暂停、恢复、取消、重试、处理阻塞（由能力投影决定） | 准备中、生成中、暂停、等待处理、阻塞、终态 |
| 交付中心 | 哪些音频已经可用？ | 试听、下载、满足安全条件的局部重试、下载确定性 ZIP（范围和能力需契约支持） | 部分完成、已完成、无产物、失败 |

历史记录作为跨任务入口保留在侧栏，不作为主流程中的第六步。打开历史任务时，应恢复到该任务最有意义的工作区，而不是固定返回首页。

### 6.1.1 `MVP-A` 页面进入/退出规则

为避免五个工作区变成五次强制跳转，`MVP-A` 采用“独立但可跳过”的只读内容核对工作区：解析成功后默认进入内容核对，但没有未处理问题时，主动作直接为“进入声音配置”；用户可以通过任务轨道返回前一工作区，不能把离开页面解释成服务端已确认。

| 事实/动作 | 进入工作区 | 退出与主动作 | 约束 |
| --- | --- | --- | --- |
| 解析成功 | 内容核对 | 无问题时“进入声音配置”；发现问题时的出路见下方只读核对规则 | 只读核对不写入服务端确认状态 |
| 配置已保存且未开始 TTS | 声音配置 | “开始生成” | 必须先保存并回显 workflow revision；未保存值不能进入生成请求 |
| 生成已接受 | 生成任务 | “查看问题”或等待终态；不能把事件直接当作终态 | 任务轨道负责跨工作区导航，生成页阶段轨道只负责本次执行阶段 |
| 终态结果已确认 | 交付中心 | “归档”或“重新运行” | 只有 `READY`、`verified=true` 且格式事实一致的 Artifact 才能下载 |
| 历史任务打开 | 由 adapter 按派生用户态选择 | 保留任务上下文并允许安全切换 | 不因打开/切换任务自动发送生成、暂停或取消命令 |

任务选择器与阶段轨道必须分工：任务选择器切换当前 `workflow` 订阅，阶段轨道只切换当前任务内的 `activeView`；两者都不能隐式发送生成、暂停或取消命令。

| 场景 | UI 行为 | 约束 |
| --- | --- | --- |
| 切换任务 | 保存当前视图位置并切换订阅，显示目标任务的 workspace | 不暂停、不恢复、不重新生成；目标 workspace 未同步前不显示可操作命令 |
| 当前任务有未保存配置 | 弹出“保存并切换 / 放弃修改 / 留在当前任务” | 不静默丢弃输入，也不把切换当作保存成功 |
| 生成中切换任务 | 允许切换到其他任务的工作区 | 原任务继续由服务端运行；关闭窗口或离开视图不等于取消 |
| 应用重启 | 先读取活动候选，再按 `can_takeover`/`can_resume` 和 workspace 选择视图 | 没有能力证据时只恢复上下文或问题处理，不显示“继续生成” |

只读核对的出路规则（`MVP-A`）：内容核对为只读时，用户发现拆分或内容错误后不能被困在没有可行动作的视图里。默认出路是“返回导入重新处理”：由导入页重新选择/处理文档并创建新任务，旧任务保留为草稿或按归档语义处理，UI 必须明确说明“当前任务不会保留修改”。后端已有有限 `item_overrides` 原语，待前端修订闭环接入后，本规则由条目级编辑/跳过取代。

### 6.2 建议的 Shell

#### 左侧任务轨道

- 新 Shell 的目标宽度约 220px，可折叠到 64px；这不是当前 CSS 的既有断点，必须在真实 Electron 窗口中按 §7.1 验收并据此调整。
- 顶部为产品标识和“新建任务”。
- 中部为五个工作区，当前工作区显示明确的编号/状态，不只使用颜色。
- 工作区旁显示任务级异常计数或完成度，例如“2 项待处理”。
- 底部为历史任务、服务状态、设置和帮助入口。

#### 顶部任务栏

顶部是上下文栏，而不是第二套导航：

- 文档名和可选的任务别名。
- 条目数量、成功数、失败数等关键计数。
- 当前服务连接状态。
- 当前工作区的辅助动作，例如保存草稿、重新解析、打开诊断。
- 新建任务或切换任务入口。

#### 主工作区

主区域保持稳定的阅读宽度；宽屏时可以使用双栏或三栏，窄屏时将右侧详情折叠为抽屉。所有页面都保留统一的标题、说明、主动作位置和底部反馈区域。

#### 状态提示层

错误、等待用户处理、暂停请求和恢复中的状态使用页面内状态条或局部提示，避免只依赖 Toast。Toast 只用于轻量成功反馈，例如“已复制链接”或“设置已保存”。

## 7. 视觉系统建议

以下是视觉方向的角色定义，色值为实施前可调整的示意 token，不代表最终品牌定稿。

| Token 角色 | 视觉倾向 | 使用场景 |
| --- | --- | --- |
| `canvas` | 温暖的纸张灰米色 | 页面背景、空白工作区 |
| `surface` | 低对比度暖白 | 主内容面、浮层、详情区域 |
| `ink` | 深海军蓝/墨色 | 主文字、侧栏、重要标题 |
| `signal` | 稳定的钴蓝或群青 | 主按钮、当前工作区、链接、进度 |
| `recording` | 柔和珊瑚红 | 播放中、生成中、音频活动状态 |
| `success` | 低饱和薄荷绿 | 成功、已交付、可继续 |
| `attention` | 暖琥珀色 | 等待用户处理、可重试、风险提示 |
| `danger` | 清晰但不刺眼的红色 | 失败、取消、破坏性操作 |

视觉上建议由“白色卡片堆叠”转向“纸面分区 + 少量重点容器”：

- 大面积使用背景、分隔线和留白建立层级。
- 重要任务对象可以使用一块主面板，但不要在面板内无限嵌套卡片。
- 圆角统一为少数等级，阴影只用于真正浮起的菜单、抽屉和确认层。
- 主标题、阶段标题和字段标签形成稳定的字号梯度。
- 正文和状态信息保证窄窗口下仍可读，元信息不低于可接受的最小字号。
- 图标承担语义，不作为纯装饰；重要动作同时显示文字。

动效只服务于“状态正在发生”的理解：

- 页面切换使用短距离、低幅度的淡入或位移动效。
- 生成阶段用细微的进度或播放头动效表达活动状态。
- 状态完成使用一次性确认动效，不持续闪烁。
- 用户开启减少动效时，所有循环动效降级为静态状态。

### 7.1 视觉 token 与响应式最低基线

以下数值是可执行的工程基线，不是最终品牌定稿；正式 token 变更时必须同步更新对比度和窗口验收。

| 类别 | 基线 | 说明 |
| --- | --- | --- |
| 间距 | 4 为最小单位，常用 `8/12/16/24/32` | 同一层级不混用近似间距 |
| 圆角 | 控件 8、面板 12、主要工作面最多 16 | 不在面板内无限嵌套圆角容器 |
| 字级 | 正文 14–16px；元信息 12px 仅用于非关键内容 | 状态、错误和主动作不得只用小字表达 |
| 对比度 | 普通文字至少 4.5:1；大字和图形控件至少 3:1 | 以实际测量为准，不能只凭色板判断 |
| ≥1280px 宽 | 任务轨道 + 主工作区；仅在主区不少于 560px 时启用第三栏 | 第三栏不应挤压条目正文和主动作 |
| 1024–1279px 宽 | 任务轨道 + 主工作区；详情改为抽屉 | 抽屉打开后主动作仍保持可见 |
| 900–1023px 宽 | 任务轨道折叠，主区单栏，详情和问题均进抽屉 | 不出现三栏并行布局 |
| 高度低于 680px | 底部主动作固定在可见区域，日志/列表单独滚动 | 避免页面、面板、列表三层嵌套滚动 |

抽屉打开时焦点进入抽屉的标题或首个控件，关闭后恢复到触发按钮；所有仅 hover 的信息必须有键盘或触摸等价路径。`900×600` 必须按单栏降级规则完成导入、配置、启动和查看错误。

## 8. 核心页面方案

### 8.1 导入页：从“上传卡片”变成“任务入口”

#### 页面结构

1. 页面标题：`新建配音任务`。
2. 中央导入区：拖拽、选择文件、支持格式说明、文件大小/名称反馈。
3. 下方提供最近使用的配置预设和最近任务入口。
4. 导入后在原地进入三段式状态：接收中 → 解析中 → 可核对。
5. 失败时显示用户可理解的原因、是否可重试和可执行动作。

#### 关键交互

- 拖入文件后立即显示文件名、大小和正在进行的阶段。
- 解析期间允许取消（目标能力），取消要说明是否会保留已创建的草稿任务；当前 v1 没有解析取消接口，Renderer 的 `AbortController` 只会让本地回调失效，并不会中止已发到服务端的解析。若首版不补接口，动作应命名为“停止等待/返回导入”，不能文案承诺服务端已取消。
- 解析失败后保留任务上下文（目标能力）；当前 v1 的失败路径只在 Renderer 显示错误并重置导入区，尚未提供同一任务的“重试解析”入口。若首版不补这条链路，应只提供“返回重新选择”，不要让用户误以为原任务仍可直接继续解析。
- 如果检测到已有活动任务，优先提示“继续上次任务”，而不是静默创建重复任务；但只有服务端返回可续跑能力时才提供继续动作，否则显示“恢复上下文/查看问题”。

### 8.2 内容核对页：让用户确认“文档被怎样理解”

这是新版本建议重点增加或明确化的工作区。语音生成的质量首先取决于拆分出的条目是否正确。

#### 页面结构

- 左侧：文档章节/标题树，显示每节条目数和问题数。
- 中间：条目列表，按序号、标题、正文摘要和当前状态展示。
- 右侧：选中条目的检查面板，展示原文、角色、可选的局部覆盖设置和错误详情。
- 顶部：解析结果摘要，例如“共 48 个条目，2 个需要确认”。

章节/标题树依赖解析投影提供稳定的标题、父级和定位字段；当前 workspace 甚至还没有 `item_type`、`normalized_content`、`source_locator` 和 `metadata.section`，如果首版不扩展 workspace 或提供条目详情接口，页面只能降级为按序号展示的扁平结构预览，不得由 Renderer 猜测章节结构或把 hash 当作正文摘要。

#### 关键交互

- 点击章节树快速定位条目。
- 单条目可编辑或标记为跳过，编辑后显示“已修改”标记；这是前端目标能力，需先接入现有条目覆盖并补齐 workspace 投影、条目级版本和修订反馈。
- 在修订契约允许时，对单条目设置角色覆盖，而不强迫用户回到全局声音页。
- 只有在存在未处理问题时才强调“需要确认”，普通条目不制造额外步骤。
- 只读版（`MVP-A`）发现问题时的主动作是“返回导入重新处理”，并说明旧任务去向（保留草稿或归档）；不显示编辑/跳过等未接入的控件。

实现前置条件：当前解析后任务已经进入 `ACTIVE`；现有 `patchDraft`/`PATCH /api/v1/workflows/{workflow_id}/workspace` 已能承载有限的角色、声音、正文、metadata 和 `PENDING/SKIPPED` 覆盖，但 `WorkflowWorkspace` 尚未提供编辑所需的正文/定位字段，Renderer 也未接线条目修订与冲突处理。因此，若暂不补齐前端投影和修订闭环，`MVP-A` 应将本页限定为只读核对；“进入声音配置/生成”只是 UI 导航，不应显示为已经写入服务端的“确认”。如果产品确实需要持久化确认，必须新增明确的确认字段或命令，并把它纳入版本校验、SSE 事件和验收 fixture。

### 8.3 声音配置页：从“浏览器”变成“角色编排台”

#### 页面结构

- 左栏：角色列表。每个角色显示名称、性别/类型、当前声音、配置状态和试听入口。
- 中央：角色配置摘要和试听区域，展示当前声音、语速、音调、音量等常用参数。
- 右侧抽屉：声音目录，包含搜索、供应商/语言/性别/风格筛选、最近使用和预览。
- 高级参数默认折叠，只有需要精细调校时才展开。

#### 关键交互

- 先选择角色，再打开声音目录；不要让用户一开始面对所有声音。
- 声音试听使用明确的播放中状态和停止动作，避免多个声音同时播放。
- 切换声音时保留角色的其他参数，除非新声音不支持某参数并明确提示。
- “应用到所有同类角色”作为次级动作，避免误覆盖。
- 在离开页面前，如存在未保存的草稿修改，显示轻量的未保存状态。
- 首次真实生成前显式展示 Provider 登录态：未登录时提供“打开登录/重新检测”，会话失效或浏览器关闭时提供“重新登录”，未获得 provider-ready 证据前不允许提交生成。

### 8.4 生成任务页：从“进度展示”变成“任务控制台”

#### 页面结构

- 顶部：任务标题、总进度、成功/失败/待处理计数。
- 左侧或顶部：阶段轨道，显示导入、解析、准备、生成、校验、交付的状态。
- 中央：当前阶段主面板，展示当前正在处理的目标条目、速度、预计剩余信息（后端有可靠数据时才展示）。
- 右侧或下方：问题清单，按“需要用户处理 / 可重试 / 仅供参考”分组。
- 诊断时间线作为可展开抽屉，默认展示摘要。

#### 主动作规则

- 准备中：`取消任务`；如果取消需要等待外部调用收敛，要明确显示等待原因。
- 生成中：只有服务端能力投影确认可协作暂停时才显示 `暂停生成`；否则显示“等待当前调用完成”或可用的停止/取消动作。当前仅当条目已持久化为 `FAILED`、没有已验证产物且请求携带明确 `item_ids` 时，才显示“重试此项”；`AMBIGUOUS` 或未决副作用必须进入对账/解决路径。
- 暂停后：`继续生成`。
- 等待用户处理：`查看问题`，跳转到具体条目或动作。
- 阻塞：`处理阻塞` 或“发起/查看对账”，并解释执行影响；“对账”不能被文案暗示成已经自动解决问题。
- 部分成功：`查看失败项`、在重试范围契约成立时 `重试失败项`、`下载已完成内容`。
- 已完成：`前往交付中心`。

按钮存在不等于控制已经生效：当前后端的取消是协作式的，外部提交/下载调用无法保证被强制中止；暂停/恢复已接入 runner 的安全边界，但同样不能中断已经进行中的浏览器/网络调用。`complete_tts` 已在发布产物前设置最终控制态栅栏，仍需持续覆盖取消竞态。因此 UI 必须根据服务端确认结果更新文案，不能在点击后立即承诺“已暂停”或“已取消”，后端也必须在发布产物前重新校验控制态和版本。

#### 诊断层级

第一层只回答“现在是什么状态”。第二层显示目标条目、失败原因和建议动作。第三层才展示事件时间线、请求标识和技术细节，便于排查但不打扰普通用户。

### 8.5 交付中心：让“产物是否可交付”一目了然

#### 页面结构

- 顶部交付摘要：`48 个条目 · 46 个成功 · 2 个失败`。
- 条件主按钮：下载 ZIP。当前 v1 可在终态成功/部分成功时按需创建或复用确定性 `export-zip` Artifact，但只包含已验证的 TTS segment；workspace/export 响应已经返回包含/排除 ID、原因和文件字段，部分文件名/MIME 是 projection 推导值、时长可能为 `null`，Renderer 尚未把它们渲染到交付中心。只有取得 `READY` 且 `verified=true` 的 Artifact 后才显示可用下载动作，大文件流式仍是后续能力。
- 中央列表：条目名称、角色、时长/大小（有数据时）、生成状态、试听按钮和更多操作。
- 选中条目后展开右侧播放器/详情，而不是默认把所有播放器同时铺开。
- 失败项在满足持久化 `FAILED`、无已验证产物、目标范围和版本条件时显示可执行的“重试此项”；未决/需对账条目显示“查看问题”或“提交证据”，不显示重试动作。

#### 关键交互

- 播放器包含播放/暂停、进度、当前条目名称和停止其他播放的行为。
- 搜索、筛选服务于定位问题，不能替代总体交付摘要。
- ZIP 下载前显示已知包含范围，例如“将包含 46 个成功音频”；当前 API 已返回 `included_item_ids`、`excluded_item_ids` 和排除原因，UI 必须逐条消费这些事实，不能只显示一个成功总数。
- ZIP 的包含规则必须由后端返回并可解释：明确成功、失败、跳过、未决/待对账条目是否纳入；部分成功包应显示范围和结果状态。
- 下载失败时提供重试和复制诊断信息的入口。

### 8.6 历史页：从记录表变成任务恢复中心

#### 页面结构

- 顶部搜索、状态筛选和时间排序。
- 任务列表显示：文档名、创建/更新时间、条目数、成功/失败数、当前状态、最近动作。
- 每条记录提供与事实匹配的主动作：未结束且服务端确认可续跑时提供“继续任务”，阻塞/待对账时提供“查看问题”，终态记录提供“查看结果”。
- 终态记录的次级菜单提供归档；草稿的“放弃/关闭”需另有后端语义，不能直接复用归档接口。只有确认存在真实删除能力时才提供删除。

#### 语义要求

- `ACTIVE`、`DRAFT`、`ABANDONED`、`CLOSED` 等后端状态不能直接裸露给用户，要映射成中文状态和可执行动作。
- “归档”表示从默认列表隐藏但保留事实；当前没有用户侧取消归档接口，若要继续处理，应通过受控重跑创建新任务，而不是恢复原记录。“删除”表示真正移除，二者必须分开。
- 打开历史任务后，恢复到任务当前最相关的工作区：执行中进入生成任务页，有阻塞进入问题页，有结果进入交付中心，草稿进入内容核对或声音配置。

## 9. 状态模型与交互契约

### 9.1 单一事实来源

建议把后端 `WorkflowSnapshot` 作为工作流事实来源，把 SSE 作为快照更新和事件通知通道；页面还需要一个独立的 `WorkflowWorkspace` 投影，承载条目、产物、配置、问题和服务端可用动作。前端本地只保存不属于后端业务状态的 UI 状态，例如：

```text
uiState = {
  activeView,
  selectedItemId,
  selectedRoleId,
  openDrawer,
  filters,
  audioPlayerState,
  pendingNavigation
}
```

其中 `activeView`、选择项和播放器状态是 UI 状态；当前任务 ID 即使写入本地，也只能作为启动提示，不能替代服务端恢复结果。任务配置、草稿修订和生成计划必须持久化在后端。不要再用 `currentStep` 推断任务真实状态；视图可以根据快照和 workspace 投影计算，但不能反过来覆盖后端状态。

建议的分层边界是：

- `workflowStore`：保存规范化的快照、事件游标、连接状态和命令 pending；幂等键的生成/传递由 API adapter 或独立 command layer 负责，Store 只负责协调 pending 和结果收敛。
- `workspaceProjection`：由快照、条目、产物、配置和问题聚合而成，负责提供页面所需的数据。
- `uiState`：只保存视图、选择、抽屉、筛选和播放器等临时状态。

### 9.2 后端状态到 UI 状态的映射

后端工作流不是一个可以直接映射到文案的枚举，而是多个正交维度的组合：`WorkflowStatus` 表示生命周期，`ExecutionState` 表示执行进度，`ControlState` 表示暂停/终止控制，`CleanupState` 表示清理进度，`ResultStatus` 表示结果。下面的用户态必须由 adapter 根据组合状态派生：

| 用户态（派生） | 事实条件 | 用户看到的文案/页面表现 | 可用动作 |
| --- | --- | --- | --- |
| 草稿待配置 | `status=DRAFT` 且尚未进入执行 | 草稿，显示缺少的配置项 | 编辑、解析；放弃需另有后端动作 |
| 已放弃 | `status=ABANDONED` | 任务已放弃，只读保留事实 | 查看事实；仅在能力投影允许时创建新运行，不显示继续生成 |
| 已归档 | `status=CLOSED` | 已归档；默认从历史列表隐藏 | 只读查看（若能直接打开）；不把归档当作成功或删除 |
| 等待初始化/输入 | `execution_state=CREATED` 且尚未满足草稿或解析条件 | 等待导入、初始化或补充输入 | 按 `available_actions` 执行下一步 |
| 待核对/待配置 | 解析步骤已完成、尚无 TTS step/attempt，通常为 `execution_state=PREPARING` | 显示解析摘要、内容核对和声音配置 | 修订/确认（以契约为准）、开始生成 |
| 准备/生成 | 已确认配置且已有 TTS step/plan，`execution_state=PREPARING\|RUNNING` 且 `control_state=RUNNING` | 阶段轨道、进度、当前目标和计数 | 取消；仅在后端真正支持时暂停 |
| 正在暂停 | `control_state=PAUSE_REQUESTED` | 请求处理中，禁止重复点击 | 等待服务确认 |
| 已暂停 | `control_state=PAUSED` | 保留进度和上下文 | 继续、取消 |
| 等待重试 | `execution_state=WAITING_RETRY` | 显示可重试目标和原因 | 仅当 `available_actions` 返回安全的目标范围时重试；否则查看详情或创建新运行 |
| 需要用户处理 | `execution_state=WAITING_USER` | 顶部异常条 + 问题清单 | 查看问题、执行指定动作 |
| 正在恢复 | `execution_state=RECOVERING` | 展示恢复阶段和已保留进度 | 等待；仅在后端允许时取消 |
| 阻塞/取消收敛 | `execution_state=BLOCKED`，尤其是取消后同时 `control_state=TERMINATING` | 解释阻塞原因、外部副作用和下一步 | 发起/查看对账、提交解决证据、查看诊断 |
| 已完成 | `execution_state=TERMINAL`、`control_state=TERMINATED`、`result_status=SUCCEEDED` | 进入交付中心，显示完整产物摘要 | 试听、下载、归档 |
| 部分完成 | 终态组合 + `result_status=PARTIAL_SUCCESS` | 成功内容可交付，失败项可定位 | 下载成功项、按 `retry_scope` 重试失败项 |
| 生成失败 | 终态组合 + `result_status=FAILED` | 显示结论、失败目标和恢复建议 | 按 `retry_scope` 重试或创建新运行、查看诊断 |
| 已取消 | 终态组合 + `result_status=CANCELLED` | 保留取消位置和已有产物 | 查看结果、重新运行（以能力投影为准） |

映射规则：

- `DRAFT/ACTIVE/ABANDONED/CLOSED` 不能与执行态或结果态互相替代；`ABANDONED` 表示放弃，`CLOSED` 表示归档，二者都不等于已成功。
- `SUCCEEDED/PARTIAL_SUCCESS/FAILED/CANCELLED` 属于 `ResultStatus`，不能写入或冒充 `ExecutionState`。只有执行终止、控制终止且结果已确定时，才显示最终结果文案。
- `PAUSE_REQUESTED/PAUSED/TERMINATING/TERMINATED` 属于 `ControlState`；`CleanupState` 只作为清理进度的辅助提示，不应单独决定主标题。
- `DRAFT`、`ABANDONED`、`CLOSED` 先作为生命周期处理；其中 `ABANDONED/CLOSED` 只进入生命周期文案，不进入“已完成/失败/取消”等结果文案。对于仍可执行的 workflow，只有执行态为 `TERMINAL`、控制态为 `TERMINATED` 且结果已确定时才进入终态文案。其余非终态先显示 `TERMINATING/PAUSE_REQUESTED/PAUSED` 控制覆盖层，再按 `RECOVERING > BLOCKED > WAITING_USER > WAITING_RETRY > PREPARING > RUNNING > CREATED` 选择执行主态。`PREPARING` 需要结合解析步骤、TTS 计划和可用动作区分“待配置”和“生成准备中”，不能只按枚举值写文案。
- 发送取消命令后，后端首先可能处于 `control_state=TERMINATING`、`execution_state=BLOCKED`，且 `result_status` 仍为 `IN_PROGRESS`。此时只能显示“正在取消/等待对账”，不能提前显示“已取消”。

### 9.3 事件处理规则

- SSE 事件只触发状态刷新、局部提示或时间线追加，不直接决定长期 UI 状态。
- 当前 Store 的事件消费会更新内存中的 workflow 最新事件、游标和序号，但不会自动补齐条目、产物或完整快照；收到关键事件后应重新拉取快照和 workspace 投影，避免仅凭事件顺序拼接出不完整状态。
- 事件丢失、断线或版本落后时，使用 `state_version` / `last_event_id` 做补偿刷新；出现序号缺口或 `CURSOR_EXPIRED`/HTTP 410 时，必须丢弃旧游标，重新拉取快照/workspace 并从新游标重新建立 SSE，而不是继续用同一游标盲目重试。为此，`workflow-proxy`/Preload 必须保留结构化 `error_code` 和 HTTP 状态，Store 必须提供按 workflow 清除 cursor/seq 的 `reset`（或等价）能力；在这两个前置条件落地前，不得把 410 重试称为“已恢复”。
- 按钮在命令发送后进入 pending 状态，服务确认前不能重复发送同一命令。
- 当状态变化导致当前视图不再适用时，保留用户上下文并给出可解释的视图迁移，例如“任务已暂停，仍停留在生成任务页”。

### 9.3.1 事件到 UI 的最小契约矩阵

事件本身不是终态，也不是可点击动作的来源。以下矩阵规定首版收到事件后的最小行为；未列出的事件按“记录诊断 + 拉取最新快照”处理，不能直接改变用户态或打开高风险动作。

| 事件 | 必须刷新 | UI 反馈 | 动作规则 |
| --- | --- | --- | --- |
| `TTS_PLAN_PREPARED` | snapshot、items、workspace | 更新“准备中”和条目总数 | 重新按能力投影计算主动作 |
| `TTS_SUBMISSION_IN_FLIGHT` | snapshot、workspace | “正在提交，结果待确认” | 禁止重发同一提交，不显示安全重试 |
| `TTS_SUBMISSION_AMBIGUOUS`、`RECOVERY_REQUIRES_RECONCILE`、`RECOVERY_REQUIRES_EXTERNAL_RECONCILE` | snapshot、items、artifacts、workspace | “生成结果待核验” + 对账问题 | `requires_reconcile=true` 时只显示查看/提交证据，不显示重试 |
| `TTS_SUBMISSION_REJECTED` | snapshot、items、artifacts、workspace | 显示失败原因和可恢复动作 | 以 `safe_to_retry + retry_scope` 决定重试；不由事件名推断 |
| `GENERATION_TASK_FAILED` | snapshot、items、artifacts、workspace | 显示任务异常，并核对持久化状态是否已经收敛 | 只有 snapshot/workspace 已落到 `WAITING_RETRY`、`WAITING_USER` 或终态时，才按 `safe_to_retry + retry_scope` 提供动作；事件单独不能打开终态失败页 |
| `TTS_OUTPUT_VERIFIED` | snapshot、items、artifacts、delivery | 更新产物摘要 | 只有终态快照确认后才进入交付完成文案 |
| `RETRY_CLAIMED`、`INTERVENTION_EXPIRED` | snapshot、workspace | 更新等待/问题状态 | 不自动发送新命令 |
| 取消命令已被服务端接受（无专用事件） | snapshot、workspace | `control_state=TERMINATING/BLOCKED` 期间显示“正在取消/等待对账” | 当前实现没有“取消已受理”事件，取消中状态由命令响应 + 快照表达；只有 `TERMINAL + TERMINATED` 的快照按 `result_status` 派生终态 |
| `WORKFLOW_CLEANUP_COMPLETED` | snapshot、workspace | 非用户取消的清理收敛后按快照重新派生状态 | 不把清理完成解释为成功或失败，以快照 `result_status` 为准 |
| `WORKFLOW_CANCELLED` | snapshot、items、artifacts、workspace、delivery | 只有终态快照确认后才进入“已取消”或“部分完成”文案 | `result_status=CANCELLED` 才显示“已取消”；若为 `PARTIAL_SUCCESS`，显示部分完成并保留成功产物，不能由事件名覆盖结果 |
| `WORKFLOW_ARCHIVED` | workflow 列表、当前 snapshot | 从默认历史列表移除并提示已归档 | 不将归档解释为删除或成功 |

> 事件矩阵更正（2026-08-29 评审）：后端从未发出 `WORKFLOW_CANCEL` 事件；上表已按代码事实将“取消请求通知”改为“取消命令已被服务端接受（无专用事件）”。Renderer 中处理 `WORKFLOW_CANCEL` 的分支（`app.js`）是后端从未触发的死代码，Store 接线时应移除。

### 9.4 异常与降级矩阵

工作流快照不是全部异常事实；导入、命令和产物也可能各自失败。首版至少用下面的矩阵约束页面行为，避免每个视图自行猜测恢复方式：

| 来源/信号 | 典型事实 | 默认 UI 表现 | 恢复规则 |
| --- | --- | --- | --- |
| 服务启动 | 服务未启动、认证失败、依赖未就绪 | 顶部服务条说明原因；新建/命令按钮禁用 | 仅重试连接或修复依赖，不重复提交任务 |
| Provider/登录态 | 首次真实生成需要讯飞浏览器登录；会话失效、浏览器关闭或 provider 未就绪 | 显示登录/检测/重新认证状态；未确认前禁用生成 | 只打开或重新检测登录，不重复提交可能已有副作用的生成；状态恢复后再按能力投影启用生成 |
| 源文件/解析 | 上传取消或过期、解析为空、部分解析 | 保留文件和任务上下文；说明是否可重试以及已保存内容 | 先确认原上传/解析请求已结束，或由服务端状态检查/幂等保证不会重复提交，再重试上传/解析；空结果不能进入生成；部分结果必须标出未解析范围 |
| 命令响应 | `409 STATE_CONFLICT`、超时、幂等重放 | 先拉取最新快照/workspace；超时显示“结果待确认”，不立即再次发送 | 幂等重放优先使用服务端返回结果；未决副作用转入对账 |
| SSE | 序号缺口、`CURSOR_EXPIRED`/HTTP 410、连接断开 | 停止旧流并显示同步中 | 清除对应 workflow 的游标和序号，先拉快照/workspace，再用新游标订阅；禁止带旧游标盲目重试 |
| 配置保存 | 版本冲突、保存失败、离开页面有未保存修改 | 明确“未保存”，保留可恢复的临时输入，不允许伪装成已生效配置 | 重新加载服务端配置后让用户选择合并/覆盖；生成前必须再次回显有效配置 |
| 产物/播放 | Artifact 未就绪、缺失/无效、下载票据过期、播放器失败 | 条目显示不可交付原因；不从文件名或默认格式推断成功 | 重新申请票据或重试下载；产物事实异常时回到问题清单，不直接标记成功 |
| 多任务切换 | 多个未结束任务或当前任务快照过期 | 任务选择器显示每个任务状态和更新时间 | 切换只改变当前订阅和视图，不改变服务端控制状态；命令必须带最新版本，防止切换后重复提交 |

## 10. 后端接口配合建议

当前工作流 API 已经暴露了获取任务、活动任务候选、workspace、列表、条目、产物、草稿 patch、解析、生成、暂停、恢复、取消、重试、对账、重跑、SSE 和产物下载票据等领域原语。配置持久化、SSE 游标清理/事件流 snapshot 重锚定、启动安全恢复、`READY + verified` 交付、终态 rerun、失败条目目标范围执行、确定性 ZIP 和安全 retry dispatch 已接通；服务端已经返回 workspace/action、配置修订和交付包含/排除字段，但 Renderer 尚未完成消费与状态投影。“重试/对账”仍需区分安全失败重试与未决副作用，编辑/跳过的前端修订闭环、未决调用恢复、workspace hydrate、大文件端到端流式和前端传输控制仍有缺口。

### 10.1 P0：建议优先确认或补齐

#### A. 活动任务恢复入口

服务端现在已有 `GET /api/v1/workflows/active` 活动任务候选接口，并返回 `can_resume`、`can_takeover`、`resume_reason` 和 `requires_reconcile`；普通 `GET /api/v1/workflows` 仍是按 `limit` 限制的历史投影，不能代替活动索引。当前缺口已经从“后端没有入口”转为“Renderer 只有局部接线”：`app.js` 的 `refreshHistoryRecords` 已并行读取普通列表和活动候选，并在部分历史恢复提示中使用候选能力，但启动/历史打开尚未把候选接成 hydrate workspace、任务选择和 Store 的统一恢复索引。前端应以活动候选作为恢复索引，再对候选 workflow 调用 `GET /api/v1/workflows/{workflow_id}` 和 `GET /api/v1/workflows/{workflow_id}/workspace`；adapter 综合 `status`、`result_status`、`execution_state`、`control_state`、`cleanup_state`、能力和 `requires_reconcile` 决定可处理任务。不要把所有 `ACTIVE` 记录都直接当成可续跑任务。

启动流程应是：连接服务 → 获取活动候选 → 有界地拉取每个候选的 snapshot/workspace → 派生可处理任务和视图 → 恢复视图。“有界”的数值须在 G1 冻结：建议候选数上限 8、并发拉取 ≤ 2、单候选拉取超时后降级为“无法同步任务状态”；`/workflows/active` 必须返回是否截断（或支持分页），避免把第一页当成完整活动集。单个候选可以自动打开，但仍需依据候选能力和 workspace 判断主动作；多个候选时展示选择器并说明各自状态，不能静默选择“最近的一条”。如果任务处于 `WAITING_USER`、`BLOCKED` 或副作用未对账状态，应恢复到问题处理/对账界面，而不是显示“继续生成”。候选详情拉取失败时保留记录并显示“无法同步任务状态”，不能把它当成无产物或终态失败。

如果首版承诺“自动继续同一运行”，服务端必须在进程启动时完成恢复扫描和运行时任务接管，Renderer 只负责重新建立连接和订阅。当前实现已经对无未决外部副作用、处于安全执行态的运行提供有限接管，但不会自动重发未决外部调用，也不会自动恢复用户主动暂停任务；因此 `MVP-A` 仍至少要返回明确的 `can_takeover`/`can_resume` 能力和禁用原因，前端不能把仅存在数据库中的任务显示成可继续生成。当前运行时还有单生成槽位和有限队列，因此 Shell 不能只有“启动时选一次任务”：任务栏/侧栏还需要显示活动任务列表、每个任务的后台状态和安全切换入口。

#### B. 快照中的操作能力

不要把面向 UI 的字段无条件塞进规范领域快照。建议由快照、条目、产物和问题聚合出 `WorkflowWorkspace` 投影，并返回：

`WorkflowWorkspace` 的 schema、版本号和 fixture 属于 P0 契约闸门；当前服务端已提供 `GET /api/v1/workflows/{workflow_id}/workspace`，前端可以直接以它作为初始化和重同步的权威聚合来源，也应保留按领域接口组合的降级适配。该接口不再是等待后端新增的前置条件；当前 P0 是把它接入 Store、adapter、页面渲染和命令后的收敛流程，不能继续让 `app.js` 只读取零散列表和本地统计。

```ts
type RetryScope = 'NONE' | 'WORKFLOW' | 'ITEMS';
type ActionKind = 'SERVICE' | 'UI';
type ServiceActionType =
  | 'PARSE' | 'SAVE_CONFIGURATION' | 'GENERATE' | 'PAUSE' | 'RESUME'
  | 'CANCEL' | 'RETRY' | 'RECONCILE' | 'RESOLVE' | 'ARCHIVE'
  | 'ABANDON' | 'RERUN' | 'EXPORT_ZIP';
type UiActionType =
  | 'OPEN_VIEW' | 'DOWNLOAD_ARTIFACT' | 'DOWNLOAD_ZIP' | 'RECONNECT';
type WorkspaceActionType = ServiceActionType | UiActionType;

type UiActionTarget =
  | { target_type: 'WORKFLOW'; workflow_id: string }
  | { target_type: 'STEP'; step_id: string }
  | { target_type: 'ITEM'; step_id: string; item_id: string }
  | { target_type: 'WORK_UNIT'; work_unit_id: string }
  | { target_type: 'WORK_UNIT_ATTEMPT'; work_unit_attempt_id: string }
  | { target_type: 'PROVIDER_RECEIPT'; provider_receipt_id: string }
  | { target_type: 'EXTERNAL_OPERATION'; external_operation_id: string }
  | { target_type: 'ARTIFACT'; artifact_id: string }
  | null;

type WorkspaceAction = {
  kind: ActionKind;
  type: WorkspaceActionType;
  enabled: boolean;
  reason: string | null;
  target: UiActionTarget;
  expected_state_version: number | null;
  expected_target_state_version: number | null;
  expected_group_state_version: number | null;
  safe_to_retry: boolean;
  retry_scope: RetryScope;
};

type WorkspaceBlocker = {
  code: string;
  title: string;
  message: string;
  severity: 'INFO' | 'WARNING' | 'ERROR' | 'BLOCKING';
  affected_item_ids: string[];
  retryable: boolean;
  safe_to_retry: boolean;
  retry_scope: RetryScope;
  requires_reconcile: boolean;
  recovery_action: WorkspaceAction | null;
};

type WorkspaceItem = {
  item_id: string;
  item_identity_key: string;
  sequence: number;
  content_hash: string;
  item_type: string | null;
  normalized_content: string | null;
  source_locator: string | null;
  metadata: Record<string, unknown>;
  status:
    | 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED'
    | 'AMBIGUOUS' | 'CANCELLED' | 'SKIPPED' | 'UNRESOLVED';
  role: string | null;
  voice_key: string | null;
  attempt_count: number;
  error_code: string | null;
  user_message: string | null;
  retry_scope: RetryScope;
  requires_reconcile: boolean;
  artifact_ids: string[];
  updated_at: string;
};

type WorkspaceArtifact = {
  artifact_id: string;
  workflow_id: string;
  item_id: string | null;
  step_id: string | null;
  work_unit_id: string | null;
  artifact_type: string;
  lifecycle_state: 'TEMP' | 'READY' | 'INVALID' | 'DELETED';
  format: string | null;
  extension: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  sha256: string | null;
  verified: boolean;
  filename: string | null;
  duration_ms: number | null;
  producer: string;
  producer_version: string;
  created_at: string;
  updated_at: string;
};

type EffectiveConfiguration = {
  provider: string;
  generation_mode: 'composite_cut' | 'single_segment';
  format: string;
  quality: string | null;
  preview: boolean;
  preview_limit: number | null;
  rate: number | null;
  pitch: number | null;
  volume: number | null;
  default_female_voice: string | null;
  default_male_voice: string | null;
  role_voices: Record<string, string | null>;
  role_configs: Record<string, {
    rate: number | null;
    pitch: number | null;
    volume: number | null;
  }>;
};

type ConfigurationProjection = {
  configuration_revision: number;
  configuration_hash: string;
  effective: EffectiveConfiguration;
  source_priority: Record<string, 'GLOBAL' | 'ROLE' | 'ITEM'>;
  frozen_fields: string[];
};

type WorkflowWorkspace = {
  schema_version: number;
  snapshot: WorkflowSnapshot;
  progress: {
    total: number;
    completed: number;
    failed: number;
    cancelled: number;
    skipped: number;
    pending: number;
    percent: number;
    deliverable_percent: number;
  };
  blockers: WorkspaceBlocker[];
  available_actions: WorkspaceAction[];
  current_target: { item_id: string; label: string; started_at: string } | null;
  items: WorkspaceItem[];
  artifacts: WorkspaceArtifact[];
  configuration: ConfigurationProjection;
  provider: {
    status: 'UNKNOWN' | 'CHECKING' | 'READY' | 'LOGIN_REQUIRED' | 'EXPIRED' | 'UNAVAILABLE';
    can_generate: boolean;
    reason: string | null;
  } | null;
  delivery: {
    zip_artifact_id: string | null;
    zip_available: boolean;
    included_item_ids: string[];
    excluded_item_ids: string[];
    exclusion_reasons: Record<string, string>;
  };
  sync: {
    state_version: number;
    last_event_id: string | null;
    requires_resync: boolean;
  };
};
```

上例是 G1 的最小可执行轮廓，不要求首版立即新增聚合接口，但所有字段、枚举和空值语义必须在正式 schema 与 fixture 中保持一致。这里将 `configuration_revision` 定义为 workflow 内单调递增的整数，且不等于 `draft_revision`；如果实现改用 opaque string，必须同步修改 schema、生成请求和验收 fixture。`provider` 只作为经过能力校验的非敏感配置返回，账号凭据和本地路径不得进入 projection。

当前实现与该轮廓的字段级差异如下（依据 `workflow/workspace.py` 当前投影核对）：

| 字段组 | 状态 | 当前事实 |
| --- | --- | --- |
| 顶层 `schema_version`、`snapshot`、`blockers`、`available_actions`（含 `kind=SERVICE/UI`）、`current_target`、`configuration`、`delivery`、`sync` | 已有 | `GET .../workspace` 已返回；`sync` 含 `state_version/last_event_id/requires_resync` |
| `progress` 的 `total/completed/failed/skipped/pending/percent` | 已有 | 缺 `cancelled` 与 `deliverable_percent`（待补） |
| `items` 的身份/顺序/hash/状态/角色/`voice_key`/尝试/错误/`retry_scope`/`requires_reconcile`/`artifact_ids`/`updated_at` | 已有 | 缺 `item_type`、`normalized_content`、`source_locator`、`metadata`（待补） |
| `artifacts` 的契约字段及 `format/extension/mime_type/size_bytes/sha256/verified/filename/producer` | 已有，部分推导 | `extension/mime_type` 由 `format` 推导；`duration_ms` 恒为 `null`；`filename` 为 projection 生成值 |
| `configuration` 的 `configuration_revision/configuration_hash` 与脱敏 `effective` | 已有 | revision 以服务端保留标记持久化，公共投影只返回数值 |
| `available_actions` 的 workflow/target/group 版本字段 | 部分已有 | 当前 `RERUN` 把 `group_state_version` 填入 `expected_state_version`；待补独立的 `expected_group_state_version`，否则不能把该动作当作通用可执行命令 |
| `items` 的 `item_type/normalized_content/source_locator/metadata`、独立 `provider` 投影组、`EXPORT_ZIP` 动作、`progress.cancelled/deliverable_percent` | 待补 | 按本节 schema 冻结后补入 workspace 或专门接口 |

为使 G1 成为可执行契约，字段不能停留在 `[...]` 或省略号：

- `available_actions` 必须区分动作来源。建议拆成 `service_actions` 和 `ui_actions`；如果首版仍使用一个数组，至少增加 `kind=SERVICE|UI`。`SERVICE` 才是服务端状态机/能力投影，必须带所需的 workflow/target/group 版本、目标和安全重试语义；`UI` 只表示导航、重连、保存本地输入或申请下载票据等客户端动作，不能被当作 workflow command。`DOWNLOAD_ARTIFACT`/`DOWNLOAD_ZIP` 这类 UI 动作只能在对应 Artifact 已同时满足 `READY + verified` 时派生；`POST /export-zip` 会创建或复用 Artifact，应建模为独立的 `SERVICE` action（建议名为 `EXPORT_ZIP`），不能把“准备打包”和“下载已有 ZIP”塞进同一个 UI 动作。
- `WorkflowCommandType` 至少冻结为 `PARSE`、`SAVE_CONFIGURATION`、`GENERATE`、`PAUSE`、`RESUME`、`CANCEL`、`RETRY`、`RECONCILE`、`RESOLVE`、`ARCHIVE`、`ABANDON`、`RERUN`、`EXPORT_ZIP`；`RETRY` 通过 `retry_scope` 区分全量/条目范围，`ABANDON` 仅在产品决定提供草稿放弃语义并冻结其状态转移后启用。`SAVE_CONFIGURATION` 虽然由配置页触发，但会写入服务端，必须归入 `SERVICE`。`EXPORT_ZIP` 只表示服务端准备/复用 ZIP，完成后再派生纯 UI 的 `DOWNLOAD_ZIP`；`UiActionType` 另行冻结为 `OPEN_VIEW`、`DOWNLOAD_ARTIFACT`、`DOWNLOAD_ZIP`、`RECONNECT` 等纯 UI/资源动作，不要把两类动作混成同一个自由枚举。
- `UiActionTarget` 是 UI 投影类型，不得冒充 `contracts/domain.ts` 的 `CommandTarget`。当前领域目标只有 `STEP`、`ITEM`（同时需要 `step_id` 和 `item_id`）、`WORK_UNIT`、`WORK_UNIT_ATTEMPT`、`PROVIDER_RECEIPT`、`EXTERNAL_OPERATION`；UI 的 `WORKFLOW` 目标应只携带 `workflow_id`，普通工作流命令的版本放在 action 的 `expected_state_version`，`ITEM` 映射为 `{ target_type: "ITEM", step_id, item_id }`，`STEP` 映射为 `{ target_type: "STEP", step_id }`，`ATTEMPT` 映射为 `{ target_type: "WORK_UNIT_ATTEMPT", work_unit_attempt_id }`，`ARTIFACT` 则只用于 `artifact_id` 下载票据，不能作为工作流命令 target。`RERUN` 是例外：后端校验的是 workflow group 的 `group_state_version`，必须放在独立的 `expected_group_state_version`，当前兼容投影在修正前不得把它暴露为可执行按钮。无目标时必须为 `null`；诊断/对账所需的 `WORK_UNIT`、`PROVIDER_RECEIPT` 和 `EXTERNAL_OPERATION` 只在对应服务动作中显式出现。
- `RetryScope` 冻结为 `NONE`、`WORKFLOW`、`ITEMS`；`UiIssueSeverity` 冻结为 `INFO`、`WARNING`、`ERROR`、`BLOCKING`。`recovery_action` 使用带 `kind` 的上述动作对象（或 `null`），不要使用无法校验的自由文本；它可以指向同步/导航等 `UI` 动作，也可以指向真正的 `SERVICE` 命令。
- `progress` 的计数必须互斥且覆盖全部条目：`total` 是当前解析 revision 的全部条目（包括 `SKIPPED`）。先按是否存在 `READY + verified` 产物计入 `completed`，并按唯一 `item_id` 计数；复合产物或没有 `item_id` 的 Artifact 不得增加 `completed`。如果条目状态与产物事实冲突，或出现未知状态，必须增加 blocker/invalid 标记，并把该条目暂计入 `pending` 等待重新同步，不能静默当成成功。没有冲突时，`SKIPPED` 计入 `skipped`、`FAILED` 计入 `failed`、`CANCELLED` 计入 `cancelled`，其余 `PENDING/RUNNING/AMBIGUOUS/UNRESOLVED` 以及“状态为 `SUCCEEDED` 但没有已验证产物”的条目计入 `pending`。应满足 `completed + failed + cancelled + skipped + pending = total`；`AMBIGUOUS/UNRESOLVED` 仍需通过条目的 `requires_reconcile` 区分待对账，不能因为被记入 `pending` 就显示成普通等待。`percent=floor(100*(completed+failed+cancelled+skipped)/total)` 是“已处理/已收敛”比例，不是成功或可交付比例；`deliverable_percent=floor(100*completed/total)`，`total=0` 时两者均为 `0`。当前 domain/OpenAPI 的兼容投影仍只有 `failed` 桶时，adapter 也必须从 `items.status` 分离 `CANCELLED`，并在 UI 文案中显示“已取消”，不能把 `percent=100` 写成“全部成功”。
- `items` 至少返回 `item_id`、`item_identity_key`、`sequence`、`content_hash`、`item_type`、`normalized_content`（或可分页详情引用）、`source_locator`、安全的 `metadata`、`status`、`role`、`voice_key`、`attempt_count`、`error_code`、`user_message`、`retry_scope`、`requires_reconcile`、`artifact_ids`、`updated_at`；允许为空的字段没有事实时使用 `null`，`attempt_count` 无尝试时使用 `0`，不得用默认值冒充其他事实。正文可以通过独立详情接口按需加载，但未提供正文/定位数据时不能把内容核对页当作已完成。
- `artifacts` 至少返回现有 Artifact 契约字段，并为 UI 明确 `item_id`、`artifact_type`、`lifecycle_state`、`format`、`size_bytes`、`sha256`、`verified`；`filename`、`duration_ms`、`mime_type` 若纳入 workspace 必须由后端返回，缺失时为 `null`，前端不得从文件名或默认 MP3 推断。
- 任何试听、下载或交付计数都必须同时满足 `lifecycle_state=READY` 和 `verified=true`；`READY` 只能表示存储对象可读，不能单独证明内容已通过格式/完整性校验。复合产物不得伪装成条目级可交付音频。
- `configuration` 必须说明 `configuration_revision`、`configuration_hash`、脱敏后的 `effective` 值、来源优先级和已冻结字段；`configuration_revision` 必须是服务端保存的可生成配置快照版本，并与配置 hash/工作流版本绑定，不得直接复用当前仅表示草稿修订的 `draft_revision`；不得把 provider 凭据或本地路径放进 projection。

当前服务端已经提供 `GET /api/v1/workflows/{workflow_id}/workspace`，`contracts/domain.ts` 与生成契约也已有 `WorkflowWorkspace`、活动候选和配置修订类型；但现有动作枚举还没有独立的 `EXPORT_ZIP`，进度也没有 `cancelled`/`deliverable_percent`，内容核对字段和脱敏的 Provider 状态仍需补入 workspace 或由专门接口承载。因此本节的前端实现路径应改为“workspace 优先、领域接口可降级组合”，并把上述目标扩展同步到 OpenAPI、生成契约和 fixture。正式 fixture、`workflow-adapter.js`、`workflow-reducer.js`、Store workspace 缓存和对应 Node tests 仍需新增；在这些前端载体落地前，G1/G3 不能标记为通过。聚合接口是减少请求的实现基础，不是把未接线状态继续归咎于后端缺字段的理由。

有副作用的 `SERVICE` actions 必须由服务端状态机和能力决定，前端只做展示、权限/上下文过滤和 pending 反馈，不在各个页面复制状态机或猜测按钮是否可用。每个服务动作必须能说明目标类型、所需 workflow/target 版本、是否可安全重试以及被禁用的原因；没有安全范围时不生成“重试此项”动作。纯 `UI` actions 可以由 adapter 根据已确认的资源/导航条件派生，但不得借此绕过服务端状态机。

如果现有 API 尚未返回 `available_actions`，adapter 对有副作用的 `SERVICE` 动作只能显示未知/禁用；静态能力清单最多用于补充已确认的能力标签，不能单独把服务命令变成可点击按钮。纯 `UI`/资源动作可以依据已确认的资源条件派生；不能从某个状态枚举自行推导出 workflow 命令。

P0 至少冻结 `items`、`artifacts`、`blockers`、`available_actions`、`configuration` 的字段结构、枚举、空值语义和分页/上限，并提供“待配置、生成中、待对账、部分成功、已完成”等 §13.0 表列的七组主 fixture；另外必须补充“Provider 未登录/会话过期”“全部取消或部分取消”“无正文投影”“无可交付物”等边界 fixture。每组 fixture 都要能驱动 reducer、页面主动作和错误分支测试；后续增加聚合接口时不得改变这些事实语义。

建议在投影中额外提供 `configuration_revision`、`delivery` 和同步信息，例如：

```text
delivery: {
  zip_artifact_id,
  zip_available,
  included_item_ids,
  excluded_item_ids,
  exclusion_reasons
}
sync: { state_version, last_event_id, requires_resync }
```

这样交付范围和断线恢复不会由 Renderer 根据零散字段猜测。

#### C. 统一错误信封

现有 API 错误响应实际接近：

```text
ApiError = {
  request_id,
  error_code,
  message,
  retryable,
  side_effect_occurred,
  workflow_id,
  step_id,
  attempt_id,
  details
}
```

前端应将它适配为目标 UI 投影，而不是假设后端已经返回全部字段：

```text
UiIssue = {
  code,
  title,
  user_message,
  severity,
  recovery_action,
  retry_scope,
  safe_to_retry,
  requires_reconcile,
  affected_item_ids,
  request_id
}
```

`retry_scope` 应显式表示 `NONE`、`WORKFLOW` 或 `ITEMS`，并在目标化重试时携带目标条目、是否创建新 attempt、是否复用成功产物和未决条目的处理方式。`retryable` 不等于 `safe_to_retry`；只要 `side_effect_occurred=true` 或提交结果不确定，就不能盲目重试，应引导对账/解决。Renderer 默认展示用户文案和下一步动作，技术详情放入诊断抽屉；`request_id` 只用于复制诊断，内部细节需脱敏。当前直接展示 `err.message` 和 HTTP 状态的路径应在 adapter 中统一收口。错误信封中的关联 ID 会由工作流异常边界根据请求路径或异常详情尽量补齐，但在外层鉴权、旧版路由或缺少上下文的异常中仍可能为 `null`；UI 必须允许缺失并优先使用 `details` 和 `request_id` 定位。

当前 `reconcile` 路由更接近记录一次对账意图/审计事件，并不会直接查询供应商或把未决状态变成已解决；真正的 `resolve` 还需要明确的证据和决策。因此 UI 动作应写成“发起/查看对账”和“提交解决证据”，不能把“重新对账”当作自动修复。

首版还需要把错误码与用户动作固定下来，避免每个页面自行解释 `err.message`：

| 错误码/场景 | 用户标题与主文案 | 主动作 | 重试/对账规则 |
| --- | --- | --- | --- |
| `STATE_CONFLICT` | 任务状态已变化，请先同步最新状态 | 重新同步 | 同步完成前不自动重发原命令；同步后重新计算 `available_actions` |
| `CURSOR_EXPIRED`（事件流） | 进度连接已过期，正在重新同步 | 重新同步 | 清除旧 cursor/seq，拉 snapshot/workspace 后重新订阅；不得沿用旧游标 |
| `CURSOR_EXPIRED`（Artifact ticket） | 下载凭证已过期 | 重新获取下载凭证 | 只重新申请 ticket，不重复提交生成；由错误来源区分事件流和下载场景 |
| `SUBMISSION_AMBIGUOUS`、`PERSISTENCE_AMBIGUOUS` | 生成结果待核验，不能确认是否已提交 | 查看/发起对账 | `requires_reconcile=true` 或 `side_effect_occurred=true` 时禁止盲目重试，等待 `resolve` 证据 |
| `PROVIDER_RATE_LIMITED`、无副作用的 `RESOURCE_EXHAUSTED` | 服务暂时繁忙 | 按 `Retry-After` 等待后重试 | 只有 `safe_to_retry=true` 且 `retry_scope` 非 `NONE` 才显示重试 |
| `ARTIFACT_INVALID`、`SOURCE_NOT_AVAILABLE` | 文件或产物不可用 | 查看问题/重新同步 | 不从默认扩展名推断成功；必要时创建新运行 |
| `PERSISTENCE_ERROR`、`MIGRATION_ERROR` | 本地工作区暂时不可用 | 重试连接/查看诊断 | 不重复发送可能已产生副作用的命令，保留 `request_id` |

统一 reducer 规则：`requires_reconcile` 或 `side_effect_occurred` 优先级高于 `retryable`；`retryable=true` 只能说明“理论上可再试”，不能单独打开按钮。`safe_to_retry=true`、明确的 `retry_scope` 和目标/版本齐全时，才允许重试；`STATE_CONFLICT` 和事件流 `CURSOR_EXPIRED` 必须先同步再重新评估。该表应作为 G1 fixture 的预期输出，而不是仅作为 UI 文案参考。

#### D. 条目级状态和失败原因

当前 OpenAPI 的 `WorkItemInfo` 要求条目身份、workflow 身份、类型、顺序、规范化内容、内容 hash 和基础状态；角色、声音、尝试次数、失败原因和动作并非全部保证。交付中心和有限局部重试需要 workspace 投影至少提供：条目身份/顺序/内容摘要、角色、`voice_key`、状态、当前阶段、尝试次数、错误码/用户文案、`retryable`、`requires_reconcile`、关联 artifact ID、最后更新时间以及（有数据时）时长/大小。当前后端已经能执行安全的 `FAILED + item_ids` 目标范围重试，但 UI 仍需要正式的 `retry_scope`、版本和能力投影。`ArtifactInfo` 当前不保证文件名和时长，前端不能从未约定的字段推断它们。

还要处理类型边界：当前 `contracts/domain.ts` 的通用 `ArtifactInfo` 已包含可选的 `item_id`、`step_id`、`work_unit_id` 以及 `format`、`size_bytes`、`sha256` 等字段，但仍比 workspace 投影窄，未声明 `extension`、`mime_type`、`filename`、`duration_ms`、`producer` 等交付/展示元数据。G1 必须明确 workspace 使用的正式类型来源（建议以冻结后的 UI projection 为准），不能让 Renderer 复用通用领域类型后再自行补字段。

同样要保持生成请求的类型与约束一致：OpenAPI、`contracts/generated.ts` 和 `contracts/domain.ts` 目前都已声明可选的 `item_ids`，但上限、唯一性、条目状态/版本校验和适配层传递仍需由 G1 fixture 固化，避免目标范围在适配时丢失或被扩大。

局部重试还需要在 workspace 中返回明确的 `retry_scope` 和执行结果语义：当前安全路径只重跑显式 `item_ids`，保留其他条目的已验证产物；仍需明确是否创建新 attempt，以及失败/未决条目如何进入交付包。仅有一个能把条目状态改回 `PENDING` 的命令不足以支撑“重试此项”，但当前实现已经超出单纯状态重置，文档不应再把它概括为“只能改状态”。

#### E. 产物格式与 ZIP 契约

当前 v1 已有 `POST /api/v1/workflows/{workflow_id}/export-zip` 打包路径：对终态成功/部分成功任务按已验证的 TTS segment 生成或复用确定性 `export-zip` Artifact，Renderer 下载前仍必须检查 `READY + verified=true`。workspace/export 响应已经返回 `included_item_ids`、`excluded_item_ids`、排除原因以及文件名/MIME/大小等字段；其中部分文件名/MIME 是 projection 推导值，时长可能为 `null`，不能直接当作已完成最终格式/时长验收。当前缺口是 Renderer 未消费这些事实，且试听/下载的端到端流式和传输控制仍未完成。若把 ZIP 作为主动作，仍需在 workspace、Renderer 和验收中落实这些语义；不能因为已有生成路径就推断完整批量交付已完成。

同时，v1 引擎已改为使用 provider receipt 的 `output_format`：legacy Xunfei 适配器路径报告 MP3，测试/模拟 Provider 可能报告 `bin`；因此不能把任一默认值直接当作真实编码格式。现有音频校验仍主要记录 hash/size/format，真实字节的可解码性、逐条独立编码和下载链路尚未完成现场验收。最终必须由后端返回真实格式、扩展名、MIME、大小和校验字段，前端不得从字节或旧版默认值猜测。

#### F. 解析取消与重复提交契约

当前没有 `/workflows/{workflow_id}/parse` 的取消路由；已有 `/source-imports/{import_id}/abort` 仅用于源文件上传中止。Renderer 的 `AbortController` 只会使本地等待失效，不能撤销已经进入服务端的解析任务。如果产品保留“取消解析”，必须新增明确的取消/终止语义、状态转移、幂等键和“取消后是否仍可能发布解析结果”的约定，并把源文件上传中止与解析中止分开验收。在此之前，UI 只能提供“停止等待/返回导入”，重新解析前必须先通过服务端状态或幂等结果确认原请求不会继续发布，避免重复创建解析投影。

### 10.2 P0：草稿修订与配置持久化契约

当前 `patchDraft` 虽沿用 Draft 命名，但实现已允许解析后 `ACTIVE` 的安全准备态、`WAITING_RETRY` 和 `WAITING_USER` 保存声音参数、角色覆盖和生成配置；Renderer 也会在生成前调用它，随后再发起生成。生成引擎从服务端配置快照组装计划，而不是只依赖 Renderer 内存字段；但当前 Renderer 仍把 `generation_mode`、`provider`、`account_scope` 作为可选请求覆盖发送，后端会把这些值并入本次接受配置，因此仍存在配置 revision 与请求覆盖字段的双重事实源。当前只有生成命令对明确的 `STATE_CONFLICT` 做一次刷新/重试，`patchDraft` 遇到 `CONFIGURATION_CONFLICT` 仍直接失败，尚未形成统一的合并/重试策略。除此之外，同一修订接口已经支持有限的 `item_overrides`，包括正文、角色/声音、metadata、`PENDING/SKIPPED` 状态和跳过原因；这仍是后端原语，尚未形成 workspace 正文投影、条目级版本冲突和 Renderer 操作闭环。建议在开始页面重构前仍选择一条明确的 workspace revision 路线，因为“已经持久化配置”不等于“前端编辑/跳过修订闭环已完成”：

- 将所有必须配置移动到解析前，确认现有接口能持久化完整配置，并把解析后的内容核对限定为只读；或
- 保留解析后的配置页，并沿用现有的 `PATCH /api/v1/workflows/{workflow_id}/workspace` 工作区修订接口，要求携带 `expected_state_version` 和配置修订号；`MVP-A` 先支持声音/角色分配及生成参数持久化；文本编辑/跳过的前端 UI、条目级版本冲突和完整条目覆盖投影作为后续扩展，并返回完整 workspace。

默认推荐第二条路线：当前流程已经是解析后进入 `ACTIVE`，因此保留解析后的配置页并补充“配置字段优先”的 workspace revision，比把全部配置移动到解析前更贴合现有用户路径。`MVP-A` 不因这条路线自动获得文本编辑/跳过的前端能力；若产品决策选择第一条路线，必须同步修改第 8、12、13 节，不能两条路线并存为隐含实现选项。

配置权威顺序建议固定为：全局默认 < 角色覆盖 < 条目覆盖；`MVP-A` 先落地全局/角色两层，条目层只有在条目修订契约启用后才生效。有效配置由后端保存并返回 `configuration_revision`、`configuration_hash` 和经过供应商能力校验后的 `effective` 配置。生成请求只携带期望的 workflow 版本和配置 revision（或等价的完整配置快照），不能接受 Renderer 内存中的未保存值。当前生成请求仍允许并传递与 revision 并列的 `generation_mode`、`provider`、`account_scope` 等覆盖字段，这是待收口风险；在 revision 路线下服务端应拒绝这些覆盖值，或明确校验它们与已保存快照一致，否则同一个请求仍可能绕过已保存配置，产生“revision 与实际生成参数不一致”的任务。这里的 `configuration_revision` 不等于当前 snapshot 中的 `draft_revision`：前者必须能唯一指向一份已保存、可生成的有效配置快照，后者仍是现有草稿修订事实。首次 TTS attempt 开始后，影响条目身份、声音、文本、范围和输出格式的字段冻结；修改这些字段必须使计划哈希失效并创建新 attempt/新运行。

第二条路线下，配置保存必须允许 `ACTIVE` 阶段确认；仅保存已有草稿字段不够，不能承诺重启后恢复声音配置。

前端完整修订闭环还必须冻结：首次 TTS attempt 后哪些字段冻结、修订如何使计划哈希失效、版本冲突如何提示、改变条目身份时如何生成 `UNRESOLVED` 或新运行，以及如何把现有的 `SKIPPED` 语义展示为可解释的跳过结果并从计划中排除。生成前必须让后端持久化并回显完整配置，且 engine/provider 使用同一份配置；仅写入 Renderer 内存或 `localStorage` 不算保存成功。

### 10.3 P0：workspace 聚合接口的前端接线

服务端已经提供下面的面向 UI 聚合接口：

```text
GET /api/v1/workflows/{workflow_id}/workspace
```

该接口只提供面向 UI 的聚合投影，不替代领域接口。当前 Renderer 已在历史详情、结果/取消收敛和生成前 revision 检查等路径局部调用它，但尚未接入 `workflowStore.hydrate`/`subscribe` 和统一 workspace 渲染链；实施时应把它接入 Store hydrate、历史/活动任务打开、关键 SSE 事件后的 coalesced refresh 和交付页最终校验。若接口短暂不可用，adapter 可降级组合 `getWorkflow/listItems/listArtifacts`，但不得让两个来源同时独立驱动同一页面或用本地统计覆盖 workspace 事实。

### 10.4 P2：体验增强接口

- 解析阶段的可计算进度和当前章节。
- 生成阶段的可靠 ETA 或最近处理速度；没有可靠数据时不展示假 ETA。
- 产物预览/波形元数据，减少播放器首次加载等待。
- 任务级审计摘要，方便用户复制诊断信息而不暴露内部实现细节。

## 11. 前端架构建议

### 11.1 不立即进行框架迁移

当前项目已经是 Electron + 原生 Renderer 结构。本次重构的主要问题是状态、信息架构和视觉层级，不是缺少 React/Vue。建议第一阶段保留现有技术栈，通过清晰的 Store/Adapter/Views 边界完成结构化；如果采用 ES Module，必须同步把 `index.html` 的 classic script 加载改为明确的 `type="module"`/import 入口并固定加载顺序，否则先保持现有脚本方式逐步拆分。等状态与交互稳定后，再评估是否有必要引入框架。

### 11.2 建议的模块边界

```text
  electron/renderer/
    app-shell.js              # Shell、路由、全局命令和布局
    workflow-store.js         # Snapshot、事件游标、命令 pending、连接状态
    workflow-reducer.js       # 五维状态组合、事件/错误归一化和视图迁移
    workflow-adapter.js       # workflow-api + workspace 投影到 UI ViewModel 的映射
  views/
    import-view.js
    review-view.js
    voice-view.js
    generation-view.js
    delivery-view.js
    history-view.js
  components/
    task-rail.js
    task-header.js
    status-banner.js
    item-list.js
    voice-picker.js
    audio-player.js
    issue-drawer.js
    event-drawer.js
  styles/
    tokens.css
    shell.css
    workflow.css
    components.css
```

以上 `app-shell.js`、`workflow-reducer.js`、`workflow-adapter.js`、`views/`、`components/` 和 `styles/` 均为待新增的目标文件/目录；当前工作树只有 `app.js`、`workflow-store.js`、`ui-components.js` 和单一 `styles.css`。这是一种目标边界，不要求一次性按目录全部拆完。迁移时应优先把“状态映射”和“页面渲染”从目前的大文件中抽出，再逐步替换 CSS 旧规则。

### 11.3 状态层职责

目标状态层中，`workflow-store` 负责：

- 当前 workflow、规范化 snapshot、workspace 资源缓存和连接状态。
- SSE 连接、断线重连和快照补偿刷新。
- 命令 pending、幂等保护和错误处理。
- 启动时 hydrate 活动任务，并在多个任务时交给任务选择器。

`workflow-adapter` 负责把五维后端状态派生为用户态，并把 API 错误映射为 `UiIssue`；它不能修改后端状态机，也不能把本地表单值当作已保存配置。当前 Renderer 尚未达到这条边界，迁移期间应把现有局部变量逐步收口。

它不负责：

- 当前选中哪个声音卡片。
- 抽屉是否展开。
- 当前列表的临时筛选文本。

这些属于页面或组件 UI 状态，避免业务状态和视觉状态再次混在一起。

## 12. 实施阶段建议

### 12.0 依赖闸门

实施顺序不能只按页面名排列，必须按事实来源和契约依赖推进：

`产品决策冻结 → OpenAPI/TypeScript 契约与 fixtures → 后端闭环 → adapter/reducer → 旧 UI 接线 → 新 Shell/页面 → 视觉打磨`

| 闸门 | 负责角色 | 必须产出 | 进入下一闸门的条件 |
| --- | --- | --- | --- |
| G0 决策 | 产品 + 前后端代表 | `MVP-A` 范围、术语表、配置权威顺序、暂停/重试/ZIP/恢复承诺 | 第 15 节的阻塞决策已明确，默认路线或覆盖路线已记录 |
| G1 契约 | 前后端 + QA | 五维状态 reducer 输入、`WorkflowWorkspace`、`UiIssue`、`available_actions`（含 `SERVICE/UI` 分类）/`retry_scope` 的 OpenAPI/TS 定义、事件/错误映射矩阵和七组可执行 fixture（§13.0 表列） | 字段、枚举、空值、版本条件、事件刷新规则和错误响应有自动校验 |
| G2 后端闭环 | 后端 + 运行时 | 配置持久化、格式/产物事实、SSE 游标清理/事件流快照重锚定、workspace hydrate 所需的恢复事实、恢复接管或明确的上下文恢复、错误上下文、生成异常的持久化收敛、取消发布前的控制态/版本栅栏，以及源文件流式写入与 Artifact 流式读取/下载的服务端配合（§16.1/16.2） | API/Repository/Engine 的回归测试覆盖 G1 fixture；未支持能力返回明确禁用原因 |
| G3 前端收口 | 前端 | Store 订阅、workspace adapter、命令 pending/冲突/超时处理，以及 §16.1–16.5 的导入/试听流式边界、workspace/Store 接线、命令协调与任务控制入口 | 页面不再从原始 SSE 事件或 `currentStep` 独立推断 workflow 状态 |
| G4 页面迁移 | 前端 + 设计 + QA | 新 Shell、五个工作区、历史/多任务入口和降级布局 | `MVP-A` 主流程在三种窗口尺寸和关键异常 fixture 下可完成 |
| G5 视觉验收 | 前端 + QA + 发布 | token、焦点、ARIA、减少动效、播放器和交付细节 | 13.4/13.5 的可测验收全部通过，且文档承诺与 README/发布说明同步 |

没有通过 G1/G2/G3 时，可以先实现 fixture 驱动的结构验证，但不得接入真实按钮或对外承诺未完成的动作。

与第 16 节的关系（合并实施顺序）：第 16 节的传输与接线条目是 G2/G3 的必须产出，不构成独立于闸门的第二条时间线。合并后的主顺序固定为：

`G0 决策 → G1 契约（含 §16 的传输/workspace/动作契约与 fixture）→ G2 后端闭环 ∥ G3 前端收口（= 本章 P0 状态可靠性 ∪ §16.1–16.7：导入/试听传输边界、workspace/Store 接线、命令协调与任务控制、导入/解析生命周期、结果/媒体收口）→ §16.8 传输指标与低端设备基线 → G4 页面迁移 → G5 视觉验收（含完整指标验收）`

G2/G3 批次内部允许并行推进；§16.1–16.7 与本章 P0 全部完成前，不得把“已完成、可暂停、可继续生成、大文件可用”写入用户承诺或接入真实按钮。§16.8 末尾的顺序说明以本段为准。

### P0：状态可靠性与可恢复性

目标是先让现有功能“可信”，顺序上先过契约闸门，再做页面重构：

- 先实现五维状态组合到用户态的单一 reducer，并覆盖取消中的 `BLOCKED/TERMINATING` 与终态结果的区别。
- 让 Renderer 真正订阅 Store，启动时 hydrate workspace；复用现有服务端启动恢复扫描和安全接管，并补齐活动候选消费、`can_takeover`/`can_resume` 能力和 Renderer 的恢复 UI；未决外部调用和用户主动暂停任务不能自动续跑，若只承诺恢复上下文，则必须返回明确的可续跑能力/禁用原因。
- 同批完成第 16 节的 P0 前端条目（§16.1–16.4：导入/试听流式边界、workspace/Store 接线、命令协调、任务控制入口）；它们是本阶段 P0 的组成部分，不是后续增强，完成前不得把对应能力写入用户承诺。
- 持久化完整任务配置，验证 engine/provider 使用的是用户确认过的配置，而不是 Renderer 内存或默认值。
- 明确内容编辑/跳过的草稿修订、版本冲突、计划失效和 attempt 后冻结规则；若暂不补齐前端投影和条目级修订契约，内容核对先做只读版。
- 明确解析取消与重复解析语义；在没有服务端取消能力前，UI 只能停止等待，不能把本地 `AbortController` 当作服务端取消。
- 将现有 API 错误适配为用户文案、恢复动作和对账提示，保留可复制的请求标识。
- 基于服务端能力投影完成暂停、恢复、取消、重试、对账等命令的 UI 映射；不为尚未形成完整前端闭环的协作式控制提前展示承诺。
- 先修正历史中心的状态投影：活动任务、草稿、阻塞、部分成功和终态必须显示不同的状态/主动作；对持久化 `FAILED` 且没有已验证产物的条目可以保留目标范围重试，但 `AMBIGUOUS`/未决条目必须改为查看问题或对账。
- 保证 SSE 断线、刷新和版本落后时可以补偿，并验证多任务启动时由用户选择而非静默选中。
- 验证取消命令被服务端接受后（无专用“已受理”事件）只显示取消收敛中；收到 `WORKFLOW_CANCELLED` 且终态 snapshot 确认后才显示“已取消”，并移除 Renderer 中永不触发的 `WORKFLOW_CANCEL` 死分支。
- 为已接受的生成任务补齐异常收敛：`GENERATION_TASK_FAILED` 必须伴随 `WAITING_RETRY`、`WAITING_USER` 或终态等可恢复的持久化状态；发布 TTS 产物前必须重新校验控制态和版本，避免取消竞态被写成成功。

这一阶段可以暂时保留旧布局，但必须先解决“用户看到的状态不是真实状态”的问题。

### P1：信息架构与页面重构

- 建立新的 Shell、任务轨道和顶部任务栏。
- 为新旧 Shell 保留明确的 feature flag 或构建级回退点，记录旧任务打开兼容矩阵；迁移失败时能回到旧入口，不以回滚数据库或清理任务数据作为 Renderer 回退手段。
- 实现导入入口和活动任务恢复；只有 P0 的状态投影和恢复契约通过后，才把恢复动作接入真实按钮。
- 增加独立但可跳过的只读内容核对工作区；编辑/跳过等修订能力仅在对应契约完成后开放。
- 重构声音配置为角色编排台。
- 重构生成任务页为任务控制台。
- 重构结果页为交付中心。
- 在 P0 状态投影完成后，将历史页改为任务恢复中心。

### P2：视觉与交互打磨

- 整理并收敛现有 tokens，删除或隔离旧 CSS 叠加规则；当前样式已存在基础 token 和 reduced-motion 规则，不要重复建设一套未接线的 token 系统。
- 完善播放器、试听状态、轻量波形或播放头视觉。
- 统一空状态、加载态、错误态和确认弹层。
- 补齐键盘操作、焦点环、ARIA label 和状态播报。
- 检查 1280×860、1024×768、900×600 三个窗口尺寸。
- 检查 macOS 与 Windows 的窗口边缘、字体、滚动条和系统菜单适配。
- 根据减少动效设置降低非必要动画。

## 13. 验收标准

### 13.0 验收方法与测试夹具

验收不能只看“页面最后能否完成”，必须从可重复的状态和故障输入验证派生结果：

- 为 G1 固定下表所列七组 fixture：待配置、生成中、待对账、部分成功、已完成、取消/无可交付物、Provider 未就绪；每组必须包含完整的 `WorkflowStatus`、`ExecutionState`、`ControlState`、`CleanupState`、`ResultStatus` 五维字段、`available_actions` 和配置/产物摘要。
- reducer 测试逐组断言用户态、所在工作区、主动作、次级动作、禁用原因和视图迁移；同一事实输入不能在不同页面产生不同主动作。
- SSE 410/序号缺口测试必须断言清除 `wordtts.workflow.last-event.<workflow_id>` 及其 `.seq` 键，按“拉取快照/workspace → 重新订阅”的顺序恢复，且不会再次使用旧游标。
- 为 `409 STATE_CONFLICT`、幂等重放、命令超时、配置保存失败、票据过期、产物缺失和播放失败各准备一个输入，验证不会重复提交、丢失用户输入或虚假显示成功。
- `MVP-A` fixture 中不应把尚未接入前端/未纳入首版的编辑/跳过、任意范围或未决条目重试、完整 ZIP 包投影或自动续跑作为可操作主流程；如需验证现有后端 `item_overrides`，应单独标为修订契约 fixture。已有的安全 `FAILED + item_ids` 重试和终态确定性 ZIP 只能在明确标注范围、产物条件和能力投影后打开。至少增加“全部取消/部分取消”“无可交付物”“Provider 未登录/会话过期”和“正文/定位字段缺失”夹具，验证用户不会看到“全部成功”、无条件生成或虚假的原文核对。

七组 fixture 不能只用名称占位。建议作为待新增的 `contracts/fixtures/workflow/*.json` 固定输入，并为每组同时写出预期 projection。`待配置` 应采用当前推荐的解析后路径（`ACTIVE` + 解析步骤完成 + 尚无 TTS attempt），未解析的 `DRAFT` 另作为导入/解析 fixture；不能用一个 `DRAFT` 同时表达“尚未解析”和“已有条目待配置”。

| fixture | 关键事实 | 预期工作区/主动作 |
| --- | --- | --- |
| `待配置` | `ACTIVE` + 解析步骤完成，无 TTS attempt，配置不完整 | 内容核对或声音配置；“保存配置/开始生成”按缺项禁用 |
| `生成中` | `ACTIVE` + `RUNNING`，控制态 `RUNNING`，无未决副作用 | 生成任务；显示当前进度；只展示能力投影允许的取消/暂停 |
| `待对账` | 存在 `AMBIGUOUS` 或 `requires_reconcile=true` | 问题处理/对账；禁止盲目重试 |
| `部分成功` | 终态；部分条目有 `READY + verified`，其余有明确失败原因 | 交付中心；可下载成功条目；只有 `retry_scope=ITEMS` 才显示局部重试 |
| `已完成` | 终态；所有计划条目均有已验证产物，格式事实一致 | 交付中心；试听、单条下载和归档 |
| `取消/无可交付物` | 终态；条目为 `CANCELLED` 或没有任何 `READY + verified` Artifact | 显示已取消/无可交付物；不计为失败或成功，不显示下载/“继续生成”主按钮 |
| `Provider 未就绪` | 生成前 provider 未登录、会话过期或浏览器不可用 | 生成页显示登录/检测/重新认证；禁止提交生成，不自动重发 |

每个 fixture 至少要同时被 reducer、错误适配和页面测试消费，并写出 `activeView`、主动作、次级动作、禁用原因、`progress` 计数和视图迁移结果；fixture 目录、schema、测试入口落地前，G1/G4 不能标记为通过。

首版还必须具备以下 Given/When/Then 级断言：

- Given 历史列表返回一个 `ACTIVE` 任务且暂时没有 Artifact，When 打开历史页，Then 显示“生成中/查看任务”，不能显示“文件缺失”或直接显示“已完成”。
- Given Provider 未登录、会话过期或浏览器被关闭，When 用户点击开始生成，Then 阻止提交并显示登录/检测/重新认证动作；恢复 provider-ready 证据后才能再次提交，不能自动重发原生成请求。
- Given 用户已发送取消命令且服务端已接受（当前没有“取消已受理”事件），snapshot 仍为 `BLOCKED + TERMINATING + IN_PROGRESS`，When 更新页面，Then 显示“正在取消/等待对账”；只有 `execution_state=TERMINAL`、`control_state=TERMINATED` 且 `result_status` 已确定的 snapshot 才显示“已取消/部分完成”。
- Given 收到 `WORKFLOW_CANCELLED` 且终态 snapshot 的 `result_status=PARTIAL_SUCCESS`，When 更新页面，Then 显示“部分完成”并保留已验证产物，不能显示为纯“已取消”。
- Given 收到 `GENERATION_TASK_FAILED` 但 snapshot 仍为 `RUNNING + IN_PROGRESS`，When 更新页面，Then 先显示状态待同步并刷新 workspace，不能直接打开终态失败或重试按钮；最终必须收敛到明确的等待、失败或可恢复状态。
- Given 收到 `TTS_OUTPUT_VERIFIED` 但终态 snapshot 尚未确认，或关联 Artifact 仍是 `READY + verified=false`，When 更新页面，Then 先显示状态/产物待同步，不进入完成交付视图；只有终态快照和每个条目的已验证产物、格式事实都收敛后，才能显示完成、试听或下载。
- Given SSE 返回 410 或 Store 检测到序号缺口，When 执行恢复，Then 保留 `error_code`、清除该 workflow 的 cursor/seq、先拉 snapshot/workspace 再重连，且重连请求不携带旧游标。
- Given 配置保存失败，When 用户点击生成，Then 保留未保存输入、不发送生成命令，并提示重新加载/合并，而不是使用 Renderer 内存值生成。
- Given `retry_scope=NONE` 或仅有状态重置命令，When 用户查看失败项，Then 隐藏或重命名为“沿用设置重新运行（全量）”，请求中不得伪装成条目级重试。
- Given Artifact 为 `READY` 但 `verified=false`，When 进入交付中心，Then 不计入完成数、不开放试听/下载，也不能作为 ZIP 包的可交付依据。
- Given Artifact 元数据、扩展名、MIME 和实际字节不一致，或条目 Artifact 只是复合音频加标记字节，When 进入交付中心，Then 不开放下载并将发布判定置为失败，不降级为 `.bin`。

### 13.1 任务与状态

- 应用重启后能够通过有界且可分页（或等价的活动任务索引）的服务端查询找到未结束任务；不能把 history 接口的第一页当成完整活动任务集。只有活动候选返回 `can_takeover`/`can_resume` 且 workspace 已同步时，才显示“继续生成”，否则明确显示“恢复上下文/等待处理”。
- 多个未结束任务同时存在时，显示任务选择器，不静默打开最近任务。
- 任何非终态都有中文状态、当前阶段和下一步动作。
- 五维状态组合的派生结果与状态机一致；取消收敛前不显示“已取消”。
- 暂停、恢复和取消只在服务端能力投影允许且实际语义可验证时提供；命令发送后不会重复触发。
- 失败能够定位到条目；对持久化 `FAILED`、无已验证产物且携带明确 `item_ids` 的条目提供安全范围内的局部重试；`AMBIGUOUS`/未决副作用仍只能进入对账或解决流程。
- 断线或 SSE 事件丢失后，页面最终能通过快照恢复正确状态；遇到游标过期/HTTP 410 时会清除旧游标并完成重新同步，而不是循环重试旧游标。
- 多个活动任务存在时，任务栏/侧栏可以查看后台状态并安全切换，不会因切换任务而重复提交生成。

### 13.2 配置、修订与结果一致性

- 用户确认的声音、角色覆盖、语速/音调/音量等配置会持久化，并能在服务端生成计划和最终产物中被验证。
- 前端开放编辑/跳过时，有明确的修订号、版本冲突和冻结规则；改变条目身份不会静默复用旧音频。
- 交付中心只展示契约明确的文件名、时长、大小和产物状态，不从未约定字段推断结果。
- 预览范围、生成模式、质量和供应商参数在 UI、持久化配置、生成计划和最终产物之间保持一致；预览不会只改变前端计数。
- 交付中心使用服务端实际生成并验证的 `READY + verified=true` `export-zip` Artifact；当前 workspace/export 响应已经返回已验证 segment 的确定性打包结果、`included_item_ids`、`excluded_item_ids`、排除原因和文件字段，但部分 filename/MIME 是 projection 推导值、duration 可能为 `null`，Renderer 尚未消费这些字段，Artifact 试听和大文件端到端流式仍需补齐后才能作为完整批量交付承诺。
- 音频交付必须同时通过 `READY + verified=true`、实际字节、Artifact `format`/校验元数据、下载文件扩展名和 HTTP `Content-Type` 五项一致性校验；每个条目 Artifact 还必须是独立可解码的实际音频，而不是复合音频加标记字节。任一项不一致都不开放下载，不会把可播放音频交付为 `.bin`/通用二进制。
- 上述校验要明确责任边界：实际字节可解码性、`verified`、`format`/hash 以及是否纳入交付包由服务端在产物发布和下载票据边界校验；HTTP `Content-Type` 由 API/Preload 集成测试断言。当前 `openArtifact` 只返回字节流、没有把响应头暴露给 Renderer，因此若 UI 需要显示或二次校验 MIME，必须由 Preload/API 返回规范化的 `content_type`，不能让 Renderer 仅凭扩展名猜测。

### 13.3 交互与可用性

- 每个工作区存在一个明确的主导下一步动作；取消、返回、帮助和诊断等作为次级动作。
- 用户不需要打开日志才能理解普通错误。
- 生成页面默认展示结论和问题，技术日志按需展开。
- 交付中心能够区分单条试听、单条下载、终态确定性整包下载、失败项处理和归档；只有安全的 `retry_scope=ITEMS` 且目标条件满足时才显示失败项重试，未决条目不进入该动作。
- 历史页的“归档”和“删除”语义与后端行为一致。
- 常见路径不会产生三层以上的嵌套滚动区域。

### 13.4 视觉与可访问性

- 1280×860 为主要设计基准，1024×768 可正常完成任务，900×600 不出现关键动作不可见。
- 状态不只依赖颜色，同时有文字、图标或结构表达。
- 键盘可以完成主流程，焦点位置清晰。
- 播放、暂停、取消、重试等按钮具有明确的 accessible name。
- 章节树/条目列表支持明确的 Tab 或方向键规则；抽屉用 Escape 关闭并把焦点还给触发控件；播放器进度、pending、错误和登录态通过可读的状态文本或 live region 播报。
- 200% 系统缩放和高对比度模式下，主动作、错误详情和焦点指示仍可见且可操作。
- `prefers-reduced-motion` 下不会持续播放必要性之外的动画。
- 浅色工作区和深色侧栏的对比度通过实际测量，而不是只凭视觉判断。

### 13.5 工程质量

- 不再由多个独立变量分别推断同一个 workflow 状态。
- 业务状态映射集中在 adapter/store，而不是散落在各个事件处理分支。
- 新旧视图迁移期间不破坏现有 workflow API 和 Preload 安全边界。
- 每个阶段都有可回滚的提交边界和可执行的回归清单。
- 在格式、ZIP、恢复或重试能力发生变化时，同步更新 README、实施状态索引和发布说明；产品说明不得继续承诺当前未由代码与契约证实的能力。

## 14. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 后端快照字段不足以表达进度/阻塞 | UI 继续猜状态 | 先定义 UI 投影契约，再补快照字段或聚合接口 |
| 只改样式、不改状态层 | 新界面仍然无法恢复和处理异常 | P0 必须优先于大面积视觉重做 |
| 一次性重写 renderer 大文件 | 回归范围不可控 | 按 Store、Shell、视图、组件分阶段迁移 |
| 为了“现代化”强行引入框架 | 增加构建和迁移成本 | 先用模块边界解决问题，框架迁移另行评估 |
| 日志、进度和提示全部常驻 | 信息噪声增加 | 建立三层诊断信息，默认只展示用户决策所需内容 |
| 小窗口下三栏布局拥挤 | 关键动作不可用 | 以抽屉、折叠和单栏降级作为响应式策略 |
| 重试导致重复生成或重复扣费/消耗 | 数据与资源风险 | 后端明确副作用、幂等键和目标范围；前端仅在无未决副作用且能力投影允许时重试 |
| 用户配置只停留在 Renderer | 页面显示与实际音频不一致 | 生成前持久化并回显配置，增加计划/产物一致性验收 |
| 服务重启后只保留数据库记录 | UI 虚假显示“继续生成”或重复提交 | 复用并验证现有安全接管；未决外部调用或用户主动暂停任务只恢复上下文并引导对账/处理 |
| 状态维度被压扁成单一枚举 | 错误显示终态或提供危险动作 | 使用组合状态 reducer，按状态机和能力投影派生用户态 |
| 内容编辑/跳过复用旧 artifact | 交付错误音频或漏项 | 版本化草稿修订，身份变化标记 `UNRESOLVED` 或创建新运行 |
| 目标化重试范围被错误扩大 | 重复生成、覆盖或重复消耗资源 | 仅允许持久化 `FAILED`、无已验证产物且带版本条件的 `item_ids`；后端保持其他成功产物，未决副作用转入对账 |
| ZIP/音频格式契约与 Renderer 不一致 | 下载按钮不可用，或用户得到 `.bin` 文件 | 服务端已返回包含/排除明细和文件元数据，但 Renderer 仍需接线；同时补齐试听/下载流式和真实格式/MIME 校验，完成前继续保持交付闸门 |
| SSE 游标过期后继续使用旧游标 | 页面反复断线，状态长期落后 | 统一 410/缺口重同步流程，清除旧游标并以最新快照重新订阅 |

上表是风险与应对的基线，不是完整的发布登记。G0 前应为每项风险补充负责人、触发信号、关闭证据、残余风险和 go/no-go 条件，并把对应证据链接到 G1–G5 门槛；没有负责人或关闭证据的“已完成”只能视为推断。

## 15. 需要确认的产品决策

以下决策会影响视觉和交互定稿，建议在开始实现前确认：

为了让团队在决策尚未全部回复时仍有可执行路线，若没有明确覆盖，暂按以下默认值推进：

| 决策主题 | 默认路线 | 对 `MVP-A` 的影响 |
| --- | --- | --- |
| 内容核对 | 只读核对；“确认”不写入服务端状态 | 不开放编辑、跳过或持久化确认按钮 |
| 配置来源 | 采用解析后的 workspace revision；有效值按全局 < 角色 < 条目合并 | 生成前必须保存并回显 revision/hash |
| 重启恢复 | 恢复任务上下文；没有活动候选的 `can_takeover`/`can_resume` 证据就不显示自动续跑 | 不重复提交生成 |
| 暂停/取消 | 只显示服务端 `available_actions` 明确允许的动作；未收敛时显示处理中 | 不虚假显示“已暂停/已取消” |
| 目标重试 | 对持久化 `FAILED`、无已验证产物且有明确 `item_ids` 的条目开放安全范围重试；未决条目仍查看问题/对账 | 不把任意失败或状态重置命令包装成“重试此项” |
| ZIP | 后端已有终态确定性 `export-zip` 生成/复用；是否作为 `MVP-A` 主动作仍取决于包含/排除明细、元数据和大文件流式验收 | 可提供受限的已验证条目整包下载；完整批量交付承诺需等契约补齐 |
| 音频格式 | README 仍列多格式，当前 Renderer 固定 MP3、workspace 对缺失格式以 `bin` 兜底；默认先按 MP3-only 规划，但只有端到端验证 `READY + verified=true`、每个条目的独立可解码音频字节、Artifact 元数据、扩展名和 MIME 一致后才开放交付 | 不从旧版默认值或文件扩展名猜测格式，也不把 `bin` 标记直接当作编码格式；多格式支持需另行冻结并同步代码/说明 |
| 多任务 | 可以列出并让用户选择任务，但后台控制和并发语义需另行验收 | 切换任务不改变服务端运行状态 |
| 输入格式 | 当前 parser/Renderer 接受 `.docx`、`.xlsx`，产品主张仍以 Word 为中心；默认首版以 `.docx` 作为承诺范围 | 若保留 `.xlsx`，必须补工作表/行列/合并单元格/角色标记的核对投影和 fixture，否则 UI/README 不得宣称同等支持 |
| 归档与清理 | 默认只提供终态归档；当前“20 条”只是 Renderer 展示上限，不是超限自动删除 | 需冻结保留期、磁盘占用上限、永久删除/清理入口和是否支持取消归档；未冻结前统一使用“归档”文案，不称为“删除” |

下面只保留会改变产品范围或用户承诺的决策；契约字段、事件矩阵、错误优先级和验收闸门已在前文固定，不再重复提问。若团队没有显式覆盖，默认路线直接生效：

1. 是否接受“文档配音工作台”作为本次版本的核心定位，以及“教材排版 × 声音信号”的视觉方向。
2. `MVP-A` 是否采用独立但可跳过的只读内容核对；文本编辑、跳过和持久化确认默认延期到后续版本。
3. 声音/角色分配和生成参数是否按解析后的 workflow revision 持久化；默认采用“全局 < 角色 < 条目”的优先级，但首版只开放全局/角色两层。
4. 首版服务重启默认只恢复任务上下文；是否要承诺自动续跑，取决于服务端安全接管、活动候选能力和可续跑证据，未决外部调用与用户主动暂停任务不自动续跑。
5. 是否允许真实暂停/恢复/取消和局部重试；默认只显示能力投影允许的控制动作，并仅对安全 `FAILED + item_ids` 范围提供局部重试，未决项使用查看问题/对账。
6. 首版是否把已有的终态确定性 ZIP 作为主交付动作；默认可提供仅含已验证条目的受限 ZIP，但 Renderer 完成包含/排除明细、元数据消费以及大文件流式验收前，不扩大为完整批量交付承诺。
7. 首版是否继续支持旧任务打开；默认保留只读查看和受控重跑，不能把旧字段推断为可恢复配置。
8. 首版是否正式支持 `.xlsx`；默认承诺范围为 `.docx`，若保留 `.xlsx` 必须完成独立的结构投影和验收。
9. 历史记录是否需要永久删除；默认只提供终态归档，归档不是删除，也没有取消归档承诺；“最近 20 条”只是展示数量，不能代替保留策略。

在这些问题确认前，本方案中的色值、文案和具体控件尺寸都应视为可调整的设计建议；信息架构、状态单一来源和分阶段顺序则建议作为工程约束保留。

## 16. 实施后前端补充缺口审计（2026-08-29）

本节只记录前文尚未明确到“当前调用点、传输边界、资源生命周期和可复现故障”这一层的前端缺口，不重新定义第 3、9、10 节已经说明的产品方向。审计对象包括 Renderer、Store、API adapter、Preload、Electron 主进程和本地 HTTP proxy；这些边界共同决定前端是否真的能支撑 300MB 导入、试听、恢复和交付。

本轮最重要的结论是：服务端的 workspace、活动任务候选、`available_actions`、配置 revision 和交付包含/排除字段已经存在，Renderer 已在若干关键路径读取 workspace/活动候选并在生成前保存配置 revision，但尚未形成 Store 驱动的统一页面；桌面主导入路径已改为不透明句柄 + 主进程流式上传，浏览器/兼容回退和 Artifact 试听仍有全量字节路径。后续实现不得只把页面换成新卡片，而要先把下面的事实链路接通。

| 优先级 | 新增缺口 | 当前代码事实 | 直接影响 |
| --- | --- | --- | --- |
| P0 | 源文件导入的完整流式/取消体验仍未闭环 | Electron 主路径已使用 `selectFileStream`、不透明句柄、Preload `workflow-source-upload` 和主进程 `requestWorkflowUpload` 流式转发；旧 Preload 的 `selectFileContent`、浏览器 `File.arrayBuffer()` 和 API `toBytes` 仍是整块回退，Renderer 的 AbortController 也未取消在途上传 | 桌面主路径已消除 Renderer 大 Buffer，但浏览器/兼容路径、进度/取消和 300MB 低端设备证据仍未证明 |
| P0 | Artifact 试听仍是全量缓冲 | Electron 主路径通过 `workflow-artifact-open` 申请一次性 ticket，再由主进程 `requestWorkflowStream` 分块经 IPC 传到 Renderer；但 Renderer 的 `readArtifactBytes` 仍收集成完整 `Uint8Array` 后创建 Blob，只有无 `openArtifactStream` 的兼容 fallback 才走普通 `workflow-request`/16MiB `collectResponse` | Electron 试听当前主要受 Renderer 内存/首帧约束；兼容 fallback 仍受 16MiB 响应上限影响，播放开始时间和资源释放仍不可接受 |
| P0 | workspace/action/configuration revision 尚未接入统一 Store 渲染链 | `app.js` 已在历史、结果收敛和生成前调用 `listActiveWorkflows`/`getWorkspace`，生成请求也携带服务端 `configuration_revision`；`workflow-store.js` 虽提供 `subscribe`，但仍只保存受限 scalar projection，`app.js` 没有把订阅接入渲染，也没有 workspace 缓存；`patchWorkspace` 和 `available_actions` 尚未形成统一 UI 来源 | 页面仍可能由 `currentSession`、事件统计和局部缓存分别驱动，动作与 workspace 事实没有单一渲染入口 |
| P0 | 任务控制入口没有完成接线 | 生成页没有工作流级暂停/恢复/取消按钮；`sendCommand('cancel')` 目前只在“新建任务”清理流程中使用，声音试听的暂停图标不是任务控制 | 用户不能独立取消或暂停当前任务，服务端能力投影也无法成为可操作 UI |
| P1 | 命令协调和超时收敛没有独立边界 | 生成、失败项重试、对账、归档、打包等命令散落在 `app.js`；只有生成对 `STATE_CONFLICT` 做局部重试，没有统一 pending、超时后对账和结果确认 | 网络超时后容易出现“请求已生效但页面以为失败”的重复操作风险 |
| P1 | 导入/解析没有同一任务的进度、取消和重试生命周期 | Renderer 的 `AbortController` 只停止本地等待，不传入 API；失败后重置上传 UI，再次选择会新建 workflow/source import | 用户看不到上传字节进度，重试可能留下重复草稿或继续运行的旧解析 |
| P1 | 流式下载缺少可观察的传输控制 | Electron 保存 Artifact 已在主进程流式写入临时文件，但 Renderer 只收到最终成功/失败，没有进度和取消；浏览器 fallback 仍把内容读回内存 | 大 ZIP 或单条音频下载看似“卡住”，无法停止或判断是否仍在写入 |
| P1 | 交付结果的最终收敛尚未完全由统一 workspace 驱动 | `resultFilesFromArtifacts` 已要求 `SUCCEEDED` + `READY` + `verified`，并校验服务端文件名/格式/MIME/大小；但结果页仍以事件/`lastStats` 作为过渡输入，只有关键事件后才异步刷新 workspace，未接入 Store 订阅 | 最终交付闸门已收紧，但中间态仍可能短暂显示旧统计，缺少单一 hydrate/render 顺序 |
| P1 | 试听仍是全量内存路径，失败重试已补齐但缺少真实媒体验收 | `ensureAudioReady` 仍收集完整 Artifact 后创建 Blob；Promise、媒体 error 和波形失败现在会清理旧资源并提供重新获取 Artifact 的路径 | 大音频仍受内存/首帧约束，媒体流、取消、资源释放和真实 Electron 验收尚未证明 |

### 16.1 P0：源文件导入的全链路流式边界与取消体验

当前导入路径分为两类。桌面生产路径是：Electron 文件对话框打开带 `O_NOFOLLOW` 的文件句柄 → IPC 只返回不透明 `sourceFileId` 和大小 → 主进程 `workflow-source-upload` 以固定长度流式转发 → 服务端按块接收并校验 → `workflow-api.writeSourceImport` 返回状态；Renderer 不接触路径或整份源文件。旧 Preload 的兼容路径仍会走 `selectFileContent`/`readFile`，浏览器路径仍是 `File.arrayBuffer()` → `Uint8Array` → `toBytes`；当前正式发布形态是 Electron，因此桌面主路径的流式传输已落地，但回退路径、进度/取消和 300MB 现场证据仍不能宣称完成。

桌面主路径不再经过主进程文件 Buffer、IPC 文件内容副本或 Renderer `Uint8Array`，而是由文件句柄和 HTTP 请求流背压传输；服务端当前会把请求流写入受控临时文件以计算 hash，再转正为 Blob。兼容/浏览器路径仍会产生整块副本，所有路径也还没有把本地 `AbortController` 贯通到文件读取、HTTP 请求和 source import 的中止状态。

按本方案的“安全边界 + 声音信号”方向，建议补成以下边界：

- 原生文件选择只返回安全的文件元数据和一次性 opaque source handle，例如文件名、声明大小、内容类型和句柄 ID；绝对路径、文件内容和可复用路径不能进入 Renderer。句柄必须绑定可信 sender、workflow/source import、generation，并在使用一次或过期后失效。
- 主进程持有文件读取流和 HTTP 写入流，使用固定大小 chunk、背压和可中止的 pipeline；writer ticket/grant、实际字节数和 hash 仍由受控链路校验。Renderer 只接收阶段、已传输字节、总字节、速度和可解释错误，不接触原生路径。
- 浏览器 fallback 使用 `File.stream()` 或等价的流式 Request body；不能因为浏览器分支没有 Electron IPC 就回退到 `arrayBuffer()`。不支持流式 body 的运行环境要明确降级上限，而不是默默宣称 300MB 可用。
- 上传进度至少区分“读取文件、上传源文件、等待写入确认、解析中”；取消时同时关闭文件流、HTTP 请求和服务端 source import。重试必须复用同一 import generation 的幂等结果，或明确创建新的 generation 并清理旧的，不得只清空页面文案。
- 以服务端确认的 `size_bytes`/`sha256` 为事实；声明值和实际值不一致时进入问题状态。前端显示的百分比不能代替服务端 READY/校验状态。

首版验收：

- Given 一个至少 300MB 的真实或稀疏 `.docx/.xlsx` fixture，When 在 Electron 选择并导入，Then 生产路径不调用 `readFile`、`arrayBuffer` 或 `toBytes` 聚合整个源文件，内存峰值由固定 chunk 和明确的并发上限决定。
- Given 上传处于中途，When 用户取消或关闭导入，Then UI 显示“正在停止导入”，文件流/HTTP 请求/服务端 generation 最终收敛，重新导入不会复用已中止的 generation。
- Given 网络在上传或写入确认阶段短暂断开，When 用户点击重试，Then 先读取同一 source import 的服务端状态，已完成的写入不会被重复计入，未决结果不会直接创建第二个 workflow。
- Given 声明大小、实际大小或 hash 不一致，When 进入解析页，Then 禁止开始解析并显示可定位问题；不能把部分上传内容当成可解析源文件。

### 16.2 P0：试听、波形和下载必须拆成不同的资源路径

当前 Electron 主路径由 Preload 的 `openArtifactStream` 调用主进程 `workflow-artifact-open`：主进程先申请一次性 ticket，再用 `requestWorkflowStream` 读取内容并经 IPC 分块发送给 Renderer；Renderer 的 `readArtifactBytes` 随后仍收集全部 chunk，创建完整 Blob URL 给 `Audio`，WaveSurfer 再对同一份 Blob 做解码。只有缺少 `openArtifactStream` 的兼容 fallback 才会由 `openArtifact` 通过普通 `workflow-request` 读取内容，并受主进程 `collectResponse` 的 16MiB 上限约束。结果页前两条会优先调用 `ensureAudioReady`，因此“延迟加载”仍然是延迟全量下载，不是流式播放。

现有 Electron `saveArtifactStream` 只证明“保存到文件”路径可以流式写入，不能证明 `<audio>` 试听路径已经流式；浏览器 fallback 的 ZIP/单条下载也仍然调用 `readArtifactBytes`。因此必须把三类资源分开：

- 播放路径：使用 ticket 绑定的受控媒体流（按 Electron 能力选择 `MediaSource`、受控本地协议或等价的 stream-backed media source），让原生 Audio 能在未下载完整文件前开始播放。不得把长期 token、原始后端 URL 或 query token 暴露给 Renderer。
- 波形路径：优先读取服务端提供的 duration/peaks/摘要元数据；没有元数据时，只允许明确的有界降级，不得为画一条波形而把整段音频复制到内存。切换条目、离开结果页和筛选重建时要中止未完成的媒体/波形读取。
- 下载路径：沿用主进程临时文件 + 流式写入的安全边界，并增加已接收字节、总大小、取消和失败清理反馈；浏览器不能支持同等能力时要显示降级上限。

票据响应还要通过 Preload 返回规范化的 `content_type`、`content_length`、`sha256` 和 `filename`（若服务端已确认），供媒体和交付层校验；这些字段不能由 Renderer 只凭扩展名猜测。一次性的 Artifact ticket、响应头和流的生命周期要绑定同一个请求，过期或失败后允许重新申请新 ticket。

首版验收：

- Given 一个明显大于 16MiB 的音频 Artifact，When 用户第一次点击试听，Then 不经过 `requestWorkflow`/`collectResponse` 的全量路径，不构造 `Uint8Array(total)` 或完整 Blob，能在达到可播放阈值后开始播放。
- Given 用户切换到另一个条目或离开交付中心，When 旧音频仍在读取，Then 旧流/解码/波形任务被中止，最多保留约定数量的活动媒体资源，不继续后台占用内存。
- Given Artifact ticket 过期或网络短暂失败，When 用户再次点击试听，Then 重新申请 ticket 并重建媒体源；错误不会永久污染该条目的播放状态。
- Given 用户下载大 ZIP，When 传输中途取消或失败，Then 主进程关闭响应、删除 `.part` 临时文件，Renderer 显示可重试状态，目标目录不留下伪成功文件。

### 16.3 P0：把 workspace、动作和配置修订接到同一个 Store 事实源

当前 API adapter 已提供 `getWorkspace`、`listActiveWorkflows` 和 `patchWorkspace`，`contracts/domain.ts`/生成契约也已经定义 `WorkflowWorkspace`、活动候选和 `configuration_revision`。`app.js` 已在历史、生成前配置持久化、结果/取消收敛等路径使用部分 workspace/active 接口，生成请求也会带服务端 revision；但 `workflow-store.js` 只保存受限的 scalar workflow projection，没有 `workflowStore.subscribe` 渲染路径，启动、历史和关键事件仍由多个局部请求分别更新 `currentSession`、`generatedFiles` 和页面。后端契约因此尚未转化为“页面由 workspace 驱动”。

前端收口必须明确以下调用规则：

- Store 增加按 workflow 管理的 `workspace`、hydrate 状态、请求 token、同步原因和资源错误；提供 `hydrate(workflowId)`、`reconcile(workflowId, reason)` 或等价边界。workspace 读取失败时保留旧事实并标记同步错误，不能用空数组覆盖成“没有文件”。
- 启动和历史入口先调用 `listActiveWorkflows` 作为候选索引，再按候选拉取 snapshot/workspace；普通历史列表只负责历史展示。候选的 `can_resume`、`can_takeover`、`resume_reason` 和 `requires_reconcile` 必须成为按钮和文案的输入。
- SSE 事件只触发一次合并刷新，不能由事件分支分别修改 `currentSession`、`generatedFiles`、进度和结果页。关键事件后的顺序固定为“接受事件 → hydrate snapshot/workspace → reducer 派生用户态 → 视图渲染”。
- 配置页保存使用 `PATCH /workspace`（兼容旧接口时也必须走同一 adapter），携带 `expected_state_version` 和当前 `configuration_revision`；服务端回传的新 revision/hash 必须写入 Store。生成命令携带同一 revision，不能继续只发送 Renderer 内存中的 `generation_mode`、`provider`、`account_scope` 覆盖值。
- 结果、历史、失败项重试和交付范围都从 workspace 的 `items`、`artifacts`、`delivery`、`blockers` 和 `available_actions` 派生；`currentStep`、`lastStats`、`generatedFiles` 只能是过渡缓存或纯 UI 状态，不能再决定业务成功。

首版验收：

- Given 应用重启后存在一个活动候选，When 启动完成，Then 先 hydrate 该候选的 workspace，再决定进入生成、问题处理、内容核对或交付视图；不能因没有本地 `currentSession` 就创建新任务。
- Given 配置保存返回新的 revision，When 随后点击生成，Then 请求带该 revision；若服务端返回配置冲突，保留用户未保存输入、刷新 workspace 并要求合并，不能用旧 `lastGenerationConfig` 发起生成。
- Given 收到 `TTS_OUTPUT_VERIFIED`、`GENERATION_TASK_FAILED` 或 `WORKFLOW_CANCELLED`，When workspace 尚未收敛到终态，Then 页面停留在同步/处理中状态；只有 workspace 的终态、条目和 Artifact 条件全部满足时才进入交付中心。

### 16.4 P0：任务控制必须有独立的可见入口

当前生成页 HTML 没有工作流级暂停、恢复或取消控件；Renderer 中唯一的 `sendCommand('cancel')` 位于重置/新建任务的清理流程。工具栏的“新建任务”会先提示并尝试中止当前任务，它不能替代用户想要的“只取消当前生成、保留任务上下文”；声音卡片中的 pause 图标也只控制试听。

实现时应在生成任务页提供由 `available_actions` 派生的任务控制组：

- `PAUSE`、`RESUME`、`CANCEL` 只能来自 `kind=SERVICE` 的能力投影；未提供或被禁用时不显示虚假的可点击按钮，可以显示“当前调用完成后才能操作”等解释。
- 点击后立刻进入 action-specific pending：暂停显示“正在请求暂停”，恢复显示“正在恢复”，取消显示“正在停止/等待清理”；直到新的 workspace 确认才改变控制态，不能按点击事件直接写成 `PAUSED` 或“已取消”。
- “新建任务”仍是生命周期操作，需要单独确认会清理/保留什么；它不能通过隐式 cancel 作为生成页唯一的停止入口。取消失败或超时要回到 workspace 对账，不要让页面恢复成可重复提交的“生成中”。
- 任务栏、历史页和交付页切换任务时只切换订阅与视图，不隐式发送暂停、恢复、取消或生成命令。

首版验收：

- Given workspace 返回 `PAUSE`、`RESUME` 或 `CANCEL`，When 用户进入生成页，Then 显示对应动作、目标和当前 pending 状态；同一动作在 pending 期间不可重复发送。
- Given workspace 不支持暂停，When 任务运行中，Then 不出现“暂停生成”按钮；如果支持取消，则取消仍可独立使用。
- Given 用户点击取消且命令被服务端接受，但尚未收到 `WORKFLOW_CANCELLED` 或终态 workspace，When 更新页面，Then 仍显示取消收敛中；只有 `execution_state=TERMINAL`、`control_state=TERMINATED` 且 `result_status` 与条目/Artifact 事实一致时，才显示已取消/部分完成。

### 16.5 P1：增加命令协调层，统一冲突、超时和幂等结果

当前命令调用散落在 `startProcessing`、失败项重试、对账解决、归档、ZIP 生成和重置清理等分支。只有生成命令在 `STATE_CONFLICT` 时局部重新读取一次；下载、归档、重试和取消没有统一的“请求已发出但结果未知”模型。建议在现有 `workflow-store`/`workflow-adapter` 边界中增加独立的 `workflow-command-coordinator.js`，或实现等价的独立模块，不要求引入框架。

每个有副作用命令统一经过：

`读取最新 workspace → 校验 action target/版本 → 生成并固定一次幂等键 → 发送命令 → pending → 以响应或事件 hydrate workspace → 超时则标记结果待确认 → 只在 workspace 明确安全时重试`

当前 `workflow-api.mutate` 每次调用都会生成新的幂等键，生成命令的冲突重试也会再次调用该路径；这还不满足“一次逻辑命令固定一个幂等键”。协调层要记录命令类型、workflow/target ID、期望版本、幂等键、开始时间和可脱敏的 request ID；不能保存能力 token、原始路径或完整错误响应。`409`、超时、断线和重复响应都必须回到同一个收敛函数，不由每个按钮自行决定是否再发一次。

验收至少包括：命令请求已到达服务端但 Renderer 超时、响应已成功但 SSE 断开、版本在发送前变化、用户连续点击两次、以及重连后收到幂等重放响应。所有场景最终都只能有一个确定的 workflow 状态和一个可解释的按钮状态。

### 16.6 P1：补齐导入/解析的任务生命周期和用户反馈

`processSourceContent` 会先创建 workflow，再创建 source import、写入内容、读取 READY 状态和发起解析；桌面文件句柄路径的写入已经由主进程流式完成，但字节进度、服务端阶段和同一任务的失败恢复仍未形成 UI 生命周期。当前本地 `AbortController` 没有进入 API 请求，不能取消已经发出的写入或解析；失败后页面仍会清掉上传区，用户再次选择文件时重新创建 workflow/source import。

在流式传输完成后，仍需要单独补以下 UI 生命周期：

- 上传和解析拆成可观察阶段，显示已传输/总字节、服务端写入确认、解析条目数和当前阶段；未知总大小时使用不确定进度，不显示伪百分比。
- “停止导入”只承诺停止等待还是会终止服务端处理，必须由 source import/parse 契约决定；如果只能停止等待，页面要保留 workflow/import ID 并先对账，不能让用户误以为服务端已取消。
- 失败重试先读取同一 workflow/import generation 的状态，区分可安全重试、结果待确认和必须新建任务；保留文件名、大小、hash 和错误 request ID，不能只恢复默认上传占位文案。

### 16.7 P1：结果页改为 Artifact/条目权威投影，并修复试听重试 bug

当前 `resultFilesFromArtifacts` 已同时要求关联 `WorkItem.status=SUCCEEDED`、`READY + verified`、服务端文件名/格式/MIME/大小一致，且不会在最新 Artifact 无效时回退旧产物；其 artifact 类型白名单仍包含 `tts-segment` 和 `tts-output`，但 workspace/ZIP 的条目级交付路径只认独立的 `tts-segment`，因此 `tts-output` 只有在契约明确它具备独立 `item_id` 语义时才应进入交付。`handleDone` 仍会先使用事件/`lastStats` 和 Renderer 内存中的 `generatedFiles` 作为过渡输入，关键核验事件后才异步刷新 workspace/Artifact。ZIP 卡片也已改为要求 delivery 明确提供已生成的 Artifact；最终收敛仍未统一到 Store。

这些逻辑必须改为：

- 只接收 workspace 中状态为 `SUCCEEDED`、关联独立 `tts-segment` Artifact 为 `READY + verified=true`、字节/格式/MIME/扩展名校验通过的条目；`SKIPPED`、`FAILED`、`AMBIGUOUS`、`UNRESOLVED` 和 `tts-output`/`tts-composite` 等未明确独立条目语义的父产物不能进入单条交付列表。
- 使用服务端返回的 `filename`、`duration_ms`、`size_bytes`、`mime_type`、`format` 和 `artifact_id`；字段缺失就显示“元数据待同步/不可交付”，不能用数组序号或 MP3 默认值掩盖缺失。
- 成功数、失败数、跳过数、待处理数和 ZIP 的 included/excluded 明细全部来自 workspace；只有 delivery 明确 `zip_available` 且 ZIP Artifact 自身满足交付闸门时显示可下载主动作。若下载按钮会触发 `export-zip` 服务动作，先显示“准备交付”而不是把它伪装成已有文件。
- `audioReadyPromise`、媒体流、波形加载和票据在失败/取消时必须清理；失败后下一次播放要重新申请 ticket。波形的 `_waveformFailed` 不能成为无重试入口的永久状态，应提供条目级“重试试听/重试波形”或在可见时自动以退避方式重新加载。

新增回归断言：一次短暂的 401/网络错误后再次点击试听可以成功；一个 `SUCCEEDED` 条目只有 `READY` 但 `verified=false` 时不计入完成数；一个 `FAILED` 条目存在 READY Artifact 时仍不进入交付；服务端文件名不是 `001.mp3` 时页面和下载都使用服务端名字；ZIP 的排除原因能在交付页逐条查看。

### 16.8 P2：低端设备和传输体验的验收不能只看后端测试

后端已有流式存储和部分流式保存能力，但当前前端仍没有真实的内存、首帧播放、上传/下载速度、取消延迟和长列表资源指标。完成本方案前，应增加独立的 Renderer/Preload 集成夹具和三档设备预算：普通桌面、4GB 内存低端设备、慢速磁盘/网络环境。当前 Node 单元测试和契约检查不能替代真实 Electron 窗口、媒体资源和 IPC 验收；如果暂时没有 Electron/Playwright harness，必须将下面场景登记为带截图/日志/版本信息的手工验收项，不能把它们标成自动通过。

至少记录以下指标，并在文档和发布说明中区分“测得”与“推断”。G0 还必须为每项冻结设备、样本、采集方式、通过阈值和超限处理；在阈值未冻结前，下面只是测量清单，不是发布通过标准：

- 300MB 源文件导入的 Renderer/主进程峰值内存、上传吞吐、取消收敛时间和重试后的重复字节数。
- 大于 16MiB Artifact 的首次可播放时间、试听峰值内存、切换条目后的资源释放时间和并发媒体/波形数量。
- 大 ZIP 的首个进度反馈时间、写入速度、取消到临时文件删除的时间，以及应用重启后是否残留 `.part` 文件。
- `1280×860`、`1024×768`、`900×600` 下控制组、同步提示、错误恢复和交付范围明细的可见性；这项验收必须在真实 Electron 窗口中完成，不以静态 HTML 截图代替。

本节条目归入 §12.0 的合并实施顺序，不构成第二条时间线：传输边界（§16.1/16.2）与 workspace/Store 接线（§16.3）在 G2/G3 批次内可并行，随后是命令与任务控制（§16.4/16.5）、导入/解析生命周期与结果/媒体收口（§16.6/16.7），再进入本节传输指标与低端设备验收，最后是新 Shell/视觉迁移（G4/G5）。

在前四项完成前，不能把“已完成”“可暂停”“可继续生成”“大文件可用”写入用户承诺；其中“可继续生成”只适用于有 `can_takeover`/`can_resume` 证据的任务，不能泛化为所有重启任务。视觉重构可以并行做 fixture 和静态结构，但真实按钮必须等对应能力投影和回归证据齐全。

## 17. 参考依据

本方案基于当前仓库中的产品说明、Renderer UI、渲染逻辑、工作流 API 和领域契约整理：

- `README.md`：产品能力、平台和用户流程。
- `electron/renderer/index.html`：当前 Shell、四步流程、配置、生成、结果和历史结构。
- `electron/renderer/app.js`：当前页面导航、上传/解析、SSE 事件处理、历史操作和错误展示逻辑。
- `electron/renderer/styles.css`：当前主题、卡片体系、声音浏览器布局、生成页面重建和响应式规则。
- `electron/workflow-api.js`：工作流快照、条目、产物、命令、SSE、重试、对账和重跑能力。
- `contracts/domain.ts`：`WorkflowStatus`、`ExecutionState`、`ControlState`、`CleanupState`、结果状态和快照模型。
- `contracts/generated.ts`：由 OpenAPI 生成的 TypeScript 契约，作为前端类型和 fixture 校验的同步结果。
- `contracts/openapi.yaml`：现有工作流、源文件、解析、生成、控制、事件和产物接口。
- `workflow/state_machine.py`：执行态、控制态和命令转移规则。
- `workflow/repositories.py`：草稿修订限制、解析后生命周期、历史归档和条目/产物事实。
- `workflow/engine.py`、`workflow/providers.py`：生成计划、控制检查和供应商参数传递。
- `workflow/recovery.py`、`api/workflow_routes.py`：恢复能力、启动接线现状、错误响应和 SSE 行为。
- `workflow/artifact_store.py`、`workflow/audio.py`：产物存储、格式标记和音频处理事实。
- `electron/preload.js`、`electron/main.js`、`electron/file-dialogs.js`、`electron/workflow-proxy.js`：Renderer 与主进程之间的文件选择、Artifact 保存、HTTP 缓冲/流式边界和安全 IPC 面。
- `docs/workflow-spec.md`、`docs/implementation-plan.md`：工作流语义与实现状态的补充说明；`implementation-plan.md` 的完成标记只作里程碑记录，不替代代码、契约和测试证据；若与代码冲突，以当前代码和契约为准。
- `electron/renderer/workflow-store.js`：Renderer 侧快照消费、游标持久化和缺口处理现状。
- `electron/test/workflow-api.test.js`、`electron/test/workflow-store.test.js`、`electron/test/renderer-config.test.js`：现有传输、游标和 Renderer 配置回归证据；尚未覆盖本节新增的真实大文件流式、workspace 接线、控制按钮和媒体流/取消集成场景；媒体错误重试的代码边界已有静态回归。

当前仓库未发现已定稿的 `PRODUCT.md` 或 `DESIGN.md`。因此本文件中的产品主张与视觉方向是基于现有代码和 README 的方案建议，不能视作已经批准的品牌/产品设计规范。工作流 API 的部分领域原语已经存在，且配置传递、启动安全恢复、无未决外部副作用任务的有限安全接管、SSE 游标清理/事件流快照重锚定、`READY + verified` 交付闸门、试听范围、终态 rerun、失败条目安全范围重试、确定性 ZIP、活动任务候选、workspace/action/configuration revision 投影和受运行时/真实 Provider 开关控制的安全 retry dispatch 已在当前工作树形成有限闭环；Renderer 尚未完成 Snapshot/workspace/action 接线，workspace hydrate、编辑/跳过的前端修订闭环、未决外部调用重发、用户暂停任务自动续跑、暂停/恢复控制入口、导入/试听/下载的大文件端到端流式、传输进度/取消和真实外部验收仍有实现缺口；媒体错误后的资源/Promise 重置代码已补齐，但真实媒体流、首帧、取消和大文件集成证据仍未完成。待本方案确认并完成第 16 节的前端接线与回归证据后，再决定是否把设计契约沉淀为项目级设计文档。
