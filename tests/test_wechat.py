"""Offline UI adapter checks; never import or operate the real Weixin client."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jingjing.config import Config
from jingjing.wechat import REPLY_PREFIX, TEXT_CLASS, WeChatAdapter, WeChatError, _id


class FakeItem:
    def __init__(self, number, text, *, class_name=TEXT_CLASS, mine=False, top=10, bottom=30):
        self.element_info = SimpleNamespace(runtime_id=(42, number))
        self.text = text
        self._class_name = class_name
        self.mine = mine
        self.top = top
        self.bottom = bottom

    def class_name(self):
        return self._class_name

    def window_text(self):
        return self.text

    def rectangle(self):
        return SimpleNamespace(top=self.top, bottom=self.bottom)


class FakeList:
    def __init__(self, items=None):
        self.items = list(items or [])

    def children(self, **kwargs):
        return list(self.items)

    def rectangle(self):
        return SimpleNamespace(top=0, bottom=100)

    def exists(self, **kwargs):
        return True


class FakeEdit:
    def __init__(self):
        self.text = ""
        self.sent = []
        self.on_send = None
        self.clear_after_send = True
        self.clicked = False

    def exists(self, **kwargs):
        return True

    def window_text(self):
        return self.text

    def click_input(self):
        self.clicked = True

    def set_text(self, text):
        self.text = text

    def type_keys(self, keys, **kwargs):
        self.sent.append(self.text)
        if self.on_send:
            self.on_send(self.text)
        if self.clear_after_send:
            self.text = ""


class FakeWindow:
    def __init__(self, name="好友甲", *, group=False, items=None):
        self.name = name
        self.group = group
        self.handle = 42
        self.open = True
        self.messages = FakeList(items)
        self.edit = FakeEdit()

    def window_text(self):
        return self.name

    def exists(self, **kwargs):
        return self.open

    def child_window(self, **kwargs):
        return self.edit if kwargs.get("auto_id") == "chat_input_field" else self.messages


class FakeDesktop:
    def __init__(self, windows):
        self._windows = windows

    def windows(self, **kwargs):
        return self._windows

    def window(self, **kwargs):
        return next(window for window in self._windows if window.handle == kwargs["handle"])


class WeChatTests(unittest.TestCase):
    def setUp(self):
        self.old = FakeItem(1, "启动前的历史消息")
        self.window = FakeWindow(items=[self.old])
        self.config = Config(friends=("好友甲",))
        self.tools = SimpleNamespace(is_group_chat=lambda window: window.group, is_my_bubble=lambda item: item.mine)
        self.auto = SimpleNamespace(FAILSAFE=False)
        self.native = SimpleNamespace(GetForegroundWindow=lambda: 42, GetAncestor=lambda handle, flags: 42)
        self.patches = [
            patch("jingjing.wechat._controls", return_value=FakeDesktop([self.window])),
            patch.object(WeChatAdapter, "_focus"),
            patch("jingjing.wechat.time.sleep"),
            patch.dict(sys.modules, {"pyweixin": SimpleNamespace(Tools=self.tools), "pyautogui": self.auto, "win32gui": self.native}),
        ]
        for mock_patch in self.patches:
            mock_patch.start()
        self.adapter = WeChatAdapter(self.config)
        self.adapter.attach()
        self.chat = self.adapter.chats[0]

    def tearDown(self):
        for mock_patch in reversed(self.patches):
            mock_patch.stop()

    def test_attach_baselines_old_messages_and_restores_failsafe(self):
        self.assertTrue(self.auto.FAILSAFE)
        self.assertEqual([], list(self.adapter.poll()))
        self.assertIn(_id(self.old), self.chat.seen)

    def test_poll_yields_every_new_text_bubble_in_order_once(self):
        self.window.messages.items.extend([FakeItem(2, "新消息一"), FakeItem(3, "新消息二")])
        events = list(self.adapter.poll())
        self.assertEqual(["新消息一", "新消息二"], [event.text for _, event in events])
        self.assertEqual([], list(self.adapter.poll()))

    def test_poll_skips_own_prefix_own_bubbles_nontext_and_empty(self):
        self.window.messages.items.extend([
            FakeItem(2, REPLY_PREFIX + "机器人的回复"),
            FakeItem(3, "小号自己发的文字", mine=True),
            FakeItem(4, "[图片]", class_name="mmui::ChatImageItemView"),
            FakeItem(5, "  "),
            FakeItem(6, "好友的新问题"),
        ])
        events = list(self.adapter.poll())
        self.assertEqual(["好友的新问题"], [event.text for _, event in events])
        self.assertEqual([], list(self.adapter.poll()))

    def test_offscreen_bubbles_are_not_used_for_pixel_classification(self):
        self.window.messages.items.append(FakeItem(2, "不完整显示的消息", top=-10))
        self.assertEqual([], list(self.adapter.poll()))

    def test_group_adapter_uses_explicit_shared_sender(self):
        self.window.group = True
        self.window.name = "指定测试群"
        adapter = WeChatAdapter(Config(groups=("指定测试群",)))
        adapter.attach()
        self.window.messages.items.extend([FakeItem(2, "/鲸鲸 甲的问题"), FakeItem(3, "/鲸鲸 乙的问题")])
        events = list(adapter.poll())
        self.assertTrue(all(event.is_group for _, event in events))
        self.assertEqual(["群共享会话", "群共享会话"], [event.sender for _, event in events])

    def test_closed_window_stops_poll_and_send(self):
        self.window.open = False
        with self.assertRaises(WeChatError):
            list(self.adapter.poll())
        with self.assertRaises(WeChatError):
            self.adapter.send(self.chat, "不能发送")
        self.assertEqual([], self.window.edit.sent)

    def test_changed_title_stops_send(self):
        self.window.name = "另一个会话"
        with self.assertRaises(WeChatError):
            self.adapter.send(self.chat, "不能发送")
        self.assertEqual([], self.window.edit.sent)

    def test_existing_draft_even_whitespace_is_preserved(self):
        for draft in ("用户正在编辑的草稿", "   "):
            with self.subTest(draft=draft):
                self.window.edit.text = draft
                with self.assertRaises(WeChatError):
                    self.adapter.send(self.chat, "不能覆盖")
                self.assertEqual(draft, self.window.edit.text)
                self.assertFalse(self.window.edit.clicked)
                self.assertEqual([], self.window.edit.sent)

    def test_send_prefixes_and_limits_reply_then_marks_outgoing_only(self):
        def outgoing(text):
            self.window.messages.items.append(FakeItem(2, text, mine=True))

        self.window.edit.on_send = outgoing
        self.adapter.send(self.chat, "鲸" * 2000)
        self.assertEqual([REPLY_PREFIX + "鲸" * 1000], self.window.edit.sent)
        self.assertIn((42, 2), self.chat.seen)
        self.assertEqual("", self.window.edit.text)

    def test_message_arriving_during_send_is_not_swallowed(self):
        def simultaneous_messages(text):
            self.window.messages.items.extend([FakeItem(2, text, mine=True), FakeItem(3, "恰好到达的新问题")])

        self.window.edit.on_send = simultaneous_messages
        self.adapter.send(self.chat, "回复")
        self.assertNotIn((42, 3), self.chat.seen)
        events = list(self.adapter.poll())
        self.assertEqual(["恰好到达的新问题"], [event.text for _, event in events])

    def test_message_arriving_while_poll_generator_is_paused_is_seen_next_poll(self):
        self.window.messages.items.append(FakeItem(2, "本轮的问题"))
        iterator = self.adapter.poll()
        _, event = next(iterator)
        self.assertEqual("本轮的问题", event.text)
        self.window.messages.items.append(FakeItem(3, "生成回复期间到达"))
        self.assertEqual([], list(iterator))
        self.assertEqual(["生成回复期间到达"], [event.text for _, event in self.adapter.poll()])

    def test_send_does_not_retry_when_result_is_unclear(self):
        self.window.edit.clear_after_send = False
        with self.assertRaisesRegex(WeChatError, "不会自动重发"):
            self.adapter.send(self.chat, "结果不明时保留")
        self.assertEqual([REPLY_PREFIX + "结果不明时保留"], self.window.edit.sent)
        self.assertEqual(REPLY_PREFIX + "结果不明时保留", self.window.edit.text)

    def test_whitespace_remaining_after_send_is_not_treated_as_confirmed(self):
        self.window.edit.clear_after_send = False
        self.window.edit.on_send = lambda text: setattr(self.window.edit, "text", " ")
        with self.assertRaisesRegex(WeChatError, "不会自动重发"):
            self.adapter.send(self.chat, "检查空白残留")
        self.assertEqual(" ", self.window.edit.text)
        self.assertEqual(1, len(self.window.edit.sent))

    def test_lost_focus_preserves_prepared_text_without_pressing_send(self):
        self.native.GetAncestor = lambda handle, flags: 99
        with self.assertRaisesRegex(WeChatError, "焦点发生变化"):
            self.adapter.send(self.chat, "保留草稿")
        self.assertEqual(REPLY_PREFIX + "保留草稿", self.window.edit.text)
        self.assertEqual([], self.window.edit.sent)

    def test_attach_rejects_type_mismatch_and_ambiguous_titles(self):
        self.window.group = True
        with self.assertRaises(WeChatError):
            WeChatAdapter(self.config).attach()
        self.window.group = False
        with patch("jingjing.wechat._controls", return_value=FakeDesktop([self.window, self.window])):
            with self.assertRaises(WeChatError):
                WeChatAdapter(self.config).attach()


if __name__ == "__main__":
    unittest.main()
