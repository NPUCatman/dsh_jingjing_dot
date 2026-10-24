"""Small Windows UI adapter. Importing this module never touches Weixin."""

from collections import deque
from dataclasses import dataclass, field
import time

from .engine import Event


REPLY_PREFIX = "🐳 小鲸鱼：\n"
TEXT_CLASS = "mmui::ChatTextItemView"
PINNED_SOURCE = "8589baa049bb91d3a500602c167f07b2f8397a13"


class WeChatError(RuntimeError):
    pass


@dataclass
class ChatWindow:
    name: str
    is_group: bool
    window: object
    message_list: object
    edit: object
    seen: set = field(default_factory=set)
    order: deque = field(default_factory=deque)

    def remember(self, runtime_id):
        if runtime_id not in self.seen:
            self.seen.add(runtime_id)
            self.order.append(runtime_id)
        while len(self.order) > 8192:
            self.seen.discard(self.order.popleft())


def _controls():
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise WeChatError("缺少微信依赖，请先运行 setup.ps1。") from exc
    return Desktop(backend="uia")


def _message_list(window):
    for title in ("消息", "Messages", "訊息"):
        control = window.child_window(title=title, control_type="List")
        if control.exists(timeout=0.2):
            return control
    raise WeChatError("无法读取消息列表。请打开普通聊天页面；如果 UI 树不可见，此账号暂不适用本接入方式。")


def _id(item):
    value = item.element_info.runtime_id
    if not value:
        raise WeChatError("微信没有提供稳定的消息标识，已停止监听。")
    return tuple(value)


def probe(config):
    """Read only UI structure, never focus a window, read text, or send."""
    desktop = _controls()
    main = desktop.windows(class_name="mmui::MainWindow")
    separate = desktop.windows(class_name="mmui::ChatSingleWindow")
    if not main and not separate:
        print("未发现已登录的微信 4.x 窗口。请登录小号，并让微信窗口保持打开。")
        return False
    print("已发现微信 4.x 窗口。本检测不会发送消息，也不会输出聊天内容。")
    targets = [*config.friends, *config.groups]
    if not targets:
        print("尚未填写聊天名单。请先运行 configure，再双击名单中的会话打开独立窗口。")
        return False
    success = True
    for name in targets:
        matches = [w for w in separate if w.window_text() == name]
        if len(matches) != 1:
            print(f"[待处理] {name}：请双击此聊天，打开唯一的独立聊天窗口（名称需完全一致）。")
            success = False
            continue
        try:
            window = desktop.window(handle=matches[0].handle)
            _message_list(window)
            edit = window.child_window(auto_id="chat_input_field", control_type="Edit")
            if not edit.exists(timeout=0.2):
                raise WeChatError("无法读取输入框。")
            print(f"[通过] {name}：消息列表和输入框可访问。实际收发仍需用测试消息验证。")
        except WeChatError as exc:
            print(f"[未通过] {name}：{exc}")
            success = False
    return success


class WeChatAdapter:
    def __init__(self, config):
        self.config = config
        self.chats = []
        self.tools = None

    def attach(self):
        desktop = _controls()
        windows = desktop.windows(class_name="mmui::ChatSingleWindow")
        names = [(n, False) for n in self.config.friends] + [(n, True) for n in self.config.groups]
        if not names:
            raise WeChatError("聊天名单为空，请先运行 configure。")
        for name, is_group in names:
            matches = [w for w in windows if w.window_text() == name]
            if len(matches) != 1:
                raise WeChatError(f"请先双击“{name}”打开唯一的独立聊天窗口，再运行 probe。")
            window = desktop.window(handle=matches[0].handle)
            message_list = _message_list(window)
            edit = window.child_window(auto_id="chat_input_field", control_type="Edit")
            if not edit.exists(timeout=0.2):
                raise WeChatError(f"“{name}”的输入框不可访问，请运行 probe。")
            chat = ChatWindow(name, is_group, window, message_list, edit)
            for item in message_list.children(control_type="ListItem"):
                chat.remember(_id(item))
            self.chats.append(chat)
        # Upstream imports disable PyAutoGUI's failsafe. Restore it immediately.
        try:
            from pyweixin import Tools
            import pyautogui
            pyautogui.FAILSAFE = True
        except Exception as exc:
            raise WeChatError("无法加载固定版本的 pyweixin。请检查依赖和已登录的微信 4.x。") from exc
        self.tools = Tools
        for chat in self.chats:
            if self.tools.is_group_chat(chat.window) != chat.is_group:
                raise WeChatError(f"“{chat.name}”的群聊/私聊类型与 config.json 不一致，请调整名单。")

    @staticmethod
    def _focus(chat):
        import win32gui
        chat.window.restore()
        chat.window.set_focus()
        time.sleep(0.12)
        if win32gui.GetAncestor(win32gui.GetForegroundWindow(), 2) != chat.window.handle:
            raise WeChatError("无法确认微信聊天窗口焦点，已停止操作。")

    def poll(self):
        """Yield all new visible text bubbles. Old history is baselined at attach."""
        for chat in self.chats:
            if not chat.window.exists(timeout=0.1):
                raise WeChatError(f"“{chat.name}”窗口已关闭，请重新打开并重启机器人。")
            items = chat.message_list.children(control_type="ListItem")
            pending = [item for item in items if _id(item) not in chat.seen]
            if not pending:
                continue
            text_pending = [item for item in pending if item.class_name() == TEXT_CLASS]
            if text_pending:
                self._focus(chat)
            for item in pending:
                runtime_id = _id(item)
                chat.remember(runtime_id)
                if item.class_name() != TEXT_CLASS:
                    continue
                text = item.window_text().strip()
                if not text or text.startswith(REPLY_PREFIX.strip()):
                    continue
                # Pixel based upstream classification requires a visible bubble.
                rect, area = item.rectangle(), chat.message_list.rectangle()
                if rect.top < area.top or rect.bottom > area.bottom:
                    continue
                if self.tools.is_my_bubble(item):
                    continue
                # Ordinary UIA bubbles do not reliably expose a group sender.
                sender = "群共享会话" if chat.is_group else chat.name
                yield chat, Event(chat=chat.name, sender=sender, text=text, is_group=chat.is_group)

    def send(self, chat, reply):
        if not chat.window.exists(timeout=0.1):
            raise WeChatError(f"“{chat.name}”窗口已关闭，请重新打开并重启机器人。")
        self._focus(chat)
        if chat.window.window_text() != chat.name:
            raise WeChatError("聊天窗口名称已变化，请核对名单后重启。")
        if chat.edit.window_text():
            raise WeChatError("输入框内已有草稿，已停止发送；请自行保留草稿并清空输入框后重启。")
        # One short text message; never allow upstream conversion to a file.
        text = REPLY_PREFIX + reply[:self.config.max_output_chars]
        chat.edit.click_input()
        chat.edit.set_text(text)
        import win32gui
        if win32gui.GetAncestor(win32gui.GetForegroundWindow(), 2) != chat.window.handle:
            raise WeChatError("发送前焦点发生变化，已保留草稿并停止。")
        chat.edit.type_keys("%s", set_foreground=False)
        for _ in range(20):
            time.sleep(0.1)
            if not chat.edit.window_text():
                # Only suppress our outgoing bubble; keep simultaneous inbound messages.
                for item in chat.message_list.children(control_type="ListItem"):
                    if item.class_name() == TEXT_CLASS and item.window_text().strip() == text.strip():
                        chat.remember(_id(item))
                return
        raise WeChatError("输入框未清空，发送结果不明确。已停止，请在微信中检查；不会自动重发。")
