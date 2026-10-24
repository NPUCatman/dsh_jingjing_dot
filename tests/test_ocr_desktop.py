"""Offline transport checks. No real window, cursor, clipboard or input access."""

import ctypes
import unittest
from unittest.mock import patch

from PIL import Image

from jingjing.ocr_desktop import (
    DesktopError, WindowInfo, WindowsDesktop, _INPUT, _KEYBDINPUT,
    _MOUSEINPUT, _unicode_inputs,
)


class FakeApi:
    def __init__(self):
        self.windows = {
            42: dict(pid=10, title="好友甲", visible=True, minimized=False, size=(600, 640), root=42),
        }
        self.processes = {10: "Weixin.exe"}
        self.unlocked = True
        self.active = 42
        self.at = (100, 200)
        self.occluded = False
        self.covered_points = set()
        self.modifiers = False
        self.cursor_position = (0, 0)
        self.cursor_writes = []
        self.sent = []
        self.activated = []
        self.captured = []
        self.on_grab = None
        self.on_cursor = None
        self.input_count = None

    def desktop_unlocked(self):
        return self.unlocked

    def enum_windows(self):
        return list(self.windows)

    def is_window(self, handle):
        return handle in self.windows

    def pid(self, handle):
        return self.windows[handle]["pid"]

    def title(self, handle):
        return self.windows[handle]["title"]

    def process_name(self, pid):
        return self.processes[pid]

    def visible(self, handle):
        return self.windows[handle]["visible"]

    def minimized(self, handle):
        return self.windows[handle]["minimized"]

    def root(self, handle):
        return self.windows.get(handle, {}).get("root", handle)

    def size(self, handle):
        return self.windows[handle]["size"]

    def origin(self, handle):
        return self.at

    def foreground(self):
        return self.active

    def window_at(self, point):
        return 99 if self.occluded or point in self.covered_points else 42

    def screen_rect(self):
        return -1920, 0, 1920, 1080

    def modifiers_down(self):
        return self.modifiers

    def set_foreground(self, handle):
        self.activated.append(handle)
        self.active = handle

    def set_cursor(self, point):
        self.cursor_writes.append(point)
        self.cursor_position = point
        if self.on_cursor:
            self.on_cursor()

    def cursor(self):
        return self.cursor_position

    def grab(self, bounds):
        self.captured.append(bounds)
        if self.on_grab:
            self.on_grab()
        return Image.new("RGBA", (600, 640))

    def send_inputs(self, inputs):
        self.sent.append(inputs)
        return len(inputs) if self.input_count is None else self.input_count


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.desktop = WindowsDesktop(_api=self.api)
        self.window = self.desktop.find("好友甲")

    def test_discovery_requires_exact_title_top_level_and_weixin_process(self):
        self.api.windows[43] = dict(self.api.windows[42], title="好友甲 ", root=43)
        self.api.windows[44] = dict(self.api.windows[42], pid=11, root=44)
        self.api.processes[11] = "Other.exe"
        self.api.windows[45] = dict(self.api.windows[42], root=42)
        self.assertEqual(WindowInfo(42, 10, "好友甲", (600, 640)), self.desktop.find("好友甲"))
        self.assertEqual([], self.api.activated)
        self.assertEqual([], self.api.sent)

    def test_discovery_rejects_duplicate_visible_titles(self):
        self.api.windows[43] = dict(self.api.windows[42], root=43)
        with self.assertRaisesRegex(DesktopError, "多个同名"):
            self.desktop.find("好友甲")

    def test_discovery_excludes_hidden_minimized_and_old_wechat(self):
        for change in ({"visible": False}, {"minimized": True}, {"pid": 11}):
            with self.subTest(change=change):
                self.api.windows[42].update(visible=True, minimized=False, pid=10)
                self.api.windows[42].update(change)
                self.api.processes[11] = "WeChat.exe"
                with self.assertRaisesRegex(DesktopError, "未找到"):
                    self.desktop.find("好友甲")

    def test_small_window_is_rejected_before_calibration(self):
        for size in ((299, 640), (600, 399)):
            self.api.windows[42]["size"] = size
            with self.assertRaisesRegex(DesktopError, "过小"):
                self.desktop.find("好友甲")

    def test_inspection_detects_reused_handle_title_resize_and_visibility(self):
        for changes in (
            {"pid": 11}, {"title": "另一会话"}, {"size": (700, 640)},
            {"visible": False}, {"minimized": True}, {"root": 43},
        ):
            with self.subTest(changes=changes):
                original = dict(self.api.windows[42])
                self.api.windows[42].update(changes)
                with self.assertRaises(DesktopError):
                    self.desktop.inspect(self.window)
                self.api.windows[42] = original
        self.api.processes[10] = "Other.exe"
        with self.assertRaises(DesktopError):
            self.desktop.inspect(self.window)

    def test_closed_window_and_blocked_desktop_prevent_all_actions(self):
        self.api.unlocked = False
        for method, args in (
            (self.desktop.find, ("好友甲",)), (self.desktop.inspect, (self.window,)),
            (self.desktop.activate, (self.window,)), (self.desktop.capture, (self.window,)),
            (self.desktop.click_input, (self.window, (20, 500))),
            (self.desktop.type_text, (self.window, "回复")),
            (self.desktop.press_enter, (self.window,)),
        ):
            with self.subTest(method=method.__name__), self.assertRaises(DesktopError):
                method(*args)
        self.assertEqual([], self.api.sent)
        self.assertEqual([], self.api.activated)
        self.assertEqual([], self.api.cursor_writes)
        self.api.unlocked = True
        del self.api.windows[42]
        with self.assertRaises(DesktopError):
            self.desktop.capture(self.window)

    def test_capture_requires_foreground_uncovered_and_fully_onscreen(self):
        self.api.active = 99
        with self.assertRaisesRegex(DesktopError, "前台焦点"):
            self.desktop.capture(self.window)
        self.api.active = 42
        self.api.occluded = True
        with self.assertRaisesRegex(DesktopError, "遮挡"):
            self.desktop.capture(self.window)
        self.api.occluded = False
        self.api.at = (1800, 200)
        with self.assertRaisesRegex(DesktopError, "完整显示"):
            self.desktop.capture(self.window)
        self.assertEqual([], self.api.captured)

    def test_capture_rechecks_identity_focus_and_position_after_grab(self):
        for change in (
            lambda: setattr(self.api, "active", 99),
            lambda: setattr(self.api, "at", (110, 200)),
            lambda: self.api.windows[42].update(title="另一会话"),
        ):
            with self.subTest(change=change):
                self.api = FakeApi()
                self.desktop = WindowsDesktop(_api=self.api)
                self.api.on_grab = change
                with self.assertRaises(DesktopError):
                    self.desktop.capture(self.window)

    def test_capture_returns_rgb_client_image_and_allows_negative_monitor_origin(self):
        self.api.at = (-900, 200)
        image = self.desktop.capture(self.window)
        self.assertEqual((600, 640), image.size)
        self.assertEqual("RGB", image.mode)
        self.assertEqual([(-900, 200, -300, 840)], self.api.captured)

    @patch("jingjing.ocr_desktop.time.sleep")
    def test_activation_is_one_attempt_and_verifies_focus(self, _sleep):
        self.api.active = 99
        self.desktop.activate(self.window)
        self.assertEqual([42], self.api.activated)
        self.api.active = 99
        with patch.object(self.api, "set_foreground", side_effect=lambda handle: None):
            with self.assertRaisesRegex(DesktopError, "前台焦点"):
                self.desktop.activate(self.window)

    def test_click_validates_client_point_and_rechecks_after_cursor_move(self):
        for point in ((-1, 500), (600, 500), (20, 640), (20.5, 500), (True, 500), "20,500"):
            with self.subTest(point=point), self.assertRaises(DesktopError):
                self.desktop.click_input(self.window, point)
        self.assertEqual([], self.api.cursor_writes)
        self.api.on_cursor = lambda: setattr(self.api, "active", 99)
        with self.assertRaises(DesktopError):
            self.desktop.click_input(self.window, (20, 500))
        self.assertEqual([], self.api.sent)

    def test_click_checks_actual_point_even_when_grid_does_not_hit_overlay(self):
        self.api.covered_points.add((120, 700))
        with self.assertRaisesRegex(DesktopError, "遮挡"):
            self.desktop.click_input(self.window, (20, 500))
        self.assertEqual([], self.api.cursor_writes)
        self.assertEqual([], self.api.sent)

    def test_click_sends_one_mouse_pair_at_client_position(self):
        self.desktop.click_input(self.window, (20, 500))
        self.assertEqual([(120, 700)], self.api.cursor_writes)
        self.assertEqual(1, len(self.api.sent))
        self.assertEqual([0, 0], [value.type for value in self.api.sent[0]])
        self.assertEqual([2, 4], [value.u.mi.dwFlags for value in self.api.sent[0]])

    def test_modifier_or_focus_changes_prevent_keyboard_and_mouse_input(self):
        self.api.modifiers = True
        for method, args in (
            (self.desktop.click_input, (self.window, (20, 500))),
            (self.desktop.type_text, (self.window, "回复")),
            (self.desktop.press_enter, (self.window,)),
        ):
            with self.subTest(method=method.__name__), self.assertRaisesRegex(DesktopError, "修饰键"):
                method(*args)
        self.assertEqual([], self.api.sent)
        self.assertEqual([], self.api.cursor_writes)
        self.api.modifiers = False
        self.api.active = 99
        with self.assertRaises(DesktopError):
            self.desktop.press_enter(self.window)

    def test_text_and_enter_are_separate_single_sendinput_calls(self):
        self.desktop.type_text(self.window, "鲸鲸 🐳")
        self.desktop.press_enter(self.window)
        self.assertEqual(2, len(self.api.sent))
        self.assertTrue(all(value.u.ki.wVk == 0 for value in self.api.sent[0]))
        self.assertEqual([13, 13], [value.u.ki.wVk for value in self.api.sent[1]])
        self.assertEqual([0, 2], [value.u.ki.dwFlags for value in self.api.sent[1]])

    def test_partial_input_is_never_retried(self):
        self.api.input_count = 1
        with self.assertRaisesRegex(DesktopError, "不会自动重试"):
            self.desktop.type_text(self.window, "测试")
        self.assertEqual(1, len(self.api.sent))

    def test_native_exception_content_is_not_exposed(self):
        with patch.object(self.api, "send_inputs", side_effect=OSError("private-chat-content")):
            with self.assertRaises(DesktopError) as caught:
                self.desktop.press_enter(self.window)
        self.assertNotIn("private-chat-content", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


class NativeInputLayoutTests(unittest.TestCase):
    def test_native_structures_use_pointer_sized_alignment(self):
        is_64_bit = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(40 if is_64_bit else 28, ctypes.sizeof(_INPUT))
        self.assertEqual(24 if is_64_bit else 16, ctypes.sizeof(_KEYBDINPUT))
        self.assertEqual(32 if is_64_bit else 24, ctypes.sizeof(_MOUSEINPUT))
        self.assertEqual(8 if is_64_bit else 4, _INPUT.u.offset)
        self.assertEqual(16 if is_64_bit else 12, _KEYBDINPUT.dwExtraInfo.offset)
        sentinel = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8 - 1)) + 123
        value = _KEYBDINPUT(dwExtraInfo=sentinel)
        self.assertEqual(sentinel, value.dwExtraInfo)

    def test_unicode_input_preserves_chinese_and_astral_surrogate_pairs(self):
        inputs = _unicode_inputs("鲸🐳")
        self.assertEqual([0x9CB8, 0x9CB8, 0xD83D, 0xD83D, 0xDC33, 0xDC33], [v.u.ki.wScan for v in inputs])
        self.assertEqual([4, 6, 4, 6, 4, 6], [v.u.ki.dwFlags for v in inputs])
        self.assertTrue(all(v.type == 1 and v.u.ki.wVk == 0 for v in inputs))

    def test_newlines_tabs_controls_and_invalid_surrogates_are_rejected(self):
        for text in ("", None, "line\nline", "line\rline", "a\tb", "\x00", "\x7f", "\x85", "a\u2028b", "a\u2029b", "\ud800"):
            with self.subTest(text=repr(text)), self.assertRaises(DesktopError):
                _unicode_inputs(text)


if __name__ == "__main__":
    unittest.main()
