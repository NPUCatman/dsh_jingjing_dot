"""Conservative OCR adapter for explicitly calibrated independent chat windows.

Screenshots and recognized conversation text stay in memory. This module does
not operate a desktop on import, read credentials, or infer arbitrary targets.
The transport must guard the native identity, dimensions and foreground before
captures and input actions. An ambiguous compose operation is never retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time
import unicodedata

from .engine import Event
from .ocr_parse import SnapshotTracker, parse_messages


REPLY_PREFIX = "小鲸鱼："
MAX_COMPOSE_CHARS = 160


class OcrAdapterError(RuntimeError):
    """An operator-readable error without recognized text or native details."""


@dataclass(frozen=True)
class Calibration:
    size: tuple[int, int]
    messages: tuple[int, int, int, int]
    input: tuple[int, int, int, int]
    empty_rgb: tuple[int, int, int]


@dataclass
class OcrChat:
    name: str
    is_group: bool
    native: object
    calibration: Calibration
    tracker: SnapshotTracker = field(default_factory=lambda: SnapshotTracker(stable_frames=2))
    startup_count: int = 0


def _integers(value, length: int) -> tuple[int, ...]:
    if not isinstance(value, (tuple, list)) or len(value) != length or any(type(item) is not int for item in value):
        raise OcrAdapterError("OCR 校准数据格式不正确，请重新校准聊天窗口。")
    return tuple(value)


def _rect(value, size: tuple[int, int]) -> tuple[int, int, int, int]:
    rectangle = _integers(value, 4)
    left, top, right, bottom = rectangle
    width, height = size
    if not (4 <= left < right <= width - 4 and 4 <= top < bottom <= height - 4):
        raise OcrAdapterError("OCR 校准区域超出窗口内部，请重新校准。")
    if right - left < 24 or bottom - top < 24:
        raise OcrAdapterError("OCR 校准区域过小，请重新校准。")
    return rectangle


def _calibration(value) -> Calibration:
    if not isinstance(value, dict) or set(value) != {"size", "messages", "input", "empty_rgb"}:
        raise OcrAdapterError("OCR 校准数据字段不正确，请重新校准聊天窗口。")
    size = _integers(value["size"], 2)
    if not (160 <= size[0] <= 16384 and 240 <= size[1] <= 16384):
        raise OcrAdapterError("OCR 校准窗口尺寸不正确，请重新校准。")
    messages, composer = _rect(value["messages"], size), _rect(value["input"], size)
    # A calibration chooses the inner typing area, excluding border, toolbar,
    # send button and title. The message viewport must end above that area.
    if messages[3] + 4 > composer[1]:
        raise OcrAdapterError("OCR 消息区域与输入区域必须分开，且输入区域应位于下方。")
    rgb = _integers(value["empty_rgb"], 3)
    if min(rgb) < 240 or max(rgb) > 255 or max(rgb) - min(rgb) > 8:
        raise OcrAdapterError("OCR 目前只支持浅色空白输入区域，请重新校准。")
    return Calibration(size, messages, composer, rgb)


def validate_calibration(data) -> Calibration:
    """Validate one chat's size/messages/input/empty_rgb calibration entry."""
    return _calibration(data)


def _normalized(text: str) -> str:
    # Preserve punctuation, case, Hanzi and numbers. Dropping punctuation could
    # accidentally approve a changed decimal, negative number or command.
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()

    def ideograph(char):
        value = ord(char)
        return (0x3400 <= value <= 0x4DBF or 0x4E00 <= value <= 0x9FFF
                or 0xF900 <= value <= 0xFAFF or 0x20000 <= value <= 0x323AF)

    # OCR often inserts a space at a Chinese line wrap. ASCII spaces carry
    # meaning (paths, command arguments, identifiers) and must remain present.
    return "".join(
        char for index, char in enumerate(text)
        if char != " " or not (
            (index > 0 and ideograph(text[index - 1]))
            or (index + 1 < len(text) and ideograph(text[index + 1]))
        )
    )


def is_input_empty(image, empty_rgb: tuple[int, int, int], *, allow_caret: bool = False) -> bool:
    """Accept only the calibrated background and, optionally, a small caret.

    OCR returning no text is insufficient evidence of an empty composer. Any
    color, image, glyph-shaped run or ambiguous anti-aliased mark blocks input.
    The caret exception is available only after a verified empty composer was
    clicked, and is limited to its expected first insertion position. Pixels
    alone cannot distinguish every possible glyph from an insertion caret.
    """
    if image.mode != "RGB" or image.width < 24 or image.height < 24:
        return False
    marks = []
    pixel_values = getattr(image, "get_flattened_data", image.getdata)()
    for index, color in enumerate(pixel_values):
        if all(abs(channel - blank) <= 6 for channel, blank in zip(color, empty_rgb)):
            continue
        if not allow_caret:
            return False
        # The only exception is a neutral dark caret, never a colored marker.
        if max(color) - min(color) > 6 or any(channel >= blank - 6 for channel, blank in zip(color, empty_rgb)):
            return False
        marks.append((index % image.width, index // image.width))
        if len(marks) > 64:
            return False
    if not marks:
        return True
    xs, ys = zip(*marks)
    width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    if width > 2 or not 10 <= height <= 32 or min(xs) > 20 or min(ys) > 30:
        return False
    # Require a solid thin vertical rectangle, not separated dots or a glyph
    # with a horizontal stroke. This covers the usual 100-150% DPI cursor.
    return len(marks) == width * height


class OcrWeChatAdapter:
    def __init__(self, config, root: Path, desktop=None, backend=None):
        self.config = config
        self.root = Path(root)
        self.desktop = desktop
        self.backend = backend
        self.chats: list[OcrChat] = []
        self._attached = False
        self._uncertain_write = False

    def _call(self, operation, *args):
        try:
            return operation(*args)
        except OcrAdapterError:
            raise
        except Exception:
            raise OcrAdapterError("微信窗口状态或本地 OCR 检查失败，已停止；请检查目标窗口后重新启动。") from None

    def _guard(self, chat: OcrChat):
        current = self._call(self.desktop.inspect, chat.native)
        # Some transports only raise on failure. If metadata is returned, also
        # verify it here, independently of transport implementation details.
        current = chat.native if current is None else current
        if (current.title != chat.name or tuple(current.size) != chat.calibration.size
                or current.pid != chat.native.pid or current.handle != chat.native.handle):
            raise OcrAdapterError("目标聊天名称、进程或窗口尺寸发生变化，请重新校准并启动。")

    def _capture(self, chat: OcrChat):
        self._guard(chat)
        image = self._call(self.desktop.capture, chat.native)
        self._guard(chat)
        if image.mode != "RGB" or image.size != chat.calibration.size:
            raise OcrAdapterError("微信截图尺寸或颜色格式与校准不一致，已停止。")
        return image

    def _check_empty(self, chat: OcrChat, image, *, allow_caret: bool = False):
        if not is_input_empty(image.crop(chat.calibration.input), chat.calibration.empty_rgb, allow_caret=allow_caret):
            raise OcrAdapterError("输入框有草稿或无法确认空白，已保留原内容并停止。请先手动处理草稿。")

    def _capture_empty(self, chat: OcrChat):
        """Wait briefly for a blinking caret to disappear, using captures only.

        A pre-existing vertical glyph must not be accepted as a caret. Requiring
        an actually blank frame preserves that rule without failing whenever
        the insertion caret happens to be visible on the first capture.
        """
        for attempt in range(8):
            image = self._capture(chat)
            if is_input_empty(image.crop(chat.calibration.input), chat.calibration.empty_rgb):
                return image
            if attempt < 7:
                time.sleep(0.1)
        raise OcrAdapterError("输入框有草稿或无法确认空白，已保留原内容并停止。请先手动处理草稿。")

    def _messages(self, chat: OcrChat, image):
        viewport = image.crop(chat.calibration.messages)
        lines, bubbles = self._call(self.backend.recognize_bubbles, viewport)
        return self._call(lambda: parse_messages(lines, viewport.width, viewport.height, bubbles, min_confidence=0.8))

    def attach(self):
        self._attached = False
        self.chats = []
        names = tuple(self.config.friends) + tuple(self.config.groups)
        if not names or len(set(names)) != len(names):
            raise OcrAdapterError("请先配置唯一的好友备注或群名称。")
        path = self.root / "data" / "ocr-calibration.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise OcrAdapterError("未找到有效的 OCR 校准文件，请先校准指定聊天窗口。") from None
        if (not isinstance(data, dict) or set(data) != {"version", "chats"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["chats"], dict)):
            raise OcrAdapterError("OCR 校准文件版本或结构不正确，请重新校准。")
        calibrations = {}
        for name in names:
            if name not in data["chats"]:
                raise OcrAdapterError("部分指定聊天尚未校准，请先完成 OCR 校准。")
            calibrations[name] = _calibration(data["chats"][name])
        if self.desktop is None:
            from .ocr_desktop import WindowsDesktop
            self.desktop = WindowsDesktop()
        if self.backend is None:
            from .ocr_backend import LocalOcrBackend
            # Keep uncertain extra composer marks visible to the send gate;
            # pre-filtering them could approve only the confident substring.
            self.backend = LocalOcrBackend(min_confidence=0.0)
        bound = []
        for name in names:
            native = self._call(self.desktop.find, name)
            if native.title != name or tuple(native.size) != calibrations[name].size:
                raise OcrAdapterError("指定聊天窗口与校准不一致，请打开独立聊天窗口并重新校准。")
            chat = OcrChat(name, name in self.config.groups, native, calibrations[name])
            self._call(self.desktop.activate, native)
            # Obtain two matching captures before treating visible history as a
            # baseline. An unstable opening animation never becomes an event.
            saw_text = False
            for _ in range(6):
                image = self._capture_empty(chat)
                messages = self._messages(chat, image)
                chat.startup_count = len(messages)
                if not messages:
                    # An empty OCR result cannot distinguish blank history
                    # from a recognition failure. Never commit it as baseline.
                    chat.tracker.reset()
                    continue
                saw_text = True
                chat.tracker.update(messages)
                if chat.tracker.ready:
                    break
            if not chat.tracker.ready:
                if not saw_text:
                    raise OcrAdapterError("未识别到可建立基线的文字。请先保留一条完整文字气泡可见再启动。")
                raise OcrAdapterError("聊天画面持续变化，无法建立历史基线，请停留在聊天底部后重新启动。")
            bound.append(chat)
        self.chats = bound
        self._attached = True

    def _authorized(self, chat: OcrChat):
        if not self._attached or not any(item is chat for item in self.chats):
            raise OcrAdapterError("这个聊天窗口不在当前已连接的名单中。")
        allowed = self.config.groups if chat.is_group else self.config.friends
        if chat.name not in allowed:
            raise OcrAdapterError("这个聊天不在允许名单中。")
        if self._uncertain_write:
            raise OcrAdapterError("上次输入结果不明确，已停止且不会自动重发。请手动检查草稿后重新启动。")

    def poll(self) -> list[tuple[OcrChat, Event]]:
        if not self._attached:
            raise OcrAdapterError("请先连接已经校准的独立聊天窗口。")
        result = []
        for chat in self.chats:
            self._authorized(chat)
            self._call(self.desktop.activate, chat.native)
            image = self._capture(chat)
            for message in chat.tracker.update(self._messages(chat, image)):
                if message.text.lstrip().startswith(REPLY_PREFIX.strip()):
                    continue
                result.append((chat, Event(chat.name, "群共享会话" if chat.is_group else chat.name, message.text, chat.is_group)))
        return result

    def send(self, window: OcrChat, reply: str):
        self._authorized(window)
        if not isinstance(reply, str) or not reply.strip():
            raise OcrAdapterError("回复必须包含文字。")
        if any(unicodedata.category(char) == "Cc" and char not in "\r\n\t" for char in reply):
            raise OcrAdapterError("回复含有不支持的控制字符，已停止发送。")
        single_line = " ".join(reply.split())
        if len(single_line) > MAX_COMPOSE_CHARS:
            raise OcrAdapterError("OCR 回复过长，尚未输入。请将 max_output_chars 设为 140 或更小后重新启动。")
        intended = REPLY_PREFIX + single_line
        self._call(self.desktop.activate, window.native)
        self._capture_empty(window)
        left, top, right, bottom = window.calibration.input
        self._guard(window)
        self._call(self.desktop.click_input, window.native, (left + min(12, (right - left) // 2), top + min(12, (bottom - top) // 2)))
        self._check_empty(window, self._capture(window), allow_caret=True)
        self._guard(window)
        # Set before calling the transport: even an exception can occur after
        # some Unicode characters have already been inserted.
        self._uncertain_write = True
        self._call(self.desktop.type_text, window.native, intended)
        time.sleep(0.1)
        image = self._capture(window)
        lines = self._call(self.backend.recognize, image.crop(window.calibration.input))
        if not lines or any(not 0.8 <= line.confidence <= 1 for line in lines):
            raise OcrAdapterError("无法完整确认已输入的回复，已保留草稿且不会自动发送或重试。")
        observed = " ".join(line.text for line in sorted(lines, key=lambda line: (line.top, line.left)))
        if _normalized(observed) != _normalized(intended):
            raise OcrAdapterError("输入框识别结果与回复不一致，已保留草稿且不会自动发送或重试。")
        self._guard(window)
        self._call(self.desktop.press_enter, window.native)
        time.sleep(0.15)
        try:
            self._check_empty(window, self._capture(window), allow_caret=True)
        except OcrAdapterError:
            raise OcrAdapterError("回复已尝试提交，但输入框仍有内容或无法确认清空；可能发送快捷键不同。已停止且不会自动重发。") from None
        # This confirms only that the guarded Enter operation completed. It is
        # not a network-delivery receipt and must never cause an automatic retry.
        self._uncertain_write = False
