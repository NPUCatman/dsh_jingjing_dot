"""Regression checks for running Weixin with unavailable accessibility."""

from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jingjing.config import Config
from jingjing.diagnostics import NativeSnapshot, NativeWindow
from jingjing.wechat import WeChatAdapter, WeChatError, probe


class FakeDesktop:
    def __init__(self, enumerated=(), by_handle=None):
        self.enumerated = enumerated
        self.by_handle = by_handle or {}

    def windows(self, *, class_name):
        return [window for window in self.enumerated if window.class_name() == class_name]

    def window(self, *, handle):
        return self.by_handle[handle]


def window(class_name, name="指定好友", handle=42):
    control = SimpleNamespace(exists=lambda **kwargs: True)
    result = Mock()
    result.handle = handle
    result.class_name.return_value = class_name
    result.window_text.return_value = name
    result.child_window.return_value = control
    return result


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(friends=("指定好友",))
        self.qt_snapshot = NativeSnapshot(
            weixin_pids=(7,),
            windows=(NativeWindow(handle=42, pid=7, class_name="Qt51514QWindowIcon", visible=True, minimized=False),),
        )

    def run_probe(self, desktop, snapshot):
        output = StringIO()
        with patch("jingjing.wechat._controls", return_value=desktop), patch("jingjing.wechat.native_snapshot", return_value=snapshot), redirect_stdout(output):
            result = probe(self.config)
        return result, output.getvalue()

    def test_running_qt_window_is_not_misreported_as_missing_login(self):
        desktop = FakeDesktop(by_handle={42: window("Qt51514QWindowIcon")})
        result, output = self.run_probe(desktop, self.qt_snapshot)
        self.assertFalse(result)
        self.assertNotIn("未发现已登录", output)
        self.assertIn("UIA", output)
        self.assertIn("Qt51514QWindowIcon", output)

    def test_handle_fallback_works_without_desktop_uia_enumeration(self):
        target = window("mmui::ChatSingleWindow")
        result, output = self.run_probe(FakeDesktop(by_handle={42: target}), self.qt_snapshot)
        self.assertTrue(result)
        self.assertIn("通过", output)
        target.set_focus.assert_not_called()
        target.restore.assert_not_called()
        target.children.assert_not_called()

    def test_native_legacy_process_handle_is_never_adopted(self):
        snapshot = NativeSnapshot(wechat_pids=(7,), windows=self.qt_snapshot.windows)
        result, _ = self.run_probe(FakeDesktop(by_handle={42: window("mmui::ChatSingleWindow")}), snapshot)
        self.assertFalse(result)

    def test_accessible_main_window_requires_separate_chat(self):
        main = window("mmui::MainWindow")
        result, output = self.run_probe(FakeDesktop([main]), NativeSnapshot())
        self.assertFalse(result)
        self.assertIn("独立聊天窗口", output)
        self.assertNotIn("未发现已登录", output)
        main.window_text.assert_not_called()

    def test_login_message_requires_an_explicit_accessible_login_window(self):
        result, output = self.run_probe(FakeDesktop([window("mmui::LoginWindow")]), NativeSnapshot())
        self.assertFalse(result)
        self.assertIn("登录页面", output)

    def test_unknown_qt_window_is_never_promoted_to_chat_target(self):
        target = window("Qt51514QWindowIcon")
        result, _ = self.run_probe(FakeDesktop(by_handle={42: target}), self.qt_snapshot)
        self.assertFalse(result)
        target.child_window.assert_not_called()

    def test_attach_reports_same_unavailable_uia_diagnosis(self):
        desktop = FakeDesktop(by_handle={42: window("Qt51514QWindowIcon")})
        with patch("jingjing.wechat._controls", return_value=desktop), patch("jingjing.wechat.native_snapshot", return_value=self.qt_snapshot):
            with self.assertRaisesRegex(WeChatError, "UIA"):
                WeChatAdapter(self.config).attach()


if __name__ == "__main__":
    unittest.main()
