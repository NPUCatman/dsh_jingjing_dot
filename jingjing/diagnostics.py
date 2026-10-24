"""Read-only native window diagnostics; never read titles or message contents."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NativeWindow:
    handle: int
    pid: int
    class_name: str
    visible: bool
    minimized: bool


@dataclass(frozen=True)
class NativeSnapshot:
    weixin_pids: tuple[int, ...] = ()
    wechat_pids: tuple[int, ...] = ()
    windows: tuple[NativeWindow, ...] = ()
    errors: tuple[str, ...] = ()


def _error_kind(exc: Exception) -> str:
    """Return an allowlisted category, never exception text or arguments."""
    name = type(exc).__name__
    if isinstance(exc, PermissionError) or name == "AccessDenied":
        return "权限不足"
    if isinstance(exc, ImportError):
        return "依赖缺失"
    if isinstance(exc, OSError) or name in {"error", "com_error"}:
        return "系统接口错误"
    return "检查异常"


def _related_class(class_name: str) -> bool:
    lowered = class_name.casefold()
    if any(marker in lowered for marker in ("tray", "ime", "helper", "tooltip", "shadow", "notify")):
        return False
    return (
        (class_name.startswith("Qt") and "QWindow" in class_name)
        or (class_name.startswith("mmui::") and class_name.endswith("Window"))
        or class_name in {"WeChatMainWndForPC", "LoginWndForPC"}
    )


def native_snapshot() -> NativeSnapshot:
    """Inspect main executable names and native window metadata without UI actions.

    Dependency and permission failures are retained as safe categories so an
    incomplete inspection cannot be mistaken for evidence that Weixin is absent.
    Importing this module does not import Windows libraries or inspect the client.
    """
    errors: list[str] = []
    weixin_pids: set[int] = set()
    wechat_pids: set[int] = set()
    windows: list[NativeWindow] = []

    def error(message: str) -> None:
        if message not in errors:
            errors.append(message)

    try:
        import psutil
    except Exception as exc:
        return NativeSnapshot(errors=(f"无法加载 psutil（{_error_kind(exc)}）",))

    try:
        # These are the only process attributes requested. Never request paths,
        # command lines, account names, environment variables, or open files.
        for process in psutil.process_iter(attrs=["pid", "name"], ad_value=None):
            try:
                info = process.info
                pid, name = info.get("pid"), info.get("name")
                if name is None:
                    error("部分进程名称不可读取")
                    continue
                if not isinstance(pid, int) or not isinstance(name, str):
                    error("部分进程元数据不可读取")
                    continue
                if name.casefold() == "weixin.exe":
                    weixin_pids.add(pid)
                elif name.casefold() == "wechat.exe":
                    wechat_pids.add(pid)
            except Exception as exc:
                if type(exc).__name__ != "NoSuchProcess":
                    error(f"部分进程不可读取（{_error_kind(exc)}）")
    except Exception as exc:
        error(f"进程枚举未完整完成（{_error_kind(exc)}）")

    pids = weixin_pids | wechat_pids
    if pids:
        try:
            import win32gui
            import win32process
        except Exception as exc:
            error(f"无法加载 Win32 窗口接口（{_error_kind(exc)}）")
        else:
            def inspect_window(handle, _):
                try:
                    _, pid = win32process.GetWindowThreadProcessId(handle)
                    if pid not in pids:
                        return True
                    class_name = win32gui.GetClassName(handle)
                    if _related_class(class_name):
                        windows.append(NativeWindow(
                            handle=int(handle),
                            pid=pid,
                            class_name=class_name,
                            visible=bool(win32gui.IsWindowVisible(handle)),
                            minimized=bool(win32gui.IsIconic(handle)),
                        ))
                except Exception as exc:
                    error(f"部分窗口状态不可读取（{_error_kind(exc)}）")
                return True

            try:
                win32gui.EnumWindows(inspect_window, None)
            except Exception as exc:
                error(f"窗口枚举未完整完成（{_error_kind(exc)}）")

    return NativeSnapshot(
        weixin_pids=tuple(sorted(weixin_pids)),
        wechat_pids=tuple(sorted(wechat_pids)),
        windows=tuple(sorted(windows, key=lambda window: (window.pid, window.handle))),
        errors=tuple(errors),
    )


def missing_window_message(snapshot: NativeSnapshot) -> str:
    """Explain missing supported UIA windows without inferring login status."""
    if not snapshot.weixin_pids:
        if snapshot.wechat_pids:
            message = (
                "已发现 WeChat.exe（通常是旧版微信 3.x），尚未发现 Weixin.exe。"
                "本项目使用微信 4.x 的控件结构；请确认已打开微信 4.x 后再运行 probe。"
            )
        elif snapshot.errors:
            message = "本次进程检查未完整完成，无法判断微信主程序是否正在运行。"
        else:
            message = "未发现 Weixin.exe 或 WeChat.exe 主程序进程。请先打开微信 4.x，再运行 probe。"
    else:
        windows = [window for window in snapshot.windows if window.pid in snapshot.weixin_pids]
        if windows and all(window.minimized for window in windows):
            message = (
                "已发现 Weixin.exe 进程，但相关窗口均已最小化。"
                "请手动恢复微信和独立聊天窗口后再运行 probe。"
            )
        elif not any(window.visible for window in windows):
            message = (
                "已发现 Weixin.exe 进程，但没有找到可见的相关原生窗口。"
                "请从任务栏或托盘手动打开微信窗口后再运行 probe。"
            )
        else:
            classes = "、".join(sorted({window.class_name for window in windows if window.visible}))
            message = (
                f"已发现 Weixin.exe 进程和可见窗口（类名：{classes}），"
                "但未找到本项目支持的微信 4.x UIA 窗口。"
                "请确认已进入聊天主界面并保持窗口打开；若已在聊天界面，"
                "需要继续检查当前版本的 UIA 兼容性或权限差异。"
            )
    if snapshot.errors:
        message += "\n诊断受限：" + "；".join(snapshot.errors) + "。"
    return message
