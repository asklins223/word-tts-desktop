# 小猪wordTTS

![小猪wordTTS 应用图标](electron/renderer/assets/app-icon.png)

小猪wordTTS 是一款面向教学 Word 文档的本机配音工作台。应用会解析 `.docx` 内容，按文档标记自动分配男女声，生成可试听、可单独保存或打包下载的音频，并把最近完成的任务保存在当前电脑中。

当前正式版本为 **2.4.1（修复 Understanding Idea 句子跟读缺失）**。

## 主要能力

- 四步桌面流程：导入文档、核对与设置、生成音频、试听与下载。
- 统一使用讯飞配音：`w / W` 使用 Amanda 女声，`m / M` 使用 George 男声；无标识内容默认使用 Amanda。
- 讯飞语速、语调、音量均支持 0-100 任意整数值（默认 50），另可配置输出格式与质量并保存常用预设；讯飞返回的 MP3 首尾音频保持原样。
- 支持 MP3、OGG、AAC、OPUS、WAV 输出，提供逐条试听、单文件保存和 ZIP 打包保存。
- 本机历史中心最多保留最近 20 个任务，可重新查看、试听、下载或删除。
- 生成过程提供结构化时间线、断线恢复、取消与失败重试信息。
- macOS 与 Windows 桌面安装包均由 GitHub Actions 自动构建。

## 获取应用

正式安装包会发布到 [GitHub Releases](https://github.com/asklins223/word-tts-desktop/releases)：

- macOS：`小猪wordTTS-<版本>-<架构>.dmg`
- Windows：`小猪wordTTS-Setup-<版本>-x64.exe`

## 本地开发

需要 Python 3.11、Node.js 24，以及可用的 FFmpeg。

```bash
python3 -m pip install -r requirements_electron.txt
cd electron
npm ci
npm test
cd ..
./start_electron.sh
```

Windows 可在安装依赖后从 `electron` 目录运行：

```powershell
npm ci
npm test
npm start
```

## 构建桌面安装包

macOS：

```bash
bash build_electron.sh
```

Windows：

```bat
build_electron_windows.bat
```

构建流程会打包 Python 后端、Playwright Chromium 和 Electron 前端，并执行后端健康检查与桌面界面端到端冒烟测试。

## 测试

```bash
python3 -m unittest tests.test_desktop_server tests.test_xunfei_config tests.test_audio_assembly -v
cd electron && npm test
```

完整 Python 测试：

```bash
python3 -m unittest discover -s tests -v
```

## 数据与升级兼容

应用沿用原 `WordTTS` 用户数据目录、应用 ID、API Header 和环境变量；已生成历史不会丢失。讯飞版配置使用独立的本地存储命名空间，旧版倍率/音色预设不会误套用。首次生成时需要在讯飞配音浏览器窗口完成登录，登录状态会保存在本机。

完整版本说明见 [CHANGELOG.md](CHANGELOG.md)。
