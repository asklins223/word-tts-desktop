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

# PyInstaller executes the spec as a namespace rather than importing it as a
# normal Python module. Recent versions do not guarantee ``__file__`` exists,
# so use the directory supplied by PyInstaller and keep the normal-module path
# for local tooling that does provide it.
_SPEC_FILE = globals().get('__file__')
SPEC_DIR = (
    os.path.dirname(os.path.abspath(_SPEC_FILE))
    if _SPEC_FILE
    else os.path.abspath(globals().get('SPECPATH') or os.getcwd())
)

# GitHub 的 Windows runner 可能把 Python 控制台设为 cp1252。spec 中的中文
# 诊断不应反过来令构建失败，因此在任何输出前固定为 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='backslashreplace')

from PyInstaller.utils.hooks import collect_data_files

# ============================================================================
# 数据文件（资源）
# ============================================================================
datas = [
    # The desktop release version is shared by the Electron shell and the
    # backend. Keep it beside the frozen backend so direct backend launches
    # report the same version as the packaged application.
    (os.path.join(SPEC_DIR, 'version.json'), '.'),
    # 迁移运行器通过 ``Path(__file__).parent / 'migrations'`` 在运行时
    # 读取 SQL。db 本身不是一个带 package data 的第三方包，因此必须显式
    # 将迁移目录放进 frozen backend；否则首个 /api/v1/workflows 请求会在
    # 初始化数据库时抛出 ``MigrationError: no migration files found``，
    # Electron 端只能看到没有诊断信息的 HTTP 500。
    (os.path.join(SPEC_DIR, 'db', 'migrations'), 'db/migrations'),
    # 首次启动在线刷新失败时使用的音色目录种子缓存。
    (os.path.join(SPEC_DIR, 'resources', 'voices.json'), 'resources'),
]

# Playwright 官方 hook（playwright/_impl/__pyinstaller/）在某些 PyInstaller
# 版本下可能不被自动发现。显式收集 playwright 数据文件（含 driver/package/cli.js），
# 确保打包后同步 driver 可用。
datas += collect_data_files('playwright', include_py_files=False)

binaries = []
hiddenimports = [
    # 这些导入位于容错分支内，显式列出以避免 PyInstaller 将其判为可选。
    # openpyxl 在 question_types.vocabulary 中是 try/except 导入。
    'docx',
    'openpyxl',
    # 题型切片包：wordtts.config 静态导入 question_types，正常会被 Analysis
    # 跟随；显式列出以防切片被误判为可选依赖。
    'question_types',
    'question_types.base',
    'question_types.text_utils',
    'question_types.info_acquisition',
    'question_types.listening_selection',
    'question_types.listening_response',
    'question_types.text_reading',
    'question_types.info_retelling',
    'question_types.listening_record_retelling',
    'question_types.imitation_reading',
    'question_types.vocabulary',
    'xunfei',
    'xunfei.config',
    'xunfei.errors',
    'xunfei.signing',
    'xunfei.voice_catalog',
    'xunfei.page_scripts',
    'xunfei.page_actions',
    'xunfei.downloads',
    'xunfei.composite_actions',
    'xunfei.generation',
    'xunfei.submission_tracker',
    'xunfei.helpers',
    'xunfei.session',
    'xunfei.runtime',
    'xunfei_voice_catalog',
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
    # The project package is also named ``workflow``.  pyinstaller-hooks-contrib
    # ships a hook for an unrelated PyPI distribution with the same name and
    # otherwise attempts to copy metadata that is not installed in the build
    # environment.  The project-local hook keeps our package importable while
    # shadowing that unrelated metadata hook.
    hookspath=[os.path.join(globals().get('SPECPATH', os.getcwd()), 'pyinstaller_hooks')],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 不需要旧版桌面/UI 框架
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
        'pandas', 'pyarrow', 'matplotlib', 'h5py',
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
if not any(
    _normalized_target(entry) == 'db/migrations/0001_foundation.sql'
    for entry in _analysis_entries
):
    raise RuntimeError('数据库迁移 SQL 未被收集，无法启动工作流 API')
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
