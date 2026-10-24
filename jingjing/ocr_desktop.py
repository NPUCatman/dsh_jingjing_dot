"""Guarded Windows transport for the local OCR adapter.

Importing this module does not inspect or operate any application. Every action
is bound to one previously identified, visible Weixin independent chat window.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import sys
import time


class DesktopError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    pid: int
    title: str
    size: tuple[int, int]


# Windows uses 32-bit LONG/DWORD even in a 64-bit process. ULONG_PTR alone is
# pointer-sized; using a DWORD there truncates SendInput's 64-bit structure.
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_LONG = ctypes.c_int32
_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", _LONG), ("dy", _LONG), ("mouseData", _DWORD),
        ("dwFlags", _DWORD), ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", _WORD), ("wScan", _WORD), ("dwFlags", _DWORD),
        ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("u", _INPUTUNION)]


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    value = _INPUT(type=1)
    value.u.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)
    return value


def _unicode_inputs(text: str) -> list[_INPUT]:
    if not isinstance(text, str) or not text:
        raise DesktopError("待输入文字为空或格式无效。")
    # Reject control and line separator characters, including tabs. The OCR
    # adapter intentionally inserts a single line; Enter is a separate action.
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in text):
        raise DesktopError("待输入文字含换行或控制字符，已停止输入。")
    try:
        encoded = text.encode("utf-16-le", errors="strict")
    except UnicodeError:
        raise DesktopError("待输入文字含无效 Unicode 字符，已停止输入。") from None
    values = []
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset:offset + 2], "little")
        values.extend((_key(scan=unit, flags=0x0004), _key(scan=unit, flags=0x0006)))
    return values


@contextmanager
def _safe_errors(message: str):
    """Never expose Win32 exceptions, which may contain private window text."""
    try:
        yield
    except DesktopError:
        raise
    except Exception:
        raise DesktopError(message) from None


class _WindowsApi:
    """Small lazy binding layer, replaceable in offline tests."""

    def __init__(self):
        if sys.platform != "win32":
            raise DesktopError("本地微信窗口接入仅支持 Windows。")
        import psutil
        import win32gui
        import win32process
        from PIL import ImageGrab

        self.psutil = psutil
        self.gui = win32gui
        self.process = win32process
        self.image_grab = ImageGrab
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._bind()
        self._set_dpi_awareness()

    def _bind(self):
        user = self.user32
        user.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
        user.SendInput.restype = ctypes.c_uint
        user.OpenInputDesktop.argtypes = [_DWORD, ctypes.c_int, _DWORD]
        user.OpenInputDesktop.restype = ctypes.c_void_p
        user.CloseDesktop.argtypes = [ctypes.c_void_p]
        user.CloseDesktop.restype = ctypes.c_int
        user.GetUserObjectInformationW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, _DWORD, ctypes.POINTER(_DWORD),
        ]
        user.GetUserObjectInformationW.restype = ctypes.c_int
        user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user.SetCursorPos.restype = ctypes.c_int
        user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user.GetAsyncKeyState.restype = ctypes.c_int16
        user.GetSystemMetrics.argtypes = [ctypes.c_int]
        user.GetSystemMetrics.restype = ctypes.c_int

    def _set_dpi_awareness(self):
        # A previously initialized GUI library may already have set process DPI.
        # The thread override keeps client points and screen captures consistent.
        process_setter = getattr(self.user32, "SetProcessDpiAwarenessContext", None)
        if process_setter:
            process_setter.argtypes = [ctypes.c_void_p]
            process_setter.restype = ctypes.c_int
            process_setter(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        thread_setter = getattr(self.user32, "SetThreadDpiAwarenessContext", None)
        if thread_setter:
            thread_setter.argtypes = [ctypes.c_void_p]
            thread_setter.restype = ctypes.c_void_p
            if not thread_setter(ctypes.c_void_p(-4)):
                raise DesktopError("无法建立一致的屏幕缩放坐标，已停止窗口接入。")
        elif not process_setter:
            legacy_setter = self.user32.SetProcessDPIAware
            legacy_setter.argtypes = []
            legacy_setter.restype = ctypes.c_int
            legacy_setter()

    def desktop_unlocked(self):
        # A locked machine or UAC secure desktop has a different input desktop,
        # or denies access. Do not switch desktops or attempt to unlock anything.
        desktop = self.user32.OpenInputDesktop(0, False, 0x0001)  # READOBJECTS
        if not desktop:
            return False
        try:
            buffer = ctypes.create_unicode_buffer(256)
            required = _DWORD()
            if not self.user32.GetUserObjectInformationW(
                desktop, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(required),
            ):
                return False
            return buffer.value.casefold() == "default"
        finally:
            self.user32.CloseDesktop(desktop)

    def enum_windows(self):
        handles = []
        def collect(handle, _):
            handles.append(handle)
            return True
        self.gui.EnumWindows(collect, None)
        return handles

    def is_window(self, handle):
        return self.gui.IsWindow(handle)

    def pid(self, handle):
        return self.process.GetWindowThreadProcessId(handle)[1]

    def title(self, handle):
        return self.gui.GetWindowText(handle)

    def process_name(self, pid):
        return self.psutil.Process(pid).name()

    def visible(self, handle):
        return self.gui.IsWindowVisible(handle)

    def minimized(self, handle):
        return self.gui.IsIconic(handle)

    def root(self, handle):
        return self.gui.GetAncestor(handle, 2)  # GA_ROOT, not GA_ROOTOWNER

    def size(self, handle):
        left, top, right, bottom = self.gui.GetClientRect(handle)
        return right - left, bottom - top

    def origin(self, handle):
        return self.gui.ClientToScreen(handle, (0, 0))

    def foreground(self):
        return self.gui.GetForegroundWindow()

    def window_at(self, point):
        return self.gui.WindowFromPoint(point)

    def screen_rect(self):
        x, y, width, height = (self.user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        return x, y, x + width, y + height

    def modifiers_down(self):
        return any(self.user32.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C))

    def set_foreground(self, handle):
        self.gui.SetForegroundWindow(handle)

    def set_cursor(self, point):
        if not self.user32.SetCursorPos(*point):
            raise DesktopError("无法定位输入位置，已停止操作。")

    def cursor(self):
        return self.gui.GetCursorPos()

    def grab(self, bounds):
        return self.image_grab.grab(bbox=bounds, all_screens=True).convert("RGB")

    def send_inputs(self, inputs):
        array = (_INPUT * len(inputs))(*inputs)
        return self.user32.SendInput(len(array), array, ctypes.sizeof(_INPUT))


class WindowsDesktop:
    def __init__(self, *, _api=None):
        with _safe_errors("无法加载 Windows 窗口依赖，请检查本地微信接入依赖。"):
            self._api = _api if _api is not None else _WindowsApi()

    def _unlocked(self):
        if not self._api.desktop_unlocked():
            raise DesktopError("屏幕已锁定或安全桌面正在显示，已停止窗口操作。")

    def find(self, name: str) -> WindowInfo:
        with _safe_errors("无法完整检查微信窗口，已停止接入。"):
            if not isinstance(name, str) or not name.strip():
                raise DesktopError("聊天名称为空，请检查配置。")
            self._unlocked()
            matches = []
            for handle in self._api.enum_windows():
                if not self._api.visible(handle) or self._api.minimized(handle):
                    continue
                if self._api.title(handle) != name or self._api.root(handle) != handle:
                    continue
                pid = self._api.pid(handle)
                if self._api.process_name(pid).casefold() != "weixin.exe":
                    continue
                matches.append(WindowInfo(int(handle), int(pid), name, tuple(self._api.size(handle))))
            if not matches:
                raise DesktopError("未找到指定的独立微信聊天窗口，请双击此会话并保持窗口打开。")
            if len(matches) != 1:
                raise DesktopError("存在多个同名微信聊天窗口，请只保留一个后重试。")
            return self.inspect(matches[0])

    def inspect(self, window: WindowInfo) -> WindowInfo:
        with _safe_errors("无法确认聊天窗口身份，已停止操作。"):
            self._unlocked()
            api = self._api
            if not api.is_window(window.handle) or api.root(window.handle) != window.handle:
                raise DesktopError("指定聊天窗口已关闭或发生变化，请重新打开并校准。")
            if api.pid(window.handle) != window.pid or api.title(window.handle) != window.title:
                raise DesktopError("聊天窗口身份发生变化，已停止操作。")
            if api.process_name(window.pid).casefold() != "weixin.exe":
                raise DesktopError("聊天窗口所属程序发生变化，已停止操作。")
            if not api.visible(window.handle) or api.minimized(window.handle):
                raise DesktopError("聊天窗口不可见或已最小化，请手动恢复后重启。")
            size = tuple(api.size(window.handle))
            if size[0] < 300 or size[1] < 400:
                raise DesktopError("独立聊天窗口过小，请放大后重新校准。")
            if size != window.size:
                raise DesktopError("聊天窗口尺寸已变化，请重新校准后启动。")
            return window

    def _guard(self, window: WindowInfo, *, input_action=False):
        self.inspect(window)
        api = self._api
        if api.foreground() != window.handle:
            raise DesktopError("聊天窗口已失去前台焦点，已停止操作。")
        x, y = api.origin(window.handle)
        width, height = window.size
        left, top, right, bottom = api.screen_rect()
        if x < left or y < top or x + width > right or y + height > bottom:
            raise DesktopError("聊天窗口未完整显示在屏幕内，已停止操作。")
        # Check corners and a 3x3 grid. Foreground alone does not rule out an
        # always-on-top overlay, tooltip or dialog covering the capture/input.
        xs = (12, width // 2, width - 13)
        ys = (12, height // 2, height - 13)
        for dx in xs:
            for dy in ys:
                hit = api.window_at((x + dx, y + dy))
                if not hit or api.root(hit) != window.handle:
                    raise DesktopError("聊天窗口被其他窗口遮挡，已停止操作。")
        if input_action and api.modifiers_down():
            raise DesktopError("检测到修饰键仍被按住，已停止输入。")
        return x, y, x + width, y + height

    def activate(self, window: WindowInfo):
        with _safe_errors("无法切换到指定聊天窗口，请手动打开后重试。"):
            self.inspect(window)
            if self._api.foreground() != window.handle:
                self._api.set_foreground(window.handle)
                time.sleep(0.12)
            self._guard(window)

    def capture(self, window: WindowInfo):
        with _safe_errors("无法读取聊天窗口画面，已停止操作。"):
            bounds = self._guard(window)
            image = self._api.grab(bounds)
            if self._guard(window) != bounds or image.size != window.size:
                raise DesktopError("截图期间窗口位置或尺寸发生变化，已停止操作。")
            return image.convert("RGB")

    def _send(self, inputs):
        if self._api.send_inputs(inputs) != len(inputs):
            raise DesktopError("输入结果不明确，已停止；不会自动重试，请在微信中检查。")

    def click_input(self, window: WindowInfo, point: tuple[int, int]):
        with _safe_errors("无法确认输入位置，已停止操作。"):
            if (
                not isinstance(point, (tuple, list)) or len(point) != 2
                or any(type(value) is not int for value in point)
                or not 0 <= point[0] < window.size[0]
                or not 0 <= point[1] < window.size[1]
            ):
                raise DesktopError("输入位置超出聊天窗口，已停止操作。")
            left, top, _, _ = self._guard(window, input_action=True)
            screen_point = left + point[0], top + point[1]
            if self._api.root(self._api.window_at(screen_point)) != window.handle:
                raise DesktopError("输入位置被遮挡，已停止操作。")
            self._api.set_cursor(screen_point)
            self._guard(window, input_action=True)
            if (
                self._api.cursor() != screen_point
                or self._api.root(self._api.window_at(screen_point)) != window.handle
            ):
                raise DesktopError("鼠标位置已变化，已停止操作。")
            down, up = _INPUT(type=0), _INPUT(type=0)
            down.u.mi.dwFlags, up.u.mi.dwFlags = 0x0002, 0x0004
            self._send([down, up])

    def type_text(self, window: WindowInfo, text: str):
        # Validate before touching the desktop. No clipboard or keyboard layout
        # conversion is involved; astral characters use UTF-16 surrogate pairs.
        inputs = _unicode_inputs(text)
        with _safe_errors("无法输入回复，已停止；请在微信中检查已保留的文字。"):
            self._guard(window, input_action=True)
            self._send(inputs)

    def press_enter(self, window: WindowInfo):
        with _safe_errors("无法确认发送结果，已停止；不会自动重发，请在微信中检查。"):
            self._guard(window, input_action=True)
            self._send([_key(vk=0x0D), _key(vk=0x0D, flags=0x0002)])
