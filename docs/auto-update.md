# 小猪wordTTS 自动更新与发布流程

## 目标与边界

桌面包发布到 GitHub Releases，不增加自建更新服务器。Windows 和 macOS 使用两条明确分开的更新链路：

- Windows：使用独立的 HTML 自绘 `Setup.exe` 完成首次安装、更新和卸载；客户端读取 `latest-win.json`，优先结合 `.blockmap` 和 HTTP Range 下载差异，再启动它的 `update` 模式。
- macOS：继续使用 `electron-updater` 和 `latest-mac.yml`；DMG 负责首次安装，ZIP 负责自动更新。
- 开发模式和 `--smoke-test` 不访问更新服务。
- 当前 macOS Actions 按 runner 架构构建一个包；如果要同时覆盖 Intel 与 Apple Silicon，应增加对应架构构建并把两个 ZIP 一起放进同一个 macOS 元数据流程。

Windows 不再使用 `electron-updater`、`latest.yml` 或系统安装页面。这样安装器的视觉、交互、安装/卸载文案和流程都由 `installer-prototype/` 控制；差分下载只复用 electron-builder 的 blockmap 运算，不改变这套自绘 Setup 方案。

## Windows 结构

```text
electron/release/win-unpacked/       Electron 应用目录
                │
                ▼
scripts/build_windows_installer.js   把应用目录归档并嵌入独立的 Setup.exe
                │
                ▼
小猪wordTTS-Setup-<version>-x64.exe  用户分发包
小猪wordTTS-Setup-<version>-x64.exe.blockmap  差分索引
```

Windows Setup 是一个短生命周期的 Electron 包，启动时只携带自绘页面、安装逻辑和小型 `wordtts-7za.exe`；完整应用先作为非固实 `wordtts-payload.7z` 资源按需导出，因此页面不必等待整个 Chromium/Python 目录先复制完。首次安装或更新时，服务层把归档解压到 staging 目录，再原子替换应用目录，并保存 `install-state.json`。已安装目录中会留下同一套自绘程序作为 `小猪wordTTS-uninstaller.exe`，因此卸载也不会回到原生页面。卸载启动后会先把该可执行文件迁移到系统临时目录；经过标记校验的临时副本同步删除并确认安装目录消失，同时预写一个只删除该临时副本的批处理。Electron 退出后，仍掌握外层生命周期的 portable NSIS 外壳启动该批处理并随即释放自身文件句柄，不再由 Electron 反向派生 PowerShell 等待父进程。只有旧版或未迁移的 portable 外壳才使用延迟目录收尾脚本，避免新路径重新引入安装目录锁定。

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
3. 客户端先查找本机当前版本留下的 `小猪wordTTS-uninstaller.exe`，并尝试下载当前版本和目标版本的 `.blockmap`。如果服务端支持 HTTP Range，客户端只下载 blockmap 计算出的新增分块，并把旧安装器中可复用的分块直接复制到临时 Setup.exe。
4. 差分路径完成后仍会计算新 Setup.exe 的完整大小和 SHA-512；如果旧安装器、blockmap、Range 响应或差分结果缺失/异常，则删除临时文件并自动回退到原有的全量 Setup.exe 下载。
5. 用户点击“重启并安装”后，应用启动下载好的 Setup.exe：

   ```text
   Setup.exe --mode=update --auto-start --target-version <latest-win.json.version> --target <当前安装目录>
   ```

6. Setup 使用与首次安装相同的自绘界面，必要时通过 UAC 启动带操作计划的管理员实例，关闭旧应用后原子替换应用目录。

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
    "size": 123456789,
    "blockmap": "wordTTS-Setup-3.0.2-x64.exe.blockmap"
  },
  "blockmap": {
    "url": "wordTTS-Setup-3.0.2-x64.exe.blockmap",
    "sha512": "<base64 sha512>",
    "size": 45678
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

- Windows：`latest-win.json`、`wordTTS-Setup-<version>-x64.exe`、对应的 `wordTTS-Setup-<version>-x64.exe.blockmap`。`.blockmap` 缺失时旧客户端仍可全量更新，但新客户端会回退全量下载。
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
- Windows 发布资产同时包含 Setup.exe 和同名 `.blockmap`；更新客户端优先走差分，任何前置条件不满足都必须安全回退全量下载。
- 自绘安装器应先显示页面，再在安装/更新操作开始时导出并解压 payload；不能重新把完整 `win-unpacked` 目录作为 `extraResources` 复制进 Setup 启动阶段。
- 安装、更新、卸载都使用同一套自绘 HTML 页面；卸载不会误删用户数据。
- Windows 生命周期冒烟除了检查安装目录消失，还必须看到迁移卸载器的成功日志和临时外壳自清理完成日志；异步 `[error]` 不能被绿色步骤掩盖。
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
