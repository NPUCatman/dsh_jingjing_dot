from __future__ import annotations

import json
import unittest
from dataclasses import replace

import httpx

from jingjing.config import Config, ConfigError
from jingjing.deepseek import DeepSeekClient, DeepSeekError


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(api_key="sk-" + "a1" * 16)
        self.messages = [{"role": "system", "content": "你是小鲸鱼"}, {"role": "user", "content": "你好"}]

    def test_successful_request_has_current_model_and_thinking_disabled(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "  你好呀 🐳  "}}]})

        with DeepSeekClient(self.config, transport=httpx.MockTransport(respond)) as client:
            self.assertEqual("你好呀 🐳", client.reply(self.messages))
        self.assertEqual(1, len(requests))
        self.assertEqual("https://api.deepseek.com/chat/completions", str(requests[0].url))
        self.assertEqual("Bearer " + self.config.api_key, requests[0].headers["authorization"])
        payload = json.loads(requests[0].content)
        self.assertEqual("deepseek-flash", payload["model"])
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertEqual(2048, payload["max_tokens"])
        self.assertFalse(payload["stream"])
        self.assertEqual(self.messages, payload["messages"])

    def test_model_and_v1_base_url_are_configurable(self):
        config = replace(self.config, base_url="https://api.deepseek.com/v1/", model="configured-model")

        def respond(request):
            self.assertEqual("/v1/chat/completions", request.url.path)
            self.assertEqual("configured-model", json.loads(request.content)["model"])
            return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})

        with DeepSeekClient(config, transport=httpx.MockTransport(respond)) as client:
            self.assertEqual("好", client.reply(self.messages))

    def test_status_errors_are_specific_redacted_and_never_retried(self):
        for status, hint in ((401, "认证失败"), (402, "余额不足"), (429, "频率限制"), (500, "暂时不可用"), (503, "暂时不可用"), (400, "未被接受"), (302, "未被接受")):
            with self.subTest(status=status):
                calls = []

                def respond(request):
                    calls.append(request)
                    return httpx.Response(status, json={"error": "SECRET-RESPONSE " + self.config.api_key})

                with DeepSeekClient(self.config, transport=httpx.MockTransport(respond)) as client:
                    with self.assertRaises(DeepSeekError) as raised:
                        client.reply(self.messages)
                self.assertIn(hint, str(raised.exception))
                self.assertNotIn("SECRET-RESPONSE", str(raised.exception))
                self.assertNotIn(self.config.api_key, str(raised.exception))
                self.assertEqual(1, len(calls))

    def test_timeout_and_network_errors_are_redacted_and_never_retried(self):
        for error_type, hint in ((httpx.ReadTimeout, "没有及时响应"), (httpx.ConnectError, "连接失败")):
            with self.subTest(error_type=error_type):
                calls = []

                def respond(request):
                    calls.append(request)
                    raise error_type("SECRET-ERROR " + self.config.api_key, request=request)

                with DeepSeekClient(self.config, transport=httpx.MockTransport(respond)) as client:
                    with self.assertRaises(DeepSeekError) as raised:
                        client.reply(self.messages)
                self.assertIn(hint, str(raised.exception))
                self.assertNotIn("SECRET-ERROR", str(raised.exception))
                self.assertNotIn(self.config.api_key, str(raised.exception))
                self.assertEqual(1, len(calls))

    def test_malformed_empty_and_nontext_results_are_rejected(self):
        bodies = (
            b"not-json SECRET", b"{}", b"[]", b'{"choices": []}',
            b'{"choices": [{"message": {"content": null}}]}',
            b'{"choices": [{"message": {"content": "   "}}]}',
            b'{"choices": [{"message": {"content": []}}]}',
        )
        for body in bodies:
            with self.subTest(body=body):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
                with DeepSeekClient(self.config, transport=transport) as client:
                    with self.assertRaises(DeepSeekError) as raised:
                        client.reply(self.messages)
                self.assertNotIn("SECRET", str(raised.exception))

    def test_client_refuses_missing_or_placeholder_key_before_network(self):
        for value in ("", "your_deepseek_api_key_here", "sk-xxxxxxxxxxxxxxxxxxxxxxxx"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    DeepSeekClient(replace(self.config, api_key=value))


if __name__ == "__main__":
    unittest.main()
