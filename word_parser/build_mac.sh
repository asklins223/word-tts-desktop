#!/bin/bash
# ============================================================================
# Word 文档解析工具 — macOS 打包脚本
# ============================================================================
# 生成 WordParser.app 桌面应用
#
# 用法:
#   bash word_parser/build_mac.sh
#
# 前置条件:
#   pip install -r requirements_app.txt
# ============================================================================

set -e

# 确定项目根目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "================================================"
echo "  Word 文档解析工具 — macOS 打包"
echo "================================================"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

# 检查 PyInstaller
if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "[安装] PyInstaller 未安装，正在安装..."
    pip3 install pyinstaller
fi

# 检查依赖
echo "[检查] 依赖包..."
python3 -c "import gradio; import docx; print(f'  gradio {gradio.__version__}')"
echo "  python-docx OK"
python3 -c "import webview; print(f'  pywebview {webview.__version__}')" 2>/dev/null || { echo "[安装] pywebview 未安装，正在安装..."; pip3 install pywebview; }

# 清理旧构建
echo "[清理] 旧构建文件..."
rm -rf "$PROJECT_ROOT/build/WordParser" "$PROJECT_ROOT/dist/WordParser.app" "$SCRIPT_DIR/word_parser_app.spec" 2>/dev/null || true

# 打包
echo "[打包] 开始构建..."
cd "$SCRIPT_DIR"
pyinstaller \
    --noconfirm \
    --name "WordParser" \
    --windowed \
    --distpath "$PROJECT_ROOT/dist" \
    --workpath "$PROJECT_ROOT/build/WordParser" \
    --collect-all gradio \
    --collect-all gradio_client \
    --collect-all docx \
    --collect-all lxml \
    --collect-all PIL \
    --collect-all numpy \
    --collect-all pydantic \
    --collect-all fastapi \
    --collect-all uvicorn \
    --collect-all markdown_it \
    --collect-all mdit_py_plugins \
    --collect-all safehttpx \
    --collect-all groovy \
    --collect-all ffmpy \
    --collect-all httpx \
    --collect-all starlette \
    --collect-all anyio \
    --collect-all h11 \
    --collect-all certifi \
    --collect-all Jinja2 \
    --collect-all webview \
    --collect-all proxy_tools \
    --collect-all bottle \
    --hidden-import docx \
    --hidden-import lxml.etree \
    --exclude-module mysql \
    --exclude-module mysql_mcp_server \
    --exclude-module pymysql \
    --exclude-module mysql.connector \
    --exclude-module aiomysql \
    --exclude-module sqlalchemy \
    --exclude-module alembic \
    --exclude-module redis \
    --exclude-module celery \
    --exclude-module pytest \
    --exclude-module playwright \
    --exclude-module IPython \
    --exclude-module notebook \
    --exclude-module jupyter \
    --exclude-module cv2 \
    --exclude-module sklearn \
    --exclude-module scipy \
    --exclude-module torch \
    --exclude-module tensorflow \
    --add-data "word_parser.py:." \
    word_parser_app.py

echo ""
echo "================================================"
echo "  打包完成！"
echo "  应用位置: $PROJECT_ROOT/dist/WordParser.app"
echo ""
echo "  双击 WordParser.app 即可启动"
echo "  首次启动可能需要 10-20 秒（初始化环境）"
echo "================================================"

# 可选：创建 DMG
if [ "$1" == "--dmg" ]; then
    echo "[DMG] 正在创建 DMG 安装包..."
    if command -v hdiutil &> /dev/null; then
        hdiutil create -volname "WordParser" -srcfolder "$PROJECT_ROOT/dist/WordParser.app" -ov -format UDZO "$PROJECT_ROOT/dist/WordParser.dmg"
        echo "  DMG 位置: $PROJECT_ROOT/dist/WordParser.dmg"
    else
        echo "  [跳过] hdiutil 不可用"
    fi
fi
