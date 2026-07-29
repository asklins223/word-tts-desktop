#!/bin/bash
# ============================================================================
# Word → TTS — Electron 混合打包脚本
# ============================================================================
# 产出: electron/release/WordTTS-<version>.dmg
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

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[构建]${NC} $1"; }
warn() { echo -e "${YELLOW}[警告]${NC} $1"; }
err()  { echo -e "${RED}[错误]${NC} $1"; }

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

    # 验证产物
    if [ ! -f "$PYINSTALLER_DIST/server_backend/server_backend" ]; then
        err "PyInstaller 打包失败：未找到 server_backend 可执行文件"
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
    rm -rf "$ELECTRON_DIR/release/mac-arm64" "$ELECTRON_DIR/release/mac" 2>/dev/null || true

    # 确保后端二进制有执行权限
    chmod +x "$ELECTRON_DIR/server_backend_build/server_backend/server_backend" 2>/dev/null || true

    cd "$ELECTRON_DIR"
    npx electron-builder --mac

    # 找到构建产物 .app（dir 模式输出到 mac-arm64/ 或 mac/）
    local app_path=""
    for candidate in \
        "$ELECTRON_DIR/release/mac-arm64/WordTTS.app" \
        "$ELECTRON_DIR/release/mac/WordTTS.app"; do
        if [ -d "$candidate" ]; then
            app_path="$candidate"
            break
        fi
    done
    if [ -z "$app_path" ]; then
        app_path=$(find "$ELECTRON_DIR/release" -name "WordTTS.app" -type d 2>/dev/null | head -1)
    fi

    if [ -z "$app_path" ] || [ ! -d "$app_path" ]; then
        err "未找到构建产物 WordTTS.app"
        exit 1
    fi

    log "构建产物: $app_path"

    # ---- ad-hoc 签名（从内到外）----
    log "对 .app 进行 ad-hoc 签名..."
    xattr -cr "$app_path" 2>/dev/null || true

    local fw_dir="$app_path/Contents/Frameworks"

    # 1. 签 server_backend 内部的动态库和可执行文件
    local backend_dir="$app_path/Contents/Resources/server_backend"
    if [ -d "$backend_dir" ]; then
        find "$backend_dir" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.pyd" \) \
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

    # 验证签名
    if codesign --verify --deep --strict "$app_path" 2>/dev/null; then
        echo "  签名验证通过 ✓"
    else
        warn "签名验证未通过（ad-hoc 签名可能被 Gatekeeper 拦截）"
    fi

    # ---- 手动创建 DMG（绕过 electron-builder 的 dmgbuild bug） ----
    log "创建 DMG 安装包..."
    local dmg_path="$ELECTRON_DIR/release/WordTTS.dmg"
    rm -f "$dmg_path"

    # 创建临时 DMG 目录
    local dmg_staging="/tmp/wordtts_dmg_staging"
    rm -rf "$dmg_staging"
    mkdir -p "$dmg_staging"
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
