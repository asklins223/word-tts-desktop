#!/bin/bash
# ============================================================================
# 小猪wordTTS — Electron 混合打包脚本
# ============================================================================
# 产出: electron/release/小猪wordTTS-<version>-<arch>.dmg
#
# 流程:
#   1. PyInstaller 打包 server.py → server_backend/
#   2. electron-builder 打包 Electron 壳 + server_backend → .dmg
#
# 用法:
#   bash build_electron.sh              → 完整构建（PyInstaller + electron-builder）
#   bash build_electron.sh --python     → 仅构建 Python 后端
#   bash build_electron.sh --electron   → 重建当前 Python 后端并打包 Electron 壳
#
# 前置条件（脚本会同步 Python 依赖）:
#   建议先创建并激活独立 Python venv
#   cd electron && npm install
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ELECTRON_DIR="$SCRIPT_DIR/electron"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements_electron.txt"
PRODUCT_NAME="小猪wordTTS"
BUILD_PYTHON_CMD=""
BUILD_VENV_DIR=""

cleanup_build_environment() {
    # Only remove the venv created by this invocation at the exact mktemp path.
    if [ -n "$BUILD_VENV_DIR" ] && [ -d "$BUILD_VENV_DIR" ]; then
        rm -rf "$BUILD_VENV_DIR"
    fi
}

trap cleanup_build_environment EXIT

case "$(uname -m)" in
    arm64)
        BUILD_ARCH="arm64"
        BUILDER_ARCH_FLAG="--arm64"
        MAC_OUTPUT_DIR="mac-arm64"
        ;;
    x86_64)
        BUILD_ARCH="x64"
        BUILDER_ARCH_FLAG="--x64"
        MAC_OUTPUT_DIR="mac"
        ;;
    *)
        echo "[错误] 不支持的 macOS 构建架构: $(uname -m)"
        exit 1
        ;;
esac

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[构建]${NC} $1"; }
warn() { echo -e "${YELLOW}[警告]${NC} $1"; }
err()  { echo -e "${RED}[错误]${NC} $1"; }

version_lt() {
    awk -v left="$1" -v right="$2" 'BEGIN {
        left_count = split(left, left_parts, ".")
        right_count = split(right, right_parts, ".")
        count = left_count > right_count ? left_count : right_count
        for (i = 1; i <= count; i++) {
            l = left_parts[i] + 0
            r = right_parts[i] + 0
            if (l < r) exit 0
            if (l > r) exit 1
        }
        exit 1
    }'
}

# ============================================================================
# 环境检查
# ============================================================================
check_environment() {
    log "检查构建环境..."

    # Python
    if ! command -v python3 &> /dev/null; then
        err "未找到 python3，请先安装 Python 3.10+"
        exit 1
    fi
    local python_cmd
    python_cmd="$(command -v python3)"
    echo "  Python: $("$python_cmd" --version)"

    # Homebrew Python 遵循 PEP 668，不能直接向系统环境 pip install。
    # 未处于 venv 时自动创建一次性、精确路径的构建 venv，避免构建依赖
    # 污染用户环境；CI 的 setup-python/venv 则直接复用已有隔离环境。
    if ! "$python_cmd" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' 2>/dev/null; then
        warn "当前未使用 Python 虚拟环境；为本次构建创建一次性隔离环境"
        BUILD_VENV_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wordtts-build-venv.XXXXXX")"
        "$python_cmd" -m venv "$BUILD_VENV_DIR"
        python_cmd="$BUILD_VENV_DIR/bin/python"
        export PATH="$BUILD_VENV_DIR/bin:$PATH"
        echo "  已创建构建 venv: $BUILD_VENV_DIR"
    fi
    BUILD_PYTHON_CMD="$python_cmd"

    # electron-builder 的 DMG 工具需要 `python` 命令；激活 venv 时已由
    # PATH 提供，否则只在仓库忽略的临时目录创建精确链接。
    if ! command -v python &> /dev/null; then
        local tmp_bin="$SCRIPT_DIR/.build_bin"
        mkdir -p "$tmp_bin"
        ln -sf "$BUILD_PYTHON_CMD" "$tmp_bin/python"
        export PATH="$tmp_bin:$PATH"
        echo "  已创建临时 python 链接: $tmp_bin/python"
    fi

    # 只从 Electron 专用依赖清单同步当前解释器，避免零散安装导致版本漂移。
    if [ ! -f "$REQUIREMENTS_FILE" ]; then
        err "缺少依赖清单: $REQUIREMENTS_FILE"
        exit 1
    fi
    # macOS CI 的前置校验步骤已经在同一个 venv 中完成安装；显式复用时
    # 跳过第二次 pip 解析/安装，但仍保留后面的完整性和版本校验。
    if [ "${WORDTTS_SKIP_PYTHON_DEPENDENCY_INSTALL:-0}" = "1" ]; then
        log "复用当前 Python 环境中已安装的 Electron 构建依赖..."
    else
        log "同步 Electron Python 构建依赖..."
        "$BUILD_PYTHON_CMD" -m pip install --disable-pip-version-check -r "$REQUIREMENTS_FILE"
    fi
    "$BUILD_PYTHON_CMD" -m pip check
    "$BUILD_PYTHON_CMD" -c "from importlib.metadata import version; raise SystemExit(0 if version('playwright') == '1.56.0' else 1)" || {
        err "Playwright 版本必须为 1.56.0"
        exit 1
    }

    # Node.js
    if ! command -v node &> /dev/null; then
        err "未找到 Node.js，请先安装: https://nodejs.org"
        exit 1
    fi
    echo "  Node.js: $(node --version)"
    local node_major
    node_major="$(node -p 'process.versions.node.split(".")[0]')"
    if [ "$node_major" != "24" ]; then
        err "项目构建要求 Node.js 24.x，当前为 $(node --version)；请使用 Node 24 后重试"
        exit 1
    fi

    # version.json 是唯一手工维护的应用版本；electron/package*.json
    # 只是 electron-builder/npm 所需的同步元数据。
    log "同步项目版本信息..."
    node "$SCRIPT_DIR/scripts/project_version.js" --sync

    # electron-builder
    if [ ! -d "$ELECTRON_DIR/node_modules/electron-builder" ]; then
        warn "electron-builder 未安装，正在安装..."
        cd "$ELECTRON_DIR"
        npm install
        cd "$SCRIPT_DIR"
    fi

    # 此命令幂等，只会补齐 Playwright 1.56.0 所要求的准确 Chromium revision。
    log "检查 Playwright Chromium..."
    "$BUILD_PYTHON_CMD" -m playwright install chromium

    echo "  环境检查通过 ✓"
}

# ============================================================================
# 步骤 1: PyInstaller 打包后端
# ============================================================================
build_python_backend() {
    log "=== 步骤 1/2: PyInstaller 打包后端 ==="

    PYINSTALLER_DIST="$ELECTRON_DIR/server_backend_build"
    PYINSTALLER_WORK="$ELECTRON_DIR/server_backend_build_tmp"

    # 清理旧构建
    log "清理旧构建..."
    rm -rf "$PYINSTALLER_DIST" "$PYINSTALLER_WORK"

    cd "$SCRIPT_DIR"
    "$BUILD_PYTHON_CMD" -m PyInstaller server_pyinstaller.spec \
        --noconfirm \
        --distpath "$PYINSTALLER_DIST" \
        --workpath "$PYINSTALLER_WORK"

    # Chromium 是一个已签名的嵌套应用，必须在 PyInstaller 后原样复制，
    # 否则 PyInstaller 的二进制重签会破坏其 Framework bundle。
    node "$SCRIPT_DIR/scripts/stage_playwright_browser.js" \
        "$PYINSTALLER_DIST/server_backend"
    local browser_app
    browser_app="$(find "$PYINSTALLER_DIST/server_backend/_internal/playwright_browsers" \
        -maxdepth 4 -type d \
        \( -name "Chromium.app" -o -name "Google Chrome for Testing.app" \) \
        2>/dev/null | head -1)"
    if [ -z "$browser_app" ]; then
        err "内置 Playwright Chromium 不完整：未找到 Chromium 浏览器应用"
        exit 1
    fi
    codesign --force --deep --sign - "$browser_app"
    codesign --verify --deep --strict "$browser_app"

    # 验证产物
    if [ ! -f "$PYINSTALLER_DIST/server_backend/server_backend" ]; then
        err "PyInstaller 打包失败：未找到 server_backend 可执行文件"
        exit 1
    fi

    # PyInstaller 产物必须和随后构建的 Electron 壳为同一架构。
    local backend_info
    backend_info="$(file "$PYINSTALLER_DIST/server_backend/server_backend")"
    if [ "$BUILD_ARCH" = "arm64" ] && [[ "$backend_info" != *"arm64"* ]]; then
        err "后端架构不匹配，期望 arm64: $backend_info"
        exit 1
    fi
    if [ "$BUILD_ARCH" = "x64" ] && [[ "$backend_info" != *"x86_64"* ]]; then
        err "后端架构不匹配，期望 x64: $backend_info"
        exit 1
    fi

    # 清理临时目录
    rm -rf "$PYINSTALLER_WORK"

    log "后端打包完成 ✓"
    echo "  产物位置: $PYINSTALLER_DIST/server_backend/"
    echo "  大小: $(du -sh "$PYINSTALLER_DIST/server_backend/" | awk '{print $1}')"
}

# ============================================================================
# 步骤 2: electron-builder 打包前端 + 后端
# ============================================================================
build_electron_app() {
    log "=== 步骤 2/2: electron-builder 打包应用 ==="

    local package_version
    package_version="$(node "$SCRIPT_DIR/scripts/project_version.js")"
    local app_identifier
    app_identifier="$(node -p "require('$ELECTRON_DIR/package.json').build.appId")"
    local adhoc_designated_requirement="=designated => identifier \"$app_identifier\""
    local expected_zip_path="$ELECTRON_DIR/release/$PRODUCT_NAME-${package_version}-${BUILD_ARCH}.zip"

    log "生成 macOS / Windows 应用图标..."
    "$BUILD_PYTHON_CMD" "$SCRIPT_DIR/scripts/build_app_icons.py"

    # 确认后端产物存在
    if [ ! -d "$ELECTRON_DIR/server_backend_build/server_backend" ]; then
        err "未找到后端产物，请先运行: bash build_electron.sh --python"
        exit 1
    fi

    # 清理上次失败构建可能残留的挂载点
    log "清理残留挂载点..."
    for vol in "/Volumes/$PRODUCT_NAME" "/Volumes/$PRODUCT_NAME 1.0.0" "/Volumes/$PRODUCT_NAME-1.0.0" "/Volumes/WordTTS"; do
        hdiutil detach -force "$vol" 2>/dev/null || true
    done
    # 清理旧的 release 目录
    rm -rf "$ELECTRON_DIR/release/$MAC_OUTPUT_DIR" 2>/dev/null || true
    # 不允许沿用同版本的旧 ZIP。自动更新元数据必须始终对应本次完成签名
    # 和冒烟验证的 .app，否则本地重跑失败构建时可能把旧包上传到 Release。
    rm -f "$expected_zip_path"

    # 确保后端二进制有执行权限
    chmod +x "$ELECTRON_DIR/server_backend_build/server_backend/server_backend" 2>/dev/null || true

    local backend_info
    backend_info="$(file "$ELECTRON_DIR/server_backend_build/server_backend/server_backend")"
    if [ "$BUILD_ARCH" = "arm64" ] && [[ "$backend_info" != *"arm64"* ]]; then
        err "现有后端不是 arm64，不能装入 arm64 Electron: $backend_info"
        exit 1
    fi
    if [ "$BUILD_ARCH" = "x64" ] && [[ "$backend_info" != *"x86_64"* ]]; then
        err "现有后端不是 x64，不能装入 x64 Electron: $backend_info"
        exit 1
    fi

    # package.json 给出产品期望的最低版本，但本机构建环境里的 Python、OpenSSL
    # 或 Chromium 可能要求更高版本。扫描所有内置 Mach-O，避免
    # Info.plist 声称支持旧系统、实际却在启动时被 dyld 拒绝。
    local configured_macos_min
    local backend_macos_min="0.0"
    local candidate_minos
    local candidate_binary
    if ! command -v vtool &> /dev/null; then
        err "未找到 vtool，无法校验包内二进制的最低 macOS 版本"
        exit 1
    fi
    configured_macos_min="$(node -p "require('$ELECTRON_DIR/package.json').build.mac.minimumSystemVersion || '0.0'")"
    while IFS= read -r -d '' candidate_binary; do
        if file "$candidate_binary" | grep -q "Mach-O"; then
            candidate_minos="$(vtool -show-build "$candidate_binary" 2>/dev/null | awk '/minos/{print $2; exit}')"
            if [ -n "$candidate_minos" ] && version_lt "$backend_macos_min" "$candidate_minos"; then
                backend_macos_min="$candidate_minos"
            fi
        fi
    done < <(find "$ELECTRON_DIR/server_backend_build/server_backend" -type f -print0)

    local effective_macos_min="$configured_macos_min"
    if version_lt "$configured_macos_min" "$backend_macos_min"; then
        effective_macos_min="$backend_macos_min"
    fi
    log "本次产物最低系统版本: macOS ${effective_macos_min} (配置 ${configured_macos_min}, 内置二进制 ${backend_macos_min})"

    # 清理空的签名环境变量，避免 electron-builder 将空 CSC_LINK
    # 误解析为路径（指向当前目录）导致 "not a file" 错误
    if [ -z "${CSC_LINK:-}" ]; then
        unset CSC_LINK
    fi
    if [ -z "${CSC_KEY_PASSWORD:-}" ]; then
        unset CSC_KEY_PASSWORD
    fi

    cd "$ELECTRON_DIR"
    npx electron-builder --mac "$BUILDER_ARCH_FLAG" \
        --publish never \
        -c.mac.minimumSystemVersion="$effective_macos_min"

    # electron-builder 的 zip 是在后续签名步骤之前生成的，不能作为自动
    # 更新包发布。保留它会让通配符上传把未完成签名的副本也带进 Release；
    # 后面只从最终签名并通过冒烟验证的 .app 重建目标 ZIP。
    local builder_zip_path="$ELECTRON_DIR/release/$PRODUCT_NAME-${package_version}-${BUILD_ARCH}-mac.zip"
    rm -f "$builder_zip_path" "$builder_zip_path.blockmap" "$ELECTRON_DIR/release/latest-mac.yml"

    # 只接受本次目标架构对应的目录，禁止从旧目录误拿另一架构产物。
    local app_path="$ELECTRON_DIR/release/$MAC_OUTPUT_DIR/$PRODUCT_NAME.app"

    if [ -z "$app_path" ] || [ ! -d "$app_path" ]; then
        err "未找到构建产物 $PRODUCT_NAME.app"
        exit 1
    fi

    local packaged_macos_min
    packaged_macos_min="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "$app_path/Contents/Info.plist")"
    if version_lt "$packaged_macos_min" "$effective_macos_min"; then
        err "应用最低系统版本写入失败：期望 $effective_macos_min，实际 $packaged_macos_min"
        exit 1
    fi

    log "构建产物: $app_path"

    local app_info
    app_info="$(file "$app_path/Contents/MacOS/$PRODUCT_NAME")"
    if [ "$BUILD_ARCH" = "arm64" ] && [[ "$app_info" != *"arm64"* ]]; then
        err "Electron 主程序架构不匹配: $app_info"
        exit 1
    fi
    if [ "$BUILD_ARCH" = "x64" ] && [[ "$app_info" != *"x86_64"* ]]; then
        err "Electron 主程序架构不匹配: $app_info"
        exit 1
    fi

    # ---- 签名（从内到外）----
    # electron-builder 已使用 Developer ID 时必须保留其签名，不能再被 ad-hoc 覆盖。
    # 没有 Developer ID 时，主应用必须使用显式且跨版本稳定的 designated
    # requirement。codesign 默认生成的 ad-hoc requirement 会绑定本次构建的
    # CDHash，下一版内容一变就会被 Squirrel.Mac/ShipIt 拒绝。
    if codesign -dv --verbose=4 "$app_path" 2>&1 | grep -q "Authority=Developer ID Application"; then
        log "检测到 Developer ID 签名，保留现有签名"
    else
        log "未配置 Developer ID，进行 ad-hoc 签名..."
        xattr -cr "$app_path" 2>/dev/null || true

    local fw_dir="$app_path/Contents/Frameworks"

    # 1. 签 server_backend 内部的动态库和可执行文件
    local backend_dir="$app_path/Contents/Resources/server_backend"
    if [ -d "$backend_dir" ]; then
        find "$backend_dir" -path "*/playwright_browsers" -prune -o \
            -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.pyd" \) \
            -exec codesign --force --sign - {} \; 2>/dev/null || true
        codesign --force --sign - "$backend_dir/server_backend" 2>/dev/null || true
    fi

    # 2. 签 Frameworks 内的叶子二进制文件（.dylib，但不签 framework bundle 内部的文件）
    if [ -d "$fw_dir" ]; then
        find "$fw_dir" -type f -name "*.dylib" \
            -not -path "*/Electron Framework.framework/*" \
            -exec codesign --force --sign - {} \; 2>/dev/null || true
    fi

    # 3. 签 Framework bundle（整体签名，不破坏 seal）
    if [ -d "$fw_dir/Electron Framework.framework" ]; then
        codesign --force --sign - \
            "$fw_dir/Electron Framework.framework" 2>/dev/null || true
    fi
    for fw in "$fw_dir"/*.framework; do
        [ -d "$fw" ] && [ "$(basename "$fw")" != "Electron Framework.framework" ] && \
            codesign --force --sign - "$fw" 2>/dev/null || true
    done

    # 4. 签 Helper apps
    if [ -d "$fw_dir" ]; then
        find "$fw_dir" -maxdepth 1 -name "*.app" -type d \
            -exec codesign --force --sign - {} \; 2>/dev/null || true
    fi

    # 5. 签主应用。只有主 bundle 的 designated requirement 会被 ShipIt
    # 用来验证下一版；嵌套代码仍由 codesign 的 deep 校验保证完整性。
    if ! codesign --force --sign - \
        --identifier "$app_identifier" \
        --requirements "$adhoc_designated_requirement" \
        "$app_path"; then
        err "主应用 ad-hoc 签名失败"
        exit 1
    fi
    xattr -cr "$app_path" 2>/dev/null || true
        echo "  ad-hoc 签名完成 ✓"
    fi

    # 同时验证包体完整性与 ShipIt 真正使用的跨版本指定要求。不能再把
    # 仅通过 codesign 包体校验的一次性 CDHash 签名当成可更新产物。
    WORDTTS_MAC_BUNDLE_ID="$app_identifier" \
        bash "$SCRIPT_DIR/scripts/verify_macos_update_signature.sh" "$app_path"

    # 在创建 DMG 前验证最终 .app 内的 Playwright driver、Chromium 和
    # Electron Node 回退路径，避免只验证源码或 PyInstaller 目录而漏掉
    # 实际分发包中的运行时缺件。
    log "验证打包 Playwright/Chromium..."
    "$app_path/Contents/Resources/server_backend/server_backend" --smoke-playwright

    # 再验证最终 Electron 壳、主进程、Preload、Renderer 和后端启动协议；
    # --smoke-test 会强制离线，不登录讯飞、不产生第三方副作用。
    log "验证打包 Electron 桌面端..."
    env -u ELECTRON_RUN_AS_NODE -u PLAYWRIGHT_NODEJS_PATH \
        "$app_path/Contents/MacOS/$PRODUCT_NAME" --smoke-test

    # ---- 手动创建 DMG（绕过 electron-builder 的 dmgbuild bug） ----
    log "创建 DMG 安装包..."
    local dmg_path="$ELECTRON_DIR/release/$PRODUCT_NAME-${package_version}-${BUILD_ARCH}.dmg"
    local zip_path="$ELECTRON_DIR/release/$PRODUCT_NAME-${package_version}-${BUILD_ARCH}.zip"
    rm -f "$dmg_path"

    # Squirrel.Mac 的自动更新必须使用 ZIP；DMG 只负责首次安装。这里始终
    # 从已经完成签名和冒烟验证的最终 .app 重建 ZIP，不能复用
    # electron-builder 在签名步骤之前留下的同名压缩包。
    rm -f "$zip_path"
    if ! ditto -c -k --keepParent "$app_path" "$zip_path" 2>/dev/null; then
        rm -f "$zip_path"
        err "无法创建 macOS 自动更新 ZIP: $zip_path"
        exit 1
    fi
    if [ ! -s "$zip_path" ] || ! unzip -tqq "$zip_path" >/dev/null 2>&1; then
        rm -f "$zip_path"
        err "macOS 自动更新 ZIP 校验失败: $zip_path"
        exit 1
    fi
    echo "  自动更新 ZIP: $zip_path"

    # 创建临时 DMG 目录
    local dmg_staging
    dmg_staging="$(mktemp -d /tmp/wordtts_dmg_staging.XXXXXX)"
    # 复制 .app 和 Applications 链接
    cp -R "$app_path" "$dmg_staging/"
    ln -s /Applications "$dmg_staging/Applications"

    # 创建 DMG（带重试，CI 环境偶发 hdiutil 失败）
    local dmg_created=false
    for attempt in 1 2 3; do
        if hdiutil create \
            -volname "$PRODUCT_NAME" \
            -srcfolder "$dmg_staging" \
            -ov -format UDZO \
            "$dmg_path"; then
            dmg_created=true
            break
        fi
        warn "hdiutil 第 $attempt 次创建 DMG 失败，重试..."
        rm -f "$dmg_path" 2>/dev/null || true
        sleep 3
    done

    rm -rf "$dmg_staging"

    if [ "$dmg_created" = true ] && [ -f "$dmg_path" ]; then
        # 清除 DMG 的隔离属性
        xattr -cr "$dmg_path" 2>/dev/null || true
        echo "  DMG 创建完成 ✓"
        echo "  位置: $dmg_path"
        echo "  大小: $(du -sh "$dmg_path" | awk '{print $1}')"
    else
        warn "DMG 创建失败，改用 zip 打包 .app 作为分发产物"
        if [ -f "$zip_path" ]; then
            echo "  ZIP 创建完成 ✓"
            echo "  位置: $zip_path"
            echo "  大小: $(du -sh "$zip_path" | awk '{print $1}')"
        else
            err "DMG 和 ZIP 均创建失败"
            exit 1
        fi
    fi

    log "打包完成 ✓"

    # 提示用户如何绕过 Gatekeeper
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────────┐"
    echo "  │ ⚠ 分发提示：未签名应用会被 macOS Gatekeeper 拦截              │"
    echo "  │                                                             │"
    echo "  │ 用户首次打开时，请右键点击 $PRODUCT_NAME.app → 选择「打开」   │"
    echo "  │ 在弹窗中点击「打开」即可绕过 Gatekeeper                       │"
    echo "  │                                                             │"
    echo "  │ 或在终端运行: xattr -cr /Applications/$PRODUCT_NAME.app      │"
    echo "  └─────────────────────────────────────────────────────────────┘"
}

# ============================================================================
# 主流程
# ============================================================================
main() {
    echo ""
    echo "=========================================="
    echo "  $PRODUCT_NAME — Electron 混合打包"
    echo "=========================================="
    echo ""

    local mode="${1:-all}"

    check_environment

    case "$mode" in
        --python)
            build_python_backend
            ;;
        --electron)
            # 即使调用者只想打 Electron 壳，也必须先重建后端；否则很容易
            # 把旧的 server_backend_build 装进新前端，运行时悄悄退回逐条流程。
            build_python_backend
            build_electron_app
            ;;
        all|"")
            build_python_backend
            build_electron_app
            ;;
        *)
            err "未知参数: $mode"
            echo "用法: bash build_electron.sh [--python|--electron]"
            exit 1
            ;;
    esac

    echo ""
    log "全部完成！"
    echo "  DMG 位置: $ELECTRON_DIR/release/"
    echo ""
    echo "  分发: 将 .dmg 发给用户，拖入 Applications 即可使用"
    echo "  无需安装 Python 或 Node.js"
    echo ""
}

main "$@"
