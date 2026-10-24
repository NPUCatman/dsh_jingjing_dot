"""Platform-independent allowlists, commands and short-lived chat memory."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .config import Config
from .deepseek import DeepSeekError


@dataclass(frozen=True, slots=True)
class Event:
    chat: str
    sender: str
    text: str
    is_group: bool = False


class ReplyClient(Protocol):
    def reply(self, messages: Sequence[Mapping[str, str]]) -> str: ...


class BotEngine:
    def __init__(self, config: Config, client: ReplyClient, *, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._client = client
        self._clock = clock
        self._histories: dict[tuple[bool, str, str], list[dict[str, str]]] = {}
        self._last_request: dict[tuple[bool, str, str], float] = {}
        self._lock = threading.Lock()

    def handle(self, event: Event) -> str | None:
        """Return a reply or None; the adapter filters self messages and sends it.

        Group mentions are recognized from leading text, because Event does not
        carry platform mention metadata. Only allowlisted groups are considered.
        The synchronous adapter may call this safely from several threads.
        """
        if not all(isinstance(value, str) for value in (event.chat, event.sender, event.text)):
            return None
        chat, sender, text = event.chat.strip(), event.sender.strip(), event.text.strip()
        if not chat or not sender or not text:
            return None
        if event.is_group:
            if chat not in self.config.groups:
                return None
            text = self._group_text(text)
            if text is None:
                return None
        elif chat not in self.config.friends:
            return None
        key = (event.is_group, chat, sender if event.is_group else "")

        # A single lock also prevents two simultaneous generations from racing
        # the same conversation's history or bypassing its cooldown.
        with self._lock:
            if text in ("/帮助", "/help"):
                return self._help(event.is_group)
            if text in ("/清空", "/reset"):
                self._histories.pop(key, None)
                self._last_request.pop(key, None)
                return "已清空本群的共享对话记忆。🐳" if event.is_group else "已清空这段对话的记忆。🐳"
            if len(text) > self.config.max_input_chars:
                return f"这条消息太长啦，请控制在 {self.config.max_input_chars} 字以内。🐳"

            now = self._clock()
            last = self._last_request.get(key)
            if last is not None and now - last < self.config.cooldown_seconds:
                return None
            self._last_request[key] = now
            history = self._histories.get(key, [])
            messages = [
                {"role": "system", "content": self.config.system_prompt},
                *[dict(message) for message in history],
                {"role": "user", "content": text},
            ]
            try:
                reply = self._client.reply(messages)
            except DeepSeekError as exc:
                return str(exc)
            except Exception:
                # Never expose exception text: a backend exception may contain
                # a URL, a credential, or the user's private conversation.
                return "小鲸鱼服务暂时出错，请稍后再试。"
            if not isinstance(reply, str) or not reply.strip():
                return "小鲸鱼服务暂时没有生成文字回复，请稍后再试。"
            reply = reply.strip()
            if len(reply) > self.config.max_output_chars:
                reply = reply[: self.config.max_output_chars - 1] + "…"
            updated = [*history, {"role": "user", "content": text}, {"role": "assistant", "content": reply}]
            self._histories[key] = updated[-2 * self.config.max_history_turns :]
            return reply

    def _group_text(self, text: str) -> str | None:
        for trigger in (self.config.group_prefix, "@" + self.config.bot_name):
            if text == trigger:
                return "/帮助"
            if text.startswith(trigger):
                remainder = text[len(trigger) :]
                if remainder and remainder[0].isspace():
                    return remainder.strip() or "/帮助"
        return None

    def _help(self, is_group: bool) -> str:
        if is_group:
            prefix = self.config.group_prefix
            return (
                f"我是{self.config.bot_name} 🐳\n"
                f"群里请用“{prefix} 你的问题”或在开头 @我 后空一格提问。\n"
                f"{prefix} /帮助：查看帮助\n"
                f"{prefix} /清空：清空本群共享记忆\n"
                "本群成员共享对话记忆；不同群、私聊互相独立。"
            )
        return (
            f"我是{self.config.bot_name} 🐳，直接发文字就可以聊天。\n"
            "/帮助：查看帮助\n"
            "/清空：清空当前对话记忆\n"
            "记忆只保存在程序内存中，重启后清空。"
        )
