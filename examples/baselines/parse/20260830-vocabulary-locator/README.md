# 基线：词汇条目行级定位（有意变更，2026-08-30）

对照 20260830-retelling-asking-tasks 的**有意差异**：

- `U6单词导入模板.xlsx` 的词汇条目新增 `source_locator`
  （`工作表/Sheet1/行/N/单词|例句`，Excel 行级定位，方案 9.1）；
  词汇解析器同时引入表头精确匹配优先的列识别（`_find_header_column`）。
- 该变更由并行开发引入，随 d43b7e8 一并入库；`items` 数量与既有字段
  不变，progress 投影指纹不受影响（PROGRESS_ITEM_FIELDS 白名单）。
- 其余 12 份文档逐字节一致。

流程备注：d43b7e8 曾以 `git add question_types/` 宽泛暂存误打包此
并行改动，此后恢复逐文件暂存纪律。
