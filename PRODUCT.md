# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

主要用户是需要把教学 Word 文档制作成课堂试听或交付音频的教师、教研人员和内容制作人员。他们通常在本机处理一份文档，按文档结构检查条目、配置角色声音，然后等待批量生成并逐条核验结果。

> 以下用户、场景和目标来自 `docs/frontend-ui-redesign-plan.md` 的明确产品事实；本次会话没有结构化问答入口，因此未额外确认的内容均按方案默认路线执行。

## Product Purpose

小猪wordTTS 是一款本地桌面工作台，把教学文档转换为可试听、可交付的音频。成功意味着用户能清楚知道当前任务、文档解析出的条目、声音配置、生成进度和可交付产物，并能在异常或重启后安全地恢复上下文，而不会因为界面猜测状态而重复提交任务。

## Positioning

产品以可恢复的文档配音任务为中心，把文档结构核对、角色声音编排、生成过程观察和逐条交付放在同一个工作台中；它不是提交一次表单后只能等待的播放器或后台列表。

## Operating Context

- 运行在 Electron 本地应用中，Renderer 使用原生 HTML、CSS 和 JavaScript，Preload 保持安全边界。
- 用户从本机选择或拖入教学文档；首版承诺范围为 `.docx` 与 `.xlsx`，其中 Excel 词汇题目是明确支持的导入场景。
- 音频生成依赖工作流 API、SSE 事件流和讯飞供应商登录/会话状态。
- workflow 是可持久化、可恢复的用户工作单元；条目拥有独立状态和产物事实。
- 默认按解析后的 workspace configuration revision 持久化全局/角色配置；对安全的 `PENDING/SKIPPED` 条目支持受版本和首次执行冻结保护的有限正文编辑、跳过与恢复，条目级声音覆盖仍不作为首版可操作能力。

## Capabilities and Constraints

- 工作区包括导入、内容核对、声音配置、生成任务和交付中心；历史任务作为跨任务入口保留在侧栏。
- 内容核对支持受安全边界约束的正文编辑、跳过与恢复；不伪造服务端“已确认”语义，发现无法安全修订的问题时返回导入重新处理。
- 生成参数包括角色声音、语速、语调、音量、质量、生成模式和预览范围；生成前需要保存并回显服务端配置 revision。
- workflow 状态由生命周期、执行、控制、清理和结果五个维度组合派生；UI 不应仅凭 `currentStep`、单个事件或本地计数判断终态。
- 暂停、恢复、取消、重试、对账和 ZIP 交付只在服务端 `available_actions` 和产物闸门明确允许时开放。未决外部副作用不得自动重试。
- 只有带 `item_id`、`artifact_type=tts-segment` 且同时满足 `READY + verified=true` 和格式事实一致的产物才能进入音频交付列表；`source`/`parse-output` 不得伪装成音频。
- 默认按 MP3-only 规划，但不会从文件扩展名或默认值推断真实格式；真实字节、Artifact 元数据、扩展名和 MIME 一致后才开放下载。
- 重启默认恢复任务上下文；只有 `can_takeover`/`can_resume` 证据和 workspace 同步完成时才展示继续生成。
- 当前首版不承诺自动接管未决外部调用、用户主动暂停任务自动续跑、任意范围重试或完整批量 ZIP 交付；编辑/跳过仅限服务端契约允许的安全条目范围。

## Brand Commitments

- 产品名为“小猪wordTTS”，保留现有小猪品牌图标，不重绘或替换品牌资产。
- 产品语气应清楚、友好、可靠；核心任务界面保持轻微亲和感，但不幼儿化或装饰化。
- 视觉方向采用“教材排版 × 声音信号”的轻盈蓝紫工作台：文档结构负责秩序，声音信号只出现在声音、生成和交付场景。

## Evidence on Hand

- 产品与交互事实：[docs/frontend-ui-redesign-plan.md](docs/frontend-ui-redesign-plan.md)
- 当前 Renderer：[electron/renderer/index.html](electron/renderer/index.html)、[electron/renderer/app.js](electron/renderer/app.js)、[electron/renderer/workflow-store.js](electron/renderer/workflow-store.js)
- 现有品牌图标：[electron/renderer/assets/app-icon.png](electron/renderer/assets/app-icon.png)
- 工作流契约：[contracts/openapi.yaml](contracts/openapi.yaml)、[contracts/domain.ts](contracts/domain.ts)
- 没有额外的用户研究、品牌手册、客户评价或可公开宣传的性能/业务数据；后续界面不得虚构这些证据。

## Product Principles

1. 先让用户知道任务事实，再展示技术细节。
2. 每个工作区都给出一个清晰且安全的下一步。
3. 事件是更新信号，快照、workspace 和能力投影才是操作依据。
4. 失败、取消、待对账和不可交付必须与成功明确区分。
5. 本地数据和用户配置应可恢复、可解释，不因主题或界面切换改变 workflow。

## Accessibility & Inclusion

- 键盘应能完成主流程，焦点环清晰，抽屉可用 Escape 关闭并恢复焦点。
- 状态不能只依赖颜色，必须同时提供文字、图标或结构表达；错误、pending、播放和 Provider 状态需要 live region 或等价的可读反馈。
- 目标窗口包括 1280×860、1024×768 和 900×600；支持窄窗口、200% 缩放、较少动效和高对比度使用。
