"""Local-only control panel and owned SSH tunnels; no third-party packages."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import webbrowser


ROOT = Path(__file__).resolve().parent
SERVICE_NAMES = ("astrbot", "snowluma", "novnc")


class ManagerError(RuntimeError):
    """A useful, credential-free explanation suitable for the local console."""


def _port(value: Any, label: str) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ManagerError(f"{label} 必须是 1024 到 65535 之间的整数。")
    return value


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    port: int
    key_path: Path
    ssh_path: str
    remote_root: str
    panel_port: int
    forwards: dict[str, dict[str, int]]
    bot_qq: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise ManagerError("无法读取 cloud/local.json，请先完成本地连接配置。") from None
        if not isinstance(raw, dict):
            raise ManagerError("local.json 必须是 JSON 对象。")
        host, user = str(raw.get("host", "")), str(raw.get("user", ""))
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
                raise ManagerError("服务器地址格式不正确。") from None
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}", user):
            raise ManagerError("SSH 用户名格式不正确。")
        ssh_port = raw.get("port", 22)
        if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
            raise ManagerError("SSH 端口格式不正确。")
        key_path = Path(str(raw.get("key_path", ""))).expanduser()
        if not key_path.is_absolute() or not key_path.is_file():
            raise ManagerError("SSH 私钥文件不存在，请检查 local.json 的 key_path。")
        ssh_setting = str(raw.get("ssh_path", "ssh"))
        ssh_path = shutil.which("ssh" if ssh_setting == "systemssh" else ssh_setting)
        if not ssh_path:
            raise ManagerError("未找到 OpenSSH 客户端，请安装 Windows 的 OpenSSH 客户端。")
        remote_root = str(raw.get("remote_root", "/opt/jingjing"))
        if not re.fullmatch(r"/[a-zA-Z0-9_/-]+", remote_root) or ".." in remote_root:
            raise ManagerError("remote_root 必须是没有空格的绝对目录。")
        forwards = raw.get("forwards")
        if not isinstance(forwards, dict) or set(forwards) != set(SERVICE_NAMES):
            raise ManagerError("forwards 必须包含 astrbot、snowluma 和 novnc。")
        normalized = {}
        for name in SERVICE_NAMES:
            item = forwards[name]
            if not isinstance(item, dict):
                raise ManagerError(f"{name} 的端口配置不正确。")
            normalized[name] = {part: _port(item.get(part), f"{name}.{part}") for part in ("local", "remote")}
        panel_port = _port(raw.get("panel_port", 18780), "panel_port")
        ports = [panel_port, *(v["local"] for v in normalized.values())]
        if len(ports) != len(set(ports)):
            raise ManagerError("管理面板和三个隧道的本地端口不能重复。")
        qq = str(raw.get("bot_qq", ""))
        if not re.fullmatch(r"[0-9]{5,12}", qq):
            raise ManagerError("bot_qq 必须是机器人 QQ 号。")
        return cls(host, user, ssh_port, key_path, ssh_path, remote_root, panel_port, normalized, qq)

    @property
    def urls(self) -> dict[str, str]:
        return {name: f"http://127.0.0.1:{ports['local']}/" for name, ports in self.forwards.items()}

    @property
    def panel_url(self) -> str:
        return f"http://127.0.0.1:{self.panel_port}/"

    def ssh_base(self) -> list[str]:
        return [self.ssh_path, "-T", "-i", str(self.key_path), "-p", str(self.port),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
                "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3"]

    def tunnel_command(self) -> list[str]:
        command = self.ssh_base() + ["-N", "-o", "ExitOnForwardFailure=yes"]
        for ports in self.forwards.values():
            command += ["-L", f"127.0.0.1:{ports['local']}:127.0.0.1:{ports['remote']}"]
        return command + [f"{self.user}@{self.host}"]

    def status_command(self) -> list[str]:
        return self.ssh_base() + [f"{self.user}@{self.host}",
                                  f"sudo -n python3 {self.remote_root}/bin/status.py"]


def preflight_ports(ports: list[int]) -> None:
    """Hold every socket until all ports pass. Never stop another process."""
    held = []
    try:
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            held.append(sock)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise ManagerError(f"本地端口 {port} 已被占用；请关闭已有管理窗口后重试。") from None
    finally:
        for sock in held:
            sock.close()


class ConsoleJob:
    """Put this manager in a Windows Job BEFORE starting any child process.

    The job handle is deliberately retained until process exit: closing it while
    this manager is alive would also terminate the manager. Windows closes it on
    console termination, killing tunnel AND in-flight status SSH processes.
    """

    def __init__(self) -> None:
        self.handle = None
        if sys.platform != "win32":
            return

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ManagerError("无法建立 SSH 进程清理机制，未启动隧道。")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(handle)
            raise ManagerError("无法设置 SSH 自动清理机制，未启动隧道。")
        if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
            kernel32.CloseHandle(handle)
            raise ManagerError("无法保护 SSH 子进程，请从桌面快捷方式重新启动管理面板。")
        self.handle = handle


def ssh_failure(stderr: str) -> str:
    """Turn potentially sensitive tool output into fixed user-facing messages."""
    lowered = stderr.lower()
    if "host key verification failed" in lowered or "remote host identification has changed" in lowered:
        return "SSH 主机指纹未确认或已变化；请重新核对服务器主机指纹。"
    if "permission denied" in lowered or "invalid format" in lowered or "unprotected private key" in lowered:
        return "SSH 私钥认证失败；请检查私钥权限、用户名和密钥绑定。"
    if "address already in use" in lowered or "cannot listen to port" in lowered:
        return "隧道端口被占用；请关闭已有管理窗口后重试。"
    if "timed out" in lowered or "connection refused" in lowered or "no route to host" in lowered:
        return "SSH 连接不可达；请检查服务器运行状态和网络。"
    return "SSH 连接已断开，请检查服务器和网络后重新启动管理面板。"


def _popen_options() -> dict[str, Any]:
    return {"close_fds": True, "creationflags": subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0}


class Tunnel:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self.error = ""

    def start(self) -> None:
        self.process = subprocess.Popen(self.config.tunnel_command(), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", **_popen_options())
        threading.Thread(target=self._read_errors, daemon=True, name="ssh-stderr").start()

    def _read_errors(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        try:
            for line in self.process.stderr:
                self.error = ssh_failure(line[:2000])
        except (OSError, ValueError):
            pass

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait_ready(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        pending = {item["local"] for item in self.config.forwards.values()}
        while time.monotonic() < deadline:
            if not self.alive:
                raise ManagerError(self.error or "SSH 隧道启动失败；请检查连接配置和主机指纹。")
            for port in tuple(pending):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        pending.remove(port)
                except OSError:
                    pass
            if not pending and self.alive:
                return
            time.sleep(0.1)
        raise ManagerError("SSH 隧道启动超时，请检查服务器连接。")

    def close(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            if self.process.stderr:
                self.process.stderr.close()


def redact(text: Any) -> str:
    value = str(text)
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[私钥已隐藏]", value, flags=re.S)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[API 密钥已隐藏]", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_.~-]+", r"\1[已隐藏]", value)
    value = re.sub(r'''(?i)((?:api[_-]?key|access[_-]?token|authorization|password|passwd|secret|token)["']?\s*[:=]\s*["']?)[^\s,"'}]+''', r"\1[已隐藏]", value)
    return value[:2000]


def clean_status(raw: Any) -> dict[str, Any]:
    """Only fields used by the panel may cross the local HTTP API boundary."""
    if not isinstance(raw, dict) or not isinstance(raw.get("server"), dict):
        raise ValueError("invalid status schema")
    server = raw["server"]
    result: dict[str, Any] = {"server": {}, "services": {}, "bot": {}, "logs": {}}
    for key in ("hostname", "os", "cpu_count", "uptime_seconds", "load", "memory", "disk"):
        value = server.get(key)
        if isinstance(value, dict):
            result["server"][key] = {name: number for name, number in value.items()
                                      if name in ("total", "available", "used", "free") and type(number) in (int, float)}
        elif isinstance(value, list):
            result["server"][key] = [number for number in value[:3] if type(number) in (int, float)]
        elif type(value) in (int, float, bool) or value is None:
            result["server"][key] = value
        else:
            result["server"][key] = redact(value)
    services = raw.get("services", {})
    for name in SERVICE_NAMES:
        service = services.get(name, {}) if isinstance(services, dict) else {}
        if name == "novnc" and not service and isinstance(services, dict):
            # noVNC is supervised inside the SnowLuma container, not a separate
            # deployment. An explicit helper result still takes precedence.
            service = services.get("snowluma", {})
        result["services"][name] = {key: redact(service.get(key, "未知")) for key in
                                     ("state", "health", "started_at", "image")} if isinstance(service, dict) else {}
    bot = raw.get("bot", {})
    if isinstance(bot, dict):
        result["bot"] = {key: redact(bot.get(key, "")) for key in ("qq", "admin_qq", "model")}
        for key in ("online", "onebot_connected"):
            result["bot"][key] = bot.get(key) if type(bot.get(key)) is bool else None
    logs = raw.get("logs", {})
    for name in ("astrbot", "snowluma"):
        lines = logs.get(name, []) if isinstance(logs, dict) else []
        result["logs"][name] = [redact(line) for line in lines[-160:]] if isinstance(lines, list) else []
    result["sampled_at"] = redact(raw.get("sampled_at", ""))
    return result


class StatusCache:
    def __init__(self, config: Config, tunnel: Tunnel) -> None:
        self.config, self.tunnel = config, tunnel
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._error = "正在读取服务器状态……"
        self._last_success: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="server-status")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(5)

    def refresh(self) -> None:
        if not self.tunnel.alive:
            with self._lock:
                self._error = self.tunnel.error or "SSH 已断开；关闭本窗口后重新启动即可重连。"
            return
        try:
            completed = subprocess.run(self.config.status_command(), stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=12, **_popen_options())
            if completed.returncode:
                raise ManagerError(ssh_failure(completed.stderr))
            if len(completed.stdout) > 1_000_000:
                raise ValueError("status response too large")
            data = clean_status(json.loads(completed.stdout))
            with self._lock:
                self._data = data
                self._error = ""
                self._last_success = datetime.now(timezone.utc).isoformat()
        except subprocess.TimeoutExpired:
            with self._lock:
                self._error = "读取服务器状态超时，稍后自动重试。"
        except ManagerError as error:
            with self._lock:
                self._error = str(error)
        except (OSError, ValueError, TypeError):
            with self._lock:
                self._error = "服务器状态暂不可用；请检查远端状态服务。"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"connected": self.tunnel.alive, "error": self._error,
                    "stale": bool(self._error) or not self.tunnel.alive,
                    "last_success": self._last_success, "status": self._data,
                    "links": self.config.urls, "host": self.config.host, "bot_qq": self.config.bot_qq}

    def close(self) -> None:
        self._stop.set()
        # Closing the persistent tunnel first causes an active status read to
        # fail promptly; the Windows Job also owns that short-lived SSH child.
        if self._thread:
            self._thread.join(timeout=0.3)


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, config: Config, cache: StatusCache, html_path: Path) -> None:
        self.config, self.cache = config, cache
        self.html = html_path.read_bytes()
        super().__init__(("127.0.0.1", config.panel_port), PanelHandler)


class PanelHandler(BaseHTTPRequestHandler):
    server: PanelServer

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-src http://127.0.0.1:*; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # Block DNS rebinding and cross-origin browser requests to this local API.
        if self.headers.get("Host") not in (f"127.0.0.1:{self.server.config.panel_port}",
                                             f"localhost:{self.server.config.panel_port}"):
            self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            return
        origin = self.headers.get("Origin")
        if origin and origin != self.server.config.panel_url.rstrip("/"):
            self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            return
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, self.server.html, "text/html; charset=utf-8")
        elif path == "/api/status":
            body = json.dumps(self.server.cache.snapshot(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="小鲸鱼云端机器人本地管理面板")
    parser.add_argument("--config", type=Path, default=ROOT / "local.json")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    tunnel = cache = server = None
    try:
        config = Config.load(args.config)
        preflight_ports([config.panel_port, *(item["local"] for item in config.forwards.values())])
        job = ConsoleJob()  # Keep alive; create BEFORE any subprocess.
        tunnel = Tunnel(config)
        cache = StatusCache(config, tunnel)
        server = PanelServer(config, cache, ROOT / "panel.html")
        print("正在建立小鲸鱼 SSH 隧道……", flush=True)
        tunnel.start()
        tunnel.wait_ready()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="local-panel")
        server_thread.start()
        with socket.create_connection(("127.0.0.1", config.panel_port), timeout=2):
            pass
        cache.start()
        print(f"管理面板：{config.panel_url}\n保持此窗口打开；关闭窗口或按 Ctrl+C，将自动断开 SSH。", flush=True)
        if not args.no_browser:
            try:
                webbrowser.open(config.panel_url)
            except (webbrowser.Error, OSError):
                print("未能自动打开浏览器，请手动访问上方管理面板地址。", flush=True)
        announced_disconnect = False
        while server_thread.is_alive():
            if not tunnel.alive and not announced_disconnect:
                print("SSH 已断开。管理面板会显示断开状态；关闭本窗口并重新启动即可重连。", flush=True)
                announced_disconnect = True
            time.sleep(0.3)
        return 0
    except KeyboardInterrupt:
        print("\n正在关闭管理面板与 SSH 隧道……", flush=True)
        return 0
    except ManagerError as error:
        print(f"[未启动] {error}", file=sys.stderr, flush=True)
        return 1
    except (OSError, ValueError):
        print("[未启动] 无法启动管理面板，请检查本地配置、端口和文件。", file=sys.stderr, flush=True)
        return 1
    finally:
        if cache:
            cache.close()
        if tunnel:
            tunnel.close()
        if server:
            # shutdown() must only be called after serve_forever() has started.
            if "server_thread" in locals() and server_thread.is_alive():
                server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
