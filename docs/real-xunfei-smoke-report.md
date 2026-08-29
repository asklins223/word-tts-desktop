# 真实讯飞 smoke 报告

日期：2026-08-29（Asia/Shanghai）  
STATUS: BLOCKED  
SIDE_EFFECT_POLICY: NO_REAL_CALL

本机未执行真实讯飞请求。原因是当前请求没有提供经确认的真实账号、浏览器 profile、受控文档和真实副作用授权；没有把 FakeProvider 的结果冒充为讯飞结果。

说明：这只表示本报告没有执行真实 smoke，并不代表正式 App 关闭了能力。正式 App 和直接启动的后端默认开启真实调用；`--disable-real-provider` 或 `WORDTTS_ENABLE_REAL_PROVIDER=0` 才会显式离线，`--smoke-test` 始终强制离线。

无页面逻辑链路已单独通过：见 [`logical-xunfei-smoke-report.md`](logical-xunfei-smoke-report.md)。它使用 XunfeiTTSAdapter 的内存 backend，只验证工作流逻辑，不替代真实账号证据。

安全入口已实现，但必须同时满足以下条件才会进入真实调用：

```sh
WORDTTS_ENABLE_REAL_PROVIDER=1 \
WORDTTS_XUNFEI_ACCOUNT_SCOPE='<approved-account-scope>' \
python3 tools/xunfei_smoke.py \
  --confirm-real-side-effects \
  --source '<approved.docx-or-xlsx>' \
  --report docs/real-xunfei-smoke-report.json
```

执行前还必须完成 Node 24、账号/profile 隔离、预算和清理确认；提交不确定时只允许查询/人工对账，不自动重提。当前默认运行 `python3 tools/xunfei_smoke.py --report docs/real-xunfei-smoke-report.json` 只产生 BLOCKED 证据，不产生网络副作用。
