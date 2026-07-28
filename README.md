# edge-tts-webui

`edge-tts-webui` 是 [edge-tts](https://github.com/rany2/edge-tts) 的 Gradio
Web 界面，支持多段落、背景音乐、多格式导出，以及 Remy → 788 的低延迟匹配预设。

![](Snipaste.png)

## 项目结构

```
edge-tts-webui-main/
├── word_parser/          # Word 文档解析工具（客户端部分）
│   ├── word_parser.py        # 解析核心库
│   ├── word_parser_app.py    # Gradio Web UI + pywebview 桌面应用
│   ├── WordParser.spec       # PyInstaller 打包配置
│   ├── build_mac.sh          # macOS 打包脚本
│   ├── build_windows.bat     # Windows 打包脚本
│   ├── word/                 # 输入 Word 文档目录
│   └── word_parsed/          # 解析结果输出目录
│
├── edge_tts/             # Edge TTS Web UI（音频生成部分）
│   ├── app.py                # Gradio Web UI 主应用
│   ├── voice_match_788.py     # 788 低延迟匹配 DSP
│   ├── style.css             # 界面样式
│   ├── example/              # 音色示例音频
│   ├── bgm/                  # 背景音乐
│   ├── outputs/              # 音频输出目录
│   └── voice_profiles/       # 788 匹配配置文件
│
├── voice_training/       # 声音训练与匹配部分
│   ├── calibrate_788_profile.py  # 788 频谱校准工具
│   ├── collect_788_training.py   # 训练数据采集
│   ├── prepare_788_corpus.py     # 训练语料生成
│   ├── validate_788_corpus.py     # 语料验证
│   ├── zero_shot_788.py          # 零样本声音克隆
│   ├── voice_clone_788.py        # RVC 声纹转换
│   ├── analyze_voice.py          # 声纹分析
│   ├── match_voices.py           # 声纹匹配（MFCC）
│   ├── match_voices_embed.py     # 声纹匹配（Speaker Embedding）
│   ├── match_voices_embed_v2.py   # 声纹匹配 v2
│   ├── match_voices_final.py     # 终极声纹匹配
│   ├── match_voices_grid.py      # 网格搜索匹配
│   └── datasets/                 # 训练数据集
│
├── ttsmaker/             # TTSMaker TTS 生成工具
│   ├── ttsmaker.py            # TTS 生成 v10
│   ├── ttsmaker_api.py        # 自动生成
│   ├── ttsmaker_direct.py     # 直接 API 调用
│   ├── ttsmaker_gui.py        # 交互式 GUI
│   ├── ttsmaker_requests.py   # requests 调用
│   └── ttsmaker_output/       # 输出目录
│
├── experiments/          # 实验脚本与中间产物
│   ├── _1to1_restore.py       # 1:1 还原实验
│   ├── _remy_finetune.py      # Remy 精细还原
│   ├── _verify_788*.py        # 788 验证脚本
│   ├── _scrape_ttsmaker*.py   # TTSMaker 爬虫
│   └── ...
│
├── tests/                # 单元测试
├── best_matches/         # 最佳匹配结果（共享数据）
├── ref_samples/          # 参考音频样本（共享数据）
├── 788.mp3               # 788 参考音频（共享数据）
├── 1480.mp3              # 1480 参考音频（共享数据）
├── voices_list.json      # Edge TTS 音色列表（共享数据）
└── ...
```

## 安装

先安装 FFmpeg：

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg
```

再安装 Python 依赖：

```bash
pip install -r requirements_app.txt
```

788 完整匹配要求 FFmpeg 带有 `rubberband`、`firequalizer` 和 `alimiter`
滤镜；缺少时界面会给出明确错误，不会静默换成不同效果。

## 运行

### Edge TTS Web UI

```bash
python edge_tts/app.py
```

浏览器访问：

```text
localhost:7860
```

### Word 文档解析工具

```bash
python word_parser/word_parser_app.py
```

或打包为桌面应用：

```bash
bash word_parser/build_mac.sh        # macOS
word_parser\build_windows.bat        # Windows
```

## 788 实时匹配

1. 展开"788 音色匹配"，勾选"启用 788 极速匹配"。界面会自动选择
   `fr-FR-RemyMultilingualNeural`。
2. 普通"生成"会在合成后、混入背景音乐前应用同一套匹配处理，并提供完整格式下载。
3. "边生成边播放 788 试听"会消费 Edge 的 MP3 增量流，经一个常驻 FFmpeg
   进程逐块解码和调整，再以独立 WAV 块回传，适合快速试听；完成后另行编码一份完整
   MP3 下载。实时试听使用引擎自然停顿；需要精确段间停顿或背景音乐时使用普通"生成"。
4. "匹配强度"默认 100。若听到处理感，可降到 70–90。

当前预设是从仓库中的 `788.mp3` 与同文本 Remy 样本校准的低延迟 DSP。它会改善
音高色彩和长期频谱，但不是训练好的声纹克隆模型，不能把单样本声学指标当作
"99% 身份相同"。当前参考与配置均按英语校准；其他语言可以处理，但口音与相似度
不作保证。高相似路线、数据要求和验收方法见
[`788_REALTIME_PLAN.md`](788_REALTIME_PLAN.md)。

只在需要重新生成校准配置时安装分析依赖：

```bash
pip install -r requirements_calibration.txt
```

第二阶段训练语料的固定文本、交付格式与自动质检说明见
[`voice_training/datasets/788/README.md`](voice_training/datasets/788/README.md)。

## 测试

```bash
python -m unittest discover -s tests -v
```
