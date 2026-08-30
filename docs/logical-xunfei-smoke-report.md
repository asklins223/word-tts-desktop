# 讯飞逻辑 smoke 报告

日期：2026-08-28（Asia/Shanghai）  
STATUS: PASS  
SIDE_EFFECT_POLICY: LOGICAL_ONLY_NO_NETWORK

本次 smoke 使用真实的导入、解析、WorkflowApplicationService、`XunfeiTTSAdapter`、composite_cut、receipt、Artifact 和最终状态收敛逻辑；Provider backend 是内存确定性实现，不打开浏览器页面、不读取账号 Cookie、不访问网络、不产生讯飞副作用。

原始 JSON：[`logical-xunfei-smoke-report.json`](logical-xunfei-smoke-report.json)

输入使用仓库内已跟踪的 `examples/documents/信息转述及询问信息 7上- U1.docx`，因此该报告不依赖个人未跟踪文档。

关键结果：

- `network_calls=0`
- `page_calls=0`
- `real_calls_enabled=false`
- `backend_submit_calls=1`
- 1 个输入条目成功生成 composite 与 segment Artifacts

该报告证明代码逻辑链路，不替代真实账号 smoke；真实账号报告仍保持 `BLOCKED`，以避免打包流或测试环境触发真实页面。
