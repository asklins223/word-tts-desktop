from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import browser_window_controller as controller_module
from browser_window_controller import BrowserWindowController


class BrowserWindowControllerTests(unittest.TestCase):
    def test_windows_controls_only_bound_handles_and_verifies_state(self):
        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.callback(*args)

        class FakeUser32:
            def __init__(self):
                self.windows = {1001: {"pid": 1234, "visible": True, "iconic": False}}
                self.GetWindowThreadProcessId = FakeFunction(self.get_window_pid)
                self.EnumWindows = FakeFunction(self.enum_windows)
                self.ShowWindow = FakeFunction(self.show_window)
                self.IsWindow = FakeFunction(lambda hwnd: int(hwnd in self.windows))
                self.IsWindowVisible = FakeFunction(
                    lambda hwnd: int(self.windows.get(int(hwnd), {}).get("visible", False))
                )
                self.IsIconic = FakeFunction(
                    lambda hwnd: int(self.windows.get(int(hwnd), {}).get("iconic", False))
                )

            def get_window_pid(self, hwnd, pid_pointer):
                pid_pointer._obj.value = self.windows[int(hwnd)]["pid"]
                return 1

            def enum_windows(self, callback, _lparam):
                for hwnd in list(self.windows):
                    callback(hwnd, 0)
                return 1

            def show_window(self, hwnd, command):
                state = self.windows[int(hwnd)]
                if command == 0:
                    state.update(visible=False, iconic=False)
                elif command == 6:
                    state.update(visible=True, iconic=True)
                elif command in {5, 9}:
                    state.update(visible=True, iconic=False)
                return 1

        fake_user32 = FakeUser32()
        controller = BrowserWindowController(pid=1234)

        with mock.patch.object(controller_module.sys, "platform", "win32"), \
                mock.patch.object(controller_module.ctypes, "windll", SimpleNamespace(user32=fake_user32), create=True), \
                mock.patch.object(controller_module.ctypes, "WINFUNCTYPE", lambda *_args: (lambda callback: callback), create=True):
            hidden = controller.set_visibility(False)
            shown = controller.set_visibility(True)
            minimized = controller.set_visibility(True, minimize=True)

        self.assertEqual(hidden["visibility"], "hidden")
        self.assertEqual(shown["visibility"], "visible")
        self.assertEqual(minimized["visibility"], "minimized")
        self.assertEqual(minimized["window_handles"], ["1001"])
        self.assertEqual(fake_user32.GetWindowThreadProcessId.argtypes[0], controller_module.ctypes.c_void_p)
        self.assertEqual(fake_user32.EnumWindows.argtypes[1], controller_module.ctypes.c_ssize_t)
        self.assertEqual(fake_user32.ShowWindow.argtypes, [controller_module.ctypes.c_void_p, controller_module.ctypes.c_int])

    def test_snapshot_preserves_process_identity(self):
        controller = BrowserWindowController(
            pid=1234,
            process_ids=[1234, 1235, "1235", "invalid"],
            executable_path="/opt/wordtts/chrome",
            profile_dir="/tmp/wordtts-profile",
            started_at=12.5,
        )

        snapshot = controller.snapshot()

        self.assertEqual(snapshot["pid"], 1234)
        self.assertEqual(snapshot["process_ids"], [1234, 1235])
        self.assertEqual(snapshot["executable_path"], "/opt/wordtts/chrome")
        self.assertEqual(snapshot["profile_dir"], "/tmp/wordtts-profile")
        self.assertEqual(snapshot["started_at"], 12.5)
        self.assertEqual(snapshot["visibility"], "visible")

    def test_unsupported_desktop_returns_explicit_unavailable_state(self):
        controller = BrowserWindowController(pid=1234)

        with mock.patch.object(controller_module.sys, "platform", "linux"):
            result = controller.set_visibility(False)

        self.assertEqual(result["visibility"], "unavailable")
        self.assertFalse(result["permission_required"])
        self.assertIn("Linux", result["last_error"])

    def test_macos_accessibility_failure_requires_manual_permission(self):
        controller = BrowserWindowController(pid=1234)
        process_result = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Not authorized to send Apple events to System Events.",
        )

        with mock.patch.object(controller_module.sys, "platform", "darwin"), \
                mock.patch.object(controller_module.shutil, "which", return_value="/usr/bin/osascript"), \
                mock.patch.object(controller_module.subprocess, "run", return_value=process_result):
            result = controller.set_visibility(False)

        self.assertEqual(result["visibility"], "manual_required")
        self.assertTrue(result["permission_required"])
        self.assertIn("authorized", result["last_error"])

    def test_macos_success_updates_hidden_state(self):
        controller = BrowserWindowController(pid=1234)
        process_result = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with mock.patch.object(controller_module.sys, "platform", "darwin"), \
                mock.patch.object(controller_module.shutil, "which", return_value="/usr/bin/osascript"), \
                mock.patch.object(controller_module.subprocess, "run", return_value=process_result) as run:
            result = controller.set_visibility(False)

        self.assertEqual(result["visibility"], "hidden")
        self.assertFalse(result["permission_required"])
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertEqual(run.call_args.args[0][-1], "1234")

    def test_windows_without_bound_window_fails_closed(self):
        controller = BrowserWindowController(pid=1234)

        with mock.patch.object(controller_module.sys, "platform", "win32"), \
                mock.patch.object(controller, "_win32_window_handles", return_value=[]):
            result = controller.set_visibility(False)

        self.assertEqual(result["visibility"], "unavailable")
        self.assertFalse(result["permission_required"])
        self.assertIn("顶层窗口", result["last_error"])


if __name__ == "__main__":
    unittest.main()
