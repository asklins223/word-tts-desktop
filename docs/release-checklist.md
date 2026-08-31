# 发布放行清单

发布门禁：`PATH="/opt/homebrew/opt/node@24/bin:$PATH" python3 tools/release_gate.py --output docs/release-gate-strict-report.json`。默认命令遇到开放项返回非零并阻止放行；`--allow-open` 只用于本地代码验收，不能改写支持矩阵。

| 检查 | 当前状态 | 说明 |
| --- | --- | --- |
| package/package-lock 同版本 | PASS | 唯一手工版本源为 `version.json`，构建前自动同步 Electron package/package-lock。 |
| DATA_FORMAT_VERSION | PASS | 文件与 `workflow.version` 一致。 |
| Renderer/Preload/Proxy 禁止旧 token、路径和 EventSource | PASS | 静态扫描通过。 |
| Electron single instance | PASS | 主进程请求 single-instance lock。 |
| `/api/v1` 启动探测与 capability | PASS | 旧 `/api/*` 生产模式 410。 |
| Python 307 / Electron 70 / contracts | PASS | 2.7.44 修复后全量 Python 回归、Electron 回归和契约检查通过；Electron/构建使用 Node 24.20.0。 |
| Node.js 24 | PASS | 本轮使用 Node 24.20.0 重跑 Electron、契约和 macOS 构建；系统默认 Node 22 不作为本轮项目验证入口。 |
| 讯飞逻辑 smoke | PASS（无页面/无网络） | 真实导入、解析、composite_cut、receipt、Artifact 和状态收敛已通过；`network_calls=0`、`page_calls=0`。 |
| 真实讯飞 composite_cut smoke | OPEN | 当前报告为 BLOCKED，未产生真实副作用。 |
| 目标设备性能模型 | PASS（模拟） | 已按 CPU 降速、I/O 延迟和内存预算完成模型模拟；不替代目标设备现场证据。 |
| 最低支持设备性能/fsync | OPEN | 仍需要目标设备现场证据。 |
| macOS arm64 打包 | PASS（ad-hoc） | [`小猪wordTTS-2.7.44-arm64.dmg`](../electron/release/小猪wordTTS-2.7.44-arm64.dmg) SHA-256=`0f0ee98d92aa5d2f56a28ff7485e6faca363a110e2e2e5b6cd14e45f29df194d`；后端 Playwright/Chromium、Electron `--smoke-test`、DMG 校验和代码签名验证通过。未配置 Developer ID/公证。 |

只有全部 OPEN 项补齐且重新运行默认严格门禁成功，才能把 `release_ready` 视为 true。发布前还需保存真实 smoke 的清理记录、备份验证结果、构建产物哈希和目标设备报告。
