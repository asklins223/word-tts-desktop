# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — 将 server.py 打包为独立可执行文件
=====================================================
用于 Electron + PyInstaller 混合打包方案。

打包后产出 server_backend/ 目录，包含：
  - server_backend (可执行文件)
  - _internal/ (Python 运行时 + 所有依赖 + 资源文件)

Electron 应用启动时 spawn 此可执行文件，无需用户安装 Python。

用法:
    pyinstaller server_pyinstaller.spec --noconfirm
"""

import sys
import os
import shutil
from PyInstaller.utils.hooks import collect_all

# ============================================================================
# 辅助函数：安全添加数据目录（目录不存在时跳过，不报错）
# ============================================================================
def safe_dir(src_dir, dest_dir):
    """如果源目录存在则添加到 datas，否则跳过。"""
    if os.path.isdir(src_dir):
        return [(src_dir, dest_dir)]
    print(f"[spec] 跳过不存在的目录: {src_dir}")
    return []

# ============================================================================
# 数据文件（资源）
# ============================================================================
datas = [
    # 核心模块
    ('word_tts_app.py', '.'),
    ('ttsmaker_client.py', '.'),
    ('word_parser/word_parser.py', 'word_parser'),
    ('word_parser/word_parser_app.py', 'word_parser'),
    # TTSMaker 模块（Playwright + Chromium 浏览器已内置打包）
    ('ttsmaker/ttsmaker.py', 'ttsmaker'),
    ('ttsmaker/__init__.py', 'ttsmaker'),
]

# 安全添加可选目录（目录可能为空或不存在）
datas += safe_dir('edge_tts/voice_profiles/', 'edge_tts/voice_profiles/')

binaries = []
hiddenimports = [
    'docx',
    'lxml.etree',
    'edge_tts',
    'pydub',
    'aiohttp',
    'word_parser',
    'word_tts_app',
    'ttsmaker_client',
    'ttsmaker.ttsmaker',
    # Playwright + TTSMaker
    'playwright',
    'playwright.sync_api',
    'playwright.async_api',
    'playwright._impl',
    'playwright._impl._driver',
    'playwright.driver',
    'greenlet',
    'pyee',
    # 验证码识别
    'ddddocr',
    'onnxruntime',
]

# ============================================================================
# 收集第三方库的所有子模块和资源
# ============================================================================
# --- 后端框架 ---
for pkg in ['fastapi', 'uvicorn', 'starlette', 'anyio', 'h11', 'pydantic',
            'pydantic_core', 'httpx', 'certifi', 'Jinja2']:
    tmp = collect_all(pkg)
    datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]

# --- TTS 核心 ---
for pkg in ['edge_tts', 'aiohttp', 'aiosignal', 'frozenlist', 'multidict',
            'yarl', 'async_timeout', 'aiofiles']:
    tmp = collect_all(pkg)
    datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]

# --- 文档解析 ---
for pkg in ['docx', 'lxml', 'PIL', 'numpy']:
    tmp = collect_all(pkg)
    datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]

# --- 音频处理 ---
for pkg in ['pydub', 'imageio_ffmpeg']:
    tmp = collect_all(pkg)
    datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]

# --- Playwright + TTSMaker（男声生成）---
# Playwright Python 模块 + driver（含 Node.js 运行时）
for pkg in ['playwright', 'greenlet', 'pyee']:
    try:
        tmp = collect_all(pkg)
        datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]
        print(f"[spec] 已收集 {pkg}")
    except Exception as e:
        print(f"[spec] 警告: 收集 {pkg} 失败: {e}")

# ddddocr（验证码识别）+ onnxruntime（ddddocr 依赖）
for pkg in ['ddddocr', 'onnxruntime']:
    try:
        tmp = collect_all(pkg)
        datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]
        print(f"[spec] 已收集 {pkg}")
    except Exception as e:
        print(f"[spec] 警告: 收集 {pkg} 失败: {e}（验证码识别将回退到 pytesseract）")

# --- 其他依赖 ---
for pkg in ['markdown_it', 'mdit_py_plugins', 'safehttpx', 'ffmpy',
            'sniffio', 'idna', 'httpcore', 'click', 'typing_extensions',
            'python_multipart']:
    tmp = collect_all(pkg)
    datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]

# ============================================================================
# Playwright Chromium 浏览器二进制 — 打包内置，开箱即用
# ============================================================================
# 查找 Playwright 安装的 Chromium 浏览器目录
_chromium_browser_added = False

# 方式 1: 通过 PLAYWRIGHT_BROWSERS_PATH 环境变量查找
_pw_browsers_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
if _pw_browsers_path and os.path.isdir(_pw_browsers_path):
    for _name in sorted(os.listdir(_pw_browsers_path), reverse=True):
        if _name.startswith('chromium-') and not _name.startswith('chromium_headless'):
            _chromium_dir = os.path.join(_pw_browsers_path, _name)
            if os.path.isdir(_chromium_dir):
                datas.append((_chromium_dir, 'playwright_browsers/' + _name))
                print(f"[spec] 已添加 Playwright Chromium: {_chromium_dir}")
                _chromium_browser_added = True
                break

# 方式 2: 默认缓存路径 ~/Library/Caches/ms-playwright/ (macOS)
if not _chromium_browser_added:
    _default_cache = os.path.join(os.path.expanduser('~'), 'Library', 'Caches', 'ms-playwright')
    if os.path.isdir(_default_cache):
        for _name in sorted(os.listdir(_default_cache), reverse=True):
            if _name.startswith('chromium-') and not _name.startswith('chromium_headless'):
                _chromium_dir = os.path.join(_default_cache, _name)
                if os.path.isdir(_chromium_dir):
                    datas.append((_chromium_dir, 'playwright_browsers/' + _name))
                    print(f"[spec] 已添加 Playwright Chromium (默认缓存): {_chromium_dir}")
                    _chromium_browser_added = True
                    break

# 方式 3: ~/.cache/ms-playwright/ (Linux)
if not _chromium_browser_added:
    _linux_cache = os.path.join(os.path.expanduser('~'), '.cache', 'ms-playwright')
    if os.path.isdir(_linux_cache):
        for _name in sorted(os.listdir(_linux_cache), reverse=True):
            if _name.startswith('chromium-') and not _name.startswith('chromium_headless'):
                _chromium_dir = os.path.join(_linux_cache, _name)
                if os.path.isdir(_chromium_dir):
                    datas.append((_chromium_dir, 'playwright_browsers/' + _name))
                    print(f"[spec] 已添加 Playwright Chromium (Linux 缓存): {_chromium_dir}")
                    _chromium_browser_added = True
                    break

# 方式 4: %USERPROFILE%\AppData\Local\ms-playwright\ (Windows)
if not _chromium_browser_added and sys.platform == 'win32':
    _windows_cache = os.environ.get('LOCALAPPDATA', os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'AppData', 'Local'))
    if _windows_cache:
        _win_playwright = os.path.join(_windows_cache, 'ms-playwright')
        if os.path.isdir(_win_playwright):
            for _name in sorted(os.listdir(_win_playwright), reverse=True):
                if _name.startswith('chromium-') and not _name.startswith('chromium_headless'):
                    _chromium_dir = os.path.join(_win_playwright, _name)
                    if os.path.isdir(_chromium_dir):
                        datas.append((_chromium_dir, 'playwright_browsers/' + _name))
                        print(f"[spec] 已添加 Playwright Chromium (Windows 缓存): {_chromium_dir}")
                        _chromium_browser_added = True
                        break

if not _chromium_browser_added:
    print("[spec] 警告: 未找到 Playwright Chromium 浏览器二进制！")
    print("[spec] 请先运行: playwright install chromium")
    print("[spec] 打包后男声 TTSMaker 将依赖系统 Chrome，无 Chromium 回退")

# ============================================================================
# FFmpeg 二进制文件 — 确保 imageio_ffmpeg 自带的 ffmpeg 被打包进去
# ============================================================================
# 方式 1: 通过 imageio_ffmpeg.get_ffmpeg_exe() 获取并显式添加
try:
    import imageio_ffmpeg as _iio_ff
    _ffmpeg_exe = _iio_ff.get_ffmpeg_exe()
    if _ffmpeg_exe and os.path.isfile(_ffmpeg_exe):
        # 将 ffmpeg 二进制文件添加到 imageio_ffmpeg/binaries/ 目录
        binaries.append((_ffmpeg_exe, 'imageio_ffmpeg/binaries'))
        print(f"[spec] 已添加 FFmpeg 二进制: {_ffmpeg_exe}")
    else:
        print("[spec] 警告: imageio_ffmpeg 未找到 ffmpeg 二进制文件")
except Exception as _e:
    print(f"[spec] 警告: 获取 imageio_ffmpeg ffmpeg 失败: {_e}")

# 方式 2: 手动搜索 imageio_ffmpeg/binaries/ 目录中的所有文件
try:
    import imageio_ffmpeg as _iio_ff2
    _binaries_dir = os.path.join(os.path.dirname(_iio_ff2.__file__), 'binaries')
    if os.path.isdir(_binaries_dir):
        for _name in os.listdir(_binaries_dir):
            _path = os.path.join(_binaries_dir, _name)
            if os.path.isfile(_path) and _name.lower().startswith('ffmpeg'):
                # 避免重复添加
                _already = any(os.path.basename(b[0]) == _name for b in binaries)
                if not _already:
                    binaries.append((_path, 'imageio_ffmpeg/binaries'))
                    print(f"[spec] 已添加 FFmpeg 二进制 (扫描): {_name}")
except Exception as _e2:
    print(f"[spec] 警告: 扫描 imageio_ffmpeg/binaries 失败: {_e2}")

# 方式 3: 系统 PATH 中的 ffmpeg 作为备用
_system_ff = shutil.which('ffmpeg')
if _system_ff:
    _already_sys = any(
        os.path.basename(b[0]) == os.path.basename(_system_ff)
        for b in binaries
    )
    if not _already_sys:
        binaries.append((_system_ff, 'imageio_ffmpeg/binaries'))
        print(f"[spec] 已添加系统 FFmpeg: {_system_ff}")

# ============================================================================
# 构建
# ============================================================================
a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 不需要旧版 UI 框架
        'gradio', 'gradio_client',
        'webview', 'bottle', 'proxy_tools',
        # 不需要的重量级库
        'numba', 'llvmlite',  # numpy 的 JIT 编译器，本应用不需要
        'mysql', 'mysql_mcp_server', 'pymysql', 'mysql.connector', 'aiomysql',
        'sqlalchemy', 'alembic', 'redis', 'celery',
        'pytest', 'IPython', 'notebook', 'jupyter',
        'cv2', 'sklearn', 'scipy', 'torch', 'tensorflow',
        # 不需要 Firefox / WebKit 浏览器（仅用 Chromium）
        'playwright.firefox', 'playwright.webkit',
        # 排除测试模块（collect_all 会拉入 numpy 的测试子包）
        'numpy.tests', 'numpy.f2py.tests', 'numpy.lib.tests',
        'numpy.ma.tests', 'numpy.linalg.tests', 'numpy.random.tests',
        'numpy.typing.tests', 'numpy.matrixlib.tests', 'numpy.fft.tests',
        'numpy.polynomial.tests', 'numpy.core.tests',
        'numpy.distutils.tests', 'numpy.compat.tests',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='server_backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='server_backend',
)
