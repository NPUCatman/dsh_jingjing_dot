"""Small synchronous DeepSeek client using its OpenAI-compatible HTTP API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from .config import Config, validate_api_key


class DeepSeekError(RuntimeError):
    """A safe, user-facing API failure without response bodies or credentials."""


class DeepSeekClient:
    def __init__(self, config: Config, *, transport: httpx.BaseTransport | None = None):
        api_key = validate_api_key(config.api_key)
        self._model = config.model
        self._endpoint = config.base_url.rstrip("/") + "/chat/completions"
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    def reply(self, messages: Sequence[Mapping[str, str]]) -> str:
        payload = {
            "model": self._model,
            "messages": [dict(message) for message in messages],
            "stream": False,
            "thinking": {"type": "disabled"},
            "max_tokens": 2048,
        }
        try:
            # Generations are deliberately never retried: retries may cost twice.
            response = self._http.post(self._endpoint, json=payload)
        except httpx.TimeoutException:
            raise DeepSeekError("小鲸鱼服务暂时没有及时响应，请稍后再试。") from None
        except httpx.HTTPError:
            raise DeepSeekError("小鲸鱼服务暂时连接失败，请检查网络后再试。") from None

        if response.status_code == 401:
            raise DeepSeekError("小鲸鱼服务认证失败，请联系管理员检查 API Key。")
        if response.status_code == 402:
            raise DeepSeekError("小鲸鱼服务余额不足，请联系管理员检查 DeepSeek 账户。")
        if response.status_code == 429:
            raise DeepSeekError("小鲸鱼服务暂时繁忙，已达到请求频率限制，请稍后再试。")
        if response.status_code >= 500:
            raise DeepSeekError("小鲸鱼服务暂时不可用，请稍后再试。")
        if not 200 <= response.status_code < 300:
            raise DeepSeekError("小鲸鱼服务请求未被接受，请联系管理员检查接口和模型配置。")

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise DeepSeekError("小鲸鱼服务返回了无法读取的回复，请稍后再试。") from None
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekError("小鲸鱼服务暂时没有生成文字回复，请稍后再试。")
        return content.strip()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DeepSeekClient:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
