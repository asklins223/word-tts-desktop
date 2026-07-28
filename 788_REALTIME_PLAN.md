# 788（Alfie）实时匹配方案

## 结论

项目现在提供了一条可直接使用的低延迟路径：

```text
Edge Remy MP3 增量流
  -> 常驻 FFmpeg 解码
  -> 16 kHz PCM
  -> 轻微音色位移
  -> 校准频谱包络
  -> 限幅
  -> 独立 WAV 块
  -> Gradio 增量播放 + 完整 MP3 下载
```

这条路径适合“生成过程中马上听到调整结果”。本机使用已有 5.3 秒同文本样本校准时，
Resemblyzer speaker-embedding cosine 从约 0.757 提升到约 0.872（完整批处理）
或约 0.874（常驻 FFmpeg 实时路径），20 维平均 MFCC cosine 从约 0.979 提升到
约 0.995。它们只是单句诊断值，不是百分比，也不是对新文本
的保证。

仅用音高、EQ、压缩或共振峰调整，无法把 Remy 的发音方式、连读、重音和说话人身份
变成 788。因此“尽量冲击 99%”必须采用第二层声线转换模型，并在未参与训练的文本上
验收。

## 已落地的第一层：极速匹配

- 固定源音色：`fr-FR-RemyMultilingualNeural`。
- 参考音频：根目录 `788.mp3`，实际文本为：
  `TTSMaker is a free text-to-speech tool that provides speech synthesis services.`
- 校准配置：`edge_tts/voice_profiles/788_remy.json`。
- 可复现校准工具：`voice_training/calibrate_788_profile.py`。它仅离线需要 `numpy`、`librosa`
  和 `scipy`，WebUI 运行时不需要这些分析库。
- 批处理入口：`edge_tts/voice_match_788.py` 中的 `process_audio_segment()`。
- 真流式入口：`edge_tts/voice_match_788.py` 中的 `stream_edge_tts_788_pcm()`；每个 PCM 块会包装成
  独立可解码的 WAV 后交给 Gradio，避免把任意 MP3 字节切片误当成音频文件。
- UI 同时提供完整生成和边生成边播放。两条路径共用同一套 FFmpeg 滤镜图，避免试听
  与下载音色不一致。
- 强度 0–100 可连续调整；100 对应当前校准曲线。

重新生成一份配置时，先保证两条音频文本完全相同，再运行：

```bash
pip install -r requirements_calibration.txt

python voice_training/calibrate_788_profile.py \
  --target 788.mp3 \
  --source experiments/_tmp_1to1/tts_raw.mp3 \
  --output edge_tts/voice_profiles/788_remy.generated.json
```

工具默认拒绝覆盖已有文件；明确需要覆盖时才使用 `--force`。

频谱曲线使用 125 Hz 间隔的长期 log-spectrum 差，经过平滑和增益限制后固化。
处理时不复制参考句子的逐帧频谱，也不把任意文本强行拉伸到 5.3 秒，所以可用于不同
文本，并保持流式状态。

## 第二层：高相似实时 VC

### 数据

第二阶段的锁定语料包已经生成：

- `voice_training/datasets/788/prompts/788_corpus.tsv`：480 条、预计 41.4–51.0 分钟；
- 400 条 train、40 条 validation、40 条封存 test；
- `voice_training/prepare_788_corpus.py`：确定性重建语料表和哈希；
- `voice_training/validate_788_corpus.py`：检查缺失、重复、格式、时长、静音、电平、削波和
  SHA-256，不修改原始音频；
- `voice_training/datasets/788/README.md`：文件命名、原生格式和交付方式。

用户提供的 788 音频应放入 `voice_training/datasets/788/inbox/`，并确认音色授权和来源服务条款。
旧的 `voice_training/collect_788_training.py` 只有约 6 分钟文本且元数据/QC 不足，不作为本轮正式
采集入口。当前目录仍没有可用的 788 `.pth`、`.index` 或 `.onnx` 模型。

### 模型与实时参数

先在隔离环境对同一小模型做 CPU、可用时的 MPS、ONNX/CoreML 路径赛马，再确定生产
后端；当前机器不能仅凭 M4 硬件就假设 PyTorch MPS 可用。RVC v2 40k + RMVPE
作为首个训练候选，但必须以实测延迟和质量决定是否进入应用：

- 预加载模型和检索索引，不能每个音频块重新加载；
- 160–240 ms 推理块；
- 保留左侧上下文，并做 30–50 ms overlap/crossfade；
- 在 M4 上分别测试可用后端，只有实时因子 `RTF < 1` 才启用高相似模式；
- 建议门槛：本地转换 p95 不超过 250 ms，发生积压时自动降级到极速 DSP。

接入位置保持为：

```text
Edge stream -> FFmpeg raw PCM decoder -> ring buffer
  -> [DSP fallback | RVC worker]
  -> shared polish/limiter -> streaming player + download accumulator
```

VC 入口必须使用尚未套用 788 频谱曲线的原始 Remy PCM；当前完整 DSP 只作为 fallback，
否则会发生双重染色和提前丢失高频。

## “99%”的可验收定义

不要把 cosine 乘 100 后称为匹配率。建议把目标定义为：

> 在锁定的未见测试集上，至少 99% 的转换片段通过预先校准的目标说话人阈值，同时该
> 阈值在非目标声音上的误接受率不超过 1%。

至少同时检查：

- 两个独立说话人验证器（例如 ECAPA-TDNN 与 WavLM/x-vector）；
- 转换前后 WER 增量不超过 1–2 个百分点；
- F0 RMSE（cents）、V/UV F1、DTW 对齐后的 MCD；
- 盲听 speaker-similarity MOS / ABX 与自然度 MOS；
- 首包时间、转换 p50/p95、RTF、长文本断流和爆音次数。

只有锁定测试集通过后才报告结果。当前一条 5.3 秒参考既不能校准 1% 误接受率，也不能
覆盖新文本，所以现阶段不能诚实承诺 99%。

## 推荐执行顺序

1. 先用现有极速匹配收集主观反馈，重点听齿音、鼻音、低频厚度和长句疲劳感。
2. 采集 30–60 分钟合法的 788 语料，建立固定的训练/验证/测试划分。
3. 训练 RVC，先离线达到身份与可懂度门槛，再改为 160–240 ms 分块推理。
4. 将 VC worker 接入现有 PCM 流，保留当前 DSP 作为降级路径。
5. 用锁定测试集和盲听完成验收；达不到门槛时继续补充音素覆盖，而不是继续针对
   `788.mp3` 单句过拟合。
