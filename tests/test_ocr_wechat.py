"""Offline OCR adapter checks with pixel images and an inert desktop fake."""

from dataclasses import dataclass, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from jingjing.config import Config
from jingjing.ocr_parse import BubbleBox, OcrLine
from jingjing.ocr_wechat import (
    OcrAdapterError, OcrChat, OcrWeChatAdapter, REPLY_PREFIX,
    is_input_empty, validate_calibration,
)


SIZE = (300, 400)
CALIBRATION = {
    "size": list(SIZE), "messages": [12, 60, 288, 230],
    "input": [18, 250, 282, 360], "empty_rgb": [250, 250, 250],
}


def line(text, *, top=15, confidence=0.99):
    return OcrLine(text, ((55, top), (115, top), (115, top + 12), (55, top + 12)), confidence)


def bubble(text, *, top=10, direction="incoming"):
    left, right = (50, 125) if direction == "incoming" else (150, 240)
    return (
        OcrLine(text, ((left + 5, top + 5), (right - 5, top + 5), (right - 5, top + 17), (left + 5, top + 17)), 0.99),
        BubbleBox(left, top, right, top + 25, direction),
    )


@dataclass(frozen=True)
class Native:
    title: str
    handle: int = 41
    pid: int = 123
    size: tuple[int, int] = SIZE


class FakeDesktop:
    def __init__(self, name):
        self.window = Native(name)
        self.current = self.window
        self.focus = None
        self.draft = ""
        self.clicks = []
        self.typed = []
        self.entered = []
        self.on_click = None
        self.on_type = None
        self.fail_enter = False
        self.clear_after_enter = True
        self.captures = 0
        self.caret_box = None
        self.blink_frames = 0

    def find(self, name):
        if name != self.window.title:
            raise RuntimeError("no such window")
        return self.window

    def inspect(self, window):
        return self.current

    def activate(self, window):
        self.focus = window.handle

    def capture(self, window):
        if self.focus != window.handle:
            raise RuntimeError("foreground changed")
        self.captures += 1
        image = Image.new("RGB", self.current.size, (250, 250, 250))
        if self.draft:
            ImageDraw.Draw(image).rectangle((30, 265, 60, 280), fill=(30, 30, 30))
        if self.caret_box:
            left, top, right, bottom = self.caret_box
            ImageDraw.Draw(image).rectangle((18 + left, 250 + top, 18 + right, 250 + bottom), fill=(40, 40, 40))
        if self.blink_frames > 0:
            self.blink_frames -= 1
            ImageDraw.Draw(image).line((28, 260, 28, 285), fill=(40, 40, 40))
        return image

    def click_input(self, window, point):
        if self.focus != window.handle:
            raise RuntimeError("foreground changed")
        self.clicks.append(point)
        if self.on_click:
            self.on_click()

    def type_text(self, window, text):
        if self.focus != window.handle:
            raise RuntimeError("foreground changed")
        assert "\r" not in text and "\n" not in text
        self.draft += text
        self.typed.append(text)
        if self.on_type:
            self.on_type()

    def press_enter(self, window):
        if self.focus != window.handle:
            raise RuntimeError("foreground changed")
        self.entered.append(self.draft)
        if self.fail_enter:
            raise RuntimeError("possibly already submitted")
        if self.clear_after_enter:
            self.draft = ""


class FakeBackend:
    def __init__(self, desktop):
        self.desktop = desktop
        self.messages = [bubble("启动前的历史消息")]
        self.message_frames = None
        self.compose_result = None
        self.compose_confidence = 0.99
        self.extra_compose_line = None

    def recognize_bubbles(self, image):
        if self.message_frames:
            messages = self.message_frames.pop(0)
        else:
            messages = self.messages
        return [item[0] for item in messages], [item[1] for item in messages]

    def recognize(self, image):
        text = self.desktop.draft if self.compose_result is None else self.compose_result
        result = [line(text, confidence=self.compose_confidence)] if text else []
        if self.extra_compose_line:
            result.append(self.extra_compose_line)
        return result


class OcrWeChatTests(unittest.TestCase):
    def setUp(self):
        sleep_patch = patch("jingjing.ocr_wechat.time.sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(friends=("好友甲",))
        self.desktop = FakeDesktop("好友甲")
        self.backend = FakeBackend(self.desktop)
        self.write_calibration("好友甲")
        self.adapter = self.make_adapter()
        self.adapter.attach()
        self.chat = self.adapter.chats[0]

    def write_calibration(self, name, value=None):
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "ocr-calibration.json").write_text(
            json.dumps({"version": 1, "chats": {name: CALIBRATION if value is None else value}}), encoding="utf-8",
        )

    def make_adapter(self, config=None):
        return OcrWeChatAdapter(config or self.config, self.root, desktop=self.desktop, backend=self.backend)

    def test_attach_captures_two_stable_frames_and_suppresses_history(self):
        self.assertEqual(2, self.desktop.captures)
        self.assertEqual(1, self.chat.startup_count)
        self.assertTrue(self.chat.tracker.ready)
        self.assertEqual([], self.adapter.poll())

    def test_new_incoming_is_confirmed_twice_and_returned_only_once(self):
        self.backend.messages.extend([bubble("小号自己的消息", top=45, direction="outgoing"), bubble("新问题", top=80)])
        self.assertEqual([], self.adapter.poll())
        pairs = self.adapter.poll()
        self.assertEqual(["新问题"], [event.text for _, event in pairs])
        self.assertIs(self.chat, pairs[0][0])
        self.assertEqual("好友甲", pairs[0][1].sender)
        self.assertEqual([], self.adapter.poll())

    def test_group_uses_shared_sender_and_preserves_trigger(self):
        self.desktop = FakeDesktop("测试群")
        self.backend = FakeBackend(self.desktop)
        self.write_calibration("测试群")
        adapter = self.make_adapter(Config(groups=("测试群",)))
        adapter.attach()
        self.backend.messages.append(bubble("/鲸鲸 一个问题", top=50))
        self.assertEqual([], adapter.poll())
        event = adapter.poll()[0][1]
        self.assertEqual("群共享会话", event.sender)
        self.assertEqual("/鲸鲸 一个问题", event.text)
        self.assertTrue(event.is_group)

    def test_incoming_bot_prefix_is_suppressed(self):
        self.backend.messages.append(bubble(REPLY_PREFIX + "不形成环路", top=50))
        self.assertEqual([], self.adapter.poll())
        self.assertEqual([], self.adapter.poll())

    def test_send_rejects_unbound_window_and_changed_allowlist(self):
        foreign = OcrChat("好友甲", False, self.desktop.window, self.chat.calibration)
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(foreign, "不能发送")
        self.adapter.config = Config(friends=("另一好友",))
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(self.chat, "不能发送")
        self.assertEqual([], self.desktop.typed)

    def test_resize_title_and_process_change_stop_before_input(self):
        for changed in (replace(self.desktop.window, size=(301, 400)), replace(self.desktop.window, title="另一好友"), replace(self.desktop.window, pid=321)):
            with self.subTest(changed=changed):
                self.desktop.current = changed
                with self.assertRaises(OcrAdapterError):
                    self.adapter.send(self.chat, "不能发送")
                with self.assertRaises(OcrAdapterError):
                    self.adapter.poll()
                self.assertEqual([], self.desktop.typed)

    def test_existing_draft_blocks_without_click_or_overwrite(self):
        self.desktop.draft = "用户原有草稿"
        previous_captures = self.desktop.captures
        with self.assertRaisesRegex(OcrAdapterError, "草稿"):
            self.adapter.send(self.chat, "不要覆盖")
        self.assertEqual(8, self.desktop.captures - previous_captures)
        self.assertEqual("用户原有草稿", self.desktop.draft)
        self.assertEqual([], self.desktop.clicks)
        self.assertEqual([], self.desktop.typed)
        self.assertEqual([], self.desktop.entered)

    def test_startup_waits_readonly_for_caret_blink_then_baselines(self):
        self.desktop.blink_frames = 2
        previous_captures = self.desktop.captures
        adapter = self.make_adapter()
        adapter.attach()
        self.assertEqual(4, self.desktop.captures - previous_captures)
        self.assertEqual(1, adapter.chats[0].startup_count)
        self.assertEqual([], self.desktop.clicks)
        self.assertEqual([], self.desktop.typed)
        self.assertEqual([], self.desktop.entered)

    def test_send_waits_readonly_for_caret_blink_before_clicking(self):
        self.desktop.blink_frames = 2
        previous_captures = self.desktop.captures
        self.adapter.send(self.chat, "光标闪烁后再确认空白")
        self.assertEqual(6, self.desktop.captures - previous_captures)
        self.assertEqual([REPLY_PREFIX + "光标闪烁后再确认空白"], self.desktop.typed)
        self.assertEqual(self.desktop.typed, self.desktop.entered)

    def test_startup_with_real_draft_stops_after_eight_readonly_frames(self):
        self.desktop.draft = "真实草稿"
        previous_captures = self.desktop.captures
        adapter = self.make_adapter()
        with self.assertRaisesRegex(OcrAdapterError, "草稿"):
            adapter.attach()
        self.assertEqual(8, self.desktop.captures - previous_captures)
        self.assertEqual("真实草稿", self.desktop.draft)
        self.assertEqual([], self.desktop.clicks)
        self.assertEqual([], self.desktop.typed)
        self.assertEqual([], self.desktop.entered)

    def test_draft_created_during_focus_click_blocks_typing(self):
        self.desktop.on_click = lambda: setattr(self.desktop, "draft", "同时编辑")
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(self.chat, "不要覆盖")
        self.assertEqual("同时编辑", self.desktop.draft)
        self.assertEqual([], self.desktop.typed)

    def test_valid_compose_is_checked_then_entered_once(self):
        self.adapter.send(self.chat, "你好\n  世界")
        expected = REPLY_PREFIX + "你好 世界"
        self.assertEqual([expected], self.desktop.typed)
        self.assertEqual([expected], self.desktop.entered)
        self.assertEqual("", self.desktop.draft)

    def test_mismatch_preserves_draft_and_blocks_all_automatic_retry(self):
        self.backend.compose_result = REPLY_PREFIX + "被改动的数字 12"
        with self.assertRaisesRegex(OcrAdapterError, "不一致"):
            self.adapter.send(self.chat, "原来的数字 21")
        draft = self.desktop.draft
        with self.assertRaisesRegex(OcrAdapterError, "不会自动重发"):
            self.adapter.send(self.chat, "再尝试")
        with self.assertRaises(OcrAdapterError):
            self.adapter.poll()
        self.assertEqual([], self.desktop.entered)
        self.assertEqual(draft, self.desktop.draft)
        self.assertEqual(1, len(self.desktop.typed))

    def test_punctuation_is_preserved_in_ocr_comparison(self):
        self.backend.compose_result = REPLY_PREFIX + "数字 12"
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(self.chat, "数字 1.2")
        self.assertEqual([], self.desktop.entered)

    def test_ascii_argument_space_is_preserved_in_ocr_comparison(self):
        self.backend.compose_result = REPLY_PREFIX + "rm -rf /tmp"
        with self.assertRaisesRegex(OcrAdapterError, "不一致"):
            self.adapter.send(self.chat, "rm -rf / tmp")
        self.assertEqual([], self.desktop.entered)

    def test_chinese_wrapping_spaces_can_be_discarded(self):
        self.backend.compose_result = REPLY_PREFIX + "你 好 世界"
        self.adapter.send(self.chat, "你好世界")
        self.assertEqual([REPLY_PREFIX + "你好世界"], self.desktop.entered)

    def test_extra_low_confidence_composer_line_blocks_enter(self):
        self.backend.extra_compose_line = line("识别不清的额外标记", top=40, confidence=0.2)
        with self.assertRaisesRegex(OcrAdapterError, "无法完整确认"):
            self.adapter.send(self.chat, "正确部分不能掩盖额外标记")
        self.assertEqual([], self.desktop.entered)

    def test_preexisting_caret_shaped_mark_blocks_before_click(self):
        self.desktop.caret_box = (10, 10, 10, 35)
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(self.chat, "不能将已有竖线当成空白")
        self.assertEqual([], self.desktop.clicks)
        self.assertEqual([], self.desktop.typed)

    def test_first_position_caret_after_empty_click_is_allowed(self):
        self.desktop.on_click = lambda: setattr(self.desktop, "caret_box", (10, 10, 10, 35))
        self.adapter.send(self.chat, "首次位置的输入光标")
        self.assertEqual([REPLY_PREFIX + "首次位置的输入光标"], self.desktop.entered)

    def test_low_confidence_or_empty_compose_cannot_press_enter(self):
        for confidence, observed in ((0.79, None), (0.99, "")):
            with self.subTest(confidence=confidence, observed=observed):
                self.desktop.draft = ""
                adapter = self.make_adapter()
                adapter.attach()
                self.backend.compose_confidence = confidence
                self.backend.compose_result = observed
                with self.assertRaises(OcrAdapterError):
                    adapter.send(adapter.chats[0], "待检查内容")
                self.assertEqual([], self.desktop.entered)

    def test_lost_foreground_after_typing_preserves_unsent_draft(self):
        self.desktop.on_type = lambda: setattr(self.desktop, "focus", 999)
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(self.chat, "保留草稿")
        self.assertEqual(REPLY_PREFIX + "保留草稿", self.desktop.draft)
        self.assertEqual([], self.desktop.entered)

    def test_enter_failure_cannot_trigger_another_enter(self):
        self.desktop.fail_enter = True
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(self.chat, "结果可能已提交")
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(self.chat, "不能重试")
        self.assertEqual(1, len(self.desktop.entered))

    def test_enter_that_leaves_draft_is_uncertain_and_never_retried(self):
        self.desktop.clear_after_enter = False
        with self.assertRaisesRegex(OcrAdapterError, "不会自动重发"):
            self.adapter.send(self.chat, "保留且不要自动重试")
        self.assertEqual(REPLY_PREFIX + "保留且不要自动重试", self.desktop.draft)
        with self.assertRaises(OcrAdapterError):
            self.adapter.send(self.chat, "再尝试")
        self.assertEqual(1, len(self.desktop.entered))

    def test_long_or_control_character_reply_is_rejected_before_typing(self):
        for text in ("鲸" * 161, "带\x00控制字符", "  "):
            with self.subTest(text_length=len(text)):
                with self.assertRaises(OcrAdapterError):
                    self.adapter.send(self.chat, text)
        self.assertEqual([], self.desktop.typed)

    def test_missing_wrong_or_incomplete_calibration_cannot_attach(self):
        for payload in ({"version": True, "chats": {}}, {"version": 1, "chats": {}}, []):
            with self.subTest(payload=payload):
                (self.root / "data" / "ocr-calibration.json").write_text(json.dumps(payload), encoding="utf-8")
                adapter = self.make_adapter()
                with self.assertRaises(OcrAdapterError):
                    adapter.attach()
                self.assertEqual([], adapter.chats)

    def test_unstable_startup_is_not_baselined_and_does_not_attach(self):
        self.backend.message_frames = [[bubble(str(index))] for index in range(6)]
        adapter = self.make_adapter()
        with self.assertRaisesRegex(OcrAdapterError, "持续变化"):
            adapter.attach()
        self.assertEqual([], adapter.chats)

    def test_zero_ocr_startup_cannot_establish_empty_baseline(self):
        self.backend.messages = []
        adapter = self.make_adapter()
        with self.assertRaisesRegex(OcrAdapterError, "完整文字气泡"):
            adapter.attach()
        self.assertEqual([], adapter.chats)
        self.backend.messages = [bubble("此前未识别的历史文字")]
        with self.assertRaises(OcrAdapterError):
            adapter.poll()

    def test_ocr_recovery_during_attach_baselines_history_without_events(self):
        self.backend.message_frames = [[], [], [bubble("识别恢复后的旧消息")], [bubble("识别恢复后的旧消息")]]
        self.backend.messages = [bubble("识别恢复后的旧消息")]
        adapter = self.make_adapter()
        adapter.attach()
        self.assertEqual(1, adapter.chats[0].startup_count)
        self.assertEqual([], adapter.poll())

    def test_empty_frames_break_consecutive_startup_confirmation(self):
        self.backend.message_frames = [[bubble("旧消息")], [], [bubble("旧消息")], [], [bubble("旧消息")], []]
        adapter = self.make_adapter()
        with self.assertRaises(OcrAdapterError):
            adapter.attach()
        self.assertEqual([], adapter.chats)

    def test_duplicate_target_names_cannot_attach(self):
        with self.assertRaises(OcrAdapterError):
            self.make_adapter(Config(friends=("好友甲",), groups=("好友甲",))).attach()


class EmptyInputTests(unittest.TestCase):
    def image(self):
        return Image.new("RGB", (120, 60), (250, 250, 250))

    def test_blank_and_small_neutral_solid_caret_are_allowed(self):
        blank = self.image()
        self.assertTrue(is_input_empty(blank, (250, 250, 250)))
        ImageDraw.Draw(blank).line((10, 10, 10, 29), fill=(40, 40, 40))
        self.assertFalse(is_input_empty(blank, (250, 250, 250)))
        self.assertTrue(is_input_empty(blank, (250, 250, 250), allow_caret=True))

    def test_draft_color_broad_marks_and_separated_rows_are_blocked(self):
        for box, color in (((10, 10, 16, 19), (40, 40, 40)), ((10, 10, 10, 10), (200, 50, 40)), ((10, 10, 11, 42), (40, 40, 40))):
            with self.subTest(box=box, color=color):
                image = self.image()
                ImageDraw.Draw(image).rectangle(box, fill=color)
                self.assertFalse(is_input_empty(image, (250, 250, 250), allow_caret=True))
        image = self.image()
        image.putpixel((10, 10), (40, 40, 40))
        image.putpixel((10, 13), (40, 40, 40))
        self.assertFalse(is_input_empty(image, (250, 250, 250), allow_caret=True))

    def test_caret_exception_only_accepts_solid_first_position_line(self):
        for box in ((21, 10, 21, 35), (10, 31, 10, 56), (10, 10, 10, 17)):
            with self.subTest(box=box):
                image = self.image()
                ImageDraw.Draw(image).rectangle(box, fill=(40, 40, 40))
                self.assertFalse(is_input_empty(image, (250, 250, 250), allow_caret=True))
        image = self.image()
        draw = ImageDraw.Draw(image)
        draw.line((10, 10, 10, 29), fill=(40, 40, 40))
        draw.point((11, 29), fill=(40, 40, 40))
        self.assertFalse(is_input_empty(image, (250, 250, 250), allow_caret=True))

    def test_calibration_bounds_overlap_and_noninteger_values_are_rejected(self):
        self.assertEqual(SIZE, validate_calibration(CALIBRATION).size)
        for changes in ({"input": [18, 220, 282, 360]}, {"messages": [-1, 60, 288, 230]}, {"empty_rgb": [30, 30, 30]}, {"size": [True, 400]}, {"input": [18, 250, 282, 400]}):
            with self.subTest(changes=changes):
                with self.assertRaises(OcrAdapterError):
                    validate_calibration({**CALIBRATION, **changes})


if __name__ == "__main__":
    unittest.main()
