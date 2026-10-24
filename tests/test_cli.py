"""CLI regressions for local-state preservation, imports and listener locks."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jingjing import __main__ as cli
from jingjing.config import ConfigError


class CLITests(unittest.TestCase):
    def test_import_does_not_load_native_wechat_or_open_http_client(self):
        # A fresh interpreter catches import-time side effects that a reload in
        # the test process could hide. Neither a key nor an open desktop is needed.
        source = """
import sys
import httpx
def forbidden(*args, **kwargs):
    raise AssertionError('an HTTP client must not open during import')
httpx.Client = forbidden
import jingjing.__main__
assert 'jingjing.wechat' not in sys.modules
assert 'pyweixin' not in sys.modules
assert 'pywinauto' not in sys.modules
assert 'pyautogui' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", source], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)

    def test_invalid_configure_input_keeps_existing_files_and_hides_new_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "system.txt").write_text("你是小鲸鱼", encoding="utf-8")
            original_json = json.dumps({"friends": ["原好友"], "groups": [], "max_history_turns": 5}, ensure_ascii=False)
            original_env = "DEEPSEEK_API_KEY=\nDEEPSEEK_MODEL=deepseek-flash\n"
            (root / "config.json").write_text(original_json, encoding="utf-8")
            (root / ".env").write_text(original_env, encoding="utf-8")
            key = "sk-" + "b2" * 16
            for names in (("过" * 201,), ("同名", "同名")):
                with self.subTest(names=names), patch.object(cli, "ROOT", root), patch.object(cli, "getpass", return_value=key), patch("builtins.input", side_effect=names):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), self.assertRaises(ConfigError):
                        cli.configure()
                    self.assertEqual(original_json, (root / "config.json").read_text(encoding="utf-8"))
                    self.assertEqual(original_env, (root / ".env").read_text(encoding="utf-8"))
                    self.assertNotIn(key, output.getvalue())
                    self.assertEqual([], list(root.glob(".config-*.tmp")))

    @unittest.skipUnless(sys.platform == "win32", "Windows listener lock")
    def test_listener_lock_excludes_second_instance_and_releases_after_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, "ROOT", Path(directory)):
            with self.assertRaisesRegex(RuntimeError, "test interruption"):
                with cli.InstanceLock():
                    with self.assertRaisesRegex(ConfigError, "已经有一个"):
                        with cli.InstanceLock():
                            self.fail("a duplicate listener acquired the same lock")
                    raise RuntimeError("test interruption")
            with cli.InstanceLock():
                self.assertTrue((Path(directory) / "data" / "listener.lock").is_file())


if __name__ == "__main__":
    unittest.main()
