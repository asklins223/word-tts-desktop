# 788 v1 训练语料交付说明

这一目录用于接收 788（Alfie）原始音频。请先使用
[`prompts/788_corpus.tsv`](prompts/788_corpus.tsv) 中的锁定文本生成音频，再把原始
文件放入 `inbox/`。不要对交付音频做降噪、EQ、变调、拉伸、响度归一化或二次转码。

## 这次需要提供什么

请生成全部 480 条，但首次交付必须采用“两包制”：

- 首批语料包：`train` 400 条，用于模型和检索索引；`validation` 40 条，只用于选模型与调参；
- 封存测试包：`test` 40 条，完全独立保存，最后一次盲测才可使用。

按文本长度估算，train+validation 约 39–48 分钟，全部 480 条约 42–51 分钟。若实际
有效语音不足 40 分钟，完成首轮质检后再生成补充包。最小首版必须包含全部
validation、至少 220 条 train，且两者合计达到 30 分钟有效语音；少于这个规模只能
验证流水线。test 暂不交付并由你封存。

现在只交付 `tr_` 和 `va_`，把全部 `te_` 音频单独打包并由你保管；等模型、阈值和
后处理参数冻结后，我再请你提供该包。这样 test 的目标音频不会进入训练、检索索引
或调参过程。请在封存后先执行
`shasum -a 256 788_test_v1.zip`，现在只把 64 位 SHA-256 告诉我；不要重新压缩该
ZIP，否则哈希会变化。

## 文件要求

1. 每个文件只包含对应 ID 的一句话，文件名必须是 `<id>.<扩展名>`，例如
   `tr_0001.wav`、`va_0001.mp3`、`te_0001.flac`。
2. 优先提供引擎能导出的最高原生质量：单声道 WAV/FLAC、16 或 24 bit。若服务只能
   下载 MP3，就提供原始 MP3；不要先转成 WAV，也不要多次压缩。
3. 全部使用 788 的同一套默认设置：相同语速、音调、音量、风格和采样选项。
4. 每句建议 2–10 秒，最多 12 秒；句首和句尾保留约 100–250 ms 安静区。
5. 不得含背景音乐、提示音、混响、环境声、削波或被截断的首尾辅音。
6. 数字、日期、缩写和符号句需抽听。若引擎实际读法与文本明显不一致，请单独列出
   ID 和实际读法，不要自行剪接修补。
7. 保留下载得到的原文件和时间信息，并填写
   [`SOURCE_AND_RIGHTS_TEMPLATE.json`](SOURCE_AND_RIGHTS_TEMPLATE.json)，另存为
   `SOURCE_AND_RIGHTS.json`。只有 `training_authorized: true`、
   `postprocessed: false` 且签名信息完整时才能进入训练。

支持的原始文件格式：WAV、FLAC、MP3、M4A、AAC、OGG、OPUS。

## 交付方式

最方便的方式是先把 440 条 train/validation 按下面结构打包为
`788_train_validation_v1.zip` 后提供：

```text
788_corpus_v1/
  SOURCE_AND_RIGHTS.json
  audio/
    tr_0001.wav
    tr_0002.wav
    ...
    va_0001.wav
    ...
    va_0040.wav
```

40 条 test 请另存为 `788_test_v1.zip`，首次交付时不要把这个 ZIP 或其中任何音频放
进项目，只提供它的 SHA-256：

```text
788_test_v1/
  audio/
    te_0001.wav
    ...
    te_0040.wav
```

也可以直接把音频放进本项目的 `datasets/788/inbox/`。文件可以位于子目录中，只要
文件名 ID 不重复即可。

## 本地质检

随到随检：

```bash
python3 validate_788_corpus.py --level partial
```

检查 30 分钟最低交付：

```bash
python3 validate_788_corpus.py --level minimum
```

检查完整推荐包：

```bash
python3 validate_788_corpus.py --level recommended
```

模型参数冻结、test 揭封后检查全部 480 条：

```bash
python3 validate_788_corpus.py \
  --level complete \
  --frozen-run datasets/788/runs/frozen_run.json \
  --sealed-test-package /path/to/788_test_v1.zip \
  --reveal-test
```

不要把 `te_*` 解压或复制进 `datasets/788/inbox/`。complete 阶段会直接从
`--sealed-test-package` 指定的、哈希已冻结的 ZIP 中读取 40 条 test；ZIP 是唯一
允许的 test 音频来源。冻结记录还必须列出 approved train manifest、模型、检索
index、配置、阈值和评估器包各自的实际文件路径及 SHA-256，验收器会重新计算每个
文件的哈希，任何一项变化都会阻止 test 解码。

工具只在内存中解码测量，不修改原音频；报告写入
`datasets/788/reports/quality_report.json`。这一步只是信号预检，不会把音频自动
判定为可训练数据。收到语料后还会继续做文本/ASR 对齐、788 身份一致性、噪声/混响
检测和人工抽听。因此“信号预检通过但训练准入尚未完成”会返回退出码 `2`；只有完整
训练准入流程通过后才会返回 `0`，避免自动化把一个文件名正确的错误声音误收进训练。

## 盲测隔离

`validation` 和 `test` 不得复制进 train，也不得加入 RVC 检索 index。
validation 可用于选择 epoch、F0 方法、index rate、分块和后处理参数；test 在这些
设置冻结后只运行一次。后续会为 test 生成完全同文本的 Remy 输入，以测量声纹、
F0、MCD、WER、自然度和实时延迟。

这 40 条 test 足够做首版模型门禁，但不足以对“真实通过率至少 99%”给出有意义的
统计置信结论。只有首版质量接近目标后，才值得再生成一个由你保管的至少 299 条最终
验收包；它不会进入训练或日常调参。

若已有的是其他文本或长录音，也可以原样提供，同时附准确转录或时间戳；收到后会另做
切分和 manifest。但固定语料表的结果最容易复现和公平验收。
