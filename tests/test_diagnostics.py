"""Native diagnostics tests use fakes and never inspect the real desktop."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jingjing.diagnostics import NativeSnapshot, NativeWindow, missing_window_message, native_snapshot


class NativeSnapshotTests(unittest.TestCase):
    def snapshot(self, processes=(), native_windows=(), *, process_error=None, window_error=None):
        def process_iter(**kwargs):
            self.assertEqual({"attrs": ["pid", "name"], "ad_value": None}, kwargs)
            for pid, name in processes:
                yield SimpleNamespace(info={"pid": pid, "name": name})
            if process_error:
                raise process_error

        by_handle = {window.handle: window for window in native_windows}

        def enum_windows(callback, extra):
            for handle in by_handle:
                self.assertTrue(callback(handle, extra))
            if window_error:
                raise window_error

        gui = SimpleNamespace(
            EnumWindows=enum_windows,
            GetClassName=lambda handle: by_handle[handle].class_name,
            IsWindowVisible=lambda handle: by_handle[handle].visible,
            IsIconic=lambda handle: by_handle[handle].minimized,
            # This sentinel must never be called, including on unrelated windows.
            GetWindowText=Mock(side_effect=AssertionError("must not read window titles")),
        )
        modules = {
            "psutil": SimpleNamespace(process_iter=process_iter),
            "win32gui": gui,
            "win32process": SimpleNamespace(GetWindowThreadProcessId=lambda handle: (1, by_handle[handle].pid)),
        }
        with patch.dict(sys.modules, modules):
            result = native_snapshot()
        gui.GetWindowText.assert_not_called()
        return result

    def test_filters_processes_and_windows_without_reading_titles(self):
        windows = (
            NativeWindow(11, 21, "Qt51514QWindowIcon", True, False),
            NativeWindow(12, 21, "mmui::ChatSingleWindow", True, False),
            NativeWindow(13, 22, "WeChatMainWndForPC", True, False),
            NativeWindow(14, 22, "LoginWndForPC", True, False),
            NativeWindow(15, 23, "mmui::MainWindow", True, False),
            NativeWindow(16, 21, "WeChatTrayWnd", False, False),
            NativeWindow(17, 21, "mmui::HelperWindow", False, False),
            NativeWindow(18, 21, "Qt51514QWindowToolTip", False, False),
            NativeWindow(19, 21, "IME", False, False),
            NativeWindow(20, 21, "UnrecognizedWindow", True, False),
        )
        snapshot = self.snapshot(
            ((21, "WEIXIN.EXE"), (22, "WeChat.exe"), (23, "WeixinHelper.exe")), windows,
        )
        self.assertEqual((21,), snapshot.weixin_pids)
        self.assertEqual((22,), snapshot.wechat_pids)
        self.assertEqual(windows[:4], snapshot.windows)
        self.assertEqual((), snapshot.errors)

    def test_no_processes_returns_empty_snapshot(self):
        self.assertEqual(NativeSnapshot(), self.snapshot(((99, "Other.exe"),)))

    def test_process_enumeration_error_retains_partial_result_and_hides_exception_text(self):
        snapshot = self.snapshot(((21, "Weixin.exe"),), process_error=PermissionError("secret account path"))
        self.assertEqual((21,), snapshot.weixin_pids)
        self.assertEqual(("进程枚举未完整完成（权限不足）",), snapshot.errors)
        self.assertNotIn("secret", repr(snapshot))

    def test_inaccessible_process_name_makes_absence_inconclusive(self):
        snapshot = self.snapshot(((21, None),))
        self.assertEqual(("部分进程名称不可读取",), snapshot.errors)
        self.assertIn("无法判断", missing_window_message(snapshot))

    def test_window_enumeration_error_retains_partial_result(self):
        window = NativeWindow(11, 21, "Qt51514QWindowIcon", True, False)
        snapshot = self.snapshot(((21, "Weixin.exe"),), (window,), window_error=OSError("private text"))
        self.assertEqual((window,), snapshot.windows)
        self.assertEqual(("窗口枚举未完整完成（系统接口错误）",), snapshot.errors)
        self.assertNotIn("private", repr(snapshot))

    def test_missing_dependency_is_a_safe_inconclusive_error(self):
        with patch.dict(sys.modules, {"psutil": None}):
            snapshot = native_snapshot()
        self.assertEqual(("无法加载 psutil（依赖缺失）",), snapshot.errors)
        self.assertIn("无法判断", missing_window_message(snapshot))

    def test_missing_win32_dependency_preserves_known_process(self):
        process_iter = Mock(return_value=[SimpleNamespace(info={"pid": 21, "name": "Weixin.exe"})])
        with patch.dict(sys.modules, {"psutil": SimpleNamespace(process_iter=process_iter), "win32gui": None}):
            snapshot = native_snapshot()
        self.assertEqual((21,), snapshot.weixin_pids)
        self.assertEqual(("无法加载 Win32 窗口接口（依赖缺失）",), snapshot.errors)


class MissingWindowMessageTests(unittest.TestCase):
    def test_no_process_message(self):
        self.assertIn("未发现 Weixin.exe 或 WeChat.exe", missing_window_message(NativeSnapshot()))

    def test_old_client_process_has_distinct_guidance(self):
        message = missing_window_message(NativeSnapshot(wechat_pids=(22,)))
        self.assertIn("旧版微信 3.x", message)
        self.assertIn("微信 4.x", message)

    def test_running_but_hidden_or_without_native_windows(self):
        for windows in ((), (NativeWindow(11, 21, "mmui::MainWindow", False, False),)):
            with self.subTest(windows=windows):
                message = missing_window_message(NativeSnapshot(weixin_pids=(21,), windows=windows))
                self.assertIn("没有找到可见", message)
                self.assertIn("托盘", message)

    def test_minimized_windows_are_not_reported_as_no_process_or_no_login(self):
        window = NativeWindow(11, 21, "mmui::MainWindow", True, True)
        message = missing_window_message(NativeSnapshot(weixin_pids=(21,), windows=(window,)))
        self.assertIn("均已最小化", message)
        self.assertNotIn("未登录", message)

    def test_native_qt_window_is_evidence_of_uia_problem_not_login_state(self):
        window = NativeWindow(11, 21, "Qt51514QWindowIcon", True, False)
        message = missing_window_message(NativeSnapshot(weixin_pids=(21,), windows=(window,)))
        self.assertIn("Qt51514QWindowIcon", message)
        self.assertIn("UIA", message)
        self.assertIn("已进入聊天主界面", message)
        self.assertNotIn("未登录", message)
        self.assertNotIn("永久", message)

    def test_old_client_window_cannot_supply_four_x_visibility(self):
        window = NativeWindow(11, 22, "WeChatMainWndForPC", True, False)
        message = missing_window_message(NativeSnapshot(weixin_pids=(21,), wechat_pids=(22,), windows=(window,)))
        self.assertIn("没有找到可见", message)
        self.assertNotIn("类名", message)

    def test_diagnostic_error_is_shown_without_claiming_process_absence(self):
        message = missing_window_message(NativeSnapshot(errors=("进程枚举未完整完成（权限不足）",)))
        self.assertIn("无法判断", message)
        self.assertIn("诊断受限", message)
        self.assertNotIn("未发现 Weixin.exe 或 WeChat.exe", message)


if __name__ == "__main__":
    unittest.main()
