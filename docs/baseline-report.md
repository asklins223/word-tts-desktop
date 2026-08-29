# T0 基线报告

日期：2026-08-28（Asia/Shanghai）  
基线提交：`6c507f6b2cabd8b7e3bfc2b4d94f6f0487fbc2a6`；当前实现为未提交工作树，不能把工作树改动冒充该提交内容。  
目标：为契约、迁移和工作流运行时提供可重复的回退点。

## 运行时与依赖

| 项目 | 实测值 | 说明 |
| --- | --- | --- |
| Python | 3.12.14 | 既有 Python 测试环境 |
| Node | v24.20.0（项目验证） / v22.23.2（系统默认） | Node 24 已通过项目级测试；默认 PATH 仍保留 Node 22 |
| npm | 11.19.0（Node 24 项目验证） / 10.9.8（系统默认 Node 22） | 锁文件已用 `npm ci` 验证 |
| SQLite | 3.51.0 | WAL/FULL/busy_timeout 可用 |
| Electron | 43.2.0 | `electron/package.json` 固定版本 |
| Redocly CLI | 2.49.0 | 固定 devDependency |
| openapi-typescript | 7.13.0 | 固定 devDependency |
| TypeScript | 5.9.2 | 固定 devDependency |

## 基线命令

- `python3 -m unittest discover -s tests -p 'test_*.py' -q`：234 passed。
- `(cd electron && npm test)`：42 passed（27 个顶层/嵌套 node:test 用例）。
- `(cd electron && npm run check:contracts)`：lint、生成结果一致性和类型检查通过；`contracts/generated.ts` 当前 1493 行。
- `python3 tools/process_recovery_probe.py`：真实子进程 `SIGKILL` 后恢复为 `AMBIGUOUS + NEEDS_RECONCILE`，Provider submit calls 为 0。
- `PATH="/opt/homebrew/opt/node@24/bin:$PATH" (cd electron && npm test)`：Node 24.20.0 下 Electron 测试通过。
- `python3 tools/xunfei_smoke.py --logical-only --source "examples/documents/信息转述及询问信息 7上- U1.docx"`：导入→解析→XunfeiTTSAdapter→composite_cut→Artifact 逻辑链路通过，网络/页面调用均为 0。
- `python3 tools/performance_baseline.py --simulate-target --items 3 --runs 2`：目标设备模型模拟通过；仅为模拟证据。
- `PATH="/opt/homebrew/opt/node@24/bin:$PATH" bash build_electron.sh --electron`：macOS arm64 PyInstaller + Electron Builder + ad-hoc 签名 + DMG 产物校验通过；构建过程未调用真实 Provider。
- `python3 db/migration_runner.py --check --profile 2a` 与 `python3 db/schema_checks.py --profile 2a`：通过，0004/33 tables。
- `python3 db/migration_runner.py --check --profile full` 与 `python3 db/schema_checks.py --profile full`：通过，0005/37 tables。

## 已选默认边界

- SQLite 使用 WAL、`synchronous=FULL`、`busy_timeout=5000ms`；迁移使用 `BEGIN IMMEDIATE`。
- 2A 只允许单机、单账号和一个活动 Provider 执行租约；队列必须有界，不能以无限内存队列承载事实。
- 迁移 `--check` 对显式数据库只读打开，对默认路径只使用临时库；不会修改被检查的运行时数据库。
- 外部系统真实账号、真实讯飞、最低支持设备阈值和平台差异 fsync 尚未在本机完成现场测量，继续标记为 `OPEN`，不作为已支持能力；Node 24 已完成本机项目级验证，本地实现与 FakeProvider/FakeExternal 证据不替代现场验收。

## 工作树说明

本次新增的 contracts/db/docs/scripts 文件是实施产物；`examples/documents/` 下已有的四个未跟踪示例文档视为用户资料，没有修改、迁移或删除。未提交变更仍可整体回退到上述基线提交。
