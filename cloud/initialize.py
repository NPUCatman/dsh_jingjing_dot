"""Initialize only new cloud application data; preserve existing state."""

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def write_new(path, value):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def main():
    os.umask(0o077)
    secrets = json.loads((ROOT / "credentials.json").read_text())
    bot = json.loads((ROOT / "bot.json").read_text())
    admin, qq = bot["admin_qq"], bot["bot_qq"]
    token = secrets["onebot_token"]
    config = {
        "dashboard": {"enable": True, "host": "0.0.0.0", "port": 6185},
        "admins_id": [admin],
        "platform_settings": {
            "enable_id_white_list": True,
            "id_whitelist": [f"qq_snowluma:FriendMessage:{admin}"],
            "wl_ignore_admin_on_group": False,
            "wl_ignore_admin_on_friend": False,
        },
        "platform": [{"id": "qq_snowluma", "type": "aiocqhttp", "enable": True,
                      "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199,
                      "ws_reverse_token": token}],
        "provider_sources": [{"id": "deepseek", "provider": "deepseek",
            "type": "openai_chat_completion", "provider_type": "chat_completion",
            "enable": True, "key": ["$DEEPSEEK_API_KEY"], "api_base": bot["api_base"],
            "timeout": 120, "proxy": "", "custom_headers": {}}],
        "provider": [{"id": "deepseek_flash", "provider_source_id": "deepseek",
            "type": "openai_chat_completion", "provider_type": "chat_completion",
            "enable": True, "model": bot["model"],
            "custom_extra_body": {"thinking": {"type": "disabled"}}}],
        "agent_runner": {"runner_type": "local", "config": {
            "model": {"provider_id": "deepseek_flash"}}},
    }
    write_new(ROOT / "astrbot/data/cmd_config.json", config)
    onebot = {"mode": "snapshot", "networks": {
        "httpServers": [], "httpClients": [], "wsServers": [],
        "wsClients": [{"name": "astrbot", "enabled": True, "url": "ws://astrbot:6199/ws",
            "role": "Universal", "accessToken": token, "messageFormat": "array",
            "reportSelfMessage": False, "reconnectIntervalMs": 5000}]},
        "statusCommand": {"enabled": False, "swallow": False, "cooldownSeconds": 5, "trigger": "#sl"},
        "historySync": {"enabled": False}, "notifications": {"channelIds": []}}
    write_new(ROOT / f"snowluma/data/config/onebot_{qq}.json", onebot)
    for name in ("snowluma/qq-config", "snowluma/qq-share"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    print("Cloud configuration initialized; existing application data was preserved.")


if __name__ == "__main__":
    main()
