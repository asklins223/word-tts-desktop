# 基线：听后选择业务字段通道（有意变更，2026-08-30）

对照 20260829-pre-atomic-model 的**有意差异**（方案阶段3⑤）：

- `听后选择-7上 Starter Unit 1 Hello!.docx` 的 parse_results 新增
  `questions` 键：8 道题的题干+A/B/C 选项，按 `script_ordinal` 归属
  到其后录音稿；`items`（音频通道）逐字节不变——TTS 流零影响。
- 其余 12 份文档逐字节一致。
- 文档无答案标记 → `answer=None`，`question_fields_complete=False`，
  小题保持 DRAFT 不进外部链路（方案 5.2.2）。

生成：`tools/parse_baseline.py capture --label 20260830-listening-choice-questions`
