# 小猪wordTTS

![小猪wordTTS 应用图标](electron/renderer/assets/app-icon.png)

小猪wordTTS 是一款面向教学 Word 文档的本机配音工作台。应用会解析 `.docx` 内容，按文档标记自动分配男女声，生成可试听、可单独保存或打包下载的音频，并把最近完成的任务保存在当前电脑中。

当前正式版本为 **2.7.45（流式拖拽导入、Store 进度投影与停顿快速路径）**。

## 主要能力

- 四步桌面流程：导入文档、核对与设置、生成音频、试听与下载。
- 统一使用讯飞配音：`w / W` 使用英语-Amanda 女声，`m / M` 使用英语-George 男声；无标识内容默认使用英语-Amanda。
- 讯飞语速、语调、音量均支持 0-100 任意整数值（默认 50），另可配置输出格式与质量并保存常用预设；讯飞返回的 MP3 首尾音频保持原样。
- 支持 MP3、OGG、AAC、OPUS、WAV 输出，提供逐条试听、单文件保存和 ZIP 打包保存。
- 本机历史中心最多保留最近 20 个任务，可重新查看、试听、下载或删除。
- 生成过程提供结构化时间线、断线恢复、取消与失败重试信息。
- macOS 与 Windows 桌面安装包均由 GitHub Actions 自动构建。
- 正式桌面 App 默认开启真实讯飞调用，双击安装包即可使用；`--smoke-test` 始终只走逻辑离线流程，不打开真实页面。直接诊断后端时可用 `--disable-real-provider` 或 `WORDTTS_ENABLE_REAL_PROVIDER=0` 显式离线。

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

构建流程会打包 Python 后端、Playwright Chromium 和 Electron 前端，并执行本地后端健康检查与产物文件校验；不启动业务页面、不登录讯飞、不访问第三方页面。讯飞工作流使用 `python3 tools/xunfei_smoke.py --logical-only ...` 做无页面逻辑验证，真实账号 smoke 不进入默认构建流。

## 项目目录约定

- `question_types/` 按题型保留解析器源码与注册表；可运行 `python3 -m question_types` 批量解析示例文档，示例输入放在 `examples/documents/`，示例解析结果放在 `examples/parsed/`。
- `resources/voices.json` 是打包进应用的音色种子目录；在线刷新后的可写缓存位于用户数据目录的 `cache/voices.json`。
- `app_paths.py` 统一解析只读资源目录和可写数据目录。Electron 通过 `WORDTTS_DATA_DIR` 指定用户数据位置，源码直接运行时使用 `.runtime/`。

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

应用沿用原 `WordTTS` 用户数据目录、应用 ID、API Header 和环境变量；已生成历史不会丢失。工作流 SQLite 位于该目录下的 `workflow.db`，不在 `.app` 或安装目录中，覆盖安装新版本不会删除它。若固定数据目录不可访问，应用会停止启动并提示，不会退回到空的默认目录。讯飞版配置使用独立的本地存储命名空间，旧版倍率/音色预设不会误套用。首次生成时需要在讯飞配音浏览器窗口完成登录，登录状态会保存在本机。

从旧版 JSON/会话目录切换到工作流 SQLite 时，旧文件会保留，但不会在每次启动时自动导入；如需把旧任务纳入新工作台，请先用 `tools/import_legacy_readonly.py` 做 dry-run，再显式执行 `--apply`。

完整版本说明见 [CHANGELOG.md](CHANGELOG.md)。
