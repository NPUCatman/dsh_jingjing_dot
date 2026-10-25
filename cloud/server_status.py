"""Read-only server snapshot for the local panel; stdout contains only safe JSON.

Install as /opt/jingjing/bin/status.py and invoke with sudo -n python3.
No third-party Python packages or Docker socket libraries are required.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from typing import Any
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
CONTAINERS = {"astrbot": "jingjing-astrbot", "snowluma": "jingjing-snowluma"}
HTTP_ROOT = "http://127.0.0.1:5099"
MAX_BYTES = 1_000_000
REDACTED = "[已隐藏]"
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return {}
        value = json.loads(raw.decode("utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def bot_settings() -> dict[str, str]:
    """Personal account identifiers stay in a private deployment file."""
    raw = read_json(ROOT / "bot.json")
    return {key: str(raw.get(key, "")) if re.fullmatch(r"[0-9]{5,12}", str(raw.get(key, ""))) else ""
            for key in ("bot_qq", "admin_qq")}


def make_redactor(credentials: dict[str, Any]):
    """Hide stored secrets and credentials generated before the current ones."""
    secrets = set()
    for value in credentials.values():
        if isinstance(value, str) and value:
            secrets.add(value)
            secrets.add(json.dumps(value, ensure_ascii=True)[1:-1])
    ordered = sorted(secrets, key=len, reverse=True)

    def redact(value: Any) -> str:
        text = ANSI.sub("", str(value))
        text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                      REDACTED, text, flags=re.S)
        for secret in ordered:
            text = text.replace(secret, REDACTED)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", REDACTED, text)
        text = re.sub(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1" + REDACTED, text)
        text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", REDACTED, text)
        text = re.sub(
            r"(?im)((?:initial\s+password|remote\s+desktop\s+password|"
            r"临时密码|远程桌面密码|初始密码)\s*[:：=]\s*)[^\r\n]+",
            r"\1" + REDACTED, text,
        )
        text = re.sub(
            r'''(?i)((?:api[_-]?key|access[_-]?token|authorization|password|passwd|secret|token|'''
            r'''ASTRBOT_DASHBOARD_INITIAL_PASSWORD|SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD|VNC_PASSWD)'''
            r'''["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)''',
            r"\1" + REDACTED, text,
        )
        return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)

    return redact


def run_docker(arguments: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["docker", *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def inspect_services(redact) -> tuple[dict[str, dict[str, str]], set[str]]:
    services = {
        name: {"state": "unavailable", "health": "unknown", "started_at": "", "image": ""}
        for name in CONTAINERS
    }
    result = run_docker(["inspect", *CONTAINERS.values()])
    if result is None or len(result.stdout) > MAX_BYTES:
        return services, set()
    try:
        inspected = json.loads(result.stdout)
    except (ValueError, TypeError):
        return services, set()
    if not isinstance(inspected, list):
        return services, set()
    owned = set()
    for item in inspected:
        if not isinstance(item, dict):
            continue
        config = item.get("Config") or {}
        if not isinstance(config, dict):
            continue
        labels = config.get("Labels") or {}
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != "jingjing":
            continue
        for name, container in CONTAINERS.items():
            if item.get("Name") != "/" + container:
                continue
            state = item.get("State") or {}
            if not isinstance(state, dict):
                continue
            health = state.get("Health") or {}
            services[name] = {
                "state": redact(state.get("Status", "unknown"))[:80],
                "health": redact(health.get("Status", "none") if isinstance(health, dict) else "unknown")[:80],
                "started_at": redact(state.get("StartedAt", ""))[:80],
                "image": redact(config.get("Image", ""))[:512],
            }
            owned.add(name)
    if result.returncode:
        for name in set(CONTAINERS) - owned:
            services[name]["state"] = "not_found"
    return services, owned


def service_logs(name: str, redact) -> list[str]:
    result = run_docker(["logs", "--timestamps", "--tail", "80", CONTAINERS[name]])
    if result is None or result.returncode:
        return []
    # Docker returns application stderr on its own stderr, even on success.
    combined = result.stdout[-MAX_BYTES:] + "\n" + result.stderr[-MAX_BYTES:]
    return [line[:2000] for line in redact(combined).splitlines() if line.strip()][-80:]


def server_info() -> dict[str, Any]:
    result: dict[str, Any] = {
        "hostname": socket.gethostname(), "os": "Linux", "cpu_count": os.cpu_count(),
        "uptime_seconds": None, "load": [],
        "memory": {"total": None, "available": None},
        "disk": {"total": None, "used": None, "free": None},
    }
    try:
        release = Path("/etc/os-release").read_text(encoding="utf-8")
        for line in release.splitlines():
            if line.startswith("PRETTY_NAME="):
                result["os"] = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    try:
        result["uptime_seconds"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError, OverflowError):
        pass
    try:
        result["load"] = list(os.getloadavg())
    except (AttributeError, OSError):
        pass
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            field, _, value = line.partition(":")
            if field in ("MemTotal", "MemAvailable"):
                memory[field] = int(value.split()[0]) * 1024
        result["memory"] = {"total": memory.get("MemTotal"), "available": memory.get("MemAvailable")}
    except (OSError, ValueError, IndexError):
        pass
    try:
        disk = shutil.disk_usage(str(ROOT))
        result["disk"] = {"total": disk.total, "used": disk.used, "free": disk.free}
    except OSError:
        pass
    return result


def model_name(config: dict[str, Any]) -> str:
    try:
        selected = config.get("agent_runner", {}).get("config", {}).get("model", {}).get("provider_id")
        providers = config.get("provider", [])
        if not isinstance(providers, list):
            return ""
        active = [p for p in providers if isinstance(p, dict) and p.get("enable", True)]
        provider = next((p for p in active if p.get("id") == selected), None)
        provider = provider or (active[0] if active else {})
        return str(provider.get("model", ""))[:200]
    except (AttributeError, TypeError):
        return ""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bootstrap password or bearer token to another listener.
        return None


def request_json(opener, path: str, deadline: float, *, token: str = "", body=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(HTTP_ROOT + path, data=data, headers=headers)
    try:
        with opener.open(request, timeout=min(3, remaining)) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def snowluma_password_matches(password: str) -> bool:
    """Avoid repeated failed logins after the operator changes the WebUI password.

    SnowLuma v2.2.0 webui/auth.ts stores a 16-byte hexadecimal salt and a
    64-byte scrypt hash (N=16384, r=8, p=1). Unknown/missing formats remain
    unavailable; the status probe must never guess credentials over HTTP.
    """
    state = read_json(ROOT / "snowluma/data/config/webui.json")
    salt, expected = state.get("passwordSalt"), state.get("passwordHash")
    if not isinstance(salt, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", salt):
        return False
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{128}", expected):
        return False
    try:
        derived = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                 n=16384, r=8, p=1, dklen=64)
        return hmac.compare_digest(derived, bytes.fromhex(expected))
    except (AttributeError, ValueError, TypeError, UnicodeError):
        return False


def snowluma_status(credentials: dict[str, Any], bot_qq: str) -> tuple[bool | None, bool | None]:
    password = credentials.get("snowluma_password")
    if not isinstance(password, str) or not password or not bot_qq:
        return None, None
    if not snowluma_password_matches(password):
        return None, None
    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 3
    auth = request_json(opener, "/api/login", deadline, body={"password": password})
    if not auth or auth.get("success") is not True or not isinstance(auth.get("token"), str):
        return None, None
    token = auth["token"]
    if not token:
        return None, None
    try:
        accounts = request_json(opener, "/api/qq-list", deadline, token=token)
        online = None
        if accounts and isinstance(accounts.get("list"), list):
            online = any(isinstance(a, dict) and str(a.get("uin")) == bot_qq for a in accounts["list"])
        connections = request_json(opener, "/api/connections", deadline, token=token)
        connected = None
        if connections and isinstance(connections.get("list"), list):
            connected = False
            for account in connections["list"]:
                if not isinstance(account, dict) or str(account.get("uin")) != bot_qq:
                    continue
                adapters = account.get("adapters", [])
                if not isinstance(adapters, list):
                    continue
                connected = any(
                    isinstance(a, dict) and a.get("name") == "astrbot"
                    and a.get("kind") == "wsClient" and a.get("status") == "ok"
                    for a in adapters
                )
                break
        return online, False if online is False else connected
    finally:
        request_json(opener, "/api/logout", deadline, token=token, body={})


def collect_status() -> dict[str, Any]:
    credentials = read_json(ROOT / "credentials.json")
    accounts = bot_settings()
    redact = make_redactor(credentials)
    config = read_json(ROOT / "astrbot/data/cmd_config.json")
    with ThreadPoolExecutor(max_workers=4) as pool:
        host_future = pool.submit(server_info)
        bot_future = pool.submit(snowluma_status, credentials, accounts["bot_qq"])
        services, owned = inspect_services(redact)
        logs = {name: pool.submit(service_logs, name, redact) for name in owned}
        online, connected = bot_future.result()
        return {
            "server": host_future.result(), "services": services,
            "bot": {"qq": accounts["bot_qq"], "admin_qq": accounts["admin_qq"], "model": redact(model_name(config)),
                    "online": online, "onebot_connected": connected},
            "logs": {name: logs[name].result() if name in logs else [] for name in CONTAINERS},
            "sampled_at": datetime.now(timezone.utc).isoformat(),
        }


def main() -> None:
    try:
        status = collect_status()
    except Exception:
        # Never serialize exception messages: dependencies may include credentials.
        accounts = bot_settings()
        status = {"server": {}, "services": {}, "bot": {"qq": accounts["bot_qq"], "admin_qq": accounts["admin_qq"],
                  "model": "", "online": None, "onebot_connected": None},
                  "logs": {name: [] for name in CONTAINERS},
                  "sampled_at": datetime.now(timezone.utc).isoformat()}
    print(json.dumps(status, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
