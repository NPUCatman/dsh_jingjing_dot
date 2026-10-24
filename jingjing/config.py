"""Read local configuration without printing or persisting API credentials."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


class ConfigError(ValueError):
    """A configuration problem that can safely be shown to the operator."""


@dataclass(frozen=True, slots=True)
class Config:
    friends: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    bot_name: str = "小鲸鱼"
    group_prefix: str = "/鲸鲸"
    cooldown_seconds: float = 2.0
    max_input_chars: int = 4000
    max_output_chars: int = 1000
    max_history_turns: int = 10
    timeout_seconds: float = 60.0
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    system_prompt: str = "你是小鲸鱼，一个友好、诚实、简洁的中文助手。"


_JSON_FIELDS = {
    "friends", "groups", "bot_name", "group_prefix", "cooldown_seconds",
    "max_input_chars", "max_output_chars", "max_history_turns", "timeout_seconds",
}


def validate_api_key(value: str) -> str:
    """Reject empty/sample values; actual validity is checked by DeepSeek."""
    value = value.strip()
    lower = value.lower()
    placeholders = (
        "your_api", "your_deepseek", "your-api", "your-deepseek", "your-key",
        "replace", "changeme", "example", "placeholder", "填入", "填写", "替换",
        "这里", "你的", "<", ">",
    )
    suffix = value[3:] if lower.startswith("sk-") else value
    if (
        len(value) < 16
        or any(char.isspace() for char in value)
        or any(marker in lower for marker in placeholders)
        or re.fullmatch(r"[xX*._-]+", suffix)
    ):
        raise ConfigError("请在项目 .env 中填写真实的 DEEPSEEK_API_KEY。")
    return value


def _names(data: dict, key: str) -> tuple[str, ...]:
    values = data.get(key, [])
    if not isinstance(values, list) or len(values) > 1000:
        raise ConfigError(f"config.json 的 {key} 必须是至多 1000 个名称组成的数组。")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values):
        raise ConfigError(f"config.json 的 {key} 必须包含非空字符串名称。")
    cleaned = tuple(v.strip() for v in values)
    if len(set(cleaned)) != len(cleaned):
        raise ConfigError(f"config.json 的 {key} 含有重复名称，请保留唯一名称。")
    return cleaned


def _number(data: dict, key: str, default: float, low: float, high: float, *, integer: bool = False):
    value = data.get(key, default)
    numeric_type = type(value) is int if integer else type(value) in (int, float)
    if not numeric_type or not low <= value <= high or not math.isfinite(value):
        kind = "整数" if integer else "数字"
        raise ConfigError(f"config.json 的 {key} 必须是 {low:g} 至 {high:g} 之间的{kind}。")
    return value


def _text(data: dict, key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ConfigError(f"config.json 的 {key} 必须是 1 至 64 字的字符串。")
    return value.strip()


def load_config(project_dir: str | Path, *, require_api_key: bool = True) -> Config:
    """Load config.json, .env and prompts/system.txt beneath project_dir.

    Environment variables take precedence over .env. Missing config.json means
    empty allowlists, so an unconfigured bot never starts answering contacts.
    This function deliberately does not change the process environment.
    """
    root = Path(project_dir).resolve()
    config_path = root / "config.json"
    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError("无法读取 config.json，请检查 UTF-8 编码和 JSON 格式。") from exc
        if not isinstance(data, dict):
            raise ConfigError("config.json 顶层必须是 JSON 对象。")
        unknown = set(data) - _JSON_FIELDS
        if unknown:
            raise ConfigError("config.json 含有不支持的字段：" + "、".join(sorted(unknown)))

    env_path = root / ".env"
    try:
        local_env = dotenv_values(env_path, encoding="utf-8-sig", interpolate=False) if env_path.exists() else {}
    except (OSError, UnicodeError) as exc:
        raise ConfigError("无法读取 .env，请检查文件权限和 UTF-8 编码。") from exc

    def environment(name: str, default: str = "") -> str:
        value = os.environ.get(name, local_env.get(name, default))
        return value.strip() if isinstance(value, str) else ""

    api_key = environment("DEEPSEEK_API_KEY")
    if require_api_key:
        api_key = validate_api_key(api_key)
    base_url = environment("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    try:
        parsed = urlsplit(base_url)
        valid_url = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        parsed.port  # Validate malformed port values without echoing the URL.
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigError("DEEPSEEK_BASE_URL 必须是无账号、参数和片段的 HTTPS 接口地址。")
    model = environment("DEEPSEEK_MODEL", "deepseek-flash")
    if not model or len(model) > 128 or any(char.isspace() for char in model):
        raise ConfigError("DEEPSEEK_MODEL 必须是非空的模型名称，且不能包含空白。")

    prompt_path = root / "prompts" / "system.txt"
    try:
        system_prompt = prompt_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise ConfigError("无法读取 prompts/system.txt，请先准备小鲸鱼的角色提示词。") from exc
    if not system_prompt or len(system_prompt) > 12000:
        raise ConfigError("prompts/system.txt 必须包含 1 至 12000 字的角色提示词。")

    bot_name = _text(data, "bot_name", "小鲸鱼")
    group_prefix = _text(data, "group_prefix", "/鲸鲸")
    if any(char.isspace() for char in bot_name) or bot_name.startswith("@"):
        raise ConfigError("bot_name 不应包含空白或开头的 @。")
    if not group_prefix.startswith("/") or any(char.isspace() for char in group_prefix):
        raise ConfigError("group_prefix 必须以 / 开头，且不能包含空白。")
    friends = _names(data, "friends")
    groups = _names(data, "groups")
    if set(friends) & set(groups):
        raise ConfigError("friends 和 groups 不能使用相同名称，请先在微信中设置可区分的会话名称。")

    return Config(
        friends=friends,
        groups=groups,
        bot_name=bot_name,
        group_prefix=group_prefix,
        cooldown_seconds=_number(data, "cooldown_seconds", 2.0, 0, 300),
        max_input_chars=_number(data, "max_input_chars", 4000, 1, 4000, integer=True),
        max_output_chars=_number(data, "max_output_chars", 1000, 1, 1000, integer=True),
        max_history_turns=_number(data, "max_history_turns", 10, 1, 10, integer=True),
        timeout_seconds=_number(data, "timeout_seconds", 60.0, 1, 180),
        api_key=api_key,
        base_url=base_url,
        model=model,
        system_prompt=system_prompt,
    )
