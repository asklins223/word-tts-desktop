# 小猪wordTTS

![小猪wordTTS 应用图标](electron/renderer/assets/app-icon.png)

小猪wordTTS 是一款本地桌面文档配音工作台：解析教学 `.docx` 与 `.xlsx`，按文档结构核对条目，为不同角色配置声音，批量生成可试听、可单独保存或打包下载的 MP3，并可选地把已验证的音频继续录入外部教学系统。全部任务状态、产物和恢复事实保存在当前电脑，应用离线重启后可以接着做。

版本以 `version.json` 为唯一来源（当前 3.2.9），构建前由 `scripts/project_version.js --sync` 同步到 Electron 的 `package.json`、`package-lock.json` 与 `CHANGELOG.md`。

> 仓库目录名 `edge-tts-webui-main` 与包名 `wordtts/` 是历史遗留。自 v3.x 起 Edge TTS、TTSMaker 及其回退代码已全部移除，音频统一由讯飞配音生成；`edge-tts` 不再出现在依赖清单中。

## 主要能力

- 五段工作台流程：导入文档 → 内容核对 → 配置中心 → 生成任务 → 交付中心；交付阶段下挂可选的「系统录入」，界面在 `electron/renderer/index.html` 的 stepper 中固定为这五个主阶段。
- 八类题型的结构化解析与混合顺序还原：信息获取、听后选择、听后应答、课文跟读、信息转述及询问、听后记录并转述信息、模仿朗读、词汇（Excel 模板）。识别只依据文档结构证据，文件名不作为证据。
- 默认音色：`w/W` 与 `m/M` 标记分别落到可配置的默认女声/男声（出厂为英语-Amanda、英语-George），无标识内容默认女声，单词与例句始终女声。另有两条容易踩水的例外：题干角色使用「题干音色」（晓燕 `common:10000023`，参数 40/50/50），解析器主动判定为男声的条目会覆盖「默认女声」规则。`Reporter:`、`Mr Yan:` 一类通用角色按称谓自动归入男/女声，可在配置中心改名。
- 语速、语调、音量支持 0-100 任意整数（默认 50），输出固定 MP3，质量选项对应 MP3 码率；配置方案可保存为预设复用。
- 生成方式默认「全部生成后切割」：整批提交后按安全停顿切回每段，减少提交次数；无法确认边界时保留合并音频并报告异常，不静默伪造分段。另一个选项是逐条生成。
- 只有同时满足 `READY + verified=true`、Artifact 与字节 Blob 双 `READY`、且 SHA256/大小/扩展名/MIME 事实一致的 `tts-segment` 产物才会进入交付列表；源文档与解析产物不会伪装成音频，ZIP 打包要求全部父项 SUCCEEDED。
- 断线恢复、暂停/恢复/取消/重试/对账由服务端 `available_actions` 投影授权；未决外部副作用不自动重试。讯飞 TTS 明确不提供供应商对账路径（`RECONCILIATION_DISABLED`），此时按可重试呈现而不是猜测结果。
- 本机历史中心最多 20 个任务，可重新查看、试听、下载或归档；归档保留审计事实和 Artifact，不等于物理删除。
- macOS 与 Windows 安装包均由 GitHub Actions 构建；桌面端内置版本中心，自动检查 GitHub Releases，支持可选/强制更新、下载与重启安装。
- Windows 使用自绘 HTML Setup.exe（`installer-prototype/`），安装、更新、卸载共用同一套界面，支持选择安装目录与数据保留策略，更新优先走 `.blockmap` 差分下载，条件不满足回退全量；macOS 保持打开即用或拖入 Applications。
- 正式桌面 App 默认开启真实讯飞调用，双击安装包即用。Electron 的 `--smoke-test` 始终只走逻辑离线流程，不打开真实页面；后端独立诊断时可用 `--disable-real-provider` 或 `WORDTTS_ENABLE_REAL_PROVIDER=0` 显式离线。

## 架构总览

后端是 FastAPI 单进程，由 Electron 主进程拉起，Renderer 不直连 HTTP。

| 目录 | 职责 |
| --- | --- |
| `server.py` | 进程入口：打包态 `PLAYWRIGHT_*` 路径解析、健康检查、`/api/v1/config`、音色目录与试听资产磁盘缓存，其余交给 `install_workflow_api()` |
| `api/` | 唯一 HTTP 面：`workflow_routes.py` 定义 `WorkflowRuntime` 与 `/api/v1` 全部路由 |
| `application/` | 用例编排：`workflow_service.py` 应用服务，`atomic_bridge.py` 旧模型→原子模型的追加式旁路写入 |
| `workflow/` | 领域与持久化核心（32 个模块）：`repositories`、`state_machine`、`engine`、`providers`、`workspace`、`artifact_store`、`event_store`、`recovery`、`scheduler`、`retry_policy`、`garbage_collector`、`side_effect_log`、`data_safety`、`system_input*`、`textbook_catalog`、`platform_template_catalog` |
| `db/` | `migrations/0001..0012.sql`、`migration_runner.py`、`schema_checks.py` |
| `contracts/` | `openapi.yaml`（契约事实源）、`generated.ts`、`domain.ts`、`fixtures/workflow/` 状态夹具 |
| `question_types/` | 各题型解析器；注册表由 `question_model` 派生，唯一手写连接点是 `PARSERS_BY_FAMILY` |
| `question_model/` | 原子题目实体模型：身份与 revision 匹配、判定（adjudication）、音频投影与 TTS 适配 |
| `wordtts/` | 配音参数与音频装配：`config`、`tts_config`、`speakers`、`synthesis`、`composite_plan/cut`、`batch`、`progress`、`audio_io`、`xunfei_bridge` |
| `xunfei/` | 讯飞浏览器客户端：`session`（可见窗口持久登录）、`runtime`（单线程串行化 Playwright 调用）、`generation`、`downloads`、`signing`、`submission_tracker` |
| `platform_entry/` | 外部系统录入的页面自动化：`text_input`、`paper_input`、`adapter/` |
| `electron/` | 主进程与 Renderer；`main.js` 负责拉起后端与健康握手（要求后端契约版本 5），`workflow-proxy.js`/`workflow-event-transport.js`/`workflow-artifact-transport.js` 转发 API、SSE 与产物流 |
| `installer-prototype/` | Windows 自绘安装器（独立 Electron 工程 + 惰性 7z payload） |

`app_paths.py` 统一解析只读资源目录与可写数据目录：Electron 通过 `WORDTTS_DATA_DIR` 指定，源码直接运行时使用 `.runtime/`，打包态使用 `~/Library/Application Support/WordTTS` 或 `%APPDATA%\WordTTS`。

### 鉴权与并发约束

- 三层防护：主机/端口与 Origin 守卫（`ORIGIN_NOT_ALLOWED` 403）、非 `/api/v1` 的旧路径返回 410（`API_VERSION_RETIRED`，除非 `WORDTTS_LEGACY_API=1`）、`X-Desktop-Capability` 令牌常量时间比对；无令牌时退化为进程内随机 token，CORS 只允许 `null`。
- 所有写操作要求 `X-Idempotency-Key`，事件驱动重放；状态更新用 `state_version` 与 `draft_revision` 做乐观并发。
- SSE 端点 `GET /api/v1/workflows/{id}/events` 需要 `POST .../event-tickets` 换取一次性 `X-SSE-Ticket`（60 秒 TTL），支持 `Last-Event-ID` 续传与 `CursorExpired` 重新锚定。
- 只绑定 `127.0.0.1`。独立启动默认端口 `7863`（`DEFAULT_PORT`），Electron 实际使用动态端口，`contracts/openapi.yaml` 中的 `17321` 是文档占位。

## 工作流状态与数据

SQLite 位于数据目录下的 `workflow.db`（可用 `WORDTTS_WORKFLOW_DB_PATH` 覆盖），Artifact 在同目录 `artifacts/`；首次请求时惰性建库，导入模块不会触发迁移。WAL + `synchronous=FULL` + 外键开启，写事务 `BEGIN IMMEDIATE`，跨进程用 `.workflow.lock` 独占（`fcntl`，Windows 回退 `msvcrt`），文件 `0600`、目录 `0700`。迁移前自动备份，路径记录在 `WorkflowDatabase.last_migration_backup`。

workflow 状态由五个正交维度组合派生，UI 不得凭单个事件或计数判断终态：`result_status`、`execution_state`、`control_state`、`cleanup_state`，以及仅用于展示的 `status`。迁移分两个 profile：`2a` 停在 0004，`full` 应用全部 12 个迁移（服务端使用 `full`）。

```bash
python3 db/migration_runner.py --check --profile full
python3 db/schema_checks.py --profile full
python3 tools/verify_backup.py   # 备份与迁移摘要校验
```

## 题型解析

`python3 -m question_types` 批量解析示例文档：路径硬编码为输入 `examples/documents/`、输出 `examples/parsed/parsed_results.json`，只处理 `.docx`/`.xlsx` 并跳过 `~$` 临时文件，不接受命令行参数。`question_types/` 只保留解析器与手写注册表连接点，题型清单来自 `question_model/model.py` 的 `FAMILY_REGISTRY`。

解析回归基线在 `examples/baselines/parse/`，每次快照记录 `git_commit`、脏文件清单与各文档的 `sha256` 与解析结果：

```bash
python3 tools/parse_baseline.py capture --label <标签>
python3 tools/parse_baseline.py compare <旧基线> <新基线>
```

## 获取应用

正式安装包发布在 [GitHub Releases](https://github.com/asklins223/word-tts-desktop/releases)：

- macOS：`小猪wordTTS-<版本>-<架构>.dmg`
- Windows：`小猪wordTTS-Setup-<版本>-x64.exe`

## 本地开发

需要 Python 3.11（本地脚本最低接受 3.10）、Node.js 24（`>=24 <25`）与可用的 FFmpeg。

```bash
python3 -m pip install -r requirements_electron.txt
cd electron && npm ci && npm test && cd ..
./start_electron.sh
```

`start_electron.sh` 会解析虚拟环境里的 Python、按需补齐 Electron 与 Playwright Chromium，并启动未打包的应用。Windows 在装好依赖后从 `electron` 目录执行 `npm ci`、`npm test`、`npm start`。

契约与类型（`contracts/openapi.yaml` 为事实源）：

```bash
cd electron
npm run check:contracts    # Redocly lint + 重新生成 + diff + tsc
npm run generate:contracts # 显式更新 generated.ts
```

目前 `/system-input*`、`/textbook-catalog`、`/platform-template*`、`/workflows/{id}/recovery` 等约二十条路由尚未写入 `openapi.yaml`，改动这些路径时需同步补契约。

## 构建桌面安装包

macOS：`bash build_electron.sh`　Windows：`build_electron_windows.bat`

流程一致：环境自检 → 版本同步 → PyInstaller 按 `server_pyinstaller.spec` 打包后端（内含 `version.json`、`db/migrations`、`resources/voices.json` 与文档渲染 UMD 资产）→ 冻结之后再原样投放 Chromium（避免重签名破坏 bundle）→ 图标与 electron-builder → 自绘安装器（Windows）→ 后端 `--smoke-playwright` + 应用 `--smoke-test` 校验产物。构建不启动业务页面、不登录讯飞、不访问第三方页面；讯飞工作流用 `python3 tools/xunfei_smoke.py --logical-only ...` 做无页面逻辑验证，真实账号 smoke 不进入默认构建流。

macOS 另需 `CSC_LINK`/`CSC_KEY_PASSWORD` 才能 Developer ID 签名与公证；`WORDTTS_SKIP_PYTHON_DEPENDENCY_INSTALL=1` 可跳过依赖安装。版本发布、强制更新策略与 Release 资产要求见 [自动更新与发布流程](docs/auto-update.md)。

## 测试与发布闸门

```bash
python3 -m unittest discover -s tests -q   # 56 个测试文件
cd electron && npm test                    # node --test，27 个测试文件
```

Python 侧使用标准库 `unittest`（无 pytest 配置）。局部回归示例：

```bash
python3 -m unittest tests.test_desktop_server tests.test_xunfei_config tests.test_audio_assembly -v
```

闸门脚本：`tools/run_2a_gate.py`（本地确定性 2A 流程，含两种 schema profile，不产生真实供应商副作用）、`tools/release_gate.py`（发布边界不变量，默认严格，校验 `version.json`/`package.json`/`package-lock.json` 版本一致性）、`tools/performance_baseline.py`（有界 FakeProvider 基线）、`tools/process_recovery_probe.py`（子进程被杀后的恢复探针）。

CI 只有三个工作流，且仅在推送 `v*` 标签或手动派发时运行：`build-macos.yml`、`build-windows.yml`、`build-release.yml`（tag 时发布 GitHub Release）。所有构建环境固定 `WORDTTS_ENABLE_REAL_PROVIDER: '0'`。

## 常用环境变量

`WORDTTS_DATA_DIR`、`WORDTTS_PORT`、`WORDTTS_API_TOKEN`、`WORDTTS_WORKFLOW_DB_PATH`、`WORDTTS_ARTIFACT_ROOT`、`WORDTTS_ENABLE_REAL_PROVIDER`、`WORDTTS_AUTO_RETRY`、`WORDTTS_LEGACY_API`、`WORDTTS_SYSTEM_INPUT_ENABLED`、`WORDTTS_XUNFEI_ACCOUNT_SCOPE`、`WORDTTS_PLATFORM_INPUT_PROFILE_DIR`、`WORDTTS_BROWSER_PERF`、`WORDTTS_DEBUG_LOGS`。

## 数据与升级兼容

应用沿用原 `WordTTS` 用户数据目录、应用 ID、API Header 与环境变量，已生成历史不会丢失。`workflow.db` 在用户数据目录中，不在 `.app` 或安装目录里，覆盖安装不会删除它。固定数据目录不可访问时应用会停止启动并提示，不会退回到空的默认目录。讯飞版配置使用独立本地命名空间，旧版倍率/音色预设不会误套用。首次生成需要在讯飞配音浏览器窗口完成登录，登录态保存在本机持久化 profile 目录。

从旧版 JSON/会话目录切换到工作流 SQLite 时，旧文件保留但不自动导入；需要纳入新工作台时先用 `tools/import_legacy_readonly.py` 做 dry-run，再显式 `--apply`。`tools/backfill_legacy.py` 用于原子模型侧的补齐。

## 文档索引

- [DESIGN.md](DESIGN.md) / [PRODUCT.md](PRODUCT.md)：设计契约与产品事实
- [docs/workflow-spec.md](docs/workflow-spec.md)：工作流状态与恢复规格
- [docs/document-parsing-architecture.md](docs/document-parsing-architecture.md)：解析架构
- [docs/frontend-ui-redesign-plan.md](docs/frontend-ui-redesign-plan.md)：交互与产品方案
- [docs/system-input-integration-plan.md](docs/system-input-integration-plan.md)：音频生成与系统录入整合方案
- [docs/release-checklist.md](docs/release-checklist.md)、[docs/rollback-matrix.md](docs/rollback-matrix.md)、[docs/performance-baseline.md](docs/performance-baseline.md)

完整版本说明见 [CHANGELOG.md](CHANGELOG.md)。
