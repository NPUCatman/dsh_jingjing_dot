"""Run with the project's virtualenv: python -m jingjing <command>."""

import argparse
from dataclasses import replace
from getpass import getpass
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from dotenv import set_key

from .config import ConfigError, load_config, validate_api_key
from .deepseek import DeepSeekClient, DeepSeekError
from .engine import BotEngine, Event


ROOT = Path(__file__).resolve().parent.parent


def configure():
    """Prompt locally. Credentials are never echoed or sent anywhere here."""
    for source, destination in ((".env.example", ".env"), ("config.example.json", "config.json")):
        if not (ROOT / destination).exists():
            shutil.copyfile(ROOT / source, ROOT / destination)
    config = load_config(ROOT, require_api_key=False)
    print("本地配置向导。API Key 隐藏输入；直接回车保留已有设置。")
    key = getpass("DeepSeek API Key（仅保存在本地 .env）：").strip()
    if key:
        validate_api_key(key)

    def names(label, current):
        print(f"当前{label}：{'、'.join(current) if current else '空'}")
        raw = input(f"{label}完整名称（多个用逗号分隔；回车保留，输入 - 清空）：").strip()
        if not raw:
            return list(current)
        if raw == "-":
            return []
        result = list(dict.fromkeys(name.strip() for name in re.split(r"[,，]", raw) if name.strip()))
        if len(result) > 1000 or any(len(name) > 200 for name in result):
            raise ConfigError("每份名单最多包含 1000 个名称，每个名称最多 200 字。原配置尚未修改。")
        return result

    data = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    data["friends"] = names("好友备注", config.friends)
    data["groups"] = names("群聊名称", config.groups)
    if set(data["friends"]) & set(data["groups"]):
        raise ConfigError("好友与群名称不能重复，请给好友设置唯一备注后重新配置。")
    # Validate every interactive change before writing either local file.
    # Replace the JSON atomically, so interruption cannot truncate an existing
    # valid configuration. python-dotenv also replaces .env atomically.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=ROOT, prefix=".config-", suffix=".tmp", delete=False) as staged:
            temporary = Path(staged.name)
            staged.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            staged.flush()
            os.fsync(staged.fileno())
        if key:
            set_key(str(ROOT / ".env"), "DEEPSEEK_API_KEY", key, quote_mode="always")
        os.replace(temporary, ROOT / "config.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("配置已保存。没有调用 API，也没有操作微信。接下来运行 check 和 api-test。")
    return 0


def check():
    config = load_config(ROOT, require_api_key=False)
    print(f"Python：{sys.version.split()[0]}，{64 if sys.maxsize > 2**32 else 32} 位")
    for package in ("httpx", "python-dotenv", "pywechat127", "pywinauto", "pywin32"):
        try:
            print(f"依赖 {package}：{version(package)}")
        except PackageNotFoundError:
            print(f"依赖 {package}：未安装（运行 setup.ps1）")
    try:
        validate_api_key(config.api_key)
        print("API Key：已填写，尚需 api-test 验证")
    except ConfigError:
        print("API Key：待填写（运行 configure）")
    print(f"模型：{config.model}")
    print(f"聊天名单：{len(config.friends)} 位好友、{len(config.groups)} 个群")
    print("配置检查完成。未调用 API、未操作微信。")
    return 0


def api_test():
    config = load_config(ROOT)
    print("正在向 DeepSeek 发起一次短文本请求……")
    with DeepSeekClient(config) as client:
        reply = client.reply([{"role": "user", "content": "请只回复：小鲸鱼连接成功"}])
    print(reply)
    print("API 测试完成。未操作微信。")
    return 0


def chat():
    config = replace(load_config(ROOT), friends=("终端测试",), cooldown_seconds=0)
    print("终端聊天测试，未连接微信。输入 /退出 结束，/清空 清空记忆。")
    with DeepSeekClient(config) as client:
        engine = BotEngine(config, client)
        while True:
            text = input("你：").strip()
            if text in ("/退出", "/exit"):
                return 0
            reply = engine.handle(Event(chat="终端测试", sender="你", text=text))
            if reply:
                print("🐳：" + reply)


class InstanceLock:
    """One listener per checkout, including dry-run listeners."""
    def __enter__(self):
        import msvcrt
        (ROOT / "data").mkdir(exist_ok=True)
        self.file = (ROOT / "data" / "listener.lock").open("a+b")
        try:
            # Reading byte zero fails on Windows when another listener owns
            # its lock. Inspect size without touching the locked byte instead.
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise ConfigError("本项目已经有一个微信监听进程，请先按 Ctrl+C 关闭原进程。") from None
        return self

    def __exit__(self, *args):
        import msvcrt
        try:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.file.close()


def run(dry_run=False):
    from .wechat import WeChatAdapter
    if sys.platform != "win32":
        raise ConfigError("当前微信接入需要 Windows 10/11 桌面。")
    config = load_config(ROOT)
    with InstanceLock(), DeepSeekClient(config) as client:
        adapter = WeChatAdapter(config)
        adapter.attach()
        engine = BotEngine(config, client)
        print(f"已连接 {len(adapter.chats)} 个独立聊天窗口。启动前的历史消息不会触发回复。")
        print("预览模式：会调用 DeepSeek，仅在本终端显示结果。" if dry_run else "已启用微信回复。请保持桌面解锁、不要在这些窗口手动输入。")
        print("Ctrl+C 停止。群里可发 /鲸鲸 /帮助。")
        while True:
            for window, event in adapter.poll():
                reply = engine.handle(event)
                if reply:
                    if dry_run:
                        print(f"[预览] {window.name}\n{reply}")
                    else:
                        adapter.send(window, reply)
                        print(f"[已提交发送] {window.name}")
            time.sleep(0.7)


def main():
    parser = argparse.ArgumentParser(description="小鲸鱼：DeepSeek + Windows 微信文字机器人")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("configure", "本地填写 Key 和聊天名单"),
        ("check", "检查环境与配置，不连接微信或 API"),
        ("api-test", "调用一次 DeepSeek，不连接微信"),
        ("chat", "在终端测试 DeepSeek 多轮聊天"),
        ("probe", "只检测微信控件，不发送消息"),
    ):
        sub.add_parser(command, help=help_text)
    listener = sub.add_parser("run", help="监听新文字消息并回复")
    listener.add_argument("--dry-run", action="store_true", help="调用 API 生成结果，只显示在终端")
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run(args.dry_run)
        if args.command == "probe":
            from .wechat import probe
            return 0 if probe(load_config(ROOT, require_api_key=False)) else 2
        return {"configure": configure, "check": check, "api-test": api_test, "chat": chat}[args.command]()
    except (KeyboardInterrupt, EOFError):
        print("\n已停止。")
        return 0
    except (ConfigError, DeepSeekError) as exc:
        print(f"[待处理] {exc}")
        return 2
    except Exception as exc:
        from .wechat import WeChatError
        if isinstance(exc, WeChatError):
            print(f"[已停止] {exc}")
        else:
            # Avoid raw third-party exception strings containing private data.
            print(f"[已停止] {type(exc).__name__}。请检查窗口、登录状态及依赖；不会自动重发。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
