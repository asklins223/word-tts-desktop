# 小猪wordTTS 自动更新与发布流程

## 目标与边界

桌面包发布到 GitHub Releases，不增加自建更新服务器。Windows 和 macOS 使用两条明确分开的更新链路：

- Windows：使用独立的 HTML 自绘 `Setup.exe` 完成首次安装、更新和卸载；客户端读取 `latest-win.json`，下载并校验新的 Setup.exe，再启动它的 `update` 模式。
- macOS：继续使用 `electron-updater` 和 `latest-mac.yml`；DMG 负责首次安装，ZIP 负责自动更新。
- 开发模式和 `--smoke-test` 不访问更新服务。
- 当前 macOS Actions 按 runner 架构构建一个包；如果要同时覆盖 Intel 与 Apple Silicon，应增加对应架构构建并把两个 ZIP 一起放进同一个 macOS 元数据流程。

Windows 不再使用 `electron-updater`、`latest.yml` 或系统安装页面。这样安装器的视觉、交互、安装/卸载文案和流程都由 `installer-prototype/` 控制。

## Windows 结构

```text
electron/release/win-unpacked/       Electron 应用目录
                │
                ▼
scripts/build_windows_installer.js   把应用目录嵌入独立的 Setup.exe
                │
                ▼
小猪wordTTS-Setup-<version>-x64.exe  用户分发包
```

Windows Setup 是一个短生命周期的 Electron 包，运行时把 `payload` 写入用户选择的目录，并保存 `install-state.json`。已安装目录中会留下同一套自绘程序作为 `小猪wordTTS-uninstaller.exe`，因此卸载也不会回到原生页面。卸载启动后会先把该可执行文件迁移到系统临时目录，再由临时副本删除安装目录，避免 Windows 对正在运行的卸载器和工作目录持有锁定。

关键文件：

| 文件 | 职责 |
| --- | --- |
| `installer-prototype/index.html` / `styles.css` / `app.js` | 安装、更新、卸载共用的自绘 UI 和静态交互。 |
| `installer-prototype/installer-main.js` | Electron 窗口、IPC、UAC 提权和自动启动。 |
| `installer-prototype/installer-service.js` | 文件替换、快捷方式、注册表、数据保留和卸载清理。 |
| `scripts/build_windows_installer.js` | 先构建应用目录，再生成独立 Setup.exe。 |
| `electron/windows-update-client.js` | Windows 更新元数据、下载、大小/SHA-512 校验和 Setup 启动。 |
| `electron/update-manager.js` | 统一更新状态机；Windows 走上面的客户端，macOS 才加载 `electron-updater`。 |
| `scripts/prepare_update_metadata.js` | Windows 生成 `latest-win.json`，macOS 生成 `latest-mac.yml`。 |

安装状态会记录版本、安装位置、范围、快捷方式和应用数据目录。更新只替换应用文件，不删除用户数据；卸载默认保留个人设置和历史任务，可选择删除缓存或全部个人数据。

## Windows 更新流程

1. 已安装的应用读取 GitHub Releases 的 `latest-win.json`。
2. 客户端确认版本高于当前版本，并确认元数据包含当前版本的 `.exe`、正数文件大小和 SHA-512。
3. 客户端下载 Setup.exe 到临时目录，流式计算 SHA-512，并校验完整大小。
4. 用户点击“重启并安装”后，应用启动下载好的 Setup.exe：

   ```text
   Setup.exe --mode=update --auto-start --target-version <latest-win.json.version> --target <当前安装目录>
   ```

5. Setup 使用与首次安装相同的自绘界面，必要时通过 UAC 启动带操作计划的管理员实例，关闭旧应用后原子替换应用目录。

更新下载失败、校验失败或 Setup 启动失败都会回到版本中心的错误状态，不会静默覆盖旧版本。

## 更新元数据

Windows 元数据由实际安装包计算，不手工复制校验值。简化示例：

```json
{
  "schemaVersion": 1,
  "platform": "win32",
  "version": "3.0.2",
  "tag": "v3.0.2",
  "artifact": {
    "url": "wordTTS-Setup-3.0.2-x64.exe",
    "sha512": "<base64 sha512>",
    "size": 123456789
  },
  "updateMode": "optional",
  "minimumSupportedVersion": null,
  "updateMessage": "这是一个可选更新。",
  "releaseNotes": "..."
}
```

`files`、`path`、`sha512` 和 `size` 也会保留在 JSON 中，方便状态机和诊断代码使用。GitHub Release 的 Windows 资产名为 `wordTTS-Setup-<version>-x64.exe`，本地构建名为 `小猪wordTTS-Setup-<version>-x64.exe`。

macOS 仍按 `latest-mac.yml` 的格式发布 ZIP；Windows JSON 和 macOS YAML 不能互换。

## 更新策略

`release/update-policy.json` 是每个版本的唯一策略入口：

- `mode: "optional"`：显示可选更新，不打断正在进行的任务。
- `mode: "force"`：收到有效更新包后显示强更遮罩，必须下载并重启安装。
- `minimumSupportedVersion`：只淘汰低于指定版本的客户端。
- `message`：显示在版本中心和强更提示中，最多 500 个字符。

版本号不在策略文件里重复维护，统一从根目录 `version.json` 读取；构建时会同步到
`electron/package.json`、`electron/package-lock.json` 和安装器构建元数据。Release 的
`latest-win.json.version` 就是 Windows 安装器实际更新到的目标版本。CI 会在构建前运行：

```bash
node scripts/project_version.js --set 3.0.2
node scripts/validate_update_policy.js --tag v3.0.2
```

## 正常发布步骤

1. 只更新根目录 `version.json`，或运行 `node scripts/project_version.js --set <version>`。
2. 按需更新 `release/update-policy.json`（只维护更新规则，不再填写版本号）。
3. 在 `CHANGELOG.md` 增加严格匹配的 `## v<version>` 章节。
4. 推送 `v<version>` tag。
5. `Build and Release` 会把唯一版本源同步到 Electron、后端和自绘 Setup；Windows 先构建 `dir` 应用，再生成自绘 Setup.exe。
6. 发布 job 汇总两个构建产物，生成 `latest-win.json` 和 `latest-mac.yml`，创建 Draft Release，上传所有资产后再公开。

最终 Release 至少包含：

- Windows：`latest-win.json`、`wordTTS-Setup-<version>-x64.exe`。
- macOS：`latest-mac.yml`、`wordTTS-<version>-<arch>.zip` 和首次安装用的 `.dmg`。

## 签名

- Windows：配置 `WINDOWS_CSC_LINK`、`WINDOWS_CSC_KEY_PASSWORD`，对最终自绘 Setup.exe 进行 Authenticode 签名。未签名包只适合内部验证。
- macOS：配置 `MAC_CSC_LINK`、`MAC_CSC_KEY_PASSWORD`，需要公证时再配置 Apple 相关凭据。

签名应作用于最终分发的 Setup.exe，而不是只签名 `win-unpacked` 目录中的应用壳。

## 审阅清单

- Windows 构建目标是 `dir`，随后确实运行 `scripts/build_windows_installer.js`。
- Windows 用户分发的是独立 Setup.exe，不是 `win-unpacked` 目录或单独的 Electron 应用 exe。
- Windows 更新代码没有加载 `electron-updater`，只读取 `latest-win.json`。
- Setup.exe、安装目录中的 uninstaller 和更新客户端都能在无 Node/Python 环境下运行。
- Windows 更新包下载后校验大小和 SHA-512，再启动 `--mode=update`。
- 安装、更新、卸载都使用同一套自绘 HTML 页面；卸载不会误删用户数据。
- Release 不是 Draft，tag、`version.json`、构建包版本和更新元数据版本一致。
- macOS Release 同时存在 `latest-mac.yml` 和 ZIP，不能只上传 DMG。

## 本地检查

```bash
node scripts/validate_update_policy.js
node scripts/project_version.js --sync
node --check installer-prototype/installer-main.js
node --check installer-prototype/installer-service.js
node --check electron/windows-update-client.js
cd electron
npm test
```

生成 Windows 元数据需要真实的 `electron/release/小猪wordTTS-Setup-<version>-x64.exe` 和 `release-notes.md`：

```bash
node scripts/project_version.js --set 3.0.2
RELEASE_TAG=v3.0.2 \
  node scripts/prepare_update_metadata.js --platform win32
```
