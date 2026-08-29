# 2A 本地验收报告

日期：2026-08-29（Asia/Shanghai）  
STATUS: PASS  
SIDE_EFFECT_POLICY: REAL_PROVIDER_DISABLED

2A 本地硬门已通过。报告原始证据见 [`2a-gate-report.json`](2a-gate-report.json)。2.7.44 修复后的全量回归为 Python 308 项、Electron 71 项；OpenAPI 生成/校验/typecheck、2A/Full 迁移和 schema 检查均通过；Electron 和 macOS 构建均使用 Node 24.20.0。逻辑 smoke 在离线策略下通过。当前修正版 macOS arm64 包已重新构建并完成产物校验。

本次实施审计又修复了源文件上传非 2xx 响应未向 Renderer 传播、失败操作/Attempt 错误释放错误幂等占位、预事务 side-effect journal 孤儿、Artifact 符号链接/TOCTOU 读取路径、垃圾回收删除前引用复核、结果页回退到旧产物，以及 CI 仍调用已退休健康检查路径等问题；同时修复了打包后 Playwright 直跑 smoke 找不到 Node 的运行时回退、已观察 receipt 后输出失败导致重复提交、通用 resolve 越过 receipt 边界、provider receipt 父状态未同步、外部 receipt 被覆盖，以及敏感字段和异常细节脱敏遗漏。公开 ErrorResponse 错误码也已与运行时对齐。修复后的回归证据已写入本报告和门禁 JSON。

前一轮 macOS arm64 DMG：`electron/release/小猪wordTTS-2.7.42-arm64.dmg`，SHA-256 为 `79d7addfcaeafed0b23e2d328a74ad8ea0ad02404a98c3000defb775254b67c`。后端直跑 Playwright/Chromium smoke 和 Electron 桌面 `--smoke-test` 均通过；该本机产物为 ad-hoc 签名，未 notarize。该包早于本轮 Provider 默认开关修复，不能继续使用。

当前修正版 macOS arm64 DMG：`electron/release/小猪wordTTS-2.7.44-arm64.dmg`，SHA-256 为 `93171edecfe00a0f3f9e6dfb4132ba8a4d0a030cd683b6b48fc393662dd14ca2`（在调度器同意门控与停顿快速路径合入后于 2026-08-29 16:16 重新打包）。打包内 `app.asar` 已确认正式路径 `const realProviderEnabled = !isSmokeTest`，后端帮助已包含 `--disable-real-provider`；DMG 校验和、ad-hoc 签名和离线桌面 smoke 均通过。

前一轮 macOS arm64 打包版已在隔离数据目录完成 DOCX 导入、源 Artifact 校验、解析、解析 Artifact、内容回读、SSE snapshot 游标、暂停/恢复和历史查询黑盒链路。PyInstaller 已显式收集 `db/migrations/*.sql`；迁移资源异常时 API 会返回结构化 `MIGRATION_ERROR`，不会再把首个工作流请求表现为 HTTP 500。源码继续变更后，发布前仍需重新打包复验该黑盒链路。

本轮新增的原生导入路径不再把所选文档整体复制到 Renderer：主进程持有在支持的平台上带 `O_NOFOLLOW` 的文件句柄，以一次性不透明句柄和固定 `Content-Length` 流式写入受管 source generation；上传失败、窗口关闭或句柄超过 5 分钟均会关闭并失效。对应文件句柄、上传代理和分块请求回归已通过；浏览器拖拽、语音资源读取及结果页波形组装仍保留各自的内存路径，未宣称 300MB 全链路支持。

本次问题复现还覆盖了最终 `.app` 的 Renderer 上传按钮链路：真实界面触发后依次完成工作流创建、源文档写入票据、26685 字节 DOCX 写入和 `POST /parse`，界面进入“核对与设置”并显示“文档解析成功”。期间修复了 Renderer 源文档写入票据请求漏传 `X-Idempotency-Key` 导致的 400 校验失败，并保留原始安全文件名；对应 Electron 回归测试已覆盖请求体和请求头断言。

针对中断后重试出现 `state_version conflict: expected 2, current 3` 的问题，Renderer 现在会在返回配置和重新生成前读取服务端权威快照，并在提交前后保持状态版本单调；GET/POST 之间若发生明确的乐观锁冲突，只重读一次后安全重试。旧 SSE 快照不会再回退状态版本、执行状态或事件游标；对应回归测试已覆盖。

针对第一次生成时关闭浏览器、再次选择音色后浏览器只打开页面但不继续操作的问题，自动化会在登录、页面恢复和每次生成尝试前识别讯飞持久 profile 的“发现本地缓存”拦截层，并选择“空白开始”；生成前当前文档和音色/参数配置也会写入工作流快照，避免重试时回落到默认音色。确认前的通用 `XunfeiError` 会按非歧义失败处理；即使 Playwright 先抛传输错误，也会通过 `page.is_closed()` 识别关闭页面。确认前的浏览器关闭/用户取消会进入 `WAITING_USER`，不会被 provider-aware scheduler 自动重开旧任务；用户改音色后建立新 plan，旧 attempt 保留为审计历史；已开始外部提交且仍有未决副作用的原任务继续冻结配置，必须人工核对或按安全策略处理。

针对讯飞新版编辑器未插入停顿标签的问题，composite_cut 现在使用原生编辑器选区，在打开“停顿”菜单前后恢复选区，并兼容页面显示的 `2秒`、旧页面的 `2s` 及 `data-value/data-duration` 等元数据；每个边界插入后都会回读 DOM 标记，缺失或时长错误会让任务明确失败，不会伪装成已完成。默认 composite_cut 在逻辑段落之间插入停顿；single_segment 按设计不插入边界停顿标签。

试听计划只提交前三条并以 `PARTIAL_SUCCESS` 收敛，避免预览误提交整篇文档；终态工作流再次点击生成会先创建新的 rerun workflow；持久化质量档位会传入旧版讯飞音频导出的 MP3 码率。以上三条均有新增回归覆盖。

已覆盖的关键故障边界：

- 迁移 checksum、显式 2A/Full profile、事务回滚、升级前 SQLite 与 side-effect journal 备份；
- source generation 单 writer/fencing、Blob size/SHA-256/READY 一致性、Artifact ticket 重放拒绝；
- EventStore seq/snapshot/cursor 过期、标准 SSE、Store 去重/缺口/持久游标；
- FakeProvider 提交前失败、提交后不确定、receipt/Artifact 复用、旧 fencing 回写和终态归档；
- ExternalRecord 业务主键、lease、operation、跨 run binding、reconcile/人工解决和重启后 `IN_FLIGHT → AMBIGUOUS`；
- 实际子进程 kill 探针：`python3 tools/process_recovery_probe.py`。

本地 PASS 不等于发布支持声明。按产品决定（2026-08-29），真实讯飞账号 smoke（当前账号无额度）与目标设备现场性能/fsync（仅开发机基线）作为显式豁免项记录在 [`release-gate-report.json`](release-gate-report.json) 的 `waived_items` 中，不阻塞 `release_ready`；`python3 tools/release_gate.py --no-waivers` 可随时恢复严格校验。大文件全链路流式、具体外部业务系统接入和账号额度恢复后的真实账号 smoke 仍是后续待办。

## 本轮复核补充（2026-08-29）

针对复核发现的确定性缺陷，已补齐幂等键的命令/方法/资源/目标绑定、失败请求清理的 scope 歧义保护、TTS 主 Artifact 的语义归属、旧 workflow 异步结果覆盖新任务的竞态，以及媒体错误后旧 Blob URL/失败 Promise 未清理的问题；Store 也新增非法 snapshot/event 序号拒绝。2.7.44 本轮又修复了解析后的 `PREPARING` 工作流被后台恢复器误当成已确认生成、生成启动竞态导致的配置冻结，以及新版讯飞停顿菜单点击后原生选区丢失；Python 308 项、Electron 71 项均通过，Node 24 下 2A gate 为 PASS。发布开放项按产品决定豁免两项：真实讯飞账号 smoke（当前账号无额度）与目标设备现场性能/fsync（仅开发机基线），均已在发布门报表留痕；浏览器/渲染器大文件全链路流式仍需完成。

本轮复核补充又落地两项修复并刷新证据：自动重试调度器现在要求 run 存在用户确认生成的 `WORKFLOW_GENERATE` 事件才无人值守重试，历史接管产物遗留的 `WAITING_RETRY` 任务不再自行重开讯飞浏览器（新增回归 `test_unattended_retry_requires_an_accepted_generation_command`）；composite_cut 停顿插入新增“光标折叠到行尾”快速路径（`JS.PLACE_CARET_AT_ROW_END` 一次求值完成聚焦与选区折叠，一步点击时长控件），失败时依次降级到原脚本选区契约与原生 `select_text` 兜底，单处停顿的 Playwright 往返回到原脚本水平，插入结果仍由逐行回读校验兜住。`tools/release_gate.py` 新增显式豁免机制（`WAIVED` 状态 + 原因留痕，`--no-waivers` 恢复严格门），发布门在豁免后 `release_ready=true`。

本轮针对实施复核的 7 个高优先级问题又完成了租约续期 TTL/阻塞调用心跳、失租约后的持久化对账收敛、journal/SQLite 意图状态对齐、STEP/ITEM resolve 的结构化拒绝、已成功 WorkUnit 的重试保护、正式运行默认开启真实 Provider 和创建 workflow 幂等原子化；窗口退出时也会关闭未消费的源文件句柄及 Artifact 流。创建路径之外，已有资源命令的 reservation/业务变更/response complete 仍是分开的短事务，尚未宣称为全局单事务语义。

## 2.7.45 收尾（2026-08-29）

T8/T16 剩余项已在本地完成并刷新证据：

- **T16a 拖拽导入流式化**：新增 `electron/source-staging.js` 分块暂存（约 4MB/块、一次性 uploadId、顺序校验、大小上限、TTL 与退出清理），主进程经 `source-upload-begin/write/complete/abort` IPC 落盘后按一次性句柄复用 `workflow-source-upload` 流式管道；渲染层拖拽路径不再整块读取文档，失败自动回退整块读取并提示。
- **T8 Store workspace 投影**：`workflow-store.js` 维护有界 workspace（阶段、分段计数、运行时消息、条目总数、执行/结果状态），`prepare` 切换 run 时重置；渲染层以订阅回调从投影渲染进度权威数值，断线重连/快照重同步后自动回到最新值。
- **T16b 语音资源上限**：头像/试听样本在渲染层按资源类型限 8MB（主进程代理 16MB 之内）。结果页音频为按条目按需的已验证 Artifact、ZIP 走服务端 export-zip，内存边界已写入 `docs/workflow-spec.md`。
- **T11 逻辑证据**：新增停顿快速路径优先级回归——工具栏直带时长按钮的页面由折叠光标主路径完成插入，重型兜底零调用；插入位置仍按行尾回读校验。
- 证据：Python `272` 项、Electron `84` 项通过（新增 source-staging 8 项、workspace 4 项、停顿优先级 1 项；server.py 旧引擎删除后 `test_desktop_server` 收敛为 6 项活跃面测试）；Node 24.20.0 下 2A gate 13 项 PASS（`2a-gate-report.json`）。2.7.45 macOS arm64 DMG [`小猪wordTTS-2.7.45-arm64.dmg`](../electron/release/小猪wordTTS-2.7.45-arm64.dmg)（SHA-256=`9a283f89b19313fde150c47e3e3c957e6ce808cbaaff0bd91f9f70040b7cadb4`，server.py 删除旧引擎后重新打包）已通过后端 Playwright/Chromium smoke、桌面 `--smoke-test` 与 ad-hoc 签名校验。

## server.py 旧引擎物理删除（2026-08-29）

按产品决定，方案 13.1 的旧 API 处置从"410 网关 + 保留实现"升级为**物理删除**：`server.py` 由 4074 行收敛到 646 行，删除内容为旧 `SessionState` 会话引擎、约 1950 行旧生成流水线（`generate_audio_stream`）、历史/解析缓存/进度复用辅助函数与全部旧 `/api/*` 路由处理器。保留内容仅限宿主职责：`/api/v1` 工作流挂载、能力校验中间件、音色目录加载与音色资产缓存（`/api/v1/config`、`/api/v1/voice-assets/*`、`/api/v1/health`）、CORS/本地来源限制与打包启动逻辑（含 Playwright 打包 smoke）。

未迁移路径（`/api/config`、`/api/health`、`/api/generate` 等）由中间件统一返回 `410 API_VERSION_RETIRED`，不再依赖路由存在；`tools/release_gate.py` 的 `legacy-api-410` 与 `legacy-api-retirement-code` 探针均 PASS。`tests/test_desktop_server.py` 由 43 项（1592 行）收敛为 6 项活跃面安全/契约测试，并新增"旧路径 410 收口"回归。全量验证：Python `272` 项、Electron `84` 项、2A gate 13 项 PASS（Node 24.20.0）、发布门 `release_ready=true`。

