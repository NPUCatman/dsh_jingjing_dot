"""Deployment regressions use temporary files and never SSH or launch Docker."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cloud import deploy, initialize


BOT = "123456789"
ADMIN = "987654321"


def local_settings():
    return {"host": "192.0.2.10", "user": "ubuntu", "key_path": "private-key.pem",
            "remote_root": "/opt/jingjing", "bot_qq": BOT, "admin_qq": ADMIN,
            "model": "deepseek-flash", "api_base": "https://api.deepseek.com/v1"}


def fake_credentials():
    return {"astrbot_password": "Astr1!fake", "snowluma_password": "Snow1!fake",
            "novnc_password": "Vnc1fake", "onebot_token": "onebot-fake-token",
            "deepseek_api_key": "sk-fake-key-never-use"}


class PrepareTests(unittest.TestCase):
    def arguments(self):
        return SimpleNamespace(host="192.0.2.10", user="ubuntu", key="private-key.pem",
                               bot_qq=BOT, admin_qq=ADMIN)

    def test_password_policy_holds_even_if_random_suffix_has_no_digits(self):
        existing = SimpleNamespace(model="deepseek-flash", base_url="https://api.deepseek.com", api_key="sk-private-fake")
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(deploy, "CLOUD", Path(temporary)), \
             patch("jingjing.config.load_config", return_value=existing), \
             patch.object(deploy, "private_json") as write, \
             patch.object(deploy.secrets, "token_urlsafe", return_value="onlylettersinthegeneratedsuffix"), \
             patch.object(deploy.secrets, "choice", return_value="x"), redirect_stdout(io.StringIO()):
            deploy.prepare(self.arguments())
        credentials = write.call_args_list[1].args[1]
        password = credentials["astrbot_password"]
        self.assertGreaterEqual(len(password), 8)
        for requirement in (r"[a-z]", r"[A-Z]", r"[0-9]"):
            self.assertRegex(password, requirement)
        self.assertEqual(8, len(credentials["novnc_password"]))
        local = write.call_args_list[0].args[1]
        self.assertEqual("https://api.deepseek.com/v1", local["api_base"])
        self.assertNotIn(existing.api_key, json.dumps(local))

    def test_existing_connection_or_credentials_file_prevents_overwrite(self):
        for filename in ("local.json", "credentials.local.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / filename).write_text("preserve", encoding="utf-8")
                with patch.object(deploy, "CLOUD", root), \
                     patch("jingjing.config.load_config", return_value=SimpleNamespace()), \
                     patch.object(deploy, "private_json") as write:
                    with self.assertRaises(RuntimeError):
                        deploy.prepare(self.arguments())
                write.assert_not_called()
                self.assertEqual("preserve", (root / filename).read_text())


class BundleTests(unittest.TestCase):
    def prepare_files(self, root, credentials=None):
        (root / "local.json").write_text(json.dumps(local_settings()), encoding="utf-8")
        (root / "credentials.local.json").write_text(json.dumps(credentials or fake_credentials()), encoding="utf-8")
        for name in ("compose.yaml", "initialize.py", "server_status.py"):
            (root / name).write_text("fixture for " + name, encoding="utf-8")

    def test_archive_keeps_secrets_private_and_dotenv_values_literal(self):
        credentials = fake_credentials()
        credentials["deepseek_api_key"] = "sk-fake$literal#value"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare_files(root, credentials)
            with patch.object(deploy, "CLOUD", root):
                payload = deploy.bundle()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            self.assertEqual({"compose.yaml", "bin/initialize.py", "bin/status.py", "credentials.json", "bot.json", ".env"},
                             {member.name for member in members})
            self.assertTrue(all(member.mode == 0o600 and member.isfile() for member in members))
            environment = archive.extractfile(".env").read().decode()
            self.assertIn("DEEPSEEK_API_KEY='sk-fake$literal#value'\n", environment)
            self.assertEqual(credentials, json.load(archive.extractfile("credentials.json")))
            metadata = json.load(archive.extractfile("bot.json"))
            self.assertEqual(BOT, metadata["bot_qq"])
            self.assertNotIn("password", json.dumps(metadata))
            self.assertNotIn(credentials["deepseek_api_key"], json.dumps(metadata))

    def test_secret_cannot_inject_dotenv_entries(self):
        for injected in ("fake\nINJECTED=true", "fake\rINJECTED=true", "fake'quoted"):
            credentials = fake_credentials()
            credentials["deepseek_api_key"] = injected
            with self.subTest(injected=injected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.prepare_files(root, credentials)
                with patch.object(deploy, "CLOUD", root), self.assertRaises(RuntimeError) as error:
                    deploy.bundle()
                self.assertNotIn(injected, str(error.exception))

    def test_upload_sends_archive_only_as_stdin_and_guards_existing_stack(self):
        sentinel = b"private-archive-bytes"
        with patch.object(deploy, "read_local", return_value=local_settings()), \
             patch.object(deploy, "bundle", return_value=sentinel), patch.object(deploy, "remote") as remote:
            deploy.upload()
        calls = remote.call_args_list
        self.assertIn("test ! -e /opt/jingjing/compose.yaml", calls[0].args[0])
        self.assertEqual(sentinel, calls[1].args[1])
        self.assertIn("tar -xzf -", calls[1].args[0])
        self.assertIn("initialize.py", calls[2].args[0])
        for call in calls:
            self.assertNotIn(sentinel.decode(), call.args[0])


class InitializeTests(unittest.TestCase):
    def test_bot_configuration_connects_same_token_and_limits_to_admin_private_chat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "credentials.json").write_text(json.dumps(fake_credentials()), encoding="utf-8")
            (root / "bot.json").write_text(json.dumps(local_settings()), encoding="utf-8")
            with patch.object(initialize, "ROOT", root), patch.object(initialize.os, "umask"), redirect_stdout(io.StringIO()):
                initialize.main()
            config = json.loads((root / "astrbot/data/cmd_config.json").read_text())
            onebot = json.loads((root / f"snowluma/data/config/onebot_{BOT}.json").read_text())
        self.assertEqual([ADMIN], config["admins_id"])
        settings = config["platform_settings"]
        self.assertTrue(settings["enable_id_white_list"])
        self.assertEqual([f"qq_snowluma:FriendMessage:{ADMIN}"], settings["id_whitelist"])
        self.assertFalse(settings["wl_ignore_admin_on_group"])
        self.assertFalse(settings["wl_ignore_admin_on_friend"])
        self.assertEqual(["$DEEPSEEK_API_KEY"], config["provider_sources"][0]["key"])
        self.assertNotIn(fake_credentials()["deepseek_api_key"], json.dumps(config))
        self.assertEqual(config["platform"][0]["ws_reverse_token"], onebot["networks"]["wsClients"][0]["accessToken"])
        self.assertEqual("ws://astrbot:6199/ws", onebot["networks"]["wsClients"][0]["url"])
        self.assertEqual([], onebot["networks"]["httpServers"])
        self.assertEqual([], onebot["networks"]["wsServers"])
        self.assertFalse(onebot["networks"]["wsClients"][0]["reportSelfMessage"])

    def test_initialization_preserves_existing_user_application_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "credentials.json").write_text(json.dumps(fake_credentials()), encoding="utf-8")
            (root / "bot.json").write_text(json.dumps(local_settings()), encoding="utf-8")
            paths = [root / "astrbot/data/cmd_config.json", root / f"snowluma/data/config/onebot_{BOT}.json"]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"user_data":"preserve"}', encoding="utf-8")
            with patch.object(initialize, "ROOT", root), patch.object(initialize.os, "umask"), redirect_stdout(io.StringIO()):
                initialize.main()
            self.assertTrue(all(path.read_text() == '{"user_data":"preserve"}' for path in paths))


class ComposeTests(unittest.TestCase):
    def test_only_loopback_management_ports_are_published(self):
        text = (Path(deploy.__file__).parent / "compose.yaml").read_text(encoding="utf-8")
        mappings = re.findall(r'^\s+-\s+["\']?(\d+\.\d+\.\d+\.\d+:\d+:\d+)["\']?\s*$', text, re.M)
        self.assertEqual({"127.0.0.1:6185:6185", "127.0.0.1:5099:5099", "127.0.0.1:6081:6081"}, set(mappings))
        self.assertNotRegex(text, r"(?m)^\s+-\s+[\"']?\d+:\d+[\"']?\s*$")
        self.assertNotIn("6199:6199", text)

    def test_images_are_digest_pinned_and_docker_socket_is_not_mounted(self):
        text = (Path(deploy.__file__).parent / "compose.yaml").read_text(encoding="utf-8")
        images = re.findall(r"(?m)^\s+image:\s+(\S+)$", text)
        self.assertEqual(2, len(images))
        self.assertTrue(all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in images))
        self.assertNotIn("/var/run/docker.sock", text)
        self.assertNotIn("privileged: true", text)


if __name__ == "__main__":
    unittest.main()
