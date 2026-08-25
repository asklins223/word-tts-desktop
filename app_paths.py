"""应用运行时路径。

源码运行和 PyInstaller 运行使用不同的资源根目录，但可写数据目录必须
由所有后端模块共享。Electron 会通过 ``WORDTTS_DATA_DIR`` 传入用户数据
目录；直接运行源码时使用项目内的 ``.runtime``，避免把生成音频、缓存和
浏览器登录状态散落在源码与只读资源旁边。
"""

from __future__ import annotations

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def resource_dir() -> str:
    """返回只读资源根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(
            getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        )
    return PROJECT_ROOT


def data_dir() -> str:
    """返回可写的应用数据目录。"""
    configured = os.environ.get("WORDTTS_DATA_DIR", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))

    # Electron 开发模式会显式传入 app.getPath('userData')；直接运行源码时
    # 使用项目内独立目录，避免污染代码目录和 resources/ 下的种子资源。
    if not getattr(sys, "frozen", False):
        return os.path.join(PROJECT_ROOT, ".runtime")

    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"),
            "Library",
            "Application Support",
            "WordTTS",
        )
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")),
            "WordTTS",
        )
    return os.path.join(os.path.expanduser("~"), ".wordtts")


def ensure_data_dir() -> str:
    """创建并返回可写的应用数据目录。"""
    path = data_dir()
    os.makedirs(path, exist_ok=True)
    return path
