# 回退矩阵

| 变更/故障 | 可观察证据 | 安全动作 | 不允许的动作 |
| --- | --- | --- | --- |
| 迁移语句失败 | `schema_migrations` 无新版本；事务回滚 | 停止 worker，保留数据库并从备份恢复/修复迁移 | 继续启动半成品 schema |
| checksum 不匹配或版本倒退 | runner fail-closed | 恢复与代码匹配的迁移文件或备份 | 修改 checksum 伪装已应用 |
| 数据库锁/写入超时 | `PERSISTENCE_ERROR` 或启动失败 | 等待有界时间后重试只读诊断，必要时人工处理 | 假报成功或绕过 SQLite |
| staging 写入中断 | generation 为 `RECEIVING/FAILED`，没有 READY Artifact | 过期回收 staging，重试用新 generation | 迟到 writer 覆盖 READY/当前 generation |
| hash/size 校验失败 | generation/Artifact 非 READY | 删除或隔离临时文件，重新导入 | 将未校验文件标 READY |
| Blob 文件缺失/损坏 | `ARTIFACT_INVALID`/完整性诊断 | 保留 Artifact 审计，重新生成新的 Blob | 直接改写既有 READY Blob |
| SSE ticket 重放/游标过期 | 401/410 与 request_id | 重新申请 ticket 或返回 snapshot 重同步 | 复用旧 ticket、从内存队列猜测缺口 |
| Provider 回调/提交边界不明 | `AMBIGUOUS`、receipt/binding 证据 | 只做 reconcile 或人工确认 | 自动创建新的 EXECUTE submission |
| 旧 Electron 后端/第二实例 | contract version 或 single-instance lock 失败 | 拒绝启动并提示重新安装/聚焦已有实例 | 让两个 backend/profile 同时运行 |
