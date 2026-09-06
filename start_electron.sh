#!/bin/bash
# ============================================================
# 小猪wordTTS Electron 启动脚本
# 用法: ./start_electron.sh
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ELECTRON_DIR="$PROJECT_DIR/electron"

# Keep the development backend and the dependency bootstrap on the same
# interpreter. The Word block-image flow needs Playwright and Pillow; using a
# system Homebrew Python for the server while installing packages into a
# project venv makes ordinary DOCX parsing pass and fails later at capture.
PYTHON_BIN="${PYTHON_CMD:-}"
if [ -z "$PYTHON_BIN" ] && [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
fi
if [ -z "$PYTHON_BIN" ] && [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
fi
if [ -z "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -n "$PYTHON_BIN" ] && [ ! -x "$PYTHON_BIN" ]; then
    resolved_python="$(command -v "$PYTHON_BIN" || true)"
    if [ -n "$resolved_python" ]; then
        PYTHON_BIN="$resolved_python"
    fi
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    echo "[错误] 未找到可用的 Python 3"
    exit 1
fi
export PYTHON_CMD="$PYTHON_BIN"

echo "=========================================="
echo "  小猪wordTTS Electron 应用启动"
echo "=========================================="

# 1. 检查 Node.js。Electron 依赖明确要求 Node 24；如果系统 PATH
# 仍然指向 Node 22，继续启动只会把错误推迟到 npm/Electron 运行阶段。
NODE_BIN="${NODE_CMD:-}"
if [ -z "$NODE_BIN" ]; then
    for candidate in \
        "/opt/homebrew/opt/node@24/bin/node" \
        "/usr/local/opt/node@24/bin/node"
    do
        if [ -x "$candidate" ]; then
            NODE_BIN="$candidate"
            break
        fi
    done
fi
if [ -z "$NODE_BIN" ]; then
    NODE_BIN="$(command -v node || true)"
fi
if [ -n "$NODE_BIN" ] && [ ! -x "$NODE_BIN" ]; then
    resolved_node="$(command -v "$NODE_BIN" || true)"
    if [ -n "$resolved_node" ]; then
        NODE_BIN="$resolved_node"
    fi
fi
if [ -z "$NODE_BIN" ] || [ ! -x "$NODE_BIN" ]; then
    echo "[错误] 未找到 Node.js 24，请先安装: https://nodejs.org"
    exit 1
fi
NODE_MAJOR="$("$NODE_BIN" -p "process.versions.node.split('.')[0]" 2>/dev/null || true)"
if [ "$NODE_MAJOR" != "24" ]; then
    echo "[错误] 当前 Node.js 为 ${NODE_MAJOR:-未知版本}，项目要求 Node.js 24.x"
    echo "       可用 NODE_CMD=/path/to/node24 重试"
    exit 1
fi
export PATH="$(dirname "$NODE_BIN"):$PATH"
NODE_VERSION="$("$NODE_BIN" --version)"
echo "  使用 Node.js: $NODE_VERSION"

# 2. 检查 Python
echo "  使用 Python: $PYTHON_BIN"

# 3. 安装/验证 Electron（如果尚未安装或损坏）
ELECTRON_APP="$ELECTRON_DIR/node_modules/electron/dist/Electron.app"
if [ ! -d "$ELECTRON_APP" ]; then
    echo "[1/3] 安装 Electron..."
    cd "$ELECTRON_DIR"
    npm install
    node node_modules/electron/install.js
    cd "$PROJECT_DIR"
else
    echo "[1/3] Electron 已安装 ✓"
fi

# 3.1 修复框架符号链接（防止 dyld 错误）
if [ -d "$ELECTRON_APP/Contents/Frameworks" ]; then
    cd "$ELECTRON_APP/Contents/Frameworks"
    for fw in "Electron Framework" Mantle ReactiveObjC Squirrel; do
        if [ -d "$fw.framework/Versions/A" ]; then
            # 创建 Versions/Current 链接
            [ -e "$fw.framework/Versions/Current" ] || ln -sf A "$fw.framework/Versions/Current"
            # 创建顶层框架二进制链接
            if [ -f "$fw.framework/Versions/A/$fw" ] && [ ! -e "$fw.framework/$fw" ]; then
                ln -sf "Versions/Current/$fw" "$fw.framework/$fw"
            fi
            # 创建 Helpers 和 Libraries 链接（仅 Electron Framework）
            if [ "$fw" = "Electron Framework" ]; then
                [ -e "$fw.framework/Helpers" ] || ln -sf Versions/Current/Helpers "$fw.framework/Helpers"
                [ -e "$fw.framework/Libraries" ] || ln -sf Versions/Current/Libraries "$fw.framework/Libraries"
            fi
            # 创建 Resources 链接
            [ -e "$fw.framework/Resources" ] || ln -sf Versions/Current/Resources "$fw.framework/Resources"
        fi
    done
    cd "$PROJECT_DIR"
fi

# 4. 去除隔离属性 + 签名（macOS Gatekeeper）
# 注意：移除 --deep 标志以避免签名问题
if [ -d "$ELECTRON_APP" ]; then
    echo "[2/3] 清除隔离属性..."
    xattr -cr "$ELECTRON_APP" 2>/dev/null || true

    # 单独签名框架（不使用 --deep）
    FRAMEWORKS_DIR="$ELECTRON_APP/Contents/Frameworks"
    if [ -d "$FRAMEWORKS_DIR" ]; then
        for fw in "$FRAMEWORKS_DIR"/*.framework; do
            if [ -d "$fw" ]; then
                codesign --force --sign - "$fw" 2>/dev/null || true
            fi
        done
        # 签名 Helper 应用
        for helper in "$FRAMEWORKS_DIR"/*.app; do
            if [ -d "$helper" ]; then
                codesign --force --sign - "$helper" 2>/dev/null || true
            fi
        done
    fi
    # 最后签名主应用
    codesign --force --sign - "$ELECTRON_APP" 2>/dev/null || true
    echo "  ✓ 已签名并清除隔离属性 ✓"
fi

# 5. 检查 Python 依赖
echo "[3/3] 检查 Python 依赖..."
"$PYTHON_BIN" -c "import fastapi, uvicorn" 2>/dev/null || {
    echo "  安装 FastAPI 和 uvicorn..."
    "$PYTHON_BIN" -m pip install fastapi uvicorn
}
"$PYTHON_BIN" -c "import playwright" 2>/dev/null || {
    echo "  安装讯飞配音依赖 (Playwright)..."
    "$PYTHON_BIN" -m pip install playwright greenlet pyee
}
# DOCX 表格/浮动图形由上面的 Chromium 直接渲染，Pillow 只负责拼接。
"$PYTHON_BIN" -c "import PIL" 2>/dev/null || {
    echo "  安装 Word 文档块图片依赖 (Pillow)..."
    "$PYTHON_BIN" -m pip install Pillow
}
# 开发模式下需要 Chromium 浏览器（打包后内置，开发时需手动安装）
_pw_cache="$("$PYTHON_BIN" -c "import os; print(os.path.join(os.path.expanduser('~'), 'Library', 'Caches', 'ms-playwright'))" 2>/dev/null)"
if [ -n "$_pw_cache" ] && ! ls "$_pw_cache"/chromium-* 1>/dev/null 2>&1; then
    echo "  安装 Playwright Chromium 浏览器..."
    "$PYTHON_BIN" -m playwright install chromium
fi
echo "  Python 依赖就绪 ✓"

# 6. 启动 — 关键：清除 ELECTRON_RUN_AS_NODE
echo "启动应用..."
cd "$ELECTRON_DIR"
unset ELECTRON_RUN_AS_NODE
export ELECTRON_RUN_AS_NODE=
exec "./node_modules/electron/dist/Electron.app/Contents/MacOS/Electron" . "$@"

# 清理
pkill -f "python3 server.py" 2>/dev/null || true
