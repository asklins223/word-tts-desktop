# 基线：词汇原子候选贯通行级定位（有意变更，2026-08-30）

对照 20260830-vocabulary-locator 的**有意差异**：

- 词汇 ContentUnit 的 source_locator 从自造定位（`单词/词条N`）改为
  优先采用解析器提供的 Excel 行级定位（`工作表/Sheet1/行/N/单词|例句`），
  content_unit_id 随之变化；旧解析结果（无该字段）回退自造定位。
- 这是第四轮审查修复：此前抽取器丢弃了解析器提供的行级定位，
  违反方案 9.1（定位精确回溯原文）。
- 其余 12 份文档逐字节一致。

生成：`tools/parse_baseline.py capture --label 20260830-vocabulary-model-locator`
