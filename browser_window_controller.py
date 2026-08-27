"""专用自动化浏览器的原生窗口控制。

Playwright 的 Sync API 只能在创建它的线程中操作 context/page，但操作系统窗口
隐藏/显示不依赖 Playwright 对象。因此这里保存经过进程身份绑定的 PID，只对这些
PID 的窗口发出命令，避免按标题误伤用户正在使用的 Chrome。

Windows 使用 user32 枚举 HWND；macOS 使用 System Events 的 unix id 精确定位进程。
macOS 的 AppleScript 失败会明确标记为 ``manual_required``，由上层提示辅助功能
权限，而不是把“请求已发出”伪装成隐藏成功。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Iterable, Optional


class BrowserWindowController:
    """控制一个由 Playwright 启动且已绑定进程身份的浏览器应用。"""

    WINDOWS_SHOW = 5
    WINDOWS_RESTORE = 9
    WINDOWS_MINIMIZE = 6
    WINDOWS_HIDE = 0

    def __init__(
        self,
        *,
        pid: Optional[int] = None,
        process_ids: Optional[Iterable[int]] = None,
        executable_path: str = "",
        profile_dir: str = "",
        started_at: Optional[float] = None,
    ):
        self._lock = threading.RLock()
        self.pid: Optional[int] = int(pid) if pid else None
        self.process_ids = self._normalize_pids(process_ids or ([pid] if pid else []))
        if self.pid and self.pid not in self.process_ids:
            self.process_ids.insert(0, self.pid)
        self.executable_path = str(executable_path or "")
        self.profile_dir = str(profile_dir or "")
        self.started_at = float(started_at or time.time())
        self.visibility = "visible"
        self.permission_required = False
        self.last_error = None
        self.window_handles = []
        self.last_operation_at = None

    @staticmethod
    def _normalize_pids(values):
        result = []
        for value in values:
            try:
                pid = int(value)
            except (TypeError, ValueError):
                continue
            if pid > 0 and pid not in result:
                result.append(pid)
        return result

    def attach_processes(self, pid: Optional[int], process_ids: Iterable[int]):
        with self._lock:
            self.pid = int(pid) if pid else self.pid
            self.process_ids = self._normalize_pids(process_ids)
            if self.pid and self.pid not in self.process_ids:
                self.process_ids.insert(0, self.pid)

    def _set_failure(self, error: str, *, permission=False):
        self.last_error = str(error or "窗口控制失败")[:500]
        self.permission_required = bool(permission)
        self.visibility = "manual_required" if permission else "unavailable"
        self.last_operation_at = time.time()
        return self.snapshot()

    def _set_success(self, visibility: str, handles=None):
        self.last_error = None
        self.permission_required = False
        self.visibility = visibility
        self.window_handles = [str(handle) for handle in (handles or [])]
        self.last_operation_at = time.time()
        return self.snapshot()

    def _win32_window_handles(self):
        if sys.platform != "win32":
            return []
        pids = set(self.process_ids)
        if not pids:
            return []
        user32 = ctypes.windll.user32
        handles = []
        # 不声明 Win32 原型时，ctypes 会按 c_int 传递没有显式类型的参数。
        # 64 位 Windows 的 HWND/LPARAM 是指针宽度，必须显式绑定，否则窗口
        # 句柄可能被截断，最终表现为“找到窗口但隐藏/恢复没有效果”。
        hwnd_type = ctypes.c_void_p
        lparam_type = ctypes.c_ssize_t
        bool_type = ctypes.c_int
        enum_type = ctypes.WINFUNCTYPE(bool_type, hwnd_type, lparam_type)
        get_window_thread_process_id = user32.GetWindowThreadProcessId
        get_window_thread_process_id.argtypes = [
            hwnd_type,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        get_window_thread_process_id.restype = ctypes.c_ulong
        enum_windows = user32.EnumWindows
        enum_windows.argtypes = [enum_type, lparam_type]
        enum_windows.restype = bool_type

        def callback(hwnd, _lparam):
            process_id = ctypes.c_ulong(0)
            get_window_thread_process_id(hwnd, ctypes.byref(process_id))
            if int(process_id.value) in pids:
                handles.append(int(hwnd))
            return True

        enum_windows(enum_type(callback), lparam_type(0))
        return handles

    @staticmethod
    def _macos_script(visible: bool):
        value = "true" if visible else "false"
        return f'''on run argv
    if (count of argv) is 0 then error "missing pid"
    set targetPid to (item 1 of argv) as integer
    tell application "System Events"
        set matchingProcesses to (every process whose unix id is targetPid)
        if (count of matchingProcesses) is 0 then return "not_found"
        repeat with processRef in matchingProcesses
            set visible of processRef to {value}
            if (visible of processRef) is not {value} then return "mismatch"
        end repeat
    end tell
    return "ok"
end run'''

    def _set_macos_visibility(self, visible: bool):
        if sys.platform != "darwin":
            return self._set_failure("当前平台不支持 macOS 窗口控制")
        if not self.pid:
            return self._set_failure("未找到专用 Chromium 主进程 PID")
        if not shutil.which("osascript"):
            return self._set_failure("系统缺少 osascript，无法控制 macOS 窗口")
        try:
            result = subprocess.run(
                ["osascript", "-e", self._macos_script(visible), str(self.pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as error:
            return self._set_failure(str(error))
        output = "\n".join(
            part.strip() for part in (result.stdout or "", result.stderr or "") if part
        )
        if result.returncode != 0:
            permission = any(
                marker in output.casefold()
                for marker in (
                    "assistive",
                    "accessibility",
                    "not authorized",
                    "not allowed",
                    "-1719",
                    "-1743",
                )
            )
            return self._set_failure(
                output or "System Events 无法访问专用 Chromium",
                permission=permission,
            )
        if output.strip() == "not_found":
            return self._set_failure("专用 Chromium 进程已退出或没有可控制窗口")
        if output.strip() == "mismatch":
            return self._set_failure("专用 Chromium 窗口状态未达到请求结果")
        return self._set_success("visible" if visible else "hidden")

    def set_visibility(self, visible: bool, *, minimize=False):
        """显示或隐藏专用浏览器，返回可直接序列化的状态。"""
        with self._lock:
            if sys.platform == "win32":
                handles = self._win32_window_handles()
                if not handles:
                    return self._set_failure("未找到专用 Chromium 的顶层窗口")
                command = self.WINDOWS_HIDE
                if visible:
                    command = self.WINDOWS_MINIMIZE if minimize else self.WINDOWS_RESTORE
                try:
                    user32 = ctypes.windll.user32
                    hwnd_type = ctypes.c_void_p
                    bool_type = ctypes.c_int
                    show_window = user32.ShowWindow
                    show_window.argtypes = [hwnd_type, ctypes.c_int]
                    show_window.restype = bool_type
                    is_window = user32.IsWindow
                    is_window.argtypes = [hwnd_type]
                    is_window.restype = bool_type
                    is_window_visible = user32.IsWindowVisible
                    is_window_visible.argtypes = [hwnd_type]
                    is_window_visible.restype = bool_type
                    is_iconic = user32.IsIconic
                    is_iconic.argtypes = [hwnd_type]
                    is_iconic.restype = bool_type
                    for hwnd in handles:
                        if not is_window(hwnd):
                            continue
                        show_window(hwnd, command)
                    if visible and not minimize:
                        for hwnd in handles:
                            if is_window(hwnd):
                                show_window(hwnd, self.WINDOWS_SHOW)
                except Exception as error:
                    return self._set_failure(str(error))
                refreshed_handles = self._win32_window_handles()
                if not refreshed_handles:
                    return self._set_failure("专用 Chromium 窗口在操作后已不可用")
                actual_state = [
                    bool(is_iconic(hwnd)) if minimize else bool(is_window_visible(hwnd))
                    for hwnd in refreshed_handles
                    if is_window(hwnd)
                ]
                # 两种请求的验证都只关心“窗口已进入目标状态”：显示和最小化
                # 都会让对应 Win32 查询返回真，隐藏则要求可见性为假。
                expected_state = True if visible else False
                if not actual_state or any(value != expected_state for value in actual_state):
                    return self._set_failure(
                        "专用 Chromium 窗口状态未达到请求结果，请手动检查窗口"
                    )
                return self._set_success(
                    "minimized" if visible and minimize else ("visible" if visible else "hidden"),
                    refreshed_handles,
                )
            if sys.platform == "darwin":
                return self._set_macos_visibility(visible)
            return self._set_failure("当前 Linux 桌面环境暂不提供统一窗口控制")

    def snapshot(self):
        with self._lock:
            return {
                "visibility": self.visibility,
                "platform": sys.platform,
                "permission_required": self.permission_required,
                "last_error": self.last_error,
                "pid": self.pid,
                "process_ids": list(self.process_ids),
                "executable_path": self.executable_path,
                "profile_dir": self.profile_dir,
                "started_at": self.started_at,
                "window_handles": list(self.window_handles),
                "last_operation_at": self.last_operation_at,
            }
