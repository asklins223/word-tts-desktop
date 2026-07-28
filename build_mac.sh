#!/bin/bash
# ============================================================================
# Word → TTS 一体化工具 — macOS 打包脚本
# ============================================================================
# 生成 WordTTS.app 桌面应用
#
# 用法:
#   bash build_mac.sh           → 构建 .app
#   bash build_mac.sh --dmg     → 同时创建 .dmg 安装包
#
# 前置条件:
#   pip install gradio edge-tts pydub python-docx pyinstaller
#   brew install ffmpeg
# ============================================================================

set -e

# 确定项目根目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

echo "================================================"
echo "  Word → TTS 一体化工具 — macOS 打包"
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
python3 -c "import gradio; print(f'  gradio {gradio.__version__}')"
python3 -c "import edge_tts; print(f'  edge_tts {edge_tts.__version__}')"
python3 -c "import pydub; print('  pydub OK')"
python3 -c "import docx; print('  python-docx OK')"
python3 -c "import aiohttp; print(f'  aiohttp {aiohttp.__version__}')"
python3 -c "import webview; print(f'  pywebview {webview.__version__}')" 2>/dev/null || { echo "[安装] pywebview 未安装，正在安装..."; pip3 install pywebview; }
python3 -c "import imageio_ffmpeg; print('  imageio-ffmpeg OK')" 2>/dev/null || { echo "[安装] imageio-ffmpeg 未安装，正在安装..."; pip3 install imageio-ffmpeg; }

# 检查 ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "[警告] 未找到 ffmpeg，音频导出功能将不可用"
    echo "        请安装: brew install ffmpeg"
else
    echo "  ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
fi

# 清理旧构建
echo "[清理] 旧构建文件..."
rm -rf "$PROJECT_ROOT/build/WordTTS" "$PROJECT_ROOT/dist/WordTTS.app" "$PROJECT_ROOT/dist/WordTTS.dmg" "$PROJECT_ROOT/WordTTS.spec" 2>/dev/null || true

# 打包
echo "[打包] 开始构建..."
cd "$PROJECT_ROOT"

pyinstaller \
    --noconfirm \
    --name "WordTTS" \
    --windowed \
    --distpath "$PROJECT_ROOT/dist" \
    --workpath "$PROJECT_ROOT/build/WordTTS" \
    --collect-all gradio \
    --collect-all gradio_client \
    --collect-all edge_tts \
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
    --collect-all aiohttp \
    --collect-all aiosignal \
    --collect-all frozenlist \
    --collect-all multidict \
    --collect-all yarl \
    --collect-all async_timeout \
    --collect-all webview \
    --collect-all proxy_tools \
    --collect-all bottle \
    --collect-all imageio_ffmpeg \
    --hidden-import docx \
    --hidden-import lxml.etree \
    --hidden-import edge_tts \
    --hidden-import pydub \
    --hidden-import aiohttp \
    --add-data "word_parser/word_parser.py:word_parser" \
    --add-data "edge_tts/voice_match_788.py:edge_tts" \
    --add-data "edge_tts/voice_profiles/:edge_tts/voice_profiles/" \
    --add-data "edge_tts/bgm/:edge_tts/bgm/" \
    --hidden-import voice_match_788 \
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
    word_tts_app.py

echo ""
echo "================================================"
echo "  打包完成！"
echo "  应用位置: $PROJECT_ROOT/dist/WordTTS.app"
echo ""
echo "  双击 WordTTS.app 即可启动"
echo "  首次启动可能需要 10-20 秒（初始化环境）"
echo "  将以原生窗口模式打开（无需浏览器）"
echo "================================================"

# 可选：创建 DMG
if [ "$1" == "--dmg" ]; then
    echo "[DMG] 正在创建 DMG 安装包..."
    if command -v hdiutil &> /dev/null; then
        hdiutil create -volname "WordTTS" -srcfolder "$PROJECT_ROOT/dist/WordTTS.app" -ov -format UDZO "$PROJECT_ROOT/dist/WordTTS.dmg"
        echo "  DMG 位置: $PROJECT_ROOT/dist/WordTTS.dmg"
    else
        echo "  [跳过] hdiutil 不可用"
    fi
fi
