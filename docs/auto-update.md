# 小猪wordTTS 自动更新与发布流程

## 目标与边界

小猪wordTTS 不增加自建服务器。正式桌面包发布到 GitHub Releases，客户端通过 `electron-updater` 查询同一 Release 下的更新元数据，下载对应平台安装包，再由系统安装器完成覆盖升级。

当前支持范围：

- Windows：NSIS 安装包本地构建名为 `小猪wordTTS-Setup-<version>-x64.exe`，GitHub 下载资产名为 `wordTTS-Setup-<version>-x64.exe`，元数据为 `latest.yml`。
- macOS：DMG 负责首次安装，ZIP 负责自动更新；本地构建名为 `小猪wordTTS-<version>-<arch>.zip`，GitHub 下载资产名为 `wordTTS-<version>-<arch>.zip`，元数据为 `latest-mac.yml`。
- 开发模式和 `--smoke-test` 不会访问更新服务。
- 当前 macOS Actions 按 runner 架构构建一个包；如果要同时覆盖 Intel 与 Apple Silicon，应增加对应架构构建并把两个 ZIP 一起放进同一个 `latest-mac.yml`。

GitHub Release 对客户端必须是已发布的非 Draft Release。两个平台的构建会先把安装包上传为 Actions Artifact，只有两端构建都成功后，统一发布 job 才会汇总 Artifact、生成两份 `latest*.yml` 并创建 Draft Release；所有安装包和元数据上传完成后才公开 Release。客户端还会探测当前平台的具体安装包，避免只看到 tag、半成品 Release 或上传中的元数据就显示更新。客户端不把 GitHub Token 写入安装包；因此当前方案按公开仓库/公开 Release 设计。私有仓库需要另行设计鉴权，不能直接把 Token 放进 renderer 或安装包。

## 代码与产物职责

| 文件 | 职责 |
| --- | --- |
| `electron/update-manager.js` | 主进程更新状态机：检查、下载、已下载、安装、错误和定时检查。 |
| `electron/main.js` | 只在可信本地 renderer 上开放更新 IPC；固定 GitHub Release 页面地址。 |
| `electron/preload.js` | 通过 context bridge 暴露最小更新 API，不暴露 Node 或任意远程 URL。 |
| `electron/renderer/index.html` / `app.js` / `styles.css` | 固定的“版本中心”页面、更新日志、进度和强更遮罩。 |
| `release/update-policy.json` | 每个版本发布前必须填写的强更/非强更策略。 |
| `scripts/validate_update_policy.js` | 发布前检查版本号、tag 和策略字段是否一致。 |
| `scripts/prepare_update_metadata.js` | 根据实际安装包计算 SHA-512、大小，并生成 `latest.yml` 或 `latest-mac.yml`。 |
| `.github/workflows/build-macos.yml` / `build-windows.yml` | 可复用的平台构建、校验和构建 Artifact；两个平台在统一发布流程中并行执行。 |
| `.github/workflows/build-release.yml` | 监听 `v*` tag，调用两个平台构建，汇总 Artifact，生成两端 Release 元数据并一次性发布 GitHub Release。 |

安装包、校验值和策略的关系是：

```text
release/update-policy.json + CHANGELOG.md + electron/package.json
                    │
                    ▼
        tag → Actions 构建真实安装包
                    │
                    ▼
  prepare_update_metadata.js 计算文件大小和 SHA-512
                    │
                    ▼
       GitHub Release + latest*.yml + 更新日志
                    │
                    ▼
     客户端检查 → 下载 → 用户确认重启 → 覆盖安装
```

## 更新策略配置

`release/update-policy.json` 是发布前的唯一策略入口。示例：

```json
{
  "$schema": "./update-policy.schema.json",
  "version": "2.7.46",
  "mode": "optional",
  "minimumSupportedVersion": null,
  "message": "这是一个可选更新。你可以在方便时下载并重启安装。"
}
```

字段规则：

- `version` 必须等于 `electron/package.json` 版本，也必须等于 `v<version>` tag 去掉 `v` 后的值。
- `mode: "optional"` 是非强制更新：后台监测到新版本后显示版本中心角标，不打断正在进行的任务。
- `mode: "force"` 是强制更新：检测到该 Release 后显示强更遮罩，必须下载并重启安装才能继续使用。
- `minimumSupportedVersion` 可用于只强制淘汰旧版本。例如新 Release 为 `2.7.46`，设置为 `2.7.40` 后，`< 2.7.40` 的客户端必须升级，较新的客户端仍按 `mode` 判断。
- `message` 会进入客户端状态页和强更提示，限制为 500 个字符；不要放入 Token、内部地址或个人信息。
- CI 同时拒绝缺少必填字段、未知字段和错误类型；即使 `minimumSupportedVersion` 不设，也要明确写成 `null`。

策略下发在 `latest.yml` / `latest-mac.yml` 中，额外字段包括 `updateMode`、`minimumSupportedVersion`、`updateMessage` 和 `releaseNotes`。旧的 `files`、`sha512` 和 `size` 字段仍由脚本按实际文件生成，不能手工复制上一版。

## 正常 tag 发布步骤

每次推 tag 前按下面顺序完成：

1. 更新版本。推荐在 `electron/` 目录执行 `npm version <新版本> --no-git-tag-version`，让 `package.json` 和 lock 文件同步；例如 `npm version 2.7.46 --no-git-tag-version`。
2. 修改 `release/update-policy.json` 的 `version`、`mode`、`minimumSupportedVersion` 和 `message`。
3. 在 `CHANGELOG.md` 顶部增加严格匹配的 `## v<新版本>` 标题和更新内容。Actions 会把这一节提取为 Release body 和应用内更新日志。
4. 在仓库根目录执行预检：

   ```bash
   node scripts/validate_update_policy.js --version 2.7.46 --tag v2.7.46
   ```

5. 审阅 `git diff`，确认没有把强更误留在 `force`，并提交上述版本、策略、日志和代码变更。
6. 创建并推送 tag：

   ```bash
   git tag v2.7.46
   git push origin main --follow-tags
   ```

7. 等待 `Build and Release` 完成。它会并行执行 macOS 和 Windows 构建，两个构建成功后由同一个 Release job 汇总 Artifact、生成两端元数据，并一次性发布 GitHub Release；单独运行 `Build macOS` 或 `Build Windows` 只生成对应构建 Artifact，不发布 Release。
8. 在 GitHub Release 页面确认至少存在：

   - Windows：`latest.yml`、`wordTTS-Setup-2.7.46-x64.exe`。
   - macOS：`latest-mac.yml`、`wordTTS-2.7.46-<arch>.zip`，以及用于首次安装的 `.dmg`。

不要先推 tag、再补策略文件。CI 会在构建早期因为版本不一致失败；这是有意设计的发布门。

## 客户端行为

- 启动约 6 秒后自动检查，之后默认每 6 小时检查一次；版本中心也可以手动检查。
- 非强更只显示版本中心的“新”角标和可选下载按钮，不强迫用户中断生成任务。
- 下载过程中显示百分比、已传输大小和速度；下载完成后显示“重启并安装”。关闭应用不会绕过这个按钮自动安装，用户可以稍后再处理。
- 强更只在客户端收到有效的新版本元数据且当前平台安装包可下载后生效。单纯 tag、Draft Release、缺少当前平台安装包或上传中的资产都不会显示“新”角标，也不会锁死应用；网络失败时用户可以重试或打开 Release 页面手动处理。
- 安装由 `electron-updater` 调用平台安装器完成；应用数据目录仍是原来的 `WordTTS`，更新不会删除文档、任务记录或偏好设置。

## 签名与首次安装

自动更新最容易在签名环节失败，发布前应配置：

- macOS：`MAC_CSC_LINK`、`MAC_CSC_KEY_PASSWORD`，以及需要公证时的 `APPLE_ID`、`APPLE_APP_SPECIFIC_PASSWORD`、`APPLE_TEAM_ID`。当前脚本在没有 Developer ID 时会生成 ad-hoc 包供本地验证，但这不是面向普通用户的生产签名方案，Gatekeeper 或 Squirrel.Mac 可能拒绝自动更新。
- Windows：`WINDOWS_CSC_LINK`、`WINDOWS_CSC_KEY_PASSWORD`。未签名包可以用于内部验证，但签名包更适合生产分发，并能减少 SmartScreen 和更新校验问题。

## 审阅清单

审阅代码或一次 Release 时，重点确认：

- 更新逻辑只在 packaged app 启用，开发/冒烟不会联网。
- renderer 不能传入任意下载 URL；外部页面只能打开固定的 GitHub Releases 地址。
- `latest*.yml` 的 `files.url`、`sha512`、`size` 与本次实际产物一致。
- Windows 发布的是 NSIS 安装包，不是 `win-unpacked` 内的可执行文件；macOS 元数据引用 ZIP，不引用 DMG。
- 发布 Release 前必须同时存在两端安装包与 `latest*.yml`；客户端只接受当前平台扩展名、校验字段齐全且远端可访问的资产。
- Release 不是 Draft，tag、package version、policy version 三者一致。
- `latest*.yml` 的 `files.url` 与 `path` 必须使用 GitHub Release 实际下载资产名；发布脚本会把本地中文构建名转换为 GitHub 的 ASCII 规范名。
- `optional` 与 `force` 的判断有对应 CHANGELOG 和产品审批记录。
- Release 之间不复用旧版本号。若包有问题，应发布更高版本修复，而不是替换同版本资产后期待客户端稳定恢复。

## 故障处理

- 页面显示“检查失败”：先检查网络和 GitHub Release 是否为已发布状态，再用版本中心的“重试检查”或打开 Release 页面手动下载。
- Windows 下载后提示签名错误：检查新的 `.exe` 是否由正确证书签名，以及 Release 中的 `latest.yml` 是否由当前包重新生成。
- macOS 找不到更新：检查 Release 是否同时有 `latest-mac.yml` 和 `.zip`；不能只上传 DMG。确认 ZIP 内的应用名称和构建架构与客户端一致。
- 强更规则误配：修改 `release/update-policy.json` 后发布更高版本，并把 `minimumSupportedVersion` 调整到正确范围；不要删除正在被旧客户端查询的 Release 元数据。

## 本地检查命令

```bash
# 策略与 tag 预检
node scripts/validate_update_policy.js

# Electron 单元/静态测试
cd electron
npm test
```

生成元数据需要真实的 `electron/release` 安装包和 `release-notes.md`，通常由 tag workflow 自动完成。手工验证时先生成日志，再在目标平台执行：

```bash
RELEASE_TAG=v2.7.46 node scripts/extract_release_notes.js
UPDATE_VERSION=2.7.46 RELEASE_TAG=v2.7.46 node scripts/prepare_update_metadata.js --platform win32
```

上面的 Windows 命令只能在 `electron/release` 已有对应 NSIS 安装包时执行；macOS 使用 `--platform darwin`，并需要对应架构 ZIP。
