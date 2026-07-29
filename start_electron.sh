#!/bin/bash
# ============================================================
# Word → TTS Electron 启动脚本
# 用法: ./start_electron.sh
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ELECTRON_DIR="$PROJECT_DIR/electron"

echo "=========================================="
echo "  Word → TTS Electron 应用启动"
echo "=========================================="

# 1. 检查 Node.js
if ! command -v node &> /dev/null; then
    echo "[错误] 未找到 Node.js，请先安装: https://nodejs.org"
    exit 1
fi

# 2. 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 python3"
    exit 1
fi

# 3. 安装 Electron（如果尚未安装）
if [ ! -d "$ELECTRON_DIR/node_modules/electron/dist/Electron.app" ]; then
    echo "[1/3] 安装 Electron..."
    cd "$ELECTRON_DIR"
    npm install
    node node_modules/electron/install.js
    cd "$PROJECT_DIR"
else
    echo "[1/3] Electron 已安装 ✓"
fi

# 4. 去除隔离属性 + ad-hoc 签名（macOS Gatekeeper）
ELECTRON_APP="$ELECTRON_DIR/node_modules/electron/dist/Electron.app"
if [ -d "$ELECTRON_APP" ]; then
    xattr -cr "$ELECTRON_APP" 2>/dev/null || true
    codesign --force --deep --sign - "$ELECTRON_APP" 2>/dev/null || true
    echo "[2/3] 已签名并清除隔离属性 ✓"
fi

# 5. 检查 Python 依赖
echo "[3/3] 检查 Python 依赖..."
python3 -c "import fastapi, uvicorn" 2>/dev/null || {
    echo "  安装 FastAPI 和 uvicorn..."
    pip3 install fastapi uvicorn
}
python3 -c "import playwright, ddddocr, onnxruntime" 2>/dev/null || {
    echo "  安装 TTSMaker 依赖 (playwright + ddddocr)..."
    pip3 install playwright ddddocr onnxruntime greenlet pyee
}
# 开发模式下需要 Chromium 浏览器（打包后内置，开发时需手动安装）
_pw_cache="$(python3 -c "import os; print(os.path.join(os.path.expanduser('~'), 'Library', 'Caches', 'ms-playwright'))" 2>/dev/null)"
if [ -n "$_pw_cache" ] && ! ls "$_pw_cache"/chromium-* 1>/dev/null 2>&1; then
    echo "  安装 Playwright Chromium 浏览器..."
    python3 -m playwright install chromium
fi
echo "  Python 依赖就绪 ✓"

# 6. 启动 — 关键：清除 ELECTRON_RUN_AS_NODE
echo "启动应用..."
cd "$ELECTRON_DIR"
unset ELECTRON_RUN_AS_NODE
export ELECTRON_RUN_AS_NODE=
./node_modules/electron/dist/Electron.app/Contents/MacOS/Electron . "$@"

# 清理
pkill -f "python3 server.py" 2>/dev/null || true
