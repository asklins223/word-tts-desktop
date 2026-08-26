@echo off
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
REM ============================================================================
REM 小猪wordTTS -- Electron 混合打包脚本 (Windows)
REM ============================================================================
REM 产出: electron\release\小猪wordTTS-Setup-<version>-x64.exe (NSIS 安装包)
REM
REM 流程:
REM   1. PyInstaller 打包 server.py -> server_backend\ (server_backend.exe)
REM   2. electron-builder 打包 Electron 壳 + server_backend -> .exe (NSIS)
REM
REM 用法:
REM   build_electron_windows.bat              -> 完整构建 (PyInstaller + electron-builder)
REM   build_electron_windows.bat --python     -> 仅构建 Python 后端
REM   build_electron_windows.bat --electron   -> 重建当前 Python 后端并打包 Electron 壳
REM
REM 前置条件（脚本会同步 Python 依赖）:
REM   建议先创建并激活独立 Python venv
REM   cd electron && npm install
REM ============================================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "ELECTRON_DIR=%SCRIPT_DIR%\electron"
set "REQUIREMENTS_FILE=%SCRIPT_DIR%\requirements_electron.txt"
set "PRODUCT_NAME=小猪wordTTS"

REM ============================================================================
REM 颜色 / 日志
REM ============================================================================
REM Windows CMD 不支持 ANSI 颜色（旧版），使用简单前缀
goto :main

:log
echo [构建] %~1
goto :eof

:warn
echo [警告] %~1
goto :eof

:err
echo [错误] %~1
goto :eof

REM ============================================================================
REM 环境检查
REM ============================================================================
:check_environment
call :log "检查构建环境..."

REM ---- Python ----
where python >nul 2>&1
if !errorlevel! neq 0 (
    call :err "未找到 python，请先安装 Python 3.10+ 并加入 PATH"
    echo   下载地址: https://www.python.org/downloads/
    echo   安装时请勾选 "Add Python to PATH"
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
echo   Python: !PY_VER!

set "ISOLATED_PYTHON=1"
python -c "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)" >nul 2>&1
if !errorlevel! neq 0 (
    set "ISOLATED_PYTHON=0"
    call :warn "当前未使用 Python 虚拟环境；建议在独立 venv 中构建，避免全局可选依赖影响分析结果"
)

REM ---- Electron 专用 Python 依赖 ----
if not exist "%REQUIREMENTS_FILE%" (
    call :err "缺少依赖清单: %REQUIREMENTS_FILE%"
    exit /b 1
)
call :log "同步 Electron Python 构建依赖..."
python -m pip install --disable-pip-version-check -r "%REQUIREMENTS_FILE%"
if !errorlevel! neq 0 (
    call :err "安装 Electron Python 构建依赖失败"
    exit /b 1
)
if "!ISOLATED_PYTHON!"=="1" (
    python -m pip check
    if !errorlevel! neq 0 (
        call :err "Python 依赖冲突，请在独立虚拟环境中重新构建"
        exit /b 1
    )
)
python -c "from importlib.metadata import version; raise SystemExit(0 if version('playwright') == '1.56.0' else 1)" >nul 2>&1
if !errorlevel! neq 0 (
    call :err "Playwright 版本必须为 1.56.0"
    exit /b 1
)

REM ---- Node.js ----
where node >nul 2>&1
if !errorlevel! neq 0 (
    call :err "未找到 Node.js，请先安装: https://nodejs.org"
    exit /b 1
)
for /f "delims=" %%v in ('node --version 2^>^&1') do set "NODE_VER=%%v"
echo   Node.js: !NODE_VER!

REM ---- electron-builder ----
if not exist "%ELECTRON_DIR%\node_modules\electron-builder" (
    call :warn "electron-builder 未安装，正在安装..."
    pushd "%ELECTRON_DIR%"
    call npm install
    popd
)

REM 此命令幂等，只会补齐 Playwright 1.56.0 所要求的准确 Chromium revision。
call :log "检查 Playwright Chromium..."
python -m playwright install chromium
if !errorlevel! neq 0 (
    call :err "安装 Playwright Chromium 失败"
    exit /b 1
)

echo   环境检查通过 OK
goto :eof

REM ============================================================================
REM 步骤 1: PyInstaller 打包后端
REM ============================================================================
:build_python_backend
call :log "=== 步骤 1/2: PyInstaller 打包后端 ==="

set "PYINSTALLER_DIST=%ELECTRON_DIR%\server_backend_build"
set "PYINSTALLER_WORK=%ELECTRON_DIR%\server_backend_build_tmp"

REM 清理旧构建
call :log "清理旧构建..."
if exist "%PYINSTALLER_DIST%" rmdir /s /q "%PYINSTALLER_DIST%"
if exist "%PYINSTALLER_WORK%" rmdir /s /q "%PYINSTALLER_WORK%"

pushd "%SCRIPT_DIR%"
python -m PyInstaller server_pyinstaller.spec ^
    --noconfirm ^
    --distpath "%PYINSTALLER_DIST%" ^
    --workpath "%PYINSTALLER_WORK%"
set "BUILD_EXIT=!errorlevel!"
popd

if !BUILD_EXIT! neq 0 (
    call :err "PyInstaller 打包失败 (exit code: !BUILD_EXIT!)"
    exit /b 1
)

node "%SCRIPT_DIR%\scripts\stage_playwright_browser.js" "%PYINSTALLER_DIST%\server_backend"
if !errorlevel! neq 0 (
    call :err "复制 Playwright Chromium 失败"
    exit /b 1
)

REM 验证产物
if not exist "%PYINSTALLER_DIST%\server_backend\server_backend.exe" (
    call :err "PyInstaller 打包失败: 未找到 server_backend.exe 可执行文件"
    exit /b 1
)

REM 清理临时目录
if exist "%PYINSTALLER_WORK%" rmdir /s /q "%PYINSTALLER_WORK%"

call :log "后端打包完成 OK"
echo   产物位置: %PYINSTALLER_DIST%\server_backend\
goto :eof

REM ============================================================================
REM 步骤 2: electron-builder 打包前端 + 后端
REM ============================================================================
:build_electron_app
call :log "=== 步骤 2/2: electron-builder 打包应用 ==="

call :log "生成 macOS / Windows 应用图标..."
python "%SCRIPT_DIR%\scripts\build_app_icons.py"
if !errorlevel! neq 0 (
    call :err "应用图标生成失败"
    exit /b 1
)

REM 确认后端产物存在
if not exist "%ELECTRON_DIR%\server_backend_build\server_backend\server_backend.exe" (
    call :err "未找到后端产物，请先运行: build_electron_windows.bat --python"
    exit /b 1
)

REM 清理旧的 release 目录（仅清理 win 相关）
call :log "清理旧构建产物..."
if exist "%ELECTRON_DIR%\release\win-unpacked" rmdir /s /q "%ELECTRON_DIR%\release\win-unpacked"
del /q "%ELECTRON_DIR%\release\!PRODUCT_NAME!-Setup-*.exe" >nul 2>&1

pushd "%ELECTRON_DIR%"
call npx electron-builder --win
set "BUILD_EXIT=!errorlevel!"
popd

if !BUILD_EXIT! neq 0 (
    call :err "electron-builder 打包失败 (exit code: !BUILD_EXIT!)"
    exit /b 1
)

REM 查找构建产物
set "EXE_PATH="
for %%f in ("%ELECTRON_DIR%\release\!PRODUCT_NAME!-Setup-*-x64.exe") do (
    if not defined EXE_PATH set "EXE_PATH=%%f"
)

if not defined EXE_PATH (
    REM 检查 win-unpacked 目录
    if exist "%ELECTRON_DIR%\release\win-unpacked\!PRODUCT_NAME!.exe" (
        call :log "构建产物 (unpacked): %ELECTRON_DIR%\release\win-unpacked\!PRODUCT_NAME!.exe"
        call :warn "未找到 NSIS 安装包，可直接使用 win-unpacked 目录"
    ) else (
        call :err "未找到构建产物"
        exit /b 1
    )
) else (
    call :log "构建产物: !EXE_PATH!"
)

call :log "打包完成 OK"

echo.
echo   +-------------------------------------------------------------+
echo   ^| 分发提示：NSIS 安装包可直接分发给 Windows 用户              ^|
echo   ^|                                                             ^|
echo   ^| 用户双击 .exe 安装包即可安装，无需安装 Python 或 Node.js    ^|
echo   ^| 安装后从开始菜单启动小猪wordTTS                              ^|
echo   ^|                                                             ^|
echo   ^| 如遇 SmartScreen 拦截：点击"更多信息" -> "仍要运行"         ^|
echo   ^+-------------------------------------------------------------+
goto :eof

REM ============================================================================
REM 主流程
REM ============================================================================
:main
echo.
echo ==========================================
echo   小猪wordTTS -- Electron 混合打包 (Windows)
echo ==========================================
echo.

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

call :check_environment
if !errorlevel! neq 0 exit /b !errorlevel!

if "%MODE%"=="--python" (
    call :build_python_backend
    if !errorlevel! neq 0 exit /b !errorlevel!
) else if "%MODE%"=="--electron" (
    REM 即使只打 Electron 壳，也先重建后端，避免把旧 server_backend 装入新客户端。
    call :build_python_backend
    if !errorlevel! neq 0 exit /b !errorlevel!
    call :build_electron_app
    if !errorlevel! neq 0 exit /b !errorlevel!
) else if "%MODE%"=="all" (
    call :build_python_backend
    if !errorlevel! neq 0 exit /b !errorlevel!
    call :build_electron_app
    if !errorlevel! neq 0 exit /b !errorlevel!
) else (
    call :err "未知参数: %MODE%"
    echo 用法: build_electron_windows.bat [--python^|--electron]
    exit /b 1
)

echo.
call :log "全部完成！"
echo   安装包位置: %ELECTRON_DIR%\release\
echo.
echo   分发: 将 .exe 安装包发给用户，双击安装即可使用
echo   无需安装 Python 或 Node.js
echo.

endlocal
