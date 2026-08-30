# 基线：信息转述/询问信息任务拆分（有意变更，2026-08-30）

对照 20260830-listening-choice-questions 的**有意差异**（方案阶段3⑤c）：

- `信息转述及询问信息 7上- U1.docx` 的 parse_results 新增 `tasks` 键：
  转述参考答案 + 询问信息两个问题（中文提示 + 英文参考应答）；
  `items`（音频通道）逐字节不变。
- `asking_info` 小题型激活（reserved → active，answer_kind=spoken_response）；
  询问信息小题字段完整（stem+answer，无选项——按注册表能力判定），
  `resolution_state=CANDIDATE` 待人工确认。
- 其余 12 份文档逐字节一致。

生成：`tools/parse_baseline.py capture --label 20260830-retelling-asking-tasks`
