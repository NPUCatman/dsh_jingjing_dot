"""Synthetic OCR geometry and snapshot sequences; no real chat contents."""

import unittest

from jingjing.ocr_parse import BubbleBox, OcrLine, ParsedMessage, SnapshotTracker, parse_messages


def line(text, left=60, top=110, right=200, bottom=130, confidence=0.99):
    return OcrLine(text, ((left, top), (right, top), (right, bottom), (left, bottom)), confidence)


def message(text, top=100, direction="incoming", left=None):
    left = left if left is not None else (50 if direction == "incoming" else 400)
    return ParsedMessage(text, direction, BubbleBox(left, top, left + 200, top + 40, direction))


class OcrParsingTests(unittest.TestCase):
    def test_no_explicit_bubbles_means_no_messages(self):
        self.assertEqual([], parse_messages([line("昵称或正文无法仅凭位置区分")], 800, 600))

    def test_nickname_timestamp_and_system_notice_outside_bubbles_are_excluded(self):
        incoming = BubbleBox(50, 100, 250, 150, "incoming")
        outgoing = BubbleBox(450, 200, 750, 250, "outgoing")
        lines = [line("群成员昵称", top=70, bottom=85), line("12:30", left=370, right=430, top=20, bottom=35),
                 line("入站消息"), line("出站消息", 470, 210, 600, 230),
                 line("某人加入群聊", left=300, top=170, right=500, bottom=185)]
        parsed = parse_messages(lines, 800, 600, [outgoing, incoming])
        self.assertEqual([("入站消息", "incoming"), ("出站消息", "outgoing")], [(item.text, item.direction) for item in parsed])

    def test_multiline_and_multiple_runs_join_within_each_bubble(self):
        bubble = BubbleBox(50, 100, 350, 190, "incoming")
        lines = [line("world", 130, 110, 190, 130), line("第二行", 60, 150, 150, 170), line("Hello", 60, 110, 120, 130)]
        self.assertEqual("Hello world\n第二行", parse_messages(lines, 800, 600, [bubble])[0].text)

    def test_adjacent_chinese_runs_do_not_gain_spaces(self):
        bubble = BubbleBox(50, 100, 350, 150, "incoming")
        lines = [line("第一", 60, 110, 100, 130), line("句话", 105, 110, 150, 130)]
        self.assertEqual("第一句话", parse_messages(lines, 800, 600, [bubble])[0].text)

    def test_low_confidence_line_invalidates_whole_bubble(self):
        bubble = BubbleBox(50, 100, 350, 190, "incoming")
        lines = [line("可靠部分"), line("不可靠部分", top=150, bottom=170, confidence=0.6)]
        self.assertEqual([], parse_messages(lines, 800, 600, [bubble]))

    def test_edge_crossing_line_does_not_silently_shorten_question(self):
        bubble = BubbleBox(50, 100, 250, 190, "incoming")
        lines = [line("完整的第一行"), line("边缘第二行", 60, 150, 260, 170)]
        self.assertEqual([], parse_messages(lines, 800, 600, [bubble]))

    def test_clipped_or_wrong_side_bubbles_are_not_incoming(self):
        for bubble in [BubbleBox(50, -5, 250, 150, "incoming"), BubbleBox(50, 100, 250, 610, "incoming"),
                       BubbleBox(450, 100, 750, 150, "incoming"), BubbleBox(50, 100, 250, 150, "unknown")]:
            with self.subTest(bubble=bubble):
                self.assertEqual([], parse_messages([line("不可入站", 470, 110, 600, 130)], 800, 600, [bubble]))

    def test_ambiguous_overlapping_bubbles_are_discarded(self):
        boxes = [BubbleBox(50, 100, 250, 150, "incoming"), BubbleBox(45, 95, 255, 155, "incoming")]
        self.assertEqual([], parse_messages([line("边界有歧义")], 800, 600, boxes))

    def test_invalid_ocr_geometry_is_ignored(self):
        bubble = BubbleBox(50, 100, 250, 150, "incoming")
        bad = [OcrLine("坏数据", (), 0.99), line("坏数据", confidence=float("nan")), line("坏数据", right=float("nan"))]
        self.assertEqual([], parse_messages(bad, 800, 600, [bubble]))

    def test_invalid_region_raises(self):
        for width, height in [(0, 600), (800, -1), (float("nan"), 600)]:
            with self.assertRaises(ValueError):
                parse_messages([], width, height)


class SnapshotTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = SnapshotTracker()
        self.old = [message("历史甲", 100), message("历史乙", 160)]

    def stable(self, messages):
        first = self.tracker.update(messages)
        second = self.tracker.update(messages)
        return first + second

    def test_startup_requires_stability_and_only_baselines_history(self):
        self.assertFalse(self.tracker.ready)
        self.assertEqual([], self.tracker.update(self.old))
        self.assertFalse(self.tracker.ready)
        self.assertEqual([], self.tracker.update(self.old))
        self.assertTrue(self.tracker.ready)
        self.assertEqual([], self.tracker.update(self.old))

    def test_each_new_occurrence_emits_once_after_two_frames(self):
        self.stable(self.old)
        new = self.old + [message("问题", 220)]
        self.assertEqual([], self.tracker.update(new))
        self.assertEqual(["问题"], [item.text for item in self.tracker.update(new)])
        self.assertEqual([], self.tracker.update(new))

    def test_consecutive_identical_texts_are_distinct_occurrences(self):
        self.stable([message("相同", 100)])
        for count in range(2, 5):
            snapshot = [message("相同", 40 + index * 60) for index in range(count)]
            self.assertEqual(["相同"], [item.text for item in self.stable(snapshot)])

    def test_outgoing_messages_anchor_sequence_without_becoming_events(self):
        self.stable(self.old)
        new = self.old + [message("自己的回复", 220, "outgoing"), message("好友问题", 280)]
        self.assertEqual(["好友问题"], [item.text for item in self.stable(new)])

    def test_upward_scroll_from_new_messages_aligns_suffix_and_prefix(self):
        self.stable(self.old)
        new = [message("历史乙", 70), message("新问题", 130)]
        self.assertEqual(["新问题"], [item.text for item in self.stable(new)])

    def test_no_shared_anchor_rebaselines_without_replying_to_visible_messages(self):
        self.stable(self.old)
        changed = [message("另一段历史", 100)]
        self.assertEqual([], self.stable(changed))
        self.assertTrue(self.tracker.rebaselined)
        self.assertEqual(["后续新消息"], [item.text for item in self.stable(changed + [message("后续新消息", 160)])])

    def test_returning_from_disjoint_history_does_not_replay_known_messages(self):
        self.stable(self.old)
        self.assertEqual([], self.stable([message("不相邻的历史页", 100)]))
        self.assertEqual([], self.stable(self.old[:1]))
        self.assertEqual([], self.stable(self.old))
        self.assertEqual(["新到达"], [item.text for item in self.stable(self.old + [message("新到达", 220)])])

    def test_unstable_ocr_text_does_not_create_an_event(self):
        self.stable(self.old)
        self.assertEqual([], self.tracker.update(self.old + [message("识别抖动", 220)]))
        self.assertEqual([], self.tracker.update(self.old))
        new = self.old + [message("正确文字", 220)]
        self.assertEqual(["正确文字"], [item.text for item in self.stable(new)])

    def test_position_animation_must_settle_before_emit(self):
        self.stable(self.old)
        self.assertEqual([], self.tracker.update(self.old + [message("新消息", 220)]))
        moved = self.old + [message("新消息", 230)]
        self.assertEqual([], self.tracker.update(moved))
        self.assertEqual(["新消息"], [item.text for item in self.tracker.update(moved)])

    def test_empty_ocr_does_not_erase_baseline_or_replay_history(self):
        self.stable(self.old)
        self.assertEqual([], self.stable([]))
        self.assertEqual([], self.stable(self.old))

    def test_missing_bubbles_do_not_replay_when_ocr_recovers(self):
        self.stable(self.old)
        self.assertEqual([], self.stable(self.old[:1]))
        self.assertEqual([], self.stable(self.old))
        self.assertEqual(["真正的新消息"], [item.text for item in self.stable(self.old + [message("真正的新消息", 220)])])

    def test_empty_initial_chat_allows_first_new_message_after_stability(self):
        self.stable([])
        self.assertEqual(["第一条新消息"], [item.text for item in self.stable([message("第一条新消息")])])

    def test_direction_changes_do_not_match_text_only(self):
        self.stable([message("一样的文字", 100, "outgoing")])
        self.assertEqual([], self.stable([message("一样的文字", 100)]))
        self.assertTrue(self.tracker.rebaselined)

    def test_whitespace_ocr_jitter_does_not_replay_message(self):
        self.stable([message("Hello  world", 100)])
        self.assertEqual([], self.stable([message("Hello world", 100)]))

    def test_reset_and_explicit_baseline(self):
        self.tracker.baseline(self.old)
        self.assertTrue(self.tracker.ready)
        self.assertEqual(["新消息"], [item.text for item in self.stable(self.old + [message("新消息", 220)])])
        self.tracker.reset()
        self.assertFalse(self.tracker.ready)
        self.assertEqual([], self.stable([message("重启时的历史")]))

    def test_identical_viewports_do_not_invent_new_identical_messages(self):
        repeated = [message("相同", 100), message("相同", 160)]
        self.stable(repeated)
        self.assertEqual([], self.stable(repeated))

    def test_stable_frames_must_be_at_least_two(self):
        with self.assertRaises(ValueError):
            SnapshotTracker(stable_frames=1)


if __name__ == "__main__":
    unittest.main()
