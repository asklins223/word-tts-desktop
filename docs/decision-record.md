# 阶段 0/1 决策记录

| decision_id | 主题 | 负责人 | 截止/目标版本 | 已选方案 | 备选及取舍 | 验证证据 | 回退方式 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D-T0-001 | 数据事实源 | 实现者 | 2026-08-28 / 2A | 新 run 只写 SQLite；旧 JSON/目录不双写 | 兼容双写会产生状态分叉，拒绝 | `db/schema_checks.py`、迁移测试 | 回退到基线；保留原运行时数据库 | FROZEN |
| D-T0-002 | 2A schema 边界 | 实现者 | 2026-08-28 / 2A | 0001–0004；0005 延后到阶段 6 | 一次启用外部录入会扩大副作用面，拒绝 | runner `--profile 2a/full` | 目标版本不可降级；使用数据库备份 | FROZEN |
| D-T0-003 | 迁移事务 | 实现者 | 2026-08-28 / 2A | `BEGIN IMMEDIATE` + statement parser + checksum + integrity checks | `executescript` 可能在失败前提交半个脚本，拒绝 | migration rollback/read-only tests | 停止启动并恢复备份 | FROZEN |
| D-T0-004 | 状态并发 | 实现者 | 2026-08-28 / 2A | `state_version` 条件更新；租约另带 fencing token | 仅普通读取或仅 If-Match 无法保护租约回写，拒绝 | DDL CHECK/FK/trigger fixtures | 进入 BLOCKED/人工恢复 | FROZEN |
| D-T1-001 | SSE 授权 | 实现者 | 2026-08-28 / 2A | capability header + 每连接一次性 ticket | URL/query token 会进入日志和历史，拒绝 | OpenAPI/security schema | 重新签发 ticket；游标过期时重同步 | FROZEN |
| D-T1-002 | 条件版本公开形态 | 实现者 | 2026-08-28 / 2A | body 必填 `expected_state_version`；target 命令再带目标版本 | 同时支持可选 If-Match 会产生歧义，拒绝 | OpenAPI generated TS/typecheck | 返回 409，不猜测客户端版本 | FROZEN |
| D-T1-003 | 未决副作用 | 实现者 | 2026-08-28 / 2A | `AMBIGUOUS` 只允许 reconcile/resolve，不自动重提 | “查询失败即重试”会制造重复副作用，拒绝 | workflow-spec 与 receipt schema | 进入 WAITING_USER/BLOCKED | FROZEN |
| D-T0-005 | 运行时版本 | 实现者 | 进入 T11 前 | Node 24.20.0 已安装并完成项目级验证；系统默认 Node 22.23.2 保留，验收命令显式使用 Node 24 PATH | 保留系统默认版本可避免影响其他项目；项目门禁固定验证 Node 24 | `docs/baseline-report.md`、`docs/2a-gate-report.json`、`docs/release-gate-report.json` | 发布机按同一 Node 24 入口复现 | FROZEN |
| D-T0-006 | retention/资源阈值 | 实现者 | 进入 T10 前 | 运行时固定 `MAX_ACTIVE=1`、`QUEUE=4`、staging TTL/按需 Artifact；已完成 CPU 2x、每条 I/O 8ms、512MB 预算的目标模型模拟，真实最低设备数值仍 OPEN | 无界资源可导致低端设备崩溃，拒绝；模拟结果不能冒充硬件实测 | `docs/performance-baseline.md`、`tools/performance_baseline.py` | 保守降低并发/转人工 | OPEN |
| D-T10-001 | 2A 故障门 | 实现者 | 2026-08-28 / 2A | FakeProvider、临时目录、真实子进程 kill、迁移/schema/票据/归属/恢复回归全部通过；真实 Provider 关闭 | 用单元测试数量代替崩溃恢复证据，拒绝 | `docs/2a-gate-report.json`、`tools/process_recovery_probe.py` | 任一硬失败停止新副作用 | FROZEN |
| D-T11-001 | 真实讯飞开关 | 实现者 | 进入真实发布前 | 正式 Electron App 和直接启动的后端默认开启真实 Provider，`--disable-real-provider`/`WORDTTS_ENABLE_REAL_PROVIDER=0` 仅作显式离线诊断选项；`--smoke-test` 始终强制逻辑离线；独立 CLI real smoke 仍必须额外显式确认、限定账号作用域和受控文档；异常只对账不自动重提 | 在本机猜测账号/读取隐式 Cookie，拒绝 | `electron/main.js`、`server.py`、`tools/xunfei_smoke.py`、`docs/real-xunfei-smoke-report.md` | 现场按需显式确认账号、作用域和文档；FakeProvider/逻辑 smoke 回归仍可继续运行 | OPEN |
| D-T15-001 | ExternalRecord 边界 | 实现者 | Full profile | 本地映射/lease/operation/receipt/reconcile/人工解决先完成，具体业务系统适配器和凭据独立接入 | 把外部 ID 当本地主键或承诺 exactly-once，拒绝 | `tests/test_external_runtime.py`、0005 | 进入 AMBIGUOUS/人工对账 | FROZEN |
| D-T17-001 | 发布放行 | 实现者 | 发布前 | 硬代码门通过且 Node 24、真实 smoke、目标设备性能均有现场证据才 `release_ready`；当前 Node 24 已通过，真实 smoke 与目标设备现场证据仍 OPEN | `--allow-open` 不能成为支持声明，拒绝 | `tools/release_gate.py`、`docs/release-checklist.md` | 保持 OPEN，禁止发布 | FROZEN |
