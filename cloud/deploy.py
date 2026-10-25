"""Explicit cloud deployment helpers. Secrets travel only through SSH stdin."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import string
import subprocess
import sys
import tarfile


CLOUD = Path(__file__).resolve().parent
PROJECT = CLOUD.parent
sys.path.insert(0, str(PROJECT))


def read_local():
    return json.loads((CLOUD / "local.json").read_text(encoding="utf-8-sig"))


def ssh_command(config, command):
    return [config.get("ssh_path", "ssh"), "-i", config["key_path"], "-p", str(config.get("port", 22)),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "-o", "ClearAllForwardings=yes",
            config["user"] + "@" + config["host"], command]


def remote(command, data=None):
    result = subprocess.run(ssh_command(read_local(), command), input=data)
    if result.returncode:
        raise RuntimeError(f"SSH operation failed (exit {result.returncode}); no automatic retry.")


def private_json(path, data):
    # Restrict the empty file before writing any credentials, including on Windows.
    with path.open("x", encoding="utf-8") as stream:
        if os.name == "nt":
            identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                      capture_output=True, text=True, check=True)
            sid = next(csv.reader(io.StringIO(identity.stdout)))[1]
            result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
                                    capture_output=True)
            if result.returncode:
                raise RuntimeError("Could not restrict private file permissions; no credentials written.")
        else:
            path.chmod(0o600)
        stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def prepare(args):
    from jingjing.config import load_config
    existing = load_config(PROJECT)
    config_path = CLOUD / "local.json"
    if config_path.exists() or (CLOUD / "credentials.local.json").exists():
        raise RuntimeError("Private cloud settings already exist; preserve and edit them deliberately.")
    local = {"host": args.host, "user": args.user, "port": 22,
        "key_path": str(Path(args.key).resolve()), "ssh_path": "C:\\Windows\\System32\\OpenSSH\\ssh.exe" if os.name == "nt" else "ssh",
        "remote_root": "/opt/jingjing", "panel_port": 18780,
        "forwards": {"astrbot": {"local": 16185, "remote": 6185},
                     "snowluma": {"local": 15099, "remote": 5099},
                     "novnc": {"local": 16081, "remote": 6081}},
        "bot_qq": args.bot_qq, "admin_qq": args.admin_qq,
        "model": existing.model, "api_base": existing.base_url.rstrip("/")}
    if not local["api_base"].endswith("/v1"):
        local["api_base"] += "/v1"
    alphabet = string.ascii_letters + string.digits
    credentials = {"astrbot_password": "Af1!" + secrets.token_urlsafe(16),
        "snowluma_password": "Sf!" + secrets.token_urlsafe(16),
        "novnc_password": "".join(secrets.choice(alphabet) for _ in range(8)),
        "onebot_token": secrets.token_urlsafe(36), "deepseek_api_key": existing.api_key}
    private_json(config_path, local)
    private_json(CLOUD / "credentials.local.json", credentials)
    print("Private local connection settings and unique passwords prepared.")


def bundle():
    local = read_local()
    credentials = json.loads((CLOUD / "credentials.local.json").read_text())
    buffer = io.BytesIO()
    environment = {"DEEPSEEK_API_KEY": credentials["deepseek_api_key"],
        "ASTRBOT_DASHBOARD_INITIAL_PASSWORD": credentials["astrbot_password"],
        "SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD": credentials["snowluma_password"],
        "VNC_PASSWD": credentials["novnc_password"]}
    # Compose single quoted dotenv values are literal. Refuse control characters
    # instead of creating additional environment entries from private inputs.
    if any("\n" in value or "\r" in value or "'" in value for value in environment.values()):
        raise RuntimeError("Deployment secret contains unsupported dotenv characters.")
    env = "".join(f"{name}='{value}'\n" for name, value in environment.items())
    bot = {key: local[key] for key in ("bot_qq", "admin_qq", "model", "api_base")}
    entries = {"compose.yaml": (CLOUD / "compose.yaml").read_bytes(),
        "bin/initialize.py": (CLOUD / "initialize.py").read_bytes(),
        "bin/status.py": (CLOUD / "server_status.py").read_bytes(),
        "credentials.json": json.dumps(credentials).encode(),
        "bot.json": json.dumps(bot).encode(), ".env": env.encode()}
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), 0o600
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def upload():
    root = shlex.quote(read_local()["remote_root"])
    # Never overwrite a running stack or credentials on an accidental rerun.
    remote(f"sudo -n test ! -e {root}/compose.yaml && sudo -n install -d -m700 {root} {root}/bin")
    remote(f"sudo -n tar -xzf - -C {root} --no-same-owner", bundle())
    remote(f"sudo -n python3 {root}/bin/initialize.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("prepare")
    for name in ("host", "key", "bot-qq", "admin-qq"):
        init.add_argument("--" + name, required=True)
    init.add_argument("--user", default="ubuntu")
    commands.add_parser("upload")
    commands.add_parser("start")
    commands.add_parser("status")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "upload":
        upload()
    elif args.command == "start":
        root = shlex.quote(read_local()["remote_root"])
        remote(f"sudo -n docker compose --env-file {root}/.env -f {root}/compose.yaml up -d --pull never")
    elif args.command == "status":
        root = shlex.quote(read_local()["remote_root"])
        remote(f"sudo -n python3 {root}/bin/status.py")


if __name__ == "__main__":
    main()
