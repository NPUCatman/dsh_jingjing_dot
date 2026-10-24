from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jingjing.config import Config, ConfigError, load_config
from jingjing.deepseek import DeepSeekError
from jingjing.engine import BotEngine, Event


class FakeClient:
    def __init__(self, answer="你好呀 🐳"):
        self.answer = answer
        self.calls = []

    def reply(self, messages):
        self.calls.append([dict(message) for message in messages])
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.config = Config(friends=("好友甲", "好友乙"), groups=("测试群",), system_prompt="你是小鲸鱼。")
        self.client = FakeClient()
        self.engine = BotEngine(self.config, self.client, clock=lambda: self.now)

    def private(self, text, chat="好友甲"):
        return Event(chat=chat, sender=chat, text=text)

    def group(self, text, sender="群友甲", chat="测试群"):
        return Event(chat=chat, sender=sender, text=text, is_group=True)

    def test_unconfigured_bot_ignores_all_contacts(self):
        engine = BotEngine(Config(), self.client)
        self.assertIsNone(engine.handle(self.private("你好")))
        self.assertIsNone(engine.handle(self.group("/鲸鲸 你好")))
        self.assertEqual([], self.client.calls)

    def test_only_allowlisted_private_and_group_chats_reply(self):
        self.assertIsNone(self.engine.handle(self.private("你好", "陌生人")))
        self.assertIsNone(self.engine.handle(self.group("/鲸鲸 你好", chat="未授权群")))
        self.assertEqual("你好呀 🐳", self.engine.handle(self.private("你好")))
        self.assertEqual("你好", self.client.calls[0][-1]["content"])

    def test_group_requires_trigger_at_start_and_word_boundary(self):
        for text in ("你好", "说说 @小鲸鱼 的故事", "@小鲸鱼同学 你好", "/鲸鲸其他 你好"):
            with self.subTest(text=text):
                self.assertIsNone(self.engine.handle(self.group(text)))
        self.assertEqual([], self.client.calls)
        self.assertEqual("你好呀 🐳", self.engine.handle(self.group("/鲸鲸 帮我写一句话")))
        self.assertEqual("帮我写一句话", self.client.calls[-1][-1]["content"])

    def test_group_accepts_wechat_mention_space(self):
        self.assertEqual("你好呀 🐳", self.engine.handle(self.group("@小鲸鱼\u2005你好")))
        self.assertEqual("你好", self.client.calls[-1][-1]["content"])

    def test_bare_trigger_and_help_use_no_api(self):
        for event in (self.private("/帮助"), self.group("/鲸鲸"), self.group("@小鲸鱼"), self.group("/鲸鲸 /帮助")):
            with self.subTest(event=event):
                self.assertIn("帮助", self.engine.handle(event))
        self.assertEqual([], self.client.calls)

    def test_private_history_isolated_between_contacts(self):
        self.engine.handle(self.private("甲的秘密"))
        self.engine.handle(self.private("乙的问题", "好友乙"))
        self.assertEqual(["你是小鲸鱼。", "乙的问题"], [m["content"] for m in self.client.calls[-1]])
        self.now += 2
        self.engine.handle(self.private("甲的后续"))
        self.assertEqual("甲的秘密", self.client.calls[-1][1]["content"])

    def test_group_history_isolated_per_sender_and_from_private_chat(self):
        self.engine.handle(self.group("/鲸鲸 甲的群聊"))
        self.engine.handle(self.group("/鲸鲸 乙的群聊", sender="群友乙"))
        self.assertEqual(2, len(self.client.calls[-1]))
        self.engine.handle(self.private("私聊问题"))
        self.assertEqual(2, len(self.client.calls[-1]))
        self.now += 2
        self.engine.handle(self.group("/鲸鲸 甲的后续"))
        self.assertEqual("甲的群聊", self.client.calls[-1][1]["content"])

    def test_clear_only_resets_current_group_member(self):
        self.engine.handle(self.group("/鲸鲸 甲的群聊"))
        self.engine.handle(self.group("/鲸鲸 乙的群聊", sender="群友乙"))
        self.assertIn("已清空", self.engine.handle(self.group("/鲸鲸 /清空")))
        self.engine.handle(self.group("/鲸鲸 甲的新问题"))
        self.assertEqual(2, len(self.client.calls[-1]))
        self.now += 2
        self.engine.handle(self.group("/鲸鲸 乙的后续", sender="群友乙"))
        self.assertEqual("乙的群聊", self.client.calls[-1][1]["content"])

    def test_cooldown_prevents_second_generation_and_is_per_session(self):
        self.engine.handle(self.private("第一条"))
        self.assertIsNone(self.engine.handle(self.private("太快了")))
        self.assertEqual("你好呀 🐳", self.engine.handle(self.private("另一个好友", "好友乙")))
        self.assertEqual(2, len(self.client.calls))
        self.now += 2
        self.assertEqual("你好呀 🐳", self.engine.handle(self.private("两秒后")))

    def test_failed_generation_does_not_enter_history(self):
        self.engine.handle(self.private("成功问题"))
        self.now += 2
        self.client.answer = DeepSeekError("小鲸鱼服务暂时不可用。")
        self.assertIn("暂时不可用", self.engine.handle(self.private("失败问题")))
        self.now += 2
        self.client.answer = "恢复了"
        self.engine.handle(self.private("恢复问题"))
        contents = [m["content"] for m in self.client.calls[-1]]
        self.assertIn("成功问题", contents)
        self.assertNotIn("失败问题", contents)

    def test_unexpected_backend_exception_is_not_exposed(self):
        self.client.answer = RuntimeError("secret-key https://private.example/user-text")
        answer = self.engine.handle(self.private("问题"))
        self.assertIn("暂时出错", answer)
        self.assertNotIn("secret", answer)
        self.assertNotIn("private", answer)

    def test_history_contains_at_most_ten_completed_rounds(self):
        for number in range(12):
            self.engine.handle(self.private(f"问题{number}"))
            self.now += 2
        self.engine.handle(self.private("最后问题"))
        sent = self.client.calls[-1]
        self.assertEqual(22, len(sent))  # system + 20 remembered messages + user
        self.assertEqual("问题2", sent[1]["content"])
        self.assertNotIn("问题0", [m["content"] for m in sent])

    def test_long_input_is_rejected_without_generation(self):
        self.assertIn("4000", self.engine.handle(self.private("鲸" * 4001)))
        self.assertEqual([], self.client.calls)

    def test_output_is_limited_and_memory_matches_sent_reply(self):
        self.client.answer = "鲸" * 2000
        answer = self.engine.handle(self.private("长回复"))
        self.assertEqual(1000, len(answer))
        self.assertTrue(answer.endswith("…"))
        self.now += 2
        self.engine.handle(self.private("后续"))
        self.assertEqual(answer, self.client.calls[-1][-2]["content"])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "prompts").mkdir()
        (self.root / "prompts" / "system.txt").write_text("你是小鲸鱼。", encoding="utf-8")
        self.environ = patch.dict(os.environ, {}, clear=True)
        self.environ.start()

    def tearDown(self):
        self.environ.stop()
        self.temp.cleanup()

    def write_config(self, value):
        (self.root / "config.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_missing_local_config_has_empty_allowlists_and_current_model(self):
        config = load_config(self.root, require_api_key=False)
        self.assertEqual((), config.friends)
        self.assertEqual((), config.groups)
        self.assertEqual("deepseek-flash", config.model)

    def test_api_key_is_required_and_placeholder_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.root)
        (self.root / ".env").write_text("DEEPSEEK_API_KEY=your_deepseek_api_key_here\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_environment_precedence_and_secret_not_in_repr(self):
        file_key = "sk-" + "a1" * 16
        real_key = "sk-" + "b2" * 16
        (self.root / ".env").write_text(f"DEEPSEEK_API_KEY={file_key}\nDEEPSEEK_MODEL=file-model\n", encoding="utf-8")
        os.environ["DEEPSEEK_API_KEY"] = real_key
        os.environ["DEEPSEEK_MODEL"] = "environment-model"
        config = load_config(self.root)
        self.assertEqual(real_key, config.api_key)
        self.assertEqual("environment-model", config.model)
        self.assertNotIn(real_key, repr(config))
        self.assertNotIn("DEEPSEEK_BASE_URL", os.environ)

    def test_invalid_types_ranges_duplicates_and_unknown_fields(self):
        for data in (
            {"friends": "全部"}, {"groups": [""]}, {"friends": ["甲", "甲"]},
            {"friends": ["同名会话"], "groups": ["同名会话"]},
            {"max_history_turns": True}, {"max_history_turns": 11},
            {"max_input_chars": 4001}, {"max_output_chars": 0},
            {"cooldown_seconds": float("nan")}, {"cooldown_seconds": -1},
            {"timeout_seconds": 10**1000}, {"group_prefix": "你好"},
            {"bot_name": "@小鲸鱼"}, {"typo": "字段"},
        ):
            with self.subTest(data=data):
                self.write_config(data)
                with self.assertRaises(ConfigError):
                    load_config(self.root, require_api_key=False)

    def test_non_https_or_credential_bearing_url_is_rejected_without_echo(self):
        for endpoint in ("http://api.deepseek.com", "https://user:secret@api.example.com", "https://api.example.com?key=secret"):
            with self.subTest(endpoint=endpoint):
                os.environ["DEEPSEEK_BASE_URL"] = endpoint
                with self.assertRaises(ConfigError) as raised:
                    load_config(self.root, require_api_key=False)
                self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
