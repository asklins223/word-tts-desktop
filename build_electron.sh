#!/bin/bash
# ============================================================================
# Word → TTS — Electron 混合打包脚本
# ============================================================================
# 产出: electron/release/WordTTS-<version>-<arch>.dmg
#
# 流程:
#   1. PyInstaller 打包 server.py → server_backend/
#   2. electron-builder 打包 Electron 壳 + server_backend → .dmg
#
# 用法:
#   bash build_electron.sh              → 完整构建（PyInstaller + electron-builder）
#   bash build_electron.sh --python     → 仅构建 Python 后端
#   bash build_electron.sh --electron   → 仅构建 Electron 壳（需先 --python）
#
# 前置条件:
#   pip install fastapi uvicorn edge-tts pydub python-docx pyinstaller imageio-ffmpeg
#   cd electron && npm install
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ELECTRON_DIR="$SCRIPT_DIR/electron"

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
    echo "  Python: $(python3 --version)"

    # electron-builder 的 DMG 打包工具需要 `python` 命令（非 python3）
    # macOS Homebrew 默认只安装 python3，需要创建符号链接
    if ! command -v python &> /dev/null; then
        PYTHON3_PATH="$(command -v python3)"
        if [ -n "$PYTHON3_PATH" ]; then
            # 优先尝试 /usr/local/bin（需要权限）
            if [ -w /usr/local/bin ] || [ -w "$(dirname /usr/local/bin)" ]; then
                ln -sf "$PYTHON3_PATH" /usr/local/bin/python 2>/dev/null && echo "  已创建 /usr/local/bin/python 链接"
            fi
            # 如果仍不可用，创建临时 bin 目录并加入 PATH
            if ! command -v python &> /dev/null; then
                TMP_BIN="$SCRIPT_DIR/.build_bin"
                mkdir -p "$TMP_BIN"
                ln -sf "$PYTHON3_PATH" "$TMP_BIN/python"
                export PATH="$TMP_BIN:$PATH"
                echo "  已创建临时 python 链接: $TMP_BIN/python"
            fi
        fi
    fi

    # PyInstaller
    if ! python3 -c "import PyInstaller" 2>/dev/null; then
        warn "PyInstaller 未安装，正在安装..."
        pip3 install pyinstaller
    fi

    # Node.js
    if ! command -v node &> /dev/null; then
        err "未找到 Node.js，请先安装: https://nodejs.org"
        exit 1
    fi
    echo "  Node.js: $(node --version)"

    # electron-builder
    if [ ! -d "$ELECTRON_DIR/node_modules/electron-builder" ]; then
        warn "electron-builder 未安装，正在安装..."
        cd "$ELECTRON_DIR"
        npm install
        cd "$SCRIPT_DIR"
    fi

    # 关键 Python 依赖
    log "检查 Python 依赖..."
    python3 -c "import fastapi, uvicorn, edge_tts, pydub, docx, aiohttp" 2>/dev/null || {
        err "缺少关键 Python 依赖，请运行: pip3 install -r requirements_app.txt"
        exit 1
    }
    python3 -c "import imageio_ffmpeg" 2>/dev/null || {
        warn "imageio-ffmpeg 未安装，正在安装..."
        pip3 install imageio-ffmpeg
    }

    # Playwright + ddddocr（TTSMaker 男声生成依赖）
    python3 -c "import playwright, ddddocr, onnxruntime" 2>/dev/null || {
        warn "TTSMaker 依赖未安装，正在安装 playwright + ddddocr..."
        pip3 install playwright ddddocr onnxruntime greenlet pyee
    }

    # Playwright Chromium 浏览器二进制（打包内置必须先下载）
    _pw_cache="$(python3 -c "import os; print(os.path.join(os.path.expanduser('~'), 'Library', 'Caches', 'ms-playwright'))" 2>/dev/null)"
    if [ -z "$_pw_cache" ] || ! ls "$_pw_cache"/chromium-* 1>/dev/null 2>&1; then
        warn "Playwright Chromium 未下载，正在安装..."
        python3 -m playwright install chromium
    fi

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
    pyinstaller server_pyinstaller.spec \
        --noconfirm \
        --distpath "$PYINSTALLER_DIST" \
        --workpath "$PYINSTALLER_WORK"

    # Chromium 是一个已签名的嵌套应用，必须在 PyInstaller 后原样复制，
    # 否则 PyInstaller 的二进制重签会破坏其 Framework bundle。
    node "$SCRIPT_DIR/scripts/stage_playwright_browser.js" \
        "$PYINSTALLER_DIST/server_backend"
    local browser_app
    browser_app="$(find "$PYINSTALLER_DIST/server_backend/_internal/playwright_browsers" \
        -maxdepth 4 -type d -name "Google Chrome for Testing.app" 2>/dev/null | head -1)"
    if [ -z "$browser_app" ]; then
        err "内置 Playwright Chromium 不完整：未找到 Google Chrome for Testing.app"
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

    log "生成 macOS / Windows 应用图标..."
    python3 "$SCRIPT_DIR/scripts/build_app_icons.py"

    # 确认后端产物存在
    if [ ! -d "$ELECTRON_DIR/server_backend_build/server_backend" ]; then
        err "未找到后端产物，请先运行: bash build_electron.sh --python"
        exit 1
    fi

    # 清理上次失败构建可能残留的挂载点
    log "清理残留挂载点..."
    for vol in "/Volumes/WordTTS" "/Volumes/WordTTS 1.0.0" "/Volumes/WordTTS-1.0.0"; do
        hdiutil detach -force "$vol" 2>/dev/null || true
    done
    # 清理旧的 release 目录
    rm -rf "$ELECTRON_DIR/release/$MAC_OUTPUT_DIR" 2>/dev/null || true

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

    # package.json 给出产品期望的最低版本，但本机构建环境里的 Python、OpenSSL、
    # ONNX Runtime 或 Chromium 可能要求更高版本。扫描所有内置 Mach-O，避免
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

    cd "$ELECTRON_DIR"
    npx electron-builder --mac "$BUILDER_ARCH_FLAG" \
        -c.mac.minimumSystemVersion="$effective_macos_min"

    # 只接受本次目标架构对应的目录，禁止从旧目录误拿另一架构产物。
    local app_path="$ELECTRON_DIR/release/$MAC_OUTPUT_DIR/WordTTS.app"

    if [ -z "$app_path" ] || [ ! -d "$app_path" ]; then
        err "未找到构建产物 WordTTS.app"
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
    app_info="$(file "$app_path/Contents/MacOS/WordTTS")"
    if [ "$BUILD_ARCH" = "arm64" ] && [[ "$app_info" != *"arm64"* ]]; then
        err "Electron 主程序架构不匹配: $app_info"
        exit 1
    fi
    if [ "$BUILD_ARCH" = "x64" ] && [[ "$app_info" != *"x86_64"* ]]; then
        err "Electron 主程序架构不匹配: $app_info"
        exit 1
    fi

    # ---- ad-hoc 签名（从内到外）----
    # electron-builder 已使用 Developer ID 时必须保留其签名，不能再被 ad-hoc 覆盖。
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

    # 5. 签主应用
    codesign --force --sign - "$app_path" 2>/dev/null || true
    xattr -cr "$app_path" 2>/dev/null || true
        echo "  ad-hoc 签名完成 ✓"
    fi

    # 验证签名
    if codesign --verify --deep --strict "$app_path" 2>/dev/null; then
        echo "  签名验证通过 ✓"
    else
        warn "签名验证未通过（ad-hoc 签名可能被 Gatekeeper 拦截）"
    fi

    # ---- 手动创建 DMG（绕过 electron-builder 的 dmgbuild bug） ----
    log "创建 DMG 安装包..."
    local package_version
    package_version="$(node -p "require('$ELECTRON_DIR/package.json').version")"
    local dmg_path="$ELECTRON_DIR/release/WordTTS-${package_version}-${BUILD_ARCH}.dmg"
    rm -f "$dmg_path"

    # 创建临时 DMG 目录
    local dmg_staging
    dmg_staging="$(mktemp -d /tmp/wordtts_dmg_staging.XXXXXX)"
    # 复制 .app 和 Applications 链接
    cp -R "$app_path" "$dmg_staging/"
    ln -s /Applications "$dmg_staging/Applications"

    # 创建 DMG
    hdiutil create \
        -volname "WordTTS" \
        -srcfolder "$dmg_staging" \
        -ov -format UDZO \
        "$dmg_path" 2>/dev/null

    rm -rf "$dmg_staging"

    if [ -f "$dmg_path" ]; then
        # 清除 DMG 的隔离属性
        xattr -cr "$dmg_path" 2>/dev/null || true
        echo "  DMG 创建完成 ✓"
        echo "  位置: $dmg_path"
        echo "  大小: $(du -sh "$dmg_path" | awk '{print $1}')"
    else
        warn "DMG 创建失败，可直接使用 .app: $app_path"
    fi

    log "打包完成 ✓"

    # 提示用户如何绕过 Gatekeeper
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────────┐"
    echo "  │ ⚠ 分发提示：未签名应用会被 macOS Gatekeeper 拦截              │"
    echo "  │                                                             │"
    echo "  │ 用户首次打开时，请右键点击 WordTTS.app → 选择「打开」         │"
    echo "  │ 在弹窗中点击「打开」即可绕过 Gatekeeper                       │"
    echo "  │                                                             │"
    echo "  │ 或在终端运行: xattr -cr /Applications/WordTTS.app           │"
    echo "  └─────────────────────────────────────────────────────────────┘"
}

# ============================================================================
# 主流程
# ============================================================================
main() {
    echo ""
    echo "=========================================="
    echo "  Word → TTS — Electron 混合打包"
    echo "=========================================="
    echo ""

    local mode="${1:-all}"

    check_environment

    case "$mode" in
        --python)
            build_python_backend
            ;;
        --electron)
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
