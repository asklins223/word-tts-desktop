# Workflow Spec — 当前冻结摘要

本摘要承载主方案的 5–8 节落地约束；主方案仍是背景、风险和阶段门禁的来源。`openapi.yaml` 与 `db/migrations/*.sql` 是字段事实源，本文不再复制完整 DDL/YAML。

## 领域边界

- `workflow_group_id` 表示业务流程集合，`workflow_id` 表示不可变的一次执行 run；rerun 创建新 run，不能覆盖旧 run。
- Workflow 的 `result_status`、`execution_state`、`control_state`、`cleanup_state` 分开持久化；终态 run 不可重新打开。
- Definition 是不可变的已发布快照。Step、Item、Assignment、Attempt、WorkUnit 和 Artifact 都带 run/step 归属，跨层关系由复合外键或同事务触发器校验。
- `WorkItem.item_identity_key` 用于跨 run 匹配和缓存，`item_id` 只用于当前 run 外键；编辑后无法证明相同 identity 时必须 `UNRESOLVED`，不能静默复用音频。
- ProviderSubmission 保存跨 run 的提交意图和 `tts_submission_key`；WorkUnit/WorkUnitAttempt 保存 run-local 观察；canonical receipt、identifier 和 binding 保存可对账历史。
- SourceImport 是逻辑会话，Generation 是一代不可变导入事实。当前投影可查询，历史 generation 可按 `import_id + generation` 查询；客户端永远看不到 staging path/key。
- Artifact 的正式事实是不可变 Blob + run-local Artifact。跨 run 命中缓存时新建 Artifact 行，只能复用 Blob。

## 状态与条件更新

Workflow 汇总优先级为 `RECOVERING > BLOCKED > WAITING_USER > WAITING_RETRY > RUNNING > terminal`。Step 的执行、验证、失败、对账和清理 attempt 分开编号；`execute_attempt_no` 不被 RECONCILE/VERIFY/CLEANUP 占用。

所有聚合写入都使用 `state_version` 条件更新；租约回写还必须匹配单调递增的 fencing token。`AMBIGUOUS` 只能进入 RECONCILE 或人工解决：确认已提交进入 VERIFY，确认未提交后才允许重新进入 READY。取消遇到未决副作用时只能收敛到 BLOCKED/TERMINATING，不能报告为普通成功取消。

公开命令分两类：parse/generate/pause/resume/cancel 使用 workflow-level `expected_state_version`；retry/reconcile/resolve 必须同时携带 typed target 和 `expected_target_state_version`。MIXED attempt 只能下钻到 WorkUnit、WorkUnitAttempt、receipt 或 external operation，不能以步骤级命令覆盖子操作。

## 幂等与重试

- HTTP 命令使用 `X-Idempotency-Key` 和规范化 body hash；同 key 同 hash 返回原结果，不同 hash 返回 `IDEMPOTENCY_CONFLICT`。
- TTS 提交使用 group/provider/account 范围的 `tts_submission_key`；同一提交意图只能有一个 canonical ProviderSubmission。
- 预算持久化在 `retry_budgets`：pure、tts、external 三类分别记录 reserve/use、次数、耗时和 deadline。`AMBIGUOUS` 期间预算不释放，只有证据确认未提交后才可释放。
- 提交前网络失败可按类别退避；提交后或结果边界不明一律对账，不自动重提。错误必须携带稳定 `error_code`、`retryable` 和 `side_effect_occurred`。
- 用户同意门控：无人值守派发（调度器自动重试）与重启接管都要求 run 存在用户确认生成的 `WORKFLOW_GENERATE` 事件。从未被用户接受生成的 run（例如恢复器/接管过程产生的 `PREPARING` 产物或其遗留的 `WAITING_RETRY` 步骤）只能等待用户在配置页显式确认，不得自动重开浏览器；显式 retry 命令不受此门控限制。

## 事件与交付

事件与状态同事务落库，使用每个 workflow 单调递增 `seq`、唯一 `event_id`、`mutation_id`、correlation/causation 和 actor 审计字段。SSE 的 `id` 是 `event_id`，data 带 `seq`；连接先消费一次性 ticket，再通过 `Last-Event-ID` catch-up，游标过期则返回可识别的重同步错误。内存广播只能唤醒连接，不能作为恢复事实。

上传、Provider 下载和音频处理都先写受管 staging，关闭文件后校验 size/SHA-256，再原子转正为 Blob；数据库提交前不能暴露 READY。迁移、数据库锁、磁盘不足、旧 generation 迟到写入、ticket 重放和进程中断都必须 fail-closed，并留下可恢复诊断。

## MVP 边界

2A 为单机、单账号、单活动 Provider 租约、固定小队列、SQLite WAL + FULL synchronous；默认 2A 运行时使用 0001–0004，0005 由 Full profile 显式启用给 ExternalRecord 阶段。新 `/api/v1` 已成为生产入口，直接运行后端时旧 `/api/*` 返回结构化 410；旧 token/path 代码仅为历史测试和一次性导入保留，新 Renderer 不读取它们。正式 Electron App 和直接启动的后端默认开启真实讯飞副作用，双击安装包即可使用；`--disable-real-provider` 或 `WORDTTS_ENABLE_REAL_PROVIDER=0` 才是显式离线选项。`--smoke-test` 和逻辑 smoke 始终不触网、不打开真实页面；测试/导入 runtime 如需离线必须显式传入 `allow_real=False`。
