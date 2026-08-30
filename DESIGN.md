<!-- impeccable:design-schema 1 -->

# 小猪wordTTS · 文档配音工作台设计契约

## Design mode

这是一个以任务完成为目标的 Operate 界面。视觉系统服务于导入、核对、配置、生成和交付五个连续动作；状态事实、恢复路径和可访问性优先于装饰。

## Direction contract

- **Thesis**：文档配音工作台是一张轻盈、可恢复的任务桌面，而不是一次性表单。
- **Own world**：教材排版的清晰层级与声音信号的节奏共同构成明亮纯蓝工作台。文档区域使用纸面、细线和留白；声音、生成与交付区域才使用波形、节奏和状态色。
- **Story**：导入源文档 → 核对内容 → 配置声音 → 生成任务 → 交付音频。
- **First viewport**：左侧流程轨道、顶部任务上下文、中央单一主动作，以及同一视线内的状态证据。
- **Form**：白色纸面与浅蓝画布、清透明亮的纯蓝主色、薄边界、适度圆角和克制阴影；不用渐变文字、装饰性光晕或把卡片套进卡片来制造层次。
- **Signature interaction**：每个服务端允许的动作都从 workspace 投影进入统一命令协调层，带 pending、状态冲突重试和超时后的只读对账；页面不凭本地猜测宣称任务已暂停、已完成或可交付。

## Tokens

| Role | Light | Dark | Use |
| --- | --- | --- | --- |
| Canvas | `#F4F9FF` | `#0B1F38` | 页面背景 |
| Surface | `#FFFFFF` | `#102B49` | 纸面、面板、输入区 |
| Ink | `#24445F` | `#DCEEFF` | 标题、正文、关键数字 |
| Muted ink | `#55748F` | `#A8C4DE` | 辅助说明、元数据 |
| Line | `#D7E7F7` | `#2A4B6C` | 分隔线、边界、焦点外圈 |
| Primary | `#1A73E8` | `#57A3FF` | 主动作、当前步骤、可交付强调 |
| Primary soft | `#E7F0FF` | `#123B68` | 选中面、提示面、轻量状态 |
| Sky | `#C4E5FF` | `#2B6591` | 导入与文档信号 |
| Blush | `#FFB9CA` | `#7C425B` | 需要处理的非破坏性提示 |
| Mint | `#AEE8D0` | `#2D6A59` | 已核验、成功、可交付 |
| Attention | `#F2CB79` | `#8D6B2F` | 暂停、等待、待对账 |
| Danger | `#D86A78` | `#8B394B` | 失败与不可交付 |

## Typography and rhythm

- 中文正文使用系统无衬线字体栈，保持跨平台可读；标题使用更大字号、较重字重和短行宽形成层级。
- 正文和帮助文字保持舒适行高；功能性文字不低于 11px，正文不低于 14px。
- 间距采用 4px 基准，但按关系分成紧凑组、面板内边距和区域间距，不把同一数值铺满所有组件。
- 数字、进度和文件信息使用等宽数字或 `font-variant-numeric: tabular-nums`，便于扫读变化。

## Component grammar

- Shell：左侧流程轨道 + 顶部任务上下文 + 中央滚动工作面 + 底部状态栏。
- Surfaces：白色面板用 1px 细线和层级留白区分，不依赖厚重投影；只有真正需要浮起的弹层保留柔和阴影。
- Controls：主按钮只承担当前阶段的主动作；次按钮使用幽灵样式；原生 `select` 保留系统箭头、键盘行为和焦点反馈。
- Status：状态同时由文字、结构、图标和颜色表达。`PENDING`、`BLOCKED`、`WAITING_RETRY`、`SUCCEEDED`、`PARTIAL_SUCCESS`、`CANCELLED` 和 `FAILED` 不共用成功样式。
- Audio：波形与播放控件只在生成和交付区域出现；播放失败必须清除旧媒体资源并允许重新申请 Artifact。

## Product/data guardrails

- 首版正式支持 `.docx` 与 `.xlsx`。Excel 词汇模板按“单词/例句”解析；核对条目保留 `工作表/…/行/…` 来源定位。未识别的 Excel 结构不能被 Renderer 默认为可生成内容。
- 只有 `item_id`、`artifact_type=tts-segment`、`READY`、`verified=true` 且格式事实一致的产物进入音频交付列表；源文档和解析产物不伪装成音频。
- 进度拆分为已完成、失败、已取消、已跳过、待处理和可交付；可交付数由服务端/Artifact 事实驱动。
- 文档、主题和历史查看不会改变 workflow；控制动作只由服务端 `available_actions` 投影授权。

## Responsive and accessibility contract

- 目标尺寸：`1280×860`、`1024×768`、`900×600`；窄屏将双栏收为单栏，声音列表和结果指标允许自然换行，不以横向滚动隐藏主动作。
- 所有流程节点和关键操作可用键盘访问；焦点环清晰，弹层用 Escape 关闭并恢复焦点。
- 状态使用 `aria-live` 或 `role=status` 提供文本反馈；拖拽导入同时保留可点击、键盘触发和文件格式提示。
- 颜色不是唯一状态信号；支持深色模式和 `prefers-reduced-motion`，减少动效时内容仍默认可见。

## Motion

动效只用于状态变化、进度和媒体反馈，采用短促的 ease-out；不使用弹跳、循环装饰性脉冲、渐变光晕或会造成布局抖动的宽高过渡。大文件传输使用流式路径，浏览器兼容回退有明确大小上限并在 UI 中诚实说明。

## Asset provenance

- 保留既有品牌图标：[electron/renderer/assets/app-icon.png](electron/renderer/assets/app-icon.png)，品牌资产本身不重绘；新增插画素材只用于工作台背景与状态空面。
- 新增导入工作台背景：[electron/renderer/assets/pig-document-desk-background.png](electron/renderer/assets/pig-document-desk-background.png)，使用内置 `image_gen` 生成的手绘小猪、文档与声音波形插画；中心保留低干扰留白供 HTML 操作文案使用。
- 新增跨页面小猪素材：[electron/renderer/assets/pig-mascot-sticker.png](electron/renderer/assets/pig-mascot-sticker.png)，使用内置 `image_gen` 生成的透明背景小猪贴纸，用于空状态和全局拖拽提示，不替代语义图标。
- SVG 图标为界面语义图标，使用统一线宽；不以 emoji 或 Unicode 字符替代图标。

## Evidence boundary

自动化证据包括 Electron Node 测试、Python 测试、OpenAPI 生成/类型检查、语法检查和静态设计检测。真实 Electron 窗口在 300MB 源文件、>16MiB 音频、大 ZIP、低端设备和三档尺寸下的内存、首帧、吞吐与取消时延仍需现场采集；设计契约不把这些阈值写成已经测得的事实。
