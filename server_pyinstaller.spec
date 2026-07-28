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
from PyInstaller.utils.hooks import collect_all

# ============================================================================
# 数据文件（资源）
# ============================================================================
datas = [
    # 核心模块
    ('word_tts_app.py', '.'),
    ('word_parser/word_parser.py', 'word_parser'),
    ('word_parser/word_parser_app.py', 'word_parser'),
    # 788 音色匹配
    ('edge_tts/voice_match_788.py', 'edge_tts'),
    ('edge_tts/voice_profiles/', 'edge_tts/voice_profiles/'),
    # 背景音乐
    ('edge_tts/bgm/', 'edge_tts/bgm/'),
]

binaries = []
hiddenimports = [
    'docx',
    'lxml.etree',
    'edge_tts',
    'pydub',
    'aiohttp',
    'voice_match_788',
    'word_parser',
    'word_tts_app',
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

# --- 其他依赖 ---
for pkg in ['markdown_it', 'mdit_py_plugins', 'safehttpx', 'ffmpy',
            'sniffio', 'idna', 'httpcore', 'click', 'typing_extensions',
            'python_multipart']:
    tmp = collect_all(pkg)
    datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]

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
        'pytest', 'playwright', 'IPython', 'notebook', 'jupyter',
        'cv2', 'sklearn', 'scipy', 'torch', 'tensorflow',
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
