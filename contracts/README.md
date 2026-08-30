# Contracts

`openapi.yaml` 是公开 API 的唯一事实源；`generated.ts` 必须由它生成，`domain.ts` 只补充品牌 ID、状态机和 Electron 的窄接口。

当前契约版本为 `1.0.0-test`，面向本地单机工作流。它明确禁止旧的绝对路径、query token、`If-Match`/隐式条件版本和 EventSource JSON fallback：状态变更请求必须携带 body 中的 `expected_state_version`，目标命令还必须携带 typed target 与 `expected_target_state_version`。

导入使用 `source_import_id + generation`；服务端只返回状态，不暴露 staging 路径或 storage key。SSE 和 Artifact 内容分别使用 Header 中的一次性 ticket；长期 capability 只由主进程/Preload 持有。

从仓库根目录执行：

```sh
cd electron
npm ci --ignore-scripts
npm run check:contracts
```

`check:contracts` 会运行固定版本的 Redocly lint、重新生成到临时文件并比较 `contracts/generated.ts`，最后进行 TypeScript 类型检查。需要更新生成文件时才执行：

```sh
npm run generate:contracts
```

数据库 2A 使用 `python3 db/migration_runner.py --check --profile 2a` 和 `python3 db/schema_checks.py --profile 2a`；完整 schema（含阶段 6 外部录入表）使用 `--profile full`。生成文件、OpenAPI、DDL 和拆分文档必须在同一变更中同步.
