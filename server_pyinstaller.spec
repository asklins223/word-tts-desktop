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

# GitHub 的 Windows runner 可能把 Python 控制台设为 cp1252。spec 中的中文
# 诊断不应反过来令构建失败，因此在任何输出前固定为 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='backslashreplace')

# ============================================================================
# 数据文件（资源）
# ============================================================================
datas = [
    # word_parser 没有 __init__.py；运行时会把这个目录加入 sys.path。
    # 其余本地 Python 模块均由 Analysis 作为代码模块收集，不再重复作为 data 打包。
    ('word_parser/word_parser.py', 'word_parser'),
]

binaries = []
hiddenimports = [
    # 这些导入位于容错分支内，显式列出以避免 PyInstaller 将其判为可选。
    # word_parser.py 作为 data 加载，Analysis 看不到它对 python-docx 的导入。
    'docx',
    'ttsmaker_client',
    'ttsmaker.ttsmaker',
    # 只使用同步 Playwright API。playwright 自带的官方 PyInstaller hook
    # 会收集 driver/package；下方在 Analysis 后剔除重复的 Node 可执行文件。
    'playwright.sync_api',
    # FastAPI 在注册 UploadFile 路由时动态验证这两个兼容导入路径。
    'python_multipart',
    'multipart',
    'multipart.multipart',
]

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
        # 构建工具/可选 Web 功能不属于后端运行时。
        'PIL',
        'httpx', 'httpcore', 'safehttpx',
        'jinja2', 'Jinja2', 'markdown', 'markdown_it', 'mdit_py_plugins',
        'aiofiles', 'ffmpy',
        # 本应用只使用 REST + SSE，不启用 WebSocket 或可选高性能协议栈。
        'websockets', 'wsproto', 'uvloop', 'httptools',
        # 不需要的重量级库
        'numba', 'llvmlite',  # numpy 的 JIT 编译器，本应用不需要
        'mysql', 'mysql_mcp_server', 'pymysql', 'mysql.connector', 'aiomysql',
        'sqlalchemy', 'alembic', 'redis', 'celery',
        'pytest', 'IPython', 'notebook', 'jupyter',
        'numpy', 'cv2', 'sklearn', 'scipy', 'torch', 'tensorflow',
        'torchaudio', 'torchvision', 'transformers', 'datasets', 'sympy',
        'pandas', 'pyarrow', 'matplotlib', 'openpyxl', 'h5py',
        'moviepy', 'librosa', 'soundfile', 'selenium', 'langchain',
        'boto3', 'botocore', 'google.cloud',
        # 不需要 Firefox / WebKit 浏览器（仅用 Chromium）
        'playwright.async_api', 'playwright.firefox', 'playwright.webkit',
    ],
    noarchive=False,
    optimize=0,
)


def _normalized_target(entry):
    """将 PyInstaller TOC 目标路径归一化为跨平台的 POSIX 小写路径。"""
    return str(entry[0]).replace('\\', '/').lstrip('./').casefold()


def _is_playwright_bundled_node(entry):
    target = _normalized_target(entry)
    return target in {
        'playwright/driver/node',
        'playwright/driver/node.exe',
    }


# Playwright 官方 hook 会收集完整 driver，其中自带一份约 106MB 的 Node。
# Electron 主进程通过 PLAYWRIGHT_NODEJS_PATH 指向 process.execPath，因此只剔除
# node/node.exe，保留 driver/package/cli.js 及其余协议文件。
_removed_playwright_node = [
    entry for entry in [*a.binaries, *a.datas]
    if _is_playwright_bundled_node(entry)
]
a.binaries = [entry for entry in a.binaries if not _is_playwright_bundled_node(entry)]
a.datas = [entry for entry in a.datas if not _is_playwright_bundled_node(entry)]

_analysis_entries = [*a.binaries, *a.datas]
if not any(
    _normalized_target(entry) == 'playwright/driver/package/cli.js'
    for entry in _analysis_entries
):
    raise RuntimeError('Playwright driver/package/cli.js 未被收集，无法启动同步 driver')
if not any(
    _normalized_target(entry).startswith('imageio_ffmpeg/binaries/ffmpeg')
    for entry in _analysis_entries
):
    raise RuntimeError('imageio-ffmpeg 的 FFmpeg 二进制未被收集')

print(f"[spec] 已移除 Playwright 内置 Node: {len(_removed_playwright_node)} 个文件")

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
