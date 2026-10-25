"""Offline checks for the local dashboard, safe config, and SSH lifecycle."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cloud import manager


class ManagerTests(unittest.TestCase):
    def make_config(self, root: Path, **overrides) -> manager.Config:
        key = root / "identity.pem"
        key.write_text("test placeholder, never a real key", encoding="utf-8")
        raw = {
            "host": "203.0.113.10", "user": "ubuntu", "port": 22,
            "key_path": str(key.resolve()), "ssh_path": "systemssh",
            "remote_root": "/opt/jingjing", "panel_port": 18780,
            "forwards": {"astrbot": {"local": 16185, "remote": 6185},
                         "snowluma": {"local": 15099, "remote": 5099},
                         "novnc": {"local": 16081, "remote": 6081}},
            "bot_qq": "100000001",
        }
        raw.update(overrides)
        config_path = root / "local.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        with patch.object(manager.shutil, "which", return_value="C:/Windows/System32/OpenSSH/ssh.exe"):
            return manager.Config.load(config_path)

    def test_config_builds_only_loopback_forwards_and_readonly_status_command(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            command = config.tunnel_command()
            self.assertEqual(3, command.count("-L"))
            self.assertIn("127.0.0.1:16185:127.0.0.1:6185", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("ExitOnForwardFailure=yes", command)
            self.assertIn("BatchMode=yes", command)
            self.assertEqual("ubuntu@203.0.113.10", command[-1])
            self.assertEqual("sudo -n python3 /opt/jingjing/bin/status.py", config.status_command()[-1])
            self.assertEqual("http://127.0.0.1:18780/", config.panel_url)

    def test_config_rejects_commands_and_duplicate_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            for overrides in ({"host": "-oProxyCommand=bad"}, {"user": "ubuntu; bad"},
                              {"remote_root": "/opt/x;echo bad"}, {"panel_port": 16185},
                              {"panel_port": True}, {"bot_qq": "<script>"}):
                with self.subTest(overrides=overrides), self.assertRaises(manager.ManagerError):
                    self.make_config(Path(directory), **overrides)

    def test_preflight_does_not_kill_or_claim_an_occupied_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            port = occupied.getsockname()[1]
            with self.assertRaisesRegex(manager.ManagerError, str(port)):
                manager.preflight_ports([port])
            self.assertEqual(port, occupied.getsockname()[1])
        manager.preflight_ports([port])

    def test_status_is_whitelisted_and_credentials_are_redacted(self):
        raw = {"server": {"hostname": "<img src=x onerror=alert(1)>", "os": "Ubuntu",
                          "memory": {"total": 99, "available": 22, "token": "hidden"},
                          "credentials": "must not cross API"},
               "services": {"astrbot": {"state": "running", "secret": "hidden"}},
               "bot": {"qq": "100000001", "model": "deepseek-chat", "online": True,
                       "admin_qq": "12345678", "onebot_connected": "true", "password": "hidden"},
               "logs": {"astrbot": ["api_key=sk-examplekey123456", "Authorization: Bearer token12345", "ok"],
                        "snowluma": ["password: letmein123"]}, "password": "hidden"}
        result = manager.clean_status(raw)
        encoded = json.dumps(result)
        for secret in ("must not cross API", "examplekey123456", "token12345", "letmein123", "hidden"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(raw["server"]["hostname"], result["server"]["hostname"])
        self.assertTrue(result["bot"]["online"])
        self.assertEqual("12345678", result["bot"]["admin_qq"])
        self.assertIsNone(result["bot"]["onebot_connected"])
        html = (manager.ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("document.write", html)
        self.assertNotIn("https://", html)

    def test_novnc_follows_snowluma_container_unless_helper_has_explicit_state(self):
        raw = {"server": {}, "services": {"snowluma": {"state": "running", "health": "healthy"}}}
        self.assertEqual("running", manager.clean_status(raw)["services"]["novnc"]["state"])
        raw["services"]["novnc"] = {"state": "stopped", "health": "unhealthy"}
        self.assertEqual("stopped", manager.clean_status(raw)["services"]["novnc"]["state"])

    def test_status_timeout_retains_last_sample_but_marks_it_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            tunnel = Mock(alive=True, error="")
            cache = manager.StatusCache(config, tunnel)
            completed = subprocess.CompletedProcess([], 0, json.dumps({"server": {"hostname": "cloud"}}), "")
            with patch.object(manager.subprocess, "run", return_value=completed) as runner:
                cache.refresh()
            self.assertFalse(cache.snapshot()["stale"])
            self.assertEqual(12, runner.call_args.kwargs["timeout"])
            self.assertTrue(runner.call_args.kwargs["close_fds"])
            with patch.object(manager.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 12)):
                cache.refresh()
            self.assertTrue(cache.snapshot()["stale"])
            self.assertEqual("cloud", cache.snapshot()["status"]["server"]["hostname"])
            tunnel.alive = False
            with patch.object(manager.subprocess, "run", side_effect=AssertionError("do not reconnect")):
                cache.refresh()
            self.assertFalse(cache.snapshot()["connected"])

    def test_tunnel_cleanup_only_terminates_its_own_process(self):
        with tempfile.TemporaryDirectory() as directory:
            tunnel = manager.Tunnel(self.make_config(Path(directory)))
            process = Mock()
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired([], 3), 0]
            tunnel.process = process
            tunnel.close()
            process.terminate.assert_called_once_with()
            process.kill.assert_called_once_with()
            self.assertEqual(2, process.wait.call_count)
            process.stderr.close.assert_called_once_with()

    def test_dead_tunnel_never_passes_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            tunnel = manager.Tunnel(self.make_config(Path(directory)))
            tunnel.error = "SSH 私钥认证失败。"
            with self.assertRaisesRegex(manager.ManagerError, "私钥认证失败"):
                tunnel.wait_ready(timeout=0.1)

    def test_ssh_errors_use_fixed_messages_and_never_echo_raw_output(self):
        secret = "sk-do-not-print-123456789"
        for text in (f"Permission denied {secret}", f"Host key verification failed {secret}", secret):
            self.assertNotIn(secret, manager.ssh_failure(text))

    def test_handler_rejects_rebinding_host_and_never_exposes_config_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            handler = manager.PanelHandler.__new__(manager.PanelHandler)
            handler.server = Mock(config=config)
            handler._send = Mock()
            handler.headers = {"Host": "untrusted.example:18780"}
            handler.path = "/api/status"
            handler.do_GET()
            self.assertEqual(403, handler._send.call_args.args[0])
            handler.headers = {"Host": "127.0.0.1:18780"}
            handler.path = "/local.json"
            handler.do_GET()
            self.assertEqual(404, handler._send.call_args.args[0])
            handler.headers["Origin"] = "https://untrusted.example"
            handler.path = "/api/status"
            handler.do_GET()
            self.assertEqual(403, handler._send.call_args.args[0])

    @unittest.skipUnless(sys.platform == "win32", "Windows console-close lifecycle")
    def test_forced_manager_exit_kills_child_through_windows_job(self):
        # This uses only a sleeping Python child: no SSH, UI, key, or server.
        # Force-terminating its parent reproduces console-close handle cleanup.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            source = "\n".join([
                "from cloud.manager import ConsoleJob",
                "import subprocess, sys, time",
                "from pathlib import Path",
                "job = ConsoleJob()",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)",
                f"Path({str(pid_file)!r}).write_text(str(child.pid))",
                "time.sleep(60)",
            ])
            parent = subprocess.Popen([sys.executable, "-c", source], cwd=manager.ROOT.parent,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                      creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
            handle = None
            try:
                deadline = time.monotonic() + 6
                while not pid_file.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not pid_file.exists():
                    parent.kill()
                    _, stderr = parent.communicate(timeout=3)
                    self.fail(f"Job child did not start: {stderr.decode('utf-8', 'replace')}")
                child_pid = int(pid_file.read_text())
                handle = kernel32.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE
                self.assertTrue(handle, "could not inspect dummy child lifecycle")
                parent.kill()
                parent.wait(timeout=3)
                self.assertEqual(0, kernel32.WaitForSingleObject(handle, 5000),
                                 "closing the manager must terminate its owned child")
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=3)
                if parent.stderr:
                    parent.stderr.close()
                if handle:
                    kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
