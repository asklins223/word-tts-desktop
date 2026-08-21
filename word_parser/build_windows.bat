@echo off
REM ============================================================================
REM  Word 文档解析工具 — Windows 打包脚本
REM ============================================================================
REM  生成 WordParser.exe 桌面应用（使用 pywebview 原生窗口）
REM
REM  用法:
REM    word_parser\build_windows.bat
REM
REM  前置条件:
REM    pip install -r requirements_app.txt
REM ============================================================================

REM 确定项目根目录
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."

echo ================================================
echo   Word 文档解析工具 — Windows 打包
echo ================================================

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.10+
    pause
    exit /b 1
)

REM 检查 PyInstaller
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [安装] PyInstaller 未安装，正在安装...
    pip install pyinstaller
)

REM 检查依赖
echo [检查] 依赖包...
python -c "import gradio; print('  gradio', gradio.__version__)"
python -c "import docx; print('  python-docx OK')"
python -c "import webview; print('  pywebview', webview.__version__)" 2>nul
if errorlevel 1 (
    echo [安装] pywebview 未安装，正在安装...
    pip install pywebview
)

REM 清理旧构建
echo [清理] 旧构建文件...
if exist "%PROJECT_ROOT%\build\WordParser" rmdir /s /q "%PROJECT_ROOT%\build\WordParser"
if exist "%PROJECT_ROOT%\dist\WordParser" rmdir /s /q "%PROJECT_ROOT%\dist\WordParser"
if exist "%SCRIPT_DIR%WordParser.spec" del /q "%SCRIPT_DIR%WordParser.spec"

REM 打包
echo [打包] 开始构建...
cd /d "%SCRIPT_DIR%"
pyinstaller ^
    --noconfirm ^
    --name "WordParser" ^
    --windowed ^
    --distpath "%PROJECT_ROOT%\dist" ^
    --workpath "%PROJECT_ROOT%\build\WordParser" ^
    --collect-all gradio ^
    --collect-all gradio_client ^
    --collect-all docx ^
    --collect-all openpyxl ^
    --collect-all lxml ^
    --collect-all PIL ^
    --collect-all numpy ^
    --collect-all pydantic ^
    --collect-all fastapi ^
    --collect-all uvicorn ^
    --collect-all markdown_it ^
    --collect-all mdit_py_plugins ^
    --collect-all safehttpx ^
    --collect-all groovy ^
    --collect-all ffmpy ^
    --collect-all httpx ^
    --collect-all starlette ^
    --collect-all anyio ^
    --collect-all h11 ^
    --collect-all certifi ^
    --collect-all Jinja2 ^
    --collect-all webview ^
    --collect-all proxy_tools ^
    --collect-all bottle ^
    --hidden-import docx ^
    --hidden-import lxml.etree ^
    --exclude-module mysql ^
    --exclude-module mysql_mcp_server ^
    --exclude-module pymysql ^
    --exclude-module mysql.connector ^
    --exclude-module aiomysql ^
    --exclude-module sqlalchemy ^
    --exclude-module alembic ^
    --exclude-module redis ^
    --exclude-module celery ^
    --exclude-module pytest ^
    --exclude-module playwright ^
    --exclude-module IPython ^
    --exclude-module notebook ^
    --exclude-module jupyter ^
    --exclude-module cv2 ^
    --exclude-module sklearn ^
    --exclude-module scipy ^
    --exclude-module torch ^
    --exclude-module tensorflow ^
    --add-data "word_parser.py;." ^
    word_parser_app.py

echo.
echo ================================================
echo   打包完成！
echo   应用位置: %PROJECT_ROOT%\dist\WordParser\WordParser.exe
echo.
echo   双击 WordParser.exe 即可启动
echo   首次启动可能需要 10-20 秒（初始化环境）
echo ================================================
pause
