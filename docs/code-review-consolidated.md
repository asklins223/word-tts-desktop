# 代码审查问题汇总（历史基线与当前复核）

> 范围：`main` → 当前工作树，后端工作流/原子模型 + 前端工作台五区重构。下方“历史审计明细”保留早期逐项定位和原始优先级，不能直接当作当前待修清单。  
> 方法：多轮逐项对照源码 `file:line`，并复现 Starlette `latin-1` / `JSONResponse allow_nan=False` / `_number(NaN)` / `BEGIN IMMEDIATE` 等边界。当前最终证据以方案文档 §16.9、契约检查、全量回归和现场验收清单为准。

---

## 当前复核结论（2026-08-30）

本轮已把会造成错误交付、重复副作用、正文不可读或恢复死路的代码问题收口；“现场边界”不是静态代码可替代的证据，仍按现场清单追踪。

| 审计项 | 当前状态 | 复核落点 |
| --- | --- | --- |
| 1 中文文件名响应头 | 已修复 | 自定义文件名头改为 percent-encoded；`Content-Disposition` 保持 RFC 5987；API 回归覆盖中文源文件。 |
| 2 JSONL 撕裂尾部 | 已修复 | 读取前只截断无换行的最终半行；完整换行的坏 JSON 仍 fail-closed；有定向测试。 |
| 3 跳过条目竞态 | 已修复 | TTS 失败更新增加 `status <> 'SKIPPED'` 守卫。 |
| 4 超长正文 | 已修复 | `content_ref` 详情接口按 UTF-8 offset 分块，单次响应 ≤ 64KiB，Renderer 可重组全文。 |
| 5 定向命令吞并 | 已修复 | 协调器键包含 workflow、动作和稳定序列化后的 target。 |
| 6 事件唯一约束 | 已修复 | EventStore 对唯一冲突做一致性重查；同 mutation 仍只返回同一事件。 |
| 7 旧配置快速复用 | 已修复 | 生成计划按当前 input/profile/submission key 精确复用，不再按“最新成功计划”旁路复用。 |
| 8 Provider READY | 代码已 fail-closed，现场待证 | 无明确快照时投影为 `UNKNOWN/false`；真实登录、过期和恢复仍需 Provider 现场证据，不能由静态适配器构造证明。 |
| 9 原子桥接 | 已修复 | bridge 写操作统一进入数据库事务。 |
| 10 Artifact 背压 | 已修复 | 主进程有并发上限、空闲 watchdog、取消清理和 renderer 消费确认。 |
| 11 生成调度竞态 | 已修复 | 容量检查、持久化受理和本地任务登记由进程级临界区保护。 |
| 12 Windows 文件锁 | 已修复 | 使用 `fcntl`/`msvcrt`，无可用锁后 fail-closed。 |
| 13 内容缓存串 workflow | 已修复 | cache key 包含 workflow ID；仍建议后续补 LRU。 |
| 14 一致性读 | 已修复 | `read_transaction` 显式开启只读事务，workspace/content 读取在同一快照内完成。 |
| 15 成功无产物死路 | 已收口为安全恢复边界 | workspace 不把该条目算作完成；存在工作单元时提供只读 `RECONCILE`，无工作单元时明确禁用并提示缺失事实，绝不盲目重提。真实 Provider receipt/产物恢复仍需现场或后续证据路径。 |
| 16 / N7 非有限数值 | 已修复 | workspace 元数据、进度数值和 Provider 详情均过滤 `NaN/Inf`。 |
| N3 幂等 UNIQUE | 已修复 | `begin_idempotency` 对竞争插入做重查、重放或返回进行中，不泄漏唯一约束 500。 |
| N4/N5/N6 维护项 | N4 已修复；N5/N6 后续优化 | `workspaceByWorkflow` 已限制为 8 个并保留当前 workflow；文件日志与 SQLite 双写、SSE 轮询退避仍是后续优化，不改变当前安全动作语义。 |
| ZIP 当前交付范围 | 已修复 | 确定性 ZIP、workspace delivery、历史列表和结果页下载均按当前可交付 Artifact 集合核对；旧的部分成功 ZIP 不会在新音频产出后继续冒充当前交付，并有回归覆盖。 |

以下内容从这里开始是历史审计明细，保留用于追溯问题如何发现、为何分级以及哪些建议已经落地。

## 历史 P0 阻断（初始审计基线）

### 1. 中文文件名致产物下载 500 — 属实 P0

- **现象**：`X-Artifact-Filename` 原样写入中文，用户上传 `英语听力.docx`（默认名 `未命名文档.docx`）后 `GET /artifacts/{id}/content` 与 `export-zip` 500。
- **定位**：`api/workflow_routes.py:2145` `headers["X-Artifact-Filename"] = metadata["filename"]`；`api/workflow_routes.py:771` `_safe_content_filename` 保留中文（仅滤 `ord<32`）；`api/workflow_routes.py:2135` 同函数 `Content-Disposition` 已 `quote(safe='')`，唯此头遗漏。Starlette 头按 `latin-1` 编码，`python3 -c "'未命名文档.docx'.encode('latin-1')"` 复现 `UnicodeEncodeError`，`quote` 后 `%E6%9C%AA...` 通过。
- **影响**：中文命名文档（最常见输入）无法下载 ZIP 与源文件；`tts-segment 001.mp3` 不受影响。
- **建议**：`quote(metadata["filename"], safe='')` 或移除该自定义头，仅保留 `Content-Disposition` + `X-Artifact-SHA256/ETag`。
- **复核补充**：根因与 `_artifact_content_metadata:864` 一致；`tts-segment` 因数字文件名不受影响判断准确。

### 2. 副作用日志撕裂写导致永久瘫痪 — 属实 P0

- **现象**：进程在 `write` 与 `fsync` 间断电留下半行 JSON，重启后所有 TTS 与恢复均失败。
- **定位**：`workflow/side_effect_log.py:234-262` `_append` 裸 `ab` 追加无截断修复；`workflow/side_effect_log.py:186-196` `read_entries` 遇半行直接抛 `SideEffectLogError`；调用点 `workflow/repositories.py:3483` `prepare_tts_plan` 与 `workflow/recovery.py:121` `apply_safe_recovery` 均首步读日志。
- **补充**：`side_effect_log.py:240` `fcntl.flock` `except ImportError/OSError: pass`，Windows 下并发裸写同样可撕裂，不止断电。
- **建议**：启动时截断至最后一条完整 `\n` 再读。

### 3. `mark_tts_failure` 覆盖已跳过条目 — 属实 P0

- **现象**：失败后用户将条目置为 `SKIPPED`，仍被后续失败路径覆写为 `FAILED/AMBIGUOUS` 并重新计费。
- **定位**：`workflow/repositories.py:4063-4070` `UPDATE work_items SET status=? WHERE item_id IN (SELECT ... work_unit_items)` 缺 `AND status <> 'SKIPPED'`；同文件 `workflow/repositories.py:4392` `complete_tts:4392` 已有该守卫。
- **触发窗口**：调度器 15s 自动重派与用户跳过竞态；`workflow/engine.py:252` 物化后 `begin` 前无重校验。
- **建议**：补 `AND status <> 'SKIPPED'`。

### 4. 超过 64KB 的条目正文永久不可读 — 属实 P0

- **现象**：`>64KB` 条目可见但点击 `加载全文` 永远 `413`，亦无法编辑。
- **定位**：`workflow/workspace.py:31-32` `INLINE 16KB / DETAIL 64KB`；`api/workflow_routes.py:1257-1271` `size > DETAIL_LIMIT → 413 ITEM_CONTENT_TOO_LARGE` 且 `truncated:false` 固定；`contracts/openapi.yaml:1690` `truncated: {enum: [false]}`；写入侧允许 100 万字符；`electron/renderer/app.js:2242` `hasContent=false` 禁编。
- **建议**：增加分段/流式读取或提升上限并同步契约。

### 5. 命令并发键缺 `target` 导致 `RETRY` 吞并 — 属实 P0

- **现象**：同 workflow 不同 item 并发 `RETRY`，第二目标被静默合并丢失。
- **定位**：`electron/renderer/workflow-command-coordinator.js:89` `key = workflowId:actionType`；`electron/renderer/workflow-command-coordinator.js:23` `TARGETED_COMMANDS={retry,reconcile}` 但键未含 `target`；`electron/renderer/workflow-command-coordinator.js:90-91` 直接复用 `inFlight`。
- **建议**：键加入 `target` 哈希或 `expected_target_state_version`。

### 6. `EventStore.append_in_transaction` 并发 `UNIQUE` 未捕获 — 部分高估，**P0→P1**

- **现象**：同 `mutation_id` 并发提交原报告称转 500，幂等失效。
- **定位**：`workflow/event_store.py:111-152` 先 `SELECT` 后 `INSERT`，无 `sqlite3.IntegrityError` 捕获重查描述属实。
- **复核降级理由**：`workflow/database.py:138` `BEGIN IMMEDIATE` 已串行化同库写，第二事务在 `BEGIN` 即阻塞，实际撞 `UNIQUE` 需裸 `isolation_level=None` 或跨进程未走 `transaction()` 时才可达（`application/atomic_bridge.py:65` 为此例）。单进程 `pytest 407` 不暴露，防御性缺陷非稳定复现 P0。
- **建议**：`except sqlite3.IntegrityError: SELECT 重查返回已存在事件` 作防御，随 P0 同批修复。

### 7. 已交付快速路径未校验配置 — 属实 P0

- **现象**：改音色/模式后仍静默复用旧 mp3。
- **定位**：`workflow/engine.py:216-235` `delivered_ids == len(all_items)` 直接 `get_latest_successful_tts_plan:3321` 按 `finished_at DESC` 取任意 `SUCCEEDED`，未比对 `profile_hash / input_hash / generation_mode`。正常 `submission_key` 路径 `engine.py:300` 本会校验，此旁路绕过。
- **建议**：复用前校验 `profile_hash` 与 `input_hash` 一致。

---

## 历史 P1 严重（初始审计基线）

### 8. Provider READY 乐观导致按钮可用提交才拦 — 属实 P1（描述补 nuance）

- **定位**：`api/workflow_routes.py:709-712` `ready = has_backend || allow_real` 默认 `True`；`workflow/providers.py:360` `capability_snapshot` 无 `status/ready` 字段，仅返回 `real_calls_enabled/backend`，`api/workflow_routes.py:723-731` 无 `status` 时不覆写，`736-743` 仅非 `READY` 才强制 `false`，生产 `allow_real=True` 时永远 `READY`。`workflow/workspace.py:740` 同理。
- **补充**：原报告“快照无登录态仍保持READY”方向对，应点明缺字段方为 `XunfeiTTSAdapter`。

### 9. 桥接半提交 — 属实 P1

- **定位**：`application/atomic_bridge.py:65` `database.connect(write=True)` `isolation_level=None` 自动提交，未走 `workflow/database.py:134` `BEGIN IMMEDIATE`，多步 `persist_parse / create_operation_plan / create_audio_tasks:76-82` 可留孤儿 `source_document`。

### 10. 产物/事件背压泄漏 — 属实 P1

- **定位**：`electron/main.js:1207` `stream.pause()` 待 `ack`；`electron/workflow-artifact-transport.js:53` `desiredSize <= 0` 不 `ack`；`electron/main.js:45` `workflowArtifactStreams` 无 `idleTimeout` 与并发上限，渲染崩溃永久 `pause`。
- **补充（加强版）**：`workflow-artifact-transport.js:52-53` `desiredSize<=0` 不发 `ack`，而 `main.js:1207-1208` 每 `chunk` 即 `pause()+waitingForAck=true`。渲染器正忙（波形解码/大表渲染）时 `pull()` 不触发，`desiredSize` 为 0 导致永不 `ack`，与本项同根但为必现界面卡死路径。

### 11. 生成调度非原子 — 属实 P1（非资损）

- **定位**：`api/workflow_routes.py:656` `_ensure_generation_dispatch_capacity` 与 `api/workflow_routes.py:871` `_schedule_generation_task` 各 `len >= 5` 无锁，中间已 `accept_generation:1553` 写库。虽二次校验可避免稳定双计费，但会产生 `202` 后回退闪烁。

### 12. Windows 文件锁失效 — 属实 P1

- **定位**：`workflow/database.py:98-100` `except ImportError: return True`，Windows 无 `fcntl` 时假装加锁成功，双实例可并行。SQLite `WAL+busy_timeout:54` 防损坏但应用层防重入失效；同影响 `workflow/side_effect_log.py:240`。

### 13. 前端 `itemContentCache` 跨 workflow 污染 — **P1→P2**（原高估）

- **定位**：`electron/renderer/app.js:140` `Map` 以裸 `item_id` 为 key，`electron/renderer/app.js:2199,2233` 直接 `get/set(itemId)`，仅 `electron/renderer/app.js:5034` `adoptWorkflowWorkspace` 切 `workflowId` 时 `clear()`，`hydrateWorkflowWorkspace:2643` 不清理。
- **复核降级理由**：`item_id=new_id("item")` 随机，跨 workflow 真正“命中污染”概率极低，更多是内存泄漏+旧条目误展示。Severity 偏高，移至 P2。

### 14. `read_transaction` 非一致性读 — 属实 P1

- **定位**：`workflow/database.py:149` `read_transaction` 无 `BEGIN`，`api/workflow_routes.py:1241` 两次 `SELECT` 各快照，`workflow/workspace.py:363` 多表 JOIN 同一连接但 autocommit 逐语句快照，`state_version` 与正文可来自不同 `commit`，围栏削弱（`item_content_id` 哈希围栏仍存）。

### 15. `SUCCEEDED` 无产物死路 — 属实 P1

- **定位**：`workflow/workspace.py:559-569` `SUCCEEDED` 但无 `READY+verified` 产物置 `BLOCKING ARTIFACT_MISSING_OR_UNVERIFIED`，`GENERATE` 要求 `execution_state ∈ {CREATED,PREPARING,WAITING_RETRY,WAITING_USER}:768` 不可重试，`RETRY` 仅 `FAILED:863`，GC 后无出口。`RERUN` 可绕但同 workflow 无自愈。

### 16. `NaN` 导致 `GET /workspace` 500 — 属实 P1（补第二路径）

- **定位**：`workflow/workspace.py:53-55` `_safe_metadata_value` 放行 `NaN/Inf`，`json.dumps(allow_nan=True)` 可过但 Starlette `JSONResponse(allow_nan=False)` 抛 `ValueError`，`python3 -c` 已复现；`xlsx` 数值单元格为现实来源。
- **补充第二路径**：`workflow/workspace.py:150-159` `_number=float→int(round(NaN))` 抛 `ValueError→500` 未拦截，`electron/renderer/workflow-store.js:81 finiteNumber` 仅在 store 层过滤，后端仍 500。

---

## 历史 P2 中等（可维护性）

- **TTS 干预单永不关闭**：`workflow/repositories.py:2349` `_resolve_target` `WORK_UNIT` 分支未更新 `user_interventions`，仅 `external` 分支 `2580` 会 `RESOLVED`；`workflow/recovery.py:50` 仅过期 `expires_at IS NOT NULL`，TTS `NULL` 永久 `OPEN`。
- **External 租约 60s 无心跳**：`workflow/external.py:251` `acquire_record_lease` 默认 60s，无 `workflow/engine.py:30` `_LeaseHeartbeat:30`，浏览器提交超 60s 即 `STALE_ATTEMPT`。
- **文件名三叉与截断分叉**：`api/workflow_routes.py:771` `workflow/workspace.py:116` `application/workflow_service.py:657` 三处 `_safe_*filename` 各自实现，仅后缀处理一致。
- **GC 竞态**：`application/workflow_service.py:500` `promote` 落盘与入库间若周期 GC 介入可删待引用 `blob`，当前仅启动时 GC 未触发。`workflow/garbage_collector.py:52 referenced_now` 与 `promote` 非原子，周期 GC 会误删 `stage→promote→INSERT` 窗口的待引用 `blob`。
- **前端内存驻留**：`electron/renderer/app.js:140` `itemContentCache` 不随重启自动清理，`VOICE_ASSET_OBJECT_URL_LIMIT:128` 同类 LRU 已有可复用；`itemContentCache` 无 LRU（见 P1-13 降级）。
- **Windows `side_effect_log` 并发撕裂**（新增）：`workflow/side_effect_log.py:240` Windows 落 `except ImportError/OSError: pass`，双线程/双实例并发 `ab` 追加可交叉行，叠加 P0-2 无截断单机亦可瘫。

---

## 已剔除（经核实为夸大或已修复）

- `prepare_tts_plan` 未验 `plan_hash` — 已在 `workflow/repositories.py:3559` 校验 `plan_hash/input_hash/profile_hash`。
- `ROW_NUMBER PARTITION BY item_id` 使统计失真 — 为去重重试后旧段的预期修正 `repositories.py:3047`。
- `8.3 短名 PROGRA~1 绕过` — `electron/main.js:672` `realpathSync.native` 已展开。
- `bridge_parse_to_atomic_model` 失败阻断主链路 — 内部 `application/atomic_bridge.py:106` 已 `try/except` 返回 `bridged:false`。
- `Host/Origin` 未校验（原 N2）— 不成立。`server.py:385 _local_request_allowed` 已校验 `Host∈{127.0.0.1,::1}` + 精确端口 + `Origin∈{null,None}`，`410` 中间件前置，`tests/test_desktop_server.py:71` `Host: attacker.example / Origin: https://attacker.example →403 ORIGIN_NOT_ALLOWED` 回归覆盖。

---

## 新增问题（文档未覆盖，去重后）

### N3 [P0] 幂等键 `scope_hash+client_key` 并发 `UNIQUE` 未捕获 — 全量写接口幂等失效

- **定位**：`db/migrations/0002_execution.sql:243` `UNIQUE(scope_hash,client_key)`；`workflow/repositories.py:4870-4927` `begin_idempotency:4870 SELECT → 4918 INSERT` 无 `try/except IntegrityError` 重查。同 P0-6 同模但影响面更广（`workflow/*`、`source-imports/*`、`external-*` 全经此）。
- **复现**：两并发同 `X-Idempotency-Key`（双击生成/前端超时重发）第二条直接 500 而非 `200重放/409 IDEMPOTENCY_CONFLICT`，`abandon_idempotency:4929` 仅部分 `code` 分支删除，剩余 `IN_PROGRESS` 需 24h 过期。
- **建议**：`INSERT` 包 `except sqlite3.IntegrityError: SELECT … return / 转 IdempotencyInProgress`。

### N4 [P1→P2] `workflow-store.js` `workspaceByWorkflow` 无界 + `artifactObjectUrls` 第二条无界

- **定位**：`electron/renderer/workflow-store.js:467 workspaceByWorkflow:{}` 永不 `delete`，`676-695 setActiveCandidates` 仅 `slice(0,32)` 截断候选不淘汰 map；`electron/renderer/app.js:125 artifactObjectUrls Set` `VOICE_ASSET_OBJECT_URL_LIMIT=128` 仅约束音色资产，`workflow-store` 侧 `workspaceByWorkflow` 与 P1-13 同源但字典级泄漏，`7528 artifactObjectUrls.add / 7505 delete` 逐项回收但 `workspaceByWorkflow` 常驻。
- **建议**：`workspaceByWorkflow` 加 `MAX_WORKSPACES=8` LRU；`artifactObjectUrls` 复用同类 LRU 并在 `audioElements` 卸载时 `revoke`。

### N5 [P2] `SideEffectIntentLog` 文件与 `side_effect_intents` 双写无原子

- **定位**：`workflow/repositories.py:4997-5034` `_transaction_after_intent` `intent_log.record()` 在 `BEGIN IMMEDIATE` 外，`recovery.py:73-127` 重启先读 DB 再 `intent_log.mark(NEEDS_RECONCILE)`。`record` 成功而 DB 回滚 → 文件 `NEEDS_RECONCILE` / DB `RECORDED` 错位，`side_effect_log.py:205 verify_against_rows` 报 `journal state mismatch`，可能误导用户误选 `NOT_SUBMITTED`。
- **补充**：`_resolve_target:2372 NOT_SUBMITTED` 需 `receipt==None && state∉{SUBMITTED,CONFIRMED}` 二次校验，实际丢计费需组合条件，但双写架构债仍存。
- **建议**：单一事实源（仅 DB）或 `intent_log` 引入 WAL 两阶段，与 DB `payload_hash` 校验。

### N6 [P2] SSE 0.25s 轮询 + `1+4` 队列 + 固定重连

- **定位**：`api/workflow_routes.py:1997-2052` `sleep 0.25 → read_after` 每连接 `4 qps`，`_automatic_retry_loop:1103 sleep 1.0` 三路 `limit=8/8/32`；`_ensure_generation_dispatch_capacity:656 / _schedule_generation_task:871` 上限 `1+4` 固定 `Retry-After:1`，`workflow-command-coordinator.js:73` 无指数退避。多 workflow 多窗口下 `CursorExpired→全量 hydrate` 正反馈。
- **建议**：`asyncio.Event` 广播替代轮询或 `poll≥2s` 长轮询；`429` 动态 `Retry-After` + 前端指数退避。P2 策略优化。

### N7 [P2] `NaN` 第二路径 + `source-staging` TTL 边缘

- **定位**：`workflow/workspace.py:150-159` 已修 `metadata` 路径但 `_number: int(round(float(NaN)))` 仍 `ValueError→500`（见 P1-16 补充）；`electron/source-staging.js:47-52 armExpiry 10min` `99/157` 每次 `write` 重置 idle，故弱网持续写不触发，仅 `write→complete` 间隙>10min 才误删，`workflow/source_imports.py:307 expires_at 60min` 双重过期并行。
- **建议**：`_safe_metadata_value/_number` 对 `!isFinite` 丢弃；`staging TTL` 按 `bytesWritten==sizeBytes` 豁免或改心跳续租。

### 其他修正

- **N1 票据 4096 容量（原高估→P2）**：`workflow/security.py:47-61` 已有 `_purge_locked` + `tombstone` 有界化，`server.py:410 authenticate_local_api` `X-Desktop-Capability` 为环回秘密，同机未授权进程无法批量签发，原 P0 降至 **P2** 策略增强（建议每 `resource_id` 限流 + `429`）。
- **`read_artifact` 中文头双写风险**：`api/workflow_routes.py:2135 Content-Disposition: quote` 与 `2145 X-Artifact-Filename` 同时设置，代理层对 `X-Artifact-Filename` 的 `latin-1` 再编码会早于 `Content-Disposition` 生效，建议直接移除该自定义头，仅保留已 `quote` 的 `Content-Disposition` + `X-Artifact-SHA256/ETag`。

---

## 复核记录

> 两轮复核均逐项 `file:line` + `python3 -c` 复现，`grep` 全量扫描。第一轮：16 项定位基本准确，2 项降级（#6 P0→P1，#13 P1→P2），1 项补 nuance（#8），4 项剔除正确；第二轮：新增 N3 P0 经核实为真实全量幂等缺陷，N4/N5/N6/N7 方向属实但 N4/N5/N6 原标 P1 高估应为 P2，N2 不成立，N1 高估。该文档已按复核结果替换对应条目并追加新增章节。

### 修复优先级（历史记录；当前状态见上表）

**合入前必修（P0）**：1 中文头 `quote`、2 日志截断修复、3 `AND status<>'SKIPPED'`、4 详情分段/提升上限并同步 `openapi.yaml:1690`、5 协调器键加入 `target`、7 交付复用校验 `profile_hash/input_hash`、**N3 幂等 UNIQUE 捕获**。

**下版必修（P1）**：8 Provider `status` 缺省分支、9 桥接改 `transaction()`、10 产物背压 `idleTimeout+并发上限+desiredSize<=0` 分支、12 Windows 锁（或文档注明不支援双开）、14 `read_transaction` 加 `BEGIN`、15 `SUCCEEDED` 无产物回退/重建出口、16 + N7 `NaN` 双路径过滤（`53-55` 与 `150-159`）。

**可维护（P2）**：11 调度原子化、13 `itemContentCache` 命名空间+LRU、GC 竞态、三文件名实现收敛、N1 票据限流、N4 `workspaceByWorkflow` LRU、N5 双写原子化、N6 轮询优化、`N2` 已澄清。

> 历史记录中的 `pytest 407 / Electron 104` 是审计当时的基线数字。当前定向边界已补回归；最终数字和仍待现场证据以 `docs/frontend-ui-redesign-plan.md` §16.9 为准。
