# Workflow workspace fixtures

这些 JSON 是 Renderer 的状态投影夹具，不是伪造的 Provider 运行记录。它们覆盖 `frontend-ui-redesign-plan.md` §13.0 要求的七组主场景和五组边界场景；每个文件都包含完整的五维工作流状态、条目分桶、Provider 投影、可用动作和交付范围。

自动检查入口：

```bash
node --test electron/test/workflow-fixtures.test.js
```

夹具中的 `expected` 只用于测试投影结果，不会进入生产 API 响应。真实讯飞登录、浏览器外部提交、300MB 可解析 `.docx`、大音频/ZIP 传输和三档窗口指标仍必须按文档 §16.8 在现场采集；夹具不能替代这些证据。
