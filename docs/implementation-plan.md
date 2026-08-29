# 自动化工作流实施状态

本文件是 `AUTOMATION_WORKFLOW_OPTIMIZATION_PLAN.md` 的实施索引。方案文档是需求蓝图；本文件只记录当前工作树中已经落盘并有验证证据的内容，不把外部环境尚未证明的事项写成“已支持”。方案原文和用户提供的 `examples/documents/` 文件均未改写。

## 任务状态

| 批次 | 状态 | 当前交付与证据 |
| --- | --- | --- |
| T0 | 本地完成 | 版本来源、数据目录、资源边界、回退矩阵、威胁模型和基线见 `docs/baseline-report.md`、`docs/decision-record.md`、`docs/rollback-matrix.md`。 |
| T1/T4 | 本地完成 | `contracts/openapi.yaml` 为 HTTP 事实源；`generated.ts`、`domain.ts`、typed target、SSE、错误码、ticket 和 429 契约已生成并由固定脚本校验。 |
| T2 | 本地完成 | 0001–0004 为 2A，0005 为 Full；checksum、显式 profile、`BEGIN IMMEDIATE`、原子回滚、只读 check、锁、完整性校验、升级前 SQLite/journal 备份已落地。 |
| T3 | 本地完成；命令幂等仍分层 | Repository、状态机、条件版本、租约 fencing、持久预算、receipt/binding、终态不可逆和归档事实保留已落地；创建 workflow 的幂等占位、run 创建和响应已合并到同一事务，已有资源命令仍使用持久化 reservation/complete 协议。 |
| T5 | 本地完成 | SourceImport generation、单 writer grant、staging → SHA-256/size 校验 → 不可变 Blob、run-local Artifact、历史查询和 orphan 扫描已落地。 |
| T6 | 本地完成 | 持久 EventStore、seq/snapshot/cursor 过期、标准 SSE（包括 snapshot 的 `id` 重连游标）、主进程/Preload fetch 代理和一次性 ticket 已落地。 |
| T7 | 本地完成 | FakeProvider 已串起 plan → durable submission → receipt → composite/segment Artifact → verify；提交前/后失败、恢复、重试和复用有回归测试。 |
| T8 | 本地完成 | Renderer 已接入 UMD Store，只有 Store 接受事件后才推进并持久化 `last_event_id + seq`；SSE 410/缺口会清除旧游标并回到 snapshot。语音资源、Artifact、历史和下载均走窄 Preload/API ticket。Store 现在维护有界 workspace 投影（阶段、分段计数、运行时消息、条目总数），进度 UI 的权威数值由订阅回调从投影渲染，断线重连/快照重同步后自动回到最新值；事件处理函数只负责日志与一次性转场。 |
| T9 | 本地完成；真实账号现场验证按产品豁免 | 启动时已接入安全恢复、干预过期和限量 GC；持久 scheduler 只认安全的 `WAITING_RETRY`、可重试错误、未跨外部副作用边界的步骤，并要求 run 存在 `WORKFLOW_GENERATE` 同意事件才无人值守派发，由正式运行时以 `MAX_ACTIVE=1`、队列上限 4 派发。真实讯飞自动重试的账号现场验证按产品决定豁免（账号无额度）。 |
| T10 | 本地硬门通过 | 2A gate、迁移/schema 负向检查、全量 Python/Electron 回归和进程 kill 探针通过；硬门细节见 `docs/2a-acceptance-report.md` 与 `docs/2a-gate-report.json`。 |
| T11 | 逻辑链路完成；真实账号 smoke 按产品豁免 | `XunfeiTTSAdapter`、固定 `composite_cut` smoke harness、无页面逻辑 smoke 和预算已完成；正式桌面 App 和后端默认开启真实讯飞，`--disable-real-provider`/`WORDTTS_ENABLE_REAL_PROVIDER=0` 仅用于显式离线诊断，`--smoke-test` 始终离线；真实账号 smoke 因账号无额度按产品决定豁免并在发布门留痕，额度恢复后应补做受控 smoke。 |
| T12 | 本地完成 | Application Service/WorkflowEngine 已承接编排；路由保持薄层；生产模式旧 `/api/*` 返回 410，新 `/api/v1` 为运行入口。 |
| T13 | 本地完成 | Provider Port、BrowserRuntime、SubmissionTracker、ArtifactDownloader、能力快照和 Xunfei 适配边界已抽取；新增 Provider 不改 Engine。 |
| T14 | 本地完成 | Parser/AudioProcessor/Verifier 端口、稳定 identity/version/hash、流式校验和 segment 边界校验已落地。 |
| T15 | 本地完成；具体外部系统待接入 | Full profile 的 ExternalRecord、业务主键唯一映射、记录 lease/fencing、operation、receipt、verify/reconcile、人工解决、跨 run binding 和 FakeExternal 测试已完成；具体业务系统及其凭据不在当前请求中，真实集成保持关闭。 |
| T16 | 本地完成；目标设备现场指标按产品豁免 | `MAX_ACTIVE=1`、`QUEUE=4`、429/Retry-After、有界任务、原生源文档主进程流式上传、音频按需 Artifact ticket、主进程 Artifact 分块传输、文件保存临时文件 + fsync + 原子替换、头像缓存 Blob URL 和 Store 收敛已完成。拖拽导入已改为主进程分块暂存（约 4MB/块，一次性 uploadId、顺序校验、TTL 与退出清理），渲染进程不再整块持有文档；语音头像/样本按资源类型限 8MB（代理层 16MB），结果页音频为按条目按需的已验证 Artifact，ZIP 走服务端 export-zip。内存边界已写入 `docs/workflow-spec.md`；波形 MSE 流式播放是可选优化。最低支持设备现场指标按产品豁免。 |
| T17 | 本地完成；发布环境待验收 | 只读/dry-run 旧数据导入、版本校验、旧 API/路径静态门禁、备份验证和发布门禁已完成；本轮使用 Node 24.20.0 完成项目级 Electron、契约和 macOS 构建验证；中断后 `state_version`/`configuration_revision` 冲突的快照同步与安全重试、试听范围、终态 rerun、质量参数传递、浏览器断开后的用户接管和安全 retry 派发已补齐；真实讯飞证据与目标设备现场性能按产品豁免，浏览器/渲染器大文件端到端流式仍是待办。当前 2.7.44 macOS arm64 包已重新生成，并通过后端 Playwright/Chromium、Electron 桌面冒烟、DMG 校验和 ad-hoc 签名验证。 |

## 当前可重复验证

本轮 2.7.45 最新证据：Python `309` 项、Electron Node tests `84` 项通过（新增 source-staging 分块暂存 8 项、Store workspace 投影 4 项、停顿快速路径优先级 1 项）；2A gate、OpenAPI 契约检查通过。当前 DMG 为 [`小猪wordTTS-2.7.45-arm64.dmg`](../electron/release/小猪wordTTS-2.7.45-arm64.dmg)，SHA-256=`31c5d9ffb536f361c3629f81d1e0928a1338c2a552b8dc1396167a5f16df067f`，已通过后端 Playwright/Chromium smoke、桌面 `--smoke-test` 与 ad-hoc 签名校验。正式包默认启用真实 Provider，只有 `--smoke-test`、`--disable-real-provider` 或 `WORDTTS_ENABLE_REAL_PROVIDER=0` 才进入离线路径。

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
(cd electron && npm test)
(cd electron && npm run generate:contracts && npm run check:contracts)
python3 db/migration_runner.py --check --profile 2a
python3 db/schema_checks.py --profile 2a
python3 db/migration_runner.py --check --profile full
python3 db/schema_checks.py --profile full
python3 tools/process_recovery_probe.py
PATH="/opt/homebrew/opt/node@24/bin:$PATH" python3 tools/run_2a_gate.py --output docs/2a-gate-report.json
PATH="/opt/homebrew/opt/node@24/bin:$PATH" python3 tools/release_gate.py --output docs/release-gate-report.json
```

当前工作树最后一次回归：Python `308` 项通过，Electron Node tests `71` 项通过，OpenAPI 生成/静态检查/typecheck、2A gate 和目标设备模型模拟均通过；项目级 Electron/契约/打包验证使用 Node `v24.20.0`，系统默认 Node 22 不作为本轮验证入口。试听范围现在由后端把预览计划限制为前三条并以 `PARTIAL_SUCCESS` 收敛；继续生成会创建新的 rerun workflow，避免在终态任务上错误 patch；真实旧版讯飞音频导出会按持久化质量档位选择 MP3 码率。生成前当前音色及速率/音调/音量、provider、账号作用域和生成模式会写入工作流快照，并在 Provider 计划中固化；配置补丁和生成接受都带 `configuration_revision`。原生文件选择现在通过不透明一次性句柄由主进程流式上传，避免把大文档复制到 Renderer；写入票据漏传 `X-Idempotency-Key` 的 400 校验问题、中断后渲染器使用旧 `state_version`/配置版本的问题、讯飞持久浏览器残留“发现本地缓存”弹窗阻塞新任务的问题、过期源写入租约和 staging 路径越界问题、浏览器关闭后旧音色被自动 retry 抢占的问题、讯飞新版停顿菜单未插入标签的问题、无启动 token 时版本化 API 默认放行的问题、上传非 2xx 错误丢失、失败幂等占位误释放、预事务日志孤儿、Artifact 符号链接/TOCTOU 读取风险、垃圾回收删除前的引用复核与受控安全删除、结果页旧产物回退展示、打包后 Playwright Node 回退以及 CI 旧健康检查路径均已修复。后续审计又修复了已观察 receipt 后的输出失败不重复提交、通用 resolve 越过 receipt 边界、provider receipt 不回写父 WorkUnit，以及外部 receipt 被不同证据覆盖的问题；同时补齐配置/事件/来源元数据和异常细节的敏感字段脱敏，并把公开 ErrorResponse 错误码枚举与运行时对齐。又修复了同一 scope 内幂等键未绑定命令/方法/资源/目标元数据、失败请求清理可能误删另一 scope 的未决占位、TTS 产物列表顺序变化导致派生关系错挂，以及旧 workflow 异步结果晚返回覆盖新任务结果页的问题；Store 现在还会拒绝非法快照/事件序号；试听媒体解码失败时会清理旧 Blob URL 和失败 Promise，后续点击会重新获取 Artifact 内容。结果页现在只接受最新的 READY/已验证且元数据一致的服务端产物，不会在新产物无效时回退展示同一条目的旧音频；历史页按服务端状态投影展示活动任务和交付缺口。正式 Electron App 和后端默认开启真实 Provider，`--disable-real-provider`/`WORDTTS_ENABLE_REAL_PROVIDER=0` 仅用于显式离线诊断；`--smoke-test` 始终离线。当前 macOS arm64 修正版 DMG [`小猪wordTTS-2.7.44-arm64.dmg`](../electron/release/小猪wordTTS-2.7.44-arm64.dmg) SHA-256=`93171edecfe00a0f3f9e6dfb4132ba8a4d0a030cd683b6b48fc393662dd14ca2`，为 ad-hoc、未 notarize 产物；包内 `app.asar` 的正式路径默认启用 Provider，后端离线开关只由显式参数触发。真实讯飞账号 smoke（账号无额度）与目标设备现场性能/fsync（开发机基线）按产品决定豁免并在发布门 `waived_items` 留痕，豁免后 `release_ready=true`；`--no-waivers` 可恢复严格门。浏览器拖拽/语音资源/渲染器结果组装的大文件端到端流式内存证据仍待闭环。目标设备模型模拟已完成，但不替代现场实测。

## 资料边界

新增实现只以 SQLite、受管 Blob、side-effect journal 和事件事实为运行时来源。旧 JSON/目录只由 `tools/import_legacy_readonly.py` 做显式一次性 dry-run/apply，原始文件不会被修改；旧兼容代码仅为历史测试/导入保留，生产旧 API 已 410。四个用户提供的 `examples/documents/` 示例文档保持原样。
