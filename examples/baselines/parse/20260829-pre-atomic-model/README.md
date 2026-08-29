# 解析基线快照说明

本目录是「原子小题模型改造」开始前，用**当前解析规则**对 `examples/documents/`
全部示例文档生成的解析结果存档，作为后续改造的回归对照基准。

- 生成工具：`tools/parse_baseline.py capture`
- 对照命令：`.venv/bin/python tools/parse_baseline.py compare <旧目录> <新目录>`
- 生成时代码状态：见 `manifest.json` 的 `git_commit` 与 `git_dirty_files`

## 每份文档存档内容

`docs/<文档名>.json`：

| 字段 | 含义 |
| --- | --- |
| `sha256` | 源文档内容哈希 |
| `parse_results` | 应用路径 `parse_document_auto`（内容识别，可多题型）完整结果 |
| `cli_result` / `cli_filename_type` / `cli_error` | CLI 路径（`detect_doc_type` 文件名识别）单题型解析结果 |
| `progress_items` | 旧链路 `wordtts/progress.py: build_progress` 派生的音频条目（id/category/seq/文件名/音色），已剔除运行时字段与时间戳 |

`manifest.json`：代码版本（commit、parser_version、audio_algorithm_version、
题型注册表顺序、默认 TTS 配置）、逐文档指纹与全量指纹。
指纹用键排序的紧凑 JSON 计算，可直接判断两次快照是否逐字节一致。

## 本次基线（20260829-pre-atomic-model）记录的已知异常

生成时全部 13 份文档的「内容识别路径」与「CLI 文件名识别路径」结果完全一致，
总计 465 条，与 git 内 `examples/parsed/parsed_results.json` 吻合。已知的
当前规则边界（不是回归，改造时如行为变化需显式说明）：

1. **`词汇-G7-u1.docx` 解析出 0 条**。该文档是 Word 版词汇表，格式为
   `（01） without /wɪðˈaʊt/ prep. 缺乏，没有` + `例句：…` + `翻译：…`。
   `词汇` 题型只注册了 `.xlsx` 扩展名、无内容标记，`detect_doc_type`
   返回 None，两条路径均不产出。若原子模型阶段开始抽取 Word 词汇，
   属于新能力而非回归，需在差异报告中标明。
2. **`信息转述及询问信息 7上- U1.docx` 仅产出 1 条**（整段录音稿），
   符合当前「信息转述只出录音稿、不拆任务」的规则边界。
3. 所有课文跟读文档的 `听选信息题目` 等编号题均未在其中出现，
   属内容识别按题型注册顺序的正常归属。
